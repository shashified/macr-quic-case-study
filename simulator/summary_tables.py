"""Prints the headline tables used in the report (Part B.4)."""
import json, numpy as np
from scipy.stats import wilcoxon
R=[r for r in json.load(open("results.json")) if "error" not in r]
def med(s,p,k): return float(np.median([r[k] for r in R if r["scn"]==s and r["policy"]==p]))
print("\nTABLE B4 - median outcomes per return migration (first 2 s)")
print(f'{"Scenario":10s}{"Policy":9s}{"T90(s)":>8s}{"MB2":>8s}{"OwnLoss":>9s}{"CompLoss":>10s}{"PeakQ(ms)":>11s}')
for s in ["S1_d5","S2_hbdp","S3_cont","S4_join","S5_rate"]:
    for p in ["RESET","CARRY","RESTORE","MACR"]:
        cl = med(s,p,"comp_loss") if s in ("S3_cont","S4_join") else float("nan")
        print(f'{s:10s}{p:9s}{med(s,p,"t90"):8.2f}{med(s,p,"mb2"):8.2f}{med(s,p,"main_loss"):9.0f}{cl:10.1f}{med(s,p,"qpeak_ms"):11.1f}')
def seedmed(s,p,k):
    o={}
    for r in R:
        if r["scn"]==s and r["policy"]==p: o.setdefault(r["seed"],[]).append(r[k])
    return [np.median(o[i]) for i in sorted(o)]
print("\nSeed-level Wilcoxon signed-rank tests, MACR vs RESET (n=20 seeds)")
for s,k in [("S1_d5","qpeak_ms"),("S2_hbdp","t90"),("S2_hbdp","mb2"),("S3_cont","mb2"),("S4_join","main_loss"),("S4_join","comp_loss")]:
    try: pv=wilcoxon(seedmed(s,"MACR",k),seedmed(s,"RESET",k),zero_method="pratt").pvalue
    except ValueError: pv=float("nan")
    print(f"  {s:8s} {k:10s} p = {pv:.2g}")
