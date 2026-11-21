# -*- coding: utf-8 -*-
"""
参考标签**方言**的回归测试（2026-11-21 现场复验发现的跨层缺陷）。

【这个文件在防什么】
上一轮把参考标签一律写成 H3 官方的 `<Picture N>`，结果**参考生图
（Qwen-Image 2.1 图片编辑）被自己修坏了**：

    增强输出  <Picture 1> / <Picture 2>
    ↓ 提交
    HTTP 400  参考图校验未通过：有 2 张参考图，提示词里必须有
              <image1>..<image2> 占位标记
              （官方 PE-I2I 多图模板要求用 <imageN> 引用每张图）

用户的表现是「我点了增强，增强出来的提示词提交不了」。

【根因】
不同模型族的官方参考标记语法**不是同一套**：
  * H3 视频（t2v / flf / r2v / longfilm）
    -> `<Picture N>` / `<Video N>` / `<Audio N>`
       （官方 skill h3-prompt-writing/references/ref-en.txt）
  * Qwen-Image 2.1 图片编辑（i2i_qwen）
    -> `<imageN>`（小写、无空格）
       （官方 PE-I2I system prompt: "the rewritten instruction MUST use
       <image1>, <image2>, ..."）
  * MiniMax-H3-Image 图片编辑（i2i_h3image）
    -> `<Picture N>`（h3studio/h3image.py 的 H3_IMAGE_PLACEHOLDER）
       该模块明确写了两套语义必须隔离。

【断言口径】
每条断言都由**命名该行为的那条断言**抓住，并且都做过变异验证
（改坏实现 -> 断言必须红）。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from h3studio import enhance_assets as ea  # noqa: E402


# ------------------------------------------------------------------ 官方语法

def test_h3_dialect_uses_official_picture_tags():
    """H3 官方方言：图片是 <Picture N>（带空格）。"""
    assert ea.label_for("image", 1, ea.LABEL_DIALECT_H3) == "<Picture 1>"
    assert ea.label_for("image", 9, ea.LABEL_DIALECT_H3) == "<Picture 9>"
    assert ea.label_for("video", 1, ea.LABEL_DIALECT_H3) == "<Video 1>"
    assert ea.label_for("audio", 1, ea.LABEL_DIALECT_H3) == "<Audio 1>"


def test_qwen_dialect_uses_official_image_tags():
    """Qwen 官方方言：图片是 <imageN>（小写、无空格）。"""
    assert ea.label_for("image", 1, ea.LABEL_DIALECT_QWEN) == "<image1>"
    assert ea.label_for("image", 2, ea.LABEL_DIALECT_QWEN) == "<image2>"
    assert ea.label_for("image", 9, ea.LABEL_DIALECT_QWEN) == "<image9>"


def test_qwen_dialect_keeps_h3_tags_for_non_image():
    """Qwen 没有视频/音频参考槽位；真走到的只可能是图片。

    视频/音频仍返回 H3 官方写法 —— 不编造 Qwen 不存在的语法。
    """
    assert ea.label_for("video", 1, ea.LABEL_DIALECT_QWEN) == "<Video 1>"
    assert ea.label_for("audio", 1, ea.LABEL_DIALECT_QWEN) == "<Audio 1>"


def test_default_dialect_is_h3():
    """不传 dialect 时走 H3（与修复前的默认行为一致，不破坏视频面板）。"""
    assert ea.label_for("image", 1) == "<Picture 1>"


# ------------------------------------------------------------------ 方言隔离

def test_qwen_pattern_does_not_accept_picture_tag():
    """【关键断言】Qwen 方言下 <Picture N> **不算**命中。

    为什么必须这样：如果这里放宽，模型写错方言会被判「通过」，
    而真正提交时仍会被 400 拒掉 —— 变成「静默假通过」，比报错更糟。
    """
    assert ea.extract_reference_labels("用 <Picture 1> 和 <Picture 2>",
                                      ea.LABEL_DIALECT_QWEN) == []
    assert ea.has_reference_label("<Picture 1>", ea.LABEL_DIALECT_QWEN) is False


def test_h3_pattern_does_not_accept_image_tag():
    """H3 方言下 <image1> 不算命中（那是 Qwen 的写法）。"""
    assert ea.extract_reference_labels("用 <image1>", ea.LABEL_DIALECT_H3) == []
    assert ea.has_reference_label("<image1>", ea.LABEL_DIALECT_H3) is False


def test_qwen_pattern_accepts_image_tag_with_space():
    """模型写 <image 1>（带空格）也算命中 —— 能对上号就行。"""
    assert ea.extract_reference_labels("用 <image 1>", ea.LABEL_DIALECT_QWEN) \
        == ["<image 1>"]


# ------------------------------------------------------------------ 归一化比较

def test_missing_labels_normalizes_whitespace():
    """模型写 <image 1>、我们分配 <image1> -> 不算缺（归一化后相等）。

    否则一次成功的增强会被误报成「缺参考标签」。
    """
    found = ea.extract_reference_labels("参考 <image 1> 和 <image 2>",
                                        ea.LABEL_DIALECT_QWEN)
    assert ea.missing_labels(["<image1>", "<image2>"], found,
                             ea.LABEL_DIALECT_QWEN) == []


def test_missing_labels_detects_wrong_dialect():
    """模型写了 H3 的 <Picture 1>，Qwen 方言下要判「缺 <image1>」。"""
    found = ea.extract_reference_labels("参考 <Picture 1>", ea.LABEL_DIALECT_QWEN)
    assert ea.missing_labels(["<image1>"], found, ea.LABEL_DIALECT_QWEN) \
        == ["<image1>"]


def test_missing_labels_detects_partial():
    """只写了一个标签 -> 另一个要判缺（不能「有标签就算过」）。"""
    found = ea.extract_reference_labels("参考 <image1>", ea.LABEL_DIALECT_QWEN)
    assert ea.missing_labels(["<image1>", "<image2>"], found,
                             ea.LABEL_DIALECT_QWEN) == ["<image2>"]


def test_normalize_label_preserves_case():
    """归一化只去空白，不改大小写 —— <Picture1> 与 <image1> 不是一回事。"""
    assert ea.normalize_label("<Picture 1>") == "<Picture1>"
    assert ea.normalize_label("<image 1>") == "<image1>"
    assert ea.normalize_label("<Picture 1>") != ea.normalize_label("<image 1>")


# ------------------------------------------------------------------ 服务端路由

def test_server_dialect_routing_per_mode():
    """服务端按面板模式选方言。

    逐条对应官方语法：
      * i2i_qwen -> qwen（官方 PE-I2I 用 <imageN>）
      * i2i_h3image -> h3（H3-Image 官方用 <Picture N>，
        见 h3studio/h3image.py 的 H3_IMAGE_PLACEHOLDER）
      * 视频面板 -> h3
    """
    import server

    assert server._label_dialect_for_mode("i2i_qwen") == ea.LABEL_DIALECT_QWEN
    assert server._label_dialect_for_mode("i2i_h3image") == ea.LABEL_DIALECT_H3
    for mode in ("flf", "r2v", "t2v", "longfilm", "t2i_qwen", "t2i", "t2a"):
        assert server._label_dialect_for_mode(mode) == ea.LABEL_DIALECT_H3


def test_qwen_ref_validator_accepts_qwen_dialect_output():
    """【端到端口径】Qwen 方言的增强结果必须能过提交侧校验。

    这条断言直接用**提交侧真正的校验函数**
    （h3studio/ref_images.validate_refs）—— 保证增强侧与提交侧
    用的是同一套标记语法，而不是各自为政。
    """
    from h3studio import ref_images as ref_imgs

    refs = [{"asset_id": "asset_a", "missing": False},
            {"asset_id": "asset_b", "missing": False}]

    qwen_prompt = "保持 <image1> 不变，把 <image2> 换成雪山"
    check = ref_imgs.validate_refs(refs, prompt=qwen_prompt, require=True)
    assert check["ok"], check["errors"]

    # 反证：H3 方言的产物在 Qwen 面板**过不了**提交侧校验 ——
    # 这正是本次要修的缺陷，必须能被测试钉住。
    h3_prompt = "保持 <Picture 1> 不变，把 <Picture 2> 换成雪山"
    check_bad = ref_imgs.validate_refs(refs, prompt=h3_prompt, require=True)
    assert not check_bad["ok"]


def test_h3image_ref_validator_accepts_h3_dialect_output():
    """H3-Image 图片编辑用 <Picture N>（与 Qwen 不同，必须隔离）。"""
    from h3studio import h3image

    refs = [{"asset_id": "asset_a", "missing": False},
            {"asset_id": "asset_b", "missing": False}]
    prompt = "保持 <Picture 1> 不变，把 <Picture 2> 换成雪山"
    check = h3image.validate_h3_image_refs(refs, prompt=prompt)
    assert check["ok"], check["errors"]


# ------------------------------------------------------------------ 素材上下文文案

def test_describe_qwen_dialect_states_the_tag_spelling():
    """Qwen 方言下，给模型的素材上下文必须**讲清楚标签写法**。

    为什么：Qwen 官方模板里写着「MUST use <image1>...」，
    而通用增强话术可能让模型去写 <Picture N>。必须显式给出写法。
    """
    resolved = {
        "notes": ["<image1> = 参考图「a.png」", "<image2> = 参考图「b.png」"],
        "text_context": "",
        "skipped": [],
        "dialect": ea.LABEL_DIALECT_QWEN,
    }
    text = ea.describe(resolved)
    assert "<image1>" in text
    assert "<image 1>" not in text
    assert "小写 image" in text
    assert "不要改写成" in text


def test_describe_h3_dialect_has_no_qwen_spelling_instruction():
    """H3 方言下不塞 Qwen 的写法说明（避免两套指令打架）。"""
    resolved = {
        "notes": ["<Picture 1> = 首帧「a.png」"],
        "text_context": "",
        "skipped": [],
        "dialect": ea.LABEL_DIALECT_H3,
    }
    text = ea.describe(resolved)
    assert "<Picture 1>" in text
    assert "小写 image" not in text


def test_summary_reports_dialect():
    """摘要要把方言带出来（前端回显用，便于排查）。"""
    resolved = {"labels": ["<image1>"], "dialect": ea.LABEL_DIALECT_QWEN}
    assert ea.summary(resolved)["dialect"] == ea.LABEL_DIALECT_QWEN
    assert ea.summary({})["dialect"] == ea.LABEL_DIALECT_H3


# ------------------------------------------------------------------ 无素材面板不回归

def test_no_assets_context_is_empty():
    """无素材 -> 素材上下文为空串（无素材面板提示词逐字不变）。"""
    assert ea.describe({}) == ""
    assert ea.describe({"images": [], "notes": [], "text_context": "",
                        "items": [], "skipped": [], "labels": []}) == ""


def test_resolve_assets_empty_refs_reports_dialect():
    """空引用时也要带上方言（结构一致，避免调用方 KeyError）。"""
    out = ea.resolve_assets([], dialect=ea.LABEL_DIALECT_QWEN)
    assert out["labels"] == []
    assert out["dialect"] == ea.LABEL_DIALECT_QWEN


# ------------------------------------------------------------------ 源码级防线

def test_server_uses_dialect_for_label_validation():
    """服务端校验输出标签时**必须**传方言。

    不传 = 走默认 H3 方言 = Qwen 面板永远判「缺标签」-> 502。
    """
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "enh_assets.extract_reference_labels(\n            enhanced, label_dialect)" \
        in src or "extract_reference_labels(enhanced, label_dialect)" in src
    assert "enh_assets.missing_labels(" in src


def test_server_resolve_assets_passes_dialect():
    """解析素材时必须把方言传下去。"""
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "_resolve_enhance_assets(\n                asset_refs, label_dialect)" in src \
        or "_resolve_enhance_assets(asset_refs, label_dialect)" in src
