#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7 探针 4：接续段的「参考视频画面切片」字段是否被服务端丢掉。

【怀疑的对象】
  h3studio/h3_video_modes.py::_continuation_media_slice 读
      info["ref_video_frame_count"]（第 1 段 worker 探测出来的参考视频帧数）
  但 server.py::_prepare_continuation_params 在重建 params["continuation"]
  时只写了固定的一组字段，**没有** ref_video_frame_count。
  ⇒ 第 2 段的 C_n>0 但 ref_frames=0 -> 走「探测不到 -> 不猜」分支
    -> video_skip_frames=0 -> 参考视频又从 0 开始喂
    -> 2026-11-19 那次「画面切片与音轨解耦」的修复在真实接续链上**完全无效**。

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
from h3studio import h3_video_modes as h3m  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="r7-probe5-"))
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

BASE = {
    "prompt": "p", "model_key": "ref2va_pruned_int8", "width": 768, "height": 448,
    "duration": 5, "steps": 8, "sampler": "euler", "scheduler": "beta",
    "accel_lora": "dmad_4step_critic", "accel_lora_strength": 1.0,
    "accel_loras": [{"key": "dmad_4step_critic", "strength": 1.0, "enabled": True}],
    "ref_videos": ["ref.mp4"],
}

# ---- 第 1 段（链首）----
seg1 = srv.create_job_record("r2v", dict(BASE))
run_id = cont.segment_run_id(seg1)
stage = srv.cont_service.stage_path(run_id, 0)
stage.parent.mkdir(parents=True, exist_ok=True)
stage.write_bytes(b"fake-latent")

# 模拟 worker 探测成功：ref_video_frame_count = 300（写进库里的 params）
with srv.db() as conn:
    row = conn.execute("SELECT params FROM jobs WHERE id=?", (seg1,)).fetchone()
    p1 = json.loads(row["params"])
    p1["continuation"]["ref_video_frame_count"] = 300
    p1["continuation"]["chain_total_frames"] = 0
    p1["continuation"]["segment_frames"] = 124
    conn.execute("UPDATE jobs SET params=? WHERE id=?",
                 (json.dumps(p1, ensure_ascii=False), seg1))

print("第 1 段库里的 continuation:")
print("  ", sorted(p1["continuation"].keys()))
print("   ref_video_frame_count =", p1["continuation"].get("ref_video_frame_count"))

# ---- 前端点「接续」-> 服务端下发上下文 -> 第 2 段提交 -> 服务端归一 ----
ctx = srv.cont_service.build_continuation_context("r2v", p1, seg1)
print()
print("前端拿到的 continuation（build_continuation_context）:")
print("  ", sorted(ctx["continuation"].keys()))
print("   含 ref_video_frame_count ?",
      "ref_video_frame_count" in ctx["continuation"])

params2 = dict(BASE)
params2["continuation"] = dict(ctx["continuation"])
err = srv._prepare_continuation_params(params2)
print()
print("_prepare_continuation_params err =", repr(err))
print("归一后第 2 段 continuation:")
print("  ", sorted(params2["continuation"].keys()))
print("   ref_video_frame_count =", params2["continuation"].get("ref_video_frame_count"))
print("   chain_total_frames    =", params2["continuation"].get("chain_total_frames"))

slice_info = h3m._continuation_media_slice(params2)
print()
print("第 2 段的参考视频画面切片参数（工作流层真正读的）：")
for k in ("video_skip_frames", "video_drop", "skip_frames", "frame_cap",
          "audio_start", "active"):
    print(f"   {k:20s} = {slice_info.get(k)}")
print()
if slice_info.get("video_skip_frames") == 0 and params2["continuation"].get(
        "chain_total_frames", 0) > 0:
    print("==> 缺陷复现：第 2 段 C_n > 0，但参考视频**画面不跳帧**")
    print("    （2026-11-19 的「按 C_n 跳帧」修复在真实接续链上不生效）")
    print("    video_drop 也为 False => 参考视频**照旧从 0 开始喂**")
    print("    => 第 3 轮那个「亮度阶跃」缺陷的触发条件仍然成立。")

shutil.rmtree(tmp, ignore_errors=True)
