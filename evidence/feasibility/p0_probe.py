#!/usr/bin/env python3
"""可实现性核查探针：验证 P0 项在**生产路径**上的真实行为。

只读探针：不改仓库、不改服务、不触碰 8190。
"""
import json
import os
import sys

sys.path.insert(0, "/root/projects/h3studio-feasibility")

R = {}


def probe(name, fn):
    try:
        R[name] = {"ok": True, "value": fn()}
    except Exception as exc:
        R[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# --- P0-3a: 深度音频分析器 backend 是否在生产接上 ---
def audio_backend():
    from h3studio import analysis
    return {
        "AudioAnalyzer._backend": repr(analysis.AudioAnalyzer._backend),
        "has_set_backend": hasattr(analysis.AudioAnalyzer, "set_backend"),
    }


probe("P0-3a_analysis_backend", audio_backend)


# --- P0-3b: 生产真实深度音频模块 audio_deep 环境检测 ---
def audio_deep_env():
    from h3studio import audio_deep
    env = audio_deep.check_environment()
    return {k: v for k, v in env.items() if not isinstance(v, (bytes, bytearray))}


probe("P0-3b_audio_deep_env", audio_deep_env)


# --- P0-3c: 深管线是否真能跑（找一个小音频素材） ---
def deep_analyze_real():
    from h3studio import audio_deep
    from pathlib import Path

    lib = Path("/mnt/usb2t/asset_library")
    if not lib.exists():
        return {"skipped": "素材库根目录不存在"}
    found = []
    for p in lib.rglob("*"):
        if p.suffix.lower() in (".mp3", ".wav", ".m4a", ".flac", ".ogg") and p.stat().st_size > 10000:
            found.append((str(p), p.stat().st_size))
        if len(found) >= 3:
            break
    if not found:
        return {"skipped": "素材库里没找到音频文件"}
    path, size = found[0]
    result = audio_deep.analyze(path, {"duration_sec": 30, "max_frames": 4})
    return {
        "file": path, "size": size,
        "result_keys": sorted(result.keys()) if isinstance(result, dict) else None,
        "bpm": result.get("bpm") if isinstance(result, dict) else None,
        "key": result.get("key") if isinstance(result, dict) else None,
        "asr_count": len(result.get("asr") or []) if isinstance(result, dict) else None,
        "instruments": (result.get("instruments") or [])[:5] if isinstance(result, dict) else None,
    }


if os.environ.get("SKIP_DEEP") != "1":
    probe("P0-3c_deep_analyze_real", deep_analyze_real)


# --- P0-1: 「智能排序」到底是不是模型打分 ---
def refs_sort_heuristic():
    from h3studio import ref_images as RI
    refs = RI.normalize_refs([
        {"id": "a1", "name": "snow.jpg", "tags": ["雪山"], "description": "雪山风景",
         "created_at": 1000},
        {"id": "a2", "name": "dog.jpg", "tags": ["柴犬"], "description": "一只柴犬",
         "created_at": 2000},
    ])
    prompt = "一只柴犬在雪山奔跑"
    res_model = RI.smart_sort(refs, prompt=prompt, use_model=True)
    res_basic = RI.smart_sort(refs, prompt=prompt, use_model=False)
    src = open("/root/projects/h3studio-feasibility/h3studio/ref_images.py", encoding="utf-8").read()
    return {
        "ranking_use_model_true": res_model.get("ranking"),
        "degraded_use_model_true": res_model.get("degraded"),
        "ranking_use_model_false": res_basic.get("ranking"),
        "identical_to_basic": res_model.get("refs") == res_basic.get("refs"),
        "score_refs_calls_model_api": any(
            k in src for k in ("ClientSession", "requests.", "urllib", "httpx", "llm_client", "provider")
        ),
    }


probe("P0-1_refs_sort_heuristic", refs_sort_heuristic)


# --- P0-2: /api/enhance 的 video/audio 档是不是官方原文 ---
def enhancer_templates():
    from h3studio import enhancer
    out = {}
    for spec_id in ("video", "audio", "image"):
        try:
            spec = enhancer.get_spec(spec_id, {})
            out[spec_id] = {
                "system_prompt_len": len(spec.get("system_prompt") or ""),
                "system_prompt_head": (spec.get("system_prompt") or "")[:160],
                "is_builtin": spec.get("builtin"),
            }
        except Exception as exc:
            out[spec_id] = {"error": str(exc)}
    # 官方原文文件是否存在
    assets = "/root/projects/h3studio-feasibility/h3studio/skills/prompt-enhancer/assets"
    out["_assets_dir"] = sorted(os.listdir(assets)) if os.path.isdir(assets) else "MISSING"
    return out


probe("P0-2_enhancer_templates", enhancer_templates)


# --- P0-7: H3-Image 的负面提示词 / LoRA 丢弃是否在 UI 有出口 ---
def h3image_ignored_field():
    out = {}
    src = open("/root/projects/h3studio-feasibility/server.py", encoding="utf-8").read()
    out["server_sets_negative_prompt_ignored"] = "negative_prompt_ignored" in src
    fe = []
    for f in sorted(os.listdir("/root/projects/h3studio-feasibility/web")):
        if not f.endswith(".js"):
            continue
        s = open(f"/root/projects/h3studio-feasibility/web/{f}", encoding="utf-8").read()
        if "negative_prompt_ignored" in s:
            fe.append(f)
    out["frontend_files_reading_it"] = fe
    return out


probe("P0-7_negative_prompt_ignored", h3image_ignored_field)

print(json.dumps(R, ensure_ascii=False, indent=1))
out_path = os.environ.get("PROBE_OUT")
if out_path:
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(R, fh, ensure_ascii=False, indent=1)
    print(f"\n[saved] {out_path}")
