# -*- coding: utf-8 -*-
"""变异测试：证明「接续成片时长」的断言真能抓住回归（2026-11-17）。

【为什么需要】
新的时长断言如果**抓不住**对应的缺陷，那它就是装饰品。
本文件逐个把修复**回退成缺陷版**，断言对应测试**必须失败**。

【硬纪律】
  * 变异必须在**临时副本**上做，绝不动工作区源码。
  * 每次变异后必须还原。
  * 变异后必须**语法合法** —— 语法错会让 pytest 在收集阶段中断，
    那不是「断言抓住」（脚本会先做 node --check / ast.parse 体检）。
  * 失败计数只认 pytest 摘要行的 `failed`（退出码非 0 也可能是
    收集期 import 崩了）。

运行：python3 tests/mutation_cont_duration_semantics.py
"""

import ast
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEST_FILE = "tests/test_cont_duration_semantics.py"

#: (变异名, 目标文件, 待替换的原文, 替换后的缺陷版, 期望失败的测试)
MUTATIONS = [
    # ---- 变异①：把「请求时长」当「新增时长」直接累加 ----
    (
        "把请求时长当新增时长累加（多扣每段 1 秒）",
        "h3studio/continuation.py",
        "        delivered = fc - win if (win and not is_head) else fc\n",
        "        delivered = want  # 变异：缺陷版（按请求时长算新增）\n",
        "test_two_segments_match_real_artifact",
    ),
    # ---- 变异②：链首段也扣重叠 ----
    (
        "链首段也扣重叠",
        "h3studio/continuation.py",
        "        delivered = fc - win if (win and not is_head) else fc\n",
        "        delivered = fc - win if win else fc  # 变异：链首段也扣\n",
        "test_single_segment_delivers_full_request",
    ),
    # ---- 变异③：重叠窗口改成 24 帧（凑「整 1 秒」）----
    (
        "重叠窗口改成 24 帧（Relay-Kit 会 raise）",
        "h3studio/continuation.py",
        "    win = snap_context_frames(int(window))\n"
        "    if win <= 0:\n"
        "        win = 0\n"
        "    durations = [float(d) for d in (requested_seconds or [])]\n",
        "    win = 24  # 变异：缺陷版（24 帧不是合法窗口）\n"
        "    durations = [float(d) for d in (requested_seconds or [])]\n",
        "test_overlap_is_22_frames_not_24",
    ),
    # ---- 变异④：用户口径公式算错 ----
    (
        "用户口径公式算错",
        "h3studio/continuation.py",
        "    formula = sum(durations) - max(0, len(durations) - 1) * 1.0\n",
        "    formula = sum(durations)  # 变异：缺陷版（不减重叠）\n",
        "test_formula_follows_user_spec",
    ),
    # ---- 变异⑤：网格余量不算 ----
    (
        "网格余量不算",
        "h3studio/continuation.py",
        "    grid_slack = total_frames + overlap_frames - requested_frames\n",
        "    grid_slack = 0  # 变异：缺陷版（余量不算）\n",
        "test_delta_explains_grid_constraints",
    ),
    # ---- 变异⑥：notes 不说原因 ----
    (
        "notes 不说差值原因",
        "h3studio/continuation.py",
        "    notes: list[str] = []\n"
        "    if grid_slack:\n",
        "    notes: list[str] = []\n"
        "    if False:  # 变异：缺陷版（不生成说明）\n",
        "test_notes_mention_both_constraints",
    ),
    # ---- 变异⑦：血缘时长不按 source_job 回溯 ----
    (
        "血缘时长不回溯",
        "server.py",
        "    out: list[float] = []\n"
        "    seen: set[str] = set()\n"
        "    current = job_id\n",
        "    out: list[float] = []\n"
        "    return out  # 变异：缺陷版（不回溯）\n"
        "    seen: set[str] = set()\n"
        "    current = job_id\n",
        "test_durations_follow_source_job_chain",
    ),
    # ---- 变异⑧：链级计划不算「下一段」----
    (
        "链级计划不算下一段",
        "server.py",
        "        if extra > 0:\n            durations = durations + [extra]\n",
        "        if False:  # 变异：缺陷版（不算下一段）\n"
        "            durations = durations + [extra]\n",
        "test_plan_includes_extra_next_segment",
    ),
    # ---- 变异⑨：前端不显示时长账 ----
    (
        "前端不显示时长账",
        "web/continuation.js",
        "      ${renderDurationRows(current.duration_plan)}\n",
        "      ${'' /* 变异：缺陷版（不显示时长账） */}\n",
        "test_continuation_js_renders_duration_plan",
    ),
    # ---- 变异⑩：前端不显示「按口径」的那个数 ----
    #
    # 【为什么要整行删掉】只把 `rows.push(` 包进 `if (false)` 不够 ——
    # `formula_seconds` 这个**标识符**仍然在函数体里，断言照样过。
    # 真正的缺陷形态是「那一行根本不存在」，所以这里整行删。
    (
        "前端不显示按口径的成片时长",
        "web/continuation.js",
        "    rows.push(`<div class=\"row\"><span>成片时长（按 1 秒重叠口径）</span>`\n"
        "      + `<b>${esc(Number(plan.formula_seconds).toFixed(3))}s</b></div>`);\n",
        "",
        "test_continuation_js_shows_formula_and_actual",
    ),
    # ---- 变异⑪：取上下文不带下一段时长 ----
    (
        "取上下文不带下一段时长",
        "web/continuation.js",
        "    const query = nextSeconds > 0 ? `?duration=${nextSeconds}` : \"\";\n",
        "    const query = \"\";  // 变异：缺陷版（不带时长）\n",
        "test_fetch_passes_next_duration",
    ),
    # ---- 变异⑫：段级账的 is_head 语义反了 ----
    (
        "段级账 is_head 语义反了",
        "h3studio/continuation.py",
        "    delivered = fc - win if (win and not is_head) else fc\n",
        "    delivered = fc if (win and not is_head) else fc - win  # 变异：反了\n",
        "test_continuation_segment_trims_overlap",
    ),
]


def count_failures(output: str) -> int:
    """从 pytest 输出里读**断言失败**条数（摘要行「N failed」）。"""
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if re.search(r"\d+\s+failed", line):
            m = re.search(r"(\d+)\s+failed", line)
            if m:
                return int(m.group(1))
    return 0


def syntax_ok(repo: Path, rel: str) -> bool:
    """变异后的文件语法必须合法（否则 pytest 收集期就断了）。"""
    path = repo / rel
    if rel.endswith(".js"):
        proc = subprocess.run(["node", "--check", str(path)],
                              capture_output=True, text=True)
        return proc.returncode == 0
    try:
        ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return False
    return True


def run_tests(repo: Path) -> tuple[int, int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST_FILE, "-q", "-p", "no:randomly"],
        cwd=str(repo), capture_output=True, text=True, timeout=300)
    out = (proc.stdout or "") + (proc.stderr or "")
    tail = "\n".join(out.strip().splitlines()[-14:])
    return proc.returncode, count_failures(out), tail


def main() -> int:
    print("=" * 72)
    print("变异测试：接续成片时长语义")
    print("=" * 72)

    tmp = Path(tempfile.mkdtemp(prefix="h3mut_dur_"))
    repo = tmp / "repo"
    repo.mkdir(parents=True)
    for rel in ["server.py", "h3studio", "web", "tests", "config.json"]:
        src = REPO / rel
        if src.is_dir():
            shutil.copytree(src, repo / rel,
                            ignore=shutil.ignore_patterns("__pycache__"))
        elif src.is_file():
            shutil.copy2(src, repo / rel)

    rc, failed, tail = run_tests(repo)
    if rc != 0 or failed != 0:
        print("❌ 基线副本本身就不绿，无法做变异测试：")
        print(tail)
        return 1
    print("✅ 变异前：目标测试全绿")
    print()

    failures = []
    for name, rel, old, new, expect_test in MUTATIONS:
        target = repo / rel
        original = target.read_text(encoding="utf-8")
        if old not in original:
            print(f"❌ [{name}] 变异锚点没找到（{rel}）—— 变异脚本与实现脱节")
            failures.append((name, expect_test, "锚点缺失"))
            continue
        target.write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            if not syntax_ok(repo, rel):
                print(f"❌ [{name}] 变异后语法非法 —— 变异本身无效")
                failures.append((name, expect_test, "变异后语法非法"))
                continue
            rc, failed, tail = run_tests(repo)
            if failed == 0:
                print(f"❌ [{name}] 变异后断言仍然全绿 —— 断言抓不住这个回归")
                failures.append((name, expect_test, "变异后仍全绿"))
            else:
                print(f"✅ [{name}] 变异后 {failed} 条断言失败（断言抓住了）")
        finally:
            target.write_text(original, encoding="utf-8")

    print()
    print("=" * 72)
    if failures:
        print(f"❌ 变异测试失败：{len(failures)} 个变异没被抓住")
        for name, test, why in failures:
            print(f"   - {name} -> {test}：{why}")
        return 1
    print(f"✅ 全部 {len(MUTATIONS)} 个变异都被对应断言抓住")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
