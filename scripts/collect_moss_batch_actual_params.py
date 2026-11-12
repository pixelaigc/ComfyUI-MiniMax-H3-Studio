#!/usr/bin/env python3
"""C 项证据汇总：从 ComfyUI /history 读回**实际执行**的采样参数
（单张任务 + 5 张批次的每一张），并列出真实产出文件。

不依赖跑批的脚本是否还活着 —— 直接读 ComfyUI 的历史与 SQLite。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

COMFY = "http://127.0.0.1:8188"


def http_json(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def comfy_params(prompt_id):
    try:
        h = http_json(f"{COMFY}/history/{prompt_id}")
    except Exception:
        return None
    for entry in h.values():
        prompt = entry.get("prompt")
        nodes = prompt[2] if isinstance(prompt, (list, tuple)) and len(prompt) > 2 else prompt
        if not isinstance(nodes, dict):
            continue
        ks = sd = None
        for nid, n in nodes.items():
            if n.get("class_type") == "KSampler":
                ks = {"node": nid, **n["inputs"]}
            elif n.get("class_type") == "ModelSamplingSD3":
                sd = {"node": nid, "shift": n["inputs"].get("shift")}
        if ks:
            return {"ksampler": ks, "sampling": sd}
    return None


def main() -> int:
    data = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/h3-moss-batch-e2e2")
    out_path = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/h3-fix-evidence/moss_batch_actual_params.json")
    db = data / "jobs.db"
    if not db.is_file():
        print(f"!! 找不到 {db}")
        return 2
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    report: dict = {"data_dir": str(data), "cases": {}}
    jobs = list(conn.execute("SELECT id, mode, status, params FROM jobs ORDER BY created_at"))
    for job in jobs:
        p = json.loads(job["params"])
        bc = p.get("batch_count")
        batches = list(conn.execute(
            "SELECT idx, seed, status, prompt_id FROM job_batches WHERE job_id=? ORDER BY idx",
            (job["id"],)))
        # 单张任务没有 job_batches 行，参数直接从 job 记录 + ComfyUI history 取
        case = {
            "job_id": job["id"], "mode": job["mode"], "status": job["status"],
            "batch_count": bc,
            "panel_params": {k: p.get(k) for k in
                             ("steps", "cfg", "shift", "sampler", "scheduler",
                              "resolution", "aspect", "model")},
            "batches": [],
        }
        for b in batches:
            actual = comfy_params(b["prompt_id"]) if b["prompt_id"] else None
            row = {"idx": b["idx"], "seed": b["seed"], "status": b["status"],
                   "prompt_id": b["prompt_id"]}
            if actual:
                ks = actual["ksampler"]
                row["actual"] = {
                    "steps": ks.get("steps"), "cfg": ks.get("cfg"),
                    "sampler": ks.get("sampler_name"), "scheduler": ks.get("scheduler"),
                    "seed": ks.get("seed"),
                    "shift": (actual.get("sampling") or {}).get("shift"),
                }
            case["batches"].append(row)
        # 单张：从 job.prompt_id 取
        if not batches:
            jp = conn.execute("SELECT prompt_id, outputs FROM jobs WHERE id=?",
                              (job["id"],)).fetchone()
            if jp and jp["prompt_id"]:
                actual = comfy_params(jp["prompt_id"])
                if actual:
                    ks = actual["ksampler"]
                    case["actual"] = {
                        "steps": ks.get("steps"), "cfg": ks.get("cfg"),
                        "sampler": ks.get("sampler_name"), "scheduler": ks.get("scheduler"),
                        "seed": ks.get("seed"),
                        "shift": (actual.get("sampling") or {}).get("shift"),
                    }
            outs = json.loads(jp["outputs"]) if jp and jp["outputs"] else []
            case["outputs"] = [o.get("file") for o in outs]
        report["cases"][f"batch_count={bc}"] = case

    conn.close()

    # 产物文件清单
    img_dir = data / "outputs" / "images"
    files = sorted(img_dir.glob("*.png")) if img_dir.is_dir() else []
    report["output_files"] = [
        {"name": f.name, "size_bytes": f.stat().st_size,
         "mtime": f.stat().st_mtime} for f in files]

    print("=" * 74)
    print("=== C 项：MOSS-Image 批量步数实测对照（数据源 ComfyUI /history）===")
    print("=" * 74)
    for name, case in report["cases"].items():
        print(f"\n{name}  job={case['job_id']} status={case['status']}")
        print(f"  面板参数: {json.dumps(case['panel_params'], ensure_ascii=False)}")
        if case.get("actual"):
            a = case["actual"]
            print(f"  实测单张: steps={a['steps']} cfg={a['cfg']} "
                  f"sampler={a['sampler']} scheduler={a['scheduler']} "
                  f"shift={a['shift']} seed={a['seed']}")
        for b in case["batches"]:
            a = b.get("actual")
            if a:
                print(f"  实测 #{b['idx']}: steps={a['steps']} cfg={a['cfg']} "
                      f"sampler={a['sampler']} scheduler={a['scheduler']} "
                      f"shift={a['shift']} seed={a['seed']} (DB seed={b['seed']}, "
                      f"status={b['status']})")
            else:
                print(f"  #{b['idx']}: status={b['status']} 读不到 history")
        if case.get("outputs"):
            print(f"  产物: {case['outputs']}")
    print(f"\n产物文件（{len(files)} 个）:")
    for f in report["output_files"]:
        print(f"  {f['name']}  {f['size_bytes'] / 1024:.0f} KiB")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n证据写入 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
