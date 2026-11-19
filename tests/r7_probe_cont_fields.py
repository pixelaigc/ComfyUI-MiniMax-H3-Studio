#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7 探针：验证 _prepare_continuation_params 是否丢掉上游 continuation 的
关键字段（allow_twopass / ref_video_frame_count / ref_video_frame_count_probe）。

只读探针，不改任何被审文件。
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import server as srv  # noqa: E402
from h3studio import continuation as cont  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="r7-probe-"))
srv.DATA = tmp
srv.OUT_DIR = tmp / "outputs"
srv.VIDEO_DIR = srv.OUT_DIR / "videos"
srv.UPLOAD_DIR = tmp / "uploads"
srv.IMAGE_DIR = srv.OUT_DIR / "images"
srv.AUDIO_DIR = srv.OUT_DIR / "audio"
srv.DB_PATH = tmp / "jobs.db"
for d in (srv.OUT_DIR, srv.VIDEO_DIR, srv.UPLOAD_DIR, srv.IMAGE_DIR, srv.AUDIO_DIR):
    d.mkdir(parents=True, exist_ok=True)
srv.init_db()

PARAMS = {
    "prompt": "p", "model_key": "fl2va_int8", "width": 768, "height": 448,
    "duration": 5, "steps": 4, "sampler": "euler", "scheduler": "simple",
    "accel_lora": "ckpt850", "accel_lora_strength": 1.0,
    "accel_loras": [{"key": "ckpt850", "strength": 1.0, "enabled": True}],
}

# --- 造第 1 段（含潜空间文件 + 探测过的参考视频帧数）---
seg1_params = dict(PARAMS)
seg1_params["ref_videos"] = ["ref.mp4"]
seg1 = srv.create_job_record("r2v", seg1_params)
run_id = cont.segment_run_id(seg1)
stage = srv.cont_service.stage_path(run_id, 0)
stage.parent.mkdir(parents=True, exist_ok=True)
stage.write_bytes(b"fake-latent")
with srv.db() as conn:
    # 模拟 worker 探测成功后的落库状态
    p = json.loads(conn.execute("SELECT params FROM jobs WHERE id=?", (seg1,)).fetchone()["params"])
    p["continuation"]["ref_video_frame_count"] = 300
    conn.execute("UPDATE jobs SET params=? WHERE id=?",
                 (json.dumps(p, ensure_ascii=False), seg1))

# --- 造第 2 段提交体（前端回填的 continuation 来自 build_continuation_context）---
ctx = srv.cont_service.build_continuation_context("r2v", p, seg1)
print("== build_continuation_context 下发的 continuation 字段 ==")
print(sorted(ctx["continuation"].keys()))
print("  含 ref_video_frame_count ?",
      "ref_video_frame_count" in ctx["continuation"])

# --- 前端显式放行二采（web/app.js 的 allow_twopass 分支）---
ctx["continuation"]["allow_twopass"] = True

params2 = dict(PARAMS)
params2["continuation"] = dict(ctx["continuation"])
params2["ref_videos"] = ["ref.mp4"]
params2["twopass"] = {"enabled": True}

print()
print("== 提交前 params['continuation'] ==")
print(sorted(params2["continuation"].keys()))
print("  allow_twopass =", params2["continuation"].get("allow_twopass"))

err = srv._prepare_continuation_params(params2)
print()
print("== _prepare_continuation_params err ==", repr(err))
after = params2["continuation"]
print("== 归一后 params['continuation'] ==")
print(sorted(after.keys()))
print("  allow_twopass            ->", after.get("allow_twopass"))
print("  ref_video_frame_count    ->", after.get("ref_video_frame_count"))
print("  chain_total_frames       ->", after.get("chain_total_frames"))

# 归一层读到的策略（此刻 allow_twopass 还在吗？）
from h3studio import twopass_policy  # noqa: E402
print()
print("== 归一层（normalize）看到的策略 ==")
use, disp = twopass_policy.resolve_effective_twopass(params2)
print("  resolve_effective_twopass ->", use, disp)
print("  工作流层再算一次 ->", twopass_policy.resolve_effective_twopass(params2))

shutil.rmtree(tmp, ignore_errors=True)
