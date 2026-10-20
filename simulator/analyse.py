import json, statistics as st, random
import numpy as np
from scipy.stats import mannwhitneyu
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

R = [r for r in json.load(open("results.json")) if "error" not in r]
errs = [r for r in json.load(open("results.json")) if "error" in r]
print("rows", len(R), "errors", len(errs), errs[:2])


def sel(scn, pol, key):
    return [r[key] for r in R if r["scn"] == scn and r["policy"] == pol]


def boot(x, n=2000):
    rng = np.random.default_rng(0)
    x = np.array(x)
    meds = [np.median(rng.choice(x, len(x))) for _ in range(n)]
    return np.median(x), np.percentile(meds, 2.5), np.percentile(meds, 97.5)


SC = ["S1_d2", "S1_d5", "S1_d10", "S2_hbdp", "S3_cont", "S4_join", "S5_rate"]
POL = ["RESET", "CARRY", "RESTORE", "MACR"]
out = {}
for s in SC:
    for p in POL + ["MACR_NOPACE", "MACR_FULLJUMP", "MACR_NOGUARD"]:
        d = [r for r in R if r["scn"] == s and r["policy"] == p]
        if not d:
            continue
        g = lambda k: [r[k] for r in d]
        m = {}
        for k in ["t90", "mb1", "mb2", "main_loss", "comp_loss", "qpeak_ms", "comp_min", "deficit"]:
            med, lo, hi = boot(g(k))
            m[k] = (round(float(med), 3), round(float(lo), 3), round(float(hi), 3), round(float(np.mean(g(k))), 3))
        cr = [r["cr"] for r in d]
        m["n"] = len(d)
        m["retreat_rate"] = round(sum(1 for c in cr if "retreat" in c) / len(d), 3)
        m["jump_rate"] = round(sum(1 for c in cr if "jump" in c) / len(d), 3)
        m["recon_fail"] = round(sum(1 for c in cr if ("recon_loss" in c or "rtt_fail" in c)) / len(d), 3)
        out[f"{s}|{p}"] = m
# significance tests vs RESET
tests = {}
for s in SC:
    for p in ["MACR", "RESTORE", "CARRY"]:
        for k in ["t90", "mb2", "main_loss", "comp_loss", "qpeak_ms"]:
            a, b = sel(s, p, k), sel(s, "RESET", k)
            if a and b:
                try:
                    tests[f"{s}|{p}|{k}"] = float(mannwhitneyu(a, b).pvalue)
                except ValueError:
                    tests[f"{s}|{p}|{k}"] = 1.0
json.dump(dict(stats=out, tests=tests), open("summary.json", "w"), indent=1)
for k, v in out.items():
    print(k, {x: v[x] for x in ["n", "t90", "mb2", "main_loss", "comp_loss", "qpeak_ms", "comp_min", "retreat_rate", "jump_rate"]})
for k, v in tests.items():
    if v < 0.05 or "MACR|t90" in k:
        pass
print({k: round(v, 5) for k, v in tests.items() if "MACR" in k})

# ---------------- figures (IEEE single-column width ~3.5in)
plt.rcParams.update({"font.family": "serif", "font.size": 8, "axes.linewidth": 0.6,
                     "pdf.fonttype": 42})
COL = {"RESET": "#4C72B0", "CARRY": "#DD8452", "RESTORE": "#C44E52", "MACR": "#55A868"}
# Fig: median T90 + 95% CI per scenario
fig, ax = plt.subplots(figsize=(3.45, 1.9))
scn_plot = ["S1_d2", "S1_d5", "S1_d10", "S2_hbdp", "S3_cont", "S4_join", "S5_rate"]
lab = ["D=2s", "D=5s", "D=10s", "High\nBDP", "Cont-\nended", "Flows\njoin", "Rate\ndrop"]
w = 0.2
for i, p in enumerate(POL):
    meds, lo, hi = [], [], []
    for s in scn_plot:
        m = out[f"{s}|{p}"]["t90"]
        meds.append(m[0]); lo.append(m[0] - m[1]); hi.append(m[2] - m[0])
    x = np.arange(len(scn_plot)) + (i - 1.5) * w
    ax.bar(x, meds, w, yerr=[lo, hi], color=COL[p], label=p, error_kw=dict(lw=0.6, capsize=1.5))
ax.set_xticks(np.arange(len(scn_plot))); ax.set_xticklabels(lab, fontsize=6.5)
ax.set_ylabel("Median T90 (s)")
ax.legend(ncol=4, fontsize=6, frameon=False, loc="upper center", bbox_to_anchor=(0.5, 1.2))
ax.grid(axis="y", lw=0.3)
fig.tight_layout(pad=0.2)
fig.savefig("fig_t90.png", dpi=200)

# Fig: time series of goodput after one return in S2 (seed 0, 2nd return)
from experiments import SCN, perturb, series
from macrsim import run_scenario
fig, ax = plt.subplots(figsize=(3.45, 1.7))
for p in ["RESET", "RESTORE", "MACR"]:
    main, comps, la, lb, rets, end = run_scenario(p, 0, **perturb(SCN["S2_hbdp"]["args"], 0))
    tm = rets[1]
    mA = sorted(t for t, q in main.deliveries if q == "A")
    s = series(mA, tm, tm + 2.0)
    ax.plot(np.arange(len(s)) * 0.1 + 0.1, s, marker="o", ms=2, lw=1, color=COL[p], label=p)
ax.axhline(la.rate, ls="--", lw=0.6, color="grey")
ax.text(0.05, la.rate * 1.02, "path capacity", fontsize=6, color="grey")
ax.set_ylim(0, la.rate * 1.12); ax.set_xlabel("Time since migration back to path A (s)"); ax.set_ylabel("Goodput (Mb/s)")
ax.legend(fontsize=6, frameon=False, loc="lower right"); ax.grid(lw=0.3)
fig.tight_layout(pad=0.2); fig.savefig("fig_ts.png", dpi=200)

# Fig: harm in S4 (competitor losses and main losses)
fig, axs = plt.subplots(1, 2, figsize=(3.45, 1.6))
for j, (k, yl) in enumerate([("comp_loss", "Competitor losses (2 s)"), ("main_loss", "Migrating-flow losses (2 s)")]):
    data = [sel("S4_join", p, k) for p in POL]
    bp = axs[j].boxplot(data, widths=0.5, patch_artist=True, showfliers=False)
    for patch, p in zip(bp["boxes"], POL):
        patch.set_facecolor(COL[p]); patch.set_alpha(0.8)
    axs[j].set_xticks(range(1, 5)); axs[j].set_xticklabels(["RST", "CAR", "RES", "MACR"], fontsize=6)
    axs[j].set_title(yl, fontsize=7); axs[j].grid(axis="y", lw=0.3)
fig.tight_layout(pad=0.2); fig.savefig("fig_harm.png", dpi=200)
print("figs done")
