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

from test_frontend_defaults import StudioServer, WAIT_TIMEOUT  # noqa: E402


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

    #: 预置任务数 —— 必须 > 每页作品数（12），否则分页条不渲染，
    #: 「FAB 遮挡分页」那条断言就成了永远绿的空转（pager.hidden 时直接返回 []）。
    SEED_JOBS = 40

    @classmethod
    def setUpClass(cls):
        cls.server = StudioServer(seed_jobs=cls.SEED_JOBS)
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
            "() => document.querySelectorAll('#jobsGrid .job-card:not(.job-skeleton)').length > 0",
            timeout=WAIT_TIMEOUT)
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

    def test_panel_fab_is_not_occluded_by_open_drawer(self):
        """抽屉打开后，「参数」浮动按钮不能被抽屉本体盖住（点不动）。

        【为什么要有这条 —— 它锁的是 2026-11-13 用户报的「MOSS-Image
        文生图面板完全不可用」的直接根因】

        缺陷现象：.panel-fab 的 z-index 是 930，而抽屉本体 .panel 是 950、
        遮罩 .panel-mask 是 949 —— FAB 比抽屉**低 20 层**。
        抽屉打开后 FAB 落在抽屉覆盖范围内，用户手指点 FAB 中心，
        命中的是**面板里的控件**而不是 FAB 自己：
          375px -> 命中 SMALL#imgCfgHint
          390px -> 命中 DIV.compact-field
        => 按钮点不动 -> 用户根本打不开参数面板 -> 表现为「面板完全不可用」。

        为什么既有测试没抓到：test_panel_is_bottom_drawer_on_phone 只断言
        「点 FAB 后 panelOpen == True」—— 那是**第一次**点（抽屉还关着，
        FAB 此刻没被遮挡），而缺陷发生在**第二次**想点的时候（抽屉已开）。
        这条测试用 elementFromPoint 做权威命中判定，锁住「抽屉开着时
        FAB 要么自己可点、要么干净地隐藏起来」。

        判定标准（两条都满足才算过）：
          1. FAB 隐藏（hidden 或 display:none）—— 抽屉打开时它不该再露脸；
          2. 或者 FAB 可见但 elementFromPoint 命中的是它自己 —— 仍可点。
        """
        for width in (375, 390):
            self.resize(width)
            self.page.evaluate("""() => {
              const p = document.querySelector('#panel');
              if (p) p.classList.remove('open');
            }""")
            self.page.wait_for_timeout(400)
            # 点开抽屉
            self.page.click("#panelFab")
            self.page.wait_for_timeout(700)

            hit = self.page.evaluate("""() => {
              const fab = document.getElementById('panelFab');
              if (!fab) return {exists: false};
              const cs = getComputedStyle(fab);
              const r = fab.getBoundingClientRect();
              const visible = !!(fab.offsetWidth || fab.offsetHeight)
                              && cs.display !== 'none' && !fab.hidden;
              let hitTag = null, hitId = null, hitIsFab = null;
              if (visible && r.width > 0 && r.height > 0) {
                const h = document.elementFromPoint(
                  r.left + r.width / 2, r.top + r.height / 2);
                hitTag = h ? h.tagName : null;
                hitId = h ? (h.id || '') : null;
                hitIsFab = !!h && (h === fab || fab.contains(h));
              }
              const panel = document.querySelector('#panel');
              return {
                exists: true,
                visible: visible,
                fabZIndex: cs.zIndex,
                panelZIndex: panel ? getComputedStyle(panel).zIndex : null,
                hitTag: hitTag,
                hitId: hitId,
                hitIsFab: hitIsFab,
              };
            }""")

            if hit["visible"]:
                # FAB 还在显示 —— 必须自己可点，否则用户点了没反应
                self.assertTrue(
                    hit["hitIsFab"],
                    "%dpx：抽屉打开后「参数」按钮被遮挡，点不动（"
                    "命中的是 %s#%s）。FAB z-index=%s，"
                    "抽屉 z-index=%s —— FAB 必须高于抽屉。"
                    % (width, hit["hitTag"], hit["hitId"],
                       hit["fabZIndex"], hit["panelZIndex"]))
            # 隐藏也算通过（抽屉打开时 FAB 干净地让位）
            self.assertTrue(
                hit["exists"],
                "%dpx：找不到 #panelFab" % width)
            # 关掉抽屉，避免影响后续用例
            self.page.evaluate("""() => {
              const p = document.querySelector('#panel');
              if (p) p.classList.remove('open');
            }""")
            self.page.wait_for_timeout(300)

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

    # ---------------------------------------------- 7. 深层溢出与遮挡
    # 以下三条来自独立窄屏审计的实测发现，首版测试没覆盖到 ——
    # 因为它们都不在「视口右边界」这个判据下：
    #   缺陷 A Provider 页：页面**容器自身**横向溢出（不是元素溢出视口）
    #   缺陷 B FAB 遮挡：FAB 是 fixed 定位，遮挡的是**页面内元素**
    #   缺陷 C 弹窗关闭按钮：宽度 12.9px < 32px，但不在初始页面 DOM 里

    def _open_nav(self, page):
        """窄屏下侧栏在抽屉里，先拉出来。"""
        self.page.click("#navToggle")
        self.page.wait_for_timeout(500)

    def test_provider_page_has_no_internal_horizontal_scroll(self):
        """缺陷 A：本地模型配置页的 <select> 把整行撑破视口。

        根因：.agent-field 是 flex item，min-width:auto 使它拒绝收缩到
        内容宽度以下；而 <select> 的最长 option 文本有 566px 宽
        （「Qwen-Image 2.1 官方 PE·图片编辑（修改X保持Y）」）。
        结果：375px 视口下该页 scrollWidth=597，出现 207px 横向滚动。

        判据落在**页面容器自身**的 scrollWidth vs clientWidth ——
        这与「元素溢出视口」是两条不同的判据（元素仍能落在视口内，
        但页面容器内部被撑出滚动条）。
        """
        for width in PHONE_WIDTHS:
            self.resize(width)
            self._open_nav(self.page)
            self.page.click("#navProviders")
            self.page.wait_for_timeout(1500)
            info = self.page.evaluate("""
              () => {
                const page = document.querySelector('#providersPage');
                const culprits = [];
                page.querySelectorAll('*').forEach(el => {
                  const r = el.getBoundingClientRect();
                  if (r.width > page.clientWidth + 1) {
                    culprits.push((el.tagName + '.' + (el.className || '')).slice(0, 46));
                  }
                });
                return {
                  clientW: page.clientWidth,
                  scrollW: page.scrollWidth,
                  hasHScroll: page.scrollWidth > page.clientWidth + 1,
                  culprits: culprits.slice(0, 5),
                };
              }
            """)
            self.assertFalse(
                info["hasHScroll"],
                "%dpx 下 Provider 页内部横向滚动：scrollWidth=%d > clientWidth=%d"
                "（撑破元素：%r）" % (width, info["scrollW"], info["clientW"],
                                     info["culprits"]))
            self.resize(390)
            self._open_nav(self.page)
            self.page.click('.nav-item[data-mode="t2i"]')
            self.page.wait_for_timeout(1200)

    def test_fab_does_not_cover_pagination(self):
        """缺陷 B：「参数」浮动按钮不得压住分页条。

        FAB 是 position:fixed（44px 高 + 16px 底边距 = 60px），
        而 .jobs-area 的 padding-bottom 原本只有 24px —— 分页条排到底时
        「下一页」按钮和「共 N 个作品」会被 FAB 盖住（375px 下实测
        盖住「下一页」右侧 6×30px、盖住计数文字 67×16px 全宽）。

        判据：FAB 的矩形与分页条内每个 button/span 的矩形**不得有交集**。
        """
        for width in PHONE_WIDTHS:
            self.resize(width)
            # 切到「全部」标签 —— 分页条在 done/all 下才渲染。
            # 必须点真实按钮：app.js 里的 `state` 是模块级 const，**不在 window 上**，
            # 写 `state.tab = 'all'` 在页面上下文里会静默失败（state 未定义 → 抛错
            # 被 evaluate 吞掉），标签根本没切，分页条位置也不对 ——
            # 那样这条断言就成了「永远绿」的哑测试。
            clicked = self.page.evaluate(
                "() => { const btn = document.querySelector(\""
                ".jobs-tabs button[data-st='all']\");"
                " if (!btn) return false; btn.click(); return true; }")
            self.assertTrue(clicked, "找不到「全部」状态标签按钮")
            self.page.wait_for_timeout(2500)
            # 滚到网格底部，让分页条落到 FAB 所在的高度
            self.page.evaluate("""
              () => {
                const area = document.querySelector('#jobsArea');
                area.scrollTop = area.scrollHeight;
              }
            """)
            self.page.wait_for_timeout(800)
            # 分页条必须真的渲染出来了 —— 没渲染就说明前置没满足
            # （任务数不足 / 还没到「全部」标签），此时直接失败，
            # 不能让判据变成「pager.hidden 就返回 []」的空转。
            self.assertFalse(
                self.page.evaluate(
                    "() => document.querySelector('#jobsPagination').hidden"),
                "分页条没有渲染 —— 无法验证 FAB 是否遮挡它"
                "（检查：任务数是否 > 每页作品数、是否已切到「全部」标签）")
            hits = self.page.evaluate("""
              () => {
                const fab = document.querySelector('#panelFab');
                const pager = document.querySelector('#jobsPagination');
                if (!fab || !pager || pager.hidden) return [];
                const fr = fab.getBoundingClientRect();
                const out = [];
                pager.querySelectorAll('button, span').forEach(el => {
                  const r = el.getBoundingClientRect();
                  if (r.width === 0 || r.height === 0) return;
                  const ovw = Math.min(fr.right, r.right) - Math.max(fr.left, r.left);
                  const ovh = Math.min(fr.bottom, r.bottom) - Math.max(fr.top, r.top);
                  if (ovw > 0 && ovh > 0) {
                    out.push({text: (el.textContent || '').trim().slice(0, 16),
                              overlapW: Math.round(ovw), overlapH: Math.round(ovh)});
                  }
                });
                return out;
              }
            """)
            self.assertEqual(
                hits, [],
                "%dpx 下「参数」浮动按钮压住了分页控件：%r" % (width, hits))

    def test_modal_close_button_is_touch_sized(self):
        """缺陷 C：弹窗右上角的「×」只有 12.9×26px，触屏点不准。

        首版测试只查了初始页面 DOM 里的元素，而弹窗默认 hidden ——
        所以这条漏掉了。现在逐个打开弹窗再量。
        """
        MODALS = [
            ("#promptModal", "#promptModalClose"),
            ("#enhanceModal", "#enhanceModalClose"),
            ("#promptVersionModal", "#promptVersionClose"),
            ("#loraInfoModal", "#loraInfoClose"),
            ("#enhanceGateModal", "#enhanceGateClose"),
        ]
        for width in PHONE_WIDTHS:
            self.resize(width)
            for modal, close_btn in MODALS:
                self.page.evaluate(
                    "(id) => { document.querySelector(id).hidden = false; }", modal)
                self.page.wait_for_timeout(250)
                size = self.page.evaluate(
                    "(sel) => { const el = document.querySelector(sel);"
                    " if (!el) return null;"
                    " const r = el.getBoundingClientRect();"
                    " return {w: Math.round(r.width), h: Math.round(r.height)}; }",
                    close_btn)
                self.page.evaluate(
                    "(id) => { document.querySelector(id).hidden = true; }", modal)
                self.assertIsNotNone(size, "%s 的关闭按钮不存在" % close_btn)
                self.assertGreaterEqual(
                    size["h"], 32,
                    "%dpx 下 %s 的关闭按钮只有 %dx%d —— 触屏点不准"
                    % (width, close_btn, size["w"], size["h"]))
                self.assertGreaterEqual(
                    size["w"], 32,
                    "%dpx 下 %s 的关闭按钮只有 %dx%d —— 触屏点不准"
                    % (width, close_btn, size["w"], size["h"]))

    def test_hardware_power_controls_are_touch_sized(self):
        """硬件页功耗上限的 select / 应用按钮 ≥ 32px。"""
        self.resize(390)
        self._open_nav(self.page)
        self.page.click("#navSystem")
        self.page.wait_for_timeout(2500)
        small = self.page.evaluate("""
          () => {
            const out = [];
            document.querySelectorAll('.sys-pl-select, .sys-pl-btn').forEach(el => {
              const r = el.getBoundingClientRect();
              if (r.width === 0 || r.height === 0) return;
              if (r.height < 32) out.push({
                sel: (el.tagName + '.' + (el.className || '')).slice(0, 40),
                h: Math.round(r.height), w: Math.round(r.width)});
            });
            return out;
          }
        """)
        self.assertEqual(small, [],
                         "硬件页功耗控件小于 32px：%r" % small)

    # ---------------------------------------------- 8. 不回归
    def test_no_console_errors(self):
        """没有 JS 报错。"""
        self.assertEqual([e for e in self.errors if e.strip()], [])


if __name__ == "__main__":
    unittest.main()
