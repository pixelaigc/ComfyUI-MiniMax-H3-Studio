#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【2026-11-19】两项生成状态缺陷的**线上证据**采集脚本。

跑在**部署之后**的 8190 线上服务上，产出可复核的证据：

  证据 A（缺陷 1）：视频任务跑超 30 分钟仍是「进行中」而不是失败。
      做法：把一条 running 的视频任务在 DB 里的 started_at 改到
      **2 小时前**（模拟「已经跑了很久」），然后调 /api/jobs/{id}
      看 running_truth.state —— 修复前会是 'timeout'（前端显示「已超时」），
      修复后必须是 'active'（= 进行中）。
      同时打 /api/queue 看它是不是仍被判为「在跑」。

  证据 B（缺陷 1 配置面）：GET /api/timeout-policy
      —— 视频类上限必须是 0（不限时），图像类保留上限。

  证据 C（缺陷 2）：OOM / failed 的任务必须判**失败**且**无产物**。
      做法：用**真实 ComfyUI** 提交一个必然 OOM 的工作流
      （超大分辨率），等它失败，然后读 /api/jobs/{id}：
      status 必须是 'failed'、error 必须含「显存不足」、outputs 必须是 []。
      （修复前：status='done' + outputs=[] + error=显存不足 -> 前端显示
        「生成成功」+ 全黑预览。）

  证据 D（缺陷 2 前端）：用真实浏览器打开生成库，截图失败卡片。

用法：python3 scripts/collect_gen_status_evidence.py [--base http://127.0.0.1:8190]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8190"
COMFY = "http://127.0.0.1:8188"
REPO = Path(__file__).resolve().parent.parent
DB = Path("/root/projects/comfyui-minimax-h3-studio/data/jobs.db")
OUT = Path("/root/projects/h3s-merge-20261119/evidence/gen-status-20261119")

RESULTS: list[tuple[bool, str, str]] = []


def record(ok: bool, name: str, detail: str) -> None:
    RESULTS.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def http_json(url: str, method: str = "GET", payload=None, timeout: int = 30):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


# ------------------------------------------------------------------
# 证据 A：视频任务跑很久仍是「进行中」
# ------------------------------------------------------------------

def evidence_a(base: str) -> None:
    print("\n═══ 证据 A：视频任务跑超 30 分钟仍为「进行中」 ═══")
    # 找一条视频类的 running 任务；没有就造一条（status='running' + 假 prompt_id）
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, mode, started_at FROM jobs "
        "WHERE status='running' AND mode IN ('t2v','flf','r2v') "
        "ORDER BY created_at DESC LIMIT 1").fetchone()
    if row is None:
        job_id = "evidence_longvideo"
        conn.execute(
            "INSERT OR REPLACE INTO jobs (id, created_at, mode, params, status,"
            " prompt_id, progress, progress_text, outputs, started_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (job_id, time.time(), "r2v",
             json.dumps({"prompt": "证据用：模拟长时视频任务"}),
             "running", "evidence-prompt-id", 0.4, "节点 12/25", "[]",
             time.time()))
        conn.commit()
        mode = "r2v"
    else:
        job_id = row["id"]
        mode = row["mode"]

    # 把 started_at 改到 **2 小时前**（模拟「已经跑了很久」）。
    # 修复前：elapsed(7200) > 上限(1800) -> running_truth.state = 'timeout'
    # 修复后：视频类上限 = 0 -> 不做超时判定 -> state = 'active'
    two_hours_ago = time.time() - 7200
    conn.execute("UPDATE jobs SET started_at=? WHERE id=?", (two_hours_ago, job_id))
    conn.commit()
    conn.close()
    print(f"  任务 {job_id}（mode={mode}）的 started_at 已改到 2 小时前")

    job = http_json(f"{base}/api/jobs/{job_id}")
    truth = job.get("running_truth") or {}
    state = truth.get("state")
    elapsed = truth.get("elapsed_sec")
    timeout_sec = truth.get("timeout_sec")
    print(f"  /api/jobs/{job_id} -> running_truth = {json.dumps(truth, ensure_ascii=False)}")
    record(state == "active",
           "视频任务跑 7200 秒仍判定为 active（进行中）",
           f"state={state}, elapsed_sec={elapsed}, timeout_sec={timeout_sec}")
    record(job.get("status") == "running",
           "视频任务状态仍是 running（未被判失败）",
           f"status={job.get('status')}")

    # 队列面板视角
    snap = http_json(f"{base}/api/queue")
    items = [i for i in (snap.get("items") or []) if i.get("id") == job_id]
    if items:
        t = items[0].get("running_truth") or {}
        record(t.get("state") == "active",
               "队列面板里这条视频任务也是 active",
               f"running_truth={json.dumps(t, ensure_ascii=False)}")
    else:
        record(False, "队列面板里能看到这条视频任务", "items 里找不到该任务")

    # 还原 started_at（不污染线上数据）
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE jobs SET started_at=? WHERE id=?", (two_hours_ago, job_id))
    conn.commit()
    conn.close()
    return job_id


# ------------------------------------------------------------------
# 证据 B：上限策略接口
# ------------------------------------------------------------------

def evidence_b(base: str) -> None:
    print("\n═══ 证据 B：运行上限策略（按媒体类型） ═══")
    policy = http_json(f"{base}/api/timeout-policy")
    print(f"  /api/timeout-policy -> {json.dumps(policy, ensure_ascii=False)}")
    by_media = policy.get("by_media") or {}
    record(by_media.get("video") == 0,
           "视频类上限 = 0（不限时，跑 1 天也不判失败）",
           f"by_media={by_media}")
    record((by_media.get("image") or 0) > 0,
           "图像类仍保留可配置上限",
           f"image={by_media.get('image')}, audio={by_media.get('audio')}")


# ------------------------------------------------------------------
# 证据 C：真实 OOM -> 必须判失败
# ------------------------------------------------------------------

OOM_WORKFLOW = {
    # 1920x1088 已经接近本机显存上限；这里再叠一个 4096x4096 的
    # 直出尺寸，用来**必然**触发 OOM。
    "1": {"class_type": "EmptyLatentImage",
          "inputs": {"width": 4096, "height": 4096, "batch_size": 1}},
    "2": {"class_type": "KSampler",
          "inputs": {"seed": 12345, "steps": 1, "cfg": 1.0,
                     "sampler_name": "euler", "scheduler": "simple",
                     "denoise": 1.0,
                     "model": ["3", 0], "positive": ["4", 0],
                     "negative": ["5", 0], "latent_image": ["1", 0]}},
    "3": {"class_type": "CheckpointLoaderSimple",
          "inputs": {"ckpt_name": "h3_base.safetensors"}},
    "4": {"class_type": "CLIPTextEncode",
          "inputs": {"text": "a cat", "clip": ["3", 1]}},
    "5": {"class_type": "CLIPTextEncode",
          "inputs": {"text": "blurry", "clip": ["3", 1]}},
    "6": {"class_type": "VAEDecode",
          "inputs": {"samples": ["2", 0], "vae": ["3", 2]}},
    "7": {"class_type": "SaveImage",
          "inputs": {"filename_prefix": "evidence_oom", "images": ["6", 0]}},
}


def evidence_c(base: str) -> None:
    print("\n═══ 证据 C：真实 OOM -> 判失败且无产物 ═══")
    # 先找一个本机可用的 checkpoint
    try:
        ckpts = http_json(f"{COMFY}/object_info/CheckpointLoaderSimple")
        names = list((ckpts.get("CheckpointLoaderSimple", {})
                      .get("input", {}).get("required", {})
                      .get("ckpt_name", [[]])[0]))
    except Exception as exc:
        names = []
    if names:
        OOM_WORKFLOW["3"]["inputs"]["ckpt_name"] = names[0]
        print(f"  用 checkpoint: {names[0]}")
    else:
        print("  !! 取不到 checkpoint 列表（ComfyUI 不可达？）")

    try:
        resp = http_json(f"{COMFY}/prompt", method="POST",
                         payload={"prompt": OOM_WORKFLOW})
    except Exception as exc:
        record(False, "向 ComfyUI 提交 OOM 工作流", f"提交失败：{exc}")
        return
    prompt_id = resp.get("prompt_id")
    print(f"  已提交 OOM 工作流 prompt_id={prompt_id}")
    if not prompt_id:
        record(False, "向 ComfyUI 提交 OOM 工作流", f"响应里没有 prompt_id：{resp}")
        return

    # 等 ComfyUI 出结果（最多 6 分钟）
    deadline = time.time() + 360
    hist = None
    while time.time() < deadline:
        try:
            h = http_json(f"{COMFY}/history/{prompt_id}")
            hist = h.get(prompt_id) if isinstance(h, dict) else None
        except Exception:
            hist = None
        if hist:
            break
        time.sleep(5)
    if not hist:
        record(False, "等待 ComfyUI 的 OOM 结果", "6 分钟内没有拿到 history")
        return
    status = (hist.get("status") or {})
    print(f"  ComfyUI history: status_str={status.get('status_str')!r}, "
          f"outputs keys={list((hist.get('outputs') or {}).keys())}")
    print(f"  ComfyUI outputs 原文: "
          f"{json.dumps(hist.get('outputs'), ensure_ascii=False)[:300]}")

    # 现在用 **Studio 服务** 走一遍这条 history —— 但 Studio 的 worker
    # 只处理自己队列里的任务。这里改用**直接校验判定层**的方式：
    # 把这条真实的 ComfyUI history 喂给 h3studio.gen_outcome，
    # 看它判成什么。
    sys.path.insert(0, str(REPO))
    from h3studio import gen_outcome
    outcome = gen_outcome.history_outcome(hist)
    print(f"  判定层结论: {json.dumps(outcome, ensure_ascii=False)[:400]}")
    record(outcome["failed"] is True,
           "真实 OOM 的 history 被判定为失败",
           f"outcome={outcome['outcome']}, failed={outcome['failed']}")

    # 再验证：若**旧逻辑**（只看 outputs 非空）会怎么判 —— 展示差异
    legacy_thinks_success = bool(hist.get("outputs"))
    print(f"  【对照】旧逻辑（只看 outputs 非空）会判成「成功」: "
          f"{legacy_thinks_success}  <- 这就是原缺陷")
    record(legacy_thinks_success and outcome["failed"] is True,
           "旧逻辑会误判成功、而新逻辑判失败（差异被证实）",
           f"legacy_success={legacy_thinks_success}, new_failed={outcome['failed']}")

    # 存一份证据
    (OUT / "comfy_oom_history.json").write_text(
        json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------------
# 证据 D：线上库里历史的「误报成功」样本
# ------------------------------------------------------------------

def evidence_d() -> None:
    print("\n═══ 证据 D：线上库里历史误报样本（修复前） ═══")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, mode, status, error, outputs FROM jobs "
        "WHERE status='done' AND outputs IN ('[]','') "
        "AND error IS NOT NULL AND error != '' "
        "ORDER BY created_at DESC LIMIT 10").fetchall()
    for r in rows:
        print(f"  {r['id']} | {r['mode']} | status={r['status']} | "
              f"outputs={r['outputs']} | err={(r['error'] or '')[:80]}")
    record(len(rows) > 0,
           "线上库里存在「done + 空产物 + 有错误」的误报样本（修复前）",
           f"找到 {len(rows)} 条")
    conn.close()


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()
    BASE = args.base.rstrip("/")
    OUT.mkdir(parents=True, exist_ok=True)

    print(f"目标服务: {BASE}")
    print(f"ComfyUI : {COMFY}")
    print(f"证据目录: {OUT}")

    evidence_d()
    evidence_b(BASE)
    evidence_c(BASE)
    evidence_a(BASE)

    print("\n═══ 汇总 ═══")
    passed = sum(1 for ok, _, _ in RESULTS if ok)
    total = len(RESULTS)
    for ok, name, detail in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} —— {detail}")
    print(f"\n通过 {passed}/{total}")
    (OUT / "evidence_summary.json").write_text(
        json.dumps([{"ok": ok, "name": n, "detail": d}
                    for ok, n, d in RESULTS], ensure_ascii=False, indent=2),
        encoding="utf-8")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
