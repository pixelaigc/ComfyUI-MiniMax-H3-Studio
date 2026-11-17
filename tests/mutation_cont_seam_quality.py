# -*- coding: utf-8 -*-
"""P2 接续点闪烁修复的**变异测试**：证明断言真能抓住回归。

【为什么需要】新增的测试如果只是「写完就绿」，那它可能什么都没防。
变异测试的做法：**故意把生产代码改坏**，看**命名该行为的那条断言**
会不会红。红了 = 那条断言真的在守这个行为。

每个变异必须：
  1. 改一处生产代码（最小改动）；
  2. 跑出**具体**的失败断言名；
  3. 立刻还原，确认还原后全绿。

【纪律】变异测试**不能**留在仓库里当常驻测试 —— 它会故意弄坏代码。
所以本文件是**一次性脚本**，不是 pytest 用例。

用法：python3 tests/mutation_cont_seam_quality.py
"""

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# 变异表：(说明, 文件, 原文, 替换文, 必须失败的测试名片段)
MUTATIONS = [
    (
        "把 300:post 整个节点去掉（回到修复前：不压色档阶跃）",
        "h3studio/video_workflow.py",
        '        p["300:post"] = {',
        '        p["300:post_disabled"] = {',
        "test_t2v_continuation_has_post_node",
    ),
    (
        "把 match_prev 关成 0（节点存在但不做统计匹配）",
        "h3studio/video_workflow.py",
        '                "match_prev": 0.5,',
        '                "match_prev": 0.0,',
        "test_post_match_prev_is_enabled_with_safe_guardrails",
    ),
    (
        "把 match_prev_stats_frames 从 1 改成 0（旧口径：首帧被推过 guide）",
        "h3studio/video_workflow.py",
        '                "match_prev_stats_frames": 1,\n                # 其余画质域项保持 0（不动时间轴、不引入额外处理）。',
        '                "match_prev_stats_frames": 0,\n                # 其余画质域项保持 0（不动时间轴、不引入额外处理）。',
        "test_post_stats_frames_is_one",
    ),
    (
        "把 guide 从 prev_tail（第4路）改接别的（统计匹配失去真参照）",
        "h3studio/video_workflow.py",
        '                "guide": ["300:trim", 3],',
        '                "guide": ["10", 0],',
        "test_post_takes_trimmed_images_and_prev_tail_guide",
    ),
    (
        "让音频也走 300:post（post 不动音频，音频必须直接取 trim）",
        "h3studio/video_workflow.py",
        '        video_audio_ref = ["300:trim", 1]',
        '        video_audio_ref = ["300:post", 1]',
        "test_post_does_not_touch_timeline_or_audio",
    ),
    (
        "把 flf/r2v 的 300:post 也去掉",
        "h3studio/h3_video_modes.py",
        '        p["300:post"] = {',
        '        p["300:post_disabled"] = {',
        "test_flf_continuation_has_post_node",
    ),
    (
        "把 match_prev 强度从 0.5 改成 0.9（偏离推荐档，护栏形同虚设）",
        "h3studio/video_workflow.py",
        '                "match_prev": 0.5,',
        '                "match_prev": 0.9,',
        "test_match_prev_strength_is_half",
    ),
    (
        "把拼接滤镜链的 setpts 拿掉（时间轴退���）",
        "server.py",
        'f"setsar=1,setpts=N/FRAME_RATE/TB,fps={cont.FPS},format=yuv420p")',
        'f"setsar=1,fps={cont.FPS},format=yuv420p")',
        "test_concat_filter_contains_setpts",
    ),
]

TEST_FILE = "tests/test_cont_seam_quality.py"
EXTRA_TEST_FILES = ["tests/test_cont_concat_frames.py"]


def run_tests() -> tuple[int, str]:
    """跑相关测试文件，返回 (returncode, 输出)。"""
    env = dict(os.environ)
    cmd = [sys.executable, "-m", "pytest", TEST_FILE] + EXTRA_TEST_FILES + ["-q"]
    proc = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                          timeout=600, env=env)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def run_one(test_id: str) -> tuple[int, str]:
    """只跑**一条**目标断言（不能 -x，否则别的断言先红会提前中止）。"""
    env = dict(os.environ)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", test_id, "-q"],
        cwd=str(REPO), capture_output=True, text=True, timeout=300, env=env)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _read(path: Path) -> bytes:
    """按**二进制**读文件。

    【为什么必须二进制】server.py 是 CRLF 行尾。`Path.read_text()` +
    `write_text()` 会把 CRLF 静默转成 LF —— 那是**改了一万行**，
    还原后 diff 巨大、看起来像「还原后不绿」，其实是行尾被改了。
    （2026-11-17 实测踩到：变异脚本把 server.py 的 9986 行 CRLF 全变成 LF。）
    """
    return path.read_bytes()


def _write(path: Path, data: bytes) -> None:
    """按**二进制**写文件（保持原行尾）。"""
    path.write_bytes(data)


def main() -> int:
    print("=" * 74)
    print("接续点闪烁修复 —— 变异测试（证明断言真能抓住回归）")
    print("=" * 74)

    rc, out = run_tests()
    if rc != 0:
        print("❌ 基线就不绿，先修基线：\n", out[-3000:])
        return 1
    print("\n[基线] 相关测试全绿 ✅\n")

    failures = []
    for idx, (desc, rel_path, old, new, expect_test) in enumerate(MUTATIONS, 1):
        path = REPO / rel_path
        backup = _read(path)
        try:
            old_b = old.encode("utf-8")
            new_b = new.encode("utf-8")
            if old_b not in backup:
                print(f"[{idx}] ⚠  跳过：找不到原文 → {desc}")
                failures.append((desc, "SKIPPED: 原文未找到"))
                continue
            _write(path, backup.replace(old_b, new_b, 1))
            # 只跑**目标断言**
            rc, out = run_one(f"{TEST_FILE}::{expect_test}")
            caught = rc != 0
            named = expect_test in out
            if caught and named:
                print(f"[{idx}] ✅ 变异被抓到：{desc}")
                print(f"      失败断言：{expect_test}")
            else:
                print(f"[{idx}] ❌ 变异**没被抓到**：{desc}")
                print(f"      rc={rc} 目标断言名出现={named}")
                print(out[-2000:])
                failures.append((desc, "未被目标断言抓住"))
        finally:
            _write(path, backup)
            rc2, out2 = run_tests()
            if rc2 != 0:
                print(f"      ⚠ 还原后**不绿**！变异残留：\n{out2[-1500:]}")
                failures.append((desc, "还原后不绿"))
            # 行尾守恒：还原后必须与变异前逐字节相同
            if _read(path) != backup:
                print(f"      ⚠ 还原后**行尾/内容**与变异前不一致！")
                failures.append((desc, "还原后内容不一致"))

    print("\n" + "=" * 74)
    if failures:
        print(f"❌ 变异测试失败 {len(failures)} 项：")
        for desc, why in failures:
            print(f"   - {desc}: {why}")
        return 1
    print(f"✅ 全部 {len(MUTATIONS)} 个变异都被对应断言抓住，且还原后全绿")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
