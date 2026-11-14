# -*- coding: utf-8 -*-
"""
交互流程图入口的浏览器回归测试（真实浏览器 + 真实后端 + 真实文件）。

【为什么要有这个测试】
「用户能在界面里点开流程图」这件事，后端的 200 只证明**文件能取到**，
不证明**界面里点得到**。这两件事会各自独立退化：
  * 前端入口被删掉 / id 改错 / 脚本没加载 -> 用户找不到入口；
  * 面板打不开 / 缩略图裂图 / .mmd 链接指向不存在的文件；
  * 窄屏下入口点不到（横向溢出 / 触控目标过小）。

【断言纪律】
  * 每条断言由「命名该行为的那条断言」抓住：
      §1 入口存在且可点   -> test_nav_entry_exists_and_is_clickable
      §2 面板打开有 5 套图 -> test_panel_lists_five_diagrams
      §3 PNG 真的加载成功  -> test_thumbnail_images_actually_load
      §4 .mmd 源文件可取   -> test_mmd_source_link_is_reachable
      §5 说明文档可取     -> test_guide_link_is_reachable
      §6 不破坏既有契约   -> test_existing_sidebar_contract_intact
  * 「图片真的加载成功」用 DOM 的 naturalWidth 判定 ——
    不断言「img 标签存在」（那等于没断言）。

运行：python3 -m pytest tests/test_flow_docs_ui.py -v
需要：playwright（chromium）。缺失时整类 skip，不算失败。
"""
import sys
import unittest
import urllib.error
import urllib.request
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

from test_frontend_defaults import StudioServer, WAIT_TIMEOUT  # noqa: E402

#: 5 套流程图（与 docs/ 下真实文件一致）
FLOW_NAMES = [
    "交互流程图-总览",
    "交互流程图-创作链路",
    "交互流程图-素材库",
    "交互流程图-内置Agent",
    "交互流程图-系统配置",
]


@unittest.skipUnless(HAS_PLAYWRIGHT, "未安装 playwright")
class TestFlowDocsUI(unittest.TestCase):
    """真实浏览器 + 真实 Studio：锁死「流程图能在界面里点开」这条契约。"""

    @classmethod
    def setUpClass(cls):
        cls.server = StudioServer(seed_jobs=5)
        assert cls.server.start(), "Studio 实例启动失败"
        cls.base = "http://127.0.0.1:%d" % cls.server.port
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.stop()

    def setUp(self):
        self.ctx = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: self.errors.append(m.text)
                     if m.type == "error" else None)
        self.page.goto(self.base + "/", wait_until="domcontentloaded")
        self.page.wait_for_function(
            "() => document.querySelectorAll('#navVideoCategories .nav-cat').length > 0",
            timeout=WAIT_TIMEOUT)
        self.page.wait_for_timeout(600)

    def tearDown(self):
        self.ctx.close()

    def _http_status(self, url):
        req = urllib.request.Request(self.base + url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, resp.headers.get("Content-Type"), resp.headers.get("Content-Length")
        except urllib.error.HTTPError as exc:
            return exc.code, None, None

    # ------------------------------------------------------ §1 入口存在
    def test_nav_entry_exists_and_is_clickable(self):
        """§1 左侧栏有「交互流程图」入口，且点它能打开面板。"""
        entry = self.page.query_selector("#navFlowDocs")
        self.assertIsNotNone(entry, "左侧栏缺少「交互流程图」入口")
        assert entry is not None
        self.assertTrue(entry.is_visible(), "入口不可见")

        entry.click()
        self.page.wait_for_selector("#fdModal:not([hidden])", timeout=10000)
        self.assertFalse(self.page.eval_on_selector("#fdModal", "el => el.hidden"),
                         "点了入口但面板没打开")

    # ------------------------------------------------------ §2 5 套图
    def test_panel_lists_five_diagrams(self):
        """§2 面板里列出 5 套流程图，且标题与服务端清单一致。"""
        self.page.click("#navFlowDocs")
        self.page.wait_for_selector("#fdModal:not([hidden])", timeout=10000)
        self.page.wait_for_function(
            "() => document.querySelectorAll('#fdBody .fd-card').length > 0",
            timeout=10000)
        self.page.wait_for_timeout(800)

        titles = self.page.eval_on_selector_all(
            "#fdBody .fd-card .fd-title", "els => els.map(e => e.textContent.trim())")
        self.assertEqual(len(titles), 5,
                         "面板里应有 5 套流程图，实际 %r" % titles)
        for name in FLOW_NAMES:
            short = name.replace("交互流程图-", "")
            self.assertIn(short, titles, "面板里缺少 %s" % name)

    # ------------------------------------------------------ §3 缩略图真加载
    def test_thumbnail_images_actually_load(self):
        """§3 5 张缩略图都真的加载成功（naturalWidth > 0），不是裂图。"""
        self.page.click("#navFlowDocs")
        self.page.wait_for_selector("#fdModal:not([hidden])", timeout=10000)
        self.page.wait_for_function(
            """() => {
                 const imgs = [...document.querySelectorAll('#fdBody .fd-thumb')];
                 return imgs.length === 5 &&
                        imgs.every(i => i.complete && i.naturalWidth > 0);
               }""",
            timeout=20000)
        sizes = self.page.eval_on_selector_all(
            "#fdBody .fd-thumb",
            "els => els.map(e => ({w: e.naturalWidth, h: e.naturalHeight}))")
        self.assertEqual(len(sizes), 5)
        for s in sizes:
            self.assertGreater(s["w"], 0, "缩略图没加载成功：%r" % s)
            self.assertGreater(s["h"], 0, "缩略图没加载成功：%r" % s)

    # ------------------------------------------------------ §4 .mmd 源文件
    def test_mmd_source_link_is_reachable(self):
        """§4 每套图的 .mmd 源文件链接都能真的取到（HTTP 200 + 是文本）。"""
        self.page.click("#navFlowDocs")
        self.page.wait_for_selector("#fdModal:not([hidden])", timeout=10000)
        self.page.wait_for_function(
            "() => document.querySelectorAll('#fdBody .fd-card').length === 5",
            timeout=10000)
        self.page.wait_for_timeout(500)
        hrefs = self.page.eval_on_selector_all(
            "#fdBody .fd-card .fd-link",
            "els => els.map(e => e.getAttribute('href'))")
        mmd_hrefs = [h for h in hrefs if h and h.endswith(".mmd")]
        self.assertEqual(len(mmd_hrefs), 5,
                         "应有 5 个 .mmd 源文件链接，实际 %r" % mmd_hrefs)
        for href in mmd_hrefs:
            with self.subTest(href=href):
                status, ctype, _ = self._http_status(href)
                self.assertEqual(status, 200, "源文件取不到：%s" % href)
                self.assertIn("text/plain", (ctype or ""),
                              ".mmd 应是 text/plain，实际 %r" % ctype)

    # ------------------------------------------------------ §5 说明文档
    def test_guide_link_is_reachable(self):
        """§5 《交互流程图说明.md》的链接能取到（HTTP 200）。"""
        self.page.click("#navFlowDocs")
        self.page.wait_for_selector("#fdModal:not([hidden])", timeout=10000)
        self.page.wait_for_function(
            "() => document.querySelectorAll('#fdBody .fd-card').length === 5",
            timeout=10000)
        self.page.wait_for_timeout(500)
        hrefs = self.page.eval_on_selector_all(
            "#fdBody .fd-guide a", "els => els.map(e => e.getAttribute('href'))")
        self.assertTrue(hrefs, "面板里没有说明文档入口")
        guide_hrefs = [h for h in hrefs if h and h.endswith(".md")]
        self.assertTrue(guide_hrefs, "没有指向说明文档的链接：%r" % hrefs)
        for href in guide_hrefs:
            with self.subTest(href=href):
                status, _, _ = self._http_status(href)
                self.assertEqual(status, 200, "说明文档取不到：%s" % href)

    # ------------------------------------------------------ §6 不破坏既有契约
    def test_existing_sidebar_contract_intact(self):
        """§6 加了入口之后：顶层分组仍是 7 个、默认高亮仍是 Z-IMAGE、硬件参数仍可滚到。"""
        keys = self.page.eval_on_selector_all(
            ".nav-group[data-nav-key]", "els => els.map(e => e.dataset.navKey)")
        self.assertEqual(
            keys,
            ["video", "image", "image-edit", "music", "assets", "agent", "system"],
            "顶层分组集合被改动了：%r" % keys)

        active = self.page.eval_on_selector_all(
            ".nav-item.active", "els => els.map(e => e.textContent.trim())")
        self.assertEqual(active, ["Z-IMAGE 文生图"],
                         "默认高亮被改动了：%r" % active)

        # 375px 高视口：滚到底后「硬件参数」仍要完整可见
        self.page.set_viewport_size({"width": 375, "height": 375})
        self.page.wait_for_timeout(600)
        self.page.evaluate("() => { document.getElementById('navToggle').click(); }")
        self.page.wait_for_timeout(600)
        st = self.page.evaluate("""() => {
          const el = document.getElementById('navScroll');
          el.scrollTop = el.scrollHeight;
          const sys = document.querySelector('.nav-item#navSystem').getBoundingClientRect();
          const er = el.getBoundingClientRect();
          return {
            scrollTop: el.scrollTop,
            systemVisible: sys.top >= er.top - 1 && sys.bottom <= er.bottom + 1,
          };
        }""")
        self.assertGreater(st["scrollTop"], 0, "侧栏滚不动")
        self.assertTrue(st["systemVisible"], "滚到底后「硬件参数」应可见")

    def test_no_console_errors_after_open(self):
        """§6b 打开/关闭面板不产生 console error 或 pageError。"""
        self.page.click("#navFlowDocs")
        self.page.wait_for_selector("#fdModal:not([hidden])", timeout=10000)
        self.page.wait_for_timeout(1200)
        self.page.click("#fdClose")
        self.page.wait_for_timeout(400)
        self.assertEqual(self.errors, [], "页面上有报错：%r" % self.errors[:5])


if __name__ == "__main__":
    unittest.main()
