# -*- coding: utf-8 -*-
"""
用例组 7：**生成动作的三条硬约束**（2026-11-19 用户实测反馈）。

【为什么单独开一组】
用户实测发现端到端测试的生成动作有三处根本性问题：

  1. 步数设为 4 步，却**未启用加速 LoRA** —— 4 步是 H3 的蒸馏档，
     没有加速 LoRA 时跑的是一条「不存在的路径」；
  2. 提示词输入框里塞的是「e2e 第二段」「XXXX」这类**占位文本**，
     对模型毫无意义；
  3. 提示词写完后**没有走增强**就直接生成 —— 不符合既定使用流程。

A1 / A2 是**脚本级**静态断言（在 tests/test_e2e_gen_flow_contract.py）。
本组是**真浏览器**断言：走完整的「填提示词 -> 增强 -> 生成」链路，
逐条验证界面**真的**提交了正确的参数。

【本组覆盖】
  * A1' 4 步的生成，提交体里**必须带加速 LoRA**（不是隐式兜底）；
  * A2' 提交体里的提示词**不含**测试占位词；
  * A3' 提交体里的 prompt **等于增强后的提示词**（且不等于原文）。

运行：
    python3 -m pytest tests/e2e/test_gen_flow_contract.py -v
"""
from __future__ import annotations

import json
import time
import urllib.request

import pytest

import gen_flow  # noqa: E402

#: 4 步档的加速 LoRA 键（与服务端 /api/video-options 的实测清单同源）
ACCEL_4STEP_KEYS = set(gen_flow.ACCEL_4STEP_KEYS)


def _wait_job(base: str, timeout: float = 240.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        with urllib.request.urlopen(base + "/api/jobs?limit=5", timeout=30) as r:
            jobs = json.loads(r.read().decode())["jobs"]
        if jobs:
            last = jobs[0]
            if last.get("status") in ("done", "failed", "canceled"):
                return last
        time.sleep(1.5)
    raise AssertionError(f"任务在 {timeout}s 内没到终态："
                         f"{(last or {}).get('status')}")


def _submitted_params(sp) -> dict:
    submitted = sp.last_submitted_params()
    assert submitted, "没有捕获到 POST /api/jobs 的提交体"
    return submitted.get("params") or {}


def test_gen_flow_enhanced_prompt_is_what_gets_submitted(studio_page, evidence):
    """A3：提交体里的 prompt == 增强后的 prompt（且 != 原文）。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2v")

    original = gen_flow.prompt_text("t2v_cat_desk")
    sp.fill("#prompt", original)
    evidence.step(f"原始提示词 = {original[:60]!r}")

    enhanced = sp.enhance_prompt()
    evidence.step(f"增强后提示词长度 = {len(enhanced)}")

    # 增强结果必须**真的不同**（否则就是没增强）
    assert enhanced != original, "增强结果与原文相同 —— 没有真的增强"
    # 增强结果**不得**含测试占位词（模型看到的是这段文本）
    hits = gen_flow.has_placeholder(enhanced)
    assert not hits, f"增强后的提示词含测试占位词 {hits}：{enhanced[:120]!r}"

    sp.submit_generation()
    params = _submitted_params(sp)
    evidence.step(f"提交的 prompt = {str(params.get('prompt'))[:120]!r}")

    # ---- A3：提交的 prompt 就是增强后的那一份 ----
    assert params.get("prompt") == enhanced, (
        f"提交的 prompt 不是增强后的提示词：\n"
        f"  增强后 = {enhanced[:160]!r}\n"
        f"  提交的 = {str(params.get('prompt'))[:160]!r}")
    assert params.get("prompt") != original, (
        "提交的 prompt 仍是原文 —— 增强没有写回提示词框")

    # ---- 原始提示词仍然保留在 original_prompt 里（供面板区分）----
    assert params.get("original_prompt") == original, (
        f"original_prompt 不是原文：{params.get('original_prompt')!r}")

    job = _wait_job(sp.base_url)
    evidence.step(f"任务 {job['id']} 状态 = {job['status']}")
    assert job["status"] == "done", f"任务没跑成功：{job.get('error')}"


def test_gen_flow_four_steps_carries_accel_lora(studio_page, evidence):
    """A1'：4 步的生成必须**显式**带 4 步档的加速 LoRA。

    【为什么这条要单列】
    服务端在「没传任何加速字段」时会**兜底**挂 ckpt850 —— 那看起来
    像是「带了加速 LoRA」，但它是隐式的：脚本并没有表达「我要 4 步
    蒸馏档」的意图，改一下采样器就会跑出一条不存在的路径。
    所以这里断言的是**提交体里显式带了**。
    """
    sp = studio_page
    sp.open()
    sp.goto_mode("t2v")

    # 用面板真实控件把步数设成 4 步，并显式选一枚 4 步档加速 LoRA
    sp.page.eval_on_selector(
        "#vidSteps",
        "e => { e.value = 4; e.dispatchEvent(new Event('input', {bubbles:true})); }")
    sp.page.wait_for_timeout(600)
    evidence.step(f"步数滑杆 = {sp.value('#vidSteps')}")

    options = sp.page.eval_on_selector_all(
        "#vidAccelLoraSelect option", "els => els.map(e => e.value)")
    evidence.step(f"加速 LoRA 下拉的可选值 = {options}")

    # 【隔离实例】连的是假 ComfyUI，模型清单是空的 -> 下拉里可能
    # 一枚加速 LoRA 都选不了。此时**显式跳过**并说明原因 ——
    # 但绝不能静默通过（那等于这条断言没测）。
    if not any(o in ACCEL_4STEP_KEYS for o in options):
        pytest.skip(
            "隔离实例的加速 LoRA 下拉是空的（假 ComfyUI 的模型清单为空）—— "
            "这条断言需要 --e2e-real-comfy 或 --live 才能跑。"
            f"当前可选值：{options}")

    picked = next(o for o in options if o in ACCEL_4STEP_KEYS)
    sp.select_option("#vidAccelLoraSelect", picked)
    sp.page.wait_for_timeout(800)
    evidence.step(f"选中的加速 LoRA = {picked}")

    # 选完之后步数应当**跟着**推荐档重算（4 步档就是 4 步）
    steps_after = sp.value("#vidSteps")
    evidence.step(f"选完加速 LoRA 后的步数 = {steps_after}")
    assert int(steps_after) == 4, (
        f"选了 4 步档加速 LoRA 后步数变成了 {steps_after}")

    sp.fill("#prompt", gen_flow.prompt_text("t2v_cat_grass"))
    sp.enhance_prompt()
    sp.submit_generation()

    params = _submitted_params(sp)
    evidence.step(f"提交的 accel_lora = {params.get('accel_lora')!r}")

    # ---- A1'：提交体里显式带了加速 LoRA ----
    assert params.get("accel_lora"), (
        "提交体里没有加速 LoRA —— 4 步是蒸馏档，必须显式带一枚")
    assert params.get("accel_lora") in ACCEL_4STEP_KEYS, (
        f"提交的加速 LoRA {params.get('accel_lora')!r} 不是 4 步档")
    assert int(params.get("steps") or 0) == 4, (
        f"提交的步数 = {params.get('steps')}，期望 4")
    # 数组形态也要在（兼容老读侧）
    arr = params.get("accel_loras") or []
    assert any(str(item.get("key")) == params.get("accel_lora") for item in arr), (
        f"提交的 accel_loras 与 accel_lora 不一致：{arr}")

    job = _wait_job(sp.base_url)
    evidence.step(f"任务 {job['id']} 状态 = {job['status']}")
    assert job["status"] == "done", f"任务没跑成功：{job.get('error')}"


def test_gen_flow_submitted_prompt_has_no_placeholder(studio_page, evidence):
    """A2'：提交体里的提示词不含任何测试占位词，且非空。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")

    sp.fill("#prompt", gen_flow.prompt_text("t2i_cat_grass"))
    sp.enhance_prompt()
    sp.submit_generation()

    params = _submitted_params(sp)
    prompt = params.get("prompt") or ""
    evidence.step(f"提交的 prompt = {prompt[:120]!r}")

    assert prompt.strip(), "提交的提示词是空串"
    hits = gen_flow.has_placeholder(prompt)
    assert not hits, f"提交的提示词含测试占位词 {hits}：{prompt[:120]!r}"

    job = _wait_job(sp.base_url)
    evidence.step(f"任务 {job['id']} 状态 = {job['status']}")
    assert job["status"] == "done", f"任务没跑成功：{job.get('error')}"
