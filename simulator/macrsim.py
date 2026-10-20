# -*- coding: utf-8 -*-
"""
macrsim.py - packet-level discrete-event simulator for evaluating congestion-state
handling across QUIC-style connection migration.

Model (deliberately simple and stated in the paper):
  * Each access path = one drop-tail FIFO bottleneck (rate C, buffer B packets) plus
    fixed base RTT. Packets are enqueued at send time; departure time follows FIFO
    service; the ACK reaches the sender one base RTT (+ small jitter) after departure.
  * Senders use RFC 9002 NewReno (slow start, AIMD, one reduction per recovery period),
    packet-threshold (3) and time-threshold (9/8 RTT) loss detection, a probe timeout,
    and pacing (gain 2 in slow start, 1.25 otherwise), one ACK per packet.
  * The migrating flow ping-pongs between path A and path B. On each migration a path
    validation round trip elapses before data is sent; packets still in flight on the old
    path are excluded from congestion accounting (RFC 9000 Sec. 9.4).
  * Policies on arrival at a path:
      RESET   - RFC 9000: cwnd = IW, ssthresh = inf, RTT seeded by the validation sample.
      CARRY   - keep cwnd/ssthresh of the previous path (implementation bug class).
      RESTORE - restore cached cwnd for this path at once, unpaced, no validation.
      MACR    - cached state re-applied via RFC 9959 phases (Recon, Unvalidated,
                Validating, Safe Retreat) + lifetime + quarantine.
      MACR_NOPACE, MACR_FULLJUMP - ablations.
"""
import heapq, random, math
from collections import deque

PKT = 1500 * 8  # bits
IW = 10
INF = float("inf")


class Link:
    def __init__(self, rate_mbps, rtt, buf_pkts):
        self.set_rate(rate_mbps)
        self.rtt = rtt
        self.buf = buf_pkts
        self.departs = deque()
        self.last = 0.0
        self.last_ack = 0.0
        self.maxq_log = []  # (time, qdelay) samples on enqueue

    def set_rate(self, r):
        self.rate = r
        self.tx = PKT / (r * 1e6)

    def enqueue(self, now):
        d = self.departs
        while d and d[0] <= now:
            d.popleft()
        if len(d) >= self.buf:
            return None
        dep = max(now, self.last) + self.tx
        self.last = dep
        d.append(dep)
        self.maxq_log.append((now, dep - now - self.tx))
        return dep


class Flow:
    def __init__(self, sim, fid, link, start, policy=None, jitter=0.0005):
        self.sim, self.fid, self.policy, self.jitter = sim, fid, policy, jitter
        self.next_pn = 0
        self.sent = {}            # pn -> (send_time, epoch)
        self.order = deque()      # pns in send order (for loss detection)
        self.largest_acked = -1
        self.epoch = 0            # path epoch; acks from older epochs ignored
        self.deliveries = []      # (time, path_name) of delivered packets (departure+rtt/2)
        self.losses = []          # (time, path_name)
        self.cache = {}           # path -> dict(saved_cwnd, saved_rtt, t)
        self.quarantine = {}      # path -> until
        self.cr_log = []          # (time, path, event)
        self.attach(link, start, initial=True)

    # ---------------------------------------------------------------- state
    def reset_cc(self, rtt_sample):
        self.cwnd = float(IW); self.ssthresh = INF
        self.srtt = rtt_sample; self.rttvar = rtt_sample / 2; self.min_rtt = rtt_sample
        self.recovery_start = -1.0
        self.phase = "NORMAL"

    def attach(self, link, now, initial=False, prev=None):
        self.link = link
        self.epoch += 1
        self.inflight = 0
        self.sent.clear(); self.order.clear()
        self.largest_acked = self.next_pn - 1
        self.path_start = now
        prev_state = None if prev is None else (self.cwnd, self.ssthresh)
        self.reset_cc(link.rtt)
        self.unpaced = False
        pol = self.policy
        if not initial and pol is not None:
            ent = self.cache.get(link.name)
            fresh = ent is not None and (now - ent["t"]) < self.sim.lifetime
            quarantined = self.quarantine.get(link.name, -1) > now
            if pol == "CARRY" and prev_state is not None:
                self.cwnd, self.ssthresh = prev_state
            elif pol == "RESTORE" and fresh:
                self.cwnd = ent["saved_cwnd"]; self.ssthresh = ent["saved_cwnd"]
                self.unpaced = True
                self.unpaced_until_pn = self.next_pn + int(self.cwnd)
            elif pol.startswith("MACR") and fresh and not quarantined and ent["saved_cwnd"] >= 4 * IW:
                self.phase = "RECON"
                self.cr = dict(ent)
                self.recon_end_pn = self.next_pn + IW
                self.recon_minrtt = INF
                self.cr_log.append((now, link.name, "recon"))
        self.schedule_send(now)

    def save_state(self, now):
        """Called on departure from a path: record validated capacity (delivered in last RTT)."""
        name = self.link.name
        horizon = max(self.min_rtt, 0.01)
        win = 1.0
        n = 0
        for t, p in reversed(self.deliveries):
            if t < now - win:
                break
            if p == name:
                n += 1
        rate_pkts = n / win
        est = rate_pkts * self.min_rtt
        saved = min(self.cwnd, est) if self.ssthresh < INF else est
        self.cache[name] = dict(saved_cwnd=saved, saved_rtt=self.min_rtt, t=now)

    # ---------------------------------------------------------------- sending
    def pacing_interval(self):
        if self.unpaced and self.next_pn < getattr(self, "unpaced_until_pn", -1):
            return 0.0
        if self.phase == "UNVAL":
            if self.policy == "MACR_NOPACE":
                return 0.0
            return self.srtt / max(self.jump, 1.0)
        gain = 2.0 if self.cwnd < self.ssthresh else 1.25
        return self.srtt / (gain * max(self.cwnd, 1.0))

    def schedule_send(self, t):
        if getattr(self, "pending", None) == self.epoch:
            return
        self.pending = self.epoch
        self.sim.push(t, 0, self, ("send", self.epoch))

    def on_send(self, now, epoch):
        if epoch != self.epoch or not self.sim.active(self, now):
            return
        self.pending = None
        if self.inflight >= int(self.cwnd):
            return  # woken by ack
        pn = self.next_pn; self.next_pn += 1
        self.sent[pn] = now; self.order.append(pn); self.inflight += 1
        dep = self.link.enqueue(now)
        if dep is not None:
            ack_t = max(dep + self.link.rtt + random.uniform(0, self.jitter), self.link.last_ack)
            self.link.last_ack = ack_t
            self.sim.push(ack_t, 1, self, ("ack", self.epoch, pn, dep))
        self.sim.push(now + 3 * self.srtt + 0.2, 2, self, ("pto", self.epoch, pn))
        iv = self.pacing_interval()
        if self.inflight < int(self.cwnd):
            self.schedule_send(now + iv)
        else:
            self.next_allowed = now + iv

    # ---------------------------------------------------------------- acks
    def on_ack(self, now, epoch, pn, dep):
        if epoch != self.epoch or pn not in self.sent:
            return
        st = self.sent.pop(pn); self.inflight -= 1
        self.deliveries.append((dep + self.link.rtt / 2, self.link.name))
        r = now - st
        self.min_rtt = min(self.min_rtt, r)
        self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - r)
        self.srtt = 0.875 * self.srtt + 0.125 * r
        self.largest_acked = max(self.largest_acked, pn)
        self.cc_on_ack(now, pn, st)
        self.detect_losses(now)
        if self.inflight < int(self.cwnd):
            self.schedule_send(max(now, getattr(self, "next_allowed", now)))

    def cc_on_ack(self, now, pn, st):
        ph = self.phase
        if ph == "RECON":
            self.recon_minrtt = min(self.recon_minrtt, now - st)
            self.grow(st)
            if pn >= self.recon_end_pn - 1:
                ok = (self.cr["saved_rtt"] / 2 < self.recon_minrtt < 10 * self.cr["saved_rtt"])
                if ok:
                    self.enter_unval(now)
                else:
                    self.phase = "NORMAL"; self.cr_log.append((now, self.link.name, "rtt_fail"))
            return
        if ph == "UNVAL":
            self.pipe += 1
            if pn >= self.first_unval or self.inflight >= self.cwnd or now - self.t_unval > self.srtt:
                self.leave_unval(now)
            return
        if ph == "VALID":
            self.pipe += 1
            self.grow(st)
            if pn >= self.last_unval:
                self.phase = "NORMAL"; self.cr_log.append((now, self.link.name, "validated"))
            return
        if ph == "RETREAT":
            self.pipe += 1
            if pn >= self.last_unval:
                self.ssthresh = max(0.5 * self.pipe, 2.0)
                self.phase = "NORMAL"
            return
        self.grow(st)

    def grow(self, st):
        if st <= self.recovery_start:
            return
        if self.cwnd < self.ssthresh:
            self.cwnd += 1
        else:
            self.cwnd += 1.0 / self.cwnd

    def enter_unval(self, now):
        self.phase = "UNVAL"
        self.pipe = float(self.inflight)
        frac = 1.0 if self.policy == "MACR_FULLJUMP" else 0.5
        self.jump = min(self.sim.max_jump, frac * self.cr["saved_cwnd"])
        self.cwnd = max(self.cwnd, self.jump)
        if self.policy != "MACR_NOGUARD":
            self.ssthresh = max(self.cr["saved_cwnd"], self.jump)   # overshoot guard
        self.first_unval = self.next_pn
        self.t_unval = now
        self.cr_log.append((now, self.link.name, "jump"))

    def leave_unval(self, now):
        self.last_unval = self.next_pn - 1
        if self.inflight <= self.pipe:
            self.cwnd = max(self.pipe, IW); self.phase = "NORMAL"
            self.cr_log.append((now, self.link.name, "rate_limited"))
        else:
            self.cwnd = float(self.inflight); self.phase = "VALID"

    # ---------------------------------------------------------------- losses
    def detect_losses(self, now):
        thr_t = now - 1.125 * max(self.srtt, self.min_rtt)
        lost = []
        while self.order:
            pn = self.order[0]
            if pn not in self.sent:
                self.order.popleft(); continue
            if pn <= self.largest_acked - 3 or (pn < self.largest_acked and self.sent[pn] <= thr_t):
                self.order.popleft(); lost.append(pn)
            else:
                break
        for pn in lost:
            self.on_lost(now, pn)

    def on_lost(self, now, pn):
        st = self.sent.pop(pn, None)
        if st is None:
            return
        self.inflight -= 1
        self.losses.append((now, self.link.name))
        if self.phase in ("UNVAL", "VALID"):
            self.phase = "RETREAT"
            if not hasattr(self, "last_unval") or self.last_unval < self.first_unval:
                self.last_unval = self.next_pn - 1
            self.last_unval = max(self.last_unval, self.next_pn - 1)
            self.cwnd = max(self.pipe / 2.0, 2.0)
            self.recovery_start = now
            self.cache.pop(self.link.name, None)
            self.quarantine[self.link.name] = now + self.sim.quarantine
            self.cr_log.append((now, self.link.name, "retreat"))
            return
        if self.phase == "RETREAT":
            return
        if self.phase == "RECON":
            self.phase = "NORMAL"; self.cr_log.append((now, self.link.name, "recon_loss"))
        if st > self.recovery_start:
            self.recovery_start = now
            self.ssthresh = max(self.cwnd * 0.5, 2.0)
            self.cwnd = self.ssthresh
        if self.inflight < int(self.cwnd):
            self.schedule_send(now)

    def on_pto(self, now, epoch, pn):
        if epoch != self.epoch or pn not in self.sent:
            return
        self.order = deque(p for p in self.order if p != pn)
        self.on_lost(now, pn)
        self.schedule_send(now)


class Sim:
    def __init__(self, seed, lifetime=30.0, max_jump=1e9, quarantine=10.0):
        random.seed(seed)
        self.q = []; self.c = 0
        self.lifetime, self.max_jump, self.quarantine = lifetime, max_jump, quarantine
        self.end = INF

    def push(self, t, prio, obj, ev):
        self.c += 1
        heapq.heappush(self.q, (t, prio, self.c, obj, ev))

    def active(self, f, now):
        return now < self.end and getattr(f, "running", True)

    def run(self, until, hooks):
        hooks = sorted(hooks)
        hi = 0
        q = self.q
        while q:
            t, prio, _, obj, ev = q[0]
            if hi < len(hooks) and hooks[hi][0] <= t:
                hooks[hi][1](hooks[hi][0]); hi += 1; continue
            if t > until:
                break
            heapq.heappop(q)
            k = ev[0]
            if k == "ack":
                obj.on_ack(t, ev[1], ev[2], ev[3])
            elif k == "send":
                obj.on_send(t, ev[1])
            else:
                obj.on_pto(t, ev[1], ev[2])
        self.end = until


def run_scenario(policy, seed, A=(50, 0.040), Bp=(20, 0.060), buf_bdp=1.0, dwell=5.0,
                 cycles=3, warm=8.0, n_comp=1, degrade=None, lifetime=30.0):
    """Returns per-return metrics list and path-event info.
    degrade: None | ('rate', new_mbps) | ('comp', extra_competitors) applied at first departure from A."""
    sim = Sim(seed, lifetime=lifetime)

    def mk(rate, rtt, name):
        bdp = rate * 1e6 * rtt / PKT
        L = Link(rate, rtt, max(8, int(round(buf_bdp * bdp))))
        L.name = name
        return L
    la, lb = mk(*A, "A"), mk(*Bp, "B")
    comps = [Flow(sim, f"c{i}", la, random.uniform(0, 0.5)) for i in range(n_comp)]
    main = Flow(sim, "m", la, 0.2 + random.uniform(0, 0.3), policy=policy)
    extra = []

    t = warm
    events = []
    on_a = True
    hooks = []
    returns = []
    for k in range(2 * cycles):
        tm = t
        if on_a:
            def go_b(now, first=(k == 0)):
                main.save_state(now)
                main.running = False
                if first and degrade:
                    if degrade[0] == "rate":
                        la.set_rate(degrade[1])
                    else:
                        for j in range(degrade[1]):
                            f = Flow(sim, f"x{j}", la, now + random.uniform(0, 0.3)); extra.append(f)
                main.running = True
                main.attach_later = (lb, now + lb.rtt)
                sim.push(now + lb.rtt, 0, Migrator(main, lb, "B"), ("mig",))
            hooks.append((tm, go_b))
        else:
            def go_a(now):
                main.save_state(now)
                sim.push(now + la.rtt, 0, Migrator(main, la, "A"), ("mig",))
                main.pending_epoch_kill = True
            hooks.append((tm, go_a))
            returns.append(tm)
        events.append((tm, "B" if on_a else "A"))
        on_a = not on_a
        t += dwell
    end = t
    # migration from A at departure: stop sending on old path immediately
    sim.run(end, [(h[0], wrap_depart(main, h[1])) for h in hooks])
    return main, comps + extra, la, lb, returns, end


def wrap_depart(main, fn):
    def h(now):
        main.epoch += 1          # old-path packets no longer count
        main.inflight = 0
        main.sent.clear(); main.order.clear()
        fn(now)
    return h


class Migrator:
    def __init__(self, flow, link, name):
        self.flow, self.link = flow, link

    def on_send(self, t, *a):
        pass


# Patch the event loop to recognise 'mig' events
_orig_run = Sim.run


def _run(self, until, hooks):
    hooks = sorted(hooks, key=lambda x: x[0])
    hi = 0
    q = self.q
    while q:
        t, prio, _, obj, ev = q[0]
        if hi < len(hooks) and hooks[hi][0] <= t:
            hooks[hi][1](hooks[hi][0]); hi += 1; continue
        if t > until:
            break
        heapq.heappop(q)
        k = ev[0]
        if k == "ack":
            obj.on_ack(t, ev[1], ev[2], ev[3])
        elif k == "send":
            obj.on_send(t, ev[1])
        elif k == "pto":
            obj.on_pto(t, ev[1], ev[2])
        elif k == "mig":
            prev_link = obj.flow.link
            obj.flow.attach(obj.link, t, prev=prev_link)
    self.end = until


Sim.run = _run
