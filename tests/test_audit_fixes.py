# -*- coding: utf-8 -*-
"""综合审查返修的**契约**测试（审查 P0-1 / P1-1 / P2-4 / P3-2 / P0-3）。

覆盖本轮返修建立的 5 条新契约：

  C1 写回目标按**能力契约**解析（P0-1）
     - 不再硬编码 `#musicStyle`（合并 enh-unify 后该 id 不存在，
       `document.querySelector` 返回 null -> `.value` 抛 TypeError）
     - 目标缺失时**不抛异常**，返回可读原因

  C2 每个面板只有「一个增强按钮 + 一个下拉 + 当前方式回显」（P2-4）
     - `#enhanceSpec` 第二个模板下拉已删除
     - 音乐面板有 `#musicEnhanceCurrent` 回显且可见有字

  C3 共用组件已抽出（P0-2）
     - web/enhance-menu-shared.js 存在并导出渲染/定位/开合/契约解析
     - enhance-menu.js 与 music-enhance.js 都**用它**，不各造一套

  C4 schema 只有一份权威实现（P1-1）
     - music_style.py 是 yue2_schema.py 的 re-export，不是第二份副本
     - 前端不再硬编码 length_hint 映射（由服务端下发）

  C5 歌词行数校验不再误报（P3-2）
     - 正常段落结构（Verse 3 行 / Intro 1 行 / Pre-Chorus 1 行）不报
     - 副歌段行数不足仍然要报（校验没有失效）

  C6 行尾策略（P0-3）
     - 4 个历史 CRLF 文件的 blob 仍是 CRLF
     - .gitattributes 存在且把这批文件标成 -text

运行：python3 -m pytest tests/test_audit_fixes.py -v
"""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

WEB = REPO / "web"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def strip_js_comments(src: str) -> str:
    """去掉 JS 注释，只留**真实代码**。

    为什么需要：本轮的注释里大量引用了 `#musicStyle` / `#enhanceSpec`
    这两个「被移除的 id」（用来解释为什么移除）。只做字符串匹配会把
    这些**说明性注释**误判成残留代码。
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    lines = []
    for line in src.split("\n"):
        i = line.find("//")
        if i >= 0:
            line = line[:i]
        lines.append(line)
    return "\n".join(lines)


def js_code(path: Path) -> str:
    return strip_js_comments(read(path))


class TestC1WriteContract(unittest.TestCase):
    """C1：写回目标按能力契约解析，缺失不抛异常。"""

    def test_no_hardcoded_musicstyle_selector(self):
        """任何前端文件都不得再硬编码 querySelector('#musicStyle')（只看真实代码）。"""
        offenders = []
        for p in sorted(WEB.glob("*.js")):
            code = js_code(p)
            if re.search(r"""querySelector\(\s*["']#musicStyle["']""", code):
                offenders.append(f"{p.name}: querySelector('#musicStyle')")
            if re.search(r"""\$\(\s*["']#musicStyle["']""", code):
                offenders.append(f"{p.name}: $('#musicStyle')")
            if re.search(r"""getElementById\(\s*["']musicStyle["']""", code):
                offenders.append(f"{p.name}: getElementById('musicStyle')")
        self.assertEqual(offenders, [],
                         f"仍硬编码 #musicStyle（合并后会拿到 null 并抛 TypeError）: {offenders}")

    def test_musicstyle_id_absent_from_dom(self):
        """#musicStyle 这个 id 在 HTML 里不存在（yue2-flow 已改名成 #musicPrompt）。"""
        html = read(WEB / "index.html")
        self.assertNotIn('id="musicStyle"', html,
                         "#musicStyle 已不存在 —— 任何硬编码它的代码都会拿到 null")
        self.assertIn('id="musicPrompt"', html)

    def test_shared_component_exposes_contract_resolver(self):
        """共用组件导出 resolveWriteTarget（契约解析的权威入口）。"""
        src = read(WEB / "enhance-menu-shared.js")
        self.assertIn("function resolveWriteTarget", src)
        self.assertIn("resolveWriteTarget: resolveWriteTarget", src)
        # 契约缺失 -> 返回 { ok:false, reason }，**不抛异常**
        self.assertIn("ok: false", src)
        self.assertIn("reason:", src)

    def test_contract_resolver_missing_target_returns_reason(self):
        """目标不存在时返回可读原因，不抛 TypeError。"""
        code = js_code(WEB / "enhance-menu-shared.js")
        # kind=text 分支：先 querySelector，再判空返回 reason
        self.assertRegex(code, r"if\s*\(\s*!el\s*\)")
        self.assertRegex(code, r"reason", "缺目标时必须给出可读原因")

    def test_music_registers_structured_contract(self):
        """音乐侧注册**结构化**契约（风格字段 + 歌词），不是自由文本框。"""
        src = read(WEB / "music-enhance.js")
        self.assertIn("function getWriteContract", src)
        self.assertIn('kind: "structured"', src)
        self.assertIn("getWriteContract: getWriteContract", src)

    def test_providers_writeback_uses_contract(self):
        """providers.js 的「替换/追加」走契约，不再直接改某个框的 .value。"""
        src = read(WEB / "providers.js")
        self.assertIn("function writeBackEnhanceResult", src)
        self.assertIn("resolveEnhanceWriteTarget", src)
        # 旧写法：enhanceTargetInput().value = text
        self.assertNotIn("enhanceTargetInput", src,
                         "旧的硬编码写回入口应已移除")

    def test_enhance_menu_null_safe_input(self):
        """enhance-menu.js 里不再有裸的 document.querySelector(targets().input).value。"""
        src = read(WEB / "enhance-menu.js")
        self.assertNotIn("document.querySelector(targets().input)", src,
                         "输入框访问应走 inputElement()/inputValue() 的 null 安全封装")


class TestC2SingleEnhanceEntry(unittest.TestCase):
    """C2：每个面板只有 1 个增强按钮 + 1 个下拉 + 当前方式回显。"""

    def test_enhance_spec_dropdown_removed(self):
        """第二个「增强模板」下拉 #enhanceSpec 已删除。"""
        html = read(WEB / "index.html")
        self.assertNotIn('id="enhanceSpec"', html,
                         "#enhanceSpec 第二个下拉应删除（模板选择已并入增强下拉）")

    def test_no_enhance_spec_in_js(self):
        """前端不再引用**旧的** #enhanceSpec 下拉（只看真实代码，注释不算）。

        注意：新的「模板选择行」用的是 #enhanceSpecRow / #enhanceSpecSelect
        （enh-unify 的 P1-2 修法：模板选择器提升为增强栏里的平级一行），
        与旧的独立下拉 #enhanceSpec 不是同一个东西。
        """
        offenders = []
        for p in sorted(WEB.glob("*.js")):
            code = js_code(p)
            if re.search(r"""["'#]enhanceSpec["']""", code):
                offenders.append(f"{p.name}: #enhanceSpec")
            if "renderEnhanceSpecSelect" in code:
                offenders.append(f"{p.name}: renderEnhanceSpecSelect")
        self.assertEqual(offenders, [], f"仍引用已删除的模板下拉: {offenders}")

    def test_current_indicator_exists_for_both_panels(self):
        """图像面板与音乐面板都有「当前增强方式」回显元素。"""
        html = read(WEB / "index.html")
        self.assertIn('id="enhanceCurrent"', html)
        self.assertIn('id="musicEnhanceCurrent"', html)

    def test_no_duplicate_ids_in_dom(self):
        """整个 HTML 里没有重复 id（合并 enh-unify 曾引入两套音乐增强栏）。"""
        html = read(WEB / "index.html")
        ids = re.findall(r'id="([^"]+)"', html)
        dup = sorted({i for i in ids if ids.count(i) > 1})
        self.assertEqual(dup, [], f"HTML 里有重复 id: {dup}")

    def test_music_enhance_bar_not_duplicated(self):
        """enh-unify 的 #musicEnhanceBar 整块已被移除（它的 id 与面板内那套重复）。"""
        html = read(WEB / "index.html")
        self.assertNotIn('id="musicEnhanceBar"', html,
                         "#musicEnhanceBar 已并入 #musicPromptField 内的增强栏")

    def test_music_enhance_updates_indicator(self):
        """音乐侧的当前方式回显由**统一注册表**维护（与图像面板同一套格式）。

        【架构说明】音乐侧的回显不再由 music-enhance.js 自己实现 ——
        增强体系合并后，回显是统一注册表（enhance-menu.js 的
        updateCurrentIndicator）的职责，它同时更新 #enhanceCurrent 与
        #musicEnhanceCurrent。music-enhance.js 只负责「怎么执行」与
        「怎么写回」。
        """
        menu = read(WEB / "enhance-menu.js")
        self.assertIn("function updateCurrentIndicator", menu,
                      "统一注册表应维护当前方式回显")
        self.assertIn("#musicEnhanceCurrent", menu,
                      "回显要同时更新音乐面板的元素")
        self.assertIn("当前增强：", menu, "回显文案与图像面板一致")

        # 音乐模块只保留执行与写回
        music = read(WEB / "music-enhance.js")
        self.assertIn("function getWriteContract", music,
                      "音乐模块应注册写回能力契约")
        self.assertIn("function run(", music, "音乐模块应提供执行入口")


class TestC3SharedComponent(unittest.TestCase):
    """C3：共用渲染组件已抽出，两边都用它。"""

    def test_shared_file_exists(self):
        self.assertTrue((WEB / "enhance-menu-shared.js").exists(),
                        "共用组件文件必须存在（合并冲突的根治办法）")

    def test_shared_exports_all_pieces(self):
        src = read(WEB / "enhance-menu-shared.js")
        for name in ("renderEnhanceMenu", "positionMenu", "bindEnhanceMenu",
                     "enhanceMenuItemHtml", "resolveWriteTarget", "esc"):
            self.assertIn(name, src, f"共用组件应导出 {name}")

    def test_both_sides_use_shared_component(self):
        """enhance-menu.js 与 music-enhance.js 都调 window.EnhanceMenuShared。"""
        for name in ("enhance-menu.js", "music-enhance.js"):
            src = read(WEB / name)
            self.assertIn("EnhanceMenuShared", src,
                          f"{name} 应使用共用组件而不是自造一套")

    def test_enhance_menu_does_not_duplicate_renderer(self):
        """enhance-menu.js 不再自带一份 renderEnhanceMenu 实现（避免两份漂移）。"""
        src = read(WEB / "enhance-menu.js")
        self.assertNotIn("function renderEnhanceMenu(menu, cfg)", src,
                         "渲染实现应在共用组件里，只有一份")
        self.assertIn("shared.renderEnhanceMenu(menu, {", src,
                      "enhance-menu.js 应调用共用渲染实现")

    def test_shared_loaded_before_enhance_menu(self):
        """脚本加载顺序：共用组件必须先于 enhance-menu.js。"""
        html = read(WEB / "index.html")
        i_shared = html.find("enhance-menu-shared.js")
        i_menu = html.find("enhance-menu.js")
        i_music = html.find("music-enhance.js")
        self.assertGreater(i_shared, 0, "index.html 必须引入共用组件")
        self.assertLess(i_shared, i_menu, "共用组件要先于 enhance-menu.js 加载")
        self.assertLess(i_menu, i_music, "enhance-menu.js 要先于 music-enhance.js 加载")


class TestC4SingleSchemaAuthority(unittest.TestCase):
    """C4：schema 只有一份权威实现，前端不再硬编码 length_hint。"""

    def test_music_style_is_reexport(self):
        """music_style.py 从 yue2_schema re-export，而不是自己抄一份。"""
        src = read(REPO / "h3studio" / "music_style.py")
        self.assertIn("from . import yue2_schema", src)
        self.assertIn("STATIC_ENUMS = yue2_schema.STATIC_ENUMS", src)
        self.assertIn("LENGTH_HINT_TEXT = yue2_schema.LENGTH_HINTS", src)
        self.assertIn("_omit = yue2_schema._omit", src)
        # 不该再有自己的一份 STATIC_ENUMS 字面量
        self.assertNotRegex(src, r"STATIC_ENUMS\s*=\s*\{",
                            "STATIC_ENUMS 必须来自 yue2_schema，不能自己再抄一份")

    def test_music_style_has_no_own_length_hint_table(self):
        src = read(REPO / "h3studio" / "music_style.py")
        self.assertNotRegex(src, r"LENGTH_HINT_TEXT\s*=\s*\{",
                            "length_hint 映射表只能有一份")

    def test_frontend_has_no_hardcoded_length_hint(self):
        """前端不再硬编码 length_hint 映射（第三份副本）。"""
        offenders = []
        for p in sorted(WEB.glob("*.js")):
            src = read(p)
            if "short song, about one minute" in src:
                offenders.append(p.name)
        self.assertEqual(offenders, [],
                         f"前端仍硬编码 length_hint 映射: {offenders}")

    def test_server_sends_length_hint_text(self):
        """服务端下发 length_hint 文本映射。"""
        src = read(REPO / "h3studio" / "music_yue2.py")
        self.assertIn("length_hint_text", src)

    def test_frontend_consumes_length_hint_text(self):
        """前端从服务端下发的数据里读 length_hint_text。"""
        src = read(WEB / "music.js")
        self.assertIn("length_hint_text", src)

    def test_two_build_style_prompt_agree(self):
        """两个入口的 style 串合成结果一致（re-export 后结构上保证）。"""
        from h3studio import music_style, yue2_schema
        cases = [
            ("Japanese", "piano pop", "bittersweet", "female vocal", "breathy",
             "acoustic piano", 76, "mid energy", "full song (~3.5-5 min)", "soft", "", ""),
            ("(custom / none)", "(custom / none)", "(none)",
             "(instrumental / no lead vocal)", "(none)", "strings", 0,
             "(none)", "model decides", "", "My Language", "My Genre"),
            ("English", "pop", "(none)", "male vocal", "warm", "drums", 120,
             "high energy", "short clip (~45-75s)", "", "", ""),
        ]
        for c in cases:
            a = music_style.build_style_prompt(*c)
            b = yue2_schema.build_style_prompt({
                "language": c[0], "genre": c[1], "mood": c[2], "vocal": c[3],
                "vocal_color": c[4], "instruments": c[5], "tempo_bpm": c[6],
                "energy": c[7], "length_hint": c[8], "extra": c[9],
                "custom_language": c[10], "custom_genre": c[11],
            })
            self.assertEqual(a, b, f"两个入口输出不一致: {c}")


class TestC5LyricsValidation(unittest.TestCase):
    """C5：歌词行数校验不再误报正常段落结构。"""

    # 审查报告里的真实案例：Intro 2 行 / Verse 3 行 / Pre-Chorus 1 行 / Outro 2 行
    NORMAL = """[Intro]
Rain on the window again

[Verse 1]
Walking home alone
Streetlights fade away
Footsteps echo slow

[Pre-Chorus]
Hold the moment tight

[Chorus]
Run with me into the sunlight
Leave the shadows far behind
We will find a brighter morning
Everything will be alright

[Verse 2]
Morning breaks the grey
Coffee going cold
Words I never said

[Pre-Chorus]
Hold the moment tight

[Chorus]
Run with me into the sunlight
Leave the shadows far behind
We will find a brighter morning
Everything will be alright

[Bridge]
Count the days until we meet
Count the stars above the street
Everything will be alright

[Final Chorus]
Run with me into the sunlight
Leave the shadows far behind
We will find a brighter morning
Everything will be alright

[Outro]
Fading into light
"""

    def test_normal_structure_not_flagged(self):
        from h3studio import yue2_schema as ys
        probs = ys._lyrics_problems(self.NORMAL)
        self.assertEqual(probs, [],
                         f"正常段落结构被误报（审查 P3-2）: {probs}")

    def test_thin_chorus_still_flagged(self):
        """副歌行数不足仍然要报 —— 校验不能被改废。"""
        from h3studio import yue2_schema as ys
        thin = self.NORMAL.replace(
            """[Chorus]
Run with me into the sunlight
Leave the shadows far behind
We will find a brighter morning
Everything will be alright""",
            """[Chorus]
Run with me into the sunlight""")
        probs = ys._lyrics_problems(thin)
        self.assertTrue(any("副歌" in p for p in probs),
                        f"副歌过短未被检出，校验失效: {probs}")

    def test_pre_chorus_not_treated_as_chorus(self):
        """[Pre-Chorus] 不算主副歌段。"""
        from h3studio import yue2_schema as ys
        self.assertFalse(ys._is_chorus_tag("[Pre-Chorus]"))
        self.assertTrue(ys._is_chorus_tag("[Chorus]"))
        self.assertTrue(ys._is_chorus_tag("[Final Chorus]"))
        self.assertTrue(ys._is_chorus_tag("[Chorus 2]"))

    def test_prompt_matches_validation_rule(self):
        """系统提示词里的行数规则与校验规则一致（不能一个说 4 行一个说 2 行）。"""
        src = read(REPO / "h3studio" / "yue2_schema.py")
        self.assertIn("副歌段至少 4 行", src)
        self.assertNotIn("每段至少 4 行", src)


class TestC6LineEndings(unittest.TestCase):
    """C6：行尾策略落地（审查 P0-3）。"""

    CRLF_FILES = ["server.py", "web/index.html", "web/app.js", "web/style.css"]

    def test_gitattributes_exists(self):
        p = REPO / ".gitattributes"
        self.assertTrue(p.exists(), ".gitattributes 必须存在（避免行尾问题复发）")
        src = p.read_text(encoding="utf-8")
        self.assertIn("* text=auto eol=lf", src)

    def test_gitattributes_marks_crlf_files_as_no_text(self):
        """.gitattributes 里这批历史 CRLF 文件标成 -text（字节原样保存）。"""
        src = (REPO / ".gitattributes").read_text(encoding="utf-8")
        for f in self.CRLF_FILES:
            self.assertRegex(src, rf"(?m)^{re.escape('/' + f)}\s+-text$",
                             f"{f} 应标成 -text")

    def test_wildcard_rule_before_specific_rules(self):
        """通配规则必须在前 —— git 是「最后一条匹配生效」。"""
        src = (REPO / ".gitattributes").read_text(encoding="utf-8")
        i_wild = src.find("* text=auto eol=lf")
        i_specific = src.find("/server.py")
        self.assertGreater(i_specific, i_wild,
                           "具体规则必须在通配规则之后，否则会被盖掉")

    def test_blob_line_endings_are_crlf(self):
        """这 4 个文件的 **blob**（不只是工作区）必须是 CRLF。"""
        out = subprocess.run(
            ["git", "ls-files", "--eol", *self.CRLF_FILES],
            cwd=REPO, capture_output=True, text=True, check=True).stdout
        for line in out.strip().splitlines():
            self.assertIn("i/crlf", line,
                          f"{line.strip()} —— blob 必须是 CRLF，否则合并会产生伪冲突")


class TestC7NoRegressionOnSharedAssets(unittest.TestCase):
    """C7：合并 enh-unify 后，两边的能力都要在（不回退任一方）。"""

    def test_music_structured_fields_present(self):
        html = read(WEB / "index.html")
        self.assertIn('id="musicStyleFieldsGrid"', html)
        self.assertIn('id="musicLyrics"', html)
        self.assertIn('id="musicStyleRegenBtn"', html)

    def test_enhunify_features_present(self):
        """enh-unify 的能力不能被合并覆盖掉。"""
        src = read(WEB / "enhance-menu.js")
        self.assertIn("function targets()", src, "多面板路由 targets() 应保留")
        self.assertIn("function updateCurrentIndicator", src)
        self.assertIn("function unavailableReason", src)
        # 模板选择：【2026-11-10 契约】收进增强菜单项内（renderSpecPicker），
        # 不再有平行的模板行 —— 面板里只剩**一个**下拉（增强方式）。
        # 这项能力（菜单项内可切换模板）必须保留，不能被合并覆盖掉。
        self.assertIn("function renderSpecPicker", src,
                      "菜单项内的模板选择器应保留")
        self.assertIn("specHtml: renderSpecPicker", src,
                      "菜单渲染应通过共用组件传入模板选择器")
        self.assertIn("function setMethodSpec", src,
                      "模板选择的单一状态来源应保留")
        # 能力契约的候选列表（审查 P0-1）
        self.assertIn("inputSelectors", src, "输入目标应是候选列表")
        self.assertIn("outputSelectors", src, "写回目标应是候选列表")
        self.assertIn("function resolveTarget", src,
                      "目标探测应返回 null 而不是抛异常")
        # 多选版本历史修复（审查 P1-2）
        self.assertIn("version:", src, "各步骤应回报版本信息")

    def test_server_keeps_both_endpoints(self):
        src = read(REPO / "server.py")
        self.assertIn("/api/music-enhance", src)
        self.assertIn("/api/enhancer", src)
        self.assertIn("/api/moss-layout", src)

    def test_no_console_breaking_reference_to_removed_bar(self):
        """app.js 不再引用已删除的 #musicEnhanceBar。"""
        src = read(WEB / "app.js")
        self.assertNotIn("#musicEnhanceBar", src,
                         "#musicEnhanceBar 已删除，引用它会拿到 null")


if __name__ == "__main__":
    unittest.main()
