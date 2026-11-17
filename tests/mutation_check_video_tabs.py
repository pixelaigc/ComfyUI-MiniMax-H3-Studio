#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异测试：证明新增的断言**真的会红**（不是恒真断言）。

做法：逐个把生产代码里的关键判定改坏，跑对应测试文件，
要求**必须出现失败**。改完立即还原（不留下任何残留改动）。

【为什么要做】
本项目的教训是「断言看起来在测东西，实际上恒真」。比如：
  * 类别顺序断言如果只断言 `len(cats) == 4`，把顺序打乱它照样绿；
  * 上限断言如果只断言 `cap > 0`，把上限改回 0.98MP 它照样绿；
  * 「超限不静默截断」如果只断言 `reasons` 非空，缩放逻辑照样绿。
所以每条关键断言都要有一个「改坏它 -> 必红」的对照。

【纪律】
  * 每轮先 git stash / git checkout 还原，再改下一个；
  * 断言必须由「命名该行为的那条断言」抓住；
  * 测量点要挑**常量巧合抓不到**的位置。

运行：python3 tests/mutation_check_video_tabs.py
"""
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TEST_FILE = "tests/test_video_tabs_resolution.py"


def run_tests(test_path: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", test_path, "-q", "--no-header", "-x"],
        cwd=REPO, capture_output=True, text=True, timeout=300)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def git_restore(path: str) -> None:
    subprocess.run(["git", "checkout", "--", path], cwd=REPO, check=True,
                   capture_output=True)


#: (描述, 相对文件路径, 要替换的原文, 替换成什么, 期望失败的测试选择器)
MUTATIONS = [
    (
        "把类别顺序打乱（VIDEO_CATEGORY_ORDER 里 t2v <-> flf 互换）"
        "——顺序断言必须红。"
        "【注意】要打的是 VIDEO_CATEGORY_ORDER 这个 list（真正决定导航顺序的那个），"
        "不是 VIDEO_CATEGORY_LABELS（dict，键序无关）—— 打错地方会得到假绿。",
        "h3studio/video_categories.py",
        'VIDEO_CATEGORY_ORDER: list[str] = ["t2v", "flf", "r2v", "longfilm"]',
        'VIDEO_CATEGORY_ORDER: list[str] = ["flf", "t2v", "r2v", "longfilm"]',
        "TestCategoryOrderAndLabels::test_category_order_is_exactly_the_required_four",
    ),
    (
        "把类别文案改成含模型名（'文生视频' -> 'minimax-h3 文生视频'）"
        "——文案污染断言必须红",
        "h3studio/video_categories.py",
        '"t2v": "文生视频",',
        '"t2v": "minimax-h3 文生视频",',
        "TestCategoryOrderAndLabels::test_labels_are_not_polluted_with_model_names",
    ),
    (
        "把上限改回旧的 768*1344 —— 2.0M 上限断言必须红",
        "h3studio/video_models.py",
        "VIDEO_MAX_CANVAS_PIXELS = 2 * 1024 * 1024",
        "VIDEO_MAX_CANVAS_PIXELS = 768 * 1344",
        "TestPixelLimitAndOverLimit::test_cap_is_2_megapixels",
    ),
    (
        "把 32 倍数吸附改成 16 倍数 —— 倍率断言必须红"
        "（1920x1088 仍然会过，所以用 1921 这个常量巧合抓不到的位置）",
        "h3studio/video_models.py",
        "VIDEO_SIZE_MULTIPLE = 32",
        "VIDEO_SIZE_MULTIPLE = 16",
        "TestResolutionSnapping::test_width_and_height_are_multiples_of_32",
    ),
    (
        "把 strict_over_limit 的拒绝分支删掉（改成静默缩放）"
        "——「不静默截断」断言必须红",
        "h3studio/video_models.py",
        "            if strict_over_limit:",
        "            if False:",
        "TestPixelLimitAndOverLimit::test_over_limit_is_not_silently_truncated",
    ),
    (
        "把 flf 的上限改回 0.8M —— 「首尾帧必须与 nonebot seg 同为 2.0M」"
        "断言必须红",
        "【2026-11-17 改】这条变异原先是「把 flf 改成 2.0M」（那时 2.0M 是"
        "错误状态）。现在 flf/r2v 的正确状态就是 2.0M，所以变异方向反过来："
        "把它改回 0.8M（= 旧面板档位表那个错误上限），"
        "「TestOtherModesNotLoosened」里的新断言必须红。",
        "h3studio/video_models.py",
        '    "flf": {\n        "max_pixels": VIDEO_MAX_CANVAS_PIXELS,',
        '    "flf": {\n        "max_pixels": int(0.8 * 1024 * 1024),',
        "TestOtherModesNotLoosened::test_flf_policy_matches_nonebot_seg_two_megapixels",
    ),
    (
        "把三个模式的面积上限全部去掉（max_pixels=None = 不限）—— "
        "「2.0MP 放行但 3.5MP 仍被拦」断言必须红",
        "【为什么这条重要】只断言「1920x1088 能过」的话，把上限改成"
        "不限也能让那条绿 —— 那等于把限制从「太小」改成「没有」。"
        "这条变异证明「上限仍然是 2.0MP」这个约束真的被断言住。",
        "h3studio/video_models.py",
        "VIDEO_MAX_CANVAS_PIXELS = 2 * 1024 * 1024",
        "VIDEO_MAX_CANVAS_PIXELS = 16 * 1024 * 1024",
        "TestOtherModesNotLoosened::test_still_rejects_above_two_megapixels_per_mode",
    ),
    (
        "把类别→模型的映射表里 t2v 的 mode 改错 —— 反查自洽断言必须红",
        "h3studio/video_categories.py",
        '"mode": "t2v",\n            "media": "video",',
        '"mode": "t2v_typo",\n            "media": "video",',
        "TestCategoryModelMapping::test_model_modes_are_real_backend_modes",
    ),
    (
        "把回退规则②（首个可用模型）改成返回空 —— 回退断言必须红",
        "h3studio/video_categories.py",
        '    return items[0].get("mode", "")',
        '    return ""',
        "TestCategoryModelMapping::test_resolve_mode_falls_back_to_first_model",
    ),
    (
        "把 applyDefault 的 onSelect 触发删掉（持久化只存不切）"
        "——「持久化必须真的切过去」断言必须红",
        "web/video-nav.js",
        "    if (model && onSelect) {\n      onSelect(category, model.key, model.mode, model);\n    }",
        "    if (model && false) {\n      onSelect(category, model.key, model.mode, model);\n    }",
        "TestFrontendWiring::test_persisted_selection_is_applied_not_just_stored",
    ),
]


def main() -> int:
    print("=" * 78)
    print("变异测试：每条改动都必须让对应断言变红")
    print("=" * 78)

    # 先确认未变异时是绿的
    code, out = run_tests(TEST_FILE)
    if code != 0:
        print("!! 变异前测试就不绿，先修好：")
        print(out[-3000:])
        return 2
    print("[OK] 变异前全部通过\n")

    failures = []
    for desc, rel, old, new, selector in MUTATIONS:
        target = REPO / rel
        original = target.read_text(encoding="utf-8")
        if old not in original:
            print(f"[SKIP] {desc}\n       目标片段没找到：{old[:60]!r}")
            failures.append((desc, "PATTERN NOT FOUND"))
            continue
        # 备份 + 变异
        target.write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            code, out = run_tests(f"{TEST_FILE}::{selector}")
        finally:
            # 还原（不留下任何残留改动）
            target.write_text(original, encoding="utf-8")

        if code == 0:
            print(f"[FAIL] {desc}\n       变异后测试仍然通过 —— 断言是恒真的！")
            failures.append((desc, "STILL GREEN"))
        else:
            # 提取失败原因
            reason = ""
            for line in out.splitlines():
                if "AssertionError" in line or "assert" in line.lower() and "Error" in line:
                    reason = line.strip()[:150]
                    break
            print(f"[ OK ] {desc}\n       变异后如期变红：{reason or '(见 pytest 输出)'}")

    print()
    print("=" * 78)
    if failures:
        print(f"变异测试结果：{len(failures)} 条未达预期 ——")
        for desc, why in failures:
            print(f"  - {desc}: {why}")
        return 1
    print(f"变异测试结果：全部 {len(MUTATIONS)} 条变异均让对应断言变红 ✅")
    print("（证明这些断言不是恒真断言）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
