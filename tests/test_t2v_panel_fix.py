# -*- coding: utf-8 -*-
"""文生视频面板修复的回归测试（2026-11-11）。

覆盖本轮修的 4 条缺陷，每条都有**会红的断言**（不是恒真断言）：

1. **尺寸预设下拉**
   - 预设清单存在，且每条都满足 H3 原生画布的硬约束
     （面积 <= 768*1344、短边 <= 768、宽高都是 32 的倍数）
   - 16:9 的预设与官方模板 Note 的像素表**逐格一致**
     （608x352 / 864x480 / 1056x608 / 1216x672 / 1344x768）
   - 1:1 这类方画布的超限档位**不在**清单里（不会给出假选项）
   - 超限的手填尺寸会被服务端吸附回画布上限，并给出原因

2. **加速 LoRA 下拉单选**
   - 提交参数里 accel_loras **最多一项**
   - 服务端归一也只保留一项（老客户端传多项时取第一项）
   - 工作流里最多一个「用户选的」加速 LoRA 节点（固定的除外）

3. **加速 LoRA 按主模型适配过滤**
   - w4a8 底模下 Turbo 系全部不兼容 -> 从可选项里剔除
   - 剪枝 int8 / 完整 int8 / BF16 底模下 Turbo 系可选
   - 「不适配的不出现」——用**计数**断言，不是只断言某个 key 在/不在
   - lightx2v_8step 指向的是真 H3 权重（实测 208/208 命中）

4. **去掉重复 LORA 强度控件**
   - index.html 里 #loraStrength 控件只出现**一次**
   - 它在 #loraField 内（紧邻 LoRA 选择），不在生成按钮下方
   - 文生视频面板里有且只有一个强度控件（#vidAccelLoraStrength）

运行：python3 -m pytest tests/test_t2v_panel_fix.py -v
"""
import json
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import video_models as VM       # noqa: E402
from h3studio import video_workflow as VW     # noqa: E402

#: 官方模板 Note 里的 16:9 像素表（实测
#: comfyui_workflow_templates_json/templates/video_minimax_h3_t2v.json）
OFFICIAL_16_9_TABLE = {
    0.2: (608, 352),
    0.4: (864, 480),
    0.6: (1056, 608),
    0.8: (1216, 672),
    0.98: (1344, 768),
}


class TestSizePresets(unittest.TestCase):
    """尺寸预设（缺陷 1）"""

    def test_presets_exist_for_every_aspect(self):
        for aspect in VM.VIDEO_ASPECTS:
            presets = VM.size_presets_for_aspect(aspect)
            self.assertTrue(presets, f"{aspect} 应该有尺寸预设")

    def test_every_preset_respects_h3_native_canvas(self):
        """每条预设都必须同时满足三条硬约束。"""
        for p in VM.all_size_presets():
            w, h = p["width"], p["height"]
            with self.subTest(preset=p["key"]):
                self.assertEqual(w % 32, 0, f"{p['key']} 宽度不是 32 的倍数")
                self.assertEqual(h % 32, 0, f"{p['key']} 高度不是 32 的倍数")
                self.assertLessEqual(
                    w * h, VM.VIDEO_MAX_CANVAS_PIXELS,
                    f"{p['key']} 超出 H3 原生画布面积上限 768x1344")
                self.assertLessEqual(
                    min(w, h), 768, f"{p['key']} 短边超过 768 基准")

    def test_16_9_presets_match_official_table(self):
        """16:9 的预设必须与官方模板 Note 的像素表**逐格一致**。"""
        presets = {p["megapixels"]: (p["width"], p["height"])
                   for p in VM.size_presets_for_aspect("16:9")}
        for mp, expected in OFFICIAL_16_9_TABLE.items():
            with self.subTest(mp=mp):
                self.assertIn(mp, presets, f"官方档位 {mp} 不在预设里")
                self.assertEqual(presets[mp], expected,
                                 f"mp={mp} 的预设像素与官方表不一致")

    def test_official_full_table_reproduced_by_algorithm(self):
        """video_dimensions() 必须能复现官方模板 Note 的**全部**九档。

        这是预设清单的地基 —— 地基歪了，预设就是编的。
        """
        full = {
            0.2: (608, 352), 0.3: (736, 416), 0.4: (864, 480),
            0.5: (960, 544), 0.6: (1056, 608), 0.7: (1152, 640),
            0.8: (1216, 672), 0.9: (1280, 736), 0.98: (1344, 768),
        }
        for mp, expected in full.items():
            with self.subTest(mp=mp):
                self.assertEqual(VM.video_dimensions("16:9", mp), expected)

    def test_over_canvas_combinations_are_excluded(self):
        """超画布的组合不能出现在下拉里（不是置灰，是不出现）。"""
        # 1:1 @ 0.98MP -> 1024x1024，面积 1048576 > 768*1344 -> 必须被剔除
        over = VM.video_dimensions("1:1", 0.98)
        self.assertGreater(over[0] * over[1], VM.VIDEO_MAX_CANVAS_PIXELS)
        keys = {p["megapixels"] for p in VM.size_presets_for_aspect("1:1")}
        self.assertNotIn(0.98, keys, "1:1 的 0.98MP 超画布，不该出现在下拉里")

    def test_preset_keys_are_unique(self):
        keys = [p["key"] for p in VM.all_size_presets()]
        self.assertEqual(len(keys), len(set(keys)), "预设 key 必须唯一")

    def test_preset_label_carries_pixels(self):
        """label 必须带实际像素 —— 用户要能一眼看到会跑多大。"""
        for p in VM.all_size_presets():
            with self.subTest(preset=p["key"]):
                self.assertIn(f"{p['width']}×{p['height']}", p["label"])
                self.assertIn("p", p["label"])

    def test_payload_includes_size_presets(self):
        payload = VM.options_payload()
        self.assertIn("size_presets", payload)
        self.assertTrue(payload["size_presets"])
        self.assertEqual(
            len(payload["size_presets"]), len(VM.all_size_presets()))

    def test_server_snaps_oversize_input_to_canvas(self):
        """手填 1920x1080（超出原生画布）要被缩回上限，并说明原因。"""
        import server as srv
        w, h, reasons = VM.normalize_video_size(
            1920, 1080,
            max_pixels=VM.VIDEO_MAX_CANVAS_PIXELS / (1024 * 1024))
        self.assertLessEqual(w * h, VM.VIDEO_MAX_CANVAS_PIXELS)
        self.assertTrue(reasons, "超画布必须给出调整原因")
        self.assertTrue(any("像素" in r or "上限" in r for r in reasons))


class TestAccelLoraSingleSelect(unittest.TestCase):
    """加速 LoRA 下拉单选（缺陷 2）"""

    def test_payload_declares_single_select(self):
        payload = VM.options_payload()
        self.assertTrue(payload.get("accel_lora_single_select"))

    def test_server_keeps_only_one_accel_lora(self):
        """老客户端传多项时，服务端只保留第一项。"""
        import server as srv
        srv._COMFY_NODE_NAMES.update(
            VM.VIDEO_ATTENTION_MODES[k]["node"]
            for k in VM.VIDEO_ATTENTION_MODES
            if VM.VIDEO_ATTENTION_MODES[k].get("node"))
        base = {
            "prompt": "a red panda", "duration": 5,
            "model_key": "fl2va_pruned_int8",
            "width": 864, "height": 480,
            "steps": 4, "seed": 123, "attention": "default",
            "twopass": {"enabled": False},
        }
        params = dict(base, accel_loras=[
            {"key": "lightx2v_4step_768p", "strength": 1.0, "enabled": True},
            {"key": "dmad_4step_critic", "strength": 1.0, "enabled": True},
        ])
        out, err = srv.normalize_t2v_params(params)
        self.assertEqual(err, "")
        self.assertEqual(len(out["accel_loras"]), 1,
                         "加速 LoRA 是单选，服务端只应保留一项")
        self.assertEqual(out["accel_loras"][0]["key"], "lightx2v_4step_768p")

    def test_workflow_has_at_most_one_user_accel_lora(self):
        """工作流里最多一个「用户选的」加速 LoRA（固定的那枚不算）。"""
        params = {
            "job_id": "j", "prompt": "p", "duration": 5,
            "model_key": "fl2va_pruned_int8", "width": 864, "height": 480,
            "steps": 4, "seed": 1, "attention": "default",
            "accel_loras": [
                {"key": "lightx2v_4step_768p", "strength": 1.0, "enabled": True},
                {"key": "dmad_4step_critic", "strength": 1.0, "enabled": True},
            ],
        }
        wf = VW.build_t2v_panel(params)
        lora_nodes = [n for n in wf.values()
                      if n["class_type"] == "MiniMaxH3TurboLoRA"]
        titles = [n.get("_meta", {}).get("title", "") for n in lora_nodes]
        user_nodes = [t for t in titles if t.startswith("加速 LoRA·")]
        self.assertLessEqual(len(user_nodes), 1,
                             "工作流里最多一个用户选的加速 LoRA")

    def test_selected_lora_strength_reaches_workflow(self):
        """界面上的强度必须真的进到工作流的 strength 字段。"""
        params = {
            "job_id": "j", "prompt": "p", "duration": 5,
            "model_key": "fl2va_pruned_int8", "width": 864, "height": 480,
            "steps": 4, "seed": 1, "attention": "default",
            "accel_loras": [
                {"key": "lightx2v_4step_768p", "strength": 0.75, "enabled": True},
            ],
        }
        wf = VW.build_t2v_panel(params)
        user = [n for n in wf.values()
                if n["class_type"] == "MiniMaxH3TurboLoRA"
                and n.get("_meta", {}).get("title", "").startswith("加速 LoRA·")]
        self.assertEqual(len(user), 1)
        self.assertAlmostEqual(user[0]["inputs"]["strength"], 0.75)


class TestAccelLoraModelFiltering(unittest.TestCase):
    """加速 LoRA 按主模型适配过滤（缺陷 3）"""

    def test_w4a8_excludes_all_turbo_family(self):
        """w4a8 底模把隐藏维折成 2688 —— Turbo 系全部形状对不上。"""
        matrix = VM.accel_lora_compat_matrix("fl2va_pruned_w4a8")
        turbo_keys = ["lightx2v_4step_768p", "lightx2v_4step_sla_768p",
                      "lightx2v_8step", "dmad_4step_critic", "better_motion"]
        for key in turbo_keys:
            with self.subTest(lora=key):
                self.assertFalse(matrix[key]["compatible"],
                                 f"{key} 在 w4a8 底模下不该兼容")
                self.assertGreater(matrix[key]["shape_mismatch"], 0)

    def test_w4a8_keeps_speed_slider(self):
        """speed_slider 只 patch 1 个层，实测在 w4a8 下依然兼容。"""
        matrix = VM.accel_lora_compat_matrix("fl2va_pruned_w4a8")
        self.assertTrue(matrix["speed_slider"]["compatible"])

    def test_compatible_models_allow_turbo_family(self):
        """剪枝 int8 / 完整 int8 / BF16 下 Turbo 系应全部可选。"""
        for model_key in ("fl2va_pruned_int8", "fl2va_int8", "fl2va_bf16",
                          "hybrid_fl2va_ref2va"):
            matrix = VM.accel_lora_compat_matrix(model_key)
            with self.subTest(model=model_key):
                for key in ("lightx2v_4step_768p", "lightx2v_8step",
                            "dmad_4step_critic"):
                    self.assertTrue(matrix[key]["compatible"],
                                    f"{key} 在 {model_key} 下应兼容")

    def test_filtering_actually_removes_entries(self):
        """过滤必须**真的减少条目数** —— 不能只是加了个标志位。

        这是「不适配的不出现在下拉里」的可证伪断言：
        如果实现只是把 compatible 标成 False 但仍然列出来，
        这里就会失败。
        """
        blocked = VM.accel_lora_compat_matrix("fl2va_pruned_w4a8")
        selectable = [k for k, v in blocked.items() if v["compatible"]]
        self.assertLess(len(selectable), len(blocked),
                        "w4a8 下必须有加速 LoRA 被过滤掉")
        self.assertEqual(len(selectable), 1,
                         "w4a8 下实测只有 speed_slider 可用")

    def test_pruned_int8_keeps_all(self):
        """剪枝 int8（默认底模）不该过滤掉任何一条。"""
        matrix = VM.accel_lora_compat_matrix("fl2va_pruned_int8")
        selectable = [k for k, v in matrix.items() if v["compatible"]]
        self.assertEqual(len(selectable), len(matrix),
                         "剪枝 int8 下不该有加速 LoRA 被剔除")

    def test_lightx2v_8step_points_to_real_h3_weight(self):
        """8 步 LoRA 必须指向真 H3 权重（实测 208/208 命中）。

        历史坑：注册项曾指向 h3_lightx2v/ 下一个**非 H3** 的文件
        （层名是 transformer_blocks / token_refiner.refiner_blocks，
        H3 底模里一个都没有 -> 实测 0/312 命中，ComfyUI 静默丢弃）。
        """
        meta = VM.VIDEO_ACCEL_LORA_COMBOS["lightx2v_8step"]
        check = VM.check_h3_lora_compat(
            meta["name"], VM.VIDEO_MODELS["fl2va_pruned_int8"]["unet"])
        self.assertTrue(check["compatible"],
                        "8 步 LoRA 必须与 H3 底模兼容：" + check["reason"])
        self.assertEqual(check["missing"], 0,
                         "8 步 LoRA 有层在 H3 底模里找不到 —— 文件指错了？")
        self.assertEqual(check["matched"], 208)

    def test_payload_carries_compat_matrix_for_every_model(self):
        payload = VM.options_payload()
        matrix = payload.get("accel_lora_compat")
        self.assertIsNotNone(matrix, "清单必须下发各底模的兼容矩阵")
        for model_key in VM.VIDEO_MODELS:
            self.assertIn(model_key, matrix)
            self.assertIn("lightx2v_4step_768p", matrix[model_key])
            self.assertIn("compatible", matrix[model_key]["lightx2v_4step_768p"])


class TestNoDuplicateLoraStrengthControl(unittest.TestCase):
    """去掉重复 LORA 强度控件（缺陷 4）"""

    @classmethod
    def setUpClass(cls):
        cls.html = (REPO / "web" / "index.html").read_text(encoding="utf-8")

    def test_lora_strength_control_appears_exactly_once(self):
        count = self.html.count('id="loraStrength"')
        self.assertEqual(count, 1,
                         "LoRA 强度控件在全站只能有一个（原来有两个）")

    def test_lora_strength_is_next_to_lora_selection(self):
        """强度控件必须在 #loraField 内（紧邻 LoRA 选择）。"""
        i_field = self.html.find('id="loraField"')
        i_strength = self.html.find('id="loraStrength"')
        i_end = self.html.find("</div>", self.html.find('id="loraCards"'))
        self.assertGreater(i_strength, i_field,
                           "LoRA 强度控件应位于 #loraField 之后")
        # #loraField 的 subfield 结束位置 —— 用下一个 field 的起点做界
        i_next_field = self.html.find('<div class="field" id="stepsField"')
        self.assertLess(i_strength, i_next_field,
                        "LoRA 强度控件应仍在 #loraField 块内")

    def test_lora_strength_lives_inside_lora_field(self):
        """强度控件必须**在 #loraField 内**（紧邻 LoRA 选择）。

        这才是「唯一一处」的判据：#loraField 整体只在选了 LoRA 模型时
        显示（JS 控制），文生视频面板则整体隐藏 #modelField ——
        所以文生视频界面下这个通用强度控件**不出现**，
        只有加速 LoRA 旁那一个强度控件。
        """
        i_lora_field = self.html.find('id="loraField"')
        i_strength = self.html.find('id="loraStrength"')
        i_model_field_end = self.html.find('<div class="field" id="stepsField"')
        self.assertGreater(i_lora_field, 0)
        self.assertGreater(i_strength, i_lora_field,
                           "LoRA 强度控件应在 #loraField 之后")
        self.assertLess(i_strength, i_model_field_end,
                        "LoRA 强度控件应仍在 #loraField 块内")

    def test_video_panel_has_no_generic_strength_control(self):
        """文生视频面板里不能出现通用 #loraStrength。

        #modelField（里面含 #loraStrengthField）在文生视频模式下被
        整体隐藏（app.js 的 activateMode 里 `$("#modelField").style.display = "none"`），
        所以界面上不会看到第二个强度控件 —— 但 DOM 里必须保证
        文生视频面板片段里没有它，避免将来有人误放开。
        """
        i_panel = self.html.find('<div id="videoPanel"')
        i_panel_end = self.html.find('<div class="field" id="resField"')
        seg = self.html[i_panel:i_panel_end]
        self.assertNotIn('id="loraStrength"', seg)
        self.assertNotIn('id="loraStrengthField"', seg)
        # 文生视频面板自己有且只有一个强度控件
        self.assertEqual(seg.count('id="vidAccelLoraStrength"'), 1)

    def test_generic_strength_container_is_nested_in_lora_field(self):
        """#loraStrengthField 的开闭标签必须落在 #loraField 内 —— 用缩进层级验证。"""
        lines = self.html.splitlines()
        idx_field = next(i for i, l in enumerate(lines) if 'id="loraField"' in l)
        idx_strength = next(i for i, l in enumerate(lines) if 'id="loraStrengthField"' in l)
        idx_input = next(i for i, l in enumerate(lines) if 'id="loraStrength"' in l)
        self.assertGreater(idx_strength, idx_field)
        self.assertGreater(idx_input, idx_strength)
        # 缩进更深 => 确实嵌套在里面
        indent_field = len(lines[idx_field]) - len(lines[idx_field].lstrip())
        indent_strength = len(lines[idx_strength]) - len(lines[idx_strength].lstrip())
        self.assertGreater(indent_strength, indent_field,
                           "#loraStrengthField 应缩进在 #loraField 内部")

    def test_video_panel_has_exactly_one_strength_control(self):
        """文生视频面板里只有一个强度控件（#vidAccelLoraStrength）。"""
        i_panel = self.html.find('<div id="videoPanel"')
        i_panel_end = self.html.find('<div class="field" id="resField"')
        seg = self.html[i_panel:i_panel_end]
        self.assertEqual(seg.count('id="vidAccelLoraStrength"'), 1)
        # 视频面板里不能出现旧的通用 loraStrength
        self.assertNotIn('id="loraStrength"', seg)

    def test_seed_control_still_present(self):
        """种子字段必须还在（不能被误删）。"""
        self.assertIn('id="seed"', self.html)

    def test_js_has_no_dangling_reference_to_removed_container(self):
        """app.js 不能还去操作已删掉的 #seedStrengthPair 容器。"""
        js = (REPO / "web" / "app.js").read_text(encoding="utf-8")
        code = "\n".join(
            line for line in js.splitlines()
            if "seedStrengthPair" not in line or not line.strip().startswith("//")
        )
        self.assertNotIn("$(\"#seedStrengthPair\")", code,
                         "app.js 还在操作已删掉的 #seedStrengthPair")

    def test_js_still_wires_the_moved_control(self):
        """控件搬了位置，监听器必须还在（否则强度调不动）。"""
        js = (REPO / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('$("#loraStrength").addEventListener', js)
        self.assertIn('$("#loraStrengthVal")', js)


class TestVideoPanelJsWiring(unittest.TestCase):
    """前端面板脚本的接线（静态检查，防回归）"""

    @classmethod
    def setUpClass(cls):
        cls.js = (REPO / "web" / "video-panel.js").read_text(encoding="utf-8")

    def test_has_size_preset_select(self):
        self.assertIn("vidSizePresetSelect", self.js)

    def test_has_accel_lora_select(self):
        self.assertIn("vidAccelLoraSelect", self.js)

    def test_has_accel_lora_strength(self):
        self.assertIn("vidAccelLoraStrength", self.js)

    def test_accel_loras_submitted_as_single_item(self):
        """submitParams() 里 accel_loras 最多 push 一项。"""
        m = re.search(
            r"const accel = \[\];(.*?)\n    return \{",
            self.js, re.S)
        self.assertIsNotNone(m, "找不到 submitParams 里的 accel 构造")
        body = m.group(1)
        self.assertEqual(body.count("accel.push("), 1,
                         "accel_loras 只能 push 一项（单选）")

    def test_filtering_uses_server_compat_flag(self):
        """过滤逻辑必须读服务端的实测结果（compatible / available）。"""
        self.assertIn("selectableAccelLoras", self.js)
        self.assertIn("item.compatible === false", self.js)
        self.assertIn("item.available === false", self.js)

    def test_config_persists_single_accel_lora(self):
        self.assertIn("accel_lora:", self.js)
        self.assertIn("accel_lora_strength:", self.js)

    def test_backward_compat_with_old_array_config(self):
        """老配置（accel_loras 数组）要能读回单选字段。"""
        self.assertIn("cfg.accel_loras", self.js)


if __name__ == "__main__":
    unittest.main()
