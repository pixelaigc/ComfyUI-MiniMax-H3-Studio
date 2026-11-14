# -*- coding: utf-8 -*-
"""A5 前端兜底：队列面板必须显示「已超时 / 状态未知 / 已丢失」而不是永远「运行中」。

【为什么用真实 JS 执行而不是字符串匹配源码】
字符串匹配只能证明「源码里出现过某个词」，不能证明「逻辑对」。
这里用 node 真正跑一遍 web/generation-queue.js 里的 truthOf()，
断言它在各种服务端下发的字段组合下给出正确的状态与文案。

【为什么不改 app.js】
app.js 是主线 agent 在改的文件（避免冲突）。
队列面板（web/generation-queue.js）是本分支负责的兜底位置；
卡片渲染需要主线补的那一行，在最终回复里说明。

运行：python3 -m pytest tests/test_a5_running_truth_frontend.py -q --tb=short
需要：node（缺失时整类 skip，不算失败）
"""

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

NODE = shutil.which("node")


def run_node(script: str) -> dict:
    """跑一段 node 脚本，返回它打印的最后一个 JSON。"""
    with tempfile.TemporaryDirectory(prefix="h3-a5-node-") as tmp:
        path = Path(tmp) / "probe.js"
        path.write_text(script, encoding="utf-8")
        proc = subprocess.run([NODE, str(path)], capture_output=True,
                              text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"node 执行失败：\n{proc.stderr}\n--- stdout ---\n{proc.stdout}")
    lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    return json.loads(lines[-1])


#: 把 web/generation-queue.js 里的 truthOf 抠出来单独跑。
#: 【为什么要抠出来】那个文件是个 IIFE，直接 require 会去碰 document。
#: 我们只需要它**纯逻辑**的部分（truthOf / timeoutSecOf）——
#: 用正则把它们抠出来在一个假 window 上执行，
#: 断言的是**同一份源码**，不是复制品。
EXTRACT_SCRIPT = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf-8');

function grab(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  if (start < 0) throw new Error('找不到标记: ' + startMarker);
  const end = src.indexOf(endMarker, start);
  if (end < 0) throw new Error('找不到结束标记: ' + endMarker);
  return src.slice(start, end);
}

// 抠出 timeoutSecOf + truthOf（两段相邻的函数定义）
const block = grab('function timeoutSecOf(job)', 'function itemHtml(');
const fns = block + '\nmodule.exports = {timeoutSecOf, truthOf};';

const mod = {exports: {}};
new Function('module', 'exports', fns)(mod, mod.exports);
const {truthOf} = mod.exports;

const cases = JSON.parse(process.argv[3]);
const out = cases.map(c => truthOf(c));
process.stdout.write(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "需要 node")
class QueuePanelFallbackTest(unittest.TestCase):
    """队列面板的「运行真相」兜底（真实 JS 执行）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="h3-a5-fe-"))
        cls.script = cls.tmp / "probe.js"
        cls.script.write_text(EXTRACT_SCRIPT, encoding="utf-8")
        cls.queue_js = REPO / "web" / "generation-queue.js"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _truth(self, cases):
        proc = subprocess.run(
            [NODE, str(self.script), str(self.queue_js), json.dumps(cases)],
            capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise AssertionError(f"node 执行失败：\n{proc.stderr}")
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_active_state_shows_running(self):
        """服务端说「真的在队列里」-> 显示「运行中」。"""
        out = self._truth([{
            "status": "running",
            "running_truth": {"state": "active", "timeout_sec": 1800},
        }])
        self.assertEqual(out[0]["state"], "running")
        self.assertEqual(out[0]["text"], "运行中")

    def test_timeout_state_shows_timeout_not_running(self):
        """【红线】超限 -> 显示「已超时」，**不能**显示「运行中」。"""
        out = self._truth([{
            "status": "running",
            "running_truth": {"state": "timeout", "timeout_sec": 1800},
        }])
        self.assertEqual(out[0]["state"], "timeout")
        self.assertEqual(out[0]["text"], "已超时")
        self.assertNotEqual(out[0]["text"], "运行中")

    def test_unknown_state_shows_unknown(self):
        """判定不了 -> 「状态未知」（不是「运行中」）。"""
        out = self._truth([{
            "status": "running",
            "running_truth": {"state": "unknown", "timeout_sec": 1800},
        }])
        self.assertEqual(out[0]["state"], "unknown")
        self.assertEqual(out[0]["text"], "状态未知")

    def test_lost_state_shows_lost(self):
        """ComfyUI 里已无此任务 -> 「已丢失」。"""
        out = self._truth([{
            "status": "running",
            "running_truth": {"state": "lost", "timeout_sec": 1800},
        }])
        self.assertEqual(out[0]["state"], "lost")
        self.assertEqual(out[0]["text"], "已丢失")

    def test_local_fallback_detects_timeout_without_running_truth(self):
        """老服务端（没有 running_truth）-> 用 started_at + 上限做兜底判定。

        这是「前端不要自己瞎猜」的**最小**兜底：
        阈值来自服务端下发的 running_timeout_sec，不是前端写死的。
        """
        # 用**真实**当前时间 —— 前端的兜底判定用的是 Date.now()，
        # 拿一个写死的 epoch 去比会永远判成「已超时」。
        now = time.time()
        out = self._truth([
            # 超过上限 -> 已超时
            {"status": "running", "started_at": now - 3600,
             "running_timeout_sec": 1800},
            # 没超过 -> 运行中
            {"status": "running", "started_at": now - 60,
             "running_timeout_sec": 1800},
        ])
        self.assertEqual(out[0]["state"], "timeout")
        self.assertEqual(out[0]["text"], "已超时")
        self.assertEqual(out[1]["state"], "running")
        self.assertEqual(out[1]["text"], "运行中")

    def test_no_threshold_available_keeps_running(self):
        """既没有 running_truth 也没有上限 -> 保持「运行中」（不瞎猜）。"""
        out = self._truth([{"status": "running"}])
        self.assertEqual(out[0]["state"], "running")
        self.assertEqual(out[0]["text"], "运行中")

    def test_terminal_status_is_untouched(self):
        """终态任务不受影响。"""
        out = self._truth([{"status": "done"}, {"status": "failed"}])
        self.assertEqual(out[0]["state"], "done")
        self.assertEqual(out[1]["state"], "failed")


if __name__ == "__main__":
    unittest.main()
