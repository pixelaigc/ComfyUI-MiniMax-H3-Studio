# -*- coding: utf-8 -*-
"""
用例组 4：提示词「增强」。

【用户要求 B.4】
「提示词增强功能：需携带此前所有段落提示词作为上下文，保证跨段一致性」

【本组要证明的】
1. 点「增强」真的调用本地模型，产出结果回到界面（弹窗）；
2. 增强请求的**内容**是当前提示词框里的原文（不是旧值、不是空）；
3. 【跨段一致性】增强请求里**携带了此前所有段落提示词作为上下文**。

【第 3 条目前是缺陷】
现状：/api/enhance 的请求体只有 prompt / mode / spec_id / mode_ctx /
model_key / images / params —— **没有任何「上文」入口**。所以接续
第 2 段点增强时，模型看不到第 1 段的提示词，跨段一致性无从保证。

本组把这条写成**可执行的断言**：先记录请求体里出现的全部字段，
再断言存在一个「携带上文」的字段（context / history / prior / prev
之类的语义）。字段不存在 -> 用例失败 = 缺陷被测出来（而不是被跳过）。
"""
from __future__ import annotations

import json

import pytest

#: 「携带上文」的字段名候选（不同实现可能叫法不同）。
CONTEXT_FIELD_CANDIDATES = (
    "context", "contexts", "history", "prior", "previous", "prev",
    "prior_prompts", "previous_prompts", "prompt_history", "segments",
    "segment_prompts", "chain_prompts", "continuation_context",
)


def _capture_enhance(page) -> list[dict]:
    """钩住 POST /api/enhance，记录每次请求体。"""
    captured: list[dict] = []
    page.on("request", lambda req: captured.append({
        "url": req.url,
        "method": req.method,
        "body": req.post_data or "",
    }) if req.method == "POST" and "/api/enhance" in req.url else None)
    return captured


def test_enhance_produces_result_in_dialog(studio_page, evidence):
    """点增强 -> 弹窗里出现模型产出的扩写文本。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")
    original = "e2e 增强：一只猫在草地上奔跑"
    sp.fill("#prompt", original)

    captured = _capture_enhance(sp.page)
    sp.click("#enhanceBtn")
    sp.page.wait_for_timeout(2000)

    # 增强是异步的（本地模型要几十秒），等弹窗出现。
    #
    # 【为什么要重试】本机模型服务是**单槽共享**的（别的任务也在用），
    # 偶尔会返回 502（排队超时 / 上游忙）。那是**环境噪声**不是功能缺陷 ——
    # 重试一次即可。真正测的是「增强这条链路通不通」。
    for attempt in range(3):
        for _ in range(180):
            if sp.page.is_visible("#enhanceModal"):
                break
            sp.page.wait_for_timeout(1000)
        if sp.page.is_visible("#enhanceModal"):
            break
        evidence.step(f"第 {attempt + 1} 次增强没出弹窗，重试")
        sp.page.keyboard.press("Escape")
        sp.page.wait_for_timeout(500)
        sp.click("#enhanceBtn")
        sp.page.wait_for_timeout(2000)
    assert sp.page.is_visible("#enhanceModal"), (
        f"点了增强但弹窗没出现（{len(captured)} 次请求）")

    result = sp.value("#enhanceResult")
    evidence.step(f"增强结果长度 = {len(result)}")
    evidence.step(f"增强元信息 = {sp.text('#enhanceMeta')[:200]}")
    assert result.strip(), "增强结果为空"
    assert result != original, "增强结果与原文相同（没有真的增强）"
    evidence.screenshot(sp.page, "enhance-result")

    # 请求体里带的是当前提示词
    assert captured, "没有捕获到 /api/enhance 请求"
    body = json.loads(captured[-1]["body"])
    evidence.step(f"增强请求体 keys = {sorted(body.keys())}")
    assert body.get("prompt") == original, (
        f"增强请求带的不是当前提示词：{body.get('prompt')!r}")


@pytest.mark.xfail(
    strict=True,
    reason="DEF-01：/api/enhance 没有「上文」入口（请求体只有 prompt/"
           "mode/spec_id/mode_ctx/model_key），跨段一致性无法保证。"
           "修好后请移除本 marker（strict 模式会让 XPASS 直接判失败）。")
def test_enhance_carries_previous_segment_prompts(studio_page, evidence):
    """跨段一致性：增强必须携带此前所有段落提示词作为上下文。

    【这是已知缺陷 DEF-01】现状：/api/enhance 的请求体里没有任何
    「上文」字段（只有 prompt / mode / spec_id / mode_ctx / model_key），
    所以第 2 段点增强时模型看不到第 1 段的提示词。

    【为什么用 strict xfail 而不是普通 xfail】
    strict=True 时，一旦有人把这个能力补上，XPASS 会被判**失败** ——
    强迫他把本 marker 摘掉、把缺陷清单划掉。这样缺陷不会「修好了却被
    静默遗忘」。
    """
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")

    captured = _capture_enhance(sp.page)

    # 模拟「此前已有若干段落提示词」的上下文：先把它们放进提示词历史
    # （这���用户真实用过的「历史提示词」面板），再增强当前段。
    segment_prompts = [
        "第一段：月光下的竹林，黑衣剑客背对镜头",
        "第二段：白衣剑客转身，剑光照亮面部",
    ]
    for text in segment_prompts:
        sp.fill("#prompt", text)
        # 提交一次（会留痕到提示词历史）
        sp.submit_generation()

    sp.fill("#prompt", "第三段：两人对峙，剑锋相交")
    sp.click("#enhanceBtn")
    for _ in range(180):
        if sp.page.is_visible("#enhanceModal") or captured:
            break
        sp.page.wait_for_timeout(1000)

    assert captured, "没有捕获到 /api/enhance 请求"
    body = json.loads(captured[-1]["body"])
    evidence.step(f"增强请求体 = {json.dumps(body, ensure_ascii=False)[:600]}")
    keys = set(body.keys())
    evidence.step(f"请求体字段 = {sorted(keys)}")

    # 断言：请求里必须有一个「携带上文」的字段
    context_fields = [k for k in keys
                      if any(cand in k.lower() for cand in CONTEXT_FIELD_CANDIDATES)]
    evidence.step(f"疑似上下文字段 = {context_fields}")

    # 字段在，也要真的带上此前段落的提示词
    carried = ""
    for key in context_fields:
        value = body.get(key)
        if isinstance(value, str):
            carried += value
        elif isinstance(value, list):
            carried += json.dumps(value, ensure_ascii=False)
    for text in segment_prompts:
        assert text in carried, (
            f"增强请求没有携带此前段落的提示词（缺：{text!r}）。"
            f"当前请求体字段：{sorted(keys)}")
