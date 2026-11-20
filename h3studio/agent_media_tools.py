# -*- coding: utf-8 -*-
"""
内置 Agent 的 **ffmpeg 音视频处理工具**（真实调用本机 ffmpeg，非模拟）。

【思路来源 —— 移植自 /home/ubuntu/nonebotqq/plugins/nonebot_plugin_novelai/】
参考实现（咏唱插件的 h3_audio_video_merge.py / h3_audio_edit.py /
h3_yue2_remix.py）里真正值得移植的是**工程思路**，不是 nonebot 的耦合代码：

  1. **先 ffprobe 再拼命令** —— 时长/流信息一律从 ffprobe 拿真实值，
     不用「猜」参数。（移植自 _probe_media）
  2. **concat 前统一视频时间刻度** —— 各源 time_base 不同（1/12288 vs
     1/90000）时，concat + `-c copy` 会把 32 秒的片子拉成 236 秒。
     先无损 remux（`-c copy` + `-video_track_timescale 153600`）再拼。
     （移植自 _remux_uniform_timescale）
  3. **音轨存在性不齐先补等长静音** —— 否则 concat 时长对不齐。
     （移植自 _add_silent_audio）
  4. **混音前统一采样率/声道/采样格式** —— amix 要求各路布局一致。
     （移植自 _stereo_audio_chain：aresample + aformat fltp/stereo）
  5. **分辨率不一致先统一画布** —— scale(decrease) + pad + setsar + fps。
     （移植自 _plan_merge 的 v_label 链）
  6. **视频流与音轨分两步做** —— 先拼视频流，再处理音轨，最后 mux。
     直接一步做会在采样率不一致时音轨漂移。（移植自 Step1/Step2）
  7. **stderr 只取尾部** + **临时文件必清理** + 失败不留半成品。

【不移植的部分】
nonebot 的事件对象 / 会话状态机 / QQ 文件下载 / 用户交互菜单 / 触发词
—— 那些与本工作台（HTTP API + 本地素材库）无关。

【安全边界】
  * 输入只能来自**素材库**（asset_id），不接受任意本机路径。
  * 产物写进素材库（derived 区），由 asset_service 统一入库去重。
  * ffmpeg 参数**全部由代码拼装**，不接受模型传入的任意命令行字符串。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from . import agent_safety
from . import assets as asset_store
from . import asset_repo
from . import asset_service
from .assets import LibraryError

#: 单条命令超时（秒）。转码/拼接大文件可能几分钟。
DEFAULT_CMD_TIMEOUT = 900.0

#: 混音时统一的采样率（与咏唱实现一致：44100）。
MIX_SAMPLE_RATE = 44100

#: 混音时统一的音频码率。
MIX_AUDIO_BITRATE = "192k"

#: concat 前统一视频时间刻度（153600 能被 12/15/20/24/25/30/48/50/60/120
# 整除，不会引入新的时间戳舍入误差 —— 与咏唱实现同值）。
CONCAT_TIMESCALE = 153600

#: 混音时每路音量的归一系数。
DEFAULT_MIX_VOLUME = 1.0

#: 抽取帧时的默认间隔（秒）。
DEFAULT_FRAME_INTERVAL = 2.0

#: 抽取帧的上限（防止长视频爆盘）。
MAX_FRAMES = 64

#: 允许的输出容器（按扩展名）。
ALLOWED_OUTPUT_SUFFIXES = {
    ".mp4", ".webm", ".mkv", ".mov", ".m4v",
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".opus", ".m4a",
}


class MediaToolError(Exception):
    """音视频工具失败。message 面向用户可读。"""

    def __init__(self, message, code="media_tool_error", status=400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


# ------------------------------------------------------------------ 底层执行


async def _run_ffmpeg(cmd: list[str], timeout: float) -> tuple[int, str]:
    """
    跑一条 ffmpeg 命令。返回 (returncode, stderr 尾部)。

    【为什么用 create_subprocess_exec 而不是 shell=True】
    参数是 list，**不做 shell 拼接** —— 模型给的参数永远进不了 shell 层，
    也就没有注入面。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise MediaToolError(
            "本机找不到 ffmpeg（可在「agent 管理」里填写 ffmpeg 路径）",
            code="ffmpeg_missing", status=503) from exc
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise MediaToolError(
            f"ffmpeg 执行超时（{int(timeout)} 秒），已终止",
            code="ffmpeg_timeout", status=504) from exc
    return proc.returncode or 0, (stderr or b"").decode("utf-8", "replace")[-1200:]


async def _probe(path: Path) -> dict:
    """用 ffprobe 拿真实流信息（时长/编码/分辨率/音轨）。"""
    from . import media_probe

    probe_bin = media_probe.FFPROBE
    if not probe_bin:
        raise MediaToolError("本机找不到 ffprobe，无法解析媒体",
                             code="ffprobe_missing", status=503)
    cmd = [probe_bin, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        raw, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError as exc:
        raise MediaToolError("ffprobe 超时", code="ffprobe_timeout",
                             status=504) from exc
    if (proc.returncode or 0) != 0:
        raise MediaToolError(
            f"ffprobe 解析失败：{(stderr or b'').decode('utf-8', 'replace')[:200]}",
            code="probe_failed")
    try:
        data = json.loads(raw or b"{}")
    except (TypeError, ValueError) as exc:
        raise MediaToolError("ffprobe 输出不是合法 JSON", code="probe_failed") from exc
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = None
    for candidate in (fmt.get("duration"),
                      (video or {}).get("duration"),
                      (audio or {}).get("duration")):
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            continue
    time_base = (video or {}).get("time_base") or ""
    fps = None
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = (video or {}).get(key) or ""
        if "/" in raw:
            num, _, den = raw.partition("/")
            try:
                num_f, den_f = float(num), float(den)
                if den_f:
                    fps = num_f / den_f
                    break
            except ValueError:
                continue
    return {
        "has_video": video is not None,
        "has_audio": audio is not None,
        "duration": duration or 0.0,
        "width": int((video or {}).get("width") or 0),
        "height": int((video or {}).get("height") or 0),
        "v_codec": (video or {}).get("codec_name") or "",
        "pix_fmt": (video or {}).get("pix_fmt") or "",
        "time_base": time_base,
        "fps": round(fps, 3) if fps else 0.0,
        "a_codec": (audio or {}).get("codec_name") or "",
        "sample_rate": int((audio or {}).get("sample_rate") or 0),
        "channels": int((audio or {}).get("channels") or 0),
        "container": (fmt.get("format_name") or "").split(",")[0],
        "size_bytes": path.stat().st_size if path.exists() else 0,
    }


# ------------------------------------------------------------------ 素材库 IO


def _resolve_asset_path(asset_id: str) -> tuple[Path, dict]:
    """asset_id -> (本机绝对路径, 素材条目)。不在素材库就报错。"""
    asset_id = str(asset_id or "").strip()
    if not asset_id:
        raise MediaToolError("缺少 asset_id")
    with asset_store.connect() as conn:
        asset = asset_repo.get_asset(conn, asset_id, include_deleted=True)
        version = asset_repo.current_version(conn, asset_id)
    if not asset or not version:
        raise MediaToolError(f"素材不存在：{asset_id}", code="asset_not_found",
                             status=404)
    if asset.get("deleted_at"):
        raise MediaToolError(f"素材在回收站里：{asset.get('name') or asset_id}")
    root = asset_store.library_root()
    path = asset_store.resolve_inside(root, version["storage_path"])
    if not path or not path.is_file():
        raise MediaToolError(f"素材文件已丢失：{asset_id}",
                             code="asset_file_missing", status=404)
    return path, {
        "asset_id": asset_id,
        "name": asset.get("name") or asset_id,
        "category": asset.get("category") or "",
        "bytes": version.get("bytes"),
        "duration_sec": version.get("duration_sec"),
        "width": version.get("width"),
        "height": version.get("height"),
    }


def _register_output(tmp_path: Path, name: str, category: str,
                     tags: list[str] | None = None) -> dict:
    """
    把产物登记进素材库（复用 asset_service.register_asset 的去重/元数据/预览）。

    category 决定素材库的分类与后续分析器 —— 必须给真实的 video / audio。
    """
    subtype = {"video": "clip", "audio": "music", "image": "photo"}.get(
        category, "other")
    with asset_store.connect() as conn:
        try:
            result = asset_service.register_asset(
                conn,
                source_path=tmp_path,
                filename=name,
                category=category,
                subtype=subtype,
                source="agent_media_tool",
                creator="agent",
                tags=tags or [],
            )
        except LibraryError as exc:
            raise MediaToolError(exc.message, code=exc.code,
                                 status=exc.status) from exc
        asset = result.get("asset") or {}
        asset_id = asset.get("id") or ""
        version = result.get("version") or asset_repo.current_version(conn, asset_id)
        return {
            "asset_id": asset_id,
            "name": asset.get("name") or name,
            "category": category,
            "duplicated": bool(result.get("duplicated")),
            "bytes": (version or {}).get("bytes"),
            "duration_sec": (version or {}).get("duration_sec"),
            "width": (version or {}).get("width"),
            "height": (version or {}).get("height"),
            "preview_url": (f"/api/assets/{asset_id}/preview"
                            if (version or {}).get("preview_path") else None),
            "content_url": f"/api/assets/{asset_id}/content",
        }


def _safe_output_name(stem: str, suffix: str) -> str:
    """产物文件名：白名单后缀 + 去掉路径分隔符。"""
    clean = "".join(ch for ch in str(stem or "output")
                    if ch.isalnum() or ch in "-_. ").strip()[:80] or "output"
    suffix = suffix.lower()
    if suffix not in ALLOWED_OUTPUT_SUFFIXES:
        suffix = ".mp4"
    return f"{clean}_{uuid.uuid4().hex[:8]}{suffix}"


def _output_category(suffix: str) -> str:
    if suffix.lower() in (".mp3", ".wav", ".flac", ".aac", ".ogg", ".opus", ".m4a"):
        return "audio"
    if suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
        return "image"
    return "video"


# ------------------------------------------------------------------ 各工具实现


async def op_probe(asset_id: str) -> dict:
    """探测一个素材的音视频参数（ffprobe 真实读）。"""
    path, entry = _resolve_asset_path(asset_id)
    info = await _probe(path)
    info["asset"] = entry
    return info


async def op_concat(asset_ids: list[str], output_name: str = "",
                     timeout: float = DEFAULT_CMD_TIMEOUT) -> dict:
    """
    拼接多个视频（顺序 = asset_ids 顺序）。

    工程要点（移植自咏唱 _plan_merge）：
      * 先 ffprobe 拿每段的真实参数；
      * time_base 不一致 -> 先无损 remux 统一刻度（否则时长被放大）；
      * 音轨有无不齐 -> 给缺的补等长静音；
      * 参数一致 -> concat demuxer + `-c copy`（免重编码）；
      * 参数不一致 -> filter_complex 统一画布/帧率后重编码。
    """
    if not asset_ids:
        raise MediaToolError("concat 需要至少一个 asset_id")
    if len(asset_ids) > 20:
        raise MediaToolError(f"一次最多拼接 20 段，收到 {len(asset_ids)} 段")

    paths: list[Path] = []
    entries: list[dict] = []
    for aid in asset_ids:
        path, entry = _resolve_asset_path(aid)
        paths.append(path)
        entries.append(entry)

    infos = []
    for path in paths:
        info = await _probe(path)
        if not info["has_video"]:
            raise MediaToolError(f"素材不是视频：{path.name}",
                                 code="not_a_video")
        infos.append(info)

    with tempfile.TemporaryDirectory(prefix="h3agent-concat-") as tmp:
        tmp_dir = Path(tmp)
        work_paths = list(paths)

        # --- 1) 音轨不齐 -> 补静音 ---
        flags = [i["has_audio"] for i in infos]
        if any(flags) and not all(flags):
            padded: list[Path] = []
            for index, (path, info) in enumerate(zip(paths, infos)):
                if info["has_audio"]:
                    padded.append(path)
                    continue
                silent = tmp_dir / f"silent_{index}.mp4"
                cmd = [
                    _ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(path),
                    "-f", "lavfi", "-i",
                    f"anullsrc=r={MIX_SAMPLE_RATE}:cl=stereo",
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", MIX_AUDIO_BITRATE,
                    "-shortest", str(silent),
                ]
                code, err = await _run_ffmpeg(cmd, timeout)
                if code != 0 or not silent.is_file():
                    raise MediaToolError(
                        f"给 {path.name} 补静音音轨失败：{err[:200]}",
                        code="concat_failed")
                padded.append(silent)
            work_paths = padded
            infos = [await _probe(p) for p in work_paths]

        # --- 2) time_base 不一致 -> 无损 remux 统一刻度 ---
        bases = {i["time_base"] for i in infos if i["time_base"]}
        remuxed = False
        if len(bases) > 1:
            uniformed: list[Path] = []
            for index, (path, info) in enumerate(zip(work_paths, infos)):
                dst = tmp_dir / f"uniform_{index}.mp4"
                cmd = [
                    _ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(path), "-map", "0", "-c", "copy",
                    "-video_track_timescale", str(CONCAT_TIMESCALE),
                    "-movflags", "+faststart", str(dst),
                ]
                code, err = await _run_ffmpeg(cmd, timeout)
                if code != 0 or not dst.is_file():
                    raise MediaToolError(
                        f"统一时间刻度失败（{path.name}）：{err[:200]}",
                        code="concat_failed")
                uniformed.append(dst)
            work_paths = uniformed
            infos = [await _probe(p) for p in work_paths]
            remuxed = True

        # --- 3) 拼视频流 ---
        v_key = {(i["v_codec"], i["width"], i["height"], i["pix_fmt"])
                 for i in infos}
        a_key = {(i["a_codec"], i["sample_rate"], i["channels"]) for i in infos}
        all_audio = all(i["has_audio"] for i in infos)

        list_file = tmp_dir / "concat.txt"
        list_file.write_text(
            "".join(f"file '{str(p).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"
                    for p in work_paths),
            encoding="utf-8")

        out_path = tmp_dir / "concat_out.mp4"
        if len(v_key) == 1 and len(a_key) == 1 and all_audio:
            # 快速路径：全链路免重编码
            cmd = [
                _ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(list_file),
                "-map", "0:v:0", "-map", "0:a:0",
                "-c:v", "copy", "-c:a", "copy",
                "-movflags", "+faststart", "-avoid_negative_ts", "make_zero",
                str(out_path),
            ]
            note = "参数一致：视频与音频均免重编码直拼"
        else:
            # 统一画布 + 统一帧率 -> 重编码
            #
            # 【filter graph 的正确结构 —— 2026-11-14 实测修的坑】
            # 视频流与音频流**各自**先 concat，再分别 map：
            #   [v0][v1]concat=n=2:v=1:a=0[vout]
            #   [a0][a1]concat=n=2:v=0:a=1[aout]
            #   -map [vout] -map [aout]
            #
            # 【原来的错法】把 v_labels 直接 map（`-map [v0][v1]`）而**没有**
            # concat 视频流 —— 结果是输出变成两路独立视频（而不是拼好的），
            # ffmpeg 直接报 "Filter 'format:default' has output 1 (v1) unconnected"。
            # 一个 -map 只能接一个流标签，多路必须各自 concat。
            base = max(infos, key=lambda i: i["width"] * i["height"])
            cw, ch = base["width"], base["height"]
            fps_list = [i.get("fps") or 0 for i in infos]
            fps_list = [f for f in fps_list if f > 0]
            tfps = max(fps_list) if fps_list else 24.0
            cmd = [_ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error"]
            for path in work_paths:
                cmd.extend(["-i", str(path)])
            chains: list[str] = []
            v_labels: list[str] = []
            a_labels: list[str] = []
            for index, info in enumerate(infos):
                vlabel = f"v{index}"
                chains.append(
                    f"[{index}:v]scale={cw}:{ch}:force_original_aspect_ratio=decrease,"
                    f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                    f"fps={tfps:.6f},format=yuv420p[{vlabel}]")
                v_labels.append(f"[{vlabel}]")
                if info["has_audio"]:
                    alabel = f"a{index}"
                    chains.append(
                        f"[{index}:a]aresample={MIX_SAMPLE_RATE},"
                        f"aformat=sample_fmts=fltp:channel_layouts=stereo[{alabel}]")
                    a_labels.append(f"[{alabel}]")
            # 视频流 concat（永远要做）
            chains.append(
                "".join(v_labels) +
                f"concat=n={len(work_paths)}:v=1:a=0[vout]")
            maps = ["-map", "[vout]"]
            # 音频流 concat（有音轨才做）
            if a_labels:
                chains.append(
                    "".join(a_labels) +
                    f"concat=n={len(work_paths)}:v=0:a=1[aout]")
                maps.extend(["-map", "[aout]"])
            cmd.extend(["-filter_complex", ";".join(chains)])
            cmd.extend(maps)
            cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                        "-c:a", "aac", "-b:a", MIX_AUDIO_BITRATE,
                        "-movflags", "+faststart", str(out_path)])
            note = "参数不一致：已统一画布/帧率/采样率后重编码"

        code, err = await _run_ffmpeg(cmd, timeout)
        if code != 0 or not out_path.is_file():
            raise MediaToolError(f"拼接失败：{err[:300]}", code="concat_failed")

        final = await _probe(out_path)
        name = _safe_output_name(output_name or "concat", ".mp4")
        registered = _register_output(out_path, name, "video")
        return {
            "operation": "concat",
            "inputs": [e["asset_id"] for e in entries],
            "output": registered,
            "note": note,
            "remuxed": remuxed,
            "duration_sec": final["duration"],
            "width": final["width"],
            "height": final["height"],
        }


async def op_transcode(asset_id: str, output_format: str = "mp4",
                       video_codec: str = "", audio_codec: str = "",
                       crf: int = 20, timeout: float = DEFAULT_CMD_TIMEOUT,
                       ) -> dict:
    """转码（换容器 / 换编码 / 压体积）。"""
    path, entry = _resolve_asset_path(asset_id)
    fmt = str(output_format or "mp4").strip().lower().lstrip(".")
    suffix = f".{fmt}"
    if suffix not in ALLOWED_OUTPUT_SUFFIXES:
        raise MediaToolError(
            f"不支持的输出格式 {fmt}（可用："
            f"{'、'.join(sorted(ALLOWED_OUTPUT_SUFFIXES))}）")
    info = await _probe(path)
    with tempfile.TemporaryDirectory(prefix="h3agent-transcode-") as tmp:
        out_path = Path(tmp) / f"out{suffix}"
        cmd = [_ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
               "-i", str(path)]
        if info["has_video"]:
            vcode = (video_codec or "").strip()
            if not vcode:
                vcode = {"webm": "libvpx-vp9", "mkv": "libx264",
                         "mov": "libx264", "mp4": "libx264"}.get(fmt, "libx264")
            cmd.extend(["-c:v", vcode])
            if vcode.startswith("libx264"):
                cmd.extend(["-preset", "veryfast", "-crf", str(int(crf))])
            elif vcode.startswith("libvpx"):
                cmd.extend(["-crf", str(int(crf)), "-b:v", "0"])
        else:
            cmd.extend(["-vn"])
        if info["has_audio"]:
            acode = (audio_codec or "").strip()
            if not acode:
                acode = {"webm": "libopus", "mp3": "libmp3lame",
                         "wav": "pcm_s16le", "flac": "flac",
                         "opus": "libopus", "ogg": "libvorbis"}.get(fmt, "aac")
            cmd.extend(["-c:a", acode])
            if acode == "libmp3lame":
                cmd.extend(["-b:a", "192k"])
            elif acode == "aac":
                cmd.extend(["-b:a", MIX_AUDIO_BITRATE])
        else:
            cmd.extend(["-an"])
        cmd.append(str(out_path))
        code, err = await _run_ffmpeg(cmd, timeout)
        if code != 0 or not out_path.is_file():
            raise MediaToolError(f"转码失败：{err[:300]}", code="transcode_failed")
        final = await _probe(out_path)
        name = _safe_output_name(entry["name"] or "transcode", suffix)
        registered = _register_output(out_path, name, _output_category(suffix))
        return {
            "operation": "transcode",
            "input": entry["asset_id"],
            "output_format": fmt,
            "output": registered,
            "duration_sec": final["duration"],
            "width": final["width"],
            "height": final["height"],
        }


async def op_extract_frames(asset_id: str, interval: float = DEFAULT_FRAME_INTERVAL,
                             max_frames: int = MAX_FRAMES,
                             timeout: float = DEFAULT_CMD_TIMEOUT) -> dict:
    """从视频抽帧（产物逐张入素材库，category=image）。"""
    path, entry = _resolve_asset_path(asset_id)
    info = await _probe(path)
    if not info["has_video"]:
        raise MediaToolError(f"素材不是视频：{entry['name']}", code="not_a_video")
    try:
        step = max(0.2, float(interval))
    except (TypeError, ValueError):
        step = DEFAULT_FRAME_INTERVAL
    count = max(1, min(int(max_frames), MAX_FRAMES))
    with tempfile.TemporaryDirectory(prefix="h3agent-frames-") as tmp:
        tmp_dir = Path(tmp)
        pattern = tmp_dir / "frame_%04d.jpg"
        cmd = [
            _ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(path),
            "-vf", f"fps=1/{step},scale=768:-2:flags=lanczos",
            "-frames:v", str(count), "-q:v", "3", str(pattern),
        ]
        code, err = await _run_ffmpeg(cmd, timeout)
        if code != 0:
            raise MediaToolError(f"抽帧失败：{err[:300]}", code="extract_failed")
        frames = sorted(tmp_dir.glob("frame_*.jpg"))
        if not frames:
            raise MediaToolError("没有抽出任何帧（视频可能为空）",
                                 code="extract_failed")
        outputs = []
        for index, frame in enumerate(frames):
            name = _safe_output_name(f"{entry['name']}_frame{index + 1}", ".jpg")
            outputs.append(_register_output(frame, name, "image"))
        return {
            "operation": "extract_frames",
            "input": entry["asset_id"],
            "interval_sec": step,
            "frame_count": len(outputs),
            "frames": outputs,
        }


async def op_mix_audio(video_asset_id: str, audio_asset_ids: list[str],
                        volumes: list[float] | None = None,
                        replace: bool = False,
                        timeout: float = DEFAULT_CMD_TIMEOUT) -> dict:
    """
    混音：把若干音频混进视频（或替换原音轨）。

    移植自咏唱的音轨处理链：
      * 每路音频先 aresample + aformat(fltp/stereo)（amix 要求一致）；
      * amix 用 duration=longest + dropout_transition=0 + normalize=0
        （normalize=0 保留各路原始响度关系）；
      * 视频流用 `-c:v copy`（不重编码画面）。
    """
    video_path, video_entry = _resolve_asset_path(video_asset_id)
    if not audio_asset_ids:
        raise MediaToolError("mix_audio 至少要一段音频")
    if len(audio_asset_ids) > 8:
        raise MediaToolError(f"一次最多混 8 段音频，收到 {len(audio_asset_ids)} 段")
    video_info = await _probe(video_path)
    if not video_info["has_video"]:
        raise MediaToolError(f"素材不是视频：{video_entry['name']}",
                             code="not_a_video")

    audio_paths: list[Path] = []
    audio_entries: list[dict] = []
    for aid in audio_asset_ids:
        apath, aentry = _resolve_asset_path(aid)
        ainfo = await _probe(apath)
        if not ainfo["has_audio"]:
            raise MediaToolError(f"素材没有音轨：{aentry['name']}",
                                 code="not_an_audio")
        audio_paths.append(apath)
        audio_entries.append(aentry)

    vols = list(volumes or [])
    while len(vols) < len(audio_paths):
        vols.append(DEFAULT_MIX_VOLUME)

    with tempfile.TemporaryDirectory(prefix="h3agent-mix-") as tmp:
        out_path = Path(tmp) / "mixed.mp4"
        cmd = [_ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
               "-i", str(video_path)]
        for apath in audio_paths:
            cmd.extend(["-i", str(apath)])
        chains: list[str] = []
        labels: list[str] = []
        for index, vol in enumerate(vols):
            label = f"a{index}"
            try:
                gain = float(vol)
            except (TypeError, ValueError):
                gain = DEFAULT_MIX_VOLUME
            gain = max(0.0, min(4.0, gain))
            chains.append(
                f"[{index + 1}:a]aresample={MIX_SAMPLE_RATE},"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"volume={gain:.4f}[{label}]")
            labels.append(f"[{label}]")
        if replace:
            # 替换原音：原音作为第 0 路一起进 amix（视频本身没音轨时跳过）
            if video_info["has_audio"]:
                chains.insert(0,
                              f"[0:a]aresample={MIX_SAMPLE_RATE},"
                              f"aformat=sample_fmts=fltp:channel_layouts=stereo[orig]")
                labels.insert(0, "[orig]")
        chains.append("".join(labels) +
                      f"amix=inputs={len(labels)}:duration=longest:"
                      f"dropout_transition=0:normalize=0[aout]")
        cmd.extend(["-filter_complex", ";".join(chains)])
        cmd.extend(["-map", "0:v:0", "-map", "[aout]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", MIX_AUDIO_BITRATE,
                    "-movflags", "+faststart", str(out_path)])
        code, err = await _run_ffmpeg(cmd, timeout)
        if code != 0 or not out_path.is_file():
            raise MediaToolError(f"混音失败：{err[:300]}", code="mix_failed")
        final = await _probe(out_path)
        name = _safe_output_name(f"{video_entry['name']}_mix", ".mp4")
        registered = _register_output(out_path, name, "video")
        return {
            "operation": "mix_audio",
            "video_input": video_entry["asset_id"],
            "audio_inputs": [e["asset_id"] for e in audio_entries],
            "volumes": vols[:len(audio_paths)],
            "replace_original": bool(replace),
            "output": registered,
            "duration_sec": final["duration"],
        }


async def op_merge_av(video_asset_id: str, audio_asset_id: str,
                      timeout: float = DEFAULT_CMD_TIMEOUT) -> dict:
    """音视频合流：给视频配上音频（或给音频配上视频）。"""
    video_path, video_entry = _resolve_asset_path(video_asset_id)
    audio_path, audio_entry = _resolve_asset_path(audio_asset_id)
    video_info = await _probe(video_path)
    audio_info = await _probe(audio_path)
    if not video_info["has_video"]:
        raise MediaToolError(f"素材不是视频：{video_entry['name']}",
                             code="not_a_video")
    if not audio_info["has_audio"]:
        raise MediaToolError(f"素材没有音轨：{audio_entry['name']}",
                             code="not_an_audio")
    with tempfile.TemporaryDirectory(prefix="h3agent-av-") as tmp:
        out_path = Path(tmp) / "merged.mp4"
        cmd = [
            _ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video_path), "-i", str(audio_path),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", MIX_AUDIO_BITRATE,
            "-shortest", "-movflags", "+faststart", str(out_path),
        ]
        code, err = await _run_ffmpeg(cmd, timeout)
        if code != 0 or not out_path.is_file():
            raise MediaToolError(f"音视频合流失败：{err[:300]}",
                                 code="merge_failed")
        final = await _probe(out_path)
        name = _safe_output_name(f"{video_entry['name']}_av", ".mp4")
        registered = _register_output(out_path, name, "video")
        return {
            "operation": "merge_av",
            "video_input": video_entry["asset_id"],
            "audio_input": audio_entry["asset_id"],
            "output": registered,
            "duration_sec": final["duration"],
        }


async def op_extract_audio(asset_id: str, output_format: str = "m4a",
                           start_sec: float = 0.0,
                           duration_sec: float | None = None,
                           timeout: float = DEFAULT_CMD_TIMEOUT) -> dict:
    """
    从视频里**抽出音轨**，产物是独立的音频素材。

    【为什么必须有（2026-11-20 需求 C）】
    用户要「把某个视频的音轨抽出来单独用」时，之前的工具集里
    **没有对应入口** —— media_transcode 只能整体转码
    （视频流还在），media_merge_av 又必须给一段现成的音频。

    实现要点：
      * 输出容器按 output_format 选（m4a / mp3 / wav / flac / opus）；
      * 可选起止（start_sec / duration_sec）—— 与 op_trim 同一套语义；
      * 视频流用 ``-vn`` 丢掉，只留音频；
      * 产物走统一的 ``_register_output`` 入库（category=audio），
        可被后续工具（media_merge_av / media_mix_audio）引用。
    """
    path, entry = _resolve_asset_path(asset_id)
    info = await _probe(path)
    if not info["has_audio"]:
        raise MediaToolError(f"素材没有音轨：{entry['name']}",
                             code="not_an_audio")
    fmt = str(output_format or "m4a").strip().lower().lstrip(".")
    suffix = f".{fmt}"
    if suffix not in ALLOWED_OUTPUT_SUFFIXES:
        raise MediaToolError(
            f"不支持的音频格式 {fmt}（可用："
            f"{'、'.join(sorted(s for s in ALLOWED_OUTPUT_SUFFIXES if s in ('.mp3', '.wav', '.flac', '.aac', '.ogg', '.opus', '.m4a')))}）")
    acode = {"m4a": "aac", "mp3": "libmp3lame", "wav": "pcm_s16le",
             "flac": "flac", "opus": "libopus", "ogg": "libvorbis",
             "aac": "aac"}.get(fmt, "aac")
    with tempfile.TemporaryDirectory(prefix="h3agent-extractaudio-") as tmp:
        out_path = Path(tmp) / f"audio{suffix}"
        cmd = [_ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error"]
        cmd.extend(_time_args(start_sec, duration_sec))
        cmd.extend(["-i", str(path), "-vn"])
        if acode == "libmp3lame":
            cmd.extend(["-b:a", "192k"])
        elif acode == "aac":
            cmd.extend(["-b:a", MIX_AUDIO_BITRATE])
        cmd.append(str(out_path))
        code, err = await _run_ffmpeg(cmd, timeout)
        if code != 0 or not out_path.is_file():
            raise MediaToolError(f"抽取音轨失败：{err[:300]}",
                                 code="extract_audio_failed")
        final = await _probe(out_path)
        name = _safe_output_name(f"{entry['name']}_audio", suffix)
        registered = _register_output(out_path, name, "audio")
        return {
            "operation": "extract_audio",
            "input": entry["asset_id"],
            "output_format": fmt,
            "output": registered,
            "duration_sec": final["duration"],
            "sample_rate": final["sample_rate"],
            "channels": final["channels"],
        }


def _time_args(start_sec: float = 0.0,
               duration_sec: float | None = None) -> list[str]:
    """
    生成 ``-ss`` / ``-t`` 参数组（**输出侧**裁剪 = 精确，不丢帧）。

    【为什么放 -ss 在 -i 之前】
    ffmpeg 的 ``-ss`` 放在输入侧是**快速定位**（按关键帧），
    放在输出侧才是**精确裁剪**。素材剪辑要的是后者 ——
    放错位置会导致切出来的片段起点偏移。

    【为什么 -t 而不是 -to】``-t`` 是「从当前起点起多少秒」，
    与 ``-ss`` 组合时语义无歧义；``-to`` 在 ``-ss`` 之后表示绝对时间点，
    容易和用户口语里的「第几秒到第几秒」混淆。
    """
    args: list[str] = []
    try:
        start = max(0.0, float(start_sec or 0.0))
    except (TypeError, ValueError):
        start = 0.0
    if duration_sec is None:
        length = None
    else:
        try:
            length = float(duration_sec)
        except (TypeError, ValueError):
            length = None
        if length is not None and length <= 0:
            raise MediaToolError("裁剪时长必须大于 0")
    if start > 0:
        args.extend(["-ss", f"{start:.3f}"])
    if length is not None:
        args.extend(["-t", f"{length:.3f}"])
    return args


async def op_trim(asset_id: str, start_sec: float = 0.0,
                   duration_sec: float | None = None,
                   output_name: str = "",
                   timeout: float = DEFAULT_CMD_TIMEOUT) -> dict:
    """
    按时长裁剪 / 截取片段。

    【语义】
      * start_sec = 0 且 duration_sec 给定 -> 截取**开头**的 N 秒；
      * start_sec 给定且 duration_sec 给定 -> 截取 [start, start+duration)；
      * 只给 start_sec -> 从 start 截到素材结尾。

    实现要点：
      * 输出侧 ``-ss``（精确裁剪，不丢帧、不偏移）；
      * **音视频一起裁**（不丢音轨）—— 保留原编码（``-c copy``）时
        若源有关键帧间隔问题会导致起点偏移，所以这里用
        ``-c:v libx264 -c:a aac`` 重编码保证帧精确；
      * 产物入库（category 随内容类型定），可被后续工具引用。
    """
    path, entry = _resolve_asset_path(asset_id)
    info = await _probe(path)
    total = float(info.get("duration") or 0.0)
    try:
        start = max(0.0, float(start_sec or 0.0))
    except (TypeError, ValueError):
        raise MediaToolError("start_sec 必须是数字")
    if total > 0 and start >= total:
        raise MediaToolError(
            f"起始时间 {start:g} 秒超出素材时长 {total:g} 秒",
            code="trim_out_of_range")
    length = None
    if duration_sec is not None:
        try:
            length = float(duration_sec)
        except (TypeError, ValueError):
            raise MediaToolError("duration_sec 必须是数字")
        if length <= 0:
            raise MediaToolError("裁剪时长必须大于 0")
        if total > 0 and start + length > total + 0.5:
            # 不静默截断 —— 明确告诉调用方「素材只有这么长」
            length = max(0.0, total - start)
            clipped = True
        else:
            clipped = False
    else:
        clipped = False
    with tempfile.TemporaryDirectory(prefix="h3agent-trim-") as tmp:
        out_path = Path(tmp) / "trim.mp4"
        cmd = [_ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error"]
        cmd.extend(_time_args(start, length))
        cmd.extend(["-i", str(path)])
        if info["has_video"]:
            cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"])
        else:
            cmd.extend(["-vn"])
        if info["has_audio"]:
            cmd.extend(["-c:a", "aac", "-b:a", MIX_AUDIO_BITRATE])
        else:
            cmd.extend(["-an"])
        cmd.extend(["-movflags", "+faststart", str(out_path)])
        code, err = await _run_ffmpeg(cmd, timeout)
        if code != 0 or not out_path.is_file():
            raise MediaToolError(f"裁剪失败：{err[:300]}", code="trim_failed")
        final = await _probe(out_path)
        suffix = ".mp4"
        name = _safe_output_name(output_name or f"{entry['name']}_trim",
                                 suffix)
        registered = _register_output(out_path, name, "video")
        return {
            "operation": "trim",
            "input": entry["asset_id"],
            "start_sec": start,
            "duration_sec": final["duration"],
            "requested_duration_sec": length,
            "clipped_to_source": clipped,
            "output": registered,
            "width": final["width"],
            "height": final["height"],
        }


async def op_normalize_audio(asset_id: str, target_lufs: float = -14.0,
                              max_true_peak: float = -1.5,
                              timeout: float = DEFAULT_CMD_TIMEOUT) -> dict:
    """
    音频**响度归一**（EBU R128 / ITU-R BS.1770）。

    【为什么必须有（2026-11-20 需求 C）】
    「把音视频合并成带背景音乐的成片」时，原声与 BGM 的响度往往
    差很多 —— 直接 amix 出来的成片要么爆音要么听不清。
    loudnorm 是唯一能给出**可复现目标响度**的 ffmpeg 滤镜。

    【实现要点】
      * 用 ffmpeg 的 ``loudnorm``（双遍法的单遍版本）：
        ``loudnorm=I=<target>:TP=<peak>:LRA=11``；
      * ``TP``（true peak）上限默认 -1.5 dBTP —— 留够余量防削波；
      * 只处理音频流，视频流 ``-c:v copy``（不重编码画面）；
      * 产物入库（category 随内容类型定）。
    """
    path, entry = _resolve_asset_path(asset_id)
    info = await _probe(path)
    if not info["has_audio"]:
        raise MediaToolError(f"素材没有音轨：{entry['name']}",
                             code="not_an_audio")
    try:
        lufs = float(target_lufs)
    except (TypeError, ValueError):
        lufs = -14.0
    if not (-40.0 <= lufs <= -5.0):
        raise MediaToolError(
            f"目标响度 {lufs:g} LUFS 超出合理区间（-40 ~ -5 LUFS）",
            code="invalid_lufs")
    try:
        peak = float(max_true_peak)
    except (TypeError, ValueError):
        peak = -1.5
    with tempfile.TemporaryDirectory(prefix="h3agent-loudnorm-") as tmp:
        out_path = Path(tmp) / ("normalized.mp4" if info["has_video"]
                                else "normalized.m4a")
        cmd = [_ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
               "-i", str(path)]
        if info["has_video"]:
            cmd.extend(["-c:v", "copy"])
        else:
            cmd.extend(["-vn"])
        cmd.extend(["-af", f"loudnorm=I={lufs:g}:TP={peak:g}:LRA=11",
                    "-c:a", "aac", "-b:a", MIX_AUDIO_BITRATE])
        if info["has_video"]:
            cmd.extend(["-movflags", "+faststart"])
        cmd.append(str(out_path))
        code, err = await _run_ffmpeg(cmd, timeout)
        if code != 0 or not out_path.is_file():
            raise MediaToolError(f"响度归一失败：{err[:300]}",
                                 code="normalize_failed")
        final = await _probe(out_path)
        suffix = ".mp4" if info["has_video"] else ".m4a"
        name = _safe_output_name(f"{entry['name']}_norm", suffix)
        registered = _register_output(out_path, name,
                                      "video" if info["has_video"] else "audio")
        return {
            "operation": "normalize_audio",
            "input": entry["asset_id"],
            "target_lufs": lufs,
            "max_true_peak": peak,
            "output": registered,
            "duration_sec": final["duration"],
        }


async def op_replace_audio(video_asset_id: str, audio_asset_id: str,
                            keep_original: bool = False,
                            volume: float = 1.0,
                            timeout: float = DEFAULT_CMD_TIMEOUT) -> dict:
    """
    **替换音轨**（不是混音）。

    【为什么与 media_mix_audio 分开（2026-11-20 需求 C）】
    ``media_mix_audio(replace=True)`` 的真实语义是「把原音**也**作为
    一路送进 amix」—— 原声仍然留在成片里。那是「混」，不是「替换」。
    用户说「把这段 BGM 换成这段音频」时，要的是**原声被去掉**。

    两条路径：
      * keep_original=False（默认）-> **只留新音轨**，原声彻底丢弃；
      * keep_original=True  -> 原声作为第一路与新音轨**并排混合**
        （用 amerge 而不是 amix：两路独立，不互相压低）。

    实现要点：
      * 音频短于视频 -> ``apad`` 补静音再 ``-shortest`` 截到视频长度；
      * 音频长于视频 -> ``-shortest`` 截到视频长度；
      * 视频流 ``-c:v copy``（不重编码画面）；
      * 产物入库，可被后续工具引用。
    """
    video_path, video_entry = _resolve_asset_path(video_asset_id)
    audio_path, audio_entry = _resolve_asset_path(audio_asset_id)
    video_info = await _probe(video_path)
    audio_info = await _probe(audio_path)
    if not video_info["has_video"]:
        raise MediaToolError(f"素材不是视频：{video_entry['name']}",
                             code="not_a_video")
    if not audio_info["has_audio"]:
        raise MediaToolError(f"素材没有音轨：{audio_entry['name']}",
                             code="not_an_audio")
    try:
        gain = max(0.0, min(4.0, float(volume)))
    except (TypeError, ValueError):
        gain = DEFAULT_MIX_VOLUME
    with tempfile.TemporaryDirectory(prefix="h3agent-replace-") as tmp:
        out_path = Path(tmp) / "replaced.mp4"
        cmd = [_ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
               "-i", str(video_path), "-i", str(audio_path)]
        chains: list[str] = []
        if keep_original and video_info["has_audio"]:
            # 两路**并排**（amerge）而不是混合 —— 原声与新音轨各自独立，
            # 不会互相压低。
            #
            # 【为什么两路都 apad】本机 ffmpeg 7.1.1 的 amerge **没有**
            # duration 选项（只有 inputs）—— 实测报
            # 「Error applying option 'duration' to filter 'amerge'」。
            # amerge 要求两路**等长**，短的那路会被截断 ——
            # 所以两路都先 apad 补静音到足够长，再由外层
            # ``-shortest`` 统一截到视频长度。
            # 【为什么先降为单声道】amerge 是**按声道**并排的：
            # 两路都是立体声 -> 出来是 4 声道（原声 L/R + 新音轨 L/R），
            # 播放时两个声道对位相消，听感是「相位抵消」的怪异效果。
            # 正确做法：两路各取**单声道**（pan 取中间），
            # 再 amerge -> 2 声道（左=原声，右=新音轨），
            # 立体声播放时两个声源各占一边，可听且不抵消。
            chains.append(
                f"[0:a]aresample={MIX_SAMPLE_RATE},"
                f"aformat=sample_fmts=fltp:channel_layouts=mono,"
                f"apad[orig]")
            chains.append(
                f"[1:a]aresample={MIX_SAMPLE_RATE},"
                f"aformat=sample_fmts=fltp:channel_layouts=mono,"
                f"volume={gain:.4f},apad[new]")
            chains.append("[orig][new]amerge=inputs=2[aout]")
        else:
            chains.append(
                f"[1:a]aresample={MIX_SAMPLE_RATE},"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"volume={gain:.4f},apad[aout]")
        cmd.extend(["-filter_complex", ";".join(chains)])
        cmd.extend(["-map", "0:v:0", "-map", "[aout]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", MIX_AUDIO_BITRATE,
                    "-shortest", "-movflags", "+faststart", str(out_path)])
        code, err = await _run_ffmpeg(cmd, timeout)
        if code != 0 or not out_path.is_file():
            raise MediaToolError(f"替换音轨失败：{err[:300]}",
                                 code="replace_audio_failed")
        final = await _probe(out_path)
        name = _safe_output_name(f"{video_entry['name']}_replaced", ".mp4")
        registered = _register_output(out_path, name, "video")
        return {
            "operation": "replace_audio",
            "video_input": video_entry["asset_id"],
            "audio_input": audio_entry["asset_id"],
            "keep_original": bool(keep_original),
            "volume": gain,
            "output": registered,
            "duration_sec": final["duration"],
        }


# ------------------------------------------------------------------ ffmpeg 路径

_FFMPEG_BIN = ""


def configure(ffmpeg_path: str) -> None:
    """设置本进程使用的 ffmpeg 路径（由 agent_settings 注入）。"""
    global _FFMPEG_BIN
    _FFMPEG_BIN = str(ffmpeg_path or "")


def _ffmpeg_bin() -> str:
    if _FFMPEG_BIN:
        return _FFMPEG_BIN
    found = shutil.which("ffmpeg")
    if found:
        return found
    fallback = Path("/usr/bin/ffmpeg")
    if fallback.exists():
        return str(fallback)
    raise MediaToolError("本机找不到 ffmpeg（可在「agent 管理」里填写 ffmpeg 路径）",
                         code="ffmpeg_missing", status=503)


# ------------------------------------------------------------------ 文件读取


def read_text_file(path: str, roots: list[str], max_bytes: int = 200_000,
                   ) -> dict:
    """
    读一个文本文件（**路径白名单**内）。

    安全边界：
      * 路径必须在 roots 之内（roots 为空 = 不允许任何路径）；
      * 文件大小上限；
      * 二进制内容不返回（按可打印字符比例判定）。
    """
    text = str(path or "").strip()
    if not text:
        raise MediaToolError("read_file 需要 path 参数")
    if not roots:
        raise MediaToolError(
            "文件读取能力已启用但没有配置允许读取的目录"
            "（请在「agent 管理」里填写 file_roots）",
            code="file_roots_empty", status=403)
    candidate = Path(text).expanduser()
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise MediaToolError(f"路径无法解析：{text}",
                             code="path_invalid") from exc
    allowed = None
    for root in roots:
        try:
            root_path = Path(root).resolve()
            resolved.relative_to(root_path)
            allowed = root_path
            break
        except (ValueError, OSError):
            continue
    if allowed is None:
        raise MediaToolError(
            f"路径不在允许读取的目录内：{text}"
            f"（允许：{'、'.join(roots)}）",
            code="path_forbidden", status=403)
    if not resolved.is_file():
        raise MediaToolError(f"文件不存在：{text}", code="file_not_found",
                             status=404)
    size = resolved.stat().st_size
    if size > max_bytes:
        raise MediaToolError(
            f"文件过大（{size} 字节，上限 {max_bytes} 字节）",
            code="file_too_large", status=413)
    try:
        content = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raw = resolved.read_bytes()[:4096]
        printable = sum(1 for b in raw if 32 <= b < 127 or b in (9, 10, 13))
        if raw and printable / len(raw) < 0.85:
            raise MediaToolError("这是二进制文件，不是文本", code="not_text")
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise MediaToolError(f"读取失败：{exc}", code="read_failed") from exc
    return {
        "path": str(resolved),
        "bytes": size,
        "lines": content.count("\n") + 1,
        "content": content,
    }
