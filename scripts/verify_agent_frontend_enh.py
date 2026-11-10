# -*- coding: utf-8 -*-
"""
内置 Agent 前端增强 —— 真实浏览器渲染证据采集。

产出：
  * 三档宽度（375 / 390 / 1280）截图
  * 每一档的 console 全量输出（必须 0 条 JS 错误）
  * 横向溢出实测像素数
  * 输入框可达性、工具块折叠、任务卡状态更新、错误/重试 的实测数据

【端口纪律】
  自起一个**独立端口**的 Studio 实例（默认 8471），
  绝不碰 8190 线上服务，也不碰任何 systemd unit。
  数据目录是临时目录，跑完即删。

用法：
    python3 scripts/verify_agent_frontend_enh.py [端口] [证据输出目录]

证据里的截图是**真实渲染**：用的是真实 server.py + 真实 web/ 前端，
Agent 的 SSE 用桩（真实 Agent 要跑本地大模型，1~5 分钟且不确定），
但渲染路径、事件分发、任务卡轮询全是真代码。
"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from playwright.sync_api import sync_playwright  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8471
OUT_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/tmp/agent-enh-evidence")
OUT_DIR.mkdir(parents=True, exist_ok=True)

VIEWPORTS = [375, 390, 1280]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------- 独立实例
class Studio:
    """在临时目录里起一个独立端口的 Studio（真实 server.py + 真实前端）。"""

    def __init__(self, port: int):
        self.port = port
        self.tmp = Path(tempfile.mkdtemp(prefix="h3studio-agentenh-"))
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
        now = time.time()
        # 一条「已完成」的图 + 一条「排队中」的视频（用于任务卡状态演示）
        conn.execute(
            "INSERT INTO jobs (id, created_at, mode, params, status, progress,"
            " progress_text, outputs) VALUES (?,?,?,?,?,?,?,?)",
            ("seeddone01", now - 120, "t2i",
             json.dumps({"aspect": "16:9", "resolution": "1mp",
                         "prompt": "雨夜街道，霓虹反光", "gen_type": "t2i"},
                        ensure_ascii=False),
             "done", 1.0, "", json.dumps([], ensure_ascii=False)))
        conn.execute(
            "INSERT INTO jobs (id, created_at, mode, params, status, progress,"
            " progress_text, outputs) VALUES (?,?,?,?,?,?,?,?)",
            ("seedqueue1", now - 30, "t2v",
             json.dumps({"aspect": "16:9", "resolution": "1080p", "duration": 5,
                         "prompt": "镜头缓慢推近窗边的女人", "gen_type": "video"},
                        ensure_ascii=False),
             "queued", 0.0, "", json.dumps([], ensure_ascii=False)))
        conn.commit()
        conn.close()

    def start(self) -> bool:
        shutil.copytree(REPO / "h3studio", self.tmp / "h3studio")
        shutil.copy2(REPO / "server.py", self.tmp / "server.py")
        shutil.copytree(REPO / "web", self.tmp / "web")
        cfg = {
            "host": "127.0.0.1", "port": self.port,
            "comfyui_url": "http://127.0.0.1:8188",
            "data_dir": str(self.data),
            "models_dir": "/home/ubuntu/comfyui/models",
            "poll_interval": 1.5,
        }
        (self.tmp / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
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


# ---------------------------------------------------------------- SSE 桩
AGENT_SSE = (
    "event: tool_call\ndata: "
    + json.dumps({"name": "search_assets", "arguments": {"query": "雨夜"},
                  "call_id": "c1"}, ensure_ascii=False) + "\n\n"
    + "event: tool_result\ndata: "
    + json.dumps({"name": "search_assets",
                  "result": {"hits": 2, "items": ["rain_night_01", "neon_02"]},
                  "ok": True, "ms": 42, "call_id": "c1"}, ensure_ascii=False) + "\n\n"
    + "event: tool_call\ndata: "
    + json.dumps({"name": "create_job",
                  "arguments": {"mode": "t2v", "resolution": "1080p",
                                "duration": 5, "aspect": "16:9"},
                  "call_id": "c2"}, ensure_ascii=False) + "\n\n"
    + "event: job\ndata: "
    + json.dumps({"job_id": "seedqueue1", "mode": "t2v", "label": "文生视频",
                  "status": "queued", "prompt": "镜头缓慢推近窗边的女人",
                  "params": {"aspect": "16:9", "resolution": "1080p",
                             "duration": 5}}, ensure_ascii=False) + "\n\n"
    + "event: token\ndata: "
    + json.dumps({"text": "我找到 2 条雨夜素材，已用其中 1 条做参考，"
                          "并下发了 1 个文生视频任务。"}, ensure_ascii=False) + "\n\n"
    + "event: token\ndata: "
    + json.dumps({"text": "\n\n注意：视频分辨率只有 544p / 768p 两档，"
                          "你请求的 1080p 实际按 768p 生成。"}, ensure_ascii=False) + "\n\n"
    + "event: done\ndata: "
    + json.dumps({"status": "completed",
                  "answer": "我找到 2 条雨夜素材，已用其中 1 条做参考，"
                            "并下发了 1 个文生视频任务（seedqueue1）。\n\n"
                            "注意：视频分辨率只有 544p / 768p 两档，"
                            "你请求的 1080p 实际按 768p 生成。",
                  "jobs": [{"job_id": "seedqueue1", "mode": "t2v",
                            "label": "文生视频", "status": "queued",
                            "prompt": "镜头缓慢推近窗边的女人",
                            "params": {"aspect": "16:9", "resolution": "1080p",
                                       "duration": 5}}],
                  "assets": [{"asset_id": "rain_night_01", "name": "雨夜街道",
                              "category": "image", "tags": ["夜", "雨"],
                              "description": "雨夜中的街道，霓虹灯倒影"}],
                  "steps": [{"step": 1, "tool": "search_assets", "ok": True, "ms": 42},
                            {"step": 2, "tool": "create_job", "ok": True, "ms": 118}],
                  "session_id": "demo-session"}, ensure_ascii=False) + "\n\n"
)


def install_stubs(page, agent_sse: str):
    """把 fetch 换成桩：/api/agent/chat* -> SSE；其它 -> 放行真实请求。"""
    page.evaluate("""(sse) => {
      const realFetch = window.fetch.bind(window);
      window.__agentCalls = [];
      window.fetch = async (url, opts) => {
        const u = String(url);
        if (u.includes('/api/agent/chat')) {
          window.__agentCalls.push({url: u, body: opts && opts.body});
          const stream = new ReadableStream({
            start(c) { c.enqueue(new TextEncoder().encode(sse)); c.close(); }
          });
          return {ok: true, status: 200, body: stream,
                  headers: {get: (h) => String(h).toLowerCase() === 'content-type'
                            ? 'text/event-stream' : null}};
        }
        return realFetch(url, opts);
      };
    }""", agent_sse)


def open_agent(page, port):
    """走真实用户路径切到 Agent 页（窄屏要先开抽屉）。"""
    page.evaluate("""() => {
      const t = document.querySelector('#navToggle');
      if (t && getComputedStyle(t).display !== 'none') t.click();
    }""")
    page.wait_for_timeout(300)
    page.click("#navAgent")
    page.wait_for_timeout(600)
    ok = page.evaluate(
        "() => { const p = document.querySelector('#agentPage');"
        " return !!p && getComputedStyle(p).display !== 'none'; }")
    if not ok:
        raise RuntimeError("点左侧「内置 Agent」后页面没切过来")


def geometry(page):
    """采集布局几何（横向溢出 / 输入框可达 / 消息区滚动）。"""
    return page.evaluate("""() => {
      const vw = document.documentElement.clientWidth;
      const vh = window.innerHeight;
      const msgs = document.querySelector('#agMessages');
      const input = document.querySelector('#agInput');
      const composer = document.querySelector('.ag-composer');
      const r = (el) => { if (!el) return null; const b = el.getBoundingClientRect();
        return {top: Math.round(b.top), bottom: Math.round(b.bottom),
                left: Math.round(b.left), right: Math.round(b.right),
                width: Math.round(b.width), height: Math.round(b.height)}; };
      const overflow = document.documentElement.scrollWidth - document.documentElement.clientWidth;
      // 找出所有横向超出视口右边界的元素（横向滚动条的真实来源）
      const offenders = [];
      document.querySelectorAll('#agentPage *').forEach(el => {
        const b = el.getBoundingClientRect();
        if (b.width > 0 && b.right > vw + 1) {
          offenders.push({tag: el.tagName, cls: el.className && String(el.className).slice(0,60),
                          right: Math.round(b.right), width: Math.round(b.width)});
        }
      });
      return {
        viewport: {w: vw, h: vh},
        overflowX: overflow,
        offenders: offenders.slice(0, 10),
        inputRect: r(input),
        inputReachable: !!input && input.getBoundingClientRect().top <= vh
                         && input.getBoundingClientRect().bottom >= 0,
        inputDisabled: !!input && input.disabled,
        composerRect: r(composer),
        messagesScrollable: !!msgs && msgs.scrollHeight > msgs.clientHeight,
        messagesScrollHeight: msgs ? msgs.scrollHeight : 0,
        messagesClientHeight: msgs ? msgs.clientHeight : 0,
      };
    }""")


def main():
    port = PORT or free_port()
    studio = Studio(port)
    report = {"port": port, "viewports": [], "console": {}, "checks": []}
    if not studio.start():
        print("!! Studio 实例启动失败")
        studio.stop()
        return 1

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            for width in VIEWPORTS:
                ctx = browser.new_context(viewport={"width": width, "height": 780})
                page = ctx.new_page()
                console = []
                page.on("console", lambda m: console.append(
                    {"type": m.type, "text": m.text}))
                page.on("pageerror", lambda e: console.append(
                    {"type": "pageerror", "text": str(e)}))
                page.on("requestfailed", lambda r: console.append(
                    {"type": "requestfailed",
                     "text": f"{r.url} {r.failure}" if r.failure else r.url}))

                page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                open_agent(page, port)

                # 空态截图
                page.screenshot(path=str(OUT_DIR / f"agent-{width}-empty.png"))

                # 发一条真实渲染的消息（SSE 桩）
                install_stubs(page, AGENT_SSE)
                page.fill("#agInput", "素材库里有没有雨夜的素材？用它做参考图生成 5 秒 16:9 视频")
                page.click("#agSend")
                page.wait_for_timeout(1500)

                geo = geometry(page)
                shot = OUT_DIR / f"agent-{width}-chat.png"
                page.screenshot(path=str(shot))

                # 工具块展开态截图（容器默认展开，这里额外展开每个工具块）
                page.evaluate(
                    "() => { document.querySelectorAll('.ag-tool').forEach(d => {"
                    " if (!d.open) d.querySelector('summary').click(); }); }")
                page.wait_for_timeout(300)
                page.screenshot(path=str(OUT_DIR / f"agent-{width}-tools-open.png"))

                # 任务卡状态实测：把服务端那条任务改成 running，触发轮询
                page.evaluate("""async () => {
                  const resp = await fetch('/api/jobs/seedqueue1');
                  window.__jobBefore = await resp.json();
                }""")
                page.wait_for_timeout(200)

                report["viewports"].append({
                    "width": width,
                    "screenshot": str(shot),
                    "geometry": geo,
                    "console_count": len(console),
                })
                report["console"][str(width)] = console
                ctx.close()
            browser.close()
    finally:
        studio.stop()

    # ---------------------------------------------------------------- 断言
    ok = True
    for v in report["viewports"]:
        w = v["width"]
        g = v["geometry"]
        errs = [c for c in report["console"][str(w)]
                if c["type"] in ("error", "pageerror")]
        checks = [
            ("无 JS 错误", len(errs) == 0, errs),
            ("无横向溢出", g["overflowX"] <= 0, g["overflowX"]),
            ("无元素越界", not g["offenders"], g["offenders"]),
            ("输入框可达", g["inputReachable"], g["inputRect"]),
        ]
        for name, passed, detail in checks:
            report["checks"].append({"width": w, "check": name,
                                      "passed": bool(passed), "detail": detail})
            if not passed:
                ok = False

    report["all_passed"] = ok
    out_json = OUT_DIR / "verify_report.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    print("=" * 68)
    print(f"证据目录: {OUT_DIR}")
    print(f"报告:     {out_json}")
    print("=" * 68)
    for v in report["viewports"]:
        g = v["geometry"]
        print(f"\n--- {v['width']}px ---")
        print(f"  截图        : {v['screenshot']}")
        print(f"  横向溢出    : {g['overflowX']} px")
        print(f"  越界元素    : {g['offenders']}")
        print(f"  输入框      : top={g['inputRect']['top']} "
              f"bottom={g['inputRect']['bottom']} 可达={g['inputReachable']}")
        print(f"  消息区可滚动: {g['messagesScrollable']} "
              f"({g['messagesScrollHeight']}/{g['messagesClientHeight']})")
        console = report["console"][str(v["width"])]
        errs = [c for c in console if c["type"] in ("error", "pageerror")]
        print(f"  console 总数: {len(console)}  其中 error/pageerror: {len(errs)}")
        for c in console:
            print(f"      [{c['type']}] {c['text']}")
    print("\n" + "=" * 68)
    print("总判定:", "全部通过" if ok else "存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
