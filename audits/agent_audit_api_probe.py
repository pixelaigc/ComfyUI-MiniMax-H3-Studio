#!/usr/bin/env python3
"""内置 Agent —— 接口级实测探针（线上 8190）。

逐个打 /api/agent/* 端点，把真实返回结构打出来，供审查报告取证。
只读 + 无副作用为主；会话删除只删自己刚建的。
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("H3_BASE", "http://127.0.0.1:8190")
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/agent_api_probe.json"


def req(method, path, body=None, timeout=120, raw=False):
    url = f"{BASE}{path}"
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            payload = resp.read().decode()
            code = resp.getcode()
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        payload = e.read().decode()
        code = e.code
        ctype = e.headers.get("Content-Type", "")
    except Exception as e:  # noqa: BLE001
        return {"http": None, "error": f"{type(e).__name__}: {e}"}
    if raw:
        return {"http": code, "ctype": ctype, "raw_head": payload[:2000],
                "raw_len": len(payload)}
    try:
        parsed = json.loads(payload)
    except Exception:
        return {"http": code, "ctype": ctype, "raw_head": payload[:600]}
    return {"http": code, "ctype": ctype, "json": parsed}


def brief(v, n=400):
    s = json.dumps(v, ensure_ascii=False)
    return s[:n]


def main():
    out = {}

    # 1) 工具清单
    r = req("GET", "/api/agent/tools")
    tools = (r.get("json") or {}).get("tools") or []
    out["tools"] = {
        "http": r.get("http"),
        "count": len(tools),
        "names": [t.get("name") for t in tools],
        "disabled": [t.get("name") for t in tools if not t.get("enabled")],
        "caps": sorted({t.get("capability") for t in tools if t.get("capability")}),
        "limits": (r.get("json") or {}).get("limits"),
        "events": (r.get("json") or {}).get("events"),
    }

    # 2) 配置读
    r = req("GET", "/api/agent/settings")
    j = r.get("json") or {}
    out["settings_get"] = {
        "http": r.get("http"),
        "keys": sorted(j.keys()),
        "capabilities": j.get("capabilities"),
        "gen_defaults_modes": sorted((j.get("gen_defaults") or {}).keys()),
        "gen_defaults_spec_modes": sorted((j.get("gen_defaults_spec") or {}).keys()),
        "limits": j.get("limits"),
        "dependencies": j.get("dependencies"),
        "t2v_options_keys": sorted((j.get("t2v_options") or {}).keys()),
    }

    # 3) 配置写（往返一致性 —— 写回同样的值再读）
    r = req("GET", "/api/agent/settings")
    cur = (r.get("json") or {}).get("settings") or {}
    r2 = req("PUT", "/api/agent/settings", {
        "max_steps": cur.get("max_steps"),
        "capabilities": cur.get("capabilities"),
        "search_engines": cur.get("search_engines"),
        "gen_defaults": cur.get("gen_defaults"),
        "file_roots": cur.get("file_roots"),
    })
    out["settings_put_roundtrip"] = {
        "http": r2.get("http"),
        "body": brief(r2.get("json") or r2, 600),
    }

    # 4) 搜索引擎自检
    out["test_engine_local9898"] = req(
        "POST", "/api/agent/settings/test-engine", {"engine_id": "local_9898"})
    out["test_engine_missing"] = req(
        "POST", "/api/agent/settings/test-engine", {"engine_id": "no_such_engine"})
    out["test_engine_ddg_direct"] = req(
        "POST", "/api/agent/settings/test-engine", {"engine_id": "ddg_direct"})

    # 5) 记忆
    out["memory_get"] = brief(req("GET", "/api/agent/memory?limit=3"), 500)
    out["memory_set"] = req("POST", "/api/agent/memory",
                            {"key": "audit_probe_key", "value": "v1",
                             "scope": "global"})
    out["memory_get_after"] = brief(req("GET", "/api/agent/memory?query=audit_probe"), 500)

    # 6) 会话 CRUD
    r = req("POST", "/api/agent/sessions", {})
    sid = (r.get("json") or {}).get("session_id")
    out["session_create"] = {"http": r.get("http"), "session_id": sid}
    out["session_get"] = brief(req("GET", f"/api/agent/sessions/{sid}"), 800)
    out["session_jobs"] = brief(req("GET", f"/api/agent/sessions/{sid}/jobs"), 500)
    out["session_list"] = brief(req("GET", "/api/agent/sessions?limit=3"), 800)

    # 7) 媒体状态（不存在）
    out["media_status_missing"] = req("GET", "/api/agent/media/nonexist/status")

    # 8) chat 契约：缺 message / 非法 body
    out["chat_no_message"] = req("POST", "/api/agent/chat", {})
    out["chat_bad_json"] = {"http": None, "note": "见 raw probe"}

    # 9) chat SSE（body 载体）
    r = req("POST", "/api/agent/chat",
            {"message": "只调用工具 list_modes，拿到结果后用 respond 简短回复。",
             "stream": 1}, timeout=900, raw=True)
    out["chat_sse_body_stream"] = {
        "http": r.get("http"), "ctype": r.get("ctype"),
        "events": [ln.split(":")[0].replace("event:", "").strip()
                   for ln in (r.get("raw_head") or "").splitlines()
                   if ln.startswith("event:")],
        "raw_head": (r.get("raw_head") or "")[:1200],
    }

    # 10) chat SSE（query 载体，旧前端兼容）
    r = req("POST", "/api/agent/chat?stream=1",
            {"message": "只调用工具 list_prompt_presets，拿到结果后用 respond 简短回复。"},
            timeout=900, raw=True)
    out["chat_sse_query_stream"] = {
        "http": r.get("http"), "ctype": r.get("ctype"),
        "events": [ln.split(":")[0].replace("event:", "").strip()
                   for ln in (r.get("raw_head") or "").splitlines()
                   if ln.startswith("event:")],
        "raw_head": (r.get("raw_head") or "")[:800],
    }

    # 11) 一次性 chat
    r = req("POST", "/api/agent/chat",
            {"message": "只调用工具 list_asset_tags，拿到结果后用 respond 简短回复。"},
            timeout=900)
    j = r.get("json") or {}
    out["chat_oneshot"] = {
        "http": r.get("http"),
        "keys": sorted(j.keys()) if isinstance(j, dict) else None,
        "status": j.get("status"),
        "steps": [{"tool": s.get("tool"), "ok": s.get("ok"), "ms": s.get("ms")}
                  for s in (j.get("steps") or [])],
        "answer_head": (j.get("answer") or "")[:200],
        "usage": j.get("usage"),
    }

    # 12) 删掉自己刚建的会话（清理）
    out["session_delete"] = req("DELETE", f"/api/agent/sessions/{sid}")
    out["session_get_after_delete"] = brief(
        req("GET", f"/api/agent/sessions/{sid}"), 300)

    # 13) 清理临时记忆
    out["memory_cleanup_probe"] = {"note": "audit_probe_key 保留（memory 无删除端点）"}

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: (v.get("http") if isinstance(v, dict) else None)
                      for k, v in out.items()}, ensure_ascii=False, indent=1))
    print("WROTE", OUT)


if __name__ == "__main__":
    main()
