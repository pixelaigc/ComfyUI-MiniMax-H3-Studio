"""全量普查：今日每条接续链的「段2 头部断点」vs 二采开关/steps。"""
import sqlite3, json, subprocess, numpy as np, os, datetime, zoneinfo
tz=zoneinfo.ZoneInfo("Asia/Shanghai")
OUT="/root/projects/comfyui-minimax-h3-studio/data/outputs"
con=sqlite3.connect("file:/root/projects/comfyui-minimax-h3-studio/data/jobs.db?mode=ro",uri=True); con.row_factory=sqlite3.Row
rows=con.execute("SELECT id,created_at,params,outputs FROM jobs WHERE created_at>=1794931200 AND created_at<1795017600 AND mode='t2v' ORDER BY created_at").fetchall()
def frames(path,w=320,h=180):
    if not os.path.exists(path): return None
    raw=subprocess.run(["ffmpeg","-v","error","-i",path,"-vf",f"scale={w}:{h}","-pix_fmt","rgb24","-f","rawvideo","-"],capture_output=True).stdout
    n=len(raw)//(w*h*3)
    if n==0: return None
    return np.frombuffer(raw[:n*w*h*3],dtype=np.uint8).reshape(n,h,w,3).astype(np.float32)
def lineage(jobid):
    out=[];cur=jobid
    while cur:
        j=con.execute("SELECT params FROM jobs WHERE id=?",(cur,)).fetchone()
        if not j: break
        p=json.loads(j['params']); c=p.get('continuation') or {}
        if c.get('segment_rel'): out.append(c['segment_rel'])
        cur=c.get('source_job') or ''
    return list(reversed(out))
print(f"{'job':14s} {'time':6s} {'2采':4s} {'stp':4s} {'ts':3s} {'seg2头断点L1':12s} {'段内中位':9s} {'比值':7s} {'缝前ΔL':8s} {'3帧亮度降':9s} {'提示词'}")
print("-"*135)
rec=[]
for j in rows:
    p=json.loads(j['params']); outs=json.loads(j['outputs'] or '[]')
    if not any(o.get('is_full') for o in outs): continue
    segs=lineage(j['id'])
    if len(segs)<2: continue
    tp=p.get('twopass') or {}
    S1=frames(os.path.join(OUT,segs[0])); S2=frames(os.path.join(OUT,segs[1]))
    if S1 is None or S2 is None or len(S2)<12: continue
    d2=[float(np.abs(S2[i]-S2[i+1]).mean()) for i in range(len(S2)-1)]
    med=float(np.median(d2[1:])) if len(d2)>1 else float('nan')
    br=d2[0]; ratio=br/med if med>0 else float('nan')
    L1=S1.mean(axis=(1,2,3)); L2=S2.mean(axis=(1,2,3))
    seam_dL=abs(L1[-1]-L2[0])
    drop=L2[0]-L2[3]
    ca=datetime.datetime.fromtimestamp(j['created_at'],tz).strftime('%H:%M')
    print(f"{j['id']:14s} {ca:6s} {'ON' if tp.get('enabled') else 'OFF':4s} {str(p.get('steps')):4s} {str(tp.get('transition_step','-')):3s} "
          f"{br:12.4f} {med:9.4f} {ratio:6.2f}x {seam_dL:8.3f} {drop:9.3f}  {(p.get('prompt') or '')[:18]}")
    rec.append((j['id'], bool(tp.get('enabled')), p.get('steps'), ratio, br, med, drop, seam_dL))
import statistics
print()
for flag in (True,False):
    rs=[r for r in rec if r[1]==flag]
    if not rs: continue
    lbl='二采ON ' if flag else '二采OFF'
    print(f"{lbl} n={len(rs):2d}  段2头断点比 中位={statistics.median(r[3] for r in rs):6.2f}x  "
          f"最大={max(r[3] for r in rs):6.2f}x   3帧亮度降中位={statistics.median(r[6] for r in rs):7.3f}  "
          f"最大={max(r[6] for r in rs):7.3f}   缝前ΔL中位={statistics.median(r[7] for r in rs):6.3f}")
def _f(v):
    # numpy float32/float64 -> python float（否则 json.dump 抛 TypeError，
    # 导致归档的 census.json 被截断 —— 上一轮证据就是这么坏的）
    return float(v) if hasattr(v, 'item') else v
def _row(r):
    d = dict(zip(('job','twopass','steps','ratio','head_break','seg_median','drop3','seam_dL'), r))
    return {k: (_f(v) if k != 'job' else v) for k, v in d.items()}
json.dump([_row(r) for r in rec],
          open('/root/projects/h3studio-cont-seg2/evidence/evidence-20261118-seg2-std/census_baseline.json','w'), ensure_ascii=False, indent=1)
