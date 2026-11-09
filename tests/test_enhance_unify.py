# -*- coding: utf-8 -*-
"""统一增强体系 —— 回归测试。

用户要求（2026-11-09）：
  1. 每个功能只有**一种**增强：文生图 / 图片编辑 / 文生音乐 / 参考生音乐 /
     文生视频等各面板的增强入口，只提供**该功能自己对应的增强能力**
     （Z-Image 自身增强、Qwen-Image 2.1 官方 PE 扩写、MOSS-Image 结构化增强、
     yue2 专用增强）。不允许在同一个面板出现两套/多个语义重叠的增强下拉。
  2. **二次增强是通用增强**：对当前提示词中的每一个物品逐一做细节增强，
     属于跨功能通用能力，应作为统一的一项呈现，不得与功能专属增强重复成两个入口。
  3. MOSS-Image 面板的增强**只有结构化增强**（该能力归 MOSS 自己使用）。
  4. 移除 Z-Image 文生图中重复的第二个「各种增强方式」下拉；
     所有面板统一为「一个增强按钮 + 一个下拉（列出可用增强方式）
     + 明确显示当前所选增强方式」。
  5. 已有能力不得回退：Qwen 官方 PE、MOSS 结构化增强、二次增强、普通增强
     等功能都仍可从统一下拉里选到。

本文件把上面 5 条写死，防止回退：

A. **后端契约**（不依赖浏览器）
   - 模板归属：yue2-style / music-refine 属于音乐（families=yue2 / audio）
   - 二次增强按媒体类型选模板（图像/视频 = image-refine，音频 = music-refine）
   - /api/enhancer 下发 refine_specs / refine_spec_by_media
   - 音乐模式进入 mode_media / mode_filters / mode_defaults

B. **前端静态契约**
   - 旧的第二个下拉 #enhanceSpec 及其渲染函数彻底消失
   - 增强注册表里没有另起一套平行的增强项列表
   - 「当前所选增强方式」回显元素存在

C. **浏览器行为**（playwright + chromium，缺失时整类 skip）
   - 每个面板只有一个增强入口
   - 二次增强在所有面板都可选（通用能力）
   - 功能专属增强只在本功能面板可用
   - 音乐面板有自己的增强入口且能列出 YuE2 专属模板
   - console 0 报错

运行：python3 -m pytest tests/test_enhance_unify.py -v
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

from h3studio import enhancer as enh

WEB = REPO / "web"


# ------------------------------------------------------------------ A. 后端契约
class TestEnhanceOwnership(unittest.TestCase):
    """增强能力的归属（后端唯一真源：h3studio/enhancer.py）。"""

    def test_music_templates_exist_and_are_audio_only(self):
        """yue2 专用增强 + 音乐版二次增强 —— 属于音频媒体、yue2 模型族。"""
        yue2 = enh.TEMPLATE_SPECS["yue2-style"]
        self.assertEqual(yue2["media"], "audio")
        self.assertIn("yue2", yue2["families"])

        music_refine = enh.TEMPLATE_SPECS["music-refine"]
        self.assertEqual(music_refine["media"], "audio")
        self.assertTrue(music_refine.get("refine"), "music-refine 应标记为二次增强档")

    def test_music_template_files_exist(self):
        """模板正文文件必须在（缺文件时后端会回落到内置兜底，内容就变了）。"""
        for spec_id in ("yue2-style", "music-refine"):
            spec = enh.TEMPLATE_SPECS[spec_id]
            path = Path(enh.SKILL_DIR) / "assets" / str(spec["template"])
            self.assertTrue(path.exists(), f"{spec_id} 的模板文件缺失：{path}")
            body = path.read_text(encoding="utf-8")
            self.assertIn("{prompt}", body,
                          f"{spec_id} 模板正文必须含 {{prompt}} 占位符")

    def test_music_modes_are_registered(self):
        """文生音乐 / 参考生音乐进 mode_media / mode_filters / mode_defaults。"""
        for mode in ("t2a", "a2a"):
            self.assertEqual(enh.MODE_MEDIA.get(mode), "audio",
                             f"{mode} 的媒体类型应是 audio")
            self.assertEqual(enh.MODE_DEFAULT_SPEC.get(mode), "yue2-style",
                             f"{mode} 的默认模板应是 yue2-style")
            ids = [s["id"] for s in enh.filter_specs(enh.list_specs(), mode)]
            self.assertIn("yue2-style", ids, f"{mode} 应能列出 yue2-style")
            self.assertIn("music-refine", ids, f"{mode} 应能列出 music-refine")

    def test_refine_template_follows_media_type(self):
        """二次增强是通用能力，但模板按媒体类型走。"""
        self.assertEqual(enh.refine_spec_for_mode("t2i"), "image-refine")
        self.assertEqual(enh.refine_spec_for_mode("t2i_qwen"), "image-refine")
        self.assertEqual(enh.refine_spec_for_mode("i2i_qwen"), "image-refine")
        self.assertEqual(enh.refine_spec_for_mode("t2v"), "image-refine")
        self.assertEqual(enh.refine_spec_for_mode("t2a"), "music-refine")
        self.assertEqual(enh.refine_spec_for_mode("a2a"), "music-refine")
        # 未知模式退回图像档（保持历史默认行为）
        self.assertEqual(enh.refine_spec_for_mode(""), "image-refine")

    def test_function_specific_templates_stay_scoped(self):
        """功能专属增强不得外溢到别的功能。"""
        self.assertEqual(enh.TEMPLATE_SPECS["image"]["families"], ["z_image"])
        self.assertEqual(enh.TEMPLATE_SPECS["moss-image"]["families"], ["moss_image"])
        self.assertEqual(enh.TEMPLATE_SPECS["qwen21-pe-t2i"]["families"], ["qwen_image21"])
        self.assertEqual(enh.TEMPLATE_SPECS["qwen21-pe-i2i"]["families"], ["qwen_image21"])

    def test_existing_capabilities_not_regressed(self):
        """规则 5：已有能力都还在。"""
        for spec_id in ("image", "image-refine", "image-scalist", "image-bbox",
                        "qwen21-pe-t2i", "qwen21-pe-i2i", "moss-image", "video"):
            self.assertIn(spec_id, enh.TEMPLATE_SPECS, f"{spec_id} 模板丢失")


# ------------------------------------------------------------------ B. 前端静态契约
class TestEnhanceFrontendSource(unittest.TestCase):
    """前端源码的静态约定（不依赖浏览器）。"""

    def read(self, name: str) -> str:
        return (WEB / name).read_text(encoding="utf-8")

    def test_second_dropdown_removed_everywhere(self):
        """规则 4：重复的第二个增强下拉彻底删除。"""
        for name in ("index.html", "app.js", "providers.js", "enhance-menu.js"):
            src = self.read(name)
            self.assertNotIn('id="enhanceSpec"', src,
                             f"{name} 仍有 #enhanceSpec（第二个增强下拉）")
            self.assertNotIn("renderEnhanceSpecSelect", src,
                             f"{name} 仍在渲染那个下拉")
            self.assertNotIn('$("#enhanceSpec")', src,
                             f"{name} 仍在引用那个下拉")

    def test_current_method_indicator_exists(self):
        """规则 4：明确显示当前所选增强方式。"""
        html = self.read("index.html")
        self.assertIn('id="enhanceCurrent"', html)
        self.assertIn('id="musicEnhanceCurrent"', html)
        src = self.read("enhance-menu.js")
        self.assertIn("updateCurrentIndicator", src)

    def test_music_panel_has_its_own_enhance_entry(self):
        """文生音乐 / 参考生音乐要有增强入口（之前完全没有）。"""
        html = self.read("index.html")
        for el in ("musicEnhanceBar", "musicEnhanceBtn", "musicEnhanceMenuBtn",
                   "musicEnhanceMenu", "musicEnhanceHint"):
            self.assertIn(f'id="{el}"', html, f"音乐面板缺 #{el}")
        js = self.read("enhance-menu.js")
        self.assertIn("isMusicContext", js)
        self.assertIn("#musicStyle", js)

    def test_no_parallel_enhance_item_list(self):
        """不得另起一套平行的增强项列表（第二套增强入口）。"""
        src = self.read("enhance-menu.js")
        for token in ("enhanceList", "enhanceWays", "enhanceTypeSelect",
                      "enhanceMethodSelect"):
            self.assertNotIn(token, src, f"出现了平行的增强项列表：{token}")

    def test_registry_is_single_source(self):
        """增强方式注册表是唯一真源。"""
        src = self.read("enhance-menu.js")
        self.assertIn("const ENHANCE_METHODS", src)
        # 四种能力都要在注册表里
        for mid in ('id: "basic"', 'id: "refine"', 'id: "layout"', 'id: "qwen_pe"'):
            self.assertIn(mid, src, f"注册表缺 {mid}")

    def test_refine_spec_not_hardcoded_to_image(self):
        """二次增强的模板不能再硬编码成 image-refine（音乐要用 music-refine）。"""
        src = self.read("enhance-menu.js")
        self.assertNotIn('spec_id: "image-refine"', src,
                         "二次增强模板被硬编码成 image-refine")
        self.assertIn("refineSpecId", src)


# ------------------------------------------------------------------ C. 浏览器行为
@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright/chromium 缺失，跳过浏览器行为测试")
class TestEnhanceUnifyBrowser(unittest.TestCase):
    """真实浏览器里的统一增强行为。"""

    @classmethod
    def setUpClass(cls):
        from test_frontend_defaults import StudioServer
        cls.server = StudioServer()
        assert cls.server.start(), "Studio 实例启动失败"
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
        """清掉上一次用例留下的增强选择（localStorage 是持久化的，会串味）。"""
        self.page.evaluate(
            "() => { try { localStorage.removeItem('h3studio.enhanceSelection.v1'); }"
            "catch (e) {} }")
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_timeout(1200)

    def switch_mode(self, mode):
        self.page.evaluate(
            "(m) => { const el = document.querySelector(`.nav-item[data-mode='${m}']`);"
            "if (el) el.click(); }", mode)
        self.page.wait_for_timeout(600)

    def open_menu(self):
        """打开当前上下文的增强下拉（音乐面板是另一套 id）。"""
        self.page.evaluate("""() => {
          const music = (state.mode === 't2a' || state.mode === 'a2a');
          const id = music ? '#musicEnhanceMenuBtn' : '#enhanceMenuBtn';
          const b = document.querySelector(id);
          if (b) b.click();
        }""")
        self.page.wait_for_timeout(400)

    def click_menu_item(self, method):
        """
        点下拉里某一项的复选框。

        下拉是**绝对定位**在按钮下方，可能落在视口之外 ——
        直接 page.click() 会因为「元素不可见」而超时（Playwright 会滚动，
        但绝对定位元素滚动后仍可能不可见）。
        所以这里显式 scrollIntoView + 用 JS 触发点击。
        """
        self.page.evaluate(
            """(sel) => {
              const el = document.querySelector(sel);
              if (!el) return false;
              el.scrollIntoView({block: 'center'});
              el.click();
              return true;
            }""",
            self.menu_sel(method))
        self.page.wait_for_timeout(400)

    def menu_sel(self, method):
        music = self.page.evaluate("state.mode === 't2a' || state.mode === 'a2a'")
        prefix = "#musicEnhanceMenu" if music else "#enhanceMenu"
        return f'{prefix} input[data-method-check="{method}"]'

    def test_only_one_enhance_entry_per_panel(self):
        """规则 4：一个面板只有一个增强入口（一个按钮 + 一个下拉）。"""
        for mode in ("t2i", "t2i_qwen", "i2i_qwen", "t2i_moss", "t2v", "t2a", "a2a"):
            self.switch_mode(mode)
            visible = self.page.evaluate("""() => {
              const out = [];
              document.querySelectorAll('.enhance-split').forEach(s => {
                const r = s.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) out.push(s.id);
              });
              return out;
            }""")
            self.assertEqual(len(visible), 1,
                             f"{mode} 面板应只有 1 个增强入口，实际 {visible}")

    def test_refine_is_universal(self):
        """规则 2：二次增强在每个面板都能选。"""
        for mode in ("t2i", "t2i_qwen", "i2i_qwen", "t2i_moss", "t2v", "t2a", "a2a"):
            self.switch_mode(mode)
            self.open_menu()
            disabled = self.page.get_attribute(self.menu_sel("refine"), "disabled")
            self.assertIsNone(disabled, f"{mode} 面板下二次增强不该被禁用")
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(150)

    def test_scoped_enhance_only_in_own_panel(self):
        """规则 1 + 3：功能专属增强只在本功能面板可用。"""
        cases = [
            ("t2i_moss", "layout", True),
            ("t2i", "layout", False),
            ("t2i_qwen", "layout", False),
            ("i2i_qwen", "layout", False),
            ("t2i_qwen", "qwen_pe", True),
            ("i2i_qwen", "qwen_pe", True),
            ("t2i", "qwen_pe", False),
            ("t2i_moss", "qwen_pe", False),
        ]
        for mode, method, should_enable in cases:
            self.switch_mode(mode)
            self.open_menu()
            disabled = self.page.get_attribute(self.menu_sel(method), "disabled")
            if should_enable:
                self.assertIsNone(disabled, f"{mode} 面板下 {method} 应可用")
            else:
                self.assertIsNotNone(disabled, f"{mode} 面板下 {method} 应被禁用")
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(150)

    def test_moss_panel_has_structured_enhance(self):
        """
        规则 3：MOSS 面板的结构化增强可用。

        多选语义：默认「普通增强」已勾上，所以这里先取消它，
        再单独勾「结构化增强」—— 这样当前方式就是 layout，
        按钮文字也变成该项的名字。
        """
        self.switch_mode("t2i_moss")
        self.open_menu()
        disabled = self.page.get_attribute(self.menu_sel("layout"), "disabled")
        self.assertIsNone(disabled, "MOSS 面板的结构化增强应可用")
        # 取消默认勾选的「普通增强」
        self.click_menu_item("basic")
        self.open_menu()
        self.click_menu_item("layout")
        selected = self.page.evaluate("window.enhanceMethods.getSelected()")
        self.assertIn("layout", selected,
                      f"MOSS 面板应能选中结构化增强，实际 {selected}")
        # 只剩结构化增强时，当前方式就是它
        others = [s for s in selected if s != "layout"]
        for other in others:
            self.click_menu_item(other)
            self.open_menu()
        selected = self.page.evaluate("window.enhanceMethods.getSelected()")
        self.assertEqual(selected, ["layout"],
                         f"MOSS 面板应只选中结构化增强，实际 {selected}")
        self.assertEqual(self.page.evaluate("window.enhanceMethods.current"), "layout")
        self.assertIn("结构化增强", self.page.inner_text("#enhanceBtnText"))
        # 意图补充栏随之出现
        self.assertTrue(self.page.is_visible("#mossLayoutBar"))

    def test_music_panel_lists_yue2_templates(self):
        """音乐面板的「普通增强」列 YuE2 专属模板，不列图像模板。"""
        self.switch_mode("t2a")
        self.open_menu()
        opts = self.page.evaluate("""() => {
          const menu = document.querySelector('#musicEnhanceMenu');
          if (!menu) return null;
          const sel = menu.querySelector('select[data-spec-method="basic"]');
          if (!sel) return null;
          return Array.from(sel.options).map(o => o.textContent);
        }""")
        self.assertIsNotNone(opts, "音乐面板的普通增强项应有模板选择器")
        joined = " | ".join(opts)
        self.assertIn("YuE2", joined, f"音乐面板应列出 YuE2 模板，实际：{joined}")
        self.assertNotIn("Z-Image", joined, f"音乐面板不该列图像模板，实际：{joined}")

    def test_music_refine_uses_music_template(self):
        """音乐模式的二次增强用 music-refine（不是画面物体向的模板）。"""
        self.switch_mode("t2a")
        spec = self.page.evaluate("window.enhanceMethods.getRefineSpecId()")
        self.assertEqual(spec, "music-refine")

    def test_current_method_shown(self):
        """规则 4：明确显示当前所选增强方式。"""
        self.switch_mode("t2i")
        self.open_menu()
        self.click_menu_item("refine")
        note = self.page.inner_text("#enhanceCurrent")
        self.assertIn("二次增强", note)
        self.assertIn("物体细化", note)

    def test_no_console_errors(self):
        """跑完上面所有交互，console 0 报错。"""
        for mode in ("t2i", "t2i_qwen", "t2i_moss", "t2v", "t2a"):
            self.switch_mode(mode)
            self.open_menu()
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(200)
        self.assertEqual(self.errors, [], f"console 报错：{self.errors}")


if __name__ == "__main__":
    unittest.main()
