#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证修复假设：options_payload 的 511ms 里，到底有多少是重复的 find_weight？

做法：把 find_weight 换成带记忆化的版本（用 monkeypatch，不改源码），
再测 options_payload 的耗时。

同时统计：
  * options_payload 调了几次 find_weight
  * 其中多少次真的触发了 rglob（慢路径）
  * 去重后只剩多少次
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/root/projects/h3s-favorites")
from h3studio import video_models as vm  # noqa: E402

# ---- 计数原始 find_weight 的调用 ----
_orig_find = vm.find_weight
CALLS = {"total": 0, "by_kind": {}, "hit_direct": 0, "hit_rglob": 0}


def counting_find(name, kind="diffusion_models"):
    CALLS["total"] += 1
    CALLS["by_kind"][kind] = CALLS["by_kind"].get(kind, 0) + 1
    roots = vm._model_dirs() if kind == "diffusion_models" else vm._lora_dirs()
    for root in roots:
        cand = root / kind / name if kind == "diffusion_models" else root / name
        if cand.is_file():
            CALLS["hit_direct"] += 1
            return cand
    CALLS["hit_rglob"] += 1
    return _orig_find(name, kind)


vm.find_weight = counting_find

t0 = time.perf_counter()
vm.options_payload()
base_ms = (time.perf_counter() - t0) * 1000.0
print(f"原始 options_payload      = {base_ms:.1f} ms")
print(f"  find_weight 调用 {CALLS['total']} 次  直连命中 {CALLS['hit_direct']}  "
      f"落到 rglob {CALLS['hit_rglob']}  按 kind {CALLS['by_kind']}")

# ---- 换成记忆化版本 ----
_MEMO = {}


def memo_find(name, kind="diffusion_models"):
    key = (name, kind)
    if key in _MEMO:
        return _MEMO[key]
    v = _orig_find(name, kind)
    _MEMO[key] = v
    return v


vm.find_weight = memo_find
_CALLS2 = {"total": 0}
_orig_inner = vm.check_h3_lora_compat


def counting_check(*a, **kw):
    _CALLS2["total"] += 1
    return _orig_inner(*a, **kw)


vm.check_h3_lora_compat = counting_check
vm.detect_accel_lora_loader.__globals__["find_weight"] = memo_find

t0 = time.perf_counter()
vm.options_payload()
memo_ms = (time.perf_counter() - t0) * 1000.0
print(f"find_weight 记忆化后      = {memo_ms:.1f} ms   （提速 {base_ms/memo_ms:.1f}x）")
print(f"  check_h3_lora_compat 调用 {_CALLS2['total']} 次（= 7 底模 × 12 LoRA）")

t0 = time.perf_counter()
vm.options_payload()
memo2_ms = (time.perf_counter() - t0) * 1000.0
print(f"再调一次（缓存全热）      = {memo2_ms:.1f} ms")

# ---- 记忆化 + 整个矩阵缓存 ----
vm.find_weight = memo_find
_MATRIX = {}


_REAL_MATRIX = vm.accel_lora_compat_matrix  # 先抓住真身，否则替换后自己调自己 -> 无限递归


def cached_matrix(model_key):
    if model_key not in _MATRIX:
        _MATRIX[model_key] = _REAL_MATRIX(model_key)
    return _MATRIX[model_key]


vm.accel_lora_compat_matrix = cached_matrix
t0 = time.perf_counter()
vm.options_payload()
full_ms = (time.perf_counter() - t0) * 1000.0
print(f"find_weight 记忆化 + 矩阵缓存 = {full_ms:.1f} ms   （原 {base_ms:.1f} ms）")
