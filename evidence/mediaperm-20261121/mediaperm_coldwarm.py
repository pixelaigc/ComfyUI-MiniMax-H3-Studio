#!/usr/bin/env python3
"""区分「冷启动一次性成本」与「每请求固定成本」。

同一个进程里:
  1) 第一次 summarize(t2i_moss 行) -> 冷 import
  2) 第二次 summarize(同一行)      -> 热路径
并单独测 live_states_for_jobs 在「有 running 任务」时的成本。
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time

LIVE = "/root/projects/h3studio-live"
REAL_DB = "/root/projects/comfyui-minimax-h3-studio/data/jobs.db"

tmp = tempfile.mkdtemp(prefix="h3perf3-")
shutil.copy2(REAL_DB, os.path.join(tmp, "jobs.db"))
os.environ["H3STUDIO_DATA_DIR"] = tmp
os.environ["H3STUDIO_PORT"] = "8399"
sys.path.insert(0, LIVE)

import server as S  # noqa: E402

row = S.db().execute(
    "SELECT * FROM jobs WHERE mode='t2i_moss' ORDER BY created_at DESC LIMIT 1").fetchone()
params = json.loads(row["params"] or "{}")
print("row:", row["id"], "mode:", row["mode"])

for i in range(4):
    t = time.time()
    S.job_meta.summarize(row["mode"], params)
    print(f"  summarize call #{i+1}: {(time.time()-t)*1000:.2f}ms")

# 另一条 t2i_moss（如果有）
row2 = S.db().execute(
    "SELECT * FROM jobs WHERE mode='t2i_moss' ORDER BY created_at DESC LIMIT 1 OFFSET 1").fetchone()
if row2:
    p2 = json.loads(row2["params"] or "{}")
    t = time.time()
    S.job_meta.summarize(row2["mode"], p2)
    print(f"  summarize (另一条 t2i_moss {row2['id']}): {(time.time()-t)*1000:.2f}ms")

print("\n=== 热路径下整页 100 条 row_to_job ===")
rows = S.db().execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100").fetchall()
for trial in range(2):
    t = time.time()
    for r in rows:
        S.row_to_job(r)
    print(f"  trial {trial+1}: {(time.time()-t)*1000:.1f}ms")


async def live_test():
    print("\n=== live_states_for_jobs（有 running 任务时）===")
    fake = [{"id": "x1", "status": "running", "prompt_id": "deadbeef",
             "started_at": time.time() - 10, "mode": "t2v"}]
    t = time.time()
    r = await S.live_states_for_jobs(fake)
    print(f"  冷（/queue 未缓存）: {(time.time()-t)*1000:.1f}ms  -> {r}")
    t = time.time()
    r = await S.live_states_for_jobs(fake)
    print(f"  热（/queue 走缓存）: {(time.time()-t)*1000:.1f}ms  -> {r}")


asyncio.run(live_test())

print("\n=== ComfyUI 可达性 / /queue 响应时间 ===")
import urllib.request
for path in ("/queue",):
    t = time.time()
    try:
        with urllib.request.urlopen("http://127.0.0.1:8188" + path, timeout=10) as resp:
            n = len(resp.read())
        print(f"  {path}: {(time.time()-t)*1000:.1f}ms bytes={n}")
    except Exception as e:
        print(f"  {path}: ERROR {e}")
