# -*- coding: utf-8 -*-
"""接续生成的**前端契约**测试（真实浏览器 + 真实后端）。

【这个测试在防什么】
接续的前端有三条硬约束，坏了任何一条用户都会遇到：

  1. 生成库的视频卡片上出现「接续」按钮 —— 且**只**在支持接续的卡片上出现
     （二采 / 老任务 / 缺潜空间的卡片**不能**有按钮，否则用户点了才报错）。
  2. 点「接续」后，左侧配置区出现「接续上下文」只读区，显示回填了什么。
  3. 「接续上下文」区**没有**手动添加入口（用户需求：只能从生成库添加），
     并且有「清除」按钮。

【为什么要用真实浏览器】
这些约束全在 DOM 上（按钮存不存在、区域显不显示、有没有添加入口）——
纯静态检查读 JS 源码证明不了「渲染出来就是那样」。

运行：python3 -m pytest tests/test_continuation_frontend.py -v
"""

import json
import os
import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:
    HAS_PLAYWRIGHT = False

from test_frontend_defaults import StudioServer   # noqa: E402


@unittest.skipUnless(HAS_PLAYWRIGHT, "未安装 playwright")
class TestContinuationFrontend(unittest.TestCase):
    """接续前端契约。"""

    @classmethod
    def setUpClass(cls):
        cls.server = StudioServer(seed_jobs=0)
        assert cls.server.start(), "Studio 实例启动失败"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])
        cls.ctx = cls.browser.new_context(viewport={"width": 1280, "height": 900})
        cls.page = cls.ctx.new_page()
        cls.errors = []
        cls.failed_requests = []
        cls.page.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.page.on("console", lambda m: cls.errors.append(m.text)
                    if m.type == "error" else None)
        cls.page.on("response", lambda r: cls.failed_requests.append(
            f"{r.status} {r.url}") if r.status >= 400 else None)
        cls.page.goto(f"http://127.0.0.1:{cls.server.port}/",
                      wait_until="domcontentloaded")
        cls.page.wait_for_timeout(2500)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.stop()

    # ------------------------------------------------------------ 工具
    def _api(self, path, method="GET"):
        """直接问服务端（绕开浏览器），用来造一条可接续的记录。"""
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.server.port}{path}", method=method)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
        return json.loads(raw) if raw else {}

    def _seed_continuable_job(self):
        """造一条**服务端认为可接续**的任务记录。

        服务端判「可接续」要三件事：t2v 模式 + 非二采 + 潜空间文件在。
        潜空间文件在 ComfyUI 的 output 目录下 —— 测试里造一个假的
        （内容无所谓，服务端只检查文件**存在**）。
        """
        import sqlite3
        from h3studio import continuation_service as cs

        job_id = "contfrontend01"
        run_id = cs.chain_run_id(job_id)
        stage = cs.stage_path(run_id, 0)
        stage.parent.mkdir(parents=True, exist_ok=True)
        stage.write_bytes(b"fake-av-latent")

        params = {
            "prompt": "接续测试提示词",
            "model_key": "fl2va_int8",
            "width": 768, "height": 448,
            "duration": 5, "steps": 4,
            "sampler": "euler", "scheduler": "simple",
            "gen_type": "t2v",
            "continuation": {
                "chain_id": job_id, "stage_index": 0,
                "source_job": "", "source_segment": -1,
                "context_frames": 22, "anchor_frames": 22,
                "chain_total_frames": 0, "segment_frames": 124,
            },
        }
        with sqlite3.connect(self.server.data_dir / "jobs.db") as conn:
            conn.execute(
                "INSERT OR REPLACE INTO jobs "
                "(id, created_at, mode, params, status, progress, outputs) "
                "VALUES (?,?,?,?,?,?,?)",
                (job_id, time.time(), "t2v", json.dumps(params, ensure_ascii=False),
                 "done", 1.0,
                 json.dumps([{"kind": "video",
                              "file": "videos/fake-cont.mp4",
                              "name": "fake-cont.mp4"}], ensure_ascii=False)))
        return job_id

    def _goto_jobs(self):
        """切到生成库（作品/任务列表）页。"""
        self.page.evaluate("() => { if (window.location.hash !== '#jobs') "
                           "window.location.hash = '#jobs'; }")
        self.page.wait_for_timeout(500)
        # 直接调刷新
        self.page.evaluate("() => { if (window.refreshJobs) window.refreshJobs(); }")
        self.page.wait_for_timeout(1500)

    # ------------------------------------------------------------ 测试
    def test_no_console_errors_on_load(self):
        """加载时控制台 0 **JS** 报错。

        资源 404（图片/视频文件不存在）单独在下面断言 —— 那不是 JS 错误。
        """
        self.page.wait_for_timeout(1000)
        js_errors = [e for e in self.errors
                     if "Failed to load resource" not in e]
        self.assertEqual(js_errors, [], f"控制台有 JS 报错: {js_errors}")

    def test_continuation_module_loaded(self):
        """continuation.js 真的加载了（window.Continuation 存在）。"""
        self.assertTrue(self.page.evaluate(
            "() => !!(window.Continuation && window.Continuation.canShowButton)"),
            "window.Continuation 没加载")

    def test_capability_endpoint_reports_available(self):
        """服务端说节点齐 -> available=true。"""
        data = self._api("/api/continuation/state")
        self.assertTrue(data.get("available"),
                        f"接续能力探测失败: {data}")

    def test_context_panel_exists_and_starts_hidden(self):
        """左侧有「接续上下文」区域，且默认隐藏（没接续时不占地方）。

        【为什么要先 clear】测试之间共享同一个浏览器页面 ——
        前一个用例可能点过「接续」，面板就是显示态。所以这里先清空
        接续状态，再断言「没有接续上下文时该区域隐藏」。
        """
        exists = self.page.evaluate(
            "() => !!document.getElementById('continuationPanel')")
        self.assertTrue(exists, "index.html 里缺 #continuationPanel")
        self.page.evaluate("() => { if (window.Continuation) window.Continuation.clear(); }")
        self.page.wait_for_timeout(300)
        hidden = self.page.evaluate(
            "() => document.getElementById('continuationPanel').hidden")
        self.assertTrue(hidden, "没接续时该区域应隐藏")

    def test_context_panel_has_no_manual_add_entry(self):
        """【用户需求】只能从生成库添加 —— 面板里不能有手动添加入口。

        具体就是：面板里不能出现任何「添加接续」类的可点按钮。
        唯一允许的按钮是「清除」。
        """
        buttons = self.page.evaluate("""() => {
          const p = document.getElementById('continuationPanel');
          if (!p) return [];
          return Array.from(p.querySelectorAll('button')).map(b => b.textContent.trim());
        }""")
        for text in buttons:
            self.assertNotIn("添加", text,
                             f"接续上下文区不允许有手动添加入口: {text}")

    def test_continue_button_appears_on_continuable_card(self):
        """可接续的卡片上出现「接续」按钮。"""
        job_id = self._seed_continuable_job()
        self._goto_jobs()
        found = self.page.evaluate("""(jid) => {
          const card = document.querySelector(`.job-card[data-jid="${jid}"]`);
          if (!card) return {card: false};
          const btn = card.querySelector('[data-act="continue"]');
          return {card: true, button: !!btn, label: btn ? btn.textContent.trim() : ''};
        }""", job_id)
        self.assertTrue(found.get("card"), "生成库里没找到那条记录")
        self.assertTrue(found.get("button"),
                        "可接续的卡片上应该有「接续」按钮")
        self.assertIn("接续", found.get("label", ""))

    def test_non_continuable_card_has_no_button(self):
        """二采卡片**不能**有「接续」按钮（用户需求明确排除）。"""
        import sqlite3
        job_id = "contselflift1"
        params = {
            "prompt": "二采测试", "model_key": "fl2va_int8",
            "width": 768, "height": 448, "duration": 5, "steps": 4,
            "gen_type": "t2v",
            "twopass": {"enabled": True},
            "continuation": {
                "chain_id": job_id, "stage_index": 0,
                "source_job": "", "source_segment": -1,
                "context_frames": 22, "anchor_frames": 22,
                "chain_total_frames": 0, "segment_frames": 124,
            },
        }
        with sqlite3.connect(self.server.data_dir / "jobs.db") as conn:
            conn.execute(
                "INSERT OR REPLACE INTO jobs "
                "(id, created_at, mode, params, status, progress, outputs) "
                "VALUES (?,?,?,?,?,?,?)",
                (job_id, time.time(), "t2v", json.dumps(params, ensure_ascii=False),
                 "done", 1.0,
                 json.dumps([{"kind": "video", "file": "videos/fake2.mp4",
                              "name": "fake2.mp4"}], ensure_ascii=False)))
        self._goto_jobs()
        has_button = self.page.evaluate("""(jid) => {
          const card = document.querySelector(`.job-card[data-jid="${jid}"]`);
          if (!card) return None;
          return !!card.querySelector('[data-act="continue"]');
        }""", job_id)
        self.assertIsNotNone(has_button, "生成库里没找到二采那条记录")
        self.assertFalse(has_button, "二采卡片不能有「接续」按钮")

    def test_click_continue_backfills_context_panel(self):
        """点「接续」-> 左侧出现上下文区，内容来自服务端回填。"""
        job_id = self._seed_continuable_job()
        self._goto_jobs()
        self.page.click(f'.job-card[data-jid="{job_id}"] [data-act="continue"]')
        self.page.wait_for_timeout(1500)
        state = self.page.evaluate("""() => {
          const p = document.getElementById('continuationPanel');
          return {
            hidden: p ? p.hidden : null,
            text: p ? p.textContent : '',
            hasClear: !!(p && p.querySelector('#continuationClear')),
            current: window.Continuation && window.Continuation.state()
              ? !!window.Continuation.state().current : false,
          };
        }""")
        self.assertFalse(state["hidden"], "点接续后上下文区应显示")
        self.assertTrue(state["current"], "接续上下文应被记下")
        self.assertTrue(state["hasClear"], "上下文区应有「清除」按钮")
        self.assertIn("接续自", state["text"], "上下文区应显示来源")

    def test_continue_does_not_copy_prompt(self):
        """【用户需求】提示词除外 —— 点接续后提示词框应被清空（让用户写下一段）。"""
        job_id = self._seed_continuable_job()
        self.page.evaluate("""() => {
          const t = document.getElementById('prompt');
          if (t) t.value = '旧提示词不该被带过来';
        }""")
        self._goto_jobs()
        self.page.click(f'.job-card[data-jid="{job_id}"] [data-act="continue"]')
        self.page.wait_for_timeout(1200)
        prompt_value = self.page.evaluate(
            "() => (document.getElementById('prompt') || {}).value")
        self.assertEqual(prompt_value, "",
                         "接续不应把上一段的提示词带过来")

    def test_clear_button_removes_context(self):
        """「清除」-> 上下文区隐藏、状态清空。"""
        job_id = self._seed_continuable_job()
        self._goto_jobs()
        self.page.click(f'.job-card[data-jid="{job_id}"] [data-act="continue"]')
        self.page.wait_for_timeout(1200)
        self.page.click("#continuationClear")
        self.page.wait_for_timeout(600)
        state = self.page.evaluate("""() => {
          const p = document.getElementById('continuationPanel');
          return {
            hidden: p ? p.hidden : null,
            current: window.Continuation.state().current,
          };
        }""")
        self.assertTrue(state["hidden"], "清除后应隐藏")
        self.assertIsNone(state["current"], "清除后状态应为空")

    def test_continue_backfills_video_panel_fields(self):
        """接续要回填左侧配置区的模型 / 尺寸等。"""
        job_id = self._seed_continuable_job()
        self._goto_jobs()
        self.page.click(f'.job-card[data-jid="{job_id}"] [data-act="continue"]')
        self.page.wait_for_timeout(1500)
        panel = self.page.evaluate("""() => {
          const vp = window.VIDEO_PANEL;
          if (!vp || !vp.state) return null;
          return {
            model: vp.state.model,
            width: vp.state.width,
            height: vp.state.height,
            steps: vp.state.steps,
          };
        }""")
        self.assertIsNotNone(panel, "VIDEO_PANEL 没加载")
        self.assertEqual(panel["model"], "fl2va_int8",
                         "模型没回填")
        self.assertEqual(panel["width"], 768, "宽度没回填")
        self.assertEqual(panel["height"], 448, "高度没回填")
        self.assertEqual(panel["steps"], 4, "步数没回填")

    def test_no_console_errors_after_interactions(self):
        """整套交互跑完，控制台仍然 0 报错。"""
        self.page.wait_for_timeout(800)
        real_errors = [e for e in self.errors
                       if "favicon" not in e.lower()
                       and "Failed to load resource" not in e]
        self.assertEqual(real_errors, [], f"控制台有报错: {real_errors}")

    def test_no_unexpected_failed_requests(self):
        """除了测试自己造的假产物文件，不该有 4xx/5xx 请求。

        本测试会造一条指向**不存在的** mp4 的记录（卡片要有个视频产物），
        那一条 404 是预期的；其它任何 404 都是真问题。
        """
        self.page.wait_for_timeout(500)
        unexpected = [r for r in self.failed_requests
                      if "fake-cont.mp4" not in r and "fake2.mp4" not in r]
        self.assertEqual(unexpected, [],
                         f"有意外的失败请求: {unexpected}")


if __name__ == "__main__":
    unittest.main()
