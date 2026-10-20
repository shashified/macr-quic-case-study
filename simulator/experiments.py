import sys, json, time, bisect
from multiprocessing import Pool
from macrsim import run_scenario, PKT

BIN = 0.1


def series(times, t0, t1):
    nb = int(round((t1 - t0) / BIN))
    b = [0] * nb
    for t in times:
        if t0 <= t < t1:
            b[int((t - t0) / BIN)] += 1
    return [x * PKT / BIN / 1e6 for x in b]  # Mb/s per bin


def perturb(args, seed):
    import random as R
    r = R.Random(1000 + seed)
    a = dict(args)
    A = a.get("A", (50, 0.040)); B = a.get("Bp", (20, 0.060))
    a["A"] = (A[0] * r.uniform(0.9, 1.1), A[1] * r.uniform(0.9, 1.1))
    a["Bp"] = (B[0] * r.uniform(0.9, 1.1), B[1] * r.uniform(0.9, 1.1))
    a["buf_bdp"] = a.get("buf_bdp", 1.0) * r.uniform(0.8, 1.2)
    return a


def analyse(cfg, policy, seed):
    main, comps, la, lb, returns, end = run_scenario(policy, seed, **perturb(cfg["args"], seed))
    dwell = cfg["args"].get("dwell", 5.0)
    out = []
    mA = sorted(t for t, p in main.deliveries if p == "A")
    cdel = sorted(t for f in comps for t, p in f.deliveries if p == "A")
    closs = sorted(t for f in comps for t, p in f.losses)
    mloss = sorted(t for t, p in main.losses if p == "A")
    qlog = la.maxq_log
    qt = [x[0] for x in qlog]
    ncomp = len(comps)
    for tm in returns:
        fair = la.rate / (1 + ncomp)
        s = series(mA, tm, tm + dwell)
        t90 = dwell
        for i in range(1, len(s)):
            if (s[i] + s[i - 1]) / 2 >= 0.9 * fair:
                t90 = (i + 1) * BIN
                break
        mb1 = sum(s[:10]) * BIN / 8
        mb2 = sum(s[:20]) * BIN / 8
        deficit = fair * 2 / 8 - mb2
        cs = series(cdel, tm, tm + 2.0)
        cfair = la.rate * ncomp / (1 + ncomp)
        st = int((la.rtt + 0.2) / BIN)
        cmin = -1.0 if not ncomp else min((cs[i] + cs[i - 1]) / 2 for i in range(max(st, 1), len(cs))) / cfair
        i0 = bisect.bisect_left(qt, tm); i1 = bisect.bisect_left(qt, tm + 2.0)
        qpk = max((qlog[i][1] for i in range(i0, i1)), default=0.0) * 1000
        cl = bisect.bisect_left(closs, tm + 2.0) - bisect.bisect_left(closs, tm)
        ml = bisect.bisect_left(mloss, tm + 2.0) - bisect.bisect_left(mloss, tm)
        ev = [e for (t, p, e) in main.cr_log if tm <= t < tm + dwell and p == "A"]
        out.append(dict(scn=cfg["name"], policy=policy, seed=seed, t_mig=tm, t90=t90, mb1=mb1,
                        mb2=mb2, deficit=deficit, comp_min=cmin, qpeak_ms=qpk,
                        comp_loss=cl, main_loss=ml, cr=ev, fair=fair))
    return out


SCN = {
 "S1_d2":  dict(name="S1_d2", args=dict(dwell=2.0, n_comp=0)),
 "S1_d5":  dict(name="S1_d5", args=dict(dwell=5.0, n_comp=0)),
 "S1_d10": dict(name="S1_d10", args=dict(dwell=10.0, n_comp=0)),
 "S2_hbdp": dict(name="S2_hbdp", args=dict(A=(100, 0.100), dwell=5.0, n_comp=0)),
 "S3_cont": dict(name="S3_cont", args=dict(dwell=5.0, n_comp=1)),
 "S4_join": dict(name="S4_join", args=dict(dwell=5.0, n_comp=0, degrade=("comp", 2))),
 "S5_rate": dict(name="S5_rate", args=dict(dwell=5.0, n_comp=0, degrade=("rate", 20))),
}
POL = ["RESET", "CARRY", "RESTORE", "MACR"]
ABL = ["MACR_NOPACE", "MACR_FULLJUMP", "MACR_NOGUARD"]


def job(x):
    k, p, s = x
    try:
        return analyse(SCN[k], p, s)
    except Exception as e:
        return [dict(error=repr(e), scn=k, policy=p, seed=s)]


if __name__ == "__main__":
    seeds = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    jobs = [(k, p, s) for k in SCN for p in POL for s in range(seeds)]
    jobs += [(k, p, s) for k in ("S2_hbdp", "S4_join") for p in ABL for s in range(seeds)]
    t0 = time.time()
    res = []
    with Pool(2) as pool:
        for r in pool.imap_unordered(job, jobs, chunksize=1):
            res.extend(r)
    json.dump(res, open("results.json", "w"))
    print("runs", len(jobs), "rows", len(res), "secs", round(time.time() - t0))
