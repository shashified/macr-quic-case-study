import json, sys
from multiprocessing import Pool
from experiments import analyse
def job(x):
    b,p,s=x
    cfg=dict(name=f"BUF{b}", args=dict(A=(100,0.100), dwell=5.0, n_comp=0, buf_bdp=b))
    return analyse(cfg,p,s)
if __name__=="__main__":
    jobs=[(b,p,s) for b in (0.25,0.5,2.0) for p in ("RESET","RESTORE","MACR") for s in range(20)]
    res=[]
    with Pool(4) as pool:
        for r in pool.imap_unordered(job,jobs): res.extend(r)
    json.dump(res,open("sweep.json","w")); print("done",len(jobs),len(res))
