#!/usr/bin/env python3
"""
假的 ComfyUI —— 给队列测试用。

【为什么需要假的】
队列的语义是「一次只跑一个、其余排队」。要验证这条语义，必须能
**精确控制**每个任务跑多久、以及什么时候结束。真实 ComfyUI（8188）
会真跑模型，耗时不可控，而且会占 GPU、污染别人的活。

这个假 ComfyUI 实现 Studio 用到的三个端点：
  POST /prompt        -> 立刻返回一个 prompt_id（记录提交顺序）
  GET  /history/{id}  -> 前 N 次轮询返回空（= 还在跑），之后返回产物
  GET  /queue         -> 空的运行/待执行队列
  GET  /system_stats  -> 最小系统信息

【控制完成时机】
每个 prompt 有 `hold_polls` 次轮询后才返回产物 —— 默认很大，
让任务一直停在「运行中」，这样测试可以自由地往队列里塞任务。
需要让某个任务跑完时，调用 `release_all()` 或设置 hold_polls。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


class FakeState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.counter = 0
        self.polls: dict[str, int] = {}
        self.hold_polls = 10 ** 9   # 默认：永远不自动完成
        self.submitted: list[str] = []   # 提交顺序

    def next_prompt_id(self) -> str:
        with self.lock:
            self.counter += 1
            pid = f"fake-{self.counter:06d}"
            self.polls[pid] = 0
            self.submitted.append(pid)
            return pid

    def poll(self, pid: str) -> int:
        with self.lock:
            n = self.polls.get(pid, 0) + 1
            self.polls[pid] = n
            return n

    def release_all(self) -> None:
        """让所有已提交的任务下一次轮询就完成。"""
        with self.lock:
            self.hold_polls = 0

    def reset(self) -> None:
        """恢复到初始状态：清空提交记录、让新任务都停在「运行中」。

        【为什么需要】release_all() 把 hold_polls 设成 0 是**全局**的 ——
        不复位的话，后续用例提交的任务会立刻完成，队列永远排不起来，
        于是「排队中」这类断言会假失败。
        """
        with self.lock:
            self.counter = 0
            self.polls = {}
            self.hold_polls = 10 ** 9
            self.submitted = []

    def is_done(self, pid: str) -> bool:
        with self.lock:
            return self.polls.get(pid, 0) > self.hold_polls

    def queue_snapshot(self) -> dict:
        """按**真实 ComfyUI 的单执行槽语义**返回队列。

        【2026-11-22 缺陷修 —— 为什么必须改这里】
        原来的实现永远返回 `{"queue_running": [], "queue_pending": []}` ——
        也就是「ComfyUI 侧永远没有任何任务在队列里」。

        这**不忠实**于真实 ComfyUI。真实实现（本机 ComfyUI 0.37
        `execution.py:1376` `get_current_queue_volatile` +
        `server.py:1157` `get_queue`）是：

            running = [x for x in self.currently_running.values()]
            queued  = copy.copy(self.queue)
            return (running, queued)

        => **正在执行的那一条在 `queue_running`，其余在 `queue_pending`**，
        且 `queue_running` 恒定**至多 1 条**（单执行槽）。

        【为什么这个忠实度很重要 —— 2026-11-22 缺陷的判定层根因】
        服务端现在按 `/queue` 的两个队列区分「执行中」与「排队中」。
        如果测试替身永远说「队列是空的」，那服务端只能把所有任务
        都判成 pending —— 于是「提交 3 个任务 -> 只有 1 个运行中」
        这条断言根本没法验证（它会看到 0 个运行中）。

        【本替身的语义】
          * 队首（最早提交、且未完成）的那条 -> `queue_running`（executing）
          * 其余未完成的 -> `queue_pending`（排队中）
          * 已完成的 -> 两个队列里都没有（进了 history）
        """
        with self.lock:
            pending = [pid for pid in self.submitted
                       if self.polls.get(pid, 0) <= self.hold_polls]
            running = pending[:1]      # 单执行槽：至多 1 条
            waiting = pending[1:]
            return {
                "queue_running": [[0, pid, {}, {"client_id": "fake"}, []]
                                  for pid in running],
                "queue_pending": [[0, pid, {}, {"client_id": "fake"}, []]
                                  for pid in waiting],
            }


STATE = FakeState()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):        # 静音
        pass

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if path == "/prompt":
            pid = STATE.next_prompt_id()
            self._send(200, {"prompt_id": pid})
            return
        if path in ("/queue", "/interrupt"):
            self._send(200, {})
            return
        self._send(200, {})

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/queue":
            # 【2026-11-22 缺陷修】忠实模拟真实 ComfyUI 的单执行槽语义
            # （executing 在 queue_running，其余在 queue_pending）。
            self._send(200, STATE.queue_snapshot())
            return
        if path == "/system_stats":
            self._send(200, {"system": {"os": "linux"}, "devices": []})
            return
        if path.startswith("/history/"):
            pid = path.split("/history/", 1)[1]
            n = STATE.poll(pid)
            if not STATE.is_done(pid):
                self._send(200, {})
                return
            self._send(200, {pid: {
                "outputs": {"9": {"images": [{"filename": "fake.png",
                                              "subfolder": "",
                                              "type": "output"}]}},
                "status": {"status_str": "success", "messages": []},
            }})
            return
        self._send(200, {})


def serve(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8299
    srv = serve(port)
    print(f"fake comfyui on http://127.0.0.1:{port}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
