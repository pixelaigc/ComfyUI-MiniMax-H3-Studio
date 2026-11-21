# -*- coding: utf-8 -*-
"""
【2026-11-22 任务：Agent 能力自述与真实能力脱节 —— 补齐「运行期真源」测试】

前面的 session 已经把「能力自述从真源渲染」做完了
（``tests/test_agent_capability_truth.py``，61 项）。本文件补的是
**本轮实测暴露、此前没有测试覆盖的三个缺口**：

  A. **运行期自述用导入期快照**（真实缺陷）
     ``run_agent`` 原来用模块级 ``SYSTEM_PROMPT``（导入期快照）构造
     system 消息 ⇒ 真源在**服务运行期间**变化时，Agent 的自述不变。
     ⇒ 测试：改真源 -> ``run_agent`` 实际发出的 system 消息必须跟着变。

  B. **提示词里的工具清单与真实注册表漂移**
     实测：``media_replace_audio`` / ``manage_media_library`` 已注册，
     但提示词工具清单里没有 ⇒ 模型不知道自己有这两个工具。
     ⇒ 测试：注册表里的每个工具都必须在提示词清单里出现。

  C. **降级路径不能说旧白名单**
     实测：``_video_size_tiers()`` 原来在真源导入失败时回退到
     ``workflows.RES_MP`` = (544p, 768p) ⇒ 重新说「只有 544p/768p」。
     ⇒ 测试：降级时返回空集合，且能力文本不含旧白名单的说法。

运行：python3 -m pytest tests/test_agent_capability_runtime_truth.py -v
"""
from __future__ import annotations

import re
import unittest
from unittest import mock

from h3studio import agent as agent_mod
from h3studio import agent_capability as cap
from h3studio import agent_schema
from h3studio import video_models as vm


# ==================================================================
# A. 运行期自述必须跟着真源变（不是导入期快照）
# ==================================================================


class TestRuntimeCapabilityTruth(unittest.TestCase):
    """run_agent 每次调用都重新渲染能力自述。"""

    def _run_and_capture_system_prompt(self):
        """
        跑一次 run_agent，截获它发给模型的 system 消息。

        【怎么截获】把 llm 层的 provider 调用换成假的 —— 它只记录
        messages 然后抛一个哨兵异常。这样不需要真的调模型，也不需要 GPU。

        【为什么 mock llm_client 而不是 agent 里的某个函数】
        agent.py 走的是 ``llm_client.call_provider_with_tools_stream``
        （见 agent.py:4128）—— 直接 mock 这一层最贴近真实链路，
        也避免「mock 了一个其实没被调用的名字」这种**假绿**测试。
        """
        captured: dict = {}

        class _Stop(Exception):
            pass

        async def _fake_stream(provider, messages, tools, **kwargs):
            captured["messages"] = list(messages)
            raise _Stop()

        with mock.patch.object(agent_mod.llm_client,
                               "call_provider_with_tools_stream",
                               _fake_stream), \
                mock.patch.object(agent_mod, "_resolve_provider",
                                  lambda *a, **k: {"name": "fake",
                                                   "model": "fake",
                                                   "base_url": "http://x",
                                                   "api_key": "x"}), \
                mock.patch.object(agent_mod, "_check_provider_supports_tools",
                                  lambda *a, **k: None):
            import asyncio
            with self.assertRaises(Exception):
                asyncio.new_event_loop().run_until_complete(
                    agent_mod.run_agent(server_module=None,
                                        message="测试",
                                        session_id="test-runtime-truth"))
        return captured.get("messages") or []

    def test_run_agent_uses_runtime_rendered_prompt_not_import_snapshot(self):
        """
        核心断言：真源改了之后，run_agent 发出的 system 消息必须变。

        【为什么这条能抓住缺陷】旧实现用的是导入期快照 SYSTEM_PROMPT，
        改 VIDEO_MAX_CANVAS_PIXELS 不会影响它 ⇒ 断言失败。
        """
        original = vm.VIDEO_MAX_CANVAS_PIXELS
        try:
            vm.VIDEO_MAX_CANVAS_PIXELS = int(0.5 * 1024 * 1024)
            messages = self._run_and_capture_system_prompt()
            system_text = "\n".join(
                str(m.get("content") or "") for m in messages
                if m.get("role") == "system")
            self.assertTrue(system_text, "没有捕获到 system 消息")
            # 新上限必须出现在**运行期渲染**的提示词里
            self.assertIn("0.5 MP", system_text,
                          "run_agent 的 system 消息没有反映真源改动 —— "
                          "它仍在用导入期快照")
            # 旧上限不应再出现
            self.assertNotIn("2.0 MP", system_text,
                             "run_agent 的 system 消息仍带旧的 2.0 MP 上限")
        finally:
            vm.VIDEO_MAX_CANVAS_PIXELS = original

    def test_build_system_prompt_reflects_config_change(self):
        """build_system_prompt() 本身是动态的（这条是上条的前提）。"""
        original = vm.VIDEO_MAX_CANVAS_PIXELS
        try:
            vm.VIDEO_MAX_CANVAS_PIXELS = int(0.75 * 1024 * 1024)
            text = agent_mod.build_system_prompt()
            self.assertIn("0.75 MP", text)
        finally:
            vm.VIDEO_MAX_CANVAS_PIXELS = original

    def test_capability_text_reflects_duration_range_change(self):
        """时长区间改动也要跟着变。"""
        original_min = vm.VIDEO_DURATION_MIN
        original_max = vm.VIDEO_DURATION_MAX
        try:
            vm.VIDEO_DURATION_MIN = 5.0
            vm.VIDEO_DURATION_MAX = 25.0
            text = agent_mod.build_system_prompt()
            self.assertIn("5~25", text)
            self.assertNotIn("2~60", text)
        finally:
            vm.VIDEO_DURATION_MIN = original_min
            vm.VIDEO_DURATION_MAX = original_max

    def test_capability_text_reflects_music_duration_change(self):
        """音乐时长也必须是真源派生。"""
        from h3studio import music_yue2
        original_max = music_yue2.YUE2_MAX_DURATION
        try:
            music_yue2.YUE2_MAX_DURATION = 120.0
            text = agent_mod.build_system_prompt()
            self.assertIn("4~120", text)
        finally:
            music_yue2.YUE2_MAX_DURATION = original_max


# ==================================================================
# B. 提示词工具清单 == 真实注册表
# ==================================================================


class TestPromptToolListMatchesRegistry(unittest.TestCase):
    """每个注册的工具都必须在系统提示词的工具清单里被点名。"""

    def _registered(self):
        tools = agent_mod.TOOLS
        return set(tools.keys()) if isinstance(tools, dict) else set(tools)

    def _listed_in_prompt(self):
        return set(re.findall(r"^- ([a-z_]+)\(",
                              agent_mod.SYSTEM_PROMPT_TEMPLATE, re.M))

    def test_every_registered_tool_is_named_in_prompt(self):
        registered = self._registered()
        listed = self._listed_in_prompt()
        missing = sorted(registered - listed)
        self.assertEqual(
            [], missing,
            f"这些工具已注册但系统提示词没列出来 —— 模型不知道自己有它们: {missing}")

    def test_no_phantom_tools_in_prompt(self):
        """提示词里不能出现未注册的工具名（会诱导模型调用不存在的工具）。"""
        registered = self._registered()
        listed = self._listed_in_prompt()
        phantom = sorted(listed - registered)
        self.assertEqual([], phantom,
                         f"提示词列了未注册的工具: {phantom}")

    def test_media_replace_audio_is_listed(self):
        """实测漏掉的那个（用户要「合并音视频」时用得上）。"""
        self.assertIn("media_replace_audio", self._listed_in_prompt())

    def test_all_media_tools_listed(self):
        """10 个 ffmpeg 媒体后处理工具全部要在清单里。"""
        listed = self._listed_in_prompt()
        expected = {
            "media_probe", "media_concat", "media_transcode",
            "media_extract_frames", "media_mix_audio", "media_merge_av",
            "media_replace_audio", "media_extract_audio", "media_trim",
            "media_normalize_audio",
        }
        self.assertEqual(set(), expected - listed,
                         "媒体后处理工具没列全")


# ==================================================================
# C. 降级路径不得说旧白名单
# ==================================================================


class TestNoStaleWhitelistFallback(unittest.TestCase):
    """真源不可用时宁可不说，也不能说 544p/768p。"""

    def test_video_size_tiers_degrades_to_empty_not_old_whitelist(self):
        """
        降级路径必须返回空集合。

        【怎么真的触发降级】把 ``h3studio`` 包上的 ``video_models``
        属性换成一个**残缺模块**（没有 VIDEO_ASPECTS）——
        这样 ``from . import video_models`` 不会抛 ImportError，
        但取属性会抛 AttributeError ⇒ 走进 except 分支。

        【为什么不只用 sys.modules[name] = None】那样 import 会拿到
        None，随后 ``vm.VIDEO_ASPECTS`` 抛 AttributeError —— 而旧代码
        把属性访问放在 try **外面**，异常会直接冒出去（测不到降级）。
        现在属性访问也在 try 里 ⇒ 任何「真源不可用」都统一降级。
        """
        import types
        import h3studio
        saved_attr = h3studio.__dict__.get("video_models", "MISSING")
        broken = types.ModuleType("h3studio.video_models")
        h3studio.__dict__["video_models"] = broken
        try:
            tiers = agent_schema._video_size_tiers()
        finally:
            if saved_attr == "MISSING":
                h3studio.__dict__.pop("video_models", None)
            else:
                h3studio.__dict__["video_models"] = saved_attr
        self.assertEqual([], tiers,
                         "降级时返回了档位清单 —— 必须为空（不谎报）")
        self.assertNotIn("544p", tiers)
        self.assertNotIn("768p", tiers)

    def test_music_duration_fallback_matches_real_truth(self):
        """
        音乐时长兜底必须跟真源一致（900），不能是旧的 360。

        【为什么这条重要】原来的 except 分支返回 (4, 360) —— 真源导入失败
        时 Agent 会宣称「音乐最长 360 秒」，而真实上限是 900 秒。

        【怎么真的触发降级】把 ``h3studio`` 包上的 ``music_yue2``
        属性换成一个**残缺模块**（没有 YUE2_MIN_DURATION）——
        这样 ``from . import music_yue2`` 不抛 ImportError，
        但取属性会抛 AttributeError ⇒ 走进 except 分支。
        """
        import types
        import h3studio
        saved_attr = h3studio.__dict__.get("music_yue2", "MISSING")
        h3studio.__dict__["music_yue2"] = types.ModuleType(
            "h3studio.music_yue2")
        try:
            lo, hi = agent_schema._music_duration_range()
        finally:
            if saved_attr == "MISSING":
                h3studio.__dict__.pop("music_yue2", None)
            else:
                h3studio.__dict__["music_yue2"] = saved_attr
        self.assertEqual((4, 900), (lo, hi),
                         "音乐时长兜底值与真源不一致（真源是 4~900）")

    def test_music_duration_fallback_is_not_the_old_360(self):
        """显式钉死「不是旧的 360」—— 变异测试 M4 要能咬到。"""
        import types
        import h3studio
        saved_attr = h3studio.__dict__.get("music_yue2", "MISSING")
        h3studio.__dict__["music_yue2"] = types.ModuleType(
            "h3studio.music_yue2")
        try:
            lo, hi = agent_schema._music_duration_range()
        finally:
            if saved_attr == "MISSING":
                h3studio.__dict__.pop("music_yue2", None)
            else:
                h3studio.__dict__["music_yue2"] = saved_attr
        self.assertNotEqual((4, 360), (lo, hi),
                            "兜底值又退回旧的 360 秒了")

    def test_video_duration_fallback_matches_real_truth(self):
        """视频时长兜底必须跟真源一致（2~60），不能写死别的数字。"""
        import sys
        saved = sys.modules.get("h3studio.video_models")
        sys.modules["h3studio.video_models"] = None
        try:
            from h3studio import video_models as vm
            lo, hi = vm.VIDEO_DURATION_MIN, vm.VIDEO_DURATION_MAX
        finally:
            if saved is not None:
                sys.modules["h3studio.video_models"] = saved
            else:
                sys.modules.pop("h3studio.video_models", None)
        self.assertEqual((2.0, 60.0), (float(lo), float(hi)))

    def test_agent_schema_duration_constants_derive_from_truth(self):
        """
        agent_schema.VIDEO_DURATION_MIN/MAX 必须**真的**从 video_models 取。

        【为什么这条能抓住缺陷】之前它们是字面量 2 / 60，注释却声称
        「从它取值」—— 真源改了这里不动 ⇒ 自述说谎。

        【为什么不能只断言「值相等」】字面量 2/60 和派生值 2/60
        **数值上就是一样的** —— 只断言相等根本抓不住「硬编码」这个缺陷
        （变异测试 M5 实测：改成硬编码后测试照样全绿 ⇒ 假绿）。

        正确做法：先把真源改成**别的值**，再**重新导入** agent_schema，
        看它的常量有没有跟着变。派生 ⇒ 跟着变；硬编码 ⇒ 不变 ⇒ 测试失败。
        """
        import importlib
        from h3studio import video_models as vm
        original = (vm.VIDEO_DURATION_MIN, vm.VIDEO_DURATION_MAX)
        try:
            vm.VIDEO_DURATION_MIN = 3.0
            vm.VIDEO_DURATION_MAX = 33.0
            reloaded = importlib.reload(agent_schema)
            self.assertEqual(3, reloaded.VIDEO_DURATION_MIN,
                             "agent_schema.VIDEO_DURATION_MIN 没跟着真源变 —— "
                             "它是硬编码的")
            self.assertEqual(33, reloaded.VIDEO_DURATION_MAX,
                             "agent_schema.VIDEO_DURATION_MAX 没跟着真源变 —— "
                             "它是硬编码的")
        finally:
            vm.VIDEO_DURATION_MIN, vm.VIDEO_DURATION_MAX = original
            importlib.reload(agent_schema)

    def test_capability_text_without_tiers_does_not_claim_old_whitelist(self):
        """
        档位清单为空时，能力文本里不能出现旧白名单（544p/768p）的**断言**。

        【为什么要精确匹配而不是 assertNotIn("768p")】
        加速 LoRA 清单里**合法地**含有 ``lightx2v_4step_768p`` /
        ``lightx2v_4step_sla_768p`` / ``lightx2v_sla_768p_root`` ——
        那是真源里的 LoRA 名字，不是分辨率档位。用裸子串判断会误杀。

        所以这里断言的是「**档位清单**这一段里不出现 544p/768p」，
        而不是「整个文本里不出现 768p」。
        """
        with mock.patch.object(cap, "video_size_tiers", return_value=[]):
            text = cap.video_capability_text()
        # 截出「短边档位 …」这一句
        match = re.search(r"短边档位[^。]*。", text)
        self.assertIsNotNone(match, "能力文本里找不到档位说明句")
        tier_sentence = match.group(0)
        self.assertNotIn("544p", tier_sentence)
        self.assertNotIn("768p", tier_sentence)
        # 并且必须明说「不可用」，让模型转去查工具
        self.assertIn("不可用", tier_sentence)
        # 整篇文本也不该出现「只有 544p / 768p」这种旧断言
        self.assertNotIn("只有 544p", text)
        self.assertNotIn("只有 768p", text)

    def test_real_capability_text_has_480p(self):
        """正常路径必须包含 480p（用户实测点名要支持的那个档位）。"""
        text = cap.video_capability_text()
        self.assertIn("480p", text)

    def test_mode_resolutions_video_includes_480p(self):
        """agent_schema 的视频档位集合必须含 480p。"""
        for mode in ("t2v", "flf", "r2v"):
            self.assertIn("480p", agent_schema.mode_resolutions(mode),
                          f"{mode} 的档位集合里没有 480p")

    def test_validate_params_accepts_480p(self):
        """480p 必须通过参数校验（不被当成非法值拦下）。"""
        for mode in ("t2v", "flf", "r2v"):
            issues = agent_schema.validate_params(mode, {"resolution": "480p"})
            errors = [i for i in issues
                      if getattr(i, "severity", "error") == "error"]
            self.assertEqual([], errors,
                             f"{mode} 把 480p 判为非法: {errors}")


# ==================================================================
# D. 能力问答必须给出具体数值（提示词约束）
# ==================================================================


class TestCapabilityQaMustBeConcrete(unittest.TestCase):
    """实测缺陷：模型回答「已核对完毕」但一个数字都没给。"""

    def test_prompt_forbids_empty_capability_answer(self):
        text = agent_mod.SYSTEM_PROMPT_TEMPLATE
        self.assertIn("必须给出具体数值", text)
        self.assertIn("禁止", text)

    def test_prompt_requires_media_chain_completion(self):
        """多步媒体处理要连着做完（用户要「一气呵成」）。"""
        text = agent_mod.SYSTEM_PROMPT_TEMPLATE
        self.assertIn("连着做完", text)

    def test_prompt_states_media_assets_only(self):
        """媒体后处理必须走素材库/生成库，不接受外部路径。"""
        text = agent_mod.SYSTEM_PROMPT_TEMPLATE
        self.assertIn("asset_id", text)
        self.assertIn("产物", text)


# ==================================================================
# E. 编排护栏（目标 D）
# ==================================================================


class TestOrchestrationGuardrails(unittest.TestCase):
    def test_max_steps_at_least_50(self):
        self.assertGreaterEqual(agent_mod.MAX_STEPS, 50)

    def test_total_timeout_present(self):
        self.assertGreater(agent_mod.TOTAL_TIMEOUT, 0)

    def test_repeat_and_no_progress_guards_present(self):
        self.assertGreaterEqual(agent_mod.REPEAT_LIMIT, 2)
        self.assertGreaterEqual(agent_mod.NO_PROGRESS_LIMIT, 2)


if __name__ == "__main__":
    unittest.main()
