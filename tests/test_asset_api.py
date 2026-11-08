# -*- coding: utf-8 -*-
"""
资产素材库 HTTP 接口测试。

用真实的 aiohttp 测试服务器跑真实请求 —— 不 mock 掉任何一层。
目的：抓「SQL 列名写错」「路由冲突」这类只有真跑才暴露的问题。

运行：python3 -m pytest tests/test_asset_api.py -v
"""
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from h3studio import asset_api  # noqa: E402
from h3studio import assets as A  # noqa: E402
from h3studio.assets import LibraryError  # noqa: E402


def _make_png(path: Path, width: int = 32, height: int = 24) -> Path:
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), (200, 40, 40)).save(path, "PNG")
    return path


class AssetApiTestBase(unittest.TestCase):
    """起一个真实的 aiohttp 测试服务器跑素材库接口。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="assetlib-api-"))
        cls.root = cls.tmp / "library"
        cls.src = cls.tmp / "src"
        cls.src.mkdir(parents=True, exist_ok=True)
        cls._old_root = os.environ.get("ASSET_LIBRARY_ROOT")
        os.environ["ASSET_LIBRARY_ROOT"] = str(cls.root)
        A.init_db()

        # 注入一个隔离的 server 替身：素材库的 job 相关接口（引用反查、
        # 从媒体库入库）会去拿任务库与产物目录。测试里必须指向**临时**的
        # jobs.db 与空的 outputs 目录 —— 不能碰真库，也不该依赖本机 Provider。
        import types
        fake_server = types.ModuleType("server")
        cls.jobs_db_path = cls.tmp / "jobs.db"

        def _fake_db():
            conn = sqlite3.connect(cls.jobs_db_path)
            conn.row_factory = sqlite3.Row
            return conn

        fake_server.db = _fake_db
        fake_server.OUT_DIR = cls.tmp / "outputs"
        (cls.tmp / "outputs").mkdir(parents=True, exist_ok=True)
        cls._fake_server = fake_server

        cls.app = web.Application()
        asset_api.register(cls.app, server_module=fake_server)
        cls.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(cls.loop)

        # TestClient 的构造需要「正在运行的事件循环」（CookieJar 会去取
        # running loop），所以整个 server + client 都要在同一个 loop 内启动。
        async def boot():
            cls.server = TestServer(cls.app)
            await cls.server.start_server()
            cls.client = TestClient(cls.server)
            await cls.client.start_server()
            cls.base = f"http://127.0.0.1:{cls.server.port}"

        cls.loop.run_until_complete(boot())

    @classmethod
    def tearDownClass(cls):
        cls.loop.run_until_complete(cls.client.close())
        cls.loop.run_until_complete(cls.server.close())
        cls.loop.close()
        if cls._old_root is None:
            os.environ.pop("ASSET_LIBRARY_ROOT", None)
        else:
            os.environ["ASSET_LIBRARY_ROOT"] = cls._old_root
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    def upload(self, path: Path, name=None, tags=None):
        """真实 multipart 上传。"""
        async def go():
            data = aiohttp.FormData()
            data.add_field("file", path.read_bytes(),
                           filename=name or path.name,
                           content_type="application/octet-stream")
            if tags:
                data.add_field("tags", tags)
            resp = await self.client.post("/api/assets", data=data)
            return resp.status, await resp.json()
        return self._run(go())

    def get(self, path):
        async def go():
            resp = await self.client.get(path)
            return resp.status, await resp.json()
        return self._run(go())

    def post_json(self, path, payload):
        async def go():
            resp = await self.client.post(path, json=payload)
            return resp.status, await resp.json()
        return self._run(go())

    def patch_json(self, path, payload):
        async def go():
            resp = await self.client.patch(path, json=payload)
            return resp.status, await resp.json()
        return self._run(go())

    def delete(self, path):
        async def go():
            resp = await self.client.delete(path)
            return resp.status, await resp.json()
        return self._run(go())


class TestAssetApi(AssetApiTestBase):
    """逐个接口真跑一遍。"""

    def _seed_one(self):
        """每次生成一张内容唯一的 PNG。

        必须是**唯一内容** —— 内容相同会命中哈希去重复用既有素材，
        那条路径在别的用例里测，这里不能互相干扰。
        """
        import hashlib
        seed = hashlib.sha256(self.id().encode()).hexdigest()[:8]
        src = self.src / f"api_{seed}.png"
        from PIL import Image
        Image.new("RGB", (32, 24),
                  (int(seed[0:2], 16), int(seed[2:4], 16), int(seed[4:6], 16))
                  ).save(src, "PNG")
        status, data = self.upload(src, tags="接口测试")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["results"], f"上传应产生结果: {data}")
        return data["results"][0]["asset_id"]

    def test_01_list_and_stats_and_tags(self):
        asset_id = self._seed_one()
        status, data = self.get("/api/assets")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(data["total"], 1)
        self.assertIn("assets", data)
        self.assertIn("total", data)
        self.assertIn("page", data)

        status, stats = self.get("/api/assets/stats")
        self.assertEqual(status, 200)
        self.assertIn("total_count", stats)
        self.assertIn("by_category", stats)
        self.assertIn("trash_count", stats)

        status, tags = self.get("/api/assets/tags")
        self.assertEqual(status, 200)
        self.assertTrue(any(t["tag"] == "接口测试" for t in tags["tags"]))

    def test_02_picker_returns_usable_rows(self):
        """回归：picker 的 SQL 曾把 duration_sec 写在 assets 表上（实际在
        asset_versions），导致 500。这里必须真的能返回条目。"""
        self._seed_one()
        status, data = self.get("/api/assets/picker?limit=50")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["assets"], "picker 必须返回素材")
        row = data["assets"][0]
        for key in ("id", "name", "category", "subtype"):
            self.assertIn(key, row)

    def test_03_picker_with_category_filter(self):
        self._seed_one()
        status, data = self.get("/api/assets/picker?category=image&limit=50")
        self.assertEqual(status, 200, data)
        for row in data["assets"]:
            self.assertEqual(row["category"], "image")

    def test_04_detail_and_content_and_preview(self):
        asset_id = self._seed_one()
        status, detail = self.get(f"/api/assets/{asset_id}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["id"], asset_id)
        self.assertIn("versions", detail)
        self.assertIn("references", detail)
        self.assertIn("analyses", detail)

        async def fetch(path):
            resp = await self.client.get(path)
            return resp.status, await resp.read()
        status, body = self._run(fetch(f"/api/assets/{asset_id}/content"))
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"\x89PNG"), "原文件内容应原样返回")

        status, body = self._run(fetch(f"/api/assets/{asset_id}/preview"))
        self.assertEqual(status, 200)
        self.assertGreater(len(body), 0)

    def test_05_update_tags_and_rename(self):
        asset_id = self._seed_one()
        status, data = self.patch_json(f"/api/assets/{asset_id}",
                                       {"name": "改名后的素材", "tags": ["新标签A"]})
        self.assertEqual(status, 200)
        self.assertEqual(data["name"], "改名后的素材")
        self.assertIn("新标签A", data["tags"])

        status, data = self.post_json(f"/api/assets/{asset_id}/tags", {"tags": ["额外标签"]})
        self.assertEqual(status, 200)
        self.assertIn("额外标签", data["tags"])

        status, data = self.get(f"/api/assets/{asset_id}")
        self.assertIn("新标签A", data["tags"])
        self.assertIn("额外标签", data["tags"])

        # 删标签
        status, data = self._run(self._delete_tag(asset_id, "额外标签"))
        self.assertEqual(status, 200)
        self.assertNotIn("额外标签", data["tags"])

        # 全局标签重命名
        status, data = self.post_json("/api/assets/tags/rename",
                                       {"old": "新标签A", "new": "改名标签"})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(data["affected"], 1)

    async def _delete_tag(self, asset_id, tag):
        resp = await self.client.delete(
            f"/api/assets/{asset_id}/tags/{tag}")
        return resp.status, await resp.json()

    def test_06_search_filters(self):
        asset_id = self._seed_one()
        status, data = self.get("/api/assets?tag=" + "接口测试")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(data["total"], 1)

        status, data = self.get("/api/assets?category=image")
        self.assertEqual(status, 200)

        status, data = self.get("/api/assets?q=" + "改名后的素材")
        self.assertEqual(status, 200)

        status, data = self.get("/api/assets?tags_any=" + "接口测试")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(data["total"], 1, "tags_any 多字标签必须能命中")

    def test_07_delete_protected_then_soft_delete_and_restore(self):
        asset_id = self._seed_one()
        # 先制造引用
        with A.connect() as conn:
            from h3studio import asset_repo
            asset_repo.add_ref(conn, asset_id, "job", "job_xyz", slot="ref_image",
                               label="任务 job_xyz 的参考图输入")
        status, data = self.delete(f"/api/assets/{asset_id}")
        self.assertEqual(status, 409, data)
        self.assertEqual(data["code"], "asset_in_use")
        self.assertTrue(data["refs"])

        # 解除引用后可删
        with A.connect() as conn:
            from h3studio import asset_repo
            asset_repo.remove_refs_for(conn, "job", "job_xyz")
        status, data = self.delete(f"/api/assets/{asset_id}")
        self.assertEqual(status, 200, data)
        self.assertIsNotNone(data["deleted_at"])

        # 默认列表不出现回收站的素材
        status, data = self.get("/api/assets")
        self.assertEqual(status, 200)
        self.assertNotIn(asset_id, [a["id"] for a in data["assets"]],
                         "回收站的素材不应出现在默认列表")

        # 回收站视图里能看到
        status, data = self.get("/api/assets?in_trash=1")
        self.assertEqual(status, 200)
        self.assertIn(asset_id, [a["id"] for a in data["assets"]],
                      "回收站视图应包含该素材")

        # 详情仍可读（抽屉里要能看到它才能「还原」）
        status, detail = self.get(f"/api/assets/{asset_id}")
        self.assertEqual(status, 200)
        self.assertIsNotNone(detail["deleted_at"])

        status, data = self.post_json(f"/api/assets/{asset_id}/restore", {})
        self.assertEqual(status, 200)
        status, data = self.get(f"/api/assets/{asset_id}")
        self.assertEqual(status, 200)
        self.assertIsNone(data["deleted_at"])

    def test_08_refs_endpoint(self):
        asset_id = self._seed_one()
        with A.connect() as conn:
            from h3studio import asset_repo
            asset_repo.add_ref(conn, asset_id, "job", "job_ref", slot="first_frame")
        status, data = self.get(f"/api/assets/{asset_id}/refs")
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["refs"][0]["slot"], "first_frame")

    def test_09_reconcile_reports_orphans(self):
        status, data = self.post_json("/api/assets/reconcile", {})
        self.assertEqual(status, 200)
        self.assertIn("orphan_count", data)
        self.assertIn("orphans", data)

    def test_10_analyze_returns_result_or_readable_error(self):
        """分析接口：要么给出分析结果，要么给出可读错误 —— 不能 500。

        本机若配有多模态 Provider，会真的跑一次模型（慢）；
        没配则返回 400 并说明原因。两种都算通过。
        """
        asset_id = self._seed_one()
        status, data = self.post_json(f"/api/assets/{asset_id}/analyze", {})
        self.assertIn(status, (200, 400, 502),
                      f"不应是 5xx 崩溃，实际 {status}: {data}")
        if status == 200:
            self.assertIn("text", data)
            self.assertTrue(data["text"], "分析结果应有文本")
            self.assertEqual(data["revision"], 1)
        else:
            self.assertIn("error", data)
            self.assertTrue(data["error"])

    def test_11_correction_requires_question(self):
        asset_id = self._seed_one()
        status, data = self.post_json(f"/api/assets/{asset_id}/corrections", {})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_12_404_for_unknown_asset(self):
        status, data = self.get("/api/assets/asset_does_not_exist")
        self.assertEqual(status, 404)

    def test_13_from_media_requires_job_id(self):
        status, data = self.post_json("/api/assets/import/from-media", {})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_14_from_media_route_is_not_shadowed(self):
        """回归：POST /api/assets/import/from-media 曾被
        POST /api/assets/{id} 抢走（405）。这里确认路由命中的是导入接口。"""
        status, data = self.post_json("/api/assets/import/from-media", {})
        # 400 = 路由正确（缺 job_id）；405 = 路由被 {id} 抢走
        self.assertNotEqual(status, 405, "路由被 /api/assets/{id} 抢走了")
        self.assertEqual(status, 400)

    def test_15_upload_rejects_unsupported_type(self):
        bad = self.src / "bad.exe"
        bad.write_bytes(b"MZ\x90\x00")
        status, data = self.upload(bad)
        self.assertEqual(status, 200)
        self.assertEqual(data["failed"], 1)
        self.assertTrue(data["errors"])

    def test_16_upload_dedup_reuses_existing(self):
        src = _make_png(self.src / "dedup_api.png")
        status1, data1 = self.upload(src)
        self.assertFalse(data1["results"][0]["duplicated"])
        status2, data2 = self.upload(src)
        self.assertTrue(data2["results"][0]["duplicated"])
        self.assertEqual(data1["results"][0]["asset_id"],
                         data2["results"][0]["asset_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
