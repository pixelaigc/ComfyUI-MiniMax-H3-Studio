#!/usr/bin/env python
"""对本轮 flf 接续链的成片做 ffprobe 实测取证（帧数/时长/音轨）。

成片 = job2 的 is_full 输出（拼接后整条）。
用法： ffprobe_evidence.py <seam_repeat.json> <out.json>
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

DATA_OUT = Path("/root/projects/comfyui-minimax-h3-studio/data/outputs")
JOBS_DB = "/root/projects/comfyui-minimax-h3-studio/data/jobs.db"


def full_output_for(job_id: str) -> str | None:
    """成片 = job 的 is_full 输出（拼接后整条）。"""
    import sqlite3
    conn = sqlite3.connect(f"file:{JOBS_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT outputs FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
        if not row:
            return None
        for o in json.loads(row["outputs"] or "[]"):
            if o.get("is_full"):
                return o.get("file")
        return None
    finally:
        conn.close()


def probe(path: Path) -> dict:
    if not path.exists():
        return {"error": "文件不存在", "path": str(path)}
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        return {"error": out.stderr.strip()[:400], "path": str(path)}
    data = json.loads(out.stdout)
    vs = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
    as_ = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    v = vs[0] if vs else {}
    fmt = data.get("format", {})
    info = {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "nb_frames": v.get("nb_frames"),
        "avg_frame_rate": v.get("avg_frame_rate"),
        "width": v.get("width"), "height": v.get("height"),
        "duration_s": float(fmt["duration"]) if fmt.get("duration") else None,
        "video_duration_s": float(v["duration"]) if v.get("duration") else None,
        "has_audio": bool(as_),
    }
    if as_:
        a = as_[0]
        info["audio_codec"] = a.get("codec_name")
        info["audio_sample_rate"] = a.get("sample_rate")
        info["audio_channels"] = a.get("channels")
        info["audio_duration_s"] = float(a["duration"]) if a.get("duration") else None
    if info.get("nb_frames") and info.get("duration_s"):
        info["fps_computed"] = round(
            int(info["nb_frames"]) / float(info["duration_s"]), 4)
    return info


def main(tj: str, out: str) -> int:
    data = json.loads(Path(tj).read_text(encoding="utf-8"))
    rows = []
    for ch in data.get("chains") or []:
        if ch.get("job2_status") != "done":
            continue
        segs = (ch.get("seam") or {}).get("segments") or []
        full = (ch.get("seam") or {}).get("full") or full_output_for(ch.get("job2"))
        row = {
            "tag": ch.get("tag"), "seed": ch.get("seed"),
            "job1": ch.get("job1"), "job2": ch.get("job2"),
            "full_rel": full,
            "full_probe": probe(DATA_OUT / full) if full else {"error": "无成片"},
            "segments": [
                {"rel": s.get("rel"), "frames": s.get("frames"),
                 "head_break_ratio": s.get("head_break_ratio"),
                 "lum_drop_3frames": s.get("lum_drop_3frames")}
                for s in segs
            ],
            "seam_L1": ((ch.get("seam") or {}).get("seams") or [{}])[0].get("seam_L1"),
        }
        rows.append(row)
    Path(out).write_text(json.dumps({"rows": rows}, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    for r in rows:
        fp = r["full_probe"]
        print(f"\n  {r['tag']} seed={r['seed']} job2={r['job2']}")
        print(f"    成片: {fp.get('path')}")
        print(f"    帧数={fp.get('nb_frames')} 时长={fp.get('duration_s')}s "
              f"尺寸={fp.get('width')}x{fp.get('height')} "
              f"音轨={fp.get('has_audio')} 音频时长={fp.get('audio_duration_s')}")
        for s in r["segments"]:
            print(f"    段 {s['rel']}: {s['frames']}帧 头断点比={s['head_break_ratio']:.4f} "
                  f"亮度降={s['lum_drop_3frames']:+.4f}")
        print(f"    拼接点 L1={r['seam_L1']:.4f}")
    print(f"\n  落盘: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
