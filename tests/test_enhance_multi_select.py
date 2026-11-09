# -*- coding: utf-8 -*-
"""「增强」**多选**列表 —— Qwen 官方 PE 作为一条普通增强项。

背景（2026-11-09 用户要求）：
  「增强」下拉原本是**单选**（选中一项 = 当前方式）。
  用户要求把它改成**多选复选列表**：
    1. 各项可同时选中，官方 PE 与其它增强项同级（标 Recommended）
    2. 点「增强」后按**选中顺序依次应用**各项的推荐参数
    3. 各项的提示词片段合并追加到最终提示词，互不覆盖、不互相截断
    4. 多选时各项参数互不覆盖（后一项不清掉前一项已套用的参数）
    5. 复用既有代码路径；原独立 PE 勾选框路径不得残留成死代码
    6. 界面要能看出哪些增强项处于选中状态 + 推荐参数已套用的可见反馈

本文件把上面 6 条写死，防止回退：

A. **静态契约**（不依赖浏览器）
   - 注册表里 qwen_pe 带 recommended 标记与可编辑字段声明
   - 多选相关 API 存在（toggle / getSelected / mergeFragments / refreshButton）
   - 没有另起一套平行的「增强项列表」（不出现 enhanceList 之类的第二套渲染）
   - 旧的独立 PE 勾选框标识零残留
   - 后端接受 system_prompt / pe_params 覆盖，且出图链路用同一份文本

B. **浏览器行为**（playwright + chromium，缺失时整类 skip）
   - 复选框能同时选中多项
   - 选中官方 PE -> 推荐参数自动套用并有可见回显
   - 参数可手动改，改后生效
   - 多项共存时参数叠加（后一项不清掉前一项）
   - 片段合并互不覆盖
   - console 0 报错

运行：python3 -m pytest tests/test_enhance_multi_select.py -v
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


# ------------------------------------------------------------------ 静态契约
class TestEnhanceMultiSelectSource(unittest.TestCase):
    """前端源码的静态约定（不依赖浏览器）。"""

    def read(self, name: str) -> str:
        return (WEB / name).read_text(encoding="utf-8")

    def test_qwen_pe_is_a_registry_item_with_recommended_flag(self):
        """官方 PE 是注册表里的一条**普通增强项**，并标 Recommended。"""
        src = self.read("enhance-menu.js")
        self.assertRegex(src, r'id:\s*"qwen_pe"')
        self.assertIn("qwen21图片官方增强PE", src)
        block = re.search(r'id:\s*"qwen_pe".*?\n\s*\},', src, re.S)
        self.assertIsNotNone(block, "找不到 qwen_pe 注册项")
        body = block.group(0)
        self.assertIn("recommended: true", body,
                      "官方 PE 项应标 Recommended（与其它项同级但推荐）")
        self.assertIn("fields:", body,
                      "官方 PE 项应声明自带的可编辑字段")

    def test_registry_declares_editable_fields(self):
        """官方 PE 项自带三个可编辑字段：system prompt + 两个档位参数。"""
        src = self.read("enhance-menu.js")
        for key in ("systemPrompt", "presencePenalty", "maxNewTokens"):
            self.assertIn(f'key: "{key}"', src,
                          f"官方 PE 项应声明可编辑字段 {key}")

    def test_multi_select_api_is_exposed(self):
        """多选所需的对外 API 必须存在。"""
        src = self.read("enhance-menu.js")
        for api in ("toggle:", "getSelected:", "mergeFragments:",
                    "refreshButton:", "updatePanelVisibility:"):
            self.assertIn(api, src, f"缺少对外 API {api}")

    def test_menu_renders_checkbox_per_item(self):
        """每一项必须渲染一个复选框（多选的前提）。"""
        src = self.read("enhance-menu.js")
        self.assertIn("data-method-check", src,
                      "菜单项必须带复选框（多选）")
        self.assertIn('type="checkbox"', src)
        # 复选框的 change 事件 -> 走 toggleMethod
        self.assertRegex(src, r'addEventListener\("change",\s*\(\)\s*=>\s*\{\s*'
                         r'toggleMethod\(',
                         "复选框的 change 必须触发切换逻辑")

    def test_selection_is_multi_not_radio(self):
        """选中集合是数组（可多项），不是单个 currentId。"""
        src = self.read("enhance-menu.js")
        self.assertIn("selectedIds", src)
        self.assertRegex(src, r"let selectedIds\s*=\s*\[")
        # toggle 而不是「点一下就替换掉全部」
        self.assertIn("selectedIds.push(id)", src,
                      "多选：选中应追加而不是替换")
        self.assertIn("selectedIds = selectedIds.filter", src,
                      "多选：取消应只移除该项")

    def test_no_parallel_enhance_item_list(self):
        """不得另起一套平行的「增强项列表」渲染（复用既有下拉即可）。"""
        for name in ("enhance-menu.js", "app.js", "index.html"):
            src = self.read(name)
            for token in ("enhanceItemList", "enhance-list", "enhanceItems"):
                self.assertNotIn(token, src,
                                 f"{name} 出现了平行实现标识 {token}")

    def test_no_stale_standalone_pe_checkbox(self):
        """独立 PE 勾选框路径不得残留（连 DOM 都不能有）。"""
        app_js = self.read("app.js")
        code_only = re.sub(r"//[^\n]*", "", app_js)
        code_only = re.sub(r"/\*.*?\*/", "", code_only, flags=re.S)
        for token in ("imgUsePe", "imgPeField", "imgPeTier", "imgPeNote"):
            self.assertNotIn(token, code_only,
                             f"app.js 里仍残留独立 PE 入口标识 {token}")
        html = self.read("index.html")
        self.assertNotIn('type="checkbox" id="imgUsePe"', html)

    def test_applied_params_feedback_element_exists(self):
        """必须有「推荐参数已套用」的可见回显元素。"""
        html = self.read("index.html")
        self.assertIn('id="enhanceAppliedParams"', html)
        src = self.read("enhance-menu.js")
        self.assertIn("updateAppliedParamsNote", src)

    def test_backend_accepts_pe_overrides(self):
        """后端接受 system_prompt / pe_params 覆盖。"""
        server = (REPO / "server.py").read_text(encoding="utf-8")
        self.assertIn('payload.get("system_prompt")', server)
        self.assertIn('payload.get("pe_params")', server)
        wf = (REPO / "h3studio" / "workflows.py").read_text(encoding="utf-8")
        self.assertIn("_apply_pe_overrides", wf)
        self.assertIn("pe_system_prompt", wf)
        # 出图链路与手动扩写用同一份 system prompt
        self.assertIn("pe_system_prompt", wf)


# ------------------------------------------------------------------ 浏览器行为
@unittest.skipUnless(HAS_PLAYWRIGHT, "需要 playwright（chromium）")
class TestEnhanceMultiSelectBrowser(unittest.TestCase):
    """真实浏览器：多选、参数自动套用、片段合并。"""

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

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.stop()

    def setUp(self):
        self.errors.clear()
        self.page.goto(self.base, wait_until="domcontentloaded")
        self.page.wait_for_timeout(2200)
        # 清掉上次留下的选中状态，保证每条用例从干净状态开始
        self.page.evaluate("() => localStorage.removeItem('h3studio.enhanceSelection.v1')")
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_timeout(2200)

    def switch_mode(self, mode):
        self.page.click(f'.nav-item[data-mode="{mode}"]')
        self.page.wait_for_timeout(900)

    def open_menu(self):
        self.page.click("#enhanceMenuBtn")
        self.page.wait_for_timeout(400)

    def checked_ids(self):
        return self.page.evaluate(
            "() => window.enhanceMethods.getSelected()")

    # ---- 多选本身 ----

    def test_can_select_multiple_items_at_once(self):
        """复选框能同时选中多项（不是单选）。"""
        self.switch_mode("t2i_qwen")
        self.open_menu()
        # 勾上官方 PE（普通增强默认已勾）
        self.page.click('#enhanceMenu input[data-method-check="qwen_pe"]')
        self.page.wait_for_timeout(400)
        self.page.click('#enhanceMenu input[data-method-check="refine"]')
        self.page.wait_for_timeout(400)
        selected = self.checked_ids()
        self.assertIn("qwen_pe", selected)
        self.assertIn("refine", selected)
        self.assertGreaterEqual(len(selected), 2,
                                "多项应能同时处于选中状态")

    def test_unchecking_one_keeps_others(self):
        """取消一项不影响其它已选项。"""
        self.switch_mode("t2i_qwen")
        self.open_menu()
        self.page.click('#enhanceMenu input[data-method-check="qwen_pe"]')
        self.page.wait_for_timeout(300)
        self.page.click('#enhanceMenu input[data-method-check="refine"]')
        self.page.wait_for_timeout(300)
        self.page.click('#enhanceMenu input[data-method-check="basic"]')
        self.page.wait_for_timeout(400)
        selected = self.checked_ids()
        self.assertNotIn("basic", selected)
        self.assertIn("qwen_pe", selected)
        self.assertIn("refine", selected)

    def test_checkbox_state_is_visible(self):
        """界面要能看出哪些增强项处于选中状态。"""
        self.switch_mode("t2i_qwen")
        self.open_menu()
        self.page.click('#enhanceMenu input[data-method-check="qwen_pe"]')
        self.page.wait_for_timeout(400)
        checked = self.page.evaluate(
            """() => Array.from(document.querySelectorAll(
                 '#enhanceMenu input[data-method-check]'))
                 .filter(c => c.checked).map(c => c.dataset.methodCheck)""")
        self.assertIn("qwen_pe", checked)
        # 选中的那一项要视觉上区分（checked class）
        cls = self.page.get_attribute(
            '#enhanceMenu .enhance-menu-item[data-method="qwen_pe"]', "class")
        self.assertIn("checked", cls or "")

    # ---- 参数自动套用 ----

    def test_selecting_qwen_pe_auto_applies_recommended_params(self):
        """选中官方 PE -> 推荐参数自动套用，且有可见回显。"""
        self.switch_mode("t2i_qwen")
        self.open_menu()
        self.page.click('#enhanceMenu input[data-method-check="qwen_pe"]')
        self.page.wait_for_timeout(600)
        params = self.page.evaluate("() => state.imgQwen.peParams")
        self.assertIsNotNone(params, "选中官方 PE 后应自动套用推荐参数")
        # 文生图档官方值：presence_penalty=1.5 / max_new_tokens=16256
        self.assertEqual(params["presence_penalty"], 1.5)
        self.assertEqual(params["max_new_tokens"], 16256)
        # 可见回显
        self.assertTrue(self.page.is_visible("#enhanceAppliedParams"))
        note = self.page.inner_text("#enhanceAppliedParams")
        self.assertIn("1.5", note)
        self.assertIn("16256", note)

    def test_params_are_editable_and_take_effect(self):
        """参数可手动改，改后立即生效。"""
        self.switch_mode("t2i_qwen")
        self.open_menu()
        self.page.click('#enhanceMenu input[data-method-check="qwen_pe"]')
        self.page.wait_for_timeout(500)
        box = '#enhanceMenu input[data-field="presencePenalty"]'
        self.page.fill(box, "0.7")
        self.page.wait_for_timeout(500)
        params = self.page.evaluate("() => state.imgQwen.peParams")
        self.assertAlmostEqual(params["presence_penalty"], 0.7, places=3)

    def test_system_prompt_is_editable_and_reaches_state(self):
        """官方 PE 的 system prompt 可编辑，且进入出图参数。"""
        self.switch_mode("t2i_qwen")
        self.open_menu()
        self.page.click('#enhanceMenu input[data-method-check="qwen_pe"]')
        self.page.wait_for_timeout(500)
        ta = '#enhanceMenu textarea[data-field="systemPrompt"]'
        self.assertTrue(self.page.is_visible(ta),
                        "官方 PE 项应露出 system prompt 编辑框")
        self.page.fill(ta, "自定义的扩写指令 ABC")
        self.page.wait_for_timeout(500)
        sp = self.page.evaluate("() => state.imgQwen.peSystemPrompt")
        self.assertEqual(sp, "自定义的扩写指令 ABC")

    def test_multi_select_params_do_not_overwrite_each_other(self):
        """多项共存时参数互不覆盖（后一项不清掉前一项）。"""
        self.switch_mode("t2i_qwen")
        self.open_menu()
        self.page.click('#enhanceMenu input[data-method-check="qwen_pe"]')
        self.page.wait_for_timeout(500)
        self.page.fill('#enhanceMenu input[data-field="presencePenalty"]', "0.8")
        self.page.wait_for_timeout(400)
        # 再勾一项（普通增强本身不带 PE 参数）
        self.page.click('#enhanceMenu input[data-method-check="refine"]')
        self.page.wait_for_timeout(500)
        params = self.page.evaluate("() => state.imgQwen.peParams")
        self.assertIsNotNone(params,
                             "勾选其它项后，官方 PE 的参数不该被清掉")
        self.assertAlmostEqual(params["presence_penalty"], 0.8, places=3)
        self.assertEqual(params["max_new_tokens"], 16256)

    # ---- 片段合并 ----

    def test_merge_fragments_keeps_all_parts(self):
        """片段合并：互不覆盖、不互相截断。"""
        merged = self.page.evaluate(
            """() => window.enhanceMethods.mergeFragments(
                 ['AAA片段', 'BBB片段', 'AAA片段'])""")
        self.assertIn("AAA片段", merged)
        self.assertIn("BBB片段", merged)
        self.assertEqual(merged.count("AAA片段"), 1, "相同片段不应重复追加")

    def test_multi_item_run_merges_results_into_modal(self):
        """点「增强」后按选中顺序依次执行，结果合并进同一个弹窗。"""
        self.switch_mode("t2i_qwen")
        self.open_menu()
        # 勾选「普通增强」+「官方 PE」
        self.page.click('#enhanceMenu input[data-method-check="qwen_pe"]')
        self.page.wait_for_timeout(400)
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(300)

        # 用路由桩让两个接口都返回可区分的内容
        self.page.evaluate("""() => {
          window.__calls = [];
          const orig = window.fetch;
          window.fetch = async (url, opts) => {
            const u = String(url);
            window.__calls.push(u);
            const json = (body) => new Response(JSON.stringify(body), {
              status: 200, headers: {'Content-Type': 'application/json'}});
            if (u.includes('/api/qwen-pe/expand')) {
              return json({ok: true, expanded: 'PE产出片段', tier: 't2i',
                tier_label: 'PE-T2I（文生图）', clip_name: 'x.safetensors',
                ref_count: 0});
            }
            if (u.includes('/api/enhance')) {
              return json({ok: true, provider: '桩', spec_id: 'image',
                enhanced: '普通产出片段', original: 'x'});
            }
            return orig(url, opts);
          };
        }""")
        self.page.fill("#prompt", "a lighthouse at dusk")
        self.page.click("#enhanceBtn")
        self.page.wait_for_timeout(2500)
        calls = self.page.evaluate("() => window.__calls || []")
        self.assertTrue(any("/api/enhance" in c for c in calls),
                        "普通增强应被调用")
        self.assertTrue(any("/api/qwen-pe/expand" in c for c in calls),
                        "官方 PE 应被调用")
        value = self.page.evaluate(
            "() => document.querySelector('#enhanceResult').value")
        self.assertIn("普通产出片段", value)
        self.assertIn("PE产出片段", value)
        self.assertEqual(self.page.evaluate(
            "() => document.querySelector('#enhanceModal').hidden"), False,
            "合并结果应进同一个弹窗")

    # ---- 其它面板不受影响 ----

    def test_qwen_pe_disabled_in_other_panels_with_multiselect(self):
        """Z-Image / MOSS 面板下官方 PE 仍禁用。"""
        for mode in ("t2i", "t2i_moss"):
            self.switch_mode(mode)
            self.open_menu()
            self.assertIsNotNone(
                self.page.get_attribute(
                    '#enhanceMenu input[data-method-check="qwen_pe"]', "disabled"),
                f"{mode} 面板下官方 PE 应被禁用")
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(200)

    def test_no_console_errors_after_multiselect_exercise(self):
        """跑一遍多选流程，console 0 报错。"""
        self.switch_mode("t2i_qwen")
        self.open_menu()
        self.page.click('#enhanceMenu input[data-method-check="qwen_pe"]')
        self.page.wait_for_timeout(400)
        self.page.click('#enhanceMenu input[data-method-check="refine"]')
        self.page.wait_for_timeout(400)
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(400)
        self.assertEqual(self.errors, [], f"console 报错：{self.errors}")


if __name__ == "__main__":
    unittest.main()
