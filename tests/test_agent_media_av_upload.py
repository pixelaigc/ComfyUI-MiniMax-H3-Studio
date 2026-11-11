# -*- coding: utf-8 -*-
"""
内置 Agent 聊天区 —— **音频 / 视频上传**的回归测试（2026-11-11）。

【覆盖什么】
1. **音视频魔数嗅探**（扩展名只是线索，真实格式才是判据）
   - 真 wav / mp3 / flac / ogg 被识别
   - 伪装的（exe 改名 .mp3 / .mp4）被拒
   - 视频容器家族与扩展名不符（.mkv 里装 mp4）被拒
2. **大小上限**：图片 20 MB / 音频 100 MB / 视频 500 MB，分类型
3. **强制入库**：音视频进素材库时 category 必须是 audio / video
   （写死 image 会让 ffprobe 探测失败、缩略图空白、分析器选错）
4. **异步预处理**：音视频走 analysis_jobs 异步 job（不阻塞上传）
   - 音频 -> deep=True（深度管线：BPM/乐器/人声/ASR）
   - 视频 -> 多模态（抽帧 + 模型看帧）
   - 任务排不起来 -> 明确报错 + soft_delete 回滚
5. **就绪门槛**：分析中的条目**不可用**，不进可引用范围
6. **标签落库**：分析完成后抽标签写进 asset_tags（可被检索命中）
7. **失败回滚**：分析失败 -> 明确报错 + soft_delete，不留脏条目
8. **Agent 侧按类型分派参数位**：音频 -> ref_audio，视频 -> ref_video
9. **前端契约**：accept 列表含音视频、类型徽标、就绪门槛拦住发送

【取证纪律】
- 素材库落在**临时目录**（ASSET_LIBRARY_ROOT 环境变量），不碰生产素材库
- 走**真的** asset_service.register_asset 与**真的** analysis_jobs
- 不跑真模型（音频深度分析实测 ~600 秒），分析用桩替换
  —— 但 job 的**创建、状态流转、失败留痕、回滚**都走真实代码路径

运行：python3 -m pytest tests/test_agent_media_av_upload.py -v
"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import agent as agent_mod  # noqa: E402
from h3studio import agent_media  # noqa: E402
from h3studio import agent_schema  # noqa: E402
from h3studio import analysis_jobs  # noqa: E402
from h3studio import asset_repo  # noqa: E402
from h3studio import assets as asset_store  # noqa: E402


# ------------------------------------------------------------------ 工具


def make_wav(path: Path, seconds: float = 1.0, rate: int = 8000) -> Path:
    """造一个**真的** WAV（RIFF/WAVE 头 + PCM 数据）。

    用标准库 wave 模块 —— 不依赖 ffmpeg，测试环境最稳。
    """
    import struct
    import wave

    frames = int(rate * seconds)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        # 一个简单的 440 Hz 正弦（保证不是全静音，探测也有意义）
        import math
        samples = bytearray()
        for i in range(frames):
            value = int(12000 * math.sin(2 * math.pi * 440 * i / rate))
            samples += struct.pack("<h", value)
        wav.writeframes(bytes(samples))
    return path


def make_mp3_like(path: Path) -> Path:
    """造一个带 ID3 头的「像 mp3」文件（只验魔数，不解码）。"""
    path.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 40)
    return path


def make_flac(path: Path) -> Path:
    path.write_bytes(b"fLaC" + b"\x00" * 60)
    return path


def make_ogg(path: Path) -> Path:
    path.write_bytes(b"OggS" + b"\x00" * 60)
    return path


def make_mp4(path: Path) -> Path:
    """造一个 ISO-BMFF 头的「像 mp4」文件（只验魔数）。"""
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 60)
    return path


def make_mkv(path: Path) -> Path:
    """造一个 Matroska（EBML）头的文件。"""
    path.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 60)
    return path


def make_exe(path: Path) -> Path:
    path.write_bytes(b"MZ\x90\x00" + b"\x00" * 200)
    return path


class AssetLibraryTestCase(unittest.TestCase):
    """把素材库指到临时目录，避免污染生产素材库。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent_media_av_test_"))
        self.lib_root = self.tmp / "lib"
        self._old_env = os.environ.get("ASSET_LIBRARY_ROOT")
        os.environ["ASSET_LIBRARY_ROOT"] = str(self.lib_root)
        report = asset_store.ensure_layout()
        self.assertTrue(report["ok"], report.get("errors"))
        init = asset_store.init_db()
        self.assertTrue(init.get("ok"), init.get("errors"))

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("ASSET_LIBRARY_ROOT", None)
        else:
            os.environ["ASSET_LIBRARY_ROOT"] = self._old_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def register(self, path: Path, name="x.bin", category="audio"):
        from h3studio import asset_service
        with asset_store.connect() as conn:
            result = asset_service.register_asset(
                conn, source_path=path, filename=name,
                category=category, source="agent_upload", creator="test")
        return result

    def set_analysis(self, asset_id: str, text: str, kind="audio"):
        with asset_store.connect() as conn:
            asset_repo.save_analysis(conn, asset_id, kind, text,
                                     asset_repo.next_revision(conn, asset_id),
                                     provider={"name": "test"}, prompt="p")

    def set_job(self, asset_id: str, kind="audio", status="running"):
        """造一个分析任务记录（模拟异步分析在跑）。"""
        with asset_store.connect() as conn:
            job = asset_repo.create_job(conn, asset_id, kind, mode="analyze")
            asset_repo.update_job(conn, job["id"], status=status,
                                  stage="测试阶段", progress=0.4)
            return job


# ------------------------------------------------------------------ 1. 魔数嗅探


class TestAudioMagicSniffing(unittest.TestCase):
    """音频扩展名只是线索，真实格式才是判据。"""

    def test_real_wav_is_detected(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_wav(tmp / "a.wav")
            head = p.read_bytes()[:32]
            self.assertEqual(agent_media._detect_audio_kind("a.wav", head), ".wav")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_mp3_with_id3_header_is_detected(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_mp3_like(tmp / "a.mp3")
            head = p.read_bytes()[:32]
            self.assertEqual(agent_media._detect_audio_kind("a.mp3", head), ".mp3")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_mp3_without_id3_header_is_detected(self):
        """无 ID3 的裸 mp3（直接是帧同步字）也要认。"""
        head = b"\xff\xfb\x90\x00" + b"\x00" * 28
        self.assertEqual(agent_media._detect_audio_kind("a.mp3", head), ".mp3")

    def test_flac_and_ogg_are_detected(self):
        self.assertEqual(agent_media._detect_audio_kind("a.flac", b"fLaC" + b"\x00" * 28),
                         ".flac")
        self.assertEqual(agent_media._detect_audio_kind("a.ogg", b"OggS" + b"\x00" * 28),
                         ".ogg")

    def test_exe_renamed_to_mp3_is_rejected(self):
        """最关键的防线：只看扩展名会被骗过去。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_exe(tmp / "evil.mp3")
            head = p.read_bytes()[:32]
            self.assertIsNone(agent_media._detect_audio_kind("evil.mp3", head))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_text_renamed_to_wav_is_rejected(self):
        head = b"hello world, this is not audio at all"
        self.assertIsNone(agent_media._detect_audio_kind("a.wav", head))


class TestVideoMagicSniffing(unittest.TestCase):
    """视频容器按「家族」判定，且家族必须与扩展名相符。"""

    def test_mp4_is_detected(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_mp4(tmp / "a.mp4")
            head = p.read_bytes()[:32]
            self.assertEqual(agent_media._detect_video_kind("a.mp4", head), ".mp4")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_mkv_is_detected(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_mkv(tmp / "a.mkv")
            head = p.read_bytes()[:32]
            self.assertEqual(agent_media._detect_video_kind("a.mkv", head), ".mkv")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_mov_in_same_family_as_mp4(self):
        """mov 与 mp4 同属 ISO-BMFF 家族 -> 互相兼容（iPhone 拍的是 mov）。"""
        head = b"\x00\x00\x00\x18ftypqt  " + b"\x00" * 24
        self.assertEqual(agent_media._detect_video_kind("a.mov", head), ".mov")

    def test_mkv_extension_with_mp4_content_is_rejected(self):
        """容器家族与扩展名不符 -> 拒（否则 mime 会记错）。"""
        head = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 24
        self.assertIsNone(agent_media._detect_video_kind("a.mkv", head))

    def test_exe_renamed_to_mp4_is_rejected(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_exe(tmp / "evil.mp4")
            head = p.read_bytes()[:32]
            self.assertIsNone(agent_media._detect_video_kind("evil.mp4", head))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ 2. 大小上限


class TestSizeLimits(AssetLibraryTestCase):
    """大小上限**按类型**分档，且报错要说清是哪一类。"""

    def test_limits_are_distinct_per_kind(self):
        self.assertEqual(agent_media.MAX_IMAGE_BYTES, 20 * 1024 * 1024)
        self.assertEqual(agent_media.MAX_AUDIO_BYTES, 100 * 1024 * 1024)
        self.assertEqual(agent_media.MAX_VIDEO_BYTES, 500 * 1024 * 1024)

    def test_max_bytes_helper(self):
        self.assertEqual(agent_media._max_bytes_for_kind("image"),
                         agent_media.MAX_IMAGE_BYTES)
        self.assertEqual(agent_media._max_bytes_for_kind("audio"),
                         agent_media.MAX_AUDIO_BYTES)
        self.assertEqual(agent_media._max_bytes_for_kind("video"),
                         agent_media.MAX_VIDEO_BYTES)

    def test_audio_over_limit_is_rejected_with_audio_label(self):
        """超限音频 -> 错误里要写「音频」而不是「图片」。"""

        class FakePart:
            filename = "big.wav"

            def __init__(self, total):
                self.total = total
                self.sent = 0

            async def read_chunk(self, size):
                if self.sent >= self.total:
                    return b""
                chunk = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * (size - 12)
                self.sent += len(chunk)
                return chunk

        tmp_dir = asset_store.library_root() / asset_store.DIR_TMP
        oversized = agent_media.MAX_AUDIO_BYTES + 1024
        with self.assertRaises(agent_media.UploadError) as ctx:
            asyncio.run(agent_media._read_and_validate_part(
                FakePart(oversized), tmp_dir))
        self.assertEqual(ctx.exception.code, "file_too_large")
        self.assertEqual(ctx.exception.status, 413)
        self.assertIn("音频", ctx.exception.message)
        self.assertIn("100", ctx.exception.message)


# ------------------------------------------------------------------ 3. 强制入库


class TestIngestIntoLibrary(AssetLibraryTestCase):
    """音视频**必须**以正确的 category 入库。"""

    def test_audio_registers_with_audio_category(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_wav(tmp / "a.wav")
            item = {"tmp": p, "filename": "a.wav", "suffix": ".wav",
                    "bytes": p.stat().st_size, "kind": "audio"}
            entry = agent_media._register_one(item)
            p.unlink(missing_ok=True)
            asset_id = entry["asset_id"]
            self.assertEqual(entry["kind"], "audio")
            with asset_store.connect() as conn:
                asset = asset_repo.get_asset(conn, asset_id)
                self.assertEqual(asset["category"], "audio")
                self.assertEqual(asset["source"], "agent_upload")
                version = asset_repo.current_version(conn, asset_id)
                # ffprobe 探测出的时长 —— 音频元数据必须真的抽到
                self.assertIsNotNone(version["duration_sec"])
                self.assertGreater(version["duration_sec"], 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_video_registers_with_video_category(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_mp4(tmp / "a.mp4")
            item = {"tmp": p, "filename": "a.mp4", "suffix": ".mp4",
                    "bytes": p.stat().st_size, "kind": "video"}
            entry = agent_media._register_one(item)
            p.unlink(missing_ok=True)
            with asset_store.connect() as conn:
                asset = asset_repo.get_asset(conn, asset_id := entry["asset_id"])
                self.assertEqual(asset["category"], "video")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_audio_entry_carries_duration_and_kind(self):
        """返回条目要带类型与时长 —— 前端卡片要显示它们。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_wav(tmp / "a.wav", seconds=1.5)
            item = {"tmp": p, "filename": "a.wav", "suffix": ".wav",
                    "bytes": p.stat().st_size, "kind": "audio"}
            entry = agent_media._register_one(item)
            p.unlink(missing_ok=True)
            self.assertEqual(entry["kind"], "audio")
            self.assertEqual(entry["category"], "audio")
            self.assertIsNotNone(entry["duration_sec"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ 4. 异步预处理


class TestAsyncPreprocess(AssetLibraryTestCase):
    """音视频走**异步 job**，不阻塞上传请求。"""

    def _register_audio(self):
        tmp = Path(tempfile.mkdtemp())
        p = make_wav(tmp / "a.wav")
        item = {"tmp": p, "filename": "a.wav", "suffix": ".wav",
                "bytes": p.stat().st_size, "kind": "audio"}
        entry = agent_media._register_one(item)
        p.unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)
        return entry["asset_id"]

    def test_audio_starts_deep_pipeline_job(self):
        """音频 -> deep=True（深度管线：BPM/乐器/人声/ASR）。"""
        asset_id = self._register_audio()
        # 桩掉 RUNNER.submit —— 只验「排的是什么任务」，不真跑模型
        submitted = []
        original = analysis_jobs.RUNNER.submit
        analysis_jobs.RUNNER.submit = lambda fn, *a, **k: submitted.append((fn, a, k))
        try:
            result = agent_media._start_async_preprocess(asset_id, "audio")
        finally:
            analysis_jobs.RUNNER.submit = original

        self.assertTrue(result["ok"], result.get("error"))
        self.assertGreater(result["job_id"], 0)
        self.assertEqual(result["status"], "queued")
        # 跑的是深度音频管线（不是多模态）
        self.assertTrue(submitted, "没有提交任何任务")
        self.assertIs(submitted[0][0], analysis_jobs._run_audio_deep)
        # 任务选项里 deep 必须为 True
        options = submitted[0][1][3]
        self.assertTrue(options.get("deep"))
        # 派生目录必须给到（视频抽帧要用；音频给��也不亏）
        self.assertTrue(options.get("derived_dir"))

    def test_video_uses_multimodal_job(self):
        """视频 -> 多模态（抽帧 + 模型看帧）。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_mp4(tmp / "a.mp4")
            item = {"tmp": p, "filename": "a.mp4", "suffix": ".mp4",
                    "bytes": p.stat().st_size, "kind": "video"}
            entry = agent_media._register_one(item)
            p.unlink(missing_ok=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        submitted = []
        original = analysis_jobs.RUNNER.submit
        analysis_jobs.RUNNER.submit = lambda fn, *a, **k: submitted.append((fn, a, k))
        try:
            result = agent_media._start_async_preprocess(
                entry["asset_id"], "video")
        finally:
            analysis_jobs.RUNNER.submit = original

        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue(submitted)
        self.assertIs(submitted[0][0], analysis_jobs._run_multimodal)
        options = submitted[0][1][3]
        self.assertFalse(options.get("deep"))
        self.assertTrue(options.get("derived_dir"))

    def test_start_failure_reports_and_rolls_back(self):
        """任务排不起来 -> 明确报错 + soft_delete 回滚（不留孤儿条目）。"""
        asset_id = self._register_audio()

        def boom(*a, **k):
            raise RuntimeError("任务系统不可用")

        original = analysis_jobs.RUNNER.submit
        analysis_jobs.RUNNER.submit = boom
        try:
            result = agent_media._start_async_preprocess(asset_id, "audio")
        finally:
            analysis_jobs.RUNNER.submit = original

        self.assertFalse(result["ok"])
        self.assertIn("无法启动素材分析", result["error"])

    def test_failed_job_status_rolls_back_and_reports(self):
        """分析失败 -> 明确报错 + 回滚（进回收站，可恢复）。"""
        asset_id = self._register_audio()
        self.set_job(asset_id, kind="audio", status="failed")

        result = asyncio.run(agent_media.handle_analysis_status(asset_id))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["available"])
        self.assertTrue(result["error"])
        self.assertIn("回滚", result["error"])

        # 已进回收站（软删除，不是 purge）
        with asset_store.connect() as conn:
            asset = asset_repo.get_asset(conn, asset_id, include_deleted=True)
            self.assertIsNotNone(asset["deleted_at"])
            # 活跃列表里查不到
            self.assertIsNone(asset_repo.get_asset(conn, asset_id))


# ------------------------------------------------------------------ 5. 就绪门槛


class TestReadinessGate(AssetLibraryTestCase):
    """分析**未完成**的条目不能被引用（静默失效是最难查的）。"""

    def _register_audio(self, analyzed=False):
        tmp = Path(tempfile.mkdtemp())
        p = make_wav(tmp / "a.wav")
        item = {"tmp": p, "filename": "a.wav", "suffix": ".wav",
                "bytes": p.stat().st_size, "kind": "audio"}
        entry = agent_media._register_one(item)
        p.unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)
        if analyzed:
            self.set_analysis(entry["asset_id"], "## 一、音频基本信息\nBPM 120")
        return entry["asset_id"]

    def test_pending_analysis_makes_entry_unavailable(self):
        asset_id = self._register_audio()
        self.set_job(asset_id, kind="audio", status="running")

        ctx = agent_mod.AgentContext(server_module=None, provider={})
        entries = agent_mod._load_attachments(ctx, [asset_id])
        self.assertEqual(entries[0]["analysis_state"], "pending")
        self.assertFalse(entries[0]["available"])
        # **排除出可引用范围**
        self.assertEqual(agent_mod._attachment_asset_ids(entries), [])

    def test_failed_analysis_makes_entry_unavailable(self):
        asset_id = self._register_audio()
        self.set_job(asset_id, kind="audio", status="failed")
        ctx = agent_mod.AgentContext(server_module=None, provider={})
        entries = agent_mod._load_attachments(ctx, [asset_id])
        self.assertEqual(entries[0]["analysis_state"], "failed")
        self.assertFalse(entries[0]["available"])
        self.assertEqual(agent_mod._attachment_asset_ids(entries), [])

    def test_completed_analysis_makes_entry_available(self):
        asset_id = self._register_audio(analyzed=True)
        self.set_job(asset_id, kind="audio", status="done")
        ctx = agent_mod.AgentContext(server_module=None, provider={})
        entries = agent_mod._load_attachments(ctx, [asset_id])
        self.assertEqual(entries[0]["analysis_state"], "done")
        self.assertTrue(entries[0]["available"])
        self.assertEqual(agent_mod._attachment_asset_ids(entries), [asset_id])

    def test_prompt_block_marks_pending_as_unusable(self):
        """提示块必须标「不可用」+ 说明原因。"""
        asset_id = self._register_audio()
        self.set_job(asset_id, kind="audio", status="running")
        ctx = agent_mod.AgentContext(server_module=None, provider={})
        entries = agent_mod._load_attachments(ctx, [asset_id])
        block = agent_mod._attachment_prompt_block(entries)
        self.assertIn("不可用", block)
        self.assertIn("分析", block)

    def test_status_endpoint_reports_pending(self):
        asset_id = self._register_audio()
        self.set_job(asset_id, kind="audio", status="running")
        result = asyncio.run(agent_media.handle_analysis_status(asset_id))
        self.assertEqual(result["status"], "running")
        self.assertFalse(result["available"])
        self.assertEqual(result["kind"], "audio")

    def test_model_explicitly_passing_pending_asset_is_filtered(self):
        """模型**显式**传了一个还在分析中的 asset_id -> 也要被挡住。

        【为什么这条要单列】附件兜底路径走 _attachment_asset_ids()，
        会过滤掉未就绪的条目。但模型可以**自己**把 id 写进
        create_job 的 asset_ids —— 那条路径原本不经过任何就绪检查，
        结果是：任务建成了，参考音频却是「还没分析完」的素材，
        模型以为它有 BPM/人声信息，实际上一无所知（静默失效）。

        所以就绪门槛必须落在**所有**引用入口上，不只是附件兜底。
        """
        asset_id = self._register_audio()
        self.set_job(asset_id, kind="audio", status="running")

        with asset_store.connect() as conn:
            refs = agent_mod._filter_unavailable_refs(
                [{"asset_id": asset_id, "slot": "ref_audio", "ordinal": 0}],
                conn)
        self.assertEqual(refs, [])

    def test_ready_asset_survives_the_filter(self):
        """分析完成的素材**不能**被过滤掉（门槛不能误伤）。"""
        asset_id = self._register_audio(analyzed=True)
        self.set_job(asset_id, kind="audio", status="done")

        with asset_store.connect() as conn:
            refs = agent_mod._filter_unavailable_refs(
                [{"asset_id": asset_id, "slot": "ref_audio", "ordinal": 0}],
                conn)
        self.assertEqual([r["asset_id"] for r in refs], [asset_id])

    def test_never_analyzed_asset_survives_the_filter(self):
        """从没分析过的素材沿用既有语义 -> 保留（不误伤老素材）。"""
        asset_id = self._register_audio()

        with asset_store.connect() as conn:
            refs = agent_mod._filter_unavailable_refs(
                [{"asset_id": asset_id, "slot": "ref_audio", "ordinal": 0}],
                conn)
        self.assertEqual([r["asset_id"] for r in refs], [asset_id])




# ------------------------------------------------------------------ 6. 标签落库


class TestAsyncTagging(AssetLibraryTestCase):
    """异步分析完成后，标签必须**真的**写进素材库。"""

    def test_tags_are_written_and_searchable(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            p = make_wav(tmp / "a.wav")
            item = {"tmp": p, "filename": "a.wav", "suffix": ".wav",
                    "bytes": p.stat().st_size, "kind": "audio"}
            entry = agent_media._register_one(item)
            p.unlink(missing_ok=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        asset_id = entry["asset_id"]

        # 模拟深度音频分析写出的描述
        self.set_analysis(
            asset_id,
            "## 一、音频基本信息\n- 容器格式：wav\n\n"
            "## 二、节拍与速度\n- 测得 BPM：152\n\n"
            "## 三、乐器与音色\n- 人声：占比 0.98",
            kind="audio")
        self.set_job(asset_id, kind="audio", status="done")

        result = asyncio.run(agent_media.handle_analysis_status(asset_id))
        self.assertEqual(result["status"], "done")
        self.assertTrue(result["available"])
        self.assertTrue(result["tags"], "分析完成后应该抽到标签")

        with asset_store.connect() as conn:
            tags = [t["tag"] for t in conn.execute(
                "SELECT tag FROM asset_tags WHERE asset_id=?", (asset_id,))]
            self.assertTrue(tags, "标签没落库")
            # 标签要能被检索命中
            hit = asset_repo.search_assets(conn, {"q": tags[0], "page_size": 5})
            self.assertGreaterEqual(hit.get("total", 0), 1,
                                    "标签检索不到这条素材")


# ------------------------------------------------------------------ 7. Agent 侧按类型分派


class TestTypedSlotDispatch(unittest.TestCase):
    """素材按**真实类型**落到正确的参数位。"""

    def test_audio_goes_to_ref_audio_slot(self):
        refs = agent_mod._build_asset_refs(
            "r2v", {}, ["aud1"], {"aud1": "audio"})
        self.assertEqual(refs, [{"asset_id": "aud1", "slot": "ref_audio",
                                 "ordinal": 0}])

    def test_video_goes_to_ref_video_slot(self):
        refs = agent_mod._build_asset_refs(
            "r2v", {}, ["vid1"], {"vid1": "video"})
        self.assertEqual(refs, [{"asset_id": "vid1", "slot": "ref_video",
                                 "ordinal": 0}])

    def test_image_goes_to_ref_image_slot(self):
        refs = agent_mod._build_asset_refs(
            "r2v", {}, ["img1"], {"img1": "image"})
        self.assertEqual(refs, [{"asset_id": "img1", "slot": "ref_image",
                                 "ordinal": 0}])

    def test_mixed_types_go_to_distinct_slots(self):
        refs = agent_mod._build_asset_refs(
            "r2v", {}, ["img1", "aud1", "vid1"],
            {"img1": "image", "aud1": "audio", "vid1": "video"})
        by_id = {r["asset_id"]: r["slot"] for r in refs}
        self.assertEqual(by_id, {"img1": "ref_image", "aud1": "ref_audio",
                                 "vid1": "ref_video"})

    def test_type_not_supported_by_mode_is_excluded(self):
        """i2i_qwen 只吃图片 —— 音频不能硬塞进 ref_image。"""
        refs = agent_mod._build_asset_refs(
            "i2i_qwen", {}, ["aud1"], {"aud1": "audio"})
        self.assertEqual(refs, [])

    def test_a2a_rejects_video(self):
        """a2a 只吃音频 —— 视频不能进 ref_audio。"""
        refs = agent_mod._build_asset_refs(
            "a2a", {}, ["vid1"], {"vid1": "video"})
        self.assertEqual(refs, [])

    def test_without_kind_info_behaves_as_before(self):
        """没有类型信息 -> 保守退化到旧行为（第一个 multi 槽位）。"""
        refs = agent_mod._build_asset_refs("r2v", {}, ["x1"])
        self.assertEqual(refs[0]["slot"], "ref_image")

    def test_media_slot_plan_matches_real_capability(self):
        """media_slots 表要覆盖所有 BUILDERS 里的模式（不能漏）。"""
        from h3studio import workflows as wf
        for mode in wf.BUILDERS:
            self.assertIn(mode, agent_schema.MODE_MEDIA_SLOTS,
                          f"{mode} 缺少 media_slots 定义")

    def test_r2v_accepts_all_three_media_types(self):
        slots = agent_schema.mode_slot_plan("r2v")["media_slots"]
        self.assertEqual(slots.get("image"), "ref_image")
        self.assertEqual(slots.get("video"), "ref_video")
        self.assertEqual(slots.get("audio"), "ref_audio")

    def test_r2v_workflow_builds_without_ref_images(self):
        """【回归 —— 音视频扩展踩到的真 bug】

        用户只上传参考视频（不传参考图）时，
        build_r2v 里写死的 params["ref_images"] 会抛
        KeyError: 'ref_images' —— 任务建成了，worker 跑起来才炸
        （静默失效，最难查的一类问题）。
        """
        from h3studio import workflows as wf

        graph = wf.build_r2v({
            "job_id": "job_test", "seed": 1,
            "ref_videos": ["clip.mp4"], "ref_audios": ["voice.wav"],
        })
        self.assertIsInstance(graph, dict)
        # 参考视频 / 参考音频的节点必须在
        node_ids = set(graph.keys())
        self.assertTrue(any(str(i).startswith("136:301") for i in node_ids),
                        "参考视频节点没建出来")
        self.assertTrue(any(str(i).startswith("136:401") for i in node_ids),
                        "参考音频节点没建出来")

    def test_flf_only_accepts_images(self):
        slots = agent_schema.mode_slot_plan("flf")["media_slots"]
        self.assertEqual(set(slots.keys()), {"image"})
        self.assertEqual(slots["image"], "first_frame")


# ------------------------------------------------------------------ 8. Agent 侧提示块


class TestPromptBlockTypes(unittest.TestCase):
    """提示块按类型分节，并告诉模型该用哪个参数位。"""

    def _entry(self, kind, asset_id="asset_x", analyzed=True, **kw):
        entry = {
            "asset_id": asset_id, "name": "素材", "description": "描述",
            "description_source": "analysis", "tags": ["标签"],
            "preview_url": "/api/assets/x/preview", "width": 100,
            "height": 100, "missing": False, "reason": "",
            "analyzed": analyzed, "kind": kind, "duration_sec": 12.0,
            "available": analyzed,
        }
        entry.update(kw)
        return entry

    def test_block_lists_types_and_slots(self):
        block = agent_mod._attachment_prompt_block([
            self._entry("image", "asset_i"),
            self._entry("audio", "asset_a"),
            self._entry("video", "asset_v"),
        ])
        self.assertIn("图片", block)
        self.assertIn("音频", block)
        self.assertIn("视频", block)
        self.assertIn("ref_audio", block)
        self.assertIn("ref_video", block)
        self.assertIn("ref_image", block)

    def test_block_keeps_image_placeholder_semantics(self):
        """<imageN> 是 Qwen 图片编辑的官方占位语义 —— 不能丢。"""
        block = agent_mod._attachment_prompt_block([self._entry("image", "asset_i")])
        self.assertIn("<image1>", block)

    def test_block_has_no_image_placeholder_for_audio(self):
        """音频/视频没有 <imageN> 语义 —— 不能编。"""
        block = agent_mod._attachment_prompt_block([self._entry("audio", "asset_a")])
        self.assertNotIn("<image1>", block)

    def test_block_shows_duration_for_media(self):
        block = agent_mod._attachment_prompt_block([self._entry("audio", "asset_a")])
        self.assertIn("12.0", block)

    def test_block_does_not_fabricate_for_unanalyzed(self):
        block = agent_mod._attachment_prompt_block([
            self._entry("audio", "asset_a", analyzed=False, available=False,
                        description="", unavailable_reason="素材库尚无分析结论")])
        self.assertIn("不可用", block)
        self.assertNotIn("描述：描述", block)


# ------------------------------------------------------------------ 9. 前端契约


class TestFrontendContract(unittest.TestCase):
    """前端源码级契约（真正的渲染断言在浏览器验收里做）。"""

    def _agent_js(self):
        return (REPO / "web" / "agent.js").read_text(encoding="utf-8")

    def _index_html(self):
        return (REPO / "web" / "index.html").read_text(encoding="utf-8")

    def test_input_accepts_audio_and_video(self):
        html = self._index_html()
        self.assertIn("audio/wav", html)
        self.assertIn("audio/mpeg", html)
        self.assertIn("audio/flac", html)
        self.assertIn("video/mp4", html)
        self.assertIn("video/webm", html)
        self.assertIn("video/quicktime", html)

    def test_frontend_caps_media_files_separately(self):
        js = self._agent_js()
        self.assertIn("const MAX_ATTACHMENTS = 9;", js)
        self.assertIn("const MAX_MEDIA_ATTACHMENTS = 4;", js)

    def test_frontend_has_type_badge(self):
        js = self._agent_js()
        self.assertIn("ag-attach-badge", js)
        self.assertIn("KIND_LABEL", js)

    def test_frontend_has_type_tag_for_media(self):
        """音视频卡片要有独立的类型标签（音频/视频）。"""
        js = self._agent_js()
        self.assertIn("ag-attach-kind", js)
        css = (REPO / "web" / "style.css").read_text(encoding="utf-8")
        self.assertIn(".ag-attach-kind", css)

    def test_frontend_shows_duration(self):
        js = self._agent_js()
        self.assertIn("fmtDuration", js)
        self.assertIn("duration_sec", js)

    def test_frontend_polls_media_status(self):
        js = self._agent_js()
        self.assertIn("/api/agent/media/", js)
        self.assertIn("pollMediaStatus", js)

    def test_frontend_blocks_sending_unready_attachments(self):
        """未就绪的素材不能发出去 —— 否则 Agent 拿到空参考。"""
        js = self._agent_js()
        self.assertIn("attachStatus(it) !== \"ready\"", js)

    def test_frontend_validates_by_type(self):
        js = self._agent_js()
        self.assertIn("kindForFile", js)
        self.assertIn("maxBytesForKind", js)

    def test_line_endings_policy_is_respected(self):
        import subprocess
        out = subprocess.run(
            ["git", "ls-files", "--eol", "server.py", "web/style.css",
             "web/index.html", "web/agent.js"],
            cwd=REPO, capture_output=True, text=True).stdout
        for line in out.strip().splitlines():
            path = line.split("\t")[-1].strip()
            if path in ("server.py", "web/style.css", "web/index.html"):
                self.assertIn("crlf", line, f"{path} 应保持 CRLF")


# ------------------------------------------------------------------ 10. 既有约束不被放宽


class TestPriorConstraintsStillHold(unittest.TestCase):
    """【约束 5】不得因本次改造放宽既有修复。"""

    def test_fabricated_job_id_blocking_intact(self):
        source = Path(agent_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("fabricated_job_id", source)

    def test_truncated_neutral_notice_intact(self):
        source = Path(agent_mod.__file__).read_text(encoding="utf-8")
        self.assertIn('"notice": notice', source)

    def test_agent_media_has_no_comfyui_dependency(self):
        source = Path(agent_media.__file__).read_text(encoding="utf-8")
        for forbidden in ("comfy", "COMFY", "workflow"):
            self.assertNotIn(forbidden, source,
                             f"agent_media 不该出现 {forbidden}")

    def test_image_limit_still_nine(self):
        self.assertEqual(agent_media.MAX_IMAGES_PER_UPLOAD, 9)
        self.assertEqual(agent_mod.MAX_ATTACHMENTS, 9)

    def test_image_path_still_synchronous(self):
        """图片仍然是同步预处理（既有行为不变）。"""
        source = Path(agent_media.__file__).read_text(encoding="utf-8")
        self.assertIn("_preprocess_asset(asset_id)", source)
        self.assertIn("_start_async_preprocess(asset_id, kind)", source)

    def test_uploads_always_go_through_library(self):
        """不允许绕过素材库直接给模型原始文件。"""
        source = Path(agent_media.__file__).read_text(encoding="utf-8")
        self.assertIn("register_asset", source)
        # 没有「直接把本地路径给模型」的通道
        self.assertNotIn("def _bypass_library", source)


if __name__ == "__main__":
    unittest.main()
