#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看门狗第 8 轮 —— api_create Content-Type 校验的**独立**复验脚本。

【为什么要单独写一个脚本，而不是直接看部署脚本的输出】
部署脚本里的 `curl -w` 输出要和 body 拼在一起看，容易读错；
而且部署脚本自带「自己对自己」的验证逻辑。本脚本**不复用**部署脚本，
用 http.client 直连、逐个 case 断言状态码与错误码 —— 独立第二来源。

用法：
    python3 scripts/verify_apicreate_ct.py --base http://127.0.0.1:8190 \
        --out /root/evidence/watchdog8-20261121/fix_verify_after_deploy.json
"""
from __future__ import annotations

import argparse
import http.client
import json
import sys
from urllib.parse import urlparse

CASES = [
    # (名称, 请求头 Content-Type, body, 期望状态码, 期望错误码)
    ("no_content_type", None, b"mode=t2v", 415, "content_type_required"),
    ("application_json", "application/json", b'{"mode":"t2v"}', 415,
     "unsupported_media_type"),
    ("text_plain", "text/plain", b"mode=t2v", 415,
     "unsupported_media_type"),
    ("form_urlencoded", "application/x-www-form-urlencoded", b"mode=t2v",
     415, "unsupported_media_type"),
    ("multipart_no_boundary", "multipart/form-data", b"mode=t2v", 400,
     "content_type_invalid"),
]

VALID_BODY = (
    b"--BOUND\r\n"
    b"Content-Disposition: form-data; name=\"mode\"\r\n\r\n"
    b"t2v\r\n"
    b"--BOUND\r\n"
    b"Content-Disposition: form-data; name=\"params\"\r\n\r\n"
    b"{}\r\n"
    b"--BOUND--\r\n"
)


def post(base: str, ctype: str | None, body: bytes, boundary: bool = False):
    u = urlparse(base)
    conn = http.client.HTTPConnection(u.hostname or "127.0.0.1",
                                       u.port or 8190, timeout=30)
    headers = {}
    if ctype is not None:
        headers["Content-Type"] = ctype
    if boundary:
        headers["Content-Type"] = "multipart/form-data; boundary=BOUND"
    conn.request("POST", "/api/jobs", body=body, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    status = resp.status
    conn.close()
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:
        parsed = {"raw": raw.decode("utf-8", "replace")[:300]}
    return status, parsed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    out = {"base": args.base, "cases": {}, "ok": True}

    for name, ctype, body, want_status, want_code in CASES:
        status, parsed = post(args.base, ctype, body)
        ok = (status == want_status
              and isinstance(parsed, dict)
              and parsed.get("code") == want_code
              and bool((parsed.get("error") or "").strip()))
        out["cases"][name] = {
            "sent_content_type": ctype,
            "http_status": status,
            "expected_status": want_status,
            "expected_code": want_code,
            "body": parsed,
            "pass": ok,
        }
        if not ok:
            out["ok"] = False
        print(f"[{'PASS' if ok else 'FAIL'}] {name:24s} status={status} "
              f"(want {want_status}) code={parsed.get('code')!r} "
              f"(want {want_code!r})")
        print(f"        error={parsed.get('error')!r}")

    # 正常 multipart：必须**穿过**内容类型校验，交给业务校验
    status, parsed = post(args.base, None, VALID_BODY, boundary=True)
    valid_ok = (status != 415
                and isinstance(parsed, dict)
                and parsed.get("code") != "unsupported_media_type"
                and parsed.get("code") != "content_type_required"
                and parsed.get("code") != "content_type_invalid")
    out["cases"]["valid_multipart"] = {
        "sent_content_type": "multipart/form-data; boundary=BOUND",
        "http_status": status,
        "body": parsed,
        "pass": valid_ok,
        "note": "正常 multipart 必须穿过内容类型校验，"
                "由业务校验接手（这里 mode=t2v 无宽高 -> 业务 400）",
    }
    if not valid_ok:
        out["ok"] = False
    print(f"[{'PASS' if valid_ok else 'FAIL'}] {'valid_multipart':24s} "
          f"status={status} body={json.dumps(parsed, ensure_ascii=False)[:120]}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print("\n输出:", args.out)

    print("\n=== 结论：", "全部符合预期" if out["ok"] else "存在不符合预期的 case", "===")
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
