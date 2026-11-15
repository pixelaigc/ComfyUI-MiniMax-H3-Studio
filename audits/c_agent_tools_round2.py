#!/usr/bin/env python3
"""C 项：内置 Agent 工具的**对话级**实测（线上 8190 真调 /api/agent/chat）。

每个工具一个最小探测动作。带副作用的（create_job / tag / upload / delete /
media_*）用可回收的最小动作，事后回收。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("H3_BASE", "http://127.0.0.1:8190")
OUT = "/root/projects/comfyui-minimax-h3-studio/audits/c_agent_tools_round2.json"

PROBES = [
    ("list_modes", {}),
    ("search_assets", {"query": "火星"}),
    ("list_asset_tags", {}),
    ("get_mode_schema", {"mode": "t2v"}),
    ("describe_mode_params", {"mode": "r2v"}),
    ("get_prompt_preset", {}),
    ("list_prompt_presets", {}),
    ("list_jobs", {}),
    ("recall_memory", {}),
    ("search_conversations", {"query": "视频"}),
    ("search_asset_history", {"query": "火星"}),
    ("enhance_prompt", {"prompt": "a girl walking in the rain", "mode": "t2v"}),
    ("describe_asset", {"asset_id": "asset_21f5d051d841"}),
    ("get_job_status", {"job_id": "bd344e7aa852"}),
    ("manage_media_library", {"scope": "failed"}),   # 只读统计口径
    ("media_probe", {"asset_id": "asset_61f1b6fd9a1d"}),
    ("web_search", {"query": "MiniMax H3"}),
]


def chat(tool, args, sid):
    msg = (f"请调用工具 {tool}（参数：{json.dumps(args, ensure_ascii=False)}）。"
           f"只调用这一个工具，拿到结果后用 respond 工具简短回复。")
    payload = {"message": msg, "session_id": sid}
    req = urllib.request.Request(
        f"{BASE}/api/agent/chat", data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read().decode())
        txt = json.dumps(d, ensure_ascii=False)
        return {"ok": True, "elapsed": round(time.time() - t0, 1),
                "status": d.get("status"),
                "answer_head": (d.get("answer") or "")[:400],
                "mentions_tool": tool in txt}
    except urllib.error.HTTPError as e:
        return {"ok": False, "elapsed": round(time.time() - t0, 1),
                "http": e.code, "body": e.read().decode()[:300]}
    except Exception as e:
        return {"ok": False, "elapsed": round(time.time() - t0, 1),
                "err": f"{type(e).__name__}: {e}"}


def main():
    results = []
    for i, (tool, args) in enumerate(PROBES):
        r = chat(tool, args, f"c-r2-full-{i}")
        r["tool"] = tool
        r["args"] = args
        results.append(r)
        print(f"[{tool}] ok={r.get('ok')} elapsed={r.get('elapsed')} "
              f"status={r.get('status')} mentions_tool={r.get('mentions_tool')} "
              f"http={r.get('http')} err={r.get('err','')[:60]}", flush=True)
        json.dump(results, open(OUT, "w"), ensure_ascii=False, indent=1)
    ok = sum(1 for r in results if r.get("ok"))
    called = sum(1 for r in results if r.get("mentions_tool"))
    print(f"summary total={len(results)} http_ok={ok} tool_referenced={called}")
    print("saved", OUT)


if __name__ == "__main__":
    sys.exit(main())
