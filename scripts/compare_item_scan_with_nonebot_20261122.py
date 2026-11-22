# -*- coding: utf-8 -*-
"""
**逐字对比**本仓库的 h3studio/item_scan.py 与 nonebot 的 h3_item_enhancer.py。

【为什么要有这个脚本】
「复刻」不能靠读代码读出来 —— 必须**跑同样的输入，比同样的输出**。
本脚本把 nonebot 的模块**只读**导入进来（不改它一个字节），对每一组
输入分别调用两边的实现，逐条断言结果**完全一致**。

不一致的地方会打印出来 —— 那是需要修的地方。
"""
import importlib.util
import json
import sys
from pathlib import Path

NB = Path("/home/ubuntu/nonebotqq/plugins/nonebot_plugin_novelai/h3_item_enhancer.py")
REPO = Path("/root/projects/h3studio-prompt-enhance")
sys.path.insert(0, str(REPO))

spec = importlib.util.spec_from_file_location("nb_item_enhancer", NB)
nb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nb)

from h3studio import item_scan as mine  # noqa: E402

FAILS = []
CHECKS = 0


def cmp(label, fn_nb, fn_mine, *args, **kwargs):
    """跑两边的实现，逐字比结果。"""
    global CHECKS
    CHECKS += 1
    try:
        a = fn_nb(*args, **kwargs)
    except Exception as exc:
        a = f"__EXC__ {type(exc).__name__}: {exc}"
    try:
        b = fn_mine(*args, **kwargs)
    except Exception as exc:
        b = f"__EXC__ {type(exc).__name__}: {exc}"
    if a != b:
        FAILS.append((label, a, b))
        print(f"  ✗ {label}")
        print(f"      nonebot: {a!r}")
        print(f"      mine   : {b!r}")
    else:
        print(f"  ✓ {label}")


print("=== 1. 常量 ===")
cmp("TIER_WORDS", lambda: nb.TIER_WORDS, lambda: mine.TIER_WORDS)
cmp("TIER_ORDER", lambda: nb.TIER_ORDER, lambda: mine.TIER_ORDER)
cmp("BACKGROUND_WORD_FACTOR",
    lambda: nb.BACKGROUND_WORD_FACTOR, lambda: mine.BACKGROUND_WORD_FACTOR)
cmp("KNOWN_TRUTH", lambda: nb.KNOWN_TRUTH, lambda: mine.KNOWN_TRUTH)
cmp("KNOWN_TRUTH_EN", lambda: nb.KNOWN_TRUTH_EN, lambda: mine.KNOWN_TRUTH_EN)
cmp("SCAN_PROMPT", lambda: nb.SCAN_PROMPT, lambda: mine.SCAN_PROMPT)

print("\n=== 2. parse_json_loose ===")
for label, txt in [
    ("plain", '{"a": 1}'),
    ("fenced", '```json\n{"a": 1}\n```'),
    ("garbage", '前言\n{"a": {"b": 2}}\n后记'),
    ("nested", '{"a": {"b": {"c": 3}}}'),
    ("empty", ""),
    ("none", None),
    ("nojson", "no json here"),
    ("unclosed", '{"a": 1'),
    ("trailing_comma", '{"a": 1,}'),
]:
    cmp(f"parse_json_loose({label})", nb.parse_json_loose, mine.parse_json_loose, txt)

print("\n=== 3. pick_truth / pick_truth_en ===")
for name in ("左眼白色眼罩", "深红领结", "水手服上衣", "领口标签", "角色全身",
             "人物整体", "整个人的鞋", "背景霓虹招牌", "不锈钢水壶", "眼睛",
             "随便一个没有命中的东西", ""):
    cmp(f"pick_truth({name!r})", nb.pick_truth, mine.pick_truth, name)
for text in ("a choker collar with label", "a plain shirt",
             "a character wearing an eye patch", "WHITE SATIN BOW at chest"):
    cmp(f"pick_truth_en({text[:24]!r})", nb.pick_truth_en, mine.pick_truth_en, text)
cmp("pick_truth_en allow=False",
    nb.pick_truth_en, mine.pick_truth_en,
    "a character wearing an eye patch", False)

print("\n=== 4. _merge_truth ===")
TRUTHS = [
    ("a white long-sleeved sailor top",
     "A white cropped sailor-style jacket with a dark navy blue collar featuring "
     "double white stripes. The sleeves end in ribbed navy cuffs, and the garment "
     "is adorned with small silver metallic buttons."),
    ("a white perforated medical eyepatch with a regular grid of small round vent holes",
     "a light blue bandage over her eye"),
    ("a white sailor collar with navy piping and twin white stripes",
     "a white sailor collar with navy piping and twin white stripes"),
    ("a gold ankh brooch with a heart-shaped clear gemstone inlay",
     "a golden brooch pinned at the bow center"),
    ("a white rigid collar with a rectangular pale-blue tag reading HEALING in plain sans-serif capitals",
     "choker collar with white uppercase label text 'HOLING'"),
    ("", "a plain description"),
    ("a thing", ""),
]
for truth, vlm in TRUTHS:
    cmp(f"_merge_truth({truth[:26]!r})", nb._merge_truth, mine._merge_truth, truth, vlm)

print("\n=== 5. tier_for ===")
for a, b in (("特写", "特写"), ("特写", "远景"), ("远景", "特写"), ("中景", "近景"),
             ("近景", "中景"), ("远景", "远景"), ("非法", "近景"), ("特写", "非法")):
    cmp(f"tier_for({a},{b})", nb.tier_for, mine.tier_for, a, b)

print("\n=== 6. _strip_negative ===")
for m in ("无文字", "没有孔洞", "illegible", "不锈钢材质", "不透明塑料", "",
          "未标注", "有细密孔洞", "英文 text", "无", "不"):
    cmp(f"_strip_negative({m!r})", nb._strip_negative, mine._strip_negative, m)

print("\n=== 7. _clip_words / _word_count ===")
for s, lo, hi in (("a b c d", 1, 2), ("a b", 1, 5), ("", 1, 5), ("x y z", 0, 0)):
    cmp(f"_clip_words({s!r},{lo},{hi})", nb._clip_words, mine._clip_words, s, lo, hi)
for s in ("a red apple", "it's a red apple", "", "中文不算"):
    cmp(f"_word_count({s!r})", nb._word_count, mine._word_count, s)

print("\n=== 8. build_strength_block（多组 VLM 输出形态）===")
SCANS = [
    # nonebot 自测里的 fake
    {"parsed": {
        "objects": [
            {"name": "左眼白色眼罩", "tier": "近景",
             "english": "a light blue bandage over her eye",
             "text_or_marking": "有细密孔洞"},
            {"name": "深红领结", "tier": "近景",
             "english": "a red bow", "text_or_marking": "无文字"},
            {"name": "背景霓虹招牌", "tier": "远景",
             "english": "a glowing sign", "text_or_marking": ""},
            {"name": "不锈钢水壶", "tier": "中景",
             "english": "a steel kettle", "text_or_marking": "不锈钢材质"},
        ],
        "character_costume": [{"english": "a white sailor top"}],
        "lighting": "cold blue key light from the left",
        "palette": ["#1b2340", "#c02b3a"],
    }},
    # 字段名是 items（5010/dsv41 形态）
    {"parsed": {
        "items": [
            {"name": "台灯", "tier": "中景", "english": "a brass desk lamp",
             "text_or_marking": ""},
            {"name": "书堆", "tier": "远景", "english": "a stack of books",
             "text_or_marking": ""},
        ],
        "character_costume": ["a white sailor blouse with navy trim",
                              "a choker collar with label"],
    }},
    # scene_objects 字段名
    {"parsed": {"scene_objects": [
        {"name": "窗帘", "tier": "远景", "english": "red curtains",
         "text_or_marking": ""}]}},
    # character_costume 全是纯字符串
    {"parsed": {"character_costume": [
        "a white long-sleeved sailor top",
        "a large deep-crimson satin bow with a gold ankh brooch at its centre",
    ]}},
    # 缺 tier 的 costume dict 条目
    {"parsed": {
        "objects": [{"name": "角色全身", "tier": "特写", "english": "a girl",
                     "text_or_marking": ""},
                    {"name": "背景", "tier": "远景", "english": "street",
                     "text_or_marking": ""}],
        "character_costume": [
            {"name": "眼罩", "appearance": "白色医用眼罩",
             "english": "White medical eye patch covering the left eye"},
            {"name": "颈部项圈", "appearance": "浅蓝色硬质项圈",
             "english": "Light blue and white rigid choker collar"},
        ],
    }},
    # 空 / 坏输入
    {"parsed": {}},
    {"parsed": {"objects": "not a list"}},
    {"parsed": None},
]
for i, scan in enumerate(SCANS):
    for shot in (None, "近景", "特写", "远景"):
        cmp(f"build_strength_block#{i} shot_tier={shot}",
            nb.build_strength_block, mine.build_strength_block, scan, shot)
    cmp(f"render_enhancement_section#{i}",
        nb.render_enhancement_section, mine.render_enhancement_section,
        scan, nb.build_strength_block(scan))
    # 用**各自的** strength 渲染（防止一边崩了影响对比）
    cmp(f"render_enhancement_section#{i}(own strength)",
        lambda s=scan: nb.render_enhancement_section(
            s, nb.build_strength_block(s)),
        lambda s=scan: mine.render_enhancement_section(
            s, mine.build_strength_block(s)))

print("\n=== 9. scan_reference 空输入 ===")
import asyncio  # noqa: E402
cmp("scan_reference([])", lambda: nb.scan_reference([]),
    lambda: asyncio.run(mine.scan_reference([])))

print(f"\n{'='*60}")
print(f"对比项：{CHECKS}  不一致：{len(FAILS)}")
if FAILS:
    print("\n不一致清单：")
    for label, a, b in FAILS:
        print(f"  - {label}")
        print(f"      nonebot: {a!r}")
        print(f"      mine   : {b!r}")
    sys.exit(1)
print("全部一致 ✅")
