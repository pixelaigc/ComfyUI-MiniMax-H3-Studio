#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异测试 M3：把 ffprobe 探测放回事件循环（去掉 to_thread）-> 断言必须变红。"""
import shutil
from pathlib import Path

REPO = Path("/root/projects/h3s-r4-20261119")
TARGET = REPO / "server.py"
BACKUP = Path("/tmp/r3_server_backup_m3.py")

shutil.copy2(TARGET, BACKUP)
src = TARGET.read_text(encoding="utf-8")
old = """            frames = await asyncio.to_thread(_probe_video_frame_count, local)"""
new = """            frames = _probe_video_frame_count(local)"""
assert old in src, "pattern not found"
TARGET.write_text(src.replace(old, new), encoding="utf-8")
print("MUTATED M3: ffprobe 探测回到事件循环里（同步阻塞）")
