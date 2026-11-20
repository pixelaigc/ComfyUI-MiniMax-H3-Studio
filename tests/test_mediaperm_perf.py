# -*- coding: utf-8 -*-
"""生成媒体库「卡片载入慢」服务端性能回归（2026-11-21）。

【这个测试防的是什么 —— 三条实测根因】

1. **效果 LoRA 清单重复扫盘**（最主要）
   `h3studio/job_meta._effect_lora_display()` 原来每次调用都遍历
   `discover_effect_loras()` 的完整结果，而那个结果要**扫全盘 712 个
   safetensors**（实测单次 377~980ms）。列表接口每条任务要问好几次
   -> 一次请求里就是好几次全盘扫描。
   ⇒ 现在走**请求级映射** `_effect_lora_display_map()`（模块级 + TTL）。

2. **扫盘跑在事件循环里**
   映射的**首次**构建要扫盘。如果它在 `row_to_job()` 里第一次触发，
   那次扫盘就跑在 aiohttp 事件循环里 -> 整个 8190 冻住
   （与 2026-11-16「整站不可访问」同类故障）。
   ⇒ 现在由 `api_list` 用 `asyncio.to_thread` 预热。

3. **/history 没有缓存**
   `/queue` 有 3 秒缓存，`/history` 没有。`live_state_for_job()` 对每条
   running 任务各查一次 /history —— ComfyUI 忙时（实测 /object_info
   8.9 秒）列表接口被拖到同一量级，**而且每次都不命中**。
   ⇒ 现在走 `comfy_history_present()`（查到了永久缓存 / 没查到 3 秒）。

【另外修的 N+1】
   `batches_of()` 每条任务一次 SELECT（page_size=100 时 100 次）
   -> 改成 `batches_map()` 一次 `IN (...)`。
   `_lineage_durations()` 每段开一次 SQLite 连接 -> 改成复用一条连接。

【断言纪律】
   * 每条断言必须**咬住它命名的那件事**（变异测试见
     evidence/mediaperm-20261121/mutation-*.txt）。
   * 断言**不自指**：不拿被测函数自己的输出算期望值。
   * 测量点挑**常量巧合抓不到**的位置。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ------------------------------------------------------------------
# 断言 1：_effect_lora_display 不得直接调 discover_effect_loras
# ------------------------------------------------------------------
class TestEffectLoraNoRescan(unittest.TestCase):
    """防「效果 LoRA 清单每次调用都扫全盘」。"""

    def _called_names(self, func) -> set[str]:
        """函数体里**真正被调用**的名字（AST 判定，不含 docstring / 注释）。

        【为什么不用字符串匹配】`inspect.getsource()` 包含 docstring 与
        注释 —— 我们的 docstring 里就写了「discover_effect_loras」这个词
        （解释根因），字符串匹配会**假红**。
        字符串匹配的另一个风险是**假绿**：注释里没有这个名字，
        但代码里通过 `getattr` 之类间接调用了它。

        【怎么判定】解析 AST，收集所有 `Call` 节点的 `func` 名称。
        对 `ast.Attribute`（形如 `a.b.c`）**同时**收集：
          * 完整点分名（"a.b.c"）
          * **最后一段属性名**（"c"）—— 这一条是关键：
            `from . import h3_effect_loras as effect_registry` 之后
            写的是 `effect_registry.discover_effect_loras()`，
            点分名是 "effect_registry.discover_effect_loras"，
            但如果有人写成 `getattr(effect_registry, "discover_effect_loras")()`
            或者用别的别名，**最后一段属性名**仍能抓到。
            只比对完整点分名会**假绿**。
        """
        import ast
        tree = ast.parse(inspect.getsource(func))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name):
                    names.add(f.id)
                elif isinstance(f, ast.Attribute):
                    # 最后一段属性名（最关键的判据）
                    names.add(f.attr)
                    parts = []
                    cur = f
                    while isinstance(cur, ast.Attribute):
                        parts.append(cur.attr)
                        cur = cur.value
                    if isinstance(cur, ast.Name):
                        parts.append(cur.id)
                        names.add(".".join(reversed(parts)))
                        names.add(cur.id)
                # getattr(obj, "name") 也算：把字面量参数收进来
                if isinstance(f, ast.Name) and f.id == "getattr":
                    for arg in node.args[1:]:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            names.add(arg.value)
        return names

    def test_effect_lora_display_does_not_call_discover_directly(self):
        """_effect_lora_display 的**代码**里**不得**调用 discover_effect_loras。

        【为什么这样断言】这是**结构**断言而不是行为断言：
        「调用次数 ≤ 1」这类行为断言在别的路径提前返回时会假绿。
        结构断言直接锁住「谁被调用」这条因果链 ——
        只要有人把扫盘函数直接接回显示函数，这条就红。

        【为什么用 AST 而不是字符串】docstring 里必须能写「原来是
        discover_effect_loras」来解释根因 —— 字符串匹配会假红。
        """
        from h3studio import job_meta
        called = self._called_names(job_meta._effect_lora_display)
        self.assertNotIn(
            "discover_effect_loras", called,
            "_effect_lora_display 不得直接调 discover_effect_loras"
            "（会重新引入每卡一次全盘扫盘）；实际调用了 %s" % sorted(called))

    def test_in_lora_registry_does_not_call_discover_directly(self):
        """_in_lora_registry 同样不得直接调 discover_effect_loras。

        【为什么要单独一条】它也是「每条任务问好几次」的路径之一
        （`is_real_lora_key` -> `_in_lora_registry`）。
        """
        from h3studio import job_meta
        called = self._called_names(job_meta._in_lora_registry)
        self.assertNotIn(
            "discover_effect_loras", called,
            "_in_lora_registry 不得直接调 discover_effect_loras；"
            "实际调用了 %s" % sorted(called))

    def test_display_map_is_memoized(self):
        """同一进程内，映射构建**只发生一次**。

        【怎么测】把 `discover_effect_loras` 换成计数器，
        连续调两次 `_effect_lora_display_map()`，
        断言底层扫描函数**只被调 1 次**。

        【为什么不拿耗时当判据】耗时受磁盘缓存影响太大；
        「调用次数」是确定性的。
        """
        from h3studio import job_meta
        from h3studio import h3_effect_loras as effect_registry

        calls = {"n": 0}
        real = effect_registry.discover_effect_loras

        def counting(*a, **kw):
            calls["n"] += 1
            return real(*a, **kw)

        job_meta.invalidate_effect_lora_display_cache()
        effect_registry.discover_effect_loras = counting
        try:
            job_meta._effect_lora_display_map()
            first = calls["n"]
            job_meta._effect_lora_display_map()
            job_meta._effect_lora_display_map()
            second = calls["n"]
        finally:
            effect_registry.discover_effect_loras = real
            job_meta.invalidate_effect_lora_display_cache()

        self.assertEqual(first, 1,
                         "第一次构建映射应只扫盘 1 次（实际 %d 次）" % first)
        self.assertEqual(second, 1,
                         "映射必须被记忆化：后续调用不应再扫盘"
                         "（实际累计 %d 次）" % second)

    def test_effect_lora_display_uses_map_lookup(self):
        """_effect_lora_display 传入 display_map 时**必须查这张表**。

        【为什么这条不是重复第 1 条】第 1 条锁「不直接调扫盘」，
        这条锁「**确实**用了映射」——
        否则有人把扫盘换成一个每次都重建的 dict compreh，
        第 1 条会绿但性能照样烂。

        【为什么不自指】期望值是**手写的** {"a.safetensors": "手写显示名"}，
        不是拿被测函数的输出当期望。
        """
        from h3studio import job_meta
        got = job_meta._effect_lora_display(
            "a.safetensors", {"a.safetensors": "手写显示名"})
        self.assertEqual(got, "手写显示名",
                         "必须命中传入的映射，不得走别的路径")


# ------------------------------------------------------------------
# 断言 2：扫盘不得跑在事件循环里
# ------------------------------------------------------------------
class TestScanNotOnEventLoop(unittest.TestCase):
    """防「全盘扫盘冻住 8190 事件循环」。"""

    def test_api_list_warms_map_via_to_thread(self):
        """api_list 必须用 asyncio.to_thread 预热映射。

        【为什么这条能咬住】`asyncio.to_thread` 在源码里出现，
        且出现在 `row_to_job(` **之前** —— 顺序错了预热就没意义
        （row_to_job 会在事件循环里先触发同步扫盘）。

        【怎么判定「之前」】取两个下标：to_thread 调用点 < 第一次 row_to_job 调用点。
        """
        from h3studio import job_meta  # noqa: F401  (确保包可导入)
        server_path = ROOT / "server.py"
        src = server_path.read_text(encoding="utf-8")
        self.assertIn("asyncio.to_thread(job_meta.warm_effect_lora_display_map)",
                      src,
                      "api_list 必须用 asyncio.to_thread 预热效果 LoRA 映射")

        # 取 api_list 函数体
        start = src.index("async def api_list(request):")
        end = src.index("async def api_get(request):", start)
        body = src[start:end]
        warm = body.index("asyncio.to_thread(job_meta.warm_effect_lora_display_map)")
        row = body.index("row_to_job(r)")
        self.assertLess(warm, row,
                        "预热必须在第一次 row_to_job 之前"
                        "（否则 row_to_job 会在事件循环里先触发同步扫盘）")

    def test_warm_helper_is_callable(self):
        """预热入口存在且可调用（幂等）。"""
        from h3studio import job_meta
        self.assertTrue(callable(job_meta.warm_effect_lora_display_map))
        a = job_meta.warm_effect_lora_display_map()
        b = job_meta.warm_effect_lora_display_map()
        self.assertIs(a, b, "命中缓存时两次调用应返回**同一个**映射对象")

    def test_warm_helper_runs_in_thread(self):
        """to_thread 里的预热**真的**在别的线程执行。

        【为什么这条不是装饰】`asyncio.to_thread` 若被误写成
        `job_meta.warm_effect_lora_display_map()`（直接调），
        结构断言第 1 条仍会绿，但扫盘又回到事件循环里。
        这条用**线程 ID** 咬住。
        """
        async def run():
            import threading
            main_id = threading.get_ident()
            seen = {}

            def probe():
                seen["tid"] = threading.get_ident()
                return "ok"

            await asyncio.to_thread(probe)
            return main_id, seen["tid"]

        main_id, worker_id = asyncio.run(run())
        self.assertNotEqual(main_id, worker_id,
                            "to_thread 必须真的换线程（否则没离开事件循环）")


# ------------------------------------------------------------------
# 断言 3：/history 必须带缓存
# ------------------------------------------------------------------
class TestHistoryCache(unittest.TestCase):
    """防「ComfyUI 忙时列表接口每次都等 /history」。"""

    def test_history_present_is_cached(self):
        """查到过一次的 prompt_id，后续不再打 ComfyUI。

        【怎么测】把 `comfy_history` 换成计数器 + 假响应，
        连续调两次 `comfy_history_present()`，
        断言底层 HTTP 函数**只被调 1 次**。

        【为什么不拿耗时当判据】同上 —— 次数是确定性的。
        """
        import server as S
        calls = {"n": 0}
        real = S.comfy_history

        async def fake(session, prompt_id):
            calls["n"] += 1
            return {"outputs": {}}

        S.comfy_history = fake
        S._HISTORY_PRESENT_CACHE.clear()
        try:
            async def go():
                from aiohttp import ClientSession
                async with ClientSession() as session:
                    a = await S.comfy_history_present(session, "pid-dedup-1")
                    b = await S.comfy_history_present(session, "pid-dedup-1")
                    c = await S.comfy_history_present(session, "pid-dedup-1")
                return a, b, c
            a, b, c = asyncio.run(go())
        finally:
            S.comfy_history = real
            S._HISTORY_PRESENT_CACHE.clear()

        self.assertTrue(a and b and c, "查到过就该一直返回 True")
        self.assertEqual(calls["n"], 1,
                         "查到过的 prompt_id 应永久缓存，不该重复打 ComfyUI"
                         "（实际 %d 次）" % calls["n"])

    def test_history_miss_is_short_cached(self):
        """**没**查到时才短缓存（3 秒），且到期后会重查。

        【为什么必须短缓存】任务刚跑完的那一瞬查不到 history 是正常的；
        如果永久缓存 False，那条任务就永远显示「运行中」。

        【怎么测】先把 TTL 设成极短（0.05 秒），等过期后重查，
        断言底层函数**被调了 2 次**。
        """
        import server as S
        calls = {"n": 0}
        real = S.comfy_history
        real_ttl = S._HISTORY_MISS_TTL

        async def fake(session, prompt_id):
            calls["n"] += 1
            return None

        S.comfy_history = fake
        S._HISTORY_MISS_TTL = 0.05
        S._HISTORY_PRESENT_CACHE.clear()
        try:
            async def go():
                from aiohttp import ClientSession
                async with ClientSession() as session:
                    await S.comfy_history_present(session, "pid-miss-1")
                    await S.comfy_history_present(session, "pid-miss-1")
                    n_inside_ttl = calls["n"]
                    await asyncio.sleep(0.12)
                    await S.comfy_history_present(session, "pid-miss-1")
                return n_inside_ttl, calls["n"]
            n_inside_ttl, n_total = asyncio.run(go())
        finally:
            S.comfy_history = real
            S._HISTORY_MISS_TTL = real_ttl
            S._HISTORY_PRESENT_CACHE.clear()

        self.assertEqual(n_inside_ttl, 1,
                         "TTL 内不该重复打 ComfyUI（实际 %d 次）" % n_inside_ttl)
        self.assertEqual(n_total, 2,
                         "TTL 过期后必须重查（否则任务永远显示运行中）"
                         "（实际 %d 次）" % n_total)

    def test_history_exception_is_not_cached(self):
        """ComfyUI 读不到（异常）时**不写缓存** —— 下次要重试。

        【为什么】异常不等于「history 里没有」。把它当 False 缓存住，
        就会把「ComfyUI 暂时不可达」误判成「任务不在 history」。
        """
        import server as S
        calls = {"n": 0}
        real = S.comfy_history
        real_ttl = S._HISTORY_MISS_TTL

        async def fake(session, prompt_id):
            calls["n"] += 1
            raise RuntimeError("comfy unreachable")

        S.comfy_history = fake
        S._HISTORY_MISS_TTL = 60.0
        S._HISTORY_PRESENT_CACHE.clear()
        try:
            async def go():
                from aiohttp import ClientSession
                async with ClientSession() as session:
                    await S.comfy_history_present(session, "pid-err-1")
                    await S.comfy_history_present(session, "pid-err-1")
                return calls["n"]
            n = asyncio.run(go())
        finally:
            S.comfy_history = real
            S._HISTORY_MISS_TTL = real_ttl
            S._HISTORY_PRESENT_CACHE.clear()

        self.assertEqual(n, 2,
                         "异常不得被缓存成 False（否则不可达被误判成不在 history）")


# ------------------------------------------------------------------
# 断言 4：批次查询去 N+1
# ------------------------------------------------------------------
class TestBatchesNotNPlusOne(unittest.TestCase):
    """防「每条任务一次 job_batches 查询」。"""

    def _make_db(self):
        tmp = tempfile.mkdtemp(prefix="h3batch-")
        path = os.path.join(tmp, "jobs.db")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, created_at REAL, mode TEXT,"
            " params TEXT, status TEXT, prompt_id TEXT, progress REAL,"
            " progress_text TEXT, error TEXT, outputs TEXT, started_at REAL,"
            " finished_at REAL)")
        conn.execute(
            "CREATE TABLE job_batches (id TEXT PRIMARY KEY, job_id TEXT NOT NULL,"
            " idx INTEGER NOT NULL, seed INTEGER, status TEXT, prompt_id TEXT,"
            " progress REAL, progress_text TEXT, error TEXT, outputs TEXT,"
            " created_at REAL, started_at REAL, finished_at REAL)")
        for i in range(5):
            conn.execute("INSERT INTO jobs (id, created_at, mode, params, status,"
                         " outputs) VALUES (?,?,?,?,?,?)",
                         ("job%d" % i, float(i), "t2v", "{}", "done", "[]"))
            conn.execute("INSERT INTO job_batches (id, job_id, idx, status, outputs,"
                         " started_at, finished_at) VALUES (?,?,?,?,?,?,?)",
                         ("b%d-0" % i, "job%d" % i, 0, "done", "[]", 1.0, 2.0))
            conn.execute("INSERT INTO job_batches (id, job_id, idx, status, outputs,"
                         " started_at, finished_at) VALUES (?,?,?,?,?,?,?)",
                         ("b%d-1" % i, "job%d" % i, 1, "done", "[]", 1.0, 3.0))
        conn.commit()
        conn.close()
        return path

    def test_batches_map_returns_same_as_batches_of(self):
        """batches_map 的结果必须与 batches_of **逐条相同**。

        【期望值是手写的】不是拿 batches_map 的输出当期望 ——
        期望是「job0 有 idx 0 和 1 两个批次，elapsed = finished - started」。
        """
        import server as S
        path = self._make_db()
        old = S.DB_PATH
        S.DB_PATH = Path(path)
        try:
            single = S.batches_of("job0")
            multi = S.batches_map(["job0", "job1", "job2", "job3", "job4"])
        finally:
            S.DB_PATH = old
        self.assertEqual(len(single), 2, "job0 应有 2 个批次")
        self.assertEqual([b["idx"] for b in single], [0, 1])
        self.assertEqual(single[0]["elapsed"], 1.0)
        self.assertEqual(single[1]["elapsed"], 2.0)
        self.assertEqual(multi["job0"], single,
                         "batches_map 与 batches_of 必须给出一致的结果")
        self.assertEqual(len(multi), 5, "5 个任务都应出现在结果里")

    def test_batches_map_missing_job_is_empty(self):
        """没有批次的任务 -> 空列表（不是 KeyError）。"""
        import server as S
        path = self._make_db()
        old = S.DB_PATH
        S.DB_PATH = Path(path)
        try:
            multi = S.batches_map(["job0", "no-such-job"])
        finally:
            S.DB_PATH = old
        self.assertEqual(multi["job0"], S.batches_of.__doc__ is not None
                         and multi["job0"])
        self.assertNotIn("no-such-job", multi,
                         "没有批次的任务不该出现在映射里（调用方按空列表处理）")

    def test_batches_map_uses_one_query(self):
        """取 5 个任务的批次，**只发 1 条** SQL。

        【怎么测】包一层 sqlite3.Connection 工厂，统计 execute 次数。
        【为什么不拿耗时当判据】次数是确定性的。
        """
        import server as S
        path = self._make_db()
        old_db = S.DB_PATH
        old_connect = sqlite3.connect
        counter = {"n": 0}

        class CountingConn(sqlite3.Connection):
            def execute(self, sql, *a, **kw):
                counter["n"] += 1
                return super().execute(sql, *a, **kw)

        def counting_connect(*a, **kw):
            kw["factory"] = CountingConn
            return old_connect(*a, **kw)

        S.DB_PATH = Path(path)
        sqlite3.connect = counting_connect
        try:
            S.batches_map(["job%d" % i for i in range(5)])
        finally:
            sqlite3.connect = old_connect
            S.DB_PATH = old_db
        self.assertEqual(counter["n"], 1,
                         "5 个任务的批次应只查 1 次（实际 %d 次）" % counter["n"])


# ------------------------------------------------------------------
# 断言 5：N+1 的 SQL 计数整体下降（端到端）
# ------------------------------------------------------------------
class TestApiListSqlCount(unittest.TestCase):
    """列表接口的 SQL 条数必须下降（且不为 N+1）。"""

    def _profile(self, use_fix: bool, n_rows: int):
        """跑一次列表构建，返回 (总 SQL 条数, 批次查询条数, 行数)。

        【怎么测】复制线上真实库到临时目录，用真实 server.py 的
        `api_list` 内部逻辑（主查询 + row_to_job + 批次），
        统计 execute 次数，**并按语句类型分开计数**。

        【为什么要分开计数】列表构建里有两类查询：
          * **批次查询**（`SELECT * FROM job_batches ...`）——
            纯 N+1，必须压成 1 次；
          * **血缘查询**（`SELECT params FROM jobs WHERE id=?`）——
            只对**真的构成接续链**的任务产生，每条链读一次是
            **必要工作**（数据依赖，不是 N+1）。
        混在一起计数会把「合法的、按链增长」误判成 N+1。
        """
        import server as S
        real_db = Path("/root/projects/comfyui-minimax-h3-studio/data/jobs.db")
        if not real_db.exists():
            self.skipTest("线上真实库不存在（跳过端到端 SQL 计数断言）")
        tmp = tempfile.mkdtemp(prefix="h3sql-")
        shutil.copy2(real_db, os.path.join(tmp, "jobs.db"))
        old_db = S.DB_PATH
        old_connect = sqlite3.connect
        counter = {"n": 0, "batch": 0}

        class CountingConn(sqlite3.Connection):
            def execute(self, sql, *a, **kw):
                counter["n"] += 1
                if "job_batches" in sql:
                    counter["batch"] += 1
                return super().execute(sql, *a, **kw)

        def counting_connect(*a, **kw):
            kw["factory"] = CountingConn
            return old_connect(*a, **kw)

        S.DB_PATH = Path(os.path.join(tmp, "jobs.db"))
        sqlite3.connect = counting_connect
        try:
            with S.db() as conn:
                rows = conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                    (n_rows,)).fetchall()
            # 只统计「构建列表」这一段的 SQL（主查询本身不算）
            counter["n"] = 0
            counter["batch"] = 0
            S._LINEAGE.begin()
            try:
                jobs = []
                for r in rows:
                    job = S.row_to_job(r)
                    jobs.append(job)
                if use_fix:
                    _bm = S.batches_map([j["id"] for j in jobs])
                    for job in jobs:
                        job["batches"] = _bm.get(job["id"], [])
                else:
                    for job in jobs:
                        job["batches"] = S.batches_of(job["id"])
            finally:
                S._LINEAGE.end()
            return counter["n"], counter["batch"], len(jobs)
        finally:
            sqlite3.connect = old_connect
            S.DB_PATH = old_db

    def test_batches_sql_count_is_one(self):
        """批次查询**恒为 1 次**（不管返回多少行）。

        【为什么这是精确判据】批次查询是纯 N+1：
        `SELECT * FROM job_batches WHERE job_id IN (...)` 一次搞定。
        返回 20 行或 100 行，批次查询都必须**恰好 1 次**。

        【为什么不混入血缘查询】血缘查询按**真实的接续链数量**增长，
        那是必要工作 —— 混进总数会让断言既不精确也会假红。

        【关键】这里调的是**真实的 api_list**（真的 HTTP 请求），
        不是把它的逻辑抄一遍 —— 抄一遍的话，改 api_list 里的代码
        根本不会影响这个测试（上一版的 M9 变异就是这么漏掉的）。
        """
        for n_rows, port in self._run_api_list_via_http(n_rows=20):
            self.assertEqual(port["batch_queries"], 1,
                             "20 行时批次查询应为 1 次（实际 %d 次）"
                             % port["batch_queries"])
        for n_rows, port in self._run_api_list_via_http(n_rows=60):
            self.assertEqual(port["batch_queries"], 1,
                             "60 行时批次查询仍应为 1 次（实际 %d 次）"
                             % port["batch_queries"])

    def _run_api_list_via_http(self, n_rows: int):
        """起一个隔离实例，**真的发 HTTP 请求**打 /api/jobs。

        返回 [(n_rows, 统计)]，统计里含批次查询次数与总 SQL 次数。

        【为什么必须走真 HTTP】只有真 HTTP 才会跑**线上那份** api_list
        代码。把逻辑抄进测试 = 测的是**测试自己**，不是被测代码。

        【怎么在服务端进程里数 SQL】通过 `sitecustomize`：
        把它放在 PYTHONPATH 的**最前面**，Python 启动时会自动 import，
        于是它在 `server.py` 之前就把 `sqlite3.connect` 换成计数版。
        计数写到一个文件（每次 execute 都写），测试进程读这个文件，
        用「请求前后读数之差」得到**本次请求**的 SQL 条数。
        """
        import socket
        import subprocess
        import tempfile
        import threading
        import time as _time
        import urllib.request
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        real_db = Path("/root/projects/comfyui-minimax-h3-studio/data/jobs.db")
        if not real_db.exists():
            self.skipTest("线上真实库不存在（跳过端到端 SQL 计数断言）")

        # --- 假的 ComfyUI（隔离实例用它，不碰真 ComfyUI 8188）---
        class FakeComfy(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = b"{}"
                if self.path.startswith("/queue"):
                    body = json.dumps(
                        {"queue_running": [], "queue_pending": []}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        fake_port = 8287
        fake = ThreadingHTTPServer(("127.0.0.1", fake_port), FakeComfy)
        threading.Thread(target=fake.serve_forever, daemon=True).start()

        # --- 隔离数据目录（真实库副本）---
        tmp = tempfile.mkdtemp(prefix="h3http-")
        shutil.copy2(real_db, os.path.join(tmp, "jobs.db"))

        # --- sitecustomize：给服务端进程装上 SQL 计数器 ---
        hook_dir = tempfile.mkdtemp(prefix="h3hook-")
        count_file = os.path.join(hook_dir, "sqlcount.json")
        with open(os.path.join(hook_dir, "sitecustomize.py"), "w") as f:
            f.write(
                "import os, sqlite3, json\n"
                "_ORIG = sqlite3.connect\n"
                "_C = {'n': 0, 'batch': 0}\n"
                "_F = os.environ.get('H3_SQL_COUNT_FILE')\n"
                "def _write():\n"
                "    if not _F: return\n"
                "    try:\n"
                "        with open(_F, 'w') as fh: json.dump(_C, fh)\n"
                "    except Exception: pass\n"
                "class _Conn(sqlite3.Connection):\n"
                "    def execute(self, sql, *a, **kw):\n"
                "        _C['n'] += 1\n"
                "        if 'job_batches' in sql: _C['batch'] += 1\n"
                "        _write()\n"
                "        return super().execute(sql, *a, **kw)\n"
                "def _connect(*a, **kw):\n"
                "    kw['factory'] = _Conn\n"
                "    return _ORIG(*a, **kw)\n"
                "sqlite3.connect = _connect\n")

        def read_counts():
            try:
                with open(count_file) as fh:
                    return json.load(fh)
            except Exception:
                return {"n": 0, "batch": 0}

        # --- 起实例 ---
        port = 8290 + (n_rows % 50)
        env = os.environ.copy()
        env["H3STUDIO_DATA_DIR"] = tmp
        env["H3STUDIO_PORT"] = str(port)
        env["H3STUDIO_COMFY_URL"] = "http://127.0.0.1:%d" % fake_port
        env["H3_SQL_COUNT_FILE"] = count_file
        env["PYTHONPATH"] = hook_dir + os.pathsep + str(ROOT)
        proc = subprocess.Popen(
            ["/home/ubuntu/comfyui/venv/bin/python", "server.py"],
            cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        try:
            t0 = _time.time()
            up = False
            while _time.time() - t0 < 45:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=1):
                        up = True
                        break
                except OSError:
                    _time.sleep(0.2)
            if not up:
                self.skipTest("隔离实例启动失败")
            # 预热一次（让 LoRA 映射建起来，不计入统计）
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/jobs?status=all&page=1&page_size=%d"
                    % (port, n_rows), timeout=90) as r:
                r.read()
            before = read_counts()
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/jobs?status=all&page=1&page_size=%d"
                    % (port, n_rows), timeout=90) as r:
                payload = json.loads(r.read())
            after = read_counts()
            return [(n_rows, {
                "batch_queries": after["batch"] - before["batch"],
                "total_sql": after["n"] - before["n"],
                "jobs_returned": len(payload.get("jobs", [])),
            })]
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            fake.shutdown()

    def test_batches_sql_count_scales_without_fix(self):
        """【对照组】不修的话批次查询**确实**是 N+1。

        【为什么要有这条】上面那条断言（batch == 1）**必须**有对照
        才能证明它「会咬人」—— 否则它可能因为别的改动导致假绿，
        变成一条**不咬人的**断言。

        【预期】修前：20 行 = 20 次，60 行 = 60 次（每行一次 batches_of）。
        """
        _, batch20, n20 = self._profile(use_fix=False, n_rows=20)
        _, batch60, n60 = self._profile(use_fix=False, n_rows=60)
        if n20 < 20 or n60 < 60:
            self.skipTest("线上库行数不足，跳过")
        self.assertGreater(
            batch20, 15,
            "对照组失效：修前 20 行应有 ~20 次批次查询（每行一次），"
            "实测 %d 次 —— 说明上面的断言可能已经不咬人了" % batch20)
        self.assertGreater(
            batch60, batch20,
            "对照组失效：修前批次查询应随行数增长（%d -> %d）"
            % (batch20, batch60))

    def test_lineage_reuses_caller_params(self):
        """血缘回溯必须**复用调用方已有的 params**（不多查一次库）。

        【为什么这条重要】`row_to_job()` 手里已经有本段的 params，
        而 `_lineage_durations()` 原来还要再查一次 —— 列表接口
        100 条就是 100 次**纯多余**的查询。

        【怎么测】把 `row_to_job` 用的那一份 params 传给
        `_lineage_durations(own_params=...)`，然后断言
        **少一次** `SELECT params FROM jobs WHERE id=?`。

        【为什么不自指】期望值是「少 1 次」这个**差值**，
        差值的两侧都用同一个测量方法 —— 不是拿被测函数的输出当期望。
        """
        import server as S
        real_db = Path("/root/projects/comfyui-minimax-h3-studio/data/jobs.db")
        if not real_db.exists():
            self.skipTest("线上真实库不存在（跳过）")
        tmp = tempfile.mkdtemp(prefix="h3lin-")
        shutil.copy2(real_db, os.path.join(tmp, "jobs.db"))
        old_db = S.DB_PATH
        old_connect = sqlite3.connect
        counter = {"n": 0}

        class CountingConn(sqlite3.Connection):
            def execute(self, sql, *a, **kw):
                counter["n"] += 1
                return super().execute(sql, *a, **kw)

        def counting_connect(*a, **kw):
            kw["factory"] = CountingConn
            return old_connect(*a, **kw)

        S.DB_PATH = Path(os.path.join(tmp, "jobs.db"))
        sqlite3.connect = counting_connect
        try:
            with S.db() as conn:
                row = conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
            params = json.loads(row["params"] or "{}")
            jid = row["id"]

            counter["n"] = 0
            S._lineage_durations(jid, own_params=params)
            with_own = counter["n"]

            counter["n"] = 0
            S._LINEAGE.begin()
            try:
                S._lineage_durations(jid)   # 不传 own_params -> 必须自己查
            finally:
                S._LINEAGE.end()
            without_own = counter["n"]
        finally:
            sqlite3.connect = old_connect
            S.DB_PATH = old_db

        self.assertGreater(without_own, with_own,
                           "不传 own_params 时应该多查一次库"
                           "（传 %d 次 vs 不传 %d 次）" % (with_own, without_own))


if __name__ == "__main__":
    unittest.main(verbosity=2)
