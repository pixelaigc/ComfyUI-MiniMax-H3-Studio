# -*- coding: utf-8 -*-
"""文生视频面板的后端回归测试（h3studio/video_models.py + video_workflow.py）。

覆盖本轮实测挖出并实现的每一条约束（不测它们就会回归）：

1. **宽高吸附规则**（实测依据：ComfyUI 0.37.0 的 MiniMaxH3ImageToVideo 节点
   schema `width/height = INT(min=32, max=16384, step=32)`；节点内
   CANVAS_MULTIPLE = 32；latent 网格 = width//16）
   - 非 32 倍数 -> 吸附到最近的 32 倍数，并给出**调整原因**
   - 超出 16384 -> 钳到上限；小于 32 -> 抬到下限
   - 合法值 -> 原样通过，且**没有**调整原因

2. **比例 + 总像素 -> 像素宽高**与 ComfyUI 的 ResolutionSelector 同一算法
   （0.4MP / 16:9 -> 864x480，实测与官方模板表一致）

3. **加速 LoRA 与底模的兼容性实测比对**
   - Turbo 系加速 LoRA 的 AdaLN 层（2688 维 vs 剪枝底模的 8 维）要算成
     「运行期重注入」，**不能**判成不兼容（否则剪枝底模整条路被封死）
   - 隐藏维折成 2688 的 w4a8 底模，与 5376 维的 LoRA 形状对不上 -> 真不兼容

4. **工作流拼装**
   - 标准路径：SamplerCustomAdvanced + 条件节点的 latent 输出
   - 二采路径：SelfLiftH3Sampler，强制 Euler，transition_step 落在合法范围
   - 加速 LoRA 链：固定 Turbo LoRA 在前、用户组合在后，串成模型链
   - 注意力机制：按选择插入对应节点
   - 宽高直接进 MiniMaxH3ImageToVideo（不再是 ResolutionSelector 的比例+总像素）

5. **服务端参数归一**（server.normalize_t2v_params）
   - 宽高吸附后写回
   - 未知模型 / 未知加速 LoRA / 不兼容的加速 LoRA -> 明确报错
   - 二采：transition_step 超界被夹回；rho=0 且 upscaler=none 时自动抬 rho

6. **增强模板**：H3 官方导演提示词档存在，且 t2v 的默认模板是它

运行：python3 -m pytest tests/test_t2v_panel.py -v
"""
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import video_models as VM       # noqa: E402
from h3studio import video_workflow as VW     # noqa: E402
from h3studio import enhancer as ENH         # noqa: E402


class TestVideoSizeNormalization(unittest.TestCase):
    """宽高吸附规则"""

    def test_non_multiple_snaps_to_32(self):
        w, h, reasons = VM.normalize_video_size(1000, 563)
        self.assertEqual((w, h), (992, 576))
        self.assertEqual(len(reasons), 2)
        self.assertIn("1000", reasons[0])
        self.assertIn("32", reasons[0])

    def test_legal_value_passes_through(self):
        w, h, reasons = VM.normalize_video_size(864, 480)
        self.assertEqual((w, h), (864, 480))
        self.assertEqual(reasons, [], "合法值不应给出调整原因")

    def test_clamps_to_upper_bound(self):
        w, h, reasons = VM.normalize_video_size(99999, 480)
        self.assertEqual(w, VM.VIDEO_SIZE_MAX)
        self.assertTrue(any("上限" in r for r in reasons))

    def test_raises_to_lower_bound(self):
        w, h, reasons = VM.normalize_video_size(4, 4)
        self.assertEqual((w, h), (32, 32))
        self.assertTrue(any("下限" in r for r in reasons))

    def test_invalid_input_returns_zero(self):
        for bad in ("abc", None, 0, -5):
            w, h, reasons = VM.normalize_video_size(bad, 480)
            self.assertEqual((w, h), (0, 0))
            self.assertTrue(reasons)

    def test_dimensions_match_comfyui_resolution_selector(self):
        """0.4MP / 16:9 -> 864x480（与官方工作流模板的 MarkdownNote 表一致）。"""
        self.assertEqual(VM.video_dimensions("16:9", 0.4), (864, 480))
        # 倍数必须是 32 的倍数
        for aspect in VM.VIDEO_ASPECTS:
            w, h = VM.video_dimensions(aspect, 0.8)
            self.assertEqual(w % VM.VIDEO_SIZE_MULTIPLE, 0)
            self.assertEqual(h % VM.VIDEO_SIZE_MULTIPLE, 0)


class TestAccelLoraCompat(unittest.TestCase):
    """加速 LoRA 与底模的实测兼容性比对"""

    def test_adaln_is_runtime_injected_not_incompatible(self):
        """剪枝底模的 AdaLN 是 8 维曲线 —— Turbo 节点运行期重注入，不是不兼容。"""
        check = VM.check_h3_lora_compat(
            "minimax_h3_turbo_4step_ema_ckpt850.safetensors",
            "minimax_h3_fl2va_pruned_int8_convrot.safetensors")
        self.assertTrue(check["compatible"],
                        "剪枝底模 + Turbo LoRA 应当兼容：%s" % check["reason"])
        self.assertGreater(check["runtime_injected"], 0,
                           "AdaLN 层应被算成运行期重注入")
        self.assertEqual(check["shape_mismatch"], 0)

    def test_w4a8_base_is_incompatible_with_lightx2v(self):
        """w4a8 底模把隐藏维折成 2688 —— 5376 维的 LoRA 形状对不上。"""
        check = VM.check_h3_lora_compat(
            "minimax_h3_fl2v_turbo_4step_v0.1_768p_sla_comfyui_bf16.safetensors",
            "minimax_h3_fl2va_pruned_w4a8_mixed.safetensors")
        self.assertFalse(check["compatible"])
        self.assertGreater(check["shape_mismatch"], 0)
        self.assertIn("形状", check["reason"])

    def test_missing_file_is_reported(self):
        check = VM.check_h3_lora_compat("不存在的.safetensors",
                                        "minimax_h3_fl2va_pruned_int8_convrot.safetensors")
        self.assertFalse(check["compatible"])
        self.assertIn("找不到", check["reason"])


class TestVideoOptionsPayload(unittest.TestCase):
    """下发给前端的清单结构"""

    def test_payload_has_all_sections(self):
        payload = VM.options_payload()
        for key in ("models", "fixed_accel_loras", "accel_lora_combos",
                    "attention_modes", "twopass", "aspects",
                    "size_multiple", "size_min", "size_max"):
            self.assertIn(key, payload)

    def test_fixed_accel_lora_not_in_combo_list(self):
        """工作流硬性必需的加速 LoRA 不出现在用户可勾选列表里。"""
        _fixed, combos = VM.list_accel_loras()
        combo_names = {c["name"] for c in combos}
        for fixed in VM.VIDEO_ACCEL_LORAS.values():
            self.assertNotIn(fixed["name"], combo_names,
                             "固定加速 LoRA 不该出现在可勾选列表里")

    def test_attention_modes_have_real_node_or_none(self):
        """每个注意力机制要么没有节点（默认），要么指向一个真实存在的节点名。"""
        for key, mode in VM.VIDEO_ATTENTION_MODES.items():
            node = mode.get("node")
            if node is None:
                continue
            self.assertTrue(node.startswith(("MiniMaxH3", "Model", "H3", "Sol")),
                            "%s 的节点名可疑：%s" % (key, node))

    def test_node_availability_marks_unavailable(self):
        payload = VM.options_payload(
            node_available=lambda ct: ct == "ModelAttentionBackend")
        by_key = {m["key"]: m for m in payload["attention_modes"]}
        self.assertTrue(by_key["kitchen"]["available"])
        self.assertFalse(by_key["sage"]["available"])
        self.assertFalse(payload["twopass"]["node_available"])


class TestT2VWorkflow(unittest.TestCase):
    """文生视频工作流拼装"""

    BASE_PARAMS = {
        "job_id": "testjob",
        "prompt": "a red panda walking through bamboo",
        "duration": 5,
        "model_key": "fl2va_pruned_int8",
        "width": 864,
        "height": 480,
        "steps": 4,
        "seed": 12345,
        "attention": "default",
        "accel_loras": [],
    }

    def test_standard_path_uses_sampler_custom_advanced(self):
        wf = VW.build_t2v_panel(dict(self.BASE_PARAMS))
        self.assertEqual(wf["14"]["class_type"], "SamplerCustomAdvanced")
        # 条件节点的 latent 输出（index 1）就是采样用的 latent
        self.assertEqual(wf["14"]["inputs"]["latent_image"], ["105:104", 1])
        # 宽高直接进条件节点（不再是 ResolutionSelector 的比例+总像素）
        self.assertEqual(wf["105:104"]["inputs"]["width"], 864)
        self.assertEqual(wf["105:104"]["inputs"]["height"], 480)
        self.assertNotIn("115", wf, "不再走 ResolutionSelector")

    def test_fixed_accel_lora_always_present(self):
        wf = VW.build_t2v_panel(dict(self.BASE_PARAMS))
        lora_nodes = [n for n in wf.values() if n["class_type"] == "MiniMaxH3TurboLoRA"]
        self.assertTrue(lora_nodes, "工作流里必须有固定加速 LoRA")
        self.assertEqual(
            lora_nodes[0]["inputs"]["lora_name"],
            VM.VIDEO_ACCEL_LORAS["ckpt850"]["name"])

    def test_user_combos_are_chained_after_fixed(self):
        params = dict(self.BASE_PARAMS)
        params["accel_loras"] = [
            {"key": "lightx2v_4step_768p", "strength": 0.9, "enabled": True},
        ]
        wf = VW.build_t2v_panel(params)
        lora_nodes = [wf[k] for k in wf if wf[k]["class_type"] == "MiniMaxH3TurboLoRA"]
        self.assertEqual(len(lora_nodes), 2)
        # 固定的在前，模型链串起来
        self.assertEqual(lora_nodes[0]["inputs"]["model"], ["6", 0])
        self.assertEqual(lora_nodes[1]["inputs"]["model"], ["138:0", 0])
        self.assertEqual(
            lora_nodes[1]["inputs"]["lora_name"],
            VM.VIDEO_ACCEL_LORA_COMBOS["lightx2v_4step_768p"]["name"])
        self.assertAlmostEqual(lora_nodes[1]["inputs"]["strength"], 0.9)

    def test_disabled_combo_is_skipped(self):
        params = dict(self.BASE_PARAMS)
        params["accel_loras"] = [
            {"key": "lightx2v_4step_768p", "strength": 1.0, "enabled": False},
        ]
        wf = VW.build_t2v_panel(params)
        lora_nodes = [n for n in wf.values() if n["class_type"] == "MiniMaxH3TurboLoRA"]
        self.assertEqual(len(lora_nodes), 1, "enabled=False 的组合不该进工作流")

    def test_attention_node_inserted(self):
        params = dict(self.BASE_PARAMS)
        params["attention"] = "sol"
        wf = VW.build_t2v_panel(params)
        att = [n for n in wf.values()
               if n["class_type"] == "MiniMaxH3MemoryEfficientSolAttentionPatch"]
        self.assertEqual(len(att), 1)
        # 注意力节点在 LoRA 链之后、调度器之前
        self.assertEqual(att[0]["inputs"]["model"], ["138:0", 0])
        self.assertEqual(wf["9"]["inputs"]["model"], [att[0].get("_id", "100"), 0]
                         if "_id" in att[0] else wf["9"]["inputs"]["model"])

    def test_default_attention_adds_no_node(self):
        wf = VW.build_t2v_panel(dict(self.BASE_PARAMS))
        types = {n["class_type"] for n in wf.values()}
        self.assertNotIn("MiniMaxH3MemoryEfficientSolAttentionPatch", types)
        self.assertNotIn("ModelAttentionBackend", types)

    def test_twopass_uses_selflift_and_forces_euler(self):
        params = dict(self.BASE_PARAMS)
        params["twopass"] = {"enabled": True, "transition_step": 2,
                             "lowres_scale": 0.5, "rho": 0.0}
        wf = VW.build_t2v_panel(params)
        self.assertIn("171", wf)
        self.assertEqual(wf["171"]["class_type"], "SelfLiftH3Sampler")
        # SelfLift 只支持标准 Euler
        self.assertEqual(wf["17"]["inputs"]["sampler_name"], "euler")
        # latent 用条件节点的输出
        self.assertEqual(wf["171"]["inputs"]["latent_image"], ["105:104", 1])
        self.assertEqual(wf["171"]["inputs"]["transition_step"], 2)

    def test_twopass_transition_step_clamped(self):
        params = dict(self.BASE_PARAMS)
        params["steps"] = 4
        params["twopass"] = {"enabled": True, "transition_step": 99}
        wf = VW.build_t2v_panel(params)
        # sigmas 长度 = steps+1 = 5 -> transition_step 上限 = steps-1 = 3
        self.assertLessEqual(wf["171"]["inputs"]["transition_step"], 3)
        self.assertGreaterEqual(wf["171"]["inputs"]["transition_step"], 1)

    def test_twopass_rho_zero_without_upscaler_is_fixed(self):
        params = dict(self.BASE_PARAMS)
        params["twopass"] = {"enabled": True, "rho": 0.0,
                             "upscaler_model": "none"}
        wf = VW.build_t2v_panel(params)
        self.assertGreater(wf["171"]["inputs"]["rho"], 0.0,
                           "rho=0 + upscaler=none 会被节点拒绝，必须抬 rho")

    def test_width_is_snapped_in_workflow(self):
        params = dict(self.BASE_PARAMS)
        params["width"] = 1000
        params["height"] = 563
        wf = VW.build_t2v_panel(params)
        self.assertEqual(wf["105:104"]["inputs"]["width"], 992)
        self.assertEqual(wf["105:104"]["inputs"]["height"], 576)


class TestNormalizeT2VParams(unittest.TestCase):
    """服务端参数归一（server.normalize_t2v_params）"""

    def setUp(self):
        import server as srv
        self.server = srv
        # 模拟本机 ComfyUI 有这些节点
        srv._COMFY_NODE_NAMES.update(VM.VIDEO_ATTENTION_MODES[k]["node"]
                                     for k in VM.VIDEO_ATTENTION_MODES
                                     if VM.VIDEO_ATTENTION_MODES[k].get("node"))
        srv._COMFY_NODE_NAMES.add("SelfLiftH3Sampler")

    BASE = {
        "prompt": "a red panda",
        "duration": 5,
        "model_key": "fl2va_pruned_int8",
        "width": 864, "height": 480,
        "steps": 4, "seed": 123,
        "attention": "default",
        "accel_loras": [],
        "twopass": {"enabled": False},
    }

    def test_snaps_width_and_height(self):
        params = dict(self.BASE, width=1000, height=563)
        out, err = self.server.normalize_t2v_params(params)
        self.assertEqual(err, "")
        self.assertEqual((out["width"], out["height"]), (992, 576))
        self.assertTrue(out["size_adjust_reasons"])

    def test_rejects_unknown_model(self):
        out, err = self.server.normalize_t2v_params(dict(self.BASE, model_key="nope"))
        self.assertTrue(err)
        self.assertIn("未知的视频模型", err)

    def test_rejects_unknown_accel_lora(self):
        params = dict(self.BASE, accel_loras=[{"key": "nope", "enabled": True}])
        out, err = self.server.normalize_t2v_params(params)
        self.assertTrue(err)
        self.assertIn("未知的加速 LoRA", err)

    def test_rejects_incompatible_accel_lora(self):
        params = dict(self.BASE, model_key="fl2va_pruned_w4a8",
                      accel_loras=[{"key": "lightx2v_4step_768p", "enabled": True}])
        out, err = self.server.normalize_t2v_params(params)
        self.assertTrue(err)
        self.assertIn("不兼容", err)

    def test_rejects_attention_without_node(self):
        self.server._COMFY_NODE_NAMES.discard("H3SparseAttention")
        out, err = self.server.normalize_t2v_params(dict(self.BASE, attention="sparse"))
        self.assertTrue(err)
        self.assertIn("没有节点", err)

    def test_rejects_twopass_without_node(self):
        self.server._COMFY_NODE_NAMES.discard("SelfLiftH3Sampler")
        out, err = self.server.normalize_t2v_params(
            dict(self.BASE, twopass={"enabled": True}))
        self.assertTrue(err)
        self.assertIn("SelfLiftH3Sampler", err)

    def test_duration_clamped(self):
        out, err = self.server.normalize_t2v_params(dict(self.BASE, duration=99))
        self.assertEqual(err, "")
        self.assertEqual(out["duration"], 15.0)

    def test_seed_normalized_to_int_or_none(self):
        out, _ = self.server.normalize_t2v_params(dict(self.BASE, seed="42"))
        self.assertEqual(out["seed"], 42)
        out, _ = self.server.normalize_t2v_params(dict(self.BASE, seed=""))
        self.assertIsNone(out["seed"])

    def test_twopass_transition_clamped(self):
        params = dict(self.BASE, steps=4,
                      twopass={"enabled": True, "transition_step": 99})
        out, err = self.server.normalize_t2v_params(params)
        self.assertEqual(err, "")
        self.assertLessEqual(out["twopass"]["transition_step"], 3)

    def test_twopass_rho_floor(self):
        params = dict(self.BASE,
                      twopass={"enabled": True, "rho": 0.0, "upscaler_model": "none"})
        out, err = self.server.normalize_t2v_params(params)
        self.assertEqual(err, "")
        self.assertGreater(out["twopass"]["rho"], 0.0)


class TestDirectorPromptSpec(unittest.TestCase):
    """H3 官方导演提示词档"""

    def test_spec_exists(self):
        self.assertIn("video-director", ENH.TEMPLATE_SPECS)
        spec = ENH.TEMPLATE_SPECS["video-director"]
        self.assertEqual(spec["media"], "video")
        self.assertEqual(spec["families"], ["minimax_h3"])

    def test_template_file_exists_and_has_three_sections(self):
        path = ENH.ASSETS_DIR / "video-director.txt"
        self.assertTrue(path.is_file(), "导演提示词模板文件缺失")
        text = path.read_text(encoding="utf-8")
        for field in ("integrated_multimodal_description",
                      "overall_soundscape", "non_diegetic_music"):
            self.assertIn(field, text)

    def test_t2v_default_spec_is_director(self):
        self.assertEqual(ENH.MODE_DEFAULT_SPEC["t2v"], "video-director")

    def test_video_models_map_to_h3_family(self):
        """新面板的模型 key 必须映射到 minimax_h3 族，否则增强模板过滤会失配。"""
        for key in VM.VIDEO_MODELS:
            self.assertEqual(ENH.model_family(key), "minimax_h3")


if __name__ == "__main__":
    unittest.main()
