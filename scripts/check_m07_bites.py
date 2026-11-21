#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只跑 M07 变异（收藏按钮的乐观更新不同步），确认断言真的会咬人。

用法：python3 scripts/check_m07_bites.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAV = REPO / "web" / "favorites.js"
TEST = "tests/test_mobile_nav_flow.py::TestMobileNavFlow::test_favorite_button_stays_in_sync_during_optimistic_window"

OLD = """    const btn = card.querySelector('[data-act="favorite-toggle"]');
    if (btn) {"""
NEW = """    const btn = null;
    if (btn) {"""


def run():
    p = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "--tb=no", "-p", "no:cacheprovider"],
        cwd=str(REPO), capture_output=True, text=True, timeout=600)
    return p.returncode != 0, p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ""


def main():
    orig = FAV.read_text(encoding="utf-8")
    if OLD not in orig:
        print("锚点不存在")
        return 1
    bak = tempfile.NamedTemporaryFile(delete=False, suffix=".bak")
    bak.write(orig.encode("utf-8"))
    bak.close()
    try:
        FAV.write_text(orig.replace(OLD, NEW, 1), encoding="utf-8")
        failed, out = run()
        print("变异后：", "FAIL（咬到了）" if failed else "PASS（没咬到！）", "|", out)
    finally:
        shutil.copy(bak.name, FAV)
        Path(bak.name).unlink(missing_ok=True)
    failed, out = run()
    print("还原后：", "FAIL（坏了！）" if failed else "PASS", "|", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
