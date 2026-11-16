# -*- coding: utf-8 -*-
"""音频**续接时序**的纯函数层（首尾帧 / 参考生视频专用）。

【为什么单独一个模块】
「音轨来源三选一」只存在于 flf / r2v（t2v 恒为生成音频，音画同源）。
而当音轨来源是「参考音频 / 参考视频的音轨」时，那条音轨是**贴上去**的
—— 它不参与 DiT 采样，所以「音画对齐」纯粹是**后处理**问题。

这个模块把那段后处理的**时间轴账**算清楚。它是纯函数：不查盘、不连
ComfyUI、不跑 ffmpeg —— 这样边界情形（音频短于视频 / 长于视频 / 每段
时长不等）都能离线单测。

【口径 —— 只有一条，且必须精确】
    t_a_start(n) = C_n / 24
        C_n = 第 n 段之前**已累计交付**的帧数（整数帧）
        24 = H3 固定帧率（continuation.FPS）
    → C_n 是整数帧，t_a_start(n) 与 skip_first_frames = C_n **严格对应**，
      不存在浮点累计误差。

【两条链路必须分开 —— 这是本模块最重要的一条】
  * **采样用**（喂给 H3 的 ref_audios 槽位）：`1s 空白 ⊕ 原始音频切片`
    —— 这一段该「听到」的东西。1s 空白是题面要求的前导静音。
  * **成片用**（合并成片时的音轨）：**原始参考音频整条**
    —— 整部片子该「听到」的东西。长度按合并后视频长度（ffmpeg
    `-af apad -shortest`：视频长补静音、视频短截断）。

    若把采样用的切片当成片音轨，每段开头那 1s 空白会**累积**
    （3 段 = 3s 空白）→ 音画错位。这是本模块存在的根本原因。

【参考视频的音轨（video_bg）】
同样规则，但对**参考视频**做**音画同步**切割：
    视频侧：skip_first_frames = C_n（帧级）
    音频侧：start_time        = C_n / 24（秒级）
因为参考视频已归一到 24fps（force_rate=24），两者指向同一时刻。
"""

from __future__ import annotations

#: H3 固定帧率（与 continuation.FPS 同值 —— 这里是本地常量以避免
#: 循环 import；两处必须一致，测试里有一致性断言）。
FPS = 24

#: 段音频的前导空白（秒）。题面要求 1 秒。
LEAD_BLANK_SECONDS = 1.0


# ------------------------------------------------------------------
# 基本换算
# ------------------------------------------------------------------
def frames_to_seconds(frames: int) -> float:
    """整数帧 -> 秒。

    【为什么不在这里做四舍五入到「毫秒」】t_a_start(n) = C_n / 24
    是**精确**的分数；真正要落地成秒的地方（VHS_LoadAudioUpload.start_time）
    由调用方给出 float。保留 float 精度，避免「先转秒再转帧」时
    出现 1 帧误差。
    """
    return int(frames) / float(FPS)


def seconds_to_frames(seconds: float) -> int:
    """秒 -> 整数帧（四舍五入）。

    用于「参考视频的 skip_first_frames」这类必须落在帧上的口径。
    """
    return int(round(float(seconds) * FPS))


# ------------------------------------------------------------------
# 一段接续的音频时间轴
# ------------------------------------------------------------------
def segment_audio_plan(*, consumed_frames: int,
                       segment_frames: int,
                       context_frames: int,
                       source_duration: float | None = None,
                       lead_blank: float = LEAD_BLANK_SECONDS) -> dict:
    """算一段接续的音频时间轴。

    参数：
      consumed_frames  本段之前已累计**交付**的帧数（口径 B 的 C_n）
      segment_frames   本段生成的总帧数（17k+5 网格）
      context_frames   钉住窗口（帧）；接续段交付 = 总帧数 - 钉住帧数
      source_duration  原始参考音频/参考视频的总时长（秒）；None = 未知
      lead_blank       本段音频的前导空白（秒）

    返回（全部是可直接落地的数值）：
      {
        "consumed_frames":     C_n,
        "delivered_frames":    本段交付帧数（= segment_frames - context_frames），
        "segment_start":       本段在成片时间轴上的起点（秒）= C_n / 24,
        "segment_duration":    本段交付时长（秒）= delivered_frames / 24,
        "source_start":        从原始音频取的起点（秒）= C_n / 24,
        "source_duration":     从原始音频取的时长（秒）= delivered_frames / 24,
        "skip_frames":         参考视频跳帧数（= C_n，整数帧）,
        "lead_blank":          前导空白（秒）,
        "sampling_duration":   **采样用**音频总长（秒）= lead_blank + segment_duration,
        "available_source":    原始音频从 source_start 起还剩多少秒（None = 未知）,
        "shortfall":           素材不够时缺多少秒（0 = 够）,
        "warnings":            [中文提示, ...],
      }
    """
    consumed = max(0, int(consumed_frames))
    total = max(0, int(segment_frames))
    window = max(0, int(context_frames))
    delivered = max(0, total - window)

    segment_start = frames_to_seconds(consumed)
    segment_duration = frames_to_seconds(delivered)

    warnings: list[str] = []

    # 素材还剩多少（够不够这一段）
    available = None
    shortfall = 0.0
    if source_duration is not None:
        available = max(0.0, float(source_duration) - segment_start)
        if available < segment_duration - 1e-6:
            shortfall = segment_duration - available
            warnings.append(
                f"第 {consumed // FPS + 1} 段需要 {segment_duration:.3f}s 音频，"
                f"但原始音轨从 {segment_start:.3f}s 起只剩 {available:.3f}s —— "
                f"缺 {shortfall:.3f}s，该段末尾会补静音（不会错位，但会短一截）。")

    # 钉住窗口 ≥ 本段帧数 -> 拷贝桥会 raise（提前给中文提示）
    if window > 0 and window >= total:
        warnings.append(
            f"钉住窗口 {window} 帧 ≥ 本段总帧数 {total} 帧 —— "
            f"拷贝桥会拒绝（没有新内容可生成）。请加长本段时长或缩小钉住窗口。")

    return {
        "consumed_frames": consumed,
        "delivered_frames": delivered,
        "segment_start": round(segment_start, 6),
        "segment_duration": round(segment_duration, 6),
        "source_start": round(segment_start, 6),
        "source_duration": round(segment_duration, 6),
        "skip_frames": consumed,
        "lead_blank": round(float(lead_blank), 6),
        "sampling_duration": round(float(lead_blank) + segment_duration, 6),
        "available_source": (None if available is None
                             else round(available, 6)),
        "shortfall": round(shortfall, 6),
        "warnings": warnings,
    }


def plan_for_chain(*, chain_frames_before: int,
                   segment_frames: int,
                   context_frames: int,
                   source_duration: float | None = None,
                   lead_blank: float = LEAD_BLANK_SECONDS) -> dict:
    """从「链上已累计帧数」出发的薄封装（语义更清晰的一层）。

    chain_frames_before = 上游各段交付帧数之和（= 口径 B 的 C_n）。
    """
    return segment_audio_plan(
        consumed_frames=chain_frames_before,
        segment_frames=segment_frames,
        context_frames=context_frames,
        source_duration=source_duration,
        lead_blank=lead_blank,
    )


# ------------------------------------------------------------------
# 边界校验（提交前用 —— 明确中文提示，不静默错位）
# ------------------------------------------------------------------
def validate_audio_alignment(*, source_duration: float | None,
                             plan: dict,
                             total_video_frames: int | None = None) -> str:
    """返回**空串 = 通过**；否则是给用户看的中文原因。

    覆盖的边界：
      * 原始音频比成片短 / 长；
      * 某段音频切片超出原始音频末尾；
      * 生成段与音频段时长不一致（以画面为准，明确说清）。
    """
    notes: list[str] = []

    if source_duration is not None and total_video_frames:
        total_seconds = frames_to_seconds(total_video_frames)
        delta = float(source_duration) - total_seconds
        if delta < -1e-6:
            notes.append(
                f"原始参考音轨比成片短 {abs(delta):.3f}s "
                f"（音轨 {float(source_duration):.3f}s / 成片 {total_seconds:.3f}s）"
                f" —— 成片尾部会补静音。")
        elif delta > 1e-6:
            notes.append(
                f"原始参考音轨比成片长 {delta:.3f}s "
                f"（音轨 {float(source_duration):.3f}s / 成片 {total_seconds:.3f}s）"
                f" —— 超出部分会在成片里被截断。")

    for item in plan.get("warnings") or []:
        notes.append(str(item))

    return "；".join(notes)


def chain_audio_state(*, track_source: str,
                      source_kind: str,
                      source_file: str,
                      source_duration: float | None,
                      consumed_frames: int) -> dict:
    """「音轨来源」在接续链上的**权威状态**（落进 params["continuation"]）。

    【为什么以链首段为准】
    nonebot 的做法是「整部长视频共用第 1 段的 audio_decision」
    （h3_chain_video.py:1677-1681）。若中途改音轨来源，前一段的
    采样音频与后一段就对不上 —— 所以这里明确记录「以链首段为准」。

    track_source: "generated" / "reference" / "video_bg"
    source_kind:  "ref_audio"（独立参考音频）/ "ref_video"（参考视频的音轨）
    """
    return {
        "audio_track_mode": str(track_source or ""),
        "audio_source_kind": str(source_kind or ""),
        "audio_source_file": str(source_file or ""),
        "audio_source_duration": (None if source_duration is None
                                  else round(float(source_duration), 6)),
        "audio_consumed_frames": max(0, int(consumed_frames)),
        "audio_lead_blank": LEAD_BLANK_SECONDS,
    }


def is_external_track(track_source: str) -> bool:
    """这条音轨是不是「贴上去的」（不参与采样）？

    只有 external 档才需要按偏移切片；generated 档音画同源，不切。
    """
    return str(track_source or "") in ("reference", "video_bg")
