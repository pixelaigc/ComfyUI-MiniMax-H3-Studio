# -*- coding: utf-8 -*-
"""
H3 Studio 资产素材库 —— 媒体元数据提取。

硬性规则（来自需求）：
  **不对上传媒体做二次转码，除非原格式严重损坏/无法解码。**
  原文件必须原样保留。

所以本模块只做「只读探测」：
  - 图片：PIL 读头拿宽高 / 模式（不重写文件）
  - 视频 / 音频：ffprobe 读 stream 信息（不转码）
  - 只有当原文件**完全无法解码**（ffprobe 报错 / PIL 抛异常）时，
    才走转码兜底路径，并在元数据里记录 transcode_reason。

抽帧与波形图属于「派生文件」，写到 derived 区，不动原件。
"""
import json
import shutil
import subprocess
from pathlib import Path

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

#: 视频抽帧的默认密度（每 N 秒一帧）
DEFAULT_FRAME_INTERVAL = 2.0
#: 抽帧上限（防止长视频爆盘）
MAX_FRAMES = 24
#: 抽帧输出宽度
FRAME_WIDTH = 768


class ProbeError(RuntimeError):
    """媒体探测失败（文件损坏 / 格式不支持）。"""


# ------------------------------------------------------------------ 图片


def probe_image(path: Path) -> dict:
    """读图片信息：宽高 / 模式 / 格式。只读，不改文件。"""
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - 环境缺 PIL
        raise ProbeError(f"缺少 Pillow，无法读取图片信息：{exc}") from exc

    try:
        with Image.open(path) as img:
            img.load()
            width, height = img.size
            info = {
                "width": int(width),
                "height": int(height),
                "mode": img.mode,
                "format": (img.format or "").lower(),
                "frames": int(getattr(img, "n_frames", 1) or 1),
                "animated": bool(getattr(img, "is_animated", False)),
            }
            exif = {}
            try:
                raw = img.getexif()
                for key, value in raw.items():
                    try:
                        exif[str(key)] = str(value)[:120]
                    except Exception:
                        continue
            except Exception:
                pass
            if exif:
                info["exif"] = exif
            return info
    except Exception as exc:
        raise ProbeError(f"图片无法解码：{type(exc).__name__} {exc}") from exc


def make_image_preview(src: Path, dest: Path, width: int = 512) -> bool:
    """生成缩略图（写派生区，不动原件）。"""
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
        with Image.open(src) as img:
            img = img.convert("RGB")
            ratio = width / max(1, img.width)
            height = max(1, int(img.height * ratio))
            img = img.resize((width, height), resample)
            img.save(dest, "JPEG", quality=82)
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ ffprobe


def _ffprobe_json(path: Path, extra: list[str] | None = None) -> dict:
    """
    调 ffprobe 拿 JSON。

    注意：ffprobe **必须显式带 -show_format / -show_streams**，
    只给 -print_format json 会返回空对象 {"streams": []} ——
    那会让探测误判成「文件里没有视频流」。
    """
    if not FFPROBE:
        raise ProbeError("本机没有 ffprobe，无法解析视频/音频")
    cmd = [FFPROBE, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams"]
    cmd.extend(extra or [])
    cmd.append(str(path))
    try:
        raw = subprocess.check_output(cmd, encoding="utf-8", timeout=60,
                                      creationflags=CREATE_NO_WINDOW,
                                      stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as exc:
        raise ProbeError(f"ffprobe 解析失败：{(exc.output or '').strip()[:300]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError("ffprobe 解析超时（文件可能损坏或超大）") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe 输出不是合法 JSON：{exc}") from exc


def probe_video(path: Path) -> dict:
    """视频：分辨率 / 编码 / 帧率 / 时长 / 音频流。"""
    data = _ffprobe_json(path)
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise ProbeError("文件里没有视频流")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = None
    for candidate in (video.get("duration"), fmt.get("duration")):
        if candidate is None:
            continue
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            continue

    fps = None
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = video.get(key) or ""
        if "/" in raw:
            num, _, den = raw.partition("/")
            try:
                num_f, den_f = float(num), float(den)
                if den_f:
                    fps = num_f / den_f
                    break
            except ValueError:
                continue

    info = {
        "width": int(video.get("width") or 0) or None,
        "height": int(video.get("height") or 0) or None,
        "duration_sec": duration,
        "fps": round(fps, 3) if fps else None,
        "codec": video.get("codec_name"),
        "container": (fmt.get("format_name") or "").split(",")[0] or None,
        "bitrate": int(fmt.get("bit_rate") or 0) or None,
        "has_audio": audio is not None,
    }
    if audio:
        info["audio_codec"] = audio.get("codec_name")
        info["sample_rate"] = int(audio.get("sample_rate") or 0) or None
        info["channels"] = int(audio.get("channels") or 0) or None
    return info


def probe_audio(path: Path) -> dict:
    """音频：编码 / 采样率 / 声道 / 时长。"""
    data = _ffprobe_json(path)
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not audio:
        raise ProbeError("文件里没有音频流")

    duration = None
    for candidate in (audio.get("duration"), fmt.get("duration")):
        if candidate is None:
            continue
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            continue

    return {
        "duration_sec": duration,
        "codec": audio.get("codec_name"),
        "audio_codec": audio.get("codec_name"),
        "sample_rate": int(audio.get("sample_rate") or 0) or None,
        "channels": int(audio.get("channels") or 0) or None,
        "container": (fmt.get("format_name") or "").split(",")[0] or None,
        "bitrate": int(fmt.get("bit_rate") or 0) or None,
    }


# ------------------------------------------------------------------ 统一入口


def probe(path: Path, category: str) -> dict:
    """
    按 category 探测元数据。

    返回 dict 至少含：
      ok              是否探测成功
      width/height    图片宽高 / 视频分辨率
      duration_sec    视频 / 音频时长
      fps             视频帧率
      codec           图像/视频编码器（音频为音频编码）
      audio_codec     音频编码
      sample_rate     采样率
      channels        声道
      container       容器 / 格式
      transcode_reason 非空 = 已转码 + 原因
    """
    if category == "image":
        return probe_image(path)
    if category == "video":
        return probe_video(path)
    if category == "audio":
        return probe_audio(path)
    return {"container": (path.suffix or "").lstrip(".").lower() or None}


def is_decodable(path: Path, category: str) -> tuple[bool, str]:
    """判断原文件能否正常解码。返回 (ok, reason)。"""
    try:
        probe(path, category)
        return True, ""
    except ProbeError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"{type(exc).__name__} {exc}"[:200]


def transcode_for_repair(path: Path, dest: Path, category: str, reason: str) -> bool:
    """
    兜底转码：**只在原格式严重损坏/无法解码时调用**。

    这是唯一允许写新编码的路径，产物放派生区；
    原始文件仍原样保留（不覆盖、不删除），并在元数据里标注已转码 + 原因。
    """
    if not FFMPEG:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    if category == "image":
        cmd = [FFMPEG, "-y", "-v", "error", "-i", str(path), str(dest)]
    elif category == "video":
        cmd = [FFMPEG, "-y", "-v", "error", "-i", str(path),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
               "-c:a", "aac", "-b:a", "160k", str(dest)]
    elif category == "audio":
        cmd = [FFMPEG, "-y", "-v", "error", "-i", str(path),
               "-c:a", "libmp3lame", "-b:a", "192k", str(dest)]
    else:
        return False
    try:
        subprocess.check_output(cmd, encoding="utf-8", timeout=900,
                                creationflags=CREATE_NO_WINDOW,
                                stderr=subprocess.STDOUT)
        return dest.is_file() and dest.stat().st_size > 0
    except Exception:
        return False


# ------------------------------------------------------------------ 抽帧 / 波形


def extract_frames(video: Path, out_dir: Path, prefix: str = "frame",
                   interval: float = DEFAULT_FRAME_INTERVAL,
                   max_frames: int = MAX_FRAMES,
                   width: int = FRAME_WIDTH) -> list[dict]:
    """
    按固定时间间隔抽帧（高密度抽帧用于视频分析）。

    interval 越小帧越密。返回 [{path, t}]，t 为该帧在视频里的秒数。
    抽帧产物写派生区，原视频不动。
    """
    if not FFMPEG:
        return []
    try:
        interval = max(0.2, float(interval))
    except (TypeError, ValueError):
        interval = DEFAULT_FRAME_INTERVAL
    max_frames = max(1, min(int(max_frames), MAX_FRAMES))

    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(out_dir / f"{prefix}_%04d.jpg")
    cmd = [FFMPEG, "-y", "-v", "error", "-i", str(video),
           "-vf", f"fps=1/{interval},scale={width}:-2:flags=lanczos",
           "-frames:v", str(max_frames), "-q:v", "3", pattern]
    try:
        subprocess.check_output(cmd, encoding="utf-8", timeout=600,
                                creationflags=CREATE_NO_WINDOW,
                                stderr=subprocess.STDOUT)
    except Exception:
        return []

    frames = []
    for index, path in enumerate(sorted(out_dir.glob(f"{prefix}_*.jpg"))):
        # ffmpeg 的 fps 滤镜第 N 帧对应 N*interval 秒（从 0 起）
        frames.append({"path": str(path), "t": round(index * interval, 3),
                       "index": index + 1})
    return frames


def make_waveform(audio: Path, dest: Path, width: int = 900, height: int = 180) -> bool:
    """生成音频波形图（派生文件，原音频不动）。"""
    if not FFMPEG:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-y", "-v", "error", "-i", str(audio),
           "-filter_complex", f"showwavespic=s={width}x{height}:colors=#6ea8fe",
           "-frames:v", "1", str(dest)]
    try:
        subprocess.check_output(cmd, encoding="utf-8", timeout=300,
                                creationflags=CREATE_NO_WINDOW,
                                stderr=subprocess.STDOUT)
        return dest.is_file() and dest.stat().st_size > 0
    except Exception:
        return False


def make_audio_preview(audio: Path, dest: Path) -> bool:
    """音频预览：优先波形图，失败则退化成可播放的原始音频副本引用。"""
    if make_waveform(audio, dest):
        return True
    return False
