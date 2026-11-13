# -*- coding: utf-8 -*-
"""变异验证：证明 tests/test_continuation_capability.py 的断言**真的能抓住 bug**。

【为什么需要这一层】
「测试全绿」本身不证明测试有用 —— 断言写错位置、变异本身是空操作，
全绿同样会发生。每个变异都要：还原 -> 改一处 -> 跑 -> 期望变红 -> 还原。

【本文件的特殊之处】
`server.py` 在本仓库是 **CRLF 行尾**。变异脚本必须按 CRLF 匹配，
否则「原文没找到」会被误当成「变异没生效」。

用法：python3 tests/mutation_check_capability.py
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET = "tests/test_continuation_capability.py"
PY = sys.executable

#: (变异名, 文件, 原文, 新文, 期望变红的断言数下限)
MUTATIONS = [
    (
        "C1 探测不到就当「节点缺失」（退回旧行为）",
        "server.py",
        "        if not probe_complete:\r\n",
        "        if False:\r\n",
        2,
    ),
    (
        "C2 探测不完整时也断言「缺节点」（三态丢失）",
        "server.py",
        "    probe_complete = _COMFY_PROBE_COMPLETE\r\n",
        "    probe_complete = bool(_COMFY_NODE_NAMES)\r\n",
        1,
    ),
    (
        "C3 探测问全标志永远为 False（什么都判成没问全）",
        "server.py",
        "    _COMFY_PROBE_COMPLETE = (not unknown) and asked >= len(_VIDEO_PANEL_NODES)\r\n",
        "    _COMFY_PROBE_COMPLETE = False\r\n",
        2,
    ),
    (
        "C4 探测失败时清空好缓存（下次就误判缺节点）",
        "server.py",
        "    if found:\r\n"
        "        # 只有真问到东西才更新缓存 —— 全挂时保留上一次的（可能还有效）\r\n"
        "        _COMFY_NODE_NAMES = found\r\n",
        "    if found or not found:\r\n"
        "        _COMFY_NODE_NAMES = found\r\n",
        1,
    ),
]


def run_tests() -> int:
    """跑目标测试，返回失败数。"""
    proc = subprocess.run(
        [PY, "-m", "pytest", TARGET, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(REPO), capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    failed = 0
    for line in out.splitlines():
        if line.startswith("FAILED"):
            failed += 1
    if proc.returncode != 0 and failed == 0:
        # 可能失败在收集阶段 —— 那也算「变红」，但要能区分
        failed = 1
        for line in out.splitlines():
            if "error" in line.lower() and "ERROR" in line:
                failed += 1
    return failed


def main() -> int:
    results = []
    all_caught = True
    for name, rel, old, new, expect_min in MUTATIONS:
        path = REPO / rel
        original = path.read_bytes()
        old_b = old.encode("utf-8")
        new_b = new.encode("utf-8")
        if old_b not in original:
            print(f"❌ 变异「{name}」定位失败：原文没找到（{rel}）")
            results.append((name, 0, expect_min, "LOCATE-FAIL"))
            all_caught = False
            continue
        try:
            path.write_bytes(original.replace(old_b, new_b, 1))
            failed = run_tests()
        finally:
            path.write_bytes(original)   # 还原（字节级，CRLF 不动）

        ok = failed >= expect_min
        all_caught = all_caught and ok
        results.append((name, failed, expect_min, "OK" if ok else "MISSED"))
        flag = "✅" if ok else "❌"
        print(f"{flag} {name}: 变红 {failed} 条（期望 >= {expect_min}）")

    print()
    print(f"合计：{len(results)} 项变异，全被抓住={all_caught}")
    return 0 if all_caught else 1


if __name__ == "__main__":
    sys.exit(main())
