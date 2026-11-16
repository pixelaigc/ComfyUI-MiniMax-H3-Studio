#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异测试：证明 tests/test_slot_state_reset.py 的断言真的有牙齿。

【为什么必须做】第 3 轮审查报告里记过一次「测试自身有洞」：
变异脚本 needle 按 LF 写，而 web/app.js 是 CRLF —— 变异**永远匹配不上**
=> 变异被跳过 => 测试「假通过」。所以这里：
  1) needle 一律**从源码里抽取**（不写死行号）；
  2) 每次变异都**断言 needle 真的被替换了**（替换 0 处 = 失败，不许 skip）；
  3) 变异后必须**重跑测试并要求它红**；红不了 = 断言没牙齿。

用法：
    python3 scripts/mutation_slot_state_reset.py
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_JS = REPO / "web" / "app.js"
TEST = REPO / "tests" / "test_slot_state_reset.py"
NODE = shutil.which("node")

# (编号, 描述, needle 抽取器, 替换)
# needle 从源码抽取，避免行号漂移。
MUTATIONS = [
    (
        "M1-reset-assetRefs",
        "resetSlotsForMode 不再清空 state.assetRefs",
        lambda s: 'state.assetRefs = {};',
        'state.assetRefs = state.assetRefs || {};',
    ),
    (
        "M2-reset-previewUrls",
        "resetSlotsForMode 不再清空 state.slotPreviewUrls",
        lambda s: 'state.slotPreviewUrls = {};',
        'state.slotPreviewUrls = state.slotPreviewUrls || {};',
    ),
    (
        "M3-reset-audioTrack",
        "resetSlotsForMode 不再复位 state.audioTrackSource",
        lambda s: 'state.audioTrackSource = "";',
        '/* mutated: 不复位 */',
    ),
    (
        "M4-keepSlots-ignored",
        "keepSlots 参数被忽略（提交后重渲染也会清槽位）",
        lambda s: 'if (keepSlots) return;',
        '/* mutated: 忽略 keepSlots */',
    ),
    (
        "M5-files-not-deleted",
        "白名单外的 state.files key 不再删除（只置 null）",
        lambda s: 'delete state.files[key];',
        'state.files[key] = null;',
    ),
    (
        "M6-restore-no-filled",
        "restoreSlotContent 不再标记 filled class",
        lambda s: 'slot.classList.add("filled");',
        '/* mutated: 不加 filled */',
    ),
    (
        "M7-restore-no-img",
        "restoreSlotContent 不再插入缩略图 <img>",
        lambda s: 'const image = document.createElement("img");',
        'const image = {tagName: "SPAN"};',
    ),
    (
        "M8-restore-no-previewUrl",
        "restoreSlotContent 不再写 dataset.previewUrl",
        lambda s: 'if (url) slot.dataset.previewUrl = url;',
        '/* mutated: 不写 previewUrl */',
    ),
    (
        "M9-restore-early-return",
        "restoreSlotContent 遇到素材直接 return（不回填）",
        lambda s: 'if (!ref && !file) return;',
        'if (ref || file) return;\n    if (!ref && !file) return;',
    ),
    (
        "M10-whitelist-drop-first",
        "flf 白名单去掉 first 槽位",
        lambda s: 'flf: (key) => key === "first" || key === "last"',
        'flf: (key) => key === "last"',
    ),
    (
        "M11-whitelist-drop-refaud",
        "flf 白名单去掉 refaud* 槽位",
        lambda s: '|| /^refvid\\d+$/.test(key) || /^refaud\\d+$/.test(key),\n  r2v:',
        '|| /^refvid\\d+$/.test(key),\n  r2v:',
    ),
    (
        "M12-whitelist-two-copies",
        "白名单被复制成两份（渲染侧与提交侧口径漂移）",
        lambda s: 'const MODE_ALLOWED_SLOT_KEYS = {',
        'const MODE_ALLOWED_SLOT_KEYS = { flf: (k) => true, r2v: (k) => true };\nconst MODE_ALLOWED_SLOT_KEYS = {',
    ),
]


def normalize(text: str) -> str:
    """CRLF -> LF（needle 一律按 LF 匹配，避免 CRLF 坑）。"""
    return text.replace("\r\n", "\n")


def run_tests(workdir: Path) -> tuple[bool, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(workdir)
    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(workdir / "tests" / "test_slot_state_reset.py"),
         "-q", "--tb=line"],
        capture_output=True, text=True, timeout=300, cwd=str(workdir))
    return r.returncode == 0, (r.stdout + r.stderr)[-1200:]


def main() -> int:
    if not NODE:
        print("SKIP: 没有 node")
        return 0

    original = APP_JS.read_text(encoding="utf-8")
    original_norm = normalize(original)
    baseline_ok, baseline_out = run_tests(REPO)
    print(f"[baseline] tests pass = {baseline_ok}")
    if not baseline_ok:
        print(baseline_out)
        return 1

    caught = 0
    leaked = []
    for mid, desc, needle_fn, repl in MUTATIONS:
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            # 复制整个仓库（排除 .git / 大目录），保证测试能跑
            shutil.copytree(REPO, wd / "repo",
                            ignore=shutil.ignore_patterns(".git", "evidence", "docs",
                                                         "audits", "__pycache__",
                                                         "*.pyc", "node_modules"))
            target = wd / "repo" / "web" / "app.js"
            src_norm = normalize(target.read_text(encoding="utf-8"))
            needle = normalize(needle_fn(src_norm))
            if needle not in src_norm:
                print(f"[SKIP-BROKEN] {mid} {desc} —— needle 抽取不到"
                      f"（这本身就是变异脚本的 bug，不许静默跳过）")
                leaked.append(f"{mid}: needle 抽取失败")
                continue
            mutated = src_norm.replace(needle, repl, 1)
            if mutated == src_norm:
                print(f"[SKIP-BROKEN] {mid} {desc} —— 替换后内容没变")
                leaked.append(f"{mid}: 替换无效")
                continue
            target.write_text(mutated, encoding="utf-8")
            ok, out = run_tests(wd / "repo")
            if ok:
                print(f"[LEAK] {mid} {desc} —— 变异后测试仍然全绿（断言没牙齿）")
                leaked.append(f"{mid}: {desc}")
            else:
                caught += 1
                print(f"[CAUGHT] {mid} {desc}")

    print("=" * 60)
    print(f"变异总数 {len(MUTATIONS)} / 捕获 {caught} / 漏网 {len(leaked)}")
    for l in leaked:
        print("  LEAK:", l)
    return 0 if not leaked else 1


if __name__ == "__main__":
    sys.exit(main())
