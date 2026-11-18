#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异测试：证明「接续段默认走标准采样」的断言**不是恒真断言**。

【为什么需要这个】
新写的 tests/test_twopass_seg2_policy.py 守的是一条**新契约**
（接续段禁二采）。新契约最容易出的问题是：断言写得太空，
实现回退到旧行为（接续段又走 SelfLift）时测试仍然绿。
⇒ 本脚本对实现做 8 个定向变异，每个变异都必须让对应断言变红。

【方法】
每个变异：
  1. 把实现改成「缺陷版」（= 本轮修复前的行为）
  2. 跑对应测试子集
  3. 必须**失败**（红）—— 失败才算变异被抓住
  4. 还原实现，重跑必须**通过**（绿）—— 确认还原干净

【重要】变异体一律在**临时副本**上做（用 git stash 之外的手段），
结束后逐个还原并用 `git diff --stat` 确认工作树干净。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = "/home/ubuntu/comfyui/venv/bin/python"

SERVER = os.path.join(ROOT, "server.py")
POLICY = os.path.join(ROOT, "h3studio", "twopass_policy.py")
VWF = os.path.join(ROOT, "h3studio", "video_workflow.py")
VMODES = os.path.join(ROOT, "h3studio", "h3_video_modes.py")

#: (编号, 描述, 目标文件, 旧串, 新串, 必须失败的测试选择器)
MUTATIONS = [
    (
        "M1",
        "把服务端归一层的「接续段禁二采」整段删掉（= 修复前的行为）",
        SERVER,
        # 【注意】server.py 是 CRLF 文件（.gitattributes 的 -text 规则），
        # 变异串必须用 \r\n，否则匹配不到 —— 这正是本仓行尾纪律的坑。
        "        _tp_use, _tp_policy = twopass_policy.resolve_effective_twopass(params)\r\n",
        "        _tp_use, _tp_policy = True, 'allow'\r\n",
        "tests/test_twopass_seg2_policy.py::"
        "TestServerNormalizationIsAuthoritative::"
        "test_continuation_with_twopass_true_gets_disabled",
    ),
    (
        "M2",
        "把工作流层的第二道防线改成「直接读入参」（绕过归一层时又走回 SelfLift）",
        VWF,
        "    use_twopass, _tp_disposition = twopass_policy.resolve_effective_twopass(params)\n",
        "    use_twopass = bool(twopass.get('enabled'))\n",
        "tests/test_twopass_seg2_policy.py::"
        "TestContinuationProducesStandardSampler::"
        "test_t2v_continuation_is_standard_sampler",
    ),
    (
        "M3",
        "flf 路径的工作流层防线改成「直接读入参」",
        VMODES,
        "    use_twopass, _tp_disposition = twopass_policy.resolve_effective_twopass(params)\n",
        "    use_twopass = bool(isinstance(twopass, dict) and twopass.get('enabled'))\n",
        "tests/test_twopass_seg2_policy.py::"
        "TestContinuationProducesStandardSampler::"
        "test_flf_continuation_is_standard_sampler",
    ),
    (
        "M4",
        "r2v 路径的工作流层防线改成「直接读入参」",
        VMODES,
        "    use_twopass, _tp_disposition = twopass_policy.resolve_effective_twopass(params)\n",
        "    use_twopass = bool(isinstance(twopass, dict) and twopass.get('enabled'))\n",
        "tests/test_twopass_seg2_policy.py::"
        "TestContinuationProducesStandardSampler::"
        "test_r2v_continuation_is_standard_sampler",
    ),
    (
        "M5",
        "策略判据从 source_job 换成永远 allow（= 旧行为：接续段不禁二采）",
        POLICY,
        '    is_continuation = bool(str(cont.get("source_job") or "").strip())\n',
        "    is_continuation = False\n",
        "tests/test_twopass_seg2_policy.py::TestPolicyResolution::"
        "test_continuation_is_deny",
    ),
    (
        "M6",
        "首段也被禁二采（把能力一刀切掉 —— 过度收紧也必须被抓住）",
        POLICY,
        'PLAIN_TWOPASS_POLICY = "allow"\n',
        'PLAIN_TWOPASS_POLICY = "deny"\n',
        "tests/test_twopass_seg2_policy.py::"
        "TestPlainSegmentKeepsTwopass::"
        "test_t2v_first_segment_still_allows_twopass",
    ),
    (
        "M7",
        "显式放行口子改成「默认放行」（= 悄悄开着，正是要防的 bug）",
        POLICY,
        '    return cont.get("allow_twopass") is True\n',
        "    return cont.get(\"allow_twopass\") is not False\n",
        "tests/test_twopass_seg2_policy.py::TestPolicyResolution::"
        "test_explicit_allow_flag_is_required",
    ),
    (
        "M8",
        "中文留痕改成空串（留痕本身也必须被守住）",
        POLICY,
        '    return (f"接续段{stage}已按标准采样路径执行（二采已关闭）："',
        '    return (f""',
        "tests/test_twopass_seg2_policy.py::TestChineseNotes::"
        "test_disabled_note_mentions_cause_and_effect",
    ),
    (
        "M9",
        "【改守新契约的旧断言】把「显式放行」口子删掉 —— "
        "tests/test_cont_selflift.py 的 6 条接线断言必须仍能拒绝「没有 SelfLift」",
        POLICY,
        '    return cont.get("allow_twopass") is True\n',
        "    return False\n",
        "tests/test_cont_selflift.py::TestSelfliftWorkflowBridge::"
        "test_selflift_sampler_consumes_bridge_latent",
    ),
    (
        "M10",
        "【改守新契约的旧断言】把「显式放行」口子的严格性放松成 "
        "「字符串也算放行」—— 必须仍能拒绝非严格的显式声明",
        POLICY,
        '    return cont.get("allow_twopass") is True\n',
        '    return bool(cont.get("allow_twopass"))\n',
        "tests/test_twopass_seg2_policy.py::TestPolicyResolution::"
        "test_explicit_allow_flag_is_required",
    ),
]


def _run_pytest(selector: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [PY, "-m", "pytest", selector, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=600)
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-2500:]


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def main() -> int:
    print("=" * 78)
    print("变异测试：接续段默认走标准采样 —— 每个变异都必须让对应断言变红")
    print("=" * 78)

    # 变异前：全部通过
    ok, out = _run_pytest("tests/test_twopass_seg2_policy.py")
    if not ok:
        print("!! 变异前就有失败，先修测试：")
        print(out[-2000:])
        return 1
    print("\n[ OK ] 变异前全部通过\n")

    backups = {p: _read(p) for p in (SERVER, POLICY, VWF, VMODES)}
    failures = []

    for mid, desc, target, old, new, selector in MUTATIONS:
        data = backups[target]
        old_b = old.encode("utf-8")
        new_b = new.encode("utf-8")
        if data.count(old_b) != 1:
            print(f"[FAIL] {mid}: 目标片段在 {os.path.basename(target)} 里出现 "
                  f"{data.count(old_b)} 次（应恰好 1 次）—— 变异体已失效")
            failures.append(mid)
            continue
        _write(target, data.replace(old_b, new_b))
        try:
            passed, out = _run_pytest(selector)
        finally:
            _write(target, backups[target])
        if passed:
            print(f"[FAIL] {mid}: {desc}\n"
                  f"        -> 断言**仍然绿**（变异没被抓住）")
            failures.append(mid)
        else:
            red = [ln for ln in out.splitlines()
                   if ln.startswith("E ") or "AssertionError" in ln]
            detail = red[-1][:160] if red else "(见 pytest 输出)"
            print(f"[ OK ] {mid}: {desc}")
            print(f"        -> 如期变红：{detail}")
        # 还原后必须恢复绿
        passed_again, _ = _run_pytest(selector)
        if not passed_again:
            print(f"[FAIL] {mid}: 还原后仍然红 —— 还原不干净")
            failures.append(mid + "-restore")

    print()
    if failures:
        print(f"变异测试结果：{len(failures)} 条有问题 -> {failures}")
        return 1
    print(f"变异测试结果：全部 {len(MUTATIONS)} 条变异均让对应断言变红 ✅")
    print("（证明这些断言不是恒真断言）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
