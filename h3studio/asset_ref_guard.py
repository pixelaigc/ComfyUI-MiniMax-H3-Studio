# -*- coding: utf-8 -*-
"""「媒体引用必须来自素材库」的后端闸门（唯一实现）。

【需求原文 —— 2026-11-16 用户要求】

> 首尾帧 / 参考生等功能的图片 / 音频 / 视频引用，与「图片编辑」保持一致 ——
> 外部素材必须先进入素材库，界面只能从素材库挑选并引用，
> 不允许功能直接引用外部文件路径。

「图片编辑」（i2i_qwen / i2i_h3image）已经实现了这条约束
（见 server.prepare_job_params 里 `code="ref_must_come_from_library"`），
但首尾帧（flf）/ 参考生视频（r2v）**没有** —— 它们仍然接受
`file_first` / `file_last` / `file_ref` / `file_refvid` / `file_refaud`
这五个 multipart 上传通道，即「直接引用外部文件路径」。

【本模块做什么】

把「某个媒体引用是不是素材库里已入库的素材」这件事收成一份实现，
三处共用：
  1. prepare_job_params —— 拒绝外部直传（flf / r2v）；
  2. 素材引用的存在性 / 类型核对 —— asset_refs 里的 asset_id 必须在库里
     且未删除；
  3. Agent 的 create_job —— 走 prepare_job_params，自动继承。

【为什么不能只在前端挡】前端挡不住「直接调 API」与「内置 Agent 建任务」。
后端是唯一的强制点。

【为什么用「库里有这个 id」而不是「id 长得像 asset_id」】素材 id 形如
`asset_<12 位 hex>`（见 h3studio.assets.new_asset_id）。只查前缀的话，
一个不存在的 id 也能过 —— 那时错误会推迟到 worker 里「素材不存在或已删除」，
用户看到的又是兜底文案。**查库**才能给出「该素材尚未入库素材库」这种
能照着改的中文原因。
"""

from __future__ import annotations

from . import assets as asset_store
from . import asset_repo


class AssetLibraryError(ValueError):
    """媒体引用不合法。message 是**可直接展示给用户**的中文原因。"""

    def __init__(self, message: str, code: str = "asset_not_in_library"):
        super().__init__(message)
        self.message = message
        self.code = code


#: 槽位 -> 期望的素材类别（用于类型核对）。
#:
#: 【为什么按槽位限定类别】参考视频槽位放一张 png 进去，
#: worker 会按视频去 ffmpeg 解它 -> 直接失败，且错误发生在
#: 「服务端未能给出具体原因」之后。提交时就该拦。
SLOT_CATEGORY = {
    "first_frame": "image",
    "last_frame": "image",
    "ref_image": "image",
    "character_ref": "image",
    "ref_video": "video",
    "source_video": "video",
    "ref_video_audio": "audio",
    "ref_audio": "audio",
    "bgm": "audio",
    "voice": "audio",
}

#: 参考音频 / 参考视频这两个槽位允许「视频当音频」吗？
#: —— 不允许。视频槽位只能是视频，音频槽位只能是音频。
#: （历史上 audioTrackAvailability 里对 refvid 槽位做了「兼容：直接放了音频」
#: 的兼容处理，那是**前端**的兜底；后端这里按严格语义拦。）


def _open():
    """打开素材库连接（素材库不可用时返回 None）。

    【为什么要容错】素材库挂载点（/mnt/usb2t/asset_library）可能没挂上。
    这时如果直接抛异常，所有建任务都不可用 —— 太粗暴。
    但**也不能放行**：放行等于「没有素材库也能引用外部路径」，
    正好违反需求。所以：查不到就**拒绝**，并给出可读原因。
    """
    try:
        return asset_store.connect()
    except Exception:
        return None


def lookup_asset(asset_id: str) -> dict | None:
    """按 id 查素材库里的条目（含已软删除的标记）。找不到返回 None。"""
    conn = _open()
    if conn is None:
        return None
    try:
        return asset_repo.get_asset(conn, asset_id, include_deleted=True)
    except Exception:
        return None
    finally:
        conn.close()


def check_asset_reference(asset_id: str, slot: str = "") -> dict:
    """校验一个 asset_id 确实是素材库里已入库的素材。

    返回素材元数据；不合法时抛 AssetLibraryError（带中文原因）。
    """
    asset_id = str(asset_id or "").strip()
    if not asset_id:
        raise AssetLibraryError(
            "该媒体尚未入库素材库，请先上传到素材库后再引用。",
            code="asset_id_missing")
    asset = lookup_asset(asset_id)
    if asset is None:
        raise AssetLibraryError(
            f"该媒体尚未入库素材库，请先上传到素材库后再引用"
            f"（素材库中找不到 {asset_id}）。",
            code="asset_not_in_library")
    if asset.get("deleted_at"):
        raise AssetLibraryError(
            f"素材 {asset.get('name') or asset_id} 已被删除（已进回收站），"
            f"请先在素材库恢复后再引用。",
            code="asset_deleted")
    expected = SLOT_CATEGORY.get(slot)
    if expected:
        actual = str(asset.get("category") or "")
        if actual != expected:
            label = {"image": "图片", "video": "视频",
                     "audio": "音频"}.get(expected, expected)
            raise AssetLibraryError(
                f"素材 {asset.get('name') or asset_id} 的类型是"
                f"「{actual or '未知'}」，不能用作{label}。"
                f"请在素材库里选一个{label}类型的素材。",
                code="asset_category_mismatch")
    return asset


def check_asset_refs(asset_refs) -> list[dict]:
    """校验一批素材引用（asset_refs）。返回**原样**的列表。

    【为什么原样返回而不是过滤】引用要么全部合法、要么整体报错。
    悄悄过滤掉一条 = 用户以为参考图有 3 张、实际只传了 2 张 ——
    占位标记与实际插槽错位，图 1 变成图 2，极难查。
    """
    refs = list(asset_refs or [])
    for item in refs:
        if not isinstance(item, dict):
            raise AssetLibraryError(
                "素材引用格式不正确：请从素材库选择素材后再引用。",
                code="asset_ref_malformed")
        slot = str(item.get("slot") or "").strip()
        check_asset_reference(str(item.get("asset_id") or ""), slot)
    return refs


def reject_external_media(saved_files: dict | None, mode: str) -> None:
    """拒绝任何「本地直传」的媒体文件。

    【为什么保留这个函数而不是直接删掉上传通道】上传通道本身
    （multipart 接收 file_first 等）还服务于别的用途 —— 素材库入库
    走的是 /api/assets（独立接口），不受影响。而这里的任务是
    「**生成任务**不许引用外部文件路径」。

    删掉接收分支会让老客户端（还在发 file_first）收到 aiohttp 的
    415/400 而不是一句中文原因 —— 保留分支并**明确报错**才是
    「不弱化既有行为 + 给出可执行原因」。
    """
    files = saved_files or {}
    if not files:
        return
    # 键 -> 中文名（面向用户的说法）
    labels = {
        "file_first": "首帧",
        "file_last": "尾帧",
        "file_ref": "参考图",
        "file_refvid": "参考视频",
        "file_refaud": "参考音乐",
    }
    for key, label in labels.items():
        if files.get(key):
            raise AssetLibraryError(
                f"{label}必须先入库素材库，不能直接引用本地文件。"
                f"请先在「素材库」页上传并入库，再从素材库选择{label}。",
                code="media_must_come_from_library")
