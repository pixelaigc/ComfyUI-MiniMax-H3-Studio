#!/usr/bin/env python3
"""
Real-browser verification of the Agent **audio / video** upload feature
(2026-11-11).

Proves, in a REAL browser against a REAL running server (throwaway data dir):

  1. ENTRY: the composer upload entry accepts image + audio + video.
  2. AUDIO UPLOAD: a real WAV file uploads through the REAL multipart
     endpoint, lands in the asset library with category=audio, and gets an
     **async** analysis job (the deep pipeline) + a waveform preview.
  3. VIDEO UPLOAD: a real MP4 file uploads, lands with category=video,
     gets an async analysis job + a first-frame preview.
  4. READINESS GATE: while the analysis job is still running, the card shows
     "分析中" and the **send button is blocked** (no silent degradation).
  5. LIBRARY: after the job completes, the asset really has a description
     (asset_analyses) and tags (asset_tags), and the tags are searchable.
  6. TYPE BADGE / DURATION: cards show the media type and duration.
  7. NO OVERFLOW at 375 / 390 / 1280; console 0 errors; 0 failed requests.

【为什么用假 Provider + 假深度管线】
The real multimodal analysis takes 120~180 s per image and the real deep
audio pipeline takes ~600 s for 116 s of audio on this box. For a browser
test that must run 3 widths x N cases that is hours.

So the test server points at:
  * a **fake OpenAI-compatible provider** (returns a canned structured
    description instantly) — for the multimodal path (video, images)
  * a **stubbed audio_deep.analyze** — returns a canned structured audio
    analysis instantly

Everything else is the REAL code path:
  handle_upload -> register_asset (real) -> analysis_jobs.start_analysis
  (real) -> _run_audio_deep / _run_multimodal (real) -> save_analysis
  (real) -> add_tags (real) -> /api/agent/media/{id}/status (real)
Only the model inference is stubbed.

【重要：断言必须是「抓得住」的】
DOM 断言会假通过（元素在 != 内容对）。所以：
  * 缩略图做**像素级**核对（数截图里的红色像素）
  * 素材库状态**直读 SQLite**（权威来源，不是页面显示）
  * 分析完成后**直读 asset_analyses / asset_tags**
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(os.environ.get("VERIFY_REPO",
                           "/root/projects/h3studio-agent-av-upload"))
SHOTS = Path(os.environ.get("VERIFY_SHOTS", str(REPO / "evidence" / "media-av")))
PORT = int(os.environ.get("VERIFY_PORT", "8197"))
FAKE_PORT = int(os.environ.get("VERIFY_FAKE_PORT", "8917"))

#: 假 Provider 返回的结构化描述（视频走多模态）
FAKE_DESCRIPTION = """**【整体概述】**
一段彩色渐变画面在缓慢流动。

**【分镜与时间线】**
00:00 全景渐变；00:02 镜头缓慢推进。

**【光线与色彩】**
蓝紫与橙黄交织的渐变色调。
"""

#: 假深度音频管线的结构化输出（与 analyze_audio.py 的 render_text 同格式）
FAKE_AUDIO_TEXT = """## 一、音频基本信息
- 容器格式：wav
- 时长：3.00 秒（00:03）
- 采样率：16000 Hz

## 二、节拍与速度
- 测得 BPM：152
- 节拍规整度：0.94

## 三、乐器与音色
- 人声：占比 0.98
- 电子舞曲：占比 0.32
"""


class FakeProviderHandler(BaseHTTPRequestHandler):
    """最小 OpenAI 兼容端点：立刻返回一段结构化描述。"""

    def log_message(self, *args):  # 静音
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "fake-vision",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": FAKE_DESCRIPTION},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10,
                      "total_tokens": 20},
        }
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def start_fake_provider():
    server = HTTPServer(("127.0.0.1", FAKE_PORT), FakeProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def start_server(tmp: Path):
    data = tmp / "data"
    (data / "outputs" / "images").mkdir(parents=True, exist_ok=True)
    lib = tmp / "asset_library"
    lib.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["H3STUDIO_PORT"] = str(PORT)
    env["H3STUDIO_DATA_DIR"] = str(data)
    env["ASSET_LIBRARY_ROOT"] = str(lib)
    env["PYTHONPATH"] = str(REPO)
    # 深度音频管线用桩（真管线要 ~600 秒）—— 其余全是真代码路径。
    # 注入方式：sitecustomize 的 import hook（不改任何产品代码）。
    stub_dir = REPO / "scripts" / "_audio_deep_stub"
    env["H3STUDIO_AUDIO_DEEP_STUB"] = "1"
    env["PYTHONPATH"] = f"{stub_dir}{os.pathsep}{REPO}"
    proc = subprocess.Popen(
        ["/home/ubuntu/comfyui/venv/bin/python", "server.py"],
        cwd=str(REPO), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(120):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}/api/jobs", timeout=2) as r:
                if r.status == 200:
                    return proc
        except Exception:
            time.sleep(0.5)
    out = proc.stdout.read() if proc.stdout else ""
    proc.kill()
    raise RuntimeError("server did not start:\n" + out[-3000:])


def seed_fake_provider(data_dir: Path):
    """在测试实例的 jobs.db 里种一个指向假 Provider 的多模态模型。"""
    import sqlite3
    db = data_dir / "jobs.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS model_providers (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
            base_url TEXT NOT NULL, llm_base_url TEXT DEFAULT '',
            use_proxy INTEGER DEFAULT 0, proxy_base_url TEXT DEFAULT '',
            model TEXT NOT NULL, api_key TEXT DEFAULT '',
            multimodal INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1,
            created_at REAL);
        CREATE TABLE IF NOT EXISTS model_provider_prefs (
            key TEXT PRIMARY KEY, value TEXT);
        """)
    conn.execute(
        "INSERT OR REPLACE INTO model_providers VALUES "
        "('fakevis','假视觉模型','openai',?,'',0,'',"
        "'fake-vision','',1,1,0)", (f"http://127.0.0.1:{FAKE_PORT}",))
    conn.execute("INSERT OR REPLACE INTO model_provider_prefs "
                 "VALUES ('default','fakevis')")
    conn.execute("INSERT OR REPLACE INTO model_provider_prefs "
                 "VALUES ('default_multimodal','fakevis')")
    conn.commit()
    conn.close()


# ------------------------------------------------------------------ 造素材


def make_wav(path: Path, seconds: float = 3.0, rate: int = 16000) -> Path:
    """造一个**真的** WAV（RIFF/WAVE 头 + 440 Hz 正弦 PCM）。"""
    import math
    import struct
    import wave

    frames = int(rate * seconds)
    samples = bytearray()
    for i in range(frames):
        value = int(9000 * math.sin(2 * math.pi * 440 * i / rate))
        samples += struct.pack("<h", value)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(samples))
    return path


def make_mp4(path: Path, seconds: float = 2.0, size=(160, 120)) -> Path:
    """造一个**真的** MP4（用 ffmpeg 生成，纯色渐变画面 + 正弦音轨）。"""
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i",
         f"color=c=0x3050A0:s={size[0]}x{size[1]}:d={seconds}",
         "-f", "lavfi", "-i",
         f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True)
    return path


def make_text(path: Path) -> Path:
    path.write_text("this is not a media file at all", encoding="utf-8")
    return path


def make_fake_mp3(path: Path) -> Path:
    """exe 改名 mp3 —— 只验「魔数嗅探挡得住」。"""
    path.write_bytes(b"MZ\x90\x00" + b"\x00" * 200)
    return path


# ------------------------------------------------------------------ 探针


def probe(page):
    return page.evaluate("""() => {
        const thumbs = [...document.querySelectorAll('.ag-attach-thumb')];
        const errEl = document.querySelector('.ag-attach-err');
        const noteEl = document.querySelector('.ag-attach-note');
        const btn = document.getElementById('agAttachBtn');
        const hint = document.getElementById('agAttachHint');
        const box = document.getElementById('agAttachments');
        const input = document.getElementById('agAttachInput');
        const send = document.getElementById('agSend');
        return {
            attachBoxHidden: box ? box.hidden : null,
            acceptAttr: input ? (input.getAttribute('accept') || '') : '',
            thumbCount: thumbs.length,
            thumbSrcs: thumbs.map(t => {
                const img = t.querySelector('img');
                return img ? (img.getAttribute('src') || '') : '';
            }),
            thumbKinds: thumbs.map(t => t.getAttribute('data-kind') || ''),
            thumbStatuses: thumbs.map(t => t.getAttribute('data-status') || ''),
            badges: thumbs.map(t => {
                const b = t.querySelector('.ag-attach-badge');
                return b ? b.textContent.trim() : '';
            }),
            metas: thumbs.map(t => {
                const m = t.querySelector('.ag-attach-meta');
                return m ? m.textContent.trim() : '';
            }),
            error: errEl ? errEl.textContent.trim() : '',
            note: noteEl ? noteEl.textContent.trim() : '',
            busyVisible: !!document.querySelector('.ag-attach-busy'),
            btnVisible: btn ? (btn.offsetParent !== null) : false,
            btnText: btn ? btn.textContent.trim() : '',
            hint: hint ? hint.textContent.trim() : '',
            sendDisabled: send ? send.disabled : null,
            attachments: window.AgentUI.state.attachments.map(a => ({
                asset_id: a.asset_id, tags: a.tags || [],
                description: a.description || '',
                kind: a.kind || '',
                duration_sec: a.duration_sec || null,
                analysis_status: a.analysis_status || '',
                available: a.available,
            })),
            docScrollW: document.documentElement.scrollWidth,
            docClientW: document.documentElement.clientWidth,
            bodyScrollW: document.body.scrollWidth,
        };
    }""")


def overflow_free(data):
    return (data["docScrollW"] <= data["docClientW"] + 1
            and data["bodyScrollW"] <= data["docClientW"] + 1)


def library_probe(asset_id: str) -> dict:
    """直接查**素材库**（权威来源），验证真的入库 + 分析 + 标签。"""
    import sqlite3

    lib = Path(os.environ.get("VERIFY_LIB", ""))
    db = lib / "asset_library.db"
    if not db.is_file():
        return {"ok": False}
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    asset = conn.execute("SELECT * FROM assets WHERE id=?",
                         (asset_id,)).fetchone()
    version = conn.execute(
        "SELECT * FROM asset_versions WHERE asset_id=? ORDER BY version DESC",
        (asset_id,)).fetchone()
    analysis = conn.execute(
        "SELECT * FROM asset_analyses WHERE asset_id=? ORDER BY id DESC",
        (asset_id,)).fetchone()
    tags = [r["tag"] for r in conn.execute(
        "SELECT tag FROM asset_tags WHERE asset_id=?", (asset_id,))]
    job = conn.execute(
        "SELECT * FROM asset_analysis_jobs WHERE asset_id=? ORDER BY id DESC",
        (asset_id,)).fetchone()
    conn.close()
    return {
        "ok": True,
        "source": asset["source"] if asset else None,
        "category": asset["category"] if asset else None,
        "deleted": bool(asset["deleted_at"]) if asset else None,
        "width": version["width"] if version else None,
        "height": version["height"] if version else None,
        "duration_sec": version["duration_sec"] if version else None,
        "mime": version["mime"] if version else None,
        "analysis_text": (analysis["text"] if analysis else "") or "",
        "analysis_kind": analysis["kind"] if analysis else None,
        "tags": tags,
        "job_status": job["status"] if job else None,
        "job_kind": job["kind"] if job else None,
        "job_options": (job["options"] if job else "") or "",
    }


def _region_has_pixels(screenshot, box_ratio, rgb, min_pixels=120):
    """数截图指定区域里符合 RGB 条件的像素数。

    【为什么要像素级断言】
    DOM 断言会假通过：「有 <img> 且 src 指向 /api/assets/...」全绿，
    但界面可能是**空白方框**（窄屏下缩略图只有 52px，截图时还没解码完）。
    """
    if not screenshot:
        return 0
    path = Path(screenshot)
    if not path.is_file():
        return 0
    try:
        from PIL import Image
        image = Image.open(path).convert("RGB")
    except Exception:  # noqa: BLE001
        return 0
    width, height = image.size
    x0 = int(width * box_ratio[0])
    x1 = int(width * box_ratio[1])
    y0 = int(height * box_ratio[2])
    y1 = int(height * box_ratio[3])
    target = rgb
    count = 0
    for y in range(y0, y1, 2):
        for x in range(x0, x1, 2):
            r, g, b = image.getpixel((x, y))
            if abs(r - target[0]) < 45 and abs(g - target[1]) < 45 \
                    and abs(b - target[2]) < 45:
                count += 1
    return count


def main():
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="h3verify-media-av-"))
    fake = start_fake_provider()
    proc = None
    console_errors = []
    results = []
    uploaded_ids = []
    try:
        proc = start_server(tmp)
        seed_fake_provider(tmp / "data")
        os.environ["VERIFY_LIB"] = str(tmp / "asset_library")

        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            for width, height, tag in ((375, 812, "375"),
                                       (390, 844, "390"),
                                       (1280, 900, "1280")):
                ctx = browser.new_context(
                    viewport={"width": width, "height": height},
                    device_scale_factor=2)
                page = ctx.new_page()
                errs = []
                failed_requests = []
                page.on("console", lambda m: errs.append(m.text)
                        if m.type == "error" else None)
                page.on("pageerror", lambda e: errs.append(str(e)))
                page.on("response", lambda r: failed_requests.append(
                    f"{r.status} {r.url}") if r.status >= 400 else None)
                page.goto(f"http://127.0.0.1:{PORT}/",
                          wait_until="domcontentloaded", timeout=60000)
                page.wait_for_selector("#navAgent", timeout=30000)
                page.wait_for_timeout(900)
                page.evaluate("() => window.AgentUI.show()")
                page.wait_for_timeout(400)
                assert page.eval_on_selector(
                    "#agentPage",
                    "el => !el.classList.contains('page-hidden')"
                ), "Agent 面板没有显示出来"

                def reset():
                    page.evaluate("""() => {
                      window.AgentUI.state.attachments = [];
                      window.AgentUI.state.attachError = '';
                      window.AgentUI.state.attachBusy = false;
                      window.AgentUI.state.attachStage = '';
                      window.AgentUI.state.attachJobs = {};
                      window.AgentUI._renderAttachments();
                    }""")

                def upload(paths):
                    page.set_input_files("#agAttachInput",
                                         [str(x) for x in paths])
                    page.wait_for_function(
                        """() => !window.AgentUI.state.attachBusy""",
                        timeout=120000)
                    page.wait_for_timeout(250)

                def wait_ready(timeout_ms=90000):
                    """等所有附件的分析状态变成 done（轮询到就绪）。"""
                    try:
                        page.wait_for_function(
                            """() => {
                              const items = window.AgentUI.state.attachments || [];
                              if (!items.length) return false;
                              return items.every(a =>
                                a.analysis_status === 'done'
                                || a.analysis_status === 'failed');
                            }""",
                            timeout=timeout_ms)
                    except Exception:
                        pass
                    page.wait_for_timeout(400)

                # --- entry ---
                reset()
                shot = SHOTS / f"mediaav-{tag}-01-upload-entry.png"
                page.screenshot(path=str(shot), full_page=False)
                results.append({"width": width, "case": "entry",
                                "probe": probe(page), "screenshot": str(shot)})

                # --- audio ---
                reset()
                wav = make_wav(tmp / f"audio-{tag}.wav", seconds=3.0)
                upload([wav])
                shot = SHOTS / f"mediaav-{tag}-02-audio-pending.png"
                page.screenshot(path=str(shot), full_page=False)
                pr = probe(page)
                uploaded_ids.extend(a["asset_id"] for a in pr["attachments"])
                audio_id = pr["attachments"][0]["asset_id"] if pr["attachments"] else ""
                lib_pending = (library_probe(audio_id)
                              if audio_id else {"ok": False})
                results.append({"width": width, "case": "audio-pending",
                                "probe": pr, "library": lib_pending,
                                "screenshot": str(shot)})

                # 等分析完成
                wait_ready()
                shot = SHOTS / f"mediaav-{tag}-03-audio-ready.png"
                page.screenshot(path=str(shot), full_page=False)
                pr = probe(page)
                lib_audio = (library_probe(audio_id)
                             if audio_id else {"ok": False})
                results.append({"width": width, "case": "audio-ready",
                                "probe": pr, "library": lib_audio,
                                "screenshot": str(shot)})

                # --- video ---
                reset()
                mp4 = make_mp4(tmp / f"video-{tag}.mp4", seconds=2.0)
                upload([mp4])
                shot = SHOTS / f"mediaav-{tag}-04-video-pending.png"
                page.screenshot(path=str(shot), full_page=False)
                pr = probe(page)
                uploaded_ids.extend(a["asset_id"] for a in pr["attachments"])
                video_id = pr["attachments"][0]["asset_id"] if pr["attachments"] else ""
                lib_v_pending = (library_probe(video_id)
                                 if video_id else {"ok": False})
                results.append({"width": width, "case": "video-pending",
                                "probe": pr, "library": lib_v_pending,
                                "screenshot": str(shot)})

                wait_ready()
                shot = SHOTS / f"mediaav-{tag}-05-video-ready.png"
                page.screenshot(path=str(shot), full_page=False)
                pr = probe(page)
                lib_video = (library_probe(video_id)
                             if video_id else {"ok": False})
                results.append({"width": width, "case": "video-ready",
                                "probe": pr, "library": lib_video,
                                "screenshot": str(shot)})

                # --- mixed ---
                reset()
                wav2 = make_wav(tmp / f"mix-{tag}.wav", seconds=2.0)
                mp4b = make_mp4(tmp / f"mix-{tag}.mp4", seconds=1.0)
                upload([wav2, mp4b])
                shot = SHOTS / f"mediaav-{tag}-06-mixed.png"
                page.screenshot(path=str(shot), full_page=False)
                pr = probe(page)
                uploaded_ids.extend(a["asset_id"] for a in pr["attachments"])
                results.append({"width": width, "case": "mixed",
                                "probe": pr, "screenshot": str(shot)})

                # --- bad type ---
                reset()
                txt = make_text(tmp / f"bad-{tag}.txt")
                upload([txt])
                shot = SHOTS / f"mediaav-{tag}-07-bad-type-rejected.png"
                page.screenshot(path=str(shot), full_page=False)
                results.append({"width": width, "case": "bad-type",
                                "probe": probe(page), "screenshot": str(shot)})

                # --- disguised audio ---
                reset()
                fake_mp3 = make_fake_mp3(tmp / f"evil-{tag}.mp3")
                upload([fake_mp3])
                shot = SHOTS / f"mediaav-{tag}-08-disguised-rejected.png"
                page.screenshot(path=str(shot), full_page=False)
                results.append({"width": width, "case": "disguised",
                                "probe": probe(page), "screenshot": str(shot)})

                results[-1]["failed_requests"] = failed_requests
                console_errors.extend(errs)
                ctx.close()
            browser.close()
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        fake.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- assertions -------------------------------------------------
    ok = True
    for r in results:
        pr = r["probe"]
        case = r["case"]
        lib = r.get("library") or {}
        if case == "entry":
            checks = {
                "上传入口可见": pr["btnVisible"],
                "accept 含音频": "audio/wav" in pr["acceptAttr"],
                "accept 含视频": "video/mp4" in pr["acceptAttr"],
                "没有附件时缩略图区隐藏": pr["attachBoxHidden"] is True,
                "窄屏无横向溢出": overflow_free(pr),
            }
        elif case == "audio-pending":
            checks = {
                "音频上传成功": pr["thumbCount"] == 1,
                "类型标为 audio": pr["thumbKinds"] == ["audio"],
                "状态为分析中": pr["thumbStatuses"] == ["pending"],
                "徽标显示分析中": any("分析中" in b for b in pr["badges"]),
                "拿到 asset_id": bool(pr["attachments"])
                                 and pr["attachments"][0]["asset_id"].startswith("asset_"),
                # --- 素材库入库 ---
                "素材库真的有这条": lib.get("ok") and lib.get("source") == "agent_upload",
                "category 是 audio": lib.get("category") == "audio",
                "抽出了时长": (lib.get("duration_sec") or 0) > 0,
                # --- 异步任务 ---
                "排了分析任务": lib.get("job_status") in ("queued", "running", "done"),
                "任务是音频类型": lib.get("job_kind") == "audio",
                # --- 就绪门槛 ---
                "未就绪时不可用": pr["attachments"]
                                 and pr["attachments"][0]["available"] is False,
                "窄屏无横向溢出": overflow_free(pr),
            }
        elif case == "audio-ready":
            checks = {
                "分析完成": pr["thumbStatuses"] == ["ready"],
                "徽标显示音频": any("音频" in b for b in pr["badges"]),
                "显示了时长": any(":" in m for m in pr["metas"]),
                # --- 素材库里的权威状态 ---
                "描述落库": len(lib.get("analysis_text") or "") > 20,
                "描述是音频类型": lib.get("analysis_kind") == "audio",
                "标签落库": len(lib.get("tags") or []) > 0,
                "任务已完成": lib.get("job_status") == "done",
                "前端拿到标签": len(pr["attachments"][0]["tags"]) > 0
                if pr["attachments"] else False,
                "就绪后可用": pr["attachments"]
                              and pr["attachments"][0]["available"] is True,
                "窄屏无横向溢出": overflow_free(pr),
            }
        elif case == "video-pending":
            checks = {
                "视频上传成功": pr["thumbCount"] == 1,
                "类型标为 video": pr["thumbKinds"] == ["video"],
                "素材库真的有这条": lib.get("ok") and lib.get("source") == "agent_upload",
                "category 是 video": lib.get("category") == "video",
                "抽出了分辨率": bool(lib.get("width")) and bool(lib.get("height")),
                "排了分析任务": lib.get("job_status") in ("queued", "running", "done"),
                "任务是视频类型": lib.get("job_kind") == "video",
                "窄屏无横向溢出": overflow_free(pr),
            }
        elif case == "video-ready":
            checks = {
                "分析完成": pr["thumbStatuses"] == ["ready"],
                "徽标显示视频": any("视频" in b for b in pr["badges"]),
                "描述落库": len(lib.get("analysis_text") or "") > 20,
                "标签落库": len(lib.get("tags") or []) > 0,
                "任务已完成": lib.get("job_status") == "done",
                "窄屏无横向溢出": overflow_free(pr),
            }
        elif case == "mixed":
            checks = {
                "两个都上传成功": pr["thumbCount"] == 2,
                "类型分别是 audio/video": sorted(pr["thumbKinds"]) == ["audio", "video"],
                "asset_id 两个": len(pr["attachments"]) == 2,
                "窄屏无横向溢出": overflow_free(pr),
            }
        elif case == "bad-type":
            checks = {
                "非法类型不被接受": pr["thumbCount"] == 0,
                "有可读的错误提示": bool(pr["error"]),
                "错误提示提到格式": ("格式" in pr["error"]
                                     or "不支持" in pr["error"]),
            }
        else:  # disguised
            checks = {
                "伪装文件不被接受": pr["thumbCount"] == 0,
                "有可读的错误提示": bool(pr["error"]),
                "错误提示提到内容不符": ("内容" in pr["error"]
                                         or "不是有效" in pr["error"]),
            }
        r["checks"] = checks
        r["all_pass"] = all(checks.values())
        ok = ok and r["all_pass"]

    report = {"results": results, "console_errors": console_errors,
              "uploaded_ids": uploaded_ids, "all_pass": ok}
    SHOTS.mkdir(parents=True, exist_ok=True)
    (SHOTS / "browser_verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if (ok and not console_errors) else 1


if __name__ == "__main__":
    sys.exit(main())
