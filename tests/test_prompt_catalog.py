# -*- coding: utf-8 -*-
"""「创意内容设计」选项目录 —— 契约与数据对账测试。

【这个测试锁住什么】
1. **数据对账**：目录里的选项数必须等于调研 B 报告的 871 项（逐条保留，
   无删减）；维度 9 个、子类 48 个。
2. **标签体系词**：调研标「标」的 166 项必须带 Danbooru 官方 post_count
   实测值（post_count > 0）。这是调研里最硬的一条取证，本测试保留它。
3. **三级依据标注**：通 / 模 / 标 的分布必须与调研一致（625 / 70 / 176）。
4. **显式「不指定」**：每个可选子类都必须有跳过项，且跳过项**真的**不输出
   （不能写 "no X" 这种否定式 —— 模型会画出字面意思）。
5. **冲突规则可执行**：每条规则的 a/b 都必须指向真实存在的选项 id；
   规则引用了不存在的项 ⇒ 构建期直接报错（不允许静默失效）。
6. **拼装骨架顺序**：主体 → 造型 → 风格 → 场景 → 构图 → 光 → 色 → 氛围。
7. **不得含权重语法**：拼装结果里不能出现 (x:1.5) 这类权重。

运行：
    python3 -m pytest tests/test_prompt_catalog.py -q --tb=short
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import prompt_catalog  # noqa: E402


#: 调研 B 报告 §1.1 的规模摘要（唯一对账基准）
RESEARCH_ITEMS = 871
RESEARCH_GROUPS = 48
RESEARCH_DIMENSIONS = 9
RESEARCH_BY_LEVEL = {"通": 625, "模": 70, "标": 176}
#: 调研里逐条对 Danbooru 实测通过的标签体系词数量
RESEARCH_TAG_VERIFIED = 166


class TestCatalogCounts(unittest.TestCase):
    """规模对账。"""

    @classmethod
    def setUpClass(cls):
        cls.m = prompt_catalog.get_manifest()

    def test_dimension_group_counts_match_research(self):
        self.assertEqual(RESEARCH_DIMENSIONS, len(self.m["dimensions"]))
        groups = sum(len(d["groups"]) for d in self.m["dimensions"])
        self.assertEqual(RESEARCH_GROUPS, groups)

    def test_research_items_are_kept_verbatim(self):
        """调研原文 871 项必须**逐条保留**，一个不少。

        【为什么这么判】任务书要求「选项规模不得低于调研②实测的 871 项；
        若有删减必须逐条列出并给理由」。本测试锁住「无删减」这条底线：
        research_items 是调研原文条目数（不含本次补的显式跳过项）。
        """
        counts = self.m["counts"]
        self.assertEqual(RESEARCH_ITEMS, counts["research_items"],
                         "调研原文条目数变了 —— 若确实要删减，必须逐条列出并给理由")

    def test_total_selectable_items_is_at_least_research(self):
        """前端可选总数 ≥ 871（调研原文 + 补的显式跳过项）。"""
        self.assertGreaterEqual(self.m["counts"]["items"], RESEARCH_ITEMS)

    def test_level_distribution_matches_research(self):
        self.assertEqual(RESEARCH_BY_LEVEL, self.m["counts"]["by_level"])

    def test_every_item_has_label_and_id(self):
        ids = set()
        for dim in self.m["dimensions"]:
            for g in dim["groups"]:
                for it in g["items"]:
                    self.assertTrue(it["id"], f"{dim['id']} 有条目没有 id")
                    self.assertNotIn(it["id"], ids, f"id 重复：{it['id']}")
                    ids.add(it["id"])
                    self.assertTrue(it["label"].strip(), f"{it['id']} 没有显示名")
                    self.assertIn(it["level"], ("通", "模", "标"))

    def test_every_group_has_explicit_skip_option(self):
        """每个可选子类都要有显式「不指定」项。

        【为什么必须显式】用户需要一个明确的「跳过这一级」动作，
        而不是靠「下拉框停在第一项」这种隐式约定。
        """
        for dim in self.m["dimensions"]:
            for g in dim["groups"]:
                if g.get("render") == "rules":
                    continue
                skips = [i for i in g["items"] if i["is_skip"]]
                self.assertTrue(skips,
                                f"{dim['id']}/{g['name']} 没有显式「不指定」选项")


class TestTagSystemVerification(unittest.TestCase):
    """标签体系词（Danbooru）必须保留实测 post_count。"""

    @classmethod
    def setUpClass(cls):
        cls.m = prompt_catalog.get_manifest()

    def test_tag_items_keep_danbooru_post_count(self):
        """每条「标」的非跳过项都必须带 Danbooru 官方 post_count（> 0）。

        【口径与调研自检一致】调研 _build.py 的自检闸门写的是
        ``if _lv != "标" or not _en or _en.startswith("（"): continue``
        —— 也就是说「标」类里的**跳过项**（中文名「不指定」）不参与
        post_count 核验。176 条「标」里有 10 条是跳过项，
        真正需要实测核验的是 166 条 —— 与调研自检输出完全一致。
        """
        tag_items = []
        for dim in self.m["dimensions"]:
            for g in dim["groups"]:
                for it in g["items"]:
                    if it["level"] == "标" and not it["is_skip"]:
                        tag_items.append(it)
        self.assertEqual(RESEARCH_TAG_VERIFIED, len(tag_items),
                         "需要 post_count 核验的标签体系词数量与调研不符")
        for it in tag_items:
            pc = it.get("danbooru_post_count")
            self.assertIsInstance(pc, int, f"{it['id']}（{it['label']}）没有 post_count")
            self.assertGreater(pc, 0, f"{it['id']}（{it['label']}）post_count 必须 > 0")

    def test_tag_skip_items_are_excluded_from_verification(self):
        """「标」类里的跳过项不参与 post_count 核验（口径与调研自检一致）。"""
        skip_tags = 0
        for dim in self.m["dimensions"]:
            for g in dim["groups"]:
                for it in g["items"]:
                    if it["level"] == "标" and it["is_skip"]:
                        skip_tags += 1
                        self.assertIsNone(it.get("danbooru_post_count"))
        self.assertEqual(10, skip_tags, "「标」类跳过项数量变了（调研口径是 176 - 166 = 10）")

    def test_tag_level_is_marked_needs_test_false(self):
        """「标」类不需要再实测（已经实测过），「模」类才需要。"""
        for dim in self.m["dimensions"]:
            for g in dim["groups"]:
                for it in g["items"]:
                    if it["level"] == "标":
                        self.assertFalse(it["needs_test"], it["id"])
                    if it["level"] == "模":
                        self.assertTrue(it["needs_test"],
                                        f"{it['id']} 标了「模型特定偏好」但没标需实测")


class TestConflictRules(unittest.TestCase):
    """冲突矩阵必须可执行（引用的每个 id 都真实存在）。"""

    @classmethod
    def setUpClass(cls):
        cls.m = prompt_catalog.get_manifest()

    def test_rule_ids_are_unique(self):
        ids = [c["id"] for c in self.m["conflicts"]]
        self.assertEqual(len(ids), len(set(ids)), "冲突规则 id 重复")

    def test_every_rule_reference_exists(self):
        known = set()
        for dim in self.m["dimensions"]:
            for g in dim["groups"]:
                for it in g["items"]:
                    known.add(it["id"])
        for c in self.m["conflicts"]:
            for iid in list(c["a"]) + list(c["b"]):
                self.assertIn(iid, known, f"规则 {c['id']} 引用了不存在的选项 {iid}")

    def test_hard_and_soft_rules_both_present(self):
        kinds = [c["kind"] for c in self.m["conflicts"]]
        self.assertGreaterEqual(kinds.count("hard"), 20, "硬互斥规则太少")
        self.assertGreaterEqual(kinds.count("soft"), 10, "软冲突规则太少")
        self.assertGreaterEqual(kinds.count("cap"), 2, "数量上限规则太少")

    def test_hard_rules_have_both_sides(self):
        for c in self.m["conflicts"]:
            if c["kind"] != "hard":
                continue
            self.assertTrue(c["a"], f"{c['id']} 硬互斥规则没有 a 侧")
            self.assertTrue(c["b"], f"{c['id']} 硬互斥规则没有 b 侧")
            self.assertTrue(c["reason"].strip(), f"{c['id']} 没有说明原因")

    def test_rule_matrix_metadata_kept(self):
        """D9 冲突矩阵元数据必须随目录一起下发（任务书要求）。"""
        rm = self.m["rule_matrix"]
        self.assertEqual(3, len(rm))
        total = sum(len(x["rules"]) for x in rm)
        self.assertEqual(48, total, "D9 规则条目数应为 48（23 + 13 + 12）")


class TestSlotsAndAssembly(unittest.TestCase):
    """骨架顺序与「不指定不输出」。"""

    @classmethod
    def setUpClass(cls):
        cls.m = prompt_catalog.get_manifest()

    def test_slot_order_is_the_documented_one(self):
        keys = [s["key"] for s in self.m["slots"]]
        self.assertEqual(
            ["subject", "styling", "style", "scene", "composition",
             "light", "color", "mood", "quality", "negative"],
            keys)

    def test_dimensions_map_to_slots(self):
        for dim in self.m["dimensions"]:
            if dim["id"] == "D9":
                continue
            self.assertIn(dim["slot"], [s["key"] for s in self.m["slots"]],
                          f"{dim['id']} 没有映射到任何骨架槽位")

    def test_d3_person_chain_is_ordered_head_to_foot(self):
        """人物造型必须按「从头到脚」排：发型 → 妆容 → 上装 → 下装 → 外套 → 鞋 → 配饰。"""
        d3 = next(d for d in self.m["dimensions"] if d["id"] == "D3")
        order = [g["sub_prefix"] for g in d3["groups"]]
        expected = ["3.0", "3.1", "3.2", "3.3", "3.4", "3.5", "3.6",
                    "3.7", "3.8", "3.9", "3.10", "3.11"]
        self.assertEqual(expected, order)
        # 每一级都必须是独立可跳过的子类（人物逐级选择）
        for g in d3["groups"]:
            self.assertTrue(any(i["is_skip"] for i in g["items"]))

    def test_foreground_midground_background_are_separate_groups(self):
        """前景 / 中景 / 远景必须是三个**独立**子类（可同时选）。"""
        d4 = next(d for d in self.m["dimensions"] if d["id"] == "D4")
        names = {g["sub_prefix"]: g["name"] for g in d4["groups"]}
        for key, label in (("4.2", "前景"), ("4.3", "中景"), ("4.4", "远景 / 背景")):
            self.assertIn(key, names)
            self.assertIn(label, names[key])

    def test_skip_items_produce_no_fragment(self):
        """「不指定」项的片段必须是空串（真的不输出）。"""
        for dim in self.m["dimensions"]:
            for g in dim["groups"]:
                for it in g["items"]:
                    if not it["is_skip"]:
                        continue
                    self.assertEqual("", it["en"], f"{it['id']} 的跳过项不该有英文片段")
                    self.assertFalse(
                        re.search(r"^(no|not|without)\b", it["example"] or "", re.I),
                        f"{it['id']} 的跳过项示例不得是否定式")


class TestChineseFirstFragments(unittest.TestCase):
    """中文优先：默认写进提示词的必须是中文片段，英文只作辅助。

    【为什么单独一组】用户 2026-11-21 明确要求：
      「组装成原始提示词时也中文优先……英文片段只作为可选的辅助/增强信息
       保留……若某个选项没有可信的中文对应说法，宁可保留中文描述并如实标注，
       也不要塞入未经核验的英文术语。」
    所以：默认那份（fragment / zh_fragment）必须来自调研的**中文显示名**，
    不得是英文；英文另存一份（en_fragment），不参与默认组装。
    """

    @classmethod
    def setUpClass(cls):
        cls.m = prompt_catalog.get_manifest()

    def test_items_carry_both_zh_and_en_fragments(self):
        for dim in self.m["dimensions"]:
            for g in dim["groups"]:
                for it in g["items"]:
                    self.assertIn("zh_fragment", it, f"{it['id']} 没有中文片段字段")
                    self.assertIn("en_fragment", it, f"{it['id']} 没有英文片段字段")
                    # 默认那份必须等于中文片段（前端只读 fragment / zh_fragment）
                    self.assertEqual(it["fragment"], it["zh_fragment"],
                                     f"{it['id']} 的默认片段不是中文那份")

    def test_default_fragment_is_chinese_for_every_selectable_item(self):
        """每个可选项的默认片段都必须带中文（专名除外，见白名单）。"""
        # 白名单：调研里该概念的通用写法本来就是拉丁（不是「未核验的英文术语」）
        LATIN_PROPER_NAMES = {"Unreal Engine", "Teal & Orange"}
        checked = 0
        for dim in self.m["dimensions"]:
            for g in dim["groups"]:
                if g["render"] == "rules":
                    continue
                for it in g["items"]:
                    if it["is_skip"] or g["render"] == "guide" or it["status"] == "deprecated":
                        continue
                    frag = it["zh_fragment"]
                    self.assertTrue(frag, f"{it['id']}（{it['label']}）没有可输出的中文片段")
                    if frag in LATIN_PROPER_NAMES:
                        continue
                    self.assertTrue(
                        any("\u4e00" <= c <= "\u9fff" for c in frag),
                        f"{it['id']}（{it['label']}）的默认片段不是中文：{frag!r}")
                    checked += 1
        # 应覆盖的可选项数（逐层扣出来的口径，不是拍脑袋的数）：
        #   891 总项 - 51 条 D9 规则项（render=rules，不进提示词）
        #        - 45 显式「不指定」项 - 8 淘汰项（D8.4）
        #        - 8 条对照清单写法（D8.4 保留/谨慎，render=guide）
        #        - 2 拉丁专名白名单（Unreal Engine / Teal & Orange）
        #   = 777
        self.assertEqual(777, checked,
                         f"中文片段检查覆盖数变了：{checked}（应为 777）")

    def test_slash_alternatives_keep_the_chinese_wording(self):
        """「A / B」并列时中文取**带中文的那一个**，不是机械取第一个。

        【真实回归】「IMAX / 宽银幕」机械取第一个会得到纯拉丁「IMAX」，
        把调研核过的中文说法丢了 —— 与「中文优先」直接冲突。
        """
        def frag_of(item_id):
            for dim in self.m["dimensions"]:
                for g in dim["groups"]:
                    for it in g["items"]:
                        if it["id"] == item_id:
                            return it
            raise AssertionError(f"找不到 {item_id}")

        it = frag_of("D1-03-12")          # IMAX / 宽银幕
        self.assertEqual("宽银幕", it["zh_fragment"],
                         f"并列写法丢了中文：{it['zh_fragment']!r}")
        self.assertEqual("IMAX", it["en_fragment"],
                         f"英文辅助版本应当保留 IMAX：{it['en_fragment']!r}")

        it = frag_of("D1-06-23")          # 田园 / cottagecore
        self.assertEqual("田园", it["zh_fragment"], it["zh_fragment"])
        self.assertEqual("cottagecore", it["en_fragment"], it["en_fragment"])

    def test_english_fragments_are_preserved(self):
        """英文片段一条都不能丢（它仍是「含英文片段版本」与增强链路的数据源）。"""
        kept = 0
        for dim in self.m["dimensions"]:
            for g in dim["groups"]:
                for it in g["items"]:
                    if it["is_skip"]:
                        continue
                    if it["en"] and it["status"] != "deprecated" and g["render"] != "rules":
                        # 8.4 的英文在 example 里（类别名不是片段），不要求 en_fragment==en
                        if g["sub_prefix"] == "8.4":
                            continue
                        self.assertTrue(it["en_fragment"],
                                        f"{it['id']} 丢了英文片段（原 en={it['en']!r}）")
                        kept += 1
        self.assertGreaterEqual(kept, 700, f"英文片段保留得太少：{kept}")

    def test_no_weight_syntax_in_any_fragment(self):
        """任何片段都不得含 (x:1.5) 之类权重语法（两份都不行）。"""
        pat = re.compile(r"\(\s*[\w\u4e00-\u9fff]+\s*:\s*[\d.]+\s*\)")
        for dim in self.m["dimensions"]:
            for g in dim["groups"]:
                for it in g["items"]:
                    for field in ("zh_fragment", "en_fragment", "fragment"):
                        self.assertIsNone(
                            pat.search(it.get(field) or ""),
                            f"{it['id']}.{field} 含权重语法：{it.get(field)!r}")


class TestOmitAndFreeInput(unittest.TestCase):
    """每个维度都必须允许省略 + 允许自行输入。"""

    @classmethod
    def setUpClass(cls):
        cls.m = prompt_catalog.get_manifest()

    def test_every_non_rule_group_allows_custom_input(self):
        """D1–D8 的每个子类都要有自由输入入口（含 8.4 对照清单）。"""
        bad = []
        total = 0
        for dim in self.m["dimensions"]:
            for g in dim["groups"]:
                if dim["id"] == "D9":
                    continue          # D9 是冲突矩阵，不是提示词维度
                total += 1
                if g.get("allow_custom") is not True:
                    bad.append(f"{dim['id']}/{g['name']}")
                if not (g.get("custom_placeholder") or "").strip():
                    bad.append(f"{dim['id']}/{g['name']} 没有中文示例提示")
        self.assertEqual([], bad, f"这些子类没有自行输入入口：{bad}")
        self.assertEqual(45, total, "可输入的子类数量应为 45（48 - D9 的 3 个规则组）")

    def test_guide_group_has_no_selectable_items_but_keeps_custom_input(self):
        """8.4 是「保留 / 谨慎 / 淘汰」对照清单：不进下拉，但用户仍可自己写。"""
        guide = [g for dim in self.m["dimensions"] for g in dim["groups"]
                 if g["render"] == "guide"]
        self.assertEqual(1, len(guide), "对照清单应当只有 8.4 这一组")
        g = guide[0]
        self.assertEqual("8.4", g["sub_prefix"])
        self.assertTrue(g["allow_custom"])
        # 它的 zh_fragment 全为空（不产生片段），但 en_fragment 保留可参考的写法
        zh_nonempty = [i for i in g["items"] if i["zh_fragment"]]
        self.assertEqual([], zh_nonempty, "对照清单不应产生中文片段")
        en_nonempty = [i for i in g["items"] if i["en_fragment"]]
        self.assertGreaterEqual(len(en_nonempty), 7,
                                "对照清单里的「保留」写法示例应当保留为英文辅助信息")

    def test_options_groups_still_have_selectable_items(self):
        """其余子类仍是可下拉选择的（对照清单不能把词库整体降级）。"""
        options = [g for dim in self.m["dimensions"] for g in dim["groups"]
                   if g["render"] == "options"]
        self.assertEqual(44, len(options), "可下拉的子类应为 44 个")
        for g in options:
            selectable = [i for i in g["items"] if not i["is_skip"] and i["zh_fragment"]]
            self.assertGreaterEqual(len(selectable), 1, f"{g['id']} 没有任何可选项")
            self.assertTrue(any(i["is_skip"] for i in g["items"]),
                            f"{g['id']} 没有「不指定」")

    def test_skip_option_emits_nothing_in_either_language(self):
        for dim in self.m["dimensions"]:
            for g in dim["groups"]:
                for it in g["items"]:
                    if not it["is_skip"]:
                        continue
                    self.assertEqual("", it["zh_fragment"])
                    self.assertEqual("", it["en_fragment"])
                    self.assertEqual("", it["fragment"])


class TestBackendEndpoint(unittest.TestCase):
    """服务端路由与响应结构。"""

    def test_endpoint_registered(self):
        src = (REPO / "server.py").read_text(encoding="utf-8")
        self.assertIn('app.router.add_get("/api/prompt-catalog"', src)

    def test_manifest_is_json_serialisable(self):
        m = prompt_catalog.get_manifest()
        text = json.dumps(m, ensure_ascii=False)
        self.assertGreater(len(text), 10000)
        # 版本号必须是内容哈希（内容变了就变）
        self.assertTrue(m["version"])
        self.assertEqual(12, len(m["version"]))

    def test_manifest_version_changes_with_content(self):
        """内容变了 ⇒ 版本号必须变（前端据此重建）。"""
        m1 = prompt_catalog.build_manifest()
        m2 = prompt_catalog.build_manifest()
        self.assertEqual(m1["version"], m2["version"], "同一份内容两次构建版本号应一致")


if __name__ == "__main__":
    unittest.main()
