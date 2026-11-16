#!/usr/bin/env python3
"""聚焦核查：SSE `error` 事件之后「重试」按钮是否还在。

背景：web/agent.js 的 send() 里，成功路径无条件 `msg.retryRequest = null`。
流式路径下服务端把失败发成 **SSE error 事件**（不是 HTTP 500），
于是 sendStreamNew 正常返回 -> 走「成功」分支 -> retryRequest 被清空
-> renderError 里的 canRetry=false -> 重试按钮消失。

本脚本对比两条失败路径：
  A) HTTP 500（fetch 返回 ok:false）      —— 现有测试覆盖的那条
  B) SSE error 事件（HTTP 200 + event:error）—— 线上真实失败形态
"""
import json
import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("H3_BASE", "http://127.0.0.1:8190")
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/agent_retry_probe"
os.makedirs(OUT, exist_ok=True)

SSE_ERROR = [{
    "event": "error",
    "data": {"type": "error", "error": "Agent 执行失败：模型超时"},
}]


def install_sse_error(page):
    # 每次都从**原始** fetch 起包，避免多次包装造成污染
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
        }""", SSE_ERROR)


def probe(page, label, installer):
    installer(page)
    page.fill("#agInput", "触发失败：" + label)
    page.click("#agSend")
    page.wait_for_timeout(2500)
    return page.evaluate("""() => {
        // 取**最新**一条（最后一条），不是列表里的第一条
        const errs = document.querySelectorAll('.ag-error-text');
        const err = errs.length ? errs[errs.length - 1] : null;
        const btns = document.querySelectorAll('[data-act="retry"]');
        const btn = btns.length ? btns[btns.length - 1] : null;
        return {
            err_text: err ? err.textContent : '',
            has_retry_btn: !!btn,
            retry_btn_text: btn ? btn.textContent : '',
            ag_error_count: document.querySelectorAll('.ag-error').length,
            retry_btn_count: btns.length,
        };
    }""")


def main():
    out = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
        failed_requests = []
        page.on("response", lambda r: failed_requests.append(
            f"{r.status} {r.url}") if r.status >= 400 else None)
        page.goto(BASE + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        for el in page.query_selector_all("a,button,[data-page]"):
            try:
                t = (el.inner_text() or "")
            except Exception:
                continue
            if "Agent" in t and "增强" not in t:
                el.click()
                break
        page.wait_for_timeout(800)

        # 路径 A：HTTP 500
        def install_http_500(page):
            page.evaluate("""() => {
                if (!window.__origFetch) window.__origFetch = window.fetch;
                const origFetch = window.__origFetch;
                window.fetch = async function(url, opts) {
                    if (String(url).indexOf('/api/agent/chat') >= 0) {
                        return {ok: false, status: 500,
                                text: async () => JSON.stringify({
                                    error: '服务端炸了',
                                    code: 'agent_internal_error'}),
                                headers: {get: () => 'application/json'}};
                    }
                    return origFetch.apply(this, arguments);
                };
            }""")

        out["A_http_500"] = probe(page, "http500", install_http_500)
        page.screenshot(path=os.path.join(OUT, "A_http_500.png"))

        # 路径 B：SSE error 事件
        out["B_sse_error_event"] = probe(page, "sse-error", install_sse_error)
        page.screenshot(path=os.path.join(OUT, "B_sse_error.png"))

        # 路径 C：SSE done 事件但 status=failed
        def install_sse_failed_done(page):
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
                }""",
                [{"event": "done",
                  "data": {"type": "done", "status": "failed",
                           "error": "Agent 执行失败：服务端返回 failed",
                           "answer": "", "jobs": [], "assets": [],
                           "steps": []}}])

        out["C_sse_done_failed"] = probe(page, "sse-failed", install_sse_failed_done)
        page.screenshot(path=os.path.join(OUT, "C_sse_done_failed.png"))

        out["failed_requests"] = failed_requests[:30]
        browser.close()

    with open(os.path.join(OUT, "retry_probe.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print("OUT", OUT)


if __name__ == "__main__":
    main()
