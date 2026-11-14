# -*- coding: utf-8 -*-
"""
/docs 文档静态托管的回归测试。

【为什么要有这个测试】
「流程图能在界面里点开」这件事，如果只有人工点一次验证，下一次有人改
路由/改 mimetype/改路径拼接就会悄悄退化（比如 .mmd 又被当成卡拉OK格式、
中文名又开始 404、目录穿越的洞被打开）。这里把契约变成**可执行的断言**。

【断言纪律】
  * 每条断言由「命名该行为的那条断言」抓住：
      §1 中文名可访问      -> test_chinese_filename_is_served
      §2 目录穿越一律 404  -> test_path_traversal_is_rejected
      §3 mimetype 正确     -> test_content_types_are_explicit
      §4 目录索引可点      -> test_directory_index_lists_entries
      §5 缺文件 404        -> test_missing_file_is_404
      §6 路由真的挂上了    -> test_routes_registered
  * 用**真实的 aiohttp 测试服务器**跑真实请求 —— 不 mock 掉任何一层。
  * 期望值来自「真实文件系统的实际大小」，不是写死的常量。

运行：python3 -m pytest tests/test_docs_static.py -v
"""
import asyncio
import re
import shutil
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from h3studio import docs_static  # noqa: E402

#: 5 套流程图的主文件名（不含扩展名）—— 与 docs/ 下真实文件一一对应
FLOW_NAMES = [
    "交互流程图-总览",
    "交互流程图-创作链路",
    "交互流程图-素材库",
    "交互流程图-内置Agent",
    "交互流程图-系统配置",
]


class Resp(object):
    """把 aiohttp 响应压成同步可断言的最小视图。"""

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self):
        return self.body.decode("utf-8", errors="replace")

    def header(self, name, default=""):
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return default


class DocsStaticTest(unittest.TestCase):
    """起一个真实的 aiohttp 测试服务器跑 /docs。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="h3studio-docs-"))
        cls.docs = cls.tmp / "docs"
        cls.docs.mkdir(parents=True)
        # 造真实的文件树：5 套流程图（.png/.mmd）+ 说明文档 + 一个子目录
        for name in FLOW_NAMES:
            (cls.docs / (name + ".png")).write_bytes(
                b"\x89PNG\r\n\x1a\n" + name.encode("utf-8"))
            (cls.docs / (name + ".mmd")).write_text(
                "flowchart TD\n  A[%s] --> B[结束]\n" % name, encoding="utf-8")
        (cls.docs / "交互流程图说明.md").write_text("# 说明\n\n内容\n", encoding="utf-8")
        (cls.docs / "sub").mkdir()
        (cls.docs / "sub" / "note.md").write_text("子目录说明\n", encoding="utf-8")

        async def _make():
            app = web.Application()
            docs_static.register(app, cls.docs)
            return app

        cls.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(cls.loop)
        cls.app = cls.loop.run_until_complete(_make())

        # 【为什么 server / client 都要在同一个 loop 内启动】
        # TestClient 的构造会去取「正在运行的 event loop」（CookieJar 需要），
        # 所以不能先在 loop 外 new 出 client 再进 loop 启 server。
        async def boot():
            cls.server = TestServer(cls.app)
            await cls.server.start_server()
            cls.client = TestClient(cls.server)
            await cls.client.start_server()

        cls.loop.run_until_complete(boot())

    @classmethod
    def tearDownClass(cls):
        cls.loop.run_until_complete(cls.client.close())
        cls.loop.run_until_complete(cls.server.close())
        cls.loop.close()
        # 【重要】把当前线程的 event loop 还原 —— 不还原的话，
        # 后面走 asyncio.get_event_loop() 的库（aiohttp / Playwright sync API）
        # 会拿到这个已关闭的 loop，表现为「后面所有用例集体失败」。
        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, path):
        """发一个真实 GET 请求，返回同步视图。"""

        async def _do():
            resp = await self.client.get(path, allow_redirects=False)
            body = await resp.read()
            return Resp(resp.status, dict(resp.headers), body)

        return self.loop.run_until_complete(_do())

    # ------------------------------------------------------ §1 中文名可访问
    def test_chinese_filename_is_served(self):
        """§1 中文文件名的 PNG 能取到，且内容与磁盘上的真实文件一致。"""
        for name in FLOW_NAMES:
            with self.subTest(name=name):
                url = "/docs/" + urllib.parse.quote(name + ".png")
                resp = self._get(url)
                self.assertEqual(resp.status, 200, "中文名取不到：%s" % name)
                expected = (self.docs / (name + ".png")).read_bytes()
                self.assertEqual(resp.body, expected,
                                 "内容与磁盘文件不一致：%s" % name)

    def test_mermaid_source_is_text_not_binary(self):
        """§1b .mmd 源文件能取到，且是 UTF-8 文本（不是卡拉OK二进制）。"""
        name = FLOW_NAMES[0]
        url = "/docs/" + urllib.parse.quote(name + ".mmd")
        resp = self._get(url)
        self.assertEqual(resp.status, 200)
        ctype = resp.header("Content-Type")
        self.assertIn("text/plain", ctype,
                      ".mmd 的 Content-Type 应是 text/plain，实际 %r" % ctype)
        self.assertNotIn("karaoke", ctype.lower(),
                         ".mmd 不能被当成卡拉OK格式")
        self.assertIn("flowchart", resp.text)

    # ------------------------------------------------------ §2 目录穿越防护
    def test_path_traversal_is_rejected(self):
        """§2 各种编码形式的目录穿越一律 404，且不泄露文件内容。"""
        attacks = [
            "/docs/../server.py",
            "/docs/%2e%2e%2fserver.py",
            "/docs/%2e%2e/%2e%2e/etc/passwd",
            "/docs/..%2fserver.py",
            "/docs/%252e%252e%252fserver.py",
            "/docs/sub/../../server.py",
            "/docs/../../../../etc/passwd",
        ]
        for attack in attacks:
            with self.subTest(attack=attack):
                resp = self._get(attack)
                self.assertEqual(resp.status, 404,
                                 "目录穿越未被挡住：%s -> %s" % (attack, resp.status))
                self.assertNotIn("import", resp.text[:200],
                                 "疑似泄露了仓库文件内容：%s" % attack)

    def test_absolute_path_is_rejected(self):
        """§2b 绝对路径（/etc/passwd）不能被当成 docs 下的相对路径。"""
        resp = self._get("/docs//etc/passwd")
        self.assertIn(resp.status, (404, 403))

    # ------------------------------------------------------ §3 mimetype
    def test_content_types_are_explicit(self):
        """§3 每种扩展名的 Content-Type 正确（不是猜的）。"""
        cases = [
            (FLOW_NAMES[0] + ".png", "image/png"),
            ("交互流程图说明.md", "text/markdown"),
            (FLOW_NAMES[0] + ".mmd", "text/plain"),
        ]
        for filename, expected in cases:
            with self.subTest(filename=filename):
                url = "/docs/" + urllib.parse.quote(filename)
                resp = self._get(url)
                self.assertEqual(resp.status, 200)
                ctype = resp.header("Content-Type")
                self.assertTrue(
                    ctype.startswith(expected),
                    "%s 的 Content-Type 应以 %r 开头，实际 %r"
                    % (filename, expected, ctype))

    def test_png_content_length_matches_disk(self):
        """§3b PNG 的 Content-Length 与磁盘真实大小一致（传输完整）。"""
        name = FLOW_NAMES[0]
        url = "/docs/" + urllib.parse.quote(name + ".png")
        resp = self._get(url)
        self.assertEqual(resp.status, 200)
        expected = (self.docs / (name + ".png")).stat().st_size
        self.assertEqual(int(resp.header("Content-Length")), expected,
                         "Content-Length 与磁盘文件大小不一致")
        self.assertEqual(len(resp.body), expected)

    # ------------------------------------------------------ §4 目录索引
    def test_directory_index_lists_entries(self):
        """§4 /docs 返回索引页，列出 5 套流程图，说明文档可点。"""
        resp = self._get("/docs")
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.header("Content-Type"))
        html = resp.text
        for name in FLOW_NAMES:
            self.assertIn(name, html, "索引里缺少 %s" % name)
        self.assertIn("交互流程图说明.md", html)
        # 链接必须做百分号编码，否则浏览器点不开中文名
        self.assertIn(urllib.parse.quote("交互流程图-总览.png"), html,
                      "索引里的中文文件名链接未做 URL 编码")

    def test_subdirectory_index_works(self):
        """§4b 子目录也能出索引，且能列出其中的文件。"""
        resp = self._get("/docs/sub")
        self.assertEqual(resp.status, 200)
        self.assertIn("note.md", resp.text)

    def test_index_links_are_reachable(self):
        """§4c 索引里列出的每一条链接点得开（这是用户实际会走的路径）。"""
        resp = self._get("/docs")
        self.assertEqual(resp.status, 200)
        hrefs = re.findall(r'href="([^"]+)"', resp.text)
        # 至少要覆盖 5 张 PNG + 说明文档
        self.assertGreaterEqual(len(hrefs), len(FLOW_NAMES))
        for href in hrefs:
            if href.startswith("..") or href.endswith("/"):
                continue
            with self.subTest(href=href):
                r = self._get(href)
                self.assertEqual(r.status, 200,
                                 "索引里的链接点不开：%s -> %s" % (href, r.status))

    # ------------------------------------------------------ §5 缺文件
    def test_missing_file_is_404(self):
        """§5 不存在的文件返回 404，且不泄露文件系统路径。"""
        resp = self._get("/docs/" + urllib.parse.quote("根本没有这个文件.png"))
        self.assertEqual(resp.status, 404)
        self.assertNotIn(str(self.tmp), resp.text,
                         "404 响应泄露了服务器路径")

    def test_dotfile_is_not_listed(self):
        """§5b 隐藏文件不进索引。"""
        (self.docs / ".secret").write_text("x", encoding="utf-8")
        try:
            resp = self._get("/docs")
            self.assertNotIn(".secret", resp.text)
        finally:
            (self.docs / ".secret").unlink()

    # ------------------------------------------------------ §6 路由
    def test_routes_registered(self):
        """§6 /docs 与 /docs/{path:.+} 两条路由都真的挂上了。"""
        paths = set()
        for route in self.app.router.routes():
            # aiohttp 3.14：canonical 在 route.resource 上（route.canonical 已移除）
            resource = getattr(route, "resource", None)
            if resource is None:
                resource = getattr(route, "_resource", None)
            if resource is None:
                continue
            canonical = getattr(resource, "canonical", None)
            if canonical:
                paths.add(canonical)
        self.assertIn("/docs", paths, "缺少 /docs 路由：%r" % paths)
        self.assertTrue(any(p.startswith("/docs/") for p in paths),
                        "缺少 /docs/{path:.+} 路由：%r" % paths)


if __name__ == "__main__":
    unittest.main()
