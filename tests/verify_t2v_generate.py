# -*- coding: utf-8 -*-
"""真实跑一次文生视频（走 Studio 的 /api/jobs，multipart）。

用法: python3 tests/verify_t2v_generate.py [base_url] [--twopass] [--attention sol]
产物: /tmp/h3t2v-verify/generate-*.json + 视频文件路径
"""
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8193"
USE_TWOPASS = "--twopass" in sys.argv
ATTENTION = "default"
for i, a in enumerate(sys.argv):
    if a == "--attention" and i + 1 < len(sys.argv):
        ATTENTION = sys.argv[i + 1]

OUT = Path("/tmp/h3t2v-verify")
OUT.mkdir(parents=True, exist_ok=True)

PROMPT = (
    "integrated_multimodal_description: [Shot 1] Live-action, cinematic, a medium "
    "shot frames a red panda walking slowly from left to right along a mossy bamboo "
    "forest floor. The camera tracks the panda's movement with small amplitude at "
    "normal speed, keeping the panda near the centre of frame while tall green "
    "bamboo stalks slide past in the background. Soft overcast daylight filters "
    "through the canopy, dappling the panda's fur.\n\n"
    "overall_soundscape: Light wind moves through dense bamboo leaves overhead "
    "while the panda's soft pawfalls tap on dry leaves and small twigs.\n\n"
    "non_diegetic_music: A sparse plucked string phrase at a slow tempo over a low "
    "sustained drone."
)

PARAMS = {
    "prompt": PROMPT,
    "model_key": "fl2va_pruned_int8",
    "width": 864,
    "height": 480,
    "aspect": "16:9",
    "duration": 5,
    "steps": 4,
    "seed": 20261110,
    "fixed_accel_lora": "ckpt850",
    "accel_loras": [],
    "attention": ATTENTION,
    "twopass": {"enabled": USE_TWOPASS},
}


def _multipart(fields):
    boundary = uuid.uuid4().hex
    chunks = []
    for name, value in fields:
        chunks.append(("--%s\r\n" % name).encode())
        chunks.append(
            ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode())
        chunks.append(value.encode() if isinstance(value, str) else value)
        chunks.append(b"\r\n")
    chunks.append(("--%s--\r\n" % boundary).encode())
    return b"".join(chunks), boundary


def main():
    # 用 requests 发 multipart —— 手搓的 boundary 很容易写错
    # （必须每个 part 前有 --boundary、结尾也要 --boundary--）。
    import requests
    resp = requests.post(
        BASE + "/api/jobs",
        data={"mode": "t2v"},
        files={"params": ("params.json", json.dumps(PARAMS, ensure_ascii=False),
                          "application/json")},
        timeout=60)
    if resp.status_code != 200:
        print("SUBMIT FAILED", resp.status_code, resp.text[:800])
        return 1
    body = resp.json()
    job_id = body["id"]
    print("submitted:", job_id, "attention=", ATTENTION, "twopass=", USE_TWOPASS)

    deadline = time.time() + 1800
    last = ""
    while time.time() < deadline:
        try:
            job = requests.get(BASE + "/api/jobs/" + job_id, timeout=20).json()
        except Exception as exc:
            print("poll error:", exc)
            time.sleep(3)
            continue
        status = job.get("status")
        line = "%s %s %s" % (status, job.get("progress_text") or "",
                             job.get("error") or "")
        if line != last:
            print("  ", line)
            last = line
        if status in ("done", "failed", "canceled"):
            out = {
                "job_id": job_id,
                "status": status,
                "params": job.get("params"),
                "outputs": job.get("outputs"),
                "error": job.get("error"),
                "elapsed": job.get("elapsed"),
            }
            path = OUT / f"generate-{job_id}.json"
            path.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            print("RESULT", json.dumps(out, ensure_ascii=False, indent=2)[:2000])
            return 0 if status == "done" else 2
        time.sleep(4)
    print("TIMEOUT")
    return 3


if __name__ == "__main__":
    sys.exit(main())
