#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""在 /api/video-options 压测期间用 py-spy 抓服务端的 Python 栈。

目的：证明「/api/jobs 被拖慢」的机制是**事件循环被同步的 safetensors 读盘
占住**（而不是 GIL 抢占或连接排队）。

用法: python scripts/spy_on_server.py --seconds 8
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
import urllib.request
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "..", "evidence", "media-perf")


def hammer(base, stop):
    while not stop.is_set():
        try:
            urllib.request.urlopen(base + "/api/video-options?x=1", timeout=30).read()
        except Exception:  # noqa: BLE001
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--interval", type=float, default=0.12)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    pid = args.pid
    if not pid:
        out = subprocess.run(["systemctl", "show", "h3-studio.service", "-p", "MainPID", "--value"],
                             capture_output=True, text=True).stdout.strip()
        pid = int(out)
    print(f"target pid = {pid}")

    stop = threading.Event()
    workers = [threading.Thread(target=hammer, args=(args.base, stop), daemon=True) for _ in range(3)]
    for w in workers:
        w.start()
    time.sleep(0.3)

    samples = []
    end = time.time() + args.seconds
    while time.time() < end:
        try:
            r = subprocess.run(["/root/.local/bin/py-spy", "dump", "--pid", str(pid)],
                               capture_output=True, text=True, timeout=20)
            samples.append(r.stdout)
        except Exception as e:  # noqa: BLE001
            samples.append(f"ERR {e}")
        time.sleep(args.interval)
    stop.set()
    for w in workers:
        w.join(timeout=3)

    # 提取**每个线程**的栈顶帧 —— py-spy dump 的格式是：
    #   Thread <id> (idle|active): "<name>"
    #       frame (file:line)
    #       ...
    # 上一版按 "Thread " 开头切块后取每块第一行，那第一行是
    # `Thread 2083285 (idle): "MainThread"` 本身（不是栈帧），
    # 于是统计出来全是 "Thread xxx" 这种无意义串。
    # 正确做法：找到 "MainThread" 那一块，取它的**栈帧行**。
    main_stacks = []
    all_threads = []
    for s in samples:
        cur_name = None
        cur_frames = []
        for ln in s.splitlines():
            if ln.startswith("Thread "):
                if cur_name is not None:
                    all_threads.append((cur_name, cur_frames, cur_state))
                cur_name = ln
                cur_state = "idle" if "(idle)" in ln else "active"
                cur_frames = []
                continue
            if cur_name is not None:
                # 栈帧行形如 "    frame (file:line)"
                st = ln.strip()
                if st and st[0].isalpha() or (st and st[0] == "_"):
                    if "(" in st and ")" in st:
                        cur_frames.append(st.split("(")[0].strip())
        if cur_name is not None:
            all_threads.append((cur_name, cur_frames, cur_state))

        for name, frames, state in all_threads:
            if "MainThread" in name:
                main_stacks.append((frames, state))

    frames = Counter()
    main_frames = Counter()
    main_state = Counter()
    interesting = []
    for frames_list, state in main_stacks:
        if frames_list:
            main_frames[frames_list[0]] += 1
            main_state[state] += 1
            frames.update(frames_list[:8])
            interesting.append(frames_list[:16])

    print(f"\n=== 采样 {len(samples)} 次；MainThread 状态分布 ===")
    print("  ", dict(main_state))
    print("\n=== MainThread 栈顶帧分布 ===")
    for f, c in main_frames.most_common(12):
        print(f"  {c:>4} 次  {f}")
    print("\n=== 出现过的关键帧（按出现次数）===")
    for f, c in frames.most_common(30):
        if any(k in f for k in ("safetensors", "lora", "compat", "torch", "read", "open",
                                 "accel", "video_models", "options_payload", "refresh_comfy",
                                 "aiohttp", "web_protocol", "server")):
            print(f"  {c:>4} 次  {f}")

    if interesting:
        print("\n=== 一个完整主线程栈样本 ===")
        for f in interesting[0]:
            print("   ", f)

    path = args.out or f"{OUT_DIR}/pyspy-video-options.json"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"pid": pid, "samples": len(samples),
                   "top_frames": dict(main_frames.most_common(20)),
                   "key_frames": {k: v for k, v in frames.most_common(60)},
                   "main_state": dict(main_state),
                   "sample_stacks": interesting[:5]}, f, ensure_ascii=False, indent=2)
    print(f"\n证据: {path}")


if __name__ == "__main__":
    main()
