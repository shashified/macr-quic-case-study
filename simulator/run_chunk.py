"""Run one scenario's jobs (or merge all parts). Same jobs and seeds as experiments.py.
Usage: python3 run_chunk.py S1_d2   ...   python3 run_chunk.py merge"""
import sys, json, glob, time
from multiprocessing import Pool
from experiments import SCN, POL, ABL, job

SEEDS = 20
if __name__ == "__main__":
    k = sys.argv[1]
    if k == "merge":
        res = []
        for f in sorted(glob.glob("part_*.json")):
            res.extend(json.load(open(f)))
        json.dump(res, open("results.json", "w"))
        print("merged rows", len(res))
        sys.exit()
    jobs = [(k, p, s) for p in POL for s in range(SEEDS)]
    if k in ("S2_hbdp", "S4_join"):
        jobs += [(k, p, s) for p in ABL for s in range(SEEDS)]
    t0 = time.time(); res = []
    print(f"Scenario {k}: {len(jobs)} runs (policies x 20 seeds), 3 return migrations each\n", flush=True)
    with Pool(4) as pool:
        for i, r in enumerate(pool.imap(job, jobs, chunksize=1), 1):
            res.extend(r)
            sc, pol, seed = jobs[i - 1]
            t90 = sum(x["t90"] for x in r) / len(r)
            loss = sum(x["main_loss"] for x in r) / len(r)
            q = sum(x["qpeak_ms"] for x in r) / len(r)
            bar = "#" * int(30 * i / len(jobs))
            print(f"[{bar:<30}] {i:3d}/{len(jobs)}  {pol:<13} seed {seed:2d}  "
                  f"T90 {t90:4.2f}s  losses {loss:6.0f}  peak queue {q:6.1f} ms", flush=True)
    json.dump(res, open(f"part_{k}.json", "w"))
    print(k, "runs", len(jobs), "rows", len(res), "secs", round(time.time() - t0))
