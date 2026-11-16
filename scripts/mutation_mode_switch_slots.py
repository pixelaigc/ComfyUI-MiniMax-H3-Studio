#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异自证：B-2（跨模式槽位串台）修复的断言是否有牙齿。

【方法】对 app.js 注入「能让断言失败」的变异，跑被测断言，
必须看到 FAIL（断言抓到了变异）才算通过。

【纪律】
- 每轮先记录文件原始字节，改完立即用原始字节还原。
- 断言必须由「命名该行为的那条断言」抓住，不能靠别的断言顺带挂。
- 变异点要挑「常量巧合抓不到」的位置 —— 例如删掉闸门而不是把
  白名单改成另一个模式。

运行：
    python3 scripts/mutation_mode_switch_slots.py
退出码非 0 = 有变异漏网。
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEST = "tests/test_mode_switch_slot_isolation.py"
PY = sys.executable
APP = "web/app.js"

GATE_LINE = "      if (!slotKeyAllowed(key)) return;"

# (变异名, 原始片段, 变异片段, 期望失败的断言 selector)
MUTATIONS = [
    # 1) 整段删除模式闸门 —— 这是「修法被回退」的典型
    ("B2-a 删除模式闸门整行",
     GATE_LINE,
     "      // (变异) 闸门被删",
     "test_r2v_submission_excludes_flf_only_slots"),

    # 2) 闸门恒真 —— 等价于没闸门，但看起来「有代码」
    ("B2-b 闸门恒真（改成 if (true) return 之外的写法）",
     GATE_LINE,
     "      if (!slotKeyAllowed(key) && false) return;",
     "test_r2v_submission_excludes_flf_only_slots"),

    # 3) 白名单口径写反：flf 也允许 ref0..8
    ("B2-c flf 白名单误加参考图槽位",
     """      flf: (key) => key === "first" || key === "last"
        || /^refvid\\d+$/.test(key) || /^refaud\\d+$/.test(key),""",
     """      flf: (key) => key === "first" || key === "last"
        || /^ref\\d+$/.test(key)
        || /^refvid\\d+$/.test(key) || /^refaud\\d+$/.test(key),""",
     "test_r2v_then_flf_excludes_ref_image_slots"),

    # 4) 白名单口径写反：r2v 误加首尾帧
    ("B2-d r2v 白名单误加首尾帧槽位",
     """      r2v: (key) => /^ref\\d+$/.test(key)
        || /^refvid\\d+$/.test(key) || /^refaud\\d+$/.test(key),""",
     """      r2v: (key) => key === "first" || key === "last"
        || /^ref\\d+$/.test(key)
        || /^refvid\\d+$/.test(key) || /^refaud\\d+$/.test(key),""",
     "test_r2v_submission_excludes_flf_only_slots"),

    # 5) r2v 白名单整体退化成恒真（放行一切槽位）
    #    【为什么不能用 `|| (() => true)` 那条】那个变异只影响
    #    「MODE_ALLOWED_SLOT_KEYS 里没有的模式」（因为 r2v/flf 命中了
    #    表里的真值，`||` 短路，根本不会走到 fallback）——
    #    所以它在 r2v 断言下是**等价变异**，改它抓不到 r2v 的行为。
    #    要抓 r2v 白名单，必须直接改表里的 r2v 条目。
    ("B2-e r2v 白名单退化成恒真",
     """      r2v: (key) => /^ref\\d+$/.test(key)
        || /^refvid\\d+$/.test(key) || /^refaud\\d+$/.test(key),""",
     """      r2v: (key) => true,""",
     "test_r2v_submission_excludes_flf_only_slots"),

    # 6) 未知模式默认放行（应默认拒绝）—— 影响的是表里没有的模式（t2v 等）
    ("B2-f 未知模式默认放行",
     "const slotKeyAllowed = MODE_ALLOWED_SLOT_KEYS[mode] || (() => false);",
     "const slotKeyAllowed = MODE_ALLOWED_SLOT_KEYS[mode] || (() => true);",
     "test_t2v_submits_no_assets"),

    # 7) 共享槽位（参考视频/参考音乐）被误过滤 —— 会打断现有功能
    ("B2-g 共享槽位被误过滤",
     """      flf: (key) => key === "first" || key === "last"
        || /^refvid\\d+$/.test(key) || /^refaud\\d+$/.test(key),""",
     """      flf: (key) => key === "first" || key === "last",""",
     "test_shared_slots_work_in_both_modes"),

    # 8) flf 自己的首尾帧被误过滤 —— 会打断现有功能
    ("B2-h flf 首尾帧被误过滤",
     """      flf: (key) => key === "first" || key === "last"
        || /^refvid\\d+$/.test(key) || /^refaud\\d+$/.test(key),""",
     """      flf: (key) => /^refvid\\d+$/.test(key) || /^refaud\\d+$/.test(key),""",
     "test_flf_submission_still_includes_its_own_slots"),
]


def run_tests(selector: str):
    r = subprocess.run(
        [PY, "-m", "pytest", TEST, "-q", "--tb=no", "-x", "-k", selector],
        cwd=str(REPO), capture_output=True, text=True, timeout=600)
    tail = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    return r.returncode, tail


def _normalize(text: str) -> str:
    """把 CRLF 归一成 LF —— app.js 是 CRLF 文件。

    【为什么必须归一】变异脚本里的 needle 如果按 LF 写，
    在 CRLF 文件里永远匹配不上 -> 变异被 SKIP，测试「假通过」。
    这正是第一次跑出来 4 个 SKIP 的原因。
    """
    return text.replace("\r\n", "\n")


def main():
    results = []
    missed = []
    path = REPO / APP
    for name, old, new, selector in MUTATIONS:
        original = path.read_bytes()
        raw = original.decode("utf-8")
        # 归一后匹配；写回时再把 LF 还原成 CRLF，保证行尾不被改坏
        lf_text = _normalize(raw)
        old_lf = _normalize(old)
        new_lf = _normalize(new)
        if old_lf not in lf_text:
            results.append((name, "SKIP", "原始片段未找到"))
            missed.append(name)
            continue
        try:
            mutated_lf = lf_text.replace(old_lf, new_lf, 1)
            mutated = mutated_lf.replace("\n", "\r\n").encode("utf-8")
            path.write_bytes(mutated)
            rc, last = run_tests(selector)
            if rc != 0:
                results.append((name, "CAUGHT", f"{selector} 失败"))
            else:
                results.append((name, "MISSED", f"{selector} 仍然通过"))
                missed.append(name)
        finally:
            path.write_bytes(original)

    print("=" * 78)
    print("变异自证结果：B-2 跨模式槽位串台修复")
    print("=" * 78)
    for name, status, detail in results:
        mark = {"CAUGHT": "✓", "MISSED": "✗", "SKIP": "?"}[status]
        print(f"  {mark} {status:7} {name}")
        print(f"              -> {detail}")
    print("-" * 78)
    print(f"总计 {len(results)} 个变异，捕获 {sum(1 for r in results if r[1]=='CAUGHT')}，"
          f"漏网 {len(missed)}")
    if missed:
        print("漏网清单:", missed)
    rc, last = run_tests("")
    print(f"还原后回归: rc={rc} | {last}")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
