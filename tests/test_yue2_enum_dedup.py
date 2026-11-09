# -*- coding: utf-8 -*-
"""P1-1 · 音乐结构化枚举去重 —— 契约断言（防止副本回流）。

【背景】审查 P1-1：
    `h3studio/music_style.py` 与 `h3studio/yue2_schema.py` 各自持有一份
    `STATIC_ENUMS` / `_omit()` / `build_style_prompt()` / length_hint 映射，
    `web/music.js` 里还有第三份 `STYLE_LENGTH_HINT_TEXT`。
    当前行为一致但属于**巧合性一致** —— 后续必然漂移。

【契约】
    契约 J：权威实现唯一 = `h3studio/yue2_schema.py`；
            `music_style.py` 只做 re-export，**不得**再抄一份字面量。
    契约 K：re-export 必须是**同一个对象**（identity），不是副本 ——
            只有 identity 相同才谈得上「改一处即全局生效」。
    契约 L：两条调用路径（music_style 位置参数封装 / yue2_schema dict）
            的产物必须**完全一致**（含纯器乐、custom、括号省略等边界）。
    契约 M：前端不得硬编码 length_hint 的英文描述；
            必须从 `/api/music-options` 下发（`length_hint_text`）。

运行：python3 -m pytest tests/test_yue2_enum_dedup.py -v
"""
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import music_style, yue2_schema   # noqa: E402

FIELDS = ["language", "genre", "mood", "vocal", "vocal_color", "instruments",
          "tempo_bpm", "energy", "length_hint", "extra",
          "custom_language", "custom_genre"]

#: 边界用例：纯器乐 / custom 语言 / custom 曲风 / 括号省略 / 空值 / BPM 非法
CASES = {
    "普通": dict(language="Japanese", genre="piano pop", mood="bittersweet",
                 vocal="female vocal", vocal_color="breathy",
                 instruments="acoustic piano, sustained strings",
                 tempo_bpm=76, energy="builds from quiet to loud",
                 length_hint="full song (~3.5-5 min)", extra="雨夜告别",
                 custom_language="", custom_genre=""),
    "纯器乐": dict(language="English", genre="ambient", mood="calm",
                   vocal="(instrumental / no lead vocal)", vocal_color="(none)",
                   instruments="felt piano", tempo_bpm=60, energy="low energy",
                   length_hint="model decides", extra="",
                   custom_language="", custom_genre=""),
    "括号省略": dict(language="(custom / none)", genre="(custom / none)",
                     mood="(none)", vocal="(none)", vocal_color="(none)",
                     instruments="", tempo_bpm=0, energy="(none)",
                     length_hint="", extra="",
                     custom_language="", custom_genre=""),
    "custom 语言与曲风": dict(language="(custom / none)", genre="(custom / none)",
                              mood="warm", vocal="male vocal", vocal_color="husky",
                              instruments="acoustic guitar", tempo_bpm=92,
                              energy="mid energy", length_hint="single edit (~2-3 min)",
                              extra="", custom_language="粤语", custom_genre="city pop"),
    "BPM 非数字": dict(language="English", genre="jazz", mood="smooth",
                       vocal="female vocal", vocal_color="warm",
                       instruments="upright bass", tempo_bpm="abc",
                       energy="mid energy", length_hint="short clip (~45-75s)",
                       extra="", custom_language="", custom_genre=""),
    "全空": dict(language="", genre="", mood="", vocal="", vocal_color="",
                 instruments="", tempo_bpm=0, energy="", length_hint="",
                 extra="", custom_language="", custom_genre=""),
}


class TestSingleAuthoritativeImplementation(unittest.TestCase):
    """契约 J：权威实现唯一。"""

    def setUp(self):
        self.h3 = REPO / "h3studio"

    def _sources(self):
        for p in sorted(self.h3.glob("*.py")):
            yield p, p.read_text(encoding="utf-8", errors="replace")

    def test_enums_defined_only_once(self):
        """STATIC_ENUMS / LENGTH_HINTS / LYRIC_TEMPLATES 各只有一处字面量定义。"""
        tokens = ["STATIC_ENUMS = {", "LENGTH_HINTS = {", "LYRIC_TEMPLATES = {"]
        for token in tokens:
            where = [p.name for p, src in self._sources() if token in src]
            self.assertEqual(len(where), 1,
                             f"`{token}` 应只在一处定义，实际在 {where}")

    def test_omit_defined_only_once(self):
        """_omit() 只有一处实现。"""
        where = [p.name for p, src in self._sources() if "def _omit(" in src]
        self.assertEqual(len(where), 1, f"_omit() 应只在一处实现，实际在 {where}")

    def test_build_style_prompt_has_single_implementation(self):
        """build_style_prompt 只有权威实现 + 一个委托封装，没有第二份逻辑。"""
        where = [p.name for p, src in self._sources() if "def build_style_prompt" in src]
        self.assertEqual(len(where), 2,
                         f"应只有权威实现 + 委托封装，实际在 {where}")
        self.assertIn("yue2_schema.py", where, "权威实现必须在 yue2_schema.py")
        # 委托封装不得含自己的拼接逻辑（不该出现 parts / join）
        wrapper = (self.h3 / "music_style.py").read_text(encoding="utf-8")
        body = wrapper[wrapper.index("def build_style_prompt"):]
        body = body[:body.index("\ndef ")] if "\ndef " in body else body
        self.assertNotIn(".join(", body,
                         "music_style.build_style_prompt 只应委托，不得自己拼串")

    def test_music_style_module_does_not_define_enums_literally(self):
        """music_style.py 里不得出现 STATIC_ENUMS = { 这样的字面量。"""
        src = (self.h3 / "music_style.py").read_text(encoding="utf-8")
        self.assertNotIn("STATIC_ENUMS = {", src,
                         "music_style.py 不得再定义 STATIC_ENUMS 字面量")


class TestReExportIdentity(unittest.TestCase):
    """契约 K：re-export 必须是同一个对象，不是副本。"""

    def test_reexports_are_identical_objects(self):
        pairs = [
            ("STATIC_ENUMS", music_style.STATIC_ENUMS, yue2_schema.STATIC_ENUMS),
            ("LENGTH_HINTS", music_style.LENGTH_HINTS, yue2_schema.LENGTH_HINTS),
            ("LYRIC_TEMPLATES", music_style.LYRIC_TEMPLATES,
             yue2_schema.LYRIC_TEMPLATES),
            ("STYLE_FIELDS", music_style.STYLE_FIELDS, yue2_schema.STYLE_FIELDS),
            ("_omit", music_style._omit, yue2_schema._omit),
            ("get_enums", music_style.get_enums, yue2_schema.get_enums),
        ]
        for name, a, b in pairs:
            self.assertIs(a, b, f"{name} 必须与权威实现是**同一个对象**（identity）")

    def test_legacy_alias_still_points_to_authority(self):
        """旧名 LENGTH_HINT_TEXT 保留，但必须指向权威映射。"""
        self.assertIs(music_style.LENGTH_HINT_TEXT, yue2_schema.LENGTH_HINTS)


class TestBehaviourEquivalence(unittest.TestCase):
    """契约 L：两条调用路径产物必须完全一致。"""

    def test_wrapper_matches_authority_for_all_cases(self):
        for name, style in CASES.items():
            with self.subTest(case=name):
                wrapper = music_style.build_style_prompt(*[style[f] for f in FIELDS])
                authority = yue2_schema.build_style_prompt(dict(style))
                self.assertEqual(wrapper, authority,
                                 f"{name}: 委托封装与权威实现产物不一致")


class TestFrontendUsesServerDownstream(unittest.TestCase):
    """契约 M：前端不硬编码 length_hint，从服务端取。"""

    def setUp(self):
        self.web = REPO / "web"

    def test_no_length_hint_english_literal_in_frontend(self):
        """前端 JS 里不得出现 length_hint 的英文描述字面量。"""
        offenders = []
        for p in sorted(self.web.glob("*.js")):
            src = p.read_text(encoding="utf-8", errors="replace")
            for lit in yue2_schema.LENGTH_HINTS.values():
                if lit and lit in src:
                    offenders.append(f"{p.name}: {lit!r}")
        self.assertEqual(offenders, [],
                         f"前端不得硬编码 length_hint 英文描述：{offenders}")

    def test_no_style_length_hint_text_constant(self):
        """旧的 STYLE_LENGTH_HINT_TEXT 常量不得再定义。"""
        for p in sorted(self.web.glob("*.js")):
            src = p.read_text(encoding="utf-8", errors="replace")
            self.assertIsNone(
                re.search(r"STYLE_LENGTH_HINT_TEXT\s*=", src),
                f"{p.name} 仍在定义 STYLE_LENGTH_HINT_TEXT（前端硬编码）")

    def test_music_js_reads_length_hint_from_options(self):
        """music.js 必须从 /api/music-options 读 length_hint_text。"""
        src = (self.web / "music.js").read_text(encoding="utf-8")
        self.assertIn("length_hint_text", src)
        self.assertIn("music-options", src)

    def test_server_downstream_length_hint_text(self):
        """服务端下发的 length_hint_text == 权威 LENGTH_HINTS。"""
        opts = music_style.style_options()
        self.assertEqual(opts["length_hint_text"], dict(yue2_schema.LENGTH_HINTS))


if __name__ == "__main__":
    unittest.main()
