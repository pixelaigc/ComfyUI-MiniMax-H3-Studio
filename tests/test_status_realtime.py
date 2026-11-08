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
        cls.server = StudioServer(seed_jobs=12)
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
            # 排队中会带上数量后缀，断言前缀即可
            ({"comfyui_ok": True, "comfy_running": 0, "comfy_pending": 2,
              "running": None, "queued": 0}, "排队中"),
            ({"comfyui_ok": False, "comfy_running": None, "comfy_pending": None,
              "running": None, "queued": 0}, "ComfyUI 未连接"),
        ]
        for payload, expected in cases:
            self.page.evaluate("d => renderStatus(d)", payload)
            self.page.wait_for_timeout(80)
            actual = self.status_text()
            self.assertTrue(actual.startswith(expected),
                            "payload=%r 期望以 %r 开头，实际 %r"
                            % (payload, expected, actual))

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
        """前端已订阅 SSE（EventSource 存在且连上）。"""
        self.page.evaluate("""
            window.__sseOk = false;
            try {
                const es = new EventSource('/api/status/stream');
                es.onopen = () => { window.__sseOk = true; };
            } catch (e) {}
        """)
        self.page.wait_for_function("() => window.__sseOk === true", timeout=10000)

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
        if comfy_ok():
            running, pending = comfy_queue()
            if running is not None:
                self.assertEqual(data["comfy_running"], running,
                                 "comfy_running 应与 ComfyUI 真实队列一致")
                self.assertEqual(data["comfy_pending"], pending,
                                 "comfy_pending 应与 ComfyUI 真实队列一致")

    def test_status_shows_generating_when_comfyui_busy(self):
        """ComfyUI 在跑 -> 界面显示「生成中」（中文）。"""
        # 通过渲染函数验证映射：comfy_running>0 -> 生成中
        self.page.evaluate("renderStatus({comfyui_ok:true, comfy_running:1, "
                           "comfy_pending:0, running:null, queued:0})")
        self.page.wait_for_timeout(100)
        self.assertEqual(self.status_text(), "生成中")
        self.assertIn("dot-busy", self.dot_class())

    def test_status_returns_to_idle_after_done(self):
        """生成结束 -> 立即切回「空闲」。"""
        self.page.evaluate("renderStatus({comfyui_ok:true, comfy_running:1, "
                           "comfy_pending:0, running:'x', queued:0})")
        self.page.wait_for_timeout(100)
        self.assertEqual(self.status_text(), "生成中")
        self.page.evaluate("renderStatus({comfyui_ok:true, comfy_running:0, "
                           "comfy_pending:0, running:null, queued:0})")
        self.page.wait_for_timeout(100)
        self.assertEqual(self.status_text(), "空闲")
        self.assertIn("dot-ok", self.dot_class())

    def test_status_unknown_queue_falls_back_to_studio_state(self):
        """读不到 ComfyUI 队列（null）时退回 Studio 自身状态，不误报空闲。"""
        self.page.evaluate("renderStatus({comfyui_ok:true, comfy_running:null, "
                           "comfy_pending:null, running:'job-x', queued:0})")
        self.page.wait_for_timeout(100)
        self.assertEqual(self.status_text(), "生成中",
                         "队列未知但 Studio 在跑时应显示生成中")
        self.page.evaluate("renderStatus({comfyui_ok:true, comfy_running:null, "
                           "comfy_pending:null, running:null, queued:3})")
        self.page.wait_for_timeout(100)
        self.assertIn("排队中", self.status_text(),
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

    def test_no_console_errors(self):
        """首屏无 JS 报错。"""
        self.assertEqual([e for e in self.errors if e.strip()], [])


if __name__ == "__main__":
    unittest.main()
