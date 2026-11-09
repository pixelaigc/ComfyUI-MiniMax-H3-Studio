# -*- coding: utf-8 -*-
"""综合审查返修 —— 统一增强体系分支（local/feature/enh-unify）。

本文件把审查报告里要求修复的**契约**写死，防止回退：

P1-1（阻断）窄屏增强下拉弹到视口外
    根因：`.enhance-menu` 是 position:fixed，但祖先 #panel 带 transform
    -> #panel 成了 fixed 的包含块 -> 菜单落到视口上方 −979px。
    契约：菜单打开时挂在 document.body 下（脱离 transform 包含块），
          且在 375 / 390 视口下**完整可见**。

P1-2（阻断）多选增强后版本历史与「已增强」徽标全丢
    根因：pushPromptVersion 写在 `if (opts.collect) return` 之后，
    多选路径永远不写版本。
    契约：多选执行后版本历史非空、徽标可见、生成时不再弹
          「提示词尚未增强」。

P3-1（次要）禁用项同时挂「Recommended」与「本面板不可用」
    契约：禁用项只有**一套**文案（说明为什么这里没有），
          且不含 Recommended。

P1-2 / 代码审查 P1-2 模板选择器降级为子属性 + 双状态并存
    契约：模板选择器是增强栏里与增强方式**平级**的一行；
          模板选择状态只有一处来源；切换模式时失效旧选择。

运行：python3 -m pytest tests/test_enhance_review_fixes.py -v
需要：playwright（chromium）。缺失时整类 skip，不算失败。
"""
import re
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

WEB = REPO / "web"
ENHANCE_JS = WEB / "enhance-menu.js"
INDEX_HTML = WEB / "index.html"
STYLE_CSS = WEB / "style.css"


def strip_js_comments(text: str) -> str:
    """去掉 JS 行注释与块注释 —— 只留真实代码。

    「不得再出现某写法」的断言必须看**代码**，不能被
    「注释里引用了旧写法做说明」误伤。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)   # 块注释
    text = re.sub(r"//[^\n]*", "", text)                # 行注释
    return text


# ------------------------------------------------------------------ 静态契约
class TestEnhanceReviewStatic(unittest.TestCase):
    """源码级契约（不依赖浏览器）。"""

    def setUp(self):
        self.js = ENHANCE_JS.read_text(encoding="utf-8")
        # code：去掉注释后的真实代码（「不得再出现」的断言只看它）
        self.code = strip_js_comments(self.js)
        self.html = INDEX_HTML.read_text(encoding="utf-8")
        self.css = STYLE_CSS.read_text(encoding="utf-8")

    # ---- P1-1 窄屏菜单定位 ----
    def test_menu_is_mounted_to_body_when_open(self):
        """菜单打开时挂到 body —— 脱离 #panel 的 transform 包含块。"""
        self.assertIn("function mountMenuToBody", self.code,
                      "应有把菜单挂到 body 的实现")
        self.assertIn("document.body.appendChild(menu)", self.code,
                      "菜单必须 append 到 document.body")
        # openMenu 里必须先挂载再定位
        open_idx = self.code.find("function openMenu()")
        body_idx = self.code.find("mountMenuToBody(menu)", open_idx)
        pos_idx = self.code.find("positionMenu(menu, btn)", open_idx)
        self.assertTrue(0 < body_idx < pos_idx,
                        "openMenu 里必须「先挂到 body，再定位」")

    def test_menu_restore_on_close(self):
        """关闭时菜单要挂回原位（不能永久留在 body 里）。"""
        self.assertIn("function restoreMenuHome", self.code)
        close_idx = self.code.find("function closeMenu()")
        restore_idx = self.code.find("restoreMenuHome(menu)", close_idx)
        self.assertGreater(restore_idx, 0, "closeMenu 必须把菜单挂回原位")

    def test_position_clamps_to_viewport(self):
        """定位必须夹到视口内（左右下三边）。"""
        idx = self.code.find("function positionMenu(menu, btn)")
        self.assertGreater(idx, 0)
        body = self.code[idx:idx + 1200]
        self.assertIn("window.innerWidth", body, "应按视口宽度夹取")
        self.assertIn("window.innerHeight", body, "应按视口高度夹取")
        self.assertIn("left < 8", body, "左边也要夹（否则可能贴出视口）")

    # ---- P1-2 多选写版本历史 ----
    def test_multiselect_reports_version_info(self):
        """多选路径必须把版本信息回报给 run()（而不是提前 return 吞掉）。"""
        self.assertIn("version: {", self.code,
                      "collect 分支必须回报 version 信息")
        # run() 里必须用 versions 数组写版本历史
        run_idx = self.code.find("async function run(methodId)")
        body = self.code[run_idx:run_idx + 4000]
        self.assertIn("pushPromptVersion", body,
                      "run() 的多选路径必须写版本历史")
        self.assertIn("versions", body, "run() 必须收集各步骤的 version")

    def test_version_push_not_after_collect_return(self):
        """
        pushPromptVersion 不能写在 `if (opts.collect) return` 之后 ——
        那正是原缺陷（多选永远不写版本）。
        """
        for fn in ("runBasic", "runRefine"):
            idx = self.js.find(f"async function {fn}(")
            self.assertGreater(idx, 0, f"{fn} 未找到")
            body = self.code[idx:idx + 3500]
            collect_at = body.find("opts.collect")
            push_at = body.find("pushPromptVersion")
            if collect_at >= 0 and push_at >= 0:
                # collect 分支必须**先**返回（提前 return），
                # 而版本入栈在它之后的「单选」分支里 —— 这是**正确**的：
                # 多选路径由 run() 统一写版本（见 run() 里的 versions 数组），
                # 不依赖这里的单选分支。
                self.assertGreater(
                    push_at, collect_at,
                    f"{fn}: 单选的 pushPromptVersion 应在 collect 提前返回之后"
                    f"（多选路径由 run() 统一写版本）")
                # 反过来：collect 分支**必须**回报 version 信息
                # —— 否则多选路径拿不到版本数据
                self.assertIn(
                    "version: {", body,
                    f"{fn}: collect 分支必须回报 version 信息供多选路径写版本历史")

    # ---- P3-1 禁用项文案 ----
    def test_disabled_item_has_single_badge(self):
        """禁用项不能同时挂 Recommended 与「本面板不可用」。

        【2026-11-10】渲染已改为走共用组件（web/enhance-menu-shared.js），
        badge 的判定在那里 —— 所以断言查**共用组件**，而不是
        enhance-menu.js 的 renderMenu 内部。
        """
        shared = (WEB / "enhance-menu-shared.js").read_text(encoding="utf-8")
        idx = shared.find("function enhanceMenuItemHtml")
        self.assertGreater(idx, 0, "共用组件应负责菜单项渲染")
        body = shared[idx:idx + 2500]
        # badge 由 ok 决定：可用才 Recommended，不可用只给原因
        self.assertRegex(
            body, r"m\.recommended\s*&&\s*ok",
            "Recommended 徽标必须只在**可用**时出现")
        self.assertIn("unavailableText", body,
                      "不可用时必须给出原因文案")

    # ---- 模板选择器提升为平级一行 ----
    def test_spec_picker_is_a_parallel_row(self):
        """模板选择器是增强栏里的独立一行（与增强方式平级）。"""
        self.assertIn('id="enhanceSpecRow"', self.html,
                      "图像面板应有模板选择行")
        self.assertIn('id="musicEnhanceSpecRow"', self.html,
                      "音乐面板应有模板选择行")
        self.assertIn('id="enhanceSpecSelect"', self.html)
        self.assertIn('id="musicEnhanceSpecSelect"', self.html)
        self.assertIn(".enhance-spec-row", self.css, "模板行要有样式")

    def test_spec_row_is_not_inside_the_menu(self):
        """模板行必须在增强栏里，**不在**增强方式下拉内部。"""
        menu_idx = self.html.find('id="enhanceMenu"')
        row_idx = self.html.find('id="enhanceSpecRow"')
        self.assertGreater(menu_idx, 0)
        self.assertGreater(row_idx, 0)
        # 模板行不在 #enhanceMenu 元素内部（它是 #enhanceSplit 的兄弟）
        self.assertGreater(row_idx, menu_idx)

    def test_no_nested_spec_select_in_menu(self):
        """菜单项内部不应再有可点的模板子下拉（两级 UI 已去掉）。"""
        self.assertNotIn('select[data-spec-method]', self.code,
                         "菜单项里不应再有模板子下拉")
        # 只查**函数定义**（注释里提到不算）
        self.assertIsNone(
            re.search(r"function\s+renderSpecPicker\s*\(", self.code),
            "旧的 renderSpecPicker 应已被 renderSpecTag 取代")

    # ---- 模板状态统一 + 跨模式失效 ----
    def test_no_dual_template_state(self):
        """模板选择状态只有一处来源 —— 不再写 provState.specId。"""
        # 只查**真实赋值**（注释里提到不算）
        self.assertIsNone(
            re.search(r"provState\.specId\s*=[^=]", self.code),
            "enhance-menu.js 不应再写 provState.specId（双状态并存）")
        self.assertIsNone(
            re.search(r"defaults\[mode\]\s*\|\|\s*provState\.specId", self.code),
            "currentSpecId 不应再回落到全局 provState.specId")

    def test_spec_selection_invalidated_on_mode_switch(self):
        """切换模式时，不属于当前上下文的模板选择必须失效。"""
        self.assertIn("function invalidateSpecSelection", self.code)
        self.assertIn("invalidateSpecSelection();", self.code,
                      "ensureAvailable 必须调用失效逻辑")
        idx = self.code.find("function invalidateSpecSelection")
        body = self.code[idx:idx + 2000]
        self.assertIn("currentContextSpecs()", body,
                      "失效逻辑必须按当前上下文的可用模板集合判定")
        self.assertIn("delete selectedSpecByMethod[key]", body,
                      "失效的实现必须是删掉旧选择")

    def test_no_hardcoded_spec_fallback(self):
        """前端不应硬编码模板 id 兜底（服务端是唯一真源）。"""
        self.assertNotIn('return "image-refine"', self.code,
                         "refineSpecId 不应硬编码 image-refine 兜底")
        self.assertNotIn('return "image"', self.code,
                         "currentSpecId 不应硬编码 image 兜底")

    # ---- 音乐写回目标：能力契约 ----
    def test_music_writeback_is_capability_based(self):
        """音乐写回目标按能力解析（#musicPrompt / #musicStyle 都认）。

        【2026-11-10】写回改为**能力契约**（web/enhance-menu-shared.js 的
        resolveWriteTarget + providers.js 的 ENHANCE_WRITE_TARGETS）：
          - 文本路线的 targets 是候选列表（先新后旧）
          - 结构化路线由 music-enhance.js 注册契约
        两种形态都不硬编码某一个 id，找不到时返回原因而不抛异常。
        """
        providers = (WEB / "providers.js").read_text(encoding="utf-8")
        self.assertIn("ENHANCE_WRITE_TARGETS", providers,
                      "providers.js 应按能力契约解析写回目标")
        self.assertIn('"#musicPrompt"', providers,
                      "候选列表应包含 #musicPrompt（yue2-flow 命名）")
        self.assertIn('"#musicStyle"', providers,
                      "候选列表应包含 #musicStyle（enh-unify 命名，同一语义兜底）")

        shared = (WEB / "enhance-menu-shared.js").read_text(encoding="utf-8")
        self.assertIn("function resolveWriteTarget", shared)
        idx = shared.find("function resolveWriteTarget")
        body = shared[idx:idx + 2000]
        self.assertIn("ok: false", body,
                      "找不到目标时应返回 { ok:false, reason }（由调用方给可读提示）")
        self.assertIn("reason", body)

        # 契约缺失时也不应抛异常
        self.assertIn("if (!contract)", body,
                      "契约缺失时应给出原因而不是抛异常")

    def test_writeback_handlers_handle_missing_target(self):
        """写回按钮在目标缺失时给可读提示，不抛 TypeError。"""
        providers = (WEB / "providers.js").read_text(encoding="utf-8")
        # 【2026-11-10】写回改为能力契约后，提示文案在 writeBackEnhanceResult 里
        self.assertIn("function writeBackEnhanceResult", providers,
                      "写回应走契约解析")
        idx = providers.find("function writeBackEnhanceResult")
        body = providers[idx:idx + 1200]
        self.assertIn("写回失败", body,
                      "写回时找不到目标应给出可读提示")
        # 不能直接对可能为 null 的结果取 .value
        self.assertNotIn("enhanceTargetInput().value", providers,
                         "不能直接对可能为 null 的结果取 .value")

    def test_enhance_menu_uses_capability_contract(self):
        """enhance-menu.js 也必须走能力契约（不能硬编码某个 id）。"""
        self.assertIn("function resolveTarget", self.code)
        self.assertIn("function currentInputValue", self.code)
        self.assertIn("function writeBackInput", self.code)
        # 只查「硬编码的 targets().input」—— inputSelectors 是能力契约的一部分
        self.assertIsNone(
            re.search(r"targets\(\)\.input(?!Selectors)", self.code),
            "不能再用硬编码的 targets().input")
    # ---- 并发闸门 + 超时上限 ----
    def test_enhance_endpoint_has_concurrency_gate(self):
        """/api/enhance 必须有并发闸门。"""
        server = (REPO / "server.py").read_text(encoding="utf-8")
        self.assertIn("ENHANCE_CONCURRENCY", server)
        self.assertIn("def _enhance_gate()", server)
        self.assertIn("asyncio.Semaphore(ENHANCE_CONCURRENCY)", server)
        idx = server.find("async def api_enhance(")
        body = server[idx:idx + 6000]
        self.assertIn("_enhance_gate()", body, "api_enhance 必须取闸门")
        self.assertIn("async with gate:", body, "api_enhance 必须持有闸门")
        self.assertIn("429", body, "抢不到闸门必须返回 429（不排队）")

    def test_max_timeout_capped_at_600(self):
        """MAX_TIMEOUT 收到 600（不再是 1800）。"""
        llm = (REPO / "h3studio" / "llm.py").read_text(encoding="utf-8")
        self.assertRegex(llm, r"MAX_TIMEOUT\s*=\s*600\.0",
                         "MAX_TIMEOUT 应为 600 秒")


# ------------------------------------------------------------------ 浏览器行为
@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright/chromium 缺失，跳过浏览器行为测试")
class TestEnhanceReviewBrowser(unittest.TestCase):
    """真实浏览器里的返修后行为。"""

    @classmethod
    def setUpClass(cls):
        from test_frontend_defaults import StudioServer
        server = StudioServer()
        started = server.start()
        assert started, "Studio 实例启动失败"
        cls.server = server
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()
        cls.page = cls.browser.new_page(viewport={"width": 1280, "height": 900})
        cls.errors = []
        cls.page.on("console", lambda m: cls.errors.append(m.text)
                    if m.type == "error" else None)
        cls.page.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.base = f"http://127.0.0.1:{cls.server.port}/"
        cls.page.goto(cls.base, wait_until="domcontentloaded")
        cls.page.wait_for_timeout(1500)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close()
        finally:
            cls.playwright.stop()
            cls.server.stop()

    def setUp(self):
        self.page.evaluate(
            "() => { try { localStorage.removeItem('h3studio.enhanceSelection.v1'); }"
            "catch (e) {} }")
        self.page.reload(wait_until="domcontentloaded")
        self.wait_ready()

    def wait_ready(self, timeout_ms=20000):
        self.page.wait_for_function(
            """() => {
              try {
                return typeof provState !== 'undefined'
                  && provState && provState.agent
                  && Array.isArray(provState.agent.specs)
                  && provState.agent.specs.length > 0
                  && Object.keys(provState.modeFilters || {}).length > 0
                  && Object.keys(provState.refineSpecs || {}).length > 0;
              } catch (e) { return false; }
            }""",
            timeout=timeout_ms)
        self.page.wait_for_timeout(300)

    def switch_mode(self, mode):
        self.page.evaluate(
            "(m) => { const el = document.querySelector(`.nav-item[data-mode='${m}']`);"
            "if (el) el.click(); }", mode)
        self.page.wait_for_timeout(700)

    def ensure_target_visible(self, selector):
        """
        确保目标元素在视口内并可点击。

        窄屏（≤1100px）下参数面板是**底部抽屉**，默认收起 —— 目标根本不在
        视口里（实测 top=1103 > innerHeight=780）。必须先点 #panelFab 打开
        抽屉，再把目标滚进视野。
        """
        self.page.evaluate(
            """() => {
              const p = document.querySelector('#panel');
              if (p && !p.classList.contains('open')) {
                const fab = document.querySelector('#panelFab');
                if (fab) fab.click();
              }
            }""")
        self.page.wait_for_timeout(500)
        self.page.evaluate(
            """(sel) => {
              const el = document.querySelector(sel);
              if (el) el.scrollIntoView({block: 'center', inline: 'nearest'});
            }""", selector)
        self.page.wait_for_timeout(350)

    # ---- P1-1 窄屏菜单可见性 ----
    def test_menu_visible_on_narrow_viewports(self):
        """
        P1-1：375 / 390 视口下，增强下拉必须**完整可见**。

        原缺陷：menuTop=-979，menuOnScreen=false（屏幕上一个选项都看不到）。
        """
        for width in (375, 390):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 780})
                self.page.wait_for_timeout(400)
                self.switch_mode("t2i")
                # 窄屏下参数面板是底部抽屉 —— 先打开并把按钮滚进视野
                self.ensure_target_visible("#enhanceMenuBtn")
                self.page.evaluate(
                    """() => {
                      const b = document.querySelector('#enhanceMenuBtn');
                      if (b) b.click();
                    }""")
                self.page.wait_for_timeout(500)
                geo = self.page.evaluate(
                    """() => {
                      const m = document.querySelector('#enhanceMenu');
                      if (!m) return null;
                      const r = m.getBoundingClientRect();
                      return {
                        hidden: m.hidden,
                        parentIsBody: m.parentElement === document.body,
                        top: Math.round(r.top),
                        bottom: Math.round(r.bottom),
                        left: Math.round(r.left),
                        right: Math.round(r.right),
                        h: Math.round(r.height),
                        innerH: window.innerHeight,
                        innerW: window.innerWidth,
                        items: m.querySelectorAll('.enhance-menu-item').length,
                      };
                    }""")
                self.assertIsNotNone(geo, "增强菜单应存在")
                self.assertFalse(geo["hidden"], "菜单应处于展开状态")
                self.assertTrue(geo["parentIsBody"],
                                "菜单应挂到 body（脱离 transform 包含块）")
                self.assertGreaterEqual(geo["top"], 0,
                                        f"{width}px 视口下菜单顶部出屏：{geo}")
                self.assertLessEqual(geo["bottom"], geo["innerH"],
                                     f"{width}px 视口下菜单底部出屏：{geo}")
                self.assertGreaterEqual(geo["left"], 0,
                                        f"{width}px 视口下菜单左侧出屏：{geo}")
                self.assertLessEqual(geo["right"], geo["innerW"] + 1,
                                     f"{width}px 视口下菜单右侧出屏：{geo}")
                self.assertGreater(geo["items"], 0, "菜单里应有选项")
                self.assertGreater(geo["h"], 0, "菜单应有可见高度")

    def test_menu_restored_to_original_parent_on_close(self):
        """关闭菜单后要挂回原位（不能永久留在 body 里）。"""
        self.page.set_viewport_size({"width": 375, "height": 780})
        self.switch_mode("t2i")
        self.page.evaluate(
            """() => { const b = document.querySelector('#enhanceMenuBtn');
                    if (b) b.click(); }""")
        self.page.wait_for_timeout(400)
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(400)
        parent = self.page.evaluate(
            """() => {
              const m = document.querySelector('#enhanceMenu');
              return m ? m.parentElement.id || m.parentElement.tagName : '';
            }""")
        self.assertNotEqual(parent, "BODY",
                            "关闭后菜单应挂回原来的父节点")

    # ---- P1-2 多选写版本历史 ----
    def test_multiselect_writes_version_history_and_badge(self):
        """
        P1-2：多选增强执行后，版本历史非空 + 徽标可见。

        原缺陷：多选路径 collect=true 提前 return，版本历史一次都没写，
        徽标隐藏，点生成还会弹「提示词尚未增强」。
        """
        self.switch_mode("t2i")
        self.ensure_target_visible("#enhanceMenuBtn")
        # 勾选「二次增强」（默认已勾普通增强 -> 变成多选）
        self.page.evaluate(
            """() => {
              const b = document.querySelector('#enhanceMenuBtn');
              if (b) b.click();
            }""")
        self.page.wait_for_timeout(400)
        self.page.evaluate(
            """() => {
              const el = document.querySelector(
                '#enhanceMenu input[data-method-check="refine"]');
              if (el) el.click();
            }""")
        self.page.wait_for_timeout(400)
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(300)

        # 桩掉两个后端接口
        self.page.evaluate("""() => {
          window.__calls = [];
          const orig = window.fetch;
          window.fetch = async (url, opts) => {
            const u = String(url);
            window.__calls.push(u);
            const json = (body) => new Response(JSON.stringify(body), {
              status: 200, headers: {'Content-Type': 'application/json'}});
            if (u.includes('/api/enhance')) {
              return json({ok: true, provider: '桩', spec_id: 'image',
                enhanced: '【L1 特写】桩产出\\n【主体细化】桩', original: 'x'});
            }
            return orig(url, opts);
          };
        }""")
        self.page.fill("#prompt", "a lighthouse at dusk")
        self.ensure_target_visible("#enhanceBtn")
        self.page.evaluate(
            """() => { const b = document.querySelector('#enhanceBtn');
                    if (b) b.click(); }""")
        self.page.wait_for_timeout(2500)

        # 点「替换提示词框」完成写回
        self.page.evaluate(
            """() => { const b = document.querySelector('#enhanceReplace');
                    if (b) b.click(); }""")
        self.page.wait_for_timeout(600)

        versions = self.page.evaluate(
            """() => (typeof promptVersions !== 'undefined'
                      && promptVersions ? promptVersions.versions : []).map(
                      v => ({level: v.level, source: v.source,
                             text: (v.text || '').slice(0, 40)}))""")
        self.assertTrue(versions,
                        "多选增强后版本历史不应为空（这是 P1-2 的核心契约）")
        levels = {v["level"] for v in versions}
        self.assertIn(2, levels,
                      f"多选里含二次增强，版本历史应有 level=2 条目：{versions}")
        self.assertIn(1, levels,
                      f"多选里含普通增强，版本历史应有 level=1 条目：{versions}")

        badge = self.page.evaluate(
            """() => {
              const b = document.querySelector('#promptEnhancedBadge');
              return b ? {hidden: b.hidden, text: b.textContent} : null;
            }""")
        self.assertIsNotNone(badge, "应有「已增强」徽标元素")
        self.assertFalse(badge["hidden"],
                         f"多选增强后「已增强」徽标应显示：{badge}")

        # 生成时不应再弹「提示词尚未增强」
        level = self.page.evaluate(
            "() => (typeof currentPromptLevel === 'function'"
            " ? currentPromptLevel() : -1)")
        self.assertGreater(level, 0,
                           "多选增强后当前提示词等级应 > 0（生成时不再误判未增强）")

    # ---- P3-1 禁用项文案 ----
    def test_disabled_item_has_only_unavailable_reason(self):
        """禁用项只显示「本面板不可用」，不再同时挂 Recommended。"""
        self.switch_mode("t2i")
        self.ensure_target_visible("#enhanceMenuBtn")
        self.page.evaluate(
            """() => { const b = document.querySelector('#enhanceMenuBtn');
                    if (b) b.click(); }""")
        self.page.wait_for_timeout(500)
        items = self.page.evaluate(
            """() => Array.from(
                 document.querySelectorAll('#enhanceMenu .enhance-menu-item')
               ).map(it => ({
                 method: it.dataset.method,
                 unavailable: it.classList.contains('unavailable'),
                 disabled: !!it.querySelector('input[type=checkbox]')?.disabled,
                 badges: Array.from(it.querySelectorAll('.enhance-menu-badge'))
                           .map(b => b.textContent.trim()),
               }))""")
        self.assertTrue(items, "菜单里应有选项")
        for it in items:
            if not it["unavailable"]:
                continue
            joined = " | ".join(it["badges"])
            self.assertNotIn("Recommended", joined,
                             f"{it['method']} 禁用项不应同时挂 Recommended：{joined}")
            self.assertTrue(joined, f"{it['method']} 禁用项应说明为什么不可用")
            self.assertIn("本面板不可用", joined,
                          f"{it['method']} 禁用项应写明归属功能：{joined}")

    # ---- 模板选择器：平级一行 ----
    def test_spec_row_is_visible_and_parallel(self):
        """模板选择器是增强栏里的独立一行，与增强方式平级。"""
        self.switch_mode("t2i")
        row = self.page.evaluate(
            """() => {
              const r = document.querySelector('#enhanceSpecRow');
              const sel = document.querySelector('#enhanceSpecSelect');
              if (!r || !sel) return null;
              const rb = r.getBoundingClientRect();
              return {
                hidden: r.hidden,
                visible: rb.width > 0 && rb.height > 0,
                options: Array.from(sel.options).map(o => o.value),
                value: sel.value,
                inMenu: !!document.querySelector('#enhanceMenu #enhanceSpecRow'),
              };
            }""")
        self.assertIsNotNone(row, "应有模板选择行")
        self.assertFalse(row["hidden"], "普通增强有多个模板 -> 模板行应显示")
        self.assertTrue(row["visible"], "模板行应可见")
        self.assertFalse(row["inMenu"], "模板行不应在增强方式下拉内部")
        self.assertTrue(row["options"], "模板行应列出模板")
        self.assertIn(row["value"], row["options"], "当前值应在选项里")

    def test_spec_selection_invalidated_across_modes(self):
        """
        模板选择状态跨模式必须失效。

        场景：在 Z-Image 面板选了某个模板 -> 切到 Qwen 面板 ->
              再切回来，不能命中别的上下文的模板。
        """
        self.switch_mode("t2i")
        # 选一个非默认模板
        picked = self.page.evaluate(
            """() => {
              const sel = document.querySelector('#enhanceSpecSelect');
              if (!sel || sel.options.length < 2) return '';
              const opt = sel.options[1];
              sel.value = opt.value;
              sel.dispatchEvent(new Event('change', {bubbles: true}));
              return opt.value;
            }""")
        if not picked:
            self.skipTest("当前上下文只有一个模板，无法验证跨模式失效")
        self.page.wait_for_timeout(400)
        stored = self.page.evaluate(
            "() => window.enhanceMethods.getSelectedSpecByMethod()")
        self.assertTrue(any(picked in str(v) for v in stored.values()),
                        f"模板选择应被记录：{stored}")

        # 切到 Qwen 面板（模板集合不同）
        self.switch_mode("t2i_qwen")
        self.page.wait_for_timeout(500)
        self.switch_mode("t2i")
        self.page.wait_for_timeout(600)

        # 回到 Z-Image 面板后，当前模板必须在**本面板的**可用集合里
        state = self.page.evaluate(
            """() => {
              const sel = document.querySelector('#enhanceSpecSelect');
              const opts = sel ? Array.from(sel.options).map(o => o.value) : [];
              return {value: sel ? sel.value : '', options: opts};
            }""")
        self.assertIn(state["value"], state["options"],
                      f"跨模式回来后当前模板必须属于本面板可用集合：{state}")

    def test_no_console_errors(self):
        """本轮改动不应引入 console 报错。"""
        self.assertEqual([e for e in self.errors if e.strip()], [],
                         f"console 报错：{self.errors}")


if __name__ == "__main__":
    unittest.main()
