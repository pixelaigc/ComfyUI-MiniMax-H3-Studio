# -*- coding: utf-8 -*-
"""
内置 Agent 管理页的默认参数 —— 「真实配置派生」与「MP 预算 × 百分比」。

【这一层测什么】
  1. **默认参数来自真实配置**：改真源（面积上限 / 档位表）后，
     管理页下发的默认参数**随之改变** —— 不是写死的常量。
  2. **flf / r2v 不再是 544p/768p 两档**：旧档位表是旧界面遗留，
     真实档位必须按各 mode 的面积上限派生。
  3. **MP 预算 × 百分比**：换算、32 对齐、超限自动下调 + 调整原因回传。
  4. **存量迁移**：老库里的 ``resolution="768p"`` 读出来时被迁移成
     等价的 MP 预算（**像素不变**），且迁移**幂等**。
  5. **两条路径都生效**：面板提交与 Agent 自建 job 都吃同一份默认值。

运行：python3 -m pytest tests/test_agent_defaults_truth.py -q
"""

import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import agent_capability                  # noqa: E402
from h3studio import agent_schema                      # noqa: E402
from h3studio import gen_defaults                     # noqa: E402
from h3studio import h3_video_size                    # noqa: E402
from h3studio import size_spec                        # noqa: E402
from h3studio import video_models as vm               # noqa: E402
from h3studio import workflows as wf                  # noqa: E402


class TestDefaultParamsFromRealConfig(unittest.TestCase):
    """默认参数必须由**真实配置**派生，改配置即改默认值。"""

    def test_flf_r2v_have_no_resolution_tier_field(self):
        """flf / r2v 不得再有独立的「分辨率档位」字段（尺寸表达必须唯一）。

        【2026-11-21 —— 用户驳回后改的断言】
        原来这三条测试断言的是「resolution 选项不能只是旧的 544p/768p 两档」，
        并且要求它「远多于两档」。那等于**默认了该字段应该存在**。

        用户看到的是：
            画面比例 16:9 ＋ 分辨率档位「768p·768×1344 (9:16)」
            ＋ MP 预算 0.8 ＋ MP 百分比 100
        —— 档位标签自带 9:16，与所选 16:9 直接矛盾；三套尺寸表达同时存在。
        原话：「同时存在百万像素和分辨率挡位和比例 … 全部都是冲突的。」

        官方首尾帧模板（nonebot workflows/minimax_h3_v1.json 节点 115）的
        尺寸表达只有一处：
            ResolutionSelector {aspect_ratio, megapixels, multiple: 32}
        —— 比例 + MP 预算，没有「档位」。所以这里断言反过来：
        **不得有 resolution 字段**，尺寸表达只能是 比例 × MP 预算 × 百分比。
        """
        for mode in ("flf", "r2v"):
            fields, supported = gen_defaults._mode_fields(mode)
            self.assertTrue(supported, mode)
            names = [f["name"] for f in fields]
            self.assertNotIn("resolution", names,
                             f"{mode} 仍有独立的分辨率档位字段：{names}")
            self.assertIn("aspect", names, mode)
            self.assertIn("megapixels", names, mode)
            self.assertIn("resolution_percent", names, mode)
            # 【为什么还要断言「只有一个尺寸数值字段族」】
            # 「唯一表达」不是「删掉一个字段」就行 —— 如果再冒出第四个
            # （比如 width/height 又回到默认参数里），冲突会以另一种形式回来。
            size_fields = [n for n in names
                           if n in ("resolution", "width", "height",
                                    "short_edge", "size_preset")]
            self.assertEqual(size_fields, [],
                             f"{mode} 出现了档位/宽高类的第二套尺寸表达：{size_fields}")

    def test_flf_r2v_aspect_options_are_the_real_selector_values(self):
        """画面比例的取值 = 官方 ResolutionSelector 的那一份（不是另一张表）。"""
        for mode in ("flf", "r2v"):
            fields, _ = gen_defaults._mode_fields(mode)
            field = next(f for f in fields if f["name"] == "aspect")
            values = {o["value"] for o in field["options"]}
            self.assertEqual(values, set(vm.VIDEO_ASPECTS.keys()), mode)

    def test_note_no_longer_mentions_legacy_mp_mapping(self):
        """任何字段的 note 都不得再写死「544p = 0.4MP / 768p = 0.8MP」旧口径。"""
        for mode in ("flf", "r2v"):
            fields, _ = gen_defaults._mode_fields(mode)
            for field in fields:
                note = field.get("note") or ""
                self.assertNotIn("544p = 0.4MP", note, f"{mode}/{field['name']}")
                self.assertNotIn("768p = 0.8MP", note, f"{mode}/{field['name']}")

    def test_max_megapixels_follows_real_policy(self):
        """MP 预算字段的上界必须等于该 mode 的**真实**面积上限。"""
        for mode in ("flf", "r2v", "t2v"):
            cap = vm.max_pixels_for_mode(mode)
            self.assertIsNotNone(cap)
            fields, _ = gen_defaults._mode_fields(mode)
            field = next(f for f in fields if f["name"] == "megapixels")
            self.assertAlmostEqual(
                float(field["max"]), float(cap), places=6,
                msg=f"{mode} 的 MP 预算上界与真实上限不符")

    def test_changing_real_policy_changes_defaults(self):
        """【关键】改真源（面积上限）-> 默认参数随之改变。

        这条锁死「默认参数来自真实配置」而不是「写死的常量」。
        """
        original = vm.VIDEO_MAX_CANVAS_PIXELS
        try:
            vm.VIDEO_MAX_CANVAS_PIXELS = 1024 * 1024   # 临时改成 1.0 MP
            fields, _ = gen_defaults._mode_fields("flf")
            field = next(f for f in fields if f["name"] == "megapixels")
            self.assertAlmostEqual(float(field["max"]), 1.0, places=6)
            self.assertAlmostEqual(
                float(size_spec.mode_max_megapixels("flf")), 1.0, places=6)
        finally:
            vm.VIDEO_MAX_CANVAS_PIXELS = original
        # 还原后回到真实值
        fields, _ = gen_defaults._mode_fields("flf")
        field = next(f for f in fields if f["name"] == "megapixels")
        self.assertAlmostEqual(
            float(field["max"]),
            original / (1024 * 1024), places=6)

    def test_default_megapixels_inside_real_cap(self):
        """默认 MP 预算不得超过真实上限。"""
        for mode in ("flf", "r2v", "t2v"):
            cap = vm.max_pixels_for_mode(mode)
            self.assertLessEqual(size_spec.default_megapixels(mode), cap + 1e-9)


class TestSizeSpecMPPercent(unittest.TestCase):
    """「MP 预算 × 百分比」的换算、吸附与超限调整。"""

    def test_legal_value_passes_through(self):
        """合法值原样放行。"""
        width, height, reasons = size_spec.plan_size(
            "flf", "16:9", megapixels=0.4)
        self.assertEqual((width, height), (864, 480))
        self.assertFalse(any("超过" in r for r in reasons), reasons)

    def test_snapped_to_multiple_of_32(self):
        """宽高必须落在 32 的倍数上。"""
        for mp in (0.31, 0.45, 0.77, 1.13, 1.99):
            width, height, _reasons = size_spec.plan_size(
                "flf", "16:9", megapixels=mp)
            self.assertEqual(width % 32, 0, f"{mp} -> {width}")
            self.assertEqual(height % 32, 0, f"{mp} -> {height}")

    def test_over_cap_is_auto_reduced_with_reason(self):
        """超出真实上限 -> 自动下调 + 给出原因。"""
        width, height, reasons = size_spec.plan_size(
            "flf", "16:9", megapixels=4.0)
        cap = vm.max_pixels_for_mode("flf")
        self.assertLessEqual(width * height, cap * 1024 * 1024 + 1)
        self.assertTrue(any("超过" in r for r in reasons),
                        f"没有给出超限原因：{reasons}")
        self.assertTrue(any("1920" in r or "缩到" in r for r in reasons),
                        reasons)

    def test_percent_is_relative_to_real_cap(self):
        """百分比以该 mode 的真实上限为基准。"""
        cap = vm.max_pixels_for_mode("flf")
        width, height, reasons = size_spec.plan_size(
            "flf", "16:9", percent=50)
        self.assertTrue(any("50" in r for r in reasons), reasons)
        # 50% 的面积应约等于上限的一半
        self.assertAlmostEqual(
            width * height / (1024 * 1024), cap * 0.5, delta=0.05)

    def test_percent_over_100_auto_reduced(self):
        """百分比超过 100 也要自动下调，不得静默产出超限尺寸。"""
        cap = vm.max_pixels_for_mode("flf")
        width, height, reasons = size_spec.plan_size(
            "flf", "16:9", percent=150)
        self.assertLessEqual(width * height, cap * 1024 * 1024 + 1)
        self.assertTrue(any("超过" in r for r in reasons), reasons)

    def test_megapixels_wins_over_percent(self):
        """给了 MP 预算就以它为准（百分比被忽略）。"""
        width_a, height_a, _ = size_spec.plan_size(
            "flf", "16:9", megapixels=0.4, percent=100)
        width_b, height_b, _ = size_spec.plan_size(
            "flf", "16:9", megapixels=0.4, percent=10)
        self.assertEqual((width_a, height_a), (width_b, height_b))

    def test_aspect_ratio_preserved(self):
        """换算后保持用户指定的比例。

        【容差说明】宽高要吸附到 32 的倍数，所以比例会有**微小**偏差
        （ComfyUI 原生 ResolutionSelector 也是这个行为）。
        容差按「一个 32 像素步进」折算到比例上。
        """
        for aspect, expect in (("16:9", 16 / 9), ("9:16", 9 / 16),
                               ("1:1", 1.0), ("4:3", 4 / 3),
                               ("21:9", 21 / 9)):
            width, height, _reasons = size_spec.plan_size(
                "flf", aspect, megapixels=0.8)
            ratio = width / height
            # 一个 32px 步进造成的比例误差上限（按短边估）
            delta = 32.0 / min(width, height) + 0.005
            self.assertAlmostEqual(ratio, expect, delta=delta,
                                   msg=f"{aspect} -> {width}×{height}")

    def test_apply_size_spec_writes_params_and_reasons(self):
        """写回 params.width/height 并带上调整原因。"""
        params = {"aspect": "16:9", "megapixels": 4.0}
        size_spec.apply_size_spec(params, "flf")
        self.assertGreater(params["width"], 0)
        self.assertGreater(params["height"], 0)
        self.assertTrue(params.get("size_adjust_reasons"))

    def test_explicit_width_height_not_overridden(self):
        """显式宽高不被 MP 预算覆盖（只做 32 对齐与上限检查）。

        【注意】1280×720 里 720 不是 32 的倍数 -> 会被吸附到 704。
        所以这里用**已经是 32 倍数**的尺寸来断言「不被覆盖」。
        """
        width, height, _ = size_spec.plan_size(
            "flf", "16:9", megapixels=0.4, width=1280, height=704)
        self.assertEqual((width, height), (1280, 704))

    def test_explicit_width_height_snapped_to_32(self):
        """【缺陷回归】显式宽高也必须吸附到 32 的倍数。

        原来显式宽高只过面积上限、不做 32 对齐 ——
        提交 1900×1000 会原样跑 1900×1000，而 H3 的 CANVAS_MULTIPLE=32
        要求对齐，否则 latent 网格与面板显示对不上。
        """
        params = {"aspect": "16:9", "width": 1900, "height": 1000}
        h3_video_size.apply_size(params, "flf")
        self.assertEqual(params["width"] % 32, 0, params)
        self.assertEqual(params["height"] % 32, 0, params)
        reasons = params.get("size_adjust_reasons") or []
        self.assertTrue(any("不是 32 的倍数" in r for r in reasons),
                        f"没有说明吸附原因：{reasons}")

    def test_explicit_width_height_over_cap_reduced(self):
        """显式宽高超面积上限 -> 自动缩 + 说明。"""
        params = {"aspect": "16:9", "width": 3000, "height": 3000}
        h3_video_size.apply_size(params, "flf")
        cap = vm.max_pixels_for_mode("flf")
        self.assertLessEqual(params["width"] * params["height"],
                             cap * 1024 * 1024 + 1)
        reasons = params.get("size_adjust_reasons") or []
        self.assertTrue(any("超过" in r for r in reasons), reasons)


class TestLegacyMigration(unittest.TestCase):
    """存量旧默认值必须在**读取时**迁移，且像素不变。"""

    def test_legacy_768p_migrates_to_same_pixels(self):
        """旧口径 768p = 0.8MP -> 1216×672，迁移后像素不变。"""
        migrated = gen_defaults.migrate_size_expression(
            "flf", {"aspect": "16:9", "resolution": "768p"})
        self.assertAlmostEqual(float(migrated["megapixels"]), 0.8, places=6)
        width, height, _ = size_spec.plan_size(
            "flf", "16:9", megapixels=migrated["megapixels"])
        self.assertEqual((width, height), (1216, 672))

    def test_migration_is_idempotent(self):
        source = {"aspect": "16:9", "resolution": "768p", "duration": 5.0}
        once = gen_defaults.migrate_size_expression("flf", source)
        twice = gen_defaults.migrate_size_expression("flf", once)
        self.assertEqual(once, twice)

    def test_migration_keeps_existing_megapixels(self):
        """存量已有 megapixels 时不被覆盖（用户配过的东西不动）。"""
        migrated = gen_defaults.migrate_size_expression(
            "t2v", {"megapixels": 1.5, "resolution": "768p"})
        self.assertEqual(float(migrated["megapixels"]), 1.5)

    def test_migration_adds_percent_when_missing(self):
        migrated = gen_defaults.migrate_size_expression(
            "t2v", {"megapixels": 0.4})
        self.assertEqual(float(migrated["resolution_percent"]), 100.0)

    def test_stored_legacy_config_reads_back_migrated(self):
        """端到端：老库存量 -> 读出来时已迁移成新表达。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "jobs.db"
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("CREATE TABLE model_provider_prefs "
                         "(key TEXT PRIMARY KEY, value TEXT)")
            legacy = {
                "max_steps": 30,
                "gen_defaults": {
                    "flf": {"aspect": "16:9", "resolution": "768p",
                            "duration": 5.0, "lora": "ckpt850"},
                    "r2v": {"aspect": "16:9", "resolution": "768p",
                            "duration": 5.0, "lora": "ckpt850"},
                },
            }
            conn.execute("INSERT INTO model_provider_prefs (key,value) "
                         "VALUES (?,?)",
                         ("agent_settings", json.dumps(legacy)))
            conn.commit()

            from h3studio import agent_settings
            settings = agent_settings.load(conn)
            for mode in ("flf", "r2v"):
                values = settings["gen_defaults"][mode]
                self.assertIn("megapixels", values,
                              f"{mode} 读出来仍没有 MP 预算")
                self.assertAlmostEqual(float(values["megapixels"]), 0.8,
                                       places=6)
                self.assertEqual(float(values["resolution_percent"]), 100.0)
            conn.close()


class TestBothPathsConsumeDefaults(unittest.TestCase):
    """面板提交与 Agent 自建 job 两条路径都吃同一份默认参数。"""

    def test_agent_path_applies_gen_defaults(self):
        """Agent 自建 job：默认参数被套进 params。"""
        defaults = {"flf": {"aspect": "16:9", "megapixels": 0.4,
                            "duration": 6.0}}
        params = {"prompt": "x"}
        applied = gen_defaults.merge_into_params(
            "flf", params, defaults, explicit=(), overridable=())
        self.assertIn("megapixels", applied)
        self.assertEqual(float(params["megapixels"]), 0.4)

    def test_agent_path_respects_explicit(self):
        """用户显式传的值优先于管理页默认值。"""
        defaults = {"flf": {"megapixels": 0.4}}
        params = {"prompt": "x", "megapixels": 1.0}
        gen_defaults.merge_into_params(
            "flf", params, defaults, explicit=("megapixels",), overridable=())
        self.assertEqual(float(params["megapixels"]), 1.0)

    def test_explicit_percent_suppresses_default_megapixels(self):
        """【缺陷回归】显式给了百分比，就**不能**再套管理页默认的 MP 预算。

        「MP 预算」与「MP 预算百分比」是同一件事的两种写法，
        而 plan_size 的优先级是 megapixels > percent。
        如果两个都进 params，用户的百分比会被无视 ——
        线上实测：提交 resolution_percent=50，落库却是管理页默认的
        0.8 MP（1216×672），而不是 50% × 2.0MP = 1.0 MP。
        """
        defaults = {"flf": {"megapixels": 0.8, "resolution_percent": 100.0}}
        params = {"prompt": "x", "resolution_percent": 50}
        gen_defaults.merge_into_params(
            "flf", params, defaults, explicit=("resolution_percent",),
            overridable=())
        self.assertNotIn("megapixels", params,
                         f"管理页默认的 megapixels 不该把用户的百分比盖掉：{params}")
        self.assertEqual(float(params["resolution_percent"]), 50.0)
        # 换算后应当是 50% × 2.0MP = 1.0 MP
        width, height, _ = size_spec.plan_size(
            "flf", "16:9", megapixels=params.get("megapixels"),
            percent=params.get("resolution_percent"))
        self.assertEqual((width, height), (1376, 768))

    def test_explicit_megapixels_suppresses_default_percent(self):
        """反过来：显式给了 MP 预算，就不套管理页默认的百分比。"""
        defaults = {"flf": {"megapixels": 0.8, "resolution_percent": 100.0}}
        params = {"prompt": "x", "megapixels": 0.4}
        gen_defaults.merge_into_params(
            "flf", params, defaults, explicit=("megapixels",), overridable=())
        self.assertNotIn("resolution_percent", params, params)
        self.assertEqual(float(params["megapixels"]), 0.4)

    def test_video_size_layer_consumes_mp_percent(self):
        """服务端尺寸归一层（flf/r2v 权威入口）吃 MP 预算。"""
        params = {"aspect": "16:9", "megapixels": 0.4}
        h3_video_size.apply_size(params, "flf")
        self.assertEqual((params["width"], params["height"]), (864, 480))

    def test_video_size_layer_percent(self):
        params = {"aspect": "16:9", "resolution_percent": 50}
        h3_video_size.apply_size(params, "flf")
        cap = vm.max_pixels_for_mode("flf")
        self.assertAlmostEqual(
            params["width"] * params["height"] / (1024 * 1024),
            cap * 0.5, delta=0.05)


class TestCapabilitySelfDescription(unittest.TestCase):
    """Agent 的能力自述必须反映新的 MP×百分比 表达。"""

    def test_video_capability_text_mentions_mp_and_percent(self):
        text = agent_capability.video_capability_text()
        self.assertIn("megapixels", text)
        self.assertIn("resolution_percent", text)

    def test_mode_schema_exposes_mp_and_percent(self):
        schema = agent_schema.mode_schema("flf")
        params = schema["params"]
        self.assertIn("megapixels", params)
        self.assertIn("resolution_percent", params)
        cap = vm.max_pixels_for_mode("flf")
        self.assertAlmostEqual(float(params["megapixels"]["max"]),
                               float(cap), places=6)

    def test_mode_schema_cap_follows_real_policy(self):
        """改真源 -> Agent schema 里的上限随之改变。"""
        original = vm.VIDEO_MAX_CANVAS_PIXELS
        try:
            vm.VIDEO_MAX_CANVAS_PIXELS = 1024 * 1024
            schema = agent_schema.mode_schema("flf")
            self.assertAlmostEqual(
                float(schema["params"]["megapixels"]["max"]), 1.0, places=6)
        finally:
            vm.VIDEO_MAX_CANVAS_PIXELS = original

    def test_image_modes_expose_mp_and_percent(self):
        """图片族同样有 MP 预算与百分比。"""
        for mode in ("t2i", "t2i_qwen", "t2i_moss", "t2i_h3image"):
            fields, supported = gen_defaults._mode_fields(mode)
            self.assertTrue(supported, mode)
            names = {f["name"] for f in fields}
            self.assertIn("megapixels", names, mode)
            self.assertIn("resolution_percent", names, mode)

    def test_music_modes_have_no_size_fields(self):
        """音乐没有尺寸概念 -> 不给 MP 字段（不臆造能力）。"""
        for mode in ("t2a", "a2a"):
            fields, _ = gen_defaults._mode_fields(mode)
            names = {f["name"] for f in fields}
            self.assertNotIn("megapixels", names, mode)
            self.assertNotIn("resolution_percent", names, mode)


if __name__ == "__main__":
    unittest.main()
