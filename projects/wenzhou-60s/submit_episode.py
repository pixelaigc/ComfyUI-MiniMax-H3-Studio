# -*- coding: utf-8 -*-
"""提交《山海温州》6 段镜头到 H3 Studio 队列"""
import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
API = "http://127.0.0.1:8190/api/jobs"

prompts = json.loads((BASE / "prompts.json").read_text(encoding="utf-8"))


def submit(prompt: str) -> dict:
    boundary = "----h3studio" + str(int(time.time() * 1000))
    params = {
        "prompt": prompt, "resolution": "544p", "aspect": "16:9", "duration": 10,
        "model": "lora", "lora": "v4step600", "lora_strength": 1.0,
        "steps": 6, "precision": "nvfp4", "seed": None,
    }
    parts = []
    for name, val in (("mode", "t2v"), ("params", json.dumps(params, ensure_ascii=False))):
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n".encode("utf-8"))
    body = b"".join(parts) + f"--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(API, data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


ids = []
for i, item in enumerate(prompts, 1):
    res = submit(item["prompt"])
    print(f"[{i}/6] {item['title']}: {res}")
    ids.append({"index": i, "title": item["title"], "job_id": res.get("id")})

(BASE / "submissions.json").write_text(json.dumps(ids, ensure_ascii=False, indent=2), encoding="utf-8")
print("已全部入队:", len(ids))
