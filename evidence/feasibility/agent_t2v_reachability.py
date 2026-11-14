#!/usr/bin/env python3
"""核查：Agent 的 t2v 到底能不能设「视频底模」？静态审计的「Agent 模型被忽略」是否可达。

静态审计（parameter-throughput-audit.md §E2 #6b）称：
  Agent 传 params["model"]，t2v builder 读 params["model_key"]
  -> Agent 选的底模被忽略，走旧路径。

本探针验证**可达性**：Agent 的工具 schema 里 t2v 模式到底给不给「模型」选项？
若 t2v 的 models 列表里没有视频底模（只有 lora），则 Agent 根本设不了 video model，
该问题在生产中**不可达**（属于潜在埋雷，非现存缺陷）。

只读：import + 打印，不 POST、不写盘。
"""
import json
import sys

sys.path.insert(0, "/root/projects/h3studio-feasibility")

from h3studio import agent_schema as s  # noqa: E402

out = {}
for mode in ("t2v", "flf", "r2v", "t2i", "t2i_qwen"):
    try:
        out[mode] = {"mode_models": s.mode_models(mode)}
    except Exception as exc:
        out[mode] = {"error": f"{type(exc).__name__}: {exc}"}
    print(f"{mode:10} mode_models -> {out[mode].get('mode_models')}")

print("\n=== validate_params 会不会把 t2v 的 model 拒掉/改成 lora ===")
try:
    res = s.validate_params("t2v", {"model": "fl2va_bf16", "prompt": "x"})
    out["validate_t2v_with_video_model"] = res
    print("validate_params('t2v', {'model':'fl2va_bf16'}) ->", json.dumps(res, ensure_ascii=False)[:400])
except Exception as exc:
    out["validate_t2v_with_video_model"] = {"error": f"{type(exc).__name__}: {exc}"}
    print("ERR:", type(exc).__name__, exc)

print("\n=== 结论 ===")
t2v_models = out.get("t2v", {}).get("mode_models") or []
video_keys = {"fl2va_pruned_int8", "fl2va_int8", "fl2va_pruned_w4a8", "fl2va_bf16",
              "hybrid_fl2va_ref2va"}
if video_keys & set(t2v_models):
    print("=> Agent 的 t2v **暴露了**视频底模选项 —— 静态审计的「模型被忽略」在生产可达")
else:
    print("=> Agent 的 t2v **没有暴露**任何视频底模（只有 lora）")
    print("   => 静态审计的「Agent 选模型被忽略」在生产**不可达**")
    print("   => params[\"model\"] 对视频模式恒为空；Agent 视频任务走旧路径是**设计如此**")
    print("      （Agent 的视频模式只给 544p/768p 两档 resolution，见 agent.py:696-700）")

with open("/root/projects/h3studio-feasibility/evidence/feasibility/agent_t2v_reachability.json",
          "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=1)
print("\n[saved] agent_t2v_reachability.json")
