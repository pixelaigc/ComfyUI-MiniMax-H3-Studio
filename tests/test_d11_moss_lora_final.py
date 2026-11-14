# -*- coding: utf-8 -*-
"""D11 收口轮：本轮修的 3 条不变量（锁死，防回归）。

背景：独立验收指出 2 条 MOSS-Image LoRA 残缺缺陷，接手复核的结论是
—— 缺陷 B（文件损坏）已由 `9249656` 修好（fail closed + 完整性校验）；
缺陷 A（半静默失效）的**根因**也已由 `9249656` 修好（`_lora_unet_to_dots`
死分支 + 融合 qkv 命中率口径），实测 patched 137 -> 263 / missing 126 -> 0。

但复核发现**两条真实残留**，本文件锁死它们的修复：

R1 **派生文件不得进用户可选列表**
    MOSS 的归一化派生文件（`h3studio_mossnorm__<原名>`）是本服务自己在
    提交工作流时生成的**中间产物**，是原文件的**下游**。
    旧过滤只排了 `lora_damping.DERIVED_PREFIX`（= `h3studio_ctx_`），
    **漏了 MOSS 这一族**；而且判据用的是 `rel.startswith(prefix)`，
    而派生文件在**族专属子目录**里（`moss-effects/h3studio_mossnorm__*.safetensors`），
    相对路径永远不以该前缀开头 -> 过滤彻底失效。
    实测后果：同一条 LoRA 在 MOSS 面板下拉里出现两次，两条 display
    完全相同（都继承原文件的「Fizgig Look LoRA（HF 原生）」）。

R2 **不可用条目不得以 compatible=true 下发**（通用不变量）
    文件本体读不出 / 被截断 -> 必须 compatible=false 且带如实原因。

R3 **形态必须如实标注**
    服务端已下发 `qkv_shape` / `qkv_convertible`，前端必须用它把
    「融合 qkv（自动转换）」标出来 —— 不能让融合形态的条目跟真·分离
    形态的条目长得一模一样（用户会以为「原生」就能直接用）。

运行：python3 -m pytest tests/test_d11_moss_lora_final.py -v
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import loras as lora_registry            # noqa: E402
from h3studio import moss_lora_compat as mlc            # noqa: E402
from h3studio import moss_lora_split as split           # noqa: E402


D1 = "moss-effects/moss_image_sft_look_lora.safetensors"
D2 = "moss-effects/hina_MOSSImageBase_animeEnhance_v1.0-lora.safetensors"


class TestDerivedFilesNotListed(unittest.TestCase):
    """R1：派生文件不得出现在用户可选列表里。"""

    def _all_listed_names(self):
        names = set()
        for lister in (lora_registry.list_moss_effect_loras,
                       lora_registry.list_z_image_loras,
                       lora_registry.list_qwen_image_loras):
            try:
                for item in lister():
                    names.add(item["name"])
            except Exception:  # noqa: BLE001
                continue
        return names

    def test_moss_derived_prefix_is_never_in_available_list(self):
        names = self._all_listed_names()
        leaked = [n for n in names
                  if Path(n).name.startswith(split.DERIVED_PREFIX)]
        self.assertEqual(
            leaked, [],
            f"归一化派生文件不得进用户可选列表（实测泄漏 {len(leaked)} 条）："
            f"{leaked[:5]}")

    def test_ctx_derived_prefix_is_never_in_available_list(self):
        from h3studio import lora_damping
        names = self._all_listed_names()
        leaked = [n for n in names
                  if Path(n).name.startswith(lora_damping.DERIVED_PREFIX)]
        self.assertEqual(leaked, [], f"阻尼派生文件泄漏：{leaked[:5]}")

    def test_d1_and_its_derived_do_not_both_appear(self):
        """原文件与派生文件不得同时出现在列表里（否则下拉出现重复项）。"""
        names = self._all_listed_names()
        derived = split.derived_name(Path(D1).name)
        derived_rel = "moss-effects/" + derived
        if D1 in names:
            self.assertNotIn(
                derived_rel, names,
                "原文件与它的归一化派生文件同时出现 -> 下拉里是同一条 LoRA "
                "重复两次（display 还完全相同），用户无法分辨")

    def test_derived_filter_uses_filename_not_relative_path(self):
        """回归点：过滤判据必须看**文件名**。

        派生文件在族专属子目录里（`moss-effects/h3studio_mossnorm__*.safetensors`），
        相对路径不以 `h3studio_mossnorm__` 开头 —— 旧判据
        `rel.startswith(prefix)` 因此永远不成立。
        """
        source = (REPO / "h3studio" / "loras.py").read_text(encoding="utf-8")
        self.assertIn(
            "path.name.startswith(moss_lora_split.DERIVED_PREFIX)",
            source,
            "过滤判据必须按文件名匹配（派生文件在子目录里，"
            "rel.startswith 永远不成立）")


class TestUnusableEntriesNeverCompatible(unittest.TestCase):
    """R2：不可用的条目不得以 compatible=true 下发。"""

    def test_no_available_entry_has_damaged_file(self):
        """通用不变量：可选列表里不允许「文件本体坏掉」的条目。"""
        for item in lora_registry.list_moss_effect_loras():
            path = lora_registry.resolve_lora(item["name"])[1]
            if not path:
                continue
            detail = mlc.read_safetensors_header_detail(path)
            if not detail["ok"]:
                self.fail(
                    f"{item['name']} 读不出完整文件却仍以 compatible=true 下发："
                    f"{detail['reason']}")
            self.assertTrue(item.get("compatible"),
                            f"{item['name']} 文件完好却未标记 compatible")

    def test_d2_is_not_in_available_and_is_incompatible(self):
        path = lora_registry.resolve_lora(D2)[1]
        if not path:
            self.skipTest("本机没有该 LoRA")
        result = mlc.check_moss_compatibility(path)
        self.assertFalse(result["compatible"],
                         "损坏的权重文件绝不能以 compatible=true 下发")
        self.assertTrue(result["reason"], "必须给出可读的原因")
        names = {i["name"] for i in lora_registry.list_moss_effect_loras()}
        self.assertNotIn(D2, names, "损坏的 LoRA 不得出现在可选列表里")

    def test_d1_is_compatible_and_fully_attachable(self):
        """D1：修复后必须能完整挂上（转换后 missing == 0）。"""
        path = lora_registry.resolve_lora(D1)[1]
        if not path:
            self.skipTest("本机没有该 LoRA")
        result = mlc.check_moss_compatibility(path)
        self.assertTrue(result["compatible"], result.get("reason"))
        self.assertEqual(result.get("qkv_shape"), mlc.QKV_SHAPE_FUSED,
                         "D1 是融合 qkv 形态")
        self.assertTrue(result.get("convertible"),
                        "D1 的融合 qkv 必须可无损拆分（可转换）")
        self.assertEqual(result.get("attention_hit"), 1.0,
                         "可无损拆分的融合 qkv 必须按「转换后」口径算命中率")

    def test_d1_conversion_produces_split_shape(self):
        import tempfile
        path = lora_registry.resolve_lora(D1)[1]
        if not path:
            self.skipTest("本机没有该 LoRA")
        with tempfile.TemporaryDirectory() as tmp:
            out = split.normalize_lora(path, target_dir=Path(tmp))
            self.assertTrue(out.get("ok"), out.get("reason"))
            self.assertTrue(out.get("changed"), "D1 需要转换")
            header = mlc.read_safetensors_header(out["target"])
            self.assertTrue(header, "派生文件必须能读出头")
            self.assertNotEqual(mlc.detect_qkv_shape(header),
                                mlc.QKV_SHAPE_FUSED,
                                "派生文件必须是分离形态")


class TestShapeIsReportedAndSurfaced(unittest.TestCase):
    """R3：形态必须如实下发 + 前端如实标注。"""

    def test_api_payload_carries_qkv_shape_fields(self):
        """服务端必须把形态字段下发到前端（否则前端无从标注）。"""
        for item in lora_registry.list_moss_effect_loras():
            self.assertIn("qkv_shape", item,
                          "MOSS 清单条目必须带 qkv_shape")
            self.assertIn("qkv_convertible", item,
                          "MOSS 清单条目必须带 qkv_convertible")

    def test_frontend_renders_qkv_tag(self):
        """前端必须渲染「融合 qkv」标注 + 详情里的形态行。"""
        app = (REPO / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("qkv_shape", app,
                      "前端必须读 qkv_shape 才能如实标注形态")
        self.assertIn("qkv_convertible", app,
                      "前端必须读 qkv_convertible 才能区分可转换/不可用")
        self.assertIn("img-lora-tag qkv", app,
                      "前端必须有 qkv 标签的渲染分支")
        self.assertIn("权重形态", app,
                      "详情面板必须有「权重形态」行")

    def test_frontend_style_defines_qkv_tag(self):
        css = (REPO / "web" / "style.css").read_text(encoding="utf-8")
        self.assertIn(".img-lora-tag.qkv", css,
                      "必须有 qkv 标签的样式")


class TestNoRegressionOtherFamilies(unittest.TestCase):
    """回归：Z-Image 与 Qwen-Image 2.1 的 LoRA 清单不受影响。"""

    def test_z_image_list_still_populated(self):
        items = lora_registry.list_z_image_loras()
        self.assertGreater(len(items), 10, "Z-Image 清单不应为空")
        self.assertTrue(all("compatible" in i for i in items))

    def test_qwen_list_still_populated(self):
        items = lora_registry.list_qwen_image_loras()
        self.assertGreater(len(items), 10, "Qwen-Image 2.1 清单不应为空")
        self.assertTrue(all("compatible" in i for i in items))

    def test_other_families_have_no_moss_derived_leak(self):
        """MOSS 的派生过滤不能误伤另外两族。"""
        for lister in (lora_registry.list_z_image_loras,
                       lora_registry.list_qwen_image_loras):
            for item in lister():
                self.assertFalse(
                    Path(item["name"]).name.startswith(split.DERIVED_PREFIX),
                    f"Z/Qwen 族出现 MOSS 派生文件：{item['name']}")


if __name__ == "__main__":
    unittest.main()
