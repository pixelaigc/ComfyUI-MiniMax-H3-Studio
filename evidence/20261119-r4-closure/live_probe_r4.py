# -*- coding: utf-8 -*-
"""线上实证（第4轮）：在 **live 服务进程内**验证两个修复真的生效。

不碰真实 ComfyUI、不建真实任务 —— 只在 live 的模块命名空间里
用假对象调生产代码本身（server.refresh_fingerprinted_path /
Worker._record_ref_video_frame_count）。
"""
import json, os, sys, tempfile, asyncio, shutil
from pathlib import Path

LIVE = "/root/projects/h3studio-live"
sys.path.insert(0, LIVE)
import server as srv
from h3studio import continuation_service as cs

out = {"live_root": LIVE, "live_server_py": srv.__file__}

tmp = tempfile.mkdtemp(prefix="r4live-")
outdir = Path(tmp) / "output"; outdir.mkdir(parents=True, exist_ok=True)
orig_out = srv.OUT_DIR
orig_comfy = cs.comfy_output_dir
orig_db = srv.DB_PATH
srv.OUT_DIR = outdir
cs.comfy_output_dir = lambda: outdir
srv.DB_PATH = Path(tmp) / "jobs.db"
srv.init_db()

# ---------- 目标2：retry 指纹刷新（线上模块本体）----------
up = srv.create_job_record("t2v", {"prompt": "x", "duration": 5})
with srv.db() as conn:
    up_params = json.loads(conn.execute("SELECT params FROM jobs WHERE id=?",
                                        (up,)).fetchone()["params"])
up_run = up_params["continuation"]["latent_run_id"]
stage = outdir / "relay_kit" / up_run / "stage_00000.safetensors"
stage.parent.mkdir(parents=True, exist_ok=True)
stage.write_bytes(b"LIVE-VERSION-ONE" * 500)

cont = {"chain_id": up, "stage_index": 1, "source_job": up, "source_segment": 0,
        "source_latent_run_id": up_run, "source_latent_stage": 0,
        "context_frames": 22, "anchor_frames": 22,
        "chain_total_frames": 0, "segment_frames": 124}
p = {"prompt": "n", "duration": 5, "continuation": dict(cont)}
srv._prepare_continuation_params(p)
path_v1 = p["continuation"]["source_latent_path"]

# 上游重跑（原子写 -> 换 inode）
tmpf = stage.with_suffix(".safetensors.tmp")
tmpf.write_bytes(b"LIVE-VERSION-TWO" * 500)
os.replace(tmpf, stage)

retry_params = {"prompt": "n", "duration": 5,
                "continuation": dict(p["continuation"])}
before = retry_params["continuation"]["source_latent_path"]
refreshed = srv.refresh_fingerprinted_path(retry_params)
after = refreshed
out["target2_live"] = {
    "path_before_refresh": os.path.basename(before),
    "path_after_refresh": os.path.basename(after),
    "path_changed": before != after,
    "refreshed_content_head": open(after, "rb").read(24).decode("utf-8", "replace"),
    "source_content_head": open(stage, "rb").read(24).decode("utf-8", "replace"),
    "refreshed_matches_source": open(after, "rb").read(24) == open(stage, "rb").read(24),
}

# ---------- 目标3：失败结果缓存（线上模块本体）----------
calls = []
orig_probe = srv._probe_video_frame_count
srv._probe_video_frame_count = lambda path: (calls.append(str(path)), 0)[1]
inp = Path("/home/ubuntu/comfyui/input/h3studio"); inp.mkdir(parents=True, exist_ok=True)
fake = inp / "r4live_ref.mp4"; fake.write_bytes(b"X" * 500)
info = {"chain_id": "C", "stage_index": 0, "source_job": "", "source_segment": -1,
        "context_frames": 22, "anchor_frames": 22,
        "chain_total_frames": 0, "segment_frames": 124}
p3 = {"prompt": "x", "duration": 5, "continuation": dict(info),
      "ref_videos": ["h3studio/r4live_ref.mp4"]}
job3 = srv.create_job_record("r2v", {"prompt": "x", "duration": 5})
w = srv.Worker.__new__(srv.Worker)
async def run3():
    for _ in range(3):
        await srv.Worker._record_ref_video_frame_count(w, job3, p3)
asyncio.run(run3())
with srv.db() as conn:
    stored = json.loads(conn.execute("SELECT params FROM jobs WHERE id=?",
                                     (job3,)).fetchone()["params"])
srv._probe_video_frame_count = orig_probe
out["target3_live"] = {
    "probe_calls_for_3_invocations": len(calls),
    "marker_in_params": p3["continuation"].get("ref_video_frame_count_probe"),
    "marker_in_db": (stored.get("continuation") or {}).get("ref_video_frame_count_probe"),
    "frame_count_still_zero": int(p3["continuation"].get("ref_video_frame_count") or 0) == 0,
}

fake.unlink(missing_ok=True)
srv.DB_PATH = orig_db
cs.comfy_output_dir = orig_comfy
srv.OUT_DIR = orig_out
shutil.rmtree(tmp, ignore_errors=True)
print(json.dumps(out, ensure_ascii=False, indent=2))
