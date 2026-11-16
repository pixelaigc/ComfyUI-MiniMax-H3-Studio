# -*- coding: utf-8 -*-
"""第5轮 A 段 —— 前端文案污染扫描器自身的回归测试。

【为什么扫描器也要有测试】
扫描器是「判定工具」。如果它有 bug（假阴性 = 漏报真污染、
假阳性 = 淹没真缺陷），第5轮 A 段的结论就不可信。
所以这里锁两件事：
  1. **假阴性防护**：把真污染注回去，扫描器必须抓到（变异测试口径）。
  2. **假阳性防护**：正常业务文案（CSS 类名 / 正常提示语）
     不得被报成污染 —— 否则扫描结果没法用。

【为什么不用「断言扫描结果为空」】
因为业务代码会继续演进，扫描器报出正常文案是**允许**的
（会走人工判定）。所以这里断言的是「**特定已知样本**的行为」，
而不是全局计数。
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "cr5_scan", REPO / "scripts" / "cr5_scan_text_candidates.py")
cr5_scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cr5_scan)


def _scan_web_dir(web_dir: Path):
    """对指定 web 目录跑扫描器，返回 findings 列表。"""
    findings = []
    for fn in sorted(web_dir.iterdir()):
        if not fn.is_file():
            continue
        if fn.suffix == ".js":
            findings += cr5_scan.scan_js(fn)
            findings += cr5_scan.scan_js_html_comment_in_template(fn)
        elif fn.suffix == ".html":
            findings += cr5_scan.scan_html(fn)
    uniq = {}
    for f in findings:
        uniq[(f["file"], f["line"], f["marker"], f["text"])] = f
    return sorted(uniq.values(), key=lambda x: (x["file"], x["line"]))


class TestScannerCatchesRealPollution(unittest.TestCase):
    """关卡 A：扫描器必须抓得住真污染（假阴性防护）。"""

    def _scan_snippet(self, js_body: str, filename="probe.js"):
        with tempfile.TemporaryDirectory(prefix="h3-scan-") as tmp:
            web = Path(tmp) / "web"
            web.mkdir()
            (web / filename).write_text(js_body, encoding="utf-8")
            return _scan_web_dir(web)

    def test_catches_dev_wording_in_object_literal(self):
        """【真实泄漏的形态】文案定义在**对象字面量**里，不是赋值点同一语句。

        这一条对应本轮真实修复的 app.js skeletonJob().prompt ——
        第4轮的扫描器只看「赋值点同一语句」，正是这种形态漏掉了。
        """
        body = (
            'function skeletonJob() {\n'
            '  return {\n'
            '    prompt: "正在载入任务内容，此处的临时文字仅用于对齐布局。",\n'
            '  };\n'
            '}\n'
        )
        hits = self._scan_snippet(body)
        self.assertTrue(hits, "扫描器漏掉了对象字面量里的开发者措辞")
        self.assertTrue(any("临时" in h["text"] or "对齐" in h["text"]
                            for h in hits),
                        f"命中了但不是目标文案：{hits}")

    def test_catches_dev_wording_in_template_literal(self):
        """模板串里的开发者措辞也要抓到。"""
        body = (
            'function render() {\n'
            '  el.innerHTML = `<div>TODO: 这里还没做完，先用占位文字</div>`;\n'
            '}\n'
        )
        hits = self._scan_snippet(body)
        self.assertTrue(hits, "扫描器漏掉了模板串里的开发者措辞")

    def test_catches_html_comment_inside_innerhtml_template(self):
        """innerHTML 模板里嵌 HTML 注释 —— 不渲染但进 DOM，必须抓到。"""
        body = (
            'function render() {\n'
            '  el.innerHTML = `<div>ok</div><!-- 【2026-11-16 内部标记】 -->`;\n'
            '}\n'
        )
        hits = self._scan_snippet(body)
        self.assertTrue(
            any(h["marker"] == "html-comment-in-template" for h in hits),
            f"扫描器漏掉了 innerHTML 模板里的 HTML 注释：{hits}")

    def test_catches_placeholder_attribute_value(self):
        """placeholder 属性里的开发者措辞也要抓到。"""
        body = (
            'function render() {\n'
            '  input.placeholder = "TODO 占位";\n'
            '}\n'
        )
        hits = self._scan_snippet(body)
        self.assertTrue(hits, "扫描器漏掉了 placeholder 里的开发者措辞")

    def test_catches_html_text_node_pollution(self):
        """HTML 文本节点里的开发者措辞也要抓到。"""
        with tempfile.TemporaryDirectory(prefix="h3-scan-") as tmp:
            web = Path(tmp) / "web"
            web.mkdir()
            (web / "probe.html").write_text(
                '<div class="x">TODO 这里还没写完</div>\n', encoding="utf-8")
            hits = _scan_web_dir(web)
        self.assertTrue(hits, "扫描器漏掉了 HTML 文本节点里的开发者措辞")


class TestScannerNoFalsePositives(unittest.TestCase):
    """关卡 B：正常业务文案不得被报成污染（假阳性防护）。"""

    def _scan_snippet(self, js_body: str):
        with tempfile.TemporaryDirectory(prefix="h3-scan-") as tmp:
            web = Path(tmp) / "web"
            web.mkdir()
            (web / "probe.js").write_text(js_body, encoding="utf-8")
            return _scan_web_dir(web)

    def test_css_class_bar_is_not_pollution(self):
        """【实测过的真实假阳性】CSS 类名 `bar` / `bar-label` / `up-bar`
        撞上 `\\bbar\\b` -> 被报成「占位变量 foo/bar/baz」。

        第4轮扫描器与本轮第1版都栽在这上面。类名不是用户可见文案。
        """
        body = (
            'function barClass(pct) {\n'
            '  return pct >= 90 ? "bar danger" : "bar warn" : "bar";\n'
            '}\n'
            'function render() {\n'
            '  el.innerHTML = `<div class="bar-label"><span>x</span></div>`;\n'
            '}\n'
        )
        hits = self._scan_snippet(body)
        self.assertEqual([], hits,
                         f"CSS 类名被误报成污染：{hits}")

    def test_normal_user_copy_is_not_pollution(self):
        """正常用户提示语不得被报成污染。

        这些是**真实存在**的业务文案（实测过），
        它们含「暂时 / 占位符」等词，但句式是写给用户的。
        """
        samples = [
            'opt.textContent = "（暂时无法确认本机 ComfyUI 能力，不可用）";',
            'alert("User 模板必须包含 {prompt} 或 {original} 占位符");',
            'title = "暂时无法确认任务是否仍在运行";',
        ]
        for s in samples:
            hits = self._scan_snippet(s)
            self.assertEqual([], hits,
                             f"正常用户文案被误报成污染：{s} -> {hits}")

    def test_js_comments_are_ignored(self):
        """JS 注释里的开发者措辞**不是**污染（注释是本仓库该有的东西）。

        【为什么这条重要】第1版宽口径脚本因为没剥注释，
        报了 54 条候选 —— 其中绝大多数是注释内容。
        """
        body = (
            '// TODO: 这里以后要重构，先这么写着\n'
            '/* FIXME: 临时方案，仅供内部测试 */\n'
            'function ok() { return 1; }\n'
        )
        hits = self._scan_snippet(body)
        self.assertEqual([], hits,
                         f"注释里的内容被误报成污染：{hits}")

    def test_nested_template_string_is_parsed_correctly(self):
        """【实测过的真实 bug】模板串里嵌套模板串 + 对象字面量。

        providers.js 真实存在这种写法：
          `${p.has_api_key ? `<span class="k">${x}</span>` : ""}`
        第1版分词器在这里配对错乱，把后续整段代码当成字面量。
        """
        body = (
            'function render(p) {\n'
            '  const html = `<div>${p.has_api_key ? `<span class="k">${p.key}</span>` : ""}'
            '  <b>尾</b></div>`;\n'
            '  const after = "正常字符串";\n'
            '  return html + after;\n'
            '}\n'
        )
        hits = self._scan_snippet(body)
        self.assertEqual([], hits,
                         f"嵌套模板串解析异常：{hits}")


class TestScannerOnRealWebDir(unittest.TestCase):
    """关卡 C：真实 web/ 目录上跑一遍，记录当前基线。"""

    def test_real_web_dir_has_no_hard_pollution(self):
        """真实 web/ 目录里不得有**硬污染**。

        【判定口径】只把「明确是开发者内部说法」的算硬污染：
        TODO / FIXME / 内部日期标记 / 调试 traceback。
        「暂时 / 占位符 / bar」这类词在正常业务文案里合法出现，
        单独列一份「已知正常命中」白名单，不做断言。
        """
        findings = _scan_web_dir(REPO / "web")
        hard_markers = {"todo-marker", "debug-trace", "internal-date",
                        "git-trace", "stacktrace", "dummy-text",
                        "internal-ref"}
        hard = [f for f in findings if f["marker"] in hard_markers]
        self.assertEqual([], hard,
                         "真实 web/ 里有硬污染：\n  "
                         + "\n  ".join(f"{f['file']}:{f['line']} {f['text'][:100]}"
                                      for f in hard))


if __name__ == "__main__":
    unittest.main()
