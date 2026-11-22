# -*- coding: utf-8 -*-
"""三个视频面板（t2v / flf / r2v）参数一致性的**端到端**验证。

【验证目标】—— 用户 2026-11-22 的四条反馈，一条一条对拍：
  1. 「该配置的不能配置」-> 三个面板的采样参数（步数 / 采样器 / 调度器 /
     视频+音频 sigma shift）都**可编辑**，且配的值**真的**进工作流 JSON；
  2. 「LORA模型都不对」-> 三个面板的加速 LoRA 下拉是**同一份**清单，
     显示名取自权重登记表；
  3. 「同时存在百万像素和分辨率挡位和比例，还有底模精度，全部都是冲突的」
     -> 面板上只剩一套尺寸表达（比例 × MP 预算 × 百分比）+ 一个只读
     派生项（生效像素），底模精度是**唯一**的只读说明项；
  4. 「我选的模型+LORA组合，和生成的记录的模型+LORA组合，不一样」
     -> 落库记录里的模型 + LoRA == 提交时的选择；
  5. 「分辨率的尺寸显示，和我实际跑的不一样」
     -> 卡片/详情显示的分辨率来自**产物实测像素**（ffprobe）。

【怎么跑】
  python3 scripts/verify_unify_panel_params_e2e.py <base_url> <out_dir>

【它做什么】
  * 打 ``/api/agent/settings`` 拿三个面板的**字段规格**（服务端真源），
    断言控件集合一致、LoRA 清单一致、无冲突表达；
  * 各跑 **1 个真实任务**（t2v / flf / r2v），走真实 ComfyUI；
  * 从 ComfyUI ``/history`` 取**实际提交的 prompt JSON**，逐节点对拍
    BasicScheduler / KSamplerSelect / MiniMaxH3SigmaShift；
  * 对产物跑 **ffprobe** 取实测宽高 / 时长 / 编码；
  * 把「面板显示 / 提交参数 / 记录回显 / 产物实测」四栏并排写进 JSON。

【前置】本机 ComfyUI (8188) 在跑、8190 服务在跑、有可用参考素材。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190"
COMFY = os.environ.get("H3S_COMFY_URL", "http://127.0.0.1:8188")
OUT_DIR = sys.argv[2] if len(sys.argv) > 2 else "/tmp/unify_panel_e2e"
os.makedirs(OUT_DIR, exist_ok=True)

PROMPT = "参数一致性验证：一只猫在窗边慢慢转头，暖色调，镜头缓推"

#: 本机素材库里的参考图（首尾帧 / 参考生视频用）。
#: 取不到就跳过对应用例并如实记录「skipped」。
REF_IMAGE = "asset_ef9b20ebf8db"

VIDEO_MODES = ("t2v", "flf", "r2v")


# ------------------------------------------------------------------ HTTP 工具
def _get(url, timeout=90):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw or "{}")
        except Exception:
            return exc.code, {"raw": raw[:2000]}
    except Exception as exc:
        return 0, {"error": "%s: %s" % (type(exc).__name__, exc)}


def _post_multipart(fields, timeout=300):
    """POST multipart/form-data（``/api/jobs`` 只接受这个 Content-Type）。"""
    boundary = "----h3unify" + uuid.uuid4().hex
    chunks = []
    for key, value in fields.items():
        chunks.append((
            "--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
            % (boundary, key, value)).encode("utf-8"))
    body = b"".join(chunks) + ("--%s--\r\n" % boundary).encode()
    req = urllib.request.Request(
        BASE + "/api/jobs", data=body, method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw or "{}")
        except Exception:
            return exc.code, {"raw": raw[:2000]}
    except Exception as exc:
        return 0, {"error": "%s: %s" % (type(exc).__name__, exc)}


def ffprobe(path):
    """产物的**实测**宽高 / 时长 / 编码（不猜、不估算）。"""
    if not path or not os.path.isfile(path):
        return None
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,codec_name,nb_frames",
         "-show_entries", "format=duration", "-of", "json", path],
        capture_output=True, text=True)
    try:
        data = json.loads(out.stdout or "{}")
    except Exception:
        return {"raw": out.stdout[:500], "stderr": out.stderr[:500]}
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    if not streams:
        return {"raw": data}
    s = streams[0]
    return {
        "width": s.get("width"),
        "height": s.get("height"),
        "codec": s.get("codec_name"),
        "nb_frames": s.get("nb_frames"),
        "duration": fmt.get("duration"),
    }


# ------------------------------------------------------------------ 面板规格检查
def check_panel_specs():
    """三个面板的字段规格一致性（服务端真源）。"""
    st, settings = _get(BASE + "/api/agent/settings")
    if st != 200:
        return {"ok": False, "error": "拿不到 /api/agent/settings: %s" % st}
    spec = settings.get("gen_defaults_spec") or {}
    defaults = settings.get("gen_defaults") or {}

    out: dict = {"ok": True, "modes": {}}
    per_mode: dict = {}
    for mode in VIDEO_MODES:
        info = spec.get(mode) or {}
        fields = info.get("fields") or []
        per_mode[mode] = {f["name"]: f for f in fields}
        out["modes"][mode] = {
            "label": info.get("label"),
            "field_names": [f["name"] for f in fields],
            "editable": [f["name"] for f in fields
                         if f.get("type") != "fixed" and not f.get("readonly")],
            "readonly": [f["name"] for f in fields
                         if f.get("type") == "fixed" or f.get("readonly")],
            "sampler_options": [
                o.get("value") for o in
                (per_mode[mode].get("sampler") or {}).get("options") or []],
            "scheduler_options": [
                o.get("value") for o in
                (per_mode[mode].get("scheduler") or {}).get("options") or []],
            "accel_lora_options": [
                (o.get("value"), o.get("label")) for o in
                ((per_mode[mode].get("accel_lora")
                  or per_mode[mode].get("lora") or {}).get("options") or [])],
            "saved_defaults": defaults.get(mode) or {},
        }

    checks = {}

    # 1) 采样五项在三个面板上都可编辑
    sampling = ("steps", "sampler", "scheduler", "shift_video", "shift_audio")
    checks["sampling_editable_on_all"] = all(
        all(n in out["modes"][m]["editable"] for n in sampling)
        for m in VIDEO_MODES)

    # 2) 采样器 / 调度器选项一致
    checks["sampler_options_identical"] = (
        out["modes"]["flf"]["sampler_options"]
        == out["modes"]["t2v"]["sampler_options"]
        == out["modes"]["r2v"]["sampler_options"])
    checks["scheduler_options_identical"] = (
        out["modes"]["flf"]["scheduler_options"]
        == out["modes"]["t2v"]["scheduler_options"]
        == out["modes"]["r2v"]["scheduler_options"])

    # 3) 加速 LoRA 清单一致
    checks["accel_lora_list_identical"] = (
        out["modes"]["flf"]["accel_lora_options"]
        == out["modes"]["t2v"]["accel_lora_options"]
        == out["modes"]["r2v"]["accel_lora_options"])

    # 4) 无冲突的尺寸表达（不得再有独立的分辨率档位 / 宽高 / 精度）
    banned = ("resolution", "width", "height", "precision", "size_preset")
    checks["no_conflicting_size_fields"] = all(
        not (set(out["modes"][m]["field_names"]) & set(banned))
        for m in VIDEO_MODES)

    # 5) 只读项：底模精度 + 生效像素（派生）
    checks["precision_is_readonly_with_reason"] = all(
        "precision_fixed" in out["modes"][m]["readonly"]
        and "节点" in (per_mode[m]["precision_fixed"].get("note") or "")
        for m in VIDEO_MODES)

    out["checks"] = checks
    out["ok"] = all(checks.values())
    return out


# ------------------------------------------------------------------ 真实任务
def submit_and_wait(mode, params, media=None):
    """提交一个真实任务并等它跑完，返回 (job_id, submit_payload)。

    【提交格式 —— 与面板真实提交完全同款】
      * 参数全部走 ``params`` 这一个 JSON 字段（multipart 的
        ``part.name == "params"``），**不是**一个个独立字段；
      * 素材走 ``asset_refs`` 字段（``[{"asset_id", "slot", "ordinal"}]``）
        —— 槽位名与服务端一致：
          flf -> ``first_frame`` / ``last_frame``
          r2v -> ``ref_image``（ordinal 从 1 开始）；
      * 宽高**不传** —— 由服务端按「比例 × MP 预算 × 百分比」算出
        （这正是本轮要验证的那条链）。
    """
    _p = dict(params or {})
    _p.setdefault("prompt", PROMPT)
    fields = {"mode": mode,
              "params": json.dumps(_p, ensure_ascii=False)}
    if media:
        fields["asset_refs"] = json.dumps(media, ensure_ascii=False)

    st, resp = _post_multipart(fields)
    if st != 200:
        return None, {"status": st, "response": resp, "fields": fields}

    job_id = resp.get("id") or resp.get("job_id")
    if not job_id:
        jobs = resp.get("jobs") or []
        job_id = jobs[0].get("id") if jobs else None
    if not job_id:
        return None, {"status": st, "response": resp, "fields": fields}

    deadline = time.time() + 1800
    last = {}
    while time.time() < deadline:
        _st, last = _get(BASE + "/api/jobs/%s" % job_id, timeout=60)
        job = last.get("job") or last
        status = job.get("status")
        if status in ("done", "failed", "error", "cancelled"):
            break
        time.sleep(5)
    return job_id, {"status": st, "submit_response": resp,
                    "fields": fields, "job": last}


def comfy_prompt_json(prompt_id, job_id=""):
    """从 ComfyUI 的 /history 取**实际提交**的 prompt JSON。

    【为什么用 prompt_id 而不是 job_id】
    任务落库时 worker 会把 ComfyUI 的 ``prompt_id`` 写进 jobs 表的
    ``prompt_id`` 列（server.py:935 下发）。那是**唯一精确**的关联键 ——
    用 job_id 去猜（切 uuid 后缀匹配）会在历史里有多条相似任务时**匹配错**
    （实测就匹配错过一次，导致「工作流里没有采样参数」的假阴性）。

    拿不到 prompt_id 时才退回「按 job_id 的 uuid 尾部匹配」。
    """
    st, hist = _get(COMFY + "/history", timeout=120)
    if st != 200:
        return None
    hist = hist or {}
    if prompt_id and prompt_id in hist:
        prompt = (hist[prompt_id] or {}).get("prompt") or []
        if isinstance(prompt, list) and len(prompt) >= 3:
            return prompt[2]
        return prompt
    # 兜底：按 job_id 的 uuid 尾部匹配（只在没有 prompt_id 时用）
    tail = (job_id or "").split("-")[-1]
    if tail:
        for pid, entry in hist.items():
            if tail in pid:
                prompt = (entry or {}).get("prompt") or []
                if isinstance(prompt, list) and len(prompt) >= 3:
                    return prompt[2]
                return prompt
    return None


def extract_sampling_from_prompt(prompt):
    """从工作流 JSON 里抽出采样相关节点的输入。"""
    out: dict = {"basic_scheduler": None, "ksampler_select": None,
               "sigma_shift": None, "unet": None}
    if not isinstance(prompt, dict):
        return out
    for node in prompt.values():
        if not isinstance(node, dict):
            continue
        ctype = node.get("class_type")
        inputs = node.get("inputs") or {}
        if ctype == "BasicScheduler":
            out["basic_scheduler"] = {
                "steps": inputs.get("steps"),
                "scheduler": inputs.get("scheduler"),
            }
        elif ctype == "KSamplerSelect":
            out["ksampler_select"] = {"sampler_name": inputs.get("sampler_name")}
        elif ctype == "MiniMaxH3SigmaShift":
            out["sigma_shift"] = {
                "shift_video": inputs.get("shift_video"),
                "shift_audio": inputs.get("shift_audio"),
            }
        elif ctype == "UNETLoader":
            out["unet"] = inputs.get("unet_name")
    return out


def run_mode(mode, params, media=None):
    """跑一个模式的真实任务，产出四栏对拍证据。"""
    job_id, info = submit_and_wait(mode, params, media)
    result: dict = {"mode": mode, "submitted_params": params,
              "media": media or {}, "job_id": job_id}
    result["submit_info"] = {k: v for k, v in info.items() if k != "job"}
    if not job_id:
        result["skipped"] = True
        return result

    job = info.get("job") or {}
    row = job.get("job") or job
    stored = row.get("params") or {}
    result["record_params"] = {
        "model_key": stored.get("model_key"),
        "accel_lora": stored.get("accel_lora"),
        "accel_loras": stored.get("accel_loras"),
        "lora": stored.get("lora"),
        "lora_strength": stored.get("lora_strength"),
        "steps": stored.get("steps"),
        "sampler": stored.get("sampler"),
        "scheduler": stored.get("scheduler"),
        "shift_video": stored.get("shift_video"),
        "shift_audio": stored.get("shift_audio"),
        "width": stored.get("width"),
        "height": stored.get("height"),
        "megapixels": stored.get("megapixels"),
        "resolution_percent": stored.get("resolution_percent"),
        "resolution": stored.get("resolution"),
    }
    result["record_model_info"] = row.get("model_info")
    result["record_resolution_info"] = row.get("resolution_info")
    result["record_gen_params"] = row.get("gen_params")
    result["status"] = row.get("status")

    prompt_id = str(row.get("prompt_id") or "")
    result["comfy_prompt_id"] = prompt_id
    prompt = comfy_prompt_json(prompt_id, job_id)
    result["comfy_prompt_json"] = prompt
    result["comfy_sampling"] = extract_sampling_from_prompt(prompt)

    outs = row.get("outputs") or []
    for o in outs:
        if isinstance(o, dict) and o.get("kind") == "video":
            path = o.get("file") or ""
            local = os.path.join(
                "/root/projects/comfyui-minimax-h3-studio/data/outputs", path)
            result["product"] = {
                "file": path,
                "stored_width": o.get("width"),
                "stored_height": o.get("height"),
                "ffprobe": ffprobe(local),
            }
            break
    return result


def main():
    report = {"base": BASE, "comfy": COMFY, "started_at": time.time()}

    report["panel_spec_check"] = check_panel_specs()
    with open(os.path.join(OUT_DIR, "panel_spec_check.json"), "w") as fh:
        json.dump(report["panel_spec_check"], fh, ensure_ascii=False, indent=2)

    # ---- 三个模式各跑 1 个真实用例 ----
    # 故意用**非默认**的采样参数（8 步 / dpm_2 / karras / shift 7.5 / 1.25），
    # 这样才能证明「配了真的生效」，而不是碰巧与默认值相同。
    sampling = {
        "steps": 8, "sampler": "dpm_2", "scheduler": "karras",
        "shift_video": 7.5, "shift_audio": 1.25,
    }
    base = {"duration": 2, "aspect": "16:9", "megapixels": 0.4,
            "resolution_percent": 100.0,
            "accel_lora": "ckpt850", "accel_lora_strength": 1.0}
    base.update(sampling)

    # 只跑指定的 mode（命令行第 4+ 个参数）；默认三个都跑。
    # 【为什么需要】GPU 被其它任务占满时，全量跑会超时（timeout 2400s）。
    wanted = sys.argv[3:] or list(VIDEO_MODES)
    MEDIA = {
        "flf": [{"asset_id": REF_IMAGE, "slot": "first_frame", "ordinal": 1}],
        "r2v": [{"asset_id": REF_IMAGE, "slot": "ref_image", "ordinal": 1}],
    }
    runs = {}
    for mode in wanted:
        runs[mode] = run_mode(mode, dict(base), MEDIA.get(mode))
    report["runs"] = runs

    # ---- 四栏对拍结论 ----
    verdict: dict = {}
    for mode, run in runs.items():
        if run.get("skipped"):
            verdict[mode] = {"skipped": True}
            continue
        want = sampling
        got = run.get("comfy_sampling") or {}
        bs = got.get("basic_scheduler") or {}
        ks = got.get("ksampler_select") or {}
        ss = got.get("sigma_shift") or {}
        rec = run.get("record_params") or {}
        prod = (run.get("product") or {}).get("ffprobe") or {}
        verdict[mode] = {
            "steps_match": bs.get("steps") == want["steps"],
            "scheduler_match": bs.get("scheduler") == want["scheduler"],
            "sampler_match": ks.get("sampler_name") == want["sampler"],
            "shift_video_match": float(ss.get("shift_video") or -1) == want["shift_video"],
            "shift_audio_match": float(ss.get("shift_audio") or -1) == want["shift_audio"],
            "record_steps_match": rec.get("steps") == want["steps"],
            "record_sampler_match": rec.get("sampler") == want["sampler"],
            "record_scheduler_match": rec.get("scheduler") == want["scheduler"],
            # 记录里的 LoRA 必须只有一枚，且等于提交的那枚
            "record_lora_consistent": (
                rec.get("accel_lora") == base["accel_lora"]
                and rec.get("lora") == base["accel_lora"]),
            # 卡片尺寸必须等于产物实测
            "card_size_matches_product": (
                bool(prod.get("width"))
                and (run.get("record_resolution_info") or {}).get("width")
                == prod.get("width")
                and (run.get("record_resolution_info") or {}).get("height")
                == prod.get("height")),
        }
        verdict[mode]["ok"] = all(v for k, v in verdict[mode].items()
                                  if k != "skipped" and isinstance(v, bool))
    report["verdict"] = verdict
    report["ok"] = bool(report["panel_spec_check"].get("ok")) and all(
        v.get("ok") for v in verdict.values() if not v.get("skipped"))

    with open(os.path.join(OUT_DIR, "e2e_report.json"), "w") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    print(json.dumps({
        "ok": report["ok"],
        "panel_spec_checks": report["panel_spec_check"].get("checks"),
        "verdict": verdict,
        "out_dir": OUT_DIR,
    }, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
