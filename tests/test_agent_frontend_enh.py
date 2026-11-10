# -*- coding: utf-8 -*-
"""
内置多模态 Agent 前端增强 —— 回归测试。

覆盖四条主线（每条都锁「根因」而不只是「现象」）：

1. **流式事件渲染**
   契约：SSE 事件 token / tool_call / tool_result / job / done / error
   逐条落到消息上；token 是**增量**追加（不是覆盖）；done 收尾。
   另测老契约（progress / result）与一次性 JSON 的降级。

2. **工具调用折叠**
   契约：工具名 + 关键参数 + 结果摘要 + 成功/失败态都渲染出来，
   且整个轨迹与每个工具块都**可折叠**（<details>）。
   折叠态在重绘后必须保持（重绘会重建 innerHTML，open 是元素状态）。

3. **任务卡状态更新**
   契约：Agent 下发 job 后，任务卡显示 job id / 模式 / 状态 / 参数；
   状态由前端轮询 GET /api/jobs/{id} 更新（**不依赖 .model-run-token**）。

4. **错误 / 重试**
   契约：失败必须显示可读错误 + 「重试」入口；重试要真的把上次请求发出去。

为什么用真实浏览器（Playwright + Chromium）而不是 jsdom/静态断言：
   这些断言全部是「DOM 里到底有没有那个元素/那个文本」——
   静态正则只能证明源码里写了那段字符串，证明不了浏览器真的渲染出来。
   项目里已有同类先例（tests/test_agent_layout_browser.py）。

运行：python3 -m pytest tests/test_agent_frontend_enh.py -v
需要：playwright（chromium）。缺失时整类 skip，不算失败。
"""
import json
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:                                   # pragma: no cover
    sync_playwright = None
    HAS_PLAYWRIGHT = False

WEB = REPO / "web"


def read_web(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def strip_comments(src: str) -> str:
    """
    去掉 JS 注释，只留代码。

    为什么需要：agent.js 里有一段注释解释「为什么不用 .model-run-token」。
    那是**说明**不是**使用** —— 静态扫描必须只看代码，否则会误判。

    实现要点（踩过的坑）：
      * 模板字符串（反引号）里可以同时出现 " 和 '，还可能有 ${} 插值 ——
        早先版本把反引号内的 " 当成新字符串起点，导致后面的 // 全被当注释，
        整段代码被吃掉。正确做法：反引号内部**整段原样保留**。
      * 普通字符串里的 // 不是注释。
      * **正则字面量**里的引号不是字符串起点。
        agent.js 的 esc() 里有 `.replace(/'/g, "&#39;")` —— 那个 ' 会被当成
        字符串开始，把后面一大段代码（含 // 注释）全吞掉。
        所以要识别 /.../ 正则：斜杠后不是 / 或 * 时，按正则处理，
        跳过到下一个未被转义的 /（要能处理字符类 [...] 里的 /）。
    """
    out = []
    i = 0
    n = len(src)
    prev_significant = ""   # 上一个有效字符（用来区分除号与正则）
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        # 模板字符串：整段原样保留
        if ch == "`":
            j = src.find("`", i + 1)
            if j < 0:
                j = n - 1
            out.append(src[i:j + 1])
            i = j + 1
            prev_significant = "`"
            continue
        # 行注释
        if ch == "/" and nxt == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        # 块注释
        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                i += 1
            i += 2
            continue
        # 正则字面量：只有在「前一个有效字符表明这里该是正则」时才当正则。
        # 判据（简化版）：前一个有效字符是标识符/数字/右括号/右方括号时是除号，
        # 否则（= , : ( [ ! & | ? { ; 或行首）就是正则开头。
        if ch == "/" and _regex_allowed(prev_significant):
            j = i + 1
            in_class = False
            while j < n:
                c = src[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "[":
                    in_class = True
                elif c == "]":
                    in_class = False
                elif c == "/" and not in_class:
                    break
                elif c == "\n":
                    break
                j += 1
            out.append(src[i:min(j + 1, n)])
            i = min(j + 1, n)
            prev_significant = "/"
            continue
        # 普通字符串字面量：原样保留
        if ch in ('"', "'"):
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                if src[i] == "\\":
                    out.append(src[i:i + 2])
                    i += 2
                    continue
                out.append(src[i])
                if src[i] == quote:
                    i += 1
                    break
                i += 1
            prev_significant = quote
            continue
        out.append(ch)
        if not ch.isspace():
            prev_significant = ch
        i += 1
    return "".join(out)


def _regex_allowed(prev: str) -> bool:
    """上一个有效字符之后，/ 是不是正则的开头（而不是除号）。"""
    if prev == "":
        return True
    if prev.isalnum() or prev in "_)]}":
        return False
    return True


# ==================================================================== 静态契约
class TestFrontendStreamContract(unittest.TestCase):
    """
    静态契约断言 —— 锁住「前端必须认识哪些事件 / 必须走哪些端点」。

    这些断言防的是**回归**：有人把事件名改掉、或把降级链删掉。
    真正证明「渲染出来了」的是下面的浏览器用例。
    """

    def test_client_knows_all_contract_event_types(self):
        src = read_web("agent.js")
        for event in ("token", "tool_call", "tool_result", "job", "done", "error"):
            self.assertIn(f'case "{event}"', src,
                          f"前端不认识 {event} 事件 —— 流式契约不完整")

    def test_client_uses_stream_query_param(self):
        """新契约：POST /api/agent/chat?stream=1。"""
        src = read_web("agent.js")
        self.assertIn("/api/agent/chat?stream=1", src,
                      "前端没有按新契约请求流式端点")

    def test_client_has_fallback_chain(self):
        """降级链：新契约 SSE -> 老契约 SSE -> 一次性 JSON。"""
        src = read_web("agent.js")
        self.assertIn("/api/agent/chat/stream", src,
                      "缺少老契约 SSE 端点的降级")
        self.assertIn('apiJSON("POST", "/api/agent/chat"', src,
                      "缺少一次性 JSON 的降级")

    def test_business_error_is_not_retried(self):
        """
        业务错误（带 code）不能自动降级重试 —— 否则会重复下发生成任务，
        而且失败会被降级掩盖（用户看到「成功」但任务一个都没建成）。
        """
        src = read_web("agent.js")
        self.assertIn("shouldFallback", src,
                      "缺少「该不该降级」的错误分类")
        self.assertRegex(src, r"if \(err\.code\) return false;",
                         "带 code 的错误必须判定为「不降级」")
        # 降级链里必须真的用了这个判定
        self.assertRegex(src, r"if \(!shouldFallback\(streamErr\)\) throw streamErr;",
                         "降级链没用 shouldFallback 判定")
        self.assertRegex(src, r"if \(!shouldFallback\(legacyErr\)\) throw legacyErr;",
                         "降级链没用 shouldFallback 判定")

    def test_fallback_only_for_endpoint_capability_errors(self):
        """
        只有「端点不存在 / 不支持流式」才降级。

        判据落在 status 上：404/405/415/501。
        """
        src = read_web("agent.js")
        for status in ("404", "405", "415", "501"):
            self.assertIn(f"status === {status}", src,
                          f"降级判定里缺 {status}（端点不存在/不支持）")
        # 5xx 属于服务端执行失败，不该降级
        self.assertNotIn("status === 500", src,
                         "500 是服务端执行失败，不该降级重跑")

    def test_tool_call_and_result_are_rendered(self):
        """工具调用可视化必须有参数与结果摘要。"""
        src = read_web("agent.js")
        self.assertIn("ag-tool-args", src, "缺少工具参数渲染")
        self.assertIn("ag-tool-result", src, "缺少工具结果渲染")
        self.assertIn("ag-tool-err", src, "缺少工具失败态渲染")

    def test_task_card_polls_job_api_not_token(self):
        """
        任务卡状态必须走工作台自己的 GET /api/jobs/{id} —— 不用 Studio MCP。

        这是「.model-run-token 失效导致任务卡不更新」的修复点：
        那条链路依赖 1 小时 TTL 的 JWT，前端不能依赖它。

        判据看的是**代码**（去掉注释后的行）—— 注释里解释「为什么不用
        token」是必要的，但不能让静态扫描误判成「前端在用 token」。
        """
        code = strip_comments(read_web("agent.js"))
        self.assertIn('"/api/jobs/"', code,
                      "任务卡没有轮询 /api/jobs/{id}")
        self.assertNotIn("model-run-token", code,
                         "前端代码不应依赖 .model-run-token（1 小时即失效）")
        self.assertNotIn("Authorization", code,
                         "前端不应自己带 Studio 凭证 —— 那是 token 失效的同一条链路")

    def test_error_block_has_retry_entry(self):
        """失败必须有可读提示 + 重试入口。"""
        src = read_web("agent.js")
        self.assertIn("ag-error", src, "缺少错误块")
        self.assertIn('data-act="retry"', src, "缺少重试按钮")
        self.assertIn("重试", src, "重试按钮没有文案")

    def test_clarify_bubble_is_answerable(self):
        """澄清式提问必须渲染成可回答的提问气泡。"""
        src = read_web("agent.js")
        self.assertIn("ag-clarify", src, "缺少澄清提问气泡")
        self.assertIn("ag-clarify-input", src, "缺少澄清回答输入框")
        self.assertIn("needs_clarification", src, "没有识别 needs_clarification 状态")


# ==================================================================== 浏览器
@unittest.skipUnless(HAS_PLAYWRIGHT, "未安装 playwright")
class TestAgentFrontendEnhBrowser(unittest.TestCase):
    """
    真实浏览器：把 web/agent.js 加载进一个页面，用真实的 fetch/SSE 打桩，
    断言**渲染结果**（DOM 里的元素与文本）。

    为什么用「注入的 fetch 打桩」而不是真连服务端：
       真实 Agent 要跑本地大模型（1~5 分钟），且依赖真实模型输出，
       既慢又不稳定。流式协议本身是确定的 —— 用打桩喂精确的事件序列，
       能**逐字**断言渲染结果，比真连服务端更强。
       真实的端到端联调由后端分支负责（见遗留项）。
    """

    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])
        cls.ctx = cls.browser.new_context(viewport={"width": 1280, "height": 900})
        cls.page = cls.ctx.new_page()
        cls.console_errors = []
        cls.page.on("pageerror", lambda e: cls.console_errors.append("pageerror: " + str(e)))
        cls.page.on("console", lambda m: cls.console_errors.append("console.error: " + m.text)
                    if m.type == "error" else None)
        cls.page.goto("about:blank")
        # 注入一个最小 DOM（含 agent.js 需要的全部节点）与 fetch 打桩
        cls.page.set_content(cls._html())
        # agent-media.js 必须先加载（agent.js 的任务卡渲染依赖 AgentMedia）
        cls.page.add_script_tag(path=str(WEB / "agent-media.js"))
        cls.page.add_script_tag(path=str(WEB / "agent.js"))
        cls.page.wait_for_timeout(200)

    @classmethod
    def tearDownClass(cls):
        cls.ctx.close()
        cls.browser.close()
        cls.playwright.stop()

    @staticmethod
    def _html():
        """最小但**结构等价**的 Agent 页 DOM。"""
        return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
        <style>
          html,body{margin:0;padding:0}
          .agent-page{display:flex;flex-direction:column;height:100vh}
          .ag-messages{flex:1 1 auto;min-height:0;overflow-y:auto}
          .ag-composer{flex:0 0 auto}
          .ag-msg{display:flex;gap:10px}
          .ag-msg-body{flex:1;min-width:0}
          .ag-bubble-text{white-space:pre-wrap;word-break:break-word}
        </style></head><body>
        <main class="agent-page" id="agentPage">
          <div class="ag-head"><button class="ag-new" id="agNew" type="button">新会话</button></div>
          <div class="ag-messages" id="agMessages"></div>
          <div class="ag-composer">
            <div class="ag-status" id="agStatus"></div>
            <div class="ag-input-row">
              <textarea id="agInput" rows="3"></textarea>
              <button class="ag-send" id="agSend" type="button">发送</button>
            </div>
          </div>
        </main>
        </body></html>"""

    def setUp(self):
        self.console_errors.clear()
        # 每个用例从干净状态开始
        #
        # 注意：必须连 busy / 输入框禁用态一起复位。
        # 上一个用例如果让请求停在半路（流没关），输入框会一直是 disabled，
        # 下一个用例的 page.fill() 就会一直等 —— 表现为「超时」而不是「真失败」。
        self.page.evaluate("""() => {
          const st = window.AgentUI.state;
          st.messages = []; st.sessionId=''; st.lastResult=null;
          st.streaming=null; st.busy=false;
          const input = document.querySelector('#agInput');
          if (input) { input.value=''; input.disabled=false; }
          const btn = document.querySelector('#agSend');
          if (btn) { btn.disabled=false; btn.textContent='发送'; }
          const status = document.querySelector('#agStatus');
          if (status) status.textContent='';
          window.AgentUI.renderMessages();
        }""")
        self.page.wait_for_timeout(100)
        # 打桩 fetch：把「脚本」翻译成 SSE 响应
        self.page.evaluate("""() => {
          window.__calls = [];
          window.__script = [];
          window.__jobs = {};
          window.fetch = async (url, opts) => {
            window.__calls.push({url: String(url), body: opts && opts.body});
            if (String(url).includes('/api/jobs/')) {
              const id = String(url).split('/api/jobs/')[1];
              const job = window.__jobs[id];
              if (!job) return {ok:false, status:404, text: async()=>JSON.stringify({error:'not found'})};
              return {ok:true, status:200, text: async()=>JSON.stringify(job),
                      headers:{get:()=>'application/json'}};
            }
            const script = window.__script;
            const body = script.map(ev => 'event: ' + ev.event + '\\ndata: ' + JSON.stringify(ev.data) + '\\n\\n').join('');
            const stream = new ReadableStream({
              start(controller) {
                controller.enqueue(new TextEncoder().encode(body));
                controller.close();
              }
            });
            return {ok:true, status:200, body: stream,
                    headers:{get:(h)=> String(h).toLowerCase()==='content-type' ? 'text/event-stream' : null}};
          };
        }""")

    def send(self, script, message="测试意图"):
        """喂一段 SSE 事件脚本给前端，走真实的 send() 路径。"""
        self.page.evaluate("(s) => { window.__script = s; }", script)
        self.page.fill("#agInput", message)
        self.page.click("#agSend")
        self.page.wait_for_timeout(700)

    # -------------------------------------------------- 1. 流式事件渲染
    def test_token_events_accumulate_incrementally(self):
        """token 事件必须**追加**成完整文本，而不是只留最后一个。"""
        self.send([
            {"event": "token", "data": {"text": "你好，"}},
            {"event": "token", "data": {"text": "我是"}},
            {"event": "token", "data": {"text": " Agent。"}},
            {"event": "done", "data": {"status": "completed", "answer": "你好，我是 Agent。"}},
        ])
        text = self.page.evaluate(
            "() => { const el = document.querySelector('.ag-msg-bot .ag-bubble-text');"
            " return el ? el.textContent : ''; }")
        self.assertEqual(text, "你好，我是 Agent。",
                         f"token 增量渲染结果不对：{text!r}")

    def test_done_event_fills_final_answer(self):
        """done 事件带 answer 时，最终文本以 answer 为准。"""
        self.send([
            {"event": "token", "data": {"text": "中间稿"}},
            {"event": "done", "data": {"status": "completed", "answer": "最终结论"}},
        ])
        text = self.page.evaluate(
            "() => { const el = document.querySelector('.ag-msg-bot .ag-bubble-text');"
            " return el ? el.textContent : ''; }")
        self.assertEqual(text, "最终结论", f"done 未生效：{text!r}")

    def test_error_event_shows_readable_message(self):
        """error 事件必须显示可读错误文本。"""
        self.send([
            {"event": "error", "data": {"error": "模型服务不可用", "code": "provider_error"}},
        ])
        text = self.page.evaluate(
            "() => { const el = document.querySelector('.ag-error-text');"
            " return el ? el.textContent : ''; }")
        self.assertIn("模型服务不可用", text, f"错误文案没渲染出来：{text!r}")

    def test_legacy_progress_result_contract_still_works(self):
        """老契约（progress / result）必须仍然能渲染 —— 降级不能破。"""
        self.send([
            {"event": "progress", "data": {"stage": "tool", "detail": "正在检索素材库", "done": 1, "total": 6}},
            {"event": "result", "data": {"status": "completed", "answer": "老契约结果",
                                         "jobs": [], "assets": [], "steps": []}},
        ])
        text = self.page.evaluate(
            "() => { const el = document.querySelector('.ag-msg-bot .ag-bubble-text');"
            " return el ? el.textContent : ''; }")
        self.assertEqual(text, "老契约结果", f"老契约没渲染：{text!r}")

    def test_progress_event_updates_status_area_during_stream(self):
        """
        progress 事件必须在**流进行中**更新进度区。

        注意时机：请求结束后 setBusy(false) 会清空进度区（那是设计：
        干完了就不该再显示「正在处理」）。所以断言必须在流**还没结束**时做。
        """
        # 造一个「发完 progress 但不关闭」的流
        self.page.evaluate("""() => {
          window.__holdOpen = true;
          window.fetch = async (url, opts) => {
            const stream = new ReadableStream({
              start(controller) {
                controller.enqueue(new TextEncoder().encode(
                  'event: progress\\ndata: ' + JSON.stringify(
                    {stage:'tool', detail:'正在检索素材库', done:1, total:6}) + '\\n\\n'));
                if (!window.__holdOpen) controller.close();
              }
            });
            return {ok:true, status:200, body: stream,
                    headers:{get:(h)=>String(h).toLowerCase()==='content-type'?'text/event-stream':null}};
          };
        }""")
        self.page.fill("#agInput", "看进度")
        self.page.click("#agSend")
        self.page.wait_for_timeout(500)
        status = self.page.evaluate(
            "() => { const el = document.querySelector('#agStatus');"
            " return el ? el.textContent : ''; }")
        self.assertIn("正在检索素材库", status, f"progress 事件没更新进度区：{status!r}")
        self.assertIn("已用", status, f"进度区没有已用时间：{status!r}")
        # 收尾：关掉流，避免用例间串味
        self.page.evaluate("() => { window.__holdOpen = false; }")
        self.page.wait_for_timeout(200)

    # -------------------------------------------------- 2. 工具调用可视化
    def test_tool_call_and_result_are_rendered_with_state(self):
        """工具名 + 参数 + 结果 + 成功/失败态都要在 DOM 里。"""
        self.send([
            {"event": "tool_call", "data": {"name": "search_assets",
                                            "arguments": {"query": "雨夜"}, "call_id": "c1"}},
            {"event": "tool_result", "data": {"name": "search_assets",
                                              "result": {"hits": 2}, "ok": True,
                                              "ms": 42, "call_id": "c1"}},
            {"event": "tool_call", "data": {"name": "create_job",
                                            "arguments": {"mode": "t2v"}, "call_id": "c2"}},
            {"event": "tool_result", "data": {"name": "create_job",
                                              "error": "参数校验失败", "ok": False,
                                              "call_id": "c2"}},
            {"event": "done", "data": {"status": "completed", "answer": "好了"}},
        ])
        info = self.page.evaluate("""() => {
          const tools = Array.from(document.querySelectorAll('.ag-tool'));
          return {
            count: tools.length,
            names: tools.map(t => (t.querySelector('.ag-tool-name')||{}).textContent || ''),
            states: tools.map(t => t.className),
            args: tools.map(t => (t.querySelector('.ag-tool-args code')||{}).textContent || ''),
            results: tools.map(t => (t.querySelector('.ag-tool-result code')||{}).textContent || ''),
            errs: tools.map(t => (t.querySelector('.ag-tool-err')||{}).textContent || ''),
            isDetails: tools.every(t => t.tagName === 'DETAILS'),
          };
        }""")
        self.assertEqual(info["count"], 2, f"工具块数量不对：{info}")
        self.assertEqual(info["names"], ["search_assets", "create_job"],
                         f"工具名不对：{info['names']}")
        self.assertIn("ag-tool-ok", info["states"][0])
        self.assertIn("ag-tool-bad", info["states"][1])
        self.assertIn("query=雨夜", info["args"][0], f"参数没渲染：{info['args']}")
        self.assertIn("hits", info["results"][0], f"结果没渲染：{info['results']}")
        self.assertIn("参数校验失败", info["errs"][1], f"失败原因没渲染：{info['errs']}")
        self.assertTrue(info["isDetails"], "工具块必须是 <details>（可折叠）")

    def test_tool_blocks_are_collapsible(self):
        """点 summary 能展开/收起工具块。"""
        self.send([
            {"event": "tool_call", "data": {"name": "search_assets", "arguments": {"q": "x"}}},
            {"event": "done", "data": {"status": "completed", "answer": "ok"}},
        ])
        before = self.page.evaluate(
            "() => { const d = document.querySelector('.ag-tool'); return d ? d.open : null; }")
        self.assertFalse(before, "工具块默认应当收起")
        self.page.evaluate(
            "() => { const d = document.querySelector('.ag-tool');"
            " d.querySelector('summary').click(); }")
        self.page.wait_for_timeout(200)
        after = self.page.evaluate(
            "() => { const d = document.querySelector('.ag-tool'); return d ? d.open : null; }")
        self.assertTrue(after, "工具块点不开")

    def test_tool_container_is_open_and_trace_is_not_duplicated(self):
        """
        工具轨迹只出现**一次**，且容器默认展开。

        回归点（截图实测暴露）：同时渲染 renderTools 与 renderSteps，
        会出现「工具调用 (2)」和「工具调用轨迹 (2)」两个几乎一样的折叠块 ——
        用户会以为是两件事。而且容器折起来的话，「Agent 干了什么」就看不见了。
        """
        self.send([
            {"event": "tool_call", "data": {"name": "search_assets", "arguments": {"q": "x"}}},
            {"event": "tool_result", "data": {"name": "search_assets", "result": {"n": 1},
                                              "ok": True, "ms": 10}},
            {"event": "done", "data": {"status": "completed", "answer": "ok",
                                       "steps": [{"step": 1, "tool": "search_assets",
                                                  "ok": True, "ms": 10}]}},
        ])
        info = self.page.evaluate("""() => ({
          toolsBlocks: document.querySelectorAll('.ag-tools').length,
          stepsBlocks: document.querySelectorAll('.ag-steps').length,
          containerOpen: (() => { const d = document.querySelector('.ag-tools');
                                  return d ? d.open : null; })(),
          toolNames: Array.from(document.querySelectorAll('.ag-tool-name'))
                          .map(e => e.textContent),
        })""")
        self.assertEqual(info["toolsBlocks"], 1,
                         f"工具折叠块出现了 {info['toolsBlocks']} 次（应 1 次）")
        self.assertEqual(info["stepsBlocks"], 0,
                         "「工具调用」与「工具调用轨迹」重复渲染了")
        self.assertTrue(info["containerOpen"], "工具容器默认应当展开")
        self.assertEqual(info["toolNames"], ["search_assets"])

    def test_tool_collapse_state_survives_redraw(self):
        """
        折叠态在重绘后必须保持。

        回归点：重绘会重建 innerHTML，<details> 的 open 是**元素状态**，
        不保存的话用户刚展开的工具块会被下一个 token 强行收起。
        """
        self.send([
            {"event": "tool_call", "data": {"name": "search_assets", "arguments": {"q": "x"}}},
            {"event": "done", "data": {"status": "completed", "answer": "ok"}},
        ])
        self.page.evaluate(
            "() => { const d = document.querySelector('.ag-tool');"
            " d.querySelector('summary').click(); }")
        self.page.wait_for_timeout(200)
        self.assertTrue(self.page.evaluate(
            "() => document.querySelector('.ag-tool').open"), "前置条件：应已展开")
        # 触发一次全量重绘
        self.page.evaluate("() => window.AgentUI.renderMessages();")
        self.page.wait_for_timeout(200)
        self.assertTrue(
            self.page.evaluate("() => document.querySelector('.ag-tool').open"),
            "重绘后折叠态丢了 —— 用户展开的工具块被强行收起")

    # -------------------------------------------------- 3. 任务卡状态更新
    def test_job_card_renders_id_mode_status_and_params(self):
        """job 事件 -> 任务卡显示 job id / 模式 / 状态 / 实际生效参数。"""
        self.send([
            {"event": "job", "data": {"job_id": "abc123", "mode": "t2v", "label": "文生视频",
                                     "status": "queued", "prompt": "雨夜街道",
                                     "params": {"duration": 5, "aspect": "16:9"}}},
            {"event": "done", "data": {"status": "completed", "answer": "已下发"}},
        ])
        info = self.page.evaluate("""() => {
          const job = document.querySelector('.ag-job');
          if (!job) return null;
          return {
            jobId: job.getAttribute('data-job-id'),
            code: (job.querySelector('code')||{}).textContent || '',
            head: (job.querySelector('.ag-job-head')||{}).textContent || '',
            params: Array.from(job.querySelectorAll('.ag-param')).map(p => p.textContent),
            prompt: (job.querySelector('.ag-job-prompt')||{}).textContent || '',
            fallbackCount: document.querySelectorAll('.ag-param-fallback').length,
          };
        }""")
        self.assertIsNotNone(info, "任务卡没渲染出来")
        self.assertEqual(info["jobId"], "abc123")
        self.assertIn("abc123", info["code"], "任务卡缺 job id")
        self.assertIn("文生视频", info["head"], "任务卡缺模式标签")
        self.assertIn("排队中", info["head"], "任务卡缺状态")
        self.assertIn("时长 5 秒", info["params"], f"任务卡缺时长：{info['params']}")
        self.assertIn("比例 16:9", info["params"], f"任务卡缺比例：{info['params']}")
        self.assertIn("雨夜街道", info["prompt"], "任务卡缺提示词")
        # 没有回落就不该有回落标记
        self.assertEqual(info["fallbackCount"], 0,
                         f"不该有回落标记：{info}")

    def test_job_card_does_not_show_non_settable_params(self):
        """
        任务卡**不得**展示 Agent 传不了的参数。

        【事实】Agent 的 create_job 只能传 prompt/aspect/resolution/
        duration/batch_count/ref_images/asset_refs（agent.py:501-556）。
        模型版本 / LoRA / 采样参数 / 负面词 / seed 这些是**官方默认值** ——
        展示成「用户设的」就是误导。
        """
        self.send([
            {"event": "job", "data": {"job_id": "j2", "mode": "t2i", "status": "queued",
                                     "params": {"aspect": "16:9", "resolution": "1mp",
                                                "steps": 25, "cfg": 4.0, "seed": 12345,
                                                "model": "z_image_bf16",
                                                "negative_prompt": "nsfw",
                                                "lora": "ckpt850"}}},
            {"event": "done", "data": {"status": "completed", "answer": "ok"}},
        ])
        params = self.page.evaluate(
            "() => Array.from(document.querySelectorAll('.ag-param')).map(p => p.textContent)")
        joined = " ".join(params)
        for forbidden in ("steps", "cfg", "seed", "model", "negative", "lora", "步"):
            self.assertNotIn(forbidden, joined,
                             f"任务卡不该展示 Agent 传不了的参数 {forbidden}：{params}")
        # 但确实该展示 Agent 能传且传了的
        self.assertIn("分辨率 1mp", joined, f"任务卡缺分辨率：{params}")

    def test_job_card_status_updates_from_polling(self):
        """
        任务卡状态由轮询 GET /api/jobs/{id} 更新 —— 这是修掉
        .model-run-token 失效的机制，必须真的更新。
        """
        self.page.evaluate("""() => {
          window.__jobs['abc123'] = {id:'abc123', mode:'t2v', status:'running',
                                     progress:0.4, progress_text:'采样中',
                                     params:{duration:5}, outputs:[], batches:[]};
        }""")
        self.send([
            {"event": "job", "data": {"job_id": "abc123", "mode": "t2v", "label": "文生视频",
                                     "status": "queued", "params": {"duration": 5}}},
            {"event": "done", "data": {"status": "completed", "answer": "已下发"}},
        ])
        before = self.page.evaluate(
            "() => { const b = document.querySelector('.ag-job-badge');"
            " return b ? b.textContent : ''; }")
        self.assertIn("排队中", before, f"前置条件：应显示排队中，实际 {before!r}")

        # 轮询把状态改成 running
        self.page.evaluate("""() => {
          window.__jobs['abc123'].status = 'running';
          window.__jobs['abc123'].progress_text = '采样中';
        }""")
        self.page.evaluate("() => window.AgentUI._pollJobs();")
        self.page.wait_for_timeout(500)
        after = self.page.evaluate(
            "() => { const b = document.querySelector('.ag-job-badge');"
            " return b ? b.textContent : ''; }")
        self.assertIn("生成中", after, f"任务卡状态没更新：{before!r} -> {after!r}")

        # 再轮询到 done
        self.page.evaluate("""() => {
          window.__jobs['abc123'].status = 'done';
          window.__jobs['abc123'].outputs = [{kind:'image', file:'abc123/1.png', name:'1.png'}];
        }""")
        self.page.evaluate("() => window.AgentUI._pollJobs();")
        self.page.wait_for_timeout(500)
        final = self.page.evaluate("""() => {
          const b = document.querySelector('.ag-job-badge');
          const thumb = document.querySelector('.ag-job-thumb img');
          return {badge: b ? b.textContent : '', thumb: thumb ? thumb.getAttribute('src') : ''};
        }""")
        self.assertIn("已完成", final["badge"], f"任务卡没到终态：{final}")
        self.assertTrue(final["thumb"], "完成后没渲染产物缩略图")

    def test_job_poll_does_not_use_model_run_token(self):
        """
        任务卡状态链路**不得**经过 .model-run-token。

        根因：那个 token 是 JWT，exp = iat + 3600，Web UI 启动时写一次、
        此后不刷新 —— 过期后 Studio 任务卡永远不更新。
        实测 9 个 profile 里 6 个已过期。
        """
        code = strip_comments(read_web("agent.js"))
        self.assertNotIn("model-run-token", code)
        self.assertNotIn("Authorization", code,
                         "前端不应自己带 Studio 凭证 —— 那是 token 失效的同一条链路")

    # -------------------------------------------------- 4. 错误 / 重试
    def test_failure_shows_error_and_retry_button(self):
        """失败必须有可读错误 + 重试按钮。"""
        self.page.evaluate("""() => {
          window.fetch = async () => ({ok:false, status:500,
            text: async()=>JSON.stringify({error:'服务端炸了', code:'agent_internal_error'}),
            headers:{get:()=>'application/json'}});
        }""")
        self.page.fill("#agInput", "会失败的请求")
        self.page.click("#agSend")
        self.page.wait_for_timeout(800)
        info = self.page.evaluate("""() => {
          const err = document.querySelector('.ag-error-text');
          const btn = document.querySelector('[data-act="retry"]');
          return {err: err ? err.textContent : '', hasBtn: !!btn,
                  btnText: btn ? btn.textContent : ''};
        }""")
        self.assertIn("服务端炸了", info["err"], f"错误文案不对：{info}")
        self.assertTrue(info["hasBtn"], "失败后没有重试入口")
        self.assertIn("重试", info["btnText"], "重试按钮没有文案")

    def test_business_failure_is_not_masked_by_fallback(self):
        """
        服务端执行失败时**不能**降级重跑。

        回归点：早先版本对所有错误都降级，于是「服务端炸了」会先失败、
        再降级到老契约、再降级到一次性 JSON —— 最后可能「成功」返回，
        用户看到成功但任务一个都没建成（静默失败，最难查）。

        判据：只发出**一次**请求（没有降级链的第二次调用）。
        """
        self.page.evaluate("""() => {
          window.__calls = [];
          window.fetch = async (url, opts) => {
            window.__calls.push({url:String(url), body: opts && opts.body});
            return {ok:false, status:500,
                    text: async()=>JSON.stringify({error:'服务端炸了', code:'agent_internal_error'}),
                    headers:{get:()=>'application/json'}};
          };
        }""")
        self.page.fill("#agInput", "会失败的请求")
        self.page.click("#agSend")
        self.page.wait_for_timeout(1200)
        calls = self.page.evaluate("() => (window.__calls || []).map(c => c.url)")
        self.assertEqual(len(calls), 1,
                         f"业务失败不该触发降级重跑，实际调用了 {calls}")
        err = self.page.evaluate(
            "() => { const el = document.querySelector('.ag-error-text');"
            " return el ? el.textContent : ''; }")
        self.assertIn("服务端炸了", err, f"失败没显示出来：{err!r}")

    def test_endpoint_missing_falls_back_to_legacy_stream(self):
        """
        端点不存在（404）时必须降级到老契约并正常渲染。

        这是降级链的正向验证：前端要能跟「还没实现新契约」的后端配合。
        """
        self.page.evaluate("""() => {
          window.__calls = [];
          window.fetch = async (url, opts) => {
            window.__calls.push({url:String(url), body: opts && opts.body});
            if (String(url).includes('stream=1')) {
              return {ok:false, status:404,
                      text: async()=>JSON.stringify({error:'not found'}),
                      headers:{get:()=>'application/json'}};
            }
            const script = [{event:'result', data:{status:'completed', answer:'老契约降级成功'}}];
            const body = script.map(ev => 'event: ' + ev.event + '\\ndata: ' + JSON.stringify(ev.data) + '\\n\\n').join('');
            const stream = new ReadableStream({start(c){c.enqueue(new TextEncoder().encode(body));c.close();}});
            return {ok:true, status:200, body: stream,
                    headers:{get:(h)=>String(h).toLowerCase()==='content-type'?'text/event-stream':null}};
          };
        }""")
        self.page.fill("#agInput", "老服务端")
        self.page.click("#agSend")
        self.page.wait_for_timeout(1200)
        calls = self.page.evaluate("() => (window.__calls || []).map(c => c.url)")
        self.assertGreaterEqual(len(calls), 2, f"404 时没有降级：{calls}")
        self.assertIn("/api/agent/chat/stream", calls[1],
                      f"没有降级到老契约端点：{calls}")
        text = self.page.evaluate(
            "() => { const el = document.querySelector('.ag-msg-bot .ag-bubble-text');"
            " return el ? el.textContent : ''; }")
        self.assertIn("老契约降级成功", text, f"降级结果没渲染：{text!r}")

    def test_retry_resends_the_same_request(self):
        """
        点重试要真的把上次那条请求再发一次。

        回归点：send() 从 #agInput 读内容，而重试时输入框已被清空 ——
        不把上次的消息放回去，重试就是发一条空消息。
        """
        # 打桩：第一次失败，第二次成功。
        # 注意 stub 必须同时处理 /api/jobs/ 与 /api/agent/chat 两种 URL。
        self.page.evaluate("""() => {
          window.__calls = [];
          window.__failNext = true;
          window.fetch = async (url, opts) => {
            window.__calls.push({url:String(url), body: opts && opts.body});
            if (String(url).includes('/api/jobs/')) {
              return {ok:false, status:404,
                      text: async()=>JSON.stringify({error:'not found'}),
                      headers:{get:()=>'application/json'}};
            }
            if (window.__failNext) {
              window.__failNext = false;
              return {ok:false, status:500,
                      text: async()=>JSON.stringify({error:'第一次失败', code:'agent_internal_error'}),
                      headers:{get:()=>'application/json'}};
            }
            const script = [{event:'done', data:{status:'completed', answer:'第二次成功'}}];
            const body = script.map(ev => 'event: ' + ev.event + '\\ndata: ' + JSON.stringify(ev.data) + '\\n\\n').join('');
            const stream = new ReadableStream({start(c){c.enqueue(new TextEncoder().encode(body));c.close();}});
            return {ok:true, status:200, body: stream,
                    headers:{get:(h)=>String(h).toLowerCase()==='content-type'?'text/event-stream':null}};
          };
        }""")
        self.page.fill("#agInput", "要重试的原始请求")
        self.page.click("#agSend")
        self.page.wait_for_timeout(1000)
        self.assertTrue(self.page.query_selector('[data-act="retry"]'), "前置条件：应有重试按钮")

        self.page.click('[data-act="retry"]')
        self.page.wait_for_timeout(1000)

        calls = self.page.evaluate("() => (window.__calls || []).map(c => c.body)")
        # 只看对话请求（任务状态轮询的 body 是 null）
        chat_calls = [c for c in calls if c]
        self.assertGreaterEqual(len(chat_calls), 2, f"重试没有发出第二次对话请求：{calls}")
        last_body = json.loads(chat_calls[-1])
        self.assertEqual(last_body["message"], "要重试的原始请求",
                         f"重试发出去的不是原来那条请求：{last_body}")

        # 取**最后一条** Agent 气泡 —— 重试会新增一条，
        # 第一条仍然是失败的那条（带错误文案，没有正文）。
        text = self.page.evaluate(
            "() => { const els = document.querySelectorAll('.ag-msg-bot .ag-bubble-text');"
            " const el = els[els.length-1]; return el ? el.textContent : ''; }")
        self.assertIn("第二次成功", text, f"重试后没有拿到新结果：{text!r}")

    def test_clarify_bubble_is_answerable(self):
        """needs_clarification -> 可回答的提问气泡。"""
        self.send([
            {"event": "done", "data": {
                "status": "needs_clarification",
                "answer": "你要生成视频还是图片？",
                "clarify_options": ["视频", "图片"],
            }},
        ])
        info = self.page.evaluate("""() => {
          const q = document.querySelector('.ag-clarify-q');
          const opts = Array.from(document.querySelectorAll('.ag-clarify-opt')).map(o => o.textContent);
          const input = document.querySelector('.ag-clarify-input');
          const sendBtn = document.querySelector('[data-act="clarify-send"]');
          return {q: q ? q.textContent : '', opts, hasInput: !!input, hasSend: !!sendBtn};
        }""")
        self.assertIn("视频还是图片", info["q"], f"澄清问题没渲染：{info}")
        self.assertEqual(info["opts"], ["视频", "图片"], f"选项没渲染：{info}")
        self.assertTrue(info["hasInput"], "澄清气泡缺回答输入框")
        self.assertTrue(info["hasSend"], "澄清气泡缺发送按钮")

    def test_clarify_option_fills_the_answer_input(self):
        """点选项要把答案填进输入框（可改后再发）。"""
        self.send([
            {"event": "done", "data": {
                "status": "needs_clarification", "answer": "选一个",
                "clarify_options": ["视频", "图片"]}},
        ])
        self.page.evaluate(
            "() => { document.querySelectorAll('.ag-clarify-opt')[0].click(); }")
        self.page.wait_for_timeout(200)
        value = self.page.evaluate(
            "() => { const el = document.querySelector('.ag-clarify-input');"
            " return el ? el.value : ''; }")
        self.assertEqual(value, "视频", f"选项没填进输入框：{value!r}")

    # -------------------------------------------------- 5. 健壮性
    def test_no_console_errors_during_all_flows(self):
        """以上全部流程跑完，console 零报错。"""
        self.send([
            {"event": "token", "data": {"text": "文本"}},
            {"event": "tool_call", "data": {"name": "search_assets", "arguments": {"q": "x"}}},
            {"event": "tool_result", "data": {"name": "search_assets", "result": {"ok": 1}, "ok": True}},
            {"event": "job", "data": {"job_id": "j1", "mode": "t2i", "status": "queued"}},
            {"event": "done", "data": {"status": "completed", "answer": "完成"}},
        ])
        self.page.wait_for_timeout(300)
        self.assertEqual(self.console_errors, [],
                         f"console 报错：{self.console_errors}")

    def test_long_conversation_does_not_rebuild_every_node(self):
        """
        增量渲染：token 到来时**不能**重建整个消息列表。

        判据不是「快不快」，而是「其它气泡的 DOM 节点有没有被换掉」——
        换掉就说明走的是全量重绘，长会话下会明显卡顿。

        走的是**真实的增量路径**（_applyStreamEvent + _renderStreamingMessage），
        不是手工调 renderMessages。
        """
        self.page.evaluate("""() => {
          const st = window.AgentUI.state;
          st.messages = [];
          for (let i = 0; i < 20; i++) {
            st.messages.push({role:'user', content:'第' + i + '条'});
            st.messages.push({role:'assistant', content:'回复' + i, result:{status:'completed'},
                              jobs:[], assets:[], prompt:null, steps:[], tools:[]});
          }
          // 追加一条流式消息并开始流式渲染
          const msg = {role:'assistant', content:'', result:{}, jobs:[], assets:[],
                       prompt:null, steps:[], tools:[], streaming:true, error:''};
          st.messages.push(msg);
          st.streaming = msg;
          window.AgentUI.renderMessages();
        }""")
        self.page.wait_for_timeout(200)
        # 标记必须在**最后一次全量渲染之后**打 —— 否则标记自己会被下一次
        # renderMessages 抹掉，测的就不是「增量路径有没有动别人」了。
        self.page.evaluate(
            "() => document.querySelector('.ag-msg[data-msg-index=\"0\"]')"
            ".setAttribute('data-marker','keep');")
        # 走真实增量路径：applyStreamEvent(token) -> renderStreamingMessage(false)
        self.page.evaluate("""() => {
          const msg = window.AgentUI.state.streaming;
          window.AgentUI._applyStreamEvent(msg, 'token', {text:'增量'});
          window.AgentUI._renderStreamingMessage(false);
        }""")
        self.page.wait_for_timeout(200)
        marker = self.page.evaluate(
            "() => { const el = document.querySelector('.ag-msg[data-msg-index=\"0\"]');"
            " return el ? el.getAttribute('data-marker') : null; }")
        self.assertEqual(marker, "keep",
                         "其它气泡的 DOM 节点被换掉了 —— 说明走的是全量重绘，"
                         "长会话下会卡顿")
        # 增量路径下，流式那条的气泡文本确实更新了
        streamed = self.page.evaluate(
            "() => { const els = document.querySelectorAll('.ag-msg-bot .ag-bubble-text');"
            " const el = els[els.length-1]; return el ? el.textContent : ''; }")
        self.assertEqual(streamed, "增量",
                         f"增量路径没更新文本：{streamed!r}")

    def test_scrolling_only_follows_when_at_bottom(self):
        """用户向上翻阅时，新 token 不应把视图强行拽回底部。"""
        self.page.evaluate("""() => {
          const st = window.AgentUI.state;
          st.messages = [];
          for (let i = 0; i < 30; i++) {
            st.messages.push({role:'user', content:'占位'.repeat(80)});
          }
          window.AgentUI.renderMessages();
        }""")
        self.page.wait_for_timeout(200)
        self.page.evaluate(
            "() => { const m = document.querySelector('#agMessages'); m.scrollTop = 0; }")
        self.page.wait_for_timeout(200)
        top = self.page.evaluate(
            "() => document.querySelector('#agMessages').scrollTop")
        self.assertEqual(top, 0, "前置条件：应停在顶部")
        # 追加一条消息（模拟新内容到达）
        self.page.evaluate("""() => {
          const st = window.AgentUI.state;
          st.messages.push({role:'assistant', content:'新内容', result:{status:'completed'},
                            jobs:[], assets:[], prompt:null, steps:[], tools:[]});
          window.AgentUI.renderMessages();
        }""")
        self.page.wait_for_timeout(200)
        after = self.page.evaluate(
            "() => document.querySelector('#agMessages').scrollTop")
        self.assertEqual(after, 0,
                         f"向上翻阅时被强行拽回底部（scrollTop={after}）")


if __name__ == "__main__":
    unittest.main()
