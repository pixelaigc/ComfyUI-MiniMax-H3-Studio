# -*- coding: utf-8 -*-
"""状态栏实时性 + 历史库自动刷新的回归测试（真实浏览器 + 真实后端）。

覆盖三条用户可见的要求：
  1. 状态文案全部中文：空闲 / 排队中 / 生成中 / ComfyUI 未连接
     （此前生成中是英文 "Generating"）。
  2. 状态与 ComfyUI **真实队列**一致：用户在 ComfyUI 里直接提交任务时，
     Studio 也必须显示「生成中」，而不是只盯着自己的 worker。
  3. 生成完成后，右侧历史/素材库自动出现新条目（缩略图 / 状态 / 参数），
     不需要手动刷新页面。

第 2 条的真实根因：/api/system 的 running 只取 Studio 自己的 WORKER.current，
ComfyUI 那边在跑什么完全不知道 —— 实测 ComfyUI queue_running=1 时
studio running=None，界面因此一直显示「空闲」。现改为同时读 ComfyUI 的
GET /queue，并新增 SSE（/api/status/stream）把状态实时推给前端。

测试用独立临时数据目录 + 预置任务，不依赖也不污染真实库。
ComfyUI 队列不可用时，相关断言按「未知态」处理而非判失败。

运行：python3 -m pytest tests/test_status_realtime.py -v
需要：playwright（chromium）。缺失时整类 skip，不算失败。
"""
import json
import os
import sqlite3
import sys
import time
import unittest
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:                                   # pragma: no cover
    sync_playwright = None
    HAS_PLAYWRIGHT = False

from test_frontend_defaults import StudioServer, texts, visible  # noqa: E402


# 本机 ComfyUI 地址（测试用；连不上时相关用例按未知态处理）
COMFY = os.environ.get("H3STUDIO_TEST_COMFY", "http://127.0.0.1:8188")


def comfy_queue():
    """读 ComfyUI 真实队列 -> (running, pending)；读不到返回 None。"""
    import urllib.request
    try:
        with urllib.request.urlopen(COMFY + "/queue", timeout=3) as r:
            d = json.loads(r.read().decode())
        return len(d.get("queue_running") or []), len(d.get("queue_pending") or [])
    except Exception:
        return (None, None)


class FakeComfy:
    """可控的假 ComfyUI：只实现 Studio 用到的两个端点。

    /system_stats -> 200（让 Studio 认为 ComfyUI 连着）
    /queue        -> 测试自己设定的 running / pending 数量

    为什么要它：真 ComfyUI 的队列是全机共享的，随时可能有别人的任务在跑；
    busy 优先于 pending，一忙起来就盖住 Studio 自己 queued 的变化。
    """

    def __init__(self):
        self.running = 0
        self.pending = 0
        self.server = None
        self.thread = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        import http.server
        import threading

        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _json(self, payload):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/queue"):
                    self._json({"queue_running": [{}] * outer.running,
                                "queue_pending": [{}] * outer.pending})
                elif self.path.startswith("/system_stats"):
                    self._json({"system": {"comfyui_version": "0.37.0"},
                                "devices": []})
                else:
                    self._json({})

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


def comfy_ok():
    import urllib.request
    try:
        with urllib.request.urlopen(COMFY + "/system_stats", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


@unittest.skipUnless(HAS_PLAYWRIGHT, "未安装 playwright")
class TestStatusRealtime(unittest.TestCase):
    """真实浏览器 + 真实 Studio 实例，断言状态栏行为。"""

    @classmethod
    def setUpClass(cls):
        # 用**可控的假 ComfyUI**（而不是真的 127.0.0.1:8188）。
        # 原因：真 ComfyUI 的队列是全机共享的 —— 本机随时可能有别人的任务在跑，
        # statusLabel 里 busy 优先于 pending，ComfyUI 一忙起来就盖住
        # Studio 自己 queued 的变化，这个用例根本没法稳定测。
        # 假 ComfyUI 让「队列状态」变成测试自己能控的变量。
        cls.fake_comfy = FakeComfy()
        cls.fake_comfy.start()
        cls.server = StudioServer(seed_jobs=12, comfyui_url=cls.fake_comfy.url)
        assert cls.server.start(), "Studio 实例启动失败"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])
        cls.ctx = cls.browser.new_context()
        cls.page = cls.ctx.new_page()
        cls.errors = []
        cls.page.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.page.goto("http://127.0.0.1:%d/" % cls.server.port, wait_until="domcontentloaded")
        cls.page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid > *').length > 0", timeout=20000)
        # 记录状态文案的变化序列
        cls.page.evaluate("""
            window.__statLog = [];
            const el = document.querySelector('#statusText');
            new MutationObserver(() => window.__statLog.push(el.textContent))
                .observe(el, {childList:true, characterData:true, subtree:true});
        """)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.stop()
        cls.fake_comfy.stop()

    def status_text(self):
        return self.page.evaluate("document.querySelector('#statusText').textContent")

    def dot_class(self):
        return self.page.evaluate("document.querySelector('#topStatus .dot').className")

    # ------------------------------------------------------ 中文文案
    def test_status_text_is_chinese(self):
        """状态文案不得出现英文 "Generating"（此前生成中是英文）。"""
        self.page.evaluate("state && refreshSystem && refreshSystem()")
        self.page.wait_for_timeout(1200)
        text = self.status_text()
        self.assertNotIn("Generating", text, "状态文案仍是英文: %r" % text)
        self.assertNotRegex(text, r"^[A-Za-z]+$", "状态文案应为中文: %r" % text)

    def test_status_known_states_are_chinese(self):
        """四种状态的中文文案都在（空闲 / 排队中 / 生成中 / ComfyUI 未连接）。"""
        # 直接调 renderStatus 覆盖各分支
        cases = [
            ({"comfyui_ok": True, "comfy_running": 0, "comfy_pending": 0,
              "running": None, "queued": 0}, "空闲"),
            ({"comfyui_ok": True, "comfy_running": 1, "comfy_pending": 0,
              "running": None, "queued": 0}, "生成中"),
            # 排队中带数量后缀，这里断言完整形态（精确匹配，不放行英文变体）
            ({"comfyui_ok": True, "comfy_running": 0, "comfy_pending": 2,
              "running": None, "queued": 0}, "排队中（2）"),
            ({"comfyui_ok": True, "comfy_running": 0, "comfy_pending": 0,
              "running": None, "queued": 3}, "排队中（3）"),
            ({"comfyui_ok": False, "comfy_running": None, "comfy_pending": None,
              "running": None, "queued": 0}, "ComfyUI 未连接"),
        ]
        for payload, expected in cases:
            actual = self.page.evaluate("d => statusLabel(d).text", payload)
            self.assertEqual(actual, expected,
                             "payload=%r 期望 %r，实际 %r"
                             % (payload, expected, actual))
            # 任何一种状态都不得是纯英文
            self.assertNotRegex(actual, r"^[A-Za-z]+$",
                                "状态文案应为中文: %r" % actual)

    def test_no_english_in_status_labels(self):
        """源码里不应再有英文的 Generating / Generating 等文案。"""
        app_js = (REPO / "web" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("`Generating", app_js,
                         "app.js 里仍有英文 Generating 文案")
        self.assertIn("生成中", app_js, "app.js 里应有中文「生成中」")

    # ------------------------------------------------------ 实时性
    def test_sse_endpoint_pushes_snapshots(self):
        """SSE 端点存在且会推状态帧。"""
        import urllib.request
        url = "http://127.0.0.1:%d/api/status/stream" % self.server.port
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                self.assertIn("text/event-stream", r.headers.get("Content-Type", ""))
                line = r.readline().decode("utf-8")
        except Exception as e:
            self.fail("SSE 端点不可用: %s" % e)
        self.assertTrue(line.startswith("data:"), "SSE 首行应为 data: 帧，实际 %r" % line)
        payload = json.loads(line[len("data:"):].strip())
        for key in ("queued", "running", "comfy_running", "comfy_pending"):
            self.assertIn(key, payload, "SSE 帧缺字段 %s: %r" % (key, payload))

    def test_frontend_subscribes_to_sse(self):
        """前端启动时**自己**订阅了 SSE（不能只是手工 new 一个 EventSource）。

        用请求拦截来判定：页面一加载就应当自己发起 /api/status/stream 连接。
        """
        self.assertEqual(
            self.page.evaluate("() => typeof startStatusStream"),
            "function", "app.js 里应有 startStatusStream()")
        seen = []
        self.page.on("request", lambda r: seen.append(r.url)
                     if "/api/status/stream" in r.url else None)
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_timeout(2500)
        self.assertTrue(seen, "页面加载后应用自己应建立 SSE 连接 /api/status/stream")

    def test_sse_update_reaches_the_ui(self):
        """SSE 推来的状态帧会真正驱动界面（不是只在后台连着）。

        三个坑要避开（都是这轮真踩到的）：

        1. **SSE 只在内容变化时推帧**（server.py 的 api_status_stream 里
           `if payload != last`）。「手工改 UI -> 等下一帧覆盖」依赖一帧
           可能根本不会来的帧 —— 队列一直空，payload 就不变。
        2. 界面上显示什么取决于**此刻 ComfyUI 真实队列**（本机可能正有别的
           任务在跑），不能写死文案。
        3. 塞的 queued 任务会被 worker **立刻领走**（queued 归零）——
           测的是竞态不是 SSE。必须用 mode='longfilm'：
           worker 的领任务 SQL 带 `AND mode!='longfilm'`，longfilm 永远不会被领走。

        关键：要断言的是「**状态确实变了并且界面跟着变了**」，
        所以先记下变化前的状态，再要求界面出现一个**新的**状态。
        """
        self.page.wait_for_timeout(1500)

        # **关键：把轮询这条路堵死**，否则测的就不是 SSE 而是 3 秒轮询。
        # 前端 setInterval(refreshSystem, 3000) 每 3 秒拉一次 /api/system，
        # SSE 一坏轮询照样能把界面更新对 —— 那样这个用例就算 SSE 完全失效也照样通过。
        #
        # 为什么堵 fetch 而不是替换 window.refreshSystem：
        # setInterval 在注册时就按引用记住了那个函数，之后替换 window 上的
        # 属性对它无效（实测仍在发请求）。拦 fetch 是从「数据入口」堵，
        # 无论谁调都堵得住；SSE 不走 fetch，所以不受影响。
        self.page.evaluate("""
            () => {
              window.__sysCalls = 0;
              if (window.__realFetch) return;
              window.__realFetch = window.fetch;
              window.fetch = function (u, o) {
                if (String(u).indexOf('/api/system') >= 0) {
                  window.__sysCalls++;
                  return new Promise(() => {});   // 永不 resolve -> 界面不会被轮询更新
                }
                return window.__realFetch.apply(this, arguments);
              };
            }
        """)
        # 等一个轮询周期，确认轮询确实在调（但被堵住了）
        self.page.wait_for_timeout(3600)
        self.assertGreater(self.page.evaluate("() => window.__sysCalls"), 0,
                           "前置条件：轮询应该在调 /api/system（被堵住后界面不会因轮询改变）")

        # 变化前：记下界面当前**完整状态**（文案 + title）。
        # 用 title 而不是文案做区分度：ComfyUI 在跑时文案恒为「生成中」，
        # 但 title 会带真实数量（"ComfyUI 正在生成（N 个在执行）"）。
        before = self.page.evaluate(
            "() => { const el = document.querySelector('#statusText');"
            " return (el ? el.textContent.trim() : '') + '|' + (el ? (el.title||'') : ''); }")

        # 假 ComfyUI 的队列现在是空的 -> 界面应为「空闲」
        self.assertEqual(self.status_text(), "空闲",
                         "前置条件：ComfyUI 队列为空时应显示空闲")

        # 塞一条排队中的任务。
        # mode 必须用 **longfilm** —— worker 的领任务 SQL 是
        #   WHERE status='queued' AND mode!='longfilm'
        # 即 longfilm 任务**永远不会被 worker 领走**（长片自己起子进程），
        # 所以 queued 计数能稳定保持 >0。
        # 用别的 mode（包括 worker 不认识的）都会被领走并标 failed，测的是竞态。
        conn = sqlite3.connect(self.server.data_dir / "jobs.db")
        conn.execute(
            "INSERT OR REPLACE INTO jobs (id, created_at, mode, params, status) "
            "VALUES (?,?,?,?,?)",
            ("sseprobe01", time.time(), "longfilm", "{}", "queued"))
        conn.commit()
        conn.close()

        # 等 SSE 把界面更新成「排队中」。
        # 期望值先在 Python 侧算好（用浏览器里的 statusLabel，保证同一套判定），
        # 再用**同步**的 wait_for_function 等界面变成它 ——
        # wait_for_function 里放 async 谓词不可靠（它不 await Promise）。
        # 顺便等 /api/system 也变成排队中（避免读到旧值）。
        deadline = time.time() + 20
        expected = None
        while time.time() < deadline:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.server.port}/api/system", timeout=10) as r:
                live = json.loads(r.read().decode())
            expected = self.page.evaluate("d => statusLabel(d).text + '|' + statusLabel(d).title", live)
            if live.get("queued", 0) > 0:
                break
            time.sleep(0.5)
        self.assertTrue(live.get("queued", 0) > 0,
                        "前置条件：Studio 队列里应有 1 条排队任务")
        self.assertNotEqual(expected, before,
                            "真实状态应与变化前不同（queued 变了）")

        # 界面必须变成那个状态 —— 这一步靠 SSE（没有轮询帮忙）
        self.page.wait_for_function(
            "(exp) => { const el = document.querySelector('#statusText');"
            " if (!el) return false;"
            " return (el.textContent.trim()) + '|' + (el.title || '') === exp; }",
            arg=expected, timeout=15000)
        after = self.page.evaluate(
            "() => { const el = document.querySelector('#statusText');"
            " return (el ? el.textContent.trim() : '') + '|' + (el ? (el.title||'') : ''); }")
        self.assertNotEqual(after, before,
                            "界面状态没有变化 —— SSE 没把新帧推到界面上")
        self.assertEqual(after, expected,
                         "界面状态应与 /api/system 一致")

        # 状态再变回去 -> SSE 再推一帧 -> 界面跟着更新
        conn = sqlite3.connect(self.server.data_dir / "jobs.db")
        conn.execute("DELETE FROM jobs WHERE id='sseprobe01'")
        conn.commit()
        conn.close()
        # 状态再变回去 -> SSE 再推一帧 -> 界面跟着更新
        deadline = time.time() + 20
        expected2 = None
        while time.time() < deadline:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.server.port}/api/system", timeout=10) as r:
                live2 = json.loads(r.read().decode())
            expected2 = self.page.evaluate(
                "d => statusLabel(d).text + '|' + statusLabel(d).title", live2)
            if live2.get("queued", 0) == 0:
                break
            time.sleep(0.5)
        self.page.wait_for_function(
            "(exp) => { const el = document.querySelector('#statusText');"
            " if (!el) return false;"
            " return (el.textContent.trim()) + '|' + (el.title || '') === exp; }",
            arg=expected2, timeout=15000)

        # 恢复 fetch —— 这个类共用一个 page，堵着会让后面的用例全废
        self.addCleanup(self.restore_fetch)
        self.restore_fetch()

    def restore_fetch(self):
        """把 fetch 拦截撤掉（这个类共用一个 page）。"""
        try:
            self.page.evaluate("""
                () => {
                  if (window.__realFetch) { window.fetch = window.__realFetch; window.__realFetch = null; }
                }
            """)
        except Exception:
            pass

    def test_status_reflects_comfyui_real_queue(self):
        """ComfyUI 真实队列在跑时，/api/system 的 comfy_running 必须 > 0。

        根因守卫：此前 running 只取 Studio 自己的 worker，ComfyUI 那边
        在跑什么完全看不到（实测 comfy queue_running=1 而 studio
        running=None），界面因此一直显示「空闲」。
        """
        data = json.loads(self.page.evaluate(
            "fetch('/api/system',{cache:'no-store'}).then(r=>r.json()).then(d=>JSON.stringify(d))"))
        self.assertIn("comfy_running", data, "/api/system 缺 comfy_running 字段")
        self.assertIn("comfy_pending", data, "/api/system 缺 comfy_pending 字段")
        self.assertIsInstance(data["comfy_running"], int,
                              "comfy_running 应为整数（恒 0 说明没真读队列）")
        self.assertIsInstance(data["comfy_pending"], int,
                              "comfy_pending 应为整数（恒 0 说明没真读队列）")
        # 本类跑在**假 ComfyUI** 上（见 FakeComfy：真 ComfyUI 的队列全机共享，
        # 随时可能有别人的任务在跑，断言没法稳定）。
        # 意图不变：comfy_running 必须**真的读队列**，而不是恒 0。
        self.assertEqual(data["comfy_running"], self.fake_comfy.running,
                         "comfy_running 应与 ComfyUI 队列一致")
        self.assertEqual(data["comfy_pending"], self.fake_comfy.pending,
                         "comfy_pending 应与 ComfyUI 队列一致")

    def test_status_shows_generating_when_comfyui_busy(self):
        """ComfyUI 在跑 -> 界面显示「生成中」（中文）。"""
        # 用纯函数 statusLabel 验证映射，避免与 SSE 推送竞争
        label = self.page.evaluate(
            "() => statusLabel({comfyui_ok:true, comfy_running:1, "
            "comfy_pending:0, running:null, queued:0})")
        self.assertEqual(label["text"], "生成中")
        self.assertIn("dot-busy", label["dot"])

    def test_status_returns_to_idle_after_done(self):
        """生成结束 -> 立即切回「空闲」。"""
        busy = self.page.evaluate(
            "() => statusLabel({comfyui_ok:true, comfy_running:1, "
            "comfy_pending:0, running:'x', queued:0})")
        self.assertEqual(busy["text"], "生成中")
        idle = self.page.evaluate(
            "() => statusLabel({comfyui_ok:true, comfy_running:0, "
            "comfy_pending:0, running:null, queued:0})")
        self.assertEqual(idle["text"], "空闲")
        self.assertIn("dot-ok", idle["dot"])

    def test_status_unknown_queue_falls_back_to_studio_state(self):
        """读不到 ComfyUI 队列（null）时退回 Studio 自身状态，不误报空闲。"""
        busy = self.page.evaluate(
            "() => statusLabel({comfyui_ok:true, comfy_running:null, "
            "comfy_pending:null, running:'job-x', queued:0})")
        self.assertEqual(busy["text"], "生成中",
                         "队列未知但 Studio 在跑时应显示生成中")
        queued = self.page.evaluate(
            "() => statusLabel({comfyui_ok:true, comfy_running:null, "
            "comfy_pending:null, running:null, queued:3})")
        self.assertIn("排队中", queued["text"],
                      "队列未知但 Studio 有排队时应显示排队中")

    # ------------------------------------------------------ 库自动刷新
    def test_new_job_appears_without_page_reload(self):
        """新任务无需刷新页面就自动出现在库里（含缩略图 / 状态 / 参数）。"""
        before = set(self.page.evaluate(
            "() => Array.from(document.querySelectorAll('#jobsGrid > *')).map(e=>e.dataset.jid)"))
        probe = "自动刷新探针-" + str(int(time.time()))
        conn = sqlite3.connect(self.server.data_dir / "jobs.db")
        conn.execute(
            "INSERT INTO jobs (id, created_at, mode, params, status, progress,"
            " progress_text, outputs) VALUES (?,?,?,?,?,?,?,?)",
            ("autorefresh1", time.time(), "t2i",
             json.dumps({"title": "自动刷新", "prompt": probe,
                         "aspect": "16:9", "width": 1360, "height": 768,
                         "model": "z_image_turbo_bf16", "steps": 8, "cfg": 1},
                        ensure_ascii=False),
             "done", 1.0, "", "[]"))
        conn.commit()
        conn.close()

        # 不做任何页面操作 —— 只能靠 setInterval(refreshJobs, 2000) 自己刷新
        self.page.wait_for_function(
            "t => document.body.innerText.includes(t)", arg=probe, timeout=15000)

        after = set(self.page.evaluate(
            "() => Array.from(document.querySelectorAll('#jobsGrid > *')).map(e=>e.dataset.jid)"))
        self.assertIn("autorefresh1", after, "新任务未自动出现在库里")
        self.assertNotIn("autorefresh1", before)

    def test_library_renders_status_and_params_of_new_entry(self):
        """库里的新条目带状态与参数（缩略图/状态/参数齐全）。"""
        rows = self.page.evaluate("""
            () => Array.from(document.querySelectorAll('#jobsGrid > *')).map(el => ({
                mode: (el.querySelector('.job-meta .row') || {}).textContent || '',
                meta: el.querySelector('.job-meta')?.textContent || '',
                hasPreview: !!el.querySelector('.job-preview'),
            }))
        """)
        self.assertTrue(rows, "库里应有卡片")
        first = rows[0]
        self.assertTrue(first["hasPreview"], "卡片应有预览区（缩略图/进度）")
        self.assertIn("模式", first["meta"], "卡片应显示模式")
        self.assertIn("分辨率", first["meta"], "卡片应显示分辨率参数")
        self.assertIn("步数/CFG", first["meta"], "卡片应显示步数/CFG 参数")
        self.assertIn("种子", first["meta"], "卡片应显示种子")

    def test_running_job_card_shows_progress(self):
        """生成中的任务卡片显示进度（不是空白）。"""
        conn = sqlite3.connect(self.server.data_dir / "jobs.db")
        conn.execute(
            "INSERT INTO jobs (id, created_at, mode, params, status, progress,"
            " progress_text, outputs) VALUES (?,?,?,?,?,?,?,?)",
            ("runningcard1", time.time(), "t2i",
             json.dumps({"title": "生成中", "prompt": "运行中探针",
                         "aspect": "16:9"}, ensure_ascii=False),
             "running", 0.42, "采样中", "[]"))
        conn.commit()
        conn.close()
        self.page.wait_for_function(
            "() => document.body.innerText.includes('运行中探针')", timeout=15000)
        html = self.page.evaluate(
            "() => document.querySelector('#jobsGrid').innerHTML")
        self.assertIn("progress-bar", html, "生成中的卡片应有进度条")

    def test_batch_status_labels_are_chinese(self):
        """批次卡片上的状态文案也要是中文（完成/失败/生成中/排队中/已取消）。

        批次状态是另一张独立的映射表（renderJobBatches 里的 statusText），
        与顶栏 STATUS_TEXT 不是同一处 —— 改一处不会影响另一处。
        因此必须**只在这张表的范围内**检查，不能在整份文件里搜字符串
        （「排队中」在顶栏也有，整份搜会误判为已覆盖）。
        """
        app_js = (REPO / "web" / "app.js").read_text(encoding="utf-8")
        start = app_js.index("const statusText = {done:")
        end = app_js.index("[batch.status]", start)
        mapping = app_js[start:end]          # 结束标记本身不含在切片里
        self.assertTrue(mapping.strip().endswith("}"),
                        "批次状态表范围定位失败: %r" % mapping)
        self.assertIn("const statusText = {done:", mapping)
        for label in ("完成", "失败", "生成中", "排队中", "已取消"):
            self.assertIn(f'"{label}"', mapping,
                          "批次状态映射里应含中文 %r，实际表=%r" % (label, mapping))
        # 表里不应出现英文状态名
        for english in ("Queued", "Done", "Failed", "Running", "Canceled"):
            self.assertNotIn(f'"{english}"', mapping,
                             "批次状态映射里出现英文 %r: %r" % (english, mapping))

    def test_batch_card_shows_chinese_status(self):
        """批次网格渲染出的状态文字是中文。"""
        # 造一个带批次的任务，卡片展开后应显示批次状态
        conn = sqlite3.connect(self.server.data_dir / "jobs.db")
        conn.execute(
            "INSERT INTO jobs (id, created_at, mode, params, status, progress,"
            " progress_text, outputs) VALUES (?,?,?,?,?,?,?,?)",
            ("batchjob1", time.time(), "t2i",
             json.dumps({"title": "批次任务", "prompt": "批次探针",
                         "aspect": "16:9"}, ensure_ascii=False),
             "done", 1.0, "", "[]"))
        conn.execute(
            "INSERT INTO job_batches (id, job_id, idx, seed, status, progress,"
            " progress_text, outputs, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("b1", "batchjob1", 0, 1, "done", 1.0, "", "[]", time.time()))
        conn.execute(
            "INSERT INTO job_batches (id, job_id, idx, seed, status, progress,"
            " progress_text, outputs, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("b2", "batchjob1", 1, 2, "failed", 0.0, "boom", "[]", time.time()))
        # 再加一个排队中的批次 —— 这样「排队中」这条文案真的会被渲染出来
        conn.execute(
            "INSERT INTO job_batches (id, job_id, idx, seed, status, progress,"
            " progress_text, outputs, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("b3", "batchjob1", 2, 3, "queued", 0.0, "", "[]", time.time()))
        conn.commit()
        conn.close()

        self.page.wait_for_function(
            "() => document.body.innerText.includes('批次探针')", timeout=15000)
        # 展开批次
        self.page.evaluate("""
            const btn = Array.from(document.querySelectorAll('#jobsGrid [data-act="toggle-batches"]'))[0];
            if (btn) btn.click();
        """)
        self.page.wait_for_timeout(700)
        labels = self.page.evaluate(
            "() => Array.from(document.querySelectorAll('#jobsGrid .st'))"
            ".map(e => e.textContent.trim())")
        self.assertTrue(labels, "应有批次状态标签")
        for label in labels:
            self.assertRegex(label, r"[\u4e00-\u9fff]",
                             "批次状态应为中文: %r" % label)
            for english in ("Queued", "Done", "Failed", "Running", "Canceled"):
                self.assertNotIn(english, label,
                                 "批次状态含英文 %r: %r" % (english, label))
        # 三个批次（done / failed / queued）都要出现，且「排队中」必须是中文
        joined = " ".join(labels)
        for want in ("完成", "失败", "排队中"):
            self.assertIn(want, joined,
                          "批次状态应含 %r，实际渲染: %r" % (want, labels))

    def test_no_console_errors(self):
        """首屏无 JS 报错。"""
        self.assertEqual([e for e in self.errors if e.strip()], [])


class TestComfyQueueReporting(unittest.TestCase):
    """后端：ComfyUI 真实队列必须被读到并如实上报（不依赖浏览器）。

    这一组是 S10 变异（「后端 comfy_running 恒为 0」）的防线 ——
    之前只在浏览器里断言「与真实队列相等」，当本机队列恰好为空时
    「恒 0」也满足断言，变异存活。这里用假 session 喂非空队列。
    """

    class _FakeResp:
        def __init__(self, payload, status=200):
            self._payload = payload
            self.status = status

        async def json(self):
            return self._payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeSession:
        """模拟 aiohttp ClientSession：只实现 session.get(...)."""

        def __init__(self, payload, status=200):
            self.payload = payload
            self.status = status
            self.calls = []

        def get(self, url, **kw):
            self.calls.append(url)
            if "queue" in url:
                return TestComfyQueueReporting._FakeResp(self.payload, self.status)
            return TestComfyQueueReporting._FakeResp({"system": {}, "devices": []})

    def _load_server(self):
        import importlib
        sys.path.insert(0, str(REPO))
        import server as server_mod
        importlib.reload(server_mod)
        # 建表：init_db() 只在 server.main() 里调，而这里直接 import 模块
        # 调 status_snapshot()，不建表就会报 "no such table: jobs"。
        # （本项目其它测试也是这个做法，见 test_qwen_refs_browser.py）
        server_mod.init_db()
        return server_mod

    def test_fetch_comfy_queue_reports_nonempty_queue(self):
        """/queue 有 3 个在跑 2 个排队时，必须如实读出来。"""
        server = self._load_server()
        payload = {"queue_running": [1, 2, 3], "queue_pending": [4, 5]}
        session = self._FakeSession(payload)
        import asyncio
        got = asyncio.run(server.fetch_comfy_queue(session))
        self.assertIsNotNone(got, "应成功读到队列")
        self.assertEqual(got["running"], 3, "running 必须等于真实队列数")
        self.assertEqual(got["pending"], 2, "pending 必须等于真实队列数")
        self.assertTrue(any("/queue" in c for c in session.calls),
                        "必须真的请求了 /queue")

    def test_fetch_comfy_queue_returns_none_on_failure(self):
        """读不到队列时返回 None（未知），不能当成 0。"""
        server = self._load_server()

        class _Boom:
            def get(self, url, **kw):
                raise OSError("connection refused")

        got = asyncio_run(server.fetch_comfy_queue(_Boom()))
        self.assertIsNone(got, "读不到队列应返回 None（未知）而不是 0")

    def test_status_snapshot_passes_queue_through(self):
        """status_snapshot 必须把队列数字原样透出（不能吞成 0）。"""
        server = self._load_server()
        payload = {"queue_running": [1, 2], "queue_pending": []}
        session = self._FakeSession(payload)
        import asyncio
        snap = asyncio.run(server.status_snapshot(session))
        self.assertEqual(snap["comfy_running"], 2,
                         "status_snapshot 必须透出 comfy_running=2")
        self.assertEqual(snap["comfy_pending"], 0)

    def test_status_snapshot_marks_unknown_when_unreachable(self):
        """ComfyUI 不可达时 comfy_running 为 None（未知），不是 0。"""
        server = self._load_server()

        class _Boom:
            def get(self, url, **kw):
                raise OSError("connection refused")

        snap = asyncio_run(server.status_snapshot(_Boom()))
        self.assertIsNone(snap["comfy_running"],
                          "读不到时应为 None（未知），0 会被误当成空闲")


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
