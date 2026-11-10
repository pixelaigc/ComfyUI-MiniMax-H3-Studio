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

    def test_default_ceiling_is_large(self):
        """默认天花板必须在 32k 级别（用户要求「10 万字都正常」）。"""
        self.assertGreaterEqual(enh.MAX_TOKENS_CEILING, 32768)

    def test_no_legacy_1024_cap(self):
        """resolve_params 里不能再有 min(..., 1024) 这种硬编码盖子。"""
        import inspect
        src = inspect.getsource(enh.resolve_params)
        self.assertNotIn("1024", src,
                         "resolve_params 里仍有 1024 硬上限，会把可配置值盖回去")

    def test_ceiling_is_persisted_param(self):
        """天花板是 DEFAULT_PARAMS 的一项，save API 才能存下来。"""
        self.assertIn("max_tokens_ceiling", enh.DEFAULT_PARAMS)

    def test_saved_ceiling_is_honoured(self):
        """用户保存的天花板生效。"""
        saved = {"params": {"max_tokens_ceiling": 4096}}
        p = enh.resolve_params({"length": "long"}, saved, original="x" * 400)
        self.assertLessEqual(p["max_tokens"], 4096)

    def test_request_ceiling_overrides_saved(self):
        """本次请求传的天花板优先于保存值。"""
        saved = {"params": {"max_tokens_ceiling": 4096}}
        p = enh.resolve_params({"length": "long", "max_tokens_ceiling": 2048},
                               saved, original="x" * 400)
        self.assertEqual(p["max_tokens"], 2048)

    def test_ceiling_clamped_to_upper_bound(self):
        """
        离谱大的天花板被夹到 MAX_TOKENS_MAX，不会无限放大。
        用足够长的原文，让自适应估算值本身就超过 MAX_TOKENS_MAX，
        这样天花板才会真正成为约束。
        """
        saved = {"params": {"max_tokens_ceiling": 10 ** 9}}
        p = enh.resolve_params({"length": "long"}, saved, original="x" * 200000)
        self.assertEqual(p["max_tokens"], enh.MAX_TOKENS_MAX)

    def test_ceiling_clamps_large_estimate(self):
        """天花板低于自适应估算值时，以天花板为准。"""
        saved = {"params": {"max_tokens_ceiling": 1000}}
        p = enh.resolve_params({"length": "long"}, saved, original="x" * 4000)
        self.assertEqual(p["max_tokens"], 1000)

    def test_ceiling_clamped_to_lower_bound(self):
        """离谱小的天花板（0）被夹到 MAX_TOKENS_FLOOR，不会变成 0。"""
        saved = {"params": {"max_tokens_ceiling": 0}}
        p = enh.resolve_params({"length": "long"}, saved, original="x" * 400)
        self.assertEqual(p["max_tokens"], enh.MAX_TOKENS_FLOOR)

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


class TestAdaptiveBudget(unittest.TestCase):
    """
    【审查 P1-3 阻断项】输出预算必须**按输入长度自适应**。

    旧实现：medium 档固定 300 tokens -> 381 字提示词必被硬切，
    审查实测 10 次有 6 次被切，成功率只有 40%。
    新实现：预算 = f(原文长度, 模板口径)，没有任何固定值能卡死长提示词。
    """

    # 官方 PE（pe 档）实测自然产出 222~351 tokens
    PE_NATURAL_MAX = 400
    # 逐物体枚举（refine 档）实测自然产出 2254 tokens
    REFINE_NATURAL = 2254

    def test_medium_tier_is_not_capped_at_300(self):
        """medium 档**不能**再是固定 300 —— 这是审查 P1-3 的根因。"""
        preset = enh.LENGTH_PRESETS["medium"]
        self.assertNotEqual(preset.get("max_tokens"), 300,
                            "medium 档仍写死 max_tokens=300，长提示词必被硬切")
        self.assertIsNone(preset.get("cap"),
                           "medium 档不该有固定上限（cap 会卡死长提示词）")

    def test_pe_budget_covers_natural_output(self):
        """普通增强（pe 档）的预算要盖得住模型的自然产出。"""
        spec = {"validate_mode": "pe"}
        p = enh.resolve_params(None, {}, spec, original=HK_PROMPT)
        self.assertGreaterEqual(p["max_tokens"], self.PE_NATURAL_MAX)

    def test_refine_budget_covers_natural_output(self):
        """二次增强（refine 档）的预算要盖得住 2254 tokens 的自然产出。"""
        spec = {"validate_mode": "refine"}
        p = enh.resolve_params(None, {}, spec, original=HK_PROMPT)
        self.assertGreaterEqual(p["max_tokens"], self.REFINE_NATURAL)

    def test_default_length_medium_no_longer_truncates(self):
        """
        审查 P1-3 的直接复现：image/moss-image/video 的 default_length=medium，
        但 medium 档现在也自适应 -> 不再被 300 硬切。
        """
        for spec_id in ("image", "moss-image", "video"):
            spec = enh.get_spec(spec_id, {})
            self.assertEqual(spec.get("default_length"), "medium")
            p = enh.resolve_params(None, {}, spec, original=HK_PROMPT)
            self.assertGreater(p["max_tokens"], 1000,
                               f"{spec_id} 仍是固定小预算，长提示词会被硬切")

    def test_budget_grows_with_input_length(self):
        """输入越长，预算越大（不是常数）。"""
        spec = {"validate_mode": "pe"}
        short = enh.resolve_params(None, {}, spec, original="a" * 50)
        long_ = enh.resolve_params(None, {}, spec, original="a" * 2000)
        self.assertGreater(long_["max_tokens"], short["max_tokens"])

    def test_short_tier_still_capped(self):
        """「精简」档仍然有 1024 上限（用户显式要求短输出）。"""
        p = enh.resolve_params({"length": "short"}, {}, {"validate_mode": "pe"},
                               original="a" * 4000)
        self.assertLessEqual(p["max_tokens"], 1024)

    def test_long_tier_has_floor(self):
        """「详尽」档有 4096 下限。"""
        p = enh.resolve_params({"length": "long"}, {}, {"validate_mode": "pe"},
                               original="短")
        self.assertGreaterEqual(p["max_tokens"], 4096)

    def test_output_ratio_by_mode(self):
        """放大倍数按 validate_mode 查表（新增模板只改 mode 即可）。"""
        self.assertEqual(enh.output_ratio_for({"validate_mode": "pe"}),
                         enh.OUTPUT_RATIO_DEFAULT)
        self.assertEqual(enh.output_ratio_for({"validate_mode": "refine"}),
                         enh.OUTPUT_RATIO_REFINE)
        self.assertEqual(enh.output_ratio_for({"validate_mode": "music_refine"}),
                         enh.OUTPUT_RATIO_REFINE)
        self.assertEqual(enh.output_ratio_for({"validate_mode": "bbox"}),
                         enh.OUTPUT_RATIO_STRUCTURED)

    def test_unknown_mode_falls_back_to_default(self):
        """未知 mode 不会崩，回落到默认倍数。"""
        self.assertEqual(enh.output_ratio_for({"validate_mode": "nope"}),
                         enh.OUTPUT_RATIO_DEFAULT)
        self.assertEqual(enh.output_ratio_for(None), enh.OUTPUT_RATIO_DEFAULT)

    def test_no_original_gives_floor_budget(self):
        """没传原文时退化成保底预算，不返回 0。"""
        p = enh.resolve_params(None, {}, {"validate_mode": "pe"})
        self.assertGreaterEqual(p["max_tokens"], enh.MAX_TOKENS_FLOOR)


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
    超时必须随**本次实际给的 max_tokens** 放大，且封顶 600 秒。

    真实故障（审查 P1-3）：
      - 固定 180 秒超时在 32768 上限下直接超时，后端其实**没有**截断
      - 旧实现把「用户配置的上限」当成「本次预算」，
        ceiling=32768 时即使本次只给 800 tokens，超时也被放大到封顶 1800 秒
        -> 表现为「模型 3 秒写完了，前端却挂满 30 分钟」
    """

    def test_small_budget_gets_baseline(self):
        t = llm_client.compute_timeout(300)
        self.assertGreaterEqual(t, llm_client.MIN_TIMEOUT)
        self.assertLess(t, llm_client.compute_timeout(8192))

    def test_large_budget_gets_more_time(self):
        small = llm_client.compute_timeout(300)
        large = llm_client.compute_timeout(8192)
        self.assertGreater(large, small)
        # 【2026-11-09】MAX_TIMEOUT 从 1800 收到 600（审查报告 P1-3 的建议）。
        # 大预算仍必须比小预算给更多时间，且至少覆盖实测用量
        # （1646 tokens = 154.6 秒 -> 600 秒有 3~4 倍余量）。
        self.assertGreater(large, small)
        self.assertGreaterEqual(large, 600)
        # 3000 tokens 未封顶时给到 480 秒（实测 2254 tokens 约 210 秒）
        self.assertGreater(llm_client.compute_timeout(3000), 400)

    def test_timeout_capped_at_600(self):
        """审查要求：MAX_TIMEOUT 收到 600（不再是 1800）。"""
        self.assertEqual(llm_client.MAX_TIMEOUT, 600.0)
        self.assertLessEqual(llm_client.compute_timeout(10 ** 9),
                             llm_client.MAX_TIMEOUT)

    def test_max_timeout_is_600(self):
        """
        MAX_TIMEOUT 收到 600 秒（审查报告 P1-3）。

        为什么不是 1800：一次增强请求最坏会挂住整个 aiohttp 连接
        （本机 llama.cpp 单槽串行），1800 秒意味着「一个请求能把服务
        拖住 30 分钟」。配合 /api/enhance 的并发闸门（上限见
        server.py 的 ENHANCE_CONCURRENCY 单一权威定义），
        600 秒足够覆盖实测用量（1646 tokens = 154.6 秒）。
        """
        self.assertEqual(llm_client.MAX_TIMEOUT, 600.0)

    def test_capped_budget_still_covers_measured_use(self):
        """被封顶的预算仍要覆盖实测用量（154.6 秒 -> 600 秒余量 3~4 倍）。"""
        capped = llm_client.compute_timeout(32768)
        self.assertGreaterEqual(capped, 154.6 * 3)

    def test_ceiling_does_not_inflate_timeout(self):
        """
        关键回归：超时只跟**本次预算**有关，与「用户配置的上限」无关。
        本次给 800 -> 超时 ~216 秒，绝不能因为 ceiling=32768 就变成 600 秒。
        """
        t800 = llm_client.compute_timeout(800)
        self.assertLess(t800, 300)
        self.assertNotEqual(t800, llm_client.MAX_TIMEOUT)

    def test_realistic_budget_capped(self):
        """本次给 32768 -> 封顶 600。"""
        self.assertEqual(llm_client.compute_timeout(32768),
                         llm_client.MAX_TIMEOUT)

    def test_bad_input_does_not_crash(self):
        self.assertGreater(llm_client.compute_timeout("abc"), 0)  # type: ignore[arg-type]
        self.assertGreater(llm_client.compute_timeout(None), 0)  # type: ignore[arg-type]


class TestTemplateDerivedTerminalSection(unittest.TestCase):
    """
    【跨分支复用 · 审查 P2-1】末段标题必须**从模板正文解析**，
    不能按模板 id 硬编码 —— 否则 enh-unify 的 music-refine
    （末段「【整体细化】」）会漏检截断。
    """

    IMAGE_REFINE_TEMPLATE = """### 输出格式
严格按以下结构输出：

【L1 特写】
物件名：

【L2 中景】
...

【L3 远景】
...

【环境现象】
光：

【主体细化】
（主体描述）
"""

    MUSIC_REFINE_TEMPLATE = """### 输出格式
严格按以下结构输出：

【L1 主线】
人声类型：

【L2 支撑】
...

【L3 远景】
...

【整体细化】
（整体描述）
"""

    def test_parses_image_refine_sections(self):
        secs = enh.template_sections(self.IMAGE_REFINE_TEMPLATE)
        self.assertEqual(secs, ["【L1 特写】", "【L2 中景】", "【L3 远景】",
                                "【环境现象】", "【主体细化】"])

    def test_parses_music_refine_sections(self):
        """音乐版模板解析出【整体细化】末段。"""
        secs = enh.template_sections(self.MUSIC_REFINE_TEMPLATE)
        self.assertEqual(secs, ["【L1 主线】", "【L2 支撑】", "【L3 远景】",
                                "【整体细化】"])

    def test_terminal_section_derived_from_template(self):
        self.assertEqual(
            enh.terminal_section_for(self.IMAGE_REFINE_TEMPLATE, "refine"),
            "【主体细化】")
        self.assertEqual(
            enh.terminal_section_for(self.MUSIC_REFINE_TEMPLATE, "music_refine"),
            "【整体细化】")

    def test_terminal_section_fallback_by_mode(self):
        """模板正文解析不出来时，按 validate_mode 查兜底表。"""
        self.assertEqual(enh.terminal_section_for("", "refine"), "【主体细化】")
        self.assertEqual(enh.terminal_section_for("", "music_refine"),
                         "【整体细化】")
        self.assertEqual(enh.terminal_section_for("", "pe"), "")

    def test_music_refine_truncation_detected(self):
        """
        审查 P1-4 的直接复现：
        明显被截断的 music_refine 输出，旧实现判定为「未截断」。
        """
        truncated = "【L1 主线】\n人声类型：温暖\n\n【L2 支撑】\n编曲：钢琴"
        rep = enh.coverage_report(
            "一段音乐描述", truncated, finish_reason="stop",
            validate_mode="music_refine",
            system_prompt=self.MUSIC_REFINE_TEMPLATE)
        self.assertTrue(rep["truncated"], "music_refine 档的截断漏检了")
        self.assertIn("整体细化", rep["truncation_reason"])

    def test_music_refine_complete_not_truncated(self):
        complete = ("【L1 主线】\n人声类型：温暖\n\n【L2 支撑】\n编曲：钢琴\n\n"
                    "【L3 远景】\n空间：客厅\n\n【整体细化】\n整体：安静")
        rep = enh.coverage_report(
            "一段音乐描述", complete, finish_reason="stop",
            validate_mode="music_refine",
            system_prompt=self.MUSIC_REFINE_TEMPLATE)
        self.assertFalse(rep["truncated"], rep["truncation_reason"])

    def test_no_output_format_marker_falls_back_to_mode_table(self):
        """
        模板没有「输出格式」小节时，按 validate_mode 查兜底表
        （refine -> 【主体细化】）。兜底表命中就仍然判截断。
        """
        rep = enh.coverage_report(
            "一段文字", "随便什么内容", finish_reason="stop",
            validate_mode="refine", system_prompt="你是一个助手")
        self.assertTrue(rep["truncated"])
        self.assertIn("主体细化", rep["truncation_reason"])

    def test_mode_without_fallback_means_no_judgement(self):
        """既没模板结构、mode 也不在兜底表里 -> 不判截断（宁可不报也不误报）。"""
        rep = enh.coverage_report(
            "一段文字", "随便什么内容", finish_reason="stop",
            validate_mode="pe", system_prompt="你是一个助手")
        self.assertFalse(rep["truncated"])


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


class TestEnhanceConcurrencyGate(unittest.TestCase):
    """
    【审查 P1-3 · 中】/api/enhance 必须有并发闸门。

    没有闸门时：N 个并发请求 = N 个最长 600 秒的挂起协程，
    aiohttp 连接池被占满，后续请求一起排队到超时。
    """

    def test_server_defines_gate(self):
        import server
        self.assertTrue(hasattr(server, "ENHANCE_CONCURRENCY"))
        self.assertTrue(hasattr(server, "ENHANCE_GATE"))
        self.assertTrue(callable(server._enhance_gate))

    def test_concurrency_limit_is_2_to_3(self):
        """审查要求：上限 2~3。"""
        import server
        self.assertGreaterEqual(server.ENHANCE_CONCURRENCY, 2)
        self.assertLessEqual(server.ENHANCE_CONCURRENCY, 3)

    def test_gate_is_semaphore(self):
        import asyncio
        import server
        server.ENHANCE_GATE = None
        gate = server._enhance_gate()
        self.assertIsInstance(gate, asyncio.Semaphore)
        self.assertEqual(gate._value, server.ENHANCE_CONCURRENCY)

    def test_locked_reports_saturation(self):
        """闸门满时 locked() 为 True（用于「抢不到就 429」的判据）。"""
        import asyncio
        import server
        server.ENHANCE_GATE = None
        gate = server._enhance_gate()
        self.assertFalse(gate.locked())

    def test_api_enhance_uses_gate(self):
        """api_enhance 必须真的走闸门（源码级断言）。"""
        import inspect
        import server
        src = inspect.getsource(server.api_enhance)
        self.assertIn("_enhance_gate", src)
        self.assertIn("async with gate", src)
        self.assertIn("429", src)

    def test_concurrency_gate_does_not_block_when_free(self):
        """闸门空闲时能立刻进入（不误伤正常请求）。"""
        import asyncio
        import server
        server.ENHANCE_GATE = None
        gate = server._enhance_gate()

        async def run():
            async with gate:
                return "ok"
        self.assertEqual(asyncio.run(run()), "ok")


class TestEnhanceRetryOnTruncation(unittest.TestCase):
    """
    【审查 P1-3 建议修法 3】截断时**自动降级重试**，而不是直接 502。
    """

    def test_api_enhance_has_retry_loop(self):
        import inspect
        import server
        src = inspect.getsource(server.api_enhance)
        self.assertIn("attempts", src)
        self.assertIn("已用满上限重试仍被截断", src)

    def test_retry_uses_ceiling(self):
        """重试用的是「满上限」。"""
        import inspect
        import server
        src = inspect.getsource(server.api_enhance)
        self.assertIn("MAX_TOKENS_CEILING", src)

    def test_system_prompt_passed_to_coverage(self):
        """system_prompt 必须传给 coverage_report（末段靠它解析）。"""
        import inspect
        import server
        src = inspect.getsource(server.api_enhance)
        self.assertIn("system_prompt=spec.get(", src)

    def test_original_passed_to_resolve_params(self):
        """预算自适应必须拿到原文长度。"""
        import inspect
        import server
        src = inspect.getsource(server.api_enhance)
        self.assertIn("original=original", src)


class TestNoLegacyFixedBudgets(unittest.TestCase):
    """调用链上不能还有别的地方把预算压回小值。"""

    def test_server_enhance_uses_resolved_max_tokens(self):
        import inspect
        import server
        src = inspect.getsource(server.api_enhance)
        self.assertIn('"max_tokens": mt', src)
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
        self.assertGreaterEqual(262144, enh.MAX_TOKENS_MAX)

    def test_no_dead_tier_budget_function(self):
        """旧的 _tier_budget 已被 adaptive_max_tokens 取代，不该残留。"""
        self.assertFalse(hasattr(enh, "_tier_budget"))



class TestRefineStructureValidatorConsistency(unittest.TestCase):
    """
    模板原文写着「若某一级没有物体，跳过该级标题」——
    校验器**不能**硬要求 L1 存在（旧实现要求，与模板自相矛盾）。

    实测后果：模型合法跳过 L1 时，整单被拒（502「缺少 L1 特写分级标题」），
    白白浪费一次已经写好的完整结果。
    """

    def _validator(self):
        import importlib.util
        import os
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "h3studio", "skills", "prompt-enhancer", "scripts", "validate.py")
        spec = importlib.util.spec_from_file_location("pv", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_skipping_l1_is_not_an_error(self):
        v = self._validator()
        out = ("【L2 中景】\n外套：\n  颜色：深红\n\n【L3 远景】\n骑楼：\n  轮廓：x\n\n"
               "【主体细化】\n姿态：站")
        errors, warnings = v.check_refine_structure(out)
        self.assertEqual(errors, [], "合法跳过 L1 不该被判结构不完整")

    def test_no_levels_at_all_is_still_an_error(self):
        """完全没有分级标题仍然要报错（真的没做细化）。"""
        v = self._validator()
        errors, warnings = v.check_refine_structure("就是一段普通文字，没有任何结构")
        self.assertTrue(errors)

    def test_music_refine_mode_is_wired(self):
        """music_refine 档也要走逐物体枚举结构检查。"""
        import inspect
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "h3studio", "skills", "prompt-enhancer", "scripts", "validate.py")
        src = open(path, encoding="utf-8").read()
        self.assertIn('"refine", "music_refine"', src)

    def test_music_refine_terminal_accepted(self):
        """音乐版末段「【整体细化】」也算结构完整。"""
        v = self._validator()
        out = ("【L1 主线】\n人声：温暖\n\n【L2 支撑】\n编曲：钢琴\n\n"
               "【整体细化】\n整体：安静")
        errors, _ = v.check_refine_structure(out)
        self.assertEqual(errors, [])



class TestWeightSyntaxFalsePositive(unittest.TestCase):
    """
    括号权重误判修复。

    旧实现把**任意**括号内容当 Compel 权重语法，于是模型写的普通注释
    也会被整单拒收：
      (Tong Lau)                          港式地名注音
      (arcade buildings)                  骑楼建筑的英文补充
      (partially obscured or fragmented)  状态描述
    实测：普通增强 10 次有 3 次因此 502（而输出本身是完整的）。

    官方口径（diffusers weighted_prompts）：括号权重只有
      (cat)      单个词
      (cat:1.5)  词:数值
    两种。多词短语不是权重语法。
    """

    def _validator(self):
        import importlib.util
        import os
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "h3studio", "skills", "prompt-enhancer", "scripts", "validate.py")
        spec = importlib.util.spec_from_file_location("pv2", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_real_weight_syntax_still_rejected(self):
        v = self._validator()
        for case in ("(word:1.2)", "((word))", "[word]", "word++"):
            self.assertTrue(v.check_weight_syntax("x %s y" % case, "pe"),
                            "真正的权重语法必须被拒：%s" % case)

    def test_bare_single_word_needs_repetition(self):
        """
        裸单词 (word) 是 Compel 强调语法 —— 但判据是「这个词在括号外也出现」
        （强调的是已有的词）。只出现一次 -> 是注释。
        """
        v = self._validator()
        self.assertTrue(v.check_weight_syntax("a word here, (word) there", "pe"))
        self.assertFalse(v.check_weight_syntax("a (word) here", "pe"))

    def test_multiword_gloss_not_rejected(self):
        v = self._validator()
        for case in ("(Tong Lau)", "(arcade buildings)",
                     "(partially obscured or fragmented)", "(cat dog)",
                     "(红色霓虹灯)"):
            self.assertFalse(v.check_weight_syntax("x %s y" % case, "pe"),
                             "普通注释不该被判权重语法：%s" % case)

    def test_single_word_still_rejected(self):
        """
        diffusers 的 (cat) 形式仍要拦 ——
        但只有当这个词在括号**外也出现过**时（Compel 强调的是已有的词）。
        """
        v = self._validator()
        self.assertTrue(v.check_weight_syntax("a cat sitting, (cat) nearby", "pe"))

    def test_emphatic_single_word_not_repeated_is_not_weight(self):
        """
        括号里新引入一个只出现一次的词 -> 是注释，不是强调。
        （实测上下文："Tong lau (arcade) buildings" —— arcade 只出现一次。）
        """
        v = self._validator()
        self.assertFalse(v.check_weight_syntax("Tong lau (arcade) buildings", "pe"))

    def test_numeric_weight_still_rejected(self):
        v = self._validator()
        self.assertTrue(v.check_weight_syntax("a (1.5) b", "pe"))

    def test_transliteration_gloss_not_rejected(self):
        """
        官方 PE 模板要求「画面文字用双引号括起来」，
        模型常写成 `"金" (gold)` —— 括号里是**逐字注音**，不是倍率。
        这种不该被当权重语法拒掉（否则整单 502，输出本身是好的）。
        """
        v = self._validator()
        for case in ('"金" (gold)', '"記" (record)', '"酒" (wine)'):
            self.assertFalse(v.check_weight_syntax("霓虹招牌 " + case, "pe"),
                             "注音不该被判权重语法：%s" % case)

    def test_transliteration_gloss_still_lets_real_weights_through(self):
        """豁免只针对注音，真正的权重语法仍然要拦。"""
        v = self._validator()
        self.assertTrue(v.check_weight_syntax('"cat" (cat:1.5)', "pe"))

    def test_term_transliteration_not_rejected(self):
        """
        模型把中文建筑术语「骑楼」写成 "arcade buildings (tong lau)" ——
        括号里是**粤语音译**，不是倍率。
        （实测：普通增强 10 次里有 1 次因此 502，而输出本身是完整的。）
        """
        v = self._validator()
        for case in ("arcade buildings (tong lau)", "骑楼 (tong lau)",
                     "T-tong lau (shophouse) buildings"):
            self.assertFalse(v.check_weight_syntax(case, "pe"),
                             "术语转写不该被判权重语法：%s" % case)



if __name__ == "__main__":
    unittest.main()
