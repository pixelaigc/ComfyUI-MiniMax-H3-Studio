# -*- coding: utf-8 -*-
"""
内置 Agent —— 聊天区**媒体上传**接口。

【为什么单独一个模块】
上传是「HTTP 层的边界」（multipart 解析 / 类型校验 / 大小上限 / 落盘），
与 Agent 的编排逻辑（多步推理 / 工具链 / 任务下发）是**两种关注点**。
混进 agent.py 会让那个已经很长的文件更难读，而两者又必须共享
「素材库」这一份权威数据源 —— 所以：
  * 本模块只负责「把上传的图片变成素材库条目（asset_id）」
  * 落库/去重/出缩略图**复用** h3studio.asset_service.register_asset
    （与素材库页的 /api/assets 上传**完全同一条链路**，不另造一套）
  * 上下文注入在 h3studio/agent.py 里做（那是 Agent 的地盘）

【与素材库页上传的差异 —— 为什么需要这个接口而不是直接用 /api/assets】
素材库页的上传是「批量入库 + 打标签 + 分类」，语义是「整理素材库」。
Agent 聊天区的上传语义是「**这一条消息的附件**」，有额外约束：
  * 只收图片（用户是要给参考图，不是要归档视频）
  * 数量上限（一次对话能挂几张参考图）
  * 单文件大小上限比素材库宽松？不 —— **更严**（见 MAX_IMAGE_BYTES）
  * 入库来源标记成 agent_upload，界面上能看出是聊天附件
但**落库本身完全复用** register_asset —— 这样去重、缩略图、
元数据探测、转码兜底全部一致，不会出现「Agent 传的图和素材库传的图
行为不一样」。

【安全边界】
  * 扩展名白名单 + **魔数嗅探**双重校验（只看扩展名会被
    「把 .exe 改名成 .png」骗过去）
  * 大小上限按**流式读取时**累计，超了就立刻停并删临时文件
  * 文件名不落盘（原名只作为素材名存进库），杜绝路径穿越
  * 落在素材库 tmp 区，靠 register_asset 做原子移动
"""
from __future__ import annotations

import uuid
from pathlib import Path

from h3studio import analysis_jobs
from h3studio import asset_repo
from h3studio import asset_service
from h3studio import assets as asset_store
from h3studio.assets import LibraryError

#: 允许上传的图片扩展名（用户要求：至少 png/jpg/webp）。
#: 这里再带上 jpeg/bmp/gif —— 它们都是标准图片格式，浏览器能预览，
#: PIL 也能解码；把它们拒掉对用户没有任何好处。
ALLOWED_IMAGE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif",
}

#: 允许上传的音频扩展名。
#:
#: 【选型依据 —— 素材库能探测 + 深度管线能读】
#:   wav / mp3 / flac —— 无损或高兼容，ffmpeg + librosa + soundfile 全支持
#:   m4a / aac      —— iPhone 录音默认格式（ffmpeg 解码没问题）
#:   ogg / opus     —— 语音与游戏音效常用
#:   aiff / wma     —— 补几个常见的老格式
#: 判定「能不能处理」看的是**能不能解出采样率/时长**（media_probe.probe_audio），
#: 深度管线再在其上做 BPM/乐器/人声/ASR。
ALLOWED_AUDIO_SUFFIXES = {
    ".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".aiff", ".aif", ".wma",
}

#: 允许上传的视频扩展名。
#:
#: 【为什么含 mov / mkv】
#: iPhone 拍的是 .mov，ComfyUI/ffmpeg 都能解；mkv 是录制常用容器。
#: 真正能不能用由 ffprobe 说了算（见 _detect_video_kind）。
ALLOWED_VIDEO_SUFFIXES = {
    ".mp4", ".webm", ".mov", ".mkv", ".m4v", ".avi",
}

#: 一次上传允许的**总**媒体类型集合（图片 + 音频 + 视频）。
ALLOWED_MEDIA_SUFFIXES = (
    ALLOWED_IMAGE_SUFFIXES | ALLOWED_AUDIO_SUFFIXES | ALLOWED_VIDEO_SUFFIXES)

#: 媒体类型 -> 素材库 category（与 assets.classify 的语义一致）。
MEDIA_CATEGORY = {
    "image": "image",
    "audio": "audio",
    "video": "video",
}

#: 单张图片大小上限（字节）。
#:
#: 【为什么比素材库的 8 GB 严得多】
#: 素材库是「归档」，用户可能丢几个 G 的视频进去；
#: 而聊天附件是「参考图」，Qwen-Image / MOSS 的参考图会被整张读进
#: 推理上下文 —— 一张 50 MB 的图会直接把推理服务的显存/内存吃爆。
#: 20 MB 足够覆盖手机原图与绝大多数设计稿，且足够安全。
MAX_IMAGE_BYTES = 20 * 1024 * 1024

#: 单个音频文件大小上限（字节）—— 100 MB。
#:
#: 【为什么音频可以比图片宽 5 倍】
#: 音频走的是**深度管线**（BPM/乐器/人声/ASR），管线内部自己重采样到
#: 16k/48k —— 不需要把原文件整段读进推理上下文。
#: 但 librosa/soundfile 载入仍要占内存：100 MB 的 wav ≈ 50 MB PCM，
#: 完全在可控范围。超过这个量级，用户应该先裁剪。
MAX_AUDIO_BYTES = 100 * 1024 * 1024

#: 单个视频文件大小上限（字节）—— 500 MB。
#:
#: 【为什么视频可以到 500 MB】
#: 视频分析是**抽帧**（每 2 秒 1 帧，最多 24 帧）—— 分析侧只吃几十张
#: JPEG，不吃原视频；原视频只是落盘 + 生成一张封面。
#: 500 MB 覆盖手机 4K 短视频（1 分钟约 200~400 MB）的绝大多数场景。
#: 再大就该走素材库页的归档上传（上限 8 GB），不适合当聊天附件。
MAX_VIDEO_BYTES = 500 * 1024 * 1024

#: 一次上传最多几张（用户要求：建议上限 9 张，与 Qwen-Image 参考图对齐）。
#:
#: 【为什么不直接用 ref_images.MAX_REF_IMAGES = 10】
#: Qwen-Image 官方能力上限是 10 张，而**素材库槽位**的上限也是 10。
#: 但 Agent 一次要「上传 + 引用 + 建任务」，给用户在交互层留 1 张余量
#: 可以避免「刚好卡在 10 张边界」时的边界困惑（9 → 10 还能再加 1 张，
#: 10 → 11 直接被拒，用户会觉得莫名其妙）。
#: 真实能力上限仍然是 10（服务端会再校验一次），这里只是交互层的软上限。
#:
#: 【图片 / 音视频分开计数 —— 2026-11-11】
#: 之前是「所有媒体共用 9 个」的额度。现在拆成两组：
#:   * 图片 9 张（与 Qwen-Image 参考图上限对齐，语义不变）
#:   * 音视频合计 4 个（参考音频 1 段 + 参考视频 3 个 = H3 参考生成节点的
#:     ref_audios 0~3 / ref_videos 0~3 上限）
#: 为什么音视频额度更小：H3 视频模式**每个**参考视频/音频都要整段
#: 参与推理（不是抽帧），上传太多会拖垮 ComfyUI 侧；
#: 而参考图只进图像编码器。分开计数也让报错信息能说清是哪一类超了。
MAX_IMAGES_PER_UPLOAD = 9

#: 一次上传最多几个**音视频**（音频 + 视频合计）。
#:
#: 【为什么是 4】
#: H3 参考生成节点的真实槽位上限（实测 /object_info）：
#:   ref_images 0~9 / ref_videos 0~3 / ref_video_audios 0~3 / ref_audios 0~3
#: 参考视频与参考音频各能到 3，合起来给 4 留了一点余量，
#: 同时避免用户一次拖进来十几个几百 MB 的视频把队列压垮。
MAX_MEDIA_FILES_PER_UPLOAD = 4

#: 魔数嗅探表：(扩展名, 头部字节, 最小长度)。
#:
#: 【为什么要嗅探 —— 不能只信扩展名】
#: 扩展名是客户端给的，攻击者可以把任意内容改名成 .png 上传。
#: 只看扩展名 = 素材库里会出现「名为 png 实为任意二进制」的条目。
#: 所以这里核对**文件真实格式**的头部字节：
#:   PNG  -> \x89PNG\r\n\x1a\n
#:   JPEG -> \xff\xd8\xff
#:   WEBP -> RIFF....WEBP
#:   BMP  -> BM
#:   GIF  -> GIF87a / GIF89a
#: 只接受**确实是对应格式**的文件；GIF 只查头（动画 GIF 合法）。
MAGIC_TABLE = (
    (".png", (b"\x89PNG\r\n\x1a\n",), 8),
    (".jpg", (b"\xff\xd8\xff",), 3),
    (".jpeg", (b"\xff\xd8\xff",), 3),
    (".webp", (b"RIFF",), 12),      # 真正的 WEBP 标记在 offset 8
    (".bmp", (b"BM",), 2),
    (".gif", (b"GIF87a", b"GIF89a"), 6),
)

#: WEBP 的完整魔数：offset 0 是 RIFF，offset 8 是 WEBP。
WEBP_RIFF = b"RIFF"
WEBP_TAG = b"WEBP"

# ------------------------------------------------------------------ 音视频魔数

#: 音频魔数表：(扩展名, 头部字节集合, 最小长度)。
#:
#: 【为什么音频也要嗅探】
#: 音视频是**二进制容器**，扩展名几乎不携带可信信息 ——
#: 把 .mp4 改名成 .wav、或者把 .exe 改名成 .mp3 都很容易。
#: 素材库后面要用 ffprobe 真的去解码它（拿时长/采样率），
#: 所以在入口处先挡一道「这根本不是音频」比让它在分析阶段炸掉好。
#:
#: 【各格式的头部】
#:   WAV  -> "RIFF"...."WAVE"
#:   MP3  -> "ID3" 或 0xFF 0xEx/0xFx 帧同步
#:   FLAC -> "fLaC"
#:   OGG  -> "OggS"
#:   MP4/M4A/M4V -> offset 4 是 "ftyp"
#:   AIFF -> "FORM"...."AIFF"
#:   WMA -> 0x30 0x26 0xB2 0x75
AUDIO_MAGIC_TABLE = (
    (".wav", (b"RIFF",), 4),          # 真正的 WAVE 标记在 offset 8
    (".mp3", (b"ID3",), 3),
    (".flac", (b"fLaC",), 4),
    (".ogg", (b"OggS",), 4),
    (".opus", (b"OggS",), 4),
    (".m4a", (b"",), 0),              # 用 ftyp 判定（见 _detect_audio_kind）
    (".aac", (b"",), 0),              # ADTS：0xFFF 同步字
    (".aiff", (b"FORM",), 4),
    (".aif", (b"FORM",), 4),
    (".wma", (b"\x30\x26\xb2\x75",), 4),
)

#: 视频魔数表：(扩展名, 头部字节集合, 最小长度)。
#:
#: 【关键现实：视频容器不是靠固定魔数区分的】
#: mp4 / mov / m4v 都是 ISO-BMFF（offset 4 = "ftyp"），
#: webm / mkv 都是 Matroska（头部是 0x1A45DFA3），
#: avi 是 "RIFF"...."AVI "。
#: 所以**光看魔数分不出 mp4 和 mov** —— 它们本来就是同一个家族。
#:
#: 【那怎么区分「这确实是视频」而不是「随便一个二进制」】
#: 判据分两层：
#:   1) 魔数对得上容器家族（ftyp / EBML / RIFF+AVI）
#:   2) 容器家族对得上扩展名 —— mp4/mov/m4v 都是 ftyp，webm/mkv 都是 EBML，
#:      avi 是 RIFF+AVI。**扩展名与家族不符就拒**（.mkv 其实是 mp4 -> 拒）。
#: 这样既不误伤（mp4 改名为 mov 仍可用，因为它们同家族），
#: 又挡住了「.mp4 里装的是 exe」。
VIDEO_MAGIC_TABLE = (
    (".mp4", (b"ftyp",), 4),
    (".m4v", (b"ftyp",), 4),
    (".mov", (b"ftyp",), 4),
    (".webm", (b"\x1a\x45\xdf\xa3",), 4),
    (".mkv", (b"\x1a\x45\xdf\xa3",), 4),
    (".avi", (b"RIFF",), 4),
)

#: WAV 的完整标记：offset 0 是 RIFF，offset 8 是 WAVE。
WAVE_TAG = b"WAVE"

#: AVI 的完整标记：offset 0 是 RIFF，offset 8 是 AVI 。
AVI_TAG = b"AVI "

#: ISO-BMFF 家族的 ftyp 标记（在 offset 4）。
FTYP_TAG = b"ftyp"

#: Matroska/WebM 的 EBML 标记。
EBML_MAGIC = b"\x1a\x45\xdf\xa3"

#: MP3 帧同步字（0xFF + 0xE0 掩码的高 11 位）。
MP3_FRAME_SYNC = 0xFFE0

#: AAC ADTS 帧同步字（0xFFF + 0xF0 掩码的高 12 位）。
AAC_FRAME_SYNC = 0xFFF0


#: 媒体类型 -> 中文标签（报错与前端提示共用）。
_KIND_LABEL = {
    "image": "图片",
    "audio": "音频",
    "video": "视频",
}


class UploadError(Exception):
    """上传被拒。message 面向用户，可直接读。"""

    def __init__(self, message: str, code: str = "invalid_upload",
                 status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def _detect_image_kind(filename: str, head: bytes) -> str | None:
    """
    按**头部字节**判断这是什么图片格式。

    返回规范化扩展名（".png" / ".jpg" / ".webp" / ".bmp" / ".gif"），
    不匹配任何已知格式时返回 None。

    【为什么不信任扩展名】见 MAGIC_TABLE 的说明。
    """
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".webp":
        # WEBP 必须同时满足 RIFF 头 + offset 8 的 WEBP 标记
        if (len(head) >= 12 and head[:4] == WEBP_RIFF
                and head[8:12] == WEBP_TAG):
            return ".webp"
        return None
    for ext, magics, min_len in MAGIC_TABLE:
        if ext == ".webp":
            continue
        if len(head) < min_len:
            continue
        for magic in magics:
            if head.startswith(magic):
                return ext
    return None


def _detect_audio_kind(filename: str, head: bytes) -> str | None:
    """
    按**头部字节**判断这是什么音频格式。

    返回规范化扩展名（".wav" / ".mp3" / ...），不匹配返回 None。

    【为什么不信任扩展名】见 AUDIO_MAGIC_TABLE 的说明。

    【MP3 的两种头】MP3 文件可能带 ID3v2 标签（"ID3" 开头），
    也可能没有（直接是帧同步字 0xFF 0xE0~0xFF）。两种都要认。
    """
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".wav":
        if (len(head) >= 12 and head[:4] == WEBP_RIFF
                and head[8:12] == WAVE_TAG):
            return ".wav"
        return None
    if suffix == ".aiff" or suffix == ".aif":
        if (len(head) >= 12 and head[:4] == b"FORM"
                and head[8:12] == b"AIFF"):
            return suffix
        return None
    if suffix == ".m4a":
        # m4a 是 ISO-BMFF 里的「只有音轨的 mp4」—— 头部同样是 ftyp
        if len(head) >= 8 and head[4:8] == FTYP_TAG:
            return ".m4a"
        return None
    if suffix == ".aac":
        # ADTS 裸流：0xFF 0xF?
        if len(head) >= 2 and (int.from_bytes(head[:2], "big")
                               & AAC_FRAME_SYNC) == AAC_FRAME_SYNC:
            return ".aac"
        return None
    if suffix == ".mp3":
        if head.startswith(b"ID3"):
            return ".mp3"
        if len(head) >= 2 and (int.from_bytes(head[:2], "big")
                               & MP3_FRAME_SYNC) == MP3_FRAME_SYNC:
            return ".mp3"
        return None
    for ext, magics, min_len in AUDIO_MAGIC_TABLE:
        if ext in (".wav", ".aiff", ".aif", ".m4a", ".aac", ".mp3"):
            continue
        if len(head) < min_len:
            continue
        for magic in magics:
            if magic and head.startswith(magic):
                return ext
    return None


def _detect_video_kind(filename: str, head: bytes) -> str | None:
    """
    按**头部字节**判断这是什么视频容器。

    返回规范化扩展名（".mp4" / ".webm" / ".mov" / ...），不匹配返回 None。

    【为什么要按「容器家族」而不是按精确魔数】
    mp4 / mov / m4v 都是 ISO-BMFF（ftyp），webm / mkv 都是 Matroska（EBML），
    avi 是 RIFF+AVI。分不出同家族的具体格式 —— 也不需要分：
    同一家族内改扩展名不影响 ffprobe 解码，素材库会按真实内容探测。

    【但家族必须与扩展名相符】
    .mkv 里装 mp4 -> 拒。理由是**元数据会写错**：
    register_asset 用扩展名猜 mime（assets.guess_mime），
    .mkv 装 mp4 会被记成 video/x-matroska，而 ComfyUI 侧按 mime 选
    加载节点时会挑错容器。宁可拒掉，也不要留一条元数据错乱的素材。
    """
    suffix = Path(filename or "").suffix.lower()
    if len(head) < 8:
        return None
    if suffix in (".mp4", ".m4v", ".mov"):
        # ISO-BMFF：offset 4 是 "ftyp"
        return suffix if head[4:8] == FTYP_TAG else None
    if suffix in (".webm", ".mkv"):
        # Matroska / WebM
        return suffix if head[:4] == EBML_MAGIC else None
    if suffix == ".avi":
        if head[:4] == WEBP_RIFF and head[8:12] == AVI_TAG:
            return ".avi"
        return None
    return None


def _media_kind_for_suffix(suffix: str) -> str:
    """扩展名 -> 媒体类型（image / audio / video）。不属于任何一类返回 ""。"""
    if suffix in ALLOWED_IMAGE_SUFFIXES:
        return "image"
    if suffix in ALLOWED_AUDIO_SUFFIXES:
        return "audio"
    if suffix in ALLOWED_VIDEO_SUFFIXES:
        return "video"
    return ""


def _validate_suffix(filename: str) -> str:
    """
    扩展名白名单校验。被拒时抛 UploadError（消息面向用户）。

    【为什么要按类型给不同的措辞】
    报错必须让用户知道**自己错在哪一类**：
    「不支持的图片类型 .txt」比「不支持的文件类型 .txt」有用得多
    （用户可能是想传音频，结果后缀打错了）。
    而音视频的扩展名与图片不重叠，所以一个 .txt 会被归到「无法归类」，
    那时给出三类各自的清单。
    """
    suffix = Path(filename or "").suffix.lower()
    if suffix in ALLOWED_MEDIA_SUFFIXES:
        return suffix

    images = "、".join(sorted(ALLOWED_IMAGE_SUFFIXES))
    audios = "、".join(sorted(ALLOWED_AUDIO_SUFFIXES))
    videos = "、".join(sorted(ALLOWED_VIDEO_SUFFIXES))

    # 后缀能看出用户**想**传哪一类（按同族近似），就用那类的措辞。
    if suffix in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif",
                  ".tiff", ".heic", ".heif", ".svg"):
        raise UploadError(
            f"不支持的图片类型 {suffix}，只接受 {images}",
            code="unsupported_type")
    if suffix in (".wma", ".amr", ".mid", ".midi", ".aiff", ".ape", ".mka"):
        raise UploadError(
            f"不支持的音频类型 {suffix}，只接受 {audios}",
            code="unsupported_type")
    if suffix in (".flv", ".wmv", ".rm", ".rmvb", ".ts", ".m2ts", ".3gp"):
        raise UploadError(
            f"不支持的视频类型 {suffix}，只接受 {videos}",
            code="unsupported_type")

    raise UploadError(
        f"不支持的文件类型 {suffix or '(无扩展名)'}，"
        f"只接受 图片（{images}）、音频（{audios}）、视频（{videos}）",
        code="unsupported_type")


def _max_bytes_for_kind(kind: str) -> int:
    """按媒体类型取单文件大小上限。"""
    if kind == "audio":
        return MAX_AUDIO_BYTES
    if kind == "video":
        return MAX_VIDEO_BYTES
    return MAX_IMAGE_BYTES


async def _read_and_validate_part(part, tmp_dir: Path) -> dict:
    """
    把一个 multipart 文件 part 流式落到临时区并做全部校验。

    返回 {"tmp": Path, "filename": str, "suffix": str, "bytes": int,
          "kind": "image"|"audio"|"video", "sha256": str}；
    被拒时抛 UploadError（临时文件已清理）。

    【三类媒体的校验顺序】
      1. 扩展名白名单（图片 / 音频 / 视频各一套）
      2. 流式落盘 + 按**该类型**的大小上限累计（超了立刻停）
      3. 魔数嗅探：文件**真的**是那个格式吗
    """
    filename = Path(part.filename or "upload.png").name  # 只取 basename
    suffix = _validate_suffix(filename)
    kind = _media_kind_for_suffix(suffix)
    max_bytes = _max_bytes_for_kind(kind)

    tmp = tmp_dir / f".agent-upload-{uuid.uuid4().hex}.tmp"
    total = 0
    head = b""
    overflow = False
    try:
        with tmp.open("wb") as handle:
            while True:
                chunk = await part.read_chunk(256 * 1024)
                if not chunk:
                    break
                if not head:
                    head = chunk[:32]
                handle.write(chunk)
                total += len(chunk)
                if total > max_bytes:
                    overflow = True
                    break
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise UploadError(f"读取文件失败：{type(exc).__name__}: {exc}"[:200],
                          code="read_failed", status=500) from exc

    if overflow:
        tmp.unlink(missing_ok=True)
        limit_mb = max_bytes // (1024 * 1024)
        raise UploadError(
            f"{_KIND_LABEL.get(kind, '文件')}超过上限 {limit_mb} MB",
            code="file_too_large", status=413)
    if total <= 0:
        tmp.unlink(missing_ok=True)
        raise UploadError("文件为空", code="empty_file")

    # --- 魔数嗅探：文件**真的**是那个格式吗？ ---
    if kind == "image":
        detected = _detect_image_kind(filename, head)
    elif kind == "audio":
        detected = _detect_audio_kind(filename, head)
    else:
        detected = _detect_video_kind(filename, head)

    if detected is None:
        tmp.unlink(missing_ok=True)
        raise UploadError(
            f"文件内容不是有效的{_KIND_LABEL.get(kind, '媒体')}文件"
            "（扩展名与真实格式不符）",
            code="invalid_media_content")
    # 真实格式与扩展名不一致时以**真实格式**为准（例如 .png 其实是 jpeg），
    # 避免 mime 写错导致下游 PIL/ComfyUI 读图失败。
    real_suffix = detected
    real_kind = _media_kind_for_suffix(real_suffix) or kind

    return {"tmp": tmp, "filename": filename, "suffix": real_suffix,
            "bytes": total, "kind": real_kind}


def _register_one(item: dict) -> dict:
    """
    把一个已落盘的临时文件入库（复用素材库的 register_asset）。

    返回 {"asset_id", "name", "preview_url", "duplicated", "bytes",
          "kind", "width", "height", "duration_sec", ...}。

    【三类媒体走同一套入库】
    category 必须是**真实的媒体类型**（image / audio / video）：
      * 素材库的筛选器按 category 分（category=audio 才能筛出音频）
      * analysis.run_analysis 按 category 选分析器
      * 缩略图生成按 category 分支（图片出缩略图 / 视频抽首帧 / 音频画波形）
    写死 category="image" 会让音视频被记成图片 —— 后面 ffprobe 探测失败、
    缩略图空白、分析器选错。**这是本次扩展最关键的一处改动。**

    【为什么刻意不写 note】
    ref_api._describe 会把 note 当成**描述的兜底**（还标成 "analysis" 来源）。
    如果这里写「Agent 聊天区上传的附件」，素材库里每个附件的「描述」
    都会变成这句固定文案 —— 模型会以为那就是内容本身。
    来源信息应该走 source 字段（= "agent_upload"），
    描述只能来自**真正的分析结果**。
    """
    kind = item.get("kind") or "image"
    category = MEDIA_CATEGORY.get(kind, "image")
    # subtype 让素材库页的分类有意义（与 assets.classify 的语义一致）：
    #   图片 -> photo / 音频 -> music / 视频 -> clip
    subtype = {"image": "photo", "audio": "music", "video": "clip"}.get(
        kind, "photo")

    with asset_store.connect() as conn:
        try:
            result = asset_service.register_asset(
                conn,
                source_path=item["tmp"],
                filename=item["filename"],
                category=category,
                subtype=subtype,
                note=None,
                source="agent_upload",
                creator="agent",
            )
        except LibraryError as exc:
            raise UploadError(exc.message, code=exc.code,
                              status=exc.status) from exc

        asset = result.get("asset") or {}
        asset_id = asset.get("id") or ""
        version = result.get("version") or {}
        if not version:
            # 【去重路径 —— 实测踩过的坑】
            # register_asset 命中 sha256 去重时返回的是
            #   {"asset": <既有条目>, "duplicated": True}
            # **没有 version 键** —— 于是 version.get("preview_path") 是 None，
            # preview_url 变成空 -> 前端 <img src=""> -> 缩略图**显示不出来**。
            #
            # 实测：同一张图传两次，第二次缩略图是坏的。
            # 修法：去重时**自己去查一次当前版本**（current_version）。
            version = asset_repo.current_version(conn, asset_id) or {}
        preview_url = (f"/api/assets/{asset_id}/preview"
                       if version.get("preview_path") else None)
        return {
            "asset_id": asset_id,
            "name": asset.get("name") or item["filename"],
            "preview_url": preview_url,
            "duplicated": bool(result.get("duplicated")),
            "bytes": item["bytes"],
            "kind": kind,
            "category": category,
            "width": version.get("width"),
            "height": version.get("height"),
            "duration_sec": version.get("duration_sec"),
            "mime": version.get("mime"),
            "codec": version.get("codec"),
            "sample_rate": version.get("sample_rate"),
            "channels": version.get("channels"),
        }


#: 预处理（分析）的等待上限（秒）。
#:
#: 【为什么是 300 秒】
#: 实测本机 Qwen 多模态分析一张 512x512 的图约需 **120 秒**（本地推理，
#: iq3 量化小模型看图 + 生成长描述）。9 张图排队（RUNNER 是单 worker）
#: 最坏要 9×120 ≈ 1080 秒 —— 但那是**全部串行**的极端情况。
#:
#: 【为什么不把上限调大到能覆盖 9 张全串行】
#: HTTP 请求挂 18 分钟，用户早就走了（前端还有 20 分钟的 SSE 超时）。
#: 更好的做法是**逐张处理**（见 handle_upload 的处理顺序），
#: 并让每张图的等待有独立上限 —— 超时就明确报错，而不是无限等。
#:
#: 【为什么不用素材库那套异步 job + 前端轮询】
#: 素材库页的「深度分析」是**用户主动点**的异步操作，轮询没问题；
#: 而 Agent 上传是**用户等着用**的同步流程 —— 他上传完就要发给 Agent。
#: 同步等待 + 明确失败，比「先返回、后台慢慢跑、Agent 拿到空描述」更符合
#: 用户预期（约束 3：失败要明确报错，不得静默降级）。
PREPROCESS_TIMEOUT = 300.0

def _extract_tags_from_description(text: str, limit: int = 8) -> list[str]:
    """
    从分析文本里抽出**关键词**作为素材标签。

    【为什么需要标签】
    用户约束：「描述文本与标签可被 Agent 检索/引用」。
    描述能检索，但标签是素材库的**结构化索引** —— search_assets 的
    关键词检索会命中标签，Agent 才能用「雨夜」「人像」这类词找到这张图。

    【怎么抽 —— 不调模型】
    再调一次模型抽标签 = 一次上传两次推理（+120 秒）。
    而分析文本本身是**结构化**的（【主体人物】【场景与环境】…），
    直接从它的结构里切就能拿到有用的标签，零额外成本。

    【抽什么 —— 小节标题 + 小节正文里的关键词】
    小节标题（「主体人物」「场景与环境」）是**固定的结构名**，
    10 张图抽出来全都一样 —— 检索价值≈0（「主体人物」匹配任何图）。
    真正有检索价值的是**正文里的内容词**（「像素化」「马赛克」「极简主义」）。

    所以：
      * 小节标题：保留但**降权**（排在内容词之后，且同名只留一次）
      * 小节正文：抽**实义词**（去掉「的」「是」「画面中」这类虚词）
    这样「主体人物 / 像素化 / 马赛克 / 极简主义」才是能命中的标签。

    抽出来的标签会**真的写进素材库**（asset_repo.add_tags），
    于是它和用户在素材库页手动打的标签**完全同构** ——
    Agent 检索、素材库筛选、引用计数全部自动生效。
    """
    import re

    if not text:
        return []

    # --- 1) 按小节切开：标题 + 正文 ---------------------------------
    #
    # 【小节标题的两种格式 —— 实测踩过的坑】
    # 同一个模型在两次调用里输出过**两种**格式：
    #   A: **【主体人物】**   （带 markdown 强调）
    #   B: 【主体人物】      （不带）
    # 原来的 pattern 要求结尾的 `**`，于是 B 格式**一个标题都匹配不到**
    # -> sections 为空 -> 标签全空（生产实测踩到）。
    #
    # 所以：**只匹配【...】本身**，markdown 强调符号交给后面的
    # `re.sub(r"[*_`#>\\-•]", " ", body)` 清理。
    #
    # 【第三种格式：markdown 标题 —— 2026-11-11 音频扩展踩到】
    # 深度音频管线（analyze_audio.py 的 render_text）产出的结构是
    # **markdown 二级标题**，不是【】：
    #     ## 一、音频基本信息
    #     ## 二、节拍与速度
    #     ## 三、乐器与音色
    # 只认【】的话，音频分析文本会抽出**零个**标签 ——
    # 而标签是素材库的结构化索引（Agent 靠它检索），
    # 音频素材就会在素材库里「搜不到」。
    # 所以这里**两种结构都认**：先切【】小节，再切 markdown 标题小节。
    sections: list[tuple[str, str]] = []
    pattern = re.compile(r"【\s*([^】]{1,20})\s*】")
    matches = list(pattern.finditer(text))
    for index, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append((title, text[start:end]))

    # markdown 标题小节（## / ### + 标题文字）
    # 标题文字里常带「一、」「二、」这类序号 —— 切掉（它们不是关键词）
    md_pattern = re.compile(r"^#{1,4}\s*(.+?)\s*$", re.MULTILINE)
    md_matches = list(md_pattern.finditer(text))
    for index, match in enumerate(md_matches):
        title = re.sub(r"^[一二三四五六七八九十\d]+[、.．)]\s*", "",
                       match.group(1).strip())
        title = re.sub(r"[*_`#]", "", title).strip()
        start = match.end()
        end = (md_matches[index + 1].start()
               if index + 1 < len(md_matches) else len(text))
        sections.append((title, text[start:end]))

    content_tags: list[str] = []
    heading_tags: list[str] = []

    # --- 2) 正文里的实义词 -----------------------------------------
    # 中文没有空格分词，用「去虚词 + 取 2-6 字片段」近似。
    stop = {"无", "没有", "画面中", "整体", "这个", "一个", "以下", "该",
            "其中", "可以", "以及", "并且", "但是", "所以", "因为", "如果",
            "没有", "似乎", "仿佛", "呈现", "显示", "感到", "看到", "位于",
            "上方", "下方", "左侧", "右侧", "中央", "边缘", "部分", "一些",
            "非常", "十分", "比较", "较为", "略微", "稍微",
            "包括", "包含", "颜色", "材质", "感觉", "构成", "填充", "边缘",
            # 以「的」开头的片段是定语残片（「的方格阵列」），不是独立关键词
            "的", "地", "得"}
    for title, body in sections:
        # 去掉 markdown 强调符号与项目符号
        clean = re.sub(r"[*_`#>\-•]", " ", body)
        # 「包括」「呈现」这类动词会把两个词粘在一起（「颜色包括橄榄绿」），
        # 在它们处切开，才能拿到「橄榄绿」这种**真正可检索**的词。
        clean = re.sub(r"(包括|包含|呈现|显示|构成|填充|组成|使用|带有)",
                       " ", clean)
        for chunk in re.split(r"[，。；：、,.;:\n()（）\[\]「」“”\"'\s]+", clean):
            chunk = chunk.strip()
            if not (2 <= len(chunk) <= 8):
                continue
            if chunk in stop or chunk in content_tags:
                continue
            # 以「的」开头 -> 定语残片，不是关键词
            if chunk.startswith("的"):
                continue
            # 含「或」「和」「及」的片段是并列结构，不是单一关键词
            if any(ch in chunk for ch in "或和及与"):
                continue
            # 全是虚词的片段不要（「的了是在」这种）
            if all(ch in stop for ch in chunk):
                continue
            content_tags.append(chunk)

    # --- 3) 小节标题（降权，放后面） --------------------------------
    for title, _body in sections:
        if title and title not in heading_tags and not title.startswith("无"):
            heading_tags.append(title)

    # 内容词优先，标题补位
    tags = content_tags[:max(0, limit - 2)] + heading_tags
    # 去重保序
    seen: set[str] = set()
    final: list[str] = []
    for tag in tags:
        if tag in seen:
            continue
        seen.add(tag)
        final.append(tag)
    return final[:limit]


def _apply_tags(asset_id: str, tags: list[str]) -> list[str]:
    """把标签**写进素材库**（与用户手动打的标签同构）。"""
    if not tags:
        return []
    try:
        with asset_store.connect() as conn:
            asset_repo.add_tags(conn, asset_id, tags, creator="agent_media")
        return list(tags)
    except Exception:  # noqa: BLE001
        return []


def _preprocess_asset(asset_id: str) -> dict:
    """
    对已入库的素材做**同步预处理**：跑分析管线 -> 写描述 -> 写标签。

    返回 {"ok": bool, "description": str, "tags": list, "error": str}。

    【失败怎么办 —— 明确报错，不静默降级】
    用户约束 3：「入库或预处理失败要明确报错，不得静默降级为
    『未入库的裸文件』」。所以这里失败时 ok=False + 具体原因，
    由 handle_upload 转成 errors 里的明确条目。

    【为什么复用素材库的管线】
    直接调 analysis.run_analysis（而不是 start_analysis + 轮询）：
      * 同步等待，语义简单
      * 失败有明确的 LibraryError 原因
      * 产出落在同一张 asset_analyses 表，素材库页看得到

    【2026-11-11：图片仍然同步，音视频走异步】
    音视频的预处理耗时差一个数量级：
      * 图片（多模态）：实测 120~180 秒/张
      * 视频（抽帧 + 多模态）：抽 24 帧 + 模型看帧，**更慢**
      * 音频（深度管线 BPM/乐器/人声/ASR）：实测 116 秒素材 ~600 秒
    同步等 600 秒会把上传请求挂死，用户早就走了。
    所以音视频走**素材库既有的异步 job 通道**（analysis_jobs），
    前端轮询 /api/assets/{id}/analysis-status。
    """
    from h3studio import analysis as analysis_mod
    from h3studio import asset_service

    try:
        with asset_store.connect() as conn:
            asset = asset_repo.get_asset(conn, asset_id)
            version = asset_repo.current_version(conn, asset_id)
            if not asset or not version:
                return {"ok": False, "description": "", "tags": [],
                        "error": "素材入库后读不到版本记录"}
            source = analysis_jobs._source_of(asset_id, version)
            if source is None or not Path(source).is_file():
                return {"ok": False, "description": "", "tags": [],
                        "error": "素材文件已丢失，无法预处理"}

            result = _run_analysis_sync(analysis_mod, asset, version,
                                        Path(source))
            text = (result or {}).get("text") or ""

            revision = asset_repo.next_revision(conn, asset_id)
            extra = dict((result or {}).get("extra") or {})
            extra["analyzer_mode"] = "multimodal"
            extra["preprocessed_by"] = "agent_media"
            asset_repo.save_analysis(
                conn, asset_id, result.get("kind", "image"), text, revision,
                provider=result.get("provider"),
                prompt=result.get("prompt", ""),
                frames=result.get("frames") or [], extra=extra)
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"[:200]
        return {"ok": False, "description": "", "tags": [],
                "error": f"素材预处理失败：{detail}"}

    tags = _apply_tags(asset_id, _extract_tags_from_description(text))
    return {"ok": True, "description": text, "tags": tags, "error": ""}


def _start_async_preprocess(asset_id: str, kind: str) -> dict:
    """
    对音视频素材**异步**排一次分析任务（复用素材库的 analysis_jobs）。

    返回 {"ok": bool, "job_id": int, "status": str, "error": str}。

    【为什么复用 analysis_jobs 而不是自己起线程】
    analysis_jobs 是素材库**既有**的分析调度层：
      * 有 job 表（asset_analysis_jobs）—— 状态可查、可回溯
      * 有进度回调（深度音频分析按阶段回调）
      * 有失败留痕（_fail_job 写一条 error 的分析记录）
      * 前端素材库页已经在轮询 /api/assets/{id}/analysis-status
    自己另造一套 = 状态无处可查 + 前端要写第二套轮询逻辑。

    【音频走深度管线，视频走多模态】
      * 音频：options["deep"]=True -> _run_audio_deep
        （BPM / 乐器 / 人声 / ASR 带时间戳 / LLM 语义补充）
      * 视频：多模态分析器（抽帧 + 模型看帧写中文描述）
    这正是素材库页「深度分析」对这两类素材的既有行为 ——
    Agent 上传只是**复用**它，不另造算法。

    【抽标签】
    深度音频分析写出来的文本是**结构化**的（## 一、音频基本信息
    ## 二、节拍与速度 …），所以复用图片那套抽标签逻辑就行
    （_extract_tags_from_description）—— 但要等分析完成后才能抽。
    见 _finalize_async_tags。
    """
    options: dict = {"derived_dir": None, "provider_id": None}
    try:
        with asset_store.connect() as conn:
            version = asset_repo.current_version(conn, asset_id)
            if not version:
                return {"ok": False, "job_id": 0, "status": "",
                        "error": "素材没有可用版本"}
            source = analysis_jobs._source_of(asset_id, version)
            if source is None:
                return {"ok": False, "job_id": 0, "status": "",
                        "error": "素材文件已丢失"}
            root = asset_store.library_root()
            options["derived_dir"] = str(
                root / asset_store.DIR_DERIVED / asset_id[:2]
                / f"{asset_id}_analysis")
            if kind == "audio":
                options["deep"] = True
            job = analysis_jobs.start_analysis(asset_id, kind, mode="analyze",
                                               options=options)
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"[:200]
        return {"ok": False, "job_id": 0, "status": "",
                "error": f"无法启动素材分析：{detail}"}
    return {"ok": True, "job_id": int(job.get("id") or 0),
            "status": job.get("status") or "queued", "error": ""}


def _finalize_async_tags(asset_id: str) -> dict:
    """
    异步分析完成后抽标签并写进素材库（描述由 analysis_jobs 自己写入）。

    返回 {"ok": bool, "tags": list, "error": str}。

    【为什么需要这一步】
    analysis_jobs._run_multimodal / _run_audio_deep 只写**描述**
    （asset_analyses.text），**不写标签**（asset_tags）。
    图片的同步预处理是 agent_media 自己抽标签再写的 ——
    异步路径必须补上这一步，否则音视频素材在素材库里没有标签，
    Agent 就搜不到它们（search_assets 的关键词检索命中标签）。

    【为什么抽标签要放在「分析完成后」而不是提交时】
    分析还没跑完时 asset_analyses 里没有文本 —— 抽不出东西来。
    前端轮询发现 job 完成后调这个接口补标签。
    """
    try:
        with asset_store.connect() as conn:
            analysis = asset_repo.latest_analysis(conn, asset_id)
            if not analysis:
                return {"ok": False, "tags": [],
                        "error": "素材还没有分析结论"}
            text = (analysis.get("text") or "").strip()
            if not text:
                return {"ok": False, "tags": [],
                        "error": "分析结论为空"}
            tags = _extract_tags_from_description(text)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "tags": [],
                "error": f"抽取标签失败：{type(exc).__name__}: {exc}"[:200]}
    return {"ok": True, "tags": _apply_tags(asset_id, tags), "error": ""}


def _run_analysis_sync(analysis_mod, asset: dict, version: dict,
                        source: Path) -> dict:
    """
    同步跑一次多模态分析。

    【为什么不能直接 await analysis_mod.run_analysis】
    那个函数是 async，但内部用 llm_client.call_provider 同步调用模型。
    在**已经有 running loop** 的线程里直接调会炸（await 一个同步阻塞调用
    会卡死整个事件循环 —— 这是最严重的一种卡法）。

    所以这里起一个**独立线程**跑同步版（asyncio.run），
    主线程 join 等它 —— 与 analysis_jobs._run_multimodal 的做法一致，
    区别只是那个是「提交到线程池不等」，这个是「提交并等结果」。

    【conn 传什么】
    run_analysis 的 conn 只用于 pick_provider，而 pick_provider 内部
    **自己开 jobs.db 连接**（Provider 表在 jobs.db，不在素材库库）。
    传 None 与传素材库连接等价 —— 这里传 None 明确表达「不依赖它」。
    """
    import asyncio
    import threading

    box: dict = {}

    def _runner():
        try:
            box["result"] = asyncio.run(analysis_mod.run_analysis(
                None, asset, version, source, {}, None))
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True,
                              name="agent-media-preprocess")
    thread.start()
    thread.join(timeout=PREPROCESS_TIMEOUT)

    if thread.is_alive():
        raise TimeoutError(
            f"素材预处理超时（超过 {int(PREPROCESS_TIMEOUT)} 秒）")
    if "error" in box:
        raise box["error"]
    return box.get("result") or {}


def _describe_asset(asset_id: str) -> dict:
    """
    取素材的**已有**描述（来自素材库分析结果，不是现编的）。

    与 agent._tool_describe_asset 用同一个解析入口（ref_api._ref_entry），
    保证「Agent 聊天区上传的图」和「素材库里的图」对模型来说长得一样。
    """
    from h3studio import ref_api

    with asset_store.connect() as conn:
        entry = ref_api._ref_entry(conn, asset_id)
    return {
        "asset_id": asset_id,
        "name": entry.get("name") or "",
        "description": entry.get("description") or "",
        "description_source": entry.get("description_source") or "empty",
        "tags": entry.get("tags") or [],
        "preview_url": entry.get("preview_url"),
        "width": entry.get("width"),
        "height": entry.get("height"),
        "missing": bool(entry.get("missing")),
        "reason": entry.get("reason") or "",
    }


async def handle_upload(request) -> dict:
    """
    处理一次 multipart 上传请求。

    期望的表单字段：
      files[] / file / image —— 一个或多个图片文件

    【完整流程 —— 严格按用户约束】
      1. 校验（扩展名 + 魔数 + 大小）
      2. **入素材库**（asset_service.register_asset）→ 拿到正式 asset_id
         + 元数据（尺寸/编码/类型，register_asset 内部探测）
      3. **素材库预处理**（分析管线）：生成描述文本 + 标签
      4. 只有 2、3 都成功，才返回给 Agent 作为可引用条目

    【失败语义 —— 不静默降级】
    入库失败 -> errors 里明确条目
    预处理失败 -> errors 里明确条目（**不返回**这个 asset_id，
    避免 Agent 拿到一个「没有描述的裸条目」）
    预处理超时的素材会**从素材库删除**（回滚）——
    留一条「已入库但没处理好」的孤儿素材在库里更糟。

    返回 {"ok": True, "results": [...], "errors": [...]}。
    单个文件失败**不影响**其它文件（与素材库上传同一语义）。
    """
    report = asset_store.ensure_layout()
    if not report.get("ok"):
        raise UploadError("；".join(report.get("errors") or ["素材库不可用"]),
                          code="library_unavailable", status=503)

    reader = await request.multipart()
    tmp_dir = asset_store.library_root() / asset_store.DIR_TMP
    tmp_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    errors: list[dict] = []
    # 图片 / 音视频**分开计数**（额度不同，报错也要说清是哪一类超了）
    image_count = 0
    media_count = 0

    async for part in reader:
        if not part.filename:
            continue
        try:
            item = await _read_and_validate_part(part, tmp_dir)
        except UploadError as exc:
            errors.append({"filename": part.filename, "error": exc.message,
                           "code": exc.code})
            continue

        kind = item.get("kind") or "image"
        # --- 数量上限（分类型计数）-------------------------------------
        if kind == "image":
            if image_count >= MAX_IMAGES_PER_UPLOAD:
                item["tmp"].unlink(missing_ok=True)
                errors.append({
                    "filename": part.filename,
                    "error": (f"一次最多上传 {MAX_IMAGES_PER_UPLOAD} 张图片，"
                              f"多余的已忽略"),
                    "code": "too_many_images",
                })
                continue
        else:
            if media_count >= MAX_MEDIA_FILES_PER_UPLOAD:
                item["tmp"].unlink(missing_ok=True)
                errors.append({
                    "filename": part.filename,
                    "error": (f"一次最多上传 {MAX_MEDIA_FILES_PER_UPLOAD} "
                              f"个音频/视频，多余的已忽略"),
                    "code": "too_many_media",
                })
                continue

        # --- 2) 入素材库 -------------------------------------------------
        try:
            entry = _register_one(item)
        except UploadError as exc:
            item["tmp"].unlink(missing_ok=True)
            errors.append({"filename": item["filename"], "error": exc.message,
                           "code": exc.code})
            continue
        finally:
            # register_asset 已把文件原子移动到正式位置；这里兜底清理
            item["tmp"].unlink(missing_ok=True)

        asset_id = entry["asset_id"]

        # --- 3) 素材库预处理 ---------------------------------------------
        # 图片：同步预处理（描述 + 标签）—— 与既有行为完全一致。
        # 音视频：异步 job（描述由 analysis_jobs 写，标签完成后补）——
        #   深度音频分析实测 ~600 秒/116 秒素材，同步等会挂死请求。
        if kind == "image":
            pre = _preprocess_asset(asset_id)
            if not pre.get("ok"):
                # 预处理失败 -> 明确报错，并**回滚**这条素材
                # （不让「已入库但没描述」的孤儿条目留在库里）
                _rollback_asset(asset_id)
                errors.append({
                    "filename": item["filename"],
                    "error": pre.get("error") or "素材预处理失败",
                    "code": "preprocess_failed",
                })
                continue

            # --- 4) 读取素材库里的权威状态（描述/标签都从库里取） ------
            described = _describe_asset(asset_id)
            entry.update({
                "analyzed": bool(described.get("description")),
                "description": described.get("description") or "",
                "tags": described.get("tags") or pre.get("tags") or [],
                "width": described.get("width") or entry.get("width"),
                "height": described.get("height") or entry.get("height"),
                "preprocessed": True,
                "analysis_status": "done",
                "available": True,
            })
            image_count += 1
        else:
            started = _start_async_preprocess(asset_id, kind)
            if not started.get("ok"):
                # 任务都排不起来 -> 明确报错 + 回滚（不留孤儿条目）
                _rollback_asset(asset_id)
                errors.append({
                    "filename": item["filename"],
                    "error": started.get("error") or "素材分析任务启动失败",
                    "code": "preprocess_failed",
                })
                continue
            entry.update({
                "analyzed": False,
                "description": "",
                "tags": [],
                "preprocessed": False,
                # 前端据此轮询 /api/assets/{id}/analysis-status
                "analysis_job_id": started.get("job_id"),
                "analysis_status": started.get("status") or "queued",
                "available": False,
                "analysis_pending": True,
            })
            media_count += 1

        results.append(entry)

    return {
        "ok": bool(results) or not errors,
        "results": results,
        "errors": errors,
        "uploaded": len(results),
        "failed": len(errors),
        "max_images": MAX_IMAGES_PER_UPLOAD,
        "max_media_files": MAX_MEDIA_FILES_PER_UPLOAD,
        "max_bytes": MAX_IMAGE_BYTES,
        "max_audio_bytes": MAX_AUDIO_BYTES,
        "max_video_bytes": MAX_VIDEO_BYTES,
    }


def _rollback_asset(asset_id: str) -> bool:
    """
    把刚入库但预处理失败的素材**撤回**（进回收站，不 purge）。

    【为什么必须回滚】
    用户约束 3：「不得静默降级为『未入库的裸文件』」。
    如果只报错却把素材留在库里，素材库页面会多出一条「已入库但没描述、
    没分析结果」的条目 —— 那是另一种形式的静默降级（用户会以为
    上传成功了，只是描述还没生成）。

    回滚进回收站（不是 purge）—— 用户在素材库页仍能看到并手动恢复，
    这样「出错了但东西还在」是可追溯的。
    """
    try:
        with asset_store.connect() as conn:
            asset_repo.soft_delete(conn, asset_id)
        return True
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ 就绪查询

#: 异步分析的前端轮询间隔（秒）。
POLL_INTERVAL_SEC = 3.0


async def handle_analysis_status(asset_id: str) -> dict:
    """
    查询音视频素材的**分析就绪状态**（前端轮询用）。

    返回 {"asset_id", "status", "stage", "progress", "message",
          "available", "description", "tags", "duration_sec",
          "width", "height", "kind", "error"}。

    status:
      pending —— 分析还在跑（不可用）
      done    —— 分析完成，描述与标签已就绪（可用）
      failed  —— 分析失败（不可用，且明确报错）
      none    —— 没有分析记录（不可用）

    【为什么在这个接口里「完成即补标签」】
    analysis_jobs 只写描述（asset_analyses.text），不写标签
    （asset_tags）。前端轮询到 done 时调用本接口，这里顺带把标签
    抽好写进去 —— 这样前端只轮询一个接口，不用再发第二个请求，
    且「标签已落库」与「状态已就绪」是同一次原子返回，不会出现
    「状态说好了但标签还没写」的中间态。

    【为什么失败时也要回滚】
    约束 3：「分析失败要明确报错并按现有约定 soft_delete 回滚，
    不留『已入库没描述』的脏条目」。
    同步路径（图片）在上传时就回滚；异步路径（音视频）失败发生在
    上传之后，所以回滚放在这里 —— 第一次检测到 failed 时回滚，
    并用 job 记录标记，保证**只回滚一次**。
    """
    from . import assets as asset_store

    try:
        with asset_store.connect() as conn:
            # include_deleted=True —— 回滚后素材进回收站，但**状态还要能查到**
            # （前端轮询要看到「已失败并已回滚」，而不是「素材不存在」）。
            asset = asset_repo.get_asset(conn, asset_id, include_deleted=True)
            if not asset:
                return {"asset_id": asset_id, "status": "failed",
                        "stage": "", "progress": 0,
                        "message": "素材不存在", "available": False,
                        "description": "", "tags": [], "kind": "",
                        "duration_sec": None, "width": None, "height": None,
                        "error": "素材不存在"}

            version = asset_repo.current_version(conn, asset_id) or {}
            analysis = asset_repo.latest_analysis(conn, asset_id)
            text = (analysis or {}).get("text") or ""
            job = asset_repo.active_job_of(conn, asset_id)
            if job is None:
                jobs = asset_repo.jobs_of(conn, asset_id, limit=1)
                job = jobs[0] if jobs else None

            status = (job or {}).get("status") or "none"
            message = (job or {}).get("message") or ""
            stage = (job or {}).get("stage") or ""
            progress = float((job or {}).get("progress") or 0)
            # 已被回滚（进了回收站）—— 不再是「可用」的候选
            rolled_back = bool(asset.get("deleted_at"))

            tags = asset_repo.tags_of(conn, asset_id)

            # 分析完成 -> 补标签（幂等：add_tags 是 INSERT OR IGNORE）
            if status == "done" and text and not rolled_back:
                if not tags:
                    finalized = _finalize_async_tags(asset_id)
                    tags = finalized.get("tags") or []
                    if not finalized.get("ok"):
                        message = finalized.get("error") or message

            result = {
                "asset_id": asset_id,
                "status": status,
                "stage": stage,
                "progress": progress,
                "message": message,
                "available": bool(status == "done" and text and not rolled_back),
                "description": text,
                "tags": tags,
                "kind": asset.get("category") or "",
                "duration_sec": version.get("duration_sec"),
                "width": version.get("width"),
                "height": version.get("height"),
                "rolled_back": rolled_back,
                "error": "",
            }

            # 分析失败 -> 明确报错 + 回滚（只回滚一次）
            if status == "failed":
                result["available"] = False
                result["error"] = message or "素材分析失败"
                if not rolled_back:
                    _rollback_asset(asset_id)
                    result["rolled_back"] = True
                    result["error"] = (result["error"] + "（素材已回滚）")
                    try:
                        asset_repo.update_job(
                            conn, int((job or {}).get("id") or 0),
                            message=result["error"])
                    except Exception:  # noqa: BLE001
                        pass
            return result
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"[:200]
        return {"asset_id": asset_id, "status": "failed", "stage": "",
                "progress": 0, "message": detail, "available": False,
                "description": "", "tags": [], "kind": "",
                "duration_sec": None, "width": None, "height": None,
                "error": detail}
