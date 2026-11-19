#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""等 GPU0 腾出足够显存后，在线上 8190 串行跑 N 条 flf 接续链。

【为什么需要等】
本机 48GB 单卡，ComfyUI 与其它 agent 的推理服务（strata-dsv41）共用 GPU0。
H3 int8 主模型约 34GB，显存不足时 ComfyUI 进程会被直接打崩
（实测 GPU 41.7GB -> 441MiB，history 被清空）。
⇒ 显存不足时**绝不提交**，只轮询等待；等到就绪才串行跑链。

【口径】
与上一轮 / dd7ab82 同一份测量器（等价 L1 口径，不是官方 HF 能量项）。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

LIVE = Path("/root/projects/h3studio-live")
NEED_MIB = 36000  # H3 int8 权重 ~34GB + 激活/VAE 余量
POLL_SEC = 30
DEADLINE_MIN = 240


def gpu_free_mib() -> int:
    out = subprocess.run(
        ["nvidia-smi", "-i", "0", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=30).stdout.strip()
    try:
        return int(out.splitlines()[0])
    except Exception:
        return -1


def comfy_alive() -> bool:
    r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                        "http://127.0.0.1:8188/system_stats"],
                       capture_output=True, text=True, timeout=20).stdout.strip()
    return r == "200"


def main() -> int:
    chains = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 20261131
    out_root = sys.argv[3] if len(sys.argv) > 3 else \
        "/root/projects/h3s-resume-cand/evidence/evidence-20261120-resume-flf"

    t0 = time.time()
    waited_log = []
    while True:
        free = gpu_free_mib()
        ok = comfy_alive()
        waited_log.append({"t": round(time.time() - t0), "free_mib": free, "comfy": ok})
        print(f"[wait] free={free}MiB comfy={ok} elapsed={int(time.time()-t0)}s", flush=True)
        if free >= NEED_MIB and ok:
            print("[wait] 显存就绪，开始串行跑链", flush=True)
            break
        if (time.time() - t0) / 60 > DEADLINE_MIN:
            print("[wait] 超时：显存始终不足，未提交任何生成任务", flush=True)
            Path(out_root).mkdir(parents=True, exist_ok=True)
            json.dump({"status": "blocked", "reason": "gpu_vram_busy",
                       "need_mib": NEED_MIB, "wait_log": waited_log},
                      open(Path(out_root) / "gpu_wait_log.json", "w"),
                      ensure_ascii=False, indent=1)
            return 3
        time.sleep(POLL_SEC)

    cmd = [sys.executable, str(LIVE / "tests" / "live_flf_seam_repeat.py"),
           "--chains", str(chains), "--base-seed", str(seed), "--out-root", out_root]
    print("[run]", " ".join(cmd), flush=True)
    rc = subprocess.run(cmd, cwd=str(LIVE)).returncode
    print(f"[run] exit={rc}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
