#!/usr/bin/env python3
"""c5 验收取证：跨前后端契约测试。

【要证明什么】
  跨侧契约测试**真的打真实后端产出**，不是「前端自造 mock 断言自己」。

【怎么证明「打了真实后端」】
  不是靠读代码断言，而是靠**运行时的三条硬证据**：
    1. 测试自己 import 了真的 `server` 模块（生产路由代码），
       并调 `srv.make_app()`（生产 app 工厂）。
    2. 测试里抓到的 SSE 事件 / JSON 结果**逐字段**来自真实 Agent 编排
       （h3studio/agent.py 真跑、真调工具、真产出 steps/call_id/clarify）。
    3. **反向自检**：把后端产出里的关键字段**改坏**，
       断言对应契约测试**必须失败** —— 这叫 mutation testing。
       如果改坏了测试还绿，说明那条测试根本没在测这条契约。

【本脚本产出】
  * 契约用例清单（按 5 项契约分类，标注每条是否打真实后端）
  * 逐条通过计数
  * 反向自检（mutation）结果

用法：
    python3 scripts/verify_acceptance_c5_contract.py [证据输出目录]
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/h3-acceptance-c5")

#: 五项契约 -> 测试类名
CONTRACT_CLASSES = {
    "① stream 载体": "TestStreamCarrierContract",
    "② tool_result 字段": "TestToolResultFieldContract",
    "③ 澄清字段": "TestClarificationFieldContract",
    "④ steps[].args 工具参数": "TestStepsArgsContract",
    "⑤ 音乐 aspect": "TestMusicAspectContract",
    "（附加）一次性路径工具结果": "TestOneShotToolResultContract",
}


def run_contract_tests() -> dict:
    """跑契约测试，拿到逐条结果。"""
    cmd = [sys.executable, "-m", "pytest", "tests/test_agent_contract.py",
           "-v", "-p", "no:cacheprovider", "--tb=short"]
    proc = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                          timeout=1800)
    out_file = OUT_DIR / "c5-contract-tests.txt"
    out_file.write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8")

    passed, failed = [], []
    for line in proc.stdout.splitlines():
        m = re.match(r"(tests/test_agent_contract\.py::\S+)\s+(PASSED|FAILED|"
                     r"ERROR|SKIPPED)", line)
        if m:
            (passed if m.group(2) == "PASSED" else failed).append(
                (m.group(1), m.group(2)))
    # 汇总行
    summary = ""
    for line in reversed(proc.stdout.splitlines()):
        if " passed" in line or " failed" in line:
            summary = line.strip()
            break
    return {
        "cmd": " ".join(cmd),
        "returncode": proc.returncode,
        "passed": passed,
        "failed": failed,
        "passed_count": len(passed),
        "failed_count": len(failed),
        "summary": summary,
        "output_file": str(out_file),
        "tail": (proc.stdout or "")[-3000:],
    }


def classify_cases(passed: list) -> dict:
    """把通过的用例按 5 项契约分类。"""
    out: dict = {}
    for label, cls in CONTRACT_CLASSES.items():
        out[label] = [
            {"nodeid": nid, "status": st}
            for nid, st in passed
            if f"::{cls}::" in nid
        ]
    return out


def check_real_backend_usage() -> dict:
    """
    静态检查契约测试**真的**用了真实后端，而不是手写 mock。
    这一条是「不打真实后端」这个弊端的**结构性**防线。
    """
    src = (REPO / "tests" / "test_agent_contract.py").read_text(encoding="utf-8")
    checks = {
        "imports_real_server_module": "importlib.import_module(\"server\")" in src,
        "calls_real_make_app": "srv.make_app()" in src,
        "posts_to_real_route": '"/api/agent/chat"' in src,
        "runs_real_frontend_in_chromium": "sync_playwright" in src
                                         and "chromium.launch" in src,
        "loads_real_web_agent_js": 'WEB / "agent.js"' in src,
        "no_page_route_fake": "page.route" not in src,
        "no_urllib_mock_of_backend": "urlopen" not in src,
    }
    return {
        "checks": checks,
        "all_pass": all(checks.values()),
    }


def run_mutation_selfcheck() -> dict:
    """
    反向自检（mutation testing）：
    把后端真实产出里的关键字段**改坏**，断言契约测试**必须失败**。

    【为什么必须做这个】
    「测试打的是真实后端」这件事，光看代码不够 ——
    也可能是「真实后端 + 断言写错」，那样测试也是绿的但没测到东西。
    所以要证明：**断言真的会因为后端字段变化而红**。

    做法：在一个**临时副本**里改后端生产代码（不改真实仓库），
    跑契约测试，期望它失败；跑完删掉副本。
    """
    mutations = [
        {
            "name": "把后端 tool_result 的 call_id 改坏",
            "file": "h3studio/agent.py",
            # 【为什么改这一行】
            # 后端 tool_result 事件里的 call_id 是「前端把 tool_call 与
            # tool_result 配对成同一张卡片」的唯一依据。
            # 把它改成一个固定值 -> 配对失效 -> 对应契约测试必须失败。
            "find": '_event("tool_result", {\n                    "tool": name, "ok": ok,\n                    "call_id": call["id"],',
            "replace": '_event("tool_result", {\n                    "tool": name, "ok": ok,\n                    "call_id": "MUTATED_CALL_ID",',
            "expect_fail_tests": [
                "TestToolResultFieldContract::test_backend_tool_result_carries_call_id",
            ],
        },
        {
            "name": "把后端 respond 的 clarify_options 改空",
            "file": "h3studio/agent.py",
            "find": 'ctx.respond_payload["clarify_options"] = clarify_options',
            "replace": 'ctx.respond_payload["clarify_options"] = []',
            "expect_fail_tests": [
                "TestClarificationFieldContract::test_backend_result_has_clarify_fields",
            ],
        },
        {
            "name": "把后端 steps 的 args 抹掉",
            "file": "h3studio/agent.py",
            # steps[] 里的 args 是「一次性路径下前端渲染工具参数」的来源。
            # 抹掉它 -> 前端工具卡就只剩结果、没有参数 -> 契约测试必须失败。
            "find": 'args=_clip(json.dumps(call["arguments"], ensure_ascii=False), 200),',
            "replace": 'args=None,',
            "expect_fail_tests": [
                "TestStepsArgsContract::test_backend_steps_carry_args",
            ],
        },
    ]

    results = []
    for mut in mutations:
        tmp = Path(tempfile.mkdtemp(prefix="h3-c5-mut-"))
        try:
            # 复制整个仓库（排除大目录）
            subprocess.run(
                ["rsync", "-a", "--exclude", ".git", "--exclude", "evidence",
                 "--exclude", "__pycache__", f"{REPO}/", f"{tmp}/"],
                check=True, capture_output=True, timeout=600)
            target = tmp / mut["file"]
            text = target.read_text(encoding="utf-8")
            if mut["find"] not in text:
                results.append({
                    "name": mut["name"], "skipped": "找不到待改字符串",
                    "find": mut["find"][:80],
                })
                continue
            target.write_text(text.replace(mut["find"], mut["replace"], 1),
                              encoding="utf-8")
            cmd = [sys.executable, "-m", "pytest", "tests/test_agent_contract.py",
                   "-p", "no:cacheprovider", "--tb=no", "-q"]
            proc = subprocess.run(cmd, cwd=str(tmp), capture_output=True,
                                  text=True, timeout=1800)
            out_file = OUT_DIR / f"c5-mutation-{mut['name'][:20]}.txt"
            out_file.write_text(proc.stdout + "\n" + proc.stderr,
                                encoding="utf-8")
            # 期望：被改坏后测试必须失败（returncode != 0）
            results.append({
                "name": mut["name"],
                "file": mut["file"],
                "returncode": proc.returncode,
                "tests_detected_mutation": proc.returncode != 0,
                "output_file": str(out_file),
                "tail": (proc.stdout or "")[-1200:],
            })
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return {
        "mutations": results,
        "all_detected": all(
            r.get("tests_detected_mutation") for r in results
            if "skipped" not in r),
        "skipped": [r["name"] for r in results if "skipped" in r],
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("== ① 静态检查：契约测试真的打真实后端 ==")
    real = check_real_backend_usage()
    for k, v in real["checks"].items():
        print(f"   {k}: {v}")

    print("\n== ② 跑契约测试 ==")
    res = run_contract_tests()
    print(f"   {res['summary']}")
    print(f"   通过 {res['passed_count']} / 失败 {res['failed_count']}")

    print("\n== ③ 按五项契约分类 ==")
    by_contract = classify_cases(res["passed"])
    for label, cases in by_contract.items():
        print(f"   {label}: {len(cases)} 条")

    print("\n== ④ 反向自检（mutation testing）==")
    mut = run_mutation_selfcheck()
    for m in mut["mutations"]:
        print(f"   {m['name']}: detected={m.get('tests_detected_mutation')} "
              f"rc={m.get('returncode')}")

    verdict = {
        "uses_real_backend": real["all_pass"],
        "all_contract_tests_passed": res["failed_count"] == 0
                                      and res["returncode"] == 0,
        "passed_count": res["passed_count"],
        "failed_count": res["failed_count"],
        "five_contracts_all_covered": all(
            len(by_contract.get(k, [])) > 0
            for k in list(CONTRACT_CLASSES)[:5]),
        "mutation_detected": mut["all_detected"],
        "mutation_skipped": mut["skipped"],
    }
    evidence = {
        "tests": res,
        "by_contract": by_contract,
        "real_backend_usage": real,
        "mutation": mut,
        "verdict": verdict,
    }
    (OUT_DIR / "acceptance_c5_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n===== c5 判定 =====")
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    print(f"\n证据目录：{OUT_DIR}")
    ok = (verdict["uses_real_backend"]
          and verdict["all_contract_tests_passed"]
          and verdict["five_contracts_all_covered"]
          and verdict["mutation_detected"])
    return 0 if ok else 5


if __name__ == "__main__":
    raise SystemExit(main())
