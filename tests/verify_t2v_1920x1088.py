# -*- coding: utf-8 -*-
"""真实跑一次 minimax-h3 文生视频 @ 1920x1088（走 Studio 的 /api/jobs，multipart）。

用途：验证「分辨率上限放宽到 2.0M 像素」不只是面板上的数字 ——
     工作流能不能真的用 1920x1088 出片。

用法: python3 tests/verify_t2v_1920x1088.py [base_url]
产物: /tmp/h3t2v-1920x1088/generate-*.json + 视频文件路径

【与 verify_t2v_generate.py 的区别】那个脚本固定 864x480；
这个固定 1920x1088（= 2.0 MP 那一档），并把实际分辨率回显出来核对。
"""
import json
import sys
import time
import uuid
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8231"
OUT = Path("/tmp/h3t2v-1920x1088")
OUT.mkdir(parents=True, exist_ok=True)

WIDTH, HEIGHT = 1920, 1088

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
    "width": WIDTH,
    "height": HEIGHT,
    "aspect": "16:9",
    "duration": 5,
    "steps": 4,
    "seed": 20261111,
    "fixed_accel_lora": "ckpt850",
    "accel_loras": [],
    "attention": "default",
    "twopass": {"enabled": False},
}


def main():
    import requests
    resp = requests.post(
        BASE + "/api/jobs",
        data={"mode": "t2v"},
        files={"params": ("params.json", json.dumps(PARAMS, ensure_ascii=False),
                          "application/json")},
        timeout=60)
    if resp.status_code != 200:
        print("SUBMIT FAILED", resp.status_code, resp.text[:1200])
        return 1
    body = resp.json()
    job_id = body["id"]
    print("submitted:", job_id, "size:", WIDTH, "x", HEIGHT)

    deadline = time.time() + 2400
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
                "requested": {"width": WIDTH, "height": HEIGHT,
                              "pixels": WIDTH * HEIGHT},
                "params": job.get("params"),
                "outputs": job.get("outputs"),
                "error": job.get("error"),
                "elapsed": job.get("elapsed"),
            }
            path = OUT / f"generate-{job_id}.json"
            path.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            print("SAVED", path)
            print("RESULT", json.dumps(out, ensure_ascii=False, indent=2)[:2500])
            return 0 if status == "done" else 2
        time.sleep(5)
    print("TIMEOUT")
    return 3


if __name__ == "__main__":
    sys.exit(main())
