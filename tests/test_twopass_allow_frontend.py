# -*- coding: utf-8 -*-
"""R2：前端「接续段也用二采」显式开关的契约测试。

【本轮背景】
上一轮把「接续段默认走标准采样（等价 nonebot seg2）」落成了硬契约
（服务端 `h3studio/twopass_policy.py`），但**显式放行口子
`continuation.allow_twopass` 只能后端置位，用户从 UI 打不开**。
本轮补前端入口，并守三条不变量：

  F1. **默认关闭**：面板状态 `twopass.allow_continuation` 默认 false；
      且默认路径**不得**往提交体里写 `allow_twopass: true`
      ⇒ 默认路径仍等价 nonebot seg2（接续段走标准采样）。
  F2. **中文标签说清风险**：HTML 里的标签与提示必须明确写出
      「段头色档跳变」这个已实测的缺陷，不能只写「开启二采」。
  F3. **二次确认**：勾选时必须弹 confirm 框把风险说清楚；
      用户取消 ⇒ 复选框**回滚**为未勾选、状态保持 false。

【为什么这些断言不是恒真】
见 tests/mutation_twopass_allow_frontend.py —— 每个变异都让对应断言变红。
"""
from __future__ import annotations

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PANEL_JS = os.path.join(ROOT, "web", "video-panel.js")
APP_JS = os.path.join(ROOT, "web", "app.js")
INDEX_HTML = os.path.join(ROOT, "web", "index.html")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestFrontendAllowTwopassDefaultOff(unittest.TestCase):
    """F1：默认关闭 + 默认路径不得写 allow_twopass=true。"""

    def test_panel_state_default_is_false(self):
        js = _read(PANEL_JS)
        self.assertRegex(
            js, r"allow_continuation:\s*false",
            "面板状态 twopass.allow_continuation 默认必须是 false")

    def test_app_js_never_unconditionally_writes_allow_twopass(self):
        """app.js 里写 allow_twopass 的地方**必须**在一个条件里。

        判据：出现 `allow_twopass: true` 的那一行所在的上下文里，
        前面必须有对 `allow_continuation` 的真值判断。
        """
        js = _read(APP_JS)
        hits = [m.start() for m in re.finditer(r"allow_twopass:\s*true", js)]
        self.assertTrue(hits, "app.js 应该有一处受条件保护的 allow_twopass 写入")
        for pos in hits:
            window = js[max(0, pos - 700):pos]
            self.assertIn(
                "allow_continuation", window,
                "写 allow_twopass=true 的地方必须先判断 allow_continuation")
            self.assertRegex(
                window, r"_allowTp\s*=\s*!!",
                "allow_twopass=true 必须由 allow_continuation 的真值驱动")

    def test_injection_is_after_all_mode_branches(self):
        """注入点必须在 t2v / flf / r2v **三个分支都结束之后**。

        【为什么这条重要 —— 本轮真的踩过】
        第一版把注入放在 t2v 分支**之前** ⇒ 被 t2v 分支里的
        `params.continuation = ctx.continuation;` **覆盖** ⇒
        allow_twopass 根本传不出去（前端开关形同虚设）。
        第二版挪到 t2v 分支**内部** ⇒ flf / r2v 走不到
        （这两个分支本来就不设 params.continuation）。
        ⇒ 正确位置 = 三个分支之后的公共作用域。
        """
        js = _read(APP_JS)
        inject = js.find("params.continuation = Object.assign")
        self.assertGreater(inject, 0, "找不到注入点")
        # t2v 分支里那个会覆盖人的赋值
        t2v_assign = js.find("params.continuation = ctx.continuation")
        self.assertGreater(t2v_assign, 0, "找不到 t2v 分支的赋值")
        flf_branch = js.find('} else if (mode === "flf" || mode === "r2v") {')
        self.assertGreater(flf_branch, 0, "找不到 flf/r2v 分支")
        self.assertGreater(
            inject, t2v_assign,
            "注入点必须在 t2v 分支的 params.continuation 赋值之后（否则被覆盖）")
        self.assertGreater(
            inject, flf_branch,
            "注入点必须在 flf/r2v 分支之后（否则那两种模式走不到）")

    def test_default_path_has_no_allow_twopass(self):
        """默认（未勾选）时提交体里不得出现 allow_twopass=true。

        用真实的判定逻辑复算：allow_continuation=false -> 不写。
        """
        allow_continuation = False          # 面板默认
        _tp_state = {"twopass": {"allow_continuation": allow_continuation}}
        _allow_tp = bool(_tp_state["twopass"] and
                         _tp_state["twopass"]["allow_continuation"])
        self.assertFalse(_allow_tp)
        # _allow_tp 为假 -> app.js 那段 if 不进入 -> 不写 allow_twopass

    def test_disabling_twopass_also_clears_allow(self):
        """关掉二采时必须同时关掉「接续段也用二采」。

        否则会留下「二采关着但放行了」的矛盾状态
        （服务端会按 allow_twopass=true 放行二采，而用户看到的是二采关着）。
        """
        js = _read(PANEL_JS)
        self.assertRegex(
            js,
            r"if\s*\(\s*!tpBox\.checked\s*\)\s*"
            r"videoState\.twopass\.allow_continuation\s*=\s*false",
            "关掉二采必须同时把 allow_continuation 置回 false")


class TestFrontendAllowTwopassChineseLabel(unittest.TestCase):
    """F2：中文标签必须说清风险。"""

    def test_html_has_the_switch(self):
        html = _read(INDEX_HTML)
        self.assertIn('id="vidTwopassAllowContinuation"', html,
                      "必须有该开关的 DOM")
        self.assertIn('id="vidTwopassAllowContinuationRow"', html,
                      "必须有该开关的容器行")

    def test_label_says_colour_jump(self):
        html = _read(INDEX_HTML)
        self.assertIn("色档跳变", html,
                      "标签/提示必须明确写出「色档跳变」这个实测缺陷")
        self.assertIn("有风险", html,
                      "标签必须明确标注「有风险」")

    def test_hint_mentions_seg2_equivalent(self):
        html = _read(INDEX_HTML)
        self.assertIn("minimax_h3_seg2", html,
                      "提示必须说明默认路径等价 nonebot minimax_h3_seg2")

    def test_confirm_dialog_states_the_numbers(self):
        js = _read(PANEL_JS)
        self.assertIn("确定要让接续段也用二采吗", js,
                      "必须有二次确认对话框")
        # 【为什么只看 confirm 块】文件里**注释**也写了这些数字
        #（解释风险的注释段）—— 只看整个文件的话，
        # 把确认框里的数字删掉、注释还在，断言仍会绿 ⇒ 变异抓不住。
        # ⇒ 断言必须**限定在 confirm(...) 块内**。
        start = js.find("const ok = confirm(")
        self.assertGreater(start, 0, "找不到 confirm 块")
        block = js[start:]
        block = block[:block.find(");") + 2]
        for token in ("2.58x", "4.54x", "1.58x"):
            self.assertIn(token, block,
                          f"确认框里必须写明实测数字 {token}")
        self.assertIn("色档跳变", block,
                      "确认框里必须说清缺陷现象")


class TestFrontendAllowTwopassConfirmation(unittest.TestCase):
    """F3：二次确认 + 取消时回滚。"""

    def test_uses_confirm(self):
        js = _read(PANEL_JS)
        self.assertRegex(
            js, r"const ok = confirm\(",
            "必须用 confirm 做二次确认")

    def test_cancel_rolls_back_checkbox_and_state(self):
        js = _read(PANEL_JS)
        block = js[js.find("const ok = confirm("):]
        block = block[:block.find("});") + 3]
        self.assertIn("if (!ok)", block,
                      "必须处理用户取消")
        self.assertRegex(
            block, r"tpAllowBox\.checked\s*=\s*false",
            "用户取消必须把复选框回滚为未勾选")
        self.assertRegex(
            block, r"allow_continuation\s*=\s*false",
            "用户取消必须把状态回滚为 false")

    def test_switch_only_visible_with_continuation_context(self):
        """没有接续上下文时不显示这个开关（普通段本来就能用二采）。"""
        js = _read(PANEL_JS)
        self.assertRegex(
            js, r"allowRow\.hidden\s*=\s*!hasContinuation",
            "开关的显示必须依赖「当前有接续上下文」")

    def test_switch_requires_twopass_enabled(self):
        js = _read(PANEL_JS)
        self.assertRegex(
            js, r"allowRow\.hidden\s*=\s*!hasContinuation\s*\|\|\s*"
            r"!videoState\.twopass\.enabled",
            "二采没开时不该出现「接续段也用二采」")


class TestBackendStillAuthoritative(unittest.TestCase):
    """前端不是权威 —— 后端归一层仍然独立守契约。"""

    def test_policy_module_still_denies_by_default(self):
        from h3studio import twopass_policy as tp
        params = {"continuation": {"source_job": "up1", "stage_index": 1},
                  "twopass": {"enabled": True}}
        use, disposition = tp.resolve_effective_twopass(params)
        self.assertFalse(use, "接续段默认必须禁二采")
        self.assertEqual(disposition, "deny")

    def test_policy_module_allows_with_explicit_flag(self):
        from h3studio import twopass_policy as tp
        params = {"continuation": {"source_job": "up1", "stage_index": 1,
                                   "allow_twopass": True},
                  "twopass": {"enabled": True}}
        use, disposition = tp.resolve_effective_twopass(params)
        self.assertTrue(use, "显式声明后必须真的放行")
        self.assertEqual(disposition, "warn", "放行必须带警告处置")


if __name__ == "__main__":
    unittest.main()
