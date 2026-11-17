# -*- coding: utf-8 -*-
"""真机 Chromium 复验：接续上下文面板里的**成片时长账**（2026-11-17）。

【为什么必须真机复验】
用户口径是「成片 = Σ(各段请求时长) - (段数-1) × 1 秒」。
而实测成片时长与这个口径有**小幅差**（H3 的 17k+5 帧网格 +
拷贝桥只接受 5+17k 合法钉住窗口）。这个差必须**让用户看得见**，
否则「我请求 5 秒接续 5 秒，为什么成片不是整 9 秒」无从判断。

【本测试证明什么】
  1. 点「接续」-> 接续上下文面板真的显示：
     * 「成片时长（按 1 秒重叠口径）」= 9.000 秒；
     * 「成片时长（实际帧数）」= 9.4167 秒 / 226 帧；
     * 「两者相差」= 0.4167 秒；
     * 分段明细（第1段请求 5s -> 交付 124 帧；第2段 -> 交付 102 帧）；
     * 差值原因（帧网格 + 重叠窗口）。
  2. 截图留证。

【口径来源】服务端 GET /api/jobs/{id}/continuation?duration=5
  -> context["duration_plan"]

运行：python3 -m pytest tests/test_cont_duration_ui_browser.py -v
"""

import json
import os
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
except Exception:                                    # noqa: BLE001
    HAS_PLAYWRIGHT = False

#: 截图落盘目录
SHOT_DIR = Path(os.environ.get(
    "H3STUDIO_SHOT_DIR",
    "/root/projects/h3studio-live/evidence/cont-duration-ui-chromium"))


@unittest.skipUnless(HAS_PLAYWRIGHT, "未安装 playwright")
class TestDurationPlanUI(unittest.TestCase):
    """真实 Chromium：接续上下文面板显示成片时长账。"""

    @classmethod
    def setUpClass(cls):
        from test_frontend_defaults import StudioServer
        cls.server = StudioServer(seed_jobs=0)
        assert cls.server.start(), "Studio 实例启动失败"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])
        cls.ctx = cls.browser.new_context(viewport={"width": 1440, "height": 1000})
        cls.page = cls.ctx.new_page()
        cls.errors = []
        cls.page.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.page.goto(f"http://127.0.0.1:{cls.server.port}/",
                      wait_until="domcontentloaded")
        cls.page.wait_for_timeout(2500)
        SHOT_DIR.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.stop()

    # ------------------------------------------------------------ 工具
    def _seed_chain(self):
        """造一条**1 段**接续链（段0 5s），造潜空间。

        【为什么要 1 段而不是 2 段】用户场景是「5 秒 -> 接续 5 秒 -> 成片 9 秒」：
        点「接续」时链上**只有第 1 段**，面板上填的 5 秒是**即将生成的
        第 2 段**的请求时长。所以：
          链上已有 1 段 + 下一段 5 秒 -> 成片账 = 2 段 = 9 秒（口径）/ 226 帧
        若种 2 段再问 duration=5，那算的是「3 段链」的口径（13 秒）。
        """
        import sqlite3
        from h3studio import continuation_service as cs

        seg0 = "duruiseg0"
        run_id = cs.segment_run_id(seg0)
        stage = cs.stage_path(run_id, 0)
        stage.parent.mkdir(parents=True, exist_ok=True)
        stage.write_bytes(b"fake-av-latent")
        p0 = {"prompt": "第1段：橘猫在旧书桌上走过。",
              "model_key": "fl2va_int8", "width": 768, "height": 448,
              "duration": 5, "steps": 4, "sampler": "euler",
              "scheduler": "simple", "gen_type": "t2v",
              "continuation": {
                  "chain_id": seg0, "stage_index": 0, "source_job": "",
                  "latent_run_id": run_id, "latent_stage": 0,
                  "context_frames": 22, "anchor_frames": 22,
                  "chain_total_frames": 0, "segment_frames": 124}}
        with sqlite3.connect(self.server.data_dir / "jobs.db") as conn:
            conn.execute(
                "INSERT OR REPLACE INTO jobs "
                "(id, created_at, mode, params, status, progress, outputs)"
                " VALUES (?,?,?,?,?,?,?)",
                (seg0, time.time(), "t2v",
                 json.dumps(p0, ensure_ascii=False),
                 "done", 1.0,
                 json.dumps([{"kind": "video",
                              "file": "videos/fake-cont.mp4",
                              "name": "fake-cont.mp4"}], ensure_ascii=False)))
        return seg0, seg0

    def _goto_jobs(self):
        self.page.evaluate("() => { if (window.location.hash !== '#jobs') "
                           "window.location.hash = '#jobs'; }")
        self.page.wait_for_timeout(500)
        self.page.evaluate("() => { if (window.refreshJobs) "
                           "window.refreshJobs(); }")
        self.page.wait_for_timeout(2500)
        try:
            self.page.wait_for_selector(
                '.job-card[data-jid="duruiseg0"]', timeout=15000)
        except Exception:                              # noqa: BLE001
            pass

    def _shoot(self, name):
        path = SHOT_DIR / f"{name}.png"
        self.page.screenshot(path=str(path), full_page=False)
        return path

    # ------------------------------------------------------------ 测试
    def test_context_endpoint_returns_duration_plan(self):
        """服务端接口必须给出 duration_plan（口径值 + 实际值 + 差值）。"""
        import urllib.request
        seg0, seg1 = self._seed_chain()
        url = (f"http://127.0.0.1:{self.server.port}"
               f"/api/jobs/{seg1}/continuation?duration=5")
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        self.assertTrue(data.get("ok"), "接续上下文失败：%r" % data)
        plan = data.get("duration_plan")
        self.assertIsInstance(plan, dict, "没有 duration_plan")
        # 用户口径：5 + (5-1) = 9 秒
        self.assertAlmostEqual(float(plan.get("formula_seconds")), 9.0,
                               places=6,
                               msg="按 1 秒重叠口径的成片时长应为 9 秒：%r" % plan)
        # 实际帧数：124 + 102 = 226 帧 = 9.4167 秒
        self.assertEqual(int(plan.get("total_frames")), 226,
                         "成片帧数应为 226：%r" % plan)
        self.assertAlmostEqual(float(plan.get("total_seconds")), 226 / 24,
                               places=4)
        # 差值 = 10 帧 = 0.4167 秒
        self.assertAlmostEqual(float(plan.get("delta_seconds")), 10 / 24,
                               places=4)
        self.assertTrue(plan.get("notes"), "没有差值原因说明")

    def test_browser_panel_shows_duration_account(self):
        """真实浏览器：面板上看得见「口径值 / 实际帧数 / 相差 / 分段明细」。"""
        seg0, seg1 = self._seed_chain()
        self._goto_jobs()

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
        self.page.wait_for_timeout(3000)

        # 面板内容
        panel_text = self.page.evaluate(
            """() => {
              const box = document.getElementById('continuationPanel');
              return box && !box.hidden ? box.innerText : '';
            }""")
        self.assertTrue(panel_text, "接续上下文面板没显示")
        print("\n--- 面板文本 ---\n" + panel_text + "\n---")

        self.assertIn("按 1 秒重叠口径", panel_text,
                      "面板没显示「按 1 秒重叠口径」的成片时长")
        self.assertIn("实际帧数", panel_text,
                      "面板没显示「实际帧数」的成片时长")
        self.assertIn("两者相差", panel_text,
                      "面板没显示「两者相差」")
        self.assertIn("9.000", panel_text,
                      "面板上的口径值不是 9.000 秒")
        self.assertIn("226", panel_text,
                      "面板上的成片帧数不是 226")
        # 分段明细
        self.assertIn("第1段", panel_text, "面板没有分段明细")
        self.assertIn("第2段", panel_text, "面板没有分段明细")
        # 差值原因
        self.assertIn("帧网格", panel_text,
                      "面板没说清楚差值来自帧网格约束")

        shot = self._shoot("01-continuation-duration-plan")
        self.assertTrue(shot.is_file(), "截图没落盘")
        print(f"截图：{shot}")

    def test_browser_panel_no_js_errors(self):
        """面板渲染不能有 JS 报错。"""
        seg0, seg1 = self._seed_chain()
        self._goto_jobs()
        self.page.evaluate(
            """(jobId) => {
              const card = document.querySelector(
                `.job-card[data-jid="${jobId}"]`);
              const btn = card && card.querySelector('[data-act="continue"]');
              if (btn) btn.click();
            }""", seg1)
        self.page.wait_for_timeout(2500)
        self.assertEqual([e for e in self.errors if "continuation" in e.lower()],
                         [], "接续面板渲染有 JS 报错")


if __name__ == "__main__":
    unittest.main()
