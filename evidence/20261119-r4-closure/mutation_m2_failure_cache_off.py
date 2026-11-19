#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异测试 M2：关掉「失败结果缓存」-> 断言必须变红。"""
import shutil
from pathlib import Path

REPO = Path("/root/projects/h3s-r4-20261119")
TARGET = REPO / "server.py"
BACKUP = Path("/tmp/r3_server_backup.py")

shutil.copy2(TARGET, BACKUP)
src = TARGET.read_text(encoding="utf-8")
old = """        if info.get("ref_video_frame_count_probe") == "failed":
            return"""
new = """        if False and info.get("ref_video_frame_count_probe") == "failed":
            return"""
assert old in src, "pattern not found"
TARGET.write_text(src.replace(old, new), encoding="utf-8")
print("MUTATED M2: 失败标记不再生效 -> 每次都会重探")
