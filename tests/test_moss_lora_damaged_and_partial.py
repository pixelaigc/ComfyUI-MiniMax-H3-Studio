# -*- coding: utf-8 -*-
"""MOSS-Image LoRA 兼容性判定的回归测试（2026-11-15 修复轮）。

覆盖两处被独立验收判为「仍以 compatible=true 下发」的缺陷：

D1  moss-effects/moss_image_sft_look_lora.safetensors
    实测 patched=137 / missing=126 / attention_hit=0.75
    —— 根因：`_lora_unet_to_dots` 里 `parts[0] == "text_embedder"`
    是**死分支**（body 用下划线切分，parts[0] 永远是 'text'），
    导致带文本塔键的 LoRA 无法归一 -> 融合 qkv 不拆分 -> 只挂一半层。

D2  moss-effects/hina_MOSSImageBase_animeEnhance_v1.0-lora.safetensors
    SafetensorError: incomplete metadata
    —— 根因：文件被**截断**（5,369,856 / 297,355,184 字节），
    但 safetensors **头部**能读出来 -> 旧逻辑「头部能读 = 正常」
    -> compatible=true。

本文件只测**判定逻辑**（用构造出来的最小 safetensors 夹具），
不依赖本机真的装了这些权重文件 —— 装了的话额外做真文件回归。
"""
from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path

from h3studio import loras as lora_registry
from h3studio import moss_lora_compat as mlc


def write_minimal_safetensors(path: Path, tensors: dict, *,
                              truncate_to: int | None = None) -> Path:
    """写一个最小可用的 safetensors 文件。

    tensors: {name: (shape, dtype_str)}
    truncate_to: 写到第几字节就停（模拟下载中断 / 拷贝未完成）

    ⚠️ truncate_to 的语义是「**保留头部、只截断数据区**」——
    这正是 D2 的真实损坏形态（头部完整，但权重数据没下完）。
    如果连头部都截断，读头会直接抛 JSONDecodeError，
    测的就不是「头部能读但文件损坏」这条路径了。
    """
    header = {}
    offset = 0
    blob = bytearray()
    for name, (shape, dtype) in tensors.items():
        n = 1
        for d in shape:
            n *= d
        width = {"F16": 2, "F32": 4, "BF16": 2}[dtype]
        size = n * width
        header[name] = {"dtype": dtype, "shape": list(shape),
                        "data_offsets": [offset, offset + size]}
        blob.extend(b"\x00" * size)
        offset += size
    raw = json.dumps(header).encode("utf-8")
    prefix = struct.pack("<Q", len(raw))
    path.parent.mkdir(parents=True, exist_ok=True)
    if truncate_to is not None:
        # 保留前缀 + 完整头部，只截断数据区
        keep = len(prefix) + len(raw) + max(0, truncate_to)
        path.write_bytes(prefix + raw + bytes(blob)[:max(0, truncate_to)])
        del keep
    else:
        path.write_bytes(prefix + raw + bytes(blob))
    return path


def fused_qkv_tensors(layers: int = 1, dim: int = 8, rank: int = 4):
    """构造一个「融合 qkv」形态的 LoRA 张量字典（lora_unet_ 前缀）。"""
    out = {}
    for i in range(layers):
        stem = f"lora_unet_layers_{i}_proj_qkv"
        out[f"{stem}.alpha"] = ((), "F32")
        out[f"{stem}.lora_down.weight"] = ((rank, dim), "F16")
        # 融合 qkv 的行映射载体：第 0 维 = 3 * dim（可被 3 整除 -> 可无损拆）
        out[f"{stem}.lora_up.weight"] = ((3 * dim, rank), "F16")
    return out


def text_embedder_tensors(blocks: int = 1):
    """构造「文本塔」形态的键（D1 的关键形态）。"""
    out = {}
    for i in range(blocks):
        stem = f"lora_unet_text_embedder_blocks_{i}"
        out[f"{stem}_ffn_down.alpha"] = ((), "F32")
        out[f"{stem}_ffn_down.lora_down.weight"] = ((4, 8), "F16")
        out[f"{stem}_ffn_down.lora_up.weight"] = ((8, 4), "F16")
        out[f"{stem}_ffn_gate.alpha"] = ((), "F32")
        out[f"{stem}_ffn_gate.lora_down.weight"] = ((4, 8), "F16")
        out[f"{stem}_ffn_gate.lora_up.weight"] = ((8, 4), "F16")
        out[f"{stem}_ffn_up.alpha"] = ((), "F32")
        out[f"{stem}_ffn_up.lora_down.weight"] = ((4, 8), "F16")
        out[f"{stem}_ffn_up.lora_up.weight"] = ((8, 4), "F16")
        out[f"{stem}_proj.alpha"] = ((), "F32")
        out[f"{stem}_proj.lora_down.weight"] = ((4, 8), "F16")
        out[f"{stem}_proj.lora_up.weight"] = ((8, 4), "F16")
        out[f"{stem}_qkv.alpha"] = ((), "F32")
        out[f"{stem}_qkv.lora_down.weight"] = ((4, 8), "F16")
        out[f"{stem}_qkv.lora_up.weight"] = ((8, 4), "F16")
    return out


class TestTextEmbedderNormalizationRegression(unittest.TestCase):
    """D1 根因：文本塔键的归一分支曾是**死代码**。"""

    def test_lora_unet_text_embedder_is_not_dead_branch(self):
        """旧代码 `parts[0] == "text_embedder"` 永远不成立。

        body 是用**下划线**切分的，'text_embedder_blocks_0_ffn_down'
        -> ['text','embedder','blocks','0','ffn','down']，
        所以 parts[0] 永远是 'text'。
        """
        self.assertNotEqual(
            "lora_unet_text_embedder_blocks_0_ffn_down".split("_")[0],
            "text_embedder",
            "本测试的前提是 parts[0] 永远不等于 'text_embedder'（死分支）")

    def test_text_embedder_modules_are_normalized(self):
        """文本塔的键必须能归一（否则整个 LoRA 放弃转换）。"""
        cases = {
            "lora_unet_text_embedder_blocks_0_ffn_down":
                "text_embedder.blocks.0.ffn_down",
            "lora_unet_text_embedder_blocks_0_ffn_gate":
                "text_embedder.blocks.0.ffn_gate",
            "lora_unet_text_embedder_blocks_0_ffn_up":
                "text_embedder.blocks.0.ffn_up",
            "lora_unet_text_embedder_blocks_0_proj":
                "text_embedder.blocks.0.proj",
            "lora_unet_text_embedder_blocks_0_qkv":
                "text_embedder.blocks.0.qkv",
            "lora_unet_text_embedder_blocks_1_k_norm":
                "text_embedder.blocks.1.k_norm",
            "lora_unet_text_embedder_blocks_0_norm1":
                "text_embedder.blocks.0.norm1",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(mlc._lora_unet_to_dots(raw), expected)

    def test_text_embedder_normalize_key_prefixes_diffusion_model(self):
        norm = mlc.normalize_key(
            "lora_unet_text_embedder_blocks_0_ffn_down.lora_down.weight")
        self.assertEqual(
            norm, "diffusion_model.text_embedder.blocks.0.ffn_down.lora_down.weight")

    def test_unknown_text_embedder_shape_still_returns_none(self):
        """无法无歧义还原的形态仍然要返回 None（不猜）。"""
        self.assertIsNone(
            mlc._lora_unet_to_dots("lora_unet_text_embedder_blocks_0_bogus_xyz"))


class TestFusedQkvHitRatio(unittest.TestCase):
    """D1 的第二处：命中率必须按「**转换后**的真实结果」算。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_fused_qkv_counts_as_hit_when_splittable(self):
        """可无损拆分的融合 qkv 必须计入 hit（拆开后必然命中）。"""
        path = write_minimal_safetensors(
            self.dir / "fused.safetensors", fused_qkv_tensors(layers=2))
        header = mlc.read_safetensors_header(path)
        self.assertTrue(header, "夹具应该能读出头")
        self.assertEqual(mlc.detect_qkv_shape(header), mlc.QKV_SHAPE_FUSED)
        # 拆分可行性由 normalize_and_split 独立验证（不读权重本体，
        # 这里用形状正确的假张量）
        import torch
        fake = {}
        for key, meta in header.items():
            if key == "__metadata__":
                continue
            shape = tuple(meta.get("shape", ()))
            fake[key] = (torch.zeros(shape, dtype=torch.float16) if shape
                         else torch.zeros((), dtype=torch.float32))
        converted, unhandled = mlc.normalize_and_split(fake)
        self.assertEqual(unhandled, [], "融合 qkv 应该能无损拆分")
        self.assertTrue(any(".proj.q." in k for k in converted),
                        "拆分后应该出现分离 q 键")
        self.assertTrue(any(".proj.k." in k for k in converted))
        self.assertTrue(any(".proj.v." in k for k in converted))

    def test_unsplittable_row_dimension_is_detected(self):
        """行映射第 0 维不能被 3 整除 -> 拆分不可行 -> 必须报出来。"""
        tensors = fused_qkv_tensors(layers=1)
        # 把行映射的第 0 维改成不能被 3 整除的数
        tensors["lora_unet_layers_0_proj_qkv.lora_up.weight"] = ((10, 4), "F16")
        path = write_minimal_safetensors(self.dir / "bad.safetensors", tensors)
        import torch
        header = mlc.read_safetensors_header(path)
        fake = {}
        for key, meta in header.items():
            if key == "__metadata__":
                continue
            shape = tuple(meta.get("shape", ()))
            fake[key] = (torch.zeros(shape, dtype=torch.float16) if shape
                         else torch.zeros((), dtype=torch.float32))
        converted, unhandled = mlc.normalize_and_split(fake)
        self.assertTrue(unhandled, "不能被 3 整除的行映射必须报为无法处理")


class TestDamagedFileNeverCompatible(unittest.TestCase):
    """D2 根因：文件损坏 / 被截断绝不能以 compatible=true 下发。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_truncated_file_header_is_rejected(self):
        """截断文件 -> 头部读取必须返回空（旧逻辑会返回完整头部）。"""
        tensors = fused_qkv_tensors(layers=2)
        # 只截断数据区，头部保持完整（D2 的真实形态）
        path = write_minimal_safetensors(self.dir / "trunc.safetensors", tensors,
                                         truncate_to=8)
        self.assertEqual(mlc.read_safetensors_header(path), {},
                         "截断文件不能被当成能读出头")
        detail = mlc.read_safetensors_header_detail(path)
        self.assertFalse(detail["ok"])
        self.assertIn("截断", detail["reason"])

    def test_truncated_file_detail_reports_missing_bytes(self):
        tensors = fused_qkv_tensors(layers=2)
        path = write_minimal_safetensors(self.dir / "trunc2.safetensors", tensors,
                                         truncate_to=8)
        detail = mlc.read_safetensors_header_detail(path)
        self.assertFalse(detail["ok"])
        self.assertIn("缺", detail["reason"])
        self.assertIn("字节", detail["reason"])

    def test_complete_file_passes_detail_check(self):
        path = write_minimal_safetensors(
            self.dir / "ok.safetensors", fused_qkv_tensors(layers=1))
        detail = mlc.read_safetensors_header_detail(path)
        self.assertTrue(detail["ok"], detail["reason"])
        self.assertTrue(mlc.read_safetensors_header(path))

    def test_garbage_file_is_rejected(self):
        path = self.dir / "garbage.safetensors"
        path.write_bytes(b"not a safetensors file at all")
        self.assertEqual(mlc.read_safetensors_header(path), {})
        detail = mlc.read_safetensors_header_detail(path)
        self.assertFalse(detail["ok"])


class TestCheckMossCompatibilityFailsClosed(unittest.TestCase):
    """check_moss_compatibility 必须在读不出完整文件时 fail closed。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._orig_keys = mlc._MOSS_KEYS_CACHE
        # 注入一个假的底模键集合（不依赖本机真的装了 MOSS 底模）
        mlc._MOSS_KEYS_CACHE = {
            "diffusion_model.layers.0.proj.q",
            "diffusion_model.layers.0.proj.k",
            "diffusion_model.layers.0.proj.v",
            "diffusion_model.layers.0.proj.o",
        }

    def tearDown(self):
        mlc._MOSS_KEYS_CACHE = self._orig_keys
        self._tmp.cleanup()

    def test_truncated_file_is_incompatible_with_reason(self):
        path = write_minimal_safetensors(
            self.dir / "trunc.safetensors", fused_qkv_tensors(layers=1),
            truncate_to=8)
        result = mlc.check_moss_compatibility(path)
        self.assertFalse(result["compatible"],
                         "截断文件绝不能判成 compatible=true")
        self.assertTrue(result["reason"], "必须给出可读的原因")
        self.assertIn("截断", result["reason"])

    def test_unreadable_file_is_incompatible(self):
        path = self.dir / "garbage.safetensors"
        path.write_bytes(b"garbage")
        result = mlc.check_moss_compatibility(path)
        self.assertFalse(result["compatible"])
        self.assertTrue(result["reason"])

    def test_compatible_file_stays_compatible(self):
        """回归：正常的（可转换的）LoRA 不能被误杀。"""
        path = write_minimal_safetensors(
            self.dir / "ok.safetensors", fused_qkv_tensors(layers=1))
        result = mlc.check_moss_compatibility(path)
        self.assertTrue(result["compatible"], result["reason"])
        self.assertEqual(result["reason"], "")


class TestOtherFamiliesUnaffected(unittest.TestCase):
    """Z-Image / Qwen-Image 族不能被本次改动带偏。"""

    def test_generic_safetensors_header_reader_still_works(self):
        """通用族用的 _read_safetensors_header 不受影响。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = write_minimal_safetensors(
                Path(tmp) / "x.safetensors", fused_qkv_tensors(layers=1))
            header = lora_registry._read_safetensors_header(path)
            self.assertTrue(header)

    def test_moss_family_detection_unaffected(self):
        self.assertFalse(lora_registry.is_moss_family_lora(""))
        self.assertFalse(lora_registry.is_moss_family_lora("nonexistent.safetensors"))


class TestDerivedFileInheritsCatalogInfo(unittest.TestCase):
    """派生文件（归一版）必须继承原文件的目录元数据。

    派生文件跟原文件是**同一个 LoRA**，但 LORA_CATALOG 只登记原文件
    —— 查不到条目 -> display / scene / trigger / 推荐强度全丢
    -> 界面显示空白条目（实测 test_effect_entries_carry_trigger_and_description
    报「缺场景说明」）。
    """

    def test_derived_name_is_inherited(self):
        from h3studio.moss_lora_split import DERIVED_PREFIX, derived_name
        source = "moss-effects/Cinematic_MossSFT_c1-st6000.safetensors"
        derived = derived_name("Cinematic_MossSFT_c1-st6000.safetensors")
        self.assertTrue(derived.startswith(DERIVED_PREFIX))
        info = lora_registry._inherit_catalog_info(
            "moss-effects/" + derived)
        self.assertTrue(info, "派生文件必须能继承到原文件的目录条目")
        self.assertTrue(info.get("display"), info)

    def test_non_derived_file_returns_empty(self):
        self.assertEqual(
            lora_registry._inherit_catalog_info(
                "moss-effects/Cinematic_MossSFT_c1-st6000.safetensors"),
            {})

    def test_unknown_derived_file_returns_empty(self):
        self.assertEqual(
            lora_registry._inherit_catalog_info(
                "moss-effects/h3studio_mossnorm__does_not_exist.safetensors"),
            {})

    def test_derived_entries_in_list_have_scene(self):
        """真实清单里的派生条目必须有说明（界面要显示）。"""
        for item in lora_registry.list_moss_effect_loras():
            if "mossnorm" not in item["name"]:
                continue
            self.assertTrue(item.get("display"),
                            f"{item['name']} 缺显示名")
            self.assertTrue(item.get("scene"),
                            f"{item['name']} 缺场景说明（界面要显示）")


class TestRealMachineFiles(unittest.TestCase):
    """本机真的装了这两个文件时，做真文件回归（没装就跳过）。"""

    D1 = "moss-effects/moss_image_sft_look_lora.safetensors"
    D2 = "moss-effects/hina_MOSSImageBase_animeEnhance_v1.0-lora.safetensors"

    def _resolve(self, rel):
        resolved, path = lora_registry.resolve_lora(rel)
        return path

    def test_d1_is_fully_attachable_after_fix(self):
        """D1：转换后必须能完整挂上（missing == 0）。"""
        path = self._resolve(self.D1)
        if not path:
            self.skipTest("本机没有该 LoRA")
        from h3studio import moss_lora_split as split
        with tempfile.TemporaryDirectory() as tmp:
            result = split.normalize_lora(path, target_dir=Path(tmp))
            self.assertTrue(result.get("ok"), result.get("reason"))
            self.assertTrue(result.get("changed"),
                            "D1 需要转换（融合 qkv + 文本塔键）")
            target = result.get("target")
            self.assertTrue(target and Path(target).is_file())
            header = mlc.read_safetensors_header(target)
            self.assertTrue(header, "派生文件必须能读出头")
            self.assertNotEqual(mlc.detect_qkv_shape(header),
                                mlc.QKV_SHAPE_FUSED,
                                "派生文件必须是分离形态")

    def test_d2_is_not_compatible(self):
        """D2：文件损坏 -> 必须判不兼容。"""
        path = self._resolve(self.D2)
        if not path:
            self.skipTest("本机没有该 LoRA")
        result = mlc.check_moss_compatibility(path)
        self.assertFalse(result["compatible"],
                         "损坏的权重文件绝不能以 compatible=true 下发")
        self.assertTrue(result["reason"])

    def test_d2_not_in_moss_available_list(self):
        """D2 不得出现在用户可选列表里。"""
        path = self._resolve(self.D2)
        if not path:
            self.skipTest("本机没有该 LoRA")
        available = lora_registry.list_moss_effect_loras()
        names = {item["name"] for item in available}
        self.assertNotIn(self.D2, names,
                         "损坏的 LoRA 不得出现在可选列表里")

    def test_moss_available_list_has_no_compatible_true_damaged_entry(self):
        """通用不变量：可选列表里不允许「损坏文件」被当成兼容。"""
        for item in lora_registry.list_moss_effect_loras():
            path = lora_registry.resolve_lora(item["name"])[1]
            if not path:
                continue
            detail = mlc.read_safetensors_header_detail(path)
            if not detail["ok"]:
                self.fail(
                    f"{item['name']} 读不出完整文件却仍以 compatible=true 下发："
                    f"{detail['reason']}")


if __name__ == "__main__":
    unittest.main()
