# -*- coding: utf-8 -*-
"""H3 视频的**音轨来源**选择 —— 首尾帧 / 参考生视频共用。

【要解决的问题】
B2 需求：「音轨三选一」：
    (a) 使用生成的音乐；
    (b) 使用参考音乐作为成片音轨；
    (c) 使用参考视频的背景音乐作为成片音轨。
并且：「没有参考音乐 / 参考视频时该项不出现」。

【为什么要有独立模块】
这三个选项的**可见性**取决于「有没有对应的参考素材」，
而「实际选哪个」决定了成片音轨接谁。这两件事必须由**同一份判定**
决定，否则会出现「界面能选但工作流里没接」或者
「界面不显示但工作流接了」的错位 —— 那是用户报的「选了不生效」的同款问题。

【三种音轨来源的语义】
  generated  —— 用 H3 自己生成的音频（VAEDecodeAudio 的输出）。
                这是 H3 的原生音画同源路径：音画一起被采样出来。
  reference  —— 用参考音乐作为成片音轨（参考音乐只做**音轨**，
                不参与 DiT 采样 —— 它是被**贴**到成片上的）。
  video_bg   —— 用参考视频的背景音乐作为成片音轨（同上，是被贴上去的）。

【重要区别：生成 vs 贴】
  * generated：音频是采样出来的，和画面同源 -> 音画天然同步。
  * reference / video_bg：音频是**外部贴上去**的 -> 长度可能与画面不等，
    所以要裁剪/补齐到视频时长（见 trim_to_video_duration）。
"""

from __future__ import annotations

#: 三种音轨来源
TRACK_GENERATED = "generated"
TRACK_REFERENCE = "reference"
TRACK_VIDEO_BG = "video_bg"

#: 全部合法值
VALID_TRACK_SOURCES = (TRACK_GENERATED, TRACK_REFERENCE, TRACK_VIDEO_BG)

#: 默认：生成的音乐（保持老行为 —— H3 音画同源）
DEFAULT_TRACK_SOURCE = TRACK_GENERATED


def available_track_sources(has_reference_audio: bool,
                            has_reference_video_audio: bool) -> list[str]:
    """按「有没有对应的参考素材」算出**当前可选**的音轨来源。

    【为什么这是唯一的可见性判据】
    「参考音乐」选项在没有任何参考音乐时出现，选了也没有素材可接 ——
    那是假选项（置灰/可点但无效）。所以它只在**真有**参考音乐时出现。
    同理「参考视频背景音」只在真有带音轨的参考视频时出现。

    (a) 生成的音乐：永远可选（H3 自己就能生成音轨）。
    """
    out = [TRACK_GENERATED]
    if has_reference_audio:
        out.append(TRACK_REFERENCE)
    if has_reference_video_audio:
        out.append(TRACK_VIDEO_BG)
    return out


def normalize_track_source(value: str | None,
                           has_reference_audio: bool,
                           has_reference_video_audio: bool) -> str:
    """把用户选的音轨来源归一到**当前真的可用**的值。

    非法值 / 选了但没有对应素材 -> 回落到默认（生成的音乐）。
    **不报错**：音轨来源是个锦上添花的选项，为它拒绝整个任务
    是本末倒置（用户想要视频，音轨退化成默认即可）。
    """
    text = str(value or "").strip()
    available = available_track_sources(has_reference_audio,
                                        has_reference_video_audio)
    if text in available:
        return text
    return DEFAULT_TRACK_SOURCE


def track_source_label(source: str) -> str:
    """音轨来源 -> 界面显示名。"""
    return {
        TRACK_GENERATED: "使用生成的音乐",
        TRACK_REFERENCE: "使用参考音乐作为成片音轨",
        TRACK_VIDEO_BG: "使用参考视频的背景音乐作为成片音轨",
    }.get(source, "使用生成的音乐")


def track_source_note(source: str) -> str:
    """音轨来源 -> 一句话说明（界面提示用）。"""
    return {
        TRACK_GENERATED: "音轨由 H3 与画面一同采样生成，音画天然同步。",
        TRACK_REFERENCE: "参考音乐被贴到成片上（不参与采样），"
                         "长度不足会补静音、超出会裁到画面时长。",
        TRACK_VIDEO_BG: "参考视频的背景音乐被贴到成片上（不参与采样），"
                        "长度不足会补静音、超出会裁到画面时长。",
    }.get(source, "")
