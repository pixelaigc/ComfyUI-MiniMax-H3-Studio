#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异测试：证明「参考视频画面切片与音轨来源解耦」的断言**不是恒真断言**。

每个变异都必须让对应断言变红。
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = "/home/ubuntu/comfyui/venv/bin/python"

MODES = os.path.join(ROOT, "h3studio", "h3_video_modes.py")
SERVER = os.path.join(ROOT, "server.py")
TEST = "tests/test_ref_video_continuation_slice.py"

#: (编号, 描述, 文件, 旧串, 新串, 必须失败的测试选择器)
#:
#: 【关于「限定函数区段」】有些变异片段在文件里出现多次
#: （build_flf 与 build_r2v 的接线代码结构相同）。那种情况下用
#: `anchor` 字段限定替换范围：只替换 anchor 之后**第一次**出现的片段。
MUTATIONS = [
    (
        "M1",
        "把视频切片重新绑回音轨来源（= 缺陷本体：generated 音轨时视频不切）",
        MODES,
        "    if not external:\n"
        "        # 生成音频：**音轨**不切（音画同源）；\n"
        "        # 但**画面**切片已经在上面算好了（见上面的注释）。\n"
        "        return out\n",
        "    if not external:\n"
        "        out['video_skip_frames'] = 0\n"
        "        out['video_drop'] = False\n"
        "        return out\n",
        f"{TEST}::TestRefVideoSliceDecoupledFromAudio::"
        "test_generated_audio_still_slices_video_by_chain_frames",
    ),
    (
        "M2",
        "删掉「参考视频不够长就丢弃」的分支（= 继续从 0 喂 -> 亮度阶跃）",
        MODES,
        "        elif ref_frames > 0:\n",
        "        elif False:\n",
        f"{TEST}::TestRefVideoDropWhenExhausted::"
        "test_drops_ref_video_when_chain_exceeds_frame_count",
        None,
    ),
    (
        "M3",
        "把「探测不到帧数时不猜」改成「猜一个默认值」（静默错误）",
        MODES,
        "        else:\n"
        "            # 探测不到帧数（老任务 / 探测失败）-> **不猜**，\n"
        "            # 保持原样（= 旧行为）。宁可少一个优化，不要引入静默错误。\n"
        "            out[\"video_skip_frames\"] = 0\n",
        "        else:\n"
        "            out[\"video_skip_frames\"] = C_n\n",
        f"{TEST}::TestNoGuessingWhenFrameCountUnknown::"
        "test_unknown_frame_count_keeps_legacy_behavior",
        None,
    ),
    (
        "M4",
        "工作流层不用 video_skip_frames（退回用音轨切片的 skip_frames）",
        MODES,
        "    if ref_videos:\n"
        "        condition_inputs.update(_ref_video_nodes(\n"
        "            p, ref_videos,\n"
        "            skip_frames=(slice_info[\"video_skip_frames\"]\n"
        "                         or slice_info[\"skip_frames\"]),\n",
        "    if ref_videos:\n"
        "        condition_inputs.update(_ref_video_nodes(\n"
        "            p, ref_videos,\n"
        "            skip_frames=slice_info[\"skip_frames\"],\n",
        f"{TEST}::TestBuildR2VAppliesSlice::"
        "test_video_node_gets_skip_frames_from_chain_position",
        "def build_r2v",          # anchor：只改 build_r2v 里的那处
    ),
    (
        "M5",
        "服务端把探测到的帧数写进**错误的字段名**（工作流层读不到）",
        SERVER,
        # 【行尾纪律】server.py 是 **CRLF** 文件（.gitattributes 的 -text 规则）
        # —— 变异串必须用 \r\n，否则匹配不到（本仓行尾纪律的坑）。
        '        info["ref_video_frame_count"] = int(frames)\r\n',
        '        info["ref_video_frames_typo"] = int(frames)\r\n',
        f"{TEST}::TestServerRecordsRefVideoFrameCount::"
        "test_record_writes_expected_field_name",
        None,
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
    print("变异测试：参考视频画面切片与音轨来源解耦 —— 每个变异都必须让断言变红")
    print("=" * 78)

    ok, out = _run_pytest(TEST)
    if not ok:
        print("!! 变异前就有失败，先修测试：")
        print(out[-2000:])
        return 1
    print("\n[ OK ] 变异前全部通过\n")

    backups = {}
    for p in (MODES, SERVER):
        with open(p, "rb") as fh:
            backups[p] = fh.read()

    failures = []
    for entry in MUTATIONS:
        mid, desc, target, old, new, selector = entry[:6]
        anchor = entry[6] if len(entry) > 6 else None
        with open(target, "rb") as fh:
            data = fh.read()
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
                print(f"[FAIL] {mid}: 目标片段在 {os.path.basename(target)} 里出现 "
                      f"{cnt} 次（应恰好 1 次）—— 变异体已失效")
                failures.append(mid)
                continue
            mutated = data.replace(old_b, new_b)
        with open(target, "wb") as fh:
            fh.write(mutated)
        try:
            passed, out = _run_pytest(selector)
        finally:
            with open(target, "wb") as fh:
                fh.write(backups[target])
        if passed:
            print(f"[FAIL] {mid}: {desc}\n        -> 断言**仍然绿**（变异没被抓住）")
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

    print()
    if failures:
        print(f"变异测试结果：{len(failures)} 条有问题 -> {failures}")
        return 1
    print(f"变异测试结果：全部 {len(MUTATIONS)} 条变异均让对应断言变红 ✅")
    print("（证明这些断言不是恒真断言）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
