#!/usr/bin/env python3
"""
全量 pytest 对跑器 —— **按文件**跑，每个文件独立超时。

【为什么不用 `pytest tests/` 一次跑完】
实测（2026-11-20）：整目录跑会在某个文件里挂死 40 分钟以上
（pytest 的输出被缓冲，看不到卡在哪）。按文件跑 + 逐文件超时
才能得到**完整的 FAILED/ERROR 名单**，而不是一个「跑了很久」的
未知状态。

用法：
    python3 run_pytest_per_file.py <out_json> [worktree]

输出：JSON {"total_files":N,"failed_files":[...],"results":{file:{"rc":..,"summary":"..","tests":[..]}}}
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

PER_FILE_TIMEOUT = int(os.environ.get("H3_PYTEST_FILE_TIMEOUT", "300"))
ROOT = Path(sys.argv[2] if len(sys.argv) > 2 else ".").resolve()
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/pytest_per_file.json")


def run_file(path: Path) -> dict:
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(path), "-q",
             "--timeout=120", "-p", "no:cacheprovider",
             "--tb=no", "-rN"],
            cwd=str(ROOT), capture_output=True, text=True,
            timeout=PER_FILE_TIMEOUT)
        rc = proc.returncode
        out = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        rc = -9
        parts = []
        for chunk in (exc.stdout, exc.stderr):
            if not chunk:
                continue
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8", "replace")
            parts.append(chunk)
        out = "".join(parts) + f"\n[TIMEOUT after {PER_FILE_TIMEOUT}s]"
    tail = [ln for ln in out.strip().splitlines() if ln.strip()]
    summary = ""
    for ln in reversed(tail):
        if " passed" in ln or " failed" in ln or " error" in ln or "skipped" in ln:
            summary = ln.strip()
            break
    if not summary and tail:
        summary = tail[-1].strip()
    return {"rc": rc, "summary": summary,
            "seconds": round(time.time() - started, 1),
            "output_tail": "\n".join(tail[-25:])}


def main() -> None:
    files = sorted((ROOT / "tests").glob("test_*.py"))
    results: dict = {}
    failed: list[str] = []
    for index, path in enumerate(files, 1):
        rel = str(path.relative_to(ROOT))
        result = run_file(path)
        results[rel] = result
        if result["rc"] != 0:
            failed.append(rel)
        print(f"[{index}/{len(files)}] {rel} rc={result['rc']} "
              f"{result['seconds']}s :: {result['summary'][:110]}",
              flush=True)
    payload = {"total_files": len(files), "failed_files": failed,
               "results": results}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\n=== {len(files)} files, {len(failed)} failed ===")
    for name in failed:
        print("  FAIL:", name)
    print("written:", OUT)


if __name__ == "__main__":
    main()
