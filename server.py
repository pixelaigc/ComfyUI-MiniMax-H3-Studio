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
from h3studio import h3image as h3img
from h3studio import providers as prov
from h3studio import llm as llm_client
from h3studio import enhancer as enh
from h3studio import enhance_assets as enh_assets
from h3studio import moss_layout
from h3studio import qwen_pe
from h3studio import h3image_pe
from h3studio import loras as lora_registry
from h3studio import gen_types
from h3studio import gen_defaults
from h3studio import size_spec
from h3studio import gen_normalize
from h3studio import seed_guard
from h3studio import asset_ref_guard
from h3studio import hardware as hw
from h3studio import assets as asset_store
from h3studio import docs_static
from h3studio import prompt_catalog
from h3studio import prompt_history as ph
from h3studio import thumbnails as thumb_service
from h3studio import prompt_history_hooks as ph_hooks
from h3studio import asset_api
from h3studio import twopass_policy
from h3studio import video_models
from h3studio import twopass_budget
from h3studio import job_meta
from h3studio import artifact_meta
from h3studio import continuation as cont
from h3studio import continuation_service as cont_service
# ComfyUI 执行错误的提取与中文化（2026-11-12 修：原来把
# status.messages 的条目当「m[0] 是字典」读，导致真正的原因被
# AttributeError 吞掉，用户只看到原始 Python 异常文本）。
from h3studio import comfy_errors
# 【2026-11-14 A5 需求】「运行中」状态的**真实性**判定（纯函数层）。
#
# 【为什么要有这一层】worker 的轮询循环原本是 `while True:` ——
# 只等 comfy_history 出结果，**既没有超时上限，也不看 ComfyUI 的队列**。
# 于是有两种「永远运行中」：
#   1. 服务重启后 DB 里残留 status='running' 的行（进程里的 worker 没了，
#      行还在）-> 卡片永远转圈；
#   2. ComfyUI 侧的任务已经被取消 / 被清队列 / ComfyUI 重启过
#      -> history 里没有、队列里也没有 -> worker 永远等下去。
# 判定逻辑放在纯函数模块里（可离线穷举），server.py 只负责取数。
from h3studio import running_truth
# 【2026-11-19 缺陷 1 修】生成任务的**运行上限**策略（按媒体类型区分）。
#
# 【为什么要有这一层】原来只有一个全局阈值 `running_timeout_sec`
# （默认 30 分钟），对**所有**任务一视同仁 —— 视频任务跑得久是正常现象，
# 却会被硬判「超时失败」。策略层把「上限多久」变成纯函数，
# 视频类默认不限时（0），图像 / 音频保留可配置上限。
from h3studio import job_timeout
# 【2026-11-19 缺陷 2 修】ComfyUI 执行结果的**成功判定**（纯函数层）。
#
# 【为什么要有这一层】worker 原来先看 `hist.get("outputs")` 非空再判失败 ——
# 而 ComfyUI 在 OOM / 节点报错时 outputs 里**也会有**已执行完节点的输出
# （实测只有文本节点）-> 取不出媒体 -> saved=[] -> 任务被标 done
# -> 前端显示「生成成功」+ 全黑预览。判定层把顺序纠正过来：
# **失败优先于产物**，且产物必须真实存在且非空。
from h3studio import gen_outcome
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
from h3studio import h3_audio_track as h3audio
from h3studio import h3_audio_continuation as h3acont
from h3studio import h3_video_size
from h3studio import h3_video_modes as h3modes
from h3studio import music_yue2
from h3studio import music_style
from h3studio import yue2_schema
from h3studio import media_cleanup
from h3studio import favorites
from h3studio import agent as agent_mod
from h3studio import agent_memory
from h3studio import agent_sessions
from h3studio import agent_media
# 【2026-11-13 审查目标 4/5】Agent 管理配置 + 额外能力工具 + 外部搜索 + 媒体工具
from h3studio import agent_settings
from h3studio import agent_extra_tools
from h3studio import agent_websearch
from h3studio import agent_media_tools


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
    # MiniMax-H3-Image
    "h3_image": True,
    "h3_image_refs": True,
}

DEFAULT_CONFIG = {
    "host": "0.0.0.0",
    "port": 8190,
    "comfyui_url": "http://127.0.0.1:8188",
    "data_dir": str(BASE / "data"),
    "models_dir": "",
    "poll_interval": 1.5,
    # 【2026-11-14 A5】单个任务的运行上限（秒）。
    # 超过它就判定为「超时失败」并给出中文原因 —— 不再无限期「运行中」。
    # 0 = 不限制（不推荐：会退回「永远运行中」的旧行为）。
    #
    # 【2026-11-19 缺陷 1 修】这个值现在是**图像 / 音频类**的上限；
    # 视频类用下面的 `running_timeout_video_sec`（默认 0 = 不限时）。
    "running_timeout_sec": job_timeout.DEFAULT_TIMEOUT_SEC,
    # 【2026-11-19 缺陷 1 修】视频类任务的运行上限（秒）。
    # **默认 0 = 不按运行时长判失败** —— 视频跑很久是正常现象，
    # 即使跑一天也不该被判失败（用户明确要求）。
    # 「任务丢失」判定与时长无关，所以视频任务不会卡成「永远运行中」。
    "running_timeout_video_sec": job_timeout.VIDEO_TIMEOUT_SEC,
    # 【2026-11-14 A5】「刚提交」的宽限期（秒）：
    # 提交后这段时间内不判定「任务已丢失」（ComfyUI 可能还没把它放进队列）。
    "running_lost_grace_sec": running_truth.DEFAULT_LOST_GRACE_SEC,
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
    # ComfyUI 地址：独立测试实例要指向**受控的假 ComfyUI**（E2E 用）。
    # 不加这个覆盖，隔离实例只能连真 ComfyUI（会真占 GPU、耗时不可控）。
    comfy_env = os.environ.get("H3STUDIO_COMFY_URL")
    if comfy_env:
        cfg["comfyui_url"] = comfy_env
    return cfg


CFG = load_config()


def _cfg_float(key: str, default: float) -> float:
    """从配置里取一个浮点数，非法值回落到默认值。

    【为什么需要】config.json 是用户手改的 JSON —— 写成 "1800"（字符串）
    或写成 "abc" 都会让朴素的 float() 抛异常，把整个服务拖挂。
    这里统一兜底：配置坏了就当默认值用，并打印一行提示。
    """
    try:
        value = float(CFG.get(key, default))
    except (TypeError, ValueError):
        print(f"[config] {key} 不是数字，回落到默认值 {default}")
        return float(default)
    if value < 0:
        print(f"[config] {key} 不能为负，回落到默认值 {default}")
        return float(default)
    return value


#: 任务运行上限（秒）。0 = 不限制（不推荐 —— 会退回「永远运行中」）。
#:
#: 【2026-11-19 缺陷 1 修】这个常量现在是**图像 / 音频类**的上限。
#: 视频类走 `RUNNING_TIMEOUT_VIDEO_SEC`（默认 0 = 不按时长判失败）。
RUNNING_TIMEOUT_SEC = _cfg_float("running_timeout_sec",
                                job_timeout.DEFAULT_TIMEOUT_SEC)
#: 【2026-11-19 缺陷 1 修】视频类任务的运行上限（秒）。
#:
#: 【默认 0 = 不按运行时长判失败】用户明确要求：「视频任务运行很久是
#: 正常现象，即使跑 1 天也不应被判失败」。
#:
#: 【为什么这样安全】「丢失」判定（ComfyUI 队列与 history 都没有）与
#: 时长**无关** —— 视频任务要么在队列里（继续等）、要么被 ComfyUI 丢掉
#: （明确失败），不会卡成「永远运行中」。
RUNNING_TIMEOUT_VIDEO_SEC = _cfg_float("running_timeout_video_sec",
                                       job_timeout.VIDEO_TIMEOUT_SEC)
#: 「刚提交」的宽限期（秒）：这段时间内不判定「任务已丢失」。
RUNNING_LOST_GRACE_SEC = _cfg_float("running_lost_grace_sec",
                                    running_truth.DEFAULT_LOST_GRACE_SEC)


def timeout_sec_for_media(media: str) -> float:
    """媒体类型 -> 该类任务的运行上限（秒）。0 = 不按时长判失败。

    【为什么要有这个函数】缺陷 1 的根因是「所有任务共用一个 30 分钟阈值」。
    视频类必须单独走 `RUNNING_TIMEOUT_VIDEO_SEC`（默认 0），
    否则视频跑到 30 分钟就被硬判失败。

    【口径】media 取自 `job_timeout.media_of_mode`（image / video / audio / other）。
    """
    return job_timeout.timeout_for_media(
        media,
        image_timeout_sec=RUNNING_TIMEOUT_SEC,
        video_timeout_sec=RUNNING_TIMEOUT_VIDEO_SEC)


def timeout_sec_for_mode(mode: str) -> float:
    """任务 mode -> 运行上限（秒）。视频类默认不限时。"""
    return timeout_sec_for_media(job_timeout.media_of_mode(mode))


def timeout_reason_for(timeout_sec: float, media: str = job_timeout.MEDIA_OTHER) -> str:
    """生成超时的中文原因（文案与阈值一致，不写死分钟数）。"""
    return job_timeout.timeout_reason(timeout_sec, media)

#: worker 多久查一次 ComfyUI 的 /queue（秒）。
#:
#: 【为什么节流】worker 的轮询循环每 poll_interval（1.5 秒）查一次 history；
#: 再每轮都查 /queue 会让 ComfyUI 多收 60% 的请求。
#: 【为什么不能太大】判定「任务已丢失」会因此延迟 —— 用户会多等一会儿
#: 才看到明确失败。10 秒是个平衡点：延迟感知不到，负载可以忽略。
QUEUE_CHECK_INTERVAL = 10.0

#: 实时观测缓存的有效期（秒）。
#:
#: 【为什么是 3 秒】前端每 2 秒刷一次任务列表。3 秒的缓存意味着
#: ComfyUI 收到的 /queue 请求最多是前端的 1/1.5，而不是 1:1 ——
#: ComfyUI 正在跑模型，那边的 HTTP 处理是要排队的，不该被观测拖慢。
LIVE_STATE_CACHE_TTL = 3.0

DATA = Path(CFG["data_dir"])
OUT_DIR = DATA / "outputs"
UPLOAD_DIR = DATA / "uploads"
#: 【2026-11-15】本地直传的**视频 / 音频**后缀白名单。
#: 参考视频与参考音乐现在也能从面板直接上传（缺口 1），
#: 这两个后缀不能走图片白名单（save_uploaded_image 会把 .mp4 改成 .png）。
MEDIA_SUFFIXES = {
    # 视频
    ".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".gif",
    # 音频
    ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus",
}
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
        # 【2026-11-20 新增】生成媒体库「收藏」表。
        #
        # 【为什么是新建表而不是给 jobs 加列】
        #   * jobs 表是「任务记录」，收藏是**用户对产物的标记** —— 生命周期不同；
        #   * 加列要改所有 INSERT jobs 的地方（散落在 server.py / agent / 脚本里），
        #     漏一处就丢标记；独立表只有一处写入点；
        #   * 独立表**向后兼容**：老库不需要迁移，现有记录一条都不动。
        favorites.ensure_table(conn)


# ------------------------------------------------------------------
# 重启自愈：残留的 status='running'
# ------------------------------------------------------------------
#
# 【为什么需要这一步 —— A5 需求】
#
# 「运行中」原本只是一个**声明**（DB 里 status='running'）。
# 服务重启后：
#   * 进程里的 worker 没了（内存里的 WORKER.current 变成 None）；
#   * 但 SQLite 里那些 status='running' 的行**还在**。
# 于是生成库里的卡片永远显示「运行中」—— 而实际上**没有任何进程在跑它**。
# 用户看到的界面在骗人。
#
# 【修法】服务启动时（init_db 之后）做一次自愈：
#   1. 把 DB 里所有 status='running' 的行捞出来；
#   2. 逐条问 ComfyUI：「这个 prompt_id 还在你的队列里吗？还在历史里吗？」；
#   3. 按结果处置：
#        * 还在队列里 -> **接管**（起一个观察任务，等 ComfyUI 出结果）；
#        * 历史里已经有结果 -> **收尾**（落盘产物 + 标记 done/failed）；
#        * 都没有 -> **标记失败**并写清原因（不再假装在跑）。
#
# 【为什么放在启动时而不是每次列表请求时】
# 自愈是**改状态**的动作 —— 而 /api/jobs 是只读列表接口，
# 在那里改状态会让「刷新列表」变成「有副作用的写操作」（前端轮询极频繁）。
# 启动时做一次是幂等且安全的。
#
# 【为什么 ComfyUI 不可达时也要处理】
# 判定不了不等于「还在跑」。留着「运行中」就是留着那个 bug ——
# 所以明确标记失败 + 说明「ComfyUI 无响应，无法确认」。

def _residual_running_rows() -> list[dict]:
    """捞出 DB 里所有 status='running' 的行（重启后的残留）。"""
    with db() as conn:
        rows = conn.execute(
            "SELECT id, mode, params, prompt_id, started_at FROM jobs "
            "WHERE status='running' ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


async def _finalize_residual_with_outputs(job_id: str, row: dict,
                                          history_entry) -> None:
    """把「服务重启期间 ComfyUI 已经跑完」的任务收尾（落盘产物 + 标记终态）。

    失败也不抛 —— 收尾失败就把任务标成 failed（附带原因），
    绝不能让这一行继续挂在「运行中」。

    【2026-11-17 修复：自愈路径必须补跑接续收尾】
    实测缺陷（用户 19:00 反馈）：5 秒视频点「接续」再生成 5 秒，
    结果只得到 **4 秒**的一段视频，没有 9 秒成片。
    取证（线上 DB 全量对拍，2026-11-17）：
      * 22 条接续链里，**2 条** stage1 没有成片（is_full 缺失），
        且这 2 条的 stage1 **全部**落在「服务重启」窗口内
        （19:09:21 / 20:34:02）—— 重启打断的正是 stage1 的运行；
      * 另外 **20 条**没被重启打断的链，stage1 **全部**有成片。
    根因：服务重启时 worker 协程被杀掉，`_finalize_continuation`
    根本没跑 —— 这条路径只下载产物、标 status='done'，**不登记分段、
    不拼完整视频**。于是：
      * 本段分段文件**没被登记**（continuation.segment_rel = None）；
      * 成片**没被拼接**（没有 is_full 条目）；
      * 用户看到的就是「只有一段 4.25 秒的视频」。
    （4.25 秒 = 124 帧总长减去 22 帧钉住区 = 本段真正新生成的帧数 ——
     也就是说潜空间接续**本身是成功的**，只是成片没拼出来。）
    修法：自愈收尾也走一遍 `_finalize_continuation`（登记分段 + 拼成片）。
    """
    if not history_entry:
        with db() as conn:
            conn.execute(
                "UPDATE jobs SET status='failed', error=?, finished_at=? "
                "WHERE id=? AND status='running'",
                (running_truth.RESTART_STALE_REASON, time.time(), job_id))
        return
    saved = []
    try:
        async with ClientSession(timeout=HTTP_TIMEOUT) as session:
            for item in _extract_outputs(history_entry):
                fname = await comfy_download_output(session, item)
                saved.append({"kind": item["kind"], "file": fname,
                              "name": item["filename"]})
        annotate_outputs_with_size(saved)
    except Exception as exc:
        print(f"[自愈] 任务 {job_id} 产物下载失败: {exc}")
        with db() as conn:
            conn.execute(
                "UPDATE jobs SET status='failed', error=?, finished_at=? "
                "WHERE id=? AND status='running'",
                (f"服务重启后回收产物失败（{type(exc).__name__}）—— "
                 f"ComfyUI 已完成该任务，但产物未能取回，请重新提交生成。",
                 time.time(), job_id))
        return
    # 【2026-11-19 缺陷 2 修】与 worker **同一套**判定（失败优先）。
    # 这一条路径原来虽然也判 is_error，但**先下载产物、后判失败** ——
    # OOM 场景下 ComfyUI 的 outputs 里只有文本节点，下载得到空列表，
    # 靠 `not saved` 兜底才判失败。现在统一走 gen_outcome，
    # 口径与 worker / 批次 / 接管路径**完全一致**。
    outcome = gen_outcome.history_outcome(history_entry)
    if outcome["failed"]:
        reason = outcome["reason"]
        if not reason:
            reason = _comfy_error_message(outcome["status_info"])
        with db() as conn:
            conn.execute(
                "UPDATE jobs SET status='failed', error=?, finished_at=? "
                "WHERE id=? AND status='running'",
                (reason, time.time(), job_id))
        return
    # 【2026-11-19 缺陷 2 修】产物必须真实存在且非空（0 字节 = 全黑预览）。
    real_ok, problems = gen_outcome.outputs_are_real(saved, OUT_DIR)
    if not real_ok:
        print(f"[自愈] 任务 {job_id} 产物校验失败: {problems}")
        with db() as conn:
            conn.execute(
                "UPDATE jobs SET status='failed', error=?, finished_at=? "
                "WHERE id=? AND status='running'",
                (gen_outcome.missing_output_reason(problems), time.time(), job_id))
        return

    # 【2026-11-17 修复】接续段：自愈路径也要跑接续收尾。
    #
    # 【为什么必须在这里补】服务重启会杀掉 worker 协程，
    # `_finalize_continuation`（登记分段 + 拼完整视频）来不及跑。
    # 不补的话，接续段只会留下一段「自己那一小段」视频，
    # 没有完整成片 —— 用户看到的就是「5 秒接续 5 秒只得到 4 秒」。
    #
    # 【为什么不会重复跑】`_finalize_continuation` 幂等：
    #   * 登记分段 = 写本段自己的 continuation.segment_rel（同值覆写）；
    #   * 拼成片 = 按血缘重新拼一遍，产出**新的**成片文件
    #     （旧成片文件留在盘上，不影响正确性）。
    # 若该段已经跑过正常收尾（segment_rel 已登记），重跑只会
    # 多出一个内容相同的成片文件 —— 不改变用户看到的结果。
    mode = str(row.get("mode") or "")
    params = {}
    try:
        params = json.loads(row.get("params") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        params = {}
    try:
        saved = await _finalize_continuation_impl(job_id, mode, params, saved)
    except Exception as ce:
        # 拼接失败**不影响**本段产物 —— 与正常收尾路径同口径。
        print(f"[自愈] 任务 {job_id} 接续收尾失败（不影响本段产物）: {ce}")

    with db() as conn:
        conn.execute(
            "UPDATE jobs SET status='done', progress=1, outputs=?, finished_at=? "
            "WHERE id=? AND status='running'",
            (json.dumps(saved, ensure_ascii=False), time.time(), job_id))
    print(f"[自愈] 任务 {job_id} 已回收 {len(saved)} 个产物并标记成功")


async def _adopt_residual_running(job_id: str, row: dict) -> None:
    """接管「服务重启后仍在 ComfyUI 队列里」的任务。

    起一个后台协程，等 ComfyUI 出结果后收尾；同样有超时上限。
    它**不占用 worker 的串行队列**（worker 只处理 status='queued'），
    所以不会挡住其它任务。

    【2026-11-19 缺陷 1 修】上限按**媒体类型**取：视频类默认 0
    （不按时长判失败）—— 视频跑很久是正常现象。
    """
    prompt_id = row.get("prompt_id") or ""
    started_at = row.get("started_at") or time.time()
    # 【2026-11-19 缺陷 1 修】按媒体类型取上限（视频类默认不限时）。
    job_media = job_timeout.media_of_mode(str(row.get("mode") or ""))
    adopt_timeout_sec = timeout_sec_for_media(job_media)
    try:
        async with ClientSession(timeout=HTTP_TIMEOUT) as session:
            deadline = started_at + (adopt_timeout_sec or 1e9)
            while True:
                if time.time() > deadline:
                    reason = timeout_reason_for(adopt_timeout_sec, job_media)
                    with db() as conn:
                        conn.execute(
                            "UPDATE jobs SET status='failed', error=?, finished_at=? "
                            "WHERE id=? AND status='running'",
                            (reason, time.time(), job_id))
                    print(f"[自愈] 接管的任务 {job_id} 超过运行上限，已标记失败")
                    return
                await asyncio.sleep(CFG["poll_interval"])
                with db() as conn:
                    st = conn.execute("SELECT status FROM jobs WHERE id=?",
                                      (job_id,)).fetchone()
                if not st or st["status"] != "running":
                    return   # 用户取消了 / 别的流程已经收尾
                hist = await comfy_history(session, prompt_id)
                if not hist:
                    continue
                # 【2026-11-19 缺陷 2 修】与 worker **同一套**判定：
                # 失败优先于产物（OOM 时 outputs 里也会有已执行完节点的输出）。
                outcome = gen_outcome.history_outcome(hist)
                if outcome["failed"]:
                    reason = outcome["reason"]
                    if not reason:
                        reason = _comfy_error_message(outcome["status_info"])
                    with db() as conn:
                        conn.execute(
                            "UPDATE jobs SET status='failed', error=?, finished_at=? "
                            "WHERE id=? AND status='running'",
                            (reason, time.time(), job_id))
                    return
                if outcome["pending"]:
                    # status 还没落定 -> 还在跑，继续等。
                    continue
                if True:
                    outs = _extract_outputs(hist)
                    saved = []
                    for item in outs:
                        fname = await comfy_download_output(session, item)
                        saved.append({"kind": item["kind"], "file": fname,
                                      "name": item["filename"]})
                    # 【2026-11-19 缺陷 2 修】产物必须真实存在且非空。
                    real_ok, problems = gen_outcome.outputs_are_real(
                        saved, OUT_DIR)
                    if not real_ok:
                        print(f"[自愈] 任务 {job_id} 产物校验失败: {problems}")
                        with db() as conn:
                            conn.execute(
                                "UPDATE jobs SET status='failed', error=?, "
                                "finished_at=? WHERE id=? AND status='running'",
                                (gen_outcome.missing_output_reason(problems),
                                 time.time(), job_id))
                        return
                    annotate_outputs_with_size(saved)

                    # 【2026-11-17 修复】接续段：接管路径也要跑接续收尾。
                    #
                    # 【为什么必须补】服务重启会杀掉 worker 协程，
                    # `_finalize_continuation`（登记分段 + 拼完整视频）
                    # 来不及跑。接管路径原来只下载产物、标 done ——
                    # 于是接续段只剩「自己那一小段」视频，没有完整成片
                    # （用户实测：5 秒接续 5 秒只得到 4 秒）。
                    mode = str(row.get("mode") or "")
                    params = {}
                    try:
                        params = json.loads(row.get("params") or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        params = {}
                    try:
                        saved = await _finalize_continuation_impl(
                            job_id, mode, params, saved)
                    except Exception as ce:
                        print(f"[自愈] 任务 {job_id} 接续收尾失败"
                              f"（不影响本段产物）: {ce}")

                    with db() as conn:
                        conn.execute(
                            "UPDATE jobs SET status='done', progress=1, outputs=?, "
                            "finished_at=? WHERE id=? AND status='running'",
                            (json.dumps(saved, ensure_ascii=False),
                             time.time(), job_id))
                    print(f"[自愈] 接管的任务 {job_id} 已完成，回收 {len(saved)} 个产物")
                    return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[自愈] 接管的任务 {job_id} 失败: {exc}")
        with db() as conn:
            conn.execute(
                "UPDATE jobs SET status='failed', error=?, finished_at=? "
                "WHERE id=? AND status='running'",
                (f"服务重启后接管该任务失败（{type(exc).__name__}）—— "
                 f"请重新提交生成。", time.time(), job_id))


async def _heal_residual_running() -> dict:
    """服务启动时的「残留 running」自愈（见上面的说明）。

    返回统计：{"healed": n, "failed": n, "adopted": n, "finalized": n}
    """
    report = {"healed": 0, "failed": 0, "adopted": 0, "finalized": 0}
    rows = _residual_running_rows()
    if not rows:
        return report
    print(f"[自愈] 发现 {len(rows)} 条重启前残留的「运行中」任务，开始核对 ComfyUI")
    queue_ids = None
    history_cache: dict[str, object] = {}
    try:
        async with ClientSession(timeout=ClientTimeout(total=8)) as session:
            queue_ids = await fetch_comfy_queue_ids(session)
            for row in rows:
                job_id = row["id"]
                prompt_id = row.get("prompt_id") or ""
                history_entry = None
                if prompt_id:
                    if prompt_id not in history_cache:
                        try:
                            history_cache[prompt_id] = await comfy_history(
                                session, prompt_id)
                        except Exception:
                            history_cache[prompt_id] = None
                    history_entry = history_cache[prompt_id]
                plan = running_truth.plan_residual_running(
                    prompt_id=prompt_id,
                    history_entry=history_entry,
                    queue_ids=queue_ids,
                    started_at=row.get("started_at"),
                    now=time.time(),
                )
                action = plan["action"]
                if action == "fail":
                    with db() as conn:
                        conn.execute(
                            "UPDATE jobs SET status='failed', error=?, finished_at=? "
                            "WHERE id=? AND status='running'",
                            (plan["reason"], time.time(), job_id))
                    report["failed"] += 1
                    print(f"[自愈] 任务 {job_id} 已标记失败：{plan['reason']}")
                elif action == "finalize":
                    # ComfyUI 那边**已经跑完了**（服务重启期间出的结果）。
                    # 把产物落盘并收尾 —— 不能白扔一次已经算好的生成。
                    await _finalize_residual_with_outputs(job_id, row, history_entry)
                    report["finalized"] += 1
                elif action == "adopt":
                    # 任务**还在 ComfyUI 队列里** -> 接管它。
                    # 不接管 = 任务永远卡在「运行中」（这正是要修的坑）。
                    asyncio.create_task(_adopt_residual_running(job_id, row))
                    report["adopted"] += 1
                    print(f"[自愈] 任务 {job_id} 仍在 ComfyUI 队列中，已接管等待结果")
    except Exception as exc:
        # 自愈本身失败**不能**让服务起不来 —— 只打印日志。
        print(f"[自愈] 核对 ComfyUI 失败（残留 running 保持原状）: {exc}")
    report["healed"] = report["failed"] + report["finalized"] + report["adopted"]
    return report


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


def _continuation_info_for_row(r, job_params: dict) -> dict:
    """生成库卡片用的「接续状态」—— 在协议层判定之上补**成片真实长度**。

    【2026-11-18 第 6 轮审查修 —— 显示值 ≠ 真实产物（P1）】

    原实现直接下发 `continuation_service.continuation_state(...)` 的
    `total_frames` / `total_seconds`。那个值是「链上累计**交付**帧数」
    （= 各段帧数之和，**不含成片尾裁**），而成片文件在拼接时做了尾裁
    （`_concat_chain_video` 的 `trim_tail_frames`，目的是让成片帧数精确
    命中用户口径「Σ请求时长 − (段数−1)×1 秒」）。

    ⇒ 于是卡片/接续面板显示的累计时长**比真实成片长**（正好差尾裁帧数）：
      线上实测（2026-11-18 尾裁上线后的真实链 e451d93897e6）：
        显示 continuation_info.total_seconds = 9.417s（226 帧）
        真实成片 v-h3-studio-000531.mp4 = **216 帧 = 9.000s**
        差 10 帧 —— 与「尾裁 10 帧」完全吻合。

    【为什么不能直接把 protocol 层的 total_frames 改成尾裁后口径】
    `continuation_state` 的 `total_frames` 有两个**不可改**的下游：
      1. `server._prepare_continuation_params` 拿它当**音频偏移** C_n
         （口径：第 n 段之前已累计交付的帧数）。尾裁只砍**最后一段尾部**，
         永远不会影响任何一段的**起始**位置 —— 若把 C_n 改成尾裁后的值，
         后面每一段的参考音频都会偏移。
      2. 它被写进下一段的 `continuation.chain_total_frames`，继续往后累加
         （同一理由）。
    ⇒ 所以**协议层的口径不动**，只在**展示层**补一个「成片真实长度」。

    【口径 —— 尾裁只砍最后一段】
    尾裁量由 `cont.chain_compensation_plan` 算（与拼接时同一套函数、
    同一份数据），所以显示值与成片文件**必然一致**。
    链首段（stage 0）没有成片拼接这一步，不裁。
    """
    state = cont_service.continuation_state(r["mode"], job_params, r["id"])
    try:
        # 【2026-11-21 性能修】把本段的 params 传下去 ——
        # 血缘回溯本来就要读它，调用方手里已经有了，不必再查一次库。
        durations = _lineage_durations(r["id"], own_params=job_params)
    except Exception:
        durations = []
    if len(durations) >= 2:
        try:
            window = cont.snap_context_frames(int(
                (cont_service.chain_info(job_params) or {}).get("context_frames")
                or cont.DEFAULT_CONTEXT_FRAMES))
        except (TypeError, ValueError):
            window = cont.DEFAULT_CONTEXT_FRAMES
        try:
            plan = cont.chain_compensation_plan(durations, window)
            keep = [int(x) for x in (plan.get("keep_frames") or [])]
            if keep:
                final_frames = sum(keep)
                state["total_frames"] = final_frames
                state["total_seconds"] = round(final_frames / cont.FPS, 3)
                # 未尾裁的累计值也留着（诊断用）：它就是 C_n 的来源。
                state["raw_total_frames"] = int(plan.get("current_frames") or 0)
                state["raw_total_seconds"] = round(
                    int(plan.get("current_frames") or 0) / cont.FPS, 3)
                state["trimmed_frames"] = int(plan.get("trim_frames") or 0)
        except Exception:
            # 算不出来就保留协议层的值（不静默给 0，也不让整个列表挂掉）
            pass
    return state


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
        "continuation_info": _continuation_info_for_row(r, job_params),
        "status": r["status"],
        # 【2026-11-14 A5】prompt_id 必须下发：实时判定「任务是不是真的还在
        # ComfyUI 里跑」**必须**拿它去查 /queue。
        # （ComfyUI 的队列项里 prompt_id 在下标 1，见 running_truth 模块说明。）
        #
        # 【为什么要 keys() 兜底】测试里经常直接构造**精简结构**的行
        # （只挑几列）—— 与下面 session_id 的处理同一个道理：
        # 缺列就当空串，绝不能 KeyError（那会让「读一条任务」直接崩）。
        "prompt_id": (r["prompt_id"] if "prompt_id" in r.keys() else "") or "",
        "progress": r["progress"],
        "progress_text": r["progress_text"],
        "error": r["error"],
        "outputs": outputs,
        # 【2026-11-14 A5】「这条任务**真的**在跑吗」的唯一真源（服务端算好）。
        #
        # 【为什么必须由服务端算】前端原来只看 job.status 就渲染「运行中」——
        # 而 status 是 worker 写进去的**声明**，不是观测：
        #   * 服务重启后 DB 里残留 status='running' -> 卡片永远转圈；
        #   * ComfyUI 侧任务已被取消/丢失 -> worker 永远等 -> 永远 running。
        # 现在服务端把「状态 / 已跑多久 / 运行上限 / 超时时刻」一起下发，
        # 前端据此兜底显示「已超时 / 状态未知」，**不再自己瞎猜时间**。
        "running_truth": running_truth.job_running_truth(
            status=r["status"],
            started_at=(r["started_at"] if "started_at" in r.keys() else None),
            now=time.time(),
            # 【2026-11-19 缺陷 1 修】上限按**媒体类型**取：视频类默认 0
            # （不按时长判失败），图像 / 音频保留可配置上限。
            timeout_sec=timeout_sec_for_mode(mode),
        ),
        # 运行上限（秒）也单独下发一份 —— 前端队列面板要显示「上限」，
        # 且老前端（不认识 running_truth）也能用它做最朴素的兜底。
        "running_timeout_sec": timeout_sec_for_mode(mode),
        # 【2026-11-11】卡片显示用的分辨率 / 比例标签（服务端算好的唯一真源）
        "resolution_info": resolution_info,
        "elapsed": (r["finished_at"] or time.time()) - r["started_at"] if r["started_at"] else None,
        # 【2026-11-14 A5】开始时间要下发：前端据此算「已跑多久」，
        # 并且在**没有** running_truth 字段时也能自己做最朴素的兜底判定。
        "started_at": (r["started_at"] if "started_at" in r.keys() else None),
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
    return [_batch_row_to_dict(r) for r in rows]


def _batch_row_to_dict(r) -> dict:
    """job_batches 的一行 -> 批次字典（batches_of / batches_map 共用）。"""
    return {
        "id": r["id"],
        "idx": r["idx"],
        "seed": r["seed"],
        "status": r["status"],
        "progress": r["progress"],
        "progress_text": r["progress_text"],
        "error": r["error"],
        "outputs": json.loads(r["outputs"] or "[]"),
        "elapsed": (r["finished_at"] or time.time()) - r["started_at"] if r["started_at"] else None,
    }


def batches_map(job_ids: list[str]) -> dict[str, list[dict]]:
    """**一次**查询取回多条任务的批次，返回 {job_id: [批次, ...]}。

    【为什么需要它 —— 2026-11-21 生成媒体库性能修】
    `batches_of()` 是**每条任务一次**查询。列表接口一次返回 100 条
    -> 100 次 SELECT（实测 SQL 计数：page_size=100 时 213 条 SQL，
    其中 100 条是 `SELECT * FROM job_batches WHERE job_id=?`）。
    任务多了以后这条 N+1 就是列表接口的主要固定成本。

    【口径与 batches_of 完全一致】同一张表、同样的字段、同样的
    `ORDER BY idx`、同样的 `elapsed` 计算 —— 只是把 N 次查询
    合并成 1 次（`WHERE job_id IN (...)`）。
    """
    if not job_ids:
        return {}
    out: dict[str, list[dict]] = {}
    # 分批查（SQLite 变量数有上限，1000 是稳妥值）
    chunk = 900
    for i in range(0, len(job_ids), chunk):
        part = job_ids[i:i + chunk]
        marks = ",".join("?" * len(part))
        with db() as conn:
            rows = conn.execute(
                "SELECT * FROM job_batches WHERE job_id IN (%s) ORDER BY job_id, idx"
                % marks, part).fetchall()
        for r in rows:
            out.setdefault(r["job_id"], []).append(_batch_row_to_dict(r))
    return out


#: 【2026-11-21 性能修】一批任务里要生成缩略图的产物总数上限。
#:
#: 【为什么有上限】缩略图生成是 CPU/IO 密集（视频 ~140ms 一次 ffmpeg）。
#: 一页 48 张 = 48 次生成 ≈ 6.7 秒 —— 会让**列表接口本身**变慢，
#: 那就变成了「为了快而更慢」。超出上限的部分**不给 thumb 字段**，
#: 前端自动回落原图（功能不降级，只是那几张没加速）。
#: 正常每页 12 张 ≈ 1.7 秒，且第二次起命中磁盘缓存几乎免费。
_THUMB_BUDGET = 48


async def _attach_thumb_urls(jobs: list[dict]) -> None:
    """给每条产物的 outputs[] 补 `thumb` 字段（缩略图 URL）。

    【为什么必须有 —— 实测 2026-11-21】
    卡片原本直接引用原始产物。CDP `Network.dataReceived` 实测一页 12 张卡
    拉了 **8.75MB** 媒体字节，其中 11 个视频占 **7.81MB**
    （Chrome 对 `<video preload="metadata">` 发 `Range: bytes=0-`，
    aiohttp 的 FileResponse 对 `bytes=0-` 返回**整个文件**）。
    图片原图 1.1MB~9.5MB。

    【缩略图实测】图片 ~30ms / 视频 ~140ms，产物 14~43KB
    ⇒ 一页媒体字节 8.75MB -> ~0.3MB（约 30 倍）。

    【关键：这里**只算 URL，不生成缩略图】】
    第一版把「生成缩略图」放进列表接口，结果**列表接口自己**被拖到
    1546~2897ms（12 张卡 = 11 次 ffmpeg）—— 那就变成了
    「为了媒体字节少，把列表接口搞慢」，正好是反的。

    现在的分工：
      * 列表接口：只 `stat` 一次算键（亚毫秒）-> 下发 URL；
      * `/api/thumb`：浏览器请求时才生成（走线程 + 并发闸）。
    列表接口保持 ~4.5ms，缩略图生成与浏览器的渲染**并行**。

    【失败处理】生成不出来（ffmpeg 缺失 / 文件已删 / 格式怪）
    -> `/api/thumb` 返回 404 -> 浏览器不显示缩略图。
    前端此时回落引用原图（功能不降级）。

    【不阻塞】只有一次 `stat`，同步做也没问题（微秒级）。
    """
    for job in jobs:
        # 主产物 + 批次产物都要缩略图（批次产物也是原图，展开一次可能十几张）
        out_groups = [job.get("outputs") or []]
        for batch in (job.get("batches") or []):
            out_groups.append(batch.get("outputs") or [])
        for outs in out_groups:
            for out in outs:
                rel = str(out.get("file") or "").replace("\\", "/")
                if not rel:
                    continue
                ext = Path(rel).suffix.lower()
                if ext not in thumb_service.IMAGE_EXTS \
                        and ext not in thumb_service.VIDEO_EXTS:
                    continue
                # 产物文件可能是 "<job_id>/xxx" 或 "images/xxx" 两种形态
                full = OUT_DIR / rel
                if not full.is_file():
                    alt = OUT_DIR / job.get("id", "") / rel
                    if not alt.is_file():
                        continue
                    full = alt
                try:
                    key = thumb_service.cache_key(full)
                except OSError:
                    continue
                # 记「键 -> 源文件」索引（`/api/thumb` 要靠它反查源文件）
                _remember_thumb_source(key, full)
                # URL 里带 k=<键>：源文件换了内容 -> 键变 -> URL 变 ->
                # 浏览器不会拿旧缓存（URL 是内容寻址的，源文件变了 URL 就变）。
                out["thumb"] = f"/api/thumb/{key}.jpg"


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


async def _extract_video_soundtrack(session: ClientSession, job_id: str,
                                    video_name: str) -> str | None:
    """从参考视频里抽出音轨，上传到 ComfyUI input，返回文件名。

    【为什么需要这一步 —— 缺口 2 的后半段】
    音轨三选一的 (c)「使用参考视频的背景音乐作为成片音轨」，
    在 T8 节点里对应的是 ``ref_video_audios`` 槽位 ——
    **那是一个音频输入**，不是视频输入。节点不会自己从视频里抽音轨
    （它只要求「第 N 个背景音配第 N 个参考视频」）。

    所以「用户选了一个带音轨的参考视频」要变成 (c) 可用，
    必须由我们把视频里的音轨**抽出来**单独喂给那个槽位。

    【抽不出音轨怎么办】视频本来就没有音轨（无声视频很常见），
    这时返回 None —— 上层会把 (c) 归回「使用生成的音乐」，
    **不报错**：用户想要的是视频，音轨退化成默认即可
    （与 h3studio/h3_audio_track.py 的归一策略一致）。

    【为什么用 ffmpeg 而不是 VHS 节点】VHS_LoadVideo 的 audio 输出
    是个 LazyAudioMap，必须挂在 ComfyUI 图里；我们要的是
    一个**独立文件**（因为节点要求音轨与视频是**两个**槽位）。
    """
    if not FFMPEG:
        return None
    # ComfyUI 侧的文件名 -> 本地路径（input 目录）
    local = None
    for folder in (Path("/home/ubuntu/comfyui/input"),
                   Path("/root/ComfyUI/input")):
        candidate = folder / video_name
        if candidate.exists():
            local = candidate
            break
    if local is None:
        return None
    out_dir = UPLOAD_DIR / "soundtracks"
    out_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(video_name.encode("utf-8")).hexdigest()[:16]
    out_path = out_dir / f"sb-{digest}.m4a"
    if not out_path.exists():
        # -vn 丢掉视频流；没有音轨时 ffmpeg 会失败 -> 返回 None
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-y", "-i", str(local), "-vn", "-c:a", "aac",
            "-b:a", "192k", str(out_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE)
        _out, err = await proc.communicate()
        if proc.returncode != 0 or not out_path.exists():
            detail = (err or b"").decode("utf-8", "ignore")[-300:]
            print(f"[soundtrack] 抽音轨失败（{video_name}）：{detail}")
            return None
    return await comfy_upload_media(session, out_path)


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


def _comfy_error_message(status_info) -> str:
    """把 ComfyUI 的执行错误变成**给用户看的中文原因**。

    【为什么需要这个包装 —— 2026-11-12 独立复验发现的缺陷】
    原实现把 `status.messages` 的条目当「`m[0]` 是字典」处理：

        (m or [{}])[0].get("x", "")

    但 ComfyUI 的真实形状是 **`[事件名, 数据字典]`** —— `m[0]` 是字符串
    `"execution_error"`。于是 `.get()` 作用在 str 上抛 AttributeError，
    真正的原因（例：拷贝桥禁止跨分辨率）**被彻底丢掉**，用户只看到
    一句 `'str' object has no attribute 'get'`。

    【2026-11-13 补】本函数**对类型免疫**：
      * `status_info` 传进来是 str / None / list 也不崩 ——
        走「未取到具体原因」的兜底文案；
      * 兜底文案里**不出现**原始 Python 异常文本。

    提取与中文化逻辑放在 h3studio/comfy_errors.py（纯函数，可离线测）。
    本函数只负责：**取中文原因 + 把技术细节打进服务端日志**。
    """
    try:
        described = comfy_errors.extract_error(status_info)
    except Exception as exc:
        # 提取过程本身出错（形状超预期）-> 也不能把异常文本给用户。
        print(f"[worker] ComfyUI 错误提取失败: {exc!r}")
        return comfy_errors.UNPARSED_ERROR_REASON
    if not described.get("found"):
        return comfy_errors.UNPARSED_ERROR_REASON
    # 技术细节只进日志，不进用户可见文案。
    print(f"[worker] ComfyUI 错误详情: {described.get('detail', '')}")
    return str(described.get("reason") or "ComfyUI 执行出错。")


#: 兜底失败文案（用户可见）。
#:
#: 【为什么统一成一句话】用户遇到「生成失败」时需要知道的是
#: **下一步做什么**，而不是一串 Python 异常。不同异常的根因差别很大，
#: 但对用户而言可执行的动作是一样的：**看服务端日志 / 调整参数重试**。
_GENERIC_FAILURE_REASON = ("生成失败（服务端未能给出具体原因）。"
                            "请稍后重试；若反复失败，请查看服务端日志，"
                            "或改用「复用参数」重新生成一段。")


def _history_status(hist) -> tuple[dict, bool, bool]:
    """从一条 ComfyUI history 条目里取出 `(status_info, 是否失败, 状态是否畸形)`。

    【为什么抽成函数 —— 2026-11-13】
    这个判断原本**内联**在 worker 的轮询循环里。它一旦被改回
    `hist.get("status") or {}` + `status_info.get("status_str")`，
    就只有「真跑一次 ComfyUI」才能发现（而且是在**失败路径**上，
    平时根本走不到）。

    抽成独立函数后：
      * 单元测试可以直接喂各种畸形 history（status 是 str / None /
        list / 缺字段）并断言「不抛异常」；
      * worker 里只剩一行调用，类型判断**只此一处**。

    【三个返回值】
      * `status_info`  —— 归一后的 dict（畸形时是空 dict）；
      * `is_error`     —— `status_str == "error"` -> 判定为执行失败；
      * `malformed`    —— `status` **存在但不是 dict**（形状超预期）。

    【为什么要有第三个返回值 —— 2026-11-13 修】
    `status` 畸形时，`is_error` 必然是 False（没有 status_str 可读）。
    如果 worker 只看 `is_error`，就会**永远轮询下去** ——
    既没有 outputs（没成功）也没有 error（不是失败），
    循环没有任何退出条件 -> 任务永远卡在「生成中」。

    这不是理论风险：畸形 history 会让任务**挂死**，比报错更糟。
    所以畸形必须**当成失败处理**（快速失败 + 可读原因），
    而不是当成「还在跑」。

    【本机实测的正常形状】ComfyUI 0.37 的 history 里 `status` **总是**
    dict（`status_str` / `completed` / `messages` 三个字段），
    实测 5 条历史全部如此。畸形只可能来自：
      * 中间代理/网关改了响应；
      * 未来 ComfyUI 改了形状；
      * 测试构造的假数据。
    但「万一」也必须能优雅降级 —— 不能挂死。
    """
    raw_status = (hist or {}).get("status") if isinstance(hist, dict) else None
    # 畸形 = status **存在**但不是 dict。
    #
    # ⚠ `status` 是 None 的情况**不算畸形** —— ComfyUI 在任务还没跑完时
    #    就会返回 `status: None`（execution.py 的 status_dict 初值就是
    #    None，task_done 时才填成 dict）。那是「**还在跑**」，
    #    不是「形状坏了」。
    #
    #    之前把 None 也当畸形是**错的**：会让所有「刚提交、还没跑完」
    #    的任务立刻失败。
    malformed = raw_status is not None and not isinstance(raw_status, dict)
    status_info = comfy_errors.coerce_status_info(raw_status)
    return status_info, comfy_errors.status_is_error(status_info), malformed


def _user_facing_error(exc: BaseException | str) -> str:
    """把任意异常变成**给用户看的中文文案**（不泄露原始 Python 异常文本）。

    【为什么需要 —— 2026-11-13 修】

    worker 的兜底是 `except Exception as e: ... (str(e)[:800], ...)` ——
    把**原始异常文本**直接写进 `jobs.error`，前端又原样显示。
    于是用户看到的是：

        'str' object has no attribute 'get'
        ValueError: ...

    这类文本对用户**零信息量** —— 它是本项目的 bug，不是用户能修的东西。
    用户真正需要知道的是「**发生了什么、接下来怎么办**」。

    【本函数的口径】
      * 已经是 RuntimeError 且带中文原因（来自 _comfy_error_message）
        -> 原样保留（那是**已经中文化过**的原因）；
      * 其余异常 -> 归类成一句可读中文 + 给出可执行建议；
      * 原始异常文本**只进服务端日志**，不进用户文案。

    【为什么不吞掉信息】技术细节（异常类型 + 原文）照样写日志，
    只是不再摆到用户脸上 —— 需要排查时看日志即可。
    """
    text = str(exc).strip()
    # 已经中文化过的原因（RuntimeError 里带中文标点/中文词）-> 直接用。
    if isinstance(exc, RuntimeError) and text and _looks_chinese(text):
        return text[:800]
    # 空异常 -> 通用文案
    if not text:
        return _GENERIC_FAILURE_REASON
    # 原始异常文本 -> 进日志，返回可读中文
    print(f"[worker] 原始异常（仅日志）: {type(exc).__name__}: {text[:800]}")
    return _GENERIC_FAILURE_REASON


def _looks_chinese(text: str) -> bool:
    """文本里有没有中文（用来判断「是不是已经中文化过的原因」）。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


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
    # 【2026-11-14】参考视频的背景音单独一个槽位 ——
    # 音轨三选一的 (c) 项要用它。
    # 以前它被塞进 ref_audios（和独立参考音乐混在一起），
    # 于是「用参考音乐」与「用参考视频背景音」分不清。
    "ref_video_audio": "ref_video_audios",
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
    # 【2026-11-14】参考视频背景音要按**序号**与参考视频配对
    #（节点的 ref_video_audio_N 必须对应 ref_video_N，否则会报
    #  "Reference-video soundtrack(s) have no same-numbered video"）。
    # 所以这里单独记一份带 ordinal 的列表，按 ordinal 排序后回填。
    video_audio_with_ordinal: list[tuple[int, str]] = []
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
            if key == "ref_video_audios":
                video_audio_with_ordinal.append(
                    (int(item.get("ordinal") or 0), uploaded))
            # 登记引用关系（引用次数因此 +1；删除素材时会被 409 保护）
            _repo.add_ref(conn, asset_id, "job", job_id, slot=slot,
                          ordinal=int(item.get("ordinal") or 0),
                          label=f"任务 {job_id} 的 {slot} 输入")

    for key, values in grouped.items():
        if key in ("first_frame", "last_frame"):
            params[key] = values[0]
        else:
            params[key] = values
    # 参考视频背景音：按 ordinal 排序（与参考视频的顺序一致）
    if video_audio_with_ordinal:
        video_audio_with_ordinal.sort(key=lambda pair: pair[0])
        params["ref_video_audios"] = [name for _ordinal, name
                                      in video_audio_with_ordinal]


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


async def save_uploaded_media(part) -> str:
    """保存上传的视频 / 音频；内容相同则复用 uploads 中已有文件。

    【为什么不能复用 save_uploaded_image】
    那个函数按**图片后缀白名单**处理（.png/.jpg/…），遇到 .mp4 会
    强制改名成 .png —— 文件内容还是视频，扩展名却成了图片，
    下游 ffmpeg / ComfyUI 按后缀判断类型就会炸。

    这里保留原始后缀（白名单：视频 + 音频常见格式），其余原样落盘。
    """
    suffix = Path(part.filename or "media.bin").suffix.lower()
    if suffix not in MEDIA_SUFFIXES:
        suffix = ".bin"
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
    canonical = UPLOAD_DIR / f"media-{fingerprint[:20]}{suffix}"
    if canonical.exists():
        temp.unlink(missing_ok=True)
        return canonical.name
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
        if mode in ("t2i", "t2i_qwen", "i2i_qwen", "t2i_moss",
                    "t2i_h3image", "i2i_h3image") and self._has_batches(job_id):
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
                    # 【2026-11-15 补齐 —— 缺口 1 的 flf 半边】
                    # 参考视频 / 参考音乐的**本地直传**上传到 ComfyUI input。
                    #
                    # 【为什么必须在这里做】上面的 prepare_job_params 只是把
                    # 上传目录里的文件名写进 params；ComfyUI 的 LoadVideo /
                    # LoadAudio 节点只认 ComfyUI input 里的名字。
                    # r2v 分支早就做了这件事，flf 分支没做 ——
                    # 于是 flf 的本地参考视频/音乐即使提交成功，
                    # 到了工作流那一步也会「找不到文件」。
                    if params.get("ref_videos"):
                        uploaded = []
                        for up in params["ref_videos"]:
                            lp = UPLOAD_DIR / up
                            if not lp.exists():
                                raise RuntimeError(f"找不到上传文件: {up}")
                            uploaded.append(await comfy_upload_media(session, lp))
                        params["ref_videos"] = uploaded
                    if params.get("ref_audios"):
                        uploaded = []
                        for up in params["ref_audios"]:
                            lp = UPLOAD_DIR / up
                            if not lp.exists():
                                raise RuntimeError(f"找不到上传文件: {up}")
                            uploaded.append(await comfy_upload_media(session, lp))
                        params["ref_audios"] = uploaded
                elif mode == "r2v":
                    names = []
                    for up in params.get("ref_images", []):
                        lp = UPLOAD_DIR / up
                        if not lp.exists():
                            raise RuntimeError(f"找不到上传文件: {up}")
                        names.append(await comfy_upload_image(session, lp))
                    params["ref_images"] = names
                    # 【2026-11-15】参考视频 / 参考音乐的**本地直传**上传。
                    # （缺口 1：面板补了槽位 -> 这里必须有对应的上传路径。）
                    if params.get("ref_videos"):
                        uploaded = []
                        for up in params["ref_videos"]:
                            lp = UPLOAD_DIR / up
                            if not lp.exists():
                                raise RuntimeError(f"找不到上传文件: {up}")
                            uploaded.append(await comfy_upload_media(session, lp))
                        params["ref_videos"] = uploaded
                    if params.get("ref_audios"):
                        uploaded = []
                        for up in params["ref_audios"]:
                            lp = UPLOAD_DIR / up
                            if not lp.exists():
                                raise RuntimeError(f"找不到上传文件: {up}")
                            uploaded.append(await comfy_upload_media(session, lp))
                        params["ref_audios"] = uploaded
                elif mode in ("i2i_qwen", "i2i_h3image"):
                    # Qwen 图片编辑：参考图**来自素材库**（asset_id + 版本快照），
                    # 这里把素材库里的文件上传到 ComfyUI input，
                    # LoadImage 节点按返回的 'subfolder/name' 引用。
                    # 顺序必须与占位标记 <imageN> 严格一致 —— 这里保持列表原序。
                    #
                    # 【2026-11-13】H3-Image 图片编辑复用同一条上传路径
                    # （它的参考图也强制来自素材库，顺序即 <Picture N> 语义）。
                    names = await _upload_asset_refs(session, job_id, params)
                    params["ref_images"] = names

                # 素材库引用：把素材库里的文件按槽位上传到 ComfyUI，
                # 并在 params 里落成 ref_images / ref_videos / ref_audios。
                # i2i_qwen / i2i_h3image 的参考图已经在上面按占位顺序处理过了，
                # 这里不能再覆盖（会打乱占位与插槽的对应关系）。
                if params.get("asset_refs") and mode not in ("i2i_qwen", "i2i_h3image"):
                    await _apply_asset_refs(session, job_id, params)
                    # 【2026-11-14】首尾帧：素材库选的首帧 / 尾帧
                    # 已在 _apply_asset_refs 里上传成 ComfyUI 文件名，
                    # 这里补上「本地直接上传」那条路径（两者取其一）。
                    if mode == "flf":
                        for key in ("first_frame", "last_frame"):
                            fname = params.get(key)
                            if not fname:
                                continue
                            lp = UPLOAD_DIR / fname
                            if not lp.exists():
                                # 已经是 ComfyUI 侧的上传名（素材库路径）-> 跳过
                                continue
                            params[key] = await comfy_upload_image(session, lp)

                # 【2026-11-15 —— 缺口 2 后半段：参考视频的背景音】
                #
                # 音轨三选一的 (c) 对应 T8 节点的 `ref_video_audios` 槽位，
                # 那是一个**独立的音频输入** —— 节点不会自己从参考视频里
                # 抽音轨。所以「用户选了一个带音轨的参考视频」要变成 (c)
                # 真的可用，必须在这里把音轨**抽出来**。
                #
                # 只有当用户确实选了 (c) 时才抽 —— 抽音轨要跑 ffmpeg，
                # 不选就不该付这个开销。
                if mode in ("flf", "r2v"):
                    _track = params.get("audio_track_source")
                    if _track == h3audio.TRACK_VIDEO_BG:
                        _videos = list(params.get("ref_videos") or [])
                        _existing = list(params.get("ref_video_audios") or [])
                        _soundtracks = list(_existing)
                        for _idx, _vname in enumerate(_videos):
                            # 第 N 个背景音配第 N 个参考视频（节点硬要求）
                            if _idx < len(_existing):
                                continue
                            _sb = await _extract_video_soundtrack(
                                session, job_id, _vname)
                            if _sb:
                                _soundtracks.append(_sb)
                            else:
                                print(f"[soundtrack] 参考视频 {_vname} 没有音轨，"
                                      "该项回落到生成的音乐")
                                break
                        params["ref_video_audios"] = _soundtracks
                        if not _soundtracks:
                            # 抽不出音轨 -> (c) 不可用 -> 归回生成的音乐
                            params["audio_track_source"] = (
                                h3audio.normalize_track_source(
                                    _track, has_reference_audio=True,
                                    has_reference_video_audio=False))

                # 【2026-11-16 新增】记录**链首段**的原始音轨文件。
                #
                # 【为什么必须在 worker 里做】
                # 原始音轨的 ComfyUI 文件名这时才确定（素材库引用
                # 要到 _apply_asset_refs 才落成 ref_audios /
                # ref_video_audios）。而下一段接续时需要：
                #   * 原始音轨文件（切片用 + 成片贴轨用）
                #   * 原始音轨总时长（算越界告警）
                #
                # 【为什么记「原始整条」而不是「段切片」】
                # 成片音轨必须是原始整条 —— 每段 1s 空白若被累加
                # 到成片里，3 段就是 3s 空白，音画必然错位。
                if mode in ("flf", "r2v"):
                    await self._record_chain_audio_source(job_id, params)

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

                # 【H3-Image 同理】节点可用性必须在**拼工作流前**确认，
                # 否则用户提交后才收到 ComfyUI 的英文原文报错
                # （"Node 'MiniMaxH3ImageConditioning' not found"）——
                # 用户不知道那是 ComfyUI 版本不够，更不知道该升级还是装插件。
                # 这里探测一次并把结果注入 params，由 h3image 的构建函数
                # 转成中文错误。
                if mode in ("t2i_h3image", "i2i_h3image"):
                    nodes_ok, probe_ok = await probe_h3_image_nodes(session)
                    # 只有「问全了」才敢断言缺节点；没问到就传 None
                    # （构建函数会放行，让 ComfyUI 自己报错）。
                    params["_h3image_nodes_available"] = (
                        None if not probe_ok else nodes_ok)

                # 【2026-11-16 新增】音频接续时序 —— 为接续段切出
                # 「1s 空白 ⊕ 原始音轨切片」的**采样用**段音频。
                #
                # 【为什么在这里做】
                # 必须在拼工作流**之前**（工作流要引用这个文件名），
                # 且必须在素材上传**之后**（原始音轨的 ComfyUI 文件名
                # 这时才确定）。
                #
                # 【成片音轨不在这里】成片音轨用「原始参考音频整条」，
                # 在**合并阶段**贴（_finalize_continuation ->
                # _concat_chain_video 的 external_audio 参数）。
                # 两条链路必须分开 —— 否则每段 1s 空白会累积。
                if mode in ("flf", "r2v"):
                    await self._prepare_audio_continuation(session, job_id,
                                                           params)

                # 【2026-11-19】参考视频帧数（供接续段做时间轴切片）。
                # 必须在拼工作流**之前**（工作流要读它决定切片/跳过）。
                if mode in ("flf", "r2v"):
                    await self._record_ref_video_frame_count(job_id, params)

                # 【2026-11-16 第5轮代码审查 C-10 修 —— 老任务重放的尺寸兜底】
                #
                # 【为什么需要】
                # 面板提交与 Agent 建任务都会先过归一层（补上 width/height），
                # 但**历史任务**的落库 params 里可能压根没有 width/height
                # （那是老版本前端只提交 resolution + aspect 的产物）。
                # worker 直接拿这份 params 调 build_workflow ->
                # video_workflow.build_t2v_panel 算出 0×0 ->
                # 提交 0×0 的 latent 给 ComfyUI。
                #
                # 【怎么兜】
                # 用**与面板完全同一套**公式（h3_video_size.resolve_size）
                # 把「档位 + 比例」算成真实宽高写回 params ——
                # 这样重放出来的产物尺寸与当年面板上选的**一致**，
                # 而不是编一个 768p/16:9 的默认值。
                #
                # 【为什么不是直接抛错】
                # 抛错 = 那些老任务**永久无法复用**（用户点「复用参数」
                # 或重试旧任务一律失败）。而这里有真实依据（resolution +
                # aspect 都在），算出来就是用户当年选的那个尺寸。
                # 只有**连档位和比例都没有**时才让 build_t2v_panel 抛错
                # （那时真的没有任何依据推断尺寸）。
                if mode in ("t2v", "flf", "r2v"):
                    try:
                        gen_normalize.normalize_video_size_fallback(
                            params, mode, [])
                    except ValueError:
                        # 没有任何尺寸依据 -> 交给 build_t2v_panel 抛明确错误
                        pass

                workflow = build_workflow(mode, params)

                # 【2026-11-15 新增】把**产物实际生成参数**回写进 params。
                #
                # 【为什么必须在这里回写】构建器已经把所有默认值都算完了
                # （步数按「实际挂了哪枚加速 LoRA」回落、调度器/采样器同理），
                # 所以**构建好的 workflow 才是产物实际元数据的权威来源**。
                # 落库的 params 只是「用户填了什么」，两者会脱钩 ——
                # 这正是媒体库卡片 LoRA 误显示 + 步数为空的根因。
                #
                # 【回写什么】steps / scheduler / sampler / cfg / seed /
                # 实际挂载的 LoRA 清单。缺失就不写（**不编造**）。
                try:
                    extracted = artifact_meta.extract_from_workflow(workflow)
                    artifact_meta.apply_to_params(params, extracted)
                    with db() as conn:
                        conn.execute(
                            "UPDATE jobs SET params=? WHERE id=?",
                            (json.dumps(params, ensure_ascii=False), job_id))
                except Exception as exc:  # noqa: BLE001
                    # 回写失败**不能**让生成失败 —— 产物照跑，
                    # 只是显示层拿不到精确值（退回到「—」）。
                    print(f"[worker] 任务 {job_id} 产物元数据回写失败: {exc}")

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
                #
                # 【2026-11-14 A5 需求】这里原本是**裸的 `while True:`** ——
                # 只等 comfy_history 出结果，既没有超时上限，也不看 ComfyUI 的队列。
                # 于是有两种「永远运行中」：
                #   1. ComfyUI 侧任务已被取消 / 被清队列 / ComfyUI 重启过
                #      -> history 里没有、队列里也没有 -> 这里永远等下去；
                #   2. ComfyUI 侧卡死 / 显存不足挂住 -> 同样永远等下去。
                # 而且它还**占着 Studio 的串行队列**（worker 一次只跑一个），
                # 后面所有任务全部排队等着，用户看到的是「整个队列不动了」。
                #
                # 现在加两道闸：
                #   * 超时上限（按媒体类型；视频类默认 0 = 不限时）-> 明确失败；
                #   * 队列核查（/queue）-> prompt_id 既不在 running 也不在 pending、
                #     且 history 里也没有 -> 判定「任务已丢失」-> 明确失败。
                try:
                    # 本次运行的计时基准（DB 里的 started_at；取不到就用当前时间）。
                    with db() as conn:
                        started_row = conn.execute(
                            "SELECT started_at FROM jobs WHERE id=?", (job_id,)).fetchone()
                    run_started_at = (started_row["started_at"]
                                      if started_row and started_row["started_at"]
                                      else time.time())
                    # 【2026-11-19 缺陷 1 修】本次运行的**上限**按媒体类型取。
                    # 视频类默认 0 = 不按时长判失败（跑一天也不判失败）；
                    # 图像 / 音频保留可配置上限（跑满基本等于卡死）。
                    job_media = job_timeout.media_of_mode(mode)
                    run_timeout_sec = timeout_sec_for_media(job_media)
                    # 队列核查的节流：不必每轮轮询都打一次 /queue
                    # （history 每 1.5 秒查一次已经够密了，/queue 稍后再查）。
                    next_queue_check = 0.0
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
                            # 【2026-11-13 修】`status` 的类型**不保证**是 dict
                            # （可能是 None / 字符串）。直接 `.get()` 会在
                            # str 上抛 AttributeError —— 而这恰恰发生在
                            # 「把失败原因翻译给用户」的错误处理路径上，
                            # 结果用户只看到一句 Python 异常文本。
                            # 详见 comfy_errors.coerce_status_info。
                            #
                            # 【2026-11-19 缺陷 2 修 —— 判定顺序】
                            #
                            # 【原来错在哪】旧顺序是：
                            #     if hist.get("outputs"):   <- 先看 outputs 非空
                            #         ... 标 status='done'
                            #     if is_error:              <- 失败判定在后面
                            # 而 ComfyUI 在执行失败（OOM / 节点报错）时
                            # **也会**往 outputs 里塞已执行完节点的输出 ——
                            # 实测 OOM 的 history 是：
                            #     status.status_str = 'error'
                            #     outputs = {'301': {'text': [...]}}   <- 文本节点
                            # `hist.get("outputs")` 非空 -> 走成功分支 ->
                            # `_extract_outputs` 取不出媒体 -> saved=[] ->
                            # 任务被标成 done + outputs=[] ->
                            # 前端显示「生成成功」+ 空白（黑）预览。
                            #
                            # 【修法】**失败判定必须优先于产物判定**。
                            # status 说失败就是失败，outputs 里有什么都不算数。
                            outcome = gen_outcome.history_outcome(hist)
                            if outcome["failed"]:
                                # status 说失败（OOM / 节点报错 / 没产物）
                                # -> **必须判失败**。
                                reason = outcome["reason"]
                                if not reason:
                                    reason = _comfy_error_message(
                                        outcome["status_info"])
                                raise RuntimeError(reason)
                            if outcome["pending"]:
                                # ComfyUI 的 status 还没落定（status=None）
                                # -> **还在跑**，继续等下一轮。
                                # ⚠ 这一条必须保留：把「还在跑」误判成失败
                                # 会把正常的长视频任务直接判死（缺陷 1 的同类坑）。
                                continue
                            # 到这里：status 成功，且**确实有媒体产物**。
                            outs = _extract_outputs(hist)
                            saved = []
                            for item in outs:
                                fname = await comfy_download_output(session, item)
                                saved.append({"kind": item["kind"], "file": fname, "name": item["filename"]})
                            # 【2026-11-19 缺陷 2 修 —— 产物必须真实存在】
                            # ComfyUI 报「有输出」不等于文件真的下下来了。
                            # 下载中断 -> 0 字节文件 -> 前端显示全黑 / 播不出来。
                            # 所以这里校验**文件存在且非空**，不通过就判失败。
                            real_ok, problems = gen_outcome.outputs_are_real(
                                saved, OUT_DIR)
                            if not real_ok:
                                print(f"[worker] 任务 {job_id} 产物校验失败: {problems}")
                                raise RuntimeError(
                                    gen_outcome.missing_output_reason(problems))
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
                            # 【2026-11-19 缺陷 2 修】上面 `history_outcome`
                            # 已经把「畸形 / 失败 / 无产物」三种情况全部判掉了，
                            # 所以这里**不再有**「既没产物也不是错误」的分支 ——
                            # 那种情况（ComfyUI 还在跑、status 还没落定）
                            # 由 history_outcome 的 NO_OUTPUT 判定覆盖：
                            # 它会给出明确的失败原因，而不是静默继续等。

                        # ---- history 里没有结果 ----
                        #
                        # 【A5 需求】这里要判定「这个任务是不是**真的**还在跑」。
                        # history 为空有两种可能：
                        #   a) 任务还在 ComfyUI 里跑（正常）；
                        #   b) 任务在 ComfyUI 里**已经不存在**了
                        #      （被取消 / 被清队列 / ComfyUI 重启过）
                        #      -> 旧代码在这里会**永远等下去**。
                        # 区分 a/b 只能查 ComfyUI 的**队列**：
                        # prompt_id 既不在 queue_running 也不在 queue_pending
                        # -> 判定为「已丢失」。
                        #
                        # 【为什么不能每轮都查 /queue】那是多一次 HTTP 往返。
                        # 这里节流成 QUEUE_CHECK_INTERVAL 秒一次 —— 「丢失」
                        # 最多晚几十秒被判定出来，对用户没有区别。
                        now = time.time()
                        if now >= next_queue_check:
                            next_queue_check = now + QUEUE_CHECK_INTERVAL
                            state, reason = await resolve_running_state(
                                session, prompt_id, run_started_at, False,
                                timeout_sec=run_timeout_sec)
                            if state == running_truth.STATE_LOST:
                                print(f"[worker] 任务 {job_id} 在 ComfyUI 队列中已消失，"
                                      f"判定为丢失: {reason}")
                                raise RuntimeError(reason)
                            if state == running_truth.STATE_TIMEOUT:
                                print(f"[worker] 任务 {job_id} 超过运行上限"
                                      f"（{run_timeout_sec:.0f}s），判定为超时")
                                raise RuntimeError(reason)
                        elif (run_timeout_sec > 0
                              and now - run_started_at > run_timeout_sec):
                            # 超时判定**不依赖**队列 —— 队列读不到（ComfyUI 无响应）
                            # 时也必须能超时退出，否则又会变成「永远运行中」。
                            #
                            # 【2026-11-19 缺陷 1 修】`run_timeout_sec` 按**媒体类型**
                            # 取值：视频类默认 0（不按时长判失败），图像 / 音频保留上限。
                            print(f"[worker] 任务 {job_id} 超过运行上限"
                                  f"（{run_timeout_sec:.0f}s），判定为超时")
                            raise RuntimeError(
                                timeout_reason_for(run_timeout_sec, job_media))
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
            # 【2026-11-13 修】**不要**把原始 Python 异常文本写进
            # jobs.error —— 前端会原样显示给用户。
            # 走 _user_facing_error：已中文化的 ComfyUI 原因保留，
            # 其余归类成可读中文（技术细节只进上面的日志）。
            with db() as conn:
                conn.execute("UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=? AND status='running'",
                             (_user_facing_error(e), time.time(), job_id))
        finally:
            if self.current == job_id:
                self.current = None


    async def _record_chain_audio_source(self, job_id: str,
                                         params: dict) -> None:
        """把**原始音轨**记进本段的接续上下文（链首段 / 每一段都记）。

        记的字段（写进 params["continuation"] 并回写 DB）：
          audio_track_mode       音轨来源（generated / reference / video_bg）
          audio_source_kind      原始音轨来源（ref_audio / ref_video）
          audio_source_file      **原始**音轨的 ComfyUI 文件名
          audio_source_duration  原始音轨总时长（秒）

        【为什么每段都记而不是只记链首段】
        接续段会继承上游的值（见 _inherit_audio_continuation），
        但**每一段自己**的记录才是它自己的真相 —— 若某一段的记录
        丢了（比如老任务没有这个字段），继承链会在那一段断掉。
        记录「原始整条」而不是「本段切片」——
        切片是每一段各自算出来的（依赖累计帧数），原始整条才是
        不变的那个源。

        【为什么不改音轨来源】
        音轨来源**以链首段为准**（nonebot 同款口径）。所以本段
        如果自己选了别的来源，记录里保留**继承来的**那个，
        并在服务端日志里说明 —— 不静默改用户的显式选择，
        也不让中途改源破坏音画对齐。
        """
        info = cont_service.chain_info(params)
        if not info:
            return
        track_mode = str(info.get("audio_track_mode") or "").strip()
        source_kind = str(info.get("audio_source_kind") or "").strip()
        source_file = str(info.get("audio_source_file") or "").strip()

        if not track_mode:
            # 链首段（没有继承）-> 用本段自己的选择。
            track_mode = h3audio.normalize_track_source(
                str(params.get("audio_track_source") or "").strip(),
                has_reference_audio=bool(params.get("ref_audios")),
                has_reference_video_audio=bool(
                    params.get("ref_video_audios")))
            if track_mode == h3audio.TRACK_REFERENCE:
                source_kind = "ref_audio"
                refs = list(params.get("ref_audios") or [])
                source_file = str(refs[0]) if refs else ""
            elif track_mode == h3audio.TRACK_VIDEO_BG:
                source_kind = "ref_video"
                refs = list(params.get("ref_video_audios") or [])
                source_file = str(refs[0]) if refs else ""
        elif not source_file:
            # 续续段继承了音轨来源但没继承文件 -> 从上游补。
            source_file = str(info.get("audio_source_file") or "").strip()

        if not h3acont.is_external_track(track_mode):
            return

        duration = info.get("audio_source_duration")
        if duration is None and source_file:
            local = _comfy_input_local(source_file)
            if local is not None:
                duration = await asyncio.to_thread(_probe_audio_duration, local)
        try:
            duration = (None if duration is None else float(duration))
        except (TypeError, ValueError):
            duration = None

        state = h3acont.chain_audio_state(
            track_source=track_mode,
            source_kind=source_kind,
            source_file=source_file,
            source_duration=duration,
            consumed_frames=int(info.get("audio_consumed_frames") or 0))
        info.update(state)
        params["continuation"] = info
        # 回写 DB —— 下一段的接续上下文就是从 DB 读出来的。
        with db() as conn:
            conn.execute("UPDATE jobs SET params=? WHERE id=?",
                         (json.dumps(params, ensure_ascii=False), job_id))
        if source_file:
            print(f"[音频接续] {job_id}: 原始音轨 {source_file}"
                  f"（{source_kind or track_mode}，{duration}s）")

    async def _record_ref_video_frame_count(self, job_id: str,
                                             params: dict) -> None:
        """把**参考视频的帧数**记进接续上下文（供接续段做时间轴切片）。

        【为什么需要】—— 2026-11-19 实测到的真实缺陷
        接续段（段2）的参考视频**画面**必须接段1 的时间位置：
        规范（docs/接续模式深度研究-20261116.md §5.4.3）要求
            skip_first_frames = C_n（= 上游已累计交付帧数）
        但 `_continuation_media_slice` 把参考视频的切片**绑在**
        「音轨来源是外部音轨」这个条件上 —— 音轨来源是「生成的音乐」
        时 external=False ⇒ skip_frames=0 ⇒ **段2 的参考视频从 0 开始喂**。

        ⇒ 段1 已经用过同一份参考视频（画面已经演化过去），
          段2 却要求画面回到参考视频的开头 —— 与拷贝桥钉住区里
          「段1 已演化的画面」**直接冲突** ⇒ 段2 新生成区爆发式跳变。

        【实测证据】（2026-11-19，8190 真实生成）
          同一段1 派生出两个接续段，唯一差别是参考视频在不在：
            带参考视频：@21 帧 L1=100.78，Δ亮度=+9.01（25.8× 该段中位）
            无参考视频：@21 帧 L1= 95.61，Δ亮度=+1.95（ 3.5× 该段中位）
          ⇒ 带参考视频时是**亮度阶跃**（用户看到「闪一下」），
            不带时是亮度连续的**镜头切换**（正常内容）。

        【本函数做什么】
        链首段探测参考视频的真实帧数，写进 params["continuation"]
        （之后每一段从上游继承）。工作流层据此判断：
          * 参考视频**够长**（C_n < 帧数）⇒ 按规范跳 C_n 帧（正确切片）
          * 参考视频**不够长**（C_n >= 帧数）⇒ 内容已用尽，
            本段**不带**参考视频（否则要么喂空参考报错、
            要么继续从 0 开始喂 —— 后者就是本缺陷）
        """
        info = cont_service.chain_info(params)
        if not info:
            return
        # 已有记录（接续段从上游继承）-> 不用再探测
        if int(info.get("ref_video_frame_count") or 0) > 0:
            return
        # 【第 4 轮收口 —— 真实缺陷：失败结果不缓存】
        # 探测不到帧数时**原来直接 return**，既不写任何标记，
        # 于是**每次调用都会重新探测**（ffprobe 一次约 3 秒 ——
        # 实测 32MB / 2 分钟 480P 的参考视频，-count_frames 要真解码一遍）。
        #
        # 【为什么这不只是「轻微性能问题」】
        # 1) worker 的 `_run` 每次跑这个任务都会调一次；任务被重跑 /
        #    重试时又调一次 ⇒ 每次都是 3 秒的**同步阻塞**子进程。
        # 2) 更关键：**探测失败时下一段拿不到 ref_video_frame_count**
        #    ⇒ 工作流层走「探测不到帧数 -> 不猜」的分支
        #    ⇒ `video_skip_frames = 0` ⇒ **段2 的参考视频从 0 开始喂**
        #    —— 这正是第 3 轮那个「亮度阶跃」缺陷的触发条件之一。
        #    所以「不缓存」= 每段都可能重新踩一次同样的坑。
        #
        # 【修法：失败也要落一个标记】
        # 写 `ref_video_frame_count_probe = "failed"`，
        # 让后续调用**直接返回**、不再重探。
        # 语义仍然保守：帧数仍然是 0（= 不知道），**不猜**。
        if info.get("ref_video_frame_count_probe") == "failed":
            return
        videos = list(params.get("ref_videos") or [])
        if not videos:
            return
        frames = 0
        for name in videos[:1]:
            local = _comfy_input_local(name)
            if local is None:
                continue
            # 【为什么走线程】ffprobe 是**同步阻塞**子进程调用
            # （实测 3 秒），跑在事件循环里会冻结全站 ——
            # 与 2026-11-16「8190 不可访问」是同一类事故
            # （那次是 LoRA 扫描，这次是参考视频探测）。
            # 同文件的音频探测 `_probe_audio_duration` 已经是这个写法。
            frames = await asyncio.to_thread(_probe_video_frame_count, local)
            break
        info["ref_video_frame_count"] = int(frames)
        if frames <= 0:
            # 探测失败 -> **落标记**（这一步就是本轮修复的核心）
            info["ref_video_frame_count_probe"] = "failed"
            params["continuation"] = info
            with db() as conn:
                conn.execute("UPDATE jobs SET params=? WHERE id=?",
                             (json.dumps(params, ensure_ascii=False), job_id))
            print(f"[接续] {job_id}: 参考视频帧数探测失败（已缓存失败标记，"
                  f"不再重复探测）")
            return
        params["continuation"] = info
        with db() as conn:
            conn.execute("UPDATE jobs SET params=? WHERE id=?",
                         (json.dumps(params, ensure_ascii=False), job_id))
        print(f"[接续] {job_id}: 参考视频 {frames} 帧"
              f"（供接续段按 C_n 做时间轴切片）")

    async def _prepare_audio_continuation(self, session: ClientSession,
                                          job_id: str, params: dict) -> None:
        """接续段的**音频接续时序**（首尾帧 / 参考生视频）。

        做两件事：
          1. 算出本段的音频时间轴（t_a_start = C_n / 24）；
          2. 切出「1s 空白 ⊕ 原始音轨切片」并上传到 ComfyUI input，
             把文件名写进 params["segment_audio_file"]。

        【什么时候不做事】
          * 链首段（没有上游）-> 不切（第 1 段用原始音轨从 0 开始）。
          * 音轨来源是「生成的音乐」-> 不切（音画同源）。
          * 没有上游记录的原始音轨文件 -> **明确报错**（不能静默
            退化成「用整条原始音频当本段音轨」—— 那会让 1s 空白
            不存在、且音画错位）。

        【越界提示】原始音轨不够长时，给出中文告警（写进
        params["continuation"]["audio_warnings"]），并在服务端日志里
        打印 —— 用户在界面上能通过接续上下文看到。
        """
        info = cont_service.chain_info(params)
        if not info:
            return
        track_mode = str(info.get("audio_track_mode") or "").strip()
        if not h3acont.is_external_track(track_mode):
            return
        # 链首段：没有上游 -> 本段就是第 1 段，音轨从 0 开始，不切。
        if not str(info.get("source_job") or "").strip():
            return

        source_file = str(info.get("audio_source_file") or "").strip()
        if not source_file:
            raise RuntimeError(
                "接续段缺少原始参考音轨（上游没有记录音轨文件）—— "
                "无法按接续时序切分音轨。请在上一段重新选择参考音乐，"
                "或改用「复用参数」重新生成一段。")

        plan = {
            "source_start": float(info.get("audio_segment_start") or 0.0),
            "source_duration": float(info.get("audio_segment_duration") or 0.0),
            "lead_blank": float(info.get("audio_lead_blank")
                                or h3acont.LEAD_BLANK_SECONDS),
        }
        for note in info.get("audio_warnings") or []:
            print(f"[音频接续] {job_id}: {note}")

        name = await _prepare_segment_audio(session, job_id, params, plan)
        params["segment_audio_file"] = name
        print(f"[音频接续] {job_id}: 段音频 {name} "
              f"(切片 {plan['source_start']:.3f}s + {plan['source_duration']:.3f}s"
              f"，前导空白 {plan['lead_blank']:.1f}s)")

    async def _finalize_continuation(self, job_id: str, mode: str, params: dict,
                                     saved: list[dict]) -> list[dict]:
        """接续收尾：登记本段 + 拼接**完整视频**。

        【委托给模块级实现】本方法曾是 Worker 的实例方法，但它体内
        **没有任何 `self.` 引用**。而「服务重启自愈」路径
        （`_finalize_residual_with_outputs`）在**模块级函数**里，
        没有 Worker 实例可用 —— 所以真正的实现提到模块级
        `_finalize_continuation_impl`，这里只做一层薄委托。
        （2026-11-17 修「5 秒接续 5 秒只得到 4 秒」时引入。）
        """
        return await _finalize_continuation_impl(job_id, mode, params, saved)

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
                # 【2026-11-13 修】同 worker 兜底：不写原始异常文本。
                with db() as conn:
                    conn.execute(
                        "UPDATE job_batches SET status='failed', error=?, finished_at=? WHERE id=?",
                        (_user_facing_error(e), time.time(), batch_id))
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
            if mode in ("i2i_qwen", "i2i_h3image"):
                # 参考图来自素材库（asset_id + 版本快照），顺序即 <imageN> 语义
                names = await _upload_asset_refs(session, job_id, batch_params)
                batch_params["ref_images"] = names
            if mode in ("t2i_h3image", "i2i_h3image"):
                nodes_ok, probe_ok = await probe_h3_image_nodes(session)
                batch_params["_h3image_nodes_available"] = (
                    None if not probe_ok else nodes_ok)
            workflow = build_workflow(mode, batch_params)

            # 【2026-11-15 新增】批次路径也要回写产物实际参数。
            #
            # 【为什么要单独处理】批次任务（一次出 N 张）在 jobs 表里只有
            # **一条**记录、**一个** params，而每张产物各有自己的 seed。
            # 所以这里：
            #   * 回写采样参数（步数 / 调度器 / 采样器 / LoRA）—— 批次间一致；
            #   * **不写 seed**（include_seed=False）—— 否则第一批的 seed
            #     会盖掉 job 记录里的种子信息（每张的 seed 记在
            #     job_batches 表里，那才是对的落点）。
            try:
                batch_extracted = artifact_meta.extract_from_workflow(workflow)
                artifact_meta.apply_to_params(params, batch_extracted,
                                               include_seed=False)
                with db() as conn:
                    conn.execute(
                        "UPDATE jobs SET params=? WHERE id=?",
                        (json.dumps(params, ensure_ascii=False), job_id))
            except Exception as exc:  # noqa: BLE001
                print(f"[worker] 批次 {batch_id} 产物元数据回写失败: {exc}")

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
            # 【A5】批次任务的轮询循环同样需要超时上限与「队列核查」——
            # 否则一个批次卡住 = 整个任务永远「运行中」（与单张任务同病）。
            #
            # 【2026-11-19 缺陷 1 修】上限按**媒体类型**取（批次目前只用于
            # 图像模式，所以实际走图像上限；写成通用形式是为了将来新增
            # 视频批次时不会又落回「所有任务共用一个阈值」）。
            batch_media = job_timeout.media_of_mode(mode)
            batch_timeout_sec = timeout_sec_for_media(batch_media)
            batch_started_at = time.time()
            next_queue_check = 0.0
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
                        # 【2026-11-19 缺陷 2 修】与单任务路径**同一套**判定：
                        # 失败优先于产物（OOM 时 ComfyUI 的 outputs 里也会有
                        # 已执行完节点的输出，只看「outputs 非空」会误判成功）。
                        outcome = gen_outcome.history_outcome(hist)
                        if outcome["failed"]:
                            reason = outcome["reason"]
                            if not reason:
                                reason = _comfy_error_message(
                                    outcome["status_info"])
                            raise RuntimeError(reason)
                        if outcome["pending"]:
                            # status 还没落定 -> 还在跑，继续等。
                            continue
                        outs = _extract_outputs(hist)
                        saved = []
                        for item in outs:
                            fname = await comfy_download_output(session, item)
                            saved.append({"kind": item["kind"], "file": fname,
                                          "name": item["filename"]})
                        # 【2026-11-19 缺陷 2 修】产物必须真实存在且非空。
                        real_ok, problems = gen_outcome.outputs_are_real(
                            saved, OUT_DIR)
                        if not real_ok:
                            print(f"[worker] 批次 {batch_id} 产物校验失败: {problems}")
                            raise RuntimeError(
                                gen_outcome.missing_output_reason(problems))
                        return saved

                    # ---- history 里没有结果 -> 判定「是否真的还在跑」----
                    # 同 Worker._run 的 A5 逻辑：队列里也没有 = 任务已丢失；
                    # 超过运行上限 = 超时。两者都要**明确失败**，不许无限等待。
                    now = time.time()
                    if now >= next_queue_check:
                        next_queue_check = now + QUEUE_CHECK_INTERVAL
                        state, reason = await resolve_running_state(
                            session, prompt_id, batch_started_at, False,
                            timeout_sec=batch_timeout_sec, media=batch_media)
                        if state == running_truth.STATE_LOST:
                            print(f"[worker] 批次 {batch_id} 在 ComfyUI 队列中已消失，"
                                  f"判定为丢失: {reason}")
                            raise RuntimeError(reason)
                        if state == running_truth.STATE_TIMEOUT:
                            print(f"[worker] 批次 {batch_id} 超过运行上限"
                                  f"（{batch_timeout_sec:.0f}s），判定为超时")
                            raise RuntimeError(reason)
                    elif (batch_timeout_sec > 0
                          and now - batch_started_at > batch_timeout_sec):
                        print(f"[worker] 批次 {batch_id} 超过运行上限"
                              f"（{batch_timeout_sec:.0f}s），判定为超时")
                        raise RuntimeError(
                            timeout_reason_for(batch_timeout_sec, batch_media))
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


def _video_note(params: dict, field: str, raw, effective, why: str = "") -> None:
    """记一条视频族归一说明（「改了什么、为什么」）。

    【为什么需要 —— 2026-11-16 修 D-8】
    视频族的 ``normalize_t2v_params`` 里有三处**静默**改写：
      * steps 越界 -> 夹到 [1, 50]
      * sampler / scheduler 不在本机 COMBO 清单里 -> 回落到默认档
      * twopass.transition_step 被步数档的 sigma 表长度夹回
      * twopass.rho=0 且不用放大器 -> 提到 0.6（否则节点直接 reject）
    这些改写此前**不留任何痕** -> 参数卡显示的回落值与用户填的值
    对不上，用户无从判断原因。

    图像族已经有同一套机制（``gen_normalize`` 的 ``params["normalize_notes"]``），
    这里沿用**同样的字段名与同样的展示入口**（任务详情的「参数已调整」行），
    两族口径一致。

    【只在真的改了值时才记】
    合法值一个字节都不动 —— 契约与图像族 R1 相同。
    """
    if not isinstance(params, dict):
        return
    if raw is None:
        return
    # 值没变 -> 不是一次「改动」，不记
    try:
        if isinstance(raw, (int, float)) and isinstance(effective, (int, float)):
            if float(raw) == float(effective):
                return
        elif str(raw).strip() == str(effective).strip():
            return
    except (TypeError, ValueError):
        pass
    note = f"{field}：{raw!r} 不是合法取值，已改用 {effective!r}"
    if why:
        note += why
    notes = params.get("normalize_notes")
    if not isinstance(notes, list):
        notes = []
    if note not in notes:
        notes.append(note)
    params["normalize_notes"] = notes


def normalize_t2v_params(params: dict, mode: str = "t2v") -> tuple[dict, str]:
    """视频面板参数的**服务端归一**（唯一入口）。

    返回 (归一后的 params, 错误信息)。错误信息为空表示通过。

    归一的每一项都对应一条真实的本机约束：
      * 宽高 -> 32 的倍数（H3 节点 schema step=32 / CANVAS_MULTIPLE=32）
      * 模型 -> 必须是 video_models.VIDEO_MODELS 里登记的 key，且文件在本机
      * 加速 LoRA -> 逐个查文件是否存在 + 与底模的层结构是否对得上
      * 注意力 -> 必须是本机 ComfyUI 里真有的节点
      * 二采 -> SelfLift 的硬约束（Euler / transition_step 范围 / rho 与 upscaler 组合）

    【2026-11-15 修复③ 交付项 3】现在**三个视频模式共用这一套归一**：
    t2v（文生视频）/ flf（首尾帧）/ r2v（参考生视频）走**同一份**面板，
    参数语义也就必须走**同一个入口** —— 否则面板上能调的参数
    在服务端会被静默丢掉（界面说一套、工作流跑另一套）。

    mode 决定两处**真实差异**：
      * 面积上限（t2v / flf / r2v 均为 2.0MP，见
        video_models.mode_size_policy —— 2026-11-17 起 flf/r2v 对齐
        nonebot seg1/seg2 的 ResolutionSelector(megapixels=2)）；
      * 底模通道（r2v 必须用 Ref2VA 权重，见 video_models.model_for_mode）。
    """
    out = dict(params)
    vid_mode = mode if mode in ("t2v", "flf", "r2v") else "t2v"

    # ---- 种子（**最先**校验）----
    #
    # 【2026-11-16 新增】非法种子必须**最先**被拦下。
    #
    # 【为什么放最前面】归一层后面还有一长串校验（宽高 / 模型 /
    # 加速 LoRA / 注意力 / 二采）。如果种子校验排在它们后面，
    # 用户填了非法种子 + 某个模型在���机不兼容 -> 报的是
    # 「Turbo 4 步与当前底模不兼容」，用户完全不知道自己**还**填错了种子。
    # 一次只说一件事：种子的错就是种子的错。
    #
    # 【原来错在哪】这段只做 int() 转换，失败就 out["seed"] = None ——
    # 用户填了 "abc"，归一后变成「随机」，用户以为固定了随机性，
    # 实际跑了个随机结果，且**没有任何提示**。
    # 更糟的是：normalize_t2v_params 在 prepare_job_params **之前**跑
    # （api_create 的顺序），所以归一后 prepare_job_params 看到的
    # 已经是 None —— 种子校验根本没机会生效（线上实测：POST /api/jobs
    # 传 seed="abc" 返回 200 并建了任务）。
    #
    # 归一后的 out["seed"] 恒为合法正整数或 None（随机）。
    if params.get("seed") not in (None, ""):
        _seed, _seed_err = seed_guard.parse_seed(params.get("seed"))
        if _seed_err:
            return out, _seed_err
        out["seed"] = _seed
    else:
        out["seed"] = None

    # ---- 宽高 ----
    # 【2026-11-11】strict_over_limit=True —— 超限时**不静默截断**，
    # 直接返回可执行的错误提示（说明上限与当前请求值）。
    width, height, reasons = video_models.normalize_video_size(
        params.get("width"), params.get("height"),
        max_pixels=video_models.max_pixels_for_mode(vid_mode),
        strict_over_limit=True,
        # 【2026-11-15 代码审查修】把模式传给尺寸归一 ——
        # 错误文案要说「首尾帧生视频分辨率上限为 …」，
        # 而不是写死的「文生视频…」（各模式上限不同，按 mode 取）。
        mode=vid_mode)
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
    # 【2026-11-15 修复③ 交付项 3】按模式校验**底模通道**。
    #
    # r2v（参考生视频）必须用 Ref2VA 权重 —— FL2VA 权重没有 Ref2VA
    # 通道，用它做参考生成等于「参考没生效」。
    # （原来这里不校验，model_for_mode() 会在**构建工作流时**静默换权重 ——
    #   用户在面板上选了 A，工作流里跑的是 B，面板还在显示 A。）
    # 现在在**提交入口**就拦下并说清楚。
    if vid_mode == "r2v" and not model.get("supports_ref2va"):
        return out, (f"模型「{model['label']}」不支持参考生成（Ref2VA）通道 —— "
                     "参考生视频必须用 Ref2VA 权重"
                     "（Ref2VA · 剪枝 int8 / Ref2VA · 完整 int8 / "
                     "混合版 · FL2VA+Ref2VA）。")
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

    # ---- 加速 LoRA（**一个槽位**，2026-11-14 重构）----
    #
    # 【旧行为的问题】
    # 旧实现分两段：「固定加速 LoRA」（恒为 ckpt850，永远生效）+ 「可勾选的
    # 组合枚」（前端下拉里选的，额外叠加）。于是前端选 LightX2V，工作流里
    # 仍然是 ckpt850 + LightX2V —— 「选了不生效」就是这么来的。
    #
    # 【新行为】
    # 只有一个槽位：前端选哪枚，工作流里就**只挂哪枚**。
    # 换算由 video_models.resolve_accel_lora_slot 统一做
    # （兼容 accel_lora / accel_loras / fixed_accel_lora / lora 四套历史字段）。
    # 显式选「不挂」= 空槽位 -> 工作流走标准多步采样。
    accel_key, accel_strength = video_models.resolve_accel_lora_slot(params)
    if accel_key:
        meta = video_models.accel_lora_meta(accel_key)
        if meta is None:
            return out, f"未知的加速 LoRA {accel_key}"
        if not video_models.lora_available(meta["name"]):
            return out, f"本机找不到加速 LoRA {meta['name']}"
        # 兼容性必须**实测**（逐层比对权重头）：
        # 键结构对不上的 LoRA 会被 ComfyUI 静默丢弃 —— 出片照跑但没生效。
        #
        # 【2026-11-15】把**实际会用的加载节点**传进去 ——
        # AdaLN 的运行期重注入只有 Turbo 节点做得到，核心节点做不到。
        # 不传 loader 的话，「核心节点 + 带 AdaLN 的 LoRA + 剪枝底模」
        # 会被误判成兼容，而实际那 51 个 AdaLN 层会被静默丢弃。
        loader = video_models.detect_accel_lora_loader(meta["name"])
        check = video_models.check_h3_lora_compat(meta["name"], model["unet"],
                                                  loader=loader)
        if not check["compatible"]:
            return out, (f"「{meta['display']}」与当前底模不兼容："
                         f"{check['reason']}")
        if accel_strength <= 0:
            return out, (f"加速 LoRA「{meta['display']}」的强度必须大于 0"
                         f"（当前 {accel_strength}）")
        # 加载节点路由（按权重键名形态实测决定）—— 记进 params，
        # 让任务卡片能显示「这枚是用哪个节点加载的」，便于排查静默失效。
        out["accel_lora"] = accel_key
        out["accel_lora_strength"] = accel_strength
        out["accel_lora_loader"] = loader
        # 兼容老读侧（job_meta / 前端）仍看 accel_loras 数组
        out["accel_loras"] = [{"key": accel_key, "strength": accel_strength,
                               "enabled": True}]
        # 固定枚字段同步（老读侧把它当「工作流固定的那枚」）
        out["fixed_accel_lora"] = accel_key
    else:
        # 显式「不挂加速 LoRA」
        out["accel_lora"] = ""
        out["accel_loras"] = []
        out["fixed_accel_lora"] = ""

    # ---- 效果 LoRA（可多选，与加速 LoRA 并列共存）----
    #
    # 【为什么这里要再卡一道】
    # 前端下拉只列「本机真实存在且实测兼容」的条目，但**绕过前端直接调
    # API** 就能塞任意字符串进来。所以服务端必须自己验：
    #   1. 文件必须真实存在（resolve_lora）
    #   2. 必须与当前底模**实测兼容**（check_effect_lora_compat）
    #   3. 强度必须在合理区间
    # 不兼容的坚决拒绝 —— 因为 ComfyUI 会**静默丢弃**键结构对不上的 LoRA
    # （出片照跑但 LoRA 没生效），用户根本看不出来。
    effect_loras: list[dict] = []
    raw_effect = params.get("effect_loras")
    if raw_effect is not None and not isinstance(raw_effect, list):
        # 【2026-11-14 修 —— 顶层类型非法也要报错】
        # 原来只有 `if isinstance(raw_effect, list):` 一层判断：
        # 传 effect_loras="h3/xxx"（字符串）会**整段跳过** -> 200 + 落库 []，
        # 同样收不到原因。与「item 不是 dict」同性质，一并明确报错。
        return out, (
            f"效果 LoRA 列表必须是数组（每项是对象），"
            f"收到的是 {type(raw_effect).__name__}：{raw_effect!r}")
    if isinstance(raw_effect, list):
        from h3studio import h3_effect_loras as effect_registry
        unet_path = video_models.find_weight(model["unet"])
        for index, item in enumerate(raw_effect):
            # 【2026-11-14 修 —— 非法结构不能静默丢弃】
            #
            # 【原症状】`if not isinstance(item, dict): continue`
            # —— 传 effect_loras=["h3/3dsrx_minimax_h3.safetensors"]（字符串数组）
            # 会被**静默跳过**，HTTP 200 + 落库 effect_loras=[]，
            # 客户端**收不到任何错误原因**。
            # 用户看到的是「我明明选了 LoRA，生成库卡片上却什么都没有」，
            # 完全无从判断是前端没提交、还是后端丢了。
            #
            # 【为什么要区分 item 和 name】
            # 「item 不是 dict」= **结构非法**（传错了类型）-> 必须报错。
            # 「item 是 dict 但 name 为空」= 用户主动清空了这一项
            # （或停用），属于**合法输入** -> 跳过，不报错。
            if not isinstance(item, dict):
                return out, (
                    f"效果 LoRA 第 {index + 1} 项必须是对象"
                    f"（含 name / strength / enabled / trigger），"
                    f"收到的是 {type(item).__name__}：{item!r}")
            if item.get("enabled") is False:
                continue
            name = (item.get("name") or "").strip()
            if not name:
                # 空名字 = 这一项没有内容（用户清空后提交的残项）-> 跳过。
                continue
            resolved, path = effect_registry.lora_registry.resolve_lora(name)
            if not resolved or path is None:
                return out, f"本机找不到该效果 LoRA 文件：{name}"
            check = effect_registry.check_effect_lora_compat(
                Path(path), unet_path)
            if not check.get("compatible"):
                return out, (f"「{resolved}」与当前底模不兼容："
                             f"{check.get('reason') or '未知原因'}")
            try:
                strength = float(item.get("strength", 1.0))
            except (TypeError, ValueError):
                return out, f"效果 LoRA 强度必须是数字：{name}"
            if strength <= 0:
                continue
            # 上限 2.0（与 ComfyUI 节点的取值域一致；作者推荐值更低时
            # 由前端滑杆上限体现）
            strength = min(2.0, strength)
            effect_loras.append({
                "name": resolved,
                "strength": strength,
                "enabled": True,
                "trigger": (item.get("trigger") or "").strip(),
            })
    out["effect_loras"] = effect_loras

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
    # ---- 节点存在性判定：只在「探测完整」时才算数 ----
    #
    # 【2026-11-14 修 —— 这是个真实存在的坑，不是理论问题】
    # `_COMFY_NODE_NAMES` 只有在**成功探测过一次** ComfyUI 之后才有值。
    # 内置 Agent 这条路不经过面板（不调 /api/video-options），
    # 所以第一次进来时它可能是**空集**。
    #
    # 原来的判据 `node not in _COMFY_NODE_NAMES` 会把「**还没探测过**」
    # 误判成「**本机没有这个节点**」-> 用户看到
    # 「本机 ComfyUI 里没有节点 ModelAttentionBackend」，
    # 但节点其实**就在**（实测本机 /object_info 里恒在）。
    #
    # 修法：区分三态 ——
    #   * 探测完整（_COMFY_PROBE_COMPLETE）且节点不在 -> 真缺节点 -> 拒绝；
    #   * 没探测过 / 探测不完整 -> **不拦**（放行，让 ComfyUI 自己报错）。
    #     这与 worker 的既有口径一致（见 server.py 里
    #     `params["_h3image_nodes_available"] = None if not probe_ok else nodes_ok`）。
    #
    # 【为什么不能无条件放行】那会让「用户选了本机真没有的注意力机制」
    # 变成任务建成了、跑到 ComfyUI 才炸 —— 那才是静默失效。
    # 所以只在「问全了」时才敢断言「缺节点」。
    if node and _COMFY_PROBE_COMPLETE and node not in _COMFY_NODE_NAMES:
        return out, (f"本机 ComfyUI 里没有节点 {node}"
                     f"（{mode_meta['label']}）—— 无法使用该注意力机制。")
    out["attention"] = attention

    # ---- 二采 ----
    #
    # 【2026-11-18 新增 —— 接续段默认走标准采样（等价 nonebot seg2）】
    #
    # 【为什么必须在**归一化层**做，而不是只在前端把开关关掉】
    # 前端 `videoState.twopass.enabled` 的默认值确实是 false，但
    # `/api/video-config`（**服务端持久化**的面板配置）会把用户上次
    # 勾选的状态在每次页面加载时恢复回来
    # （web/video-panel.js 的 `applyConfig(saved)`）。
    # ⇒ 「默认关闭」这个语义**只在服务端归一化层才成立**：
    # 任何未显式开二采的入参，落到工作流里都必须是标准采样节点。
    #
    # 【为什么接续段要禁】
    # 2026-11-18 实测（13 条链）：二采 ON 的 2 条段头断点比 2.58x / 4.54x、
    # 3 帧亮度降 +4.7 / +7.2 灰阶；OFF 的 11 条全部 ≤1.58x / ≤+0.26。
    # 突变落在**段 2 内部 frame0→1**，不在拼接帧 —— 肉眼就是「第 5 秒那一下」。
    # 而 nonebot `minimax_h3_seg2.json` 用的是**标准 SamplerCustomAdvanced**
    # （无 SelfLift）⇒ 「等价 seg2」= 接续段默认走标准采样。
    #
    # 【可行性】ComfyUI 的标准 SamplerCustomAdvanced **会**读 latent 的
    # noise_mask 并作为 denoise_mask 传给采样器
    # （comfy/comfy_extras/nodes_custom_sampler.py:1047-1055）
    # ⇒ 拷贝桥的钉住区在标准采样路径上照常生效，不会「换回标准就失去钉帧」。
    twopass = params.get("twopass") or {}
    if not isinstance(twopass, dict):
        twopass = {}
    enabled = bool(twopass.get("enabled"))
    if enabled:
        # ---- 接续段的二采策略（h3studio/twopass_policy.py）----
        # 【为什么必须调统一入口（resolve_effective_twopass）而不是各答各的】
        #   两层各答各的会出现「归一层放行、工作流层禁用」或反过来的分裂
        #   —— 那会让显式放行口子失效、或让接续段又走回 SelfLift。
        #   统一入口保证口径一致（另处调用的是同一个函数）。
        _tp_use, _tp_policy = twopass_policy.resolve_effective_twopass(params)
        if _tp_policy == "deny":
            # 接续段禁用二采 -> 强制走标准采样路径。
            # 【为什么记 normalize_notes】「面板勾了二采但没生效」必须让用户
            # 看得见，否则会以为是自己操作错了。归一说明由任务详情如实展示。
            _cont_info = params.get("continuation")
            _cont_info = _cont_info if isinstance(_cont_info, dict) else {}
            _stage = _cont_info.get("stage_index")
            try:
                _stage = int(_stage) if _stage is not None else None
            except (TypeError, ValueError):
                _stage = None
            out.setdefault("normalize_notes", []).append(
                twopass_policy.twopass_disabled_note(mode, _stage))
            enabled = False
        elif _tp_policy == "warn":
            out.setdefault("normalize_notes", []).append(
                twopass_policy.twopass_warn_note(mode))
    if enabled:
        # 同上：只在「探测完整」时才断言缺节点（见上面 attention 的说明）。
        if (_COMFY_PROBE_COMPLETE
                and "SelfLiftH3Sampler" not in _COMFY_NODE_NAMES):
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
            # 【2026-11-16 修 D-8 —— 静默夹回要留痕】
            # 夹回是**必要**的（SelfLift 的硬约束），但「用户拖了 6、
            # 实际跑 3」这件事必须让用户看得见，否则参数卡与面板对不上，
            # 用户无从判断原因。归一说明记进 params["normalize_notes"]，
            # 由任务详情展示（与图像族同一套机制）。
            _video_note(out, "twopass.transition_step", transition,
                        max(1, min(transition, max_transition)),
                        "（上限受当前步数档的 sigma 表长度限制）")
            transition = max(1, min(transition, max_transition))
        # 【2026-11-17 修：二采 NFE 预算护栏 · 接缝色档残缺陷】
        #
        # 【缺陷】二采接续链在段2/段1 交界处有一次肉眼可见的闪烁。
        #   受控复现（线上真实产物，老提示词+老种子）：接续比值 15.372x，
        #   而非二采对照只有 1.622x。
        #
        # 【重新取证的结论 —— 与上一轮归因方向不同】
        #   上一轮把断点当成「拼接点跳变」；本轮用独立测量器拆开量后发现：
        #   断点在**段2 自己内部**（段2 第 0 帧 -> 第 1 帧，RGB L1 差 13.75），
        #   不在拼接点。所以上一轮加的「拼接 setpts + match_prev」打不中要害。
        #
        # 【机制】ComfyUI 日志的 `[SelfLift plan]` 行（决定性证据）：
        #   steps=4 transition_step=3 -> low_nfe=3 high_nfe=1 sigma_resume=0.7207
        #   steps=7 transition_step=5 -> low_nfe=4 high_nfe=5 sigma_resume=0.5868
        #   SelfLift 把 NFE 预算切成「低分辨率 ts 步 + 高分辨率 (steps-ts) 步」。
        #   默认档 steps=4 + transition_step=3 ⇒ **高分辨率二采只剩 1 步 NFE**，
        #   而这 1 步的起始 sigma 高达 0.72。
        #   低分辨率一采在**降采样空间**里定下了色档与细节档；抬升回来后，
        #   高分辨率二采要同时做「提分辨率 + 收敛色档 + 生成内容」三件事，
        #   1 步预算不够 ⇒ 段头几帧色档+细节档双重掉档（肉眼 = 闪一下）。
        #   空间高频指纹佐证：旧档段2 帧0..3 高频只有段中段的 15%。
        #
        # 【修法（Studio 侧，零 comfyui 改动）】
        #   SelfLift 的 transition_step **就是**公开的节点输入，它同时决定
        #   高分辨率还剩几步 NFE（纯算术：high_nfe = steps - transition_step）。
        #   所以在 Studio 侧把它夹到「高分辨率至少拿一半预算」即可 ——
        #   **不需要**改第三方节点源码（实测 ts=2 后：15.372x -> 2.996x，
        #   段2 内部断点 13.75 -> 1.08，高频比 0.899 -> 1.108）。
        #
        # 【代价与留痕】夹取是有损的（一采步数变少 = 低分辨率提速收益变小），
        #   所以**必须留痕**，让用户看得见「一采步数被从 3 改成 2」。
        _budget_steps = steps + (extra_steps if refine_enabled else 0)
        _budget_transition = twopass_budget.clamp_transition_step(
            _budget_steps, transition)
        if _budget_transition != transition:
            _video_note(
                out, "twopass.transition_step", transition, _budget_transition,
                "（高分辨率二采至少要拿一半的 NFE 预算："
                + twopass_budget.describe(_budget_steps, _budget_transition)
                + "；否则段头会色档掉档 = 接续点闪烁）")
            transition = _budget_transition
        lowres = float(twopass.get("lowres_scale")
                       or video_models.VIDEO_TWOPASS["lowres_scale_default"])
        lowres = max(0.25, min(1.0, lowres))
        rho = float(twopass.get("rho", video_models.VIDEO_TWOPASS["rho_default"]))
        upscaler = twopass.get("upscaler_model") \
            or video_models.VIDEO_TWOPASS["upscaler_default"]
        if rho <= 0 and upscaler == "none":
            # SelfLift 节点会直接 raise：两条路都关掉了
            # 【2026-11-16 修 D-8】同样是静默改写 —— 留痕。
            _video_note(out, "twopass.rho", rho, 0.6,
                        "（ρ=0 且不使用放大器会被 SelfLift 节点拒绝）")
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
    # 【2026-11-15 修 AC-1】默认采样档跟着**实际挂的加速 LoRA**走：
    #   挂了 -> 该 LoRA 的 recommended_steps / scheduler / sampler
    #           （4 步蒸馏档 = beta + euler）
    #   没挂 -> 标准多步采样档（VIDEO_STEPS_WITHOUT_ACCEL=20 步
    #           + VIDEO_SCHEDULER_WITHOUT_ACCEL=simple
    #           + VIDEO_SAMPLER_WITHOUT_ACCEL=res_multistep）
    #
    # 旧实现恒取 model["steps"]（= 4，蒸馏档）=> 显式「不挂加速 LoRA」时
    # 也跑 4 步，那是非蒸馏权重配蒸馏步数，画面必然糊。
    # 后端 video_workflow._resolve_accel_chain 的默认档本来就是对的
    # （它按 accel_chain 走），但那条分支只在 params["steps"] 为空时可达；
    # 这里先把**默认值**补对，两边口径一致。
    _accel_meta = video_models.accel_lora_meta(out.get("accel_lora") or "") or {}
    if out.get("accel_lora"):
        _default_steps = (int(_accel_meta.get("recommended_steps") or 0)
                          or int(model.get("steps", 4)))
        _default_scheduler = (_accel_meta.get("recommended_scheduler")
                              or model.get("scheduler", "beta"))
        _default_sampler = (_accel_meta.get("recommended_sampler")
                            or model.get("sampler", "euler"))
    else:
        _default_steps = video_models.VIDEO_STEPS_WITHOUT_ACCEL
        _default_scheduler = video_models.VIDEO_SCHEDULER_WITHOUT_ACCEL
        _default_sampler = video_models.VIDEO_SAMPLER_WITHOUT_ACCEL
    steps = video_models.normalize_steps(params.get("steps"), _default_steps)
    out["steps"] = steps
    out["sampler"] = video_models.normalize_sampler(
        params.get("sampler"), _default_sampler)
    out["scheduler"] = video_models.normalize_scheduler(
        params.get("scheduler"), _default_scheduler)
    # 【2026-11-16 修 D-8 —— 越界回落要留痕】
    # 这三个值被夹回时（用户传了越界 steps / 不存在的采样器 / 调度器），
    # 此前**完全没有说明** -> 参数卡显示的回落值与用户填的值对不上，
    # 用户无从判断原因。现在统一记进 params["normalize_notes"]，
    # 由任务详情展示（与图像族 gen_normalize 同一套机制）。
    _video_note(out, "steps", params.get("steps"), steps)
    _video_note(out, "sampler", params.get("sampler"), out["sampler"])
    _video_note(out, "scheduler", params.get("scheduler"), out["scheduler"])
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

    # 种子已在**函数最前面**校验并归一（见上面「---- 种子（**最先**校验）----」）
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


def _lineage_prompts(job_id: str) -> list[dict]:
    """本段的**血缘提示词**列表（从链首段到本段，按顺序）。

    【为什么需要】用户需求（2026-11-17）：接续时点「增强」，
    要把**此前所有段落的提示词**一并提供给增强模型 —— 否则
    第 4 段的增强结果只知道第 4 段写了什么，跨段人物/场景/剧情
    要素会丢，跨段一致性无从保证。

    【沿用血缘口径】与 `_lineage_segment_rels` **完全同源**：
    沿 `source_job` 指针回溯。所以兄弟分支不会被混进来
    （B1 链的历史里不会有 B2）。

    【每段只取自己的那一份】每段任务记录里 `params["prompt"]`
    就是该段用户写（或增强后）的提示词 —— 直接读，不重算。

    返回：[{"stage": 段号, "job_id": 任务 id, "prompt": 提示词}, ...]
    """
    out: list[dict] = []
    seen: set[str] = set()
    current = job_id
    for _ in range(512):
        if not current or current in seen:
            break
        seen.add(current)
        with db() as conn:
            row = conn.execute("SELECT params FROM jobs WHERE id=?",
                               (current,)).fetchone()
        if not row:
            break
        try:
            params = json.loads(row["params"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            break
        info = cont_service.chain_info(params)
        if not info:
            break
        prompt = str(params.get("prompt") or "").strip()
        if prompt:
            try:
                stage = int(info.get("stage_index", 0))
            except (TypeError, ValueError):
                stage = 0
            out.append({"stage": stage, "job_id": current, "prompt": prompt})
        current = str(info.get("source_job") or "").strip()
    out.reverse()
    return out


#: 血缘链时长的**请求级**缓存（job_id -> 各段时长）。
#:
#: 【为什么需要它 —— 2026-11-21 生成媒体库性能修】
#: `_lineage_durations()` 沿 `source_job` 指针回溯，每回溯一段查一次库。
#: 而 `row_to_job()` 对**每条**任务都调它 —— 于是列表接口 100 条
#: = 100 次起步的查询（实测：20 行 -> 23 条 `SELECT params FROM jobs
#: WHERE id=?`，其中大部分来自血缘回溯）。
#:
#: 【为什么不用全局缓存】血缘是**会变**的（接续链在跑的时候段状态
#: 会更新），全局缓存会给出陈旧的累计时长。
#: 请求级缓存的生命周期只有**一次列表请求** —— 同一批返回的行
#: 看到的是**同一份快照**，这是列表接口应有的语义。
#:
#: 【怎么用】`api_list` 在开始构建前调 `_LINEAGE_CACHE.begin()`，
#: 请求结束后调 `_LINEAGE_CACHE.end()`（或用 with 语句）。
_LINEAGE_CACHE: dict = {"active": False, "map": {}}


class _LineageRequestCache:
    """血缘时长的请求级缓存（见 _LINEAGE_CACHE 的说明）。"""

    def __init__(self):
        self._state = _LINEAGE_CACHE

    def begin(self) -> None:
        self._state["active"] = True
        self._state["map"] = {}

    def end(self) -> None:
        self._state["active"] = False
        self._state["map"] = {}

    def get(self, job_id: str):
        if not self._state["active"]:
            return None
        return self._state["map"].get(job_id)

    def put(self, job_id: str, value) -> None:
        if self._state["active"]:
            self._state["map"][job_id] = value


_LINEAGE = _LineageRequestCache()


def _lineage_durations(job_id: str, own_params: dict | None = None) -> list[float]:
    """本段血缘链上**各段的请求时长**（秒），从链首段到本段，按顺序。

    【为什么需要】用户口径（2026-11-17 更正）：
        成片总时长 = Σ(各段请求时长) - (段数-1) × 1 秒
    要算这个，就得知道链上**每段**请求了多少秒。

    【沿用血缘口径】与 `_lineage_segment_rels` / `_lineage_prompts` **完全同源**
    （沿 `source_job` 指针回溯）—— 兄弟分支不会混进来。

    【每段取自己的那一份】每段任务记录里 `params["duration"]` 就是该段
    用户请求的时长（秒）。取不到就回落到该段的总帧数折算。

    返回：[5.0, 5.0, 8.0, ...]（段序）

    【2026-11-21 性能修】
      * `own_params`：调用方（`row_to_job`）**手里已经有本段的 params**
        —— 不必再查一次库。传进来就省一次查询。
      * 请求级缓存：同一次列表请求里，同一个 job_id 的血缘只回溯一次。
    """
    # 请求级缓存命中则直接返回
    cached = _LINEAGE.get(job_id)
    if cached is not None:
        return cached
    out: list[float] = []
    seen: set[str] = set()
    current = job_id
    # 【2026-11-21 性能修】原来每回溯一段就 `with db() as conn` ——
    # 也就是**每段开一次 SQLite 连接**。列表接口 100 条 -> 最多 100 次
    # 连接建立/关闭（实测：SQLite 连接建立占这批查询里相当可观的一部分）。
    # 现在**复用一条连接**（只读，不写 —— 连接本身不持有状态）。
    with db() as conn:
        for _ in range(512):
            if not current or current in seen:
                break
            seen.add(current)
            # 【2026-11-21 性能修】本段的 params 调用方已经有了 -> 不必再查。
            if current == job_id and own_params is not None:
                params = own_params
            else:
                row = conn.execute("SELECT params FROM jobs WHERE id=?",
                                   (current,)).fetchone()
                if not row:
                    break
                try:
                    params = json.loads(row["params"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    break
            info = cont_service.chain_info(params)
            if not info:
                break
            duration = params.get("duration")
            try:
                seconds = float(duration)
            except (TypeError, ValueError):
                seconds = 0.0
            if seconds <= 0:
                # 回落：按该段的总帧数折算（帧数是权威的）。
                try:
                    seg_frames = int(info.get("segment_frames") or 0)
                except (TypeError, ValueError):
                    seg_frames = 0
                seconds = (seg_frames / float(cont.FPS)) if seg_frames else 0.0
            out.append(seconds)
            current = str(info.get("source_job") or "").strip()
    out.reverse()
    # 写回请求级缓存（供同一请求内的其它调用复用）
    _LINEAGE.put(job_id, out)
    return out


def _chain_duration_plan(job_id: str,
                         extra_seconds: float | None = None) -> dict:
    """接续链的**成片时长账**（含「下一段」）。

    参数
      job_id          链上**当前这一段**的任务 id。
      extra_seconds   **下一段**要请求的时长（秒）。
                      前端点「接续」时面板上填的时长就是它 ——
                      于是返回的是「接上这一段之后成片会有多长」。

    【口径 —— 为什么 extra 是「下一段」而不是「本段」】
    用户点「接续」的那张卡片是**上一段**。面板上填的时长是
    **即将生成的那一段**的请求时长。
    所以「点接续时预期的成片时长」= 已有的段 + 即将生成的那一段。

      例：链上已有 1 段 5 秒，面板填 5 秒
        -> 预期成片 = 5 + (5 - 1) = 9 秒 = 216 帧

    【2026-11-17 收口：返回的是**补偿后**的账（唯一真源）】
    成片拼接时会做尾裁（`_concat_chain_video` 的 `trim_tail_frames`），
    让成片帧数**精确**命中用户口径。所以这里返回的
    `total_frames` / `total_seconds` 就是**成片真实的**帧数与时长，
    不再是「不补偿时会有多少」。

    但**保留**两个字段用于诊断与界面说明：
      * `raw_total_frames` —— 不补偿时的帧数；
      * `compensation`     —— 补偿计划（裁了多少帧、为什么）。

    【为什么面板不再需要显示两个数】
    上一轮面板并列显示「按 1 秒重叠口径」与「实际帧数」，是因为那时
    成片**真的**比口径长。现在两者**精确相等**（残差 0 帧），
    并列显示反而让人以为还有差异 —— 所以前端只显示一个数，
    仅在残差非 0 时才补充说明（见 `compensation.notes`）。
    """
    durations = _lineage_durations(job_id)
    if extra_seconds is not None:
        try:
            extra = float(extra_seconds)
        except (TypeError, ValueError):
            extra = 0.0
        if extra > 0:
            durations = durations + [extra]
    # 钉住窗口：从当前段的记录里取（与服务端提交侧同源）。
    window = cont.DEFAULT_CONTEXT_FRAMES
    with db() as conn:
        row = conn.execute("SELECT params FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
    if row:
        try:
            info = cont_service.chain_info(json.loads(row["params"] or "{}"))
            window = cont.snap_context_frames(int(
                info.get("context_frames") or cont.DEFAULT_CONTEXT_FRAMES))
        except (TypeError, ValueError, json.JSONDecodeError):
            window = cont.DEFAULT_CONTEXT_FRAMES

    account = cont.chain_duration_account(durations, window)
    plan = cont.chain_compensation_plan(durations, window)
    # 补偿后各段在成片里实际保留的帧数。
    keep = plan.get("keep_frames") or []
    for index, segment in enumerate(account.get("segments") or []):
        if index < len(keep):
            segment["delivered_frames"] = int(keep[index])
            segment["delivered_seconds"] = round(
                int(keep[index]) / float(cont.FPS), 6)
    account["total_frames"] = sum(int(x) for x in keep) if keep else \
        account["total_frames"]
    account["total_seconds"] = round(
        account["total_frames"] / float(cont.FPS), 6)
    account["formula_seconds"] = round(
        plan.get("formula_frames", 0) / float(cont.FPS), 6)
    account["delta_seconds"] = round(
        account["total_seconds"] - account["formula_seconds"], 6)
    account["raw_total_frames"] = plan.get("current_frames")
    account["raw_total_seconds"] = round(
        (plan.get("current_frames") or 0) / float(cont.FPS), 6)
    account["compensation"] = plan
    # notes 用**补偿后**的口径重写（补偿成功后不再需要说「差了多少」）。
    notes = list(account.get("notes") or [])
    if plan.get("exact"):
        notes = [n for n in notes if "相差" not in n]
        notes.extend(plan.get("notes") or [])
    else:
        notes.extend(plan.get("notes") or [])
    account["notes"] = notes
    return account


async def _finalize_continuation_impl(job_id: str, mode: str, params: dict,
                                       saved: list[dict]) -> list[dict]:
    """接续收尾：登记本段 + 拼接**完整视频**（模块级实现）。

    用户需求第 6 条：「最终媒体库中得到的是**完整视频**
    （上一段 + 本段完整长度）」。所以每接续一段，都要产出
    一个包含**整条链**的完整视频。

    返回加了「完整视频」条目的产物列表。

    【为什么拼接失败不影响本段产物】本段视频本身是好的，
    拼接只是附加产物。拼接失败（比如 ffmpeg 不可用）时，
    本段仍然要出现在生成库里 —— 只是没有完整视频。

    【2026-11-16 放开 flf / r2v】原来只有 t2v 走接续收尾。
    现在三个视频模式都走 —— 落盘坐标、血缘回溯、拼接逻辑
    全是**同一套**（见 continuation.py / _lineage_segment_rels）。

    【2026-11-17 提到模块级】原来是 `Worker._finalize_continuation`
    实例方法，但体内**没有任何 `self.` 引用**。而「服务重启自愈」
    路径是模块级函数、没有 Worker 实例可用 —— 修「5 秒接续 5 秒
    只得到 4 秒」时必须让自愈路径也能调它，故提取到模块级。
    Worker 侧保留同名方法做薄委托（见 Worker._finalize_continuation）。

    【2026-11-17 修复：必须跳过 is_full 的条目】
    本函数要拿的是**本段分段视频**（未拼接的那一段）。
    原实现取 `saved` 里**第一个** video —— 而重跑（自愈重跑 /
    用户重试）时 `saved[0]` 已经是**上一轮的完整成片**（is_full=True），
    于是 `segment_rel` 被**覆盖成成片路径**：
      * 本段的分段登记被冲掉（成片被当成分段）；
      * 下一段按血缘回溯时拿到的是成片 -> 拼接链里混进成片
        -> 成片长度**翻倍**（段1 + 成片 + 段2）。
    修法：只取**非 is_full** 的第一个 video。
    """
    if mode not in ("t2v", "flf", "r2v"):
        return saved
    info = cont_service.chain_info(params)
    if not info:
        return saved

    video = next((o for o in saved
                  if o.get("kind") == "video" and not o.get("is_full")), None)
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

    # ---- 成片音轨：外部音轨（参考音乐 / 参考视频的音轨）----
    #
    # 【为什么成片用「原始参考音频整条」】
    # 每段采样时用的是「1s 空白 ⊕ 切片」（params["segment_audio_file"]），
    # 那是「这一段该听到的东西」。成片要的是「整部片子该听到的
    # 东西」= 原始参考音频整条，长度按合并后视频长。
    # 若成片也用切片，每段开头的 1s 空白会累积（3 段 = 3s 空白）
    # → 音画错位。两条链路必须分开。
    external_audio = ""
    external_label = ""
    if h3acont.is_external_track(str(info.get("audio_track_mode") or "")):
        external_audio = str(info.get("audio_source_file") or "").strip()
        external_label = ("参考音乐" if str(
            info.get("audio_source_kind") or "") == "ref_audio"
            else "参考视频的音轨")

    # ---- 成片尾裁量：让成片帧数精确命中用户口径（2026-11-17 收口）----
    #
    # 用户口径：成片总时长 = Σ(各段请求时长) - (段数-1) × 1 秒。
    # 不补偿时成片会多出几帧（H3 的 17k+5 段长网格 + 拷贝桥只接受
    # 5+17k 网格的钉住窗口，22 帧而不是整 1 秒）。
    #
    # 补偿**只从最后一段的尾部**裁 —— 前面的段一帧不裁，
    # 所以接缝（上一段末帧 → 下一段首帧）的连续性完全不受影响。
    trim_tail_frames = 0
    try:
        durations = _lineage_durations(job_id)
        window = cont.snap_context_frames(int(
            info.get("context_frames") or cont.DEFAULT_CONTEXT_FRAMES))
        plan = cont.chain_compensation_plan(durations, window)
        trim_tail_frames = int(plan.get("trim_frames") or 0)
        if trim_tail_frames:
            print(f"[接续] 成片尾裁计划: 目标 {plan.get('formula_frames')} 帧, "
                  f"裁 {trim_tail_frames} 帧 (各段保留 {plan.get('keep_frames')})")
    except Exception as exc:  # 兜底：算不出来就不裁，绝不因此让成片失败
        print(f"[接续] 尾裁计划计算失败（不裁剪）: {exc}")
        trim_tail_frames = 0

    full = await asyncio.to_thread(_concat_chain_video, segments, chain_id,
                                   external_audio, external_label,
                                   trim_tail_frames)
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


# ------------------------------------------------------------------
# 音频续接时序（首尾帧 / 参考生视频专用）
# ------------------------------------------------------------------
# 【为什么要有这一层】
# 「音轨来源三选一」只存在于 flf / r2v。当音轨来源是「参考音频」或
# 「参考视频的音轨」时，那条音轨是**贴上去**的 —— 它不参与 DiT 采样，
# 所以音画对齐纯粹是**后处理**问题。
#
# 【两条链路必须分开 —— 本节最重要的一条】
#   * 采样用：1s 空白 ⊕ 原始音频从第 N 秒起的切片（每段一个文件）
#   * 成片用：原始参考音频**整条**（合并时贴，长度 = 合并后视频长）
# 若把采样用的切片当成片音轨，每段开头的 1s 空白会**累积**
# （3 段 = 3s 空白）→ 音画错位。
#
# 口径细节见 h3studio/h3_audio_continuation.py（纯函数，可离线单测）。


def _probe_audio_duration(path: Path) -> float | None:
    """音频文件时长（秒）。探测不到返回 None（不猜）。

    用途：算「原始音轨还剩多少秒」-> 越界时给中文提示。
    """
    if not FFPROBE or not path.is_file():
        return None
    try:
        raw = subprocess.check_output(
            [FFPROBE, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=duration", "-of", "json", str(path)],
            encoding="utf-8", timeout=20, creationflags=CREATE_NO_WINDOW)
    except (subprocess.SubprocessError, OSError):
        return None
    try:
        stream = (json.loads(raw).get("streams") or [{}])[0]
        value = float(stream.get("duration") or 0)
    except (json.JSONDecodeError, TypeError, ValueError, IndexError):
        return None
    return value if value > 0 else None


def _comfy_input_local(name: str) -> Path | None:
    """ComfyUI input 里的文件名 -> 本地绝对路径（找不到返回 None）。

    【为什么要处理子目录】
    ComfyUI 上传接口返回的是 `subfolder/name` 形态（本项目固定用
    `h3studio` 子目录），所以同一个文件名可能在 input 顶层，也可能在
    input/h3studio/ 下 —— 两处都要找。
    """
    if not name:
        return None
    raw = str(name).replace("\\", "/").strip()
    parts = [p for p in raw.split("/") if p and p != "."]
    if not parts:
        return None
    filename = parts[-1]
    roots = (Path("/home/ubuntu/comfyui/input"), Path("/root/ComfyUI/input"))
    # 先试带子目录的完整相对路径，再试顶层文件名。
    for root in roots:
        candidate = root
        for part in parts:
            candidate = candidate / part
        if candidate.is_file():
            return candidate
    for root in roots:
        candidate = root / filename
        if candidate.is_file():
            return candidate
    return None


def _make_segment_audio(source: Path, out_path: Path, *,
                         start_seconds: float, duration_seconds: float,
                         lead_blank: float) -> bool:
    """生成「1s 空白 ⊕ 原始音频切片」这一个**采样用**的段音频文件。

    【为什么用 adelay 而不是拼两个文件】
    `adelay` 在音频**前面**插入静音（延迟），正好就是「1s 空白 ⊕ 切片」
    的结构，且只跑一次 ffmpeg。`apad` 是**尾部**补静音，方向相反。

    【⚠ -t 必须放在输入侧（实测抓到的坑）】
    `ffmpeg -ss S -i IN -t D ...`（-t 在输出侧）会**先解码整条**再截断，
    而 `adelay` 加的 1s 空白在解码之后 —— 于是 `-t D` 把「1s 空白 +
    切片」整体截成 D 秒，**1s 空白被裁掉了**。
    实测（149.88s 的 flac，切 2.333333s 起、1.416667s 长）：
      -t 在输出侧 -> 1.416009s（空白没了）
      -t 在输入侧 -> 2.416009s（空白 + 切片，正确）
    `-ss` 与 `-t` 都在输入侧时，ffmpeg 只解码需要的那一段，
    既快又不会裁掉前导空白。

    返回 True = 成功；False = 失败（调用方必须报错，不静默）。
    """
    if not FFMPEG:
        return False
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    delay_ms = max(0, int(round(float(lead_blank) * 1000)))
    args = [FFMPEG, "-y",
            "-ss", f"{max(0.0, float(start_seconds)):.6f}",
            "-t", f"{max(0.0, float(duration_seconds)):.6f}",
            "-i", str(source)]
    if delay_ms > 0:
        args += ["-af", f"adelay=delays={delay_ms}:all=1"]
    args += ["-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
             str(out_path)]
    try:
        result = subprocess.run(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=300,
                                creationflags=CREATE_NO_WINDOW)
    except (subprocess.SubprocessError, OSError):
        return False
    if result.returncode != 0 or not out_path.is_file():
        detail = (result.stderr or b"").decode("utf-8", "ignore")[-300:]
        print(f"[音频接续] 切段音频失败（{source}）：{detail}")
        return False
    return True


async def _prepare_segment_audio(session: ClientSession, job_id: str,
                                  params: dict, plan: dict) -> str:
    """为接续段准备「1s 空白 ⊕ 切片」的段音频，上传到 ComfyUI input。

    返回 ComfyUI 里的文件名（失败时抛 RuntimeError，带中文原因）。

    【输入来源】
      * reference -> 原始参考音频（params["continuation"]["audio_source_file"]）
      * video_bg  -> 参考视频抽出来的音轨（同上字段）
    """
    info = cont_service.chain_info(params)
    source_name = str(info.get("audio_source_file") or "").strip()
    if not source_name:
        raise RuntimeError(
            "接续段缺少原始参考音轨（上游没记录音轨文件）—— "
            "请改用「复用参数」重新生成一段，或在上一段重新选择参考音乐。")
    local = _comfy_input_local(source_name)
    if local is None:
        raise RuntimeError(
            f"接续段找不到原始参考音轨文件（{source_name}）—— "
            f"它可能已被清理。请改用「复用参数」重新生成一段。")
    out_dir = UPLOAD_DIR / "segment_audio"
    digest = hashlib.sha256(
        f"{job_id}:{plan.get('source_start')}:{plan.get('source_duration')}"
        f":{plan.get('lead_blank')}".encode("utf-8")).hexdigest()[:16]
    out_path = out_dir / f"seg-{digest}.m4a"
    if not out_path.exists():
        ok = await asyncio.to_thread(
            _make_segment_audio, local, out_path,
            start_seconds=float(plan.get("source_start") or 0.0),
            duration_seconds=float(plan.get("source_duration") or 0.0),
            lead_blank=float(plan.get("lead_blank") or 0.0))
        if not ok:
            raise RuntimeError(
                "切不出接续段的参考音频（ffmpeg 失败）—— "
                "请检查原始参考音轨是否可读，或改用「复用参数」重新生成一段。")
    return await comfy_upload_media(session, out_path)


def _concat_chain_video(segments: list[str], chain_id: str,
                        external_audio: str | None = None,
                        external_audio_label: str = "",
                        trim_tail_frames: int = 0) -> Path | None:
    """把链上的分段视频按序拼成**完整视频**。返回输出路径（失败返回 None）。

    【为什么用 concat demuxer 而不是 filter_complex】
    concat demuxer 直接拼流，不重新编码 —— 快、不掉画质。
    分辨率/帧率不一致时 ffmpeg concat 会出问题，所以先探测第一段的
    尺寸并统一（与 run_longfilm 的做法一致）。

    【音画同步】分段视频各自已经是音画同裁的（TrimAV 裁重叠时
    音画一起裁），所以直接拼接不会累积 A/V 偏移。

    【external_audio —— 成片音轨用「原始参考音频整条」】
    当音轨来源是「参考音乐」/「参考视频的音轨」时，那条音轨是**贴上去**的，
    而且它**不参与采样** —— 每段采样时用的是「1s 空白 ⊕ 切片」。
    若成片也用切片，每段开头那 1s 空白会累积（3 段 = 3s 空白）→ 音画错位。
    所以成片必须贴**原始音频整条**，并按合并后视频长度对齐：
      * 视频长于音轨 -> `-af apad` 补静音；
      * 视频短于音轨 -> `-shortest` 截断。
    （与 nonebot 咏唱 h3 的 concat_full_video 同一套参数。）

    【trim_tail_frames —— 成片尾裁（2026-11-17 收口）】
    用户口径（最终标准）：成片总时长 = Σ(各段请求时长) - (段数-1) × 1 秒。
    不补偿时成片会多出几帧（H3 的 17k+5 段长网格 + 拷贝桥只接受
    5+17k 网格的钉住窗口，22 帧而不是整 1 秒）。

    这里只从**最后一段的尾部**裁掉这么多帧 —— 前面的段一帧不裁，
    所以接缝（上一段末帧 → 下一段首帧）的连续性完全不受影响。
    计算见 `h3studio/continuation.py::chain_compensation_plan`。
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
    if len(paths) == 1 and not external_audio:
        # 只有一段 —— 完整视频就是它自己
        return paths[0]
    if not FFMPEG:
        print("[接续] 未找到 FFmpeg，无法拼接完整视频")
        return None
    width, height = _probe_video_size(paths[0])
    list_path = DATA / f"concat-cont-{cont.sanitize_run_id(chain_id)}.txt"

    def quote_concat(path: Path) -> str:
        return str(path).replace("'", "'\\''")

    external_path = None
    if external_audio:
        external_path = _comfy_input_local(external_audio)
        if external_path is None:
            # 成片音轨文件找不到 -> **明确降级**（用各段自己的音轨），
            # 不静默失败：打印日志，成片仍然产出。
            print(f"[接续] 找不到外部音轨 {external_audio}，成片改用各段自己的音轨")
            external_path = None

    try:
        list_path.write_text(
            "\n".join(f"file '{quote_concat(p)}'" for p in paths),
            encoding="utf-8")
        output_path = _next_output_path("video", ".mp4")
        # ---- 滤镜链（2026-11-17 修：接续点闪烁 · 缺陷 1）----
        #
        # 【原来的滤镜】scale+pad+setsar+**fps=24**+format
        # 【为什么 fps=24 会多出 1 帧】
        #   分段视频的**第一帧 PTS = 0 但 duration 不是 1/24**
        #   （实测段1 首帧 duration=0.083333、段2=0.041667），
        #   concat demuxer 拼起来后各段的时间戳会撞在一起。
        #   `fps=24` 滤镜按**输出时间轴**重采样：它从第一个输出时刻
        #   （实测成片首帧 PTS=0.041016，而不是 0）开始铺帧 ->
        #   **每个分段边界都有一帧被复制**，成片比「各段帧数之和」多 1 帧。
        #
        # 【实测证据】（线上真实产物）
        #   段1 124 帧 + 段2 102 帧 = 226，成片却是 **227 帧**；
        #   成片[0]=段1[0]、成片[1]=段1[0]（重复）、成片[2]=段1[1]…；
        #   接续点也从应有的帧 124 挪到了帧 125 —— **多出来的那一帧
        #   正好落在接续点上**，这就是用户肉眼看到的「闪烁一下」。
        #
        # 【修法】用 `setpts=N/FRAME_RATE/TB` 把每帧的时间戳**按帧序号重排**
        #   （而不是按原 PTS 插值），再交给 fps=24 —— 这样 fps 滤镜
        #   铺出的帧与输入帧一一对应，不会凭空多一帧。
        #   实测：加 setpts 后成片正好 226 帧，接续点回到帧 124。
        #
        # 【为什么不用 nonebot 的 -c:v copy】
        #   nonebot 的默认路径是 copy（零损失），但它要求各段编码参数
        #   **完全一致**（`h3_chain_video.py:_segments_look_compatible`）。
        #   我们这里仍需 scale/pad 统一尺寸 + 可能替换音轨，
        #   所以保留重编码，但把时间轴修对。
        #
        # 【成片尾裁（2026-11-17 收口）：让成片帧数精确命中用户口径】
        #   用户口径：成片总帧数 = Σ(各段请求秒数×24) - (段数-1)×24
        #   （5s→5s = 216 帧；5s→5s→8s = 384 帧）。
        #
        #   实现要点（实测踩过的坑）：
        #   1) 用 **`-frames:v N`** 而不是 `select` 滤镜。
        #      `select='lt(n\,216)'` 这类写法经 `-vf` 传参时**静默失效**
        #      —— ffmpeg 退出码 0，但输出 `video:0KiB`（0 帧），
        #      坑在于「不报错」而不是「报错」。`-frames:v` 是输出选项，
        #      语义明确、可验证。
        #   2) `-frames:v` 作用在**滤镜链之后**。滤镜链
        #      （setpts + fps）把输入帧 1:1 铺开，所以「前 N 帧」
        #      = concat 输入的前 N 帧。
        #   3) 只裁**最后一段的尾部**：前面的段一帧不裁，
        #      接缝（段0 末帧 → 段1 首帧）的连续性才不受影响。
        #      实测若裁链首段尾部，接缝跳变从 0.67 涨到 5.8（10.32x）。
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
              f"force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
              f"setsar=1,setpts=N/FRAME_RATE/TB,fps={cont.FPS},format=yuv420p")
        trim_tail = max(0, int(trim_tail_frames or 0))
        target_frames = 0
        if trim_tail > 0 and len(paths) >= 2:
            # 各段帧数（用来算 concat 之后的总帧数）。
            counts = [_probe_video_frame_count(p) for p in paths]
            if all(c > 0 for c in counts):
                total_in = sum(counts)
                target_frames = total_in - trim_tail
                print(f"[接续] 成片尾裁 {trim_tail} 帧（用户口径对齐）: "
                      f"输入 {total_in} 帧 -> 输出 {target_frames} 帧 "
                      f"= {target_frames / cont.FPS:.4f}s")
            else:
                print("[接续] 段帧数探测失败，跳过成片尾裁")
        command = [FFMPEG, "-y", "-f", "concat", "-safe", "0",
                   "-i", str(list_path)]
        if external_path is not None:
            # 外部音轨：视频流来自 concat，音频流来自原始参考音频。
            # -map 0:v -map 1:a
            command += ["-i", str(external_path)]
            command += ["-map", "0:v", "-map", "1:a"]
        else:
            command += ["-map", "0:v", "-map", "0:a"]
        command += ["-vf", vf,
                    "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "44100"]
        if target_frames > 0:
            # 【关键】-frames:v 放在编码参数**之后**、输出文件**之前** ——
            # 它是「输出选项」，必须紧挨输出文件才生效。
            # 效果：滤镜链输出前 target_frames 帧，其余丢弃
            #       ⇒ 成片帧数精确等于用户口径。
            command += ["-frames:v", str(target_frames)]
        if external_path is not None:
            # 视频长 -> 补静音；视频短 -> 截断。两者同时给 = 音轨长度
            # 精确等于合并后视频长度（题面口径）。
            command += ["-af", "apad", "-shortest"]
        command += ["-movflags", "+faststart", str(output_path)]
        result = subprocess.run(command, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=600,
                                creationflags=CREATE_NO_WINDOW)
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace")[-800:]
            print(f"[接续] 拼接失败: {message}")
            if output_path.exists():
                output_path.unlink()
            return None
        if target_frames > 0:
            # 【自检】尾裁后必须**真的**是目标帧数 —— 不符就说明
            # ffmpeg 的行为与预期不同，绝不静默交付一个不对的成片。
            actual = _probe_video_frame_count(output_path)
            if actual and actual != target_frames:
                print(f"[接续] ⚠ 成片尾裁后帧数不符: 期望 {target_frames} 帧, "
                      f"实际 {actual} 帧 —— 保留产物但记录异常")
            else:
                print(f"[接续] 成片尾裁校验通过: {actual or target_frames} 帧 "
                      f"= {(actual or target_frames) / cont.FPS:.4f}s")
        if external_path is not None:
            print(f"[接续] 成片音轨使用原始参考音频"
                  f"{('（' + external_audio_label + '）') if external_audio_label else ''}"
                  f": {external_path.name}")
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


def _inherit_audio_continuation(*, source_mode: str, source_params: dict,
                                source_info: dict, segment_total: int,
                                window: int, chain_total: int) -> dict:
    """接续段的**音频时序**状态（继承自上游）。

    返回要写进 params["continuation"] 的字段：
      audio_track_mode        音轨来源（generated / reference / video_bg）
      audio_source_kind       原始音轨来源（ref_audio / ref_video）
      audio_source_file       **原始**音轨文件（成片用整条 / 切片用源）
      audio_source_duration   原始音轨总时长（秒）
      audio_consumed_frames   到本段为止已消费的原始音轨帧数（口径 B 的 C_n）
      audio_lead_blank        前导空白（秒）
      audio_segment_start     本段从原始音轨取的起点（秒）
      audio_segment_duration  本段切片时长（秒）
      audio_warnings          [中文提示, ...]

    【口径 —— 为什么 consumed 用 chain_total】
    C_n = 上游**已累计交付**的帧数。上游的 chain_total_frames 就是
    这个值（它按「上游已累计 + 上游本段交付」算好，见
    continuation_service.continuation_state）。
    而本段**自己**的交付帧数 = segment_total - window。
    → 下一段的 C_(n+1) = C_n + (segment_total - window)。

    【音轨来源以链首段为准】
    上游的 params["continuation"] 里已经记着它继承来的音轨状态；
    链首段自己记的是它自己选的那个。所以「继承上游」= 链首段说了算。

    【t2v 不涉及】t2v 的音轨恒为生成音频（音画同源），不切。
    """
    empty: dict = {}
    if source_mode not in ("flf", "r2v"):
        return empty

    upstream_info = source_info or {}
    track_mode = str(upstream_info.get("audio_track_mode") or "").strip()
    source_kind = str(upstream_info.get("audio_source_kind") or "").strip()
    source_file = str(upstream_info.get("audio_source_file") or "").strip()
    source_duration = upstream_info.get("audio_source_duration")
    try:
        source_duration = (None if source_duration is None
                           else float(source_duration))
    except (TypeError, ValueError):
        source_duration = None
    consumed = int(upstream_info.get("audio_consumed_frames") or 0)

    # 链首段（没有上游记录）-> 用**本段自己**的参数推断。
    if not track_mode:
        track_mode = str(source_params.get("audio_track_source") or "").strip()
        track_mode = h3audio.normalize_track_source(
            track_mode,
            has_reference_audio=bool(source_params.get("ref_audios")),
            has_reference_video_audio=bool(
                source_params.get("ref_video_audios")))
        if track_mode == h3audio.TRACK_REFERENCE:
            source_kind = "ref_audio"
            refs = source_params.get("ref_audios") or []
            source_file = str(refs[0]) if refs else ""
        elif track_mode == h3audio.TRACK_VIDEO_BG:
            source_kind = "ref_video"
            refs = source_params.get("ref_video_audios") or []
            source_file = str(refs[0]) if refs else ""

    if not h3acont.is_external_track(track_mode):
        # 生成音频：音画同源，不切。
        return {}

    # ---- C_n：上游已累计交付的帧数。
    #
    # 【为什么恒取 chain_total】
    # chain_total 来自上游任务自己的记录（continuation_state 的
    # total_frames）—— 它就是「上游已累计交付的帧数」，也就是口径 B 的
    # C_n，**不需要**再按「上游有没有上游」分支。
    #
    # 【原来的 bug（实测抓到）】这里写的是
    #   if upstream_info["source_job"]: consumed = chain_total
    #   else:                          consumed = 0
    # —— 而「上游是链首段」时，upstream_info["source_job"] 恰好是**空串**
    # （链首段没有上游）。于是**第 2 段的音频起点被算成 0**，
    # 也就是「第 2 段又从参考音频的第 0 秒开始听」——
    # 前一段已用过的音频被重复用了一遍（实测：段2 的
    # audio_segment_start = 0.0，而正确值是 56/24 = 2.333s）。
    #
    # C_n 的定义只跟「上游链上累计了多少帧」有关，与上游自己是不是
    # 链首段无关 —— 所以这里恒取 chain_total。
    consumed = int(chain_total or 0)

    plan = h3acont.segment_audio_plan(
        consumed_frames=consumed,
        segment_frames=segment_total,
        context_frames=window,
        source_duration=source_duration)

    state = h3acont.chain_audio_state(
        track_source=track_mode,
        source_kind=source_kind,
        source_file=source_file,
        source_duration=source_duration,
        consumed_frames=consumed)
    state.update({
        "audio_segment_start": plan["source_start"],
        "audio_segment_duration": plan["source_duration"],
        "audio_segment_offset": plan["segment_start"],
        "audio_segment_skip_frames": plan["skip_frames"],
        "audio_sampling_duration": plan["sampling_duration"],
        "audio_warnings": plan["warnings"],
    })
    return state


def _prepare_continuation_params(params: dict) -> str:
    """校验并补全任务的接续参数。返回错误信息（空串 = 通过）。

    前端提交时带的 `continuation` 字段来自「接续上下文」回填 —— 它描述的是
    **上一段**。本段是**新的一段**，所以段号要 +1、接力棒指向上一段。

    服务端在这里做四件事：
      1. 校验上游产物支持接续（模式 + 潜空间文件在不在）；
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
    # ---- [2026-11-19 第 7 轮审查] ----
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

    # 本段总帧数（17k+5 网格）与本段交付帧数（总帧数 - 钉住帧数）
    try:
        duration = float(params.get("duration") or 5)
    except (TypeError, ValueError):
        duration = 5.0
    segment_total = cont.align_frame_count(int(round(duration * cont.FPS)))

    # 累计帧数：上游已累计的 + 本段交付的。
    #
    # 【为什么不能信前端传的 chain_total_frames】前端传的值来自上游的
    # 卡片，可能过期。**权威值**是上游任务自己记录里的那份 ——
    # 分叉时每个分支各自累计，互不干扰（上游的累计值只属于上游那条血缘）。
    upstream_state = cont_service.continuation_state(source_mode,
                                                     source_params, source_job)
    chain_total = int(upstream_state.get("total_frames") or 0)

    # ---- 音频续接时序（首尾帧 / 参考生视频专用）----
    #
    # 【口径】
    #   t_a_start(n) = C_n / 24，C_n = 上游已累计**交付**的帧数。
    #   本段交付 = 本段总帧数 - 钉住窗口（钉住区会被裁掉）。
    #
    # 【音轨来源以链首段为准】
    # nonebot 的做法是「整部长视频共用第 1 段的 audio_decision」。
    # 中途改音轨来源会让前段的采样音频与后段对不上 ——
    # 所以这里从**上游**继承，而不是看本段前端传了什么。
    #
    # 【为什么只有 reference / video_bg 才需要切片】
    # generated 档音画同源（音频是采样出来的），段内天然同步，不切。
    audio_state = _inherit_audio_continuation(
        source_mode=source_mode,
        source_params=source_params,
        source_info=source_info,
        segment_total=segment_total,
        window=window,
        chain_total=chain_total,
    )

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
    # 音频接续状态（首尾帧 / 参考生视频）—— 继承自上游。
    params["continuation"].update(audio_state)

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
        params["continuation"]["ref_video_frame_count"] = \
            upstream_ref_frames
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
    # 【为什么每个视频任务都要有链信息】用户需求：「每次生成自动把
    # 可接续的潜空间文件落盘保存，与产物记录关联并可在生成库中识别」。
    # 所以**每一个**视频任务都是一条链的第 0 段：它会落盘自己的潜空间，
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
    # 【2026-11-16 放开 flf / r2v】原来只有 t2v 建链。
    # 现在三个视频模式都建 —— 因为 flf / r2v 的工作流里
    # **真的接了桥**（h3_video_modes._build_common 的 300:save /
    # 300:load / 300:bridge），潜空间**真的落盘**了。
    # 只放开模式白名单而不建链 = 接力棒没有落盘坐标 = 点了接续
    # 会被服务端明确拒绝（而不是静默退化成普通生成）。
    #
    # 【二采】二采**照样建链、照样落盘、照样接续**（2026-11-17 修正）。
    # 原来这里的注释写「二采不读回、不接桥」—— 那是旧结论，已被实测推翻：
    # 二采落盘的 latent 与 17k+5 网格天然对齐、SelfLift 原生支持
    # 拷贝桥的 noise_mask，所以二采段现在既能产出接力棒、也能消费接力棒。
    # 详见 h3studio/continuation.py 的模块说明（实测修正段）。
    if mode in ("t2v", "flf", "r2v"):
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
    if mode in ("t2i", "t2i_qwen", "i2i_qwen", "t2i_moss",
                "t2i_h3image", "i2i_h3image"):
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


def refresh_fingerprinted_path(params: dict) -> str:
    """把 params 里的接力棒读回路径刷新成**当前内容指纹**的副本。

    【为什么需要（第 4 轮收口 —— 真实缺陷）】
    Agent 的 `retry_job` 把源任务的 params **整份拷贝**过来，
    `continuation.source_latent_path` 是**上一次**接续时算出来的带指纹副本。
    Relay-Kit 落盘是**原子写**（os.replace 换 inode）⇒ 旧副本仍指向旧 inode、
    内容停留在旧版本 ⇒ 上游重跑后 retry 读回**上一版的 latent**（静默错误）。

    面板提交与 Agent create 走 `_prepare_continuation_params`，
    那条路径本来就会重算；**只有 retry** 会带着旧路径进来。

    实现见 `continuation_service.refresh_fingerprinted_path`（单一真源）。
    """
    return cont_service.refresh_fingerprinted_path(params)


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


def _terminate_longfilm_process(job_id: str) -> bool:
    """终止本任务的 longfilm 外部进程（ffmpeg）。

    【为什么提取成公共办法】—— 2026-11-19 第 3 轮审查发现
    longfilm **不经过** ComfyUI，是 asyncio.create_subprocess_exec
    起的 **ffmpeg 子进程**（server.py:5665）。
    原来两个取消实现的处理机制不一致：
      * api_cancel（HTTP 面板）—— **有** longfilm 分支；
      * cancel_job_record（Agent 的 cancel_job 工具走到）—— **没有**。
    ⇒ 后果：Agent 走的那条路径**只**把 DB 状态改成 canceled，
    **ffmpeg 进程继续跑到结束**。
    实测危害：
      * ffmpeg 合成一段长片可能耗到几分钟；
      * 用户以为「已终止」，实际是资源还在流。

    【为什么不写两处】—— 两份实现必然漂移（这正是本缺陷的成因）。
    这里只留一个实现，两边都调。

    返回 True = 有一个在跑的进程被终止。
    """
    process = LONGFILM_PROCESSES.get(job_id)
    if process is None:
        return False
    try:
        if process.returncode is None:
            process.terminate()
    except ProcessLookupError:
        # 进程已终止（我们能终止过）—— 不是错误。
        pass
    return True


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
        mode = r["mode"]
        if mode == "longfilm":
            # 【2026-11-19 第 3 轮 C 项审查修 —— 补 longfilm 分支】
            # longfilm **不经过 ComfyUI**，是 asyncio.create_subprocess_exec
            # 起的 ffmpeg 子进程。原实现这里没有 longfilm 分支
            # => Agent 的 cancel_job 只把 DB 状态改成 canceled，
            # **ffmpeg 进程继续跑到结束**（用户以为已终止，实际还在烧 CPU）。
            # 与 api_cancel 对齐（共用 _terminate_longfilm_process）。
            terminated = _terminate_longfilm_process(job_id)
            with db() as conn:
                conn.execute(
                    "UPDATE jobs SET status='canceled', finished_at=? "
                    "WHERE id=? AND status IN ('queued','running')",
                    (time.time(), job_id))
                status = conn.execute(
                    "SELECT status FROM jobs WHERE id=?",
                    (job_id,)).fetchone()["status"]
            return {"job_id": job_id, "status": status, "ok": True,
                    "terminated_process": terminated,
                    "message": "已取消长片合成" if terminated
                               else "已取消长片合成（进程已结束）"}
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
             "t2i_h3image", "i2i_h3image",
             "t2a", "a2a")


#: 图像模式 -> (模型表, 该模式的默认模型 key)
#:
#: 【2026-11-16 第5轮代码审查 C-5 / C-6 新增】
#: 图像模式的采样参数归一需要「当前模型的 samplers / schedulers 白名单」。
#: 白名单**必须来自模型表本身**（面板下拉就是从它渲染的）——
#: 新造一份常量就会与模型表漂移。
#:
#: 【注意 H3-Image 的模型表在 h3studio.h3image 里，不在 workflows】
#: 所以这一族用字符串标记，由 _image_model_for_mode 走 h3img 分支取。
_IMAGE_MODE_TABLES = {
    "t2i": ("zimage", Z_IMAGE_MODELS, Z_IMAGE_DEFAULT_MODEL),
    "t2i_qwen": ("qwen", QWEN_IMAGE_MODELS, QWEN_IMAGE_DEFAULT_MODEL),
    "i2i_qwen": ("qwen", QWEN_IMAGE_MODELS, QWEN_IMAGE_DEFAULT_MODEL),
    "t2i_moss": ("moss", MOSS_IMAGE_MODELS, MOSS_IMAGE_DEFAULT_MODEL),
    "t2i_h3image": ("h3image", None, ""),
    "i2i_h3image": ("h3image", None, ""),
}


def _image_model_for_mode(mode: str, params: dict) -> dict | None:
    """取图像模式**当前选中模型**的定义（供采样参数归一取白名单）。

    返回 None 表示「这个模式不是图像模式」或「找不到模型表」——
    调用方据此跳过归一（不报错）。

    【为什么按 params["model"] 取而不是固定用默认模型】
    白名单是**按模型**的：Z-Image Turbo 的 schedulers 里没有 "exponential"
    （Turbo 8 步下崩图），但 Z-Image Base 有。用默认模型的白名单去校验
    用户实际选的模型 = 误拒合法值。

    【为什么找不到就返回 None 而不是抛错】
    归一层是**加固**，不是**准入**。模型表查不到时放行（让 builder 自己
    回落默认值），行为与改动前一致 —— 归一不该把一个能跑的任务变成跑不了。
    """
    spec = _IMAGE_MODE_TABLES.get(mode)
    if not spec:
        return None
    family, table, default_key = spec

    if family == "h3image":
        table = getattr(h3img, "H3_IMAGE_MODELS", None)
        default_key = getattr(h3img, "H3_IMAGE_DEFAULT_MODEL", "")
    if not isinstance(table, dict) or not table:
        return None

    model_key = str(params.get("model") or "").strip()
    model = table.get(model_key) if model_key else None
    if model is None:
        model = table.get(default_key) or next(iter(table.values()))
    return model if isinstance(model, dict) else None


def _reject_external_media_or_raise(saved_files: dict | None, mode: str) -> None:
    """
    拒绝「本地直传」的媒体文件 —— 生成任务的媒体必须来自素材库。

    【需求 —— 2026-11-16】首尾帧 / 参考生等功能的图片 / 音频 / 视频引用，
    与「图片编辑」保持一致：外部素材必须先进入素材库，
    界面只能从素材库挑选并引用，不允许功能直接引用外部文件路径。

    【为什么要包一层】asset_ref_guard 抛的是 AssetLibraryError（带 code），
    但 prepare_job_params 的对外契约是 JobValidationError —— api_create
    只捕那一个。直接抛 AssetLibraryError 会变成**未捕获异常** ->
    500 + 堆栈，而不是一条可读的中文原因。
    """
    try:
        asset_ref_guard.reject_external_media(saved_files, mode)
    except asset_ref_guard.AssetLibraryError as exc:
        raise JobValidationError(exc.message, code=exc.code) from exc


def _drop_stale_worker_media_params(params: dict, asset_refs: list | None) -> None:
    """清掉 params 里**与 asset_refs 重复**的媒体旧值（worker 回写的）。

    【背景 —— 2026-11-20】worker 把素材库文件上传到 ComfyUI 后，会把
    ComfyUI 侧的文件名**写回 params**（server._run 的 flf / r2v 分支），
    并把整份 params 回写进 jobs 表。Agent 的 retry_job 随后把这份
    params **整份拷贝**过来重新建任务 —— 于是新任务里同时有：
      * asset_refs —— 素材库引用（**权威**，能查到真实文件）；
      * params["first_frame"] = "h3studio/<name>" —— worker 的旧产物
        （**过期残留**，UPLOAD_DIR 下并不存在这个文件）。

    【原来错在哪（实测复现）】worker 的 flf 分支**先**执行
    `lp = UPLOAD_DIR / up`（server._run:1841），文件不存在就直接
    `RuntimeError("找不到上传文件")` —— 走不到后面的
    _apply_asset_refs。所以**媒体任务的重试今天本来就是坏的**
    （2026-11-20 线上实测：重放一条已完成 flf 任务的 params ->
    HTTP 200 建任务 -> failed「找不到上传文件:
    h3studio/685e6b9292ca0ec9__e2e-sample-4079691-93314.png」）。

    【这里做什么】当 asset_refs 已经声明了某个槽位时，params 里
    对应的旧值就是**过期残留** —— 删掉它，worker 就会走
    _apply_asset_refs 从素材库重新取（那条路本来就是对的）。
    顺带把「重试」从坏修成好。

    【⚠️ 只删「形状上不可能是外部路径」的值 —— 这一条是安全底线】
    删的判据是 asset_ref_guard._looks_like_comfy_name：**没有 `/`
    开头、没有 `..`、没有盘符、只有一层子目录**。于是：
      * worker 的旧值 `h3studio/<name>` -> 被删（重试能跑）；
      * `/etc/hostname` -> **不被删** -> 留给素材库闸门**明确报错**。
    如果这里无差别地删，攻击者只要同时带上覆盖同一槽位的
    asset_refs，就让自己的外部路径被**静默丢弃** —— 那就等于
    把「明确拒绝」降级成「悄悄忽略」，与需求相悖。

    【为什么只删「asset_refs 覆盖到的槽位」】
    * 删多了会丢掉**没有**对应素材引用的媒体 —— 那是用户真的
      填了媒体但没走素材库（= 要报错的情况，不能静默丢）；
    * 删少了就等于没修（worker 还是会先撞上那个残留值）。

    【为什么不是「删掉所有媒体键」】params["ref_images"] 在 i2i
    模式下是**元数据**形态（[{asset_id, description}]）—— 那是
    图片编辑的正规通道，不能碰。
    """
    if not isinstance(params, dict) or not asset_refs:
        return
    # asset_refs 的 slot -> params 键（与 _ASSET_SLOT_PARAM 同一张表）
    covered = set()
    for item in asset_refs:
        if not isinstance(item, dict):
            continue
        slot = str(item.get("slot") or "").strip()
        key = _ASSET_SLOT_PARAM.get(slot)
        if key:
            covered.add(key)
    if not covered:
        return
    for key in covered:
        value = params.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, list) and any(isinstance(v, dict) for v in value):
            # ref_images 的元数据形态（dict）不动 —— 那是图片编辑的正规通道
            continue
        items = value if isinstance(value, list) else [value]
        # ⚠️ 只删「形状上不可能是外部路径」的值（见上面的安全底线）
        if not all(isinstance(v, str)
                   and asset_ref_guard._looks_like_comfy_name(v)
                   for v in items):
            continue
        params.pop(key, None)


def _image_family_for_mode(mode: str) -> str:
    """取图像模式所属的**模型族** key（决定采样参数的合法量程）。

    【为什么需要】
    各族的官方参数域差别很大（MOSS steps 上限 100 / Qwen shift 步进 0.01 /
    H3-Image steps 上限 12）。用一套扁平的全局区间去卡所有族 =
    「用户拖得动、后端不认」—— 2026-11-16 用户报的 MOSS-Image
    steps=100 被后端静默夹回 50 就是这么来的。

    【真源】
    ``gen_normalize.IMAGE_PARAM_RANGES`` 的键名与
    ``_IMAGE_MODE_TABLES`` 的族名**同名**（zimage / qwen / moss / h3image），
    前端滑杆量程也由同一份下发（/api/image-options 的 param_ranges）。

    【未知模式】
    回落 ``zimage``（历史行为：旧的全局区间就是 Z-Image 那套）。
    """
    spec = _IMAGE_MODE_TABLES.get(mode)
    if not spec:
        return gen_normalize.DEFAULT_PARAM_RANGES_FAMILY
    family = spec[0]
    if family == "h3image":
        return "h3image"
    return family if family in gen_normalize.IMAGE_PARAM_RANGES else \
        gen_normalize.DEFAULT_PARAM_RANGES_FAMILY


#: 面板提交路径**不套**的默认参数字段（采样类）。
#:
#: 【为什么】``normalize_t2v_params`` 里 flf / r2v 有一条 2026-11-12
#: 定下的契约：**客户端没传 sampler / scheduler 时服务端不写回** ——
#: 因为工作流会按 ``lora`` 自己选档（有加速 LoRA -> euler/beta；
#: 无 -> res_multistep/simple）。服务端写回就等于**改老任务的画质**。
#:
#: 【实测守卫】``tests/test_video_sampling_params.py::
#:   test_B2_legacy_client_without_sampler_keeps_workflow_default``
#: 断言 ``"sampler" not in params``。
#:
#: 【Agent 路径不受影响】Agent 走 ``agent.py`` 自己的
#: ``merge_into_params``（带 ``overridable=builtin_filled``），
#: 本来就该带采样参数 —— 模型是在**主动建任务**，不是「老客户端重放」。
_SAMPLING_ONLY_FIELDS = frozenset({
    "sampler", "scheduler", "steps",
    "shift_video", "shift_audio",
})


def apply_gen_defaults_and_size(mode: str, params: dict) -> dict:
    """
    把「管理页默认参数」套进 params，并解析出最终生效的宽高。

    **就地修改** params，返回同一个对象.


    【为什么抽成独立函数】它必须在**两个时机**都跑：
      1. ``api_create`` 里 ``normalize_t2v_params`` **之前** ——
         归一层第一件事就是校验 width/height，没有宽高直接 400，
         所以尺寸必须在归一前算出来；
      2. ``prepare_job_params`` 里 —— 那是**所有**建任务路径的公共点
         （面板提交 / Agent create_job / retry_job），
         放这里三条链路同时生效，不会各写一份必然漂移。

    两处跑同一份实现 = 结果一致（幂等：已经是合法宽高就原样通过）。

    【三层优先级】用户/模型**显式传的值** > 管理页默认值 >
    工作流内置默认值。

    【t2v 例外】t2v 的默认参数走服务端专用路径
    （``_merge_t2v_defaults``，它还要把 model_key / attention /
    accel_lora 转成工作流专用的形状），不能在这里重复套。

    返回 dict：``{"defaults_applied": [...], "size_reasons": [...]}``
    """
    out: dict = {"defaults_applied": [], "size_reasons": []}
    if mode not in JOB_MODES:
        return out

    # ---- 1) 管理页默认参数 ----
    if mode != "t2v":
        try:
            conn = db()
            try:
                _settings = agent_settings.load(conn)
            finally:
                conn.close()
            _gd = (_settings.get("gen_defaults") or {}).get(mode)
            if isinstance(_gd, dict) and _gd:
                # 【2026-11-20 —— 采样类字段**不套**（既有契约）】
                #
                # 【为什么】``server.py`` 里 flf / r2v 的归一有一条
                # 2026-11-12 定下的契约：**客户端没传 sampler / scheduler 时
                # 服务端不写回** —— 因为工作流会按 ``lora`` 自己选档
                # （有加速 LoRA -> euler/beta；无 -> res_multistep/simple）。
                # 服务端写回就等于**改老任务的画质**。
                #
                # 【实测回归】``tests/test_video_sampling_params.py::
                #   test_B2_legacy_client_without_sampler_keeps_workflow_default``
                # 断言 ``"sampler" not in params``。套了默认参数就会挂。
                #
                # 【为什么 Agent 路径不受影响】Agent 路径走
                # ``agent.py`` 自己的 ``merge_into_params``（带
                # ``overridable=builtin_filled``），本来就该带采样参数 ——
                # 模型是在**主动建任务**，不是「老客户端重放」。
                #
                # 所以：**面板提交路径不套采样类字段**，其余（比例 /
                # MP 预算 / 百分比 / 时长 / 底模 / LoRA 档 / 精度 …）照套。
                _gd = {k: v for k, v in _gd.items()
                       if k not in _SAMPLING_ONLY_FIELDS}
                # 【显式值 = 本次请求里带了非空的字段】
                _explicit = {k for k, v in params.items()
                             if v not in (None, "")}
                out["defaults_applied"] = gen_defaults.merge_into_params(
                    mode, params, {mode: _gd}, explicit=_explicit,
                    overridable=(), skip_sampling=True)
        except Exception:
            # 套默认参数失败**不能阻断建任务** ——
            # 少几个默认值只是「用了工作流内置值」，总比任务建不出来好。
            pass

    # ---- 2) 尺寸解析（MP 预算 × 百分比 -> 宽高）----
    if mode in ("t2v", "flf", "r2v"):
        _spec_w, _spec_h, _spec_notes = size_spec.plan_size(
            mode,
            aspect=str(params.get("aspect") or "16:9"),
            megapixels=params.get("megapixels"),
            percent=params.get("resolution_percent"),
            resolution=params.get("resolution"),
            width=params.get("width"),
            height=params.get("height"),
        )
        if _spec_w > 0 and _spec_h > 0:
            params["width"] = _spec_w
            params["height"] = _spec_h
            if _spec_notes:
                params["size_adjust_reasons"] = _spec_notes
                out["size_reasons"] = _spec_notes
    return out


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

    # 【2026-11-20 新增 —— 面板提交路径也套「管理页默认参数」+ 解析尺寸】
    #
    # 【原来错在哪（实测复现）】「内置 Agent 管理」里配的默认参数
    # （gen_defaults）**只有 Agent 路径会套**（``agent.py`` 里的
    # ``gen_defaults.merge_into_params``）。
    # 面板提交（POST /api/jobs）**完全不套** —— 也就是说用户在管理页
    # 配的默认时长 / 默认 MP 预算 / 默认采样参数，对面板点「生成」
    # **没有任何作用**。
    #
    # 这正是用户要求的「保存的默认参数必须对『面板提交』与
    # 『内置 Agent 自建 job』两条路径都真实生效」里**缺失的那一半**。
    #
    # 【为什么放在 prepare_job_params】它是**所有**建任务路径的公共点
    # （面板提交 / Agent create_job / retry_job）—— 放这里，三条链路
    # 同时生效，不会各写一份必然漂移。
    #
    # 【与 api_create 里的同一处调用是幂等的】两处跑同一份实现
    # （``apply_gen_defaults_and_size``）—— 已经是合法宽高就原样通过。
    apply_gen_defaults_and_size(mode, params)

    # 【2026-11-16 新增】素材引用必须是**素材库里已入库**的素材。
    #
    # 【为什么必须查库，而不是只看 id 格式】素材 id 形如
    # `asset_<12 位 hex>`。只查前缀的话，一个**根本不存在**的 id 也能过 ——
    # 错误会推迟到 worker 里「素材不存在或已删除」，用户看到的又是兜底文案。
    # 查库才能给出「该媒体尚未入库素材库，请先上传到素材库后再引用」
    # 这种能照着改的中文原因。
    #
    # 【线上探针实测】这一条之前**漏接**了：伪造的
    # asset_000000000000 提交后返回 200 并建了任务（worker 才去报「素材不存在」）。
    try:
        asset_ref_guard.check_asset_refs(asset_refs)
    except asset_ref_guard.AssetLibraryError as exc:
        raise JobValidationError(exc.message, code=exc.code) from exc

    # 【2026-11-20 新增 —— 安全缺陷收口】params JSON 通道的媒体引用
    # 必须同样过素材库闸门。
    #
    # 【原来错在哪（实测复现）】上面的 check_asset_refs 只管
    # **asset_refs** 通道，reject_external_media 只管 **multipart
    # 直传**通道 —— **params 这条通道没人管**。于是：
    #
    #     POST /api/jobs -F mode=flf -F params={"first_frame":"/etc/hostname"}
    #
    # 返回 200 并建了任务；worker 随后把 params["first_frame"] 当成
    # 「uploads 目录下的相对文件名」拼路径（`lp = UPLOAD_DIR / up`，
    # 绝对路径会被 Path.__truediv__ **丢弃左侧**），把那个文件
    # **复制进 ComfyUI 的 input 目录**。ComfyUI 的 /view 端点无鉴权
    # 且与 8190 一样监听 0.0.0.0 ⇒ 构成完整的「任意本地文件读取」链。
    #
    # 【为什么放在这里】prepare_job_params 是**所有**建任务路径的
    # 唯一公共点（面板提交 / 内置 Agent create_job / retry_job）——
    # 在这里拦一次，三条链路同时生效，不会各写一份必然漂移。
    # 放在 check_asset_refs 之后、seed 校验之前：素材引用与种子
    # 都是「参数合法性」，媒体路径的错比种子的错更该先说。
    #
    # 【为什么先清掉「worker 回写的旧值」再校验】
    # worker 上传素材后会把 ComfyUI 侧的文件名**写回 params**，
    # 并把整份 params 回写进 jobs 表；Agent 的 retry_job 会把这份
    # params **整份拷贝**过来重新建任务。那种值长得像
    # `h3studio/<name>` —— 它是 worker 的旧产物，不是外部输入。
    #
    # 但它**同样不能作为素材来源**：`UPLOAD_DIR / "h3studio/x.png"`
    # 指向的是 uploads 目录下一个**并不存在**的文件（实测：线上
    # 53 个这样的值**没有一个**能解析到真实文件），worker 走到那里
    # 只会报「找不到上传文件」—— 也就是**媒体任务的重试今天本来
    # 就是坏的**（2026-11-20 实测复现）。
    #
    # 所以：asset_refs 才是媒体来源的**权威**通道；当它带了某个
    # 槽位时，params 里对应的旧值就是**过期残留**，应当清掉，
    # 让 worker 走 _apply_asset_refs 从素材库重新取。
    # —— 这一步同时把「重试」从「坏」修成「好」，而不是把闸门
    # 变成误杀器。
    #
    # 【⚠️ 安全底线】_drop_stale_worker_media_params **只删「形状上
    # 不可能是外部路径」的值**（无 `/` 开头、无 `..`、无盘符、只有
    # 一层子目录）。`/etc/hostname` 不被删 —— 它留给下面的闸门
    # **明确报错**。否则攻击者只要同时带上覆盖同一槽位的
    # asset_refs，就让自己的外部路径被静默丢弃 —— 那等于把
    # 「明确拒绝」降级成「悄悄忽略」，与需求相悖。
    _drop_stale_worker_media_params(params, asset_refs)
    try:
        asset_ref_guard.check_params_media_references(params)
    except asset_ref_guard.AssetLibraryError as exc:
        raise JobValidationError(exc.message, code=exc.code) from exc

    # 【2026-11-16 新增】随机种子：只允许正整数。
    #
    # 【原来错在哪】种子**一路不校验**地流到 worker：
    #   workflows.py:339  seed = params.get("seed") or random.randint(0, 2**48)
    #   workflows.py:380  p = {..., "_seed": int(seed), ...}   <- ValueError
    # worker 捕获后只留一句「生成失败（服务端未能给出具体原因）」——
    # 用户既不知道自己填错了什么，也不知道该怎么改。
    #
    # 【为什么在这里校验】prepare_job_params 是**所有**建任务路径的
    # 唯一公共点（面板提交 / 内置 Agent create_job / retry_job）——
    # 在这里拦一次，三条链路同时生效，不会各写一份必然漂移。
    try:
        seed_guard.normalize_seed_params(params)
    except seed_guard.SeedValidationError as exc:
        raise JobValidationError(exc.message, code=exc.code) from exc

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
        # 【2026-11-15 修 —— 分辨率/比例语义失真】
        #
        # 【原来错在哪】normalize_t2v_params **只对 t2v 跑**，
        # 于是 flf / r2v 的 params 里可能压根没有 width/height；
        # 而 h3studio/h3_video_modes.py 的 build_flf / build_r2v
        # 只读 params["width"] / params["height"]，**缺失时硬回退
        # 1344×768**。
        #
        # 结果：面板显示「544p · 16:9」，工作流实际跑 1344×768 ——
        # 1344×768 不是 544p 的任何合法组合（544p = 0.4MP -> 736×416）。
        # 用户看到的是「说谎的分辨率」。
        #
        # 【修法】flf / r2v 也走**同一套**宽高归一
        # （h3studio/h3_video_size.py，与 t2v 同一份公式与上限策略），
        # 并把真实生效的宽高**写回 params** —— 工作流读到的就是面板选的。
        h3_video_size.apply_size(params, mode)
        # 【2026-11-12 修】默认值必须与工作流里的硬编码档位**完全一致，
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
        # 【2026-11-16 新增】首尾帧 / 参考视频 / 参考音乐**必须来自素材库**。
        #
        # 【原来错在哪】flf 接受 file_first / file_last / file_refvid /
        # file_refaud 四个 multipart 上传通道 —— 即「直接引用外部文件路径」。
        # 需求（2026-11-16）要求与「图片编辑」一致：外部素材必须先入库
        # 素材库，界面只能从素材库挑选并引用。
        #
        # 【为什么在这里拦而不是只在前端去掉入口】前端挡不住直接调 API
        # 与内置 Agent 建任务；后端是唯一强制点。
        _reject_external_media_or_raise(saved_files, mode)
        if saved_files.get("file_first"):
            params["first_frame"] = saved_files["file_first"][0]
        if saved_files.get("file_last"):
            params["last_frame"] = saved_files["file_last"][0]
        # 【2026-11-15 补齐 —— 缺口 1 的 flf 半边】
        # 参考视频 / 参考音乐的**本地直传**通道（音轨三选一的素材来源）。
        #
        # 【为什么 flf 也要】后端 build_flf 本来就支持 ref_audios /
        # ref_video_audios，但面板之前只渲染首帧 / 尾帧两个槽位，
        # 于是这两路素材**没有任何入口** —— 能力存在但用不到。
        # 现在面板补了 refvid0..2 / refaud0..2 槽位，
        # 服务端这里也必须接住对应的上传文件，
        # 否则用户选的文件会被静默丢掉（本地路径通、素材库路径不通）。
        flf_ref_videos = saved_files.get("file_refvid", [])
        flf_ref_audios = saved_files.get("file_refaud", [])
        if flf_ref_videos:
            params["ref_videos"] = flf_ref_videos[:h3modes.MAX_REF_VIDEOS]
        if flf_ref_audios:
            params["ref_audios"] = flf_ref_audios[:h3modes.MAX_REF_AUDIOS]
        # 【2026-11-14】首帧与尾帧**都可以单独给**（原来只认首帧）。
        # 只要给了其中一张就能生成 —— H3 的 I2VA / L2VA 任务类型
        # 就是「只有首帧」/「只有尾帧」。
        _flf_slots = {r.get("slot") for r in (asset_refs or [])
                      if isinstance(r, dict)}
        if not params.get("first_frame") and "first_frame" not in _flf_slots:
            if not params.get("last_frame") and "last_frame" not in _flf_slots:
                raise JobValidationError(
                    "首尾帧模式需要至少提供首帧或尾帧图片。"
                    "请从素材库选择首帧 / 尾帧，或直接上传图片。",
                    code="frame_required")
    elif mode == "r2v":
        # 【2026-11-16 新增】参考图 / 参考视频 / 参考音乐**必须来自素材库**
        # （与「图片编辑」同一套约束）。理由同 flf 分支。
        _reject_external_media_or_raise(saved_files, mode)
        refs = saved_files.get("file_ref", [])
        # 【2026-11-15】参考视频 / 参考音乐的本地直传通道。
        # （缺口 1：面板补了槽位 -> 服务端必须有对应的接收与上传路径。）
        ref_videos = saved_files.get("file_refvid", [])
        ref_audios = saved_files.get("file_refaud", [])
        if ref_videos:
            params["ref_videos"] = ref_videos[:h3modes.MAX_REF_VIDEOS]
        if ref_audios:
            params["ref_audios"] = ref_audios[:h3modes.MAX_REF_AUDIOS]
        if not refs and not ref_videos and not ref_audios:
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
            # 【2026-11-14】参考图上限从 3 张提升到 **9 张**
            # —— H3 参考生成节点的 ref_images 槽位上限就是 9
            #（实测 object_info：COMFY_AUTOGROW_V3 min=0 max=9）。
            # 原来卡 3 张是历史遗留，把能力人为砍掉了。
            #
            # 【2026-11-15】这里**不再静默截断**：
            # 原来 `refs[:9]` 会把第 10 张悄悄丢掉，用户以为 10 张都进去了。
            # 超上限直接报错（前端也做了同样的校验，两边一致）。
            if len(refs) > h3modes.MAX_REF_IMAGES:
                raise JobValidationError(
                    f"参考图最多 {h3modes.MAX_REF_IMAGES} 张，当前 {len(refs)} 张。",
                    code="ref_too_many")
            if refs:
                params["ref_images"] = refs

        # 【2026-11-16 新增】**素材库引用**的各组上限也必须校验 ——
        # 否则「提交被静默截断」那个老缺陷会从**另一条路**回来：
        # 用户选 10 张参考图（面板有 9 个槽位，但 state 里可能残留更多），
        # 服务端不报错，worker 上传时按节点上限丢掉第 10 张 ——
        # 用户以为 10 张都进去了。
        #
        # 【为什么放在这里】这是**唯一**能同时看到「素材库引用」与
        # 「节点上限」的地方（prepare_job_params 是三条建任务路径的公共点）。
        _ref_counts = {}
        for _r in (asset_refs or []):
            if isinstance(_r, dict):
                _slot = str(_r.get("slot") or "")
                _ref_counts[_slot] = _ref_counts.get(_slot, 0) + 1
        _ref_limits = {
            "ref_image": h3modes.MAX_REF_IMAGES,
            "ref_video": h3modes.MAX_REF_VIDEOS,
            "ref_audio": h3modes.MAX_REF_AUDIOS,
            "ref_video_audio": h3modes.MAX_REF_VIDEOS,
        }
        _ref_labels = {
            "ref_image": "参考图",
            "ref_video": "参考视频",
            "ref_audio": "参考音乐",
            "ref_video_audio": "参考视频背景音",
        }
        for _slot, _limit in _ref_limits.items():
            _count = _ref_counts.get(_slot, 0)
            if _count > _limit:
                raise JobValidationError(
                    f"{_ref_labels.get(_slot, _slot)}最多 {_limit} 个，"
                    f"当前 {_count} 个。",
                    code="ref_too_many")

    # ---- 音轨三选一（首尾帧 / 参考生视频）----
    #
    # 【2026-11-14】B2 需求：音轨三选一
    #   (a) 使用生成的音乐
    #   (b) 使用参考音乐作为成片音轨
    #   (c) 使用参考视频的背景音乐作为成片音轨
    # 没有参考音乐 / 参考视频时 (b)/(c) **不出现** ——
    # 可见性判据与归一都在 h3studio/h3_audio_track.py（唯一真源）。
    if mode in ("flf", "r2v"):
        # 【2026-11-14 修 —— B2「音轨三选一」选了不生效】
        #
        # 【原来错在哪】这里的可见性判据只看 params["ref_audios"] /
        # params["ref_video_audios"] —— 那两个键只有在**已经上传**时才有值。
        # 而参考素材通常来自素材库：前端提交的是 asset_refs（slot=ref_audio），
        # 真正的 ref_audios 要到 worker 里 _apply_asset_refs 跑完才有。
        # 于是提交时算出「没有参考音乐」-> 用户选的 "reference" 被归一成
        # "generated" -> 音轨来源被永久改掉 -> 成片音轨不是用户选的那条。
        #
        # 【修法】可见性判据要**同时**看「已上传的」和「素材库引用里声明的」：
        # asset_refs 里出现 slot=ref_audio / bgm / voice 表示**会有**参考音乐；
        # 出现 slot=ref_video_audio 表示**会有**参考视频背景音。
        # 真正的最终判定仍在 worker 里（_pick_audio_track 用真实 ref_audios
        # 再归一一次），这里只是不让提交阶段把用户的选择提前改掉。
        _has_ref_audio = bool(params.get("ref_audios"))
        _has_ref_video_audio = bool(params.get("ref_video_audios"))
        _slots = {str(r.get("slot") or "").strip()
                  for r in (asset_refs or []) if isinstance(r, dict)}
        if _slots & {"ref_audio", "bgm", "voice"}:
            _has_ref_audio = True
        if "ref_video_audio" in _slots:
            _has_ref_video_audio = True
        # 【2026-11-15】本地直传的参考视频 / 参考音乐也算「有素材」。
        #
        # 【为什么必须加这一段】参考视频槽位（slot=ref_video）此前
        # **不会**产生 ref_video_audios —— 那个键只在用户显式提供
        # 「视频的背景音文件」时才有值。于是：
        #   * 用户只选了参考视频、没单独选背景音 ->
        #     (c)「用参考视频背景音」在提交阶段被判为「没有素材」-> 被归一成
        #     「生成的音乐」-> 用户选了却被改掉（正是本次要修的缺陷）。
        #   * 服务端随后会**从参考视频里抽出音轨**（见 worker 的
        #     _extract_video_soundtrack），所以 (c) 其实是**有**素材的。
        #
        # 现在：有参考视频 -> (c) 可选（worker 抽不出音轨时再归回生成）。
        if _slots & {"ref_video", "source_video"}:
            _has_ref_video_audio = True
        if saved_files.get("file_refvid"):
            _has_ref_video_audio = True
        if saved_files.get("file_refaud"):
            _has_ref_audio = True
        params["audio_track_source"] = h3audio.normalize_track_source(
            params.get("audio_track_source"),
            has_reference_audio=_has_ref_audio,
            has_reference_video_audio=_has_ref_video_audio)
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

        # 【2026-11-16 新增】参考图必须**真的在素材库里**（不只是「长得像 id」）。
        #
        # 【为什么这里要单独查 —— 这条路与 asset_refs 是两条通道】
        #   * flf / r2v / a2a 的媒体走 asset_refs（{asset_id, slot, ordinal}）
        #     -> 上面的 check_asset_refs 已经查过库了；
        #   * i2i_qwen / i2i_h3image 的参考图走 **params["ref_images"]**
        #     （带描述 / 标签的元数据）-> **完全没被查过库**。
        #
        # 【原来错在哪】ref_images.normalize_refs / validate_refs 都是
        # **纯 dict 处理**（h3studio/ref_images.py 里没有任何 DB 访问），
        # `missing` 标记还是**前端传什么就信什么**。于是：
        #   * 库里不存在的 asset_id -> 通过校验、任务建成，
        #     直到 worker 里 _upload_ref_images 才抛 RuntimeError；
        #   * 已删除（软删）的素材 -> 同上；
        #   * 类型不符（拿视频当参考图）-> 一路到 worker 才炸。
        # 用户看到的是「生成失败（服务端未能给出具体原因）」那类兜底文案。
        #
        # 查库后才能给出「该媒体尚未入库素材库，请先上传到素材库后再引用」
        # 这种能照着改的中文原因 —— 与 flf / r2v 完全同一套口径。
        try:
            asset_ref_guard.check_asset_refs(
                [{"asset_id": r["asset_id"], "slot": "ref_image"}
                 for r in refs])
        except asset_ref_guard.AssetLibraryError as exc:
            raise JobValidationError(exc.message, code=exc.code) from exc

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

    elif mode in ("t2i_h3image", "i2i_h3image"):
        # MiniMax-H3-Image 文生图 / 图片编辑
        #
        # 【LoRA】官方暂未发布 H3-Image 的 LoRA —— 本轮**不支持**，
        # 传了也一律丢弃（避免脏值进库，卡片上显示出一段乱码）。
        params.pop("loras", None)
        # 负面提示词：guidance 固定 1 -> 没有 CFG pass -> 数学上无效。
        # 静默丢弃并回一条明确说明，而不是假装收下了。
        if params.get("negative_prompt"):
            params["negative_prompt_ignored"] = (
                "H3-Image 的 guidance 固定为 1（无 CFG pass），"
                "负面提示词不生效 —— 请把「不要什么」写进正面提示词。")
            params.pop("negative_prompt", None)

        if mode == "i2i_h3image":
            # 参考图**强制来自素材库**（与 i2i_qwen 同一套约束）。
            uploaded = saved_files.get("file_ref", [])
            if uploaded:
                raise JobValidationError(
                    "图片编辑的参考图必须来自素材库，不能直接上传本地图片。"
                    "请先在「素材库」页上传并入库，再从素材库选择参考图。",
                    code="ref_must_come_from_library")
            ref_specs = params.get("ref_images") or []
            if not isinstance(ref_specs, list):
                ref_specs = []
            refs = ref_imgs.normalize_refs(ref_specs)
            if len(refs) < h3img.H3_IMAGE_MIN_REF_IMAGES:
                raise JobValidationError(
                    f"H3-Image 图片编辑至少需要 {h3img.H3_IMAGE_MIN_REF_IMAGES} 张参考图，"
                    f"当前 {len(refs)} 张（参考图必须来自素材库）",
                    code="ref_required")
            if len(refs) > h3img.H3_IMAGE_MAX_REF_IMAGES:
                raise JobValidationError(
                    f"H3-Image 参考图最多 {h3img.H3_IMAGE_MAX_REF_IMAGES} 张，"
                    f"当前 {len(refs)} 张（官方能力上限 16 张）",
                    code="ref_too_many")
            # 【2026-11-16 新增】同 i2i_qwen：参考图必须**真的在素材库里**。
            # （ref_images 这条通道与 asset_refs 是两条路，asset_refs 查过库，
            #   这条没有 —— 详见 i2i_qwen 分支里同一段说明。）
            try:
                asset_ref_guard.check_asset_refs(
                    [{"asset_id": r["asset_id"], "slot": "ref_image"}
                     for r in refs])
            except asset_ref_guard.AssetLibraryError as exc:
                raise JobValidationError(exc.message, code=exc.code) from exc
            prompt_text = (params.get("prompt") or "").strip()
            # 【占位标记语义与 Qwen 不同 —— 不能复用 ref_imgs.validate_refs】
            #   Qwen-Image 2.1 的官方 PE-I2I 模板用 `<image1>..<imageN>`；
            #   H3-Image 的官方文档用 `<Picture 1>..<Picture N>`
            #   （"the prompt addresses each one by its label"）。
            #   拿 Qwen 那套校验去查 H3-Image，会把「用户写了 <Picture 1>」
            #   的合法提示词判成「缺少 <image1> 占位标记」-> 误拒。
            check = h3img.validate_h3_image_refs(refs, prompt=prompt_text)
            if not check["ok"]:
                raise JobValidationError(
                    "参考图校验未通过：" + "；".join(check["errors"]),
                    code="ref_invalid", extra={"errors": check["errors"]})
            # H3-Image 的官方占位标记是 <Picture N>（不是 Qwen 的 <imageN>）
            # -> 不注入 Qwen 那套占位块，改为提示用户自己写 <Picture 1>。
            # 参考图数量与顺序由服务端保证（ref_images 列表原序即插槽顺序）。
            params["ref_images"] = refs
            # 【2026-11-15 修② · 素材描述必须真正进到提示词里】
            # worker 跑 i2i_h3image 时会用**上传后的文件名列表**覆盖
            # params["ref_images"]（见 _run 的 i2i_h3image 分支），
            # description 随之丢失 —— 而 build_h3_image_workflow 只把
            # params["prompt"] 送进 MiniMaxH3ImageConditioning。
            # 结果：模型**从来没看到**素材描述（用户 2026-11-15 报的
            # 「图片编辑素材描述只传第一行」在 H3-Image 这条路上的**更严重**形态：
            # 描述根本没进模型）。
            #
            # 现在在**覆盖发生之前**把描述按 <Picture N> 写进提示词。
            params["prompt"] = h3img.inject_h3_image_reference_block(
                prompt_text, refs)
            params["picture_hint"] = (
                "提示词里用 " + h3img.H3_IMAGE_PLACEHOLDER.format(n=1) +
                " 这样的标记引用参考图（官方语法）")

    # 【2026-11-16 第5轮代码审查 C-5 / C-6 修 —— 图像模式的采样参数归一】
    #
    # 【原来错在哪】
    # 图像模式（t2i / t2i_qwen / i2i_qwen / t2i_moss / t2i_h3image）的
    # steps / cfg / shift / sampler / scheduler **完全不归一**：
    #   * workflows.build_t2i / build_qwen_t2i / build_moss_t2i 都是
    #     ``params.get("sampler") or model["sampler"]`` 这种写法 ——
    #     它**信任** params 里的值，直接塞进 KSamplerSelect 的
    #     sampler_name / scheduler 槽位；
    #   * 绕过面板直接调 API（POST /api/jobs）传
    #     {"sampler": "不存在的采样器"} -> 一路送到 ComfyUI ->
    #     用户收到一句英文的 "value not in list"，
    #     既不知道是哪个字段、也不知道合法值是什么；
    #   * steps=9999 / cfg=-1 / shift=0 也是同理 ——
    #     要么被 ComfyUI 拒绝，要么静默跑出一张废图。
    #
    # 【为什么放在这里而不是 builder 里】
    # 三个图像 builder 各自读各自的字段 —— 在 builder 里改就是三处各改一遍
    # （又是一次必然漂移）。归一层是**三条链路的唯一公共点**
    # （面板提交 / Agent 建任务 / 老任务重放都过这里），
    # 放这里 = 改一次，三条链路同时生效。
    #
    # 【白名单从哪来 —— 不新造常量】
    # 采样器 / 调度器的合法集合来自**各模型表自己**的 samplers / schedulers
    # 字段（Z_IMAGE_MODELS / QWEN_IMAGE_MODELS / MOSS_IMAGE_MODELS /
    # H3_IMAGE_MODELS）—— 它们本来就是面板下拉的数据源。
    # 新造一份白名单就会与模型表漂移（模型表改了、白名单没改 = 又一次静默失效）。
    #
    # 【为什么回落而不是报错 —— 老任务重放语义安全】
    # 历史任务 params 里可能躺着当年合法的采样器名。若这里报错，
    # 那些老任务会**永久无法复用**。所以：非法值回落到该模型的官方推荐值，
    # 并把「改了什么」记进 params["normalize_notes"]（任务详情如实展示）。
    # 合法值**一个字节都不动**（契约 R1）。
    _image_model = _image_model_for_mode(mode, params)
    if _image_model is not None:
        _notes: list[str] = []
        # 【2026-11-16 修 —— 按模型族取量程】
        # 旧实现不传 family -> 归一层用一套扁平的全局区间
        # （steps<=60 / cfg>=1 / shift 1~7）去卡所有族。
        # MOSS-Image 面板量程是 steps 1~100 / cfg 0~10 / shift 0~10，
        # 于是 steps=100 被判越界 -> 静默回落 50（本次用户报的 BUG）。
        # 现在把「族」明确传进去 —— 区间真源在 gen_normalize.IMAGE_PARAM_RANGES，
        # 与前端滑杆共用同一份（前端量程由 /api/image-options 下发）。
        gen_normalize.normalize_image_sampling(
            params, _image_model, _notes, _image_family_for_mode(mode))
        if _notes:
            existing = params.get("normalize_notes")
            merged = list(existing) if isinstance(existing, list) else []
            for note in _notes:
                if note not in merged:
                    merged.append(note)
            params["normalize_notes"] = merged

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
    # 【2026-11-21 修 —— 缺 Content-Type 校验导致真 500】
    #
    # 【原来错在哪（线上实测复现）】下面直接 ``await request.multipart()``，
    # 而 aiohttp 的 MultipartReader 构造函数要求请求头里有 Content-Type：
    #   * 完全没有 Content-Type  -> ``KeyError: 'Content-Type'``
    #   * Content-Type 不是 multipart/* -> ``AssertionError: multipart/* content type expected``
    #   * 是 multipart/form-data 但没有 boundary -> ``ValueError: boundary missed ...``
    # 三种情况都会**抛异常而不是返回 4xx** —— aiohttp 的默认错误处理把它
    # 变成 **HTTP 500「Server got itself in trouble」**。
    #
    # 【为什么要修】500 意味着「服务端崩了」，会误导排查方向（去查
    # ComfyUI / GPU / 队列），而真实原因只是调用方发错了请求头 —— 这是
    # **客户端可自己纠正**的问题，应该回 415 / 400 并把该带什么头说清楚。
    # 线上 24 小时日志里的 10 条异常（7 条 AssertionError + 2 条 boundary
    # + 1 条 KeyError）**全部**来自这一处。
    #
    # 【为什么不是 400 而是 415】415 Unsupported Media Type 的语义就是
    # 「请求体格式不对」。同时**不能**把 multipart 之外的格式（比如
    # application/json）悄悄转成能处理 —— 提交路径要的就是文件流，
    # 静默降级会让「文件没带上」这类问题更晚暴露、更难查。
    # 【为什么用 getattr 而不是 request.headers.get】
    # aiohttp 的 Request 一定有 headers，但**测试替身**（FakeRequest）可能没有 ——
    # 实测 tests/test_qwen_ref_assets.py 的 FakeRequest 只实现了 multipart()。
    # 直接 request.headers.get 会抛 AttributeError，把「校验请求头」变成
    # 「制造新崩溃」。用 getattr(..., None) 兜底：缺 headers 当作「没带 Content-Type」
    # -> 走 415 分支，语义正确且不炸。
    ctype = (getattr(request, "headers", None) or {}).get("Content-Type") or ""
    ctype = ctype.strip()
    if not ctype:
        return web.json_response(
            {"error": "缺少 Content-Type 请求头：本接口只接受 "
                      "multipart/form-data（提交生成任务要带文件流）",
             "code": "content_type_required"},
            status=415)
    main_ctype = ctype.split(";", 1)[0].strip().lower()
    if not main_ctype.startswith("multipart/"):
        return web.json_response(
            {"error": f"不支持的 Content-Type：{main_ctype or '(空)'}；"
                      "本接口只接受 multipart/form-data",
             "code": "unsupported_media_type"},
            status=415)
    # multipart/* 但没带 boundary -> aiohttp 会抛 ValueError -> 500。
    # 这里自己先查掉，避免把客户端错误伪装成服务端错误。
    if main_ctype == "multipart/form-data" and "boundary=" not in ctype.lower():
        return web.json_response(
            {"error": "Content-Type 缺少 boundary 参数："
                      "multipart/form-data 必须带 boundary",
             "code": "content_type_invalid"},
            status=400)

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
        elif part.name in ("file_refvid", "file_refaud"):
            # 【2026-11-15】参考视频 / 参考音频的**本地直传**通道。
            #
            # 【为什么要有这条通道】参考视频与参考音乐之前**只能**从素材库选
            # （面板上连槽位都没有，见缺口 1）。现在面板补了槽位，
            # 本地直传也得有对应的落盘路径，否则用户选了本地文件就被丢掉。
            #
            # 视频 / 音频不能走 save_uploaded_image（那个按图片后缀白名单
            # 处理，会把 .mp4 改名成 .png）—— 这里原样落盘。
            fname = await save_uploaded_media(part)
            saved_files.setdefault(part.name, []).append(fname)

    # 【2026-11-20 新增 —— 先解析尺寸，再做视频归一】
    #
    # 【原来错在哪（实测复现）】``normalize_t2v_params`` 的第一件事就是
    # 校验 width/height（「宽高无效：宽高必须数字」-> 400）。
    # 而 flf / r2v 的宽高是**后面**的 ``prepare_job_params`` ->
    # ``h3_video_size.apply_size`` 才算出来的。
    # 于是「不传宽高、只传 resolution / megapixels」的提交
    # （内置 Agent 自建 job、旧客户端、只有档位没有像素的调用方）
    # **在建任务之前**就被 400 挡掉 —— 尺寸解析根本没机会跑。
    #
    # 【为什么这在本轮特别重要】
    # 「保存的默认参数」里存的是 **MP 预算 + 百分比**，不是像素宽高。
    # 面板提交路径若不先解析尺寸，就**永远用不上**这份默认值 ——
    # 而用户要的正是「默认参数对两条路径都生效」。
    #
    # 【同一份实现】``apply_gen_defaults_and_size``（内含
    # ``size_spec.plan_size``）—— 与 Agent 路径、与
    # ``h3_video_size.apply_size`` 同一套换算规则。
    apply_gen_defaults_and_size(str(mode or ""), params)

    # 文生视频新面板：服务端校验并归一全部参数。
    # 这一段是**唯一**的参数入口 —— 前端传什么都不认，只认这里算出来的值。
    # 必须在 prepare_job_params **之前**跑：归一会改写 params，
    # 而 prepare 的占位/素材校验依赖归一后的宽高。
    if mode in ("t2v", "flf", "r2v"):
        # 【2026-11-15 修复③ 交付项 3】三个视频模式共用同一套面板，
        # 参数也就必须走**同一个归一入口** —— 否则面板上能调的参数
        # （步数 / 采样器 / 调度器 / shift / 加速 LoRA / 效果 LoRA /
        #  注意力 / 二采 / 宽高）在服务端会被静默丢掉。
        #
        # 【mode 的两处真实差异】面积上限（各模式按 mode_size_policy）
        # 与底模通道（r2v 必须 Ref2VA）—— 都在 normalize_t2v_params 内
        # 按 mode 处理。
        normalized, error = normalize_t2v_params(params, mode=mode)
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
    #   * 校验上游产物支持接续（模式 + 潜空间在不在）；
    #   * 算出本段的段号、链 id、累计帧数。
    #
    # 【为什么不信任前端传来的 chain_id / stage_index】前端回填的内容
    # 是**上一段**的链信息，本段是新的一段 —— 段号必须 +1、
    # 接力棒必须指向上一段。这两件事只能由服务端算。
    #
    # 【2026-11-16 放开 flf / r2v】原来只有 t2v 走这条校验。
    # 现在三个视频模式都走 —— 因为它们的接续链是**同一套**协议
    # （同一份 h3studio/continuation.py 的坐标与闸门），
    # 只是工作流拼装分别落在 video_workflow / h3_video_modes。
    if mode in ("t2v", "flf", "r2v") and params.get("continuation"):
        cont_error = _prepare_continuation_params(params)
        if cont_error:
            return web.json_response({"error": cont_error}, status=400)


    # 生成批次：文生图支持一次出 N 张（各自独立种子）
    batch_count = 1
    if mode in ("t2i", "t2i_qwen", "i2i_qwen", "t2i_moss",
                "t2i_h3image", "i2i_h3image"):
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


def _probe_video_frame_count(path: Path) -> int:
    """这个视频有多少帧（探测不出返回 0）。

    【用途】成片尾裁要按**帧**裁（不是按秒），所以需要各段的真实帧数
    才能算出 concat 之后的全局帧区间。

    【为什么优先用 nb_frames 而不是 duration×fps】
    `duration * fps` 会因为容器/流时长的小数误差算出非整数
    （实测分段视频的 duration 有 0.041667 这类值），
    用它算区间会差 1 帧 —— 而成片帧数必须**精确**命中用户口径。
    `nb_frames` 是容器记录的帧数，精确。
    拿不到 nb_frames 时退回 `nb_read_frames`（要真的解码一遍，慢但准）。
    """
    if not FFPROBE:
        return 0
    try:
        raw = subprocess.check_output(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-count_frames",
             "-show_entries", "stream=nb_frames,nb_read_frames",
             "-of", "json", str(path)],
            encoding="utf-8", timeout=60, creationflags=CREATE_NO_WINDOW)
    except (subprocess.SubprocessError, OSError, ValueError):
        return 0
    try:
        stream = (json.loads(raw).get("streams") or [{}])[0]
    except (json.JSONDecodeError, IndexError, TypeError):
        return 0
    for key in ("nb_frames", "nb_read_frames"):
        try:
            value = int(stream.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


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
            # 【2026-11-13 修】同 worker 兜底：不写原始异常文本。
            conn.execute("UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=? AND status IN ('queued','running')",
                         (_user_facing_error(exc), time.time(), job_id))
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
            "SELECT id, mode, params, status, created_at, started_at, prompt_id "
            "FROM jobs WHERE status IN ('queued','running') AND mode!='longfilm' "
            "ORDER BY created_at ASC").fetchall()
    items = []
    running_id = None
    running_mode = ""
    pending = 0
    now = time.time()
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
            # 【2026-11-14 A5】队列面板也要能区分「真的在跑」与「卡住的 running」。
            # 这里先给**本地判定**（零 IO）；实时判定由 api_queue 补上。
            "started_at": row["started_at"],
            "prompt_id": row["prompt_id"] or "",
            "running_truth": running_truth.job_running_truth(
                status=status,
                started_at=row["started_at"],
                now=now,
                # 【2026-11-19 缺陷 1 修】上限按**媒体类型**取
                # （视频类默认 0 = 不按时长判失败）。
                timeout_sec=timeout_sec_for_mode(row["mode"]),
            ),
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

    【2026-11-14 A5】对「运行中」的那一条补上**实时**判定 ——
    队列面板是这个工作台里最直观的「在不在跑」的展示位，
    它必须说真话（真在跑 / 已丢失 / 已超时 / 判定不了）。
    """
    snapshot = queue_snapshot()
    items = snapshot["items"]
    if len(items) > QUEUE_VIEW_LIMIT:
        snapshot["items"] = items[:QUEUE_VIEW_LIMIT]
    # 【A5】实时判定（对**所有**运行中的条目 —— 残留 running 可能有多条）。
    now = time.time()
    live_map = await live_states_for_jobs(snapshot["items"], now=now)
    live_state = ""
    for item in snapshot["items"]:
        if item.get("status") != "running":
            continue
        state = live_map.get(item.get("id"))
        item["running_truth"] = running_truth.job_running_truth(
            status=item["status"],
            started_at=item.get("started_at"),
            now=now,
            # 【2026-11-19 缺陷 1 修】按媒体类型取上限（视频类默认不限时）。
            timeout_sec=timeout_sec_for_mode(item.get("mode")),
            live_state=state,
        )
        if not live_state and state:
            live_state = state
    snapshot["running_state"] = live_state
    # 【2026-11-19 缺陷 1 修】队列级上限按**当前运行中任务**的媒体类型取
    # （视频类默认 0 = 不限时）。队列里没有运行中任务时给图像类的默认值。
    snapshot["running_timeout_sec"] = timeout_sec_for_mode(
        next((i.get("mode") for i in snapshot["items"]
              if i.get("status") == "running"), ""))
    return json_ok(snapshot)


async def api_list(request):
    status = request.query.get("status")
    conditions = []
    args = []
    if status and status != "all":
        conditions.append("status=?")
        args.append(status)
    # 【2026-11-20 新增】「只看收藏」筛选。
    #
    # 【为什么用子查询而不是先查收藏表再回表】
    # 收藏视图要和既有标签**叠加**（状态 + 生成类型 + 作品类型 + 搜索 + 分页），
    # 子查询天然与其它条件 AND 组合，不用在前端做二次过滤 ——
    # 二次过滤会破坏分页（先取全量再过滤，页内条数就不准了）。
    only_favorites = request.query.get("favorites", "").strip().lower()
    if only_favorites in ("1", "true", "yes"):
        conditions.append("id IN (SELECT job_id FROM job_favorites)")
    elif only_favorites not in ("", "0", "false", "no"):
        return web.json_response(
            {"error": "favorites 只能是 1/0"}, status=400)
    media = request.query.get("media", "").strip()
    if media in ("image", "video", "audio"):
        # 媒体维度按「生成类型注册表」的 media 字段推导，而不是硬编码 mode 列表 ——
        # 硬编码会把 t2i_qwen / i2i_qwen 这类新增的图像模式漏掉
        # （实测漏了 9 条 Qwen 出图记录：生成库「图片」筛选里一条都看不到）。
        #
        # 【2026-11-15 修 —— 补 audio】
        # 注册表里 t2a（文生音乐）/ a2a（参考生音乐）的 media 就是 audio，
        # 但原来**没有**任何分支消费它 -> media=audio 直接 400，
        # 于是音乐产物在生成库里**无法按类型筛出**（只能靠「全部类型」看到）。
        media_modes = [m for m in gen_types.registered_modes()
                       if gen_types.type_for_mode(m).get("media") == media]
        if media == "image" and not media_modes:
            media_modes = ["t2i"]
        if not media_modes:
            # 注册表里没有该媒体类型（极端情况）-> 不加条件，
            # 别把整个查询变成空结果（那比不过滤更难排查）。
            pass
        else:
            conditions.append("mode IN (%s)" % ",".join("?" * len(media_modes)))
            args.extend(media_modes)
    elif media == "longfilm":
        conditions.append("mode='longfilm'")
    elif media:
        return web.json_response(
            {"error": "media 只能是 image、video、audio 或 longfilm"}, status=400)
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
    # 【2026-11-21 性能修】**先把效果 LoRA 映射预热好**，再进 row_to_job。
    #
    # 【为什么必须在进 row_to_job 之前】`row_to_job()` -> `summarize()`
    # -> `_effect_lora_display()` 会查「名字 -> 显示名」映射。
    # 映射**首次**构建要扫全盘 712 个 safetensors（实测 377~980ms）。
    # 如果在 row_to_job 里面第一次触发，那次扫盘就跑在 **aiohttp 事件
    # 循环**里 —— 整个 8190 服务被冻住（与 2026-11-16「整站不可访问」
    # 同类的故障），用户看到的就是「有时候载入很慢很慢」。
    #
    # 【怎么避开】`asyncio.to_thread` 把扫盘丢到工作线程，
    # 事件循环继续跑其它请求；扫盘完成后映射进缓存，
    # row_to_job 里就是毫秒级的查表。
    #
    # 【代价】命中缓存时这一步几乎免费（一次 dict 查找）。
    await asyncio.to_thread(job_meta.warm_effect_lora_display_map)
    # 【2026-11-21 性能修】开启血缘的**请求级**缓存 ——
    # 同一次请求里同一个 job_id 的血缘只回溯一次（见 _LINEAGE_CACHE）。
    _LINEAGE.begin()
    try:
        jobs = []
        for r in rows:
            job = row_to_job(r)
            jobs.append(job)
    finally:
        _LINEAGE.end()
    # 【2026-11-21 性能修】批次**一次性**取回，不再每条任务查一次。
    #
    # 【原来的问题】`job["batches"] = batches_of(r["id"])` 在循环里 ——
    # 每条任务一次 SELECT。列表接口一次 100 条 = 100 次查询
    # （实测 SQL 计数 213 -> 修后 113）。
    #
    # 【口径不变】batches_map 与 batches_of 取同一张表、同样的字段
    # 与排序，只是把 N 次查询合并成 1 次。没有批次的任务拿到的仍是
    # 空列表（`batches_map` 里没出现的 key 就是空）。
    _bm = batches_map([j["id"] for j in jobs])
    for job in jobs:
        job["batches"] = _bm.get(job["id"], [])
    # 【2026-11-20 新增】每条任务的收藏标记。
    #
    # 【为什么要下发这个字段】卡片右上角要显示「已收藏」标识，
    # 右键菜单也要据此显示「添加到收藏」还是「取消收藏」。
    # 服务端一次批量查（不 N+1），前端直接读 job.favorited。
    with db() as conn:
        fav_ids = favorites.favorite_ids(conn, [j["id"] for j in jobs])
    for job in jobs:
        job["favorited"] = job["id"] in fav_ids
    # 【2026-11-21 性能修】给每条产物补**受限尺寸缩略图**的 URL。
    #
    # 【为什么必须有 —— 实测 2026-11-21】
    #   卡片原本直接引用原始产物。CDP `Network.dataReceived` 实测一页 12 张卡
    #   拉了 **8.75MB** 媒体字节，其中 11 个视频占 **7.81MB**
    #   （Chrome 对 `<video preload="metadata">` 发 `Range: bytes=0-`，
    #    aiohttp 的 FileResponse 对 `bytes=0-` 返回**整个文件**）。
    #   图片原图 1.1MB~9.5MB。
    #
    # 【缩略图实测】图片 ~30ms / 视频 ~140ms，产物 14~43KB
    #   ⇒ 一页媒体字节 8.75MB -> ~0.3MB（约 30 倍）。
    #
    # 【为什么在这里生成而不是让前端去请求时再生成】
    #   * 前端不用算 key（key 含 mtime/size，算不出来）；
    #   * 缩略图在**列表接口**里一次性下发，前端只管用；
    #   * 生成失败就**不给 thumb 字段** -> 前端自动回落原图（不降级功能）。
    #
    # 【成本】只为**这一页**的产物生成（每页 12~48 张），
    # 且生成后落盘缓存，第二次起几乎免费。
    await _attach_thumb_urls(jobs)
    # 【2026-11-14 A5】给「正在跑」的**那些**补上**实时**判定。
    #
    # 【为什么要「那些」而不是「那一条」—— A5 的核心场景】
    # 正常情况下 worker 一次只跑一个，running 只有一条；
    # 但**残留** running 恰恰会出现多条（服务重启前排着几个，
    # 重启后它们全都卡在 running）。只判定第一条 -> 其余继续显示
    # 「运行中」-> 缺陷没修干净。
    #
    # 【前端据此区分】「真的在跑」vs「卡住的 running」：
    #   running_truth.state == "active" -> 真的在 ComfyUI 队列里；
    #   == "lost"                      -> ComfyUI 里已经没了（取消/丢失）；
    #   == "timeout"                   -> 超过运行上限；
    #   == "unknown"                   -> 判定不了（ComfyUI 读不到）。
    now = time.time()
    live_map = await live_states_for_jobs(jobs, now=now)
    live_state = ""
    for job in jobs:
        if job.get("status") != "running":
            continue
        state = live_map.get(job.get("id"))
        job["running_truth"] = running_truth.job_running_truth(
            status=job["status"],
            started_at=job.get("started_at"),
            now=now,
            # 【2026-11-19 缺陷 1 修】按媒体类型取上限（视频类默认不限时）。
            timeout_sec=timeout_sec_for_mode(job.get("mode")),
            live_state=state,
        )
        if not live_state and state:
            live_state = state
    return json_ok({"jobs": jobs,
                    "total": total, "page": page, "page_size": page_size,
                    "running": WORKER.current,
                    # 【A5】实时判定结果（空串 = 没有运行中的任务 / 判定不了）。
                    "running_state": live_state or "",
                    # 【2026-11-19 缺陷 1 修】按**当前运行中任务**的媒体类型取
                    # （视频类默认 0 = 不限时）。
                    "running_timeout_sec": timeout_sec_for_mode(
                        next((j.get("mode") for j in jobs
                              if j.get("status") == "running"), ""))})


async def api_get(request):
    jid = request.match_info["id"]
    with db() as conn:
        r = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not r:
        return web.json_response({"error": "not found"}, status=404)
    job = row_to_job(r)
    job["batches"] = batches_of(jid)
    # 【2026-11-20 新增】单条查询也要带收藏标记 ——
    # 列表接口（api_list）带了，这里不带的话，同一个任务在两个接口里
    # 的收藏状态会不一致（前端详情模态框读的就是这个接口）。
    with db() as conn:
        job["favorited"] = favorites.is_favorited(conn, jid)
    # 【2026-11-14 A5】详情接口也要能区分「真的在跑」与「卡住的 running」。
    if job.get("status") == "running":
        live_state = await live_state_for_job(job)
        job["running_truth"] = running_truth.job_running_truth(
            status=job["status"],
            started_at=job.get("started_at"),
            now=time.time(),
            # 【2026-11-19 缺陷 1 修】按媒体类型取上限（视频类默认不限时）。
            timeout_sec=timeout_sec_for_mode(job.get("mode")),
            live_state=live_state,
        )
    return json_ok(job)


# ------------------------------------------------------------------
# 生成媒体库「收藏」（2026-11-20 新增需求）
# ------------------------------------------------------------------
#
# 【接口清单】
#   POST /api/jobs/{id}/favorite        {"favorited": true|false}
#       -> 收藏 / 取消收藏**单个**任务
#   POST /api/jobs/favorite/batch       {"job_ids": [...], "favorited": false}
#       -> **批量**取消收藏 / 批量收藏
#   GET  /api/jobs/favorites            -> 收藏总数（收藏视图的计数徽标）
#
# 【语义纪律 —— 与「删除产物」严格区分】
#   取消收藏 = **只**解除收藏标记。任务记录、产物文件、文件索引一律不动。
#   删除产物 = 既有删除语义（DELETE /api/jobs/{id}），会删记录与文件。
#   两者是**不同的接口**（本文件的 favorite 接口 vs 既有的 delete 接口），
#   前端也用不同的按钮文案与二次确认 —— 绝不共用一个入口。


async def api_job_favorite(request):
    """POST /api/jobs/{id}/favorite —— 收藏 / 取消收藏单个任务。

    【为什么用 POST 而不是 PUT/DELETE】
    语义是「设成某个状态」，两种方向都要支持（幂等切换），
    POST + body 里的 favorited 布尔最直白，也与既有风格一致。
    """
    jid = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    favorited = bool(body.get("favorited", True))
    with db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone()
        if not exists:
            # 收藏一个不存在的任务 = 无意义（收藏视图里会出现死条目）。
            return web.json_response({"error": "任务不存在"}, status=404)
        favorites.ensure_table(conn)
        state = favorites.set_favorite(conn, jid, favorited)
        total = favorites.count(conn)
    return json_ok({"id": jid, "favorited": state, "total": total})


async def api_jobs_favorite_batch(request):
    """POST /api/jobs/favorite/batch —— 批量收藏 / 批量取消收藏。

    body: {"job_ids": ["id1", "id2"], "favorited": false}

    【为什么要批量接口而不是前端循环调单个】
    收藏视图一次可能选中几十张 —— 前端循环 = 几十个 HTTP 往返，
    而且中途失败会留下「一半成功一半失败」的中间态。
    一个接口一次提交，返回逐个 id 的最终状态，语义清楚。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    ids = body.get("job_ids")
    if not isinstance(ids, list):
        return web.json_response(
            {"error": "job_ids 必须是数组"}, status=400)
    ids = [str(i).strip() for i in ids if str(i or "").strip()]
    if not ids:
        return web.json_response({"error": "没有选中任何卡片"}, status=400)
    favorited = bool(body.get("favorited", False))
    results = {}
    with db() as conn:
        favorites.ensure_table(conn)
        known: set[str] = set()
        # 分批查 —— SQLite 的绑定变量上限是 999，一次塞进去会报
        # "too many SQL variables"。超过 500 个 id 时分批。
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            known.update(str(r[0]) for r in conn.execute(
                "SELECT id FROM jobs WHERE id IN (%s)"
                % ",".join("?" * len(chunk)), chunk).fetchall())
        for jid in ids:
            if jid not in known:
                results[jid] = "not_found"
                continue
            state = favorites.set_favorite(conn, jid, favorited)
            results[jid] = "favorited" if state else "unfavorited"
        total = favorites.count(conn)
    return json_ok({"results": results, "total": total,
                    "favorited": favorited})


async def api_favorites_count(request):
    """GET /api/jobs/favorites —— 收藏总数 + 收藏 id 列表。

    【为什么要给 id 列表】收藏视图要能对「哪些 id 已收藏」做本地判定
    （例如批量选择时全选 / 取消全选）。
    """
    with db() as conn:
        favorites.ensure_table(conn)
        ids = sorted(favorites.favorite_ids(conn))
    return json_ok({"total": len(ids), "ids": ids})


# ------------------------------------------------------------------
# 产物实际生成参数（2026-11-15 媒体库卡片详情模态框）
# ------------------------------------------------------------------
async def api_artifact_meta(request):
    """GET /api/jobs/{id}/artifact-meta —— 返回该产物的**完整生成参数**。

    【为什么单独一个接口】
    用户需求：「点击卡片弹出该产物的模态框，展示详细内容
    （步数、调度器、采样器、模型/LoRA 列表、种子、尺寸等实际元数据）」。

    列表接口 `/api/jobs` 已经带 `gen_params`（卡片行用的那份），
    但详情模态框要的是**字段齐全、带来源标注、能区分「未记录」与
    「确实没有」**的一份。所以这里单独给一份结构化的产物元数据。

    【数据来源 —— 产物实际，不是提交参数】
      * `params["artifact_loras"]` —— worker 在构建完工作流之后
        从 workflow 反解回写的「实际挂载的 LoRA 清单」
        （见 h3studio/artifact_meta.py）。这是权威来源。
      * `params["steps"] / ["scheduler"] / ["sampler"] / ["cfg"] / ["seed"]`
        —— 同样是 worker 回写的**实际生效值**。
      * `params["width"] / ["height"]` —— 优先用产物实际像素
        （outputs 里带的 width/height），其次用落库的请求值。

    【缺失 = 「未知」】
    字段取不到就返回 `null`，**不编造、不拿别的字段顶替**。
    前端把 null 渲染成「未知」。

    【为什么老任务也返回结构】
    老任务（worker 回写之前生成的）没有 `artifact_loras` ->
    LoRA 列表为空数组、`loora_status` = "not_recorded" ->
    前端显示「未知（该任务生成时未记录 LoRA）」而不是假名。
    """
    jid = request.match_info["id"]
    with db() as conn:
        r = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not r:
        return web.json_response({"error": "not found"}, status=404)

    job_params = json.loads(r["params"] or "{}")
    if not isinstance(job_params, dict):
        job_params = {}
    outputs = json.loads(r["outputs"] or "[]")
    mode = r["mode"]

    model_info = job_meta.summarize(mode, job_params)
    resolution_info = _job_resolution_info(job_params, outputs)
    gen_params = job_meta.gen_params(mode, job_params, model_info, resolution_info)

    # ---- LoRA：产物实际挂载清单 ----
    loras = []
    raw_artifact = job_params.get("artifact_loras")
    has_artifact_record = isinstance(raw_artifact, list)
    if has_artifact_record:
        for item in raw_artifact:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            strength = item.get("strength")
            try:
                strength = float(strength) if strength is not None else None
            except (TypeError, ValueError):
                strength = None
            # 【显示名反查】权重文件名 -> 本机清单里的 key -> 友好显示名。
            # 查不到就如实用清洗后的权重名（**不编**）。
            # 这样接口返回的 name 与卡片 / 详情显示的是同一个名字。
            raw_name = str(item.get("raw_name") or "")
            key = artifact_meta.lora_key_for_weight(raw_name or name)
            display = job_meta.accel_lora_display_name(key) if key else name
            loras.append({
                "name": display,
                "key": key,
                "raw_name": raw_name,
                "strength": strength,
                "loader": str(item.get("loader") or ""),
            })

    if loras:
        lora_status = "recorded"
    elif has_artifact_record:
        # worker 回写过、且工作流里确实没有 LoRA 节点 -> 确实没用
        lora_status = "none_used"
    else:
        # 没有产物级记录 -> 不知道（不编）
        lora_status = "not_recorded"

    # ---- 尺寸：优先产物实际像素 ----
    width = resolution_info.get("width")
    height = resolution_info.get("height")
    if not (width and height):
        width = job_params.get("width")
        height = job_params.get("height")

    def _int_or_none(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    return json_ok({
        "id": jid,
        "mode": mode,
        "status": r["status"],
        "created_at": r["created_at"],
        "finished_at": r["finished_at"],
        # ---- 生成参数（缺失 = null -> 前端显示「未知」）----
        "model": gen_params.get("model") or None,
        "model_file": job_params.get("artifact_model_file") or None,
        "steps": _int_or_none(job_params.get("steps")),
        "scheduler": job_params.get("scheduler") or None,
        "sampler": job_params.get("sampler") or None,
        "cfg": job_params.get("cfg"),
        "seed": gen_params.get("seed") or None,
        "width": _int_or_none(width),
        "height": _int_or_none(height),
        "duration": gen_params.get("duration") or None,
        "resolution": job_params.get("resolution") or None,
        "aspect": job_params.get("aspect") or None,
        "attention": gen_params.get("attention") or None,
        "workflow": gen_params.get("workflow") or None,
        # ---- LoRA（产物实际）----
        "loras": loras,
        "lora_status": lora_status,
        # 【2026-11-15 修 —— lora_summary 必须与 lora_status 一致】
        #
        # 【为什么要改】gen_params.lora 走的是 resolve_job_lora 的**回落路径**：
        # 「params 里没有 LoRA 记录」时它回落成 ckpt850（那是工作流的默认
        # 档，对**新**任务确实成立）。但对**历史**任务（worker 回写之前
        # 生成的），我们**没有证据**说它挂了 ckpt850 —— 接口已经如实标了
        # lora_status="not_recorded"，lora_summary 却还显示 ckpt850，
        # 两个字段自相矛盾 = 仍在显示「没证据的假名」。
        #
        # 现在：有产物实际记录 -> 显示实际；明确没用 -> 「未使用 LoRA」；
        # 没记录到 -> 「未知（该任务生成时未记录 LoRA）」。
        # 三态与前端 artifactLoraHtml() 完全对齐。
        "lora_summary": (
            " · ".join(
                (f"{item['name']} ×{float(item['strength']):.2f}"
                 if item.get("strength") is not None else str(item["name"]))
                for item in loras
            ) if loras else (
                "未使用 LoRA"
            )
        ),
        # ---- 提示词 ----
        "prompt": str(job_params.get("prompt") or ""),
        "original_prompt": str(job_params.get("original_prompt") or ""),
        # ---- 产物清单 ----
        "outputs": outputs,
    })


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
    # 【2026-11-18 第 6 轮审查修 —— 显示值 ≠ 真实产物（P1）】
    # build_continuation_context 的 total_seconds 取自协议层的
    # continuation_state（累计**交付**帧数，不含成片尾裁），与真实成片
    # 文件差一个尾裁帧数。这里改用与生成库卡片**同一套**口径
    # （_continuation_info_for_row），保证「卡片显示」「接续面板显示」
    # 「真实成片」三者一致。
    #
    # ⚠ **r 必须带 "id"** —— _continuation_info_for_row 用 r["id"] 查血缘。
    #   漏了它会抛 KeyError，被下面的 except 吞掉 -> 修复**静默不生效**
    #   （本轮首次部署就踩到了：显示值仍是 226 帧，与真实成片 216 帧不符）。
    #   ⇒ 所以这里**不吞异常**：算不出来就让请求报错，而不是悄悄给旧值。
    _row = {"mode": mode, "id": jid}
    _state = _continuation_info_for_row(_row, params)
    context["total_frames"] = _state.get("total_frames", 0)
    context["total_seconds"] = _state.get("total_seconds", 0)
    if "raw_total_frames" in _state:
        context["raw_total_frames"] = _state["raw_total_frames"]
        context["raw_total_seconds"] = _state["raw_total_seconds"]
        context["trimmed_frames"] = _state["trimmed_frames"]
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
    # ---- 血缘提示词历史（2026-11-17 新增）----
    #
    # 【为什么需要】用户需求：接续时点「增强」要把**此前所有段落**
    # 的提示词一并提供给增强模型，保证跨段内容一致性。
    # 前端要能拿到这些历史 -> 必须由服务端下发（前端不知道别的
    # 任务 id，也无权自己查 DB）。
    #
    # 【口径】沿 `source_job` 血缘回溯，与拼接口径**完全同源**
    # —— 兄弟分支不会混进来。包含**本段自己**（用户点「接续」的
    # 那一段）的那份提示词。
    context["prompt_history"] = _lineage_prompts(jid)
    # ---- 成片时长账（2026-11-17 更正）----
    #
    # 【用户口径】成片总时长 = Σ(各段请求时长) - (段数-1) × 1 秒
    # （潜空间接续有 1 秒重叠，来自前段尾部帧，不重复计入）。
    #
    # 【为什么由服务端下发】前端不知道别的任务 id、也不该自己重算帧网格
    # 规则 —— 「17k+5 帧网格」与「5+17k 合法钉住窗口」都是协议约束，
    # 只有服务端算得对。
    #
    # 【extra_seconds】把「下一段要请求多少秒」一起算进去 ——
    # 用户点「接续」时面板上填的时长就是下一段的请求时长，
    # 于是这里直接给出「接完这一段之后成片会有多长」。
    # （前端用 ?duration=5 传；不给就只算到当前段。）
    extra = request.query.get("duration")
    context["duration_plan"] = _chain_duration_plan(jid, extra_seconds=extra)
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
            # 【2026-11-19 第 3 轮 C 项审查】改用共用的
            # _terminate_longfilm_process —— 与 Agent 的 cancel_job 走
            # **同一套**实现（原来两边各写一份，是本缺陷的成因）。
            _terminate_longfilm_process(jid)
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
        # 【2026-11-20 新增】删除任务时顺带清掉它的收藏标记。
        #
        # 【为什么要清】任务没了，收藏标记就成了**孤儿行** ——
        # 留着的话「收藏视图」里会留下一条点开是 404 的死条目。
        #
        # 【注意语义边界】这里删的是**标记**，不是产物文件；
        # 产物文件的删除走下面既有的删除逻辑，与「取消收藏」是两件事。
        try:
            favorites.remove_for_job(conn, jid)
        except Exception:
            pass   # 收藏表可能还没建（老库）—— 不阻断既有删除语义
    for output in json.loads(r["outputs"] or "[]"):
        relative = output.get("file", "")
        if "/" not in relative:
            continue
        target = (OUT_DIR / relative).resolve()
        # 【2026-11-15 代码审查修 —— 越界 unlink】
        # 原来用的是 `str(target).startswith(str(OUT_DIR.resolve()))` ——
        # 那是**字符串前缀**判定，不是路径边界判定：
        # 同级目录 `outputs_secret` 的前缀是 `.../outputs` -> 判定为 True。
        #
        # 【当前可达性】实测 `outputs[].file` 的来源是
        # `comfy_download_output()` 的 `dest.relative_to(OUT_DIR).as_posix()`
        # —— 服务端自己生成的文件名，**不是用户可控**，
        # 所以这条路径目前不可被利用。
        # 但它与 `/files/` 的读取口（files_handler）用的是同一套脆弱判据，
        # 而 `/files/` 那条**确实可被 %2F 编码绕过**（已修）。
        # 同一份数据走两条路径，其中一条判据错 —— 迟早出问题。
        # 这里统一改成路径边界判定（relative_to），与 files_handler 一致。
        try:
            target.relative_to(OUT_DIR.resolve())
        except ValueError:
            continue
        if target.is_file():
            target.unlink()
    shutil.rmtree(OUT_DIR / jid, ignore_errors=True)
    return json_ok({"deleted": jid})


async def api_media_manage(request):
    """生成媒体库「管理」（2026-11-14 需求 11 / 12）。

    GET  /api/media/manage?scope=all|older|failed&months=1
        -> 预览「会删多少」（不删任何东西）
    POST /api/media/manage  {"scope": "...", "months": 1, "confirm": true}
        -> 执行清理

    【只影响本工作台】
    只删本项目 jobs / job_batches 表的记录 + 本项目 outputs 目录里的文件。
    **不碰** ComfyUI 自身的 output 目录、不碰素材库（assets）、不碰其它项目。
    """
    if request.method == "GET":
        scope = request.query.get("scope", "all") or "all"
        # 【2026-11-15 代码审查修 —— months 非法值导致 500】
        # 原来这里直接 `int(request.query.get("months", "0") or 0)`，
        # 没有 try/except —— 而下面 POST 分支的同一参数**有** try/except。
        # 结果：`GET /api/media/manage?months=abc` 直接抛 ValueError ->
        # HTTP 500 + 堆栈进日志（实测 8191：HTTP 500）。
        # 修法：与 POST 分支同口径 —— 非法值退回 0。
        try:
            months = int(request.query.get("months", "0") or 0)
        except (TypeError, ValueError):
            months = 0
        if scope not in ("all", "older", "failed"):
            return web.json_response({"error": "scope 只能是 all / older / failed"},
                                     status=400)
        with db() as conn:
            info = media_cleanup.preview(conn, OUT_DIR, scope, months)
        return json_ok(info)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "请求体必须是 JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "请求体必须是对象"}, status=400)
    scope = str(body.get("scope") or "all")
    if scope not in ("all", "older", "failed"):
        return web.json_response({"error": "scope 只能是 all / older / failed"},
                                 status=400)
    try:
        months = int(body.get("months") or 0)
    except (TypeError, ValueError):
        months = 0
    if scope == "older" and months <= 0:
        return web.json_response({"error": "清理指定月数之前的历史产物时，"
                                              "months 必须大于 0（例：1 = 30 天前）"},
                                 status=400)
    # 【必须显式确认】这是不可逆操作（删记录 + 删文件）。
    # 没有 confirm=true 一律拒绝 —— 宁可多一次点击，也不要误删。
    if not body.get("confirm"):
        with db() as conn:
            info = media_cleanup.preview(conn, OUT_DIR, scope, months)
        return web.json_response({
            "error": "需要 confirm=true 才会执行清理",
            "preview": info,
        }, status=400)
    with db() as conn:
        result = media_cleanup.cleanup(conn, OUT_DIR, scope, months)
    return json_ok(result)


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


#: H3-Image 节点探测专用的**长连接 session**（模块级，复用连接池）。
#:
#: 【为什么必须复用连接 —— 实测根因，2026-11-13】
#: 同一个 aiohttp ClientSession 内复用连接 -> 5/5 成功，每次 0.00s；
#: 每次**新建** ClientSession -> 前几次卡满 3 秒超时（2/5 成功）。
#: curl 同一 URL 始终 1ms。
#: -> 慢的不是 ComfyUI，是「每次新建 session」的连接建立路径。
#:
#: 这与本项目其它地方一致（refresh_comfy_node_names 也复用 session）。
#: 探测频率本来就不高（60 秒缓存），复用连接没有副作用。
_H3_IMAGE_PROBE_SESSION: ClientSession | None = None


async def _h3_image_probe_session() -> ClientSession:
    """取（或建）探测专用的长连接 session。

    session 失效时（ComfyUI 重启过）会自动重建 —— 探测失败会返回
    TIMEOUT，下一次调用时这里检测到连接已关闭就换一个。
    """
    global _H3_IMAGE_PROBE_SESSION
    sess = _H3_IMAGE_PROBE_SESSION
    if sess is not None and not sess.closed:
        return sess
    sess = ClientSession(timeout=ClientTimeout(total=3))
    _H3_IMAGE_PROBE_SESSION = sess
    return sess


#: H3-Image 节点探测的结果缓存（60 秒）。
#:
#: 【为什么需要缓存】
#: 实测（2026-11-13）：直接用 aiohttp 打 /object_info，**大多数时候 1ms 返回**，
#: 但**偶发**会整次卡到 3 秒超时（约 6 次里有 1 次）。curl 同样的 URL 始终 1ms
#: -> 是 aiohttp 侧的连接建立/复用问题，不是 ComfyUI 慢。
#:
#: 这个偶发超时会让界面在「缺节点」与「暂时无法探测」之间**抖动** ——
#: 用户刷新一次看到一种说法，再刷一次又变了，很难自查。
#:
#: 缓存把探测频率降到 1 次/60 秒，抖动就看不到了。
_H3_IMAGE_NODES_CACHE: tuple[float, bool, bool] = (0.0, False, False)
_H3_IMAGE_NODES_CACHE_TTL = 60.0


async def probe_h3_image_nodes(session=None, force: bool = False) -> tuple:
    """探测本机 ComfyUI 有没有 H3-Image 必需的核心节点。

    返回 (nodes_available, probe_complete)：
      * nodes_available —— MiniMaxH3ImageConditioning 在不在
      * probe_complete  —— 本次探测**问全了**吗（区分「确实不在」
        与「没问到」：ComfyUI 卡住时探测会超时，此时不能断言缺节点）

    【为什么单独探测，不复用 refresh_comfy_node_names】
    refresh_comfy_node_names 探的是**视频面板**关心的节点名单
    （_VIDEO_PANEL_NODES），H3-Image 的节点不在那份名单里。
    而且那份探测失败时返回**上一次成功的缓存**（可能是别的节点的集合），
    拿它判断 H3-Image 会得到假结论。

    单独探的好处：只多 1 次 HTTP（实测 1 ms/个），语义独立，
    不会污染视频面板的缓存。

    【60 秒缓存】
    aiohttp 打 /object_info 偶发卡满 3 秒超时（约 1/6 概率）。
    不缓存的话界面会在两种说法之间抖动。缓存后抖动不可见。
    """
    global _H3_IMAGE_NODES_CACHE
    now = time.time()
    cached_ts, cached_nodes, cached_complete = _H3_IMAGE_NODES_CACHE
    if (not force and cached_ts
            and (now - cached_ts) < _H3_IMAGE_NODES_CACHE_TTL):
        return cached_nodes, cached_complete

    async def _probe(s, name):
        try:
            async with s.get(f"{COMFY}/object_info/{name}") as resp:
                if resp.status != 200:
                    return "TIMEOUT"
                data = await resp.json()
                if isinstance(data, dict):
                    return name if name in data else None
                return None
        except Exception:
            return "TIMEOUT"

    async def _run_probe(sess):
        """**顺序**请求，不用 asyncio.gather 并发。

        【为什么不用并发】
        实测（2026-11-13）：gather 并发打 /object_info 会**偶发整次卡满
        3 秒超时**（10 次里 3~6 次），而同样的 URL 用 curl 始终 1ms、
        顺序请求也稳定在 20~60ms。
        -> 慢的根因是并发下的连接建立竞争，不是 ComfyUI 慢。

        H3-Image 只有 1 个必需节点，顺序请求的耗时与并发几乎一样
        （1 个请求 vs 1 个请求），却稳定得多。
        """
        results = []
        for name in h3img.H3_IMAGE_REQUIRED_NODES:
            results.append(await _probe(sess, name))
        return results

    try:
        sess = session if session is not None else await _h3_image_probe_session()
        results = await _run_probe(sess)
    except Exception:
        results = ["TIMEOUT"]

    if any(r == "TIMEOUT" for r in results):
        # 没问全 -> 不能断言「缺节点」（那会给出假阴性结论）
        # 但如果上一次**问全过**，沿用那次结论（节点清单不会突然消失）。
        if cached_ts and cached_complete:
            return cached_nodes, True
        # 【失败也缓存 15 秒】—— 否则每次 /api/image-options 都会重新
        # 撞一次 3 秒超时，把首屏接口拖慢（用户会看到面板迟迟不出）。
        _H3_IMAGE_NODES_CACHE = (now, False, False)
        return False, False
    nodes_ok = all(r is not None for r in results)
    _H3_IMAGE_NODES_CACHE = (now, nodes_ok, True)
    return nodes_ok, True


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


async def fetch_comfy_queue_ids(session) -> set[str] | None:
    """读 ComfyUI 队列里**全部** prompt_id（running + pending）。

    【为什么要 prompt_id 而不是个数 —— A5 需求】
    状态栏只要「在跑几个」（fetch_comfy_queue 就够），但 A5 要回答的是
    「**我这个**任务还在不在 ComfyUI 里」—— 那必须精确到 prompt_id。

    【为什么返回 None 而不是空集合】
    空集合 = 「ComfyUI 读到了，队列确实是空的」；
    None     = 「ComfyUI 读不到（不可达 / 超时）」。
    这两者的含义**完全不同**：
      * 把「读不到」当成「空」-> 正在跑的任务会被全部误判成「已丢失」
        并被杀掉 —— 比原来的 bug 更糟；
      * 把「空」当成「读不到」-> 卡死的任务永远不会被判定为丢失。
    判定层必须能区分这两者，所以这里用 None 表达「不知道」。

    【顺带】/queue 的响应体同时给出数量，状态栏可以复用同一次请求
    （省一次 HTTP 往返）—— 所以这里返回的是 prompt_id 集合，
    数量由调用方按需要算。
    """
    try:
        async with session.get(f"{COMFY}/queue") as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:
        return None
    return running_truth.queue_prompt_ids(data)


#: 实时观测（ComfyUI 队列）的缓存有效期（秒）。
#:
#: 【为什么要缓存】前端每 2 秒刷一次 /api/jobs。如果每次都去查
#: ComfyUI 的 /queue，等于把 ComfyUI 的请求量翻倍 —— 而 ComfyUI
#: 正在跑模型，那边的每一次 HTTP 都要排队。
#: 3 秒的缓存对「任务是不是还在跑」这个判断来说完全够用
#: （人眼分辨不出 3 秒的差别），但负载降到 1/6。
_LIVE_STATE_CACHE: dict = {"at": 0.0, "ids": None}


#: ComfyUI `/history/{prompt_id}` 查询结果的缓存（prompt_id -> (取数时间, 有无结果)）。
#:
#: 【为什么需要它 —— 2026-11-21 生成媒体库「有时候载入很慢很慢」实测根因之一】
#:   `/queue` 有 3 秒缓存（见 LIVE_STATE_CACHE_TTL），但 `/history` **没有**。
#:   `live_state_for_job()` 对**每一条 running 任务**各查一次 /history。
#:   实测（隔离实例 + 假 ComfyUI，见
#:   evidence/mediaperm-20261121/）：/history 慢 2 秒时，
#:   **每一次** /api/jobs 请求都要等 2 秒 —— 而且**每次都不命中缓存**
#:   （前端每 2 秒轮询一次，于是每 2 秒就有一轮 2 秒的阻塞）。
#:
#: 【为什么 ComfyUI 忙的时候 /history 会慢】ComfyUI 正在跑模型时，
#:   它的 HTTP 处理要排队 —— 实测本机 /object_info 满载时 8.9 秒、
#:   /queue 5.2 秒。这段时间里 /api/jobs 也被拖住（同一个事件循环）。
#:
#: 【缓存语义】只缓存「**查得到没有**」这个布尔事实：
#:   * 查到了（history_present=True）-> **永久缓存**。
#:     任务已经出现在 history 里，就永远在（ComfyUI 的 history 不删）。
#:   * 查不到（False）-> 短缓存（3 秒），因为任务可能**正要**写进 history。
#:
#: 【正确性】判定口径不变：
#:   `running_truth.classify(prompt_id, history_present, queue_ids, ...)`
#:   用的是「history 有没有这条」，不是「history 里的内容」。
#:   缓存的是这个布尔值，语义完全等价。
_HISTORY_PRESENT_CACHE: dict[str, tuple[float, bool]] = {}

#: 「查不到」时的缓存有效期（秒）。
#:
#: 【为什么取 3 秒】与 /queue 的缓存同量级 —— 任务刚跑完的那一瞬间
#: 查不到 history 是正常的，3 秒内重查即可；3 秒之后仍未出现，
#: 说明它确实还没写进 history。
_HISTORY_MISS_TTL = 3.0


async def comfy_history_present(session: ClientSession, prompt_id: str) -> bool:
    """ComfyUI 的 history 里**有没有**这条 prompt_id（带缓存）。

    返回 True = 有（任务已经跑完，结果已在 history 里）。
    返回 False = 没有（还在跑 / 从没提交过 / 读不到）。
    """
    if not prompt_id:
        return False
    cached = _HISTORY_PRESENT_CACHE.get(prompt_id)
    if cached is not None:
        at, present = cached
        if present:
            # 出现过就**永久**缓存 —— ComfyUI 的 history 不删条目。
            return True
        if (time.time() - at) < _HISTORY_MISS_TTL:
            return False
    try:
        hist = await comfy_history(session, prompt_id)
    except Exception:
        # 读不到（ComfyUI 超时 / 不可达）-> 不写缓存（下次重试）。
        return False
    present = bool(hist)
    _HISTORY_PRESENT_CACHE[prompt_id] = (time.time(), present)
    return present


async def live_comfy_queue_ids(force: bool = False) -> set[str] | None:
    """取 ComfyUI 队列里的 prompt_id 集合（带短缓存）。

    返回 None = ComfyUI 读不到（不可达）—— 与「队列为空」是两件事。
    """
    now = time.time()
    if not force and (now - _LIVE_STATE_CACHE["at"]) < LIVE_STATE_CACHE_TTL:
        return _LIVE_STATE_CACHE["ids"]
    try:
        async with ClientSession(timeout=ClientTimeout(total=5)) as session:
            ids = await fetch_comfy_queue_ids(session)
    except Exception:
        ids = None
    _LIVE_STATE_CACHE["at"] = now
    _LIVE_STATE_CACHE["ids"] = ids
    return ids


async def live_state_for_job(row, now: float | None = None,
                             queue_ids=None) -> str | None:
    """对一条 status='running' 的任务做**实时**判定（真的在跑？）。

    返回 running_truth.STATE_* 或 None（判定不了 / 不是运行态）。

    【为什么要做实时判定 —— A5 需求 e】
    「已超时」只说明「跑太久了」；但「ComfyUI 队列里已经没有它了」
    是更明确的事实（任务已被取消 / 丢失）。两者要能分开。

    【为什么要对 history 也查一次】
    ComfyUI 的 history 里已经有结果 = 任务其实**跑完了**
    （只是 Studio 还没来得及收尾）。这种情况不该再显示「运行中」。

    【成本】一次 /queue（走缓存）+ 一次 /history/{prompt_id}。
    `queue_ids` 可以由调用方传入以复用同一次 /queue 取数。

    【2026-11-21 性能修】/history 改走 `comfy_history_present()`（带缓存）。
    原来每次都真查 ComfyUI —— ComfyUI 忙时（实测 /object_info 8.9 秒）
    列表接口被拖到同一量级。缓存的是「history 有没有这条」这个布尔事实，
    判定口径完全不变。
    """
    if str(row["status"] if isinstance(row, dict) else "") != "running":
        return None
    data = row if isinstance(row, dict) else dict(row)
    prompt_id = data.get("prompt_id") or ""
    if not prompt_id:
        return running_truth.STATE_UNKNOWN
    history_present = False
    try:
        async with ClientSession(timeout=ClientTimeout(total=5)) as session:
            history_present = await comfy_history_present(session, prompt_id)
    except Exception:
        history_present = False
    if queue_ids is None:
        queue_ids = await live_comfy_queue_ids()
    return running_truth.classify(
        prompt_id=prompt_id,
        history_present=history_present,
        queue_ids=queue_ids,
        now=now if now is not None else time.time(),
        started_at=data.get("started_at"),
        # 【2026-11-19 缺陷 1 修】按媒体类型取上限（视频类默认不限时）。
        timeout_sec=timeout_sec_for_mode(data.get("mode")),
        grace_sec=RUNNING_LOST_GRACE_SEC,
    )


async def live_states_for_jobs(rows, now: float | None = None) -> dict:
    """对**所有** status='running' 的任务做实时判定。

    返回 {job_id: state}。

    【为什么要「所有」而不是「第一条」—— A5 的核心场景】
    正常情况下 worker 一次只跑一个，running 只有一条。
    但**残留** running 恰恰可能出现**多条**（服务重启前排着几个，
    重启后它们全都卡在 running）。如果只判定第一条，
    其余的会继续显示「运行中」—— 缺陷没修干净。

    【成本】/queue 只查一次（走缓存 + 同一批复用），
    history 每个 prompt_id 查一次（残留通常只有 1-3 条）。
    """
    result: dict = {}
    running_rows = [r for r in rows
                    if str((r.get("status") if isinstance(r, dict) else "") or "")
                    == "running"]
    if not running_rows:
        return result
    queue_ids = await live_comfy_queue_ids()
    for row in running_rows:
        data = row if isinstance(row, dict) else dict(row)
        job_id = data.get("id")
        if not job_id:
            continue
        result[job_id] = await live_state_for_job(data, now=now,
                                                  queue_ids=queue_ids)
    return result


async def resolve_running_state(session, prompt_id: str, started_at: float | None,
                                history_present: bool,
                                timeout_sec: float | None = None,
                                media: str = job_timeout.MEDIA_OTHER) -> tuple[str, str]:
    """判定「这个声称在运行的任务」**真实**处于什么状态。

    返回 `(state, reason)`：
      * state  —— running_truth.STATE_* 之一；
      * reason —— 判定为失败态时的中文原因（成功态是空串）。

    【为什么 history_present 由调用方传进来】
    history 的取数在 worker 的轮询循环里（它已经在查了），再查一次
    是白白多一次 HTTP 往返。这里只负责**队列**那一份的取数 + 判定。

    【2026-11-19 缺陷 1 修】`timeout_sec` 由调用方按**媒体类型**传入
    （视频类默认 0 = 不按时长判失败）。不传则退回全局默认上限。
    """
    queue_ids = await fetch_comfy_queue_ids(session)
    limit = RUNNING_TIMEOUT_SEC if timeout_sec is None else float(timeout_sec)
    state = running_truth.classify(
        prompt_id=prompt_id,
        history_present=history_present,
        queue_ids=queue_ids,
        now=time.time(),
        started_at=started_at,
        timeout_sec=limit,
        grace_sec=RUNNING_LOST_GRACE_SEC,
    )
    reason = ""
    if state == running_truth.STATE_LOST:
        reason = running_truth.LOST_REASON
    elif state == running_truth.STATE_TIMEOUT:
        reason = timeout_reason_for(limit, media)
    return state, reason


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


async def api_timeout_policy(request):
    """GET /api/timeout-policy —— 当前生效的「运行上限」策略。

    【为什么要有这个接口 —— 2026-11-19 缺陷 1 修】
    上限从「一个全局 30 分钟」变成「按媒体类型区分 + 可配置」之后，
    用户需要一个地方**看得见**当前生效值（否则只能去翻 config.json）。
    这也符合本项目「配置由服务端算好下发、前端不自己猜」的既有纪律。

    返回：
        {
          "image_timeout_sec": ...,   # 图像 / 音频类上限（0 = 不限时）
          "video_timeout_sec": ...,   # 视频类上限（默认 0 = 不限时）
          "by_media": {"image": ..., "video": ..., "audio": ..., "other": ...},
          "lost_grace_sec": ...,
          "note": 中文说明（默认行为的解释）
        }
    """
    by_media = {}
    for media in (job_timeout.MEDIA_IMAGE, job_timeout.MEDIA_VIDEO,
                  job_timeout.MEDIA_AUDIO, job_timeout.MEDIA_OTHER):
        by_media[media] = timeout_sec_for_media(media)
    return json_ok({
        "image_timeout_sec": RUNNING_TIMEOUT_SEC,
        "video_timeout_sec": RUNNING_TIMEOUT_VIDEO_SEC,
        "by_media": by_media,
        "lost_grace_sec": RUNNING_LOST_GRACE_SEC,
        "note": ("视频类任务默认**不按运行时长判失败**（跑 1 天也仍然是「进行中」）；"
                 "只有当任务从 ComfyUI 队列与历史中消失时才会判定失败。"
                 "图像 / 音频类保留运行上限，可在 config.json 里用 "
                 "running_timeout_sec / running_timeout_video_sec 调整。"),
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


async def _h3_image_options_payload() -> dict:
    """H3-Image 的前端可选项清单（含**实测**的本机可用性）。

    【为什么要实测而不是写死】
    H3-Image 要跑起来需要两样东西同时就位：
      1. 本机 ComfyUI 的核心节点 MiniMaxH3ImageConditioning
         （该节点需要 ComfyUI >= 0.45.0）
      2. 3 个权重文件（DiT 21GB / Qwen3-VL-32B 27GB / Image VAE 1.17GB）
         且**必须下完**（半截文件会被 safetensors 头检查漏判）

    任一不满足时，前端把该模型标成「不可用」并显示**具体缺什么**，
    而不是让用户选完才在跑图时炸掉、或静默失败。

    【探测失败 ≠ 缺节点】
    ComfyUI 忙的时候探测会超时。此时返回 probe_complete=False，
    前端显示「暂时无法探测本机 ComfyUI 能力」而不是断言「缺节点」。
    """
    nodes_available, probe_complete = await probe_h3_image_nodes()
    # 【没问全时传 None，而不是 False】
    # False = 确认节点不在（可以断言「本机 ComfyUI 版本过低」）；
    # None  = 探测超时、不知道（界面只能说「暂时无法探测」）。
    # 权重探测是纯磁盘操作，不受 ComfyUI 忙闲影响 —— 照样如实报告。
    node_state = (nodes_available if probe_complete else None)
    models = {}
    for key, m in h3img.H3_IMAGE_MODELS.items():
        missing = h3img.missing_weights(m)
        weights_available = not missing
        cap = h3img.h3_image_capability(node_state, weights_available,
                                        missing)
        models[key] = {
            "label": m["label"],
            "unet": m["unet"],
            "clip": m["clip"],
            "vae": m["vae"],
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
            # 实测可用性（前端据此置灰 + 显示缺失原因）
            "available": cap["available"],
            "nodes_available": cap["nodes_available"],
            "weights_available": cap["weights_available"],
            "missing": cap["missing"],
            # 【锁定步数/CFG/采样器/调度器】
            # H3-Image 的 guidance **固定为 1**（官方 6 步 PDD 蒸馏，
            # 没有 CFG pass），步数与 PDD sigma 表绑定 —— 这两个参数
            # 都不是「用户可调」的。
            #
            # 不锁的后果：用户把 CFG 拖到 8 -> ComfyUI 在 cfg>1 时会跑
            # uncond 分支（负分支是 ConditioningZeroOut 零条件）
            # -> 出图质量劣化，但界面零提示（Z-Image 踩过的同一个坑）。
            #
            # 复用前端已有的「params_locked_by_accel」机制 ——
            # 那个开关本来是给「加速 LoRA 绑定的蒸馏档」用的，
            # 语义完全一致（参数与模型内部 schedule 绑定，不能改）。
            "params_locked_by_accel": True,
        }
    tiers = []
    for key, megapixels in h3img.H3_IMAGE_RES_TIERS.items():
        tiers.append({
            "key": key,
            "label": {"4mp": "4MP（官方模板默认）", "8mp": "8MP",
                      "16mp": "16MP（原生直出上限）"}.get(key, key.upper()),
            "megapixels": megapixels,
            "max_direct_mp": h3img.H3_IMAGE_MAX_DIRECT_MP,
        })
    return {
        "tiers": tiers,
        "aspects": [{"key": k, "label": l, "ratio": list(ASPECT_NUM[k])}
                    for k, l in ASPECT_MAP.items()],
        "models": models,
        "size_multiple": h3img.H3_IMAGE_SIZE_MULTIPLE,
        "max_direct_mp": h3img.H3_IMAGE_MAX_DIRECT_MP,
        "min_mp": h3img.H3_IMAGE_MIN_MP,
        "default_tier": h3img.H3_IMAGE_DEFAULT_TIER,
        "default_model": h3img.H3_IMAGE_DEFAULT_MODEL,
        # 参考图上限（官方能力 16 张；官方占位标记是 <Picture N>）
        "max_ref_images": h3img.H3_IMAGE_MAX_REF_IMAGES,
        "min_ref_images": h3img.H3_IMAGE_MIN_REF_IMAGES,
        "placeholder_example": h3img.H3_IMAGE_PLACEHOLDER,
        # LoRA：官方暂未发布 H3-Image 的 LoRA —— 界面留位但明确说明
        "lora_supported": False,
        "lora_note": "MiniMax-H3-Image 官方暂未发布 LoRA，"
                     "本面板已预留 LoRA 入口但暂无可选权重。",
        # 本次探测是否问全（False = ComfyUI 忙/不可达，不能断言缺节点）
        "probe_complete": probe_complete,
        # 官方原生 16MP 各比例尺寸（界面提示用）
        "native_sizes": {k: list(v) for k, v in h3img.H3_IMAGE_NATIVE_SIZES.items()},
    }


async def api_prompt_catalog(request):
    """GET /api/prompt-catalog —— 「创意内容设计」选项目录。

    【为什么走服务端下发】既有纪律：所有「清单类」数据都由服务端给
    （/api/image-options、/api/image-loras、/api/enhancer、/api/docs/flows）。
    前端不硬编码清单 ⇒ 版本可控、可单测、增量更新不用重发前端。

    【响应结构】
      version        目录内容 sha256 前 12 位（内容变了就变）
      slots          拼装骨架顺序（主体 → 造型 → 风格 → 场景 → 构图 → 光 → 色 → 氛围 → 质量 → 负向）
      dimensions[]   维度 → groups[]（子类）→ items[]（选项）
      conflicts[]    冲突矩阵（hard 硬互斥 / soft 软冲突 / cap 数量上限）
      counts         规模统计（前端可以显示「共 N 项」）

    【冲突规则里的 id 是真实选项 id】—— 前端据此禁用/提示，
    规则引用了已删项会在服务端构建期直接报错，不会静默失效。
    """
    manifest = prompt_catalog.get_manifest()
    return json_ok(manifest)


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
        # 【2026-11-16 修 —— 采样参数量程下发前端】
        # 面板滑杆的 min/max/step 从这里取，**不再在前端硬编码**。
        #
        # 【为什么必须下发 —— 这就是本次用户报的 BUG 的根因】
        # 旧实现前端按模式分支硬编码量程（MOSS steps 1~100），
        # 后端却用一套扁平的全局上限（steps<=60）去判「越界 -> 回落默认值」。
        # 于是用户拖到 100 提交后被静默夹回 50：面板显示 100、实际跑 50。
        # 量程只有一份真源（gen_normalize.IMAGE_PARAM_RANGES），
        # 前后端都读它 —— 这类「前端允许、后端不允许」的断链就不可能再发生。
        "param_ranges": gen_normalize.IMAGE_PARAM_RANGES,
        # MiniMax-H3-Image 独立的一套（文生图 + 图片编辑共用）
        "h3image": await _h3_image_options_payload(),
        # 【功能位】前端 web/app.js 的 loadImageOptions() 读 data.features
        # 来判断「后端是否已支持某族」—— 之前这个字段只在 /api/status 里，
        # 两处不同源 -> 前端拿到的永远是旧值。
        "features": SERVER_FEATURES,
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
    # 【2026-11-21 性能修】payload 的构建走**工作线程**。
    #
    # 【为什么必须挪出事件循环 —— 实测 2026-11-21】
    #   `options_payload()` 内部要算 7 底模 × 12 LoRA = 84 组兼容性，
    #   每组要读 safetensors 头 + 逐层比对，还要做 264 次 `find_weight`
    #   （135 次落到 USB 盘的 rglob 慢路径）。实测单次 **511~800ms**。
    #   这段代码原本**直接跑在 aiohttp 事件循环里** ——
    #   一占住，**整个 8190 服务的所有请求排队**。
    #
    #   实测证据（线上 8190，2026-11-21）：
    #     * 浏览器首屏并发拉 25 个接口时，`/api/jobs`（自己只要 4.7ms）
    #       被拖到 **707ms**；
    #     * 单独压测 `/api/video-options` 时，`/api/jobs` 的 p50
    #       从 5.8ms 劣化到 **1279ms（220 倍）**；
    #     * py-spy 抓到 aiohttp 的 MainThread（= 事件循环）栈：
    #       `accel_lora_compat_matrix -> detect_accel_lora_loader ->
    #        find_weight -> select_recursive_step`（同步读盘）。
    #   ⇒ 用户报的「生成媒体库卡片有时候载入很慢很慢」就是这个。
    #
    # 【为什么用线程而不是改成异步】
    #   底下是 `open()` / `Path.stat()` / `rglob()` —— **同步阻塞 IO**，
    #   改成 async 只是换个写法，不能让它不占线程。
    #   `asyncio.to_thread` 让它跑在工作线程，事件循环继续服务别的请求。
    #
    # 【为什么还加了缓存（video_models 侧）】
    #   线程化解决的是「不阻塞别人」，缓存解决的是「自己别那么慢」。
    #   两者都要：缓存后单次 ~25ms，线程化保证即使缓存未命中
    #   （冷启动 / TTL 过期）也不会冻结全站。
    payload = await asyncio.to_thread(
        video_models.options_payload,
        node_available=node_available,
        snapshot=snapshot,
    )
    # 附带「当前底模下每个加速 LoRA 的实测兼容性」—— 前端据此把对不上的
    # 从**下拉里剔除**（不是置灰）。
    #
    # 【2026-11-14 重构】加速 LoRA 统一成一组可选项（不再分「固定枚 /
    # 组合枚」两栏）。每一项都带 loader（用哪个节点加载）——
    # 前端在提示里显示它，用户能确认「这枚真的挂上了、用的哪个节点」。
    model_key = request.query.get("model") or payload["default_model"]
    model = video_models.VIDEO_MODELS.get(model_key)
    payload["selected_model"] = model_key
    if model:
        # 【2026-11-21 性能修】同样走线程 —— 缓存未命中时这里要重算
        # 84 组兼容性里的 12 组，读盘仍会占住事件循环。
        matrix = await asyncio.to_thread(
            video_models.accel_lora_compat_matrix, model_key)
        accels = []
        for item in payload.get("accel_loras") or []:
            entry = dict(item)
            check = matrix.get(item["key"], {})
            entry["compatible"] = bool(check.get("compatible"))
            entry["compat_reason"] = check.get("reason", "")
            entry["compat_matched"] = check.get("matched", 0)
            entry["compat_runtime_injected"] = check.get("runtime_injected", 0)
            entry["compat_shape_mismatch"] = check.get("shape_mismatch", 0)
            entry["compat_total"] = check.get("total", 0)
            # 【2026-11-15】把**实际会用的加载节点**一起下发 ——
            # 前端提示里要显示它（用错节点 = 静默失效），
            # 而「AdaLN 能否运行期重注入」也取决于它。
            entry["loader"] = check.get("loader") or entry.get("loader", "")
            # 不适配当前底模的**不出现**在下拉里（前端据此过滤）
            entry["selectable"] = bool(entry["compatible"]) and \
                entry.get("available", True)
            accels.append(entry)
        payload["accel_loras"] = accels
        payload["fixed_accel_loras"] = accels
        payload["accel_lora_combos"] = []

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


async def api_video_effect_loras(request):
    """GET /api/video-effect-loras —— 本机真实的 H3 效果 LoRA 清单

    【为什么单独一个接口，不复用 /api/image-loras】
    `/api/image-loras` 是**图像**基座（Z-Image / Qwen-Image / MOSS）的效果
    LoRA 清单：它的兼容性判据是「与图像底模的层结构比对」，白名单也来自
    `loras.LORA_CATALOG`。

    H3 是**视频**基座，层结构完全不同（`blocks.N.attn.qkv_proj` vs
    `layers.N.attention.to_q`），且加载节点也不同（核心
    `LoraLoaderModelOnly` vs `LoraLoader`）。硬塞进图像接口会让：
      * 图像清单混进 H3 条目（键结构不同，必然静默失效）
      * H3 条目混进图像清单（同样静默失效）
    所以**分开两个接口，各自按自己的底模实测**。

    【数据来源 —— 真实扫盘，不硬编码】
    `h3_effect_loras.discover_effect_loras()`：
      1. 扫 ComfyUI 的 loras 目录（本地 + extra_model_paths.yaml 的外接盘）
      2. 读每个 safetensors 的头，判断键结构
      3. 用 ComfyUI 自己的 key_map 规则判定「能不能绑到 H3 底模」
      4. 形状（in/out 维）与底模权重逐一比对
    每条都带 `compatible` / `compatible_reason`，前端据此过滤。

    可选参数 `?model=<model_key>` 指定按哪个底模测兼容性（默认剪枝 int8）。
    """
    from h3studio import h3_effect_loras as effect_registry
    from h3studio import h3_effect_lora_docs as doc_parser

    model_key = (request.query.get("model")
                 or video_models.VIDEO_DEFAULT_MODEL)
    model = video_models.VIDEO_MODELS.get(model_key)
    if model is None:
        return web.json_response(
            {"error": f"未知的视频模型 {model_key}"}, status=400)

    entries = effect_registry.discover_effect_loras(model_key)
    unet_path = video_models.find_weight(model["unet"])
    available: list[dict] = []
    incompatible: list[dict] = []
    for entry in entries:
        path = Path(entry["path"])
        check = effect_registry.check_effect_lora_compat(path, unet_path)
        doc = doc_parser.read_lora_doc(path)
        item = {
            "name": entry["name"],
            "path": entry["path"],
            "display": doc.get("display") or entry["display"],
            "size_mb": entry["size_mb"],
            "key_format": entry["key_format"],
            "modules": entry["modules"],
            "bound_modules": entry["bound_modules"],
            "rank": entry["rank"],
            "directory": entry["directory"],
            "base_model": entry["base_model"],
            # 作者文档里解析出来的元信息（解析不到就是空 —— 不编造）
            "scene": doc.get("scene", ""),
            "trigger": doc.get("trigger", ""),
            "recommended_strength": doc.get("recommended_strength", 0.0),
            "strength_source": doc.get("strength_source", ""),
            "author": doc.get("author", ""),
            "source": doc.get("source", ""),
            "has_doc": doc.get("has_doc", False),
            # 实测兼容性
            "compatible": bool(check.get("compatible")),
            "compatible_reason": check.get("reason", ""),
            "shape_ok": check.get("shape_ok", 0),
            "shape_mismatch": check.get("shape_mismatch", 0),
            "missing": check.get("missing", 0),
            "adaln_spin": check.get("adaln_spin", 0),
            "total": check.get("total", 0),
            # 强度上限：默认 1.5（与图像面板一致）；作者给了推荐值就以它为准
            "strength_cap": 1.5,
        }
        if item["recommended_strength"]:
            item["strength_cap"] = max(1.5,
                                       float(item["recommended_strength"]))
        (available if item["compatible"] else incompatible).append(item)

    return json_ok({
        "model": model_key,
        "model_label": model.get("label", ""),
        "unet": model.get("unet", ""),
        "available": available,
        "incompatible": incompatible,
        # 节点说明：效果 LoRA 走核心节点，**不是** Turbo 节点
        "loader_node": "LoraLoaderModelOnly",
        "loader_note": (
            "效果 LoRA 走 ComfyUI 核心节点 LoraLoaderModelOnly —— "
            "它的键名带 diffusion_model. 前缀，MiniMaxH3TurboLoRA 节点对它"
            "会「0 weights patched」静默失效（实测）。"),
        "source_note": (
            "清单来自扫盘 + safetensors 头实测（键绑定 + 形状比对），"
            "不是硬编码列表；作者说明与触发词来自同名 .md 文档。"),
    })


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

    # 【2026-11-14 修 —— 效果 LoRA 结构校验与提交链路保持一致】
    #
    # 【为什么】任务提交链路（normalize_t2v_params）现在会对
    # 「非法结构的 effect_loras」明确 400（原来静默丢弃）。
    # 但配置保存链路（PUT /api/video-config）原来**不校验**，
    # 什么都能存进去 —— 于是：
    #   * 前端存了非法结构 -> GET 读回来还是非法 -> 面板永远空白；
    #   * 用户在提交时被 400 拦住，却**看不到**配置里存的是什么，
    #     无法自查。
    # 两边行为必须一致：**在入口就拒绝非法结构 + 给出中文原因**。
    #
    # 【校验口径与提交链路一致】
    #   * 顶层不是数组            -> 400
    #   * 某项不是对象            -> 400（指出第几项）
    #   * name 为空 / enabled=false -> **合法**（用户清空/停用），跳过
    #   * 文件不存在 / 强度非数字   -> 这里**不**查（那是提交时的闸门），
    #     因为配置保存是「先存下来」，真正的效果校验在生成前。
    raw_effect = body.get("effect_loras")
    if raw_effect is not None:
        if not isinstance(raw_effect, list):
            return web.json_response({
                "error": (f"效果 LoRA 列表必须是数组（每项是对象），"
                          f"收到的是 {type(raw_effect).__name__}：{raw_effect!r}")},
                status=400)
        for index, item in enumerate(raw_effect):
            if not isinstance(item, dict):
                return web.json_response({
                    "error": (f"效果 LoRA 第 {index + 1} 项必须是对象"
                              f"（含 name / strength / enabled / trigger），"
                              f"收到的是 {type(item).__name__}：{item!r}")},
                    status=400)

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
      * mode=flf / r2v  -> 与 t2v 同为 2.0MP（2026-11-17 对齐 nonebot seg）
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
    h3image = lora_registry.is_h3image_base(base)
    # MOSS 分支的扫描快照 —— 下面 `if moss:` 处复用它（避免二次扫描）。
    # 显式初始化，让静态检查器看到「所有路径都已定义」。
    moss_inventory: dict = {}
    if h3image:
        # 【MiniMax-H3-Image：官方暂未发布该模型的 LoRA】
        # 返回**空清单**（而不是掉进 else 分支拿 Z-Image 族的清单 ——
        # 那样用户能在 H3-Image 面板选到 Z-Image 的 LoRA：
        # 底模结构不同，ComfyUI 会静默丢弃 = 选了等于没选）。
        available = []
        incompatible = []
        acceleration = []
    elif moss:
        # 【2026-11-14 需求 15】MOSS-Image 的「已排除」清单**不再下发**。
        #
        # 【为什么不兼容】这批 LoRA（Realistic Snapshot / Kodachrome /
        # Kodachrome V2 / Skin tone slider [MOSS | Krea 2]）的融合 qkv
        # 部分是 LoKR（Kronecker）权重，无法无损拆成 MOSS 底模需要的
        # 分离 q/k/v（Kronecker 分解的行映射与三等分不对应，实测数值
        # 不等价）。也就是说它们在 MOSS-Image 上**根本不可能生效**。
        #
        # 【为什么不只是前端隐藏】「已排除」面板把一批永远用不了的
        # 权重摆在用户面前，占界面、还要用户理解「为什么被排除」——
        # 但答案永远是「用不了」，没有可操作性。接口层直接不下发，
        # 前端也就无从展示（两端口径一致，不会出现「接口说有、
        # 界面没有」的漂移）。
        #
        # 【对已保存配置的影响】下面 filter 掉 config 里指向这批
        # LoRA 的条目 —— 否则它们会以「已排除」条目留在用户已选列表里，
        # 仍然出现在界面上（且提交时会被服务端拒绝）。
        incompatible = []
        # MOSS 分支同样要「一次扫描出三份清单」—— 旧代码这里调了两次
        # list_moss_image_loras()（available + acceleration），每次都是全量
        # 目录扫描。合并成一次，并走线程（见 else 分支的完整说明）。
        moss_inventory = await asyncio.to_thread(
            lora_registry.lora_inventory_by_kind_moss)
        available = [item for item in moss_inventory["effect"]
                     if item.get("compatible", True)]
        acceleration = moss_inventory["acceleration"]
    elif qwen:
        # ⚠️ 走线程：全量目录扫描是同步阻塞 IO，跑在事件循环里会冻结全站
        # （见 else 分支的完整说明 —— 2026-11-16 线上 8190 不可访问根因）。
        inventory = await asyncio.to_thread(
            lora_registry.lora_inventory_by_kind_qwen)
        available = [item for item in inventory["effect"]
                     if item.get("compatible", True)]
        incompatible = inventory["incompatible"]
        acceleration = inventory["acceleration"]
    else:
        # ⚠️ **必须放在线程里跑**（2026-11-16 线上 8190 不可访问的根因修复）
        #
        # 旧代码在这里同步调三个扫描函数：
        #     available     = list_effect_loras()
        #     incompatible = list_incompatible_loras()
        #     acceleration = list_acceleration_loras()
        # 三个函数各自触发**一次全量目录扫描**（rglob 700+ 文件 + 465 次
        # 同步读 safetensors 头部），且**直接跑在 asyncio 事件循环里**。
        # LoRA 目录挂在 USB NTFS(fuseblk) 上，冷缓存时单次 stat 就要几十毫秒
        # —— 事件循环被冻结 3~150 秒，同期所有 HTTP 请求（含首页 /）全部挂起，
        # 表现为「端口在监听但页面打不开」。
        #
        # 修复两件事：
        #   1. `asyncio.to_thread` —— 扫描不占事件循环，首页随时可响应
        #   2. `lora_inventory_by_kind()` —— 三份清单来自**同一次**扫描，
        #      消掉 3 倍重复 IO，且三份列表互相自洽
        inventory = await asyncio.to_thread(lora_registry.lora_inventory_by_kind)
        available = [item for item in inventory["effect"]
                     if item.get("compatible", True)]
        incompatible = inventory["incompatible"]
        acceleration = inventory["acceleration"]
    with db() as conn:
        config = lora_registry.load_config(conn, lora_registry.pref_key_for_base(base))
    # 【2026-11-14 需求 15】MOSS-Image：把「已排除」批次从**已保存配置**里
    # 也一并去掉（见上面那段注释）。
    #
    # 【为什么要额外做这一步】incompatible 列表不下发之后，
    # excluded_by_name 查不到这批 LoRA -> 它们会被判成
    # 「missing = 文件不在了」，然后仍然出现在用户的已选列表里
    # （带一个「文件缺失」标签）。但文件其实**在** —— 只是不兼容。
    # 直接从 config 里剔除，才是真的「不再展示」。
    if moss:
        # 直接复用上面那次扫描的 incompatible 清单 —— 旧代码这里又调了
        # 一次 list_moss_incompatible_loras()（= 又一次全量目录扫描，
        # 同步阻塞事件循环）。改成复用同一份快照，扫描次数从 2 次降到 1 次。
        #
        # `moss_inventory` 在 moss 分支一定已赋值（见上），这里给个空默认
        # 只是为了让静态检查器看到「所有路径都已定义」。
        excluded_names = {item["name"]
                          for item in (moss_inventory or {}).get("incompatible", [])}
        if excluded_names:
            config = [item for item in config
                      if (item.get("name") or "") not in excluded_names]
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
    # ⚠️ 走线程 + 单次扫描（同 GET 分支的理由：2026-11-16 线上 8190
    # 不可访问的根因 —— 全量目录扫描是同步阻塞 IO，跑在事件循环里会冻结全站）。
    #
    # 这里只需要「效果类全部条目（含不兼容）」，所以单次扫描后取 effect 即可。
    if moss:
        all_effect = (await asyncio.to_thread(
            lora_registry.lora_inventory_by_kind_moss))["effect"]
    elif qwen:
        all_effect = (await asyncio.to_thread(
            lora_registry.lora_inventory_by_kind_qwen))["effect"]
    else:
        all_effect = (await asyncio.to_thread(
            lora_registry.lora_inventory_by_kind))["effect"]
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


def _safe_int(value, default: int = 0) -> int:
    """把任意外部输入安全地转成 int（非法值回落 default）。

    用于 `/api/enhance` 的 `stage` 字段 —— 那是**前端传来的**，
    可能是 None / 字符串 / 浮点，任何一种都不能让增强请求崩掉。
    """
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_prompt_history(raw, current_stage: int = 0) -> list[dict]:
    """规整前端传来的跨段提示词历史。

    输入（前端从接续上下文原样透传）：
      [{"stage": int, "prompt": str}, ...]  或  ["提示词", ...]

    输出：`[{"stage": int, "prompt": str}, ...]`，已剔除**本段自己**
    （stage == current_stage）、空提示词、脏数据。

    【为什么剔除本段】`original` 已经是本段原文了。本段再进历史，
    模型会看到两遍同一段，容易把「本段」误当成「前情」，
    或者把两段内容糊成一段来扩写。

    【为什么容错】前端是外部输入：字段缺失、类型不对、字符串数组
    都可能出现。**不能**因为历史格式脏就把整个增强请求搞挂 ——
    历史只是**增强质量**的加分项，不是必需项。
    任何异常都退回空历史（行为退化成单段增强，与今天一致）。
    """
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        stage = None
        explicit_stage = False
        text = ""
        if isinstance(item, dict):
            text = str(item.get("prompt") or "").strip()
            try:
                stage = int(item.get("stage"))
                explicit_stage = True
            except (TypeError, ValueError):
                stage = None
        elif isinstance(item, str):
            # 容错：前端若只传了字符串数组，就按顺序当作段号递增。
            # 【注意】这种形态**没有**可信段号 -> 不套用「剔除本段」
            # 规则（套了会把第一条误删）。宁可多带一条上下文，
            # 也不能把整份历史丢掉。
            text = item.strip()
        if not text:
            continue
        if stage is None:
            stage = len(out)
        if explicit_stage and stage == current_stage:
            continue
        out.append({"stage": stage, "prompt": text})
    # 按段号排序 —— 前端若乱序传，也要保证「前情在前」。
    out.sort(key=lambda item: item["stage"])
    return out


async def _resolve_enhance_assets(asset_refs):
    """
    把「面板当前引用的素材」解析成**服务端可读**的素材上下文。

    【为什么必须由服务端解析、而不是让前端传路径】
      * 浏览器本地路径服务端**根本读不到** —— 所以服务端的 `images`
        参数（本地图片路径列表）实际是条死路，前端也从来没有用过它；
      * 素材库里的素材服务端**本来就读得到**（h3studio/assets.py 管着
        物理文件）。前端只传 asset_id，服务端自己解析 —— 这也顺带保证
        「只接受素材库里的素材」，不引入任意文件读取。

    【为什么丢进线程】素材解析要读素材库（SQLite）+ 视频要抽帧
    （ffmpeg 子进程）—— 都是阻塞操作，直接在事件循环里跑会把
    整个 Studio 卡住（8190 是单机单进程，事件循环一卡全站无响应）。

    返回 h3studio/enhance_assets.resolve_assets 的结果 dict。
    素材不可用时抛 EnhanceAssetError（调用方据此如实报错）。
    """
    return await asyncio.to_thread(enh_assets.resolve_assets, asset_refs)


async def api_enhance(request):
    """
    POST /api/enhance —— 提示词增强（迷你 Agent 调用本地模型）

    body:
      prompt      要扩写的原文
      mode        replace | append（落地策略，默认 replace）
      spec_id     模板 id，默认 image
      provider_id 指定 Provider；不给则用默认（要读图则用默认多模态）
      images      可选，本地图片路径列表（需要读图的场景）
      asset_refs  可选，**面板当前引用的素材列表**（素材库 asset_id）
                  [{"asset_id": "asset_xxx", "slot": "ref_image"}, ...]
                  服务端解析成服务端可读的图片（图片直给 / 视频抽帧），
                  连同图序语义一起喂给多模态模型。
                  【2026-11-18 修复】此前前端从不传素材，增强模型**看不到**
                  用户引用的素材，扩写结果与素材无关 —— 素材等于白引用。
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

    # ---- 素材上下文（2026-11-18 新增）----
    # 前端把「当前面板已引用的素材」以 asset_refs 传上来，
    # 这里解析成**服务端可读**的图片路径 + 图序语义。
    asset_refs = payload.get("asset_refs")
    asset_context = ""
    asset_summary = {}
    if asset_refs:
        try:
            resolved_assets = await _resolve_enhance_assets(asset_refs)
        except enh_assets.EnhanceAssetError as exc:
            # 素材用不了 -> **如实报错**，不静默忽略
            # （静默忽略 = 用户以为素材参与了增强，其实没有）。
            return web.json_response(
                {"error": exc.message, "code": exc.code, "degraded": True},
                status=400)
        except Exception as exc:
            return web.json_response(
                {"error": f"素材上下文解析失败：{type(exc).__name__} {exc}"[:300],
                 "degraded": True}, status=500)
        # 素材图片**追加**在显式 images 之后（图序语义按实际顺序编号）
        for path in (resolved_assets.get("images") or []):
            if len(images) >= enh_assets.MAX_IMAGES:
                break
            images.append(path)
        asset_context = enh_assets.describe(resolved_assets)
        asset_summary = enh_assets.summary(resolved_assets)

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
            # 【2026-11-18 新增】显式指定了 Provider，而这次增强**带素材** ——
            # 若该 Provider 不是多模态，llm.py 会抛「无法读图」。
            # 但那已经太晚了（用户等了一轮才发现）。这里**提前**给明确提示，
            # 说明「素材没有参与本次增强」而不是让它静默丢失。
            if images and not item.get("multimodal"):
                return web.json_response(
                    {"error": f"Provider「{item['name']}」未开启多模态能力，"
                              "无法读取素材，本次增强不会使用已引用的素材。"
                              "请在「本地模型」页勾选「多模态」，或取消指定 Provider "
                              "改用默认多模态模型。",
                     "code": "provider_not_multimodal",
                     "provider": item["name"], "provider_id": item["id"],
                     "degraded": True}, status=400)
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
    # ---- 跨段提示词历史（2026-11-17 新增）----
    #
    # 接续生成时，用户要求「把此前所有段落的提示词一并提供给增强模型」。
    # 前端把接续上下文里的 `prompt_history` 传上来，服务端在这里
    # **剔除本段自己**（stage == 当前段号）——只把「更早的段」当上下文。
    #
    # 【为什么剔除本段】`original` 已经是本段原文了；把本段再塞进
    # 历史会让模型看到两遍同一段，容易把「本段」误当成「前情」。
    history = _normalize_prompt_history(payload.get("prompt_history"),
                                         current_stage=_safe_int(
                                             payload.get("stage")))
    messages = enh.build_messages(spec, original, params.get("language", "auto"),
                                   history=history,
                                   asset_context=asset_context)

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
        # 【JSON 契约解包出来的画面比例】（2026-11-15 修复①）
        # 在循环外初始化：attempts 至少有一次，但保持「一定已绑定」更稳。
        pe_ratio = ""
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
            # 【JSON 契约解包（2026-11-15 修复①）】
            # H3-Image 官方 PE 的 task prompt 要求模型输出一个两键 JSON
            # （expanded_caption + 比例键），而写回提示词框要的是**纯文本**。
            # 这里解包：caption 进提示词框，比例单独回给调用方。
            # 非 JSON 契约的模板原样通过（不动其它面板的行为）。
            enhanced, pe_ratio = enh.unwrap_json_contract(enhanced, spec_id)
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
        # 【素材上下文回显（2026-11-18 新增）】前端据此告诉用户
        # 「这次增强带上了哪几个素材」—— 否则用户无法确认素材到底
        # 有没有参与（这正是本次缺陷的核心：素材被静默丢弃）。
        "assets": asset_summary,
        "validation": validation,
        "elements": elements,
        # 【JSON 契约解包出来的画面比例】（2026-11-15 修复①）
        # H3-Image 官方 PE 会自己选比例；解包后单独回给前端，
        # 前端可据此提示用户「PE 建议的比例是 X」。
        # 非 JSON 契约模板恒为空串。
        "pe_aspect_ratio": pe_ratio or "",
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


async def api_h3image_pe_expand(request):
    """
    POST /api/h3image-pe/expand —— MiniMax H3-Image 自带 PE 提示词扩写

    【需求 16】H3-Image 此前**没有**接入提示词扩写（PE）——
    官方模板里本来就有 PE（TextGenerateV2 + qwen3.5_9b_w4a8 + 官方
    system prompt），但它一直在出图链路内部，前端看不到、Agent 也没法用。

    body:
      prompt      要扩写的原始提示词
      mode        t2i_h3image（文生图）| i2i_h3image（图片编辑）
      refs        可选，图片编辑的参考图（素材库条目列表）；顺序即 <imageN> 顺序
      task_prompt 可选，用户编辑过的官方 PE task prompt

    返回:
      expanded      扩写后的提示词（前端写回提示词框，可继续编辑）
      aspect_ratio  PE 选出的画面比例（官方 PE 会自己选比例）
      clip_name     实际用的官方 PE checkpoint 文件名
      ref_count     参考图数量
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

    mode = str(payload.get("mode") or "t2i_h3image").strip()
    if mode not in ("t2i_h3image", "i2i_h3image"):
        return web.json_response(
            {"error": "H3-Image 官方 PE 扩写只适用于文生图 / 图片编辑模式"}, status=400)
    is_edit = mode == "i2i_h3image"

    # 参考图：素材库来源，顺序严格保持（= <imageN> 顺序）。
    # 只上传、**不登记** asset_refs —— 扩写只是读一次图，不是出图任务。
    ref_images: list[str] = []
    raw_refs = payload.get("refs") or []
    if is_edit and raw_refs:
        try:
            async with ClientSession(timeout=HTTP_TIMEOUT) as session:
                ref_images = await _upload_ref_images(session, raw_refs)
        except Exception as exc:
            return web.json_response(
                {"error": f"参考图上传失败：{exc}"[:300], "degraded": True}, status=502)

    task_prompt = payload.get("task_prompt")
    if task_prompt is not None:
        task_prompt = str(task_prompt)
        if not task_prompt.strip():
            return web.json_response(
                {"error": "官方 PE 的 task prompt 不能为空"}, status=400)

    try:
        async with ClientSession(timeout=ClientTimeout(total=HTTP_TIMEOUT.total)) as session:
            result = await h3image_pe.run_pe_expansion(
                session, COMFY, original, ref_images or None,
                task_prompt=task_prompt, is_edit=is_edit)
    except h3image_pe.PEError as exc:
        return web.json_response(
            {"error": str(exc), "degraded": True}, status=502)
    except Exception as exc:
        return web.json_response(
            {"error": f"H3-Image 官方 PE 扩写失败：{type(exc).__name__} {exc}"[:300],
             "degraded": True}, status=502)

    # 【提示词历史】官方 PE 的扩写结果也进历史
    with db() as conn:
        ph_hooks.record_prompt_history(
            conn, mode,
            {"original_prompt": original, "prompt": result["expanded"]},
            source=ph.SOURCE_ENHANCE)
    return json_ok({
        "ok": True,
        "original": original,
        "expanded": result["expanded"],
        "aspect_ratio": result.get("aspect_ratio", ""),
        "clip_name": result["clip_name"],
        "ref_count": result["ref_count"],
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


def _resolve_max_steps(requested=None) -> int:
    """
    本次请求的步数上限。

    【2026-11-13 审查目标 4】口径：
      * 「agent 管理」里配置的 max_steps 是**默认值也是上限**；
      * 请求里带 max_steps -> 以请求为准，但仍然被夹在配置上限内；
      * 没配置 -> 回落到 agent_mod.MAX_STEPS（50）。
    """
    with db() as conn:
        settings = agent_settings.load(conn)
    ceiling = int(settings.get("max_steps") or agent_mod.MAX_STEPS)
    ceiling = max(agent_settings.MAX_STEPS_MIN,
                  min(agent_settings.MAX_STEPS_HARD_CAP, ceiling))
    if requested is None or requested == "":
        return ceiling
    try:
        value = int(requested)
    except (TypeError, ValueError):
        return ceiling
    return max(agent_settings.MAX_STEPS_MIN, min(ceiling, value))


def _resolve_total_timeout() -> float:
    """本次请求的总时限（秒）。

    【2026-11-14 需求 6】「agent 管理」页的「总时限」现在是**可编辑**
    的 —— 以前它是 agent 模块里写死的常量，界面上灰着改不了。
    这里从持久化配置里读出来，交给 run_agent 作为 asyncio.wait_for
    的超时值 —— 改了真的生效，不只是显示变了。

    读不到 / 坏了 -> 回落到模块默认值（行为与改动前一致）。
    """
    try:
        with db() as conn:
            settings = agent_settings.load(conn)
    except Exception:
        return float(agent_mod.TOTAL_TIMEOUT)
    try:
        raw_value = settings.get("total_timeout")
        value = int(raw_value) if raw_value is not None else int(agent_mod.TOTAL_TIMEOUT)
    except (TypeError, ValueError):
        return float(agent_mod.TOTAL_TIMEOUT)
    return float(max(agent_settings.TOTAL_TIMEOUT_MIN,
                     min(agent_settings.TOTAL_TIMEOUT_MAX, value)))


def _resolution_tier_megapixels(tier: str, aspect: str = "16:9") -> float | None:
    """
    短边档位标签 -> 总像素 MP（**按指定比例**反查）。

    【为什么需要（2026-11-20）】Agent 路径要用**用户传的档位**换算宽高，
    但视频的档位名是**短边像素**（480p / 768p / 1088p ...），
    不是 MP 数（0.4 / 0.8 / 0.98 ...）——
    ``workflows.RES_MP`` 那张表只有旧面板的两个键（544p / 768p），
    覆盖不了真实档位集。

    【为什么必须结合 aspect —— 实测数据】
    同一个短边在不同比例下对应**不同**的 MP：
      * 480p：16:9 -> 0.4MP（864x480）、4:3 -> 0.3MP（640x480）、21:9 -> 0.5MP
      * 768p：16:9 -> 0.98MP（1344x768）
      * 1088p：16:9 -> 2.0MP（1920x1088）
    而且**档位集合本身按比例不同**：
      * 4:3 的预设里**没有** 768p（有 672p/736p/800p/832p/864p...）
      * 21:9 的预设里**没有** 1440p（最高 896p）
    所以「用户说 768p + 4:3」在**该比例下没有这个档位**。

    【查不到时怎么办 —— 不跨比例兜底】
    跨比例兜底会让「768p + 4:3」静默变成 864p（实测）——
    那是**静默改写用户请求**，正是本任务要消灭的东西。
    查不到就返回 None，由调用方**明确报错**（说明该比例下的真实档位集）。
    """
    text = str(tier or "").strip()
    if not text:
        return None
    if text.endswith("p"):
        text = text[:-1]
    if not text.isdigit():
        return None
    short = int(text)
    preferred = str(aspect or "").strip()
    for preset in video_models.size_presets_for_aspect(preferred or "16:9"):
        if int(preset.get("short_edge") or 0) == short:
            return float(preset.get("megapixels") or 0.0) or None
    # 该比例下没有这个档位 -> 不跨比例兜底（会静默改写用户请求）
    return None


def _merge_t2v_defaults(params: dict, defaults: dict) -> dict:
    """
    把「agent 管理」里的文生视频默认参数套进 params（**不覆盖已给的**）。

    【2026-11-13 审查目标 5】默认使用
      * comfy kitchen 注意力机制（VIDEO_ATTENTION_MODES["kitchen"]）
      * dmad 加速 LoRA（VIDEO_ACCEL_LORA_COMBOS["dmad_4step_critic"]）
    两者都是**默认值** —— 用户在「agent 管理」里改任意一项都立刻生效。

    【为什么走新面板路径】注意力机制与 dmad 只存在于
    video_workflow.build_t2v_panel（新路径）；旧路径（_h3_video_workflow）
    只有 ckpt850/v4step600 两枚固定 LoRA、且**没有**注意力机制。
    所以 Agent 的 t2v 必须带上 model_key + width/height 才能走新路径。
    """
    out = dict(params)
    if not out.get("model_key"):
        out["model_key"] = (defaults.get("model_key")
                            or video_models.VIDEO_DEFAULT_MODEL)
    if not out.get("attention"):
        out["attention"] = (defaults.get("attention")
                            or video_models.VIDEO_DEFAULT_ATTENTION)
    if not out.get("accel_loras"):
        key = str(defaults.get("accel_lora") or "").strip()
        if key and key in video_models.VIDEO_ACCEL_LORA_COMBOS:
            out["accel_loras"] = [{"key": key, "strength": 1.0,
                                    "enabled": True}]
    if not out.get("aspect"):
        out["aspect"] = defaults.get("aspect") or "16:9"
    if out.get("duration") in (None, ""):
        out["duration"] = defaults.get("duration") or video_models.VIDEO_DURATION_DEFAULT
    if not out.get("width") or not out.get("height"):
        # 【2026-11-20 修 —— 用户/模型传的 resolution 档位被忽略】
        #
        # 【原来错在哪（实测复现）】
        #   传 resolution="480p" -> 实际 width=1376 height=768
        #   传 resolution="768p" -> 实际 width=1376 height=768
        #   什么都不传             -> 实际 width=1376 height=768
        # 三个结果**完全一样** —— 因为下面这段只读 ``defaults["megapixels"]``，
        # 从来不看用户传的 ``resolution``。
        #
        # 【为什么这是真缺陷而不是「无害的默认值」】
        # 用户明确说了「要 480p」，服务端却跑 1.0MP（1376x768）——
        # 界面显示的和产物不是一回事，正是本任务要消灭的「说谎」。
        # 而且它与用户报的原话是同一个家族：「请求 480p 实际按 768p 生成」
        # （方向相反：那次是档位被面板门禁吃掉，这次是档位被默认值盖掉）。
        #
        # 【修法】档位优先：用 ``resolution`` 换算 MP；
        # 换算不到（未知档位）才回落到默认 megapixels。
        #
        # 【2026-11-20 续 —— 统一为「MP 预算 × 百分比」】
        # 用户要求「无论生成什么媒体，都是用百万分辨率 + 百分比模式」。
        # 现在统一走 ``size_spec.plan_size``（**同一份**换算实现）：
        #   1. 显式 megapixels（MP 预算）优先；
        #   2. 否则 resolution_percent（该模式真实上限的百分比）；
        #   3. 否则 resolution 档位反查；
        #   4. 都��给 -> 用管理页的默认 MP 预算。
        # 超上限时**自动缩到上限并给出原因**（写进 size_adjust_reasons），
        # 不静默改写 —— 用户能看到「原始请求值 -> 生效值 -> 原因」。
        tier = str(out.get("resolution") or "").strip()
        want_mp = out.get("megapixels")
        want_pct = out.get("resolution_percent")
        mp = None
        if want_mp in (None, "") and want_pct in (None, "") and tier:
            mp = _resolution_tier_megapixels(tier, str(out.get("aspect") or "16:9"))
            if mp is None:
                # 【2026-11-20】该比例下没有这个档位 -> **不写宽高**，
                # 也不回落到默认 megapixels（那会静默改写用户请求）。
                # 留空 -> 由 prepare_agent_t2v_params 明确报错并给出
                # 该比例下的真实档位集。
                out.pop("resolution", None)
                out["_invalid_resolution_tier"] = tier
                return out
        spec_w, spec_h, spec_reasons = size_spec.plan_size(
            "t2v",
            aspect=str(out.get("aspect") or "16:9"),
            megapixels=want_mp,
            percent=want_pct,
            resolution=tier if mp is None else None,
            width=out.get("width"),
            height=out.get("height"),
        )
        if spec_w > 0 and spec_h > 0:
            out["width"] = spec_w
            out["height"] = spec_h
            # 【2026-11-20】把**生效**的 MP 写回 —— 让「请求值 vs 生效值」
            # 可对比（Agent 的 _collect_adjustments 据此回报差异）。
            out["megapixels"] = round(
                spec_w * spec_h / (1024 * 1024), 3)
            if spec_reasons:
                out["size_adjust_reasons"] = spec_reasons
        else:
            if mp is None:
                mp = defaults.get("megapixels") or video_models.VIDEO_DEFAULT_MP
            width, height = video_models.video_dimensions(str(out["aspect"]), mp)
            out["width"] = width
            out["height"] = height
    # 【2026-11-14 需求 A4】通用采样字段：用户（模型）显式给了的值优先，
    # 没给的才用默认值。
    #
    # 【为什么不直接用 gen_defaults.merge_into_params】那个函数给
    # 「非视频模式」用（字段名直接就是 params 的键）。t2v 的
    # accel_lora / megapixels 是**专用语义**（要转成 accel_loras 数组 /
    # 换算成宽高），上面已经处理完了，这里只补通用的那几个键。
    for key in ("steps", "sampler", "scheduler",
                "shift_video", "shift_audio"):
        value = defaults.get(key)
        if value in (None, ""):
            continue
        if out.get(key) in (None, ""):
            out[key] = value
    return out


async def prepare_agent_t2v_params(params: dict,
                                   mode: str = "t2v") -> tuple[dict, str]:
    """
    Agent 的**视频**参数：套默认参数 -> 探测节点 -> 服务端归一。

    与面板走**同一套** normalize_t2v_params（校验 LoRA 文件是否存在、
    注意力节点是否在本机 ComfyUI 里、宽高是否超上限）。
    返回 (归一后的 params, 错误信息)；错误信息为空表示通过。

    【为什么先探测节点】normalize_t2v_params 会断言注意力机制的节点
    在本机 ComfyUI 里（`node not in _COMFY_NODE_NAMES`）。而
    _COMFY_NODE_NAMES 只有在 /api/video-options 或 worker 刷新后才有值 ——
    Agent 这条路不经过面板，不探测就会把 kitchen 误判成「节点不存在」。

    【2026-11-16 第5轮代码审查 C-9 修 —— 加 mode 形参】
    原来本函数**硬写死** ``normalize_t2v_params(merged)``（mode 默认 "t2v"）。
    于是 Agent 建**首尾帧 / 参考生视频**任务时，归一层跑的是 t2v 的口径：

      * 面积上限按各模式自己的策略判（见 video_models.mode_size_policy）
        -> Agent 建的 flf 任务能被放行到超限尺寸；
      * 底模通道按 t2v 判（r2v 必须 Ref2VA 权重）
        -> Agent 建的 r2v 任务可以选到 FL2VA 权重，「参考没生效」。

    现在 mode 由调用方传入，与面板路径（POST /api/jobs 的
    ``normalize_t2v_params(params, mode=mode)``）**完全同口径**。

    【为什么默认参数仍只套 t2v 的】
    ``_merge_t2v_defaults`` 读的是 agent_settings 里的 ``t2v_defaults`` ——
    那是**文生视频**专用的默认参数镜像（model_key / attention / accel_lora）。
    flf / r2v 没有对应的默认参数表（管理页只配了 t2v），
    硬套 t2v 的 model_key 会给 flf/r2v 塞一个不相干的底模。
    所以：**默认参数只对 t2v 套**，但**归一（校验）对三个视频模式都跑**。
    """
    with db() as conn:
        settings = agent_settings.load(conn)
    defaults = settings.get("t2v_defaults") or {}
    merged = _merge_t2v_defaults(params, defaults)

    # 【2026-11-20 新增 —— 档位在该比例下不存在时明确报错】
    #
    # 【为什么必须在这里拦】
    # 档位集合**按比例不同**（实测）：
    #   4:3 的预设里没有 768p（有 672p/736p/800p/832p/864p...）
    #   21:9 的预设里没有 1440p（最高 896p）
    # 跨比例兜底会让「768p + 4:3」静默变成 864p —— 那是**静默改写
    # 用户请求**，正是本任务要消灭的东西。
    #
    # 所以：换算不到 -> 明确报错，并给出**该比例下的真实档位集**，
    # 让模型能一次改对（而不是拿一个说不清的尺寸去跑）。
    invalid_tier = str(merged.pop("_invalid_resolution_tier", "") or "").strip()
    if invalid_tier:
        aspect_key = str(merged.get("aspect") or "16:9")
        allowed = sorted({
            int(p.get("short_edge") or 0)
            for p in video_models.size_presets_for_aspect(aspect_key)
            if int(p.get("short_edge") or 0) > 0})
        allowed_txt = "、".join(f"{v}p" for v in allowed)
        return merged, (
            f"分辨率档位「{invalid_tier}」在画面比例 {aspect_key} 下不存在"
            f"（档位集合按比例不同）。该比例下可用的档位：{allowed_txt}。"
            f"请改用其中之一，或直接给 width/height。")
    tier = str(merged.get("resolution") or "").strip()
    if tier and not merged.get("width"):
        mp = _resolution_tier_megapixels(tier, str(merged.get("aspect") or "16:9"))
        if mp is None:
            aspect_key = str(merged.get("aspect") or "16:9")
            allowed = sorted({
                int(p.get("short_edge") or 0)
                for p in video_models.size_presets_for_aspect(aspect_key)
                if int(p.get("short_edge") or 0) > 0})
            allowed_txt = "、".join(f"{v}p" for v in allowed)
            return merged, (
                f"分辨率档位「{tier}」在画面比例 {aspect_key} 下不存在"
                f"（档位集合按比例不同）。该比例下可用的档位：{allowed_txt}。"
                f"请改用其中之一，或直接给 width/height。")
    # 【强制刷新节点清单】
    #
    # 【为什么必须 force=True】refresh_comfy_node_names 默认会复用 60 秒缓存，
    # 而缓存可能是**上一次探测的残缺结果**（比如 ComfyUI 忙时超时 ->
    # 只问到了部分节点）。若拿到的是残缺缓存，normalize_t2v_params
    # 就会把「没问全」当成「节点不存在」-> 报「本机 ComfyUI 里没有节点
    # ModelAttentionBackend」—— 而节点其实在（实测本机恒在）。
    #
    # 代价：每次 Agent 建视频任务多问 11 个节点（每个 1 ms，实测几十毫秒）。
    # 这个代价换掉「假缺节点」是值得的。
    await refresh_comfy_node_names(force=True)
    return normalize_t2v_params(merged, mode=mode)



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

    # 【2026-11-16 修：请求体必须是对象】
    #
    # 原来缺这道检查 -> 传一个**合法 JSON 数组**（[1,2]）时，
    # 下面的 payload.get("stream") 抛 AttributeError，冒泡到 aiohttp
    # 的兜底处理器 -> 返回 500 + aiohttp 默认错误页（不是本项目
    # JSON 契约），还把 Python 栈信息泄给了客户端。
    #
    # 对照：PUT /api/agent/settings、POST /api/agent/settings/test-engine、
    # POST /api/agent/sessions、POST /api/agent/memory 都有这道检查，
    # 只有两个 chat 端点漏了。
    if not isinstance(payload, dict):
        return web.json_response({"error": "请求必须是对象", "code": "bad_request"},
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
    # 【2026-11-13 审查目标 4】步数上限来自「agent 管理」的配置
    # （默认 = agent_mod.MAX_STEPS = 50，用户可在管理面板调低）。
    # 请求里带 max_steps 时以请求为准，但仍被夹在配置的上限内。
    max_steps = _resolve_max_steps(payload.get("max_steps"))

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
                attachments=attachments,
                # 【2026-11-14 需求 6】总时限来自可编辑配置
                total_timeout=_resolve_total_timeout())
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

    # 【2026-11-16 修：请求体必须是对象】—— 同 api_agent_chat 的说明。
    if not isinstance(payload, dict):
        return web.json_response({"error": "请求必须是对象", "code": "bad_request"},
                                 status=400)

    message = str(payload.get("message") or "").strip()
    if not message:
        return web.json_response({"error": "请输入要处理的内容",
                                  "code": "bad_request"}, status=400)

    session_id = str(payload.get("session_id") or "").strip()
    provider_id = str(payload.get("provider_id") or "").strip() or None
    # 附件（用户随消息上传的图片）
    attachments = payload.get("attachments") or []
    # 【2026-11-13 审查目标 4】步数上限来自「agent 管理」的配置
    # （默认 = agent_mod.MAX_STEPS = 50，用户可在管理面板调低）。
    # 请求里带 max_steps 时以请求为准，但仍被夹在配置的上限内。
    max_steps = _resolve_max_steps(payload.get("max_steps"))

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
                    attachments=attachments,
                    # 【2026-11-14 需求 6】总时限来自可编辑配置
                    total_timeout=_resolve_total_timeout())
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
    with db() as conn:
        snapshot = agent_settings.snapshot(conn)
    capabilities = agent_settings.normalize_capabilities(
        snapshot["settings"].get("capabilities"))
    specs = []
    for name, spec in agent_mod.TOOLS.items():
        capability = agent_extra_tools.TOOL_CAPABILITIES.get(name)
        specs.append({
            "name": name,
            "description": spec["description"],
            "parameters": spec["parameters"],
            "capability": capability or "",
            # 「当前是否可用」—— 取决于能力开关，不是「注册了就是能用」
            "enabled": True if not capability else bool(capabilities.get(capability)),
        })
    return json_ok({
        "tools": specs,
        "tool_count": len(specs),
        "enabled_tool_count": sum(1 for s in specs if s["enabled"]),
        "limits": {
            "max_steps": agent_mod.MAX_STEPS,
            "max_steps_cap": agent_settings.MAX_STEPS_HARD_CAP,
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


# ------------------------------------------------------------------ Agent 管理
#
# 【2026-11-13 审查目标 4/5】内置 Agent 的「agent 管理」配置接口。
#
#  1) GET  /api/agent/settings     读配置（能力开关 / 搜索引擎 / 默认参数）
#  2) PUT  /api/agent/settings     写配置
#  3) POST /api/agent/settings/test-engine   搜索引擎连通性自检
#
# 【为什么单独一个端点而不是复用 /api/enhancer】
# /api/enhancer 是「提示词增强 Agent」的模板配置，语义完全不同。
# 混在一起会让两个 Agent 的配置互相污染。


def agent_settings_snapshot() -> dict:
    """
    Agent 运行期读配置的入口（h3studio.agent_extra_tools 通过它拿能力开关）。

    每次调用都读一次库 —— 配置改完**立刻生效**，不需要重启服务。
    """
    with db() as conn:
        return agent_settings.snapshot(conn)


def _t2v_default_options() -> dict:
    """
    文生视频默认参数的合法取值清单（**本地常量**，不需要探测 ComfyUI）。

    【为什么单独给一份】前端「agent 管理」面板要渲染四个下拉
    （底模 / 注意力机制 / 加速 LoRA / 画面比例）。
    而 /api/video-options 会去**探测本机 ComfyUI**（拉模型列表 + 问节点
    是否存在），实测能到 60 秒以上 —— 用它填下拉会让面板长时间空白。

    这四个清单都是 `video_models` 里的**注册表常量**，列全不需要 IO。
    可用性/兼容性的**实测**判断仍然走 /api/video-options 与
    prepare_agent_t2v_params（服务端在真正建任务前做最终校验）。

    返回：
      models       [{key,label}]
      attentions   [{key,label,note}]
      accels       [{key,label}]
      aspects      [{key,label}]
    """
    return {
        "models": [
            {"key": key, "label": meta.get("label") or key}
            for key, meta in video_models.VIDEO_MODELS.items()
        ],
        "attentions": [
            {"key": key, "label": meta.get("label") or key,
             "note": meta.get("note") or ""}
            for key, meta in video_models.VIDEO_ATTENTION_MODES.items()
            if key not in video_models.VIDEO_REMOVED_ATTENTION_MODES
        ],
        "accels": [
            {"key": key, "label": meta.get("display") or key}
            for key, meta in video_models.VIDEO_ACCEL_LORA_COMBOS.items()
        ],
        "aspects": [
            {"key": key, "label": key}
            for key in video_models.VIDEO_ASPECTS.keys()
        ],
    }


async def api_agent_settings(request):
    """
    GET /api/agent/settings —— 读「agent 管理」配置
    PUT /api/agent/settings —— 写「agent 管理」配置

    body（PUT）：
      max_steps              步数上限（1~50）
      capabilities           {file_read/web_search/code_exec/media_tools: bool}
      search_engines         [{id,name,type,url,method,enabled}]
      t2v_defaults           {model_key,attention,accel_lora,aspect,megapixels,duration}
      file_roots             允许 read_file 读取的目录（绝对路径）
      code_exec_timeout_sec  代码执行超时
      ffmpeg_path            ffmpeg 可执行文件路径（留空 = 用 PATH）
    """
    if request.method == "GET":
        with db() as conn:
            snapshot = agent_settings.snapshot(conn)
        settings = snapshot["settings"]
        return json_ok({
            "settings": settings,
            "capabilities": agent_settings.capability_state(settings),
            "search_engines": settings.get("search_engines") or [],
            "t2v_defaults": settings.get("t2v_defaults") or {},
            # 【2026-11-14 需求 A4】按生成类型分别配置的默认参数。
            #
            # 【为什么两个都下发】gen_defaults 是**完整**结构（每个生成类型
            # 一份），t2v_defaults 是**兼容镜像**（老客户端读它）。
            # 两份都发，前端用 gen_defaults 渲染，老脚本用 t2v_defaults。
            "gen_defaults": settings.get("gen_defaults") or {},
            # 字段规格（合法取值清单 + 类型 + 区间）——
            # 前端**不硬编码**任何取值集合，全部从这里来。
            "gen_defaults_spec": gen_defaults.spec_payload(),
            "limits": {
                "max_steps": agent_mod.MAX_STEPS,
                "max_steps_cap": agent_settings.MAX_STEPS_HARD_CAP,
                "max_tool_calls": agent_mod.MAX_TOOL_CALLS,
                # 【2026-11-14 需求 6】总时限现在是**可配置**的 ——
                # 下发的是当前生效值（不是写死的常量），
                # 并把可调区间一起给前端做校验。
                "total_timeout": int(settings.get("total_timeout")
                                     or agent_mod.TOTAL_TIMEOUT),
                "total_timeout_min": agent_settings.TOTAL_TIMEOUT_MIN,
                "total_timeout_max": agent_settings.TOTAL_TIMEOUT_MAX,
            },
            "dependencies": {
                "ffmpeg_path": snapshot.get("ffmpeg_path") or "",
                "ffprobe_path": snapshot.get("ffprobe_path") or "",
                "ffmpeg_available": bool(snapshot.get("ffmpeg_path")),
            },
            # 【文生视频默认参数的合法取值清单 —— 2026-11-14】
            #
            # 【为什么要在这里给一份】前端「agent 管理」面板要渲染
            # 「注意力机制 / 加速 LoRA / 底模 / 比例」四个下拉。
            # 而 /api/video-options 会去**探测本机 ComfyUI**（拉模型列表 +
            # 问节点是否存在），实测能到 60 秒以上 —— 用它填下拉会让面板
            # 长时间空白。
            #
            # 这四个清单都是**本地常量**（video_models 的注册表），
            # 不需要探测 ComfyUI 就能列全，所以直接在这里给一份快的。
            # （可用性/兼容性的实测判断仍然走 /api/video-options，
            #   服务端在 prepare_agent_t2v_params 里做最终校验。）
            "t2v_options": _t2v_default_options(),
        })

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误", "code": "bad_request"},
                                 status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "配置必须是对象", "code": "bad_request"},
                                 status=400)
    with db() as conn:
        prov.ensure_tables(conn)
        saved = agent_settings.save(conn, payload)
    # 立刻把 ffmpeg 路径推给媒体工具模块（下次调用就用新路径）
    agent_media_tools.configure(agent_settings.ffmpeg_path(saved))
    return json_ok({"ok": True, "settings": saved,
                    "capabilities": agent_settings.capability_state(saved),
                    # 【A4】保存后把最新的默认值与规格一起回给前端 ——
                    # 前端据此刷新界面（归一后的值可能与提交值不同，
                    # 比如越界的时长被夹回区间）。
                    "gen_defaults": saved.get("gen_defaults") or {},
                    "gen_defaults_spec": gen_defaults.spec_payload()})


async def api_agent_settings_test_engine(request):
    """
    POST /api/agent/settings/test-engine —— 搜索引擎连通性自检。

    body: {"engine": {...}} 或 {"engine_id": "xxx"}
    真发一次请求（不是只校验 URL 格式）。
    """
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误", "code": "bad_request"},
                                 status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "请求必须是对象", "code": "bad_request"},
                                 status=400)
    engine = payload.get("engine")
    if not isinstance(engine, dict):
        engine_id = str(payload.get("engine_id") or "").strip()
        if not engine_id:
            return web.json_response(
                {"error": "需要 engine 或 engine_id", "code": "bad_request"},
                status=400)
        with db() as conn:
            settings = agent_settings.load(conn)
        found = next((e for e in settings.get("search_engines") or []
                      if e.get("id") == engine_id), None)
        if not found:
            return web.json_response(
                {"error": f"找不到搜索引擎 {engine_id}", "code": "not_found"},
                status=404)
        engine = found
    result = await agent_websearch.check_engine(engine)
    return json_ok(result, status=200 if result.get("ok") else 502)


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
    # 【2026-11-16 修：请求体必须是对象】
    # 原来缺这道检查 -> 传一个**合法 JSON 数组**时，
    # 下面的 payload.get("key") 抛 AttributeError -> 500 + 栈泄漏。
    # （与 api_agent_chat / api_agent_chat_stream 同源缺陷，同批修复。）
    if not isinstance(payload, dict):
        return web.json_response({"error": "请求必须是对象", "code": "bad_request"},
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
    # 【2026-11-15 代码审查修 —— 路径穿越】
    #
    # 原来这里用的是 `str(full).startswith(str(OUT_DIR.resolve()))` ——
    # 那是**字符串前缀**判定，不是路径边界判定。
    # 同级目录 `outputs_secret` 的字符串前缀是 `.../outputs` ->
    # startswith 判定为 True -> 越界文件被当成产物返回。
    #
    # 实测（8190 线上，2026-11-15）：
    #   /files/..%2Foutputs_secret%2Fcreds.txt        -> 200（读到了越界文件）
    #   /files/%2e%2e%2Foutputs_secret%2Fcreds.txt    -> 200（同上）
    #   /files/../outputs_secret/creds.txt             -> 404（aiohttp 规范化掉了裸 ../）
    # 裸 ../ 被 aiohttp 规范化，所以**必须用 %2F 编码**才能逃逸 ——
    # 这正是它容易被漏掉的原因。
    #
    # 修法：用仓库里已有的正确判据 `h3studio/assets.py::resolve_inside`
    # （relative_to 是路径边界判定，不是字符串前缀）。
    try:
        full.relative_to(OUT_DIR.resolve())
    except ValueError:
        raise web.HTTPNotFound()
    if not full.is_file():
        raise web.HTTPNotFound()
    # 显式给 Content-Type —— 音频产物没有它的话浏览器会当成下载而不是播放
    mime = MEDIA_TYPES.get(full.suffix.lower())
    headers = {"Cache-Control": "no-cache"}
    if mime:
        headers["Content-Type"] = mime
    return web.FileResponse(full, headers=headers)


async def thumb_handler(request):
    """GET /api/thumb/{key}.jpg —— 生成媒体库卡片的受限尺寸缩略图。

    【为什么需要 —— 实测 2026-11-21】
    卡片原本直接引用**原始产物**：图片 1.1MB~9.5MB，视频 Chrome 会
    `Range: bytes=0-` 拉**整个文件**（aiohttp 对 `bytes=0-` 返回全量）。
    一页 12 张卡实测媒体字节 **8.75MB**（CDP `Network.dataReceived` 累计），
    其中 11 个视频就占 7.81MB。

    缩略图实测：图片 ~30ms / 视频 ~140ms，产物 14~43KB
    ⇒ 媒体字节降到 ~0.3MB（约 30 倍）。

    【key 怎么来的】key = sha1(路径 + mtime_ns + size)，服务端在
    `GET /api/jobs` 里算好并下发（见 `_attach_thumb_urls`）。
    **列表接口只算 key，不生成缩略图** —— 否则列表接口自己会被
    11 次 ffmpeg 拖到 1.5~2.9 秒（实测）。

    【key -> 源文件怎么反查】
    缓存目录里存的是 `data/thumbs/<xx>/<yy>/<key>.jpg`，反查不到源文件。
    所以另存一份**键 -> 源路径**的索引（`data/thumbs/index.json`）。
    索引写的是**相对 outputs 目录**的路径 —— 绝对路径不该进索引
    （换机器/换目录就失效了）。

    【失败回落】缩略图生成不了（ffmpeg 缺失 / 文件已删 / 格式怪）
    -> 404，前端回落引用原图（`outputUrl`），功能不降级。
    """
    name = request.match_info.get("name", "")
    # 只接受 "<40位hex>.jpg" —— 目录遍历、任意文件名一律拒绝
    if not name.endswith(".jpg"):
        raise web.HTTPNotFound()
    stem = name[:-4]
    if len(stem) != 40 or any(ch not in "0123456789abcdef" for ch in stem):
        raise web.HTTPNotFound()
    # 已生成 -> 直接返回
    cached = thumb_service.thumb_path(DATA, stem)
    if cached.is_file() and cached.stat().st_size > 0:
        return _thumb_response(cached)
    # 未生成 -> 查索引找源文件
    src = _thumb_source_for(stem)
    if src is None:
        raise web.HTTPNotFound()
    generated = await thumb_service.thumbnail_for(DATA, src)
    if generated is None or not generated.is_file():
        raise web.HTTPNotFound()
    return _thumb_response(generated)


def _thumb_response(path: Path):
    """缩略图响应。

    【缓存策略 —— 为什么不用「永久缓存」那串指令】
    缩略图 URL 是**内容寻址**的（`/api/thumb/<sha1(path+mtime+size)>.jpg`）：
    源文件一变，key 就变，URL 就变 —— 所以**同一 URL 的内容永远不会变**，
    理论上该打那串「永久缓存」指令。

    但仓库里有一条既有契约（tests/test_frontend_assets_hygiene.py::
    test_server_static_has_no_cache_control_is_known_risk）：
    **`server.py` 里不允许出现那串「永久缓存」指令** —— 因为静态资源的
    `?v=` 版本号才是唯一的破缓存防线，那串指令会让浏览器永久缓存旧 URL。

    那条契约针对的是 `/static/*`（带 `?v=` 的版本化资源），
    而缩略图是 `/api/thumb/*`（内容寻址）—— 两者机制不同。
    但**不为一条缩略图去改既有契约**（改测试让自己变绿 = 假绿），
    所以这里退一步：**去掉那串指令，只保留 `max-age=86400`**。

    【代价有多大】几乎为零：
      * URL 内容寻址 -> 源文件变了就是新 URL，浏览器必然重新取；
      * 24 小时内不会重新取（max-age）；
      * 就算过期了要重验，也是 `If-None-Match` -> 304 / 0 字节 / ~2ms。
    实测：带与不带那串指令，浏览器拿到的字节数完全一样。
    """
    return web.FileResponse(path, headers={
        "Content-Type": "image/jpeg",
        "Cache-Control": "public, max-age=86400",
    })


#: 键 -> 源文件相对路径 的索引文件
_THUMB_INDEX = "index.json"

#: 索引的**内存副本** —— 每次列表请求都要查，写磁盘会变成新的瓶颈。
#: 进程内维护 + 延迟落盘（攒够一批或超过 N 秒再写）。
_THUMB_INDEX_MEM: dict = {}
#: 上次落盘时间
_THUMB_INDEX_TS = 0.0
#: 落盘间隔（秒）
_THUMB_INDEX_FLUSH_INTERVAL = 30.0
#: 内存索引上限（超出就丢弃最旧的 —— 缩略图本身也会被清掉）
_THUMB_INDEX_MAX = 5000


def _thumb_index_path() -> Path:
    return thumb_service.thumb_dir(DATA) / _THUMB_INDEX


def _load_thumb_index() -> dict:
    """加载索引到内存（只在第一次 / 内存为空时读盘）。"""
    if _THUMB_INDEX_MEM:
        return _THUMB_INDEX_MEM
    path = _thumb_index_path()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            _THUMB_INDEX_MEM.update(data)
    except Exception:  # noqa: BLE001
        pass
    return _THUMB_INDEX_MEM


def _save_thumb_index(index: dict) -> None:
    path = _thumb_index_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp.json")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(index, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


def _thumb_source_for(key: str) -> Path | None:
    """键 -> 源文件的绝对路径（查索引）。找不到返回 None。"""
    index = _load_thumb_index()
    rel = index.get(key)
    if not rel:
        return None
    full = (OUT_DIR / str(rel)).resolve()
    try:
        full.relative_to(OUT_DIR.resolve())
    except ValueError:
        return None
    return full if full.is_file() else None


def _remember_thumb_source(key: str, src: Path) -> None:
    """把「键 -> 源文件相对路径」记进索引（供反查）。

    【为什么不在这里直接写盘】列表接口每 2 秒轮询一次，
    每次都写一次 index.json = 无谓的磁盘 IO（而且会和自己的
    写-读竞争）。改成：**内存维护 + 延迟批量落盘**。
    """
    global _THUMB_INDEX_TS
    try:
        rel = str(Path(src).resolve().relative_to(OUT_DIR.resolve()))
    except (ValueError, OSError):
        return
    index = _load_thumb_index()
    if index.get(key) == rel:
        return
    index[key] = rel
    if len(index) > _THUMB_INDEX_MAX:
        for stale in list(index)[:len(index) - _THUMB_INDEX_MAX]:
            index.pop(stale, None)
    now = time.time()
    if now - _THUMB_INDEX_TS >= _THUMB_INDEX_FLUSH_INTERVAL:
        _THUMB_INDEX_TS = now
        _save_thumb_index(index)


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
    # 【2026-11-15】产物实际生成参数（卡片详情模态框用）
    app.router.add_get("/api/jobs/{id}/artifact-meta", api_artifact_meta)
    # 生成队列快照（队列顺序 / 状态 / 计数）—— 队列化需求的只读视图
    app.router.add_get("/api/queue", api_queue)
    # 接续生成：上下文回填 + 功能可用性探测
    app.router.add_get("/api/jobs/{id}/continuation", api_continuation_context)
    app.router.add_get("/api/continuation/state", api_continuation_state)
    app.router.add_post("/api/jobs/{id}/cancel", api_cancel)
    app.router.add_delete("/api/jobs/{id}", api_delete)
    # 【2026-11-20 新增】生成媒体库「收藏」。
    # 【路由顺序】/api/jobs/favorite/batch 必须**先于** /api/jobs/{id}/favorite
    # 注册 —— aiohttp 按注册顺序匹配，`{id}` 会把 "favorite" 当成 id 吃掉。
    app.router.add_post("/api/jobs/favorite/batch", api_jobs_favorite_batch)
    app.router.add_get("/api/jobs/favorites", api_favorites_count)
    app.router.add_post("/api/jobs/{id}/favorite", api_job_favorite)
    # 生成媒体库「管理」（2026-11-14 需求 11 / 12）
    app.router.add_get("/api/media/manage", api_media_manage)
    app.router.add_post("/api/media/manage", api_media_manage)
    app.router.add_get("/api/system", api_system)
    # 【2026-11-19 缺陷 1 修】运行上限策略的只读查询（前端展示用）。
    app.router.add_get("/api/timeout-policy", api_timeout_policy)
    app.router.add_get("/api/status/stream", api_status_stream)
    app.router.add_get("/api/hardware", api_hardware)
    app.router.add_post("/api/gpu/power-limit", api_gpu_power_limit)
    app.router.add_post("/api/unload", api_unload)
    app.router.add_get("/api/image-options", api_image_options)
    # 「创意内容设计」选项目录（文生图提示词构造器）
    app.router.add_get("/api/prompt-catalog", api_prompt_catalog)
    app.router.add_get("/api/music-options", api_music_options)
    app.router.add_post("/api/music-enhance", api_music_enhance)

    # 文生视频面板（模型 / 加速 LoRA / 注意力 / 二采 / 宽高吸附）
    app.router.add_get("/api/video-options", api_video_options)
    # H3 效果 LoRA 清单（独立于 /api/image-loras —— 视频基座层结构不同）
    app.router.add_get("/api/video-effect-loras", api_video_effect_loras)
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
    # H3-Image 自带 PE（2026-11-14 需求 16）
    app.router.add_post("/api/h3image-pe/expand", api_h3image_pe_expand)
    # OpenAI 兼容代理（给 ComfyUI 的 LLM 节点用，可关 thinking）
    app.router.add_get("/api/llm/p/{id}/v1/models", api_llm_proxy_models)
    app.router.add_post("/api/llm/p/{id}/v1/chat/completions", api_llm_proxy_chat)
    # 内置多模态 Agent
    app.router.add_post("/api/agent/chat", api_agent_chat)
    app.router.add_post("/api/agent/upload", api_agent_upload)
    app.router.add_get("/api/agent/media/{id}/status", api_agent_media_status)
    app.router.add_post("/api/agent/chat/stream", api_agent_chat_stream)
    app.router.add_get("/api/agent/tools", api_agent_tools)
    # 【2026-11-13 审查目标 4/5】「agent 管理」配置端点
    app.router.add_get("/api/agent/settings", api_agent_settings)
    app.router.add_put("/api/agent/settings", api_agent_settings)
    app.router.add_post("/api/agent/settings/test-engine",
                        api_agent_settings_test_engine)
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
    # 【2026-11-21 性能修】卡片缩略图（受限尺寸，走缓存）
    app.router.add_get("/api/thumb/{name}", thumb_handler)
    app.router.add_static("/static", BASE / "web")
    # 【2026-11-15 D12 流程图线上可达】文档目录挂到 /docs。
    # 之前只挂了 /static（前端资源），docs/ 下的 5 套交互流程图与
    # 《交互流程图说明.md》虽然已经随 D12 进了主线，但线上 404 —— 用户
    # 看不到、也没法打开。挂载后 /docs/ 直接是目录索引，
    # /docs/交互流程图-总览.png 直接出图（见 h3studio/docs_static.py）。
    docs_static.register(app, BASE / "docs")
    app.router.add_get("/", index_handler)
    return app


async def main():
    init_db()
    # 【2026-11-14 A5 需求】服务启动时做一次「残留 running」自愈。
    #
    # 【为什么必须在这里做】DB 里 status='running' 的行是**声明**，
    # 不是观测 —— 进程重启后没有任何 worker 在跑它们了。
    # 不自愈的话，生成库里的卡片会永远显示「运行中」。
    #
    # 【为什么放在 worker.start() 之前】自愈会把「还在 ComfyUI 队列里」的
    # 任务**接管**（起后台协程等结果）；先自愈再启动 worker，
    # 接管与正常调度不会互相抢同一个任务。
    try:
        heal_report = await _heal_residual_running()
        if heal_report.get("healed"):
            print(f"[自愈] 共处理 {heal_report['healed']} 条残留「运行中」任务 "
                  f"（失败 {heal_report['failed']} / 接管 {heal_report['adopted']} / "
                  f"回收 {heal_report['finalized']}）")
    except Exception as exc:
        # 自愈失败**不能**让服务起不来 —— 只打印日志。
        print(f"[自愈] 启动自愈异常（不影响服务启动）: {exc}")
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
    # 进程退出时关闭探测用的长连接 session（否则 aiohttp 会打
    # "Unclosed client session" 警告）。
    #
    # 【注册时机】必须在 runner.setup() **之前** —— setup() 之后
    # app.on_shutdown / on_cleanup 会被冻结（FrozenList），
    # 再 append 会抛 "Cannot modify frozen list."。
    async def _close_probe_session(_app):
        global _H3_IMAGE_PROBE_SESSION
        sess = _H3_IMAGE_PROBE_SESSION
        _H3_IMAGE_PROBE_SESSION = None
        if sess is not None and not sess.closed:
            await sess.close()
    app.on_cleanup.append(_close_probe_session)
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
