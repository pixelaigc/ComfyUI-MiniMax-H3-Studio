#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7 探针 5：端到端证明「retry 后加速 LoRA 被静默丢弃」。

链路（与 Agent 的 retry_job 工具完全一致）：
  源任务落库 params（worker 已回写过 artifact_meta）
    -> retry_job 整份拷贝 params
    -> server.prepare_job_params
    -> worker: build_workflow(mode, params)
    -> 查工作流里到底有没有加速 LoRA 节点

只读探针（不建任务、不连 ComfyUI），不���任何被审文件。
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import server as srv  # noqa: E402
from h3studio import artifact_meta  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="r7-probe7-"))
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

# ---- 源任务：面板正常提交（friendly key）→ worker 回写 artifact_meta ----
panel_params = {
    "prompt": "p", "model_key": "fl2va_pruned_int8",
    "width": 1088, "height": 1920, "duration": 5,
    "steps": 8, "sampler": "euler", "scheduler": "beta",
    "accel_lora": "dmad_4step_critic", "accel_lora_strength": 1.0,
    "accel_loras": [{"key": "dmad_4step_critic", "strength": 1.0,
                     "enabled": True}],
}
job_id = srv.create_job_record("t2v", dict(panel_params))
with srv.db() as conn:
    row = conn.execute("SELECT params FROM jobs WHERE id=?", (job_id,)).fetchone()
    stored = json.loads(row["params"])

# worker 回写（模拟真实工作流里挂了 DMAD 那一枚）
artifact_meta.apply_to_params(stored, {
    "steps": 8, "scheduler": "beta", "sampler": "euler", "cfg": 1.0,
    "seed": 7, "width": 1088, "height": 1920,
    "model_file": "minimax_h3_fl2va_pruned_int8.safetensors",
    "loras": [{
        "name": "dmad_minimax_h3_4step_full_critic_comfyui",
        "raw_name": "dmad_minimax_h3_4step_full_critic_comfyui.safetensors",
        "strength": 1.0, "loader": "LoraLoaderModelOnly",
    }],
})
with srv.db() as conn:
    conn.execute("UPDATE jobs SET params=? WHERE id=?",
                 (json.dumps(stored, ensure_ascii=False), job_id))

print("源任务落库后的 params（worker 回写之后）：")
print("   accel_lora       =", repr(stored.get("accel_lora")))
print("   fixed_accel_lora =", repr(stored.get("fixed_accel_lora")))
print("   steps            =", stored.get("steps"))
print()

# ---- retry_job：整份拷贝 params + prepare_job_params ----
retry_params = dict(stored)
srv.prepare_job_params("t2v", retry_params, None, None)
print("retry 后（prepare_job_params 之后）:")
print("   accel_lora =", repr(retry_params.get("accel_lora")))
print("   steps      =", retry_params.get("steps"))
print()

# ---- worker：build_workflow ----
wf_params = dict(retry_params)
wf_params["job_id"] = "retryjob"
wf = srv.build_workflow("t2v", wf_params)

lora_nodes = []
for nid, node in wf.items():
    if not isinstance(node, dict):
        continue
    ct = str(node.get("class_type") or "")
    if "lora" in ct.lower():
        inputs = node.get("inputs") or {}
        lora_nodes.append((nid, ct, inputs.get("lora_name"),
                           inputs.get("strength_model", inputs.get("strength"))))
print("工作流里的 LoRA 节点：")
for item in lora_nodes:
    print("  ", item)
if not lora_nodes:
    print("   （无）")
print()

sched = []
for nid, node in wf.items():
    if not isinstance(node, dict):
        continue
    ct = str(node.get("class_type") or "").lower()
    if ct in ("basicscheduler", "ksampler", "ksampleradvanced"):
        sched.append((nid, ct, (node.get("inputs") or {})))
print("采样步数节点：")
for nid, ct, inputs in sched:
    print(f"   {nid} {ct} steps={inputs.get('steps')} "
          f"scheduler={inputs.get('scheduler')} sampler={inputs.get('sampler_name')}")
print()

if not lora_nodes:
    print("==> 缺陷复现：retry 出来的工作流**一个加速 LoRA 节点都没有**")
    print("    但步数仍是", retry_params.get("steps"), "（蒸馏档）")
    print("    => 8 步采样的非蒸馏权重 = 「一条不存在的路径」，且**全程不报错**。")

shutil.rmtree(tmp, ignore_errors=True)
