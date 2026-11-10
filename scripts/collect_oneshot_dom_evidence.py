# -*- coding: utf-8 -*-
"""
一次性路径的 DOM 取证脚本（产出 evidence/agent-enh-fix-v2/oneshot_dom_evidence.json）。

跑法：python3 scripts/collect_oneshot_dom_evidence.py <输出路径>

【为什么要有这个脚本】
测试断言的是「结果非空」「参数非空」，但**没把真实 DOM 抓下来**。
审查时无法核对「渲染出来到底长什么样」。
这个脚本跑**真实**的一次性路径（真 server + 真 Chromium + 真 HTTP），
把 DOM 快照 + 响应头 + 请求体落盘。

【它和测试的关系】
本脚本复用 tests/test_agent_oneshot_e2e.py 的同一套装置 ——
所以产出的 DOM 与测试断言的对象是**同一份**，不是另造一套。
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from test_agent_oneshot_e2e import _E2EBase   # noqa: E402


def main():
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "evidence/agent-enh-fix-v2/oneshot_dom_evidence.json")

    class _Runner(_E2EBase):
        def runTest(self):
            pass

    # 借 unittest 的装置（setUpClass 起 server + browser）
    _Runner.setUpClass()
    runner = _Runner()
    evidence = {}
    try:
        cases = [
            ("list_modes_success", runner._turns("list_modes", {})),
            ("create_job_with_args", runner._turns(
                "create_job",
                {"mode": "t2v", "prompt": "雨夜霓虹街道", "aspect": "9:16"})),
            ("failed_unknown_tool", runner._turns(
                "definitely_not_a_tool", {})),
        ]
        for name, turns in cases:
            dom, captured = runner._run_one_shot(turns)
            evidence[name] = {
                "request_bodies": captured.get("bodies"),
                "responses": captured.get("responses"),
                "dom_tool_count": len(dom.get("tools") or []),
                "dom_result_node_count": dom.get("resultNodeCount"),
                "dom_args_node_count": dom.get("argsNodeCount"),
                "dom_tools": dom.get("tools"),
            }
    finally:
        _Runner.tearDownClass()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"written: {out_path}")
    for name, data in evidence.items():
        print(f"  {name}: tools={data['dom_tool_count']} "
              f"result_nodes={data['dom_result_node_count']} "
              f"args_nodes={data['dom_args_node_count']}")
        for t in data["dom_tools"]:
            print(f"     - {t['name']!r} args={t['args'][:60]!r} "
                  f"result={t['result'][:80]!r} ok={t['ok']}")


if __name__ == "__main__":
    main()
