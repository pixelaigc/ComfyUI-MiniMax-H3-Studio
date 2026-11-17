#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
E2E 专用的**可控**假 ComfyUI。

【为什么需要它】
真实 ComfyUI（8188）会真跑模型：一次文生视频 1~7 分钟，还要求 GPU 空闲
（本机 4090D 常年被别人占满）。E2E 要稳定复现「界面 -> 后端 -> ComfyUI
-> 结果回到界面」这条链路，就需要一个**能精确控制时序**的 ComfyUI。

【与仓库里 tests/fake_comfyui.py 的差别】
那个只够测「队列顺序」（提交顺序 / 排队）。本文件要支撑**完整链路**，
所以多做了四件事：
  1. **真的产出媒体文件**：/view 返回真实的 PNG / MP4 字节（Studio 会把
     它落盘、再用 PIL/ffprobe 探测尺寸）—— 所以「卡片显示的实际像素」
     这类断言才有意义。
  2. **真的落盘潜空间**：看到工作流里的 H3RelayLatentSave 节点，就在
     <output>/relay_kit/<run_id>/stage_<N>.safetensors 写一个占位文件。
     没有它，服务端的「这一段能不能接续」判定永远是否 —— 接续链路根本
     测不了。
  3. **可注入故障**：
       - ``/control/drop``   —— 把任务从队列里抹掉且永不返回结果，
                                模拟「ComfyUI 那边任务丢了」；
       - ``/control/hold``   —— 让任务一直停在运行中；
       - ``/control/release``—— 让任务立刻完成。
  4. **可回读提交的工作流**：/control/workflow/<prompt_id> 返回原始
     prompt JSON —— 用例据此断言「接续段真的挂了 H3RelayLatentLoad /
     explicit_path」这类工作流级事实。
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

STATE_LOCK = threading.Lock()


class FakeComfy:
    def __init__(self, output_dir: Path, media_dir: Path):
        self.output_dir = output_dir
        self.media_dir = media_dir
        self.counter = 0
        self.polls: dict[str, int] = {}
        self.hold: dict[str, int] = {}      # prompt_id -> 还要轮询几次才完成
        self.dropped: set[str] = set()      # 被「抹掉」的任务
        self.workflows: dict[str, dict] = {}
        self.submitted: list[str] = []
        self.queue_running: list[str] = []
        self.queue_pending: list[str] = []
        self.media_bytes: dict[str, bytes] = {}
        self.load_media()

    # ---- 媒体素材 ----
    def load_media(self) -> None:
        """准备可被 /view 吐出的真实媒体字节。"""
        png = self.media_dir / "out.png"
        mp4 = self.media_dir / "out.mp4"
        if png.is_file():
            self.media_bytes["out.png"] = png.read_bytes()
        if mp4.is_file():
            self.media_bytes["out.mp4"] = mp4.read_bytes()

    def make_png(self, width: int, height: int) -> bytes:
        """按请求尺寸生成真实 PNG 字节（让 Studio 探测到的尺寸 == 请求尺寸）。"""
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new("RGB", (max(1, width), max(1, height)), (90, 140, 200)).save(buf, "PNG")
        return buf.getvalue()

    # ---- 提交 ----
    def next_prompt_id(self) -> str:
        with STATE_LOCK:
            self.counter += 1
            pid = f"e2e-fake-{self.counter:06d}"
            self.polls[pid] = 0
            self.hold.setdefault(pid, 2)
            self.workflows[pid] = {}
            self.submitted.append(pid)
            self.queue_pending.append(pid)
            return pid

    def record_workflow(self, pid: str, workflow: dict) -> None:
        with STATE_LOCK:
            self.workflows[pid] = workflow
            # 写潜空间占位文件（接续链路的必需品）
            self._write_latent(workflow)

    def _write_latent(self, workflow: dict) -> None:
        relay_dir = self.output_dir / "relay_kit"
        for node in (workflow or {}).values():
            if not isinstance(node, dict):
                continue
            if node.get("class_type") != "H3RelayLatentSave":
                continue
            inputs = node.get("inputs") or {}
            run_id = str(inputs.get("run_id") or "").strip()
            try:
                stage = int(inputs.get("stage_index") or 0)
            except (TypeError, ValueError):
                stage = 0
            if not run_id:
                continue
            target = relay_dir / run_id / f"stage_{stage:05d}.safetensors"
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(b"e2e-fake-latent\n" * 64)

    # ---- 轮询 ----
    def poll(self, pid: str) -> int:
        with STATE_LOCK:
            self.polls[pid] = self.polls.get(pid, 0) + 1
            return self.polls[pid]

    def is_done(self, pid: str) -> bool:
        with STATE_LOCK:
            if pid in self.dropped:
                return False
            return self.polls.get(pid, 0) > self.hold.get(pid, 2)

    def output_for(self, pid: str) -> dict:
        """按工作流内容决定产出图片还是视频。"""
        workflow = self.workflows.get(pid) or {}
        classes = {str(n.get("class_type")) for n in workflow.values()
                   if isinstance(n, dict)}
        is_video = bool(classes & {"CreateVideo", "SaveVideo",
                                   "MiniMaxH3ImageToVideo"})
        width = height = 0
        for node in workflow.values():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs") or {}
            if isinstance(inputs, dict):
                width = max(width, int(inputs.get("width") or 0))
                height = max(height, int(inputs.get("height") or 0))
        if is_video:
            return {
                "outputs": {
                    "92": {"images": [{
                        "filename": f"h3studio_e2e_{pid}.mp4",
                        "subfolder": "", "type": "output"}]},
                },
                "status": {"status_str": "success", "messages": []},
            }
        return {
            "outputs": {
                "9": {"images": [{
                    "filename": f"h3studio_e2e_{pid}.png",
                    "subfolder": "", "type": "output"}]},
            },
            "status": {"status_str": "success", "messages": []},
        }

    def queue_snapshot(self) -> dict:
        with STATE_LOCK:
            running = [p for p in self.queue_running if p not in self.dropped]
            pending = [p for p in self.queue_pending if p not in self.dropped]
            return {
                "queue_running": [[0, p, {}, {"client_id": "e2e"}] for p in running],
                "queue_pending": [[0, p, {}, {"client_id": "e2e"}] for p in pending],
            }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: FakeComfy = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):  # 静音
        pass

    # ---- 基础工具 ----
    def _json(self, code: int, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, code: int, body: bytes, ctype: str = "application/octet-stream"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # ---- POST ----
    def do_POST(self):
        path = urlparse(self.path).path
        raw = self._body()

        if path == "/prompt":
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                payload = {}
            workflow = payload.get("prompt") or {}
            pid = self.state.next_prompt_id()
            self.state.record_workflow(pid, workflow)
            with STATE_LOCK:
                if self.state.queue_pending:
                    self.state.queue_pending.remove(pid)
                self.state.queue_running.append(pid)
            self._json(200, {"prompt_id": pid, "number": len(self.state.submitted),
                             "node_errors": []})
            return

        if path.startswith("/control/"):
            self._control(path, raw)
            return

        if path in ("/queue", "/interrupt"):
            self._json(200, {})
            return

        if path == "/upload/image":
            self._json(200, {"name": "e2e-upload.png", "subfolder": "h3studio",
                             "type": "input"})
            return

        self._json(200, {})

    def _control(self, path: str, raw: bytes):
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            payload = {}
        action = path.split("/control/", 1)[1]
        state = self.state
        with STATE_LOCK:
            if action == "release":
                targets = payload.get("prompt_ids") or state.submitted
                for pid in targets:
                    state.hold[pid] = 0
            elif action == "hold":
                targets = payload.get("prompt_ids") or state.submitted
                for pid in targets:
                    state.hold[pid] = int(payload.get("polls", 10 ** 9))
            elif action == "drop":
                targets = payload.get("prompt_ids") or []
                for pid in targets:
                    state.dropped.add(pid)
                    if pid in state.queue_running:
                        state.queue_running.remove(pid)
                    if pid in state.queue_pending:
                        state.queue_pending.remove(pid)
            elif action == "state":
                self._json(200, {
                    "submitted": state.submitted,
                    "running": state.queue_running,
                    "pending": state.queue_pending,
                    "dropped": sorted(state.dropped),
                    "holds": state.hold,
                    "polls": state.polls,
                })
                return
            else:
                self._json(400, {"error": f"未知控制指令 {action}"})
                return
        self._json(200, {"ok": True})

    # ---- GET ----
    def do_GET(self):
        path = urlparse(self.path).path
        state = self.state

        if path == "/queue":
            self._json(200, state.queue_snapshot())
            return

        if path == "/system_stats":
            self._json(200, {"system": {"os": "linux", "comfyui_version": "e2e-fake"},
                             "devices": []})
            return

        if path.startswith("/object_info"):
            name = path.split("/object_info", 1)[1].strip("/")
            if not name:
                self._json(200, {})
                return
            # 假 ComfyUI 假装「什么节点都有」——接续能力探测会认为可用。
            self._json(200, {name: {"input": {"required": {}}}})
            return

        if path.startswith("/control/workflow/"):
            pid = path.rsplit("/", 1)[1]
            self._json(200, state.workflows.get(pid) or {})
            return

        if path.startswith("/control/state"):
            self._json(200, {
                "submitted": state.submitted,
                "running": state.queue_running,
                "pending": state.queue_pending,
                "dropped": sorted(state.dropped),
                "holds": state.hold,
                "polls": state.polls,
            })
            return

        if path.startswith("/history/"):
            pid = path.split("/history/", 1)[1]
            state.poll(pid)
            if not state.is_done(pid):
                self._json(200, {})
                return
            self._json(200, {pid: state.output_for(pid)})
            return

        if path == "/view":
            from urllib.parse import parse_qs
            query = parse_qs(urlparse(self.path).query)
            filename = (query.get("filename") or [""])[0]
            if filename.endswith(".mp4"):
                data = state.media_bytes.get("out.mp4")
                if data is None:
                    data = b""
                self._raw(200, data, "video/mp4")
                return
            # 图片：按工作流里的请求尺寸生成真实 PNG
            width = height = 0
            for pid, workflow in state.workflows.items():
                for node in (workflow or {}).values():
                    if not isinstance(node, dict):
                        continue
                    inputs = node.get("inputs") or {}
                    if isinstance(inputs, dict):
                        width = max(width, int(inputs.get("width") or 0))
                        height = max(height, int(inputs.get("height") or 0))
            data = state.make_png(width or 64, height or 64)
            self._raw(200, data, "image/png")
            return

        self._json(200, {})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8399)
    parser.add_argument("--output-dir", required=True,
                        help="ComfyUI 的 output 目录（潜空间落在这里）")
    parser.add_argument("--media-dir", required=True,
                        help="媒体素材目录（out.png / out.mp4）")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    media_dir = Path(args.media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)

    Handler.state = FakeComfy(output_dir, media_dir)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"e2e fake comfyui on http://127.0.0.1:{args.port} "
          f"(output={output_dir})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
