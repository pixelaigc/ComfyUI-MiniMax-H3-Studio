# -*- coding: utf-8 -*-
"""「增强」下拉菜单与结构化增强（layout）入口的回归测试。

背景：结构化增强原本是 MOSS-Image 面板上的**独立按钮**，
后按用户要求改成「增强」下拉菜单里的一个**增强方式选项**。
本文件把这条约定写死，防止回退：

1. **下拉选项路由** —— 注册表里有普通增强 / 二次增强 / 结构化增强三条；
   每条有独立的 id、显示名、可用模式；主按钮的文字随当前方式变化。
2. **旧独立入口已移除** —— `#mossLayoutBtn` / `#refineBtn` 不得再出现在
   任何前端文件里（不留重复入口）。
3. **选项选择触发的后端调用** —— 选中「结构化增强」后点「增强」，
   实际发出的请求必须是 `POST /api/moss-layout`（不是 /api/enhance）。
4. **回填行为** —— 产物回填到提示词框后与弹窗里的文本逐字符一致，
   且被标记为「已增强」。
5. **不破坏其它面板** —— Z-Image / Qwen-Image 面板下「结构化增强」被禁用；
   普通增强 / 二次增强仍可用。

运行：python3 -m pytest tests/test_enhance_menu.py -v
需要：playwright（chromium）。缺失时整类 skip，不算失败。
"""
import json
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

from test_frontend_defaults import StudioServer  # noqa: E402

WEB = REPO / "web"


# ------------------------------------------------------------------ 静态检查
class TestEnhanceMenuSource(unittest.TestCase):
    """前端源码的静态约定（不依赖浏览器）。"""

    def read(self, name: str) -> str:
        return (WEB / name).read_text(encoding="utf-8")

    def test_registry_lists_all_three_methods(self):
        """
        增强方式注册表必须同时含普通 / 二次 / 结构化三条。

        【Qwen 官方 PE 也在这个注册表里】（tests/test_qwen_pe_enhance.py 覆盖）
        这里断言「至少含这三条」，避免每加一种增强方式都改这条用例。
        """
        src = self.read("enhance-menu.js")
        for method_id in ("basic", "refine", "layout"):
            self.assertRegex(src, rf'id:\s*"{method_id}"',
                             f"注册表缺少 {method_id}")
        self.assertIn("结构化增强（layout）", src)

    def test_layout_method_is_moss_only(self):
        """结构化增强只在 MOSS-Image 模式可用 —— layout 坐标是 MOSS 的能力。"""
        src = self.read("enhance-menu.js")
        block = re.search(r'id:\s*"layout".*?\n\s*\}', src, re.S)
        self.assertIsNotNone(block, "找不到 layout 注册项")
        self.assertIn("t2i_moss", block.group(0),
                      "layout 注册项的 modes 必须含 t2i_moss")

    def test_layout_routes_to_moss_layout_endpoint(self):
        """选中结构化增强后必须打 /api/moss-layout（而不是 /api/enhance）。"""
        src = self.read("enhance-menu.js")
        block = re.search(r'async function runLayout\(.*?\n  \}', src, re.S)
        self.assertIsNotNone(block, "找不到 runLayout")
        self.assertIn("/api/moss-layout", block.group(0))
        self.assertNotIn("/api/enhance", block.group(0))

    def test_basic_and_refine_route_to_enhance_endpoint(self):
        """普通增强 / 二次增强仍走既有的 /api/enhance。"""
        src = self.read("enhance-menu.js")
        for fn in ("runBasic", "runRefine"):
            block = re.search(rf'async function {fn}\(.*?\n  \}}', src, re.S)
            self.assertIsNotNone(block, f"找不到 {fn}")
            self.assertIn("/api/enhance", block.group(0))

    def test_standalone_entries_are_removed(self):
        """旧的独立入口不得残留 —— 不能有两个地方能进结构化增强。"""
        for name in ("index.html", "app.js", "providers.js", "enhance-menu.js",
                     "moss-layout.js", "style.css"):
            src = self.read(name)
            self.assertNotIn("mossLayoutBtn", src,
                             f"{name} 仍有旧的 #mossLayoutBtn 独立入口")
            self.assertNotIn("mossLayoutBtnText", src,
                             f"{name} 仍有旧的 #mossLayoutBtnText")

    def test_refine_button_is_gone_but_refine_method_survives(self):
        """二次增强不再有独立按钮，但仍是下拉里的一种增强方式。"""
        for name in ("index.html", "app.js", "providers.js"):
            src = self.read(name)
            self.assertNotIn('id="refineBtn"', src,
                             f"{name} 仍有旧的 #refineBtn 独立按钮")
        src = self.read("enhance-menu.js")
        self.assertRegex(src, r'id:\s*"refine"')

    def test_dropdown_markup_exists(self):
        """面板上要有下拉箭头与菜单容器。"""
        html = self.read("index.html")
        self.assertIn('id="enhanceMenuBtn"', html)
        self.assertIn('id="enhanceMenu"', html)
        self.assertIn('role="menu"', html)

    def test_version_history_button_is_not_hidden_with_refine_target(self):
        """
        「版本历史」是所有增强方式共用的 —— 不能跟着「二次增强作用对象」
        一起被藏掉（那会让用户看不到版本历史）。
        """
        src = self.read("enhance-menu.js")
        self.assertIn("refineTargetGroup", src,
                      "应当只切换 #refineTargetGroup，而不是整个 #refineBar")
        self.assertNotRegex(src, r'refineBar.*hidden\s*=',
                            "不应整体隐藏 #refineBar（版本历史按钮在里面）")

    def test_layout_js_only_owns_the_modal(self):
        """moss-layout.js 只管产物弹窗，不再持有入口/路由。"""
        src = self.read("moss-layout.js")
        self.assertIn("renderResult", src)
        # 不该再自己发请求（路由归 enhance-menu.js）——
        # 注释里提到接口名不算，只查真正的调用。
        self.assertNotRegex(src, r"fetch\s*\(\s*[\"']/api/moss-layout",
                            "moss-layout.js 不该再自己发请求（路由归 enhance-menu.js）")
        self.assertNotIn("addEventListener(\"click\",", src.split("function bind")[0],
                         "moss-layout.js 不该再绑定入口点击（入口在 enhance-menu.js）")

    def test_no_external_api_dependency(self):
        """不引入任何对 OpenAI 官方 API 的硬依赖。"""
        for name in ("enhance-menu.js", "moss-layout.js"):
            src = self.read(name).lower()
            for banned in ("api.openai.com", "chat/completions", "bearer "):
                self.assertNotIn(banned, src, f"{name} 出现外部 API 依赖")


# ------------------------------------------------------------------ 浏览器验证
@unittest.skipUnless(HAS_PLAYWRIGHT, "需要 playwright（chromium）")
class TestEnhanceMenuBrowser(unittest.TestCase):
    """真实浏览器：下拉选项、路由、回填、其它面板不受影响。"""

    @classmethod
    def setUpClass(cls):
        cls.server = StudioServer(seed_jobs=0)
        assert cls.server.start(), "Studio 实例启动失败"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])
        cls.ctx = cls.browser.new_context(viewport={"width": 1280, "height": 900})
        cls.page = cls.ctx.new_page()
        cls.errors = []
        cls.page.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.page.on("console",
                    lambda m: cls.errors.append(m.text) if m.type == "error" else None)
        cls.base = f"http://127.0.0.1:{cls.server.port}/"
        cls.page.goto(cls.base, wait_until="domcontentloaded")
        cls.page.wait_for_timeout(2500)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.stop()

    def click_menu_item(self, method):
        """
        点下拉里某一项的复选框。

        下拉是**绝对定位**在按钮下方，可能落在视口之外 ——
        直接 page.click() 会因为「元素不可见」而超时。
        """
        self.page.evaluate(
            """(sel) => {
              const el = document.querySelector(sel);
              if (!el) return false;
              el.scrollIntoView({block: 'center'});
              el.click();
              return true;
            }""",
            f'#enhanceMenu input[data-method-check="{method}"]')
        self.page.wait_for_timeout(400)

    def uncheck_menu_item(self, method, menu="#enhanceMenu"):
        """取消勾选某一项。

        菜单每次勾选都会重渲染 -> 旧的 element handle 会失效，
        playwright 的 uncheck 会在元素被替换后做校验而超时。
        所以用 evaluate 直接改 checked 并派发 change 事件。
        """
        self.page.evaluate("""(args) => {
          const [menuId, mid] = args;
          const el = document.querySelector(
            `${menuId} input[data-method-check="${mid}"]`);
          if (el && el.checked && !el.disabled) {
            el.checked = false;
            el.dispatchEvent(new Event('change', {bubbles: true}));
          }
        }""", [menu, method])
        self.page.wait_for_timeout(300)

    def switch_mode(self, mode):
        self.page.click(f'.nav-item[data-mode="{mode}"]')
        self.page.wait_for_timeout(900)

    def reset_page(self):
        """
        每条用例前把页面恢复到干净状态。

        【为什么必须重置】
        上一条用例可能留下一个**打开的弹窗**（#enhanceModal /
        #mossLayoutModal），它的遮罩会拦截所有点击 —— 下一条用例的
        `page.click("#enhanceMenuBtn")` 就会一直超时。
        所以这里直接重新加载页面，回到干净状态。
        """
        self.page.goto(self.base, wait_until="domcontentloaded")
        self.page.wait_for_timeout(2000)
        # 「增强」多选状态存在 localStorage 里 -> 跨用例残留会让下一个用例
        # 看到上一个用例勾过的项（甚至是在当前模式**不可用**的项，
        # 那种复选框是 disabled 的，点不动）。所以每条用例前清干净。
        self.page.evaluate(
            "() => localStorage.removeItem('h3studio.enhanceSelection.v1')")
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_timeout(1500)

    def setUp(self):
        self.reset_page()


    def select_only(self, method_id):
        """只勾选指定增强项（取消默认勾的「普通增强」）。

        【多选语义】下拉默认勾了「普通增强」。要测「只跑某一项」，
        必须先把其它项取消勾 —— 否则多项都会被执行。
        """
        self.page.click("#enhanceMenuBtn")
        self.page.wait_for_timeout(300)
        # 顺序很重要：**先**勾上目标项，再取消其它项。
        # 因为界面不允许取消到「一项都不剩」（那会让「增强」按钮无事可做），
        # 所以先取消普通增强是取消不掉的。
        self.page.click(f'#enhanceMenu input[data-method-check="{method_id}"]')
        self.page.wait_for_timeout(250)
        # 取消其余项：用 evaluate 直接改 checked 并派发 change 事件
        # （菜单每次勾选都会重渲染 -> 旧的 element handle 会失效，
        #   playwright 的 uncheck 会在元素被替换后做校验而超时）。
        # 禁用的项（当前模式不可用）跳过 —— disabled 的复选框点不动。
        for mid in ("basic", "refine", "layout", "qwen_pe"):
            if mid == method_id:
                continue
            self.page.evaluate("""(mid) => {
              const el = document.querySelector(
                `#enhanceMenu input[data-method-check="${mid}"]`);
              if (el && el.checked && !el.disabled) {
                el.checked = false;
                el.dispatchEvent(new Event('change', {bubbles: true}));
              }
            }""", mid)
            self.page.wait_for_timeout(250)
        self.page.click(f'#enhanceMenu input[data-method-check="{method_id}"]')
        self.page.wait_for_timeout(350)

    # ---- 下拉选项 ----

    def test_dropdown_contains_all_three_methods(self):
        """
        下拉里至少有：普通增强 / 二次增强 / 结构化增强。

        【为什么是「至少」】Qwen 官方 PE 扩写后来也并进了同一个下拉
        （见 tests/test_qwen_pe_enhance.py），所以这里不再断言下拉项
        **恰好**是三个 —— 那样每加一种增强方式都要改这条用例。
        改成「至少包含这三个」，本用例守住的是
        「已有的三种增强方式没被改坏」。
        """
        self.switch_mode("t2i_moss")
        self.page.click("#enhanceMenuBtn")
        self.page.wait_for_timeout(400)
        items = self.page.eval_on_selector_all(
            "#enhanceMenu .enhance-menu-item[data-method]",
            "els => els.map(e => {const c = e.querySelector('input[data-method-check]');"
            " return {id: e.dataset.method, label:"
            " e.querySelector('.enhance-menu-label').textContent.trim(),"
            " checked: !!(c && c.checked), disabled: !!(c && c.disabled)};})")
        ids = [i["id"] for i in items]
        for required in ("basic", "refine", "layout"):
            self.assertIn(required, ids, f"下拉缺少 {required}")
        labels = [i["label"] for i in items]
        self.assertIn("普通增强", labels)
        self.assertIn("二次增强", labels)
        self.assertIn("结构化增强（layout）", labels)
        self.page.keyboard.press("Escape")

    def test_layout_option_enabled_in_moss_panel(self):
        self.switch_mode("t2i_moss")
        self.page.click("#enhanceMenuBtn")
        self.page.wait_for_timeout(400)
        disabled = self.page.get_attribute(
            '#enhanceMenu input[data-method-check="layout"]', "disabled")
        self.assertIsNone(disabled, "MOSS 面板下结构化增强应可用")
        self.page.keyboard.press("Escape")

    def test_layout_option_disabled_in_other_panels(self):
        """Z-Image / Qwen-Image 面板下结构化增强不可用 —— 不破坏现有行为。"""
        for mode in ("t2i", "t2i_qwen"):
            self.switch_mode(mode)
            self.page.click("#enhanceMenuBtn")
            self.page.wait_for_timeout(350)
            self.assertIsNotNone(
                self.page.get_attribute(
                    '#enhanceMenu input[data-method-check="layout"]', "disabled"),
                f"{mode} 面板下结构化增强应被禁用")
            # 其它两种方式仍可用
            for other in ("basic", "refine"):
                self.assertIsNone(
                    self.page.get_attribute(
                        f'#enhanceMenu input[data-method-check="{other}"]', "disabled"),
                    f"{mode} 面板下 {other} 不该被禁用")
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(200)

    def test_selecting_layout_changes_button_label_and_panels(self):
        self.switch_mode("t2i_moss")
        self.select_only("layout")
        self.assertEqual(
            self.page.evaluate("window.enhanceMethods && window.enhanceMethods.current"),
            "layout")
        self.assertIn("结构化增强", self.page.inner_text("#enhanceBtnText"))
        # 意图补充栏出现
        self.assertTrue(self.page.is_visible("#mossLayoutBar"))
        # 版本历史按钮仍可见（它是所有方式共用的）
        self.assertTrue(self.page.is_visible("#promptVersionBtn"))

    def test_switching_back_to_basic_restores_normal_enhance(self):
        """
        从「结构化增强」切回「普通增强」-> 意图补充栏收起、按钮文字变回「增强」。

        【多选语义】下拉是复选列表 —— 勾上「普通增强」是**追加**，
        不是替换。所以这里先取消「结构化增强」，再勾「普通增强」，
        让选中集合回到只剩普通增强。
        """
        self.switch_mode("t2i_moss")
        self.select_only("layout")
        self.assertTrue(self.page.is_visible("#mossLayoutBar"))
        # 先勾上「普通增强」（复选列表：勾选是追加，不是替换）
        self.page.click("#enhanceMenuBtn")
        self.page.wait_for_timeout(300)
        self.click_menu_item("basic")
        # 再取消「结构化增强」
        # （顺序不能反：只剩一项时取消不掉 —— 界面不允许「一项都不剩」）
        self.uncheck_menu_item("layout")
        selected = self.page.evaluate("window.enhanceMethods.getSelected()")
        self.assertEqual(selected, ["basic"],
                         f"切回普通增强后选中集合应只剩 basic，实际 {selected}")
        self.assertTrue(self.page.is_hidden("#mossLayoutBar"))
        self.assertIn("增强", self.page.inner_text("#enhanceBtnText"))

    # ---- 统一增强入口（2026-11-09 用户要求）----

    def test_no_second_enhance_dropdown(self):
        """
        规则 4：每个面板只有「一个增强按钮 + 一个下拉」。

        改造前 Z-Image 面板还有一个 #enhanceSpec「增强模板」下拉，
        与「增强方式」下拉语义重叠 —— 这里断言它已从 DOM 彻底消失。
        """
        self.assertIsNone(self.page.query_selector("#enhanceSpec"),
                          "#enhanceSpec（第二个增强下拉）应已从 DOM 删除")
        self.assertIsNone(self.page.query_selector("#musicEnhanceSpec"),
                          "音乐面板也不该有第二个增强下拉")

    def test_current_enhance_method_is_visible(self):
        """规则 4：明确显示当前所选增强方式。"""
        self.switch_mode("t2i")
        self.page.click("#enhanceMenuBtn")
        self.page.wait_for_timeout(250)
        self.click_menu_item("refine")
        note = self.page.inner_text("#enhanceCurrent")
        self.assertIn("二次增强", note,
                      f"当前增强方式回显应含「二次增强」，实际：{note!r}")
        self.assertIn("物体细化", note,
                      f"当前增强方式回显应含所用模板名，实际：{note!r}")

    def test_template_choice_lives_inside_the_menu(self):
        """
        模板选择**并入**增强方式下拉（不再是第二个平级下拉）。

        「普通增强」那一项下面带一个模板选择器，
        里面是**本面板**可用的模板（Z-Image 面板 = Z-Image 官方 PE 等）。
        """
        self.switch_mode("t2i")
        self.page.click("#enhanceMenuBtn")
        self.page.wait_for_timeout(350)
        sel = self.page.query_selector('#enhanceMenu select[data-spec-method="basic"]')
        self.assertIsNotNone(sel, "「普通增强」项下应有模板选择器")
        opts = [o.strip() for o in self.page.eval_on_selector_all(
            '#enhanceMenu select[data-spec-method="basic"] option',
            "els => els.map(e => e.textContent)")]
        self.assertTrue(opts, "模板选择器应列出模板")
        # Z-Image 面板：含 Z-Image 官方 PE 档；不含 Qwen 官方 PE 档
        joined = " | ".join(opts)
        self.assertIn("Z-Image 官方 PE", joined)
        self.assertNotIn("qwen21", joined.lower(),
                         f"Z-Image 面板不该列出 Qwen 专属模板，实际：{joined}")

    def test_second_enhance_is_universal_across_panels(self):
        """规则 2：二次增强是通用增强 —— 每个面板都能选到。"""
        for mode in ("t2i", "t2i_qwen", "i2i_qwen", "t2i_moss", "t2v", "t2a", "a2a"):
            self.switch_mode(mode)
            # 音乐面板的增强下拉是另一套 id（#musicEnhanceMenu）
            menu = "#musicEnhanceMenu" if mode in ("t2a", "a2a") else "#enhanceMenu"
            self.page.evaluate(
                """(menuId) => {
                  const music = menuId === '#musicEnhanceMenu';
                  const b = document.querySelector(music ? '#musicEnhanceMenuBtn'
                                                         : '#enhanceMenuBtn');
                  if (b) b.click();
                }""", menu)
            self.page.wait_for_timeout(300)
            disabled = self.page.get_attribute(
                f'{menu} input[data-method-check="refine"]', "disabled")
            self.assertIsNone(disabled,
                              f"{mode} 面板下「二次增强」不该被禁用（它是通用能力）")
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(150)

    def test_function_specific_enhance_is_scoped_to_its_panel(self):
        """
        规则 1：每个功能只有自己对应的增强能力。

        - MOSS 结构化增强只在 MOSS-Image 面板可用
        - Qwen 官方 PE 只在 Qwen 两个面板可用
        """
        cases = [
            ("t2i_moss", "layout", True),
            ("t2i", "layout", False),
            ("t2i_qwen", "layout", False),
            ("t2i_qwen", "qwen_pe", True),
            ("i2i_qwen", "qwen_pe", True),
            ("t2i", "qwen_pe", False),
            ("t2i_moss", "qwen_pe", False),
        ]
        for mode, method, should_enable in cases:
            self.switch_mode(mode)
            self.page.click("#enhanceMenuBtn")
            self.page.wait_for_timeout(250)
            disabled = self.page.get_attribute(
                f'#enhanceMenu input[data-method-check="{method}"]', "disabled")
            if should_enable:
                self.assertIsNone(disabled, f"{mode} 面板下 {method} 应可用")
            else:
                self.assertIsNotNone(disabled, f"{mode} 面板下 {method} 应被禁用")
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(150)

    # ---- 路由 + 回填 ----

    def test_layout_option_calls_moss_layout_endpoint(self):
        """
        选中「结构化增强」后点「增强」-> 打的是 /api/moss-layout。

        这里不连真实模型（会烧本地模型时间且不确定），
        而是用路由桩替换 fetch，**断言请求打到了哪个 URL** ——
        这正是本条要验的东西（选项 -> 接口的路由关系）。
        """
        self.switch_mode("t2i_moss")
        self.select_only("layout")
        self.page.fill("#prompt", "a lighthouse at dusk")

        # 装路由桩：记录 URL，按成功响应返回一份最小合法产物
        self.page.evaluate("""() => {
          window.__calls = [];
          const orig = window.fetch;
          window.fetch = async (url, opts) => {
            const u = String(url);
            window.__calls.push(u);
            if (u.includes('/api/moss-layout')) {
              const body = {
                ok: true, provider: '测试桩', provider_id: 'stub',
                width: 1024, height: 1024, size_notes: [],
                draft: {width:1024,height:1024,bbox_coord_space:'abs',
                        bbox_format:'xyxy',summary:{caption:'x'},items:[]},
                layout: {summary:{caption:'A quiet frame.'},
                         items:[{label:'object',caption:'A box.',bbox:[0,0,10,10]}],
                         bbox_coord_space:'abs_centralized',bbox_format:'yxyx'},
                layout_json: '{"summary":{"caption":"A quiet frame."},'
                             + '"bbox_coord_space":"abs_centralized",'
                             + '"bbox_format":"yxyx","items":[]}',
                final_prompt: 'A quiet frame.\\nAn object in the center.',
              };
              return new Response(JSON.stringify(body),
                                 {status:200, headers:{'Content-Type':'application/json'}});
            }
            return orig(url, opts);
          };
        }""")

        self.page.click("#enhanceBtn")
        self.page.wait_for_selector("#mossLayoutModal:not([hidden])", timeout=20000)
        calls = self.page.evaluate("window.__calls")
        self.assertTrue(any("/api/moss-layout" in c for c in calls),
                        f"应打 /api/moss-layout，实际请求：{calls}")
        self.assertFalse(any("/api/enhance" in c for c in calls),
                         f"不应打 /api/enhance，实际请求：{calls}")

        # 产物在弹窗里
        self.assertIn("A quiet frame", self.page.input_value("#mossLayoutPrompt"))
        self.assertIn("abs_centralized", self.page.input_value("#mossLayoutJson"))

        # 回填：逐字符一致
        expected = self.page.input_value("#mossLayoutPrompt")
        self.page.click("#mossLayoutApply")
        self.page.wait_for_timeout(400)
        self.assertEqual(self.page.input_value("#prompt").strip(), expected.strip())

        # 回填后被标记为「已增强」（版本历史里有这条）
        self.assertTrue(self.page.is_visible("#promptEnhancedBadge"))

    def test_basic_option_calls_enhance_endpoint(self):
        """普通增强仍然打 /api/enhance（没被结构化增强改坏）。"""
        self.switch_mode("t2i_moss")
        # 普通增强默认已勾 —— 这里要保证它是**唯一**被选中的项
        self.select_only("basic")
        self.page.fill("#prompt", "a lighthouse at dusk")
        self.page.evaluate("""() => {
          window.__calls = [];
          const orig = window.fetch;
          window.fetch = async (url, opts) => {
            const u = String(url);
            window.__calls.push(u);
            if (u.includes('/api/enhance')) {
              const body = {ok:true, provider:'测试桩', spec_id:'moss-image',
                            enhanced:'A lighthouse at dusk, wide view.',
                            original:'a lighthouse at dusk',
                            params:{sampling:'sample',temperature:0.7,top_p:0.9,
                                    max_tokens:300},
                            validation:{ok:true,errors:[],warnings:[],stats:{}}};
              return new Response(JSON.stringify(body),
                                 {status:200, headers:{'Content-Type':'application/json'}});
            }
            return orig(url, opts);
          };
        }""")
        self.page.click("#enhanceBtn")
        self.page.wait_for_selector("#enhanceModal:not([hidden])", timeout=20000)
        calls = self.page.evaluate("window.__calls")
        self.assertTrue(any("/api/enhance" in c for c in calls),
                        f"应打 /api/enhance，实际请求：{calls}")

    def test_layout_failure_shows_error_not_silent(self):
        """模型不可用 / 非法 JSON 时必须明确报错，不静默失败。"""
        self.switch_mode("t2i_moss")
        self.select_only("layout")
        self.page.fill("#prompt", "a lighthouse at dusk")
        # 弹窗会拦截 alert —— 记录它，证明「有明确提示」
        self.page.evaluate("""() => {
          window.__alerts = [];
          window.alert = (msg) => { window.__alerts.push(String(msg)); };
          window.fetch = async (url, opts) => {
            if (String(url).includes('/api/moss-layout')) {
              return new Response(JSON.stringify({
                error: '模型未返回有效的 JSON（第 1 行第 1 列）：Expecting value',
                degraded: true,
              }), {status:502, headers:{'Content-Type':'application/json'}});
            }
            return null;
          };
        }""")
        self.page.click("#enhanceBtn")
        self.page.wait_for_timeout(1500)
        alerts = self.page.evaluate("window.__alerts")
        self.assertTrue(alerts, "失败时必须有明确提示（alert）")
        self.assertIn("结构化增强失败", alerts[0])
        # 提示词框没被改动（回退到原文）
        self.assertEqual(self.page.input_value("#prompt").strip(),
                         "a lighthouse at dusk")

    def test_no_console_errors_after_menu_exercise(self):
        """走完一遍菜单交互后没有 console 报错。"""
        self.assertEqual(self.errors, [], f"console 报错：{self.errors}")


if __name__ == "__main__":
    unittest.main()
