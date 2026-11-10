# -*- coding: utf-8 -*-
"""测试基座的进程泄漏防护（2026-11-10 全量回归踩坑后加）。

【问题】
`StudioServer.start()` 起一个真实的 server.py 子进程。
调用方通常在 `tearDownClass` 里 `server.stop()`，
但 **setUpClass 失败时 tearDownClass 不会执行** —— 那个 server 进程就泄漏了。

实测后果：一次全量 pytest 会泄漏几十上百个 `/tmp/h3studio-ui-*` 进程，
每个占一个端口 + 一份内存，把后续所有浏览器用例拖成「端口耗尽」的假失败
（本轮实测：168 个 error，全是这类假失败，与产品代码无关）。

【契约】
1. `stop()` 可重复调用（atexit + weakref.finalize + tearDownClass 会各调一次）；
2. `stop()` 之后子进程确实退出、临时目录确实被删；
3. `start()` 注册了兜底清理（atexit + weakref.finalize）。

运行：python3 -m pytest tests/test_studio_server_cleanup.py -v
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))


class TestStudioServerCleanup(unittest.TestCase):
    def setUp(self):
        from test_frontend_defaults import StudioServer
        self.StudioServer = StudioServer

    def test_stop_is_idempotent(self):
        """stop() 调三次也不能抛异常。"""
        s = self.StudioServer()
        self.assertTrue(s.start(), "Studio 实例启动失败")
        s.stop()
        s.stop()
        s.stop()

    def test_stop_kills_process_and_removes_tmp(self):
        """stop() 之后：进程退出 + 临时目录消失。"""
        s = self.StudioServer()
        self.assertTrue(s.start(), "Studio 实例启动失败")
        proc = s.proc
        tmp = s.tmp
        self.assertTrue(tmp.exists())
        s.stop()
        # 给一点时间让 SIGTERM 生效
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        self.assertIsNotNone(proc.poll(), "stop() 之后子进程应已退出")
        self.assertFalse(tmp.exists(), "stop() 之后临时目录应被删除")

    def test_start_registers_fallback_cleanup(self):
        """start() 必须注册兜底清理（atexit + weakref.finalize）。"""
        import atexit
        s = self.StudioServer()
        try:
            self.assertTrue(s.start(), "Studio 实例启动失败")
            self.assertTrue(hasattr(s, "_finalizer"),
                            "应注册 weakref.finalize 兜底")
            self.assertTrue(s._finalizer.alive,
                            "finalizer 应处于存活状态")
        finally:
            s.stop()

    def test_harness_source_has_no_bare_browser_leak(self):
        """守护：基座源码里必须保留兜底清理的注册点。"""
        src = (REPO / "tests" / "test_frontend_defaults.py").read_text(
            encoding="utf-8")
        self.assertIn("atexit.register(self.stop)", src,
                      "StudioServer.start 必须注册 atexit 兜底")
        self.assertIn("weakref.finalize", src,
                      "StudioServer.start 必须注册 weakref.finalize 兜底")


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
