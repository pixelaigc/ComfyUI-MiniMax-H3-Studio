# -*- coding: utf-8 -*-
"""综合审查第 3 轮 —— 统一增强体系分支（local/feature/enh-unify）。

本文件把本轮**架构裁决**与**审查项**写死成契约，防止回退。

【已定架构裁决】音乐增强走「结构化风格 + 歌词」（yue2-flow 路线）
    契约 A：音乐面板的增强写回**不再**硬编码 `#musicStyle` / `#prompt`；
            每种增强方式声明自己的写回目标与形态，缺目标必须 **fail-closed**。
    契约 B：`targets()` 的 input 抽象能从「一个 textarea」扩展到
            「一组结构化字段」。

【审查 P1-2】双状态并存
    契约 C：模板选择状态**只有一处来源**（selectedSpecByMethod）；
            provState.specId 只服务「本地模型配置」页，且模式切换时失效。
    契约 D：跨模式来回切换**不会**命中别的模式的模板 id。

【审查 P1-2】并发闸门重复定义
    契约 E：ENHANCE_CONCURRENCY 在 server.py 里**只有一处定义**，
            且取值与理由写在定义处。

【审查 P0-3】行尾
    契约 F：CRLF 文件的 blob 与工作区**都是 CRLF**，零 LF 噪声。

运行：python3 -m pytest tests/test_enhance_architecture_fixes.py -v
需要：playwright（chromium）。缺失时整类 skip，不算失败。
"""
import re
import subprocess
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
SHARED_JS = WEB / "enhance-menu-shared.js"
MUSIC_JS = WEB / "music-enhance.js"
MUSIC_WEB_JS = WEB / "music.js"
PROVIDERS_JS = WEB / "providers.js"
INDEX_HTML = WEB / "index.html"
SERVER = REPO / "server.py"

#: 这些文件在仓库里是 CRLF blob（历史遗留，行尾策略见 .gitattributes）
CRLF_FILES = ["server.py", "web/index.html", "web/app.js", "web/style.css"]


def strip_js_comments(text: str) -> str:
    """去掉 JS 行注释与块注释 —— 只留真实代码。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args],
                          capture_output=True, text=True)


# =====================================================================
# 契约 A / B：音乐写回走能力契约，不是硬编码 id
# =====================================================================
class TestMusicWritebackContract(unittest.TestCase):
    """【裁决】音乐增强 = 结构化风格 + 歌词，写回走契约。"""

    def setUp(self):
        self.js = ENHANCE_JS.read_text(encoding="utf-8")
        self.code = strip_js_comments(self.js)
        self.shared = SHARED_JS.read_text(encoding="utf-8")
        self.music = MUSIC_JS.read_text(encoding="utf-8")

    def test_no_hardcoded_music_style_write_target(self):
        """音乐写回不再硬编码 `music ? "#musicStyle" : "#prompt"`。

        硬编码会让 yue2-flow 的 #musicPrompt 写回静默失败（TypeError）。
        """
        # 硬编码形态：三元表达式里直接出现 "#musicStyle"
        self.assertNotRegex(
            self.code,
            r'music\s*\?\s*"#musicStyle"\s*:\s*"#prompt"',
            "enhance-menu.js 不得硬编码 `music ? \"#musicStyle\" : \"#prompt\"`")
        # 也不得出现 outputSelectors 直接等于单个 "#musicStyle"
        self.assertNotRegex(
            self.code,
            r'outputSelectors\s*:\s*\[\s*"#musicStyle"\s*\]',
            "outputSelectors 不得只含 #musicStyle（应走候选列表/契约）")

    def test_writeback_is_declared_per_method_kind(self):
        """每种增强方式声明自己的写回形态（kind）。"""
        # 注册表里三种写回形态都要有对应 kind
        for kind in ("yue2-struct", "moss-layout", "qwen-pe", "template"):
            self.assertIn(f'kind: "{kind}"', self.code,
                          f"注册表应声明 kind: {kind}")

    def test_structured_write_contract_is_registered(self):
        """音乐侧注册结构化写回契约（含 apply 与缺目标提示）。"""
        self.assertIn("getWriteContract", self.music)
        self.assertIn("kind: \"structured\"", self.music)
        self.assertIn("apply:", self.music)
        # 缺目标时要有可读提示（describe）
        self.assertIn("describe:", self.music)

    def test_resolve_write_target_is_fail_closed(self):
        """契约解析失败必须返回 {ok:false, reason}，不抛异常。"""
        shared_code = strip_js_comments(self.shared)
        self.assertIn("resolveWriteTarget", shared_code)
        self.assertIn("ok: false", shared_code)
        self.assertIn("reason", shared_code)
        # 解析失败路径不得 throw
        fn = shared_code[shared_code.index("function resolveWriteTarget"):]
        fn = fn[:fn.index("\n  }")]
        self.assertNotIn("throw ", fn,
                         "resolveWriteTarget 不得抛异常（必须 fail-closed 返回）")

    def test_missing_target_gives_readable_message(self):
        """目标缺失要有可读原因，不是空串。"""
        shared_code = strip_js_comments(self.shared)
        self.assertIn("该增强方式没有声明写回目标", shared_code)

    def test_targets_input_is_extensible_to_field_groups(self):
        """targets() 的 input 抽象支持「一组结构化字段」。"""
        # 契约形态：targets 里除了 inputSelectors，还要能表达
        # 「一组字段」（structured 路线由 music 侧契约负责）
        self.assertIn("inputSelectors", self.code)
        self.assertIn("outputSelectors", self.code)
        # 结构化路线的目标由 MusicEnhance 契约提供（不是硬编码在 targets 里）
        self.assertIn("resolveStructuredWriteTarget", self.code)

    def test_music_mode_offers_only_structured_enhancement(self):
        """音乐面板只提供结构化增强（文本扩写类产物塞不进结构化契约）。"""
        fn = self.code[self.code.index("function isAvailable(method)"):]
        fn = fn[:fn.index("\n  }")]
        self.assertIn("isMusicContext()", fn)
        self.assertIn('method.kind === "yue2-struct"', fn)

    def test_music_panel_dom_uses_structured_fields(self):
        """DOM 约定：#musicPrompt + #musicStyleFields + #musicLyrics。"""
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn('id="musicPrompt"', html)
        self.assertIn('id="musicStyleFields"', html)
        self.assertIn('id="musicLyrics"', html)
        # 旧的 #musicStyle 应已不存在（避免两套命名）
        self.assertNotIn('id="musicStyle"', html,
                         "音乐描述框应统一为 #musicPrompt（不保留 #musicStyle）")

    def test_write_contract_is_mode_scoped(self):
        """【2026-11-10 真机回归】写回契约必须**按面板**选，不能无条件用音乐契约。

        根因：MusicEnhance 在 DOMContentLoaded 时无条件注册 window.MusicEnhance，
        enhanceWriteContract() 无条件返回音乐结构化契约 ->
        图像面板写回被解析成 kind:"structured" -> 「替换/追加」静默失败、
        徽标不显示、版本等级恒为 0。
        修法：只有音乐面板（isMusicMode()）才用结构化契约。
        """
        body = strip_js_comments(PROVIDERS_JS.read_text(encoding="utf-8"))
        idx = body.index("function enhanceWriteContract()")
        fn = body[idx:idx + 1200]
        self.assertIn("isMusicMode", fn,
                      "enhanceWriteContract 必须先判断是否音乐面板")
        # 结构化契约的取用必须在 isMusicMode 守卫之内
        self.assertLess(fn.index("isMusicMode"), fn.index("getWriteContract"),
                        "isMusicMode 判据必须出现在 getWriteContract 之前")

    def test_structured_fields_are_filled_by_contract(self):
        """契约的 apply 要同时写结构化风格字段与歌词。"""
        self.assertIn("setStyleFields", self.music)
        self.assertIn("setLyrics", self.music)


# =====================================================================
# 契约 C / D：模板状态单一来源 + 跨模式不串
# =====================================================================
class TestSingleTemplateState(unittest.TestCase):
    """【P1-2】双状态并存 + 跨模式串味。"""

    def setUp(self):
        self.js = ENHANCE_JS.read_text(encoding="utf-8")
        self.code = strip_js_comments(self.js)
        self.providers = PROVIDERS_JS.read_text(encoding="utf-8")

    def test_enhance_path_does_not_write_prov_state_spec_id(self):
        """增强路径不得写 provState.specId（那是本地模型配置页的状态）。"""
        # runBasic / runRefine 里不能再出现 `provState.specId = `
        for fn_name in ("runBasic", "runRefine"):
            idx = self.code.index(f"async function {fn_name}")
            body = self.code[idx:idx + 4000]
            self.assertNotIn("provState.specId =", body,
                             f"{fn_name} 不得写 provState.specId")

    def test_selected_spec_by_method_is_the_single_source(self):
        """模板选择状态按 mode::method 存（跨面板隔离）。"""
        self.assertIn("selectedSpecByMethod", self.code)
        self.assertIn("function specKey(methodId)", self.code)
        # key 形如 mode + "::" + methodId
        self.assertRegex(self.code, r'mode \+ "::" \+ methodId')

    def test_current_spec_id_only_reads_server_defaults(self):
        """currentSpecId 只认服务端 modeDefaults + 用户显式选择。"""
        idx = self.code.index("function currentSpecId()")
        body = self.code[idx:idx + 500]
        self.assertNotIn("provState.specId", body,
                         "currentSpecId 不得回落读 provState.specId")

    def test_spec_selection_invalidated_on_mode_switch(self):
        """切模式时失效旧模板选择。"""
        self.assertIn("function invalidateSpecSelection()", self.code)
        idx = self.code.index("function invalidateSpecSelection()")
        body = self.code[idx:idx + 1500]
        self.assertIn("delete selectedSpecByMethod[key]", body)

    def test_spec_row_is_parallel_to_method_selector(self):
        """模板选择器与增强方式平级（在增强栏内，不在下拉项里）。"""
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn('id="enhanceSpecRow"', html)
        self.assertIn('id="enhanceSpecSelect"', html)
        # 旧的独立模板下拉必须已删除
        self.assertNotIn('id="enhanceSpec"', html,
                         "独立模板下拉 #enhanceSpec 应已删除")

    def test_prov_state_spec_id_only_used_by_config_page(self):
        """provState.specId 的读写只在「本地模型配置」页范围内。"""
        # providers.js 里 provState.specId 的赋值点，应与 agentSpec 下拉绑定
        self.assertIn('provState.specId = e.target.value', self.providers)
        # enhance-menu.js 里不得读它作为模板来源
        self.assertNotIn("provState.specId", self.code,
                         "enhance-menu.js 不得读 provState.specId")


# =====================================================================
# 契约 E：并发闸门单一权威定义
# =====================================================================
class TestSingleConcurrencyGate(unittest.TestCase):
    """【P1-2】两条分支各自实现 ENHANCE_CONCURRENCY。"""

    def setUp(self):
        self.server = SERVER.read_text(encoding="utf-8")

    def test_only_one_definition(self):
        """server.py 里 ENHANCE_CONCURRENCY 只能赋值一次。"""
        assigns = re.findall(r"^ENHANCE_CONCURRENCY\s*=\s*\d+",
                             self.server, flags=re.M)
        self.assertEqual(len(assigns), 1,
                         f"ENHANCE_CONCURRENCY 应只有一处定义，实得 {len(assigns)} 处：{assigns}")

    def test_only_one_gate_instance(self):
        """ENHANCE_GATE 也只声明一次。"""
        declares = re.findall(r"^ENHANCE_GATE\s*:\s*asyncio\.Semaphore",
                              self.server, flags=re.M)
        self.assertEqual(len(declares), 1,
                         f"ENHANCE_GATE 应只有一处声明，实得 {len(declares)} 处")

    def test_value_is_documented_at_definition(self):
        """取值与理由写在定义处（不是散落在两处）。"""
        idx = self.server.index("ENHANCE_CONCURRENCY = ")
        head = self.server[max(0, idx - 3000):idx]
        self.assertIn("唯一权威定义", head,
                      "定义处应写明这是唯一权威定义")
        self.assertIn("取值裁决", head,
                      "定义处应写明取值裁决的理由")

    def test_gate_uses_locked_not_wait_for(self):
        """抢不到就 429 的判据必须是 locked()（wait_for(timeout=0) 实测全 429）。"""
        self.assertIn("gate.locked()", self.server)
        self.assertNotIn("wait_for(gate.acquire(), timeout=0)", self.server)

    def test_gate_rejects_with_429(self):
        self.assertIn("status=429", self.server)
        self.assertIn("too_many_requests", self.server)


# =====================================================================
# 契约 F：行尾
# =====================================================================
class TestLineEndings(unittest.TestCase):
    """【P0-3】CRLF 文件不能引入 LF 噪声。"""

    def test_crlf_files_have_crlf_in_index_and_worktree(self):
        """git ls-files --eol：CRLF 文件必须是 i/crlf + w/crlf。"""
        out = git("ls-files", "--eol").stdout
        rows = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            name = parts[-1]
            rows[name] = parts[0]
        for f in CRLF_FILES:
            self.assertIn(f, rows, f"{f} 应在 git ls-files --eol 里")
            self.assertEqual(rows[f], "i/crlf",
                             f"{f} 的 blob 必须是 CRLF（不得引入 LF 噪声）")

    def test_gitattributes_marks_crlf_files_as_dash_text(self):
        """-text 才能保住历史 CRLF blob（text eol=crlf 会在 add 时归一化）。"""
        ga = (REPO / ".gitattributes").read_text(encoding="utf-8")
        for f in CRLF_FILES:
            self.assertIn(f"/{f}", ga,
                          f".gitattributes 应对 {f} 标注 -text")
        self.assertIn("-text", ga)
        # 通配规则必须在前，具体规则覆盖它
        self.assertLess(ga.index("* text=auto eol=lf"), ga.index("/server.py"),
                        "通配规则必须写在具体规则之前（git 是最后一条匹配生效）")

    def test_worktree_bytes_are_crlf(self):
        """工作区文件字节本身就是 CRLF（不只是 index）。"""
        for f in CRLF_FILES:
            data = (REPO / f).read_bytes()
            crlf = data.count(b"\r\n")
            bare_lf = data.count(b"\n") - crlf
            self.assertEqual(bare_lf, 0,
                             f"{f} 工作区有 {bare_lf} 个裸 LF（应全为 CRLF）")
            self.assertGreater(crlf, 0)


# =====================================================================
# 契约 D 的浏览器实测：跨模式来回切换不串模板 id
# =====================================================================
@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright 不可用")
class TestCrossModeTemplateBrowser(unittest.TestCase):
    """【P1-2】跨模式来回切换不会命中别的模式的模板 id。"""

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
        self.page.wait_for_timeout(900)

    PROBE = """
    (mode) => {
      const em = window.enhanceMethods;
      if (!em) return {error: "enhanceMethods 未就绪"};
      const specs = (typeof enhanceSpecsForContext === "function")
        ? enhanceSpecsForContext(mode, "") : [];
      const picked = em.getMethodSpec ? em.getMethodSpec({id: "basic"}) : "";
      const stored = em.getSelectedSpecByMethod
        ? em.getSelectedSpecByMethod() : {};
      return {
        available: specs.map(s => s.id),
        picked: picked,
        stored: stored,
      };
    }
    """

    def ctx(self, mode):
        return self.page.evaluate(self.PROBE, mode)

    def test_cross_mode_roundtrip_keeps_spec_in_context(self):
        """t2i -> t2i_qwen -> t2i：回来后的模板必须属于 t2i 的可用集合。"""
        self.switch_mode("t2i")
        a = self.ctx("t2i")
        self.switch_mode("t2i_qwen")
        b = self.ctx("t2i_qwen")
        self.switch_mode("t2i")
        c = self.ctx("t2i")

        self.assertNotIn("error", a, f"enhanceMethods 未就绪：{a}")
        self.assertTrue(a["available"], f"t2i 应有可用模板：{a}")
        self.assertTrue(c["available"], f"切回 t2i 应有可用模板：{c}")

        # 关键断言：切回来的模板 id 必须属于 t2i 的可用集合
        if c["picked"]:
            self.assertIn(
                c["picked"], c["available"],
                f"切回 t2i 后命中了不属于该面板的模板 id：{c}")

        # 别的模式的模板 id 不能泄漏回 t2i
        if b["picked"] and a["available"] and b["picked"] not in a["available"]:
            self.assertNotIn(
                b["picked"], c["picked"],
                f"别的模式的模板 id 串到了 t2i：{b} -> {c}")

    def test_stored_selection_is_keyed_by_mode(self):
        """存储的模板选择必须按 mode::method 分键，不能是全局单值。"""
        self.switch_mode("t2i")
        self.page.evaluate(
            """() => {
              if (window.enhanceMethods && window.enhanceMethods.setMethodSpec) {
                const specs = (typeof enhanceSpecsForContext === 'function')
                  ? enhanceSpecsForContext('t2i', '') : [];
                if (specs.length) {
                  window.enhanceMethods.setMethodSpec('basic', specs[0].id);
                }
              }
            }""")
        self.page.wait_for_timeout(400)
        stored = self.page.evaluate(
            "() => window.enhanceMethods.getSelectedSpecByMethod()")
        self.assertTrue(stored, "应存下模板选择")
        for key in stored:
            self.assertIn("::", key,
                          f"模板选择键 {key!r} 应形如 '<mode>::<method>'")

    def test_write_contract_is_mode_scoped(self):
        """图像/视频面板用文本契约；音乐面板用结构化契约。"""
        for mode, expect in (("t2i", "text"), ("t2i_qwen", "text"),
                              ("t2i_moss", "text"), ("t2v", "text"),
                              ("t2a", "structured"), ("a2a", "structured")):
            self.switch_mode(mode)
            c = self.page.evaluate(
                """() => {
                  const t = resolveEnhanceWriteTarget();
                  return {ok: t.ok, kind: t.kind || '', selector: t.selector || ''};
                }""")
            self.assertTrue(c["ok"], f"{mode} 写回契约应解析成功：{c}")
            self.assertEqual(c["kind"], expect,
                             f"{mode} 的写回契约类型应为 {expect}：{c}")
            if expect == "text":
                self.assertEqual(c["selector"], "#prompt",
                                 f"{mode} 文本契约目标应是 #prompt：{c}")

    def test_no_console_errors(self):
        self.assertEqual(self.errors, [], f"console 报错：{self.errors}")


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
