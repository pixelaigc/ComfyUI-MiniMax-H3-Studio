# -*- coding: utf-8 -*-
"""接续**跨段提示词增强**的浏览器端验收（真实 Chromium，2026-11-17）。

【这个测试在防什么】
用户需求：接续时点「增强」要把**此前所有段落**的提示词一并提供给
增强模型，保证跨段内容一致性。

前端链路（三段）：
  1. 服务端 /api/jobs/{id}/continuation 下发 `prompt_history`；
  2. continuation.js 存进接续上下文，并暴露 `Continuation.promptHistory()`；
  3. enhance-menu.js 的增强入口把历史透传给 /api/enhance。

【为什么必须用真实浏览器】
这三步里有两步在 JS 里（DOM 事件 + fetch 载荷）。
纯静态 grep 只能证明「字符串存在」，证明不了「真的被带上了」。

【怎么验证】
  * 造一条 2 段接续链（段0 -> 段1），两段各有自己的提示词；
  * 浏览器点段1 卡片上的「接续」；
  * **拦截 /api/enhance 的请求体**（Playwright route）；
  * 点「增强」；
  * 断言请求体里的 prompt_history 含**段0 的提示词**（前情）。

运行：python3 -m pytest tests/test_cont_prompt_history_browser.py -v
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
except Exception:                                    # noqa: BLE001
    HAS_PLAYWRIGHT = False


@unittest.skipUnless(HAS_PLAYWRIGHT, "未安装 playwright")
class TestContPromptHistoryBrowser(unittest.TestCase):
    """真实 Chromium 验证「历史提示词真的进了增强请求」。"""

    @classmethod
    def setUpClass(cls):
        # 【为什么这样导入】tests/ 不是包（没有 __init__.py），
        # 所以要用「把 tests/ 加进 sys.path 再按模块名导」的方式 ——
        # 与 tests/test_continuation_frontend.py 自己的做法一致。
        sys.path.insert(0, str(REPO / "tests"))
        try:
            from test_frontend_defaults import StudioServer
        finally:
            sys.path.remove(str(REPO / "tests"))
        cls.server = StudioServer(seed_jobs=0)
        assert cls.server.start(), "Studio 实例启动失败"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])
        cls.ctx = cls.browser.new_context(viewport={"width": 1280,
                                                     "height": 900})
        cls.page = cls.ctx.new_page()
        cls.errors = []
        cls.page.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.page.goto(f"http://127.0.0.1:{cls.server.port}/",
                      wait_until="domcontentloaded")
        cls.page.wait_for_timeout(2500)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.stop()

    # ------------------------------------------------------------ 工具
    def _seed_chain(self):
        """造一条 2 段接续链：段0 -> 段1，各有提示词。

        返回 (job_id_seg0, job_id_seg1)。
        """
        import sqlite3
        from h3studio import continuation_service as cs

        seg0, seg1 = "prompthistseg0", "prompthistseg1"
        run0 = cs.segment_run_id(seg0)
        stage = cs.stage_path(run0, 0)
        stage.parent.mkdir(parents=True, exist_ok=True)
        stage.write_bytes(b"fake-av-latent")
        # 【为什么段1 也要造潜空间】接续上下文的判定要查**本段**的
        # 潜空间文件在不在（不在这就返回 ok:false）—— 只造段0 的
        # 潜空间，段1 的上下文取不到。
        run1 = cs.segment_run_id(seg1)
        stage1 = cs.stage_path(run1, 0)
        stage1.parent.mkdir(parents=True, exist_ok=True)
        stage1.write_bytes(b"fake-av-latent")

        p0 = {
            "prompt": "一只橘猫在洒满阳光的旧书桌上走过，镜头缓缓跟随。",
            "model_key": "fl2va_int8",
            "width": 768, "height": 448,
            "duration": 5, "steps": 4,
            "accel_lora": "ckpt850", "accel_lora_strength": 1.0,
            "accel_loras": [{"key": "ckpt850", "strength": 1.0, "enabled": True}],
            "sampler": "euler", "scheduler": "simple",
            "gen_type": "t2v",
            "continuation": {
                "chain_id": seg0, "stage_index": 0,
                "source_job": "", "source_segment": -1,
                "latent_run_id": run0, "latent_stage": 0,
                "context_frames": 22, "anchor_frames": 22,
                "chain_total_frames": 0, "segment_frames": 124,
            },
        }
        p1 = {
            "prompt": "橘猫跳下书桌，钻进纸箱里，纸箱微微晃动。",
            "model_key": "fl2va_int8",
            "width": 768, "height": 448,
            "duration": 5, "steps": 4,
            "accel_lora": "ckpt850", "accel_lora_strength": 1.0,
            "accel_loras": [{"key": "ckpt850", "strength": 1.0, "enabled": True}],
            "sampler": "euler", "scheduler": "simple",
            "gen_type": "t2v",
            "continuation": {
                "chain_id": seg0, "stage_index": 1,
                "source_job": seg0, "source_segment": 0,
                "latent_run_id": cs.segment_run_id(seg1), "latent_stage": 0,
                "context_frames": 22, "anchor_frames": 22,
                "chain_total_frames": 124, "segment_frames": 124,
            },
        }
        with sqlite3.connect(self.server.data_dir / "jobs.db") as conn:
            # 【为什么 created_at 要错开】生成库按 created_at DESC 排、
            # 每页 12 条。两条用同一个 time.time() 时**顺序不确定** ——
            # seg0 可能排在 seg1 前面，seg1 就被挤到第 2 页，
            # 测试点不到它的卡片。错开 1 秒让 seg1 稳定地更新。
            for offset, (jid, params) in enumerate(
                    ((seg1, p1), (seg0, p0))):
                conn.execute(
                    "INSERT OR REPLACE INTO jobs "
                    "(id, created_at, mode, params, status, progress, outputs)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (jid, time.time() - offset * 60, "t2v",
                     json.dumps(params, ensure_ascii=False),
                     "done", 1.0,
                     json.dumps([{"kind": "video",
                                  "file": "videos/fake-cont.mp4",
                                  "name": "fake-cont.mp4"}], ensure_ascii=False)))
        return seg0, seg1

    def _goto_jobs(self):
        self.page.evaluate("() => { if (window.location.hash !== '#jobs') "
                           "window.location.hash = '#jobs'; }")
        self.page.wait_for_timeout(500)
        self.page.evaluate("() => { if (window.refreshJobs) "
                           "window.refreshJobs(); }")
        # 【为什么等 2500ms】生成库是**分页**渲染（每页 12 条）+ 异步拉取。
        # 只等 1500ms 时卡片还没渲染出来，点不到「接续」按钮。
        # （实测：等 1500ms -> 找不到卡片；等 2500ms -> 卡片与按钮都在。）
        self.page.wait_for_timeout(2500)
        # 再等一下「卡片真的出现」，而不是靠固定时间赌
        try:
            self.page.wait_for_selector(
                '.job-card[data-jid="prompthistseg1"]', timeout=15000)
        except Exception:                              # noqa: BLE001
            pass

    # ------------------------------------------------------------ 测试
    def test_continuation_context_includes_prompt_history(self):
        """接续上下文必须带 prompt_history（含前一段的提示词）。"""
        import urllib.request
        seg0, seg1 = self._seed_chain()
        url = (f"http://127.0.0.1:{self.server.port}"
               f"/api/jobs/{seg1}/continuation")
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        self.assertTrue(data.get("ok"), "接续上下文失败：%r" % data)
        history = data.get("prompt_history")
        self.assertIsInstance(history, list,
                              "接续上下文没有 prompt_history")
        self.assertGreaterEqual(len(history), 2,
                                "prompt_history 段数不对：%r" % (history,))
        prompts = [h.get("prompt", "") for h in history]
        # 【2026-11-19】断言按**提示词内容**判定，不再按「第1段 / 第2段」
        # 这种测试占位词 —— 那些词对模型毫无意义（用户 2026-11-19 实测反馈）。
        self.assertTrue(
            any("旧书桌上走过" in p for p in prompts),
            "prompt_history 里没有前一段的提示词：%r" % (prompts,))
        self.assertTrue(
            any("纸箱里" in p for p in prompts),
            "prompt_history 里没有本段的提示词：%r" % (prompts,))
        # 段号必须按顺序
        stages = [h.get("stage") for h in history]
        self.assertEqual(stages, sorted(stages),
                         "prompt_history 段号没排序：%r" % (stages,))

    def test_browser_prompt_history_reaches_enhance_request(self):
        """浏览器里点「接续」-> 点「增强」-> 请求体带历史。"""
        seg0, seg1 = self._seed_chain()
        self._goto_jobs()

        # ---- 拦截 /api/enhance，录下请求体 ----
        captured = []

        def handler(route, request):
            try:
                captured.append(request.post_data_json)
            except Exception:                          # noqa: BLE001
                captured.append(None)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"ok": True, "enhanced": "增强后的提示词",
                                  "original": "x", "merged": "x",
                                  "provider": "test", "spec_id": "video",
                                  "validation": {"ok": True}}))

        self.page.route("**/api/enhance", handler)

        # ---- 点段1 卡片上的「接续」----
        # 【选择器口径】与 tests/test_continuation_frontend.py 一致：
        # 卡片是 `.job-card[data-jid="..."]`，接续按钮是 `[data-act="continue"]`。
        clicked = self.page.evaluate(
            """(jobId) => {
              const card = document.querySelector(
                `.job-card[data-jid="${jobId}"]`);
              if (!card) return false;
              const btn = card.querySelector('[data-act="continue"]');
              if (!btn) return false;
              btn.click();
              return true;
            }""", seg1)
        self.assertTrue(clicked, "没找到段1 的卡片/接续按钮")

        # 等待接续上下文加载完
        self.page.wait_for_timeout(3000)

        # ---- 前端确实拿到了历史 ----
        history = self.page.evaluate(
            "() => (window.Continuation && "
            " typeof window.Continuation.promptHistory === 'function') "
            "? window.Continuation.promptHistory() : null")
        self.assertIsInstance(history, list,
                              "前端拿不到 promptHistory()")
        self.assertGreaterEqual(len(history), 2,
                                "前端 promptHistory() 段数不对：%r"
                                % (history,))

        # ---- 在提示词框里写本段内容，再点「增强」----
        self.page.evaluate(
            """() => {
              const box = document.querySelector('#prompt');
              if (box) {
                box.value = '橘猫从纸箱里探出头，好奇地张望。';
                box.dispatchEvent(new Event('input', {bubbles: true}));
              }
            }""")
        self.page.wait_for_timeout(300)

        # ---- 直接调用增强菜单的公开入口 ----
        # 【为什么这么调】增强按钮在菜单里，可能因为面板折叠而点不到。
        # 这里调**增强菜单暴露的同一个入口**（window.enhanceMethods.run
        # -> runBasic -> /api/enhance），验证的是**请求载荷**里有没有
        # 历史 —— 那才是本测试要证明的事。
        self.page.evaluate(
            """async () => {
              const em = window.enhanceMethods;
              if (!em || typeof em.run !== 'function') return;
              await em.run();
            }""")
        self.page.wait_for_timeout(1500)
        # 兜底：菜单没暴露就直接发一次请求，验证前端能构造出历史载荷
        if not captured:
            history_json = self.page.evaluate(
                """() => JSON.stringify(
                    (window.Continuation && window.Continuation.promptHistory)
                      ? window.Continuation.promptHistory() : [])""")
            self.assertTrue(history_json and history_json != "[]",
                            "前端拿不到任何提示词历史")

        # ---- 断言：请求体里带历史 ----
        self.assertTrue(captured, "没有捕获到 /api/enhance 请求")
        payload = captured[0]
        self.assertIsNotNone(payload, "/api/enhance 请求体不是 JSON")
        ph = payload.get("prompt_history")
        self.assertIsInstance(ph, list,
                              "增强请求里没有 prompt_history：%r"
                              % json.dumps(payload, ensure_ascii=False)[:400])
        self.assertGreaterEqual(len(ph), 1,
                                "增强请求里的 prompt_history 是空的")
        prompts = [str(x.get("prompt", "")) for x in ph
                   if isinstance(x, dict)]
        # 【2026-11-19】同上：按提示词内容判定，不用「第X段」占位词。
        self.assertTrue(
            any("旧书桌上走过" in p for p in prompts),
            "增强请求里没有前序段落的提示词（跨段一致性会丢）：%r"
            % (prompts,))
        self.assertTrue(
            any("纸箱里" in p for p in prompts),
            "增强请求里没有本段来源段的提示词：%r" % (prompts,))


if __name__ == "__main__":
    unittest.main()
