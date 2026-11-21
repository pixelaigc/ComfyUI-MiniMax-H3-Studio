# -*- coding: utf-8 -*-
"""flf 真实产物的 ffprobe 证据（时长 / 分辨率 / 音轨）。

用法：python3 probe_flf_output.py <job_id> <base_url> <out_dir>
"""
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

JOB = sys.argv[1] if len(sys.argv) > 1 else "f6441e31decc"
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8190"
OUT = Path(sys.argv[3] if len(sys.argv) > 3 else "/tmp/flf_out")
OUT.mkdir(parents=True, exist_ok=True)

CANDIDATE_ROOTS = [
    "/home/ubuntu/comfyui/output",
    "/root/projects/h3studio-live/data/output",
    "/root/projects/h3studio-live/output",
]


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=90) as r:
        return json.loads(r.read().decode() or "{}")


def find_output(job):
    """从 job 记录里找产物文件名，再在本机输出目录里定位。"""
    hits = []
    for key in ("output", "result", "assets", "files"):
        value = job.get(key)
        if isinstance(value, str):
            hits.append(value)
        elif isinstance(value, list):
            hits.extend(str(v) for v in value)
        elif isinstance(value, dict):
            hits.extend(str(v) for v in value.values())
    found = []
    for token in hits:
        name = token.split("/")[-1].strip().strip("'\"")
        if not name or "." not in name:
            continue
        for root in CANDIDATE_ROOTS:
            p = Path(root) / name
            if p.exists():
                found.append(p)
            # 也可能在子目录里
            rp = Path(root)
            if rp.is_dir():
                for sub in rp.rglob(name):
                    found.append(sub)
                    break
    # 去重
    uniq = []
    for p in found:
        if p not in uniq:
            uniq.append(p)
    return uniq


def main():
    data = get("/api/jobs?mode=flf&limit=20")
    items = data.get("items") or data.get("jobs") or []
    job = next((j for j in items if j.get("id") == JOB), None)
    result = {"job_id": JOB, "job_found": job is not None}
    if job is None:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    result["status"] = job.get("status")
    result["params"] = job.get("params")
    paths = find_output(job)
    result["candidates"] = [str(p) for p in paths]
    probes = []
    for path in paths:
        if path.stat().st_size == 0:
            probes.append({"file": str(path), "error": "0 字节"})
            continue
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_format", "-show_streams",
                 "-of", "json", str(path)],
                capture_output=True, text=True, timeout=60)
            data_json = json.loads(out.stdout or "{}")
        except Exception as exc:
            probes.append({"file": str(path), "error": str(exc)})
            continue
        streams = []
        for st in data_json.get("streams", []):
            streams.append({
                "codec_type": st.get("codec_type"),
                "codec_name": st.get("codec_name"),
                "width": st.get("width"),
                "height": st.get("height"),
                "duration": st.get("duration"),
                "r_frame_rate": st.get("r_frame_rate"),
                "channels": st.get("channels"),
                "sample_rate": st.get("sample_rate"),
            })
        fmt = data_json.get("format", {})
        probes.append({
            "file": str(path),
            "size_bytes": path.stat().st_size,
            "format_name": fmt.get("format_name"),
            "duration": fmt.get("duration"),
            "nb_streams": fmt.get("nb_streams"),
            "streams": streams,
            "has_video": any(s.get("codec_type") == "video" for s in streams),
            "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        })
    result["probes"] = probes
    text = json.dumps(result, ensure_ascii=False, indent=2)
    (OUT / "ffprobe_flf.json").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
