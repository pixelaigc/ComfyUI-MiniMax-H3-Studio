# -*- coding: utf-8 -*-
"""
C 组修复的回归测试（需求 C1 / C2 / C3 / C4）+ 作者推荐参数元数据。

【为什么这些断言必须存在】
用户 2026-11-13 报的真实 BUG：媒体库「文生视频」卡片的 LoRA 行显示
`integrated_multimodal_description`（H3 提示词字段名，不是 LoRA），
卡片又不显示步数/调度器。

这类回归**靠字符串断言抓不住** —— 「把三元表达式改成恒渲染」
「把白名单改成不过滤」都能让旧断言跑绿。所以这里的断言全部是：
  * 直接调用**纯函数**（job_meta.summarize / model_recommendations），
    断言返回值；
  * 用**反向 fixture**（脏值 + 合法值）成对断言 ——
    脏值必须被挡掉，合法值必须原样通过。

【断言不可自指】
期望值全部是**字面量**，不是拿被测函数自己的输出去算。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from h3studio import job_meta                     # noqa: E402
from h3studio import model_recommendations as mrec  # noqa: E402


# 用户截图里出现的那个脏值：H3 三段式提示词的**字段名**，不是 LoRA。
BOGUS_LORA = "integrated_multimodal_description"


class TestC1LoraWhitelist(unittest.TestCase):
    """C1：LoRA 行只能来自真实使用的 LoRA；脏值必须被挡掉。"""

    def test_bogus_lora_prompt_field_is_not_treated_as_lora(self):
        """脏值（提示词字段名）不能出现在 lora_label 里。"""
        params = {
            "model": "lora",
            "lora": BOGUS_LORA,
            "prompt": f"{BOGUS_LORA}: [Shot 1] ...",
            "steps": 4,
        }
        info = job_meta.summarize("t2v", params)
        self.assertEqual(info["lora_label"], "",
                         "提示词字段名被当成 LoRA 显示了（用户 2026-11-13 报的 BUG）")
        self.assertEqual(info["lora_name"], "")
        self.assertIsNone(info["lora_strength"])
        # 脏值被记录下来，便于排查，但**不能**进 lora_label
        self.assertEqual(info.get("lora_rejected"), BOGUS_LORA)

    def test_valid_lora_key_passes_through(self):
        """合法 key 必须原样通过（不能连正常值一起挡掉）。"""
        for key in ("ckpt850", "v4step600"):
            with self.subTest(key=key):
                info = job_meta.summarize("t2v", {"model": "lora", "lora": key})
                self.assertEqual(info["lora_label"], key)
                self.assertTrue(info["lora_name"].endswith(".safetensors"))
                self.assertEqual(info["lora_strength"], 1.0)

    def test_none_means_no_lora(self):
        """字面量 "none" = 明确不用加速 LoRA。"""
        info = job_meta.summarize("t2v", {"model": "lora", "lora": "none"})
        self.assertEqual(info["lora_label"], "")
        self.assertNotIn("lora_rejected", info)

    def test_missing_lora_falls_back_to_workflow_default(self):
        """没记录 lora -> 工作流默认 ckpt850（真实行为，不是编的）。"""
        info = job_meta.summarize("t2v", {"model": "lora"})
        self.assertEqual(info["lora_label"], "ckpt850")

    def test_new_panel_path_uses_accel_loras_not_legacy_lora(self):
        """新面板路径（model_key + accel_loras）不受脏 lora 字段影响。"""
        params = {
            "model_key": "hybrid_fl2va_ref2va",
            "accel_loras": [{"key": "dmad_4step_critic", "strength": 1.0,
                             "enabled": True}],
            "fixed_accel_lora": "ckpt850",
            "lora": BOGUS_LORA,          # 脏值同时存在
        }
        info = job_meta.summarize("t2v", params)
        self.assertEqual(info["lora_label"], "DMAD 4 步（critic 版）")
        self.assertEqual(info["lora_key"], "dmad_4step_critic")
        self.assertNotIn(BOGUS_LORA, info["lora_label"])

    def test_every_summary_path_has_stable_shape(self):
        """所有模式的摘要形状恒定（前端只渲染，不判断字段是否存在）。"""
        cases = [
            ("t2v", {"model_key": "fl2va_pruned_int8"}),
            ("t2v", {"model": "lora", "lora": "ckpt850"}),
            ("flf", {"model": "lora", "lora": "v4step600"}),
            ("r2v", {"model": "lora"}),
            ("t2i", {"model": "z_image_turbo_bf16"}),
            ("t2a", {"checkpoint": "yue2_3b_bf16"}),
        ]
        for mode, params in cases:
            with self.subTest(mode=mode):
                info = job_meta.summarize(mode, params)
                for field in ("model_key", "model_label", "lora_label",
                              "lora_strength", "lora_name", "lora_key",
                              "fixed_lora_label", "fixed_lora_key",
                              "attention_label", "source"):
                    self.assertIn(field, info)


class TestC2GenerationParams(unittest.TestCase):
    """C2：卡片补齐生成参数；取不到显示「—」，不得顶替。"""

    def test_params_are_taken_by_explicit_key(self):
        params = {
            "model_key": "hybrid_fl2va_ref2va",
            "steps": 4, "sampler": "euler", "scheduler": "beta",
            "shift_video": 12.0, "shift_audio": 3.0,
            "width": 1344, "height": 768, "duration": 5, "seed": 42,
        }
        info = job_meta.summarize("t2v", params)
        self.assertEqual(info["steps"], 4)
        self.assertEqual(info["sampler"], "euler")
        self.assertEqual(info["scheduler"], "beta")
        self.assertEqual(info["shift_video"], 12.0)
        self.assertEqual(info["shift_audio"], 3.0)
        self.assertEqual(info["width"], 1344)
        self.assertEqual(info["height"], 768)
        self.assertEqual(info["duration"], 5)
        self.assertEqual(info["seed"], 42)

    def test_missing_params_are_none_not_borrowed(self):
        """取不到的字段是 None（前端显示「—」），不能拿别的字段顶替。"""
        info = job_meta.summarize("t2v", {"model_key": "fl2va_pruned_int8"})
        for field in ("steps", "sampler", "scheduler", "shift_video",
                      "shift_audio", "width", "height", "duration", "seed"):
            self.assertIsNone(info[field], f"{field} 不该被别的字段顶替")

    def test_prompt_and_description_never_become_params(self):
        """提示词 / 描述 / 工作流节点名永远不进生成参数字典。"""
        params = {
            "model_key": "fl2va_pruned_int8",
            "prompt": f"{BOGUS_LORA}: [Shot 1] ...",
            "negative_prompt": "KSamplerSelect BasicScheduler UNETLoader",
            "description": "工作流节点名",
        }
        info = job_meta.summarize("t2v", params)
        for field in job_meta.GENERATION_PARAM_FIELDS:
            value = info[field]
            if isinstance(value, str):
                self.assertNotIn(BOGUS_LORA, value)
                self.assertNotIn("KSamplerSelect", value)

    def test_param_whitelist_excludes_prompt(self):
        """白名单本身就不能包含 prompt / negative_prompt 之类。"""
        self.assertNotIn("prompt", job_meta.GENERATION_PARAM_FIELDS)
        self.assertNotIn("negative_prompt", job_meta.GENERATION_PARAM_FIELDS)
        self.assertNotIn("lora", job_meta.GENERATION_PARAM_FIELDS)


class TestAttentionMechanism(unittest.TestCase):
    """需求 3：媒体库条目标注**生成时实际使用**的注意力机制。"""

    def test_attention_from_params(self):
        info = job_meta.summarize("t2v", {
            "model_key": "fl2va_pruned_int8", "attention": "sparse"})
        self.assertEqual(info["attention_key"], "sparse")
        self.assertEqual(info["attention_label"], "稀疏视频注意力（预算制）")
        self.assertTrue(info["attention_speed_label"])

    def test_removed_attention_is_marked(self):
        """已下线的机制也要如实标注（并说明已下线）。"""
        info = job_meta.summarize("t2v", {
            "model_key": "fl2va_pruned_int8", "attention": "sol_scheduled"})
        self.assertEqual(info["attention_key"], "sol_scheduled")
        self.assertFalse(info["attention_available"])
        self.assertIn("已下线", info["attention_label"])

    def test_no_attention_record_is_empty_not_invented(self):
        """没记录 -> 空（不编造）。"""
        info = job_meta.summarize("t2v", {"model_key": "fl2va_pruned_int8"})
        self.assertEqual(info["attention_key"], "")
        self.assertEqual(info["attention_label"], "")


class TestModelRecommendations(unittest.TestCase):
    """需求 1 / 2：作者推荐参数 + 来源可追溯 + 「作者未提供」不编造。"""

    def test_dmad_author_values_are_used(self):
        """DMAD 的参数是作者写在 safetensors metadata 里的。"""
        rec = mrec.accel_lora_recommendation("dmad_4step_critic")
        self.assertEqual(rec["steps"], 4)
        self.assertEqual(rec["shift_video"], 12.0)
        self.assertEqual(rec["shift_audio"], 2.0)   # 作者写的 2，不是内生的 3
        self.assertEqual(rec["source_kind"], "author")
        self.assertIn("sampling", rec["source"])

    def test_turbo_author_values_are_used(self):
        rec = mrec.accel_lora_recommendation("ckpt850")
        self.assertEqual(rec["steps"], 4)
        self.assertEqual(rec["scheduler"], "simple")
        self.assertEqual(rec["source_kind"], "author")
        self.assertIn("README.md", rec["source"])

    def test_not_provided_is_explicit(self):
        """作者没写的项必须是 not_provided（前端显示「作者未提供」）。"""
        rec = mrec.accel_lora_recommendation("speed_slider")
        self.assertEqual(rec["steps"], mrec.NOT_PROVIDED)
        self.assertEqual(rec["sampler"], mrec.NOT_PROVIDED)
        self.assertEqual(rec["scheduler"], mrec.NOT_PROVIDED)
        self.assertEqual(rec["source_kind"], "author")
        self.assertIn("未提供", rec["source"])

    def test_lightx2v_has_no_fabricated_steps(self):
        """LightX2V 作者没给步数 -> not_provided（不能拿文件名里的 4step 冒充）。"""
        rec = mrec.accel_lora_recommendation("lightx2v_4step_768p")
        self.assertEqual(rec["steps"], mrec.NOT_PROVIDED)
        self.assertEqual(rec["scheduler"], mrec.NOT_PROVIDED)
        self.assertEqual(rec["sampler"], mrec.NOT_PROVIDED)
        # 但 base_model 是作者在 metadata 里给的
        self.assertNotEqual(rec["base_model"], mrec.NOT_PROVIDED)
        self.assertIn("MiniMax-H3", rec["base_model"])

    def test_every_accel_lora_has_a_recommendation_entry(self):
        """清单里的每一枚加速 LoRA 都有推荐参数条目（哪怕全是「未提供」）。"""
        from h3studio import video_models as vmodels
        keys = set(vmodels.VIDEO_ACCEL_LORAS) | set(vmodels.VIDEO_ACCEL_LORA_COMBOS)
        payload = mrec.accel_lora_recommendations_payload()
        for key in keys:
            with self.subTest(key=key):
                self.assertIn(key, payload)
                rec = payload[key]
                self.assertIn("source_kind", rec)
                self.assertIn("source", rec)

    def test_every_model_has_a_recommendation_entry(self):
        from h3studio import video_models as vmodels
        payload = mrec.model_recommendations_payload()
        for key in vmodels.VIDEO_MODELS:
            with self.subTest(key=key):
                self.assertIn(key, payload)

    def test_hybrid_model_author_provided_nothing(self):
        """hybrid 底模的作者明说没给采样参数 -> 步数等必须是「作者未提供」。"""
        rec = mrec.model_recommendation("hybrid_fl2va_ref2va")
        self.assertEqual(rec["steps"], mrec.NOT_PROVIDED)
        self.assertEqual(rec["scheduler"], mrec.NOT_PROVIDED)
        self.assertEqual(rec["sampler"], mrec.NOT_PROVIDED)
        # 但 shift 是 ComfyUI 的模型硬性值（漏挂画质明显变差）
        self.assertEqual(rec["shift_video"], 12.0)
        self.assertEqual(rec["shift_audio"], 3.0)
        self.assertEqual(rec["source_kind"], "comfyui")

    def test_unknown_key_returns_all_not_provided(self):
        rec = mrec.model_recommendation("no_such_model")
        for field in mrec.REC_FIELDS:
            self.assertEqual(rec[field], mrec.NOT_PROVIDED)

    def test_shift_is_always_the_comfyui_hard_value(self):
        """H3 的 shift 12/3 是模型硬性值，所有底模都一样。"""
        for key in ("fl2va_pruned_int8", "fl2va_int8", "fl2va_pruned_w4a8",
                    "fl2va_bf16", "hybrid_fl2va_ref2va"):
            with self.subTest(key=key):
                rec = mrec.model_recommendation(key)
                self.assertEqual(rec["shift_video"], 12.0)
                self.assertEqual(rec["shift_audio"], 3.0)

    def test_recommendation_attached_to_summary(self):
        """摘要里挂上推荐参数（前端模态框直接渲染）。"""
        info = job_meta.summarize("t2v", {
            "model_key": "hybrid_fl2va_ref2va",
            "accel_loras": [{"key": "dmad_4step_critic", "strength": 1.0,
                             "enabled": True}],
            "fixed_accel_lora": "ckpt850",
        })
        self.assertEqual(info["lora_recommendation"]["steps"], 4)
        self.assertEqual(info["model_recommendation"]["shift_video"], 12.0)
        self.assertEqual(
            info["fixed_lora_recommendation"]["scheduler"], "simple")

    def test_fixed_lora_without_record_is_not_invented(self):
        """没记录固定 LoRA 时不编一个（老任务的真实情况）。"""
        info = job_meta.summarize("t2v", {"model_key": "fl2va_pruned_int8"})
        self.assertEqual(info["fixed_lora_key"], "")
        self.assertEqual(
            info["fixed_lora_recommendation"]["scheduler"],
            mrec.NOT_PROVIDED)



if __name__ == "__main__":
    unittest.main()
