# -*- coding: utf-8 -*-
"""加速 LoRA 的「加载节点感知」兼容性判定（2026-11-15）。

【为什么要有这组用例】
AdaLN 的运行期重注入是 **MiniMaxH3TurboLoRA（Turbo 节点）专属**能力：

  * 核心节点 LoraLoaderModelOnly 的权重数学是
        lora_diff = torch.mm(mat1, mat2).reshape(weight.shape)
    其中 mat1 = lora_B [96768, 16]、mat2 = lora_A [16, 2688]。
  * 对剪枝底模（AdaLN 权重 [96768, 8]）这一步直接抛
    「shape '[96768, 8]' is invalid for input of size 260112384」，
    而 comfy/weight_adapter/lora.py:284 用 try/except 吞掉异常 ->
    **AdaLN 的贡献被静默丢弃**，只留一条 ERROR 日志。

所以「带 AdaLN 的 LoRA + 核心加载节点 + 剪枝底模」这个组合**真的不兼容**，
但旧实现无条件把 AdaLN 记成 runtime_injected 并判 compatible=True ——
这正是「静默失效」的另一种形态。

这组用例锁住：**判定必须跟着实际会用的加载节点走。**
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from h3studio import video_models as VM  # noqa: E402


#: 剪枝底模（AdaLN 折叠成 8 维曲线）
PRUNED = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
#: 未剪枝 int8 底模（AdaLN 是 2688 维，与 Turbo 系 LoRA 完全一致）
FULL = "minimax_h3_fl2va_int8_convrot.safetensors"
#: 混合底模（AdaLN 也是 8 维曲线）
HYBRID = "minimax_h3_hybrid_fl2va_ref2va_b30-49-int8.safetensors"

#: 裸键名族（Turbo 节点加载）—— 带 AdaLN
TURBO_BARE = "minimax_h3_turbo_4step_ema_ckpt850.safetensors"
#: diffusion_model. 前缀族（核心节点加载）—— 带 AdaLN
TURBO_PREFIXED = "minimax_h3_turbo_4步加速_comfyui.safetensors"
#: diffusion_model. 前缀族 —— 不带 AdaLN
PREFIXED_NO_ADALN = "h3_lightx2v/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors"


class TestLoaderAwareAdaln(unittest.TestCase):
    """AdaLN 的判定必须跟随加载节点。"""

    def test_turbo_loader_adaln_counts_as_runtime_injected(self):
        """Turbo 节点 + 剪枝底模：AdaLN 记 runtime_injected，兼容。"""
        check = VM.check_h3_lora_compat(
            TURBO_BARE, PRUNED, loader=VM.ACCEL_LOADER_TURBO)
        self.assertTrue(check["compatible"], check["reason"])
        self.assertGreater(check["runtime_injected"], 0,
                           "Turbo 节点下 AdaLN 应算运行期重注入")
        self.assertEqual(check["shape_mismatch"], 0)

    def test_core_loader_adaln_is_incompatible_on_pruned_base(self):
        """核心节点 + 剪枝底模 + 带 AdaLN 的 LoRA：真的不兼容，且原因说清楚。"""
        check = VM.check_h3_lora_compat(
            TURBO_PREFIXED, PRUNED, loader=VM.ACCEL_LOADER_CORE)
        self.assertFalse(check["compatible"],
                         "核心节点无法做 AdaLN 运行期重注入，不该判兼容")
        self.assertEqual(check["runtime_injected"], 0,
                         "核心节点下 AdaLN 不得计入运行期重注入")
        self.assertGreater(check["shape_mismatch"], 0)
        self.assertIn("AdaLN", check["reason"])
        self.assertIn("Turbo", check["reason"],
                      "原因里要指出换用 Turbo 节点 / 未剪枝底模的出路")

    def test_core_loader_adaln_is_compatible_on_unpruned_base(self):
        """核心节点 + 未剪枝底模：AdaLN 形状本来就对得上，兼容。"""
        check = VM.check_h3_lora_compat(
            TURBO_PREFIXED, FULL, loader=VM.ACCEL_LOADER_CORE)
        self.assertTrue(check["compatible"], check["reason"])
        self.assertEqual(check["shape_mismatch"], 0)
        self.assertEqual(check["runtime_injected"], 0)

    def test_loader_defaults_to_detected_one(self):
        """loader 传 None 时按实测键名形态自动判定 —— 两族结论必须不同。"""
        turbo = VM.check_h3_lora_compat(TURBO_BARE, PRUNED)
        core = VM.check_h3_lora_compat(TURBO_PREFIXED, PRUNED)
        self.assertEqual(turbo["loader"], VM.ACCEL_LOADER_TURBO)
        self.assertEqual(core["loader"], VM.ACCEL_LOADER_CORE)
        self.assertTrue(turbo["compatible"])
        self.assertFalse(core["compatible"])

    def test_prefixed_lora_without_adaln_still_compatible_on_pruned(self):
        """不含 AdaLN 的前缀族 LoRA 在剪枝底模上照常兼容（不能误伤）。"""
        check = VM.check_h3_lora_compat(
            PREFIXED_NO_ADALN, PRUNED, loader=VM.ACCEL_LOADER_CORE)
        self.assertTrue(check["compatible"], check["reason"])
        self.assertEqual(check["shape_mismatch"], 0)


class TestCompatMatrixUsesLoader(unittest.TestCase):
    """兼容性矩阵（前端下拉的唯一判据）必须走 loader-aware 判定。"""

    def test_matrix_marks_prefixed_adaln_lora_incompatible_on_pruned(self):
        matrix = VM.accel_lora_compat_matrix("fl2va_pruned_int8")
        entry = matrix.get("turbo_4step_cn")
        self.assertIsNotNone(entry, "新登记的 turbo_4step_cn 应在矩阵里")
        self.assertEqual(entry["loader"], VM.ACCEL_LOADER_CORE)
        self.assertFalse(entry["compatible"],
                         "剪枝底模 + 核心节点 + 带 AdaLN -> 必须不可选")
        self.assertIn("AdaLN", entry["reason"])

    def test_matrix_keeps_turbo_family_compatible_on_pruned(self):
        matrix = VM.accel_lora_compat_matrix("fl2va_pruned_int8")
        entry = matrix.get("ckpt850")
        self.assertTrue(entry["compatible"], entry.get("reason", ""))
        self.assertGreater(entry["runtime_injected"], 0)

    def test_matrix_allows_prefixed_adaln_lora_on_unpruned_base(self):
        matrix = VM.accel_lora_compat_matrix("fl2va_int8")
        entry = matrix.get("turbo_4step_cn")
        self.assertTrue(entry["compatible"], entry.get("reason", ""))


class TestNewCatalogEntries(unittest.TestCase):
    """新登记的加速 LoRA 必须在本机真实存在，且加载节点判定正确。"""

    NEW_ENTRIES = {
        "turbo_4step_cn": ("minimax_h3_turbo_4步加速_comfyui.safetensors",
                            VM.ACCEL_LOADER_CORE),
        "turbo_4step_cn_ema": ("minimax_h3_turbo_4步加速ema_comfyui.safetensors",
                               VM.ACCEL_LOADER_CORE),
        "lightx2v_4step_v01_comfy": (
            "minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy.safetensors",
            VM.ACCEL_LOADER_CORE),
        "lightx2v_sla_768p_root": (
            "minimax_h3_fl2v_turbo_4step_v0.1_768p_sla_comfyui_bf16.safetensors",
            VM.ACCEL_LOADER_CORE),
    }

    def test_entries_are_registered_and_present_on_disk(self):
        for key, (filename, loader) in self.NEW_ENTRIES.items():
            meta = VM.accel_lora_meta(key)
            self.assertIsNotNone(meta, "%s 未登记" % key)
            self.assertEqual(meta["name"], filename)
            self.assertIsNotNone(
                VM.find_weight(meta["name"], kind="loras"),
                "%s 的权重文件本机不存在：%s" % (key, filename))

    def test_entries_route_to_core_loader(self):
        for key, (filename, loader) in self.NEW_ENTRIES.items():
            self.assertEqual(VM.detect_accel_lora_loader(filename), loader,
                             "%s 的加载节点判定不对" % key)

    def test_new_entries_appear_in_all_accel_keys(self):
        keys = VM.all_accel_lora_keys()
        for key in self.NEW_ENTRIES:
            self.assertIn(key, keys, "%s 不在可选项清单里" % key)

    def test_new_entries_have_author_recommended_params(self):
        """每条都得有步数 / 采样器 / 调度器 —— 否则面板拿不到默认值。"""
        for key in self.NEW_ENTRIES:
            meta = VM.accel_lora_meta(key)
            self.assertTrue(meta.get("recommended_steps"),
                            "%s 缺 recommended_steps" % key)
            self.assertTrue(meta.get("recommended_sampler"),
                            "%s 缺 recommended_sampler" % key)
            self.assertTrue(meta.get("recommended_scheduler"),
                            "%s 缺 recommended_scheduler" % key)


if __name__ == "__main__":
    unittest.main()
