
# -*- coding: utf-8 -*-
"""目标3 继承性取证：ref_video_frame_count 会不会被下一段继承？"""
import json, os, shutil, sys, tempfile, asyncio
from pathlib import Path

REPO = Path("/root/projects/h3s-merge-20261119")
sys.path.insert(0, str(REPO))
import server as srv
from h3studio import continuation as cont
from h3studio import continuation_service as cs

tmp = tempfile.mkdtemp(prefix="r3t3i-")
outdir = Path(tmp)/"output"; outdir.mkdir(parents=True, exist_ok=True)
srv.OUT_DIR = outdir
cs.comfy_output_dir = lambda: outdir
srv.DB_PATH = Path(tmp)/"jobs.db"; srv.init_db()

# 真实 input 文件
inp = Path("/home/ubuntu/comfyui/input/h3studio"); inp.mkdir(parents=True, exist_ok=True)
fake = inp/"r3inh_ref.mp4"; fake.write_bytes(b"X"*1000)

calls = []
srv._probe_video_frame_count = lambda p: (calls.append(str(p)), 77)[1]

# ---- 段1（链首）----
head = srv.create_job_record("r2v", {"prompt":"p","duration":5})
info1 = {"chain_id": head, "stage_index":0, "source_job":"", "source_segment":-1,
         "context_frames":22, "anchor_frames":22,
         "chain_total_frames":0, "segment_frames":124}
p1 = {"prompt":"p","duration":5,"continuation":dict(info1),
      "ref_videos":["h3studio/r3inh_ref.mp4"]}
w = srv.Worker.__new__(srv.Worker)
asyncio.run(srv.Worker._record_ref_video_frame_count(w, head, p1))
print("段1 探测后 continuation:", {k:v for k,v in p1["continuation"].items() if "ref_video" in k or k in ("chain_id","stage_index")})

# ---- 段2（接续段1）----
# 上游潜空间落盘
up_run = p1["continuation"].get("latent_run_id") or cont.segment_run_id(head)
stage = outdir/"relay_kit"/up_run/"stage_00000.safetensors"
stage.parent.mkdir(parents=True, exist_ok=True); stage.write_bytes(b"L"*500)
# 段1 自己的 continuation 落库（worker 会写回）
with srv.db() as conn:
    conn.execute("UPDATE jobs SET params=? WHERE id=?", (json.dumps(p1, ensure_ascii=False), head))

p2 = {"prompt":"p2","duration":5,"continuation":{
      "chain_id": head, "stage_index":1, "source_job": head, "source_segment":0,
      "source_latent_run_id": up_run, "source_latent_stage":0,
      "context_frames":22,"anchor_frames":22,
      "chain_total_frames":0,"segment_frames":124}}
err = srv._prepare_continuation_params(p2)
print("段2 prepare err:", err)
print("段2 continuation 里的 ref 字段:",
      {k:v for k,v in p2["continuation"].items() if "ref_video" in k})
print("=> ref_video_frame_count 是否被继承:", "ref_video_frame_count" in p2["continuation"])
print("段2 prepare 期间的 probe 调用数:", len(calls))
asyncio.run(srv.Worker._record_ref_video_frame_count(w, "SEG2", p2))
print("段2 worker 探测后:", {k:v for k,v in p2["continuation"].items() if "ref_video" in k})
print("总 probe 调用数:", len(calls))

# 段3
p3 = {"prompt":"p3","duration":5,"continuation":{
      "chain_id": head, "stage_index":2, "source_job": "SEG2", "source_segment":1,
      "source_latent_run_id": cont.segment_run_id("SEG2"), "source_latent_stage":0,
      "context_frames":22,"anchor_frames":22,
      "chain_total_frames":0,"segment_frames":124}}
# SEG2 没落库 -> prepare 会失败；跳过，直接看继承逻辑

# 关键：段2 的 params 落库后，段3 从它继承吗？
with srv.db() as conn:
    conn.execute("INSERT OR REPLACE INTO jobs (id,created_at,mode,params,status) VALUES (?,?,?,?,?)",
                 ("SEG2", 0, "r2v", json.dumps(p2, ensure_ascii=False), "done"))
print("\n--- 结论 ---")
print("段2 params.continuation 键:", sorted(p2["continuation"].keys()))
print("含 ref_video_frame_count:", "ref_video_frame_count" in p2["continuation"])

fake.unlink(missing_ok=True)
shutil.rmtree(tmp, ignore_errors=True)
