#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7 修复补丁（第三轮）：加速 LoRA 槽位的「key ↔ 权重名」双向归一。

【缺陷 B3（严重）】两处配合出的静默失效：
  1) h3studio/artifact_meta.py::apply_to_params 回写产物元数据时，
     把 `accel_lora` / `fixed_accel_lora` / `accel_loras[].key` 写成
     **权重文件名**（如 dmad_minimax_h3_4step_full_critic_comfyui），
     而不是工作流层认的**目录 key**（dmad_4step_critic）。
  2) h3studio/video_models.py::resolve_accel_lora_slot 拿到的 key
     认不出��清单条目 -> accel_lora_meta(key) 返回 None
     -> 加速 LoRA 链**空编**（工作流里一���加速 LoRA 都不挂），
     但 steps 仍是蒸馏档（8 步）=> 8 步采样的非蒸馏权重
     = 「一条不存在的路径」，且**全程不报错**。

【修法】
  F5（artifact_meta）：回写时只写**认得出目录 key**的值；
     认不出就**不覆盖**已有的槽位（提交时写进去的本来就是正确 key）。
  F7（video_models）：resolve_accel_lora_slot 对「权重名形态」的 key
     做归一 —— 与显示层 job_meta._accel_lora_key_for_any_form 同口径。
     这条让**存量数据**（已经落库的权重名）也能被正确解析。

用法：python3 scripts/r7_apply_fixes3.py [--check]
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHECK_ONLY = "--check" in sys.argv
AM = REPO / "h3studio" / "artifact_meta.py"
VM = REPO / "h3studio" / "video_models.py"


# ---------------------------------------------------------------- F5
AM_OLD = '''    first = loras[0]
    strength = first.get("strength")
    # ---- [2026-11-19 第 7 轮审查] F5 ----
    # 这里原来写的是**权重文件名**（如 dmad_minimax_h3_4step_full_critic_comfyui）。
    # 但工作流层认的是**目录 key**（dmad_4step_critic / ckpt850 ——
    # VIDEO_ACCEL_LORAS 里的那件），拿权重名去查 accel_lora_meta(key)
    # 返回 None -> 加速 LoRA 链**空编**（重试时把模型上挂着的那枚
    # LoRA 悄悄摘掉了，而且**没有任何报错**）。
    #
    # [后果] 认不出来时（来源与其具体体例参考）只写**认得出目录 key 的**值：
    #   * 认得出 -> 写 key（工作流层认得）；
    #   * 认不出 -> **不写**这个槽位（写权重名只会让工作流**默默忘掉**
    #     挂着的 LoRA）。
    slot_key = lora_key_for_weight(str(first.get("raw_name")
                                        or first.get("name") or ""))
    if not slot_key:
        slot_key = str(first.get("name") or "").strip()
        try:
            from . import video_models as _vm
            if not _vm.accel_lora_meta(slot_key):
                slot_key = ""
        except Exception:
            slot_key = ""
    if slot_key:
        params["accel_loras"] = [{
            "key": slot_key,
            "name": first.get("name") or "",
            "strength": strength if strength is not None else 1.0,
            "enabled": True,
        }]
        params["fixed_accel_lora"] = slot_key
        params["accel_lora"] = slot_key
'''

AM_NEW = '''    first = loras[0]
    strength = first.get("strength")
    # ---- [2026-11-19 第 7 轮审查] F5 ----
    # 这里原来写的是**权重文件名**（如 dmad_minimax_h3_4step_full_critic_comfyui）。
    # 但工作流层认的是**目录 key**（dmad_4step_critic / ckpt850 ——
    # VIDEO_ACCEL_LORAS 里的那件），拿权重名去查 accel_lora_meta(key)
    # 返回 None -> 加速 LoRA 链**空编**（重试时把模型上挂着的那枚
    # LoRA 悄悄摘掉了，而且**没有任何报错**）。
    #
    # [修法] 只写**认得出目录 key 的**值：
    #   * 认得出 -> 写 key（工作流层认得）；
    #   * 认不出 -> **不覆盖**已有槽位（提交时写进去的本来就是正确 key；
    #     写成权重名只会让工作流**默默忘掉**挂着的那枚 LoRA）。
    slot_key = lora_key_for_weight(str(first.get("raw_name")
                                        or first.get("name") or ""))
    if slot_key:
        params["accel_loras"] = [{
            "key": slot_key,
            "name": first.get("name") or "",
            "strength": strength if strength is not None else 1.0,
            "enabled": True,
        }]
        params["fixed_accel_lora"] = slot_key
        params["accel_lora"] = slot_key
'''

# ---------------------------------------------------------------- F7
VM_OLD = '''    # 【显式槽位】键存在即为用户显式选择（空串 = 不挂）
    if "accel_lora" in params:
        raw = params.get("accel_lora")
        key = str(raw or "").strip()
        if not key:
            return "", 1.0
        meta = accel_lora_meta(key) or {}
'''

VM_NEW = '''    # 【显式槽位】键存在即为用户显式选择（空串 = 不挂）
    if "accel_lora" in params:
        raw = params.get("accel_lora")
        key = str(raw or "").strip()
        if not key:
            return "", 1.0
        # ---- [2026-11-19 第 7 轮审查] F7 ----
        # 归一「权重名形态」的 key -> 目录 key。
        #
        # [为什么必须] 产物回写（artifact_meta.apply_to_params）与更早的
        # 存量落库数据里，`accel_lora` 存的是**权重文件名**
        # （如 dmad_minimax_h3_4step_full_critic_comfyui），
        # 而这里认的是**目录 key**（dmad_4step_critic）。
        # 认不出 -> accel_lora_meta(key) 返回 None -> 加速 LoRA 链**空编**
        # （工作流里一枚都不挂），但 steps 仍是蒸馏档
        # => 8 步采样的非蒸馏权重 = 「一条不存在的路径」，且不报错。
        #
        # [与显示层同口径] 显示层 job_meta._accel_lora_key_for_any_form
        # 早就做了这件事 —— 工作流层没做，两边口径不一致。
        key = _normalize_accel_key(key)
        meta = accel_lora_meta(key) or {}
'''

# F7 的辅助函数（插在 resolve_accel_lora_slot 之前）
VM_HELPER = '''def _normalize_accel_key(value: str) -> str:
    """把「任意形态」的加速 LoRA 标识归一到**目录 key**。

    形态（按顺序尝试）：
      1. 已经是目录 key（ckpt850 / dmad_4step_critic …）-> 原样；
      2. 完整权重文件名（含目录前缀与扩展名）-> 反查清单；
      3. 裸权重名（无扩展名）-> 反查清单（存量落库数据的实际形态）。

    归一不到 -> **原样返回**（调用方据此判断「认不出」）。
    与显示层 job_meta._accel_lora_key_for_any_form 同口径。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if accel_lora_meta(text):
        return text
    try:
        from . import artifact_meta as _am
    except Exception:
        return text
    try:
        key = _am.lora_key_for_weight(text)
        if key:
            return key
        cleaned = _am._clean_weight_name(text)
        for table in (VIDEO_ACCEL_LORAS, VIDEO_ACCEL_LORA_COMBOS):
            for cand_key, meta in table.items():
                if not isinstance(meta, dict):
                    continue
                name = str(meta.get("name") or "")
                if name and _am._clean_weight_name(name) == cleaned:
                    return cand_key
    except Exception:
        return text
    return text


'''


def sub(text: str, old: str, new: str, label: str, expect: int = 1) -> str:
    count = text.count(old)
    if count != expect:
        raise SystemExit(f"[{label}] 期望命中 {expect} 处，实际 {count} 处")
    return text.replace(old, new)


def main() -> int:
    am = AM.read_text(encoding="utf-8")
    am = sub(am, AM_OLD, AM_NEW, "F5")
    if not CHECK_ONLY:
        AM.write_text(am, encoding="utf-8")

    vm = VM.read_text(encoding="utf-8")
    vm = sub(vm, VM_OLD, VM_NEW, "F7")
    anchor = "def resolve_accel_lora_slot(params: dict,"
    if vm.count(anchor) != 1:
        raise SystemExit("[F7helper] 找不到 resolve_accel_lora_slot 的定义")
    vm = vm.replace(anchor, VM_HELPER + anchor)
    if not CHECK_ONLY:
        VM.write_text(vm, encoding="utf-8")

    print(("检查模式（未落盘）: " if CHECK_ONLY else "已应用: ")
          + "F5 回写只写认得出的目录 key；F7 工作流层归一权重名形态的 key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
