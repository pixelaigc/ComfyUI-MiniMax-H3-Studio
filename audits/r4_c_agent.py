#!/usr/bin/env python3
"""C 项：内置 Agent 工具的对话级实测（线上 8190 真调 /api/agent/chat）。"""
import json, os, sys, time, urllib.error, urllib.request
BASE = os.environ.get("H3_BASE", "http://127.0.0.1:8190")
OUT = "/root/projects/comfyui-minimax-h3-studio/audits/r4_c_agent.json"
PROBES = [
    ("list_modes", {}), ("search_assets", {"query": "火星"}),
    ("get_mode_schema", {"mode": "r2v"}), ("list_jobs", {}),
    ("describe_asset", {"asset_id": "asset_21f5d051d841"}),
    ("get_job_status", {"job_id": "b48e9a06a50d"}),
    ("media_probe", {"asset_id": "asset_61f1b6fd9a1d"}),
    ("list_prompt_presets", {}), ("list_asset_tags", {}),
    ("enhance_prompt", {"prompt": "a girl in rain", "mode": "t2v"}),
    ("search_conversations", {"query": "视频"}), ("recall_memory", {}),
    ("describe_mode_params", {"mode": "flf"}),
    ("manage_media_library", {"scope": "failed"}),
    ("get_prompt_preset", {}), ("web_search", {"query": "MiniMax H3"}),
    ("search_asset_history", {"query": "火星"}),
]
def chat(tool, args, sid):
    msg = f"请调用工具 {tool}（参数：{json.dumps(args, ensure_ascii=False)}），只调这一个工具，然后用 respond 简短回复。"
    req = urllib.request.Request(f"{BASE}/api/agent/chat",
        data=json.dumps({"message": msg, "session_id": sid}, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read().decode())
        txt = json.dumps(d, ensure_ascii=False)
        return {"ok": True, "elapsed": round(time.time()-t0, 1), "status": d.get("status"),
                "mentions": tool in txt, "answer": (d.get("answer") or "")[:200]}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "body": e.read().decode()[:200]}
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}: {e}"}
res = []
for i, (t, a) in enumerate(PROBES):
    r = chat(t, a, f"r4-c-{i}"); r["tool"] = t; res.append(r)
    print(f"[{t}] ok={r.get('ok')} elapsed={r.get('elapsed')} status={r.get('status')} mentions={r.get('mentions')}", flush=True)
    json.dump(res, open(OUT, "w"), ensure_ascii=False, indent=1)
print("summary total=", len(res), "ok=", sum(1 for x in res if x.get("ok")),
      "mentions=", sum(1 for x in res if x.get("mentions")))
