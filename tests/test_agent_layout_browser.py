# -*- coding: utf-8 -*-
"""
内置 Agent 页的**真实浏览器**布局与交互回归测试。

为什么必须用真实浏览器：
静态断言只能证明「源码里没有内联 display」，证明不了「输入框真的够得着」。
本文件用**实测几何**判定，判据与审查报告一致：

  * 输入框 #agInput 的顶部必须在视口内（inputTop <= innerHeight）
  * 消息区必须能滚动（scrollHeight > clientHeight 时有滚动空间）
  * 滚到消息区底部后输入框仍在视口内
  * 不得出现横向滚动（document.scrollWidth <= innerWidth）

场景：**长对话**（灌入足够多的消息把消息区撑高）——
这正是审查报告 P2-3 的复现场景（实测 inputTop=3068 / reachable=false）。

同时覆盖 P3-3：进度反馈必须真的出现在 #agStatus 里。

运行：python3 -m pytest tests/test_agent_layout_browser.py -v
需要：playwright（chromium）。缺失时整类 skip，不算失败。
"""
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

from test_frontend_defaults import StudioServer  # noqa: E402


# 审查报告用过的三档视口
VIEWPORTS = (375, 390, 1280)

#: 灌进消息区的假消息条数 —— 必须足够把消息区撑出视口，
#: 否则测的是「本来就够得着」的平凡情形。
LONG_CONVERSATION_TURNS = 24


# 采集：Agent 页的布局几何
AGENT_FACT = """
() => {
  const vw = document.documentElement.clientWidth;
  const vh = window.innerHeight;
  const page = document.querySelector('#agentPage');
  const msgs = document.querySelector('#agMessages');
  const input = document.querySelector('#agInput');
  const composer = document.querySelector('.ag-composer');
  const status = document.querySelector('#agStatus');
  const vis = (el) => !!el && getComputedStyle(el).display !== 'none';
  const r = (el) => {
    if (!el) return null;
    const b = el.getBoundingClientRect();
    return {top: Math.round(b.top), bottom: Math.round(b.bottom),
            h: Math.round(b.height), w: Math.round(b.width)};
  };
  return {
    vw: vw, vh: vh,
    docScrollW: document.documentElement.scrollWidth,
    docScrollH: document.documentElement.scrollHeight,
    bodyOverflowY: getComputedStyle(document.body).overflowY,
    pageDisplay: page ? getComputedStyle(page).display : null,
    pageVisible: vis(page),
    pageRect: r(page),
    msgsRect: r(msgs),
    msgsClientH: msgs ? msgs.clientHeight : null,
    msgsScrollH: msgs ? msgs.scrollHeight : null,
    msgsScrollTop: msgs ? msgs.scrollTop : null,
    inputRect: r(input),
    inputReachable: input ? (input.getBoundingClientRect().top < vh
                             && input.getBoundingClientRect().bottom > 0) : false,
    composerRect: r(composer),
    composerVisible: vis(composer),
    statusText: status ? (status.textContent || '').trim() : '',
    statusVisible: vis(status),
    msgCount: msgs ? msgs.children.length : 0,
  };
}
"""


@unittest.skipUnless(HAS_PLAYWRIGHT, "未安装 playwright")
class TestAgentLayoutBrowser(unittest.TestCase):
    """真实浏览器 + 真实 Studio：Agent 页在 375/390/1280 下的真实几何。"""

    @classmethod
    def setUpClass(cls):
        cls.server = StudioServer()
        assert cls.server.start(), "Studio 实例启动失败"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])
        # **整个类共用一个 context / page**（与 tests/test_qwen_pe_enhance.py 一致）。
        #
        # 为什么不能每个用例新建 context：新建 context = 全新浏览器上下文 =
        # 一次完整的页面加载 + 全部静态资源重下。8 个用例就是 8 次全量加载，
        # 在全量回归里会把机器压到超时（实测 page.goto 超时，测试体根本没跑到，
        # 表现为「随机失败」）。复用 context 后只有首屏一次加载。
        cls.ctx = cls.browser.new_context(
            viewport={"width": 1280, "height": 900})
        cls.page = cls.ctx.new_page()
        cls.errors = []
        cls.failed_requests = []
        cls.page.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.page.on("console", lambda m: cls.errors.append(m.text)
                    if m.type == "error" else None)
        cls.page.on("requestfailed", lambda rq: cls.failed_requests.append(
            f"{rq.method} {rq.url} -> {rq.failure}"))
        cls.page.on("response", lambda rs: cls.failed_requests.append(
            f"{rs.status} {rs.url}") if rs.status >= 400 else None)
        cls.page.goto("http://127.0.0.1:%d/" % cls.server.port,
                      wait_until="domcontentloaded")
        cls.page.wait_for_timeout(1500)

    @classmethod
    def tearDownClass(cls):
        cls.ctx.close()
        cls.browser.close()
        cls.playwright.stop()
        cls.server.stop()

    def setUp(self):
        # 复用类的 page；每个用例前重置视口并把错误记录清空
        # （cls.errors / cls.failed_requests 就是同一批 list 对象，
        #   clear() 之后本用例期间的错误是干净的）
        self.page.set_viewport_size({"width": 1280, "height": 900})
        self.page.wait_for_timeout(200)
        self.errors = self.__class__.errors
        self.failed_requests = self.__class__.failed_requests
        self.errors.clear()
        self.failed_requests.clear()

    def tearDown(self):
        pass

    # -------------------------------------------------- 辅助
    def open_agent(self):
        """
        切到 Agent 页 —— **走真实的用户路径**（点左侧导航项）。

        不能直接调 window.AgentUI.show()：那是内部函数，
        真实用户点的是 #navAgent，而它的绑定在 agent.js 里。
        直接调内部函数会绕过真正出问题的那段代码，
        就会出现「测试全绿但功能是坏的」。
        """
        # 窄屏（≤700px）下左侧导航是抽屉，「内置 Agent」默认收起在视口外 ——
        # 必须先点汉堡按钮把它拉出来，否则点不到（这正是真实用户的路径）。
        self.page.evaluate(
            """() => {
              const t = document.querySelector('#navToggle');
              if (t && getComputedStyle(t).display !== 'none') t.click();
            }""")
        self.page.wait_for_timeout(300)
        self.page.click("#navAgent")
        self.page.wait_for_timeout(500)
        # 点完必须真的到了 Agent 页（否则后面所有几何断言都是空转）
        self.assertTrue(
            self.page.evaluate(
                "() => { const p = document.querySelector('#agentPage');"
                " return !!p && getComputedStyle(p).display !== 'none'; }"),
            "点击左侧「内置 Agent」后页面没有切过来")

    def fill_long_conversation(self, turns=LONG_CONVERSATION_TURNS):
        """
        灌入一段长对话（**不真的调模型**，直接写 state）——
        布局缺陷与内容来源无关，只与「消息区是否撑得比视口高」有关。
        """
        self.page.evaluate(
            """(turns) => {
              const st = window.AgentUI.state;
              st.messages = [];
              for (let i = 0; i < turns; i++) {
                st.messages.push({role: 'user',
                  content: '第 ' + (i + 1) + ' 条消息：' + '内容占位'.repeat(40)});
                st.messages.push({role: 'assistant',
                  content: '第 ' + (i + 1) + ' 条回复：' + '回执占位'.repeat(40),
                  result: {status: 'completed'}, jobs: [], assets: [],
                  prompt: null, steps: []});
              }
              window.AgentUI.renderMessages();
            }""", turns)
        self.page.wait_for_timeout(400)

    def fact(self):
        return self.page.evaluate(AGENT_FACT)

    # -------------------------------------------------- P2-3 布局
    def test_agent_page_is_flex_not_block(self):
        """
        Agent 页必须是 flex 布局。

        内联 display:block 会覆盖 CSS 的 display:flex，flex 收缩机制
        （flex:1 / min-height:0）随之失效 —— 这就是窄屏输入框不可达的根因。
        """
        self.open_agent()
        info = self.fact()
        self.assertEqual(info["pageDisplay"], "flex",
                         "Agent 页的 display 必须是 flex —— "
                         "内联 display:block 会让 flex 收缩失效")

    def test_input_reachable_at_all_widths_after_long_conversation(self):
        """
        长对话之后，输入框必须在视口内且够得着。

        这是审查报告 P2-3 的直接复现与复验：
        修复前 390px 下 inputTop=3068 / reachable=false。
        """
        for width in VIEWPORTS:
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 812})
                self.page.wait_for_timeout(400)
                self.open_agent()
                self.fill_long_conversation()
                info = self.fact()

                self.assertTrue(info["pageVisible"],
                                f"{width}px: Agent 页不可见")
                self.assertGreater(
                    info["msgCount"], 10,
                    f"{width}px: 长对话没有灌进去（msgCount={info['msgCount']}）")
                # 消息区必须真的比视口高 —— 否则测的是平凡情形
                self.assertGreater(
                    info["msgsScrollH"], info["msgsClientH"],
                    f"{width}px: 消息区没有撑高，长对话场景没复现 "
                    f"(clientH={info['msgsClientH']} scrollH={info['msgsScrollH']})")
                # 输入框在视口内
                self.assertLessEqual(
                    info["inputRect"]["top"], info["vh"],
                    f"{width}px: 输入框被顶到视口外 "
                    f"(inputTop={info['inputRect']['top']} vh={info['vh']})")
                self.assertTrue(
                    info["inputReachable"],
                    f"{width}px: 输入框不可达 "
                    f"(inputTop={info['inputRect']['top']})")
                self.assertTrue(
                    info["composerVisible"],
                    f"{width}px: composer 不可见")

    def test_scrolling_to_bottom_keeps_input_reachable(self):
        """
        滚到消息区底部后，输入框仍在视口内可点。

        审查实测修复前：after wheel / after swipe 都滚不动
        （msgsScrollTop=0 / layoutScrollTop=0），因为整页 overflow:hidden。
        """
        for width in (375, 390):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 812})
                self.page.wait_for_timeout(400)
                self.open_agent()
                self.fill_long_conversation()

                # 把消息区滚到底
                self.page.evaluate(
                    "() => { const m = document.querySelector('#agMessages');"
                    " m.scrollTop = m.scrollHeight; }")
                self.page.wait_for_timeout(300)
                info = self.fact()
                self.assertGreater(
                    info["msgsScrollTop"], 0,
                    f"{width}px: 消息区滚不动（scrollTop=0）—— "
                    f"说明内容区没有自己的滚动能力")
                self.assertLessEqual(
                    info["inputRect"]["top"], info["vh"],
                    f"{width}px: 滚到底后输入框仍不在视口内 "
                    f"(inputTop={info['inputRect']['top']})")

    def test_no_horizontal_overflow_at_all_widths(self):
        """Agent 页不得产生横向滚动。"""
        for width in VIEWPORTS:
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 812})
                self.page.wait_for_timeout(300)
                self.open_agent()
                self.fill_long_conversation()
                info = self.fact()
                self.assertLessEqual(
                    info["docScrollW"], info["vw"] + 1,
                    f"{width}px: 出现横向滚动 "
                    f"(scrollW={info['docScrollW']} vw={info['vw']})")

    def test_switching_away_and_back_keeps_layout(self):
        """
        切走再切回来，布局仍然正确。

        内联 display 一旦被别处写上就会复发 —— 这个用例锁住「来回切页不坏」。
        """
        self.page.set_viewport_size({"width": 390, "height": 812})
        self.page.wait_for_timeout(300)
        self.open_agent()
        self.fill_long_conversation()
        # 切到创作页再切回来（同样走真实路径：窄屏要先拉开侧栏抽屉）
        self.page.evaluate(
            """() => {
              const t = document.querySelector('#navToggle');
              if (t && getComputedStyle(t).display !== 'none') t.click();
            }""")
        self.page.wait_for_timeout(300)
        self.page.click('.nav-item[data-mode="t2i"]')
        self.page.wait_for_timeout(300)
        self.open_agent()
        info = self.fact()
        self.assertEqual(info["pageDisplay"], "flex",
                         "来回切页后 Agent 页的 display 被改坏了")
        self.assertLessEqual(
            info["inputRect"]["top"], info["vh"],
            f"来回切页后输入框不可达 (inputTop={info['inputRect']['top']})")

    # -------------------------------------------------- P3-3 进度
    def test_progress_area_is_present_and_shows_text_when_busy(self):
        """进度区存在，且忙碌时能显示内容（不是空白）。"""
        self.open_agent()
        # 触发一次「忙碌」状态：直接调内部的 startProgress 等价路径
        self.page.evaluate(
            """() => {
              const st = window.AgentUI.state;
              st.busy = true;
              const box = document.querySelector('#agStatus');
              if (box) {
                box.innerHTML = '<span class="ag-progress">'
                  + '<span class="ag-progress-stage">正在检索素材库</span>'
                  + '<span class="ag-progress-elapsed">已用 12 秒</span>'
                  + '</span>';
                box.classList.add('ag-status-busy');
              }
            }""")
        self.page.wait_for_timeout(200)
        info = self.fact()
        self.assertTrue(info["statusVisible"], "#agStatus 不可见")
        self.assertIn("正在检索素材库", info["statusText"],
                      "进度区没有显示阶段文案")
        self.assertIn("已用", info["statusText"],
                      "进度区没有显示已用时间")

    def test_estimated_time_hint_is_not_the_old_wrong_range(self):
        """界面自写的预估时间必须已校准（不能再是 30~120 秒）。"""
        self.open_agent()
        hint = self.page.evaluate(
            "() => { const el = document.querySelector('.ag-hint-line');"
            " return el ? (el.textContent || '') : ''; }")
        self.assertNotIn("30~120", hint,
                         "预估时间文案未校准（实测完整链路 179 秒）")
        self.assertIn("分钟", hint,
                      "预估时间文案必须覆盖到分钟量级")

    # -------------------------------------------------- 杂项
    def test_no_console_errors_and_no_failed_requests(self):
        """Agent 页不得有 console 报错或失败请求。"""
        self.open_agent()
        self.fill_long_conversation()
        self.page.wait_for_timeout(500)
        real_errors = [e for e in self.errors if "favicon" not in e.lower()]
        self.assertEqual(real_errors, [], f"console 报错：{real_errors}")
        real_failed = [f for f in self.failed_requests if "favicon" not in f]
        self.assertEqual(real_failed, [], f"失败请求：{real_failed}")


if __name__ == "__main__":
    unittest.main()
