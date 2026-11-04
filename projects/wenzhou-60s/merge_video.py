# -*- coding: utf-8 -*-
"""
把多段 H3 成片合并为一个长视频（concat demuxer + 统一重编码 H.264 + AAC）
优先使用 imageio-ffmpeg 自带的 ffmpeg，回退 PyAV。
用法: python merge_video.py out.mp4 clip1.mp4 clip2.mp4 ...
"""
import subprocess
import sys
import tempfile
from pathlib import Path


def get_ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def merge(out_path: str, files: list[str], fps: int = 24):
    files = [str(Path(f).resolve()) for f in files if Path(f).exists()]
    if not files:
        raise SystemExit("没有可合并的文件")
    out_path = str(Path(out_path).resolve())

    # concat 列表（ASCII 临时目录，规避中文路径问题）
    with tempfile.TemporaryDirectory() as td:
        list_file = Path(td) / "list.txt"
        list_file.write_text(
            "".join(f"file '{f}'\n" for f in files), encoding="utf-8")
        tmp_out = str(Path(td) / "merged.mp4")
        cmd = [
            get_ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-r", str(fps),
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", tmp_out,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            print(r.stderr[-2000:])
            raise SystemExit(f"ffmpeg 失败: {r.returncode}")
        import shutil
        shutil.move(tmp_out, out_path)
    print(f"合并完成: {out_path} | {len(files)} 段")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("用法: python merge_video.py out.mp4 clip1.mp4 ...")
    merge(sys.argv[1], sys.argv[2:])
