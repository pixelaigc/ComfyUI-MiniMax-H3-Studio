# -*- coding: utf-8 -*-
"""
用例组 4：提示词「增强」。

【用户要求 B.4】
「提示词增强功能：需携带此前所有段落提示词作为上下文，保证跨段一致性」

【本组要证明的】
1. 点「增强」真的调用本地模型，产出结果回到界面（弹窗）；
2. 增强请求的**内容**是当前提示词框里的原文（不是旧值、不是空）；
3. 【跨段一致性】**接续链上**点增强时，增强请求携带**此前所有段落**的
   提示词作为上下文。

【第 3 条的实现口径（2026-11-17 并行任务落地）】
- 服务端 `server.py:_lineage_prompts()` 沿 `source_job` 血缘回溯，
  取出链首段到本段的每一段提示词；
- `GET /api/jobs/{id}/continuation` 把这份 `prompt_history` 下发给前端；
- 前端 `web/continuation.js` 的 `Continuation.promptHistory()` 暴露它，
  `web/enhance-menu.js` 在点增强时透传给 `/api/enhance`；
- 服务端 `_normalize_prompt_history()` 剔除本段自己、容错脏数据。

【为什么要走「真实接续链」才能测】
历史来自**任务血缘**（source_job 指针），不是「本面板的提示词历史」——
所以只有真的跑出一段接续链（点接续 -> 生成第 2 段），
再点增强，才能验证历史真的带上了此前段落的提示词。
"""
from __future__ import annotations

import json
import time
import urllib.request

import pytest
import gen_flow  # noqa: E402

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


def _click_enhance_and_wait(sp, captured, attempts: int = 3) -> dict:
    """点增强并等**请求发出**；返回最后一次请求体。

    【为什么要等「请求发出」而不是「弹窗出现」】
    增强要等本地模型（几十秒），而且本机模型服务是**单槽共享**的
    （别的任务也在用），偶尔会 502 / 排队很久。等请求发出就够断言
    「界面确实把提示词和上下文发出去了」—— 那是这条用例要验的东西。

    【为什么要重试】502 是环境噪声（上游忙），不是功能缺陷。
    """
    for attempt in range(attempts):
        sp.click("#enhanceBtn")
        for _ in range(180):
            if captured:
                break
            sp.page.wait_for_timeout(1000)
        if captured:
            break
        sp.ev.step(f"第 {attempt + 1} 次增强没发出请求，重试")
        hint = sp.page.inner_text("#enhanceHint")
        sp.ev.step(f"增强提示条：{hint}")
        sp.page.wait_for_timeout(500)
    assert captured, (
        "没有捕获到 /api/enhance 请求；提示条："
        + sp.page.inner_text("#enhanceHint"))
    body = json.loads(captured[-1]["body"])
    sp.ev.step(f"增强请求体 keys = {sorted(body.keys())}")
    return body


def test_enhance_produces_result_in_dialog(studio_page, evidence):
    """点增强 -> 弹窗里出现模型产出的扩写文本。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")
    original = "e2e 增强：一只猫在草地上奔跑"
    sp.fill("#prompt", original)

    captured = _capture_enhance(sp.page)
    body = _click_enhance_and_wait(sp, captured)

    # 请求发出后，弹窗最终会出现（模型出结果后）
    for _ in range(180):
        if sp.page.is_visible("#enhanceModal"):
            break
        sp.page.wait_for_timeout(1000)
    assert sp.page.is_visible("#enhanceModal"), (
        f"点了增强但弹窗没出现（{len(captured)} 次请求）；"
        f"提示条：{sp.page.inner_text('#enhanceHint')}")

    result = sp.value("#enhanceResult")
    evidence.step(f"增强结果长度 = {len(result)}")
    evidence.step(f"增强元信息 = {sp.text('#enhanceMeta')[:200]}")
    assert result.strip(), "增强结果为空"
    assert result != original, "增强结果与原文相同（没有真的增强）"
    evidence.screenshot(sp.page, "enhance-result")

    # 请求体里带的是当前提示词
    evidence.step(f"增强请求体 keys = {sorted(body.keys())}")
    assert body.get("prompt") == original, (
        f"增强请求带的不是当前提示词：{body.get('prompt')!r}")


def test_enhance_carries_previous_segment_prompts(studio_page, evidence):
    """【跨段一致性】接续链上点增强，携带此前所有段落的提示词。

    这条用例走**真实的接续链**：
        第 1 段（5 秒）-> 点接续 -> 第 2 段（5 秒）-> 点接续 -> 第 3 段
        -> 在第 3 段点增强
    此时增强请求里必须带上第 1、2 段的提示词（不含本段自己）。
    """
    sp = studio_page
    sp.open()

    # 【2026-11-19】提示词改成**对模型有意义**的中性描述 ——
    # 原来的「第一段：」「第二段：」是测试占位词，对模型毫无意义。
    segment_prompts = [
        gen_flow.prompt_text("t2v_swordsman_bamboo"),
        gen_flow.prompt_text("t2v_swordsman_turn"),
        gen_flow.prompt_text("t2v_swordsman_stand"),
    ]

    def generate_segment(prompt: str, duration: float = 5.0) -> tuple[dict, str]:
        """生成一段，返回 (任务, **实际提交**的提示词)。

        【2026-11-19】提交的是**增强后**的提示词（既定流程），
        所以断言历史时要拿这一份，而不是原文。
        """
        sp.goto_mode("t2v")
        sp.page.eval_on_selector(
            "#duration",
            "e => { e.value = %s; e.dispatchEvent(new Event('input', {bubbles:true})); }"
            % duration)
        sp.page.wait_for_timeout(300)
        sp.fill("#prompt", prompt)
        sp.submit_generation()
        submitted_prompt = (sp.last_submitted_params().get("params")
                            or {}).get("prompt") or ""
        job = _wait_job(sp.base_url)
        assert job["status"] == "done", f"任务没跑成功：{job.get('error')}"
        return job, submitted_prompt

    def continue_from(job_id: str, prompt: str,
                       duration: float = 5.0) -> tuple[dict, str]:
        """接续一段，返回 (任务, **实际提交**的提示词)。"""
        sp.goto_tab("done")
        button = None
        for _ in range(30):
            button = sp.page.query_selector(
                f'.job-card[data-jid="{job_id}"] button[data-act="continue"]')
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
        submitted_prompt = (sp.last_submitted_params().get("params")
                            or {}).get("prompt") or ""
        job = _wait_job(sp.base_url)
        assert job["status"] == "done", f"接续段没跑成功：{job.get('error')}"
        return job, submitted_prompt

    # 第 1 段
    first, prompt_1 = generate_segment(segment_prompts[0])
    evidence.step(f"第 1 段 {first['id']} 完成")
    # 第 2 段（接续第 1 段）
    second, prompt_2 = continue_from(first["id"], segment_prompts[1])
    evidence.step(f"第 2 段 {second['id']} 完成")
    # 第 3 段（接续第 2 段）—— 这一段点增强时，应当带上第 1、2 段
    third, prompt_3 = continue_from(second["id"], segment_prompts[2])
    evidence.step(f"第 3 段 {third['id']} 完成")

    # 此时接续上下文挂着第 2 段的来源 —— 再点一次接续拿到第 3 段的血脉
    sp.goto_tab("done")
    button = None
    for _ in range(30):
        button = sp.page.query_selector(
            f'.job-card[data-jid="{third["id"]}"] button[data-act="continue"]')
        if button is not None:
            break
        sp.page.wait_for_timeout(1000)
    assert button is not None, "第 3 段卡片上没有「接续」按钮"
    button.click()
    sp.page.wait_for_timeout(2500)

    # 点「接续」会**清空提示词框**（设计如此：用户要写下一段）——
    # 所以必须重新输入本段提示词，否则增强会说「请先输入要增强的提示词」。
    assert sp.value("#prompt") == "", (
        f"点接续后提示词框应被清空，实际 {sp.value('#prompt')!r}")
    sp.fill("#prompt", segment_prompts[2])
    evidence.step("已重新输入本段提示词（点接续会清空提示词框）")

    # 接续上下文里应该带着 3 段提示词（服务端下发 prompt_history）
    history = sp.page.evaluate("() => window.Continuation "
                                "&& window.Continuation.promptHistory()")
    evidence.step(f"接续上下文里的 promptHistory = {history}")
    assert history, "接续上下文没有下发 prompt_history"
    carried = [item["prompt"] for item in history]
    # 【2026-11-19】按**实际提交的提示词**（= 增强后的那一份）判定。
    #
    # 【为什么不再断言原文】修正后的真实流程是「写提示词 -> 增强 -> 生成」，
    # 所以落库的 params["prompt"] 是**增强后**的那一份（服务端
    # _lineage_prompts 读的就是 params["prompt"]）。
    # 断言原文在历史里，等于断言「用户没走增强」—— 那正是本次要修掉的路径。
    for index, expected in ((0, prompt_1), (1, prompt_2)):
        assert any(expected[:40] in text for text in carried), (
            f"接续上下文里缺第 {index + 1} 段的提示词"
            f"（该段实际提交 {expected[:60]!r}；现有 {carried}）")

    # 点增强 -> 请求体必须带上这些历史
    captured = _capture_enhance(sp.page)
    body = _click_enhance_and_wait(sp, captured)
    evidence.step(f"增强请求体 = {json.dumps(body, ensure_ascii=False)[:800]}")

    keys = set(body.keys())
    context_fields = [k for k in keys
                      if any(cand in k.lower() for cand in CONTEXT_FIELD_CANDIDATES)]
    evidence.step(f"请求体里的上下文字段 = {context_fields}")
    assert context_fields, (
        f"增强请求里没有「上文」字段（现有字段：{sorted(keys)}）")

    # 字段在，也要真的带上此前段落的提示词。
    #
    # 【2026-11-19 修正判据】原来是把上下文字段**拼成一个大字符串**
    # 再做子串匹配 —— 但 json.dumps 会把换行转义成 \n，于是原文里的
    # 换行位置与序列化后的不一致，子串匹配会**假失败**。
    # 现在改成「按列表项逐条比」。
    carried_prompts: list[str] = []
    for key in context_fields:
        value = body.get(key)
        if isinstance(value, str):
            carried_prompts.append(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    text = item.get("prompt")
                    if isinstance(text, str):
                        carried_prompts.append(text)
                elif isinstance(item, str):
                    carried_prompts.append(item)
        elif isinstance(value, dict):
            text = value.get("prompt")
            if isinstance(text, str):
                carried_prompts.append(text)
            else:
                carried_prompts.append(json.dumps(value, ensure_ascii=False))
    evidence.step(f"增强请求里带上的提示词条数 = {len(carried_prompts)}")
    for index, expected in ((0, prompt_1), (1, prompt_2)):
        assert any(expected[:60] in text for text in carried_prompts), (
            f"增强请求没有携带第 {index + 1} 段的提示词"
            f"（该段实际提交 {expected[:60]!r}）。"
            f"当前请求体字段：{sorted(keys)}；"
            f"带上的提示词：{[t[:60] for t in carried_prompts]}")
    # 前端是**原样**把接续上下文的 prompt_history 传上去的（含本段自己），
    # 剔除本段是**服务端**的职责（server.py:_normalize_prompt_history）。
    # 所以这里断言「历史完整传上去了」，剔除由服务端那条用例守住。
    assert any(prompt_3[:60] in text for text in carried_prompts), (
        "历史里应有本段（前端原样传，剔除由服务端负责）："
        f"{[t[:60] for t in carried_prompts]}")
    evidence.screenshot(sp.page, "enhance-with-history")
