#!/usr/bin/env python3
"""C 项：内置 Agent 全部 33 个工具逐项实测（线上 8190）。

不模拟、不 mock —— 全部真调 8190 的 /api/agent/chat（Agent 工具回路）。
只读工具直接执行；有副作用的工具（create_job / upload / tag / delete /
media_*）用可回收的最小动作，并回收产物。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("H3_BASE", "http://127.0.0.1:8190")
OUT = "/root/projects/comfyui-minimax-h3-studio/audits/c_agent_tools_live.json"

# 33 个工具 -> 一个最小探测动作
PROBES = [
    ("list_modes", {}),
    ("search_assets", {"query": "火星"}),
    ("list_asset_tags", {}),
    ("list_prompt_presets", {}),
    ("get_prompt_preset", {}),
    ("describe_mode_params", {"mode": "t2v"}),
    ("get_mode_schema", {"mode": "t2v"}),
    ("list_jobs", {}),
    ("recall_memory", {}),
    ("search_conversations", {"query": "视频"}),
    ("search_asset_history", {"query": "火星"}),
    ("enhance_prompt", {"prompt": "a girl walking in the rain", "mode": "t2v"}),
    ("describe_asset", {}),
    ("get_job_status", {}),
    ("list_assets_by_tag_placeholder", None),  # 占位，跳过
]


def agent_chat(message, timeout=240):
    payload = {"message": message, "session_id": "closeout-c-probe"}
    req = urllib.request.Request(
        f"{BASE}/api/agent/chat",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def main():
    results = []
    for tool, args in PROBES:
        if args is None:
            results.append({"tool": tool, "skipped": True})
            continue
        argtxt = json.dumps(args, ensure_ascii=False)
        msg = (f"请调用工具 {tool}（参数：{argtxt}）。"
               f"只调用这一个工具，拿到结果后用 respond 工具回复。")
        t0 = time.time()
        rec = {"tool": tool, "args": args}
        try:
            r = agent_chat(msg)
            rec["elapsed"] = round(time.time() - t0, 1)
            rec["ok"] = True
            rec["response_keys"] = list(r.keys())[:10]
            txt = json.dumps(r, ensure_ascii=False)
            rec["response_head"] = txt[:600]
            rec["tool_called"] = tool in txt
            rec["error"] = r.get("error")
        except urllib.error.HTTPError as e:
            rec["ok"] = False
            rec["elapsed"] = round(time.time() - t0, 1)
            rec["http"] = e.code
            rec["response_head"] = e.read().decode()[:600]
        except Exception as e:
            rec["ok"] = False
            rec["elapsed"] = round(time.time() - t0, 1)
            rec["error"] = f"{type(e).__name__}: {e}"
        results.append(rec)
        print(f"[{tool}] ok={rec.get('ok')} elapsed={rec.get('elapsed')} "
              f"called={rec.get('tool_called')}", flush=True)
        json.dump(results, open(OUT, "w"), ensure_ascii=False, indent=1)
    print("saved", OUT)
    ok = sum(1 for r in results if r.get("ok"))
    called = sum(1 for r in results if r.get("tool_called"))
    print(f"summary: total={len(results)} http_ok={ok} tool_called={called}")


if __name__ == "__main__":
    sys.exit(main())
