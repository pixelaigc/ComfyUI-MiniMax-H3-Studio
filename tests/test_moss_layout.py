# -*- coding: utf-8 -*-
"""MOSS-Image 结构化（layout）增强的自动化测试。

覆盖三块（对应本轮任务的验收点）：

1. **schema 校验** —— layout 草稿的字段名/类型/层级/可选项/示例，
   逐条对着官方 ComfyUI-Moss 的 ``defaults/layout.md`` 与
   ``scripts/compile_prompt.py`` 写死。
   非法字段必须报错并给出**可读原因**（不是「invalid」这种黑盒）。

2. **layout -> 文本翻译** —— 确定性：同一份 layout 永远得到同一段文本；
   位置/尺度/属性/材质/镜头光照逐条落到自然语言；图内文字逐字保留。

3. **错误回退** —— 模型返回空 / 非 JSON / JSON 非法 / 画布不符 /
   provider 不可达 —— 一律抛可读异常，**绝不返回半成品**。

另外还锁了一条**上游一致性**：本项目的坐标编译结果必须与官方
``scripts/compile_prompt.py`` 逐字节一致（若插件在本机存在）。
这不是自证 —— 断言的期望值来自**上游真程序的输出**。

运行：python3 -m pytest tests/test_moss_layout.py -v
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import moss_layout as ML  # noqa: E402

# 官方 ComfyUI-Moss 插件在本机的位置（用于上游一致性对照）
UPSTREAM_ROOT = Path("/home/ubuntu/comfyui/custom_nodes/ComfyUI-Moss")
UPSTREAM_COMPILER = UPSTREAM_ROOT / "scripts" / "compile_prompt.py"


def sample_draft(width=720, height=1280):
    """一份合法的 layout 草稿（绝对像素 xyxy，左上原点）。"""
    return {
        "width": width,
        "height": height,
        "bbox_coord_space": "abs",
        "bbox_format": "xyxy",
        "summary": {
            "caption": "A minimalist vertical poster with a deep navy night sky.",
            "meta": ["poster"],
            "canvas": "single",
            "palette": ["deep navy", "warm gold"],
        },
        "items": [
            {
                "label": "object",
                "caption": "A pale cream full moon with soft crater texture.",
                "bbox": [220, 180, 500, 540],
                "tags": ["moon", "luminous"],
                "palette": ["cream"],
            },
            {
                "label": "text",
                "caption": "Bold serif title characters in warm gold.",
                "text": "月光 MOSS",
                "bbox": [160, 820, 560, 960],
            },
        ],
    }


# ------------------------------------------------------------------ schema 校验


class TestLayoutSchema(unittest.TestCase):
    """layout 草稿的字段名 / 类型 / 层级 / 可选项，逐条写死。"""

    def test_valid_draft_passes(self):
        result = ML.validate_layout(sample_draft())
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["errors"], [])

    def test_root_requires_exactly_six_fields(self):
        draft = sample_draft()
        del draft["bbox_format"]
        result = ML.validate_layout(draft)
        self.assertFalse(result["ok"])
        self.assertTrue(any("bbox_format" in e for e in result["errors"]), result["errors"])

    def test_root_rejects_unknown_field(self):
        draft = sample_draft()
        draft["extra"] = 1
        result = ML.validate_layout(draft)
        self.assertFalse(result["ok"])
        self.assertTrue(any("未知字段" in e and "extra" in e for e in result["errors"]),
                        result["errors"])

    def test_bbox_coord_space_must_be_abs(self):
        draft = sample_draft()
        draft["bbox_coord_space"] = "abs_centralized"
        result = ML.validate_layout(draft)
        self.assertFalse(result["ok"])
        self.assertTrue(any("bbox_coord_space" in e for e in result["errors"]),
                        result["errors"])

    def test_bbox_format_must_be_xyxy(self):
        draft = sample_draft()
        draft["bbox_format"] = "yxyx"
        result = ML.validate_layout(draft)
        self.assertFalse(result["ok"])
        self.assertTrue(any("bbox_format" in e for e in result["errors"]), result["errors"])

    def test_dimensions_must_be_multiple_of_16(self):
        draft = sample_draft(width=720, height=1270)
        result = ML.validate_layout(draft)
        self.assertFalse(result["ok"])
        self.assertTrue(any("height" in e and "16" in e for e in result["errors"]),
                        result["errors"])

    def test_dimensions_must_be_positive_int(self):
        draft = sample_draft(width=0, height=1280)
        result = ML.validate_layout(draft)
        self.assertFalse(result["ok"])
        self.assertTrue(any("width" in e for e in result["errors"]), result["errors"])

    def test_summary_requires_caption(self):
        draft = sample_draft()
        del draft["summary"]["caption"]
        result = ML.validate_layout(draft)
        self.assertFalse(result["ok"])
        self.assertTrue(any("summary" in e and "caption" in e for e in result["errors"]),
                        result["errors"])

    def test_summary_optional_fields_are_accepted(self):
        """meta / palette / canvas 是可选项 —— 缺了也要合法。"""
        draft = sample_draft()
        draft["summary"] = {"caption": "Only a caption."}
        result = ML.validate_layout(draft)
        self.assertTrue(result["ok"], result["errors"])

    def test_summary_canvas_enum(self):
        draft = sample_draft()
        draft["summary"]["canvas"] = "diptych"
        result = ML.validate_layout(draft)
        self.assertFalse(result["ok"])
        self.assertTrue(any("canvas" in e for e in result["errors"]), result["errors"])

    def test_summary_rejects_unknown_field(self):
        draft = sample_draft()
        draft["summary"]["watermark"] = "x"
        result = ML.validate_layout(draft)
        self.assertFalse(result["ok"])
        self.assertTrue(any("summary" in e and "未知字段" in e for e in result["errors"]),
                        result["errors"])

    def test_items_must_be_nonempty_array(self):
        draft = sample_draft()
        draft["items"] = []
        result = ML.validate_layout(draft)
        self.assertFalse(result["ok"])
        self.assertTrue(any("items" in e for e in result["errors"]), result["errors"])

    def test_item_requires_label_caption_bbox(self):
        draft = sample_draft()
        del draft["items"][0]["bbox"]
        result = ML.validate_layout(draft)
        self.assertFalse(result["ok"])
        self.assertTrue(any("bbox" in e for e in result["errors"]), result["errors"])

    def test_item_label_enum(self):
        draft = sample_draft()
        draft["items"][0]["label"] = "prop"
        result = ML.validate_layout(draft)
        self.assertFalse(result["ok"])
        self.assertTrue(any("label" in e for e in result["errors"]), result["errors"])

    def test_text_item_requires_text_field(self):
        draft = sample_draft()
        del draft["items"][1]["text"]
        result = ML.validate_layout(draft)
        self.assertFalse(result["ok"])
        self.assertTrue(any("text" in e for e in result["errors"]), result["errors"])

    def test_text_item_may_carry_optional_tags_and_palette(self):
        draft = sample_draft()
        draft["items"][1]["tags"] = ["title"]
        draft["items"][1]["palette"] = ["gold"]
        result = ML.validate_layout(draft)
        self.assertTrue(result["ok"], result["errors"])

    def test_item_rejects_nested_children(self):
        """官方 layout.md 明说「No other fields, nested children ... accepted」。"""
        draft = sample_draft()
        draft["items"][0]["children"] = [{"label": "object"}]
        result = ML.validate_layout(draft)
        self.assertFalse(result["ok"])
        self.assertTrue(any("未知字段" in e and "children" in e for e in result["errors"]),
                        result["errors"])

    def test_bbox_must_have_positive_area(self):
        draft = sample_draft()
        draft["items"][0]["bbox"] = [300, 300, 300, 300]
        result = ML.validate_layout(draft)
        self.assertFalse(result["ok"])
        self.assertTrue(any("bbox" in e for e in result["errors"]), result["errors"])

    def test_bbox_must_be_inside_canvas(self):
        draft = sample_draft()
        # bbox 是 [x_min, y_min, x_max, y_max] —— x_max=900 超出 720 宽
        draft["items"][0]["bbox"] = [220, 180, 900, 540]
        result = ML.validate_layout(draft)
        self.assertFalse(result["ok"])
        self.assertTrue(any("画布" in e for e in result["errors"]), result["errors"])

    def test_bbox_exceeding_canvas_height_is_rejected(self):
        draft = sample_draft()
        draft["items"][0]["bbox"] = [220, 180, 500, 1400]   # y_max 超出 1280 高
        result = ML.validate_layout(draft)
        self.assertFalse(result["ok"])
        self.assertTrue(any("画布" in e for e in result["errors"]), result["errors"])

    def test_bbox_must_be_four_finite_numbers(self):
        draft = sample_draft()
        draft["items"][0]["bbox"] = [1, 2, 3]
        result = ML.validate_layout(draft)
        self.assertFalse(result["ok"])
        self.assertTrue(any("bbox" in e for e in result["errors"]), result["errors"])

    def test_draft_size_mismatch_is_rejected(self):
        """草稿画布必须等于目标分辨率 —— 官方 engine.parse_draft 也是这么做的。"""
        result = ML.validate_layout(sample_draft(720, 1280), width=1024, height=1024)
        self.assertFalse(result["ok"])
        self.assertTrue(any("分辨率不一致" in e for e in result["errors"]),
                        result["errors"])


# ------------------------------------------------------------------ 编译


class TestLayoutCompile(unittest.TestCase):
    """草稿 -> MOSS Layout（abs_centralized / yxyx）。"""

    def test_compiled_uses_official_coord_space_and_format(self):
        compiled = ML.compile_layout(sample_draft())
        self.assertEqual(compiled["bbox_coord_space"], "abs_centralized")
        self.assertEqual(compiled["bbox_format"], "yxyx")

    def test_compiled_drops_canvas_size(self):
        """官方编译器导出的是 summary/items/坐标空间/格式 —— 不含宽高。"""
        compiled = ML.compile_layout(sample_draft())
        self.assertNotIn("width", compiled)
        self.assertNotIn("height", compiled)

    def test_compiled_bbox_is_yxyx_centered(self):
        compiled = ML.compile_layout(sample_draft())
        # 草稿 items[0].bbox = [220,180,500,540]，即 x1=220,y1=180,x2=500,y2=540
        # 官方算法：[floor(y1)-h//2, floor(x1)-w//2, ceil(y2)-h//2, ceil(x2)-w//2]
        #            = [180-640, 220-360, 540-640, 500-360]
        self.assertEqual(compiled["items"][0]["bbox"],
                         [180 - 640, 220 - 360, 540 - 640, 500 - 360])

    def test_compile_rejects_invalid_draft(self):
        draft = sample_draft()
        draft["items"][0]["bbox"] = [0, 0, 9999, 9999]
        with self.assertRaises(ML.LayoutError):
            ML.compile_layout(draft)

    @unittest.skipUnless(UPSTREAM_COMPILER.exists(),
                         "本机没有官方 ComfyUI-Moss 编译器，跳过上游一致性对照")
    def test_compiled_output_matches_official_compiler_byte_for_byte(self):
        """
        本项目的编译结果必须与官方 scripts/compile_prompt.py **逐字节**一致。

        期望值不是手写的 —— 是**真的跑一遍上游程序**拿到的输出。
        所以这条断言不是自证，而是与上游权威实现的对照。
        """
        draft = sample_draft()
        proc = subprocess.run(
            [sys.executable, str(UPSTREAM_COMPILER)],
            input=json.dumps(draft, ensure_ascii=False),
            capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        mine = json.dumps(ML.compile_layout(draft), ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False)
        self.assertEqual(mine, proc.stdout.strip())


# ------------------------------------------------------------------ 翻译


class TestLayoutToText(unittest.TestCase):
    """layout -> 自然语言提示词的确定性翻译。"""

    def setUp(self):
        self.compiled = ML.compile_layout(sample_draft())
        self.text = ML.layout_to_text(self.compiled, 720, 1280)

    def test_translation_is_deterministic(self):
        """同一份 layout 必须永远得到同一段文本（不含模型随机性）。"""
        again = ML.layout_to_text(ML.compile_layout(sample_draft()), 720, 1280)
        self.assertEqual(self.text, again)

    def test_summary_caption_leads_the_prompt(self):
        self.assertTrue(self.text.startswith(
            "A minimalist vertical poster with a deep navy night sky."))

    def test_item_position_is_translated(self):
        """元素位置要落成自然语言方位短语，而不是留在 JSON 里。"""
        self.assertIn("in the horizontal center", self.text)
        self.assertIn("in the upper area", self.text)
        self.assertIn("in the lower area", self.text)

    def test_item_size_is_translated(self):
        self.assertRegex(self.text, r"(of moderate size|filling most of the frame"
                                r"|occupying a large part of the frame|small within the frame)")

    def test_item_attribute_and_material_kept(self):
        """外观/材质等描述必须进提示词。"""
        self.assertIn("crater texture", self.text)
        self.assertIn("Bold serif title characters", self.text)

    def test_visible_text_is_verbatim(self):
        """图内文字一字不差，且被引号包裹。"""
        self.assertIn('"月光 MOSS"', self.text)

    def test_tags_and_palette_are_included(self):
        self.assertIn("moon", self.text)
        self.assertIn("deep navy", self.text)

    def test_no_json_leaks_into_prompt(self):
        """翻译后的提示词不该再出现 bbox / JSON 结构 —— 那是工具链的事。"""
        self.assertNotIn("bbox", self.text)
        self.assertNotIn("{", self.text)
        self.assertNotIn("}", self.text)

    def test_chinese_caption_yields_chinese_phrases(self):
        """中文 caption -> 中文方位短语（不要混语言）。"""
        draft = sample_draft()
        draft["summary"]["caption"] = "极简竖版海报，深蓝夜空。"
        compiled = ML.compile_layout(draft)
        text = ML.layout_to_text(compiled, 720, 1280)
        self.assertIn("位于画面", text)
        self.assertNotIn("in the upper area", text)

    def test_english_caption_with_chinese_visible_text_stays_english(self):
        """
        回归：英文描述 + 中文图内文字，**不能**被判成中文。

        实测踩过的坑：caption 是英文但里面写了中文标题 "月光 MOSS"，
        早期用「含不含 CJK 字符」判定语言 -> 整段提示词变成
        「英文正文 + 中文方位短语」的混合语言，CLIPTextEncode 效果打折。
        """
        draft = sample_draft()
        draft["summary"]["caption"] = (
            "A minimalist vertical poster with a deep navy night sky and a "
            "full moon above the title reading '月光 MOSS'.")
        draft["items"][1]["text"] = "月光 MOSS"
        compiled = ML.compile_layout(draft)
        text = ML.layout_to_text(compiled, 720, 1280)
        # 方位短语应保持英文
        self.assertIn("in the upper area", text)
        self.assertNotIn("位于画面", text)
        # 但图内文字本身仍逐字保留
        self.assertIn("月光 MOSS", text)

    def test_language_detection_thresholds(self):
        """CJK 占比 < 20% 判英文，>= 20% 判中文。"""
        self.assertEqual(ML._detect_language("A quiet night sky with 月光"), "en")
        self.assertEqual(
            ML._detect_language("极简竖版海报，深蓝夜空中的月亮"), "zh")
        self.assertEqual(ML._detect_language(""), "en")

    def test_translation_without_items_uses_summary_only(self):
        compiled = {"summary": {"caption": "A quiet empty frame."}, "items": []}
        text = ML.layout_to_text(compiled, 720, 1280)
        self.assertIn("A quiet empty frame.", text)


# ------------------------------------------------------------------ 错误回退


class TestLayoutFallback(unittest.TestCase):
    """模型不可用 / 返回非法内容时，必须明确报错，绝不静默失败。"""

    def test_empty_model_output_raises(self):
        with self.assertRaises(ML.LayoutError) as ctx:
            ML.enhance("a poster", "", 720, 1280)
        self.assertIn("空", str(ctx.exception))

    def test_whitespace_only_output_raises(self):
        with self.assertRaises(ML.LayoutError):
            ML.enhance("a poster", "   \n  ", 720, 1280)

    def test_non_json_output_raises_with_position(self):
        with self.assertRaises(ML.LayoutError) as ctx:
            ML.enhance("a poster", "抱歉，我无法完成这个请求。", 720, 1280)
        message = str(ctx.exception)
        self.assertIn("JSON", message)
        self.assertIn("行", message)      # 给出出错位置

    def test_json_array_instead_of_object_raises(self):
        with self.assertRaises(ML.LayoutError) as ctx:
            ML.enhance("a poster", "[1,2,3]", 720, 1280)
        self.assertIn("对象", str(ctx.exception))

    def test_duplicate_json_key_raises(self):
        with self.assertRaises(ML.LayoutError):
            ML.enhance("a poster",
                       '{"width":720,"width":720,"height":1280,'
                       '"bbox_coord_space":"abs","bbox_format":"xyxy",'
                       '"summary":{"caption":"x"},"items":[]}', 720, 1280)

    def test_schema_violation_raises_with_readable_reason(self):
        """JSON 合法但不符合 schema —— 必须给出**是哪一条**不合法。"""
        bad = {"width": 720, "height": 1280, "bbox_coord_space": "abs",
               "bbox_format": "xyxy", "summary": {"caption": "x"}, "items": []}
        with self.assertRaises(ML.LayoutError) as ctx:
            ML.enhance("a poster", json.dumps(bad), 720, 1280)
        message = str(ctx.exception)
        self.assertIn("items", message)
        self.assertTrue(ctx.exception.errors)

    def test_fenced_json_is_accepted(self):
        """模型爱加 ```json 围栏 —— 必须能吃下。"""
        fenced = "```json\n" + json.dumps(sample_draft(), ensure_ascii=False) + "\n```"
        result = ML.enhance("a poster", fenced, 720, 1280)
        self.assertEqual(result["layout"]["bbox_coord_space"], "abs_centralized")

    def test_blank_prompt_raises(self):
        with self.assertRaises(ML.LayoutError):
            ML.enhance("   ", json.dumps(sample_draft()), 720, 1280)

    def test_non_multiple_of_16_canvas_raises(self):
        with self.assertRaises(ML.LayoutError) as ctx:
            ML.enhance("a poster", json.dumps(sample_draft()), 720, 1270)
        self.assertIn("16", str(ctx.exception))

    def test_enhance_returns_both_artifacts(self):
        """成功时必须同时给出 layout JSON 与最终文本提示词。"""
        result = ML.enhance("a poster", json.dumps(sample_draft()), 720, 1280)
        self.assertIn("layout_json", result)
        self.assertIn("final_prompt", result)
        self.assertIn("draft", result)
        self.assertEqual(result["width"], 720)
        self.assertEqual(result["height"], 1280)
        # layout_json 是合法 JSON，且带官方坐标空间标记
        parsed = json.loads(result["layout_json"])
        self.assertEqual(parsed["bbox_coord_space"], "abs_centralized")
        self.assertEqual(parsed["bbox_format"], "yxyx")


# ------------------------------------------------------------------ 消息构造


class TestBuildMessages(unittest.TestCase):
    """发给本地模型的消息必须带齐 schema 约束与画布尺寸。"""

    def test_messages_include_canvas_size_and_schema(self):
        messages = ML.build_messages("a poster", 720, 1280, "")
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")
        system = messages[0]["content"]
        self.assertIn("720", system)
        self.assertIn("1280", system)
        self.assertIn("abs", system)
        self.assertIn("xyxy", system)

    def test_user_message_carries_prompt_and_intent(self):
        messages = ML.build_messages("一只猫", 1024, 1024, "想要暖色调")
        user = messages[1]["content"]
        self.assertIn("一只猫", user)
        self.assertIn("想要暖色调", user)
        self.assertIn("1024", user)

    def test_no_external_api_dependency_in_messages(self):
        """不引入任何对 OpenAI 官方 API 的硬依赖 —— 提示词里不能出现 API 相关指引。"""
        messages = ML.build_messages("a poster", 720, 1280, "")
        blob = " ".join(m["content"] for m in messages).lower()
        for banned in ("api.openai.com", "chat/completions", "bearer", "api_key"):
            self.assertNotIn(banned, blob)


if __name__ == "__main__":
    unittest.main()
