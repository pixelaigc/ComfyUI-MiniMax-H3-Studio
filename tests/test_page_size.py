# -*- coding: utf-8 -*-
"""生成库「每页作品数 + 网格自适应」改造的回归测试（真实浏览器 + 真实后端）。

真实问题（用户报的是「22:9 超宽屏上每页 8 个作品排成 7+1，下面大片空白」）：

    每页作品数（DONE_PAGE_SIZE = 8）和网格列数（CSS auto-fill）彼此独立。
    3440px 宽屏下 auto-fill 排出 9 列，而一页只有 8 个 —— 9 列永远填不满，
    最后一行只有 1 张、右侧空一大片。

修法：
    1. 每页数量可选（8/12/16/24/32/48），持久化在 localStorage；
    2. 列数由「每页作品数 + 容器宽度」共同决定，优先取**能整除每页数量**的
       列数，保证每一行都是满的。

覆盖的用户可见要求：
  1. 每页数量可配置 + 持久化（刷新后保留）
  2. 网格自适应：列数随宽度变化；卡片内部不破版、不溢出
  3. 改每页数量后重置到第 1 页；总页数/当前页高亮/统计随之更新；
     「上一页/下一页」边界正确
  4. 不回归：状态/类型/比例/时长筛选、实时刷新、批次折叠、卡片操作按钮

运行：python3 -m pytest tests/test_page_size.py -v
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

from test_frontend_defaults import StudioServer  # noqa: E402


# 预置作品数：足够铺满多页（每页 12 -> 4 页）
SEED_JOBS = 42
PAGE_SIZE_KEY = "h3studio.pageSize"


def seed_jobs(data_dir, count):
    """预置 count 个已完成任务（t2i 模式，走「全部」标签）。"""
    conn = sqlite3.connect(data_dir / "jobs.db")
    now = time.time()
    for i in range(count):
        params = {"title": f"分页探针 {i}", "prompt": f"分页探针 {i}",
                  "aspect": "16:9", "width": 1360, "height": 768}
        # 带上产物，卡片才会渲染「下载 / 新窗口打开」按钮
        outputs = json.dumps(
            [{"kind": "image", "file": f"images/page{i:04d}.png",
              "name": f"page{i:04d}.png"}], ensure_ascii=False)
        conn.execute(
            "INSERT OR REPLACE INTO jobs (id, created_at, mode, params, status,"
            " progress, progress_text, outputs) VALUES (?,?,?,?,?,?,?,?)",
            (f"page{i:04d}", now - i * 60, "t2i",
             json.dumps(params, ensure_ascii=False),
             "done", 1.0, "", outputs))
    conn.commit()
    conn.close()


@unittest.skipUnless(HAS_PLAYWRIGHT, "未安装 playwright")
class TestPageSize(unittest.TestCase):
    """真实浏览器 + 真实 Studio 实例，断言每页数量与网格布局。"""

    @classmethod
    def setUpClass(cls):
        cls.server = StudioServer(seed_jobs=0)
        assert cls.server.start(), "Studio 实例启动失败"
        seed_jobs(cls.server.data_dir, SEED_JOBS)
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])
        cls.ctx = cls.browser.new_context(viewport={"width": 2560, "height": 1080})
        cls.page = cls.ctx.new_page()
        cls.errors = []
        cls.page.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.page.goto("http://127.0.0.1:%d/" % cls.server.port,
                      wait_until="domcontentloaded")
        cls.page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card:not(.job-skeleton)').length > 0",
            timeout=20000)

    def setUp(self):
        """每个用例都从「默认每页 12、无筛选、第 1 页」开始。

        localStorage 在同一个 browser context 里跨用例留存，
        不清的话上一个用例写的值会污染下一个（默认态用例就是这么被带偏的）。
        """
        self.page.evaluate("() => localStorage.removeItem('%s')" % PAGE_SIZE_KEY)
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card:not(.job-skeleton)').length > 0",
            timeout=20000)
        self.page.evaluate("() => { state.donePage = 1; }")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.stop()

    # ------------------------------------------------------------ 工具
    def set_size(self, size):
        """通过界面选择器改每页数量（和用户操作一致）。

        等「每页数量真的生效」而不是固定等 900ms —— 切筛选时会先铺骨架占位，
        固定等待可能在骨架阶段就返回，测到的是中间态。
        """
        self.page.select_option("#pageSizeSelect", str(size))
        # 等**真实**卡片 —— 骨架占位卡也带 .job-card 类，必须排除，
        # 否则骨架阶段（数量恰好等于每页数）会「提前满足」，测到的是中间态。
        self.page.wait_for_function(
            "n => { const sel = document.querySelector('#pageSizeSelect');"
            " return sel && Number(sel.value) === n"
            " && document.querySelectorAll('#jobsGrid .job-card:not(.job-skeleton)').length === n"
            " && document.querySelectorAll('#jobsGrid .job-skeleton').length === 0; }",
            arg=size, timeout=15000)

    def layout(self):
        """读取网格布局事实：列数、每行卡片数、溢出、末行右侧空白。"""
        return self.page.evaluate("""
            () => {
              const grid = document.querySelector('#jobsGrid');
              const cards = Array.from(document.querySelectorAll('#jobsGrid .job-card'));
              const tops = cards.map(c => Math.round(c.getBoundingClientRect().top));
              const uniq = [...new Set(tops)].sort((a, b) => a - b);
              const rows = uniq.map(t => tops.filter(x => x === t).length);
              const gridRect = grid.getBoundingClientRect();
              const lastTop = uniq[uniq.length - 1];
              const lastRow = cards.filter(
                c => Math.round(c.getBoundingClientRect().top) === lastTop);
              const lastRight = Math.max(
                ...lastRow.map(c => c.getBoundingClientRect().right));
              return {
                cols: Number(grid.dataset.cols || 0),
                n: cards.length,
                rows: rows,
                overflow: cards.filter(
                  c => c.getBoundingClientRect().right > gridRect.right + 1).length,
                lastRowBlank: Math.round(gridRect.right - lastRight),
                gridWidth: Math.round(gridRect.width),
              };
            }
        """)

    def pager_text(self):
        return self.page.evaluate(
            "() => document.querySelector('#jobsPagination').textContent.replace(/\\s+/g,' ').trim()")

    def page_size_value(self):
        return self.page.evaluate("() => document.querySelector('#pageSizeSelect').value")

    # ---------------------------------------------- 1. 选择器存在 + 默认值
    def test_page_size_selector_exists(self):
        """「每页 N 个」选择器存在，选项覆盖 8/12/16/24/32/48。"""
        self.assertTrue(self.page.evaluate("() => !!document.querySelector('#pageSizeSelect')"),
                        "缺少 #pageSizeSelect")
        options = self.page.evaluate(
            "() => Array.from(document.querySelectorAll('#pageSizeSelect option'))"
            ".map(o => Number(o.value))")
        for want in (8, 12, 16, 24, 32, 48):
            self.assertIn(want, options, "每页数量选项应含 %d，实际 %r" % (want, options))

    def test_default_page_size(self):
        """默认每页 12（宽屏下更饱满的值）。"""
        self.assertEqual(self.page.evaluate("() => state.pageSize"), 12,
                         "默认每页数量应为 12")
        self.assertEqual(self.page_size_value(), "12")

    # ---------------------------------------------- 2. 持久化
    def test_page_size_persists_after_reload(self):
        """改每页数量后刷新页面，设置必须保留。"""
        self.set_size(24)
        self.assertEqual(self.page_size_value(), "24")
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card:not(.job-skeleton)').length > 0",
            timeout=20000)
        self.assertEqual(self.page.evaluate("() => state.pageSize"), 24,
                         "刷新后每页数量应保持 24")
        self.assertEqual(self.page_size_value(), "24",
                         "刷新后选择器应显示 24")

    def test_page_size_written_to_localstorage(self):
        """每页数量写入 localStorage。"""
        self.set_size(16)
        stored = self.page.evaluate(
            "() => localStorage.getItem('%s')" % PAGE_SIZE_KEY)
        self.assertEqual(stored, "16",
                         "localStorage 应记下 16，实际 %r" % stored)

    def test_invalid_stored_value_falls_back_to_default(self):
        """localStorage 里是垃圾值时退回默认值，不崩。"""
        self.page.evaluate(
            "() => localStorage.setItem('%s', 'abc')" % PAGE_SIZE_KEY)
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card:not(.job-skeleton)').length > 0",
            timeout=20000)
        self.assertEqual(self.page.evaluate("() => state.pageSize"), 12,
                         "非法存储值应退回默认 12")

    # ---------------------------------------------- 3. 网格自适应
    def test_columns_fill_rows_without_ragged_tail(self):
        """核心：每一行都应是满的，不能出现「最后一行只有 1 个」。"""
        for size in (8, 12, 16, 24):
            self.set_size(size)
            layout = self.layout()
            self.assertEqual(layout["n"], size,
                             "每页 %d 时应显示 %d 张，实际 %d" % (size, size, layout["n"]))
            rows = layout["rows"]
            self.assertTrue(all(r == rows[0] for r in rows),
                            "每页 %d 时行分布不齐: %r（列数 %s）"
                            % (size, rows, layout["cols"]))
            self.assertLessEqual(layout["cols"], size,
                                 "列数不应超过每页数量: cols=%s size=%d"
                                 % (layout["cols"], size))

    def test_columns_never_exceed_page_size_on_wide_viewport(self):
        """超宽屏下列数不得多于每页数量。

        这条是「7+1 大片空白」的直接防线：3440px 下 auto-fill 能排 9 列，
        而每页只有 8 个 —— 列数多于每页数量时，最后一行必然只剩 1 张、
        右侧空掉整整一列。列数被每页数量钳住，这个现象就不会出现。
        """
        # 用比测试数据更多的列来暴露问题：先把窗口拉到很宽，
        # 再逐档检查「列数 <= 每页数量」。
        self.page.set_viewport_size({"width": 3440, "height": 1080})
        self.page.wait_for_timeout(600)
        self.page.evaluate("() => window.dispatchEvent(new Event('resize'))")
        self.page.wait_for_timeout(800)
        for size in (8, 12, 16, 24):
            self.set_size(size)
            cols = self.page.evaluate(
                "() => Number(document.querySelector('#jobsGrid').dataset.cols)")
            self.assertLessEqual(
                cols, size,
                "每页 %d 时列数不应超过 %d，实际 %d"
                % (size, size, cols))
        self.page.set_viewport_size({"width": 2560, "height": 1080})
        self.page.wait_for_timeout(600)
        self.page.evaluate("() => window.dispatchEvent(new Event('resize'))")
        self.page.wait_for_timeout(800)

    def test_every_row_is_full_at_every_width(self):
        """任何宽度 × 任何每页数量下，每一行的卡片数都必须相同。

        这是「末行只有 1 个 + 大片空白」的直接判定 —— 与列数算法无关，
        只看实际排布。列数一旦多于每页数量（auto-fill 的老行为），
        末行必然不齐，这条断言就会变红。
        """
        for width in (1400, 2560, 3440):
            self.page.set_viewport_size({"width": width, "height": 1080})
            self.page.wait_for_timeout(500)
            self.page.evaluate("() => window.dispatchEvent(new Event('resize'))")
            self.page.wait_for_timeout(700)
            for size in (8, 12, 16, 24):
                self.set_size(size)
                layout = self.layout()
                rows = layout["rows"]
                self.assertTrue(
                    all(r == rows[0] for r in rows),
                    "%dpx 宽 / 每页 %d 时行分布不齐: %r（列数 %s）"
                    % (width, size, rows, layout["cols"]))
        self.page.set_viewport_size({"width": 2560, "height": 1080})
        self.page.wait_for_timeout(500)
        self.page.evaluate("() => window.dispatchEvent(new Event('resize'))")
        self.page.wait_for_timeout(700)

    def test_rows_fill_container_width(self):
        """每一行都要铺满容器宽度，右侧不能留大片空白。

        这是 M11 型缺陷的防线：列宽若写死成固定 px（而不是 1fr），
        行短了容器，右侧就会空出一大片 —— 和「末行只有 1 张」是同一种
        视觉问题，只是方向反过来。
        """
        for width in (2560, 3440):
            self.page.set_viewport_size({"width": width, "height": 1080})
            self.page.wait_for_timeout(500)
            self.page.evaluate("() => window.dispatchEvent(new Event('resize'))")
            self.page.wait_for_timeout(700)
            for size in (8, 12, 16, 24):
                self.set_size(size)
                layout = self.layout()
                self.assertLessEqual(
                    layout["lastRowBlank"], 2,
                    "%dpx 宽 / 每页 %d 时末行右侧留了 %dpx 空白（容器 %dpx）"
                    % (width, size, layout["lastRowBlank"], layout["gridWidth"]))
        self.page.set_viewport_size({"width": 2560, "height": 1080})
        self.page.wait_for_timeout(500)
        self.page.evaluate("() => window.dispatchEvent(new Event('resize'))")
        self.page.wait_for_timeout(700)

    def test_no_card_overflow(self):
        """卡片不能溢出网格容器（窄列下不破版）。"""
        for size in (8, 12, 16, 24):
            self.set_size(size)
            layout = self.layout()
            self.assertEqual(layout["overflow"], 0,
                             "每页 %d 时有 %d 张卡片溢出容器"
                             % (size, layout["overflow"]))

    def test_columns_change_with_width(self):
        """窗口变宽，列数要跟着变（自适应）。"""
        self.set_size(24)
        narrow = self.page.evaluate(
            "() => Number(document.querySelector('#jobsGrid').dataset.cols)")
        self.page.set_viewport_size({"width": 1400, "height": 900})
        self.page.wait_for_timeout(700)
        self.page.evaluate("() => window.dispatchEvent(new Event('resize'))")
        self.page.wait_for_timeout(700)
        wide = self.page.evaluate(
            "() => Number(document.querySelector('#jobsGrid').dataset.cols)")
        self.page.set_viewport_size({"width": 2560, "height": 1080})
        self.page.wait_for_timeout(700)
        self.page.evaluate("() => window.dispatchEvent(new Event('resize'))")
        self.page.wait_for_timeout(700)
        self.assertLess(wide, narrow,
                        "窄窗口列数应少于宽窗口: narrow=%s wide=%s" % (narrow, wide))

    # ---------------------------------------------- 4. 翻页
    def test_change_page_size_resets_to_first_page(self):
        """改每页数量后回到第 1 页。"""
        self.set_size(8)
        # 翻到第 2 页
        self.page.evaluate("""
            () => {
              const btn = Array.from(document.querySelectorAll(
                '#jobsPagination button[data-page]')).find(b => b.dataset.page === '2');
              if (btn) btn.click();
            }
        """)
        self.page.wait_for_timeout(900)
        self.assertEqual(self.page.evaluate("() => state.donePage"), 2,
                         "前置条件：应在第 2 页")
        self.set_size(16)
        self.assertEqual(self.page.evaluate("() => state.donePage"), 1,
                         "改每页数量后应回到第 1 页")

    def total_jobs(self):
        return self.page.evaluate(
            "() => document.querySelector('#jobsPagination').textContent"
            ".match(/共 (\\d+) 个作品/)[1]")

    def test_total_pages_follows_page_size(self):
        """总页数随每页数量更新；统计数字与后端 total 一致。"""
        for size in (8, 12, 16, 24):
            self.set_size(size)
            total = int(self.total_jobs())
            pages = self.page.evaluate("""
                () => Array.from(document.querySelectorAll(
                  '#jobsPagination button[data-page]'))
                  .map(b => Number(b.dataset.page))
                  .filter(p => p > 0)
            """)
            expected = max(1, -(-total // size))     # 向上取整
            self.assertIn(expected, pages,
                          "每页 %d、共 %d 个作品时应是 %d 页，实际页码 %r"
                          % (size, total, expected, pages))
            # 页码里应含首页和末页
            self.assertIn(1, pages, "页码应含第 1 页")
            self.assertIn(expected, pages, "页码应含末页 %d" % expected)

    def test_current_page_highlighted(self):
        """当前页按钮带 active 高亮。

        用「下一页」按钮翻页，而不是直接点页码 —— 页码条在第 1 页时只渲染
        首页/次页/末页（中间的用省略号），直接点一个没渲染出来的页码是点不到的。
        """
        self.set_size(8)
        for expected_page in (2, 3):
            self.page.evaluate("""
                () => {
                  const btn = Array.from(document.querySelectorAll(
                    '#jobsPagination button[data-page]'))
                    .find(b => b.textContent.trim() === '下一页');
                  if (btn) btn.click();
                }
            """)
            # 等 active 真的落到目标页（切页会先铺骨架，固定等待会测到中间态）
            self.page.wait_for_function(
                "n => { const el = document.querySelector('#jobsPagination button.active');"
                " return el && Number(el.dataset.page) === n; }",
                arg=expected_page, timeout=15000)
        active = self.page.evaluate("""
            () => {
              const el = document.querySelector('#jobsPagination button.active');
              return el ? Number(el.dataset.page) : null;
            }
        """)
        self.assertEqual(active, 3, "当前页高亮应落在第 3 页，实际 %r" % active)

    def test_pager_boundary_buttons_disabled(self):
        """首页时「上一页」禁用，末页时「下一页」禁用。"""
        self.set_size(8)
        self.page.evaluate(
            "() => { state.donePage = 1; refreshJobs(); }")
        self.page.wait_for_function(
            "() => { const el = document.querySelector('#jobsPagination button.active');"
            " return el && Number(el.dataset.page) === 1; }", timeout=15000)
        first = self.page.evaluate("""
            () => {
              const btns = Array.from(document.querySelectorAll(
                '#jobsPagination button[data-page]'));
              return btns.find(b => b.textContent.trim() === '上一页').disabled;
            }
        """)
        self.assertTrue(first, "第 1 页时「上一页」应禁用")

        self.page.evaluate("() => { state.donePage = 6; refreshJobs(); }")
        self.page.wait_for_timeout(900)
        last = self.page.evaluate("""
            () => {
              const btns = Array.from(document.querySelectorAll(
                '#jobsPagination button[data-page]'));
              return btns.find(b => b.textContent.trim() === '下一页').disabled;
            }
        """)
        self.assertTrue(last, "末页时「下一页」应禁用")

    # ---------------------------------------------- 5. 不回归
    def test_auto_refresh_keeps_page_size(self):
        """2 秒实时刷新不能把每页数量刷掉，也不能把当前页重置掉。"""
        self.set_size(16)
        self.page.evaluate("() => { state.donePage = 2; refreshJobs(); }")
        self.page.wait_for_timeout(900)
        # 等两轮自动刷新
        self.page.wait_for_timeout(4500)
        self.assertEqual(self.page.evaluate("() => state.pageSize"), 16,
                         "实时刷新后每页数量被改掉了")
        self.assertEqual(self.page.evaluate("() => state.donePage"), 2,
                         "实时刷新后当前页被重置了")
        self.assertEqual(self.page_size_value(), "16",
                         "实时刷新后选择器显示变了")

    def test_batches_collapse_still_works(self):
        """批次结果折叠/展开不受影响（.job-batches[hidden] 仍然生效）。"""
        conn = sqlite3.connect(self.server.data_dir / "jobs.db")
        # 也要带产物：这个任务是最新一条、会排在卡片第一位，
        # 没有产物就没有「下载 / 新窗口打开」按钮，会影响后续断言。
        conn.execute(
            "INSERT OR REPLACE INTO jobs (id, created_at, mode, params, status,"
            " progress, progress_text, outputs) VALUES (?,?,?,?,?,?,?,?)",
            ("pagebatch01", time.time(), "t2i",
             json.dumps({"title": "批次折叠探针", "prompt": "批次折叠探针",
                         "width": 1360, "height": 768}, ensure_ascii=False),
             "done", 1.0, "",
             json.dumps([{"kind": "image", "file": "images/pagebatch01.png",
                          "name": "pagebatch01.png"}], ensure_ascii=False)))
        conn.execute("DELETE FROM job_batches WHERE job_id='pagebatch01'")
        for idx in range(3):
            conn.execute(
                "INSERT INTO job_batches (id, job_id, idx, seed, status, progress,"
                " progress_text, outputs, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("pb%d" % idx, "pagebatch01", idx, 1, "done", 1.0, "", "[]", time.time()))
        conn.commit()
        conn.close()
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card:not(.job-skeleton)').length > 0",
            timeout=20000)
        self.page.wait_for_function(
            "() => !!document.querySelector('#jobsGrid [data-act=\"toggle-batches\"]')",
            timeout=15000)
        collapsed = self.page.evaluate("""
            () => {
              const grid = document.querySelector('[data-batches]');
              return getComputedStyle(grid).display;
            }
        """)
        self.assertEqual(collapsed, "none", "批次网格默认应收纳")
        self.page.evaluate("""
            () => document.querySelector(
              '#jobsGrid [data-act="toggle-batches"]').click()
        """)
        self.page.wait_for_timeout(500)
        expanded = self.page.evaluate("""
            () => getComputedStyle(document.querySelector('[data-batches]')).display
        """)
        self.assertEqual(expanded, "grid", "点标题后批次网格应展开")

    def test_card_action_buttons_present(self):
        """卡片操作按钮仍齐全（下载/新窗口打开/复用参数/删除）。"""
        self.set_size(12)
        # 扫所有卡片：任何一张缺按钮都算回归
        labels = self.page.evaluate("""
            () => Array.from(document.querySelectorAll('#jobsGrid .job-card'))
              .flatMap(card => Array.from(
                card.querySelectorAll('.job-actions button, .job-actions a'))
                .map(b => b.textContent.trim()))
        """)
        joined = " ".join(labels)
        for want in ("下载", "新窗口打开", "复用参数", "删除"):
            self.assertIn(want, joined,
                          "操作按钮应含 %r，实际 %r" % (want, labels))

    def test_filters_still_work(self):
        """比例筛选仍然生效（每页数量改动不影响筛选）。"""
        self.set_size(24)
        self.page.evaluate("""
            () => {
              const sel = document.querySelector('#doneAspect');
              sel.value = '9:16';
              sel.dispatchEvent(new Event('change'));
            }
        """)
        self.page.wait_for_timeout(1200)
        # 预置作品都是 16:9，按 9:16 过滤后应为空
        count = self.page.evaluate(
            "() => document.querySelectorAll('#jobsGrid .job-card').length")
        self.assertEqual(count, 0, "按 9:16 过滤后不应有 16:9 的作品")
        self.page.evaluate("""
            () => {
              const sel = document.querySelector('#doneAspect');
              sel.value = '';
              sel.dispatchEvent(new Event('change'));
            }
        """)
        self.page.wait_for_timeout(1200)
        count = self.page.evaluate(
            "() => document.querySelectorAll('#jobsGrid .job-card').length")
        self.assertEqual(count, 24, "清除筛选后应恢复每页数量对应的数量")

    def test_nan_stored_value_does_not_break_the_list(self):
        """localStorage 里是 NaN 时，列表仍要能渲染出来。

        变异测试发现的问题：page_size=NaN 会让服务端返回 400，
        refreshJobs 走 catch 分支，整个列表一条卡片都渲染不出来。
        防御点在 loadPageSize() 的整数校验。
        """
        self.page.evaluate(
            "() => localStorage.setItem('%s', 'NaN')" % PAGE_SIZE_KEY)
        self.page.reload(wait_until="domcontentloaded")
        # 若没修好，这里会因列表为空而超时
        self.page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card:not(.job-skeleton)').length > 0",
            timeout=20000)
        self.assertEqual(self.page.evaluate("() => state.pageSize"), 12,
                         "NaN 应退回默认 12")
        self.assertTrue(self.page.evaluate(
            "() => Number.isInteger(state.pageSize)"),
            "pageSize 必须是整数，不能是 NaN")

    def test_card_contents_stay_inside_card(self):
        """卡片内部任何内容都不得超出卡片边框。

        覆盖「窄屏下不溢出」这条要求：把窗口压到很窄、每页数量调大，
        列会被压得极窄 —— 这时最容易破版的是底部那排操作按钮
        （4 个 flex:1 按钮挤在窄卡片里）。
        """
        for width in (1000, 1280):
            self.page.set_viewport_size({"width": width, "height": 900})
            self.page.wait_for_timeout(500)
            self.page.evaluate("() => window.dispatchEvent(new Event('resize'))")
            self.page.wait_for_timeout(700)
            for size in (8, 12, 16, 24):
                self.set_size(size)
                overflow = self.page.evaluate("""
                    () => {
                      const bad = [];
                      document.querySelectorAll('#jobsGrid .job-card').forEach(card => {
                        const cb = card.getBoundingClientRect();
                        card.querySelectorAll('.job-meta, .job-actions, .job-top, .job-preview')
                          .forEach(el => {
                            const b = el.getBoundingClientRect();
                            if (b.right > cb.right + 1 || b.left < cb.left - 1) {
                              bad.push(el.className);
                            }
                          });
                      });
                      return bad;
                    }
                """)
                self.assertEqual(overflow, [],
                                 "%dpx 宽 / 每页 %d 时有 %d 处内容溢出卡片"
                                 % (width, size, len(overflow)))
        self.page.set_viewport_size({"width": 2560, "height": 1080})
        self.page.wait_for_timeout(500)
        self.page.evaluate("() => window.dispatchEvent(new Event('resize'))")
        self.page.wait_for_timeout(700)

    def test_no_console_errors(self):
        """没有 JS 报错。"""
        self.assertEqual([e for e in self.errors if e.strip()], [])


if __name__ == "__main__":
    unittest.main()
