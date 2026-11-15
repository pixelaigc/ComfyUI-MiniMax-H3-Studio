"""D-2 修复定向用例：flf / r2v 任务卡详情模态框的「LoRA」行必须显示用户实际所选。

【缺陷 D-2 原文】
线上 job 95fefee8803e（flf, **failed**）：
  params.accel_loras = [{"key":"lightx2v_4step_768p","strength":1.0,"enabled":true}]
  params.lora（旧 key）= None
  详情模态框显示「ckpt850 ×1.00」—— 与用户所选不符，
  且 worker 失败 -> 永不回写 artifact_loras -> **永不自愈**。

根因：job_meta._legacy_video_summary() 的 LoRA 分支只读旧 key params["lora"]；
新面板提交的是 accel_lora / accel_loras / fixed_accel_lora，被该分支无视。
t2v 走 _t2v_summary()（用 resolve_accel_lora_slot）所以无此问题。

【本文件锁住的契约】
  1. 新面板字段（accel_loras / accel_lora）有值 -> 摘要显示**用户所选**那枚，
     绝不是写死的 ckpt850。
  2. 旧 key params["lora"] 仍按旧 key 显示（历史老任务不许丢 LoRA 显示）。
  3. 明确「不挂加速 LoRA」-> 摘要**不显示**任何 LoRA，也不回落成 ckpt850。
  4. 没有任何 LoRA 字段的历史老任务 -> 仍显示 ckpt850
     （工作流确实挂默认 ckpt850，如实显示；见 video_workflow.
      _resolve_accel_chain -> resolve_accel_lora_slot(include_fixed_fallback=True)
      -> VIDEO_FIXED_ACCEL_LORA_DEFAULT）。

【变异纪律】
断言必须由「命名该行为的那条断言」抓住，且**不可自指**
（不能拿被测函数自己的输出算期望值）——
期望值取自 video_models 清单 / 独立常量这一真源。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from h3studio import job_meta, video_models as vmodels


#: 线上 failed job 95fefee8803e 的真实落库形态（复现条件）
REAL_FAILED_FLF_PARAMS = {
    "model_key": "fl2va_int8",
    "accel_lora": "lightx2v_4step_768p",
    "accel_loras": [{"key": "lightx2v_4step_768p", "strength": 1.0,
                      "enabled": True}],
    "fixed_accel_lora": "lightx2v_4step_768p",
    "accel_lora_strength": 1.0,
}


def _expected_label(key):
    """期望显示名取自独立真源（video_models 的清单 + job_meta 的显示映射）。"""
    meta = vmodels.accel_lora_meta(key)
    assert meta is not None, f"测试用到的加速 LoRA {key} 必须在本机清单里"
    return job_meta._accel_lora_display(key)


# ------------------------------------------------------------------
# 用例 1：新面板字段有值 -> 显示用户所选，不是 ckpt850
# ------------------------------------------------------------------
def test_flf_summary_shows_selected_accel_lora_not_hardcoded_ckpt850():
    """线上 failed job 的复现：accel_loras 有值 -> 显示 lightx2v_4step_768p。"""
    info = job_meta.summarize("flf", dict(REAL_FAILED_FLF_PARAMS))

    assert info["lora_key"] == "lightx2v_4step_768p", (
        f"flf 摘要的 lora_key 应为用户所选加速 LoRA，实际={info['lora_key']!r}")
    assert info["lora_label"] == _expected_label("lightx2v_4step_768p")

    # 关键：不得显示成写死的 ckpt850（D-2 本体）
    assert info["lora_key"] != "ckpt850", (
        "flf 摘要仍显示写死的 ckpt850 —— 这正是 D-2")
    assert info["lora_label"] != "ckpt850", (
        "flf 摘要的 LoRA 行仍是 ckpt850 —— 这正是 D-2")


def test_r2v_summary_shows_selected_accel_lora_not_hardcoded_ckpt850():
    """r2v 同样走 _legacy_video_summary -> 修复必须对 r2v 生效。"""
    params = dict(REAL_FAILED_FLF_PARAMS)
    params["model_key"] = "ref2va_int8"
    info = job_meta.summarize("r2v", params)

    assert info["lora_key"] == "lightx2v_4step_768p"
    assert info["lora_label"] != "ckpt850"


def test_summary_reports_accel_lora_loader_like_t2v():
    """加载节点也要与 t2v 同口径（便于排查「选了但不生效」）。"""
    info = job_meta.summarize("flf", dict(REAL_FAILED_FLF_PARAMS))
    meta = vmodels.accel_lora_meta("lightx2v_4step_768p")
    expected = vmodels.detect_accel_lora_loader(str(meta.get("name") or ""))
    assert info["accel_lora_loader"] == expected


# ------------------------------------------------------------------
# 用例 2：旧 key params["lora"] 仍按旧 key 显示（不许丢历史老任务的显示）
# ------------------------------------------------------------------
def test_legacy_lora_key_still_displayed():
    """只有旧 key lora="ckpt850" 的历史老任务 -> 仍按旧 key 显示。"""
    info = job_meta.summarize("flf", {"lora": "ckpt850", "lora_strength": 1.0})
    assert info["lora_key"] == "ckpt850"
    assert info["lora_label"] == "ckpt850"
    assert info["lora_strength"] == 1.0


def test_legacy_lora_key_v4step600_still_displayed():
    """旧 key v4step600 -> 仍按旧 key 显示。"""
    info = job_meta.summarize("r2v", {"lora": "v4step600"})
    assert info["lora_key"] == "v4step600"
    assert info["lora_label"] == "v4step600"


def test_legacy_prompt_as_lora_still_yields_none():
    """脏值（提示词冒充 LoRA）-> 仍不显示（白名单闸门不回归）。"""
    info = job_meta.summarize("flf", {
        "lora": "integrated_multimodal_description: [Shot 1] ..."})
    assert info["lora_label"] == ""
    assert "integrated_multimodal" not in info["lora_label"]


# ------------------------------------------------------------------
# 用例 3：明确「不挂加速 LoRA」-> 不显示任何 LoRA，不回落 ckpt850
# ------------------------------------------------------------------
def test_explicit_no_accel_lora_shows_nothing_not_ckpt850():
    """accel_loras=[] + accel_lora="" -> 不显示任何 LoRA。"""
    params = {
        "model_key": "fl2va_int8",
        "accel_lora": "",
        "accel_loras": [],
        "fixed_accel_lora": "",
    }
    info = job_meta.summarize("flf", params)

    assert info["lora_label"] == "", (
        f"明确不挂加速 LoRA 时摘要不应显示任何 LoRA，实际={info['lora_label']!r}")
    assert info["lora_key"] == ""
    assert info["lora_strength"] is None
    assert info["lora_label"] != "ckpt850", (
        "明确不挂时回落成 ckpt850 —— 工作流里确实没挂，这是谎报")


def test_explicit_none_accel_lora_empty_string_shows_nothing():
    """只有 accel_lora=""（空串 = 显式不挂）-> 同样不显示。"""
    info = job_meta.summarize("flf", {"model_key": "fl2va_int8",
                                       "accel_lora": ""})
    assert info["lora_label"] == ""
    assert info["lora_key"] == ""


# ------------------------------------------------------------------
# 用例 4：历史老任务（任何 LoRA 字段都没有）-> 仍显示 ckpt850（不回归）
# ------------------------------------------------------------------
def test_legacy_task_without_any_lora_field_still_shows_ckpt850():
    """没有任何 LoRA 字段 -> 工作流确实挂默认 ckpt850，如实显示。"""
    info = job_meta.summarize("flf", {"model_key": "fl2va_pruned_int8"})
    assert info["lora_label"] == "ckpt850"
    assert info["lora_key"] == "ckpt850"


def test_legacy_task_without_model_and_lora_still_shows_ckpt850():
    """连 model 都没有的老任务 -> 仍显示 ckpt850（既有契约）。"""
    info = job_meta.summarize("flf", {"precision": "int8"})
    assert info["lora_label"] == "ckpt850"


# ------------------------------------------------------------------
# 用例 5：t2v 路径不受影响（回归保护）
# ------------------------------------------------------------------
def test_t2v_summary_unaffected():
    """t2v 走 _t2v_summary -> 本次改动不得影响它。"""
    info = job_meta.summarize("t2v", {
        "model_key": "fl2va_int8",
        "accel_loras": [{"key": "dmad_4step_critic", "strength": 1.0,
                         "enabled": True}],
    })
    assert info["lora_key"] == "dmad_4step_critic"
    assert info["lora_label"] == _expected_label("dmad_4step_critic")
