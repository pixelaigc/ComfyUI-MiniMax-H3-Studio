#!/usr/bin/env python
"""给上一轮 seam_repeat.json 补上「段2 采样节点名」等新字段。

依据：同目录下的 workflow_job2_<prompt_id>.json（ComfyUI history 权威导出）。
不改原始指标数值，只补采样路径取证字段。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SEAM = Path("/root/projects/h3studio-flf-lum-20261118/tests/live_flf_seam_repeat.py")
spec = importlib.util.spec_from_file_location("seam_mod", SEAM)
assert spec is not None and spec.loader is not None
seam = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seam)


def main(target: str) -> int:
    tj = Path(target)
    data = json.loads(tj.read_text(encoding="utf-8"))
    out_dir = tj.parent
    # prompt_id -> 采样节点标签
    wf_index: dict[str, dict] = {}
    for wf_path in out_dir.glob("workflow_job*.json"):
        wf = json.loads(wf_path.read_text(encoding="utf-8"))
        a = seam.analyze_workflow(wf)
        # workflow_job2_<pid>.json -> pid
        pid = wf_path.stem.replace("workflow_job1_", "").replace("workflow_job2_", "")
        wf_index[pid] = a

    n_patch = 0
    for row in data.get("stats") or []:
        # stats 里没存 prompt_id，从 chains 里按 job2 反查
        for ch in data.get("chains") or []:
            if ch.get("job2") != row.get("job2"):
                continue
            pid2 = ch.get("job2_prompt_id")
            pid1 = ch.get("job1_prompt_id")
            if pid2 and pid2 in wf_index:
                a2 = wf_index[pid2]
                row["job2_prompt_id"] = pid2
                row["job2_sampler_nodes"] = a2.get("sampler_node_labels")
                row["job2_selflift_count"] = a2.get("selflift_count")
                row["job2_has_standard_sampler"] = a2.get("has_standard_sampler")
                n_patch += 1
            if pid1 and pid1 in wf_index:
                a1 = wf_index[pid1]
                row["job1_prompt_id"] = pid1
                row["job1_sampler_nodes"] = a1.get("sampler_node_labels")
                row["job1_selflift_count"] = a1.get("selflift_count")
                row["job1_has_standard_sampler"] = a1.get("has_standard_sampler")
            # twopass 落库值 + normalize_notes
            for label in ("job1", "job2"):
                tw = ch.get(f"{label}_twopass_in_db")
                if isinstance(tw, dict):
                    row[f"{label}_twopass_in_db"] = tw.get("enabled")
                nn = ch.get(f"{label}_normalize_notes")
                if nn:
                    row[f"{label}_normalize_notes"] = nn
    data["_backfilled_by"] = "tests/seam_lum_closure_stats.py 的补录步骤"
    tj.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  补录 {n_patch} 条 → {tj}")
    for row in data.get("stats") or []:
        print(f"    {row.get('tag')} seed={row.get('seed')} job2={row.get('job2')} "
              f"段2采样节点={row.get('job2_sampler_nodes')} "
              f"selflift={row.get('job2_selflift_count')} "
              f"twopass落库={row.get('job2_twopass_in_db')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
