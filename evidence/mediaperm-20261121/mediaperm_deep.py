#!/usr/bin/env python3
"""深挖 summarize(t2i_moss) 的 686ms 到底花在哪（逐步骤计时）。

对 t2i_moss 行逐个内部步骤计时：
  _image_summary / _artifact_lora_overlay / _attention_summary /
  _generation_params / _recommendations
"""
import json
import os
import shutil
import sys
import tempfile
import time

LIVE = sys.argv[1] if len(sys.argv) > 1 else "/root/projects/h3studio-live"
REAL_DB = "/root/projects/comfyui-minimax-h3-studio/data/jobs.db"

tmp = tempfile.mkdtemp(prefix="h3deep-")
shutil.copy2(REAL_DB, os.path.join(tmp, "jobs.db"))
os.environ["H3STUDIO_DATA_DIR"] = tmp
os.environ["H3STUDIO_PORT"] = "8399"
sys.path.insert(0, LIVE)

import server as S  # noqa: E402
from h3studio import job_meta as jm  # noqa: E402

row = S.db().execute(
    "SELECT * FROM jobs WHERE mode='t2i_moss' ORDER BY created_at DESC LIMIT 1").fetchone()
params = json.loads(row["params"] or "{}")
print("row:", row["id"], "mode:", row["mode"], "status:", row["status"])
print("params keys:", sorted(params.keys())[:30])

STEPS = [
    ("_image_summary", lambda: jm._image_summary(params, row["mode"])),
    ("_artifact_lora_overlay", lambda: jm._artifact_lora_overlay(params)),
    ("_attention_summary", lambda: jm._attention_summary(params)),
    ("_generation_params", lambda: jm._generation_params(params)),
]


def timeit(label, fn, n=3):
    best = None
    for _ in range(n):
        t = time.time()
        r = fn()
        dt = (time.time() - t) * 1000
        best = dt if best is None else min(best, dt)
    print(f"  {label:<26} {best:9.2f}ms")
    return best


print("\n=== 第一次（冷）===")
t = time.time()
jm.summarize(row["mode"], params)
print(f"  summarize TOTAL           {(time.time()-t)*1000:9.2f}ms")
for lab, fn in STEPS:
    timeit(lab, fn)

print("\n=== 第二次（热）===")
t = time.time()
jm.summarize(row["mode"], params)
print(f"  summarize TOTAL           {(time.time()-t)*1000:9.2f}ms")
for lab, fn in STEPS:
    timeit(lab, fn)

# _image_summary 内部再拆
print("\n=== _image_summary 内部再拆（热）===")
t = time.time()
from h3studio import workflows as wf  # noqa
print(f"  import workflows           {(time.time()-t)*1000:9.2f}ms")
t = time.time()
tbl = jm._image_model_table()
print(f"  _image_model_table()       {(time.time()-t)*1000:9.2f}ms  (n={len(tbl)})")
model_key = str(params.get("model") or "").strip()
print("  model_key =", repr(model_key))
t = time.time()
meta = tbl.get(model_key) or {}
print(f"  tbl.get(model_key)         {(time.time()-t)*1000:9.2f}ms")
