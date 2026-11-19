# -*- coding: utf-8 -*-
"""缺陷 A 收口轮（2026-11-15 第二轮）：**用户路径**可行性判定。

【本轮要锁死的不变量】

上一轮（`9249656`）修好了两件事：
  * 缺陷 B（文件截断）-> 完整性校验 + fail closed
  * 缺陷 A 的**根因**（文本塔键死分支 + 融合 qkv 命中率口径）
    -> 转换后 patched 137 -> 263 / missing 126 -> 0

但复核发现**判定本身还有缺口**：

    `check_moss_compatibility` 只看 **safetensors 头部**，
    判出 compatible=true 的含义是「**结构**对得上底模」。

    而用户能不能用，取决于**另一件事**：这份权重是不是**融合 qkv** 形态
    —— 是的话，ComfyUI 会静默丢弃它（只打 "lora key not loaded"，
    出图照跑但 LoRA 没生效），必须先转成派生文件才能用。

    `convertible` 只按头部形态推断，**从未验证过转换真的可行**
    （转换要读 2GB 权重 + 写 2GB 派生文件，完全可能失败）。

    于是「结构兼容但转换不可行 / 未验证」的 LoRA 依然以 compatible=true
    照常下发 —— 这就是缺陷 A 说的「半静默失效」。

本文件锁死：
  R1 `conversion_status` 四态判定正确
      direct     分离形态，原样可挂
      derived    融合形态 + 派生文件已存在且完整 -> 转换**已验证**
      unverified 融合形态 + 无派生文件 -> 转换**未验证**
      broken     文件本体损坏
  R2 **未验证的融合形态条目不得以 compatible=true 下发**（本轮核心）
  R3 派生文件存在（已验证）时 compatible=true 成立
  R4 损坏文件不得以 compatible=true 下发
  R5 清单接口必须下发 conversion_status / needs_conversion 字段
  R6 前端必须按转换状态如实标注三档

运行：python3 -m pytest tests/test_d11_defect_a_conversion.py -v
"""
from __future__ import annotations

import os

import json
import struct
import tempfile
import unittest
from pathlib import Path

from h3studio import loras as lora_registry
from h3studio import moss_lora_compat as mlc
from h3studio import moss_lora_split as split


def write_safetensors(path: Path, tensors: dict, *, truncate_to: int | None = None) -> Path:
    """写一个最小可用的 safetensors 文件（与 test_moss_lora_damaged_and_partial 同款）。"""
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


def split_tensors(layers: int = 1, dim: int = 8, rank: int = 4):
    """分离 q/k/v 形态（与本机底模结构一致，可直接挂）。"""
    out = {}
    for i in range(layers):
        for proj in ("q", "k", "v"):
            stem = f"lora_unet_layers_{i}_proj_{proj}"
            out[f"{stem}.alpha"] = ((), "F32")
            out[f"{stem}.lora_down.weight"] = ((rank, dim), "F16")
            out[f"{stem}.lora_up.weight"] = ((dim, rank), "F16")
    return out


def fused_qkv_tensors(layers: int = 1, dim: int = 8, rank: int = 4):
    """融合 qkv 形态（行映射第 0 维 = 3*dim，可无损拆分）。"""
    out = {}
    for i in range(layers):
        stem = f"lora_unet_layers_{i}_proj_qkv"
        out[f"{stem}.alpha"] = ((), "F32")
        out[f"{stem}.lora_down.weight"] = ((rank, dim), "F16")
        out[f"{stem}.lora_up.weight"] = ((3 * dim, rank), "F16")
    return out


class TestConversionStatus(unittest.TestCase):
    """R1：四态判定。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_split_shape_is_direct(self):
        p = write_safetensors(self.dir / "split.safetensors", split_tensors())
        st = split.conversion_status(p)
        self.assertEqual(st["status"], split.STATUS_DIRECT)
        self.assertFalse(st["needs_conversion"])

    def test_fused_without_derived_is_unverified(self):
        p = write_safetensors(self.dir / "fused.safetensors", fused_qkv_tensors())
        st = split.conversion_status(p)
        self.assertEqual(st["status"], split.STATUS_UNVERIFIED)
        self.assertTrue(st["needs_conversion"])
        self.assertTrue(st["reason"], "必须给出可读的原因")

    def test_fused_with_valid_derived_is_derived(self):
        p = write_safetensors(self.dir / "fused2.safetensors", fused_qkv_tensors())
        # 造一个「归一形态」的派生文件（分离 q/k/v）
        derived = self.dir / split.derived_name("fused2.safetensors")
        write_safetensors(derived, split_tensors())
        st = split.conversion_status(p)
        self.assertEqual(st["status"], split.STATUS_DERIVED)
        self.assertTrue(st["needs_conversion"])
        self.assertEqual(Path(st["derived_path"]), derived)

    def test_broken_file_is_broken(self):
        p = write_safetensors(self.dir / "broken.safetensors",
                               fused_qkv_tensors(), truncate_to=16)
        st = split.conversion_status(p)
        self.assertEqual(st["status"], split.STATUS_BROKEN)
        self.assertTrue(st["reason"])


class TestUnverifiedNotServedAsCompatible(unittest.TestCase):
    """R2：**未验证的融合形态条目不得以 compatible=true 下发**（本轮核心）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_unverified_fused_is_not_compatible(self):
        p = write_safetensors(self.dir / "fused.safetensors", fused_qkv_tensors())
        result = mlc.check_moss_compatibility(p)
        self.assertFalse(
            result["compatible"],
            "融合形态且转换未验证 -> 绝不能以 compatible=true 下发"
            "（用户选了才发现 LoRA 不生效 = 半静默失效）")
        self.assertTrue(result["reason"], "必须给出可读的原因")
        self.assertEqual(result.get("conversion_status"), split.STATUS_UNVERIFIED)
        self.assertTrue(result.get("needs_conversion"))

    def test_derived_fused_is_compatible(self):
        """R3：派生文件存在（转换已验证）-> compatible=true 成立。"""
        p = write_safetensors(self.dir / "fused2.safetensors", fused_qkv_tensors())
        write_safetensors(self.dir / split.derived_name("fused2.safetensors"),
                          split_tensors())
        result = mlc.check_moss_compatibility(p)
        self.assertTrue(result["compatible"], result.get("reason"))
        self.assertEqual(result.get("conversion_status"), split.STATUS_DERIVED)

    def test_broken_file_is_not_compatible(self):
        """R4：损坏文件不得以 compatible=true 下发。"""
        p = write_safetensors(self.dir / "broken.safetensors",
                               fused_qkv_tensors(), truncate_to=16)
        result = mlc.check_moss_compatibility(p)
        self.assertFalse(result["compatible"])
        self.assertTrue(result["reason"])


class TestManifestCarriesConversionStatus(unittest.TestCase):
    """R5：清单接口必须下发转换状态字段。"""

    def test_moss_entries_carry_conversion_fields(self):
        for item in lora_registry.list_moss_image_loras():
            self.assertIn("conversion_status", item,
                          "MOSS 清单条目必须带 conversion_status")
            self.assertIn("needs_conversion", item,
                          "MOSS 清单条目必须带 needs_conversion")

    def test_no_available_entry_is_unverified(self):
        """通用不变量：可选列表里不允许出现「转换未验证」的条目。"""
        for item in lora_registry.list_moss_effect_loras():
            self.assertNotEqual(
                item.get("conversion_status"), split.STATUS_UNVERIFIED,
                f"{item['name']} 转换未验证却出现在可选列表里 —— "
                "用户选中后会遇到 LoRA 不生效（半静默失效）")
            self.assertTrue(item.get("compatible"),
                            f"{item['name']} 在可选列表里却不是 compatible")

    def test_no_available_entry_has_broken_file(self):
        """R4 的清单层不变量：文件本体坏掉的条目不得在可选列表里。"""
        for item in lora_registry.list_moss_effect_loras():
            rel, path = lora_registry.resolve_lora(item["name"])
            if not path:
                continue
            detail = mlc.read_safetensors_header_detail(path)
            self.assertTrue(detail["ok"],
                            f"{item['name']} 文件本体损坏却在可选列表里："
                            f"{detail['reason']}")


class TestFrontendAnnotatesConversionStatus(unittest.TestCase):
    """R6：前端必须按转换状态如实标注三档。"""

    @classmethod
    def setUpClass(cls):
        cls.app = (Path(__file__).resolve().parent.parent
                   / "web" / "app.js").read_text(encoding="utf-8")
        cls.css = (Path(__file__).resolve().parent.parent
                   / "web" / "style.css").read_text(encoding="utf-8")

    def test_frontend_reads_conversion_status(self):
        self.assertIn("conversion_status", self.app,
                      "前端必须读 conversion_status 才能如实标注")

    def test_frontend_has_three_tier_labels(self):
        self.assertIn("融合 qkv · 已验证可转换", self.app)
        self.assertIn("融合 qkv · 转换未验证", self.app)

    def test_frontend_does_not_fake_native_verdict(self):
        """空 base_verdict 不得再兜底成「原生」。"""
        self.assertNotIn(
            'meta.base_verdict || "原生"', self.app,
            "空的 base_verdict 不得兜底成「原生」—— "
            "本机 84 条 MOSS 条目目录里没登记基座判定结论，"
            "兜底成「原生」是误导")
        self.assertIn("未标注（本机实测可用，但上游目录未登记基座判定结论）",
                      self.app)

    def test_css_defines_qkv_tag(self):
        self.assertIn(".img-lora-tag.qkv", self.css)


class TestRealMachineFiles(unittest.TestCase):
    """本机真的装了这些文件时的真文件回归（没装就跳过）。"""

    D1 = "moss-effects/moss_image_sft_look_lora.safetensors"
    D2 = "moss-effects/hina_MOSSImageBase_animeEnhance_v1.0-lora.safetensors"

    def _resolve(self, rel):
        return lora_registry.resolve_lora(rel)[1]

    def test_d1_is_available_and_fully_attachable(self):
        """D1：可转换且转换已验证 -> 可选且完整生效。"""
        path = self._resolve(self.D1)
        if not path:
            self.skipTest("本机没有该 LoRA")
        result = mlc.check_moss_compatibility(path)
        self.assertTrue(result["compatible"], result.get("reason"))
        self.assertEqual(result.get("conversion_status"), split.STATUS_DERIVED)
        names = {i["name"] for i in lora_registry.list_moss_effect_loras()}
        self.assertIn(self.D1, names)

    def _is_file_intact(self, path) -> bool:
        """磁盘文件本体是否**完整**（safetensors 头声明长度 == 实际大小）。

        【为什么需要这个判据】—— 2026-11-19 第 3 轮 B 项的根因
        本测试原本写死「D2 文件本体损坏 -> 必须 incompatible」。
        但 D2（hina_MOSSImageBase_animeEnhance_v1.0-lora.safetensors）
        在 **2026-11-15 15:17** 被重新下载修复过：

          当时（36d510b，11-15 06:00）：5,369,856 B / 头部声明需 297,355,184 B
                                    -> 缺 291,985,328 B（真损坏）
          现在（11-15 15:17 起）  ：297,355,184 B / 头部声明需 297,355,184 B
                                    -> 差值 0 B（**已完整**）

        ⇒ 测试的**前提失效**了（不是产品缺陷）：
          产品现在判 D2 compatible=True / qkv_shape=split /
          conversion_status=direct / verified=True —— 这**是对的**。
        ⇒ 断言必须按**文件当前实际状态**自适应，而不是钉死历史状态。
        """
        import struct
        import json as _json
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                n = struct.unpack("<Q", fh.read(8))[0]
                header = _json.loads(fh.read(n).decode("utf-8"))
        except Exception:
            return False
        max_end = 0
        for key, val in header.items():
            if key == "__metadata__":
                continue
            if isinstance(val, dict) and "data_offsets" in val:
                max_end = max(max_end, int(val["data_offsets"][1]))
        return size == 8 + n + max_end

    def test_d2_state_matches_actual_file_integrity(self):
        """D2：判定必须与**文件当前实际完整性**一致。

        * 文件完整 -> 必须 compatible 且出现在可选列表里
          （2026-11-15 15:17 起 D2 已修复，本断言转为这一支）
        * 文件损坏 -> 必须 incompatible 且**不得**出现在可选列表里
          （fail closed；本测试原本只覆盖这一支）
        """
        path = self._resolve(self.D2)
        if not path:
            self.skipTest("本机没有该 LoRA")
        result = mlc.check_moss_compatibility(path)
        names = {i["name"] for i in lora_registry.list_moss_effect_loras()}
        if self._is_file_intact(path):
            # 文件已修复 -> 产品必须把它当作可用的 LoRA
            self.assertTrue(result["compatible"],
                            f"文件已完整，compatible 必须为 True：{result}")
            self.assertIn(self.D2, names,
                          "文件已完整的 LoRA 必须出现在可选列表里")
        else:
            # 文件仍损坏 -> fail closed
            self.assertFalse(result["compatible"],
                             f"文件损坏时 compatible 必须为 False：{result}")
            self.assertTrue(result["reason"])
            self.assertNotIn(self.D2, names,
                             "损坏的 LoRA 不得出现在可选列表里")

    def test_truncated_file_is_incompatible_regardless_of_real_files(self):
        """**自造**损坏样本 -> 必须 incompatible（不依赖本机任何文件状态）。

        【为什么必须自造】—— 原来那条断言把「回归防护」绑在
        「本机某个特定文件恰好是坏的」上。D2 在 2026-11-15 被修复后，
        那条断言就**永远绿**了 —— 真正的防护（损坏文件不得被当成可用）
        **实际已经失效**：把 fail closed 逻辑改坏，这条测试照样绿。

        本条用**截断副本**造一个必然损坏的文件，锁住真实行为。
        截断长度取 5,369,856 —— 那是 2026-11-15 实测 D2 损坏时的**真实大小**。
        """
        import shutil
        import tempfile
        src = self._resolve(self.D2)
        if not src or not os.path.exists(src):
            self.skipTest("本机没有该 LoRA，无法造截断副本")
        tmpdir = tempfile.mkdtemp(prefix="d11_trunc_")
        try:
            broken = os.path.join(tmpdir, "truncated.safetensors")
            with open(src, "rb") as fin, open(broken, "wb") as fout:
                fout.write(fin.read(5_369_856))
            result = mlc.check_moss_compatibility(broken)
            self.assertFalse(result["compatible"],
                             f"截断文件必须 incompatible：{result}")
            self.assertTrue(result["reason"],
                            "截断文件必须给出明确原因")
            self.assertIn("截断", result["reason"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_unreadable_file_is_incompatible(self):
        """**读不出头部**的文件 -> 必须 incompatible（fail closed 的另一半）。

        【为什么单独一条】—— 2026-11-19 第 3 轮 B 项的变异自证发现：
        把 `check_moss_compatibility` 里的 `if not detail["ok"]` 分支
        关掉（= 读不出头部就当兼容），「截断样本」那条**抓不住** ——
        因为截断文件的头部是**能读出来的**（完整性判定在更内层先拦住），
        根本走不到那个分支。

        ⇒ 必须另造一个「头部读不出来」的样本：
          * 空文件（读 8 字节长度就抛）
          * 非 safetensors 垃圾内容（头部长度字段是垃圾）
        这两个样本直接命中 `read_safetensors_header_detail` 的异常分支。
        """
        import shutil
        import tempfile
        tmpdir = tempfile.mkdtemp(prefix="d11_unread_")
        try:
            cases = {
                "empty.safetensors": b"",
                "garbage.safetensors": b"NOT-A-SAFETENSORS" * 64,
            }
            for name, blob in cases.items():
                p = os.path.join(tmpdir, name)
                with open(p, "wb") as fh:
                    fh.write(blob)
                result = mlc.check_moss_compatibility(p)
                self.assertFalse(result["compatible"],
                                 f"{name} 必须 incompatible：{result}")
                self.assertTrue(result["reason"],
                                f"{name} 必须给出明确原因")
                self.assertEqual(0.0, result["attention_hit"],
                                 f"{name} 的命中率必须是 0，不能是 1.0")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
