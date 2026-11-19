# -*- coding: utf-8 -*-
"""【2026-11-19】两项生成状态缺陷的回归测试。

缺陷 1：视频任务不再因「运行超过 30 分钟」被判失败
缺陷 2：ComfyUI OOM / failed 不再被误报为「生成成功」（全黑预览）

【缺陷 1 的取证 —— 线上 jobs.db】
    任务 `56651c0ad452`（mode=r2v）：
        status = 'failed'
        error  = '任务运行超过 30 分钟仍未完成，已判定为超时失败。…'
        started_at=1795073671  finished_at=1795075472   <- 跑了 1801 秒
    视频任务跑了 30 分钟就被硬判失败 —— 而视频跑很久是正常现象。

【缺陷 2 的取证 —— 线上 jobs.db + 本机 ComfyUI /history】
    任务 `628d92cfc062` / `86db94915467`：
        status  = 'done'
        error   = '显存不足（Allocation on device 0 would exceed allowed
                   memory. (out of memory)）。…'
        outputs = []
    对应的 ComfyUI history `921b024c-…`：
        status.status_str = 'error'
        outputs           = {'301': {'text': [...]}}   <- **只有文本节点**
    根因：worker 先判 `hist.get("outputs")` 非空（文本节点也让它非空）
    -> 走成功分支 -> `_extract_outputs` 取不出媒体 -> saved=[] ->
    标 status='done' -> 前端显示「生成成功」+ 空白（黑）预览区。

【断言纪律】
  * 期望值是**字面量**，不从被测函数反推；
  * 每条断言由「命名该行为的那条断言」抓住；
  * 前端断言用**真实 JS 执行**（node），不是字符串匹配源码。

运行：python3 -m pytest tests/test_gen_status_defects.py -v
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import server as srv                                    # noqa: E402
from h3studio import gen_outcome                        # noqa: E402
from h3studio import job_timeout                       # noqa: E402
from h3studio import running_truth                      # noqa: E402


NODE = shutil.which("node")



def _strip_js_comments(text: str) -> str:
    """剥掉 JS 的行注释与块注释（保留字符串字面量里的内容）。

    【为什么需要】断言要针对**代码**而不是注释 ——
    注释里必须写清「原来错在哪」，必然出现「30 分钟」这几个字。
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    out.append(text[i:i + 2])
                    i += 2
                    continue
                out.append(text[i])
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# ------------------------------------------------------------------
# 缺陷 1：视频任务不再因运行时长被判失败
# ------------------------------------------------------------------

class VideoTimeoutPolicyTest(unittest.TestCase):
    """job_timeout —— 按媒体类型区分的运行上限策略。"""

    def test_video_modes_map_to_video_media(self):
        """视频类 mode（文生视频 / 首尾帧 / 参考生视频 / 长片）-> video。"""
        for mode in ("t2v", "flf", "r2v", "longfilm"):
            self.assertEqual(job_timeout.media_of_mode(mode),
                             job_timeout.MEDIA_VIDEO,
                             f"{mode} 必须被识别为视频类")

    def test_image_and_audio_modes_keep_their_own_media(self):
        """图像 / 音频类 mode -> image / audio（不受视频策略影响）。"""
        for mode in ("t2i", "t2i_qwen", "t2i_moss", "t2i_h3image",
                     "i2i_qwen", "i2i_h3image"):
            self.assertEqual(job_timeout.media_of_mode(mode),
                             job_timeout.MEDIA_IMAGE, mode)
        for mode in ("t2a", "a2a"):
            self.assertEqual(job_timeout.media_of_mode(mode),
                             job_timeout.MEDIA_AUDIO, mode)

    def test_video_default_timeout_is_unlimited(self):
        """【红线 1】视频类默认上限 = 0（不限时）。"""
        self.assertEqual(job_timeout.VIDEO_TIMEOUT_SEC, 0.0,
                         "视频任务默认**不能**有运行时长上限 —— "
                         "跑 1 天也不该被判失败")

    def test_timeout_for_media_returns_zero_for_video(self):
        """按媒体类型取上限时，视频类拿到 0。"""
        limit = job_timeout.timeout_for_media(
            job_timeout.MEDIA_VIDEO,
            image_timeout_sec=1800.0, video_timeout_sec=0.0)
        self.assertEqual(limit, 0.0,
                         "视频类必须拿到 0（不限时），否则 30 分钟误判会回来")

    def test_timeout_for_media_keeps_limit_for_image(self):
        """图像 / 音频类保留可配置上限。"""
        limit = job_timeout.timeout_for_media(
            job_timeout.MEDIA_IMAGE,
            image_timeout_sec=1800.0, video_timeout_sec=0.0)
        self.assertEqual(limit, 1800.0)

    def test_video_limit_is_still_configurable(self):
        """上限**可配置**：显式给了非 0 值就按它走。"""
        limit = job_timeout.timeout_for_media(
            job_timeout.MEDIA_VIDEO,
            image_timeout_sec=1800.0, video_timeout_sec=7200.0)
        self.assertEqual(limit, 7200.0,
                         "上限可配置 —— 用户想设上限时必须能设上")

    def test_unknown_mode_is_not_treated_as_video(self):
        """未知 mode -> other（拿图像的上限），**不能**猜成视频。

        猜错方向的代价不对称：把图像当视频 = 永不超时（卡死风险）；
        把视频当其它 = 误判失败。所以未知一律走保守口径。
        """
        self.assertEqual(job_timeout.media_of_mode("some_unknown_mode"),
                         job_timeout.MEDIA_OTHER)

    def test_timeout_reason_mentions_actual_minutes(self):
        """文案里的分钟数必须来自**实际阈值**（上限可配置，不能写死）。"""
        text = job_timeout.timeout_reason(600.0)
        self.assertIn("10 分钟", text)
        self.assertNotIn("30 分钟", text,
                          "文案里的分钟数必须来自实际阈值，不能写死")

    def test_timeout_reason_for_zero_limit_explains_no_time_limit(self):
        """上限为 0 时的文案要说清「不按运行时长限制」。"""
        text = job_timeout.timeout_reason(0.0)
        self.assertIn("不按运行时长限制", text)


class ServerTimeoutWiringTest(unittest.TestCase):
    """server.py —— 上限策略的接线（不是「所有任务共用一个阈值」了）。"""

    def test_server_has_separate_video_timeout(self):
        """服务端必须有**独立的**视频上限常量。"""
        self.assertTrue(hasattr(srv, "RUNNING_TIMEOUT_VIDEO_SEC"))
        self.assertEqual(srv.RUNNING_TIMEOUT_VIDEO_SEC, 0.0,
                         "视频类默认上限必须是 0（不限时）")

    def test_timeout_sec_for_mode_treats_video_as_unlimited(self):
        """视频 mode -> 上限 0；图像 mode -> 上限不为 0。"""
        self.assertEqual(srv.timeout_sec_for_mode("t2v"), 0.0)
        self.assertEqual(srv.timeout_sec_for_mode("flf"), 0.0)
        self.assertEqual(srv.timeout_sec_for_mode("r2v"), 0.0)
        self.assertGreater(srv.timeout_sec_for_mode("t2i"), 0.0)

    def test_config_json_supports_video_timeout_key(self):
        """config.json 支持 `running_timeout_video_sec`（可配置项）。"""
        self.assertIn("running_timeout_video_sec", srv.DEFAULT_CONFIG)

    def test_timeout_policy_endpoint_exists(self):
        """有只读接口暴露当前生效的上限（用户看得见、可自查）。"""
        app = srv.make_app()
        paths = {str(getattr(getattr(r, "resource", None), "canonical", "") or "")
                 for r in app.router.routes()}
        self.assertIn("/api/timeout-policy", paths,
                      "缺少 /api/timeout-policy 接口")


class VideoJobNotTimedOutTest(unittest.TestCase):
    """worker 的轮询循环 —— 视频任务跑超 30 分钟仍应保持「进行中」。

    【断言纪律】这里**不**真的等 30 分钟：把上限调到极小，
    然后构造「视频任务（上限 0）+ 超时阈值不为 0」的组合，
    断言视频任务**不**因为时长被判失败。
    """

    def setUp(self):
        self._orig_db = srv.DB_PATH
        self._orig_out = srv.OUT_DIR
        self._orig_comfy = srv.COMFY
        self._orig_submit = srv.comfy_submit
        self._orig_refresh = srv.refresh_comfy_node_names
        self._orig_interval = srv.CFG["poll_interval"]
        self._orig_timeout = srv.RUNNING_TIMEOUT_SEC
        self._orig_video_timeout = srv.RUNNING_TIMEOUT_VIDEO_SEC
        self.tmp = tempfile.mkdtemp(prefix="h3-timeout-")
        srv.DB_PATH = Path(self.tmp) / "jobs.db"
        srv.OUT_DIR = Path(self.tmp) / "outputs"
        srv.OUT_DIR.mkdir(parents=True, exist_ok=True)
        srv.init_db()
        srv.CFG["poll_interval"] = 0.01
        srv.QUEUE_CHECK_INTERVAL = 0.0

    def tearDown(self):
        srv.DB_PATH = self._orig_db
        srv.OUT_DIR = self._orig_out
        srv.COMFY = self._orig_comfy
        srv.comfy_submit = self._orig_submit
        srv.refresh_comfy_node_names = self._orig_refresh
        srv.CFG["poll_interval"] = self._orig_interval
        srv.RUNNING_TIMEOUT_SEC = self._orig_timeout
        srv.RUNNING_TIMEOUT_VIDEO_SEC = self._orig_video_timeout
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_job(self, mode):
        job_id = srv.create_job_record(mode, {"prompt": "long video", "duration": 5})
        with srv.db() as conn:
            # 模拟「已经跑了很久」（2 小时 >> 30 分钟）
            conn.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?",
                         (time.time() - 7200, job_id))
        return job_id

    def _run_worker(self, job_id, mode, history_result, queue_ids):
        async def _fake_submit(session, workflow, client_id=None):
            return "pid-probe"

        async def _fake_refresh(session=None, force=False):
            return set()

        async def _fake_history(session, prompt_id):
            return history_result

        async def _fake_queue_ids(session):
            return queue_ids

        srv.comfy_submit = _fake_submit
        srv.refresh_comfy_node_names = _fake_refresh
        srv.comfy_history = _fake_history
        srv.fetch_comfy_queue_ids = _fake_queue_ids

        worker = srv.Worker()
        job_row = {"id": job_id, "mode": mode,
                   "params": json.dumps({"prompt": "long video", "duration": 5})}
        asyncio.run(worker._run(job_row))
        with srv.db() as conn:
            row = conn.execute("SELECT status, error FROM jobs WHERE id=?",
                               (job_id,)).fetchone()
        return (row["status"], row["error"] or "")

    def test_video_running_over_30min_is_not_failed_by_duration(self):
        """【红线 1】视频任务跑了 2 小时，仍在 ComfyUI 队列里 ->
        **不能**因时长被判失败。"""
        job_id = self._make_job("t2v")
        # 图像类的上限故意设得很小（模拟「如果还用全局阈值就会被误杀」）
        srv.RUNNING_TIMEOUT_SEC = 1.0
        srv.RUNNING_TIMEOUT_VIDEO_SEC = 0.0
        # history 空 + 队列里有它 -> 判定为「还在跑」-> 继续等。
        # 用「取消」把测试收尾（模拟用户取消），断言的是它**没被超时杀掉**。
        with srv.db() as conn:
            conn.execute("UPDATE jobs SET status='canceled' WHERE id=?", (job_id,))
        status, error = self._run_worker(job_id, "t2v", None, {"pid-probe"})
        self.assertEqual(status, "canceled",
                         "视频任务在队列里跑很久时不能被判失败")
        self.assertNotIn("超时", error,
                         "视频任务不该出现超时文案")

    def test_image_task_still_times_out(self):
        """【取舍】图像类**保留**超时上限 —— 跑满上限判失败。

        理由：图像本机是秒级 ~ 分钟级完成，跑满上限基本等于卡死，
        保留上限能让用户更快拿到明确失败而不是无限干等。
        """
        job_id = self._make_job("t2i")
        srv.RUNNING_TIMEOUT_SEC = 1.0     # 上限调到 1 秒
        srv.RUNNING_TIMEOUT_VIDEO_SEC = 0.0
        status, error = self._run_worker(job_id, "t2i", None, {"pid-probe"})
        self.assertEqual(status, "failed",
                         "图像类超过运行上限仍应判失败（保留原策略）")
        self.assertIn("超时", error)

    def test_video_task_lost_from_queue_still_fails(self):
        """【关键】视频类**不限时**不等于「永不失败」——
        任务从 ComfyUI 队列与历史中消失时仍必须明确失败。

        没有这一条，视频任务就会变成「永远运行中」的另一个版本。
        """
        job_id = self._make_job("t2v")
        srv.RUNNING_TIMEOUT_SEC = 1.0
        srv.RUNNING_TIMEOUT_VIDEO_SEC = 0.0
        # history 空 + 队列里没有它 -> 判定为「已丢失」
        status, error = self._run_worker(job_id, "t2v", None, set())
        self.assertEqual(status, "failed",
                         "视频任务从 ComfyUI 队列消失时必须判失败")
        self.assertIn("队列", error)


# ------------------------------------------------------------------
# 缺陷 2：OOM / failed 不再被误报为「生成成功」
# ------------------------------------------------------------------

class OutcomeJudgementTest(unittest.TestCase):
    """gen_outcome —— 成功判定的纯函数层。"""

    def test_oom_history_with_text_output_is_failed(self):
        """【红线 2】OOM 的 history（outputs 里只有文本节点）-> 判失败。

        这是线上缺陷 2 的**原样形状**（取自 ComfyUI /history/921b024c-…）。
        """
        hist = {
            "outputs": {"301": {"text": ["Attention: Comfy Kitchen INT8 Sparse\n"]}},
            "status": {
                "status_str": "error",
                "completed": False,
                "messages": [
                    ["execution_error", {
                        "node_type": "SelfLiftH3Sampler",
                        "exception_type": "torch.cuda.OutOfMemoryError",
                        "exception_message": "Allocation on device 0 would "
                                             "exceed allowed memory. (out of memory)",
                    }],
                ],
            },
        }
        outcome = gen_outcome.history_outcome(hist)
        self.assertTrue(outcome["failed"],
                        "status=error 时必须判失败，**无论** outputs 里有什么")
        self.assertEqual(outcome["outcome"], gen_outcome.OUTCOME_FAILED)
        self.assertFalse(outcome["pending"])

    def test_success_with_media_output_is_ok(self):
        """正常成功（status=success + 媒体产物）-> OK。"""
        hist = {
            "outputs": {"18": {"images": [{"filename": "x.png",
                                          "subfolder": "", "type": "output"}]}},
            "status": {"status_str": "success", "completed": True, "messages": []},
        }
        outcome = gen_outcome.history_outcome(hist)
        self.assertFalse(outcome["failed"])
        self.assertEqual(outcome["outcome"], gen_outcome.OUTCOME_OK)
        self.assertTrue(outcome["has_media"])

    def test_status_none_means_still_running(self):
        """【关键】status=None -> **还在跑**（不能判失败）。

        ComfyUI 在任务没跑完时 status 就是 None（execution.py 的
        status_dict 初值）。把它当成失败会把正常的长视频任务直接判死。
        """
        hist = {"outputs": {}, "status": None}
        outcome = gen_outcome.history_outcome(hist)
        self.assertTrue(outcome["pending"],
                        "status=None 表示任务还在跑，不能判失败")
        self.assertFalse(outcome["failed"])

    def test_malformed_status_is_failed(self):
        """status 存在但不是 dict -> 快速失败（别挂死）。"""
        hist = {"outputs": {}, "status": "error"}
        outcome = gen_outcome.history_outcome(hist)
        self.assertTrue(outcome["failed"])
        self.assertIn("格式异常", outcome["reason"])

    def test_success_without_media_output_is_failed(self):
        """status=success 但没有任何媒体产物 -> 判失败（不是「成功但空」）。"""
        hist = {"outputs": {"301": {"text": ["只是文本"]}},
                "status": {"status_str": "success", "completed": True}}
        outcome = gen_outcome.history_outcome(hist)
        self.assertTrue(outcome["failed"],
                        "没有媒体产物不能算成功（否则就是「全黑预览」）")

    def test_has_media_output_ignores_text_nodes(self):
        """文本节点不算媒体产物。"""
        self.assertFalse(gen_outcome.has_media_output(
            {"outputs": {"301": {"text": ["x"]}}}))
        self.assertTrue(gen_outcome.has_media_output(
            {"outputs": {"18": {"images": [{"filename": "a.png"}]}}}))

    def test_outputs_are_real_rejects_missing_file(self):
        """产物文件缺失 -> 校验不通过。"""
        with tempfile.TemporaryDirectory() as tmp:
            ok, problems = gen_outcome.outputs_are_real(
                [{"kind": "video", "file": "videos/nope.mp4"}], tmp)
            self.assertFalse(ok, "文件缺失必须被校验拦下")
            self.assertTrue(problems)

    def test_outputs_are_real_rejects_zero_byte_file(self):
        """产物文件 0 字节 -> 校验不通过（0 字节视频就是「全黑」）。"""
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "v.mp4").write_bytes(b"")
            ok, problems = gen_outcome.outputs_are_real(
                [{"kind": "video", "file": "v.mp4"}], tmp)
            self.assertFalse(ok, "0 字节产物必须被校验拦下")
            self.assertTrue(any("为空" in p for p in problems))

    def test_outputs_are_real_accepts_nonempty_file(self):
        """真实存在的非空文件 -> 校验通过。"""
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "v.mp4").write_bytes(b"x" * 32)
            ok, problems = gen_outcome.outputs_are_real(
                [{"kind": "video", "file": "v.mp4"}], tmp)
            self.assertTrue(ok, f"正常产物不应被拦下：{problems}")

    def test_empty_saved_list_is_not_ok(self):
        """产物列表为空 -> 校验不通过。"""
        ok, problems = gen_outcome.outputs_are_real([], None)
        self.assertFalse(ok)
        self.assertTrue(problems)


class WorkerOOMRegressionTest(unittest.TestCase):
    """worker 的轮询循环 —— OOM 必须判失败，且**不产生**黑图成功提示。

    这是用户报的缺陷 2 的**可复现回归用例**。
    """

    def setUp(self):
        self._orig_db = srv.DB_PATH
        self._orig_out = srv.OUT_DIR
        self._orig_comfy = srv.COMFY
        self._orig_submit = srv.comfy_submit
        self._orig_refresh = srv.refresh_comfy_node_names
        self._orig_interval = srv.CFG["poll_interval"]
        self.tmp = tempfile.mkdtemp(prefix="h3-oom-")
        srv.DB_PATH = Path(self.tmp) / "jobs.db"
        srv.OUT_DIR = Path(self.tmp) / "outputs"
        srv.OUT_DIR.mkdir(parents=True, exist_ok=True)
        srv.init_db()
        srv.CFG["poll_interval"] = 0.01
        srv.QUEUE_CHECK_INTERVAL = 0.0

    def tearDown(self):
        srv.DB_PATH = self._orig_db
        srv.OUT_DIR = self._orig_out
        srv.COMFY = self._orig_comfy
        srv.comfy_submit = self._orig_submit
        srv.refresh_comfy_node_names = self._orig_refresh
        srv.CFG["poll_interval"] = self._orig_interval
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_worker_with_history(self, mode, history_result):
        job_id = srv.create_job_record(mode, {"prompt": "oom probe", "duration": 5})
        with srv.db() as conn:
            conn.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?",
                         (time.time(), job_id))

        async def _fake_submit(session, workflow, client_id=None):
            return "pid-probe"

        async def _fake_refresh(session=None, force=False):
            return set()

        async def _fake_history(session, prompt_id):
            return history_result

        async def _fake_queue_ids(session):
            return {"pid-probe"}

        srv.comfy_submit = _fake_submit
        srv.refresh_comfy_node_names = _fake_refresh
        srv.comfy_history = _fake_history
        srv.fetch_comfy_queue_ids = _fake_queue_ids

        worker = srv.Worker()
        job_row = {"id": job_id, "mode": mode,
                   "params": json.dumps({"prompt": "oom probe", "duration": 5})}
        asyncio.run(worker._run(job_row))
        with srv.db() as conn:
            row = conn.execute("SELECT status, error, outputs FROM jobs WHERE id=?",
                               (job_id,)).fetchone()
        return {"id": job_id, "status": row["status"], "error": row["error"] or "",
                "outputs": row["outputs"]}

    def test_oom_history_marks_job_failed_not_done(self):
        """【回归用例 · 缺陷 2】OOM -> status='failed'，**不能**是 'done'。"""
        hist = {
            "outputs": {"301": {"text": ["Attention: Comfy Kitchen INT8 Sparse\n"]}},
            "status": {
                "status_str": "error",
                "completed": False,
                "messages": [
                    ["execution_error", {
                        "node_type": "SelfLiftH3Sampler",
                        "exception_type": "torch.cuda.OutOfMemoryError",
                        "exception_message": "Allocation on device 0 would "
                                             "exceed allowed memory. (out of memory)",
                    }],
                ],
            },
        }
        result = self._run_worker_with_history("t2v", hist)
        self.assertEqual(result["status"], "failed",
                         "OOM 任务必须判失败 —— 原来被判成了 done（生成成功）")
        self.assertIn("显存不足", result["error"],
                      "失败原因必须明确指出显存不足")

    def test_oom_history_produces_no_outputs(self):
        """【回归用例 · 缺陷 2】OOM 不得留下任何产物（否则就是黑图来源）。"""
        hist = {
            "outputs": {"301": {"text": ["x"]}},
            "status": {"status_str": "error", "completed": False,
                       "messages": [["execution_error", {
                           "node_type": "X",
                           "exception_message": "out of memory"}]]},
        }
        result = self._run_worker_with_history("t2v", hist)
        outputs = json.loads(result["outputs"] or "[]")
        self.assertEqual(outputs, [],
                         "失败任务不得记录任何产物条目（那会让前端渲染出黑图）")

    def test_failed_job_has_no_preview_outputs_via_api(self):
        """【回归用例 · 缺陷 2】接口下发的 payload 里，
        失败任务**没有**任何可渲染的产物。"""
        hist = {
            "outputs": {"301": {"text": ["x"]}},
            "status": {"status_str": "error", "completed": False,
                       "messages": [["execution_error", {
                           "node_type": "X",
                           "exception_message": "out of memory"}]]},
        }
        result = self._run_worker_with_history("t2v", hist)
        job = srv.row_to_job(self._row_of(result["id"]))
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["outputs"], [],
                         "失败任务的 outputs 必须为空（前端据此不渲染预览）")
        self.assertTrue(job["error"], "失败任务必须带原因")

    def _row_of(self, job_id):
        with srv.db() as conn:
            return conn.execute("SELECT * FROM jobs WHERE id=?",
                                (job_id,)).fetchone()

    def test_success_path_still_marks_done(self):
        """【不破坏既有行为】正常成功 -> 仍然是 done（有产物）。"""
        # 造一个真实的媒体产物文件（下载桩会写它）。
        hist = {
            "outputs": {"18": {"images": [{"filename": "ok.png",
                                          "subfolder": "", "type": "output"}]}},
            "status": {"status_str": "success", "completed": True, "messages": []},
        }

        async def _fake_download(session, item):
            path = srv.OUT_DIR / "i-test-000001.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fakepng" * 100)
            return "i-test-000001.png"

        orig_download = srv.comfy_download_output
        srv.comfy_download_output = _fake_download
        try:
            result = self._run_worker_with_history("t2i", hist)
        finally:
            srv.comfy_download_output = orig_download
        self.assertEqual(result["status"], "done",
                         "正常成功必须仍然是 done —— 修复不能误杀成功任务")
        outputs = json.loads(result["outputs"] or "[]")
        self.assertTrue(outputs, "成功任务必须有产物")


class FrontendNoBlackPreviewTest(unittest.TestCase):
    """前端 —— 失败 / 无产物时**不得**渲染空白（黑）预览。"""

    @unittest.skipUnless(NODE, "需要 node 才能执行真实 JS")
    def test_done_without_outputs_renders_explicit_notice(self):
        """status=done 且 outputs=[] -> 显示明确的「没有产物」提示。

        原来这里渲染的是一个**空**的 .job-preview（背景 #0d0f12 近黑）
        -> 用户看到「生成成功 + 一张全黑图」。
        """
        script = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf-8');

// 抠出 jobCard 里的「预览区渲染」那一段来执行。
// 断言的是**同一份源码**，不是复制品 —— 变异「改 app.js」会被抓住。
const start = src.indexOf('let preview = "";');
if (start < 0) throw new Error('找不到预览渲染起点');
const end = src.indexOf('const topRight =', start);
if (end < 0) throw new Error('找不到预览渲染终点');
let block = src.slice(start, end);

// 把它包成一个函数（块里有 let 声明，不能直接 eval 到同一作用域）。
const fn = new Function(
  'job', 'runningOrQueued', 'pctText', 'prog', 'outputUrl',
  'friendlyError', 'info',
  block + '\nreturn preview;');

// done + 无产物
const job = { outputs: [], status: 'done', error: '', progress: 1,
              progress_text: '', started_at: 0, created_at: 0, params: {} };
const preview = fn(job, false, 'x', 1, () => '',
                   (t) => t || '生成失败',
                   { name: 'n', kind: 'video' });
console.log(JSON.stringify({ preview: preview }));
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.js"
            path.write_text(script, encoding="utf-8")
            proc = subprocess.run(
                [NODE, str(path), str(REPO / "web" / "app.js")],
                capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("没有产物", proc.stdout,
                      "done 且无产物时必须显示明确提示，不能留空黑框")

    def test_no_hardcoded_30min_text_in_frontend(self):
        """前端**代码**里不得再出现写死的 30 分钟上限。

        【为什么只查代码不查注释】注释里要写清楚「原来错在哪」，
        必然会出现「30 分钟」这几个字（那是缺陷说明，不是行为）。
        所以这里把注释剥掉再断言 —— 断言的是**行为**，不是文案。
        """
        for name in ("app.js", "agent.js", "generation-queue.js", "job-detail.js"):
            text = (REPO / "web" / name).read_text(encoding="utf-8")
            code = _strip_js_comments(text)
            self.assertNotIn("30 * 60 * 1000", code,
                             f"{name} 仍有写死的 30 分钟轮询上限")
            self.assertNotIn("超过 30 分钟", code,
                             f"{name} 仍有写死的「超过 30 分钟」话术")


if __name__ == "__main__":
    unittest.main()
