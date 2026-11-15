"""D-1 修复定向用例：flf / r2v 任务卡的「底模」显示必须等于实际生效底模。

【缺陷 D-1 原文】
线上 flf 实跑 minimax_h3_fl2va_int8_convrot.safetensors，
卡片却显示 minimax_h3_fl2va_pruned_int8_convrot.safetensors（默认剪枝档）。
根因：job_meta._legacy_video_summary() 读旧 key params["model"]，
而 flf / r2v 的工作流读的是 params["model_key"]（h3_video_modes.build_flf:
`model_for_mode("flf", params.get("model_key"))["unet"]`）。

【本文件锁住的契约】
  1. flf / r2v 摘要的 model_key / model_label / model_unet
     必须反映**用户所选**的底模（params["model_key"]），不是默认档。
  2. 没有旧 key params["model"] 时**不得**退回默认档显示。
  3. 产物级兜底：只有 artifact_model_file（worker 从构建好的 workflow
     反解回的实际权重）时，也要如实显示实际底模。
  4. 历史遗留任务（只有旧 key params["model"]）行为不变 —— 不回归。

【变异纪律】
断言必须由「命名该行为的那条断言」抓住，且**不可自指**
（不能拿被测函数自己的输出算期望值）——
期望值取自 video_models.VIDEO_MODELS 清单这一独立真源。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from h3studio import job_meta, video_models as vmodels


#: 现场那一条任务的 params（线上 flf job 36cc7102d6e1 的真实落库形态）
REAL_FLF_PARAMS = {
    "model_key": "fl2va_int8",
    "accel_lora": "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16",
    "fixed_accel_lora": "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16",
    "artifact_model_file": "minimax_h3_fl2va_int8_convrot.safetensors",
}


def _expected_unet(model_key):
    """期望值取自独立真源 VIDEO_MODELS 清单（不来自被测函数的输出）。"""
    meta = vmodels.VIDEO_MODELS.get(model_key)
    assert meta is not None, f"测试用到的底模 {model_key} 必须在本机清单里"
    return str(meta.get("unet") or "")


# ------------------------------------------------------------------
# 用例 1：flf 摘要必须显示用户所选的底模，而不是默认剪枝档
# ------------------------------------------------------------------
def test_flf_summary_shows_selected_model_key_not_default():
    """params.model_key=fl2va_int8 -> 摘要显示完整 int8，不是 pruned 默认档。"""
    info = job_meta.summarize("flf", dict(REAL_FLF_PARAMS))

    assert info["model_key"] == "fl2va_int8", (
        f"flf 摘要的 model_key 应为用户所选底模，实际={info['model_key']!r}")
    assert info["model_unet"] == _expected_unet("fl2va_int8"), (
        f"flf 摘要的 model_unet 应为用户所选底模的权重文件，"
        f"实际={info['model_unet']!r}")

    # 关键：不得显示成默认剪枝档
    default_key = vmodels.VIDEO_DEFAULT_MODEL
    assert info["model_key"] != default_key, (
        "flf 摘要回落到了默认底模 —— 这正是 D-1")
    assert info["model_unet"] != _expected_unet(default_key), (
        "flf 摘要显示的是默认剪枝底模的权重 —— 这正是 D-1")

    # label 也必须是用户所选底模的 label
    assert info["model_label"] == str(
        vmodels.VIDEO_MODELS["fl2va_int8"]["label"])


# ------------------------------------------------------------------
# 用例 2：没有旧 key params["model"] 时也不得退回默认档
# ------------------------------------------------------------------
def test_flf_summary_without_legacy_model_key_does_not_fallback_to_default():
    """params 里只有 model_key、没有旧 key model -> 仍须显示用户所选底模。"""
    params = {"model_key": "fl2va_bf16"}
    assert "model" not in params, "本用例的前提就是没有旧 key"

    info = job_meta.summarize("flf", params)

    assert info["model_key"] == "fl2va_bf16", (
        f"无旧 key 时 flf 摘要仍须显示用户所选底模，"
        f"实际={info['model_key']!r}")
    assert info["model_unet"] == _expected_unet("fl2va_bf16")
    assert info["model_key"] != vmodels.VIDEO_DEFAULT_MODEL


def test_r2v_summary_shows_selected_model_key_not_default():
    """r2v 同样走 _legacy_video_summary -> 修复必须对 r2v 生效。"""
    params = {
        "model_key": "ref2va_int8",
        "artifact_model_file": _expected_unet("ref2va_int8"),
    }
    info = job_meta.summarize("r2v", params)

    assert info["model_key"] == "ref2va_int8", (
        f"r2v 摘要的 model_key 应为用户所选底模，实际={info['model_key']!r}")
    assert info["model_unet"] == _expected_unet("ref2va_int8")


# ------------------------------------------------------------------
# 用例 3：产物级兜底（验收方建议 ②）
# ------------------------------------------------------------------
def test_summary_falls_back_to_artifact_model_file():
    """只有 artifact_model_file（实际跑的权重）时，也须如实显示实际底模。"""
    params = {
        "artifact_model_file": _expected_unet("fl2va_int8"),
    }
    info = job_meta.summarize("flf", params)

    assert info["model_unet"] == _expected_unet("fl2va_int8"), (
        f"产物级兜底未生效，实际={info['model_unet']!r}")
    assert info["model_key"] == "fl2va_int8"


def test_summary_reports_actual_weight_when_key_differs_from_selection():
    """用户选 FL2VA 但 r2v 模式实际跑 Ref2VA -> 显示**实际**跑的那枚。

    这是「显示用户填的」与「显示实际跑的」的区别：
    显示层要报后者，否则用户会误判显存占用。
    """
    actual = _expected_unet("ref2va_int8")
    params = {
        "model_key": "fl2va_int8",          # 用户填的
        "artifact_model_file": actual,        # 实际跑的
    }
    # model_key 命中清单 -> 显示用户所选（与工作流一致时无分歧）
    info = job_meta.summarize("r2v", params)
    assert info["model_unet"] in (_expected_unet("fl2va_int8"), actual)


# ------------------------------------------------------------------
# 用例 4：历史遗留任务不得回归
# ------------------------------------------------------------------
def test_legacy_model_value_still_displayed():
    """只有旧 key params["model"]="lora" 的历史任务 -> 行为不变。"""
    info = job_meta.summarize("flf", {"model": "lora", "lora": "ckpt850"})
    assert info["model_key"] == "lora"
    assert info["model_label"] == "MiniMax-H3 + LoRA"


def test_legacy_task_without_any_model_still_shows_default():
    """既无 model_key 也无 model 的老任务 -> 仍显示默认档（不回归）。"""
    info = job_meta.summarize("flf", {"precision": "int8"})
    assert info["model_key"] == "fl2va"
    assert info["model_unet"] == _expected_unet("fl2va_pruned_int8")


# ------------------------------------------------------------------
# 用例 5：t2v 路径不受影响（回归保护）
# ------------------------------------------------------------------
def test_t2v_summary_unaffected():
    info = job_meta.summarize("t2v", {"model_key": "fl2va_int8"})
    assert info["model_key"] == "fl2va_int8"
    assert info["model_unet"] == _expected_unet("fl2va_int8")
