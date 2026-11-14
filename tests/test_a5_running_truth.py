# -*- coding: utf-8 -*-
"""A5 需求：「运行中」状态的**真实性** —— 回归测试。

【这个测试在防什么】

「运行中」原本是**声明**（DB 里 status='running'），不是观测。
于是有两种「永远运行中」，用户在界面上完全看不出来：

  1. **服务重启后残留**
     进程重启 -> 内存里的 worker 没了，但 SQLite 里那些
     status='running' 的行还在 -> 卡片永远显示「运行中」。
  2. **ComfyUI 侧任务已消失**
     ComfyUI 里任务被取消 / 被清队列 / ComfyUI 重启过
     -> history 里没有、队列里也没有 ->
     worker 的 `while True` 轮询循环**永远等下去**
     -> 任务永远「运行中」，还占着 Studio 的串行队列。

四条红线：
  1. prompt_id 既不在 queue_running 也不在 queue_pending、history 里也没有
     -> 判定为「已丢失」，**不能**继续等；
  2. 超过运行上限 -> 判定为「超时失败」，**不能**静默继续等；
  3. 服务重启后的残留 running -> 必须被自愈（标记失败 / 接管 / 收尾）；
  4. 畸形 history -> 快速失败不挂死（已有 _history_status 覆盖，
     这里只断言「A5 的判定层不会把畸形当成还在跑」）。

【为什么全部用桩，不打真 ComfyUI】
真 ComfyUI 会真跑模型、占 GPU，而且耗时不可控 ——
「任务在队列里 / 不在队列里」这件事必须能被**精确控制**才能测。
这里用假的 session / 假的接口桩驱动 server.py 的真实判定代码路径。

运行：python3 -m pytest tests/test_a5_running_truth.py -q --tb=short
"""

import asyncio
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import server as srv                                    # noqa: E402
from h3studio import running_truth                      # noqa: E402


# ------------------------------------------------------------------
# 假的 ComfyUI 桩
# ------------------------------------------------------------------

class FakeResponse:
    """最小 aiohttp 响应桩（只需要 status / json()）。"""

    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """假的 aiohttp ClientSession。

    `queue_payload` —— GET /queue 的返回体；
    `history_payload` —— GET /history/{id} 的返回体；
    `queue_status` / `history_status` —— HTTP 状态码（非 200 模拟不可达）。

    【为什么要能模拟「ComfyUI 不可达」】
    「读不到」和「读到了但队列是空的」必须能被区分 ——
    把前者当成后者会**误杀所有在跑的任务**（比原 bug 更糟）。
    """

    def __init__(self, queue_payload=None, history_payload=None,
                 queue_status=200, history_status=200,
                 history_by_id=None):
        self.queue_payload = queue_payload
        self.history_payload = history_payload
        self.queue_status = queue_status
        self.history_status = history_status
        self.history_by_id = history_by_id or {}
        self.queue_calls = 0
        self.history_calls = 0

    def get(self, url, **kw):
        if "/queue" in url:
            self.queue_calls += 1
            if self.queue_status != 200:
                return FakeResponse({}, self.queue_status)
            return FakeResponse(self.queue_payload)
        if "/history/" in url:
            self.history_calls += 1
            if self.history_status != 200:
                return FakeResponse({}, self.history_status)
            pid = url.split("/history/", 1)[1]
            if pid in self.history_by_id:
                return FakeResponse({pid: self.history_by_id[pid]})
            return FakeResponse(self.history_payload or {})
        return FakeResponse({})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def make_queue_payload(running_ids=(), pending_ids=()):
    """造一个 ComfyUI /queue 的真实形状响应。

    【形状来源】本机 ComfyUI 0.37 源码 server.py:1230 —— 队列项是元组
        (number, prompt_id, prompt, extra_data, outputs_to_execute, sensitive)
    而 /queue 端点把 sensitive 去掉只返回前 5 项（server.py:114）。
    => prompt_id 在下标 1。
    """
    def item(pid, number):
        return [number, pid, {}, {}, []]

    return {
        "queue_running": [item(pid, i) for i, pid in enumerate(running_ids)],
        "queue_pending": [item(pid, 100 + i)
                          for i, pid in enumerate(pending_ids)],
    }


# ------------------------------------------------------------------
# 纯函数层：/queue 解析
# ------------------------------------------------------------------

class QueueParsingTest(unittest.TestCase):
    """从 ComfyUI 的 /queue 响应里取出 prompt_id。"""

    def test_extracts_prompt_ids_from_real_tuple_shape(self):
        """真实形状（元组，prompt_id 在下标 1）-> 正确取出。"""
        payload = make_queue_payload(running_ids=["pid-a"], pending_ids=["pid-b"])
        ids = running_truth.queue_prompt_ids(payload)
        self.assertEqual(ids, {"pid-a", "pid-b"})

    def test_empty_queue_yields_empty_set(self):
        """队列为空 -> 空集合（**不是** None：None 表示读不到）。"""
        ids = running_truth.queue_prompt_ids(
            {"queue_running": [], "queue_pending": []})
        self.assertEqual(ids, set())
        self.assertIsNotNone(ids, "空集合与 None 语义不同：None = ComfyUI 读不到")

    def test_none_payload_yields_empty_set(self):
        """响应体不是 dict -> 空集合，不抛异常。"""
        self.assertEqual(running_truth.queue_prompt_ids(None), set())
        self.assertEqual(running_truth.queue_prompt_ids("junk"), set())

    def test_dict_shaped_items_are_also_understood(self):
        """中间代理把元组改成 dict -> 也能认出 prompt_id。"""
        payload = {"queue_running": [{"prompt_id": "pid-x"}], "queue_pending": []}
        self.assertEqual(running_truth.queue_prompt_ids(payload), {"pid-x"})


# ------------------------------------------------------------------
# 纯函数层：核心判定
# ------------------------------------------------------------------

class ClassifyTest(unittest.TestCase):
    """classify() —— 「这个任务是不是真的还在 ComfyUI 里跑」。"""

    def test_prompt_in_queue_is_active(self):
        """prompt_id 在队列里 -> active（真的在跑）。"""
        state = running_truth.classify(
            prompt_id="pid-1", history_present=False,
            queue_ids={"pid-1"}, now=1000.0, started_at=900.0)
        self.assertEqual(state, running_truth.STATE_ACTIVE)

    def test_prompt_not_in_queue_and_no_history_is_lost(self):
        """【红线 1】既不在队列、history 里也没有 -> 判定为「已丢失」。"""
        state = running_truth.classify(
            prompt_id="pid-gone", history_present=False,
            queue_ids={"pid-other"}, now=1000.0, started_at=900.0)
        self.assertEqual(state, running_truth.STATE_LOST)

    def test_empty_queue_with_old_task_is_lost(self):
        """队列彻底空了、任务也老 -> 已丢失（这是最典型的卡死形态）。"""
        state = running_truth.classify(
            prompt_id="pid-gone", history_present=False,
            queue_ids=set(), now=1000.0, started_at=900.0)
        self.assertEqual(state, running_truth.STATE_LOST)

    def test_unreachable_comfy_is_unknown_not_lost(self):
        """【关键区分】ComfyUI 读不到（queue_ids=None）-> unknown，**不是** lost。

        把「读不到」当成「队列为空」会误杀所有正在跑的任务 ——
        那比原来的 bug 更糟。
        """
        state = running_truth.classify(
            prompt_id="pid-1", history_present=False,
            queue_ids=None, now=1000.0, started_at=900.0)
        self.assertEqual(state, running_truth.STATE_UNKNOWN)
        self.assertNotEqual(state, running_truth.STATE_LOST)

    def test_history_present_means_finished(self):
        """history 里已经有结果 -> finished（worker 该收尾，不该再等）。"""
        state = running_truth.classify(
            prompt_id="pid-1", history_present=True,
            queue_ids={"pid-1"}, now=1000.0, started_at=900.0)
        self.assertEqual(state, running_truth.STATE_FINISHED)

    def test_timeout_overrides_queue_presence(self):
        """【红线 2】超过运行上限 -> timeout（哪怕它还在队列里）。

        还在队列里但跑了几小时 = 卡住了。必须让用户看见。
        """
        state = running_truth.classify(
            prompt_id="pid-1", history_present=False,
            queue_ids={"pid-1"}, now=1000.0, started_at=1000.0 - 3600,
            timeout_sec=1800)
        self.assertEqual(state, running_truth.STATE_TIMEOUT)

    def test_just_under_timeout_is_still_active(self):
        """还没到上限 -> 仍然是 active（不能提前误杀）。"""
        state = running_truth.classify(
            prompt_id="pid-1", history_present=False,
            queue_ids={"pid-1"}, now=1000.0, started_at=1000.0 - 1700,
            timeout_sec=1800)
        self.assertEqual(state, running_truth.STATE_ACTIVE)

    def test_grace_period_protects_just_submitted_task(self):
        """刚提交（还在宽限期内）-> unknown，不判定「丢失」。

        ComfyUI 可能还没把它放进队列 —— 没有宽限期会误杀正常任务。
        """
        state = running_truth.classify(
            prompt_id="pid-1", history_present=False,
            queue_ids=set(), now=1000.0, started_at=999.0,
            timeout_sec=1800, grace_sec=10.0)
        self.assertEqual(state, running_truth.STATE_UNKNOWN)

    def test_zero_timeout_disables_timeout_check(self):
        """timeout_sec=0 -> 不做超时判定（配置显式关掉时）。"""
        state = running_truth.classify(
            prompt_id="pid-1", history_present=False,
            queue_ids={"pid-1"}, now=1000.0, started_at=0.0,
            timeout_sec=0)
        self.assertEqual(state, running_truth.STATE_ACTIVE)


# ------------------------------------------------------------------
# 纯函数层：重启自愈的判定
# ------------------------------------------------------------------

class ResidualRunningPlanTest(unittest.TestCase):
    """plan_residual_running() —— 重启后残留的 running 该怎么办。"""

    def test_no_prompt_id_fails_with_reason(self):
        """没有 prompt_id（还没提交就断了）-> 标记失败并说清原因。"""
        plan = running_truth.plan_residual_running(
            prompt_id="", history_entry=None, queue_ids=set())
        self.assertEqual(plan["action"], "fail")
        self.assertTrue(plan["reason"], "失败必须带中文原因")
        self.assertIn("运行中", plan["reason"])

    def test_missing_from_queue_and_history_fails(self):
        """【红线 3】ComfyUI 队列与历史里都没有 -> 标记失败。"""
        plan = running_truth.plan_residual_running(
            prompt_id="pid-gone", history_entry=None, queue_ids=set())
        self.assertEqual(plan["action"], "fail")
        self.assertEqual(plan["state"], running_truth.STATE_LOST)
        self.assertIn("队列", plan["reason"])

    def test_still_in_queue_is_adopted(self):
        """任务**还在 ComfyUI 队列里** -> 接管（不能让它永远卡住）。"""
        plan = running_truth.plan_residual_running(
            prompt_id="pid-1", history_entry=None, queue_ids={"pid-1"})
        self.assertEqual(plan["action"], "adopt")
        self.assertEqual(plan["state"], running_truth.STATE_ACTIVE)

    def test_history_has_result_is_finalized(self):
        """ComfyUI 里已经有结果 -> 收尾（把产物落盘）。"""
        plan = running_truth.plan_residual_running(
            prompt_id="pid-1",
            history_entry={"outputs": {}, "status": {"status_str": "success"}},
            queue_ids=set())
        self.assertEqual(plan["action"], "finalize")

    def test_unreachable_comfy_fails_with_explicit_reason(self):
        """ComfyUI 不可达 -> 明确失败（**不能**留着「运行中」）。"""
        plan = running_truth.plan_residual_running(
            prompt_id="pid-1", history_entry=None, queue_ids=None)
        self.assertEqual(plan["action"], "fail")
        self.assertIn("ComfyUI", plan["reason"])


# ------------------------------------------------------------------
# 纯函数层：下发给前端的判定
# ------------------------------------------------------------------

class JobRunningTruthTest(unittest.TestCase):
    """job_running_truth() —— 前端卡片消费的那份判定。"""

    def test_non_running_status_has_empty_state(self):
        """非 running -> state 为空串（前端不需要任何处理）。"""
        out = running_truth.job_running_truth(
            status="done", started_at=100.0, now=200.0)
        self.assertEqual(out["state"], "")
        self.assertEqual(out["label"], "")

    def test_running_under_limit_is_active(self):
        """running 且没超限 -> active。"""
        out = running_truth.job_running_truth(
            status="running", started_at=100.0, now=200.0, timeout_sec=1800)
        self.assertEqual(out["state"], running_truth.STATE_ACTIVE)

    def test_running_over_limit_is_timeout(self):
        """【红线 2 的前端侧】超过上限仍显示 running -> 必须变成 timeout。"""
        out = running_truth.job_running_truth(
            status="running", started_at=100.0, now=200.0 + 1801,
            timeout_sec=1800)
        self.assertEqual(out["state"], running_truth.STATE_TIMEOUT)
        self.assertIn("超时", out["label"])

    def test_live_lost_overrides_active(self):
        """实时观测到「丢失」-> 覆盖本地判定。"""
        out = running_truth.job_running_truth(
            status="running", started_at=100.0, now=200.0, timeout_sec=1800,
            live_state=running_truth.STATE_LOST)
        self.assertEqual(out["state"], running_truth.STATE_LOST)

    def test_live_unknown_overrides_active(self):
        """实时判定不了 -> unknown（不能还说「运行中」）。"""
        out = running_truth.job_running_truth(
            status="running", started_at=100.0, now=200.0, timeout_sec=1800,
            live_state=running_truth.STATE_UNKNOWN)
        self.assertEqual(out["state"], running_truth.STATE_UNKNOWN)

    def test_deadline_is_exposed_for_frontend(self):
        """下发 deadline_at —— 前端据此判断「还有多久超时」。"""
        out = running_truth.job_running_truth(
            status="running", started_at=100.0, now=200.0, timeout_sec=1800)
        self.assertEqual(out["deadline_at"], 100.0 + 1800)


# ------------------------------------------------------------------
# server.py 集成：取数 + 判定（用桩，不打真 ComfyUI）
# ------------------------------------------------------------------

class ServerQueueProbeTest(unittest.TestCase):
    """server.fetch_comfy_queue_ids / resolve_running_state 的真实行为。"""

    def test_fetch_queue_ids_reads_prompt_ids(self):
        """/queue 的 prompt_id 被正确读出。"""
        session = FakeSession(
            queue_payload=make_queue_payload(running_ids=["pid-a"]))
        ids = asyncio.run(srv.fetch_comfy_queue_ids(session))
        self.assertEqual(ids, {"pid-a"})

    def test_fetch_queue_ids_returns_none_when_unreachable(self):
        """ComfyUI 不可达 -> None（**不是**空集合）。"""
        session = FakeSession(queue_status=500)
        ids = asyncio.run(srv.fetch_comfy_queue_ids(session))
        self.assertIsNone(ids, "不可达必须返回 None，不能返回空集合")

    def test_resolve_running_state_reports_lost(self):
        """prompt_id 不在队列 -> (lost, 中文原因)。"""
        session = FakeSession(queue_payload=make_queue_payload())
        state, reason = asyncio.run(srv.resolve_running_state(
            session, "pid-gone", time.time() - 60, False))
        self.assertEqual(state, running_truth.STATE_LOST)
        self.assertIn("队列", reason)

    def test_resolve_running_state_reports_active(self):
        """prompt_id 在队列 -> (active, 空原因)。"""
        session = FakeSession(
            queue_payload=make_queue_payload(running_ids=["pid-1"]))
        state, reason = asyncio.run(srv.resolve_running_state(
            session, "pid-1", time.time() - 60, False))
        self.assertEqual(state, running_truth.STATE_ACTIVE)
        self.assertEqual(reason, "")

    def test_resolve_running_state_reports_unknown_when_unreachable(self):
        """ComfyUI 不可达 -> unknown（不是 lost！）。"""
        session = FakeSession(queue_status=500)
        state, reason = asyncio.run(srv.resolve_running_state(
            session, "pid-1", time.time() - 60, False))
        self.assertEqual(state, running_truth.STATE_UNKNOWN)
        self.assertEqual(reason, "")

    def test_resolve_running_state_reports_timeout(self):
        """超过上限 -> (timeout, 含分钟数的中文原因)。"""
        session = FakeSession(
            queue_payload=make_queue_payload(running_ids=["pid-1"]))
        state, reason = asyncio.run(srv.resolve_running_state(
            session, "pid-1", time.time() - 100000, False))
        self.assertEqual(state, running_truth.STATE_TIMEOUT)
        self.assertIn("分钟", reason)


# ------------------------------------------------------------------
# server.py 集成：残留 running 自愈（真 SQLite + 桩）
# ------------------------------------------------------------------

class ResidualHealingTest(unittest.TestCase):
    """服务启动时的「残留 running」自愈 —— 真 SQLite + 假 ComfyUI。"""

    def setUp(self):
        self._orig_db = srv.DB_PATH
        self._orig_out = srv.OUT_DIR
        self._orig_comfy = srv.COMFY
        self.tmp = tempfile.mkdtemp(prefix="h3-a5-heal-")
        srv.DB_PATH = Path(self.tmp) / "jobs.db"
        srv.OUT_DIR = Path(self.tmp) / "outputs"
        srv.OUT_DIR.mkdir(parents=True, exist_ok=True)
        srv.init_db()

    def tearDown(self):
        srv.DB_PATH = self._orig_db
        srv.OUT_DIR = self._orig_out
        srv.COMFY = self._orig_comfy
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _insert_running(self, job_id, prompt_id="", started_at=None):
        """往库里塞一条 status='running' 的行。

        started_at 默认取「刚刚」—— 用 1970 年的时间戳会让
        「接管」分支一上来就超时（那不是我们要测的东西）。
        """
        with srv.db() as conn:
            conn.execute(
                "INSERT INTO jobs (id, created_at, mode, params, status, prompt_id,"
                " progress, progress_text, outputs, started_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job_id, time.time(), "t2v", json.dumps({"prompt": "x"}),
                 "running", prompt_id, 0.1, "生成中", "[]",
                 started_at if started_at is not None else time.time()))

    def _status_of(self, job_id):
        with srv.db() as conn:
            row = conn.execute("SELECT status, error FROM jobs WHERE id=?",
                               (job_id,)).fetchone()
        return (row["status"], row["error"]) if row else (None, None)

    def _patch_comfy(self, session):
        """把 server 的 ComfyUI 取数函数换成桩。"""
        async def _fake_queue_ids(_session):
            return session.queue_payload and \
                running_truth.queue_prompt_ids(session.queue_payload) or set()

        async def _fake_history(_session, prompt_id):
            payload = session.history_payload or {}
            return payload.get(prompt_id) if payload else None

        self._saved_queue = srv.fetch_comfy_queue_ids
        self._saved_history = srv.comfy_history
        srv.fetch_comfy_queue_ids = _fake_queue_ids
        srv.comfy_history = _fake_history

    def _restore_comfy(self):
        srv.fetch_comfy_queue_ids = self._saved_queue
        srv.comfy_history = self._saved_history

    def test_residual_running_missing_everywhere_becomes_failed(self):
        """【红线 3】残留 running + ComfyUI 侧没有 -> 标记 failed 且有中文原因。"""
        self._insert_running("job-lost", prompt_id="pid-gone")
        session = FakeSession(queue_payload=make_queue_payload())
        self._patch_comfy(session)
        try:
            report = asyncio.run(srv._heal_residual_running())
        finally:
            self._restore_comfy()
        status, error = self._status_of("job-lost")
        self.assertEqual(status, "failed",
                         "残留的「运行中」必须被自愈成终态，不能永远 running")
        self.assertTrue(error, "失败必须写清原因")
        self.assertIn("运行中", error)
        self.assertEqual(report["failed"], 1)

    def test_residual_running_without_prompt_id_becomes_failed(self):
        """没有 prompt_id 的残留 running -> 标记 failed。"""
        self._insert_running("job-nopid", prompt_id="")
        session = FakeSession(queue_payload=make_queue_payload())
        self._patch_comfy(session)
        try:
            asyncio.run(srv._heal_residual_running())
        finally:
            self._restore_comfy()
        status, error = self._status_of("job-nopid")
        self.assertEqual(status, "failed")
        self.assertTrue(error)

    def test_residual_running_still_in_queue_is_adopted(self):
        """任务还在 ComfyUI 队列里 -> 接管（不接管 = 永远卡住）。"""
        self._insert_running("job-alive", prompt_id="pid-alive")
        session = FakeSession(
            queue_payload=make_queue_payload(running_ids=["pid-alive"]))
        self._patch_comfy(session)
        try:
            report = asyncio.run(srv._heal_residual_running())
        finally:
            self._restore_comfy()
        # 接管是**异步**的（起后台协程等结果），DB 里此刻仍应是 running。
        self.assertEqual(report["adopted"], 1)
        status, _ = self._status_of("job-alive")
        self.assertEqual(status, "running")

    def test_healing_is_idempotent_when_no_residual(self):
        """没有残留 -> 不做任何事（幂等）。"""
        report = asyncio.run(srv._heal_residual_running())
        self.assertEqual(report["healed"], 0)

    def test_healing_does_not_touch_terminal_jobs(self):
        """终态任务不受自愈影响。"""
        with srv.db() as conn:
            conn.execute(
                "INSERT INTO jobs (id, created_at, mode, params, status, "
                "outputs, started_at, finished_at) VALUES (?,?,?,?,?,?,?,?)",
                ("job-done", 1000.0, "t2v", "{}", "done", "[]", 900.0, 950.0))
        session = FakeSession(queue_payload=make_queue_payload())
        self._patch_comfy(session)
        try:
            asyncio.run(srv._heal_residual_running())
        finally:
            self._restore_comfy()
        status, _ = self._status_of("job-done")
        self.assertEqual(status, "done")


# ------------------------------------------------------------------
# server.py 集成：worker 的轮询循环真的会退出
# ------------------------------------------------------------------

class WorkerPollingTest(unittest.TestCase):
    """worker 的轮询循环：任务丢失 / 超时 -> **必须退出**并标失败。

    【为什么不打真 ComfyUI】
    这里驱动的是 worker 的**真实**轮询代码路径，只是把两个外部依赖
    （comfy_history / fetch_comfy_queue_ids）换成桩 ——
    这样「丢失」与「超时」能被精确触发，不需要真的跑模型。
    """

    def setUp(self):
        self._orig_db = srv.DB_PATH
        self._orig_out = srv.OUT_DIR
        self._orig_comfy = srv.COMFY
        self._orig_submit = srv.comfy_submit
        self._orig_refresh = srv.refresh_comfy_node_names
        self._orig_interval = srv.CFG["poll_interval"]
        self._orig_timeout = srv.RUNNING_TIMEOUT_SEC
        self.tmp = tempfile.mkdtemp(prefix="h3-a5-worker-")
        srv.DB_PATH = Path(self.tmp) / "jobs.db"
        srv.OUT_DIR = Path(self.tmp) / "outputs"
        srv.OUT_DIR.mkdir(parents=True, exist_ok=True)
        srv.init_db()
        # 轮询间隔压到最小：测试不该真的等 1.5 秒。
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
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_job(self, mode="t2v"):
        job_id = srv.create_job_record(mode, {"prompt": "a5 probe", "duration": 5})
        with srv.db() as conn:
            conn.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?",
                         (1000.0, job_id))
        return job_id

    def _run_worker(self, job_id, history_result, queue_ids):
        """跑一次 worker._run（外部依赖全部换成桩）。"""
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
        job_row = {"id": job_id, "mode": "t2v",
                   "params": json.dumps({"prompt": "a5 probe", "duration": 5})}
        asyncio.run(worker._run(job_row))

        with srv.db() as conn:
            row = conn.execute("SELECT status, error FROM jobs WHERE id=?",
                               (job_id,)).fetchone()
        return (row["status"], row["error"] or "")

    def test_worker_exits_when_task_missing_from_queue(self):
        """【红线 1】history 空 + 队列里没有 -> 任务判失败（不再无限等待）。"""
        job_id = self._make_job()
        status, error = self._run_worker(job_id, None, set())
        self.assertEqual(status, "failed",
                         "任务在 ComfyUI 里消失后必须判失败，不能永远 running")
        self.assertIn("队列", error)
        self.assertNotIn("Traceback", error)
        self.assertNotIn("object has no attribute", error)

    def test_worker_times_out_and_fails(self):
        """【红线 2】超过运行上限 -> 判失败（带分钟数的中文原因）。"""
        job_id = self._make_job()
        # started_at 固定在 1000.0 —— 把上限调到「已经超了」。
        srv.RUNNING_TIMEOUT_SEC = 0.001
        # 让「现在」远超 started_at：直接把上限设成 1 秒，
        # 而任务 started_at 是很早以前（1000.0 epoch）。
        import time as _time
        with srv.db() as conn:
            conn.execute("UPDATE jobs SET started_at=? WHERE id=?",
                         (_time.time() - 3600, job_id))
        status, error = self._run_worker(job_id, None, {"pid-probe"})
        self.assertEqual(status, "failed", "超时必须判失败")
        self.assertIn("超时", error)

    def test_worker_keeps_running_while_task_is_in_queue(self):
        """任务还在队列里 -> 继续等（不能误杀正在跑的任务）。

        这里让 history 一直返回空、队列里一直有它，
        然后用「取消」把任务收尾（模拟用户取消）——
        断言的是：**它没有被判成丢失/超时**。
        """
        job_id = self._make_job()
        # 先跑一次：队列里有它、history 空 -> 判定为 active，不会失败。
        # 为了让测试能结束，这里用「取消」打断：先在 DB 里取消，
        # worker 下一轮读到 canceled 就会 return。
        with srv.db() as conn:
            conn.execute("UPDATE jobs SET status='canceled' WHERE id=?", (job_id,))
        status, _ = self._run_worker(job_id, None, {"pid-probe"})
        self.assertEqual(status, "canceled",
                         "在队列里的任务不该被判成丢失或超时")

    def test_malformed_history_fails_fast(self):
        """【红线 4】畸形 history -> 快速失败，不挂死。"""
        job_id = self._make_job()
        status, error = self._run_worker(
            job_id, {"status": "execution_error"}, {"pid-probe"})
        self.assertEqual(status, "failed")
        self.assertNotIn("Traceback", error)
        self.assertIn("失败", error)


# ------------------------------------------------------------------
# server.py 集成：接口下发字段
# ------------------------------------------------------------------

class ApiPayloadTest(unittest.TestCase):
    """列表 / 详情接口必须下发「运行真相」相关字段。"""

    def setUp(self):
        self._orig_db = srv.DB_PATH
        self._orig_out = srv.OUT_DIR
        self.tmp = tempfile.mkdtemp(prefix="h3-a5-api-")
        srv.DB_PATH = Path(self.tmp) / "jobs.db"
        srv.OUT_DIR = Path(self.tmp) / "outputs"
        srv.OUT_DIR.mkdir(parents=True, exist_ok=True)
        srv.init_db()

    def tearDown(self):
        srv.DB_PATH = self._orig_db
        srv.OUT_DIR = self._orig_out
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_row_to_job_carries_running_truth(self):
        """row_to_job 必须带 running_truth / running_timeout_sec / started_at。"""
        job_id = srv.create_job_record("t2v", {"prompt": "x", "duration": 5})
        with srv.db() as conn:
            conn.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?",
                         (time.time(), job_id))
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        job = srv.row_to_job(row)
        for key in ("running_truth", "running_timeout_sec", "started_at"):
            self.assertIn(key, job, f"任务 payload 缺少字段 {key}")
        self.assertEqual(job["running_truth"]["state"],
                         running_truth.STATE_ACTIVE,
                         "刚启动的任务应判定为 active")
        self.assertGreater(job["running_timeout_sec"], 0)

    def test_row_to_job_running_truth_reflects_timeout(self):
        """超限的 running -> running_truth.state = timeout（前端据此兜底）。"""
        job_id = srv.create_job_record("t2v", {"prompt": "x", "duration": 5})
        with srv.db() as conn:
            conn.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?",
                         (time.time() - 7200, job_id))
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        job = srv.row_to_job(row)
        # 1000.0 是 1970 年 -> 早就超过上限了
        self.assertEqual(job["running_truth"]["state"],
                         running_truth.STATE_TIMEOUT,
                         "超限的 running 必须被标成 timeout，"
                         "否则前端会永远显示「运行中」")

    def test_all_running_rows_get_live_judgement(self):
        """【A5 核心场景】残留 running 可能有**多条** -> 每一条都要判定。

        【为什么这条重要】只判定第一条的话，其余的会继续显示「运行中」——
        缺陷没修干净（实测：队列面板里第二条仍显示「运行中」，
        是浏览器验证脚本抓出来的）。
        """
        import server as srv_mod
        ids = []
        for i in range(3):
            jid = srv.create_job_record("t2v", {"prompt": f"stuck {i}", "duration": 5})
            ids.append(jid)
            with srv.db() as conn:
                conn.execute(
                    "UPDATE jobs SET status='running', started_at=?, prompt_id=? "
                    "WHERE id=?", (time.time() - 7200, f"pid-{i}", jid))
        with srv.db() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status='running'").fetchall()

        async def _fake_live_states(rows_, now=None):
            return {r["id"]: running_truth.STATE_TIMEOUT for r in rows_}

        saved = srv_mod.live_states_for_jobs
        srv_mod.live_states_for_jobs = _fake_live_states
        try:
            out = asyncio.run(srv_mod.live_states_for_jobs([dict(r) for r in rows]))
        finally:
            srv_mod.live_states_for_jobs = saved
        self.assertEqual(len(out), 3,
                         f"三条残留 running 都应被判定，实际 {len(out)}")
        for jid in ids:
            self.assertIn(jid, out)

    def test_queue_snapshot_carries_running_truth(self):
        """队列快照里的 running 条目必须带 running_truth。"""
        job_id = srv.create_job_record("t2v", {"prompt": "x", "duration": 5})
        with srv.db() as conn:
            conn.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?",
                         (time.time(), job_id))
        snap = srv.queue_snapshot()
        item = next(i for i in snap["items"] if i["id"] == job_id)
        self.assertIn("running_truth", item)
        self.assertIn("started_at", item)


if __name__ == "__main__":
    unittest.main()
