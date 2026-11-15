# -*- coding: utf-8 -*-
"""
修复① 回归测试：PE 动作全量外置到 WEBUI + H3-IMAGE 专属 PE 模板。

跑法：python3 -m pytest tests/test_pe_externalize.py -q

【本文件验证的六条硬约束】
  1. Qwen-Image 2.1 文生图工作流内**不存在**任何 PE 节点
     （TextGenerate / BatchImagesNode / PE 用的 CLIPLoader）。
  2. Qwen-Image 2.1 图片编辑工作流同上，且参考图接线仍然完整
     （images.image_N -> LoadImage）—— 移除 PE 不能把参考图链路弄断。
  3. 其它所有工作流（Z-Image / MOSS / H3-Image / YuE2 / 视频）内**不存在** PE 动作。
  4. 增强下拉**没有**「独立勾选框」形式的 PE 选项：
     - 后端 Agent 默认参数里不再有 use_pe 布尔字段；
     - 前端出图参数里不再提交 use_pe；
     - 官方 PE 只能是「增强」下拉里的一项。
  5. H3-IMAGE 文生图 / 图片编辑的默认增强模板是**自己的官方 PE 档**，
     不再是通用「物体细化（二次增强）」。
  6. H3-Image 官方 PE 的工作流接法与官方模板一致：
     - 占位符是 "{input text}"（不是 HF 仓库那份的 "input_text: {}"）；
     - 文生图取 aspect_ratio，图片编辑取 target_aspect_ratio；
     - task prompt 正文与官方 ComfyUI 模板逐字节一致。
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from h3studio import enhancer as E          # noqa: E402
from h3studio import gen_defaults            # noqa: E402
from h3studio import h3image as H3IMG        # noqa: E402
from h3studio import h3image_pe as H3PE      # noqa: E402
from h3studio import workflows as W          # noqa: E402


#: 工作流里任何形态的 PE 节点（扩写动作）
PE_CLASS_TYPES = ("TextGenerate", "TextGenerateV2", "BatchImagesNode")


def workflow_class_types(wf: dict) -> set:
    return {node.get("class_type") for node in wf.values()}


class TestQwenWorkflowHasNoPE(unittest.TestCase):
    """约束 1 + 2：Qwen 两个工作流内不得有任何 PE 动作。"""

    def _t2i(self):
        return W.build_qwen_t2i({
            "prompt": "a cat on a desk", "job_id": "pe-t1", "seed": 1,
            "aspect": "1:1", "resolution": "1mp",
        })

    def _i2i(self):
        return W.build_qwen_i2i({
            "prompt": "make it blue", "job_id": "pe-t2", "seed": 1,
            "aspect": "1:1", "resolution": "1mp",
            "ref_images": [{"file": "h3studio/ref.png"}],
        })

    def test_t2i_workflow_has_no_pe_node(self):
        wf = self._t2i()
        ct = workflow_class_types(wf)
        for pe in PE_CLASS_TYPES:
            self.assertNotIn(pe, ct,
                             f"Qwen 文生图工作流里仍有 PE 节点 {pe}: {sorted(ct)}")

    def test_i2i_workflow_has_no_pe_node(self):
        wf = self._i2i()
        ct = workflow_class_types(wf)
        for pe in PE_CLASS_TYPES:
            self.assertNotIn(pe, ct,
                             f"Qwen 图片编辑工作流里仍有 PE 节点 {pe}: {sorted(ct)}")

    def test_t2i_has_no_pe_clip_loader(self):
        """PE 专用 CLIPLoader（qw:30）必须消失。

        只查 class_type 不够 —— CLIPLoader 本身是条件编码器也要用。
        判据：没有任何节点的 clip_name 指向官方 PE 权重。
        """
        wf = self._t2i()
        for node_id, node in wf.items():
            clip_name = (node.get("inputs") or {}).get("clip_name")
            if clip_name:
                self.assertNotIn("_pe_t2i", clip_name,
                                 f"{node_id} 仍加载官方 PE 权重 {clip_name}")
                self.assertNotIn("_pe_i2i", clip_name,
                                 f"{node_id} 仍加载官方 PE 权重 {clip_name}")

    def test_t2i_prompt_reaches_encoder_verbatim(self):
        """提示词必须**原样**进编码器（扩写只发生在 WEBUI 侧）。"""
        wf = self._t2i()
        prompt_input = wf["qw:27"]["inputs"]["prompt"]
        self.assertEqual(prompt_input, "a cat on a desk",
                         "提示词没有原样进 TextEncodeQwenImage21")
        # 不能是某个节点的输出引用（那就是工作流内又做了一次转换）
        self.assertIsInstance(prompt_input, str)

    def test_i2i_prompt_reaches_encoder_verbatim(self):
        wf = self._i2i()
        prompt_input = wf["qw:27"]["inputs"]["prompt"]
        self.assertEqual(prompt_input, "make it blue")
        self.assertIsInstance(prompt_input, str)

    def test_i2i_reference_image_wiring_intact(self):
        """约束 2：移除 PE 不能把参考图链路弄断。

        官方 PE-I2I 会把参考图也喂给扩写节点；PE 移除后，
        参考图**仍必须**接进 TextEncodeQwenImage21 的 images 输入，
        且用的是扁平点号键（写错键名参考图会被静默丢弃）。
        """
        wf = self._i2i()
        te_inputs = wf["qw:27"]["inputs"]
        self.assertIn("images.image_1", te_inputs,
                      "图片编辑的参考图没有接进 TextEncodeQwenImage21")
        self.assertEqual(te_inputs["images.image_1"], ["qw:ref0", 0])
        self.assertEqual(wf["qw:ref0"]["class_type"], "LoadImage")
        # 不能出现嵌套写法（会被 ComfyUI 静默丢弃参考图）
        self.assertNotIn("images", te_inputs,
                         "images 必须是扁平点号键，不是嵌套字典")

    def test_use_pe_param_is_ignored(self):
        """即便调用方仍传 use_pe=True，工作流里也不能出现 PE 节点。

        这是「职责单一化」的结构性保证：不是开关状态的问题，
        是工作流里根本没有 PE 这条路。
        """
        wf = W.build_qwen_t2i({
            "prompt": "x", "job_id": "pe-t3", "seed": 1, "use_pe": True,
        })
        ct = workflow_class_types(wf)
        self.assertFalse([c for c in ct if c in PE_CLASS_TYPES],
                         f"use_pe=True 仍然拉起了 PE 节点: {sorted(ct)}")


class TestOtherWorkflowsHaveNoPE(unittest.TestCase):
    """约束 3：其它工作流内一律不得有 PE 动作。"""

    def test_moss_workflow_has_no_pe(self):
        wf = W.build_moss_t2i({
            "prompt": "a cat", "job_id": "pe-m1", "seed": 1,
        })
        ct = workflow_class_types(wf)
        for pe in PE_CLASS_TYPES:
            self.assertNotIn(pe, ct, f"MOSS 工作流里仍有 PE 节点 {pe}")

    def test_h3image_workflow_has_no_pe(self):
        wf = H3IMG.build_h3_image_t2i({
            "prompt": "a cat", "job_id": "pe-h1", "seed": 1,
        })
        ct = workflow_class_types(wf)
        for pe in PE_CLASS_TYPES:
            self.assertNotIn(pe, ct, f"H3-Image 工作流里仍有 PE 节点 {pe}")

    def test_h3image_i2i_workflow_has_no_pe(self):
        wf = H3IMG.build_h3_image_i2i({
            "prompt": "a cat", "job_id": "pe-h2", "seed": 1,
            "ref_images": [{"file": "h3studio/ref.png"}],
        })
        ct = workflow_class_types(wf)
        for pe in PE_CLASS_TYPES:
            self.assertNotIn(pe, ct, f"H3-Image 图片编辑工作流里仍有 PE 节点 {pe}")


class TestNoIndependentPeCheckbox(unittest.TestCase):
    """约束 4：官方 PE 只能是「增强」下拉里的一项，不得再有独立勾选框。"""

    def test_agent_defaults_have_no_use_pe_boolean(self):
        """Agent 默认参数面板里的「启用官方 PE 扩写」勾选框必须消失。"""
        spec = gen_defaults.spec()
        for mode in ("t2i_qwen", "i2i_qwen"):
            fields = spec.get(mode, {}).get("fields") or []
            names = {f.get("name") for f in fields}
            self.assertNotIn(
                "use_pe", names,
                f"{mode} 的默认参数里仍有 use_pe 勾选框（独立 PE 开关）")

    def test_no_spec_mentions_use_pe(self):
        """任何模板 spec 都不该声明 use_pe 参数。"""
        for spec_id, spec in E.TEMPLATE_SPECS.items():
            self.assertNotIn("use_pe", spec,
                             f"模板 {spec_id} 仍声明 use_pe")


class TestH3ImageDedicatedPETemplate(unittest.TestCase):
    """约束 5：H3-IMAGE 两个面板的默认增强模板是自己的官方 PE 档。"""

    def _default(self, mode):
        specs = E.filter_specs(E.list_specs(), mode)
        spec_id = E.default_spec_for_mode(mode, specs, "h3_image_pruned_int8")
        return spec_id, E.get_spec(spec_id)

    def test_t2i_default_is_h3image_official_pe(self):
        spec_id, spec = self._default("t2i_h3image")
        self.assertEqual(spec_id, "h3image-pe-t2i",
                         "H3-Image 文生图的默认增强模板不是自己的官方 PE 档")
        self.assertIn("H3-Image", spec["label"])

    def test_i2i_default_is_h3image_official_pe(self):
        spec_id, spec = self._default("i2i_h3image")
        self.assertEqual(spec_id, "h3image-pe-i2i",
                         "H3-Image 图片编辑的默认增强模板不是自己的官方 PE 档")
        self.assertIn("H3-Image", spec["label"])

    def test_default_is_not_generic_refine(self):
        """回归用户报的那个现象：默认项曾经是「物体细化（二次增强）」。"""
        for mode in ("t2i_h3image", "i2i_h3image"):
            spec_id, spec = self._default(mode)
            self.assertNotEqual(spec_id, "image-refine",
                                f"{mode} 的默认增强模板退回成了通用二次增强")
            self.assertNotIn("物体细化", spec.get("label", ""),
                             f"{mode} 的默认增强模板标签仍是「物体细化」")

    def test_h3image_specs_are_family_locked(self):
        """H3-Image 专属档只能出现在 H3-Image 面板（families 锁 h3_image）。"""
        for spec_id in ("h3image-pe-t2i", "h3image-pe-i2i"):
            spec = E.TEMPLATE_SPECS[spec_id]
            self.assertEqual(spec.get("families"), ["h3_image"])
            # 别的模型族看不到它
            for other_mode, other_key in (("t2i_qwen", "qwen_image_2.1_bf16"),
                                          ("t2i_moss", "moss_image_base"),
                                          ("t2i", "z_image_turbo_bf16")):
                ids = [s["id"] for s in
                       E.filter_specs(E.list_specs(), other_mode, other_key)]
                self.assertNotIn(spec_id, ids,
                                 f"{spec_id} 泄漏到了 {other_mode} 面板")

    def test_other_panels_defaults_unchanged(self):
        """兼容性：MOSS / Qwen / Z-Image 的默认模板不能被这次改动带偏。"""
        cases = [
            ("t2i_qwen", "qwen_image_2.1_bf16", "qwen21-pe-t2i"),
            ("i2i_qwen", "qwen_image_2.1_bf16", "qwen21-pe-i2i"),
            ("t2i_moss", "moss_image_base", "moss-image"),
            ("t2i", "z_image_turbo_bf16", "image"),
        ]
        for mode, key, want in cases:
            specs = E.filter_specs(E.list_specs(), mode)
            got = E.default_spec_for_mode(mode, specs, key)
            self.assertEqual(got, want, f"{mode} 的默认模板被改动了")


class TestH3ImageOfficialPEWorkflow(unittest.TestCase):
    """约束 6：H3-Image 官方 PE 的接法与官方 ComfyUI 模板一致。"""

    def test_placeholder_is_official_one(self):
        """官方 ComfyUI 模板用 "{input text}"（不是 HF 仓库那份的 "input_text: {}"）。"""
        for is_edit in (False, True):
            wf = H3PE.build_pe_workflow("a cat", is_edit=is_edit)
            find = wf["pe:replace"]["inputs"]["find"]
            self.assertEqual(find, "{input text}",
                             f"is_edit={is_edit} 的占位符不是官方那个")
            task = wf["pe:task"]["inputs"]["value"]
            self.assertIn("{input text}", task)

    def test_ratio_key_differs_by_mode(self):
        """官方文生图用 aspect_ratio，官方图片编辑用 target_aspect_ratio。"""
        t2i = H3PE.build_pe_workflow("a cat", is_edit=False)
        i2i = H3PE.build_pe_workflow("a cat", is_edit=True)
        self.assertEqual(t2i["pe:json_ratio"]["inputs"]["key"], "aspect_ratio")
        self.assertEqual(i2i["pe:json_ratio"]["inputs"]["key"],
                         "target_aspect_ratio")

    def test_task_prompt_matches_official_template(self):
        """task prompt 正文必须与官方 ComfyUI 模板**逐字节一致**。

        判据：官方模板文件在站点包目录里（comfyui_workflow_templates_json）。
        找不到模板时跳过（本机没装该版本 ComfyUI 时不该让测试挂）。
        """
        import glob
        tpl_dir = glob.glob(
            "/home/ubuntu/comfyui/venv/lib/python3.*/site-packages/"
            "comfyui_workflow_templates_json/templates")
        if not tpl_dir:
            self.skipTest("本机没有 ComfyUI 官方模板包，无法逐字节比对")
        pairs = [
            ("image_minimax_h3_image_t2i.json", "t2i"),
            ("image_minimax_h3_image_edit.json", "i2i"),
        ]
        for filename, key in pairs:
            path = os.path.join(tpl_dir[0], filename)
            if not os.path.exists(path):
                self.skipTest(f"官方模板缺失: {filename}")
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            # 文生图模板的 task prompt 在子图里；编辑模板在顶层节点
            candidates = []
            for sg in data.get("definitions", {}).get("subgraphs", []) or []:
                candidates.extend(sg.get("nodes", []) or [])
            candidates.extend(data.get("nodes", []) or [])
            official = ""
            for node in candidates:
                if node.get("type") != "PrimitiveStringMultiline":
                    continue
                value = (node.get("widgets_values_named") or {}).get("value") or ""
                if len(value) > 5000 and "{input text}" in value:
                    official = value
                    break
            self.assertTrue(official, f"{filename} 里没找到官方 task prompt")
            ours = H3PE.task_prompt_for(key == "i2i")
            self.assertEqual(ours, official,
                             f"{key} 的 task prompt 与官方模板不一致")

    def test_system_prompt_asset_present(self):
        self.assertTrue((E.ASSETS_DIR / "h3image-pe-system.txt").exists())

    def test_edit_ratio_accepts_arbitrary_wh(self):
        """官方编辑档允许任意正整数 W:H（不限于固定档位）。"""
        self.assertEqual(H3PE._valid_ratio("137:83", True), "137:83")
        self.assertEqual(H3PE._valid_ratio("16:9", True), "16:9")
        self.assertEqual(H3PE._valid_ratio("0:1", True), "")
        self.assertEqual(H3PE._valid_ratio("abc", True), "")

    def test_t2i_ratio_rejects_non_whitelist(self):
        """官方文生图档只允许那 10 个固定比例。"""
        self.assertEqual(H3PE._valid_ratio("16:9", False), "16:9")
        self.assertEqual(H3PE._valid_ratio("137:83", False), "")
        self.assertEqual(H3PE._valid_ratio("4:7", False), "")


class TestJsonContractUnwrap(unittest.TestCase):
    """H3-Image 官方 PE 的产物是 JSON，写回提示词框前必须解包。"""

    def test_unwrap_extracts_caption_and_ratio(self):
        text, ratio = E.unwrap_json_contract(
            '{"expanded_caption": "A cat on a desk.", "aspect_ratio": "16:9"}',
            "h3image-pe-t2i")
        self.assertEqual(text, "A cat on a desk.")
        self.assertEqual(ratio, "16:9")

    def test_unwrap_edit_variant_ratio_key(self):
        text, ratio = E.unwrap_json_contract(
            '{"expanded_caption": "Edited scene.", "target_aspect_ratio": "4:3"}',
            "h3image-pe-i2i")
        self.assertEqual(text, "Edited scene.")
        self.assertEqual(ratio, "4:3")

    def test_unwrap_handles_fenced_json(self):
        text, ratio = E.unwrap_json_contract(
            '```json\n{"expanded_caption": "Fenced.", "aspect_ratio": "1:1"}\n```',
            "h3image-pe-t2i")
        self.assertEqual(text, "Fenced.")
        self.assertEqual(ratio, "1:1")

    def test_non_contract_spec_untouched(self):
        """其它模板的产物原样通过（不破坏 MOSS / Qwen 等既有行为）。"""
        original = '{"expanded_caption": "x"}'
        text, ratio = E.unwrap_json_contract(original, "qwen21-pe-t2i")
        self.assertEqual(text, original)
        self.assertEqual(ratio, "")

    def test_plain_text_untouched(self):
        text, ratio = E.unwrap_json_contract("plain caption", "h3image-pe-t2i")
        self.assertEqual(text, "plain caption")
        self.assertEqual(ratio, "")


if __name__ == "__main__":
    unittest.main()
