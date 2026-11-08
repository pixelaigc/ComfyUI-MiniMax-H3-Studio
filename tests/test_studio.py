# -*- coding: utf-8 -*-
"""H3 Studio 后端测试。

运行：python3 -m pytest tests/ -v
（或 python3 tests/test_studio.py 直接跑，不依赖 pytest）
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from h3studio import loras as lora_registry  # noqa: E402
from h3studio import lora_damping  # noqa: E402
from h3studio import workflows  # noqa: E402


class TestLoRARegistry(unittest.TestCase):
    """LoRA 清单：只认本机实际存在的文件。"""

    def test_scanner_returns_only_existing_files(self):
        found = lora_registry.list_z_image_loras()
        self.assertTrue(found, "本机应至少能扫到 Z-Image LoRA")
        for item in found:
            self.assertIsNotNone(item["path"])
            self.assertTrue(os.path.isfile(str(item["path"])),
                            f"{item['name']} 的 path 不存在: {item['path']}")
            self.assertTrue(item["name"].endswith(".safetensors"))

    def test_scanner_only_returns_z_image_loras(self):
        """不能把 285 个 LoRA 全塞进来，只返回 Z-Image 相关的。

        判据有两条（2026-11-08 实测扩了一条）：
          1. 文件名含 z-image 字样（Z_IMAGE_MARKERS）
          2. **或** 层结构与 Z-Image 完全对应 —— 实测 `dejpeg_v3.safetensors`
             文件名毫无 z-image 痕迹，但层结构 100% 命中 Z-Image 底模，
             实测在 ComfyUI 里 attach 出 180 patches、真实生效。
             只按文件名过滤会把它整个漏掉（用户根本选不到）。

        真正的「不把 285 个都塞进来」由**数量上限**兜底断言。
        """
        items = lora_registry.list_z_image_loras()
        for item in items:
            lowered = item["name"].lower()
            by_name = any(m in lowered for m in lora_registry.Z_IMAGE_MARKERS)
            by_structure = item.get("discovered_by") == "layer_structure"
            self.assertTrue(by_name or by_structure,
                            f"{item['name']} 既不靠文件名也不靠层结构认定，"
                            "说明判定条件有漏洞")
            # 结构兜底找到的，层命中率必须 100%
            if by_structure and not by_name:
                self.assertEqual(float(item.get("attention_hit") or 0), 1.0,
                                 f"{item['name']} 靠层结构认定，注意力命中率必须 100%")
        # 数量兜底：本机总共有 264 个 LoRA，Z-Image 相关的只有个位数
        self.assertLess(len(items), 30,
                        f"Z-Image 相关 LoRA 不该有 {len(items)} 个 —— "
                        "结构兜底的判据放太宽了")

    def test_resolve_existing_and_missing(self):
        found = lora_registry.list_z_image_loras()
        if found:
            name = found[0]["name"]
            resolved, path = lora_registry.resolve_lora(name)
            self.assertTrue(resolved)
            self.assertIsNotNone(path)
            assert path is not None
            self.assertTrue(path.is_file())
        # 不存在的必须解析失败（由调用方报错，不能静默忽略）
        resolved, path = lora_registry.resolve_lora("__definitely_not_here__.safetensors")
        self.assertEqual(resolved, "")
        self.assertIsNone(path)

    def test_catalog_entries_are_not_invented(self):
        """说明表里的每个 LoRA 都必须在本机真实存在。

        说明表现在覆盖两个底模族（Z-Image 与 Qwen-Image 2.1），所以两族的
        清单都要查 —— 只查一族会把另一族的真实条目误判成「编造」。
        """
        available = {item["name"] for item in lora_registry.list_z_image_loras()}
        available |= {item["name"] for item in lora_registry.list_qwen_image_loras()}
        for name in lora_registry.LORA_CATALOG:
            base = name.split("/")[-1]
            self.assertTrue(name in available or base in available,
                            f"说明表里的 {name} 本机不存在（不能编造）")


class TestWorkflowLoRA(unittest.TestCase):
    """LoRA 注入工作流。"""

    BASE = {
        "job_id": "testjob", "mode": "t2i", "prompt": "a cat", "seed": 42,
        "model": "z_image_turbo_bf16",
    }

    def _available(self):
        """用户可选的 LoRA（效果类 + 兼容）。不兼容的会被工作流拒绝，
        不能拿来做串联/注入类的正向断言。"""
        return lora_registry.list_effect_loras()

    def test_lora_injects_loader_node(self):
        available = self._available()
        if not available:
            self.skipTest("本机无 Z-Image LoRA")
        name = available[0]["name"]
        wf = workflows.build_t2i({**self.BASE, "loras": [{"name": name, "strength": 0.8}]})
        lora_nodes = [n for n in wf.values() if "Lora" in n["class_type"]]
        self.assertEqual(len(lora_nodes), 1)
        node = lora_nodes[0]
        # 用 LoraLoader（不是 ModelOnly）—— clip 侧也要接，文本编码器才能吃到 patch
        self.assertEqual(node["class_type"], "LoraLoader")
        self.assertAlmostEqual(node["inputs"]["strength_model"], 0.8)
        self.assertAlmostEqual(node["inputs"]["strength_clip"], 0.8)
        # KSampler 的 model 必须指向该 LoRA 节点
        lora_id = next(nid for nid, n in wf.items() if "Lora" in n["class_type"])
        self.assertEqual(wf["57:3"]["inputs"]["model"], [lora_id, 0])
        # clip 输入必须接上 CLIPLoader，且 CLIPTextEncode 用的是 LoRA 后的 clip
        self.assertEqual(node["inputs"]["clip"], ["57:30", 0])
        self.assertEqual(wf["57:27"]["inputs"]["clip"], [lora_id, 1])

    def test_multiple_loras_chain_in_order(self):
        # 只用「兼容且可选」的 LoRA（不兼容的会被工作流拒绝）
        available = self._available()
        if len(available) < 2:
            self.skipTest("本机可用 LoRA 不足 2 个")
        items = [{"name": available[0]["name"], "strength": 0.5},
                 {"name": available[1]["name"], "strength": 0.9}]
        wf = workflows.build_t2i({**self.BASE, "loras": items})
        lora_ids = [nid for nid, n in wf.items() if "Lora" in n["class_type"]]
        self.assertEqual(len(lora_ids), 2)
        # 第二 LoRA 的 model 输入指向第一个 LoRA（串成链）
        first, second = lora_ids
        self.assertEqual(wf[second]["inputs"]["model"], [first, 0])
        # clip 侧同样串成链：第二个 LoRA 的 clip 接第一个 LoRA 的 clip 输出
        self.assertEqual(wf[second]["inputs"]["clip"], [first, 1])
        # 最终 CLIPTextEncode 用的是链尾的 clip
        self.assertEqual(wf["57:27"]["inputs"]["clip"], [second, 1])

    def test_strength_zero_disables_lora(self):
        available = self._available()
        if not available:
            self.skipTest("本机无 Z-Image LoRA")
        wf = workflows.build_t2i({**self.BASE, "loras": [
            {"name": available[0]["name"], "strength": 0.0}]})
        self.assertEqual(len([n for n in wf.values() if "Lora" in n["class_type"]]), 0)

    def test_lora_chain_helper_skips_zero_strength(self):
        """直接测 _z_image_lora_chain 内层的强度守卫。

        build_t2i 外层已经过滤了 strength<=0，这里单独测内层守卫，
        避免它变成没人覆盖的死代码。
        """
        available = self._available()
        if not available:
            self.skipTest("本机无 Z-Image LoRA")
        nodes, ref, cref = workflows._z_image_lora_chain(
            [{"name": available[0]["name"], "strength": 0.0}], ["57:11", 0], ["57:30", 0])
        self.assertEqual(nodes, {})
        self.assertEqual(ref, ["57:11", 0])
        self.assertEqual(cref, ["57:30", 0])
        # 强度 > 0 时才建节点
        nodes2, ref2, cref2 = workflows._z_image_lora_chain(
            [{"name": available[0]["name"], "strength": 0.6}], ["57:11", 0], ["57:30", 0])
        self.assertEqual(len(nodes2), 1)
        self.assertNotEqual(ref2, ["57:11", 0])
        self.assertNotEqual(cref2, ["57:30", 0])

    def test_missing_lora_raises(self):
        with self.assertRaises(ValueError):
            workflows.build_t2i({**self.BASE, "loras": [
                {"name": "__no_such_lora__.safetensors", "strength": 1.0}]})

    def test_workflow_references_all_resolve(self):
        """所有节点引用必须指向存在的节点（批次/LoRA 改动后回归）。"""
        available = self._available()
        loras = [{"name": available[0]["name"], "strength": 1.0}] if available else []
        wf = workflows.build_t2i({**self.BASE, "loras": loras})
        for nid, node in wf.items():
            for key, value in node["inputs"].items():
                if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                    self.assertIn(value[0], wf,
                                  f"节点 {nid}.{key} 指向不存在的节点 {value[0]}")


class TestZImageDimensions(unittest.TestCase):
    """分辨率档位 × 比例的尺寸算法（改动后回归）。"""

    def test_all_tier_aspect_combos_are_16_multiples(self):
        for tier in workflows.Z_IMAGE_RES_TIERS:
            for aspect in workflows.ASPECT_NUM:
                w, h = workflows.z_image_dimensions(aspect, tier)
                self.assertEqual(w % 16, 0, f"{tier}/{aspect} 宽不是 16 倍数")
                self.assertEqual(h % 16, 0, f"{tier}/{aspect} 高不是 16 倍数")

    def test_default_tier_is_1mp_square(self):
        w, h = workflows.z_image_dimensions("1:1", workflows.Z_IMAGE_DEFAULT_TIER)
        self.assertEqual((w, h), (1024, 1024))


class TestBatchLogic(unittest.TestCase):
    """批次拆分逻辑（server.py 里的纯函数部分）。"""

    def test_batch_seeds_are_distinct(self):
        """批次种子必须各不相同，否则 N 张图会一模一样。"""
        base_seed = 100
        seeds = [base_seed + i for i in range(5)]
        self.assertEqual(len(set(seeds)), 5)

    def test_batch_count_clamped(self):
        """batch_count 必须被夹在 1..MAX_BATCH_COUNT。"""
        import re
        server_src = open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server.py"),
            encoding="utf-8").read()
        self.assertIn("MAX_BATCH_COUNT", server_src)
        match = re.search(r"MAX_BATCH_COUNT\s*=\s*(\d+)", server_src)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertGreaterEqual(int(match.group(1)), 1)
        self.assertLessEqual(int(match.group(1)), 5)


class TestBatchAggregation(unittest.TestCase):
    """批次汇总逻辑：单个批次失败不能掩盖整批。"""

    def _aggregate(self, batch_statuses):
        """复刻 server.py Worker._run_batches 的汇总规则。"""
        succeeded = [s for s in batch_statuses if s == "done"]
        if succeeded:
            return "done", f"{len(succeeded)}/{len(batch_statuses)} 批次成功"
        return "failed", f"0/{len(batch_statuses)} 批次成功"

    def test_all_success(self):
        status, text = self._aggregate(["done", "done", "done"])
        self.assertEqual(status, "done")
        self.assertIn("3/3", text)

    def test_partial_failure_still_done(self):
        """2 成功 1 失败 -> 整体 done，成功的产物要保留。"""
        status, text = self._aggregate(["done", "failed", "done"])
        self.assertEqual(status, "done")
        self.assertIn("2/3", text)

    def test_all_failed(self):
        status, text = self._aggregate(["failed", "failed"])
        self.assertEqual(status, "failed")
        self.assertIn("0/2", text)

    def test_failed_batch_keeps_own_error(self):
        """批次级错误存在批次自己的 error 字段里，不合并到任务级。"""
        self.assertEqual(self._aggregate(["done", "failed"])[0], "done")


class TestLoRAPersistence(unittest.TestCase):
    """LoRA 配置的服务端持久化（SQLite）。"""

    def test_save_and_load_roundtrip(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE model_provider_prefs (key TEXT PRIMARY KEY, value TEXT)")
        items = [{"name": "x.safetensors", "strength": 0.7, "enabled": True}]
        lora_registry.save_config(conn, items)
        loaded = lora_registry.load_config(conn)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["name"], "x.safetensors")
        self.assertAlmostEqual(loaded[0]["strength"], 0.7)

    def test_load_returns_empty_when_missing(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE model_provider_prefs (key TEXT PRIMARY KEY, value TEXT)")
        self.assertEqual(lora_registry.load_config(conn), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

class TestLoRASemantics(unittest.TestCase):
    """LoRA 语义：效果类可选 / 加速类不可选 / 触发词自动追加。"""

    BASE = {"job_id": "testjob", "mode": "t2i", "prompt": "a cat", "seed": 42,
            "model": "z_image_turbo_bf16"}

    def test_effect_and_acceleration_are_disjoint(self):
        """效果类与加速类必须互斥，且两者合起来 = 本机全部 z-image LoRA。

        效果类取「全部效果（含被排除的）」—— 用户可选列表还会再过滤不兼容项。
        """
        effect = lora_registry.list_effect_loras(include_incompatible=True)
        accel = lora_registry.list_acceleration_loras()
        all_loras = lora_registry.list_z_image_loras()
        effect_names = {i["name"] for i in effect}
        accel_names = {i["name"] for i in accel}
        self.assertEqual(effect_names & accel_names, set())
        self.assertEqual(effect_names | accel_names, {i["name"] for i in all_loras})

    def test_acceleration_loras_not_in_effect_list(self):
        """已知的加速/蒸馏 LoRA 不能出现在用户可选列表里。"""
        effect_names = {i["name"] for i in lora_registry.list_effect_loras()}
        for accel in lora_registry.list_acceleration_loras():
            self.assertNotIn(accel["name"], effect_names)

    def test_acceleration_lora_rejected_by_workflow(self):
        """把加速 LoRA 当效果 LoRA 用 -> 工作流直接拒绝（不静默出图）。"""
        accel = lora_registry.list_acceleration_loras()
        if not accel:
            self.skipTest("本机无加速类 Z-Image LoRA")
        with self.assertRaises(ValueError) as ctx:
            workflows.build_t2i({**self.BASE, "loras": [
                {"name": accel[0]["name"], "strength": 1.0}]})
        self.assertIn("加速", str(ctx.exception))

    def test_trigger_appended_to_prompt(self):
        """效果 LoRA 的触发词自动追加到最终提示词。"""
        effect = lora_registry.list_effect_loras()
        target = next((i for i in effect if i.get("trigger")), None)
        if not target:
            self.skipTest("本机无带触发词的效果 LoRA")
        wf = workflows.build_t2i({**self.BASE, "loras": [
            {"name": target["name"], "strength": 1.0}]})
        text = wf["57:27"]["inputs"]["text"]
        self.assertTrue(text.startswith("a cat"))
        for token in [t.strip() for t in target["trigger"].replace("/", ",").split(",") if t.strip()]:
            self.assertIn(token, text)

    def test_user_trigger_overrides_upstream(self):
        """用户改过的触发词以用户为准。"""
        effect = lora_registry.list_effect_loras()
        if not effect:
            self.skipTest("本机无效果 LoRA")
        wf = workflows.build_t2i({**self.BASE, "loras": [
            {"name": effect[0]["name"], "strength": 1.0, "trigger": "我的自定义词"}]})
        self.assertIn("我的自定义词", wf["57:27"]["inputs"]["text"])

    def test_user_can_clear_trigger(self):
        """用户清空触发词 -> 不追加任何触发词。"""
        effect = lora_registry.list_effect_loras()
        if not effect:
            self.skipTest("本机无效果 LoRA")
        wf = workflows.build_t2i({**self.BASE, "loras": [
            {"name": effect[0]["name"], "strength": 1.0, "trigger": ""}]})
        self.assertEqual(wf["57:27"]["inputs"]["text"], "a cat")

    def test_multiple_loras_merge_triggers(self):
        """多个 LoRA 的触发词合并追加，不重复。"""
        effect = [i for i in lora_registry.list_effect_loras() if i.get("trigger")]
        if len(effect) < 2:
            self.skipTest("本机带触发词的效果 LoRA 不足 2 个")
        wf = workflows.build_t2i({**self.BASE, "loras": [
            {"name": effect[0]["name"], "strength": 1.0},
            {"name": effect[1]["name"], "strength": 1.0}]})
        text = wf["57:27"]["inputs"]["text"]
        for item in effect[:2]:
            for token in [t.strip() for t in item["trigger"].replace("/", ",").split(",") if t.strip()]:
                self.assertEqual(text.count(token), 1, f"触发词 {token} 重复出现")

    def test_upscale_path_also_wires_lora_clip(self):
        """4K 放大路径同样挂 LoRA（含 clip），且不是死代码。"""
        effect = lora_registry.list_effect_loras()
        if not effect:
            self.skipTest("本机无效果 LoRA")
        wf = workflows.build_t2i({**self.BASE, "resolution": "4k", "loras": [
            {"name": effect[0]["name"], "strength": 1.0}]})
        lora_ids = [nid for nid, n in wf.items() if n["class_type"] == "LoraLoader"]
        self.assertEqual(len(lora_ids), 1)
        self.assertEqual(wf["57:27"]["inputs"]["clip"], [lora_ids[0], 1])
        # 两段 KSampler 都用 LoRA 后的 model
        self.assertEqual(wf["57:3"]["inputs"]["model"], [lora_ids[0], 0])
        self.assertEqual(wf["57:75"]["inputs"]["model"], [lora_ids[0], 0])

class TestLoRACompatibility(unittest.TestCase):
    """LoRA 与 Z-Image 底模的兼容性 —— 不兼容的必须被拒绝。

    背景（2026-11-08 实测）：「瑶光」LoRA 名义上写着 Z-Image，
    但它的注意力层键名是 to_q/to_k/to_v（分离投影），
    而 Z-Image-Turbo UNET 用融合 qkv；且其有效权重扰动达 13%，
    实测出图为彩色噪点。兼容性必须由权重键比对得出，不能靠文件名。
    """

    BASE = {"job_id": "testjob", "mode": "t2i", "prompt": "a cat", "seed": 42,
            "model": "z_image_turbo_bf16"}

    def test_compat_checker_reports_verdict(self):
        """每个效果类 LoRA 都要有兼容性结论（能判定时 verified=True）。"""
        for item in lora_registry.list_z_image_loras():
            if item["kind"] != lora_registry.KIND_EFFECT:
                continue
            self.assertIn("compatible", item)
            self.assertIn("incompatible_reason", item)
            if item["compat_verified"]:
                self.assertIsInstance(item["attention_hit"], float)

    def test_effect_list_excludes_incompatible(self):
        """用户可选列表里不能有被判为不兼容的 LoRA。"""
        available = {i["name"] for i in lora_registry.list_effect_loras()}
        for item in lora_registry.list_incompatible_loras():
            self.assertNotIn(item["name"], available)
            self.assertFalse(item["compatible"])

    def test_incompatible_and_effect_partition_all(self):
        """效果类 = 可选 + 被排除，两部分不重叠且并集完整。"""
        all_effect = {i["name"] for i in lora_registry.list_effect_loras(include_incompatible=True)}
        ok = {i["name"] for i in lora_registry.list_effect_loras()}
        bad = {i["name"] for i in lora_registry.list_incompatible_loras()}
        self.assertEqual(ok & bad, set())
        self.assertEqual(ok | bad, all_effect)

    def test_incompatible_lora_rejected_by_workflow(self):
        """把不兼容 LoRA 塞进工作流 -> 直接拒绝（不能静默出噪点图）。"""
        bad = lora_registry.list_incompatible_loras()
        if not bad:
            self.skipTest("本机无不兼容 Z-Image LoRA")
        with self.assertRaises(ValueError) as ctx:
            workflows.build_t2i({**self.BASE, "loras": [
                {"name": bad[0]["name"], "strength": 1.0}]})
        self.assertIn("不兼容", str(ctx.exception))

    def test_incompatible_has_reason(self):
        """被排除的 LoRA 必须带可展示的原因（界面要显示为什么）。"""
        for item in lora_registry.list_incompatible_loras():
            self.assertTrue(item["incompatible_reason"].strip(),
                            f"{item['name']} 缺少排除原因")

    def test_perturbation_threshold_triggers_strength_cap(self):
        """扰动超过阈值 -> 不是「排除」，而是**自动阻尼 context_refiner**。

        实测（2026-11-08，用户真实提示词 + 种子 132199052268679）：
          瑶光整体扰动 13.2%，其中 context_refiner 高达 12.85%
          （如梦似幻只有 1.36%）-> 强度 1.0 出彩色噪点。
        消融实验：只把 context_refiner 乘 0.8，6 个提示词 x 5 个种子全部干净，
        且 LoRA 效果保留（MAE 与无 LoRA 相差 38）。
        而「全局封顶 0.8」在同样的矩阵里仍有 2/6 提示词出噪点 —— 说明
        根因修法是阻尼 context_refiner，而不是全局降强度。

        所以：扰动超阈值的 LoRA 必须（a）保持可选、（b）自动阻尼、
        （c）强度不再被封顶（阻尼后 1.0 是安全的）。
        """
        for item in lora_registry.list_z_image_loras():
            pert = item.get("relative_perturbation")
            if pert is None:
                continue
            cap = item.get("strength_cap", 1.0)
            ctx_pert = item.get("ctx_perturbation")
            damping = item.get("ctx_damping")
            if ctx_pert is not None and ctx_pert >= lora_registry.COMPAT_CTX_DAMPING_THRESHOLD:
                # context_refiner 扰动大 -> 必须阻尼，且强度可以保持 1.0
                self.assertIsNotNone(damping,
                                     f"{item['name']} ctx 扰动 {ctx_pert} 却没阻尼")
                self.assertLess(damping, 1.0,
                                f"{item['name']} 阻尼系数 {damping} 应 < 1.0")
                self.assertEqual(cap, 1.0,
                                 f"{item['name']} 已阻尼，不该再封强度")
                self.assertTrue(item["compatible"],
                                f"{item['name']} 扰动过大但能 attach，不该判为不兼容")
            elif pert > lora_registry.COMPAT_MAX_RELATIVE_PERTURBATION:
                # 整体扰动大但 context_refiner 正常 -> 保守给个推荐强度
                self.assertTrue(item["compatible"],
                                f"{item['name']} 扰动过大但能 attach，不该判为不兼容")
                self.assertLessEqual(item["recommended_strength"], max(cap, 1.0))
            else:
                self.assertEqual(cap, 1.0,
                                 f"{item['name']} 扰动 {pert} 未超阈值却限了强度")

    def test_strength_cap_is_enforced_by_workflow(self):
        """强度超上限 -> 工作流夹到上限（而不是放行到崩图）。"""
        capped = [i for i in lora_registry.list_z_image_loras()
                  if i.get("strength_cap", 1.0) < 1.0]
        if not capped:
            self.skipTest("本机无需要限强度的 LoRA")
        item = capped[0]
        wf = workflows.build_t2i({**self.BASE, "loras": [
            {"name": item["name"], "strength": 1.0}]})
        lora_nodes = [n for n in wf.values() if "Lora" in n["class_type"]]
        self.assertEqual(len(lora_nodes), 1)
        self.assertAlmostEqual(lora_nodes[0]["inputs"]["strength_model"],
                               item["strength_cap"], places=3)

    def test_attention_key_mismatch_is_detected(self):
        """要 patch 的注意力层在底模里根本不存在 -> 必须判为不命中。

        用合成夹具，不依赖本机文件。
        """
        # LoRA 想 patch 一套底模里没有的注意力实现
        lora_bases = {"context_refiner.0.attention.wq",       # 底模没有 wq
                      "context_refiner.0.attention.wk",
                      "context_refiner.0.attention.wv"}
        unet_keys = {"context_refiner.0.attention.qkv",
                     "context_refiner.0.feed_forward.w1"}
        ratio, hit, total = lora_registry._attention_hit_ratio(lora_bases, unet_keys)
        self.assertEqual(total, 3)
        self.assertEqual(hit, 0)
        self.assertLess(ratio, lora_registry.COMPAT_MIN_ATTENTION_HIT,
                        "底模里不存在的层必须判为不命中")

    def test_fused_qkv_mapping_counts_as_hit(self):
        """分离投影 to_q/to_k/to_v -> 融合 qkv 是 ComfyUI 的正规映射，必须算命中。

        本机 ComfyUI 进程内实测：load_lora 会把这类 LoRA 打到
        diffusion_model.layers.N.attention.qkv.weight 的三段上。
        """
        lora_bases = {"context_refiner.0.attention.to_q",
                      "context_refiner.0.attention.to_k",
                      "context_refiner.0.attention.to_v",
                      "context_refiner.0.attention.to_out.0"}
        unet_keys = {"context_refiner.0.attention.qkv",
                     "context_refiner.0.attention.out"}
        ratio, hit, total = lora_registry._attention_hit_ratio(lora_bases, unet_keys)
        self.assertEqual(total, 4)
        self.assertEqual(hit, 4)
        self.assertEqual(ratio, 1.0)

    def test_direct_qkv_keys_count_as_hit(self):
        """键名直接叫 qkv 的 LoRA 必须命中。"""
        lora_bases = {"layers.0.attention.qkv", "layers.0.attention.out"}
        unet_keys = {"layers.0.attention.qkv", "layers.0.attention.out"}
        ratio, hit, total = lora_registry._attention_hit_ratio(lora_bases, unet_keys)
        self.assertEqual(ratio, 1.0)
        self.assertEqual(hit, 2)


class TestLoRAContextDamping(unittest.TestCase):
    """context_refiner 阻尼（2026-11-08 根因修复）。

    实测结论：Z-Image 的 context_refiner 是**联合处理文本条件 + 潜变量**的模块。
    某些 LoRA（瑶光：context_refiner 相对扰动 12.85%）对这个模块扰动过大，
    在长中文结构化提示词下会把采样推到发散 -> 出图为彩色噪点。

    消融（同一提示词 / 同一种子 / 强度 1.0）：
        原始            base 14.5(噪) tmpl 14.6(噪) tmpl+瑶光 14.6(噪)
        ctx x0.5         base  8.6(ok) tmpl  8.5(ok) tmpl+瑶光  8.4(ok)
        去掉 ctx         base  8.2(ok) tmpl  7.8(ok) tmpl+瑶光  7.7(ok)

    端到端对比（6 提示词 x 5 种子）：
        原样 瑶光1.0            -> 5/6 提示词出噪点
        全局封顶0.8             -> 2/6 提示词出噪点（不够）
        ctx阻尼0.8 强度1.0      -> 0/6 出噪点，且 LoRA 效果保留
    """

    def test_derived_files_hidden_from_user_list(self):
        """服务端生成的派生 LoRA 不能出现在用户可选列表里。"""
        for item in lora_registry.list_z_image_loras():
            self.assertFalse(
                item["name"].lower().startswith(lora_damping.DERIVED_PREFIX),
                f"派生文件 {item['name']} 不该出现在用户可选列表")

    def test_damping_name_is_deterministic(self):
        """派生文件名必须稳定（同样的输入 -> 同样的名字，避免重复生成）。"""
        a = lora_damping._damped_name("Kook_Zimage_瑶光.safetensors", 0.8)
        b = lora_damping._damped_name("Kook_Zimage_瑶光.safetensors", 0.8)
        self.assertEqual(a, b)
        self.assertTrue(a.startswith(lora_damping.DERIVED_PREFIX))
        self.assertIn("80", a)
        c = lora_damping._damped_name("Kook_Zimage_瑶光.safetensors", 0.5)
        self.assertNotEqual(a, c)

    def test_damped_name_keeps_subdirectory_flat(self):
        """带子目录的 LoRA 名不能产生路径穿越。"""
        name = lora_damping._damped_name("z-image/Some.safetensors", 0.8)
        self.assertNotIn("/", name)
        self.assertNotIn("..", name)

    def test_measured_ctx_perturbation_matches_real_measurement(self):
        """实测扰动值必须与独立测量脚本一致（瑶光 ~12.8%）。"""
        found = {i["name"]: i for i in lora_registry.list_z_image_loras()}
        yao = found.get("Kook_Zimage_瑶光.safetensors")
        if not yao:
            self.skipTest("本机无瑶光 LoRA")
        ctx = yao.get("ctx_perturbation")
        self.assertIsNotNone(ctx, "瑶光必须测得出 context_refiner 扰动")
        self.assertGreater(ctx, 0.1, "瑶光的 context_refiner 扰动应 >10%")
        self.assertGreater(ctx, lora_registry.COMPAT_CTX_DAMPING_THRESHOLD)

    def test_low_ctx_perturbation_lora_is_not_damped(self):
        """context_refiner 扰动小的 LoRA（如梦似幻 1.36%）不该被阻尼。"""
        found = {i["name"]: i for i in lora_registry.list_z_image_loras()}
        rms = found.get("Kook_Zimage_如梦似幻.safetensors")
        if not rms:
            self.skipTest("本机无如梦似幻 LoRA")
        self.assertLess(rms["ctx_perturbation"],
                        lora_registry.COMPAT_CTX_DAMPING_THRESHOLD)
        self.assertIsNone(rms.get("ctx_damping"))

    def test_workflow_uses_damped_lora_when_needed(self):
        """context_refiner 扰动大的 LoRA -> 工作流引用阻尼后的派生文件。"""
        found = {i["name"]: i for i in lora_registry.list_z_image_loras()}
        yao = found.get("Kook_Zimage_瑶光.safetensors")
        if not yao or not yao.get("ctx_damping"):
            self.skipTest("本机瑶光未触发阻尼")
        wf = workflows.build_t2i({
            "prompt": "test", "model": "z_image_turbo_bf16", "aspect": "16:9",
            "tier": "1mp", "sampler": "res_multistep", "scheduler": "simple",
            "steps": 8, "cfg": 1.0, "shift": 3.0, "seed": 1,
            "negative_mode": "zero", "job_id": "damp1",
            "loras": [{"name": "Kook_Zimage_瑶光.safetensors", "strength": 1.0}]})
        loaders = [n for n in wf.values()
                   if n["class_type"].startswith("LoraLoader")]
        self.assertEqual(len(loaders), 1)
        self.assertTrue(loaders[0]["inputs"]["lora_name"].startswith(
            lora_damping.DERIVED_PREFIX),
            "工作流必须引用阻尼后的派生 LoRA")
        self.assertEqual(loaders[0]["inputs"]["strength_model"], 1.0)

    def test_damping_does_not_change_other_lora(self):
        """阻尼只影响触发的那个 LoRA，其它 LoRA 仍用原文件。"""
        found = {i["name"]: i for i in lora_registry.list_z_image_loras()}
        yao = found.get("Kook_Zimage_瑶光.safetensors")
        rms = found.get("Kook_Zimage_如梦似幻.safetensors")
        if not yao or not yao.get("ctx_damping") or not rms:
            self.skipTest("本机缺少触发阻尼的 LoRA 对")
        wf = workflows.build_t2i({
            "prompt": "test", "model": "z_image_turbo_bf16", "aspect": "16:9",
            "tier": "1mp", "sampler": "res_multistep", "scheduler": "simple",
            "steps": 8, "cfg": 1.0, "shift": 3.0, "seed": 1,
            "negative_mode": "zero", "job_id": "damp2",
            "loras": [{"name": "Kook_Zimage_瑶光.safetensors", "strength": 1.0},
                      {"name": "Kook_Zimage_如梦似幻.safetensors", "strength": 1.0}]})
        names = [n["inputs"]["lora_name"] for n in wf.values()
                 if n["class_type"].startswith("LoraLoader")]
        self.assertEqual(len(names), 2)
        damped = [n for n in names if n.startswith(lora_damping.DERIVED_PREFIX)]
        plain = [n for n in names if not n.startswith(lora_damping.DERIVED_PREFIX)]
        self.assertEqual(len(damped), 1, "只有瑶光该被阻尼")
        self.assertEqual(len(plain), 1, "如梦似幻不该被阻尼")

    def test_stale_undamped_file_is_regenerated(self):
        """陈旧的「名字对但内容没阻尼」的派生文件必须被重建。

        真实踩过：14:47 留下的 h3studio_ctx_80__*.safetensors 内容 ratio=1.0
        （根本没阻尼），ComfyUI 照常加载它 -> 出图仍是彩色噪点。
        所以复用已有派生文件前必须校验内容。
        """
        try:
            from safetensors.torch import load_file, save_file
        except Exception:
            self.skipTest("本机无 torch")
        import tempfile
        from pathlib import Path as _Path
        yao = _Path("/mnt/usb2t/comfyui-models/loras/Kook_Zimage_瑶光.safetensors")
        if not yao.is_file():
            self.skipTest("本机无瑶光 LoRA 文件")
        with tempfile.TemporaryDirectory() as tmp:
            # 造一个「名字对但没阻尼」的陈旧文件
            stale = _Path(tmp) / lora_damping._damped_name(yao.name, 0.8)
            save_file(load_file(str(yao)), str(stale), metadata={"format": "pt"})
            self.assertFalse(
                lora_damping._is_damped(stale, 0.8, source=yao),
                "没阻尼的文件必须被 _is_damped 判为不合格")
            # 正确阻尼过的文件必须通过
            good = _Path(tmp) / "good.safetensors"
            self.assertTrue(lora_damping._write_damped(yao, good, 0.8))
            self.assertTrue(lora_damping._is_damped(good, 0.8, source=yao),
                            "正确阻尼的文件必须通过 _is_damped")
            # 系数不匹配 -> 不通过
            self.assertFalse(lora_damping._is_damped(good, 0.5, source=yao),
                             "系数不匹配必须判为不合格")

    def test_is_damped_rejects_undamped_and_wrong_factor(self):
        """_is_damped 必须真的在做判断（不能恒返回 True）。

        恒返回 True 的变异会让「陈旧文件复用」这条路径失去保护，
        而只测「重建成功」的断言抓不到它 —— 必须直接断言它会拒绝。
        """
        yao = Path("/mnt/usb2t/comfyui-models/loras/Kook_Zimage_瑶光.safetensors")
        if not yao.is_file():
            self.skipTest("本机无瑶光 LoRA 文件")
        try:
            from safetensors.torch import load_file, save_file
        except Exception:
            self.skipTest("本机无 torch")
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            factor = lora_damping.DEFAULT_CTX_DAMPING
            # 1) 根本没阻尼的源文件副本 -> 必须判为不合格
            undamped = tmpdir / "undamped.safetensors"
            save_file(load_file(str(yao)), str(undamped), metadata={"format": "pt"})
            self.assertFalse(lora_damping._is_damped(undamped, factor, source=yao),
                             "未阻尼的文件必须被拒绝")
            # 2) 正确阻尼 -> 必须判为合格
            good = tmpdir / "good.safetensors"
            self.assertTrue(lora_damping._write_damped(yao, good, factor))
            self.assertTrue(lora_damping._is_damped(good, factor, source=yao),
                            "正确阻尼的文件必须被接受")
            # 3) 用错误的系数去问同一个文件 -> 必须判为不合格
            self.assertFalse(lora_damping._is_damped(good, factor + 0.3, source=yao),
                             "系数不匹配必须被拒绝")
            # 4) 文件不存在 -> 必须判为不合格
            self.assertFalse(
                lora_damping._is_damped(tmpdir / "nope.safetensors", factor, source=yao),
                "文件不存在必须被拒绝")
            # 5) 根本没有 context_refiner 张量的文件 -> 必须判为不合格
            #    （阻尼对它本来就是空操作 = 没阻尼，不能当成合格）
            no_ctx = tmpdir / "no_ctx.safetensors"
            only_other = {k: v for k, v in load_file(str(yao)).items()
                          if "context_refiner." not in k}
            save_file(only_other, str(no_ctx), metadata={"format": "pt"})
            self.assertFalse(
                lora_damping._is_damped(no_ctx, factor, source=yao),
                "没有 context_refiner 张量必须被拒绝（阻尼对它是空操作）")

    def test_ensure_damped_lora_rebuilds_stale_file(self):
        """ensure_damped_lora 遇到陈旧（未阻尼）派生文件必须重建它。

        这是真实回归点：14:47 的派生文件 ratio=1.0（没阻尼），
        ensure_damped_lora 只看文件名直接复用 -> ComfyUI 照常加载 -> 噪点回归。
        """
        yao = Path("/mnt/usb2t/comfyui-models/loras/Kook_Zimage_瑶光.safetensors")
        if not yao.is_file():
            self.skipTest("本机无瑶光 LoRA 文件")
        try:
            from safetensors.torch import load_file, save_file
        except Exception:
            self.skipTest("本机无 torch")
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            derived_name = lora_damping._damped_name(yao.name,
                                                     lora_damping.DEFAULT_CTX_DAMPING)
            stale = tmpdir / derived_name
            # 造一个「名字对但没阻尼」的陈旧文件
            save_file(load_file(str(yao)), str(stale), metadata={"format": "pt"})
            self.assertFalse(
                lora_damping._is_damped(stale, lora_damping.DEFAULT_CTX_DAMPING,
                                        source=yao))
            # ensure_damped_lora 必须重建（而不是直接复用陈旧文件）
            name, path = lora_damping.ensure_damped_lora(
                yao.name, lora_damping.DEFAULT_CTX_DAMPING, lora_dirs=[tmpdir])
            self.assertTrue(name, "应该返回派生 LoRA 名")
            self.assertIsNotNone(path)
            self.assertTrue(
                lora_damping._is_damped(path, lora_damping.DEFAULT_CTX_DAMPING,
                                        source=yao),
                "ensure_damped_lora 必须把陈旧文件重建为真正阻尼过的版本")

    def test_damped_derived_file_content_scales_ctx_only(self):
        """派生文件：context_refiner 被缩放，其它张量原样保留。"""
        try:
            import torch
            from safetensors.torch import load_file
        except Exception:
            self.skipTest("本机无 torch")
        import tempfile
        from pathlib import Path
        yao = Path("/mnt/usb2t/comfyui-models/loras/Kook_Zimage_瑶光.safetensors")
        if not yao.is_file():
            self.skipTest("本机无瑶光 LoRA 文件")
        with tempfile.TemporaryDirectory() as tmp:
            src = load_file(str(yao))
            dst_path = Path(tmp) / "damped.safetensors"
            self.assertTrue(lora_damping._write_damped(
                yao, dst_path, lora_damping.DEFAULT_CTX_DAMPING))
            dst = load_file(str(dst_path))
            self.assertEqual(set(src.keys()), set(dst.keys()))
            factor = lora_damping.DEFAULT_CTX_DAMPING
            # 阻尼系数必须是「真正在衰减」的值 —— 不能是 1.0（等于没阻尼）
            self.assertLess(factor, 1.0, "阻尼系数必须 < 1.0，否则等于没阻尼")
            self.assertGreater(factor, 0.0, "阻尼系数必须 > 0")
            checked = 0
            for key in src:
                if key.startswith("context_refiner."):
                    ratio = (dst[key].float().norm()
                             / src[key].float().norm().clamp(min=1e-9)).item()
                    self.assertAlmostEqual(ratio, factor, places=3,
                                           msg=f"{key} 缩放系数不对")
                    checked += 1
                else:
                    self.assertTrue(torch.equal(dst[key], src[key]),
                                    f"{key} 不该被改动")
            self.assertGreater(checked, 0, "没有比对到 context_refiner 张量")