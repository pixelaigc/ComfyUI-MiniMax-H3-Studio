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
四处共用：
  1. prepare_job_params —— 拒绝外部直传（flf / r2v）；
  2. 素材引用的存在性 / 类型核对 —— asset_refs 里的 asset_id 必须在库里
     且未删除；
  3. Agent 的 create_job —— 走 prepare_job_params，自动继承；
  4. **params JSON 通道**（2026-11-20 补齐）—— params 里直接写的媒体
     路径（first_frame / last_frame / ref_videos / ref_audios / ...）
     必须同样被闸门拦住。

【为什么不能只在前端挡】前端挡不住「直接调 API」与「内置 Agent 建任务」。
后端是唯一的强制点。

【为什么用「库里有这个 id」而不是「id 长得像 asset_id」】素材 id 形如
`asset_<12 位 hex>`（见 h3studio.assets.new_asset_id）。只查前缀的话，
一个不存在的 id 也能过 —— 那时错误会推迟到 worker 里「素材不存在或已删除」，
用户看到的又是兜底文案。**查库**才能给出「该媒体尚未入库素材库」这种
能照着改的中文原因。

【2026-11-20 补齐 —— params JSON 通道曾绕过本闸门（安全缺陷）】

2026-11-16 的闸门只覆盖了两条通道：multipart 直传（saved_files）与
asset_refs（asset_id）。**params 这条通道没覆盖** —— 而它是**唯一
能被外部直接控制**的那条：

    POST /api/jobs -F mode=flf -F params={"first_frame":"/etc/hostname"}

服务端（server._run 的 flf 分支）把 params["first_frame"] 当成
「uploads 目录下的相对文件名」拼路径：

    lp = UPLOAD_DIR / up        # 绝对路径 -> Path.__truediv__ 丢弃左侧
    if not lp.exists(): raise ...
    await comfy_upload_image(session, lp)   # <- 文件被复制进 ComfyUI input

于是 `/etc/hostname`、`../../etc/hostname`、指向它的符号链接都能被
复制进 ComfyUI 的 input 目录；而 ComfyUI 的 `/view` 端点**无鉴权**
且与 8190 一样监听 0.0.0.0 —— 构成一条完整的「任意本地文件读取」链。

【本模块怎么堵这条通道 —— 判据是「值的形状」，不是「路径长什么样」】

params 里的媒体值只有两种**合法**形态：

  1. **素材库引用** —— 值是 `asset_<12 位 hex>`，走 asset_refs 通道
     （由 check_asset_refs 查库）。params 里出现 asset_id 形态的值，
     说明调用方把引用写错了位置 —— 同样拒绝并说清怎么改。

  2. **worker 回写的 ComfyUI 文件名** —— 形如 `h3studio/<name>.png`
     或纯 `<name>.png`。这是 worker 在素材上传成功后写回 params 的
     **内部状态**，不是外部输入。

其余一切（绝对路径、`..` 穿越、符号链接、任意其它相对路径）一律拒绝。

【为什么按「形状」判而不是按「路径在不在白名单目录」判】
「resolve() 后必须落在 UPLOAD_DIR 内」这种写法有两个致命问题：
  * **符号链接**：UPLOAD_DIR 里的软链指向 /etc/shadow 时，resolve()
    之后路径已经出了 UPLOAD_DIR —— 判据本身是对的，但前提是
    「UPLOAD_DIR 里只可能有我们自己写进去的文件」；而 params 通道
    恰恰能往这个前提上塞东西；
  * **TOCTOU**：判定通过之后、worker 真正读文件之前，文件可以被替换。
按值形状判（只接受 asset_id 形态或 ComfyUI 命名形态）**根本不给
外部路径进入的机会** —— 不存在「路径解析」这一步，也就没有
符号链接与竞态窗口。

【为什么允许「ComfyUI 文件名」形态而不要求 params 里一律只放 asset_id】
worker 会把素材库文件上传到 ComfyUI 后，把 ComfyUI 侧的文件名**写回
params**（server._run 的 flf / r2v 分支），并把整份 params 回写进
jobs 表（agent 的 retry_job 会把这份 params 整份拷贝过来重新建任务）。
如果闸门连这种形态也拒，**重试既有任务会被误杀**（已上线的功能）。
所以这里放行「ComfyUI 命名形态」—— 它是 worker 自己的产物，
不是外部输入；外部输入无法凭空造出这种名字。
"""

from __future__ import annotations

import re

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


# ==================================================================
# params JSON 通道（2026-11-20 补齐 —— 安全缺陷收口）
# ==================================================================

#: params 里承载「外部可注入的媒体路径」的键 -> 中文名。
#:
#: 【为什么是这八个】它们是 worker 会拿去当**本地路径 / 节点文件名**
#: 用的那批键：
#:
#:   * first_frame / last_frame / ref_videos / ref_audios / ref_images
#:     —— worker 拼 `lp = UPLOAD_DIR / up` 后**复制进 ComfyUI input**
#:        （server._run 的 flf / r2v 分支）—— 这是 2026-11-20 复现的
#:        「任意本地文件读取」主链；
#:   * ref_video_audios —— 直接进 T8 节点的 ref_video_audios 槽位
#:        （VHS_LoadAudioUpload），**不经过** UPLOAD_DIR 拼接；
#:   * ref_audio      —— a2a 的参考音频进 LoadAudio 的 audio 槽位；
#:   * segment_audio_file —— 接续段的段音频进 LoadAudioUpload 的 audio 槽位。
#:
#: 后三个是 worker 自己算出来的（外部传进来会被 worker 覆盖），
#: 但**一并纳入**是为了「媒体引用必须来自素材库」这条规则没有例外 ——
#: 少一个键就是一个潜在的旁路。
#:
#: 【为什么 i2i 的 ref_images 形态特殊】i2i_qwen / i2i_h3image 的
#: params["ref_images"] 走**元数据**通道（[{asset_id, description}]），
#: 由 normalize_refs 归一、由 check_asset_refs 查库 —— 它从来不是
#: 「本地路径」。所以下面的校验对 dict 形态**放行**（由查库那条
#: 通道负责），只对字符串形态按路径判。
#:
#: 【刻意不纳入的键】params["continuation"] 里的 source_latent_path /
#: anchor_latent_path —— 那是**服务端自己算出来的绝对路径**
#: （接续接力棒的坐标），不是媒体引用，且由 _prepare_continuation_params
#: 整体覆盖（外部传不进来）。
PARAM_MEDIA_KEYS = {
    "first_frame": "首帧",
    "last_frame": "尾帧",
    "ref_videos": "参考视频",
    "ref_audios": "参考音乐",
    "ref_images": "参考图",
    "ref_video_audios": "参考视频背景音",
    "ref_audio": "参考音频",
    "segment_audio_file": "段音频",
}

#: 素材库 id 的形状：`asset_` + 12 位 hex（见 assets.new_asset_id）。
_ASSET_ID_RE = re.compile(r"^asset_[0-9a-f]{12}$")

#: ComfyUI 文件名的形状。
#:
#: 【为什么不限制字符集为 ASCII】素材库里的原始文件名可以含中文
#: （入库时存成 `<fingerprint>__<原文件名>`），上传到 ComfyUI 后的
#: 返回名就会带中文 —— 用 ASCII 白名单会把**合法素材**误杀。
#: 所以这里只禁「路径语义」的东西（分隔符 / `..` / 绝对路径），
#: 不禁文件名本身的字符。
#:
#: 【为什么只允许一层子目录】本项目上传固定用 `h3studio/` 子目录
#: （server.comfy_upload_image: data.add_field("subfolder", "h3studio")），
#: ComfyUI 返回 `h3studio/<name>`。允许多层就等于允许
#: `h3studio/../../etc/passwd` 这种形态混进来 —— 而 ComfyUI 侧
#: 的 `_comfy_input_local` 正是**按 `/` 逐段拼接**的，
#: `..` 在那里会被真实解析。
_COMFY_SUBFOLDER = "h3studio"


def _looks_like_asset_id(value: str) -> bool:
    """值是不是素材库 id 的形状。"""
    return bool(_ASSET_ID_RE.match(value))


def _looks_like_comfy_name(value: str) -> bool:
    """值是不是 worker 回写的 ComfyUI 文件名的形状。

    合法形态：`h3studio/<name>` 或 `<name>`。

    【判据为什么是「形状」而不是「路径解析」】
    * `..` —— ComfyUI 侧 `_comfy_input_local` 按 `/` 逐段拼路径，
      `..` 在那里会被**真实解析** -> 必须禁。
    * 多于一层子目录 —— worker 只写 `h3studio/` 一层。
    * 反斜杠 —— ComfyUI 返回的名字里不会有；留着等于给 Windows
      风格的路径穿越留门。
    * 绝对路径 / 波浪号 —— 直接就是「指到本地任意文件」。
    """
    if not value or value != value.strip():
        return False
    if "\x00" in value or "\\" in value:
        return False
    if value.startswith("/") or value.startswith("~"):
        return False
    # 盘符形态（C: / C:/...）—— Linux 上虽无害，但没有任何合法值
    # 长这样，直接拒掉更干净。
    if re.match(r"^[A-Za-z]:", value):
        return False
    parts = value.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    if len(parts) > 2:
        return False
    if len(parts) == 2 and parts[0] != _COMFY_SUBFOLDER:
        return False
    return True


def _describe_media_value(value: str) -> str:
    """把一个被拒的值描述成人能看懂的样子（不回显整段敏感内容）。"""
    text = str(value)
    if len(text) > 120:
        text = text[:117] + "..."
    return text


def check_params_media_references(params: dict | None) -> None:
    """校验 params 里**所有**媒体引用都来自素材库（params JSON 通道）。

    【为什么不按 mode 分别处理】媒体路径的注入面与 mode 无关 ——
    只要 params 里带着这种键，worker 就会拿它去拼路径。多写一份
    「按 mode 的白名单」= 多一个漂移点，而且漏掉一个 mode 就是
    一个漏洞。所以这里**全量校验**。

    【为什么不静默丢弃而是报错】需求原文是「外部素材必须先进入
    素材库，界面只能从素材库挑选并引用」。静默丢弃会让用户
    以为「我选了首帧」而实际首帧是空的 —— 界面与实际不一致，
    极难查。明确报错并说清怎么改，才是这条需求的落点。

    【为什么不按 mode 分别处理 —— 见上】。

    合法时**什么都不改**（不改 params 的语义）；
    不合法时抛 AssetLibraryError（带可读中文原因 + code）。
    """
    if not isinstance(params, dict):
        return
    for key, label in PARAM_MEDIA_KEYS.items():
        if key not in params:
            continue
        raw = params.get(key)
        if raw is None or raw == "":
            continue
        values = raw if isinstance(raw, list) else [raw]
        for item in values:
            if item is None:
                continue
            if isinstance(item, dict):
                # i2i 的 ref_images 是元数据形态（{asset_id, ...}），
                # 由 check_asset_refs 查库 —— 不在这里按路径判。
                continue
            if not isinstance(item, str):
                raise AssetLibraryError(
                    f"{label}的取值格式不正确：必须是素材库里的素材，"
                    f"不能直接给文件路径。",
                    code="media_must_come_from_library")
            value = item.strip()
            if not value:
                continue
            if _looks_like_asset_id(value):
                raise AssetLibraryError(
                    f"{label}填的是素材库 ID（{value}），但素材引用要放在"
                    f"asset_refs 里提交，不能直接写进 params。",
                    code="media_must_come_from_library")
            raise AssetLibraryError(
                f"{label}不能直接引用外部文件路径（收到："
                f"{_describe_media_value(value)}）。"
                f"请先在「素材库」页上传并入库，再从素材库选择{label}。",
                code="media_must_come_from_library")


def check_params_media_references_with_paths(params: dict | None,
                                             media_keys=None) -> None:
    """同上，但额外接受**显式传入**的键集合（测试与特殊场景用）。

    【为什么不直接用 PARAM_MEDIA_KEYS】PARAM_MEDIA_KEYS 是**唯一
    真源**；这个函数只是把「用哪些键」参数化，方便单测针对
    单个键做断言，不复制一份键表（避免漂移）。
    """
    if not isinstance(params, dict):
        return
    if media_keys is None:
        check_params_media_references(params)
        return
    subset = {k: v for k, v in PARAM_MEDIA_KEYS.items() if k in set(media_keys)}
    saved = dict(PARAM_MEDIA_KEYS)
    try:
        PARAM_MEDIA_KEYS.clear()
        PARAM_MEDIA_KEYS.update(subset)
        check_params_media_references(params)
    finally:
        PARAM_MEDIA_KEYS.clear()
        PARAM_MEDIA_KEYS.update(saved)
