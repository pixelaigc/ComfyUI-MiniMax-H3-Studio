# -*- coding: utf-8 -*-
"""
生成媒体库卡片的**受限尺寸缩略图**服务。

【为什么需要 —— 实测 2026-11-21】
生成媒体库的卡片原本直接引用**原始产物**：

  * 图片：`<img src="/files/images/i-zimage-000166.png">` —— 原图
    实测 1.1MB ~ **9.5MB**（Z-Image 出图）
  * 视频：`<video src="/files/videos/v-h3-studio-000128.mp4" preload="metadata">`
    —— Chrome 会发 `Range: bytes=0-`，而 aiohttp 的 FileResponse
    对 `bytes=0-` 返回**整个文件**（Content-Range: bytes 0-2237631/2237632）

一页 12 张卡 = 实测 **8.75MB** 媒体字节（CDP `Network.dataReceived` 累计，
其中 11 个视频占 7.81MB）。用户抱怨的「卡片载入很慢」里，
这部分是**恒定成本**（不解释「有时候」，但决定「一直不算快」）。

【缩略图实测】
  图片 ~30ms / 视频 ~140ms，产物 **14~43KB**（原 1.1~9.5MB）
  ⇒ 一页 12 张卡的媒体字节从 **8.75MB 降到 0**（视频改 `preload="none"`）。

【关键设计：列表接口**不生成**缩略图】
第一版把生成放进 `GET /api/jobs`，结果**列表接口自己**被拖到
1546~2897ms（12 张卡 = 11 次 ffmpeg）—— 那就变成了
「为了媒体字节少，把列表接口搞慢」，正好是反的。

正确分工：
  * `GET /api/jobs`  —— 只算**键**（一次 `stat`，亚毫秒）-> 下发 URL。
  * `GET /api/thumb`  —— 浏览器请求时才生成（走线程，不占事件循环）。

这样列表接口保持 ~4.5ms，缩略图生成与浏览器的渲染**并行**。

【缓存】
缓存键 = sha1(相对路径 + mtime_ns + size)：
  * 源文件换了内容 -> 键变 -> 旧缩略图自动失效，不会显示上一版的图；
  * URL 里带 `k=<键>` -> 浏览器也不会拿旧缓存。

【并发】
用**每键锁**保证同一张缩略图只生成一次（否则 12 张卡会并发跑 12 次 ffmpeg）。
生成失败**不缓存** —— 下次重试。

【不做什么】
* **不删用户产物** —— 缩略图写在独立目录 `data/thumbs/`。
* **不改原文件** —— 只读。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

#: 缩略图最长边上限（像素）。卡片预览区最大约 640px 宽。
THUMB_MAX_EDGE = 640
#: 视频缩略图宽度
THUMB_WIDTH = 640
#: JPEG 质量
THUMB_QUALITY = 82

#: 支持生成缩略图的扩展名
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".m4v"}

#: 缩略图缓存根目录名（相对 data_dir）
THUMB_DIR_NAME = "thumbs"

#: 每键锁：防止同一张缩略图被并发生成多次
_KEY_LOCKS: dict[str, threading.Lock] = {}
_KEY_LOCKS_GUARD = threading.Lock()

#: 同时进行的缩略图生成数上限（ffmpeg 是 CPU 密集，不能无限并发）
_GEN_SEMAPHORE = threading.Semaphore(4)


def _key_lock(key: str) -> threading.Lock:
    with _KEY_LOCKS_GUARD:
        lock = _KEY_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _KEY_LOCKS[key] = lock
        return lock


def thumb_dir(data_dir: Path) -> Path:
    return Path(data_dir) / THUMB_DIR_NAME


def cache_key(src: Path) -> str:
    """源文件身份 -> 缓存键。

    含 mtime_ns 与 size：源文件换了内容 -> 键变 -> 旧缩略图自动失效。
    """
    try:
        st = Path(src).stat()
        raw = f"{Path(src).resolve()}|{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        raw = f"{Path(src).resolve()}|0|0"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def thumb_path(data_dir: Path, key: str) -> Path:
    """缩略图在磁盘上的位置（两级分桶，避免单目录几万个文件）。"""
    d = thumb_dir(Path(data_dir))
    sub = d / key[:2] / key[2:4]
    return sub / f"{key}.jpg"


def _make_image_thumb(src: Path, dest: Path) -> bool:
    try:
        from PIL import Image
    except ImportError:
        return False
    tmp = dest.with_suffix(".tmp.jpg")
    try:
        with Image.open(src) as im:
            im = im.convert("RGB")
            # LANCZOS 在 Pillow >= 9.1 起从 Image.LANCZOS 挪到
            # Image.Resampling.LANCZOS（Pillow 12 实测只有后者）。
            resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
            im.thumbnail((THUMB_MAX_EDGE, THUMB_MAX_EDGE), resample)
            im.save(tmp, "JPEG", quality=THUMB_QUALITY, optimize=True)
        os.replace(tmp, dest)
        return True
    except Exception:  # noqa: BLE001
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return False


def _make_video_thumb(src: Path, dest: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    tmp = dest.with_suffix(".tmp.jpg")
    cmd = [ffmpeg, "-y", "-v", "error", "-ss", "0", "-i", str(src),
           "-frames:v", "1", "-vf", f"scale={THUMB_WIDTH}:-2:flags=lanczos",
           "-q:v", "4", str(tmp)]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60)
        if not tmp.exists() or tmp.stat().st_size == 0:
            raise RuntimeError("ffmpeg 没产出")
        os.replace(tmp, dest)
        return True
    except Exception:  # noqa: BLE001
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return False


def generate_thumbnail(src: Path, data_dir: Path) -> Path | None:
    """生成（或复用）一张缩略图。失败返回 None。

    **同步函数** —— 必须在工作线程里跑（ffmpeg / PIL 都是阻塞 IO）。
    """
    src = Path(src)
    ext = src.suffix.lower()
    if ext not in IMAGE_EXTS and ext not in VIDEO_EXTS:
        return None
    if not src.is_file():
        return None
    key = cache_key(src)
    dest = thumb_path(Path(data_dir), key)
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    lock = _key_lock(key)
    with lock, _GEN_SEMAPHORE:
        # 双检：等锁期间可能已被别人生成好了
        if dest.is_file() and dest.stat().st_size > 0:
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        ok = _make_video_thumb(src, dest) if ext in VIDEO_EXTS \
            else _make_image_thumb(src, dest)
        if not ok:
            return None
        try:
            return dest
        except OSError:
            return None


async def thumbnail_for(data_dir: Path, src: Path) -> Path | None:
    """异步版 —— 生成走工作线程，不占事件循环。"""
    return await asyncio.to_thread(generate_thumbnail, src, data_dir)


def sweep_thumbs(data_dir: Path, max_age_seconds: float = 7 * 24 * 3600) -> int:
    """清理过老的缩略图（源文件已不存在 / 太久没用到）。

    返回删除的文件数。**只删缩略图缓存目录，不碰用户产物。**
    """
    root = thumb_dir(Path(data_dir))
    if not root.is_dir():
        return 0
    now = time.time()
    removed = 0
    for path in root.rglob("*.jpg"):
        try:
            if now - path.stat().st_mtime > max_age_seconds:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
