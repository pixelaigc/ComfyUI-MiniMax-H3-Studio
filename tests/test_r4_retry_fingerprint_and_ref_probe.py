# -*- coding: utf-8 -*-
"""第 4 轮收口：两个**真实缺陷**的回归测试。

缺陷 A（retry_job 指纹副本不刷新）
----------------------------------
Agent 的 `retry_job` 工具把源任务的 params **整份拷贝**过来。
`params["continuation"]["source_latent_path"]` 是**上一次**接续时算出来的
带内容指纹的副本路径。

Relay-Kit 的落盘是**原子写**（`save_file(tmp)` + `os.replace(tmp, path)`
—— 见 comfyui-H3-Relay-Kit/relay_core.py::save_av_latent），
原子替换 ⇒ **换 inode** ⇒ 之前用 `os.link` 建的硬链接副本仍指向**旧 inode**，
内容停留在旧版本。

⇒ 上游一旦被重跑，retry 就拿着**旧路径**去读回，
  读到的是**上一版的 latent** —— 静默错误：不报错、画面接错、
  界面全程显示成功。

正常接续路径（`_prepare_continuation_params`）每次都重算
（`ensure_fingerprinted_copy`），所以只有 **retry** 这一条路是坏的。

缺陷 B（`_record_ref_video_frame_count` 失败结果不缓存）
--------------------------------------------------------
探测不到参考视频帧数时，函数**直接 return** —— 既不写帧数、
也不写任何「已试过」的标记。于是**每次调用都会重新探测**
（ffprobe `-count_frames` 要真解码一遍，实测 32MB / 2 分钟 480P ≈ 3 秒，
而且是**同步阻塞**子进程调用）。

失败还会导致下一段拿不到 `ref_video_frame_count`
⇒ 工作流层走「探测不到 -> 不猜」的分支 ⇒ `video_skip_frames = 0`
⇒ **段2 的参考视频从 0 开始喂** —— 第 3 轮那个「亮度阶跃」缺陷的触发条件。

【断言纪律】
  * 期望值全是**字面量**（路径后缀、字段名、调用次数），不从被测函数反推。
  * 每条断言由「命名该行为的那条断言」抓住。
  * 断言**不自指**：不拿被测函数自己的输出算期望值。
  * **修复前必须红**：本文件在未修复的代码上跑，第一组断言必须失败。

运行：python3 -m pytest tests/test_r4_retry_fingerprint_and_ref_probe.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server as srv                                    # noqa: E402
from h3studio import continuation as cont               # noqa: E402
from h3studio import continuation_service as cs         # noqa: E402


# ======================================================================
# 缺陷 A：retry_job 的指纹副本不刷新
# ======================================================================
class RetryFingerprintRefreshTest(unittest.TestCase):
    """重试接续段时，读回路径必须刷新成**当前内容指纹**。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="h3r4-fp-")
        self.out = Path(self.tmp) / "output"
        self.out.mkdir(parents=True, exist_ok=True)
        self._orig_out = srv.OUT_DIR
        self._orig_comfy_out = cs.comfy_output_dir
        self._orig_db = srv.DB_PATH
        srv.OUT_DIR = self.out
        cs.comfy_output_dir = lambda: self.out
        srv.DB_PATH = Path(self.tmp) / "jobs.db"
        srv.init_db()

    def tearDown(self):
        srv.DB_PATH = self._orig_db
        cs.comfy_output_dir = self._orig_comfy_out
        srv.OUT_DIR = self._orig_out
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_stage(self, run_id: str, stage: int, payload: bytes) -> Path:
        p = self.out / "relay_kit" / run_id / ("stage_%05d.safetensors" % stage)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(payload)
        return p

    def _upstream_job(self) -> tuple[str, str]:
        """建一个链首段任务，返回 (job_id, latent_run_id)。"""
        job_id = srv.create_job_record("t2v", {"prompt": "x", "duration": 5})
        with srv.db() as conn:
            params = json.loads(conn.execute(
                "SELECT params FROM jobs WHERE id=?", (job_id,)).fetchone()["params"])
        return job_id, params["continuation"]["latent_run_id"]

    def test_retry_refreshes_the_fingerprint_copy_after_upstream_rerun(self):
        """核心断言：上游重跑后，retry 的读回路径必须换到新指纹。

        这条直接锁住缺陷 A 的后果：retry 必须**不再**读旧 latent。
        """
        up, up_run = self._upstream_job()
        self._write_stage(up_run, 0, b"VERSION-ONE" * 500)

        # 第一次接续：算出一个带指纹的读回路径。
        cont_params = {
            "prompt": "n", "duration": 5,
            "continuation": {
                "chain_id": up, "stage_index": 1, "source_job": up,
                "source_segment": 0,
                "source_latent_run_id": up_run, "source_latent_stage": 0,
                "context_frames": 22, "anchor_frames": 22,
                "chain_total_frames": 0, "segment_frames": 124},
        }
        err = srv._prepare_continuation_params(cont_params)
        self.assertEqual(err, "", "接续参数准备不该报错")
        path_v1 = cont_params["continuation"]["source_latent_path"]
        self.assertTrue(path_v1.endswith(".safetensors"))
        self.assertIn("_", os.path.basename(path_v1),
                      "读回路径必须带指纹后缀")

        # 上游被重跑：Relay-Kit 的原子写 = os.replace => **换 inode**。
        stage = self.out / "relay_kit" / up_run / "stage_00000.safetensors"
        tmp = stage.with_suffix(".safetensors.tmp")
        tmp.write_bytes(b"VERSION-TWO-NEW-INODE" * 500)
        os.replace(tmp, stage)

        # 旧副本仍指向旧 inode —— 这就是「副本不刷新」的机制。
        with open(path_v1, "rb") as handle:
            stale_bytes = handle.read(32)
        with open(stage, "rb") as handle:
            fresh_bytes = handle.read(32)
        self.assertNotEqual(stale_bytes, fresh_bytes,
                            "测试前提失效：原子写后旧副本的内容应该还是旧的")

        # Agent 的 retry_job：整份拷贝源任务 params（不重算指纹）。
        retry_params = json.loads(json.dumps(cont_params))
        retry_params = {"prompt": "n", "duration": 5,
                        "continuation": retry_params["continuation"]}
        srv.prepare_job_params("t2v", retry_params, [], None)
        before = retry_params["continuation"]["source_latent_path"]
        self.assertEqual(before, path_v1,
                         "测试前提失效：retry 在修复前应该沿用旧路径")

        # 修复点：重试前刷新指纹副本。
        refreshed = srv.refresh_fingerprinted_path(retry_params)
        self.assertTrue(refreshed, "刷新函数必须返回路径")
        self.assertNotEqual(refreshed, path_v1,
                            "上游重跑后读回路径没变 -> 会读回旧 latent")
        retry_params["continuation"]["source_latent_path"] = refreshed

        # 断言落到**后果**：retry 读到的是新内容。
        with open(refreshed, "rb") as handle:
            self.assertEqual(handle.read(32), fresh_bytes,
                             "刷新后的副本内容不是上游的新 latent")

    def test_refresh_is_idempotent(self):
        """内容不变时，刷新必须返回同一个路径（不能每次都换）。"""
        up, up_run = self._upstream_job()
        self._write_stage(up_run, 0, b"STABLE" * 500)
        params = {"continuation": {"source_latent_run_id": up_run,
                                   "source_latent_stage": 0}}
        first = srv.refresh_fingerprinted_path(params)
        second = srv.refresh_fingerprinted_path(params)
        self.assertEqual(first, second,
                         "内容没变却换了路径 -> 每次接续都会白白破一次缓存")

    def test_refresh_does_not_touch_segment_identity(self):
        """刷新**只**改读回路径 —— 段号 / 链 id / 接力棒坐标不得动。

        「重跑这一段」的语义要求 source_job / chain_id / stage_index 保持不变
        （第 3 轮报告 §3.3 已确认那是正确设计，不是缺陷）。
        """
        up, up_run = self._upstream_job()
        self._write_stage(up_run, 0, b"PAYLOAD" * 300)
        params = {"continuation": {
            "chain_id": up, "stage_index": 1, "source_job": up,
            "source_segment": 0,
            "source_latent_run_id": up_run, "source_latent_stage": 0,
            "source_latent_path": "/old/path.safetensors",
        }}
        before = dict(params["continuation"])
        srv.refresh_fingerprinted_path(params)
        for key in ("chain_id", "stage_index", "source_job", "source_segment",
                    "source_latent_run_id", "source_latent_stage"):
            self.assertEqual(params["continuation"][key], before[key],
                             f"刷新动了 {key} —— 改变了「重跑这一段」的语义")

    def test_refresh_returns_empty_when_no_continuation(self):
        """没有接续上下文时返回空串（不改任何东西）。"""
        self.assertEqual(srv.refresh_fingerprinted_path({}), "")
        self.assertEqual(srv.refresh_fingerprinted_path({"continuation": {}}), "")

    def test_refresh_survives_missing_source_file(self):
        """源潜空间已被清理时不能抛错（由 worker 明确报错）。"""
        params = {"continuation": {
            "source_latent_run_id": "h3studio_sNOTHERE",
            "source_latent_stage": 0,
            "source_latent_path": "/old/path.safetensors",
        }}
        result = srv.refresh_fingerprinted_path(params)
        # 源不存在 -> ensure_fingerprinted_copy 返回原路径，不抛错
        self.assertIsInstance(result, str)


class RetryJobToolRefreshTest(unittest.TestCase):
    """**端到端**：Agent 的 retry_job 工具必须刷新指纹。

    这一组是「行为级」断言 —— 它调的是**真实的 retry_job 工具**
    （不是直接调刷新函数），所以它在**未修复的代码上也会红**
    （AttributeError 那种「函数不存在」不算好红 ——
    必须红在「retry 沿用了旧路径」这个**行为**上）。

    做法：用真实的 server 模块 + 一个最小替身，把 retry_job 的
    `get_job_params` 指向我们造好的任务参数，然后断言**新建任务**
    落库后的 `source_latent_path` 是不是当前指纹那一份。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="h3r4-tool-")
        self.out = Path(self.tmp) / "output"
        self.out.mkdir(parents=True, exist_ok=True)
        self._orig_out = srv.OUT_DIR
        self._orig_comfy_out = cs.comfy_output_dir
        self._orig_db = srv.DB_PATH
        srv.OUT_DIR = self.out
        cs.comfy_output_dir = lambda: self.out
        srv.DB_PATH = Path(self.tmp) / "jobs.db"
        srv.init_db()

        # 建上游段 + 落盘潜空间
        self.up = srv.create_job_record("t2v", {"prompt": "x", "duration": 5})
        with srv.db() as conn:
            up_params = json.loads(conn.execute(
                "SELECT params FROM jobs WHERE id=?",
                (self.up,)).fetchone()["params"])
        self.up_run = up_params["continuation"]["latent_run_id"]
        self.stage = self.out / "relay_kit" / self.up_run / "stage_00000.safetensors"
        self.stage.parent.mkdir(parents=True, exist_ok=True)
        self.stage.write_bytes(b"VERSION-ONE" * 500)

        # 一次正常接续 -> 拿到带指纹的读回路径
        self.cont = {
            "chain_id": self.up, "stage_index": 1, "source_job": self.up,
            "source_segment": 0,
            "source_latent_run_id": self.up_run, "source_latent_stage": 0,
            "context_frames": 22, "anchor_frames": 22,
            "chain_total_frames": 0, "segment_frames": 124,
        }
        cont_params = {"prompt": "n", "duration": 5,
                       "continuation": dict(self.cont)}
        srv._prepare_continuation_params(cont_params)
        self.path_v1 = cont_params["continuation"]["source_latent_path"]

        # 上游被重跑（原子写 -> 换 inode）
        tmp = self.stage.with_suffix(".safetensors.tmp")
        tmp.write_bytes(b"VERSION-TWO-NEW-INODE" * 500)
        os.replace(tmp, self.stage)

        # 上游段的任务记录 = 一个「接续段」的 params（含旧路径）
        self.source_job_params = {
            "prompt": "n", "duration": 5,
            "continuation": dict(cont_params["continuation"]),
        }

    def tearDown(self):
        srv.DB_PATH = self._orig_db
        cs.comfy_output_dir = self._orig_comfy_out
        srv.OUT_DIR = self._orig_out
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_retry_job_tool_writes_the_refreshed_path_into_the_new_job(self):
        """retry_job 建出来的新任务必须用**当前**指纹的读回路径。"""
        import asyncio
        from h3studio import agent as agent_mod

        created: list[tuple[str, str, dict]] = []
        source_job_id = self.up
        source_job_params = self.source_job_params

        class _FakeServer:
            def get_job_params(self, job_id):
                if job_id != source_job_id:
                    return None
                return {"mode": "t2v",
                        "params": json.loads(json.dumps(source_job_params))}

            def prepare_job_params(self, mode, params, asset_refs=None,
                                   saved_files=None):
                params.setdefault("gen_type", "t2v")
                return asset_refs or []

            def create_job_record(self, mode, params, session_id=""):
                job_id = "retry" + str(len(created))
                created.append((job_id, mode, dict(params)))
                with srv.db() as conn:
                    conn.execute(
                        "INSERT INTO jobs (id, created_at, mode, params, status)"
                        " VALUES (?,?,?,?, 'queued')",
                        (job_id, 0, mode,
                         json.dumps(params, ensure_ascii=False)))
                return job_id

        fake = _FakeServer()
        # 【关键 —— 这决定了红绿是否作数】
        # 生产环境里 ctx.server **就是 server 模块本身**
        # （server.py 调 agent_mod.run_agent(sys.modules[__name__], ...)），
        # 所以「有没有刷新能力」取决于 server 模块**有没有这个函数**。
        # 这里照抄生产语义：**有才挂上**。
        #
        # ⇒ 未修复的版本：没有这个函数 -> retry 不会刷新 ->
        #   新任务沿用旧路径 -> 断言「路径必须变」在**行为上**变红。
        #   （不是 AttributeError 那种「函数不存在」的假红。）
        # ⇒ 已修复的版本：挂上真实实现 -> 刷新 -> 绿。
        if hasattr(srv, "refresh_fingerprinted_path"):
            fake.refresh_fingerprinted_path = srv.refresh_fingerprinted_path

        ctx = agent_mod.AgentContext(server_module=fake,
                                     provider={"name": "t", "model": "m"})
        result = asyncio.run(agent_mod.TOOLS["retry_job"]["handler"](
            ctx, {"job_id": self.up}))
        self.assertTrue(result.get("job_id"))

        self.assertEqual(len(created), 1, "retry_job 应当建出恰好一个新任务")
        new_params = created[0][2]
        new_path = new_params["continuation"]["source_latent_path"]
        self.assertNotEqual(new_path, self.path_v1,
                            "retry 沿用了旧指纹路径 -> 上游重跑后会读回旧 latent")
        # 断言落到后果：新路径的内容必须是上游的**新**内容
        with open(new_path, "rb") as handle:
            refreshed_bytes = handle.read(32)
        with open(self.stage, "rb") as handle:
            fresh_bytes = handle.read(32)
        self.assertEqual(refreshed_bytes, fresh_bytes,
                         "刷新后的副本内容不是上游的新 latent")


# ======================================================================
# 缺陷 B：_record_ref_video_frame_count 失败结果不缓存
# ======================================================================
class RefVideoFrameCountCacheTest(unittest.TestCase):
    """探测不到帧数时也要落标记 —— 不再重复探测。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="h3r4-ref-")
        self._orig_db = srv.DB_PATH
        srv.DB_PATH = Path(self.tmp) / "jobs.db"
        srv.init_db()
        self._orig_probe = srv._probe_video_frame_count
        self._orig_local = srv._comfy_input_local
        self.probe_calls: list[str] = []

    def tearDown(self):
        srv._probe_video_frame_count = self._orig_probe
        srv._comfy_input_local = self._orig_local
        srv.DB_PATH = self._orig_db
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_probe(self, result: int):
        def _probe(path):
            self.probe_calls.append(str(path))
            return result
        return _probe

    def _params(self) -> dict:
        return {
            "prompt": "x", "duration": 5,
            "ref_videos": ["h3studio/ref.mp4"],
            "continuation": {
                "chain_id": "c1", "stage_index": 0, "source_job": "",
                "source_segment": -1, "context_frames": 22,
                "anchor_frames": 22, "chain_total_frames": 0,
                "segment_frames": 124},
        }

    def _run(self, params: dict, job_id: str) -> None:
        worker = srv.Worker.__new__(srv.Worker)
        asyncio.run(srv.Worker._record_ref_video_frame_count(worker, job_id, params))

    def test_failed_probe_is_cached_and_not_retried(self):
        """核心断言：探测失败后**再次调用不得再探测**。

        修复前：3 次调用 -> 3 次 ffprobe（每次约 3 秒的同步阻塞）。
        """
        srv._comfy_input_local = lambda name: Path("/tmp/ref.mp4")
        srv._probe_video_frame_count = self._fake_probe(0)

        params = self._params()
        job_id = srv.create_job_record("r2v", {"prompt": "x", "duration": 5})
        self._run(params, job_id)
        self._run(params, job_id)
        self._run(params, job_id)

        self.assertEqual(len(self.probe_calls), 1,
                         f"探测失败后仍在重复探测（{len(self.probe_calls)} 次）—— "
                         "失败结果没有缓存")

    def test_failed_probe_writes_a_marker_to_db(self):
        """失败标记必须**落库** —— 下一段是从 DB 读上下文继承的。"""
        srv._comfy_input_local = lambda name: Path("/tmp/ref.mp4")
        srv._probe_video_frame_count = self._fake_probe(0)

        params = self._params()
        job_id = srv.create_job_record("r2v", {"prompt": "x", "duration": 5})
        self._run(params, job_id)

        with srv.db() as conn:
            row = conn.execute("SELECT params FROM jobs WHERE id=?",
                               (job_id,)).fetchone()
        stored = json.loads(row["params"]) if row else {}
        cont = stored.get("continuation") or {}
        self.assertEqual(cont.get("ref_video_frame_count_probe"), "failed",
                         "失败标记没落库 -> 下一段/下次运行还会重复探测")
        self.assertEqual(int(cont.get("ref_video_frame_count") or 0), 0,
                         "探测失败时帧数必须是 0（不知道），绝不猜")

    def test_successful_probe_is_not_reprobed(self):
        """探测成功时同样不得重复探测（这是修复前的既有正确行为）。"""
        srv._comfy_input_local = lambda name: Path("/tmp/ref.mp4")
        srv._probe_video_frame_count = self._fake_probe(123)

        params = self._params()
        job_id = srv.create_job_record("r2v", {"prompt": "x", "duration": 5})
        self._run(params, job_id)
        self._run(params, job_id)

        self.assertEqual(len(self.probe_calls), 1,
                         "探测成功后仍在重复探测")
        self.assertEqual(int(params["continuation"]["ref_video_frame_count"]), 123)

    def test_failure_marker_does_not_block_a_later_success(self):
        """标记只挡「已失败的同一份参考视频」—— 不影响别的路径。

        语义保守性检查：标记是**这一份**参考视频的探测结果，
        不是「这个任务以后都不许探测」。
        """
        srv._comfy_input_local = lambda name: Path("/tmp/ref.mp4")
        srv._probe_video_frame_count = self._fake_probe(0)
        params = self._params()
        job_id = srv.create_job_record("r2v", {"prompt": "x", "duration": 5})
        self._run(params, job_id)
        self.assertEqual(params["continuation"].get(
            "ref_video_frame_count_probe"), "failed")

        # 换一个**没有标记**的 params（模拟另一个段）—— 仍应探测一次。
        params2 = self._params()
        params2["continuation"]["chain_id"] = "c2"
        self.probe_calls.clear()
        self._run(params2, job_id)
        self.assertEqual(len(self.probe_calls), 1,
                         "失败标记误伤了别的段 —— 标记的作用域太宽")

    def test_probe_runs_off_the_event_loop(self):
        """ffprobe 是同步阻塞子进程 —— 必须走线程，不能冻结事件循环。

        这条锁住「探测不能阻塞事件循环」：与 2026-11-16「8190 不可访问」
        是同一类事故（那次是 LoRA 扫描，这次是参考视频帧数探测）。

        判据：探测**睡眠 0.3 秒**（模拟 ffprobe 真解码），
        期间另一个协程必须能跑完 —— 如果探测跑在事件循环里，
        另一个协程会被整整 0.3 秒卡住（tick 数 = 0）。
        """
        import time

        # 探测「在飞」标志 —— 只有它为 True 时才说明探测正在跑。
        state = {"inflight": False, "ticks_during": 0}

        def _slow_probe(path):
            self.probe_calls.append(str(path))
            state["inflight"] = True
            try:
                time.sleep(0.5)      # 模拟 ffprobe 的阻塞（要真解码）
            finally:
                state["inflight"] = False
            return 50

        srv._comfy_input_local = lambda name: Path("/tmp/ref.mp4")
        srv._probe_video_frame_count = _slow_probe

        params = self._params()
        job_id = srv.create_job_record("r2v", {"prompt": "x", "duration": 5})
        worker = srv.Worker.__new__(srv.Worker)

        async def _ticker():
            # 反复让出控制权，统计「探测正在飞」的窗口里拿到了几次调度。
            for _ in range(2000):
                await asyncio.sleep(0)
                if state["inflight"]:
                    state["ticks_during"] += 1
                    if state["ticks_during"] >= 3:
                        return

        async def _run_both():
            await asyncio.gather(
                srv.Worker._record_ref_video_frame_count(worker, job_id, params),
                _ticker(),
            )

        asyncio.run(_run_both())
        # 探测是同步阻塞的子进程调用 —— 跑在事件循环里时，
        # 这 0.5 秒内另一个协程**一次都调度不上**（ticks_during == 0）。
        # 走线程时事件循环是自由的 -> ticks_during >= 3。
        self.assertGreaterEqual(
            state["ticks_during"], 3,
            "探测阻塞了事件循环（在探测进行的窗口内一次都没调度）—— "
            "ffprobe 必须放到线程里跑")


if __name__ == "__main__":
    unittest.main()
