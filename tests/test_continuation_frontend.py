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

        【为什么要参数化 job_id】这个测试类共用**同一个浏览器页面**。
        某个测试真的提交了任务（会新增卡片）之后，列表会变 —— 后面的
        测试再按固定 id 找卡片就可能找不到（生成库是分页的）。
        所以每个测试用**自己的** job_id，互不干扰。
        """
        return self._seed_continuable_job_as("contfrontend01")

    def _seed_continuable_job_as(self, job_id):
        """造一条可接续记录（可指定 id —— 见 _seed_continuable_job 的说明）。"""
        return self._seed_continuable_job_as_mode(job_id, "t2v")

    def _seed_continuable_job_as_mode(self, job_id, mode="t2v",
                                       extra_params=None, asset_refs=None):
        """造一条可接续记录（可指定模式 / 附加参数 / 素材引用）。"""
        import sqlite3
        from h3studio import continuation_service as cs

        run_id = cs.segment_run_id(job_id)
        stage = cs.stage_path(run_id, 0)
        stage.parent.mkdir(parents=True, exist_ok=True)
        stage.write_bytes(b"fake-av-latent")

        params = {
            "prompt": "接续测试提示词",
            "model_key": "fl2va_int8",
            "width": 768, "height": 448,
            "duration": 5, "steps": 4,
            "sampler": "euler", "scheduler": "simple",
            "gen_type": mode,
            "continuation": {
                "chain_id": job_id, "stage_index": 0,
                "source_job": "", "source_segment": -1,
                "latent_run_id": run_id, "latent_stage": 0,
                "context_frames": 22, "anchor_frames": 22,
                "chain_total_frames": 0, "segment_frames": 124,
            },
        }
        if extra_params:
            params.update(extra_params)
        if asset_refs is not None:
            params["asset_refs"] = asset_refs
        with sqlite3.connect(self.server.data_dir / "jobs.db") as conn:
            conn.execute(
                "INSERT OR REPLACE INTO jobs "
                "(id, created_at, mode, params, status, progress, outputs) "
                "VALUES (?,?,?,?,?,?,?)",
                (job_id, time.time(), mode, json.dumps(params, ensure_ascii=False),
                 "done", 1.0,
                 json.dumps([{"kind": "video",
                              "file": "videos/fake-cont.mp4",
                              "name": "fake-cont.mp4"}], ensure_ascii=False)))
        return job_id

    def _real_asset_ids(self):
        """素材库里真实存在的（图片 id, 音频 id）。取不到返回 (None, None)。

        【为什么要真实 id】前端回填时要按 id 查素材名与类别 ——
        编造的 id 查不到，槽位就是空的（那不是回填失败，是库里没这条）。
        """
        try:
            from h3studio import assets as A
            with A.connect() as conn:
                image = conn.execute(
                    "SELECT id FROM assets WHERE category='image' "
                    "AND deleted_at IS NULL LIMIT 1").fetchone()
                audio = conn.execute(
                    "SELECT id FROM assets WHERE category='audio' "
                    "AND deleted_at IS NULL LIMIT 1").fetchone()
        except Exception:
            return None, None
        return ((image["id"] if image else None),
                (audio["id"] if audio else None))

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
          if (!card) return null;
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

    def test_continue_submit_payload_carries_continuation(self):
        """【2026-11-12 第三轮】正常的接续提交**必须**仍然带 continuation。

        【为什么要有这条】第三轮修「接续 + 复用参数」的混合态时，
        加了「点复用参数清 context」与「提交前尺寸闸」两道闸。
        这两道闸有可能**误伤正常路径** —— 例如尺寸闸算错导致
        正常接续也被拦下、或者清 context 时把正常路径的 continuation
        一起清了。

        所以这里锁住：点「接续」-> 提交 -> 载荷里**有** continuation
        且尺寸与来源段一致（闸放行）。
        """
        job_id = self._seed_continuable_job_as("contfrontend-submit")
        self._goto_jobs()
        self.page.click(f'.job-card[data-jid="{job_id}"] [data-act="continue"]')
        self.page.wait_for_timeout(1500)

        captured = self.page.evaluate("""async () => {
          const orig = window.fetch;
          let captured = null;
          window.fetch = function (url, opts) {
            if (opts && opts.method === 'POST'
                && String(url).includes('/api/jobs')
                && opts.body instanceof FormData) {
              const raw = opts.body.get('params');
              if (raw) captured = JSON.parse(raw);
              // 【关键】**不真的发出去** —— 只看载荷长什么样。
              // 真的提交会往库里加一条任务，把生成库的分页顶掉
              // （每页 12 条），后面那些按固定 id 找卡片的测试就会
              // 找不到自己的卡片 —— 变成**顺序依赖**的假失败。
              return new Promise(() => {});
            }
            return orig.apply(this, arguments);
          };
          document.getElementById('prompt').value = 'next segment probe';
          const btn = document.getElementById('genBtn');
          if (!btn) { window.fetch = orig; return {noButton: true}; }
          btn.click();
          await new Promise(r => setTimeout(r, 1500));
          window.fetch = orig;
          return {captured};
        }""")
        params = (captured or {}).get("captured") or {}
        self.assertTrue(params, "没抓到提交载荷（断言会空转）")
        self.assertIn("continuation", params,
                        "正常接续的提交载荷里必须有 continuation")
        self.assertEqual(params.get("width"), 768,
                         "接续提交的本段尺寸应与来源段一致")
        self.assertEqual(params.get("height"), 448)

    # ------------------------------------------------------------
    # 【2026-11-19 新增】flf / r2v 的接续提交体**必须**带 continuation
    # ------------------------------------------------------------
    def _capture_submit_payload(self, job_id, mode_label):
        """点「接续」-> 提交 -> 抓提交体（**不真的发出去**）。"""
        self._goto_jobs()
        self.page.click(f'.job-card[data-jid="{job_id}"] [data-act="continue"]')
        self.page.wait_for_timeout(1800)
        captured = self.page.evaluate("""async () => {
          const orig = window.fetch;
          let captured = null;
          window.fetch = function (url, opts) {
            if (opts && opts.method === 'POST'
                && String(url).includes('/api/jobs')
                && opts.body instanceof FormData) {
              const raw = opts.body.get('params');
              if (raw) captured = JSON.parse(raw);
              // 【关键】**不真的发出去** —— 真的提交会往库里加一条任务，
              // 把生成库的分页顶掉（每页 12 条），后面那些按固定 id
              // 找卡片的测试就会找不到自己的卡片 —— 变成**顺序依赖**
              // 的假失败。
              return new Promise(() => {});
            }
            return orig.apply(this, arguments);
          };
          const t = document.getElementById('prompt');
          if (t) t.value = 'next segment probe ' + 'MODE';
          const btn = document.getElementById('genBtn');
          if (!btn) { window.fetch = orig; return {noButton: true}; }
          btn.click();
          await new Promise(r => setTimeout(r, 1800));
          window.fetch = orig;
          return {captured};
        }""".replace("MODE", mode_label))
        # 【2026-11-19】测试类**共用同一个浏览器页面**。点「接续」会把
        # 面板切到来源任务的模式（flf / r2v）并挂上接续上下文 ——
        # 不清掉的话，后面那些按 t2v 假设的用例会被污染（实测：
        # test_continue_switches_to_source_mode 单独跑绿、跟着跑红）。
        # ⇒ 抓完载荷立刻复位：清接续上下文 + 切回 t2v。
        self.page.evaluate("""() => {
          if (window.Continuation) window.Continuation.clear();
          if (typeof window.activateVideoMode === 'function') {
            window.activateVideoMode('t2v');
          }
        }""")
        self.page.wait_for_timeout(600)
        return (captured or {}).get("captured") or {}

    def test_flf_continuation_submit_carries_continuation(self):
        """【2026-11-19 修的核心缺陷】首尾帧的接续提交体**必须**带 continuation。

        【原来错在哪】web/app.js 只在 **t2v 分支**里写
        `params.continuation = ctx.continuation;` —— flf / r2v 分支从不设它。
        ⇒ 点「接续」-> 面板正确回填 -> 点生成 -> 提交体里**没有**
        continuation -> 服务端看不到接续 -> 本段退化成**普通生成**。

        【为什么这条断言不是恒真】
        现有那条 `test_continue_submit_payload_carries_continuation`
        用的是 **t2v** 模式（`_seed_continuable_job_as` 默认 t2v）——
        它**抓不到**本缺陷（t2v 一直是好的）。这里补 flf / r2v 两条。
        """
        # 【为什么要素材引用】flf / r2v 的提交入口有**素材必填**守卫
        # （app.js：首尾帧至少给首帧或尾帧 / 参考模式至少 1 个参考素材），
        # 没有素材就 alert 中止 -> 提交根本不会发生 -> 抓不到载荷。
        # 接续时这些槽位由「接续回填」填上（continuation.js 的
        # applyAssetRefs），所以这里也要给素材引用。
        image_id, _audio_id = self._real_asset_ids()
        if not image_id:
            self.skipTest("素材库里没有可用的图片素材")
        job_id = self._seed_continuable_job_as_mode(
            "contfrontend-flf-submit", "flf",
            asset_refs=[
                {"asset_id": image_id, "slot": "first_frame", "ordinal": 0},
            ])
        params = self._capture_submit_payload(job_id, "flf")
        self.assertTrue(params, "没抓到提交载荷（断言会空转）")
        self.assertIn(
            "continuation", params,
            "首尾帧的接续提交载荷里必须有 continuation"
            "（缺了它，本段会静默退化成普通生成）")
        self.assertEqual(
            params.get("continuation", {}).get("source_job"), job_id,
            "continuation 必须指向来源任务")

    def test_r2v_continuation_submit_carries_continuation(self):
        """参考生视频的接续提交体**必须**带 continuation（同上）。"""
        image_id, _audio_id = self._real_asset_ids()
        if not image_id:
            self.skipTest("素材库里没有可用的图片素材")
        job_id = self._seed_continuable_job_as_mode(
            "contfrontend-r2v-submit", "r2v",
            asset_refs=[
                {"asset_id": image_id, "slot": "ref_image", "ordinal": 0},
            ])
        params = self._capture_submit_payload(job_id, "r2v")
        self.assertTrue(params, "没抓到提交载荷（断言会空转）")
        self.assertIn(
            "continuation", params,
            "参考生视频的接续提交载荷里必须有 continuation")
        self.assertEqual(
            params.get("continuation", {}).get("source_job"), job_id,
            "continuation 必须指向来源任务")

    def test_mismatched_size_is_blocked_before_submit(self):
        """【2026-11-12 第三轮】尺寸不一致时**提交前**就拦下。

        这条守的是「提交前尺寸闸」：用户在点完接续之后自己改了宽高，
        点生成时必须被拦下，且**不发出** /api/jobs 请求。
        """
        job_id = self._seed_continuable_job_as("contfrontend-mismatch")
        self._goto_jobs()
        self.page.click(f'.job-card[data-jid="{job_id}"] [data-act="continue"]')
        self.page.wait_for_timeout(1500)

        result = self.page.evaluate("""async () => {
          const orig = window.fetch;
          let posted = 0;
          window.fetch = function (url, opts) {
            if (opts && opts.method === 'POST'
                && String(url).includes('/api/jobs')) posted += 1;
            return orig.apply(this, arguments);
          };
          // 把面板尺寸改成与来源段不同
          const vp = window.VIDEO_PANEL;
          vp.state.width = 1024;
          vp.state.height = 576;
          document.getElementById('prompt').value = 'mismatched size probe';
          const btn = document.getElementById('genBtn');
          if (!btn) { window.fetch = orig; return {noButton: true}; }
          btn.click();
          await new Promise(r => setTimeout(r, 2000));
          window.fetch = orig;
          return {posted, toast: window.__h3ContinuationToast || ''};
        }""")
        self.assertEqual(result.get("posted"), 0,
                         "尺寸不一致时不该发出 /api/jobs 请求")
        self.assertIn("分辨率", result.get("toast", ""),
                      f"拦下时必须给出中文原因，实际: {result.get('toast')!r}")

    # ------------------------------------------------------------
    # 【2026-11-16 新增】首尾帧 / 参考生视频的接续
    # ------------------------------------------------------------
    def test_flf_card_has_continue_button(self):
        """首尾帧产物的卡片上**要有**「接续」按钮。

        【为什么这条重要】旧实现里 flf/r2v 被服务端直接拒绝
        （「只有视频类产物支持接续。」—— 那句文案本身就是错的）。
        """
        job_id = self._seed_continuable_job_as_mode("contfrontend-flf", "flf")
        self._goto_jobs()
        found = self.page.evaluate("""(jid) => {
          const card = document.querySelector(`.job-card[data-jid="${jid}"]`);
          if (!card) return {card: false};
          const btn = card.querySelector('[data-act="continue"]');
          return {card: true, button: !!btn};
        }""", job_id)
        self.assertTrue(found.get("card"), "生成库里没找到首尾帧那条记录")
        self.assertTrue(found.get("button"),
                        "首尾帧卡片上应该有「接续」按钮")

    def test_r2v_card_has_continue_button(self):
        """参考生视频产物的卡片上**要有**「接续」按钮。"""
        job_id = self._seed_continuable_job_as_mode("contfrontend-r2v", "r2v")
        self._goto_jobs()
        found = self.page.evaluate("""(jid) => {
          const card = document.querySelector(`.job-card[data-jid="${jid}"]`);
          if (!card) return {card: false};
          const btn = card.querySelector('[data-act="continue"]');
          return {card: true, button: !!btn};
        }""", job_id)
        self.assertTrue(found.get("card"), "生成库里没找到参考生那条记录")
        self.assertTrue(found.get("button"),
                        "参考生卡片上应该有「接续」按钮")

    def test_continue_switches_to_source_mode(self):
        """点首尾帧产物的「接续」-> 面板切到**首尾帧**（不是文生视频）。

        【为什么】硬切 t2v 会让首尾帧 / 参考生的素材槽位根本不存在
        —— 用户看不到参考图，也没法追加本段的参考媒体。
        """
        job_id = self._seed_continuable_job_as_mode("contfrontend-mode", "flf")
        self._goto_jobs()
        self.page.click(f'.job-card[data-jid="{job_id}"] [data-act="continue"]')
        self.page.wait_for_timeout(1800)
        mode = self.page.evaluate(
            "() => (window.__h3State && window.__h3State.mode) || ''")
        self.assertEqual(mode, "flf",
                         "接续后应切到来源任务自己的模式")

    def test_continue_backfills_asset_slots(self):
        """接续要**自动回填原始参考素材**（参考图 / 参考音乐）。

        【用户需求】次段自动填入原始参考图 + 修正后的参考音频。
        """
        # 用**素材库里真实存在的**素材 id（前端要按 id 查名字/类别）。
        image_id, audio_id = self._real_asset_ids()
        if not image_id or not audio_id:
            self.skipTest("素材库里没有可用的图片 / 音频素材")
        job_id = self._seed_continuable_job_as_mode(
            "contfrontend-assets", "r2v",
            asset_refs=[
                {"asset_id": image_id, "slot": "ref_image", "ordinal": 0},
                {"asset_id": audio_id, "slot": "ref_audio", "ordinal": 0},
            ])
        self._goto_jobs()
        self.page.click(f'.job-card[data-jid="{job_id}"] [data-act="continue"]')
        self.page.wait_for_timeout(2500)
        slots = self.page.evaluate("""() => {
          const s = (window.__h3State && window.__h3State.assetRefs) || {};
          return Object.keys(s);
        }""")
        self.assertIn("ref0", slots,
                      f"参考图槽位没回填（现有：{slots}）")
        self.assertIn("refaud0", slots,
                      f"参考音乐槽位没回填（现有：{slots}）")

    def test_continue_restores_audio_track_source(self):
        """接续要**回填音轨来源**（以链首段为准）。

        【为什么必须回填】切模式会把音轨下拉复位成「使用生成的音乐」——
        那会让接续段的音轨与上一段对不上。

        【前提】音轨来源选「参考音乐」时，参考音乐**必须**在素材槽位里
        —— 音轨三选一的可见性判据就是「有没有对应素材」（见
        h3_audio_track.available_track_sources）。所以这条用例要带
        一条真实的参考音乐素材引用。
        """
        image_id, audio_id = self._real_asset_ids()
        if not audio_id:
            self.skipTest("素材库里没有可用的音频素材")
        job_id = self._seed_continuable_job_as_mode(
            "contfrontend-audio", "r2v",
            extra_params={
                "audio_track_source": "reference",
                "ref_audios": ["h3studio/orig.m4a"],
            },
            asset_refs=[
                {"asset_id": audio_id, "slot": "ref_audio", "ordinal": 0},
            ])
        # 往任务的 continuation 里塞音频接续状态（模拟链首段记录的）
        import sqlite3
        with sqlite3.connect(self.server.data_dir / "jobs.db") as conn:
            row = conn.execute("SELECT params FROM jobs WHERE id=?",
                               (job_id,)).fetchone()
            params = json.loads(row[0])
            params["continuation"].update({
                "audio_track_mode": "reference",
                "audio_source_kind": "ref_audio",
                "audio_source_file": "h3studio/orig.m4a",
                "audio_source_duration": 60.0,
                "audio_consumed_frames": 124,
                "audio_lead_blank": 1.0,
                "audio_segment_start": 5.166667,
                "audio_segment_duration": 4.25,
            })
            conn.execute("UPDATE jobs SET params=? WHERE id=?",
                         (json.dumps(params, ensure_ascii=False), job_id))
        self._goto_jobs()
        self.page.click(f'.job-card[data-jid="{job_id}"] [data-act="continue"]')
        self.page.wait_for_timeout(2200)
        track = self.page.evaluate(
            "() => (window.__h3State && window.__h3State.audioTrackSource) || ''")
        self.assertEqual(track, "reference",
                         "音轨来源没回填（接续必须以链首段为准）")

    def test_continue_panel_shows_audio_timing(self):
        """接续上下文区要显示**音频接续时序**（起点 / 时长 / 前导空白）。"""
        job_id = self._seed_continuable_job_as_mode(
            "contfrontend-audio-panel", "r2v")
        import sqlite3
        with sqlite3.connect(self.server.data_dir / "jobs.db") as conn:
            row = conn.execute("SELECT params FROM jobs WHERE id=?",
                               (job_id,)).fetchone()
            params = json.loads(row[0])
            params["continuation"].update({
                "audio_track_mode": "reference",
                "audio_source_kind": "ref_audio",
                "audio_source_file": "h3studio/orig.m4a",
                "audio_source_duration": 60.0,
                "audio_consumed_frames": 124,
                "audio_lead_blank": 1.0,
                "audio_segment_start": 5.166667,
                "audio_segment_duration": 4.25,
            })
            conn.execute("UPDATE jobs SET params=? WHERE id=?",
                         (json.dumps(params, ensure_ascii=False), job_id))
        self._goto_jobs()
        self.page.click(f'.job-card[data-jid="{job_id}"] [data-act="continue"]')
        self.page.wait_for_timeout(1800)
        text = self.page.evaluate("""() => {
          const p = document.getElementById('continuationPanel');
          return p ? p.textContent : '';
        }""")
        self.assertIn("音轨来源", text, "上下文区应显示音轨来源")
        self.assertIn("本段音频起点", text, "上下文区应显示音频起点")
        self.assertIn("前导空白", text, "上下文区应显示前导空白")

    def test_prompt_is_editable_and_enhance_available(self):
        """提示词**可编辑**且「增强」可用（用户需求）。

        【为什么断言这个】接续把提示词框清空（用户写下一段），
        所以必须确认用户能写、并且能点增强 —— 而不是被清空成
        不可编辑状态。
        """
        job_id = self._seed_continuable_job_as_mode("contfrontend-enh", "r2v")
        self._goto_jobs()
        self.page.click(f'.job-card[data-jid="{job_id}"] [data-act="continue"]')
        self.page.wait_for_timeout(1500)
        editable = self.page.evaluate("""() => {
          const t = document.getElementById('prompt');
          if (!t) return null;
          t.value = '下一段的提示词';
          t.dispatchEvent(new Event('input', {bubbles: true}));
          return {disabled: !!t.disabled, readOnly: !!t.readOnly,
                  value: t.value};
        }""")
        self.assertIsNotNone(editable, "找不到提示词框")
        self.assertFalse(editable["disabled"], "提示词框不该被禁用")
        self.assertFalse(editable["readOnly"], "提示词框不该只读")
        self.assertEqual(editable["value"], "下一段的提示词",
                         "提示词框应该可以编辑")

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
