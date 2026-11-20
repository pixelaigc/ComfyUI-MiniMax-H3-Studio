#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""修前/修后对照：起一个隔离实例，用**同一组**请求量测。

用法:
    python scripts/compare_perf_beforeafter.py \
        --before /root/projects/h3studio-live \
        --after  /root/projects/h3s-favorites
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "..", "evidence", "media-perf")
JOBS = "/api/jobs?status=done&page=1&page_size=12"


def get(base, path, timeout=180):
    """返回 (毫秒, 字节数, HTTP 状态码)。

    【踩坑记录】第一版在成功分支把状态码写成 **0**，于是
    「等服务起来」的轮询 `if code == 200` 永远不成立 ->
    明明服务已经打印「已启动」却报「起不来」。
    """
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            n = len(r.read())
            return (time.perf_counter() - t0) * 1000.0, n, r.status
    except Exception as e:  # noqa: BLE001
        return (time.perf_counter() - t0) * 1000.0, 0, getattr(e, "code", 0)


def st(xs):
    s = sorted(xs)
    return {"n": len(s), "p50": round(s[len(s) // 2], 1),
            "p90": round(s[int(len(s) * 0.9) - 1], 1) if len(s) >= 10 else round(max(s), 1),
            "max": round(max(s), 1), "mean": round(sum(s) / len(s), 1)}


def run_instance(src_dir, port, label):
    """把某个 worktree 复制到临时目录，用独立端口起服务。"""
    tmp = tempfile.mkdtemp(prefix=f"perf-{label}-")
    dst = os.path.join(tmp, "app")
    # 【为什么用 `cp -a` 而不是 shutil.copytree】
    #   worktree 里有 .git 文件（指向主仓库的 gitdir 链接），copytree 遇到
    #   指向目录的符号链接会报 "CalledProcessError" / 权限错。
    #   `cp -a` 保留一切，且忽略 .git（服务不需要 git 元数据）。
    subprocess.run(
        f"cp -a {shlex.quote(src_dir)}/. {shlex.quote(dst)}/ "
        f"&& rm -rf {shlex.quote(dst)}/.git {shlex.quote(dst)}/data "
        f"{shlex.quote(dst)}/evidence {shlex.quote(dst)}/__pycache__",
        shell=True, check=True)
    # data 目录用软链（真库很大，不复制）
    real_data = "/root/projects/comfyui-minimax-h3-studio/data"
    os.symlink(real_data, os.path.join(dst, "data"))
    env = dict(os.environ)
    env["H3STUDIO_PORT"] = str(port)
    # data_dir 指向**线上真库**（只读量测用；不写）
    env["H3STUDIO_DATA_DIR"] = real_data
    logfile = open(os.path.join(tmp, "server.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-u", "server.py"], cwd=dst, env=env,
        stdout=logfile, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    # 等起来
    for _ in range(120):
        try:
            code = get(base, "/api/generation-types", timeout=3)[2]
            if code == 200:
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    else:
        proc.kill()
        try:
            with open(os.path.join(tmp, "server.log"), encoding="utf-8") as f:
                out = f.read()[-4000:]
        except Exception:  # noqa: BLE001
            out = ""
        raise RuntimeError(f"{label} 起不来\n--- 服务输出 ---\n{out}")
    return proc, base, dst, tmp


def measure(base, label):
    # 预热（让服务端填自己的缓存）
    get(base, "/api/video-options")
    out = {"label": label}
    out["video_options"] = st([get(base, "/api/video-options")[0] for _ in range(6)])
    out["jobs_idle"] = st([get(base, JOBS)[0] for _ in range(20)])
    # 并发场景：3 个 video-options 同时压，期间采 /api/jobs
    import threading
    samples = []
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            samples.append(get(base, JOBS)[0])
            time.sleep(0.02)

    def hammer():
        get(base, "/api/video-options")

    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    ws = [threading.Thread(target=hammer) for _ in range(3)]
    for w in ws:
        w.start()
    for w in ws:
        w.join()
    stop.set()
    th.join(timeout=5)
    out["jobs_under_video_options_load"] = st(samples)
    out["slowdown_x"] = round(out["jobs_under_video_options_load"]["p50"]
                              / max(out["jobs_idle"]["p50"], 0.001), 1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", default="/root/projects/h3studio-live")
    ap.add_argument("--after", default="/root/projects/h3s-favorites")
    ap.add_argument("--port-before", type=int, default=8911)
    ap.add_argument("--port-after", type=int, default=8912)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    results = {}
    for label, src, port in [("before", args.before, args.port_before),
                             ("after", args.after, args.port_after)]:
        print(f"=== {label} ({src}) 起隔离实例 :{port} ===")
        proc, base, dst, tmp = run_instance(src, port, label)
        try:
            results[label] = measure(base, label)
            print(json.dumps(results[label], ensure_ascii=False, indent=2))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                proc.kill()
            shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== 对照 ===")
    b, a = results["before"], results["after"]
    print(f"  /api/video-options p50 : {b['video_options']['p50']}ms -> {a['video_options']['p50']}ms")
    print(f"  /api/jobs 空闲 p50     : {b['jobs_idle']['p50']}ms -> {a['jobs_idle']['p50']}ms")
    print(f"  /api/jobs 负载下 p50   : {b['jobs_under_video_options_load']['p50']}ms -> "
          f"{a['jobs_under_video_options_load']['p50']}ms")
    print(f"  负载下劣化倍数         : x{b['slowdown_x']} -> x{a['slowdown_x']}")

    path = args.out or f"{OUT_DIR}/beforeafter-isolated.json"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n证据: {path}")


if __name__ == "__main__":
    main()
