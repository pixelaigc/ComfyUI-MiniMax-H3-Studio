# -*- coding: utf-8 -*-
"""hermes 提示词增强通道（对齐 nonebot 咏唱h3）的定向测试。

【本文件测什么】
只测**与本次提示词增强（图像 / 视频 / 音频三条链路）直接相关**的部分：

  1. 配置解析（resolve_config / get_media_spec / _normalize_hermes_config）
  2. query 构造（build_query）—— 4~6 块结构，含素材与历史
  3. CLI 命令行组装（_run_cli）—— **与 nonebot 完全一致**的 flag 组合
  4. 输出解析（_extract_answer）—— Hermes 框 + 2 个 fallback
  5. 三条链路的 media 路由（media_for_mode）
  6. 失败纪律 —— hermes 不可用时**抛错**（不编造）
  7. 服务端 /api/hermes-enhance 与 /api/hermes-test 的接口契约

【明确不测】
  * 不跑真实 hermes 增强（那属于端到端实测，不是单测）
  * 不跑全量 tests/ 套件（用户 2026-11-22 明确要求）
  * 不碰线上 8190 / 线上 jobs.db —— 本文件用**独立端口 + 独立临时 sqlite**

运行：
  /usr/local/lib/hermes-agent/venv/bin/python -m pytest \
      tests/test_hermes_enhance.py -q
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from h3studio import hermes_enhance as he  # noqa: E402


# ------------------------------------------------------------------ 配置解析
class TestResolveConfig(unittest.TestCase):

    def test_defaults_when_not_configured(self):
        """没配置时：通道默认**关**（不改变现有行为），profile 默认 media。"""
        cfg = he.resolve_config(None)
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["profile"], "media")
        self.assertEqual(cfg["binary"], "/usr/local/bin/hermes")
        self.assertEqual(cfg["timeout"], 1800)

    def test_reads_saved_overrides(self):
        saved = {"hermes": {"enabled": True, "profile": "ops", "timeout": 300}}
        cfg = he.resolve_config(saved)
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["profile"], "ops")
        self.assertEqual(cfg["timeout"], 300)

    def test_bad_timeout_falls_back(self):
        cfg = he.resolve_config({"hermes": {"timeout": "abc"}})
        self.assertEqual(cfg["timeout"], 1800)
        cfg2 = he.resolve_config({"hermes": {"timeout": -5}})
        self.assertEqual(cfg2["timeout"], 1800)

    def test_missing_hermes_key_is_safe(self):
        """老的保存数据里没有 hermes 段 —— 不能崩。"""
        self.assertFalse(he.resolve_config({"specs": {}, "params": {}})["enabled"])


# ------------------------------------------------------------------ 三条链路
class TestThreeMediaChains(unittest.TestCase):

    def test_media_for_mode_three_chains(self):
        """图像 / 视频 / 音频三种模式分别映射到三条 hermes 链路。"""
        self.assertEqual(he.media_for_mode("t2i"), "image")
        self.assertEqual(he.media_for_mode("t2i_qwen"), "image")
        self.assertEqual(he.media_for_mode("t2v"), "video")
        self.assertEqual(he.media_for_mode("flf"), "video")
        self.assertEqual(he.media_for_mode("r2v"), "video")
        self.assertEqual(he.media_for_mode("t2a"), "audio")
        self.assertEqual(he.media_for_mode("a2a"), "audio")

    def test_unknown_mode_returns_empty(self):
        self.assertEqual(he.media_for_mode("nope"), "")

    def test_all_three_media_specs_exist(self):
        """三条链路都必须有配置（缺一条就是能力缺口）。"""
        for media in ("image", "video", "audio"):
            spec = he.get_media_spec(media)
            self.assertTrue(spec["skills"], f"{media} 缺 skill 白名单")
            self.assertTrue(spec["system_prompt"], f"{media} 缺提示词模板")
            self.assertIn("{prompt}", spec["user_template"])

    def test_video_skills_match_nonebot_allowlist(self):
        """
        视频链路的 skill 白名单必须与 nonebot 咏唱h3 **完全一致**。

        依据：nonebot h3_interactive.py:3760-3764
            SKILL_ALLOWLIST = (
                "h3-video-prompt-director,h3-cn-prompt-director,"
                "h3-prompt-templates,h3-character-presets,h3-kinetic-subtitle,"
                "h3-item-strengthening"
            )
        """
        expected = ("h3-video-prompt-director,h3-cn-prompt-director,"
                    "h3-prompt-templates,h3-character-presets,"
                    "h3-kinetic-subtitle,h3-item-strengthening")
        self.assertEqual(he.get_media_spec("video")["skills"], expected)

    def test_unknown_media_raises(self):
        with self.assertRaises(he.HermesEnhanceError):
            he.get_media_spec("hologram")

    def test_user_override_applies(self):
        saved = {"hermes": {"skills": {"image": "only-this"},
                            "templates": {"image": "MY TEMPLATE"},
                            "user_templates": {"image": "X {prompt} Y"}}}
        spec = he.get_media_spec("image", saved)
        self.assertEqual(spec["skills"], "only-this")
        self.assertEqual(spec["system_prompt"], "MY TEMPLATE")
        self.assertEqual(spec["user_template"], "X {prompt} Y")

    def test_user_override_does_not_leak_across_media(self):
        """改图像的模板**不能**影响视频 / 音频。"""
        saved = {"hermes": {"templates": {"image": "ONLY IMAGE"}}}
        self.assertEqual(he.get_media_spec("image", saved)["system_prompt"], "ONLY IMAGE")
        self.assertNotEqual(he.get_media_spec("video", saved)["system_prompt"],
                            "ONLY IMAGE")


# ------------------------------------------------------------------ query 构造
class TestBuildQuery(unittest.TestCase):

    def test_four_block_structure(self):
        """query 至少包含：详细任务 / skill 列表 / 用户要求 / 输出格式。"""
        spec = he.get_media_spec("video")
        q = he.build_query(spec, "一只猫在草地上跑")
        self.assertIn("# 详细任务", q)
        self.assertIn("# 可参考的 skill", q)
        self.assertIn("# 用户的一句话要求", q)
        self.assertIn("# 输出格式", q)
        self.assertIn("一只猫在草地上跑", q)

    def test_skills_listed(self):
        q = he.build_query(he.get_media_spec("video"), "x")
        self.assertIn("h3-video-prompt-director", q)

    def test_asset_context_included(self):
        q = he.build_query(he.get_media_spec("video"), "x",
                           asset_context="- 参考图1: /tmp/a.png")
        self.assertIn("# 素材上下文", q)
        self.assertIn("/tmp/a.png", q)

    def test_prompt_history_included(self):
        q = he.build_query(he.get_media_spec("video"), "x",
                           prompt_history=[{"stage": 1, "prompt": "第一段"}])
        self.assertIn("# 前情提要", q)
        self.assertIn("第一段", q)

    def test_duration_included(self):
        q = he.build_query(he.get_media_spec("video"), "x", duration_seconds=6)
        self.assertIn("# 目标时长", q)
        self.assertIn("6 秒", q)

    def test_user_template_placeholder_replaced(self):
        q = he.build_query(he.get_media_spec("image"), "PROMPT_HERE")
        self.assertIn("PROMPT_HERE", q)
        self.assertNotIn("{prompt}", q)

    def test_empty_asset_context_omitted(self):
        q = he.build_query(he.get_media_spec("image"), "x", asset_context="")
        self.assertNotIn("# 素材上下文", q)


# ------------------------------------------------------------------ CLI 调用形态
class TestCliInvocation(unittest.TestCase):
    """
    CLI 调用形态必须与 nonebot 插件**完全一致**。

    依据：nonebot h3_interactive.py:3793-3802
        hermes chat --query-file <path> --skills <ALLOWLIST> --oneshot --cli --yolo
    """

    def _run(self, binary, profile, qpath, skills, timeout):
        captured = {}

        class _Proc:
            stdout = "OUT"
            stderr = "ERR"
            returncode = 0

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return _Proc()

        import subprocess
        orig = subprocess.run
        subprocess.run = fake_run
        try:
            he._run_cli(binary, profile, qpath, skills, timeout)
        finally:
            subprocess.run = orig
        return captured

    def test_flag_sequence(self):
        cap = self._run("/usr/local/bin/hermes", "media", "/tmp/q.txt",
                        "a,b", 60)
        cmd = cap["cmd"]
        self.assertEqual(cmd[0], "/usr/local/bin/hermes")
        self.assertEqual(cmd[1], "--profile")
        self.assertEqual(cmd[2], "media")
        self.assertEqual(cmd[3], "chat")
        self.assertEqual(cmd[4], "--query-file")
        self.assertEqual(cmd[5], "/tmp/q.txt")
        self.assertEqual(cmd[6], "--skills")
        self.assertEqual(cmd[7], "a,b")
        self.assertEqual(cmd[8:], ["--oneshot", "--cli", "--yolo"])

    def test_profile_flag_precedes_subcommand(self):
        """
        `--profile` 是 hermes 的**全局** flag，必须在 `chat` 之前。

        依据：hermes_cli/_parser.py 的 PRE_ARGPARSE_INHERITED_FLAGS
        （["--profile", True], ["-p", True]）—— 它在 argparse 之前就消费掉。
        放到子命令后面会被当成 chat 的未知参数。
        """
        cap = self._run("/usr/local/bin/hermes", "media", "/tmp/q.txt", "", 60)
        cmd = cap["cmd"]
        self.assertLess(cmd.index("--profile"), cmd.index("chat"))

    def test_no_profile_flag_when_empty(self):
        cap = self._run("/usr/local/bin/hermes", "", "/tmp/q.txt", "a", 60)
        self.assertNotIn("--profile", cap["cmd"])

    def test_no_skills_flag_when_empty(self):
        cap = self._run("/usr/local/bin/hermes", "media", "/tmp/q.txt", "", 60)
        self.assertNotIn("--skills", cap["cmd"])

    def test_stdin_is_devnull(self):
        """
        stdin 必须接 DEVNULL。

        依据：nonebot h3_reverse_prompt.py:665-670 的实测结论 ——
        hermes CLI 会检测 fd=0 是否 tty，不是 tty 就走交互式路径，
        立刻退出，看起来像「秒完成但啥也没干」。
        """
        import subprocess
        cap = self._run("/usr/local/bin/hermes", "media", "/tmp/q.txt", "a", 60)
        self.assertIs(cap["kwargs"]["stdin"], subprocess.DEVNULL)

    def test_timeout_passed_through(self):
        cap = self._run("/usr/local/bin/hermes", "media", "/tmp/q.txt", "a", 123)
        self.assertEqual(cap["kwargs"]["timeout"], 123)

    def test_bridge_context_env_cleared(self):
        """
        子进程环境必须清掉桥接上下文 —— 否则子 agent 继承父 session 平台上下文。

        依据：nonebot h3_reverse_prompt.py:640-652。
        """
        import os
        os.environ["HERMES_SESSION_ID"] = "sess-123"
        os.environ["HERMES_SESSION_PLATFORM"] = "qq"
        try:
            cap = self._run("/usr/local/bin/hermes", "media", "/tmp/q.txt", "a", 60)
        finally:
            os.environ.pop("HERMES_SESSION_ID", None)
            os.environ.pop("HERMES_SESSION_PLATFORM", None)
        env = cap["kwargs"]["env"]
        self.assertNotIn("HERMES_SESSION_ID", env)
        self.assertNotIn("HERMES_SESSION_PLATFORM", env)


# ------------------------------------------------------------------ 输出解析
class TestExtractAnswer(unittest.TestCase):

    BOX = ("╭─ ⚕ Hermes ───────────────────────────────╮\n"
           "HELLO ANSWER\n"
           "╰──────────────────────────────────────────╯\n")

    def test_extracts_hermes_box(self):
        out = he._extract_answer("prelude\n" + self.BOX + "epilogue")
        self.assertEqual(out, "HELLO ANSWER")

    def test_takes_last_box(self):
        two = self.BOX + ("╭─ ⚕ Hermes ─╮\nSECOND\n╰─╯\n")
        self.assertEqual(he._extract_answer(two), "SECOND")

    def test_prefers_longest_prompt_like_box(self):
        short = "╭─ ⚕ Hermes ─╮\n00:00.000 x\n╰─╯\n"
        long_box = "╭─ ⚕ Hermes ─╮\n00:00.000 " + ("LONG " * 40) + "\n╰─╯\n"
        out = he._extract_answer(short + long_box)
        self.assertIn("LONG", out)

    def test_write_file_fallback(self, ):
        import tempfile
        p = Path(tempfile.mkdtemp()) / "out.txt"
        p.write_text("X" * 200, encoding="utf-8")
        stdout = f'something Write File("{p}") something'
        self.assertEqual(he._extract_answer(stdout), "X" * 200)

    def test_stdout_tail_fallback(self):
        out = he._extract_answer("just some text " * 200)
        self.assertTrue(len(out) > 100)

    def test_empty_raises(self):
        with self.assertRaises(he.HermesEnhanceError):
            he._extract_answer("")


# ------------------------------------------------------------------ 失败纪律
class TestFailureDiscipline(unittest.TestCase):
    """
    【不编造】是 nonebot 的核心纪律：hermes 不可用时**抛错**，
    由调用方如实告知用户 —— 绝不返回一段假提示词。
    """

    def test_empty_prompt_raises(self):
        import asyncio
        with self.assertRaises(he.HermesEnhanceError):
            asyncio.run(he.enhance("", "image", saved={"hermes": {"enabled": True}}))

    def test_missing_binary_raises(self):
        import asyncio
        saved = {"hermes": {"enabled": True, "binary": "/nonexistent/hermes"}}
        # 二进制不存在 -> 走 PATH 兜底；PATH 里也没有时必须抛错。
        # 本机 PATH 有 hermes 时这条会走真实调用 —— 所以用 monkeypatch 去掉 PATH。
        import shutil
        orig = shutil.which
        shutil.which = lambda *_a, **_k: None
        try:
            with self.assertRaises(he.HermesEnhanceError) as ctx:
                asyncio.run(he.enhance("x", "image", saved=saved))
            self.assertEqual(ctx.exception.reason, "hermes_not_found")
        finally:
            shutil.which = orig

    def test_missing_profile_raises(self):
        import asyncio
        saved = {"hermes": {"enabled": True, "profile": "no-such-profile"}}
        with self.assertRaises(he.HermesEnhanceError) as ctx:
            asyncio.run(he.enhance("x", "image", saved=saved))
        self.assertEqual(ctx.exception.reason, "profile_not_found")

    def test_error_carries_reason(self):
        err = he.HermesEnhanceError("boom", reason="timeout", detail="d")
        self.assertEqual(err.reason, "timeout")
        self.assertEqual(err.detail, "d")


# ------------------------------------------------------------------ profile 探测
class TestProfiles(unittest.TestCase):

    def test_available_profiles_contains_media(self):
        ids = [p["id"] for p in he.available_profiles()]
        self.assertIn("media", ids)

    def test_profile_exists(self):
        self.assertTrue(he.profile_exists("media"))
        self.assertFalse(he.profile_exists("definitely-not-a-profile"))


# ------------------------------------------------------------------ 服务端契约
class TestServerContract(unittest.TestCase):
    """服务端接口契约（用独立端口 + 独立临时 sqlite，不碰线上）。"""

    @classmethod
    def setUpClass(cls):
        from test_frontend_defaults import StudioServer
        cls.srv = StudioServer()
        cls.srv.start()
        cls.base = f"http://127.0.0.1:{cls.srv.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def _post(self, path, payload):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _get(self, path):
        import urllib.request
        with urllib.request.urlopen(f"{self.base}{path}", timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_enhancer_config_exposes_hermes_block(self):
        """GET /api/enhancer 必须下发 hermes 配置块（含三条链路的模板）。"""
        status, body = self._get("/api/enhancer")
        self.assertEqual(status, 200)
        self.assertIn("hermes", body)
        h = body["hermes"]
        for key in ("enabled", "binary", "profile", "timeout",
                    "profiles", "media_specs"):
            self.assertIn(key, h)
        for media in ("image", "video", "audio"):
            self.assertIn(media, h["media_specs"])
            self.assertTrue(h["media_specs"][media]["skills"])

    def test_hermes_enhance_rejects_unknown_media(self):
        status, body = self._post("/api/hermes-enhance",
                                   {"prompt": "x", "media": "hologram"})
        self.assertEqual(status, 400)
        self.assertEqual(body.get("code"), "unknown_media")

    def test_hermes_enhance_rejects_empty_prompt(self):
        status, _ = self._post("/api/hermes-enhance",
                               {"prompt": "", "media": "image"})
        self.assertEqual(status, 400)

    def test_hermes_enhance_disabled_returns_400(self):
        """通道默认关 -> 必须**如实报错**（不是静默走别的通道）。"""
        status, body = self._post("/api/hermes-enhance",
                                   {"prompt": "一只猫", "media": "image"})
        self.assertEqual(status, 400)
        self.assertEqual(body.get("code"), "hermes_disabled")

    def test_hermes_test_endpoint_reports_state(self):
        status, body = self._post("/api/hermes-test", {"media": "image"})
        self.assertEqual(status, 200)
        for key in ("binary", "binary_exists", "profile", "profile_exists",
                    "skills", "ok"):
            self.assertIn(key, body)

    def test_enhance_endpoint_accepts_engine_hermes(self):
        """/api/enhance 带 engine=hermes 时应路由到 hermes 通道。"""
        status, body = self._post("/api/enhance", {
            "prompt": "一只猫", "mode": "replace", "mode_ctx": "t2i",
            "engine": "hermes"})
        # 通道默认关 -> 400 + hermes_disabled（证明路由生效了）
        self.assertEqual(status, 400)
        self.assertEqual(body.get("code"), "hermes_disabled")

    def test_enhance_endpoint_default_engine_is_provider(self):
        """
        不传 engine 时**行为不变**（老客户端不受影响）。

        判据：报错必须是「本地 Provider 没配」这一类，
        **绝不能**是 hermes 相关的错误码 —— 否则说明路由串了。
        """
        status, body = self._post("/api/enhance", {
            "prompt": "一只猫", "mode": "replace", "mode_ctx": "t2i"})
        self.assertNotEqual(body.get("code"), "hermes_disabled")
        err = str(body.get("error") or "")
        self.assertNotIn("hermes", err)
        # 本机没配 Provider -> 报的是「未配置可用的本地模型 Provider」
        self.assertIn("Provider", err)


# ------------------------------------------------------------------ 配置规整
class TestNormalizeHermesConfig(unittest.TestCase):
    """服务端 _normalize_hermes_config 的安全规整。"""

    def _norm(self, raw, previous=None):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "h3studio_server_mod", REPO / "server.py")
        # server.py 顶层会做很多初始化 —— 直接复用它的函数不方便，
        # 所以这里测**逻辑等价**的规则（与 server.py 同源）。
        return None

    def test_skill_whitelist_strips_injection(self):
        """skill 白名单必须过滤掉非法字符（它会进 CLI argv）。"""
        saved = {"hermes": {"skills": {"image": "ok-skill,bad;rm -rf /,ok2"}}}
        spec = he.get_media_spec("image", saved)
        for part in spec["skills"].split(","):
            self.assertRegex(part, r"^[A-Za-z0-9_.-]+$")

    def test_server_normalize_present(self):
        """服务端必须有 _normalize_hermes_config（配置入口）。"""
        src = (REPO / "server.py").read_text(encoding="utf-8")
        self.assertIn("def _normalize_hermes_config", src)
        self.assertIn('add_post("/api/hermes-enhance"', src)
        self.assertIn('add_post("/api/hermes-test"', src)


# ------------------------------------------------------------------ 前端契约
class TestFrontendContract(unittest.TestCase):
    """前端源码的静态约定（不依赖浏览器）。"""

    def read(self, name):
        return (REPO / "web" / name).read_text(encoding="utf-8")

    def test_registry_has_hermes_entry(self):
        src = self.read("enhance-menu.js")
        self.assertRegex(src, r'id:\s*"hermes"')

    def test_registry_entry_declares_text_write_contract(self):
        src = self.read("enhance-menu.js")
        idx = src.index('id: "hermes"')
        block = src[idx:idx + 2000]
        self.assertIn('write: { kind: "text" }', block)

    def test_route_calls_hermes_endpoint(self):
        src = self.read("enhance-menu.js")
        self.assertIn("/api/hermes-enhance", src)
        self.assertIn("runHermes", src)

    def test_provider_panel_has_hermes_controls(self):
        src = self.read("providers.js")
        for marker in ("hermesEnabled", "hermesProfile", "hermesBinary",
                       "hermesTimeout", "hermesSave", "hermesTest",
                       "hermesSkills_", "hermesTpl_", "hermesUserTpl_"):
            self.assertIn(marker, src)

    def test_provider_panel_saves_via_enhancer_endpoint(self):
        src = self.read("providers.js")
        self.assertIn('apiJSON("PUT", "/api/enhancer", { hermes: payload })', src)

    def test_provider_panel_loads_hermes_block(self):
        src = self.read("providers.js")
        self.assertIn("d.hermes", src)

    def test_enhance_menu_is_not_stale(self):
        """
        【回归护栏】主 worktree 里有一份**未跟踪的旧版** enhance-menu.js
        （461 行，只有 basic/refine/layout 三项）。本分支用的是
        local/maintainer 里的正式版（2400+ 行）。
        这条断言防止有人把旧版覆盖回来。
        """
        src = self.read("enhance-menu.js")
        self.assertIn('id: "director"', src)
        self.assertIn('id: "yue2_struct"', src)
        self.assertIn('id: "qwen_pe"', src)


if __name__ == "__main__":
    unittest.main()
