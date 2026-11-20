# -*- coding: utf-8 -*-
"""
【2026-11-20 任务：修正内置 Agent 与真实能力脱节】

覆盖两件事：

  A. **能力自述来自真实配置** —— 改真源，Agent 的陈述随之改变。
     （目标 B 的验收点：不再硬编码「544p/768p」「5~15 秒」「4~360」）

  B. **ffmpeg 媒体后处理工具的参数校验与真实产物生成**。
     （目标 C：抽取音轨 / 时长裁剪 / 响度归一 / 替换音轨）

运行：python3 -m pytest tests/test_agent_capability_truth.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from h3studio import agent as agent_mod
from h3studio import agent_capability as cap
from h3studio import agent_media_tools
from h3studio import agent_schema


FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/usr/bin/ffprobe"


# ==================================================================
# A. 能力自述来自真实配置
# ==================================================================


class TestCapabilityComesFromRealConfig(unittest.TestCase):
    """「改配置 -> 陈述随之改变」是本任务的核心验收点。"""

    def test_system_prompt_has_no_hardcoded_resolution_claim(self):
        """系统提示词**不得**再出现「视频分辨率只有 544p / 768p」这种陈述。"""
        prompt = agent_mod.build_system_prompt()
        self.assertNotIn("只有 544p", prompt)
        self.assertNotIn("只有 544p / 768p", prompt)
        self.assertNotIn("时长 5~15 秒", prompt)
        self.assertNotIn("4~360 秒", prompt)

    def test_system_prompt_states_real_video_duration_range(self):
        """视频时长必须说真话（2~60 秒，来自 video_models）。"""
        from h3studio import video_models as vm
        prompt = agent_mod.build_system_prompt()
        self.assertIn(f"{int(vm.VIDEO_DURATION_MIN)}~{int(vm.VIDEO_DURATION_MAX)}",
                      prompt)

    def test_system_prompt_states_real_music_duration(self):
        """音乐时长必须说真话（4~900 秒，默认 900）。"""
        from h3studio import music_yue2
        prompt = agent_mod.build_system_prompt()
        self.assertIn(f"{int(music_yue2.YUE2_MIN_DURATION)}~"
                      f"{int(music_yue2.YUE2_MAX_DURATION)}", prompt)
        self.assertNotIn("4~360", prompt)

    def test_system_prompt_mentions_480p_as_legal(self):
        """用户报出的 480p 必须在能力自述里被承认为合法档位。"""
        prompt = agent_mod.build_system_prompt()
        self.assertIn("480p", prompt)

    def test_capability_text_reflects_changed_config(self):
        """
        **核心契约**：改真源 -> 能力自述随之改变。

        做法：临时把视频时长上限改成 33 秒，看自述是否跟着变；
        改完必须还原（finally）。
        """
        from h3studio import video_models as vm
        original = vm.VIDEO_DURATION_MAX
        try:
            vm.VIDEO_DURATION_MAX = 33.0
            text = cap.video_capability_text()
            self.assertIn("2~33", text,
                          "改了真源，自述没跟着变 —— 能力自述不是派生的")
            self.assertNotIn("2~60", text)
        finally:
            vm.VIDEO_DURATION_MAX = original
        # 还原后必须恢复
        self.assertIn("2~60", cap.video_capability_text())

    def test_capability_text_reflects_changed_size_policy(self):
        """改面积上限 -> 自述里的 MP 数跟着变。"""
        from h3studio import video_models as vm
        original = vm.VIDEO_MAX_CANVAS_PIXELS
        try:
            vm.VIDEO_MAX_CANVAS_PIXELS = 1024 * 1024  # 1.0 MP
            text = cap.video_capability_text()
            self.assertIn("1.0 MP", text)
            self.assertNotIn("2.0 MP", text)
        finally:
            vm.VIDEO_MAX_CANVAS_PIXELS = original
        self.assertIn("2.0 MP", cap.video_capability_text())

    def test_video_resolution_forms_come_from_size_policy(self):
        """分辨率表达方式（档位 + 上限 + 对齐倍数）来自真实策略。"""
        forms = cap.video_resolution_forms("t2v")
        self.assertEqual(forms["max_pixels_mp"], 2.0)
        self.assertEqual(forms["multiple"], 32)
        self.assertTrue(forms["custom_width_height"])
        self.assertIn("480p", forms["tiers"])
        self.assertIn("768p", forms["tiers"])

    def test_video_tier_pixels_maps_to_real_dimensions(self):
        """480p 必须映射到真实的 0.4MP 像素（16:9 下 864x480）。"""
        self.assertEqual(cap.video_tier_pixels("480p"), (864, 480))
        self.assertIsNone(cap.video_tier_pixels("999p"))

    def test_list_modes_reports_real_video_resolutions(self):
        """list_modes 的分辨率说明不能再是「只有 544p/768p」。"""
        ctx = agent_mod.AgentContext(server_module=None, provider={})
        result = asyncio.run(agent_mod.TOOLS["list_modes"]["handler"](ctx, {}))
        note = result.get("video_resolution_note") or ""
        self.assertNotIn("只有 544p", note)
        self.assertIn("480p", note)
        self.assertIn("2.0 MP", note)

    def test_list_modes_reports_real_music_duration(self):
        ctx = agent_mod.AgentContext(server_module=None, provider={})
        result = asyncio.run(agent_mod.TOOLS["list_modes"]["handler"](ctx, {}))
        from h3studio import music_yue2
        self.assertEqual(result["music_duration_range"],
                         [int(music_yue2.YUE2_MIN_DURATION),
                          int(music_yue2.YUE2_MAX_DURATION)])
        self.assertEqual(result["music_duration_default"],
                         int(music_yue2.YUE2_DEFAULT_DURATION))

    def test_list_modes_result_fits_tool_result_limit(self):
        """list_modes 不能被截断（截断会让模型以为工具坏了）。"""
        ctx = agent_mod.AgentContext(server_module=None, provider={})
        result = asyncio.run(agent_mod.TOOLS["list_modes"]["handler"](ctx, {}))
        size = len(json.dumps(result, ensure_ascii=False))
        self.assertLessEqual(size, agent_mod.TOOL_RESULT_LIMIT,
                             f"list_modes 结果 {size} 字符，超出上限会被截断")

    def test_list_modes_still_reports_all_buildable_modes(self):
        from h3studio import workflows as wf
        ctx = agent_mod.AgentContext(server_module=None, provider={})
        result = asyncio.run(agent_mod.TOOLS["list_modes"]["handler"](ctx, {}))
        listed = [m["mode"] for m in result["modes"]]
        self.assertEqual(sorted(listed), sorted(wf.BUILDERS.keys()))

    def test_list_modes_still_reports_asset_slots(self):
        """槽位信息不能因为重构丢掉。"""
        ctx = agent_mod.AgentContext(server_module=None, provider={})
        result = asyncio.run(agent_mod.TOOLS["list_modes"]["handler"](ctx, {}))
        by_mode = {m["mode"]: m for m in result["modes"]}
        self.assertEqual(by_mode["a2a"].get("slots"), "ref_audio")
        self.assertIn("first_frame", by_mode["flf"].get("slots", ""))
        self.assertEqual(by_mode["i2i_qwen"].get("slots"), "ref_image")
        self.assertEqual(by_mode["t2a"].get("duration_field"), "max_duration")

    def test_list_modes_negative_prompt_effectiveness(self):
        ctx = agent_mod.AgentContext(server_module=None, provider={})
        result = asyncio.run(agent_mod.TOOLS["list_modes"]["handler"](ctx, {}))
        by_mode = {m["mode"]: m for m in result["modes"]}
        self.assertTrue(by_mode["t2i_moss"].get("negative_ok"))
        self.assertFalse(by_mode["t2i_qwen"].get("negative_ok"))
        self.assertFalse(by_mode["t2v"].get("negative_ok", False))


class TestVideoResolutionValidation(unittest.TestCase):
    """视频分辨率的**校验**也必须按真实能力（不能误杀 480p）。"""

    def test_480p_is_legal_for_video(self):
        issues = agent_schema.validate_params("t2v", {"resolution": "480p"})
        self.assertEqual([i.code for i in issues], [],
                         "480p 是真实合法档位，不该被拦")

    def test_768p_still_legal(self):
        issues = agent_schema.validate_params("t2v", {"resolution": "768p"})
        self.assertEqual([i.code for i in issues], [])

    def test_bogus_resolution_is_still_rejected(self):
        issues = agent_schema.validate_params("t2v", {"resolution": "not_a_tier"})
        codes = [i.code for i in issues]
        self.assertIn("invalid_resolution", codes)

    def test_mode_resolutions_for_video_is_real_tier_set(self):
        tiers = agent_schema.mode_resolutions("t2v")
        self.assertIn("480p", tiers)
        self.assertIn("768p", tiers)
        self.assertGreater(len(tiers), 2)

    def test_mode_schema_video_exposes_size_policy(self):
        schema = agent_schema.mode_schema("t2v")
        resolution = schema["params"]["resolution"]
        self.assertIn("480p", resolution["values"])
        self.assertIn("VIDEO_MODE_SIZE_POLICY", resolution["source"])
        self.assertIn("width", schema["params"])
        self.assertIn("height", schema["params"])

    def test_mode_schema_video_exposes_model_attention_accel(self):
        """底模 / 注意力 / 加速 LoRA 必须对模型可见（此前完全缺失）。"""
        from h3studio import video_models as vm
        schema = agent_schema.mode_schema("t2v")
        self.assertEqual(sorted(schema["params"]["model_key"]["values"]),
                         sorted(vm.VIDEO_MODELS.keys()))
        self.assertIn("kitchen", schema["params"]["attention"]["values"])
        self.assertNotIn("sol_scheduled", schema["params"]["attention"]["values"])
        self.assertIn("dmad_4step_critic",
                      schema["params"]["accel_lora"]["values"])

    def test_mode_schema_music_duration_is_real(self):
        from h3studio import music_yue2
        schema = agent_schema.mode_schema("t2a")
        duration = schema["params"]["max_duration"]
        self.assertEqual(duration["max"], int(music_yue2.YUE2_MAX_DURATION))
        self.assertIn("music_yue2", duration["source"])

    def test_mode_schema_batch_count_matches_server(self):
        schema = agent_schema.mode_schema("t2i")
        self.assertEqual(schema["params"]["batch_count"]["max"], 5)


class TestToolResultClippingKeepsJsonValid(unittest.TestCase):
    """
    目标 D 附带修：工具结果裁剪**不得破坏 JSON 结构**。

    旧实现是「头 + 尾」拼接 —— 实测 7 个案例里 5 个 json.loads 直接失败。
    模型收到残缺 JSON -> 认为「结果不完整」-> 重复调同一工具 ->
    撞重复检测 -> 整轮被截断。
    """

    def test_clip_keeps_json_valid(self):
        payload = {"a": "x" * 5000, "b": {"c": "y" * 3000},
                   "list": [{"k": "v"}] * 50}
        text = json.dumps(payload, ensure_ascii=False)
        clipped = agent_mod._clip(text, agent_mod.TOOL_RESULT_LIMIT)
        parsed = json.loads(clipped)          # 解析失败会直接抛异常
        self.assertEqual(set(parsed.keys()), {"a", "b", "list"})

    def test_clip_keeps_json_valid_for_many_shapes(self):
        shapes = [
            {"n": i, "long": "z" * 2000} for i in range(5)
        ]
        for payload in shapes:
            text = json.dumps(payload, ensure_ascii=False)
            clipped = agent_mod._clip(text, agent_mod.TOOL_RESULT_LIMIT)
            json.loads(clipped)

    def test_clip_short_text_unchanged(self):
        self.assertEqual(agent_mod._clip("hello", 100), "hello")

    def test_clip_non_json_falls_back_without_crashing(self):
        out = agent_mod._clip("x" * 5000, 1000)
        self.assertTrue(out)
        self.assertLessEqual(len(out), 1200)

    def test_clip_result_stays_within_limit_for_json(self):
        payload = {"a": "x" * 100000}
        text = json.dumps(payload, ensure_ascii=False)
        clipped = agent_mod._clip(text, agent_mod.TOOL_RESULT_LIMIT)
        self.assertLessEqual(len(clipped), agent_mod.TOOL_RESULT_LIMIT)


class TestMergeAvReportsAudioFacts(unittest.TestCase):
    """
    【2026-11-20 新增】合并工具必须**如实回报**音轨实际长度。

    【实测过的真实误述】Agent 合并「6 秒视频 + 2 秒音频」后在结论里说
    「2 秒之后是无声段」—— **说错了**：工具内部 apad 补了静音，
    音轨实际 5.99 秒。模型看不到 apad，只能按输入音频的 2 秒推断。

    修法：工具结果里回报音轨实际时长与是否补了静音，
    并让工具结果摘要带上它（模型看得到）。
    """

    def test_replace_audio_reports_padded_audio(self):
        import asyncio as _aio
        import subprocess as _sp
        import tempfile as _tf
        from h3studio import asset_repo
        from h3studio import asset_service
        from h3studio import assets as store

        tmp = Path(_tf.mkdtemp(prefix="agent-cap-report-"))
        try:
            video = tmp / "v6s.mp4"
            audio = tmp / "a2s.mp3"
            _sp.check_call([
                "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                "testsrc=size=320x240:rate=25:duration=6", "-c:v", "libx264",
                "-preset", "veryfast", "-pix_fmt", "yuv420p", "-an", str(video)],
                stderr=_sp.DEVNULL)
            _sp.check_call([
                "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=2", "-c:a", "libmp3lame",
                str(audio)], stderr=_sp.DEVNULL)
            with store.connect() as conn:
                vid = asset_service.register_asset(
                    conn, source_path=video, filename=video.name,
                    category="video", source="agent_cap_test",
                    creator="test")["asset"]["id"]
                aud = asset_service.register_asset(
                    conn, source_path=audio, filename=audio.name,
                    category="audio", source="agent_cap_test",
                    creator="test")["asset"]["id"]
            try:
                result = _aio.run(agent_media_tools.op_replace_audio(vid, aud))
                self.assertTrue(result["audio_padded"],
                                "音轨比视频短时必须标记已补静音")
                self.assertGreater(result["output_audio_duration_sec"],
                                   result["input_audio_duration_sec"] + 0.3)
                self.assertIn("补静音", result["note"])
                # 工具结果摘要里必须带上这条事实（模型看得到）
                summary = agent_mod._tool_result_summary(
                    "media_replace_audio", result, True)
                self.assertIn("补静音", summary)
            finally:
                for aid in (vid, aud):
                    try:
                        with store.connect() as conn:
                            asset_repo.purge(conn, aid)
                    except Exception:
                        pass
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestResolutionTierIsHonoured(unittest.TestCase):
    """
    【2026-11-20 新增】Agent 传的 resolution 档位必须**原样生效**。

    【原缺陷（实测复现）】
    ``server._merge_t2v_defaults`` 在缺 width/height 时只读
    ``defaults["megapixels"]``，**从不看用户传的 resolution**：
        传 resolution="480p" -> 实际 1376x768
        传 resolution="768p" -> 实际 1376x768
        什么都不传          -> 实际 1376x768
    三个结果完全一样 —— 用户明确要的档位被**静默丢弃**。
    这与用户报的原话（请求 480p 实际按 768p 生成）是同一个家族。
    """

    def _prepare(self, **params):
        import asyncio as _aio
        import server as srv
        base = {"prompt": "x", "aspect": "16:9", "duration": 5}
        base.update(params)
        return _aio.run(srv.prepare_agent_t2v_params(base, mode="t2v"))

    def test_480p_is_honoured(self):
        merged, err = self._prepare(resolution="480p")
        self.assertEqual(err, "")
        self.assertEqual((merged["width"], merged["height"]), (864, 480))
        self.assertEqual(merged["resolution"], "480p")

    def test_768p_is_honoured(self):
        merged, err = self._prepare(resolution="768p")
        self.assertEqual(err, "")
        self.assertEqual((merged["width"], merged["height"]), (1344, 768))

    def test_1088p_is_honoured(self):
        merged, err = self._prepare(resolution="1088p")
        self.assertEqual(err, "")
        self.assertEqual((merged["width"], merged["height"]), (1920, 1088))

    def test_portrait_480p_is_honoured(self):
        merged, err = self._prepare(resolution="480p", aspect="9:16")
        self.assertEqual(err, "")
        self.assertEqual((merged["width"], merged["height"]), (480, 864))

    def test_tier_absent_for_aspect_is_rejected_not_silently_changed(self):
        """该比例下没有这个档位 -> **明确报错**，不静默变成别的档位。"""
        merged, err = self._prepare(resolution="768p", aspect="4:3")
        self.assertTrue(err, "4:3 下没有 768p，必须报错而不是静默改写")
        self.assertIn("不存在", err)
        # 错误信息里要给出该比例下的真实档位集（模型能一次改对）
        self.assertIn("4:3", err)
        self.assertIn("p", err)

    def test_panel_path_unaffected(self):
        """面板路径（总是提交 width/height）不受影响。"""
        import asyncio as _aio
        import server as srv
        merged, err = srv.normalize_t2v_params(
            {"prompt": "x", "aspect": "16:9", "duration": 5,
             "width": 864, "height": 480}, mode="t2v")
        self.assertEqual(err, "")
        self.assertEqual((merged["width"], merged["height"]), (864, 480))


class TestMergeAvPadsShortAudio(unittest.TestCase):
    """【2026-11-20 新增】音轨短于视频时要补静音（否则尾部画面没声音）。"""

    def test_short_audio_is_padded_to_video_length(self):
        import asyncio as _aio
        import json as _json
        import subprocess as _sp
        import tempfile as _tf
        from h3studio import asset_repo
        from h3studio import asset_service
        from h3studio import assets as store

        tmp = Path(_tf.mkdtemp(prefix="agent-cap-apad-"))
        try:
            video = tmp / "v4s.mp4"
            audio = tmp / "a2s.mp3"
            # 视频 4 秒（**不带音频**，避免 -shortest 把视频也截短）
            _sp.check_call([
                "ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=4",
                "-c:v", "libx264", "-preset", "veryfast",
                "-pix_fmt", "yuv420p", "-an", str(video)],
                stderr=_sp.DEVNULL)
            _sp.check_call([
                "ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", "sine=frequency=660:duration=2",
                "-c:a", "libmp3lame", str(audio)], stderr=_sp.DEVNULL)
            with store.connect() as conn:
                vid = asset_service.register_asset(
                    conn, source_path=video, filename=video.name,
                    category="video", source="agent_cap_test",
                    creator="test")["asset"]["id"]
                aud = asset_service.register_asset(
                    conn, source_path=audio, filename=audio.name,
                    category="audio", source="agent_cap_test",
                    creator="test")["asset"]["id"]
            try:
                result = _aio.run(agent_media_tools.op_merge_av(vid, aud))
                out_id = result["output"]["asset_id"]
                with store.connect() as conn:
                    version = asset_repo.current_version(conn, out_id)
                path = store.resolve_inside(store.library_root(),
                                           version["storage_path"])
                probe = _json.loads(_sp.check_output([
                    "ffprobe", "-v", "error", "-print_format", "json",
                    "-show_streams", str(path)]))
                streams = {s["codec_type"]: s for s in probe["streams"]}
                self.assertIn("audio", streams)
                audio_dur = float(streams["audio"].get("duration") or 0)
                video_dur = float(streams["video"].get("duration") or 0)
                # 音轨必须被补到接近视频长（原来是 2 秒，视频 4 秒）
                self.assertGreater(audio_dur, video_dur - 0.3,
                                   f"音轨没补静音：audio={audio_dur} video={video_dur}")
            finally:
                for aid in (vid, aud):
                    try:
                        with store.connect() as conn:
                            asset_repo.purge(conn, aid)
                    except Exception:
                        pass
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestClarificationDoesNotStopExecution(unittest.TestCase):
    """
    目标 D：**澄清式 respond 不收尾**。

    旧实现 respond 无条件 break -> 「先问再做」成为唯一路径，
    用户必须中途催促。现在澄清式 respond（needs_clarification）
    只是提问，编排层继续执行剩余工具调用。
    """

    def test_clarification_respond_is_detected(self):
        self.assertTrue(agent_mod._is_clarification_respond(
            {"ok": True, "needs_clarification": True}))
        self.assertFalse(agent_mod._is_clarification_respond(
            {"ok": True, "needs_clarification": False}))
        self.assertFalse(agent_mod._is_clarification_respond({"ok": True}))
        self.assertFalse(agent_mod._is_clarification_respond(None))

    def test_respond_tool_returns_needs_clarification_flag(self):
        ctx = agent_mod.AgentContext(server_module=None, provider={})
        result = asyncio.run(agent_mod.TOOLS["respond"]["handler"](
            ctx, {"summary": "要替换原音轨吗？", "needs_clarification": True}))
        self.assertTrue(result.get("needs_clarification"),
                        "respond 必须把 needs_clarification 带进工具结果，"
                        "编排层据此判断「这是提问不是结论」")


class TestMultistepRequestCompletesInOneTurn(unittest.TestCase):
    """
    目标 D 端到端：**多步请求在单次对话内自动完成**。

    模拟用户的真实诉求：「把某个既有视频与某个既有音频合并成带背景音乐的成片」
    —— 链路是 search_assets -> merge -> respond。

    关键断言：
      1. 不追问（不调 needs_clarification 的 respond）；
      2. 工具链完整走完（search -> merge -> respond）；
      3. 产物路径被回报给用户。
    """

    class _Server:
        """最小服务端替身（run_agent 需要 db() 取默认 Provider）。"""

        def __init__(self, tmp: Path):
            import sqlite3
            self.db_path = tmp / "jobs.db"
            conn = sqlite3.connect(self.db_path)
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS model_providers ("
                "id TEXT PRIMARY KEY, name TEXT, type TEXT, base_url TEXT,"
                "llm_base_url TEXT DEFAULT '', use_proxy INTEGER DEFAULT 0,"
                "proxy_base_url TEXT DEFAULT '', model TEXT, api_key TEXT"
                " DEFAULT '', multimodal INTEGER DEFAULT 0,"
                "enabled INTEGER DEFAULT 1, created_at REAL);"
                "CREATE TABLE IF NOT EXISTS model_provider_prefs ("
                "key TEXT PRIMARY KEY, value TEXT);")
            conn.execute(
                "INSERT OR REPLACE INTO model_providers VALUES "
                "('p','测试','openai','http://127.0.0.1:5000','',0,'','m','',1,1,0)")
            conn.execute("INSERT OR REPLACE INTO model_provider_prefs "
                         "VALUES ('default','p')")
            conn.commit()
            conn.close()

        def db(self):
            import sqlite3
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return conn

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-cap-e2e-"))
        self.server = self._Server(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_clarification_does_not_stop_the_chain(self):
        """
        模型先澄清式 respond -> 编排层**不收尾** -> 模型继续把活做完。
        """
        calls = {"n": 0}
        tools_used: list[str] = []

        async def fake(provider, messages, tools, options=None, images=None,
                      on_token=None):
            index = calls["n"]
            calls["n"] += 1
            if index == 0:
                # 第 1 轮：先澄清式提问
                return {"content": None, "tool_calls": [{
                    "id": "c0", "name": "respond",
                    "arguments": {"summary": "要不要替换原音轨？",
                                  "needs_clarification": True}}],
                    "raw": {}}
            if index == 1:
                # 第 2 轮：收到 nudge 后继续干活
                return {"content": None, "tool_calls": [{
                    "id": "c1", "name": "search_assets",
                    "arguments": {"query": "视频"}}], "raw": {}}
            if index == 2:
                return {"content": None, "tool_calls": [{
                    "id": "c2", "name": "media_merge_av",
                    "arguments": {"video_asset_id": "asset_v",
                                  "audio_asset_id": "asset_a"}}],
                    "raw": {}}
            # 第 4 轮：给结论
            return {"content": "已合并完成，产物在素材库。", "tool_calls": [],
                    "raw": {}}

        original_call = agent_mod.llm_client.call_provider_with_tools
        original_stream = agent_mod.llm_client.call_provider_with_tools_stream
        original_search = agent_mod.TOOLS["search_assets"]["handler"]
        original_merge = agent_mod.TOOLS["media_merge_av"]["handler"]

        async def fake_search(ctx, args):
            tools_used.append("search_assets")
            return {"total": 1, "assets": [
                {"asset_id": "asset_v", "name": "v", "category": "video",
                 "description": "一段视频", "analyzed": True}]}

        async def fake_merge(ctx, args):
            tools_used.append("media_merge_av")
            return {"operation": "merge_av",
                    "video_input": args.get("video_asset_id"),
                    "audio_input": args.get("audio_asset_id"),
                    "output": {"asset_id": "asset_out", "name": "成片.mp4",
                               "category": "video",
                               "content_url": "/api/assets/asset_out/content"},
                    "duration_sec": 5.0}

        agent_mod.llm_client.call_provider_with_tools = fake
        agent_mod.llm_client.call_provider_with_tools_stream = fake
        agent_mod.TOOLS["search_assets"]["handler"] = fake_search
        agent_mod.TOOLS["media_merge_av"]["handler"] = fake_merge
        try:
            result = asyncio.run(agent_mod.run_agent(
                self.server, "把那个视频和那个音频合并成带背景音乐的成片"))
        finally:
            agent_mod.llm_client.call_provider_with_tools = original_call
            agent_mod.llm_client.call_provider_with_tools_stream = original_stream
            agent_mod.TOOLS["search_assets"]["handler"] = original_search
            agent_mod.TOOLS["media_merge_av"]["handler"] = original_merge

        self.assertEqual(result["status"], "completed",
                         f"多步请求没跑完：{result['status']} / "
                         f"{result.get('error')}")
        self.assertIn("search_assets", tools_used)
        self.assertIn("media_merge_av", tools_used,
                      "澄清之后没有继续把活做完 —— 这正是用户报的缺陷")
        self.assertIn("已合并完成", result.get("answer") or "")

    def test_no_progress_detection_stops_spinning(self):
        """模型换着关键词空转 -> 无进展检测必须收尾（不是烧光 50 步）。"""
        calls = {"n": 0}

        async def fake(provider, messages, tools, options=None, images=None,
                      on_token=None):
            index = calls["n"]
            calls["n"] += 1
            return {"content": None, "tool_calls": [{
                "id": f"c{index}", "name": "search_assets",
                "arguments": {"query": f"关键词{index}"}}], "raw": {}}

        original_call = agent_mod.llm_client.call_provider_with_tools
        original_stream = agent_mod.llm_client.call_provider_with_tools_stream
        original_search = agent_mod.TOOLS["search_assets"]["handler"]

        async def fake_search(ctx, args):
            # 每次都返回**同一个**素材 -> 事实指纹不变
            return {"total": 1, "assets": [
                {"asset_id": "asset_same", "name": "同一个",
                 "category": "image", "description": "d", "analyzed": True}]}

        agent_mod.llm_client.call_provider_with_tools = fake
        agent_mod.llm_client.call_provider_with_tools_stream = fake
        agent_mod.TOOLS["search_assets"]["handler"] = fake_search
        try:
            result = asyncio.run(agent_mod.run_agent(
                self.server, "找一个不存在的素材", total_timeout=120))
        finally:
            agent_mod.llm_client.call_provider_with_tools = original_call
            agent_mod.llm_client.call_provider_with_tools_stream = original_stream
            agent_mod.TOOLS["search_assets"]["handler"] = original_search

        self.assertEqual(result["status"], "truncated")
        self.assertIn("没有获得任何新信息", result.get("error") or "")
        # 步数必须远小于 50（否则就是烧光预算）
        self.assertLess(result["usage"]["steps"],
                        agent_mod.MAX_STEPS,
                        "无进展检测没生效 —— 步数被烧光了")


class TestSystemPromptOrchestrationGuidance(unittest.TestCase):
    """目标 D：提示词不能再教模型「先反问」与「预算只有 8 次」。"""

    def test_prompt_has_no_stale_tool_budget(self):
        prompt = agent_mod.build_system_prompt()
        self.assertNotIn("工具调用预算是 8 次", prompt)
        self.assertIn(f"工具调用预算是 {agent_mod.MAX_TOOL_CALLS} 次", prompt)

    def test_prompt_tells_model_to_finish_multistep_requests(self):
        prompt = agent_mod.build_system_prompt()
        self.assertIn("多步请求", prompt)
        self.assertIn("一次做完", prompt)

    def test_prompt_limits_clarification_to_real_blockers(self):
        """澄清只允许在「真的缺输入」时用，不能泛化成「参数没给就问」。"""
        prompt = agent_mod.build_system_prompt()
        self.assertIn("不要问", prompt)
        self.assertIn("一律用工作台默认值", prompt)


# ==================================================================
# B. ffmpeg 媒体后处理工具（真实产物）
# ==================================================================


def _make_media(directory: Path) -> dict:
    """用**真实 ffmpeg** 造两段视频 + 两段音频。"""
    v1 = directory / "v1.mp4"
    v2 = directory / "v2.mp4"
    a1 = directory / "a1.mp3"
    a2 = directory / "a2.mp3"
    subprocess.check_call([
        FFMPEG, "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(v1)], stderr=subprocess.DEVNULL)
    # v2：**无音轨**的纯视频（用来验证「没有音轨」这条分支）。
    # 【为什么单独造而不是复用 v1】v1 是有音轨的 —— 拿它测
    # 「应该报 not_an_audio」会**测不到目标分支**（前提就是错的）。
    subprocess.check_call([
        FFMPEG, "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=3",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-an", str(v2)], stderr=subprocess.DEVNULL)
    subprocess.check_call([
        FFMPEG, "-y", "-v", "error",
        "-f", "lavfi", "-i", "sine=frequency=660:duration=2",
        "-c:a", "libmp3lame", str(a1)], stderr=subprocess.DEVNULL)
    subprocess.check_call([
        FFMPEG, "-y", "-v", "error",
        "-f", "lavfi", "-i", "sine=frequency=220:duration=5",
        "-c:a", "libmp3lame", str(a2)], stderr=subprocess.DEVNULL)
    return {"v1": v1, "v2": v2, "a1": a1, "a2": a2}


@unittest.skipUnless(FFMPEG and FFPROBE, "本机没有 ffmpeg/ffprobe")
class TestNewMediaToolsReal(unittest.TestCase):
    """目标 C：新工具必须**真的产出文件**，且产物进素材库。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="agent-cap-media-"))
        cls.media = _make_media(cls.tmp)
        from h3studio import asset_service
        from h3studio import assets as asset_store
        cls.assets = {}
        #: 本类测试创建的全部素材 id（含**产物**）—— tearDownClass 要清干净。
        #:
        #: 【为什么必须清（2026-11-20 实测踩到的坑）】
        #: 素材库是**共享**的（线上服务同一个库）。测试往里塞十几个
        #: 产物后，其它测试（比如 test_agent_paste_browser 断言
        #: 「素材库里有 clipboard_pasted_xxx」）会因为**列表前 20 条
        #: 被测试素材占满**而失败 —— 那不是它的 bug，是**我们污染了现场**。
        #: 所以：记录所有创建的 id，tearDownClass 里逐个 purge。
        cls.created: list[str] = []
        for key, path in cls.media.items():
            with asset_store.connect() as conn:
                result = asset_service.register_asset(
                    conn, source_path=path, filename=path.name,
                    category="audio" if key.startswith("a") else "video",
                    source="agent_cap_test", creator="test")
            cls.assets[key] = result["asset"]["id"]
            cls.created.append(result["asset"]["id"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        cls._purge_created()

    @classmethod
    def _purge_created(cls):
        """把本类测试创建的全部素材（含产物）从共享素材库里清掉。"""
        from h3studio import asset_repo
        from h3studio import assets as asset_store
        for asset_id in list(getattr(cls, "created", [])):
            try:
                with asset_store.connect() as conn:
                    asset_repo.purge(conn, asset_id)
            except Exception:
                # 素材被引用（check_references）就 purge 不掉 ——
                # 那时只能标记删除，不能硬删。静默继续（清理是尽力而为）。
                try:
                    with asset_store.connect() as conn:
                        conn.execute(
                            "UPDATE assets SET deleted_at=datetime('now') "
                            "WHERE id=?", (asset_id,))
                        conn.commit()
                except Exception:
                    pass

    def _track(self, asset_id: str) -> str:
        """登记一个新产物 id（供 tearDownClass 清理）。"""
        self.created.append(asset_id)
        return asset_id

    def _assert_in_library(self, asset_id: str) -> Path:
        """产物必须真的在素材库里（能解析到真实文件）。"""
        from h3studio import asset_repo
        from h3studio import assets as asset_store
        with asset_store.connect() as conn:
            version = asset_repo.current_version(conn, asset_id)
        self.assertIsNotNone(version, f"产物没进素材库：{asset_id}")
        path = asset_store.resolve_inside(asset_store.library_root(),
                                          version["storage_path"])
        self.assertIsNotNone(path, f"产物路径解析失败：{asset_id}")
        assert path is not None
        self.assertTrue(path.is_file(), f"产物文件不存在：{path}")
        self.assertGreater(path.stat().st_size, 0, "产物是空文件")
        return path

    # -------------------------------------------------- 抽取音轨

    def test_extract_audio_produces_real_audio_asset(self):
        result = asyncio.run(agent_media_tools.op_extract_audio(
            self.assets["v1"], "m4a"))
        output = result["output"]
        self.assertTrue(output["asset_id"])
        self._track(output["asset_id"])
        self.assertEqual(output["category"], "audio")
        path = self._assert_in_library(output["asset_id"])
        self.assertTrue(path.name.endswith(".m4a"), path.name)
        # 产物体积必须合理（真的解出了音频，不是空壳）
        self.assertGreater(path.stat().st_size, 1000)

    def test_extract_audio_rejects_asset_without_audio(self):
        """纯视频素材没有音轨 -> 明确报错，不是静默产出空文件。"""
        with self.assertRaises(agent_media_tools.MediaToolError) as caught:
            asyncio.run(agent_media_tools.op_extract_audio(self.assets["v2"]))
        self.assertEqual(caught.exception.code, "not_an_audio")

    def test_video_asset_really_has_no_audio(self):
        """守住上面那条测试的前提（v2 必须真的没有音轨）。"""
        result = asyncio.run(agent_media_tools.op_probe(self.assets["v2"]))
        self.assertTrue(result["has_video"])
        self.assertFalse(result["has_audio"])

    def test_extract_audio_rejects_unknown_format(self):
        with self.assertRaises(agent_media_tools.MediaToolError) as caught:
            asyncio.run(agent_media_tools.op_extract_audio(
                self.assets["v1"], "exe"))
        self.assertIn("不支持", caught.exception.message)

    # -------------------------------------------------- 时长裁剪

    def test_trim_produces_shorter_clip(self):
        result = asyncio.run(agent_media_tools.op_trim(
            self.assets["v1"], start_sec=1.0, duration_sec=2.0))
        output = result["output"]
        self._track(output["asset_id"])
        path = self._assert_in_library(output["asset_id"])
        self.assertGreater(path.stat().st_size, 0)
        # 4 秒素材从第 1 秒截 2 秒 -> 产物约 2 秒
        self.assertAlmostEqual(result["duration_sec"], 2.0, delta=0.6)
        self.assertFalse(result["clipped_to_source"])

    def test_trim_clips_to_source_length(self):
        """请求时长超出素材长度 -> 截到素材末尾，并**标记** clipped。"""
        result = asyncio.run(agent_media_tools.op_trim(
            self.assets["v1"], start_sec=1.0, duration_sec=100.0))
        self._track(result["output"]["asset_id"])
        self.assertTrue(result["clipped_to_source"])
        self.assertAlmostEqual(result["duration_sec"], 3.0, delta=0.6)

    def test_trim_rejects_out_of_range_start(self):
        with self.assertRaises(agent_media_tools.MediaToolError) as caught:
            asyncio.run(agent_media_tools.op_trim(
                self.assets["v1"], start_sec=999.0))
        self.assertEqual(caught.exception.code, "trim_out_of_range")

    def test_trim_rejects_non_positive_duration(self):
        with self.assertRaises(agent_media_tools.MediaToolError) as caught:
            asyncio.run(agent_media_tools.op_trim(
                self.assets["v1"], start_sec=0.0, duration_sec=0.0))
        self.assertIn("大于 0", caught.exception.message)

    # -------------------------------------------------- 响度归一

    def test_normalize_audio_produces_real_output(self):
        result = asyncio.run(agent_media_tools.op_normalize_audio(
            self.assets["v1"], target_lufs=-14.0))
        output = result["output"]
        self._track(output["asset_id"])
        path = self._assert_in_library(output["asset_id"])
        self.assertGreater(path.stat().st_size, 1000)
        self.assertAlmostEqual(result["duration_sec"], 4.0, delta=0.6)

    def test_normalize_audio_rejects_out_of_range_lufs(self):
        with self.assertRaises(agent_media_tools.MediaToolError) as caught:
            asyncio.run(agent_media_tools.op_normalize_audio(
                self.assets["v1"], target_lufs=10.0))
        self.assertEqual(caught.exception.code, "invalid_lufs")

    def test_normalize_audio_rejects_asset_without_audio(self):
        with self.assertRaises(agent_media_tools.MediaToolError) as caught:
            asyncio.run(agent_media_tools.op_normalize_audio(
                self.assets["v2"]))
        self.assertEqual(caught.exception.code, "not_an_audio")

    # -------------------------------------------------- 替换音轨

    def test_replace_audio_drops_original_track(self):
        """
        **替换**与**混合**必须区分：替换后原声应消失。

        判据：产物里只剩一路音频流（ffprobe 的 audio stream 数 == 1），
        且混音（media_mix_audio）会有两路输入混成一路 —— 这里断言
        「替换后音频流仍然只有一路且音轨非空」。
        """
        result = asyncio.run(agent_media_tools.op_replace_audio(
            self.assets["v1"], self.assets["a1"]))
        output = result["output"]
        self._track(output["asset_id"])
        path = self._assert_in_library(output["asset_id"])
        self.assertGreater(path.stat().st_size, 1000)
        # 视频 4 秒 / 音频 2 秒 -> apad 补静音后 -shortest 截到视频长度
        self.assertAlmostEqual(result["duration_sec"], 4.0, delta=0.8)
        probe = json.loads(subprocess.check_output([
            FFPROBE, "-v", "error", "-print_format", "json",
            "-show_streams", str(path)]))
        audio_streams = [s for s in probe.get("streams", [])
                         if s.get("codec_type") == "audio"]
        self.assertEqual(len(audio_streams), 1,
                         f"替换后应只有一路音频，实得 {len(audio_streams)}")

    def test_replace_audio_keep_original_merges_two_tracks(self):
        """keep_original=True -> 原声与新音轨**并排**（amerge，两路）。"""
        result = asyncio.run(agent_media_tools.op_replace_audio(
            self.assets["v1"], self.assets["a1"], keep_original=True))
        output = result["output"]
        self._track(output["asset_id"])
        path = self._assert_in_library(output["asset_id"])
        probe = json.loads(subprocess.check_output([
            FFPROBE, "-v", "error", "-print_format", "json",
            "-show_streams", str(path)]))
        audio_streams = [s for s in probe.get("streams", [])
                         if s.get("codec_type") == "audio"]
        self.assertEqual(len(audio_streams), 1,
                         "amerge 后是**一路**双声道（两路并排混在一轨里）")
        # 双声道 = 原声 + 新音轨各占一个声道
        self.assertEqual(int(audio_streams[0].get("channels") or 0), 2)

    def test_replace_audio_rejects_non_audio_input(self):
        """给一段**没有音轨**的视频当音轨 -> 明确报错。"""
        with self.assertRaises(agent_media_tools.MediaToolError) as caught:
            asyncio.run(agent_media_tools.op_replace_audio(
                self.assets["v1"], self.assets["v2"]))
        self.assertEqual(caught.exception.code, "not_an_audio")

    def test_replace_audio_rejects_unknown_asset(self):
        with self.assertRaises(agent_media_tools.MediaToolError) as caught:
            asyncio.run(agent_media_tools.op_replace_audio(
                self.assets["v1"], "asset_does_not_exist"))
        self.assertEqual(caught.exception.code, "asset_not_found")


class TestNewMediaToolsRegistered(unittest.TestCase):
    """新工具必须真的注册给模型（且挂在 media_tools 能力下）。"""

    def test_all_four_new_tools_exist(self):
        for name in ("media_extract_audio", "media_trim",
                     "media_normalize_audio", "media_replace_audio"):
            self.assertIn(name, agent_mod.TOOLS, f"{name} 没注册")
            self.assertIn("ffmpeg", agent_mod.TOOLS[name]["description"],
                          f"{name} 的描述没说明是真实 ffmpeg")

    def test_new_tools_are_gated_by_media_tools_capability(self):
        from h3studio import agent_extra_tools
        for name in ("media_extract_audio", "media_trim",
                     "media_normalize_audio", "media_replace_audio"):
            self.assertEqual(agent_extra_tools.TOOL_CAPABILITIES.get(name),
                             "media_tools")

    def test_new_tools_hidden_when_capability_disabled(self):
        from h3studio import agent_extra_tools
        specs = agent_mod._tool_specs({"media_tools": False})
        names = {s["function"]["name"] for s in specs}
        for name in ("media_extract_audio", "media_trim",
                     "media_normalize_audio", "media_replace_audio"):
            self.assertNotIn(name, names)

    def test_new_tools_present_when_capability_enabled(self):
        from h3studio import agent_extra_tools
        specs = agent_mod._tool_specs({"media_tools": True})
        names = {s["function"]["name"] for s in specs}
        for name in ("media_extract_audio", "media_trim",
                     "media_normalize_audio", "media_replace_audio"):
            self.assertIn(name, names)


if __name__ == "__main__":
    unittest.main()
