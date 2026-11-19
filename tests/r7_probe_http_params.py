#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7：畸形 continuation 走真实 HTTP 提交（multipart），看是 400 还是 500。

api_create 只收 multipart，所以必须用 multipart 打。
"""
import json
import urllib.request
import urllib.error
import uuid

BASE = "http://127.0.0.1:8190"


def submit(params: dict):
    boundary = uuid.uuid4().hex
    body = b""
    parts = [("mode", "t2v"), ("params", json.dumps(params, ensure_ascii=False))]
    for name, value in parts:
        body += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                 f"{value}\r\n").encode("utf-8")
    body += f"--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        BASE + "/api/jobs", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, r.read()[:300].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300].decode("utf-8", "replace")
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


BASE_P = {
    "prompt": "p", "model_key": "fl2va_pruned_int8",
    "width": 1088, "height": 1920, "duration": 5,
    "steps": 8, "sampler": "euler", "scheduler": "beta",
    "accel_lora": "dmad_4step_critic",
}

cases = [
    ("正常（无 continuation）", None),
    ("continuation 里 context_frames='abc'",
     {"source_job": "no_such_job", "context_frames": "abc"}),
    ("continuation 里 anchor_frames='xyz'",
     {"source_job": "no_such_job", "anchor_frames": "xyz"}),
    ("continuation 里 context_frames=[1,2]",
     {"source_job": "no_such_job", "context_frames": [1, 2]}),
    ("duration='abc'", "__duration_bad__"),
    ("accel_lora 是未知 key", "__unknown_accel__"),
]

for label, cont in cases:
    params = dict(BASE_P)
    if cont == "__duration_bad__":
        params["duration"] = "abc"
    elif cont == "__unknown_accel__":
        params["accel_lora"] = "not_a_real_lora_key"
    elif cont is not None:
        params["continuation"] = cont
    code, text = submit(params)
    print(f"{label:42s} -> HTTP {code}  {text[:120]}")
