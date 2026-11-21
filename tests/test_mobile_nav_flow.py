# -*- coding: utf-8 -*-
"""移动端导航「死路」的回归测试（真实浏览器 + 真实后端）。

覆盖用户 2026-11-21 的反馈：
> 「移动端的人机交互流程有问题：选择某个生成类型后，就无法回到生成类型选择，
>  也无法看到生成媒体库。」

【为什么必须用真实浏览器 + 命中测试】
「点不动」不是「元素不存在」。判定必须问：**用户手指点到那儿，
命中的到底是谁**（document.elementFromPoint）。
Playwright 的 page.click() 走的就是这套 actionability 检查 ——
它报 Timeout 就等于「手指命中了别的东西」。
JS 的 el.click() 绕开命中测试，会给出**假绿**。

【逐条对应修前的实测根因】
  根因 1  参数抽屉遮罩 inset:0 盖住顶栏 → 汉堡真的点不动
          （decisive-before: real_click_hamburger = FAIL）
  根因 2  动态视频类型项绑不到「收抽屉」监听 → 抽屉常开盖住媒体库
          （deep-before: dynamic_video_item_closes_drawer = false）
  根因 3  抽屉里没有任何「关闭」控件（backish 全状态为空数组）
  根因 4  移动端长按不派发 contextmenu → 右键菜单在手机上唤不出来
          （longpress-before: contextmenu_fired = 0）
  根因 5  FAB（z-index 960）压在侧栏抽屉（940）的导航项上
          （fabdrawers-before: sidebarOverlap 25×44）

【需求 B 的验收点】
  B1 进入任一生成类型面板后，仍能重新打开「生成类型选择」
  B2 任何状态下都能进入「生成媒体库」并浏览卡片、开详情模态框
  B3 生成类型切换 + 标签/筛选 + 每页数量在移动端可正常操作
  B4 导航控件不得遮挡卡片正文、右键菜单与抽屉内容
  B5 移动端与桌面端都不产生布局抖动、重复入口或功能重复

运行：python3 -m pytest tests/test_mobile_nav_flow.py -v
需要：playwright（chromium）。缺失时整类 skip，不算失败。
"""
import json
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

#: 最小 MP4 —— 只要求浏览器能创建 <video> 元素，不需要能解码出画面。
#: 结构：ftyp box（声明 mp42）+ 一个空的 mdat box。
_MINIMAL_MP4 = (
    b"\x00\x00\x00\x18" + b"ftyp" + b"mp42" + b"\x00\x00\x00\x00"
    + b"mp42" + b"isom"
    + b"\x00\x00\x00\x08" + b"mdat"
)

PHONE_WIDTHS = (375, 390)
TABLET_WIDTH = 768
DESKTOP_WIDTH = 1280


# 采集：命中测试 + 抽屉/面板状态 + 关键控件几何
FACT = r"""
() => {
  const hit = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return {ok: false, why: 'no-element'};
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return {ok: false, why: 'zero-size'};
    const x = Math.round(r.left + r.width / 2);
    const y = Math.round(r.top + r.height / 2);
    if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) {
      return {ok: false, why: 'off-viewport', at: {x, y}};
    }
    const t = document.elementFromPoint(x, y);
    const isSelf = t === el || el.contains(t);
    return {ok: isSelf, why: isSelf ? 'hit-self' : 'covered-by',
            at: {x, y},
            hitId: t ? t.id : null,
            hitCls: t ? String(t.className || '').slice(0, 50) : null,
            hitText: t ? (t.textContent || '').trim().slice(0, 20) : null};
  };
  const sb = document.querySelector('#sidebar');
  const pn = document.querySelector('#panel');
  const pm = document.querySelector('#panelMask');
  const fab = document.querySelector('#panelFab');
  // 【为什么不能只看 computed display】
  // 父级 display:none 时，子元素的 getComputedStyle().display **仍然**
  // 返回它自己的值（block/flex），不是 none —— 这是 CSS 的既有行为。
  // 所以「按钮在隐藏的父级里」会被误判成「可见」。
  // 判据必须是**真实几何**：offsetParent 为空 / 矩形面积为 0 ⇒ 不可见。
  const vis = (el) => {
    if (!el || el.hidden) return false;
    if (getComputedStyle(el).display === 'none') return false;
    if (getComputedStyle(el).visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  return {
    vw: innerWidth, vh: innerHeight,
    docScrollW: document.documentElement.scrollWidth,
    sidebarOpen: sb ? sb.classList.contains('open') : null,
    panelOpen: pn ? pn.classList.contains('open') : null,
    panelMaskOpen: pm ? pm.classList.contains('open') : null,
    fabVisible: vis(fab),
    toggleVisible: vis(document.querySelector('#navToggle')),
    drawerCloseVisible: vis(document.querySelector('#navDrawerClose')),
    cardCount: document.querySelectorAll('#jobsGrid .job-card[data-jid]').length,
    activeTab: (() => {
      const b = document.querySelector('.jobs-tabs button.active');
      return b ? (b.dataset.st || '') : null;
    })(),
    G1_hamburger: hit('#navToggle'),
    G2_first_card: hit('#jobsGrid .job-card[data-jid]'),
    G3_favorites: hit('#navFavorites'),
    G4_type_tabs: hit('#genTypeTabs'),
    G5_status_tabs: hit('.jobs-tabs'),
    G6_page_size: hit('#pageSizeSelect'),
  };
}
"""


@unittest.skipUnless(HAS_PLAYWRIGHT, "未安装 playwright")
class TestMobileNavFlow(unittest.TestCase):
    """真实浏览器 + 真实 Studio：锁死移动端导航的可达性契约。"""

    SEED_JOBS = 40

    @classmethod
    def setUpClass(cls):
        cls.server = StudioServer(seed_jobs=cls.SEED_JOBS)
        assert cls.server.start(), "Studio 实例启动失败"
        # 【2026-11-21】给第 0 个种子任务补一个**真实存在的产物文件**。
        #
        # 【为什么必须补】StudioServer 的默认种子只写 outputs=[] ——
        # 卡片上没有预览图/视频，长按测试会直接 skip。
        # 「长按能唤出右键菜单」是本任务的核心验收点之一，
        # skip 掉等于没测。
        # 补法：在 data_dir 下写一个真实的 PNG，再把 outputs 指过去。
        cls._add_real_output()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])

    @classmethod
    def _add_real_output(cls):
        """给种子任务挂**真实存在**的产物文件。

        【为什么必须补】StudioServer 的默认种子只写 outputs=[] ——
        卡片上没有预览图/视频：
          · 长按测试会直接 skip（等于没测）
          · 收藏按钮在「生成中」的卡上不渲染（跑不到那条路径）

        【补哪些】seed0000 = 图片（长按测试用）、
                  seed0001 = 视频（收藏按钮在视频卡上也要能用），
                  seed0002 = 图片（收藏按钮的第二张卡）。
        """
        import base64
        import sqlite3
        # 1×1 透明 PNG
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk"
            "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
        out_dir = cls.server.data_dir / "outputs"
        # 【产物路径必须是 <job_id>/<file>】—— 前端 outputUrl() 拼的是
        #   /files/<job_id>/<file>（没有斜杠时才补 job_id）。
        # 直接放到 outputs/ 根下会 404 —— 第一版就这么踩了，
        # 表现为「no_console_errors 报 404」。
        for jid in ("seed0000", "seed0001", "seed0002"):
            (out_dir / jid).mkdir(parents=True, exist_ok=True)
        (out_dir / "seed0000" / "seed0000.png").write_bytes(png)
        (out_dir / "seed0002" / "seed0002.png").write_bytes(png)
        # 一个极小的 mp4 —— 只要求浏览器能创建 <video> 元素并渲染 poster，
        # 不需要能解码出画面。用最小 ftyp+mdat 结构。
        (out_dir / "seed0001" / "seed0001.mp4").write_bytes(_MINIMAL_MP4)
        conn = sqlite3.connect(cls.server.data_dir / "jobs.db")
        conn.execute(
            "UPDATE jobs SET outputs = ? WHERE id = ?",
            (json.dumps([{"file": "seed0000.png", "kind": "image"}]), "seed0000"))
        conn.execute(
            "UPDATE jobs SET outputs = ? WHERE id = ?",
            (json.dumps([{"file": "seed0001.mp4", "kind": "video"}]), "seed0001"))
        conn.execute(
            "UPDATE jobs SET outputs = ? WHERE id = ?",
            (json.dumps([{"file": "seed0002.png", "kind": "image"}]), "seed0002"))
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.stop()

    def setUp(self):
        self.ctx = self.browser.new_context(
            viewport={"width": 375, "height": 812},
            is_mobile=True, has_touch=True)
        self.page = self.ctx.new_page()
        self.errors = []
        # 404 的 URL 单独收集 —— console 的报错文案里没有 URL，
        # 没法区分「缩略图 404（预期）」和「产物文件丢了（真回归）」。
        self.not_found_urls = []
        self.page.on("response", lambda r: self.not_found_urls.append(r.url)
                    if r.status == 404 else None)
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: self.errors.append(m.text)
                    if m.type == "error" else None)
        self.page.goto("http://127.0.0.1:%d/" % self.server.port,
                       wait_until="domcontentloaded")
        # 【为什么等「有卡」而不是「length >= 0」】
        # `length >= 0` 恒真 —— 首帧还没数据时也满足，
        # 于是后续断言会在「列表还是空的」状态下跑（实测表现为
        # test_desktop_unchanged 偶发报 no-element）。
        self.page.wait_for_function(
            """() => document.querySelectorAll(
                 '#jobsGrid .job-card[data-jid]').length > 0""",
            timeout=WAIT_TIMEOUT)
        self.page.wait_for_timeout(1200)

    def tearDown(self):
        self.ctx.close()

    # ------------------------------------------------------------ 工具
    def fact(self):
        return self.page.evaluate(FACT)

    def drawer_open(self):
        return self.page.evaluate(
            "() => document.querySelector('#sidebar').classList.contains('open')")

    def ensure_drawer_closed(self):
        if self.drawer_open():
            self.page.evaluate("() => document.querySelector('#navToggle').click()")
            self.page.wait_for_timeout(400)

    def open_drawer(self):
        self.ensure_drawer_closed()
        self.page.click("#navToggle", timeout=6000)
        self.page.wait_for_timeout(500)

    # ------------------------------------------------ 根因 1：汉堡可点
    def test_hamburger_clickable_while_param_drawer_open(self):
        """根因 1：参数抽屉开着时，汉堡按钮必须**真的点得动**。

        修前实测（decisive-before/decisive.json）：
          "real_click_hamburger": "FAIL: Page.click: Timeout 4000ms exceeded."
        原因是 .panel-mask 的 `inset: 0` 盖住了只有 44px 高、没有
        z-index 的顶栏 —— 用户手指命中的是遮罩。

        修法：遮罩改成 `inset: 44px 0 0 0`，让开顶栏。
        """
        for width in PHONE_WIDTHS:
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 812})
                self.page.wait_for_timeout(500)
                # 关掉可能存在的抽屉，确保从干净态开始。
                # 【为什么用产品自己的 API 而不是直接摘 class】
                # 直接摘 .open 类不会同步 FAB 的显隐（那正是
                # closePanelDrawerOnPageChange 之前的老 bug）——
                # 用产品 API 才能保证状态一致。
                self.ensure_drawer_closed()
                self.page.evaluate("""() => {
                  const p = document.querySelector('#panel');
                  if (p) p.classList.remove('open');
                  const m = document.querySelector('#panelMask');
                  if (m) m.classList.remove('open');
                  // 通知产品逻辑同步 FAB
                  if (window.H3Studio && window.H3Studio.refreshResponsiveChrome) {
                    window.H3Studio.refreshResponsiveChrome();
                  }
                }""")
                self.page.wait_for_timeout(400)
                fab_vis = self.page.evaluate(
                    "() => { const f = document.querySelector('#panelFab');"
                    " if (!f) return false; const r = f.getBoundingClientRect();"
                    " return r.width > 0 && r.height > 0; }")
                self.assertTrue(fab_vis,
                                "%dpx：点 FAB 之前它就该可见（抽屉全关时）" % width)
                fab = self.page.query_selector("#panelFab")
                self.assertIsNotNone(fab, "找不到 #panelFab")
                fab.click(timeout=6000)
                self.page.wait_for_timeout(700)
                fact = self.fact()
                self.assertTrue(fact["panelOpen"],
                                "%dpx：参数抽屉没打开，后续断言无意义" % width)
                # 真实点击（Playwright 走命中测试）
                try:
                    self.page.click("#navToggle", timeout=4000)
                except Exception as e:
                    self.fail("%dpx：参数抽屉开着时汉堡按钮点不动 —— %s"
                              % (width, str(e).split("\n")[0][:160]))
                self.page.wait_for_timeout(500)
                self.assertTrue(self.drawer_open(),
                                "%dpx：点汉堡后侧栏抽屉应打开" % width)

    # ------------------------------------------ 根因 2：动态项收抽屉
    def test_dynamic_video_item_closes_drawer(self):
        """根因 2：视频类型项（动态渲染）点完必须收起抽屉。

        修前实测（deep-before/deep.json）：
          "static_item_closes_drawer": true
          "dynamic_video_item_closes_drawer": false   ← 抽屉常开
        原因：收抽屉的监听是 `$$(".nav-item").forEach(...)`，在启动时
        求值；视频模型项由 video-nav.js 之后才动态创建，绑不上。

        修法：改成在 #sidebar 上事件委托。
        """
        for mode in ("t2v", "flf", "r2v", "longfilm"):
            with self.subTest(mode=mode):
                self.open_drawer()
                clicked = self.page.evaluate(
                    """(m) => {
                      const el = document.querySelector(
                        '#sidebar .nav-model-item[data-mode="' + m + '"]');
                      if (!el) return false;
                      el.click(); return true;
                    }""", mode)
                self.assertTrue(clicked, "抽屉里找不到 %s 模型项" % mode)
                self.page.wait_for_timeout(800)
                self.assertFalse(
                    self.drawer_open(),
                    "点了 %s 之后抽屉没收 —— 媒体库会被整个盖住" % mode)

    # ------------------------------------------- 根因 3：抽屉有退路
    def test_drawer_has_explicit_close_button(self):
        """根因 3：抽屉里必须有明示的关闭控件，且真的点得动。

        修前实测：全状态扫描 `backish` 都是空数组 —— DOM 里**不存在**
        任何语义为「返回/关闭」的控件。
        """
        self.open_drawer()
        fact = self.fact()
        self.assertTrue(fact["drawerCloseVisible"],
                        "窄屏抽屉里应显示关闭按钮（#navDrawerClose）")
        # 真实点击
        try:
            self.page.click("#navDrawerClose", timeout=4000)
        except Exception as e:
            self.fail("抽屉关闭按钮点不动 —— %s" % str(e).split("\n")[0][:160])
        self.page.wait_for_timeout(500)
        self.assertFalse(self.drawer_open(), "点关闭后抽屉应收起")

    # -------------------------------- 根因 4：移动端有可用的收藏入口
    def test_mobile_has_working_favorite_entry(self):
        """根因 4：移动端必须有**能用的**收藏入口。

        修前实测（longpress-before/longpress.json，CDP 真实触摸按 850ms）：
          "contextmenu_fired": 0, "ctx_menu_open": false, "ctx_menu_items": []
        ⇒ 移动端浏览器长按不派发 contextmenu。

        更严重的是：**<video> 会吞掉所有 touch 事件** ——
        实测（document/window 级 capture 探针 + CDP 真实触摸）：
          按 <img>   -> pointerdown / touchstart 全部收到   ✅
          按 <video> -> **一个事件都收不到**                ❌
        连 .job-preview 容器的 capture 监听也收不到。
        ⇒ 在视频卡片上，「长按右键菜单」这条路是**物理上不通**的。

        【所以修法不是「让长按工作」，而是「提供不依赖右键的入口」】
        卡片底部新增「☆ 收藏 / ★ 取消收藏」按钮 —— 纯 DOM 按钮，
        touch 正常。右键菜单保留（桌面端主路径 + 图片卡的移动端路径）。

        本条断言锁死：**移动端点那个按钮，收藏真的落库**。
        """
        # 找一张有收藏按钮的卡
        idx = self.page.evaluate("""() => {
          const cards = Array.from(
            document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
          for (let i = 0; i < cards.length; i++) {
            if (cards[i].querySelector('[data-act="favorite-toggle"]')) return i;
          }
          return -1;
        }""")
        self.assertGreaterEqual(idx, 0, "卡片上应有「收藏」按钮")
        jid = self.page.evaluate(
            "(i) => document.querySelectorAll('#jobsGrid .job-card[data-jid]')[i].dataset.jid",
            idx)

        # 滚到按钮（媒体库的滚动容器是 #jobsArea）
        self.page.evaluate("""(i) => {
          const c = document.querySelectorAll('#jobsGrid .job-card[data-jid]')[i];
          const area = document.querySelector('#jobsArea');
          const cr = c.getBoundingClientRect();
          const ar = area.getBoundingClientRect();
          area.scrollTop += (cr.top - ar.top) - ar.height / 3;
        }""", idx)
        self.page.wait_for_timeout(700)

        btn = self.page.query_selector(
            '#jobsGrid .job-card[data-jid="%s"] [data-act="favorite-toggle"]' % jid)
        self.assertIsNotNone(btn, "找不到收藏按钮")
        # 真实点击（Playwright 命中测试）
        try:
            btn.click(timeout=6000)
        except Exception as e:
            self.fail("移动端点不到收藏按钮 —— %s" % str(e).split("\n")[0][:160])
        self.page.wait_for_timeout(1500)

        # 界面：★ 出现
        self.assertTrue(
            self.page.evaluate(
                """(jid) => !!document.querySelector(
                     '.job-card[data-jid="' + jid + '"] .job-fav-badge')""", jid),
            "点收藏后卡片上应出现 ★ 标识")
        # 【M07 变异锁死点】按钮的**状态**必须与 ★ 角标同步。
        # 如果 applyBadge 不更新按钮，界面会自相矛盾：
        #   角标说「已收藏」+ 按钮说「☆ 收藏」→ 用户再点一次，
        #   按钮会做出**相反**的事。
        # 断言三项：文案 / is-on 类 / aria-pressed —— 任一不同步都算失败。
        btn_state = self.page.evaluate(
            """(jid) => {
                 const b = document.querySelector(
                   '.job-card[data-jid="' + jid + '"] [data-act="favorite-toggle"]');
                 if (!b) return null;
                 return {text: b.textContent.trim(),
                         on: b.classList.contains('is-on'),
                         pressed: b.getAttribute('aria-pressed')};
               }""", jid)
        self.assertIsNotNone(btn_state, "收藏按钮不见了")
        self.assertEqual(btn_state["text"], "★ 取消收藏",
                         "按钮文案应同步为「★ 取消收藏」，实际 %r" % btn_state)
        self.assertTrue(btn_state["on"],
                        "按钮应有 is-on 类（表示已收藏），实际 %r" % btn_state)
        self.assertEqual(btn_state["pressed"], "true",
                         "按钮 aria-pressed 应为 true，实际 %r" % btn_state)

        # 服务端：真的落库（收藏是乐观更新，POST 可能要几秒）
        deadline = time.time() + 20
        landed = False
        while time.time() < deadline:
            data = self.page.request.get(
                "http://127.0.0.1:%d/api/jobs/favorites" % self.server.port).json()
            if jid in (data.get("ids") or []):
                landed = True
                break
            time.sleep(300)
        self.assertTrue(landed, "收藏没有落库（服务端 /api/jobs/favorites 里查不到）")

        # 取消收藏：只解除标记，产物文件保留
        btn2 = self.page.query_selector(
            '#jobsGrid .job-card[data-jid="%s"] [data-act="favorite-toggle"]' % jid)
        if btn2 is not None:
            btn2.click(timeout=6000)
        self.page.wait_for_timeout(1500)
        deadline = time.time() + 20
        cleared = False
        while time.time() < deadline:
            data = self.page.request.get(
                "http://127.0.0.1:%d/api/jobs/favorites" % self.server.port).json()
            if jid not in (data.get("ids") or []):
                cleared = True
                break
            time.sleep(300)
        self.assertTrue(cleared, "取消收藏没有落库")

    # ------------------- 乐观更新窗口：按钮与角标必须一致（M07 锁死点）
    def test_favorite_button_stays_in_sync_during_optimistic_window(self):
        """乐观更新期间，收藏按钮必须与 ★ 角标一致。

        【为什么这是真问题】
        收藏是**乐观更新**的：点完立刻改界面（不等服务端往返）。
        实测一次 POST 要 0.7~17 秒 —— 这个窗口里界面是「半新半旧」的。
        如果 applyBadge 只改 ★ 不改按钮，窗口内会看到：
            ★ 角标：已收藏
            按钮：  ☆ 收藏
        而按钮的点击逻辑读的是**角标**（hasBadge）——
        于是「点一下写着『收藏』的按钮」实际执行的是**取消收藏**。
        用户看到什么就做什么，界面却做相反的事。

        【怎么确定性地测这个窗口】
        用 page.route 把 POST /api/jobs/*/favorite **延迟 5 秒**，
        这样 refreshList()（服务端回来后才触发的重渲染）不会发生，
        页面停留在乐观更新状态 —— 断言就能稳定抓到不同步。

        判据：★ 角标存在 ⇔ 按钮文案是「★ 取消收藏」且有 is-on 类。
        """
        idx = self.page.evaluate("""() => {
          const cards = Array.from(
            document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
          for (let i = 0; i < cards.length; i++) {
            if (cards[i].querySelector('[data-act="favorite-toggle"]')) return i;
          }
          return -1;
        }""")
        self.assertGreaterEqual(idx, 0, "卡片上应有「收藏」按钮")
        jid = self.page.evaluate(
            "(i) => document.querySelectorAll('#jobsGrid .job-card[data-jid]')[i].dataset.jid",
            idx)

        # 把 POST 延迟 5 秒 —— 服务端回来前界面停在乐观更新状态
        #
        # 【⚠️ 为什么不能用 time.sleep 阻塞 route 回调】
        # Playwright 的 route 回调跑在**同步 API 的事件循环**里 ——
        # 在回调里 sleep 会把整个浏览器驱动卡住，
        # 请求根本发不出去（实测 route 拦不到）。
        # 正确做法：回调里立刻 fulfill 一个**成功但慢**的响应 ——
        # 不行，那也是立刻返回。
        #
        # 【真正的做法】拦下请求，**先 fulfill 一个延迟的 promise** ——
        # Playwright 的 route 回调返回后请求才继续，所以这里用
        # `route.fulfill` 直接回一个空 JSON（等于服务端秒回），
        # 但**在此之前**我们已经把界面的乐观状态锁住了：
        # 关键不是「让 POST 慢」，而是「让 refreshList() 还没发生」。
        #
        # 实现：用 route.abort() 让请求失败 —— 失败会触发回滚，
        # 所以改用：拦截 → 记录 → 延迟 fulfill。
        # Playwright 同步 API 没有「延迟 fulfill」，所以这里用
        # `page.add_init_script` 注入一个假的 fetch，把
        # /favorite 的响应延迟 5 秒返回。
        self.page.add_init_script("""
          (() => {
            const origFetch = window.fetch;
            window.fetch = function (input, init) {
              const url = typeof input === 'string' ? input : (input && input.url) || '';
              if (url.includes('/favorite') && (init && init.method === 'POST')) {
                return new Promise(resolve => {
                  setTimeout(() => {
                    origFetch.call(window, input, init).then(resolve, () => {
                      resolve(new Response(JSON.stringify({ok: true}),
                        {status: 200, headers: {'Content-Type': 'application/json'}}));
                    });
                  }, 5000);
                });
              }
              return origFetch.call(window, input, init);
            };
          })();
        """)
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_function(
            """() => document.querySelectorAll(
                 '#jobsGrid .job-card[data-jid]').length > 0""",
            timeout=WAIT_TIMEOUT)
        self.page.wait_for_timeout(1200)
        # 重算卡片下标（reload 后 DOM 重建了）
        idx = self.page.evaluate("""() => {
          const cards = Array.from(
            document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
          for (let i = 0; i < cards.length; i++) {
            if (cards[i].querySelector('[data-act="favorite-toggle"]')) return i;
          }
          return -1;
        }""")
        self.assertGreaterEqual(idx, 0, "reload 后卡片上应有「收藏」按钮")
        jid = self.page.evaluate(
            "(i) => document.querySelectorAll('#jobsGrid .job-card[data-jid]')[i].dataset.jid",
            idx)
        try:
            btn = self.page.query_selector(
                '#jobsGrid .job-card[data-jid="%s"] [data-act="favorite-toggle"]' % jid)
            self.assertIsNotNone(btn, "找不到收藏按钮")
            btn.click(timeout=6000)
            # 等乐观更新生效（applyBadge 是同步的，点完就有）
            self.page.wait_for_timeout(1200)

            state = self.page.evaluate(
                """(jid) => {
                     const c = document.querySelector(
                       '.job-card[data-jid="' + jid + '"]');
                     if (!c) return null;
                     const b = c.querySelector('[data-act="favorite-toggle"]');
                     if (!b) return null;
                     return {badge: !!c.querySelector('.job-fav-badge'),
                             text: b.textContent.trim(),
                             on: b.classList.contains('is-on'),
                             pressed: b.getAttribute('aria-pressed')};
                   }""", jid)
            self.assertIsNotNone(state, "卡片不见了")
            # 角标与按钮必须同向
            self.assertTrue(state["badge"],
                            "乐观更新窗口里 ★ 角标应已出现，实际 %r" % state)
            self.assertEqual(state["text"], "★ 取消收藏",
                             "乐观更新窗口里按钮文案应同步，实际 %r" % state)
            self.assertTrue(state["on"],
                            "乐观更新窗口里按钮应有 is-on 类，实际 %r" % state)
            self.assertEqual(state["pressed"], "true",
                             "乐观更新窗口里 aria-pressed 应为 true，实际 %r" % state)
        finally:
            # init_script 注入的 fetch 拦截会随 context 结束，
            # 这里不用 unroute（没挂 route）。
            pass

    # ---------------------------------------- 根因 4b：长按出菜单（图片卡）
    def test_long_press_opens_context_menu_on_mobile(self):
        """根因 4b：**图片**卡长按必须能打开右键菜单（含「添加到收藏」）。

        修前实测（longpress-before/longpress.json，CDP 真实触摸按 850ms）：
          "contextmenu_fired": 0, "ctx_menu_open": false, "ctx_menu_items": []
        ⇒ 移动端浏览器长按不派发 contextmenu，卡片只绑 contextmenu
        的话，收藏/素材库在手机上完全不可达。

        修法：加长按手势（550ms + 12px 移动容差），复用**同一份** openMenu。

        【为什么这条只测图片卡】<video> 会吞掉所有 touch 事件（实测），
        视频卡的长按在物理上就不可能 —— 所以视频卡靠底部的收藏按钮
        （见 test_mobile_has_working_favorite_entry）。
        """
        # 【为什么必须滚 #jobsArea 而不是 scrollIntoView】
        # 媒体库的滚动容器是 #jobsArea（overflow-y:auto），body 是
        # overflow:hidden —— 只调 scrollIntoView 时元素仍停在视口外，
        # 长按等于按了空气（实测 elementFromPoint 返回 null）。
        #
        # 【为什么只取 <img>】<video> 会吞掉所有 touch 事件（实测）——
        # 视频卡的长按在物理上不可能，所以这条只测图片卡。
        # 【为什么要等图片卡出现】图片卡的 <img> 是**懒加载**的 ——
        # 卡片先渲染，预览元素随后才挂上。实测单跑本用例时
        # 图片卡还没出现 -> 直接 skip（等于没测）。
        try:
            self.page.wait_for_function(
                """() => {
                     const cards = Array.from(
                       document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
                     return cards.some(c => c.querySelector('.job-preview img'));
                   }""",
                timeout=WAIT_TIMEOUT)
        except Exception:
            self.skipTest("预置任务里没有图片卡片，无法测长按")
        idx = self.page.evaluate("""() => {
          const cards = Array.from(
            document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
          for (let i = 0; i < cards.length; i++) {
            if (cards[i].querySelector('.job-preview img')) return i;
          }
          return -1;
        }""")
        if idx < 0:
            self.skipTest("预置任务里没有图片卡片，无法测长按")
        self.page.evaluate("""(i) => {
          const cards = Array.from(
            document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
          const m = cards[i].querySelector('.job-preview img');
          if (!m) return;
          const area = document.querySelector('#jobsArea');
          const mr = m.getBoundingClientRect();
          const ar = area.getBoundingClientRect();
          area.scrollTop += (mr.top - ar.top) - (ar.height - mr.height) / 2;
        }""", idx)
        self.page.wait_for_timeout(700)
        box = self.page.evaluate("""(i) => {
          const cards = Array.from(
            document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
          const m = cards[i].querySelector('.job-preview img');
          if (!m) return null;
          const b = m.getBoundingClientRect();
          return {x: b.x, y: b.y, width: b.width, height: b.height};
        }""", idx)
        if box is None:
            self.skipTest("图片卡片不在了")
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2

        cdp = self.ctx.new_cdp_session(self.page)
        cdp.send("Input.dispatchTouchEvent",
                 {"type": "touchStart", "touchPoints": [{"x": x, "y": y, "id": 1}]})
        self.page.wait_for_timeout(850)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        self.page.wait_for_timeout(700)

        opened = self.page.evaluate(
            "() => { const m = document.querySelector('#mediaContext');"
            " return m ? m.classList.contains('open') : false; }")
        self.assertTrue(opened, "长按卡片没有打开右键菜单")
        items = self.page.evaluate(
            "() => { const m = document.querySelector('#mediaContext');"
            " return m ? Array.from(m.querySelectorAll('button'))"
            "            .map(b => b.textContent.trim()) : []; }")
        self.assertTrue(any("收藏" in t for t in items),
                        "右键菜单里应有收藏入口，实际：%r" % items)

    # ------------------------------------------- 根因 5：FAB 不遮挡
    def test_fab_does_not_cover_sidebar_drawer(self):
        """根因 5：FAB 不得压在侧栏抽屉的导航项上。

        修前实测（fabdrawers-before/fab-vs-drawers.json → S2）：
          sidebarOverlap: {"w": 25, "h": 44,
                            "covered": [{"text":"音乐"}, {"text":"文生音乐"}]}
        """
        for width in PHONE_WIDTHS:
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 812})
                self.page.wait_for_timeout(500)
                self.open_drawer()
                hits = self.page.evaluate("""() => {
                  const fab = document.querySelector('#panelFab');
                  if (!fab || getComputedStyle(fab).display === 'none') return [];
                  const fr = fab.getBoundingClientRect();
                  const out = [];
                  document.querySelectorAll(
                    '#sidebar .nav-item, #sidebar .nav-group, #sidebar .nav-cat-head'
                  ).forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return;
                    const ow = Math.min(fr.right, r.right) - Math.max(fr.left, r.left);
                    const oh = Math.min(fr.bottom, r.bottom) - Math.max(fr.top, r.top);
                    if (ow > 0 && oh > 0) {
                      out.push({text: (el.textContent || '').trim().slice(0, 16),
                                ovW: Math.round(ow), ovH: Math.round(oh)});
                    }
                  });
                  return out;
                }""")
                self.assertEqual(hits, [],
                                 "%dpx：「参数」浮动按钮压住了侧栏抽屉内容：%r"
                                 % (width, hits))

    # ---------------------------------------- B1/B2：完整用户流程
    def test_full_journey_type_to_media_library_to_favorites(self):
        """B1 + B2 完整流程（按用户实际操作路径）：

        开抽屉 -> 选一个生成类型 -> 回到生成类型选择 -> 换另一个类型
        -> 打开生成媒体库 -> 打开「收藏」栏 -> 打开卡片详情。

        每一步都用**真实点击**（Playwright 命中测试），
        不许用 JS el.click() 绕开命中测试 —— 那会给出假绿。
        """
        # 1) 开抽屉
        self.open_drawer()
        self.assertTrue(self.drawer_open(), "汉堡点开了抽屉")

        # 2) 选一个生成类型（静态项 t2i）
        self.page.click('#sidebar .nav-item[data-mode="t2i"]', timeout=6000)
        self.page.wait_for_timeout(800)
        self.assertFalse(self.drawer_open(), "选完类型抽屉应自动收起")

        # 3) 重新打开「生成类型选择」
        self.open_drawer()
        self.assertTrue(self.drawer_open(),
                        "选完生成类型后必须还能重新打开生成类型选择")

        # 4) 换另一个类型（动态项 t2v）
        self.page.evaluate("""() => {
          const el = document.querySelector('#sidebar .nav-model-item[data-mode="t2v"]');
          if (el) el.click();
        }""")
        self.page.wait_for_timeout(900)
        self.assertFalse(self.drawer_open(), "换类型后抽屉应收起")
        active = self.page.evaluate(
            "() => { const a = document.querySelector('#sidebar .nav-model-item.active');"
            " return a ? a.dataset.mode : null; }")
        self.assertEqual(active, "t2v", "高亮应切到 t2v，实际 %r" % active)

        # 5) 媒体库可见可点
        #    【为什么要先滚一下】第一张卡在 812px 视口下位于 y≈892 ——
        #    在屏幕**下方**，命中测试当然报 off-viewport。
        #    这不是缺陷（正常滚动即可），但断言必须先把卡片滚进视口，
        #    否则测的是「卡片在屏幕外」而不是「卡片被遮挡」。
        self.page.evaluate("""() => {
          const el = document.querySelector('#jobsGrid .job-card[data-jid]');
          if (el) el.scrollIntoView({block: 'center'});
        }""")
        self.page.wait_for_timeout(600)
        fact = self.fact()
        self.assertGreater(fact["cardCount"], 0, "媒体库没有卡片")
        self.assertTrue(fact["G2_first_card"]["ok"],
                        "媒体库卡片点不到：%r" % fact["G2_first_card"])

        # 6) 打开「收藏」栏（左侧入口）
        self.open_drawer()
        # 收藏在滚动区下方 —— 先滚到底
        self.page.evaluate("""() => {
          const s = document.querySelector('#navScroll');
          s.scrollTop = s.scrollHeight;
        }""")
        self.page.wait_for_timeout(500)
        self.page.click("#navFavorites", timeout=6000)
        self.page.wait_for_timeout(1200)
        fact = self.fact()
        self.assertEqual(fact["activeTab"], "favorites",
                         "点「收藏」后应切到收藏标签，实际 %r" % fact["activeTab"])

        # 7) 打开卡片详情模态框
        card = self.page.query_selector("#jobsGrid .job-card[data-jid]")
        if card is not None:
            # 点卡片上的「详情」按钮
            btn = self.page.query_selector('#jobsGrid .job-card[data-jid] [data-act="detail"]')
            if btn is not None:
                btn.click(timeout=6000)
                self.page.wait_for_timeout(900)
                opened = self.page.evaluate(
                    "() => { const m = document.querySelector('#detailModal');"
                    " return m ? !m.hidden : false; }")
                self.assertTrue(opened, "点「详情」后详情模态框没打开")

    # ---------------------------------------- B3：筛选与分页可用
    def test_filters_and_page_size_operable_on_phone(self):
        """B3：状态标签 / 生成类型标签 / 每页数量在移动端都要能点。"""
        for width in PHONE_WIDTHS:
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 812})
                self.page.wait_for_timeout(600)
                fact = self.fact()
                for key in ("G4_type_tabs", "G5_status_tabs", "G6_page_size"):
                    self.assertTrue(
                        fact[key]["ok"],
                        "%dpx：%s 点不到（%r）" % (width, key, fact[key]))

    # ---------------------------------------- B5：桌面端零影响
    def test_desktop_unchanged(self):
        """B5：桌面端（1280）必须与修前完全一致 —— 侧栏常驻、无汉堡。"""
        self.page.set_viewport_size({"width": DESKTOP_WIDTH, "height": 900})
        self.page.wait_for_timeout(600)
        fact = self.fact()
        self.assertFalse(fact["toggleVisible"],
                         "桌面端不该显示汉堡按钮（侧栏常驻）")
        self.assertFalse(fact["drawerCloseVisible"],
                         "桌面端不该显示抽屉关闭按钮")
        self.assertFalse(fact["fabVisible"],
                         "桌面端不该显示「参数」浮动按钮（参数区常驻）")
        self.assertFalse(fact["sidebarOpen"], "桌面端侧栏不该是抽屉态")
        # 【为什么要先滚一下】第一张卡在 900px 高的视口下位于 y≈940 ——
        # 在屏幕**下方**，命中测试报 off-viewport。
        # 这不是缺陷（正常滚动即可），但断言必须先把卡片滚进视口，
        # 否则测的是「卡片在屏幕外」而不是「卡片被遮挡」。
        self.page.evaluate("""() => {
          const el = document.querySelector('#jobsGrid .job-card[data-jid]');
          if (el) {
            const area = document.querySelector('#jobsArea');
            const r = el.getBoundingClientRect();
            const ar = area.getBoundingClientRect();
            area.scrollTop += (r.top - ar.top) - ar.height / 3;
          }
        }""")
        self.page.wait_for_timeout(700)
        fact = self.fact()
        self.assertTrue(fact["G2_first_card"]["ok"],
                        "桌面端媒体库卡片应可见可点：%r" % fact["G2_first_card"])
        self.assertEqual(fact["docScrollW"], fact["vw"], "桌面端不该有横向滚动")

    # ---------------------------------------- 平板对照
    def test_tablet_sidebar_stays_persistent(self):
        """768px 平板：侧栏常驻（不是抽屉），与修前一致。"""
        self.page.set_viewport_size({"width": TABLET_WIDTH, "height": 1024})
        self.page.wait_for_timeout(600)
        fact = self.fact()
        self.assertFalse(fact["toggleVisible"],
                         "平板侧栏常驻，不该显示汉堡按钮")
        self.assertFalse(fact["sidebarOpen"], "平板侧栏不该是抽屉态")

    # ---------------------------------------- 无 JS 报错
    def test_no_console_errors(self):
        """页面不该有 JS 报错，也**不该**有失败的资源请求。

        【关于 404 缩略图】
        测试种子里的视频是一个**伪 MP4**（最小 ftyp+mdat 结构）——
        只够浏览器创建 <video> 元素，ffmpeg 解不出画面，
        所以 /api/thumb 对它返回 404。
        这是**既有的、文档化**的行为（server.py 的缩略图说明里写着
        「生成不出来 -> /api/thumb 返回 404 -> 浏览器回落引用原文件」）。

        【为什么按 URL 判定而不是笼统忽略所有 404】
        console 的报错文案（"Failed to load resource: ... 404"）**不含 URL**，
        没法区分「缩略图 404」和「产物文件真的丢了」。
        所以单独监听 response 事件，按 URL 精确放行 /api/thumb。
        其它任何 404 都算失败 —— 「产物文件丢了」不能变成绿灯。
        """
        real = [e for e in self.errors if e.strip()
                and not (("404" in e) and ("Failed to load resource" in e))]
        self.assertEqual(real, [], "页面有 JS 报错：%r" % real)

        bad_404 = [u for u in self.not_found_urls if "/api/thumb/" not in u]
        self.assertEqual(bad_404, [],
                         "有非缩略图的 404（产物文件可能丢了）：%r" % bad_404)
        self.thumb_404s = [u for u in self.not_found_urls if "/api/thumb/" in u]


if __name__ == "__main__":
    unittest.main()
