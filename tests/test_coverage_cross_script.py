# -*- coding: utf-8 -*-
"""要素保留率的跨书写系统判据（2026-11-10 审查返修发现的问题）。

【问题】
`max_token` 分支的 `ce63001` 把「原文要素保留率」这个**逐字比对**指标
接到了 /api/enhance 的响应里，并让前端显示它。
但 `moss-image` 模板的官方口径是「**默认用英文描述画面**」
（h3studio/skills/prompt-enhancer/assets/moss-image.txt 第 4 条）——
于是「中文原文 → 英文产出」必然一个都匹配不上，前端显示成
「原文要素保留率 0%（0/3 项）」+「未逐字命中的原文片段：…」
用户会误以为「要素全丢了」，而实际上产出是**正确**的。

【契约】
1. 原文与产出的书写系统不同（cjk vs latin）时，这个指标**不适用**：
   返回 applicable=False、ratio=None；
2. 书写系统相同时，行为不变（applicable=True，ratio 照常算）；
3. 前端在 applicable=False 时**不显示**这个数字。

运行：python3 -m pytest tests/test_coverage_cross_script.py -v
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import h3studio.enhancer as enh  # noqa: E402


HK_PROMPT = ("一位年轻亚洲女性站立在狭窄的香港后街人行道边缘，身穿米白色高领针织衫，"
             "外搭深红色灯芯绒西装外套，背景是港式老旧街道，左侧一排骑楼建筑，"
             "地面潮湿沥青路面反射昏暗光线。")


class TestScriptDetection(unittest.TestCase):
    def test_cjk_text(self):
        self.assertEqual(enh._script_of("这是一段中文"), "cjk")

    def test_latin_text(self):
        self.assertEqual(enh._script_of("This is an English paragraph."), "latin")

    def test_empty_text(self):
        self.assertEqual(enh._script_of(""), "empty")
        self.assertEqual(enh._script_of("   "), "empty")

    def test_mixed_prefers_majority(self):
        # 计数口径：CJK_RUN 数的是**连续汉字片段的个数**（不是字数），
        # EN_WORD 数的是**英文单词数**（>=4 字母）。
        # 「中文中文中文 mixed english words here」= 1 个汉字片段 vs 3 个英文单词
        # -> 判 latin。
        self.assertEqual(enh._script_of("中文中文中文 mixed english words here"), "latin")
        # 汉字片段更多时判 cjk
        self.assertEqual(
            enh._script_of("中文一 中文二 中文三 中文四 english"), "cjk")


class TestCrossScriptCoverage(unittest.TestCase):
    def test_cross_script_is_not_applicable(self):
        """中文原文 vs 英文产出 -> 指标不适用（不是 0%）。"""
        rep = enh.check_element_coverage(
            HK_PROMPT,
            "A young Asian woman standing at the edge of a narrow Hong Kong "
            "backstreet, wearing a cream turtleneck knit sweater and a dark red "
            "corduroy jacket. Behind her are weathered shophouses with rusted "
            "iron railings and neon signs; the damp asphalt road reflects dim light.")
        self.assertFalse(rep["applicable"],
                         "跨书写系统时指标应标记为不适用")
        self.assertIsNone(rep["ratio"], "不适用时不应给出 ratio")
        self.assertEqual(rep["missing_count"], 0)
        self.assertTrue(rep["reason"], "应给出可读原因")

    def test_same_script_still_computed(self):
        """同书写系统时行为不变。"""
        rep = enh.check_element_coverage(HK_PROMPT, "骑楼 霓虹 海报 沥青 雨雾 针织衫 灯芯绒")
        self.assertTrue(rep["applicable"], "同书写系统时指标应可用")
        self.assertIsNotNone(rep["ratio"])
        self.assertGreater(rep["total"], 0)

    def test_empty_enhanced_is_applicable(self):
        rep = enh.check_element_coverage(HK_PROMPT, "")
        self.assertTrue(rep["applicable"])

    def test_coverage_report_propagates_applicable(self):
        """coverage_report 必须把 applicable / reason 透出去。"""
        rep = enh.coverage_report(
            HK_PROMPT,
            "A young Asian woman in a cream turtleneck near shophouses, "
            "neon signs, damp asphalt reflecting dim light.",
            finish_reason="stop", validate_mode="pe")
        self.assertFalse(rep["applicable"])
        self.assertIsNone(rep["ratio"])
        self.assertTrue(rep["reason"])
        # 截断判定不受影响
        self.assertFalse(rep["truncated"])

    def test_english_original_and_english_output_computed(self):
        """英文原文 + 英文产出 -> 正常算（不能被误判成不适用）。"""
        rep = enh.check_element_coverage(
            "a young asian woman in a cream turtleneck on a narrow hong kong backstreet",
            "a young asian woman in a cream turtleneck stands on a narrow hong kong backstreet")
        self.assertTrue(rep["applicable"])
        self.assertIsNotNone(rep["ratio"])


class TestFrontendHidesInapplicableCoverage(unittest.TestCase):
    """前端：applicable=false 时不显示这个数字。"""

    def test_frontend_checks_applicable(self):
        src = (REPO / "web" / "providers.js").read_text(encoding="utf-8")
        self.assertIn("cov.applicable !== false", src,
                      "前端必须判断 cov.applicable")
        # 不再有裸的 `if (cov && cov.total)` 判据
        self.assertNotIn("if (cov && cov.total) {", src,
                         "前端不应在 applicable 为 false 时仍显示保留率")


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
