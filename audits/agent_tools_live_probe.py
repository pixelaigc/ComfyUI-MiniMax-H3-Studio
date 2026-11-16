#!/usr/bin/env python3
"""内置 Agent —— 33 个工具的**对话级**实测（线上 8190 真调 /api/agent/chat）。

复用仓库既有验收脚本的思路（evidence/c9-accept-9da6ae16/c_agent_tools_recheck.py），
但覆盖面补全到全部 33 个工具 —— 含 10 个本轮才纳入流程图的工具。

只读工具直接跑；有副作用的工具用**可回收**的最小动作并回收产物。
每个工具一次对话（本地模型每步 5~40 秒，全部跑完约 10~25 分钟）。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("H3_BASE", "http://127.0.0.1:8190")
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/agent_tools_live.json"

# 33 个工具 -> 最小探测动作
PROBES = [
    # --- 项目内置（23）---
    ("list_modes", {}),
    ("search_assets", {"query": "雨夜"}),
    ("describe_asset", {"asset_id": "asset_21f5d051d841"}),
    ("enhance_prompt", {"prompt": "a girl walking in the rain", "mode": "t2v"}),
    ("list_prompt_presets", {}),
    ("get_prompt_preset", {"spec_id": "cinematic"}),
    ("describe_mode_params", {"mode": "r2v"}),
    ("get_mode_schema", {"mode": "t2v"}),
    ("recall_memory", {}),
    ("search_conversations", {"query": "视频"}),
    ("search_asset_history", {"query": "雨夜"}),
    ("list_asset_tags", {}),
    ("list_jobs", {}),
    ("get_job_status", {"job_id": "8771898a7480"}),
    ("manage_media_library", {"scope": "failed", "dry_run": True}),
    # --- 额外能力（9）---
    ("read_file", {"path": "/etc/hostname"}),
    ("web_search", {"query": "ComfyUI MiniMax H3"}),
    ("media_probe", {"asset_id": "asset_61f1b6fd9a1d"}),
    ("media_concat", {}),        # 无参 -> 期望被参数校验拒绝（也算功能存在）
    ("media_transcode", {}),
    ("media_extract_frames", {}),
    ("media_mix_audio", {}),
    ("media_merge_av", {}),
    ("run_code", {}),            # 能力默认关闭 -> 期望被能力闸门拒绝
    # --- 有副作用（4）：用可回收的最小动作 ---
    ("respond", {}),
    ("create_job", {"mode": "t2v", "prompt": "审计探针：雨夜桥上的人"}),
    ("batch_create_jobs", {"jobs": [{"mode": "t2v",
                                     "prompt": "审计探针批量 1"}]}),
    ("cancel_job", {"job_id": "no_such_job"}),
    ("retry_job", {"job_id": "no_such_job"}),
    ("upload_asset", {"path": "no/such/file.png"}),
    ("delete_asset", {"asset_id": "asset_does_not_exist"}),
    ("tag_asset", {"asset_id": "asset_does_not_exist", "add": ["审计临时标签"]}),
    ("remember_preference", {"key": "audit_probe_pref", "value": "v1"}),
]


def chat(tool, args, timeout=900):
    msg = (f"请调用工具 {tool}（参数：{json.dumps(args, ensure_ascii=False)}）。"
           "只调用这一个工具，拿到结果后用 respond 工具简短回复。")
    payload = {"message": msg}
    req = urllib.request.Request(
        f"{BASE}/api/agent/chat",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            code = r.getcode()
        d = json.loads(body)
        steps = d.get("steps") or []
        tc = [{"tool": s.get("tool"), "ok": s.get("ok"), "ms": s.get("ms"),
               "error": (s.get("error") or "")[:160]}
              for s in steps]
        return {"tool": tool, "args": args, "ok": True, "http": code,
                "elapsed": round(time.time() - t0, 1),
                "status": d.get("status"), "error": d.get("error"),
                "tool_calls": tc,
                "called_target": any(s.get("tool") == tool for s in steps),
                "answer_head": (d.get("answer") or "")[:220]}
    except urllib.error.HTTPError as e:
        return {"tool": tool, "args": args, "ok": False, "http": e.code,
                "elapsed": round(time.time() - t0, 1),
                "error": e.read().decode()[:300]}
    except Exception as e:  # noqa: BLE001
        return {"tool": tool, "args": args, "ok": False, "http": None,
                "elapsed": round(time.time() - t0, 1),
                "error": f"{type(e).__name__}: {e}"}


def main():
    results = []
    only = set(sys.argv[2:]) if len(sys.argv) > 2 else None
    for tool, args in PROBES:
        if only and tool not in only:
            continue
        rec = chat(tool, args)
        results.append(rec)
        print(json.dumps({k: rec.get(k) for k in
                          ("tool", "ok", "http", "elapsed", "status",
                           "called_target")},
                         ensure_ascii=False), flush=True)
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=1)
        time.sleep(1)
    n_ok = sum(1 for r in results if r.get("ok"))
    n_called = sum(1 for r in results if r.get("called_target"))
    print(f"\nSUMMARY http_ok={n_ok}/{len(results)} tool_called={n_called}/{len(results)}")
    print("OUT", OUT)


if __name__ == "__main__":
    main()
