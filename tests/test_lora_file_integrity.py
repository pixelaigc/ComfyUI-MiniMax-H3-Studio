# -*- coding: utf-8 -*-
"""**跨族** LoRA 权重文件完整性守门测试（2026-11-15 收口轮）。

## 为什么需要这个文件

2026-11-15 的独立验收（c8）判出两条「MOSS LoRA 仍以 compatible=true 下发」
的残缺缺陷，已由并发任务在 `local/feature/moss-lora-fix-20261114` 修掉并上线
（见 `h3studio/moss_lora_compat.py` 的 `read_safetensors_header_detail`）。

**但那个修复只覆盖了 MOSS 一族。** 三族的 LoRA 清单共用
`h3studio/loras.py` 的通用读取器 `_read_safetensors_header()`，
而它当时**只读头部、不校验完整性** —— 与 MOSS 侧修复前的行为一样。

safetensors 的布局是「8 字节长度 + JSON 头 + 数据区」：
**头部本身完整不代表权重完整**。下载中断 / 拷贝未完成会留下
「头完好、数据区被截断」的文件，旧读取器照样读出完整头部 -> 判成好文件。

本机实测的损坏文件（2026-11-15 扫描 523 个 safetensors）：

| 文件 | 实际字节 | 头部声明需要 | 缺失 |
|---|---|---|---|
| `moss-effects/hina_MOSSImageBase_animeEnhance_v1.0-lora.safetensors` | 5,369,856 | 297,355,184 | 291,985,328 |
| `ipadapter/ip-adapter-faceid-plus_sd15_lora.safetensors` | 50,818,772 | 51,059,544 | 240,772 |
| `AWPortrait-QW/AWPortrait-QW.safetensors` | 0 | — | 空文件 |
| `QIE-SRD/Qwen-Image-Edit-Semi-Realistic-Detailer-v1.safetensors` | 0 | — | 空文件 |

MOSS 那条已被上一轮修掉；**后三条（Z-Image / Qwen / 通用路径）没人管**。

## 本文件锁死的不变量

**「权重文件本体不可用的条目，一律不得以 compatible=true 下发」**
—— 跨三族、跨通用读取器路径。

## 变异验证（改坏任一条都会红）

- 去掉 `_read_safetensors_header` 的完整性校验 -> `TestGenericHeaderIntegrity`
  与 `TestTruncatedFileFailsClosed` 变红
- 去掉 `check_compatibility` 的 fail-closed 分支 -> `TestTruncatedFileFailsClosed`
  变红
- 去掉「available 列表里不允许 compatible=false」的断言 ->
  `TestNoDamagedEntryIsSurfacedAsCompatible` 变红

运行：python3 -m pytest tests/test_lora_file_integrity.py -v
需要：真实安装的 LoRA 目录（用于真文件回归；缺失时相关用例 skip，不算失败）。
"""
from __future__ import annotations

import json
import os
import struct
import tempfile
import unittest
from pathlib import Path

from h3studio import loras as lora_registry
from h3studio import moss_lora_compat as mlc

REPO = Path(__file__).resolve().parent.parent

#: 三个基座族（与 loras.LORA_BASES 的键一致）
FAMILIES = ("Z-Image", "Qwen-Image 2.1", "MOSS-Image")

#: 本机实测损坏的权重文件（相对 LoRA 目录的路径）
DAMAGED_SAMPLES = (
    "moss-effects/hina_MOSSImageBase_animeEnhance_v1.0-lora.safetensors",
    "ipadapter/ip-adapter-faceid-plus_sd15_lora.safetensors",
    "AWPortrait-QW/AWPortrait-QW.safetensors",
    "QIE-SRD/Qwen-Image-Edit-Semi-Realistic-Detailer-v1.safetensors",
)


def write_minimal_safetensors(path: Path, tensors: dict, *,
                              truncate_to: int | None = None) -> Path:
    """写一个最小可用的 safetensors 文件。

    truncate_to 的语义是「**保留头部、只截断数据区**」——
    这正是下载中断 / 拷贝未完成留下的真实损坏形态
    （头部完整，但权重数据没下完）。如果连头部都截断，
    读头会直接抛 JSONDecodeError，测的就不是这条路径了。
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
        path.write_bytes(prefix + raw + bytes(blob)[:max(0, truncate_to)])
    else:
        path.write_bytes(prefix + raw + bytes(blob))
    return path


def z_image_like_tensors(layers: int = 1, dim: int = 8, rank: int = 4):
    """构造一个 Z-Image 形态的 LoRA 张量字典（分层 + 融合 qkv + context_refiner）。"""
    out = {}
    for i in range(layers):
        for stem in (f"diffusion_model.layers.{i}.attention.qkv",
                     f"diffusion_model.context_refiner.{i}.attention.qkv",
                     f"diffusion_model.noise_refiner.{i}.attention.qkv"):
            out[f"{stem}.lora_down.weight"] = ((rank, dim), "F16")
            out[f"{stem}.lora_up.weight"] = ((3 * dim, rank), "F16")
    return out


def _lora_dirs() -> list[Path]:
    return [Path(d) for d in lora_registry.comfyui_lora_dirs()]


def _find_damaged(rel: str) -> Path | None:
    for d in _lora_dirs():
        p = d / rel
        if p.is_file():
            return p
    return None


class TestGenericHeaderIntegrity(unittest.TestCase):
    """通用读取器（Z-Image / Qwen / 通用路径）必须做完整性校验。"""

    def test_truncated_file_header_is_rejected(self):
        """「头完好、数据区被截断」的文件 -> 通用读取器必须拒绝。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = write_minimal_safetensors(
                Path(tmp) / "trunc.safetensors",
                z_image_like_tensors(), truncate_to=16)
            self.assertEqual(
                lora_registry._read_safetensors_header(path), {},
                "被截断的权重文件必须读不出头部（头完好不代表文件完整）")

    def test_complete_file_header_is_accepted(self):
        """完好文件必须照常读出头（完整性校验不能误杀好文件）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = write_minimal_safetensors(
                Path(tmp) / "good.safetensors", z_image_like_tensors())
            header = lora_registry._read_safetensors_header(path)
            self.assertTrue(header, "完好文件必须能读出头")
            self.assertIn("diffusion_model.layers.0.attention.qkv.lora_down.weight",
                          header)

    def test_empty_file_header_is_rejected(self):
        """0 字节空文件 -> 读不出头。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.safetensors"
            path.write_bytes(b"")
            self.assertEqual(lora_registry._read_safetensors_header(path), {})

    def test_generic_reader_and_moss_reader_agree(self):
        """通用读取器必须与 MOSS 侧那份**口径一致**（不能两份实现漂移）。"""
        with tempfile.TemporaryDirectory() as tmp:
            trunc = write_minimal_safetensors(
                Path(tmp) / "trunc.safetensors",
                z_image_like_tensors(), truncate_to=16)
            good = write_minimal_safetensors(
                Path(tmp) / "good.safetensors", z_image_like_tensors())
            for path in (trunc, good):
                self.assertEqual(
                    bool(lora_registry._read_safetensors_header(path)),
                    bool(mlc.read_safetensors_header(path)),
                    f"{path.name}: 通用读取器与 MOSS 读取器结论不一致")


class TestTruncatedFileFailsClosed(unittest.TestCase):
    """check_compatibility 必须 **fail closed**。"""

    def _z_unet(self):
        unet = lora_registry._z_image_unet_path()
        if unet is None:
            self.skipTest("本机没有 Z-Image 底模")
        return unet

    def test_truncated_lora_is_incompatible_with_reason(self):
        """被截断的 LoRA -> compatible=false + 如实原因（不能 compatible=true）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = write_minimal_safetensors(
                Path(tmp) / "trunc.safetensors",
                z_image_like_tensors(), truncate_to=16)
            result = lora_registry.check_compatibility(
                path, self._z_unet(), family="Z-Image")
            self.assertFalse(
                result.get("compatible", True),
                "被截断的权重文件不得判为 compatible=true")
            self.assertTrue(result.get("reason"),
                            "必须给出不兼容原因")
            self.assertIn("无法在本机上使用", result["reason"])

    def test_empty_lora_is_incompatible(self):
        """0 字节文件 -> compatible=false。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.safetensors"
            path.write_bytes(b"")
            result = lora_registry.check_compatibility(
                path, self._z_unet(), family="Z-Image")
            self.assertFalse(result.get("compatible", True))

    def test_good_lora_stays_compatible(self):
        """完好文件不能被这次修复误伤（仍 compatible=true）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = write_minimal_safetensors(
                Path(tmp) / "good.safetensors", z_image_like_tensors())
            result = lora_registry.check_compatibility(
                path, self._z_unet(), family="Z-Image")
            self.assertTrue(
                result.get("compatible", True),
                f"完好文件被误判为不兼容：{result.get('reason')}")


class TestRealMachineDamagedFiles(unittest.TestCase):
    """真机回归：本机真实存在的损坏文件必须被挡住。"""

    def test_real_damaged_files_are_rejected_by_generic_reader(self):
        """本机真实损坏文件 -> 通用读取器读不出头。"""
        found = 0
        for rel in DAMAGED_SAMPLES:
            path = _find_damaged(rel)
            if path is None:
                continue
            found += 1
            self.assertEqual(
                lora_registry._read_safetensors_header(path), {},
                f"{rel} 是损坏文件，通用读取器却读出了头部")
        if not found:
            self.skipTest("本机没有这些损坏样本文件")

    def test_real_damaged_files_not_surfaced_as_compatible(self):
        """损坏文件不得出现在任何族的 compatible=true 清单里。"""
        damaged = {}
        for rel in DAMAGED_SAMPLES:
            path = _find_damaged(rel)
            if path is not None:
                damaged[str(path)] = rel
        if not damaged:
            self.skipTest("本机没有这些损坏样本文件")

        def _list_for(family: str) -> list[dict]:
            if family == "Z-Image":
                return lora_registry.list_effect_loras()
            if family == "Qwen-Image 2.1":
                return lora_registry.list_qwen_effect_loras()
            return lora_registry.list_moss_effect_loras()

        for family in FAMILIES:
            for item in _list_for(family):
                if not item.get("compatible", True):
                    continue
                path = item.get("path") or ""
                if path in damaged:
                    self.fail(
                        f"{family} 清单把损坏文件 {damaged[path]} 以 compatible=true 下发")


class TestNoDamagedEntryIsSurfacedAsCompatible(unittest.TestCase):
    """**全量**扫描：三族所有下发条目都必须文件完整。"""

    def test_available_lists_have_no_damaged_entry(self):
        """available 列表里不允许出现「文件损坏却 compatible=true」的条目。"""
        def _list_for(family: str) -> list[dict]:
            if family == "Z-Image":
                return lora_registry.list_effect_loras()
            if family == "Qwen-Image 2.1":
                return lora_registry.list_qwen_effect_loras()
            return lora_registry.list_moss_effect_loras()

        checked = 0
        for family in FAMILIES:
            for item in _list_for(family):
                if not item.get("compatible", True):
                    continue
                path = item.get("path")
                if not path or not Path(path).is_file():
                    continue
                checked += 1
                detail = mlc.read_safetensors_header_detail(path)
                self.assertTrue(
                    detail["ok"],
                    f"{family} 清单里的 {item['name']} 文件本体损坏："
                    f"{detail['reason']}")
        if not checked:
            self.skipTest("本机没有可校验的已安装 LoRA")

    def test_every_damaged_file_on_disk_is_excluded_from_lists(self):
        """本机磁盘上所有损坏文件，都不得出现在任何族的可用清单里。"""
        damaged: dict[str, str] = {}
        for d in _lora_dirs():
            try:
                candidates = sorted(d.rglob("*.safetensors"))
            except Exception:
                continue
            for p in candidates:
                detail = mlc.read_safetensors_header_detail(p)
                if not detail["ok"]:
                    damaged[str(p)] = p.name
        if not damaged:
            self.skipTest("本机磁盘上没有损坏文件")

        surfaced: set[str] = set()
        for family in FAMILIES:
            if family == "Z-Image":
                items = lora_registry.list_effect_loras()
            elif family == "Qwen-Image 2.1":
                items = lora_registry.list_qwen_effect_loras()
            else:
                items = lora_registry.list_moss_effect_loras()
            for item in items:
                if item.get("compatible", True):
                    surfaced.add(item.get("path") or "")
        leaked = sorted(surfaced & set(damaged))
        self.assertEqual(
            leaked, [],
            f"这些损坏文件仍以 compatible=true 下发："
            f"{[damaged[p] for p in leaked]}")


class TestBaseUnetFilesStillReadable(unittest.TestCase):
    """完整性校验不能误杀底模本体（底模也是 safetensors）。"""

    def test_z_image_and_qwen_unet_headers_readable(self):
        for label, unet in (("Z-Image", lora_registry._z_image_unet_path()),
                            ("Qwen-Image 2.1", lora_registry._qwen_image_unet_path())):
            if unet is None:
                continue
            header = lora_registry._read_safetensors_header(Path(unet))
            self.assertTrue(
                header,
                f"{label} 底模的头部读不出来（完整性校验误伤了底模）")


if __name__ == "__main__":
    unittest.main()
