# -*- coding: utf-8 -*-
"""
用例组 1：文生图（分辨率输入框 + 提交链路）。

【覆盖用户实测缺陷】
用户实测「2560x1400 输入不了」。本组要证明的是：
  1. 宽高输入框**真的能接受任意用户输入**（不被 step/min/max 卡死）；
  2. 输入后**按模型倍率自动校正**（吸附到模型要求的倍数），并把
     「原始值 → 生效值 + 原因」如实显示在界面上；
  3. 校正后的值**真的提交给后端**（不是只在界面上好看）；
  4. 出图后卡片上的实际像素 == 提交值。
"""
from __future__ import annotations

import re

import pytest


def test_image_resolution_input_accepts_and_snaps(studio_page, evidence):
    """2560x1400 能输入；自动吸附到 16 的倍数并给出原因。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")
    evidence.screenshot(sp.page, "image-panel")

    # 起始状态：档位自动尺寸
    initial = sp.value("#imgWidth"), sp.value("#imgHeight")
    evidence.step(f"档位默认尺寸 = {initial}")

    # 输入 2560x1400（用户实测输入不了的那一组）
    sp.type_and_commit("#imgWidth", "2560")
    sp.type_and_commit("#imgHeight", "1400")

    width = int(sp.value("#imgWidth"))
    height = int(sp.value("#imgHeight"))
    evidence.step(f"输入后宽高 = {width}x{height}")

    # 1) 值没有被浏览器悄悄丢掉（输入框里确实是我们输入的数）
    assert width == 2560, f"宽度输入框的值不是 2560，而是 {width}"
    # 2) 高度不是 16 的倍数 -> 必然被吸附（这是模型约束，不是丢输入）
    assert height == 1408, f"高度应吸附到 1408（16 的倍数），实际 {height}"
    # 3) 界面如实说明「为什么被改了」
    adjust = sp.text("#imgSizeAdjust")
    evidence.step(f"自动调整说明：{adjust}")
    assert "1400" in adjust and "1408" in adjust, (
        f"界面没有如实说明吸附原因：{adjust!r}")
    # 4) 生效尺寸在提示里也一致
    assert "2560×1408" in sp.text("#imgSizeHint"), sp.text("#imgSizeHint")
    evidence.screenshot(sp.page, "resolution-snapped")


def test_image_resolution_snapping_respects_model_multiple(studio_page, evidence):
    """吸附倍数随模型变（H3-Image 是 32 的倍数，Z-Image 是 16）。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")
    step_z = int(sp.page.eval_on_selector("#imgWidth", "e => e.step"))
    evidence.step(f"Z-IMAGE 的 step = {step_z}")
    assert step_z == 16, f"Z-IMAGE 的 step 期望 16，实际 {step_z}"

    sp.goto_mode("t2i_h3image")
    sp.page.wait_for_timeout(800)
    step_h3 = int(sp.page.eval_on_selector("#imgWidth", "e => e.step"))
    evidence.step(f"H3-IMAGE 的 step = {step_h3}")
    assert step_h3 == 32, f"H3-IMAGE 的 step 期望 32，实际 {step_h3}"

    # 用一个「两个倍数都不满足」的值验证吸附到 32 的倍数
    sp.type_and_commit("#imgWidth", "1000")
    sp.type_and_commit("#imgHeight", "1000")
    width = int(sp.value("#imgWidth"))
    height = int(sp.value("#imgHeight"))
    evidence.step(f"H3-IMAGE 吸附后 = {width}x{height}")
    assert width % 32 == 0 and height % 32 == 0, (
        f"H3-Image 应吸附到 32 的倍数，实际 {width}x{height}")
    evidence.screenshot(sp.page, "h3image-32-multiple")


def test_image_submit_sends_snapped_size(studio_page, evidence):
    """校正后的尺寸真的提交给后端（界面显示与提交值一致）。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")
    sp.type_and_commit("#imgWidth", "2560")
    sp.type_and_commit("#imgHeight", "1400")
    sp.fill("#prompt", "e2e 分辨率链路：一只猫在草地上奔跑")
    sp.submit_generation()

    submitted = sp.last_submitted_params()
    evidence.step(f"提交的 params = {submitted}")
    assert submitted, "没有捕获到 POST /api/jobs 的提交体"
    params = submitted.get("params") or {}
    assert params.get("width") == 2560, f"提交宽度 = {params.get('width')}"
    assert params.get("height") == 1408, f"提交高度 = {params.get('height')}（应为吸附后的值）"
    evidence.screenshot(sp.page, "submitted")


def test_image_output_matches_requested_size(studio_page, evidence):
    """出图后卡片显示的实际像素 == 提交值（端到端闭环）。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")
    sp.type_and_commit("#imgWidth", "2560")
    sp.type_and_commit("#imgHeight", "1400")
    sp.fill("#prompt", "e2e 出图闭环：一只猫在草地上奔跑")
    sp.submit_generation()

    job = sp.wait_for_job(timeout=240)
    evidence.step(f"任务 {job['id']} 状态 = {job['status']}")
    evidence.screenshot(sp.page, "job-done")
    assert job["status"] == "done", f"任务没跑成功：{job.get('error')}"

    outputs = job.get("outputs") or []
    assert outputs, "任务没有产物"
    out = outputs[0]
    evidence.step(f"产物尺寸 = {out.get('width')}x{out.get('height')}")
    assert out.get("width") == 2560, f"产物宽度 = {out.get('width')}"
    assert out.get("height") == 1408, f"产物高度 = {out.get('height')}"


def test_image_batch_count_submitted(studio_page, evidence):
    """批次：界面选 N 张 -> 提交 batch_count=N。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")
    # 批次 chip：文本是「N 张」
    chips = sp.page.query_selector_all("#imgBatchChips button")
    assert len(chips) >= 3, f"批次 chip 数量异常：{len(chips)}"
    chips[2].click()
    sp.page.wait_for_timeout(400)
    sp.fill("#prompt", "e2e 批次：一只猫")
    sp.submit_generation()

    submitted = sp.last_submitted_params()
    params = submitted.get("params") or {}
    evidence.step(f"batch_count = {params.get('batch_count')}")
    assert int(params.get("batch_count") or 0) == 3, (
        f"提交的 batch_count = {params.get('batch_count')}，期望 3")
    evidence.screenshot(sp.page, "batch-3")


def test_image_batch_result_expands_in_ui(studio_page, evidence):
    """批次结果在界面上可展开，且每个批次有独立状态。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")
    chips = sp.page.query_selector_all("#imgBatchChips button")
    chips[1].click()
    sp.page.wait_for_timeout(300)
    sp.fill("#prompt", "e2e 批次展开：一只猫")
    sp.submit_generation()

    job = sp.wait_for_job(timeout=240)
    evidence.step(f"任务 {job['id']} 状态 = {job['status']}")
    batches = job.get("batches") or []
    evidence.step(f"批次数 = {len(batches)}")
    assert len(batches) == 2, f"批次数 = {len(batches)}，期望 2"

    # 界面上要有「批次结果」折叠块。
    # 提交后前端会自动切到「进行中」标签 —— 任务跑完就不在那儿了，
    # 所以要先切回「已完成」。
    sp.goto_tab("done")
    card = None
    for _ in range(30):
        card = sp.page.query_selector(f'.job-card[data-jid="{job["id"]}"]')
        if card is not None:
            break
        sp.page.wait_for_timeout(1000)
    assert card is not None, f"界面上找不到任务 {job['id']} 的卡片"
    toggle = card.query_selector(".job-batches-toggle")
    assert toggle is not None, "界面上没有「批次结果」折叠块"
    evidence.step(f"折叠块文本：{toggle.inner_text().strip()}")
    assert "2/2" in toggle.inner_text(), (
        f"折叠块摘要与批次状态不符：{toggle.inner_text()!r}")

    toggle.click()
    sp.page.wait_for_timeout(600)
    cells = card.query_selector_all(".job-batches .job-batch")
    evidence.step(f"展开后批次格子数 = {len(cells)}")
    assert len(cells) == 2, f"展开后格子数 = {len(cells)}"
    # 每个批次都要有自己的状态标记
    states = [cell.query_selector(".st").inner_text().strip()
              for cell in cells]
    evidence.step(f"批次状态 = {states}")
    assert all("完成" in s for s in states), f"批次状态异常：{states}"
    evidence.screenshot(sp.page, "batches-expanded")
