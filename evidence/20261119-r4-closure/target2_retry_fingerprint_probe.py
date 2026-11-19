# -*- coding: utf-8 -*-
"""目标2 生产真实性复核：Relay-Kit 用 os.replace 原子写 => 源文件换 inode。
硬链接副本因此保留**旧内容**。retry 路径沿用旧 source_latent_path => 读回旧 latent。"""
import json, os, shutil, sys, tempfile
from pathlib import Path

REPO = Path("/root/projects/h3s-merge-20261119")
sys.path.insert(0, str(REPO))
import server as srv
from h3studio import continuation_service as cs

tmp = tempfile.mkdtemp(prefix="r3t2-")
outdir = Path(tmp) / "output"; outdir.mkdir(parents=True, exist_ok=True)
srv.OUT_DIR = outdir
cs.comfy_output_dir = lambda: outdir
srv.DB_PATH = Path(tmp) / "jobs.db"; srv.init_db()

up = srv.create_job_record("t2v", {"prompt": "x", "duration": 5})
with srv.db() as conn:
    up_params = json.loads(conn.execute("SELECT params FROM jobs WHERE id=?", (up,)).fetchone()["params"])
up_run = up_params["continuation"]["latent_run_id"]

stage = outdir / "relay_kit" / up_run / "stage_00000.safetensors"
stage.parent.mkdir(parents=True, exist_ok=True)
stage.write_bytes(b"VERSION-ONE" * 500)

# 首次接续（正常路径）
p_cont = {"prompt": "n", "duration": 5, "continuation": {
    "chain_id": up, "stage_index": 1, "source_job": up, "source_segment": 0,
    "source_latent_run_id": up_run, "source_latent_stage": 0,
    "context_frames": 22, "anchor_frames": 22,
    "chain_total_frames": 0, "segment_frames": 124}}
err = srv._prepare_continuation_params(p_cont)
path_v1 = p_cont["continuation"]["source_latent_path"]

# 上游被重跑：Relay-Kit 的原子写 = os.replace(tmp, path) => 新 inode
tmpfile = stage.with_suffix(".safetensors.tmp")
tmpfile.write_bytes(b"VERSION-TWO-NEW-INODE" * 500)
os.replace(tmpfile, stage)     # <- 与 Relay-Kit save_av_latent 完全同口径

st = os.stat(path_v1); ss = os.stat(stage)
print("副本 inode:", st.st_ino, " 源 inode:", ss.st_ino, " 同 inode?", st.st_ino == ss.st_ino)
print("副本内容头:", open(path_v1,'rb').read(30).decode())
print("源内容头  :", open(stage,'rb').read(30).decode())

# 新的一次接续（正常路径）—— 应当拿到新指纹
p_cont2 = {"prompt": "n", "duration": 5, "continuation": {
    "chain_id": up, "stage_index": 1, "source_job": up, "source_segment": 0,
    "source_latent_run_id": up_run, "source_latent_stage": 0,
    "context_frames": 22, "anchor_frames": 22,
    "chain_total_frames": 0, "segment_frames": 124}}
srv._prepare_continuation_params(p_cont2)
path_v2 = p_cont2["continuation"]["source_latent_path"]
print("新接续路径:", os.path.basename(path_v2))
print("新接续副本内容头:", open(path_v2,'rb').read(30).decode())

# === retry 路径：整份拷贝源段 params（Agent retry_job 的做法） ===
retry_params = json.loads(json.dumps(p_cont["continuation"]))
retry_params = {"prompt": "n", "duration": 5, "continuation": retry_params}
srv.prepare_job_params("t2v", retry_params, [], None)
rp = retry_params["continuation"]
print("\nRETRY 沿用的 source_latent_path:", os.path.basename(rp.get("source_latent_path") or ""))
print("RETRY 沿用副本内容头:", open(rp["source_latent_path"],'rb').read(30).decode())
print("=> retry 读到的是旧版 latent？", open(rp['source_latent_path'],'rb').read(30) != open(stage,'rb').read(30))
print("=> 当前正确路径应是:", os.path.basename(path_v2))

shutil.rmtree(tmp, ignore_errors=True)
