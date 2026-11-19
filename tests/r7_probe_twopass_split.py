#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7 探针 2：端到端复现「接续段显式放行二采」被服务端静默丢弃。

链路（与线上真实顺序一致）：
  面板提交体 (params.continuation.allow_twopass=true, twopass.enabled=true)
    -> normalize_t2v_params   （归一层：读 allow_twopass -> policy=warn -> 记 normalize_notes）
    -> _prepare_continuation_params （重建 continuation -> allow_twopass 被丢掉）
    -> 落库（存的是重建后的 continuation）
    -> worker build_workflow  （工作流层：再读一次 allow_twopass -> 没有 -> deny）

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
from h3studio import twopass_policy  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="r7-probe2-"))
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
    "prompt": "p", "model_key": "fl2va_int8", "width": 768, "height": 448,
    "duration": 5, "steps": 4, "sampler": "euler", "scheduler": "simple",
    "accel_lora": "ckpt850", "accel_lora_strength": 1.0,
    "accel_loras": [{"key": "ckpt850", "strength": 1.0, "enabled": True}],
}

# 第 1 段（链首）
seg1 = srv.create_job_record("t2v", dict(BASE))
run_id = cont.segment_run_id(seg1)
stage_path = srv.cont_service.stage_path(run_id, 0)
stage_path.parent.mkdir(parents=True, exist_ok=True)
stage_path.write_bytes(b"fake-latent")

# 第 2 段的提交体：前端回填的 continuation + 用户显式勾了「接续段也用二采」
ctx = srv.cont_service.build_continuation_context("t2v", {"continuation": {
    "chain_id": seg1, "stage_index": 0, "source_job": "",
    "source_segment": -1, "latent_run_id": run_id, "latent_stage": 0,
    "context_frames": 22, "anchor_frames": 22,
    "chain_total_frames": 0, "segment_frames": 124,
}}, seg1)
params2 = dict(BASE)
params2["continuation"] = dict(ctx["continuation"])
params2["continuation"]["allow_twopass"] = True   # web/app.js 的显式放行分支
params2["twopass"] = {"enabled": True}

print("=" * 72)
print("步骤 1：面板提交体")
print("  continuation.allow_twopass =",
      params2["continuation"].get("allow_twopass"))
print("  twopass.enabled            =", params2["twopass"].get("enabled"))

print()
print("步骤 2：归一层 normalize_t2v_params（线上顺序：在 _prepare 之前）")
normalized, err = srv.normalize_t2v_params(params2, mode="t2v")
print("  err =", repr(err))
print("  normalize_notes =", normalized.get("normalize_notes"))
print("  归一后 twopass.enabled =", (normalized.get("twopass") or {}).get("enabled"))
params2 = normalized

print()
print("步骤 3：_prepare_continuation_params（重建 continuation）")
err = srv._prepare_continuation_params(params2)
print("  err =", repr(err))
print("  重建后 continuation 的键 =", sorted(params2["continuation"].keys()))
print("  allow_twopass 是否还在 =",
      "allow_twopass" in params2["continuation"])

print()
print("步骤 4：落库（create_job_record 存的就是重建后的 params）")
job2 = srv.create_job_record("t2v", params2)
with srv.db() as conn:
    stored = json.loads(conn.execute(
        "SELECT params FROM jobs WHERE id=?", (job2,)).fetchone()["params"])
print("  落库后 continuation 的键 =", sorted(stored["continuation"].keys()))
print("  落库后 twopass =", stored.get("twopass"))
print("  落库后 normalize_notes =", stored.get("normalize_notes"))

print()
print("步骤 5：worker 的工作流层（build_workflow 读落库 params）")
use_wf, disp_wf = twopass_policy.resolve_effective_twopass(stored)
print(f"  工作流层 resolve_effective_twopass -> use_twopass={use_wf} disposition={disp_wf}")

print()
print("=" * 72)
use_norm, disp_norm = twopass_policy.resolve_effective_twopass(params2)
print(f"分裂结果：归一层 disposition={disp_norm}（用户被告知已走二采）")
print(f"          工作流层 disposition={disp_wf}（实际走标准采样）")
if disp_norm == "warn" and disp_wf == "deny":
    print("  ==> 缺陷复现：用户显式勾选并确认后，二采**没有**生效；"
          "且 normalize_notes 谎称「走了二采路径」。")

shutil.rmtree(tmp, ignore_errors=True)
