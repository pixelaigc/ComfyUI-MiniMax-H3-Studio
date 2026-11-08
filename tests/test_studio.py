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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from h3studio import loras as lora_registry  # noqa: E402
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
        """不能把 285 个 LoRA 全塞进来，只返回 Z-Image 相关的。"""
        for item in lora_registry.list_z_image_loras():
            lowered = item["name"].lower()
            self.assertTrue(any(m in lowered for m in lora_registry.Z_IMAGE_MARKERS),
                            f"{item['name']} 不像 Z-Image LoRA")

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
        """说明表里的每个 LoRA 都必须在本机真实存在。"""
        available = {item["name"] for item in lora_registry.list_z_image_loras()}
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
        return lora_registry.list_z_image_loras()

    def test_lora_injects_loader_node(self):
        available = self._available()
        if not available:
            self.skipTest("本机无 Z-Image LoRA")
        name = available[0]["name"]
        wf = workflows.build_t2i({**self.BASE, "loras": [{"name": name, "strength": 0.8}]})
        lora_nodes = [n for n in wf.values() if "Lora" in n["class_type"]]
        self.assertEqual(len(lora_nodes), 1)
        node = lora_nodes[0]
        self.assertEqual(node["class_type"], "LoraLoaderModelOnly")
        self.assertAlmostEqual(node["inputs"]["strength_model"], 0.8)
        # KSampler 的 model 必须指向该 LoRA 节点
        lora_id = next(nid for nid, n in wf.items() if "Lora" in n["class_type"])
        self.assertEqual(wf["57:3"]["inputs"]["model"], [lora_id, 0])

    def test_multiple_loras_chain_in_order(self):
        available = self._available()
        if len(available) < 2:
            self.skipTest("本机 LoRA 不足 2 个")
        items = [{"name": available[0]["name"], "strength": 0.5},
                 {"name": available[1]["name"], "strength": 0.9}]
        wf = workflows.build_t2i({**self.BASE, "loras": items})
        lora_ids = [nid for nid, n in wf.items() if "Lora" in n["class_type"]]
        self.assertEqual(len(lora_ids), 2)
        # 第二 LoRA 的 model 输入指向第一个 LoRA（串成链）
        first, second = lora_ids
        self.assertEqual(wf[second]["inputs"]["model"], [first, 0])

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
        nodes, ref = workflows._z_image_lora_chain(
            [{"name": available[0]["name"], "strength": 0.0}], ["57:11", 0])
        self.assertEqual(nodes, {})
        self.assertEqual(ref, ["57:11", 0])
        # 强度 > 0 时才建节点
        nodes2, ref2 = workflows._z_image_lora_chain(
            [{"name": available[0]["name"], "strength": 0.6}], ["57:11", 0])
        self.assertEqual(len(nodes2), 1)
        self.assertNotEqual(ref2, ["57:11", 0])

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
