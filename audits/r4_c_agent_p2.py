#!/usr/bin/env python3
"""C 项第二轮：补跑上轮被并发闸门（429）挡下的工具。
每次只跑一个，跑完等闸门释放（轮询 /api/agent/tools 之前的 chat 返回 200）。
"""
import json, os, time, urllib.error, urllib.request
BASE = os.environ.get("H3_BASE", "http://127.0.0.1:8190")
OUT = "/root/projects/comfyui-minimax-h3-studio/audits/r4_c_agent_p2.json"
PROBES = [
    ("list_prompt_presets", {}), ("list_asset_tags", {}),
    ("enhance_prompt", {"prompt": "a girl in rain", "mode": "t2v"}),
    ("search_conversations", {"query": "视频"}), ("recall_memory", {}),
    ("describe_mode_params", {"mode": "flf"}),
    ("manage_media_library", {"scope": "failed"}),
    ("get_prompt_preset", {}), ("web_search", {"query": "MiniMax H3"}),
    ("search_asset_history", {"query": "火星"}),
]
def chat(tool, args, sid, timeout=240):
    msg = f"请调用工具 {tool}（参数：{json.dumps(args, ensure_ascii=False)}），只调这一个工具，然后用 respond 简短回复。"
    req = urllib.request.Request(f"{BASE}/api/agent/chat",
        data=json.dumps({"message": msg, "session_id": sid}, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        txt = json.dumps(d, ensure_ascii=False)
        return {"ok": True, "elapsed": round(time.time()-t0, 1), "status": d.get("status"),
                "mentions": tool in txt, "answer": (d.get("answer") or "")[:200]}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "elapsed": round(time.time()-t0, 1),
                "body": e.read().decode()[:160]}
    except Exception as e:
        return {"ok": False, "elapsed": round(time.time()-t0, 1), "err": f"{type(e).__name__}: {e}"}
res = []
for i, (t, a) in enumerate(PROBES):
    for attempt in range(4):
        r = chat(t, a, f"r4-c2-{i}-{attempt}")
        if r.get("ok") or r.get("http") != 429:
            break
        time.sleep(20)   # 等并发闸门释放
    r["tool"] = t; r["attempt"] = attempt + 1; res.append(r)
    print(f"[{t}] ok={r.get('ok')} attempt={attempt+1} elapsed={r.get('elapsed')} mentions={r.get('mentions')} http={r.get('http')}", flush=True)
    json.dump(res, open(OUT, "w"), ensure_ascii=False, indent=1)
    time.sleep(3)
print("summary total=", len(res), "ok=", sum(1 for x in res if x.get("ok")),
      "mentions=", sum(1 for x in res if x.get("mentions")))
