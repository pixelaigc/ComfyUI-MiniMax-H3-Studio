# -*- coding: utf-8 -*-
"""
A2：素材库「添加到播放器」清单 + 播放器前端逻辑 —— 回归测试。

覆盖三条主线（每条都锁「根因」而不只是「现象」）：

1. **播放清单持久化往返**（后端）
   - add -> get -> 重新连库 -> get，条目还在（重启服务后仍在）。
   - 顺序 = 添加顺序。
   - 重复添加是幂等的（不产生第二条）。
   - 删素材（purge）后清单条目自动消失（外键级联，不留悬空引用）。

2. **「添加到播放器」只影响清单**（后端）
   - 加进清单**不会**改动素材本体（列表接口返回的条目不变）。
   - 素材库里没被加过的素材**不会**出现在清单里。
   - 清空清单后素材本体仍在（只是不再被播）。

3. **音频三种播放模式的推进逻辑**（前端，纯 JS）
   用 Node 真跑 web/asset-player.js 里的 advanceIndex / stepIndex：
   - 单曲循环：el.loop=true，序号不变。
   - 列表循环：播完自动下一首，最后一首之后回到第一首。
   - 随机：每首播完随机挑下一首，且**不重复当前这首**。

另外锁两条前端结构约束（A2 明确要求）：
   - 图片模式**只有**顶部一个「每张停止时间」输入框（.al-pitem-sec 已删除）。
   - 播放器的数据源是服务端清单，**不是** window.ASSETLIB.getItems()（当前页素材）。

运行：python3 -m pytest tests/test_a23_player.py -v
"""
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from h3studio import asset_api  # noqa: E402
from h3studio import asset_repo  # noqa: E402
from h3studio import assets as A  # noqa: E402


# ==================================================================
# 1 + 2. 后端：清单持久化 / 只影响清单
# ==================================================================


class PlayerApiTestBase(unittest.TestCase):
    """
    起一个真实的 aiohttp 测试服务器跑播放器清单接口。

    【为什么不 mock】
    这次的 bug 恰好在「SQL 列名」「路由匹配」「外键级联」这些
    只有真跑才暴露的地方 —— mock 掉等于什么都没测。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="a23-player-"))
        cls.root = cls.tmp / "library"
        cls._old_root = os.environ.get("ASSET_LIBRARY_ROOT")
        os.environ["ASSET_LIBRARY_ROOT"] = str(cls.root)
        A.init_db()

        # 素材库有 job 相关接口会去取任务库 —— 测试里指向临时库。
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
        # 还原 event loop —— 不还原的话，后面走 asyncio.get_event_loop()
        # 的库会拿到一个已关闭的 loop（Playwright / aiohttp 集体报错）。
        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass
        if cls._old_root is None:
            os.environ.pop("ASSET_LIBRARY_ROOT", None)
        else:
            os.environ["ASSET_LIBRARY_ROOT"] = cls._old_root
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # 每个用例都从空清单开始（用例之间互不干扰）。
        with A.connect() as conn:
            asset_repo.player_clear(conn)
            conn.commit()

    # ---------------------------------------------------------- 工具

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    def _make_assets(self, specs):
        """直接往库里塞素材（比走上传快得多，也不用造文件）。"""
        ids = []
        with A.connect() as conn:
            stamp = A.now()
            for asset_id, name, category in specs:
                conn.execute(
                    "INSERT INTO assets (id, name, category, subtype, mime, tags, "
                    "source, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (asset_id, name, category, "photo" if category == "image" else "music",
                     "image/png" if category == "image" else "audio/mpeg",
                     "[]", "upload", stamp, stamp))
                ids.append(asset_id)
            conn.commit()
        return ids

    def _get_player(self):
        async def go():
            async with self.client.get("/api/assets/player") as resp:
                return resp.status, await resp.json()
        return self._run(go())

    def _post_player(self, path, payload):
        async def go():
            async with self.client.post(path, json=payload) as resp:
                return resp.status, await resp.json()
        return self._run(go())

    def _list_assets(self):
        async def go():
            async with self.client.get("/api/assets") as resp:
                return resp.status, await resp.json()
        return self._run(go())

    # ---------------------------------------------------------- 用例

    def test_01_player_routes_are_not_swallowed_by_asset_id(self):
        """
        /api/assets/player 不能被 /api/assets/{id} 抢走。

        踩过的坑：aiohttp 的路由匹配如果先命中动态段，
        /api/assets/player 会被当成素材 id="player" -> 404。
        这里真跑一次，确认静态路由优先。
        """
        status, data = self._get_player()
        self.assertEqual(status, 200)
        self.assertIn("entries", data)
        self.assertIn("img_seconds", data)

    def test_02_add_then_get_roundtrip(self):
        """添加到播放器 -> GET 清单能看到，顺序 = 添加顺序。"""
        ids = self._make_assets([
            ("asset_img1", "分镜1", "image"),
            ("asset_aud1", "配乐1", "audio"),
            ("asset_img2", "分镜2", "image"),
        ])
        status, result = self._post_player("/api/assets/player/add",
                                           {"asset_ids": ids})
        self.assertEqual(status, 200)
        self.assertEqual(result["added"], ids)
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["missing"], [])

        status, state = self._get_player()
        self.assertEqual(status, 200)
        self.assertEqual([e["asset_id"] for e in state["entries"]], ids)
        self.assertEqual(state["counts"], {"image": 2, "audio": 1, "video": 0, "total": 3})

    def test_03_add_is_idempotent(self):
        """重复添加是幂等的：不会产生第二条。"""
        ids = self._make_assets([("asset_x", "X", "image")])
        self._post_player("/api/assets/player/add", {"asset_ids": ids})
        status, result = self._post_player("/api/assets/player/add", {"asset_ids": ids})
        self.assertEqual(status, 200)
        self.assertEqual(result["added"], [])
        self.assertEqual(result["skipped"], ids)
        _, state = self._get_player()
        self.assertEqual(len(state["entries"]), 1)

    def test_04_add_reports_missing_assets(self):
        """不存在的素材 id 要**如实**报进 missing，不能静默吞掉。"""
        self._make_assets([("asset_ok", "OK", "image")])
        status, result = self._post_player(
            "/api/assets/player/add", {"asset_ids": ["asset_ok", "asset_nope"]})
        self.assertEqual(status, 200)
        self.assertEqual(result["added"], ["asset_ok"])
        self.assertEqual(result["missing"], ["asset_nope"])

    def test_05_add_requires_asset_ids(self):
        """空请求要 400，不能静默成功。"""
        status, _ = self._post_player("/api/assets/player/add", {})
        self.assertEqual(status, 400)

    def test_06_persists_across_connections(self):
        """
        清单必须**持久化**：换一条连接（模拟服务重启）后条目还在。

        【为什么这条最重要】
        原实现把播放列表放在前端的内存里 ——
        刷新页面 / 重启服务就没了，用户配好的顺序全丢。
        """
        ids = self._make_assets([("asset_p1", "P1", "audio"),
                                 ("asset_p2", "P2", "audio")])
        self._post_player("/api/assets/player/add", {"asset_ids": ids})

        # 全新连接（相当于进程重启后重新 connect()）
        with A.connect() as fresh:
            state = asset_repo.player_state(fresh)
        self.assertEqual([e["asset_id"] for e in state["entries"]], ids)

    def test_07_img_seconds_persists(self):
        """图片模式「每张停止时间」也持久化（存在清单表里）。"""
        status, result = self._post_player("/api/assets/player/settings",
                                           {"img_seconds": 7})
        self.assertEqual(status, 200)
        self.assertEqual(result["img_seconds"], 7.0)
        with A.connect() as fresh:
            state = asset_repo.player_state(fresh)
        self.assertEqual(state["img_seconds"], 7.0)

    def test_08_img_seconds_is_clamped(self):
        """停止时间要夹在 1..600 秒，超范围不能把播放器搞坏。"""
        self._post_player("/api/assets/player/settings", {"img_seconds": 99999})
        _, state = self._get_player()
        self.assertEqual(state["img_seconds"], 600.0)
        self._post_player("/api/assets/player/settings", {"img_seconds": 0})
        _, state = self._get_player()
        self.assertEqual(state["img_seconds"], 1.0)

    def test_09_add_does_not_change_assets(self):
        """
        「添加到播放器」**只影响清单**，不改素材本体。

        锁这条是因为：如果实现时顺手 UPDATE 了 assets 表，
        素材的名称/标签/更新时间就会被改掉 —— 用户会发现
        「我只是加进播放器，素材怎么变了」。
        """
        self._make_assets([("asset_keep", "原名", "image")])
        before = self._list_assets()[1]["assets"][0]
        self._post_player("/api/assets/player/add", {"asset_ids": ["asset_keep"]})
        after = self._list_assets()[1]["assets"][0]
        self.assertEqual(before["name"], after["name"])
        self.assertEqual(before["updated_at"], after["updated_at"])
        self.assertEqual(before["id"], after["id"])

    def test_10_playlist_only_contains_added_assets(self):
        """
        清单里**只有**被主动加过的素材。

        这是 A2-1 的核心：播放列表不再自动包含素材库里的所有素材。
        """
        self._make_assets([("asset_in", "加过的", "image"),
                           ("asset_out", "没加的", "image")])
        self._post_player("/api/assets/player/add", {"asset_ids": ["asset_in"]})
        _, state = self._get_player()
        self.assertEqual([e["asset_id"] for e in state["entries"]], ["asset_in"])
        # 素材库里两条都在（按 id 查，不按总数 —— 用例之间共用同一个库，
        # 总数会被别的用例的素材污染），但清单里只有一条。
        library_ids = {a["id"] for a in self._list_assets()[1]["assets"]}
        self.assertIn("asset_in", library_ids)
        self.assertIn("asset_out", library_ids)

    def test_11_remove_and_clear(self):
        """移除与清空：清单空了，但素材本体还在。"""
        ids = self._make_assets([("asset_r1", "R1", "image"),
                                 ("asset_r2", "R2", "audio")])
        self._post_player("/api/assets/player/add", {"asset_ids": ids})

        status, result = self._post_player("/api/assets/player/remove",
                                           {"asset_ids": ["asset_r1"]})
        self.assertEqual(status, 200)
        self.assertEqual(result["removed"], ["asset_r1"])
        _, state = self._get_player()
        self.assertEqual([e["asset_id"] for e in state["entries"]], ["asset_r2"])

        self._post_player("/api/assets/player/clear", {})
        _, state = self._get_player()
        self.assertEqual(state["entries"], [])
        # 素材本体不受影响
        library_ids = {a["id"] for a in self._list_assets()[1]["assets"]}
        for asset_id in ids:
            self.assertIn(asset_id, library_ids)

    def test_12_remove_is_idempotent(self):
        """移除不存在的条目不算错（幂等）。"""
        status, result = self._post_player("/api/assets/player/remove",
                                           {"asset_ids": ["asset_ghost"]})
        self.assertEqual(status, 200)
        self.assertEqual(result["removed"], [])

    def test_13_purged_asset_leaves_no_dangling_entry(self):
        """
        素材被彻底删除后，清单里不能留悬空引用。

        靠的是 player_entries.asset_id 的 ON DELETE CASCADE。
        """
        ids = self._make_assets([("asset_purge", "P", "image")])
        self._post_player("/api/assets/player/add", {"asset_ids": ids})
        with A.connect() as conn:
            asset_repo.purge(conn, "asset_purge")
            conn.commit()
        _, state = self._get_player()
        self.assertEqual(state["entries"], [])

    def test_14_soft_deleted_asset_is_flagged_not_dropped(self):
        """
        素材进回收站（软删除）时，清单条目**保留但标记 deleted**。

        为什么保留：用户还原素材后清单要能自动恢复，
        不该因为一次误删就丢掉整份编排。
        """
        ids = self._make_assets([("asset_trash", "T", "image")])
        self._post_player("/api/assets/player/add", {"asset_ids": ids})
        with A.connect() as conn:
            asset_repo.soft_delete(conn, "asset_trash")
            conn.commit()
        _, state = self._get_player()
        self.assertEqual(len(state["entries"]), 1)
        self.assertTrue(state["entries"][0]["deleted"])

    def test_15_entry_carries_playable_urls(self):
        """条目要带上前端能直接用的 URL（图片缩略图 / 音视频原件）。"""
        ids = self._make_assets([("asset_url", "U", "image")])
        self._post_player("/api/assets/player/add", {"asset_ids": ids})
        _, state = self._get_player()
        entry = state["entries"][0]
        self.assertEqual(entry["content_url"], "/api/assets/asset_url/content")
        self.assertIn("preview_url", entry)

    def test_16_get_on_empty_library_does_not_write(self):
        """
        清单还没建过时，GET **不能**往库里写。

        【为什么锁这条】
        素材库在外接盘上。读操作（看一眼清单）如果顺手 INSERT 一行，
        盘只读 / 满了 / 没挂的时候 GET 直接 500 ——
        读操作不该因为写失败而失败。
        """
        with A.connect() as conn:
            conn.execute("DELETE FROM player_entries")
            conn.execute("DELETE FROM player_playlists")
            conn.commit()

        status, state = self._get_player()
        self.assertEqual(status, 200)
        self.assertEqual(state["entries"], [])
        self.assertEqual(state["counts"]["total"], 0)

        # 库里的清单表仍然为空 —— GET 没写东西
        with A.connect() as conn:
            rows = conn.execute(
                "SELECT COUNT(*) c FROM player_playlists").fetchone()
        self.assertEqual(int(rows["c"]), 0)

    def test_17_add_creates_playlist_on_first_write(self):
        """第一次写操作时才建清单（add / settings 走这条路）。"""
        with A.connect() as conn:
            conn.execute("DELETE FROM player_entries")
            conn.execute("DELETE FROM player_playlists")
            conn.commit()

        ids = self._make_assets([("asset_first", "F", "image")])
        status, result = self._post_player("/api/assets/player/add",
                                           {"asset_ids": ids})
        self.assertEqual(status, 200)
        self.assertEqual(result["added"], ids)
        with A.connect() as conn:
            rows = conn.execute("SELECT COUNT(*) c FROM player_playlists").fetchone()
        self.assertEqual(int(rows["c"]), 1)


# ==================================================================
# 3. 前端：三种播放模式的推进逻辑（Node 真跑 JS）
# ==================================================================

NODE = shutil.which("node") or shutil.which("nodejs")

PLAYER_JS = (REPO / "web" / "asset-player.js").read_text(encoding="utf-8")
ASSETS_JS = (REPO / "web" / "assets.js").read_text(encoding="utf-8")


def strip_js_comments(src: str) -> str:
    """
    去掉 JS 注释，只留代码。

    【为什么必须去注释再断言】
    源码里**注释**经常要写「这里原本有个 xxx，我们删掉了」——
    那正是要证明的「历史痕迹」，不是「代码还在用」。
    直接在原文里 grep 会把注释当成实现，断言就失真了。
    （本项目的 test_agent_frontend_enh.py 有同款实现。）
    """
    out = []
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "*":
            end = src.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
            continue
        if ch == "/" and nxt == "/":
            end = src.find("\n", i)
            i = n if end < 0 else end
            out.append(" ")
            continue
        if ch in ("'", '"', "`"):
            # 字符串/模板字符串：整段原样拷贝（里面可能有 // 和 /*）
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                c = src[i]
                out.append(c)
                if c == "\\":
                    if i + 1 < n:
                        out.append(src[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                if c == quote:
                    i += 1
                    break
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


#: 去掉注释后的源码（结构断言用这个，避免把「历史说明」当成「实现还在」）
PLAYER_CODE = strip_js_comments(PLAYER_JS)
ASSETS_CODE = strip_js_comments(ASSETS_JS)


def _extract_iife_body(src: str) -> str:
    """
    把 asset-player.js 的 IIFE 拆开，让内部函数进入当前作用域。

    （只用于测试，不改线上文件。做法同 tests/test_job_meta_frontend.py。）
    """
    start = src.find("(function () {")
    if start < 0:
        raise AssertionError("asset-player.js 里找不到 IIFE 开头")
    end = src.rfind("})();")
    if end < 0:
        raise AssertionError("asset-player.js 里找不到 IIFE 结尾")
    return src[:start] + src[start + len("(function () {"):end] + src[end + len("})();"):]


#: Node 侧的最小 DOM shim —— asset-player.js 只需要这些。
_PRELUDE = """
global.window = global;
global.document = {
  readyState: "complete",
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: () => {},
};
global.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });
global.Audio = class { constructor() {} play() { return Promise.resolve(); }
                      pause() {} load() {} addEventListener() {} setAttribute() {}
                      removeAttribute() {} };
"""


def _run_node(script: str, timeout: int = 60) -> dict:
    assert NODE is not None, "node 不可用"
    full = (_PRELUDE
            + "\n" + _extract_iife_body(PLAYER_JS)
            + "\n" + script)
    proc = subprocess.run(
        [NODE, "--input-type=commonjs", "-e", full],
        capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise AssertionError(f"node 执行失败:\n{proc.stderr}")
    out = proc.stdout.strip()
    return json.loads(out) if out else {}


@unittest.skipIf(NODE is None, "node 不可用，跳过前端逻辑测试")
class PlayerFrontendLogicTest(unittest.TestCase):
    """纯逻辑断言：advanceIndex / stepIndex / collect。"""

    def test_01_list_mode_advances_and_wraps(self):
        """
        列表循环：播完一首自动下一首，最后一首之后回到第一首。

        这是老实现最大的 bug（没有 ended 监听 -> 播完静默停止）。
        """
        out = _run_node("""
        const seq = [];
        let cur = 0;
        for (let k = 0; k < 4; k++) {
          cur = advanceIndex(cur, 3, "list");
          seq.push(cur);
        }
        console.log(JSON.stringify(seq));
        """)
        self.assertEqual(out, [1, 2, 0, 1])

    def test_02_single_mode_keeps_index(self):
        """单曲循环：序号不变（实际重播由 el.loop=true 负责）。"""
        out = _run_node("""
        console.log(JSON.stringify([
          advanceIndex(1, 3, "single"),
          advanceIndex(0, 3, "single"),
        ]));
        """)
        self.assertEqual(out, [1, 0])

    def test_03_shuffle_never_repeats_current(self):
        """
        随机模式：下一首**不能**是当前这首。

        抽到当前时必须顺延 —— 否则用户听到的是「又播了一遍」，
        看起来就像随机模式没生效（实测过这个坑）。
        """
        out = _run_node("""
        // 注入固定随机源：0.0 -> floor(0*3)=0
        console.log(JSON.stringify({
          hit0: advanceIndex(0, 3, "shuffle", () => 0.0),
          hit1: advanceIndex(1, 3, "shuffle", () => 0.34),
          hit2: advanceIndex(2, 3, "shuffle", () => 0.67),
          single: advanceIndex(0, 1, "shuffle", () => 0.0),
        }));
        """)
        self.assertEqual(out["hit0"], 1)    # 抽到 0（=current）-> 顺延到 1
        self.assertEqual(out["hit1"], 2)    # 抽到 1（=current）-> 顺延到 2
        self.assertEqual(out["hit2"], 0)    # 抽到 2（=current）-> 回绕到 0
        self.assertEqual(out["single"], 0)  # 只有一首时没有「下一首」可言

    def test_04_shuffle_random_source_actually_varies(self):
        """随机源不同时，选出来的下一首也不同（真的在随机，不是固定值）。"""
        out = _run_node("""
        const picks = new Set();
        for (let k = 0; k < 30; k++) {
          picks.add(advanceIndex(0, 4, "shuffle", () => (k % 4) / 4));
        }
        console.log(JSON.stringify(Array.from(picks).sort()));
        """)
        # 4 首里，随机源覆盖 0/0.25/0.5/0.75 -> 能抽到 1/2/3（0 被顺延）
        self.assertEqual(out, [1, 2, 3])

    def test_05_shuffle_never_out_of_range(self):
        """随机值越界（1.0 / 负数）也不能选出越界的序号。"""
        out = _run_node("""
        console.log(JSON.stringify([
          advanceIndex(0, 3, "shuffle", () => 1.0),
          advanceIndex(0, 3, "shuffle", () => -0.5),
          advanceIndex(0, 3, "shuffle", () => NaN),
        ]));
        """)
        for value in out:
            self.assertTrue(0 <= value < 3, f"越界: {value}")

    def test_06_step_list_supports_previous(self):
        """手动「上一首」要真的往回走（列表模式）。"""
        out = _run_node("""
        console.log(JSON.stringify({
          prevFrom0: stepIndex(0, 3, -1, "list"),
          nextFrom2: stepIndex(2, 3, 1, "list"),
          nextFrom0: stepIndex(0, 3, 1, "list"),
        }));
        """)
        self.assertEqual(out, {"prevFrom0": 2, "nextFrom2": 0, "nextFrom0": 1})

    def test_07_empty_list_is_safe(self):
        """空清单不能抛异常（越界崩溃会让播放器整个打不开）。"""
        out = _run_node("""
        console.log(JSON.stringify({
          adv: advanceIndex(0, 0, "list"),
          step: stepIndex(0, 0, 1, "list"),
        }));
        """)
        self.assertEqual(out, {"adv": 0, "step": 0})

    def test_08_collect_groups_by_category(self):
        """清单条目按 image / audio / video 分组，回收站的跳过。"""
        out = _run_node("""
        const entries = [
          { asset_id: "i1", name: "图1", category: "image",
            preview_url: "/p/i1", content_url: "/c/i1" },
          { asset_id: "a1", name: "音1", category: "audio",
            preview_url: null, content_url: "/c/a1" },
          { asset_id: "v1", name: "视1", category: "video",
            preview_url: null, content_url: "/c/v1" },
          { asset_id: "i2", name: "图2", category: "image",
            preview_url: "/p/i2", content_url: "/c/i2", deleted: true },
          { asset_id: "d1", name: "文档", category: "doc",
            preview_url: null, content_url: "/c/d1" },
        ];
        collect(entries);
        console.log(JSON.stringify({
          image: PLAYLIST.image.map(x => x.id),
          audio: PLAYLIST.audio.map(x => x.id),
          video: PLAYLIST.video.map(x => x.id),
          imageUrl: PLAYLIST.image[0].url,
          audioUrl: PLAYLIST.audio[0].url,
        }));
        """)
        self.assertEqual(out["image"], ["i1"])       # i2 在回收站 -> 跳过
        self.assertEqual(out["audio"], ["a1"])
        self.assertEqual(out["video"], ["v1"])
        # 图片用缩略图，音频用原件
        self.assertEqual(out["imageUrl"], "/p/i1")
        self.assertEqual(out["audioUrl"], "/c/a1")


@unittest.skipIf(NODE is None, "node 不可用，跳过前端结构断言")
class PlayerFrontendStructureTest(unittest.TestCase):
    """
    前端结构约束（源码级断言 —— 这些是「必须不存在」的结构性要求）。

    为什么用源码断言而不是浏览器：
    「.al-pitem-sec 这个类名不该再出现」是**结构**约束，
    源码里没有它 + 渲染出来的 DOM 里没有它，两边都查最省事。
    """

    def test_01_no_per_item_seconds_input(self):
        """图片模式不再有每张一个小输入框（.al-pitem-sec）。"""
        # 用**去注释后**的源码断言 —— 注释里写的「这里原本有 .al-pitem-sec」
        # 是历史说明，不是实现还在。
        self.assertNotIn("al-pitem-sec", PLAYER_CODE)
        self.assertNotIn("al-pitem-sec", ASSETS_CODE)
        # 顶部那个「每张停止时间」输入框必须还在
        self.assertIn('id="alPSeconds"', PLAYER_CODE)
        # 也不该再有 per-item 的 data-idx 输入框绑定
        self.assertNotIn('class="al-pitem-sec"', PLAYER_CODE)

    def test_02_player_reads_server_playlist(self):
        """
        播放器的数据源必须是**服务端清单**，不是当前页素材。

        锁这条是因为老实现用 window.ASSETLIB.getItems() ——
        那是「当前页的全部素材」，正是 A2-1 要修的 bug。
        """
        self.assertIn("/api/assets/player", PLAYER_CODE)
        self.assertNotIn("ASSETLIB.getItems", PLAYER_CODE)
        self.assertNotIn("ASSETLIB", PLAYER_CODE)

    def test_03_audio_has_ended_listener(self):
        """必须有 ended 监听（否则列表/随机模式播完就静默停止）。"""
        self.assertIn('"ended"', PLAYER_CODE)
        self.assertIn("onMediaEnded", PLAYER_CODE)
        # 背景音也要有 ended 推进（老 bug：只播第一首）
        self.assertIn("bgIndex", PLAYER_CODE)

    def test_04_audio_playing_flag_driven_by_events(self):
        """
        audioPlaying 必须由媒体元素的 play / pause 事件驱动。

        老实现从来没有把它置成 true（只有 stopAudio 置 false），
        于是勾选框状态与真实播放状态对不上。
        """
        self.assertIn('el.addEventListener("play"', PLAYER_CODE)
        self.assertIn('el.addEventListener("pause"', PLAYER_CODE)
        self.assertIn("S.audioPlaying = true", PLAYER_CODE)

    def test_05_assets_js_has_add_to_player_entry(self):
        """素材库侧要有「添加到播放器」的入口（右键 + 多选条）。"""
        self.assertIn("add-player", ASSETS_CODE)
        self.assertIn("addToPlayer", ASSETS_CODE)
        self.assertIn("/api/assets/player/add", ASSETS_CODE)

    def test_06_index_html_has_player_button(self):
        """多选条上要有「添加到播放器」按钮，播放器要有「清空清单」。"""
        html = (REPO / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="alSelPlayer"', html)
        self.assertIn('id="alPlayerClear"', html)


if __name__ == "__main__":
    unittest.main()
