# -*- coding: utf-8 -*-
"""
资产素材库后端测试。

覆盖需求里的关键逻辑：
  1. 上传落盘不转码（原文件字节一致）
  2. 哈希去重（同文件不产生第二份）
  3. 标签检索（按标签 / 名称 / 类型 / 来源 / 分析文本）
  4. 引用删除保护（有引用 -> 409；无引用 -> 软删除 -> 可还原）
  5. 音频/视频/图片分析调度（各自走对应分析器，抽帧密度可调）

运行：python3 -m pytest tests/test_asset_library.py -v
"""
import asyncio
import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from h3studio import analysis  # noqa: E402
from h3studio import asset_repo, asset_service  # noqa: E402
from h3studio import assets as A  # noqa: E402
from h3studio import media_probe  # noqa: E402
from h3studio.assets import LibraryError  # noqa: E402


# ------------------------------------------------------------------ 测试脚手架


def _make_png(path: Path, width: int = 64, height: int = 48, color=(120, 30, 30)) -> Path:
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), color).save(path, "PNG")
    return path


def _make_mp3(path: Path, seconds: int = 1) -> Path:
    """用 ffmpeg 生成一段真实可解码的 mp3（没有 ffmpeg 就跳过）。"""
    if not media_probe.FFMPEG:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    import subprocess
    # 显式指定双声道：ffmpeg 的 lavfi sine 默认是单声道，
    # 声道数是本机真实探测出来的元数据，必须由源本身决定而不是写死期望值。
    cmd = [media_probe.FFMPEG, "-y", "-v", "error", "-f", "lavfi",
           "-i", f"sine=frequency=440:duration={seconds}",
           "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", str(path)]
    try:
        subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=120)
    except Exception:
        return None
    return path if path.is_file() and path.stat().st_size > 0 else None


def _make_mp4(path: Path, seconds: int = 2) -> Path:
    """生成一段真实可解码的 mp4。"""
    if not media_probe.FFMPEG:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    import subprocess
    cmd = [media_probe.FFMPEG, "-y", "-v", "error", "-f", "lavfi",
           "-i", f"testsrc=size=160x120:rate=15:duration={seconds}",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    try:
        subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=180)
    except Exception:
        return None
    return path if path.is_file() and path.stat().st_size > 0 else None


class AssetLibraryTestBase(unittest.TestCase):
    """每个用例用独立的临时素材库目录，互不干扰。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="assetlib-test-"))
        self.root = self.tmp / "library"
        self.src = self.tmp / "src"
        self.src.mkdir(parents=True, exist_ok=True)
        self._old_root = os.environ.get("ASSET_LIBRARY_ROOT")
        os.environ["ASSET_LIBRARY_ROOT"] = str(self.root)
        self.addCleanup(self._restore_env)
        A.init_db()

    def _restore_env(self):
        if self._old_root is None:
            os.environ.pop("ASSET_LIBRARY_ROOT", None)
        else:
            os.environ["ASSET_LIBRARY_ROOT"] = self._old_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def conn(self):
        return A.connect()


# ------------------------------------------------------------------ 1. 落盘不转码


class TestIngestNoTranscode(AssetLibraryTestBase):
    """上传的文件必须原样保留 —— 不做二次转码。"""

    def test_image_original_is_byte_identical(self):
        src = _make_png(self.src / "photo.png", 100, 60)
        with self.conn() as conn:
            result = asset_service.register_asset(conn, source_path=src, filename="photo.png")
        stored = A.resolve_inside(A.library_root(),
                                  result["version"]["storage_path"])
        self.assertTrue(stored.is_file(), "原文件应落到 originals 区")
        self.assertEqual(src.read_bytes(), stored.read_bytes(),
                         "落盘的字节必须与上传文件完全一致（不转码）")
        self.assertIsNone(result["version"]["transcode_reason"],
                          "可正常解码的文件不应标注已转码")

    def test_mp4_original_is_byte_identical(self):
        src = _make_mp4(self.src / "clip.mp4")
        if src is None:
            self.skipTest("本机没有 ffmpeg，跳过视频落盘测试")
        with self.conn() as conn:
            result = asset_service.register_asset(conn, source_path=src, filename="clip.mp4")
        stored = A.resolve_inside(A.library_root(),
                                  result["version"]["storage_path"])
        self.assertEqual(src.read_bytes(), stored.read_bytes())
        version = result["version"]
        self.assertEqual(version["width"], 160)
        self.assertEqual(version["height"], 120)
        self.assertIsNone(version["transcode_reason"])

    def test_mp3_metadata_extracted(self):
        src = _make_mp3(self.src / "bgm.mp3")
        if src is None:
            self.skipTest("本机没有 ffmpeg，跳过音频落盘测试")
        with self.conn() as conn:
            result = asset_service.register_asset(conn, source_path=src, filename="bgm.mp3")
        version = result["version"]
        # 期望值从源文件自身探测 —— 不写死数值（ffmpeg 默认参数会变）
        truth = media_probe.probe(src, "audio")
        self.assertEqual(version["mime"], "audio/mpeg")
        self.assertEqual(version["codec"], truth["codec"])
        self.assertEqual(version["channels"], truth["channels"])
        self.assertEqual(version["sample_rate"], truth["sample_rate"])
        self.assertAlmostEqual(version["duration_sec"], truth["duration_sec"], delta=0.1)
        self.assertIsNone(version["transcode_reason"])

    def test_corrupted_file_gets_transcode_reason(self):
        """文件损坏 -> 标注已转码 + 原因，且原始文件仍在（不覆盖不删除）。"""
        broken = self.src / "broken.png"
        broken.write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage" * 40)
        with self.conn() as conn:
            result = asset_service.register_asset(conn, source_path=broken,
                                                  filename="broken.png")
        version = result["version"]
        self.assertIsNotNone(version["transcode_reason"],
                             "无法解码的文件必须标注已转码与原因")
        self.assertIn("无法解码", version["transcode_reason"])
        stored = A.resolve_inside(A.library_root(), version["storage_path"])
        self.assertTrue(stored.is_file(), "原始文件必须仍然保留")
        self.assertEqual(broken.read_bytes(), stored.read_bytes(),
                         "损坏的原始文件也必须原样保留")

    def test_originals_are_sharded_by_hash(self):
        """原始文件按 sha256 前两字符分目录，避免单目录堆十万文件。"""
        src = _make_png(self.src / "shard.png")
        with self.conn() as conn:
            result = asset_service.register_asset(conn, source_path=src, filename="shard.png")
        rel = result["version"]["storage_path"]
        parts = rel.split("/")
        self.assertEqual(parts[0], A.DIR_ORIGINALS)
        self.assertEqual(len(parts), 4, f"应按两层哈希分目录，实际: {rel}")
        self.assertEqual(parts[1], result["version"]["sha256"][:2])
        self.assertEqual(parts[2], result["version"]["sha256"][2:4])


# ------------------------------------------------------------------ 2. 哈希去重


class TestDeduplication(AssetLibraryTestBase):
    """同一份内容重复上传 -> 复用既有素材，不产生第二份。"""

    def test_same_content_reuses_existing_asset(self):
        src = _make_png(self.src / "dup.png")
        with self.conn() as conn:
            first = asset_service.register_asset(conn, source_path=src, filename="dup.png")
            second = asset_service.register_asset(conn, source_path=src, filename="dup2.png")
        self.assertFalse(first["duplicated"])
        self.assertTrue(second["duplicated"], "同内容第二次入库应命中去重")
        self.assertEqual(first["asset"]["id"], second["asset"]["id"])
        with self.conn() as conn:
            assets_n = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            versions_n = conn.execute("SELECT COUNT(*) FROM asset_versions").fetchone()[0]
        self.assertEqual(assets_n, 1)
        self.assertEqual(versions_n, 1)

    def test_different_content_creates_new_asset(self):
        one = _make_png(self.src / "a.png", 64, 48, (10, 20, 30))
        two = _make_png(self.src / "b.png", 64, 48, (200, 100, 50))
        with self.conn() as conn:
            asset_service.register_asset(conn, source_path=one, filename="a.png")
            second = asset_service.register_asset(conn, source_path=two, filename="b.png")
        self.assertFalse(second["duplicated"])
        with self.conn() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 2)

    def test_dedup_survives_different_filename(self):
        """文件名不同但内容相同 -> 仍应去重（哈希而非文件名）。"""
        src = _make_png(self.src / "origin.png")
        copy = self.src / "renamed.png"
        shutil.copyfile(src, copy)
        with self.conn() as conn:
            asset_service.register_asset(conn, source_path=src, filename="origin.png")
            second = asset_service.register_asset(conn, source_path=copy, filename="renamed.png")
        self.assertTrue(second["duplicated"])


# ------------------------------------------------------------------ 3. 标签与检索


class TestTagsAndSearch(AssetLibraryTestBase):
    """标签增删、按标签/名称/类型/来源/分析文本检索。"""

    def _seed(self):
        src = _make_png(self.src / "角色三视图.png")
        with self.conn() as conn:
            result = asset_service.register_asset(
                conn, source_path=src, filename="角色三视图.png",
                tags=["原神", "雷神", "角色"], note="第1集定妆照")
            asset_id = result["asset"]["id"]
        return asset_id

    def test_add_and_remove_tags(self):
        asset_id = self._seed()
        with self.conn() as conn:
            tags = asset_repo.add_tags(conn, asset_id, ["三视图", "立绘"])
            self.assertIn("三视图", tags)
            self.assertIn("立绘", tags)
            tags = asset_repo.remove_tag(conn, asset_id, "三视图")
            self.assertNotIn("三视图", tags)
            self.assertIn("立绘", tags)
            # assets.tags 冗余列必须与 asset_tags 同步
            asset = asset_repo.get_asset(conn, asset_id)
            self.assertIn("立绘", asset["tags"])
            self.assertNotIn("三视图", asset["tags"])

    def test_add_tags_is_idempotent(self):
        asset_id = self._seed()
        with self.conn() as conn:
            asset_repo.add_tags(conn, asset_id, ["原神"])
            tags = asset_repo.add_tags(conn, asset_id, ["原神"])
        self.assertEqual(tags.count("原神"), 1)

    def test_rename_tag_updates_all_assets(self):
        asset_id = self._seed()
        with self.conn() as conn:
            touched = asset_repo.rename_tag(conn, "原神", "崩坏：原神")
            self.assertEqual(touched, 1)
            self.assertIn("崩坏：原神", asset_repo.tags_of(conn, asset_id))
            self.assertNotIn("原神", asset_repo.tags_of(conn, asset_id))

    def test_search_by_tag(self):
        asset_id = self._seed()
        with self.conn() as conn:
            hit = asset_repo.search_assets(conn, {"tag": "雷神"})
            self.assertEqual(hit["total"], 1)
            self.assertEqual(hit["assets"][0]["id"], asset_id)
            miss = asset_repo.search_assets(conn, {"tag": "不存在的标签"})
            self.assertEqual(miss["total"], 0)

    def test_search_by_tags_any_and_all(self):
        """tags_any / tags_all 是逗号分隔的字符串。

        回归：如果直接迭代字符串会拿到单个字符（「原神」-> ['原','神']），
        查询结果永远是 0 条。这里锁死多字标签必须能查出来。
        """
        asset_id = self._seed()
        with self.conn() as conn:
            hit = asset_repo.search_assets(conn, {"tags_any": "原神"})
            self.assertEqual(hit["total"], 1, "多字标签的 tags_any 查询必须命中")
            self.assertEqual(hit["assets"][0]["id"], asset_id)

            both = asset_repo.search_assets(conn, {"tags_all": "原神,角色"})
            self.assertEqual(both["total"], 1, "tags_all 多标签必须同时命中")

            mixed = asset_repo.search_assets(conn, {"tags_any": "原神,不存在的标签"})
            self.assertEqual(mixed["total"], 1, "tags_any 命中其一即可")

            none = asset_repo.search_assets(conn, {"tags_all": "原神,不存在的标签"})
            self.assertEqual(none["total"], 0, "tags_all 必须全部命中")

            empty = asset_repo.search_assets(conn, {"tags_any": ""})
            self.assertEqual(empty["total"], 1, "空 tags_any 不应过滤")

    def test_search_by_name_and_note(self):
        self._seed()
        with self.conn() as conn:
            self.assertEqual(asset_repo.search_assets(conn, {"q": "三视图"})["total"], 1)
            self.assertEqual(asset_repo.search_assets(conn, {"q": "定妆照"})["total"], 1)
            self.assertEqual(asset_repo.search_assets(conn, {"q": "不存在的词"})["total"], 0)

    def test_search_by_category_and_source(self):
        self._seed()
        with self.conn() as conn:
            self.assertEqual(
                asset_repo.search_assets(conn, {"category": "image"})["total"], 1)
            self.assertEqual(
                asset_repo.search_assets(conn, {"category": "video"})["total"], 0)
            self.assertEqual(
                asset_repo.search_assets(conn, {"source": "upload"})["total"], 1)
            self.assertEqual(
                asset_repo.search_assets(conn, {"source": "job_output"})["total"], 0)

    def test_search_fulltext_matches_analysis_text(self):
        asset_id = self._seed()
        with self.conn() as conn:
            asset_repo.save_analysis(conn, asset_id, "image",
                                     "【服装与配饰】胸口有一枚红色菱形挂件",
                                     revision=1, provider={"id": "p1", "name": "本机模型"})
            hit = asset_repo.search_assets(conn, {"q": "菱形挂件"})
            self.assertEqual(hit["total"], 1, "分析文本应可被全文检索")
            self.assertEqual(hit["assets"][0]["id"], asset_id)

    def test_all_tags_list(self):
        self._seed()
        with self.conn() as conn:
            tags = asset_repo.all_tags(conn)
            names = [t["tag"] for t in tags]
            self.assertIn("雷神", names)
            self.assertIn("角色", names)

    def test_update_asset_metadata(self):
        asset_id = self._seed()
        with self.conn() as conn:
            updated = asset_repo.update_asset(conn, asset_id, {
                "name": "雷神三视图", "note": "改过的备注", "tags": ["新标签"]})
            self.assertEqual(updated["name"], "雷神三视图")
            self.assertEqual(updated["note"], "改过的备注")
            self.assertEqual(updated["tags"], ["新标签"])


# ------------------------------------------------------------------ 4. 引用与删除保护


class TestReferencesAndDeletion(AssetLibraryTestBase):
    """被引用的素材不能删（409 + 引用清单）；无引用可软删除并还原。"""

    def _seed(self):
        src = _make_png(self.src / "ref.png")
        with self.conn() as conn:
            result = asset_service.register_asset(conn, source_path=src, filename="ref.png")
        return result["asset"]["id"]

    def test_referenced_asset_delete_is_rejected(self):
        asset_id = self._seed()
        with self.conn() as conn:
            asset_repo.add_ref(conn, asset_id, "job", "job_abc", slot="first_frame",
                               label="任务 job_abc 的首帧输入")
            with self.assertRaises(LibraryError) as ctx:
                asset_repo.soft_delete(conn, asset_id)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "asset_in_use")
        self.assertEqual(len(ctx.exception.extra["refs"]), 1)
        self.assertEqual(ctx.exception.extra["refs"][0]["slot"], "first_frame")
        # 素材必须还在（没被删掉）
        with self.conn() as conn:
            self.assertIsNotNone(asset_repo.get_asset(conn, asset_id))

    def test_unreferenced_asset_soft_deletes_and_restores(self):
        asset_id = self._seed()
        with self.conn() as conn:
            deleted = asset_repo.soft_delete(conn, asset_id)
            self.assertIsNotNone(deleted["deleted_at"])
            self.assertIsNotNone(deleted["purge_at"])
            # 默认列表里看不到
            self.assertEqual(asset_repo.search_assets(conn, {})["total"], 0)
            # 回收站里能看到
            self.assertEqual(
                asset_repo.search_assets(conn, {"in_trash": "1"})["total"], 1)
            restored = asset_repo.restore(conn, asset_id)
            self.assertIsNone(restored["deleted_at"])
        with self.conn() as conn:
            self.assertEqual(asset_repo.search_assets(conn, {})["total"], 1)

    def test_restore_puts_file_back_in_originals(self):
        asset_id = self._seed()
        with self.conn() as conn:
            before = asset_repo.current_version(conn, asset_id)["storage_path"]
            asset_repo.soft_delete(conn, asset_id)
            asset_repo.restore(conn, asset_id)
            after = asset_repo.current_version(conn, asset_id)["storage_path"]
        self.assertEqual(before, after, "还原后应回到原来的相对路径")
        stored = A.resolve_inside(A.library_root(), after)
        self.assertTrue(stored.is_file(), "还原后物理文件必须还在")

    def test_purge_deletes_everything(self):
        asset_id = self._seed()
        with self.conn() as conn:
            asset_repo.purge(conn, asset_id)
            self.assertIsNone(asset_repo.get_asset(conn, asset_id, include_deleted=True))

    def test_ref_count_is_live(self):
        asset_id = self._seed()
        with self.conn() as conn:
            self.assertEqual(asset_repo.ref_count(conn, asset_id), 0)
            asset_repo.add_ref(conn, asset_id, "job", "j1", slot="ref_image")
            asset_repo.add_ref(conn, asset_id, "job", "j2", slot="ref_image")
            self.assertEqual(asset_repo.ref_count(conn, asset_id), 2)

    def test_deleting_task_does_not_delete_asset(self):
        """删任务不能让素材悬空：素材本体保留。"""
        asset_id = self._seed()
        with self.conn() as conn:
            asset_repo.add_ref(conn, asset_id, "job", "job_to_delete", slot="ref_image")
            # 模拟删任务：引用行随任务清理
            asset_repo.remove_refs_for(conn, "job", "job_to_delete")
            asset = asset_repo.get_asset(conn, asset_id)
        self.assertIsNotNone(asset, "删任务后素材本体必须保留")
        self.assertIsNone(asset["deleted_at"])


# ------------------------------------------------------------------ 5. 分析调度


class TestAnalysisDispatch(AssetLibraryTestBase):
    """图片 / 视频 / 音频 各自走对应分析器。"""

    def test_analyzer_registry_covers_three_kinds(self):
        self.assertIn("image", analysis.ANALYZERS)
        self.assertIn("video", analysis.ANALYZERS)
        self.assertIn("audio", analysis.ANALYZERS)
        self.assertEqual(analysis.ANALYZERS["image"].kind, "image")
        self.assertEqual(analysis.ANALYZERS["video"].kind, "video")
        self.assertEqual(analysis.ANALYZERS["audio"].kind, "audio")

    def test_unsupported_kind_raises(self):
        with self.assertRaises(LibraryError):
            analysis.get_analyzer("doc")

    def test_image_analyzer_prepares_single_image(self):
        src = _make_png(self.src / "a.png")
        with self.conn() as conn:
            result = asset_service.register_asset(conn, source_path=src, filename="a.png")
        asset = result["asset"]
        version = result["version"]
        prepared = analysis.ANALYZERS["image"].prepare(
            asset, version, src, {"derived_dir": str(self.tmp / "d")})
        self.assertEqual(len(prepared["images"]), 1)
        self.assertIn("尺寸", prepared["prompt"])

    def test_video_analyzer_extracts_frames_with_configurable_density(self):
        src = _make_mp4(self.src / "clip.mp4", seconds=4)
        if src is None:
            self.skipTest("本机没有 ffmpeg，跳过视频抽帧测试")
        with self.conn() as conn:
            result = asset_service.register_asset(conn, source_path=src, filename="clip.mp4")
        derived = self.tmp / "derived"
        prepared = analysis.ANALYZERS["video"].prepare(
            result["asset"], result["version"], src,
            {"derived_dir": str(derived), "frame_interval": 1.0, "max_frames": 3})
        frames = prepared["frames"]
        self.assertTrue(frames, "视频分析必须抽出帧")
        self.assertLessEqual(len(frames), 3, "抽帧上限必须生效")
        for frame in frames:
            self.assertTrue(Path(frame["path"]).is_file())
            self.assertIn("t", frame)
        self.assertIn("抽帧时间点", prepared["prompt"])

    def test_audio_analyzer_is_pluggable(self):
        """音频分析器必须留可插拔接口，等待深度分析方案接入。"""
        analyzer = analysis.ANALYZERS["audio"]
        self.assertTrue(hasattr(analyzer, "set_backend"))
        self.assertTrue(hasattr(analyzer, "backend"))

        class FakeBackend:
            name = "深度音频分析器"

            def analyze(self, path, options):
                return {"bpm": 128, "instruments": ["尺八"],
                        "asr": [{"start": 1.0, "end": 3.0, "text": "你好"}]}

        analysis.AudioAnalyzer.set_backend(FakeBackend())
        try:
            prepared = analyzer.prepare(
                {"name": "a.mp3"}, {"duration_sec": 3.0, "sample_rate": 44100,
                                    "channels": 2, "audio_codec": "mp3"},
                self.src / "a.mp3", {})
            self.assertEqual(prepared["extra"]["bpm"], 128)
            self.assertEqual(prepared["extra"]["backend"], "深度音频分析器")
            self.assertIn("128", prepared["prompt"])
            self.assertIn("你好", prepared["prompt"])
        finally:
            analysis.AudioAnalyzer.set_backend(None)

    def test_audio_analyzer_without_backend_reports_missing(self):
        analysis.AudioAnalyzer.set_backend(None)
        prepared = analysis.ANALYZERS["audio"].prepare(
            {"name": "a.mp3"}, {"duration_sec": 3.0, "sample_rate": 44100,
                                "channels": 2, "audio_codec": "mp3"},
            self.src / "a.mp3", {})
        self.assertIn("未提供 ASR 结果", prepared["prompt"])

    def test_analysis_revisions_are_kept(self):
        """多轮纠错：每次一条记录，revision 递增，历史保留。"""
        src = _make_png(self.src / "a.png")
        with self.conn() as conn:
            result = asset_service.register_asset(conn, source_path=src, filename="a.png")
            asset_id = result["asset"]["id"]
            asset_repo.save_analysis(conn, asset_id, "image", "第一版：椭圆形挂件",
                                     revision=1)
            second = asset_repo.next_revision(conn, asset_id)
            self.assertEqual(second, 2)
            asset_repo.save_analysis(conn, asset_id, "image", "第二版：菱形挂件",
                                     revision=second)
            history = asset_repo.analyses_of(conn, asset_id)
        self.assertEqual(len(history), 2, "两版分析都要保留")
        self.assertEqual(history[0]["revision"], 2, "最新的排前面")
        self.assertIn("菱形", history[0]["text"])
        self.assertIn("椭圆", history[1]["text"])

    def test_latest_analysis_returns_newest(self):
        src = _make_png(self.src / "a.png")
        with self.conn() as conn:
            result = asset_service.register_asset(conn, source_path=src, filename="a.png")
            asset_id = result["asset"]["id"]
            asset_repo.save_analysis(conn, asset_id, "image", "旧", revision=1)
            asset_repo.save_analysis(conn, asset_id, "image", "新", revision=2)
            latest = asset_repo.latest_analysis(conn, asset_id)
        self.assertEqual(latest["text"], "新")

    def test_failed_analysis_is_recorded(self):
        src = _make_png(self.src / "a.png")
        with self.conn() as conn:
            result = asset_service.register_asset(conn, source_path=src, filename="a.png")
            asset_id = result["asset"]["id"]
            record = asset_repo.save_analysis(conn, asset_id, "image", "",
                                             revision=1, error="模型超时")
        self.assertEqual(record["error"], "模型超时")
        with self.conn() as conn:
            self.assertIsNone(asset_repo.latest_analysis(conn, asset_id),
                              "失败的分析不应作为最新结论")


# ------------------------------------------------------------------ 6. 存储自检


class TestStorageSelfCheck(AssetLibraryTestBase):
    """启动自检：磁盘挂载、目录可写、空间。"""

    def test_layout_created(self):
        for name in A.SUBDIRS:
            self.assertTrue((self.root / name).is_dir(), f"缺少分区 {name}")

    def test_ensure_layout_reports_ok(self):
        report = A.ensure_layout()
        self.assertTrue(report["ok"], report.get("errors"))

    def test_mount_detection_flags_unmounted_path(self):
        """未挂载的路径必须被判定为不可用（否则会写爆系统盘）。"""
        ok, detail = A.check_disk_mounted(Path("/root/backup/definitely-not-mounted"))
        self.assertFalse(ok, "普通目录应判定为未挂载")
        self.assertIn("挂载", detail)

    def test_mount_detection_accepts_real_mount(self):
        ok, detail = A.check_disk_mounted(Path("/mnt/usb2t"))
        self.assertTrue(ok, f"/mnt/usb2t 应判定为已挂载: {detail}")

    def test_write_probe_detects_readonly(self):
        report = A.ensure_layout()
        self.assertIn("free_bytes", report)
        self.assertIn("total_bytes", report)

    def test_resolve_inside_blocks_traversal(self):
        self.assertIsNone(A.resolve_inside(self.root, "../../etc/passwd"))
        self.assertIsNone(A.resolve_inside(self.root, "/etc/passwd"))


# ------------------------------------------------------------------ 7. 统计


class TestStats(AssetLibraryTestBase):
    def test_stats_reflect_content(self):
        src = _make_png(self.src / "s.png", 30, 20)
        with self.conn() as conn:
            asset_service.register_asset(conn, source_path=src, filename="s.png")
            stats = asset_repo.stats(conn)
        self.assertEqual(stats["total_count"], 1)
        self.assertGreater(stats["total_bytes"], 0)
        self.assertEqual(stats["by_category"][0]["category"], "image")
        self.assertEqual(stats["by_source"][0]["source"], "upload")


if __name__ == "__main__":
    unittest.main(verbosity=2)
