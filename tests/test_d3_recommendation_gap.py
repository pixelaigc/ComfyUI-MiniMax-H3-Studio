"""D-3 修复定向用例：A1 引入的 4 枚加速 LoRA 必须有「作者推荐参数」条目。

【缺陷 D-3 原文】
`_ACCEL_LORA_RECOMMENDATIONS` 只有 8 条，而 `VIDEO_ACCEL_LORAS` +
`VIDEO_ACCEL_LORA_COMBOS` 已有 12 枚 —— 缺
`turbo_4step_cn` / `turbo_4step_cn_ema` /
`lightx2v_4step_v01_comfy` / `lightx2v_sla_768p_root` 四条。
这 4 枚在真机前端 tooltip 里「作者推荐参数」**整段缺失**
（其余 8 枚都有步数/调度器/shift 段）—— 与核心卖点不一致。

【归属】本任务 A1 提交 0616abb 把 accel_loras 从 8 枚扩到 12 枚，
但没同步推荐参数表 -> 欠账属本任务范围。

【本文件锁住的契约】
  1. 12 枚加速 LoRA **每一枚**都有推荐参数条目（数量对齐）。
  2. 每条都有可追溯的 `source`（作者依据），不是编造。
  3. 字段级来源标注正确：作者没给的项标 `not_provided`，
     不得伪装成「作者提供」。
  4. 前端 tooltip 的数据源（options_payload / 前端 FALLBACK manifest）
     都能拿到这 4 枚的推荐参数。

【变异纪律】
期望值取自**权重自身的 __metadata__**（第一手依据）与同族已有条目，
不拿被测函数的输出算期望（不自指）。
"""

import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from h3studio import model_recommendations as mrec
from h3studio import video_models as vmodels

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FALLBACK_JS = os.path.join(REPO, "web", "video-panel-fallback.js")

#: A1 提交 0616abb 引入、本批补齐的 4 枚
NEW_KEYS = (
    "turbo_4step_cn",
    "turbo_4step_cn_ema",
    "lightx2v_4step_v01_comfy",
    "lightx2v_sla_768p_root",
)


# ------------------------------------------------------------------
# 用例 1：每枚加速 LoRA 都有推荐条目（数量对齐）
# ------------------------------------------------------------------
def test_every_accel_lora_has_recommendation_entry():
    """12 枚加速 LoRA 全部有推荐参数条目（这条正是原来的失败用例）。"""
    payload = mrec.accel_lora_recommendations_payload()
    keys = set(vmodels.VIDEO_ACCEL_LORAS) | set(vmodels.VIDEO_ACCEL_LORA_COMBOS)
    missing = [k for k in keys if k not in payload]
    assert not missing, f"缺推荐条目: {missing}"
    assert len(payload) == len(keys), (
        f"推荐条目数 {len(payload)} != 加速 LoRA 数 {len(keys)}")


def test_the_four_new_keys_are_present():
    """A1 引入的 4 枚必须在推荐表里。"""
    payload = mrec.accel_lora_recommendations_payload()
    for key in NEW_KEYS:
        assert key in payload, f"{key} 缺推荐参数条目"
        assert payload[key].get("source"), f"{key} 的推荐条目必须有来源"


# ------------------------------------------------------------------
# 用例 2：来源可追溯，且不是编造
# ------------------------------------------------------------------
def test_each_new_entry_has_traceable_source():
    """每条新条目都要有指向具体文件/规格表的来源。"""
    payload = mrec.accel_lora_recommendations_payload()
    for key in NEW_KEYS:
        source = payload[key]["source"]
        assert source and len(source) > 20, f"{key} 的来源过于简略"
        # 来源必须指向具体的权重文件或作者规格表
        assert ("safetensors" in source
                or "README" in source
                or "supported_models.py" in source), (
            f"{key} 的来源未指向具体文件: {source[:80]!r}")


def test_field_sources_are_honest():
    """字段级来源标注：作者没给的项不得标成「作者提供」。"""
    payload = mrec.accel_lora_recommendations_payload()
    for key in NEW_KEYS:
        entry = payload[key]
        sources = entry.get("field_sources") or {}
        for field in ("steps", "scheduler", "sampler",
                      "shift_video", "shift_audio", "base_model"):
            assert field in sources, f"{key} 缺 {field} 的来源标注"
            value = entry.get(field)
            # 值是「作者未提供」时，来源也必须是 not_provided
            if value == mrec.NOT_PROVIDED:
                assert sources[field] == mrec.NOT_PROVIDED, (
                    f"{key}.{field} 值是「作者未提供」但来源标成 "
                    f"{sources[field]!r} —— 那是谎报")


# ------------------------------------------------------------------
# 用例 3：具体数值与第一手依据一致
# ------------------------------------------------------------------
def test_turbo_cn_family_steps_and_base_model():
    """turbo_4step_cn*：步数 4（权重元数据 sampler_steps）、非剪枝底模。"""
    payload = mrec.accel_lora_recommendations_payload()
    for key in ("turbo_4step_cn", "turbo_4step_cn_ema"):
        entry = payload[key]
        assert entry["steps"] == 4, f"{key} 的步数应为 4（作者 sampler_steps）"
        # 作者明确写了 incompatible_base = pruned_* -> base_model 必须写明
        assert "非剪枝" in entry["base_model"], (
            f"{key} 的 base_model 必须写明非剪枝底模（作者 incompatible_base）")


def test_lightx2v_family_shift_is_six_not_twelve():
    """LightX2V 768p 族 shift 是 6/3（作者规格表），不是 H3 内生的 12/3。"""
    payload = mrec.accel_lora_recommendations_payload()
    for key in ("lightx2v_4step_v01_comfy", "lightx2v_sla_768p_root"):
        entry = payload[key]
        assert entry["shift_video"] == 6.0, (
            f"{key} 的视频 shift 应为 6（作者规格表 768p 档）")
        assert entry["shift_audio"] == 3.0
        assert entry["steps"] == 4, f"{key} 是 4 步蒸馏"


def test_turbo_cn_shift_is_comfyui_value_not_author():
    """turbo_4step_cn* 的 shift 用 ComfyUI 内生值 -> 来源标 comfyui。"""
    payload = mrec.accel_lora_recommendations_payload()
    for key in ("turbo_4step_cn", "turbo_4step_cn_ema"):
        entry = payload[key]
        assert entry["shift_video"] == 12.0
        assert entry["field_sources"]["shift_video"] == "comfyui", (
            f"{key} 的 shift 作者未提供，来源必须标 comfyui")
        # 调度器 / 采样器作者未写 -> 未提供
        assert entry["scheduler"] == mrec.NOT_PROVIDED
        assert entry["sampler"] == mrec.NOT_PROVIDED


def test_same_family_entries_agree():
    """同族两枚的推荐值必须一致（逐项沿用同族来源）。"""
    payload = mrec.accel_lora_recommendations_payload()
    pairs = (("turbo_4step_cn", "turbo_4step_cn_ema"),
             ("lightx2v_4step_v01_comfy", "lightx2v_sla_768p_root"))
    for a, b in pairs:
        for field in ("steps", "scheduler", "sampler",
                      "shift_video", "shift_audio"):
            assert payload[a][field] == payload[b][field], (
                f"{a} 与 {b} 的 {field} 应一致（同族同规格）")


# ------------------------------------------------------------------
# 用例 4：前端数据源能拿到（tooltip 用）
# ------------------------------------------------------------------
def test_options_payload_exposes_new_entries():
    """options_payload（前端下拉的权威来源）含这 4 枚。"""
    payload = vmodels.options_payload()
    by_key = {c["key"]: c for c in payload["accel_loras"]}
    for key in NEW_KEYS:
        assert key in by_key, f"options_payload 的 accel_loras 缺 {key}"


def test_recommendation_payload_used_by_tooltip_has_new_entries():
    """前端 tooltip 用的 recommendations payload 含这 4 枚。"""
    payload = mrec.accel_lora_recommendations_payload()
    for key in NEW_KEYS:
        assert key in payload
        # tooltip 要能渲染出「作者推荐参数」段 -> 至少步数有值
        assert payload[key]["steps"] != mrec.NOT_PROVIDED, (
            f"{key} 的步数缺失 -> tooltip 的推荐参数段会是空的")
