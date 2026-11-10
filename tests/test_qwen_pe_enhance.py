# -*- coding: utf-8 -*-
"""「增强」下拉里的 Qwen 官方 PE 扩写 —— 回归测试。

背景：Qwen-Image 2.1 的「官方 PE 提示词扩写」原本是 Qwen 面板上的
**独立勾选框**（#imgUsePe）。按用户要求，它必须并入「增强」按钮的
下拉菜单：用户在「增强」下拉里选中「qwen21图片官方增强PE」，
点「增强」按钮即执行官方 PE 扩写。

本文件把这条约定写死，防止回退：

1. **注册表有第四条** —— id=qwen_pe，中文名「qwen21图片官方增强PE」，
   可用模式是 Qwen 的两个（t2i_qwen / i2i_qwen）。
2. **独立勾选框已移除** —— 前端任何文件里都不得再出现
   #imgUsePe / #imgPeField / #imgPeTier / #imgPeNote。
3. **路由正确** —— 选中该选项后点「增强」，打的是
   POST /api/qwen-pe/expand（不是 /api/enhance）。
4. **回填行为与其它增强方式一致** —— 结果进同一个弹窗、
   同一个「替换/追加」按钮组；替换后提示词框内容逐字符一致、
   标记为「已增强」、可继续编辑。
5. **两个 Qwen 面板都可用** —— 文生图与图片编辑面板下该选项都不禁用；
   其它模型面板（Z-Image / MOSS）下禁用并说明原因。
6. **后端契约** —— /api/qwen-pe/expand 的入参校验、PE 工作流构造
   （官方 checkpoint + 官方 system prompt + 官方采样参数）、
   参考图接线、产物校验。

运行：python3 -m pytest tests/test_qwen_pe_enhance.py -v
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
class TestQwenPeEnhanceSource(unittest.TestCase):
    """前端源码的静态约定（不依赖浏览器）。"""

    def read(self, name: str) -> str:
        return (WEB / name).read_text(encoding="utf-8")

    def test_registry_has_qwen_pe_entry(self):
        """注册表必须有 qwen_pe 这一条，且中文名符合要求。"""
        src = self.read("enhance-menu.js")
        self.assertRegex(src, r'id:\s*"qwen_pe"')
        self.assertIn("qwen21图片官方增强PE", src)

    def test_qwen_pe_modes_are_qwen_only(self):
        """官方 PE 只对 Qwen-Image 2.1 的两个模式开放。"""
        src = self.read("enhance-menu.js")
        block = re.search(r'id:\s*"qwen_pe".*?\n\s*\},', src, re.S)
        self.assertIsNotNone(block, "找不到 qwen_pe 注册项")
        body = block.group(0)
        self.assertIn("t2i_qwen", body)
        self.assertIn("i2i_qwen", body)
        # 不得对 Z-Image / MOSS 开放
        self.assertNotIn("\"t2i\"", body)
        self.assertNotIn("t2i_moss", body)

    def test_no_stale_text_pointing_at_removed_pe_checkbox(self):
        """界面文案不得再指引用户去「面板里关掉 PE 编码器」。

        独立 PE 勾选框已删除，warn 文案若还这么说，用户会去找一个
        不存在的控件 —— 属于死指引，必须一起改掉。

        断言直接打**用户能看到的 warn 字符串**，而不是全文搜源码 ——
        否则源码注释里提到这些词就会误报（这正是本测试第一版的毛病）。
        """
        from h3studio import workflows as W
        qwen_models = [k for k in W.QWEN_IMAGE_MODELS if "qwen_image_2.1" in k]
        self.assertTrue(qwen_models, "找不到 Qwen-Image 2.1 模型定义")
        dead_phrases = ("可在面板里关掉", "可在面板关闭", "面板里关掉 PE", "面板里关掉")
        # 只有「官方 PE 文本编码器自带扩写」那条 warn 需要改写；
        # Turbo 那条讲的是 sigma schedule，不提 PE，不该强加「增强」字样。
        pe_warn_keys = [k for k in qwen_models if "PE" in W.QWEN_IMAGE_MODELS[k].get("warn", "")]
        self.assertTrue(pe_warn_keys, "应至少有一个模型提到官方 PE 扩写行为")
        for key in qwen_models:
            warn = W.QWEN_IMAGE_MODELS[key].get("warn", "")
            self.assertTrue(warn.strip(), f"{key} 的 warn 不应为空")
            for dead in dead_phrases:
                self.assertNotIn(dead, warn,
                                 f"{key} 的 warn 仍指向已删除的 PE 勾选框：{dead}")
        for key in pe_warn_keys:
            warn = W.QWEN_IMAGE_MODELS[key].get("warn", "")
            # 正向：应当把用户引到「增强」下拉里的官方 PE 选项
            self.assertIn("增强", warn, f"{key} 的 warn 应指引到「增强」下拉")
            self.assertIn("qwen21图片官方增强PE", warn,
                          f"{key} 的 warn 应写明官方 PE 下拉项名称")

    def test_no_dead_qwen_pe_tier_helper(self):
        """随独立勾选框一起删掉的档位读取函数不得留成死代码。"""
        app_js = self.read("app.js")
        # 去掉注释后再查 —— 注释里可以叙述「这个函数被删了」
        code_only = re.sub(r"//[^\n]*", "", app_js)
        code_only = re.sub(r"/\*.*?\*/", "", code_only, flags=re.S)
        self.assertNotIn("qwenPeTierInfo", code_only,
                         "qwenPeTierInfo() 已无调用点，应随档位徽标一并删除")
        for token in ("imgUsePe", "imgPeField", "imgPeTier", "imgPeNote"):
            self.assertNotIn(token, code_only,
                             f"app.js 可执行代码里仍残留独立 PE 入口标识 {token}")

    def test_qwen_pe_routes_to_its_own_endpoint(self):
        """选中官方 PE 后必须打 /api/qwen-pe/expand（不是 /api/enhance）。"""
        src = self.read("enhance-menu.js")
        block = re.search(r'async function runQwenPe\(.*?\n  \}', src, re.S)
        self.assertIsNotNone(block, "找不到 runQwenPe")
        self.assertIn("/api/qwen-pe/expand", block.group(0))
        self.assertNotIn("/api/enhance", block.group(0))

    def test_other_methods_still_route_to_enhance_endpoint(self):
        """普通增强 / 二次增强 / 结构化增强的路由没被改坏。"""
        src = self.read("enhance-menu.js")
        for fn, endpoint in (("runBasic", "/api/enhance"),
                             ("runRefine", "/api/enhance"),
                             ("runLayout", "/api/moss-layout")):
            block = re.search(rf'async function {fn}\(.*?\n  \}}', src, re.S)
            self.assertIsNotNone(block, f"找不到 {fn}")
            self.assertIn(endpoint, block.group(0))

    def test_standalone_pe_checkbox_is_removed(self):
        """独立 PE 勾选框必须彻底消失 —— 面板上不再有第二个入口。"""
        for name in ("index.html", "app.js", "providers.js", "enhance-menu.js",
                     "qwen-refs.js", "moss-layout.js", "style.css"):
            src = self.read(name)
            for token in ("imgUsePe", "imgPeField", "imgPeTier", "imgPeNote"):
                self.assertNotIn(token, src,
                                 f"{name} 仍残留独立 PE 入口标识 {token}")

    def test_no_standalone_pe_label_in_html(self):
        """页面上不得再出现「官方 PE 提示词扩写」这个独立勾选框标签。"""
        html = self.read("index.html")
        self.assertNotIn("官方 PE 提示词扩写", html)
        self.assertNotIn('type="checkbox" id="imgUsePe"', html)

    def test_backend_module_and_route_exist(self):
        """后端模块与路由都要在。"""
        self.assertTrue((REPO / "h3studio" / "qwen_pe.py").is_file())
        server = (REPO / "server.py").read_text(encoding="utf-8")
        self.assertIn("/api/qwen-pe/expand", server)
        self.assertIn("api_qwen_pe_expand", server)


# ------------------------------------------------------------------ 后端契约
class TestQwenPeBackend(unittest.TestCase):
    """h3studio/qwen_pe.py 的契约（不连真实模型）。"""

    def setUp(self):
        from h3studio import qwen_pe
        self.pe = qwen_pe

    def test_tier_selection_follows_mode(self):
        """图片编辑 -> i2i 档，文生图 -> t2i 档（官方 checkpoint 不可互换）。"""
        self.assertEqual(self.pe.tier_for(True), "i2i")
        self.assertEqual(self.pe.tier_for(False), "t2i")
        self.assertEqual(self.pe.PE_CLIP_NAMES["t2i"],
                         "qwen3.5_qwen_image_2.1_pe_t2i.int8_convrot.safetensors")
        self.assertEqual(self.pe.PE_CLIP_NAMES["i2i"],
                         "qwen3.5_qwen_image_2.1_pe_i2i.int8_convrot.safetensors")

    def test_official_sampling_params(self):
        """官方采样参数：T2I presence_penalty=1.5/16256；I2I 0/24000。"""
        self.assertEqual(self.pe.pe_params_for(False)["presence_penalty"], 1.5)
        self.assertEqual(self.pe.pe_params_for(False)["max_new_tokens"], 16256)
        self.assertEqual(self.pe.pe_params_for(True)["presence_penalty"], 0.0)
        self.assertEqual(self.pe.pe_params_for(True)["max_new_tokens"], 24000)

    def test_workflow_uses_official_checkpoint_and_prompt(self):
        """工作流必须用官方 PE checkpoint + 官方 system prompt + thinking。"""
        wf = self.pe.build_pe_workflow("a cat", is_edit=False)
        self.assertEqual(wf["pe:clip"]["inputs"]["clip_name"],
                         self.pe.PE_CLIP_NAMES["t2i"])
        self.assertEqual(wf["pe:clip"]["class_type"], "CLIPLoader")
        gen = wf["pe:gen"]["inputs"]
        self.assertEqual(gen["clip"], ["pe:clip", 0])
        self.assertTrue(gen["thinking"], "官方 PE 的 thinking 必须开")
        self.assertTrue(gen["use_default_template"])
        # system prompt 是官方原文（不是空串）
        self.assertTrue(gen["system_prompt"].strip())
        self.assertEqual(len(gen["system_prompt"]),
                         len(self.pe.system_prompt_for(False)))
        # max_length 不能压到官方模板的 4096（会静默返回空串）
        self.assertEqual(gen["max_length"], 16256)
        self.assertEqual(gen["sampling_mode.presence_penalty"], 1.5)
        # 必须有输出节点，否则 ComfyUI 报 prompt_no_outputs
        self.assertIn("pe:out", wf)
        self.assertEqual(wf["pe:out"]["inputs"]["source"], ["pe:gen", 0])

    def test_workflow_wires_reference_images_in_order(self):
        """图片编辑时参考图按顺序喂给 PE（官方 PE-I2I 工作流就是这么接的）。"""
        wf = self.pe.build_pe_workflow("edit", is_edit=True,
                                       ref_images=["h3studio/a.png", "h3studio/b.png"])
        self.assertIn("pe:ref0", wf)
        self.assertIn("pe:ref1", wf)
        self.assertEqual(wf["pe:ref0"]["inputs"]["image"], "h3studio/a.png")
        self.assertEqual(wf["pe:ref1"]["inputs"]["image"], "h3studio/b.png")
        batch = wf["pe:batch"]["inputs"]
        self.assertEqual(batch["images.image0"], ["pe:ref0", 0])
        self.assertEqual(batch["images.image1"], ["pe:ref1", 0])
        self.assertEqual(wf["pe:gen"]["inputs"]["image"], ["pe:batch", 0])

    def test_t2i_workflow_has_no_reference_nodes(self):
        """文生图没有参考图 —— 不能有多余的 BatchImagesNode。"""
        wf = self.pe.build_pe_workflow("a cat", is_edit=False)
        self.assertNotIn("pe:batch", wf)
        self.assertNotIn("image", wf["pe:gen"]["inputs"])

    def test_thinking_strip(self):
        """<think> 残留必须被剥掉（否则思维链进提示词框）。"""
        self.assertEqual(self.pe.strip_thinking("<think>abc</think>real text"), "real text")
        self.assertEqual(self.pe.strip_thinking("<think>never closed"), "")
        self.assertEqual(self.pe.strip_thinking("plain text"), "plain text")

    def test_output_validation_rejects_unusable(self):
        """空 / 过短 / 与原文一致 -> 明确报错，不返回半成品。"""
        with self.assertRaises(self.pe.PEError):
            self.pe.validate_pe_output("", "a cat")
        with self.assertRaises(self.pe.PEError):
            self.pe.validate_pe_output("too short", "a cat")
        with self.assertRaises(self.pe.PEError):
            self.pe.validate_pe_output("a cat", "a cat")
        # 正常产物放行
        good = "A warm photograph of an orange cat sitting on a wooden windowsill."
        self.assertEqual(self.pe.validate_pe_output(good, "a cat"), good)

    def test_extract_pe_text_takes_the_right_node(self):
        """必须按节点 id 精确取，不能遍历取最长。"""
        outputs = {"pe:out": {"text": ["the real expansion"]},
                   "other": {"text": ["a much much much longer string that is not ours"]}}
        self.assertEqual(self.pe.extract_pe_text(outputs), "the real expansion")
        self.assertEqual(self.pe.extract_pe_text({}), "")


# ------------------------------------------------------------------ 浏览器验证
@unittest.skipUnless(HAS_PLAYWRIGHT, "需要 playwright（chromium）")
class TestQwenPeEnhanceBrowser(unittest.TestCase):
    """真实浏览器：下拉选项、路由、回填、无独立勾选框、console 0 报错。"""

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

    def reset_page(self):
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

    def switch_mode(self, mode):
        self.page.click(f'.nav-item[data-mode="{mode}"]')
        self.page.wait_for_timeout(900)

    def open_menu(self):
        self.page.click("#enhanceMenuBtn")
        self.page.wait_for_timeout(400)

    def menu_items(self):
        return self.page.eval_on_selector_all(
            "#enhanceMenu .enhance-menu-item[data-method]",
            "els => els.map(e => {const c = e.querySelector('input[data-method-check]');"
            " return {id: e.dataset.method,"
            " label: e.querySelector('.enhance-menu-label').textContent.trim(),"
            " hint: (e.querySelector('.enhance-menu-hint')||{}).textContent||'',"
            " badges: Array.from(e.querySelectorAll('.enhance-menu-badge'))"
            "          .map(b => b.textContent.trim()),"
            " checked: !!(c && c.checked), disabled: !!(c && c.disabled)};})")


    def select_only_qwen_pe(self):
        """只勾选官方 PE（取消其它已勾项）。

        【多选语义】下拉默认勾了「普通增强」。要测「只跑官方 PE」，
        必须先把其它项取消勾 —— 否则多项都会被执行。
        """
        self.open_menu()
        # 顺序很重要：**先**勾上官方 PE，再取消其它项。
        # 因为界面不允许取消到「一项都不剩」（那会让「增强」按钮无事可做），
        # 所以先取消普通增强是取消不掉的。
        self.page.click('#enhanceMenu input[data-method-check="qwen_pe"]')
        self.page.wait_for_timeout(250)
        # 取消其余项：用 evaluate 直接改 checked 并派发 change 事件
        # （菜单每次勾选都会重渲染 -> 旧的 element handle 会失效）。
        for mid in ("basic", "refine"):
            self.page.evaluate("""(mid) => {
              const el = document.querySelector(
                `#enhanceMenu input[data-method-check="${mid}"]`);
              if (el && el.checked && !el.disabled) {
                el.checked = false;
                el.dispatchEvent(new Event('change', {bubbles: true}));
              }
            }""", mid)
            self.page.wait_for_timeout(250)
        self.page.wait_for_timeout(150)
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(200)

    # ---- 下拉选项 ----

    def test_dropdown_lists_four_methods(self):
        """下拉里同时有：普通增强 / 二次增强 / 结构化增强 / 官方PE。

        【2026-11-10 架构裁决】统一注册表现在有 **5** 项 ——
        多了「YuE2 结构化风格 + 歌词」（kind: "yue2-struct"）。
        它在**图像/视频面板是禁用的**（写明「YuE2 音乐专属，本面板不可用」），
        但仍会出现在同一个下拉里 —— 这样用户能看到「音乐增强是什么」，
        而不是完全看不到这一项。
        所以本用例的判据从「恰好 4 项」放宽为
        「4 项图像/视频相关的都在，且多出来的音乐项是禁用的」。
        """
        self.switch_mode("t2i_qwen")
        self.open_menu()
        items = self.menu_items()
        ids = [i["id"] for i in items]
        # 图像/视频面板应有的 4 项（顺序即注册表顺序）
        for want in ("basic", "refine", "layout", "qwen_pe"):
            self.assertIn(want, ids, f"下拉应包含 {want}：{ids}")
        # 顺序：basic -> refine -> layout -> qwen_pe（yue2_struct 插在 layout 之后）
        self.assertEqual(
            [i for i in ids if i != "yue2_struct"],
            ["basic", "refine", "layout", "qwen_pe"],
            f"图像/视频面板的 4 项顺序应为注册表顺序：{ids}")
        # 音乐项在图像/视频面板必须禁用（复选框 disabled），
        # 并在菜单项上写明归属。
        yue2 = [i for i in items if i["id"] == "yue2_struct"]
        self.assertTrue(yue2, f"下拉应包含 yue2_struct 项：{ids}")
        self.assertTrue(yue2[0]["disabled"],
                        "yue2_struct 在图像/视频面板应禁用（复选框 disabled）")
        # 归属文案在菜单项的徽标里
        badge = self.page.eval_on_selector(
            '#enhanceMenu .enhance-menu-item[data-method="yue2_struct"]',
            "e => (e.querySelector('.enhance-menu-badge')||{}).textContent||''")
        self.assertIn("音乐专属", badge,
                      f"yue2_struct 应写明是音乐专属：{badge!r}")
        labels = [i["label"] for i in items]
        self.assertIn("普通增强", labels)
        self.assertIn("二次增强", labels)
        self.assertIn("结构化增强（layout）", labels)
        self.assertIn("qwen21图片官方增强PE", labels)
        # 每个项都要有简短说明
        for item in items:
            self.assertTrue(item["hint"].strip(),
                            f"{item['id']} 缺少说明文字")

    def test_qwen_pe_enabled_in_both_qwen_panels(self):
        """文生图与图片编辑面板下官方 PE 都可用。"""
        for mode in ("t2i_qwen", "i2i_qwen"):
            self.switch_mode(mode)
            self.open_menu()
            disabled = self.page.get_attribute(
                '#enhanceMenu input[data-method-check="qwen_pe"]', "disabled")
            self.assertIsNone(disabled, f"{mode} 面板下官方 PE 应可用")
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(200)

    def test_qwen_pe_disabled_in_other_panels(self):
        """Z-Image / MOSS 面板下官方 PE 禁用（官方 checkpoint 是 Qwen 专用的）。"""
        for mode in ("t2i", "t2i_moss"):
            self.switch_mode(mode)
            self.open_menu()
            self.assertIsNotNone(
                self.page.get_attribute(
                    '#enhanceMenu input[data-method-check="qwen_pe"]', "disabled"),
                f"{mode} 面板下官方 PE 应被禁用")
            # 其它方式仍可用
            for other in ("basic", "refine"):
                self.assertIsNone(
                    self.page.get_attribute(
                        f'#enhanceMenu input[data-method-check="{other}"]', "disabled"),
                    f"{mode} 面板下 {other} 不该被禁用")
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(200)

    def test_selecting_qwen_pe_changes_button_label(self):
        """
        选中后主按钮文字随之变化。

        【多选语义】官方 PE 是复选项：勾上后它会**追加**到已选项里
        （普通增强默认已勾）。此时按钮文字按「选中了几项」走：
        多项 -> 「增强（N 项）」。这里断言的是「PE 项已生效且被选中」，
        而不是旧单选下的「current == qwen_pe」。
        """
        self.switch_mode("t2i_qwen")
        self.open_menu()
        self.page.click('#enhanceMenu input[data-method-check="qwen_pe"]')
        self.page.wait_for_timeout(400)
        selected = self.page.evaluate(
            "() => window.enhanceMethods.getSelected()")
        self.assertIn("qwen_pe", selected, "官方 PE 应处于选中状态")
        btn = self.page.inner_text("#enhanceBtnText")
        self.assertTrue("PE" in btn or "项" in btn,
                        f"按钮文字应反映选中状态，实际：{btn}")
        # 【统一增强入口】模板下拉已删除（它并入了「增强」下拉里的模板选择器）——
        # 断言这里确实只有一个增强入口。
        self.assertIsNone(self.page.query_selector("#enhanceSpec"),
                          "不该再有第二个增强下拉")
        # 官方 PE 项在菜单里带自己的模板标注
        self.page.click("#enhanceMenuBtn")
        self.page.wait_for_timeout(300)
        item = self.page.inner_text('#enhanceMenu .enhance-menu-item[data-method="qwen_pe"]')
        self.assertIn("官方 PE", item)
        # 版本历史按钮是共用的，仍可见
        self.assertTrue(self.page.is_visible("#promptVersionBtn"))

    def test_switching_mode_falls_back_when_method_unavailable(self):
        """
        在 Qwen 面板选了官方 PE，切到 Z-Image ->
        当前方式不可用 -> 必须剔除不可用的项（否则点「增强」没反应）。

        【多选语义】不再是「回落到普通增强」这一个，而是**剔除**不可用项；
        剩下的仍是可执行的（这里普通增强默认已勾，所以还在）。
        """
        self.switch_mode("t2i_qwen")
        self.open_menu()
        self.page.click('#enhanceMenu input[data-method-check="qwen_pe"]')
        self.page.wait_for_timeout(300)
        self.assertIn("qwen_pe", self.page.evaluate(
            "() => window.enhanceMethods.getSelected()"))
        self.switch_mode("t2i")
        self.page.wait_for_timeout(600)
        selected = self.page.evaluate(
            "() => window.enhanceMethods.getSelected()")
        self.assertNotIn("qwen_pe", selected,
                         "切到不可用模式后官方 PE 应被剔除")
        self.assertIn("basic", selected,
                      "剩下的项应仍可执行（普通增强默认已勾）")

    # ---- 无独立勾选框 ----

    def test_no_standalone_pe_checkbox_in_dom(self):
        """两个 Qwen 面板下都不再有独立的 PE 勾选框。"""
        for mode in ("t2i_qwen", "i2i_qwen"):
            self.switch_mode(mode)
            info = self.page.evaluate("""() => ({
              hasCheckbox: !!document.querySelector('#imgUsePe'),
              hasField: !!document.querySelector('#imgPeField'),
              checkboxCount: Array.from(
                document.querySelectorAll('#imagePanel input[type=checkbox]'))
                .map(i => i.id),
              peText: (document.body.innerText || '').includes('官方 PE 提示词扩写'),
            })""")
            self.assertFalse(info["hasCheckbox"],
                             f"{mode} 下仍有独立 PE 勾选框")
            self.assertFalse(info["hasField"],
                             f"{mode} 下仍有独立 PE 区块")
            self.assertNotIn("imgUsePe", info["checkboxCount"])
            self.assertFalse(info["peText"],
                             f"{mode} 下页面仍出现「官方 PE 提示词扩写」文案")

    # ---- 路由 + 回填 ----

    def _stub_pe_endpoint(self, expanded="A warm photograph of an orange tabby cat."):
        self.page.evaluate("""(expanded) => {
          window.__calls = [];
          window.__bodies = [];
          const orig = window.fetch;
          window.fetch = async (url, opts) => {
            const u = String(url);
            window.__calls.push(u);
            if (opts && opts.body) window.__bodies.push(String(opts.body));
            if (u.includes('/api/qwen-pe/expand')) {
              const body = {ok:true, original:'a cat', expanded: expanded,
                            tier:'t2i', tier_label:'PE-T2I（文生图）',
                            clip_name:'qwen3.5_qwen_image_2.1_pe_t2i.int8_convrot.safetensors',
                            ref_count:0, mode:'t2i_qwen'};
              return new Response(JSON.stringify(body),
                                 {status:200, headers:{'Content-Type':'application/json'}});
            }
            return orig(url, opts);
          };
        }""", expanded)

    def test_qwen_pe_calls_its_own_endpoint(self):
        """选中官方 PE 后点「增强」-> 打 /api/qwen-pe/expand（不是 /api/enhance）。"""
        self.switch_mode("t2i_qwen")
        self.select_only_qwen_pe()
        self.page.fill("#prompt", "a cat")
        self._stub_pe_endpoint()

        self.page.click("#enhanceBtn")
        self.page.wait_for_selector("#enhanceModal:not([hidden])", timeout=20000)
        calls = self.page.evaluate("window.__calls")
        self.assertTrue(any("/api/qwen-pe/expand" in c for c in calls),
                        f"应打 /api/qwen-pe/expand，实际请求：{calls}")
        self.assertFalse(any("/api/enhance" in c for c in calls),
                         f"不应打 /api/enhance，实际请求：{calls}")

    def test_qwen_pe_result_uses_same_modal_and_fills_prompt(self):
        """
        回填语义与其它增强方式一致：
          - 结果进同一个弹窗（#enhanceModal / #enhanceResult）
          - 点「替换提示词框」-> 提示词框内容逐字符一致
          - 标记为「已增强」
          - 之后还能继续编辑
        """
        self.switch_mode("t2i_qwen")
        self.select_only_qwen_pe()
        self.page.fill("#prompt", "a cat")
        expanded = "A warm photograph of an orange tabby cat on a wooden windowsill."
        self._stub_pe_endpoint(expanded)

        self.page.click("#enhanceBtn")
        self.page.wait_for_selector("#enhanceModal:not([hidden])", timeout=20000)
        self.assertEqual(self.page.input_value("#enhanceResult"), expanded)
        # 弹窗的 meta 要说明用的是官方 PE
        self.assertIn("PE-T2I", self.page.inner_text("#enhanceMeta"))

        # 回填
        self.page.click("#enhanceReplace")
        self.page.wait_for_timeout(400)
        self.assertEqual(self.page.input_value("#prompt").strip(), expanded.strip())
        # 标记为「已增强」
        self.assertTrue(self.page.is_visible("#promptEnhancedBadge"))
        # 可继续编辑
        self.page.fill("#prompt", expanded + " 再加一点细节")
        self.page.wait_for_timeout(200)
        self.assertIn("再加一点细节", self.page.input_value("#prompt"))

    def test_qwen_pe_disable_double_expansion_on_generate(self):
        """
        手动跑过官方 PE 并**写回提示词框**后，出图时不再二次扩写
        （提示词框里已经是官方 PE 结果，再扩会冲掉用户的编辑）。
        """
        self.switch_mode("t2i_qwen")
        self.select_only_qwen_pe()
        self.page.fill("#prompt", "a cat")
        self._stub_pe_endpoint()
        self.page.click("#enhanceBtn")
        self.page.wait_for_selector("#enhanceModal:not([hidden])", timeout=20000)
        # 只是弹窗里预览、还没写回 -> 不应改变出图语义
        self.assertTrue(self.page.evaluate("() => state.imgQwen.usePe"),
                        "还没写回提示词框时，出图仍应走官方 PE 接线")
        # 写回之后才关掉二次扩写
        self.page.click("#enhanceReplace")
        self.page.wait_for_timeout(400)
        self.assertFalse(self.page.evaluate("() => state.imgQwen.usePe"),
                         "写回官方 PE 结果后，出图不应再二次扩写")

    def test_qwen_pe_failure_shows_error_not_silent(self):
        """官方 PE 失败必须明确报错，提示词框不被改动。"""
        self.switch_mode("t2i_qwen")
        self.select_only_qwen_pe()
        self.page.fill("#prompt", "a cat")
        self.page.evaluate("""() => {
          window.__alerts = [];
          window.alert = (msg) => { window.__alerts.push(String(msg)); };
          window.fetch = async (url, opts) => {
            if (String(url).includes('/api/qwen-pe/expand')) {
              return new Response(JSON.stringify({
                error: 'ComfyUI 执行官方 PE 失败：CUDA out of memory',
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
        self.assertIn("官方 PE", alerts[0])
        # 提示词框没被改动
        self.assertEqual(self.page.input_value("#prompt").strip(), "a cat")

    def test_i2i_panel_sends_reference_images_to_pe(self):
        """图片编辑面板：参考图要一起送去官方 PE（官方 PE-I2I 工作流接法）。"""
        self.switch_mode("i2i_qwen")
        self.select_only_qwen_pe()
        self.page.fill("#prompt", "把茶壶改成橙色")
        # 造一个假的参考图（前端只关心 payload 形状）
        self.page.evaluate("""() => {
          window.__calls = [];
          window.__bodies = [];
          const orig = window.fetch;
          window.fetch = async (url, opts) => {
            const u = String(url);
            window.__calls.push(u);
            if (opts && opts.body) window.__bodies.push(String(opts.body));
            if (u.includes('/api/qwen-pe/expand')) {
              const body = {ok:true, original:'x',
                            expanded:'An edited directive mentioning <image1>.',
                            tier:'i2i', tier_label:'PE-I2I（图片编辑）',
                            clip_name:'qwen3.5_qwen_image_2.1_pe_i2i.int8_convrot.safetensors',
                            ref_count:1, mode:'i2i_qwen'};
              return new Response(JSON.stringify(body),
                                 {status:200, headers:{'Content-Type':'application/json'}});
            }
            return orig(url, opts);
          };
          // 塞一个参考图进 QwenRefs（模拟用户从素材库加了一张）
          if (window.QwenRefs) {
            window.QwenRefs.setRefs([{asset_id:'asset_test', version:1,
              name:'teapot.png', description:'白色陶瓷茶壶',
              description_source:'user', tags:[], created_at:1,
              preview_url:'', missing:false}]);
          }
        }""")
        self.page.click("#enhanceBtn")
        self.page.wait_for_selector("#enhanceModal:not([hidden])", timeout=20000)
        bodies = self.page.evaluate("window.__bodies")
        pe_bodies = [b for b in bodies if "/api/qwen-pe/expand" in b or "asset_test" in b]
        self.assertTrue(pe_bodies, "应发出带参考图的请求")
        payload = json.loads(pe_bodies[-1])
        self.assertEqual(payload["mode"], "i2i_qwen")
        self.assertEqual(len(payload["refs"]), 1)
        self.assertEqual(payload["refs"][0]["asset_id"], "asset_test")

    def test_no_console_errors_after_menu_exercise(self):
        """走完一遍交互后没有 console 报错。"""
        self.assertEqual(self.errors, [], f"console 报错：{self.errors}")


if __name__ == "__main__":
    unittest.main()
