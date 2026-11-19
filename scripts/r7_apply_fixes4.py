#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7 修复补丁（第四轮）：前端可见文案里的**内部工程黑话**。

【缺陷 B5（中等）】用户可见界面里出现了内部工程黑话：
  * 「nonebot」—— 第三方插件名（用户根本不知道这是什么项目）；
  * 「BasicScheduler / KSamplerSelect / SelfLiftH3Sampler」
    —— ComfyUI 节点类名（普通用户不需要知道节点叫什么）；
  * 「低噪段加密采样（借鉴 nonebot H3 工作流）」——
    「借鉴某工作流」是**开发过程**，不是用户需要的信息。

【修法】改成用户能懂的话，**信息量不丢**：
  * 「nonebot minimax_h3_seg2 等价」->「标准采样路径（与 nonebot 之外的
    官方标准采样等价）」→ 直接说「标准采样路径（实测无此缺陷）」；
  * 节点类名 -> 换成参数名（步数 / 采样器 / 调度器 / 二采节点）。

【行尾】web/index.html 是 CRLF、web/video-panel.js 是 LF ——
本脚本按各文件**自身**的行尾展开模板。

用法：python3 scripts/r7_apply_fixes4.py [--check]
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHECK_ONLY = "--check" in sys.argv


def read_text(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def write_text(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def newline_of(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def sub(text: str, old: str, new: str, label: str, expect: int = 1) -> str:
    nl = newline_of(text)
    old = old.replace("\n", nl)
    new = new.replace("\n", nl)
    count = text.count(old)
    if count != expect:
        raise SystemExit(f"[{label}] 期望命中 {expect} 处，实际 {count} 处")
    return text.replace(old, new)


# ---------------------------------------------------------------- index.html
IDX_OLD = '''          <span class="muted" id="vidTwopassAllowContinuationHint">接续段用二采会强制走段头色档跳变（实测段头断点比 2.58x–4.54x、3 帧亮度降 +4.7～+7.2 灰阶）；标准采样等价 nonebot minimax_h3_seg2 且无此缺陷</span>'''

IDX_NEW = '''          <span class="muted" id="vidTwopassAllowContinuationHint">接续段用二采会强制走段头色档跳变（实测段头断点比 2.58x–4.54x、3 帧亮度降 +4.7～+7.2 灰阶）；标准采样路径无此缺陷</span>'''


# ---------------------------------------------------------------- video-panel.js
VP_OLD_SAMPLING = '''        `步数 ${videoState.steps} → BasicScheduler.steps`,
        `采样器 ${videoState.sampler || "—"} → KSamplerSelect.sampler_name`,
        `调度器 ${videoState.scheduler || "—"} → BasicScheduler.scheduler`,
        `shift ${Number(videoState.shiftVideo).toFixed(2)} / `
          + `${Number(videoState.shiftAudio).toFixed(2)} → MiniMaxH3SigmaShift`,
'''

VP_NEW_SAMPLING = '''        `步数 ${videoState.steps}`,
        `采样器 ${videoState.sampler || "—"}`,
        `调度器 ${videoState.scheduler || "—"}`,
        `shift ${Number(videoState.shiftVideo).toFixed(2)} / `
          + `${Number(videoState.shiftAudio).toFixed(2)}`,
'''

VP_OLD_NODE_HINT = '''        : "本机 ComfyUI 没有 SelfLiftH3Sampler 节点，不可用";'''

VP_NEW_NODE_HINT = '''        : "本机 ComfyUI 没有二采节点，不可用";'''

VP_OLD_REFINE = '''        ? "低噪段加密采样（借鉴 nonebot H3 工作流）"'''

VP_NEW_REFINE = '''        ? "低噪段加密采样"'''

VP_OLD_CONFIRM = '''            + "默认路径（不勾这个）走标准采样，与 nonebot minimax_h3_seg2 等价，无此缺陷。\\n"'''

VP_NEW_CONFIRM = '''            + "默认路径（不勾这个）走标准采样，无此缺陷。\\n"'''

VP_OLD_SAMPLER_TITLE = '''        ? "二采（SelfLift）只支持标准 Euler 采样器 —— 开启二采时实际使用 euler"'''

VP_NEW_SAMPLER_TITLE = '''        ? "二采只支持标准 Euler 采样器 —— 开启二采时实际使用 euler"'''

VP_OLD_SAMPLER_HINT = '''      mainSamplerHint.title = "二采（SelfLift）节点只支持标准 Euler 采样器";'''

VP_NEW_SAMPLER_HINT = '''      mainSamplerHint.title = "二采节点只支持标准 Euler 采样器";'''


def patch_index() -> list[str]:
    path = REPO / "web" / "index.html"
    text = read_text(path)
    text = sub(text, IDX_OLD, IDX_NEW, "B5-idx")
    if not CHECK_ONLY:
        write_text(path, text)
    return ["B5 index.html：提示语去掉 nonebot minimax_h3_seg2"]


def patch_panel() -> list[str]:
    path = REPO / "web" / "video-panel.js"
    text = read_text(path)
    text = sub(text, VP_OLD_SAMPLING, VP_NEW_SAMPLING, "B5-sampling")
    text = sub(text, VP_OLD_NODE_HINT, VP_NEW_NODE_HINT, "B5-nodehint")
    text = sub(text, VP_OLD_REFINE, VP_NEW_REFINE, "B5-refine")
    text = sub(text, VP_OLD_CONFIRM, VP_NEW_CONFIRM, "B5-confirm")
    text = sub(text, VP_OLD_SAMPLER_TITLE, VP_NEW_SAMPLER_TITLE, "B5-title")
    text = sub(text, VP_OLD_SAMPLER_HINT, VP_NEW_SAMPLER_HINT, "B5-hint")
    if not CHECK_ONLY:
        write_text(path, text)
    return ["B5 video-panel.js：说明行 / 二采文案去掉节点类名与 nonebot"]


def main() -> int:
    applied = patch_index() + patch_panel()
    print(("检查模式（未落盘）: " if CHECK_ONLY else "已应用: ") + "；".join(applied))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
