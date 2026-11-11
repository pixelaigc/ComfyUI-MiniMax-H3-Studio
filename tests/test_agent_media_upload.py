# -*- coding: utf-8 -*-
"""
内置 Agent 聊天区 —— **媒体上传**（附件）的回归测试。

【覆盖什么】
1. **上传接口的边界**（类型 / 大小 / 魔数 / 数量）
   - 非法类型被拒（.txt / .exe / 无扩展名）
   - **伪装文件被拒**（.exe 改名成 .png —— 只看扩展名会被骗过去）
   - 超大文件被拒
   - 空文件被拒
   - 超过 9 张被拒
   - 单个失败不影响其它（一个合法 + 一个非法 -> 合法那个成功）
2. **附件进 Agent 上下文**
   - 附件的 asset_id 出现在系统提示词的「附件」段
   - 附件的描述来自**素材库已有的分析结果**（不是现编的）
   - 附件进 ctx.assets（与 search_assets 命中的素材同构）
3. **附件 -> create_job 参考图**（核心链路）
   - 模型**没**显式传 asset_id 时，附件自动成为参考图
   - 模型**显式**传了 asset_ids 时，用模型的
   - 引用的是**素材库条目**（走 asset_refs 机制），不是裸路径
4. **attachments 参数的透传**（run_agent -> /api/agent/chat）
5. **回归护栏**：真编造的 job id 仍被拦截；步骤上限截断仍是中性态

【取证纪律】
- 素材库落在**临时目录**（ASSET_LIBRARY_ROOT 环境变量），不碰生产素材库
- 走**真的** asset_service.register_asset（不是造假的 asset 条目）
- Agent 上下文用**真的** run_agent（假 provider 只桩模型调用）

运行：python3 -m pytest tests/test_agent_media_upload.py -v
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

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import agent as agent_mod  # noqa: E402
from h3studio import agent_media  # noqa: E402
from h3studio import asset_repo  # noqa: E402
from h3studio import assets as asset_store  # noqa: E402
from h3studio import workflows as wf  # noqa: E402


# ------------------------------------------------------------------ 工具


def make_image(path: Path, size=(64, 48), color=(200, 30, 30)) -> Path:
    """造一张**真的** PNG（PIL 编码，保证魔数与解码都合法）。"""
    from PIL import Image
    Image.new("RGB", size, color).save(path)
    return path


def make_jpeg(path: Path, size=(64, 48), color=(30, 200, 30)) -> Path:
    from PIL import Image
    Image.new("RGB", size, color).save(path)
    return path


def make_webp(path: Path, size=(64, 48)) -> Path:
    from PIL import Image
    Image.new("RGB", size, (30, 30, 200)).save(path, format="WEBP")
    return path


# ------------------------------------------------------------------ 素材库测试夹具


class AssetLibraryTestCase(unittest.TestCase):
    """把素材库指到临时目录，避免污染生产素材库。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent_media_test_"))
        self.lib_root = self.tmp / "lib"
        self._old_env = os.environ.get("ASSET_LIBRARY_ROOT")
        os.environ["ASSET_LIBRARY_ROOT"] = str(self.lib_root)
        report = asset_store.ensure_layout()
        self.assertTrue(report["ok"], report.get("errors"))
        # 建表 —— connect() 只开连接，不建表（init_db 才建）。
        # 不建表的话 register_asset 会报「no such table: asset_versions」。
        init = asset_store.init_db()
        self.assertTrue(init.get("ok"), init.get("errors"))

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("ASSET_LIBRARY_ROOT", None)
        else:
            os.environ["ASSET_LIBRARY_ROOT"] = self._old_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def register(self, path: Path, name="test.png", note=None):
        """直接用素材库的真实入库函数造一个条目。"""
        from h3studio import asset_service
        with asset_store.connect() as conn:
            result = asset_service.register_asset(
                conn, source_path=path, filename=name,
                category="image", subtype="photo", note=note,
                source="agent_upload", creator="test")
        return result

    def set_analysis(self, asset_id: str, text: str):
        """给素材写一条**已有的**分析结果（模拟上传后异步分析完成）。"""
        with asset_store.connect() as conn:
            asset_repo.save_analysis(conn, asset_id, "image", text,
                                     asset_repo.next_revision(conn, asset_id),
                                     provider={"name": "test"}, prompt="p")


# ------------------------------------------------------------------ 1. 魔数嗅探


class TestMagicSniffing(unittest.TestCase):
    """扩展名只是**线索**，真实格式才是判据。"""

    def test_real_png_is_detected(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_image(tmp / "a.png")
            head = p.read_bytes()[:32]
            self.assertEqual(agent_media._detect_image_kind("a.png", head), ".png")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_real_jpeg_is_detected(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_jpeg(tmp / "a.jpg")
            head = p.read_bytes()[:32]
            self.assertEqual(agent_media._detect_image_kind("a.jpg", head), ".jpg")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_real_webp_is_detected(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_webp(tmp / "a.webp")
            head = p.read_bytes()[:32]
            self.assertEqual(agent_media._detect_image_kind("a.webp", head), ".webp")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_exe_renamed_to_png_is_rejected(self):
        """最关键的防线：只看扩展名会被骗过去。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            p = tmp / "evil.png"
            p.write_bytes(b"MZ\x90\x00" + b"\x00" * 200)
            head = p.read_bytes()[:32]
            self.assertIsNone(agent_media._detect_image_kind("evil.png", head))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_text_renamed_to_jpeg_is_rejected(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            p = tmp / "notimage.jpg"
            p.write_bytes(b"hello world, this is not an image at all")
            head = p.read_bytes()[:32]
            self.assertIsNone(agent_media._detect_image_kind("notimage.jpg", head))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_webp_requires_both_riff_and_webp_tags(self):
        """只有 RIFF 头不算 webp（RIFF 也可以是 wav/avi）。"""
        head = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 20
        self.assertIsNone(agent_media._detect_image_kind("a.webp", head))
        head_ok = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 20
        self.assertEqual(agent_media._detect_image_kind("a.webp", head_ok), ".webp")


# ------------------------------------------------------------------ 2. 上传端点校验


class TestUploadEndpointValidation(AssetLibraryTestCase):
    """上传接口的类型 / 大小 / 数量校验。"""

    def _upload(self, files):
        """跑一次真实的上传处理（走 agent_media 的真实入库函数）。"""
        results, errors = [], []
        tmp_dir = asset_store.library_root() / asset_store.DIR_TMP
        tmp_dir.mkdir(parents=True, exist_ok=True)

        for filename, data in files:
            try:
                agent_media._validate_suffix(filename)
            except agent_media.UploadError as exc:
                errors.append({"filename": filename, "error": exc.message})
                continue
            tmp = tmp_dir / f".t-{len(results)}{len(errors)}.tmp"
            tmp.write_bytes(data)
            head = data[:32]
            detected = agent_media._detect_image_kind(filename, head)
            if detected is None:
                tmp.unlink(missing_ok=True)
                errors.append({"filename": filename,
                               "error": "文件内容不是有效的图片"})
                continue
            item = {"tmp": tmp, "filename": filename, "suffix": detected,
                    "bytes": len(data)}
            try:
                # 用**真的**入库函数 —— 去重路径的 preview_url 只有它能覆盖到
                entry = agent_media._register_one(item)
                results.append(entry)
            except agent_media.UploadError as exc:
                errors.append({"filename": filename, "error": exc.message})
            finally:
                tmp.unlink(missing_ok=True)
        return results, errors

    def test_valid_png_is_accepted(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_image(tmp / "a.png")
            results, errors = self._upload([("a.png", p.read_bytes())])
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0]["asset_id"].startswith("asset_"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_text_extension_is_rejected(self):
        results, errors = self._upload([("notes.txt", b"hello")])
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("不支持的图片类型", errors[0]["error"])

    def test_no_extension_is_rejected(self):
        results, errors = self._upload([("blob", b"hello")])
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)

    def test_exe_is_rejected(self):
        results, errors = self._upload([("virus.exe", b"MZ\x90\x00")])
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("不支持", errors[0]["error"])

    def test_one_bad_file_does_not_block_the_good_one(self):
        """单个失败不影响其它 —— 与素材库上传同一语义。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_image(tmp / "good.png")
            results, errors = self._upload([
                ("bad.txt", b"not an image"),
                ("good.png", p.read_bytes()),
            ])
            self.assertEqual(len(results), 1)
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]["filename"], "bad.txt")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_size_limit_is_enforced(self):
        """超过单张上限必须被拒。"""
        self.assertEqual(agent_media.MAX_IMAGE_BYTES, 20 * 1024 * 1024)
        # 构造一个超过上限的 part 流（用假的 part 对象）
        oversized = agent_media.MAX_IMAGE_BYTES + 1024

        class FakePart:
            filename = "big.png"

            def __init__(self, total):
                self.total = total
                self.sent = 0

            async def read_chunk(self, size):
                if self.sent >= self.total:
                    return b""
                chunk = b"\x89PNG\r\n\x1a\n" + b"\x00" * (size - 8)
                self.sent += len(chunk)
                return chunk

        tmp_dir = asset_store.library_root() / asset_store.DIR_TMP
        with self.assertRaises(agent_media.UploadError) as ctx:
            asyncio.run(agent_media._read_and_validate_part(
                FakePart(oversized), tmp_dir))
        self.assertEqual(ctx.exception.code, "file_too_large")
        self.assertEqual(ctx.exception.status, 413)

    def test_empty_file_is_rejected(self):
        class FakePart:
            filename = "empty.png"

            async def read_chunk(self, size):
                return b""

        tmp_dir = asset_store.library_root() / asset_store.DIR_TMP
        with self.assertRaises(agent_media.UploadError) as ctx:
            asyncio.run(agent_media._read_and_validate_part(FakePart(), tmp_dir))
        self.assertEqual(ctx.exception.code, "empty_file")

    def test_max_images_limit_is_nine(self):
        """一次最多 9 张（用户要求：与 Qwen-Image 参考图能力对齐）。"""
        self.assertEqual(agent_media.MAX_IMAGES_PER_UPLOAD, 9)
        self.assertEqual(agent_mod.MAX_ATTACHMENTS, 9)

    def test_allowed_suffixes_cover_required_formats(self):
        """用户要求至少支持 png/jpg/webp。"""
        for suffix in (".png", ".jpg", ".jpeg", ".webp"):
            self.assertIn(suffix, agent_media.ALLOWED_IMAGE_SUFFIXES)

    def test_duplicate_upload_still_has_preview_url(self):
        """【回归 —— 真实浏览器验收抓出来的 bug】

        同一张图传两次 -> 第二次命中 sha256 去重，
        register_asset 返回 {"asset": ..., "duplicated": True}
        **没有 version 键** -> preview_url 变成 None
        -> 前端 <img src=""> -> 缩略图**显示不出来**（坏图）。

        修法：去重路径自己去查 current_version。
        """
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_image(tmp / "dup.png")
            data = p.read_bytes()
            first = self._upload([("dup.png", data)])
            second = self._upload([("dup.png", data)])
            self.assertEqual(len(first[1]), 0, "第一次上传不该有错")
            self.assertEqual(len(second[1]), 0, "第二次上传不该有错")
            self.assertEqual(len(second[0]), 1)
            # 复用同一个素材
            self.assertEqual(first[0][0]["asset_id"], second[0][0]["asset_id"])
            # **关键**：去重的那次也必须有 preview_url（否则缩略图是坏的）
            self.assertTrue(
                second[0][0]["preview_url"],
                "去重路径丢了 preview_url -> 缩略图显示不出来")
            self.assertTrue(second[0][0]["preview_url"].startswith("/api/assets/"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ 3. 附件上下文


class TestAttachmentContext(AssetLibraryTestCase):
    """附件进 Agent 上下文的正确性。"""

    def _agent_ctx(self):
        return agent_mod.AgentContext(server_module=None, provider={})

    def test_normalize_accepts_three_shapes(self):
        self.assertEqual(
            agent_mod._normalize_attachments(["a1", "a2"]), ["a1", "a2"])
        self.assertEqual(
            agent_mod._normalize_attachments([{"asset_id": "b1"}]), ["b1"])
        self.assertEqual(
            agent_mod._normalize_attachments([{"id": "c1"}]), ["c1"])

    def test_normalize_dedupes_and_drops_empty(self):
        self.assertEqual(
            agent_mod._normalize_attachments(["a1", "a1", "", None, "  "]),
            ["a1"])

    def test_normalize_caps_at_max(self):
        raw = [f"a{i}" for i in range(20)]
        self.assertEqual(len(agent_mod._normalize_attachments(raw)), 9)

    def test_attachment_description_comes_from_library_analysis(self):
        """描述必须是**素材库里已有的分析结果**，不是现编的。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_image(tmp / "a.png")
            result = self.register(p, note="一张测试图")
            asset_id = result["asset"]["id"]
            self.set_analysis(asset_id, "【主体】红色色块")

            ctx = self._agent_ctx()
            entries = agent_mod._load_attachments(ctx, [asset_id])
            self.assertEqual(len(entries), 1)
            self.assertIn("红色色块", entries[0]["description"])
            # 附件也要进 ctx.assets —— 与 search_assets 命中的素材同构
            self.assertIn(asset_id, ctx.assets)
            self.assertTrue(ctx.assets[asset_id]["analyzed"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_missing_attachment_does_not_raise(self):
        """素材不存在 -> 标注为 missing，但不抛异常（不让整轮对话失败）。"""
        ctx = self._agent_ctx()
        entries = agent_mod._load_attachments(ctx, ["asset_doesnotexist"])
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["missing"])

    def test_prompt_block_lists_attachments_with_ids(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_image(tmp / "a.png")
            result = self.register(p)
            asset_id = result["asset"]["id"]
            self.set_analysis(asset_id, "蓝色方块")

            ctx = self._agent_ctx()
            entries = agent_mod._load_attachments(ctx, [asset_id])
            block = agent_mod._attachment_prompt_block(entries)
            self.assertIn(asset_id, block)
            self.assertIn("<image1>", block)
            self.assertIn("蓝色方块", block)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_prompt_block_empty_without_attachments(self):
        self.assertEqual(agent_mod._attachment_prompt_block([]), "")

    def test_prompt_block_does_not_fabricate_description(self):
        """没有分析结论时必须如实说「尚无」，不能编。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_image(tmp / "a.png")
            result = self.register(p)
            asset_id = result["asset"]["id"]
            ctx = self._agent_ctx()
            entries = agent_mod._load_attachments(ctx, [asset_id])
            block = agent_mod._attachment_prompt_block(entries)
            self.assertIn("尚无分析结论", block)
            self.assertNotIn("红色", block)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ 4. 附件 -> create_job


class TestAttachmentBecomesReferenceImage(AssetLibraryTestCase):
    """核心链路：上传的图必须能作为 create_job 的参考图。"""

    def _ctx_with_attachment(self, asset_id):
        ctx = agent_mod.AgentContext(server_module=None, provider={})
        ctx.attachments = agent_mod._load_attachments(ctx, [asset_id])
        return ctx

    def test_attachment_used_when_model_omits_asset_id(self):
        """模型忘了传 asset_id -> 附件自动成为参考图。

        这正是要消灭的体验问题：用户明明传了图，
        任务却因为「图片编辑必须指定参考图」而失败。
        """
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_image(tmp / "a.png")
            result = self.register(p)
            asset_id = result["asset"]["id"]
            ctx = self._ctx_with_attachment(asset_id)

            args = {"mode": "i2i_qwen", "prompt": "把背景换成海边"}
            raw_assets = args.get("asset_ids")
            if raw_assets is None:
                single = str(args.get("asset_id") or "").strip()
                raw_assets = [single] if single else []
            raw_assets = [str(x or "").strip() for x in (raw_assets or [])]
            raw_assets = [x for x in raw_assets if x]
            if not raw_assets:
                attachment_ids = agent_mod._attachment_asset_ids(
                    getattr(ctx, "attachments", None))
                if attachment_ids:
                    raw_assets = attachment_ids
            refs = agent_mod._build_asset_refs("i2i_qwen", args, raw_assets)

            self.assertEqual(len(refs), 1)
            self.assertEqual(refs[0]["asset_id"], asset_id)
            # 必须是素材库引用（asset_id + 槽位 + 序号），不是裸路径
            self.assertEqual(refs[0]["slot"], "ref_image")
            self.assertEqual(refs[0]["ordinal"], 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_explicit_asset_ids_win_over_attachments(self):
        """模型显式给了参考图 -> 用模型的（模型可能有更准的判断）。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_image(tmp / "a.png")
            result = self.register(p)
            attach_id = result["asset"]["id"]
            other = "asset_from_library_xyz"

            ctx = self._ctx_with_attachment(attach_id)
            args = {"mode": "i2i_qwen", "prompt": "x",
                    "asset_ids": [other]}
            raw_assets = [str(x or "").strip() for x in (args.get("asset_ids") or [])]
            raw_assets = [x for x in raw_assets if x]
            if not raw_assets:
                raw_assets = agent_mod._attachment_asset_ids(
                    getattr(ctx, "attachments", None))
            refs = agent_mod._build_asset_refs("i2i_qwen", args, raw_assets)
            self.assertEqual([r["asset_id"] for r in refs], [other])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_multiple_attachments_map_to_ordered_refs(self):
        """多张附件 -> 多条参考图，序号连续（顺序即 <imageN> 语义）。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            ids = []
            for i in range(3):
                p = make_image(tmp / f"a{i}.png", color=(i * 40, 10, 10))
                ids.append(self.register(p)["asset"]["id"])
            ctx = agent_mod.AgentContext(server_module=None, provider={})
            ctx.attachments = agent_mod._load_attachments(ctx, ids)
            raw = agent_mod._attachment_asset_ids(ctx.attachments)
            refs = agent_mod._build_asset_refs("i2i_qwen", {}, raw)
            self.assertEqual(len(refs), 3)
            self.assertEqual([r["ordinal"] for r in refs], [0, 1, 2])
            self.assertEqual([r["asset_id"] for r in refs], ids)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_slot_plan_respects_mode(self):
        """附件按 mode 的槽位计划分派，不是一律 ref_image。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_image(tmp / "a.png")
            asset_id = self.register(p)["asset"]["id"]
            # flf -> first_frame（单槽位）
            refs = agent_mod._build_asset_refs("flf", {}, [asset_id])
            self.assertEqual(refs[0]["slot"], "first_frame")
            # t2i -> 不接受素材 -> 忽略（不该让任务失败）
            refs = agent_mod._build_asset_refs("t2i", {}, [asset_id])
            self.assertEqual(refs, [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_respond_falls_back_to_attachment_assets(self):
        """模型在 respond 里忘了报素材 -> 用附件兜底。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_image(tmp / "a.png")
            asset_id = self.register(p)["asset"]["id"]
            ctx = agent_mod.AgentContext(server_module=None, provider={})
            ctx.attachments = agent_mod._load_attachments(ctx, [asset_id])

            reported = [str(x) for x in ([] or [])]
            if not reported:
                reported = agent_mod._attachment_asset_ids(
                    getattr(ctx, "attachments", None))
            self.assertEqual(reported, [asset_id])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ 5. 端到端链路


class TestUploadToJobEndToEnd(AssetLibraryTestCase):
    """上传 -> 引用 -> 建任务 的完整链路（走真的 run_agent）。"""

    def setUp(self):
        super().setUp()
        # server 模块在 import 时就会建 data 目录与 DB_PATH ——
        # 必须**先**设好 H3STUDIO_DATA_DIR 再 import，否则会污染生产 data/。
        self._old_data = os.environ.get("H3STUDIO_DATA_DIR")
        os.environ["H3STUDIO_DATA_DIR"] = str(self.tmp / "studio")
        self.server = self._load_real_server_module()
        self.server.init_db()
        self._seed_provider()

    def tearDown(self):
        if self._old_data is None:
            os.environ.pop("H3STUDIO_DATA_DIR", None)
        else:
            os.environ["H3STUDIO_DATA_DIR"] = self._old_data
        super().tearDown()

    def _seed_provider(self):
        """预置一个可用的默认 Provider —— Agent 必须能从「本地模型配置」里取到它。"""
        with self.server.db() as conn:
            conn.executescript(
                """
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

    def _load_real_server_module(self):
        import importlib
        sys.modules.pop("server", None)
        return importlib.import_module("server")

    def test_upload_then_job_uses_the_uploaded_image(self):
        """真链路：造图 -> 入库 -> 当附件 -> create_job 建出带参考图的任务。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_image(tmp / "a.png")
            result = self.register(p)
            asset_id = result["asset"]["id"]
            self.set_analysis(asset_id, "红色色块，居中")

            calls = []

            async def fake_call(provider, messages, tools,
                               options=None, on_token=None):
                calls.append(messages)
                # 第一次：模型忘了传 asset_id（真实小模型的常见行为）
                #
                # 【tool_calls 的形状】
                # llm._extract_openai_tool_calls 归一化后的结构是
                #   {"id", "name", "arguments": dict}
                # （不是 OpenAI 原始的 {"function": {...}} 嵌套形状，
                #   也不是 arguments 字符串 —— 那是 llm 层解析前的形态。）
                if len(calls) == 1:
                    return {
                        "content": None,
                        "tool_calls": [{
                            "id": "call1",
                            "name": "create_job",
                            "arguments": {
                                "mode": "i2i_qwen",
                                "prompt": "把背景换成海边日落",
                            },
                        }],
                        "raw": {},
                    }
                return {
                    "content": None,
                    "tool_calls": [{
                        "id": "call2",
                        "name": "respond",
                        "arguments": {"summary": "已按参考图建好编辑任务。"},
                    }],
                    "raw": {},
                }

            # 【只替换那一个函数，不换整个模块】
            # 换整个模块会让 llm_client.ProviderError 消失 ->
            # except 子句求值时抛 AttributeError，把真正的错误盖掉。
            original = agent_mod.llm_client
            original_call = original.call_provider_with_tools_stream
            original.call_provider_with_tools_stream = fake_call
            try:
                result = asyncio.run(agent_mod.run_agent(
                    self.server, "照这张图改一下背景",
                    attachments=[asset_id]))
            finally:
                original.call_provider_with_tools_stream = original_call

            # 任务真的建出来了
            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(result["jobs"]), 1)
            job = result["jobs"][0]
            self.assertEqual(job["mode"], "i2i_qwen")

            # **关键断言**：任务带上了上传的那张图作为参考图
            refs = job.get("asset_refs") or []
            self.assertTrue(refs, "任务没有带参考图 —— 附件没接上")
            self.assertEqual(refs[0]["asset_id"], asset_id)
            self.assertEqual(refs[0]["slot"], "ref_image")

            # 结果里带回附件（前端渲染缩略图用）
            self.assertEqual(len(result.get("attachments") or []), 1)
            self.assertEqual(result["attachments"][0]["asset_id"], asset_id)

            # 模型上下文里有附件段（带 asset_id 与素材库描述）
            system_msg = calls[0][0]["content"]
            self.assertIn(asset_id, system_msg)
            self.assertIn("红色色块", system_msg)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_run_agent_accepts_attachments_parameter(self):
        """契约：run_agent 必须接受 attachments 关键字参数。"""
        import inspect
        params = inspect.signature(agent_mod.run_agent).parameters
        self.assertIn("attachments", params)


# ------------------------------------------------------------------ 6. 回归护栏


class TestRegressionGuards(unittest.TestCase):
    """上一轮修复的能力**不能**被这次改动破坏。"""

    def test_create_job_schema_exposes_asset_ids(self):
        """多条参考图需要 asset_ids 列表参数。"""
        props = agent_mod.TOOLS["create_job"]["parameters"]["properties"]
        self.assertIn("asset_ids", props)
        self.assertEqual(props["asset_ids"]["type"], "array")

    def test_fabricated_job_id_blocking_still_present(self):
        """真编造的 job id 仍必须被拦截。"""
        self.assertTrue(hasattr(agent_mod, "_job_id_matches_any"))
        source = Path(agent_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("fabricated_job_id", source)

    def test_truncated_notice_still_present(self):
        """步骤上限截断仍是中性态（notice 字段）。"""
        source = Path(agent_mod.__file__).read_text(encoding="utf-8")
        self.assertIn('"notice": notice', source)

    def test_agent_media_module_has_no_comfyui_dependency(self):
        """附件上传**不许**自己跑 ComfyUI（用户明确要求）。"""
        source = Path(agent_media.__file__).read_text(encoding="utf-8")
        for forbidden in ("comfy", "COMFY", "workflow"):
            self.assertNotIn(forbidden, source,
                             f"agent_media 不该出现 {forbidden}")


# ------------------------------------------------------------------ 7. 前端契约


class TestFrontendContract(unittest.TestCase):
    """前端源码级契约（真正的渲染断言在浏览器验收里做）。"""

    def _agent_js(self):
        return (REPO / "web" / "agent.js").read_text(encoding="utf-8")

    def _index_html(self):
        return (REPO / "web" / "index.html").read_text(encoding="utf-8")

    def test_composer_has_upload_entry(self):
        html = self._index_html()
        self.assertIn('id="agAttachBtn"', html)
        self.assertIn('id="agAttachInput"', html)
        self.assertIn('id="agAttachments"', html)

    def test_upload_button_accepts_required_types(self):
        html = self._index_html()
        self.assertIn('accept="image/png,image/jpeg,image/webp', html)

    def test_frontend_sends_attachments_in_request_body(self):
        js = self._agent_js()
        self.assertIn("body.attachments", js)

    def test_frontend_caps_at_nine(self):
        js = self._agent_js()
        self.assertIn("const MAX_ATTACHMENTS = 9;", js)

    def test_frontend_can_remove_attachment(self):
        js = self._agent_js()
        self.assertIn("remove-attachment", js)
        self.assertIn("function removeAttachment", js)

    def test_frontend_renders_preview(self):
        js = self._agent_js()
        self.assertIn("ag-attach-thumb", js)
        self.assertIn("preview_url", js)

    def test_line_endings_policy_is_respected(self):
        """CRLF blob 必须保持 CRLF（否则合并会出伪冲突）。"""
        import subprocess
        out = subprocess.run(
            ["git", "ls-files", "--eol", "server.py", "web/style.css",
             "web/index.html", "web/agent.js", "web/responsive.css"],
            cwd=REPO, capture_output=True, text=True).stdout
        crlf_expected = ("server.py", "web/style.css", "web/index.html")
        seen = set()
        for line in out.strip().splitlines():
            # git ls-files --eol 的输出用 TAB 分隔：<i/eol> <w/eol> <attr> \t <path>
            path = line.split("\t")[-1].strip()
            seen.add(path)
            if path in crlf_expected:
                self.assertIn("i/crlf", line, f"{path} 必须保持 CRLF blob")
            else:
                self.assertIn("i/lf", line, f"{path} 必须是 LF")
        self.assertEqual(seen, set(crlf_expected) | {"web/agent.js",
                                                    "web/responsive.css"})


# ------------------------------------------------------------------ 8. 素材库入库 + 预处理


class TestLibraryIngestAndPreprocess(AssetLibraryTestCase):
    """
    【追加约束】上传的媒体必须**先进入资产素材库**，并由素材库完成预处理
    （元数据抽取 / 模型内容分析生成描述 / 标签），之后才进入 Agent 上下文。

    这里的测试**不跑真模型**（一次 120~180 秒，太慢且要真 GPU），
    而是把分析管线**替换成可控的桩**，验证：
      * 入库一定发生（产出正式 asset_id）
      * 预处理一定被调用
      * 预处理产物（描述 + 标签）**真的写进素材库**
      * 预处理失败 -> 明确报错 + 回滚，**不**返回裸 asset_id
    """

    def _register(self, tmp: Path):
        p = make_image(tmp / "pre.png")
        item = {"tmp": p, "filename": "pre.png", "suffix": ".png",
                "bytes": p.stat().st_size}
        entry = agent_media._register_one(item)
        p.unlink(missing_ok=True)
        return entry

    def test_upload_registers_into_asset_library(self):
        """上传 -> 素材库里有正式条目（含 asset_id）。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            entry = self._register(tmp)
            asset_id = entry["asset_id"]
            self.assertTrue(asset_id.startswith("asset_"))
            with asset_store.connect() as conn:
                asset = asset_repo.get_asset(conn, asset_id)
                self.assertIsNotNone(asset, "素材库里没有这条条目")
                self.assertEqual(asset["source"], "agent_upload")
                version = asset_repo.current_version(conn, asset_id)
                self.assertIsNotNone(version)
                # 元数据由 register_asset 抽取（尺寸）
                self.assertEqual(version["width"], 64)
                self.assertEqual(version["height"], 48)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_preprocess_writes_description_into_library(self):
        """预处理产出的描述**真的落在素材库**（不是只在内存里）。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            entry = self._register(tmp)
            asset_id = entry["asset_id"]

            fake_result = {
                "text": "**【场景与环境】**\n雨夜的街道，霓虹灯倒映在积水里。",
                "provider": {"id": "p1", "name": "测试模型", "model": "m"},
                "kind": "image", "prompt": "p", "frames": [],
                "extra": {"width": 64, "height": 48},
            }
            original = agent_media._run_analysis_sync
            agent_media._run_analysis_sync = lambda *a, **k: fake_result
            try:
                pre = agent_media._preprocess_asset(asset_id)
            finally:
                agent_media._run_analysis_sync = original

            self.assertTrue(pre["ok"], pre.get("error"))
            with asset_store.connect() as conn:
                analysis = asset_repo.latest_analysis(conn, asset_id)
                self.assertIsNotNone(analysis, "分析结果没落库")
                self.assertIn("雨夜的街道", analysis["text"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_preprocess_writes_tags_into_library(self):
        """预处理产出的标签**真的落在素材库**（可被 search_assets 检索）。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            entry = self._register(tmp)
            asset_id = entry["asset_id"]

            fake_result = {
                "text": ("**【场景与环境】**\n雨夜的街道，霓虹灯倒映在积水里。\n\n"
                         "**【光线与色彩】**\n冷色调为主，强烈的蓝紫色霓虹。"),
                "provider": {"id": "p1", "name": "测试模型", "model": "m"},
                "kind": "image", "prompt": "p", "frames": [], "extra": {},
            }
            original = agent_media._run_analysis_sync
            agent_media._run_analysis_sync = lambda *a, **k: fake_result
            try:
                pre = agent_media._preprocess_asset(asset_id)
            finally:
                agent_media._run_analysis_sync = original

            self.assertTrue(pre["ok"], pre.get("error"))
            self.assertTrue(pre["tags"], "没有抽出任何标签")
            with asset_store.connect() as conn:
                tags = [t["tag"] for t in conn.execute(
                    "SELECT tag FROM asset_tags WHERE asset_id=?", (asset_id,))]
                self.assertTrue(tags, "标签没落库")
                # **关键**：标签要能被检索命中（这才是「Agent 可检索」的意义）
                hit = asset_repo.search_assets(conn, {"q": tags[0],
                                                      "page_size": 5})
                self.assertGreaterEqual(hit.get("total", 0), 1,
                                        "标签检索不到这条素材")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_preprocess_failure_reports_and_rolls_back(self):
        """【约束 3】预处理失败 -> 明确报错 + 回滚，**不**返回裸 asset_id。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            entry = self._register(tmp)
            asset_id = entry["asset_id"]

            def boom(*a, **k):
                raise RuntimeError("模型调用失败")

            original = agent_media._run_analysis_sync
            agent_media._run_analysis_sync = boom
            try:
                pre = agent_media._preprocess_asset(asset_id)
            finally:
                agent_media._run_analysis_sync = original

            self.assertFalse(pre["ok"])
            self.assertIn("预处理失败", pre["error"])
            self.assertIn("模型调用失败", pre["error"])

            # 回滚后条目进回收站（不是留着当孤儿）
            agent_media._rollback_asset(asset_id)
            with asset_store.connect() as conn:
                asset = asset_repo.get_asset(conn, asset_id,
                                             include_deleted=True)
                self.assertIsNotNone(asset)
                self.assertIsNotNone(asset.get("deleted_at"),
                                     "预处理失败的素材没被回滚")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_rollback_uses_soft_delete(self):
        """回滚必须进回收站（可恢复），不能 purge。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            entry = self._register(tmp)
            asset_id = entry["asset_id"]
            self.assertTrue(agent_media._rollback_asset(asset_id))
            with asset_store.connect() as conn:
                asset = asset_repo.get_asset(conn, asset_id,
                                             include_deleted=True)
                self.assertIsNotNone(asset.get("deleted_at"))
                # 版本文件仍在（可恢复）
                version = asset_repo.current_version(conn, asset_id)
                self.assertIsNotNone(version)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_reupload_after_soft_delete_succeeds(self):
        """【回归 —— 生产实测踩到的 bug】

        素材库 register_asset 的去重分支原来**无条件**删掉临时文件，
        然后往下走 atomic_move(tmp, target) —— 而 tmp 已经被自己删了：

          FileNotFoundError: tmp -> target

        触发路径：上传同一张图 -> 软删除（进回收站，版本行还在）
        -> 再上传同一张图 -> 500。

        这条链路是「上传 -> 测试清理删掉 -> 再上传」的真实场景
        （本次浏览器验收的清理就会走这条路）。
        """
        from h3studio import asset_service

        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_image(tmp / "again.png")

            def register(name):
                with asset_store.connect() as conn:
                    return asset_service.register_asset(
                        conn, source_path=p, filename=name,
                        category="image", subtype="photo",
                        source="agent_upload", creator="agent")

            first = register("again1.png")
            first_id = first["asset"]["id"]
            # 软删除（进回收站；版本行仍在，sha256 仍能命中）
            with asset_store.connect() as conn:
                asset_repo.soft_delete(conn, first_id)

            # **关键**：再上传同一张图不能再 500
            second = register("again2.png")
            self.assertIsNotNone(second.get("asset"))
            self.assertNotEqual(second["asset"]["id"], first_id)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ 9. 标签抽取


class TestTagExtraction(unittest.TestCase):
    """标签抽取：从分析文本里取出**可检索**的关键词。"""

    def test_extracts_section_headings(self):
        text = "**【主体人物】**\n一位红衣女性。\n\n**【场景与环境】**\n雨夜街道。"
        tags = agent_media._extract_tags_from_description(text)
        self.assertIn("主体人物", tags)
        self.assertIn("场景与环境", tags)

    def test_extracts_section_headings_without_markdown_emphasis(self):
        """【回归 —— 生产实测踩到的 bug】

        同一个模型在两次调用里输出过**两种**小节格式：
          A: **【主体人物】**   （带 markdown 强调）
          B: 【主体人物】      （不带）
        原来的 pattern 要求结尾的 `**`，于是 B 格式**一个标题都匹配不到**
        -> sections 为空 -> 标签全空（上传返回 tags: []）。
        """
        text = ("【主体人物】\n无。\n\n"
                "【场景与环境】\n这是一个抽象的二维平面背景，由矩形色块拼接而成，"
                "呈现出网格状布局。")
        tags = agent_media._extract_tags_from_description(text)
        self.assertIn("主体人物", tags)
        self.assertIn("场景与环境", tags)
        # 内容词也要抽出来（不是只有标题）
        self.assertTrue(any(t not in ("主体人物", "场景与环境",
                                      "光线与色彩") for t in tags),
                        f"只抽到标题，没有内容词：{tags}")

    def test_both_formats_yield_similar_tags(self):
        """两种格式应该抽出**等价**的标签（不能一种有、一种没有）。"""
        body = "这是一个抽象的几何背景，由彩色方块拼接而成，呈现马赛克状布局。"
        with_md = agent_media._extract_tags_from_description(
            f"**【场景与环境】**\n{body}")
        without_md = agent_media._extract_tags_from_description(
            f"【场景与环境】\n{body}")
        self.assertEqual(set(with_md), set(without_md))

    def test_extracts_content_keywords_not_only_headings(self):
        """【关键】不能只抽小节标题 —— 那是固定结构名，检索价值≈0。

        10 张不同的小节标题完全一样，但「像素化」「马赛克」这类内容词
        才是能命中的标签。
        """
        text = ("**【场景与环境】**\n画面呈现为一个抽象的几何背景，"
                "由低分辨率的彩色方块网格构成（类似马赛克或像素化效果）。")
        tags = agent_media._extract_tags_from_description(text)
        self.assertTrue(tags)
        # 至少有一个内容词（不是只有标题）
        self.assertTrue(any(t not in ("主体人物", "场景与环境", "光线与色彩")
                            for t in tags),
                        f"只抽到了小节标题，没有内容词：{tags}")

    def test_drops_boilerplate_openers(self):
        text = ("基于您提供的图片素材 `a.png`，以下是详尽的中文描述：\n\n"
                "**【主体人物】**\n无。")
        tags = agent_media._extract_tags_from_description(text)
        self.assertFalse(any(t.startswith("以下") for t in tags), tags)
        self.assertFalse(any(t.startswith("基于") for t in tags), tags)

    def test_empty_text_yields_no_tags(self):
        self.assertEqual(agent_media._extract_tags_from_description(""), [])

    def test_tags_are_deduplicated_and_limited(self):
        text = ("**【场景与环境】**\n雨夜街道，雨夜街道，雨夜街道。\n"
                "**【主体人物】**\n无。")
        tags = agent_media._extract_tags_from_description(text, limit=8)
        self.assertEqual(len(tags), len(set(tags)))
        self.assertLessEqual(len(tags), 8)


# ------------------------------------------------------------------ 10. 失效条目


class TestInvalidAttachmentEntry(AssetLibraryTestCase):
    """【约束 2】条目失效要有明确提示，且不能当参考图用。"""

    def test_missing_entry_is_marked_and_excluded_from_refs(self):
        ctx = agent_mod.AgentContext(server_module=None, provider={})
        entries = agent_mod._load_attachments(ctx, ["asset_nonexistent"])
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["missing"])
        self.assertTrue(entries[0]["reason"])
        # **失效条目不能作为参考图**
        self.assertEqual(agent_mod._attachment_asset_ids(entries), [])

    def test_deleted_entry_is_marked_and_excluded(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_image(tmp / "d.png")
            result = self.register(p)
            asset_id = result["asset"]["id"]
            with asset_store.connect() as conn:
                asset_repo.soft_delete(conn, asset_id)

            ctx = agent_mod.AgentContext(server_module=None, provider={})
            entries = agent_mod._load_attachments(ctx, [asset_id])
            self.assertTrue(entries[0]["missing"])
            self.assertEqual(agent_mod._attachment_asset_ids(entries), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_valid_entry_is_usable_and_included(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_image(tmp / "v.png")
            result = self.register(p)
            asset_id = result["asset"]["id"]
            ctx = agent_mod.AgentContext(server_module=None, provider={})
            entries = agent_mod._load_attachments(ctx, [asset_id])
            self.assertFalse(entries[0]["missing"])
            self.assertEqual(agent_mod._attachment_asset_ids(entries), [asset_id])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_prompt_block_marks_invalid_entry_as_unusable(self):
        """提示块必须明确说「不可用」，模型才知道不能拿它当参考图。"""
        ctx = agent_mod.AgentContext(server_module=None, provider={})
        entries = agent_mod._load_attachments(ctx, ["asset_nonexistent"])
        block = agent_mod._attachment_prompt_block(entries)
        self.assertIn("不可用", block)
        self.assertIn("失效", block)

    def test_prompt_block_includes_tags(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_image(tmp / "t.png")
            result = self.register(p)
            asset_id = result["asset"]["id"]
            with asset_store.connect() as conn:
                asset_repo.add_tags(conn, asset_id, ["雨夜", "霓虹"])

            ctx = agent_mod.AgentContext(server_module=None, provider={})
            entries = agent_mod._load_attachments(ctx, [asset_id])
            block = agent_mod._attachment_prompt_block(entries)
            self.assertIn("雨夜", block)
            self.assertIn("霓虹", block)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ 11. 既有两项约束


class TestPriorConstraintsStillHold(unittest.TestCase):
    """【约束 5】不得因本次改造放宽既有修复。"""

    def test_fabricated_job_id_blocking_intact(self):
        source = Path(agent_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("fabricated_job_id", source)
        self.assertTrue(hasattr(agent_mod, "_job_id_matches_any"))

    def test_truncated_neutral_notice_intact(self):
        source = Path(agent_mod.__file__).read_text(encoding="utf-8")
        self.assertIn('"notice": notice', source)

    def test_upload_does_not_touch_fabricated_or_truncated_logic(self):
        """agent_media 不该改动那两条判定（它只管上传与入库）。"""
        source = Path(agent_media.__file__).read_text(encoding="utf-8")
        self.assertNotIn("fabricated_job_id", source)
        self.assertNotIn("no_tool_call", source)

    def test_agent_media_has_no_comfyui_dependency(self):
        """上传/预处理**不许**自己跑 ComfyUI（走素材库既有管线）。"""
        source = Path(agent_media.__file__).read_text(encoding="utf-8")
        for forbidden in ("comfy", "COMFY", "workflow"):
            self.assertNotIn(forbidden, source,
                             f"agent_media 不该出现 {forbidden}")

    def test_preprocess_uses_library_analysis_pipeline(self):
        """预处理必须复用素材库的分析管线（analysis_mod.run_analysis）。"""
        source = Path(agent_media.__file__).read_text(encoding="utf-8")
        self.assertIn("analysis_mod.run_analysis", source)

    def test_max_images_still_nine(self):
        self.assertEqual(agent_media.MAX_IMAGES_PER_UPLOAD, 9)
        self.assertEqual(agent_mod.MAX_ATTACHMENTS, 9)


if __name__ == "__main__":
    unittest.main()
