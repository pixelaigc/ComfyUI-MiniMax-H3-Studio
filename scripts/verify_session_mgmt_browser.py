#!/usr/bin/env python3
"""
Real-browser verification of the Agent session-management feature (2026-11-11).

Proves, in a REAL browser against a REAL running server (throwaway data dir):

  1. LIST:    the session sidebar lists real persisted sessions
               (title / preview / time / message count).
  2. RESTORE: after a **page reload**, the current session and its message
               history come back (the core bug: history was lost on reload).
  3. NEW:     "新建交互" creates a session that really exists server-side.
  4. DELETE:  "删除交互" (with confirm) removes it server-side.
  5. SWITCH:  switching sessions restores the OTHER session's messages
               (and does NOT mix the two).
  6. ARTIFACTS: the "本次会话产物" panel lists the session's jobs
               (id / mode / status / thumbnail / params).
  7. NO OVERFLOW at 375 / 390 / 1280; console 0 errors; 0 failed requests.

【为什么必须用真浏览器】
The whole feature is DOM + fetch. A DOM-existence assertion proves nothing
about whether the session actually reloads, whether switching mixes the two
sessions, or whether the artifact panel renders real thumbnails.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_png import make_png  # noqa: E402

REPO = Path(os.environ.get("VERIFY_REPO",
                           "/root/projects/h3studio-session-mgmt"))
SHOTS = Path(os.environ.get("VERIFY_SHOTS",
                            REPO / "evidence" / "session-mgmt"))
PORT = int(os.environ.get("VERIFY_PORT", "8197"))
FAKE_PORT = int(os.environ.get("VERIFY_FAKE_PORT", "8917"))

sys.path.insert(0, str(REPO / "scripts"))


class FakeProviderHandler(BaseHTTPRequestHandler):
    """最小 OpenAI 兼容端点：立刻返回一句话。"""

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = {
            "id": "chatcmpl-fake", "object": "chat.completion",
            "created": int(time.time()), "model": "fake-model",
            "choices": [{"index": 0, "message": {"role": "assistant",
                                                  "content": "好的。"},
                         "finish_reason": "stop"}],
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
    from http.server import HTTPServer
    import threading

    server = HTTPServer(("127.0.0.1", FAKE_PORT), FakeProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def assert_port_free(port: int):
    """
    端口必须先空着。

    【为什么必须查 —— 实测踩过】
    上一次跑失败时进程没被杀掉，端口一直被占着。下一次跑起来时，
    start_server 起的**新进程**绑定失败（端口已占），但脚本里的
    健康检查照样连上了**那个旧进程** —— 于是测的是旧代码：
    报 405 Method Not Allowed（POST 路由还没加），
    让人以为是自己刚写的路由没注册。
    这类「测了旧进程」的假失败比真失败更费时间。
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(
                f"端口 {port} 已被占用 —— 可能有上次的残留进程。"
                f"先杀掉它再跑（否则会测到旧代码）。")


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
    proc = subprocess.Popen(
        ["/home/ubuntu/comfyui/venv/bin/python", "server.py"],
        cwd=str(REPO), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(120):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}/api/agent/sessions", timeout=2) as r:
                if r.status == 200:
                    return proc
        except Exception:
            time.sleep(0.5)
    out = proc.stdout.read() if proc.stdout else ""
    proc.kill()
    raise RuntimeError("server did not start:\n" + out[-3000:])


def seed_fake_provider(data_dir: Path):
    """种一个指向假 Provider 的模型 —— /api/agent/chat 需要可用的 provider。

    【为什么需要】Agent 的 provider 解析走「本地模型配置」，
    没有 provider 时 /api/agent/chat 直接 400（而不是静默成功）。
    会话功能本身不需要真的推理，所以用最快的假端点。
    """
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
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO model_providers VALUES "
        "('fakesess','假会话模型','openai',?,'',0,'','fake-model','',1,1,0)",
        (f"http://127.0.0.1:{FAKE_PORT}",))
    conn.execute("INSERT OR REPLACE INTO model_provider_prefs "
                 "VALUES ('default','fakesess')")
    conn.execute("INSERT OR REPLACE INTO model_provider_prefs "
                 "VALUES ('default_multimodal','fakesess')")
    conn.commit()
    conn.close()


def api(path, method="GET", body=None):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw else {}


def main():
    from playwright.sync_api import sync_playwright

    assert_port_free(PORT)
    assert_port_free(FAKE_PORT)
    tmp = Path(tempfile.mkdtemp(prefix="h3agent-session-"))
    fake_proc = start_fake_provider()
    proc = start_server(tmp)
    seed_fake_provider(tmp / "data")
    failures = []
    checks = 0

    def check(name, cond, detail=""):
        nonlocal checks
        checks += 1
        if cond:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name}  {detail}")
            failures.append(f"{name} {detail}")

    try:
        jobs_db = tmp / "data" / "jobs.db"

        def seed_sessions():
            """
            造两个真实会话 + 两个任务。**每个宽度都重新造一遍**。

            【为什么每档宽度都要重新造】
            上一档宽度会在末尾**删掉**「林黛玉」会话（测级联清理）。
            如果不重新造，下一档宽度刷新后：
              * localStorage 是新的（每个宽度用独立 browser context）
              * 服务端只剩「赛博朋克」会话 + 一个刚新建的空会话
              * 刷新后恢复到**最新的那个** —— 也就是刚新建的空会话
              * -> 消息数 0 -> 「刷新后恢复会话历史」假失败
            这是**测试之间互相污染**，不是产品 bug。
            每档独立造数据，三档互不影响。
            """
            sids = []
            # 【先把上一档宽度留下的会话全清掉】
            # 否则三档之间会互相污染：
            #   上一档末尾删了「林黛玉」会话、又「新建交互」建了一个空会话；
            #   下一档刷新时服务端有 4~5 个会话，列表里会出现**同名**会话，
            #   「找到第一个标题含林黛玉的项」就可能找到别的会话。
            # 每档从**已知状态**开始，三档互不影响。
            for old in api("/api/agent/sessions?limit=500")["sessions"]:
                api("/api/agent/sessions/" + old["id"], "DELETE")

            # 顺序很重要：后创建的那个 updated_at 更大 -> 刷新后默认回到它。
            # 所以「林黛玉」（后面要挂产物、还要切过去看）必须**最后**创建。
            for text in ("帮我做一段雨夜的赛博朋克视频",
                         "用素材库里的林黛玉做一段电影场景"):
                sid = api("/api/agent/sessions", "POST", {})["session_id"]
                api("/api/agent/chat", "POST",
                    {"message": text, "session_id": sid})
                sids.append(sid)
            # sids[1] = 林黛玉（最后创建 -> 刷新后默认回到它；也挂产物）
            # sids[0] = 赛博朋克（更早 -> 切过去时用来验「不串味」）
            conn = sqlite3.connect(jobs_db)
            conn.execute("DELETE FROM jobs")
            # 【2026-11-12】第一条任务挂一张**真实的图片产物**。
            #   需求 5 要求「集中查看 … 缩略图 … 点击产物可打开大图/预览」——
            #   没有真实文件的话，缩略图 <img> 会加载失败（onerror），
            #   lightbox 打开也是空白，验收脚本只能验到「DOM 存在」，
            #   验不到「真的能看」。
            #   PNG 用 seed_png 现算（64x36）—— 见那个文件的说明。
            seed_png = tmp / "data" / "outputs" / "aaaa1111bbbb"
            seed_png.mkdir(parents=True, exist_ok=True)
            (seed_png / "preview.png").write_bytes(make_png(64, 36))
            conn.execute(
                "INSERT INTO jobs (id, created_at, mode, params, status, session_id, outputs)"
                " VALUES (?,?,?,?,?,?,?)",
                ("aaaa1111bbbb", time.time(), "t2i",
                 json.dumps({"prompt": "雨夜霓虹", "aspect": "16:9",
                             "width": 1344, "height": 768, "resolution": "768p"},
                            ensure_ascii=False), "done", sids[1],
                 json.dumps([{"kind": "image", "file": "preview.png",
                              "name": "preview.png"}], ensure_ascii=False)))
            conn.execute(
                "INSERT INTO jobs (id, created_at, mode, params, status, session_id)"
                " VALUES (?,?,?,?,?,?)",
                ("cccc2222dddd", time.time(), "t2v",
                 json.dumps({"prompt": "林黛玉回眸", "duration": 5},
                            ensure_ascii=False), "queued", sids[1]))
            conn.commit()
            conn.close()
            print(f"[seed] sessions={sids}")
            return sids


        SHOTS.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox"])
            for width in (375, 390, 1280):
                # 每档宽度重新造数据（三档互不影响 —— 见 seed_sessions 的说明）
                sids = seed_sessions()
                # 每个宽度用**独立的 browser context** ——
                # 独立的缓存分区，保证测到的是当前代码（不是上一轮缓存的 JS）。
                ctx = browser.new_context(viewport={"width": width,
                                                    "height": 780})
                page = ctx.new_page()
                errors = []
                failed_req = []
                all_console = []
                page.on("console", lambda m: (all_console.append((m.type, m.text)),
                                              errors.append(m.text) if m.type == "error" else None))
                page.on("requestfailed", lambda r: failed_req.append(r.url))
                resp_log = []
                page.on("response", lambda r: resp_log.append(
                    (r.status, r.url)) if "/api/agent" in r.url else None)
                page.goto(f"http://127.0.0.1:{PORT}/", wait_until="domcontentloaded")
                page.wait_for_timeout(900)

                # 打开 Agent 页
                page.evaluate("""() => {
                    const nav = document.getElementById('navAgent');
                    if (nav) nav.click();
                }""")
                # 等 Agent 页真的显示出来（.page-hidden 被摘掉）。
                # 【为什么不能只 sleep】固定 sleep 在慢机器上不够 ->
                # 后面点 #agMenu 时元素还不可见，Playwright 会静默等到超时，
                # 看起来像「抽屉打不开」，实际是页面还没切过来。
                page.wait_for_function(
                    """() => {
                        const p = document.getElementById('agentPage');
                        return p && !p.classList.contains('page-hidden')
                               && p.offsetParent !== null;
                    }""",
                    timeout=15000)
                page.wait_for_timeout(300)

                # 1) 抽屉里有会话列表
                page.click("#agMenu")
                # 等会话列表真的渲染出来。
                # 【为什么不能用固定 sleep】会话清单是**异步**拉的
                # （loadSessions -> fetch -> renderSessionList）。
                # 固定 sleep 会在快慢机器上时好时坏 —— 实测就 flaky 过
                # （标题拿到空串，因为列表还没渲染就断言了）。
                page.wait_for_function(
                    """() => window.AgentSessions
                        && window.AgentSessions.state.sessions.length > 0""",
                    timeout=15000)
                print("  [console]", errors[:5])
                print("  [dbg]", page.evaluate("""() => {
                    const p = document.getElementById('agentPage');
                    const d = document.getElementById('agDrawer');
                    const it = document.querySelector('.ag-session-item');
                    const cs = it ? getComputedStyle(it) : null;
                    const r = it ? it.getBoundingClientRect() : null;
                    return {hasAgentSessions: !!window.AgentSessions,
                            sessCount: window.AgentSessions ? window.AgentSessions.state.sessions.length : -1,
                            drawerOpen: window.AgentSessions ? window.AgentSessions.state.drawerOpen : null,
                            pageClass: p ? p.className : null,
                            drawerVis: d ? getComputedStyle(d).visibility : null,
                            itemVis: cs ? cs.visibility : null,
                            rect: r ? {x:r.x,y:r.y,w:r.width,h:r.height} : null,
                            n: document.querySelectorAll('.ag-session-item').length};
                }"""))
                page.wait_for_selector(".ag-session-item", timeout=10000)
                items = page.locator(".ag-session-item")
                item_count = items.count()
                check(f"[{width}] 侧栏列出真实会话",
                      item_count >= 2,
                      f"实际 {item_count} 项")
                titles = [i.locator(".ag-session-item-title").inner_text()
                          for i in items.all()]
                check(f"[{width}] 会话标题有内容",
                      all(t and t.strip() for t in titles),
                      f"titles={titles}")
                metas = [i.locator(".ag-session-item-meta").inner_text()
                         for i in items.all()]
                check(f"[{width}] 会话条目带时间与条数",
                      all("条" in m for m in metas),
                      f"metas={metas}")
                page.screenshot(path=str(SHOTS / f"session-list-{width}.png"))

                # 2) 关闭抽屉 -> 刷新 -> 历史必须还在
                page.click("#agDrawerClose")
                page.wait_for_timeout(300)
                before = page.evaluate(
                    "() => window.AgentUI.state.messages.length")
                page.reload(wait_until="domcontentloaded")
                page.wait_for_timeout(1200)
                page.evaluate("""() => {
                    const nav = document.getElementById('navAgent');
                    if (nav) nav.click();
                }""")
                page.wait_for_timeout(800)
                # 恢复是**异步**的（loadSessions -> loadSession -> restoreMessages），
                # 等它真的恢复完再断言，不能只 sleep 一下就查。
                page.wait_for_function(
                    "() => window.AgentUI && window.AgentUI.state.messages.length > 0",
                    timeout=15000)
                after = page.evaluate(
                    "() => window.AgentUI.state.messages.length")
                check(f"[{width}] 刷新后恢复会话历史",
                      after > 0,
                      f"刷新前 {before} 条 -> 刷新后 {after} 条")

                # 恢复出来的消息要有正文（不是空壳）
                texts = page.evaluate(
                    "() => window.AgentUI.state.messages.map(m => m.content)")
                check(f"[{width}] 恢复出的消息有正文",
                      any(t and t.strip() for t in texts),
                      f"texts={texts}")
                page.screenshot(path=str(SHOTS / f"restored-{width}.png"))

                # 3) 切换会话 -> 不串味
                page.click("#agMenu")
                page.wait_for_timeout(400)
                target = None
                fresh = page.locator(".ag-session-item").all()
                for i in fresh:
                    if "林黛玉" in (i.locator(
                            ".ag-session-item-title").inner_text() or ""):
                        target = i
                        break
                if target:
                    target.click()
                    page.wait_for_timeout(900)
                    texts2 = page.evaluate(
                        "() => window.AgentUI.state.messages.map(m => m.content)")
                    check(f"[{width}] 切换会话后内容正确",
                          any("林黛玉" in (t or "") for t in texts2),
                          f"texts={texts2}")
                    check(f"[{width}] 切换会话不串味",
                          not any("赛博朋克" in (t or "") for t in texts2),
                          f"texts={texts2}")
                else:
                    check(f"[{width}] 找到目标会话", False, "没找到林黛玉会话")

                # 4) 产物面板（切到有任务的那个会话后再看）
                page.click("#agArtifactsBtn")
                page.wait_for_timeout(800)
                art_items = page.query_selector_all(".ag-artifact-item")
                check(f"[{width}] 产物面板列出任务", len(art_items) >= 1,
                      f"实际 {len(art_items)} 项")
                if art_items:
                    # 【为什么要遍历找而不是取第一个】
                    # 产物列表按 created_at **倒序**（最新的在前）——
                    # 所以第一个是那条 queued 的任务，不是 done 的。
                    # 取第一个会让断言依赖列表顺序（换种子顺序就假失败）。
                    # 遍历找是「集合里有它」的语义，与顺序无关。
                    found = False
                    prompt_ok = False
                    for el in art_items:
                        art_id = el.query_selector(".ag-artifact-id").inner_text()
                        if "aaaa1111bbbb" in art_id:
                            found = True
                            prompt = el.query_selector(
                                ".ag-artifact-prompt").inner_text()
                            prompt_ok = "雨夜" in prompt
                    check(f"[{width}] 产物带任务 id", found,
                          f"ids={[e.query_selector('.ag-artifact-id').inner_text() for e in art_items]}")
                    check(f"[{width}] 产物带提示词", prompt_ok, "")
                page.screenshot(path=str(SHOTS / f"artifacts-{width}.png"))

                # 4b) 产物卡片：未完成任务标记 + 定位入口
                pending_items = page.query_selector_all(".ag-artifact-item.is-pending")
                check(f"[{width}] 未完成任务有标记", len(pending_items) >= 1,
                      f"实际 {len(pending_items)} 个 pending")
                locate_btns = page.query_selector_all(
                    ".ag-artifact-locate[data-act='locate-job']")
                check(f"[{width}] 产物有「在生成库中定位」入口",
                      len(locate_btns) >= 1, f"实际 {len(locate_btns)} 个")
                if locate_btns:
                    job_id_attr = locate_btns[0].get_attribute("data-job-id")
                    check(f"[{width}] 定位按钮带真实 job id",
                          bool(job_id_attr), f"job-id={job_id_attr}")

                # 4c) 未完成任务状态实时刷新：把库里的 queued 改成 running，
                #     面板应该自己轮询到（不需要用户手动刷新）。
                with sqlite3.connect(jobs_db) as c2:
                    c2.execute("UPDATE jobs SET status='running' WHERE id=?",
                               ("cccc2222dddd",))
                    c2.commit()
                # 等一轮轮询（4 秒间隔）—— 给 12 秒余量
                try:
                    page.wait_for_function(
                        """() => [...document.querySelectorAll(
                                '.ag-artifact-item')]
                            .some(el => el.textContent.includes('running'))""",
                        timeout=12000)
                    refreshed = True
                except Exception:
                    refreshed = False
                check(f"[{width}] 未完成任务状态实时刷新", refreshed,
                      "queued -> running 没有被轮询到")
                page.screenshot(path=str(SHOTS / f"artifact-pending-{width}.png"))

                # 4c2) 【2026-11-12 新增】产物分辨率 + 大图预览
                #       需求 5：「集中查看 … 状态、缩略图、生成参数与时间；
                #       点击产物可跳转到生成库对应条目或打开大图/预览」
                reso_texts = page.evaluate("""() => [...document.querySelectorAll(
                    '.ag-artifact-meta')].map(e => e.textContent)""")
                check(f"[{width}] 产物显示分辨率/参数",
                      any("×" in t for t in reso_texts), f"meta={reso_texts}")

                preview_btns = page.query_selector_all(
                    ".ag-artifact-thumb-btn[data-act='preview-artifact']")
                check(f"[{width}] 图片产物缩略图可点看大图",
                      len(preview_btns) >= 1, f"实际 {len(preview_btns)} 个")
                if preview_btns:
                    preview_btns[0].click()
                    page.wait_for_timeout(700)
                    lb_open = page.evaluate(
                        "() => { const el = document.querySelector('#lightbox');"
                        " return !!el && !el.hidden; }")
                    check(f"[{width}] 点缩略图打开大图查看器", lb_open,
                          "lightbox 没打开")
                    page.screenshot(path=str(SHOTS / f"artifact-lightbox-{width}.png"))
                    # 关掉大图（Esc）
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(400)
                    lb_closed = page.evaluate(
                        "() => { const el = document.querySelector('#lightbox');"
                        " return !!el && el.hidden; }")
                    check(f"[{width}] 大图查看器可关闭", lb_closed,
                          "Esc 没关掉 lightbox")

                # 4c3) 【2026-11-12 新增】「在生成库中定位」真的能定位
                #       原实现只写 location.hash（全仓 0 消费者）-> 假功能。
                #       现在应该：切到生成页 + 关键词筛到该任务 + 卡片高亮。
                #
                # 【为什么要先确认面板状态 —— 2026-11-12 踩过】
                # #agArtifactsBtn 是**开关**（toggle）。面板本来就是开着的，
                # 再点一次就是**关掉**它 —— DOM 被清空，后面的断言全部找不到元素。
                # 这里改成「只在关着的时候才点开」。
                panel_open = page.evaluate(
                    "() => { const el = document.querySelector('#agArtifacts');"
                    " return !!el && !el.hidden; }")
                if not panel_open:
                    page.click("#agArtifactsBtn")
                    page.wait_for_timeout(600)
                locate_btn = page.query_selector(
                    ".ag-artifact-locate[data-act='locate-job']")
                if locate_btn:
                    target_job = locate_btn.get_attribute("data-job-id")
                    locate_btn.click()
                    # 等定位链路走完（跳页 + 拉数据 + 渲染卡片 + 高亮）。
                    #
                    # 【为什么要轮询等而不是 sleep 固定时长 —— 2026-11-12 踩过】
                    # 定位是异步链路：切页 -> fetch /api/jobs -> 渲染卡片 -> 加高亮 class。
                    # 固定 sleep 会在慢机器上「查得太早」（高亮还没加上），
                    # 或者在快机器上「查得太晚」（高亮已经撤掉）—— 两种都是假失败。
                    # 轮询等高亮出现，是唯一稳的判据。
                    highlighted = False
                    try:
                        page.wait_for_function(
                            """(jid) => !!document.querySelector(
                                '.job-card.is-located[data-jid="' + jid + '"]')""",
                            arg=target_job or "", timeout=12000)
                        highlighted = True
                    except Exception:
                        highlighted = False
                    # 生成页应该可见
                    jobs_visible = page.evaluate(
                        "() => { const el = document.querySelector('#jobsArea');"
                        " return !!el && !el.classList.contains('page-hidden'); }")
                    check(f"[{width}] 定位切到生成库", jobs_visible,
                          "生成库页面没显示")
                    # 搜索框被填成了目标 id
                    kw = page.evaluate(
                        "() => (document.querySelector('#doneSearch')||{}).value || ''")
                    check(f"[{width}] 定位填入任务 id 筛选", target_job in kw,
                          f"keyword={kw} target={target_job}")
                    # 目标卡片被高亮
                    check(f"[{width}] 定位高亮目标任务卡片", highlighted,
                          f"job={target_job}")
                    page.screenshot(path=str(SHOTS / f"artifact-located-{width}.png"))
                    # 定位那步切到了生成库 —— 要回 Agent 页才能继续后面的步骤
                    # （产物面板在 Agent 页里，生成库页面上点不到它）。
                    #
                    # 【为什么用 JS 点击而不是 page.click —— 2026-11-12 踩过】
                    # 375/390px 下左侧导航是**折叠抽屉**（#sidebar 收在屏幕外），
                    # #navAgent 虽然「可见」但**在视口外**，
                    # page.click 会一直 retry 到 30 秒超时。
                    # 直接调元素的 .click() 走的是同一套事件处理，不受视口限制。
                    page.evaluate(
                        "() => { const el = document.querySelector('#navAgent');"
                        " if (el) el.click(); }")
                    page.wait_for_timeout(700)
                else:
                    check(f"[{width}] 产物有定位按钮（可点）", False,
                          "没找到定位按钮")

                # 面板用完了才关（4b/4c 需要它是打开的 ——
                # 关掉会停掉轮询，DOM 也会被清空）
                panel_open = page.evaluate(
                    "() => { const el = document.querySelector('#agArtifacts');"
                    " return !!el && !el.hidden; }")
                if panel_open:
                    page.click("#agArtifactsClose")
                    page.wait_for_timeout(200)

                # 4d) 会话列表带关联产物数
                page.click("#agMenu")
                page.wait_for_selector(".ag-session-item", timeout=10000)
                metas = page.evaluate("""() => [...document.querySelectorAll(
                    '.ag-session-item-meta')].map(e => e.textContent)""")
                check(f"[{width}] 会话条目显示产物数",
                      any("产物" in m for m in metas), f"metas={metas}")

                # 4d2) 【2026-11-12 新增】创建时间 + 状态徽章
                #      需求 2：「展示每条会话的标题、创建/最后更新时间、
                #      消息数、产物数、状态」
                created_texts = page.evaluate("""() => [...document.querySelectorAll(
                    '.ag-session-item-created')].map(e => e.textContent)""")
                check(f"[{width}] 会话条目显示创建时间",
                      len(created_texts) >= 1
                      and all("建于" in t for t in created_texts),
                      f"created={created_texts}")
                status_badges = page.evaluate("""() => [...document.querySelectorAll(
                    '.ag-session-status')].map(e => e.textContent.trim())""")
                check(f"[{width}] 会话条目显示状态徽章",
                      len(status_badges) >= 1, f"status={status_badges}")
                # 状态徽章的文案必须是后端给的中文标签之一
                # （不能是空串、不能是 undefined）
                check(f"[{width}] 状态徽章文案有效",
                      bool(status_badges)
                      and all(s in ("空会话", "完成", "已截断", "失败", "待补充")
                              for s in status_badges),
                      f"status={status_badges}")
                page.screenshot(path=str(SHOTS / f"session-status-{width}.png"))

                # 4e) 搜索/过滤
                page.fill("#agSessionSearch", "林黛玉")
                page.wait_for_timeout(900)
                hits = page.query_selector_all(".ag-session-item")
                check(f"[{width}] 搜索能过滤出会话", len(hits) == 1,
                      f"实际 {len(hits)} 项")
                if hits:
                    hit_text = hits[0].inner_text()
                    check(f"[{width}] 搜索命中正确会话",
                          "林黛玉" in hit_text, f"text={hit_text}")
                page.screenshot(path=str(SHOTS / f"search-{width}.png"))

                # 搜一个不存在的词 -> 空列表
                page.fill("#agSessionSearch", "zzz不存在的关键词zzz")
                page.wait_for_timeout(900)
                empty_hits = page.query_selector_all(".ag-session-item")
                check(f"[{width}] 搜不到时列表为空", len(empty_hits) == 0,
                      f"实际 {len(empty_hits)} 项")
                # 清空搜索 -> 恢复
                page.fill("#agSessionSearch", "")
                page.wait_for_timeout(900)
                restored = page.query_selector_all(".ag-session-item")
                check(f"[{width}] 清空搜索后恢复全部", len(restored) >= 2,
                      f"实际 {len(restored)} 项")
                page.click("#agDrawerClose")
                page.wait_for_timeout(300)

                # 5) 新建会话 -> 服务端真的多一个
                # （新建按钮在抽屉里 —— 抽屉此刻是关着的，要先打开）
                before_list = len(api("/api/agent/sessions")["sessions"])
                page.click("#agMenu")
                page.wait_for_selector(".ag-session-item", timeout=10000)
                page.click("#agDrawerNew")
                page.wait_for_timeout(900)
                after_list = len(api("/api/agent/sessions")["sessions"])
                check(f"[{width}] 新建交互落库",
                      after_list == before_list + 1,
                      f"{before_list} -> {after_list}")

                # 6) 删除会话（带二次确认）-> 服务端真的少一个
                # 抽屉在「新建」那步已经关了，要再打开一次。
                page.click("#agMenu")
                page.wait_for_selector(".ag-session-item", timeout=10000)
                page.once("dialog", lambda d: d.accept())
                # 【为什么要精确找到「林黛玉」会话的删除按钮】
                # 列表按 updated_at 倒序 —— 刚「新建交互」建出来的空会话
                # 排在最前面，query_selector(".ag-session-del") 取到的
                # 是**它**的删除按钮。删掉它跟级联清理毫无关系
                # （那个会话本来就没挂任务）—— 那样测的就不是级联清理了。
                del_btn = None
                for item in page.locator(".ag-session-item").all():
                    title = item.locator(".ag-session-item-title").inner_text()
                    if "林黛玉" in (title or ""):
                        del_btn = item.locator(".ag-session-del").first
                        break
                if del_btn:
                    del_btn.click()
                    page.wait_for_timeout(900)
                    final_list = len(api("/api/agent/sessions")["sessions"])
                    check(f"[{width}] 删除交互落库",
                          final_list == after_list - 1,
                          f"{after_list} -> {final_list}")
                else:
                    check(f"[{width}] 找到删除按钮（林黛玉会话）", False,
                          "列表里没找到林黛玉会话")

                # 级联清理：删掉的会话如果原来挂着任务，任务应该**仍在**
                # 生成库里（只是不再归属会话）。
                with sqlite3.connect(jobs_db) as c3:
                    remaining = c3.execute(
                        "SELECT COUNT(*) FROM jobs WHERE id IN "
                        "('aaaa1111bbbb','cccc2222dddd')").fetchone()[0]
                    still_attached = c3.execute(
                        "SELECT COUNT(*) FROM jobs WHERE session_id!='' "
                        "AND id IN ('aaaa1111bbbb','cccc2222dddd')"
                    ).fetchone()[0]
                check(f"[{width}] 级联清理不删任务记录", remaining == 2,
                      f"剩余 {remaining} 条")
                check(f"[{width}] 级联清理只清引用关系",
                      still_attached == 0,
                      f"仍有 {still_attached} 条挂在会话上")

                # 7) 不溢出 + console 干净
                dims = page.evaluate("""() => ({
                    docScrollW: document.documentElement.scrollWidth,
                    docClientW: document.documentElement.clientWidth,
                    bodyScrollW: document.body.scrollWidth,
                })""")
                check(f"[{width}] 无横向溢出",
                      dims["docScrollW"] <= dims["docClientW"] + 1
                      and dims["bodyScrollW"] <= dims["docClientW"] + 1,
                      f"{dims}")
                check(f"[{width}] console 0 错误", not errors, f"{errors[:3]}")
                check(f"[{width}] 0 失败请求", not failed_req,
                      f"{failed_req[:3]}")
                page.close()
                ctx.close()
                ctx.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            fake_proc.shutdown()
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    print(f"\n=== {checks - len(failures)}/{checks} passed ===")
    if failures:
        for f in failures:
            print("  FAILED:", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
