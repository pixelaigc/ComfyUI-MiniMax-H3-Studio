"""D-4 修复定向用例：卡片显示的「加速 LoRA 加载节点」必须等于工作流实际使用的节点。

【缺陷 D-4 原文】
全量扫 534 个 job，26 条卡片显示加载节点 `MiniMaxH3TurboLoRA`，
而工作流实际用的是核心 `LoraLoaderModelOnly`
（LightX2V×13 / DMAD×9 / 更好运动×2 / SLA×1 / 中文名×1）。

根因：存量落库数据的 `params["accel_lora"]` 存的是**权重名形式**
（实测 253/253 个 job 都是裸名，如
`minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16`），
不是目录 key -> `accel_lora_meta()` 返回 None ->
`detect_accel_lora_loader("")` 走「读不到文件 -> 保守回落 Turbo 节点」
分支 -> **显示 Turbo 节点**（与实际相反）。

工作流实际挂载正确（history 逐条反查 class 确认权重确实生效），
但该字段正是排查静默失效的依据，显示反了直接违反
「前端选什么就加载什么」的承诺。

【本文件锁住的契约】
  1. 权重名形式（含目录前缀 / 含扩展名 / 裸名）都能归一到目录 key，
     归一后 detect 出的加载节点与工作流实际一致。
  2. 归一不到 -> 显示「未知」，**不**回落 Turbo 节点
     （回落即谎报：把「不知道」说成「用 Turbo 节点加载」）。
  3. 同时有旧 key params["lora"] 与新字段 params["accel_lora"] 时，
     加载节点按**新字段**算（工作流实际挂的是新字段那枚）。
  4. Turbo 系两枚仍显示 Turbo 节点（不得被误改成核心节点）。

【变异纪律】
期望值取自 `video_models` 清单与**工作流构建器实测结果**这一独立真源，
不拿被测函数自己的输出算期望（不自指）。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from h3studio import job_meta, video_models as vmodels


#: 存量落库数据的实际形态（实测：params["accel_lora"] 存裸权重名）
BARE_LIGHTX2V = "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16"
BARE_DMAD = "dmad_minimax_h3_4step_full_critic_comfyui"
BARE_BETTER_MOTION = "h3_better_motion_720x1280_w0.4-0.8_s15-30_vpakarinen_v1"
BARE_CKPT850 = "minimax_h3_turbo_4step_ema_ckpt850"


def _expected_loader(key):
    """期望加载节点 = 清单里该枚的真实权重名 detect 出来的结果。

    这是**独立真源**（video_models 清单 + 实测键名形态），
    不是被测函数的输出。
    """
    meta = vmodels.accel_lora_meta(key)
    assert meta is not None, f"测试用到的 {key} 必须在本机清单里"
    return vmodels.detect_accel_lora_loader(str(meta.get("name") or ""))


# ------------------------------------------------------------------
# 用例 1：权重名形式 -> 归一到目录 key + 加载节点正确
# ------------------------------------------------------------------
def test_bare_weight_name_resolves_to_catalog_key():
    """裸权重名形态能归一到目录 key（D-4 根因所在）。"""
    assert job_meta._accel_lora_key_for_any_form(BARE_LIGHTX2V) \
        == "lightx2v_4step_768p"
    assert job_meta._accel_lora_key_for_any_form(BARE_DMAD) \
        == "dmad_4step_critic"
    assert job_meta._accel_lora_key_for_any_form(BARE_BETTER_MOTION) \
        == "better_motion"
    assert job_meta._accel_lora_key_for_any_form(BARE_CKPT850) == "ckpt850"


def test_bare_weight_name_loader_matches_workflow():
    """裸权重名 -> 加载节点与工作流实际一致（核心断言）。

    工作流侧 `video_workflow._resolve_accel_chain` 用的是**清单里的
    真实权重名**去 detect；显示侧必须得到同一个节点。
    """
    for bare, key in ((BARE_LIGHTX2V, "lightx2v_4step_768p"),
                       (BARE_DMAD, "dmad_4step_critic"),
                       (BARE_BETTER_MOTION, "better_motion")):
        expected = _expected_loader(key)
        assert job_meta._accel_lora_loader_of(bare) == expected, (
            f"裸权重名 {bare!r} 的加载节点应与工作流一致"
            f"（{expected}），实际={job_meta._accel_lora_loader_of(bare)!r}")
        # 关键：这枚在清单里是核心节点，不能显示成 Turbo
        assert job_meta._accel_lora_loader_of(bare) != vmodels.ACCEL_LOADER_TURBO, (
            f"裸权重名 {bare!r} 被显示成 Turbo 节点 —— 这正是 D-4")


def test_full_weight_filename_with_dir_prefix_loader():
    """完整权重文件名（含目录前缀）同样归一正确。"""
    raw = "h3_lightx2v/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors"
    assert job_meta._accel_lora_loader_of(raw) == _expected_loader(
        "lightx2v_4step_768p")


def test_catalog_key_still_works():
    """目录 key 形态不受影响（回归保护）。"""
    assert job_meta._accel_lora_loader_of("lightx2v_4step_768p") \
        == _expected_loader("lightx2v_4step_768p")
    assert job_meta._accel_lora_loader_of("ckpt850") \
        == _expected_loader("ckpt850")


# ------------------------------------------------------------------
# 用例 2：Turbo 系仍显示 Turbo 节点（不得误改）
# ------------------------------------------------------------------
def test_turbo_family_still_shows_turbo_loader():
    """Turbo 系两枚仍显示 Turbo 节点。"""
    assert job_meta._accel_lora_loader_of(BARE_CKPT850) \
        == vmodels.ACCEL_LOADER_TURBO
    assert job_meta._accel_lora_loader_of("minimax_h3_turbo_v4_step600_ema") \
        == vmodels.ACCEL_LOADER_TURBO


# ------------------------------------------------------------------
# 用例 3：归一不到 -> 「未知」，不回落 Turbo
# ------------------------------------------------------------------
def test_unknown_lora_shows_unknown_not_turbo():
    """认不出来的标识 -> 显示「未知」，绝不回落 Turbo 节点。"""
    result = job_meta._accel_lora_loader_of("根本不存在的LoRA权重")
    assert result == job_meta.ACCEL_LOADER_UNKNOWN
    assert result != vmodels.ACCEL_LOADER_TURBO, (
        "认不出来时回落成 Turbo 节点 —— 那正是 D-4 的谎报形态")


def test_empty_value_shows_unknown():
    """空串 -> 未知（不编造）。"""
    assert job_meta._accel_lora_loader_of("") == job_meta.ACCEL_LOADER_UNKNOWN


# ------------------------------------------------------------------
# 用例 4：摘要层端到端（旧 key + 新字段同时存在时按新字段算）
# ------------------------------------------------------------------
def test_summary_loader_uses_new_field_when_both_keys_present():
    """同时有旧 key lora 与新字段 accel_lora -> 加载节点按新字段算。

    线上 job 09baa05e717a 的真实形态：
      lora="ckpt850"（历史遗留）
      accel_lora="minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16"
    工作流实际挂的是**新字段**那枚 -> 加载节点必须是核心节点。
    """
    params = {
        "model": "lora",
        "lora": "ckpt850",                     # 历史遗留旧 key
        "accel_lora": BARE_LIGHTX2V,           # 实际生效的那枚
        "accel_loras": [{"key": BARE_LIGHTX2V, "strength": 1.0, "enabled": True}],
        "fixed_accel_lora": BARE_LIGHTX2V,
    }
    info = job_meta.summarize("r2v", params)
    assert info["accel_lora_loader"] == _expected_loader("lightx2v_4step_768p"), (
        f"同时有两套字段时加载节点应按新字段算，实际={info['accel_lora_loader']!r}")
    assert info["accel_lora_loader"] != vmodels.ACCEL_LOADER_TURBO


def test_summary_loader_for_bare_weight_name_job():
    """线上 failed/done 存量 job 的形态：裸权重名 + 无旧 key。"""
    params = {
        "model_key": "fl2va_int8",
        "accel_lora": BARE_DMAD,
        "accel_loras": [{"key": BARE_DMAD, "strength": 1.0, "enabled": True}],
    }
    info = job_meta.summarize("flf", params)
    assert info["accel_lora_loader"] == _expected_loader("dmad_4step_critic")
    assert info["accel_lora_loader"] != vmodels.ACCEL_LOADER_TURBO


def test_t2v_path_loader_also_normalized():
    """t2v 路径同样走归一口径（回归保护）。"""
    params = {
        "model_key": "fl2va_int8",
        "accel_loras": [{"key": BARE_LIGHTX2V, "strength": 1.0, "enabled": True}],
    }
    info = job_meta.summarize("t2v", params)
    assert info["accel_lora_loader"] == _expected_loader("lightx2v_4step_768p")


# ------------------------------------------------------------------
# 用例 5：工作流侧不得被改动（保守回落是正确防御）
# ------------------------------------------------------------------
def test_detect_accel_lora_loader_itself_unchanged():
    """工作流侧的保守回落**不动** —— 那是「宁可可见失败」的防御。

    显示层归一是另一件事；不能为了让显示好看而削弱工作流侧防御。
    """
    assert vmodels.detect_accel_lora_loader("") == vmodels.ACCEL_LOADER_TURBO
    assert vmodels.detect_accel_lora_loader("不存在的权重") \
        == vmodels.ACCEL_LOADER_TURBO
