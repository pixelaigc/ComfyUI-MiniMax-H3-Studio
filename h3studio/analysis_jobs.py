# -*- coding: utf-8 -*-
"""
异步分析任务调度。

为什么要异步：
  深度音频分析（CLAP 逐窗 + AST + silero VAD + demucs + faster-whisper + LLM）
  在本机实测 116s 音频要 600s 上下。同步执行会把上传接口和触发接口卡死，
  所以：
    * 触发接口立刻返回 job_id，UI 显示「分析中」；
    * 分析在后台线程里跑，状态实时写进 asset_analysis_jobs；
    * 前端轮询 /api/assets/{id}/analysis-status 拿进度。

图片/视频分析也走同一条通道（它们也要调多模态模型，十几秒起），
这样 UI 逻辑统一：任何素材的分析都是「触发 -> 轮询 -> 出结果」。
"""
from __future__ import annotations

import asyncio
import threading
import traceback
from pathlib import Path

from . import analysis as analysis_mod
from . import asset_repo
from . import assets as A
from .assets import LibraryError


class _JobRunner:
    """
    单进程内的后台分析执行器。

    用线程而不是 asyncio task：分析器内部既有同步的 subprocess 调用
    （深度音频分析）也有 aiohttp 的异步模型调用（图片/视频/音频的 LLM 描述），
    两种都在独立线程里跑最省事，也不会阻塞主事件循环。

    线程池上限默认 1 —— 深度分析吃满 CPU，并发跑只会互相拖慢。
    """

    def __init__(self, max_workers: int = 1):
        self._sem = threading.Semaphore(max_workers)
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    def submit(self, fn, *args, **kwargs) -> None:
        def runner():
            with self._sem:
                try:
                    fn(*args, **kwargs)
                except Exception:
                    traceback.print_exc()

        th = threading.Thread(target=runner, daemon=True,
                              name="asset-analysis-job")
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(th)
        th.start()


#: 全局执行器
RUNNER = _JobRunner()


# ------------------------------------------------------------------ 状态文案

STAGE_LABELS = {
    "queued": "排队中",
    "running": "分析中",
    "done": "已完成",
    "failed": "失败",
}

#: 深度音频分析各阶段的中文提示（按进度粗略推进）
AUDIO_STAGES = [
    ("bpm", "测算节拍速度", 0.05),
    ("instruments", "识别乐器与音色", 0.15),
    ("vocals", "检测人声与分离伴奏", 0.55),
    ("asr", "语音识别（含时间戳）", 0.85),
    ("llm", "大模型语义补充", 0.95),
]


def _audio_progress_for(stage: str) -> tuple[float, str]:
    """阶段名 -> (进度基准, 中文提示)。"""
    idx = next((i for i, (k, _, _) in enumerate(AUDIO_STAGES) if k == stage), 0)
    _, label, base = AUDIO_STAGES[idx]
    return base, label


def _audio_progress(stage_index: int, frac: float = 0.5) -> tuple[float, str]:
    """按阶段推进进度条。"""
    if stage_index < 0:
        return 0.0, "准备中"
    idx = min(stage_index, len(AUDIO_STAGES) - 1)
    _, label, base = AUDIO_STAGES[idx]
    span = (AUDIO_STAGES[idx + 1][2] - base) if idx + 1 < len(AUDIO_STAGES) else (1.0 - base)
    return round(base + span * frac, 3), label


# ------------------------------------------------------------------ 工具


def _library_conn():
    """素材库连接。延迟 import，避免循环依赖。"""
    from . import assets as asset_store
    return asset_store.connect()


def _source_of(asset_id: str, version: dict) -> Path | None:
    root = A.library_root()
    try:
        path = A.resolve_inside(root, version["storage_path"])
    except Exception:
        return None
    return path if path and path.is_file() else None


# ------------------------------------------------------------------ 任务执行


def _run_audio_deep(job_id: int, asset_id: str, source: Path,
                     options: dict) -> None:
    """
    深度音频分析：调用 analyze_audio.py 的可插拔后端。

    走 AUDIO_DEEP 后端（h3studio.audio_deep），**不重写算法**。
    结果写入 asset_analyses.text —— 与图片/视频的分析文本同一处存储。
    """
    from . import audio_deep

    conn = _library_conn()
    try:
        asset_repo.update_job(conn, job_id, status="running",
                              stage="启动深度音频分析", progress=0.02,
                              started_at=A.now())
        conn.close()

        # 分阶段回调 -> 更新进度条
        def progress_cb(stage: str, frac: float):
            """脚本每进入一个新阶段回调一次。"""
            try:
                c = _library_conn()
                _, label = _audio_progress_for(stage)
                asset_repo.update_job(c, job_id, stage=label, progress=frac)
                c.close()
            except Exception:
                pass

        result = audio_deep.analyze(source, dict(options or {},
                                                 progress=progress_cb))

        conn = _library_conn()
        try:
            version = asset_repo.current_version(conn, asset_id)
            revision = asset_repo.next_revision(conn, asset_id)
            extra = dict(result["extra"])
            extra["analyzer_mode"] = "deep_audio_pipeline"
            record = asset_repo.save_analysis(
                conn, asset_id, "audio", result["text"], revision,
                provider=None,
                prompt="analyze_audio.py 深度管线：BPM/乐器/人声/ASR/语义",
                frames=[], extra=extra)
            asset_repo.update_job(conn, job_id, status="done", stage="已完成",
                                  progress=1.0,
                                  message=f"已生成第 {record['revision']} 版深度分析",
                                  revision=record["revision"],
                                  finished_at=A.now())
        finally:
            conn.close()
    except audio_deep.DeepAudioError as exc:
        _fail_job(job_id, asset_id, exc.message, exc.code)
    except Exception as exc:  # noqa: BLE001
        _fail_job(job_id, asset_id, f"{type(exc).__name__}: {exc}",
                  "audio_analysis_failed")


def _fail_job(job_id: int, asset_id: str, message: str, code: str) -> None:
    """失败也要留痕：写一条 error 的分析记录 + 任务置 failed。"""
    try:
        conn = _library_conn()
        try:
            asset_repo.update_job(conn, job_id, status="failed", stage="失败",
                                  message=message, finished_at=A.now())
            revision = asset_repo.next_revision(conn, asset_id)
            asset_repo.save_analysis(conn, asset_id, "audio", "", revision,
                                     provider=None, prompt="",
                                     frames=[], extra={"error_code": code},
                                     error=message)
        finally:
            conn.close()
    except Exception:
        traceback.print_exc()


def _run_multimodal(job_id: int, asset_id: str, source: Path,
                    options: dict) -> None:
    """
    图片 / 视频 / 音频的**多模态**分析（走工作台已配置的本地模型 Provider）。

    与深度音频分析的区别：深度音频分析是**确定性信号处理**
    （BPM/乐器/人声/ASR），多模态是**模型看图看帧**写自然语言描述。
    两者结果都落在 asset_analyses.text，用 extra.analyzer_mode 区分。
    """
    conn = _library_conn()
    try:
        asset = asset_repo.get_asset(conn, asset_id)
        version = asset_repo.current_version(conn, asset_id)
        provider_id = (options or {}).get("provider_id")
        conn.close()
        if not asset or not version:
            raise LibraryError("素材或版本不存在", status=404)

        asset_repo.update_job(conn, job_id, status="running",
                              stage="调用多模态模型", progress=0.3,
                              started_at=A.now())

        result = asyncio.run(analysis_mod.run_analysis(
            conn, asset, version, source, options or {}, provider_id))

        conn = _library_conn()
        try:
            revision = asset_repo.next_revision(conn, asset_id)
            extra = dict(result.get("extra") or {})
            extra["analyzer_mode"] = "multimodal"
            record = asset_repo.save_analysis(
                conn, asset_id, result["kind"], result["text"], revision,
                provider=result.get("provider"), prompt=result.get("prompt", ""),
                frames=result.get("frames") or [], extra=extra)
            asset_repo.update_job(conn, job_id, status="done", stage="已完成",
                                  progress=1.0,
                                  message=f"已生成第 {record['revision']} 版分析",
                                  revision=record["revision"],
                                  finished_at=A.now())
        finally:
            conn.close()
    except LibraryError as exc:
        _fail_job(job_id, asset_id, exc.message, exc.code)
    except Exception as exc:  # noqa: BLE001
        _fail_job(job_id, asset_id, f"{type(exc).__name__}: {exc}",
                  "analysis_failed")


def _run_correction(job_id: int, asset_id: str, source: Path,
                    question: str, options: dict) -> None:
    """多轮交互纠错（基于历史分析 + 素材 + 用户问题重新出描述）。"""
    conn = _library_conn()
    try:
        asset = asset_repo.get_asset(conn, asset_id)
        version = asset_repo.current_version(conn, asset_id)
        provider_id = (options or {}).get("provider_id")
        conn.close()
        if not asset or not version:
            raise LibraryError("素材或版本不存在", status=404)

        asset_repo.update_job(conn, job_id, status="running",
                              stage="调用多模态模型纠错", progress=0.3,
                              started_at=A.now())

        result = asyncio.run(analysis_mod.run_correction(
            conn, asset, version, source, question, options or {}, provider_id))

        conn = _library_conn()
        try:
            revision = asset_repo.next_revision(conn, asset_id)
            extra = dict(result.get("extra") or {})
            extra["analyzer_mode"] = "multimodal_correction"
            record = asset_repo.save_analysis(
                conn, asset_id, result["kind"], result["text"], revision,
                provider=result.get("provider"), prompt=question,
                frames=result.get("frames") or [], extra=extra)
            asset_repo.update_job(conn, job_id, status="done", stage="已完成",
                                  progress=1.0,
                                  message=f"已生成第 {record['revision']} 版修正",
                                  revision=record["revision"],
                                  finished_at=A.now())
        finally:
            conn.close()
    except LibraryError as exc:
        _fail_job(job_id, asset_id, exc.message, exc.code)
    except Exception as exc:  # noqa: BLE001
        _fail_job(job_id, asset_id, f"{type(exc).__name__}: {exc}",
                  "analysis_failed")


# ------------------------------------------------------------------ 对外入口


def start_analysis(asset_id: str, kind: str, mode: str = "analyze",
                   question: str = "", options: dict | None = None) -> dict:
    """
    启动一个异步分析任务。**立即返回** job 记录。

    kind  = image | video | audio
    mode  = analyze | correction
    """
    options = options or {}
    conn = _library_conn()
    try:
        asset = asset_repo.get_asset(conn, asset_id)
        if not asset:
            raise LibraryError("素材不存在", status=404,
                               code="asset_not_found")
        version = asset_repo.current_version(conn, asset_id)
        if not version:
            raise LibraryError("素材没有可用版本", status=400)
        source = _source_of(asset_id, version)
        if source is None:
            raise LibraryError("素材文件已丢失", status=404)

        job = asset_repo.create_job(conn, asset_id, kind, mode=mode,
                                    question=question, options=options)
    finally:
        conn.close()

    if mode == "correction":
        RUNNER.submit(_run_correction, job["id"], asset_id, source,
                       question, options)
    elif kind == "audio" and options.get("deep"):
        RUNNER.submit(_run_audio_deep, job["id"], asset_id, source, options)
    else:
        RUNNER.submit(_run_multimodal, job["id"], asset_id, source, options)
    return job


def job_status(conn, asset_id: str, job_id: int | None = None) -> dict:
    """给前端轮询用：当前任务状态 + 该素材的历史任务。"""
    if job_id:
        job = asset_repo.get_job(conn, int(job_id))
    else:
        job = asset_repo.active_job_of(conn, asset_id)
        if not job:
            jobs = asset_repo.jobs_of(conn, asset_id, limit=1)
            job = jobs[0] if jobs else None
    history = asset_repo.jobs_of(conn, asset_id, limit=10)
    return {
        "asset_id": asset_id,
        "job": job,
        "status": (job or {}).get("status", "idle"),
        "stage": (job or {}).get("stage", ""),
        "progress": (job or {}).get("progress", 0),
        "message": (job or {}).get("message", ""),
        "revision": (job or {}).get("revision"),
        "history": history,
    }
