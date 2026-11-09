# -*- coding: utf-8 -*-
"""米哈游（HoYoverse）效果 LoRA 目录接入的回归测试。

锁死本轮接入挖出并写死的**真事实**（不测它们就会回归）：

1. **目录里的每一条都真的在本机磁盘上** —— 目录与磁盘不脱节
   （调研清单里 22 条因 Civitai 登录墙 / 魔搭申请制没下下来，
   那些**不能**出现在目录里）。

2. **每条都是真 LoRA 权重，不是 Civitai 登录页 HTML**
   —— safetensors 头部可解析 + 含 LoRA 键。

3. **触发词只来自权威来源**（权重头部的 ss_tag_frequency /
   trigger_word / trigger，或上游模型卡标注）——
   上游没标的必须留空并注明「触发词未标注」，**不编造**。

4. **推荐强度的依据必须写清** —— 作者明示值优先；
   上游没给区间的一律 1.0 且注明「作者未给」。

5. **族归属唯一真相源是目录前缀** —— 目录前缀 ↔ base 字段一致。

6. **加速 / 蒸馏类绝不进目录** —— 按显式 kind 声明，不靠文件名启发式。

7. **语音音色 LoRA 绝不混入生图目录** —— 键里不能有
   base_lm.* / residual_lm.*。

8. **编号不撞车** —— plan_id 在全库（既有 78 条 + 本轮 46 条）唯一。

运行：/home/ubuntu/comfyui/venv/bin/python -m pytest tests/test_lora_mihoyo.py -v
"""
import json
import re
import struct
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import lora_catalog_effects as base_catalog   # noqa: E402
from h3studio import lora_catalog_mihoyo as catalog          # noqa: E402
from h3studio import loras as lora_registry                  # noqa: E402

LORAS_ROOT = Path("/mnt/usb2t/comfyui-models/loras")
FAM_PREFIX = {"Z-Image": "z-image-effects/",
               "Qwen-Image 2.1": "qwen21-effects/",
               "MOSS-Image": "moss-effects/"}
LORA_KEY_MARKERS = ("lora_A", "lora_B", "lokr_", "hada_", "lora_down", "lora_up")


def read_safetensors_header(path: Path):
    with open(path, "rb") as f:
        head = f.read(8)
        n = struct.unpack("<Q", head)[0]
        raw = f.read(n)
    return json.loads(raw.decode("utf-8", "replace"))


class TestMihoyoCatalogOnDisk(unittest.TestCase):
    """目录里的每一条都必须真的落在磁盘上。"""

    def test_every_entry_exists_on_disk(self):
        missing = []
        for key in catalog.ALL_MIHOYO_EFFECTS:
            path = LORAS_ROOT / key
            if not path.is_file():
                missing.append(key)
        self.assertFalse(missing, f"目录里有 {len(missing)} 条本机不存在：{missing[:5]}")

    def test_every_entry_is_a_real_lora_file(self):
        """头部可解析 + 含 LoRA 键（排除 Civitai 登录页 HTML）。"""
        bad = []
        for key, info in catalog.ALL_MIHOYO_EFFECTS.items():
            path = LORAS_ROOT / key
            if not path.is_file():
                continue
            with open(path, "rb") as f:
                head = f.read(10)
            if len(head) < 10 or head[8:9] != b"{":
                bad.append((key, "not_safetensors"))
                continue
            hdr = read_safetensors_header(path)
            keys = [k for k in hdr if k != "__metadata__"]
            if not any(m in " ".join(keys) for m in LORA_KEY_MARKERS):
                bad.append((key, "no_lora_keys"))
        self.assertFalse(bad, f"以下条目不是真 LoRA 权重：{bad[:5]}")

    def test_no_voice_tts_lora_in_catalog(self):
        """语音音色 LoRA（base_lm.* / residual_lm.*）绝不混进生图目录。"""
        offenders = []
        for key in catalog.ALL_MIHOYO_EFFECTS:
            path = LORAS_ROOT / key
            if not path.is_file():
                continue
            hdr = read_safetensors_header(path)
            for k in hdr:
                if k.startswith(("base_lm.", "residual_lm.")):
                    offenders.append(key)
                    break
        self.assertFalse(offenders, f"目录里混进了语音音色 LoRA：{offenders}")

    def test_entries_are_not_zero_byte(self):
        zero = [k for k in catalog.ALL_MIHOYO_EFFECTS
                if (LORAS_ROOT / k).is_file()
                and (LORAS_ROOT / k).stat().st_size == 0]
        self.assertFalse(zero, f"目录里有 0 字节文件：{zero}")


class TestMihoyoCatalogFields(unittest.TestCase):
    """字段完整性 —— 界面靠这些字段显示。"""

    def test_every_entry_has_all_fields(self):
        for key, info in catalog.ALL_MIHOYO_EFFECTS.items():
            self.assertTrue(info.get("display"), f"{key} 缺 display")
            self.assertTrue(info.get("scene"), f"{key} 缺 scene")
            self.assertTrue(info.get("source"), f"{key} 缺 source")
            self.assertTrue(info.get("strength_source"),
                            f"{key} 缺 strength_source")
            self.assertTrue(info.get("plan_id"), f"{key} 缺 plan_id")
            self.assertIn("trigger", info, f"{key} 缺 trigger 字段")
            self.assertIsInstance(info.get("recommended_strength"), (int, float),
                                  f"{key} 的 recommended_strength 不是数值")
            self.assertGreater(info["recommended_strength"], 0,
                               f"{key} 的推荐强度必须 > 0")
            self.assertLessEqual(info["recommended_strength"], 1.5,
                                 f"{key} 的推荐强度超出滑块量程 0-1.5")

    def test_prefix_matches_base_field(self):
        """族归属唯一真相源是目录前缀 —— 前缀 ↔ base 必须一致。"""
        for key, info in catalog.ALL_MIHOYO_EFFECTS.items():
            prefix = key.split("/")[0] + "/"
            expected = next((base for base, pfx in FAM_PREFIX.items()
                             if pfx == prefix), None)
            self.assertIsNotNone(expected, f"{key} 的目录前缀不在三族前缀表里")
            self.assertEqual(info["base"], expected,
                             f"{key} 的 base={info['base']} 与前缀 {prefix} 不符")

    def test_plan_ids_unique_against_whole_library(self):
        """编号在全库唯一（既有 78 条 + 本轮 46 条）。"""
        existing = [i.get("plan_id") for i in base_catalog.ALL_EFFECTS.values()]
        mine = [i.get("plan_id") for i in catalog.ALL_MIHOYO_EFFECTS.values()]
        self.assertEqual(len(mine), len(set(mine)),
                         f"本轮编号有重复：{[x for x in mine if mine.count(x) > 1]}")
        clash = set(mine) & set(existing)
        self.assertFalse(clash, f"与既有目录编号冲突：{clash}")

    def test_plan_ids_continue_existing_numbering(self):
        """本轮编号必须接着既有编号往下排（Z-33+ / Q-22+ / M-28+）。"""
        def nums(ids):
            return sorted(int(i.split("-")[1]) for i in ids if i)
        base_ids = [i.get("plan_id") for i in base_catalog.ALL_EFFECTS.values()]
        for base, prefix, start in (("Z-Image", "Z", 33),
                                    ("Qwen-Image 2.1", "Q", 22),
                                    ("MOSS-Image", "M", 28)):
            base_nums = nums([i for i in base_ids if i and i.startswith(prefix + "-")])
            my_nums = nums([v["plan_id"] for v in catalog.ALL_MIHOYO_EFFECTS.values()
                            if v["base"] == base])
            if base_nums:
                self.assertGreaterEqual(min(my_nums), max(base_nums) + 1,
                                        f"{base} 的本轮编号与既有编号重叠")
                self.assertEqual(min(my_nums), start,
                                 f"{base} 的本轮起始编号应为 {start}")
            self.assertEqual(my_nums, list(range(start, start + len(my_nums))),
                             f"{base} 的本轮编号不连续")


class TestMihoyoTriggerDiscipline(unittest.TestCase):
    """触发词纪律 —— 上游没标的不许编。"""

    def test_empty_trigger_is_labelled_as_unannotated(self):
        """触发词留空的条目，scene 里必须注明「触发词未标注」。"""
        for key, info in catalog.ALL_MIHOYO_EFFECTS.items():
            if info["trigger"]:
                continue
            self.assertIn("触发词未标注", info["scene"],
                          f"{key} 触发词为空却没注明「触发词未标注」")

    def test_trigger_is_not_a_placeholder(self):
        """触发词不能是空串/三引号/None 这类占位符。"""
        for key, info in catalog.ALL_MIHOYO_EFFECTS.items():
            trig = info["trigger"]
            self.assertIsInstance(trig, str, f"{key} 的 trigger 不是字符串")
            self.assertNotIn(trig, ('"""', "'''", '""', "''", "null", "None"),
                             f"{key} 的 trigger 是占位符：{trig!r}")

    def test_trigger_matches_weight_header(self):
        """有触发词的条目，触发词必须能在权重头部找到出处。"""
        checked = 0
        for key, info in catalog.ALL_MIHOYO_EFFECTS.items():
            trig = info["trigger"]
            if not trig:
                continue
            path = LORAS_ROOT / key
            if not path.is_file():
                continue
            hdr = read_safetensors_header(path)
            meta = hdr.get("__metadata__") or {}
            blob = " ".join(str(v) for v in meta.values()) + " " + \
                str(meta.get("ss_tag_frequency", ""))
            norm = trig.replace(" ", "").replace("，", "").replace(",", "")
            found = any(tok and tok.replace(" ", "") in blob.replace(" ", "")
                        for tok in re.split(r"[/,，、\s]+", trig) if tok)
            if not found and norm.lower() not in blob.replace(" ", "").lower():
                # 头部没有出处 -> 必须在上游模型卡标注里（scene 注明来源）
                self.assertIn("触发词来源：上游模型卡",
                              info["scene"],
                              f"{key} 的触发词 {trig!r} 在权重头部与场景说明里都无出处")
            checked += 1
        self.assertGreater(checked, 0, "没有任何条目可校验触发词出处")


class TestMihoyoStrengthDiscipline(unittest.TestCase):
    """推荐强度纪律 —— 作者明示值优先，依据必须写清。"""

    def test_strength_source_states_the_basis(self):
        for key, info in catalog.ALL_MIHOYO_EFFECTS.items():
            src = info["strength_source"]
            self.assertTrue(src, f"{key} 的 strength_source 为空")
            self.assertTrue(
                ("作者明示" in src) or ("未给" in src) or ("中值" in src)
                or ("超出" in src),
                f"{key} 的 strength_source 没写明依据：{src}")

    def test_default_strength_is_one_only_when_author_silent(self):
        """作者没给区间 -> 强度 1.0 且依据写明「未给」。

        反向也要成立：**作者明示 1.0 是合法值**（强度刚好等于默认），
        不能因此要求依据里出现「未给」。
        """
        for key, info in catalog.ALL_MIHOYO_EFFECTS.items():
            src = info["strength_source"]
            if "未给" in src:
                self.assertEqual(info["recommended_strength"], 1.0,
                                 f"{key} 依据说作者未给，强度却不是 1.0："
                                 f"{info['recommended_strength']}")
            else:
                self.assertIn("作者明示", src,
                              f"{key} 的强度既非作者明示也非作者未给：{src}")

    def test_no_strength_exceeds_slider_range(self):
        for key, info in catalog.ALL_MIHOYO_EFFECTS.items():
            self.assertLessEqual(info["recommended_strength"], 1.5,
                                 f"{key} 的推荐强度超出滑块量程")


class TestMihoyoKindAndFamily(unittest.TestCase):
    """分类与族隔离。"""

    def test_all_entries_are_effect_kind(self):
        for key, info in catalog.ALL_MIHOYO_EFFECTS.items():
            self.assertEqual(info.get("kind"), lora_registry.KIND_EFFECT,
                             f"{key} 的 kind 不是 effect")
            self.assertEqual(lora_registry.classify_kind(key, info),
                             lora_registry.KIND_EFFECT,
                             f"{key} 被判成了加速类")

    def test_effect_family_of_recognizes_entries(self):
        for key in catalog.ALL_MIHOYO_EFFECTS:
            self.assertIn(lora_registry.effect_family_of(key),
                          ("Z-Image", "Qwen-Image 2.1", "MOSS-Image"),
                          f"{key} 的族前缀没被识别")

    def test_catalog_is_registered_in_registry(self):
        """注册表里必须能查到每一条（接入真的生效）。"""
        for key in catalog.ALL_MIHOYO_EFFECTS:
            self.assertIn(key, lora_registry.LORA_CATALOG,
                          f"{key} 没进 LORA_CATALOG")

    def test_registered_entries_carry_plan_id(self):
        for key in catalog.ALL_MIHOYO_EFFECTS:
            self.assertEqual(lora_registry.LORA_CATALOG[key].get("plan_id"),
                             catalog.ALL_MIHOYO_EFFECTS[key]["plan_id"],
                             f"{key} 注册表里的 plan_id 与目录不一致")


class TestMihoyoVisibleInFamilyList(unittest.TestCase):
    """本机已安装的米哈游 LoRA 必须出现在对应族的面板清单里。"""

    def _list_for(self, family):
        if family == "Z-Image":
            return lora_registry.list_z_image_loras()
        if family == "Qwen-Image 2.1":
            return lora_registry.list_qwen_image_loras()
        return lora_registry.list_moss_image_loras()

    def test_installed_entries_are_visible(self):
        seen = 0
        for family in ("Z-Image", "Qwen-Image 2.1", "MOSS-Image"):
            names = {item["name"] for item in self._list_for(family)}
            for key, info in catalog.ALL_MIHOYO_EFFECTS.items():
                if info["base"] != family:
                    continue
                if not (LORAS_ROOT / key).is_file():
                    continue
                self.assertIn(key, names,
                              f"{key} 本机已安装却没出现在 {family} 清单里")
                seen += 1
        self.assertGreater(seen, 0, "一条都没在清单里")

    def test_installed_entries_are_compatible(self):
        """已安装的米哈游 LoRA 不能被判成不兼容（否则用户选不到）。"""
        for family in ("Z-Image", "Qwen-Image 2.1", "MOSS-Image"):
            for item in self._list_for(family):
                if item["name"] not in catalog.ALL_MIHOYO_EFFECTS:
                    continue
                self.assertTrue(item.get("compatible", True),
                                f"{item['name']} 被判不兼容："
                                f"{item.get('incompatible_reason', '')}")


if __name__ == "__main__":
    unittest.main()
