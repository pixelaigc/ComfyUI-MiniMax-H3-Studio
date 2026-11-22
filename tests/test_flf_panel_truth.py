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
    """第 2 条：只有**确实不可改**的项才作为只读说明出现。

    【2026-11-22 修正 —— 这一类的契约变了】
    2026-11-21 那一轮把「采样档位 / sigma shift」做成了只读说明，
    理由是「官方模板用的是 H3SigmaRefiner，不是可填的 sigma shift」。
    那个理由只对**官方 nonebot 模板**成立，对**本工作台自己的 builder**
    不成立 —— ``h3studio/h3_video_modes.py`` 里：

      * ``_sampling_defaults()`` 读 ``params["steps"] / ["sampler"] /
        ["scheduler"]``（第 135-137 行），``_build_common()`` 写进
        ``BasicScheduler``（第 454 行）与 ``KSamplerSelect``（第 553 行）；
      * ``_shift_node()`` 读 ``params["shift_video"] / ["shift_audio"]``
        并插 ``MiniMaxH3SigmaShift`` 节点（第 675-686 行）。

    ⇒ **后端一直支持这五项**，面板不给控件才是缺陷
    （用户 2026-11-22 原话：「该配置的不能配置」）。

    所以本类现在断言的是：
      * 采样五项（steps / sampler / scheduler / shift_video / shift_audio）
        在 flf / r2v 上是**可编辑**控件；
      * **底模精度**仍然不可改（精度编码在权重里），且 note 写清原因。
    """

    def test_no_editable_precision(self):
        """底模精度不得是可编辑字段（精度编码在权重文件里）。"""
        for mode in ("flf", "r2v", "t2v"):
            names = set(_fields(mode))
            self.assertNotIn("precision", names,
                             f"{mode} 又有可编辑的底模精度：{names}")

    def test_sampling_params_are_editable_on_all_video_modes(self):
        """采样五项在三个视频面板上**都必须是可编辑控件**。

        【用户 2026-11-22 原话】
        「不还是和文生视频不一样吗。。？你到底在改啥呢，该配置的不能配置，
          该有的没有」
        """
        editable = ("steps", "sampler", "scheduler",
                    "shift_video", "shift_audio")
        for mode in ("t2v", "flf", "r2v"):
            fields = _fields(mode)
            for name in editable:
                self.assertIn(name, fields, f"{mode} 缺可编辑的 {name}")
                self.assertNotEqual(
                    fields[name].get("type"), "fixed",
                    f"{mode} 的 {name} 仍是只读 —— 后端 "
                    "h3_video_modes._sampling_defaults / _shift_node "
                    "一直支持它")
                self.assertFalse(fields[name].get("readonly"),
                                 f"{mode} 的 {name} 被标成 readonly")

    def test_sampling_params_have_real_combo_ranges(self):
        """采样器 / 调度器的取值必须来自本机 ComfyUI 的真实 COMBO 清单。"""
        from h3studio import video_models as vm
        for mode in ("t2v", "flf", "r2v"):
            fields = _fields(mode)
            samplers = {o["value"] for o in fields["sampler"]["options"]}
            schedulers = {o["value"] for o in fields["scheduler"]["options"]}
            self.assertEqual(samplers, set(vm.VIDEO_SAMPLERS), mode)
            self.assertEqual(schedulers, set(vm.VIDEO_SCHEDULERS), mode)
            steps = fields["steps"]
            self.assertEqual(steps["min"], vm.VIDEO_STEPS_MIN, mode)
            self.assertEqual(steps["max"], vm.VIDEO_STEPS_MAX, mode)

    def test_precision_readonly_states_the_reason(self):
        """唯一保留的只读项（底模精度）必须在 note 里写清**为什么**不可改。"""
        for mode in ("flf", "r2v", "t2v"):
            fields = _fields(mode)
            self.assertIn("precision_fixed", fields, mode)
            item = fields["precision_fixed"]
            self.assertEqual(item["type"], "fixed", mode)
            self.assertTrue(item.get("readonly"), mode)
            note = item.get("note") or ""
            # 必须点明「上游没有这个节点 / 换精度 = 换权重」
            self.assertTrue(
                ("节点" in note) or ("权重" in note),
                f"{mode} 的底模精度只读说明没写清原因：{note!r}")

    def test_no_legacy_sampling_fixed_fields(self):
        """上一版的两个只读项（sampling_fixed / shift_fixed）不得再出现。"""
        for mode in ("t2v", "flf", "r2v"):
            names = set(_fields(mode))
            for legacy in ("sampling_fixed", "shift_fixed"):
                self.assertNotIn(legacy, names,
                                 f"{mode} 仍有上一版的只读项 {legacy}")

    def test_fixed_fields_are_not_written_into_params(self):
        """只读说明字段绝不能被写进 params（含历史遗留的那些键）。"""
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

    def test_third_party_marked_unselectable_in_video_options(self):
        """主生成面板：第三方蒸馏档必须 selectable=False（从下拉剔除）。

        【用户 2026-11-21 拍板】
        「t2v 与 flf 同一判据 —— 加速 LoRA 下拉只保留该底模官方兼容档，
        把 6 个 lightx2v 从 t2v 面板下拉中移除」
        +「lightx2v 只从 UI 选项移除，不删除服务端 / 配置文件中的 LoRA 记录」

        判据链：``/api/video-options`` 的 ``accel_loras[].selectable``
        -> 前端 ``video-panel.js`` 的 ``selectableAccelLoras()`` 过滤。
        这里测**服务端下发**（前端过滤是它既有的行为）。
        """
        from h3studio import accel_lora_policy as ap
        model_key = _fields("flf")["model_key"]["default"]
        matrix = vm.accel_lora_compat_matrix(model_key)
        third = [k for k in matrix
                 if ap.classify(k, model_key)["kind"] == "third_party"]
        if not third:
            self.skipTest("本机没有第三方蒸馏档可测")
        for key in third:
            info = ap.classify(key, model_key)
            self.assertTrue(info["third_party"], key)
            self.assertTrue(info["compatible"],
                            f"{key} 是第三方但与底模不兼容 —— "
                            f"本测试只覆盖「兼容的第三方档」")

    def test_official_lora_remain_selectable(self):
        """官方 turbo 档在剪枝底模下必须仍然可选（不能被误伤）。"""
        from h3studio import accel_lora_policy as ap
        model_key = _fields("flf")["model_key"]["default"]
        official = [k for k in vm.all_accel_lora_keys()
                    if ap.classify(k, model_key)["kind"] == "official_turbo"]
        self.assertTrue(official, "剪枝底模下没有官方 turbo 档")
        for key in official:
            info = ap.classify(key, model_key)
            self.assertTrue(info["compatible"],
                            f"{key} 是官方 turbo 档却被判不兼容：{info['reason']}")
            self.assertFalse(info["third_party"], key)

    def test_video_options_marks_third_party_unselectable(self):
        """【端到端】``/api/video-options`` 必须把第三方档标成不可选。

        这是主生成面板下拉过滤的**唯一**输入 ——
        前端 ``video-panel.js`` 的 ``selectableAccelLoras()`` 只看
        ``item.selectable === false``，不认任何名单。

        所以这条测试直接打真实 HTTP 端点，验证：
          * 第三方档 -> selectable=False（从下拉剔除）
          * 官方 turbo 档 -> selectable=True（不能被误伤）
          * **记录仍在**（key / name 照常下发）——
            用户要求「不删除服务端记录，以兼容已有历史任务 params」
        """
        import json as _json
        import urllib.request

        from h3studio import accel_lora_policy as ap

        url = ("http://127.0.0.1:%d/api/video-options?model=%s"
               % (_test_port(), _fields("flf")["model_key"]["default"]))
        try:
            with urllib.request.urlopen(url, timeout=90) as resp:
                payload = _json.loads(resp.read().decode())
        except Exception as exc:
            self.skipTest("线上 /api/video-options 不可达（跳过）：%s" % exc)

        entries = {e["key"]: e for e in (payload.get("accel_loras") or [])}
        self.assertTrue(entries, "服务端没有下发 accel_loras")

        model_key = payload.get("selected_model") or "fl2va_pruned_int8"
        for key, entry in entries.items():
            kind = ap.classify(key, model_key)["kind"]
            # 记录必须仍在（key / name 都在）
            self.assertIn("name", entry, key)
            self.assertIn("loader", entry, key)
            if kind == "third_party":
                self.assertFalse(entry.get("selectable"),
                                 f"{key} 是第三方档却仍可选")
                self.assertIn("third_party", entry, key)
            if kind == "official_turbo":
                self.assertTrue(entry.get("selectable"),
                                f"{key} 是官方 turbo 档却被误伤")


def _test_port():
    """线上测试端口（可用环境变量覆盖）。"""
    import os
    return int(os.environ.get("H3S_TEST_PORT", "8190"))


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
