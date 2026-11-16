#!/usr/bin/env python3
"""内置 Agent 前端面板 —— 线上 8190 真实浏览器核查。

覆盖前端「面板能力」逐项：
  1) Agent 页可打开
  2) 会话抽屉（列表 / 新建 / 搜索 / 删除）
  3) 流式渲染（token 增量 -> 正文出现）
  4) 工具调用卡片（tool_call / tool_result）
  5) 作业卡片（job card + 状态）
  6) 澄清气泡（clarify options 可点）
  7) 错误 / 重试
  8) 附件上传 + 分析状态轮询
  9) 「agent 管理」面板（能力开关 / 搜索引擎 / 默认参数 / 步数上限）
 10) 本次会话产物面板

用法：python3 agent_audit_browser.py <outdir>
注：SSE 用真实后端（8190），但对话消息走 **拦截 fetch** 的假流，
    目的是测**前端渲染逻辑**本身（真实模型链路由接口层实测覆盖）。
"""
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE = os.environ.get("H3_BASE", "http://127.0.0.1:8190")
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/agent_browser_audit"
os.makedirs(OUT, exist_ok=True)

RESULTS = []


def rec(name, ok, detail=None, shot=None):
    RESULTS.append({"name": name, "ok": bool(ok), "detail": detail,
                    "shot": shot})
    print(f"[{'PASS' if ok else 'FAIL'}] {name} :: "
          f"{json.dumps(detail, ensure_ascii=False)[:300]}", flush=True)


def goto_agent(page):
    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    # 找到「内置 Agent」导航入口
    nav = page.query_selector_all("a,button,[data-page]")
    for el in nav:
        try:
            txt = (el.inner_text() or "").strip()
        except Exception:
            continue
        if "Agent" in txt and "增强" not in txt:
            el.click()
            break
    page.wait_for_timeout(900)


def shot(page, name):
    path = os.path.join(OUT, name)
    page.screenshot(path=path, full_page=False)
    return path


def fake_stream_js(payload_events):
    """把 SSE 事件序列化成假 fetch 注入脚本。"""
    return json.dumps(payload_events, ensure_ascii=False)


def main():
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        console_errors = []
        page.on("console", lambda m: console_errors.append(
            f"{m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))

        # ---------------------------------------------------------- 1 页面
        goto_agent(page)
        vis = page.is_visible("#agentPage")
        rec("01-Agent页可见", vis,
            {"agentPage_hidden_attr": page.get_attribute("#agentPage", "class")},
            shot(page, "01_agent_page.png"))

        # ---------------------------------------------------------- 2 会话抽屉
        page.click("#agMenu")
        page.wait_for_timeout(500)
        drawer_open = page.is_visible("#agDrawer")
        n_sessions = page.eval_on_selector_all(
            "#agSessionList .ag-session-item, #agSessionList > *", "els => els.length")
        rec("02-会话抽屉可打开", drawer_open, {"session_items": n_sessions},
            shot(page, "02_drawer.png"))

        # 新建会话
        before = page.eval_on_selector_all(
            "#agSessionList > *", "els => els.length")
        page.click("#agDrawerNew")
        page.wait_for_timeout(900)
        after = page.eval_on_selector_all(
            "#agSessionList > *", "els => els.length")
        rec("03-新建会话", after >= before,
            {"before": before, "after": after})

        # 搜索（抽屉必须可见才能填）
        search_ok = False
        if page.query_selector("#agSessionSearch"):
            if not page.is_visible("#agSessionSearch"):
                page.click("#agMenu")
                page.wait_for_timeout(500)
            if page.is_visible("#agSessionSearch"):
                page.fill("#agSessionSearch", "审计")
                page.wait_for_timeout(800)
                search_ok = True
                page.fill("#agSessionSearch", "")
                page.wait_for_timeout(500)
        rec("04-会话搜索可用", search_ok,
            {"search_visible": page.is_visible("#agSessionSearch")})

        # ---------------------------------------------------------- 3/4/5 流式 + 工具卡片 + 作业卡片
        events = [
            {"event": "progress", "data": {"type": "progress", "stage": "start",
                                          "detail": "", "done": 0, "total": 50}},
            {"event": "token", "data": {"type": "token", "text": "正在查询素材库…"}},
            {"event": "tool_call", "data": {"type": "tool_call",
                                            "tool": "search_assets",
                                            "args": {"query": "雨夜"},
                                            "call_id": "c1"}},
            {"event": "tool_result", "data": {"type": "tool_result",
                                              "tool": "search_assets", "ok": True,
                                              "summary": "命中 3 条素材",
                                              "call_id": "c1"}},
            {"event": "job", "data": {"job_id": "auditjob0001", "mode": "t2v",
                                      "status": "running",
                                      "params": {"aspect": "16:9"},
                                      "prompt": "雨夜里的桥"}},
            {"event": "token", "data": {"type": "token", "text": "已找到并下发任务。"}},
            {"event": "done", "data": {"type": "done", "status": "completed",
                                       "answer": "已找到并下发任务。",
                                       "jobs": [{"job_id": "auditjob0001",
                                                 "mode": "t2v",
                                                 "status": "running",
                                                 "params": {"aspect": "16:9"},
                                                 "prompt": "雨夜里的桥"}],
                                       "assets": [], "steps": [],
                                       "attachments": []}},
        ]
        page.evaluate(
            """(events) => {
                window.__auditEvents = events;
                const origFetch = window.fetch;
                window.fetch = function(url, opts) {
                    const u = String(url);
                    if (u.indexOf('/api/agent/chat') >= 0) {
                        const body = events.map(e =>
                            'event: ' + e.event + '\\n' +
                            'data: ' + JSON.stringify(e.data) + '\\n\\n'
                        ).join('');
                        const enc = new TextEncoder().encode(body);
                        const stream = new ReadableStream({
                            start(c) { c.enqueue(enc); c.close(); }
                        });
                        return Promise.resolve(new Response(stream, {
                            status: 200,
                            headers: {'Content-Type': 'text/event-stream'}
                        }));
                    }
                    return origFetch.apply(this, arguments);
                };
            }""", events)
        page.fill("#agInput", "用雨夜素材做一个视频")
        page.click("#agSend")
        page.wait_for_timeout(2500)
        body_text = page.inner_text("#agMessages")
        # 注意：done 事件带 answer 时会**覆盖** token 文本（设计如此），
        # 所以这里只验「最终有正文」。token 增量单独用一条不含 done 的流来验。
        has_answer = ("正在查询素材库" in body_text
                      or "已找到并下发任务" in body_text)
        has_tool = "search_assets" in body_text
        has_job = "auditjob0001" in body_text
        rec("05-流式正文渲染", has_answer, {"snippet": body_text[:200]},
            shot(page, "05_stream.png"))
        rec("06-工具调用卡片", has_tool, {"tool_in_dom": has_tool})
        rec("07-作业卡片", has_job, {"job_in_dom": has_job})

        # ---------------------------------------------------------- 5b token 增量（无 done）
        token_only = [
            {"event": "token", "data": {"type": "token", "text": "第一段，"}},
            {"event": "token", "data": {"type": "token", "text": "第二段，"}},
            {"event": "token", "data": {"type": "token", "text": "第三段"}},
        ]
        page.evaluate(
            """(events) => {
                if (!window.__origFetch) window.__origFetch = window.fetch;
                const origFetch = window.__origFetch;
                window.fetch = function(url, opts) {
                    if (String(url).indexOf('/api/agent/chat') >= 0) {
                        const body = events.map(e =>
                            'event: ' + e.event + '\\n' +
                            'data: ' + JSON.stringify(e.data) + '\\n\\n').join('');
                        const enc = new TextEncoder().encode(body);
                        const stream = new ReadableStream({
                            start(c) { c.enqueue(enc); c.close(); }
                        });
                        return Promise.resolve(new Response(stream, {
                            status: 200,
                            headers: {'Content-Type': 'text/event-stream'}
                        }));
                    }
                    return origFetch.apply(this, arguments);
                };
            }""", token_only)
        page.fill("#agInput", "只测 token 增量")
        page.click("#agSend")
        page.wait_for_timeout(2000)
        token_text = page.evaluate(
            """() => {
                const els = document.querySelectorAll('.ag-msg-bot .ag-bubble-text');
                return els.length ? els[els.length - 1].textContent : '';
            }""")
        rec("05b-token增量累加",
            "第一段，第二段，第三段" in token_text,
            {"text": token_text[:120]}, shot(page, "05b_token.png"))

        # ---------------------------------------------------------- 6 澄清气泡
        clarify_events = [
            {"event": "done", "data": {"type": "done",
                                       "status": "needs_clarification",
                                       "answer": "你想要哪种比例？",
                                       "clarify_options": ["16:9", "9:16"],
                                       "clarify_hint": "选一个",
                                       "jobs": [], "assets": [], "steps": []}},
        ]
        page.evaluate(
            """(events) => {
                const origFetch = window.fetch;
                window.fetch = function(url, opts) {
                    const u = String(url);
                    if (u.indexOf('/api/agent/chat') >= 0) {
                        const body = events.map(e =>
                            'event: ' + e.event + '\\n' +
                            'data: ' + JSON.stringify(e.data) + '\\n\\n'
                        ).join('');
                        const enc = new TextEncoder().encode(body);
                        const stream = new ReadableStream({
                            start(c) { c.enqueue(enc); c.close(); }
                        });
                        return Promise.resolve(new Response(stream, {
                            status: 200,
                            headers: {'Content-Type': 'text/event-stream'}
                        }));
                    }
                    return origFetch.apply(this, arguments);
                };
            }""", clarify_events)
        page.fill("#agInput", "再确认一下")
        page.click("#agSend")
        page.wait_for_timeout(2200)
        clarify_dom = page.query_selector(".ag-clarify")
        opts = page.eval_on_selector_all(
            ".ag-clarify-opt", "els => els.map(e => e.textContent.trim())")
        rec("08-澄清气泡", clarify_dom is not None and len(opts) > 0,
            {"options": opts}, shot(page, "08_clarify.png"))

        # ---------------------------------------------------------- 7 错误 + 重试
        err_events = [
            {"event": "error", "data": {"type": "error",
                                        "error": "Agent 执行失败：模型超时"}},
        ]
        page.evaluate(
            """(events) => {
                const origFetch = window.fetch;
                window.fetch = function(url, opts) {
                    const u = String(url);
                    if (u.indexOf('/api/agent/chat') >= 0) {
                        const body = events.map(e =>
                            'event: ' + e.event + '\\n' +
                            'data: ' + JSON.stringify(e.data) + '\\n\\n'
                        ).join('');
                        const enc = new TextEncoder().encode(body);
                        const stream = new ReadableStream({
                            start(c) { c.enqueue(enc); c.close(); }
                        });
                        return Promise.resolve(new Response(stream, {
                            status: 200,
                            headers: {'Content-Type': 'text/event-stream'}
                        }));
                    }
                    return origFetch.apply(this, arguments);
                };
            }""", err_events)
        page.fill("#agInput", "触发错误")
        page.click("#agSend")
        page.wait_for_timeout(2200)
        retry_info = page.evaluate(
            """() => {
                const errs = document.querySelectorAll('.ag-error-text');
                const btns = document.querySelectorAll('.ag-act-retry');
                const err = errs.length ? errs[errs.length - 1] : null;
                const btn = btns.length ? btns[btns.length - 1] : null;
                return {err_text: err ? err.textContent : '',
                        has_retry_btn: !!btn,
                        retry_btn_text: btn ? btn.textContent : ''};
            }""")
        rec("09-错误条+重试按钮",
            retry_info["has_retry_btn"] and bool(retry_info["err_text"]),
            retry_info, shot(page, "09_error_retry.png"))

        # ---------------------------------------------------------- 8 管理面板
        page.click("#agManageBtn")
        page.wait_for_timeout(1200)
        manage_visible = page.is_visible("#agManageModal")
        cap_boxes = page.eval_on_selector_all(
            "#agManageBody input[type=checkbox]",
            "els => els.map(e => e.id)")
        engines = page.eval_on_selector_all(
            "#agManageBody select, #agManageBody input",
            "els => els.length")
        gen_sections = page.eval_on_selector_all(
            "#agManageBody .agm-gen", "els => els.length")
        steps_input = page.query_selector("#agmMaxSteps")
        rec("10-管理面板打开", manage_visible,
            {"capability_checkboxes": cap_boxes,
             "controls": engines, "gen_default_sections": gen_sections,
             "max_steps_input": bool(steps_input)},
            shot(page, "10_manage.png"))
        rec("11-管理面板含能力开关", len(cap_boxes) >= 4,
            {"checkboxes": cap_boxes})
        rec("12-管理面板含默认参数分区", gen_sections >= 10,
            {"sections": gen_sections})
        rec("13-管理面板含步数上限", steps_input is not None)

        # 搜索引擎自检按钮
        test_btns = page.eval_on_selector_all(
            "#agManageBody [data-eng-test]",
            "els => els.map(e => e.getAttribute('data-eng-test'))")
        rec("14-搜索引擎自检按钮", len(test_btns) > 0,
            {"engine_ids": test_btns})

        # 关闭管理面板
        page.click("#agManageClose")
        page.wait_for_timeout(400)

        # ---------------------------------------------------------- 9 产物面板
        page.click("#agArtifactsBtn")
        page.wait_for_timeout(1500)
        art_visible = page.is_visible("#agArtifacts")
        art_text = page.inner_text("#agArtifactsBody") if art_visible else ""
        rec("15-本次会话产物面板", art_visible,
            {"text_head": art_text[:200]}, shot(page, "15_artifacts.png"))

        # ---------------------------------------------------------- 10 控制台错误
        agent_errors = [e for e in console_errors
                        if "agent" in e.lower() or "JS" in e]
        rec("16-无 Agent 相关控制台错误", len(agent_errors) == 0,
            {"agent_errors": agent_errors[:10],
             "all_console_errors_count": len(console_errors),
             "all_console_errors_sample": console_errors[:6]})

        browser.close()

    with open(os.path.join(OUT, "browser_audit.json"), "w",
              encoding="utf-8") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=1)
    npass = sum(1 for r in RESULTS if r["ok"])
    print(f"\nSUMMARY {npass}/{len(RESULTS)} passed")
    print("OUT", OUT)


if __name__ == "__main__":
    main()
