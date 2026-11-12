# -*- coding: utf-8 -*-
"""接续 + 复用参数的**混合态** —— 回归测试（2026-11-12 修）。

【这个测试在防什么】

独立复验发现：点过「接续」（context 挂在左侧）之后，又点**另一张**
卡片的「复用参数」，两个动作叠加出一个**混合态**：

  * 左侧控件 = B 卡片的参数（尺寸 1024×576、模型、步数…）
  * 提交载荷 = B 的 params + A 的 continuation（接力棒）

实测后果：Relay-Kit 的拷贝桥直接 raise
「拷贝桥禁止跨分辨率：上段 768x448 ≠ 本段 1024x576」，
而错误文本提取还有缺陷 -> 用户只看到
`'str' object has no attribute 'get'`。

三条红线：
  1. 点「复用参数」后，**接续上下文必须被清掉**（不留混合态）；
  2. 提交前**尺寸不一致必须被拦下**（不浪费一次生成）；
  3. 服务端**独立**校验尺寸（前端被绕过也要拦）。

运行：python3 -m pytest tests/test_continuation_mixed_state.py -v
"""

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import continuation as cont  # noqa: E402
from h3studio import continuation_service as cont_service  # noqa: E402


def _strip_js_comments(source: str) -> str:
    """去掉 JS 的行注释与块注释，只留**代码**。

    【为什么要这个辅助函数】源码级断言必须看代码而不是注释 ——
    注释里出现「alert(」只是解释为什么不用它，不是调用。
    """
    import re
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"//[^\n]*", "", without_block)


class TestSizeMismatchRule(unittest.TestCase):
    """协议层：分辨率必须一致（纯函数）。"""

    def test_mismatch_is_rejected_with_actionable_chinese(self):
        reason = cont.size_mismatch(
            {"width": 768, "height": 448},
            {"width": 1024, "height": 576})
        self.assertTrue(reason, "尺寸不一致必须给出原因")
        self.assertIn("768×448", reason)
        self.assertIn("1024×576", reason)
        # 必须告诉用户怎么办
        self.assertIn("复用参数", reason)

    def test_match_is_allowed(self):
        self.assertEqual(cont.size_mismatch(
            {"width": 768, "height": 448},
            {"width": 768, "height": 448}), "")

    def test_only_width_differs_is_rejected(self):
        self.assertTrue(cont.size_mismatch(
            {"width": 768, "height": 448},
            {"width": 1024, "height": 448}))

    def test_unknown_size_is_allowed(self):
        """无从比较 -> 放行（不臆造约束）。"""
        self.assertEqual(cont.size_mismatch({}, {"width": 1, "height": 1}), "")
        self.assertEqual(cont.size_mismatch({"width": 1, "height": 1}, {}), "")


class TestServerSideGuard(unittest.TestCase):
    """服务端：提交时必须独立校验（前端被绕过也拦得住）。"""

    def setUp(self):
        self.server = self._import_server()

    @staticmethod
    def _import_server():
        import importlib
        return importlib.import_module("server")

    def test_server_exposes_size_mismatch_guard(self):
        self.assertTrue(hasattr(self.server, "_continuation_size_mismatch"))

    def test_guard_delegates_to_protocol_layer(self):
        reason = self.server._continuation_size_mismatch(
            {"width": 768, "height": 448},
            {"width": 1024, "height": 576})
        self.assertIn("分辨率", reason)

    def test_guard_allows_matching_size(self):
        self.assertEqual(self.server._continuation_size_mismatch(
            {"width": 768, "height": 448},
            {"width": 768, "height": 448}), "")

    def test_prepare_continuation_params_calls_the_guard(self):
        """【源码级契约】_prepare_continuation_params 必须调用尺寸校验。

        为什么用源码级断言：这条校验一旦被删，前端两道闸仍能挡住
        正常用户路径，但**直接调 API** 的路径会重新变成「跑一次
        生成才失败」。这里锁住「服务端仍在校验」这个事实。
        """
        source = (REPO / "server.py").read_text(encoding="utf-8")
        self.assertIn("_continuation_size_mismatch(source_params, params)",
                      source,
                      "_prepare_continuation_params 里必须有尺寸一致性校验")


class TestFrontendMixedStateContract(unittest.TestCase):
    """前端契约（源码级）—— 两条路径必须存在且成对。"""

    @classmethod
    def setUpClass(cls):
        cls.app_js = (REPO / "web" / "app.js").read_text(encoding="utf-8")
        cls.cont_js = (REPO / "web" / "continuation.js").read_text(encoding="utf-8")
        cls.panel_js = (REPO / "web" / "video-panel.js").read_text(encoding="utf-8")

    def test_reuse_handler_clears_continuation_context(self):
        """【红线 1】点「复用参数」必须清掉接续上下文。"""
        marker = 'card.querySelector(\'[data-act="reuse"]\')'
        idx = self.app_js.find(marker)
        self.assertGreater(idx, 0, "找不到「复用参数」的点击处理")
        # 取该处理函数的前 800 字符（清 context 的调用必须在开头）
        block = self.app_js[idx:idx + 800]
        self.assertIn("clearForReuse", block,
                      "「复用参数」的处理里必须先清掉接续上下文")

    def test_continuation_exposes_clear_for_reuse(self):
        self.assertIn("function clearForReuse", self.cont_js)
        self.assertIn("clearForReuse,", self.cont_js,
                      "clearForReuse 必须导出到 window.Continuation")

    def test_submit_guard_is_wired(self):
        """【红线 2】提交前必须调一致性闸。"""
        self.assertIn("checkSubmitConsistency", self.cont_js)
        self.assertIn("checkSubmitConsistency", self.app_js,
                      "app.js 提交路径必须调用一致性闸")

    def test_submit_guard_can_block(self):
        """一致性闸必须能返回 false（中止提交）。"""
        self.assertIn("return false;", self.cont_js)

    def test_toast_never_falls_back_to_alert(self):
        """toast 不得退到 alert() —— alert 是阻塞的，会卡死页面/测试。

        只看**代码**，不看注释（注释里提到 alert() 是解释，不是调用）。
        """
        code = _strip_js_comments(self.cont_js)
        self.assertNotIn("alert(", code,
                         "continuation.js 的代码里不应有 alert（会阻塞页面）")
        # 自带的 toast 必须真的存在
        self.assertIn("function toast(", code)

    def test_accel_lora_is_reset_not_merged(self):
        """【混合态的又一种形态】applyConfig 必须**归零**加速 LoRA。

        旧格式 accel_loras 数组是「完整表达这个任务的配置」，不是增量。
        数组为空 = 这个任务没有额外加速 LoRA —— 若不清零，
        上一张卡片的 LoRA 会残留在面板上。

        【为什么断言要带上下文】`videoState.accelLora = "";` 在本文件里
        出现**两次**（另一处在「切了主模型后回落」的逻辑里，与本次修复
        无关）。只断言字符串存在会**匹配到错误的那一处** ——
        变异验证已经抓到过这个坑（变异改了归零，断言却没红）。
        所以这里断言的是「归零那一行**紧跟在**旧格式分支的条件后面」。
        """
        code = _strip_js_comments(self.panel_js)
        self.assertIn(
            'if (!hasNewAccel && Array.isArray(cfg.accel_loras)) {\n'
            '      videoState.accelLora = "";',
            code,
            "applyConfig 的旧格式分支必须先归零 accel_lora")

    def test_has_new_accel_flag_exists(self):
        """新格式的「显式给了」标记必须存在（区分「空串」与「没给」）。"""
        code = _strip_js_comments(self.panel_js)
        self.assertIn("const hasNewAccel = cfg.accel_lora !== undefined;",
                      code)


class TestContinuationContextPayload(unittest.TestCase):
    """接续上下文载荷里必须带尺寸（前端闸要用）。"""

    def test_context_params_include_width_height(self):
        params = {
            "model_key": "fl2va_int8",
            "width": 768, "height": 448,
            "duration": 5.0, "steps": 4,
            "accel_loras": [], "twopass": {"enabled": False},
        }
        context = cont.build_context(params, "t2v")
        self.assertEqual(context["params"]["width"], 768)
        self.assertEqual(context["params"]["height"], 448)

    def test_context_params_exclude_prompt(self):
        """提示词必须不在上下文里（用户要写下一段）。"""
        params = {"prompt": "上一段的提示词", "width": 768, "height": 448}
        context = cont.build_context(params, "t2v")
        self.assertNotIn("prompt", context["params"])
        self.assertEqual(context["prompt"], "")


if __name__ == "__main__":
    unittest.main()
