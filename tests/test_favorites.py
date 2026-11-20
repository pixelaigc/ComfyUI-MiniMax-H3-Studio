# -*- coding: utf-8 -*-
"""生成媒体库「收藏」（2026-11-20 需求 2/3/4/5）回归测试。

覆盖 5 组：

  A. 后端收藏读写（持久化 / 幂等 / 404 / 批量）
  B. 清理跳过收藏（preview 与 cleanup 同口径；取消收藏后重新纳入范围）
  C. 列表接口的 favorites 过滤 + favorited 标记
  D. 前端：提交后**不跳标签**（真实浏览器）
  E. 前端：右键收藏 / 左侧收藏栏 / 批量取消收藏（真实浏览器）

【为什么既有单测又有浏览器用例】
收藏的「持久化」是后端事实（单测能验）；
但「右键菜单里有没有这一项」「★ 标识有没有出现」「左侧栏点进去对不对」
是**渲染出来才存在**的事实 —— 源码里有字符串不等于界面里有。
上一轮审查看过这类空洞断言的教训（test_agent_media_semantics_frontend
里那条「把插值改成空串断言照样全绿」），所以 D/E 用真实 Chromium。

运行：
    python3 -m pytest tests/test_favorites.py -v
需要 playwright（chromium）。缺失时浏览器用例整类 skip，不算失败。
"""
from __future__ import annotations

import json
import os
import re
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
sys.path.insert(0, str(REPO / "tests"))

from h3studio import favorites as fav_mod          # noqa: E402
from h3studio import media_cleanup                # noqa: E402

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:                                   # pragma: no cover
    sync_playwright = None
    HAS_PLAYWRIGHT = False


WAIT_TIMEOUT = 60000


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_db(path: Path, jobs: list[tuple] | None = None) -> sqlite3.Connection:
    """建一个最小 jobs 库（含收藏表），返回已连接的 conn。"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
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
    fav_mod.ensure_table(conn)
    now = time.time()
    for jid, status, out in (jobs or []):
        conn.execute(
            "INSERT OR REPLACE INTO jobs (id, created_at, mode, params, status,"
            " progress, progress_text, outputs) VALUES (?,?,?,?,?,?,?,?)",
            (jid, now, "t2i", json.dumps({"prompt": "p " + jid}),
             status, 1.0 if status == "done" else 0.0, "",
             json.dumps(out)),
        )
    conn.commit()
    return conn


# ==================================================================
# A. 后端：收藏读写
# ==================================================================
class TestFavoritesRepo(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fav-repo-"))
        self.conn = make_db(self.tmp / "jobs.db",
                            [("j1", "done", []), ("j2", "done", [])])

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_set_and_read_is_persistent(self):
        """收藏写入后能从同一个库读回来（真持久化，不是内存态）。"""
        self.assertTrue(fav_mod.set_favorite(self.conn, "j1", True))
        self.conn.commit()
        self.assertTrue(fav_mod.is_favorited(self.conn, "j1"))
        self.assertFalse(fav_mod.is_favorited(self.conn, "j2"))
        self.assertEqual(fav_mod.count(self.conn), 1)

    def test_favorite_is_idempotent(self):
        """重复收藏不新增第二条记录（幂等）。"""
        fav_mod.set_favorite(self.conn, "j1", True)
        fav_mod.set_favorite(self.conn, "j1", True)
        self.conn.commit()
        self.assertEqual(fav_mod.count(self.conn), 1)

    def test_unfavorite_removes_marker_only(self):
        """取消收藏只删标记 —— jobs 记录与 outputs 一律不动。"""
        out = [{"kind": "image", "file": "images/a.png"}]
        self.conn.execute("UPDATE jobs SET outputs=? WHERE id='j1'",
                          (json.dumps(out),))
        self.conn.commit()
        fav_mod.set_favorite(self.conn, "j1", True)
        self.conn.commit()
        fav_mod.set_favorite(self.conn, "j1", False)
        self.conn.commit()
        self.assertFalse(fav_mod.is_favorited(self.conn, "j1"))
        row = self.conn.execute(
            "SELECT status, outputs FROM jobs WHERE id='j1'").fetchone()
        self.assertIsNotNone(row, "取消收藏**不能**删掉任务记录")
        self.assertEqual(json.loads(row["outputs"]), out,
                         "取消收藏**不能**动产物索引")

    def test_favorite_ids_batch(self):
        """批量取收藏状态（一次查询，不 N+1）。"""
        fav_mod.set_favorite(self.conn, "j1", True)
        fav_mod.set_favorite(self.conn, "j2", True)
        self.conn.commit()
        self.assertEqual(fav_mod.favorite_ids(self.conn, ["j1", "j2"]),
                         {"j1", "j2"})
        self.assertEqual(fav_mod.favorite_ids(self.conn, ["j1"]), {"j1"})
        self.assertEqual(fav_mod.favorite_ids(self.conn, []), set())

    def test_ensure_table_is_backward_compatible(self):
        """老库（没有收藏表）执行 ensure_table 不报错、不动既有记录。"""
        raw = sqlite3.connect(self.tmp / "legacy.db")
        raw.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT)")
        raw.execute("INSERT INTO jobs VALUES ('x', 'done')")
        raw.commit()
        fav_mod.ensure_table(raw)
        raw.commit()
        rows = raw.execute("SELECT id, status FROM jobs").fetchall()
        self.assertEqual(rows, [("x", "done")], "既有记录不得丢失或被改动")
        raw.close()

    def test_remove_for_job_clears_orphan(self):
        """删任务时顺带清收藏标记（防孤儿行）。"""
        fav_mod.set_favorite(self.conn, "j1", True)
        self.conn.commit()
        fav_mod.remove_for_job(self.conn, "j1")
        self.conn.commit()
        self.assertEqual(fav_mod.count(self.conn), 0)


# ==================================================================
# B. 清理跳过收藏（需求 4）
# ==================================================================
class TestCleanupSkipsFavorites(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fav-clean-"))
        self.out = self.tmp / "outputs"
        (self.out / "images").mkdir(parents=True)
        self.conn = make_db(self.tmp / "jobs.db", [
            ("keep", "done", [{"kind": "image", "file": "images/keep.png"}]),
            ("drop", "done", [{"kind": "image", "file": "images/drop.png"}]),
        ])
        for name in ("keep.png", "drop.png"):
            (self.out / "images" / name).write_bytes(b"x")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_preview_and_cleanup_skip_favorited(self):
        """已收藏的任务不进清理范围（preview 与 cleanup 同口径）。"""
        fav_mod.set_favorite(self.conn, "keep", True)
        self.conn.commit()

        info = media_cleanup.preview(self.conn, self.out, "all", 0)
        self.assertEqual(info["jobs"], 1, "预览只能算上未收藏的那条")
        self.assertEqual(info["skipped_favorited"], 1)

        result = media_cleanup.cleanup(self.conn, self.out, "all", 0)
        self.assertEqual(result["deleted_jobs"], 1)
        self.assertEqual(result["skipped_favorited"], 1)
        # 收藏项：记录在、文件在、索引在。
        row = self.conn.execute(
            "SELECT outputs FROM jobs WHERE id='keep'").fetchone()
        self.assertIsNotNone(row, "已收藏的任务记录不得被清理")
        self.assertTrue((self.out / "images" / "keep.png").is_file(),
                        "已收藏的产物文件不得被删除")
        self.assertTrue(json.loads(row["outputs"]), "文件索引不得被清空")
        # 未收藏项：记录与文件都清掉。
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM jobs WHERE id='drop'").fetchone())
        self.assertFalse((self.out / "images" / "drop.png").exists())

    def test_unfavorite_reenters_cleanup_scope(self):
        """取消收藏后，该卡片重新纳入清理范围。"""
        fav_mod.set_favorite(self.conn, "keep", True)
        self.conn.commit()
        media_cleanup.cleanup(self.conn, self.out, "all", 0)
        self.assertTrue((self.out / "images" / "keep.png").is_file())

        fav_mod.set_favorite(self.conn, "keep", False)
        self.conn.commit()
        result = media_cleanup.cleanup(self.conn, self.out, "all", 0)
        self.assertEqual(result["deleted_jobs"], 1)
        self.assertFalse((self.out / "images" / "keep.png").exists(),
                         "取消收藏后应能被清理")

    def test_all_cleanup_scopes_skip_favorites(self):
        """**全部**清理路径都跳过收藏（all / older / failed）。"""
        # 造一条老的失败任务
        self.conn.execute(
            "INSERT INTO jobs (id, created_at, mode, params, status, progress,"
            " progress_text, outputs) VALUES ('oldfail', ?, 't2i', '{}',"
            " 'failed', 0, '', '[]')", (time.time() - 90 * 86400,))
        self.conn.commit()
        for jid in ("keep", "oldfail"):
            fav_mod.set_favorite(self.conn, jid, True)
        self.conn.commit()

        for scope, months in (("all", 0), ("older", 1), ("failed", 0)):
            with self.subTest(scope=scope):
                info = media_cleanup.preview(self.conn, self.out, scope, months)
                self.assertNotIn("keep",
                                 [m["id"] for m in info["sample"]],
                                 "%s 口径不能命中已收藏项" % scope)
                self.assertGreaterEqual(info["skipped_favorited"], 1)

        result = media_cleanup.cleanup(self.conn, self.out, "all", 0)
        self.assertEqual(result["skipped_favorited"], 2)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM jobs WHERE id='keep'").fetchone())

    def test_cleanup_works_without_favorites_table(self):
        """老库（没有收藏表）-> 清理行为与加收藏之前完全一致。"""
        legacy = sqlite3.connect(self.tmp / "legacy.db")
        legacy.row_factory = sqlite3.Row
        legacy.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, created_at REAL,"
                       " mode TEXT, params TEXT, status TEXT, progress REAL,"
                       " progress_text TEXT, error TEXT, outputs TEXT,"
                       " started_at REAL, finished_at REAL)")
        legacy.execute("CREATE TABLE job_batches (id TEXT PRIMARY KEY,"
                       " job_id TEXT, idx INTEGER, outputs TEXT)")
        legacy.execute("INSERT INTO jobs VALUES ('x', ?, 't2i', '{}', 'done',"
                       " 1.0, '', '', '[]', NULL, NULL)", (time.time(),))
        legacy.commit()
        result = media_cleanup.cleanup(legacy, self.out, "all", 0)
        self.assertEqual(result["deleted_jobs"], 1)
        self.assertEqual(result["skipped_favorited"], 0)
        legacy.close()


# ==================================================================
# C/E. 真实浏览器端到端
# ==================================================================
class StudioServer:
    """隔离的 Studio 实例（独立端口 + 独立数据目录），不碰真实库。"""

    def __init__(self, seed_jobs: int = 0):
        self.port = free_port()
        self.tmp = Path(tempfile.mkdtemp(prefix="fav-ui-"))
        self.seed_jobs = seed_jobs
        self.data_dir = self.tmp / "data"
        self.proc = None

    def _seed(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.data_dir / "jobs.db")
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
        modes = ["t2i", "t2v", "t2i_qwen"]
        for i in range(self.seed_jobs):
            mode = modes[i % len(modes)]
            conn.execute(
                "INSERT INTO jobs (id, created_at, mode, params, status,"
                " progress, progress_text, outputs) VALUES (?,?,?,?,?,?,?,?)",
                (f"seed{i:03d}", now - i * 60, mode,
                 json.dumps({"prompt": f"seed {i}", "aspect": "16:9"}),
                 "done", 1.0, "",
                 json.dumps([{"kind": "image", "file": f"images/s{i}.png",
                              "name": f"s{i}.png"}])),
            )
        conn.commit()
        conn.close()

    def start(self):
        shutil.copytree(REPO / "h3studio", self.tmp / "h3studio")
        shutil.copy2(REPO / "server.py", self.tmp / "server.py")
        shutil.copytree(REPO / "web", self.tmp / "web")
        cfg = {"host": "127.0.0.1", "port": self.port,
               "comfyui_url": "http://127.0.0.1:8188",
               "data_dir": str(self.data_dir),
               "models_dir": "/home/ubuntu/comfyui/models",
               "poll_interval": 1.5}
        (self.tmp / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self._seed()
        self.proc = subprocess.Popen(
            [sys.executable, "server.py"], cwd=str(self.tmp),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import atexit
        atexit.register(self.stop)
        self._finalizer = weakref.finalize(self, _reap, self.proc, self.tmp)
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
            except Exception:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)


def _reap(proc, tmp):
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            pass
    shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(HAS_PLAYWRIGHT, "未安装 playwright")
class TestFavoritesBrowser(unittest.TestCase):
    """真实 Chromium：右键收藏 / 收藏栏 / 批量取消 / 提交不跳标签。

    【为什么每条用例都起一个自己的服务实例】
    这组用例里有一条会**真的清理产物**（需求 4 的取证）——
    如果全类共用一个实例，那条用例把库里的卡片删掉之后，
    后面几条用例的前置条件（库里有 N 条卡片）就没了，
    表现为「单跑通过、一起跑失败」—— 那是测试污染，不是产品缺陷。
    每条用例一个实例 = 完全隔离，谁也不影响谁。
    """

    def setUp(self):
        self.server = StudioServer(seed_jobs=6)
        self.assertTrue(self.server.start(), "Studio 实例启动失败")
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(args=["--no-sandbox"])
        self.ctx = self.browser.new_context(viewport={"width": 1440, "height": 950})
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: self.errors.append(m.text)
                    if m.type == "error" else None)
        self.page.goto("http://127.0.0.1:%d/" % self.server.port,
                       wait_until="domcontentloaded")
        # 【必须等**真实**卡片，不能只等 .job-card】
        # 骨架卡也带 .job-card 类（只是没有 data-jid）——
        # 只等 .job-card 会拿到骨架，后面 query_selector('[data-jid=...]')
        # 全是 None。这不是理论风险，是实测踩到过的。
        self.page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length > 0",
            timeout=WAIT_TIMEOUT)
        self.page.wait_for_timeout(400)

    def tearDown(self):
        try:
            self.ctx.close()
            self.browser.close()
            self.playwright.stop()
        finally:
            self.server.stop()

    def _api(self, path, method="GET", body=None):
        import urllib.request
        url = "http://127.0.0.1:%d%s" % (self.server.port, path)
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())

    def _diag(self):
        """收集诊断信息：弹窗 / 提示条 / 控制台错误 / FAVORITES 是否就绪。

        【为什么要这个】断言只说「服务端未记录收藏状态」是不够的 ——
        到底是请求没发出去、发了但内容不对、还是发了但被拒？
        没有这些信息就只能靠猜。
        """
        return self.page.evaluate("""() => {
            const t = document.querySelector('#favToast');
            return {
                favoritesModule: typeof window.FAVORITES,
                toggleFn: typeof (window.FAVORITES||{}).toggleFavorite,
                toast: t ? t.textContent : null,
                toastClass: t ? t.className : null,
            };
        }""")

    # ------------------------------------------------ ① 提交后不跳标签
    def test_submit_keeps_current_tab(self):
        """点「生成」后标签**不**跳到「进行中」。

        【为什么要拦 POST /api/jobs】提交会真的建一个任务并占用
        ComfyUI 的 GPU 与生成额度 —— 取证不该消耗真实额度。
        这里把提交请求拦下来并回一个假的 job id，
        **前端提交后的那一段逻辑（标签渲染 + 列表刷新）照常跑** ——
        而标签跳变恰恰就发生在那段逻辑里，所以这条断言照样有效。
        """
        # 先切到「已完成」标签
        self.page.click(".jobs-tabs button[data-st='done']")
        self.page.wait_for_timeout(600)
        before = self.page.evaluate(
            "() => (document.querySelector('.jobs-tabs button[data-st].active')"
            "||{}).dataset?.st || ''")
        self.assertEqual(before, "done", "前置条件：当前标签应为「已完成」")

        # 拦下提交：不真的建任务
        posted = {}

        def _fake_submit(route, request):
            posted["hit"] = True
            route.fulfill(status=200,
                          content_type="application/json",
                          body=json.dumps({"id": "faketest0001"}))

        # 【为什么必须精确匹配 POST】`**/api/jobs` 会把 GET /api/jobs 也拦掉 ——
        # 列表接口被拦后返回假数据，后面的用例（同一个 page 对象）就全乱了。
        # 这里用正则只拦 POST 的建任务请求。
        #
        # 【为什么不用 addCleanup(unroute)】tearDown 里 playwright 已经停了，
        # 再调 unroute 会抛「Event loop is closed」——
        # 用例失败时这个异常会盖掉真正的失败原因。
        # 改成在**用例内部**（用 try/finally）撤销路由。
        route_pattern = re.compile(r".*/api/jobs$")
        self.page.route(route_pattern, _fake_submit)
        try:
            self.page.fill("#prompt", "收藏功能测试提示词")
            self.page.click("#genBtn")
            # 图像模式提示词未增强时会弹确认框 —— 走「确认生成」
            self.page.wait_for_timeout(1200)
            if self.page.is_visible("#enhanceGateConfirm"):
                self.page.click("#enhanceGateConfirm")
            self.page.wait_for_timeout(3000)
            self.assertTrue(posted.get("hit"), "前置条件：应发出过提交请求")

            after = self.page.evaluate(
                "() => (document.querySelector('.jobs-tabs button[data-st].active')"
                "||{}).dataset?.st || ''")
            self.assertEqual(after, before,
                             "提交后标签跳走了：%r -> %r" % (before, after))
        finally:
            try:
                self.page.unroute(route_pattern, _fake_submit)
            except Exception:
                pass   # playwright 已停 —— 无所谓，实例本来就是一次性的

    # ------------------------------------------------ ② 右键收藏
    def test_right_click_favorite_and_badge(self):
        """右键「添加到收藏」-> ★ 标识出现 -> 再右键变成「取消收藏」。"""
        card = self.page.query_selector("#jobsGrid .job-card")
        self.assertIsNotNone(card, "前置条件：应有卡片")
        jid = card.get_attribute("data-jid")

        # 右键预览区（收藏入口挂在预览区的 contextmenu 上）
        target = card.query_selector(".job-preview") or card
        target.click(button="right")
        self.page.wait_for_timeout(400)
        self.assertTrue(self.page.is_visible("#mediaContext.open"),
                        "右键菜单应打开")
        btn = self.page.query_selector('#mediaContext button[data-act="favorite"]')
        self.assertIsNotNone(btn, "右键菜单应包含收藏入口")
        self.assertEqual(btn.inner_text().strip(), "添加到收藏")
        btn.click()
        # 【为什么等「服务端已落库」而不是固定 sleep】
        # 浏览器对同一 host 只有 6 条并发连接，而页面后台还在跑几个慢请求
        # （/api/image-loras 要扫本机 LoRA 目录，实测 ~0.9s；
        #   /api/agent/sessions 也是慢接口）。
        # 连接被占满时，本请求会排在队列里 —— 固定 sleep 200ms 就断言
        # 必然是「假失败」（实测 POST 实际耗时 0.7~1.7s）。
        # 这里改成**等条件成立**，既不慢也不假。
        self.page.wait_for_function(
            """async (jid) => {
                const r = await fetch('/api/jobs/' + jid);
                const d = await r.json();
                return !!d.favorited;
            }""", arg=jid, timeout=WAIT_TIMEOUT)

        # 服务端已落库
        data = self._api("/api/jobs/" + jid)
        self.assertTrue(data.get("favorited"),
                        "服务端未记录收藏状态；诊断=%r；控制台=%r"
                        % (self._diag(), self.errors[-5:]))

        # 卡片上出现 ★ 标识
        self.page.wait_for_function(
            """(jid) => {
                const c = document.querySelector(`.job-card[data-jid="${jid}"]`);
                return !!(c && c.querySelector('.job-fav-badge'));
            }""", arg=jid, timeout=WAIT_TIMEOUT)
        badge = self.page.query_selector(
            '.job-card[data-jid="%s"] .job-fav-badge' % jid)
        self.assertIsNotNone(badge, "卡片上应出现已收藏标识")
        self.assertEqual(badge.inner_text().strip(), "★")

        # 再右键 -> 文案变成「取消收藏」
        self.page.query_selector(
            '.job-card[data-jid="%s"] .job-preview' % jid).click(button="right")
        self.page.wait_for_timeout(400)
        btn2 = self.page.query_selector('#mediaContext button[data-act="favorite"]')
        self.assertIsNotNone(btn2)
        self.assertEqual(btn2.inner_text().strip(), "取消收藏")

    # ------------------------------------------------ ③ 左侧收藏栏
    def test_left_nav_favorites_view(self):
        """左侧「收藏」入口可进入并显示收藏内容。"""
        # 先收藏一条
        jid = self._api("/api/jobs?status=all")["jobs"][0]["id"]
        self._api("/api/jobs/%s/favorite" % jid, "POST",
                  {"favorited": True})

        nav = self.page.query_selector("#navFavorites")
        self.assertIsNotNone(nav, "左侧应有「收藏」入口")
        self.assertIn("收藏", nav.inner_text())
        nav.click()
        self.page.wait_for_timeout(1500)

        # 收藏标签被激活
        active = self.page.evaluate(
            "() => (document.querySelector('.jobs-tabs button[data-st].active')"
            "||{}).dataset?.st || ''")
        self.assertEqual(active, "favorites", "进入后应切到「收藏」标签")
        # 导航高亮
        self.assertTrue(self.page.evaluate(
            "() => document.querySelector('#navFavorites').classList.contains('active')"),
            "「收藏」导航项应高亮")
        # 列表里能看到收藏的卡片
        self.page.wait_for_function(
            """(jid) => !!document.querySelector(
                 `.job-card[data-jid="${jid}"]`)""",
            arg=jid, timeout=WAIT_TIMEOUT)
        # 未收藏的卡片**不在**收藏视图里（收藏视图只显示收藏过的）
        total = self._api("/api/jobs?status=all")["total"]
        fav_total = self._api("/api/jobs?status=all&favorites=1")["total"]
        self.assertEqual(fav_total, 1, "收藏视图应只有 1 条")
        self.assertGreater(total, fav_total,
                           "前置条件：库里应有未收藏的卡片")

    # ------------------------------------------------ ④ 清理跳过收藏
    def test_cleanup_skips_favorited(self):
        """执行「管理 -> 清理」后，已收藏的卡片仍在。"""
        jobs = self._api("/api/jobs?status=all")["jobs"]
        keep = jobs[0]["id"]
        drop = jobs[1]["id"]
        self._api("/api/jobs/%s/favorite" % keep, "POST", {"favorited": True})
        # 预置一条会被清理掉的
        self._api("/api/jobs/%s/favorite" % drop, "POST", {"favorited": True})
        self._api("/api/jobs/%s/favorite" % drop, "POST", {"favorited": False})

        before_total = self._api("/api/jobs?status=all")["total"]
        result = self._api("/api/media/manage", "POST",
                           {"scope": "all", "months": 1, "confirm": True})
        self.assertGreaterEqual(result["skipped_favorited"], 1,
                                "清理结果应报告跳过了收藏项")
        after = self._api("/api/jobs?status=all")
        ids = [j["id"] for j in after["jobs"]]
        self.assertIn(keep, ids, "已收藏的卡片不应被清理")
        self.assertNotIn(drop, ids, "未收藏的卡片应被清理")

    # ------------------------------------------------ ⑤⑥ 取消收藏 / 批量
    def test_unfavorite_via_right_click(self):
        """右键「取消收藏」生效，且取消后卡片重新纳入清理范围。"""
        jid = self._api("/api/jobs?status=all")["jobs"][0]["id"]
        self._api("/api/jobs/%s/favorite" % jid, "POST", {"favorited": True})

        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length > 0",
            timeout=WAIT_TIMEOUT)
        self.page.wait_for_timeout(600)
        self.page.query_selector(
            '.job-card[data-jid="%s"] .job-preview' % jid).click(button="right")
        self.page.wait_for_timeout(400)
        btn = self.page.query_selector('#mediaContext button[data-act="favorite"]')
        self.assertEqual(btn.inner_text().strip(), "取消收藏")
        btn.click()
        # 等「服务端已取消」—— 同上，固定 sleep 会被后台慢请求拖成假失败。
        self.page.wait_for_function(
            """async (jid) => {
                const r = await fetch('/api/jobs/' + jid);
                const d = await r.json();
                return d.favorited === false;
            }""", arg=jid, timeout=WAIT_TIMEOUT)

        data = self._api("/api/jobs/" + jid)
        self.assertFalse(data.get("favorited"), "取消收藏未生效")

        # 取消后重新纳入清理范围
        result = self._api("/api/media/manage", "POST",
                           {"scope": "all", "months": 1, "confirm": True})
        after = self._api("/api/jobs?status=all")
        self.assertNotIn(jid, [j["id"] for j in after["jobs"]],
                         "取消收藏后应可被清理")

    def test_batch_unfavorite(self):
        """收藏视图里批量勾选 -> 批量取消收藏。"""
        jobs = self._api("/api/jobs?status=all")["jobs"]
        ids = [j["id"] for j in jobs[:2]]
        for jid in ids:
            self._api("/api/jobs/%s/favorite" % jid, "POST", {"favorited": True})

        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length > 0",
            timeout=WAIT_TIMEOUT)
        self.page.wait_for_timeout(600)
        self.page.click("#navFavorites")
        self.page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .fav-sel').length > 0",
            timeout=WAIT_TIMEOUT)
        self.page.wait_for_timeout(400)

        # 全选本页
        self.page.click("#favSelectAllBtn")
        self.page.wait_for_timeout(400)
        count_text = self.page.inner_text("#favToolsCount")
        self.assertIn("已选", count_text)

        # 批量取消收藏（confirm 自动接受）
        self.page.once("dialog", lambda d: d.accept())
        self.page.click("#favBatchUnfavBtn")
        # 等「服务端全部取消」—— 固定 sleep 会被后台慢请求拖成假失败。
        self.page.wait_for_function(
            """async (ids) => {
                for (const jid of ids) {
                    const r = await fetch('/api/jobs/' + jid);
                    const d = await r.json();
                    if (d.favorited !== false) return false;
                }
                return true;
            }""", arg=ids, timeout=WAIT_TIMEOUT)

        for jid in ids:
            data = self._api("/api/jobs/" + jid)
            self.assertFalse(data.get("favorited"),
                             "批量取消收藏未生效：%s" % jid)


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
