#!/usr/bin/env python3
"""变异自证：证明 tests/test_image_param_ranges.py 的断言**真的能抓住回归**。

【为什么需要这个文件】
第 5 轮审查里踩过的坑：「断言看着对，但把被测代码改坏它照样全绿」。
本文件把每个修复点**故意改坏**一次（mutation），确认对应测试**变红**。

【纪律】
  * 每个 mutation 必须让**至少一条**断言变红；
  * mutation 必须在**副本**上做，绝不碰真实工作区；
  * 跑完必须还原（git checkout）。

运行：python3 scripts/mutation_image_param_ranges.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = "/home/ubuntu/comfyui/venv/bin/python"
TEST = "tests/test_image_param_ranges.py"


def run_tests(root: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [PY, "-m", "pytest", TEST, "-q", "-p", "no:cacheprovider", "--tb=no"],
        cwd=root, capture_output=True, text=True, timeout=300,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "PYTHONPATH": str(root),
             "HOME": "/root"},
    )
    out = proc.stdout + proc.stderr
    m = re.search(r"(\d+) failed", out)
    failed = int(m.group(1)) if m else 0
    return failed, out


#: (名称, 文件, 原文, 替换文, 期望变红的测试名片段)
MUTATIONS = [
    (
        "M1 把 MOSS 的步数上限改回 60（还原本次 BUG 的根因）",
        "h3studio/gen_normalize.py",
        '"moss": {\n        "steps": {"min": 1, "max": 100, "step": 1},',
        '"moss": {\n        "steps": {"min": 1, "max": 60, "step": 1},',
        "test_moss_steps_100_is_accepted",
    ),
    (
        "M2 把 Qwen 的 shift 下限改回 1.0（还原 shift=0.69 被夹的缺陷）",
        "h3studio/gen_normalize.py",
        '"shift": {"min": 0.0, "max": 3.0, "step": 0.01},',
        '"shift": {"min": 1.0, "max": 3.0, "step": 0.01},',
        "test_qwen_shift_069_default_is_not_flagged",
    ),
    (
        "M3 归一层忽略 family（所有族都用 Z-Image 量程）",
        "h3studio/gen_normalize.py",
        "    ranges = param_ranges_for_family(str(family))",
        "    ranges = param_ranges_for_family('zimage')",
        "test_moss_steps_100_is_accepted",
    ),
    (
        "M4 放大路径第一段写死 steps=8（还原 Z-Image 放大分支的缺陷）",
        "h3studio/workflows.py",
        '"57:3": {"inputs": {"seed": int(seed), "steps": eff_steps, "cfg": eff_cfg,',
        '"57:3": {"inputs": {"seed": int(seed), "steps": 8, "cfg": 1.0,',
        "test_upscale_first_stage_uses_user_steps",
    ),
    (
        "M5 放大路径第一段写死 cfg=1.0",
        "h3studio/workflows.py",
        '"57:3": {"inputs": {"seed": int(seed), "steps": eff_steps, "cfg": eff_cfg,',
        '"57:3": {"inputs": {"seed": int(seed), "steps": eff_steps, "cfg": 1.0,',
        "test_upscale_first_stage_uses_user_cfg",
    ),
    (
        "M6 前端重新硬编码量程（还原前端那半边的根因）",
        "web/app.js",
        "  const ranges = imgParamRanges();",
        '  const ranges = {steps:{min:1,max:60,step:1},'
        'cfg:{min:1.0,max:8.0,step:0.5},shift:{min:1.0,max:7.0,step:0.5}};',
        "test_frontend_no_longer_hardcodes_ranges",
    ),
    (
        "M6b 前端自己维护一份兜底量程表（两份量程必然漂移）",
        "web/app.js",
        "function imgParamRanges() {\n  const family = imgParamFamily();",
        "function imgParamRanges() {\n  return {steps:{min:1,max:60,step:1},"
        "cfg:{min:1.0,max:8.0,step:0.5},shift:{min:1.0,max:7.0,step:0.5}};\n"
        "  const family = imgParamFamily();",
        "test_frontend_no_longer_hardcodes_ranges",
    ),
    (
        "M7 gen_params 不再下发 normalize_notes（归一说明退回静默）",
        "h3studio/job_meta.py",
        '"normalize_notes": list(params.get("normalize_notes") or []),',
        '"normalize_notes": [],',
        "test_gen_params_includes_normalize_notes",
    ),
    (
        "M8 前端详情不再显示「参数已调整」",
        "web/app.js",
        'rows.push(`<div class="detail-kv-row"><span>参数已调整</span>`',
        'rows.push(`<div class="detail-kv-row"><span>参数说明</span>`',
        "test_frontend_detail_modal_renders_normalize_notes",
    ),
    (
        "M9 模型表族标记漏掉（param_family 不再打）",
        "h3studio/workflows.py",
        '    _m.setdefault("param_family", "moss")',
        "    pass",
        "test_model_tables_are_tagged_with_family",
    ),
]


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mutation-imgparams-"))
    shutil.copytree(ROOT, workdir / "repo",
                    ignore=shutil.ignore_patterns(".git", "__pycache__",
                                                 "*.pyc", "data", "outputs"))
    repo = workdir / "repo"

    print("=== 基线（未变异）===")
    failed, out = run_tests(repo)
    if failed:
        print("基线就有 %d 条失败 —— 先修基线再做变异" % failed)
        print(out[-3000:])
        return 1
    print("基线全绿（0 failed）")

    problems: list[str] = []
    for name, rel, old, new, expect in MUTATIONS:
        path = repo / rel
        original = path.read_text(encoding="utf-8")
        if old not in original:
            problems.append("!! %s：锚点没找到（%s）" % (name, rel))
            continue
        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            failed, out = run_tests(repo)
        finally:
            path.write_text(original, encoding="utf-8")
        status = "OK  抓住了" if failed > 0 else "!! 没抓住"
        print("%-4s %-58s failed=%d" % (status, name, failed))
        if failed == 0:
            problems.append("!! %s：变异后测试仍全绿 —— 断言抓不住这个回归" % name)
        else:
            m = re.search(r"FAILED [^ ]+::(\w+)", out)
            if m and expect not in out:
                problems.append("!! %s：红了，但红的不是预期的 %s" % (name, expect))

    shutil.rmtree(workdir, ignore_errors=True)

    print()
    if problems:
        print("=== 变异自证失败 ===")
        for p in problems:
            print(p)
        return 1
    print("=== 全部 %d 个变异都被抓住 ===" % len(MUTATIONS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
