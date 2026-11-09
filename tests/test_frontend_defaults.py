# -*- coding: utf-8 -*-
"""前端默认态与任务列表回归测试（真实浏览器 + 真实后端 + 真实 SQLite）。

覆盖两条用户可见的缺陷：
  1. 页面加载时左侧导航没有默认选中项，参数区渲染的是「文生视频」而不是
     「Z-IMAGE 文生图」。
  2. 默认「全部状态 + 全部类型」下任务列表空白，必须再点一次「全部」才有内容。

第 2 条的真实根因是：app.js 顶层启动脚本里任何一处抛异常都会中断整段脚本，
`refreshJobs()` / `setInterval(refreshJobs, 2000)` 根本没注册；而标签的
active 状态是在更早的 renderTabs() 里设好的，所以看起来「筛选是对的、
列表却是空的」。这里用故障注入复现该形态，并验证 boot() 逐项隔离能挡住。

测试用独立临时数据目录 + 预置任务，不依赖也不污染真实库。

运行：python3 -m pytest tests/test_frontend_defaults.py -v
需要：playwright（chromium）。缺失时整类 skip，不算失败。
"""
import atexit
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import weakref
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:                                   # pragma: no cover
    sync_playwright = None
    HAS_PLAYWRIGHT = False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# 预置任务：覆盖全部已注册生成类型 + 各种状态，用于验证「全部类型」语义
SEED_MODES = ["t2i", "t2v", "flf", "r2v", "longfilm"]
SEED_STATUSES = ["done", "done", "done", "failed", "canceled"]


class StudioServer:
    """在临时目录里起一个独立端口的 Studio 实例（真实 server.py + 真实前端）。

    数据目录是临时的，并预置若干任务 —— 「默认筛选返回非空列表」这条断言
    必须有数据才有意义，且不能依赖真实库的内容。
    """

    def __init__(self, app_js_override: bytes | None = None, seed_jobs: int = 0,
                 comfyui_url: str | None = None):
        self.port = free_port()
        self.tmp = Path(tempfile.mkdtemp(prefix="h3studio-ui-"))
        self.app_js_override = app_js_override
        self.seed_jobs = seed_jobs
        # comfyui_url 可指向可控的假 ComfyUI（测试自己控队列状态），
        # 默认仍用真实的 127.0.0.1:8188
        self.comfyui_url = comfyui_url or "http://127.0.0.1:8188"
        self.data_dir = self.tmp / "data"
        self.proc = None

    # ------------------------------------------------------------ 数据
    def _seed(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        db_path = self.data_dir / "jobs.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, created_at REAL, mode TEXT, params TEXT,
                status TEXT, prompt_id TEXT, progress REAL DEFAULT 0,
                progress_text TEXT, error TEXT, outputs TEXT,
                started_at REAL, finished_at REAL
            );
            CREATE TABLE IF NOT EXISTS job_batches (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL, idx INTEGER NOT NULL,
                seed INTEGER, status TEXT, prompt_id TEXT, progress REAL DEFAULT 0,
                progress_text TEXT, error TEXT, outputs TEXT,
                created_at REAL, started_at REAL, finished_at REAL
            );
            """
        )
        now = time.time()
        for i in range(self.seed_jobs):
            mode = SEED_MODES[i % len(SEED_MODES)]
            status = SEED_STATUSES[i % len(SEED_STATUSES)]
            params = {"title": f"种子任务 {i}", "aspect": "16:9", "prompt": f"seed prompt {i}"}
            conn.execute(
                "INSERT INTO jobs (id, created_at, mode, params, status, progress,"
                " progress_text, outputs) VALUES (?,?,?,?,?,?,?,?)",
                (f"seed{i:04d}", now - i * 60, mode,
                 json.dumps(params, ensure_ascii=False), status,
                 1.0 if status == "done" else 0.0, "", "[]"),
            )
        conn.commit()
        conn.close()

    # ------------------------------------------------------------ 进程
    def start(self):
        shutil.copytree(REPO / "h3studio", self.tmp / "h3studio")
        shutil.copy2(REPO / "server.py", self.tmp / "server.py")
        shutil.copytree(REPO / "web", self.tmp / "web")
        if self.app_js_override is not None:
            (self.tmp / "web" / "app.js").write_bytes(self.app_js_override)
        cfg = {
            "host": "127.0.0.1",
            "port": self.port,
            "comfyui_url": self.comfyui_url,
            "data_dir": str(self.data_dir),
            "models_dir": "/home/ubuntu/comfyui/models",
            "poll_interval": 1.5,
        }
        (self.tmp / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self._seed()
        self.proc = subprocess.Popen(
            [sys.executable, "server.py"], cwd=str(self.tmp),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # 【2026-11-10】注册兜底清理 ——
        # setUpClass 里 `assert started` 之类失败时，tearDownClass **不会**执行，
        # 于是这个 server 进程就泄漏了。实测一次全量 pytest 会泄漏几十上百个
        # /tmp/h3studio-ui-* 进程（每个占一个端口 + 一份内存），
        # 把后续所有浏览器用例拖成「端口耗尽」的假失败
        # （本轮实测：168 个 error，全是这类假失败，不是产品缺陷）。
        #
        # 兜底方案：atexit + weakref.finalize 双保险，
        # 无论测试怎么结束（正常 / 异常 / Ctrl-C），进程都会被收掉。
        atexit.register(self.stop)
        self._finalizer = weakref.finalize(self, _reap_proc, self.proc, self.tmp)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return True
            except OSError:
                time.sleep(0.2)
        return False

    def stop(self):
        proc, self.proc = self.proc, None
        if proc:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
            except Exception:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)


def _reap_proc(proc, tmp):
    """atexit / weakref.finalize 的兜底收尸函数（模块级，避免闭包持有 self）。"""
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass
    shutil.rmtree(tmp, ignore_errors=True)


def texts(page, selector):
    """取一组元素的文本（显式走 evaluate，避免 eval_on_selector_all 的语义坑）。"""
    return page.evaluate(
        "sel => Array.from(document.querySelectorAll(sel)).map(e => e.textContent.trim())",
        selector)


def visible(page, selector):
    return page.evaluate(
        "sel => { const el = document.querySelector(sel);"
        " return !!el && getComputedStyle(el).display !== 'none'; }",
        selector)


@unittest.skipUnless(HAS_PLAYWRIGHT, "未安装 playwright")
class TestFrontendDefaults(unittest.TestCase):
    """真实浏览器打开真实 Studio，断言默认态。"""

    @classmethod
    def setUpClass(cls):
        cls.server = StudioServer(seed_jobs=25)
        assert cls.server.start(), "Studio 实例启动失败"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])
        cls.ctx = cls.browser.new_context()
        cls.page = cls.ctx.new_page()
        cls.console_errors = []
        cls.page.on("pageerror", lambda e: cls.console_errors.append(str(e)))
        cls.page.on("console", lambda m: cls.console_errors.append(m.text)
                    if m.type == "error" else None)
        cls.page.goto("http://127.0.0.1:%d/" % cls.server.port, wait_until="domcontentloaded")
        # 等首屏任务列表渲染完
        cls.page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid > *').length > 0", timeout=20000)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.stop()

    # ------------------------------------------------------- 需求 1
    def test_default_nav_item_is_zimage(self):
        """默认加载时左侧导航高亮的是「Z-IMAGE 文生图」。"""
        active = texts(self.page, ".nav-item.active")
        self.assertEqual(active, ["Z-IMAGE 文生图"],
                         "默认高亮项应为「Z-IMAGE 文生图」，实际 %r" % active)

    def test_default_mode_is_t2i(self):
        """默认状态机停在 t2i（图像模式）。"""
        self.assertEqual(self.page.evaluate("state.mode"), "t2i")

    def test_default_panel_shows_image_controls(self):
        """默认参数区展示图像面板（分辨率档位 / 比例 / 模型），不是视频控件。"""
        self.assertTrue(visible(self.page, "#imagePanel"),
                        "默认应展示图像面板（分辨率/比例/提示词/模型/LoRA）")
        for hidden in ("#resField", "#durField", "#aspectField"):
            self.assertFalse(visible(self.page, hidden),
                             "图像模式下 %s 应隐藏（视频专属控件）" % hidden)

    def test_default_panel_has_real_image_options(self):
        """图像面板内容是真实的 Z-Image 参数（档位 / 比例 / 模型），不是空壳。"""
        tiers = texts(self.page, "#imgResSelect option")
        self.assertGreaterEqual(len(tiers), 5, "分辨率档位过少: %r" % tiers)
        self.assertTrue(any("×" in t for t in tiers),
                        "档位应显示具体像素宽高: %r" % tiers)
        aspects = texts(self.page, "#imgAspectChips button")
        for want in ("1:1", "16:9", "9:16"):
            self.assertIn(want, aspects)
        models = self.page.evaluate(
            "() => Array.from(document.querySelectorAll('#imgModelSelect option'))"
            ".map(o => o.value)")
        self.assertTrue(models, "模型下拉不应为空")

    def test_switching_mode_keeps_nav_and_panel_in_sync(self):
        """手动切到其它模式：选中态与面板内容同步；切回 Z-IMAGE 也同步。"""
        for mode, label in (("t2v", "文生视频"), ("flf", "首尾帧"), ("r2v", "参考生成")):
            self.page.click('.nav-item[data-mode="%s"]' % mode)
            self.page.wait_for_function("m => state.mode === m", arg=mode, timeout=5000)
            self.assertEqual(texts(self.page, ".nav-item.active"), [label],
                             "切到 %s 后高亮应同步" % label)
            self.assertFalse(visible(self.page, "#imagePanel"),
                             "切到 %s 后图像面板应隐藏" % label)

        self.page.click('.nav-item[data-mode="t2i"]')
        self.page.wait_for_function("() => state.mode === 't2i'", timeout=5000)
        self.assertEqual(texts(self.page, ".nav-item.active"), ["Z-IMAGE 文生图"])
        self.assertTrue(visible(self.page, "#imagePanel"))

    # ------------------------------------------------------- 需求 2
    def test_default_filters_show_job_list(self):
        """默认「全部状态 + 全部类型」下任务列表非空，无需再点一次「全部」。"""
        self.assertEqual(texts(self.page, '.jobs-tabs button[data-st].active'), ["全部"])
        self.assertEqual(texts(self.page, '#genTypeTabs button.active'), ["全部类型"])

        count = self.page.evaluate("document.querySelectorAll('#jobsGrid > *').length")
        self.assertGreater(count, 0, "默认筛选下任务列表为空")
        self.assertEqual(texts(self.page, "#jobsGrid .empty-hint"), [],
                         "默认筛选下不该出现空提示")

    def test_default_filter_returns_all_generation_types(self):
        """「全部类型」语义一致：各分类的任务都能在「全部」里翻到。"""
        data = self.page.evaluate(
            "fetch('/api/jobs?status=all&page=1&page_size=100')"
            ".then(r => r.json()).then(d => d)")
        self.assertGreater(data["total"], 0)
        modes = {j["mode"] for j in data["jobs"]}
        for want in ("t2i", "t2v"):
            self.assertIn(want, modes, "「全部」应包含 %s 类任务，实际 %r" % (want, modes))

        # 「全部」标签必须可翻页，否则「全部类型」名义下部分分类根本看不到
        self.assertFalse(self.page.evaluate(
            "document.querySelector('#jobsPagination').hidden"),
            "「全部」标签应有分页（否则只显示最新一页，与「全部类型」语义不符）")

    def test_pagination_reaches_other_generation_types(self):
        """翻页能看到首页之外的其它类型（修复前首页 8 条可能全是同一类）。"""
        seen = set()
        for page_no in (1, 2, 3):
            self.page.click('#jobsPagination button[data-page="%d"]' % page_no)
            self.page.wait_for_timeout(600)
            seen.update(self.page.evaluate(
                "() => Array.from(document.querySelectorAll('#jobsGrid .job-top'))"
                ".map(e => e.textContent.trim())"))
        self.assertGreater(len(seen), 8, "翻页后应能看到更多任务，实际 %d 条" % len(seen))

    def test_periodic_refresh_is_registered(self):
        """定时刷新已注册：新任务无需用户操作就自动出现在列表里。

        这是「启动脚本被中断就丢 setInterval」的直接检验 —— 旧行为下
        页面打开后任务列表永远停在初始状态。
        """
        # 先回到第 1 页（前面用例可能翻过页），保证新任务会落在当前页
        self.page.evaluate("state.donePage = 1; refreshJobs();")
        self.page.wait_for_timeout(800)

        # 卡片上显示的是 params.prompt（标题只给 longfilm 用），用 prompt 做探针
        probe = "刷新探针-" + str(int(time.time()))
        conn = sqlite3.connect(self.server.data_dir / "jobs.db")
        conn.execute(
            "INSERT INTO jobs (id, created_at, mode, params, status, progress,"
            " progress_text, outputs) VALUES (?,?,?,?,?,?,?,?)",
            ("live0001", time.time(), "t2i",
             json.dumps({"title": "定时刷新探针", "prompt": probe}, ensure_ascii=False),
             "done", 1.0, "", "[]"))
        conn.commit()
        conn.close()

        # 不做任何用户操作 —— 只能靠 setInterval(refreshJobs, 2000) 自己刷新
        self.page.wait_for_function(
            "t => document.body.innerText.includes(t)", arg=probe, timeout=15000)

    def test_no_console_errors_on_load(self):
        """首屏无 JS 报错（脚本被中断会在这里暴露）。"""
        self.assertEqual([e for e in self.console_errors if e.strip()], [])

    # ------------------------------------------------- 缓存失效（防旧版卡死）
    def test_index_html_is_not_cached(self):
        """首页必须回源校验。

        index.html 是前端资源的「版本清单」：一旦被浏览器缓存，app.js 的
        ?v= 版本号就传不出去，用户会一直用着旧 app.js —— 曾经因此出现
        esc() 未定义 -> 任务列表空白。
        """
        headers = self.page.evaluate(
            "async () => { const r = await fetch('/', {cache:'no-store'});"
            " const h = {}; r.headers.forEach((v,k) => h[k]=v); return h; }")
        cache_control = headers.get("cache-control", "")
        self.assertIn("no-cache", cache_control,
                      "首页应回源校验，实际 Cache-Control=%r" % cache_control)

    def test_static_assets_have_cache_buster(self):
        """app.js / providers.js / style.css 都带 ?v= 版本号。"""
        srcs = self.page.evaluate(
            "() => Array.from(document.querySelectorAll('script[src], link[rel=stylesheet]'))"
            ".map(e => e.src || e.href)")
        for asset in ("app.js", "providers.js", "style.css"):
            hit = [s for s in srcs if asset in s]
            self.assertTrue(hit, "页面没引用 %s" % asset)
            self.assertIn("?v=", hit[0],
                          "%s 缺少 ?v= 缓存失效标记（升级后用户拿不到新版）: %s"
                          % (asset, hit[0]))


@unittest.skipUnless(HAS_PLAYWRIGHT, "未安装 playwright")
class TestStartupFaultIsolation(unittest.TestCase):
    """启动脚本逐项隔离：单点异常不得让整段启动脚本失效。

    这正是「默认筛选下历史列表空白」的根因修复：
    旧 app.js 里 esc() 未定义 -> ReferenceError -> 顶层脚本中断 ->
    refreshJobs() / setInterval 未注册 -> 列表空白，点一次「全部」才恢复。
    """

    @classmethod
    def setUpClass(cls):
        # 制造「app.js 里没有 esc 兜底」的故障态（esc 只在 providers.js，后加载）
        app_js = (REPO / "web" / "app.js").read_bytes()
        marker = "// esc() 的正式定义".encode()
        start = app_js.index(marker)
        end = app_js.index(b"function escapeHtml")
        broken = app_js[:start] + app_js[end:]
        assert b"function esc(s)" not in broken, "故障注入没生效"

        cls.server = StudioServer(app_js_override=broken, seed_jobs=25)
        assert cls.server.start()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.stop()

    def test_job_list_still_loads_despite_startup_error(self):
        """即使 app.js 启动期抛错，任务列表依然要自动加载（不用手动点）。"""
        page = self.browser.new_page()
        try:
            page.goto("http://127.0.0.1:%d/" % self.server.port,
                      wait_until="domcontentloaded")
            page.wait_for_function(
                "() => document.querySelectorAll('#jobsGrid > *').length > 0",
                timeout=20000)
            count = page.evaluate("document.querySelectorAll('#jobsGrid > *').length")
            self.assertGreater(count, 0,
                               "启动期异常后任务列表仍为空白 —— 逐项隔离失效")
            self.assertEqual(texts(page, "#jobsGrid .empty-hint"), [])
        finally:
            page.close()

    def test_default_mode_still_selected_despite_startup_error(self):
        """启动期异常也不影响默认模式的高亮与面板。"""
        page = self.browser.new_page()
        try:
            page.goto("http://127.0.0.1:%d/" % self.server.port,
                      wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            self.assertEqual(texts(page, ".nav-item.active"), ["Z-IMAGE 文生图"])
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
