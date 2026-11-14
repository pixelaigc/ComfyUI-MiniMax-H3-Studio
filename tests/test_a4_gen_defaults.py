# -*- coding: utf-8 -*-
"""
需求 A4 —— 内置 Agent 管理页「每个生成类型分别配置默认参数」。

【这一层测什么】
  1. **规格**：每个生成类型的字段表只列**工作流真的读**的参数，
     合法取值全部来自真实注册表（不硬编码白名单）。
  2. **可存可读回**：每个生成类型的默认参数能存进库、能读回来。
  3. **老配置兼容**：老库里的 t2v_defaults 必须还能读出来、还能生效，
     且新结构与老结构**双向同步**。
  4. **非法值被归一**：脏值不得进库（枚举回落、数字夹区间、类型非法回落）。
  5. **运行时真的生效**：Agent 建任务时，用户显式传的参数优先，
     没传的用管理页配的值。

运行：python3 -m pytest tests/test_a4_gen_defaults.py -q
"""

import asyncio
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import agent as agent_mod              # noqa: E402
from h3studio import agent_settings                   # noqa: E402
from h3studio import gen_defaults                     # noqa: E402
from h3studio import gen_types                        # noqa: E402
from h3studio import music_yue2                       # noqa: E402
from h3studio import video_models as vm               # noqa: E402
from h3studio import workflows as wf                  # noqa: E402


#: 需求里点名要覆盖的生成类型
REQUIRED_MODES = [
    "t2v",          # MiniMax H3 文生视频
    "t2i",          # Z-IMAGE 文生图
    "t2i_moss",     # MOSS-Image 文生图
    "t2a",          # YuE2 音乐生成
    "t2i_qwen",     # Qwen-Image 2.1 文生图
    "t2i_h3image",  # H3-Image 文生图
    "i2i_qwen",     # Qwen 图片编辑
    "i2i_h3image",  # H3-Image 图片编辑
    "flf",          # 首尾帧生成
    "r2v",          # 参考图生成
    "a2a",          # 参考生音乐
    "longfilm",     # 长片拼接（无可调参数）
]


def _make_conn(tmp: Path):
    path = tmp / "jobs.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS model_provider_prefs "
                 "(key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    return conn


# ------------------------------------------------------------------ 1. 规格


class TestGenDefaultsSpec(unittest.TestCase):
    """字段规格必须反映**真实可调**的参数。"""

    def test_all_required_modes_are_present(self):
        spec = gen_defaults.spec()
        for mode in REQUIRED_MODES:
            self.assertIn(mode, spec, f"规格表缺少生成类型 {mode}")

    def test_longfilm_is_marked_as_no_params(self):
        spec = gen_defaults.spec()
        self.assertFalse(spec["longfilm"]["supported"])
        self.assertEqual(spec["longfilm"]["fields"], [])
        self.assertTrue(spec["longfilm"]["note"], "无参数也要写清原因")

    def test_supported_modes_have_fields(self):
        spec = gen_defaults.spec()
        for mode, info in spec.items():
            if not info["supported"]:
                continue
            self.assertTrue(info["fields"], f"{mode} 声明支持却没有字段")

    def test_field_names_are_unique_per_mode(self):
        for mode, info in gen_defaults.spec().items():
            names = [f["name"] for f in info["fields"]]
            self.assertEqual(len(names), len(set(names)),
                             f"{mode} 的字段名重复：{names}")

    def test_t2v_fields_match_real_video_registry(self):
        """t2v 的取值必须来自 video_models 的真实注册表。"""
        fields = {f["name"]: f for f in gen_defaults.spec()["t2v"]["fields"]}
        models = {o["value"] for o in fields["model_key"]["options"]}
        self.assertEqual(models, set(vm.VIDEO_MODELS.keys()))
        attentions = {o["value"] for o in fields["attention"]["options"]}
        self.assertEqual(attentions, set(vm.VIDEO_ATTENTION_MODES.keys())
                         - set(vm.VIDEO_REMOVED_ATTENTION_MODES.keys()))
        accels = {o["value"] for o in fields["accel_lora"]["options"]}
        self.assertEqual(accels, set(vm.VIDEO_ACCEL_LORA_COMBOS.keys()))

    def test_t2v_keeps_legacy_field_names(self):
        """老配置项的 6 个字段必须原样保留（兼容要求）。"""
        fields = {f["name"] for f in gen_defaults.spec()["t2v"]["fields"]}
        for name in agent_settings.__dict__.get("_legacy_fields", ()) or ():
            self.assertIn(name, fields)
        for name in ("model_key", "attention", "accel_lora", "aspect",
                     "megapixels", "duration"):
            self.assertIn(name, fields, f"t2v 丢了历史字段 {name}")

    def test_flf_and_r2v_have_no_attention_field(self):
        """旧工作流路径没有注意力机制 —— 不能给出「配了但不生效」的控件。"""
        for mode in ("flf", "r2v"):
            names = {f["name"] for f in gen_defaults.spec()[mode]["fields"]}
            self.assertNotIn("attention", names)
            self.assertNotIn("accel_lora", names)
            self.assertNotIn("model_key", names)
            self.assertIn("lora", names)
            self.assertIn("resolution", names)

    def test_image_modes_sampler_pool_comes_from_model_tables(self):
        """图像模式的采样器清单必须来自该族模型表。"""
        fields = {f["name"]: f for f in gen_defaults.spec()["t2i_moss"]["fields"]}
        samplers = {o["value"] for o in fields["sampler"]["options"]}
        expected = set()
        for meta in wf.MOSS_IMAGE_MODELS.values():
            expected.update(meta.get("samplers") or [])
        self.assertEqual(samplers, expected)

    def test_h3image_has_no_shift_field(self):
        """H3-Image 的 builder 不读 params['shift'] —— 不给这个控件。"""
        names = {f["name"] for f in gen_defaults.spec()["t2i_h3image"]["fields"]}
        self.assertNotIn("shift", names)

    def test_music_modes_use_yue2_registry(self):
        fields = {f["name"]: f for f in gen_defaults.spec()["t2a"]["fields"]}
        checkpoints = {o["value"] for o in fields["checkpoint"]["options"]}
        self.assertEqual(checkpoints, set(music_yue2.YUE2_CHECKPOINTS.keys()))
        formats = {o["value"] for o in fields["format"]["options"]}
        self.assertEqual(formats, set(music_yue2.YUE2_AUDIO_FORMATS))
        plans = {o["value"] for o in fields["plan_mode"]["options"]}
        self.assertEqual(plans, set(music_yue2.YUE2_MODES))

    def test_a2a_has_audio_encoder_but_t2a_does_not(self):
        """只有参考生音乐才需要音频编码器（SheetSage2）。"""
        t2a = {f["name"] for f in gen_defaults.spec()["t2a"]["fields"]}
        a2a = {f["name"] for f in gen_defaults.spec()["a2a"]["fields"]}
        self.assertNotIn("audio_encoder", t2a)
        self.assertIn("audio_encoder", a2a)

    def test_spec_payload_carries_labels(self):
        payload = gen_defaults.spec_payload()
        self.assertEqual(payload["t2v"]["label"], "文生视频")
        self.assertEqual(payload["t2i"]["label"], "Z-IMAGE 文生图")
        self.assertEqual(payload["t2a"]["label"], "文生音乐")

    def test_spec_modes_match_registered_modes(self):
        """规格表覆盖后端真实注册的全部模式（不多不少）。"""
        spec = set(gen_defaults.spec().keys())
        registered = set(gen_types.registered_modes())
        self.assertTrue(registered.issubset(spec),
                        f"规格表漏了模式：{registered - spec}")


# ------------------------------------------------------------------ 2. 归一


class TestGenDefaultsNormalize(unittest.TestCase):
    """非法值必须被归一 —— 脏值不进库。"""

    def test_invalid_enum_falls_back_to_default(self):
        out = gen_defaults.normalize_mode("t2v", {"attention": "不存在的机制"})
        self.assertEqual(out["attention"], "kitchen")

    def test_invalid_number_falls_back_or_clamps(self):
        out = gen_defaults.normalize_mode("t2v", {"megapixels": "abc",
                                                 "duration": 9999})
        self.assertEqual(out["megapixels"], 0.4)
        self.assertEqual(out["duration"], 60.0)

    def test_number_is_clamped_into_range(self):
        out = gen_defaults.normalize_mode("t2v", {"megapixels": 99,
                                                 "duration": 0.5})
        self.assertEqual(out["megapixels"], 2.0)
        self.assertEqual(out["duration"], 2.0)

    def test_invalid_model_falls_back(self):
        out = gen_defaults.normalize_mode("t2i", {"model": "no_such_model"})
        self.assertEqual(out["model"], wf.Z_IMAGE_DEFAULT_MODEL)

    def test_invalid_checkpoint_falls_back(self):
        out = gen_defaults.normalize_mode("t2a", {"checkpoint": "no_such"})
        self.assertEqual(out["checkpoint"], music_yue2.YUE2_DEFAULT_CHECKPOINT)

    def test_unknown_mode_yields_empty(self):
        self.assertEqual(gen_defaults.normalize_mode("no_such_mode", {"a": 1}),
                         {})

    def test_missing_keys_get_defaults(self):
        out = gen_defaults.normalize_mode("t2i", {})
        self.assertEqual(out["model"], wf.Z_IMAGE_DEFAULT_MODEL)
        self.assertEqual(out["steps"], wf.Z_IMAGE_MODELS[
            wf.Z_IMAGE_DEFAULT_MODEL]["steps"])

    def test_normalize_gen_defaults_covers_all_modes(self):
        out = gen_defaults.normalize_gen_defaults({})
        for mode in ("t2v", "t2i", "t2i_moss", "t2a", "flf", "r2v"):
            self.assertIn(mode, out)
            self.assertTrue(out[mode])


# ------------------------------------------------------------------ 3. 老配置兼容


class TestLegacyT2vCompat(unittest.TestCase):
    """老库里的 t2v_defaults 必须还能读出来、还能生效。"""

    def test_legacy_values_are_read_verbatim(self):
        out = agent_settings.normalize({"t2v_defaults": {
            "model_key": "fl2va_int8",
            "attention": "sparse",
            "accel_lora": "lightx2v_4step_768p",
            "aspect": "9:16",
            "megapixels": 0.8,
            "duration": 12,
        }})
        legacy = out["t2v_defaults"]
        self.assertEqual(legacy["model_key"], "fl2va_int8")
        self.assertEqual(legacy["attention"], "sparse")
        self.assertEqual(legacy["accel_lora"], "lightx2v_4step_768p")
        self.assertEqual(legacy["aspect"], "9:16")
        self.assertEqual(legacy["megapixels"], 0.8)
        self.assertEqual(legacy["duration"], 12.0)

    def test_legacy_values_reach_gen_defaults(self):
        """老配置的值必须真的进到新结构里（运行时读的是新结构）。"""
        out = agent_settings.normalize({"t2v_defaults": {
            "attention": "sparse", "duration": 12,
        }})
        self.assertEqual(out["gen_defaults"]["t2v"]["attention"], "sparse")
        self.assertEqual(out["gen_defaults"]["t2v"]["duration"], 12.0)

    def test_explicit_legacy_wins_over_gen_defaults(self):
        out = agent_settings.normalize({
            "gen_defaults": {"t2v": {"attention": "sage", "duration": 9}},
            "t2v_defaults": {"attention": "sparse"},
        })
        self.assertEqual(out["gen_defaults"]["t2v"]["attention"], "sparse")
        self.assertEqual(out["gen_defaults"]["t2v"]["duration"], 9.0)

    def test_partial_legacy_does_not_clobber_new_fields(self):
        """
        老客户端只 PUT t2v_defaults（6 个字段）时，
        **不能**把 gen_defaults 里新加的字段（步数/采样器/…）冲掉。
        """
        out = agent_settings.normalize({
            "gen_defaults": {"t2v": {"steps": 12, "sampler": "euler",
                                     "scheduler": "beta"}},
            "t2v_defaults": {"attention": "sparse"},
        })
        self.assertEqual(out["gen_defaults"]["t2v"]["steps"], 12)
        self.assertEqual(out["gen_defaults"]["t2v"]["sampler"], "euler")
        self.assertEqual(out["gen_defaults"]["t2v"]["attention"], "sparse")

    def test_write_syncs_both_structures(self):
        """写库时两者同步 —— 老脚本读到的永远是最新值。"""
        out = agent_settings.normalize({
            "gen_defaults": {"t2v": {"attention": "sage"}},
            "t2v_defaults": {"attention": "sage"},
        })
        self.assertEqual(out["t2v_defaults"]["attention"], "sage")
        self.assertEqual(out["gen_defaults"]["t2v"]["attention"], "sage")

    def test_legacy_dirty_value_is_not_stored(self):
        out = agent_settings.normalize({"t2v_defaults": {
            "attention": "不存在的机制", "megapixels": "abc"}})
        self.assertEqual(out["t2v_defaults"]["attention"], "kitchen")
        self.assertEqual(out["t2v_defaults"]["megapixels"], 0.4)

    def test_defaults_contain_both_structures(self):
        base = agent_settings.defaults()
        self.assertIn("gen_defaults", base)
        self.assertIn("t2v_defaults", base)
        self.assertEqual(base["t2v_defaults"]["attention"], "kitchen")
        self.assertEqual(base["gen_defaults"]["t2v"]["attention"], "kitchen")


# ------------------------------------------------------------------ 4. 持久化


class TestGenDefaultsPersistence(unittest.TestCase):
    """每个生成类型的默认参数能存进库、能读回来。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="a4-gen-defaults-"))
        self.conn = _make_conn(self.tmp)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_roundtrip_for_every_mode(self):
        payload = {
            "gen_defaults": {
                "t2v": {"attention": "sparse", "duration": 8.0},
                "t2i": {"model": "z_image_bf16", "steps": 25},
                "t2i_moss": {"cfg": 4.0, "slg_single_layers": "20"},
                "t2a": {"format": "mp3", "max_duration": 120.0},
                "flf": {"duration": 6.0},
                "r2v": {"lora": "v4step600"},
            },
        }
        saved = agent_settings.save(self.conn, payload)
        self.conn.commit()
        loaded = agent_settings.load(self.conn)
        for mode, values in payload["gen_defaults"].items():
            for key, value in values.items():
                self.assertEqual(loaded["gen_defaults"][mode][key], value,
                                 f"{mode}.{key} 读回来不对")
        # 老结构也同步了（老客户端读它）
        self.assertEqual(saved["t2v_defaults"]["attention"], "sparse")

    def test_saved_json_has_gen_defaults_key(self):
        agent_settings.save(self.conn, {"gen_defaults": {"t2i": {"steps": 30}}})
        self.conn.commit()
        row = self.conn.execute(
            "SELECT value FROM model_provider_prefs WHERE key=?",
            (agent_settings.PREF_KEY,)).fetchone()
        raw = json.loads(row["value"])
        self.assertIn("gen_defaults", raw)
        self.assertIn("t2v_defaults", raw)
        self.assertEqual(raw["gen_defaults"]["t2i"]["steps"], 30)

    def test_load_from_empty_db_returns_defaults(self):
        loaded = agent_settings.load(self.conn)
        self.assertIn("gen_defaults", loaded)
        self.assertEqual(loaded["gen_defaults"]["t2v"]["attention"], "kitchen")

    def test_dirty_values_never_reach_the_db(self):
        agent_settings.save(self.conn, {
            "gen_defaults": {
                "t2v": {"attention": "不存在的", "duration": 99999},
                "t2i": {"model": "no_such_model", "steps": "abc"},
            },
        })
        self.conn.commit()
        row = self.conn.execute(
            "SELECT value FROM model_provider_prefs WHERE key=?",
            (agent_settings.PREF_KEY,)).fetchone()
        raw = json.loads(row["value"])
        self.assertEqual(raw["gen_defaults"]["t2v"]["attention"], "kitchen")
        self.assertEqual(raw["gen_defaults"]["t2v"]["duration"], 60.0)
        self.assertEqual(raw["gen_defaults"]["t2i"]["model"],
                         wf.Z_IMAGE_DEFAULT_MODEL)


# ------------------------------------------------------------------ 5. 运行时


class FakeServer:
    """带「管理页配置」的最小服务端替身。"""

    def __init__(self, tmp: Path, settings: dict):
        self.tmp = tmp
        self.created = []
        self._settings = settings
        self._server_mod = None

    def db(self):
        conn = sqlite3.connect(self.tmp / "jobs.db")
        conn.row_factory = sqlite3.Row
        return conn

    def agent_settings_snapshot(self):
        return {"settings": self._settings}

    def prepare_job_params(self, mode, params, asset_refs=None,
                           saved_files=None):
        mod = self._server_mod
        return mod.prepare_job_params(mode, params, asset_refs, saved_files)

    def create_job_record(self, mode, params):
        job_id = f"job{len(self.created):04d}"
        self.created.append((job_id, mode, dict(params)))
        return job_id


class TestRuntimeEffect(unittest.TestCase):
    """Agent 建任务时默认值真的生效。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="a4-runtime-"))
        self.server_mod = _load_server_module()
        self.server = FakeServer(self.tmp, {})
        self.server._server_mod = self.server_mod
        self.ctx = agent_mod.AgentContext(server_module=self.server,
                                          provider={"name": "t", "model": "m"})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _set_settings(self, gen_defaults: dict):
        self.server._settings = agent_settings.normalize(
            {"gen_defaults": gen_defaults})

    def _call(self, args):
        return asyncio.run(
            agent_mod.TOOLS["create_job"]["handler"](self.ctx, args))

    def test_merge_into_params_fills_missing_only(self):
        params = {"prompt": "x", "aspect": "9:16"}
        defaults = {"t2i": {"aspect": "16:9", "steps": 25, "cfg": 4.0}}
        applied = gen_defaults.merge_into_params("t2i", params, defaults)
        self.assertEqual(params["aspect"], "9:16", "用户显式给的值不能被覆盖")
        self.assertEqual(params["steps"], 25)
        self.assertEqual(params["cfg"], 4.0)
        self.assertIn("steps", applied)

    def test_merge_respects_explicit_set(self):
        params = {"prompt": "x"}
        defaults = {"t2i": {"steps": 25}}
        gen_defaults.merge_into_params("t2i", params, defaults,
                                       explicit={"steps"})
        self.assertNotIn("steps", params)

    def test_merge_overrides_builtin_default_when_allowed(self):
        """管理页的值必须压过工作流内置默认值。"""
        params = {"prompt": "x", "steps": 8}
        defaults = {"t2i": {"steps": 25}}
        gen_defaults.merge_into_params("t2i", params, defaults,
                                       overridable={"steps"})
        self.assertEqual(params["steps"], 25)

    def test_empty_string_value_is_not_written(self):
        params = {"prompt": "x"}
        defaults = {"t2i_moss": {"slg_double_layers": ""}}
        gen_defaults.merge_into_params("t2i_moss", params, defaults)
        self.assertNotIn("slg_double_layers", params)

    def test_merge_is_a_noop_for_unknown_mode(self):
        params = {"prompt": "x"}
        applied = gen_defaults.merge_into_params("no_such", params, {})
        self.assertEqual(applied, [])

    def test_agent_uses_managed_defaults_for_t2i(self):
        """真实链路：Agent 建 t2i 任务时用上管理页配的默认值。"""
        self._set_settings({"t2i": {"steps": 33, "cfg": 5.5,
                                    "sampler": "euler"}})
        self._call({"mode": "t2i", "prompt": "一只猫"})
        self.assertEqual(len(self.server.created), 1)
        _, _, params = self.server.created[0]
        self.assertEqual(params["steps"], 33)
        self.assertEqual(params["cfg"], 5.5)
        self.assertEqual(params["sampler"], "euler")

    def test_explicit_user_value_wins_over_managed_default(self):
        self._set_settings({"t2i": {"steps": 33, "cfg": 5.5}})
        self._call({"mode": "t2i", "prompt": "一只猫", "steps": 12,
                    "cfg": 2.0})
        _, _, params = self.server.created[0]
        self.assertEqual(params["steps"], 12)
        self.assertEqual(params["cfg"], 2.0)

    def test_agent_uses_managed_defaults_for_t2a(self):
        self._set_settings({"t2a": {"format": "mp3", "max_duration": 120.0,
                                    "plan_mode": "melody"}})
        self._call({"mode": "t2a", "prompt": "一段摇滚"})
        _, _, params = self.server.created[0]
        self.assertEqual(params["format"], "mp3")
        self.assertEqual(params["max_duration"], 120.0)
        self.assertEqual(params["plan_mode"], "melody")

    def test_agent_uses_managed_defaults_for_moss(self):
        self._set_settings({"t2i_moss": {"cfg": 4.0,
                                         "slg_single_layers": "20"}})
        self._call({"mode": "t2i_moss", "prompt": "一座山"})
        _, _, params = self.server.created[0]
        self.assertEqual(params["cfg"], 4.0)
        self.assertEqual(params["slg_single_layers"], "20")

    def test_managed_defaults_are_reported(self):
        """工具结果要如实告知「套了哪些默认参数」。"""
        self._set_settings({"t2i": {"steps": 33}})
        result = self._call({"mode": "t2i", "prompt": "一只猫"})
        # defaults_applied 挂在任务条目上（ctx.jobs），
        # enhance_info 是给模型/用户看的文字说明。
        entry = self.ctx.jobs[0]
        self.assertIn("defaults_applied", entry)
        self.assertIn("steps", entry["defaults_applied"])
        self.assertTrue(any("默认参数" in line
                            for line in result.get("enhance_info") or []))

    def test_no_settings_keeps_builtin_behaviour(self):
        """服务端拿不到配置时，行为与改动前一致（用工作流内置默认值）。"""
        self._call({"mode": "t2i", "prompt": "一只猫"})
        _, _, params = self.server.created[0]
        self.assertEqual(params["steps"], wf.Z_IMAGE_MODELS[
            wf.Z_IMAGE_DEFAULT_MODEL]["steps"])

    def test_merge_t2v_defaults_still_applies(self):
        """t2v 的专用合并路径不受影响。"""
        merged = self.server_mod._merge_t2v_defaults(
            {}, {"model_key": "fl2va_int8", "attention": "kitchen",
                "accel_lora": "dmad_4step_critic", "aspect": "16:9",
                "megapixels": 0.4, "duration": 5.0})
        self.assertEqual(merged["model_key"], "fl2va_int8")
        self.assertEqual(merged["attention"], "kitchen")
        self.assertEqual(merged["accel_loras"][0]["key"], "dmad_4step_critic")

    def test_merge_t2v_defaults_fills_new_sampling_fields(self):
        """t2v 的新增字段（步数/采样器/…）也要被套上。"""
        merged = self.server_mod._merge_t2v_defaults(
            {}, {"model_key": "fl2va_int8", "attention": "kitchen",
                "accel_lora": "dmad_4step_critic", "aspect": "16:9",
                "megapixels": 0.4, "duration": 5.0,
                "steps": 8, "sampler": "res_multistep",
                "scheduler": "beta", "shift_video": 9.0,
                "shift_audio": 2.5})
        self.assertEqual(merged["steps"], 8)
        self.assertEqual(merged["sampler"], "res_multistep")
        self.assertEqual(merged["scheduler"], "beta")
        self.assertEqual(merged["shift_video"], 9.0)
        self.assertEqual(merged["shift_audio"], 2.5)


class TestGenDefaultsRoundTrip(unittest.TestCase):
    """管理页「读 -> 改 -> 存」往返后，用户改的值必须真的存住。

    【缺陷复现路径】GET /api/agent/settings 的响应里**总是**带着
    t2v_defaults（它是随 gen_defaults 一起下发的兼容镜像）。
    前端拿整份响应改一项再 PUT 回去时，如果服务端把 t2v_defaults 的
    键全算「显式给出」，用户改的 gen_defaults["t2v"].steps 就会被
    自己刚读到的旧镜像覆盖回去 —— 界面显示存好了，实际没变。

    【断言的是「那一行断言本身能抓住这个缺陷」】
    这里模拟前端的真实往返：读全量 settings -> 改一项 -> 存 -> 再读。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="a4-roundtrip-"))
        self.conn = _make_conn(self.tmp)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_edited_gen_defaults_survive_full_settings_roundtrip(self):
        """带整份 settings（含 t2v_defaults 镜像）往返，改动必须留住。"""
        # 第一次存：建立基线
        agent_settings.save(self.conn, {
            "gen_defaults": {"t2v": {"steps": 4}, "t2i": {"steps": 8}}})
        self.conn.commit()

        # 模拟前端：GET 全量 settings
        loaded = agent_settings.load(self.conn)
        payload = dict(loaded)
        payload["gen_defaults"] = dict(loaded["gen_defaults"])
        payload["gen_defaults"]["t2v"] = dict(loaded["gen_defaults"]["t2v"])
        payload["gen_defaults"]["t2v"]["steps"] = 7          # 用户改了这一项
        payload["gen_defaults"]["t2i"] = dict(loaded["gen_defaults"]["t2i"])
        payload["gen_defaults"]["t2i"]["steps"] = 5          # 以及这一项

        # PUT 回去
        agent_settings.save(self.conn, payload)
        self.conn.commit()

        # 再读 —— 必须还是用户改的值
        after = agent_settings.load(self.conn)
        self.assertEqual(after["gen_defaults"]["t2v"]["steps"], 7,
                         "t2v.steps 被兼容镜像覆盖回去了")
        self.assertEqual(after["gen_defaults"]["t2i"]["steps"], 5,
                         "t2i.steps 没存住")

    def test_legacy_only_client_still_works(self):
        """老客户端（只发 t2v_defaults）仍然生效 —— 兼容不能破。"""
        agent_settings.save(self.conn, {
            "t2v_defaults": {"attention": "sparse", "duration": 8.0}})
        self.conn.commit()
        loaded = agent_settings.load(self.conn)
        self.assertEqual(loaded["gen_defaults"]["t2v"]["attention"], "sparse")
        self.assertEqual(loaded["gen_defaults"]["t2v"]["duration"], 8.0)
        # 兼容镜像同步出去了
        self.assertEqual(loaded["t2v_defaults"]["attention"], "sparse")


def _load_server_module():
    import importlib
    sys.modules.pop("server", None)
    return importlib.import_module("server")


if __name__ == "__main__":
    unittest.main()
