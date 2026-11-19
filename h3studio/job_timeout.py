# -*- coding: utf-8 -*-
"""生成任务的**运行上限**策略（按媒体类型区分）。

【为什么需要这个模块 —— 2026-11-19 缺陷 1】

原实现只有一个全局阈值 `running_timeout_sec`（默认 30 分钟），
对**所有**任务一视同仁。于是：

  * 视频任务（文生视频 / 首尾帧 / 参考生视频 / 视频接续）跑得久是
    **正常现象** —— 本机 2080Ti 上 1080p 长片单段可能到 10 分钟量级，
    接续链多段叠加更长。用户明确要求：「视频任务运行很久是正常现象，
    即使跑 1 天也不应被判失败」。
  * 而 30 分钟一到，worker 直接 `raise RuntimeError(timeout_reason(...))`
    -> 任务被标 failed -> 用户看到「✕ 任务运行超过 30 分钟仍未完成，
    已判定为超时失败」。

【本模块的职责】

把「上限是多久」这个决策从 server.py 里抽出来，变成**纯函数**：

  * `timeout_for_media(media)` —— 按媒体类型给出上限秒数；
  * `media_of_mode(mode)` —— mode -> 媒体类型（image / video / audio）；
  * `timeout_reason(timeout_sec, media)` —— 生成中文原因文案。

【设计约束 —— 与 running_truth / comfy_errors 同一纪律】

  * **纯函数、无 IO**：不连 ComfyUI、不查盘、不读环境变量。
    单元测试完全离线可跑。
  * **视频默认不限时**：`VIDEO_TIMEOUT_SEC = 0` 表示「不按时长判失败」。
    但**丢失判定照旧**（任务从 ComfyUI 队列里消失 -> 明确失败），
    所以不会退回「永远运行中」的老坑 —— 那是 A5 需求要消灭的东西。
  * **图像 / 音频保留上限**：图像与音频本机实测是秒级 ~ 分钟级，
    超过上限基本等于「卡死了」，保留阈值能更快给用户一个明确的失败。
    阈值同样可配置。
"""

from __future__ import annotations


# ------------------------------------------------------------------
# 默认阈值（集中管理 —— 唯一真源）
# ------------------------------------------------------------------

#: 图像 / 音频类任务的运行上限（秒）。默认 30 分钟。
#:
#: 【为什么保留】图像（Z-Image / Qwen / MOSS / H3-Image）与音乐（YuE2）
#: 在本机是**秒级 ~ 分钟级**完成。跑满 30 分钟基本等于卡死
#: （ComfyUI 侧显存不足挂住、节点死锁等），保留阈值能让用户更快
#: 拿到一个明确的失败而不是无限干等。
#:
#: 【可配置】config.json 的 `running_timeout_sec` 覆盖它；
#: `running_timeout_video_sec` 单独覆盖视频类。
DEFAULT_TIMEOUT_SEC = 1800.0

#: 视频类任务的运行上限（秒）。**默认 0 = 不按时长判失败**。
#:
#: 【为什么是 0】用户明确要求：「视频任务运行很久是正常现象，
#: 即使跑 1 天也不应被判失败」。设成非 0 就会回到 30 分钟误判的老坑。
#:
#: 【为什么 0 不会造成「永远运行中」】时长判定只是**四种**判定之一：
#:   * 任务仍在 ComfyUI 队列里 -> ACTIVE（继续等）；
#:   * history 已有结果 -> FINISHED（收尾）；
#:   * ComfyUI 读不到 -> UNKNOWN（不瞎猜）；
#:   * 队列与 history 都没有 -> LOST（明确失败）。
#: 「丢失」判定与时长**无关**，所以视频任务不会卡死 —— 它要么在跑，
#: 要么被 ComfyUI 丢掉（判失败）。
VIDEO_TIMEOUT_SEC = 0.0

#: 视频类任务的**丢失宽限期**（秒）—— 沿用全局值。
DEFAULT_LOST_GRACE_SEC = 10.0

#: 支持的媒体类型。
MEDIA_IMAGE = "image"
MEDIA_VIDEO = "video"
MEDIA_AUDIO = "audio"
MEDIA_OTHER = "other"

#: 媒体类型 -> 默认上限（秒）。
DEFAULT_TIMEOUT_BY_MEDIA = {
    MEDIA_IMAGE: DEFAULT_TIMEOUT_SEC,
    MEDIA_AUDIO: DEFAULT_TIMEOUT_SEC,
    MEDIA_VIDEO: VIDEO_TIMEOUT_SEC,
    MEDIA_OTHER: DEFAULT_TIMEOUT_SEC,
}


# ------------------------------------------------------------------
# mode -> 媒体类型
# ------------------------------------------------------------------

#: mode -> 媒体类型的**静态映射**。
#:
#: 【为什么不直接依赖 gen_types】gen_types 的 media 字段是权威，
#: 但它按「生成类型」注册表分组（t2v / flf / r2v / longfilm 都是 video），
#: 而这里需要的是**按任务产出物**判定 —— 同一个 mode 可能产出不同媒体
#: 的边缘情况（例如 longfilm 产出视频）。本映射与 gen_types 保持一致，
#: 并且**在判定时优先查 gen_types**（它是运行时的唯一真源）。
MODE_MEDIA = {
    "t2i": MEDIA_IMAGE,
    "t2i_qwen": MEDIA_IMAGE,
    "t2i_moss": MEDIA_IMAGE,
    "t2i_h3image": MEDIA_IMAGE,
    "i2i_qwen": MEDIA_IMAGE,
    "i2i_h3image": MEDIA_IMAGE,
    "t2v": MEDIA_VIDEO,
    "flf": MEDIA_VIDEO,
    "r2v": MEDIA_VIDEO,
    "longfilm": MEDIA_VIDEO,
    "t2a": MEDIA_AUDIO,
    "a2a": MEDIA_AUDIO,
}


def media_of_mode(mode: str) -> str:
    """mode -> 媒体类型（image / video / audio / other）。

    【口径】优先查生成类型注册表（gen_types 是运行时的唯一真源，
    新增模式时自动跟上）；查不到再退回静态映射；再查不到就是 other。

    【为什么 unknown 也归 other 而不是 video】unknown 的上限与图像
    一样（保守），不能因为「猜错了类型」就把一个本该有时限的任务
    变成无限时。
    """
    key = str(mode or "").strip()
    if not key:
        return MEDIA_OTHER
    try:
        from . import gen_types
        info = gen_types.type_for_mode(key)
        media = str(info.get("media") or "")
        if media in (MEDIA_IMAGE, MEDIA_VIDEO, MEDIA_AUDIO):
            return media
    except Exception:
        pass
    return MODE_MEDIA.get(key, MEDIA_OTHER)


def timeout_for_media(media: str, *,
                      image_timeout_sec: float = DEFAULT_TIMEOUT_SEC,
                      video_timeout_sec: float = VIDEO_TIMEOUT_SEC) -> float:
    """媒体类型 -> 运行上限（秒）。0 = 不按时长判失败。

    参数是**配置值**（由 server.py 从 config.json 读出后传入），
    本函数只做选择，不自己读配置 —— 保持纯函数。
    """
    media_key = str(media or MEDIA_OTHER)
    if media_key == MEDIA_VIDEO:
        try:
            return float(video_timeout_sec)
        except (TypeError, ValueError):
            return VIDEO_TIMEOUT_SEC
    try:
        return float(image_timeout_sec)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SEC


def timeout_reason(timeout_sec: float, media: str = MEDIA_OTHER) -> str:
    """超时的中文原因（把秒数换算成分钟，用户看得懂）。

    【口径纪律 —— 2026-11-19】文案里**必须**体现「时长明确」，
    且**不得**出现「超过 30 分钟」这类写死的话术 —— 阈值可配置后，
    写死的分钟数会与实际阈值不一致（用户看到的与真实行为对不上）。
    """
    try:
        seconds = float(timeout_sec)
    except (TypeError, ValueError):
        seconds = DEFAULT_TIMEOUT_SEC
    if seconds <= 0:
        return ("该任务已超过运行上限而被终止（当前上限：不按运行时长限制，"
                "仅在 ComfyUI 侧任务丢失时判定失败）。请检查 ComfyUI 队列状态，"
                "确认任务是否仍在运行。")
    minutes = max(1, int(round(seconds / 60.0)))
    return (f"任务运行超过 {minutes} 分钟仍未完成，已判定为超时失败。"
            "本机 ComfyUI 可能仍在占用显卡，请先在 ComfyUI 界面确认队列状态，"
            "再重新提交生成。")
