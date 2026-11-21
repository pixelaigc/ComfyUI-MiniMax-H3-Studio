# -*- coding: utf-8 -*-
"""内置 Agent 管理页 flf 默认参数的三条硬约束（2026-11-21 用户驳回后新增）。

【用户原话】
  「你管这个叫完成已上线？LORA模型都不对，并且同时存在百万像素和分辨率挡位和比例，
    还有底模精度，全部都是冲突的。和文生视频完全不一样好吧」

【这一层测什么】—— 三条，各自独立：
  1. **尺寸表达唯一性**：flf / r2v 不得再有独立的「分辨率档位」字段，
     尺寸表达只能是 画面比例 × MP 预算 × 百分比 一套；
  2. **不可编辑项不暴露**：官方工作流里固定不变的项（底模精度 / 采样档位 /
     sigma shift）不得作为可编辑控件出现，只能作为**只读说明**；
  3. **LoRA 与模型族兼容性过滤**：加速 LoRA 下拉只允许「本机真实存在 +
     与该底模逐层实测兼容 + 属于官方 H3 谱系」的档位，
     且这份清单**必须由服务端真实数据派生**（不是硬编码名单）。

【为什么这些判据不是拍脑袋】
  * 官方首尾帧工作流 = nonebot ``workflows/minimax_h3_v1.json``
    （``h3_executor.py:199-231`` 明确 fl2va 走这个模板）；
  * 尺寸表达 = 该模板节点 115 ``ResolutionSelector {aspect_ratio,
    megapixels, multiple: 32}`` —— 只有比例 + MP 预算；
  * 加速 LoRA = 该模板节点 333 ``LoraLoaderModelOnly
    {lora_name: minimax_h3_turbo_v4_step600_ema, strength_model: 0.7}``；
  * 「官方谱系 vs 第三方」= 权重自己的 safetensors ``__metadata__``
    （``base_model: MiniMax-H3`` vs ``Comfy-Org``），字节级证据。

运行：python3 -m pytest tests/test_flf_panel_truth.py -q
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import accel_lora_policy as accel_policy   # noqa: E402
from h3studio import agent_capability                      # noqa: E402
from h3studio import agent_schema                          # noqa: E402
from h3studio import gen_defaults                          # noqa: E402
from h3studio import video_models as vm                    # noqa: E402


def _fields(mode):
    fields, supported = gen_defaults._mode_fields(mode)
    assert supported, mode
    return {f["name"]: f for f in fields}


class TestSizeExpressionUniqueness(unittest.TestCase):
    """第 1 条：尺寸表达只能有一套。"""

    def test_no_resolution_tier_field(self):
        """flf / r2v 不得出现独立的「分辨率档位」字段。"""
        for mode in ("flf", "r2v"):
            names = list(_fields(mode))
            self.assertNotIn("resolution", names,
                             f"{mode} 又有独立的分辨率档位字段：{names}")

    def test_exactly_one_size_numeric_family(self):
        """不得出现「档位 / 宽高」这类第二套尺寸表达。"""
        for mode in ("flf", "r2v"):
            names = list(_fields(mode))
            extra = [n for n in names
                     if n in ("resolution", "width", "height",
                              "short_edge", "size_preset", "megapixels_preset")]
            self.assertEqual(extra, [],
                             f"{mode} 出现第二套尺寸表达：{extra}")

    def test_size_fields_are_aspect_mp_percent(self):
        """尺寸表达 = 画面比例 × MP 预算 × 百分比（与 t2v 同一套）。"""
        for mode in ("flf", "r2v", "t2v"):
            names = set(_fields(mode))
            self.assertIn("aspect", names, mode)
            self.assertIn("megapixels", names, mode)
            self.assertIn("resolution_percent", names, mode)

    def test_mp_bound_matches_real_policy(self):
        """MP 预算上界 = 该 mode 的**真实**面积上限（不是写死的数）。"""
        for mode in ("flf", "r2v", "t2v"):
            cap = vm.max_pixels_for_mode(mode)
            self.assertIsNotNone(cap, mode)
            assert cap is not None
            self.assertAlmostEqual(
                float(_fields(mode)["megapixels"]["max"]),
                float(cap), places=6, msg=mode)


class TestFixedItemsNotEditable(unittest.TestCase):
    """第 2 条：官方固定项不得作为可编辑控件暴露。"""

    def test_no_editable_precision(self):
        """底模精度不得是可编辑字段（精度编码在权重文件里）。"""
        for mode in ("flf", "r2v"):
            names = set(_fields(mode))
            self.assertNotIn("precision", names,
                             f"{mode} 又有可编辑的底模精度：{names}")

    def test_sampling_and_shift_are_readonly_facts(self):
        """采样档位 / sigma shift 只能作为**只读说明**出现。"""
        for mode in ("flf", "r2v"):
            fields = _fields(mode)
            for name in ("sampling_fixed", "shift_fixed"):
                self.assertIn(name, fields, f"{mode} 缺只读说明 {name}")
                self.assertEqual(fields[name]["type"], "fixed", name)
                self.assertTrue(fields[name].get("readonly"), name)

    def test_editable_sampling_fields_removed(self):
        """steps / sampler / scheduler / shift_* 不得再是可编辑字段。

        【为什么】``server.py`` 的 ``_SAMPLING_ONLY_FIELDS`` 决定了
        **面板提交路径本来就不套**这些字段 —— 面板给了控件，
        用户改了却完全不起作用（server.py:5455-5458）。
        暴露它们 = 「配了但不生效」的假控件。
        """
        for mode in ("flf", "r2v"):
            names = set(_fields(mode))
            for fake in ("steps", "sampler", "scheduler",
                         "shift_video", "shift_audio"):
                self.assertNotIn(fake, names,
                                 f"{mode} 仍有不可生效的可编辑字段 {fake}")

    def test_fixed_fields_are_not_written_into_params(self):
        """只读说明字段绝不能被写进 params。"""
        params = {"prompt": "x", "duration": 5.0}
        gen_defaults.merge_into_params(
            "flf", params,
            {"flf": {"sampling_fixed": 4, "shift_fixed": 12.0,
                     "precision_fixed": "随底模"}},
            explicit=())
        for name in ("sampling_fixed", "shift_fixed", "precision_fixed"):
            self.assertNotIn(name, params, name)


class TestAccelLoraCompatibilityFilter(unittest.TestCase):
    """第 3 条：LoRA 列表按模型族 / 实测兼容性过滤，且由服务端派生。"""

    def test_options_come_from_real_compat_matrix(self):
        """flf 的加速 LoRA 选项必须来自真实兼容性矩阵，不是硬编码名单。"""
        field = _fields("flf")["lora"]
        options = [o["value"] for o in field["options"]]
        self.assertIn("none", options)
        # 至少要有一枚真实可用且兼容的
        real = [k for k in vm.all_accel_lora_keys()]
        self.assertTrue(set(options) - {"none"}, "没有任何加速 LoRA 选项")
        for key in set(options) - {"none"}:
            self.assertIn(key, real, f"{key} 不在真实加速 LoRA 表里")

    def test_options_exclude_incompatible_with_this_base_model(self):
        """与该底模实测不兼容的档位不得出现在选项里。

        判据 = ``accel_lora_compat_matrix``（逐层比对权重头，
        含「加载节点能不能做 AdaLN 运行期重注入」）。
        """
        model_key = _fields("flf")["model_key"]["default"]
        matrix = vm.accel_lora_compat_matrix(model_key)
        options = {o["value"] for o in _fields("flf")["lora"]["options"]}
        for key, check in matrix.items():
            if check.get("compatible"):
                continue
            self.assertNotIn(
                key, options,
                f"{key} 与 {model_key} 实测不兼容（{check.get('reason')}），"
                f"不应出现在选项里")

    def test_third_party_distill_not_in_official_pool(self):
        """第三方蒸馏（LightX2V 系）不得进 flf 的官方加速档池。

        判据 = 权重自己的 safetensors metadata（``base_model: Comfy-Org``）
        + 权重键名形态（第三方一律 ``diffusion_model.`` 前缀 ->
        核心 LoraLoaderModelOnly；官方 turbo 是裸键名 -> Turbo 节点）。
        """
        fields = _fields("flf")
        options = {o["value"] for o in fields["lora"]["options"]}
        for item in accel_policy.available_accel_loras(
                fields["model_key"]["default"]):
            if accel_policy.lora_kind(item) == "third_party":
                self.assertNotIn(
                    item["key"], options,
                    f"{item['key']} 是第三方蒸馏，不该进官方档池")

    def test_official_turbo_pool_is_official_by_bytes(self):
        """官方 turbo 档必须同时满足：官方谱系 + 官方 Turbo 权重形态。"""
        fields = _fields("flf")
        model_key = fields["model_key"]["default"]
        official = [it for it in accel_policy.available_accel_loras(model_key)
                    if accel_policy.lora_kind(it) == "official_turbo"]
        self.assertTrue(official, "FL2VA 底模下没有任何官方 turbo 档可用")
        for item in official:
            self.assertTrue(item["official_h3"], item["key"])
            self.assertEqual(item["loader"],
                             accel_policy.OFFICIAL_TURBO_LOADER, item["key"])

    def test_agent_legal_loras_match_real_compat(self):
        """Agent 的合法 LoRA 集合与真实兼容性一致（同一真源）。"""
        allowed = set(agent_schema.mode_loras("flf"))
        matrix = vm.accel_lora_compat_matrix(
            _fields("flf")["model_key"]["default"])
        for key, check in matrix.items():
            if check.get("compatible"):
                self.assertIn(key, allowed,
                              f"{key} 实测兼容却不在 Agent 合法集合里")

    def test_agent_capability_text_has_no_precision_param(self):
        """Agent 能力自述不得再说「精度」是可调参数。"""
        text = agent_capability.video_capability_text()
        self.assertNotIn("（precision）", text)
        self.assertIn("精度已经编码在", text)
        self.assertIn("没有独立的 precision 参数", text)

    def test_incompatible_lora_is_rejected_with_reason(self):
        """不兼容的 LoRA 必须被服务端明确拒绝并给出原因（不静默加载）。"""
        import server
        model_key = _fields("flf")["model_key"]["default"]
        matrix = vm.accel_lora_compat_matrix(model_key)
        bad = [k for k, c in matrix.items() if not c.get("compatible")]
        if not bad:
            self.skipTest("当前底模下没有不兼容的加速 LoRA 可测")
        key = bad[0]
        _out, err = server.normalize_t2v_params(
            {"mode": "flf", "prompt": "x", "duration": 5.0,
             "width": 1216, "height": 672, "accel_lora": key},
            mode="flf")
        self.assertTrue(err, f"{key} 不兼容却通过了校验")
        self.assertIn("不兼容", err)


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
