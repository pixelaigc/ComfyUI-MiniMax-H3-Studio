# -*- coding: utf-8 -*-
"""综合审查返修 · yue2 音乐流程分支（local/feature/yue2-flow）—— 新契约。

本文件把本轮**返修要求**写死成契约，防止回退。

【P0-1 写回契约】音乐增强的写回目标**按能力契约**解析，不再硬编码 id
    契约 A：注册表里每种增强方式都声明自己的 `write` 契约
            （text = 文本路线 / structured = 结构化路线）。
    契约 B：文本路线的目标是**候选列表**，先新后旧探测；
            音乐侧 `#musicPrompt` 优先、`#musicStyle` 兼容（同一语义两个名字）。
    契约 C：目标缺失时**不抛 TypeError**，返回可读原因。
    契约 D：外部调用方（providers.js 的「替换/追加」）走同一份契约，
            不再各自硬编码 id。
    契约 E：契约形态按「当前可运行的增强方式」决定，
            不能被注册表里排在前面的已失效项带偏。

【P1 形态统一】每个面板只有一个增强入口 + 明确的当前方式回显
    契约 F：音乐面板只有一个增强主按钮 + 一个增强方式下拉。
    契约 G：音乐面板有「当前增强方式」显式回显。
    契约 H：增强方式下拉里不再嵌套第二个模板下拉。

【P0-3 行尾】
    契约 I：CRLF 文件的 blob 与工作区**都是 CRLF**，零 LF 噪声。

运行：python3 -m pytest tests/test_yue2_flow_review_fixes.py -v
"""
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

WEB = REPO / "web"
ENHANCE_JS = WEB / "enhance-menu.js"
SHARED_JS = WEB / "enhance-menu-shared.js"
MUSIC_ENHANCE_JS = WEB / "music-enhance.js"
PROVIDERS_JS = WEB / "providers.js"
INDEX_HTML = WEB / "index.html"

#: 这些文件在仓库里是 CRLF blob（历史遗留，行尾策略见 .gitattributes）
CRLF_FILES = ["server.py", "web/index.html", "web/app.js", "web/style.css"]


def strip_js_comments(text: str) -> str:
    """去掉 JS 行注释与块注释 —— 只留真实代码。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


class TestWriteContractDeclaration(unittest.TestCase):
    """契约 A / E：注册表按增强方式声明写回契约。"""

    def setUp(self):
        self.raw = read(ENHANCE_JS)
        self.code = strip_js_comments(self.raw)

    def test_every_method_declares_write_contract(self):
        """每种增强方式都必须声明 write 契约（text 或 structured）。"""
        # 取注册表里每个 id 对应的条目块
        blocks = re.findall(
            r"\{\s*id:\s*\"([a-z_]+)\",(.*?)\n    \},", self.raw, flags=re.S)
        self.assertTrue(blocks, "没解析到 ENHANCE_METHODS 条目")
        missing = []
        for mid, body in blocks:
            if "write:" not in body:
                missing.append(mid)
        self.assertEqual(missing, [],
                         f"这些增强方式没声明 write 契约: {missing}")

    def test_yue2_struct_declares_structured_write(self):
        """yue2_struct 走结构化路线（风格字段 + 歌词）。"""
        m = re.search(r"id:\s*\"yue2_struct\",(.*?)\n    \},", self.raw, flags=re.S)
        self.assertIsNotNone(m, "注册表里没有 yue2_struct")
        self.assertIn("write:", m.group(1))
        self.assertIn("structured", m.group(1),
                      "yue2_struct 的写回契约必须是 structured")

    def test_text_methods_declare_text_write(self):
        """文本扩写类（basic / refine / layout / qwen_pe）走文本路线。"""
        for mid in ("basic", "refine", "layout", "qwen_pe"):
            m = re.search(r"id:\s*\"%s\",(.*?)\n    \}," % mid, self.raw, flags=re.S)
            self.assertIsNotNone(m, "注册表里没有 %s" % mid)
            self.assertIn("write:", m.group(1), "%s 没声明 write 契约" % mid)
            self.assertRegex(m.group(1), r'write:\s*\{\s*kind:\s*"text"',
                             "%s 的写回契约应该是 text" % mid)

    def test_method_write_contract_resolver_exists(self):
        """注册表对外暴露按方式取契约的入口。"""
        self.assertIn("function methodWriteContract", self.code)
        self.assertIn("getWriteContract:", self.code)

    def test_get_write_contract_prefers_runnable_method(self):
        """契约 E：默认取当前**可运行**的方式，不能被失效项带偏。"""
        m = re.search(r"getWriteContract:\s*\(methodId\)\s*=>\s*\{(.*?)\n        \}",
                      self.code, flags=re.S)
        self.assertIsNotNone(m, "getWriteContract 没导出")
        body = m.group(1)
        self.assertIn("runnableMethods()", body,
                      "getWriteContract 必须优先用 runnableMethods()（按可用性过滤）")


class TestWriteTargetResolution(unittest.TestCase):
    """契约 B / C：候选列表探测 + 缺失时 fail-closed。"""

    def setUp(self):
        self.raw = read(ENHANCE_JS)
        self.code = strip_js_comments(self.raw)
        self.shared = read(SHARED_JS)

    def test_music_input_candidates_prioritise_music_prompt(self):
        """#musicPrompt 优先，#musicStyle 兼容（候选列表，先新后旧）。"""
        m = re.search(r"inputSelectors:\s*music\s*\?\s*\[(.*?)\]", self.code, flags=re.S)
        self.assertIsNotNone(m, "没找到 inputSelectors 的音乐候选列表")
        cand = m.group(1)
        self.assertIn("#musicPrompt", cand, "音乐输入候选里必须有 #musicPrompt")
        self.assertIn("#musicStyle", cand, "音乐输入候选里应兼容 #musicStyle")
        self.assertLess(cand.index("#musicPrompt"), cand.index("#musicStyle"),
                        "#musicPrompt 必须排在 #musicStyle 前面（先新后旧）")

    def test_no_hardcoded_musicstyle_query(self):
        """真实代码里不得硬编码 querySelector('#musicStyle')。"""
        for p in (ENHANCE_JS, PROVIDERS_JS, SHARED_JS, MUSIC_ENHANCE_JS):
            code = strip_js_comments(read(p))
            self.assertIsNone(
                re.search(r"""querySelector\(\s*["']#musicStyle["']""", code),
                "%s 仍硬编码 querySelector('#musicStyle')" % p.name)
            self.assertIsNone(
                re.search(r"""\$\(\s*["']#musicStyle["']""", code),
                "%s 仍硬编码 $('#musicStyle')" % p.name)

    def test_resolve_target_returns_null_not_throw(self):
        """契约 C：解析不到目标时返回 null（不抛）。"""
        self.assertIn("function resolveTarget", self.code)
        m = re.search(r"function resolveTarget\(selectors\)\s*\{(.*?)\n  \}",
                      self.code, flags=re.S)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("return { el: null", body,
                      "resolveTarget 找不到目标时必须返回 el:null 而不是抛异常")

    def test_writeback_input_uses_declared_contract(self):
        """writeBackInput 必须按 method.write 声明分派，不硬编码。"""
        m = re.search(r"function writeBackInput\(text,\s*method\)\s*\{(.*?)\n  \}",
                      self.code, flags=re.S)
        self.assertIsNotNone(m, "writeBackInput 签名应带 method 参数")
        body = m.group(1)
        self.assertIn("method.write", body, "writeBackInput 必须读 method.write 声明")
        self.assertIn("structured", body, "writeBackInput 必须支持结构化路线")
        self.assertIn("outputSelectors", body, "文本路线仍走能力解析的候选列表")

    def test_shared_resolve_write_target_fail_closed(self):
        """共用组件的 resolveWriteTarget 缺目标时返回 { ok:false, reason }。"""
        self.assertIn("function resolveWriteTarget", self.shared)
        self.assertIn("ok: false", self.shared)
        self.assertIn("reason", self.shared)


class TestProvidersUseSharedContract(unittest.TestCase):
    """契约 D：外部调用方走同一份契约。"""

    def setUp(self):
        self.raw = read(PROVIDERS_JS)
        self.code = strip_js_comments(self.raw)

    def test_providers_prefers_registry_contract(self):
        """providers.js 优先问注册表，而不是自己硬编码。"""
        self.assertIn("window.enhanceMethods", self.code)
        self.assertIn("getWriteContract", self.code)
        m = re.search(r"function enhanceWriteContract\(\)\s*\{(.*?)\n\}",
                      self.code, flags=re.S)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertLess(body.index("enhanceMethods"), body.index("ENHANCE_WRITE_TARGETS"),
                        "必须先问注册表，静态表只能当兜底")

    def test_providers_structured_branch_has_readable_hint(self):
        """结构化路线不走文本写回时，必须给可读提示。"""
        m = re.search(r'if \(target\.kind === "structured"\)\s*\{(.*?)\n\s*\}',
                      self.code, flags=re.S)
        self.assertIsNotNone(m, "providers.js 里找不到 structured 分支")
        self.assertIn("setEnhanceHint", m.group(1),
                      "结构化路线必须给出可读提示（不能静默失败）")


class TestMusicPanelSingleEnhanceEntry(unittest.TestCase):
    """契约 F / G / H：音乐面板只有一个增强入口 + 当前方式回显。"""

    def setUp(self):
        self.html = read(INDEX_HTML)

    def test_only_one_enhance_main_button(self):
        """音乐面板只有一个增强主按钮。"""
        mains = re.findall(r'id="musicEnhanceBtn"', self.html)
        self.assertEqual(len(mains), 1, "音乐面板应有且仅有一个增强主按钮")

    def test_only_one_enhance_menu(self):
        """音乐面板只有一个增强方式下拉。"""
        menus = re.findall(r'id="musicEnhanceMenu"', self.html)
        self.assertEqual(len(menus), 1, "音乐面板应有且仅有一个增强方式下拉")

    def test_has_current_method_indicator(self):
        """契约 G：音乐面板有「当前增强方式」显式回显。"""
        self.assertIn('id="musicEnhanceCurrent"', self.html,
                      "音乐面板缺少「当前增强方式」回显元素")

    def test_current_indicator_is_populated_by_shared_code(self):
        """回显由共享逻辑填充（图像与音乐共用一套）。"""
        code = strip_js_comments(read(ENHANCE_JS))
        self.assertIn("#musicEnhanceCurrent", code,
                      "当前方式回显必须同时更新音乐与图像两处")
        m = re.search(r'const ids = \["#enhanceCurrent", "#musicEnhanceCurrent"\]', code)
        self.assertIsNotNone(m, "两个面板的回显必须由同一段代码更新")

    def test_no_nested_template_select_in_enhance_menu(self):
        """契约 H：面板里不再有第二个模板下拉（模板选择收进增强菜单项内）。

        【2026-11-10 契约变更】
        改造前模板选择是增强栏里与「增强方式」平级的一行
        （#musicEnhanceSpecRow / #musicEnhanceSpecSelect）—— 那是
        第二个下拉，与「增强方式」下拉语义重叠。
        现在模板选择收进菜单项内（renderSpecPicker）。
        """
        # 平行的模板行 / 模板下拉必须已从 HTML 删掉
        for gone in ('id="musicEnhanceSpecRow"', 'id="musicEnhanceSpecSelect"',
                     'id="enhanceSpecRow"', 'id="enhanceSpecSelect"'):
            self.assertNotIn(gone, self.html,
                             f"{gone} 是第二个模板下拉，必须删除")
        # 模板选择器在菜单项内
        code = strip_js_comments(read(ENHANCE_JS))
        self.assertIn("function renderSpecPicker", code,
                      "必须有 renderSpecPicker（菜单项内的模板选择器）")
        self.assertIn("specHtml: renderSpecPicker", code,
                      "菜单渲染必须用 renderSpecPicker")

    def test_music_mode_switcher_removed(self):
        """面板顶部不再有生成模式切换。"""
        for gone in ("musicModeChips", "musicModeNote"):
            self.assertNotIn(f'id="{gone}"', self.html,
                             f"音乐面板不该有 {gone}（模式只靠左侧标签切换）")

    def test_duration_control_removed(self):
        """不再有「最长时长」参数。"""
        self.assertNotIn('id="musicDuration"', self.html)
        code = strip_js_comments("".join(
            read(WEB / n) for n in ("music.js", "app.js", "enhance-menu.js")))
        self.assertNotIn("musicDuration", code,
                         "前端代码里不该再引用 #musicDuration")


class TestCrlfFilesStayCrlf(unittest.TestCase):
    """契约 I：CRLF 文件不得被转成 LF（否则制造伪冲突）。"""

    def test_worktree_files_are_crlf(self):
        for rel in CRLF_FILES:
            p = REPO / rel
            if not p.exists():
                continue
            data = p.read_bytes()
            self.assertNotEqual(data, b"", "%s 是空文件" % rel)
            crlf = data.count(b"\r\n")
            lf = data.count(b"\n")
            self.assertGreater(crlf, 0, "%s 没有 CRLF（行尾被转成 LF 了）" % rel)
            self.assertEqual(crlf, lf,
                             "%s 行尾混杂（CRLF=%d / LF=%d）" % (rel, crlf, lf))

    def test_blob_matches_worktree(self):
        """工作区内容必须与提交进仓库的 blob 字节一致。"""
        out = subprocess.run(
            ["git", "ls-files", "--eol", *CRLF_FILES],
            cwd=str(REPO), capture_output=True, text=True).stdout
        for line in out.strip().splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            eol = parts[0]
            path = parts[-1]
            self.assertTrue(eol.startswith("i/crlf"),
                            "%s 的 blob 不是 CRLF（%s）——会造成伪冲突" % (path, eol))


class TestNoConflictWithEnhUnify(unittest.TestCase):
    """合并面：与 enh-unify 合并时冲突面归零。"""

    def test_merge_tree_is_clean(self):
        """用 merge-tree 模拟合并，不应报冲突。"""
        for ref in ("local/feature/enh-unify", "local/maintainer"):
            exists = subprocess.run(
                ["git", "rev-parse", "--verify", ref],
                cwd=str(REPO), capture_output=True, text=True).returncode == 0
            if not exists:
                continue
            r = subprocess.run(
                ["git", "merge-tree", "--write-tree", ref, "HEAD"],
                cwd=str(REPO), capture_output=True, text=True)
            self.assertEqual(r.returncode, 0,
                             "与 %s 合并有冲突:\n%s" % (ref, r.stdout + r.stderr))


if __name__ == "__main__":
    unittest.main()
