#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异测试 M1：关掉 retry_job 的指纹刷新 -> 断言必须变红。"""
import shutil
from pathlib import Path

REPO = Path("/root/projects/h3s-r4-20261119")
TARGET = REPO / "h3studio" / "agent_tools.py"
BACKUP = Path("/tmp/r3_at_backup.py")

shutil.copy2(TARGET, BACKUP)
src = TARGET.read_text(encoding="utf-8")
old = """        refreshed = getattr(server, "refresh_fingerprinted_path", None)
        if refreshed is not None:"""
new = """        refreshed = getattr(server, "refresh_fingerprinted_path", None)
        if False and refreshed is not None:"""
assert old in src, "pattern not found"
TARGET.write_text(src.replace(old, new), encoding="utf-8")
print("MUTATED M1: retry_job 的指纹刷新被关闭")
