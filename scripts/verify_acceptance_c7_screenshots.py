#!/usr/bin/env python3
"""c7 验收取证：三档宽度真实浏览器截图 + console 报错计数。

【要证明什么】
  1. 375 / 390 / 1280 三档宽度下，界面**真的**能渲染：
     * 澄清卡（含可点击快捷选项）
     * 工具卡（含参数行 + 结果行）
     * 流式增量中间态（句子没写完）
  2. 每档的 console 报错数与 pageError 数。

【跟 c1/c2/c3 的关系】
  c1/c2/c3 是**行为断言**（DOM/状态/网络）。
  c7 是**视觉证据** —— 把「断言说它在那儿」变成「截图里看得见」。
  两者共用同一套取证脚本，本脚本负责**汇总 + 复制到统一目录**。

用法：
    python3 scripts/verify_acceptance_c7_screenshots.py [证据输出目录]
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/h3-acceptance-c7")
VIEWPORTS = [375, 390, 1280]


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    shots_dir = OUT_DIR / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)

    # c1/c2/c3 的取证 JSON 是权威来源（截图 + console 计数都在里面）
    src_candidates = sorted(
        p for p in OUT_DIR.parent.glob("*/acceptance_c123_evidence.json"))
    if not src_candidates:
        print(f"!! 在 {OUT_DIR.parent} 下找不到 acceptance_c123_evidence.json")
        return 1
    src = src_candidates[-1]
    print(f"读取取证 JSON: {src}")
    data = json.loads(src.read_text(encoding="utf-8"))

    vp_data = data.get("browser", {}).get("viewports", {})
    console = data.get("browser", {}).get("console", [])
    page_errors = data.get("browser", {}).get("pageErrors", [])

    # 复制截图到统一目录（并在 JSON 里记绝对路径）
    manifest = {"viewports": {}, "console_errors": {}, "page_errors": {},
                "source_evidence": str(src)}
    for width in VIEWPORTS:
        entry = vp_data.get(str(width), {})
        per_vp = {"console_errors": 0, "page_errors": 0, "shots": {}}
        for key, label in (("c2_shot", "clarify"),
                           ("c3_shot", "toolcard"),
                           ("c3_failed_shot", "toolcard_failed")):
            src_path = entry.get(key)
            if not src_path:
                continue
            dst = shots_dir / f"c7-{label}-{width}.png"
            shutil.copy2(src_path, dst)
            per_vp["shots"][label] = str(dst)

        # 流式中间态：按长度阈值存了多张（12 / 30 / 55），
        # 全部收进来 —— 报告里要能并排看到「短句 -> 更长 -> 完整」。
        stream = entry.get("c3_stream") or {}
        for sub in ("mid_shot_12", "mid_shot_30", "mid_shot_55"):
            src_path = stream.get(sub)
            if not src_path:
                continue
            tag = sub.replace("mid_shot_", "mid")
            dst = shots_dir / f"c7-stream-{tag}-{width}.png"
            shutil.copy2(src_path, dst)
            per_vp["shots"][f"stream_{tag}"] = str(dst)
        final = stream.get("final_shot")
        if final:
            dst = shots_dir / f"c7-stream-final-{width}.png"
            shutil.copy2(final, dst)
            per_vp["shots"]["stream_final"] = str(dst)

        per_vp["console_errors"] = sum(
            1 for c in console if c.get("width") == width
            and c.get("type") == "error")
        per_vp["page_errors"] = sum(
            1 for e in page_errors if e.get("width") == width)
        # 关键可见性判定
        clarify = entry.get("c2_clarify") or {}
        tool = entry.get("c3_toolcard") or {}
        failed = entry.get("c3_failed_tool") or {}
        per_vp["visible"] = {
            "clarify_card": clarify.get("domChipCount", 0) >= 3,
            "clarify_chip_texts": clarify.get("domChipTexts", []),
            "toolcard_args": tool.get("toolsWithArgs", 0) >= 1,
            "toolcard_result": tool.get("toolsWithResult", 0) >= 1,
            "toolcard_empty_result_blocks": tool.get("emptyResultBlocks", -1),
            "failed_tool_error_text": failed.get("toolsWithErrorText", 0) >= 1,
            "stream_distinct_lengths": stream.get("distinct_lengths", 0),
            "stream_mid12_len": stream.get("mid_shot_12_len", 0),
            "stream_mid30_len": stream.get("mid_shot_30_len", 0),
            "stream_mid55_len": stream.get("mid_shot_55_len", 0),
            "stream_max_len": stream.get("max_len", 0),
        }
        manifest["viewports"][str(width)] = per_vp

    manifest["console_errors"] = {str(w): sum(
        1 for c in console if c.get("width") == w and c.get("type") == "error")
        for w in VIEWPORTS}
    manifest["page_errors"] = {str(w): sum(
        1 for e in page_errors if e.get("width") == w) for w in VIEWPORTS}
    manifest["total_console_errors"] = sum(manifest["console_errors"].values())
    manifest["total_page_errors"] = sum(manifest["page_errors"].values())

    verdict = {
        "all_three_viewports_present": all(
            str(w) in vp_data for w in VIEWPORTS),
        "clarify_card_visible_all": all(
            manifest["viewports"][str(w)]["visible"]["clarify_card"]
            for w in VIEWPORTS),
        "toolcard_args_and_result_all": all(
            manifest["viewports"][str(w)]["visible"]["toolcard_args"]
            and manifest["viewports"][str(w)]["visible"]["toolcard_result"]
            for w in VIEWPORTS),
        "no_empty_result_blocks": all(
            manifest["viewports"][str(w)]["visible"]["toolcard_empty_result_blocks"] == 0
            for w in VIEWPORTS),
        "failed_tool_error_visible_all": all(
            manifest["viewports"][str(w)]["visible"]["failed_tool_error_text"]
            for w in VIEWPORTS),
        "streaming_increment_all": all(
            manifest["viewports"][str(w)]["visible"]["stream_distinct_lengths"] >= 3
            for w in VIEWPORTS),
        "streaming_partial_shots_all": all(
            all(manifest["viewports"][str(w)]["visible"][k] > 0
                for k in ("stream_mid12_len", "stream_mid30_len",
                          "stream_mid55_len"))
            and manifest["viewports"][str(w)]["visible"]["stream_mid55_len"]
            < manifest["viewports"][str(w)]["visible"]["stream_max_len"]
            for w in VIEWPORTS),
        "total_console_errors": manifest["total_console_errors"],
        "total_page_errors": manifest["total_page_errors"],
    }
    verdict["all_pass"] = (
        verdict["all_three_viewports_present"]
        and verdict["clarify_card_visible_all"]
        and verdict["toolcard_args_and_result_all"]
        and verdict["no_empty_result_blocks"]
        and verdict["failed_tool_error_visible_all"]
        and verdict["streaming_increment_all"]
        and verdict["streaming_partial_shots_all"]
        and verdict["total_console_errors"] == 0
        and verdict["total_page_errors"] == 0)

    manifest["verdict"] = verdict
    out_file = OUT_DIR / "acceptance_c7_evidence.json"
    out_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    print("\n===== c7 判定 =====")
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    print(f"\n证据目录：{OUT_DIR}")
    print(f"截图目录：{shots_dir}")
    return 0 if verdict["all_pass"] else 5


if __name__ == "__main__":
    raise SystemExit(main())
