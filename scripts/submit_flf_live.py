# -*- coding: utf-8 -*-
"""线上真实 flf（首尾帧）短任务生成 —— multipart 提交 + 轮询 + ffprobe。

【为什么必须 multipart】
``POST /api/jobs`` 只接受 ``multipart/form-data``（并行任务
``bca5f57 fix(api): /api/jobs 校验 Content-Type`` 引入的显式校验），
发 ``application/json`` 会得到 HTTP 415。

【为什么提交时不传尺寸 / LoRA / 时长】
让它们**全部来自管理页默认参数**（也就是本轮修的那份）——
落库的 params 于是能直接与面板显示对拍。

用法：python3 submit_flf_live.py <base_url> <out_json>
"""
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/flf_live.json"

FIRST = "asset_ef9b20ebf8db"      # e2e-sample-3561685-52885.png
LAST = "asset_53c1e8f3cc6d"       # i-zimage-000221.png

PROMPT = "首尾帧验证：一朵花在镜头前缓慢开放，暖色调"


def _get(path, timeout=60):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw or "{}")
        except Exception:
            return exc.code, {"raw": raw[:2000]}
    except Exception as exc:
        return 0, {"error": "%s: %s" % (type(exc).__name__, exc)}


def submit_multipart(fields):
    """POST multipart/form-data（不接受 application/json）。"""
    boundary = "----h3flf" + uuid.uuid4().hex
    chunks = []
    for key, value in fields.items():
        chunks.append((
            "--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
            % (boundary, key, value)).encode("utf-8"))
    body = b"".join(chunks) + ("--%s--\r\n" % boundary).encode()
    req = urllib.request.Request(
        BASE + "/api/jobs", data=body, method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw or "{}")
        except Exception:
            return exc.code, {"raw": raw[:2000]}
    except Exception as exc:
        return 0, {"error": "%s: %s" % (type(exc).__name__, exc)}


def main():
    out = {"base": BASE, "first_frame": FIRST, "last_frame": LAST,
           "prompt": PROMPT, "submitted_without_size_lora_duration": True}

    # 1) 管理页默认参数（改后面板的真实下发值）
    st, settings = _get("/api/agent/settings")
    gd = (settings.get("gen_defaults") or {}).get("flf") or {}
    spec = ((settings.get("gen_defaults_spec") or {}).get("flf") or {})
    out["gen_defaults_flf"] = gd
    out["spec_fields"] = [
        {"name": f.get("name"), "type": f.get("type"),
         "default": f.get("default"),
         "options": [o.get("value") for o in (f.get("options") or [])]}
        for f in (spec.get("fields") or [])
    ]

    # 2) 提交 —— **不传**尺寸 / LoRA / 时长
    #
    # 【multipart 契约（server.py api_create）】
    #   mode        -> 普通文本 part
    #   params      -> **JSON 字符串** part（不是平铺的字段！）
    #   asset_refs  -> JSON 字符串 part
    # 把 prompt 平铺成顶层 part 会让 params={} -> 「提示词不能为空」。
    status, res = submit_multipart({
        "mode": "flf",
        "params": json.dumps({"prompt": PROMPT}, ensure_ascii=False),
        "asset_refs": json.dumps([
            {"asset_id": FIRST, "slot": "first_frame", "ordinal": 0},
            {"asset_id": LAST, "slot": "last_frame", "ordinal": 0},
        ]),
    })
    out["submit_status"] = status
    out["submit_response"] = res
    job_id = res.get("id") if isinstance(res, dict) else None
    out["job_id"] = job_id
    print("submit -> HTTP %s job_id=%s" % (status, job_id))
    if not job_id:
        with open(OUT, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
        print(json.dumps(out, ensure_ascii=False, indent=2)[:1500])
        return

    # 3) 轮询
    deadline = time.time() + 1800
    final = None
    while time.time() < deadline:
        time.sleep(8)
        st, jobs = _get("/api/jobs?mode=flf&limit=8", timeout=90)
        items = jobs.get("items") or jobs.get("jobs") or []
        hit = next((j for j in items if j.get("id") == job_id), None)
        if hit:
            final = hit
            state = str(hit.get("status") or "")
            print("  status=%s" % state)
            if state in ("done", "success", "failed", "error", "cancelled"):
                break
        else:
            print("  ... 暂未在列表里")
    out["final_job"] = final
    out["final_status"] = (final or {}).get("status")
    out["final_params"] = (final or {}).get("params")
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print("final status:", out["final_status"])
    print("final params:", json.dumps(out["final_params"], ensure_ascii=False))


if __name__ == "__main__":
    main()
