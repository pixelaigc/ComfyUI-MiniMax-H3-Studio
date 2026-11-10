# -*- coding: utf-8 -*-
"""提示词历史的**服务端落点**（供 server.py 调用）。

分成两件事：
  1. 从一次提交 / 一次增强里**还原出**「原始提示词」与「最终使用的提示词」
  2. 写进 prompt_history 表

【为什么单独成文件】
server.py 已经 3500 行，再堆逻辑会失控；
而这块逻辑需要被单测直接调用（不启 HTTP 服务）。
"""

from __future__ import annotations

import sqlite3

from . import prompt_history as ph


def panel_key_for_mode(mode: str) -> str:
    """生成模式 -> 面板 key（跨面板隔离的判据）。

    直接用 mode —— 它已经是后端真实注册的模式名
    （t2i / t2i_qwen / i2i_qwen / t2i_moss / t2v / flf / r2v / t2a / a2a），
    天然把「Z-Image 文生图」和「MOSS 文生图」分成两个面板。
    """
    return (mode or "").strip()


def model_key_for_params(params: dict) -> str:
    """从提交参数里取实际使用的模型 key（没有就空串）。

    图像面板用 params["model"]，视频面板用 params["model_key"]，
    音乐面板用 params["checkpoint"]。取不到不猜 —— 空串表示「未记录」。
    """
    if not isinstance(params, dict):
        return ""
    for key in ("model_key", "model", "checkpoint"):
        value = str(params.get(key) or "").strip()
        if value:
            return value
    return ""


def extract_prompt_pair(params: dict) -> tuple[str, str]:
    """从提交参数里还原 (原始提示词, 最终使用的提示词)。

    为什么要分两个字段：
      * 原始提示词 = 用户自己写的那段（历史面板的「填入原始」用它）
      * 最终使用的 = 真正送进模型的那段（可能已被增强替换/追加）

    判定口径：
      * params["original_prompt"] 是**权威**来源（前端在提交时带上，
        它记的是增强之前用户输入框里的原文）。
      * 没有它时退化：params["prompt"] 视为原始提示词，
        而最终提示词与它相同（未增强直出）。
    """
    if not isinstance(params, dict):
        return "", ""
    original = str(params.get("original_prompt") or "").strip()
    final = str(params.get("prompt") or "").strip()
    if original:
        return original, (final or original)
    # 没有 original_prompt：只能认为这一次是「未增强直出」。
    return final, final


def record_prompt_history(conn: sqlite3.Connection, mode: str, params: dict,
                          source: str = ph.SOURCE_GENERATE) -> int:
    """把一次提交 / 一次增强写进提示词历史。

    返回新记录 id（0 = 没写进去，例如提示词为空）。
    """
    panel_key = panel_key_for_mode(mode)
    if not panel_key:
        return 0
    original, final = extract_prompt_pair(params)
    if not original:
        return 0
    return ph.record(
        conn,
        panel_key=panel_key,
        original_prompt=original,
        enhanced_prompt=final,
        model_key=model_key_for_params(params),
        source=source,
    )
