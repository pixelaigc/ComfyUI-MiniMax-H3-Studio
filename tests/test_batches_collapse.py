# -*- coding: utf-8 -*-
"""「批次结果」区块折叠/展开的回归测试（真实浏览器 + 真实后端）。

真实根因（不是事件绑定丢失，也不是 CSS 高度问题）：

    .job-batches { display: grid; }        ← 作者样式
    [hidden]       { display: none; }      ← 浏览器默认（UA 样式）

同一层级里 **作者样式优先于 UA 样式**，所以 JS 里的
`grid.hidden = true` 只是加了个属性，网格照样铺开占满一屏。
表现就是：默认展开、点三角「没反应」（其实状态变了，只是看不见）。

修法两条：
  1. CSS 补 `.job-batches[hidden] { display: none; }`（与本项目其它 5 个
     用 hidden 的元素保持同一约定）。
  2. 前端把折叠状态集中到 applyBatchesState()，三角用显式字形
     （展开 ▼ / 收纳 ▶）而不是 CSS 旋转，朝向永远和状态一一对应。

覆盖的用户可见要求：
  1. 默认收纳（不铺开）
  2. 单击标题行或三角可展开；展开后再单击可再次收纳（状态互斥）
  3. 刷新/重新渲染后折叠状态保持合理
  4. 折叠三角朝向与实际状态一致

运行：python3 -m pytest tests/test_batches_collapse.py -v
需要：playwright（chromium）。缺失时整类 skip，不算失败。
"""
import json
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

from test_frontend_defaults import StudioServer, WAIT_TIMEOUT  # noqa: E402


BATCH_JOB_ID = "batchcollapse01"


def seed_batch_job(data_dir, job_id=BATCH_JOB_ID, width=1920, height=1080):
    """在 Studio 的库里塞一个带 5 个批次（3 成功 1 失败 1 排队）的任务。"""
    conn = sqlite3.connect(data_dir / "jobs.db")
    conn.execute(
        "INSERT OR REPLACE INTO jobs (id, created_at, mode, params, status,"
        " progress, progress_text, outputs) VALUES (?,?,?,?,?,?,?,?)",
        (job_id, time.time(), "t2i",
         json.dumps({"title": "批次折叠探针", "prompt": "批次折叠探针",
                     "aspect": "16:9", "width": width, "height": height,
                     "batch_count": 5}, ensure_ascii=False),
         "done", 1.0, "", "[]"))
    conn.execute("DELETE FROM job_batches WHERE job_id=?", (job_id,))
    plan = [("done", ""), ("done", ""), ("done", ""),
            ("failed", "boom"), ("queued", "")]
    for idx, (status, err) in enumerate(plan):
        conn.execute(
            "INSERT INTO job_batches (id, job_id, idx, seed, status, progress,"
            " progress_text, error, outputs, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"bb{idx}", job_id, idx, 1000 + idx, status,
             1.0 if status == "done" else 0.0, "", err, "[]", time.time()))
    conn.commit()
    conn.close()


@unittest.skipUnless(HAS_PLAYWRIGHT, "未安装 playwright")
class TestBatchesCollapse(unittest.TestCase):
    """真实浏览器 + 真实 Studio 实例，断言批次结果区块的折叠行为。"""

    @classmethod
    def setUpClass(cls):
        cls.server = StudioServer(seed_jobs=0)
        assert cls.server.start(), "Studio 实例启动失败"
        seed_batch_job(cls.server.data_dir)
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])
        cls.ctx = cls.browser.new_context(viewport={"width": 1400, "height": 1000})
        cls.page = cls.ctx.new_page()
        cls.errors = []
        cls.page.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.page.goto("http://127.0.0.1:%d/" % cls.server.port,
                      wait_until="domcontentloaded")
        cls.page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid [data-act=\"toggle-batches\"]').length > 0",
            timeout=WAIT_TIMEOUT)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.stop()

    # ------------------------------------------------------------ 工具
    def toggle(self):
        """返回该卡片的折叠状态快照（DOM 事实，不是文案推断）。"""
        return self.page.evaluate("""
            () => {
              const btn = document.querySelector('#jobsGrid [data-act="toggle-batches"]');
              const grid = btn.querySelector('[data-batches]') ||
                           btn.parentElement.querySelector('[data-batches]');
              const caret = btn.querySelector('.caret');
              return {
                hiddenAttr: grid.hasAttribute('hidden'),
                display: getComputedStyle(grid).display,
                height: grid.getBoundingClientRect().height,
                openClass: btn.classList.contains('open'),
                ariaExpanded: btn.getAttribute('aria-expanded'),
                caret: caret ? caret.textContent.trim() : null,
                title: btn.textContent.replace(/\\s+/g, ' ').trim(),
              };
            }
        """)

    def click_toggle(self, target="caret"):
        """真实鼠标点击（Playwright 会自动滚到可见），而不是合成 click()。"""
        sel = ('#jobsGrid [data-act="toggle-batches"] .caret' if target == "caret"
               else '#jobsGrid [data-act="toggle-batches"]')
        self.page.locator(sel).first.click()
        self.page.wait_for_timeout(350)

    def reset_collapsed(self):
        """把状态复位到收纳（直接改 expandedBatches，避免用例互相污染）。"""
        self.page.evaluate("() => { if (typeof expandedBatches !== 'undefined') expandedBatches.clear(); }")
        self.page.evaluate("() => refreshJobs()")
        self.page.wait_for_timeout(700)

    # ---------------------------------------------------- 1. 默认收纳
    def test_batches_collapsed_by_default(self):
        """默认必须是收纳的：网格不可见、零高度。"""
        self.reset_collapsed()
        s = self.toggle()
        self.assertTrue(s["hiddenAttr"], "默认应收纳（hidden 属性应存在）")
        self.assertEqual(s["display"], "none",
                         "默认收纳时网格必须 display:none，实际 %r" % s["display"])
        self.assertEqual(s["height"], 0,
                         "默认收纳时网格高度应为 0，实际 %r" % s["height"])
        self.assertFalse(s["openClass"], "默认不该带 open 类")
        self.assertEqual(s["ariaExpanded"], "false", "默认 aria-expanded 应为 false")

    def test_hidden_attribute_actually_hides_grid(self):
        """根因防线：.job-batches 的 display:grid 会盖掉 UA 的 [hidden]。

        必须补 `.job-batches[hidden] { display: none; }`，
        否则 hidden 属性形同虚设（这正是本次的真实根因）。
        """
        css = (REPO / "web" / "style.css").read_text(encoding="utf-8")
        self.assertRegex(
            css, r"\.job-batches\[hidden\]\s*\{\s*display:\s*none",
            "style.css 缺少 .job-batches[hidden] { display: none; } —— "
            "hidden 属性会被 .job-batches 的 display:grid 盖掉")

    # ---------------------------------------------------- 2. 展开/收纳互斥
    def test_click_caret_expands(self):
        """点三角能展开。"""
        self.reset_collapsed()
        self.click_toggle("caret")
        s = self.toggle()
        self.assertFalse(s["hiddenAttr"], "点三角后应收纳（hidden 应移除）")
        self.assertEqual(s["display"], "grid", "展开后网格应可见")
        self.assertGreater(s["height"], 0, "展开后网格高度应 > 0")
        self.assertTrue(s["openClass"], "展开后应带 open 类")
        self.assertEqual(s["ariaExpanded"], "true", "展开后 aria-expanded 应为 true")

    def test_click_again_collapses(self):
        """展开后再点一次能收纳回去（状态互斥，不会卡在展开态）。"""
        self.reset_collapsed()
        self.click_toggle("caret")
        self.assertEqual(self.toggle()["display"], "grid", "前置条件：应已展开")
        self.click_toggle("caret")
        s = self.toggle()
        self.assertTrue(s["hiddenAttr"], "再点一次应收纳")
        self.assertEqual(s["display"], "none", "再点一次后应 display:none")
        self.assertEqual(s["height"], 0, "再点一次后高度应为 0")
        self.assertFalse(s["openClass"], "再点一次后不该带 open 类")
        self.assertEqual(s["ariaExpanded"], "false", "再点一次后 aria-expanded 应为 false")

    def test_click_title_row_toggles(self):
        """点标题行（不点三角字形本身）同样能展开/收纳。"""
        self.reset_collapsed()
        self.click_toggle("row")
        s = self.toggle()
        self.assertEqual(s["display"], "grid", "点标题行应能展开")
        self.assertEqual(s["height"] > 0, True, "展开后高度应 > 0")
        self.click_toggle("row")
        s = self.toggle()
        self.assertEqual(s["display"], "none", "再点标题行应收纳")
        self.assertEqual(s["height"], 0, "收纳后高度应为 0")

    def test_toggle_is_exclusive_across_clicks(self):
        """连点 N 次，展开/收纳必须严格交替，不出现「点了没反应」。"""
        self.reset_collapsed()
        seen = []
        for _ in range(6):
            self.click_toggle("caret")
            seen.append(self.toggle()["display"])
        self.assertEqual(
            seen, ["grid", "none", "grid", "none", "grid", "none"],
            "展开/收纳未严格互斥: %r" % (seen,))

    # ---------------------------------------------------- 3. 三角朝向
    def test_caret_direction_matches_state(self):
        """三角朝向必须与状态一致：收纳 ▶ / 展开 ▼。"""
        self.reset_collapsed()
        collapsed = self.toggle()
        self.assertEqual(collapsed["caret"], "▶",
                         "收纳时三角应为 ▶，实际 %r" % collapsed["caret"])
        self.click_toggle("caret")
        expanded = self.toggle()
        self.assertEqual(expanded["caret"], "▼",
                         "展开时三角应为 ▼，实际 %r" % expanded["caret"])
        self.click_toggle("caret")
        self.assertEqual(self.toggle()["caret"], "▶",
                         "再次收纳后三角应回到 ▶")

    # ---------------------------------------------------- 4. 重渲染后保持
    def test_state_survives_auto_refresh(self):
        """实时刷新（setInterval(refreshJobs,2000)）后折叠状态要保持。"""
        self.reset_collapsed()
        self.click_toggle("caret")
        self.assertEqual(self.toggle()["display"], "grid", "前置条件：应已展开")
        # 等两轮自动刷新过去
        self.page.wait_for_timeout(4500)
        s = self.toggle()
        self.assertEqual(s["display"], "grid",
                         "自动刷新后展开状态丢失了，实际 %r" % s["display"])
        self.assertEqual(s["caret"], "▼", "自动刷新后三角朝向应仍是 ▼")

    def test_state_survives_card_rebuild(self):
        """卡片被重建（批次状态变化 → jobSig 变 → replaceWith）后状态要保持。"""
        self.reset_collapsed()
        self.click_toggle("caret")
        self.assertEqual(self.toggle()["display"], "grid", "前置条件：应已展开")
        # 改批次状态 → jobSig 变化 → 卡片被重建
        conn = sqlite3.connect(self.server.data_dir / "jobs.db")
        conn.execute(
            "UPDATE job_batches SET status='done', progress=1.0, error='' "
            "WHERE job_id=? AND idx=4", (BATCH_JOB_ID,))
        conn.commit()
        conn.close()
        self.page.wait_for_timeout(3500)
        s = self.toggle()
        self.assertEqual(s["display"], "grid",
                         "卡片重建后展开状态丢失了，实际 %r" % s["display"])
        self.assertEqual(s["caret"], "▼", "卡片重建后三角朝向应仍是 ▼")

    def test_collapsed_state_survives_rebuild(self):
        """收纳状态同样要在重建后保持（不能自动弹开）。"""
        self.reset_collapsed()
        self.assertEqual(self.toggle()["display"], "none", "前置条件：应收纳")
        conn = sqlite3.connect(self.server.data_dir / "jobs.db")
        conn.execute(
            "UPDATE job_batches SET progress=0.5 WHERE job_id=? AND idx=1",
            (BATCH_JOB_ID,))
        conn.commit()
        conn.close()
        self.page.wait_for_timeout(3500)
        s = self.toggle()
        self.assertEqual(s["display"], "none",
                         "卡片重建后收纳状态被弹开了，实际 %r" % s["display"])
        self.assertEqual(s["caret"], "▶", "卡片重建后三角朝向应仍是 ▶")

    # ---------------------------------------------------- 5. 标题行摘要
    def test_collapsed_title_shows_summary(self):
        """折叠时标题行要给出摘要（完成数 + 尺寸），不展开也能看懂。"""
        self.reset_collapsed()
        s = self.toggle()
        self.assertIn("批次结果", s["title"])
        self.assertIn("3/5", s["title"],
                      "标题行应显示完成/总数，实际 %r" % s["title"])
        self.assertIn("1920×1080", s["title"],
                      "标题行应显示出图尺寸，实际 %r" % s["title"])

    def test_no_console_errors(self):
        """折叠相关改动没引入 JS 报错。"""
        self.assertEqual([e for e in self.errors if e.strip()], [])


if __name__ == "__main__":
    unittest.main()
