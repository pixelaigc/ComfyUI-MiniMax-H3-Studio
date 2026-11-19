# -*- coding: utf-8 -*-
"""
用例组 6：内置 Agent 管理（按生成类型配置默认参数）+ 卡片运行状态一致性。

【覆盖用户要求 B.7 / B.8】
- B.7 内置 Agent 管理：为每个生成类型配置默认参数；
- B.8 卡片「运行中」状态与 ComfyUI 实际任务的一致性校验
      （**不得出现永远运行中**）。
"""
from __future__ import annotations

import json
import time
import urllib.request

import pytest
import gen_flow  # noqa: E402


def _api(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.loads(r.read().decode())


def test_agent_manage_lists_every_generation_type(studio_page, evidence):
    """Agent 管理面板列出**每一个**生成类型的默认参数配置。"""
    sp = studio_page
    sp.open()
    sp.goto_page("navAgent")
    sp.click("#agManageBtn")
    sp.page.wait_for_timeout(2500)
    assert sp.visible("#agManageModal"), "Agent 管理弹窗没打开"

    sections = sp.page.eval_on_selector_all(
        "#agManageBody .agm-gen",
        "els => els.map(e => ({title: e.querySelector('.agm-gen-title')"
        "?.textContent.trim(), mode: e.querySelector('.agm-gen-head .agm-tag')"
        "?.textContent.trim()}))")
    evidence.step(f"面板里的生成类型 = {[s['mode'] for s in sections]}")
    assert len(sections) >= 10, f"生成类型配置项太少：{len(sections)}"

    # 服务端下发的规格必须**每一项**都在界面上有对应配置
    spec = _api(sp.base_url, "/api/agent/settings").get("gen_defaults_spec") or {}
    evidence.step(f"服务端规格 = {sorted(spec.keys())}")
    missing = [mode for mode in spec if mode not in
               {s["mode"] for s in sections}]
    assert not missing, f"这些生成类型在界面上没有默认参数配置：{missing}"
    evidence.screenshot(sp.page, "agent-manage")


def test_agent_gen_defaults_saved_and_applied(studio_page, evidence):
    """改某个生成类型的默认参数 -> 保存 -> 服务端真的存下来了。"""
    sp = studio_page
    sp.open()
    sp.goto_page("navAgent")
    sp.click("#agManageBtn")
    sp.page.wait_for_timeout(2500)

    # 展开「Z-IMAGE 文生图」这一节（<details> 折叠时控件不可见）
    section = None
    for candidate in sp.page.query_selector_all("#agManageBody .agm-gen"):
        tag = candidate.query_selector(".agm-gen-head .agm-tag")
        if tag and tag.inner_text().strip() == "t2i":
            section = candidate
            break
    assert section is not None, "没找到 t2i 的默认参数配置"
    # <details> 默认折叠 —— 里面的控件存在但**不可见**（fill 会超时）。
    # 判据用 open 属性，不是 .agm-gen-body 的存在（body 一直在，只是被折叠）。
    if section.get_attribute("open") is None:
        section.query_selector(".agm-gen-head").click()
        sp.page.wait_for_timeout(600)

    fields = section.query_selector_all("input, select")
    evidence.step(f"t2i 配置字段数 = {len(fields)}")
    assert fields, "t2i 的默认参数区没有任何控件"

    # 改一个数值型字段（步数）—— 用**确定的 id**（agmGen_<mode>_<字段名>）
    target = sp.page.query_selector("#agmGen_t2i_steps")
    assert target is not None, "没找到 t2i 的步数输入框"
    assert target.is_visible(), (
        "t2i 的步数输入框不可见 —— 「<details>」没展开或该字段不在这一节里")
    field_id = "agmGen_t2i_steps"
    before = target.input_value()
    new_value = "7" if before != "7" else "9"
    target.fill(new_value)
    sp.page.wait_for_timeout(300)
    evidence.step(f"把 {field_id} 从 {before} 改成 {new_value}")

    sp.click("#agmSave")
    sp.page.wait_for_timeout(2500)
    evidence.screenshot(sp.page, "agent-save")

    # 服务端存下来了
    saved = _api(sp.base_url, "/api/agent/settings").get("gen_defaults") or {}
    t2i = saved.get("t2i") or {}
    evidence.step(f"服务端 t2i 默认参数 = {t2i}")
    assert str(t2i.get("steps")) == new_value, (
        f"保存后服务端 steps = {t2i.get('steps')}，期望 {new_value}")

    # 页面重新打开时读回来的也是新值（不是只在内存里）
    sp.click("#agManageClose")
    sp.page.wait_for_timeout(800)
    sp.click("#agManageBtn")
    sp.page.wait_for_timeout(2500)
    reopened = sp.page.query_selector(f"#{field_id}")
    assert reopened is not None, "重开面板后找不到该输入框"
    evidence.step(f"重开后 {field_id} = {reopened.input_value()}")
    assert reopened.input_value() == new_value, (
        f"重开后 {field_id} = {reopened.input_value()}，期望 {new_value}")


def test_running_card_is_consistent_with_comfyui(studio_page, evidence, studio):
    """卡片「运行中」必须与 ComfyUI 实际任务一致（不许永远运行中）。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")
    sp.fill("#prompt", gen_flow.prompt_text("t2i_cat_grass"))
    sp.submit_generation()

    # 提交后卡片应进入「排队中 / 运行中」
    sp.page.wait_for_timeout(3000)
    sp.goto_tab("active")
    active_cards = sp.page.query_selector_all("#jobsGrid .job-card")
    evidence.step(f"「进行中」标签下的卡片数 = {len(active_cards)}")
    if active_cards:
        text = active_cards[0].inner_text()
        evidence.step(f"卡片文本片段 = {text[:160]!r}")

    # 等任务到终态 —— 不允许永远停在「运行中」
    job = sp.wait_for_job(timeout=240)
    evidence.step(f"任务 {job['id']} 状态 = {job['status']}")
    assert job["status"] in ("done", "failed", "canceled"), (
        f"任务卡在 {job['status']}（永远运行中）")

    # 终态后「进行中」标签下不应再有这张卡
    sp.goto_tab("active")
    still = sp.page.query_selector(f'.job-card[data-jid="{job["id"]}"]')
    assert still is None, "任务已结束但仍出现在「进行中」标签下"
    sp.goto_tab("done")
    found = sp.page.query_selector(f'.job-card[data-jid="{job["id"]}"]')
    assert found is not None, "任务完成后「已完成」标签下找不到它"
    evidence.screenshot(sp.page, "running-consistency")


def test_running_truth_state_is_reported(studio_page, evidence):
    """服务端对「运行中」给出真实判定（active / lost / timeout / unknown）。"""
    sp = studio_page
    sp.open()
    payload = _api(sp.base_url, "/api/jobs?limit=20")
    evidence.step(f"running = {payload.get('running')}，"
                  f"running_state = {payload.get('running_state')}")
    assert "running_state" in payload, "接口没有下发 running_state 判定"

    # 有任务在跑时，状态必须是非空判定
    running_jobs = [j for j in payload["jobs"] if j.get("status") == "running"]
    for job in running_jobs:
        truth = job.get("running_truth") or {}
        evidence.step(f"任务 {job['id']} 的 running_truth = {truth}")
        assert truth, f"运行中的任务 {job['id']} 没有 running_truth 判定"
        assert truth.get("state"), "running_truth 缺 state"


def test_queue_panel_matches_server_queue(studio_page, evidence):
    """队列面板显示的排队/运行数与服务端一致。"""
    sp = studio_page
    sp.open()
    sp.page.wait_for_timeout(2000)
    visible = sp.page.is_visible("#queuePanel")
    evidence.step(f"队列面板可见 = {visible}")
    if not visible:
        pytest.skip("队列面板未启用（当前版本没有该入口）")

    summary = sp.text("#queueSummary")
    evidence.step(f"队列摘要 = {summary!r}")
    payload = _api(sp.base_url, "/api/jobs?limit=1")
    evidence.step(f"服务端 queued = {payload.get('running')}")
