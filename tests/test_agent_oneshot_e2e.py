# -*- coding: utf-8 -*-
"""
**一次性（非 SSE）JSON 路径的工具卡「结果」区** —— 真实 HTTP 端到端。

【为什么要单独再写一套 —— 2026-11-10 第二轮独立复验】
第一轮修复（commit 375895c）补了后端 steps[].summary 与前端
syncToolsFromSteps 的结果消费，配套的契约测试也补了 8 条。

但复验发现**既有契约测试有一个取证弱点**：
`tests/test_agent_contract.py::TestOneShotToolResultContract`
的前端断言走的是 `window.fetch = async (...) => JSON.stringify(result)`
—— 也就是**把 fetch 换成桩**，返回后端真实产出的 JSON。

这满足了「数据来自真实后端」，但**没有验证真实网络路径**：
  * 前端会不会真的发出那个请求？
  * 服务端在真实请求下**真的会**返回一次性 JSON（而不是 SSE）？
  * 前端的降级链（sendStreamNew -> sendStreamLegacy -> apiJSON）
    在真实响应下走的是哪一段？
  * 响应体 Content-Type 真的是 application/json 吗？

也就是说：桩掉的 fetch 让「后端产出」是真的，但「前后端在真实
HTTP 上的对接」**没被测到**。本文件补上这一段。

【取证纪律 —— 不可退让】
1. **真起 server.make_app()**（生产路由），真起 HTTP 端口；
2. **真 Chromium** 加载真实的 web/agent.js；
3. 页面用**真实 fetch**（不桩）打到**真实服务端**；
4. 断言打在**真实渲染出的 DOM** 上；
5. 绝不用「前端自造 mock 断言自己」。

【为什么要把浏览器放进子线程 —— 2026-11-10】
Playwright 的 **sync API** 会拒绝在「已有 running asyncio loop」的
线程里启动。本文件的服务端跑在自己的事件循环里（子线程），
而 HTTP 请求由主线程发起 —— 这条链路会让主线程残留一个
running loop（同线程内 asyncio.run / get_event_loop 的副作用）。

实测证据：跑完「真实 HTTP 请求」那条用例后，主线程
`asyncio.get_running_loop()` 返回一个 running=True 的 loop，
于是紧接着的前端用例全部被 Playwright 拒绝。

所以：**浏览器整体放进独立子线程**，主线程只通过队列收发指令。
这样无论主线程的 asyncio 状态如何，浏览器侧都不受影响 ——
这条路径上的取证能力反而更强（页面与 server 真的跨进程跨线程通信）。

运行：python3 -m pytest tests/test_agent_oneshot_e2e.py -v
"""
import asyncio
import json
import queue
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import agent as agent_mod          # noqa: E402
from h3studio import agent_sessions              # noqa: E402


def _free_port() -> int:
    """拿一个空闲端口（避免与 8190 线上服务冲突）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


#: 真的 server.prepare_job_params（在替换 server 模块属性之前抓住它）。
#: 必须**先抓住再替换** —— 替换后 server.prepare_job_params 就是 fake 的，
#: fake 再 import server 调它 -> 无限递归。
_REAL_PREPARE_JOB_PARAMS = None


def _fake_server(tmp: Path):
    """
    最小服务端替身（与 test_agent_contract.py 同款）。

    【为什么必须带完整的 jobs / model_providers 表】
    server.py 的路由会查 jobs 表（状态快照）和 model_providers
    （默认 provider），表不存在或记录缺失都会让请求 500。
    这里照搬 test_agent_contract.py 的实现 —— 不重造轮子，
    免得「测试替身」和「被测代码」各写一套假设。
    """
    import sqlite3

    class FakeServer:
        def __init__(self):
            self.db_path = tmp / "jobs.db"
            self.created = []
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    created_at REAL,
                    mode TEXT,
                    params TEXT,
                    status TEXT,
                    prompt_id TEXT,
                    progress REAL DEFAULT 0,
                    progress_text TEXT,
                    error TEXT,
                    outputs TEXT,
                    started_at REAL,
                    finished_at REAL
                );
                CREATE TABLE IF NOT EXISTS job_batches (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    idx INTEGER NOT NULL,
                    seed INTEGER,
                    status TEXT,
                    prompt_id TEXT,
                    progress REAL DEFAULT 0,
                    progress_text TEXT,
                    error TEXT,
                    outputs TEXT,
                    created_at REAL,
                    started_at REAL,
                    finished_at REAL
                );
                CREATE TABLE IF NOT EXISTS model_providers (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
                    base_url TEXT NOT NULL, llm_base_url TEXT DEFAULT '',
                    use_proxy INTEGER DEFAULT 0, proxy_base_url TEXT DEFAULT '',
                    model TEXT NOT NULL, api_key TEXT DEFAULT '',
                    multimodal INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1,
                    created_at REAL);
                CREATE TABLE IF NOT EXISTS model_provider_prefs (
                    key TEXT PRIMARY KEY, value TEXT);
                """
            )
            conn.execute(
                "INSERT OR REPLACE INTO model_providers VALUES "
                "('testprov','测试模型','openai','http://127.0.0.1:5000','',0,'',"
                "'test-model','',1,1,0)")
            conn.execute("INSERT OR REPLACE INTO model_provider_prefs "
                         "VALUES ('default','testprov')")
            conn.commit()
            conn.close()

        def db(self):
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return conn

        def prepare_job_params(self, mode, params, asset_refs=None,
                               saved_files=None):
            # 注意：这里**不能**再 `import server` ——
            # server 模块此刻正在处理请求，再 import 会拿到同一个模块对象，
            # 而 prepare_job_params 内部又可能回调到这里 -> 无限递归。
            # 真实实现由 _ServerThread.start() 挂上去（见该文件）。
            return _REAL_PREPARE_JOB_PARAMS(mode, params, asset_refs,
                                           saved_files)

        def create_job_record(self, mode, params):
            job_id = f"job{len(self.created):04d}"
            self.created.append((job_id, mode, dict(params)))
            return job_id

    return FakeServer()


class _ServerThread:
    """
    在**独立线程 + 独立事件循环**里跑真实的 server.make_app()。

    【为什么事件循环必须在子线程里创建】
    连 `asyncio.new_event_loop()` 都不在主线程做 —— 否则主线程会
    残留一个「当前 loop」，Playwright sync API 会拒绝启动。
    """

    def __init__(self, port: int, fake_srv):
        self.port = port
        self.fake_srv = fake_srv
        self.loop = None
        self.runner = None
        self.thread = None
        self._stop = False
        self._ready = threading.Event()

    def start(self):
        global _REAL_PREPARE_JOB_PARAMS
        import importlib.util
        from aiohttp import web

        # 【关键：加载一份**私有**的 server 模块副本】
        # 仓库里其它测试（test_agent_contract.py 等）会
        # `sys.modules.pop("server")` 再重新 import —— 那会把
        # sys.modules["server"] 换成**另一个模块对象**，并替换它的
        # db / prepare_job_params / create_job_record。
        # 如果本测试用 sys.modules 里那个模块对象，就会被那些测试
        # 抢走（表现为：全量跑时 HTTP 请求 30 秒超时）。
        #
        # 所以这里用 spec_from_file_location 加载一份**独立副本**，
        # 以私有名字注册 —— 与 sys.modules["server"] 完全无关。
        spec = importlib.util.spec_from_file_location(
            "h3studio_e2e_server", REPO / "server.py")
        srv = importlib.util.module_from_spec(spec)
        sys.modules["h3studio_e2e_server"] = srv
        spec.loader.exec_module(srv)
        # 先抓住真的 prepare_job_params，再替换 —— 顺序反了会无限递归
        if _REAL_PREPARE_JOB_PARAMS is None:
            _REAL_PREPARE_JOB_PARAMS = srv.prepare_job_params
        # srv.db 要赋**方法**（server_module.db() 是被调用的），
        # 不是 fake server 对象 —— 赋错会得到
        # "TypeError: 'FakeServer' object is not callable"。
        srv.db = self.fake_srv.db
        srv.prepare_job_params = self.fake_srv.prepare_job_params
        srv.create_job_record = self.fake_srv.create_job_record
        app = srv.make_app()
        self.srv_module = srv

        def _runner():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.loop = loop
            try:
                loop.run_until_complete(self._boot(app))
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

        self.thread = threading.Thread(target=_runner, daemon=True)
        self.thread.start()
        self._ready.wait(timeout=20)
        # 等端口真的可用
        for _ in range(60):
            try:
                s = socket.create_connection(("127.0.0.1", self.port), 0.2)
                s.close()
                return
            except OSError:
                time.sleep(0.1)

    async def _boot(self, app):
        from aiohttp import web
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", self.port)
        await site.start()
        self.runner = runner
        self._ready.set()
        # keep-alive —— 事件循环必须一直在跑，否则连接建得上但不处理请求。
        # 停止方式：stop_event（线程安全）+ 在 loop 上挂一个 watcher，
        # 标志一变就立刻唤醒 sleep，让协程**自然结束**
        # （而不是强行 stop loop —— 那会抛
        #  "Event loop stopped before Future completed"）。
        stop_event = asyncio.Event()
        self._stop_event = stop_event
        while not self._stop:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=0.05)
            except asyncio.TimeoutError:
                continue
            break

    def stop(self):
        self._stop = True
        # 在 loop 上设置事件，唤醒 keep-alive 协程
        loop = self.loop
        ev = getattr(self, "_stop_event", None)
        if loop is not None and ev is not None:
            try:
                loop.call_soon_threadsafe(ev.set)
            except Exception:
                pass
        if self.thread is not None:
            self.thread.join(timeout=10)
        # 若仍未退出（异常卡住），再强行停 loop
        if self.thread is not None and self.thread.is_alive():
            if loop is not None:
                try:
                    loop.call_soon_threadsafe(loop.stop)
                except Exception:
                    pass
            self.thread.join(timeout=5)


class _BrowserThread:
    """
    在**独立线程**里跑 Playwright + 真 Chromium。

    主线程通过两个 queue 与它通信：
      * cmd_q  —— 送进浏览器线程的命令（callable）
      * res_q  —— 取回结果

    【为什么要单独一个线程】见模块 docstring ——
    Playwright sync API 拒绝在有 running asyncio loop 的线程启动。
    """

    def __init__(self, port: int):
        self.port = port
        self.cmd_q = queue.Queue()
        self.res_q = queue.Queue()
        self.thread = None
        self._stop = False
        self._ready = threading.Event()

    def start(self):
        def _runner():
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            browser = pw.chromium.launch(args=["--no-sandbox"])
            self._pw = pw
            self._browser = browser
            self._page = self._make_page(browser)
            self._ready.set()
            while not self._stop:
                try:
                    item = self.cmd_q.get(timeout=0.2)
                except queue.Empty:
                    continue
                if item is None:
                    break
                fn, kwargs = item
                try:
                    self.res_q.put(("ok", fn(**kwargs)))
                except Exception as exc:      # pragma: no cover - 诊断用
                    self.res_q.put(("err", exc))
            try:
                browser.close()
                pw.stop()
            except Exception:
                pass

        self.thread = threading.Thread(target=_runner, daemon=True)
        self.thread.start()
        self._ready.wait(timeout=30)

    def _make_page(self, browser):
        """
        开一个页面并注入真实前端脚本（必须在浏览器线程里做）。

        【为什么要复用同一个 context/page】
        全量 pytest 跑时 CPU 饱和，每条用例都新建 context+page
        会显著拖慢（实测 goto 在重负载下会超时）。
        所以这里**复用**一个 context，只在需要时换 page。

        【为什么 goto 要重试】重负载下首次 goto 可能超时
        （默认 30s）。这里显式加长超时 + 重试，
        否则整条 E2E 会在重负载环境下**随机**失败。
        """
        if getattr(self, "_context", None) is None:
            self._context = browser.new_context()
        page = self._context.new_page()
        # 真实 origin —— 页面里的 fetch("/api/agent/chat") 打到真服务端。
        # 静态资源挂在 /static/ 下（server.py: app.router.add_static("/static", BASE/"web")）。
        last_err = None
        for attempt in range(3):
            try:
                page.goto(f"http://127.0.0.1:{self.port}/",
                          wait_until="domcontentloaded", timeout=60000)
                last_err = None
                break
            except Exception as exc:
                last_err = exc
                time.sleep(1.0 + attempt)
        if last_err is not None:
            raise last_err
        # 真实前端脚本（用本地路径注入 —— /static/agent.js 是同一份文件）
        page.add_script_tag(path=str(REPO / "web" / "agent.js"))
        page.add_script_tag(path=str(REPO / "web" / "agent-media.js"))
        page.wait_for_timeout(400)
        # 真实页面里 Agent 页默认是 .page-hidden（要用户点导航才显示）。
        # 这里把它显示出来 —— 否则 Playwright 的 fill/click 会因为
        # 元素不可见而失败。
        page.evaluate("""() => {
          const p = document.getElementById('agentPage');
          if (p) p.classList.remove('page-hidden');
        }""")
        page.wait_for_timeout(200)
        return page

    def _new_page_raw(self):
        """在浏览器线程里开一个全新页面（状态彻底隔离）。"""
        old = self._page
        self._page = self._make_page(self._browser)
        try:
            if old is not None:
                old.close()
        except Exception:
            pass
        return True

    def call(self, fn, timeout=180, **kwargs):
        """
        在浏览器线程里执行 fn(**kwargs)，返回结果。

        【为什么默认 180 秒】页面操作本身可能耗到 90 秒
        （expect_response / wait_for_function 的超时），
        队列等待必须**比它更长**，否则主线程会先抛 Empty ——
        而浏览器线程还在跑，结果就丢了。
        """
        self.cmd_q.put((fn, kwargs))
        kind, value = self.res_q.get(timeout=timeout)
        if kind == "err":
            raise value
        return value

    def stop(self):
        self._stop = True
        try:
            self.cmd_q.put(None)
        except Exception:
            pass
        if self.thread is not None:
            self.thread.join(timeout=10)


class _E2EBase(unittest.TestCase):
    """
    真起 server + 真起 HTTP 端口 + 真 Chromium（浏览器在独立线程）。

    【为什么不用 aiohttp TestClient】
    TestClient 走的是**内存 transport**，没有真实 socket，
    也没有真实的 Content-Type 协商。前端要真的能用
    `fetch("/api/agent/chat")` 打到服务端，页面必须来自
    **同一个 http:// origin** —— 所以这里把真实 app 跑在
    一个真实端口上，页面用 page.goto() 打开它。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="h3agent-oneshot-e2e-"))
        cls.port = _free_port()
        cls.server = _fake_server(cls.tmp)
        agent_sessions.reset_store(cls.tmp / "agent_sessions.db")
        cls._srv_thread = _ServerThread(cls.port, cls.server)
        cls._srv_thread.start()
        cls._browser = None

    @classmethod
    def tearDownClass(cls):
        if cls._browser is not None:
            cls._browser.stop()
        if getattr(cls, "_srv_thread", None) is not None:
            cls._srv_thread.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ------------------------------------------------------------ 真浏览器
    def browser(self):
        """
        真 Chromium（**整个测试类共用一个**浏览器线程）。

        【为什么必须是类级共享】
        unittest **每个测试方法都新建一个实例** ——
        如果把浏览器存在实例属性上，每条用例都会新起一个 Chromium
        （实测：全量 pytest 跑时旧实例的浏览器线程不退出，
        资源耗尽 -> goto 超时 -> 随机失败）。
        所以这里存在**类属性**上，setUpClass 建一次，tearDownClass 关一次。
        """
        cls = type(self)
        if getattr(cls, "_browser", None) is None:
            browser = _BrowserThread(cls.port)
            browser.start()
            cls._browser = browser
        return cls._browser

    def _new_page(self):
        """开一个全新的页面（状态彻底隔离）。"""
        browser = self.browser()
        return browser.call(lambda: browser._new_page_raw())

    # ------------------------------------------------------------ 模型打桩
    def _patch_model(self, turns):
        """把**两条** LLM 调用链都打桩。

        【2026-11-16 S5 修 —— 补上第二条链】
        原来只打桩了 Agent 主循环用的
          ``agent_mod.llm_client.call_provider_with_tools``
        但 **create_job 工具在建任务前会走一次「提示词增强管线」**：
          ``h3studio/agent.py:1581`` -> ``agent_enhance.enhance_for_mode``
          -> ``agent_enhance._text_enhance`` -> ``h3studio/llm.py::call_provider``
        —— 那是**另一个模块**（``h3studio.llm``）、**另一个函数**
        （``call_provider`` 而不是 ``call_provider_with_tools``），
        原来没被打桩 -> 增强管线**真的去调本机 LLM**
        （替身里注册的 provider 指向 ``http://127.0.0.1:5000``）。

        实测证据（本轮现场插桩）：
          总耗时 17.06s，其中 ``call_provider`` = **10.50s**（占 62%）
          单独测同一 provider 的真实响应：10.5s ~ 27.9s（随本机负载波动）
        而用例的等待预算是 ``page.expect_response(timeout=90000)`` ——
        本机 LLM 忙一点 + 其它测试抢资源 -> 90s 被击穿 -> 偶发失败。

        这不是「预算不够」，是**测试隔离缺陷**：E2E 用例不该依赖
        本机 LLM 的**可用性**和**速度**。补上打桩后，
        增强管线返回固定文本 -> 用例只测它该测的东西
        （一次性 JSON 路径 + 工具卡的「参数」「结果」两行）。

        【为什么不用 skip / xfail】
        那是掩盖缺陷。真实缺陷在**测试自身**（没隔离外部依赖），
        修测试隔离才是正解 —— 产品代码一行没动。
        """
        original = agent_mod.llm_client.call_provider_with_tools
        calls = {"n": 0}

        async def fake_call(provider, messages, tools, options=None, images=None):
            index = calls["n"]
            calls["n"] += 1
            if index >= len(turns):
                return {"content": "结束", "tool_calls": [], "raw": {}}
            return turns[index]

        agent_mod.llm_client.call_provider_with_tools = fake_call
        self.addCleanup(
            lambda: setattr(agent_mod.llm_client,
                            "call_provider_with_tools", original))

        # ---- 第二条链：提示词增强管线的 llm.call_provider ----
        import h3studio.llm as enhance_llm
        original_enhance = enhance_llm.call_provider

        async def fake_enhance_call(provider, messages, options=None, **kw):
            """增强管线的 LLM 返回固定扩写文本。

            返回值必须能通过增强管线的校验：
              * 非空
              * 不是系统提示词复读（echo 检测）
              * 不违反提示词硬约束（validate_output）
            """
            return ("雨夜霓虹街道，霓虹招牌倒映在湿润的柏油路面上，"
                    "镜头缓慢向前推进，赛博朋克都市夜景，细节丰富。")

        enhance_llm.call_provider = fake_enhance_call
        self.addCleanup(
            lambda: setattr(enhance_llm, "call_provider", original_enhance))

    # ------------------------------------------------------------ 一次性路径
    def _run_one_shot(self, turns):
        """
        走**真实**的一次性 JSON 路径，返回 (DOM 断言数据, 抓到的响应)。

        怎么造一次性：
          前端 sendStreamNew 发的请求 body 里**去掉 stream 字段**
          —— 服务端收到不带 stream 的 body 就直接返回一次性 JSON。
          这里用 Playwright 的 route 拦截**改写请求体**，
          但**请求照常打到真实服务端**（不是伪造响应）。

        整段逻辑跑在浏览器线程里 —— 主线程只负责发指令。
        """
        self._patch_model(turns)
        browser = self.browser()
        # 每个用例开**全新页面** —— 状态彻底隔离，
        # 断言只可能打到自己产生的东西（见 browser() 的说明）。
        self._new_page()

        def _work():
            page = browser._page
            captured = {"bodies": [], "responses": []}

            # 清状态（新页面本来就是干净的 —— 这里是双保险）
            page.evaluate("""() => {
              window.__probe = {agentui: typeof window.AgentUI,
                                tools: document.querySelectorAll('.ag-tool').length,
                                msgs: -1};
              if (!window.AgentUI) return;
              window.AgentUI.state.messages = [];
              window.AgentUI.state.sessionId = '';
              window.AgentUI.state.lastResult = null;
              window.AgentUI.state.streaming = null;
              window.AgentUI.state.busy = false;
              const m = document.getElementById('agMessages');
              if (m) m.innerHTML = '';
            }""")

            def _route(route):
                req = route.request
                try:
                    body = req.post_data or ""
                except Exception:
                    body = ""
                captured["bodies"].append(body)
                if req.url.endswith("/api/agent/chat"):
                    try:
                        payload = json.loads(body) if body else {}
                    except Exception:
                        payload = {}
                    # 去掉 stream -> 服务端走一次性 JSON 分支
                    payload.pop("stream", None)
                    route.continue_(post_data=json.dumps(payload))
                    return
                route.continue_()

            page.route("**/api/agent/chat*", _route)

            def _on_response(resp):
                try:
                    if "/api/agent/chat" in resp.url:
                        captured["responses"].append({
                            "url": resp.url,
                            "status": resp.status,
                            "content_type": resp.headers.get("content-type", ""),
                        })
                except Exception:
                    pass

            page.on("response", _on_response)
            probe = {}
            try:
                page.fill("#agInput", "做个视频")
                # 用 expect_response 同步等真实响应 ——
                # page.on("response") 是异步派发，在 route 拦截下
                # 不保证在本用例内被观察到（实测捕获到 0 条）。
                with page.expect_response(
                        lambda r: "/api/agent/chat" in r.url,
                        timeout=90000) as resp_info:
                    page.click("#agSend")
                resp = resp_info.value
                captured["responses"].append({
                    "url": resp.url,
                    "status": resp.status,
                    "content_type": resp.headers.get("content-type", ""),
                })
                # 等本条用例的请求**真正跑完**（前端 busy 置回 false），
                # 而不是死等固定毫秒数 —— 机器慢的时候固定等待会漏。
                try:
                    page.wait_for_function(
                        """() => {
                          if (!window.AgentUI) return false;
                          const s = window.AgentUI.state;
                          return !s.busy && !s.streaming;
                        }""",
                        timeout=90000)
                except Exception:
                    pass
                page.wait_for_timeout(500)
                probe = page.evaluate("() => window.__probe || {}")
                dom = page.evaluate("""() => ({
                  tools: Array.from(document.querySelectorAll('.ag-tool')).map(t => ({
                    name: (t.querySelector('.ag-tool-name')||{}).textContent || '',
                    args: (t.querySelector('.ag-tool-args code')||{}).textContent || '',
                    result: (t.querySelector('.ag-tool-result code')||{}).textContent || '',
                    err: (t.querySelector('.ag-tool-err')||{}).textContent || '',
                    ok: !t.classList.contains('ag-tool-bad'),
                  })),
                  resultNodeCount: document.querySelectorAll('.ag-tool-result').length,
                  argsNodeCount: document.querySelectorAll('.ag-tool-args').length,
                })""")
            finally:
                page.unroute("**/api/agent/chat*", _route)
            captured['probe'] = probe
            return dom, captured

        return browser.call(_work)

    @staticmethod
    def _turns(tool_name, arguments):
        return [
            {"content": None, "tool_calls": [
                {"id": "call_1", "name": tool_name, "arguments": arguments}]},
            {"content": "好了", "tool_calls": [], "raw": {}},
        ]

    def _http_post(self, payload):
        """主线程直接发真实 HTTP 请求，返回 (ctype, body)。"""
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/agent/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            return (resp.headers.get("Content-Type", ""),
                    resp.read().decode("utf-8"))

    # ================================================================ 用例
    def test_real_http_one_shot_response_is_json_not_sse(self):
        """
        真实 HTTP 下，服务端在不带 stream 时**真的**返回一次性 JSON。

        这条锁死「一次性路径在真实网络下确实存在且可达」——
        如果服务端行为变了（比如强制 SSE），这条会红，
        后面所有「一次性路径」的断言就变成了测一个不存在的东西。
        """
        self._patch_model(self._turns("list_modes", {}))
        ctype, raw = self._http_post({"message": "做个视频"})
        self.assertNotIn("text/event-stream", ctype,
                         f"不带 stream 的请求不该返回 SSE：{ctype}")
        self.assertIn("application/json", ctype, ctype)
        payload = json.loads(raw)
        self.assertTrue(payload.get("steps"), f"一次性响应没有 steps：{payload}")

    def test_one_shot_real_http_steps_carry_summary_and_call_id(self):
        """
        真实 HTTP 的一次性响应里，steps[] 必须带 summary 与 call_id。

        这是前端能渲染出「结果」行的**数据前提** ——
        没有 summary，前端拿不到结果内容（只能显示空区块）。
        """
        self._patch_model(self._turns("list_modes", {}))
        _, raw = self._http_post({"message": "做个视频"})
        payload = json.loads(raw)
        steps = payload.get("steps") or []
        self.assertTrue(steps,
                        f"没有步骤：{payload}（诊断："
                        f"model_patched="
                        f"{agent_mod.llm_client.call_provider_with_tools.__name__}）")
        step = steps[0]
        self.assertIn("summary", step, f"steps[] 没有 summary：{step}")
        self.assertTrue(str(step.get("summary") or "").strip(),
                        f"steps[].summary 是空的：{step}")
        self.assertTrue(step.get("call_id"), f"steps[] 没有 call_id：{step}")
        self.assertIn("args", step, f"steps[] 没有 args：{step}")

    def test_one_shot_real_http_tool_card_has_result(self):
        """
        **真实 HTTP 一次性路径**：工具卡必须有非空的「结果」行。

        这条直接对应复核实测的「`.ag-tool-result` 节点数 = 0」。
        """
        dom, captured = self._run_one_shot(self._turns("list_modes", {}))
        self.assertTrue(captured["responses"],
                        f"没有捕获到真实 HTTP 响应：{captured}")
        for r in captured["responses"]:
            self.assertNotIn("text/event-stream", r["content_type"],
                             f"一次性路径却返回了 SSE：{r}")
        self.assertTrue(dom["tools"],
                        f"一次性路径没有渲染出工具块：{dom}")
        self.assertGreaterEqual(dom["resultNodeCount"], 1,
                                f"没有 .ag-tool-result 节点：{dom}")
        self.assertTrue(dom["tools"][0]["result"].strip(),
                        f"一次性路径工具卡的「结果」区是空的：{dom['tools'][0]}")

    def test_one_shot_real_http_tool_card_has_args_and_result(self):
        """「参数」「结果」两行都要有。"""
        dom, _ = self._run_one_shot(self._turns(
            "create_job",
            {"mode": "t2v", "prompt": "雨夜霓虹街道", "aspect": "9:16"}))
        self.assertTrue(dom["tools"], f"没有工具块：{dom}")
        tool = dom["tools"][0]
        self.assertTrue(tool["args"].strip(), f"缺「参数」行：{tool}")
        self.assertTrue(tool["result"].strip(), f"缺「结果」行：{tool}")
        self.assertIn("mode=t2v", tool["args"])
        self.assertIn("已创建任务", tool["result"])

    def test_one_shot_real_http_failed_tool_shows_error_text(self):
        """失败工具在一次性路径下必须显示错误文案。"""
        dom, _ = self._run_one_shot(
            self._turns("definitely_not_a_tool", {}))
        self.assertTrue(dom["tools"], f"没有工具块：{dom}")
        tool = dom["tools"][0]
        self.assertFalse(tool["ok"], f"失败工具被渲染成成功态：{tool}")
        self.assertIn("未知工具", tool["result"],
                      f"失败工具没有显示错误文案：{tool}")
        # 错误文案只显示一次（结果行里已有，不重复渲染 .ag-tool-err）
        self.assertEqual(tool["err"].strip(), "",
                         f"错误文案重复显示了：{tool}")


if __name__ == "__main__":
    unittest.main()
