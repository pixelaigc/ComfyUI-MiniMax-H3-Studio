# -*- coding: utf-8 -*-
"""H3 效果 LoRA（文生视频面板）—— 单元测试。

覆盖 5 条主线：
  1. **发现机制**：清单来自真实扫盘，不是硬编码；加速 LoRA 不混进来
  2. **兼容性**：键绑定 + 形状匹配两���判据
  3. **工作流注入**：LoraLoaderModelOnly 节点真的接进链路，strength 真的写进去
  4. **参数映射**：强度 0 = 关闭；多枚 LoRA 串成链；触发词合并进提示词
  5. **服务端闸门**：不存在的文件 / 不兼容的底模 一律拒绝

⚠️ 【本测试不碰 GPU】所有兼容性判定都基于**本机真实权重文件的
safetensors 头**（只读头部，不加载权重本体）—— 与线上跑的是同一套判据。
「strength 在 ComfyUI 的权重运算里真的生效」由
`docs/effect-lora-evidence/prove_strength_effect.py` 用 ComfyUI 自己的
`LoRAAdapter.calculate_weight()` 单独证明（那里需要 ComfyUI 的 venv）。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from h3studio import h3_effect_loras as effect_registry  # noqa: E402
from h3studio import h3_effect_lora_docs as doc_parser  # noqa: E402
from h3studio import video_models as vmodels  # noqa: E402
from h3studio import video_workflow as vw  # noqa: E402


def _has_real_weights() -> bool:
    """本机是否有 H3 底模权重（没有就跳过依赖真实文件的用例）。"""
    model = vmodels.VIDEO_MODELS.get(vmodels.VIDEO_DEFAULT_MODEL)
    if not model:
        return False
    return vmodels.find_weight(model["unet"]) is not None


class TestEffectLoraDiscovery(unittest.TestCase):
    """发现机制 —— 清单必须是真实扫盘的结果。"""

    def test_discovery_returns_list(self):
        items = effect_registry.discover_effect_loras()
        self.assertIsInstance(items, list)

    def test_every_entry_has_real_file_on_disk(self):
        """每一条都必须对应一个**真实存在**的文件（不能是假条目）。"""
        for entry in effect_registry.discover_effect_loras():
            path = Path(entry["path"])
            self.assertTrue(path.is_file(),
                            f"{entry['name']} 的路径不存在：{path}")

    def test_names_are_comfyui_relative_paths(self):
        """name 必须是相对 loras 根目录的路径（ComfyUI 里的 lora_name 写法）。"""
        for entry in effect_registry.discover_effect_loras():
            name = entry["name"]
            self.assertFalse(name.startswith("/"), name)
            self.assertTrue(name.endswith(".safetensors"), name)

    def test_acceleration_loras_excluded(self):
        """加速/蒸馏类 LoRA 不能混进效果列表。

        Turbo / LightX2V 这类是「把 20 步压到 4 步」的蒸馏权重，
        跟固定挂的 Turbo LoRA 叠加会打架，不是效果。
        """
        names = {e["name"] for e in effect_registry.discover_effect_loras()}
        for accel in ("minimax_h3_turbo_4step_ema_ckpt850.safetensors",
                      "dmad_minimax_h3_4step_full_critic_comfyui.safetensors"):
            self.assertNotIn(accel, names,
                             f"加速 LoRA {accel} 不应出现在效果列表里")

    def test_accel_name_pattern_filter(self):
        """按文件名的加速语义词过滤。"""
        self.assertTrue(effect_registry._is_accel_by_name(
            "minimax_h3_turbo_4step_ema_ckpt850.safetensors"))
        self.assertTrue(effect_registry._is_accel_by_name(
            "h3_lightx2v/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors"))
        self.assertTrue(effect_registry._is_accel_by_name(
            "minimax_h3_turbo_4步加速_comfyui.safetensors"))
        # 效果 LoRA 不能被误杀
        self.assertFalse(effect_registry._is_accel_by_name(
            "h3/3dsrx_minimax_h3.safetensors"))
        self.assertFalse(effect_registry._is_accel_by_name(
            "h3/Minimax H3真实电影质感.safetensors"))
        self.assertFalse(effect_registry._is_accel_by_name(
            "h3/manga_tone_v2.safetensors"))

    def test_key_format_detected(self):
        """键名格式要被正确识别（决定能不能用核心节点加载）。"""
        formats = {e["name"]: e["key_format"]
                   for e in effect_registry.discover_effect_loras()}
        self.assertTrue(formats)
        for fmt in formats.values():
            self.assertIn(fmt, ("diffusion_model", "lora_unet", "bare"))


class TestEffectLoraCompat(unittest.TestCase):
    """兼容性 —— 两层判据（键绑定 + 形状）。"""

    @unittest.skipUnless(_has_real_weights(), "本机无 H3 底模权重")
    def test_real_loras_bind_to_h3_base(self):
        """真实效果 LoRA 必须能绑到 H3 底模的权重键上。

        这是「H3 的 LoRA」这个归属判据本身 —— 图像 LoRA（Wan / MOSS /
        Z-Image）绑不上，因为层名完全不重叠。
        """
        model = vmodels.VIDEO_MODELS[vmodels.VIDEO_DEFAULT_MODEL]
        unet = vmodels.find_weight(model["unet"])
        items = effect_registry.discover_effect_loras()
        self.assertTrue(items, "本机应至少有一枚 H3 效果 LoRA")
        for entry in items:
            check = effect_registry.check_effect_lora_compat(
                Path(entry["path"]), unet)
            self.assertTrue(
                check["compatible"],
                f"{entry['name']} 应与 H3 底模兼容，实际：{check['reason']}")
            self.assertEqual(check["shape_mismatch"], 0, entry["name"])
            self.assertEqual(check["missing"], 0, entry["name"])
            self.assertGreater(check["shape_ok"], 0, entry["name"])

    @unittest.skipUnless(_has_real_weights(), "本机无 H3 底模权重")
    def test_image_lora_is_not_h3_lora(self):
        """图像 LoRA 不能被误判成 H3 效果 LoRA（层结构完全不同）。"""
        model = vmodels.VIDEO_MODELS[vmodels.VIDEO_DEFAULT_MODEL]
        unet = vmodels.find_weight(model["unet"])
        image_lora = None
        for root in effect_registry.lora_registry.comfyui_lora_dirs():
            candidate = root / "z-image-effects" / "AWPortrait-Z.safetensors"
            if candidate.is_file():
                image_lora = candidate
                break
        if image_lora is None:
            self.skipTest("本机没有 Z-Image 效果 LoRA 可作对照")
        check = effect_registry.check_effect_lora_compat(image_lora, unet)
        self.assertFalse(check["compatible"])

    def test_missing_file_reports_reason(self):
        check = effect_registry.check_effect_lora_compat(
            Path("/nonexistent/nope.safetensors"),
            Path("/nonexistent/base.safetensors"))
        self.assertFalse(check["compatible"])
        self.assertIn("找不到", check["reason"])


class TestEffectLoraWorkflowInjection(unittest.TestCase):
    """工作流注入 —— 节点真的接进链路，strength 真的写进去。"""

    def _params(self, effect_loras):
        return {
            "job_id": "unit-test",
            "model_key": vmodels.VIDEO_DEFAULT_MODEL,
            "width": 768, "height": 1344, "duration": 5, "seed": 1,
            "prompt": "a cat walking",
            "fixed_accel_lora": "ckpt850",
            "effect_loras": effect_loras,
        }

    def _real_effect_name(self):
        """挑一枚本机真实存在的效果 LoRA（给工作流用例用）。"""
        items = effect_registry.discover_effect_loras()
        return items[0]["name"] if items else None

    def test_no_effect_lora_means_no_loader_node(self):
        """不选效果 LoRA 时**不能**凭空多出节点（不改变原有行为）。"""
        p = vw.build_t2v_panel(self._params([]))
        loaders = [n for n in p.values()
                   if n.get("class_type") == "LoraLoaderModelOnly"]
        self.assertEqual(loaders, [])

    def test_effect_lora_creates_loader_node(self):
        name = self._real_effect_name()
        if not name:
            self.skipTest("本机没有真实效果 LoRA")
        p = vw.build_t2v_panel(self._params([{"name": name, "strength": 0.8}]))
        loaders = [(nid, n) for nid, n in p.items()
                   if n.get("class_type") == "LoraLoaderModelOnly"]
        self.assertEqual(len(loaders), 1)
        nid, node = loaders[0]
        self.assertEqual(node["inputs"]["lora_name"], name)
        self.assertEqual(node["inputs"]["strength_model"], 0.8)

    def test_multiple_effect_loras_chain_in_order(self):
        items = effect_registry.discover_effect_loras()
        if len(items) < 2:
            self.skipTest("本机效果 LoRA 少于 2 枚")
        first, second = items[0], items[1]
        p = vw.build_t2v_panel(self._params([
            {"name": first["name"], "strength": 0.6},
            {"name": second["name"], "strength": 0.9},
        ]))
        loaders = sorted(
            [(nid, n) for nid, n in p.items()
             if n.get("class_type") == "LoraLoaderModelOnly"],
            key=lambda kv: kv[0])
        self.assertEqual(len(loaders), 2)
        # 第二枚（320:1）的 model 输入 = 第一枚（320:0）的输出 -> 串成链
        self.assertEqual(loaders[1][1]["inputs"]["model"], [loaders[0][0], 0])
        self.assertEqual(loaders[0][1]["inputs"]["strength_model"], 0.6)
        self.assertEqual(loaders[1][1]["inputs"]["strength_model"], 0.9)

    def test_strength_zero_disables_lora(self):
        """强度 0 = 关闭（不产生节点）。"""
        name = self._real_effect_name()
        if not name:
            self.skipTest("本机没有真实效果 LoRA")
        p = vw.build_t2v_panel(self._params([{"name": name, "strength": 0}]))
        loaders = [n for n in p.values()
                   if n.get("class_type") == "LoraLoaderModelOnly"]
        self.assertEqual(loaders, [])

    def test_effect_lora_sits_after_accel_lora(self):
        """效果 LoRA 在加速 LoRA **之后**（先改采样语义，再叠风格）。"""
        name = self._real_effect_name()
        if not name:
            self.skipTest("本机没有真实效果 LoRA")
        p = vw.build_t2v_panel(self._params([{"name": name, "strength": 0.8}]))
        loader = next(n for n in p.values()
                      if n.get("class_type") == "LoraLoaderModelOnly")
        upstream = loader["inputs"]["model"]
        self.assertEqual(len(upstream), 2)
        upstream_node = p[upstream[0]]
        self.assertEqual(upstream_node["class_type"], "MiniMaxH3TurboLoRA")

    def test_effect_lora_chain_reaches_sampler(self):
        """效果 LoRA 必须真的在**推理链路**上（不是悬空节点）。"""
        name = self._real_effect_name()
        if not name:
            self.skipTest("本机没有真实效果 LoRA")
        p = vw.build_t2v_panel(self._params([{"name": name, "strength": 0.8}]))
        # 从采样器往回走，必须能走到效果 LoRA 节点
        cur = p["9"]["inputs"]["model"]
        seen = []
        while True:
            nid, _idx = cur
            node = p[nid]
            seen.append(node["class_type"])
            nxt = node.get("inputs", {}).get("model")
            if not nxt:
                break
            cur = nxt
        self.assertIn("LoraLoaderModelOnly", seen)
        self.assertIn("MiniMaxH3TurboLoRA", seen)
        self.assertEqual(seen[-1], "UNETLoader")

    def test_missing_file_raises(self):
        """不存在的文件必须报错，不能静默忽略。"""
        with self.assertRaises(ValueError):
            vw.build_t2v_panel(self._params([
                {"name": "h3/definitely-not-here.safetensors", "strength": 1.0}]))

    def test_trigger_merged_into_prompt(self):
        """触发词自动追加到提示词末尾（与图像面板同一套规则）。"""
        name = self._real_effect_name()
        if not name:
            self.skipTest("本机没有真实效果 LoRA")
        p = vw.build_t2v_panel(self._params([
            {"name": name, "strength": 0.8, "trigger": "test_trigger_word"}]))
        prompt = p["105:104"]["inputs"]["prompt"]
        self.assertIn("test_trigger_word", prompt)
        self.assertIn("a cat walking", prompt)

    def test_trigger_not_duplicated(self):
        """提示词里已有的触发词不重复追加。"""
        name = self._real_effect_name()
        if not name:
            self.skipTest("本机没有真实效果 LoRA")
        params = self._params([
            {"name": name, "strength": 0.8, "trigger": "already_here"}])
        params["prompt"] = "a cat walking, already_here"
        p = vw.build_t2v_panel(params)
        prompt = p["105:104"]["inputs"]["prompt"]
        self.assertEqual(prompt.count("already_here"), 1)

    def test_disabled_effect_lora_skipped(self):
        """enabled=false 的项不生成节点。"""
        name = self._real_effect_name()
        if not name:
            self.skipTest("本机没有真实效果 LoRA")
        p = vw.build_t2v_panel(self._params([
            {"name": name, "strength": 0.8, "enabled": False}]))
        loaders = [n for n in p.values()
                   if n.get("class_type") == "LoraLoaderModelOnly"]
        self.assertEqual(loaders, [])


class TestEffectLoraServerGate(unittest.TestCase):
    """服务端闸门 —— 绕过前端直接调 API 也要拦住脏值。

    【闸门在哪】`normalize_t2v_params()` 是文生视频**唯一**的参数入口
    （server.py:2848，在 prepare_job_params **之前**跑）。
    它返回 `(归一后的params, 错误原因)`；错误原因非空 = 提交被拒。
    """

    def _normalize(self, effect_loras):
        import server
        params = {
            "job_id": "unit-test",
            "model_key": vmodels.VIDEO_DEFAULT_MODEL,
            "width": 768, "height": 1344, "duration": 5,
            "prompt": "x", "seed": 1, "fixed_accel_lora": "ckpt850",
            "effect_loras": effect_loras,
        }
        return server.normalize_t2v_params(params)

    def test_normalize_accepts_real_effect_lora(self):
        items = effect_registry.discover_effect_loras()
        if not items:
            self.skipTest("本机没有真实效果 LoRA")
        out, error = self._normalize([
            {"name": items[0]["name"], "strength": 0.8}])
        self.assertEqual(error, "")
        self.assertEqual(len(out["effect_loras"]), 1)
        self.assertEqual(out["effect_loras"][0]["strength"], 0.8)

    def test_normalize_rejects_missing_file(self):
        out, error = self._normalize([
            {"name": "h3/nope-not-here.safetensors", "strength": 1.0}])
        self.assertNotEqual(error, "", "不存在的文件必须被拒绝")
        self.assertIn("找不到", error)

    def test_normalize_rejects_non_numeric_strength(self):
        items = effect_registry.discover_effect_loras()
        if not items:
            self.skipTest("本机没有真实效果 LoRA")
        out, error = self._normalize([
            {"name": items[0]["name"], "strength": "abc"}])
        self.assertNotEqual(error, "", "非数字强度必须被拒绝")

    def test_normalize_clamps_strength_to_max(self):
        items = effect_registry.discover_effect_loras()
        if not items:
            self.skipTest("本机没有真实效果 LoRA")
        out, error = self._normalize([
            {"name": items[0]["name"], "strength": 99.0}])
        self.assertEqual(error, "")
        self.assertLessEqual(out["effect_loras"][0]["strength"], 2.0)

    def test_normalize_drops_zero_strength(self):
        items = effect_registry.discover_effect_loras()
        if not items:
            self.skipTest("本机没有真实效果 LoRA")
        out, error = self._normalize([
            {"name": items[0]["name"], "strength": 0.0}])
        self.assertEqual(error, "")
        self.assertEqual(out["effect_loras"], [])

    def test_normalize_drops_disabled(self):
        items = effect_registry.discover_effect_loras()
        if not items:
            self.skipTest("本机没有真实效果 LoRA")
        out, error = self._normalize([
            {"name": items[0]["name"], "strength": 0.8, "enabled": False}])
        self.assertEqual(error, "")
        self.assertEqual(out["effect_loras"], [])

    def test_job_meta_shows_effect_lora_labels(self):
        """任务卡片上要显示用了哪几枚效果 LoRA + 强度。"""
        from h3studio import job_meta
        items = effect_registry.discover_effect_loras()
        if not items:
            self.skipTest("本机没有真实效果 LoRA")
        result = job_meta.resolve_job_lora("t2v", {
            "model_key": vmodels.VIDEO_DEFAULT_MODEL,
            "fixed_accel_lora": "ckpt850",
            "effect_loras": [{"name": items[0]["name"], "strength": 0.8,
                              "trigger": "t"}],
        })
        self.assertTrue(result["effect_loras"])
        self.assertEqual(result["effect_loras"][0]["strength"], 0.8)
        self.assertTrue(any("效果" in label for label in result["labels"]))


class TestEffectLoraDocParsing(unittest.TestCase):
    """作者文档解析 —— 触发词/推荐强度不能臆造。"""

    def test_missing_doc_returns_empty(self):
        doc = doc_parser.read_lora_doc(Path("/nonexistent/x.safetensors"))
        self.assertFalse(doc["has_doc"])
        self.assertEqual(doc["trigger"], "")
        self.assertEqual(doc["recommended_strength"], 0.0)

    def test_prose_is_not_mistaken_for_trigger(self):
        """「作者未提供触发词」是**负向结论** -> 触发词必须留空。"""
        section = "作者**未提供触发词**（trainedWords 为空）—— 不需要。"
        self.assertEqual(doc_parser._parse_trigger(section), "")

    def test_code_block_trigger(self):
        section = "```\n3dsrx\n```"
        self.assertEqual(doc_parser._parse_trigger(section), "3dsrx")

    def test_table_row_trigger(self):
        section = "| **触发词** | **`m1sa_b0dy`** |"
        self.assertEqual(doc_parser._parse_trigger(section), "m1sa_b0dy")

    def test_strength_range_takes_midpoint(self):
        section = "| 场景 | 建议强度 | 原因 |\n|---|---|---|\n| 常规 | 0.6~0.8 | ... |"
        strength, _basis = doc_parser._parse_strength(section)
        self.assertAlmostEqual(strength, 0.7, places=3)

    def test_percentage_is_not_a_strength(self):
        """正文里的百分比是扰动比例，不是强度。"""
        section = "最大 1.540%，1.0 是安全上限附近。"
        strength, _basis = doc_parser._parse_strength(section)
        self.assertAlmostEqual(strength, 1.0, places=3)

    def test_dimension_number_is_not_a_strength(self):
        """`2D 动漫` 里的 2 不是强度。"""
        section = "一个旋钮控制 2D 动漫到写实。"
        strength, _basis = doc_parser._parse_strength(section)
        self.assertEqual(strength, 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
