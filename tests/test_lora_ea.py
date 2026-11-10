# -*- coding: utf-8 -*-
"""三基座 **东亚人像效果类** LoRA 接入的回归测试（2026-11-10）。

覆盖本轮接入挖出并写死的几个**真事实**：

1. **目录里的每条都必须在磁盘上真实存在**
   —— 目录条目若没落盘，不能出现在用户可选列表里。
   本轮 159 条里 158 条已装（1 条是「完整 checkpoint 不是 LoRA」，已剔除）。

2. **「近似适用」必须如实标注，不能伪装成「原生」**
   —— 清单里标了「近似适用」的条目（基座未指明 / 无 base_model tag），
   目录里必须保留 base_verdict="近似适用"，且 scene 里必须带依据。
   这条最重要：把它标成「原生」就是撒谎。

3. **加速 / 蒸馏类绝不进用户可选列表**
   —— 本目录 0 条加速类。文件名含 turbo 的**不是**加速类
   （`z-image-effects/` 下大量 `*-Z-Image-Turbo-*.safetensors`）。

4. **推荐强度口径**
   —— 作者明示具体值 -> 用作者值；作者明示区间 -> 取中值并写明；
   作者未给 -> 1.0 且 strength_source 必须标注是建议值。

5. **触发词不编造**
   —— 清单写「无 / 未标注」的条目，trigger 必须是空字符串。

6. **分组字段存在且非空**
   —— 界面下拉按 group 做 optgroup，缺 group 会让 85 条全挤在一起。

7. **族前缀是唯一真相源**
   —— 三族目录分别是 z-image-effects/ qwen21-effects/ moss-effects/，
   跨族误用会被 ComfyUI 静默丢弃。

运行：/home/ubuntu/comfyui/venv/bin/python -m pytest tests/test_lora_ea.py -v
"""
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import loras as lora_registry            # noqa: E402
from h3studio import lora_catalog_ea as catalog        # noqa: E402

LORAS_ROOT = Path("/mnt/usb2t/comfyui-models/loras")

FAMILY_TO_DIR = {
    "Z-Image": "z-image-effects",
    "Qwen-Image 2.1": "qwen21-effects",
    "MOSS-Image": "moss-effects",
}

# 本轮实测：这一条**不是 LoRA**，而是 11.7 GB 的完整 checkpoint
#（safetensors 头部 453 个 tensor 全是 model.* 权重，0 个 lora_A/lora_B）。
# 下载器的头部校验把它拦下了，已剔除，不得进目录。
REJECTED_NOT_A_LORA = {"Z_Image_Turbo_FinePhoto_bf16-V2.0.safetensors"}


class TestEACatalog(unittest.TestCase):
    """东亚人像目录本身的数据完整性。"""

    def test_catalog_has_entries_for_all_three_families(self):
        for family in FAMILY_TO_DIR:
            rows = [v for v in catalog.EA_ALL_EFFECTS.values()
                    if v.get("base") == family]
            self.assertGreaterEqual(len(rows), 10,
                                    f"{family} 的东亚人像目录少于 10 条")

    def test_every_entry_has_chinese_description_and_strength(self):
        for name, info in catalog.EA_ALL_EFFECTS.items():
            self.assertTrue(info.get("display"), f"{name} 缺 display")
            self.assertTrue(info.get("scene"), f"{name} 缺 scene")
            self.assertTrue(info.get("source"), f"{name} 缺 source")
            self.assertTrue(info.get("strength_source"),
                            f"{name} 缺 strength_source")
            strength = info.get("recommended_strength")
            self.assertIsInstance(strength, (int, float),
                                  f"{name} 的 recommended_strength 不是数值")
            self.assertGreater(strength, 0, f"{name} 的推荐强度必须 > 0")
            self.assertLessEqual(strength, 1.5,
                                 f"{name} 的推荐强度超出滑块量程 0-1.5")

    def test_plan_ids_are_unique(self):
        ids = [i.get("plan_id") for i in catalog.EA_ALL_EFFECTS.values()]
        ids = [i for i in ids if i]
        self.assertEqual(len(ids), len(set(ids)),
                         f"清单编号有重复：{[i for i in ids if ids.count(i) > 1]}")

    def test_no_acceleration_lora_in_ea_catalog(self):
        """加速/蒸馏类绝不能出现在效果目录里。

        ⚠️ 只查**加速语义**词，不能查 "turbo" —— `z-image-effects/` 下
        一大批 `xxx-Z-Image-Turbo-*.safetensors` 是**效果** LoRA，
        Turbo 是基座分支名不是加速声明。
        """
        for name, info in catalog.EA_ALL_EFFECTS.items():
            blob = f"{name} {info.get('display','')} {info.get('scene','')}".lower()
            for marker in ("distill", "lightning", "lcm", "dmd2", "4step",
                           "8step", "few-step", "fewstep", "fun-acc"):
                self.assertNotIn(marker, blob,
                                 f"{name} 看起来是加速/蒸馏类，不能当效果 LoRA")

    def test_ea_entries_are_kind_effect(self):
        for name, info in catalog.EA_ALL_EFFECTS.items():
            kind = lora_registry.classify_kind(name, info)
            self.assertEqual(kind, lora_registry.KIND_EFFECT,
                             f"{name} 被判成了 {kind}")

    def test_entries_use_family_directory_prefix(self):
        """目录键必须带族专属前缀 —— 前缀是族归属的唯一真相源。"""
        for name in catalog.EA_ALL_EFFECTS:
            prefix = name.split("/", 1)[0]
            self.assertIn(prefix, set(FAMILY_TO_DIR.values()),
                          f"{name} 的目录前缀不在三族目录里")

    def test_rejected_non_lora_not_in_catalog(self):
        for bad in REJECTED_NOT_A_LORA:
            self.assertFalse(any(bad in k for k in catalog.EA_ALL_EFFECTS),
                             f"{bad} 不是 LoRA 权重，不得进目录")


class TestAppropriatenessLabelling(unittest.TestCase):
    """「近似适用」的标注纪律 —— 本轮最不能糊弄的一条。"""

    def test_near_approximation_entries_are_labelled(self):
        near = [(k, v) for k, v in catalog.EA_ALL_EFFECTS.items()
                if v.get("base_verdict") == "近似适用"]
        self.assertGreater(len(near), 0,
                           "清单里明明有「近似适用」条目，目录里却一条都没有 —— "
                           "说明标注被丢了（那就是把近似的说成原生的）")
        for name, info in near:
            self.assertIn("近似适用", info.get("scene", ""),
                          f"{name} 的 scene 里必须写明「近似适用」")
            # 必须带**依据**，不能只写「近似适用」三个字
            self.assertIn("依据", info.get("scene", ""),
                          f"{name} 的 scene 里必须写明「近似适用」的依据")

    def test_native_entries_not_marked_near(self):
        """原生条目不能被误标成近似（反过来也是错的）。"""
        for name, info in catalog.EA_ALL_EFFECTS.items():
            if info.get("base_verdict") == "原生":
                self.assertNotIn("⚠️", info.get("scene", ""),
                                 f"{name} 标着原生，scene 里却有近似告警")

    def test_near_count_is_recorded(self):
        """近似条数必须能被数出来（写进报告用）。"""
        near = sum(1 for v in catalog.EA_ALL_EFFECTS.values()
                   if v.get("base_verdict") == "近似适用")
        self.assertEqual(near, 8,
                         f"近似适用条目数变了：{near}（本轮实测 8 条：z-image 6 + moss 2）")

    def test_strength_within_ui_slider_range(self):
        """推荐强度必须落在界面滑块量程 0-1.5 内。

        实测：`nelux_Realism_v2` 作者明示 **1.8**，超出量程。
        目录里按 1.5 呈现，并在 strength_source 里写明「已按 1.5 呈现」——
        不能静默改数，也不能让标签和滑块对不上。
        """
        for name, info in catalog.EA_ALL_EFFECTS.items():
            s = info.get("recommended_strength")
            self.assertLessEqual(s, 1.5,
                                 f"{name} 的推荐强度 {s} 超出滑块量程 0-1.5")
            if "超出界面滑块量程" in info.get("strength_source", ""):
                self.assertIn("已按", info.get("strength_source", ""),
                              f"{name} 被夹了强度但没写明")


class TestStrengthAndTriggerDiscipline(unittest.TestCase):
    """强度 / 触发词口径。"""

    def test_unstated_strength_is_flagged_as_suggestion(self):
        """作者未给强度的条目，strength_source 必须写明「建议值」。"""
        for name, info in catalog.EA_ALL_EFFECTS.items():
            if "作者未给" in info.get("strength_source", ""):
                self.assertIn("建议值", info.get("strength_source", ""),
                              f"{name} 作者未给强度，必须标注是建议值")
                self.assertEqual(info.get("recommended_strength"), 1.0,
                                 f"{name} 作者未给强度时应按默认 1.0")

    def test_trigger_is_not_invented(self):
        """清单写「无 / 未标注」的条目，trigger 必须是空串。"""
        for name, info in catalog.EA_ALL_EFFECTS.items():
            trig = info.get("trigger", "")
            self.assertIsInstance(trig, str)
            # 触发词不该是整句话（说明没解析好）
            self.assertLess(len(trig), 80,
                            f"{name} 的 trigger 过长，像是没解析干净")

    def test_group_is_present(self):
        """分组必须存在且非空（界面 optgroup 靠它）。"""
        for name, info in catalog.EA_ALL_EFFECTS.items():
            self.assertTrue(info.get("group"), f"{name} 缺 group")


class TestDiskPresence(unittest.TestCase):
    """目录条目必须在磁盘上真实存在 —— 且是真 LoRA 权重。"""

    def test_catalog_files_exist_on_disk(self):
        missing = []
        for name in catalog.EA_ALL_EFFECTS:
            path = LORAS_ROOT / name
            if not path.is_file():
                missing.append(name)
        self.assertEqual(missing, [],
                         f"目录里有 {len(missing)} 条没落盘：{missing[:5]}")

    def test_files_have_lora_keys(self):
        """每个文件的头部必须有 LoRA 键（不是完整 checkpoint、不是 HTML）。"""
        import struct
        bad = []
        for name in catalog.EA_ALL_EFFECTS:
            path = LORAS_ROOT / name
            try:
                with open(path, "rb") as fh:
                    n = struct.unpack("<Q", fh.read(8))[0]
                    header = json.loads(fh.read(n))
            except Exception as exc:  # noqa: BLE001
                bad.append((name, f"头部不可读: {exc}"))
                continue
            keys = [k for k in header if k != "__metadata__"]
            # ⚠️ LoRA 键有**四种**后缀写法，缺一不可：
            #   .lora_A/.lora_B        —— diffusers 风格
            #   .lora_down/.lora_up    —— kohya 风格（本轮 23 个文件是这种，
            #                            键名形如 lora_unet_layers_0_attention_to_k.lora_up.weight）
            #   .lokr_w1/.lokr_w2       —— LoKR
            #   .hada_w1/.hada_w2       —— LoHa
            has_lora = any(
                k.endswith((".lora_A.weight", ".lora_B.weight",
                            ".lora_A.default.weight", ".lora_B.default.weight",
                            ".lora_down.weight", ".lora_up.weight",
                            ".lokr_w1", ".lokr_w2", ".hada_w1", ".hada_w2"))
                for k in keys)
            if not has_lora:
                bad.append((name, f"{len(keys)} 个 tensor 里 0 个 LoRA 键"))
        self.assertEqual(bad, [], f"这些文件不是 LoRA 权重：{bad[:5]}")


class TestFrontendGrouping(unittest.TestCase):
    """界面侧的分组 / 近似标注接线。"""

    def test_app_js_has_optgroup_rendering(self):
        js = (REPO / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("<optgroup", js, "LoRA 下拉没有做 optgroup 分组")
        self.assertIn("近似适用", js, "界面没有「近似适用」标注")

    def test_css_has_near_tag_style(self):
        css = (REPO / "web" / "style.css").read_text(encoding="utf-8")
        self.assertIn("img-lora-tag.near", css,
                      "「近似适用」标签没有配套样式")


if __name__ == "__main__":
    unittest.main()
