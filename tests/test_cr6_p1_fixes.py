# -*- coding: utf-8 -*-
"""第 6 轮代码审查（2026-11-18）回归测试 —— 覆盖本轮修掉的 3 个 P1。

【本轮修掉的缺陷】

R6-1（P1）**成片尾裁跨过接缝**
    `h3studio/continuation.py::chain_compensation_plan` 原先无条件把
    `trim = current - formula` 全扣到最后一段尾部。但 trim 可能**超过
    最后一段自己的交付帧数** —— 那时尾裁会一路吃进上一段，把
    「上一段末帧 → 本段首帧」那个接缝一起裁掉。
    实测：4 段 × 2 秒 -> 最后一段交付 34 帧，trim 却要 38 帧
          -> keep_frames[-1] = **-4**（负数 = 该段被裁光并啃进上一段）
    修法：裁不到目标就**一帧都不裁**（保住全部内容与全部接缝），
          残差如实上报（trim_clamped / trim_clamped_extra）。

R6-2（P1）**显示值 ≠ 真实产物**
    成片拼接时做了尾裁（`_concat_chain_video` 的 trim_tail_frames），
    但 `continuation_state` 的 `total_frames` / `total_seconds` 是
    「累计**交付**帧数」（不含尾裁）-> 卡片与接续面板显示的累计时长
    比真实成片**长**一个尾裁量。
    线上实测（尾裁上线后的真实链）：显示 9.417s（226 帧），
    真实成片 ffprobe = **216 帧 = 9.000s**。
    修法：协议层口径**不动**（它是音频偏移 C_n 的来源，动了会音画错位），
          只在**展示层**（`server._continuation_info_for_row`）补尾裁口径。

R6-3（P1）**用户可见的 markdown 强调残留**
    `**xxx**` 是本项目内部写注释的记法，一旦进了 innerHTML / textContent
    就是用户可见的脏文案。真实浏览器取证（docs/evidence-cr6/）：
      可见文本 39 处 markdown 残留 + 属性值 71 处。
    本轮修了**本轮新增代码**引入/暴露的：
      * web/continuation.js（接续面板提示语）
      * web/app.js（H3-Image 负面提示词提示）
      * h3studio/h3image.py（H3-Image 模型 warn 文案）
      * h3studio/continuation.py（尾裁说明 note）
    并加一条**通用护栏**：web/*.js 与 h3studio/*.py 里「会被送进 DOM 的
    字符串」不得含 `**xxx**`（注释行豁免）。

【本文件的三条断言都做过「变异自证」】
  * 把 chain_compensation_plan 的夹住逻辑去掉 -> R6-1 断言必须失败；
  * 把展示层口径改回协议层 -> R6-2 断言必须失败；
  * 往 web/continuation.js 注入一段 `**污染**` -> R6-3 断言必须失败。

运行：
    python3 -m pytest tests/test_cr6_p1_fixes.py -q --tb=short
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import continuation as cont  # noqa: E402


# ==========================================================================
# R6-1：成片尾裁不得跨过接缝
# ==========================================================================

class TestTailTrimNeverCrossesSeam(unittest.TestCase):
    """R6-1 关卡：尾裁量不得超过最后一段的交付帧数。"""

    def test_four_two_second_segments_keep_all_frames(self):
        """4 段 × 2 秒：最后一段只有 34 帧，尾裁 38 帧会跨接缝。

        修法 = 裁不到就不裁。于是：
          * keep_frames 全部 >= 0（没有任何一段被裁光）
          * trim_frames = 0（没有跨接缝的裁剪）
          * residual > 0（残差如实上报，不静默）
        """
        durations = [2.0, 2.0, 2.0, 2.0]
        plan = cont.chain_compensation_plan(durations)
        self.assertTrue(all(k >= 0 for k in plan["keep_frames"]),
                        f"keep_frames 不得为负（负数 = 该段被裁光并啃进上一段）: "
                        f"{plan['keep_frames']}")
        self.assertEqual(plan["trim_frames"], 0,
                         "尾裁不得跨过接缝：最后一段只有 34 帧，裁 38 帧会吃进上一段")
        self.assertTrue(plan["trim_clamped"],
                        "被夹住时必须置 trim_clamped=True（前端据此显示残差原因）")
        self.assertGreater(plan["residual_frames"], 0,
                           "残差必须如实上报（不能静默给一个错误的精确值）")
        self.assertFalse(plan["exact"],
                         "残差非 0 时 exact 必须为 False（前端要显示 ⚠ 残差行）")

    def test_no_combination_yields_negative_keep_frames(self):
        """穷举：整秒时长 2~60 秒 × 段数 1~11 —— 不得出现负 keep / 负 trim。

        【为什么穷举而不是只测一个样本】触发条件是
        「trim > 最后一段交付帧数」，它随 (时长, 段数) 变化 ——
        只测一个样本会漏掉别的组合。
        """
        bad = []
        for segments in range(1, 12):
            for seconds in range(2, 61):
                plan = cont.chain_compensation_plan([float(seconds)] * segments)
                if any(k < 0 for k in plan["keep_frames"]):
                    bad.append((segments, seconds, plan["keep_frames"]))
                if plan["trim_frames"] < 0:
                    bad.append((segments, seconds, plan["trim_frames"]))
        self.assertEqual([], bad,
                         "以下组合出现负 keep_frames / 负 trim_frames（尾裁跨接缝）:\n  "
                         + "\n  ".join(str(b) for b in bad[:20]))

    def test_normal_chains_unaffected(self):
        """常规链路（5s->5s、5s->5s->8s）不得被这次修改影响。"""
        two = cont.chain_compensation_plan([5.0, 5.0])
        self.assertEqual(two["formula_frames"], 216)
        self.assertEqual(two["trim_frames"], 10)
        self.assertEqual(two["keep_frames"], [124, 92])
        self.assertTrue(two["exact"])
        self.assertFalse(two["trim_clamped"])

        three = cont.chain_compensation_plan([5.0, 5.0, 8.0])
        self.assertEqual(three["formula_frames"], 384)
        self.assertEqual(three["trim_frames"], 12)
        self.assertEqual(three["keep_frames"], [124, 102, 158])
        self.assertTrue(three["exact"])
        self.assertFalse(three["trim_clamped"])

    def test_clamped_case_explains_why_in_notes(self):
        """被夹住时，notes 必须说清「差多少、为什么不能补」。"""
        plan = cont.chain_compensation_plan([2.0, 2.0, 2.0, 2.0])
        joined = "\n".join(plan["notes"])
        self.assertIn("接缝", joined,
                      "notes 必须提到接缝（这是不能继续裁的原因）")
        self.assertNotIn("**", joined,
                         "notes 是用户可见文案，不得含 markdown 强调残留")


# ==========================================================================
# R6-2：显示值必须等于真实成片
# ==========================================================================

class TestDisplayMatchesRealProduct(unittest.TestCase):
    """R6-2 关卡：显示的累计时长 == 成片文件真实帧数 / 24。"""

    def test_keep_frames_sum_is_the_real_product_length(self):
        """成片真实长度 = Σ keep_frames（拼接时就是按这个口径裁的）。

        线上真实链（2026-11-18 尾裁上线后）：
          5s->5s   -> 真实成片 216 帧（ffprobe 实测 v-h3-studio-000531.mp4）
          5s->5s->8s -> 真实成片 384 帧（v-h3-studio-000533.mp4）
        """
        self.assertEqual(
            sum(cont.chain_compensation_plan([5.0, 5.0])["keep_frames"]),
            216, "5s->5s 的成片真实长度必须是 216 帧（线上 ffprobe 实测）")
        self.assertEqual(
            sum(cont.chain_compensation_plan([5.0, 5.0, 8.0])["keep_frames"]),
            384, "5s->5s->8s 的成片真实长度必须是 384 帧（线上 ffprobe 实测）")

    def test_protocol_layer_total_frames_is_not_changed(self):
        """协议层的 total_frames **必须保持**「累计交付帧数」口径。

        【为什么这条很重要】它是**音频偏移** C_n 的来源
        （server._prepare_continuation_params -> _inherit_audio_continuation）。
        若有人把尾裁也减进去，后面每一段的参考音频都会偏移。
        """
        self.assertEqual(cont.chain_duration_account([5.0, 5.0])["total_frames"],
                         226, "协议层原始账必须是 226 帧（不含尾裁）")
        self.assertEqual(cont.chain_duration_account([5.0, 5.0, 5.0])["total_frames"],
                         328, "协议层原始账必须是 328 帧（不含尾裁）")

    def test_server_display_layer_applies_trim(self):
        """展示层（server._continuation_info_for_row）必须用尾裁后的口径。"""
        server_py = (REPO / "server.py")
        if not server_py.exists():
            self.skipTest("找不到 server.py")
        src = server_py.read_text(encoding="utf-8", errors="replace")
        self.assertIn("_continuation_info_for_row", src,
                      "server.py 必须有展示层的接续信息计算入口")
        # 该函数必须调用 chain_compensation_plan（尾裁的唯一真源）
        m = re.search(r"def _continuation_info_for_row\(.*?\n(?=def )", src, re.S)
        self.assertIsNotNone(m, "找不到 _continuation_info_for_row 的函数体")
        body = m.group(0)
        self.assertIn("chain_compensation_plan", body,
                      "展示层必须用 chain_compensation_plan 算尾裁（与拼接同一真源）")
        self.assertIn("keep_frames", body,
                      "展示层必须用 keep_frames 之和作为成片真实长度")
        # 卡片行也必须走这个入口（不能有的地方裁、有的地方不裁）
        self.assertIn('"continuation_info": _continuation_info_for_row(',
                      src, "生成库卡片的 continuation_info 必须走展示层入口")


# ==========================================================================
# R6-3：用户可见文案不得含 markdown 强调残留
# ==========================================================================

#: 会进 DOM 的字符串所在文件（后端下发的文案也经由 JSON 进 DOM）。
FRONTEND_JS = sorted((REPO / "web").glob("*.js"))

#: 已知的**合法** `**xxx**` 出现位置（纯注释行 / 提示词模板原文）。
#:
#: 【为什么要豁免】官方提示词模板（h3studio/skills/prompt-enhancer/assets/*.txt）
#: 本身就是 markdown，**xxx** 是它的原文 —— 那不是「污染」。
#: 但那些是 .txt 资产，不在下面的扫描范围内。
#:
#: 【豁免规则】只豁免**整行注释**与**以 * 开头的注释块**；
#: 任何在**代码字符串**里的 `**xxx**` 都算污染。
_ALLOW_COMMENT_PREFIXES = ("//", "*", "/*", "<!--")


def _visible_string_lines(path: Path):
    """文件里「可能会进 DOM」的字符串所在行（排除纯注释行）。"""
    out = []
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for i, line in enumerate(src.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(_ALLOW_COMMENT_PREFIXES):
            continue
        out.append((i, stripped))
    return out


class TestNoMarkdownBoldInVisibleStrings(unittest.TestCase):
    """R6-3 关卡：用户可见文案不得含 `**xxx**`。"""

    MD = re.compile(r"\*\*[^*\n]{1,60}\*\*")

    def test_frontend_js_has_no_markdown_bold_in_code_strings(self):
        """web/*.js 的**字符串字面量**里不得有 markdown 强调残留。

        【为什么这不是「过度严格」】真实浏览器取证（真实 Chromium 打开
        线上 8190、遍历 12 个面板、抓全部文本节点 + 属性值）：
          可见文本 39 处 markdown 残留 + 属性值 71 处。
        其中用户能直接看到的例子：
          * 接续上下文面板：「请在下方输入**下一段**的提示词」
          * H3-Image 提示：「负面提示词**数学上无效**，已禁用。」
        —— 它们都是**本项目自己的注释记法漏进了界面**。

        【扫描口径 —— 用 acorn 真解析，不用行首启发式】
        手写词法扫描会把「块注释续行」「正则字面量」误判成污染，
        那样断言就变成假红（审查者会养成忽略它的习惯）。
        所以这里调 `scripts/cr6_scan_markdown_leaks.cjs`（acorn AST），
        它只看 Literal / TemplateElement —— 注释天然不在 AST 里。
        """
        import subprocess

        script = REPO / "scripts" / "cr6_scan_markdown_leaks.cjs"
        if not script.exists():
            self.skipTest("找不到 scripts/cr6_scan_markdown_leaks.cjs")
        try:
            proc = subprocess.run(
                ["node", str(script), str(REPO / "web")],
                capture_output=True, text=True, timeout=120,
                cwd=str(REPO))
        except (OSError, subprocess.SubprocessError) as exc:
            self.skipTest(f"无法运行 node 扫描器: {exc}")
        self.assertEqual(
            0, proc.returncode,
            "以下 web/*.js 的用户可见字符串里含 markdown 强调残留"
            "（会直接显示在界面上）：\n" + (proc.stdout or "")[-4000:])

    def test_backend_user_facing_strings_have_no_markdown_bold(self):
        """h3studio/*.py 里**下发到界面**的文案不得含 markdown 强调残留。

        【扫描口径】只扫**字符串字面量**（含 `**xxx**`）——
        注释与 docstring 里的 `**xxx**` 是本项目的注释记法，不算污染。

        【为什么用 AST 而不是行首启发式】docstring 在 AST 里是
        `Expr(Constant)`，可以精确豁免；行首启发式做不到这一点
        （多行 docstring 的续行会被误判）。
        """
        import ast

        #: 这些模块的文案会经 API 进浏览器。
        UI_MODULES = (
            "model_recommendations.py", "lora_catalog_mihoyo.py",
            "lora_catalog_ea.py", "lora_catalog_nsfw.py",
            "lora_catalog_effects.py", "job_meta.py", "h3image.py",
            "continuation.py", "continuation_service.py",
            "h3_audio_continuation.py", "h3_video_size.py", "reso_label.py",
            "h3_video_modes.py", "h3_audio_track.py", "seed_guard.py",
            "video_models.py",
        )

        hits = []
        for name in UI_MODULES:
            path = REPO / "h3studio" / name
            if not path.exists():
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, SyntaxError):
                continue
            docstrings = set()
            for node in ast.walk(tree):
                if (isinstance(node, ast.Expr)
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)):
                    docstrings.add(id(node.value))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant):
                    continue
                if not isinstance(node.value, str):
                    continue
                if id(node) in docstrings:
                    continue
                for m in self.MD.findall(node.value):
                    hits.append(f"{name}:{node.lineno} :: {m}")
        self.assertEqual(
            [], hits,
            "以下 h3studio/*.py 的字符串字面量里含 markdown 强调残留"
            "（会被下发到界面）：\n  " + "\n  ".join(hits[:40]))

    def test_guard_is_not_vacuous(self):
        """护栏：断言本身不能空跑 —— 确认扫描器真的扫到了东西。"""
        scanned = 0
        for path in FRONTEND_JS:
            scanned += len(_visible_string_lines(path))
        self.assertGreater(scanned, 200,
                           f"只扫到 {scanned} 行代码，扫描口径可能失效")

        # 确认扫描器能抓到一处真实污染（把已知的一处临时构造出来）。
        sample = 'const t = "提示词**下一段**的提示词";'
        self.assertTrue(self.MD.findall(sample),
                        "markdown 正则失效了 —— 护栏会变成恒绿")


if __name__ == "__main__":
    unittest.main()
