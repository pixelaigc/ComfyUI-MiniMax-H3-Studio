# -*- coding: utf-8 -*-
"""变异测试：证明「接续时长缺失 + 跨段提示词增强」的新断言真能抓住回归。

【为什么需要这个文件】
新写的测试如果**抓不住**对应的缺陷，那它就是装饰品。
本文件逐个把修复**回退成缺陷版**，然后断言对应测试**必须失败**。
若某个变异后测试仍然通过 -> 断言无效 -> 必须改断言（不是改实现）。

【硬纪律】
  * 变异必须在**临时副本**上做，绝不动工作区源码。
  * 每次变异后必须还原。
  * 不弱化任何断言。

运行：python3 tests/mutation_cont_duration_prompt_history.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEST_FILE = "tests/test_cont_duration_and_prompt_history.py"

#: (变异名, 目标文件, 待替换的原文, 替换后的缺陷版)
#:
#: 【硬纪律：变异后必须仍是合法 Python / JS】
#: 语法错误会让 pytest 在**收集阶段**就中断 —— 那不是「断言抓住缺陷」，
#: 而是「测试根本没跑」。所以每个变异都保持语法合法，
#: 只把行为改回缺陷形态。
MUTATIONS = [
    # ---- 变异①：自愈路径不跑接续收尾（原始缺陷形态）----
    (
        "自愈收尾不调用接续收尾",
        "server.py",
        "        saved = await _finalize_continuation_impl(job_id, mode, params, saved)\n"
        "    except Exception as ce:\n"
        "        # 拼接失败**不影响**本段产物 —— 与正常收尾路径同口径。\n"
        "        print(f\"[自愈] 任务 {job_id} 接续收尾失败（不影响本段产物）: {ce}\")\n",
        "        pass  # 变异：缺陷版（自愈路径不跑接续收尾）\n"
        "    except Exception as ce:\n"
        "        print(f\"[自愈] 任务 {job_id} 接续收尾失败（不影响本段产物）: {ce}\")\n",
        "test_finalize_residual_calls_continuation_finalize",
    ),
    # ---- 变异②：接管路径不跑接续收尾 ----
    (
        "接管路径不调用接续收尾",
        "server.py",
        "                        saved = await _finalize_continuation_impl(\n"
        "                            job_id, mode, params, saved)\n"
        "                    except Exception as ce:\n"
        "                        print(f\"[自愈] 任务 {job_id} 接续收尾失败\"\n"
        "                              f\"（不影响本段产物）: {ce}\")\n",
        "                        pass  # 变异：缺陷版\n"
        "                    except Exception as ce:\n"
        "                        print(f\"[自愈] 任务 {job_id} 接续收尾失败\"\n"
        "                              f\"（不影响本段产物）: {ce}\")\n",
        "test_adopt_residual_calls_continuation_finalize",
    ),
    # ---- 变异③：Worker 不委托给模块级实现 ----
    (
        "Worker 不委托模块级实现",
        "server.py",
        "        return await _finalize_continuation_impl(job_id, mode, params, saved)\n",
        "        return await _finalize_continuation_impl_DISABLED(job_id, mode, params, saved)\n",
        "test_worker_finalize_delegates_to_module_impl",
    ),
    # ---- 变异④：血缘提示词不回溯（只返回空）----
    (
        "血缘提示词不回溯",
        "server.py",
        "    out: list[dict] = []\n"
        "    seen: set[str] = set()\n"
        "    current = job_id\n",
        "    out: list[dict] = []\n"
        "    return out  # 变异：缺陷版（不回溯血缘）\n"
        "    seen: set[str] = set()\n"
        "    current = job_id\n",
        "test_lineage_prompts_walks_source_job_chain",
    ),
    # ---- 变异⑤：历史不进模型消息 ----
    (
        "历史不进模型消息",
        "h3studio/enhancer.py",
        "    if history:\n        lines = []\n",
        "    if False:  # 变异：缺陷版（历史不注入）\n        lines = []\n",
        "test_build_messages_includes_history",
    ),
    # ---- 变异⑥：不剔除本段 ----
    (
        "不剔除本段自己",
        "server.py",
        "        if explicit_stage and stage == current_stage:\n            continue\n",
        "        if False:  # 变异：缺陷版（本段没被剔除）\n            continue\n",
        "test_normalize_history_drops_current_stage",
    ),
    # ---- 变异⑦：历史排在当前段之后 ----
    #
    # 【为什么这样变异】原实现是「历史拼在 user_text **前面**」。
    # 缺陷版把历史拼到 **后面** —— 这才是真正的行为回归。
    # （只改拼接语序、不移动位置，断言抓不住 —— 那不是缺陷。）
    (
        "历史排在当前段之后",
        "h3studio/enhancer.py",
        "            user_text = (\n"
        "                \"以下是同一条视频**此前各段**的提示词（用于保持跨段一致性，\"\n"
        "                \"人物/场景/风格/剧情要素必须延续）：\\n\"\n"
        "                + \"\\n\".join(lines)\n"
        "                + \"\\n\\n现在请基于以上全部段落内容，增强**下面这一段**的提示词。\"\n"
        "                + \"\\n\\n【当前段原文】\\n\" + user_text\n"
        "            )\n",
        "            user_text = (\n"
        "                user_text\n"
        "                + \"\\n\\n【当前段原文】\\n\" + user_text\n"
        "                + \"\\n\\n以下为此前各段：\\n\" + \"\\n\".join(lines) + \"\\n\"\n"
        "            )\n",
        "test_history_comes_before_current_prompt",
    ),
    # ---- 变异⑧：端点不传历史 ----
    (
        "端点不传历史",
        "server.py",
        "    messages = enh.build_messages(spec, original, params.get(\"language\", \"auto\"),\n"
        "                                   history=history)\n",
        "    messages = enh.build_messages(spec, original, params.get(\"language\", \"auto\"))\n",
        "test_enhance_endpoint_passes_history_to_build_messages",
    ),
    # ---- 变异⑨：前端不透传历史 ----
    #
    # 【为什么要 replace_all】视频面板有**两处**增强入口会传历史
    # （普通增强 + 视频导演稿）。只变异一处 -> 另一处还在传 ->
    # 断言（要求 >= 2 处）仍然通过 -> 变异被漏掉。
    # 所以这里两处都要变异。
    (
        "前端不透传历史",
        "web/enhance-menu.js",
        "        prompt_history: continuationPromptHistory(),\n",
        "        prompt_history: [],  // 变异：缺陷版（不透传历史）\n",
        "test_enhance_menu_passes_prompt_history",
        True,   # replace_all
    ),
]


def run_tests(repo: Path) -> tuple[int, int, str]:
    """跑目标测试文件，返回 (退出码, 失败条数, 输出尾部)。

    【为什么不能只看退出码】pytest 在「收集阶段出错」时也返回非 0，
    但那时**并没有**断言失败（例如 import 崩了）。变异测试要确认的
    是「**断言**抓不住」—— 所以必须读 pytest 摘要行里的 failed 计数。

    【为什么先做语法体检】变异的 JS 若语法坏了，浏览器端测试会因为
    「脚本没加载」而失败 —— 那不是「断言抓住缺陷」。
    所以变异后先 `node --check`，语法坏就直接判「变异无效」。
    """
    for rel in [m[1] for m in MUTATIONS]:
        if rel.endswith(".js"):
            node = subprocess.run(["node", "--check", str(repo / rel)],
                                  capture_output=True, text=True)
            if node.returncode != 0:
                return 99, 0, ("变异后 JS 语法非法（变异本身无效）：\n"
                               + node.stderr[:600])
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST_FILE, "-q", "-p", "no:randomly"],
        cwd=str(repo), capture_output=True, text=True, timeout=300)
    out = (proc.stdout or "") + (proc.stderr or "")
    tail = "\n".join(out.strip().splitlines()[-14:])
    return proc.returncode, count_failures(out), tail


def count_failures(output: str) -> int:
    """从 pytest 输出里读**断言失败**条数（摘要行「N failed」）。

    【为什么不用退出码】退出码非 0 也可能是收集/导入错误 ——
    那不是「断言抓住」。只认摘要行里的 failed 计数。
    """
    import re
    # 摘要行形如「1 failed, 18 passed in 0.10s」/「5 failed, 14 passed」
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if re.search(r"\d+\s+failed", line):
            m = re.search(r"(\d+)\s+failed", line)
            if m:
                return int(m.group(1))
    return 0


def main() -> int:
    print("=" * 72)
    print("变异测试：接续时长缺失 + 跨段提示词增强")
    print("=" * 72)

    tmp = Path(tempfile.mkdtemp(prefix="h3mut_cont_"))
    repo = tmp / "repo"
    # 只拷必要文件（避免拷整个仓库的巨型输出目录）
    repo.mkdir(parents=True)
    for rel in ["server.py", "h3studio", "web", "tests"]:
        src = REPO / rel
        if src.is_dir():
            shutil.copytree(src, repo / rel,
                            ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(src, repo / rel)
    # 需要 config.json 等运行期文件
    for name in ["config.json"]:
        if (REPO / name).is_file():
            shutil.copy2(REPO / name, repo / name)

    rc, failed, tail = run_tests(repo)
    if rc != 0 or failed != 0:
        print("❌ 基线副本本身就不绿，无法做变异测试：")
        print(tail)
        return 1
    print("✅ 变异前：目标测试全绿")
    print()

    failures = []
    for mutation in MUTATIONS:
        name, rel, old, new, expect_test = mutation[:5]
        replace_all = bool(mutation[5]) if len(mutation) > 5 else False
        target = repo / rel
        original = target.read_text(encoding="utf-8")
        if old not in original:
            print(f"❌ [{name}] 变异锚点没找到（{rel}）—— 变异脚本与实现脱节")
            failures.append((name, expect_test, "锚点缺失"))
            continue
        if replace_all:
            if original.count(old) < 2:
                print(f"❌ [{name}] 锚点只出现 {original.count(old)} 次，"
                      f"需要 >= 2 次才能做 replace_all 变异")
                failures.append((name, expect_test, "锚点次数不足"))
                continue
            target.write_text(original.replace(old, new), encoding="utf-8")
        else:
            target.write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            rc, failed, tail = run_tests(repo)
            if failed == 0:
                print(f"❌ [{name}] 变异后断言仍然全绿 —— 断言抓不住这个回归")
                failures.append((name, expect_test, "变异后仍全绿"))
            else:
                print(f"✅ [{name}] 变异后 {failed} 条断言失败"
                      f"（断言抓住了）")
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
