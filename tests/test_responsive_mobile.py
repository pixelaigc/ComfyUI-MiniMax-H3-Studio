# -*- coding: utf-8 -*-
"""手机端响应式适配的回归测试（真实浏览器 + 真实后端 + 真实素材库）。

覆盖任务书要求的三类问题，每条都用**实测几何**判定，不靠肉眼：
  1. 横向滚动：document.scrollWidth 不得大于视口宽度
  2. 元素溢出：任何可见元素的右边界不得超出视口
  3. 触控目标：可点元素的渲染高度不得小于 32px

同时锁死三条响应式契约：
  · 窄屏侧栏收成抽屉（默认关闭，汉堡按钮可见）
  · 窄屏参数面板收成底部抽屉（浮动按钮可见）
  · 网格列数随宽度变化，且卡片不溢出网格容器

断点真源在 web/responsive.css（--bp-phone / --bp-tablet），
JS 侧 app.js 的 BREAKPOINTS 与之同值 —— 本测试锁住「CSS 断点 = JS 断点」
这条一致性，避免两边各写一份数字而走偏。

运行：python3 -m pytest tests/test_responsive_mobile.py -v
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

from test_frontend_defaults import StudioServer  # noqa: E402


PHONE_WIDTHS = (375, 390, 414)
TABLET_WIDTH = 768
DESKTOP_WIDTH = 1280

# 采集：横向滚动 + 溢出元素 + 小触控目标 + 关键容器几何
FACT = """
() => {
  const vw = document.documentElement.clientWidth;
  const overflowing = [];
  document.querySelectorAll('body *').forEach(el => {
    if (el.closest('[hidden]')) return;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;
    // 关闭态的抽屉天然在视口外，不算溢出
    if (el.closest('.al-drawer:not(.open)')) return;
    if (el.classList.contains('al-drawer') && !el.classList.contains('open')) return;
    if (r.right > vw + 1) {
      overflowing.push({
        sel: (el.tagName + '.' + (el.className || '')).slice(0, 60),
        right: Math.round(r.right), over: Math.round(r.right - vw),
        text: (el.textContent || '').trim().slice(0, 20),
      });
    }
  });
  const small = [];
  document.querySelectorAll('button, a.nav-item, input, select, textarea').forEach(el => {
    if (el.closest('[hidden]')) return;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;
    if (r.height < 32) {
      small.push({sel: (el.tagName + '.' + (el.className || '')).slice(0, 50),
                  h: Math.round(r.height), w: Math.round(r.width),
                  text: (el.textContent || '').trim().slice(0, 14)});
    }
  });
  const grid = document.querySelector('#jobsGrid');
  const sidebar = document.querySelector('#sidebar');
  const panel = document.querySelector('#panel');
  const toggle = document.querySelector('#navToggle');
  const fab = document.querySelector('#panelFab');
  const vis = (el) => !!el && getComputedStyle(el).display !== 'none' && !el.hidden;
  return {
    vw: vw,
    docScrollW: document.documentElement.scrollWidth,
    hasHScroll: document.documentElement.scrollWidth > vw + 1,
    overflowing: overflowing,
    small: small,
    gridCols: grid ? Number(grid.dataset.cols || 0) : 0,
    gridWidth: grid ? Math.round(grid.getBoundingClientRect().width) : 0,
    sidebarX: sidebar ? Math.round(sidebar.getBoundingClientRect().x) : null,
    sidebarVisible: vis(sidebar),
    toggleVisible: vis(toggle),
    panelOpen: panel ? panel.classList.contains('open') : false,
    fabVisible: vis(fab),
  };
}
"""


@unittest.skipUnless(HAS_PLAYWRIGHT, "未安装 playwright")
class TestResponsiveMobile(unittest.TestCase):
    """真实浏览器 + 真实 Studio：锁死窄屏下的布局契约。"""

    @classmethod
    def setUpClass(cls):
        cls.server = StudioServer(seed_jobs=12)
        assert cls.server.start(), "Studio 实例启动失败"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.stop()

    def setUp(self):
        self.ctx = self.browser.new_context(
            viewport={"width": 390, "height": 900},
            is_mobile=True, has_touch=True)
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.goto("http://127.0.0.1:%d/" % self.server.port,
                      wait_until="domcontentloaded")
        self.page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card').length > 0",
            timeout=20000)
        self.page.wait_for_timeout(1200)

    def tearDown(self):
        self.ctx.close()

    def resize(self, width):
        self.page.set_viewport_size({"width": width, "height": 900})
        self.page.wait_for_timeout(700)
        self.page.evaluate("() => window.dispatchEvent(new Event('resize'))")
        self.page.wait_for_timeout(700)

    # ---------------------------------------------- 1. 无横向滚动
    def test_no_horizontal_scroll_at_any_width(self):
        """任何宽度下都不该出现横向滚动条。"""
        for width in (375, 390, 414, 768, 1280):
            self.resize(width)
            fact = self.page.evaluate(FACT)
            self.assertFalse(
                fact["hasHScroll"],
                "%dpx 下出现横向滚动：scrollWidth=%d > clientWidth=%d"
                % (width, fact["docScrollW"], fact["vw"]))

    # ---------------------------------------------- 2. 无元素溢出
    def test_no_element_overflows_viewport_on_phone(self):
        """手机宽度下，任何可见元素都不得超出视口右边界。"""
        for width in PHONE_WIDTHS:
            self.resize(width)
            fact = self.page.evaluate(FACT)
            self.assertEqual(
                fact["overflowing"], [],
                "%dpx 下有 %d 个元素溢出视口：%r"
                % (width, len(fact["overflowing"]), fact["overflowing"][:5]))

    # ---------------------------------------------- 3. 触控目标
    def test_touch_targets_are_large_enough_on_phone(self):
        """手机宽度下，可点元素的渲染高度不得小于 32px。"""
        for width in PHONE_WIDTHS:
            self.resize(width)
            fact = self.page.evaluate(FACT)
            self.assertEqual(
                fact["small"], [],
                "%dpx 下有 %d 个触控目标小于 32px：%r"
                % (width, len(fact["small"]), fact["small"][:5]))

    # ---------------------------------------------- 4. 抽屉契约
    def test_sidebar_is_drawer_on_phone(self):
        """手机宽度下侧栏默认收起（抽屉在视口外），汉堡按钮可见。"""
        self.resize(390)
        fact = self.page.evaluate(FACT)
        self.assertTrue(fact["toggleVisible"], "手机端应显示汉堡按钮")
        self.assertLess(
            fact["sidebarX"], 0,
            "手机端侧栏应收在视口外（x=%s），实际是 %s"
            % (fact["sidebarX"], fact["sidebarX"]))
        # 点汉堡 → 侧栏滑入
        self.page.click("#navToggle")
        self.page.wait_for_timeout(500)
        opened = self.page.evaluate(FACT)
        self.assertGreaterEqual(
            opened["sidebarX"], 0,
            "点汉堡后侧栏应滑入视口（x=%s）" % opened["sidebarX"])

    def test_panel_is_bottom_drawer_on_phone(self):
        """手机宽度下参数面板收成底部抽屉，浮动按钮可见。"""
        self.resize(390)
        fact = self.page.evaluate(FACT)
        self.assertTrue(fact["fabVisible"], "手机端应显示「参数」浮动按钮")
        self.assertFalse(fact["panelOpen"], "参数面板默认应是收起的")
        # 点浮动按钮 → 面板展开
        self.page.click("#panelFab")
        self.page.wait_for_timeout(500)
        opened = self.page.evaluate(FACT)
        self.assertTrue(opened["panelOpen"], "点浮动按钮后参数面板应展开")

    def test_sidebar_is_persistent_on_desktop(self):
        """桌面端（1280px）侧栏常驻，汉堡按钮与浮动按钮都隐藏。"""
        self.resize(DESKTOP_WIDTH)
        fact = self.page.evaluate(FACT)
        self.assertFalse(fact["toggleVisible"], "桌面端不应显示汉堡按钮")
        self.assertFalse(fact["fabVisible"], "桌面端不应显示「参数」浮动按钮")
        self.assertGreaterEqual(
            fact["sidebarX"], 0,
            "桌面端侧栏应常驻（x=%s）" % fact["sidebarX"])

    def test_sidebar_persistent_on_tablet(self):
        """平板（768px）侧栏常驻，参数面板走底部抽屉。"""
        self.resize(TABLET_WIDTH)
        fact = self.page.evaluate(FACT)
        self.assertFalse(fact["toggleVisible"], "平板端侧栏常驻，不应显示汉堡按钮")
        self.assertTrue(fact["fabVisible"], "平板端参数区已收进抽屉，应有浮动按钮")
        self.assertGreaterEqual(
            fact["sidebarX"], 0,
            "平板端侧栏应常驻（x=%s）" % fact["sidebarX"])

    # ---------------------------------------------- 5. 网格列数
    def test_grid_columns_adapt_to_width(self):
        """网格列数随**容器宽度**变化。

        注意：不是「视口越宽列数越多」这种单调关系 ——
        列数由「容器宽度 ÷ 卡片最小宽度」决定，而桌面端侧栏+参数面板
        占掉固定宽度。实测：390px → 内容区 366px → 2 列；
                1600px → 内容区 1080px → 3 列；
                1280px → 内容区 760px → 2 列（不是 3）。
        所以断言必须比较**内容区宽度**不同的两档，而不是随便挑一个宽视口。
        """
        self.resize(390)
        narrow = self.page.evaluate(FACT)
        self.resize(1600)
        wide = self.page.evaluate(FACT)
        self.assertGreater(
            wide["gridCols"], narrow["gridCols"],
            "内容区更宽时列数应更多：390px(内容区 %dpx)=%s 列，"
            "1600px(内容区 %dpx)=%s 列"
            % (narrow["gridWidth"], narrow["gridCols"],
               wide["gridWidth"], wide["gridCols"]))

    def test_cards_do_not_overflow_grid_container(self):
        """卡片不得溢出网格容器（窄列下不破版）。"""
        for width in PHONE_WIDTHS + (TABLET_WIDTH, DESKTOP_WIDTH):
            self.resize(width)
            overflow = self.page.evaluate("""
              () => {
                const grid = document.querySelector('#jobsGrid');
                const gr = grid.getBoundingClientRect();
                const bad = [];
                grid.querySelectorAll('.job-card').forEach(card => {
                  const cr = card.getBoundingClientRect();
                  if (cr.right > gr.right + 1 || cr.left < gr.left - 1) {
                    bad.push(Math.round(cr.right - gr.right));
                  }
                });
                return bad;
              }
            """)
            self.assertEqual(overflow, [],
                             "%dpx 下有 %d 张卡片溢出网格容器" % (width, len(overflow)))

    # ---------------------------------------------- 6. 断点一致性
    def test_css_and_js_breakpoints_agree(self):
        """CSS 的 --bp-phone 必须等于 JS 的 BREAKPOINTS.phone。

        两边各写一份断点必然走偏：CSS 收起了侧栏、JS 还按桌面算，
        结果是手机上点汉堡没反应，或者桌面端侧栏被藏起来。
        """
        css_phone = self.page.evaluate("""
          () => {
            const v = getComputedStyle(document.documentElement)
              .getPropertyValue('--bp-phone').trim();
            return parseFloat(v);
          }
        """)
        js_phone = self.page.evaluate(
            "() => (window.BREAKPOINTS && window.BREAKPOINTS.phone) || null")
        self.assertIsNotNone(js_phone, "app.js 未暴露 BREAKPOINTS")
        self.assertEqual(
            float(css_phone), float(js_phone),
            "CSS --bp-phone(%s) 与 JS BREAKPOINTS.phone(%s) 不一致"
            % (css_phone, js_phone))

    def test_responsive_stylesheet_is_loaded(self):
        """响应式样式表必须真的被加载（否则窄屏规则全部失效）。"""
        loaded = self.page.evaluate("""
          () => Array.from(document.styleSheets).some(function (sheet) {
            try {
              return Array.from(sheet.cssRules || []).some(function (rule) {
                return rule.cssText && rule.cssText.indexOf('--bp-phone') !== -1;
              });
            } catch (e) { return false; }
          })
        """)
        self.assertTrue(loaded, "responsive.css 未被加载或不含 --bp-phone 变量")

    # ---------------------------------------------- 7. 不回归
    def test_no_console_errors(self):
        """没有 JS 报错。"""
        self.assertEqual([e for e in self.errors if e.strip()], [])


if __name__ == "__main__":
    unittest.main()
