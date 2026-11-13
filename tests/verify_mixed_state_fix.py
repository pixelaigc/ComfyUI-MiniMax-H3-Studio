# -*- coding: utf-8 -*-
"""【复现-修复对照】接续 + 复用参数的混合态（2026-11-12 修）。

这个脚本复刻独立复验发现缺陷的那条路径，并对照修复前后的差异：

  场景：点「接续」（来源 A，768×448）之后，又点另一张卡片的「复用参数」
        （B，1024×576）。提交时 params 来自 B、continuation 来自 A。

  修复前：任务失败，job.error = "'str' object has no attribute 'get'"
          （原始 Python 异常；真正的原因「拷贝桥禁止跨分辨率」被吞掉）
  修复后：服务端**提交前**就拦下，给出中文原因
          「接续失败：上一段是 768×448，本段是 1024×576 —— …」
          且**不**产生失败任务（不浪费一次生成）

另外还验证：
  * 尺寸一致的接续**仍然能跑通**（修复没有误伤正常路径）；
  * 错误提取不再抛 AttributeError。

用法：
    ACC_BASE=http://127.0.0.1:8202 \
    ACC_OUT=/path/to/evidence \
    python3 tests/verify_mixed_state_fix.py
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

BASE = os.environ.get("ACC_BASE", "http://127.0.0.1:8202")
OUT = Path(os.environ.get(
    "ACC_OUT",
    str(REPO / "evidence" / "continuation-mixed-state")))
DATA = Path(os.environ.get("ACC_DATA_DIR", "/tmp/h3t2v-fix-data"))

# 混合态的两张卡片：A = 768x448（接续源），B = 1024x576（复用参数源）
SIZE_A = (768, 448)
SIZE_B = (1024, 576)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode())


def post(fields):
    b = "----h3fix8811"
    body = b""
    for k, v in fields.items():
        body += f"--{b}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        body += str(v).encode("utf-8") + b"\r\n"
    body += f"--{b}--\r\n".encode()
    req = urllib.request.Request(BASE + "/api/jobs", data=body,
                                 headers={"Content-Type":
                                          f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())


def wait(jid, t=1800):
    dl = time.time() + t
    while time.time() < dl:
        j = get(f"/api/jobs/{jid}")
        if j.get("status") in ("done", "failed", "canceled"):
            return j
        time.sleep(3)
    raise TimeoutError(jid)


def create_t2v(prompt, width, height, seed, duration=5.0, steps=4):
    """建一条 t2v 任务并等它完成。"""
    params = {
        "model_key": "fl2va_int8",
        "fixed_accel_lora": "ckpt850",
        "accel_loras": [],
        "attention": "default",
        "twopass": {"enabled": False},
        "width": width, "height": height,
        "duration": duration,
        "steps": steps,
        "sampler": "euler", "scheduler": "simple",
        "shift_video": 12.0, "shift_audio": 3.0,
        "seed": seed,
        "prompt": prompt,
    }
    jid = post({"mode": "t2v", "params": json.dumps(params)})["id"]
    print(f"  建任务 {jid}（{width}×{height}）…")
    job = wait(jid)
    print(f"    status={job.get('status')} error={job.get('error')}")
    return jid, job


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    report = {"base": BASE, "steps": []}

    # ------------------------------------------------------------------
    # 1) 造两张卡片：A（接续源，768×448）与 B（复用参数源，1024×576）
    # ------------------------------------------------------------------
    print("=== 1) 造两张卡片 ===")
    a_id, a_job = create_t2v(
        "A: a lone swordsman standing in a bamboo grove at dusk",
        SIZE_A[0], SIZE_A[1], seed=4242)
    b_id, b_job = create_t2v(
        "B: a red lantern festival at night, crowds walking",
        SIZE_B[0], SIZE_B[1], seed=9999)

    if a_job.get("status") != "done" or b_job.get("status") != "done":
        report["error"] = "前置卡片没跑成功，无法做混合态复现"
        (OUT / "mixed_state_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("失败：" + report["error"])
        return 2

    ctx = get(f"/api/jobs/{a_id}/continuation")
    if not ctx.get("ok"):
        report["error"] = f"A 不可接续: {ctx.get('error')}"
        (OUT / "mixed_state_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("失败：" + report["error"])
        return 2
    report["steps"].append({
        "step": "created",
        "A": a_id, "A_size": list(SIZE_A),
        "B": b_id, "B_size": list(SIZE_B),
        "A_continuation_ok": True,
    })

    # ------------------------------------------------------------------
    # 2) 复刻混合态提交：params 来自 B，continuation 来自 A
    # ------------------------------------------------------------------
    print("\n=== 2) 提交混合态（B 的参数 + A 的接力棒）===")
    mixed = dict(ctx["params"])
    mixed["width"] = SIZE_B[0]
    mixed["height"] = SIZE_B[1]
    mixed["prompt"] = ("MIXED STATE PROBE: continue from A but render at B "
                        "resolution")
    mixed["seed"] = 9999
    mixed["continuation"] = ctx["continuation"]
    print(f"  提交参数: width={mixed['width']} height={mixed['height']} "
          f"continuation.source_job={mixed['continuation']['source_job']}")

    # 修复后：服务端应在**提交时**返回 400 + 中文原因（不建任务）
    rejected = None
    created = None
    try:
        created = post({"mode": "t2v", "params": json.dumps(mixed)})
        print(f"  服务端接受了提交 -> job_id={created.get('id')}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        rejected = {"http_status": exc.code, "body": body}
        print(f"  服务端在提交时拒绝: HTTP {exc.code}")
        print(f"  错误文案: {body[:400]}")

    mixed_result = {
        "rejected_at_submit": rejected is not None,
        "reject_detail": rejected,
        "created_job": created,
    }
    if created and created.get("id"):
        job = wait(created["id"])
        mixed_result["job_status"] = job.get("status")
        mixed_result["job_error"] = job.get("error")
        print(f"  任务结果: status={job.get('status')} "
              f"error={job.get('error')}")
    report["steps"].append({"step": "mixed_submit", **mixed_result})

    # ------------------------------------------------------------------
    # 3) 断言 1：不得再出现原始 Python 异常文本
    # ------------------------------------------------------------------
    error_text = ""
    if rejected:
        error_text = str(rejected.get("body") or "")
    elif mixed_result.get("job_error"):
        error_text = str(mixed_result["job_error"])
    # 【为什么要解 JSON】服务端返回的是 JSON，汉字被转义成 \\uXXXX ——
    # 直接对原始 body 做「是不是中文」的判断会得到 False（假阴性）。
    # 先解出真正的 error 字段再判断。
    decoded = error_text
    try:
        payload = json.loads(error_text)
        if isinstance(payload, dict) and payload.get("error"):
            decoded = str(payload["error"])
    except (ValueError, TypeError):
        pass
    mixed_result["user_visible_text"] = decoded
    error_text = decoded
    forbidden = ["'str' object has no attribute 'get'",
                 "object has no attribute",
                 "AttributeError",
                 "Traceback (most recent call last)"]
    leaked = [token for token in forbidden if token in error_text]
    report["no_raw_python_exception"] = not leaked
    report["leaked_tokens"] = leaked
    report["user_visible_text"] = decoded
    print(f"\n=== 3) 原始 Python 异常泄漏: {'无 ✅' if not leaked else '有 ❌ ' + str(leaked)}")
    print(f"  用户看到的文案: {error_text[:300]}")

    # ------------------------------------------------------------------
    # 4) 断言 2：文案必须是中文且带真正原因
    # ------------------------------------------------------------------
    has_chinese = any("一" <= ch <= "鿿" for ch in error_text)
    has_real_cause = ("分辨率" in error_text
                      or "跨分辨率" in error_text
                      or "768" in error_text)
    report["error_is_chinese"] = has_chinese
    report["error_has_real_cause"] = has_real_cause
    print(f"  中文: {has_chinese} / 带真正原因: {has_real_cause}")

    # ------------------------------------------------------------------
    # 5) 断言 3：修复没有误伤正常路径 —— 尺寸一致的接续仍能跑通
    # ------------------------------------------------------------------
    print("\n=== 4) 正常接续（尺寸一致）仍能跑通 ===")
    good = dict(ctx["params"])
    good["prompt"] = "the swordsman raises his blade toward the moonlight"
    good["seed"] = 7777
    good["continuation"] = ctx["continuation"]
    gid = post({"mode": "t2v", "params": json.dumps(good)})["id"]
    print(f"  提交接续任务 {gid}…")
    gjob = wait(gid)
    print(f"    status={gjob.get('status')} error={gjob.get('error')}")
    ok_full = False
    full_frames = None
    for o in (gjob.get("outputs") or []):
        if o.get("kind") == "video" and o.get("is_full"):
            ok_full = True
            path = DATA / "outputs" / o["file"]
            if path.is_file():
                raw = subprocess.check_output(
                    ["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-count_frames", "-show_entries",
                     "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
                    text=True).strip()
                full_frames = int(raw)
    report["steps"].append({
        "step": "normal_continuation",
        "job": gid, "status": gjob.get("status"),
        "error": gjob.get("error"),
        "has_full_video": ok_full, "full_frames": full_frames,
    })
    print(f"  完整视频: {ok_full} 帧数: {full_frames}")
    report["normal_continuation_ok"] = (
        gjob.get("status") == "done" and ok_full)

    # ------------------------------------------------------------------
    # 6) 断言 4：错误提取模块本身不再抛 AttributeError
    # ------------------------------------------------------------------
    print("\n=== 5) 错误提取模块自检 ===")
    from h3studio import comfy_errors
    real_status = {
        "status_str": "error",
        "messages": [["execution_error", {
            "node_type": "H3RelayCopyBridge",
            "exception_type": "ValueError",
            "exception_message": "拷贝桥禁止跨分辨率：上段 768x448 ≠ 本段 1024x576\n",
        }]],
    }
    try:
        described = comfy_errors.extract_error(real_status)
        report["extractor_ok"] = True
        report["extractor_reason"] = described["reason"]
        print(f"  提取成功: {described['reason'][:200]}")
    except Exception as exc:
        report["extractor_ok"] = False
        report["extractor_error"] = f"{type(exc).__name__}: {exc}"
        print(f"  提取抛异常: {exc}")

    report["verdict_pass"] = bool(
        report.get("no_raw_python_exception")
        and report.get("error_is_chinese")
        and report.get("error_has_real_cause")
        and report.get("normal_continuation_ok")
        and report.get("extractor_ok"))

    out_file = OUT / "mixed_state_report.json"
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n报告: {out_file}")
    print(f"结论: {'通过 ✅' if report['verdict_pass'] else '未通过 ❌'}")
    return 0 if report["verdict_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
