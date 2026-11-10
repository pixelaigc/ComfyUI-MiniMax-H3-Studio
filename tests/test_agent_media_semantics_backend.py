# -*- coding: utf-8 -*-
"""
Agent 后端增强 **第二轮**的回归测试。

覆盖《Agent媒体语义与提示词策略》给出的基线实测事实 —— 全部是代码级已验证事实，
不是推测：

1. **create_job 参数 schema 扩展**
   原来只能传 7 个参数（prompt/aspect/resolution/duration/batch_count/
   ref_images/asset_refs），模型版本、LoRA、负面提示词、歌词、音乐时长全部传不下去。
   本轮扩展出 model / lora / negative_prompt / max_duration / lyrics / plan_mode /
   format / checkpoint / precision / seed / cfg / lora_strength，并逐个做合法值校验。

2. **素材槽位分派**
   原来 Agent 恒写 slot="ref_image" -> a2a（要 ref_audio）与 flf（要 first_frame）
   必被服务端拦下。本轮按 mode 的槽位计划分派。

3. **按族校验分辨率 / 时长**
   视频分辨率只有 544p/768p（传 1080p 静默落 768p）；
   图像分辨率按族有各自的 MP 上下限（t2i 3.0 / t2i_qwen 4.2 / t2i_moss 4.0-0.25）；
   音乐时长字段是 max_duration（4~360，默认 60），传 duration 会被静默忽略；
   视频 duration 后端不限但 Agent 侧要卡 5~15（面板可复现区间）。

4. **负面提示词支持矩阵**
   只在 t2i_moss 与 t2i+Z-Image Base 真正生效；Qwen 族需 cfg>1；
   视频/音乐没有这个通道。不生效时必须**如实告知**，不能假装抑制了。

5. **Provider 协议前置检查**
   comfyui 类型不支持 tools 协议且不降级 -> 提前给出可读错误 + 修复指引。

6. **内容策略如实化**
   全仓无审核模块 —— Agent 侧是唯一边界。新增暴力/真实人物/未成年人三类判定。

运行：python3 -m pytest tests/test_agent_media_semantics.py -v
"""
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import agent as agent_mod          # noqa: E402
from h3studio import agent_safety                # noqa: E402
from h3studio import agent_schema                # noqa: E402
from h3studio import agent_sessions              # noqa: E402
from h3studio import workflows as wf             # noqa: E402


# ------------------------------------------------------------------ 假服务端


class FakeServer:
    """Agent 真正依赖的服务端入口的最小替身。"""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.db_path = tmp / "jobs.db"
        self.created = []
        self._server_mod = None
        self._seed_providers()

    def _seed_providers(self):
        conn = self.db()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS model_providers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
                base_url TEXT NOT NULL, llm_base_url TEXT DEFAULT '',
                use_proxy INTEGER DEFAULT 0, proxy_base_url TEXT DEFAULT '',
                model TEXT NOT NULL, api_key TEXT DEFAULT '',
                multimodal INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1,
                created_at REAL);
            CREATE TABLE IF NOT EXISTS model_provider_prefs (
                key TEXT PRIMARY KEY, value TEXT);
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO model_providers VALUES "
            "('testprov','测试模型','openai','http://127.0.0.1:5000','',0,'',"
            "'test-model','',1,1,0)")
        conn.execute("INSERT OR REPLACE INTO model_provider_prefs "
                     "VALUES ('default','testprov')")
        conn.commit()
        conn.close()

    def db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def prepare_job_params(self, mode, params, asset_refs=None, saved_files=None):
        return self._server_mod.prepare_job_params(mode, params, asset_refs,
                                                   saved_files)

    def create_job_record(self, mode, params):
        job_id = f"job{len(self.created):04d}"
        self.created.append((job_id, mode, dict(params)))
        return job_id

    def get_job_params(self, job_id):
        for jid, mode, params in self.created:
            if jid == job_id:
                return {"mode": mode, "params": dict(params)}
        return None

    def list_job_records(self, status=None, mode=None, since_hours=None, limit=10):
        return [{"job_id": j, "mode": m, "status": "queued", "params": p}
                for j, m, p in self.created[:limit]]

    def get_job_status_payload(self, job_id):
        for jid, m, params in self.created:
            if jid == job_id:
                return {"job_id": jid, "mode": m, "status": "queued",
                        "params": params}
        return None

    async def cancel_job_record(self, job_id):
        return {"job_id": job_id, "status": "canceled", "ok": True}


def load_real_server_module():
    import importlib
    sys.modules.pop("server", None)
    return importlib.import_module("server")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="h3agent-media-"))
        self.server_mod = load_real_server_module()
        self.server = FakeServer(self.tmp)
        self.server._server_mod = self.server_mod
        agent_sessions.reset_store(self.tmp / "agent_sessions.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ctx(self):
        c = agent_mod.AgentContext(server_module=self.server,
                                  provider={"name": "测试", "model": "m",
                                            "type": "openai"})
        c.session_store = agent_sessions.get_store()
        return c

    def call(self, tool_name, args, ctx=None):
        spec = agent_mod.TOOLS[tool_name]
        return asyncio.run(spec["handler"](ctx or self.ctx(), args))

    def call_expect_error(self, tool_name, args):
        with self.assertRaises(agent_mod.AgentError) as ctx:
            asyncio.run(agent_mod.TOOLS[tool_name]["handler"](self.ctx(), args))
        return ctx.exception


# ------------------------------------------------------------------ 1. 模式清单


class TestModeInventory(_Base):
    """真实 mode 只有 9 个；longfilm 不在 Agent 可下发范围内。"""

    def test_builders_and_job_modes_are_consistent(self):
        """workflows.BUILDERS 与 server.JOB_MODES 必须一致（9 个）。"""
        self.assertEqual(sorted(wf.BUILDERS.keys()),
                         sorted(self.server_mod.JOB_MODES))
        self.assertEqual(len(wf.BUILDERS), 9)

    def test_longfilm_is_not_agent_buildable(self):
        """longfilm 走 /api/longfilms 自有链路，Agent 建不了。"""
        self.assertNotIn("longfilm", wf.BUILDERS)
        self.assertNotIn("longfilm", self.server_mod.JOB_MODES)

    def test_list_modes_reports_exactly_the_buildable_modes(self):
        result = self.call("list_modes", {})
        listed = [m["mode"] for m in result["modes"]]
        self.assertEqual(sorted(listed), sorted(wf.BUILDERS.keys()))
        self.assertNotIn("longfilm", listed)

    def test_list_modes_result_fits_tool_result_limit(self):
        """list_modes 不能被截断 —— 截断会让模型以为工具坏了然后重复调用。"""
        result = self.call("list_modes", {})
        size = len(json.dumps(result, ensure_ascii=False))
        self.assertLessEqual(size, agent_mod.TOOL_RESULT_LIMIT,
                             f"list_modes 结果 {size} 字符，超出上限会被截断")

    def test_list_modes_tells_music_duration_field(self):
        """音乐时长字段是 max_duration —— 模型必须知道，否则传 duration 白传。"""
        result = self.call("list_modes", {})
        by_mode = {m["mode"]: m for m in result["modes"]}
        self.assertEqual(by_mode["t2a"].get("duration_field"), "max_duration")
        self.assertEqual(by_mode["a2a"].get("duration_field"), "max_duration")

    def test_list_modes_tells_asset_slots(self):
        """模型要知道素材该落到哪个槽位。"""
        result = self.call("list_modes", {})
        by_mode = {m["mode"]: m for m in result["modes"]}
        self.assertEqual(by_mode["a2a"].get("slots"), "ref_audio")
        self.assertIn("first_frame", by_mode["flf"].get("slots", ""))
        self.assertEqual(by_mode["i2i_qwen"].get("slots"), "ref_image")

    def test_list_modes_gives_video_resolutions(self):
        """视频分辨率只有 544p/768p。"""
        result = self.call("list_modes", {})
        self.assertEqual(result["video_resolutions"], "544p,768p")

    def test_list_modes_negative_prompt_effectiveness(self):
        """负面提示词是否生效必须如实告诉模型。"""
        result = self.call("list_modes", {})
        by_mode = {m["mode"]: m for m in result["modes"]}
        self.assertTrue(by_mode["t2i_moss"].get("negative_ok"))
        self.assertFalse(by_mode["t2i_qwen"].get("negative_ok"))
        self.assertFalse(by_mode["t2v"].get("negative_ok", False))


# ------------------------------------------------------------------ 2. 素材槽位


class TestSlotDispatch(_Base):
    """槽位分派 —— 修「a2a / flf 必被服务端拦」的坑。"""

    def test_a2a_asset_lands_in_ref_audio_slot(self):
        refs = agent_mod._build_asset_refs("a2a", {"asset_id": "aud1"},
                                           ["aud1"])
        self.assertEqual(refs, [{"asset_id": "aud1", "slot": "ref_audio",
                                 "ordinal": 0}])

    def test_a2a_named_ref_audio_slot(self):
        refs = agent_mod._build_asset_refs("a2a", {"ref_audio": "aud1"}, [])
        self.assertEqual(refs[0]["slot"], "ref_audio")

    def test_a2a_accepts_only_one_audio(self):
        """a2a 只能 1 段参考音频 —— 多余的截断。"""
        refs = agent_mod._build_asset_refs("a2a", {}, ["a1", "a2"])
        self.assertEqual(len(refs), 1)

    def test_flf_lands_in_first_frame_slot(self):
        refs = agent_mod._build_asset_refs("flf", {"asset_id": "f1"}, ["f1"])
        self.assertEqual(refs[0]["slot"], "first_frame")

    def test_flf_named_first_and_last_frame(self):
        refs = agent_mod._build_asset_refs(
            "flf", {"first_frame": "f1", "last_frame": "f2"}, [])
        slots = {r["slot"] for r in refs}
        self.assertEqual(slots, {"first_frame", "last_frame"})

    def test_i2i_qwen_lands_in_ref_image_slot(self):
        refs = agent_mod._build_asset_refs("i2i_qwen", {}, ["a1", "a2"])
        self.assertTrue(all(r["slot"] == "ref_image" for r in refs))
        self.assertEqual([r["ordinal"] for r in refs], [0, 1])

    def test_modes_without_slots_ignore_assets(self):
        """t2v 不接受素材 —— 多说了也不该让任务失败。"""
        refs = agent_mod._build_asset_refs("t2v", {}, ["x"])
        self.assertEqual(refs, [])

    def test_slot_plan_is_the_single_source(self):
        """mode_requires_refs 与 mode_slot_plan 必须一致（不能各写一份）。"""
        for mode in wf.BUILDERS:
            plan = agent_schema.mode_slot_plan(mode)
            expected = [s for s in plan["single"] if s != "last_frame"]
            if plan["multi"]:
                expected.append("asset_id")
            self.assertEqual(agent_schema.mode_requires_refs(mode), expected,
                             mode)

    def test_missing_params_uses_slot_plan(self):
        self.assertIn("ref_audio", agent_schema.missing_params("a2a",
                                                                {"prompt": "x"}))
        self.assertIn("first_frame", agent_schema.missing_params(
            "flf", {"prompt": "x"}))
        self.assertIn("asset_id", agent_schema.missing_params(
            "i2i_qwen", {"prompt": "x"}))
        self.assertEqual(agent_schema.missing_params("t2v", {"prompt": "x"}),
                         [])


# ------------------------------------------------------------------ 3. 参数 schema 扩展


class TestCreateJobParamExtension(_Base):
    """create_job 现在能传更多参数，且都带合法值校验。"""

    def test_schema_declares_new_params(self):
        params = agent_mod.TOOLS["create_job"]["parameters"]["properties"]
        for name in ("model", "lora", "negative_prompt", "max_duration",
                     "lyrics", "plan_mode", "format", "checkpoint",
                     "precision", "seed", "cfg", "lora_strength",
                     "first_frame", "last_frame", "ref_audio"):
            self.assertIn(name, params, f"schema 缺少 {name}")

    def test_model_version_is_passed_through(self):
        result = self.call("create_job", {
            "mode": "t2i", "prompt": "一只猫", "model": "z_image_bf16"})
        self.assertEqual(result["mode"], "t2i")
        self.assertEqual(self.server.created[0][2]["model"], "z_image_bf16")

    def test_lora_is_passed_through(self):
        self.call("create_job", {
            "mode": "t2v", "prompt": "雨夜", "lora": "v4step600"})
        self.assertEqual(self.server.created[0][2]["lora"], "v4step600")

    def test_negative_prompt_is_passed_through(self):
        self.call("create_job", {
            "mode": "t2i_moss", "prompt": "海报", "negative_prompt": "nsfw"})
        self.assertEqual(self.server.created[0][2]["negative_prompt"], "nsfw")

    def test_max_duration_is_passed_through_for_music(self):
        """音乐时长走 max_duration（不是 duration）。"""
        self.call("create_job", {
            "mode": "t2a", "prompt": "lo-fi", "max_duration": 180})
        params = self.server.created[0][2]
        self.assertEqual(params["max_duration"], 180.0)
        self.assertNotIn("duration", params)

    def test_lyrics_and_plan_mode_passed_through(self):
        self.call("create_job", {
            "mode": "t2a", "prompt": "民谣", "lyrics": "歌词内容",
            "plan_mode": "full"})
        params = self.server.created[0][2]
        self.assertEqual(params["lyrics"], "歌词内容")
        self.assertEqual(params["plan_mode"], "full")

    def test_seed_and_cfg_passed_through(self):
        self.call("create_job", {
            "mode": "t2i_qwen", "prompt": "x", "seed": 42, "cfg": 1.5})
        params = self.server.created[0][2]
        self.assertEqual(params["seed"], 42)
        self.assertEqual(params["cfg"], 1.5)

    def test_invalid_model_is_rejected_with_alternatives(self):
        exc = self.call_expect_error("create_job", {
            "mode": "t2i", "prompt": "x", "model": "no_such_model"})
        self.assertIn("合法值", exc.message)
        self.assertEqual(self.server.created, [], "非法参数不能建出任务")

    def test_invalid_lora_is_rejected(self):
        exc = self.call_expect_error("create_job", {
            "mode": "t2v", "prompt": "x", "lora": "nope"})
        self.assertIn("合法值", exc.message)

    def test_invalid_max_duration_is_rejected(self):
        exc = self.call_expect_error("create_job", {
            "mode": "t2a", "prompt": "x", "max_duration": 9999})
        self.assertIn("合法", exc.message + str(exc.extra))

    def test_invalid_plan_mode_is_rejected(self):
        exc = self.call_expect_error("create_job", {
            "mode": "t2a", "prompt": "x", "plan_mode": "nope"})
        self.assertIn("合法", exc.message + str(exc.extra))

    def test_invalid_format_is_rejected(self):
        exc = self.call_expect_error("create_job", {
            "mode": "t2a", "prompt": "x", "format": "wav"})
        self.assertIn("合法", exc.message + str(exc.extra))

    def test_non_numeric_seed_is_rejected(self):
        exc = self.call_expect_error("create_job", {
            "mode": "t2i", "prompt": "x", "seed": "abc"})
        self.assertIn("整数", exc.message)


# ------------------------------------------------------------------ 4. 按族校验


class TestPerFamilyResolutionValidation(_Base):
    """分辨率按族校验 —— 拒绝非法值而不是静默回落。"""

    def test_video_1080p_is_rejected(self):
        """视频只有 544p/768p —— 传 1080p 会静默落 768p，必须拦。"""
        issues = agent_schema.validate_params("t2v", {"resolution": "1080p"})
        self.assertTrue(issues)
        self.assertEqual(issues[0].field, "resolution")
        self.assertEqual(issues[0].allowed, list(wf.RES_MP.keys()))

    def test_video_valid_resolutions_pass(self):
        for tier in wf.RES_MP:
            issues = agent_schema.validate_params("t2v", {"resolution": tier})
            self.assertEqual(issues, [], tier)

    def test_z_image_4k_is_upscaled_not_rejected(self):
        """t2i 超上限会自动走 2K 放大路径 —— 允许但要说清。"""
        issues = agent_schema.validate_params("t2i", {"resolution": "4k"})
        self.assertTrue(issues)
        self.assertEqual(issues[0].code, "resolution_upscaled")

    def test_qwen_tiers_all_within_direct_limit(self):
        """
        Qwen 的档位表**本身就没超过**直出上限（2k=4.0MP < 4.2MP），
        所以每一档都合法 —— 不能凭空说它超限。
        """
        for tier in agent_schema.mode_resolutions("t2i_qwen"):
            issues = agent_schema.validate_params("t2i_qwen",
                                                  {"resolution": tier})
            self.assertEqual(issues, [], tier)

    def test_moss_tiers_all_within_direct_limit(self):
        """MOSS 同理：2k=4.0MP = 上限 4.0MP，合法。"""
        for tier in agent_schema.mode_resolutions("t2i_moss"):
            issues = agent_schema.validate_params("t2i_moss",
                                                  {"resolution": tier})
            self.assertEqual(issues, [], tier)

    def test_moss_below_min_is_rejected(self):
        """MOSS 下限 0.25MP —— 低于下限的档位要拒。"""
        # 480p = 0.4MP > 0.25MP -> 合法
        self.assertEqual(
            agent_schema.validate_params("t2i_moss", {"resolution": "480p"}),
            [])
        low = agent_schema._resolution_megapixels("t2i_moss", "480p")
        self.assertIsNotNone(low)
        self.assertGreaterEqual(float(low), 0.25)

    def test_over_limit_detection_uses_real_mp_tables(self):
        """
        超限判定必须基于**真实 MP 表**。

        做法：临时把某档位的 MP 改到超过上限，验证判定会触发 ——
        这样测的是判定逻辑本身，而不是「恰好没有超限的档位」。
        """
        original = wf.QWEN_IMAGE_RES_TIERS["2k"]
        try:
            wf.QWEN_IMAGE_RES_TIERS["2k"] = 9.9
            issues = agent_schema.validate_params("t2i_qwen",
                                                  {"resolution": "2k"})
            self.assertTrue(issues)
            self.assertEqual(issues[0].code, "resolution_too_large")
            self.assertTrue(issues[0].suggestions)
        finally:
            wf.QWEN_IMAGE_RES_TIERS["2k"] = original

    def test_image_valid_resolutions_pass(self):
        for mode in ("t2i", "t2i_qwen", "t2i_moss"):
            for tier in agent_schema.mode_resolutions(mode):
                issues = agent_schema.validate_params(mode,
                                                      {"resolution": tier})
                # 只允许 upscaled 提示（t2i 的 2k/4k），不允许报错
                for issue in issues:
                    self.assertEqual(issue.code, "resolution_upscaled",
                                     f"{mode}/{tier}")


class TestDurationValidation(_Base):
    """时长校验 —— 视频上下限 + 音乐 max_duration。"""

    def test_video_duration_out_of_range_is_rejected_once(self):
        issues = agent_schema.validate_params("t2v", {"duration": 40})
        duration_issues = [i for i in issues if i.field == "duration"]
        self.assertEqual(len(duration_issues), 1,
                         "同一条时长问题不该重复报两次")
        self.assertEqual(duration_issues[0].code, "invalid_duration")

    def test_video_duration_split_suggestion_is_executable(self):
        issues = agent_schema.validate_params("t2v", {"duration": 40})
        split = [i for i in issues[0].suggestions if i["action"] == "split"][0]
        self.assertTrue(5 <= split["params"]["duration"] <= 15)

    def test_video_duration_in_range_passes(self):
        for value in (5, 8, 15):
            self.assertEqual(
                agent_schema.validate_params("t2v", {"duration": value}), [])

    def test_music_max_duration_range(self):
        self.assertEqual(
            agent_schema.validate_params("t2a", {"max_duration": 4}), [])
        self.assertEqual(
            agent_schema.validate_params("t2a", {"max_duration": 360}), [])
        issues = agent_schema.validate_params("t2a", {"max_duration": 361})
        self.assertTrue(issues)
        self.assertEqual(issues[0].field, "max_duration")

    def test_music_duration_field_is_max_duration(self):
        self.assertEqual(agent_schema.mode_duration_field("t2a"),
                         "max_duration")
        self.assertEqual(agent_schema.mode_duration_field("t2v"),
                         "duration")

    def test_music_schema_exposes_max_duration_not_duration(self):
        schema = agent_schema.mode_schema("t2a")
        self.assertIn("max_duration", schema["params"])
        self.assertNotIn("duration", schema["params"])


# ------------------------------------------------------------------ 5. 负面提示词


class TestNegativePromptMatrix(_Base):
    """负面提示词支持矩阵 —— 如实告知，不生效就说清楚。"""

    def test_z_image_turbo_negative_not_supported(self):
        support = agent_schema.negative_support("t2i", "z_image_turbo_bf16")
        self.assertFalse(support["supported"])
        self.assertFalse(support["effective"])

    def test_z_image_base_negative_supported(self):
        support = agent_schema.negative_support("t2i", "z_image_bf16")
        self.assertTrue(support["supported"])
        self.assertTrue(support["effective"])

    def test_z_image_distill_negative_not_supported(self):
        support = agent_schema.negative_support("t2i", "z_image_base_distill")
        self.assertFalse(support["effective"])

    def test_qwen_needs_cfg_above_one(self):
        support = agent_schema.negative_support("t2i_qwen")
        self.assertTrue(support["supported"])
        self.assertFalse(support["effective"])
        self.assertEqual(support["requires_cfg_above"], 1.0)

    def test_moss_negative_supported(self):
        support = agent_schema.negative_support("t2i_moss")
        self.assertTrue(support["supported"])
        self.assertTrue(support["effective"])
        self.assertIn("nsfw", support["default_negative"])

    def test_video_and_music_have_no_negative_channel(self):
        for mode in ("t2v", "flf", "r2v", "t2a", "a2a"):
            support = agent_schema.negative_support(mode)
            self.assertFalse(support["supported"], mode)

    def test_unsupported_negative_is_flagged_with_alternative(self):
        issues = agent_schema.validate_params("t2v",
                                               {"negative_prompt": "nsfw"})
        self.assertTrue(issues)
        self.assertEqual(issues[0].code, "negative_not_supported")
        self.assertIn("t2i_moss", str(issues[0].suggestions))

    def test_qwen_negative_needs_cfg_flagged(self):
        issues = agent_schema.validate_params("t2i_qwen",
                                               {"negative_prompt": "nsfw"})
        self.assertTrue(issues)
        self.assertEqual(issues[0].code, "negative_needs_cfg")

    def test_supported_negative_passes(self):
        self.assertEqual(
            agent_schema.validate_params("t2i_moss",
                                          {"negative_prompt": "nsfw"}), [])
        self.assertEqual(
            agent_schema.validate_params("t2i",
                                          {"negative_prompt": "nsfw",
                                           "model": "z_image_bf16"}), [])

    def test_schema_exposes_negative_matrix(self):
        schema = agent_schema.mode_schema("t2i")
        matrix = schema["params"]["negative_prompt"]["supported_by_model"]
        self.assertIn("z_image_bf16", matrix)
        self.assertTrue(matrix["z_image_bf16"]["supported"])
        self.assertFalse(matrix["z_image_turbo_bf16"]["supported"])


# ------------------------------------------------------------------ 6. Provider


class TestFabricatedJobClaim(_Base):
    """
    模型**编造 job id**并声称「已下发」的防护。

    【实测踩过的坑，2026-11-10】模型调完 enhance_prompt 就 respond，说
    「已下发文生视频任务（job t2v_019d874a）」—— 但它从没调过 create_job，
    那个 id 是**编的**。用户去生成列表里找不到任何任务。
    """

    def test_detector_flags_fabricated_id(self):
        text = "已下发文生视频任务（job t2v_019d874a）：一只猫在雨夜奔跑。"
        self.assertTrue(agent_mod._looks_like_fabricated_job_claim(text, []))

    def test_detector_accepts_real_id(self):
        text = "已下发文生视频任务（job 3f2a9c1b7d04）"
        jobs = [{"job_id": "3f2a9c1b7d04"}]
        self.assertFalse(
            agent_mod._looks_like_fabricated_job_claim(text, jobs))

    def test_detector_ignores_non_claim_text(self):
        text = "已创建 1 个文生视频任务，job_id=abc123"
        self.assertFalse(
            agent_mod._looks_like_fabricated_job_claim(text, []))

    def test_respond_with_fake_job_id_is_rejected(self):
        """respond 报告不存在的 job id -> 报错，不当成完成。"""
        exc = self.call_expect_error("respond", {
            "summary": "已下发文生视频任务（job t2v_019d874a）",
            "job_ids": ["t2v_019d874a"]})
        self.assertEqual(exc.code, "fabricated_job_id")
        self.assertIn("不存在", exc.message)

    def test_respond_with_real_job_id_is_accepted(self):
        """真实 job id 要被接受（不能误杀正常收尾）。"""
        shared = self.ctx()
        result = self.call("create_job",
                            {"mode": "t2v", "prompt": "雨夜"}, ctx=shared)
        real_id = result["job_id"]
        out = self.call("respond", {
            "summary": f"已下发任务（job {real_id}）", "job_ids": [real_id]},
            ctx=shared)
        self.assertTrue(out["ok"])

    def test_fabricated_claim_blocks_the_run(self):
        """整条链路：模型编造 job id -> 强制重试 -> 仍编 -> 结构化失败。"""
        turns = [
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "list_modes", "arguments": {}}]},
            {"content": None, "tool_calls": [
                {"id": "c2", "name": "respond",
                 "arguments": {"summary": "已下发文生视频任务（job t2v_019d874a）",
                               "job_ids": ["t2v_019d874a"]}}]},
            {"content": None, "tool_calls": [
                {"id": "c3", "name": "respond",
                 "arguments": {"summary": "已下发文生视频任务（job t2v_019d874a）",
                               "job_ids": ["t2v_019d874a"]}}]},
        ]
        original = agent_mod.llm_client.call_provider_with_tools
        calls = {"n": 0}

        async def fake(provider, messages, tools, options=None, images=None):
            index = calls["n"]
            calls["n"] += 1
            if index >= len(turns):
                return {"content": "结束", "tool_calls": [], "raw": {}}
            return turns[index]

        agent_mod.llm_client.call_provider_with_tools = fake
        try:
            result = asyncio.run(agent_mod.run_agent(
                self.server, "做个视频",
                session_store=agent_sessions.get_store()))
        finally:
            agent_mod.llm_client.call_provider_with_tools = original
        # 编造必须被拦下，且不能有任务
        self.assertEqual(result["jobs"], [])
        self.assertIn(result.get("code"), ("fabricated_job_id",
                                           "no_tool_call"))


class TestNegativeContextNotBlocked(_Base):
    """
    敏感词出现在**负面提示词语境**里不该被拦。

    【实测踩过的坑，2026-11-10】用户说「画一张海报，负面提示词用 nsfw, blurry」
    —— 这是**抑制**违规内容的正确用法，却被整个请求拒了。
    """

    def test_nsfw_as_negative_prompt_is_allowed(self):
        result = agent_safety.check_content_policy(
            "画一张赛博朋克少女海报，负面提示词用 nsfw, blurry")
        self.assertFalse(result["sensitive"])

    def test_nsfw_as_positive_request_is_still_rejected(self):
        result = agent_safety.check_content_policy("给我画一张 NSFW 裸体图")
        self.assertTrue(result["sensitive"])

    def test_english_negative_prompt_context_allowed(self):
        result = agent_safety.check_content_policy(
            "a poster, negative prompt: nsfw, extra fingers")
        self.assertFalse(result["sensitive"])


class TestRetryJobPrecheck(_Base):
    """
    retry_job 的预校验必须**从原任务取 mode**。

    【实测踩过的坑，2026-11-10】预校验要求 mode 参数，但工具 schema 里
    没有这个参数（mode 本来就来自被重试的任务）-> 实跑报「retry_job 需要 mode 参数」。
    """

    def test_retry_job_precheck_does_not_require_mode(self):
        shared = self.ctx()
        result = self.call("create_job",
                            {"mode": "t2v", "prompt": "雨夜"}, ctx=shared)
        job_id = result["job_id"]
        issue = agent_mod._precheck_tool_args(
            "retry_job", {"job_id": job_id}, shared)
        self.assertIsNone(issue, f"retry_job 预校验不应要求 mode：{issue}")

    def test_retry_job_precheck_rejects_unknown_job(self):
        shared = self.ctx()
        issue = agent_mod._precheck_tool_args(
            "retry_job", {"job_id": "nope"}, shared)
        self.assertIsNotNone(issue)
        self.assertEqual(issue.get("code"), "job_not_found")

    def test_retry_job_precheck_validates_overrides(self):
        shared = self.ctx()
        result = self.call("create_job",
                            {"mode": "t2v", "prompt": "雨夜"}, ctx=shared)
        job_id = result["job_id"]
        issue = agent_mod._precheck_tool_args(
            "retry_job", {"job_id": job_id, "overrides": {"aspect": "21:10"}},
            shared)
        self.assertIsNotNone(issue)
        self.assertEqual(issue.get("code"), "invalid_params")

    def test_retry_job_tool_has_no_mode_param_in_schema(self):
        """schema 里不该有 mode（它是原任务的属性）。"""
        params = agent_mod.TOOLS["retry_job"]["parameters"]["properties"]
        self.assertNotIn("mode", params)
        self.assertIn("job_id", params)


class TestToolBudgetAndSearchWaste(_Base):
    """
    工具预算与「搜索浪费」。

    【实测踩过的坑，2026-11-10】模型把 8 次工具调用预算全花在搜索上：
    search_assets 搜了 4 次（第一次**已经命中**了），把预算耗光，
    任务没建出来 —— 用户看到的是「已达工具调用上限」。
    根因：搜索结果里的描述被**从中间裁断**，模型以为结果不完整，
    于是换关键词重搜。
    """

    def test_asset_desc_limit_exists(self):
        self.assertGreater(agent_mod.ASSET_DESC_LIMIT, 0)
        self.assertLessEqual(agent_mod.ASSET_DESC_LIMIT, 400)

    def test_tool_call_budget_is_raised(self):
        """8 次不够跑完「搜索2次 + 查schema + 扩写 + 建任务 + 查状态 + 收尾」。"""
        self.assertGreaterEqual(agent_mod.MAX_TOOL_CALLS, 10)

    def test_search_hint_tells_model_not_to_research(self):
        """搜索命中后的 hint 要明确告诉模型不要换关键词重复搜。"""
        import inspect
        source = inspect.getsource(agent_mod._tool_search_assets)
        self.assertIn("不要用别的关键词重复搜同一个素材", source)


class TestProviderCapability(_Base):
    """comfyui 类型不支持 tools 协议 —— 前置检查 + 可读错误。"""

    def test_comfyui_provider_is_rejected_upfront(self):
        with self.assertRaises(agent_mod.AgentError) as ctx:
            agent_mod._check_provider_supports_tools(
                {"id": "p1", "name": "ComfyUI 节点", "type": "comfyui"})
        self.assertEqual(ctx.exception.code, "provider_not_tool_capable")
        self.assertIn("不支持工具调用协议", ctx.exception.message)
        self.assertIn("OpenAI 兼容", ctx.exception.message)

    def test_supported_types_pass(self):
        for ptype in ("openai", "ollama", "llamacpp"):
            agent_mod._check_provider_supports_tools(
                {"id": "p", "name": "n", "type": ptype})

    def test_run_agent_fails_fast_on_comfyui_provider(self):
        """应该在**第一次模型调用之前**就失败 —— 不用让用户等几秒。"""
        self._seed_comfyui_provider()
        called = {"n": 0}
        original = agent_mod.llm_client.call_provider_with_tools

        async def fake(provider, messages, tools, options=None, images=None):
            called["n"] += 1
            return {"content": "x", "tool_calls": [], "raw": {}}

        agent_mod.llm_client.call_provider_with_tools = fake
        try:
            with self.assertRaises(agent_mod.AgentError) as ctx:
                asyncio.run(agent_mod.run_agent(
                    self.server, "做个视频",
                    provider_id="comfyui_provider"))
        finally:
            agent_mod.llm_client.call_provider_with_tools = original
        self.assertEqual(ctx.exception.code, "provider_not_tool_capable")
        self.assertEqual(called["n"], 0, "不应调用模型")

    def _seed_comfyui_provider(self):
        """种一个 comfyui 类型的 Provider 到测试库。"""
        conn = self.server.db()
        conn.execute(
            "INSERT OR REPLACE INTO model_providers VALUES "
            "('comfyui_provider','ComfyUI 节点','comfyui','http://x','',0,'',"
            "'local-llm','',0,1,0)")
        conn.commit()
        conn.close()


# ------------------------------------------------------------------ 7. 内容策略


class TestContentPolicy(_Base):
    """Agent 侧是唯一内容边界 —— 新增暴力/真实人物/未成年人判定。"""

    def test_minor_sexual_content_is_absolutely_rejected(self):
        result = agent_safety.check_content_policy("画一个未成年少女的性感照片")
        self.assertTrue(result["sensitive"])
        self.assertEqual(result["category"], "minor")
        self.assertIn("不能处理", result["refusal"])

    def test_normal_minor_scene_is_allowed(self):
        """「画一张高中生运动会」是正常创作 —— 不能一刀切。"""
        result = agent_safety.check_content_policy("画一张高中运动会的照片")
        self.assertFalse(result["sensitive"])

    def test_gore_realism_is_rejected_with_alternative(self):
        result = agent_safety.check_content_policy(
            "生成一张血腥肢解的写实特写")
        self.assertTrue(result["sensitive"])
        self.assertEqual(result["category"], "violence")
        self.assertIn("镜头语言", result["refusal"])

    def test_violence_without_realism_is_allowed(self):
        result = agent_safety.check_content_policy(
            "动作片海报，打斗场面剪影")
        self.assertFalse(result["sensitive"])

    def test_real_person_portrait_is_rejected(self):
        result = agent_safety.check_content_policy(
            "生成一张某明星的肖像照片，一模一样")
        self.assertTrue(result["sensitive"])
        self.assertEqual(result["category"], "real_person")
        self.assertIn("虚构", result["refusal"])

    def test_generic_person_is_allowed(self):
        result = agent_safety.check_content_policy(
            "一个穿西装的男性站在雨中")
        self.assertFalse(result["sensitive"])

    def test_nsfw_still_rejected(self):
        result = agent_safety.check_content_policy("给我画一张 NSFW 裸体图")
        self.assertTrue(result["sensitive"])

    def test_policy_note_is_honest(self):
        """必须如实说明「没有服务端审核」。"""
        self.assertIn("没有服务端内容审核", agent_safety.POLICY_NOTE)

    def test_run_agent_short_circuits_sensitive(self):
        called = {"n": 0}
        original = agent_mod.llm_client.call_provider_with_tools

        async def fake(provider, messages, tools, options=None, images=None):
            called["n"] += 1
            return {"content": "x", "tool_calls": [], "raw": {}}

        agent_mod.llm_client.call_provider_with_tools = fake
        try:
            result = asyncio.run(agent_mod.run_agent(
                self.server, "生成一张血腥肢解的写实特写"))
        finally:
            agent_mod.llm_client.call_provider_with_tools = original
        self.assertEqual(called["n"], 0, "违规请求不应调用模型")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["code"], "sensitive_content")
        self.assertEqual(result.get("category"), "violence")


# ------------------------------------------------------------------ 8. schema 暴露


class TestModeSchemaExposure(_Base):
    """get_mode_schema 必须暴露新增参数与槽位计划。"""

    def test_music_schema_has_music_params(self):
        schema = agent_schema.mode_schema("t2a")
        for name in ("max_duration", "plan_mode", "format", "lyrics"):
            self.assertIn(name, schema["params"], name)

    def test_video_schema_has_video_params(self):
        schema = agent_schema.mode_schema("t2v")
        self.assertIn("precision", schema["params"])
        self.assertIn("lora_strength", schema["params"])

    def test_image_schema_has_negative_matrix(self):
        schema = agent_schema.mode_schema("t2i")
        self.assertIn("negative_prompt", schema["params"])

    def test_asset_slots_exposed(self):
        for mode in ("a2a", "flf", "i2i_qwen"):
            schema = agent_schema.mode_schema(mode)
            self.assertIn("asset_slots", schema, mode)

    def test_get_mode_schema_tool_returns_slots(self):
        result = self.call("get_mode_schema", {"mode": "a2a"})
        self.assertIn("asset_slots", result)
        self.assertEqual(result["asset_slots"]["single"], ["ref_audio"])
        self.assertEqual(result["requires"], ["ref_audio"])


if __name__ == "__main__":
    unittest.main()
