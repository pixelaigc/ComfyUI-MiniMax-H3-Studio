# -*- coding: utf-8 -*-
"""节点探测**定律**的直接测试：`refresh_comfy_node_names` 算出的
`_COMFY_PROBE_COMPLETE` 必须与「有没有问全」一致。

【为什么必须单独测这一层】
`tests/test_t2v_panel.py` 里的三态断言**都手动把 `_COMFY_PROBE_COMPLETE`
设成 True / False** —— 它们测的是「归一层怎么用这个标志」，
**没有测「这个标志本身算得对不对」**。

后果（实测）：变异脚本
`tests/mutation_check_t2v_node_probe.py` 的 M5
（「`_COMFY_PROBE_COMPLETE` 恒为 False」）**一条测试都没变红** ——
因为没有任何测试碰过那段计算逻辑。

后果的实际危害：标志恒为 False -> 三态退化成「永远放行」 ->
**真缺节点也不再拒绝** -> 面板给出「本机能用」的假控件，
任务建成了、跑到 ComfyUI 才炸。那就是静默失效。

本文件直接对着**真实的探测函数**测标志的取值：
  * 全部节点都问到（HTTP 200 且在）-> 标志 True
  * 全部节点都问到但某个确实不在（HTTP 200 空体）-> 标志 True（问全了！）
  * 部分节点问不到（超时 / 500 / 连不上）-> 标志 False
  * 一个都问不到 -> 标志 False

断言纪律：
  * 期望值是字面量（True / False），不从被测函数反推；
  * 用**可控的假 ComfyUI**（真 HTTP、真超时），不 mock 掉探测函数；
  * 每条断言由「命名该行为的那条断言」抓住。

运行：python3 -m pytest tests/test_node_probe_completeness.py -v
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
    """可控的假 ComfyUI：只回 /object_info/<name>。

    mode:
      * "present" -> 回 {name: {...}}（节点在）
      * "absent"  -> 回 {}（HTTP 200 但没这个节点 -> **确实不在**）
      * "error"   -> 回 500（服务异常 -> **没问到**）
      * "hang"    -> 不回（超时 -> **没问到**）
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


class ProbeCompletenessTest(unittest.TestCase):
    """`_COMFY_PROBE_COMPLETE` 的取值必须反映「有没有问全」。"""

    def setUp(self):
        self._orig_names = srv._COMFY_NODE_NAMES
        self._orig_ts = srv._COMFY_NODES_TS
        self._orig_comfy = srv.COMFY
        self._orig_complete = srv._COMFY_PROBE_COMPLETE
        srv._COMFY_NODE_NAMES = set()
        srv._COMFY_NODES_TS = 0.0

    def tearDown(self):
        srv._COMFY_NODE_NAMES = self._orig_names
        srv._COMFY_NODES_TS = self._orig_ts
        srv.COMFY = self._orig_comfy
        srv._COMFY_PROBE_COMPLETE = self._orig_complete

    def _probe(self, url):
        srv.COMFY = url
        asyncio.run(srv.refresh_comfy_node_names(force=True))
        return srv._COMFY_PROBE_COMPLETE

    def test_all_nodes_answered_marks_probe_complete(self):
        """全部节点都问到（HTTP 200 且在）-> 标志必须 True。"""
        with _FakeComfyServer("present") as fake:
            self.assertTrue(self._probe(fake.url),
                            "全部节点都问到了，标志应为 True")

    def test_node_absent_but_answered_still_marks_probe_complete(self):
        """
        节点**确实不在**但**问到了**（HTTP 200 空体）-> 标志仍必须 True。

        【为什么这条重要】「问到了」与「在不在」是**两件事**。
        问到了 + 不在 -> 可以断言「真缺节点」-> 标志必须 True，
        否则归一层的「拒绝」路径永远走不到（真缺节点也放过）。
        """
        with _FakeComfyServer("absent") as fake:
            self.assertTrue(self._probe(fake.url),
                            "问到了但节点不在，答案可信，标志应为 True")
            self.assertEqual(srv._COMFY_NODE_NAMES, set(),
                             "「确实不在」的节点不应出现在缓存里")

    def test_comfy_error_marks_probe_incomplete(self):
        """ComfyUI 回 500（服务异常 = 没问到）-> 标志必须 False。"""
        with _FakeComfyServer("error") as fake:
            self.assertFalse(self._probe(fake.url),
                             "服务异常时没问到任何节点，标志应为 False")

    def test_comfy_unreachable_marks_probe_incomplete(self):
        """ComfyUI 连不上 -> 标志必须 False。"""
        self.assertFalse(self._probe("http://127.0.0.1:1"),
                         "ComfyUI 不可达时标志应为 False")

    def test_incomplete_probe_must_not_enable_reject_path(self):
        """
        端到端：探测不完整 -> 归一层**不得**报「缺节点」。

        这是本文件的核心断言 —— 它把「标志算错」和「归一层用错标志」
        两类缺陷连起来：标志若恒为 False，归一层就会永远放行。
        """
        srv.COMFY = "http://127.0.0.1:1"      # 一个都问不到
        asyncio.run(srv.refresh_comfy_node_names(force=True))
        self.assertFalse(srv._COMFY_PROBE_COMPLETE)

        out, err = srv.normalize_t2v_params({
            "prompt": "x", "duration": 5,
            "model_key": "fl2va_pruned_int8", "width": 864, "height": 480,
            "steps": 4, "seed": 1, "accel_loras": [],
            "twopass": {"enabled": True}, "attention": "kitchen",
        })
        self.assertEqual(err, "",
                         f"探测不完整时不该报缺节点：{err}")
        self.assertEqual(out.get("attention"), "kitchen")

    def test_complete_probe_with_missing_node_still_rejects(self):
        """
        反向端到端：探测完整 + 节点真不在 -> 归一层**必须**拒绝。

        【为什么这条是 M5 的牙齿】若 `_COMFY_PROBE_COMPLETE` 恒为 False，
        三态退化成「永远放行」，这条会变红。
        """
        with _FakeComfyServer("absent") as fake:
            srv.COMFY = fake.url
            asyncio.run(srv.refresh_comfy_node_names(force=True))
            self.assertTrue(srv._COMFY_PROBE_COMPLETE,
                            "前置条件：探测必须标记为「问全了」")

            out, err = srv.normalize_t2v_params({
                "prompt": "x", "duration": 5,
                "model_key": "fl2va_pruned_int8", "width": 864, "height": 480,
                "steps": 4, "seed": 1, "accel_loras": [],
                "twopass": {"enabled": True}, "attention": "kitchen",
            })
            self.assertTrue(err, "探测完整且节点确实不在时必须拒绝")
            self.assertIn("没有节点", err)


if __name__ == "__main__":
    unittest.main()
