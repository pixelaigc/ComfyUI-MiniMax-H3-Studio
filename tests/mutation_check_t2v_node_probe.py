# -*- coding: utf-8 -*-
"""变异验证：证明「节点探测三态语义」的断言**真的能抓住 bug**。

【为什么需要这一层】
「测试全绿」本身不证明测试有用 —— 断言写错位置、变异本身是空操作，
全绿同样会发生。每个变异都要：还原 -> 改一处 -> 跑 -> 期望变红 -> 还原。

【本次的语义判定（2026-11-14）】
`server.normalize_t2v_params` 对「注意力机制节点 / 二采节点是否在本机」，
采用**三态**判定：
  * 探测**完整**（_COMFY_PROBE_COMPLETE=True）+ 节点不在 -> 真缺节点 -> **拒绝**；
  * 没探测过 / 探测不完整 -> 「问不到」不等于「不存在」 -> **放行**
    （交给 ComfyUI 自己报错），否则会把「还没探测过」误判成「本机缺节点」。

判定结论：**新语义正确且更安全**。理由：
  1. 旧语义有**真实触发路径** —— 内置 Agent 不经过面板（不调 /api/video-options），
     首次进来时 `_COMFY_NODE_NAMES` 是空集 -> 旧判据报
     「本机 ComfyUI 里没有节点 ModelAttentionBackend」，而该节点实测恒在
     （curl /object_info/ModelAttentionBackend -> HTTP 200）。
  2. 新语义**没有放松**「真缺节点」的拒绝路径 —— 本脚本的 M3/M4 变异证明：
     一旦把三态退回旧语义，对应断言**立刻变红**。

【行尾注意】
`server.py` 在本仓库是 **CRLF 行尾**。变异脚本必须按 CRLF 匹配，
否则「原文没找到」会被误当成「变异没生效」。

用法：python3 tests/mutation_check_t2v_node_probe.py
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable

#: 被验证的测试文件
#:
#: 【为什么必须含 test_node_probe_completeness.py】
#: 前面两个文件里的三态断言都**手动**把 `_COMFY_PROBE_COMPLETE` 设成
#: True / False —— 它们测的是「归一层怎么用这个标志」，
#: **没有测「这个标志本身算得对不对」**。
#: 只有 test_node_probe_completeness.py 直接对着真实探测函数测标志取值，
#: M5（标志恒为 False）才有牙齿。
TARGETS = [
    "tests/test_t2v_panel.py",
    "tests/test_h3_panel_tune.py",
    "tests/test_node_probe_completeness.py",
]

#: (变异名, 文件, 原文CRLF, 新文CRLF, 期望变红的断言数下限)
MUTATIONS = [
    # ---- M1: 注意力侧退回旧语义（去掉「探测完整」前提）----
    (
        "M1 注意力侧退回旧语义（去掉 _COMFY_PROBE_COMPLETE 前提）",
        "server.py",
        "    if node and _COMFY_PROBE_COMPLETE and node not in _COMFY_NODE_NAMES:\r\n",
        "    if node and node not in _COMFY_NODE_NAMES:\r\n",
        1,
    ),
    # ---- M2: 二采侧退回旧语义 ----
    (
        "M2 二采侧退回旧语义（去掉 _COMFY_PROBE_COMPLETE 前提）",
        "server.py",
        "        if (_COMFY_PROBE_COMPLETE\r\n"
        "                and \"SelfLiftH3Sampler\" not in _COMFY_NODE_NAMES):\r\n",
        "        if \"SelfLiftH3Sampler\" not in _COMFY_NODE_NAMES:\r\n",
        1,
    ),
    # ---- M3: 注意力侧完全不校验节点（假控件：静默接受本机没有的机制）----
    (
        "M3 注意力侧完全不校验节点（静默接受本机没有的注意力机制）",
        "server.py",
        "    if node and _COMFY_PROBE_COMPLETE and node not in _COMFY_NODE_NAMES:\r\n",
        "    if False:\r\n",
        1,
    ),
    # ---- M4: 二采侧完全不校验节点 ----
    (
        "M4 二采侧完全不校验节点（静默接受本机没有的二采节点）",
        "server.py",
        "        if (_COMFY_PROBE_COMPLETE\r\n"
        "                and \"SelfLiftH3Sampler\" not in _COMFY_NODE_NAMES):\r\n",
        "        if False:\r\n",
        1,
    ),
    # ---- M5: 「探测完整」标志恒为 False -> 三态退化成「永远放行」----
    #      这会让「真缺节点」的拒绝路径彻底失效（假控件）。
    (
        "M5 探测完整标志恒为 False（三态退化成永远放行，真缺节点也放过）",
        "server.py",
        "    _COMFY_PROBE_COMPLETE = (not unknown) and asked >= len(_VIDEO_PANEL_NODES)\r\n",
        "    _COMFY_PROBE_COMPLETE = False\r\n",
        2,
    ),
]


def run_tests() -> tuple[int, str]:
    """跑目标测试，返回 (失败条数, 输出全文)。"""
    proc = subprocess.run(
        [PY, "-m", "pytest", *TARGETS, "-q", "--no-header",
         "-p", "no:cacheprovider"],
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
            if line.startswith("ERROR"):
                failed += 1
    return failed, out


def main() -> int:
    print("=== 变异验证：节点探测三态语义 ===")
    print(f"目标测试：{' '.join(TARGETS)}")
    print()

    # 先跑一次未变异，确认基线是绿的（否则「变红」没有意义）
    base_failed, base_out = run_tests()
    if base_failed:
        print("❌ 基线就不是绿的，无法做变异验证：")
        print(base_out[-3000:])
        return 1
    print("✅ 基线全绿（变异前）")
    print()

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
            failed, out = run_tests()
        finally:
            path.write_bytes(original)   # 还原（字节级，CRLF 不动）

        ok = failed >= expect_min
        all_caught = all_caught and ok
        results.append((name, failed, expect_min, "OK" if ok else "MISSED"))
        flag = "✅" if ok else "❌"
        print(f"{flag} {name}: 变红 {failed} 条（期望 >= {expect_min}）")
        if not ok:
            # 打印失败明细，方便定位断言是否写错位置
            for line in out.splitlines():
                if line.startswith("FAILED") or line.startswith("ERROR"):
                    print("      " + line)

    print()
    print("=== 汇总 ===")
    for name, failed, expect_min, status in results:
        print(f"  [{status:>11}] {name}: 变红 {failed} 条（期望 >= {expect_min}）")
    print()
    print(f"合计：{len(results)} 项变异，全被抓住={all_caught}")
    return 0 if all_caught else 1


if __name__ == "__main__":
    sys.exit(main())
