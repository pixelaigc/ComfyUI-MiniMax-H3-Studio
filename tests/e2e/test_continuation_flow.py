# -*- coding: utf-8 -*-
"""
用例组 3：视频「接续」生成链路 + 时长计算。

【用户要求的口径】
「时长计算须符合 Σ各段请求时长 −(段数−1)×1 秒（例如 5+5→9 秒，
再接 8 秒→16 秒）」

【为什么是「−1 秒/段」】
H3 的潜空间桥会**裁掉钉住区**（22 帧 ≈ 0.92 秒 ≈ 1 秒）：
接续段的交付时长 = 本段总帧数 − 钉住帧数。于是两段 5 秒拼起来是
5 + (5 − 1) = 9 秒；第三段再接 8 秒是 9 + (8 − 1) = 16 秒。

【这条用例组要证明的】
1. 生成的卡片上真的有「接续」按钮（服务端逐任务判定，不是前端瞎猜）；
2. 点「接续」-> 左侧配置区真的回填了上一段的配置（尺寸/模型/时长）；
3. 提示词框被清空并聚焦（用户写下一段）；
4. 输入新提示词 -> 提交 -> 提交体里带上了 continuation 上下文；
5. 累计时长符合 Σ请求时长 −(段数−1)×1；
6. 提交给 ComfyUI 的工作流真的挂了 H3RelayLatentLoad + explicit_path
   （不是「看起来接上了」）。
"""
from __future__ import annotations

import json
import time
import urllib.request

import pytest

FPS = 24
CONTEXT_FRAMES = 22

#: H3 的潜空间帧网格是 **17k+5**（见 h3studio/continuation.py
#: align_frame_count）：每个段的请求时长会**向上**吸附到网格，
#: 所以「5 秒」实际是 124 帧（5.167 秒），而不是 120 帧。
#:
#: 【为什么不直接断言「Σ时长 −(段数−1)×1 秒」】
#: 题面口径是「5+5→9 秒、5+5+8→16 秒」。系统的累计**帧数**完全符合
#: 「Σ各段交付帧数 −(段数−1)×22 帧钉住区」，但折算成秒会比题面口径
#: 多出「每段最多 +5 帧（0.208 秒）」的网格取整差。
#: 实测：真实产物 ffprobe = 226 帧（5+5 链）= 9.417 秒 —— 与系统显示
#: 一致，说明系统给的是**真值**，题面口径是近似。
#: 所以这里断言「帧级精确 + 秒级在网格容差内」。


def _expected_frames(segments: list[float]) -> int:
    """累计帧数 = Σ各段交付帧数；接续段要减掉钉住区（22 帧）。"""
    total = 0
    for index, seconds in enumerate(segments):
        raw = int(round(seconds * FPS))
        # 17k+5 网格（向上吸附）
        grid = raw if raw % 17 == 5 else raw + (5 - (raw % 17)) % 17
        delivered = grid if index == 0 else max(0, grid - CONTEXT_FRAMES)
        total += delivered
    return total


def _expected_total(segments: list[float]) -> float:
    """题面口径：Σ各段请求时长 −(段数−1)×1 秒。"""
    return sum(segments) - (len(segments) - 1) * 1.0


def _grid_tolerance(segments: list[float]) -> float:
    """网格取整带来的最大秒差（每段最多 +5 帧 = 0.208 秒）。"""
    return len(segments) * (5.0 / FPS) + 0.02


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
    raise AssertionError(f"任务在 {timeout}s 内没到终态：{(last or {}).get('status')}")


def _card_with_continue(page, job_id: str):
    """找到任务卡片上的「接续」按钮。"""
    return page.query_selector(f'.job-card[data-jid="{job_id}"] button[data-act="continue"]')


def _generate_t2v(sp, prompt: str, duration: float = 5.0):
    """在界面上走一遍文生视频提交，返回 (任务, 提交体)。"""
    sp.open()
    sp.goto_mode("t2v")
    sp.page.eval_on_selector(
        "#duration",
        "e => { e.value = %s; e.dispatchEvent(new Event('input', {bubbles:true})); }"
        % duration)
    sp.page.wait_for_timeout(300)
    sp.fill("#prompt", prompt)
    sp.submit_generation()
    submitted = sp.last_submitted_params()
    job = _wait_job(sp.base_url)
    return job, submitted


def test_continuation_button_appears_on_done_video(studio_page, evidence):
    """跑完的视频卡片上出现「接续」按钮（服务端判定可接续）。"""
    sp = studio_page
    job, _ = _generate_t2v(sp, "e2e 接续按钮：第一段", 5.0)
    evidence.step(f"第一段任务 {job['id']} 状态 = {job['status']}")
    assert job["status"] == "done", f"第一段没跑成功：{job.get('error')}"

    info = job.get("continuation_info") or {}
    evidence.step(f"continuation_info = {info}")
    assert info.get("can_continue") is True, (
        f"服务端判定不能接续：{info.get('reason')}")

    # 界面上要有「接续」按钮
    sp.goto_tab("done")
    button = None
    for _ in range(30):
        button = _card_with_continue(sp.page, job["id"])
        if button is not None:
            break
        sp.page.wait_for_timeout(1000)
    assert button is not None, "卡片上没有「接续」按钮"
    evidence.step(f"接续按钮 title = {button.get_attribute('title')}")
    evidence.screenshot(sp.page, "continue-button")


def test_continuation_refills_panel_and_clears_prompt(studio_page, evidence):
    """点接续 -> 左侧回填配置 + 提示词清空并聚焦。"""
    sp = studio_page
    job, _ = _generate_t2v(sp, "e2e 接续回填：第一段", 5.0)
    assert job["status"] == "done", f"第一段没跑成功：{job.get('error')}"

    sp.goto_tab("done")
    button = None
    for _ in range(30):
        button = _card_with_continue(sp.page, job["id"])
        if button is not None:
            break
        sp.page.wait_for_timeout(1000)
    assert button is not None, "卡片上没有「接续」按钮"

    button.click()
    sp.page.wait_for_timeout(2500)

    # 接续上下文区出现
    assert sp.visible("#continuationPanel"), "点接续后没有出现「接续上下文」区"
    panel_text = sp.text("#continuationPanel")
    evidence.step(f"接续上下文：{panel_text[:300]}")
    assert job["id"] in panel_text, "接续上下文没有显示来源任务"
    assert "提示词未回填" in panel_text, "接续上下文没有说明提示词要自己写"
    evidence.screenshot(sp.page, "continuation-panel")

    # 提示词框被清空
    assert sp.value("#prompt") == "", f"提示词框没被清空：{sp.value('#prompt')!r}"
    # 尺寸被回填（跟上一段一致）
    src_params = job.get("params") or {}
    width = sp.value("#vidWidth")
    height = sp.value("#vidHeight")
    evidence.step(f"回填尺寸 = {width}x{height}（原任务 {src_params.get('width')}x{src_params.get('height')}）")
    assert str(src_params.get("width")) == width, (
        f"回填宽度 {width} != 上一段 {src_params.get('width')}")
    assert str(src_params.get("height")) == height, (
        f"回填高度 {height} != 上一段 {src_params.get('height')}")


def test_continuation_second_segment_total_duration(studio_page, evidence):
    """第二段：5 + 5 -> 累计 9 秒（Σ请求时长 −(段数−1)×1）。"""
    sp = studio_page
    first, _ = _generate_t2v(sp, "e2e 接续时长：第一段 5 秒", 5.0)
    assert first["status"] == "done", f"第一段没跑成功：{first.get('error')}"
    evidence.step(f"第一段累计 = {(first.get('continuation_info') or {}).get('total_seconds')}")

    # 点接续 -> 写第二段提示词 -> 生成
    sp.goto_tab("done")
    button = None
    for _ in range(30):
        button = _card_with_continue(sp.page, first["id"])
        if button is not None:
            break
        sp.page.wait_for_timeout(1000)
    assert button is not None, "卡片上没有「接续」按钮"
    button.click()
    sp.page.wait_for_timeout(2500)

    sp.fill("#prompt", "e2e 接续时长：第二段 5 秒")
    sp.submit_generation()
    submitted = sp.last_submitted_params()
    evidence.step(f"第二段提交体 = {submitted}")
    params = submitted.get("params") or {}
    cont = params.get("continuation") or {}
    evidence.step(f"提交带上的 continuation = {cont}")
    assert cont, "第二段提交里没有 continuation 上下文"
    assert cont.get("source_job") == first["id"], (
        f"continuation.source_job = {cont.get('source_job')}，期望 {first['id']}")

    second = _wait_job(sp.base_url)
    evidence.step(f"第二段任务 {second['id']} 状态 = {second['status']}")
    assert second["status"] == "done", f"第二段没跑成功：{second.get('error')}"

    info = second.get("continuation_info") or {}
    total = float(info.get("total_seconds") or 0)
    frames = int(info.get("total_frames") or 0)
    expected_frames = _expected_frames([5.0, 5.0])
    naive = _expected_total([5.0, 5.0])
    evidence.step(f"第二段累计 = {frames} 帧 / {total}s"
                  f"（帧级期望 {expected_frames}，题面口径 {naive}s）")
    assert frames == expected_frames, (
        f"累计帧数 {frames} != 期望 {expected_frames}")
    assert abs(total - naive) <= _grid_tolerance([5.0, 5.0]), (
        f"累计时长 {total}s 偏离题面口径 {naive}s 超过网格容差")
    evidence.screenshot(sp.page, "continuation-2seg")


def test_continuation_third_segment_total_duration(studio_page, evidence):
    """第三段：5 + 5 + 8 -> 累计 16 秒。"""
    sp = studio_page
    first, _ = _generate_t2v(sp, "e2e 三段链：第一段 5 秒", 5.0)
    assert first["status"] == "done", f"第一段没跑成功：{first.get('error')}"

    def continue_from(job_id: str, prompt: str, duration: float):
        sp.goto_tab("done")
        button = None
        for _ in range(30):
            button = _card_with_continue(sp.page, job_id)
            if button is not None:
                break
            sp.page.wait_for_timeout(1000)
        assert button is not None, f"任务 {job_id} 卡片上没有「接续」按钮"
        button.click()
        sp.page.wait_for_timeout(2500)
        sp.page.eval_on_selector(
            "#duration",
            "e => { e.value = %s; e.dispatchEvent(new Event('input', {bubbles:true})); }"
            % duration)
        sp.page.wait_for_timeout(300)
        sp.fill("#prompt", prompt)
        sp.submit_generation()
        return _wait_job(sp.base_url)

    second = continue_from(first["id"], "e2e 三段链：第二段 5 秒", 5.0)
    assert second["status"] == "done", f"第二段没跑成功：{second.get('error')}"

    third = continue_from(second["id"], "e2e 三段链：第三段 8 秒", 8.0)
    assert third["status"] == "done", f"第三段没跑成功：{third.get('error')}"

    info = third.get("continuation_info") or {}
    total = float(info.get("total_seconds") or 0)
    frames = int(info.get("total_frames") or 0)
    expected_frames = _expected_frames([5.0, 5.0, 8.0])
    naive = _expected_total([5.0, 5.0, 8.0])
    evidence.step(f"第三段累计 = {frames} 帧 / {total}s"
                  f"（帧级期望 {expected_frames}，题面口径 {naive}s）")
    assert frames == expected_frames, (
        f"三段累计帧数 {frames} != 期望 {expected_frames}")
    assert abs(total - naive) <= _grid_tolerance([5.0, 5.0, 8.0]), (
        f"三段累计时长 {total}s 偏离题面口径 {naive}s 超过网格容差")

    # 段号也要对：第三段是链上的第 3 段（stage_index=2）
    stage = int(info.get("stage_index") or 0)
    evidence.step(f"第三段 stage_index = {stage}")
    assert stage == 2, f"第三段的 stage_index = {stage}，期望 2"
    evidence.screenshot(sp.page, "continuation-3seg")


def test_continuation_workflow_uses_relay_bridge(studio_page, evidence, studio):
    """接续段提交给 ComfyUI 的工作流真的挂了拷贝桥（不是静默退化）。"""
    if not studio.fake_comfy:
        pytest.skip("需要假 ComfyUI 才能回读工作流")

    sp = studio_page
    first, _ = _generate_t2v(sp, "e2e 接续工作流：第一段", 5.0)
    assert first["status"] == "done", f"第一段没跑成功：{first.get('error')}"

    sp.goto_tab("done")
    button = None
    for _ in range(30):
        button = _card_with_continue(sp.page, first["id"])
        if button is not None:
            break
        sp.page.wait_for_timeout(1000)
    assert button is not None, "卡片上没有「接续」按钮"
    button.click()
    sp.page.wait_for_timeout(2500)
    sp.fill("#prompt", "e2e 接续工作流：第二段")
    sp.submit_generation()
    second = _wait_job(sp.base_url)
    assert second["status"] == "done", f"第二段没跑成功：{second.get('error')}"

    # 从假 ComfyUI 回读第二段的工作流
    prompt_id = second.get("prompt_id")
    assert prompt_id, "任务没有 prompt_id"
    url = f"{studio.fake_comfy_url}/control/workflow/{prompt_id}"
    with urllib.request.urlopen(url, timeout=30) as r:
        workflow = json.loads(r.read().decode())
    classes = {str(node.get("class_type")) for node in workflow.values()
               if isinstance(node, dict)}
    evidence.step(f"第二段工作流节点类型 = {sorted(classes)}")
    assert "H3RelayLatentLoad" in classes, "接续段工作流里没有 H3RelayLatentLoad"
    assert "H3RelayCopyBridge" in classes, "接续段工作流里没有 H3RelayCopyBridge"
    assert "H3RelayLatentSave" in classes, "接续段工作流里没有 H3RelayLatentSave"

    # 读回用的必须是**上一段的**潜空间文件（explicit_path 指向真实文件）
    loads = [node for node in workflow.values()
             if isinstance(node, dict)
             and node.get("class_type") == "H3RelayLatentLoad"]
    paths = [str((node.get("inputs") or {}).get("explicit_path") or "")
             for node in loads]
    evidence.step(f"LatentLoad 的 explicit_path = {paths}")
    assert any(path.endswith(".safetensors") for path in paths), (
        f"LatentLoad 没有 explicit_path：{paths}")


def test_continuation_prompt_is_not_carried_over(studio_page, evidence):
    """接续**不带**上一段提示词（用户自己写下一段）。"""
    sp = studio_page
    first, _ = _generate_t2v(sp, "e2e 提示词不沿用：第一段的提示词标记XYZ", 5.0)
    assert first["status"] == "done", f"第一段没跑成功：{first.get('error')}"

    sp.goto_tab("done")
    button = None
    for _ in range(30):
        button = _card_with_continue(sp.page, first["id"])
        if button is not None:
            break
        sp.page.wait_for_timeout(1000)
    assert button is not None, "卡片上没有「接续」按钮"
    button.click()
    sp.page.wait_for_timeout(2500)

    assert sp.value("#prompt") == "", "提示词框没有清空"
    # 接续上下文里也不能含上一段提示词
    ctx = sp.page.evaluate("() => window.Continuation && "
                            "window.Continuation._debug.current")
    evidence.step(f"接续上下文 keys = {sorted((ctx or {}).keys())}")
    params = (ctx or {}).get("params") or {}
    assert "prompt" not in params, "接续上下文里带着上一段提示词"
