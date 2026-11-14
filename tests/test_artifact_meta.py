# -*- coding: utf-8 -*-
"""
产物实际生成元数据（2026-11-15 媒体库卡片 LoRA 误显示 + 详情模态框）—— 回归测试。

【为什么要有这个文件】
用户 2026-11-15 实测报出两个缺陷：
  BUG 1  媒体库卡片上的 LoRA 名称与**实际生成产物**不是同一个
          （存在「根本就没这个 LoRA」的误显示）。
  BUG 2  卡片不显示步数；也没有地方能看完整生成参数。

【根因（本文件保护的行为）】
`jobs.params` 存的是**任务提交参数**，不是**产物实际使用的参数**：
  * 步数 / 调度器 / 采样器在**构建工作流时**才按「实际挂了哪枚加速 LoRA」
    回落出来 —— 落库的 params 里没有它们；
  * 显示层只能靠猜，猜的那套默认值（ckpt850）与工作流的默认值
    不是同一套 -> 卡片显示「根本就没这个 LoRA」。

【修法】
以**构建好的 workflow**（产物实际会跑的那份）为真源反解出真实参数，
worker 回写进 params；显示层只读。

【测试纪律】
  1. 断言必须由「命名该行为的那条断言」抓住，不自指；
  2. 每条用例带「为什么这样断言」注释；
  3. 前端逻辑用 Node 真跑（不是字符串断言）。

运行：python3 -m pytest tests/test_artifact_meta.py -v
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from h3studio import artifact_meta, job_meta  # noqa: E402


#: 用户实测看到的「假装成 LoRA 的提示词」。
PROMPT_AS_LORA = (
    "integrated_multimodal_description: [Shot 1] Live-action, cinematic "
    "Chinese wuxia period film. At night in a moonlit bamboo forest..."
)


# ==================================================================
# 第一层：workflow 反解（产物实际元数据的取数口）
# ==================================================================
class TestExtractFromWorkflow(unittest.TestCase):
    """从构建好的 workflow 里反解出真正会跑的那份参数。"""

    def test_extracts_steps_scheduler_sampler_from_real_nodes(self):
        """BasicScheduler + KSamplerSelect 的值必须被取到。

        为什么这样断言：步数/调度器/采样器在 ComfyUI 里就是这三个节点的
        输入，取不到就等于「卡片显示不出步数」（BUG 2）。
        """
        workflow = {
            "9": {"class_type": "BasicScheduler",
                  "inputs": {"scheduler": "beta", "steps": 4, "denoise": 1.0}},
            "17": {"class_type": "KSamplerSelect",
                   "inputs": {"sampler_name": "euler"}},
        }
        out = artifact_meta.extract_from_workflow(workflow)
        self.assertEqual(4, out["steps"])
        self.assertEqual("beta", out["scheduler"])
        self.assertEqual("euler", out["sampler"])

    def test_extracts_actual_lora_name_and_strength(self):
        """工作流里挂的 LoRA 权重名与强度必须被取到（BUG 1 的取数口）。"""
        workflow = {
            "138:0": {
                "class_type": "MiniMaxH3TurboLoRA",
                "inputs": {
                    "lora_name": "minimax_h3_turbo_4step_ema_ckpt850.safetensors",
                    "strength_model": 0.65,
                },
            },
        }
        out = artifact_meta.extract_from_workflow(workflow)
        self.assertEqual(1, len(out["loras"]))
        item = out["loras"][0]
        self.assertEqual("minimax_h3_turbo_4step_ema_ckpt850", item["name"])
        self.assertAlmostEqual(0.65, item["strength"])
        self.assertEqual("MiniMaxH3TurboLoRA", item["loader"])

    def test_workflow_without_lora_yields_empty_list(self):
        """工作流里没有 LoRA 节点 -> 清单为空数组（**不是** None、不是假名）。

        为什么这样断言：空数组与「缺这个字段」语义不同 ——
        空数组 = 「确实没挂」，缺字段 = 「不知道」。
        """
        workflow = {
            "9": {"class_type": "BasicScheduler",
                  "inputs": {"scheduler": "simple", "steps": 20}},
        }
        out = artifact_meta.extract_from_workflow(workflow)
        self.assertEqual([], out["loras"])

    def test_missing_fields_stay_none_not_fabricated(self):
        """取不到就必须是 None —— **不编造默认值**。

        为什么这样断言：卡片上的「步数 4」如果是编的，用户会照着它
        复现出不一样的结果。缺失必须如实显示「未知」。
        """
        out = artifact_meta.extract_from_workflow({})
        self.assertIsNone(out["steps"])
        self.assertIsNone(out["scheduler"])
        self.assertIsNone(out["sampler"])
        self.assertIsNone(out["cfg"])
        self.assertIsNone(out["seed"])
        self.assertEqual([], out["loras"])

    def test_lora_weight_path_is_cleaned_conservatively(self):
        """带目录前缀的权重路径只去目录与扩展名，不做任何美化。"""
        workflow = {
            "7": {"class_type": "LoraLoaderModelOnly",
                  "inputs": {
                      "lora_name": ("h3_lightx2v/minimax_h3_fl2v_turbo_4step"
                                    "_v1.0_768p_comfyui_bf16.safetensors"),
                      "strength_model": 1.0,
                  }},
        }
        out = artifact_meta.extract_from_workflow(workflow)
        self.assertEqual("minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16",
                         out["loras"][0]["name"])

    def test_non_lora_node_whose_class_contains_lora_word_is_not_falsely_counted(self):
        """class_type 里带 lora 字样但**没有 lora_name** 的节点不算 LoRA。

        为什么这样断言：靠 class_type 名字匹配必然有误判风险，
        没有 lora_name 的节点就不是挂载点。
        """
        workflow = {
            "5": {"class_type": "LoraInfo", "inputs": {"note": "no weight here"}},
        }
        out = artifact_meta.extract_from_workflow(workflow)
        self.assertEqual([], out["loras"])


# ==================================================================
# 第二层：回写 params（让落库的参数不再说谎）
# ==================================================================
class TestApplyToParams(unittest.TestCase):
    """把反解出来的真实值写回 params。"""

    def test_writes_actual_sampling_params_back(self):
        """步数/调度器/采样器必须被写回 params（BUG 2 的修复落点）。"""
        params = {"prompt": "x", "duration": 5}
        extracted = artifact_meta.extract_from_workflow({
            "9": {"class_type": "BasicScheduler",
                  "inputs": {"scheduler": "beta", "steps": 4}},
            "17": {"class_type": "KSamplerSelect",
                   "inputs": {"sampler_name": "euler"}},
        })
        artifact_meta.apply_to_params(params, extracted)
        self.assertEqual(4, params["steps"])
        self.assertEqual("beta", params["scheduler"])
        self.assertEqual("euler", params["sampler"])

    def test_writes_actual_loras_and_legacy_reader_fields(self):
        """实际挂载的 LoRA 必须同时写进新字段与旧读侧字段。

        为什么这样断言：读侧（job_meta.resolve_job_lora / 前端）现在
        读的是 `accel_loras` / `fixed_accel_lora`；只写新字段的话
        读侧还是猜 -> 还是误显示。
        """
        params: dict = {"prompt": "x"}
        extracted = artifact_meta.extract_from_workflow({
            "138:0": {
                "class_type": "MiniMaxH3TurboLoRA",
                "inputs": {
                    "lora_name": "minimax_h3_turbo_4step_ema_ckpt850.safetensors",
                    "strength_model": 0.65,
                },
            },
        })
        artifact_meta.apply_to_params(params, extracted)
        self.assertEqual(1, len(params["artifact_loras"]))
        artifact_list = params["artifact_loras"]
        self.assertEqual("minimax_h3_turbo_4step_ema_ckpt850",
                         artifact_list[0]["name"])
        # 旧读侧字段也要有真实值
        self.assertEqual("minimax_h3_turbo_4step_ema_ckpt850",
                         params["fixed_accel_lora"])
        accel_list = params["accel_loras"]
        self.assertEqual("minimax_h3_turbo_4step_ema_ckpt850",
                         accel_list[0]["key"])
        self.assertAlmostEqual(0.65, accel_list[0]["strength"])

    def test_workflow_without_lora_records_explicit_empty_list(self):
        """工作流确实没挂 LoRA -> 显式记空清单，并清掉旧的固定枚字段。

        为什么这样断言：留着 `fixed_accel_lora=ckpt850` 会让读侧
        继续显示一枚**根本没挂**的 LoRA —— 那就是用户报的误显示。
        """
        params: dict = {"prompt": "x", "fixed_accel_lora": "ckpt850",
                        "accel_lora": "ckpt850"}
        extracted = artifact_meta.extract_from_workflow({
            "9": {"class_type": "BasicScheduler",
                  "inputs": {"scheduler": "simple", "steps": 20}},
        })
        artifact_meta.apply_to_params(params, extracted)
        self.assertEqual([], params["artifact_loras"])
        self.assertNotIn("fixed_accel_lora", params)
        self.assertNotIn("accel_lora", params)

    def test_does_not_touch_prompt_or_continuation(self):
        """回写不能碰非生成参数字段（提示词 / 接续信息必须原样）。"""
        params: dict = {"prompt": "hello", "continuation": {"chain_id": "abc"},
                        "asset_refs": [{"asset_id": "x"}]}
        extracted = artifact_meta.extract_from_workflow({
            "9": {"class_type": "BasicScheduler",
                  "inputs": {"scheduler": "beta", "steps": 4}},
        })
        artifact_meta.apply_to_params(params, extracted)
        self.assertEqual("hello", params["prompt"])
        self.assertEqual({"chain_id": "abc"}, params["continuation"])
        self.assertEqual([{"asset_id": "x"}], params["asset_refs"])


# ==================================================================
# 第二层之二：真实工作流构建器 -> 反解（端到端，不造假 workflow）
# ==================================================================
class TestRealWorkflowRoundTrip(unittest.TestCase):
    """用**真实的工作流构建器**造 workflow，再反解回去。

    【为什么必须用真实构建器】手写 workflow 夹具只能证明
    「反解函数认得这几个 class_type」；用真实构建器才能证明
    「构建器产出的东西真的能被反解回来」——
    也就是 worker 那条链路真的跑得通。
    """

    def test_t2v_panel_workflow_roundtrip(self):
        """真实 t2v 面板工作流 -> 反解 -> 步数/调度器/采样器/LoRA 都在。"""
        from h3studio import workflows as wf
        params = {
            "job_id": "roundtrip001",
            "model_key": "fl2va_pruned_int8",
            "width": 1216,
            "height": 672,
            "duration": 5,
            "seed": 123456,
        }
        try:
            workflow = wf.build_workflow("t2v", params)
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"本机工作流构建失败（缺模型/节点?）: {exc}")
        extracted = artifact_meta.extract_from_workflow(workflow)
        self.assertIsNotNone(extracted["steps"],
                             "真实工作流里必须有 BasicScheduler 的步数")
        self.assertIsNotNone(extracted["scheduler"])
        self.assertIsNotNone(extracted["sampler"])
        # 步数必须是正整数（不能是 0 / None）
        self.assertGreater(extracted["steps"], 0)

    def test_t2v_legacy_workflow_roundtrip(self):
        """真实 t2v 旧路径工作流 -> 反解 -> 能拿到 LoRA 与步数。

        为什么这样断言：旧路径（params 有 lora、没有 model_key）
        正是用户报「LoRA 显示不对」的那条路径。
        """
        from h3studio import workflows as wf
        params = {
            "job_id": "roundtrip002",
            "duration": 5,
            "seed": 654321,
        }
        try:
            workflow = wf.build_workflow("t2v", params)
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"本机工作流构建失败: {exc}")
        extracted = artifact_meta.extract_from_workflow(workflow)
        self.assertIsNotNone(extracted["steps"])
        # 旧路径默认挂 ckpt850 -> 反解必须认出来
        self.assertTrue(extracted["loras"],
                        f"旧路径工作流里没反解到 LoRA: {extracted}")
        # 反查回本机 key（证明反查表是对的）
        raw = extracted["loras"][0]["raw_name"]
        self.assertEqual("ckpt850", artifact_meta.lora_key_for_weight(raw))


# ==================================================================
# 第三层：显示层读侧（LoRA 映射正确 / 无 LoRA 不显示假 LoRA）
# ==================================================================
class TestResolveJobLoraTruth(unittest.TestCase):
    """job_meta.resolve_job_lora —— 显示层的唯一取值入口。"""

    def test_artifact_branch_is_reachable(self):
        """【防回归】artifact_loras 分支必须**真的可达**。

        为什么这样断言：2026-11-15 本轮实测遇到过工作区被注入
        `if False and "artifact_loras" in params:` —— 分支永不执行，
        而所有「产物实际优先」的断言**照样绿**（因为它们断言的是
        回落路径的结果）。所以必须有一条**直接针对分支可达性**的断言。

        做法：造一个「产物实际」与「提交参数」**矛盾**的样本 ——
        产物实际是 ckpt850，提交参数说 v4step600。
        只有分支真的执行，才会返回 ckpt850。
        """
        resolved = job_meta.resolve_job_lora("t2v", {
            "artifact_loras": [{
                "name": "minimax_h3_turbo_4step_ema_ckpt850",
                "raw_name": "minimax_h3_turbo_4step_ema_ckpt850.safetensors",
                "strength": 1.0,
                "loader": "MiniMaxH3TurboLoRA",
            }],
            # 提交参数说另一枚 —— 产物实际必须压过它
            "accel_loras": [{"key": "v4step600", "strength": 1.0,
                             "enabled": True}],
            "fixed_accel_lora": "v4step600",
        })
        self.assertEqual("Turbo 4 步 · ckpt850", resolved["lora"],
                         "artifact_loras 分支未执行（被短路了？）")

    def test_artifact_loras_take_priority_and_are_mapped_correctly(self):
        """产物实际清单优先，且名字反查成友好显示名（BUG 1 的核心断言）。

        为什么这样断言：产物实际挂的是**权重文件名**
        （`minimax_h3_turbo_4step_ema_ckpt850.safetensors`），
        而面板下拉里是**友好 key**（`ckpt850`）。
        显示时必须反查回同一个 LoRA —— 显示成权重名虽然不算错，
        但显示成**别的** LoRA 名就是错（用户报的正是这个）。
        """
        resolved = job_meta.resolve_job_lora("t2v", {
            "artifact_loras": [{
                "name": "minimax_h3_turbo_4step_ema_ckpt850",
                "raw_name": "minimax_h3_turbo_4step_ema_ckpt850.safetensors",
                "strength": 0.65,
                "loader": "MiniMaxH3TurboLoRA",
            }],
            # 提交参数里的旧字段是**别的**东西 —— 产物实际必须压过它
            "accel_loras": [{"key": "ckpt850", "strength": 1.0}],
        })
        # 反查成功 -> 显示友好名（与 model_info.lora_label 同源）
        self.assertEqual("Turbo 4 步 · ckpt850", resolved["lora"])
        self.assertEqual(1, len(resolved["labels"]))
        self.assertIn("Turbo 4 步 · ckpt850", resolved["labels"][0])
        self.assertIn("0.65", resolved["labels"][0])

    def test_artifact_lora_display_matches_model_info_label(self):
        """model_info.lora_label 与 gen_params.lora 必须是**同一个**名字。

        为什么这样断言：历史上这两套取值路径口径不一致 ——
        同一张卡片上「LoRA 行」说「无」、详情区说「ckpt850」。
        这条断言把「两处必须一致」钉死。
        """
        params = {
            "artifact_loras": [{
                "name": "minimax_h3_turbo_4step_ema_ckpt850",
                "raw_name": "minimax_h3_turbo_4step_ema_ckpt850.safetensors",
                "strength": 1.0,
                "loader": "MiniMaxH3TurboLoRA",
            }],
        }
        info = job_meta.summarize("t2v", params)
        gp = job_meta.gen_params("t2v", params, info)
        self.assertEqual(info["lora_label"], gp["lora"].split(" ×")[0])

    def test_artifact_lora_not_in_local_registry_shows_weight_name(self):
        """产物挂的 LoRA 不在本机清单里 -> 如实显示权重名，不编。

        为什么这样断言：反查失败时**编一个名字**比显示权重名更糟。
        """
        resolved = job_meta.resolve_job_lora("t2v", {
            "artifact_loras": [{
                "name": "some_unknown_lora_v9",
                "raw_name": "some_unknown_lora_v9.safetensors",
                "strength": 1.0,
            }],
        })
        self.assertEqual("some_unknown_lora_v9", resolved["lora"])

    def test_no_lora_record_does_not_show_fake_ckpt850(self):
        """**产物实际记录为空** -> 不得显示 ckpt850。

        为什么这样断言：用户报的「根本就没这个 LORA」有两种形态：
          (a) 显示了一个**从来没挂过**的名字（提示词冒充）；
          (b) 显示的 LoRA 与产物实际用的**不是同一个**。
        本条守住 (b) 的极端形态：worker 已明确回写「工作流里没有 LoRA
        节点」，此时显示层必须尊重这个事实，不能又回落成 ckpt850。

        【注意 —— 与「params 里没记 lora」的区别】
        params 里**没有** lora 字段 ≠ 没用 LoRA：工作流会挂默认 ckpt850
        （这由既有测试 test_video_without_lora_field_uses_workflow_default
        守着）。**只有产物实际记录**才能否定它。
        """
        resolved = job_meta.resolve_job_lora("t2v", {
            "artifact_loras": [],
            "accel_loras": [{"key": "ckpt850", "strength": 1.0}],
        })
        self.assertEqual([], resolved["labels"])
        self.assertIsNone(resolved["lora"])

    def test_missing_record_falls_back_to_workflow_default(self):
        """**没有产物记录** -> 回落工作流默认 ckpt850（与工作流同源）。

        为什么这样断言：resolve_job_lora 现在直接复用工作流的
        `resolve_accel_lora_slot()`，所以「没传」时显示的必须是工作流
        真正会挂的那枚 —— 显示口径 == 工作流口径。
        """
        resolved = job_meta.resolve_job_lora("t2v", {"prompt": "x", "duration": 5})
        self.assertEqual("ckpt850", resolved["lora"])
        self.assertTrue(resolved["labels"])

    def test_prompt_as_lora_is_rejected(self):
        """提示词冒充 LoRA -> 仍然被白名单挡掉（用户报的原缺陷）。

        为什么这样断言：这是用户 2026-11-13 报的 BUG 1 原型 ——
        `integrated_multimodal_description: ...` 被当 LoRA 显示。
        读取侧（resolve_accel_lora_slot 的 legacy 分支）必须过白名单。
        """
        resolved = job_meta.resolve_job_lora("t2v", {"lora": PROMPT_AS_LORA})
        self.assertEqual([], resolved["labels"])
        self.assertNotIn("integrated_multimodal_description",
                         " ".join(resolved["labels"]))

    def test_multiple_artifact_loras_all_listed(self):
        """产物挂多枚 LoRA -> 全部列出（不只第一枚）。"""
        resolved = job_meta.resolve_job_lora("t2i_moss", {
            "artifact_loras": [
                {"name": "moss_xvstyle", "strength": 0.7},
                {"name": "BackgroundSharpness_MOSS", "strength": 1.0},
            ],
        })
        self.assertEqual(2, len(resolved["labels"]))


# ==================================================================
# 第四层：gen_params（卡片行 + 详情模态框的数据源）
# ==================================================================
class TestGenParamsTruth(unittest.TestCase):
    """job_meta.gen_params —— 卡片行与详情模态框的唯一真源。"""

    def test_missing_lora_renders_explicit_not_used_text(self):
        """**产物记录为空** -> 显示「未使用 LoRA」（用户原话）。

        为什么这样断言：用户明确要求「缺失时应显示『未使用 LoRA』」。
        「缺失」在这里指**产物实际没有 LoRA**（worker 回写空清单），
        不是「params 里没写 lora 字段」—— 后者工作流会挂默认 ckpt850，
        显示 ckpt850 才是对的（既有测试守着那条）。
        """
        gp = job_meta.gen_params("t2v", {"prompt": "x", "artifact_loras": []})
        self.assertEqual("未使用 LoRA", gp["lora"])

    def test_artifact_lora_renders_actual_name(self):
        """产物实际 LoRA 名必须出现在 gen_params 里。

        为什么这样断言：没有 raw_name -> 反查不到 -> 如实显示清洗后的
        权重名（不编）。这正是「以产物实际为准」的最小保证。
        """
        gp = job_meta.gen_params("t2v", {
            "artifact_loras": [{
                "name": "minimax_h3_turbo_4step_ema_ckpt850",
                "strength": 0.65,
            }],
        })
        self.assertIn("minimax_h3_turbo_4step_ema_ckpt850", gp["lora"])
        self.assertNotEqual("未使用 LoRA", gp["lora"])

    def test_steps_scheduler_sampler_pass_through_when_recorded(self):
        """落库的步数/调度器/采样器必须出现在 gen_params（BUG 2）。"""
        gp = job_meta.gen_params("t2v", {
            "steps": 4, "scheduler": "beta", "sampler": "euler",
        })
        self.assertEqual("4", gp["steps"])
        self.assertEqual("beta", gp["scheduler"])
        self.assertEqual("euler", gp["sampler"])

    def test_missing_sampling_params_stay_empty_string(self):
        """缺失的采样参数保持空串 -> 前端渲染成「—」（不拿别的顶替）。"""
        gp = job_meta.gen_params("t2v", {"prompt": "x"})
        self.assertEqual("", gp["steps"])
        self.assertEqual("", gp["scheduler"])
        self.assertEqual("", gp["sampler"])

    def test_artifact_loras_list_exposed_for_detail_modal(self):
        """详情模态框要能逐枚列出 LoRA -> gen_params 必须带列表。"""
        gp = job_meta.gen_params("t2v", {
            "artifact_loras": [
                {"name": "a", "strength": 1.0, "loader": "LoraLoader"},
                {"name": "b", "strength": 0.5, "loader": "LoraLoader"},
            ],
        })
        self.assertEqual(2, len(gp["artifact_loras"]))
        self.assertEqual("a", gp["artifact_loras"][0]["name"])


# ==================================================================
# 第五层：端到端（服务端 row_to_job 下发的数据）
# ==================================================================
class TestServerRowToJobTruth(unittest.TestCase):
    """server.row_to_job —— 前端拿到的最终数据。"""

    def _row(self, params):
        import server
        return {
            "id": "job123",
            "created_at": 1700000000.0,
            "mode": "t2v",
            "params": json.dumps(params),
            "status": "done",
            "outputs": json.dumps([{"kind": "video", "file": "videos/v.mp4",
                                    "width": 1216, "height": 672}]),
            "prompt_id": "p1",
            "progress": 1.0,
            "progress_text": "",
            "error": None,
            "started_at": 1700000000.0,
            "finished_at": 1700000010.0,
        }

    def test_row_to_job_gen_params_reflects_artifact_truth(self):
        """row_to_job 下发的 gen_params 必须反映产物实际值。"""
        import server
        row = self._row({
            "artifact_loras": [{
                "name": "minimax_h3_turbo_4step_ema_ckpt850",
                "strength": 0.65,
            }],
            "steps": 4, "scheduler": "beta", "sampler": "euler",
        })
        job = server.row_to_job(row)
        self.assertEqual("4", job["gen_params"]["steps"])
        self.assertEqual("beta", job["gen_params"]["scheduler"])
        self.assertEqual("euler", job["gen_params"]["sampler"])
        self.assertIn("minimax_h3_turbo_4step_ema_ckpt850",
                      job["gen_params"]["lora"])

    def test_row_to_job_without_lora_record_shows_not_used(self):
        """产物记录为空 -> 卡片行显示「未使用 LoRA」，不是错名。"""
        import server
        job = server.row_to_job(self._row({"prompt": "x", "duration": 5,
                                           "artifact_loras": []}))
        self.assertEqual("未使用 LoRA", job["gen_params"]["lora"])


# ==================================================================
# 第六层：前端（真跑 Node，不是字符串断言）
# ==================================================================
class TestFrontendRendersTruth(unittest.TestCase):
    """前端渲染层：卡片 LoRA 行 / 详情模态框字段。

    【为什么用「从 app.js 真抽取」而不是手抄一份】
    手抄的副本会与 app.js 漂移 —— 测绿了但线上是坏的。
    这里直接**从 web/app.js 的源码里切出函数体**再 eval，
    所以测的就是线上真正跑的那份代码。
    """

    def _run_node(self, script):
        node = subprocess.run(["node", "--version"], capture_output=True,
                              text=True)
        if node.returncode != 0:
            self.skipTest("本机没有 node")
        proc = subprocess.run(["node", "-e", script], capture_output=True,
                              text=True, cwd=str(ROOT), timeout=60)
        self.assertEqual(0, proc.returncode,
                         msg=f"node 执行失败:\n{proc.stdout}\n{proc.stderr}")
        return json.loads(proc.stdout)

    #: 从 app.js 里切出「函数声明」或「const 常量」的源码片段。
    _EXTRACT_JS = r"""
const fs = require('fs');
const src = fs.readFileSync('web/app.js', 'utf8');
function grabFn(name) {
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('找不到函数 ' + name);
  let i = src.indexOf('{', start);
  let depth = 0;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') { depth--; if (depth === 0) return src.slice(start, j + 1); }
  }
  throw new Error('函数 ' + name + ' 解析失败');
}
function grabConst(name) {
  const start = src.indexOf('const ' + name + ' = [');
  if (start < 0) throw new Error('找不到常量 ' + name);
  const end = src.indexOf('];', start);
  return src.slice(start, end + 2);
}
"""

    def test_lora_row_shows_not_used_lora_text(self):
        """卡片 LoRA 行：没有记录 -> 显示「未使用 LoRA」。

        为什么这样断言：用户原话「缺失时应显示『未使用 LoRA』」。
        """
        result = self._run_node(self._EXTRACT_JS + r"""
const escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
const loraRowHtml = new Function('escapeHtml', grabFn('loraRowHtml')
  + '; return loraRowHtml;')(escapeHtml);
console.log(JSON.stringify({
  withLora: loraRowHtml('Turbo 4 步 · ckpt850 ×1.00'),
  without: loraRowHtml(''),
  nullish: loraRowHtml(null),
}));
""")
        self.assertIn("Turbo 4 步 · ckpt850 ×1.00", result["withLora"])
        self.assertIn("未使用 LoRA", result["without"])
        self.assertIn("未使用 LoRA", result["nullish"])

    def test_detail_modal_field_list_covers_required_fields(self):
        """详情模态框必须包含用户点名的字段：步数/调度器/采样器/LoRA/种子/尺寸。

        为什么这样断言：用户原话「展示详细内容（步数、调度器、采样器、
        模型/LoRA 列表、种子、尺寸等实际元数据）」。
        """
        result = self._run_node(self._EXTRACT_JS + r"""
const raw = grabConst('ARTIFACT_META_FIELDS');
const ARTIFACT_META_FIELDS = new Function(raw + '; return ARTIFACT_META_FIELDS;')();
console.log(JSON.stringify({labels: ARTIFACT_META_FIELDS.map(p => p[1])}));
""")
        labels = result["labels"]
        for required in ("步数", "调度器", "采样器", "模型", "种子", "宽度", "高度"):
            self.assertIn(required, labels)

    def test_detail_modal_renders_actual_values_and_unknown_for_missing(self):
        """详情模态框：有值显示真值，缺失显式写「未知」。

        为什么这样断言：用户要求「字段来自真实元数据，缺失字段明确标注
        『未知』而非编造」。所以缺失**不能**留空、也不能编一个值。
        """
        result = self._run_node(self._EXTRACT_JS + r"""
const escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
const rawFields = grabConst('ARTIFACT_META_FIELDS');
const ARTIFACT_META_FIELDS = new Function(rawFields + '; return ARTIFACT_META_FIELDS;')();
const ARTIFACT_UNKNOWN = '未知';
const artifactMetaRowsHtml = new Function(
  'escapeHtml', 'ARTIFACT_META_FIELDS', 'ARTIFACT_UNKNOWN',
  grabFn('artifactMetaRowsHtml') + '; return artifactMetaRowsHtml;'
)(escapeHtml, ARTIFACT_META_FIELDS, ARTIFACT_UNKNOWN);

const html = artifactMetaRowsHtml({
  model: 'MiniMax-H3 FL2VA（int8，默认档）',
  steps: 4, scheduler: 'beta', sampler: 'euler',
  cfg: null, seed: null, width: 1216, height: 672,
});
console.log(JSON.stringify({html}));
""")
        html = result["html"]
        self.assertIn("4", html)
        self.assertIn("beta", html)
        self.assertIn("euler", html)
        self.assertIn("1216", html)
        self.assertIn("672", html)
        # 缺失字段 -> 显式「未知」，且带 is-missing 标记
        self.assertIn("未知", html)
        self.assertIn("is-missing", html)

    def test_detail_modal_lora_states_are_distinguished(self):
        """详情模态框：LoRA 三种状态必须区分（recorded / none_used / not_recorded）。

        为什么这样断言：「确实没用 LoRA」与「没记录到」是两回事，
        混成一个字就是在撒谎。
        """
        result = self._run_node(self._EXTRACT_JS + r"""
const escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
const artifactLoraHtml = new Function('escapeHtml',
  grabFn('artifactLoraHtml') + '; return artifactLoraHtml;')(escapeHtml);
console.log(JSON.stringify({
  recorded: artifactLoraHtml({
    lora_status: 'recorded',
    loras: [{name: 'Turbo 4 步 · ckpt850', strength: 0.65,
             loader: 'MiniMaxH3TurboLoRA'}],
  }),
  noneUsed: artifactLoraHtml({lora_status: 'none_used', loras: []}),
  notRecorded: artifactLoraHtml({lora_status: 'not_recorded', loras: []}),
}));
""")
        self.assertIn("Turbo 4 步 · ckpt850", result["recorded"])
        self.assertIn("0.65", result["recorded"])
        self.assertIn("MiniMaxH3TurboLoRA", result["recorded"])
        self.assertIn("未使用 LoRA", result["noneUsed"])
        self.assertIn("未记录", result["notRecorded"])
        # 三者互不相同
        self.assertNotEqual(result["noneUsed"], result["notRecorded"])

    def test_detail_modal_loads_from_artifact_meta_endpoint(self):
        """详情模态框必须从 /api/jobs/{id}/artifact-meta 取数据。

        为什么这样断言：数据源必须是「产物实际元数据」接口，
        而不是继续读卡片摘要（那是把 BUG 2 原样搬过去）。
        """
        result = self._run_node(self._EXTRACT_JS + r"""
const src2 = fs.readFileSync('web/app.js', 'utf8');
console.log(JSON.stringify({
  hasEndpoint: src2.includes('/artifact-meta'),
  loadsAsync: src2.includes('loadArtifactMeta(job.id)'),
}));
""")
        self.assertTrue(result["hasEndpoint"])
        self.assertTrue(result["loadsAsync"])


if __name__ == "__main__":
    unittest.main()
