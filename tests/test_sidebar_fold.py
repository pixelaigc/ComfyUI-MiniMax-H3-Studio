# -*- coding: utf-8 -*-
"""左侧栏「类别折叠 + 独立滚动」的回归测试（真实浏览器 + 真实后端）。

覆盖需求（2026-11-13 用户清单）：
  1) 最左侧每个类别标签可点击展开/收纳其子类别，折叠状态持久化到
     localStorage，刷新后保持；
  2) 最左侧列必须可上下滚动，小分辨率浏览器（375/390px 高）也能看到
     最下面几项，不能被 overflow:hidden 截断；
  3) 折叠图标/交互风格与现有 UI 一致，不破坏现有选中态与路由。

【为什么既有测试不够】
  tests/test_responsive_mobile.py 测的是通用响应式（横向溢出 / 触控目标 /
  抽屉契约），没有「折叠」这个维度；
  tests/verify_video_tabs_browser.py 测的是视频两级导航的内容与高亮，
  不测折叠状态。单独一个测试文件，断言贴需求。

运行：python3 -m pytest tests/test_sidebar_fold.py -v
需要：playwright（chromium）。缺失时整类 skip，不算失败。
"""
import sys
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

FOLD_KEY = "h3studio.sidebarFold"

#: 顶层分组的稳定 key（与 web/index.html 的 data-nav-key 一致）
GROUP_KEYS = ["video", "image", "image-edit", "music", "assets", "agent", "system"]

#: 视频类别 key（服务端 /api/video-categories 下发）
VIDEO_CATS = ["t2v", "flf", "r2v", "longfilm"]


@unittest.skipUnless(HAS_PLAYWRIGHT, "未安装 playwright")
class TestSidebarFold(unittest.TestCase):
    """真实浏览器 + 真实 Studio：锁死左侧栏的折叠与滚动契约。"""

    @classmethod
    def setUpClass(cls):
        cls.server = StudioServer(seed_jobs=25)
        assert cls.server.start(), "Studio 实例启动失败"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.stop()

    def setUp(self):
        self.ctx = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: self.errors.append(m.text)
                    if m.type == "error" else None)
        self.page.goto("http://127.0.0.1:%d/" % self.server.port,
                       wait_until="domcontentloaded")
        self.page.wait_for_function(
            "() => document.querySelectorAll('#navVideoCategories .nav-cat').length > 0",
            timeout=WAIT_TIMEOUT)
        self.page.wait_for_timeout(800)
        # 每个用例从「全部展开」的干净态开始
        self.page.evaluate("k => localStorage.removeItem(k)", FOLD_KEY)
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_function(
            "() => document.querySelectorAll('#navVideoCategories .nav-cat').length > 0",
            timeout=WAIT_TIMEOUT)
        self.page.wait_for_timeout(800)

    def tearDown(self):
        self.ctx.close()

    # ------------------------------------------------------- 需求 1：折叠
    def test_all_seven_groups_are_collapsible(self):
        """7 个顶层分组标题都是可点击的折叠按钮。"""
        keys = self.page.eval_on_selector_all(
            ".nav-group[data-nav-key]", "els => els.map(e => e.dataset.navKey)")
        self.assertEqual(keys, GROUP_KEYS,
                         "顶层分组 key 集合不对：%r" % keys)
        for key in GROUP_KEYS:
            expanded = self.page.get_attribute(
                '.nav-group[data-nav-key="%s"]' % key, "aria-expanded")
            self.assertEqual(expanded, "true",
                             "分组 %s 初始应展开" % key)

    def test_group_fold_hides_children(self):
        """点分组标题 -> 该分组的入口项被收起。"""
        self.page.click('.nav-group[data-nav-key="music"]')
        self.page.wait_for_timeout(400)
        st = self.page.evaluate("""() => ({
          expanded: document.querySelector('.nav-group[data-nav-key="music"]')
                     .getAttribute('aria-expanded'),
          t2a: document.querySelector('.nav-item[data-mode="t2a"]').offsetParent !== null,
        })""")
        self.assertEqual(st["expanded"], "false")
        self.assertFalse(st["t2a"], "「音乐」分组折叠后其入口项应隐藏")

    def test_group_fold_persists_across_reload(self):
        """折叠状态持久化到 localStorage，刷新后保持。"""
        self.page.click('.nav-group[data-nav-key="image"]')
        self.page.wait_for_timeout(400)
        stored = self.page.evaluate("k => localStorage.getItem(k)", FOLD_KEY)
        self.assertIsNotNone(stored, "折叠状态未写入 localStorage")
        self.assertIn("image", stored)

        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_function(
            "() => document.querySelectorAll('#navVideoCategories .nav-cat').length > 0",
            timeout=WAIT_TIMEOUT)
        self.page.wait_for_timeout(800)
        st = self.page.evaluate("""() => ({
          expanded: document.querySelector('.nav-group[data-nav-key="image"]')
                     .getAttribute('aria-expanded'),
          t2i: document.querySelector('.nav-item[data-mode="t2i"]').offsetParent !== null,
        })""")
        self.assertEqual(st["expanded"], "false", "刷新后应仍是折叠态")
        self.assertFalse(st["t2i"], "刷新后入口项应仍隐藏")

    def test_video_category_fold_toggles_models(self):
        """视频类别有独立折叠开关：点它收起/展开该类别的模型列表。"""
        for cat in VIDEO_CATS:
            self.assertTrue(
                self.page.query_selector('.nav-cat-toggle[data-category="%s"]' % cat),
                "类别 %s 缺折叠开关" % cat)
        self.page.click('#navVideoCategories .nav-cat-toggle[data-category="t2v"]')
        self.page.wait_for_timeout(400)
        st = self.page.evaluate("""() => {
          const cat = document.querySelector('.nav-cat[data-category="t2v"]');
          return {
            expanded: cat.querySelector('.nav-cat-toggle').getAttribute('aria-expanded'),
            head: cat.querySelector('.nav-cat-head').getAttribute('aria-expanded'),
            models: cat.querySelector('.nav-cat-models').offsetParent !== null,
            others: [...document.querySelectorAll('.nav-cat')].filter(c =>
              c.dataset.category !== 't2v')
              .every(c => c.querySelector('.nav-cat-models').offsetParent !== null),
          };
        }""")
        self.assertFalse(st["models"], "折叠后模型列表应隐藏")
        self.assertEqual(st["expanded"], "false")
        self.assertEqual(st["head"], "false", "head 的 aria-expanded 应同步")
        self.assertTrue(st["others"], "其它类别不应被连带折叠")

    def test_video_category_fold_persists_across_reload(self):
        """视频类别的折叠状态也持久化。"""
        self.page.click('#navVideoCategories .nav-cat-toggle[data-category="flf"]')
        self.page.wait_for_timeout(400)
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_function(
            "() => document.querySelectorAll('#navVideoCategories .nav-cat').length > 0",
            timeout=WAIT_TIMEOUT)
        self.page.wait_for_timeout(800)
        visible = self.page.evaluate("""() => {
          const cat = document.querySelector('.nav-cat[data-category="flf"]');
          return cat.querySelector('.nav-cat-models').offsetParent !== null;
        }""")
        self.assertFalse(visible, "刷新后该类别应仍折叠")

    # ------------------------------------------ 需求 3：不破坏选中态与路由
    def test_fold_does_not_break_mode_switch(self):
        """折叠状态下点入口项仍能切 mode，高亮也正确。"""
        self.page.click('.nav-group[data-nav-key="image"]')
        self.page.wait_for_timeout(400)
        # 折叠后入口不可见，用 JS 触发（模拟「复用」按钮的程序化点选）
        self.page.evaluate("""() => {
          document.querySelector('.nav-item[data-mode="t2i"]').click();
        }""")
        self.page.wait_for_timeout(900)
        st = self.page.evaluate("""() => ({
          mode: window.__h3State ? window.__h3State.mode : null,
          active: [...document.querySelectorAll('.nav-item.active')]
                    .map(e => e.textContent.trim()),
          expanded: document.querySelector('.nav-group[data-nav-key="image"]')
                     .getAttribute('aria-expanded'),
        })""")
        self.assertEqual(st["mode"], "t2i", "折叠不应影响模式切换")
        self.assertEqual(st["active"], ["Z-IMAGE 文生图"])
        self.assertEqual(st["expanded"], "true",
                         "点入口项应自动展开其所在分组（选中态要看得见）")

    def test_fold_does_not_break_video_two_level_nav(self):
        """视频两级导航的选中态与高亮不受折叠影响。"""
        self.page.click('.nav-cat-head[data-category="flf"]')
        self.page.wait_for_timeout(900)
        st = self.page.evaluate("""() => ({
          mode: window.__h3State ? window.__h3State.mode : null,
          cat: [...document.querySelectorAll('.nav-cat-head.active')]
                 .map(e => e.querySelector('.nav-cat-label').textContent.trim()),
          model: [...document.querySelectorAll('.nav-model-item.active')]
                  .map(e => e.dataset.mode),
        })""")
        self.assertEqual(st["mode"], "flf")
        self.assertEqual(st["cat"], ["首尾帧生视频"])
        self.assertEqual(st["model"], ["flf"])

    def test_video_nav_caret_markup_is_not_nested_in_button(self):
        """折叠开关必须是 .nav-cat-head 的**兄弟**，不能塞在 button 里面。

        <button> 套 <button> 是非法 HTML —— 浏览器会把内层按钮从可交互树
        里摘掉，点击事件落到外层 head 上，折叠永远点不动。
        """
        nested = self.page.evaluate("""() => {
          return [...document.querySelectorAll('.nav-cat-head button')]
            .map(e => e.className);
        }""")
        self.assertEqual(nested, [], "head 内部不允许再套 button")
        siblings = self.page.evaluate("""() => {
          return [...document.querySelectorAll('.nav-cat')]
            .filter(c => c.querySelector('.nav-cat-toggle')).length;
        }""")
        self.assertEqual(siblings, len(VIDEO_CATS),
                         "每个类别都应有一个平级的折叠开关")

    # ------------------------------------------------------- 需求 2：滚动
    def test_sidebar_has_dedicated_scroll_container(self):
        """最左侧列有独立滚动容器（不是靠 body 滚动）。"""
        info = self.page.evaluate("""() => {
          const el = document.getElementById('navScroll');
          if (!el) return {exists: false};
          const cs = getComputedStyle(el);
          return {
            exists: true,
            overflowY: cs.overflowY,
            scrollable: el.scrollHeight > el.clientHeight,
          };
        }""")
        self.assertTrue(info["exists"], "缺少 .nav-scroll 滚动容器")
        self.assertIn(info["overflowY"], ("auto", "scroll"),
                      "滚动容器 overflow-y 应为 auto/scroll，实际 %s" % info["overflowY"])

    def test_short_viewport_can_scroll_to_bottom_items(self):
        """375px 高视口：最左侧列能滚到底，看到最下面几项。"""
        self.page.set_viewport_size({"width": 375, "height": 375})
        self.page.wait_for_timeout(600)
        self.page.evaluate("() => { document.getElementById('navToggle').click(); }")
        self.page.wait_for_timeout(600)
        st = self.page.evaluate("""() => {
          const el = document.getElementById('navScroll');
          const before = {scrollH: el.scrollHeight, clientH: el.clientHeight};
          el.scrollTop = el.scrollHeight;
          const sys = document.querySelector('.nav-item#navSystem').getBoundingClientRect();
          const er = el.getBoundingClientRect();
          return {
            before: before,
            scrollTop: el.scrollTop,
            systemVisible: sys.top >= er.top - 1 && sys.bottom <= er.bottom + 1,
          };
        }""")
        self.assertGreater(st["before"]["scrollH"], st["before"]["clientH"],
                           "375px 高视口下内容应超出可视高度")
        self.assertGreater(st["scrollTop"], 0,
                           "滚动未生效 —— 可能被 overflow:hidden 截断")
        self.assertTrue(st["systemVisible"],
                        "滚到底后「硬件参数」应可见")

    def test_sidebar_footer_stays_visible_outside_scroll(self):
        """「释放显存」常驻底栏（在滚动区外），任何视口高度都够得着。"""
        self.page.set_viewport_size({"width": 375, "height": 375})
        self.page.wait_for_timeout(600)
        self.page.evaluate("() => { document.getElementById('navToggle').click(); }")
        self.page.wait_for_timeout(600)
        st = self.page.evaluate("""() => {
          const foot = document.querySelector('.sidebar-foot');
          const el = document.getElementById('navScroll');
          const bar = document.querySelector('#sidebar').getBoundingClientRect();
          const fr = foot.getBoundingClientRect();
          return {
            outside: !el.contains(foot),
            visible: fr.top >= bar.top - 1 && fr.bottom <= bar.bottom + 1,
          };
        }""")
        self.assertTrue(st["outside"], "释放显存按钮应在滚动区外（常驻）")
        self.assertTrue(st["visible"], "释放显存按钮应始终可见")

    def test_no_console_errors(self):
        """console 0 错误。"""
        real = [e for e in self.errors
                if "favicon" not in e.lower() and "Failed to load resource" not in e]
        self.assertEqual(real, [], "console 有报错：%r" % real[:3])


if __name__ == "__main__":
    unittest.main()
