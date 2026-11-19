#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7 修复补丁（行尾安全版）。

server.py 是 CRLF、其余是 LF —— 本脚本按各文件**自身**的行尾展开
old/new 模板，读写都用 newline=""，行尾原样保留。

五处修复：
  F1  server.py::_prepare_continuation_params —— 保留 allow_twopass
  F2  server.py::_prepare_continuation_params —— 继承 ref_video_frame_count
  F3  server.py::_prepare_continuation_params —— 畸形 continuation 不再抛 500
  F4  h3studio/continuation_service.py::build_continuation_context
      —— 下发 ref_video_frame_count
  F5  h3studio/artifact_meta.py::apply_to_params
      —— accel_lora 写目录 key（工作流层认得的那个）

用法：python3 scripts/r7_apply_fixes.py [--check]
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
    """文件的主导行尾（CRLF 或 LF）。"""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def sub(text: str, old: str, new: str, label: str, expect: int = 1) -> str:
    """文本替换：把模板里的 \\n 展开成目标文件的行尾。"""
    nl = newline_of(text)
    old = old.replace("\n", nl)
    new = new.replace("\n", nl)
    count = text.count(old)
    if count != expect:
        raise SystemExit(f"[{label}] 期望命中 {expect} 处，实际 {count} 处")
    return text.replace(old, new)


# ---------------------------------------------------------------- F1 + F2
SERVER_OLD_1 = '''    params["continuation"].update(audio_state)
    return ""
'''

SERVER_NEW_1 = '''    params["continuation"].update(audio_state)

    # ---- [2026-11-19 第 7 轮审查] ----
    #
    # F1：显式放行标记（allow_twopass）不能丢。
    #
    # [原因] 用户在前端勾选「接续段也用二采」（并二次确认）后，
    # 前端把 allow_twopass=true 放进提交的 continuation。本段重建
    # continuation 时只写固定的一组字段，**没有**带上它 ——
    # 用户显式勾选（并确认过）的接续段二采被**静默丢弃**。
    #
    # [为什么必须保留] 只有 info 里的 allow_twopass 才算是用户显式
    # 放行；服务端策略层（h3studio/twopass_policy.py）正是读这个键
    # 决定「deny」还是「warn」的。丢了它 = 用户勾了等于没勾。
    if info.get("allow_twopass") is True:
        params["continuation"]["allow_twopass"] = True

    # F2：参考视频帧数必须继承 —— 第 2 段要按 C_n 跳帧。
    #
    # [为什么必须继承] 参考视频带检出的帧数在**上游那一段**里 ——
    # 不继承就没法跳帧；参考视频又从 0 开始喂（2026-11-19 实测的
    # 「段头跳变」修复在真实接续链上等于没生效）。
    # 参考视频帧数来自**上游那一段**的 continuation（worker 探测直接
    # 写进 continuation 字段的）—— 不依赖前端，前端不会带这个字段。
    try:
        upstream_ref_frames = int(
            source_info.get("ref_video_frame_count") or 0)
    except (TypeError, ValueError):
        upstream_ref_frames = 0
    if upstream_ref_frames > 0:
        params["continuation"]["ref_video_frame_count"] = \\
            upstream_ref_frames
    return ""
'''

# ---------------------------------------------------------------- F3
SERVER_OLD_3 = '''    window = cont.snap_context_frames(
        int(info.get("context_frames") or cont.DEFAULT_CONTEXT_FRAMES))
    if not window:
        window = cont.DEFAULT_CONTEXT_FRAMES
    anchor = cont.snap_context_frames(
        int(info.get("anchor_frames") or cont.DEFAULT_CONTEXT_FRAMES))
    if not anchor:
        anchor = cont.DEFAULT_CONTEXT_FRAMES
'''

SERVER_NEW_3 = '''    # ---- [2026-11-19 第 7 轮审查] ----
    # F3：畸形的 context_frames / anchor_frames 不得把 HTTP 打成 500。
    #
    # [原因] 这两行直接 int()，一个字段不对（前端传了字符串 / 数组）
    # 就直接 ValueError —— 用户点「生成」，服务端直接 500，
    # 用户既不知道自己填错了什么，也不知道该怎么改。
    #
    # —— 修法 —— 字段不对 -> 回退到默认值（接续上下文的钉住窗口必须
    # 对齐；对不齐就回退到默认值，不猜）。
    try:
        window = cont.snap_context_frames(
            int(info.get("context_frames") or cont.DEFAULT_CONTEXT_FRAMES))
    except (TypeError, ValueError):
        window = 0
    if not window:
        window = cont.DEFAULT_CONTEXT_FRAMES
    try:
        anchor = cont.snap_context_frames(
            int(info.get("anchor_frames") or cont.DEFAULT_CONTEXT_FRAMES))
    except (TypeError, ValueError):
        anchor = 0
    if not anchor:
        anchor = cont.DEFAULT_CONTEXT_FRAMES
'''

# ---------------------------------------------------------------- F3b
SERVER_OLD_3B = '''    duration = float(params.get("duration") or 5)
    segment_total = cont.align_frame_count(int(round(duration * cont.FPS)))
'''

SERVER_NEW_3B = '''    try:
        duration = float(params.get("duration") or 5)
    except (TypeError, ValueError):
        duration = 5.0
    segment_total = cont.align_frame_count(int(round(duration * cont.FPS)))
'''

# ---------------------------------------------------------------- F4
CS_OLD = '''        # 累计帧数（用于算拼接后的总长）
        "chain_total_frames": state["total_frames"],
        "segment_frames": int(info.get("segment_frames") or 0),
    }
'''

CS_NEW = '''        # 累计帧数（用于算拼接后的总长）
        "chain_total_frames": state["total_frames"],
        "segment_frames": int(info.get("segment_frames") or 0),
        # ---- [2026-11-19 第 7 轮审查] F4 ----
        # 参考视频的实际帧数一起下发（接续段要按 C_n 跳帧）。
        #
        # [为什么必须下发] 不下发 -> 第 2 段的参考视频仍然从 0 开始喂
        # （2026-11-19 里实测的「段头跳变」修复在真实接续链上等于没生效）。
        # 参考视频帧数来自**上游那一段**的 continuation（worker 探测直接
        # 写进 continuation 字段的）—— 不依赖前端，前端不会带这个字段。
        "ref_video_frame_count": int(
            info.get("ref_video_frame_count") or 0),
    }
'''

# ---------------------------------------------------------------- F5
AM_OLD = '''    first = loras[0]
    strength = first.get("strength")
    params["accel_loras"] = [{
        "key": first.get("name") or "",
        "name": first.get("name") or "",
        "strength": strength if strength is not None else 1.0,
        "enabled": True,
    }]
    params["fixed_accel_lora"] = first.get("name") or ""
    params["accel_lora"] = first.get("name") or ""
'''

AM_NEW = '''    first = loras[0]
    strength = first.get("strength")
    # ---- [2026-11-19 第 7 轮审查] F5 ----
    # 这里原来写的是**权重文件名**（如 dmad_minimax_h3_4step_full_critic_comfyui）。
    # 但工作流层认的是**目录 key**（dmad_4step_critic / ckpt850 ——
    # VIDEO_ACCEL_LORAS 里的那件），拿权重名去查 accel_lora_meta(key)
    # 返回 None -> 加速 LoRA 链**空编**（重试时把模型上挂着的那枚
    # LoRA 悄悄摘掉了，而且**没有任何报错**）。
    #
    # [后果] 认不出来时（来源与其具体体例参考）只写**认得出目录 key 的**值：
    #   * 认得出 -> 写 key（工作流层认得）；
    #   * 认不出 -> **不写**这个槽位（写权重名只会让工作流**默默忘掉**
    #     挂着的 LoRA）。
    slot_key = lora_key_for_weight(str(first.get("raw_name")
                                        or first.get("name") or ""))
    if not slot_key:
        slot_key = str(first.get("name") or "").strip()
        try:
            from . import video_models as _vm
            if not _vm.accel_lora_meta(slot_key):
                slot_key = ""
        except Exception:
            slot_key = ""
    if slot_key:
        params["accel_loras"] = [{
            "key": slot_key,
            "name": first.get("name") or "",
            "strength": strength if strength is not None else 1.0,
            "enabled": True,
        }]
        params["fixed_accel_lora"] = slot_key
        params["accel_lora"] = slot_key
'''


def patch_server() -> list[str]:
    path = REPO / "server.py"
    text = read_text(path)
    crlf_before = text.count("\r\n")
    lf_before = text.count("\n") - crlf_before
    text = sub(text, SERVER_OLD_1, SERVER_NEW_1, "F1+F2")
    text = sub(text, SERVER_OLD_3, SERVER_NEW_3, "F3")
    text = sub(text, SERVER_OLD_3B, SERVER_NEW_3B, "F3b")
    if not CHECK_ONLY:
        write_text(path, text)
    after = read_text(path)
    # 行尾校验：**不得出现裸 LF**（CRLF 文件里混进裸 LF 会让 .gitattributes
    # 的 -text 规则把整文件当二进制，diff 全毁）。
    bare_lf = after.count("\n") - after.count("\r\n")
    assert bare_lf == 0, f"server.py 混进了 {bare_lf} 个裸 LF"
    assert after.count("\r\n") >= crlf_before, "server.py 的 CRLF 行数变少了"
    return ["F1 allow_twopass 保留", "F2 ref_video_frame_count 继承",
            "F3 畸形 continuation 不再 500", "F3b duration 畸形不再抛异常"]


def patch_cs() -> list[str]:
    path = REPO / "h3studio" / "continuation_service.py"
    text = read_text(path)
    text = sub(text, CS_OLD, CS_NEW, "F4")
    if not CHECK_ONLY:
        write_text(path, text)
    return ["F4 build_continuation_context 下发 ref_video_frame_count"]


def patch_am() -> list[str]:
    path = REPO / "h3studio" / "artifact_meta.py"
    text = read_text(path)
    text = sub(text, AM_OLD, AM_NEW, "F5")
    if not CHECK_ONLY:
        write_text(path, text)
    return ["F5 accel_lora 写目录 key"]


def main() -> int:
    applied = patch_server() + patch_cs() + patch_am()
    print(("检查模式（未落盘）: " if CHECK_ONLY else "已应用: ")
          + "；".join(applied))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
