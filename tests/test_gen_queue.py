# -*- coding: utf-8 -*-
"""生成队列化（2026-11-13 需求第 4 条）的回归测试。

覆盖五件事：

1. **后端真的排队**（不是前端改名）
   - 连续提交 N 个任务 -> 库里 N 条都是 'queued'；
   - Worker 同一时刻只跑一个（running 数量恒为 1）；
   - 执行顺序 = 提交顺序（FIFO，按 created_at）。

2. **队列位置可见**
   GET /api/queue 返回 items[]，每条带 position（1 = 队首/正在跑），
   并带 pending / total 计数。

3. **前端按钮不阻塞**
   - 提交后生成按钮**不禁用**（disabled=false）—— 用户能继续添加；
   - 按钮文案变成「已入队（队列 N）」而不是「生成中…」；
   - 队列为空时回到「生成」。

4. **队列状态可见**
   队列面板（#queuePanel）里能区分 排队中 / 运行中 / 成功 / 失败，
   并显示执行顺序（position）。

5. **队列任务可取消**
   POST /api/jobs/{id}/cancel 能取消**排队中**（还没开始跑）的任务，
   取消后它离开队列、出现在 recent 里。

【为什么用假 ComfyUI】队列的语义是「一次只跑一个、其余排队」。
要精确验证这条语义，必须能控制每个任务跑多久。真实 ComfyUI 会真跑模型，
耗时不可控、还占 GPU。tests/fake_comfyui.py 让任务停在「运行中」，
这样可以自由地往队列里塞任务，再逐个放行。

测试用独立临时数据目录 + 独立端口，不依赖也不污染真实库。
运行：python3 -m pytest tests/test_gen_queue.py -v
需要：playwright（chromium）。缺失时整类 skip，不算失败。
"""
import json
import os
import socket
import sqlite3
import sys
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

from test_frontend_defaults import StudioServer, WAIT_TIMEOUT  # noqa: E402
import fake_comfyui  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_json(url: str, timeout: int = 20):
    """GET 一个 JSON 接口（失败抛异常，测试里据此判失败）。"""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_job(base: str, mode: str, prompt: str, timeout: int = 30) -> dict:
    """POST /api/jobs 建一个任务（multipart，与前端真实提交同形状）。"""
    boundary = "----h3queueboundary"
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


def post_cancel(base: str, job_id: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(f"{base}/api/jobs/{job_id}/cancel",
                                 data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def db_rows(data_dir: Path) -> list[tuple]:
    """直接读库：返回 [(id, status, mode, created_at), ...]（按创建时间排序）。

    【为什么要直接读库】前端看到的都是渲染结果；「后端真的排队了」
    这件事必须看**真相**（SQLite 里的 status 列），不能靠界面反推。
    """
    db_path = Path(data_dir) / "jobs.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, status, mode, created_at FROM jobs ORDER BY created_at ASC"
        ).fetchall()
    finally:
        conn.close()
    return [(r["id"], r["status"], r["mode"], r["created_at"]) for r in rows]


# ------------------------------------------------------------------ 后端队列


class TestQueueBackend(unittest.TestCase):
    """后端：真排队 + FIFO + 可取消（真 HTTP + 真 SQLite + 假 ComfyUI）。"""

    @classmethod
    def setUpClass(cls):
        cls.comfy_port = free_port()
        cls.comfy = fake_comfyui.serve(cls.comfy_port)
        cls.comfy_url = f"http://127.0.0.1:{cls.comfy_port}"
        cls.srv = StudioServer(comfyui_url=cls.comfy_url)
        cls.srv.start()
        cls.base = f"http://127.0.0.1:{cls.srv.port}"

    @classmethod
    def tearDownClass(cls):
        try:
            cls.srv.stop()
        finally:
            cls.comfy.shutdown()

    def setUp(self):
        # 每个用例从「队列全空 + 假 ComfyUI 复位」开始
        # 【为什么复位】release_all() 是全局的 —— 不复位的话新任务会立刻
        # 完成，队列排不起来，「排队中」这类断言会假失败。
        fake_comfyui.STATE.reset()
        for row in db_rows(self.srv.data_dir):
            try:
                post_cancel(self.base, row[0])
            except Exception:
                pass
        time.sleep(1.5)

    def test_multiple_submissions_all_queued_not_blocked(self):
        """连续提交 3 个任务 -> 库里 3 条都是 queued（没有「阻塞式」这回事）。"""
        ids = []
        for i in range(3):
            res = post_job(self.base, "t2i", f"queue backend probe {i}")
            self.assertIn("id", res, f"提交没返回任务 id：{res}")
            self.assertEqual(res.get("status"), "queued",
                             f"建任务时状态就该是 queued：{res}")
            ids.append(res["id"])
            # 间隔要 > 前端的 800ms 连点节流阈值，否则真实前端会拒收第 2 个
            time.sleep(1.0)

        rows = db_rows(self.srv.data_dir)
        self.assertGreaterEqual(len(rows), 3, "库里应有至少 3 条任务")
        by_id = {r[0]: r for r in rows}
        for jid in ids:
            self.assertIn(jid, by_id, f"任务 {jid} 没落库")
            status = by_id[jid][1]
            # 队首那条会被 Worker 取走变成 running —— 其余必须还在排队
            self.assertIn(status, ("queued", "running"),
                          f"任务 {jid} 状态异常：{status}")
        not_started = [jid for jid in ids if by_id[jid][1] == "queued"]
        self.assertGreaterEqual(len(not_started), 2,
                                "后两个任务应仍在排队（证明没有阻塞式提交）")

        # 队列视图里应该能按顺序看到这 3 条
        snap = http_json(f"{self.base}/api/queue")
        queued_items = [i for i in snap["items"] if i["id"] in ids]
        self.assertEqual(len(queued_items), 3,
                         f"队列视图应含 3 条，实际 {len(queued_items)}")
        self.assertEqual([i["id"] for i in queued_items], ids,
                         "队列顺序必须等于提交顺序（FIFO）")
        positions = [i["position"] for i in queued_items]
        self.assertEqual(positions, list(range(positions[0], positions[0] + 3)),
                         f"position 必须连续递增，实际 {positions}")

    def test_queue_snapshot_reports_counts_and_labels(self):
        """快照要有 pending / total 计数，以及每条的类型标签与提示词。"""
        post_job(self.base, "t2i", "queue snapshot probe")
        time.sleep(0.6)
        snap = http_json(f"{self.base}/api/queue")
        for key in ("running", "pending", "total", "items", "recent"):
            self.assertIn(key, snap, f"快照缺少字段 {key}")
        self.assertGreaterEqual(snap["total"], 1)
        self.assertGreaterEqual(snap["pending"], 0)
        first = snap["items"][0]
        for key in ("id", "mode", "status", "position", "label", "prompt"):
            self.assertIn(key, first, f"队列条目缺少字段 {key}")
        self.assertTrue(first["label"], "队列条目必须有可读的类型标签")

    def test_queued_job_can_be_canceled(self):
        """排队中的任务可以被取消（复用既有 /cancel 能力）。"""
        # 先占住 worker：提交一个任务让它跑起来（假 ComfyUI 不会让它结束）
        blocker = post_job(self.base, "t2i", "queue cancel blocker")
        time.sleep(1.2)
        victim = post_job(self.base, "t2i", "queue cancel victim")
        time.sleep(1.2)

        snap = http_json(f"{self.base}/api/queue")
        victim_state = next((i for i in snap["items"] if i["id"] == victim["id"]),
                            None)
        self.assertIsNotNone(victim_state, "被取消的任务应先出现在队列里")
        self.assertEqual(victim_state["status"], "queued",
                         "第二个任务应还在排队（第一个占着 worker）")

        res = post_cancel(self.base, victim["id"])
        self.assertEqual(res.get("status"), "canceled",
                         f"取消排队任务应返回 canceled，实际 {res}")

        time.sleep(1.2)
        snap2 = http_json(f"{self.base}/api/queue")
        in_queue = [i for i in snap2["items"] if i["id"] == victim["id"]]
        self.assertEqual(in_queue, [], "取消后的任务应离开队列")
        in_recent = [i for i in snap2["recent"] if i["id"] == victim["id"]]
        self.assertTrue(in_recent, "取消后的任务应出现在 recent 里")

        # 库里状态也必须是 canceled
        by_id = {r[0]: r for r in db_rows(self.srv.data_dir)}
        self.assertEqual(by_id[victim["id"]][1], "canceled",
                         "库里状态应变成 canceled")

    def test_only_one_job_runs_at_a_time(self):
        """串行执行：任何时刻 running 的任务数 <= 1。"""
        for i in range(3):
            post_job(self.base, "t2i", f"serial probe {i}")
            time.sleep(1.0)
        for _ in range(5):
            snap = http_json(f"{self.base}/api/queue")
            running = [i for i in snap["items"] if i["status"] == "running"]
            self.assertLessEqual(len(running), 1,
                                 f"同一时刻跑了 {len(running)} 个任务，违反串行语义")
            time.sleep(0.4)

    def test_finished_job_moves_to_recent_with_success_state(self):
        """跑完的任务进 recent，状态是「成功」（done）。"""
        job = post_job(self.base, "t2i", "recent probe")
        # 放行假 ComfyUI：让队首任务完成
        fake_comfyui.STATE.release_all()
        deadline = time.time() + 40
        status = None
        while time.time() < deadline:
            time.sleep(1.0)
            rows = {r[0]: r[1] for r in db_rows(self.srv.data_dir)}
            status = rows.get(job["id"])
            if status in ("done", "failed"):
                break
        self.assertIn(status, ("done", "failed"),
                      f"任务没能跑到终态，实际 {status}")
        snap = http_json(f"{self.base}/api/queue")
        in_recent = [i for i in snap["recent"] if i["id"] == job["id"]]
        self.assertTrue(in_recent, "完成的任务应出现在 recent 里")
        self.assertIn(in_recent[0]["status"], ("done", "failed"))
        # 假 ComfyUI 返回产物 -> 应该是成功
        self.assertEqual(in_recent[0]["status"], "done",
                         f"任务应成功完成，实际 {in_recent[0]}")


# ------------------------------------------------------------------ 前端队列


@unittest.skipUnless(HAS_PLAYWRIGHT, "需要 playwright（chromium）")
class TestQueueFrontend(unittest.TestCase):
    """前端：按钮不阻塞 + 队列面板可见（真浏览器 + 真后端 + 假 ComfyUI）。"""

    @classmethod
    def setUpClass(cls):
        cls.comfy_port = free_port()
        cls.comfy = fake_comfyui.serve(cls.comfy_port)
        cls.comfy_url = f"http://127.0.0.1:{cls.comfy_port}"
        cls.srv = StudioServer(comfyui_url=cls.comfy_url)
        cls.srv.start()
        cls.base = f"http://127.0.0.1:{cls.srv.port}"
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
            cls.srv.stop()
            cls.comfy.shutdown()

    def setUp(self):
        fake_comfyui.STATE.reset()
        for row in db_rows(self.srv.data_dir):
            try:
                post_cancel(self.base, row[0])
            except Exception:
                pass
        time.sleep(1.0)
        self.page.goto(self.base, wait_until="domcontentloaded")
        self.page.wait_for_selector("#genBtn", timeout=WAIT_TIMEOUT)

    def _gen_btn_state(self):
        return self.page.evaluate(
            """() => {
              const b = document.getElementById('genBtn');
              if (!b) return null;
              return {text: b.textContent.trim(), disabled: b.disabled,
                      queued: b.classList.contains('queued'),
                      busy: b.classList.contains('busy')};
            }""")

    def test_gen_button_is_not_disabled_when_queue_has_jobs(self):
        """核心要求：队列有任务时，生成按钮**仍然可点**（用户能继续添加）。"""
        post_job(self.base, "t2i", "frontend queue probe")
        time.sleep(2.5)

        state = self._gen_btn_state()
        self.assertIsNotNone(state, "找不到生成按钮")
        self.assertFalse(state["disabled"],
                         "队列非空时生成按钮仍被禁用 —— 用户无法继续添加任务")
        self.assertNotEqual(state["text"], "生成中…",
                            "按钮不该再是阻塞式的「生成中…」")
        self.assertIn("已入队", state["text"],
                      f"按钮文案应为「已入队（队列 N）」，实际 {state['text']!r}")
        self.assertTrue(state["queued"], "按钮应带 queued 样式类")
        self.assertFalse(state["busy"], "按钮不应再是 busy（阻塞）态")

    def test_queue_panel_shows_order_and_states(self):
        """队列面板：显示顺序 + 排队中/运行中 状态。"""
        for i in range(2):
            post_job(self.base, "t2i", f"panel probe {i}")
            time.sleep(1.0)
        time.sleep(2.5)

        self.page.wait_for_selector("#queuePanel:not([hidden])", timeout=WAIT_TIMEOUT)
        items = self.page.evaluate(
            """() => Array.from(document.querySelectorAll('#queueList .queue-item'))
                 .map(li => ({
                   pos: li.querySelector('.queue-pos')?.textContent.trim(),
                   label: li.querySelector('.queue-label')?.textContent.trim(),
                   state: li.querySelector('.queue-state')?.textContent.trim(),
                   prompt: li.querySelector('.queue-prompt')?.textContent.trim(),
                 }))""")
        self.assertTrue(items, "队列面板应至少有一条记录")
        states = {i["state"] for i in items}
        self.assertTrue(states & {"排队中", "运行中"},
                        f"队列条目应显示排队中/运行中，实际 {states}")
        positions = [i["pos"] for i in items if i["pos"].isdigit()]
        self.assertTrue(positions, "队列条目应显示顺序号")
        self.assertEqual(positions, sorted(positions, key=int),
                         f"队列顺序号应递增，实际 {positions}")

    def test_cancel_button_in_queue_panel(self):
        """队列面板里每条待处理任务都有「取消」按钮。"""
        post_job(self.base, "t2i", "panel cancel probe")
        time.sleep(2.5)
        self.page.wait_for_selector("#queuePanel:not([hidden])", timeout=WAIT_TIMEOUT)
        count = self.page.evaluate(
            "() => document.querySelectorAll('#queueList .queue-cancel').length")
        self.assertGreaterEqual(count, 1, "队列条目应有取消按钮")

    def test_button_returns_to_plain_when_queue_empty(self):
        """队列清空后按钮回到「生成」且仍可点。"""
        snap = http_json(f"{self.base}/api/queue")
        for item in list(snap["items"]):
            try:
                post_cancel(self.base, item["id"])
            except Exception:
                pass
        time.sleep(2.5)
        state = self._gen_btn_state()
        self.assertIsNotNone(state)
        self.assertFalse(state["disabled"], "队列空时按钮不该禁用")
        self.assertEqual(state["text"], "生成",
                         f"队列空时按钮应回到「生成」，实际 {state['text']!r}")

    def test_second_submit_is_accepted_while_first_is_running(self):
        """端到端：第一个任务还在跑时，第二个提交**仍然成功**（不被拒）。"""
        first = post_job(self.base, "t2i", "double submit first")
        time.sleep(1.0)
        second = post_job(self.base, "t2i", "double submit second")
        time.sleep(1.0)
        self.assertIn("id", second, "第二个提交应成功建任务")
        snap = http_json(f"{self.base}/api/queue")
        ids = [i["id"] for i in snap["items"]]
        self.assertIn(first["id"], ids)
        self.assertIn(second["id"], ids, "第二个任务应进同一个队列")
        self.assertLess(ids.index(first["id"]), ids.index(second["id"]),
                        "先提交的应排在前面")


if __name__ == "__main__":
    unittest.main()
