# -*- coding: utf-8 -*-
"""接续能力探测：**「没问到」不等于「节点缺失」。

【缺陷现象】
全量回归期间（并行跑真实 GPU 生成），偶发失败：
    GET /api/continuation/state -> available=false
    reason = 「本机 ComfyUI 里缺少接续所需的节点：H3RelayLatentSave、…」
明明 Relay-Kit **装着**（节点都能在 /object_info 里查到）。

【根因】
ComfyUI 被生成任务占满时，`/object_info/<节点>` 会**超时**。
探测代码把「超时（没问到）」和「HTTP 200 但响应里没这个节点（确实不在）」
都返回 `None` —— 两者被混为一谈。

于是「ComfyUI 忙」-> 探测拿到空集 -> 断言「缺节点」-> **整个接续功能
在用户眼前消失**（按钮不出现、功能被判不可用）。
而单独跑同一条测试时 ComfyUI 空闲 -> 探测成功 -> 通过。
这就是「全量跑失败、单独跑通过」的来源。

【修法】
  * `_probe` 改成**三态**：`name`（在）/ `None`（确实不在）/ `"TIMEOUT"`（没问到）；
  * 探测**不完整**（没问到任何节点）时 -> `available=true`（不阻断功能）
    + `probe_complete=false` + 说明「状态未探全」；
  * 只有**确实问到**且响应里没有该节点，才断言「缺节点」。

【断言纪律】
  * 期望值是字面量，不从被测函数反推；
  * 每条断言由「命名该行为的那条断言」抓住。

运行：python3 -m pytest tests/test_continuation_capability.py -v
"""

import asyncio
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import server as srv                                    # noqa: E402


class _FakeComfy(BaseHTTPRequestHandler):
    """一个「可控」的假 ComfyUI —— 只回 /object_info/<name>。

    mode:
      * "present"  -> 回 {name: {...}}（节点在）
      * "absent"   -> 回 {}（HTTP 200 但没这个节点 -> **确实不在**）
      * "error"    -> 回 500（服务异常 -> **没问到**）
      * "hang"     -> 不回（超时 -> **没问到**）
    """

    mode = "present"

    def do_GET(self):
        if self.mode == "hang":
            import time
            time.sleep(30)
            return
        if self.mode == "error":
            self.send_response(500)
            self.end_headers()
            return
        name = self.path.rsplit("/", 1)[-1]
        body = {} if self.mode == "absent" else {name: {"input": {}}}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


class _FakeComfyServer:
    """起一个假 ComfyUI（后台线程），用完关掉。"""

    def __init__(self, mode):
        _FakeComfy.mode = mode
        self.httpd = HTTPServer(("127.0.0.1", 0), _FakeComfy)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"


class CapabilityProbeTest(unittest.TestCase):
    """能力探测：超时不得被当成「节点缺失」。"""

    def setUp(self):
        self._orig_names = srv._COMFY_NODE_NAMES
        self._orig_ts = srv._COMFY_NODES_TS
        self._orig_comfy = srv.COMFY
        srv._COMFY_NODE_NAMES = set()
        srv._COMFY_NODES_TS = 0.0

    def tearDown(self):
        srv._COMFY_NODE_NAMES = self._orig_names
        srv._COMFY_NODES_TS = self._orig_ts
        srv.COMFY = self._orig_comfy

    def _call_state(self):
        """直接调 api_continuation_state 的返回体（绕开 HTTP）。"""
        async def _run():
            class _Req:
                pass
            resp = await srv.api_continuation_state(_Req())
            return json.loads(resp.text)
        return asyncio.run(_run())

    def test_comfy_unreachable_still_reports_available(self):
        """ComfyUI 完全问不到 -> **不得**报「缺节点」，功能不该消失。"""
        # 指向一个不存在的端口 -> 全部探测失败（没问到）。
        srv.COMFY = "http://127.0.0.1:1"
        body = self._call_state()

        self.assertTrue(body["available"],
                        "ComfyUI 问不到时不应断言「缺节点」—— "
                        f"整个接续功能会在用户眼前消失: {body}")
        self.assertFalse(body["probe_complete"],
                         "探测没问全时 probe_complete 应为 false")
        self.assertIn("未探全", body["reason"])

    def test_comfy_returns_500_still_reports_available(self):
        """ComfyUI 回 500（服务异常）-> 同样算「没问到」，不得报缺节点。"""
        with _FakeComfyServer("error") as fake:
            srv.COMFY = fake.url
            body = self._call_state()
        self.assertTrue(body["available"],
                        f"服务异常时不应断言「缺节点」: {body}")
        self.assertFalse(body["probe_complete"])

    def test_probe_timeout_does_not_overwrite_good_cache(self):
        """探测不完整时，**不能**用残缺结果覆盖上一次的好结果。"""
        # 先放一份「好」的缓存（假装上次探测成功过）。
        srv._COMFY_NODE_NAMES = {"H3RelayLatentSave", "H3RelayLatentLoad",
                                 "H3RelayCopyBridge", "H3RelayTrimAV"}
        srv.COMFY = "http://127.0.0.1:1"   # 全部问不到
        asyncio.run(srv.refresh_comfy_node_names(force=True))

        self.assertEqual(len(srv._COMFY_NODE_NAMES), 4,
                         "探测全失败却把好缓存清空了 —— "
                         "下一次请求就会误判「缺节点」")

    def test_node_actually_missing_is_still_reported(self):
        """节点**确实**不在时，仍必须报「缺节点」（不能把真缺也放过）。

        假 ComfyUI 回 HTTP 200 但响应里没有这个节点 -> 「问到了，
        确实不在」-> 必须报不可用。
        """
        with _FakeComfyServer("absent") as fake:
            srv.COMFY = fake.url
            body = self._call_state()
        self.assertFalse(body["available"],
                         "节点确实缺失时必须报不可用")
        self.assertTrue(body["probe_complete"],
                        "问到了（HTTP 200）-> 探测是完整的")
        self.assertIn("缺少接续所需的节点", body["reason"])
        self.assertIn("H3RelayLatentSave", body["reason"])

    def test_all_nodes_present_reports_available(self):
        """节点齐全 -> available=true + probe_complete=true。"""
        with _FakeComfyServer("present") as fake:
            srv.COMFY = fake.url
            body = self._call_state()
        self.assertTrue(body["available"], body)
        self.assertTrue(body["probe_complete"])
        self.assertEqual(body["reason"], "")


if __name__ == "__main__":
    unittest.main()
