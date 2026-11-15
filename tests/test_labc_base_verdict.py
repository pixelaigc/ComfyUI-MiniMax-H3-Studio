# -*- coding: utf-8 -*-
"""L-C：`base_verdict` 字段（按实测结论推导，绝不是常量）。

【这个测试在防什么】
上一轮遗留项：`base_verdict` 字段未补 —— 目录里只有 77 条登记了
「原生 / 近似适用」，其余条目一律空 -> 界面只能显示「未标注」。

补的时候最容易犯的错是**新增一个常量为 true 的假值**（比如所有
条目一律写「原生」）—— 那比空值更糟：它把「本机实测可用」伪装成
「上游声明原生」。

本测试锁死：
  1. 每条条目都**有** base_verdict（不再有空值）
  2. 判定**随实测结论变化** —— 三类真实输入三种结论：
       可适配   -> 不得是「不适配」/「不可用」
       不可适配 -> 必须判「不适配」（不是「原生」！）
       broken   -> 必须判「不可用」
  3. 目录登记过的结论（原生 / 近似适用）被保留
  4. 判定函数是**纯函数**：同样输入 -> 同样输出

运行：python3 -m pytest tests/test_labc_base_verdict.py -v
"""
import inspect
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import loras as lora_registry  # noqa: E402


class TestBaseVerdictResolver(unittest.TestCase):
    """`_resolve_base_verdict` 的判定纪律。"""

    def test_verdict_is_never_empty(self):
        """每种输入都必须给出结论 —— 不允许空值。"""
        cases = [
            ({}, {"compatible": True, "attention_hit": 1.0}),
            ({}, {"compatible": True, "attention_hit": 0.5}),
            ({}, {"compatible": False, "attention_hit": 0.0}),
            ({}, {"compatible": True, "attention_hit": 1.0,
                  "conversion_status": "broken"}),
            ({"base_verdict": "原生"}, {"compatible": True,
                                        "attention_hit": 1.0}),
            ({"base_verdict": "近似适用"}, {"compatible": True,
                                            "attention_hit": 1.0}),
        ]
        for info, compat in cases:
            verdict = lora_registry._resolve_base_verdict(info, compat)
            self.assertTrue(verdict,
                            f"base_verdict 不能为空：info={info} compat={compat}")

    def test_compatible_maps_to_usable_verdict(self):
        """可适配 -> 不是「不适配」也不是「不可用」。"""
        v = lora_registry._resolve_base_verdict(
            {}, {"compatible": True, "attention_hit": 1.0})
        self.assertNotIn(v, (lora_registry.BASE_VERDICT_INCOMPATIBLE,
                             lora_registry.BASE_VERDICT_BROKEN))
        self.assertEqual(v, lora_registry.BASE_VERDICT_VERIFIED)

    def test_incompatible_maps_to_incompatible_verdict(self):
        """不可适配 -> 必须判「不适配」，绝不能是「原生」。"""
        v = lora_registry._resolve_base_verdict(
            {}, {"compatible": False, "attention_hit": 0.0})
        self.assertEqual(v, lora_registry.BASE_VERDICT_INCOMPATIBLE)
        self.assertNotEqual(v, lora_registry.BASE_VERDICT_NATIVE)
        self.assertNotEqual(v, lora_registry.BASE_VERDICT_VERIFIED)

    def test_broken_maps_to_unusable_verdict(self):
        """broken（文件本体损坏）-> 必须判「不可用」。"""
        v = lora_registry._resolve_base_verdict(
            {}, {"compatible": False, "attention_hit": 0.0,
                "conversion_status": "broken"})
        self.assertEqual(v, lora_registry.BASE_VERDICT_BROKEN)
        self.assertNotEqual(v, lora_registry.BASE_VERDICT_NATIVE)

    def test_broken_beats_declared_native(self):
        """目录说「原生」但实测 broken -> 以实测为准（不得写「原生」）。"""
        v = lora_registry._resolve_base_verdict(
            {"base_verdict": "原生"},
            {"compatible": False, "attention_hit": 0.0,
             "conversion_status": "broken"})
        self.assertEqual(v, lora_registry.BASE_VERDICT_BROKEN)

    def test_declared_verdicts_are_preserved(self):
        """目录登记过的结论（原生 / 近似适用）必须保留。"""
        for declared in (lora_registry.BASE_VERDICT_NATIVE,
                         lora_registry.BASE_VERDICT_NEAR):
            v = lora_registry._resolve_base_verdict(
                {"base_verdict": declared},
                {"compatible": True, "attention_hit": 1.0})
            self.assertEqual(v, declared)

    def test_partial_hit_is_incompatible(self):
        """注意力命中 < 1.0 -> 半静默失效 -> 判「不适配」。"""
        v = lora_registry._resolve_base_verdict(
            {}, {"compatible": True, "attention_hit": 0.75})
        self.assertEqual(v, lora_registry.BASE_VERDICT_INCOMPATIBLE)

    def test_verdict_is_pure(self):
        """同样输入 -> 同样输出（判定不是随机的、不是写死的）。"""
        info = {}
        compat = {"compatible": True, "attention_hit": 1.0}
        first = lora_registry._resolve_base_verdict(info, compat)
        for _ in range(5):
            self.assertEqual(
                lora_registry._resolve_base_verdict(dict(info), dict(compat)),
                first)

    def test_verdict_varies_with_input(self):
        """判定随输入变化 —— 证明不是常量。"""
        verdicts = {
            lora_registry._resolve_base_verdict(
                {}, {"compatible": True, "attention_hit": 1.0}),
            lora_registry._resolve_base_verdict(
                {}, {"compatible": False, "attention_hit": 0.0}),
            lora_registry._resolve_base_verdict(
                {}, {"compatible": False, "attention_hit": 0.0,
                    "conversion_status": "broken"}),
        }
        self.assertGreaterEqual(len(verdicts), 3,
                                "三类输入必须给三类结论 —— 判定不能是常量")


class TestBaseVerdictInPayload(unittest.TestCase):
    """清单 payload 里 base_verdict 字段必须存在且有意义。"""

    def test_payload_includes_base_verdict_key(self):
        """两条 payload 构造处都必须写 base_verdict。"""
        src = inspect.getsource(lora_registry)
        self.assertIn("_resolve_base_verdict(info, compat)", src)
        # 不允许再有「直接取目录、没有实测兜底」的写法
        self.assertNotIn('"base_verdict": info.get("base_verdict", "")', src,
                         "base_verdict 必须由 _resolve_base_verdict 推导，"
                         "不能直接取目录值（目录没登记就是空）")


if __name__ == "__main__":
    unittest.main()
