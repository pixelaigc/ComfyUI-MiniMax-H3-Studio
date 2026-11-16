# -*- coding: utf-8 -*-
"""测试用素材库夹具 —— 让单测能用**真的**素材 id 调 prepare_job_params。

【为什么需要它 —— 2026-11-16】

本轮把「媒体引用必须是素材库已入库素材」变成后端硬校验
（server.prepare_job_params -> asset_ref_guard.check_asset_refs）。
于是所有**调 prepare_job_params 且带 asset_refs** 的测试，
不能再用编造的 asset_id（"a" / "b" / "x"）—— 那些 id 库里根本没有，
服务端会正确地报「该媒体尚未入库素材库」。

【为什么不改成「校验时跳过不存在的 id」】那等于把校验掏空：
伪造的 / 已删除的 asset_id 就会重新被放行 —— 正是本轮要消灭的缺陷。

【所以】测试改用**临时素材库里真实入库的素材**：
  * 夹具建一个临时素材库目录（ASSET_LIBRARY_ROOT 指向它）；
  * 真的 register_asset 入库，拿到真 asset_id；
  * 用完还原环境变量 + 删临时目录。

用法：
    with temp_asset_library(image=1, video=1, audio=1) as ids:
        server.prepare_job_params(
            "flf", params, [{"asset_id": ids["image"], "slot": "first_frame",
                             "ordinal": 0}], {})
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path


def _make_image(path: Path, width: int = 32, height: int = 24) -> Path:
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), (12, 34, 56)).save(path, "PNG")
    return path


def _make_audio(path: Path, seconds: int = 1) -> Path | None:
    """用 ffmpeg 生成一段真实可解码的 mp3（没有 ffmpeg 就返回 None）。"""
    from h3studio import media_probe
    if not media_probe.FFMPEG:
        return None
    import subprocess
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [media_probe.FFMPEG, "-y", "-v", "error", "-f", "lavfi",
           "-i", f"sine=frequency=440:duration={seconds}",
           "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", str(path)]
    try:
        subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=120)
    except Exception:
        return None
    return path if path.is_file() and path.stat().st_size > 0 else None


@contextmanager
def temp_asset_library(image: int = 0, video: int = 0, audio: int = 0):
    """建一个临时素材库并真实入库若干素材。

    返回 {"image": [asset_id, ...], "video": [...], "audio": [...]}。
    某种类型建不出来（比如本机没 ffmpeg -> 建不出音频）时，
    该类型的列表为空 —— 调用方应据此跳过依赖它的断言。
    """
    tmp = Path(tempfile.mkdtemp(prefix="h3s-assetlib-fixture-"))
    root = tmp / "library"
    src = tmp / "src"
    src.mkdir(parents=True, exist_ok=True)
    old_root = os.environ.get("ASSET_LIBRARY_ROOT")
    os.environ["ASSET_LIBRARY_ROOT"] = str(root)
    try:
        from h3studio import asset_service, assets as A
        A.init_db()
        out = {"image": [], "video": [], "audio": []}
        with A.connect() as conn:
            for index in range(image):
                path = _make_image(src / f"img{index}.png")
                result = asset_service.register_asset(
                    conn, source_path=path, filename=path.name,
                    mime="image/png", category="image")
                out["image"].append(result["asset"]["id"])
            for index in range(audio):
                path = _make_audio(src / f"aud{index}.mp3")
                if path is None:
                    break
                result = asset_service.register_asset(
                    conn, source_path=path, filename=path.name,
                    mime="audio/mpeg", category="audio")
                out["audio"].append(result["asset"]["id"])
            for index in range(video):
                # 视频：库里 category=video 的条目可以用任意二进制占位 ——
                # 校验只看 category 与文件存在，不解码。
                path = src / f"vid{index}.mp4"
                path.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00")
                result = asset_service.register_asset(
                    conn, source_path=path, filename=path.name,
                    mime="video/mp4", category="video")
                out["video"].append(result["asset"]["id"])
        yield out
    finally:
        if old_root is None:
            os.environ.pop("ASSET_LIBRARY_ROOT", None)
        else:
            os.environ["ASSET_LIBRARY_ROOT"] = old_root
        shutil.rmtree(tmp, ignore_errors=True)
