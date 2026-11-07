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
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

import psutil

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aiohttp import web, ClientSession, ClientTimeout
import aiohttp

from h3studio.workflows import build_workflow

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"

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
            """
        )


def row_to_job(r):
    return {
        "id": r["id"],
        "created_at": r["created_at"],
        "mode": r["mode"],
        "params": json.loads(r["params"] or "{}"),
        "status": r["status"],
        "progress": r["progress"],
        "progress_text": r["progress_text"],
        "error": r["error"],
        "outputs": json.loads(r["outputs"] or "[]"),
        "elapsed": (r["finished_at"] or time.time()) - r["started_at"] if r["started_at"] else None,
    }


# ------------------------------------------------------------------ ComfyUI client
COMFY = CFG["comfyui_url"].rstrip("/")
HTTP_TIMEOUT = ClientTimeout(total=600)
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
LONGFILM_LOCK = asyncio.Lock()
LONGFILM_TASKS = set()
LONGFILM_PROCESSES = {}
TTS_PROCESSES = {}


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
        for key, kind in (("videos", "video"), ("gifs", "video"), ("images", "image"),
                          ("audio", "audio"), ("audios", "audio")):
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
                    elif fname.lower().endswith((".wav", ".flac", ".mp3", ".ogg", ".m4a")):
                        kind = "audio"
                    outs.append({"kind": kind, "filename": fname,
                                 "subfolder": item.get("subfolder", ""),
                                 "type": item.get("type", "output")})
    return outs


def _next_output_path(kind: str, suffix: str) -> Path:
    """为新输出分配稳定、易检索的顺序文件名。"""
    if kind == "image":
        dest_dir, prefix = IMAGE_DIR, "i-zimage-"
    elif kind == "qwen_image":
        dest_dir, prefix = IMAGE_DIR, "i-qwen-"
    elif kind == "audio":
        dest_dir, prefix = AUDIO_DIR, "a-tts-"
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


async def comfy_download_output(session: ClientSession, item: dict, mode: str = "") -> str:
    params = {"filename": item["filename"], "subfolder": item["subfolder"], "type": item["type"]}
    suffix = Path(item["filename"]).suffix or (".png" if item["kind"] == "image" else ".mp4")
    output_kind = "qwen_image" if item["kind"] == "image" and mode == "qwen_t2i" else item["kind"]
    dest = _next_output_path(output_kind, suffix)
    async with session.get(f"{COMFY}/view", params=params) as resp:
        resp.raise_for_status()
        dest.write_bytes(await resp.read())
    return dest.relative_to(OUT_DIR).as_posix()


async def run_local_tts(job_id: str, params: dict):
    """使用 Windows 自带 SAPI 离线合成中文语音。"""
    dest = _next_output_path("audio", ".wav")
    voice = str(params.get("voice") or "Microsoft Huihui Desktop")
    allowed = {"Microsoft Huihui Desktop", "Microsoft Huihui", "Microsoft Kangkang", "Microsoft Yaoyao"}
    if voice not in allowed:
        voice = "Microsoft Huihui Desktop"
    speed = max(.5, min(2.0, float(params.get("speed") or 1.0)))
    rate = round((speed - 1.0) * 8)
    volume = max(0, min(100, int(params.get("volume") or 100)))
    script = BASE / "h3studio" / "tts_sapi.ps1"
    process = await asyncio.create_subprocess_exec(
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
        "-Output", str(dest), "-Text", str(params.get("prompt") or ""), "-Voice", voice,
        "-Rate", str(rate), "-Volume", str(volume),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    TTS_PROCESSES[job_id] = process
    try:
        stdout, stderr = await process.communicate()
    finally:
        TTS_PROCESSES.pop(job_id, None)
    if process.returncode != 0 or not dest.exists():
        raise RuntimeError((stderr or stdout).decode("utf-8", "replace")[-500:] or "离线语音合成失败")
    output = {"kind": "audio", "file": dest.relative_to(OUT_DIR).as_posix(), "name": dest.name}
    with db() as conn:
        conn.execute("UPDATE jobs SET status='done', progress=1, outputs=?, finished_at=? WHERE id=? AND status='running'",
                     (json.dumps([output], ensure_ascii=False), time.time(), job_id))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
                               job_id: str, total_nodes: int):
    """监听 ComfyUI WebSocket 的 progress_state 事件，把真实百分比写入 DB"""
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
                        conn.execute("UPDATE jobs SET progress=?, progress_text=? WHERE id=?",
                                     (frac, text, job_id))
                elif etype == "execution_error":
                    err = str(d.get("exception_message") or d.get("exception_type") or "执行出错")[:400]
                    with db() as conn:
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
        try:
            if mode == "tts":
                await run_local_tts(job_id, params)
                print(f"[worker] 任务 {job_id} 完成: 离线语音")
                return
            async with ClientSession(timeout=HTTP_TIMEOUT) as session:
                # 上传图片素材
                image_inputs = {}
                if mode in ("flf", "pika"):
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

                params["job_id"] = job_id
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
                                    fname = await comfy_download_output(session, item, mode)
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


WORKER = Worker()

# ------------------------------------------------------------------ Routes
def json_ok(payload=None, **kw):
    payload = payload or {}
    payload.update(kw)
    return web.json_response(payload)


async def api_create(request):
    """POST /api/jobs  (multipart: mode, params(JSON), files[])"""
    reader = await request.multipart()
    mode = None
    params = {}
    saved_files = {}
    async for part in reader:
        if part.name == "mode":
            mode = (await part.text()).strip()
        elif part.name == "params":
            try:
                params = json.loads(await part.text())
            except Exception:
                params = {}
        elif part.name in ("file_first", "file_last", "file_ref"):
            fname = await save_uploaded_image(part)
            saved_files.setdefault(part.name, []).append(fname)

    if mode not in ("t2v", "flf", "r2v", "t2i", "pika", "qwen_t2i", "tts"):
        return web.json_response({"error": f"未知模式 {mode}"}, status=400)

    if mode in ("flf", "pika"):
        if saved_files.get("file_first"):
            params["first_frame"] = saved_files["file_first"][0]
        if saved_files.get("file_last"):
            params["last_frame"] = saved_files["file_last"][0]
        if mode == "flf" and not params.get("first_frame"):
            return web.json_response({"error": "首尾帧模式需要至少上传首帧图片"}, status=400)
    elif mode == "r2v":
        refs = saved_files.get("file_ref", [])
        if not refs:
            return web.json_response({"error": "参考模式需要至少 1 张参考图"}, status=400)
        params["ref_images"] = refs[:3]

    prompt = (params.get("prompt") or "").strip()
    if not prompt:
        return web.json_response({"error": "提示词不能为空"}, status=400)

    job_id = uuid.uuid4().hex[:12]
    with db() as conn:
        conn.execute(
            "INSERT INTO jobs (id, created_at, mode, params, status) VALUES (?,?,?,?, 'queued')",
            (job_id, time.time(), mode, json.dumps(params, ensure_ascii=False)))
    return json_ok({"id": job_id, "status": "queued"})


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
        encoding="utf-8", timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
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
                creationflags=subprocess.CREATE_NO_WINDOW)
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
        conditions.append("mode IN ('t2i','qwen_t2i')")
    elif media == "video":
        conditions.append("mode IN ('t2v','flf','r2v','pika')")
    elif media == "audio":
        conditions.append("mode='tts'")
    elif media == "longfilm":
        conditions.append("mode='longfilm'")
    elif media:
        return web.json_response({"error": "media 只能是 image、video、audio 或 longfilm"}, status=400)
    aspect = request.query.get("aspect", "").strip()
    if aspect:
        if aspect not in ("16:9", "9:16", "4:3", "3:4", "1:1"):
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
    return json_ok({"jobs": [row_to_job(r) for r in rows],
                    "total": total, "page": page, "page_size": page_size,
                    "running": WORKER.current})


async def api_get(request):
    jid = request.match_info["id"]
    with db() as conn:
        r = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not r:
        return web.json_response({"error": "not found"}, status=404)
    return json_ok(row_to_job(r))


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
        if r["mode"] == "tts":
            process = TTS_PROCESSES.get(jid)
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


async def api_system(request):
    """队列状态 + ComfyUI 状态"""
    with db() as conn:
        queued = conn.execute("SELECT COUNT(*) c FROM jobs WHERE status='queued'").fetchone()["c"]
    sysinfo = None
    try:
        async with ClientSession(timeout=ClientTimeout(total=3)) as s:
            async with s.get(f"{COMFY}/system_stats") as resp:
                if resp.status == 200:
                    sysinfo = await resp.json()
    except Exception:
        pass
    longfilm_running = next(iter(LONGFILM_PROCESSES), None)
    return json_ok({
        "queued": queued,
        "running": WORKER.current or longfilm_running,
        "comfyui_ok": sysinfo is not None,
        "comfyui": (sysinfo or {}).get("system") or {},
        "devices": (sysinfo or {}).get("devices") or [],
    })


async def api_unload(request):
    try:
        async with ClientSession() as s:
            await s.post(f"{COMFY}/unload")
        return json_ok({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ------------------------------------------------------------------ 硬件信息
_HW_CACHE = {"t": 0, "data": None}
_MODEL_CACHE = {"t": 0, "data": None}
psutil.cpu_percent(interval=None)  # 预热，避免首次全 0


def _cpu_name():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
        return name.strip()
    except Exception:
        return platform.processor() or "未知 CPU"


def _gpu_info():
    """优先 nvidia-smi，取不到则回退 torch/ComfyUI 信息"""
    try:
        out = subprocess.check_output(
            ["nvidia-smi",
             "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu,"
             "temperature.gpu,power.draw,power.limit,driver_version,clocks.current.graphics",
             "--format=csv,noheader,nounits"],
            encoding="utf-8", timeout=4, creationflags=subprocess.CREATE_NO_WINDOW)
        gpus = []
        for line in out.strip().splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) < 9:
                continue
            gpus.append({
                "name": p[0],
                "vram_total": float(p[1]) * 1024 * 1024,
                "vram_used": float(p[2]) * 1024 * 1024,
                "vram_free": float(p[3]) * 1024 * 1024,
                "util": float(p[4]),
                "temp": float(p[5]),
                "power": float(p[6]),
                "power_limit": float(p[7]),
                "driver": p[8],
                "clock": p[9] if len(p) > 9 else "",
            })
        return gpus
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

    data = {
        "cpu": {
            "name": _cpu_name(),
            "cores": psutil.cpu_count(logical=False),
            "threads": psutil.cpu_count(logical=True),
            "percent": psutil.cpu_percent(interval=None),
            "freq": round(psutil.cpu_freq().current) if psutil.cpu_freq() else None,
        },
        "ram": {"total": vm.total, "used": vm.used, "available": vm.available,
                "percent": vm.percent, "swap_total": sm.total, "swap_used": sm.used},
        "gpus": _gpu_info(),
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


# ------------------------------------------------------------------ static
MEDIA_TYPES = {".mp4": "video/mp4", ".webm": "video/webm", ".png": "image/png",
               ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
               ".wav": "audio/wav", ".flac": "audio/flac", ".mp3": "audio/mpeg",
               ".ogg": "audio/ogg", ".m4a": "audio/mp4"}


async def files_handler(request):
    path = request.match_info.get("path", "")
    full = (OUT_DIR / path).resolve()
    if not str(full).startswith(str(OUT_DIR.resolve())) or not full.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(full, headers={"Cache-Control": "no-cache"})


async def index_handler(request):
    return web.FileResponse(BASE / "web" / "index.html")


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
    app.router.add_get("/api/hardware", api_hardware)
    app.router.add_post("/api/unload", api_unload)
    app.router.add_get("/files/{path:.+}", files_handler)
    app.router.add_static("/static", BASE / "web")
    app.router.add_get("/", index_handler)
    return app


async def main():
    init_db()
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
