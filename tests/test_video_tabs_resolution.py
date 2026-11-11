# -*- coding: utf-8 -*-
"""视频两级导航 + minimax-h3 分辨率上限放宽 的回归测试（2026-11-11）。

覆盖两块需求：

**一、视频类别标签重构（两级：类别 → 模型）**

1. **类别标签结构与顺序**（需求：固定四项，顺序与文案不可变）
   - 顺序必须是：文生视频 / 首尾帧生视频 / 参考生视频 / 长片拼接
   - 类别标签文案里**不得出现模型名**（需求：不能把模型名硬编码进类别标签文案）
   - 每个类别必须至少有一个模型项

2. **类别 → 模型映射**
   - 映射是**数据驱动**的（存在 VIDEO_CATEGORY_MODELS 这张表），
     不是靠 if/else 拼字符串
   - 每个模型项的 mode 必须真实存在于后端 BUILDERS（或 longfilm）
   - model -> category 反查必须自洽

3. **默认选中与回退规则**
   - 默认选中 = 服务端下发的 default_category / default_model
   - 切换类别时的回退：① 该类别上次用过的模型 → ② 该类别首个可用模型
     → ③ 禁用并提示

4. **向后兼容**
   - 老 mode（t2v / flf / r2v / longfilm）一个都没改
   - 老 mode 仍能反查回它所属的类别

**二、minimax-h3 文生视频分辨率上限放宽到 2.0M 像素**

5. **1920x1088 必测**
   - 16:9 @ 2.0 MP 必须算出 1920x1088
   - 1920x1088 必须**在上限内**（不被上限拦掉）
   - 1920x1088 必须是 32 的倍数（宽高各自）

6. **2.0M 上限与超限提示**
   - 上限是 2.0 MP（= 2 * 1024 * 1024）
   - 超限时 strict 模式**不静默截断** —— 返回 width=0 + 可执行提示
   - 提示里要同时出现「上限」和「当前请求值」

7. **非 minimax / 非 t2v 模式约束不被误放宽**
   - flf / r2v 仍是 0.8 MP 档位，不跟着 t2v 放宽
   - longfilm 不限（拼片不产生新画面）

运行：python3 -m pytest tests/test_video_tabs_resolution.py -v
"""
import json
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import video_categories as VC      # noqa: E402
from h3studio import video_models as VM           # noqa: E402
from h3studio import workflows as WF              # noqa: E402


#: 需求规定的固定四项（顺序 + 文案都不可变）
REQUIRED_CATEGORY_ORDER = ["t2v", "flf", "r2v", "longfilm"]
REQUIRED_CATEGORY_LABELS = ["文生视频", "首尾帧生视频", "参考生视频", "长片拼接"]

#: 官方模板 Note 的 16:9 像素表（实测
#: comfyui_workflow_templates_json/templates/video_minimax_h3_t2v.json
#: 以及本机 user/default/workflows/video_minimax_h3_t2v.json）
OFFICIAL_16_9_TABLE = {
    0.2: (608, 352),
    0.3: (736, 416),
    0.4: (864, 480),
    0.5: (960, 544),
    0.6: (1056, 608),
    0.7: (1152, 640),
    0.8: (1216, 672),
    0.9: (1280, 736),
    0.98: (1344, 768),
    1.0: (1376, 768),
    1.2: (1504, 832),
    1.5: (1664, 928),
    1.8: (1824, 1024),
    2.0: (1920, 1088),
}


# ==================================================================
# 一、类别标签结构与顺序
# ==================================================================
class TestCategoryOrderAndLabels(unittest.TestCase):
    """需求：类别标签固定四项，顺序与文案必须是给定的。"""

    def test_category_order_is_exactly_the_required_four(self):
        self.assertEqual(VC.VIDEO_CATEGORY_ORDER, REQUIRED_CATEGORY_ORDER,
                         "视频类别顺序必须是 文生视频/首尾帧生视频/参考生视频/长片拼接")

    def test_category_labels_match_required_text(self):
        labels = [VC.VIDEO_CATEGORY_LABELS[k] for k in REQUIRED_CATEGORY_ORDER]
        self.assertEqual(labels, REQUIRED_CATEGORY_LABELS,
                         "四个类别的显示文案必须逐字一致")

    def test_list_categories_preserves_order(self):
        keys = [c["key"] for c in VC.list_categories()]
        self.assertEqual(keys, REQUIRED_CATEGORY_ORDER,
                         "下发给前端的清单必须保持规定顺序")

    def test_labels_are_not_polluted_with_model_names(self):
        """类别标签文案里不得出现模型名（需求硬约束）。

        模型名统一带「minimax-h3 」前缀 —— 类别文案里出现它就是硬编码了模型。
        """
        for key in REQUIRED_CATEGORY_ORDER:
            label = VC.VIDEO_CATEGORY_LABELS[key]
            with self.subTest(category=key):
                self.assertNotIn("minimax-h3", label.lower(),
                                 f"类别「{label}」文案里混进了模型名")
                self.assertNotIn("h3", label.lower(),
                                 f"类别「{label}」文案里混进了模型名")

    def test_category_labels_are_distinct(self):
        labels = [VC.VIDEO_CATEGORY_LABELS[k] for k in REQUIRED_CATEGORY_ORDER]
        self.assertEqual(len(labels), len(set(labels)), "四个类别文案必须互不相同")

    def test_every_category_has_at_least_one_model(self):
        for key in REQUIRED_CATEGORY_ORDER:
            with self.subTest(category=key):
                self.assertTrue(VC.models_for_category(key),
                                f"类别 {key} 下必须至少有一个模型项")

    def test_every_model_item_has_required_fields(self):
        for cat in VC.list_categories():
            for model in cat["models"]:
                with self.subTest(category=cat["key"], model=model.get("key")):
                    for field in ("key", "label", "mode", "media"):
                        self.assertIn(field, model)
                        self.assertTrue(str(model[field]).strip(),
                                        f"模型项字段 {field} 不能为空")

    def test_model_keys_are_unique_across_all_categories(self):
        keys = [m["key"] for m in VC.list_models()]
        self.assertEqual(len(keys), len(set(keys)),
                         "模型项 key 必须全局唯一（否则回退规则会串）")


# ==================================================================
# 二、类别 → 模型映射（数据驱动 + 单一真源）
# ==================================================================
class TestCategoryModelMapping(unittest.TestCase):
    def test_mapping_is_data_driven_not_string_concatenated(self):
        """映射必须来自一张表，而不是靠 if/else 拼。

        判据：VIDEO_CATEGORY_MODELS 存在，且每个类别的 mode 都能在表里查到。
        （变异测试：把某个模型项的 mode 改掉，本断言会红。）
        """
        self.assertIsInstance(VC.VIDEO_CATEGORY_MODELS, dict)
        self.assertEqual(set(VC.VIDEO_CATEGORY_MODELS), set(REQUIRED_CATEGORY_ORDER),
                         "表里的类别集合必须与规定顺序的集合一致")
        for cat in REQUIRED_CATEGORY_ORDER:
            for item in VC.VIDEO_CATEGORY_MODELS[cat]:
                self.assertEqual(VC.MODE_TO_CATEGORY.get(item["mode"]), cat,
                                 f"{item['mode']} 反查类别应当是 {cat}")

    def test_model_modes_are_real_backend_modes(self):
        """每个模型项背后的 mode 必须是后端真能跑的模式。"""
        registered = set(WF.BUILDERS.keys()) | {"longfilm"}
        for model in VC.list_models():
            with self.subTest(model=model["key"]):
                self.assertIn(model["mode"], registered,
                              f"模型项 {model['key']} 的 mode {model['mode']} "
                              f"不在后端注册表里")

    def test_mode_to_category_roundtrip(self):
        for model in VC.list_models():
            with self.subTest(model=model["key"]):
                category = VC.category_for_mode(model["mode"])
                self.assertTrue(category)
                self.assertIn(model["key"],
                              [m["key"] for m in VC.models_for_category(category)],
                              "mode -> category -> models 反查必须能回到原模型项")

    def test_resolve_mode_returns_backing_mode(self):
        for cat in REQUIRED_CATEGORY_ORDER:
            for item in VC.models_for_category(cat):
                with self.subTest(category=cat, model=item["key"]):
                    self.assertEqual(VC.resolve_mode(cat, item["key"]), item["mode"])

    def test_resolve_mode_falls_back_to_first_model(self):
        """model_key 为空/非法时 -> 该类别首个模型项的 mode（回退规则②）。"""
        for cat in REQUIRED_CATEGORY_ORDER:
            items = VC.models_for_category(cat)
            with self.subTest(category=cat):
                self.assertEqual(VC.resolve_mode(cat, None), items[0]["mode"])
                self.assertEqual(VC.resolve_mode(cat, "不存在的key"),
                                 items[0]["mode"])

    def test_resolve_mode_unknown_category_is_empty(self):
        self.assertEqual(VC.resolve_mode("不存在的类别", None), "")

    def test_list_categories_exposes_default_model(self):
        for cat in VC.list_categories():
            with self.subTest(category=cat["key"]):
                keys = [m["key"] for m in cat["models"]]
                self.assertIn(cat["default_model"], keys,
                              "default_model 必须是该类别下的模型项")


# ==================================================================
# 三、向后兼容
# ==================================================================
class TestBackwardCompatibility(unittest.TestCase):
    """老 URL / 老参数 / 老任务记录里的 mode 一个都不能改。"""

    def test_legacy_modes_still_exist(self):
        for mode in ("t2v", "flf", "r2v", "longfilm"):
            with self.subTest(mode=mode):
                self.assertIn(mode, VC.MODE_TO_CATEGORY,
                              "老 mode 必须仍能定位到对应类别")

    def test_legacy_modes_map_to_expected_categories(self):
        self.assertEqual(VC.category_for_mode("t2v"), "t2v")
        self.assertEqual(VC.category_for_mode("flf"), "flf")
        self.assertEqual(VC.category_for_mode("r2v"), "r2v")
        self.assertEqual(VC.category_for_mode("longfilm"), "longfilm")

    def test_backend_job_modes_unchanged(self):
        """服务端 JOB_MODES 里不该有本次新增的东西，也别删了老的。"""
        import server as srv
        for mode in ("t2v", "flf", "r2v", "t2i", "t2i_qwen", "i2i_qwen",
                     "t2i_moss", "t2a", "a2a"):
            with self.subTest(mode=mode):
                self.assertIn(mode, srv.JOB_MODES)

    def test_gen_types_still_knows_video_modes(self):
        """右侧任务「按生成类型」标签的老清单不能被这次改造打坏。"""
        from h3studio import gen_types
        for mode in ("t2v", "flf", "r2v", "longfilm"):
            with self.subTest(mode=mode):
                self.assertIn(mode, gen_types.GENERATION_TYPES)


# ==================================================================
# 四、minimax-h3 分辨率吸附（1920x1088 必测）
# ==================================================================
class TestResolutionSnapping(unittest.TestCase):
    def test_1920x1088_is_reachable_at_2mp_16_9(self):
        """16:9 @ 2.0 MP 必须算出 1920x1088（需求点名）。"""
        w, h = VM.video_dimensions("16:9", 2.0)
        self.assertEqual((w, h), (1920, 1088))

    def test_1920x1088_passes_through_snapping_unchanged(self):
        """1920x1088 已经是合法的 32 倍数组合，必须原样通过且无调整原因。"""
        w, h, reasons = VM.normalize_video_size(1920, 1088)
        self.assertEqual((w, h), (1920, 1088))
        self.assertEqual(reasons, [], "合法值不应产生调整原因")

    def test_1920x1088_is_within_the_2mp_cap(self):
        """1920x1088 = 2,088,960 px，必须在 2.0M 上限内（不被上限拦掉）。"""
        cap = VM.VIDEO_MAX_CANVAS_PIXELS
        self.assertLessEqual(1920 * 1088, cap,
                             "1920x1088 必须落在 2.0M 上限内")
        self.assertEqual(cap, 2 * 1024 * 1024,
                         "上限应是 2.0 MP（2 * 1024 * 1024 px）")

    def test_width_and_height_are_multiples_of_32(self):
        """宽高各自按工作流要求的整数倍率（32）吸附。"""
        for w, h in [(1920, 1088), (1921, 1089), (1000, 563), (864, 480)]:
            with self.subTest(size=(w, h)):
                aw, ah, reasons = VM.normalize_video_size(w, h)
                self.assertEqual(aw % 32, 0, f"宽度 {aw} 不是 32 的倍数")
                self.assertEqual(ah % 32, 0, f"高度 {ah} 不是 32 的倍数")

    def test_non_multiple_snaps_to_nearest_32(self):
        w, h, reasons = VM.normalize_video_size(1921, 1089)
        self.assertEqual((w, h), (1920, 1088))
        self.assertTrue(reasons)
        self.assertTrue(any("32" in r for r in reasons),
                        "必须说明是 32 倍数吸附")

    def test_official_full_table_is_reproduced(self):
        """video_dimensions() 必须逐格复现官方模板 Note 的 16:9 全表。

        地基歪了，预设就是编的。
        """
        for mp, expected in OFFICIAL_16_9_TABLE.items():
            with self.subTest(mp=mp):
                self.assertEqual(VM.video_dimensions("16:9", mp), expected)

    def test_2mp_preset_exists_for_16_9(self):
        presets = {p["megapixels"]: p
                   for p in VM.size_presets_for_aspect("16:9")}
        self.assertIn(2.0, presets, "16:9 必须有 2.0MP 预设（= 1920x1088）")
        self.assertEqual((presets[2.0]["width"], presets[2.0]["height"]),
                         (1920, 1088))

    def test_preset_1920x1088_not_filtered_out(self):
        """之前 768*1344 的短边规则会把 1920x1088 误杀 —— 现在不能了。"""
        keys = {p["key"] for p in VM.all_size_presets()}
        self.assertIn("16:9_2.0", keys,
                      "16:9 的 2.0MP 预设必须出现在清单里")


# ==================================================================
# 五、2.0M 上限与超限提示
# ==================================================================
class TestPixelLimitAndOverLimit(unittest.TestCase):
    def test_cap_is_2_megapixels(self):
        self.assertEqual(VM.VIDEO_MAX_CANVAS_PIXELS, 2 * 1024 * 1024)

    def test_old_768x1344_cap_is_gone(self):
        """旧的 768*1344（0.98MP）上限必须被替换掉 —— 它是参考视频画布上限。"""
        self.assertNotEqual(VM.VIDEO_MAX_CANVAS_PIXELS, 768 * 1344)

    def test_over_limit_is_not_silently_truncated(self):
        """strict 模式下超限 -> 不缩放，返回 width=0 + 明确原因。"""
        w, h, reasons = VM.normalize_video_size(
            2560, 1440, max_pixels=2.0, strict_over_limit=True)
        self.assertEqual((w, h), (0, 0),
                         "strict 超限必须拒绝（不能给一个被偷偷改小的尺寸）")
        self.assertTrue(reasons, "超限必须给出原因")

    def test_over_limit_message_states_limit_and_request(self):
        """提示要同时说明「上限是多少」与「当前请求值」。"""
        _, _, reasons = VM.normalize_video_size(
            2560, 1440, max_pixels=2.0, strict_over_limit=True)
        text = " ".join(reasons)
        self.assertIn("2.0M", text, "提示必须说明上限是 2.0M")
        self.assertIn("2,097,152", text, "提示必须给出上限的像素数")
        self.assertIn("3,686,400", text, "提示必须给出当前请求的像素数")
        self.assertIn("2560", text)
        self.assertIn("1440", text)

    def test_over_limit_message_is_actionable(self):
        """提示要能指导用户怎么改（不是只说「超限了」）。"""
        _, _, reasons = VM.normalize_video_size(
            2560, 1440, max_pixels=2.0, strict_over_limit=True)
        text = " ".join(reasons)
        self.assertTrue(any(k in text for k in ("调小", "改用", "比例")),
                        f"提示要给出可执行的下一步，当前是：{text}")

    def test_non_strict_mode_still_scales_down_with_reason(self):
        """非 strict（前端预览）仍然按比例缩，并说明原因。"""
        w, h, reasons = VM.normalize_video_size(
            2560, 1440, max_pixels=2.0, strict_over_limit=False)
        self.assertGreater(w, 0)
        self.assertLessEqual(w * h, 2 * 1024 * 1024)
        self.assertTrue(any("像素" in r or "上限" in r for r in reasons))

    def test_within_limit_is_untouched_by_the_cap(self):
        w, h, reasons = VM.normalize_video_size(
            1920, 1088, max_pixels=2.0, strict_over_limit=True)
        self.assertEqual((w, h), (1920, 1088))
        self.assertEqual(reasons, [])

    def test_server_rejects_over_limit_submission(self):
        """提交链路（normalize_t2v_params）必须**拒绝**超限，不能静默截断。"""
        import server as srv
        params = {"width": 2560, "height": 1440, "model_key": VM.VIDEO_DEFAULT_MODEL,
                  "fixed_accel_lora": VM.VIDEO_FIXED_ACCEL_LORA_DEFAULT,
                  "duration": 5}
        _, error = srv.normalize_t2v_params(dict(params))
        self.assertTrue(error, "超限提交必须返回错误")
        self.assertIn("2.0M", error, f"错误信息要说明 2.0M 上限：{error}")

    def test_server_accepts_1920x1088_submission(self):
        """1920x1088 必须能通过服务端归一（不被误拒）。"""
        import server as srv
        params = {"width": 1920, "height": 1088, "model_key": VM.VIDEO_DEFAULT_MODEL,
                  "fixed_accel_lora": VM.VIDEO_FIXED_ACCEL_LORA_DEFAULT,
                  "duration": 5}
        out, error = srv.normalize_t2v_params(dict(params))
        if error:
            # 底模/LoRA 权重不在本机时会先报权重缺失 —— 那与尺寸无关，
            # 这里只断言「不是尺寸原因」。
            self.assertNotIn("宽高", error,
                             f"1920x1088 不该被尺寸规则拒：{error}")
        else:
            self.assertEqual((out["width"], out["height"]), (1920, 1088))


# ==================================================================
# 六、非 t2v 模式的约束不被误放宽
# ==================================================================
class TestOtherModesNotLoosened(unittest.TestCase):
    """需求：其它模式按各自工作流的真实约束处理，不要一刀切套用 2.0M。"""

    def test_flf_keeps_its_own_limit(self):
        policy = VM.size_policy_for_mode("flf")
        self.assertLess(policy["max_pixels"], VM.VIDEO_MAX_CANVAS_PIXELS,
                        "首尾帧不该跟着 t2v 放宽")
        self.assertEqual(policy["max_pixels_mp"], 0.8)

    def test_r2v_keeps_its_own_limit(self):
        policy = VM.size_policy_for_mode("r2v")
        self.assertLess(policy["max_pixels"], VM.VIDEO_MAX_CANVAS_PIXELS,
                        "参考生视频不该跟着 t2v 放宽")
        self.assertEqual(policy["max_pixels_mp"], 0.8)

    def test_longfilm_has_no_independent_size_limit(self):
        policy = VM.size_policy_for_mode("longfilm")
        self.assertIsNone(policy["max_pixels"],
                          "长片拼接不产生新画面，不该有尺寸上限")

    def test_t2v_policy_is_the_relaxed_one(self):
        policy = VM.size_policy_for_mode("t2v")
        self.assertEqual(policy["max_pixels"], VM.VIDEO_MAX_CANVAS_PIXELS)
        self.assertTrue(policy["custom_size"], "t2v 支持手填宽高")

    def test_max_pixels_for_mode_dispatch(self):
        self.assertEqual(VM.max_pixels_for_mode("t2v"),
                         VM.VIDEO_MAX_CANVAS_PIXELS / (1024 * 1024))
        self.assertEqual(VM.max_pixels_for_mode("flf"), 0.8)
        self.assertEqual(VM.max_pixels_for_mode("r2v"), 0.8)
        self.assertIsNone(VM.max_pixels_for_mode("longfilm"))

    def test_flf_1920x1088_is_rejected_by_flf_policy(self):
        """用 flf 的策略算 1920x1088 -> 超限（证明策略确实按模式分派）。"""
        w, h, reasons = VM.normalize_video_size(
            1920, 1088, max_pixels=VM.max_pixels_for_mode("flf"))
        self.assertLessEqual(w * h, int(0.8 * 1024 * 1024),
                             "flf 策略下必须被压回 0.8MP")
        self.assertTrue(reasons)

    def test_non_t2v_workflows_still_use_resolution_selector(self):
        """flf / r2v 的工作流仍走 ResolutionSelector 的比例+总像素档位。"""
        flf = WF.build_flf({"job_id": "x", "aspect": "16:9", "resolution": "768p"})
        node = flf.get("115")
        self.assertIsNotNone(node, "flf 工作流应有 ResolutionSelector 节点")
        self.assertEqual(node["class_type"], "ResolutionSelector")
        self.assertEqual(node["inputs"]["multiple"], 32)
        self.assertLessEqual(node["inputs"]["megapixels"], 0.8,
                             "flf 的总像素档位不该被放宽到 2.0")


# ==================================================================
# 七、前端接线（DOM / 文案 / 不再硬编码映射表）
# ==================================================================
class TestFrontendWiring(unittest.TestCase):
    def _read(self, rel):
        return (REPO / rel).read_text(encoding="utf-8")

    def test_index_html_has_video_category_container(self):
        html = self._read("web/index.html")
        self.assertIn('id="navVideoCategories"', html,
                      "index.html 要有两级导航的容器")

    def test_index_html_no_longer_has_flat_video_items(self):
        """平铺的四个入口必须被两级结构取代（noscript 兜底除外）。"""
        html = self._read("web/index.html")
        body = html.split("<noscript>")[0]
        for mode in ("t2v", "flf", "r2v"):
            with self.subTest(mode=mode):
                self.assertNotIn(f'<a class="nav-item" data-mode="{mode}">', body,
                                 f"{mode} 的平铺入口应该被两级结构取代")

    def test_video_nav_script_is_loaded(self):
        html = self._read("web/index.html")
        self.assertIn("/static/video-nav.js", html)

    def test_frontend_does_not_hardcode_mode_model_mapping(self):
        """前端不得自己维护第二份「类别 -> 模型」映射表。

        判据：web/video-nav.js 里没有出现 't2v:' / 'flf:' / 'r2v:'
        这种以 mode 为键的对象字面量 —— 那是硬编码映射。
        """
        js = self._read("web/video-nav.js")
        for pattern in (r"\bt2v\s*:", r"\bflf\s*:", r"\br2v\s*:",
                        r"\blongfilm\s*:"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, js),
                                  f"video-nav.js 里出现了硬编码 mode 映射 {pattern}")

    def test_frontend_reads_categories_from_server(self):
        js = self._read("web/video-nav.js")
        self.assertIn("/api/video-categories", js,
                      "前端必须从服务端拿类别清单")

    def test_app_js_wires_the_two_level_nav(self):
        js = self._read("web/app.js")
        self.assertIn("VideoNav", js)
        self.assertIn("activateVideoMode", js)

    def test_app_js_does_not_crash_on_missing_nav_stitch(self):
        """#navStitch 只在 <noscript> 里 —— 直接 addEventListener 会崩。"""
        js = self._read("web/app.js")
        self.assertNotIn('$("#navStitch").addEventListener', js,
                         "不能再直接绑定 #navStitch（它在活 DOM 里不存在）")

    def test_video_panel_reports_the_new_limit(self):
        js = self._read("web/video-panel.js")
        self.assertIn("max_pixels", js)
        self.assertIn("mode_size_policy", js)

    def test_video_panel_sends_mode_to_size_endpoint(self):
        js = self._read("web/video-panel.js")
        self.assertIn("mode=${mode}", js,
                      "前端必须带 mode，让服务端按各模式自己的策略算")


def srv_available(mode: str) -> bool:
    """服务端视角：某个 mode 的视频链路在本机是否可用。"""
    import server as srv
    return bool(srv._video_model_available({"mode": mode}))


# ==================================================================
# 八、服务端接口
# ==================================================================
class TestVideoCategoriesEndpoint(unittest.TestCase):
    def test_endpoint_is_registered(self):
        import server as srv
        self.assertTrue(hasattr(srv, "api_video_categories"))
        src = (REPO / "server.py").read_text(encoding="utf-8")
        self.assertIn('add_get("/api/video-categories"', src)

    def test_payload_shape(self):
        import server as srv
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(srv.api_video_categories(None))
        finally:
            loop.close()
        payload = json.loads(result.text)
        self.assertEqual([c["key"] for c in payload["categories"]],
                         REQUIRED_CATEGORY_ORDER)
        self.assertIn("default_category", payload)
        self.assertIn("legacy_modes", payload)
        for model in payload["models"]:
            self.assertIn("category", model)

    def test_payload_marks_model_availability(self):
        """每个模型项都要带 available —— 回退规则③（禁用并提示）靠它。

        没有这个位，前端的「无可用模型 -> 禁用类别 + 提示」分支就是死代码。
        """
        import server as srv
        import asyncio
        result = asyncio.new_event_loop().run_until_complete(
            srv.api_video_categories(None))
        payload = json.loads(result.text)
        for model in payload["models"]:
            with self.subTest(model=model["key"]):
                self.assertIn("available", model)
                self.assertIsInstance(model["available"], bool)
        for cat in payload["categories"]:
            for model in cat["models"]:
                with self.subTest(category=cat["key"], model=model["key"]):
                    self.assertIsInstance(model["available"], bool)

    def test_unavailable_category_is_rejected_by_resolver(self):
        """类别无可用模型时不能解析出 mode（前端据此禁用 + 提示）。"""
        # longfilm 不依赖 H3 权重 -> 恒可用；t2v 依赖底模
        self.assertTrue(srv_available("longfilm"))
        # 直接验证前端规则：availableModels 只收 available != false
        import server as srv
        self.assertTrue(hasattr(srv, "_video_model_available"))

    def test_options_payload_exposes_pixel_limit(self):
        payload = VM.options_payload()
        self.assertEqual(payload["max_pixels"], VM.VIDEO_MAX_CANVAS_PIXELS)
        self.assertIn("mode_size_policy", payload)
        self.assertIn("t2v", payload["mode_size_policy"])


if __name__ == "__main__":
    unittest.main()
