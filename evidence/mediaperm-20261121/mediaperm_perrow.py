#!/usr/bin/env python3
"""逐行定位 row_to_job 里的慢函数（只读）。

在「全部」标签第一页里逐行计时，拆出：
  row_to_job 内部的 _job_resolution_info / _continuation_info_for_row /
  gen_params / summarize / batches_of 各自耗时
"""
import json
import os
import shutil
import sys
import tempfile
import time

LIVE = "/root/projects/h3studio-live"
REAL_DB = "/root/projects/comfyui-minimax-h3-studio/data/jobs.db"

tmp = tempfile.mkdtemp(prefix="h3perf2-")
shutil.copy2(REAL_DB, os.path.join(tmp, "jobs.db"))
os.environ["H3STUDIO_DATA_DIR"] = tmp
os.environ["H3STUDIO_PORT"] = "8399"
sys.path.insert(0, LIVE)

import server as S  # noqa: E402

rows = S.db().execute(
    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100").fetchall()

per_row = []
for r in rows:
    mode = r["mode"]
    params = json.loads(r["params"] or "{}")
    outputs = json.loads(r["outputs"] or "[]")
    t = time.time()
    mi = S.job_meta.summarize(mode, params)
    t_sum = time.time() - t
    t = time.time()
    ri = S._job_resolution_info(params, outputs)
    t_reso = time.time() - t
    t = time.time()
    ci = S._continuation_info_for_row(r, params)
    t_cont = time.time() - t
    t = time.time()
    gp = S.job_meta.gen_params(mode, params, mi, ri)
    t_gp = time.time() - t
    t = time.time()
    b = S.batches_of(r["id"])
    t_bat = time.time() - t
    per_row.append({
        "id": r["id"], "mode": mode, "status": r["status"],
        "t_summarize_ms": round(t_sum * 1000, 2),
        "t_resolution_ms": round(t_reso * 1000, 2),
        "t_continuation_ms": round(t_cont * 1000, 2),
        "t_genparams_ms": round(t_gp * 1000, 2),
        "t_batches_ms": round(t_bat * 1000, 2),
        "total_ms": round((t_sum + t_reso + t_cont + t_gp + t_bat) * 1000, 2),
        "has_cont": bool((ci or {}).get("total_frames")),
        "cont_state": (ci or {}).get("state"),
    })

per_row.sort(key=lambda x: -x["total_ms"])
print("=== 全部标签第一页：最慢 15 行 ===")
for x in per_row[:15]:
    print(f"  {x['total_ms']:8.2f}ms  cont={x['t_continuation_ms']:8.2f} reso={x['t_resolution_ms']:7.2f}"
          f" sum={x['t_summarize_ms']:6.2f} gp={x['t_genparams_ms']:6.2f} bat={x['t_batches_ms']:5.2f}"
          f"  {x['mode']:<12} {x['status']:<8} cont_state={x['cont_state']}")

tot = sum(x["total_ms"] for x in per_row)
print(f"\n合计 {tot:.1f}ms / {len(per_row)} 行")
agg = {}
for k in ("t_summarize_ms", "t_resolution_ms", "t_continuation_ms", "t_genparams_ms", "t_batches_ms"):
    agg[k] = round(sum(x[k] for x in per_row), 1)
print("分项合计:", json.dumps(agg, ensure_ascii=False))

slow_cont = [x for x in per_row if x["t_continuation_ms"] > 5]
print(f"\ncontinuation 超过 5ms 的行: {len(slow_cont)}")
for x in slow_cont[:10]:
    print(f"  {x['id']} mode={x['mode']} t_cont={x['t_continuation_ms']}ms cont_state={x['cont_state']}")

json.dump(per_row, open("/root/.hermes-web-ui/.ekko/workspace/default/"
                         "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/"
                         "evidence/mediaperm-20261121/per-row-profile.json", "w"),
          ensure_ascii=False, indent=2)
print("\nwrote per-row-profile.json")
