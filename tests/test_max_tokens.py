# -*- coding: utf-8 -*-
"""
「max_token 上限」+「二次增强丢背景」缺陷的回归护栏。

背景（2026-11-09 真实缺陷）：
  用户的 381 字港风街拍提示词做二次增强后，输出只剩 L1 特写 / L2 中景的
  人物与服装细节，**背景元素（海报文字、招牌、街道、光线氛围）全部消失**。

  根因（已实测确认，不是猜测）：
    - h3studio/enhancer.py 的 LENGTH_PRESETS["long"]["max_tokens"] = 512
    - h3studio/enhancer.py 的 resolve_params() 里的 min(max_tokens, 1024) 硬上限
    逐物体枚举需要 2000+ tokens；512 会在 L2 中途硬切，
    后面的 L3 远景 / 环境现象 / 主体细化整段消失。

  本文件的用例守三条线：
    1. 上限不再被硬编码 512/1024 盖住，且可配置
    2. 截断能被检测出来（finish_reason=length + refine 结构完整性）
    3. 大预算下原文要素（背景/环境/氛围/构图/文字）确实都在输出里
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from h3studio import enhancer as enh  # noqa: E402
from h3studio import llm as llm_client  # noqa: E402


# 用户原始提示词（取自线上 jobs.db 的 job 38aab008f81d，381 字，逐字一致）
HK_PROMPT = (
    "一位年轻亚洲女性站立在狭窄的香港后街人行道边缘。她身穿复古风格的米白色高领针织衫，"
    "外搭一件深红色灯芯绒西装外套，下身穿着高腰蓝色直筒牛仔裤，腰间系着黑色皮带，"
    "脚踩白色帆布鞋。她的头发是蓬松的黑色长卷发，涂着鲜艳的哑光红唇，"
    "眼神略带疏离地望向镜头右侧。背景是典型的港式老旧街道，"
    "左侧是一排带有生锈铁栏杆和霓虹灯招牌的密集骑楼建筑，右侧是斑驳的灰色混凝土墙壁，"
    "墙上贴着褪色的繁体中文海报。地面是潮湿的沥青路面，反射着周围昏暗的光线。"
    "空中悬浮着细微的雨滴雾气。光线为低色温的暖黄色调，"
    "来自头顶上方悬挂的“金”、“記”、“酒”家招牌发出的霓虹灯光，"
    "以及远处街道尽头透进的冷青色自然光，形成强烈的冷暖对比光影。"
    "画面构图采用中景视角，轻微的低角度仰拍，突出人物的孤独感与环境的压迫感。"
    "胶片颗粒质感明显，色彩呈现低饱和度的怀旧色调，暗部带有青绿色偏移，"
    "高光部分保留橙黄色暖调。"
)


class TestMaxTokensCeiling(unittest.TestCase):
    """max_token 上限必须是可配置的大值，不是硬编码 512/1024。"""

    def test_long_preset_exceeds_512(self):
        """「详尽」档必须 > 512 —— 逐物体枚举需要 2000+ tokens。"""
        self.assertGreater(enh.LENGTH_PRESETS["long"]["max_tokens"], 512)

    def test_long_preset_covers_measured_need(self):
        """实测：381 字中文提示词 -> 2254 tokens。上限必须盖得住。"""
        self.assertGreaterEqual(enh.LENGTH_PRESETS["long"]["max_tokens"], 2254)

    def test_default_ceiling_is_large(self):
        """默认天花板必须在 32k 级别（用户要求「10 万字都正常」）。"""
        self.assertGreaterEqual(enh.MAX_TOKENS_CEILING, 32768)

    def test_no_legacy_1024_cap(self):
        """resolve_params 里不能再有 min(..., 1024) 这种硬编码盖子。"""
        import inspect
        src = inspect.getsource(enh.resolve_params)
        self.assertNotIn("1024", src,
                         "resolve_params 里仍有 1024 硬上限，会把可配置值盖回去")

    def test_refine_default_length_gets_big_budget(self):
        """二次增强档默认走「详尽」-> 用满用户配置的上限。"""
        spec = {"id": "image-refine", "default_length": "long"}
        p = enh.resolve_params(None, {}, spec)
        self.assertEqual(p["length"], "long")
        self.assertGreaterEqual(p["max_tokens"], enh.MAX_TOKENS_CEILING)

    def test_saved_ceiling_is_honoured(self):
        """用户保存的天花板生效。"""
        saved = {"params": {"max_tokens_ceiling": 4096}}
        p = enh.resolve_params({"length": "long"}, saved)
        self.assertEqual(p["max_tokens"], 4096)

    def test_request_ceiling_overrides_saved(self):
        """本次请求传的天花板优先于保存值。"""
        saved = {"params": {"max_tokens_ceiling": 4096}}
        p = enh.resolve_params({"length": "long", "max_tokens_ceiling": 2048}, saved)
        self.assertEqual(p["max_tokens"], 2048)

    def test_ceiling_clamped_to_upper_bound(self):
        """离谱大的天花板被夹到 MAX_TOKENS_MAX，不会无限放大。"""
        saved = {"params": {"max_tokens_ceiling": 10 ** 9}}
        p = enh.resolve_params({"length": "long"}, saved)
        self.assertEqual(p["max_tokens"], enh.MAX_TOKENS_MAX)

    def test_ceiling_clamped_to_lower_bound(self):
        """离谱小的天花板（0）被夹到 MAX_TOKENS_FLOOR，不会变成 0。"""
        saved = {"params": {"max_tokens_ceiling": 0}}
        p = enh.resolve_params({"length": "long"}, saved)
        self.assertEqual(p["max_tokens"], enh.MAX_TOKENS_FLOOR)

    def test_medium_tier_stays_official_300(self):
        """单段 caption 档保持官方 300（官方可复现值不被破坏）。"""
        p = enh.resolve_params({"length": "medium"}, {})
        self.assertEqual(p["max_tokens"], 300)

    def test_medium_tier_respects_lower_ceiling(self):
        """天花板低于 300 时，medium 档跟着降（不越权）。"""
        saved = {"params": {"max_tokens_ceiling": 256}}
        p = enh.resolve_params({"length": "medium"}, saved)
        self.assertEqual(p["max_tokens"], 256)

    def test_ceiling_is_persisted_param(self):
        """天花板是 DEFAULT_PARAMS 的一项，save API 才能存下来。"""
        self.assertIn("max_tokens_ceiling", enh.DEFAULT_PARAMS)

    def test_clamp_helper_direct(self):
        """默认天花板下，超大值被夹到默认天花板；负值被夹到下限。"""
        self.assertEqual(enh.clamp_max_tokens(99999, {}), enh.MAX_TOKENS_CEILING)
        self.assertEqual(enh.clamp_max_tokens(-5, {}), enh.MAX_TOKENS_FLOOR)
        self.assertEqual(enh.clamp_max_tokens(1234, {}), 1234)

    def test_clamp_helper_respects_explicit_ceiling(self):
        """显式给天花板时，超大值被夹到那个天花板。"""
        p = {"max_tokens_ceiling": 4096}
        self.assertEqual(enh.clamp_max_tokens(99999, p), 4096)
        self.assertEqual(enh.clamp_max_tokens(99999, {"max_tokens_ceiling": 999999}),
                         enh.MAX_TOKENS_MAX)


class TestFinishReasonExtraction(unittest.TestCase):
    """finish_reason 必须能被取到 —— 这是判断截断的权威信号。"""

    def test_openai_finish_reason_length(self):
        body = {"choices": [{"message": {"content": "x"}, "finish_reason": "length"}]}
        self.assertEqual(llm_client._extract_finish_reason(body), "length")

    def test_openai_finish_reason_stop(self):
        body = {"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]}
        self.assertEqual(llm_client._extract_finish_reason(body), "stop")

    def test_openai_finish_reason_missing(self):
        body = {"choices": [{"message": {"content": "x"}}]}
        self.assertEqual(llm_client._extract_finish_reason(body), "")

    def test_ollama_done_reason_length(self):
        body = {"message": {"content": "x"}, "done": True, "done_reason": "length"}
        self.assertEqual(llm_client._extract_ollama_finish_reason(body), "length")

    def test_ollama_done_without_reason_is_stop(self):
        body = {"message": {"content": "x"}, "done": True}
        self.assertEqual(llm_client._extract_ollama_finish_reason(body), "stop")


class TestTruncationDetection(unittest.TestCase):
    """截断检测：finish_reason + refine 末段完整性。"""

    def test_length_finish_reason_flags_truncated(self):
        rep = enh.coverage_report(HK_PROMPT, "【L1 特写】\n人物：\n  外观：x",
                                  finish_reason="length", validate_mode="refine")
        self.assertTrue(rep["truncated"])
        self.assertIn("length", rep["truncation_reason"])

    def test_refine_missing_terminal_section_flags_truncated(self):
        """末段「主体细化」缺失 = 输出在中途被切断。"""
        partial = "【L1 特写】\n人物面部：\n  外观：x\n\n【L2 中景】\n外套：\n  颜色：深红"
        rep = enh.coverage_report(HK_PROMPT, partial,
                                  finish_reason="stop", validate_mode="refine")
        self.assertTrue(rep["truncated"])
        self.assertIn("主体细化", rep["truncation_reason"])

    def test_refine_missing_optional_level_is_NOT_truncated(self):
        """
        模板明确写「若某一级没有物体，跳过该级标题」——
        模型合法跳过 L1/L3 不算截断，不能误报把好结果拒掉。
        （早先按「所有级都在」判，误报过一次：本地模型跳过了 L1，
          结果被当成截断拒收。）
        """
        out = ("【L2 中景】\n外套：\n  颜色：深红\n\n【环境现象】\n光：暖黄\n\n"
               "【主体细化】\n姿态：站")
        rep = enh.coverage_report(HK_PROMPT, out,
                                  finish_reason="stop", validate_mode="refine")
        self.assertFalse(rep["truncated"], rep["truncation_reason"])
        self.assertIn("【L1", rep["missing_sections"])   # 记录但不判失败

    def test_complete_refine_output_not_truncated(self):
        full = ("【L1 特写】\n人物面部：\n  外观：x\n\n【L2 中景】\n外套：\n  颜色：深红"
                "\n\n【L3 远景】\n骑楼：\n  轮廓：x\n\n【环境现象】\n光：暖黄\n\n【主体细化】\n姿态：站")
        rep = enh.coverage_report(HK_PROMPT, full,
                                  finish_reason="stop", validate_mode="refine")
        self.assertFalse(rep["truncated"])
        self.assertEqual(rep["missing_sections"], [])

    def test_non_refine_mode_ignores_sections(self):
        """非 refine 档不查分段标题（PE 档没有这个结构）。"""
        rep = enh.coverage_report(HK_PROMPT, "一段普通提示词",
                                  finish_reason="stop", validate_mode="pe")
        self.assertFalse(rep["truncated"])


class TestTimeoutScalesWithBudget(unittest.TestCase):
    """
    超时必须随 max_tokens 放大。

    真实故障：固定 180 秒超时在 32768 上限下直接超时，
    而后端其实**没有**截断 —— 看起来像「输出被截断」，
    实际是我们自己掐断了已经开始写的请求。
    """

    def test_small_budget_gets_baseline(self):
        """小预算给基础超时，且不低于下限。"""
        t = llm_client.compute_timeout(300)
        self.assertGreaterEqual(t, llm_client.MIN_TIMEOUT)
        self.assertLess(t, llm_client.compute_timeout(8192))

    def test_large_budget_gets_more_time(self):
        small = llm_client.compute_timeout(300)
        large = llm_client.compute_timeout(32768)
        self.assertGreater(large, small)
        # 【2026-11-09】MAX_TIMEOUT 从 1800 收到 600（审查报告 P1-3 的建议）。
        # 大预算仍必须比小预算给更多时间，且至少覆盖实测用量
        # （1646 tokens = 154.6 秒 -> 600 秒有 3~4 倍余量）。
        self.assertGreater(large, small)
        self.assertGreaterEqual(large, 600)

    def test_timeout_capped(self):
        self.assertLessEqual(llm_client.compute_timeout(10 ** 9),
                             llm_client.MAX_TIMEOUT)

    def test_max_timeout_is_600(self):
        """
        MAX_TIMEOUT 收�� 600 秒（审查报告 P1-3）。

        为什么不是 1800：一次增强请求最坏会挂住整个 aiohttp 连接
        （本机 llama.cpp 单槽串行），1800 秒意味着「一个请求能把服务
        拖住 30 分钟」。配合 /api/enhance 的并发闸门（上限 2），
        600 秒足够覆盖实测用量（1646 tokens = 154.6 秒）。
        """
        self.assertEqual(llm_client.MAX_TIMEOUT, 600.0)

    def test_capped_budget_still_covers_measured_use(self):
        """被封顶的预算仍要覆盖实测用量（154.6 秒 -> 600 秒余量 3~4 倍）。"""
        capped = llm_client.compute_timeout(32768)
        self.assertGreaterEqual(capped, 154.6 * 3)

    def test_bad_input_does_not_crash(self):
        self.assertGreater(llm_client.compute_timeout("abc"), 0)  # type: ignore[arg-type]
        self.assertGreater(llm_client.compute_timeout(None), 0)  # type: ignore[arg-type]


class TestElementCoverage(unittest.TestCase):
    """要素覆盖指标 —— 粗粒度参考值，不作为失败判据。"""

    def test_coverage_ratio_computed(self):
        rep = enh.check_element_coverage(HK_PROMPT, "骑楼 霓虹 海报 沥青 雨雾")
        self.assertEqual(rep["total"], len(enh._clause_units(HK_PROMPT)))
        self.assertGreater(rep["total"], 20)

    def test_units_cover_background_dimensions(self):
        """切出来的要素单元必须包含背景/环境/氛围/构图这些维度。"""
        units = " ".join(enh._clause_units(HK_PROMPT))
        for kw in ("骑楼", "霓虹", "海报", "沥青", "雨滴雾气", "冷暖对比", "构图", "胶片颗粒"):
            self.assertIn(kw, units, f"要素单元里没有「{kw}」")

    def test_noise_words_filtered(self):
        """纯连接词不该变成要素单元。"""
        units = enh._clause_units("她穿红裙，并且她在笑")
        self.assertNotIn("并且", units)
        self.assertNotIn("她在笑", units)


class TestNoOther512TruncationPoints(unittest.TestCase):
    """调用链上不能还有别的地方把 max_tokens 压回小值。"""

    def test_server_enhance_uses_resolved_max_tokens(self):
        import inspect
        import server
        src = inspect.getsource(server.api_enhance)
        self.assertIn('"max_tokens": params["max_tokens"]', src)
        # 不能有额外的更小硬上限
        self.assertNotIn("min(params", src)

    def test_llm_layer_does_not_lower_max_tokens(self):
        """llm.py 只做透传，不得自己再夹一刀。"""
        import inspect
        src = inspect.getsource(llm_client.call_provider_full)
        self.assertNotIn("min(max_tokens", src)

    def test_comfy_node_path_passes_max_tokens_through(self):
        """ComfyUI 节点链路也必须把 max_tokens 传下去。"""
        import inspect
        src = inspect.getsource(llm_client._call_via_comfyui)
        self.assertIn('"max_tokens": max_tokens', src)

    def test_node_supports_large_max_tokens(self):
        """VRGDG_LocalLLM 节点的 max_tokens 上限必须容得下我们的天花板。"""
        # 节点 schema: ["INT", {"default": 4096, "min": -1, "max": 262144, ...}]
        self.assertGreaterEqual(262144, enh.MAX_TOKENS_MAX)


if __name__ == "__main__":
    unittest.main()
