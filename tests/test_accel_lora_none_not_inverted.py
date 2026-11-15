# -*- coding: utf-8 -*-
"""回归测试：t2v「不挂加速 LoRA」不能被静默反转成「挂 ckpt850」。

【缺陷（2026-11-15 代码审查发现，Critical）】
前端 `web/video-panel.js::submitParams()` 对「不挂加速 LoRA」提交的是
``accel_loras: []``（**没有** ``accel_lora`` 键）。

而 `h3studio/video_models.py::resolve_accel_lora_slot()` 的四条分支：
  1. ``"accel_lora" in params``  -> False（键不存在）
  2. ``accel_loras`` 数组        -> 空，for 循环不执行
  3. ``fixed_accel_lora``        -> 没有
  4. ``lora``                    -> 没有
全不命中 -> 落到兜底 ``return VIDEO_FIXED_ACCEL_LORA_DEFAULT`` = ``"ckpt850"``。

后果：**用户明确选了「不挂」，工作流里却挂着 4 步蒸馏 LoRA** ——
画面/步数语义与用户的选择相反，且没有任何提示。

【修法】
``accel_loras`` **键存在**即视为用户显式表达过意图（与 ``accel_lora`` 同源）：
数组存在但为空 = 显式「不挂」。

【测试为什么这样写】
不能只测「空数组 -> 不挂」这一条 —— 那样「把整个函数改成永远返回空」
也能蒙混过关。这里把**全部七种输入形态**都列出来，逐条断言，
包括「没有任何加速信息 -> 仍然走默认 ckpt850」这条（防止过度修复）。

运行：
    python3 -m pytest tests/test_accel_lora_none_not_inverted.py -q --tb=short
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio.video_models import resolve_accel_lora_slot  # noqa: E402


class TestAccelLoraNoneNotInverted(unittest.TestCase):
    """「不挂」不能被反转成「挂 ckpt850」。"""

    def test_empty_accel_loras_array_means_none(self):
        """前端 t2v 的「不挂」形态：accel_loras: []（无 accel_lora 键）。"""
        key, _strength = resolve_accel_lora_slot({"accel_loras": []})
        self.assertEqual("", key,
                         "空 accel_loras 数组被反转成挂加速 LoRA —— "
                         "用户选了「不挂」，工作流里却挂着 ckpt850")

    def test_explicit_empty_accel_lora_means_none(self):
        """flf / r2v 的「不挂」形态：accel_lora: ""。"""
        key, _ = resolve_accel_lora_slot({"accel_lora": ""})
        self.assertEqual("", key)

    def test_legacy_lora_none_means_none(self):
        key, _ = resolve_accel_lora_slot({"accel_loras": [], "lora": "none"})
        self.assertEqual("", key)

    def test_explicit_accel_lora_is_used(self):
        """显式选了某枚就必须用它（别把修复做成「永远不挂」）。"""
        key, strength = resolve_accel_lora_slot(
            {"accel_loras": [{"key": "ckpt850", "strength": 1.0,
                              "enabled": True}]})
        self.assertEqual("ckpt850", key)
        self.assertAlmostEqual(1.0, strength, places=3)

    def test_disabled_entry_means_none(self):
        """被禁用的条目 = 不挂（不能回落到默认 ckpt850）。"""
        key, _ = resolve_accel_lora_slot(
            {"accel_loras": [{"key": "ckpt850", "strength": 1.0,
                              "enabled": False}]})
        self.assertEqual("", key)

    def test_no_accel_info_falls_back_to_default(self):
        """【防止过度修复】完全没有加速信息的老任务仍走默认 ckpt850。

        【为什么这条重要】老任务（历史落库数据）根本没有加速 LoRA 字段，
        它们的语义是「工作流固定挂 ckpt850」。如果把判据改成
        「没有明确的就一律不挂」，老任务重放出来的画面会变样 ——
        那是比原缺陷更严重的行为变更。
        """
        key, _ = resolve_accel_lora_slot({})
        self.assertEqual("ckpt850", key,
                         "没有任何加速信息的输入必须仍走默认 ckpt850 "
                         "（老任务重放语义不变）")

    def test_legacy_fixed_accel_lora_still_used(self):
        """老格式 fixed_accel_lora 仍按原语义取用。"""
        key, _ = resolve_accel_lora_slot({"fixed_accel_lora": "ckpt850"})
        self.assertEqual("ckpt850", key)


class TestFrontendSubmitShape(unittest.TestCase):
    """前端「不挂」的实际提交形态必须被后端认出来。

    【为什么单列】上面的用例是**手写**的输入形态。如果前端哪天改了
    提交形态（比如改成只发 accel_lora 而不发数组），后端的判据必须
    跟着变 —— 而这种「前后端契约」最容易悄悄漂移。
    这里直接从 web/video-panel.js 的源码里读出真实提交形态。
    """
    def test_frontend_sends_accel_loras_array(self):
        import re
        src = (REPO / "web" / "video-panel.js").read_text(encoding="utf-8")
        m = re.search(r"function submitParams\(\)", src)
        self.assertIsNotNone(m, "找不到 submitParams()")
        i = src.index("{", m.start())
        depth = 0
        j = i
        while j < len(src):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        body = src[m.start(): j + 1]
        self.assertIn("accel_loras:", body,
                      "submitParams() 不再提交 accel_loras —— "
                      "后端判据需要同步复核")
        # 「不挂」时前端发的是空数组
        self.assertRegex(body, r"accel_loras:\s*accel\b",
                         "accel_loras 不是从 accel 数组派生的 —— "
                         "前端形态变了，后端判据要复核")

    def test_backend_handles_the_real_frontend_shape(self):
        """把前端的真实形态喂给后端，必须得到「不挂」。"""
        import re
        src = (REPO / "web" / "video-panel.js").read_text(encoding="utf-8")
        m = re.search(r"const accel = \[\];(.*?)const effectLoras", src, re.S)
        self.assertIsNotNone(m, "找不到前端 accel 数组的构建逻辑")
        # 前端只有在 videoState.accelLora 非空且强度 > 0 时才 push
        self.assertIn("videoState.accelLora", m.group(1))
        self.assertIn("enabled: true", m.group(1))
        # 「不挂」-> accel 数组为空 -> 后端必须认成「不挂」
        key, _ = resolve_accel_lora_slot({"accel_loras": []})
        self.assertEqual("", key)


if __name__ == "__main__":
    unittest.main()
