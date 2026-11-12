# -*- coding: utf-8 -*-
"""变异验证：证明「错误中文化 + 混合态」的新断言**真的能抓住回归】。

【为什么需要这个脚本】
一组全绿的测试可能什么都没证明 —— 如果断言是空转的（断言了一个
恒为真的东西），或者断言抓的不是出问题的那条代码。

这个脚本逐个**注入变异**（把修好的代码改回缺陷状态），然后跑相关
测试，要求**至少一条断言变红**。变红的条数记进报告。

【纪律】
  * 每个变异前先 commit 当前状态，用 `git checkout -- <file>` 恢复；
  * 恢复用 `git checkout --`（会丢未提交改动）—— 所以先 commit；
  * 不碰 local/maintainer，不碰共享检出。

用法：python3 tests/mutation_check_mixed_state.py
"""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = "/home/ubuntu/comfyui/venv/bin/python"

# 变异表：(名称, 文件, 原文, 替换文, 相关测试)
MUTATIONS = [
    (
        "错误提取退回「把 m[0] 当 dict 读」（原始缺陷）",
        "h3studio/comfy_errors.py",
        '        payload = None\n'
        '        for item in message:\n'
        '            if isinstance(item, dict):\n'
        '                payload = item\n'
        '                break\n'
        '        if payload is None:\n'
        '            continue\n',
        '        payload = message[0]\n'
        '        if not isinstance(payload, dict):\n'
        '            continue\n',
        ["tests/test_comfy_errors.py"],
    ),
    (
        "错误文案不再中文化（直接回传原始 exception_message）",
        "h3studio/comfy_errors.py",
        '    matched = _match_rule(node, detail)\n'
        '    if matched:\n'
        '        template, d = matched\n'
        '        reason = template.format(detail=d)\n'
        '    else:\n'
        '        reason = _generic_message(node, kind, detail)\n',
        '    reason = detail\n',
        ["tests/test_comfy_errors.py"],
    ),
    (
        "错误文案只说「生成失败」（丢掉真正原因）",
        "h3studio/comfy_errors.py",
        '    if detail:\n'
        '        return f"生成失败：{kind}{detail}{where}。详细信息见服务端日志。"\n',
        '    if detail:\n'
        '        return f"生成失败：{kind}{where}。详细信息见服务端日志。"\n',
        ["tests/test_comfy_errors.py"],
    ),
    (
        "接续不再校验分辨率一致性（混合态的根因）",
        "h3studio/continuation.py",
        '    if (src_w, src_h) == (dst_w, dst_h):\n'
        '        return ""\n'
        '    return (f"接续失败：上一段是 {src_w}×{src_h}，本段是 {dst_w}×{dst_h} —— "',
        '    if (src_w, src_h) == (dst_w, dst_h):\n'
        '        return ""\n'
        '    return ("") or (f"接续失败：上一段是 {src_w}×{src_h}，本段是 {dst_w}×{dst_h} —— "',
    ),
    (
        "「复用参数」不再清接续上下文（混合态的根因）",
        "web/app.js",
        '    if (window.Continuation && typeof window.Continuation.clearForReuse === "function") {\n'
        '      window.Continuation.clearForReuse();\n'
        '    }\n',
        '    // 变异：不清接续上下文\n',
        ["tests/test_continuation_mixed_state.py"],
    ),
    (
        "提交前一致性闸被摘掉",
        "web/app.js",
        '        if (typeof window.Continuation.checkSubmitConsistency === "function") {\n'
        '          const size = window.VIDEO_PANEL._debug\n'
        '            ? window.VIDEO_PANEL._debug.currentSize()\n'
        '            : null;\n'
        '          if (!window.Continuation.checkSubmitConsistency(size)) return;\n'
        '        }\n',
        '        // 变异：没有提交前闸\n',
        ["tests/test_continuation_mixed_state.py"],
    ),
    (
        "applyConfig 不再归零加速 LoRA（混合态的又一种形态）",
        "web/video-panel.js",
        '    if (!hasNewAccel && Array.isArray(cfg.accel_loras)) {\n'
        '      videoState.accelLora = "";\n',
        '    if (!hasNewAccel && Array.isArray(cfg.accel_loras)) {\n',
        ["tests/test_continuation_mixed_state.py"],
    ),
    (
        "toast 退回阻塞式 alert",
        "web/continuation.js",
        '  function toast(message) {\n'
        '    const text = String(message == null ? "" : message);\n',
        '  function toast(message) {\n'
        '    alert(message);\n'
        '    const text = String(message == null ? "" : message);\n',
        ["tests/test_continuation_mixed_state.py"],
    ),
]


def run(cmd):
    return subprocess.run(cmd, cwd=REPO, shell=True, capture_output=True,
                          text=True)


def red_count(output):
    """数出失败的测试条数。"""
    count = 0
    for line in output.splitlines():
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            count += 1
    return count


def main():
    # 先把当前状态 commit 掉 —— 变异后要用 `git checkout --` 恢复，
    # 而 `git checkout --` 会**丢未提交改动**。
    st = run("git status --short")
    if st.stdout.strip():
        print("工作区有未提交改动，先 commit：")
        print(st.stdout)
        return 2
    head = run("git rev-parse HEAD").stdout.strip()

    results = []
    for name, relpath, old, new, tests in MUTATIONS:
        print(f"\n=== 变异：{name} ===")
        path = REPO / relpath
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print(f"  ⚠ 原文没找到（变异未生效）：{relpath}")
            results.append({"mutation": name, "file": relpath,
                            "applied": False, "red": 0})
            continue
        # 注入变异
        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        # 跑相关测试
        proc = subprocess.run(
            [PY, "-m", "pytest", *tests, "-q", "--no-header", "-p",
             "no:cacheprovider"],
            cwd=REPO, capture_output=True, text=True)
        out = proc.stdout + proc.stderr
        red = red_count(out)
        tail = [ln for ln in out.splitlines() if ln.startswith("FAILED")]
        print(f"  变红的断言: {red}")
        for ln in tail[:6]:
            print(f"    {ln}")
        results.append({
            "mutation": name, "file": relpath, "applied": True,
            "red": red, "failed_tests": tail,
        })
        # 恢复
        run(f"git checkout -- {relpath}")

    # 校验：工作区已恢复
    st2 = run("git status --short")
    clean = not st2.stdout.strip()

    # 恢复后跑一次完整相关测试，确认全绿
    proc = subprocess.run(
        [PY, "-m", "pytest", "tests/test_comfy_errors.py",
         "tests/test_continuation_mixed_state.py", "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True)
    after = proc.stdout + proc.stderr
    after_green = red_count(after) == 0

    report = {
        "base_commit": head,
        "mutations": results,
        "all_mutations_caught": all(
            r.get("applied") and r.get("red", 0) > 0 for r in results),
        "workspace_restored": clean,
        "green_after_restore": after_green,
    }
    report["verdict_pass"] = bool(
        report["all_mutations_caught"] and clean and after_green)

    out_file = REPO / "evidence" / "continuation-mixed-state" / \
        "mutation_report.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    print("\n=== 汇总 ===")
    for r in results:
        mark = "✅" if r.get("red", 0) > 0 else "❌"
        print(f"  {mark} {r['mutation']} -> {r.get('red', 0)} 条断言变红")
    print(f"\n工作区已恢复: {clean}")
    print(f"恢复后全绿: {after_green}")
    print(f"报告: {out_file}")
    print(f"结论: {'通过 ✅' if report['verdict_pass'] else '未通过 ❌'}")
    return 0 if report["verdict_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
