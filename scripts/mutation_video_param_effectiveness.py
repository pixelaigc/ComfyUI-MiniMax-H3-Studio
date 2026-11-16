#!/usr/bin/env python3
"""变异自证：证明 tests/test_video_param_effectiveness.py 的断言**真能抓住回归**。

【为什么需要这个文件】
第 5 轮审查踩过的坑：「断言看着对，但把被测代码改坏它照样全绿」。
本文件把每个视频/音乐修复点**故意改坏**一次（mutation），
确认对应断言**变红**。

【纪律】
  * 每个 mutation 必须让**至少一条**断言变红；
  * mutation 在**副本**上做，绝不碰真实工作区；
  * 跑完必须还原。

运行：python3 scripts/mutation_video_param_effectiveness.py
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
TEST = "tests/test_video_param_effectiveness.py"


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
        "M1 载入守卫改回读顶层 saved.steps（还原 D-1 根因）",
        "web/video-panel.js",
        "          const savedSteps = savedModeCfg.steps != null\n"
        "            ? savedModeCfg.steps : saved.steps;\n"
        "          if (savedSteps == null) recomputeSamplingForAccel();",
        "          if (saved.steps == null) recomputeSamplingForAccel();",
        "test_load_guard_reads_mode_scoped_steps",
    ),
    (
        "M2 applyConfig 改回「mode_configs 优先并 return」（还原 D-2 根因）",
        "web/video-panel.js",
        "    const merged = {};\n"
        "    if (modeCfg && typeof modeCfg === \"object\") merged = { ...modeCfg };\n"
        "    // 顶层字段覆盖 mode_configs 的片段（顶层 = 调用方的显式意图）\n"
        "    for (const key of Object.keys(cfg)) {\n"
        "      if (key === \"mode_configs\") continue;\n"
        "      merged[key] = cfg[key];\n"
        "    }\n"
        "    applyModeConfig(merged);",
        "    if (modeCfg && typeof modeCfg === \"object\") {\n"
        "      applyModeConfig(modeCfg);\n"
        "      return;\n"
        "    }\n"
        "    applyModeConfig(cfg);",
        "test_apply_config_top_level_overrides_mode_configs",
    ),
    (
        "M3 二采时不再置灰主采样器下拉（还原 D-3 根因）",
        "web/video-panel.js",
        "      mainSampler.disabled = twopassOn;",
        "      mainSampler.disabled = false;",
        "test_render_twopass_disables_main_sampler",
    ),
    (
        "M4 shift 可用性改回读 sampling 子对象（还原 D-6 根因）",
        "web/video-panel.js",
        "    const shiftAvailable = (VIDEO_OPTIONS.sigma_shift_available !== undefined\n"
        "      ? VIDEO_OPTIONS.sigma_shift_available\n"
        "      : cfg.sigma_shift_available) !== false;",
        "    const shiftAvailable = cfg.sigma_shift_available !== false;",
        "test_frontend_reads_top_level_availability",
    ),
    (
        "M5 音乐温度改回 falsy 判空（还原 D-7 根因）",
        "web/music.js",
        "      if (st.temperature == null) st.temperature = ms.temperature;",
        "      if (!st.temperature) st.temperature = ms.temperature;",
        "test_music_js_does_not_use_falsy_check_for_temperature",
    ),
    (
        "M6 视频族归一不再记说明（还原 D-8 根因）",
        "server.py",
        "    _video_note(out, \"steps\", params.get(\"steps\"), steps)\n"
        "    _video_note(out, \"sampler\", params.get(\"sampler\"), out[\"sampler\"])\n"
        "    _video_note(out, \"scheduler\", params.get(\"scheduler\"), out[\"scheduler\"])",
        "    pass",
        "test_out_of_range_steps_is_noted",
    ),
    (
        "M7 transition 夹回不再留痕（还原 D-4 根因）",
        "server.py",
        "            _video_note(out, \"twopass.transition_step\", transition,\n"
        "                        max(1, min(transition, max_transition)),\n"
        "                        \"（上限受当前步数档的 sigma 表长度限制）\")",
        "            pass",
        "test_twopass_transition_clamp_is_noted",
    ),
    (
        "M8 rho 改写不再留痕（还原 D-5 根因）",
        "server.py",
        "            _video_note(out, \"twopass.rho\", rho, 0.6,\n"
        "                        \"（ρ=0 且不使用放大器会被 SelfLift 节点拒绝）\")",
        "            pass",
        "test_twopass_rho_rewrite_is_noted",
    ),
    (
        "M9 合法值也记说明（违反 R1 —— 不得多记）",
        "server.py",
        "    # 值没变 -> 不是一次「改动」，不记\n"
        "    try:\n"
        "        if isinstance(raw, (int, float)) and isinstance(effective, (int, float)):\n"
        "            if float(raw) == float(effective):\n"
        "                return\n"
        "        elif str(raw).strip() == str(effective).strip():\n"
        "            return\n"
        "    except (TypeError, ValueError):\n"
        "        pass",
        "    # 值没变 -> 也要记（变异：破坏「只在真改了值时才记」的规则）\n"
        "    pass",
        "test_legal_values_leave_no_notes",
    ),
]


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mutation-videoparams-"))
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
        elif expect not in out:
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
