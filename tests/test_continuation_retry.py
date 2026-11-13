# -*- coding: utf-8 -*-
"""【D1 补修】重跑/重试不得覆盖原任务的潜空间 + 内容指纹破节点缓存。

【背景 —— 两个缺口，都在 D1 调研报告覆盖的缺陷类别内】

缺口 A：**重试透传 `latent_run_id` → 覆盖原任务的潜空间**
    D1 修掉了「分叉分支互相覆盖」（每段一个专属目录，由段自己的
    任务 id 派生）。但落盘目录的写入点用的是 `setdefault`：

        info.setdefault("latent_run_id", cont.segment_run_id(job_id))

    `setdefault` 只在**字段缺失**时才写。若 params 里已经带了一个
    `latent_run_id`（Agent 的 `retry_job` 工具会把源任务的 params
    **整份拷贝**过来），新任务就沿用**源任务**的目录名
    -> 写同一个 `stage_00000.safetensors`
    -> **原任务的潜空间被覆盖**。

    这与 D1 的分叉覆盖是**同一类缺陷**：落盘坐标必须与
    「产出它的那一段」一一对应，不能由「参数里恰好带了什么」决定。

缺口 B：**没有内容指纹 → ComfyUI 节点缓存拿回旧 latent**
    ComfyUI 的节点缓存 key = 节点**输入**的哈希
    （comfy_execution/caching.py 的 `get_immediate_node_signature`），
    对每个 input 取「链路 or 字面值」—— **文件路径只是普通字符串**，
    缓存**不看文件内容变没变**。

    于是「上一段潜空间被重跑替换」之后，新一段的
    `H3RelayLatentLoad`（run_id / stage_index / explicit_path 三项
    一字未变）会**命中缓存**，节点压根不被调用，返回**老 latent**
    —— 新一段实际接的是上一版的画面，**且不报错、界面全程显示成功**。

    解法（借鉴 nonebot 咏唱 h3 的内容指纹机制）：把**内容指纹**
    编进读回用的文件路径。内容变 -> 指纹变 -> 路径变 -> 节点输入必然变
    -> 缓存必然失效。

【断言纪律】
  * 期望值全是**字面量**（目录名前缀、字段名），不从被测函数反推。
  * 每条断言由「命名该行为的那条断言」抓住。
  * 断言**不自指**：不拿被测函数自己的输出算期望值。

运行：python3 -m pytest tests/test_continuation_retry.py -v
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


class RetryLatentCoordTest(unittest.TestCase):
    """缺口 A：重试/重跑不得沿用别人的落盘目录。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="h3retry-")
        self.out = Path(self.tmp) / "output"
        self.out.mkdir(parents=True, exist_ok=True)
        self._orig_out = srv.OUT_DIR
        srv.OUT_DIR = self.out

    def tearDown(self):
        srv.OUT_DIR = self._orig_out
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_job(self, params):
        return srv.create_job_record("t2v", params)

    def test_retry_of_a_job_does_not_reuse_the_source_latent_dir(self):
        """重试 A -> 新任务必须有自己的落盘目录，不能是 A 的那个。

        这条断言直接锁住 D1 的不变量：落盘坐标由**产出它的那一段**
        派生。重试用 setdefault 就会在这里变红。
        """
        a = self._make_job({"prompt": "x", "duration": 5})
        with srv.db() as conn:
            row = conn.execute("SELECT params FROM jobs WHERE id=?",
                               (a,)).fetchone()
        a_params = json.loads(row["params"])
        a_run_id = a_params["continuation"]["latent_run_id"]

        # 模拟 Agent 的 retry_job：把源任务的 params **整份拷贝**过来。
        retry_params = json.loads(json.dumps(a_params))
        b = self._make_job(retry_params)

        with srv.db() as conn:
            row = conn.execute("SELECT params FROM jobs WHERE id=?",
                               (b,)).fetchone()
        b_params = json.loads(row["params"])
        b_run_id = b_params["continuation"]["latent_run_id"]

        self.assertNotEqual(b_run_id, a_run_id,
                            "重试出来的任务沿用了源任务的潜空间落盘目录 —— "
                            "它会把源任务的潜空间覆盖掉")
        self.assertEqual(b_run_id, "h3studio_s" + b,
                         "落盘目录必须由**本段自己的任务 id** 派生")
        self.assertEqual(a_run_id, "h3studio_s" + a)

    def test_retry_keeps_the_source_latent_file_untouched(self):
        """重试跑完后，源任务的潜空间文件内容不得被改写。

        这是缺陷的**后果**断言：两个任务写同一个文件时，
        后完成者覆盖先完成者。
        """
        a = self._make_job({"prompt": "x", "duration": 5})
        with srv.db() as conn:
            a_params = json.loads(conn.execute(
                "SELECT params FROM jobs WHERE id=?", (a,)).fetchone()["params"])
        a_run_id = a_params["continuation"]["latent_run_id"]

        # 造一个「源任务已落盘」的潜空间文件。
        a_file = self.out / "relay_kit" / a_run_id / "stage_00000.safetensors"
        a_file.parent.mkdir(parents=True, exist_ok=True)
        a_file.write_bytes(b"ORIGINAL-LATENT-BYTES")
        original = a_file.read_bytes()

        # 重试：整份拷贝源任务 params。
        retry_params = json.loads(json.dumps(a_params))
        b = self._make_job(retry_params)
        with srv.db() as conn:
            b_params = json.loads(conn.execute(
                "SELECT params FROM jobs WHERE id=?", (b,)).fetchone()["params"])
        b_run_id = b_params["continuation"]["latent_run_id"]

        # 模拟 worker 写本段接力棒。
        b_file = self.out / "relay_kit" / b_run_id / "stage_00000.safetensors"
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_bytes(b"RETRY-LATENT-BYTES")

        self.assertEqual(a_file.read_bytes(), original,
                         "重试任务的落盘覆盖了源任务的潜空间文件")

    def test_continuation_segment_also_gets_its_own_dir(self):
        """接续段（带 continuation 的 params）同样不能沿用别人的目录。"""
        upstream = self._make_job({"prompt": "x", "duration": 5})
        with srv.db() as conn:
            up_params = json.loads(conn.execute(
                "SELECT params FROM jobs WHERE id=?", (upstream,)).fetchone()["params"])

        # 前端回传的上下文里带了**上游**的 latent_run_id（伪造）。
        cont_params = {
            "prompt": "next",
            "duration": 5,
            "continuation": dict(up_params["continuation"]),
        }
        cont_params["continuation"]["chain_id"] = upstream
        cont_params["continuation"]["stage_index"] = 1
        cont_params["continuation"]["source_job"] = upstream
        cont_params["continuation"]["source_latent_run_id"] = up_params[
            "continuation"]["latent_run_id"]

        new_id = self._make_job(cont_params)
        with srv.db() as conn:
            new_params = json.loads(conn.execute(
                "SELECT params FROM jobs WHERE id=?",
                (new_id,)).fetchone()["params"])
        self.assertEqual(new_params["continuation"]["latent_run_id"],
                         "h3studio_s" + new_id,
                         "接续段沿用了上游的落盘目录 —— 会覆盖上游潜空间")


class FingerprintCacheBreakTest(unittest.TestCase):
    """缺口 B：内容指纹必须让「内容变了」变成「路径变了」。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="h3fp-")
        self.out = Path(self.tmp) / "output"
        self.out.mkdir(parents=True, exist_ok=True)
        self._orig_out = srv.OUT_DIR
        srv.OUT_DIR = self.out
        self._orig_comfy_out = cs.comfy_output_dir
        cs.comfy_output_dir = lambda: self.out

    def tearDown(self):
        cs.comfy_output_dir = self._orig_comfy_out
        srv.OUT_DIR = self._orig_out
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_stage(self, run_id, stage, payload: bytes) -> Path:
        p = self.out / "relay_kit" / run_id / ("stage_%05d.safetensors" % stage)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(payload)
        return p

    def test_fingerprint_changes_when_content_changes(self):
        """同一坐标、内容不同 -> 指纹必须不同。"""
        run_id = "h3studio_sFP1"
        self._write_stage(run_id, 0, b"A" * 1024)
        fp1 = cs.latent_fingerprint(
            self.out / "relay_kit" / run_id / "stage_00000.safetensors")
        self._write_stage(run_id, 0, b"B" * 1024)
        fp2 = cs.latent_fingerprint(
            self.out / "relay_kit" / run_id / "stage_00000.safetensors")
        self.assertNotEqual(fp1, fp2,
                            "内容变了但指纹没变 -> ComfyUI 会命中旧缓存")

    def test_fingerprint_changes_when_only_head_bytes_change(self):
        """**只有头部字节变了** -> 指纹也必须变。

        指纹要覆盖文件的头部采样区 —— 只采样中段/尾部会漏掉
        「重跑后头部不同」这种改动（潜空间的头部正是起始帧）。
        """
        run_id = "h3studio_sFP1H"
        self._write_stage(run_id, 0, b"HEAD-OLD" + b"M" * 4096)
        p = self.out / "relay_kit" / run_id / "stage_00000.safetensors"
        fp1 = cs.latent_fingerprint(p)
        p.write_bytes(b"HEAD-NEW" + b"M" * 4096)
        fp2 = cs.latent_fingerprint(p)
        self.assertNotEqual(fp1, fp2,
                            "只有头部字节变了但指纹没变 -> 指纹没覆盖头部采样区")

    def test_fingerprint_changes_when_only_tail_bytes_change(self):
        """**只有尾部字节变了** -> 指纹也必须变。

        潜空间的尾部正是「上一段的末帧」—— 它变了必须能被察觉。
        """
        run_id = "h3studio_sFP1T"
        self._write_stage(run_id, 0, b"M" * 4096 + b"TAIL-OLD")
        p = self.out / "relay_kit" / run_id / "stage_00000.safetensors"
        fp1 = cs.latent_fingerprint(p)
        p.write_bytes(b"M" * 4096 + b"TAIL-NEW")
        fp2 = cs.latent_fingerprint(p)
        self.assertNotEqual(fp1, fp2,
                            "只有尾部字节变了但指纹没变 -> 指纹没覆盖尾部采样区")

    def test_fingerprint_is_stable_for_same_content(self):
        """内容不变 -> 指纹不变（否则每次接续都会破缓存，失去意义）。"""
        run_id = "h3studio_sFP2"
        self._write_stage(run_id, 0, b"STABLE" * 100)
        p = self.out / "relay_kit" / run_id / "stage_00000.safetensors"
        self.assertEqual(cs.latent_fingerprint(p),
                         cs.latent_fingerprint(p))

    def test_fingerprint_includes_file_size(self):
        """大小变了 -> 指纹必须变（哪怕头部字节一样）。"""
        run_id = "h3studio_sFP3"
        self._write_stage(run_id, 0, b"SAME-HEADER" * 10)
        p1 = self.out / "relay_kit" / run_id / "stage_00000.safetensors"
        fp1 = cs.latent_fingerprint(p1)
        p1.write_bytes(b"SAME-HEADER" * 10 + b"EXTRA")
        fp2 = cs.latent_fingerprint(p1)
        self.assertNotEqual(fp1, fp2)

    def test_read_back_path_carries_the_fingerprint(self):
        """读回路径必须带指纹后缀 -> 节点输入变了 -> 缓存必然失效。"""
        run_id = "h3studio_sFP4"
        self._write_stage(run_id, 0, b"PAYLOAD" * 200)
        path = cs.ensure_fingerprinted_copy(run_id, 0)
        self.assertTrue(path.is_file())
        self.assertTrue(path.name.startswith("stage_00000_"),
                        f"读回路径没带指纹后缀：{path.name}")
        self.assertTrue(path.name.endswith(".safetensors"))
        self.assertNotEqual(path.name, "stage_00000.safetensors",
                            "读回路径与落盘路径相同 -> 内容变了会命中旧缓存")

    def test_read_back_path_changes_after_source_is_rerun(self):
        """**核心断言**：上一段被重跑（内容变）-> 读回路径必须变。

        这条正是 D1 第 4 项要的：路径不变 -> ComfyUI 命中缓存 ->
        返回老 latent -> 新一段接的是上一版画面（静默错误）。
        """
        run_id = "h3studio_sFP5"
        self._write_stage(run_id, 0, b"VERSION-ONE" * 200)
        p1 = cs.ensure_fingerprinted_copy(run_id, 0)

        # 重跑：同一坐标，内容换掉。
        self._write_stage(run_id, 0, b"VERSION-TWO" * 200)
        p2 = cs.ensure_fingerprinted_copy(run_id, 0)

        self.assertNotEqual(str(p1), str(p2),
                            "上一段重跑后读回路径没变 -> ComfyUI 会拿回旧 latent")

    def test_fingerprinted_copy_has_same_bytes_as_source(self):
        """副本内容必须与源文件一致（不能是空壳或截断）。"""
        run_id = "h3studio_sFP6"
        payload = b"REAL-LATENT-BYTES" * 300
        src = self._write_stage(run_id, 0, payload)
        dst = cs.ensure_fingerprinted_copy(run_id, 0)
        self.assertEqual(dst.read_bytes(), src.read_bytes())

    def test_fingerprinted_copy_is_idempotent(self):
        """重复调用返回同一个副本（幂等，不重复占盘）。"""
        run_id = "h3studio_sFP7"
        self._write_stage(run_id, 0, b"IDEMPOTENT" * 100)
        first = cs.ensure_fingerprinted_copy(run_id, 0)
        second = cs.ensure_fingerprinted_copy(run_id, 0)
        self.assertEqual(str(first), str(second))
        copies = list((self.out / "relay_kit" / run_id).glob("stage_00000_*.safetensors"))
        self.assertEqual(len(copies), 1)

    def test_missing_source_returns_original_path(self):
        """源文件不存在时返回原路径（错误信息里仍是真实路径）。"""
        path = cs.ensure_fingerprinted_copy("h3studio_sNOPE", 0)
        self.assertEqual(path.name, "stage_00000.safetensors")

    def test_cleanup_removes_stale_copies_only(self):
        """清理只删「非当前指纹」的旧副本，当前那份必须留着。"""
        run_id = "h3studio_sFP8"
        self._write_stage(run_id, 0, b"OLD" * 100)
        stale = cs.ensure_fingerprinted_copy(run_id, 0)
        self.assertTrue(stale.is_file())

        # 重跑 -> 内容变 -> 新指纹。
        self._write_stage(run_id, 0, b"NEW" * 100)
        current = cs.ensure_fingerprinted_copy(run_id, 0)
        self.assertNotEqual(str(stale), str(current))

        removed = cs.cleanup_fingerprint_copies(run_id, 0)
        self.assertEqual(removed, 1)
        self.assertFalse(stale.is_file(), "旧指纹副本没被清掉")
        self.assertTrue(current.is_file(), "当前指纹副本被误删了")

    def test_context_read_target_carries_fingerprint(self):
        """接续上下文里的读回路径必须带指纹（前端拿到的那份）。"""
        run_id = "h3studio_sFP9"
        self._write_stage(run_id, 0, b"CTX" * 200)
        params = {
            "prompt": "x", "duration": 5,
            "continuation": {
                "chain_id": "FP9", "stage_index": 0, "source_job": "",
                "source_segment": -1,
                "latent_run_id": run_id, "latent_stage": 0,
                "context_frames": 22, "anchor_frames": 22,
                "chain_total_frames": 0, "segment_frames": 124,
            },
        }
        ctx = cs.build_continuation_context("t2v", params, "FP9JOB")
        self.assertTrue(ctx["ok"], ctx.get("error"))
        read_target = ctx["continuation"]["source_latent_path"]
        self.assertIn("_", os.path.basename(read_target),
                      f"接续上下文的读回路径没带指纹：{read_target}")
        self.assertTrue(os.path.basename(read_target).startswith("stage_00000_"))


if __name__ == "__main__":
    unittest.main()
