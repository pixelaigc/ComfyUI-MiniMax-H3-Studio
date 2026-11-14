#!/usr/bin/env python3
"""核查「Agent 建 t2v 任务」与「面板建 t2v 任务」的最终 ComfyUI 图差异。

链路事实（本次核查读码确认）：
- 面板：POST /api/jobs -> api_create -> normalize_t2v_params(params)
         -> params["model_key"] 写好 -> build_workflow -> build_t2v_panel
- Agent：agent.py:1360 -> _create_job_record -> server.create_job_record
         **不经过 api_create / normalize_t2v_params**
         agent.py:1193 只写 params["model"]（不是 model_key）
         -> worker: build_workflow(mode, params)
         -> workflows.build_t2v: 判据 params.get("model_key") 或 (width 且 height)
            都不满足 -> 落旧的 _h3_video_workflow

=> Agent 的 t2v 任务：用户选的底模被忽略、走旧工作流、连宽高都没有。

本探针只读：直接调真实 builder + 真实参数准备函数，不 POST、不写盘。
"""
import json
import sys

sys.path.insert(0, "/root/projects/h3studio-feasibility")

import server  # noqa: E402
from h3studio import workflows  # noqa: E402

BASE = {
    "prompt": "AGENT-PROBE 柴犬",
    "seed": 12345,
    "duration": 5,
    "steps": 4,
    "sampler": "euler",
    "scheduler": "beta",
    "job_id": "agentprobe03",
}


def describe(g):
    info = {"nodes": len(g)}
    for nid, nd in g.items():
        ct = nd.get("class_type")
        if ct == "UNETLoader":
            info["unet"] = nd["inputs"].get("unet_name")
        if ct == "BasicScheduler":
            info["steps"] = nd["inputs"].get("steps")
            info["scheduler"] = nd["inputs"].get("scheduler")
        if ct == "MiniMaxH3ImageToVideo":
            info["w"] = nd["inputs"].get("width")
            info["h"] = nd["inputs"].get("height")
        if ct == "KSamplerSelect":
            info["sampler"] = nd["inputs"].get("sampler_name")
        if ct == "MiniMaxH3SigmaShift":
            info["shift"] = nd["inputs"].get("shift_video")
    return info


def which_path(params):
    """workflows.build_t2v 的真实分支判据（workflows.py:443）。"""
    if params.get("model_key") or (params.get("width") and params.get("height")):
        return "新路径 build_t2v_panel（video_workflow）"
    return "旧路径 _h3_video_workflow"



out = {}

# ---- A) Agent 形态：只写 params["model"]，无 model_key / 无宽高 ----
agent_params = {**BASE, "model": "fl2va_bf16"}
try:
    g = workflows.build_t2v(dict(agent_params))
    out["A_Agent形态"] = describe(g)
    out["A_Agent形态"]["path"] = which_path(agent_params)
    out["A_Agent形态"]["params"] = {k: v for k, v in agent_params.items() if k != "prompt"}
except Exception as exc:
    out["A_Agent形态"] = {"error": f"{type(exc).__name__}: {exc}"}

# ---- B) 面板形态：完整参数 + 服务端归一 ----
panel_raw = {**BASE, "model_key": "fl2va_bf16", "width": 1344, "height": 768}
try:
    normalized, err = server.normalize_t2v_params(dict(panel_raw))
    out["B_面板形态"] = {"normalize_error": err}
    if not err:
        bp = dict(normalized)
        bp["job_id"] = BASE["job_id"]
        g = workflows.build_t2v(bp)
        out["B_面板形态"].update(describe(g))
        out["B_面板形态"]["path"] = which_path(bp)
        out["B_面板形态"]["prepared_model_key"] = normalized.get("model_key")
except Exception as exc:
    out["B_面板形态"] = {"error": f"{type(exc).__name__}: {exc}"}

print("=== A) Agent 形态（只有 params['model']）===")
print(json.dumps(out["A_Agent形态"], ensure_ascii=False, indent=1))
print("\n=== B) 面板形态（model_key + 宽高，走服务端归一）===")
print(json.dumps(out["B_面板形态"], ensure_ascii=False, indent=1))

a, b = out["A_Agent形态"], out["B_面板形态"]
print("\n=== 结论 ===")
print(f"底模:   Agent={a.get('unet')}  |  面板={b.get('unet')}")
print(f"宽高:   Agent={a.get('w')}x{a.get('h')}  |  面板={b.get('w')}x{b.get('h')}")
print(f"采样器: Agent={a.get('sampler')}  |  面板={b.get('sampler')}")
if a.get("unet") != b.get("unet"):
    print("=> **Agent 的模型选择被静默忽略**（回落默认底模）")
if not a.get("w"):
    print("=> **Agent 的 t2v 任务没有宽高**（走旧工作流，尺寸由旧路径决定）")
print(f"工作流: Agent={a.get('path')}  |  面板={b.get('path')}")
if a.get("path") != b.get("path"):
    print("=> **两条链路走了不同的工作流**")

with open("/root/projects/h3studio-feasibility/evidence/feasibility/agent_t2v_probe.json",
          "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=1)
print("\n[saved] agent_t2v_probe.json")
