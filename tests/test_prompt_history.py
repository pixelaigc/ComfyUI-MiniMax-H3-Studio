# -*- coding: utf-8 -*-
"""提示词历史（Prompt History）的回归护栏。

覆盖 5 条主线：
  1. 写入     —— 生成提交 / 增强流程都进历史，且原始与增强后两份都在
  2. 过滤     —— 按 panel_key / model_key 过滤
  3. 分页     —— limit / offset 正确切片，has_more 正确
  4. 去重上限 —— 同面板同原文合并计数；每面板条数有上限
  5. 跨面板隔离 —— MOSS 的历史不出现在 Z-Image 面板，反之亦然

运行：python3 -m pytest tests/test_prompt_history.py -v
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from h3studio import prompt_history as ph  # noqa: E402
from h3studio import prompt_history_hooks as hooks  # noqa: E402


def _conn():
    """建一个内存库并建表。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ph.ensure_table(conn)
    return conn


class TestPromptHistoryWrite(unittest.TestCase):
    """1. 写入。"""

    def test_record_returns_id_and_stores_both_prompts(self):
        conn = _conn()
        rid = ph.record(conn, "t2i", "一只猫", "一只**毛发蓬松**的猫", model_key="z_image_bf16")
        self.assertGreater(rid, 0)
        items = ph.list_history(conn, panel_key="t2i")
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["original_prompt"], "一只猫")
        self.assertEqual(item["enhanced_prompt"], "一只**毛发蓬松**的猫")
        self.assertEqual(item["model_key"], "z_image_bf16")
        self.assertTrue(item["has_enhanced"])
        self.assertGreater(item["timestamp"], 0)

    def test_empty_prompt_is_not_recorded(self):
        conn = _conn()
        self.assertEqual(ph.record(conn, "t2i", "   "), 0)
        self.assertEqual(ph.record(conn, "", "一只猫"), 0)
        self.assertEqual(ph.count_history(conn), 0)

    def test_enhanced_equal_to_original_counts_as_not_enhanced(self):
        """没真增强（增强结果 == 原文）时，不能算成「有增强版本」。"""
        conn = _conn()
        ph.record(conn, "t2i", "一只猫", "一只猫")
        item = ph.list_history(conn, panel_key="t2i")[0]
        self.assertEqual(item["enhanced_prompt"], "")
        self.assertFalse(item["has_enhanced"])

    def test_generate_source_counts_use_count(self):
        conn = _conn()
        ph.record(conn, "t2i", "一只猫", source=ph.SOURCE_GENERATE)
        ph.record(conn, "t2i", "一只猫", source=ph.SOURCE_GENERATE)
        item = ph.list_history(conn, panel_key="t2i")[0]
        self.assertEqual(item["use_count"], 2)

    def test_enhance_source_does_not_inflate_use_count(self):
        """增强本身不算「使用了一次」—— 否则计数虚高。"""
        conn = _conn()
        ph.record(conn, "t2i", "一只猫", "一只**蓬松**的猫", source=ph.SOURCE_GENERATE)
        ph.record(conn, "t2i", "一只猫", "一只**蓬松**的猫", source=ph.SOURCE_ENHANCE)
        items = ph.list_history(conn, panel_key="t2i")
        self.assertEqual(len(items), 1, "增强不应新增一条记录")
        self.assertEqual(items[0]["use_count"], 1, "增强不应计入 use_count")


class TestPromptHistoryHooks(unittest.TestCase):
    """写入钩子：从提交参数里还原「原始 / 最终」两份。"""

    def test_original_prompt_wins_over_prompt(self):
        conn = _conn()
        hooks.record_prompt_history(conn, "t2i", {
            "original_prompt": "原始的",
            "prompt": "增强后的",
            "model": "z_image_bf16",
        })
        item = ph.list_history(conn, panel_key="t2i")[0]
        self.assertEqual(item["original_prompt"], "原始的")
        self.assertEqual(item["enhanced_prompt"], "增强后的")

    def test_without_original_prompt_degrades_to_same(self):
        """没带 original_prompt -> 原始 == 最终（未增强直出）。"""
        conn = _conn()
        hooks.record_prompt_history(conn, "t2v", {"prompt": "一段视频描述"})
        item = ph.list_history(conn, panel_key="t2v")[0]
        self.assertEqual(item["original_prompt"], "一段视频描述")
        self.assertEqual(item["enhanced_prompt"], "")
        self.assertFalse(item["has_enhanced"])

    def test_model_key_resolution_order(self):
        """model_key 优先于 model，checkpoint 最后。"""
        self.assertEqual(hooks.model_key_for_params({"model_key": "a", "model": "b"}), "a")
        self.assertEqual(hooks.model_key_for_params({"model": "b", "checkpoint": "c"}), "b")
        self.assertEqual(hooks.model_key_for_params({"checkpoint": "c"}), "c")
        self.assertEqual(hooks.model_key_for_params({}), "")

    def test_panel_key_is_mode(self):
        self.assertEqual(hooks.panel_key_for_mode("t2i_moss"), "t2i_moss")
        self.assertEqual(hooks.panel_key_for_mode("t2i"), "t2i")


class TestPromptHistoryFilter(unittest.TestCase):
    """2. 过滤。"""

    def setUp(self):
        self.conn = _conn()
        ph.record(self.conn, "t2i", "z 的提示词", model_key="z_image_bf16")
        ph.record(self.conn, "t2i", "z 的另一个", model_key="z_image_turbo_bf16")
        ph.record(self.conn, "t2i_moss", "moss 的提示词", model_key="moss_base")
        ph.record(self.conn, "t2v", "v 的提示词", model_key="minimax_h3")

    def test_filter_by_panel(self):
        items = ph.list_history(self.conn, panel_key="t2i")
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertEqual(item["panel_key"], "t2i")

    def test_filter_by_model(self):
        items = ph.list_history(self.conn, panel_key="t2i", model_key="z_image_turbo_bf16")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["original_prompt"], "z 的另一个")

    def test_count_respects_filters(self):
        self.assertEqual(ph.count_history(self.conn, panel_key="t2i"), 2)
        self.assertEqual(ph.count_history(self.conn, panel_key="t2i",
                                          model_key="z_image_bf16"), 1)
        self.assertEqual(ph.count_history(self.conn), 4)

    def test_list_all_panels(self):
        self.assertEqual(len(ph.list_history(self.conn)), 4)


class TestPromptHistoryPagination(unittest.TestCase):
    """3. 分页。"""

    def setUp(self):
        self.conn = _conn()
        for i in range(25):
            ph.record(self.conn, "t2i", f"提示词 {i:02d}")

    def test_limit_and_offset_slice(self):
        first = ph.list_history(self.conn, panel_key="t2i", limit=10, offset=0)
        second = ph.list_history(self.conn, panel_key="t2i", limit=10, offset=10)
        self.assertEqual(len(first), 10)
        self.assertEqual(len(second), 10)
        # 两页不能有重叠
        ids_first = {i["id"] for i in first}
        ids_second = {i["id"] for i in second}
        self.assertFalse(ids_first & ids_second)

    def test_limit_is_capped(self):
        """limit 上限 100 —— 不能被请求参数放大成「一次拉全量」。"""
        items = ph.list_history(self.conn, panel_key="t2i", limit=99999)
        self.assertLessEqual(len(items), ph.MAX_LIMIT)

    def test_offset_beyond_end_returns_empty(self):
        items = ph.list_history(self.conn, panel_key="t2i", limit=10, offset=999)
        self.assertEqual(items, [])

    def test_order_is_newest_first(self):
        items = ph.list_history(self.conn, panel_key="t2i", limit=5, offset=0)
        stamps = [i["timestamp"] for i in items]
        self.assertEqual(stamps, sorted(stamps, reverse=True))


class TestPromptHistoryDedupAndCap(unittest.TestCase):
    """4. 去重 + 上限。"""

    def test_same_original_in_same_panel_merges(self):
        conn = _conn()
        ph.record(conn, "t2i", "同一句")
        ph.record(conn, "t2i", "同一句")
        ph.record(conn, "t2i", "同一句")
        self.assertEqual(ph.count_history(conn, panel_key="t2i"), 1)
        item = ph.list_history(conn, panel_key="t2i")[0]
        self.assertEqual(item["use_count"], 3)

    def test_same_original_in_different_panels_stays_separate(self):
        """同一个提示词用在两个面板 -> 两条独立记录（面板隔离）。"""
        conn = _conn()
        ph.record(conn, "t2i", "同一句")
        ph.record(conn, "t2i_moss", "同一句")
        self.assertEqual(ph.count_history(conn), 2)

    def test_unenhanced_submit_keeps_previous_enhanced_value(self):
        """关键回归：「未增强直出」不能把之前那次增强的成果抹掉。"""
        conn = _conn()
        ph.record(conn, "t2i", "同一句", "增强版", source=ph.SOURCE_ENHANCE)
        ph.record(conn, "t2i", "同一句", "", source=ph.SOURCE_GENERATE)
        item = ph.list_history(conn, panel_key="t2i")[0]
        self.assertEqual(item["enhanced_prompt"], "增强版")
        self.assertTrue(item["has_enhanced"])

    def test_new_enhanced_value_overwrites_old(self):
        conn = _conn()
        ph.record(conn, "t2i", "同一句", "增强版 1", source=ph.SOURCE_ENHANCE)
        ph.record(conn, "t2i", "同一句", "增强版 2", source=ph.SOURCE_ENHANCE)
        item = ph.list_history(conn, panel_key="t2i")[0]
        self.assertEqual(item["enhanced_prompt"], "增强版 2")

    def test_per_panel_cap_enforced(self):
        """每面板条数有上限 —— 超出时删最旧的。"""
        conn = _conn()
        limit = ph.MAX_PER_PANEL
        for i in range(limit + 20):
            ph.record(conn, "t2i", f"提示词 {i:04d}")
        self.assertEqual(ph.count_history(conn, panel_key="t2i"), limit)

    def test_cap_keeps_newest(self):
        conn = _conn()
        for i in range(ph.MAX_PER_PANEL + 5):
            ph.record(conn, "t2i", f"提示词 {i:04d}")
        items = ph.list_history(conn, panel_key="t2i", limit=ph.MAX_LIMIT)
        prompts = {i["original_prompt"] for i in items}
        # 最新的那条必须在
        self.assertIn(f"提示词 {ph.MAX_PER_PANEL + 4:04d}", prompts)
        # 最老的那条必须被淘汰
        self.assertNotIn("提示词 0000", prompts)

    def test_cap_is_per_panel(self):
        """上限是**每面板**的 —— 一个面板满了不影响另一个。"""
        conn = _conn()
        for i in range(ph.MAX_PER_PANEL + 3):
            ph.record(conn, "t2i", f"t2i {i}")
        ph.record(conn, "t2i_moss", "moss 的")
        self.assertEqual(ph.count_history(conn, panel_key="t2i"), ph.MAX_PER_PANEL)
        self.assertEqual(ph.count_history(conn, panel_key="t2i_moss"), 1)


class TestPromptHistoryIsolation(unittest.TestCase):
    """5. 跨面板隔离。"""

    def setUp(self):
        self.conn = _conn()
        ph.record(self.conn, "t2i", "z-image 的猫", "z-image 增强的猫")
        ph.record(self.conn, "t2i_moss", "moss 的猫", "moss 增强的猫")
        ph.record(self.conn, "t2i_qwen", "qwen 的猫")
        ph.record(self.conn, "i2i_qwen", "qwen 编辑的猫")
        ph.record(self.conn, "t2v", "视频的猫")
        ph.record(self.conn, "t2a", "音乐的猫")

    def test_moss_history_not_in_z_image(self):
        items = ph.list_history(self.conn, panel_key="t2i")
        for item in items:
            self.assertNotIn("moss", item["original_prompt"])
            self.assertNotIn("moss", item["enhanced_prompt"])

    def test_z_image_history_not_in_moss(self):
        items = ph.list_history(self.conn, panel_key="t2i_moss")
        for item in items:
            self.assertNotIn("z-image", item["original_prompt"])

    def test_qwen_t2i_and_i2i_are_separate(self):
        t2i = ph.list_history(self.conn, panel_key="t2i_qwen")
        i2i = ph.list_history(self.conn, panel_key="i2i_qwen")
        self.assertEqual(len(t2i), 1)
        self.assertEqual(len(i2i), 1)
        self.assertNotEqual(t2i[0]["id"], i2i[0]["id"])

    def test_video_and_music_are_separate(self):
        self.assertEqual(len(ph.list_history(self.conn, panel_key="t2v")), 1)
        self.assertEqual(len(ph.list_history(self.conn, panel_key="t2a")), 1)

    def test_each_panel_sees_only_its_own(self):
        for panel in ("t2i", "t2i_moss", "t2i_qwen", "i2i_qwen", "t2v", "t2a"):
            items = ph.list_history(self.conn, panel_key=panel)
            self.assertEqual(len(items), 1, f"{panel} 条数不对")
            self.assertEqual(items[0]["panel_key"], panel)


class TestPromptHistoryApi(unittest.TestCase):
    """HTTP 接口层：真起一个 aiohttp 测试服务器打真实请求。

    不 mock 任何一层 —— 目的抓「SQL 列名写错」「路由冲突」
    这类只有真跑才暴露的问题。
    """

    @classmethod
    def setUpClass(cls):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        cls.tmp = Path(tempfile.mkdtemp(prefix="prompt-history-api-"))
        cls.db_path = cls.tmp / "jobs.db"

        # 起一个只挂提示词历史路由的最小 app
        import server as server_mod

        cls._orig_db_path = server_mod.DB_PATH
        server_mod.DB_PATH = cls.db_path
        server_mod.init_db()

        app = web.Application()
        app.router.add_get("/api/prompt-history", server_mod.api_prompt_history_list)
        app.router.add_delete("/api/prompt-history", server_mod.api_prompt_history_delete)

        # TestClient 的构造需要「正在运行的事件循环」（CookieJar 会去取
        # running loop），所以 server + client 都要在同一个 loop 内启动。
        cls.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(cls.loop)

        async def boot():
            cls.server = TestServer(app)
            await cls.server.start_server()
            cls.client = TestClient(cls.server)
            await cls.client.start_server()

        cls.loop.run_until_complete(boot())

    @classmethod
    def tearDownClass(cls):
        cls.loop.run_until_complete(cls.client.close())
        cls.loop.run_until_complete(cls.server.close())
        cls.loop.close()
        # 把当前线程的 event loop 还原（否则后面的用例拿到的是已关闭的 loop）
        asyncio.set_event_loop(None)
        import server as server_mod
        server_mod.DB_PATH = cls._orig_db_path

    def _get(self, **params):
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = "/api/prompt-history" + (("?" + query) if query else "")
        resp = self.loop.run_until_complete(self.client.get(url))
        # aiohttp 的 resp.json() 是协程 —— 必须 await，否则拿到的是协程对象。
        body = self.loop.run_until_complete(resp.json())
        return resp.status, body

    def test_endpoint_returns_expected_shape(self):
        status, body = self._get(panel_key="t2i", limit=5, offset=0)
        self.assertEqual(status, 200)
        for key in ("items", "total", "limit", "offset", "has_more"):
            self.assertIn(key, body)
        self.assertEqual(body["limit"], 5)
        self.assertEqual(body["offset"], 0)

    def test_endpoint_filters_by_panel(self):
        # 先写两条不同面板的记录
        import server as server_mod
        with server_mod.db() as conn:
            ph.record(conn, "api_t2i", "api 的 z 提示词")
            ph.record(conn, "api_t2i_moss", "api 的 moss 提示词")

        _, body = self._get(panel_key="api_t2i")
        for item in body["items"]:
            self.assertEqual(item["panel_key"], "api_t2i")
            self.assertNotIn("moss", item["original_prompt"])

    def test_endpoint_items_have_required_fields(self):
        import server as server_mod
        with server_mod.db() as conn:
            ph.record(conn, "api_fields", "原始", "增强", model_key="m1")
        _, body = self._get(panel_key="api_fields")
        item = body["items"][0]
        for key in ("id", "original_prompt", "enhanced_prompt", "timestamp"):
            self.assertIn(key, item)
        self.assertEqual(item["original_prompt"], "原始")
        self.assertEqual(item["enhanced_prompt"], "增强")
        self.assertEqual(item["model_key"], "m1")

    def test_endpoint_pagination_has_more(self):
        import server as server_mod
        with server_mod.db() as conn:
            for i in range(12):
                ph.record(conn, "api_page", f"提示词 {i:02d}")
        _, body = self._get(panel_key="api_page", limit=5, offset=0)
        self.assertEqual(len(body["items"]), 5)
        self.assertTrue(body["has_more"])
        _, body2 = self._get(panel_key="api_page", limit=5, offset=10)
        self.assertEqual(len(body2["items"]), 2)
        self.assertFalse(body2["has_more"])

    def test_endpoint_limit_is_capped(self):
        _, body = self._get(panel_key="api_page", limit=100000)
        self.assertLessEqual(body["limit"], ph.MAX_LIMIT)

    def test_delete_requires_panel_key(self):
        resp = self.loop.run_until_complete(self.client.delete("/api/prompt-history"))
        self.assertEqual(resp.status, 400)

    def test_delete_clears_panel_only(self):
        import server as server_mod
        with server_mod.db() as conn:
            ph.record(conn, "api_del", "要删的")
            ph.record(conn, "api_keep", "要留的")
        resp = self.loop.run_until_complete(
            self.client.delete("/api/prompt-history?panel_key=api_del"))
        self.assertEqual(resp.status, 200)
        _, body = self._get(panel_key="api_keep")
        self.assertTrue(body["items"])


class TestPromptHistoryEndToEnd(unittest.TestCase):
    """端到端：真跑 POST /api/jobs，验证提交真的落进历史。

    这一层是「写入钩子接对了」的护栏 —— 前面那些用例都直接调
    prompt_history.record()，接错了钩子也发现不了。
    """

    @classmethod
    def setUpClass(cls):
        import tempfile as _tf
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        cls.tmp = Path(_tf.mkdtemp(prefix="prompt-history-e2e-"))
        import server as server_mod
        cls._orig_db_path = server_mod.DB_PATH
        server_mod.DB_PATH = cls.tmp / "jobs.db"
        server_mod.init_db()

        app = server_mod.make_app()
        cls.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(cls.loop)

        async def boot():
            cls.server = TestServer(app)
            await cls.server.start_server()
            cls.client = TestClient(cls.server)
            await cls.client.start_server()

        cls.loop.run_until_complete(boot())

    @classmethod
    def tearDownClass(cls):
        cls.loop.run_until_complete(cls.client.close())
        cls.loop.run_until_complete(cls.server.close())
        cls.loop.close()
        asyncio.set_event_loop(None)
        import server as server_mod
        server_mod.DB_PATH = cls._orig_db_path

    def _post_job(self, mode, params):
        """按真实前端的方式提交一个生成任务（multipart/form-data）。"""
        import uuid
        boundary = "----ph" + uuid.uuid4().hex
        body = (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="mode"\r\n\r\n{mode}\r\n'
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="params"\r\n\r\n'
                f"{json.dumps(params, ensure_ascii=False)}\r\n"
                f"--{boundary}--\r\n").encode("utf-8")
        ctype = f"multipart/form-data; boundary={boundary}"

        async def go():
            resp = await self.client.post("/api/jobs", data=body,
                                          headers={"Content-Type": ctype})
            return resp.status, await resp.json()
        return self.loop.run_until_complete(go())

    def _history(self, panel_key):
        async def go():
            resp = await self.client.get(f"/api/prompt-history?panel_key={panel_key}")
            return await resp.json()
        return self.loop.run_until_complete(go())

    def test_submit_records_original_and_final_prompt(self):
        status, body = self._post_job("t2i", {
            "prompt": "增强后的最终提示词",
            "original_prompt": "用户写的原始提示词",
            "model": "z_image_bf16",
            "resolution": "1mp", "aspect": "1:1", "steps": 8,
        })
        self.assertEqual(status, 200, body)
        items = self._history("t2i")["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["original_prompt"], "用户写的原始提示词")
        self.assertEqual(items[0]["enhanced_prompt"], "增强后的最终提示词")
        self.assertTrue(items[0]["has_enhanced"])

    def test_submit_without_original_is_recorded_as_not_enhanced(self):
        """没带 original_prompt（未增强直出）-> 原始 == 最终，无增强版本。"""
        self._post_job("t2i", {"prompt": "没走增强的提示词", "model": "z_image_turbo_bf16"})
        items = self._history("t2i")["items"]
        hits = [i for i in items if i["original_prompt"] == "没走增强的提示词"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["enhanced_prompt"], "")
        self.assertFalse(hits[0]["has_enhanced"])

    def test_submissions_are_isolated_per_panel(self):
        self._post_job("t2i_moss", {"prompt": "moss 提示词",
                                    "original_prompt": "moss 原始",
                                    "model": "moss_base"})
        moss = self._history("t2i_moss")["items"]
        self.assertEqual(len(moss), 1)
        self.assertEqual(moss[0]["original_prompt"], "moss 原始")
        # t2i 面板里不能出现 moss 的记录
        for item in self._history("t2i")["items"]:
            self.assertNotIn("moss", item["original_prompt"])


class TestPromptHistoryPersistence(unittest.TestCase):
    """持久化：不是前端内存、不是 localStorage —— 必须落在 SQLite 文件里。"""

    def test_history_survives_reconnect(self):
        tmp = Path(tempfile.mkdtemp(prefix="prompt-history-db-"))
        path = tmp / "jobs.db"

        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        ph.ensure_table(conn)
        ph.record(conn, "t2i", "落盘的提示词", "落盘的增强")
        conn.commit()
        conn.close()

        conn2 = sqlite3.connect(str(path))
        conn2.row_factory = sqlite3.Row
        items = ph.list_history(conn2, panel_key="t2i")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["original_prompt"], "落盘的提示词")
        self.assertEqual(items[0]["enhanced_prompt"], "落盘的增强")
        conn2.close()

    def test_table_is_created_with_expected_columns(self):
        conn = _conn()
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(prompt_history)").fetchall()}
        for name in ("id", "panel_key", "model_key", "original_prompt",
                     "enhanced_prompt", "source", "created_at", "use_count"):
            self.assertIn(name, cols)


if __name__ == "__main__":
    unittest.main()
