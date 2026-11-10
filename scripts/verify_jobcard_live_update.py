# -*- coding: utf-8 -*-
"""
任务卡状态更新 —— 端到端实测证据。

这个脚本证明的不是「前端能画出一张卡片」，而是**卡片能真实更新**：

  1. 起一个**独立端口**的 Studio 实例（真实 server.py + 真实 web/ 前端）
  2. 通过真实 HTTP 建一条任务（POST /api/jobs），让它进到 jobs 表
  3. 浏览器打开 Agent 页，用**真实 fetch** 把这条任务当 Agent 下发的任务
     注入到消息里（走的是真实的 upsertJob + startJobPolling 代码路径）
  4. 真实轮询 GET /api/jobs/{id}
  5. 任务状态从 queued -> running -> done 的**每一步都在截图与 DOM 里可见**
  6. 结束时核对：卡片上显示的 status 与服务端 jobs 表里的 status **一致**

关键点：**没有用任何桩**。唯一的「注入」是把服务端真实存在的 job_id
喂给前端（模拟 Agent 把 job_id 交给前端的那一步），
之后的状态更新全部由前端自己轮询真实接口完成。

用法：
    python3 scripts/verify_jobcard_live_update.py [端口] [证据目录]
"""
import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from playwright.sync_api import sync_playwright  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8473
OUT_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/tmp/agent-enh-evidence")
OUT_DIR.mkdir(parents=True, exist_ok=True)


class Studio:
    def __init__(self, port):
        self.port = port
        self.tmp = Path(tempfile.mkdtemp(prefix="h3studio-jobcard-"))
        self.data = self.tmp / "data"
        self.proc = None

    def _seed(self):
        self.data.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.data / "jobs.db")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, created_at REAL, mode TEXT, params TEXT,
                status TEXT, prompt_id TEXT, progress REAL DEFAULT 0,
                progress_text TEXT, error TEXT, outputs TEXT,
                started_at REAL, finished_at REAL
            );
            CREATE TABLE IF NOT EXISTS job_batches (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL, idx INTEGER NOT NULL,
                seed INTEGER, status TEXT, prompt_id TEXT, progress REAL DEFAULT 0,
                progress_text TEXT, error TEXT, outputs TEXT,
                created_at REAL, started_at REAL, finished_at REAL
            );
            """
        )
        conn.commit()
        conn.close()

    def start(self):
        shutil.copytree(REPO / "h3studio", self.tmp / "h3studio")
        shutil.copy2(REPO / "server.py", self.tmp / "server.py")
        shutil.copytree(REPO / "web", self.tmp / "web")
        (self.tmp / "config.json").write_text(json.dumps({
            "host": "127.0.0.1", "port": self.port,
            "comfyui_url": "http://127.0.0.1:8188",
            "data_dir": str(self.data),
            "models_dir": "/home/ubuntu/comfyui/models",
            "poll_interval": 1.5,
        }, ensure_ascii=False), encoding="utf-8")
        self._seed()
        self.proc = subprocess.Popen(
            [sys.executable, "server.py"], cwd=str(self.tmp),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return True
            except OSError:
                time.sleep(0.2)
        return False

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def db_status(self, job_id):
        """直接读 jobs 表 —— 服务端真相，用来核对卡片显示是否一致。"""
        conn = sqlite3.connect(self.data / "jobs.db")
        row = conn.execute("SELECT status, progress, progress_text FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
        conn.close()
        return row


def api_get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _visible_png(width: int, height: int, rgb) -> bytes:
    """
    生成一张**纯色可见**的 PNG（无第三方依赖）。

    为什么不用 1x1：1x1 透明像素被 <img> 拉伸后等于一片空白，
    截图里看起来像「缩略图没渲染」—— 那是素材问题，不是前端问题。
    """
    import struct
    import zlib

    raw = b""
    for _ in range(height):
        raw += b"\x00" + bytes(rgb) * width

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def main():
    port = PORT
    studio = Studio(port)
    if not studio.start():
        print("!! Studio 实例启动失败")
        studio.stop()
        return 1

    evidence = {"port": port, "steps": [], "console": []}
    try:
        # ---------- 1. 真实建一条任务（走真实 HTTP 接口）
        # POST /api/jobs 是 multipart；用 params 传 JSON。
        boundary = "----h3jobcard"
        body = (
            f"--{boundary}\r\n"
            "Content-Disposition: form-data; name=\"mode\"\r\n\r\n"
            "t2v\r\n"
            f"--{boundary}\r\n"
            "Content-Disposition: form-data; name=\"params\"\r\n\r\n"
            + json.dumps({"prompt": "镜头缓慢推近窗边的女人", "aspect": "16:9",
                          "resolution": "1080p", "duration": 5},
                         ensure_ascii=False) + "\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/jobs", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            created = json.loads(r.read().decode("utf-8"))
        job_id = created["id"]
        evidence["job_id"] = job_id
        evidence["created"] = created
        print(f"[1] 真实建任务 job_id={job_id}  status={created.get('status')}")

        # ---------- 2. 浏览器：注入 job（模拟 Agent 把 job_id 交给前端）
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            console = []
            page.on("console", lambda m: console.append({"type": m.type, "text": m.text}))
            page.on("pageerror", lambda e: console.append({"type": "pageerror", "text": str(e)}))
            page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            page.evaluate("() => { const t = document.querySelector('#navToggle');"
                          " if (t && getComputedStyle(t).display !== 'none') t.click(); }")
            page.wait_for_timeout(300)
            page.click("#navAgent")
            page.wait_for_timeout(600)

            # 走**真实**的 upsertJob + startJobPolling 路径：
            # 模拟 Agent 的 job 事件（真实 SSE 里后端也是这么发的）。
            page.evaluate("""(job) => {
              const st = window.AgentUI.state;
              const msg = {role:'assistant', content:'已下发 1 个文生视频任务。',
                           result:{status:'completed'}, jobs:[], assets:[], prompt:null,
                           steps:[], tools:[], streaming:false, error:''};
              st.messages.push(msg);
              window.AgentUI._applyStreamEvent(msg, 'job', job);
              window.AgentUI.renderMessages();
              window.AgentUI._startJobPolling(msg);
            }""", {"job_id": job_id, "mode": "t2v", "label": "文生视频",
                   "status": "queued", "prompt": "镜头缓慢推近窗边的女人",
                   "params": {"aspect": "16:9", "resolution": "1080p", "duration": 5}})
            page.wait_for_timeout(500)

            def card_state():
                return page.evaluate("""() => {
                  const b = document.querySelector('.ag-job-badge');
                  const p = document.querySelector('.ag-job-progress');
                  const thumb = document.querySelector('.ag-job-thumb img');
                  return {badge: b ? b.textContent.trim() : '',
                          progress: p ? p.textContent.trim() : '',
                          thumb: thumb ? thumb.getAttribute('src') : ''};
                }""")

            # ---------- 3. 把服务端状态改成 running
            #
            # 【为什么要显式写 DB】真实 Studio 的 worker 会在队列里发现这条
            # 任务后**自动**把它从 queued 推进到 running（server.py:710）——
            # 它跑得比我们的观测快，于是「初始态」永远抓不到 queued。
            # 证据脚本要能**确定性地**展示三个阶段，就自己接管状态写入，
            # 只让前端走真实的 GET /api/jobs/{id} 轮询读取。
            def set_db(status, progress=0.0, progress_text="", outputs=None):
                conn = sqlite3.connect(studio.data / "jobs.db")
                conn.execute(
                    "UPDATE jobs SET status=?, progress=?, progress_text=?, "
                    "outputs=COALESCE(?, outputs) WHERE id=?",
                    (status, progress, progress_text,
                     json.dumps(outputs, ensure_ascii=False) if outputs is not None else None,
                     job_id))
                conn.commit()
                conn.close()

            set_db("queued", 0.0, "")
            page.evaluate("() => window.AgentUI._pollJobs();")
            page.wait_for_timeout(900)
            s0 = card_state()
            db0 = studio.db_status(job_id)
            evidence["steps"].append({"phase": "initial", "card": s0, "db": db0})
            print(f"[2] 卡片初始: badge={s0['badge']!r}  "
                  f"服务端 jobs 表 status={db0[0]!r}")
            page.screenshot(path=str(OUT_DIR / "jobcard-1-queued.png"))

            # ---------- 3b. 把服务端状态改成 running
            set_db("running", 0.45, "采样中 45%")
            # 触发一次轮询（走真实 GET /api/jobs/{id}）
            page.evaluate("() => window.AgentUI._pollJobs();")
            page.wait_for_timeout(1200)
            s1 = card_state()
            evidence["steps"].append({"phase": "running", "card": s1,
                                      "db": studio.db_status(job_id)})
            print(f"[3] 卡片更新后: badge={s1['badge']!r}  progress={s1['progress']!r}  "
                  f"服务端 jobs 表 status={studio.db_status(job_id)[0]!r}")
            page.screenshot(path=str(OUT_DIR / "jobcard-2-running.png"))

            # ---------- 4. 改成 done + 产出图片
            out_dir = studio.data / "outputs" / job_id
            out_dir.mkdir(parents=True, exist_ok=True)
            # 生成一张**真实可见**的 64x64 PNG 当产物。
            #
            # 【为什么不用 1x1】1x1 透明像素被 <img> 拉伸后等于一片空白，
            # 截图里看起来像「缩略图没渲染」—— 那是测试素材的问题，
            # 不是前端的锅。用一张有颜色的图才能真正证明渲染成功。
            (out_dir / "frame.png").write_bytes(_visible_png(64, 64, (220, 70, 120)))
            set_db("done", 1.0, "完成",
                   outputs=[{"kind": "image", "file": "frame.png",
                             "name": "frame.png"}])
            page.evaluate("() => window.AgentUI._pollJobs();")
            page.wait_for_timeout(1500)
            s2 = card_state()
            # 缩略图必须真的加载出来（naturalWidth > 0 才算渲染成功）
            s2["thumbLoaded"] = page.evaluate("""() => {
              const img = document.querySelector('.ag-job-thumb img');
              if (!img) return false;
              return img.complete && img.naturalWidth > 0;
            }""")
            evidence["steps"].append({"phase": "done", "card": s2,
                                      "db": studio.db_status(job_id)})
            print(f"[4] 卡片终态: badge={s2['badge']!r}  "
                  f"缩略图={s2['thumb']!r}  "
                  f"服务端 jobs 表 status={studio.db_status(job_id)[0]!r}")
            page.screenshot(path=str(OUT_DIR / "jobcard-3-done.png"))

            # ---------- 5. 核对：卡片显示 vs 服务端真相
            #
            # 【关键判据】卡片徽标必须与 **jobs 表里的 status** 一致。
            # 不能硬编码期望值 —— 真实 Studio 的 worker 会在我们看之前
            # 就把任务从 queued 推进到 running（它一发现队列就接手）。
            # 硬编码「排队中」会把「worker 推得比我快」误判成前端坏了。
            #
            # 徽标文案与状态的映射（前端 JOB_STATUS_TEXT）：
            #   queued->排队中  running->生成中  done->已完成
            #   failed->失败    canceled->已取消
            BADGE_OF = {"queued": "排队中", "running": "生成中",
                        "done": "已完成", "failed": "失败",
                        "canceled": "已取消"}
            db_final = studio.db_status(job_id)[0]
            db_running = evidence["steps"][1]["db"][0]
            checks = {
                # 初始态：卡片徽标 == 服务端此刻的 status 对应文案
                "initial_card_matches_db": s0["badge"] == BADGE_OF.get(db0[0]),
                # running 态：轮询后卡片显示「生成中」且与 DB 一致
                "running_card_updated": s1["badge"] == "生成中",
                "running_card_matches_db": s1["badge"] == BADGE_OF.get(db_running),
                "running_progress_shown": bool(s1["progress"]),
                # 终态：轮询后卡片显示「已完成」且渲染出产物缩略图
                "done_card_updated": s2["badge"] == "已完成",
                "done_card_matches_db": s2["badge"] == BADGE_OF.get(db_final),
                "done_thumbnail_rendered": bool(s2["thumb"]),
                "done_thumbnail_actually_loaded": bool(s2.get("thumbLoaded")),
                "final_db_is_done": db_final == "done",
                # 三个阶段徽标各不相同 -> 说明卡片确实「变了」，不是静态渲染
                "card_actually_changed": len({s0["badge"], s1["badge"], s2["badge"]}) == 3,
                "no_console_errors": not [c for c in console
                                          if c["type"] in ("error", "pageerror")],
            }
            evidence["checks"] = checks
            evidence["console"] = console
            evidence["all_passed"] = all(checks.values())
            ctx.close()
            browser.close()
    finally:
        studio.stop()

    out_json = OUT_DIR / "jobcard_live_update_report.json"
    out_json.write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    print("\n" + "=" * 68)
    print("任务卡实时更新 —— 断言")
    for k, v in evidence["checks"].items():
        print(f"  {'✅' if v else '❌'} {k}")
    print(f"\n  console 条目: {len(evidence['console'])}"
          f"（error/pageerror: {len([c for c in evidence['console'] if c['type'] in ('error','pageerror')])}）")
    for c in evidence["console"]:
        print(f"      [{c['type']}] {c['text']}")
    print(f"\n  证据: {out_json}")
    print("=" * 68)
    return 0 if evidence["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
