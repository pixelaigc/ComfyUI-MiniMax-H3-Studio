# -*- coding: utf-8 -*-
"""米哈游**角色 / 机体**增量批次的回归测试。

首批（画风 + 场景）的测试在 test_lora_mihoyo.py；这里只锁死
增量批次**特有**的坑 —— 每一条都是本轮实测踩出来的：

1. **基座实为 Qwen-Image 1 代的条目不得进 2.1 族目录**
   —— 权重元数据 ss_base_model_version=qwen_image、层名 add_q_proj，
   而 2.1 底模是 to_q（实测注意力层仅 128/240 命中）。
   放进去 ComfyUI 会静默丢弃（只打 "lora key not loaded" WARNING，
   出图照跑但 LoRA 没生效）。

2. **清单的 copyright 字段不可照抄**
   —— 上游给 11 条鸣潮（库洛游戏 IP）条目写的是「角色 IP 归米哈游所有」。
   IP 归属必须按游戏本体判定。

3. **清单的 strength 栏可能填的是触发词串**
   —— 上游把 trainedWords 误填进 strength 栏（如
   "`Cissia (zenless zone zero)`；另含 `3D Modeling Style`"），
   直接正则取数字会得到 3.0 这种荒谬强度。

4. **同一权重不得在目录里出现两次**
   —— 首批已收录的条目被增量再收一次时，Python dict 里后者静默覆盖前者，
   还会在 plan_id 上留一个空洞（实测 M-35 空洞 = firefly26710_Moss）。

5. **编号必须全库连续**（每族从既有基础库之后接排）。

运行：python -m pytest tests/test_lora_mihoyo_char_mech.py -v
"""
import json
import os
import re
import struct
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import lora_catalog_effects as base_catalog   # noqa: E402
from h3studio import lora_catalog_mihoyo as catalog           # noqa: E402
from h3studio import loras as lora_registry                   # noqa: E402

WS = Path("/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957")
LORAS_ROOT = Path("/mnt/usb2t/comfyui-models/loras")
MANIFEST = WS / "mihoyo_lora_catalog_char_mech.json"
EXCLUDED = WS / "mihoyo_lora_excluded_char_mech.json"

# 基座实为 Qwen-Image 1 代的两条（权重自述 ss_base_model_version=qwen_image）
QWEN1_GEN_FILES = {
    "20251117-13973386.safetensors",
    "qwen_image_sethos_lora_v1_000002000.safetensors",
}
# 非米哈游 IP 的游戏 —— 这两类的条目不得声称「IP 归米哈游所有」
NON_MIHOYO_GAMES = {"鸣潮", "通用（非米哈游专有）"}


def _manifest():
    if not MANIFEST.is_file():
        return []
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _excluded():
    if not EXCLUDED.is_file():
        return []
    return json.loads(EXCLUDED.read_text(encoding="utf-8"))


def read_safetensors_header(path: Path):
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        return json.loads(f.read(n).decode("utf-8", "replace"))


class TestQwen1GenExcluded(unittest.TestCase):
    """基座实为 Qwen-Image 1 代的条目不得混进 2.1 族目录。"""

    def test_qwen1_gen_files_not_in_qwen21_family_dir(self):
        for fn in QWEN1_GEN_FILES:
            self.assertFalse(
                (LORAS_ROOT / "qwen21-effects" / fn).exists(),
                f"{fn} 基座实为 Qwen-Image 1 代，仍留在 qwen21-effects/ 里")

    def test_qwen1_gen_files_not_in_catalog(self):
        for fn in QWEN1_GEN_FILES:
            key = f"qwen21-effects/{fn}"
            self.assertNotIn(key, catalog.ALL_MIHOYO_EFFECTS,
                             f"{key} 基座实为 1 代，却被收进了 2.1 目录")

    def test_qwen1_gen_exclusion_is_recorded(self):
        """排除必须留痕 —— 报告要如实列出，不能悄悄消失。"""
        excl = _excluded()
        names = {e["file"] for e in excl}
        self.assertEqual(names, QWEN1_GEN_FILES,
                         f"排除清单与预期不符：{names}")
        for e in excl:
            self.assertTrue(e.get("reason"), f"{e['file']} 的排除原因没写")

    def test_qwen1_gen_layer_names_are_not_qwen21(self):
        """直接验权重层名：1 代是 add_q_proj，2.1 是 to_q。"""
        for fn in QWEN1_GEN_FILES:
            path = LORAS_ROOT / "qwen1-effects" / fn
            if not path.is_file():
                continue
            hdr = read_safetensors_header(path)
            keys = [k for k in hdr if k != "__metadata__"]
            self.assertTrue(any("add_q_proj" in k for k in keys),
                            f"{fn} 里没有 add_q_proj —— 它可能不是 1 代结构")


class TestIpOwnershipByGame(unittest.TestCase):
    """IP 归属按游戏本体判定 —— 不照抄上游写错的 copyright 字段。"""

    def test_non_mihoyo_games_are_not_claimed_as_mihoyo(self):
        for key, info in catalog.ALL_MIHOYO_EFFECTS.items():
            scene = info.get("scene") or ""
            head = scene.split("。")[0]
            if not any(g in head for g in NON_MIHOYO_GAMES):
                continue
            self.assertNotIn("IP 归米哈游所有", scene,
                             f"{key} 是非米哈游 IP，却声称 IP 归米哈游所有")

    def test_every_entry_marks_ip_ownership(self):
        missing = [k for k, v in catalog.ALL_MIHOYO_EFFECTS.items()
                   if "复刻米哈游 IP" not in (v.get("scene") or "")
                   and "非米哈游 IP" not in (v.get("scene") or "")]
        self.assertFalse(missing, f"以下条目没标明 IP 归属：{missing[:5]}")

    def test_mihoyo_entries_note_upstream_rights_differ(self):
        for key, info in catalog.ALL_MIHOYO_EFFECTS.items():
            if "复刻米哈游 IP" not in (info.get("scene") or ""):
                continue
            self.assertIn("原作", info["scene"],
                          f"{key} 复刻米哈游 IP 却没说明原作授权归属")


class TestStrengthNotFromTriggerString(unittest.TestCase):
    """强度栏被上游填成触发词串时，不得从里面取数字。"""

    def test_strength_within_slider_range(self):
        for key, info in catalog.ALL_MIHOYO_EFFECTS.items():
            self.assertLessEqual(info["recommended_strength"], 1.5,
                                 f"{key} 的推荐强度超出滑块量程 0-1.5")
            self.assertGreater(info["recommended_strength"], 0,
                               f"{key} 的推荐强度必须 > 0")

    def test_strength_source_states_basis(self):
        for key, info in catalog.ALL_MIHOYO_EFFECTS.items():
            src = info["strength_source"]
            self.assertTrue(src, f"{key} 的 strength_source 为空")
            self.assertTrue(
                ("作者明示" in src) or ("未给" in src) or ("中值" in src),
                f"{key} 的 strength_source 没写明依据：{src}")

    def test_silent_author_means_strength_one(self):
        for key, info in catalog.ALL_MIHOYO_EFFECTS.items():
            if "未给" in info["strength_source"]:
                self.assertEqual(info["recommended_strength"], 1.0,
                                 f"{key} 依据说作者未给，强度却不是 1.0："
                                 f"{info['recommended_strength']}")

    def test_no_strength_value_invented_beyond_author(self):
        """作者明示区间的条目，取值必须落在该区间内。"""
        man = {m["key"]: m for m in _manifest()}
        for key, m in man.items():
            src = m.get("strength_source") or ""
            nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", src)]
            nums = [n for n in nums if 0 < n <= 3]
            if len(nums) < 2:
                continue
            v = m["recommended_strength"]
            self.assertGreaterEqual(v + 1e-6, min(nums),
                                    f"{key} 推荐强度 {v} 低于作者区间下限 {min(nums)}")
            self.assertLessEqual(v - 1e-6, max(nums),
                                 f"{key} 推荐强度 {v} 高于作者区间上限 {max(nums)}")

    def test_version_number_not_mistaken_for_strength(self):
        """版本号里的数字不是强度。

        实测踩过的坑：`Moss_danica_v2step400_azusa` 的上游 strength 栏写的是
        "v2.0 = 1.0；v1.0 = 0.6–0.8" —— 直接正则取数字再平均会得到
        (2.0 + 1.0) / 2 = 1.5，而作者明说 v2.0 档的强度是 1.0。
        """
        key = "moss-effects/Moss_danica_v2step400_azusa.safetensors"
        info = catalog.ALL_MIHOYO_EFFECTS.get(key)
        self.assertIsNotNone(info, f"{key} 不在目录里")
        self.assertEqual(info["recommended_strength"], 1.0,
                         f"{key} 的推荐强度应为 1.0（作者明示 v2.0 档强度为 1），"
                         f"实际 {info['recommended_strength']}")
        self.assertIn("取 v2.0 档", info["strength_source"],
                      f"{key} 的 strength_source 没写明取的是哪一档："
                      f"{info['strength_source']}")


class TestNoDuplicateKeyInCatalog(unittest.TestCase):
    """同一权重不得在目录文件里出现两次（后者会静默覆盖前者）。"""

    def test_catalog_source_has_no_duplicate_keys(self):
        src = (REPO / "h3studio" / "lora_catalog_mihoyo.py").read_text(encoding="utf-8")
        keys = re.findall(r"^    '([^']+\.safetensors)': \{", src, re.M)
        dup = {k for k in keys if keys.count(k) > 1}
        self.assertFalse(dup, f"目录文件里有重复 key：{dup}")
        self.assertEqual(len(keys), len(catalog.ALL_MIHOYO_EFFECTS),
                         "目录文件里的条目数与导入后的 dict 条目数不一致"
                         "（说明有 key 被覆盖）")

    def test_manifest_entries_are_unique(self):
        keys = [m["key"] for m in _manifest()]
        self.assertEqual(len(keys), len(set(keys)),
                         f"增量清单里有重复 key：{[k for k in keys if keys.count(k) > 1]}")


class TestPlanIdsContinuous(unittest.TestCase):
    """每族编号必须从既有基础库之后接排且连续无洞。"""

    def _nums(self, ids, prefix):
        return sorted(int(i.split("-")[1]) for i in ids
                      if i and re.fullmatch(prefix + r"-\d+", i))

    def test_plan_ids_continuous_per_family(self):
        for base, prefix in (("Z-Image", "Z"), ("Qwen-Image 2.1", "Q"),
                             ("MOSS-Image", "M")):
            mine = self._nums([v["plan_id"] for v in catalog.ALL_MIHOYO_EFFECTS.values()
                                if v["base"] == base], prefix)
            self.assertTrue(mine, f"{base} 一条都没有")
            self.assertEqual(mine, list(range(mine[0], mine[0] + len(mine))),
                             f"{base} 的编号有断档：{mine}")

    def test_plan_ids_start_after_existing_library(self):
        existing = [i.get("plan_id") for i in base_catalog.ALL_EFFECTS.values()]
        for base, prefix in (("Z-Image", "Z"), ("Qwen-Image 2.1", "Q"),
                             ("MOSS-Image", "M")):
            base_nums = self._nums(existing, prefix)
            mine = self._nums([v["plan_id"] for v in catalog.ALL_MIHOYO_EFFECTS.values()
                                if v["base"] == base], prefix)
            if base_nums:
                self.assertGreaterEqual(min(mine), max(base_nums) + 1,
                                        f"{base} 编号与既有目录重叠")

    def test_plan_ids_unique(self):
        ids = [v["plan_id"] for v in catalog.ALL_MIHOYO_EFFECTS.values()]
        self.assertEqual(len(ids), len(set(ids)), "plan_id 有重复")


class TestIncrementalEntriesVisibleAndUsable(unittest.TestCase):
    """增量条目必须在界面清单里可见、可勾选、不被判不兼容。"""

    def _lists(self):
        return {
            "Z-Image": lora_registry.list_z_image_loras(),
            "Qwen-Image 2.1": lora_registry.list_qwen_image_loras(),
            "MOSS-Image": lora_registry.list_moss_image_loras(),
        }

    def test_all_incremental_entries_visible_in_right_family(self):
        lists = self._lists()
        for m in _manifest():
            key = m["key"]
            fam = key.split("/")[0]
            expected = {"z-image-effects": "Z-Image",
                        "qwen21-effects": "Qwen-Image 2.1",
                        "moss-effects": "MOSS-Image"}[fam]
            names = {i["name"] for i in lists[expected]}
            self.assertIn(key, names,
                          f"{key} 没出现在 {expected} 清单里")

    def test_incremental_entries_not_marked_incompatible(self):
        lists = self._lists()
        for m in _manifest():
            key = m["key"]
            for items in lists.values():
                for it in items:
                    if it["name"] != key:
                        continue
                    self.assertTrue(
                        it.get("compatible", True),
                        f"{key} 被判不兼容：{it.get('incompatible_reason', '')}")

    def test_incremental_entries_carry_metadata(self):
        lists = self._lists()
        idx = {}
        for items in lists.values():
            for it in items:
                idx[it["name"]] = it
        for m in _manifest():
            it = idx.get(m["key"])
            if not it:
                continue
            self.assertTrue(it.get("display"), f"{m['key']} 缺 display")
            self.assertTrue(it.get("scene"), f"{m['key']} 缺 scene")
            self.assertTrue(it.get("plan_id"), f"{m['key']} 缺 plan_id")

    def test_manifest_entries_all_exist_on_disk(self):
        for m in _manifest():
            path = LORAS_ROOT / m["key"]
            self.assertTrue(path.is_file(), f"{m['key']} 不在磁盘上")
            self.assertEqual(path.stat().st_size, m["size"],
                             f"{m['key']} 磁盘大小与登记不符")


class TestTriggerDisciplineIncremental(unittest.TestCase):
    """增量条目的触发词纪律：能溯源，不能编。"""

    def test_empty_trigger_is_labelled(self):
        for m in _manifest():
            if m["trigger"]:
                continue
            self.assertIn("触发词未标注", m["scene"],
                          f"{m['key']} 触发词为空却没注明「触发词未标注」")

    def test_trigger_has_source_in_scene(self):
        for m in _manifest():
            if not m["trigger"]:
                continue
            self.assertIn("触发词来源：", m["scene"],
                          f"{m['key']} 有触发词但 scene 里没写来源")

    def test_header_sourced_trigger_exists_in_weight(self):
        """来源是权重头部的触发词，必须真能在头部找到。"""
        for m in _manifest():
            if not m["trigger"] or "权重头部" not in m["trigger_src"]:
                continue
            path = LORAS_ROOT / m["key"]
            if not path.is_file():
                continue
            meta = read_safetensors_header(path).get("__metadata__") or {}
            blob = " ".join(str(v) for v in meta.values()).replace(" ", "")
            norm = m["trigger"].replace(" ", "")
            self.assertIn(norm.lower(), blob.lower(),
                          f"{m['key']} 的触发词 {m['trigger']!r} 在权重头部找不到")


if __name__ == "__main__":
    unittest.main()
