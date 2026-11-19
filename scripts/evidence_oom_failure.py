#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""缺陷 2 的**真机**证据：真实 OOM 的任务必须判失败，且不产生黑图。

【为什么不能只靠单测】
单测证明的是「判定层」的逻辑。真实证据要证明：
  1. ComfyUI 侧真的会因为显存不足而 failed（真 OOM，不是造的桩）；
  2. **服务端**把这条任务判成 failed（不是 done）；
  3. 接口下发的 payload 里**没有任何可渲染的产物**（前端不会显示黑图）；
  4. 前端对「done + 无产物」有明确的失败占位（不是空白黑框）。

【做法】
  1. 通过**线上服务** POST /api/jobs 提交一条 t2i 任务，
     分辨率设成 8192×8192（本机 48GB 显存必然 OOM）；
  2. 等 worker 跑完（最多 15 分钟）；
  3. 读 /api/jobs/{id}：status 必须是 'failed'、error 必须含「显存不足」、
     outputs 必须是 []；
  4. 对照：ComfyUI 的 history 里 status_str 必须是 'error'；
  5. 用真实浏览器打开生成库，截图失败卡片（无黑图）。

用法：python3 scripts/evidence_oom_failure.py [--shots]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.request
import uuid

BASE = "http://127.0.0.1:8190"
COMFY = "http://127.0.0.1:8188"
DB = "/root/projects/comfyui-minimax-h3-studio/data/jobs.db"
OUT = "/root/projects/h3s-merge-20261119/evidence/gen-status-20261119"

RESULTS = []


def record(ok, name, detail):
    RESULTS.append({"ok": bool(ok), "name": name, "detail": detail})
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def get_json(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def submit_image_job():
    """通过线上服务提交一条**必然 OOM** 的出图任务。"""
    boundary = uuid.uuid4().hex
    parts = []

    def field(name, value):
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8"))

    field("mode", "t2i")
    field("params", json.dumps({
        "prompt": "a red cube on a white table, studio light",
        # 8192x8192 —— 本机 48GB 显存必然 OOM（这是**故意**的）
        "width": 8192,
        "height": 8192,
        "seed": 42,
        "steps": 4,
    }, ensure_ascii=False))
    body = b"".join(parts) + f"--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/api/jobs", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", action="store_true", help="用真实浏览器截图")
    args = ap.parse_args()

    import os
    os.makedirs(OUT, exist_ok=True)

    print("═══ 提交一条必然 OOM 的出图任务（8192×8192） ═══")
    resp = submit_image_job()
    job_id = resp.get("id")
    print(f"  任务已提交: {job_id}")
    if not job_id:
        print("  !! 提交失败:", resp)
        return 1

    # 等它到终态
    deadline = time.time() + 900
    job = None
    while time.time() < deadline:
        job = get_json(f"{BASE}/api/jobs/{job_id}")
        if job.get("status") in ("done", "failed", "canceled"):
            break
        time.sleep(5)
    else:
        record(False, "OOM 任务在 15 分钟内到终态", f"仍是 {job.get('status')}")
        return 1

    print(f"  任务终态: status={job.get('status')}")
    print(f"  error    = {job.get('error')}")
    print(f"  outputs  = {json.dumps(job.get('outputs'), ensure_ascii=False)}")

    # 1) 必须判失败
    record(job.get("status") == "failed",
           "OOM 任务判定为 failed（不是 done / 生成成功）",
           f"status={job.get('status')}")

    # 2) 原因必须明确指出显存不足
    err = str(job.get("error") or "")
    record("显存不足" in err or "out of memory" in err.lower(),
           "失败原因明确指出显存不足（OOM）",
           f"error={err[:120]}")

    # 3) 不得有任何产物（前端不会显示黑图）
    outs = job.get("outputs") or []
    record(len(outs) == 0,
           "失败任务没有任何产物条目（前端不会渲染出黑图）",
           f"outputs={json.dumps(outs, ensure_ascii=False)[:200]}")

    # 4) ComfyUI 侧确实是 error（证明这是真 OOM，不是我们编的）
    prompt_id = job.get("prompt_id") or ""
    comfy_hist = {}
    if prompt_id:
        try:
            h = get_json(f"{COMFY}/history/{prompt_id}")
            comfy_hist = h.get(prompt_id) or {}
        except Exception as exc:
            print(f"  !! 取 ComfyUI history 失败: {exc}")
    status_info = comfy_hist.get("status") or {}
    print(f"  ComfyUI 侧: status_str={status_info.get('status_str')!r} "
          f"outputs_keys={list((comfy_hist.get('outputs') or {}).keys())}")
    record(status_info.get("status_str") == "error",
           "ComfyUI 侧确实是执行失败（真 OOM，不是构造的）",
           f"status_str={status_info.get('status_str')!r}")

    # 5) ComfyUI 的 outputs 里**可能有**已执行完节点的输出 —— 正是原缺陷的触发条件
    comfy_outputs = comfy_hist.get("outputs") or {}
    legacy_would_succeed = bool(comfy_outputs)
    print(f"  【原缺陷触发条件】ComfyUI outputs 非空 = {legacy_would_succeed} "
          f"（keys={list(comfy_outputs.keys())}）")
    print(f"     旧逻辑只看 outputs 非空 -> 会判「成功」；"
          f"新逻辑先看 status -> 判失败。")

    # 存证据
    out = {
        "job_id": job_id,
        "prompt_id": prompt_id,
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
        "verdict_failed": job.get("status") == "failed",
        "verdict_no_outputs": len(outs) == 0,
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(f"{OUT}/evidence_oom_failure.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print(f"  证据已存: {OUT}/evidence_oom_failure.json")

    if args.shots:
        subprocess_shot(job_id)

    passed = sum(1 for r in RESULTS if r["ok"])
    print(f"\n通过 {passed}/{len(RESULTS)}")
    with open(f"{OUT}/evidence_oom_summary.json", "w", encoding="utf-8") as fh:
        json.dump(RESULTS, fh, ensure_ascii=False, indent=2)
    return 0 if passed == len(RESULTS) else 1


def subprocess_shot(job_id):
    """用真实浏览器打开生成库，截图这条失败卡片。"""
    print("\\n═══ 真实浏览器截图 ═══")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        print(f"  !! 没有 playwright: {exc}")
        return
    shot = f"{OUT}/oom-failed-card.png"
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.goto(f"{BASE}/", wait_until="networkidle")
        page.wait_for_timeout(3000)
        # 切到「生成库」页
        for sel in ("text=生成库", "text=作品", "a:has-text('生成')"):
            try:
                page.locator(sel).first.click(timeout=3000)
                break
            except Exception:
                continue
        page.wait_for_timeout(2000)
        # 定位这条失败任务卡片
        card = page.locator(f'.job-card[data-jid="{job_id}"]')
        if card.count():
            card.first.scroll_into_view_if_needed()
            page.wait_for_timeout(800)
            card.first.screenshot(path=shot)
            print(f"  已截图失败卡片: {shot}")
            # 断言卡片里**没有** <img>/<video>（= 没有黑图预览）
            has_media = card.first.locator(".job-preview img, .job-preview video").count()
            record(has_media == 0,
                   "失败卡片里没有 <img>/<video>（不会显示全黑预览图）",
                   f"媒体元素数={has_media}")
            text = card.first.inner_text()
            record("显存不足" in text,
                   "失败卡片上显示了明确失败原因（显存不足）",
                   f"卡片文本片段={text[:160]!r}")
        else:
            print("  !! 页面上没找到该卡片（可能不在当前页）")
            record(False, "页面上找到该失败任务卡片", "找不到")
        browser.close()


if __name__ == "__main__":
    sys.exit(main())
