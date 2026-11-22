# -*- coding: utf-8 -*-
"""三个视频面板（t2v / flf / r2v）的**参数一致性**回归测试。

【用户 2026-11-22 的原话】
  「不还是和文生视频不一样吗。。？你到底在改啥呢，该配置的不能配置，
    该有的没有」
  「LORA模型都不对，并切同时存在百万像素和分辨率挡位和比例，还有底模精度，
    全部都是冲突的。和文生视频完全不一样好吧」
  「文生视频，我选的模型+LORA组合，和生成的记录的模型+LORA组合，不一样」
  「右侧生成库里，分辨率的尺寸显示，和我实际跑的不一样」

【这一层测什么】—— 五个维度，各自独立：
  1. **控件集合一致**：三个面板的采样参数 / 尺寸口径 / LoRA 控件**同名同义**；
  2. **LoRA 清单真源一致**：三个面板的下拉来自**同一份**真实清单，
     显示名取自权重登记表（**不自造名称**）；
  3. **提交 == 记录**：面板配的采样参数必须**原样**进 params，
     且落进 ComfyUI 工作流 JSON 的对应节点；
  4. **记录回显 == 提交**：选了什么 LoRA，任务记录里就只有那一枚
     （不能同时躺着两枚互相矛盾的）；
  5. **卡片尺寸取值**：卡片/详情显示的分辨率档位由**真实像素**反算，
     不是请求时记的档位串。

运行：python3 -m pytest tests/test_video_panel_param_parity.py -q
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import gen_defaults                          # noqa: E402
from h3studio import h3_video_modes                         # noqa: E402
from h3studio import reso_label                            # noqa: E402
from h3studio import video_models as vm                     # noqa: E402
from h3studio import video_panel_params as vpp              # noqa: E402

VIDEO_MODES = ("t2v", "flf", "r2v")

#: 三个面板必须共有的采样参数（与 t2v 同名同义）
SAMPLING_FIELDS = ("steps", "sampler", "scheduler",
                   "shift_video", "shift_audio")

#: 三个面板必须共有的尺寸表达
SIZE_FIELDS = ("aspect", "megapixels", "resolution_percent")


def _fields(mode):
    fields, supported = gen_defaults._mode_fields(mode)
    assert supported, mode
    return {f["name"]: f for f in fields}


class TestControlParity(unittest.TestCase):
    """第 1 条：三个面板的控件集合一致。"""

    def test_sampling_fields_present_on_all_modes(self):
        """采样五项在三个面板上都在，且**都是可编辑**。"""
        for mode in VIDEO_MODES:
            fields = _fields(mode)
            for name in SAMPLING_FIELDS:
                self.assertIn(name, fields, f"{mode} 缺 {name}")
                self.assertNotEqual(fields[name].get("type"), "fixed",
                                    f"{mode}.{name} 被做成只读了")
                self.assertFalse(fields[name].get("readonly"),
                                 f"{mode}.{name} 标了 readonly")

    def test_sampling_field_types_and_bounds_identical(self):
        """采样字段的**类型与区间**在三个面板上完全一致。"""
        base = _fields("t2v")
        for mode in ("flf", "r2v"):
            fields = _fields(mode)
            for name in SAMPLING_FIELDS:
                for key in ("type", "min", "max", "step"):
                    self.assertEqual(
                        fields[name].get(key), base[name].get(key),
                        f"{mode}.{name}.{key} 与 t2v 不一致")

    def test_sampling_option_values_identical(self):
        """采样器 / 调度器的**选项集合**在三个面板上完全一致。"""
        base = _fields("t2v")
        for mode in ("flf", "r2v"):
            fields = _fields(mode)
            for name in ("sampler", "scheduler"):
                self.assertEqual(
                    [o["value"] for o in fields[name]["options"]],
                    [o["value"] for o in base[name]["options"]],
                    f"{mode}.{name} 的选项与 t2v 不一致")

    def test_size_fields_present_on_all_modes(self):
        """尺寸表达（比例 × MP 预算 × 百分比）在三个面板上都在。"""
        for mode in VIDEO_MODES:
            names = set(_fields(mode))
            for name in SIZE_FIELDS:
                self.assertIn(name, names, f"{mode} 缺 {name}")

    def test_no_conflicting_size_expressions(self):
        """不得同时出现「MP 预算 / 分辨率档位 / 宽高」等第二套尺寸表达。

        【用户原话】「同时存在百万像素和分辨率挡位和比例 … 全部都是冲突的」
        """
        for mode in VIDEO_MODES:
            names = set(_fields(mode))
            for banned in ("resolution", "width", "height",
                           "size_preset", "short_edge", "precision"):
                self.assertNotIn(
                    banned, names,
                    f"{mode} 出现冲突的尺寸/精度表达 {banned}：{sorted(names)}")

    def test_derived_pixels_is_readonly_on_all_modes(self):
        """「生效像素」在三个面板上都是**只读派生**项。"""
        for mode in VIDEO_MODES:
            fields = _fields(mode)
            self.assertIn("derived_pixels", fields, mode)
            item = fields["derived_pixels"]
            self.assertEqual(item["type"], "fixed", mode)
            self.assertTrue(item.get("readonly"), mode)

    def test_only_precision_remains_readonly(self):
        """唯一保留的只读项是「底模精度」，且 note 写清**为什么**不可改。"""
        for mode in VIDEO_MODES:
            fields = _fields(mode)
            fixed = [f["name"] for f in fields.values()
                     if f.get("type") == "fixed"]
            self.assertIn("precision_fixed", fixed, mode)
            for name in fixed:
                if name == "precision_fixed":
                    continue
                # 其它只读项必须**明确说明**它是派生结果（不能藏缺陷）
                note = fields[name].get("note") or ""
                self.assertTrue("派生" in note or "只读" in note,
                                f"{mode}.{name} 的只读说明没写清性质")


class TestAccelLoraParity(unittest.TestCase):
    """第 2 条：LoRA 清单真源一致 + 不自造名称。"""

    def test_all_three_modes_share_one_lora_list(self):
        """三个面板的加速 LoRA 下拉是**同一份清单**（同一组 value + label）。"""
        lists = {}
        for mode in VIDEO_MODES:
            field = _fields(mode)["accel_lora" if mode == "t2v" else "lora"]
            lists[mode] = [(o["value"], o["label"]) for o in field["options"]]
        self.assertEqual(lists["flf"], lists["t2v"],
                         "flf 与 t2v 的 LoRA 清单不一致")
        self.assertEqual(lists["r2v"], lists["t2v"],
                         "r2v 与 t2v 的 LoRA 清单不一致")

    def test_lora_labels_come_from_registry(self):
        """显示名必须取自权重登记表（**禁止自造名称**）。"""
        for mode in VIDEO_MODES:
            field = _fields(mode)["accel_lora" if mode == "t2v" else "lora"]
            for opt in field["options"]:
                key = opt["value"]
                if key == "none":
                    continue
                meta = vm.accel_lora_meta(key) or {}
                self.assertIn(key, vm.all_accel_lora_keys(),
                              f"{mode}: {key} 不在真实登记表里")
                self.assertEqual(opt["label"], meta.get("display") or key,
                                 f"{mode}: {key} 的显示名与登记表不一致")

    def test_lora_options_are_real_and_compatible(self):
        """选项里的每一枚都必须本机存在 + 与该底模实测兼容。"""
        for mode in VIDEO_MODES:
            fields = _fields(mode)
            field = fields["accel_lora" if mode == "t2v" else "lora"]
            model_key = fields["model_key"]["default"]
            matrix = vm.accel_lora_compat_matrix(model_key) or {}
            for opt in field["options"]:
                key = opt["value"]
                if key == "none":
                    continue
                meta = vm.accel_lora_meta(key) or {}
                self.assertTrue(vm.lora_available(meta.get("name") or ""),
                                f"{mode}: {key} 本机没有这枚权重")
                check = matrix.get(key) or {}
                self.assertTrue(check.get("compatible"),
                                f"{mode}: {key} 与 {model_key} 实测不兼容，"
                                f"不应出现在选项里：{check.get('reason')}")

    def test_no_third_party_in_any_mode(self):
        """第三方蒸馏（LightX2V 系）不得出现在任何面板的下拉里。"""
        from h3studio import accel_lora_policy as policy
        for mode in VIDEO_MODES:
            fields = _fields(mode)
            field = fields["accel_lora" if mode == "t2v" else "lora"]
            options = {o["value"] for o in field["options"]}
            model_key = fields["model_key"]["default"]
            for key in vm.all_accel_lora_keys():
                if key in options:
                    info = policy.classify(key, model_key)
                    self.assertFalse(
                        info["third_party"],
                        f"{mode}: {key} 是第三方蒸馏档，不该进下拉")


class TestSubmitEqualsRecord(unittest.TestCase):
    """第 3 条：面板配的值 == 提交参数 == 工作流 JSON。"""

    def _merged_params(self, mode, overrides):
        """模拟「管理页默认值套进 Agent 建任务参数」后的 params。"""
        saved = {mode: dict(overrides)}
        params = {"prompt": "x", "job_id": "test-job"}
        gen_defaults.merge_into_params(
            mode, params, saved, explicit=set(),
            overridable=set(), skip_sampling=False)
        return params

    def test_sampling_survives_merge_into_params(self):
        """采样五项不能被 merge_into_params 吃掉（它们是**可配**的）。"""
        overrides = {
            "model_key": "fl2va_pruned_int8", "aspect": "16:9",
            "megapixels": 0.8, "resolution_percent": 100.0,
            "duration": 5.0, "lora": "ckpt850", "lora_strength": 1.0,
            "steps": 8, "sampler": "dpm_2", "scheduler": "karras",
            "shift_video": 7.5, "shift_audio": 1.25,
        }
        for mode in ("flf", "r2v"):
            params = self._merged_params(mode, overrides)
            for name in SAMPLING_FIELDS:
                self.assertIn(name, params, f"{mode} 的 {name} 被丢了")
            self.assertEqual(params["steps"], 8, mode)
            self.assertEqual(params["sampler"], "dpm_2", mode)
            self.assertEqual(params["scheduler"], "karras", mode)
            self.assertAlmostEqual(float(params["shift_video"]), 7.5,
                                   places=4, msg=mode)
            self.assertAlmostEqual(float(params["shift_audio"]), 1.25,
                                   places=4, msg=mode)

    def test_workflow_json_carries_the_configured_sampling(self):
        """配的采样参数必须**原样**进 ComfyUI 工作流 JSON。"""
        overrides = {
            "model_key": "fl2va_pruned_int8", "aspect": "16:9",
            "megapixels": 0.8, "resolution_percent": 100.0,
            "duration": 5.0, "lora": "ckpt850", "lora_strength": 1.0,
            "steps": 8, "sampler": "dpm_2", "scheduler": "karras",
            "shift_video": 7.5, "shift_audio": 1.25,
        }
        for mode in ("flf", "r2v"):
            params = self._merged_params(mode, overrides)
            params["mode"] = mode
            if mode == "flf":
                params["first_frame"] = "frame.png"
            else:
                params["ref_images"] = ["frame.png"]
            wf = (h3_video_modes.build_flf(params) if mode == "flf"
                  else h3_video_modes.build_r2v(params))

            sched = (wf.get("9") or {}).get("inputs") or {}
            self.assertEqual(sched.get("steps"), 8, f"{mode}: BasicScheduler.steps")
            self.assertEqual(sched.get("scheduler"), "karras",
                             f"{mode}: BasicScheduler.scheduler")

            ksampler = (wf.get("17") or {}).get("inputs") or {}
            self.assertEqual(ksampler.get("sampler_name"), "dpm_2",
                             f"{mode}: KSamplerSelect.sampler_name")

            shift = None
            for node in wf.values():
                if isinstance(node, dict) and \
                        node.get("class_type") == "MiniMaxH3SigmaShift":
                    shift = node.get("inputs") or {}
                    break
            self.assertIsNotNone(shift, f"{mode}: 工作流里没有 SigmaShift 节点")
            _shift = shift or {}
            self.assertAlmostEqual(float(_shift.get("shift_video") or 0.0), 7.5,
                                   places=4, msg=mode)
            self.assertAlmostEqual(float(_shift.get("shift_audio") or 0.0), 1.25,
                                   places=4, msg=mode)


class TestRecordEchoMatchesSelection(unittest.TestCase):
    """第 4 条：记录回显 == 提交（不允许两枚矛盾的 LoRA）。"""

    def test_legacy_lora_alias_matches_selected_accel_lora(self):
        """选了加速 LoRA 后，遗留的 ``params["lora"]`` 必须与它一致。

        【用户原话】「我选的模型+LORA组合，和生成的记录的模型+LORA组合，不一样」
        """
        from h3studio import agent as agent_mod
        # 直接测归一后的落库 params：Agent 路径会先填 mode_defaults 的
        # lora=ckpt850，再被「用户选的那枚」覆盖。
        params = {
            "mode": "t2v", "prompt": "x",
            "accel_lora": "dmad_4step_critic",
            "accel_lora_strength": 1.0,
            "accel_loras": [{"key": "dmad_4step_critic", "strength": 1.0,
                             "enabled": True}],
        }
        # 模拟 agent.py 里的那段归一（与实际实现同一份逻辑）
        agent_mod._sync_legacy_lora_alias(params)
        self.assertEqual(params["lora"], "dmad_4step_critic")
        self.assertAlmostEqual(float(params["lora_strength"]), 1.0, places=4)

    def test_no_accel_lora_maps_to_none(self):
        """「不挂加速 LoRA」-> 遗留字段必须是 "none"，不能回落到 ckpt850。"""
        from h3studio import agent as agent_mod
        params = {"mode": "t2v", "prompt": "x",
                  "accel_lora": "", "accel_lora_strength": 1.0}
        agent_mod._sync_legacy_lora_alias(params)
        self.assertEqual(params["lora"], "none")

    def test_display_layer_shows_the_selected_one(self):
        """显示层（resolve_job_lora）必须给出用户选的那一枚。"""
        from h3studio import job_meta
        params = {"accel_lora": "dmad_4step_critic",
                  "accel_lora_strength": 1.0,
                  "accel_loras": [{"key": "dmad_4step_critic",
                                   "strength": 1.0, "enabled": True}],
                  "lora": "dmad_4step_critic"}
        result = job_meta.resolve_job_lora("t2v", params)
        self.assertEqual(result["lora"], "dmad_4step_critic")


class TestCardResolutionFromRealPixels(unittest.TestCase):
    """第 5 条：卡片 / 详情显示的分辨率来自**真实像素**。"""

    def test_tier_is_recomputed_from_pixels(self):
        """params 里的档位串与实际像素不一致时，档位必须按像素重算。"""
        from h3studio import job_meta
        params = {"resolution": "768p", "width": 1216, "height": 672,
                  "aspect": "16:9"}
        info = job_meta.summarize("flf", params)
        # 1216×672 的短边是 672 -> 档位必须是 672p，不是请求时记的 768p
        self.assertEqual(info["resolution"], "672p")
        self.assertEqual(info["width"], 1216)
        self.assertEqual(info["height"], 672)

    def test_no_pixels_keeps_requested_tier(self):
        """拿不到像素时保留请求里的档位（**如实**呈现，不编造）。"""
        from h3studio import job_meta
        params = {"resolution": "768p", "aspect": "16:9"}
        info = job_meta.summarize("flf", params)
        self.assertEqual(info["resolution"], "768p")

    def test_card_label_matches_actual_pixels(self):
        """卡片标签（resolution_info）必须与真实像素一致。"""
        actual = {"width": 1920, "height": 1088}
        info = reso_label.describe({"resolution": "1088p"}, actual)
        self.assertEqual(info["source"], "actual")
        self.assertEqual((info["width"], info["height"]), (1920, 1088))
        self.assertIn("1920×1088", info["label"])

    def test_artifact_meta_uses_pixel_tier_not_request_tier(self):
        """``/api/jobs/{id}/artifact-meta`` 的 resolution 必须是像素反算的档位。

        【用户原话】「右侧生成库里，分辨率的尺寸显示，和我实际跑的不一样」
        """
        import inspect
        src = inspect.getsource(_artifact_meta_source())
        self.assertIn("reso_label.tier_for_pixels", src)
        # 不允许再直接用 job_params.get("resolution") 作为分辨率下发
        self.assertNotIn('"resolution": job_params.get("resolution") or None', src)


def _artifact_meta_source():
    """拿到 api_artifact_meta 的源码对象（用于上面的源码级断言）。"""
    import server
    return server.api_artifact_meta


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
