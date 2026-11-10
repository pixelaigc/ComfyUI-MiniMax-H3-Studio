# -*- coding: utf-8 -*-
"""
A3：LoRA 目录间同名 key 冲突的显式消解。

背景（2026-11-10 实测）：四个目录（effects / mihoyo / ea / nsfw）
**不是互斥**的 —— 同一个文件可能同时被两个目录登记，且 plan_id 不同。
实测 15 处跨模块同名 key（如
`moss-effects/MOSSRealisticSnapshotV1.safetensors` 同时是
EA-MOSS-046 与 MN-01）。

旧实现用 `LORA_CATALOG.update(...)` —— 后者覆盖前者，
于是**合并顺序**决定哪条生效，任何顺序都会挂掉一侧的 plan_id 断言。

解法：`loras._merge_catalog` —— **先合并者优先**，后来的同名条目
不覆盖，但**显式登记**进 `LORA_CATALOG_CONFLICTS` 台账。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from h3studio import loras as L  # noqa: E402


class TestCatalogConflictPolicy(unittest.TestCase):
    """同名 key 的消解策略必须是**确定的**（不靠合并顺序碰运气）。"""

    def test_conflict_ledger_exists(self):
        self.assertTrue(hasattr(L, "LORA_CATALOG_CONFLICTS"),
                        "缺 LORA_CATALOG_CONFLICTS 台账 —— "
                        "冲突必须被显式登记，不是静默丢失")
        self.assertIsInstance(L.LORA_CATALOG_CONFLICTS, dict)

    def test_merge_catalog_does_not_overwrite(self):
        """_merge_catalog 不能覆盖已有的同名 key。"""
        target = {}
        original = L.LORA_CATALOG
        original_conflicts = L.LORA_CATALOG_CONFLICTS
        try:
            L.LORA_CATALOG = target
            L.LORA_CATALOG_CONFLICTS = {}
            L._merge_catalog({"a": {"plan_id": "P1", "display": "first"}}, "s1")
            L._merge_catalog({"a": {"plan_id": "P2", "display": "second"}}, "s2")
            self.assertEqual(target["a"]["plan_id"], "P1",
                             "后来的同名条目覆盖了先合并的 —— "
                             "策略必须是「先合并者优先」")
            self.assertIn("a", L.LORA_CATALOG_CONFLICTS,
                          "冲突没进台账")
            entries = L.LORA_CATALOG_CONFLICTS["a"]
            self.assertTrue(any(e.get("kept") for e in entries),
                            "台账里没有 kept 条目")
            self.assertEqual([e["plan_id"] for e in entries], ["P1", "P2"],
                             "台账必须记录**全部**候选（含被挡的）")
        finally:
            L.LORA_CATALOG = original
            L.LORA_CATALOG_CONFLICTS = original_conflicts

    def test_real_conflicts_are_registered(self):
        """本机真实存在的跨模块同名 key 必须都在台账里。"""
        import h3studio.lora_catalog_effects as E
        import h3studio.lora_catalog_mihoyo as M
        import h3studio.lora_catalog_ea as EA
        import h3studio.lora_catalog_nsfw as N

        sources = {}
        for name, mod in (("effects", E), ("mihoyo", M), ("ea", EA), ("nsfw", N)):
            for attr, table in vars(mod).items():
                if not isinstance(table, dict) or not table:
                    continue
                if not all(isinstance(v, dict) for v in table.values()):
                    continue
                if not (attr.endswith("_ALL_EFFECTS") or attr == "ALL_EFFECTS"):
                    continue
                for key, info in table.items():
                    sources.setdefault(key, {})[name] = (info or {}).get("plan_id")

        cross = {k: v for k, v in sources.items() if len(v) > 1}
        self.assertTrue(cross,
                        "本机应当存在跨模块同名 key（否则这条测试没有意义）")
        for key, entries in cross.items():
            self.assertIn(key, L.LORA_CATALOG_CONFLICTS,
                          f"{key} 是跨模块同名 key（{entries}），但没进冲突台账")

    def test_every_conflict_has_exactly_one_kept(self):
        """每个冲突必须**恰好**有一条 kept（不能 0 条也不能多条）。"""
        for key, entries in L.LORA_CATALOG_CONFLICTS.items():
            kept = [e for e in entries if e.get("kept")]
            self.assertEqual(len(kept), 1,
                             f"{key} 的台账里有 {len(kept)} 条 kept（必须恰好 1 条）")

    def test_kept_entry_matches_registry(self):
        """台账里 kept 的那条必须与注册表里的实际值一致。"""
        for key, entries in L.LORA_CATALOG_CONFLICTS.items():
            kept = [e for e in entries if e.get("kept")][0]
            actual = L.LORA_CATALOG.get(key, {})
            self.assertEqual(actual.get("plan_id"), kept["plan_id"],
                             f"{key} 注册表里的 plan_id 与台账的 kept 不一致")

    def test_kept_is_the_first_merged(self):
        """kept 必须是**先合并**的那条（合并顺序：effects -> mihoyo -> ea -> nsfw）。"""
        order = ("effects", "mihoyo", "ea", "nsfw")
        for key, entries in L.LORA_CATALOG_CONFLICTS.items():
            kept = [e for e in entries if e.get("kept")][0]
            sources = [e["source"] for e in entries]
            self.assertEqual(kept["source"], sources[0],
                             f"{key} 的 kept 不是先合并的那条 —— "
                             f"台账顺序 {sources}")

    def test_plan_ids_in_conflict_are_unique_per_source(self):
        """台账里同一来源的 plan_id 必须唯一（否则台账本身有问题）。"""
        for key, entries in L.LORA_CATALOG_CONFLICTS.items():
            by_source = {}
            for e in entries:
                by_source.setdefault(e["source"], []).append(e["plan_id"])
            for source, ids in by_source.items():
                self.assertEqual(len(ids), len(set(ids)),
                                 f"{key} 的 {source} 来源有重复 plan_id：{ids}")


if __name__ == "__main__":
    unittest.main()
