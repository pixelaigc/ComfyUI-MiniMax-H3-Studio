# -*- coding: utf-8 -*-
"""
H3 Studio 后端 —— 本地视频生成工作台
aiohttp 常驻服务 + SQLite FIFO 队列，通过 HTTP API 驱动本机 ComfyUI。

用 ComfyUI 自带 python_embeded 运行（已含 aiohttp，零额外依赖）：
  d:\\cfup\\python_embeded\\python.exe server.py
"""
import asyncio
import hashlib
import json
import os
import platform
import random
import re
import shutil
import sqlite3
import subprocess

# --- 平台兼容补丁（Linux/macOS）---
# 原仓库为 Windows 专用：subprocess.CREATE_NO_WINDOW 是 Windows 专有属性，
# 在 POSIX 上访问即 AttributeError，会让 /api/hardware、/api/longfilms 直接 500。
# POSIX 下 creationflags=0 是合法的（等价于不传），所以统一替换为常量 0。
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
import time
import uuid
from pathlib import Path

import psutil

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aiohttp import web, ClientSession, ClientTimeout
import aiohttp

from h3studio.workflows import (
    build_workflow,
    QWEN_IMAGE_CLIP_COND,
    QWEN_IMAGE_DEFAULT_MODEL,
    QWEN_IMAGE_DEFAULT_TIER,
    QWEN_IMAGE_MAX_DIRECT_MP,
    QWEN_IMAGE_MAX_REF_IMAGES,
    QWEN_IMAGE_MIN_REF_IMAGES,
    QWEN_IMAGE_MODELS,
    QWEN_IMAGE_RES_TIERS,
    QWEN_IMAGE_SIZE_MULTIPLE,
    QWEN_IMAGE_NODE_REF_SLOTS,
    QWEN_IMAGE_PE_PARAMS,
    QWEN_IMAGE_PE_DEFAULT_MAX_LENGTH,
    qwen_unet_available,
    ASPECT_MAP,
    ASPECT_NUM,
    Z_IMAGE_MODELS,
    Z_IMAGE_DEFAULT_MODEL,
    Z_IMAGE_RES_TIERS,
    Z_IMAGE_DEFAULT_TIER,
    Z_IMAGE_MAX_DIRECT_MP,
    Z_IMAGE_SIZE_MULTIPLE,
    Z_IMAGE_SIZE_MIN,
    Z_IMAGE_SIZE_MAX,
    z_image_dimensions,
    z_image_megapixels,
    MOSS_IMAGE_DEFAULT_MODEL,
    MOSS_IMAGE_DEFAULT_TIER,
    MOSS_IMAGE_MAX_DIRECT_MP,
    MOSS_IMAGE_MIN_MP,
    MOSS_IMAGE_MODELS,
    MOSS_IMAGE_RES_TIERS,
    MOSS_IMAGE_SIZE_MULTIPLE,
    MOSS_IMAGE_DEFAULT_NEGATIVE,
    moss_image_model_available,
    normalize_moss_image_size,
)
from h3studio import providers as prov
from h3studio import llm as llm_client
from h3studio import enhancer as enh
from h3studio import moss_layout
from h3studio import qwen_pe
from h3studio import loras as lora_registry
from h3studio import gen_types
from h3studio import hardware as hw
from h3studio import assets as asset_store
from h3studio import asset_api
from h3studio import ref_api
from h3studio import ref_images as ref_imgs
from h3studio import music_yue2
from h3studio import agent as agent_mod
from h3studio import agent_sessions

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"


def _build_fingerprint() -> str:
    """后端「代码指纹」—— 前端用它判断自己连的是不是最新后端。

    为什么需要：前端资源是静态文件（改了立刻生效），后端是长跑进程
    （改了要重启才生效）。两者版本错配时，界面上会出现**最难查的一类 bug**：
    新前端把 t2i_moss 提交给旧后端 -> 旧后端的模式白名单里没有它 ->
    直接 400「未知模式」；或者 /api/image-options 没有 moss 字段 ->
    分辨率档位和模型下拉全是空的、LoRA 列表串到 Z-Image 族。

    指纹取「本项目所有后端 .py 文件的内容哈希」+ 启动时刻，
    前端拿到后与「页面里 script 标签的版本参数」一起比对，
    不一致就在面板顶部挂一条醒目的红色横幅，明确告诉用户「重启服务」。

    这里刻意用**内容哈希**而不是 mtime：git checkout / rsync 会保留或
    重置 mtime，但内容一定变。
    """
    import hashlib
    h = hashlib.sha256()
    for path in sorted(BASE.rglob("*.py")):
        if "__pycache__" in path.parts or ".git" in path.parts:
            continue
        try:
            h.update(path.relative_to(BASE).as_posix().encode("utf-8"))
            h.update(path.read_bytes())
        except OSError:
            continue
    return h.hexdigest()[:12]


# 进程启动时刻（用于「后端重启了没有」的判断）
SERVER_STARTED_AT = time.time()
BUILD_FINGERPRINT = _build_fingerprint()
# 后端支持的功能位 —— 前端按这个决定某个模式/入口是否可用。
# 老后端没有这些位 -> 前端不会把 MOSS 入口当成可用功能展示。
SERVER_FEATURES = {
    "moss_image": True,
    "moss_image_loras": True,
    "moss_image_slg": True,
}

DEFAULT_CONFIG = {
    "host": "0.0.0.0",
    "port": 8190,
    "comfyui_url": "http://127.0.0.1:8188",
    "data_dir": str(BASE / "data"),
    "models_dir": "",
    "poll_interval": 1.5,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"[config] 读取失败，用默认配置: {e}")
    return cfg


CFG = load_config()
DATA = Path(CFG["data_dir"])
OUT_DIR = DATA / "outputs"
UPLOAD_DIR = DATA / "uploads"
VIDEO_DIR = OUT_DIR / "videos"
IMAGE_DIR = OUT_DIR / "images"
# 音乐（YuE2）产物目录 —— 与视频/图片同级，产物统一走 /files/ 路由
AUDIO_DIR = OUT_DIR / "audio"
DB_PATH = DATA / "jobs.db"
for d in (DATA, OUT_DIR, UPLOAD_DIR, VIDEO_DIR, IMAGE_DIR, AUDIO_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------------ DB
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                created_at REAL,
                mode TEXT,
                params TEXT,
                status TEXT,          -- queued|running|done|failed|canceled
                prompt_id TEXT,
                progress REAL DEFAULT 0,
                progress_text TEXT,
                error TEXT,
                outputs TEXT,         -- JSON [{kind,file,name}]
                started_at REAL,
                finished_at REAL
            );

            -- 生成批次：一次提交 N 张时，每个批次独立的状态/种子/产物
            CREATE TABLE IF NOT EXISTS job_batches (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                idx INTEGER NOT NULL,
                seed INTEGER,
                status TEXT,          -- queued|running|done|failed|canceled
                prompt_id TEXT,
                progress REAL DEFAULT 0,
                progress_text TEXT,
                error TEXT,
                outputs TEXT,         -- JSON [{kind,file,name}]
                created_at REAL,
                started_at REAL,
                finished_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_job_batches_job ON job_batches(job_id, idx);
            """
        )
    # 本地模型 Provider 配置 + 提示词增强 Agent 的模板/参数覆盖
    prov.ensure_tables(conn)


def row_to_job(r):
    mode = r["mode"]
    job_params = json.loads(r["params"] or "{}")
    # 生成类型：优先读任务 meta 里记录的 gen_type；
    # 老任务没有该字段时，按 mode 一致兜底归类（不出现空白 / 全归未知）
    stored_type = str(job_params.get("gen_type") or "").strip()
    gen_type = (gen_types.type_for_mode(stored_type) if stored_type
                else gen_types.type_for_mode(mode))
    return {
        "id": r["id"],
        "created_at": r["created_at"],
        "mode": mode,
        "gen_type": gen_type["key"],
        "gen_type_label": gen_type["label"],
        "gen_type_media": gen_type["media"],
        "params": job_params,
        "status": r["status"],
        "progress": r["progress"],
        "progress_text": r["progress_text"],
        "error": r["error"],
        "outputs": json.loads(r["outputs"] or "[]"),
        "elapsed": (r["finished_at"] or time.time()) - r["started_at"] if r["started_at"] else None,
    }


def batches_of(job_id: str) -> list[dict]:
    """取某个任务的全部批次（按 idx 排序）。没有批次时返回空列表。"""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM job_batches WHERE job_id=? ORDER BY idx", (job_id,)).fetchall()
    return [{
        "id": r["id"],
        "idx": r["idx"],
        "seed": r["seed"],
        "status": r["status"],
        "progress": r["progress"],
        "progress_text": r["progress_text"],
        "error": r["error"],
        "outputs": json.loads(r["outputs"] or "[]"),
        "elapsed": (r["finished_at"] or time.time()) - r["started_at"] if r["started_at"] else None,
    } for r in rows]


# ------------------------------------------------------------------ ComfyUI client
COMFY = CFG["comfyui_url"].rstrip("/")
HTTP_TIMEOUT = ClientTimeout(total=600)
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
LONGFILM_LOCK = asyncio.Lock()
LONGFILM_TASKS = set()
LONGFILM_PROCESSES = {}


async def comfy_upload_image(session: ClientSession, path: Path) -> str:
    """上传图片到 ComfyUI input，返回 'name'（subfolder 走 h3studio/）"""
    data = aiohttp.FormData()
    data.add_field("image", open(path, "rb"),
                   filename=path.name, content_type="application/octet-stream")
    data.add_field("subfolder", "h3studio")
    data.add_field("overwrite", "true")
    async with session.post(f"{COMFY}/upload/image", data=data) as resp:
        r = await resp.json()
    sub = r.get("subfolder", "")
    name = r.get("name", path.name)
    return f"{sub}/{name}" if sub else name


async def comfy_upload_media(session: ClientSession, path: Path) -> str:
    """
    上传视频 / 音频到 ComfyUI input，返回 'subfolder/name'。

    ComfyUI 的 /upload/image 端点只接受图片；视频与音频要走
    /upload/image 的通用 multipart 端点（ComfyUI 允许任意文件类型走同一端点，
    由节点侧 VHS_LoadVideo / VHS_LoadAudioUpload 读取）。
    """
    data = aiohttp.FormData()
    data.add_field("image", open(path, "rb"),
                   filename=path.name, content_type="application/octet-stream")
    data.add_field("subfolder", "h3studio")
    data.add_field("overwrite", "true")
    data.add_field("type", "input")
    async with session.post(f"{COMFY}/upload/image", data=data) as resp:
        raw = await resp.text()
    try:
        r = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ComfyUI 上传失败：{raw[:200]}") from exc
    if not r.get("name"):
        raise RuntimeError(f"ComfyUI 上传失败：{raw[:200]}")
    sub = r.get("subfolder", "")
    name = r["name"]
    return f"{sub}/{name}" if sub else name


async def _upload_ref_audio_flat(session: ClientSession, job_id: str,
                                 params: dict) -> str:
    """把参考音频（素材库）上传到 ComfyUI input 顶层，返回纯文件名。"""
    from h3studio import asset_repo as _repo
    from h3studio import assets as _assets

    refs = params.get("asset_refs") or []
    item = next((r for r in refs if isinstance(r, dict)
                 and r.get("slot") == "ref_audio"), None)
    if not item:
        raise RuntimeError("参考生音乐缺少参考音频")
    asset_id = str(item.get("asset_id") or "").strip()
    with _assets.connect() as conn:
        version = _repo.current_version(conn, asset_id)
        if not version:
            raise RuntimeError(f"参考音频素材不存在或已删除：{asset_id}")
        path = _assets.resolve_inside(_assets.library_root(), version["storage_path"])
        if not path or not path.is_file():
            raise RuntimeError(f"参考音频素材文件已丢失：{asset_id}")
        # 文件名带素材 id 前缀 —— 顶层的输入目录是共用的，避免撞名
        safe_id = asset_id.replace("_", "-")[-12:]
        filename = f"h3studio_ref_{safe_id}{path.suffix.lower()}"
        return await comfy_upload_audio_flat(session, path, filename)


async def comfy_upload_audio_flat(session: ClientSession, path: Path,
                                    filename: str | None = None) -> str:
    """
    上传音频到 ComfyUI 的 input **顶层**（不带 subfolder），返回纯文件名。

    为什么不能复用 comfy_upload_media：
      * comfy_upload_media 传到 `input/h3studio/` 子目录，返回 "h3studio/x.wav"
      * ComfyUI 的 `LoadAudio` 节点 audio 是 COMBO，值必须命中 `input/`
        **顶层**的文件清单（没有子目录项）-> 传子目录路径会报
        "value not in list"
      * `VHS_LoadAudio` 的 audio_file 要的是**绝对文件系统路径**
        （validate_path 用 os.path.isfile 判）-> 相对路径报 "Invalid file path"

    参考音频（参考生音乐）必须能命中 LoadAudio 的 COMBO，所以单开这条通道。
    文件名带内容指纹，避免撞名。
    """
    name = filename or path.name
    data = aiohttp.FormData()
    data.add_field("image", open(path, "rb"),
                   filename=name, content_type="application/octet-stream")
    data.add_field("subfolder", "")
    data.add_field("overwrite", "true")
    data.add_field("type", "input")
    async with session.post(f"{COMFY}/upload/image", data=data) as resp:
        raw = await resp.text()
    try:
        r = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ComfyUI 上传失败：{raw[:200]}") from exc
    if not r.get("name"):
        raise RuntimeError(f"ComfyUI 上传失败：{raw[:200]}")
    # 只取文件名 —— LoadAudio 的 COMBO 里没有子目录项
    return str(r["name"]).split("/")[-1]


async def comfy_submit(session: ClientSession, workflow: dict, client_id: str | None = None) -> str:
    payload = {"prompt": workflow}
    if client_id:
        payload["client_id"] = client_id
    async with session.post(f"{COMFY}/prompt", json=payload) as resp:
        body = await resp.json()
    if resp.status != 200 or "prompt_id" not in body:
        raise RuntimeError(f"ComfyUI 提交失败: {json.dumps(body, ensure_ascii=False)[:500]}")
    return body["prompt_id"]


def _extract_outputs(history_entry):
    """从 history 条目提取输出文件列表 [{kind,filename,subfolder}]"""
    outs = []
    for node_out in (history_entry.get("outputs") or {}).values():
        # ComfyUI 的 SaveAudioAdvanced 通过 SavedAudios.as_dict() 挂在
        # **"audio"** 键下（comfy_api/latest/_ui.py:56-61）—— 不是 "images"。
        for key, kind in (("videos", "video"), ("gifs", "video"),
                          ("images", "image"), ("audio", "audio")):
            val = node_out.get(key)
            items = val if isinstance(val, list) else ([val] if val else [])
            for item in items:
                if isinstance(item, dict) and item.get("filename"):
                    fname = item["filename"]
                    # 按扩展名修正类型（SaveVideo 的输出也会挂在 images 键下）
                    if fname.lower().endswith((".mp4", ".webm", ".mkv", ".mov")):
                        kind = "video"
                    elif fname.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                        kind = "image"
                    elif fname.lower().endswith((".flac", ".mp3", ".opus", ".wav",
                                               ".m4a", ".ogg", ".aac")):
                        kind = "audio"
                    outs.append({"kind": kind, "filename": fname,
                                 "subfolder": item.get("subfolder", ""),
                                 "type": item.get("type", "output")})
    return outs


def _next_output_path(kind: str, suffix: str) -> Path:
    """为新输出分配稳定、易检索的顺序文件名。"""
    if kind == "image":
        dest_dir, prefix = IMAGE_DIR, "i-zimage-"
    elif kind == "audio":
        dest_dir, prefix = AUDIO_DIR, "a-yue2-"
    elif kind == "longfilm":
        dest_dir, prefix = VIDEO_DIR, "v-l-"
    else:
        dest_dir, prefix = VIDEO_DIR, "v-h3-studio-"
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$", re.IGNORECASE)
    highest = 0
    for path in dest_dir.iterdir():
        if not path.is_file():
            continue
        match = pattern.match(path.stem)
        if match:
            highest = max(highest, int(match.group(1)))
    return dest_dir / f"{prefix}{highest + 1:06d}{suffix.lower()}"


async def comfy_download_output(session: ClientSession, item: dict) -> str:
    params = {"filename": item["filename"], "subfolder": item["subfolder"], "type": item["type"]}
    suffix = Path(item["filename"]).suffix or (".png" if item["kind"] == "image" else ".mp4")
    dest = _next_output_path(item["kind"], suffix)
    async with session.get(f"{COMFY}/view", params=params) as resp:
        resp.raise_for_status()
        dest.write_bytes(await resp.read())
    return dest.relative_to(OUT_DIR).as_posix()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# 素材库槽位 -> ComfyUI 参数键
_ASSET_SLOT_PARAM = {
    "first_frame": "first_frame",
    "last_frame": "last_frame",
    "ref_image": "ref_images",
    "character_ref": "ref_images",
    "ref_video": "ref_videos",
    "source_video": "ref_videos",
    "ref_audio": "ref_audios",
    "bgm": "ref_audios",
    "voice": "ref_audios",
}


async def _apply_asset_refs(session: ClientSession, job_id: str, params: dict):
    """
    把任务里的素材库引用解析成 ComfyUI 能用的文件名。

    params["asset_refs"] = [{"asset_id":..., "slot":"first_frame"|"ref_image"|..., "ordinal":0}]

    规则：
      * 图片槽位 -> 走 comfy_upload_image
      * 视频/音频槽位 -> 走 comfy_upload_media
      * 结果按槽位分组写回 params（ref_images / ref_videos / ref_audios / first_frame ...）
      * 同时在素材库里登记引用关系（被引用次数 +1）
    """
    from h3studio import asset_repo as _repo
    from h3studio import assets as _assets

    refs = params.get("asset_refs") or []
    if not isinstance(refs, list) or not refs:
        return

    grouped: dict[str, list] = {}
    with _assets.connect() as conn:
        for item in refs:
            if not isinstance(item, dict):
                continue
            asset_id = str(item.get("asset_id") or "").strip()
            slot = str(item.get("slot") or "ref_image").strip()
            if not asset_id:
                continue
            version = _repo.current_version(conn, asset_id)
            if not version:
                raise RuntimeError(f"素材不存在或已删除：{asset_id}")
            path = _assets.resolve_inside(_assets.library_root(), version["storage_path"])
            if not path or not path.is_file():
                raise RuntimeError(f"素材文件已丢失：{asset_id}")

            category = version.get("mime") or ""
            is_video = "video" in category
            is_audio = "audio" in category
            if is_video:
                uploaded = await comfy_upload_media(session, path)
            elif is_audio:
                uploaded = await comfy_upload_media(session, path)
            else:
                uploaded = await comfy_upload_image(session, path)

            key = _ASSET_SLOT_PARAM.get(slot, "ref_images")
            grouped.setdefault(key, []).append(uploaded)
            # 登记引用关系（引用次数因此 +1；删除素材时会被 409 保护）
            _repo.add_ref(conn, asset_id, "job", job_id, slot=slot,
                          ordinal=int(item.get("ordinal") or 0),
                          label=f"任务 {job_id} 的 {slot} 输入")

    for key, values in grouped.items():
        if key in ("first_frame", "last_frame"):
            params[key] = values[0]
        else:
            params[key] = values


async def _upload_asset_refs(session: ClientSession, job_id: str, params: dict) -> list[str]:
    """
    把「素材库来源」的参考图上传到 ComfyUI input，返回 ComfyUI 文件名列表。

    params["ref_images"] = [{"asset_id": ..., "description": ..., "tags": ...}, ...]

    规则：
      * **顺序即语义** —— 返回顺序与入参顺序严格一致，
        因为占位标记 <image1>..<imageN> 与 ComfyUI 的 image_1..image_N
        是按下标一一对应的（见 workflows.build_qwen_i2i）。
      * 素材失效（已删除 / 文件丢失）-> 明确报错，不静默跳过
        （静默跳过会让占位号与实际插槽错位，图1 变成图2 —— 很难查）。
      * 登记引用关系（asset_refs），素材被删时 409 保护。
    """
    names = await _upload_ref_images(session, params.get("ref_images") or [])
    if not names:
        raise RuntimeError("图片编辑没有参考图（参考图必须来自素材库）")
    from h3studio import asset_repo as _repo
    from h3studio import assets as _assets
    refs = ref_imgs.normalize_refs(params.get("ref_images") or [])
    with _assets.connect() as conn:
        for index, ref in enumerate(refs):
            _repo.add_ref(conn, ref["asset_id"], "job", job_id, slot="ref_image",
                          ordinal=index,
                          label=f"任务 {job_id} 的参考图 {index + 1}")
    return names


async def _upload_ref_images(session: ClientSession, raw_refs) -> list[str]:
    """
    把素材库参考图上传到 ComfyUI input，返回文件名列表（**不登记引用关系**）。

    与 _upload_asset_refs 的区别：那个是出图任务用的，会在 asset_refs 里
    登记「任务引用了哪些素材」（素材被删时 409 保护）。
    官方 PE 扩写只是**读一次图**，不是出图任务 —— 登记引用会让素材的
    ref_count 被无意义地抬高，还会挡住素材删除。所以这里走独立的一条路。

    顺序仍然严格保持入参顺序（占位语义）。
    """
    from h3studio import asset_repo as _repo
    from h3studio import assets as _assets

    refs = ref_imgs.normalize_refs(raw_refs)
    names: list[str] = []
    with _assets.connect() as conn:
        for index, ref in enumerate(refs):
            asset_id = ref["asset_id"]
            version = _repo.current_version(conn, asset_id)
            if not version:
                asset = _repo.get_asset(conn, asset_id, include_deleted=True)
                state = "已从素材库删除" if asset else "不存在"
                raise RuntimeError(
                    f"第 {index + 1} 张参考图的素材{state}：{asset_id}。")
            path = _assets.resolve_inside(_assets.library_root(), version["storage_path"])
            if not path or not path.is_file():
                raise RuntimeError(
                    f"第 {index + 1} 张参考图的素材文件已丢失：{asset_id}")
            names.append(await comfy_upload_image(session, path))
    return names


async def save_uploaded_image(part) -> str:
    """保存上传图片；内容相同则复用 uploads 中已有文件。"""
    suffix = Path(part.filename or "image.png").suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
        suffix = ".png"
    temp = UPLOAD_DIR / f".upload-{uuid.uuid4().hex}.tmp"
    digest = hashlib.sha256()
    with temp.open("wb") as handle:
        while True:
            chunk = await part.read_chunk(1024 * 256)
            if not chunk:
                break
            digest.update(chunk)
            handle.write(chunk)
    fingerprint = digest.hexdigest()
    canonical = UPLOAD_DIR / f"img-{fingerprint[:20]}{suffix}"
    if canonical.exists():
        temp.unlink(missing_ok=True)
        return canonical.name
    # 兼容升级前的随机命名：若内容已经存在，直接复用旧文件。
    for existing in UPLOAD_DIR.iterdir():
        if not existing.is_file() or existing == temp or existing.name.startswith(".upload-"):
            continue
        try:
            if existing.stat().st_size == temp.stat().st_size and _file_sha256(existing) == fingerprint:
                temp.unlink(missing_ok=True)
                return existing.name
        except OSError:
            continue
    temp.replace(canonical)
    return canonical.name


async def comfy_history(session: ClientSession, prompt_id: str):
    async with session.get(f"{COMFY}/history/{prompt_id}") as resp:
        data = await resp.json()
    return data.get(prompt_id)


async def ws_progress_listener(session: ClientSession, client_id: str, prompt_id: str,
                               job_id: str, total_nodes: int, batch_id: str | None = None):
    """监听 ComfyUI WebSocket 的 progress_state 事件，把真实百分比写入 DB

    batch_id 非空时写 job_batches（批次进度），否则写 jobs（任务进度）。
    """
    try:
        async with session.ws_connect(f"{COMFY}/ws?clientId={client_id}", heartbeat=30) as ws:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue
                d = data.get("data") or {}
                if d.get("prompt_id") != prompt_id:
                    continue
                etype = data.get("type")
                if etype == "progress_state":
                    nodes = d.get("nodes") or {}
                    # 采样类节点（有迭代步数 max>1）耗时占大头，给 8 倍权重
                    W_SAMPLER, base_w = 8.0, 1.0
                    finished_w, running_w = 0.0, 0.0
                    step_label = ""
                    for n in nodes.values():
                        st = n.get("state")
                        mx = float(n.get("max") or 0)
                        w = W_SAMPLER if mx > 1 else base_w
                        if st == "finished":
                            finished_w += w
                        elif st == "running":
                            f = min(float(n.get("value") or 0) / mx, 1.0) if mx > 0 else 0.0
                            running_w += f * w
                            if mx > 1:
                                step_label = f"{int(n.get('value') or 0)}/{int(mx)}"
                    total_w = base_w * (total_nodes - 1) + W_SAMPLER
                    frac = min(max((finished_w + running_w) / total_w, 0.03), 0.98)
                    done_cnt = sum(1 for n in nodes.values() if n.get("state") == "finished")
                    text = f"节点 {done_cnt}/{total_nodes}" + (f" · 步 {step_label}" if step_label else "")
                    with db() as conn:
                        if batch_id:
                            conn.execute(
                                "UPDATE job_batches SET progress=?, progress_text=? WHERE id=?",
                                (frac, text, batch_id))
                        else:
                            conn.execute("UPDATE jobs SET progress=?, progress_text=? WHERE id=?",
                                         (frac, text, job_id))
                elif etype == "execution_error":
                    err = str(d.get("exception_message") or d.get("exception_type") or "执行出错")[:400]
                    with db() as conn:
                        if batch_id:
                            conn.execute("UPDATE job_batches SET error=? WHERE id=?", (err, batch_id))
                        else:
                            conn.execute("UPDATE jobs SET error=? WHERE id=?", (err, job_id))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[ws] 进度监听结束({client_id}): {e}")


# ------------------------------------------------------------------ Worker
class Worker:
    def __init__(self):
        self.current = None  # 当前运行 job id
        self._task = None
        self.submit_lock = asyncio.Lock()

    async def start(self):
        self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            try:
                job = self._next_queued()
                if job is None:
                    await asyncio.sleep(1.0)
                    continue
                await self._run(job)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[worker] 异常: {e}")
                await asyncio.sleep(2.0)

    def _next_queued(self):
        with db() as conn:
            r = conn.execute("SELECT * FROM jobs WHERE status='queued' AND mode!='longfilm' ORDER BY created_at LIMIT 1").fetchone()
            if not r:
                return None
            conn.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?",
                         (time.time(), r["id"]))
            self.current = r["id"]
            return dict(r)

    async def _run(self, job_row):
        job_id = job_row["id"]
        params = json.loads(job_row["params"])
        mode = job_row["mode"]
        print(f"[worker] 开始任务 {job_id} ({mode})")
        # 批次任务：逐个批次独立跑，单个批次失败不影响其它批次
        if mode in ("t2i", "t2i_qwen", "i2i_qwen", "t2i_moss") and self._has_batches(job_id):
            await self._run_batches(job_row)
            return
        try:
            async with ClientSession(timeout=HTTP_TIMEOUT) as session:
                # 上传图片素材
                image_inputs = {}
                if mode == "flf":
                    for key in ("first_frame", "last_frame"):
                        up = params.get(key)
                        if up:
                            lp = UPLOAD_DIR / up
                            if not lp.exists():
                                raise RuntimeError(f"找不到上传文件: {up}")
                            image_inputs[key] = await comfy_upload_image(session, lp)
                    params["first_frame"] = image_inputs.get("first_frame")
                    params["last_frame"] = image_inputs.get("last_frame")
                elif mode == "r2v":
                    names = []
                    for up in params.get("ref_images", []):
                        lp = UPLOAD_DIR / up
                        if not lp.exists():
                            raise RuntimeError(f"找不到上传文件: {up}")
                        names.append(await comfy_upload_image(session, lp))
                    params["ref_images"] = names
                elif mode == "i2i_qwen":
                    # Qwen 图片编辑：参考图**来自素材库**（asset_id + 版本快照），
                    # 这里把素材库里的文件上传到 ComfyUI input，
                    # LoadImage 节点按返回的 'subfolder/name' 引用。
                    # 顺序必须与占位标记 <imageN> 严格一致 —— 这里保持列表原序。
                    names = await _upload_asset_refs(session, job_id, params)
                    params["ref_images"] = names

                # 素材库引用：把素材库里的文件按槽位上传到 ComfyUI，
                # 并在 params 里落成 ref_images / ref_videos / ref_audios。
                # i2i_qwen 的参考图已经在上面按 <imageN> 顺序处理过了，
                # 这里不能再覆盖（会打乱占位与插槽的对应关系）。
                if params.get("asset_refs") and mode != "i2i_qwen":
                    await _apply_asset_refs(session, job_id, params)

                # 参考生音乐（a2a）：参考音频已经在上面的 _apply_asset_refs
                # 里落进 params["ref_audios"]。这里取第一段，转成
                # SheetSage2AudioToABC 需要的 LoadAudio 输入名。
                if mode == "a2a":
                    audios = params.get("ref_audios") or []
                    if not audios:
                        raise RuntimeError("参考生音乐缺少参考音频")
                    # 重新上传到 input 顶层（LoadAudio 的 COMBO 只认顶层文件名）
                    flat_name = await _upload_ref_audio_flat(session, job_id, params)
                    params["ref_audio"] = flat_name

                params["job_id"] = job_id
                # 【关键，2026-11-09】params["mode"] 有两个语义：
                #   - HTTP 生成模式（t2a / a2a / t2i / t2v …）
                #   - 音乐工作流里的「谱面规划模式」（full / melody）
                # 以前这里直接 params["mode"] = mode，把用户选的谱面模式
                # 覆盖成了 t2a/a2a -> 任务记录里谱面模式永久丢失，
                # 作品卡片只能显示一个编造的「完整谱面」。
                # 现在把谱面模式单独存到 params["plan_mode"]，
                # music_yue2 优先读它（读不到再回退 params["mode"]）。
                if mode in ("t2a", "a2a"):
                    params["plan_mode"] = params.get("plan_mode") or params.get("music_plan") or "full"
                params["mode"] = mode
                workflow = build_workflow(mode, params)
                client_id = f"h3studio-{job_id}"
                async with self.submit_lock:
                    with db() as conn:
                        row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
                    if not row or row["status"] != "running":
                        return
                    prompt_id = await comfy_submit(session, workflow, client_id)
                    with db() as conn:
                        conn.execute("UPDATE jobs SET prompt_id=? WHERE id=?", (prompt_id, job_id))
                ws_task = asyncio.create_task(
                    ws_progress_listener(session, client_id, prompt_id, job_id, len(workflow)))

                # 轮询 history
                try:
                    while True:
                        await asyncio.sleep(CFG["poll_interval"])
                        # 检查取消
                        with db() as conn:
                            st = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
                        if st and st["status"] == "canceled":
                            print(f"[worker] 任务 {job_id} 已取消")
                            return

                        hist = await comfy_history(session, prompt_id)
                        if hist:
                            status_info = hist.get("status") or {}
                            if hist.get("outputs"):
                                outs = _extract_outputs(hist)
                                saved = []
                                for item in outs:
                                    fname = await comfy_download_output(session, item)
                                    saved.append({"kind": item["kind"], "file": fname, "name": item["filename"]})
                                with db() as conn:
                                    conn.execute(
                                        "UPDATE jobs SET status='done', progress=1, outputs=?, finished_at=? WHERE id=? AND status='running'",
                                        (json.dumps(saved, ensure_ascii=False), time.time(), job_id))
                                print(f"[worker] 任务 {job_id} 完成: {len(saved)} 个文件")
                                return
                            if status_info.get("status_str") == "error":
                                msgs = "; ".join(
                                    (m or [{}])[0].get("x", "") if isinstance(m, list) else str(m)
                                    for m in status_info.get("messages", []) if m and m[0] == "execution_error"
                                ) or "执行出错"
                                raise RuntimeError(msgs[:400])
                finally:
                    ws_task.cancel()
                    try:
                        await ws_task
                    except (asyncio.CancelledError, Exception):
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[worker] 任务 {job_id} 失败: {e}")
            with db() as conn:
                conn.execute("UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=? AND status='running'",
                             (str(e)[:800], time.time(), job_id))
        finally:
            if self.current == job_id:
                self.current = None


    def _has_batches(self, job_id: str) -> bool:
        with db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) c FROM job_batches WHERE job_id=?", (job_id,)).fetchone()
        return bool(row and row["c"] > 1)

    async def _run_batches(self, job_row):
        """逐批次执行：每个批次有独立的种子、状态、进度与产物。

        任何一个批次失败只标记该批次 failed，其余批次继续跑；
        任务整体状态 = done（至少一个成功）/ failed（全部失败）。
        """
        job_id = job_row["id"]
        params = json.loads(job_row["params"])
        with db() as conn:
            rows = conn.execute(
                "SELECT * FROM job_batches WHERE job_id=? ORDER BY idx", (job_id,)).fetchall()
        total = len(rows)
        done_count = 0
        for row in rows:
            batch_id = row["id"]
            with db() as conn:
                conn.execute(
                    "UPDATE job_batches SET status='running', started_at=? WHERE id=?",
                    (time.time(), batch_id))
            try:
                outputs = await self._run_one_batch(job_id, batch_id, params, row["seed"])
                done_count += 1
                with db() as conn:
                    conn.execute(
                        "UPDATE job_batches SET status='done', progress=1, outputs=?, "
                        "finished_at=? WHERE id=?",
                        (json.dumps(outputs, ensure_ascii=False), time.time(), batch_id))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[worker] 批次 {batch_id} 失败: {e}")
                with db() as conn:
                    conn.execute(
                        "UPDATE job_batches SET status='failed', error=?, finished_at=? WHERE id=?",
                        (str(e)[:800], time.time(), batch_id))
            # 更新任务级进度
            with db() as conn:
                conn.execute(
                    "UPDATE jobs SET progress=?, progress_text=? WHERE id=?",
                    (done_count / total, f"批次 {done_count}/{total}", job_id))
        # 汇总
        with db() as conn:
            rows2 = conn.execute(
                "SELECT status, outputs FROM job_batches WHERE job_id=?", (job_id,)).fetchall()
        succeeded = [r for r in rows2 if r["status"] == "done"]
        merged = []
        for r in succeeded:
            merged.extend(json.loads(r["outputs"] or "[]"))
        if succeeded:
            with db() as conn:
                conn.execute(
                    "UPDATE jobs SET status='done', progress=1, outputs=?, progress_text=?, "
                    "finished_at=? WHERE id=?",
                    (json.dumps(merged, ensure_ascii=False), f"{len(succeeded)}/{total} 批次成功",
                     time.time(), job_id))
        else:
            with db() as conn:
                conn.execute(
                    "UPDATE jobs SET status='failed', error=?, progress_text=?, finished_at=? WHERE id=?",
                    ("所有批次均失败", f"0/{total} 批次成功", time.time(), job_id))

    async def _run_one_batch(self, job_id: str, batch_id: str, params: dict, seed):
        """跑单个批次并返回产物列表（文件已落盘）。"""
        mode = params.get("mode") or "t2i"
        batch_params = dict(params)
        batch_params["seed"] = int(seed)
        batch_params["job_id"] = f"{job_id}_{batch_id}"
        batch_params["mode"] = mode
        client_id = f"h3studio-{batch_id}"
        async with ClientSession(timeout=HTTP_TIMEOUT) as session:
            # 参考图要上传到 ComfyUI input，LoadImage 才能引用
            if mode == "i2i_qwen":
                # 参考图来自素材库（asset_id + 版本快照），顺序即 <imageN> 语义
                names = await _upload_asset_refs(session, job_id, batch_params)
                batch_params["ref_images"] = names
            workflow = build_workflow(mode, batch_params)
            async with self.submit_lock:
                with db() as conn:
                    row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
                if not row or row["status"] not in ("running", "queued"):
                    raise RuntimeError("任务已取消")
                prompt_id = await comfy_submit(session, workflow, client_id)
                with db() as conn:
                    conn.execute("UPDATE job_batches SET prompt_id=? WHERE id=?",
                                 (prompt_id, batch_id))
            ws_task = asyncio.create_task(
                ws_progress_listener(session, client_id, prompt_id, batch_id, len(workflow),
                                     batch_id=batch_id))
            try:
                while True:
                    await asyncio.sleep(CFG["poll_interval"])
                    with db() as conn:
                        st = conn.execute("SELECT status FROM jobs WHERE id=?",
                                          (job_id,)).fetchone()
                        bst = conn.execute("SELECT status FROM job_batches WHERE id=?",
                                           (batch_id,)).fetchone()
                    if st and st["status"] == "canceled":
                        raise RuntimeError("任务已取消")
                    if bst and bst["status"] == "canceled":
                        raise RuntimeError("批次已取消")
                    hist = await comfy_history(session, prompt_id)
                    if hist:
                        if hist.get("outputs"):
                            outs = _extract_outputs(hist)
                            saved = []
                            for item in outs:
                                fname = await comfy_download_output(session, item)
                                saved.append({"kind": item["kind"], "file": fname,
                                              "name": item["filename"]})
                            return saved
                        status_info = hist.get("status") or {}
                        if status_info.get("status_str") == "error":
                            msgs = "; ".join(
                                (m or [{}])[0].get("x", "") if isinstance(m, list) else str(m)
                                for m in status_info.get("messages", []) if m and m[0] == "execution_error"
                            ) or "执行出错"
                            raise RuntimeError(msgs[:400])
            finally:
                ws_task.cancel()
                try:
                    await ws_task
                except (asyncio.CancelledError, Exception):
                    pass


WORKER = Worker()

# 一次提交最多几个批次
MAX_BATCH_COUNT = 5

# 历史上出现、但已不在当前注册表里的 mode —— 有独立类型标签，不进兜底类
LEGACY_UNKNOWN_MODES = list(gen_types.LEGACY_MODE_LABELS.keys())

# ------------------------------------------------------------------ Routes
def json_ok(payload=None, **kw):
    payload = payload or {}
    payload.update(kw)
    return web.json_response(payload)


def create_job_record(mode: str, params: dict) -> str:
    """
    建一条生成任务记录，返回 job_id。

    【为什么要抽成独立函数】
    POST /api/jobs（面板点「生成」）与内置 Agent 的 create_job 工具
    必须是**同一套**建任务逻辑 —— 两份实现必然漂移：
      * 生成类型（gen_type）写不进去 -> 任务列表标签变成「其它生成」
      * 批次拆分逻辑不同 -> 面板出 3 张、Agent 出 1 张
      * 素材引用（asset_refs）漏写 -> worker 拿不到参考图
    所以这里抽出唯一的实现，两边都调它。

    params 会被就地写入（补 gen_type / asset_refs），调用方按需自取。
    """
    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    # 生成类型写进任务 meta（真实记录，不靠读时推断）
    params["gen_type"] = gen_types.type_key_for_mode(mode)
    asset_refs = params.get("asset_refs")
    if asset_refs:
        params["asset_refs"] = asset_refs
    # 生成批次：文生图支持一次出 N 张（各自独立种子）
    batch_count = 1
    if mode in ("t2i", "t2i_qwen", "i2i_qwen", "t2i_moss"):
        try:
            batch_count = int(params.get("batch_count") or 1)
        except (TypeError, ValueError):
            batch_count = 1
        batch_count = min(MAX_BATCH_COUNT, max(1, batch_count))
    with db() as conn:
        conn.execute(
            "INSERT INTO jobs (id, created_at, mode, params, status) VALUES (?,?,?,?, 'queued')",
            (job_id, now, mode, json.dumps(params, ensure_ascii=False)))
        if batch_count > 1:
            base_seed = params.get("seed")
            for index in range(batch_count):
                # 种子递推：同参数批量出图但每张可区分
                if base_seed:
                    seed = int(base_seed) + index
                else:
                    seed = random.randint(0, 2**48)
                conn.execute(
                    "INSERT INTO job_batches (id, job_id, idx, seed, status, created_at) "
                    "VALUES (?,?,?,?, 'queued', ?)",
                    (uuid.uuid4().hex[:12], job_id, index, seed, now))
    return job_id


class JobValidationError(ValueError):
    """建任务前的参数校验失败。message 面向用户，直接可读。"""

    def __init__(self, message, code="bad_request", extra=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.extra = extra or {}


#: 允许的生成模式（与 workflows.BUILDERS 一致 —— 后端真实注册的模式）
JOB_MODES = ("t2v", "flf", "r2v", "t2i", "t2i_qwen", "i2i_qwen", "t2i_moss",
             "t2a", "a2a")


def prepare_job_params(mode: str, params: dict, asset_refs: list | None = None,
                       saved_files: dict | None = None) -> list:
    """
    校验并补全一个任务的参数。**就地修改 params**，返回最终的 asset_refs。

    【为什么要抽成独立函数】
    POST /api/jobs（面板点「生成」）与内置 Agent 的 create_job 工具
    必须是**同一套**校验 —— 各写一份必然漂移：
      * Agent 允许 i2i_qwen 不带参考图 -> 提交后才炸
      * Agent 允许 a2a 带 3 段音频 -> worker 报错
      * Agent 漏掉 <imageN> 占位注入 -> 图1 变成图2（极难查）
    所以这里抽出唯一的实现，两边都调它。

    失败一律抛 JobValidationError（带可读原因 + code）。
    """
    asset_refs = list(asset_refs or [])
    saved_files = saved_files or {}

    if mode not in JOB_MODES:
        raise JobValidationError(f"未知模式 {mode}")

    if mode == "flf":
        if saved_files.get("file_first"):
            params["first_frame"] = saved_files["file_first"][0]
        if saved_files.get("file_last"):
            params["last_frame"] = saved_files["file_last"][0]
        if not params.get("first_frame"):
            raise JobValidationError("首尾帧模式需要至少上传首帧图片")
    elif mode == "r2v":
        refs = saved_files.get("file_ref", [])
        if not refs:
            # 素材库来源的参考图也算（worker 会按 asset_refs 上传）
            if not [r for r in asset_refs
                    if isinstance(r, dict) and r.get("slot") == "ref_image"]:
                raise JobValidationError("参考模式需要至少 1 张参考图")
        else:
            params["ref_images"] = refs[:3]
    elif mode == "i2i_qwen":
        # Qwen-Image 2.1 图片编辑：**参考图强制来自素材库**。
        #
        # 需求原文：「编辑里的参考图，强制要求必须走素材库」。
        # 所以这里**不接受**本地上传通道（file_ref）作为图片编辑的参考图 ——
        # 走本地上传的会被明确拒绝，并提示改从素材库选择。
        # （上传到素材库本身仍然可用：素材库页有上传入口，
        #   入库后的条目带 asset_id，才能作为参考图。）
        uploaded = saved_files.get("file_ref", [])
        if uploaded:
            raise JobValidationError(
                "图片编辑的参考图必须来自素材库，不能直接上传本地图片。"
                "请先在「素材库」页上传并入库，再从素材库选择参考图。",
                code="ref_must_come_from_library")

        # 参考图元数据（asset_id + 描述 + 标签 + 顺序）由前端提交
        ref_specs = params.get("ref_images") or []
        if not isinstance(ref_specs, list):
            ref_specs = []
        refs = ref_imgs.normalize_refs(ref_specs)
        if len(refs) < ref_imgs.MIN_REF_IMAGES:
            raise JobValidationError(
                f"图片编辑至少需要 {ref_imgs.MIN_REF_IMAGES} 张参考图，"
                f"当前 {len(refs)} 张（参考图必须来自素材库）",
                code="ref_required")
        if len(refs) > ref_imgs.MAX_REF_IMAGES:
            raise JobValidationError(
                f"参考图最多 {ref_imgs.MAX_REF_IMAGES} 张，当前 {len(refs)} 张。"
                f"请删除多余参考图（上限 {ref_imgs.MAX_REF_IMAGES} 张，"
                f"模型官方能力上限 10 张）",
                code="ref_too_many")

        # 一致性校验：素材失效 / 占位符与图片数不匹配 -> 阻止提交并说明原因
        prompt_text = (params.get("prompt") or "").strip()
        check = ref_imgs.validate_refs(refs, prompt=prompt_text, require=True)
        if not check["ok"]:
            raise JobValidationError(
                "参考图校验未通过：" + "；".join(check["errors"]),
                code="ref_invalid", extra={"errors": check["errors"]})

        # 占位标记由服务端统一注入（保证与插槽顺序一致，前端不能自己乱填）
        params["prompt"] = ref_imgs.inject_reference_block(prompt_text, refs)
        params["ref_images"] = refs

    # 参考生音乐（a2a）：参考音频必须来自素材库（asset_refs, slot=ref_audio）
    if mode == "a2a":
        audio_refs = [r for r in (asset_refs or [])
                      if isinstance(r, dict) and r.get("slot") == "ref_audio"]
        if not audio_refs:
            raise JobValidationError(
                "参考生音乐需要选择一段参考音频（从素材库选择）。",
                code="ref_audio_required")
        if len(audio_refs) > 1:
            raise JobValidationError(
                f"参考生音乐只能选 1 段参考音频，当前 {len(audio_refs)} 段。",
                code="ref_audio_too_many")
        asset_refs = audio_refs

    if not (params.get("prompt") or "").strip():
        raise JobValidationError("提示词不能为空")
    return asset_refs


async def api_create(request):
    """POST /api/jobs  (multipart: mode, params(JSON), files[])"""
    reader = await request.multipart()
    mode = None
    params = {}
    saved_files = {}
    asset_refs = []
    async for part in reader:
        if part.name == "mode":
            mode = (await part.text()).strip()
        elif part.name == "params":
            try:
                params = json.loads(await part.text())
            except Exception:
                params = {}
        elif part.name == "asset_refs":
            # 素材库引用：[{"asset_id":"asset_xxx","slot":"first_frame","ordinal":0}]
            try:
                loaded = json.loads(await part.text())
                if isinstance(loaded, list):
                    asset_refs = loaded
            except Exception:
                asset_refs = []
        elif part.name in ("file_first", "file_last", "file_ref"):
            fname = await save_uploaded_image(part)
            saved_files.setdefault(part.name, []).append(fname)

    try:
        asset_refs = prepare_job_params(str(mode or ""), params, asset_refs,
                                        saved_files)
    except JobValidationError as exc:
        payload = {"error": exc.message, "code": exc.code}
        payload.update(exc.extra or {})
        return web.json_response(payload, status=400)

    # 生成批次：文生图支持一次出 N 张（各自独立种子）
    batch_count = 1
    if mode in ("t2i", "t2i_qwen", "i2i_qwen", "t2i_moss"):
        try:
            batch_count = int(params.get("batch_count") or 1)
        except (TypeError, ValueError):
            batch_count = 1
        batch_count = min(MAX_BATCH_COUNT, max(1, batch_count))

    # 素材库引用写进 params：worker 跑的时候据此把素材上传到 ComfyUI
    if asset_refs:
        params["asset_refs"] = asset_refs
    # 建任务记录走共享实现（与内置 Agent 的 create_job 工具同一条路）
    job_id = create_job_record(str(mode or ""), params)
    return json_ok({"id": job_id, "status": "queued", "batch_count": batch_count})


def _verified_video_path(job_id: str, relative: str) -> Path | None:
    """只允许引用已完成任务登记过的视频输出。"""
    with db() as conn:
        row = conn.execute("SELECT status, outputs FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row or row["status"] != "done":
        return None
    matched = any(
        output.get("kind") == "video" and output.get("file") == relative
        for output in json.loads(row["outputs"] or "[]")
    )
    if not matched:
        return None
    candidate = (OUT_DIR / (relative if "/" in relative else f"{job_id}/{relative}")).resolve()
    try:
        candidate.relative_to(OUT_DIR.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _probe_video_size(path: Path) -> tuple[int, int]:
    if not FFPROBE:
        return 864, 480
    raw = subprocess.check_output(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(path)],
        encoding="utf-8", timeout=15, creationflags=CREATE_NO_WINDOW)
    stream = (json.loads(raw).get("streams") or [{}])[0]
    width = max(2, int(stream.get("width") or 864)) // 2 * 2
    height = max(2, int(stream.get("height") or 480)) // 2 * 2
    return width, height


async def run_longfilm(job_id: str):
    output_path = None
    list_path = None
    try:
        async with LONGFILM_LOCK:
            with db() as conn:
                row = conn.execute("SELECT params, status FROM jobs WHERE id=?", (job_id,)).fetchone()
                if not row or row["status"] != "queued":
                    return
                conn.execute("UPDATE jobs SET status='running', started_at=?, progress=?, progress_text=? WHERE id=?",
                             (time.time(), 0.05, "准备片段", job_id))
            if not FFMPEG:
                raise RuntimeError("未找到 FFmpeg，无法合成长片")
            params = json.loads(row["params"] or "{}")
            paths = []
            for clip in params.get("clips", []):
                path = _verified_video_path(str(clip.get("job_id", "")), str(clip.get("file", "")))
                if not path:
                    raise RuntimeError("有片段已被删除或无法读取")
                paths.append(path)
            if len(paths) < 2:
                raise RuntimeError("长片至少需要 2 个视频片段")
            width, height = await asyncio.to_thread(_probe_video_size, paths[0])
            list_path = DATA / f"concat-{job_id}.txt"
            def quote_concat(path):
                return str(path).replace("'", "'\\''")
            list_path.write_text("\n".join(f"file '{quote_concat(path)}'" for path in paths), encoding="utf-8")
            output_path = _next_output_path("longfilm", ".mp4")
            vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                  f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,format=yuv420p")
            command = [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
                       "-vf", vf, "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                       "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-movflags", "+faststart",
                       str(output_path)]
            with db() as conn:
                conn.execute("UPDATE jobs SET progress=?, progress_text=? WHERE id=?",
                             (0.25, f"正在合成 {len(paths)} 个片段", job_id))
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW)
            LONGFILM_PROCESSES[job_id] = process
            _, stderr = await process.communicate()
            LONGFILM_PROCESSES.pop(job_id, None)
            with db() as conn:
                current = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if current and current["status"] == "canceled":
                if output_path.exists():
                    output_path.unlink()
                return
            if process.returncode != 0:
                message = stderr.decode("utf-8", errors="replace")[-1200:]
                raise RuntimeError("FFmpeg 合成失败：" + message)
            relative = output_path.relative_to(OUT_DIR).as_posix()
            outputs = [{"kind": "video", "file": relative, "name": output_path.name}]
            with db() as conn:
                conn.execute("UPDATE jobs SET status='done', progress=1, progress_text='合成完成', outputs=?, finished_at=? WHERE id=? AND status='running'",
                             (json.dumps(outputs, ensure_ascii=False), time.time(), job_id))
    except Exception as exc:
        if output_path and output_path.exists():
            output_path.unlink()
        with db() as conn:
            conn.execute("UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=? AND status IN ('queued','running')",
                         (str(exc)[:1200], time.time(), job_id))
    finally:
        LONGFILM_PROCESSES.pop(job_id, None)
        if list_path and list_path.exists():
            list_path.unlink()


async def api_longfilm_create(request):
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误"}, status=400)
    title = str(payload.get("title") or "").strip()
    intro = str(payload.get("intro") or "").strip()
    clips = payload.get("clips") or []
    if not title:
        return web.json_response({"error": "请填写长片标题"}, status=400)
    if len(clips) < 2:
        return web.json_response({"error": "至少选择 2 个视频片段"}, status=400)
    verified = []
    for clip in clips[:100]:
        job_id = str(clip.get("job_id") or "")
        relative = str(clip.get("file") or "")
        if not _verified_video_path(job_id, relative):
            return web.json_response({"error": "所选视频中有文件已失效"}, status=400)
        verified.append({"job_id": job_id, "file": relative, "name": str(clip.get("name") or "")})
    job_id = uuid.uuid4().hex[:12]
    params = {"title": title[:120], "intro": intro[:1000], "clips": verified,
              "duration": payload.get("duration")}
    with db() as conn:
        conn.execute("INSERT INTO jobs (id, created_at, mode, params, status, progress_text) VALUES (?,?,?,?, 'queued', '等待合成')",
                     (job_id, time.time(), "longfilm", json.dumps(params, ensure_ascii=False)))
    task = asyncio.create_task(run_longfilm(job_id))
    LONGFILM_TASKS.add(task)
    task.add_done_callback(LONGFILM_TASKS.discard)
    return json_ok({"id": job_id, "status": "queued"})


async def api_list(request):
    status = request.query.get("status")
    conditions = []
    args = []
    if status and status != "all":
        conditions.append("status=?")
        args.append(status)
    media = request.query.get("media", "").strip()
    if media == "image":
        # 媒体维度按「生成类型注册表」的 media 字段推导，而不是硬编码 mode 列表 ——
        # 硬编码会把 t2i_qwen / i2i_qwen 这类新增的图像模式漏掉
        # （实测漏了 9 条 Qwen 出图记录：生成库「图片」筛选里一条都看不到）。
        image_modes = [m for m in gen_types.registered_modes()
                       if gen_types.type_for_mode(m).get("media") == "image"]
        if not image_modes:
            image_modes = ["t2i"]
        conditions.append("mode IN (%s)" % ",".join("?" * len(image_modes)))
        args.extend(image_modes)
    elif media == "video":
        video_modes = [m for m in gen_types.registered_modes()
                       if gen_types.type_for_mode(m).get("media") == "video"]
        conditions.append("mode IN (%s)" % ",".join("?" * len(video_modes)))
        args.extend(video_modes)
    elif media == "longfilm":
        conditions.append("mode='longfilm'")
    elif media:
        return web.json_response({"error": "media 只能是 image、video 或 longfilm"}, status=400)
    # 生成类型筛选（与 status 叠加）：type key -> 对应的 mode 列表
    gen_type = request.query.get("gen_type", "").strip()
    if gen_type:
        if gen_type == "__other__":
            # 兜底类：既不是当前注册的模式，也不是已知历史 mode 的那些
            known = list(gen_types.registered_modes()) + list(LEGACY_UNKNOWN_MODES)
            conditions.append("mode NOT IN (%s)" % ",".join("?" * len(known)))
            args.extend(known)
        else:
            mapping = gen_types.type_filter_keys()
            modes = mapping.get(gen_type)
            if not modes:
                return web.json_response({"error": f"未知的生成类型 {gen_type}"}, status=400)
            conditions.append("mode IN (%s)" % ",".join("?" * len(modes)))
            args.extend(modes)
    aspect = request.query.get("aspect", "").strip()
    if aspect:
        if aspect not in ASPECT_NUM:
            return web.json_response({"error": "不支持的画面比例"}, status=400)
        conditions.append("json_extract(params, '$.aspect')=?")
        args.append(aspect)
    duration = request.query.get("duration", "").strip()
    if duration:
        try:
            duration_value = float(duration)
        except ValueError:
            return web.json_response({"error": "duration 必须是数字"}, status=400)
        conditions.append("CAST(json_extract(params, '$.duration') AS REAL)=?")
        args.append(duration_value)
    keyword = request.query.get("keyword", "").strip()
    if keyword:
        like = f"%{keyword}%"
        conditions.append("(params LIKE ? OR id LIKE ? OR outputs LIKE ?)")
        args.extend((like, like, like))
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    try:
        page = max(1, int(request.query.get("page", "1")))
        page_size = min(100, max(1, int(request.query.get("page_size", "200"))))
    except ValueError:
        return web.json_response({"error": "page 和 page_size 必须是整数"}, status=400)
    offset = (page - 1) * page_size
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM jobs" + where, args).fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM jobs" + where + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*args, page_size, offset],
        ).fetchall()
    jobs = []
    for r in rows:
        job = row_to_job(r)
        job["batches"] = batches_of(r["id"])
        jobs.append(job)
    return json_ok({"jobs": jobs,
                    "total": total, "page": page, "page_size": page_size,
                    "running": WORKER.current})


async def api_get(request):
    jid = request.match_info["id"]
    with db() as conn:
        r = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not r:
        return web.json_response({"error": "not found"}, status=404)
    job = row_to_job(r)
    job["batches"] = batches_of(jid)
    return json_ok(job)


async def api_cancel(request):
    jid = request.match_info["id"]
    async with WORKER.submit_lock:
        with db() as conn:
            r = conn.execute("SELECT status, prompt_id, mode FROM jobs WHERE id=?", (jid,)).fetchone()
        if not r:
            return web.json_response({"error": "not found"}, status=404)
        if r["status"] not in ("running", "queued"):
            return json_ok({"status": r["status"]})
        pid = r["prompt_id"]
        if r["mode"] == "longfilm":
            process = LONGFILM_PROCESSES.get(jid)
            if process and process.returncode is None:
                process.terminate()
            with db() as conn:
                conn.execute("UPDATE jobs SET status='canceled', finished_at=? WHERE id=? AND status IN ('queued','running')",
                             (time.time(), jid))
            return json_ok({"status": "canceled"})
        if pid:
            try:
                async with ClientSession(timeout=ClientTimeout(total=6)) as session:
                    for endpoint, payload in (("queue", {"delete": [pid]}), ("interrupt", {"prompt_id": pid})):
                        async with session.post(f"{COMFY}/{endpoint}", json=payload) as resp:
                            resp.raise_for_status()
            except Exception as exc:
                return web.json_response({"error": f"无法确认停止请求，请重试：{exc}"}, status=502)
        with db() as conn:
            conn.execute("UPDATE jobs SET status='canceled', finished_at=? WHERE id=? AND status IN ('queued','running','failed')", (time.time(), jid))
            status = conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()["status"]
        return json_ok({"status": status})



async def api_delete(request):
    jid = request.match_info["id"]
    with db() as conn:
        r = conn.execute("SELECT status, outputs FROM jobs WHERE id=?", (jid,)).fetchone()
        if not r:
            return web.json_response({"error": "not found"}, status=404)
        if r["status"] in ("queued", "running"):
            return web.json_response({"error": "请先取消再删除"}, status=400)
        conn.execute("DELETE FROM jobs WHERE id=?", (jid,))
    for output in json.loads(r["outputs"] or "[]"):
        relative = output.get("file", "")
        if "/" not in relative:
            continue
        target = (OUT_DIR / relative).resolve()
        if str(target).startswith(str(OUT_DIR.resolve())) and target.is_file():
            target.unlink()
    shutil.rmtree(OUT_DIR / jid, ignore_errors=True)
    return json_ok({"deleted": jid})


async def fetch_comfy_queue(session):
    """读 ComfyUI 的真实队列：{running: n, pending: n}。

    状态栏必须反映 ComfyUI 实际在跑什么，而不只是 Studio 自己的 worker ——
    用户可以直接在 ComfyUI 里提交任务，那边在跑，Studio 也该显示「生成中」。
    读不到就返回 None（表示未知，不当成空闲）。
    """
    try:
        async with session.get(f"{COMFY}/queue") as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        return {
            "running": len(data.get("queue_running") or []),
            "pending": len(data.get("queue_pending") or []),
        }
    except Exception:
        return None


async def status_snapshot(session=None):
    """状态栏所需的全部信息（一次算完，轮询与 SSE 共用）。

    running_job: Studio 自己正在跑的任务 id（没有则 None）
    comfy_running / comfy_pending: ComfyUI 真实队列（未知时为 None）
    queued: Studio 库里排队中的任务数
    """
    with db() as conn:
        queued = conn.execute(
            "SELECT COUNT(*) c FROM jobs WHERE status='queued'").fetchone()["c"]
    longfilm_running = next(iter(LONGFILM_PROCESSES), None)
    running_job = WORKER.current or longfilm_running
    queue = await fetch_comfy_queue(session) if session else None
    return {
        "queued": queued,
        "running": running_job,
        "comfy_running": queue["running"] if queue else None,
        "comfy_pending": queue["pending"] if queue else None,
    }


async def api_system(request):
    """队列状态 + ComfyUI 状态"""
    sysinfo = None
    snap = None
    try:
        async with ClientSession(timeout=ClientTimeout(total=3)) as s:
            async with s.get(f"{COMFY}/system_stats") as resp:
                if resp.status == 200:
                    sysinfo = await resp.json()
            snap = await status_snapshot(s)
    except Exception:
        pass
    if snap is None:
        snap = await status_snapshot(None)
    return json_ok({
        "queued": snap["queued"],
        "running": snap["running"],
        "comfy_running": snap["comfy_running"],
        "comfy_pending": snap["comfy_pending"],
        "comfyui_ok": sysinfo is not None,
        "comfyui": (sysinfo or {}).get("system") or {},
        "devices": (sysinfo or {}).get("devices") or [],
        # 后端指纹 + 功能位：前端据此判断「我连的是不是最新后端」
        # （前端静态文件改了立刻生效、后端改了要重启 —— 两者错配时
        #   界面上是空白下拉 / 400 未知模式这类极难自查的问题）。
        "build_fingerprint": BUILD_FINGERPRINT,
        "features": SERVER_FEATURES,
        "started_at": SERVER_STARTED_AT,
    })


async def api_status_stream(request):
    """SSE：把状态快照实时推给前端。

    前端用 EventSource 订阅，状态一变就重绘，不必等下一次轮询。
    每 1 秒推一次（只在内容变化时真正写出），连接断开自动收尾。
    """
    resp = web.StreamResponse(
        headers={"Content-Type": "text/event-stream",
                 "Cache-Control": "no-cache",
                 "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    last = None
    try:
        async with ClientSession(timeout=ClientTimeout(total=3)) as s:
            while True:
                try:
                    snap = await status_snapshot(s)
                    payload = json.dumps({
                        "queued": snap["queued"],
                        "running": snap["running"],
                        "comfy_running": snap["comfy_running"],
                        "comfy_pending": snap["comfy_pending"],
                    }, ensure_ascii=False)
                    # 只在内容变化时推，避免前端无谓重绘
                    if payload != last:
                        last = payload
                        await resp.write(f"data: {payload}\n\n".encode("utf-8"))
                except (ConnectionResetError, asyncio.CancelledError):
                    break
                except Exception:
                    pass
                await asyncio.sleep(1.0)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        try:
            await resp.write_eof()
        except Exception:
            pass
    return resp


async def api_unload(request):
    try:
        async with ClientSession() as s:
            await s.post(f"{COMFY}/unload")
        return json_ok({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ------------------------------------------------------------------ 图像模式参数清单
# 图像面板的档位 / 比例 / 模型 / 采样器 / 调度器清单。
# 模型只列本机 ComfyUI 实测存在的权重（见 h3studio.workflows.Z_IMAGE_MODELS）。
#
# 采样器 / 调度器列表**按模型版本区分**：
#   - Turbo 系（蒸馏）：karras / exponential 调度器在 8 步下会崩图 -> 不给
#   - Base（非蒸馏）：exponential 采样器反而可用 -> 给
# 依据见《Z-Image 最佳参数与提示词增强 Skill 范式_技术调研.md》§1.5 / §1.6。

_Z_IMAGE_TIER_LABELS = {
    "1mp": "1MP（官方默认）",
    "480p": "480P",
    "720p": "720P",
    "1080p": "1080P",
    "1440p": "1440P",
    "2k": "2K",
    "4k": "4K",
}


async def api_music_options(request):
    """GET /api/music-options —— 音乐面板可选项（模型/采样参数/本机可用性）

    模型清单来自 h3studio/music_yue2，权重是否在本机由该模块实测磁盘给出 ——
    前端据此把不可用的版本标灰并禁用，而不是等提交后才报错。
    """
    return json_ok(music_yue2.model_options())


async def api_image_options(request):
    """GET /api/image-options —— 图像面板可选项（含每个模型版本的推荐参数）"""
    tiers = []
    for key, megapixels in Z_IMAGE_RES_TIERS.items():
        tiers.append({
            "key": key,
            "label": _Z_IMAGE_TIER_LABELS.get(key, key.upper()),
            "megapixels": megapixels,
            "max_direct_mp": Z_IMAGE_MAX_DIRECT_MP,
        })
    aspects = []
    for key, label in ASPECT_MAP.items():
        w, h = ASPECT_NUM[key]
        aspects.append({"key": key, "label": label, "ratio": [w, h]})
    models = {}
    for key, m in Z_IMAGE_MODELS.items():
        models[key] = {
            "label": m["label"],
            "unet": m["unet"],
            "note": m["note"],
            "warn": m["warn"],
            "sampler": m["sampler"],
            "scheduler": m["scheduler"],
            "steps": m["steps"],
            "cfg": m["cfg"],
            "shift": m["shift"],
            "supports_negative": m["supports_negative"],
            "samplers": m["samplers"],
            "schedulers": m["schedulers"],
            # 【加速 LoRA 固定在工作流里】—— 只告诉界面「这个版本内置了
            # 加速 LoRA」以便在说明里显示，**名字本身不下发到可选列表**。
            "has_accel_lora": bool(m.get("accel_lora")),
            # 步数/CFG 与加速 LoRA 绑定 -> 界面要锁定这几个控件
            "params_locked_by_accel": bool(m.get("params_locked_by_accel")),
        }
    # Qwen-Image 2.1 的档位/比例与 Z-Image 共用同一套 ResolutionSelector 语义，
    # 但档位表与倍数不同（VAE 16 倍下采样 -> 宽高取 16 的倍数）。
    qwen_tiers = []
    for key, megapixels in QWEN_IMAGE_RES_TIERS.items():
        qwen_tiers.append({
            "key": key,
            "label": _Z_IMAGE_TIER_LABELS.get(key, key.upper()),
            "megapixels": megapixels,
            "max_direct_mp": QWEN_IMAGE_MAX_DIRECT_MP,
        })
    qwen_models = {}
    for key, m in QWEN_IMAGE_MODELS.items():
        # 权重是否在本机 —— 前端据此把该版本标成「不可用」并给出说明，
        # 而不是让用户选完才在跑图时炸掉。
        available = qwen_unet_available(m["unet"])
        qwen_models[key] = {
            "label": m["label"],
            "unet": m["unet"],
            "note": m["note"],
            "warn": m["warn"],
            "sampler": m["sampler"],
            "scheduler": m["scheduler"],
            "steps": m["steps"],
            "cfg": m["cfg"],
            "shift": m["shift"],
            "supports_negative": m["supports_negative"],
            "negative_effective_only_above_cfg1": m.get("negative_effective_only_above_cfg1", False),
            "samplers": m["samplers"],
            "schedulers": m["schedulers"],
            # 权重是否就位（False 时前端置灰该版本）
            "available": available,
            # Turbo 走固定 sigma 表（前端据此提示「步数与 schedule 绑定」）
            "manual_sigmas_steps": m.get("manual_sigmas_steps"),
            # PE 开关涉及的编码器（供前端显示当前用哪个）
            "clip_t2i": m["clip_t2i"],          # PE 编码器（文生图）
            "clip_i2i": m["clip_i2i"],          # PE 编码器（图片编辑）
            "clip_cond": QWEN_IMAGE_CLIP_COND,  # 条件编码器（TextEncode 的 clip 输入）
            # Qwen-Image 2.1 没有加速 LoRA（bfs_head 是换头效果类）
        }
    # MOSS-Image 的档位/比例：官方 USERGUIDE 推荐 1M / 1.5M / 2M 总像素，
    # 可外推 0.25M(512^2)~4M(2K)。Flux2 VAE 16 倍下采样 -> 宽高取 16 的倍数。
    moss_tiers = []
    for key, megapixels in MOSS_IMAGE_RES_TIERS.items():
        moss_tiers.append({
            "key": key,
            "label": _Z_IMAGE_TIER_LABELS.get(key, key.upper()),
            "megapixels": megapixels,
            "max_direct_mp": MOSS_IMAGE_MAX_DIRECT_MP,
        })
    moss_models = {}
    for key, m in MOSS_IMAGE_MODELS.items():
        # 底模目录是否在本机 —— 前端据此把该版本标成「不可用」并给出说明
        available = moss_image_model_available(m)
        moss_models[key] = {
            "label": m["label"],
            "model_path": m["model_path"],
            "note": m["note"],
            "warn": m["warn"],
            "sampler": m["sampler"],
            "scheduler": m["scheduler"],
            "steps": m["steps"],
            "cfg": m["cfg"],
            "shift": m["shift"],
            "supports_negative": m["supports_negative"],
            "samplers": m["samplers"],
            "schedulers": m["schedulers"],
            # SLG（SkipLayerGuidance）skip layer —— 官方推荐 22
            "slg_single_layers": m["slg_single_layers"],
            "slg_double_layers": m["slg_double_layers"],
            # 底模是否就位（False 时前端置灰该版本）
            "available": available,
        }
    return json_ok({
        "tiers": tiers,
        "aspects": aspects,
        "models": models,
        "size_multiple": Z_IMAGE_SIZE_MULTIPLE,
        "max_direct_mp": Z_IMAGE_MAX_DIRECT_MP,
        "default_tier": Z_IMAGE_DEFAULT_TIER,
        "default_model": Z_IMAGE_DEFAULT_MODEL,
        # 自定义宽高的合法范围（ComfyUI EmptySD3LatentImage 节点 schema：
        # min=16 max=16384 step=16）
        "size_min": Z_IMAGE_SIZE_MIN,
        "size_max": Z_IMAGE_SIZE_MAX,
        # Qwen-Image 2.1 独立的一套（文生图 + 图片编辑共用）
        "qwen": {
            "tiers": qwen_tiers,
            "aspects": aspects,
            "models": qwen_models,
            "size_multiple": QWEN_IMAGE_SIZE_MULTIPLE,
            "max_direct_mp": QWEN_IMAGE_MAX_DIRECT_MP,
            "default_tier": QWEN_IMAGE_DEFAULT_TIER,
            "default_model": QWEN_IMAGE_DEFAULT_MODEL,
            # 参考图数量限制（模型官方能力上限 10 张 / ComfyUI 节点 16 槽 -> 取 10）
            "max_ref_images": QWEN_IMAGE_MAX_REF_IMAGES,
            "min_ref_images": QWEN_IMAGE_MIN_REF_IMAGES,
            "node_ref_slots": QWEN_IMAGE_NODE_REF_SLOTS,
            # 官方 PE 两个 checkpoint 的参数（前端显示当前档位，不可互换）
            "pe_params": QWEN_IMAGE_PE_PARAMS,
            "pe_default_max_length": QWEN_IMAGE_PE_DEFAULT_MAX_LENGTH,
        },
        # MOSS-Image 独立的一套（文生图）
        "moss": {
            "tiers": moss_tiers,
            "aspects": aspects,
            "models": moss_models,
            "size_multiple": MOSS_IMAGE_SIZE_MULTIPLE,
            "max_direct_mp": MOSS_IMAGE_MAX_DIRECT_MP,
            "min_mp": MOSS_IMAGE_MIN_MP,
            "default_tier": MOSS_IMAGE_DEFAULT_TIER,
            "default_model": MOSS_IMAGE_DEFAULT_MODEL,
            # 官方推荐的负面提示词（前端作为默认值预填）
            "default_negative": MOSS_IMAGE_DEFAULT_NEGATIVE,
        },
    })


async def api_generation_types(request):
    """GET /api/generation-types —— 右侧任务「按生成类型」标签的清单

    清单来自后端真实注册的生成模式（workflows.BUILDERS + longfilm），
    前端不硬编码任何类型名。
    """
    return json_ok({
        "types": gen_types.list_types(),
        "registered_modes": gen_types.registered_modes(),
        # 界面分类（文生图 / 图片编辑 / 视频）—— 前端左侧入口按这个分组
        "categories": gen_types.list_categories(),
    })


async def api_image_loras(request):
    """GET /api/image-loras —— 本机实际存在的 Z-Image LoRA 清单 + 当前配置

    清单只列本机真实存在的文件（跟 ComfyUI 自己的 lora 下拉同源），
    配置存服务端（局域网所有客户端共用同一份）。
    """
    # 只下发「效果类」LoRA —— 加速/蒸馏类由工作流固定使用，
    # 不进用户可选列表（用户自选加速器会与 Turbo 自身蒸馏冲突）。
    #
    # base 决定用哪一族：Z-Image 与 Qwen-Image 2.1 的底模结构不同，
    # 兼容性要各自比对、配置也要分开存，混在一起会让用户选到不匹配的权重。
    base = request.query.get("base", "") or ""
    qwen = lora_registry.is_qwen_base(base)
    moss = lora_registry.is_moss_base(base)
    if moss:
        available = lora_registry.list_moss_effect_loras()
        incompatible = lora_registry.list_moss_incompatible_loras()
        acceleration = [item for item in lora_registry.list_moss_image_loras()
                        if item["kind"] == lora_registry.KIND_ACCELERATION]
    elif qwen:
        available = lora_registry.list_qwen_effect_loras()
        incompatible = lora_registry.list_qwen_incompatible_loras()
        acceleration = [item for item in lora_registry.list_qwen_image_loras()
                        if item["kind"] == lora_registry.KIND_ACCELERATION]
    else:
        available = lora_registry.list_effect_loras()
        incompatible = lora_registry.list_incompatible_loras()
        acceleration = lora_registry.list_acceleration_loras()
    with db() as conn:
        config = lora_registry.load_config(conn, lora_registry.pref_key_for_base(base))
    # 配置里的 LoRA 若已从磁盘移除，标记 missing 而不是静默丢弃
    by_name = {item["name"]: item for item in available}
    # 已被排除的（不兼容）也要能对上 —— 前端要显示「为什么被排除」
    excluded_by_name = {item["name"]: item for item in incompatible}
    resolved = []
    for item in config:
        name = item.get("name") or ""
        meta = by_name.get(name)
        excluded = excluded_by_name.get(name)
        # 触发词：以前端保存的为准（用户可能改过），否则用注册表里的
        trigger = item.get("trigger")
        if trigger is None:
            trigger = (meta or excluded or {}).get("trigger", "")
        resolved.append({
            "name": name,
            "strength": float(item.get("strength", 1.0)),
            "enabled": bool(item.get("enabled", True)),
            # missing = 文件不在了；excluded = 文件在但不兼容（前端要区别对待）
            "missing": meta is None and excluded is None,
            "excluded": excluded is not None,
            "incompatible_reason": (excluded or {}).get("incompatible_reason", ""),
            "display": (meta or excluded or {}).get("display") or item.get("display") or name,
            "trigger": trigger or "",
            "recommended_strength": (meta or excluded or {}).get("recommended_strength", 1.0),
            "strength_cap": (meta or excluded or {}).get("strength_cap", 1.0),
            # context_refiner 阻尼信息（界面提示用；服务端会自动应用）
            "ctx_damping": (meta or excluded or {}).get("ctx_damping"),
            "ctx_perturbation": (meta or excluded or {}).get("ctx_perturbation"),
            "kind": (meta or excluded or {}).get("kind", lora_registry.KIND_EFFECT),
        })
    return json_ok({"available": available,
                    "acceleration": acceleration,
                    "incompatible": incompatible,
                    "config": resolved})


async def api_image_loras_save(request):
    """PUT /api/image-loras —— 保存 LoRA 配置（服务端持久化）

    每一项都会校验文件是否真实存在，不存在直接 400 报错，不静默忽略。
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "请求体必须是 JSON"}, status=400)
    items = body.get("loras")
    if not isinstance(items, list):
        return web.json_response({"error": "loras 必须是数组"}, status=400)
    if len(items) > 8:
        return web.json_response({"error": "最多 8 个 LoRA"}, status=400)
    # 配置按底模族分开存
    base = body.get("base", "") or ""
    pref_key = lora_registry.pref_key_for_base(base)
    qwen = lora_registry.is_qwen_base(base)
    moss = lora_registry.is_moss_base(base)
    if moss:
        all_effect = lora_registry.list_moss_effect_loras(include_incompatible=True)
    elif qwen:
        all_effect = lora_registry.list_qwen_effect_loras(include_incompatible=True)
    else:
        all_effect = lora_registry.list_effect_loras(include_incompatible=True)
    cleaned = []
    for item in items:
        if not isinstance(item, dict):
            return web.json_response({"error": "每个 LoRA 必须是对象"}, status=400)
        name = (item.get("name") or "").strip()
        if not name:
            continue
        resolved, path = lora_registry.resolve_lora(name)
        if not resolved or path is None:
            return web.json_response(
                {"error": f"本机不存在该 LoRA 文件：{name}"}, status=400)
        # 真·不兼容的 LoRA 拒绝保存（要 patch 的层在底模里根本不存在）
        meta = next((m for m in all_effect if m["name"] == resolved), {})
        # 跨底模族误用拒绝保存：MOSS 底模挂 Z-Image 的 LoRA 会静默失效
        # （ComfyUI 只打 "lora key not loaded" 的 WARNING，出图照跑但没生效）
        if moss and not lora_registry.is_moss_family_lora(resolved):
            return web.json_response({
                "error": f"「{name}」不是 MOSS-Image 的 LoRA，不能加入 MOSS-Image 的 "
                          "LoRA 列表：底模结构不同，键名对不上，ComfyUI 会静默丢弃。"
            }, status=400)
        if meta and not meta.get("compatible", True):
            base_label = ("MOSS-Image" if moss else
                          "Qwen-Image 2.1" if qwen else "Z-Image")
            return web.json_response({
                "error": f"「{meta.get('display') or name}」与 {base_label} 底模不兼容，"
                          f"不能加入 LoRA 列表：{meta.get('incompatible_reason') or '未知原因'}"
            }, status=400)
        try:
            strength = float(item.get("strength", 1.0))
        except (TypeError, ValueError):
            return web.json_response({"error": f"strength 必须是数字：{name}"}, status=400)
        # 幅度过大的 LoRA -> 夹到安全上限（实测 1.0 出噪点、0.8 安全）
        cap = float(meta.get("strength_cap", 1.0)) if meta else 1.0
        strength = min(cap, max(0.0, strength))
        # 触发词：用户改过的以用户为准（留空 = 不追加）；没传就用注册表里的
        trigger = item.get("trigger")
        if trigger is None:
            trigger = meta.get("trigger", "")
        cleaned.append({
            "name": resolved,
            "strength": strength,
            "enabled": bool(item.get("enabled", True)),
            "trigger": str(trigger or ""),
            "recommended_strength": meta.get("recommended_strength", 1.0),
            "strength_cap": meta.get("strength_cap", 1.0),
            # context_refiner 阻尼信息 —— 工作流侧会自动套用阻尼后的派生 LoRA
            "ctx_damping": meta.get("ctx_damping"),
            "ctx_perturbation": meta.get("ctx_perturbation"),
            "kind": meta.get("kind", lora_registry.KIND_EFFECT),
        })
    with db() as conn:
        lora_registry.save_config(conn, cleaned, pref_key)
    return json_ok({"loras": cleaned})


# ------------------------------------------------------------------ 硬件信息
_HW_CACHE = {"t": 0, "data": None}
_MODEL_CACHE = {"t": 0, "data": None}
psutil.cpu_percent(interval=None)  # 预热，避免首次全 0


def _cpu_name():
    """CPU 型号名。

    旧实现只读 Windows 注册表，Linux 上必然失败；回退到 platform.processor()
    在 Linux 上返回空串，于是页面上显示「未知 CPU」。
    现在统一走 h3studio.hardware.detect_cpu()，多级来源降级，
    最差也返回「架构 · 核心数」的可辨识兜底。
    """
    return hw.detect_cpu()["name"]


def _gpu_info():
    """优先 nvidia-smi，取不到则回退 torch/ComfyUI 信息。

    每张卡附带 power_limit_info（可调范围 + 是否支持 + 不可用原因）。
    """
    try:
        return hw.query_gpus()
    except Exception:
        return []


def _scan_models(root: Path):
    """统计模型库各分类的文件数与体积"""
    result = []
    if not root.exists():
        return result
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        cnt, size = 0, 0
        try:
            for f in d.rglob("*"):
                if f.is_file() and f.stat().st_size > 1024 * 1024:
                    cnt += 1
                    size += f.stat().st_size
        except Exception:
            continue
        if cnt:
            result.append({"name": d.name, "count": cnt, "size": size})
    result.sort(key=lambda x: -x["size"])
    return result


async def api_hardware(request):
    now = time.time()
    if _HW_CACHE["data"] and now - _HW_CACHE["t"] < 1.5:
        return json_ok(_HW_CACHE["data"])

    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    disks = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts or not part.fstype:
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
            disks.append({"device": part.device.replace("\\", ""), "mount": part.mountpoint,
                          "total": u.total, "used": u.used, "free": u.free,
                          "percent": u.percent, "fstype": part.fstype})
        except Exception:
            continue

    comfy = {}
    try:
        async with ClientSession(timeout=ClientTimeout(total=3)) as s:
            async with s.get(f"{COMFY}/system_stats") as resp:
                if resp.status == 200:
                    comfy = await resp.json()
    except Exception:
        pass

    cpu_info = hw.detect_cpu()
    power_caps = hw.query_power_limits()

    data = {
        "cpu": {
            # name: 显示名（异构时形如 "A x12 + B x12"）
            "name": cpu_info["name"],
            "model": cpu_info["model"],
            "cores": cpu_info["cores"],
            "threads": cpu_info["threads"],
            "percent": psutil.cpu_percent(interval=None),
            "freq": round(psutil.cpu_freq().current) if psutil.cpu_freq() else None,
            "architecture": cpu_info["architecture"],
            "vendor": cpu_info["vendor"],
            "source": cpu_info["source"],
            "heterogeneous": cpu_info["heterogeneous"],
            "models": cpu_info["models"],
        },
        "ram": {"total": vm.total, "used": vm.used, "available": vm.available,
                "percent": vm.percent, "swap_total": sm.total, "swap_used": sm.used},
        "gpus": _gpu_info(),
        # 功耗上限整体能力（nvidia-smi 缺失 / 驱动不支持时 available=false + reason）
        "power_limit": {
            "available": power_caps["available"],
            "reason": power_caps["reason"],
        },
        "disks": disks,
        "os": {"platform": platform.platform(), "machine": platform.machine(),
               "hostname": platform.node(), "boot": psutil.boot_time()},
        "software": {
            "comfyui": (comfy.get("system") or {}).get("comfyui_version"),
            "python": (comfy.get("system") or {}).get("python_version"),
            "pytorch": (comfy.get("system") or {}).get("pytorch_version"),
            "studio_python": platform.python_version(),
            "cuda_devices": [{"name": d.get("name"), "vram_total": d.get("vram_total")}
                             for d in (comfy.get("devices") or [])],
        },
        "models_dir": CFG.get("models_dir", ""),
    }

    # 模型库统计较慢，单独缓存 120s
    if not _MODEL_CACHE["data"] or now - _MODEL_CACHE["t"] > 120:
        loop = asyncio.get_running_loop()
        _MODEL_CACHE["data"] = await loop.run_in_executor(
            None, _scan_models, Path(CFG.get("models_dir", "")))
        _MODEL_CACHE["t"] = now
    data["models"] = _MODEL_CACHE["data"]

    _HW_CACHE.update(t=now, data=data)
    return json_ok(data)


# ------------------------------------------------------------------ 本地模型 Provider
async def api_gpu_power_limit(request):
    """POST /api/gpu/power-limit —— 修改单张显卡的功耗上限（Power Limit）。

    body: {"index": 0, "watts": 200, "allow_raise": false}
      - index  : 显卡序号（nvidia-smi 的 GPU index）
      - watts  : 目标功耗上限（W）
      - allow_raise: 是否允许**提高**功耗上限。默认 false —— 安全约束：
        默认只允许下调；提高必须由调用方显式确认。

    返回 {ok, index, requested, previous, current, verified, message}
    失败时 ok=false 且 message 说明原因（不支持 / 超范围 / 拒绝提高 / 回读不一致）。
    """
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "请求格式错误"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "请求格式错误"}, status=400)

    index = payload.get("index")
    watts = payload.get("watts")
    allow_raise = bool(payload.get("allow_raise", False))
    if index is None or watts is None:
        return web.json_response(
            {"ok": False, "error": "缺少参数 index / watts"}, status=400)

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, hw.set_power_limit, index, watts, allow_raise)
    if not result.get("ok"):
        return web.json_response(result, status=400)
    # 功耗上限已变，硬件面板缓存必须失效，否则前端仍读到旧值
    _HW_CACHE.update(t=0, data=None)
    return json_ok(result)


async def api_providers_list(request):
    """GET /api/providers —— 列出全部 Provider + 默认项设置"""
    with db() as conn:
        items = prov.list_providers(conn)
        prefs = prov.prefs_snapshot(conn)
    return json_ok({"providers": items, **prefs,
                    "types": [{"value": k, "label": v}
                              for k, v in prov.PROVIDER_TYPE_LABELS.items()]})


async def api_providers_create(request):
    """POST /api/providers —— 新增 Provider"""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误"}, status=400)
    try:
        data = prov.normalize_provider(payload)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    with db() as conn:
        if prov.get_provider(conn, data["id"]):
            return web.json_response({"error": "该 ID 已存在"}, status=409)
        # ComfyUI 类型默认开启代理：节点自己关不掉 thinking 模型，
        # 走 H3 Studio 的 OpenAI 兼容代理才能拿到干净结果。
        # 代理地址指向本服务自己的 /api/llm/p/{id}/v1
        if data["type"] == "comfyui" and not payload.get("use_proxy"):
            data["use_proxy"] = 1
        if data["type"] == "comfyui" and data["use_proxy"] and not data["proxy_base_url"]:
            data["proxy_base_url"] = f"{_studio_self_url()}{LLM_PROXY_PREFIX}/{data['id']}/v1"
        item = prov.insert_provider(conn, data)
        # 第一个 Provider 自动成为默认，避免用户必须先手动设置
        prefs = prov.prefs_snapshot(conn)
        if not prefs.get("default_provider_id"):
            prov.set_pref(conn, "default", item["id"])
            prefs["default_provider_id"] = item["id"]
        if item["multimodal"] and not prefs.get("default_multimodal_provider_id"):
            prov.set_pref(conn, "default_multimodal", item["id"])
            prefs["default_multimodal_provider_id"] = item["id"]
    return json_ok({"provider": item, **prefs})


async def api_providers_update(request):
    """PUT /api/providers/{id} —— 修改 Provider"""
    pid = request.match_info["id"]
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误"}, status=400)
    payload = dict(payload)
    payload["id"] = pid
    try:
        data = prov.normalize_provider(payload)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    with db() as conn:
        current = prov.get_provider_full(conn, pid)
        if not current:
            return web.json_response({"error": "Provider 不存在"}, status=404)
        # 前端不回显 API Key 明文：没带 api_key 字段就保持原值
        if not payload.get("api_key"):
            data["api_key"] = None
        item = prov.update_provider(conn, pid, data)
        prefs = prov.prefs_snapshot(conn)
    return json_ok({"provider": item, **prefs})


async def api_providers_delete(request):
    """DELETE /api/providers/{id} —— 删除 Provider"""
    pid = request.match_info["id"]
    with db() as conn:
        if not prov.delete_provider(conn, pid):
            return web.json_response({"error": "Provider 不存在"}, status=404)
        prefs = prov.prefs_snapshot(conn)
    return json_ok({"deleted": pid, **prefs})


async def api_providers_default(request):
    """POST /api/providers/{id}/default —— 设为默认 Provider
    body: {"multimodal": true} 表示设为「默认多模态模型」"""
    pid = request.match_info["id"]
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    want_multimodal = bool(payload.get("multimodal"))
    with db() as conn:
        item = prov.get_provider_full(conn, pid)
        if not item:
            return web.json_response({"error": "Provider 不存在"}, status=404)
        if not item["enabled"]:
            return web.json_response({"error": "该 Provider 已停用，无法设为默认"}, status=400)
        if want_multimodal and not item["multimodal"]:
            return web.json_response(
                {"error": "该 Provider 未标记多模态能力，无法设为默认多模态模型"}, status=400)
        prov.set_pref(conn, "default_multimodal" if want_multimodal else "default", pid)
        prefs = prov.prefs_snapshot(conn)
    return json_ok({"ok": True, **prefs})


async def api_providers_test(request):
    """POST /api/providers/{id}/test —— 连通性探测（不生成内容）"""
    pid = request.match_info["id"]
    with db() as conn:
        item = prov.get_provider_full(conn, pid)
    if not item:
        return web.json_response({"error": "Provider 不存在"}, status=404)
    result = await llm_client.check_provider(item)
    return json_ok({"ok": result["ok"], "detail": result["detail"],
                    "models": result["models"], "provider_id": pid})


# ------------------------------------------------------------------ 提示词增强 Agent
async def api_enhancer_config(request):
    """
    GET /api/enhancer —— 读取增强 Agent 的模板清单与参数

    可选查询参数 ?mode=t2i&model=z_image_turbo_bf16：
      返回该上下文下适用的模板（filter_specs），并带上 default_spec_id。
      不带 mode 时返回全部模板 + 各模式的过滤结果，前端可自行切换。
    """
    mode = (request.query.get("mode") or "").strip()
    model_key = (request.query.get("model") or "").strip()
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM model_provider_prefs WHERE key=?",
            ("enhancer",)).fetchone()
    saved = {}
    if row:
        try:
            saved = json.loads(row["value"] or "{}")
        except Exception:
            saved = {}
    specs = enh.list_specs()
    for spec in specs:
        custom = saved.get("specs", {}).get(spec["id"])
        if custom:
            spec["customized"] = True
            spec["custom_system_prompt"] = custom.get("system_prompt", "")
            spec["custom_user_template"] = custom.get("user_template", "")
        else:
            # 未自定义时，把内置模板正文回给前端，让用户能在界面里编辑
            full = enh.get_spec(spec["id"], saved)
            spec["builtin_system_prompt"] = full["system_prompt"]
            spec["builtin_user_template"] = full["user_template"]

    payload = {
        "specs": specs,
        "params": enh.resolve_params(None, saved),
        "default_params": enh.DEFAULT_PARAMS,
        "length_presets": enh.LENGTH_PRESETS,
        "sampling_presets": enh.SAMPLING_PRESETS,
        "skill_dir": str(enh.SKILL_DIR),
        # 上下文 -> 适用模板 id 列表（前端按当前模式/模型取用；新增模型只改服务端这两张表）
        "mode_filters": {
            m: [s["id"] for s in enh.filter_specs(specs, m)]
            for m in enh.MODE_MEDIA
        },
        "mode_defaults": dict(enh.MODE_DEFAULT_SPEC),
        # 模式 -> 媒体类型（前端据此做 media 维度过滤）
        "mode_media": dict(enh.MODE_MEDIA),
        # 模型族映射（前端据此判断当前模型属于哪一族）
        "model_families": dict(enh.MODEL_FAMILY),
    }
    if mode:
        filtered = enh.filter_specs(specs, mode, model_key)
        payload["mode"] = mode
        payload["model"] = model_key
        payload["mode_specs"] = filtered
        payload["default_spec_id"] = enh.default_spec_for_mode(mode, specs, model_key)
    return json_ok(payload)

async def api_enhancer_save(request):
    """PUT /api/enhancer —— 保存用户可编辑的模板覆盖 + 参数"""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误"}, status=400)
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM model_provider_prefs WHERE key=?",
            ("enhancer",)).fetchone()
        saved = {}
        if row:
            try:
                saved = json.loads(row["value"] or "{}")
            except Exception:
                saved = {}
        specs = saved.get("specs") or {}
        for spec_id, custom in (payload.get("specs") or {}).items():
            if not isinstance(custom, dict):
                continue
            if spec_id not in enh.TEMPLATE_SPECS:
                return web.json_response({"error": f"未知模板 {spec_id}"}, status=400)
            system_prompt = str(custom.get("system_prompt") or "").strip()
            user_template = str(custom.get("user_template") or "").strip()
            if user_template and "{prompt}" not in user_template \
                    and "{original}" not in user_template:
                return web.json_response(
                    {"error": "用户模板必须包含 {prompt} 或 {original} 占位符"}, status=400)
            if not system_prompt and not user_template:
                # 两个字段都空 = 恢复内置模板，删掉覆盖项
                specs.pop(spec_id, None)
                continue
            specs[spec_id] = {"system_prompt": system_prompt, "user_template": user_template}
        saved["specs"] = specs
        if payload.get("params"):
            saved["params"] = {k: v for k, v in payload["params"].items()
                               if k in enh.DEFAULT_PARAMS}
        prov.set_pref(conn, "enhancer", json.dumps(saved, ensure_ascii=False))
    return json_ok({"ok": True, "params": enh.resolve_params(None, saved)})


async def api_enhance(request):
    """
    POST /api/enhance —— 提示词增强（迷你 Agent 调用本地模型）

    body:
      prompt      要扩写的原文
      mode        replace | append（落地策略，默认 replace）
      spec_id     模板 id，默认 image
      provider_id 指定 Provider；不给则用默认（要读图则用默认多模态）
      images      可选，本地图片路径列表（需要读图的场景）
    """
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误"}, status=400)

    original = str(payload.get("prompt") or "").strip()
    if not original:
        return web.json_response({"error": "请先输入要增强的提示词"}, status=400)
    if len(original) > 4000:
        return web.json_response({"error": "提示词过长（上限 4000 字）"}, status=400)

    mode = payload.get("mode") or "replace"
    if mode not in ("replace", "append"):
        return web.json_response({"error": "mode 只能是 replace 或 append"}, status=400)

    spec_id = payload.get("spec_id") or "image"
    # 上下文一致性：若请求带了 mode，而 spec_id 在该模式下不适用，
    # 则回落到该模式的默认模板 —— 保证「下拉显示」与「实际执行」一致。
    mode_ctx = str(payload.get("mode_ctx") or payload.get("mode_kind") or "").strip()
    model_key = str(payload.get("model_key") or "").strip()
    if mode_ctx:
        spec_id = enh.resolve_spec_for_mode(spec_id, mode_ctx, model_key=model_key)
    images = payload.get("images") or []
    if not isinstance(images, list):
        images = []
    images = [str(p) for p in images[:4]]

    with db() as conn:
        row = conn.execute(
            "SELECT value FROM model_provider_prefs WHERE key=?",
            ("enhancer",)).fetchone()
    saved = {}
    if row:
        try:
            saved = json.loads(row["value"] or "{}")
        except Exception:
            saved = {}

    spec = enh.get_spec(spec_id, saved)
    params = enh.resolve_params(payload.get("params"), saved, spec)

    # 选 Provider：显式指定 > 默认多模态（有图时） > 默认
    with db() as conn:
        provider_id = payload.get("provider_id")
        item = None
        if provider_id:
            item = prov.get_provider_full(conn, provider_id)
            if not item:
                return web.json_response({"error": "指定的 Provider 不存在"}, status=404)
            if not item["enabled"]:
                return web.json_response({"error": "该 Provider 已停用"}, status=400)
        else:
            if images:
                item = prov.resolve_default(conn, "default_multimodal", require_multimodal=True)
                if not item:
                    return web.json_response(
                        {"error": "需要读图，但未找到可用的多模态 Provider。"
                                 "请在「本地模型」页配置并设为默认多模态模型。"}, status=400)
            else:
                item = prov.resolve_default(conn, "default")
                if not item:
                    return web.json_response(
                        {"error": "尚未配置可用的本地模型 Provider。"
                                 "请先在「本地模型」页添加并启用一个 Provider。"}, status=400)

    messages = enh.build_messages(spec, original, params.get("language", "auto"))
    try:
        raw = await llm_client.call_provider(
            item, messages, images=images or None,
            options={"temperature": params["temperature"],
                     "top_p": params["top_p"],
                     "max_tokens": params["max_tokens"],
                     "do_sample": params.get("do_sample", True)})
    except llm_client.ProviderError as exc:
        # 本地模型不可达 —— 明确报错，不静默失败
        return web.json_response(
            {"error": str(exc), "provider": item["name"], "provider_id": item["id"],
             "degraded": True}, status=502)

    # thinking 残留（llm 层已剥离一次，这里再兜一次，防不同后端行为差异）
    raw = enh.strip_thinking(raw)
    enhanced = enh.strip_wrapping(raw)
    if not enhanced:
        return web.json_response(
            {"error": "模型返回了空内容，未做修改", "provider": item["name"],
             "degraded": True}, status=502)
    # 小模型可能把系统提示词复读回来 —— 这不是有效的提示词，按失败处理
    if enh.looks_like_echo(enhanced, spec["system_prompt"]):
        return web.json_response(
            {"error": f"Provider「{item['name']}」返回的是系统提示词复读，不是有效的提示词。"
                      "该模型可能太小或未按指令输出，请换用更强的模型。",
             "provider": item["name"], "provider_id": item["id"],
             "degraded": True}, status=502)

    # 硬约束校验（质量词 / 权重语法 / think 段 / bbox 结构）
    validation = enh.validate_output(enhanced, original,
                                     spec.get("validate_mode", "pe"))
    if not validation.get("ok", True):
        return web.json_response(
            {"error": "扩写结果违反 Z-Image 提示词硬约束："
                      + "；".join(validation.get("errors", [])),
             "errors": validation.get("errors", []),
             "provider": item["name"], "provider_id": item["id"],
             "degraded": True}, status=502)

    # bbox 档：额外抽出 JSON 数组，前端可单独渲染/复制
    elements = None
    if spec.get("validate_mode") == "bbox":
        blob = enh.extract_json_block(enhanced)
        if blob:
            try:
                elements = json.loads(blob)
            except Exception:
                elements = None

    merged = enh.merge_result(original, enhanced, mode)
    return json_ok({
        "original": original,
        "enhanced": enhanced,
        "merged": merged,
        "mode": mode,
        "provider": item["name"],
        "provider_id": item["id"],
        "spec_id": spec["id"],
        "used_multimodal": bool(images),
        "validation": validation,
        "elements": elements,
        "params": {"temperature": params["temperature"], "top_p": params["top_p"],
                   "max_tokens": params["max_tokens"],
                   "sampling": params.get("sampling", "sample"),
                   "length": params.get("length", "medium")},
    })


# ------------------------------------------------------------------ MOSS 结构化增强（layout）
async def api_moss_layout(request):
    """
    POST /api/moss-layout —— MOSS-Image 结构化（layout）提示词增强

    这是 ComfyUI-Moss 插件里 MossPrompt 节点的**外部复现**：
      - 不接 OpenAI 官方 API，也不接任何外部 API
      - 模型调用走本项目已有的 provider 配置（本地模型）
      - 模型只出 layout 草稿；schema 校验与坐标编译都在本地做
        （编译算法与官方 scripts/compile_prompt.py 逐条对齐）

    body:
      prompt      要增强的原始提示词（可含已有增强结果）
      intent      可选，画面意图补充说明
      width       画布宽（必须是 16 的倍数，且与出图尺寸一致）
      height      画布高
      provider_id 可选，指定 Provider；不给则用默认 Provider

    返回:
      draft        草稿（原样，abs/xyxy 左上原点）
      layout       编译后的 MOSS Layout（abs_centralized/yxyx）
      layout_json  编译后 layout 的紧凑 JSON 文本
      final_prompt 由 layout 确定性翻译出的自然语言提示词（送 CLIPTextEncode）

    失败一律 4xx/5xx + 可读原因，**不返回半成品**。
    """
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误"}, status=400)

    original = str(payload.get("prompt") or "").strip()
    if not original:
        return web.json_response({"error": "请先输入要增强的提示词"}, status=400)
    if len(original) > 4000:
        return web.json_response({"error": "提示词过长（上限 4000 字）"}, status=400)

    intent = str(payload.get("intent") or "").strip()
    if len(intent) > 1000:
        return web.json_response({"error": "画面意图过长（上限 1000 字）"}, status=400)

    try:
        width = int(payload.get("width"))
        height = int(payload.get("height"))
    except Exception:
        return web.json_response(
            {"error": "画布尺寸无效：需要给出 width 与 height"}, status=400)
    if width <= 0 or height <= 0:
        return web.json_response({"error": "画布宽高必须是正整数"}, status=400)
    # 画布尺寸用**后端权威**的吸附规则归一：
    # workflows.normalize_moss_image_size() 才是 MOSS 出图尺寸的唯一定义
    # （16 倍数 + 单边 16~16384 + 总像素 0.25M~4M）。
    # 前端若自己算一份必然走偏（同一常量写两处的问题）——
    # 所以这里由后端归一，并把生效尺寸回传给前端显示。
    width, height, size_reasons = normalize_moss_image_size(width, height)
    if width <= 0 or height <= 0:
        return web.json_response(
            {"error": "画布尺寸无效：" + "；".join(size_reasons)}, status=400)

    # 选 Provider：显式指定 > 默认。与 /api/enhance 走同一套配置体系，
    # 不新增第二套 provider 配置。
    with db() as conn:
        provider_id = payload.get("provider_id")
        item = None
        if provider_id:
            item = prov.get_provider_full(conn, provider_id)
            if not item:
                return web.json_response({"error": "指定的 Provider 不存在"}, status=404)
            if not item["enabled"]:
                return web.json_response({"error": "该 Provider 已停用"}, status=400)
        else:
            item = prov.resolve_default(conn, "default")
            if not item:
                return web.json_response(
                    {"error": "尚未配置可用的本地模型 Provider。"
                             "请先在「本地模型」页添加并启用一个 Provider。"}, status=400)

    messages = moss_layout.build_messages(original, width, height, intent)
    try:
        raw = await llm_client.call_provider(
            item, messages,
            options={"temperature": 0.3, "top_p": 0.9, "max_tokens": 1400,
                     "do_sample": False})
    except llm_client.ProviderError as exc:
        # 本地模型不可达 —— 明确报错，不静默失败
        return web.json_response(
            {"error": str(exc), "provider": item["name"], "provider_id": item["id"],
             "degraded": True}, status=502)

    raw = enh.strip_thinking(raw)
    try:
        result = moss_layout.enhance(original, raw, width, height, intent)
    except moss_layout.LayoutError as exc:
        # 模型返回的内容没通过 schema 校验 —— 明确报错 + 逐条原因
        return web.json_response(
            {"error": str(exc), "errors": exc.errors,
             "provider": item["name"], "provider_id": item["id"],
             "degraded": True}, status=502)

    return json_ok({
        "ok": True,
        "original": original,
        "intent": intent,
        "provider": item["name"],
        "provider_id": item["id"],
        "draft": result["draft"],
        "layout": result["layout"],
        "layout_json": result["layout_json"],
        "final_prompt": result["final_prompt"],
        "width": result["width"],
        "height": result["height"],
        # 画布尺寸的归一说明（前端提示用：用户填的值可能被吸附过）
        "size_notes": size_reasons,
    })


# ------------------------------------------------------------------ Qwen 官方 PE 扩写
async def api_qwen_pe_expand(request):
    """
    POST /api/qwen-pe/expand —— Qwen-Image 2.1 官方 PE 提示词扩写

    「增强」下拉里的「qwen21图片官方增强PE」走这个接口。
    与 /api/enhance 的区别：那个走本项目配置的 Provider + 文本 system prompt；
    这个走**官方 PE checkpoint 本身**（ComfyUI TextGenerate + 官方 PE 权重
    + 官方 system prompt + 官方采样参数）。

    body:
      prompt      要扩写的原始提示词
      mode        t2i_qwen（文生图）| i2i_qwen（图片编辑）—— 决定 PE 档位
      refs        可选，图片编辑的参考图（素材库条目列表）；顺序即 <imageN> 顺序
      system_prompt  可选，用户在「增强」多选列表里编辑过的官方 PE system prompt；
                     不传 = 用官方内置模板（与出图链路同一份文本）
      pe_params   可选，用户编辑过的官方 PE 档位参数
                   （presence_penalty / max_new_tokens），不传 = 官方卡值

    返回:
      expanded    扩写后的提示词（前端写回提示词框，可继续编辑）
      tier        实际用的 PE 档位（t2i / i2i）
      tier_label  档位中文名
      clip_name   实际用的官方 PE checkpoint 文件名
      ref_count   参考图数量
      pe_params   实际套用的档位参数（前端用于回显「推荐参数已套用」）

    失败一律 4xx/5xx + 可读原因，**不返回半成品**。
    """
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误"}, status=400)

    original = str(payload.get("prompt") or "").strip()
    if not original:
        return web.json_response({"error": "请先输入要增强的提示词"}, status=400)
    if len(original) > 4000:
        return web.json_response({"error": "提示词过长（上限 4000 字）"}, status=400)

    mode = str(payload.get("mode") or "t2i_qwen").strip()
    if mode not in ("t2i_qwen", "i2i_qwen"):
        return web.json_response(
            {"error": "官方 PE 扩写只适用于 Qwen-Image 2.1 的文生图 / 图片编辑模式"}, status=400)
    is_edit = mode == "i2i_qwen"

    # 参考图：素材库来源，与出图链路同一套约束（顺序即 <imageN> 顺序）。
    # 这里要的是**能直接喂给 ComfyUI LoadImage 的文件名**，
    # 所以走 _upload_ref_images（按入参顺序上传，顺序严格保持）。
    # 注意：它**不登记** asset_refs —— 扩写只是读一次图，不是出图任务。
    ref_images: list[str] = []
    raw_refs = payload.get("refs") or []
    if is_edit and raw_refs:
        try:
            async with ClientSession(timeout=HTTP_TIMEOUT) as session:
                names = await _upload_ref_images(session, raw_refs)
                ref_images = names
        except Exception as exc:
            return web.json_response(
                {"error": f"参考图上传失败：{exc}"[:300], "degraded": True}, status=502)

    # 用户编辑过的官方 PE system prompt（不传 = 官方内置模板）。
    # 走「增强」多选列表时，前端会把该项的可编辑字段一起传上来 ——
    # 扩写与出图共用同一份文本（出图链路见 params["pe_system_prompt"]）。
    system_prompt = payload.get("system_prompt")
    if system_prompt is not None:
        system_prompt = str(system_prompt)
        if not system_prompt.strip():
            return web.json_response(
                {"error": "官方 PE 的 system prompt 不能为空（清空请改用「不使用官方 PE」）"},
                status=400)

    # 用户编辑过的官方 PE 档位参数（不传 = 官方卡值）
    pe_params = payload.get("pe_params")
    if pe_params is not None and not isinstance(pe_params, dict):
        pe_params = None

    try:
        async with ClientSession(timeout=ClientTimeout(total=HTTP_TIMEOUT.total)) as session:
            result = await qwen_pe.run_pe_expansion(
                session, COMFY, original, is_edit, ref_images or None,
                system_prompt=system_prompt, pe_params=pe_params)
    except qwen_pe.PEError as exc:
        return web.json_response(
            {"error": str(exc), "degraded": True}, status=502)
    except Exception as exc:
        return web.json_response(
            {"error": f"官方 PE 扩写失败：{type(exc).__name__} {exc}"[:300],
             "degraded": True}, status=502)

    return json_ok({
        "ok": True,
        "original": original,
        "expanded": result["expanded"],
        "tier": result["tier"],
        "tier_label": result["tier_label"],
        "clip_name": result["clip_name"],
        "ref_count": result["ref_count"],
        "pe_params": result.get("pe_params"),
        "mode": mode,
    })


# ------------------------------------------------------------------ LLM 代理
# ComfyUI 的 VRGDG_LocalLLM 节点自己拼 payload（没有 chat_template_kwargs），
# 对 Qwen3 这类 thinking 模型关不掉思考，接口返回 content=null，节点再
# str(None) 变成字符串 "None" —— 提示词彻底丢失。
#
# 解决办法：H3 Studio 暴露一个 OpenAI 兼容代理，转发时强制带上
# enable_thinking=false，并把 reasoning_content 兜底成 content。
# ComfyUI 节点指向这个代理即可。
LLM_PROXY_PREFIX = "/api/llm/p"


def _studio_self_url() -> str:
    """本服务自己的可访问地址（供 ComfyUI 节点回连）。"""
    host = CFG.get("host") or "127.0.0.1"
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return f"http://{host}:{CFG['port']}"


async def api_llm_proxy_models(request):
    """GET /api/llm/p/{id}/v1/models —— 代理模型列表（供节点探测）"""
    pid = request.match_info["id"]
    with db() as conn:
        item = prov.get_provider_full(conn, pid)
    if not item:
        return web.json_response({"error": "Provider 不存在"}, status=404)
    target = (item.get("llm_base_url") or item["base_url"]).rstrip("/")
    if not target.endswith("/v1"):
        target += "/v1"
    try:
        async with ClientSession(timeout=ClientTimeout(total=15)) as session:
            async with session.get(f"{target}/models",
                                   headers=llm_client._auth_headers(item)) as resp:
                body = await resp.text()
        return web.Response(text=body, status=200,
                            content_type="application/json")
    except Exception as exc:
        return web.json_response(
            {"error": f"代理请求失败：{type(exc).__name__} {exc}"[:200]}, status=502)


async def api_llm_proxy_chat(request):
    """
    POST /api/llm/p/{id}/v1/chat/completions —— OpenAI 兼容代理。

    转发到该 Provider 的 LLM 服务，强制关闭 thinking，
    并把 reasoning_content 兜底成 content（thinking 模型 content 常为 null）。
    """
    pid = request.match_info["id"]
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误"}, status=400)

    with db() as conn:
        item = prov.get_provider_full(conn, pid)
    if not item:
        return web.json_response({"error": "Provider 不存在"}, status=404)

    target = (item.get("llm_base_url") or item["base_url"]).rstrip("/")
    if not target.endswith("/v1"):
        target += "/v1"

    forward = dict(payload)
    # 强制关闭 thinking（不支持时由下游忽略）
    forward["chat_template_kwargs"] = {"enable_thinking": False}
    forward.setdefault("stream", False)

    try:
        async with ClientSession(timeout=ClientTimeout(total=300)) as session:
            async with session.post(f"{target}/chat/completions", json=forward,
                                    headers=llm_client._auth_headers(item)) as resp:
                raw = await resp.text()
                status = resp.status
    except Exception as exc:
        return web.json_response(
            {"error": f"代理请求失败：{type(exc).__name__} {exc}"[:200]}, status=502)

    if status != 200:
        return web.Response(text=raw, status=status, content_type="application/json")

    try:
        body = json.loads(raw)
    except Exception:
        return web.Response(text=raw, status=200, content_type="application/json")

    # thinking 模型：content 为 null 时用 reasoning_content 兜底
    choices = body.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        content = message.get("content")
        if content is None or (isinstance(content, str) and not content.strip()):
            reasoning = message.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning.strip():
                message["content"] = reasoning
                choices[0]["message"] = message
                body["choices"] = choices
                raw = json.dumps(body, ensure_ascii=False)
    return web.Response(text=raw, status=200, content_type="application/json")


# ------------------------------------------------------------------ 内置多模态 Agent
#
# Agent 的能力边界与设计见 docs/内置多模态Agent-架构设计.md。
# 这里只做 HTTP 适配层：解析请求 -> 调 h3studio.agent.run_agent -> 返回结构化结果。

#: 同时运行的 Agent 会话数上限（本地模型单卡，并发只会互相拖慢）
AGENT_CONCURRENCY = agent_mod.MAX_CONCURRENT
#: 并发闸门（惰性创建，避免 import 期就占住事件循环）
AGENT_GATE: asyncio.Semaphore | None = None


def _agent_gate() -> asyncio.Semaphore:
    global AGENT_GATE
    if AGENT_GATE is None:
        AGENT_GATE = asyncio.Semaphore(AGENT_CONCURRENCY)
    return AGENT_GATE


def _agent_store():
    return agent_sessions.get_store()


def _agent_error(exc: agent_mod.AgentError) -> web.Response:
    payload = {"error": exc.message, "code": exc.code}
    payload.update(exc.extra or {})
    return web.json_response(payload, status=exc.status)


async def api_agent_chat(request):
    """
    POST /api/agent/chat —— 内置多模态 Agent 主入口。

    body:
      message       用户意图（必填，上限 4000 字）
      session_id    会话 ID（可选；不给则新建，多轮对话靠它串联）
      provider_id   指定本地模型 Provider（可选；不给则用「本地模型配置」里的默认）
      max_steps     最大步骤数（可选，默认 6，上限 6）

    返回结构化结果（见 h3studio.agent.run_agent 的 docstring）：
      status / answer / jobs / assets / prompt / steps / usage / provider / error
    """
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误", "code": "bad_request"},
                                 status=400)

    message = str(payload.get("message") or "").strip()
    if not message:
        return web.json_response({"error": "请输入要处理的内容",
                                  "code": "bad_request"}, status=400)

    session_id = str(payload.get("session_id") or "").strip()
    provider_id = str(payload.get("provider_id") or "").strip() or None
    try:
        max_steps = int(payload.get("max_steps") or agent_mod.MAX_STEPS)
    except (TypeError, ValueError):
        max_steps = agent_mod.MAX_STEPS

    store = _agent_store()
    session_id = store.create(session_id or None)
    # 存用户消息（在跑 Agent 之前存 —— 即使 Agent 挂了，历史也不丢）
    store.add_message(session_id, "user", message)

    history = store.history(session_id, limit=agent_sessions.HISTORY_LIMIT)
    # history 末尾那条就是本次刚存的 user 消息 —— 去掉，避免重复喂给模型
    if history and history[-1].get("role") == "user" \
            and history[-1].get("content") == message:
        history = history[:-1]

    # 并发闸门：抢不到就立刻 429，**不排队**。
    #
    # 为什么用 locked() 而不是 wait_for(acquire(), timeout=0)：
    # 实测（50 并发 × 20 轮）wait_for(acquire(), timeout=0) **一次都抢不到**
    # —— timeout=0 会在 acquire 真正执行前就取消它，结果是所有请求都 429。
    # locked() 判据则实测 peak 并发恰好等于上限，无超发。
    # （asyncio.Semaphore.acquire 内部是「先减计数再 await」，本身是原子的。）
    gate = _agent_gate()
    if gate.locked():
        return web.json_response(
            {"error": f"当前已有 {AGENT_CONCURRENCY} 个 Agent 任务在运行，"
                      "请稍候再试。",
             "code": "too_many_requests"}, status=429)

    async with gate:
        try:
            result = await agent_mod.run_agent(
                sys.modules[__name__], message, session_id=session_id,
                provider_id=provider_id, max_steps=max_steps, history=history)
        except agent_mod.AgentError as exc:
            store.add_message(session_id, "assistant", f"[请求失败] {exc.message}")
            return _agent_error(exc)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"[:300]
            store.add_message(session_id, "assistant", f"[请求失败] {detail}")
            return web.json_response(
                {"error": f"Agent 执行失败：{detail}", "code": "agent_internal_error"},
                status=500)
    # 落库：模型结论 + 工具调用轨迹（下一轮对话时作为上下文）
    if result.get("answer"):
        store.add_message(session_id, "assistant", result["answer"])
    for step in result.get("steps") or []:
        tool_name = step.get("tool")
        if not tool_name:
            continue
        note = step.get("error") or "ok"
        store.add_message(session_id, "tool", f"{tool_name}: {note}",
                           tool_name=tool_name)

    result["session_id"] = session_id
    result["provider_name"] = (result.get("provider") or {}).get("name") or ""
    return json_ok(result)


async def api_agent_chat_stream(request):
    """
    POST /api/agent/chat/stream —— 内置多模态 Agent 主入口（SSE 流式）。

    与 POST /api/agent/chat 的**请求体完全一致**，区别只在响应形式：
      * /api/agent/chat      —— 一次性返回完整 JSON
      * /api/agent/chat/stream —— SSE，过程中先推 progress 事件，最后推 result 事件

    为什么要有流式：本地模型单步就要 20~120 秒，一条完整链路 4~6 分钟。
    非流式的话，整个等待期间界面毫无反馈，实测 179 秒用户以为卡死
    （审查报告 P3-3）。流式把「现在到哪一步」实时告诉用户。

    事件格式（text/event-stream）：
      event: progress
      data: {"stage":"tool","detail":"正在检索素材库","done":1,"total":8,"elapsed_ms":31200}

      event: result
      data: { ...与 /api/agent/chat 相同的完整 JSON... }

      event: error
      data: {"error":"...","code":"..."}

    失败语义与 /api/agent/chat 对齐：并发超限返回 HTTP 429（非 SSE），
    参数错误返回 HTTP 400（非 SSE），便于前端统一处理。
    """
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误", "code": "bad_request"},
                                 status=400)

    message = str(payload.get("message") or "").strip()
    if not message:
        return web.json_response({"error": "请输入要处理的内容",
                                  "code": "bad_request"}, status=400)

    session_id = str(payload.get("session_id") or "").strip()
    provider_id = str(payload.get("provider_id") or "").strip() or None
    try:
        max_steps = int(payload.get("max_steps") or agent_mod.MAX_STEPS)
    except (TypeError, ValueError):
        max_steps = agent_mod.MAX_STEPS

    store = _agent_store()
    session_id = store.create(session_id or None)
    store.add_message(session_id, "user", message)
    history = store.history(session_id, limit=agent_sessions.HISTORY_LIMIT)
    if history and history[-1].get("role") == "user" \
            and history[-1].get("content") == message:
        history = history[:-1]

    # 并发闸门：与 /api/agent/chat 完全一致（抢不到就 429，不排队）
    gate = _agent_gate()
    if gate.locked():
        return web.json_response(
            {"error": f"当前已有 {AGENT_CONCURRENCY} 个 Agent 任务在运行，"
                      "请稍候再试。",
             "code": "too_many_requests"}, status=429)

    # 进度队列：Agent 编排器通过 on_progress 往里塞事件，
    # 这里边跑边从队列里取出来写成 SSE。
    queue: asyncio.Queue = asyncio.Queue()

    def _on_progress(stage: str, detail: str, done: int, total: int) -> None:
        queue.put_nowait({
            "stage": stage,
            "detail": detail or "",
            "done": int(done or 0),
            "total": int(total or 0),
        })

    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })
    await resp.prepare(request)

    async def _write(event: str, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False)
        await resp.write(f"event: {event}\ndata: {body}\n\n".encode("utf-8"))

    async def _runner() -> dict | None:
        """跑 Agent，把 progress 事件实时转发出去。返回最终结果。"""
        try:
            async with gate:
                result = await agent_mod.run_agent(
                    sys.modules[__name__], message, session_id=session_id,
                    provider_id=provider_id, max_steps=max_steps,
                    history=history, on_progress=_on_progress)
            return result
        except agent_mod.AgentError as exc:
            await _write("error", {"error": exc.message, "code": exc.code})
            return None
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"[:300]
            await _write("error", {"error": f"Agent 执行失败：{detail}",
                                   "code": "agent_internal_error"})
            return None

    task = asyncio.ensure_future(_runner())
    stream_started = time.time()
    #: 心跳间隔（秒）。只在**长时间没有新事件**时补一个注释行，
    #: 让代理/浏览器知道连接还活着。间隔太短会刷出大量 : keep-alive
    #: （实测 0.25s 一跳时 12 条进度事件配了 187 条心跳，噪音压过信号）。
    HEARTBEAT_SEC = 5.0
    last_beat = time.time()
    try:
        # 一边等结果，一边把进度事件刷出去
        while True:
            if task.done() and queue.empty():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                if task.done():
                    break
                now = time.time()
                if now - last_beat >= HEARTBEAT_SEC:
                    last_beat = now
                    await resp.write(b": keep-alive\n\n")
                continue
            event["elapsed_ms"] = int((time.time() - stream_started) * 1000)
            await _write("progress", event)

        result = task.result()
        if result is not None:
            if result.get("answer"):
                store.add_message(session_id, "assistant", result["answer"])
            for step in result.get("steps") or []:
                tool_name = step.get("tool")
                if not tool_name:
                    continue
                note = step.get("error") or "ok"
                store.add_message(session_id, "tool", f"{tool_name}: {note}",
                                   tool_name=tool_name)
            result["session_id"] = session_id
            result["provider_name"] = (result.get("provider") or {}).get("name") or ""
            await _write("result", result)
    finally:
        if not task.done():
            task.cancel()
        await resp.write_eof()
    return resp


async def api_agent_sessions(request):
    """GET /api/agent/sessions —— 会话清单（最近 50 条）。"""
    store = _agent_store()
    return json_ok({"sessions": store.list_sessions(limit=50)})


async def api_agent_session_get(request):
    """GET /api/agent/sessions/{id} —— 取某个会话的完整消息历史。"""
    sid = request.match_info["id"]
    store = _agent_store()
    if not store.exists(sid):
        return web.json_response({"error": "会话不存在", "code": "not_found"},
                                 status=404)
    return json_ok({"session_id": sid, "messages": store.raw_history(sid)})


async def api_agent_session_delete(request):
    """DELETE /api/agent/sessions/{id} —— 清空会话。"""
    sid = request.match_info["id"]
    store = _agent_store()
    deleted = store.delete(sid)
    return json_ok({"deleted": sid, "ok": deleted})


async def api_agent_tools(request):
    """GET /api/agent/tools —— 列出 Agent 的工具清单（调试 / 前端展示用）。"""
    specs = []
    for name, spec in agent_mod.TOOLS.items():
        specs.append({
            "name": name,
            "description": spec["description"],
            "parameters": spec["parameters"],
        })
    return json_ok({
        "tools": specs,
        "limits": {
            "max_steps": agent_mod.MAX_STEPS,
            "max_tool_calls": agent_mod.MAX_TOOL_CALLS,
            "total_timeout": agent_mod.TOTAL_TIMEOUT,
            "step_timeout": agent_mod.STEP_TIMEOUT,
            "max_concurrent": agent_mod.MAX_CONCURRENT,
        },
    })


# ------------------------------------------------------------------ static
MEDIA_TYPES = {".mp4": "video/mp4", ".webm": "video/webm", ".png": "image/png",
               ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
               # 音频（音乐生成产物）—— 补上后浏览器 <audio> 能正常播放
               ".flac": "audio/flac", ".mp3": "audio/mpeg", ".opus": "audio/opus",
               ".wav": "audio/wav", ".m4a": "audio/mp4", ".ogg": "audio/ogg",
               ".aac": "audio/aac"}


async def files_handler(request):
    path = request.match_info.get("path", "")
    full = (OUT_DIR / path).resolve()
    if not str(full).startswith(str(OUT_DIR.resolve())) or not full.is_file():
        raise web.HTTPNotFound()
    # 显式给 Content-Type —— 音频产物没有它的话浏览器会当成下载而不是播放
    mime = MEDIA_TYPES.get(full.suffix.lower())
    headers = {"Cache-Control": "no-cache"}
    if mime:
        headers["Content-Type"] = mime
    return web.FileResponse(full, headers=headers)


async def index_handler(request):
    # index.html 必须每次回源校验：它是前端资源的「版本清单」，
    # 一旦被浏览器缓存，app.js 的 ?v= 版本号就再也传不出去 ——
    # 用户会一直用着旧 app.js（曾经因此出现 esc() 未定义 -> 任务列表空白）。
    resp = web.FileResponse(BASE / "web" / "index.html")
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


# ------------------------------------------------------------------ app
def make_app():
    app = web.Application(client_max_size=200 * 1024 * 1024)
    app.router.add_post("/api/jobs", api_create)
    app.router.add_post("/api/longfilms", api_longfilm_create)
    app.router.add_get("/api/jobs", api_list)
    app.router.add_get("/api/jobs/{id}", api_get)
    app.router.add_post("/api/jobs/{id}/cancel", api_cancel)
    app.router.add_delete("/api/jobs/{id}", api_delete)
    app.router.add_get("/api/system", api_system)
    app.router.add_get("/api/status/stream", api_status_stream)
    app.router.add_get("/api/hardware", api_hardware)
    app.router.add_post("/api/gpu/power-limit", api_gpu_power_limit)
    app.router.add_post("/api/unload", api_unload)
    app.router.add_get("/api/image-options", api_image_options)
    app.router.add_get("/api/music-options", api_music_options)
    app.router.add_get("/api/generation-types", api_generation_types)
    app.router.add_get("/api/image-loras", api_image_loras)
    app.router.add_put("/api/image-loras", api_image_loras_save)
    # 本地模型 Provider 配置
    app.router.add_get("/api/providers", api_providers_list)
    app.router.add_post("/api/providers", api_providers_create)
    app.router.add_put("/api/providers/{id}", api_providers_update)
    app.router.add_delete("/api/providers/{id}", api_providers_delete)
    app.router.add_post("/api/providers/{id}/default", api_providers_default)
    app.router.add_post("/api/providers/{id}/test", api_providers_test)
    # 提示词增强迷你 Agent
    app.router.add_get("/api/enhancer", api_enhancer_config)
    app.router.add_put("/api/enhancer", api_enhancer_save)
    app.router.add_post("/api/enhance", api_enhance)
    # MOSS-Image 结构化（layout）增强 —— 外部复现 MossPrompt 节点的能力
    app.router.add_post("/api/moss-layout", api_moss_layout)
    # Qwen-Image 2.1 官方 PE 扩写（「增强」下拉里的 qwen21图片官方增强PE）
    app.router.add_post("/api/qwen-pe/expand", api_qwen_pe_expand)
    # OpenAI 兼容代理（给 ComfyUI 的 LLM 节点用，可关 thinking）
    app.router.add_get("/api/llm/p/{id}/v1/models", api_llm_proxy_models)
    app.router.add_post("/api/llm/p/{id}/v1/chat/completions", api_llm_proxy_chat)
    # 内置多模态 Agent
    app.router.add_post("/api/agent/chat", api_agent_chat)
    app.router.add_post("/api/agent/chat/stream", api_agent_chat_stream)
    app.router.add_get("/api/agent/tools", api_agent_tools)
    app.router.add_get("/api/agent/sessions", api_agent_sessions)
    app.router.add_get("/api/agent/sessions/{id}", api_agent_session_get)
    app.router.add_delete("/api/agent/sessions/{id}", api_agent_session_delete)
    # 资产素材库（/api/assets/*）
    asset_api.register(app, server_module=sys.modules[__name__])
    # Qwen 图片编辑的参考图（/api/refs/*）—— 素材库来源约束
    ref_api.register(app)
    app.router.add_get("/files/{path:.+}", files_handler)
    app.router.add_static("/static", BASE / "web")
    app.router.add_get("/", index_handler)
    return app


async def main():
    init_db()
    # 资产素材库自检：磁盘是否挂载可用、目录是否可写。
    # 不可用时只打印明确错误，**不破坏既有数据**（不删目录、不清库）。
    lib_report = asset_store.init_db()
    if lib_report.get("ok"):
        free_gb = lib_report.get("free_bytes", 0) / (1024 ** 3)
        print(f"[素材库] 已就绪: {lib_report['root']}（可用 {free_gb:.1f} GB）")
    else:
        print("[素材库] 不可用，素材库相关接口将返回 503：")
        for item in lib_report.get("errors", []):
            print(f"  - {item}")
    app = make_app()
    await WORKER.start()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, CFG["host"], CFG["port"])
    await site.start()
    print(f"""
==============================================
  ComfyUI-MiniMax-H3 Studio 已启动
  本机访问:   http://127.0.0.1:{CFG['port']}
  局域网访问: http://<本机IP>:{CFG['port']}
  ComfyUI:    {COMFY}
  数据目录:   {DATA}
==============================================""")
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("已退出")
