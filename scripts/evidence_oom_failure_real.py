#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""缺陷 2 的**真机**证据：复现**线上真实发生过的 OOM 配置**，验证判失败且无黑图。

【为什么不用 8192×8192 出图】

第一版证据脚本用「8192×8192 出图」制造 OOM，结果 ComfyUI 进程被**直接
打死**（连接被拒 / 服务重启），history 条目被冲掉，服务端只拿到
「连接失败」这种兜底异常 —— 判成 failed 但**原因不是 OOM**，
而且顺手把本机 ComfyUI 打挂（影响其他人正在跑的任务）。

【本脚本的做法 —— 复现真正的线上 OOM】

线上库里三条**历史误报样本**的参数是现成的真相：

    628d92cfc062  r2v  1920x1088  15s  hybrid_fl2va_ref2va
                   refs = [ref_image asset_d706cdfa6101,
                           ref_audio asset_13cb1ba4dcd9]
                   audio_track_source = reference
    86db94915467  r2v  1920x1088  20s  同上
    c5c3f63343b8  r2v  1920x1088   5s  同上（cube-order 报错）

这三个都在 ComfyUI 侧报的是 `Allocation on device 0 would exceed
allowed memory. (out of memory)` —— **服务进程活着**，只是这一次
分配失败。本脚本用**同一套素材 + 同一分辨率 + 同样时长**提交，
拿到的是**真 OOM**（status_str='error'），ComfyUI 不死，
服务端能拿到完整的 OOM 原因。

【判定链路的硬证据】

脚本会**离线复算**旧逻辑：拿同一条 history，
旧逻辑（只看 outputs 非空）判「成功」，新逻辑判「失败」——
证明这次确实是「原缺陷的触发条件」，而不是随便一条失败。

用法：python3 scripts/evidence_oom_failure_real.py [--shots]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = "http://127.0.0.1:8190"
COMFY = "http://127.0.0.1:8188"
DB = "/root/projects/comfyui-minimax-h3-studio/data/jobs.db"
OUT = "/root/projects/h3s-merge-20261119/evidence/gen-status-20261119"
REPO = "/root/projects/h3s-merge-20261119"

#: 线上历史误报样本用过的素材（仍在素材库里）。
REF_IMAGE = "asset_d706cdfa6101"
REF_AUDIO = "asset_13cb1ba4dcd9"

RESULTS: list[dict] = []


def record(ok, name, detail):
    RESULTS.append({"ok": bool(ok), "name": name, "detail": str(detail)})
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    return bool(ok)


def get_json(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def submit_r2v_job(duration=15.0):
    """提交一条与线上历史 OOM **同配置**的参考生视频任务。"""
    boundary = uuid.uuid4().hex
    parts = []

    def field(name, value):
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8"))

    field("mode", "r2v")
    field("params", json.dumps({
        "prompt": "A calm portrait in soft studio light, slow camera push-in.",
        "model_key": "hybrid_fl2va_ref2va",
        "width": 1920,
        "height": 1088,
        "duration": duration,
        "steps": 8,
        "cfg": 1.0,
        "shift_video": 12.0,
        "shift_audio": 2.0,
        "audio_track_source": "reference",
    }, ensure_ascii=False))
    field("asset_refs", json.dumps([
        {"asset_id": REF_IMAGE, "slot": "ref_image", "ordinal": 0},
        {"asset_id": REF_AUDIO, "slot": "ref_audio", "ordinal": 0},
    ], ensure_ascii=False))
    body = b"".join(parts) + f"--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/api/jobs", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def comfy_health():
    try:
        get_json(f"{COMFY}/queue", timeout=10)
        return True
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", action="store_true")
    ap.add_argument("--duration", type=float, default=15.0)
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)

    print("═══ 前置检查 ═══")
    if not comfy_health():
        print("  !! ComfyUI 不健康，终止")
        return 2
    record(True, "ComfyUI 健康（8188 可用）", "GET /queue 200")

    print(f"═══ 提交复现线上 OOM 配置的 r2v 任务 "
          f"(1920x1088 / {args.duration}s / ref_image+ref_audio) ═══")
    resp = submit_r2v_job(args.duration)
    job_id = resp.get("id")
    print(f"  任务已提交: {job_id}")
    if not job_id:
        print("  !! 提交失败:", resp)
        return 1

    deadline = time.time() + 2400
    job = None
    last = ""
    while time.time() < deadline:
        job = get_json(f"{BASE}/api/jobs/{job_id}")
        cur = f"{job.get('status')}|{str(job.get('error') or '')[:60]}"
        if cur != last:
            print(f"  [{time.strftime('%H:%M:%S')}] {cur}", flush=True)
            last = cur
        if job.get("status") in ("done", "failed", "canceled"):
            break
        time.sleep(8)
    else:
        record(False, "任务在 40 分钟内到终态", f"仍是 {job.get('status')}")
        return 1

    print(f"  任务终态: status={job.get('status')}")
    print(f"  error    = {job.get('error')}")
    print(f"  outputs  = {json.dumps(job.get('outputs'), ensure_ascii=False)}")

    record(job.get("status") == "failed",
           "OOM 任务判定为 failed（不是 done / 生成成功）",
           f"status={job.get('status')}")

    err = str(job.get("error") or "")
    record("显存不足" in err or "out of memory" in err.lower(),
           "失败原因明确指出显存不足（OOM）",
           f"error={err[:160]}")

    outs = job.get("outputs") or []
    record(len(outs) == 0,
           "失败任务没有任何产物条目（前端不会渲染出黑图）",
           f"outputs={json.dumps(outs, ensure_ascii=False)[:200]}")

    prompt_id = job.get("prompt_id") or ""
    comfy_hist = {}
    if prompt_id:
        try:
            h = get_json(f"{COMFY}/history/{prompt_id}")
            comfy_hist = h.get(prompt_id) or {}
        except Exception as exc:
            print(f"  !! 取 ComfyUI history 失败: {exc}")
    status_info = comfy_hist.get("status") or {}
    comfy_outputs = comfy_hist.get("outputs") or {}
    print(f"  ComfyUI 侧: status_str={status_info.get('status_str')!r} "
          f"outputs_keys={list(comfy_outputs.keys())}")
    record(status_info.get("status_str") == "error",
           "ComfyUI 侧确实是执行失败（真 OOM，不是构造的）",
           f"status_str={status_info.get('status_str')!r}")

    # 【原缺陷触发条件】离线复算旧逻辑
    sys.path.insert(0, REPO)
    from h3studio import gen_outcome  # noqa: E402
    legacy_would_succeed = bool(comfy_outputs)
    new_outcome = gen_outcome.history_outcome(comfy_hist)
    print(f"  【原缺陷触发条件】ComfyUI outputs 非空 = {legacy_would_succeed} "
          f"（keys={list(comfy_outputs.keys())}）")
    print(f"     旧逻辑（只看 outputs 非空）-> 会判「成功」")
    print(f"     新逻辑 -> outcome={new_outcome['outcome']!r} "
          f"failed={new_outcome['failed']}")
    record(legacy_would_succeed and new_outcome["failed"],
           "同一条 history：旧逻辑会误判成功，新逻辑判失败（判定顺序修复生效）",
           f"legacy_succeed={legacy_would_succeed} new_outcome={new_outcome['outcome']}")

    out = {
        "job_id": job_id,
        "prompt_id": prompt_id,
        "reproduced_config": {
            "mode": "r2v", "width": 1920, "height": 1088,
            "duration": args.duration, "steps": 8,
            "model_key": "hybrid_fl2va_ref2va",
            "asset_refs": [REF_IMAGE, REF_AUDIO],
            "note": "与线上历史误报样本 628d92cfc062 / 86db94915467 同配置",
        },
        "studio_job": {
            "status": job.get("status"),
            "error": job.get("error"),
            "outputs": job.get("outputs"),
        },
        "comfy_history": {
            "status_str": status_info.get("status_str"),
            "completed": status_info.get("completed"),
            "outputs_keys": list(comfy_outputs.keys()),
            "messages": status_info.get("messages"),
        },
        "legacy_would_succeed": legacy_would_succeed,
        "new_outcome": new_outcome["outcome"],
        "verdict_failed": job.get("status") == "failed",
        "verdict_no_outputs": len(outs) == 0,
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(f"{OUT}/evidence_oom_failure_real.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print(f"  证据已存: {OUT}/evidence_oom_failure_real.json")

    if args.shots:
        subprocess_shot(job_id)

    passed = sum(1 for r in RESULTS if r["ok"])
    print(f"\n通过 {passed}/{len(RESULTS)}")
    with open(f"{OUT}/evidence_oom_real_summary.json", "w", encoding="utf-8") as fh:
        json.dump(RESULTS, fh, ensure_ascii=False, indent=2)
    return 0 if passed == len(RESULTS) else 1


def subprocess_shot(job_id):
    """用真实浏览器打开生成库，截图这条失败卡片。"""
    print("\n═══ 真实浏览器截图 ═══")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        print(f"  !! 没有 playwright: {exc}")
        record(False, "浏览器截图", f"缺 playwright: {exc}")
        return
    shot = f"{OUT}/oom-failed-card-real.png"
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.goto(f"{BASE}/", wait_until="networkidle")
        page.wait_for_timeout(3000)
        for sel in ("text=生成库", "text=作品", "a:has-text('生成')"):
            try:
                page.locator(sel).first.click(timeout=3000)
                break
            except Exception:
                continue
        page.wait_for_timeout(2000)
        card = page.locator(f'.job-card[data-jid="{job_id}"]')
        if card.count():
            card.first.scroll_into_view_if_needed()
            page.wait_for_timeout(800)
            card.first.screenshot(path=shot)
            print(f"  已截图失败卡片: {shot}")
            has_media = card.first.locator(
                ".job-preview img, .job-preview video").count()
            record(has_media == 0,
                   "失败卡片里没有 <img>/<video>（不会显示全黑预览图）",
                   f"媒体元素数={has_media}")
            text = card.first.inner_text()
            print(f"  卡片文本: {text[:240]!r}")
            record("失败" in text and ("显存" in text or "不足" in text),
                   "失败卡片上显示了明确失败原因（显存不足）",
                   f"卡片文本片段={text[:160]!r}")
        else:
            print("  !! 页面上没找到该卡片")
            record(False, "页面上找到该失败任务卡片", "找不到")
        browser.close()


if __name__ == "__main__":
    sys.exit(main())
