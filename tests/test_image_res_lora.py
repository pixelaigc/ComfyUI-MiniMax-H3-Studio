# -*- coding: utf-8 -*-
"""图像面板「自定义分辨率 + 效果 LoRA 语义」的回归测试。

覆盖本轮实测挖出并修复/新增的行为（不测它们就会回归）：

1. **宽高吸附规则**（实测依据：ComfyUI 0.37.0 的 EmptySD3LatentImage
   节点 schema `width/height = INT(min=16, max=16384, step=16)`；
   latent 尺寸 = H//8, W//8，再经 UNET patch_size=2 -> 有效网格步长 16）
   - 非 16 倍数 -> 吸附到最近的 16 倍数，并给出**调整原因**
   - 超出 16384 -> 钳到上限
   - 小于 16 -> 抬到下限
   - 合法值 -> 原样通过，且**没有**调整原因

2. **加速 LoRA 固定接在工作流里、且不在用户可选列表里**
   - 官方三份 Z-Image 模板的子图里**一个 LoRA 节点都没有**
     （Z-Image-Turbo 是原生蒸馏）
   - `z_image_base_distill` 这个模型版本的工作流里**有**加速 LoRA 节点
   - 加速 LoRA **不出现在** `/api/image-loras` 的 available（用户可选）里，
     只出现在 acceleration 里
   - 用户的**效果** LoRA 排在加速 LoRA **之后**

3. **Load LoRA 节点的 model 与 clip 两个输入都要接**
   （只接 model 的话文本编码器那半边的 patch 会被 ComfyUI 记为
   "NOT LOADED" 丢弃）

4. **触发词自动合并追加到最终提示词**，且用户改过的以用户的为准

5. **兼容性检测不能误判带 `diffusion_model.` 前缀的 LoRA**
   （实测 `dejpeg_v3.safetensors` 与官方蒸馏 LoRA 都带这个前缀；
   之前因为只拿裸键比对，全被判成「不兼容」，但它们在 ComfyUI 里
   明明 attach 成功 —— 180 / 136 patches attached）

运行：python3 -m pytest tests/test_image_res_lora.py -v
"""
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import workflows as W          # noqa: E402
from h3studio import loras as lora_registry  # noqa: E402


class TestSizeNormalization(unittest.TestCase):
    """宽高吸附规则"""

    def test_non_multiple_snaps_to_16(self):
        """非 16 倍数 -> 吸附，并说明原因"""
        w, h, reasons = W.normalize_z_image_size(2559, 1401)
        self.assertEqual((w, h), (2560, 1408))
        self.assertTrue(reasons, "必须给出调整原因（界面要显示给用户）")
        self.assertTrue(any("2559" in r for r in reasons))
        self.assertTrue(any("1401" in r for r in reasons))

    def test_valid_size_passes_through(self):
        """合法值原样通过，且没有调整原因"""
        w, h, reasons = W.normalize_z_image_size(2048, 1152)
        self.assertEqual((w, h), (2048, 1152))
        self.assertEqual(reasons, [], "合法值不该有任何调整原因")

    def test_clamps_to_max_resolution(self):
        """超过 16384（nodes.MAX_RESOLUTION）-> 钳到上限"""
        w, h, reasons = W.normalize_z_image_size(20000, 1000)
        self.assertEqual(w, W.Z_IMAGE_SIZE_MAX)
        self.assertTrue(any("上限" in r for r in reasons))

    def test_raises_to_min(self):
        """小于 16 -> 抬到下限"""
        w, h, reasons = W.normalize_z_image_size(8, 4)
        self.assertGreaterEqual(w, W.Z_IMAGE_SIZE_MIN)
        self.assertGreaterEqual(h, W.Z_IMAGE_SIZE_MIN)
        self.assertTrue(reasons)

    def test_rejects_non_positive(self):
        """非正数 -> 明确报错信息（不抛异常，界面要显示）"""
        w, h, reasons = W.normalize_z_image_size(0, -5)
        self.assertEqual((w, h), (0, 0))
        self.assertTrue(reasons)

    def test_size_multiple_matches_comfy_node_schema(self):
        """倍数必须是 16 —— 来自 EmptySD3LatentImage 的 step=16"""
        self.assertEqual(W.Z_IMAGE_SIZE_MULTIPLE, 16)
        self.assertEqual(W.Z_IMAGE_SIZE_MAX, 16384)   # nodes.MAX_RESOLUTION

    def test_z_image_dimensions_uses_normalization(self):
        """z_image_dimensions 手填宽高时也要走吸附"""
        w, h = W.z_image_dimensions("16:9", "1mp", 2559, 1401)
        self.assertEqual((w, h), (2560, 1408))


class TestAccelLoraIsWorkflowFixed(unittest.TestCase):
    """加速 LoRA 固定接在工作流里，界面不暴露"""

    def test_official_z_image_templates_have_no_lora_node(self):
        """官方 Z-Image 模板的子图里一个 LoRA 节点都没有。

        这是「Turbo 是原生蒸馏、不需要加速 LoRA」的事实依据。
        模板路径按本机 ComfyUI 的模板目录解析，找不到就跳过（本机没装
        模板包时不能判失败）。
        """
        import glob
        base = ("/home/ubuntu/comfyui/venv/lib/python3.13/site-packages/"
                "comfyui_workflow_templates_json/templates")
        tpls = [f"{base}/image_z_image_turbo.json",
                f"{base}/image_z_image.json",
                f"{base}/utility_z_image_turbo_2k_upscaler.app.json"]
        checked = 0
        for path in tpls:
            if not Path(path).is_file():
                continue
            checked += 1
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            subs = (data.get("definitions") or {}).get("subgraphs") or []
            types = [n.get("type") for sg in subs for n in sg.get("nodes", [])]
            lora_nodes = [t for t in types if "Lora" in (t or "")]
            self.assertEqual(lora_nodes, [],
                             f"{Path(path).name} 不该有 LoRA 节点（实测确实没有）")
        if checked == 0:
            self.skipTest("本机没装官方工作流模板包")

    def test_base_distill_workflow_has_accel_lora_node(self):
        """Base+蒸馏 这个模型版本的工作流里必须有加速 LoRA 节点"""
        wf = W.build_t2i({"job_id": "t", "model": "z_image_base_distill",
                          "prompt": "x", "width": 1024, "height": 1024})
        lora_nodes = {k: v for k, v in wf.items()
                      if v.get("class_type", "").startswith("LoraLoader")}
        self.assertEqual(len(lora_nodes), 1, "应当恰好挂 1 个加速 LoRA")
        node = list(lora_nodes.values())[0]
        self.assertIn("Distill", node["inputs"]["lora_name"])

    def test_turbo_workflow_has_no_accel_lora(self):
        """Turbo 是原生蒸馏 -> 工作流里不该有多余的加速 LoRA"""
        wf = W.build_t2i({"job_id": "t", "model": "z_image_turbo_bf16",
                          "prompt": "x", "width": 1024, "height": 1024})
        lora_nodes = [v for v in wf.values()
                      if v.get("class_type", "").startswith("LoraLoader")]
        self.assertEqual(lora_nodes, [], "Turbo 不该挂加速 LoRA")

    def test_accel_lora_never_in_user_selectable_list(self):
        """加速 LoRA 不在用户可选列表里（只在 acceleration 里）"""
        accel = {i["name"] for i in lora_registry.list_acceleration_loras()}
        if not accel:
            self.skipTest("本机没有加速类 LoRA")
        effect = {i["name"] for i in lora_registry.list_effect_loras()}
        self.assertFalse(accel & effect,
                         "加速类 LoRA 不能同时出现在用户可选列表里")

    def test_accel_lora_is_not_rejected_as_user_choice(self):
        """用户手动提交加速 LoRA 时必须被明确拒绝（而不是静默吞掉）"""
        name = "Z-Image-Fun-Lora-Distill-8-Steps-2603-ComfyUI.safetensors"
        if name not in {i["name"] for i in lora_registry.list_acceleration_loras()}:
            self.skipTest("本机没有该加速 LoRA")
        with self.assertRaises(ValueError) as ctx:
            W.build_t2i({"job_id": "t", "model": "z_image_turbo_bf16",
                         "prompt": "x", "width": 1024, "height": 1024,
                         "loras": [{"name": name, "strength": 1.0}]})
        self.assertIn("加速", str(ctx.exception))

    def test_accel_lora_comes_before_user_effect_lora(self):
        """链序：加速 LoRA 在前，用户效果 LoRA 在后"""
        effect_name = "Kook_Zimage_如梦似幻.safetensors"
        if effect_name not in {i["name"] for i in lora_registry.list_effect_loras()}:
            self.skipTest("本机没有该效果 LoRA")
        wf = W.build_t2i({
            "job_id": "t", "model": "z_image_base_distill", "prompt": "x",
            "width": 1024, "height": 1024,
            "loras": [{"name": effect_name, "strength": 1.0, "trigger": "如梦似幻"}]})
        names = [v["inputs"]["lora_name"] for v in wf.values()
                 if v.get("class_type", "").startswith("LoraLoader")]
        self.assertEqual(len(names), 2, "应当是 加速 + 效果 两个")
        self.assertIn("Distill", names[0], "加速 LoRA 必须在前面")
        self.assertEqual(names[1], effect_name)

    def test_accel_params_are_distill_tuned(self):
        """加速版本的步数/CFG/采样器必须是蒸馏档"""
        m = W.Z_IMAGE_MODELS["z_image_base_distill"]
        self.assertEqual(m["steps"], 8)
        self.assertEqual(m["cfg"], 1.0)
        self.assertTrue(m["params_locked_by_accel"])


class TestLoraLoaderWiring(unittest.TestCase):
    """Load LoRA 节点的 model 与 clip 两个输入都要接"""

    def test_lora_node_has_both_model_and_clip(self):
        """LoraLoader 的 model 与 clip 输入都要有值"""
        wf = W.build_t2i({
            "job_id": "t", "model": "z_image_turbo_bf16", "prompt": "x",
            "width": 1024, "height": 1024,
            "loras": [{"name": "Kook_Zimage_如梦似幻.safetensors",
                       "strength": 1.0, "trigger": "如梦似幻"}]})
        lora_nodes = {k: v for k, v in wf.items()
                      if v.get("class_type", "").startswith("LoraLoader")}
        if not lora_nodes:
            self.skipTest("本机没有该效果 LoRA")
        for node_id, node in lora_nodes.items():
            self.assertIn("model", node["inputs"], f"{node_id} 缺 model 输入")
            self.assertIn("clip", node["inputs"], f"{node_id} 缺 clip 输入")

    def test_clip_text_encode_uses_post_lora_clip(self):
        """CLIPTextEncode 的 clip 输入要接 LoRA 之后的 clip"""
        wf = W.build_t2i({
            "job_id": "t", "model": "z_image_turbo_bf16", "prompt": "x",
            "width": 1024, "height": 1024,
            "loras": [{"name": "Kook_Zimage_如梦似幻.safetensors",
                       "strength": 1.0, "trigger": "如梦似幻"}]})
        lora_ids = [k for k, v in wf.items()
                    if v.get("class_type", "").startswith("LoraLoader")]
        if not lora_ids:
            self.skipTest("本机没有该效果 LoRA")
        last = lora_ids[-1]
        self.assertEqual(wf["57:27"]["inputs"]["clip"], [last, 1],
                         "CLIPTextEncode 的 clip 必须接最后一个 LoRA 的 clip 输出")

    def test_clip_chain_is_continuous(self):
        """多个 LoRA 时，clip 也要一路串下去（不能每个都接原始 CLIP）"""
        wf = W.build_t2i({
            "job_id": "t", "model": "z_image_base_distill", "prompt": "x",
            "width": 1024, "height": 1024,
            "loras": [{"name": "Kook_Zimage_如梦似幻.safetensors",
                       "strength": 1.0, "trigger": "如梦似幻"}]})
        lora_ids = sorted(k for k, v in wf.items()
                          if v.get("class_type", "").startswith("LoraLoader"))
        if len(lora_ids) < 2:
            self.skipTest("本机没有足够的效果 LoRA")
        for prev, cur in zip(lora_ids, lora_ids[1:]):
            self.assertEqual(wf[cur]["inputs"]["model"], [prev, 0],
                             f"{cur} 的 model 应接 {prev}")
            self.assertEqual(wf[cur]["inputs"]["clip"], [prev, 1],
                             f"{cur} 的 clip 应接 {prev}")


class TestTriggerMerge(unittest.TestCase):
    """触发词自动合并追加"""

    def test_trigger_appended_to_prompt(self):
        out = W.merge_lora_triggers(
            "a lighthouse", [{"name": "x", "strength": 1.0, "trigger": "如梦似幻"}])
        self.assertIn("如梦似幻", out)
        self.assertTrue(out.startswith("a lighthouse"))

    def test_multiple_triggers_all_merged(self):
        out = W.merge_lora_triggers("base", [
            {"name": "a", "strength": 1.0, "trigger": "如梦似幻"},
            {"name": "b", "strength": 1.0, "trigger": "瑶光 / 真实幻想"},
        ])
        for token in ("如梦似幻", "瑶光", "真实幻想"):
            self.assertIn(token, out)

    def test_no_duplicate_when_already_present(self):
        out = W.merge_lora_triggers(
            "a lighthouse, 如梦似幻", [{"name": "x", "strength": 1.0,
                                        "trigger": "如梦似幻"}])
        self.assertEqual(out.count("如梦似幻"), 1)

    def test_empty_trigger_leaves_prompt_untouched(self):
        out = W.merge_lora_triggers("base", [{"name": "x", "strength": 1.0,
                                              "trigger": ""}])
        self.assertEqual(out, "base")

    def test_user_edited_trigger_wins(self):
        """用户改过的触发词以用户的为准（前端传了就用前端的）"""
        out = W.merge_lora_triggers(
            "base", [{"name": "x", "strength": 1.0, "trigger": "我自己的词"}])
        self.assertIn("我自己的词", out)
        self.assertNotIn("如梦似幻", out)


class TestCompatibilityNotFalseNegative(unittest.TestCase):
    """兼容性检测不能误判带 diffusion_model. 前缀的 LoRA"""

    def test_unet_keys_carry_diffusion_model_prefix(self):
        """UNET 的键集必须带 diffusion_model. 前缀（跟 ComfyUI 运行时一致）"""
        unet = lora_registry._z_image_unet_path()
        if unet is None:
            self.skipTest("本机没有 Z-Image UNET")
        keys = lora_registry._unet_layer_keys(unet)
        self.assertTrue(keys)
        self.assertTrue(all(k.startswith("diffusion_model.") for k in keys),
                        "UNET 键集应统一带 diffusion_model. 前缀")

    def test_layer_hit_accepts_both_prefix_styles(self):
        """带前缀与不带前缀的层名都要能命中"""
        unet = lora_registry._z_image_unet_path()
        if unet is None:
            self.skipTest("本机没有 Z-Image UNET")
        keys = lora_registry._unet_layer_keys(unet)
        bare = next(k[len("diffusion_model."):] for k in keys
                    if k.endswith(".attention.qkv"))
        self.assertTrue(lora_registry._layer_hit(bare, keys),
                        "不带前缀的写法也要命中")
        self.assertTrue(lora_registry._layer_hit("diffusion_model." + bare, keys),
                        "带前缀的写法也要命中")


if __name__ == "__main__":
    unittest.main()
