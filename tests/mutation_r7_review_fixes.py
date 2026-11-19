#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7 变异测试：证明第 7 轮那 20 条断言**不是恒真断言**。

每个变异都必须让**对应的命名断言**变红。变异体用完立即还原，
并复核「还原后仍然绿」（证明还原干净）。

【行尾纪律】server.py 是 CRLF（.gitattributes 的 -text 规则）——
变异串必须用 \\r\\n，否则匹配不到。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = "/home/ubuntu/comfyui/venv/bin/python"
TEST = "tests/test_r7_review_fixes.py"

SERVER = ROOT / "server.py"
CS = ROOT / "h3studio" / "continuation_service.py"
AM = ROOT / "h3studio" / "artifact_meta.py"
VM = ROOT / "h3studio" / "video_models.py"
MR = ROOT / "h3studio" / "model_recommendations.py"
VP = ROOT / "web" / "video-panel.js"

#: (编号, 描述, 文件, 旧串, 新串, 必须失败的测试选择器)
MUTATIONS = [
    # ---- B1：allow_twopass ----
    (
        "M1",
        "把 allow_twopass 的保留逻辑删掉（= 缺陷本体）",
        SERVER,
        '    if info.get("allow_twopass") is True:\r\n'
        '        params["continuation"]["allow_twopass"] = True\r\n',
        "",
        f"{TEST}::AllowTwopassPreservedTest::"
        "test_allow_twopass_survives_server_normalization",
    ),
    (
        "M2",
        "让 allow_twopass 无条件为 True（= 反而放开了没勾选的路径）",
        SERVER,
        '    if info.get("allow_twopass") is True:\r\n',
        '    if True:\r\n',
        f"{TEST}::AllowTwopassPreservedTest::"
        "test_no_allow_means_still_denied",
    ),
    # ---- B2：ref_video_frame_count ----
    (
        "M3",
        "不再继承上游的 ref_video_frame_count（= 缺陷本体）",
        SERVER,
        '    if upstream_ref_frames > 0:\r\n'
        '        params["continuation"]["ref_video_frame_count"] = \\\r\n'
        '            upstream_ref_frames\r\n',
        "",
        f"{TEST}::RefVideoFrameCountInheritedTest::"
        "test_second_segment_inherits_frame_count",
    ),
    (
        "M4",
        "接续上下文不再下发 ref_video_frame_count",
        CS,
        '        "ref_video_frame_count": int(\n'
        '            info.get("ref_video_frame_count") or 0),\n',
        "",
        f"{TEST}::RefVideoFrameCountInheritedTest::"
        "test_context_carries_ref_video_frame_count",
    ),
    # ---- B3：加速 LoRA key ----
    (
        "M5",
        "回写时用权重文件名而不是目录 key（= 缺陷本体）",
        AM,
        '        params["fixed_accel_lora"] = slot_key\n'
        '        params["accel_lora"] = slot_key\n',
        '        params["fixed_accel_lora"] = first.get("name") or ""\n'
        '        params["accel_lora"] = first.get("name") or ""\n',
        f"{TEST}::ArtifactAccelKeyTest::test_accel_lora_is_the_catalog_key",
    ),
    (
        "M6",
        "工作流层不再归一「权重名形态」的 key（存量数据失效）",
        VM,
        "        key = _normalize_accel_key(key)\n",
        "",
        f"{TEST}::ArtifactAccelKeyTest::"
        "test_legacy_weight_name_slot_is_still_resolved",
    ),
    # ---- B4：畸形参数 ----
    (
        "M7",
        "context_frames 去掉 try/except（畸形值又变 500）",
        SERVER,
        '    try:\r\n'
        '        window = cont.snap_context_frames(\r\n'
        '            int(info.get("context_frames") or cont.DEFAULT_CONTEXT_FRAMES))\r\n'
        '    except (TypeError, ValueError):\r\n'
        '        window = 0\r\n',
        '    window = cont.snap_context_frames(\r\n'
        '        int(info.get("context_frames") or cont.DEFAULT_CONTEXT_FRAMES))\r\n',
        f"{TEST}::MalformedContinuationTest::"
        "test_string_context_frames_does_not_raise",
    ),
    (
        "M8",
        "畸形 context_frames 回退成 5 帧（不是默认窗口）",
        SERVER,
        '    except (TypeError, ValueError):\r\n'
        '        window = 0\r\n',
        '    except (TypeError, ValueError):\r\n'
        '        window = cont.snap_context_frames(5)\r\n',
        f"{TEST}::MalformedContinuationTest::"
        "test_malformed_falls_back_to_default_window",
    ),
    # ---- B5：可见文案 ----
    (
        "M9",
        "把「出处」里的源码表达式放回去（第一处）",
        MR,
        '            "（不挂加速 LoRA 时 = 20 步 / simple / res_multistep）"\n',
        '            "（default_steps = 4 if lora != \'none\' else 20）"\n',
        f"{TEST}::FrontendVisibleTextHygieneTest::"
        "test_no_workflow_source_code_in_recommendation_source",
        "_MODEL_RECOMMENDATIONS: dict[str, dict] = {",
    ),
    (
        "M10",
        "把 nonebot 写回前端可见文案",
        VP,
        '        ? "低噪段加密采样"\n',
        '        ? "低噪段加密采样（借鉴 nonebot H3 工作流）"\n',
        f"{TEST}::FrontendVisibleTextHygieneTest::"
        "test_no_nonebot_in_user_visible_strings",
    ),
    (
        "M11",
        "把节点类名写回前端可见文案",
        VP,
        '        `采样器 ${videoState.sampler || "—"}`,\n',
        '        `采样器 ${videoState.sampler || "—"} → KSamplerSelect.sampler_name`,\n',
        f"{TEST}::FrontendVisibleTextHygieneTest::"
        "test_no_comfy_node_class_names_in_user_visible_strings",
    ),
    # ---- 第 7 轮追加：后端发给前端的说明文案（M12/M13/M14）----
    (
        "M12",
        "把「（SelfLift）」写回后端 twopass.note（= 第 7 轮漏修的缺陷本体）",
        VM,
        '    "note": "二采 = 低分辨率一采 + 高分辨率二采。"\n',
        '    "note": "二采 = 低分辨率一采 + 高分辨率二采（SelfLift）。"\n',
        f"{TEST}::BackendVisibleTextHygieneTest::"
        "test_backend_note_has_no_node_class_names",
    ),
    (
        "M13",
        "把 BasicGuider / SelfLiftH3Sampler 写回后端 CFG 说明文案",
        VM,
        '    "H3 标准采样路径固定 CFG=1.0：标准引导器"\n',
        '    "H3 标准采样路径固定 CFG=1.0：官方工作流用的是 BasicGuider"\n',
        f"{TEST}::BackendVisibleTextHygieneTest::"
        "test_backend_note_has_no_node_class_names",
    ),
    (
        "M14",
        "把「潜空间放大器」写回 web/index.html 的可见标签（= 第 7 轮漏修的缺陷本体）",
        ROOT / "web" / "index.html",
        '<div class="compact-label"><span>放大器模型</span>',
        '<div class="compact-label"><span>潜空间放大器</span>',
        f"{TEST}::BackendVisibleTextHygieneTest::"
        "test_frontend_html_has_no_internal_jargon",
    ),
]


def _run_pytest(selector: str):
    proc = subprocess.run(
        [PY, "-m", "pytest", selector, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=600)
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-2500:]


def main() -> int:
    print("=" * 78)
    print("变异测试：第 7 轮审查的断言是否非恒真")
    print("=" * 78)

    ok, out = _run_pytest(TEST)
    if not ok:
        print("!! 变异前就有失败，先修测试：")
        print(out[-2500:])
        return 1
    print("\n[ OK ] 变异前全部通过\n")

    targets = [SERVER, CS, AM, VM, MR, VP]
    backups: dict[str, bytes] = {}
    for p in targets:
        backups[str(p)] = p.read_bytes()
    # 【重要】所有 MUTATIONS 的目标文件都要备份 —— 否则 finally 里的
    # restore 会因 KeyError 崩掉，把已写入的变异体留在磁盘上。
    for entry in MUTATIONS:
        t = entry[2]
        backups.setdefault(str(t), t.read_bytes())

    failures = []
    try:
        for entry in MUTATIONS:
            mid, desc, target, old, new, selector = entry[:6]
            anchor = entry[6] if len(entry) > 6 else None
            data = target.read_bytes()
            old_b = old.encode("utf-8")
            new_b = new.encode("utf-8")
            if anchor:
                # 只在 anchor 之后替换**第一次**出现
                at = data.find(anchor.encode("utf-8"))
                if at < 0:
                    print(f"[FAIL] {mid}: 找不到 anchor {anchor!r}")
                    failures.append(mid)
                    continue
                pos = data.find(old_b, at)
                if pos < 0:
                    print(f"[FAIL] {mid}: anchor 之后找不到目标片段")
                    failures.append(mid)
                    continue
                mutated = data[:pos] + new_b + data[pos + len(old_b):]
            else:
                cnt = data.count(old_b)
                if cnt != 1:
                    print(f"[FAIL] {mid}: 目标片段在 {target.name} 里出现 {cnt} 次"
                          "（应恰好 1 次）—— 变异体已失效")
                    failures.append(mid)
                    continue
                mutated = data.replace(old_b, new_b)
            target.write_bytes(mutated)
            try:
                passed, out = _run_pytest(selector)
            finally:
                target.write_bytes(backups[str(target)])
            if passed:
                print(f"[FAIL] {mid}: {desc}\n        -> 断言**仍然绿**"
                      "（变异没被抓住）")
                failures.append(mid)
            else:
                red = [ln for ln in out.splitlines()
                       if ln.startswith("E ") or "AssertionError" in ln]
                detail = red[-1][:170] if red else "(见 pytest 输出)"
                print(f"[ OK ] {mid}: {desc}")
                print(f"        -> 如期变红：{detail}")
            passed_again, _ = _run_pytest(selector)
            if not passed_again:
                print(f"[FAIL] {mid}: 还原后仍然红 —— 还原不干净")
                failures.append(mid + "-restore")
    finally:
        for p_str, data in backups.items():
            Path(p_str).write_bytes(data)

    print()
    if failures:
        print(f"变异测试结果：{len(failures)} 条有问题 -> {failures}")
        return 1
    print(f"变异测试结果：全部 {len(MUTATIONS)} 条变异均让对应断言变红 ✅")
    print("（证明这些断言不是恒真断言）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
