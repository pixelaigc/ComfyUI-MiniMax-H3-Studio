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
    LORAS,
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
from h3studio import prompt_history as ph
from h3studio import prompt_history_hooks as ph_hooks
from h3studio import asset_api
from h3studio import video_models
from h3studio import video_workflow
from h3studio import job_meta
from h3studio import continuation as cont
from h3studio import continuation_service as cont_service
# ComfyUI 执行错误的提取与中文化（2026-11-12 修：原来把
# status.messages 的条目当「m[0] 是字典」读，导致真正的原因被
# AttributeError 吞掉，用户只看到原始 Python 异常文本）。
from h3studio import comfy_errors
# 【2026-11-13 合并说明】model_recommendations（h3-model-meta 分支）：
# 主模型 / 加速 LoRA 的作者推荐参数元数据（步数/调度器/采样器/音视频 shift）。
# 与上面的 continuation / comfy_errors 是**互不相关的独立模块**，
# 合并时两边都保留 —— 删掉任何一边都会让对应功能失效。
from h3studio import model_recommendations as model_recs
# 合并说明：video_categories（video-tabs 分支）、comfy_models
# （model-list-fix 分支）、reso_label（reso-label-fix 分支）是三个
# 独立模块，三条 import 都保留。
from h3studio import video_categories
from h3studio import comfy_models
from h3studio import reso_label
from h3studio import ref_api
from h3studio import ref_images as ref_imgs
from h3studio import music_yue2
from h3studio import music_style
from h3studio import yue2_schema
from h3studio import agent as agent_mod
from h3studio import agent_memory
from h3studio import agent_sessions
from h3studio import agent_media


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
    """读取配置。

    优先级：环境变量 > config.json > 默认值。

    ⚠️ 2026-11-10 加环境变量覆盖：独立测试实例需要**独立端口 + 独立
    data_dir**，但 config.json 是被 git 跟踪的（不能就地改）。
    agent 分支的 run_live_8199.sh 已经在用 H3STUDIO_DATA_DIR，
    这里把端口也补上（之前端口只能从 config.json 读，
    独立实例根本起不来 —— 实测报 "address already in use"）。
    """
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"[config] 读取失败，用默认配置: {e}")
    # 环境变量覆盖（独立实例用）
    port_env = os.environ.get("H3STUDIO_PORT")
    if port_env:
        try:
            cfg["port"] = int(port_env)
        except ValueError:
            print(f"[config] H3STUDIO_PORT 不是数字，忽略: {port_env!r}")
    data_env = os.environ.get("H3STUDIO_DATA_DIR")
    if data_env:
        cfg["data_dir"] = data_env
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
        # 提示词历史表（各功能面板的提示词留痕）
        ph.ensure_table(conn)
        # 会话 -> 任务的关联列（见 _ensure_jobs_session_column 的说明）
        _ensure_jobs_session_column(conn)
        # 迁移可能刚补上这一列 -> 让列名判断重新算一次
        _JOBS_HAS_SESSION_COL.pop(str(DB_PATH), None)


def _ensure_jobs_session_column(conn) -> None:
    """
    给 jobs 表补 session_id 列（增量迁移，老库自动升级）。

    【为什么需要这一列 —— 2026-11-11 会话管理需求】
    「本次会话产物」面板要列出**这个会话里** Agent 建的所有任务。
    但 jobs 表原先**没有**任何会话维度的信息 —— 只有 id/mode/params/status。
    于是有两种坏做法：
      1. 按时间窗猜（「会话活跃期间创建的任务」）-> 会话之间会互相串味，
         而且用户隔一天回来点开会话，看到的是别人的任务；
      2. 从 agent_messages 的文本里正则抠 job id -> 那是**模型写的**文本，
         模型可能编 id（这正是本项目修过的 bug），不可信。

    唯一可靠的做法是**建任务时就把会话 id 记进去**。
    老任务没有这个信息（session_id 为 NULL），它们本来就不属于任何会话，
    不影响新功能。

    【为什么用 PRAGMA 查列名而不是写死迁移版本】
    幂等：已经有列就跳过。老库执行 ALTER TABLE 后立刻可查。
    """
    rows = conn.execute("PRAGMA table_info(jobs)").fetchall()
    columns = {str(r[1]) for r in rows}
    if "session_id" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN session_id TEXT DEFAULT ''")
    # 索引：按会话查任务时会用到。没有索引的话会话多了会全表扫。
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_session ON jobs(session_id)")


def _first_media_output(outputs: list[dict]) -> dict | None:
    """取第一个图像/视频产物（音频没有尺寸，不参与）。"""
    for item in outputs or []:
        if not isinstance(item, dict):
            continue
        if item.get("kind") in ("image", "video"):
            return item
    return None


def _job_resolution_info(job_params: dict, outputs: list[dict]) -> dict:
    """
    生成库卡片「分辨率 / 画面比例」这一行的**唯一计算入口**（服务端算好下发）。

    【为什么放服务端】前端曾经直接读 `params["resolution"]`（一个档位字符串），
    于是文生视频实际跑 1344×768 时，卡片显示的是遗留默认值 "544p" ——
    544p 根本不是 1344×768 的任何合法档位。

    现在统一按 reso_label.describe() 的规则算：
      * 有产物实际像素 -> 以实际为准（档位 + 比例都由实际像素反算）；
      * 没有产物 -> 退回请求的 width/height；
      * 都没有 -> 只把请求的档位/比例原样带出，不编造尺寸。
    """
    actual = _first_media_output(outputs)
    actual_size = None
    if actual and actual.get("width") and actual.get("height"):
        actual_size = {"width": actual["width"], "height": actual["height"]}
    return reso_label.describe(job_params, actual_size)


def row_to_job(r):
    mode = r["mode"]
    job_params = json.loads(r["params"] or "{}")
    # 生成类型：优先读任务 meta 里记录的 gen_type；
    # 老任务没有该字段时，按 mode 一致兜底归类（不出现空白 / 全归未知）
    stored_type = str(job_params.get("gen_type") or "").strip()
    gen_type = (gen_types.type_for_mode(stored_type) if stored_type
                else gen_types.type_for_mode(mode))
    outputs = json.loads(r["outputs"] or "[]")
    model_info = job_meta.summarize(mode, job_params)
    resolution_info = _job_resolution_info(job_params, outputs)
    return {
        "id": r["id"],
        "created_at": r["created_at"],
        "mode": mode,
        "gen_type": gen_type["key"],
        "gen_type_label": gen_type["label"],
        "gen_type_media": gen_type["media"],
        "params": job_params,
        # 【2026-11-12 修】模型 / LoRA 的**权威摘要**（服务端算好的唯一真源）。
        #
        # 【为什么必须有它】用户实测报过：面板选了「混合版 + DMAD LoRA」，
        # 卡片却恒显「H3 + ckpt850 · 4步」、LoRA 显示「空」——
        # 因为前端 modelLabel() 读的是**遗留字段** p.model / p.lora，
        # 而新面板提交的是 model_key / accel_loras。
        # 现在由服务端按任务真实参数算好摘要下发，前端只渲染这一份，
        # 「面板所选 == 落库记录 == 卡片显示」三者必然同源。
        "model_info": model_info,
        # 【2026-11-13 新增】完整生成参数摘要（C2 卡片行 + C3 详情模态框）。
        # 服务端算好下发，前端只渲染 —— 与 model_info 同一纪律。
        #
        # 【为什么要单独一份】BUG 2 报的是「卡片不显示步数等参数」。
        # 步数 / 调度器 / 采样器 / shift / 尺寸 / 时长 / seed 分散在
        # 不同字段里（视频用 shift_video、图像用 shift；音乐时长是
        # max_duration），前端自己去拼必然漏、也必然串。
        # **缺失的字段这里是空串 —— 前端显示「—」，不拿别的字段顶替。**
        "gen_params": job_meta.gen_params(mode, job_params,
                                          model_info, resolution_info),
        # 【2026-11-12 新增】接续状态（能否被接续 / 为什么不能 / 累计帧数）。
        # 服务端算好下发，前端只渲染 —— 与 model_info 同一纪律。
        "continuation_info": cont_service.continuation_state(mode, job_params, r["id"]),
        "status": r["status"],
        "progress": r["progress"],
        "progress_text": r["progress_text"],
        "error": r["error"],
        "outputs": outputs,
        # 【2026-11-11】卡片显示用的分辨率 / 比例标签（服务端算好的唯一真源）
        "resolution_info": resolution_info,
        "elapsed": (r["finished_at"] or time.time()) - r["started_at"] if r["started_at"] else None,
        # 任务归属的 Agent 会话（面板侧建的任务为空）。
        # row["session_id"] 在**迁移前**的老库上不存在 —— 用 keys 兜底，
        # 否则老库一读就 KeyError（init_db 的迁移在进程启动时跑，
        # 但测试里经常直接构造旧结构的库）。
        "session_id": (r["session_id"] if "session_id" in r.keys() else "") or "",
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

# ------------------------------------------------------------------ 增强并发闸门
#
# 【2026-11-09 审查返修】/api/enhance 之前**没有任何并发限制**。
# 单次请求最坏挂起 600 秒（llm.MAX_TIMEOUT），N 个并发 = N 个长挂起协程，
# 会把 aiohttp 连接池占满，并让后续请求一起排队。
#
# 为什么用「抢不到就 429」而不是「排队等」：
#   本机 llama.cpp server 的 total_slots = 1（实测），
#   多个请求并发进去也只是在模型侧串行排队 —— 对用户没有更快，
#   只会让所有请求一起等到超时。直接 429 让用户立刻知道「现在忙」，
#   比静默排队 10 分钟然后超时要好。
#
# 为什么用 locked() 而不是 wait_for(acquire(), timeout=0)：
#   timeout=0 会在 acquire 真正执行前就取消它，结果是**所有**请求都 429
#   （builtin-agent 分支实测踩过这个坑）。locked() 判据实测 peak 并发
#   恰好等于上限，无超发。asyncio.Semaphore.acquire 内部是「先减计数再 await」，
#   本身是原子的。
#
# 【2026-11-10 取值裁决】**本文件是唯一权威定义处**。
#   enh-unify 分支曾取 2、max-token 分支曾取 3，两份定义同时存在于
#   server.py（后定义覆盖先定义）—— 上限实际值取决于代码顺序，注释也自相矛盾。
#   现统一取 **3**，理由：
#     1. 本机 llama.cpp server total_slots = 1（实测），并发请求在模型侧
#        本就串行；闸门的意义是「不要把 aiohttp 连接池占满 + 给出即时反馈」，
#        不是「让模型并行跑」。
#     2. 取 3 时，最坏挂起时长 = 3 × llm.MAX_TIMEOUT(600s) = 30 分钟；
#        取 2 时是 20 分钟。两者都不构成「一个请求拖住服务 30 分钟」的问题，
#        因为闸门本身已经挡住了 N+1 个请求。
#     3. 取 3 对**多选增强**更友好：一次多选要连续跑 2~3 项增强
#        （普通增强 + 二次增强 + 官方 PE），上限 2 会让同一次操作里的
#        后续项立刻 429，用户看到「刚点了就报忙」。上限 3 允许同一轮
#        多选的各项顺序执行而不互相挤掉。
#   若将来实测 llama.cpp 改为多槽（total_slots > 1），应重新评估此值。
ENHANCE_CONCURRENCY = 3
ENHANCE_GATE: asyncio.Semaphore | None = None


def _enhance_gate() -> asyncio.Semaphore:
    """并发闸门（惰性创建，避免 import 期就占住事件循环）。"""
    global ENHANCE_GATE
    if ENHANCE_GATE is None:
        ENHANCE_GATE = asyncio.Semaphore(ENHANCE_CONCURRENCY)
    return ENHANCE_GATE


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


def _comfy_error_message(status_info: dict) -> str:
    """把 ComfyUI 的执行错误变成**给用户看的中文原因**。

    【为什么需要这个包装 —— 2026-11-12 独立复验发现的缺陷】
    原实现把 `status.messages` 的条目当「`m[0]` 是字典」处理：

        (m or [{}])[0].get("x", "")

    但 ComfyUI 的真实形状是 **`[事件名, 数据字典]`** —— `m[0]` 是字符串
    `"execution_error"`。于是 `.get()` 作用在 str 上抛 AttributeError，
    真正的原因（例：拷贝桥禁止跨分辨率）**被彻底丢掉**，用户只看到
    一句 `'str' object has no attribute 'get'`。

    提取与中文化逻辑放在 h3studio/comfy_errors.py（纯函数，可离线测）。
    本函数只负责：**取中文原因 + 把技术细节打进服务端日志**。
    """
    described = comfy_errors.extract_error(status_info)
    if not described.get("found"):
        return "ComfyUI 执行出错（未取到具体原因，详细信息见服务端日志）。"
    # 技术细节只进日志，不进用户可见文案。
    print(f"[worker] ComfyUI 错误详情: {described.get('detail', '')}")
    return str(described.get("reason") or "ComfyUI 执行出错。")


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


# ------------------------------------------------------------------
# 产物实际尺寸探测
# ------------------------------------------------------------------
# 【为什么必须探测真实产物，而不是只信请求参数】
# 请求尺寸 ≠ 实际输出尺寸是**常态**，不是异常：
#   * 视频：H3 原生画布上限 768×1344，超出的会被按比例缩；
#   * 图像：Z-Image ≥3MP 自动走「低分辨率生成 + RealESRGAN 放大 + 低 denoise
#     二次细化」的官方 2K 放大链，最终像素由 ImageScale 节点决定；
#   * 图片编辑（i2i_qwen）：实测请求 1360×768 出了 2048×2048 / 2720×1536。
# 生成库卡片要显示「实际输出尺寸」，就只能去读产物本身。
def _probe_image_size(path: Path) -> tuple[int, int] | None:
    """读图片头拿宽高（只读文件头，不解码整图、不转码）。"""
    try:
        from PIL import Image
    except ImportError:                     # pragma: no cover - 环境缺 PIL
        return None
    try:
        with Image.open(path) as img:
            width, height = img.size
        if width > 0 and height > 0:
            return int(width), int(height)
    except Exception:
        return None
    return None


def _probe_media_video_size(path: Path) -> tuple[int, int] | None:
    """
    读视频流宽高（ffprobe 只读容器头，不解码）。

    【与长片链路的区别】长片那边有个同名的 `_probe_video_size`，
    那个在 ffprobe 缺失时会**返回猜测值 864×480**（长片拼接必须有尺寸才能跑）。
    本函数用于给卡片显示真实尺寸，**探测不到就返回 None** ——
    宁可少显示一栏，也不能显示一个与产物对不上的数字。
    """
    if not FFPROBE:
        return None
    cmd = [FFPROBE, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)]
    try:
        raw = subprocess.check_output(cmd, encoding="utf-8", timeout=30,
                                      creationflags=CREATE_NO_WINDOW,
                                      stderr=subprocess.STDOUT).strip()
    except Exception:
        return None
    parts = [p for p in raw.replace("\n", ",").split(",") if p.strip().isdigit()]
    if len(parts) < 2:
        return None
    width, height = int(parts[0]), int(parts[1])
    return (width, height) if width > 0 and height > 0 else None


def probe_output_size(kind: str, rel_path: str) -> dict | None:
    """
    探测一个产物的实际像素尺寸。

    返回 {"width": w, "height": h} 或 None（探测不了就返回 None，
    **绝不返回猜测值** —— 卡片宁可少显示，也不能显示错的）。
    """
    try:
        path = OUT_DIR / rel_path
    except Exception:
        return None
    if not path.is_file():
        return None
    size = None
    if kind == "image":
        size = _probe_image_size(path)
    elif kind == "video":
        size = _probe_media_video_size(path)
    if not size:
        # 后缀兜底：有些 ComfyUI 节点把视频挂在 images 键下（见 _extract_outputs）
        suffix = path.suffix.lower()
        if suffix in (".png", ".jpg", ".jpeg", ".webp"):
            size = _probe_image_size(path)
        elif suffix in (".mp4", ".webm", ".mkv", ".mov"):
            size = _probe_media_video_size(path)
    if not size:
        return None
    return {"width": size[0], "height": size[1]}


def annotate_outputs_with_size(saved: list[dict]) -> list[dict]:
    """
    给一批已落盘的产物探测实际尺寸。

    产物条目形如 {"kind": ..., "file": ..., "name": ...}，
    这里补上 "width" / "height"（探测不到就不加这个键）。
    """
    for item in saved:
        kind = item.get("kind")
        if kind not in ("image", "video"):
            continue
        size = probe_output_size(kind, item.get("file") or "")
        if size:
            item["width"] = size["width"]
            item["height"] = size["height"]
    return saved


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
                    # 【2026-11-12 修】原来直接把 ComfyUI 的原始
                    # exception_message 存进 job.error -> 前端展示的是
                    # 英文/技术文本。现在统一走 comfy_errors：
                    # 用户看到中文原因，技术细节进服务端日志。
                    described = comfy_errors.describe_exception(
                        d.get("node_type") or "",
                        d.get("exception_type") or "",
                        d.get("exception_message") or "")
                    print(f"[ws] ComfyUI 错误详情: {described['detail']}")
                    err = str(described.get("reason") or "执行出错")[:400]
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

                # 【2026-11-12 修】视频任务的节点可用性必须在这里**重新确认**。
                #
                # 归一层（normalize_t2v_params / prepare_job_params）算出的
                # use_sigma_shift 依赖 _COMFY_NODE_NAMES，而那个集合只有
                # /api/video-options 被调用过才有值。
                # 真实场景：内置 Agent 建的任务**不经过面板**
                #            -> 集合是空的
                #            -> use_sigma_shift=False
                #            -> MiniMaxH3SigmaShift 节点不进工作流
                #            -> 用户在面板上拖的 shift 滑杆对这个任务完全无效
                #               （且没有任何报错 —— 静默失效）。
                #
                # 修法：worker 在拼工作流前强制刷新一次节点清单，
                #       并把结果回写进 params —— 工作流拼装拿到的是真实可用性。
                if mode in ("t2v", "flf", "r2v"):
                    await refresh_comfy_node_names(session, force=True)
                    params["use_sigma_shift"] = bool(
                        params.get("use_sigma_shift", True)
                        and "MiniMaxH3SigmaShift" in _COMFY_NODE_NAMES)

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
                                # 【2026-11-11】记录产物的**实际像素尺寸**。
                                # 卡片要显示真实输出，而不是请求值（请求值可能
                                # 被画布上限 / 放大链 / 模型限制改写过）。
                                annotate_outputs_with_size(saved)
                                # 【2026-11-12 新增】接续：把本段登记进链，
                                # 并拼接出**完整视频**（上一段 + 本段）。
                                # 失败**不影响**本段本身的产物 —— 拼接是附加产物。
                                try:
                                    saved = await self._finalize_continuation(
                                        job_id, mode, params, saved)
                                except Exception as ce:
                                    print(f"[worker] 接续拼接失败（不影响本段产物）: {ce}")
                                with db() as conn:
                                    conn.execute(
                                        "UPDATE jobs SET status='done', progress=1, outputs=?, finished_at=? WHERE id=? AND status='running'",
                                        (json.dumps(saved, ensure_ascii=False), time.time(), job_id))
                                print(f"[worker] 任务 {job_id} 完成: {len(saved)} 个文件")
                                return
                            if status_info.get("status_str") == "error":
                                raise RuntimeError(_comfy_error_message(status_info))
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


    async def _finalize_continuation(self, job_id: str, mode: str, params: dict,
                                     saved: list[dict]) -> list[dict]:
        """接续收尾：登记本段 + 拼接**完整视频**。

        用户需求第 6 条：「最终媒体库中得到的是**完整视频**
        （上一段 + 本段完整长度）」。所以每接续一段，都要产出
        一个包含**整条链**的完整视频。

        返回加了「完整视频」条目的产物列表。

        【为什么拼接失败不影响本段产物】本段视频本身是好的，
        拼接只是附加产物。拼接失败（比如 ffmpeg 不可用）时，
        本段仍然要出现在生成库里 —— 只是没有完整视频。
        """
        if mode != "t2v":
            return saved
        info = cont_service.chain_info(params)
        if not info:
            return saved

        video = next((o for o in saved if o.get("kind") == "video"), None)
        if not video:
            return saved

        chain_id = str(info.get("chain_id") or "")
        try:
            stage = int(info.get("stage_index", 0))
        except (TypeError, ValueError):
            stage = 0
        if not chain_id:
            return saved

        # 本段的**分段**视频（未拼接）相对 OUT_DIR 的路径
        segment_rel = str(video.get("file") or "")

        # 【本段潜空间被重写后，清掉同坐标的旧指纹副本】
        # 本段刚跑完 -> `H3RelayLatentSave` 重新写了 `stage_00000.safetensors`
        # -> 内容变了 -> 旧指纹副本（内容指纹已过期）不再对应任何有效内容。
        # 不清会随每次重跑堆积一份 4.8MB 的死副本。
        try:
            own_run_id, own_stage = cont.latent_coords(info)
            if own_run_id:
                removed = cont_service.cleanup_fingerprint_copies(own_run_id,
                                                                  own_stage)
                if removed:
                    print(f"[接续] 清理本段旧指纹副本 {removed} 个 "
                          f"({own_run_id}/stage_{own_stage:05d})")
        except Exception as ce:
            print(f"[worker] 清理指纹副本失败（不影响本段产物）: {ce}")

        # 把本段记到**自己**的任务记录里（不再往链首段的共享账本里塞 ——
        # 共享账本会让兄弟分支互相污染，见 _record_segment_rel 的说明）。
        await asyncio.to_thread(_record_segment_rel, job_id, segment_rel)

        # 拼完整视频用**血缘**（沿 source_job 回溯）：本段 -> 上游 -> ...
        # 兄弟分支不会混进来。
        segments = await asyncio.to_thread(_lineage_segment_rels, job_id)
        if segment_rel and segment_rel not in segments:
            segments.append(segment_rel)

        # 只有**续接段**才需要拼完整视频（第 0 段本身就是完整视频）
        if stage <= 0:
            return saved

        full = await asyncio.to_thread(_concat_chain_video, segments, chain_id)
        if not full:
            return saved
        full_rel = full.relative_to(OUT_DIR).as_posix()
        annotate_outputs_with_size([{"kind": "video", "file": full_rel,
                                     "name": full.name}])
        # 完整视频放在产物列表**前面** —— 生成库卡片取第一个视频显示，
        # 用户看到的第一眼就该是完整视频。
        out = [{"kind": "video", "file": full_rel, "name": full.name,
                "is_full": True}]
        out.extend(saved)
        print(f"[worker] 接续完整视频: {full_rel} ({len(segments)} 段拼接)")
        return out

    def _has_batches(self, job_id: str) -> bool:
        with db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) c FROM job_batches WHERE job_id=?", (job_id,)).fetchone()
        return bool(row and row["c"] > 1)

    async def _run_batches(self, job_row):
        """逐批次执行：每个批次有独立的种子、状态、进度与产物。

        任何一个批次失败只标记该批次 failed，其余批次继续跑；
        任务整体状态 = done（至少一个成功）/ failed（全部失败）。

        ⚠️ 2026-11-10 修复：**mode 必须从 job_row 的 DB 列取**。
            旧代码让 `_run_one_batch` 用 `params.get("mode") or "t2i"`，
            而 params（DB 的 params JSON）里**从来没有 mode 键** ——
            `params["mode"] = mode` 只在**非批次**分支里设置（server.py:829）。
            于是 MOSS 批量任务（mode=t2i_moss）走到批次分支时 mode 变成 "t2i"，
            build_workflow 走的是 **Z-Image t2i** 工作流：
            用户设的 steps=100 / cfg=4.3 / shift=5 / sgm_uniform 全被忽略，
            实际跑的是 Z-Image 的 steps=8 + 放大分支的 KSampler(steps=5)
            —— 界面显示「MOSS-Image 采样步数 100」，日志却只跑 5 步。
            现在把 mode 显式传下去，批次与单张走**同一条**工作流构建路径。
        """
        job_id = job_row["id"]
        params = json.loads(job_row["params"])
        mode = job_row["mode"]
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
                outputs = await self._run_one_batch(job_id, batch_id, mode, params, row["seed"])
                done_count += 1
                # 【2026-11-11】批次产物同样记录实际像素尺寸（卡片「批次结果」
                # 的缩略图网格与标题行摘要都会用到）。
                annotate_outputs_with_size(outputs)
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

    async def _run_one_batch(self, job_id: str, batch_id: str, mode: str,
                             params: dict, seed):
        """跑单个批次并返回产物列表（文件已落盘）。

        mode 由调用方从 jobs 表的 mode 列传入（**不能**从 params 里取 ——
        params 里没有 mode 键，见 _run_batches 的说明）。
        批次与单张走**完全相同**的 build_workflow(mode, params) 路径，
        所以 steps / cfg / shift / scheduler / seed 的语义一致，
        只有 seed 按批次递推（每张一个变体）。
        """
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
                            raise RuntimeError(_comfy_error_message(status_info))
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


def normalize_t2v_params(params: dict) -> tuple[dict, str]:
    """文生视频面板参数的**服务端归一**（唯一入口）。

    返回 (归一后的 params, 错误信息)。错误信息为空表示通过。

    归一的每一项都对应一条真实的本机约束：
      * 宽高 -> 32 的倍数（H3 节点 schema step=32 / CANVAS_MULTIPLE=32）
      * 模型 -> 必须是 video_models.VIDEO_MODELS 里登记的 key，且文件在本机
      * 加速 LoRA -> 逐个查文件是否存在 + 与底模的层结构是否对得上
      * 注意力 -> 必须是本机 ComfyUI 里真有的节点
      * 二采 -> SelfLift 的硬约束（Euler / transition_step 范围 / rho 与 upscaler 组合）
    """
    out = dict(params)

    # ---- 宽高 ----
    # 【2026-11-11】strict_over_limit=True —— 超限时**不静默截断**，
    # 直接返回可执行的错误提示（说明 2.0M 上限与当前请求值）。
    width, height, reasons = video_models.normalize_video_size(
        params.get("width"), params.get("height"),
        max_pixels=video_models.max_pixels_for_mode("t2v"),
        strict_over_limit=True)
    if width <= 0 or height <= 0:
        return out, "宽高无效：" + ("；".join(reasons) or "请输入数字")
    out["width"] = width
    out["height"] = height
    if reasons:
        out["size_adjust_reasons"] = reasons
    # 【2026-11-11】把**实际生效**的档位标签写回 params.resolution。
    #
    # 【为什么】文生视频新面板只提交 width/height，不写 resolution。
    # 而任务记录里的 params.resolution 会保留前端全局 state 的**遗留默认值**
    # "544p" —— 于是任务卡片显示「544p · 16:9」，实际产物却是 1344×768。
    # 544p 不是 1344×768 的任何合法档位，这就是用户报出的那个现象。
    #
    # 现在写回真实档位（由实际像素反算的短边标签），
    # 让 params 本身也不再说谎。卡片显示仍以「产物实际像素」为准
    # （见 reso_label.describe），这一行只是让落库的参数自洽。
    out["resolution"] = reso_label.tier_for_pixels(width, height)

    # ---- 时长（2026-11-12 放宽到 60 秒）----
    #
    # 【旧行为】`max(2.0, min(15.0, duration))` —— 硬卡 15 秒，
    # 用户实测反馈「15 秒太短，一分钟都行，最多 OOM」。
    # 【新行为】
    #   * 上限放宽到 video_models.VIDEO_DURATION_MAX（60 秒 =
    #     1440 帧；ComfyUI 节点 schema 的 length 上限是 3600 帧，
    #     15 秒是我们自己的限制，不是模型能力上限）；
    #   * **超上限不静默截断** —— 直接返回错误信息（含上限值与
    #     当前请求值），提交被拒。静默截断会让用户以为生成了
    #     60 秒、实际只拿到 15 秒，那是撒谎；
    #   * 非法值（不是数字）-> 回落到默认 5 秒，不报错（老客户端兼容）。
    duration, duration_error = video_models.normalize_video_duration(
        params.get("duration"))
    if duration_error:
        return out, duration_error
    out["duration"] = duration

    # ---- 底模 ----
    model_key = params.get("model_key") or video_models.VIDEO_DEFAULT_MODEL
    model = video_models.VIDEO_MODELS.get(model_key)
    if model is None:
        return out, f"未知的视频模型 {model_key}"
    # 【2026-11-11】可用性判定与面板**同源** —— 都以 ComfyUI 的模型列表
    # API 为准（面板显示什么，这里就按什么判）。
    # 原来这里只扫盘（model_available），面板与提交走两条路，
    # 就会出现「下拉显示 A、提交被拒」的不一致。
    model_status = video_models.model_status(model_key)
    if model_status != "available":
        if model_status == "external_missing":
            return out, (f"模型「{model['label']}」的权重位于外置盘 "
                         f"{model.get('location') or ''}，但 ComfyUI 的模型"
                         "列表里没有它（外置盘未挂载或未扫描到）—— 当前不可选。")
        if model_status == "missing":
            return out, (f"本机找不到模型权重 {model['unet']}"
                         f"（{model.get('location') or '未知目录'}）。")
        # unknown：ComfyUI 连不上，无法确认 -> 放行（让 ComfyUI 自己判）
        # 但要把原因带出去，避免「静默失败」。
        out["model_status_note"] = video_models.model_status_note(model_key)
    out["model_key"] = model_key

    # ---- 固定加速 LoRA ----
    fixed_key = params.get("fixed_accel_lora") or video_models.VIDEO_FIXED_ACCEL_LORA_DEFAULT
    fixed = video_models.VIDEO_ACCEL_LORAS.get(fixed_key)
    if fixed is None:
        return out, f"未知的固定加速 LoRA {fixed_key}"
    if not video_models.lora_available(fixed["name"]):
        return out, f"本机找不到固定加速 LoRA {fixed['name']}"
    out["fixed_accel_lora"] = fixed_key

    # ---- 可勾选的加速 LoRA 组合（**单选**：一个主模型只能挑一个）----
    #
    # 【2026-11-11 改】原来是「可多选叠加」。现在前端是下拉单选，
    # 服务端这里也必须收紧 —— 否则绕过前端直接调 API 仍能塞多个，
    # 两枚 4 步蒸馏 LoRA 叠在一起没有意义（都是把 20 步压到 4 步的
    # 蒸馏权重，叠加只会互相打架）。
    #
    # 兼容旧客户端：老任务里存的是 accel_loras 数组（可能多项）。
    #   * 全部不兼容 / 未启用 -> 空
    #   * 有多项 -> 取**第一个可用且兼容的**（保持老任务能重放）
    combos = []
    for item in params.get("accel_loras") or []:
        if not isinstance(item, dict):
            continue
        if item.get("enabled") is False:
            continue
        key = item.get("key") or ""
        meta = video_models.VIDEO_ACCEL_LORA_COMBOS.get(key)
        if meta is None:
            return out, f"未知的加速 LoRA 组合 {key}"
        if not video_models.lora_available(meta["name"]):
            return out, f"本机找不到加速 LoRA {meta['name']}"
        check = video_models.check_h3_lora_compat(meta["name"], model["unet"])
        if not check["compatible"]:
            return out, (f"「{meta['display']}」与当前底模不兼容："
                         f"{check['reason']}")
        try:
            strength = float(item.get("strength", meta.get("recommended_strength", 1.0)))
        except (TypeError, ValueError):
            strength = float(meta.get("recommended_strength", 1.0))
        if strength <= 0:
            continue
        combos.append({"key": key, "strength": strength, "enabled": True})
        # 单选：拿到第一个就停 —— 后面的忽略
        break
    out["accel_loras"] = combos

    # ---- 注意力机制 ----
    attention = params.get("attention") or video_models.VIDEO_DEFAULT_ATTENTION
    mode_meta = video_models.VIDEO_ATTENTION_MODES.get(attention)
    if mode_meta is None:
        # 【2026-11-12】已下线的机制（sol_scheduled —— 用户实测画面有叠影）
        # 要给**明确**的原因，而不是笼统的「未知注意力机制」——
        # 否则用户以为是面板 bug，不知道这个选项为什么消失了。
        removed = video_models.VIDEO_REMOVED_ATTENTION_MODES.get(attention)
        if removed:
            return out, (f"注意力机制「{removed['label']}」已下线："
                         f"{removed['reason']}"
                         f"请改选其它注意力机制（当前默认 "
                         f"{video_models.VIDEO_DEFAULT_ATTENTION}）。")
        return out, f"未知的注意力机制 {attention}"
    node = mode_meta.get("node")
    if node and node not in _COMFY_NODE_NAMES:
        return out, (f"本机 ComfyUI 里没有节点 {node}"
                     f"（{mode_meta['label']}）—— 无法使用该注意力机制。")
    out["attention"] = attention

    # ---- 二采 ----
    twopass = params.get("twopass") or {}
    if not isinstance(twopass, dict):
        twopass = {}
    enabled = bool(twopass.get("enabled"))
    if enabled:
        if "SelfLiftH3Sampler" not in _COMFY_NODE_NAMES:
            return out, ("本机 ComfyUI 里没有 SelfLiftH3Sampler 节点 —— 无法开启二采。")
        try:
            steps = int(params.get("steps") or 4)
        except (TypeError, ValueError):
            steps = 4
        # ---- 【2026-11-13】sigma 精修参数（借鉴 nonebot H3 工作流）----
        # 先算精修参数，**再**夹 transition_step ——
        # 因为 transition_step 是 sigma 表里的**索引**，
        # 精修后表长变了，索引上限也跟着变。
        #
        # 节点不在本机 -> 强制关闭（避免面板给出「能开但跑不了」的假选项）。
        refine = twopass.get("sigma_refine") or {}
        refine_enabled = bool(refine.get("enabled"))
        if refine_enabled and "H3SigmaRefiner" not in _COMFY_NODE_NAMES:
            refine_enabled = False
        extra_steps = 0
        start_at_sigma = video_models.VIDEO_TWOPASS[
            "sigma_refine_start_at_sigma_default"]
        end_at_sigma = video_models.VIDEO_TWOPASS[
            "sigma_refine_end_at_sigma_default"]
        spacing = video_models.VIDEO_TWOPASS["sigma_refine_spacing_default"]
        if refine_enabled:
            try:
                extra_steps = int(refine.get(
                    "extra_steps",
                    video_models.VIDEO_TWOPASS["sigma_refine_extra_steps_default"]))
            except (TypeError, ValueError):
                extra_steps = video_models.VIDEO_TWOPASS[
                    "sigma_refine_extra_steps_default"]
            extra_steps = max(0, min(15, extra_steps))
            if extra_steps <= 0:
                refine_enabled = False
        if refine_enabled:
            try:
                start_at_sigma = float(refine.get(
                    "start_at_sigma", start_at_sigma))
            except (TypeError, ValueError):
                pass
            try:
                end_at_sigma = float(refine.get(
                    "end_at_sigma", end_at_sigma))
            except (TypeError, ValueError):
                pass
            spacing = str(refine.get("spacing", spacing))
            if spacing not in ("cosine", "linear", "exponential"):
                spacing = video_models.VIDEO_TWOPASS[
                    "sigma_refine_spacing_default"]
        # transition_step 上限 = 精修后 sigma 表长度 - 2
        # （SelfLift 把表切成 sigmas[:ts+1] 与 sigmas[ts:]，
        #  硬约束是 ts <= len(sigmas) - 2）。
        refined_len = steps + 1 + (extra_steps if refine_enabled else 0)
        max_transition = max(1, refined_len - 2)
        transition = int(twopass.get("transition_step")
                         or video_models.VIDEO_TWOPASS["transition_step_default"])
        if transition < 1 or transition > max_transition:
            transition = max(1, min(transition, max_transition))
        lowres = float(twopass.get("lowres_scale")
                       or video_models.VIDEO_TWOPASS["lowres_scale_default"])
        lowres = max(0.25, min(1.0, lowres))
        rho = float(twopass.get("rho", video_models.VIDEO_TWOPASS["rho_default"]))
        upscaler = twopass.get("upscaler_model") \
            or video_models.VIDEO_TWOPASS["upscaler_default"]
        if rho <= 0 and upscaler == "none":
            # SelfLift 节点会直接 raise：两条路都关掉了
            rho = 0.6
        w_min = float(twopass.get("w_min", video_models.VIDEO_TWOPASS["w_min_default"]))
        w_max = float(twopass.get("w_max", video_models.VIDEO_TWOPASS["w_max_default"]))
        if w_min > w_max:
            w_min, w_max = w_max, w_min
        # CFG：SelfLiftH3Sampler 自带 cfg 输入（0~100）—— 这是 H3 链路里
        # **唯一**真正可调的 CFG（标准路径的 BasicGuider 没有 cfg 槽位）。
        try:
            twopass_cfg = float(twopass.get(
                "cfg", video_models.VIDEO_TWOPASS["cfg_default"]))
        except (TypeError, ValueError):
            twopass_cfg = float(video_models.VIDEO_TWOPASS["cfg_default"])
        twopass_cfg = max(0.0, min(100.0, twopass_cfg))
        out["twopass"] = {
            "enabled": True,
            "transition_step": transition,
            "lowres_scale": lowres,
            "rho": rho,
            "w_min": w_min,
            "w_max": w_max,
            "upscaler_model": upscaler,
            "cfg": twopass_cfg,
            "sigma_refine": {
                "enabled": refine_enabled,
                "extra_steps": extra_steps,
                "start_at_sigma": max(0.0, min(20.0, start_at_sigma)),
                "end_at_sigma": max(0.0, min(5.0, end_at_sigma)),
                "spacing": spacing,
            },
        }
    else:
        out["twopass"] = {"enabled": False}

    # ---- 步数 / 采样参数 ----
    #
    # 【2026-11-11】文生视频面板补齐采样参数后，这几个字段真的有前端控件了，
    # 所以这里必须做**白名单归一化**：
    #   * steps 越界 -> 夹到 [1, 50]
    #   * sampler / scheduler 不在本机 ComfyUI 的 COMBO 清单里 -> 回落到模型默认
    #     （不报错：用户在老任务里存过的名字可能是旧版清单里的）
    #   * shift 越界 -> 夹到 [0.01, 100]
    #
    # CFG 单独处理：H3 **标准采样路径没有 CFG**（BasicGuider 无 cfg 槽位），
    # 所以 out["cfg"] 固定为 1.0（VIDEO_CFG_FIXED）；面板只做只读说明。
    # 真正可调的 CFG 是二采路径的 twopass.cfg -> SelfLiftH3Sampler.cfg。
    steps = video_models.normalize_steps(
        params.get("steps"), int(model.get("steps", 4)))
    out["steps"] = steps
    out["sampler"] = video_models.normalize_sampler(
        params.get("sampler"), model.get("sampler", "euler"))
    out["scheduler"] = video_models.normalize_scheduler(
        params.get("scheduler"), model.get("scheduler", "beta"))
    # 标准路径 CFG：固定 1.0（见 video_models.VIDEO_CFG_FIXED_REASON）
    out["cfg"] = video_models.VIDEO_CFG_FIXED

    # ---- shift（MiniMaxH3SigmaShift）----
    # 节点是 ComfyUI 核心节点（comfy_extras.nodes_minimax_h3），
    # 默认值 12.0 / 3.0 = 模型内生值 -> 不改任何出图结果。
    out["shift_video"] = video_models.normalize_shift(
        params.get("shift_video"), video_models.VIDEO_SHIFT_DEFAULT_VIDEO)
    out["shift_audio"] = video_models.normalize_shift(
        params.get("shift_audio"), video_models.VIDEO_SHIFT_DEFAULT_AUDIO)
    # 节点不存在时前端会收到 sigma_shift_node 的可用性 -> 这里再兜一层，
    # 保证提交到工作流的一定是本机 ComfyUI 认识的节点。
    out["use_sigma_shift"] = bool(
        params.get("use_sigma_shift", True)
        and "MiniMaxH3SigmaShift" in _COMFY_NODE_NAMES)

    # 【2026-11-12 补】shift 节点在本机 ComfyUI 里真不存在时，
    # 服务端必须**明确告诉前端**，否则面板上两个 shift 滑杆
    # 变成「能拖但服务端会丢掉」的假控件。
    # （本机实测该节点是 ComfyUI 核心节点，恒在 —— 但不能假设。）
    out["sigma_shift_available"] = "MiniMaxH3SigmaShift" in _COMFY_NODE_NAMES

    # 种子：留空则随机（worker 里再兜一次）
    if params.get("seed") not in (None, ""):
        try:
            out["seed"] = int(params["seed"])
        except (TypeError, ValueError):
            out["seed"] = None
    else:
        out["seed"] = None

    return out, ""


def _record_segment_rel(job_id: str, segment_rel: str) -> None:
    """把本段的分段视频路径记到**本段自己的**任务记录里。

    【为什么记在自己身上，而不是链首段的共享账本】
    旧实现把分段列表存在**链首段**的 params 里（一个共享的可变列表）。
    同一条链分叉成 B1 / B2 时，两兄弟都会往这个列表里追加 ->
    列表变成 [A, B1, B2]。再从 B1 接出 C1 时，按这个列表拼接得到的
    「完整视频」会把 **B2 混进 C1** —— 又是一次静默的错误内容。

    改成**每段记自己**、再用 `source_job` 指针**回溯血缘**：
      * B1 的血缘 = A -> B1
      * B2 的血缘 = A -> B2
      * C1 的血缘 = A -> B1 -> C1   （B2 不在里面）
    分支天然隔离，不需要任何共享可变状态。
    """
    if not segment_rel:
        return
    with db() as conn:
        row = conn.execute("SELECT params FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
        if not row:
            return
        params = json.loads(row["params"] or "{}")
        info = cont_service.chain_info(params)
        if not info:
            return
        info["segment_rel"] = segment_rel
        params["continuation"] = info
        conn.execute("UPDATE jobs SET params=? WHERE id=?",
                     (json.dumps(params, ensure_ascii=False), job_id))


def _lineage_segment_rels(job_id: str) -> list[str]:
    """本段的**血缘**分段视频列表（从链首段到本段，按顺序）。

    沿 `source_job` 指针回溯 —— 每一步都指向**自己的上游**，
    所以兄弟分支永远不会混进来。
    """
    rels: list[str] = []
    seen: set[str] = set()
    current = job_id
    # 上限：防止脏数据造成死循环（链最多也就几十段）。
    for _ in range(512):
        if not current or current in seen:
            break
        seen.add(current)
        with db() as conn:
            row = conn.execute("SELECT params FROM jobs WHERE id=?",
                               (current,)).fetchone()
        if not row:
            break
        params = json.loads(row["params"] or "{}")
        info = cont_service.chain_info(params)
        if not info:
            break
        rel = str(info.get("segment_rel") or "").strip()
        if rel:
            rels.append(rel)
        current = str(info.get("source_job") or "").strip()
    rels.reverse()
    return rels


def _concat_chain_video(segments: list[str], chain_id: str) -> Path | None:
    """把链上的分段视频按序拼成**完整视频**。返回输出路径（失败返回 None）。

    【为什么用 concat demuxer 而不是 filter_complex】
    concat demuxer 直接拼流，不重新编码 —— 快、不掉画质。
    分辨率/帧率不一致时 ffmpeg concat 会出问题，所以先探测第一段的
    尺寸并统一（与 run_longfilm 的做法一致）。

    【音画同步】分段视频各自已经是音画同裁的（TrimAV 裁重叠时
    音画一起裁），所以直接拼接不会累积 A/V 偏移。
    """
    if not segments:
        return None
    paths: list[Path] = []
    for rel in segments:
        candidate = (OUT_DIR / rel).resolve()
        try:
            candidate.relative_to(OUT_DIR.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            paths.append(candidate)
    if not paths:
        return None
    if len(paths) == 1:
        # 只有一段 —— 完整视频就是它自己
        return paths[0]
    if not FFMPEG:
        print("[接续] 未找到 FFmpeg，无法拼接完整视频")
        return None
    width, height = _probe_video_size(paths[0])
    list_path = DATA / f"concat-cont-{cont.sanitize_run_id(chain_id)}.txt"

    def quote_concat(path: Path) -> str:
        return str(path).replace("'", "'\\''")

    try:
        list_path.write_text(
            "\n".join(f"file '{quote_concat(p)}'" for p in paths),
            encoding="utf-8")
        output_path = _next_output_path("video", ".mp4")
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
              f"force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
              f"setsar=1,fps={cont.FPS},format=yuv420p")
        command = [FFMPEG, "-y", "-f", "concat", "-safe", "0",
                   "-i", str(list_path), "-vf", vf,
                   "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                   "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                   "-movflags", "+faststart", str(output_path)]
        result = subprocess.run(command, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=600,
                                creationflags=CREATE_NO_WINDOW)
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace")[-800:]
            print(f"[接续] 拼接失败: {message}")
            if output_path.exists():
                output_path.unlink()
            return None
        return output_path
    except Exception as exc:
        print(f"[接续] 拼接异常: {exc}")
        return None
    finally:
        if list_path.exists():
            try:
                list_path.unlink()
            except OSError:
                pass


def _continuation_size_mismatch(source_params: dict, target_params: dict) -> str:
    """接续时上游与本段的分辨率不一致 -> 中文原因（空串 = 放行）。

    规则实现放在协议层（h3studio/continuation.py 的 size_mismatch），
    那里是纯函数、可离线单测；这里只做一次转发。
    """
    return cont.size_mismatch(source_params, target_params)


def _prepare_continuation_params(params: dict) -> str:
    """校验并补全任务的接续参数。返回错误信息（空串 = 通过）。

    前端提交时带的 `continuation` 字段来自「接续上下文」回填 —— 它描述的是
    **上一段**。本段是**新的一段**，所以段号要 +1、接力棒指向上一段。

    服务端在这里做四件事：
      1. 校验上游产物支持接续（不是二采 —— 二采潜空间不同源）；
      2. 校验上游潜空间文件**真的在**（不在就明确报错）；
      3. 算本段的段号（上游段号 + 1）；
      4. 算累计帧数（用于拼接出完整视频的总长）。

    【为什么不信任前端传来的 chain_id / stage_index】前端可以随便填。
    段号与累计帧数必须由服务端按**任务记录**重算，否则用户改个数字
    就能让接缝错位（表现为整体位移的接缝）。
    """
    info = params.get("continuation")
    if not isinstance(info, dict) or not info:
        return ""

    source_job = str(info.get("source_job") or "").strip()
    if not source_job:
        return ("接续上下文缺少来源任务（source_job）—— "
                "请在生成库里重新点一次「接续」。")

    with db() as conn:
        row = conn.execute("SELECT mode, params, status FROM jobs WHERE id=?",
                           (source_job,)).fetchone()
    if not row:
        return f"接续的上游任务 {source_job} 不存在（可能已被删除）。"

    source_mode = row["mode"]
    source_params = json.loads(row["params"] or "{}")

    # 上游必须支持接续
    allowed, reason = cont.continuation_allowed(source_mode, source_params)
    if not allowed:
        return ("接续失败：" + reason)

    # 上游的段号（权威值来自上游任务自己的记录，不是前端传的）
    source_info = cont_service.chain_info(source_params)
    try:
        source_stage = int(source_info.get("stage_index", 0))
    except (TypeError, ValueError):
        source_stage = 0
    source_chain = str(source_info.get("chain_id") or "").strip()

    # 上游潜空间必须真的在 —— 且必须是**它自己那一份**。
    #
    # 【关键：坐标从上游任务记录里算，不从链段号推】
    # 每段一个专属目录（run_id 由该段任务 id 派生），所以坐标是
    # 「上游是谁 -> 上游的目录 + 目录里的段号」。
    # 若按链段号推，兄弟分支 B1/B2 会算出**同一个**坐标 ->
    # 写同一个文件、互相覆盖 -> 再接续读到兄弟分支的内容（静默错误）。
    source_run_id, source_latent_stage = cont.latent_coords(source_info)
    if not source_run_id:
        return ("接续的上游任务没有可用的潜空间（不是用「接续」生成的）—— "
                "请改用「复用参数」重新生成一段。")
    if not cont_service.stage_exists(source_run_id, source_latent_stage):
        return cont.missing_reason(cont_service.comfy_output_dir(),
                                   source_run_id, source_latent_stage)

    # ---- 分辨率必须与上游一致（2026-11-12 修）----
    #
    # 【为什么必须在这里拦】潜空间桥是**逐 token 拷贝**上一段的尾部 ——
    # 它的 latent 空间分辨率是固定的。跨分辨率接续在 Relay-Kit 里会直接
    # raise：「拷贝桥禁止跨分辨率：上段 768x448 ≠ 本段 1024x576」。
    #
    # 【为什么以前会变成一句 Python 报错】用户点「接续」后 context 还挂着，
    # 又点别的卡片的「复用参数」改了尺寸 —— 提交时 params 是新尺寸、
    # continuation 还是旧段的。服务端此前**不校验**这一项，于是错误一路
    # 跑到 ComfyUI；再加上错误文本提取本身有缺陷（把 m[0] 当 dict 读），
    # 用户最终只看到 `'str' object has no attribute 'get'`。
    #
    # 现在：**提交前**就给出可执行的中文提示，不浪费一次生成。
    mismatch = _continuation_size_mismatch(source_params, params)
    if mismatch:
        return mismatch

    # 上游潜空间文件的**绝对路径** —— 读回必须精确到这一个文件。
    #
    # 【⚠ 带内容指纹的副本 —— 破 ComfyUI 的节点缓存】
    # ComfyUI 的缓存 key 只看**节点输入**（run_id / stage_index /
    # explicit_path 三个字符串），**不看文件内容变没变**。
    # 上一段潜空间被重跑替换后，这三个值一字未变 -> 命中缓存 ->
    # `H3RelayLatentLoad` 压根没被调用 -> 返回**老 latent** ->
    # 新一段实际接的是上一版的画面，且**不报错**。
    # 把内容指纹编进路径即可破缓存（借鉴 nonebot 咏唱 h3）。
    source_path = str(cont_service.ensure_fingerprinted_copy(
        source_run_id, source_latent_stage))

    # 外观锚：链首段那一份（防长程漂移）。链首段的坐标同样从它的记录算。
    anchor_path = ""
    anchor_run_id = ""
    anchor_stage = 0
    if source_chain:
        with db() as conn:
            head_row = conn.execute("SELECT params FROM jobs WHERE id=?",
                                    (source_chain,)).fetchone()
        if head_row:
            head_info = cont_service.chain_info(json.loads(head_row["params"] or "{}"))
            head_run_id, head_stage = cont.latent_coords(head_info)
            if head_run_id and cont_service.stage_exists(head_run_id, head_stage):
                anchor_run_id = head_run_id
                anchor_stage = head_stage
                # 锚同样带指纹 —— 它也是「读一个可能变过的文件」。
                anchor_path = str(cont_service.ensure_fingerprinted_copy(
                    head_run_id, head_stage))

    # 本段的段号 = 上游段号 + 1
    stage_index = source_stage + 1

    # 链 id：链首段用**自己的任务 id**做链 id，后续段沿用上游的链 id。
    # 这样「用户对完整视频再点接续」时链 id 不变、段号 +1 —— 无限接续。
    chain_id = source_chain or source_job

    # 钉住窗口 / 外观锚帧数：必须落在 5+17k 网格上
    window = cont.snap_context_frames(
        int(info.get("context_frames") or cont.DEFAULT_CONTEXT_FRAMES))
    if not window:
        window = cont.DEFAULT_CONTEXT_FRAMES
    anchor = cont.snap_context_frames(
        int(info.get("anchor_frames") or cont.DEFAULT_CONTEXT_FRAMES))
    if not anchor:
        anchor = cont.DEFAULT_CONTEXT_FRAMES

    # 本段总帧数（17k+5 网格）与本段交付帧数（总帧数 - 钉住帧数）
    duration = float(params.get("duration") or 5)
    segment_total = cont.align_frame_count(int(round(duration * cont.FPS)))

    # 累计帧数：上游已累计的 + 本段交付的。
    #
    # 【为什么不能信前端传的 chain_total_frames】前端传的值来自上游的
    # 卡片，可能过期。**权威值**是上游任务自己记录里的那份 ——
    # 分叉时每个分支各自累计，互不干扰（上游的累计值只属于上游那条血缘）。
    upstream_state = cont_service.continuation_state(source_mode,
                                                     source_params, source_job)
    chain_total = int(upstream_state.get("total_frames") or 0)

    params["continuation"] = {
        "chain_id": chain_id,
        "stage_index": stage_index,
        "source_job": source_job,
        "source_segment": source_stage,
        # 接力棒的**精确坐标**（读回就靠它 —— 不能按链段号推）
        "source_latent_run_id": source_run_id,
        "source_latent_stage": source_latent_stage,
        "source_latent_path": source_path,
        # 外观锚（链首段那份）；找不到就留空 —— 锚是防漂移的加分项，
        # 不能因为锚缺失就让整个接续失败。
        "anchor_latent_path": anchor_path,
        "anchor_latent_run_id": anchor_run_id,
        "anchor_latent_stage": anchor_stage,
        "context_frames": window,
        "anchor_frames": anchor,
        "chain_total_frames": chain_total,
        "segment_frames": segment_total,
    }
    return ""


def create_job_record(mode: str, params: dict, session_id: str = "") -> str:
    """
    建一条生成任务记录，返回 job_id。
    POST /api/jobs（面板点「生成」）与内置 Agent 的 create_job 工具
    必须是**同一套**建任务逻辑 —— 两份实现必然漂移：
      * 生成类型（gen_type）写不进去 -> 任务列表标签变成「其它生成」
      * 批次拆分逻辑不同 -> 面板出 3 张、Agent 出 1 张
      * 素材引用（asset_refs）漏写 -> worker 拿不到参考图
    所以这里抽出唯一的实现，两边都调它。

    params 会被就地写入（补 gen_type / asset_refs），调用方按需自取。

    session_id:
      任务归属的 Agent 会话 id（面板侧创建的任务留空）。
      建任务时**当场**记进 jobs.session_id —— 这是「本次会话产物」面板
      唯一可信的数据来源（详见 _ensure_jobs_session_column 的说明）。
    """
    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    # 生成类型写进任务 meta（真实记录，不靠读时推断）
    params["gen_type"] = gen_types.type_key_for_mode(mode)
    asset_refs = params.get("asset_refs")
    if asset_refs:
        params["asset_refs"] = asset_refs

    # ---- 接续链的建立 ----
    #
    # 【为什么每个 t2v 任务都要有链信息】用户需求：「每次生成自动把
    # 可接续的潜空间文件落盘保存，与产物记录关联并可在生成库中识别」。
    # 所以**每一个** t2v 任务都是一条链的第 0 段：它会落盘自己的潜空间，
    # 从而可以被继续接续。
    #
    # 链 id = **自己的任务 id**（链首段）。后续段沿用上游的链 id，
    # 于是「对完整视频再点接续」时链 id 不变、段号 +1 —— 无限接续。
    #
    # 【潜空间坐标 = 每段一个专属目录】`latent_run_id` 由**本段自己的
    # 任务 id** 派生，段号恒 0。这样兄弟分支（B1/B2，各自从同一段接出）
    # 各写各的目录 —— 结构上不可能互相覆盖。
    # 若沿用「一条链一个目录 + 链段号」，两个分支会算出同一个坐标 ->
    # 写同一个文件 -> 后续接续读到兄弟分支的内容（静默错误内容）。
    # 详见 continuation.segment_run_id。
    #
    # 二采不建链（潜空间不同源，见 h3studio/continuation.py）。
    if mode == "t2v":
        info = params.get("continuation")
        if not isinstance(info, dict) or not info:
            duration = float(params.get("duration") or 5)
            segment_total = cont.align_frame_count(int(round(duration * cont.FPS)))
            params["continuation"] = {
                "chain_id": job_id,
                "stage_index": 0,
                "source_job": "",
                "source_segment": -1,
                # 本段自己的落盘目录（每段一个，防分叉覆盖）
                "latent_run_id": cont.segment_run_id(job_id),
                "latent_stage": 0,
                "context_frames": cont.DEFAULT_CONTEXT_FRAMES,
                "anchor_frames": cont.DEFAULT_CONTEXT_FRAMES,
                "chain_total_frames": 0,
                "segment_frames": segment_total,
            }
        else:
            # 接续段：本段也要有**自己的**落盘目录（接力棒写这里）。
            # 上游坐标由 _prepare_continuation_params 权威算好并带入。
            #
            # 【⚠ 这里必须**强制覆盖**，不能用 setdefault】
            # `latent_run_id` 是「**本段自己**的落盘目录」—— 它必须由
            # **本段的任务 id** 派生，一段一个目录。
            #
            # 旧实现用 setdefault：如果 params 里已经带了一个
            # `latent_run_id`（例如「重试」把源任务的 params 整份拷过来，
            # 或前端回传了带坐标的上下文），新任务就会**沿用别人的目录**
            # -> 写同一个 `stage_00000.safetensors` -> **覆盖上游的潜空间**。
            # 这与 D1 修掉的「分叉分支互相覆盖」是**同一类缺陷**：
            # 落盘坐标必须与「产出它的那一段」一一对应。
            #
            # 证据（tests/test_continuation_retry.py）：
            #   重试 A -> 新任务沿用 A 的 latent_run_id -> A 的潜空间被覆盖。
            info["latent_run_id"] = cont.segment_run_id(job_id)
            info["latent_stage"] = 0
            params["continuation"] = info
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
            "INSERT INTO jobs (id, created_at, mode, params, status, session_id) "
            "VALUES (?,?,?,?, 'queued', ?)",
            (job_id, now, mode, json.dumps(params, ensure_ascii=False),
             str(session_id or "")))
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


# ------------------------------------------------------------------ Agent 任务查询
#
# Agent 的任务类工具（cancel_job / retry_job / list_jobs / get_job_status）
# 需要**读**任务库。写成模块级函数而不是 HTTP handler，是为了让 Agent
# 直接复用（与 create_job_record / prepare_job_params 同一个模式）。
#
# 为什么不复用 api_get / api_cancel（它们是 async handler）：
#   * api_cancel 会去 ComfyUI 撤单 —— Agent 的 cancel_job 需要这个行为，
#     但它得在 async 上下文里跑。Agent 的工具 handler 本来就是 async，
#     所以这里提供 async 版本。
#   * 读类（get/list）用同步函数即可 —— SQLite 读很快。

def get_job_params(job_id: str) -> dict | None:
    """取一个任务的 mode + params（供 retry_job 复用）。不存在返回 None。"""
    with db() as conn:
        row = conn.execute(
            "SELECT mode, params FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    try:
        params = json.loads(row["params"] or "{}")
    except (json.JSONDecodeError, TypeError):
        params = {}
    return {"mode": row["mode"], "params": params}


def list_job_records(status: str | None = None, mode: str | None = None,
                     since_hours: int | None = None,
                     limit: int = 10) -> list[dict]:
    """
    检索历史任务（供 Agent 的 list_jobs 工具用）。

    刻意**自己查**而不复用 api_list 的过滤逻辑：api_list 面向前端
    （有一堆 UI 专用筛选），Agent 需要的是「按状态/模式/时间窗口」的
    简单检索。两套筛选各有用途，强行合并会让两边都变复杂。
    """
    conditions = []
    args: list = []
    if status:
        conditions.append("status=?")
        args.append(status)
    if mode:
        conditions.append("mode=?")
        args.append(mode)
    if since_hours and since_hours > 0:
        conditions.append("created_at >= ?")
        args.append(time.time() - float(since_hours) * 3600.0)
    sql = "SELECT * FROM jobs"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(int(limit))
    with db() as conn:
        rows = conn.execute(sql, args).fetchall()
    result = []
    for row in rows:
        job = row_to_job(row)
        result.append({
            "job_id": job["id"],
            "mode": job["mode"],
            "label": job["gen_type_label"],
            "status": job["status"],
            "created_at": job["created_at"],
            "progress": job["progress"],
            "progress_text": job["progress_text"],
            "prompt": (job["params"] or {}).get("prompt") or "",
            "error": job["error"],
            "outputs": job["outputs"],
            # 【2026-11-11】把「实际输出尺寸 / 比例」一起给 Agent 任务卡。
            # 原来 Agent 卡只显示 params.resolution 那个档位字符串，
            # 文生视频实际跑 1344×768 时它恒为遗留默认 "544p"。
            "params": job["params"],
            "resolution_info": job["resolution_info"],
        })
    return result


def session_job_counts(session_ids: list[str]) -> dict[str, int]:
    """
    一批会话各自关联的任务数（会话库 -> 任务库的跨库统计）。

    【为什么单独一个函数】
    会话库（agent_sessions.db）与任务库（jobs.db）是**两个库**，
    会话库自己不知道产物数；要拿就得查 jobs 表。

    【为什么用一次 IN 查询而不是每个会话查一次】
    会话列表可能有几百条 —— N+1 次查询在 SQLite 上也不致命，
    但一次 IN 查询更干净，也避免两个库的连接来回切。

    返回 {session_id: count}；查不到/出错返回空 dict（不阻断列表）。

    【口径说明 —— 2026-11-12】
    这里数的是**任务数**，不是「产出了文件的任务数」。
    失败的任务、空输出的任务也计入。
    前端把它显示成「N 产物」略有歧义（见 session_job_stats 的说明）。
    """
    ids = [str(s) for s in (session_ids or []) if s]
    if not ids:
        return {}
    if not _jobs_table_has_session_column():
        # 这张库没有归属信息 -> 产物数一律 0（不抛异常，列表照常显示）。
        return {}
    try:
        with db() as conn:
            placeholders = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT session_id, COUNT(*) AS c FROM jobs "
                f"WHERE session_id IN ({placeholders}) GROUP BY session_id",
                ids).fetchall()
        return {str(r["session_id"]): int(r["c"]) for r in rows}
    except Exception:
        # 查不到就返回空 —— 列表照常显示，产物数显示 0。
        # **不能**因为统计失败就让整个会话列表打不开。
        return {}


def session_job_stats(session_ids: list[str]) -> dict[str, dict]:
    """
    一批会话的任务统计（任务数 / 未完成数）—— **新增**，不改动 session_job_counts。

    【为什么不直接改 session_job_counts —— 2026-11-12】
    session_job_counts 返回 {sid: int}，被 server.py 与既有测试按整数用。
    改成 dict-of-dict 是 breaking change。
    这里**另起一个函数**，老函数原样保留 —— 零风险。

    【返回什么】
      {session_id: {"count": 任务数, "pending": 排队中+生成中的任务数}}

    【pending_count 的用途】
    会话列表的「状态」只描述**这一轮对话的收尾方式**，
    不含「进行中」—— 因为服务端没有 per-session 运行注册表
    （AGENT_GATE 是**全局**信号量，不记录是哪个会话在跑）。
    但「这个会话还有任务在生成」是**任务**层面的事实，
    可以作为独立副信息显示（文案必须指向任务，不能说会话「进行中」）。

    一次查询同时拿到两个数 —— 不额外往返。
    """
    ids = [str(s) for s in (session_ids or []) if s]
    if not ids:
        return {}
    if not _jobs_table_has_session_column():
        return {}
    try:
        with db() as conn:
            placeholders = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT session_id, COUNT(*) AS c, "
                f"       SUM(CASE WHEN status IN ('queued','running') THEN 1 ELSE 0 END) AS pending "
                f"FROM jobs WHERE session_id IN ({placeholders}) GROUP BY session_id",
                ids).fetchall()
        return {str(r["session_id"]): {"count": int(r["c"]),
                                       "pending": int(r["pending"] or 0)}
                for r in rows}
    except Exception:
        return {}


def clear_session_job_refs(session_id: str) -> int:
    """
    清掉某个会话的任务**引用关系**（jobs.session_id 置空）。

    【删除会话时调用 —— 2026-11-11 新增需求】
    用户要求「删除会话需级联清理该会话下的消息与产物引用关系，
    不误删素材库实体文件」。

    【关键：只清引用，不删任务】
    jobs 表里的任务记录**不是**会话的私有财产：
      * 它同时是「生成库」里的一条记录（用户能在生成列表里看到它）；
      * 它带着产物文件路径（outputs），删掉任务记录会让生成列表
        里出现「任务不见了但文件还在」的孤儿状态；
      * 素材库里的 asset_refs 是**独立**的实体文件（/mnt/usb2t/asset_library），
        与会话完全无关 —— **绝对不能**碰。
    所以这里只把 jobs.session_id 置空：任务本身留在生成库里，
    只是不再归属任何会话。

    【为什么不删任务，而是置空】
    删任务 = 用户在生成列表里凭空少了一条记录（可能是他想要的结果）。
    置空 = 「这个会话删了，但生成结果还在生成库里」——
    与「会话数据可丢弃、任务不可」的整体取舍一致。

    返回被清理的任务数。
    """
    sid = str(session_id or "").strip()
    if not sid:
        return 0
    if not _jobs_table_has_session_column():
        return 0
    try:
        with db() as conn:
            cur = conn.execute(
                "UPDATE jobs SET session_id='' WHERE session_id=?", (sid,))
            return int(cur.rowcount or 0)
    except Exception:
        # 清引用失败不阻断会话删除 —— 会话数据本身才是要删的。
        return 0



#: 缓存「当前 jobs 库有没有 session_id 列」。
#:
#: 【为什么需要缓存】
#: 正常路径下 init_db() 会保证这一列存在（迁移）。
#: 但**测试替身**（tests/test_agent_oneshot_e2e.py 里的 FakeServer）会自己
#: 建一张 12 列的 jobs 表且不调 init_db() —— 服务端去 ALTER 别人的库
#: 是不可接受的副作用。
#: 所以这里改成「查询前先看有没有这一列，没有就不按它过滤」，
#: 并把判断结果按库路径缓存（同一个进程里查一次就够）。
_JOBS_HAS_SESSION_COL: dict[str, bool] = {}


def _jobs_table_has_session_column() -> bool:
    """当前 jobs 库有没有 session_id 列（按 DB_PATH 缓存结果）。"""
    key = str(DB_PATH)
    cached = _JOBS_HAS_SESSION_COL.get(key)
    if cached is not None:
        return cached
    try:
        with db() as conn:
            rows = conn.execute("PRAGMA table_info(jobs)").fetchall()
        has = any(str(r[1]) == "session_id" for r in rows)
    except Exception:
        has = False
    _JOBS_HAS_SESSION_COL[key] = has
    return has


def list_jobs_for_session(session_id: str) -> list[dict]:
    """
    列出某个 Agent 会话里创建的**全部**任务（权威来源：jobs 表）。

    【为什么这是唯一可信的来源 —— 2026-11-11】
    「本次会话产物」面板不能用这两种做法：
      1. 从 agent_messages 的正文里正则抠 job id
         -> 那是**模型写的文本**，模型可能编 id（本项目修过这个 bug），
            抠出来的可能是假任务；
      2. 按时间窗猜「会话活跃期间创建的任务」
         -> 会话之间串味（用户在会话 A 里操作，看到会话 B 的任务）。
    只有 jobs.session_id 是**建任务时当场写入**的，天然精确。

    返回结构与 /api/jobs 一致（row_to_job 的形状），
    外加 session_id 与 batches —— 前端可以直接渲染，不用再拼。

    【兼容没有 session_id 列的库 —— 2026-11-11】
    老库 / 测试替身的 jobs 表可能没有这一列。这种情况下
    **返回空列表**（而不是抛异常）——
    因为「查不到归属」等价于「这个会话没有产物」。
    抛异常会让整个 /api/agent/sessions/{id} 500，
    前端连对话历史都读不回来（实测一次性路径的工具块因此没渲染）。
    """
    sid = str(session_id or "").strip()
    if not sid:
        return []
    if not _jobs_table_has_session_column():
        # 这张库没有归属信息 -> 无法按会话过滤 -> 当作「没有产物」。
        # 不抛异常：调用方是 /api/agent/sessions/{id}，
        # 抛了会让**整个会话详情** 500（历史消息都读不回来）。
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE session_id=? ORDER BY created_at DESC",
            (sid,)).fetchall()
    out = []
    for row in rows:
        job = row_to_job(row)
        job["session_id"] = sid
        job["batches"] = batches_of(str(row["id"]))
        out.append(job)
    return out



def list_job_ids_since(since_ts: float) -> list[str]:
    """
    **权威来源**：某个时间点之后创建的任务 id 列表。

    【为什么需要这个函数 —— 2026-11-11 实测误报】
    Agent 的「模型是否在编造 job id」判定，原来只看 ctx.jobs
    （= 本次运行 create_job 工具往内存里 append 的条目）。
    但真实任务可能来自**别的路径**：
      * batch_create_jobs / retry_job（内部调 create_job，条目也会进 ctx.jobs）
      * 面板侧点「生成」（同一时刻另一个会话建的任务）
      * 任务行已提交但 Agent 进程还没拿到回执（异步落库延迟）
    这些情况下 ctx.jobs 为空，但任务**真的存在**。
    拿 ctx.jobs 当唯一判据 = 把真实任务判成「编造」。

    所以判定必须落到**数据库**这一层：本函数直接查 jobs 表。

    【为什么不用 list_job_records 再筛】
    list_job_records 返回整行（含 params/outputs），Agent 只需要 id，
    多读一堆 JSON 纯属浪费；而且它的 since_hours 是**小时**粒度，
    覆盖不了「几秒前刚建的任务」这种判定窗口。

    返回的 id 按 created_at 升序（旧的在前）。
    """
    if not since_ts:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE created_at >= ? ORDER BY created_at ASC",
            (float(since_ts),)).fetchall()
    return [str(row["id"]) for row in rows]


def get_job_status_payload(job_id: str) -> dict | None:
    """
    单个任务的**轮询友好**状态（供 Agent 的 get_job_status 工具用）。

    刻意做成「前端拿到就能直接渲染」的形状：
      * status / progress / progress_text —— 进度条能直接用
      * outputs —— 产物路径（前端轮询这个拿缩略图/输出）
      * elapsed —— 已耗时
      * poll_after_ms —— 建议的下次轮询间隔（前端照着排程即可）
    """
    with db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    job = row_to_job(row)
    batches = batches_of(job_id)
    status = job["status"]
    # 未结束的任务建议 3 秒后再查；已结束的让前端停掉轮询
    poll_after_ms = 0 if status in ("done", "failed", "canceled") else 3000
    payload = {
        "job_id": job["id"],
        "mode": job["mode"],
        "label": job["gen_type_label"],
        "status": status,
        "progress": job["progress"],
        "progress_text": job["progress_text"] or "",
        "error": job["error"],
        "outputs": job["outputs"] or [],
        "params": job["params"] or {},
        "created_at": job["created_at"],
        "elapsed": job["elapsed"],
        "poll_after_ms": poll_after_ms,
        "is_terminal": status in ("done", "failed", "canceled"),
    }
    if batches:
        payload["batches"] = batches
    return payload


async def cancel_job_record(job_id: str) -> dict:
    """
    取消一个任务（供 Agent 的 cancel_job 工具用）。

    与 api_cancel 的区别：api_cancel 是 HTTP handler（读 request），
    这里接受 job_id 直接调用；两者共用同一套状态机与 ComfyUI 撤单逻辑。
    """
    async with WORKER.submit_lock:
        with db() as conn:
            r = conn.execute(
                "SELECT status, prompt_id, mode FROM jobs WHERE id=?",
                (job_id,)).fetchone()
        if not r:
            raise ValueError(f"任务不存在：{job_id}")
        if r["status"] not in ("running", "queued"):
            # 已经结束了 —— 不是错误，直接告诉模型当前状态
            return {"job_id": job_id, "status": r["status"], "ok": True,
                    "message": f"任务已是「{r['status']}」状态，无需取消"}
        pid = r["prompt_id"]
        if pid:
            try:
                async with ClientSession(timeout=ClientTimeout(total=6)) as session:
                    for endpoint, payload in (("queue", {"delete": [pid]}),
                                              ("interrupt", {"prompt_id": pid})):
                        async with session.post(f"{COMFY}/{endpoint}",
                                                json=payload) as resp:
                            resp.raise_for_status()
            except Exception as exc:
                raise ValueError(f"无法确认停止请求，请重试：{exc}")
        with db() as conn:
            conn.execute(
                "UPDATE jobs SET status='canceled', finished_at=? "
                "WHERE id=? AND status IN ('queued','running','failed')",
                (time.time(), job_id))
            status = conn.execute(
                "SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"]
    return {"job_id": job_id, "status": status, "ok": True}


class JobValidationError(ValueError):
    """建任务前的参数校验失败。message 面向用户，直接可读。"""

    def __init__(self, message, code="bad_request", extra=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.extra = extra or {}


#: 旧路径视频加速 LoRA 的**合法 key 白名单**（workflows.LORAS 的真源）。
#: params["lora"] 只允许是这里的 key，或字面量 "none"。
#: 【2026-11-13】见 prepare_job_params 里「落库前的 LoRA 白名单闸门」。
H3_VIDEO_LORAS = dict(LORAS)

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

    # 【2026-11-11】首尾帧 / 参考生视频的采样参数归一。
    # 这两个模式走旧工作流路径，但采样链路与文生视频相同
    # （BasicScheduler + KSamplerSelect + BasicGuider），
    # 所以采样器 / 调度器 / shift 一样是真参数。
    #
    # CFG 例外：H3 标准路径固定 1.0（BasicGuider 没有 cfg 槽位），
    # 面板只做只读说明 —— 这里也不写 params["cfg"]，
    # 免得旧路径的 params 看起来像有个可调的 cfg（实际没人读）。
    # 【2026-11-13 修 —— 落库前的 LoRA 白名单闸门】
    # `params["lora"]` 只可能是 workflows.LORAS 里的 key（ckpt850 /
    # v4step600）或字面量 "none"。这条链路上**原本没有任何白名单校验**：
    #   * prepare_job_params 只按 lora 决定采样档位，不校验它本身；
    #   * Agent 的 validate_params 只守 Agent 工具入口，
    #     面板提交 / 内置 Agent 批量 / 直接调 API 都不经过它。
    # 于是**任意字符串**都能落进 params["lora"] 并被原样存进 jobs 表。
    # 用户实测就遇到卡片 LoRA 行显示 `integrated_multimodal_description`
    # —— 那是 H3 三段式提示词的**字段名**，根本不是 LoRA。
    #
    # 这里在**落库前**拦一道：白名单外的值直接丢弃（视为「没传」，
    # 由工作流走自己的默认档），绝不让脏值进库、也不报错打断用户
    # （历史任务里已经躺着这类脏值，报错会让老任务无法复用）。
    #
    # 读取侧还有第二道闸（job_meta._is_valid_legacy_lora_key），
    # 负责把**已经落库**的脏值在显示层挡掉 —— 两道闸互补。
    #
    # 【2026-11-13 修 —— 闸门扩到**全部生成模式**】
    # 原实现只对视频模式（t2v / flf / r2v / longfilm）做白名单，
    # 于是 **图像 / 音乐模式完全裸奔**：
    #   * t2i / t2i_qwen / t2i_moss / t2a / a2a 的 `params["lora"]`
    #     原样落库（实测落库值 = "integrated_multimodal_description: [Shot 1] ..."）；
    #   * 素材库入库又把 params.lora 原文写进 source_meta -> 抽屉里原样印出来。
    # 现在改成**按模式语义**统一处理：
    #   * 该模式的工作流**真的读** params["lora"]（视频旧路径）
    #     -> 值必须过白名单，否则丢弃；
    #   * 该模式**不读**这个字段（图像 / 音乐）
    #     -> 传了就是误填（提示词塞进了 lora 字段），一律丢弃。
    # 【为什么丢弃而不是报错】
    # 历史任务里已经躺着这类脏值；报错会让老任务无法复用，
    # 而丢弃是「当作没传」——工作流走自己的默认档，行为可预期。
    raw_lora = params.get("lora")
    if raw_lora is not None:
        safe_lora = job_meta.sanitize_legacy_lora_value(raw_lora)
        if not safe_lora:
            params.pop("lora", None)
            params.pop("lora_strength", None)
        elif safe_lora != str(raw_lora).strip():
            params["lora"] = safe_lora

    # 【2026-11-13 修 —— `params["loras"]` 数组同样要过白名单】
    # 图像模式的 LoRA 入口是 `params["loras"]`（[{name, strength, trigger}]）。
    # 它的 `name` 同样**没有落库校验** —— Agent 把提示词字段名塞进去时，
    # 卡片 LoRA 行会显示整段提示词。
    # 这里按「本机真实 LoRA 清单」逐项过滤：
    #   * 不在清单里的（提示词 / 乱填）-> 整项丢掉；
    #   * 清单里的 -> 原样保留（strength / trigger 都不动）。
    raw_loras = params.get("loras")
    if isinstance(raw_loras, list) and raw_loras:
        kept = []
        for item in raw_loras:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not job_meta.is_real_lora_key(name):
                continue
            kept.append(item)
        if kept:
            params["loras"] = kept
        else:
            params.pop("loras", None)

    if mode in ("flf", "r2v"):
        # 【2026-11-12 修】默认值必须与工作流里的硬编码档位**完全一致**，
        # 否则老客户端（不传 sampler/scheduler）会被静默改画质：
        #   workflows._h3_video_workflow 的规则是
        #     lora != "none" -> steps 4 / scheduler beta   / sampler euler
        #     lora == "none" -> steps 20 / scheduler simple / sampler res_multistep
        # 之前这里写死 default="euler"/"beta" 并且**无条件写回** params，
        # -> lora=="none" 的老任务被改成 euler/beta（静默回归）。
        # 现在：客户端**没传**就不写回（让工作流用自己的档位），
        #       传了才做白名单归一。
        legacy_default_sampler = "euler" if params.get("lora") != "none" else "res_multistep"
        legacy_default_scheduler = "beta" if params.get("lora") != "none" else "simple"
        if params.get("sampler"):
            params["sampler"] = video_models.normalize_sampler(
                params["sampler"], legacy_default_sampler)
        if params.get("scheduler"):
            params["scheduler"] = video_models.normalize_scheduler(
                params["scheduler"], legacy_default_scheduler)
        params["shift_video"] = video_models.normalize_shift(
            params.get("shift_video"), video_models.VIDEO_SHIFT_DEFAULT_VIDEO)
        params["shift_audio"] = video_models.normalize_shift(
            params.get("shift_audio"), video_models.VIDEO_SHIFT_DEFAULT_AUDIO)
        params["use_sigma_shift"] = bool(
            params.get("use_sigma_shift", True)
            and "MiniMaxH3SigmaShift" in _COMFY_NODE_NAMES)

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
            # 素材库来源的参考素材也算（worker 会按 asset_refs 上传）
            #
            # 【2026-11-11 音视频扩展】参考模式现在也接受参考视频/参考音频
            # —— H3 参考生成节点的真实槽位上限：
            #   ref_images 0~9 / ref_videos 0~3 / ref_audios 0~3
            # 扩展前只接受 ref_image，一个纯视频参考会被统一拒掉，
            # 变成一个不存在的能力。
            _r2v_slots = {r.get("slot") for r in (asset_refs or [])
                          if isinstance(r, dict)}
            if not _r2v_slots & {"ref_image", "ref_video", "ref_audio"}:
                raise JobValidationError(
                    "参考模式需要至少 1 个参考素材（图片 / 视频 / 音频）")
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

    # 【2026-11-09 对齐第三方 · 音乐结构化豁免】
    # 面板新增了结构化风格（第三方 YuE2StylePrompt 的 12 字段），
    # **纯结构化也能出活** —— 所以 t2a / a2a 的 prompt 允许为空。
    # 真正判空在 music_yue2._resolve_style() 里做（结构化 + 自由文本都空才报错）。
    # ⚠️ 这段逻辑原在 api_create 里（live 侧）；合并到 prepare_job_params 后，
    #    内置 Agent 的 create_job 工具也走同一套 —— 否则 Agent 建音乐任务时
    #    「结构化风格齐全但 prompt 空」会被误拒（回归已上线的音乐能力）。
    if not (params.get("prompt") or "").strip():
        if mode in ("t2a", "a2a"):
            has_style_field = any(
                str(params.get(f"style_{f}") or "").strip()
                for f in ("language", "genre", "mood", "vocal", "vocal_color",
                          "instruments", "tempo_bpm", "energy", "length_hint", "extra")
            )
            if has_style_field:
                # 结构化风格齐全 —— 放行（prompt 留空是合法的）
                return asset_refs
            raise JobValidationError(
                "音乐风格描述不能为空：请填写自由文本风格描述，或填写结构化风格字段。",
                code="music_style_required")
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

    # 文生视频新面板：服务端校验并归一全部参数。
    # 这一段是**唯一**的参数入口 —— 前端传什么都不认，只认这里算出来的值。
    # 必须在 prepare_job_params **之前**跑：归一会改写 params，
    # 而 prepare 的占位/素材校验依赖归一后的宽高。
    if mode == "t2v":
        normalized, error = normalize_t2v_params(params)
        if error:
            return web.json_response({"error": error}, status=400)
        params = normalized

    try:
        asset_refs = prepare_job_params(str(mode or ""), params, asset_refs,
                                        saved_files)
    except JobValidationError as exc:
        payload = {"error": exc.message, "code": exc.code}
        payload.update(exc.extra or {})
        return web.json_response(payload, status=400)

    # ---- 接续（continuation）----
    #
    # 前端提交时带 `continuation` 字段（来自「接续上下文」回填）。
    # 服务端在这里**校验并补全**它：
    #   * 校验上游潜空间真的在（不在就明确报错，不静默退化成普通生成）；
    #   * 校验上游产物支持接续（不是二采）；
    #   * 算出本段的段号、链 id、累计帧数。
    #
    # 【为什么不信任前端传来的 chain_id / stage_index】前端回填的内容
    # 是**上一段**的链信息，本段是新的一段 —— 段号必须 +1、
    # 接力棒必须指向上一段。这两件事只能由服务端算。
    if mode == "t2v" and params.get("continuation"):
        cont_error = _prepare_continuation_params(params)
        if cont_error:
            return web.json_response({"error": cont_error}, status=400)


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
    # 【提示词历史】提交即留痕 —— 原始提示词 + 最终使用的提示词。
    # 放在任务真的建成之后：任务建成了，才算一次真实的提示词使用。
    #
    # 【为什么单独一个 db() 而不是并进 create_job_record】
    # create_job_record 是 Agent 的 create_job 工具与面板「生成」共用的
    # 唯一实现（见该函数的 docstring）。提示词历史是**面板侧**的需求 ——
    # Agent 工具链自己有自己的落点（见 h3studio/agent.py 的 record 钩子），
    # 所以这里在面板提交这一层单独记账，两条链路互不干扰。
    with db() as conn:
        ph_hooks.record_prompt_history(conn, mode, params, source=ph.SOURCE_GENERATE)
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


# ------------------------------------------------------------------ 生成队列
#
# 【为什么需要这一层 —— 2026-11-13 生成队列化需求】
#
# 后端**本来就有**真正的队列：建任务时 status='queued'，Worker 单协程
# 循环按 created_at 顺序取一个跑完再取下一个（Worker._loop / _next_queued）。
# 缺的不是「串行执行」，而是**队列的可观测性**：
#   * 前端不知道某个排队任务排在第几位 -> 按钮只能显示「生成中」，
#     用户以为界面卡住了，就不敢再提交第二个；
#   * 前端不知道队列里一共有多少个 -> 无法显示「已入队（队列 N）」。
#
# 所以这里补的是**只读的队列视图**，不改动 Worker 的调度语义
# （不改「一次只跑一个」「按 created_at FIFO」这两条既有行为）。

#: 队列视图里最多返回多少条任务。
#: 队列是用户自己排的，条数有限；上限防止异常情况下一次拉太多。
QUEUE_VIEW_LIMIT = 50


def queue_snapshot(recent_limit: int = 12) -> dict:
    """
    生成队列的**只读快照** —— 队列顺序 + 每条任务的状态 + 待处理计数。

    返回形状（前端 /api/queue 直接消费）：
        {
          "running": "任务 id" 或 None,      # 正在跑的那个
          "running_mode": "t2v" 或 "",        # 正在跑的任务的模式
          "pending": 3,                       # 排队中（还没开始跑）的条数
          "total": 4,                         # 排队中 + 正在跑
          "items": [                          # 按执行顺序（队首在前）
            {"id": "...", "mode": "t2v", "status": "running"|"queued",
             "position": 1,                   # 1 = 下一个就跑；排队中的从 1 开始
             "created_at": 1234.5,
             "label": "文生视频",             # 生成类型标签
             "prompt": "……"},                 # 提示词摘要（前端列表用）
          ],
          "recent": [                         # 刚跑完的几条（成功/失败/取消）
            {"id": "...", "mode": "t2v", "status": "done"|"failed"|"canceled",
             "created_at": ..., "label": "文生视频", "prompt": "……",
             "error": "……"},
          ]
        }

    【为什么 position 从 1 开始且包含正在跑的那条】
    正在跑的那条排在最前面（position=1），排队中的依次是 2、3……
    这样前端可以直接把 position 显示成「第 N 位」，用户一眼看出
    「我刚才提交的那个排在第几」。running 的那条单独用 status 区分，
    前端据此显示「运行中」而不是「排队中」。

    【为什么只算 mode != 'longfilm'】
    longfilm 走自己的 asyncio.create_task + LONGFILM_LOCK 通道，
    不经过 Worker。它塞进同一个队列视图会给出「排在第 N 位但永远轮不到」
    的假信息 —— 所以排除掉，在 longfilm 卡片上另有提示。

    【为什么 recent 只取刚跑完的有限条】
    队列面板要能看到「成功 / 失败」，但不能把整个任务库都拉出来
    （生成库里可能有几百条历史）。取最近 N 条终态任务即可。
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT id, mode, params, status, created_at FROM jobs "
            "WHERE status IN ('queued','running') AND mode!='longfilm' "
            "ORDER BY created_at ASC").fetchall()
    items = []
    running_id = None
    running_mode = ""
    pending = 0
    for index, row in enumerate(rows, start=1):
        job_params = json.loads(row["params"] or "{}")
        status = row["status"]
        if status == "running":
            running_id = row["id"]
            running_mode = row["mode"] or ""
        else:
            pending += 1
        items.append({
            "id": row["id"],
            "mode": row["mode"],
            "status": status,
            "position": index,
            "created_at": row["created_at"],
            "label": gen_types.type_for_mode(row["mode"])["label"],
            "prompt": str(job_params.get("prompt") or "")[:120],
        })
    recent = []
    if recent_limit > 0:
        with db() as conn:
            recent_rows = conn.execute(
                "SELECT id, mode, params, status, created_at, error FROM jobs "
                "WHERE status IN ('done','failed','canceled') AND mode!='longfilm' "
                "ORDER BY created_at DESC LIMIT ?", (recent_limit,)).fetchall()
        for row in recent_rows:
            job_params = json.loads(row["params"] or "{}")
            recent.append({
                "id": row["id"],
                "mode": row["mode"],
                "status": row["status"],
                "created_at": row["created_at"],
                "label": gen_types.type_for_mode(row["mode"])["label"],
                "prompt": str(job_params.get("prompt") or "")[:120],
                "error": row["error"] or "",
            })
    return {
        "running": running_id,
        "running_mode": running_mode,
        "pending": pending,
        "total": len(items),
        "items": items,
        "recent": recent,
    }


async def api_queue(request):
    """GET /api/queue —— 生成队列快照（顺序 / 状态 / 计数）。

    前端轮询它来渲染队列面板与「已入队（队列 N）」按钮文案。
    纯只读：不建任务、不改状态。
    """
    snapshot = queue_snapshot()
    items = snapshot["items"]
    if len(items) > QUEUE_VIEW_LIMIT:
        snapshot["items"] = items[:QUEUE_VIEW_LIMIT]
    return json_ok(snapshot)


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


# ------------------------------------------------------------------
# 接续生成（continuation）
# ------------------------------------------------------------------
async def api_continuation_context(request):
    """GET /api/jobs/{id}/continuation —— 取「接续上下文」（回填用）。

    用户在生成库点「接续」-> 前端调这个接口 -> 服务端把该任务的
    全部配置/参数/模型/素材（**提示词除外**）组装好下发 ->
    前端回填到左侧配置区。

    返回 ok:false 的情况有两种：
      * 该产物不支持接续（二采 / 非视频 / 老任务）；
      * 潜空间文件已不在。
    两种都**明确说明原因** —— 不静默失败，也不让用户点了才知道。
    """
    jid = request.match_info["id"]
    with db() as conn:
        r = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not r:
        return web.json_response({"ok": False, "error": "找不到这个任务。"}, status=404)
    mode = r["mode"]
    params = json.loads(r["params"] or "{}")
    context = cont_service.build_continuation_context(mode, params, jid)
    # 附带来源信息（前端要显示「接续自哪条产物」）
    outputs = json.loads(r["outputs"] or "[]")
    video = next((o for o in outputs if o.get("kind") == "video"), None)
    context["source"] = {
        "job_id": jid,
        "mode": mode,
        "created_at": r["created_at"],
        "video_file": (video or {}).get("file", ""),
        "video_name": (video or {}).get("name", ""),
        "prompt_preview": str(params.get("prompt") or "")[:80],
    }
    return json_ok(context)


async def api_continuation_state(request):
    """GET /api/continuation/state —— 接续功能是否可用（本机能力探测）。

    前端用它决定「接续」入口要不要出现：
      * Relay-Kit 节点没装 -> 整个功能不可用，入口隐藏；
      * 节点在 -> 入口可用；具体某个产物能不能接续看卡片上的
        continuation_info（服务端逐任务算好）。

    【为什么要先刷新节点清单】节点清单是**懒加载**的（只有
    /api/video-options 被调过才有值）。这里主动刷一次，
    否则刚启动时前端会拿到空清单 -> 误判「功能不可用」。

    【⚠ 「探测不完整」不等于「节点缺失」—— 2026-11-13 修】
    ComfyUI 被生成任务占满时，`/object_info/<节点>` 会**超时**。
    超时说明「**没问到**」，不说明「**确实不在**」。
    旧实现把两者都当成「不在」-> 明明节点都在，却报
    「缺节点，接续功能不可用」-> 整个接续功能**在用户眼前消失**
    （实测：全量回归期间并行跑 GPU 生成时必现，单独跑又通过）。

    现在分三态：
      * 节点确实不在 -> available=false + 明确列出缺哪些；
      * 探测**没问全**（超时/异常）-> available=**true**（不阻断
        功能）+ `probe_complete=false` + 说明「本机状态未探全」。
      * 全部问到 -> available 按实际。
    """
    await refresh_comfy_node_names(force=True)
    relay_nodes = ("H3RelayLatentSave", "H3RelayLatentLoad",
                   "H3RelayCopyBridge", "H3RelayTrimAV")
    have = {name: name in _COMFY_NODE_NAMES for name in relay_nodes}
    # 「探测问全了」= 每个节点都问到了 -> 答案可信，可以断言「缺节点」。
    probe_complete = _COMFY_PROBE_COMPLETE
    available = all(have.values())
    reason = ""
    if not available:
        missing = [name for name, ok in have.items() if not ok]
        if not probe_complete:
            # 没问全 -> **不能**断言「缺节点」。
            available = True
            reason = ("本机 ComfyUI 暂时无响应，接续节点状态未探全 —— "
                      "接续功能按可用处理。若确实未安装 Relay-Kit，"
                      "点接续时会给出明确报错。")
        else:
            reason = ("本机 ComfyUI 里缺少接续所需的节点：" + "、".join(missing)
                      + "（ComfyUI-H3-Relay-Kit）—— 接续功能不可用。")
    return json_ok({"available": available, "nodes": have, "reason": reason,
                    "probe_complete": probe_complete})


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


# ------------------------------------------------------------------ ComfyUI 节点名探测
#
# 文生视频面板的「注意力机制」「二采」都是 ComfyUI 侧的节点。
# 用户能选哪些，取决于**本机 ComfyUI 里真有没有那个节点** ——
# 所以要按需问 ComfyUI，拿到节点名缓存下来。
#
# 【为什么按单个节点问，而不是拉整个 /object_info】
#   全量 /object_info 本机实测 26.9 MB / 4.9 秒（7027 个节点）。
#   而 ComfyUI 支持按节点名问：`/object_info/<ClassName>` —— 实测 1.2 KB / 1 ms。
#   面板只关心 6 个节点，按名单问就是 6 次 1 ms。
_COMFY_NODE_NAMES: set[str] = set()
_COMFY_NODES_TS = 0.0
#: 上一次探测**是否问全**（True = 每个节点都问到了，答案可信）。
#:
#: 【为什么需要它】ComfyUI 被生成任务占满时 `/object_info/<节点>` 会超时。
#: 超时 = 「**没问到**」，不等于「**确实不在**」。
#: 没有这个标志的话，能力探测会把「ComfyUI 忙」误判成「Relay-Kit 没装」
#: -> 整个接续功能在用户眼前消失（且不报错）。
#: 详见 refresh_comfy_node_names 与 api_continuation_state。
_COMFY_PROBE_COMPLETE = False

# 面板关心的节点名（video_models.VIDEO_ATTENTION_MODES 里用到的 +
# SelfLift 二采）。加新注意力机制时同步加到这里。
_VIDEO_PANEL_NODES = [
    "ModelAttentionBackend",
    "MiniMaxH3MemoryEfficientSageAttentionPatch",
    "MiniMaxH3MemoryEfficientSolAttentionPatch",
    # 【2026-11-12 移除】MiniMaxH3ScheduledSolAttentionPatch ——
    # 对应的「Sol-Attn 渐进调度」选项已下线（用户实测画面有叠影）。
    # 节点本机仍在，但从名单里移除后不再被面板探测（省一次 HTTP）。
    "H3SparseAttention",
    "SelfLiftH3Sampler",
    # 【2026-11-12 修】MiniMaxH3SigmaShift —— shift（视频/音频）参数对应的
    # ComfyUI **核心**节点（comfy_extras/nodes_minimax_h3.py:367，
    # display_name「ModelSamplingMiniMaxH3」）。
    #
    # 【为什么以前漏了】这个名单是给「面板可选项过滤」用的，
    # 只列了注意力机制 + 二采。shift 节点当时还没做，就没进名单。
    # 结果服务端归一里的
    #   use_sigma_shift = "MiniMaxH3SigmaShift" in _COMFY_NODE_NAMES
# 永远为 False -> shift 节点永远不进工作流 -> 面板上的两个 shift 滑杆
    # 是**死控件**（能拖、但不生效）。这正是「假控件」纪律要禁止的。
    "MiniMaxH3SigmaShift",
    # 【2026-11-12 新增】接续生成所需的 Relay-Kit 节点。
    # 这四个节点是**接续功能的硬依赖**（读回潜空间 / 拷贝桥 / 裁重叠 / 落盘）。
    # 放进这份名单 -> /api/continuation/state 能准确告诉前端
    # 「本机具备接续能力」还是「缺节点，功能不可用」。
    # 放在这里而不是另建一份名单：这份名单已经有并发探测 + 60 秒缓存
    # + 失败不拖慢的机制，另建一份会重复那套逻辑。
    "H3RelayLatentSave",
    "H3RelayLatentLoad",
    "H3RelayCopyBridge",
    "H3RelayTrimAV",
    # 【2026-11-13 新增 —— 借鉴 nonebot H3 工作流的 sigma 精修节点】
    # H3SigmaRefiner（/object_info/H3SigmaRefiner -> 200，本机已装）：
    # 在 sigma 表的低噪段局部加密采样步。
    # 不在名单里 -> 面板不探测它 -> 服务端归一里它恒为不可用 ->
    # 面板的对应控件不会出现（避免「假控件」）。
    "H3SigmaRefiner",
]


async def refresh_comfy_node_names(session=None, force: bool = False) -> set[str]:
    """确认面板关心的那些节点在本机 ComfyUI 里是否存在。

    按单个节点问（实测 1 ms/个），不用拉 27 MB 的全量 /object_info。

    【2026-11-11 修复 —— 不能让面板被 ComfyUI 拖死】
    原实现是**串行**问 6 个节点，每个 10 秒超时 —— ComfyUI 卡住/正在启动时
    （实测本机 21:01/21:05/21:06 连续出现 object_info timed out），
    这个函数要 **60 秒**才返回，导致 GET /api/video-options 挂 66 秒，
    浏览器先放弃 -> 文生视频面板**整块空白**（用户 21:06 实报）。

    现在：
      * 6 个节点**并发**问 -> 总耗时 = 最慢的那个（<= 单个超时）
      * 单个超时从 10 秒压到 **3 秒** -> 最坏 3 秒返回
      * 拿不到就返回**上一次成功的缓存**（或空集），不抛、不拖慢
    """
    global _COMFY_NODE_NAMES, _COMFY_NODES_TS
    now = time.time()
    if not force and _COMFY_NODE_NAMES and now - _COMFY_NODES_TS < 60:
        return _COMFY_NODE_NAMES

    async def _probe(s, name):
        """问一次节点在不在。

        返回三态而不是布尔 —— **「问不到」和「确实不在」是两回事**：
          * name      —— 问到了，且这个节点在；
          * None      —— 问到了，HTTP 200 但响应里没这个节点 -> 确实不在；
          * "TIMEOUT" —— **没问到**（超时/连接错/响应坏）。

        把「没问到」当成「不在」会让能力探测在 ComfyUI 忙的时候
        假阴性 —— 明明节点都在，却报「缺节点，接续功能不可用」。
        （实测：ComfyUI 被生成任务占满时，3 秒超时打满 -> 探测全空。）
        """
        try:
            async with s.get(f"{COMFY}/object_info/{name}") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, dict) and name in data:
                        return name
                    return None          # 问到了 -> 确实不在
                # 非 200：算「没问到」（服务异常），不当成「不在」
                return "TIMEOUT"
        except Exception:
            return "TIMEOUT"
        return "TIMEOUT"

    found: set[str] = set()
    unknown = False
    asked = 0
    try:
        timeout = ClientTimeout(total=3)
        if session is None:
            async with ClientSession(timeout=timeout) as s:
                names = await asyncio.gather(*[_probe(s, n)
                                                for n in _VIDEO_PANEL_NODES])
        else:
            names = await asyncio.gather(*[_probe(session, n)
                                            for n in _VIDEO_PANEL_NODES])
        found = {n for n in names if n and n != "TIMEOUT"}
        unknown = any(n == "TIMEOUT" for n in names)
        # 「问到了」的个数（不管在不在）—— 与 found 分开：
        #   found  = 「在」的节点
        #   asked  = 「问到了」的节点（在 + 确实不在）
        asked = sum(1 for n in names if n != "TIMEOUT")
    except Exception:
        found = set()
        unknown = True
        asked = 0

    # 【本轮修】记录「本次探测是否问全」——
    # 这是区分「节点确实不在」与「没问到」的唯一依据。
    # 只有「问全了」才允许据此断言「缺节点」。
    global _COMFY_PROBE_COMPLETE
    _COMFY_PROBE_COMPLETE = (not unknown) and asked >= len(_VIDEO_PANEL_NODES)

    if found:
        # 只有真问到东西才更新缓存 —— 全挂时保留上一次的（可能还有效）
        _COMFY_NODE_NAMES = found
        _COMFY_NODES_TS = now
    elif not _COMFY_NODE_NAMES:
        # 从来没成功过：记时间戳，避免每个请求都去撞一遍 3 秒超时
        _COMFY_NODES_TS = now
    # 【本轮修】探测**不完整**（部分节点没问到）时，**不要**用残缺
    # 的结果去断言「节点缺失」—— 那是假阴性。
    #   做法：本次不更新缓存（保留上一次的完整结果），并把
    #   「本次探测不完整」标记下来，让调用方知道「没问全」。
    elif unknown:
        # 保留 _COMFY_NODE_NAMES 原样（可能是空集 = 从没成功过）
        pass
    return _COMFY_NODE_NAMES


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
    # 【2026-11-10 修复】用异步版 —— 同步版内部会阻塞事件循环
    # （yue2_schema._fetch_enums 是同步 urllib，timeout 10s）。
    # 在事件循环里直接调会让**整个 server** 冻结 10 秒：
    # 前端首屏请求 /api/music-options，于是页面打不开。
    return json_ok(await music_yue2.model_options_async())


# ------------------------------------------------------------------ YuE2 音乐增强
async def api_music_enhance(request):
    """
    POST /api/music-enhance —— YuE2 音乐专用增强（结构化风格 + 歌词）

    【链路对应】
    移植自 nonebot 插件 `nonebot_plugin_novelai` 的咏唱「文生音乐」实现：
      * 系统提示词由 h3studio/yue2_schema.py 的 build_system_prompt() 渲染
        （枚举从 ComfyUI /object_info 运行时拉，永不二次手抄）
      * 模型产出的 JSON 由同模块的 normalize_and_validate() 归一化 + 校验
        （拦脏 COMBO 值、补齐缺字段、纯器乐时强制 vocal_color=(none)）
      * 归一化后的结构化字段由 h3studio/music_style.py 的 build_style_prompt()
        合成最终送进 YuE2 节点的 style 串（与官方节点逐条对齐）

    body:
      prompt      用户的一句话描述（必填，除非 base 提供了现有内容）
      mode        t2a（文生音乐）/ a2a（参考生音乐）—— 只影响措辞
      base        可选：以当前内容为底稿做「修改」（而非从零生成）
      style       可选：当前的结构化风格字段（用户编辑过的那份）
      lyrics      可选：当前的歌词正文（用户编辑过的那份）
      provider_id 可选，指定 Provider；不给则用默认 Provider

    返回:
      style       归一化后的结构化风格字段（12 个字段）
      lyrics      归一化后的结构化歌词字段（template + lyrics_override）
      style_text  按官方规则合成的 style 串（预览用）
      lyrics_text 合成后的歌词文本（预览用）
      fixes       自动修复清单（界面上可展示，让用户知道模型被纠正过什么）
      problems    遗留质量问题（不阻断，只提示）
    """
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误"}, status=400)

    original = str(payload.get("prompt") or "").strip()
    base = str(payload.get("base") or "").strip()
    if not original and not base:
        return web.json_response({"error": "请先输入要增强的描述"}, status=400)
    if len(original) > 4000:
        return web.json_response({"error": "描述过长（上限 4000 字）"}, status=400)

    mode = str(payload.get("mode") or "t2a").strip()
    if mode not in ("t2a", "a2a"):
        mode = "t2a"

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

    # 【2026-11-10 修复】先在**线程池**里把枚举取一次（见 yue2_schema 的说明），
    # 再把它同时喂给 build_system_prompt() 和 normalize_and_validate()。
    # 原来两处各自调 get_enums()，各自**同步阻塞**事件循环最长 10 秒；
    # 在 aiohttp 里这么干 = 整个 server 冻结，其它请求全部排队。
    enums = await yue2_schema.get_enums_async()
    system_prompt = yue2_schema.build_system_prompt(enums)
    user_prompt = _music_enhance_user_prompt(original, base, mode,
                                             payload.get("style"),
                                             payload.get("lyrics"))
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    try:
        raw = await llm_client.call_provider(
            item, messages,
            options={"temperature": 0.7, "top_p": 0.95,
                     "max_tokens": 4000, "do_sample": True})
    except llm_client.ProviderError as exc:
        return web.json_response(
            {"error": str(exc), "provider": item["name"],
             "provider_id": item["id"], "degraded": True}, status=502)

    raw = yue2_schema.strip_code_fence(raw)
    parsed = _parse_music_json(raw)
    if not parsed:
        return web.json_response(
            {"error": "模型返回的内容不是有效的 JSON 配置文件，未做修改。",
             "provider": item["name"], "provider_id": item["id"],
             "raw_preview": raw[:400], "degraded": True}, status=502)

    ok, normalized, fixes, problems = yue2_schema.normalize_and_validate(
        parsed, enums)
    if not ok or not normalized:
        return web.json_response(
            {"error": "模型产出无法归一化成合法配置（" +
                      "；".join(problems or ["结构不可用"]) + "）",
             "provider": item["name"], "provider_id": item["id"],
             "degraded": True}, status=502)

    style_struct = normalized[yue2_schema.STYLE_KEY]
    lyrics_struct = normalized[yue2_schema.LYRICS_KEY]
    # 最终 style 串用 music_style.build_style_prompt（与官方节点逐条对齐）
    style_text = music_style.build_style_prompt(
        style_struct.get("language", ""), style_struct.get("genre", ""),
        style_struct.get("mood", ""), style_struct.get("vocal", ""),
        style_struct.get("vocal_color", ""), style_struct.get("instruments", ""),
        style_struct.get("tempo_bpm", 0), style_struct.get("energy", ""),
        style_struct.get("length_hint", ""), style_struct.get("extra", ""),
        style_struct.get("custom_language", ""), style_struct.get("custom_genre", ""),
    )
    lyrics_text = music_style.resolve_lyrics(
        lyrics_struct.get("lyrics_override", ""),
        lyrics_struct.get("template") or "V-C-V-C-bridge-C")
    # 【提示词历史】音乐的结构化增强产物也进历史。
    # 音乐面板的「最终提示词」= 合成后的 style 串（真正送进 YuE2 的那段），
    # 原始 = 用户写的那句描述。
    with db() as conn:
        ph_hooks.record_prompt_history(
            conn, mode,
            {"original_prompt": original or base, "prompt": style_text},
            source=ph.SOURCE_ENHANCE)
    return json_ok({
        "style": style_struct,
        "lyrics": lyrics_struct,
        "style_text": style_text,
        "lyrics_text": lyrics_text,
        "fixes": fixes,
        "problems": problems,
        "provider": item["name"],
        "provider_id": item["id"],
        "mode": mode,
    })


def _music_enhance_user_prompt(original: str, base: str, mode: str,
                               style, lyrics) -> str:
    """
    组装给模型的用户消息。

    【两种语义】
      * base 为空 -> 从零生成（用户给一句描述，要结构化风格 + 歌词）
      * base 非空 -> 以当前内容为底稿做修改（用户已经有一版，想改）
    """
    parts = []
    if mode == "a2a":
        parts.append(
            "【场景】这是「参考生音乐」（cover）模式：用户会额外提供一段参考音频，"
            "音频只提供旋律线，编曲与和声由你写的风格与歌词决定。"
            "所以风格里的 instruments / extra 要写清楚编曲意图。")
    if base:
        parts.append("【任务】在下面这份**现有配置**的基础上修改，"
                     "按用户的修改意图调整相应字段；没提到的字段尽量保持原样。")
        if isinstance(style, dict) and style:
            parts.append("现有 style 字段：\n" +
                         json.dumps(style, ensure_ascii=False, indent=2))
        if isinstance(lyrics, str) and lyrics.strip():
            parts.append("现有歌词正文：\n" + lyrics.strip())
        parts.append("【修改意图】\n" + (original or base))
    else:
        parts.append("【任务】根据用户描述，从零产出结构化风格与歌词。")
        parts.append("【用户描述】\n" + original)
    parts.append("只输出 JSON 对象，不要任何解释文字。")
    return "\n\n".join(parts)


def _parse_music_json(text: str):
    """
    宽容地解析模型产出的 JSON。

    移植自 nonebot 插件 h3_yue2.py 的 _parse_yue2_json_robust()：
      先用 raw_decode（能吃住尾随内容），再截取最外层花括号重试。
    """
    text = (text or "").strip()
    if not text:
        return None
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    if "{" in text and "}" in text:
        candidate = text[text.index("{"):text.rindex("}") + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            try:
                obj, _ = decoder.raw_decode(candidate)
                return obj if isinstance(obj, dict) else None
            except Exception:
                pass
    return None


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


async def api_video_options(request):
    """GET /api/video-options —— 文生视频面板的可选项清单

    清单全部来自服务端实测（h3studio/video_models.py）：
      * 可用底模（**以 ComfyUI 的模型列表 API 为准** —— 见下）
      * 固定加速 LoRA（工作流硬性必需，不进可勾选列表）
      * 可勾选的加速 LoRA 组合（每项带用途与推荐强度）
      * 注意力机制（只列本机 ComfyUI 里真有的节点）
      * 二采（SelfLift）参数与硬约束
      * 宽高吸附规则（倍数 / 上下限）

    【2026-11-11 改造】底模的「能不能选」不再靠写死的常量，也不只靠
    扫盘 —— 现在**问 ComfyUI 本人**：拉 `/models/diffusion_models` 与
    `UNETLoader.unet_name` 枚举（两者交叉验证），再决定每条模型的状态
    （available / external_missing / missing / unknown）。
    ComfyUI 不可达时降级到扫盘，并在 payload 里标 model_list_source
    与 model_list_error —— 如实说明来源，不谎称「获取不到模型」。
    """
    # 本机 ComfyUI 里真有哪些节点 —— 决定「注意力机制」「二采」能不能选
    await refresh_comfy_node_names()

    def node_available(class_type: str) -> bool:
        return class_type in _COMFY_NODE_NAMES

    # 模型清单快照（带 20 秒缓存）：一次拉，供全部分析共用
    # 用**异步**版 —— ComfyUI 首次列目录实测要 43 秒，
    # 同步调用会把整个事件循环卡住（状态流/其它 API 全瘫）。
    snapshot = await video_models.refresh_comfy_snapshot_async()
    payload = video_models.options_payload(node_available=node_available,
                                           snapshot=snapshot)
    # 附带「当前底模下每个加速 LoRA 的实测兼容性」——
    # 前端据此把对不上的组合从**下拉里剔除**（不是置灰）。
    #
    # 【2026-11-11 改】加速 LoRA 改成单选：一个主模型只能挑一个加速 LoRA。
    #   互斥单选的语义：两枚 4 步蒸馏 LoRA 叠在一起没有意义
    #   （都是把 20 步压到 4 步的蒸馏权重，叠加只会互相打架），
    #   而「固定 Turbo LoRA」已经在工作流里常驻，用户这一栏选的是
    #   **额外**的那一枚。
    model_key = request.query.get("model") or payload["default_model"]
    model = video_models.VIDEO_MODELS.get(model_key)
    payload["selected_model"] = model_key
    if model:
        matrix = video_models.accel_lora_compat_matrix(model_key)
        combos = []
        for item in payload["accel_lora_combos"]:
            entry = dict(item)
            check = matrix.get(item["key"], {})
            entry["compatible"] = bool(check.get("compatible"))
            entry["compat_reason"] = check.get("reason", "")
            entry["compat_matched"] = check.get("matched", 0)
            entry["compat_runtime_injected"] = check.get("runtime_injected", 0)
            entry["compat_total"] = check.get("total", 0)
            # 不适配当前底模的**不出现**在下拉里（前端据此过滤）
            entry["selectable"] = bool(entry["compatible"]) and \
                entry.get("available", True)
            combos.append(entry)
        payload["accel_lora_combos"] = combos

    # 【2026-11-13 新增】作者推荐参数（需求 1 / 2 / 5）
    #
    # 每一条加速 LoRA / 主模型都带作者自己写的推荐参数：
    #   步数 / 调度器 / 采样器 / 视频 shift / 音频 shift / 推荐主模型
    # 值来自 LoRA 作者的说明（safetensors metadata / 目录 README /
    # 官方示例工作流）或 ComfyUI 的模型硬性值，**每一项都带 source**。
    # 作者没写的字段 = "not_provided" -> 前端显示「作者未提供」。
    payload["accel_lora_recommendations"] = \
        model_recs.accel_lora_recommendations_payload()
    payload["model_recommendations"] = \
        model_recs.model_recommendations_payload()
    payload["rec_source_kind_label"] = dict(model_recs.SOURCE_KIND_LABEL)
    payload["rec_field_label"] = dict(model_recs.REC_FIELD_LABEL)
    return json_ok(payload)


async def api_video_config(request):
    """GET /api/video-config —— 读回文生视频面板保存的配置（局域网共用）

    PUT /api/video-config —— 保存文生视频面板配置
    """
    key = "video_panel_config"
    if request.method == "GET":
        with db() as conn:
            row = conn.execute("SELECT value FROM model_provider_prefs WHERE key=?",
                              (key,)).fetchone()
        payload = {}
        if row and row["value"]:
            try:
                payload = json.loads(row["value"] or "{}")
            except Exception:
                payload = {}
        return json_ok(payload)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "配置必须是对象"}, status=400)
    with db() as conn:
        prov.set_pref(conn, key, json.dumps(body, ensure_ascii=False))
    return json_ok({"ok": True, "config": body})


async def api_video_size(request):
    """GET /api/video-size?width=&height= —— 宽高吸附（服务端权威）

    返回生效宽高 + 调整原因，前端原样显示「输入值 → 实际使用值」。

    【2026-11-11 放宽】文生视频的上限从 768*1344（0.98 MP）放宽到
    2.0 MP —— 官方模板 Note 的像素表明确列到 2.0 MP -> 1920x1088，
    而旧上限来自 ComfyUI 节点里**只管参考视频适配**的 MAX_PIXELS 常量，
    被误当成生成分辨率上限了。

    `mode` 参数决定用哪套约束（需求：各模式按各自工作流的真实约束
    分别处理，不要一刀切套用 2.0M）：
      * mode=t2v（默认）-> 2.0M 上限
      * mode=flf / r2v  -> 仍走 ResolutionSelector 的 0.8 MP 档位上限
      * mode=longfilm   -> 不限（拼片不产生新画面）
    """
    try:
        width = request.query.get("width")
        height = request.query.get("height")
        mode = request.query.get("mode") or "t2v"
        max_pixels = video_models.max_pixels_for_mode(mode)
        w, h, reasons = video_models.normalize_video_size(
            width, height, max_pixels=max_pixels)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=400)
    return json_ok({
        "width": w, "height": h, "reasons": reasons,
        "input_width": width, "input_height": height,
        "mode": mode,
        "max_pixels": (int(max_pixels * 1024 * 1024)
                       if max_pixels else None),
        "max_pixels_mp": max_pixels,
    })


def _video_model_available(model: dict) -> bool:
    """某个视频模型项在本机是否真的可用。

    t2v / flf / r2v 都要 H3 底模权重落盘；longfilm 只拼已完成的视频，
    不需要 H3 权重。

    返回 False 时前端会把该模型项置为不可选（并在类别无任何可用模型时
    禁用整个类别 + 提示）—— 这正是需求的回退规则③。
    """
    mode = (model.get("mode") or "").strip()
    if mode == "longfilm":
        return True
    try:
        from h3studio import workflows as _wf
        if mode not in _wf.BUILDERS:
            return False
    except Exception:
        return False
    # H3 底模：任一登记的权重在本机就算可用
    try:
        for key in video_models.VIDEO_MODELS:
            if video_models.model_available(key):
                return True
    except Exception:
        return False
    return False


async def api_video_categories(request):
    """GET /api/video-categories —— 左侧导航的**两级**视频清单

    两级结构（需求）：
      1) 第一级「视频类别」：固定四项，顺序与文案不可变 ——
         文生视频 / 首尾帧生视频 / 参考生视频 / 长片拼接
      2) 第二级：该类别下的具体模型项

    【单一真源】这份清单完全来自 h3studio/video_categories.py，
    前端不自己维护第二份 mode/model 映射表。

    【向后兼容】老 URL / 老参数 / 老任务记录里的 mode（t2v / flf /
    r2v / longfilm）**一个字都没改**，类别只是给它们套了一层分组视图。
    `legacy_modes` 字段给出「mode -> 类别」的桥，老链接照样能定位面板。
    """
    categories = video_categories.list_categories()
    models = video_categories.list_models()

    # 【可用性位】让前端的「回退规则③ 禁用并提示」不是死代码 ——
    # 每个模型项都要真的带上 available，前端才知道哪个不可选。
    #   * t2v / flf / r2v：依赖 H3 底模权重在本机（实测 check）
    #   * longfilm：拼片链路不依赖 H3 权重（它只拼已完成的视频）
    for model in models:
        model["available"] = _video_model_available(model)
    for cat in categories:
        for model in cat["models"]:
            model["available"] = _video_model_available(model)

    return json_ok({
        "categories": categories,
        "models": models,
        # mode -> 类别 key（老 mode 的桥）
        "legacy_modes": dict(video_categories.MODE_TO_CATEGORY),
        # 老 mode 的旧显示名（历史任务卡片回显用）
        "legacy_labels": dict(video_categories.LEGACY_MODE_LABELS),
        # 默认类别 / 默认模型项（前端首屏默认选中用）
        "default_category": (categories[0]["key"] if categories else ""),
        "default_model": (categories[0].get("default_model", "")
                          if categories else ""),
    })


async def api_generation_types(request):
    """GET /api/generation-types —— 右侧任务「按生成类型」标签的清单

    清单来自后端真实注册的生成模式（workflows.BUILDERS + longfilm），
    前端不硬编码任何类型名。

    【每个类型额外带 agent_asset_slots —— 2026-11-10 审查修复】
    前端原来**硬编码**「Agent 建不了 a2a / flf」（因为旧基线的 Agent
    素材槽位恒为 ref_image）。但后端增强分支已经把槽位改成分派制
    （agent_schema.MODE_SLOTS / mode_slot_plan）——
    合并后前端那条提示变成**反向阻断**（后端能建，前端说不能）。
    现在把真实槽位计划下发，前端只渲染服务端给的结论。
    """
    types = gen_types.list_types()
    for item in types:
        key = item.get("key")
        if not key or key == "__other__":
            continue
        try:
            plan = agent_mod.agent_schema.mode_slot_plan(key)
        except Exception:
            continue
        if plan.get("single") or plan.get("multi"):
            item["agent_asset_slots"] = {
                "single": plan["single"],
                "multi": plan["multi"],
                "max_per_slot": plan["max_per_slot"],
                "source": "agent_schema.MODE_SLOTS",
            }
    return json_ok({
        "types": types,
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
#
# 【2026-11-10 单一权威定义】ENHANCE_CONCURRENCY 与 ENHANCE_GATE 已上移到
# 本文件的「增强并发闸门」小节（唯一权威定义处）。
#
# 之前这里与上方**各有一份** ENHANCE_CONCURRENCY（enh-unify=2 / max-token=3），
# 后定义的会静默覆盖先定义的 —— 上限到底是 2 还是 3 取决于文件顺序，
# 两份注释还各说各话。这是本轮审查 P1-2 的根因。
# 现在：全局只有一处定义，取值与理由见上方小节。




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
        # 输出 token 上限的边界（前端输入框用同一套边界）
        "max_tokens_bounds": {
            "default": enh.MAX_TOKENS_CEILING,
            "min": enh.MAX_TOKENS_FLOOR,
            "max": enh.MAX_TOKENS_MAX,
        },
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
        # 「二次增强」是通用能力，但按媒体类型用不同模板 ——
        # 前端不再硬编码模板 id（否则音乐模式会拿到画面物体向的模板）。
        "refine_spec_by_media": dict(enh.REFINE_SPEC_BY_MEDIA),
        # 模式 -> 「二次增强」模板 id（前端直接取，不用自己推）
        "refine_specs": {m: enh.refine_spec_for_mode(m) for m in enh.MODE_MEDIA},
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
            # max_tokens_ceiling 是「输出 token 上限天花板」——
            # 必须在夹取之前先归一到合法区间，否则前端传个 999999
            # 会被 clamp 到 MAX_TOKENS_MAX，传 0 会被 clamp 到 MIN。
            ceiling = saved["params"].get("max_tokens_ceiling")
            try:
                ceiling = int(ceiling) if ceiling is not None else enh.MAX_TOKENS_CEILING
            except (TypeError, ValueError):
                ceiling = enh.MAX_TOKENS_CEILING
            saved["params"]["max_tokens_ceiling"] = max(
                enh.MAX_TOKENS_FLOOR, min(ceiling, enh.MAX_TOKENS_MAX))
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
    # 【审查返修】预算按**原文长度**自适应（不再由固定 300/512 决定）。
    #   原实现不传 original -> 只能拿到固定 300 -> 长提示词必被硬切
    #   （审查实测：普通增强 10 次有 6 次被切，成功率仅 40%）。
    params = enh.resolve_params(payload.get("params"), saved, spec, original=original)

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


    validate_mode = spec.get("validate_mode", "pe")
    messages = enh.build_messages(spec, original, params.get("language", "auto"))

    # 并发闸门：抢不到就立刻 429，**不排队**。
    # 理由见 ENHANCE_CONCURRENCY 上方的注释（本机 llama.cpp total_slots=1，
    # 排队只会让所有请求一起等到超时）。
    gate = _enhance_gate()
    if gate.locked():
        return web.json_response(
            {"error": f"当前已有 {ENHANCE_CONCURRENCY} 个提示词增强任务在运行，"
                      "本地模型一次只能处理这么多，请稍候再试。",
             "code": "too_many_requests",
             "concurrency": ENHANCE_CONCURRENCY}, status=429)

    async with gate:
        # 【审查返修】截断自动降级重试。
        #   第一次按自适应预算跑；若仍被硬切（finish_reason=length），
        #   自动用**满上限**再跑一次，而不是直接 502 让用户失败。
        #   （审查实测：普通增强 10 次有 6 次被 300 硬切，成功率仅 40%。
        #    预算自适应后这一步基本不会触发；这里是最后一道保险。）
        budget = params["max_tokens"]
        ceiling = enh.clamp_max_tokens(enh.MAX_TOKENS_CEILING, params)
        attempts = [budget]
        if ceiling > budget:
            attempts.append(ceiling)

        last = None
        for attempt_idx, mt in enumerate(attempts):
            try:
                res = await llm_client.call_provider_full(
                    item, messages, images=images or None,
                    options={"temperature": params["temperature"],
                             "top_p": params["top_p"],
                             "max_tokens": mt,
                             "do_sample": params.get("do_sample", True)})
            except llm_client.ProviderError as exc:
                # 本地模型不可达 —— 明确报错，不静默失败
                return web.json_response(
                    {"error": str(exc), "provider": item["name"],
                     "provider_id": item["id"],
                     "degraded": True}, status=502)
            raw = res["text"]
            finish_reason = res.get("finish_reason") or ""

            # thinking 残留（llm 层已剥离一次，这里再兜一次，防不同后端行为差异）
            raw = enh.strip_thinking(raw)
            enhanced = enh.strip_wrapping(raw)
            if not enhanced:
                return web.json_response(
                    {"error": "模型返回了空内容，未做修改",
                     "provider": item["name"],
                     "degraded": True}, status=502)
            # 小模型可能把系统提示词复读回来 —— 这不是有效的提示词，按失败处理
            #
            # 【2026-11-11】错误文案要**可读、可操作**：
            #   - 给出具体判据（哪一段重合、重合了多少），别只丢一句「复读」
            #   - 提示词框**绝不**写回复读内容（这里直接 502，不进 merged）
            echo = enh.echo_detail(enhanced, spec["system_prompt"])
            if echo.get("is_echo"):
                return web.json_response(
                    {"error": f"Provider「{item['name']}」返回的是系统提示词复读，"
                              f"不是有效的提示词（{echo.get('reason')}）。"
                              "该模型可能太小或未按指令输出，请换用更强的模型。",
                     "provider": item["name"], "provider_id": item["id"],
                     "code": "system_prompt_echo",
                     "echo": echo,
                     "degraded": True}, status=502)

            # 截断检测 —— 二次增强丢背景的根因守门人。
            #   finish_reason=length 或末段标题缺失 -> 判定被硬切。
            #   system_prompt 传进去是为了**从模板正文解析末段标题**，
            #   这样别的分支新增的 refine 模板（如末段「【整体细化】」的音乐版）
            #   不改代码也能被正确判定（跨分支复用，见 enhancer.template_sections）。
            coverage = enh.coverage_report(
                original, enhanced,
                finish_reason=finish_reason,
                validate_mode=validate_mode,
                system_prompt=spec.get("system_prompt", ""))
            if not coverage.get("truncated"):
                last = (mt, res, enhanced, finish_reason, coverage)
                break
            # 被截断 —— 还有下一次机会（用满上限重跑）就继续
            if attempt_idx + 1 < len(attempts):
                continue
            return web.json_response(
                {"error": "增强结果被 max_tokens 上限截断，内容不完整，未写回。"
                          + coverage.get("truncation_reason", "")
                          + f"（本次 max_tokens={mt}，已用满上限重试仍被截断）"
                          "请在「本地模型配置 → 增强 Agent」里调高"
                          "「最大输出 token 上限」，或改用更强的模型。",
                 "truncated": True,
                 "truncation_reason": coverage.get("truncation_reason", ""),
                 "missing_sections": coverage.get("missing_sections", []),
                 "coverage": coverage,
                 "provider": item["name"], "provider_id": item["id"],
                 "degraded": True}, status=502)

        used_max_tokens, res, enhanced, finish_reason, coverage = last
        params = dict(params)
        params["max_tokens"] = used_max_tokens

    # 硬约束校验（质量词 / 权重语法 / think 段 / bbox 结构）
    validation = enh.validate_output(enhanced, original, validate_mode)
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
    # 【提示词历史】增强流程也留痕 —— 无论这次增强最后有没有被写回提示词框，
    # 「原始 -> 增强后」这条对应关系都值得留下（用户下次能直接填回去）。
    # mode_ctx 是发起增强时所在的面板（t2i / t2i_moss / t2v / t2a …），
    # 它就是面板 key —— 跨面板隔离靠它。
    _ph_panel = mode_ctx or ""
    if _ph_panel:
        with db() as conn:
            ph_hooks.record_prompt_history(
                conn, _ph_panel,
                {"original_prompt": original, "prompt": enhanced,
                 "model_key": model_key},
                source=ph.SOURCE_ENHANCE)
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
        # 要素覆盖 + 截断检测结果（前端可显示保留率）
        "coverage": coverage,
        "finish_reason": finish_reason,
        "params": {"temperature": params["temperature"], "top_p": params["top_p"],
                   "max_tokens": params["max_tokens"],
                   "max_tokens_ceiling": params.get("max_tokens_ceiling"),
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

    # 【提示词历史】MOSS 结构化增强的最终文本提示词也进历史。
    # 面板固定是 t2i_moss —— layout 是 MOSS-Image 专属能力。
    with db() as conn:
        ph_hooks.record_prompt_history(
            conn, "t2i_moss",
            {"original_prompt": original, "prompt": result["final_prompt"]},
            source=ph.SOURCE_ENHANCE)
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

    # 【提示词历史】官方 PE 的扩写结果也进历史（面板 = mode，即 t2i_qwen / i2i_qwen）
    with db() as conn:
        ph_hooks.record_prompt_history(
            conn, mode,
            {"original_prompt": original, "prompt": result["expanded"]},
            source=ph.SOURCE_ENHANCE)
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


def _agent_memory_store():
    """Agent 的长期记忆库（跨会话用户偏好）。"""
    return agent_memory.get_store()


def _agent_error(exc: agent_mod.AgentError) -> web.Response:
    payload = {"error": exc.message, "code": exc.code}
    payload.update(exc.extra or {})
    return web.json_response(payload, status=exc.status)



async def api_agent_upload(request):
    """
    POST /api/agent/upload —— 内置 Agent 聊天区的**媒体上传**。

    multipart 表单：
      files[] / file / image   一个或多个**媒体**文件（图片 / 音频 / 视频）

      图片：png/jpg/jpeg/webp/bmp/gif       上限 20 MB，一次最多 9 张
      音频：wav/mp3/flac/m4a/aac/ogg/opus/aiff/aif/wma  上限 100 MB
      视频：mp4/webm/mov/mkv/m4v/avi       上限 500 MB
      音视频合计一次最多 4 个

    行为：
      * 每个文件独立校验（扩展名 / 大小 / **魔数**），一个失败不影响其它
      * 落进**素材库**（复用 asset_service.register_asset —— 与素材库页
        的 /api/assets 上传完全同一条链路，库在 /mnt/usb2t/asset_library）
      * 图片：落库后**同步**做多模态分析
      * 音视频：落库后排**异步**分析 job（深度音频管线 / 视频抽帧）
        ，前端轮询 GET /api/agent/media/{id}/status，就绪前不能引用
      * 分析失败 -> 明确报错 + soft_delete 回滚（不留“已入库没描述”的脏条目）
      * 返回每个文件的 asset_id / preview_url / kind / 描述

    为什么要有这个端点：
      用户要「拿这张图做复刻/修改」时，聊天区原本**没有任何入口**
      能把图送进 Agent —— 只能打字。参考图必须是素材库条目
      （i2i_qwen 强制要求参考图来自素材库），所以「上传」与「入库」
      必须是同一件事，不能各做一套。

    返回：{"ok", "results": [...], "errors": [...], "uploaded", "failed"}
    """
    try:
        payload = await agent_media.handle_upload(request)
    except agent_media.UploadError as exc:
        return web.json_response({"error": exc.message, "code": exc.code},
                                 status=exc.status)
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"[:300]
        return web.json_response({"error": f"上传失败：{detail}",
                                  "code": "upload_failed"},
                                 status=500)
    return web.json_response(payload)

async def api_agent_media_status(request):
    """
    GET /api/agent/media/{id}/status -- Agent 聊天区**音视频素材**的分析就绪状态。

    音视频上传后走**异步**分析（深度音频管线 / 视频抽帧多模态），
    耗时几分钟到十几分钟 —— 所以前端轮询本接口，
    直到 status == "done" 才允许把该素材作为参考素材引用。

    返回：{"asset_id", "status", "stage", "progress", "message",
          "available", "description", "tags", "kind",
          "duration_sec", "width", "height", "error"}
    """
    asset_id = request.match_info["id"]
    try:
        payload = await agent_media.handle_analysis_status(asset_id)
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"[:200]
        return web.json_response({"error": detail, "code": "status_failed"},
                                 status=500)
    return web.json_response(payload)


async def api_agent_chat(request):
    """
    POST /api/agent/chat —— 内置多模态 Agent 主入口。

    body:
      message       用户意图（必填，上限 4000 字）
      session_id    会话 ID（可选；不给则新建，多轮对话靠它串联）
      provider_id   指定本地模型 Provider（可选；不给则用「本地模型配置」里的默认）
      max_steps     最大步骤数（可选，默认 agent_mod.MAX_STEPS，上限同值）
      stream        1/true = 用 SSE 流式返回（事件契约见
                    docs/内置多模态Agent-接口契约.md）
                    等价于调用 POST /api/agent/chat/stream。

    【流式开关的两种合法写法 —— 2026-11-10 审查后明确】
      A. POST /api/agent/chat
         body: {"message": "...", "stream": 1}          ← body 载体
      B. POST /api/agent/chat?stream=1
         body: {"message": "..."}                       ← query 载体
    两种都命中同一条 SSE 契约（事件名 / 字段完全一致）。
    审查实测原前端发的是 B，而后端只读 body（A）——
    于是真实浏览器 12/12 请求返回 application/json，流式 100% 不生效。
    现在后端两边都读，前端也改用 A（body），
    这样**已部署的旧前端**（B）依然能命中流式。

    返回结构化结果（见 h3studio.agent.run_agent 的 docstring）：
      status / answer / jobs / assets / prompt / steps / usage / provider / error
      clarify_options / clarify_hint  （澄清态的快捷选项与提示）
    """
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误", "code": "bad_request"},
                                 status=400)

    # stream=1 -> 走 SSE（与 /api/agent/chat/stream 完全一致的契约）。
    #
    # 为什么要同一个端点也支持 stream：前端只需要一个 URL，
    # 带不带 stream 由请求体决定，不用在两套 URL 之间切换。
    #
    # 【两种写法都合法，2026-11-10】
    #   A. POST /api/agent/chat        + body {"stream": 1}   （body 载体）
    #   B. POST /api/agent/chat?stream=1 + body 无 stream    （query 载体）
    # 审查实测：前端原来发的是 B，而后端只读 body（A）——
    # 于是真实浏览器 12/12 请求返回 application/json，流式 100% 不生效。
    # 现在两边都读，任一写法都能命中 SSE。
    # （后端只读 query 也能兼容**已部署的旧前端**；只改前端则旧前端仍坏。）
    stream_flag = payload.get("stream")
    if stream_flag is None or str(stream_flag).strip() == "":
        stream_flag = request.query.get("stream")
    if str(stream_flag or "").lower() in ("1", "true", "yes", "on"):
        return await api_agent_chat_stream(request)

    message = str(payload.get("message") or "").strip()
    if not message:
        return web.json_response({"error": "请输入要处理的内容",
                                  "code": "bad_request"}, status=400)

    session_id = str(payload.get("session_id") or "").strip()
    provider_id = str(payload.get("provider_id") or "").strip() or None
    # 附件（用户随消息上传的图片）
    attachments = payload.get("attachments") or []
    try:
        max_steps = int(payload.get("max_steps") or agent_mod.MAX_STEPS)
    except (TypeError, ValueError):
        max_steps = agent_mod.MAX_STEPS

    store = _agent_store()
    session_id = store.create(session_id or None)
    # 存用户消息（在跑 Agent 之前存 —— 即使 Agent 挂了，历史也不丢）
    store.add_message(session_id, "user", message,
                      meta=_agent_user_meta(attachments))

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
                provider_id=provider_id, max_steps=max_steps, history=history,
                session_store=store, memory_store=_agent_memory_store(),
                attachments=attachments)
        except agent_mod.AgentError as exc:
            # 【为什么要带 meta —— 2026-11-12】
            # 不带 meta 的话，这条失败消息的 meta 是空的 -> 会话列表的
            # 「状态」判定拿不到 status -> 回落到「完成」。
            # 结果是：**失败的会话显示成「完成」**（实测复现）。
            # 失败必须落成 meta.status=failed，状态才有真源。
            store.add_message(session_id, "assistant", f"[请求失败] {exc.message}",
                              meta={"status": "failed", "error": exc.message})
            return _agent_error(exc)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"[:300]
            store.add_message(session_id, "assistant", f"[请求失败] {detail}",
                              meta={"status": "failed", "error": detail})
            return web.json_response(
                {"error": f"Agent 执行失败：{detail}", "code": "agent_internal_error"},
                status=500)
    # 落库：模型结论 + 工具调用轨迹（下一轮对话时作为上下文）
    if result.get("answer"):
        store.add_message(session_id, "assistant", result["answer"],
                          meta=_agent_message_meta(result))
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
    # 附件（用户随消息上传的图片）
    attachments = payload.get("attachments") or []
    try:
        max_steps = int(payload.get("max_steps") or agent_mod.MAX_STEPS)
    except (TypeError, ValueError):
        max_steps = agent_mod.MAX_STEPS

    store = _agent_store()
    session_id = store.create(session_id or None)
    store.add_message(session_id, "user", message,
                      meta=_agent_user_meta(attachments))
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

    # 事件队列：Agent 编排器通过 on_progress / on_event 往里塞事件，
    # 这里边跑边从队列里取出来写成 SSE。
    queue: asyncio.Queue = asyncio.Queue()

    def _on_progress(stage: str, detail: str, done: int, total: int) -> None:
        queue.put_nowait({
            "type": "progress",
            "stage": stage,
            "detail": detail or "",
            "done": int(done or 0),
            "total": int(total or 0),
        })

    def _on_event(name: str, data: dict) -> None:
        """Agent 的结构化事件（token / tool_call / tool_result / job / done / error）。"""
        payload = dict(data or {})
        payload["type"] = name
        queue.put_nowait(payload)

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
                    history=history, on_progress=_on_progress,
                    on_event=_on_event, session_store=store,
                    memory_store=_agent_memory_store(),
                    attachments=attachments)
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
            # 按事件类型分发到不同的 SSE event 名 —— 前端可以按名字监听，
            # 也可以统一用 onmessage（data 里带 type 字段）。
            #
            # type 一律补齐（哪怕事件本来就带）：前端如果统一用 onmessage
            # 分派，缺 type 就只能靠猜 —— 契约要求**每个**事件都有 type。
            event_type = event.get("type") or "progress"
            event["type"] = event_type
            await _write(event_type, event)

        result = task.result()
        if result is not None:
            # 失败链路（no_tool_call / sensitive_content）：
            # Agent 已经发了 error 事件 —— 此时**不再发 result**。
            #
            # 为什么：result 是「成功收尾」信号（基线前端靠它判定完成）。
            # 失败后再补一个 result，前端会以为任务成功了。
            failed_code = result.get("code")
            if failed_code:
                # Agent 侧**已经**发过 error 事件（见 agent.py 的 _event("error",...)），
                # 这里只补落库并**抑制 result** —— 不要重复发 error。
                if result.get("answer"):
                    store.add_message(session_id, "assistant", result["answer"],
                                      meta=_agent_message_meta(result))
                return resp
            if result.get("answer"):
                store.add_message(session_id, "assistant", result["answer"],
                                  meta=_agent_message_meta(result))
            for step in result.get("steps") or []:
                tool_name = step.get("tool")
                if not tool_name:
                    continue
                note = step.get("error") or "ok"
                store.add_message(session_id, "tool", f"{tool_name}: {note}",
                                   tool_name=tool_name)
            result["session_id"] = session_id
            result["provider_name"] = (result.get("provider") or {}).get("name") or ""
            # result 也带 type —— 契约要求每个事件都有，前端可统一 onmessage 分派
            result["type"] = "result"
            await _write("result", result)
    finally:
        if not task.done():
            task.cancel()
        await resp.write_eof()
    return resp




def _agent_user_meta(attachments) -> dict:
    """
    用户消息的 meta：只存附件（asset_id / 名字 / 预览图）。

    恢复对话时前端要能画出「你附了 2 张图」—— 但只存 asset_id 就够前端
    去拉缩略图。这里额外存 name/preview_url 是为了**少一次请求**：
    附件可能已经被素材库清理（条目失效），那时 preview_url 是最后一张底图。
    """
    items = attachments if isinstance(attachments, list) else []
    out = []
    for item in items:
        if isinstance(item, str):
            out.append({"asset_id": item})
            continue
        if not isinstance(item, dict):
            continue
        asset_id = str(item.get("asset_id") or "")
        if not asset_id:
            continue
        entry = {"asset_id": asset_id}
        for key in ("name", "preview_url"):
            value = item.get(key)
            if value:
                entry[key] = value
        out.append(entry)
    return {"attachments": out} if out else {}


def _agent_message_meta(result: dict) -> dict:
    """
    从 Agent 的结果里抽出**前端恢复对话所需**的最小结构化信息。

    只存这些：
      status       状态徽章（completed / truncated / failed / needs_clarification）
      jobs         任务卡（job_id / mode / label / status / prompt / params /
                   outputs / error / progress）
      attachments  本轮附件（asset_id / name / preview_url / description）
      error        失败原因（失败时前端要显示红条）
      notice       中性说明（截断时前端显示黄条，不是红条）

    【刻意不存的】
      * steps / usage / provider —— 对恢复对话没有价值，纯占地方；
      * 完整的 steps[].args —— 可能含超长参数；
      * 原始响应体 —— 会让库迅速膨胀。

    【为什么用最小集】
    meta 是**恢复 UI 的数据源**，不是审计日志。
    多存一点不会更准，只会让库更快变大。
    """
    if not isinstance(result, dict):
        return {}
    meta: dict = {}
    status = str(result.get("status") or "").strip()
    if status:
        meta["status"] = status
    error = str(result.get("error") or "").strip()
    if error:
        meta["error"] = error
    notice = str(result.get("notice") or "").strip()
    if notice:
        meta["notice"] = notice

    jobs = []
    for job in result.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        entry = {
            "job_id": str(job.get("job_id") or ""),
            "mode": str(job.get("mode") or ""),
            "label": str(job.get("label") or ""),
            "status": str(job.get("status") or "queued"),
        }
        for key in ("prompt", "summary", "progress", "progress_text", "error"):
            value = job.get(key)
            if value not in (None, ""):
                entry[key] = value
        params = job.get("params")
        if isinstance(params, dict) and params:
            entry["params"] = params
        outputs = job.get("outputs")
        if isinstance(outputs, list) and outputs:
            entry["outputs"] = outputs
        if entry["job_id"]:
            jobs.append(entry)
    if jobs:
        meta["jobs"] = jobs

    attachments = []
    for item in result.get("attachments") or []:
        if not isinstance(item, dict):
            continue
        asset_id = str(item.get("asset_id") or "")
        if not asset_id:
            continue
        entry = {"asset_id": asset_id}
        for key in ("name", "description", "preview_url"):
            value = item.get(key)
            if value not in (None, ""):
                entry[key] = value
        tags = item.get("tags")
        if isinstance(tags, list) and tags:
            entry["tags"] = [str(t) for t in tags][:20]
        attachments.append(entry)
    if attachments:
        meta["attachments"] = attachments

    return meta


async def api_agent_sessions_create(request):
    """
    POST /api/agent/sessions —— 新建会话。

    【为什么要有这个接口 —— 2026-11-11】
    前端原来的「新建会话」只是把内存里的 sessionId 清空，
    **服务端根本不知道有这个会话** —— 于是：
      * 刷新页面后回到的是「空会话」，用户刚发的那轮对话找不回来；
      * 会话列表里没有这一条。
    现在新建就真的落库，前端拿回 id 存起来。

    请求体（可选）：{"title": "..."} —— 不给就等首条用户消息自动生成标题。
    """
    store = _agent_store()
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    title = str(payload.get("title") or "").strip()[:80]
    sid = store.create(None)
    if title:
        store.rename(sid, title)
    return json_ok({"session_id": sid, "ok": True})


async def api_agent_sessions(request):
    """
    GET /api/agent/sessions —— 会话清单（最近 N 条）。

    可选查询参数：
      limit   返回条数上限（默认 100，上限 500）
      q       搜索关键词（标题或任意消息正文命中；为空 = 不过滤）
    """
    try:
        limit = int(request.query.get("limit") or 100)
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 500))
    query = str(request.query.get("q") or "").strip()[:120]
    store = _agent_store()
    sessions = store.list_sessions(limit=limit, query=query)
    # 关联产物数：会话库与任务库是**两个库**（见 agent_sessions.py 的说明），
    # 所以产物数要去 jobs 表数 —— 不能指望会话库自己知道。
    counts = session_job_counts([s["id"] for s in sessions])
    # 未完成任务数：一次查询同时拿（session_job_stats 与 counts 共用一次 IN 查询
    # 的思路，但 stats 多带一个 SUM 聚合列 —— 单独查避免重复往返）。
    stats = session_job_stats([s["id"] for s in sessions])
    for item in sessions:
        item["artifact_count"] = counts.get(item["id"], 0)
        # pending_count = 该会话下排队中/生成中的任务数。
        # 【为什么文案要说「任务」而不能说「会话进行中」】
        # 服务端**没有** per-session 运行注册表（AGENT_GATE 是全局信号量），
        # 这里统计的是**任务**状态，不是会话的运行状态。
        item["pending_count"] = (stats.get(item["id"]) or {}).get("pending", 0)
    return json_ok({"sessions": sessions, "query": query})


async def api_agent_session_get(request):
    """
    GET /api/agent/sessions/{id} —— 会话详情（消息历史 + 产物）。

    返回：
      session_id / title / messages（带 meta）/ jobs（该会话建的任务）
    """
    sid = request.match_info["id"]
    store = _agent_store()
    if not store.exists(sid):
        return web.json_response({"error": "会话不存在", "code": "not_found"},
                                 status=404)
    jobs = list_jobs_for_session(sid)
    return json_ok({
        "session_id": sid,
        "title": store.title_of(sid),
        "messages": store.raw_history(sid),
        "jobs": jobs,
    })


async def api_agent_session_jobs(request):
    """
    GET /api/agent/sessions/{id}/jobs —— 该会话产生的全部任务（产物观测）。

    权威来源是 jobs 表的 session_id 列（建任务时当场写入），
    不是从模型正文里抠 id —— 见 list_jobs_for_session 的说明。
    """
    sid = request.match_info["id"]
    store = _agent_store()
    if not store.exists(sid):
        return web.json_response({"error": "会话不存在", "code": "not_found"},
                                 status=404)
    jobs = list_jobs_for_session(sid)
    return json_ok({"session_id": sid, "jobs": jobs, "count": len(jobs)})


async def api_agent_session_delete(request):
    """
    DELETE /api/agent/sessions/{id} —— 删除会话（连同它的消息）。

    【语义澄清 —— 2026-11-11】
    这个接口**删除会话**（会话行 + 全部消息都删），
    不是只「清空消息」。老接口名叫 api_agent_session_delete 但文档写
    「清空会话」，前端拿不准该显示什么 —— 现在统一成「删除」。

    注意：**不会**删除该会话建的任务。任务在 jobs 库里，
    删掉会话只是丢对话历史，生成结果照旧在生成列表里。
    （这与素材库的 409 保护是同一套取舍：会话数据可丢弃，任务不可。）
    """
    sid = request.match_info["id"]
    store = _agent_store()
    existed = store.exists(sid)
    # 【级联清理产物引用 —— 2026-11-11 新增需求】
    # 删会话时把该会话的任务**引用关系**清掉（jobs.session_id 置空），
    # 但**任务记录本身留在生成库里**。
    #
    # 【为什么不删任务、也不碰素材库】
    #   * 任务同时是「生成库」里的一条记录，删了会让用户在生成列表里
    #     凭空少一条（可能是他想要的结果）；
    #   * 任务带 outputs 产物路径，删记录会产生「文件还在、任务没了」的孤儿；
    #   * 素材库实体文件在 /mnt/usb2t/asset_library，与会话完全无关 ——
    #     **绝对不能**因为删会话就删素材。
    # 详见 clear_session_job_refs 的说明。
    cleared_jobs = clear_session_job_refs(sid)
    deleted = store.delete(sid)
    return json_ok({"deleted": sid, "ok": bool(deleted and existed),
                    "cleared_job_refs": cleared_jobs})


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
        "tool_count": len(specs),
        "limits": {
            "max_steps": agent_mod.MAX_STEPS,
            "max_tool_calls": agent_mod.MAX_TOOL_CALLS,
            "total_timeout": agent_mod.TOTAL_TIMEOUT,
            "step_timeout": agent_mod.STEP_TIMEOUT,
            "max_concurrent": agent_mod.MAX_CONCURRENT,
            "no_tool_retry": agent_mod.NO_TOOL_RETRY,
            "tool_retry": agent_mod.TOOL_RETRY,
            "repeat_limit": agent_mod.REPEAT_LIMIT,
        },
        "memory_limits": {
            "max_entries": agent_memory.MAX_ENTRIES,
            "max_value_chars": agent_memory.MAX_VALUE_CHARS,
            "max_read_limit": agent_memory.MAX_READ_LIMIT,
        },
        "events": ["token", "tool_call", "tool_result", "job", "done", "error",
                   "progress", "result"],
    })


async def api_agent_memory(request):
    """GET /api/agent/memory —— 读取 Agent 的长期记忆（跨会话用户偏好）。

    查询参数：
      query   记忆键关键词（留空返回最近的全部）
      scope   global（默认）或 session:<id>
      limit   返回条数上限（默认 20，上限 100）
    """
    store = _agent_memory_store()
    query = request.query.get("query", "")
    scope = request.query.get("scope") or None
    try:
        limit = int(request.query.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    entries = store.recall(query, scope=scope, limit=limit)
    return json_ok({"memories": entries, "total": len(entries),
                    "stats": store.stats()})


async def api_agent_memory_set(request):
    """POST /api/agent/memory —— 写入一条长期记忆。

    body: {"key": str, "value": any, "scope": "global" | "session:<id>"}
    命中提示注入的内容会被拒绝（code=prompt_injection_detected）。
    """
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误", "code": "bad_request"},
                                 status=400)
    key = str(payload.get("key") or "").strip()
    value = payload.get("value")
    scope = str(payload.get("scope") or "").strip() or None
    if not key:
        return web.json_response({"error": "key 不能为空", "code": "bad_request"},
                                 status=400)
    if value is None or str(value).strip() == "":
        return web.json_response({"error": "value 不能为空",
                                  "code": "bad_request"}, status=400)
    store = _agent_memory_store()
    result = store.remember(key, value, scope=scope, source="http")
    if result.get("error"):
        return web.json_response({"error": result["error"],
                                  "code": result.get("code", "bad_request"),
                                  "matches": result.get("matches") or []},
                                 status=400)
    return json_ok(result)


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



# ------------------------------------------------------------------ 提示词历史
async def api_prompt_history_list(request):
    """GET /api/prompt-history —— 按面板读取提示词历史（分页）。

    query:
      panel_key  可选。按面板过滤（跨面板隔离的判据）。
                 省略 = 全部面板。
      model_key  可选。按模型过滤（同一个面板内换模型时用）。
      limit      可选。默认 20，上限 100。
      offset     可选。默认 0（滚动加载用）。

    返回:
      { items: [{id, panel_key, model_key, original_prompt,
                 enhanced_prompt, source, timestamp, use_count}],
        total: 该面板（or 全部）的总条数,
        limit, offset, has_more }
    """
    panel_key = (request.query.get("panel_key") or "").strip()
    model_key = (request.query.get("model_key") or "").strip()
    try:
        limit = int(request.query.get("limit") or ph.DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = ph.DEFAULT_LIMIT
    try:
        offset = int(request.query.get("offset") or 0)
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, ph.MAX_LIMIT))
    offset = max(0, offset)

    with db() as conn:
        items = ph.list_history(conn, panel_key=panel_key, model_key=model_key,
                                limit=limit, offset=offset)
        total = ph.count_history(conn, panel_key=panel_key, model_key=model_key)
        panels = ph.list_panels(conn)
    return json_ok({
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(items) < total,
        "panels": panels,
    })


async def api_prompt_history_delete(request):
    """DELETE /api/prompt-history?panel_key=xxx —— 清空某个面板的历史。"""
    panel_key = (request.query.get("panel_key") or "").strip()
    if not panel_key:
        return web.json_response({"error": "缺少 panel_key"}, status=400)
    with db() as conn:
        removed = ph.delete_panel(conn, panel_key)
    return json_ok({"ok": True, "removed": removed, "panel_key": panel_key})


async def api_comfy_models(request):
    """GET /api/comfy-models —— **ComfyUI 当前可用模型清单**（诊断用）。

    直接把 ComfyUI 的模型目录列表 + 节点输入枚举转发出来，并标注
    本面板登记的每个权重「在不在里面」。

    为什么需要这个端点：
      用户报「文生视频面板说获取不到模型」时，第一件事就是要有**一份
      可核对的原始事实** —— ComfyUI 自己说它有哪些模型。这个端点就是
      那个事实的落地形式，不用再去猜哪个接口对。

    查询参数：
      folder      diffusion_models（默认） | loras
      full        1 = 返回完整清单；0（默认）= 只返回计数 + 本面板相关项
    """
    folder = (request.query.get("folder") or "diffusion_models").strip()
    if folder not in comfy_models.COMFY_MODEL_FOLDERS:
        return web.json_response(
            {"error": f"未知目录 {folder}，可选：{list(comfy_models.COMFY_MODEL_FOLDERS)}"},
            status=400)
    full = (request.query.get("full") or "").strip() in ("1", "true", "yes")
    base = video_models.comfy_base_url()
    try:
        # 异步版：ComfyUI 首次列目录实测 43 秒，同步调用会卡住事件循环。
        # 本端点每次都直接问 ComfyUI（诊断用，不吃快照缓存）；
        # 面板自己走 /api/video-options 的快照缓存。
        async with ClientSession(timeout=ClientTimeout(total=60)) as session:
            names = await comfy_models.fetch_model_list_async(session, base,
                                                              folder)
    except comfy_models.ComfyModelListError as exc:
        return web.json_response(
            {"ok": False, "error": str(exc), "folder": folder,
             "comfyui_url": base}, status=502)
    payload = {
        "ok": True,
        "folder": folder,
        "comfyui_url": base,
        "count": len(names),
    }
    if full:
        payload["names"] = names
    # 本面板登记的权重 vs ComfyUI 清单 —— 逐个标注
    if folder == "diffusion_models":
        checked = []
        for key, meta in video_models.VIDEO_MODELS.items():
            checked.append({
                "key": key,
                "label": meta["label"],
                "unet": meta["unet"],
                "listed": meta["unet"] in set(names),
                "location": meta.get("location", ""),
            })
        payload["panel_models"] = checked
    else:
        checked = []
        for key, meta in video_models.VIDEO_ACCEL_LORAS.items():
            checked.append({"key": key, "name": meta["name"],
                            "listed": meta["name"] in set(names)})
        for key, meta in video_models.VIDEO_ACCEL_LORA_COMBOS.items():
            checked.append({"key": key, "name": meta["name"],
                            "listed": meta["name"] in set(names)})
        payload["panel_loras"] = checked
    return json_ok(payload)


# ------------------------------------------------------------------ app
def make_app():
    app = web.Application(client_max_size=200 * 1024 * 1024)
    app.router.add_post("/api/jobs", api_create)
    app.router.add_post("/api/longfilms", api_longfilm_create)
    app.router.add_get("/api/jobs", api_list)
    app.router.add_get("/api/jobs/{id}", api_get)
    # 生成队列快照（队列顺序 / 状态 / 计数）—— 队列化需求的只读视图
    app.router.add_get("/api/queue", api_queue)
    # 接续生成：上下文回填 + 功能可用性探测
    app.router.add_get("/api/jobs/{id}/continuation", api_continuation_context)
    app.router.add_get("/api/continuation/state", api_continuation_state)
    app.router.add_post("/api/jobs/{id}/cancel", api_cancel)
    app.router.add_delete("/api/jobs/{id}", api_delete)
    app.router.add_get("/api/system", api_system)
    app.router.add_get("/api/status/stream", api_status_stream)
    app.router.add_get("/api/hardware", api_hardware)
    app.router.add_post("/api/gpu/power-limit", api_gpu_power_limit)
    app.router.add_post("/api/unload", api_unload)
    app.router.add_get("/api/image-options", api_image_options)
    app.router.add_get("/api/music-options", api_music_options)
    app.router.add_post("/api/music-enhance", api_music_enhance)

    # 文生视频面板（模型 / 加速 LoRA / 注意力 / 二采 / 宽高吸附）
    app.router.add_get("/api/video-options", api_video_options)
    # 合并说明：/api/video-categories（video-tabs）与 /api/comfy-models
    # （model-list-fix）是两个独立路由，都保留。
    app.router.add_get("/api/video-categories", api_video_categories)
    app.router.add_get("/api/comfy-models", api_comfy_models)
    app.router.add_get("/api/video-size", api_video_size)
    app.router.add_get("/api/video-config", api_video_config)
    app.router.add_put("/api/video-config", api_video_config)
    app.router.add_get("/api/generation-types", api_generation_types)
    # 提示词历史（各功能面板的提示词留痕）
    app.router.add_get("/api/prompt-history", api_prompt_history_list)
    app.router.add_delete("/api/prompt-history", api_prompt_history_delete)
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
    app.router.add_post("/api/agent/upload", api_agent_upload)
    app.router.add_get("/api/agent/media/{id}/status", api_agent_media_status)
    app.router.add_post("/api/agent/chat/stream", api_agent_chat_stream)
    app.router.add_get("/api/agent/tools", api_agent_tools)
    app.router.add_get("/api/agent/memory", api_agent_memory)
    app.router.add_post("/api/agent/memory", api_agent_memory_set)
    app.router.add_post("/api/agent/sessions", api_agent_sessions_create)
    app.router.add_get("/api/agent/sessions", api_agent_sessions)
    app.router.add_get("/api/agent/sessions/{id}", api_agent_session_get)
    app.router.add_get("/api/agent/sessions/{id}/jobs", api_agent_session_jobs)
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
    # 【可选 access log —— 验收取证用】
    #
    # 默认**关闭**：8190 线上服务不写 access log（避免刷屏、避免改线上行为）。
    # 只有独立验收实例设了 H3STUDIO_ACCESS_LOG=1 才开。
    #
    # 为什么要它：验收要的是「服务端自己的日志证据」——
    # 光有客户端看到的 Content-Type 头还不够，
    # 要能在服务端日志里看到它按 text/event-stream 回了 200。
    if os.environ.get("H3STUDIO_ACCESS_LOG") == "1":
        import logging
        _alog = logging.getLogger("aiohttp.access")
        _alog.setLevel(logging.INFO)
        if not _alog.handlers:
            _h = logging.StreamHandler(sys.stdout)
            _h.setFormatter(logging.Formatter(
                '[access] %(message)s'))
            _alog.addHandler(_h)
        print("[access] access log 已启用")
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
