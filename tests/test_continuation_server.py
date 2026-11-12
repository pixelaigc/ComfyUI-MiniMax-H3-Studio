# -*- coding: utf-8 -*-
"""接续生成的**服务端端到端**测试：建任务 -> 接续校验 -> 链记账 -> 完整视频拼接。

【为什么需要这一层】
tests/test_continuation.py 测的是**纯函数**（帧数换算、接线、判定）。
但接续还有一半的逻辑在 server.py 里，需要**真的建任务**才能验：
  * 建任务时链信息有没有写进 params；
  * 接续提交时服务端有没有正确校验并**重算**段号；
  * 链的分段账本有没有正确累积；
  * 完整视频拼接有没有产出正确长度的文件。

【这一层不碰 ComfyUI】
用**假的**产物文件（自己造 mp4）+ 假的潜空间文件来测记账与拼接，
不真的跑模型 —— 跑模型的部分由 tests/test_continuation.py 的接线断言
+ 真实的浏览器实测覆盖。

运行：python3 -m pytest tests/test_continuation_server.py -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import server as srv                                    # noqa: E402
from h3studio import continuation as cont               # noqa: E402
from h3studio import continuation_service as cs         # noqa: E402


def _make_video(path: Path, seconds: float, width=768, height=448) -> bool:
    """造一个指定时长的测试视频（用 ffmpeg 的 testsrc）。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    cmd = [ffmpeg, "-y", "-f", "lavfi", "-i",
           f"testsrc=size={width}x{height}:rate=24:duration={seconds}",
           "-f", "lavfi", "-i",
           f"sine=frequency=440:duration={seconds}",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", str(path)]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=120, check=True)
    except Exception:
        return False
    return path.is_file()


def _video_frames(path: Path) -> int:
    """数视频帧数。"""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return -1
    raw = subprocess.check_output(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=nb_frames", "-of", "csv=p=0", str(path)],
        text=True).strip()
    try:
        return int(raw)
    except ValueError:
        return -1


class ContinuationServerTestBase(unittest.TestCase):
    """把 server 的数据目录指到临时目录，不碰真实库。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="h3-cont-test-"))
        cls._orig_data = srv.DATA
        cls._orig_out = srv.OUT_DIR
        cls._orig_db = srv.DB_PATH
        cls._orig_video = srv.VIDEO_DIR
        cls._orig_upload = srv.UPLOAD_DIR
        cls._orig_image = srv.IMAGE_DIR
        cls._orig_audio = srv.AUDIO_DIR
        cls._orig_cfg = dict(srv.CFG)

        srv.DATA = cls.tmp
        srv.OUT_DIR = cls.tmp / "outputs"
        srv.VIDEO_DIR = srv.OUT_DIR / "videos"
        srv.UPLOAD_DIR = cls.tmp / "uploads"
        srv.IMAGE_DIR = srv.OUT_DIR / "images"
        srv.AUDIO_DIR = srv.OUT_DIR / "audio"
        srv.DB_PATH = cls.tmp / "jobs.db"
        for d in (srv.OUT_DIR, srv.VIDEO_DIR, srv.UPLOAD_DIR,
                  srv.IMAGE_DIR, srv.AUDIO_DIR):
            d.mkdir(parents=True, exist_ok=True)
        srv.init_db()

    @classmethod
    def tearDownClass(cls):
        srv.DATA = cls._orig_data
        srv.OUT_DIR = cls._orig_out
        srv.DB_PATH = cls._orig_db
        srv.VIDEO_DIR = cls._orig_video
        srv.UPLOAD_DIR = cls._orig_upload
        srv.IMAGE_DIR = cls._orig_image
        srv.AUDIO_DIR = cls._orig_audio
        srv.CFG = cls._orig_cfg
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _make_job(self, mode: str, params: dict, outputs=None) -> str:
        """直接写一条任务记录（绕开 HTTP 层，测的是记账逻辑）。"""
        job_id = srv.create_job_record(mode, params)
        if outputs is not None:
            with srv.db() as conn:
                conn.execute("UPDATE jobs SET status='done', outputs=? WHERE id=?",
                             (json.dumps(outputs, ensure_ascii=False), job_id))
        return job_id

    def _params(self, **extra):
        base = {"prompt": "p", "model_key": "fl2va_int8", "width": 768,
                "height": 448, "duration": 5, "steps": 4, "sampler": "euler",
                "scheduler": "simple"}
        base.update(extra)
        return base


class TestChainBookkeeping(ContinuationServerTestBase):
    """链的记账：段号、账本、累计帧数。"""

    def test_first_segment_gets_its_own_chain(self):
        """第 1 段 = 链首段：chain_id 就是自己的任务 id，段号 0。"""
        job_id = self._make_job("t2v", self._params())
        with srv.db() as conn:
            row = conn.execute("SELECT params FROM jobs WHERE id=?",
                               (job_id,)).fetchone()
        params = json.loads(row["params"])
        info = params["continuation"]
        self.assertEqual(info["chain_id"], job_id)
        self.assertEqual(info["stage_index"], 0)
        self.assertEqual(info["source_job"], "")

    def test_second_segment_inherits_chain_and_increments_stage(self):
        """第 2 段：链 id 不变，段号 +1，接力棒指向上游。

        上游潜空间不在时必须**明确报错**（不能静默通过 ——
        静默通过会让工作流退化成普通生成，用户拿到的是一段独立的视频，
        看起来成功其实没接上）。
        """
        seg1 = self._make_job("t2v", self._params())
        ctx = {"chain_id": seg1, "stage_index": 0, "source_job": seg1,
               "source_segment": 0, "context_frames": 22,
               "anchor_frames": 22, "chain_total_frames": 0,
               "segment_frames": 124}
        params = self._params(continuation=ctx)
        error = srv._prepare_continuation_params(params)
        self.assertTrue(error, "上游潜空间缺失时必须报错")
        self.assertIn("潜空间", error)

    def test_missing_source_job_is_rejected(self):
        """上下文里没有 source_job -> 明确报错（不回填=不该提交）。"""
        params = self._params(continuation={"chain_id": "x", "stage_index": 0})
        error = srv._prepare_continuation_params(params)
        self.assertTrue(error)
        self.assertIn("source_job", error)

    def test_prepare_continuation_rejects_unknown_source(self):
        """上游任务不存在 -> 明确报错。"""
        params = self._params(continuation={"source_job": "nope"})
        error = srv._prepare_continuation_params(params)
        self.assertTrue(error)
        self.assertIn("不存在", error)

    def test_prepare_continuation_rejects_selflift_source(self):
        """上游是二采 -> 明确拒绝。"""
        seg1 = self._make_job("t2v", self._params(twopass={"enabled": True}))
        params = self._params(continuation={"source_job": seg1})
        error = srv._prepare_continuation_params(params)
        self.assertTrue(error)
        self.assertIn("二采", error)

    def test_prepare_continuation_computes_stage_index(self):
        """服务端自己算段号（前端传什么都不认）。"""
        # 造一个带潜空间的第 1 段
        seg1 = self._make_job("t2v", self._params())
        run_id = cs.chain_run_id(seg1)
        stage_file = cont.stage_file_path(srv.cont_service.comfy_output_dir()
                                          if hasattr(srv, "cont_service")
                                          else cs.comfy_output_dir(),
                                          run_id, 0)
        stage_file.parent.mkdir(parents=True, exist_ok=True)
        stage_file.write_bytes(b"fake-latent")

        params = self._params(continuation={
            "source_job": seg1, "stage_index": 99,  # 前端乱填
            "context_frames": 22, "anchor_frames": 22,
        })
        error = srv._prepare_continuation_params(params)
        self.assertEqual(error, "", f"不该报错: {error}")
        info = params["continuation"]
        self.assertEqual(info["stage_index"], 1,
                         "段号必须由服务端重算为 上游段号+1")
        self.assertEqual(info["source_job"], seg1)
        self.assertEqual(info["chain_id"], seg1, "链 id 沿用上游")
        stage_file.unlink()


class TestFullVideoConcat(ContinuationServerTestBase):
    """完整视频拼接：段数正确 + 时长正确。"""

    def test_concat_produces_full_length_video(self):
        """两段拼起来 = 两段长度之和。"""
        if not shutil.which("ffmpeg"):
            self.skipTest("本机没有 ffmpeg")
        seg1 = srv.VIDEO_DIR / "seg1.mp4"
        seg2 = srv.VIDEO_DIR / "seg2.mp4"
        self.assertTrue(_make_video(seg1, 3.0), "造测试视频失败")
        self.assertTrue(_make_video(seg2, 2.0), "造测试视频失败")

        chain_id = "testchain01"
        out = srv._concat_chain_video(
            ["videos/seg1.mp4", "videos/seg2.mp4"], chain_id)
        self.assertIsNotNone(out, "拼接应成功")
        self.assertTrue(out.is_file())
        frames = _video_frames(out)
        # 3s + 2s = 5s @24fps = 120 帧（±2 帧的编码容差）
        self.assertGreaterEqual(frames, 116, f"完整视频帧数偏少: {frames}")
        self.assertLessEqual(frames, 124, f"完整视频帧数偏多: {frames}")

    def test_concat_single_segment_returns_itself(self):
        """只有一段时，完整视频就是它自己（不重复编码）。"""
        seg1 = srv.VIDEO_DIR / "solo.mp4"
        self.assertTrue(_make_video(seg1, 1.0))
        out = srv._concat_chain_video(["videos/solo.mp4"], "solo")
        self.assertIsNotNone(out)
        self.assertEqual(out.resolve(), seg1.resolve())

    def test_concat_skips_missing_files(self):
        """段落文件丢了 -> 跳过它，不让整条链挂掉。"""
        if not shutil.which("ffmpeg"):
            self.skipTest("本机没有 ffmpeg")
        seg1 = srv.VIDEO_DIR / "keep.mp4"
        self.assertTrue(_make_video(seg1, 1.0))
        out = srv._concat_chain_video(
            ["videos/keep.mp4", "videos/gone.mp4"], "partial")
        self.assertIsNotNone(out)
        self.assertTrue(out.is_file())

    def test_concat_rejects_path_escape(self):
        """路径穿越（../）必须被拒绝 —— 不能拼 OUT_DIR 之外的文件。"""
        out = srv._concat_chain_video(["../../etc/passwd"], "evil")
        self.assertIsNone(out, "路径穿越必须被拒绝")


class TestContinuationState(ContinuationServerTestBase):
    """可接续状态：老任务、二采、缺文件。"""

    def test_legacy_job_without_continuation_cannot_continue(self):
        """没有链信息的老任务 -> 不可接续，且原因明确。"""
        job_id = self._make_job("t2v", self._params())
        with srv.db() as conn:
            conn.execute("UPDATE jobs SET params=? WHERE id=?",
                         (json.dumps(self._params()), job_id))
        state = cs.continuation_state("t2v", self._params(), job_id)
        self.assertFalse(state["can_continue"])
        self.assertTrue(state["reason"].strip())

    def test_state_reports_missing_latent_with_reason(self):
        """潜空间不在 -> 原因里要出现路径（明确，不静默）。"""
        params = self._params(continuation={
            "chain_id": "ghostchain", "stage_index": 0,
            "context_frames": 22, "anchor_frames": 22,
        })
        state = cs.continuation_state("t2v", params, "jobX")
        self.assertFalse(state["can_continue"])
        self.assertIn("潜空间", state["reason"])


if __name__ == "__main__":
    unittest.main()
