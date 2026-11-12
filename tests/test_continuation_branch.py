# -*- coding: utf-8 -*-
"""【D1 回归】同源分支接续 —— 潜空间落盘坐标必须互不覆盖。

【D1 是什么】
用户需求第 7 条：「用户可以继续对这条完整视频再点接续，实现超长视频
连续生成」。这意味着**同一条链可以被分叉**：对同一段点两次「接续」，
得到两个兄弟分支 B1 / B2。

旧实现把「链 id + 链段号」当落盘坐标。B1 与 B2 的上游是同一段 A，
于是两者算出**完全相同**的坐标 -> 写同一个
`output/relay_kit/<chain_id>/stage_00001.safetensors` -> 后完成者覆盖先完成者。

后果不是报错，而是**静默的错误内容**：再对 B1 接续得到 C1 时，
读回的 `stage_00001` 已经是 B2 的潜空间 —— C1 实际接的是兄弟分支的画面。
实测（验收脚本）：C1首帧 vs B1末帧 灰度 MAE = 38.65，
vs B2末帧 = 2.08 —— 偏离 18.6 倍，而界面全程显示成功。

【本文件在防什么】
  1. 兄弟分支的潜空间**落盘坐标必须不同**（结构性保证，不是靠后缀遮）。
  2. 「接续时读到的必然是**自己上游**那一段」——
     读回坐标必须来自上游任务记录，不能按链段号推。
  3. 完整视频拼接**按血缘**（沿 source_job 回溯），
     不能用「链首段共享账本」（那会让兄弟分支互相污染）。
  4. 分叉后各分支的累计帧数各自独立。

【断言纪律】
  * 期望值全部是**字面量**（目录名前缀、字段名），不从被测函数反推。
  * 每条断言由「命名该行为的那条断言」抓住。

运行：python3 -m pytest tests/test_continuation_branch.py -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import server as srv                                    # noqa: E402
from h3studio import continuation as cont               # noqa: E402
from h3studio import continuation_service as cs         # noqa: E402


class BranchTestBase(unittest.TestCase):
    """把 server 的数据目录指到临时目录，不碰真实库。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="h3-branch-test-"))
        cls._orig = {
            "DATA": srv.DATA, "OUT_DIR": srv.OUT_DIR,
            "DB_PATH": srv.DB_PATH, "VIDEO_DIR": srv.VIDEO_DIR,
            "UPLOAD_DIR": srv.UPLOAD_DIR, "IMAGE_DIR": srv.IMAGE_DIR,
            "AUDIO_DIR": srv.AUDIO_DIR,
        }
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
        for key, value in cls._orig.items():
            setattr(srv, key, value)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _params(self, **extra):
        base = {"prompt": "p", "model_key": "fl2va_int8", "width": 768,
                "height": 448, "duration": 5, "steps": 4, "sampler": "euler",
                "scheduler": "simple"}
        base.update(extra)
        return base

    def _make_job(self, params: dict) -> str:
        job_id = srv.create_job_record("t2v", params)
        with srv.db() as conn:
            conn.execute("UPDATE jobs SET status='done' WHERE id=?", (job_id,))
        return job_id

    def _info(self, job_id: str) -> dict:
        with srv.db() as conn:
            row = conn.execute("SELECT params FROM jobs WHERE id=?",
                               (job_id,)).fetchone()
        return json.loads(row["params"])["continuation"]

    def _write_latent(self, job_id: str, content: bytes = b"latent") -> Path:
        """在**该段自己的目录**里落一份假潜空间。

        目录由该段任务 id 派生（每段一个目录）—— 与服务端读回时算的
        坐标必须是同一个，否则测试就在测一个不存在的东西。
        """
        run_id = cs.segment_run_id(job_id)
        path = cont.stage_file_path(cs.comfy_output_dir(), run_id, 0)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def _branch(self, source_job: str, **extra) -> str:
        """从 source_job 接续出一段，返回新任务 id。

        走服务端真实的校验路径（_prepare_continuation_params），
        所以测的就是线上那条路。
        """
        params = self._params(continuation={"source_job": source_job,
                                            "context_frames": 22,
                                            "anchor_frames": 22}, **extra)
        error = srv._prepare_continuation_params(params)
        self.assertEqual(error, "", f"接续校验不该报错: {error}")
        return srv.create_job_record("t2v", params)


class TestBranchLatentCoords(BranchTestBase):
    """【D1 核心】兄弟分支的落盘坐标必须互不相同。"""

    def test_sibling_branches_write_to_different_files(self):
        """A 接两次 -> B1 / B2，两份潜空间文件必须**同时存在**。

        这是 D1 的直接后果：旧实现里两者写同一个文件。
        """
        a = self._make_job(self._params())
        self._write_latent(a, b"latent-A")

        b1 = self._branch(a)
        b2 = self._branch(a)

        path1 = self._write_latent(b1, b"latent-B1")
        path2 = self._write_latent(b2, b"latent-B2")

        self.assertNotEqual(path1, path2,
                            "兄弟分支的潜空间文件必须是两个不同的文件")
        self.assertTrue(path1.is_file())
        self.assertTrue(path2.is_file(),
                        "B1 的潜空间不能被 B2 覆盖掉")
        self.assertEqual(path1.read_bytes(), b"latent-B1")
        self.assertEqual(path2.read_bytes(), b"latent-B2")

    def test_branch_run_ids_are_derived_from_own_job_id(self):
        """分支的落盘目录由**该分支自己的任务 id** 派生。"""
        a = self._make_job(self._params())
        self._write_latent(a)
        b1 = self._branch(a)
        b2 = self._branch(a)
        self.assertEqual(self._info(b1)["latent_run_id"],
                         cs.segment_run_id(b1))
        self.assertEqual(self._info(b2)["latent_run_id"],
                         cs.segment_run_id(b2))
        self.assertNotEqual(self._info(b1)["latent_run_id"],
                            self._info(b2)["latent_run_id"])

    def test_read_back_targets_own_upstream_not_sibling(self):
        """【D1 的核心断言】接续读回必须是**自己上游**那份。

        再对 B1 接续 -> C1，C1 的读回坐标必须是 B1 的，不是 B2 的。
        """
        a = self._make_job(self._params())
        self._write_latent(a)
        b1 = self._branch(a)
        b2 = self._branch(a)
        self._write_latent(b1)
        self._write_latent(b2)

        c1 = self._branch(b1)
        info_c1 = self._info(c1)
        self.assertEqual(info_c1["source_latent_run_id"],
                         cs.segment_run_id(b1),
                         "C1 必须读 B1 的潜空间")
        self.assertNotEqual(info_c1["source_latent_run_id"],
                            cs.segment_run_id(b2),
                            "C1 绝不能读兄弟分支 B2 的潜空间")

    def test_source_latent_path_is_the_upstream_file(self):
        """读回路径必须精确指向上游那一个文件。"""
        a = self._make_job(self._params())
        latent_a = self._write_latent(a)
        b1 = self._branch(a)
        info = self._info(b1)
        self.assertEqual(info["source_latent_path"], str(latent_a))

    def test_front_end_cannot_forge_the_read_target(self):
        """前端伪造坐标也无效 —— 服务端按上游任务记录重算。

        前端可以随便填 source_latent_run_id / source_latent_stage；
        服务端必须忽略它们，用上游记录里的真实坐标。
        """
        a = self._make_job(self._params())
        self._write_latent(a)
        b1 = self._branch(a)
        latent_b1 = self._write_latent(b1)

        params = self._params(continuation={
            "source_job": b1,
            # 前端伪造：指向兄弟分支的坐标
            "source_latent_run_id": cs.segment_run_id("EVIL"),
            "source_latent_stage": 7,
            "source_latent_path": "/tmp/evil.safetensors",
            "context_frames": 22, "anchor_frames": 22,
        })
        error = srv._prepare_continuation_params(params)
        self.assertEqual(error, "", f"不该报错: {error}")
        info = params["continuation"]
        self.assertEqual(info["source_latent_run_id"], cs.segment_run_id(b1),
                         "服务端必须用上游记录里的真实坐标")
        self.assertEqual(info["source_latent_path"], str(latent_b1))


class TestBranchLineage(BranchTestBase):
    """【D1 第二处】完整视频拼接必须按**血缘**，不能按共享账本。"""

    def _record(self, job_id: str, rel: str) -> None:
        srv._record_segment_rel(job_id, rel)

    def test_lineage_excludes_sibling_branch(self):
        """C1 的血缘里不能有 B2。"""
        a = self._make_job(self._params())
        self._write_latent(a)
        b1 = self._branch(a)
        b2 = self._branch(a)
        self._write_latent(b1)
        self._write_latent(b2)
        c1 = self._branch(b1)

        self._record(a, "videos/a.mp4")
        self._record(b1, "videos/b1.mp4")
        self._record(b2, "videos/b2.mp4")
        self._record(c1, "videos/c1.mp4")

        lineage = srv._lineage_segment_rels(c1)
        self.assertEqual(lineage, ["videos/a.mp4", "videos/b1.mp4",
                                   "videos/c1.mp4"])
        self.assertNotIn("videos/b2.mp4", lineage,
                         "兄弟分支不能混进完整视频")

    def test_lineage_of_sibling_is_separate(self):
        """B2 的血缘是 A -> B2，不含 B1。"""
        a = self._make_job(self._params())
        self._write_latent(a)
        b1 = self._branch(a)
        b2 = self._branch(a)
        self._record(a, "videos/a.mp4")
        self._record(b1, "videos/b1.mp4")
        self._record(b2, "videos/b2.mp4")

        self.assertEqual(srv._lineage_segment_rels(b1),
                         ["videos/a.mp4", "videos/b1.mp4"])
        self.assertEqual(srv._lineage_segment_rels(b2),
                         ["videos/a.mp4", "videos/b2.mp4"])

    def test_linear_chain_lineage_is_ordered(self):
        """线性接续的血缘顺序正确（链首 -> ... -> 本段）。"""
        a = self._make_job(self._params())
        self._write_latent(a)
        b = self._branch(a)
        self._write_latent(b)
        c = self._branch(b)
        self._record(a, "videos/a.mp4")
        self._record(b, "videos/b.mp4")
        self._record(c, "videos/c.mp4")
        self.assertEqual(srv._lineage_segment_rels(c),
                         ["videos/a.mp4", "videos/b.mp4", "videos/c.mp4"])

    def test_lineage_survives_missing_upstream_record(self):
        """上游记录被删 -> 血缘里就少了它（不能崩，也不能无限回溯）。"""
        a = self._make_job(self._params())
        self._write_latent(a)
        b = self._branch(a)
        self._record(a, "videos/a.mp4")
        self._record(b, "videos/b.mp4")
        with srv.db() as conn:
            conn.execute("DELETE FROM jobs WHERE id=?", (a,))
        self.assertEqual(srv._lineage_segment_rels(b), ["videos/b.mp4"])


class TestBranchTotals(BranchTestBase):
    """分叉后各分支的累计帧数必须各自独立。"""

    def test_branch_totals_are_independent(self):
        """B1 / B2 从同一个 A 接出，累计帧数应相同但互不污染。

        再从 B1 接出 C1：C1 的累计 = A + B1 + C1，**不含 B2**。

        帧数账（钉住窗口 22 帧，只有接续段裁）：
          A（链首段，不裁）   = 124
          B1（接续段，裁 22） = 124 + (124-22) = 226
          C1（接续段，裁 22） = 226 + (124-22) = 328
        """
        a = self._make_job(self._params())
        self._write_latent(a)
        b1 = self._branch(a)
        self._write_latent(b1)
        b2 = self._branch(a)
        self._write_latent(b2)

        # B1 / B2 从同一个 A 接出 -> 累计相同
        state_b1 = cs.continuation_state("t2v", self._params_of(b1), b1)
        state_b2 = cs.continuation_state("t2v", self._params_of(b2), b2)
        self.assertEqual(state_b1["total_frames"], 226,
                         "A(124) + B1(124-22=102) = 226")
        self.assertEqual(state_b2["total_frames"], 226,
                         "B2 的累计不应受 B1 影响")

        c1 = self._branch(b1)
        self._write_latent(c1)
        state_c1 = cs.continuation_state("t2v", self._params_of(c1), c1)
        # A(124) + B1(102) + C1(102) = 328 —— **不含 B2**
        self.assertEqual(state_c1["total_frames"], 328)

    def test_first_segment_reports_full_length(self):
        """链首段没有上游、没裁过 -> 累计就是整段长度（124 帧）。

        旧实现对链首段也减了钉住窗口 -> 报 102，与真实产物（124 帧）不符。
        """
        a = self._make_job(self._params())
        self._write_latent(a)
        state = cs.continuation_state("t2v", self._params_of(a), a)
        self.assertEqual(state["total_frames"], 124)

    def _params_of(self, job_id: str) -> dict:
        with srv.db() as conn:
            row = conn.execute("SELECT params FROM jobs WHERE id=?",
                               (job_id,)).fetchone()
        return json.loads(row["params"])


class TestBranchStillWorksForLegacyJobs(BranchTestBase):
    """旧任务（上线前生成）仍可接续 —— 坐标沿用「链目录 + 链段号」。"""

    def test_legacy_job_uses_chain_directory(self):
        """没有 latent_run_id 的旧任务 -> 沿用链目录。"""
        job_id = srv.create_job_record("t2v", self._params())
        with srv.db() as conn:
            conn.execute("UPDATE jobs SET params=? WHERE id=?",
                         (json.dumps(self._params(continuation={
                             "chain_id": job_id, "stage_index": 0,
                             "context_frames": 22, "anchor_frames": 22,
                         })), job_id))
        run_id, stage = cont.latent_coords(self._info(job_id))
        self.assertEqual(run_id, cont.chain_run_id(job_id))
        self.assertEqual(stage, 0)

    def test_legacy_job_can_still_be_continued(self):
        """旧任务的潜空间文件在链目录里 -> 仍能接续（不被新机制打断）。"""
        job_id = srv.create_job_record("t2v", self._params())
        with srv.db() as conn:
            conn.execute("UPDATE jobs SET params=? WHERE id=?",
                         (json.dumps(self._params(continuation={
                             "chain_id": job_id, "stage_index": 0,
                             "context_frames": 22, "anchor_frames": 22,
                         })), job_id))
        run_id = cont.chain_run_id(job_id)
        path = cont.stage_file_path(cs.comfy_output_dir(), run_id, 0)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"legacy-latent")

        params = self._params(continuation={"source_job": job_id,
                                            "context_frames": 22,
                                            "anchor_frames": 22})
        error = srv._prepare_continuation_params(params)
        self.assertEqual(error, "", f"旧产物应仍可接续: {error}")
        self.assertEqual(params["continuation"]["source_latent_run_id"], run_id)
        path.unlink()


if __name__ == "__main__":
    unittest.main()
