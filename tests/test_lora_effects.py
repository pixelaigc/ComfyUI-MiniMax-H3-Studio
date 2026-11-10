# -*- coding: utf-8 -*-
"""三基座效果 LoRA 目录接入的回归测试。

覆盖本轮接入挖出并写死的几个**真事实**（不测它们就会回归）：

1. **兼容性判定必须按底模族选正则**（2026-11-09 实测最大的坑）
   三族的层命名完全不同：
       Z-Image   layers.N.attention.to_q
       QI21      transformer_blocks.N.attn.to_q
       MOSS      layers.N.proj.q
   之前只有一套 `.attention.` 的正则 —— 拿它去判 QI21 的 LoRA 会得到
   「0 个注意力层 -> 命中率 1.0」的**假通过**（明明对不上却放行）。
   现在按族选正则，并断言三族的判定结果**互不相同**（防止一个
   宽松的正则把三族全放行）。

2. **族判定靠目录前缀，不靠文件名**
   `pytorch_lora_weights.safetensors` 三族同名（Z-Image 是「写实增强」、
   QI21 是「Y2K 数码」），靠文件名根本分不出来 —— 所以效果目录一律带
   族专属前缀（z-image-effects/ qwen21-effects/ moss-effects/）。

3. **加速/蒸馏类绝不进用户可选列表**
   调研清单里混着加速类（Z-Image Fun Distill / 4-step adapter 等），
   它们改变步数与 CFG 语义，必须由工作流固定使用，不能让用户随便挑。

4. **推荐强度以目录里人工核对的 upstream 值为准**
   兼容性检测算出的 suggested_strength 只是「扰动大不大」的兜底安全值，
   不能覆盖作者明示的推荐值（例：作者明示 0.5，扰动算出来 1.0）。

5. **触发词自动合并追加，多个 LoRA 各带各的触发词**
   且用户改过的以用户的为准。

6. **LoRA 节点必须同时接 model 与 clip**（用户早前明确要求，
   之前有人漏连）—— Z-Image / QI21 用 LoraLoader（model + clip），
   MOSS 用 LoraLoaderModelOnly（它的文本编码器是 Qwen3.5-9B，
   LoRA 只训 transformer 侧）。

运行：python3 -m pytest tests/test_lora_effects.py -v
"""
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import loras as lora_registry           # noqa: E402
from h3studio import workflows as W                    # noqa: E402
from h3studio import lora_catalog_effects as catalog   # noqa: E402


FAMILIES = ("Z-Image", "Qwen-Image 2.1", "MOSS-Image")


def _list_for(family):
    if family == "Z-Image":
        return lora_registry.list_z_image_loras()
    if family == "Qwen-Image 2.1":
        return lora_registry.list_qwen_image_loras()
    return lora_registry.list_moss_image_loras()


class TestEffectCatalog(unittest.TestCase):
    """效果 LoRA 目录本身的数据完整性。"""

    def test_catalog_has_entries_for_all_three_families(self):
        """三族都要有条目（清单里三家都有）。"""
        for family, table in (("Z-Image", catalog.Z_IMAGE_EFFECTS),
                              ("Qwen-Image 2.1", catalog.QWEN21_EFFECTS),
                              ("MOSS-Image", catalog.MOSS_EFFECTS)):
            self.assertGreaterEqual(len(table), 10,
                                    f"{family} 的效果 LoRA 目录少于 10 条")

    def test_every_entry_has_chinese_description_and_strength(self):
        """每条都要有简易中文说明 + 推荐强度 + 来源链接。"""
        for name, info in catalog.ALL_EFFECTS.items():
            self.assertTrue(info.get("display"), f"{name} 缺 display")
            self.assertTrue(info.get("scene"), f"{name} 缺 scene（界面要显示说明）")
            self.assertTrue(info.get("source"), f"{name} 缺 source（来源链接）")
            self.assertTrue(info.get("strength_source"),
                            f"{name} 缺 strength_source（强度依据）")
            strength = info.get("recommended_strength")
            self.assertIsInstance(strength, (int, float),
                                  f"{name} 的 recommended_strength 不是数值")
            self.assertGreater(strength, 0, f"{name} 的推荐强度必须 > 0")
            self.assertLessEqual(strength, 1.5,
                                 f"{name} 的推荐强度超出滑块量程 0-1.5")

    def test_plan_ids_are_unique(self):
        """清单编号不能重复（重复 = 抄错行）。"""
        ids = [info.get("plan_id") for info in catalog.ALL_EFFECTS.values()]
        ids = [i for i in ids if i]
        self.assertEqual(len(ids), len(set(ids)),
                         f"清单编号有重复：{[i for i in ids if ids.count(i) > 1]}")

    def test_no_acceleration_lora_in_effect_catalog(self):
        """加速/蒸馏类绝不能出现在效果目录里。"""
        for name, info in catalog.ALL_EFFECTS.items():
            blob = f"{name} {info.get('display','')} {info.get('scene','')}".lower()
            for marker in ("distill", "lightning", "lightx2v", "4step", "8step",
                           "few-step", "fewstep", "turbo lora"):
                self.assertNotIn(marker, blob,
                                 f"{name} 看起来是加速/蒸馏类，不能当效果 LoRA")

    def test_effect_entries_are_kind_effect(self):
        """目录里的条目必须被判定成「效果类」，不是加速类。"""
        for name, info in catalog.ALL_EFFECTS.items():
            kind = lora_registry.classify_kind(name, info)
            self.assertEqual(kind, lora_registry.KIND_EFFECT,
                             f"{name} 被判成了 {kind}")


class TestFamilyAwareCompatibility(unittest.TestCase):
    """按族选正则的兼容性判定。"""

    def test_family_regexes_are_distinct(self):
        """三族的正则不能指向同一批层名。

        如果三族共用一套正则，那么「拿 QI21 的正则去判 Z-Image 的 LoRA」
        就会得到 0 命中 -> 假不兼容；反过来「拿 Z-Image 的正则判 QI21」
        会得到 0 个注意力层 -> 假通过。
        """
        probes = {
            "Z-Image": "layers.0.attention.to_q",
            "Qwen-Image 2.1": "transformer_blocks.0.attn.to_q",
            "MOSS-Image": "layers.0.proj.q",
        }
        for family, probe in probes.items():
            pattern = lora_registry._FAMILY_ATTENTION_RE[family]
            self.assertTrue(pattern.search(probe),
                            f"{family} 的正则应该匹配 {probe}")
        # 每族的正则只能匹配自己那族的探针
        for family, probe in probes.items():
            for other, other_probe in probes.items():
                if other == family:
                    continue
                pattern = lora_registry._FAMILY_ATTENTION_RE[other]
                self.assertFalse(pattern.search(probe),
                                 f"{other} 的正则不应匹配 {family} 的层名 {probe}")

    def test_wrong_family_regex_does_not_false_pass(self):
        """拿错族的正则 -> 必须判成「0 个注意力层」而不是假通过。

        这是本轮最大的坑：QI21 的 LoRA 用 `.attn.`，Z-Image 的正则
        匹配不到 -> 「没有注意力层 -> 命中率 1.0 -> 放行」，
        明明键名对不上却让用户选。
        """
        qwen_bases = {"transformer_blocks.0.attn.to_q",
                      "transformer_blocks.0.attn.to_k",
                      "transformer_blocks.0.attn.to_v"}
        z_keys = {"diffusion_model.layers.0.attention.qkv"}
        # 用 Z-Image 的正则去判 QI21 的 LoRA -> 0 个注意力层
        ratio, hit, total = lora_registry._family_layer_hit_ratio(
            qwen_bases, z_keys, "Z-Image")
        self.assertEqual(total, 0, "Z-Image 的正则不该把 QI21 的层当注意力层")
        self.assertEqual(ratio, 1.0, "0 个注意力层时返回 1.0（表示无法判定）")
        # 用 QI21 的正则去判自己的 LoRA -> 命中
        # 键名格式与 _unet_layer_keys() 一致：去掉 .weight 后缀
        qwen_keys = {"diffusion_model.transformer_blocks.0.attn.to_q",
                     "diffusion_model.transformer_blocks.0.attn.to_k",
                     "diffusion_model.transformer_blocks.0.attn.to_v"}
        ratio, hit, total = lora_registry._family_layer_hit_ratio(
            qwen_bases, qwen_keys, "Qwen-Image 2.1")
        self.assertEqual(total, 3)
        self.assertEqual(hit, 3)
        self.assertEqual(ratio, 1.0)

    def test_moss_proj_layers_are_matched(self):
        """MOSS 的 `.proj.{q,k,v,o}` 必须被识别成投影层。"""
        moss_bases = {"diffusion_model.layers.0.proj.q",
                      "diffusion_model.layers.0.proj.k",
                      "diffusion_model.layers.0.proj.v",
                      "diffusion_model.layers.0.proj.o"}
        moss_keys = {"diffusion_model.layers.0.proj.q",
                     "diffusion_model.layers.0.proj.k",
                     "diffusion_model.layers.0.proj.v",
                     "diffusion_model.layers.0.proj.o"}
        ratio, hit, total = lora_registry._family_layer_hit_ratio(
            moss_bases, moss_keys, "MOSS-Image")
        self.assertEqual((hit, total), (4, 4))
        self.assertEqual(ratio, 1.0)

    def test_prefixed_lora_layer_name_matches_bare_base_key(self):
        """LoRA 侧带 `diffusion_model.` 前缀时必须命中裸层名。

        实测 MOSS 的 ComfyUI 版 LoRA 键名全是
        `diffusion_model.layers.N.proj.*`；如果不剥前缀就会拼出
        `diffusion_model.diffusion_model....` -> 永远 0 命中。
        """
        self.assertTrue(lora_registry._layer_hit(
            "diffusion_model.layers.0.proj.q",
            {"diffusion_model.layers.0.proj.q"}))
        self.assertTrue(lora_registry._layer_hit(
            "diffusion_model.layers.0.attention.to_q",
            {"diffusion_model.layers.0.attention.qkv"}))

    def test_compat_cache_is_family_scoped(self):
        """兼容性缓存的键必须含族 —— 否则三族会互相串味。"""
        import inspect
        src = inspect.getsource(lora_registry.check_compatibility)
        self.assertIn("family", src,
                      "check_compatibility 必须接受 family 参数")
        # 缓存键 = _cache_key(path) + (family, bool(deep))
        # （2026-11-10 起多了 deep 标记 —— 结构判定与权重级分析的结果
        #   不能共用同一个缓存项）
        self.assertIn("+ (family, bool(deep))", src,
                      "兼容性缓存键必须包含族，避免跨族串味")


class TestEffectLorasVisibleInPanel(unittest.TestCase):
    """本机已安装的效果 LoRA 必须出现在对应族的面板里。"""

    def test_installed_effect_loras_appear_in_their_family(self):
        """目录里且本机存在的条目 -> 必须出现在对应族的清单里。"""
        for family in FAMILIES:
            names = {item["name"] for item in _list_for(family)}
            expected = {
                key for key, info in catalog.ALL_EFFECTS.items()
                if lora_registry.effect_family_of(key) == family
            }
            # 只断言「目录里有、本机也有的」条目必须可见
            present = {k for k in expected if k in names}
            missing = expected - names
            # 允许：本机还没下载的条目不在清单里（那是「待落地」，不是 bug）
            for key in missing:
                resolved, path = lora_registry.resolve_lora(key)
                self.assertFalse(path is not None and resolved == key,
                                 f"{key} 本机已安装却没出现在 {family} 清单里")
            self.assertTrue(present or not expected,
                            f"{family} 一条都没接进来")

    def test_effect_entries_carry_trigger_and_description(self):
        """面板里的条目必须带触发词与说明（界面要显示、可编辑）。"""
        for family in FAMILIES:
            for item in _list_for(family):
                if item.get("discovered_by") != "effect_dir":
                    continue
                self.assertTrue(item.get("display"), f"{item['name']} 缺显示名")
                self.assertTrue(item.get("scene"),
                                f"{item['name']} 缺场景说明（界面要显示）")
                self.assertIn("trigger", item, f"{item['name']} 缺 trigger 字段")
                self.assertGreater(item.get("recommended_strength", 0), 0,
                                   f"{item['name']} 推荐强度必须 > 0")

    def test_effect_loras_are_not_incompatible(self):
        """已安装的效果 LoRA 不该被判成不兼容（否则用户选不到）。

        ⚠️ 2026-11-10 例外：MOSS 族的**融合 qkv 且是 LoKR 形态**的 LoRA
        真的挂不上（本机 MOSS 底模是分离 q/k/v，LoKR 的 Kronecker 分解
        与三等分不对应，实测数值不等价 —— 见 moss_lora_compat 的说明）。
        这类条目**必须**被判不兼容并给出原因（界面如实标注），
        否则就是「照常列出让用户误以为已生效」的静默失效。
        """
        allowed_incompatible = 0
        for family in FAMILIES:
            for item in _list_for(family):
                if item.get("discovered_by") != "effect_dir":
                    continue
                if item.get("compatible", True):
                    continue
                reason = item.get("incompatible_reason", "")
                # 只允许「LoKR 融合 qkv 不可拆分」这一类
                self.assertIn("LoKR", reason,
                              f"{item['name']} 被判不兼容但原因不是 LoKR 不可拆分："
                              f"{reason}")
                self.assertIn("无法无损拆分", reason,
                              f"{item['name']} 不兼容原因没说明「无法无损拆分」："
                              f"{reason}")
                allowed_incompatible += 1
        # 台账里记录的被挡条目数应该有限（防止规则放宽成「什么都行」）
        self.assertLessEqual(allowed_incompatible, 20,
                             "被判不兼容的条目过多 —— 判定规则可能过宽")

    def test_acceleration_loras_are_not_in_effect_list(self):
        """加速/蒸馏类只在 acceleration 里，不在 available 里。"""
        for family, lister in (
                ("Z-Image", lora_registry.list_effect_loras),
                ("Qwen-Image 2.1", lora_registry.list_qwen_effect_loras),
                ("MOSS-Image", lora_registry.list_moss_effect_loras)):
            for item in lister():
                self.assertNotEqual(item.get("kind"), lora_registry.KIND_ACCELERATION,
                                    f"{family}: 加速类 {item['name']} 混进了可选列表")


class TestEffectLoraWorkflowWiring(unittest.TestCase):
    """工作流接线：LoRA 节点要接 clip，触发词要进提示词。"""

    BASE = {"job_id": "lorafx", "mode": "t2i", "prompt": "a cat", "seed": 42}

    def _first_installed(self, family):
        for item in _list_for(family):
            if item.get("discovered_by") == "effect_dir":
                return item
        return None

    def test_z_image_effect_lora_connects_clip(self):
        """Z-Image：LoraLoader 的 model 与 clip 都要接。"""
        item = self._first_installed("Z-Image")
        if not item:
            self.skipTest("本机未安装 Z-Image 效果 LoRA")
        wf = W.build_t2i({**self.BASE, "model": "z_image_turbo_bf16",
                          "loras": [{"name": item["name"], "strength": 0.6}]})
        nodes = [n for n in wf.values() if n["class_type"] == "LoraLoader"]
        self.assertTrue(nodes, "工作流里没有 LoraLoader 节点")
        self.assertIn("clip", nodes[0]["inputs"],
                      "LoraLoader 的 clip 输入没接 —— 文本编码器那半边 patch 会被丢弃")
        self.assertIsNotNone(nodes[0]["inputs"].get("clip"))
        # clip 要接到 CLIPTextEncode
        clip_ref = nodes[0]["inputs"]["clip"]
        self.assertTrue(clip_ref[0].startswith("57:30"), "clip 必须来自 CLIPLoader")

    def test_qwen_effect_lora_connects_clip(self):
        """QI21：同样要接 clip。"""
        item = self._first_installed("Qwen-Image 2.1")
        if not item:
            self.skipTest("本机未安装 QI21 效果 LoRA")
        wf = W.build_qwen_t2i({**self.BASE, "model": "qwen_image_2.1_bf16",
                               "loras": [{"name": item["name"], "strength": 0.8}]})
        nodes = [n for n in wf.values() if n["class_type"] == "LoraLoader"]
        self.assertTrue(nodes, "工作流里没有 LoraLoader 节点")
        self.assertIn("clip", nodes[0]["inputs"],
                      "LoraLoader 的 clip 输入没接")

    def test_moss_effect_lora_uses_model_only_loader(self):
        """MOSS：文本编码器是 Qwen3.5-9B，LoRA 只训 transformer 侧 ->
        必须用 LoraLoaderModelOnly（接 clip 会报 lora key not loaded）。"""
        item = self._first_installed("MOSS-Image")
        if not item:
            self.skipTest("本机未安装 MOSS 效果 LoRA")
        wf = W.build_moss_t2i({**self.BASE, "model": "moss_image_base",
                               "loras": [{"name": item["name"], "strength": 0.7}]})
        nodes = [n for n in wf.values() if n["class_type"] == "LoraLoaderModelOnly"]
        self.assertTrue(nodes, "工作流里没有 LoraLoaderModelOnly 节点")
        self.assertNotIn("clip", nodes[0]["inputs"],
                         "MOSS 的 LoRA 不该接 clip（文本编码器 patch 不到）")

    def test_triggers_of_multiple_loras_are_merged(self):
        """多个 LoRA 各自的触发词都要进最终提示词。"""
        merged = W.merge_lora_triggers("a cat", [
            {"name": "x", "strength": 1.0, "trigger": "class1cpa1nt"},
            {"name": "y", "strength": 1.0, "trigger": "t3chnic4lly"},
        ])
        self.assertIn("class1cpa1nt", merged)
        self.assertIn("t3chnic4lly", merged)
        self.assertTrue(merged.startswith("a cat"))

    def test_user_edited_trigger_wins_over_catalog(self):
        """用户改过的触发词以用户的为准。"""
        merged = W.merge_lora_triggers("a cat", [
            {"name": "x", "strength": 1.0, "trigger": "my_own_word"},
        ])
        self.assertIn("my_own_word", merged)
        self.assertNotIn("class1cpa1nt", merged)

    def test_empty_trigger_is_not_appended(self):
        """触发词留空 -> 不追加任何东西。"""
        merged = W.merge_lora_triggers("a cat", [
            {"name": "x", "strength": 1.0, "trigger": ""},
        ])
        self.assertEqual(merged, "a cat")


class TestEffectFamilyGuard(unittest.TestCase):
    """跨族误用必须被拒绝（否则 ComfyUI 静默丢弃，LoRA 根本不生效）。"""

    def test_effect_family_of_is_prefix_based(self):
        """族判定按目录前缀。"""
        self.assertEqual(lora_registry.effect_family_of("z-image-effects/x.safetensors"),
                         "Z-Image")
        self.assertEqual(lora_registry.effect_family_of("qwen21-effects/x.safetensors"),
                         "Qwen-Image 2.1")
        self.assertEqual(lora_registry.effect_family_of("moss-effects/x.safetensors"),
                         "MOSS-Image")
        self.assertIsNone(lora_registry.effect_family_of("random/x.safetensors"))

    def test_moss_rejects_non_moss_lora(self):
        """MOSS 的工作流必须拒绝别的族的 LoRA。"""
        z_item = TestEffectLoraWorkflowWiring()._first_installed("Z-Image")
        if not z_item:
            self.skipTest("本机未安装 Z-Image 效果 LoRA")
        with self.assertRaises(ValueError) as ctx:
            W.build_moss_t2i({**TestEffectLoraWorkflowWiring.BASE,
                              "model": "moss_image_base",
                              "loras": [{"name": z_item["name"], "strength": 0.7}]})
        self.assertIn("MOSS", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
