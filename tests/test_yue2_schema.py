# -*- coding: utf-8 -*-
"""
YuE2 音乐「结构化风格 + 歌词」链路（h3studio/yue2_schema.py）的回归测试。

【这个模块是什么】
移植自 nonebot 插件 `nonebot_plugin_novelai` 的 `yue2_schema.py`
（咏唱「文生音乐 / 参考生音乐」的实现）。对应关系：
  * 权威枚举源      -> get_enums()（ComfyUI /object_info 运行时拉 + 静态兜底）
  * 归一化 + 校验   -> normalize_and_validate()
  * 系统提示词渲染  -> build_system_prompt()
  * 风格/歌词串拼接 -> build_style_prompt() / build_lyrics_prompt()

【为什么必须有这一层 —— 写死这个真事实】
YuE2 风格节点有 8 个 COMBO 字段，合法值都是**带括号、带波浪号、带斜杠**
的固定字符串（`(instrumental / no lead vocal)`、`full song (~3.5-5 min)`）。
LLM 极容易意译 / 去括号，而 ComfyUI 侧逐字校验 ——
值不在枚举里就是 HTTP 400 value_not_in_list。
咏唱旧版把枚举手抄在系统提示词里（只列了 18/47 个 genre），
结果就是「模型产出正常、ComfyUI 直接拒绝」。
所以规则是：**枚举只从运行时拉，系统提示词从同一份 schema 渲染，永不二次手抄。**

运行：python3 -m pytest tests/test_yue2_schema.py -v
"""
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import yue2_schema as ys  # noqa: E402


class TestEnums(unittest.TestCase):
    """权威枚举 —— 静态兜底必须与官方节点一致。"""

    def test_static_enums_have_expected_values(self):
        """兜底枚举里的关键值必须逐字正确（括号/波浪号都在）。"""
        self.assertIn("(instrumental / no lead vocal)", ys.STATIC_ENUMS["vocal"])
        self.assertIn("full song (~3.5-5 min)", ys.STATIC_ENUMS["length_hint"])
        self.assertIn("V-C-V-C-bridge-C", ys.STATIC_ENUMS["template"])
        self.assertIn("English", ys.STATIC_ENUMS["language"])
        self.assertIn("Mandarin", ys.STATIC_ENUMS["language"])
        self.assertIn("(none)", ys.STATIC_ENUMS["mood"])

    def test_get_enums_never_raises(self):
        """拉不到 ComfyUI 时必须回落静态兜底，且**永不抛异常**。"""
        enums = ys.get_enums()
        self.assertIn("genre", enums)
        self.assertIn("template", enums)
        self.assertTrue(enums["genre"])

    def test_enum_values_are_unique(self):
        """枚举里有重复值 = 归一化会随机挑一个 -> 不可接受。"""
        for key, values in ys.STATIC_ENUMS.items():
            self.assertEqual(len(values), len(set(values)),
                             f"{key} 的枚举有重复值")


class TestNormalizeAndValidate(unittest.TestCase):
    """归一化 —— 把模型产出收敛成 100% 能进 ComfyUI 的配置。"""

    def _good(self):
        """一份**完全合法**的模型产出（歌词 >= 200 字符、每段 >= 4 行）。"""
        verse = "\n".join(f"这是第{i}行歌词内容示例" for i in range(1, 6))
        chorus = "\n".join(f"这是副歌第{i}行内容示例" for i in range(1, 6))
        bridge = "\n".join(f"这是桥段第{i}行内容示例" for i in range(1, 6))
        lyrics = (f"[Verse]\n{verse}\n\n[Chorus]\n{chorus}\n\n"
                  f"[Bridge]\n{bridge}")
        return {
            "style": {
                "language": "Japanese", "genre": "piano pop", "mood": "bittersweet",
                "vocal": "female vocal", "vocal_color": "breathy",
                "instruments": "acoustic piano, sustained string ensemble",
                "tempo_bpm": 76, "energy": "mid energy",
                "length_hint": "full song (~3.5-5 min)",
                "extra": "雨夜告别", "custom_language": "", "custom_genre": "",
            },
            "lyrics": {
                "template": "V-C-V-C-bridge-C",
                "lyrics_override": lyrics,
            },
        }

    def test_valid_config_passes_unchanged(self):
        cfg = self._good()
        ok, norm, fixes, problems = ys.normalize_and_validate(cfg)
        self.assertTrue(ok)
        self.assertEqual(norm["style"]["language"], "Japanese")
        self.assertEqual(norm["style"]["tempo_bpm"], 76)
        self.assertEqual(norm["style"]["length_hint"], "full song (~3.5-5 min)")
        self.assertEqual(fixes, [])

    def test_dirty_combo_value_is_repaired(self):
        """LLM 意译 / 去括号 -> 必须收敛回合法枚举，并记录 fix。"""
        cfg = self._good()
        cfg["style"]["length_hint"] = "full song (about four minutes)"
        cfg["style"]["vocal"] = "instrumental / no lead vocal"
        ok, norm, fixes, _ = ys.normalize_and_validate(cfg)
        self.assertTrue(ok)
        self.assertIn(norm["style"]["length_hint"], ys.STATIC_ENUMS["length_hint"])
        self.assertEqual(norm["style"]["vocal"], "(instrumental / no lead vocal)")
        self.assertTrue(fixes, "修复动作必须被记录下来")

    def test_instrumental_forces_vocal_color_none(self):
        """
        纯器乐时 vocal_color 必须 (none) ——
        否则拼出 "warm instrumental"，模型可能真的加进人声。
        """
        cfg = self._good()
        cfg["style"]["vocal"] = "(instrumental / no lead vocal)"
        cfg["style"]["vocal_color"] = "warm"
        ok, norm, fixes, _ = ys.normalize_and_validate(cfg)
        self.assertTrue(ok)
        self.assertEqual(norm["style"]["vocal_color"], "(none)")
        self.assertTrue(any("vocal_color" in f for f in fixes))

    def test_missing_fields_get_defaults(self):
        """
        缺字段 -> 用安全默认值补齐，绝不写空串（空串会被 ComfyUI 400）。

        【与第三方语义一致】咏唱 yue2_schema.normalize_and_validate() 里
        「两个节点块都为空」判失败（产出毫无用户意图），但**只缺一块**
        时用默认值补齐 —— 这里守的就是后者。
        """
        ok, norm, fixes, _ = ys.normalize_and_validate({"style": {"genre": "jazz"}})
        self.assertTrue(ok)
        for key in ("language", "genre", "mood", "vocal", "vocal_color",
                    "instruments", "energy", "length_hint", "extra"):
            self.assertTrue(norm["style"][key],
                            f"{key} 补齐后不能为空")
        self.assertEqual(norm["style"]["genre"], "jazz", "给了的值不该被覆盖")

    def test_both_blocks_empty_fails(self):
        """两个节点块都空 -> 判失败（产出毫无用户意图），不返回半成品。"""
        ok, norm, _, problems = ys.normalize_and_validate({"style": {}, "lyrics": {}})
        self.assertFalse(ok)
        self.assertIsNone(norm)
        self.assertTrue(problems)

    def test_tempo_out_of_range_clamped(self):
        cfg = self._good()
        cfg["style"]["tempo_bpm"] = 999
        ok, norm, _, _ = ys.normalize_and_validate(cfg)
        self.assertTrue(ok)
        self.assertEqual(norm["style"]["tempo_bpm"], ys.TEMPO_MAX)
        cfg["style"]["tempo_bpm"] = -5
        ok, norm, _, _ = ys.normalize_and_validate(cfg)
        self.assertEqual(norm["style"]["tempo_bpm"], ys.TEMPO_MIN)

    def test_tempo_from_string(self):
        cfg = self._good()
        cfg["style"]["tempo_bpm"] = "88 BPM"
        ok, norm, _, _ = ys.normalize_and_validate(cfg)
        self.assertTrue(ok)
        self.assertEqual(norm["style"]["tempo_bpm"], 88)

    def test_wrapper_is_stripped(self):
        """LLM 多包一层 {"config": {...}} -> 必须拆掉。"""
        cfg = {"config": self._good()}
        ok, norm, fixes, _ = ys.normalize_and_validate(cfg)
        self.assertTrue(ok)
        self.assertIn("style", norm)
        self.assertTrue(any("包装" in f for f in fixes))

    def test_empty_lyrics_get_skeleton(self):
        """空歌词 -> 段标签齐全的 [Instrumental] 骨架（不能让模型唱无关内容）。"""
        cfg = self._good()
        cfg["lyrics"]["lyrics_override"] = ""
        ok, norm, _, _ = ys.normalize_and_validate(cfg)
        self.assertTrue(ok)
        self.assertIn("[Chorus]", norm["lyrics"]["lyrics_override"])
        self.assertIn("[Instrumental]", norm["lyrics"]["lyrics_override"])

    def test_broken_top_level_fails(self):
        """完全不可用的顶层结构 -> ok=False（不返回半成品）。"""
        ok, norm, _, problems = ys.normalize_and_validate("not a dict")
        self.assertFalse(ok)
        self.assertIsNone(norm)
        self.assertTrue(problems)

    def test_both_nodes_missing_fails(self):
        ok, _, _, problems = ys.normalize_and_validate({})
        self.assertFalse(ok)
        self.assertTrue(problems)


class TestSystemPrompt(unittest.TestCase):
    """系统提示词 —— 枚举必须从同一份 schema 渲染，永不二次手抄。"""

    def test_prompt_contains_full_enum_lists(self):
        p = ys.build_system_prompt()
        # 关键枚举值必须逐字出现在提示词里
        self.assertIn("(instrumental / no lead vocal)", p)
        self.assertIn("full song (~3.5-5 min)", p)
        self.assertIn("V-C-V-C-bridge-C", p)
        self.assertIn("rain", p) if False else None
        # 全部 genre 都要在（手抄旧版只列了 18/47 个 -> 这条能抓住回归）
        for genre in ys.STATIC_ENUMS["genre"]:
            self.assertIn(genre, p, f"提示词漏了 genre 枚举项 {genre}")

    def test_prompt_declares_contract(self):
        p = ys.build_system_prompt()
        self.assertIn("style", p)
        self.assertIn("lyrics", p)
        self.assertIn("只输出一个 JSON 对象", p)
        # 括号即省略的隐式规则必须写进去
        self.assertIn("括号即省略", p)

    def test_prompt_has_both_examples(self):
        p = ys.build_system_prompt()
        self.assertIn("示例 A", p)
        self.assertIn("示例 B", p)


class TestStyleAndLyricsBuilders(unittest.TestCase):
    """
    风格串 / 歌词串拼接 —— 必须与官方节点
    ComfyUI-YuE2/nodes.py 的 build_style_prompt() / YuE2LyricsTemplate.build()
    逐条一致（顺序、省略规则、连接符）。
    """

    def test_style_prompt_order_and_join(self):
        s = ys.build_style_prompt({
            "language": "English", "genre": "piano pop", "mood": "intimate",
            "vocal": "female vocal", "vocal_color": "warm",
            "instruments": "acoustic piano, rounded bass and light drums",
            "tempo_bpm": 88, "energy": "mid energy",
            "length_hint": "single edit (~2-3 min)",
            "extra": "lyrical memorable melody, unhurried phrasing",
        })
        self.assertEqual(
            s,
            "English, piano pop, intimate, warm female vocal, "
            "acoustic piano, rounded bass and light drums, 88 BPM, mid energy, "
            "radio-length song, about two to three minutes, "
            "lyrical memorable melody, unhurried phrasing")

    def test_paren_values_are_omitted(self):
        """以 ( 开头的值整段省略（官方节点的 _omit 规则）。"""
        s = ys.build_style_prompt({
            "language": "English", "genre": "cinematic", "mood": "(none)",
            "vocal": "(instrumental / no lead vocal)", "vocal_color": "(none)",
            "instruments": "lead violin", "tempo_bpm": 86,
            "energy": "(none)", "length_hint": "model decides", "extra": "",
        })
        self.assertNotIn("(none)", s)
        self.assertNotIn("instrumental", s)
        self.assertEqual(s, "English, cinematic, lead violin, 86 BPM")

    def test_tempo_zero_is_skipped(self):
        s = ys.build_style_prompt({
            "language": "English", "genre": "pop", "tempo_bpm": 0,
        })
        self.assertNotIn("BPM", s)

    def test_custom_language_and_genre(self):
        s = ys.build_style_prompt({
            "language": "(custom / none)", "custom_language": "Klingon",
            "genre": "(custom / none)", "custom_genre": "space folk",
        })
        self.assertIn("Klingon", s)
        self.assertIn("space folk", s)

    def test_lyrics_override_wins(self):
        text = ys.build_lyrics_prompt({
            "template": "V-C-V-C-bridge-C", "lyrics_override": "[Verse]\n真歌词"})
        self.assertEqual(text, "[Verse]\n真歌词")

    def test_lyrics_falls_back_to_template(self):
        text = ys.build_lyrics_prompt({
            "template": "verse + chorus", "lyrics_override": ""})
        self.assertIn("[Verse]", text)
        self.assertIn("[Chorus]", text)


class TestParseRobustJson(unittest.TestCase):
    """模型产出的 JSON 解析要宽容（围栏 / 尾随文字 / 包装层）。"""

    def test_plain_json(self):
        obj = ys.strip_code_fence('{"a": 1}')
        self.assertEqual(json.loads(obj)["a"], 1)

    def test_code_fence_stripped(self):
        text = '```json\n{"a": 1}\n```'
        obj = ys.strip_code_fence(text)
        self.assertEqual(json.loads(obj)["a"], 1)

    def test_bare_fence_stripped(self):
        text = '```\n{"a": 1}\n```'
        obj = ys.strip_code_fence(text)
        self.assertEqual(json.loads(obj)["a"], 1)


if __name__ == "__main__":
    unittest.main()
