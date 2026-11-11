# -*- coding: utf-8 -*-
"""
生成一张**真实可解码**的小 PNG（验收脚本用）。

【为什么不用 base64 字面量 —— 2026-11-12】
第一版把 PNG 的 base64 直接写进脚本，结果字符串被截断（长度不是 4 的倍数），
base64.b64decode 直接抛 binascii.Error —— 验收脚本在 seed 阶段就崩了。
手写 base64 字面量对「能不能解码」零保证。

【现在的做法】
用 struct + zlib **现算**一个 PNG（IHDR/IDAT/IEND 三块 + CRC）。
好处：
  * 一定是合法 PNG（浏览器能解码，lightbox 能显示真实像素）；
  * 像素尺寸可控（能给 lightbox 一个非零 naturalWidth/Height，
    验证「按实际像素自适应」这条路真的通）；
  * 不依赖任何外部资源，也不受 base64 字面量抄错的影响。
"""
from __future__ import annotations

import binascii
import struct
import zlib


def make_png(width: int = 64, height: int = 36,
             rgb: tuple[int, int, int] = (90, 120, 220)) -> bytes:
    """生成一张纯色 PNG 字节串。"""
    width = max(1, int(width))
    height = max(1, int(height))
    # 每行前面要加一个 filter 字节（0 = None）
    raw = b""
    for _ in range(height):
        raw += b"\x00" + bytes(rgb) * width

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", binascii.crc32(body) & 0xffffffff))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (sig + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))
