#!/usr/bin/env python3
"""内置 Agent「agent 管理」配置 —— 真实往返实测（线上 8190）。

模拟前端 agent-manage.js 的 collect() 提交体，验证：
  1) max_steps 往返一致
  2) total_timeout 往返一致（且越界被夹回）
  3) capabilities 四个开关往返一致
  4) search_engines 往返一致
  5) gen_defaults（按生成类型）**逐模式逐字段**往返一致 —— 这是最容易漂的一处
  6) 失败用例：越界 max_steps / 非法 URL 的引擎被归一

只改「可安全回滚」的字段，并在结束时把原值写回。
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("H3_BASE", "http://127.0.0.1:8190")


def req(method, path, body=None, timeout=120):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(f"{BASE}{path}", data=data,
                               headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.getcode(), json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:400]}


def main():
    out = {}
    code, snap = req("GET", "/api/agent/settings")
    orig = snap["settings"]
    out["original_snapshot_keys"] = sorted(orig.keys())

    # ---------- 1) 正常往返：改一个 gen_defaults 字段 ----------
    gd = json.loads(json.dumps(orig.get("gen_defaults") or {}))
    changed_mode = "t2v"
    changed_field = "steps"
    before_val = (gd.get(changed_mode) or {}).get(changed_field)
    new_val = 7 if before_val != 7 else 6
    gd.setdefault(changed_mode, {})[changed_field] = new_val

    payload = {
        "max_steps": orig.get("max_steps"),
        "total_timeout": orig.get("total_timeout"),
        "capabilities": orig.get("capabilities"),
        "search_engines": orig.get("search_engines"),
        "gen_defaults": gd,
        "t2v_defaults": orig.get("t2v_defaults"),
    }
    code, put = req("PUT", "/api/agent/settings", payload)
    out["put"] = {"http": code, "ok": put.get("ok")}
    code, snap2 = req("GET", "/api/agent/settings")
    got = (snap2["settings"].get("gen_defaults") or {}).get(changed_mode, {})
    out["gen_defaults_roundtrip"] = {
        "mode": changed_mode, "field": changed_field,
        "sent": new_val, "got": got.get(changed_field),
        "match": got.get(changed_field) == new_val,
    }

    # ---------- 2) 逐模式逐字段对照（提交值 vs 落库值） ----------
    diffs = []
    for mode, fields in (snap2["settings"].get("gen_defaults") or {}).items():
        sent = gd.get(mode) or {}
        for k, v in sent.items():
            if k not in fields:
                continue
            if fields[k] != v:
                diffs.append({"mode": mode, "field": k,
                               "sent": v, "stored": fields[k]})
    out["gen_defaults_field_diffs"] = diffs

    # ---------- 3) max_steps 越界被夹 ----------
    code, _ = req("PUT", "/api/agent/settings",
                  {**payload, "max_steps": 9999})
    code, snap3 = req("GET", "/api/agent/settings")
    out["max_steps_clamped"] = {
        "sent": 9999,
        "stored": snap3["settings"].get("max_steps"),
        "hard_cap": snap3.get("limits", {}).get("max_steps_cap"),
    }
    code, _ = req("PUT", "/api/agent/settings",
                  {**payload, "max_steps": 0})
    code, snap4 = req("GET", "/api/agent/settings")
    out["max_steps_min_clamped"] = {
        "sent": 0, "stored": snap4["settings"].get("max_steps")}

    # ---------- 4) 非法引擎被归一（无 url 的引擎丢弃） ----------
    bad_engines = [{"id": "no_url_engine", "name": "没有 URL", "type": "custom",
                    "method": "POST", "enabled": True},
                   {"id": "dup", "name": "A", "type": "custom",
                    "url": "http://127.0.0.1:9898/x", "method": "POST",
                    "enabled": True},
                   {"id": "dup", "name": "B", "type": "custom",
                    "url": "http://127.0.0.1:9898/y", "method": "POST",
                    "enabled": True}]
    code, put2 = req("PUT", "/api/agent/settings",
                     {**payload, "search_engines": bad_engines})
    code, snap5 = req("GET", "/api/agent/settings")
    out["engine_normalize"] = {
        "sent": len(bad_engines),
        "stored_ids": [e.get("id") for e in
                       (snap5["settings"].get("search_engines") or [])],
    }

    # ---------- 5) 恢复原值 ----------
    code, _ = req("PUT", "/api/agent/settings", {
        "max_steps": orig.get("max_steps"),
        "total_timeout": orig.get("total_timeout"),
        "capabilities": orig.get("capabilities"),
        "search_engines": orig.get("search_engines"),
        "gen_defaults": orig.get("gen_defaults"),
        "t2v_defaults": orig.get("t2v_defaults"),
    })
    code, snap6 = req("GET", "/api/agent/settings")
    out["restored"] = {
        "max_steps": snap6["settings"].get("max_steps"),
        "gen_defaults_t2v_steps": (
            (snap6["settings"].get("gen_defaults") or {})
            .get("t2v", {}).get("steps")),
        "engines": [e.get("id") for e in
                    (snap6["settings"].get("search_engines") or [])],
    }

    # ---------- 6) gen_defaults_spec 与 spec_payload 一致 ----------
    out["spec_payload_keys"] = sorted(
        (snap6.get("gen_defaults_spec") or {}).keys())
    out["spec_modes_in_settings"] = sorted(
        (snap6["settings"].get("gen_defaults") or {}).keys())

    print(json.dumps(out, ensure_ascii=False, indent=1))
    with open(sys.argv[1] if len(sys.argv) > 1
              else "/tmp/agent_settings_roundtrip.json", "w",
              encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
