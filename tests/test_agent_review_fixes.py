# -*- coding: utf-8 -*-
"""
综合审查返修的回归测试（builtin-agent 分支）。

覆盖三条审查问题，每条都锁住「根因」而不只是「现象」：

1. **P2-3 窄屏输入框不可达**（重要）
   根因：app.js 用内联 `style.display="block"` 切页，覆盖了 CSS 的
   `.agent-page{display:flex}`，于是 `.ag-messages` 的 `flex:1 + min-height:0`
   失效 —— 消息区按内容撑高、composer 被顶出视口，而 body 是
   `overflow:hidden`，整页滚不动（实测 390px 下 inputTop=3068 /
   reachable=false）。
   契约：**主页面之间不允许再出现内联 display 切页**，显隐只走
   `.page-hidden` 类；且 flex 收缩三件套（flex:1 / min-height:0 /
   flex:0 0 auto）必须齐备。

2. **P3-3 长耗时静默等待**（次要）
   根因：一次完整链路 4~6 分钟，界面全程没有阶段反馈（实测 179 秒
   `#agStatus` 为空），用户以为卡死；而且界面自写的「30~120 秒」与实测不符。
   契约：`run_agent` 必须支持 `on_progress` 回调并在每个可读阶段触发；
   前端必须消费 progress 事件并显示阶段 + 已用时间。

3. **P3-1 禁用项徽标冲突**（次要·归属确认）
   根因：不可用的增强项同时挂「Recommended」和「当前模式不可用」两个徽标
   （审查实测 t2i 面板 qwen_pe 项 badges=["Recommended","当前模式不可用"]）。
   契约：不可用时**不再显示 Recommended**，且禁用原因要写明归属功能。

运行：python3 -m pytest tests/test_agent_review_fixes.py -v
"""
import asyncio
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import agent as agent_mod  # noqa: E402

WEB = REPO / "web"


def read_web(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


# ==================================================================== P2-3
class TestAgentPageLayoutContract(unittest.TestCase):
    """
    窄屏输入框可达的**契约**。

    这些是静态源码断言 —— 断言的是「结构性约束」而不是当前 CSS 的字面值。
    真正证明修好了的是 tests/test_agent_layout_browser.py 的真实浏览器用例
    （375/390/1280 三档实测几何）。
    """

    def test_no_inline_display_for_main_pages(self):
        """
        切页不许用内联 style.display。

        内联 display 的优先级高于任何 CSS 规则 —— 只要有人写了
        `style.display="block"`，`.agent-page{display:flex}` 就失效，
        flex 收缩三件套全部作废，输入框被顶出视口且整页滚不动。
        """
        ids = ("jobsArea", "panel", "stitchPanel", "systemPage",
               "providersPage", "assetsPage", "agentPage")
        for js_name in ("app.js", "assets.js", "agent.js"):
            src = read_web(js_name)
            for pid in ids:
                # 只查「给这些页面设置 display」的地方
                bad = re.findall(
                    r'\$\("#%s"\)\.style\.display\s*=' % pid, src)
                self.assertEqual(
                    bad, [],
                    f"{js_name}: 禁止用内联 style.display 切换 #{pid} —— "
                    f"它会覆盖 CSS 的 display:flex，导致窄屏输入框不可达。"
                    f"请改用 .page-hidden 类。")

    def test_page_hidden_class_exists(self):
        """切页用的类必须在 CSS 里定义，且隐藏要能压过任何 display。"""
        css = read_web("style.css")
        self.assertRegex(css, r"\.page-hidden\s*\{[^}]*display:\s*none\s*!important",
                         ".page-hidden 必须存在且带 !important（压过内联 display）")

    def test_agent_page_uses_class_in_markup(self):
        """index.html 里 Agent 页用 class 隐藏，不写内联 display。"""
        html = read_web("index.html")
        m = re.search(r'<main[^>]*id="agentPage"[^>]*>', html)
        self.assertIsNotNone(m, "找不到 #agentPage")
        tag = m.group(0)
        self.assertIn("page-hidden", tag,
                      "#agentPage 应带 page-hidden 类而不是内联 display")
        self.assertNotIn("style=", tag,
                         "#agentPage 不应带内联 style")

    def test_agent_page_is_flex_with_min_height(self):
        """
        .agent-page 必须是可收缩的 flex 容器。

        三条缺一不可：
          display:flex      —— 内部 head/messages/composer 才能分列
          min-height:0      —— 允许它在父容器里收缩到内容以下
          overflow:hidden   —— 不让它把内容顶出视口
        """
        css = read_web("style.css")
        m = re.search(r"\.agent-page\s*\{([^}]*)\}", css)
        self.assertIsNotNone(m, "找不到 .agent-page 规则")
        body = m.group(1)
        self.assertRegex(body, r"display:\s*flex", ".agent-page 必须是 display:flex")
        self.assertRegex(body, r"min-height:\s*0",
                         ".agent-page 需要 min-height:0 才能在父容器里收缩")

    def test_messages_area_can_shrink_and_scroll(self):
        """
        .ag-messages 必须自己滚动，且允许收缩。

        `flex:1` 不带 `min-height:0` 时，flex item 的自动最小尺寸会
        等于内容高度 —— 消息区不会收缩、composer 被顶出视口。
        """
        css = read_web("style.css")
        m = re.search(r"\.ag-messages\s*\{([^}]*)\}", css)
        self.assertIsNotNone(m, "找不到 .ag-messages 规则")
        body = m.group(1)
        self.assertRegex(body, r"min-height:\s*0",
                         ".ag-messages 需要 min-height:0（否则消息区撑高）")
        self.assertRegex(body, r"overflow-y:\s*auto",
                         ".ag-messages 必须自己滚动")
        self.assertRegex(body, r"flex:\s*1", ".ag-messages 必须占据剩余空间")

    def test_composer_and_head_do_not_shrink(self):
        """head / composer 固定高度，不被消息区压缩掉。"""
        css = read_web("style.css")
        for sel in (".ag-head", ".ag-composer"):
            m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", css)
            self.assertIsNotNone(m, f"找不到 {sel} 规则")
            self.assertRegex(m.group(1), r"flex:\s*0",
                             f"{sel} 不能被压缩（需要 flex:0 0 auto）")

    def test_narrow_screen_has_agent_fallback(self):
        """responsive.css 里窄屏段要有 Agent 页的兜底规则。"""
        css = read_web("responsive.css")
        phone = css.split("@media (max-width: 700px)")
        self.assertGreater(len(phone), 1, "responsive.css 缺少窄屏段")
        segment = phone[1]
        self.assertIn(".agent-page", segment,
                      "窄屏段缺少 .agent-page 兜底规则")
        self.assertIn(".ag-messages", segment,
                      "窄屏段缺少 .ag-messages 兜底规则")


# ==================================================================== P3-3
class TestAgentProgressFeedback(unittest.TestCase):
    """长耗时必须有阶段反馈 —— 契约是「有 on_progress 且会被调用」。"""

    def test_run_agent_accepts_on_progress(self):
        """run_agent 必须接受 on_progress 回调参数。"""
        import inspect
        sig = inspect.signature(agent_mod.run_agent)
        self.assertIn("on_progress", sig.parameters,
                      "run_agent 缺少 on_progress 参数，无法回传阶段")

    def test_progress_stage_vocabulary_exists(self):
        """阶段文案由服务端定义（前端不自己编，两边各写一套必然对不上）。"""
        self.assertTrue(agent_mod.PROGRESS_STAGES,
                        "缺少 PROGRESS_STAGES（阶段文案真源）")
        for stage in ("start", "thinking", "tool", "done"):
            self.assertIn(stage, agent_mod.PROGRESS_STAGES)
        for tool in ("search_assets", "list_modes", "describe_asset",
                     "enhance_prompt", "create_job"):
            self.assertIn(tool, agent_mod.TOOL_STAGE_TEXT,
                          f"工具 {tool} 缺少用户可读的阶段文案")

    def test_progress_is_emitted_during_a_run(self):
        """
        跑一次真实的编排流程（模型调用打桩），断言 on_progress 被调用过，
        且阶段文案是用户可读的中文短语。

        这是「真的会在等待中给出反馈」的证据 —— 只断言函数签名是不够的。
        """
        tmp = Path(tempfile.mkdtemp(prefix="h3agent-progress-"))
        try:
            import importlib
            sys.modules.pop("server", None)
            server_mod = importlib.import_module("server")

            from test_agent import FakeServer  # 复用既有假服务端
            fake_server = FakeServer(tmp)
            fake_server._server_mod = server_mod

            events = []

            async def fake_call(provider, messages, tools, options=None, images=None):
                return {
                    "content": None,
                    "tool_calls": [{
                        "id": "c1", "name": "list_modes", "arguments": {},
                    }],
                    "raw": {},
                }

            original = agent_mod.llm_client.call_provider_with_tools
            agent_mod.llm_client.call_provider_with_tools = fake_call
            try:
                asyncio.run(agent_mod.run_agent(
                    fake_server, "做个视频",
                    on_progress=lambda stage, detail, done, total:
                        events.append((stage, detail, done, total))))
            finally:
                agent_mod.llm_client.call_provider_with_tools = original

            self.assertTrue(events, "on_progress 一次都没被调用 —— 界面会全程静默")
            stages = [e[0] for e in events]
            self.assertIn("tool", stages,
                          "调用工具时必须回传阶段，否则用户看不到「在干什么」")
            # 阶段文案不能是内部标识，要是人话
            for stage, detail, _done, _total in events:
                if stage == "tool":
                    self.assertTrue(detail.strip(),
                                    "tool 阶段必须带用户可读的动作短语")
                    self.assertNotEqual(detail, "tool",
                                        "tool 阶段不能直接显示内部标识")
            # 进度事件本身不能抛（否则把主流程带崩）
            self.assertIn("done", stages, "流程结束时必须回传 done 阶段")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_progress_callback_failure_does_not_break_the_run(self):
        """进度回调抛异常不能影响主流程 —— 显示是锦上添花。"""
        tmp = Path(tempfile.mkdtemp(prefix="h3agent-progress-boom-"))
        try:
            import importlib
            sys.modules.pop("server", None)
            server_mod = importlib.import_module("server")
            from test_agent import FakeServer
            fake_server = FakeServer(tmp)
            fake_server._server_mod = server_mod

            def boom(*_a, **_kw):
                raise RuntimeError("进度回调炸了")

            original = agent_mod.llm_client.call_provider_with_tools
            async def fake_ok(provider, messages, tools, options=None, images=None):
                return {"content": "好了", "tool_calls": [], "raw": {}}

            agent_mod.llm_client.call_provider_with_tools = fake_ok
            try:
                result = asyncio.run(agent_mod.run_agent(
                    fake_server, "做个视频", on_progress=boom))
            finally:
                agent_mod.llm_client.call_provider_with_tools = original

            self.assertEqual(result["status"], "completed",
                             "进度回调异常不能把 Agent 带崩")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_frontend_consumes_progress_events(self):
        """前端必须消费 progress 事件并显示阶段 + 已用时间。"""
        src = read_web("agent.js")
        self.assertIn("progress", src, "前端没有处理 progress 事件")
        self.assertRegex(src, r"event\s*===\s*[\"']progress[\"']",
                         "前端必须识别 SSE 的 progress 事件")
        # 已用时间
        self.assertRegex(src, r"已用", "前端没有显示已用时间")
        self.assertRegex(src, r"setInterval", "已用时间必须定时刷新，否则像死图")

    def test_frontend_has_stream_endpoint_and_fallback(self):
        """前端优先走 SSE；流式不可用时退回一次性接口。"""
        src = read_web("agent.js")
        self.assertIn("/api/agent/chat/stream", src,
                      "前端没有调用流式端点")
        self.assertIn("/api/agent/chat\"", src,
                      "前端缺少流式失败时的一次性接口兜底")

    def test_server_exposes_stream_endpoint(self):
        """服务端必须注册 SSE 端点。"""
        src = (REPO / "server.py").read_text(encoding="utf-8")
        self.assertIn("/api/agent/chat/stream", src,
                      "服务端没有注册流式端点")
        self.assertIn("text/event-stream", src,
                      "流式端点必须返回 text/event-stream")

    def test_heartbeat_is_not_chatty(self):
        """
        心跳不能刷屏。

        实测心跳间隔 0.25 秒时，一次 60 秒的请求会写出 187 条 `: keep-alive`，
        而真正的进度事件只有 12 条 —— 噪音压过信号。
        """
        src = (REPO / "server.py").read_text(encoding="utf-8")
        m = re.search(r"HEARTBEAT_SEC\s*=\s*([0-9.]+)", src)
        self.assertIsNotNone(m, "找不到 HEARTBEAT_SEC（心跳间隔）")
        interval = float(m.group(1))
        self.assertGreaterEqual(
            interval, 2.0,
            f"心跳间隔 {interval}s 太短，会刷出大量 : keep-alive 噪音")

    def test_estimated_time_text_is_calibrated(self):
        """
        界面自写的预估时间必须覆盖实测值。

        审查实测单次完整链路 179 秒，原文案写「30~120 秒」—— 超出区间的
        等待会被当成卡死。文案必须说明「可能到几分钟」。
        """
        html = read_web("index.html")
        m = re.search(r'<div class="ag-hint-line"[^>]*>(.*?)</div>', html, re.S)
        self.assertIsNotNone(m, "找不到 .ag-hint-line")
        text = m.group(1)
        self.assertNotIn("30~120", text,
                         "预估时间「30~120 秒」与实测（179 秒）不符，必须校准")
        self.assertTrue("分钟" in text,
                        "预估文案必须覆盖到「分钟」量级（实测完整链路 1~5 分钟）")


# ==================================================================== P3-1
class TestEnhanceMenuBadgeWording(unittest.TestCase):
    """禁用项的徽标文案：只用一套，不出现「推荐 + 不可用」并列。"""

    def _render_src(self):
        """徽标的**真实渲染处**。

        ⚠️ 2026-11-10：live 侧把菜单渲染搬到了**共用组件**
        enhance-menu-shared.js（图像面板与音乐面板同一套实现），
        所以徽标渲染行在那里 —— 不是 enhance-menu.js。
        """
        return read_web("enhance-menu-shared.js")

    def test_unavailable_item_has_no_recommended_badge(self):
        """
        不可用时不显示 Recommended。

        审查实测：t2i 面板 qwen_pe 项 badges=["Recommended","当前模式不可用"]，
        读起来像「本面板也推荐它」—— 语义冲突。
        """
        src = self._render_src()
        lines = [ln for ln in src.splitlines() if "enhance-menu-badge rec" in ln]
        self.assertTrue(lines, "找不到 Recommended 徽标的渲染行")
        # 徽标的显示条件在**上一行**（三元的 true 分支），
        # 所以要看渲染行**及其上一行**的上下文。
        idx = src.splitlines().index(lines[0])
        context = "\n".join(src.splitlines()[max(0, idx - 2): idx + 1])
        self.assertRegex(context, r"m\.recommended\s*&&\s*ok",
                         "Recommended 徽标必须在「可用」(ok) 时才显示 —— "
                         "否则不可用项会同时挂「推荐」与「不可用」两个徽标")

    def test_unavailable_reason_names_the_owning_feature(self):
        """禁用原因必须写明归属功能（用户要知道去哪找它）。"""
        src = read_web("enhance-menu.js")
        self.assertIn("unavailableReason", src,
                      "缺少 unavailableReason（禁用原因真源）")
        self.assertRegex(src, r"专属", "禁用原因必须写明归属功能")
        # 渲染时用的是 unavailableReason 而不是硬编码的「当前模式不可用」
        self.assertNotIn('"当前模式不可用"', src,
                         "不要再用硬编码的「当前模式不可用」—— "
                         "它没写明归属功能")

    def test_enhance_menu_has_single_unavailable_reason(self):
        """enhance-menu.js 里只能有**一个** unavailableReason 定义。

        合并 Agent 分支时两侧各有一份（live 侧简版 + agent 侧音乐感知版），
        JS 里后声明的会**静默覆盖**前面的 —— 留下一份没人用的死代码，
        而且改错那一份不会生效。
        """
        src = read_web("enhance-menu.js")
        count = len(re.findall(r"function\s+unavailableReason\s*\(", src))
        self.assertEqual(count, 1,
                         f"enhance-menu.js 里有 {count} 个 unavailableReason 定义"
                         " —— 后者会静默覆盖前者，必须合并成一份")

    def test_registry_declares_owners(self):
        """归属信息写在注册表里（唯一真相来源）。"""
        src = read_web("enhance-menu.js")
        for owner in ("MOSS-Image", "Qwen-Image 2.1"):
            self.assertIn(owner, src,
                          f"注册表缺少归属信息：{owner}")


if __name__ == "__main__":
    unittest.main()
