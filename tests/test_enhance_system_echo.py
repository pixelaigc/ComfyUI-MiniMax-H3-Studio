# -*- coding: utf-8 -*-
"""
「文生视频面板增强按钮报错」缺陷的回归护栏。

用户实测现象（2026-11-11，8190 线上）：
  面板=文生视频（minimax-h3），点提示词框旁的「✦ 增强」→ 弹窗：
    「提示词增强失败（本机 Qwen 多模态）
      Provider『本机 Qwen 多模态』返回的是系统提示词复读，不是有效的提示词。
      该模型可能太小或未按指令输出，请换用更强的模型。」

实测根因（**两个**，都不是模型能力不足 —— 本机 qwen3.8-flash-next-iq3_xxs
直连 /v1/chat/completions 返回的是完全正确的三段式导演稿）：

  根因 1 ——「系统提示词复读」判据假阳性
    enhancer.looks_like_echo() 的旧判据是「输出与 system prompt 存在
    >= 40 字符的公共子块」。但 video-director 模板正文里有一段
    **输出格式骨架**（``` 围栏包着）：
        integrated_multimodal_description: [Shot 1] ...
        overall_soundscape: ...
        non_diegetic_music: ...
    模型**忠实按格式输出**时必然出现这几个字段名 → 公共子块天然
    44 字符 >= 40 → **完全正确的输出被判成复读** → 整单 502。

  根因 2 —— 校验器把 H3 官方结构化标记当成 Compel 权重语法
    validate.py 的 [word] 权重语法拒收规则是**给 Z-Image 图像档**定的。
    但 H3 官方规范**要求**写 [Shot 1]（镜头编号）与 [English]（台词语言标签）
    —— 模型按格式输出就必然带 → 整单 502，报「出现提示词权重语法」。

本文件守的契约：
  A. 忠实按格式输出的合法产物**不得**被判成系统提示词复读
  B. 真正的复读（模型原样吐回 system prompt）**必须**仍被抓住
  C. H3 官方结构化标记**不得**被判成权重语法；其余 [word] 仍然拒收
  D. 输出解析要剥掉：思考块 / Markdown 围栏 / 标签前缀 / 角色前缀
  E. 复读判定失败时**不得**把系统提示词原文写回提示词框
  F. 其它共用同一链路的模板档（refine / image / yue2）共享同一缺陷时一并修好

运行：python3 -m pytest tests/test_enhance_system_echo.py -v
"""
import importlib.util
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from h3studio import enhancer as enh  # noqa: E402


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_validator():
    path = os.path.join(REPO, "h3studio", "skills", "prompt-enhancer",
                        "scripts", "validate.py")
    spec = importlib.util.spec_from_file_location("echo_validator", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------
# 真实模型产物样本（本机 qwen3.8-flash-next-iq3_xxs，video-director 档）
#
# 这是**真实 HTTP 返回**，不是手编的假数据 —— 它证明「模型输出完全正确，
# 却被旧判据判成复读」。
# ------------------------------------------------------------------

# 第一次采样（max_tokens=1024）的完整产物
REAL_OUTPUT_1024 = """integrated_multimodal_description: [Shot 1] A cinematic, noir-inspired night scene with heavy rain falling on a wet asphalt street illuminated by dense neon signs in pink, cyan, and purple hues. A young woman walks slowly down the center of the sidewalk, holding a bright red umbrella that contrasts sharply with the cool-toned lighting. She wears a dark trench coat and boots, splashing slightly as she steps through puddles. Neon reflections shimmer on the wet pavement around her feet. The camera performs a slow tracking shot, moving backward at a steady pace to keep her centered in the frame while capturing the depth of the street behind her. Raindrops hit the surface of the red umbrella with visible impact. She keeps her head slightly lowered, looking ahead down the road.

overall_soundscape: Continuous heavy rainfall creates a constant hiss and splashing sound against the pavement. The distinct rhythmic patter of raindrops striking the fabric of the red umbrella is audible and dominant in the foreground, accompanied by the wet squelching sounds of boots stepping into puddles.

non_diegetic_music: A slow, melancholic synthwave track plays softly in the background, featuring a deep bassline and sparse, echoing synthesizer pads that build tension gradually."""

# 第二次采样（max_tokens=4096）的完整产物 —— 带 [Shot 2] 与 <d> 台词
REAL_OUTPUT_4096 = """integrated_multimodal_description: [Shot 1] A cinematic, low-angle tracking shot follows a young woman walking away from the camera down a wet, rain-slicked urban street at night. The street is illuminated by vibrant neon signs in pink and cyan reflecting on the puddles. She holds a bright red umbrella over her head, shielding herself from the steady rain. Her coat is dark and slightly damp. The camera tracks forward with small amplitude at slow speed, maintaining a consistent distance behind her as she walks through the glowing reflections. Raindrops fall visibly in the foreground, blurring the light sources.

[Shot 2] At 00:05.000, the shot cuts to a close-up profile view of the woman's face as she continues walking. Her expression is neutral and focused ahead. Water droplets run down the edge of the red umbrella canopy. Neon lights from the background bokeh create colorful halos around her silhouette. The camera pans slightly right with small amplitude at slow speed, tracking her movement through the rain streaks.

overall_soundscape: Continuous steady rainfall hitting pavement and metal surfaces creates a consistent white noise backdrop. The rhythmic drip of water falling fro"""

# 用户截图里的原始提示词（逐字）
USER_PROMPT = "电影。港风。90年代香港港口，亚洲年轻女性休闲散步"


class TestEchoDetectionNoFalsePositive(unittest.TestCase):
    """契约 A：忠实按格式输出的合法产物不得被判成复读"""

    def setUp(self):
        self.spec = enh.get_spec("video-director")
        self.sysp = self.spec["system_prompt"]

    def test_real_model_output_is_not_echo(self):
        """真实模型产物（1024 采样）不得被判成复读。"""
        parsed = enh.strip_wrapping(enh.strip_thinking(REAL_OUTPUT_1024))
        self.assertFalse(
            enh.looks_like_echo(parsed, self.sysp),
            "模型按格式输出的合法导演稿被判成系统提示词复读（假阳性）")

    def test_real_model_output_with_two_shots_is_not_echo(self):
        """带 [Shot 2] 的采样（公共子块更长）同样不得被误判。"""
        parsed = enh.strip_wrapping(enh.strip_thinking(REAL_OUTPUT_4096))
        self.assertFalse(
            enh.looks_like_echo(parsed, self.sysp),
            "带多镜头的合法导演稿被判成系统提示词复读（假阳性）")

    def test_old_threshold_would_have_been_a_false_positive(self):
        """
        【护栏 · 反向断言】确认旧判据确实会误判 ——
        否则这条测试就变成「永远绿」的假护栏。
        """
        parsed = enh.strip_wrapping(enh.strip_thinking(REAL_OUTPUT_1024))
        import difflib
        matcher = difflib.SequenceMatcher(None, self.sysp, parsed, autojunk=False)
        old_block = matcher.find_longest_match(
            0, len(self.sysp), 0, len(parsed)).size
        self.assertGreaterEqual(
            old_block, 40,
            "旧判据（>=40 字符公共子块）本该在此误判；"
            "若不成立说明复现样本变了，测试需要重新校准")
        # 但新判据必须放行
        self.assertFalse(enh.looks_like_echo(parsed, self.sysp))

    def test_template_skeleton_is_not_counted_as_echo_signal(self):
        """模板里的输出骨架不参与复读比对（否则字段名必然误伤）。"""
        prose = enh._instruction_prose(self.sysp)
        self.assertNotIn("integrated_multimodal_description:", prose,
                         "输出骨架的字段名不该出现在指令散文里")
        self.assertLess(len(prose), len(self.sysp),
                         "指令散文应比模板正文短（骨架被剥掉了）")
        # 但散文本身仍在（真正的复读要靠它判定）
        self.assertIn("视频导演", prose)

    def test_refine_template_faithful_output_is_not_echo(self):
        """
        契约 F：refine 档（image-refine / music-refine）模板正文里
        也有 ``` 包着的**输出格式骨架**（【L1 特写】/【L2 中景】…），
        模型忠实照抄同样会被旧判据误杀 —— 必须一并修好。
        """
        for spec_id in ("image-refine", "music-refine"):
            spec = enh.get_spec(spec_id)
            sp = spec["system_prompt"]
            fences = re.findall(r"```(.*?)```", sp, re.S)
            self.assertTrue(fences, f"{spec_id} 模板应含输出骨架")
            faithful = fences[0].replace("...", "").strip() + "\n\n物件名：一只旧钢笔\n"
            parsed = enh.strip_wrapping(faithful)
            self.assertFalse(
                enh.looks_like_echo(parsed, sp),
                f"{spec_id} 忠实照抄输出骨架的产物被判成复读（假阳性）")


class TestEchoDetectionStillCatchesRealEcho(unittest.TestCase):
    """契约 B：真正的复读必须仍然被抓住（不能把护栏改成放行一切）"""

    def setUp(self):
        self.spec = enh.get_spec("video-director")
        self.sysp = self.spec["system_prompt"]

    def test_full_system_prompt_echo_is_detected(self):
        """模型原样吐回整段 system prompt —— 必须判成复读。"""
        self.assertTrue(enh.looks_like_echo(self.sysp, self.sysp))

    def test_wrapped_system_prompt_echo_is_detected(self):
        """带围栏包裹的复读同样要抓住。"""
        wrapped = "```\n" + self.sysp + "\n```"
        parsed = enh.strip_wrapping(wrapped)
        self.assertTrue(enh.looks_like_echo(parsed, self.sysp))

    def test_partial_prose_echo_is_detected(self):
        """照抄了半段散文（输出骨架之外的散文）也要抓住。"""
        prose = enh._instruction_prose(self.sysp)
        self.assertTrue(enh.looks_like_echo(prose[:600], self.sysp),
                        "整段照抄指令散文必须被判成复读")

    def test_echo_detail_gives_reason(self):
        """复读时必须给出**具体**原因（不能只丢一个布尔）。"""
        detail = enh.echo_detail(self.sysp, self.sysp)
        self.assertTrue(detail["is_echo"])
        self.assertTrue(detail["reason"], "复读必须给出可读原因")
        self.assertGreater(detail["longest"], 0)

    def test_echo_detail_not_echo_has_empty_reason(self):
        detail = enh.echo_detail(REAL_OUTPUT_1024, self.sysp)
        self.assertFalse(detail["is_echo"])
        self.assertEqual(detail["reason"], "")


class TestOutputParsing(unittest.TestCase):
    """契约 D：输出解析要剥掉各种包装"""

    def test_strip_thinking_removes_think_block(self):
        raw = "<think>\n用户想要一个视频提示词\n</think>\n[Shot 1] A red panda walks."
        out = enh.strip_thinking(raw)
        self.assertNotIn("<think>", out)
        self.assertNotIn("用户想要", out)
        self.assertTrue(out.startswith("[Shot 1]"))

    def test_strip_wrapping_removes_markdown_fence_anywhere(self):
        """围栏在**中间**也要剥掉（原实现只处理开头的围栏）。"""
        raw = "Sure, here is the prompt:\n```\n[Shot 1] A red panda.\n```\n"
        out = enh.strip_wrapping(raw)
        self.assertNotIn("```", out)
        self.assertIn("[Shot 1] A red panda.", out)

    def test_strip_wrapping_removes_label_prefix(self):
        for prefix in ("扩写后的提示词：", "提示词：", "Prompt:", "Output:"):
            out = enh.strip_wrapping(prefix + "\n[Shot 1] content")
            self.assertTrue(out.startswith("[Shot 1]"),
                            f"标签前缀 {prefix!r} 未被剥掉：{out!r}")

    def test_strip_wrapping_removes_markdown_emphasis_label(self):
        out = enh.strip_wrapping("**扩写后的提示词：**\n\n[Shot 1] content")
        self.assertTrue(out.startswith("[Shot 1]"),
                        f"Markdown 强调标签未被剥掉：{out!r}")

    def test_strip_wrapping_removes_role_prefix(self):
        out = enh.strip_wrapping("As an AI video director, here is the prompt:\n"
                                 "[Shot 1] content")
        self.assertTrue(out.startswith("[Shot 1]"),
                        f"角色前缀未被剥掉：{out!r}")

    def test_strip_wrapping_keeps_body_intact(self):
        """不能因为剥包装而动正文（保守剥离）。"""
        body = "A neon sign reads \"营业中\" above the door."
        out = enh.strip_wrapping("提示词：" + body)
        self.assertEqual(out, body)

    def test_strip_wrapping_does_not_eat_real_content(self):
        """正文里正常出现「提示词：」不该被剥（那是内容不是标签）。"""
        body = "A poster on the wall reads 提示词：写生"
        out = enh.strip_wrapping(body)
        self.assertIn("写生", out)

    def test_strip_wrapping_empty(self):
        self.assertEqual(enh.strip_wrapping(""), "")
        self.assertEqual(enh.strip_wrapping(None), "")


class TestStructuralMarkerWhitelist(unittest.TestCase):
    """契约 C：H3 官方结构化标记不是权重语法；其余 [word] 仍然拒收"""

    def setUp(self):
        self.v = _load_validator()

    def test_shot_marker_not_weight_syntax(self):
        """[Shot 1] 是 H3 官方要求的镜头编号 —— 不得判成权重语法。"""
        text = ("integrated_multimodal_description: [Shot 1] A cinematic shot "
                "of a woman walking.")
        self.assertEqual(self.v.check_weight_syntax(text, "pe"), [],
                         "[Shot 1] 被误判成 Compel 权重语法")

    def test_shot_markers_across_numbers(self):
        for n in range(1, 16):
            text = f"[Shot {n}] content here"
            self.assertEqual(self.v.check_weight_syntax(text, "pe"), [],
                             f"[Shot {n}] 被误判成权重语法")

    def test_language_tag_not_weight_syntax(self):
        """[English] / [Chinese] 是 H3 官方 <d> 标签内的语言标签。"""
        text = 'The woman (S1) says: <d>[English] I get off here.</d>'
        self.assertEqual(self.v.check_weight_syntax(text, "pe"), [],
                         "[English] 被误判成 Compel 权重语法")

    def test_real_weight_syntax_still_rejected(self):
        """白名单是**精确枚举** —— 真权重语法仍必须拒收。"""
        for text in ("[cat] sitting in frame",
                     "[a red panda] on a branch",
                     "neon (neon) signs",
                     "a cat sitting, (cat) nearby",
                     "cat++ in the frame"):
            self.assertTrue(self.v.check_weight_syntax(text, "pe"),
                            f"真权重语法被白名单放过：{text!r}")

    def test_whitelist_does_not_open_holes(self):
        """白名单不能被相邻词绕过（[Shotty] / [Englishx] 仍应拒收）。"""
        for text in ("[Shotty] content", "[Englishx] content", "[Englishish] x"):
            self.assertTrue(self.v.check_weight_syntax(text, "pe"),
                            f"白名单边界被绕过：{text!r}")


class TestEndToEndEnhanceResult(unittest.TestCase):
    """契约 E：合法产物走完全链路必须通过；复读不得写回"""

    def setUp(self):
        self.spec = enh.get_spec("video-director")
        self.sysp = self.spec["system_prompt"]

    def test_valid_director_output_passes_echo_and_validation(self):
        """真实产物：既不是复读，也能过校验。"""
        parsed = enh.strip_wrapping(enh.strip_thinking(REAL_OUTPUT_4096))
        self.assertFalse(enh.looks_like_echo(parsed, self.sysp))
        result = enh.validate_output(parsed, USER_PROMPT, "pe")
        self.assertTrue(result["ok"],
                        f"合法导演稿被校验拒收：{result['errors']}")

    def test_echo_never_reaches_merged_result(self):
        """
        复读时必须在写回前拦下 —— merged 结果里不得出现系统提示词原文。
        （merged_result 是写回提示词框的数据源。）
        """
        echoed = enh.strip_wrapping(enh.strip_thinking(self.sysp))
        self.assertTrue(enh.looks_like_echo(echoed, self.sysp))
        # 服务端在复读时直接 502，不调用 merge_result；
        # 这里模拟「若误调 merge_result 会发生什么」，锁住风险面。
        merged = enh.merge_result(USER_PROMPT, echoed, "replace")
        self.assertIn(self.sysp[:60], merged,
                      "复读内容确实会污染 merged —— 服务端拦截是唯一的防线")

    def test_merge_result_prefers_enhanced(self):
        """正常路径：merged 用增强结果（不是原文）。"""
        merged = enh.merge_result(USER_PROMPT, REAL_OUTPUT_1024, "replace")
        self.assertIn("integrated_multimodal_description", merged)
        self.assertNotEqual(merged.strip(), USER_PROMPT)


class TestServerAndAgentEchoWiring(unittest.TestCase):
    """服务端 / Agent 两处调用点都要用新判据 + 可读文案"""

    def _read(self, rel):
        with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
            return fh.read()

    def test_server_uses_echo_detail(self):
        src = self._read("server.py")
        self.assertIn("enh.echo_detail(", src,
                      "server.py 应使用 echo_detail 取可读原因")
        self.assertIn("system_prompt_echo", src,
                      "server.py 应带上机器可读的 code")

    def test_agent_uses_echo_detail(self):
        src = self._read(os.path.join("h3studio", "agent.py"))
        self.assertIn("enh.echo_detail(", src,
                      "agent.py 应使用 echo_detail 取可读原因")
        self.assertIn("system_prompt_echo", src,
                      "agent.py 应带上机器可读的 code")


if __name__ == "__main__":
    unittest.main()
