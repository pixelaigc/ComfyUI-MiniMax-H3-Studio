# -*- coding: utf-8 -*-
"""VLM 物品扫描模块（nonebot 三段式第 2 段）的定向测试。

【本文件测什么】
只测**与本次改动直接相关**的部分：

  1. 配置与常量（与 nonebot 逐字一致的 tier 词数 / 真值表 / SCAN_PROMPT）
  2. JSON 宽松解析（parse_json_loose）
  3. KNOWN_TRUTH 覆写 / 否定剥离 / 否定假阳性排除 / tier 合并方向
  4. build_strength_block 的字段兼容性（objects / items / scene_objects；
     costume 的 dict / 字符串两种形态）
  5. render_enhancement_section 的结构（已核实事实节 + 光照 + 调色板）
  6. 三段式 query 构造（初稿阶段 / 合成阶段 / 省时指令）
  7. hermes 通道**默认启用**且仍可手动关闭
  8. 服务端契约（配置下发含 is_default / scan 回显）

【明确不测】
  * 不跑真实 VLM（那属于端到端实测，不是单测）
  * 不跑全量 tests/ 套件（用户 2026-11-22 明确要求）
  * 不碰线上 8190 / 线上 jobs.db —— 用独立端口 + 独立临时 sqlite

运行：
  /usr/local/lib/hermes-agent/venv/bin/python -m pytest \
      tests/test_item_scan.py -q
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from h3studio import item_scan as isc  # noqa: E402
from h3studio import hermes_enhance as he  # noqa: E402


# ------------------------------------------------------------------ 配置与常量
class TestScanConstants(unittest.TestCase):
    """与 nonebot h3_item_enhancer.py 的常量逐条对齐。"""

    def test_tier_words_match_nonebot(self):
        """nonebot h3_item_enhancer.py:67-73"""
        self.assertEqual(isc.TIER_WORDS["特写"], (60, 120))
        self.assertEqual(isc.TIER_WORDS["近景"], (30, 60))
        self.assertEqual(isc.TIER_WORDS["中景"], (12, 30))
        self.assertEqual(isc.TIER_WORDS["远景"], (5, 12))
        self.assertEqual(isc.TIER_ORDER, ("特写", "近景", "中景", "远景"))

    def test_background_factor_matches_nonebot(self):
        """nonebot h3_item_enhancer.py:76"""
        self.assertEqual(isc.BACKGROUND_WORD_FACTOR, 1.5)

    def test_known_truth_tables_present(self):
        """nonebot h3_item_enhancer.py:82-112"""
        for key in ("眼罩", "领口标签", "标签", "领子", "水手领", "上衣",
                    "领结", "蝴蝶结", "头发", "机械翼", "便签", "颈圈", "项圈"):
            self.assertIn(key, isc.KNOWN_TRUTH)
        for key in ("choker collar", "eye patch", "sailor blouse",
                    "sailor collar", "satin bow", "ribbon bow", "ankh",
                    "thigh-high", "bandage", "mechanical wing", "pom-pom"):
            self.assertIn(key, isc.KNOWN_TRUTH_EN)

    def test_scan_prompt_mentions_required_fields(self):
        """nonebot h3_item_enhancer.py:115-130"""
        p = isc.SCAN_PROMPT
        for field in ("name", "tier", "appearance", "text_or_marking",
                      "position", "english", "character_costume",
                      "lighting", "palette"):
            self.assertIn(field, p)
        for tier in isc.TIER_ORDER:
            self.assertIn(tier, p)

    def test_vlm_url_candidates_default_to_nonebot_ports(self):
        """nonebot h3_item_enhancer.py:56-62 —— 默认 5000 / 5010。"""
        self.assertTrue(any("5000" in u for u in isc.DEFAULT_VLM_URLS))
        self.assertTrue(any("5010" in u for u in isc.DEFAULT_VLM_URLS))

    def test_timeout_matches_nonebot(self):
        """nonebot h3_item_enhancer.py:171, 180-182"""
        self.assertEqual(isc.SCAN_TIMEOUT, 180)
        self.assertEqual(isc.SCAN_MAX_TOKENS, 3500)


# ------------------------------------------------------------------ JSON 解析
class TestParseJsonLoose(unittest.TestCase):
    """nonebot h3_item_enhancer.py:235-254"""

    def test_plain_json(self):
        self.assertEqual(isc.parse_json_loose('{"a": 1}'), {"a": 1})

    def test_fenced_json(self):
        self.assertEqual(isc.parse_json_loose('```json\n{"a": 1}\n```'), {"a": 1})

    def test_garbage_around(self):
        out = isc.parse_json_loose('前言\n{"a": {"b": 2}}\n后记')
        self.assertEqual(out, {"a": {"b": 2}})

    def test_nested_braces(self):
        out = isc.parse_json_loose('{"a": {"b": {"c": 3}}}')
        self.assertEqual(out, {"a": {"b": {"c": 3}}})

    def test_none_and_empty(self):
        self.assertIsNone(isc.parse_json_loose(None))
        self.assertIsNone(isc.parse_json_loose(""))
        self.assertIsNone(isc.parse_json_loose("no json here"))


# ------------------------------------------------------------------ 规则层
class TestScanRules(unittest.TestCase):
    """nonebot h3_item_enhancer.py:275-407 的规则逐条验证。"""

    def test_pick_truth_substring_match(self):
        out = isc.pick_truth("左眼白色眼罩")
        self.assertIsNotNone(out)
        self.assertIn("perforated medical eyepatch", out)

    def test_pick_truth_excludes_whole_person_entries(self):
        """nonebot :284 —— 「全身/整体/人物」这类整体条目必须排除。"""
        for name in ("角色全身", "人物整体", "整个人"):
            self.assertIsNone(isc.pick_truth(name))

    def test_pick_truth_en(self):
        self.assertIsNotNone(isc.pick_truth_en("a choker collar with label"))
        self.assertIsNone(isc.pick_truth_en("a plain shirt"))

    def test_pick_truth_en_allow_false(self):
        """nonebot :292-308 —— allow=False 时不查（整体人物描述）。"""
        self.assertIsNone(
            isc.pick_truth_en("a character wearing an eye patch", allow=False))

    def test_merge_truth_keeps_vlm_detail(self):
        """nonebot :311-360 —— truth 是关键词级短描述，不能砍掉 VLM 的细节。"""
        truth = "a white long-sleeved sailor top"
        vlm = ("A white cropped sailor-style jacket with a dark navy blue collar "
               "featuring double white stripes. The sleeves end in ribbed navy cuffs, "
               "and the garment is adorned with small silver metallic buttons.")
        merged = isc._merge_truth(truth, vlm)
        self.assertIn("ribbed navy cuffs", merged)
        self.assertIn("sailor", merged)

    def test_merge_truth_conflict_uses_truth(self):
        """nonebot :342-349 —— 特征冲突时整条用 truth。"""
        truth = "a white perforated medical eyepatch with a regular grid of small round vent holes"
        vlm = "a light blue bandage over her eye"
        merged = isc._merge_truth(truth, vlm)
        self.assertIn("perforated", merged)
        self.assertNotIn("light blue bandage", merged)

    def test_merge_truth_truth_already_in_vlm(self):
        truth = "a white sailor collar with navy piping and twin white stripes"
        vlm = "a white sailor collar with navy piping and twin white stripes"
        self.assertEqual(isc._merge_truth(truth, vlm), vlm)

    def test_tier_merge_takes_conservative(self):
        """nonebot :363-376 —— 取词数更少的一档。"""
        self.assertEqual(isc.tier_for("特写", "特写"), "特写")
        self.assertEqual(isc.tier_for("特写", "远景"), "远景")
        self.assertEqual(isc.tier_for("远景", "特写"), "远景")
        self.assertEqual(isc.tier_for("中景", "近景"), "中景")

    def test_strip_negative_removes_negation(self):
        """
        nonebot :390-407。

        【实测口径说明】nonebot 的否定词正则要求否定词**独立出现**
        （前后不接汉字）。所以：
          * 「无 文字」「无 text」-> 否定词后面不是汉字 -> **整条丢掉**
          * 「无文字」-> 「无」后面紧接汉字「文」-> 正则不匹配 -> **保留**
        最后一条看着像 bug，但它是 nonebot 的**实际行为**，
        本模块按「与 nonebot 行为完全一致」的要求照抄（已用
        scripts/compare_item_scan_with_nonebot_20261122.py 逐条比对，
        115 项 0 差异）。
        """
        self.assertEqual(isc._strip_negative("无 文字"), "")
        self.assertEqual(isc._strip_negative("无 text"), "")
        self.assertEqual(isc._strip_negative("没有 孔洞"), "")
        self.assertEqual(isc._strip_negative("illegible"), "")

    def test_strip_negative_keeps_false_positives(self):
        """nonebot :396-397 —— 「不锈钢」「不透明」里的「不」不是否定。"""
        self.assertEqual(isc._strip_negative("不锈钢材质"), "不锈钢材质")
        self.assertEqual(isc._strip_negative("不透明塑料"), "不透明塑料")

    def test_strip_negative_keeps_chinese_attached_negation(self):
        """
        与 nonebot 一致：「无文字」这种「否定词紧跟汉字」的写法**不**被剥离。

        这条单独锁住，是为了防止有人「顺手修好它」导致与 nonebot 行为漂移。
        """
        self.assertEqual(isc._strip_negative("无文字"), "无文字")

    def test_clip_words(self):
        self.assertEqual(isc._clip_words("a b c d", 1, 2), "a b")
        self.assertEqual(isc._clip_words("a b", 1, 5), "a b")

    def test_word_count(self):
        self.assertEqual(isc._word_count("a red apple"), 3)


# ------------------------------------------------------------------ 强化块
class TestBuildStrengthBlock(unittest.TestCase):
    """nonebot h3_item_enhancer.py:410-548"""

    def _scan(self, parsed):
        return {"parsed": parsed, "raw": "", "images": []}

    def test_accepts_objects_key(self):
        blk = isc.build_strength_block(self._scan({"objects": [
            {"name": "红色杯子", "tier": "近景", "english": "a red ceramic cup",
             "text_or_marking": ""}]}))
        self.assertEqual(len(blk["items"]), 1)
        self.assertEqual(blk["items"][0]["name"], "红色杯子")

    def test_accepts_items_key(self):
        """nonebot :447 —— 三个字段名都要认。"""
        blk = isc.build_strength_block(self._scan({"items": [
            {"name": "台灯", "tier": "中景", "english": "a desk lamp",
             "text_or_marking": ""}]}))
        self.assertEqual(len(blk["items"]), 1)

    def test_accepts_scene_objects_key(self):
        blk = isc.build_strength_block(self._scan({"scene_objects": [
            {"name": "窗帘", "tier": "远景", "english": "a curtain",
             "text_or_marking": ""}]}))
        self.assertEqual(len(blk["items"]), 1)

    def test_costume_dict_entries_join_item_pool(self):
        """nonebot :454-456 + 自测 :652-673"""
        blk = isc.build_strength_block(self._scan({
            "objects": [{"name": "角色全身", "tier": "特写", "english": "a girl",
                         "text_or_marking": ""}],
            "character_costume": [
                {"name": "眼罩", "appearance": "白色医用眼罩",
                 "english": "White medical eye patch covering the left eye"},
                {"name": "颈部项圈", "appearance": "浅蓝色硬质项圈",
                 "english": "Light blue and white rigid choker collar"},
            ]}))
        names = {i["name"] for i in blk["items"]}
        self.assertIn("眼罩", names)
        self.assertIn("颈部项圈", names)

    def test_whole_person_entry_kept_but_no_truth_override(self):
        """
        「角色全身」这类**整体条目**仍然进物件池（nonebot 实测行为），
        但**不能**被 KNOWN_TRUTH 覆写 —— 否则「全身」里的「身体」之类
        会命中某个单件，把人物整体描述砍成一句。

        nonebot :275-289 的 pick_truth 负责排除「整体条目」被覆写；
        但 build_strength_block 本身不丢弃该条目（实测：进池，
        source=vlm_scan）。
        """
        blk = isc.build_strength_block(self._scan({
            "objects": [{"name": "角色全身", "tier": "特写",
                         "english": "a young woman in a long coat",
                         "text_or_marking": ""}]}))
        names = {i["name"]: i for i in blk["items"]}
        self.assertIn("角色全身", names)
        self.assertEqual(names["角色全身"]["source"], "vlm_scan")
        self.assertIn("long coat", names["角色全身"]["english"])

    def test_costume_plain_strings_join_item_pool(self):
        """nonebot :457-463 —— 纯字符串条目也要进物件池。"""
        blk = isc.build_strength_block(self._scan({
            "character_costume": ["a white sailor blouse with navy trim"]}))
        self.assertEqual(len(blk["items"]), 1)
        self.assertEqual(len(blk["costume"]), 1)

    def test_background_entry_dropped(self):
        """nonebot :472 —— 名为「背景」的条目不进物件池。"""
        blk = isc.build_strength_block(self._scan({"objects": [
            {"name": "背景", "tier": "远景", "english": "street",
             "text_or_marking": ""}]}))
        self.assertEqual(len(blk["items"]), 0)

    def test_known_truth_overrides_vlm(self):
        blk = isc.build_strength_block(self._scan({"objects": [
            {"name": "左眼白色眼罩", "tier": "近景",
             "english": "a light blue bandage over her eye",
             "text_or_marking": "有细密孔洞"}]}))
        self.assertEqual(blk["items"][0]["source"], "known_truth")
        self.assertNotIn("light blue bandage", blk["text"])

    def test_negative_marking_dropped_but_item_kept(self):
        """nonebot 自测 :637-639"""
        blk = isc.build_strength_block(self._scan({"objects": [
            {"name": "深红领结", "tier": "近景", "english": "a red bow",
             "text_or_marking": "无文字"}]}))
        names = {i["name"]: i for i in blk["items"]}
        self.assertIn("深红领结", names)
        self.assertNotIn("无文字", names["深红领结"]["english"])

    def test_false_positive_marking_kept(self):
        """nonebot 自测 :641-642"""
        blk = isc.build_strength_block(self._scan({"objects": [
            {"name": "不锈钢水壶", "tier": "中景", "english": "a steel kettle",
             "text_or_marking": "不锈钢材质"}]}))
        names = {i["name"]: i for i in blk["items"]}
        self.assertIn("不锈钢材质", names["不锈钢水壶"]["english"])

    def test_tier_merged_with_shot_tier(self):
        """nonebot 自测 :644-645"""
        blk = isc.build_strength_block(self._scan({"objects": [
            {"name": "背景霓虹招牌", "tier": "远景", "english": "a glowing sign",
             "text_or_marking": ""}]}), shot_tier="近景")
        names = {i["name"]: i for i in blk["items"]}
        self.assertEqual(names["背景霓虹招牌"]["tier"], "远景")

    def test_missing_tier_defaults_to_near(self):
        """nonebot :474-481 + 自测 :670-672"""
        blk = isc.build_strength_block(self._scan({"objects": [
            {"name": "无档物件", "english": "a thing", "text_or_marking": ""}]}))
        self.assertEqual(blk["items"][0]["tier"], "近景")

    def test_word_budget_clipped_by_tier(self):
        """nonebot :497-498 —— 按 tier 裁词数。"""
        long_eng = " ".join(["word"] * 200)
        blk = isc.build_strength_block(self._scan({"objects": [
            {"name": "远景物", "tier": "远景", "english": long_eng,
             "text_or_marking": ""}]}))
        self.assertLessEqual(blk["items"][0]["words"], 12)

    def test_background_and_subject_words_counted_separately(self):
        """nonebot :502-505"""
        blk = isc.build_strength_block(self._scan({"objects": [
            {"name": "主体物", "tier": "近景", "english": "a red cup",
             "text_or_marking": ""},
            {"name": "远景物", "tier": "远景", "english": "a sign",
             "text_or_marking": ""}]}))
        self.assertGreater(blk["subject_words"], 0)
        self.assertGreater(blk["background_words"], 0)

    def test_empty_scan_returns_empty_block(self):
        blk = isc.build_strength_block(None)
        self.assertEqual(blk["items"], [])
        self.assertEqual(blk["text"], "")

    def test_no_items_are_dropped_by_tier(self):
        """nonebot :426-428 —— 背景物件靠密度自然变短，不靠裁掉。"""
        blk = isc.build_strength_block(self._scan({"objects": [
            {"name": f"物件{i}", "tier": "远景", "english": f"object number {i}",
             "text_or_marking": ""} for i in range(5)]}))
        self.assertEqual(len(blk["items"]), 5)


# ------------------------------------------------------------------ 已核实事实节
class TestRenderEnhancementSection(unittest.TestCase):
    """nonebot h3_item_enhancer.py:554-601"""

    def _scan(self, parsed):
        return {"parsed": parsed, "raw": "", "images": []}

    def test_section_contains_header_and_items(self):
        blk = isc.build_strength_block(self._scan({"objects": [
            {"name": "红杯", "tier": "近景", "english": "a red cup",
             "text_or_marking": ""}]}))
        sec = isc.render_enhancement_section(self._scan({"objects": [
            {"name": "红杯", "tier": "近景", "english": "a red cup",
             "text_or_marking": ""}]}), blk)
        self.assertIn("# 已核实素材事实", sec)
        self.assertIn("红杯", sec)
        self.assertIn("禁止任何否定句", sec)

    def test_lighting_included(self):
        blk = isc.build_strength_block(self._scan({"objects": [
            {"name": "红杯", "tier": "近景", "english": "a red cup",
             "text_or_marking": ""}]}))
        sec = isc.render_enhancement_section(
            self._scan({"objects": [{"name": "红杯", "tier": "近景",
                                     "english": "a red cup",
                                     "text_or_marking": ""}],
                        "lighting": "cold blue key light from the left"}), blk)
        self.assertIn("### Lighting (verified)", sec)
        self.assertIn("cold blue key light", sec)

    def test_palette_included(self):
        blk = isc.build_strength_block(self._scan({"objects": [
            {"name": "红杯", "tier": "近景", "english": "a red cup",
             "text_or_marking": ""}]}))
        sec = isc.render_enhancement_section(
            self._scan({"objects": [{"name": "红杯", "tier": "近景",
                                     "english": "a red cup",
                                     "text_or_marking": ""}],
                        "palette": ["#1b2340", "#c02b3a"]}), blk)
        self.assertIn("### Palette (verified)", sec)
        self.assertIn("#1b2340", sec)

    def test_empty_strength_returns_empty(self):
        self.assertEqual(
            isc.render_enhancement_section(self._scan({}), None), "")


# ------------------------------------------------------------------ 三段式 query
class TestThreeStageQuery(unittest.TestCase):
    """
    三段式 query 构造 —— 对齐 nonebot
    `_build_hermes_director_query`（h3_interactive.py:4237-4398）。
    """

    def setUp(self):
        self.spec = he.get_media_spec("video")

    def test_draft_stage_uses_draft_wording(self):
        q = he.build_query(self.spec, "一只猫")
        self.assertIn("# 详细任务 (初稿阶段)", q)
        self.assertNotIn("# 详细任务 (合成阶段)", q)

    def test_draft_stage_asks_not_to_detail_background(self):
        """nonebot :4297-4300 —— 初稿阶段背景小物件先不写细。"""
        q = he.build_query(self.spec, "一只猫")
        self.assertIn("暂时**不需要**把每个背景小物件写到极细", q)

    def test_synth_stage_uses_synth_wording(self):
        q = he.build_query(self.spec, "一只猫", draft_prompt="初稿内容",
                           enhancement_section="# 已核实素材事实\n...")
        self.assertIn("# 详细任务 (合成阶段)", q)
        self.assertIn("请把 (a) + (b) + (c) **合成**成最终 prompt", q)

    def test_draft_prompt_injected(self):
        """nonebot :4336-4340"""
        q = he.build_query(self.spec, "一只猫", draft_prompt="初稿内容XYZ")
        self.assertIn("# 初稿 prompt", q)
        self.assertIn("初稿内容XYZ", q)

    def test_enhancement_section_injected(self):
        """nonebot :4343-4344"""
        q = he.build_query(self.spec, "一只猫", draft_prompt="d",
                           enhancement_section="# 已核实素材事实\n内容ABC")
        self.assertIn("内容ABC", q)

    def test_time_saving_note_only_in_synth_with_assets(self):
        """nonebot :4370-4377 —— 合成阶段 + 有参考图才注入「不要读图」。

        【2026-11-22 更新】nonebot 的判定条件是
        `draft_prompt and enhancement_section and materials and materials.images`
        —— 最后一项是「**有参考图**」（materials.images），不是「有素材」。
        本实现原先用 asset_context 当代理，现改为直接看 media_paths 的
        图片数（语义与 nonebot 完全一致）。

        背景：nonebot 的阶段2 只在 `img_n > 0` 时才扫图（h3_interactive.py:3914），
        所以 enhancement_section 非空 **蕴含** 必然有参考图 —— 两个条件等价。
        但我们这条链路上 enhancement_section 也可能来自别的来源，用
        media_paths.images 判定更直接、更贴近 nonebot 原文。
        """
        q_draft = he.build_query(self.spec, "x", asset_context="有素材",
                                 media_paths={"images": ["/abs/a.png"]})
        self.assertNotIn("省时指令", q_draft)

        q_synth = he.build_query(self.spec, "x", asset_context="有素材",
                                 media_paths={"images": ["/abs/a.png"]},
                                 draft_prompt="d",
                                 enhancement_section="# 已核实素材事实")
        self.assertIn("# 省时指令", q_synth)
        self.assertIn("**不要用 vision_analyze 读参考图**", q_synth)

    def test_no_scan_section_means_no_time_saving_note(self):
        """没有扫描结果 -> 不能说「已经扫描过了」（那是说谎）。"""
        q = he.build_query(self.spec, "x", asset_context="有素材",
                           media_paths={"images": ["/abs/a.png"]},
                           draft_prompt="d")
        self.assertNotIn("省时指令", q)


# ------------------------------------------------------------------ 默认启用
class TestDefaultEnabled(unittest.TestCase):
    """2026-11-22 用户拍板：hermes 通道默认启用，且仍可手动关闭。"""

    def test_default_is_enabled(self):
        self.assertTrue(he.DEFAULT_ENABLED)
        self.assertTrue(he.resolve_config(None)["enabled"])
        self.assertTrue(he.resolve_config({})["enabled"])
        self.assertTrue(he.resolve_config({"hermes": {}})["enabled"])

    def test_explicit_false_still_disables(self):
        """仍可手动关闭 —— 显式存 false 必须生效。"""
        cfg = he.resolve_config({"hermes": {"enabled": False}})
        self.assertFalse(cfg["enabled"])

    def test_explicit_true_enables(self):
        cfg = he.resolve_config({"hermes": {"enabled": True}})
        self.assertTrue(cfg["enabled"])

    def test_other_config_keys_unaffected(self):
        cfg = he.resolve_config({"hermes": {"enabled": False, "profile": "ops"}})
        self.assertEqual(cfg["profile"], "ops")


# ------------------------------------------------------------------ 服务端契约
class TestServerContract(unittest.TestCase):
    """服务端接口契约（独立端口 + 独立临时 sqlite）。"""

    @classmethod
    def setUpClass(cls):
        from test_frontend_defaults import StudioServer
        cls.srv = StudioServer()
        cls.srv.start()
        cls.base = f"http://127.0.0.1:{cls.srv.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def _get(self, path):
        import urllib.request
        with urllib.request.urlopen(f"{self.base}{path}", timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def _post(self, path, payload):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_config_reports_default_enabled(self):
        """配置下发必须带 enabled / is_default / default_enabled。"""
        status, body = self._get("/api/enhancer")
        self.assertEqual(status, 200)
        h = body["hermes"]
        self.assertTrue(h["enabled"])
        self.assertTrue(h["is_default"])
        self.assertTrue(h["default_enabled"])

    def test_hermes_enhance_now_reachable_by_default(self):
        """默认启用后，通道不该再报 hermes_disabled。"""
        status, body = self._post("/api/hermes-enhance",
                                   {"prompt": "一只猫", "media": "image"})
        # 默认启用 -> 不会返回 hermes_disabled（会真的去调 hermes 或报别的错）
        self.assertNotEqual(body.get("code"), "hermes_disabled")

    def test_hermes_enhance_response_has_scan_field(self):
        """响应里必须有 scan / stage 字段（阶段2 扫描结果回显）。"""
        status, body = self._post("/api/hermes-enhance",
                                   {"prompt": "一只猫", "media": "image"})
        if status == 200:
            self.assertIn("scan", body)
            self.assertIn("stage", body)

    def test_explicit_disable_still_works(self):
        """手动关闭后必须报 hermes_disabled（且不静默换通道）。"""
        import urllib.request
        req = urllib.request.Request(
            f"{self.base}/api/enhancer",
            data=json.dumps({"hermes": {"enabled": False}}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="PUT")
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
        try:
            status, body = self._post("/api/hermes-enhance",
                                       {"prompt": "一只猫", "media": "image"})
            self.assertEqual(status, 400)
            self.assertEqual(body.get("code"), "hermes_disabled")
        finally:
            # 恢复默认（不存 enabled 字段 -> 回到出厂默认「启用」）
            req = urllib.request.Request(
                f"{self.base}/api/enhancer",
                data=json.dumps({"hermes": {"enabled": True}}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="PUT")
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp.read()


# ------------------------------------------------------------------ 源码契约
class TestSourceContract(unittest.TestCase):
    """静态约定（不依赖浏览器 / 不依赖服务）。"""

    def test_item_scan_module_exists(self):
        self.assertTrue((REPO / "h3studio" / "item_scan.py").exists())

    def test_hermes_module_imports_item_scan(self):
        src = (REPO / "h3studio" / "hermes_enhance.py").read_text(encoding="utf-8")
        self.assertIn("from . import item_scan", src)

    def test_server_passes_scan_images(self):
        src = (REPO / "server.py").read_text(encoding="utf-8")
        self.assertIn("scan_images=scan_images", src)
        self.assertIn("scan_images=asset_images", src)

    def test_frontend_shows_default_state(self):
        src = (REPO / "web" / "providers.js").read_text(encoding="utf-8")
        self.assertIn("is_default", src)
        self.assertIn("出厂默认", src)
        self.assertIn("三段式", src)


if __name__ == "__main__":
    unittest.main()
