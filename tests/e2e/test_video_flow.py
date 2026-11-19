# -*- coding: utf-8 -*-
"""
用例组 2：视频生成面板（文生视频 / 首尾帧 / 参考生视频 三模式）。

【覆盖的用户路径】
- 文生视频：面板切到视频模式 -> 时长/尺寸控件可用 -> 提交 -> 出片回到界面；
- 首尾帧生视频：面板出现「首帧 / 尾帧」槽位，能从素材库选素材填入；
- 参考生视频：面板出现「参考图 / 参考视频 / 参考音乐」三组槽位，
  且**三选一的音轨来源**会随素材到位而出现。

【要点】
「参考生视频：参考图/参考视频/参考音乐三选一轨道」在界面上的体现是
**音轨来源下拉**（#audioTrackSelect）：只有当参考音乐或参考视频存在时，
「使用参考音乐作音轨 / 使用参考视频的背景音」才会出现 —— 没有素材
时它们**必须**不出现（有素材时必须出现）。
"""
from __future__ import annotations

import pytest
import gen_flow  # noqa: E402


def _slot(page, key: str):
    return page.query_selector(f'.up-slot[data-key="{key}"]')


def test_video_t2v_panel_and_submit(studio_page, evidence):
    """文生视频：面板控件齐全，提交参数正确，出片回到界面。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2v")

    assert sp.visible("#videoPanel"), "文生视频面板没显示"
    assert sp.visible("#durField"), "时长控件没显示"
    assert not sp.visible("#uploadZone"), "文生视频不该有素材槽位区"
    evidence.screenshot(sp.page, "t2v-panel")

    # 时长滑杆能改，且显示同步
    sp.page.eval_on_selector("#duration", "e => { e.value = 8; e.dispatchEvent(new Event('input', {bubbles:true})); }")
    sp.page.wait_for_timeout(400)
    evidence.step(f"时长标签 = {sp.text('#durVal')}")
    assert "8" in sp.text("#durVal"), f"时长标签没同步：{sp.text('#durVal')}"

    sp.fill("#prompt", gen_flow.prompt_text("t2v_cat_grass"))
    sp.submit_generation()
    submitted = sp.last_submitted_params()
    evidence.step(f"提交 = {submitted}")
    assert submitted.get("mode") == "t2v", f"提交的 mode = {submitted.get('mode')}"
    params = submitted.get("params") or {}
    assert params.get("duration") == 8, f"提交时长 = {params.get('duration')}"
    assert params.get("width") and params.get("height"), "提交里没有尺寸"

    job = sp.wait_for_job(timeout=240)
    evidence.step(f"任务 {job['id']} 状态 = {job['status']}")
    assert job["status"] == "done", f"任务没跑成功：{job.get('error')}"
    assert job["mode"] == "t2v"
    outputs = job.get("outputs") or []
    assert outputs, "没有产物"
    evidence.screenshot(sp.page, "t2v-done")


def test_video_flf_panel_has_first_last_slots(studio_page, evidence):
    """首尾帧：面板出现首帧/尾帧槽位，可从素材库选素材填入。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("flf")

    assert sp.visible("#uploadZone"), "首尾帧模式没有素材槽位区"
    first = _slot(sp.page, "first")
    last = _slot(sp.page, "last")
    assert first is not None, "首尾帧模式没有「首帧」槽位"
    assert last is not None, "首尾帧模式没有「尾帧」槽位"
    evidence.step(f"槽位标题：{sp.text('#uploadTitle')}")
    evidence.screenshot(sp.page, "flf-slots")

    # 未填的槽位点一下会打开素材库选择器
    first.click()
    sp.page.wait_for_timeout(1200)
    assert sp.visible("#alPickerMask"), "点空槽位没有打开素材库选择器"
    evidence.step("点空槽位 -> 素材库选择器打开")
    evidence.screenshot(sp.page, "flf-picker")


def test_video_r2v_panel_has_three_media_tracks(studio_page, evidence):
    """参考生视频：参考图 / 参考视频 / 参考音乐 三组槽位都在。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("r2v")

    assert sp.visible("#uploadZone"), "参考生视频没有素材槽位区"
    keys = sp.page.eval_on_selector_all(
        "#uploadRow .up-slot", "els => els.map(e => e.dataset.key)")
    evidence.step(f"槽位 key = {keys}")
    for key in ("ref0", "ref8", "refvid0", "refvid2", "refaud0", "refaud2"):
        assert key in keys, f"缺少槽位 {key}"
    assert sum(1 for k in keys if k.startswith("ref") and k[3:].isdigit()) == 9, \
        f"参考图槽位数不对：{keys}"
    evidence.screenshot(sp.page, "r2v-slots")


def test_video_audio_track_options_depend_on_material(studio_page, evidence,
                                                       fixtures_dir):
    """音轨三选一：没有素材时「参考音乐/参考视频音轨」不出现，有素材时出现。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("r2v")

    # 1) 空面板：只有「使用生成的音乐」
    options = sp.page.eval_on_selector_all(
        "#audioTrackSelect option", "els => els.map(e => e.value)")
    evidence.step(f"空面板下的音轨选项 = {options}")
    assert options == [""], f"空面板下的音轨选项应只有默认项，实际 {options}"

    # 2) 先往素材库上传一个音频素材（走真实上传链路）
    sp.goto_page("navAssets")
    sp.page.set_input_files("#alFileInput", [str(fixtures_dir["audio"])])
    sp.page.wait_for_timeout(4000)
    assert sp.page.query_selector("#alGrid .al-card") is not None, "素材没上传成功"
    evidence.step("已上传一个音频素材")

    # 3) 回参考生视频，把音频填进「参考音乐 1」槽位
    sp.goto_mode("r2v")
    slot = _slot(sp.page, "refaud0")
    assert slot is not None, "没有参考音乐槽位"
    slot.click()
    sp.page.wait_for_timeout(1500)
    assert sp.visible("#alPickerMask"), "素材库选择器没打开"
    card = None
    for _ in range(30):
        card = sp.page.query_selector("#alPickerBody .al-card")
        if card is not None:
            break
        sp.page.wait_for_timeout(500)
    assert card is not None, (
        "素材库选择器里没有素材：" + sp.page.inner_text("#alPickerBody")[:120])
    card.click()
    sp.page.wait_for_timeout(400)
    sp.click("#alPickerOk")
    sp.page.wait_for_timeout(900)

    # 4) 槽位填上了 -> 音轨选项应该多出「参考音乐」
    options = sp.page.eval_on_selector_all(
        "#audioTrackSelect option", "els => els.map(e => e.value)")
    evidence.step(f"填入参考音乐后的音轨选项 = {options}")
    assert "reference" in options, (
        f"填了参考音乐后应出现「使用参考音乐」，实际 {options}")
    evidence.screenshot(sp.page, "r2v-audio-track")


def test_video_r2v_submit_carries_asset_refs(studio_page, evidence, fixtures_dir):
    """素材被引用后提交里带 asset_refs（界面选择真的进了请求）。"""
    sp = studio_page
    sp.open()
    sp.goto_page("navAssets")
    sp.page.set_input_files("#alFileInput", [str(fixtures_dir["image"])])
    sp.page.wait_for_timeout(4000)

    sp.goto_mode("r2v")
    slot = _slot(sp.page, "ref0")
    slot.click()
    sp.page.wait_for_timeout(1500)
    card = sp.page.query_selector("#alPickerBody .al-card")
    assert card is not None, "素材库选择器里没有素材"
    card.click()
    sp.page.wait_for_timeout(300)
    sp.click("#alPickerOk")
    sp.page.wait_for_timeout(900)

    sp.fill("#prompt", gen_flow.prompt_text("r2v_ref_cat"))
    sp.submit_generation()
    submitted = sp.last_submitted_params()
    evidence.step(f"提交字段 = {submitted.get('fieldNames')}")
    evidence.step(f"提交 params = {submitted.get('params')}")
    assert "asset_refs" in (submitted.get("fieldNames") or []), (
        f"提交里没有 asset_refs：{submitted.get('fieldNames')}")

    job = sp.wait_for_job(timeout=240)
    evidence.step(f"任务 {job['id']} 状态 = {job['status']}")
    assert job["status"] == "done", f"任务没跑成功：{job.get('error')}"


def test_video_t2v_size_and_model_controls_present(studio_page, evidence):
    """文生视频共享面板：尺寸、模型、采样参数控件都在。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2v")
    for selector in ("#vidWidth", "#vidHeight", "#vidModelSelect",
                     "#vidSamplerSelect", "#vidSchedulerSelect", "#vidSteps"):
        assert sp.visible(selector), f"缺少控件 {selector}"
    width = sp.value("#vidWidth")
    height = sp.value("#vidHeight")
    evidence.step(f"默认尺寸 = {width}x{height}")
    assert width and height, "默认尺寸是空的"
    evidence.screenshot(sp.page, "t2v-controls")
