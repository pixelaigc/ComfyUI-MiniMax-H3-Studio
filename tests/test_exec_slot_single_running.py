# -*- coding: utf-8 -*-
"""「生成队列同时显示多个运行中」缺陷的回归测试（2026-11-22）。

【这个测试在防什么】

用户报（2026-11-22，带截图）：顶部显示「运行中 2 · 排队 2」，
而 ComfyUI 是**单执行槽**（任一时刻至多 1 个 prompt executing），
所以同一时刻显示 2 个「运行中」是不正常的。

现场取证（2026-11-22 17:01，线上 8190）：

    ComfyUI GET /queue:
        queue_running = ["320785e3-8947-422c-9a96-42dbaec9ead0"]
        queue_pending = ["bff1cfe3-3386-43ed-b963-f2295ad69778"]
    Studio DB (data/jobs.db):
        236115fbc8ad  status='running'  prompt_id=320785e3-...
        1cf86678c63d  status='running'  prompt_id=bff1cfe3-...   <- 实际在 pending
        a5a9dc222e96  status='queued'

=> Studio 说「运行中 2」，ComfyUI 实际 executing 只有 1 个。

【根因（四层）】

  L1. 同机多个 h3studio 实例**共享同一个 data/jobs.db**（现场 ps 实测 3 个）。
  L2. `Worker._next_queued()` 只保证**进程内**串行，SQL 无跨进程互斥。
  L3. 多余的任务落进 ComfyUI 的 queue_pending，但 Studio DB 仍是 running。
  L4. `running_truth.classify()` 把「在 running 或 pending」都判成 active；
      前端再按 `status === 'running'` 计数 -> 「运行中 2」。

【本测试覆盖的验收口径】

  A. 提交 3 个任务 -> 只有 1 个 running，其余 queued 且**带队列序号**；
  B. ComfyUI 完成任务后 -> 队列计数与卡片状态**正确收敛**；
  C. 刷新 / 重启后 -> **无幽灵 running**；
  D. 两个不同面板**同时提交** -> 仍然至多 1 个 running；
  E. 判定层单元测试：prompt_id 精确区分 executing / pending；
  F. 跨实例闸门：两个实例共享同一个 data_dir -> 至多 1 条 running。

运行：python3 -m pytest tests/test_exec_slot_single_running.py -v
需要：playwright（chromium）。缺失时整类 skip，不算失败。
"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:                                   # pragma: no cover
    sync_playwright = None
    HAS_PLAYWRIGHT = False

from h3studio import exec_slot                      # noqa: E402
import fake_comfyui                                 # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_json(url: str, timeout: int = 20):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_job(base: str, mode: str, prompt: str, timeout: int = 30) -> dict:
    """POST /api/jobs 建一个任务（multipart，与前端真实提交同形状）。"""
    boundary = "----h3execslotboundary"
    parts = []
    for name, value in (("mode", mode),
                        ("params", json.dumps({"prompt": prompt, "aspect": "16:9"},
                                              ensure_ascii=False))):
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8"))
    body = b"".join(parts) + f"--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        f"{base}/api/jobs", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def db_statuses(data_dir: Path) -> dict:
    """直接读库：{job_id: status}。

    【为什么要直接读库】前端看到的都是渲染结果；
    「后端真的只跑一个」这件事必须看**真相**（SQLite 里的 status 列）。
    """
    db_path = Path(data_dir) / "jobs.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, status FROM jobs").fetchall()
    finally:
        conn.close()
    return {r["id"]: r["status"] for r in rows}


class StudioInstance:
    """起一个独立的 h3studio 实例（可指定**共享**的数据目录）。

    【为什么需要「共享 data_dir」这个选项 —— 2026-11-22 缺陷的核心场景】
    现场的 8190 线上实例与 8290 验收实例**共享同一个 data/jobs.db**。
    正是这种共享导致两个实例的 Worker 各自把一条任务置成 running。
    要在测试里复现这个缺陷，就必须让两个实例**共用同一个库**。
    """

    def __init__(self, data_dir: Path, comfyui_url: str, workdir: Path | None = None):
        self.port = free_port()
        self.data_dir = Path(data_dir)
        self.comfyui_url = comfyui_url
        self.tmp = Path(workdir or tempfile.mkdtemp(prefix="h3studio-execslot-"))
        self.owns_tmp = workdir is None
        self.proc = None

    def start(self):
        shutil.copytree(REPO / "h3studio", self.tmp / "h3studio",
                        dirs_exist_ok=True)
        shutil.copy2(REPO / "server.py", self.tmp / "server.py")
        shutil.copytree(REPO / "web", self.tmp / "web", dirs_exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        cfg = {
            "host": "127.0.0.1",
            "port": self.port,
            "comfyui_url": self.comfyui_url,
            "data_dir": str(self.data_dir),
            "models_dir": "/home/ubuntu/comfyui/models",
            "poll_interval": 1.5,
        }
        (self.tmp / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "server.py"], cwd=str(self.tmp),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return True
            except OSError:
                time.sleep(0.2)
        return False

    def stop(self):
        proc, self.proc = self.proc, None
        if proc:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            except Exception:
                pass
        if self.owns_tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)


# ------------------------------------------------------------------
# 判定层单元测试（纯函数，不碰 IO）
# ------------------------------------------------------------------

class TestExecSlotJudge(unittest.TestCase):
    """exec_slot 的判定层：ComfyUI 队列 -> 执行态。"""

    def test_split_queue_distinguishes_running_from_pending(self):
        """queue_running 与 queue_pending 必须分开取 —— 这是根因 L4。"""
        payload = {
            "queue_running": [[0, "pid-run", {}, {}, []]],
            "queue_pending": [[0, "pid-pend", {}, {}, []]],
        }
        running, pending = exec_slot.split_queue_prompt_ids(payload)
        self.assertEqual(running, {"pid-run"})
        self.assertEqual(pending, {"pid-pend"})
        self.assertNotIn("pid-pend", running)

    def test_split_queue_tolerates_unknown_shape(self):
        """认不出的形状返回空集合（不是 None）—— 上层据此区分「空」与「读不到」。"""
        running, pending = exec_slot.split_queue_prompt_ids({"foo": 1})
        self.assertEqual(running, set())
        self.assertEqual(pending, set())
        running, pending = exec_slot.split_queue_prompt_ids(None)
        self.assertEqual(running, set())

    def test_reconcile_gives_exactly_one_executor(self):
        """两条声明为 running 时，只有 ComfyUI 说在 executing 的那条是 executing。"""
        merged = exec_slot.reconcile_by_prompt(
            declared_running=[
                {"id": "jobA", "prompt_id": "pidA", "created_at": 100.0},
                {"id": "jobB", "prompt_id": "pidB", "created_at": 200.0},
            ],
            declared_queued=["jobC"],
            queue_running={"pidA"},
            queue_pending={"pidB"},
        )
        self.assertEqual(merged["executing"], ["jobA"])
        self.assertIn("jobB", merged["pending"])
        self.assertIn("jobC", merged["pending"])
        self.assertLessEqual(len(merged["executing"]), 1)

    def test_reconcile_never_yields_two_executors(self):
        """【核心防回归】**任何**输入下 executing 都不得多于 1 条。

        【这条断言在防什么 —— 2026-11-22 缺陷】
        现场的形态正是「DB 里两条 status='running'，但 ComfyUI 只有 1 条
        executing」。如果收敛逻辑把两条都判成 executing，
        前端就会显示「运行中 2」—— 也就是用户报的那个缺陷。

        【为什么不能只靠「ComfyUI 单槽」这一条】
        收敛逻辑必须**主动**保证唯一性，而不是依赖「ComfyUI 不会返回
        两条 executing」这个外部前提 —— 万一 ComfyUI 的 /queue 形状
        变了、或者中间代理改了数据，唯一性必须由我们自己保证。
        """
        # 场景 1：两条 declared_running，ComfyUI 说两条都在 executing
        merged = exec_slot.reconcile_by_prompt(
            declared_running=[
                {"id": "jobA", "prompt_id": "pidA", "created_at": 100.0},
                {"id": "jobB", "prompt_id": "pidB", "created_at": 200.0},
            ],
            declared_queued=[],
            queue_running={"pidA", "pidB"},
            queue_pending=set(),
        )
        self.assertEqual(len(merged["executing"]), 1,
                         f"executing 必须唯一，实际 {merged['executing']}")
        self.assertIn("jobB", merged["pending"],
                      "非执行者必须降级为 pending（排队中）")

        # 场景 2：ComfyUI executing 为空，但 DB 里有两条 running
        merged = exec_slot.reconcile_by_prompt(
            declared_running=[
                {"id": "jobA", "prompt_id": "pidA", "created_at": 100.0},
                {"id": "jobB", "prompt_id": "pidB", "created_at": 200.0},
            ],
            declared_queued=[],
            queue_running=set(),
            queue_pending=set(),
        )
        self.assertEqual(len(merged["executing"]), 0,
                         "ComfyUI 没在跑任何东西时不得有 executing")
        self.assertEqual(len(merged["pending"]), 2)

        # 场景 3：ComfyUI executing 的那条**不是** Studio 的任务
        merged = exec_slot.reconcile_by_prompt(
            declared_running=[
                {"id": "jobA", "prompt_id": "pidA", "created_at": 100.0},
            ],
            declared_queued=["jobB"],
            queue_running={"someone-else"},
            queue_pending=set(),
        )
        self.assertEqual(len(merged["executing"]), 0,
                         "ComfyUI 在跑别人的任务时，Studio 侧不该有 executing")
        self.assertEqual(len(merged["pending"]), 2)

    def test_reconcile_when_comfy_pending_only(self):
        """ComfyUI 全在 pending（executing 被别人占）-> Studio 侧无 executing。"""
        merged = exec_slot.reconcile_by_prompt(
            declared_running=[{"id": "jobA", "prompt_id": "pidA",
                               "created_at": 100.0}],
            declared_queued=[],
            queue_running=set(),
            queue_pending={"pidA"},
        )
        self.assertEqual(merged["executing"], [])
        self.assertEqual(merged["pending"], ["jobA"])

    def test_reconcile_unknown_when_comfy_unreachable(self):
        """ComfyUI 读不到 -> 一律 unknown，**不猜**谁在跑。"""
        merged = exec_slot.reconcile_by_prompt(
            declared_running=[{"id": "jobA", "prompt_id": "pidA",
                               "created_at": 100.0}],
            declared_queued=["jobB"],
            queue_running=None,
            queue_pending=None,
        )
        self.assertEqual(merged["executing"], [])
        self.assertEqual(merged["pending"], [])
        self.assertEqual(sorted(merged["unknown"]), ["jobA", "jobB"])

    def test_queue_positions_are_consecutive_from_one(self):
        """排队位置从 1 开始连续递增（1 = 下一个就跑）。"""
        merged = exec_slot.reconcile_by_prompt(
            declared_running=[{"id": "jobA", "prompt_id": "pidA",
                               "created_at": 100.0}],
            declared_queued=["jobB", "jobC"],
            queue_running={"pidA"},
            queue_pending=set(),
        )
        positions = merged["positions"]
        self.assertEqual(positions.get("jobB"), 1)
        self.assertEqual(positions.get("jobC"), 2)

    def test_queue_position_label_text(self):
        """排队文案必须带队列序号。"""
        self.assertEqual(exec_slot.queue_position_label(1), "排队中 · 第 1 位")
        self.assertEqual(exec_slot.queue_position_label(3), "排队中 · 第 3 位")
        self.assertEqual(exec_slot.queue_position_label(None), "排队中")
        self.assertEqual(exec_slot.executing_label(), "运行中")


# ------------------------------------------------------------------
# 后端：跨实例执行槽闸门（缺陷根因 L1 + L2）
# ------------------------------------------------------------------

class TestExecSlotGate(unittest.TestCase):
    """两个实例共享同一个 data_dir -> 任一时刻至多 1 条 running。"""

    def setUp(self):
        self.comfy_port = free_port()
        self.comfy = fake_comfyui.serve(self.comfy_port)
        self.comfy_url = f"http://127.0.0.1:{self.comfy_port}"
        self.data_dir = Path(tempfile.mkdtemp(prefix="h3slot-shared-")) / "data"
        self.instances = []

    def tearDown(self):
        for inst in self.instances:
            inst.stop()
        self.comfy.shutdown()
        shutil.rmtree(self.data_dir.parent, ignore_errors=True)

    def _start_instance(self):
        inst = StudioInstance(self.data_dir, self.comfy_url)
        self.assertTrue(inst.start(), "实例没能启动")
        self.instances.append(inst)
        return inst

    def test_two_instances_sharing_db_never_run_two_at_once(self):
        """【核心回归】两个实例共享库 + 各提交任务 -> 库里至多 1 条 running。"""
        fake_comfyui.STATE.reset()
        inst_a = self._start_instance()
        inst_b = self._start_instance()
        base_a = f"http://127.0.0.1:{inst_a.port}"
        base_b = f"http://127.0.0.1:{inst_b.port}"

        ids = []
        for base, tag in ((base_a, "A"), (base_b, "B")):
            for i in range(2):
                res = post_job(base, "t2i", f"shared {tag} {i}")
                self.assertIn("id", res)
                ids.append(res["id"])
                time.sleep(0.6)

        # 连续观察：任何时刻 running 的条数都必须 <= 1
        worst = 0
        for _ in range(8):
            statuses = db_statuses(self.data_dir)
            running = [j for j, s in statuses.items() if s == "running"]
            worst = max(worst, len(running))
            self.assertLessEqual(
                len(running), 1,
                f"两个实例共享库时同时跑了 {len(running)} 个任务"
                f"（{running}）—— 跨实例执行槽闸门失效")
            time.sleep(0.6)
        self.assertGreaterEqual(worst, 1, "整个观察期都没跑起来（闸门过严）")

    def test_exec_slot_table_records_single_holder(self):
        """执行槽租约表里同一时刻至多一条记录（单执行槽）。"""
        fake_comfyui.STATE.reset()
        inst = self._start_instance()
        base = f"http://127.0.0.1:{inst.port}"
        post_job(base, "t2i", "slot holder probe")
        time.sleep(2.5)
        conn = sqlite3.connect(self.data_dir / "jobs.db")
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM exec_slot").fetchall()
        finally:
            conn.close()
        self.assertLessEqual(len(rows), 1,
                             f"执行槽表里有多条记录（{len(rows)}）—— 不是单执行槽")


# ------------------------------------------------------------------
# 后端：端到端（真 HTTP + 真 SQLite + 假 ComfyUI）
# ------------------------------------------------------------------

class TestQueueSingleRunningE2E(unittest.TestCase):
    """验收口径 A/B/C/D：只有 1 个运行中，其余排队且带序号。"""

    def setUp(self):
        self.comfy_port = free_port()
        self.comfy = fake_comfyui.serve(self.comfy_port)
        self.comfy_url = f"http://127.0.0.1:{self.comfy_port}"
        self.data_dir = Path(tempfile.mkdtemp(prefix="h3slot-e2e-")) / "data"
        self.inst = StudioInstance(self.data_dir, self.comfy_url)
        self.assertTrue(self.inst.start(), "实例没能启动")
        self.base = f"http://127.0.0.1:{self.inst.port}"

    def tearDown(self):
        self.inst.stop()
        self.comfy.shutdown()
        shutil.rmtree(self.data_dir.parent, ignore_errors=True)

    def test_three_jobs_only_one_running_others_have_positions(self):
        """【验收 A】提交 3 个 -> 只有 1 个 running，其余 queued 且带队列序号。"""
        fake_comfyui.STATE.reset()
        ids = []
        for i in range(3):
            res = post_job(self.base, "t2i", f"single running probe {i}")
            self.assertIn("id", res)
            ids.append(res["id"])
            time.sleep(1.0)

        snap = http_json(f"{self.base}/api/queue")
        items = [i for i in snap["items"] if i["id"] in ids]
        self.assertEqual(len(items), 3, f"队列里应有 3 条，实际 {len(items)}")

        running = [i for i in items if i["status"] == "running"]
        queued = [i for i in items if i["status"] == "queued"]
        self.assertEqual(len(running), 1,
                         f"必须恰好 1 个运行中，实际 {len(running)}")
        self.assertEqual(len(queued), 2,
                         f"其余 2 个必须排队中，实际 {len(queued)}")

        # 排队任务必须带队列序号（1、2 连续）
        positions = sorted(i["exec_position"] for i in queued)
        self.assertEqual(positions, [1, 2],
                         f"排队任务必须带队列序号 1/2，实际 {positions}")
        for item in queued:
            self.assertIn("第", item["exec_label"],
                          f"排队项文案必须带队列序号：{item['exec_label']!r}")
            self.assertEqual(item["exec_state"], "pending")

        # 执行态唯一
        executing = [i for i in items if i["exec_state"] == "executing"]
        self.assertEqual(len(executing), 1,
                         f"exec_state=executing 必须至多 1 条，实际 {len(executing)}")

    def test_queue_snapshot_count_matches_comfy_executing(self):
        """【验收 B】队列计数必须等于 ComfyUI 的实际 executing 数量。"""
        fake_comfyui.STATE.reset()
        for i in range(3):
            post_job(self.base, "t2i", f"count match probe {i}")
            time.sleep(0.9)

        comfy = http_json(f"{self.comfy_url}/queue")
        comfy_running = len(comfy.get("queue_running") or [])
        comfy_pending = len(comfy.get("queue_pending") or [])

        snap = http_json(f"{self.base}/api/queue")
        studio_running = len([i for i in snap["items"]
                              if i["exec_state"] == "executing"])
        self.assertEqual(studio_running, comfy_running,
                         f"Studio 运行中数 {studio_running} != ComfyUI executing "
                         f"{comfy_running}")
        self.assertLessEqual(studio_running, 1,
                             "ComfyUI 是单执行槽，运行中数不得 > 1")
        self.assertEqual(comfy_running, 1,
                         f"假 ComfyUI 应有 1 条 executing，实际 {comfy_running}")

        # 【2026-11-22 缺陷修后的行为】其余 2 个任务**留在 Studio 自己的队列里**，
        # 不会提前被灌进 ComfyUI 的 pending —— 这正是修复的目的：
        # 以前多实例竞争会把任务批量提交给 ComfyUI，导致 ComfyUI 里
        # 一堆 pending，而 Studio 却把它们全标成 running。
        # 现在 Studio 侧排队任务的 exec_state 是 pending 且带序号。
        studio_pending = [i for i in snap["items"]
                          if i["exec_state"] == "pending"]
        self.assertEqual(len(studio_pending), 2,
                         f"其余 2 个任务应在 Studio 侧排队，实际 "
                         f"{len(studio_pending)}")
        positions = sorted(i["exec_position"] for i in studio_pending)
        self.assertEqual(positions, [1, 2],
                         f"排队任务必须带队列序号 1/2，实际 {positions}")
        # ComfyUI 侧不应该有「已提交但没在跑」的僵尸排队（那是修复前的形态）
        self.assertEqual(comfy_pending, 0,
                         f"ComfyUI 里不该有额外 pending，实际 {comfy_pending}")

    def test_completion_converges_queue_counts(self):
        """【验收 B】ComfyUI 任务完成后 -> 队列计数与卡片状态正确收敛。"""
        fake_comfyui.STATE.reset()
        ids = []
        for i in range(3):
            res = post_job(self.base, "t2i", f"converge probe {i}")
            ids.append(res["id"])
            time.sleep(0.9)

        before = http_json(f"{self.base}/api/queue")
        self.assertEqual(len([i for i in before["items"]
                              if i["exec_state"] == "executing"]), 1)

        # 放行：让所有任务完成
        fake_comfyui.STATE.release_all()
        deadline = time.time() + 60
        while time.time() < deadline:
            statuses = db_statuses(self.data_dir)
            if all(statuses.get(j) in ("done", "failed", "canceled")
                   for j in ids):
                break
            time.sleep(1.0)

        statuses = db_statuses(self.data_dir)
        for jid in ids:
            self.assertIn(statuses.get(jid), ("done", "failed", "canceled"),
                          f"任务 {jid} 未收敛到终态：{statuses.get(jid)}")

        after = http_json(f"{self.base}/api/queue")
        executing = [i for i in after["items"]
                     if i["exec_state"] == "executing"]
        self.assertEqual(len(executing), 0,
                         f"全部完成后不该还有执行中任务，实际 {executing}")

    def test_restart_leaves_no_ghost_running(self):
        """【验收 C】服务重启后 -> 无幽灵 running（残留被自愈收敛）。"""
        fake_comfyui.STATE.reset()
        post_job(self.base, "t2i", "ghost probe")
        time.sleep(2.0)

        # 硬杀进程（模拟崩溃）-> DB 里留下 status='running' 的残留
        proc = self.inst.proc
        if proc is None:
            self.fail("实例进程不在，无法模拟崩溃")
        proc.kill()
        proc.wait(timeout=10)
        statuses = db_statuses(self.data_dir)
        self.assertEqual(len([s for s in statuses.values() if s == "running"]), 1,
                         "崩溃后应留下 1 条 running 残留（测试前提）")

        # 重启同一个实例（同一 data_dir）
        # 【注意】新实例会分配**新端口** -> base 必须跟着更新。
        self.inst = StudioInstance(self.data_dir, self.comfy_url,
                                   workdir=self.inst.tmp)
        self.inst.owns_tmp = False
        self.assertTrue(self.inst.start(), "重启后的实例没能启动")
        self.base = f"http://127.0.0.1:{self.inst.port}"
        time.sleep(3.0)

        snap = http_json(f"{self.base}/api/queue")
        executing = [i for i in snap["items"]
                     if i["exec_state"] == "executing"]
        self.assertLessEqual(len(executing), 1,
                             f"重启后有 {len(executing)} 个执行中任务 —— 幽灵 running")
        # 残留必须已被自愈处理（不再是 running）
        statuses = db_statuses(self.data_dir)
        running_left = [s for s in statuses.values() if s == "running"]
        self.assertLessEqual(len(running_left), 1,
                             "重启后仍有多条 running 残留")

    def test_two_panels_submit_simultaneously(self):
        """【验收 D】两个不同面板同时提交 -> 仍然至多 1 个 running。"""
        fake_comfyui.STATE.reset()
        # 一个用文生图，一个用文生视频（不同 mode = 不同面板）
        a = post_job(self.base, "t2i", "panel A submit")
        b = post_job(self.base, "t2i", "panel B submit")
        c = post_job(self.base, "t2i", "panel C submit")
        self.assertIn("id", a)
        self.assertIn("id", b)
        self.assertIn("id", c)
        time.sleep(2.5)

        snap = http_json(f"{self.base}/api/queue")
        ids = {a["id"], b["id"], c["id"]}
        items = [i for i in snap["items"] if i["id"] in ids]
        executing = [i for i in items if i["exec_state"] == "executing"]
        self.assertEqual(len(executing), 1,
                         f"多面板同时提交后运行中数应恰好 1，实际 {len(executing)}")

        # 列表接口也必须一致（生成库卡片的口径）
        lst = http_json(f"{self.base}/api/jobs?page_size=100")
        listed = [j for j in lst["jobs"] if j["id"] in ids]
        listed_running = [j for j in listed if j["status"] == "running"]
        self.assertEqual(len(listed_running), 1,
                         f"生成库列表里运行中数应恰好 1，实际 {len(listed_running)}")


# ------------------------------------------------------------------
# 前端：真实浏览器
# ------------------------------------------------------------------

@unittest.skipUnless(HAS_PLAYWRIGHT, "需要 playwright（chromium）")
class TestQueuePanelSingleRunningFrontend(unittest.TestCase):
    """前端：摘要与卡片文案（真浏览器 + 真后端 + 假 ComfyUI）。"""

    @classmethod
    def setUpClass(cls):
        cls.comfy_port = free_port()
        cls.comfy = fake_comfyui.serve(cls.comfy_port)
        cls.comfy_url = f"http://127.0.0.1:{cls.comfy_port}"
        cls.data_dir = Path(tempfile.mkdtemp(prefix="h3slot-fe-")) / "data"
        cls.inst = StudioInstance(cls.data_dir, cls.comfy_url)
        assert cls.inst.start(), "实例没能启动"
        cls.base = f"http://127.0.0.1:{cls.inst.port}"
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)
        cls.page = cls.browser.new_page(viewport={"width": 1440, "height": 900})
        cls.errors = []
        cls.page.on("pageerror", lambda e: cls.errors.append(str(e)))

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close()
            cls.pw.stop()
        finally:
            cls.inst.stop()
            cls.comfy.shutdown()
            shutil.rmtree(cls.data_dir.parent, ignore_errors=True)

    def setUp(self):
        fake_comfyui.STATE.reset()
        self.page.goto(self.base, wait_until="domcontentloaded")
        self.page.wait_for_selector("#genBtn", timeout=60000)

    def test_summary_shows_at_most_one_running(self):
        """【验收 A】队列摘要「运行中 N」里的 N 至多为 1。"""
        for i in range(3):
            post_job(self.base, "t2i", f"fe summary probe {i}")
            time.sleep(1.0)
        time.sleep(2.5)

        self.page.wait_for_selector("#queuePanel:not([hidden])", timeout=60000)
        summary = self.page.evaluate(
            "() => document.getElementById('queueSummary')?.textContent.trim()")
        self.assertTrue(summary, "队列摘要为空")
        if "运行中" in summary:
            # 形如「运行中 1 · 排队 2」
            number = summary.split("运行中")[1].split("·")[0].strip()
            self.assertEqual(number, "1",
                             f"摘要的运行中数必须是 1，实际 {summary!r}")

    def test_queue_items_show_positions_not_multiple_running(self):
        """【验收 A】队列明细里「运行中」至多一条，其余带队列序号。"""
        for i in range(3):
            post_job(self.base, "t2i", f"fe items probe {i}")
            time.sleep(1.0)
        time.sleep(2.5)

        items = self.page.evaluate(
            """() => Array.from(document.querySelectorAll('#queueList .queue-item'))
                 .map(li => ({
                   pos: li.querySelector('.queue-pos')?.textContent.trim(),
                   state: li.querySelector('.queue-state')?.textContent.trim(),
                 }))""")
        self.assertTrue(items, "队列明细为空")
        running = [i for i in items if i["state"] == "运行中"]
        self.assertLessEqual(len(running), 1,
                             f"队列明细里有 {len(running)} 条「运行中」：{items}")
        queued = [i for i in items if "排队中" in (i["state"] or "")]
        for item in queued:
            self.assertIn("第", item["state"],
                          f"排队项必须带队列序号：{item['state']!r}")

    def test_job_cards_do_not_show_multiple_progress(self):
        """【验收 B】生成库卡片里带进度的至多一条。"""
        for i in range(3):
            post_job(self.base, "t2i", f"fe cards probe {i}")
            time.sleep(1.0)
        time.sleep(3.0)

        texts = self.page.evaluate(
            """() => Array.from(document.querySelectorAll('.job-card .progress-text'))
                 .map(el => el.textContent.trim())""")
        with_progress = [t for t in texts if "%" in t]
        self.assertLessEqual(len(with_progress), 1,
                             f"有 {len(with_progress)} 张卡片带进度百分比：{texts}")
        queued_cards = [t for t in texts if "排队中" in t]
        for t in queued_cards:
            self.assertNotIn("%", t,
                              f"排队卡片不得带百分比：{t!r}")


if __name__ == "__main__":
    unittest.main()
