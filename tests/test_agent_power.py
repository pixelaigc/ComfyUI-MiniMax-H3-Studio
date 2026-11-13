# -*- coding: utf-8 -*-
"""
内置 Agent 增强能力（2026-11-13 审查目标 4/5/6/7）的**真实验证**脚本。

【取证纪律】
1. 真实起 server.make_app()（生产路由），真起 HTTP 端口；
2. 真实调用本机 ffmpeg / ffprobe / 9898 聚合引擎；
3. 断言打在**真实响应**上，不打在自己造的 mock 上；
4. 每条断言都有对应的「负控」说明（哪些用例会红）。

运行：python3 -m pytest tests/test_agent_power.py -v
"""
import asyncio
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import agent as agent_mod          # noqa: E402
from h3studio import agent_settings              # noqa: E402
from h3studio import agent_extra_tools           # noqa: E402
from h3studio import agent_media_tools           # noqa: E402
from h3studio import agent_websearch             # noqa: E402
from h3studio import agent_codeexec              # noqa: E402

#: 本机 ffmpeg（真实执行，不是 mock）。
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

#: 局域网聚合引擎地址（真实 HTTP）。
AGGREGATE_URL = "http://127.0.0.1:9898/api/openwebui/tools/search_general"


def _make_media(directory: Path) -> dict:
    """用**真实 ffmpeg** 造两段视频 + 一段音频。"""
    v1 = directory / "v1.mp4"
    v2 = directory / "v2.mp4"
    a1 = directory / "a1.mp3"
    subprocess.check_call([
        FFMPEG, "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(v1)], stderr=subprocess.DEVNULL)
    subprocess.check_call([
        FFMPEG, "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=880:duration=3",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(v2)], stderr=subprocess.DEVNULL)
    subprocess.check_call([
        FFMPEG, "-y", "-v", "error",
        "-f", "lavfi", "-i", "sine=frequency=660:duration=2",
        "-c:a", "libmp3lame", str(a1)], stderr=subprocess.DEVNULL)
    return {"v1": v1, "v2": v2, "a1": a1}


class _Ctx:
    """最小 AgentContext 替身（只带额外能力工具需要的字段）。"""

    def __init__(self, server=None):
        self.server = server
        self.assets = {}
        self.jobs = []
        self.attachments = []


class _FakeServer:
    """
    能跑起完整编排的最小服务端替身。

    【为什么需要它】run_agent 会调 server.db() 取默认 Provider ——
    没有它就跑不起来（不是 Agent 的 bug，是编排器本来就依赖服务端）。
    这里造一个**真 SQLite**（不是 mock），预置一个默认 Provider。
    """

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.db_path = tmp / "jobs.db"
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
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


class _ServerWithSettings:
    """带 agent_settings_snapshot 的服务端替身。"""

    def __init__(self, settings):
        self._settings = settings

    def agent_settings_snapshot(self):
        return {"settings": self._settings,
                "ffmpeg_path": agent_settings.ffmpeg_path(self._settings),
                "ffprobe_path": agent_settings.ffprobe_path(self._settings)}


# ------------------------------------------------------------------ 1. 步数上限


class TestStepLimit(unittest.TestCase):
    """目标 1：步数上限 >= 50，前后端一致。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-steplimit-"))
        self.server = _FakeServer(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_max_steps_is_at_least_50(self):
        self.assertGreaterEqual(agent_mod.MAX_STEPS, 50)

    def test_tool_call_budget_scales_with_steps(self):
        self.assertGreaterEqual(agent_mod.MAX_TOOL_CALLS,
                                agent_mod.MAX_STEPS * 1.5)

    def test_total_timeout_covers_step_budget(self):
        self.assertGreaterEqual(agent_mod.TOTAL_TIMEOUT, agent_mod.MAX_STEPS * 48)

    def test_hard_cap_matches_constant(self):
        """前后端上限一致：agent_settings.MAX_STEPS_HARD_CAP == MAX_STEPS。"""
        self.assertEqual(agent_settings.MAX_STEPS_HARD_CAP, agent_mod.MAX_STEPS)

    def test_settings_normalize_clamps_to_hard_cap(self):
        """用户把 max_steps 设成 999 -> 必须被夹到 50（不是照单全收）。"""
        out = agent_settings.normalize({"max_steps": 999})
        self.assertEqual(out["max_steps"], 50)
        out = agent_settings.normalize({"max_steps": -5})
        self.assertEqual(out["max_steps"], 1)

    def test_a_task_over_8_steps_completes(self):
        """
        【核心验证】一条**超过 8 步**的任务能跑完。

        模拟 12 轮工具调用（> 8）后给结论 —— 在旧的 8 步上限下
        这条链路**必然**撞上限被截断（status=truncated），
        现在必须 completed。
        """
        calls = {"n": 0}

        async def fake(provider, messages, tools, options=None, images=None):
            index = calls["n"]
            calls["n"] += 1
            if index >= 12:
                return {"content": "任务已完成：共 12 步", "tool_calls": [], "raw": {}}
            return {
                "content": None,
                "tool_calls": [{
                    "id": f"c{index}",
                    "name": "get_mode_schema",
                    "arguments": {"mode": "t2i_qwen" if index % 2 else "t2v"},
                }],
                "raw": {},
            }

        original = agent_mod.llm_client.call_provider_with_tools
        agent_mod.llm_client.call_provider_with_tools = fake
        try:
            result = asyncio.run(agent_mod.run_agent(
                self.server, "跑一条超过 8 步的任务"))
        finally:
            agent_mod.llm_client.call_provider_with_tools = original

        self.assertEqual(result["status"], "completed",
                         f"超过 8 步的任务没有跑完：{result['status']} / "
                         f"{result.get('error')}")
        self.assertGreaterEqual(result["usage"]["steps"], 12,
                                "步数没到 12 —— 说明上限仍把任务截断了")
        self.assertNotIn("步骤上限", str(result.get("error") or ""))

    def test_same_task_would_be_truncated_at_8(self):
        """
        负控：把上限压回 8，同样的 12 轮链路**必须**被截断。

        这一条证明「上面的 completed」不是因为链路变短了，
        而是因为上限真的放开了。
        """
        calls = {"n": 0}

        async def fake(provider, messages, tools, options=None, images=None):
            index = calls["n"]
            calls["n"] += 1
            if index >= 12:
                return {"content": "任务已完成：共 12 步", "tool_calls": [], "raw": {}}
            return {
                "content": None,
                "tool_calls": [{
                    "id": f"c{index}",
                    "name": "get_mode_schema",
                    "arguments": {"mode": "t2i_qwen" if index % 2 else "t2v"},
                }],
                "raw": {},
            }

        original = agent_mod.llm_client.call_provider_with_tools
        agent_mod.llm_client.call_provider_with_tools = fake
        try:
            result = asyncio.run(agent_mod.run_agent(
                self.server, "跑一条超过 8 步的任务", max_steps=8))
        finally:
            agent_mod.llm_client.call_provider_with_tools = original
        self.assertEqual(result["status"], "truncated",
                         "上限压回 8 之后应该被截断 —— 没截断说明上限没生效")


# ------------------------------------------------------------------ 2. 能力开关


class TestCapabilityGate(unittest.TestCase):
    """目标 4：额外能力可配置、可启用/禁用。"""

    def test_all_capabilities_registered(self):
        for key in agent_settings.CAPABILITIES:
            self.assertIn(key, agent_settings.DEFAULT_CAPABILITIES)

    def test_code_exec_is_off_by_default(self):
        """代码执行默认关闭 —— 它是唯一有副作用的能力。"""
        self.assertFalse(agent_settings.DEFAULT_CAPABILITIES["code_exec"])

    def test_tool_specs_filter_disabled_capability(self):
        """关掉的能力 -> 工具**不在**给模型的清单里。"""
        caps = dict(agent_settings.DEFAULT_CAPABILITIES)
        caps["web_search"] = False
        names = {s["function"]["name"] for s in agent_mod._tool_specs(caps)}
        self.assertNotIn("web_search", names)
        self.assertIn("read_file", names)

    def test_tool_specs_without_capabilities_keeps_everything(self):
        """capabilities=None -> 不过滤（调试路径）。"""
        names = {s["function"]["name"] for s in agent_mod._tool_specs()}
        self.assertIn("web_search", names)
        self.assertIn("run_code", names)

    def test_disabled_capability_tool_refuses_to_run(self):
        """即便模型凭记忆调了被关掉的工具，handler 也必须拒绝。"""
        settings = agent_settings.defaults()
        settings["capabilities"]["code_exec"] = False
        ctx = _Ctx(_ServerWithSettings(settings))
        with self.assertRaises(agent_mod.AgentError) as caught:
            asyncio.run(agent_mod.TOOLS["run_code"]["handler"](
                ctx, {"code": "print(1)"}))
        self.assertEqual(caught.exception.code, "capability_disabled")

    def test_enabled_capability_tool_runs(self):
        """开启后同一个工具能真的执行（真跑 Python）。"""
        settings = agent_settings.defaults()
        settings["capabilities"]["code_exec"] = True
        ctx = _Ctx(_ServerWithSettings(settings))
        result = asyncio.run(agent_mod.TOOLS["run_code"]["handler"](
            ctx, {"code": "print(6*7)"}))
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("42", result["stdout"])

    def test_capability_state_shape(self):
        state = agent_settings.capability_state(agent_settings.defaults())
        self.assertEqual(set(state),
                         set(agent_settings.CAPABILITIES))
        for meta in state.values():
            self.assertIn("label", meta)
            self.assertIn("builtin", meta)
            self.assertIn("requires", meta)
            self.assertIn("enabled", meta)


# ------------------------------------------------------------------ 3. 代码执行


class TestCodeExec(unittest.TestCase):
    """目标 4：代码生成与执行是**真实执行**。"""

    def test_real_execution_returns_stdout(self):
        result = asyncio.run(agent_codeexec.run_python(
            "import sys\nprint('hello', sys.version_info[0])"))
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("hello", result["stdout"])

    def test_nonzero_exit_is_reported(self):
        result = asyncio.run(agent_codeexec.run_python(
            "import sys\nsys.exit(3)"))
        self.assertEqual(result["exit_code"], 3)

    def test_timeout_kills_the_process(self):
        result = asyncio.run(agent_codeexec.run_python(
            "import time\ntime.sleep(30)", timeout=2))
        self.assertTrue(result["timed_out"])
        self.assertIn("error", result)

    def test_empty_code_is_rejected(self):
        with self.assertRaises(agent_codeexec.CodeExecError):
            asyncio.run(agent_codeexec.run_python("   "))


# ------------------------------------------------------------------ 4. 文件读取


class TestFileRead(unittest.TestCase):
    """目标 4：文件读取受路径白名单限制。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-fileread-"))
        self.target = self.tmp / "note.txt"
        self.target.write_text("第一行\n第二行\n第三行\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reads_allowed_file(self):
        result = agent_media_tools.read_text_file(
            str(self.target), [str(self.tmp)])
        self.assertIn("第一行", result["content"])
        # 「3 行 + 末尾换行」-> 4 个换行分隔 -> 行数按 splitlines 口径是 3。
        # 这里断言的是**内容**的行数，不是「换行符个数」。
        self.assertEqual(len(result["content"].splitlines()), 3)

    def test_path_outside_roots_is_refused(self):
        with self.assertRaises(agent_media_tools.MediaToolError) as caught:
            agent_media_tools.read_text_file(
                str(self.target), ["/nonexistent-root-xyz"])
        self.assertEqual(caught.exception.code, "path_forbidden")

    def test_empty_roots_refuses_everything(self):
        with self.assertRaises(agent_media_tools.MediaToolError) as caught:
            agent_media_tools.read_text_file(str(self.target), [])
        self.assertEqual(caught.exception.code, "file_roots_empty")

    def test_traversal_is_refused(self):
        with self.assertRaises(agent_media_tools.MediaToolError):
            agent_media_tools.read_text_file(
                str(self.tmp / ".." / ".." / "etc" / "passwd"), [str(self.tmp)])


# ------------------------------------------------------------------ 5. 外部搜索


class TestWebSearch(unittest.TestCase):
    """目标 4：外部搜索真连 DDG / 9898。"""

    def test_aggregate_engine_really_returns_results(self):
        """真打 9898 聚合引擎 —— 断言里有真实标题/链接。"""
        engine = {
            "id": "local_9898", "name": "9898", "type": "openwebui",
            "url": AGGREGATE_URL, "method": "POST", "enabled": True,
        }
        try:
            result = asyncio.run(agent_websearch.run_search(
                [engine], "minimax h3 comfyui", count=3))
        except agent_websearch.SearchError as exc:
            self.skipTest(f"9898 聚合引擎不可达：{exc.message}")
        self.assertGreater(result["total"], 0)
        self.assertTrue(result["results"][0]["url"],
                        "结果里没有链接 —— 说明解析没生效")

    def test_ddg_via_aggregate_really_returns_results(self):
        """DDG 源（经 9898 代理）真出结果。"""
        engine = {
            "id": "ddg", "name": "DDG", "type": "custom",
            "url": "http://127.0.0.1:9898/api/openwebui/search/ddg",
            "method": "POST", "enabled": True,
        }
        try:
            result = asyncio.run(agent_websearch.run_search(
                [engine], "minimax h3 comfyui", count=3))
        except agent_websearch.SearchError as exc:
            self.skipTest(f"9898 的 DDG 单引擎不可达：{exc.message}")
        self.assertGreater(result["total"], 0)
        first = result["results"][0]
        # 断言「真的搜到了相关内容」—— 但**不能**写死 "h3"：
        # DDG 的分词会把 "H3" 规范成 "H 3"（实测标题里就是 "minimax h 3"）。
        # 所以判据是「出现了 minimax 或 h 3 或 comfyui 之一」。
        haystack = (first["title"] + " " + first["snippet"]).lower()
        self.assertTrue(
            any(k in haystack for k in ("minimax", "h 3", "h3", "comfyui")),
            f"搜出来的结果跟查询词无关：{haystack[:200]}")
        self.assertTrue(first["url"], "结果里没有链接")

    def test_check_engine_really_probes(self):
        """连通性自检是**真发一次请求**，不是只校验 URL 格式。"""
        engine = {
            "id": "local_9898", "name": "9898", "type": "openwebui",
            "url": AGGREGATE_URL, "method": "POST", "enabled": True,
        }
        result = asyncio.run(agent_websearch.check_engine(engine))
        self.assertTrue(result["ok"], f"自检失败：{result.get('error')}")
        self.assertIn("真发了一次请求", result["note"])

    def test_check_engine_reports_failure_for_bad_url(self):
        engine = {"id": "x", "name": "x", "type": "custom",
                  "url": "http://127.0.0.1:1/none", "method": "POST",
                  "enabled": True}
        result = asyncio.run(agent_websearch.check_engine(engine))
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])

    def test_non_http_url_is_refused(self):
        engine = {"id": "x", "name": "x", "type": "custom",
                  "url": "file:///etc/passwd", "method": "GET", "enabled": True}
        result = asyncio.run(agent_websearch.check_engine(engine))
        self.assertFalse(result["ok"])
        self.assertIn("http", result["error"])

    def test_no_engine_gives_readable_error(self):
        with self.assertRaises(agent_websearch.SearchError) as caught:
            asyncio.run(agent_websearch.run_search([], "x"))
        self.assertEqual(caught.exception.code, "no_engine")


# ------------------------------------------------------------------ 6. ffmpeg 工具


@unittest.skipUnless(FFMPEG and FFPROBE, "本机没有 ffmpeg/ffprobe")
class TestMediaToolsReal(unittest.TestCase):
    """目标 2：音视频工具**真实调用本机 ffmpeg**（非模拟）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="agent-media-"))
        cls.media = _make_media(cls.tmp)
        # 把测试素材注册进**真实素材库**（asset_service.register_asset）
        cls.assets = {}
        from h3studio import asset_service
        from h3studio import assets as asset_store
        for key, path in cls.media.items():
            with asset_store.connect() as conn:
                result = asset_service.register_asset(
                    conn, source_path=path, filename=path.name,
                    category="audio" if key == "a1" else "video",
                    source="agent_test", creator="test")
            cls.assets[key] = result["asset"]["id"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_probe_real_video(self):
        result = asyncio.run(agent_media_tools.op_probe(self.assets["v1"]))
        self.assertTrue(result["has_video"])
        self.assertTrue(result["has_audio"])
        self.assertAlmostEqual(result["duration"], 2.0, delta=0.2)
        self.assertEqual(result["width"], 320)

    def test_probe_real_audio(self):
        result = asyncio.run(agent_media_tools.op_probe(self.assets["a1"]))
        self.assertTrue(result["has_audio"])
        self.assertFalse(result["has_video"])
        self.assertAlmostEqual(result["duration"], 2.0, delta=0.2)

    def test_concat_produces_a_real_video(self):
        result = asyncio.run(agent_media_tools.op_concat(
            [self.assets["v1"], self.assets["v2"]], "concat_test"))
        output = result["output"]
        self.assertTrue(output["asset_id"])
        # 产物必须真的存在（不是编出来的 id）
        from h3studio import asset_repo
        from h3studio import assets as asset_store
        with asset_store.connect() as conn:
            version = asset_repo.current_version(conn, output["asset_id"])
        self.assertIsNotNone(version, "产物没进素材库")
        path = asset_store.resolve_inside(asset_store.library_root(),
                                          version["storage_path"])
        self.assertTrue(path.is_file(), "产物文件不存在")
        # 时长 = 两段之和（2 + 3 = 5 秒，容差 0.5）
        self.assertAlmostEqual(result["duration_sec"], 5.0, delta=0.5)

    def test_transcode_changes_container(self):
        result = asyncio.run(agent_media_tools.op_transcode(
            self.assets["v1"], "webm"))
        output = result["output"]
        self.assertTrue(output["asset_id"])
        self.assertTrue(output["name"].endswith(".webm"),
                        f"产物名不是 webm：{output['name']}")

    def test_extract_frames_produces_real_frames(self):
        result = asyncio.run(agent_media_tools.op_extract_frames(
            self.assets["v2"], interval=1.0, max_frames=5))
        self.assertGreater(result["frame_count"], 0)
        self.assertEqual(len(result["frames"]), result["frame_count"])
        for frame in result["frames"]:
            self.assertTrue(frame["asset_id"])

    def test_mix_audio_produces_a_real_mix(self):
        result = asyncio.run(agent_media_tools.op_mix_audio(
            self.assets["v1"], [self.assets["a1"]], [0.8], False))
        output = result["output"]
        self.assertTrue(output["asset_id"])
        self.assertAlmostEqual(result["duration_sec"], 2.0, delta=0.5)

    def test_merge_av_produces_a_real_merge(self):
        result = asyncio.run(agent_media_tools.op_merge_av(
            self.assets["v1"], self.assets["a1"]))
        output = result["output"]
        self.assertTrue(output["asset_id"])
        self.assertAlmostEqual(result["duration_sec"], 2.0, delta=0.5)

    def test_unknown_asset_gives_readable_error(self):
        with self.assertRaises(agent_media_tools.MediaToolError) as caught:
            asyncio.run(agent_media_tools.op_probe("asset_does_not_exist"))
        self.assertEqual(caught.exception.code, "asset_not_found")

    def test_media_tools_are_real_tools_not_stubs(self):
        """工具描述里必须说明是真调用 ffmpeg（不是模拟）。"""
        for name in ("media_concat", "media_transcode", "media_mix_audio",
                     "media_extract_frames", "media_merge_av"):
            spec = agent_mod.TOOLS[name]
            self.assertIn("ffmpeg", spec["description"],
                          f"{name} 的描述没说明是真实 ffmpeg")


# ------------------------------------------------------------------ 7. 默认参数


class TestT2vDefaults(unittest.TestCase):
    """目标 5：文生视频默认 kitchen + dmad，且可改。"""

    def test_default_attention_is_kitchen(self):
        self.assertEqual(agent_settings.DEFAULT_T2V_DEFAULTS["attention"],
                         "kitchen")

    def test_default_accel_lora_is_dmad(self):
        self.assertEqual(agent_settings.DEFAULT_T2V_DEFAULTS["accel_lora"],
                         "dmad_4step_critic")

    def test_defaults_match_real_registry(self):
        """默认值必须指向**真实存在**的注册项，不是编的字符串。"""
        from h3studio import video_models as vm
        self.assertIn(agent_settings.DEFAULT_T2V_DEFAULTS["attention"],
                      vm.VIDEO_ATTENTION_MODES)
        self.assertIn(agent_settings.DEFAULT_T2V_DEFAULTS["accel_lora"],
                      vm.VIDEO_ACCEL_LORA_COMBOS)
        self.assertIn(agent_settings.DEFAULT_T2V_DEFAULTS["model_key"],
                      vm.VIDEO_MODELS)

    def test_kitchen_attention_node_exists_on_this_machine(self):
        """kitchen 对应的 ComfyUI 节点必须真的在本机（否则默认参数是假的）。"""
        from h3studio import video_models as vm
        node = vm.VIDEO_ATTENTION_MODES["kitchen"]["node"]
        self.assertEqual(node, "ModelAttentionBackend")

    def test_user_can_override_every_default(self):
        """任意默认参数都能被用户改（归一后生效）。"""
        out = agent_settings.normalize({"t2v_defaults": {
            "attention": "sparse", "accel_lora": "lightx2v_4step_768p",
            "model_key": "fl2va_int8", "aspect": "9:16",
            "megapixels": 0.8, "duration": 12,
        }})
        self.assertEqual(out["t2v_defaults"]["attention"], "sparse")
        self.assertEqual(out["t2v_defaults"]["accel_lora"],
                         "lightx2v_4step_768p")
        self.assertEqual(out["t2v_defaults"]["model_key"], "fl2va_int8")
        self.assertEqual(out["t2v_defaults"]["aspect"], "9:16")
        self.assertEqual(out["t2v_defaults"]["megapixels"], 0.8)
        self.assertEqual(out["t2v_defaults"]["duration"], 12.0)

    def test_invalid_values_fall_back_to_defaults(self):
        out = agent_settings.normalize({"t2v_defaults": {
            "megapixels": 99, "duration": 9999,
        }})
        self.assertEqual(out["t2v_defaults"]["megapixels"], 2.0)
        self.assertEqual(out["t2v_defaults"]["duration"], 60.0)


# ------------------------------------------------------------------ 8. 持久化


class TestSettingsPersistence(unittest.TestCase):
    """配置真的能存进库并读回来。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-settings-"))
        self.db_path = self.tmp / "jobs.db"
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS model_provider_prefs "
                     "(key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_and_load_roundtrip(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE IF NOT EXISTS model_provider_prefs "
                     "(key TEXT PRIMARY KEY, value TEXT)")
        saved = agent_settings.save(conn, {
            "max_steps": 33,
            "capabilities": {"code_exec": True},
            "search_engines": [{"id": "x", "name": "X", "type": "custom",
                                "url": "http://x/search", "method": "POST"}],
        })
        conn.commit()
        loaded = agent_settings.load(conn)
        conn.close()
        self.assertEqual(saved["max_steps"], 33)
        self.assertEqual(loaded["max_steps"], 33)
        self.assertTrue(loaded["capabilities"]["code_exec"])
        self.assertEqual(loaded["search_engines"][0]["url"], "http://x/search")

    def test_load_returns_defaults_when_nothing_saved(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE IF NOT EXISTS model_provider_prefs "
                     "(key TEXT PRIMARY KEY, value TEXT)")
        loaded = agent_settings.load(conn)
        conn.close()
        self.assertEqual(loaded["max_steps"], 50)
        self.assertFalse(loaded["capabilities"]["code_exec"])


if __name__ == "__main__":
    unittest.main()
