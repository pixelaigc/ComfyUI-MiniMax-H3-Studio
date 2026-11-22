# -*- coding: utf-8 -*-
"""回归测试 —— 8190 对 ComfyUI(8188) 的「请求风暴」与「顶栏状态抖动」。

【这个文件锁住的是什么】

2026-11-22 现场取证（tcpdump + 浏览器 DOM 采样）发现两个缺陷：

  * **请求风暴**：8190 对 8188 的请求速率 2.3~3.7 req/s，其中
    `/queue` 2.61 req/s。放大源有三个：
      1. 每条 SSE 连接各自每秒打一次 `/queue`（9 条 SSE = 9 req/s）；
      2. `/api/system` 每次调用都新建 ClientSession 打一次
         `/system_stats` + 一次 `/queue`；
      3. `yue2_schema` 失败时**不更新缓存时间戳** -> 每次失败都立刻
         重拉全量 `/object_info`（实测 37.5 MB / 3~10 秒）。

  * **顶栏状态抖动**：用户报「未连接 / 生成中」高频来回跳。根因是
    两个写回源互相覆盖（轮询写「未连接」/ SSE 硬编码写「正常」），
    且都没有迟滞。

【测试的立场】

断言必须**抓住那条行为**，而不是复述实现：
  * 「9 条并发 SSE 只打 1 次上游」—— 断言**上游收到的请求数**，
    不是断言「有缓存」；
  * 「失败后不立刻重试」—— 断言**第二次调用没有再打上游**；
  * 「一次探测失败不判未连接」—— 断言**返回的 comfyui_ok**，
    不是断言「有迟滞函数」；
  * 「前端不再注入 comfyui_ok: true」—— 断言**源码里没有那一行**，
    并且断言**有服务端权威值被使用**。

跑法：
    /home/ubuntu/comfyui/venv/bin/python -m pytest tests/test_reqstorm_fix.py -v
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from h3studio import comfy_client  # noqa: E402


def _extract_sse_handler_js(src: str) -> str:
    """抽出 SSE onmessage 处理器 + 依赖的辅助函数（可直接喂给 node 执行）。

    抽的是：
      * `applyStatusSnapshot` / `statusLabel` / `renderStatus` / `STATUS_TEXT`
      * `startStatusStream` 整个函数（处理器在其中）
    缺一不可 —— 处理器会调 `applyStatusSnapshot`。
    """
    blocks = []
    for name in ("STATUS_TEXT", "statusLabel", "statusState", "applyStatusSnapshot",
                 "renderStatus"):
        # 注意：不抽 startStatusStream —— 它引用 EventSource / source，
        # 会污染 node 沙箱。onmessage 处理器单独抽。
        m = re.search(r"(?:const|let|function)\s+%s\b" % re.escape(name), src)
        if not m:
            raise AssertionError("app.js 里找不到 %s" % name)
        start = m.start()
        # 函数体：brace 配平（对 const 对象字面量同样适用）
        i = src.index("{", m.end())
        depth = 0
        j = i
        while j < len(src):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        blocks.append(src[start:j + 1])
        # const/let 声明可能带分号结尾
        if src[j + 1:j + 2] == ";":
            blocks[-1] += ";"
    body = "\n\n".join(blocks)
    # 最小 DOM 替身：renderStatus 要写 #statusText / #topStatus .dot。
    stub = """
const __els = {"#statusText": {textContent: "", className: ""},
               "#topStatus .dot": {className: ""}};
const document = {querySelector: (s) => __els[s] || null,
                  addEventListener: () => {}};
const $ = (s) => document.querySelector(s);
"""
    return stub + body


def _extract_sse_onmessage_js(src: str) -> str:
    """抽出 `source.onmessage = event => {...}` 里的箭头函数体。

    抽出来的代码会被 node **真的执行**（配一个假的 `source` 对象），
    从而回答「它到底怎么写 comfyui_ok」这个问题 —— 而不是靠字符串匹配。
    """
    m = re.search(r"source\.onmessage\s*=\s*event\s*=>\s*\{", src)
    if not m:
        raise AssertionError("app.js 里找不到 source.onmessage 处理器")
    i = m.end() - 1
    depth = 0
    j = i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return src[m.start():j + 1]


def strip_js_comments(src: str) -> str:
    """去掉 JS 注释，只留**代码**。

    【为什么必须去注释】本文件的多条断言是「某段代码里**不得**出现
    某个模式」。而修复本身要在注释里**解释**那个模式（「老代码写死
    {comfyui_ok: true}」）—— 不去注释的话，断言会被修复说明自己
    绊倒（这是典型的「断言自指」陷阱）。
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    out = []
    for line in src.split("\n"):
        # 去掉行尾注释（注意不要动字符串里的 // —— 本项目无此用法）
        i = line.find("//")
        if i >= 0:
            line = line[:i]
        out.append(line)
    return "\n".join(out)


# =====================================================================
# 1) 共享客户端：缓存 / 单飞 / 退避
# =====================================================================

class _FakeResp:
    def __init__(self, status=200, payload=None, body=b""):
        self.status = status
        self._payload = payload
        self._body = body

    async def json(self, content_type=None):
        # 【关键】先让出控制权 —— 否则 9 个「并发」协程其实串行，
        # 第一个就把结果写进缓存，single-flight 的断言变成空转。
        await asyncio.sleep(0)
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    async def read(self):
        return self._body

    async def text(self):
        return self._body.decode()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _YieldCtx:
    """进入时 await 一次 asyncio.sleep(0) —— 让出控制权给其它协程。"""

    async def __aenter__(self):
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    """记录每次 get 的次数，用来数「上游到底被打了几次」。"""

    def __init__(self, responses=None, exc=None):
        self.calls = []          # 每次 get 的 path
        self.responses = responses or []
        self.exc = exc
        self.closed = False      # comfy_client.session() 会检查这个

    def get(self, path, **kw):
        self.calls.append(path)
        if self.exc is not None:
            raise self.exc
        if self.responses:
            return self.responses.pop(0)
        return _FakeResp(payload={"ok": True})

    def _yield_ctx(self):
        # 【关键】必须**真的**让出控制权，否则「并发」其实��串行 ——
        # 第一次调用会在其它协程开始前就把缓存写好，single-flight
        # 的断言就变成空转（变异测试 M02 暴露过这个坑）。
        return _YieldCtx()

    async def close(self):
        pass


class TestSharedComfyClient(unittest.TestCase):
    """共享客户端的缓存 / 单飞 / 退避语义。"""

    def setUp(self):
        comfy_client._CACHE.clear()
        comfy_client._BACKOFF.clear()
        comfy_client._INFLIGHT.clear()
        comfy_client._SESSION = None

    def tearDown(self):
        comfy_client._CACHE.clear()
        comfy_client._BACKOFF.clear()
        comfy_client._INFLIGHT.clear()
        comfy_client._SESSION = None

    # -- 缓存 -------------------------------------------------------
    def test_second_call_hits_cache_and_does_not_hit_upstream(self):
        """缓存命中时**不再打上游** —— 这是「降频」的核心断言。"""
        fake = _FakeSession(responses=[_FakeResp(payload={"n": 1})])
        comfy_client._SESSION = fake

        async def run():
            a = await comfy_client.get_json("/queue", cache_ttl=60, cache_key="q")
            b = await comfy_client.get_json("/queue", cache_ttl=60, cache_key="q")
            return a, b

        a, b = asyncio.run(run())
        self.assertEqual(a, {"n": 1})
        self.assertEqual(b, {"n": 1})
        self.assertEqual(len(fake.calls), 1,
                         "第二次调用必须命中缓存，不得再打上游")

    def test_cache_expires_and_refetches(self):
        """TTL 到了就该重新取 —— 否则就成了「永远不更新」。"""
        fake = _FakeSession(responses=[_FakeResp(payload={"n": 1}),
                                       _FakeResp(payload={"n": 2})])
        comfy_client._SESSION = fake

        async def run():
            await comfy_client.get_json("/queue", cache_ttl=60, cache_key="q")
            # 手动把缓存时间戳推回到过去，模拟 TTL 过期。
            val, at = comfy_client._CACHE["q"]
            comfy_client._CACHE["q"] = (val, at - 999)
            return await comfy_client.get_json("/queue", cache_ttl=60, cache_key="q")

        second = asyncio.run(run())
        self.assertEqual(second, {"n": 2})
        self.assertEqual(len(fake.calls), 2)

    # -- 单飞 --------------------------------------------------------
    def test_concurrent_calls_are_merged_into_one_upstream_request(self):
        """N 个并发调用只打 1 次上游（这正是 9 条 SSE 的解药）。"""
        fake = _FakeSession(responses=[_FakeResp(payload={"n": 7})])
        comfy_client._SESSION = fake

        async def run():
            return await asyncio.gather(*[
                comfy_client.get_json("/queue", cache_ttl=60, cache_key="q")
                for _ in range(9)
            ])

        results = asyncio.run(run())
        self.assertTrue(all(r == {"n": 7} for r in results))
        self.assertEqual(len(fake.calls), 1,
                         "9 个并发调用只允许打 1 次上游（single-flight）")

    # -- 退避 --------------------------------------------------------
    def test_failure_does_not_retry_immediately(self):
        """失败后**不立刻重试** —— 这是放大环的另一半。"""
        fake = _FakeSession(exc=RuntimeError("boom"))
        comfy_client._SESSION = fake

        async def run():
            a = await comfy_client.get_json("/queue", cache_ttl=60, cache_key="q")
            b = await comfy_client.get_json("/queue", cache_ttl=60, cache_key="q")
            return a, b

        a, b = asyncio.run(run())
        self.assertIsNone(a, "上游失败必须返回 None（不是空列表）")
        self.assertIsNone(b)
        self.assertEqual(len(fake.calls), 1,
                         "失败后第二次调用必须被退避挡住，不得再打上游")

    def test_failure_returns_none_not_empty_list(self):
        """「读不到」必须与「读到但为空」区分开 —— 否则会误判任务丢失。"""
        fake = _FakeSession(responses=[_FakeResp(status=500)])
        comfy_client._SESSION = fake

        async def run():
            return await comfy_client.get_json("/queue", cache_ttl=60, cache_key="q")

        self.assertIsNone(asyncio.run(run()))

    def test_backoff_expires_and_allows_retry_again(self):
        """退避是**有时间窗**的 —— 到期后必须能重试（否则永久卡死）。"""
        fake = _FakeSession(exc=RuntimeError("boom"))
        comfy_client._SESSION = fake

        async def run():
            await comfy_client.get_json("/queue", cache_ttl=60, cache_key="q")
            # 把退避窗口推到过去，模拟退避到期。
            # （backoff_key 默认取 cache_key，所以键是 "q"。）
            until, fails = comfy_client._BACKOFF["q"]
            comfy_client._BACKOFF["q"] = (until - 999, fails)
            return await comfy_client.get_json("/queue", cache_ttl=60, cache_key="q")

        asyncio.run(run())
        self.assertEqual(len(fake.calls), 2, "退避到期后必须允许再次尝试")


# =====================================================================
# 2) 服务端：顶栏状态迟滞（comfyui_ok 不会因单次失败就翻转）
# =====================================================================

class TestComfyHealthHysteresis(unittest.TestCase):
    """一次探测失败**不得**立刻把界面判成「ComfyUI 未连接」。"""

    @classmethod
    def setUpClass(cls):
        import server as srv
        cls.srv = srv

    def setUp(self):
        self.srv._COMFY_HEALTH.update({"streak": 0, "ok": True, "last_probe": 0.0})

    def test_single_failure_still_reports_connected(self):
        """1 次失败 -> 仍然认为连着（迟滞生效）。"""
        self.assertTrue(self.srv._comfy_health_gate(False))
        self.assertTrue(self.srv._comfy_health_gate(False))

    def test_three_consecutive_failures_report_disconnected(self):
        """连续 3 次失败 -> 才判为未连接。"""
        results = [self.srv._comfy_health_gate(False) for _ in range(3)]
        self.assertEqual(results[-1], False,
                         "连续 3 次失败后必须判为未连接")
        self.assertTrue(results[0] and results[1],
                        "前两次失败不得翻转状态（否则就是用户看到的闪烁）")

    def test_single_success_recovers_immediately(self):
        """一次成功即恢复 —— 不能让界面一直挂着「未连接」。"""
        for _ in range(3):
            self.srv._comfy_health_gate(False)
        self.assertFalse(self.srv._comfy_health_gate(False))
        self.assertTrue(self.srv._comfy_health_gate(True),
                        "一次成功即应恢复")

    def test_threshold_is_at_least_three(self):
        """迟滞阈值不得小于 3 —— 否则退化成「一次失败即翻转」。"""
        self.assertGreaterEqual(self.srv.COMFY_HEALTH_FAIL_THRESHOLD, 3)


# =====================================================================
# 3) 服务端：SSE 共享快照（连接数与上游请求数**解耦**）
# =====================================================================

class TestStatusStreamSharedSnapshot(unittest.TestCase):
    """SSE 连接数增加时，上游请求数**不得**线性增加。"""

    @classmethod
    def setUpClass(cls):
        import server as srv
        cls.srv = srv

    def test_snapshot_is_shared_not_per_connection(self):
        """SSE 必须走**共享**快照函数，而不是每连接各自取数。"""
        src = inspect.getsource(self.srv.api_status_stream)
        # 只看**代码行**（去掉 docstring）—— docstring 里会解释老实现。
        code = "\n".join(l for l in src.split("\n")
                         if not l.strip().startswith("#"))
        code = code.split('"""')[0] + ("".join(code.split('"""')[2:])
                                      if '"""' in code else "")
        self.assertIn("_status_stream_snapshot()", code,
                      "SSE 必须使用共享快照函数")
        self.assertNotIn("status_snapshot(s)", code,
                         "SSE 里不得再每连接各自调 status_snapshot(session)")

    def test_shared_snapshot_has_ttl(self):
        """共享快照必须有 TTL —— 否则 9 条连接仍会打 9 次。"""
        self.assertGreater(self.srv._STATUS_STREAM_TTL, 0)


# =====================================================================
# 4) 服务端：队列读取收敛（fetch_comfy_queue / ids 共用一份缓存）
# =====================================================================

class TestQueueFetchConverged(unittest.TestCase):
    """`/queue` 的读取必须收敛到**一个**缓存 key。"""

    @classmethod
    def setUpClass(cls):
        import server as srv
        cls.srv = srv

    def setUp(self):
        comfy_client._CACHE.clear()
        comfy_client._BACKOFF.clear()
        comfy_client._INFLIGHT.clear()
        comfy_client._SESSION = None

    def tearDown(self):
        comfy_client._CACHE.clear()
        comfy_client._BACKOFF.clear()
        comfy_client._INFLIGHT.clear()
        comfy_client._SESSION = None

    def test_fetch_comfy_queue_uses_shared_cache(self):
        """**行为**断言：服务端取两次队列 -> 上游只被打 1 次。

        【为什么要有这条】变异测试 M08 把 `fetch_comfy_queue` 里的
        `cache_ttl=comfy_client.QUEUE_TTL` 改成 0（缓存失效），
        但客户端层的测试看不见（它测的是客户端自己的缓存）。
        这条从**服务端函数**这一层验证缓存真的生效。
        """
        fake = _FakeSession(responses=[
            _FakeResp(payload={"queue_running": [], "queue_pending": []}),
        ])
        comfy_client._SESSION = fake

        async def run():
            a = await self.srv.fetch_comfy_queue()
            b = await self.srv.fetch_comfy_queue()
            return a, b

        a, b = asyncio.run(run())
        self.assertEqual(a, {"running": 0, "pending": 0})
        self.assertEqual(b, {"running": 0, "pending": 0})
        self.assertEqual(len(fake.calls), 1,
                         "两次取队列必须只打上游 1 次（服务端缓存生效）")

    def test_fetch_comfy_queue_ids_uses_shared_cache(self):
        """同上：prompt_id 集合的读取也必须走缓存。"""
        fake = _FakeSession(responses=[
            _FakeResp(payload={"queue_running": [], "queue_pending": []}),
        ])
        comfy_client._SESSION = fake

        async def run():
            a = await self.srv.fetch_comfy_queue_ids()
            b = await self.srv.fetch_comfy_queue_ids()
            return a, b

        a, b = asyncio.run(run())
        self.assertEqual(a, set())
        self.assertEqual(b, set())
        self.assertEqual(len(fake.calls), 1,
                         "两次取队列 prompt_id 必须只打上游 1 次")

    def test_both_queue_readers_use_same_cache_key(self):
        src_count = inspect.getsource(self.srv.fetch_comfy_queue)
        src_ids = inspect.getsource(self.srv.fetch_comfy_queue_ids)
        self.assertIn('cache_key="queue"', src_count)
        self.assertIn('cache_key="queue"', src_ids)
        self.assertIn('backoff_key="queue"', src_count)
        self.assertIn('backoff_key="queue"', src_ids)

    def test_queue_readers_share_one_upstream_call(self):
        """两个队列读取器 + 9 个并发调用 -> 上游只应被打 1 次。"""
        fake = _FakeSession(responses=[_FakeResp(
            payload={"queue_running": [], "queue_pending": []})])
        comfy_client._SESSION = fake

        async def run():
            await comfy_client.get_json("/queue", timeout=5.0,
                                        cache_ttl=comfy_client.QUEUE_TTL,
                                        cache_key="queue", backoff_key="queue")
            await comfy_client.get_json("/queue", timeout=5.0,
                                        cache_ttl=comfy_client.QUEUE_TTL,
                                        cache_key="queue", backoff_key="queue")
            return await asyncio.gather(*[
                comfy_client.get_json("/queue", timeout=5.0,
                                      cache_ttl=comfy_client.QUEUE_TTL,
                                      cache_key="queue", backoff_key="queue")
                for _ in range(9)
            ])

        asyncio.run(run())
        self.assertEqual(len(fake.calls), 1,
                         "队列读取必须收敛到 1 次上游请求（缓存 + 单飞）")

    def test_queue_readers_no_longer_open_raw_sessions(self):
        """不得再 `async with session.get(f"{COMFY}/queue")` —— 那会绕过缓存。"""
        for fn in (self.srv.fetch_comfy_queue, self.srv.fetch_comfy_queue_ids):
            src = inspect.getsource(fn)
            self.assertNotIn("session.get(", src,
                             "队列读取不得再直接用裸 session（会绕过缓存与退避）")


# =====================================================================
# 5) yue2_schema：失败也要写缓存（杜绝「失败即重试」放大环）
# =====================================================================

class TestYue2EnumCacheOnFailure(unittest.TestCase):
    """全量 /object_info 是 37.5 MB —— 失败后不得立刻重拉。"""

    def test_failure_updates_cache_timestamp(self):
        """失败分支必须更新 `_cache["at"]` —— 否则下次立刻重试。"""
        from h3studio import yue2_schema as ys

        calls = {"n": 0}

        def boom(timeout=10.0):
            calls["n"] += 1
            raise RuntimeError("comfy busy")

        orig = ys._fetch_enums
        ys._fetch_enums = boom
        ys._cache.update({"at": 0.0, "data": dict(ys.STATIC_ENUMS),
                          "ttl": ys.OBJECT_INFO_TTL})
        try:
            ys.get_enums()
            first = calls["n"]
            ys.get_enums()
            second = calls["n"]
        finally:
            ys._fetch_enums = orig
        self.assertEqual(first, 1)
        self.assertEqual(second, 1,
                         "失败后第二次调用不得再拉（必须命中失败缓存）")

    def test_failure_cache_ttl_is_short(self):
        """失败缓存的 TTL 必须比成功 TTL 短 —— 否则恢复太慢。"""
        from h3studio import yue2_schema as ys
        self.assertLess(ys.OBJECT_INFO_FAIL_TTL, ys.OBJECT_INFO_TTL)
        self.assertGreater(ys.OBJECT_INFO_FAIL_TTL, 0)

    def test_fetch_enums_prefers_per_node_query(self):
        """优先按节点名问（1.2 KB）而不是全量（37.5 MB）。"""
        from h3studio import yue2_schema as ys
        src = inspect.getsource(ys._fetch_enums)
        self.assertIn("_fetch_node_input", src,
                      "必须优先按节点名问 /object_info/<Node>")
        # 全量路径只能作为**回落**存在（不能是唯一路径）。
        self.assertIn("COMFY_OBJECT_INFO_URL", src,
                      "全量路径必须保留为回落（老版本兼容）")

    def test_fetch_enums_actually_uses_per_node_path(self):
        """**行为**断言：按节点名问可用时，绝不能去拉全量。

        【为什么要行为断言】只断言源码里出现了 `_fetch_node_input`
        是**不够**的 —— 变异测试 M10 把按节点名问的路径直接 raise 掉
        （即「永远走全量」）后，源码断言仍然全绿。
        这里用**假 HTTP**把两条路都计数，断言「全量一次都没被打」。
        """
        import io
        import urllib.request as _urlreq
        from h3studio import yue2_schema as ys

        asked = []

        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return json.dumps(self._payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(url, timeout=None):
            asked.append(url)
            if url.endswith("/YuE2StylePrompt"):
                payload = {"YuE2StylePrompt": {"input": {"required": {
                    "language": [["English"], {}],
                    "genre": [["pop"], {}],
                    "mood": [["none"], {}],
                    "vocal": [["v"], {}],
                    "vocal_color": [["c"], {}],
                    "energy": [["e"], {}],
                    "length_hint": [["h"], {}],
                }}}}
            elif url.endswith("/YuE2LyricsTemplate"):
                payload = {"YuE2LyricsTemplate": {"input": {"required": {
                    "template": [["blank"], {}],
                }}}}
            else:
                raise AssertionError("不该走到全量 /object_info: %s" % url)
            return _Resp(payload)

        orig = _urlreq.urlopen
        _urlreq.urlopen = fake_urlopen
        try:
            out = ys._fetch_enums(timeout=2)
        finally:
            _urlreq.urlopen = orig

        self.assertEqual(out["genre"], ["pop"])
        self.assertTrue(all("/YuE2" in u for u in asked),
                        "必须只问按节点名的接口，不得拉全量 /object_info")
        self.assertFalse(any(u.endswith("/object_info") for u in asked),
                         "全量 /object_info 不得被调用（37.5 MB）")

    def test_per_node_query_helper_exists(self):
        from h3studio import yue2_schema as ys
        self.assertTrue(callable(ys._fetch_node_input))


# =====================================================================
# 6) 前端：不再注入 comfyui_ok: true（抖动的另一半根因）
# =====================================================================

class TestFrontendStatusAuthority(unittest.TestCase):
    """前端必须用服务端的权威判定，不得自己臆造。"""

    @classmethod
    def setUpClass(cls):
        raw = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        cls.app_js_raw = raw
        # 断言只针对**代码**（去掉注释）—— 否则修复说明本身会绊倒断言。
        cls.app_js = strip_js_comments(raw)
        cls.poll_js = strip_js_comments(
            (ROOT / "web" / "poll-scheduler.js").read_text(encoding="utf-8"))
        cls.index_html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    def test_sse_handler_behaviour_does_not_invent_comfyui_ok(self):
        """**行为**断言：用 node 真跑一遍 SSE onmessage 处理器。

        【为什么要行为断言】变异测试 M11 把处理器改回
        `data.comfyui_ok = true` 后，纯源码字符串断言没能抓住 ——
        因为「源码里没有 comfyui_ok: true」这个断言被注释里的
        解释文字干扰。这里直接**执行**代码，看它到底怎么写状态。

        场景：服务端下发的帧**没有** comfyui_ok 字段（老服务端），
        且此前没有任何判定 -> 处理器**不得**凭空造出「已连接」。
        """
        import subprocess
        import tempfile

        deps = _extract_sse_handler_js(self.app_js_raw)
        handler = _extract_sse_onmessage_js(self.app_js_raw)
        script = """
const source = {onmessage: null};
%s
%s
const data = {queued: 0, running: null, comfy_running: null, comfy_pending: null};
const ev = {data: JSON.stringify(data)};
source.onmessage(ev);
console.log(JSON.stringify({
  comfyui_ok: (statusState.applied || {}).comfyui_ok === undefined
  ? "UNDEFINED" : (statusState.applied || {}).comfyui_ok,
  }));
  """ % (deps, handler)
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
            f.write(script)
            path = f.name
        proc = subprocess.run(["node", path], capture_output=True, text=True,
                              timeout=30)
        out = proc.stdout.strip().splitlines()
        self.assertTrue(out, "node 脚本必须有输出: %s %s" % (proc.stdout, proc.stderr))
        result = json.loads(out[-1])
        self.assertEqual(result["comfyui_ok"], "UNDEFINED",
                         "SSE 帧没有 comfyui_ok 时，处理器不得臆造「已连接」")

    def test_sse_handler_does_not_force_comfyui_ok_true(self):
        """SSE 里不得再写死 {comfyui_ok: true} —— 那正是抖动的来源。"""
        m = re.search(r"source\.onmessage\s*=\s*event\s*=>\s*\{(.*?)\n  \};",
                      self.app_js, re.S)
        self.assertIsNotNone(m, "必须能找到 SSE onmessage 处理器")
        body = m.group(1)
        self.assertNotIn("comfyui_ok: true", body,
                         "SSE 处理器不得再注入 comfyui_ok: true")
        self.assertNotIn("{comfyui_ok: true}", body)
        # 反向断言：必须**使用**服务端给的权威值。
        self.assertIn("data.comfyui_ok", body,
                      "SSE 必须使用服务端下发的 comfyui_ok")

    def test_all_status_writes_go_through_single_entry(self):
        """状态更新必须走单一入口 `applyStatusSnapshot`。"""
        self.assertIn("function applyStatusSnapshot(", self.app_js)
        # renderStatus 只能被 applyStatusSnapshot 调用（外加自身定义 1 次）。
        callers = re.findall(r"renderStatus\(", self.app_js)
        self.assertEqual(len(callers), 2,
                         "renderStatus 只能有一个调用点（applyStatusSnapshot），"
                         "否则就又出现「多个源互相覆盖顶栏」")

    def test_poll_scheduler_exists_and_is_loaded_early(self):
        """调度器必须存在，且在 generation-queue.js 之前加载。"""
        self.assertTrue((ROOT / "web" / "poll-scheduler.js").exists())
        idx = self.index_html.index("/static/poll-scheduler.js")
        self.assertLess(idx, self.index_html.index("/static/generation-queue.js"),
                        "poll-scheduler.js 必须早于 generation-queue.js 加载")
        self.assertLess(idx, self.index_html.index("/static/app.js"),
                        "poll-scheduler.js 必须早于 app.js 加载")

    def test_poll_scheduler_has_backoff_and_jitter_and_visibility(self):
        """调度器必须含：退避、抖动、页面不可见暂停。"""
        self.assertIn("Math.pow(2, failures)", self.poll_js,
                      "必须有指数退避")
        self.assertIn("jitter", self.poll_js, "必须有抖动")
        self.assertIn("document.hidden", self.poll_js,
                      "必须在页面不可见时暂停轮询")

    def test_poll_scheduler_behaviour_jitter_backoff_visibility(self):
        """**行为**断言：用 node 真跑调度器，量它的三个性质。

        【为什么要行为断言】变异测试 M13/M15 把抖动 / 可见性暂停
        抹掉后，纯源码字符串断言没能抓住。这里直接跑起来量：

          1. **抖动**：连续 3 轮的间隔**必须各不相同**（相位被打散）；
          2. **退避**：连续失败后间隔**必须变长**；
          3. **可见性**：页面 hidden 时**必须不调** fn。
        """
        import subprocess
        import tempfile

        js = (ROOT / "web" / "poll-scheduler.js").read_text(encoding="utf-8")
        script = """
// --- 假定时器：记录每次 setTimeout 的延迟 ---
const __timers = [];
function setTimeout(fn, delay) {
  __timers.push({fn: fn, delay: delay});
  return __timers.length;
}
function clearTimeout(id) { if (__timers[id-1]) __timers[id-1].cancelled = true; }
let __hidden = false;
const document = {
  get hidden() { return __hidden; },
  addEventListener: () => {},
  removeEventListener: () => {},
};
globalThis.setTimeout = setTimeout;
globalThis.clearTimeout = clearTimeout;
globalThis.document = document;
globalThis.window = globalThis;

%s

const __calls = [];
let __fail = false;   // 成功路径 —— base 恒为 intervalMs，间隔差异
                       // 只可能来自抖动（退避会改 base，混一起就测不出）。
const h = window.H3Poll("t", async () => { __calls.push(1); if (__fail) throw new Error("x"); }, 1000);

// 手动驱动 3 轮。
// 【关键】每轮之后必须 await 让异步 tick **跑完** —— 否则下一次
// 排程还没发生，读到的是同一���旧定时器（这正是第一次写这测试时
// 「三轮间隔全相同」的假阴性来源）。
const flush = () => new Promise(r => setImmediate(r));
async function drive(n) {
  const delays = [];
  for (let i = 0; i < n; i++) {
    const t = __timers[__timers.length - 1];
    delays.push(t.delay);
    t.fn();
    await flush();
    await flush();
  }
  return delays;
}
drive(9).then(d1 => {
  // 【关键】只统计**由 tick 排的**那些延迟（第 2 个起）。
  // 第 1 个是注册时排的（总是带抖动），把它算进来会掩盖
  // 「tick 排程没有抖动」这个缺陷（变异 M13 就是这么活下来的）。
  console.log(JSON.stringify({delays: d1, tickDelays: d1.slice(1),
                               calls: __calls.length}));
});
""" % js
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
            f.write(script)
            path = f.name
        proc = subprocess.run(["node", path], capture_output=True, text=True,
                              timeout=30)
        self.assertTrue(proc.stdout.strip(),
                        "node 必须有输出: %s %s" % (proc.stdout, proc.stderr))
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        tick_delays = data["tickDelays"]
        self.assertGreaterEqual(len(tick_delays), 8, "必须量到足够多的间隔")
        # 1) 抖动：tick 排的间隔必须有**多个不同的值**（相位被打散）。
        #    没有抖动时它们会**全部相等**（= base）。
        self.assertGreaterEqual(len(set(tick_delays)), 3,
                                 "tick 排的间隔必须带抖动"
                                 "（多标签页相位相同 = 风暴）")
        self.assertNotEqual(len(set(tick_delays)), 1,
                            "tick 排的间隔不得完全相同（抖动被抹掉了）")
        # 2) 抖动幅度受限（±30% 容忍）：不能大到乱，也不能小到没有。
        base = 1000
        for d in tick_delays:
            self.assertGreaterEqual(d, base * 0.7 - 1,
                                    "抖动不得小于 -30%（间隔会退化）")
            self.assertLessEqual(d, base * 1.3 + 1,
                                 "抖动不得大于 +30%（间隔会失控）")

    def test_poll_scheduler_behaviour_backoff_on_failure(self):
        """连续失败后间隔**必须变长**（行为断言，与抖动测试分开）。"""
        import subprocess
        import tempfile

        js = (ROOT / "web" / "poll-scheduler.js").read_text(encoding="utf-8")
        script = """
const __timers = [];
function setTimeout(fn, delay) { __timers.push({fn: fn, delay: delay}); return __timers.length; }
function clearTimeout(id) { if (__timers[id-1]) __timers[id-1].cancelled = true; }
let __hidden = false;
const document = {get hidden() { return __hidden; }, addEventListener: () => {}, removeEventListener: () => {}};
globalThis.setTimeout = setTimeout; globalThis.clearTimeout = clearTimeout;
globalThis.document = document; globalThis.window = globalThis;
%s
const flush = () => new Promise(r => setImmediate(r));
async function drive(n) {
  const delays = [];
  for (let i = 0; i < n; i++) {
    const t = __timers[__timers.length - 1];
    delays.push(t.delay);
    t.fn();
    await flush(); await flush();
  }
  return delays;
}
let __fail = true;
window.H3Poll("t", async () => { if (__fail) throw new Error("x"); }, 1000);
drive(4).then(d => console.log(JSON.stringify({delays: d})));
""" % js
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
            f.write(script)
            path = f.name
        proc = subprocess.run(["node", path], capture_output=True, text=True,
                              timeout=30)
        self.assertTrue(proc.stdout.strip(),
                        "node 必须有输出: %s %s" % (proc.stdout, proc.stderr))
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        delays = data["delays"]
        self.assertEqual(len(delays), 4)
        # 失败后间隔必须单调变长（2x -> 4x -> 8x，抖动 ±20%）。
        for i in range(2, len(delays)):
            self.assertGreater(delays[i], delays[i - 1],
                               "连续失败后间隔必须变长（指数退避）")

    def test_poll_scheduler_behaviour_skips_when_hidden(self):
        """页面不可见时**不得**调用轮询函数（行为断言）。"""
        import subprocess
        import tempfile

        js = (ROOT / "web" / "poll-scheduler.js").read_text(encoding="utf-8")
        script = """
const __timers = [];
function setTimeout(fn, delay) { __timers.push({fn: fn, delay: delay}); return __timers.length; }
function clearTimeout(id) { if (__timers[id-1]) __timers[id-1].cancelled = true; }
let __hidden = true;
const document = {get hidden() { return __hidden; }, addEventListener: () => {}, removeEventListener: () => {}};
globalThis.setTimeout = setTimeout; globalThis.clearTimeout = clearTimeout;
globalThis.document = document; globalThis.window = globalThis;
%s
let __calls = 0;
window.H3Poll("t", async () => { __calls++; }, 1000);
const t = __timers[__timers.length - 1];
t.fn();
console.log(JSON.stringify({calls: __calls}));
""" % js
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
            f.write(script)
            path = f.name
        proc = subprocess.run(["node", path], capture_output=True, text=True,
                              timeout=30)
        self.assertTrue(proc.stdout.strip(),
                        "node 必须有输出: %s %s" % (proc.stdout, proc.stderr))
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(data["calls"], 0,
                         "页面不可见时不得执行轮询函数")

    def test_bare_setinterval_removed_from_status_pollers(self):
        """状态/任务/队列的轮询不得再用裸 setInterval。"""
        # app.js 里不再有 setInterval(refreshSystem, 3000) 这类写法
        self.assertNotIn("setInterval(refreshSystem", self.app_js)
        self.assertNotIn("setInterval(() => scheduleRefresh(\"jobs\"", self.app_js)
        gq = strip_js_comments(
            (ROOT / "web" / "generation-queue.js").read_text(encoding="utf-8"))
        # 队列轮询的**主路径**必须走带抖动的调度器。
        self.assertIn('window.H3Poll("generation-queue", refresh, 1000)', gq,
                      "队列轮询必须走 window.H3Poll（抖动 + 退避 + 可见性暂停）")


# =====================================================================
# 7) 会话复用：不再每次新建 ClientSession
# =====================================================================

class TestSessionReuse(unittest.TestCase):
    """热点路径必须复用会话。"""

    @classmethod
    def setUpClass(cls):
        import server as srv
        cls.srv = srv

    def test_api_system_does_not_create_session_per_call(self):
        """/api/system 每次调用都新建 session = 每 3 秒一条新 TCP 连接。"""
        src = inspect.getsource(self.srv.api_system)
        self.assertNotIn("ClientSession(", src,
                         "/api/system 不得再每次新建 ClientSession")
        self.assertIn("comfy_client.get_json", src,
                      "/api/system 必须走共享客户端")

    def test_status_stream_does_not_create_session_per_call(self):
        src = inspect.getsource(self.srv.api_status_stream)
        self.assertNotIn("ClientSession(", src,
                         "SSE 不得再每连接新建 ClientSession")

    def test_node_probe_uses_shared_session(self):
        """节点探测也走共享会话（实测：每次新建 session 会卡满 3 秒）。"""
        src = inspect.getsource(self.srv.refresh_comfy_node_names)
        self.assertIn("comfy_client.session()", src,
                      "节点探测必须复用共享会话")

    def test_shared_client_module_exposes_session(self):
        self.assertTrue(callable(comfy_client.session))
        self.assertTrue(callable(comfy_client.close_session))


if __name__ == "__main__":
    unittest.main()
