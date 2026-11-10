# -*- coding: utf-8 -*-
"""复现「主线程残留一个正在运行的 event loop」这个真实故障条件。

背景（2026-11-10 实测）：
  全量 pytest 里，前面的浏览器用例 setUpClass 失败 -> tearDownClass 不跑
  -> playwright.stop() 不被调用 -> Playwright 那个**正在运行**的 loop
  留在主线程上 -> 后续任何 asyncio.run_until_complete 都抛
     RuntimeError: Cannot run the event loop while another loop is running

这个文件用「人为在主线程留一个 running loop」来模拟该条件，
然后跑 test_prompt_history.py 的两个 event-loop 类，
验证它们不再被这个外部脏状态打倒。

用法：python3 -m pytest tests/test_prompt_history_dirtyloop.py -q
"""
import asyncio
import sys
import threading
import unittest
from pathlib import Path

import greenlet

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))


class _DirtyLoopHolder:
    """在**主线程**留一个正在运行的 event loop（模拟 Playwright 残留）。

    【为什么必须是「同一个线程里正在跑」】
    asyncio.base_events.BaseEventLoop._check_running() 的判断是：
        if events._get_running_loop() is not None:
            raise RuntimeError('Cannot run the event loop while another loop is running')
    `_get_running_loop()` 读的是 **thread-local**（threading.local），
    所以只有当**当前线程**里真的有一个正在运行的 loop 时才会触发。
    —— 光把 loop 设成 current loop（但不 run）是不够的。

    Playwright 的 sync API 正是这种形态：它用 greenlet 在**调用线程**里
    驱动 `loop.run_until_complete(self._connection.run_as_sync())`，
    那个 loop 从此一直 running，且留在主线程。
    """

    def __init__(self):
        self._loop = None
        self._thread = None
        self._dispatcher = None
        self._ready = threading.Event()

    def install(self):
        """复现 Playwright 的形态：在**当前线程**里用 greenlet 驱动 loop。

        Playwright 的 sync API 走的是：
            dispatcher_fiber = MainGreenlet(greenlet_main)
            greenlet_main: self._loop.run_until_complete(self._connection.run_as_sync())
        greenlet 与主线程**共享 thread-local**，而 `run_until_complete` 会把
        这个 loop 写进 `events._get_running_loop()`（thread-local）。
        于是从那一刻起，主线程就带着一个「正在运行的 loop」。

        这里照抄这个结构：greenlet + run_until_complete。
        关键：greenlet 里第一次跑到 `g_self.switch()` 时切回主线程，
        但 loop 的 run_until_complete **还挂着**（它在等一个永不完成的任务），
        于是「running loop」的标记留在主线程上 —— 这正是要模拟的状态。
        """
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)

        g_self = greenlet.getcurrent()

        async def _never_ends():
            # 切回主线程，之后这个协程永远不完成
            g_self.switch()
            await asyncio.Event().wait()

        def greenlet_main():
            try:
                loop.run_until_complete(_never_ends())
            except BaseException:       # noqa: BLE001
                pass

        self._dispatcher = greenlet.greenlet(greenlet_main)
        self._ready.set()
        self._dispatcher.switch()          # 切进 greenlet，它会再切回主线程
        return self

    @staticmethod
    async def _never_ends():
        await asyncio.Event().wait()

    def uninstall(self):
        """把 greenlet 从「永不完成」的 await 里救出来，并把 thread-local 清干净。

        greenlet 里那个协程在 `await asyncio.Event().wait()` 上挂着。
        要退出来，得：
          1. 把 loop 里那个 Event set 掉 —— 让协程结束、run_until_complete 返回；
          2. 再切一次 greenlet，让它把控制权交回主线程；
          3. 最后清掉 thread-local 里的 running loop 标记。
        """
        loop = self._loop
        if loop is not None and self._dispatcher is not None:
            try:
                async def _release():
                    loop.call_soon_threadsafe(loop.stop)
                # 直接让 loop 停转：协程等的是 Event，但 loop 一停，
                # run_until_complete 会以「loop 停了」结束，greenlet 拿到控制权。
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
            try:
                # 把控制权交回 greenlet，让它从 run_until_complete 里退出
                self._dispatcher.switch()
            except BaseException:           # noqa: BLE001
                pass
            self._dispatcher = None
        if loop is not None:
            try:
                loop.close()
            except RuntimeError:
                pass
        self._loop = None
        self._thread = None
        # 清掉 thread-local 里的 running loop 标记
        from asyncio import events
        events._set_running_loop(None)
        asyncio.set_event_loop(None)


class TestPromptHistorySurvivesDirtyLoop(unittest.TestCase):
    """主线程有脏的 running loop 时，本功能的用例必须照常通过。"""

    @classmethod
    def setUpClass(cls):
        cls.holder = _DirtyLoopHolder().install()
        # 先确认「脏状态」确实存在 —— 否则这个测试就是空转。
        # 判据与 asyncio 内部一致：events._get_running_loop() is not None
        from asyncio import events
        running = events._get_running_loop() is not None
        assert running, "前置条件不成立：主线程并没有 running loop"

        # 同时验证「旧写法」确实会被这个脏状态打倒 —— 证明前置条件有效，
        # 也证明这个测试不是「怎么都绿」的空转。
        async def _noop():
            return None

        probe = asyncio.new_event_loop()
        try:
            coro = _noop()
            try:
                probe.run_until_complete(coro)
            except RuntimeError:
                coro.close()
                pass
            else:
                raise AssertionError(
                    "前置条件不成立：脏状态下 run_until_complete 本该抛 "
                    "RuntimeError，却成功了")
        finally:
            probe.close()

    @classmethod
    def tearDownClass(cls):
        cls.holder.uninstall()

    def test_api_class_passes_with_dirty_loop(self):
        from test_prompt_history import TestPromptHistoryApi
        suite = unittest.TestSuite()
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(
            TestPromptHistoryApi))
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertEqual(result.testsRun, 7)
        self.assertEqual(len(result.errors), 0, result.errors)
        self.assertEqual(len(result.failures), 0, result.failures)

    def test_e2e_class_passes_with_dirty_loop(self):
        from test_prompt_history import TestPromptHistoryEndToEnd
        suite = unittest.TestSuite()
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(
            TestPromptHistoryEndToEnd))
        result = unittest.TextTestRunner(verbosity=0).run(suite)
        self.assertEqual(result.testsRun, 3)
        self.assertEqual(len(result.errors), 0, result.errors)
        self.assertEqual(len(result.failures), 0, result.failures)


if __name__ == "__main__":
    unittest.main()
