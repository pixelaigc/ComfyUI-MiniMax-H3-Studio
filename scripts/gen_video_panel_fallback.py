#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从服务端真源重新生成 web/video-panel-fallback.js 的清单内容。

为什么需要这个脚本（2026-11-12 合并时发现）：
  web/video-panel-fallback.js 是一份**手抄的静态快照**，用来在
  ComfyUI/后端不可达时仍让视频面板渲染出可用的下拉。
  它必须与服务端 h3studio/video_models.py 的真源**逐条相同**，
  否则兜底态下算出的尺寸是错的（这正是 panel-fallback 分支自己
  写的 test_fallback_size_presets_match_server 在守的东西）。

  本次把 video-tabs 分支（2.0M 像素上限，尺寸预设 57 -> 111 条）
  合进来后，手抄快照就过期了 —— 兜底态会给用户错误的尺寸。
  这个脚本把快照重新从服务端真源生成，**不再手抄**。

用法：/home/ubuntu/comfyui/venv/bin/python scripts/gen_video_panel_fallback.py
"""
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from h3studio import video_models as VM  # noqa: E402

TARGET = os.path.join(REPO, "web", "video-panel-fallback.js")

# 兜底清单必须覆盖的 key（前端 applyFallbackManifest 只读这些）
KEYS = [
    "size_presets",
    "models",
    "default_model",
    "attention_modes",
    "default_attention",
    "accel_lora_combos",
    "fixed_accel_loras",
    "default_fixed_accel_lora",
    "aspects",
    "size_multiple",
    "size_min",
    "size_max",
    "mp_table",
    "default_mp",
]

HEADER = """/* ============================================================
   文生视频面板 —— **静态兜底清单**（离线 / 后端异常时用）
   ------------------------------------------------------------
   为什么需要这个文件：
     2026-11-11 用户实机报「文生视频面板下拉全部空白」。
     根因：loadVideoOptions() 在 /api/video-options 失败时直接进 catch，
     renderVideoPanel() **从未执行** —— 于是尺寸预设、模型、加速 LoRA、
     注意力机制**全部**是空下拉（控件在、选项没有），
     只有一个空的 .img-warn 元素还带着橙色背景挂在模型框下面。

     这份清单让面板在拿不到服务端时**仍能渲染出可用的选项**，
     而不是一片空白。

   ⚠️ 形状必须与服务端 h3studio/video_models.py 的 options_payload() **完全一致**
     （size_presets 是**扁平 list**，不是按 aspect 分组的 dict ——
     形状错了会在 renderSizePresetSelect 里抛
     "object is not iterable"，反而把面板搞崩）。

   ⚠️ 这不是「模型一定可用」的承诺 —— 兜底态下 status 一律是
     "unknown"，界面上会明确提示「未能确认，可用性未知」。

   ⚠️ 本文件由 scripts/gen_video_panel_fallback.py **从服务端真源生成**，
     不要手改 —— 手改会让兜底态与服务端不一致（尺寸算错）。
     改完请跑：/home/ubuntu/comfyui/venv/bin/python -m pytest
     tests/test_panel_fallback.py -q
   ============================================================ */
"""


def build():
    payload = VM.options_payload()
    data = {}
    for key in KEYS:
        if key not in payload:
            raise SystemExit("服务端 payload 缺 key: %s" % key)
        data[key] = payload[key]
    return data


def main():
    data = build()
    body = json.dumps(data, ensure_ascii=False, indent=2)
    out = HEADER + "window.VIDEO_PANEL_FALLBACK = " + body + ";\n"
    with open(TARGET, "w", encoding="utf-8", newline="\n") as f:
        f.write(out)

    # 回读校验：正则必须能解析出合法 JSON（测试用同一个正则）
    src = open(TARGET, encoding="utf-8").read()
    m = re.search(r"window\.VIDEO_PANEL_FALLBACK\s*=\s*(\{.*\});?\s*$", src, re.S)
    if not m:
        raise SystemExit("生成的文件不符合测试用的解析正则")
    back = json.loads(m.group(1))
    if back["size_presets"] != VM.all_size_presets():
        raise SystemExit("回读校验失败：size_presets 与服务端不一致")
    print("已重新生成 %s" % TARGET)
    print("  size_presets = %d 条" % len(back["size_presets"]))
    print("  models       = %d 条" % len(back["models"]))
    print("  回读校验：通过（size_presets 与服务端真源逐条一致）")


if __name__ == "__main__":
    main()
