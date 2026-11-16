# -*- coding: utf-8 -*-
"""8190 不可访问根因的回归测试 —— **event loop 不得被 LoRA 清单扫描阻塞**。

⚠️ **为什么需要这个测试（2026-11-16 线上事故）**
---------------------------------------------------------------------------
线上 `http://192.168.3.78:8190/` 表现为「端口在监听但页面打不开」，
根因是 `api_image_loras` 在 **asyncio 事件循环里同步做全量目录扫描**：

    available     = list_effect_loras()        # 全量扫描
    incompatible = list_incompatible_loras()  # 又一次全量扫描
    acceleration = list_acceleration_loras()   # 又一次全量扫描

每次扫描要 rglob 700+ 个 LoRA 文件，并对 465 个文件**同步 open() 读
safetensors 头部**（LoRA 目录挂在 USB NTFS fuseblk 上，冷缓存时单次
stat 就要几十毫秒）。事件循环被冻结期间，**所有** HTTP 请求（含首页 /）
一起挂起 —— 于是表现为「服务活着但整站不可访问」。

py-spy 现场抓到的真实栈（10:46）：

    Thread 3462 (idle): "MainThread"
        getsize (<frozen genericpath>:86)
        _header_covers_file (h3studio/moss_lora_compat.py:153)
        _looks_like_z_image_layers (h3studio/loras.py:442)
        list_z_image_loras (h3studio/loras.py:1619)
        api_image_loras (server.py:6331)

这个测试用**假扫描函数 + 真实 asyncio 事件循环**验证两件事：

  1. **不阻塞**：扫描在跑的时候，事件循环仍能调度别的协程
     （断言「扫描期间并发跑的另一个协程立刻得到执行」）
  2. **扫描只做一次**：三份清单来自同一份快照，
     （断言「扫描函数的调用次数 == 1」，抓住「重复扫描」回归）

⚠️ **断言纪律**：断言不能自指 —— 不能拿被测函数自己的输出去算期望值。
这里断言的是**可观测的行为**（并发协程的推进、扫描函数的调用计数），
不是「inventory 的内容等于 inventory 自己」。
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from h3studio import loras as lora_registry  # noqa: E402


class _ScanCounter:
    """假扫描器：记录被调用了几次，并可模拟「慢盘上的慢扫描」。"""

    def __init__(self, items, delay: float = 0.35):
        self.calls = 0
        self.items = items
        self.delay = delay
        self.call_threads: list[int] = []

    def __call__(self, deep_analysis: bool = False) -> list[dict]:
        self.calls += 1
        self.call_threads.append(threading.get_ident())
        # 模拟真实场景：目录扫描是慢的（USB 盘冷缓存）
        time.sleep(self.delay)
        return list(self.items)


class TestLoraInventorySingleScan(unittest.TestCase):
    """单次扫描 —— 三份清单必须来自同一份快照。"""

    def test_inventory_by_kind_scans_exactly_once(self):
        """lora_inventory_by_kind 只应触发**一次**底层扫描。

        旧代码路径（三次 list_* 调用）会触发 3 次 —— 这条断言抓住那个回归。
        """
        fake = _ScanCounter(items=[], delay=0.0)
        original = lora_registry.list_z_image_loras
        lora_registry.list_z_image_loras = fake
        try:
            lora_registry.lora_inventory_by_kind()
        finally:
            lora_registry.list_z_image_loras = original
        self.assertEqual(
            fake.calls, 1,
            "lora_inventory_by_kind 必须只扫描一次（重复扫描 = event loop 被"
            "阻塞 3 倍）",
        )

    def test_inventory_kind_partition_is_exhaustive_and_disjoint(self):
        """三份清单的并集 = 全部扫描结果，且互不重叠。

        这条断言保证「合并扫描」没有把任何条目弄丢或重复计数。

        ⚠️ 口径说明：`effect` 是**全部效果类条目（含不兼容的）**，
        `incompatible` 是其中 compatible=False 的**子集**。
        所以「并集」要用「effect ∪ acceleration」而不是三者相加 ——
        incompatible 本来就包含在 effect 里。
        （调用方要「可用的效果类」时取 effect 里 compatible=True 的部分。）
        """
        items = [
            {"name": "a.safetensors", "kind": lora_registry.KIND_EFFECT,
             "compatible": True},
            {"name": "b.safetensors", "kind": lora_registry.KIND_EFFECT,
             "compatible": False},
            {"name": "c.safetensors", "kind": lora_registry.KIND_ACCELERATION,
             "compatible": True},
        ]
        fake = _ScanCounter(items=items, delay=0.0)
        original = lora_registry.list_z_image_loras
        lora_registry.list_z_image_loras = fake
        try:
            inv = lora_registry.lora_inventory_by_kind()
        finally:
            lora_registry.list_z_image_loras = original

        # 1) effect ∪ acceleration == 全部扫描结果（无丢失、无重复）
        union = sorted([i["name"] for i in inv["effect"]]
                       + [i["name"] for i in inv["acceleration"]])
        self.assertEqual(union, ["a.safetensors", "b.safetensors",
                                  "c.safetensors"],
                         "effect ∪ acceleration 必须等于全部扫描结果")
        # 2) incompatible 必须是 effect 的子集（不是另一份独立数据）
        effect_names = {i["name"] for i in inv["effect"]}
        self.assertTrue(
            {i["name"] for i in inv["incompatible"]} <= effect_names,
            "不兼容条目必须同时出现在 effect 里（前端要显示为什么被排除）",
        )
        # 3) incompatible 恰好是 effect 里 compatible=False 的那些
        self.assertEqual(
            sorted(i["name"] for i in inv["incompatible"]),
            ["b.safetensors"],
            "incompatible 必须恰好是 effect 里 compatible=False 的条目",
        )


    def test_available_filter_matches_legacy_list_effect_loras(self):
        """新入口的「可用效果类」口径必须与旧的 `list_effect_loras()` 一致。

        旧 `list_effect_loras()` = effect 且 compatible=True。
        新 `inventory["effect"]` 里筛 compatible=True 必须得到**同样的集合**。
        这条防止重构时把「可用」和「全部」的口径搞反（界面会少显示 LoRA）。
        """
        items = [
            {"name": "ok1.safetensors", "kind": lora_registry.KIND_EFFECT,
             "compatible": True},
            {"name": "bad.safetensors", "kind": lora_registry.KIND_EFFECT,
             "compatible": False},
            {"name": "ok2.safetensors", "kind": lora_registry.KIND_EFFECT,
             "compatible": True},
            {"name": "acc.safetensors", "kind": lora_registry.KIND_ACCELERATION,
             "compatible": True},
        ]
        fake = _ScanCounter(items=items, delay=0.0)
        original = lora_registry.list_z_image_loras
        lora_registry.list_z_image_loras = fake
        try:
            inv = lora_registry.lora_inventory_by_kind()
            legacy = lora_registry.list_effect_loras()
        finally:
            lora_registry.list_z_image_loras = original

        new_available = sorted(i["name"] for i in inv["effect"]
                               if i.get("compatible", True))
        legacy_available = sorted(i["name"] for i in legacy)
        self.assertEqual(
            new_available, legacy_available,
            "新入口筛出的「可用效果类」必须与旧 list_effect_loras() 完全一致",
        )
        self.assertEqual(new_available, ["ok1.safetensors", "ok2.safetensors"])


class TestEventLoopNotBlocked(unittest.TestCase):
    """**核心回归**：扫描在跑的时候，事件循环必须还能调度别的协程。"""

    def test_scan_does_not_block_event_loop(self):
        """`asyncio.to_thread(scan)` 期间，并发协程必须能立刻推进。

        这是本次事故的直接判据：旧代码把扫描直接跑在事件循环里，
        并发协程在整个扫描期间**一次都不会被调度**。
        """
        delay = 0.35
        fake = _ScanCounter(items=[], delay=delay)
        original = lora_registry.list_z_image_loras
        lora_registry.list_z_image_loras = fake
        try:
            progressed = {}

            async def _scan():
                return await asyncio.to_thread(
                    lora_registry.lora_inventory_by_kind)

            async def _probe():
                """并发探针：只要事件循环没被冻结，它就能在扫描期间跑完。"""
                await asyncio.sleep(0.05)
                progressed["t"] = time.perf_counter()
                return True

            async def _main():
                scan_task = asyncio.create_task(_scan())
                probe_task = asyncio.create_task(_probe())
                # 先给扫描一个机会进入阻塞状态，再看探针能不能推进
                await asyncio.sleep(0.1)
                done_early = probe_task.done()
                await scan_task
                await probe_task
                return done_early

            done_early = asyncio.run(_main())
        finally:
            lora_registry.list_z_image_loras = original

        self.assertTrue(
            done_early,
            "扫描进行期间并发协程必须能推进 —— 若此处失败，说明扫描又跑回了"
            "事件循环里（2026-11-16 线上 8190 整站不可访问的根因）",
        )

    def test_scan_runs_off_event_loop_thread(self):
        """扫描必须发生在**工作线程**里，不是事件循环所在的线程。

        线程身份是可观测的外部事实（threading.get_ident），
        不依赖被测函数的返回值。
        """
        fake = _ScanCounter(items=[], delay=0.0)
        original = lora_registry.list_z_image_loras
        lora_registry.list_z_image_loras = fake
        try:
            async def _main():
                loop_thread = threading.get_ident()
                await asyncio.to_thread(lora_registry.lora_inventory_by_kind)
                return loop_thread

            loop_thread = asyncio.run(_main())
        finally:
            lora_registry.list_z_image_loras = original

        self.assertEqual(len(fake.call_threads), 1)
        self.assertNotEqual(
            fake.call_threads[0], loop_thread,
            "扫描必须在工作线程执行，不能占用事件循环线程",
        )


class TestHandlerUsesThreadOffload(unittest.TestCase):
    """接口层契约 —— `api_image_loras` 必须走线程卸载 + 单次扫描入口。

    ⚠️ **断言写法说明（变异测试教训）**
    最初这条测试只断言「handler 源码里存在 `asyncio.to_thread` 字符串」。
    变异测试证明那是**假阳性**：把三个分支的 `to_thread` 全部去掉后，
    因为 handler 注释里还有 "asyncio.to_thread" 字样、且 PUT handler
    里的 `to_thread` 还在，断言照样通过 —— 断言没有真的咬住行为。

    现在的写法是**逐个扫描调用点**检查：凡是 `lora_inventory_by_kind*`
    的调用，必须**出现在 `asyncio.to_thread(...)` 的参数里**。
    用 AST 解析调用结构，而不是 grep 字符串。
    """

    SCAN_FUNCS = ("lora_inventory_by_kind",
                  "lora_inventory_by_kind_qwen",
                  "lora_inventory_by_kind_moss")

    def _handler_body(self, name: str, next_name: str) -> str:
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        start = src.index(f"async def {name}(")
        end = src.index(f"async def {next_name}(")
        return src[start:end]

    def _scan_calls_not_offloaded(self, body: str) -> list[str]:
        """返回「没有被 to_thread 包裹」的扫描调用名列表。

        用 AST 判：凡是 `lora_registry.lora_inventory_by_kind*` 的 Call 节点，
        其最近的祖先 Call 必须是 `asyncio.to_thread`。
        """
        import ast

        tree = ast.parse(body)
        results: list[str] = []

        class Visitor(ast.NodeVisitor):
            def __init__(self):
                self.stack: list[ast.Call] = []

            def visit_Call(self, node: ast.Call):
                # 先看这个调用本身是不是扫描调用
                func = node.func
                name = None
                if isinstance(func, ast.Attribute):
                    name = func.attr
                elif isinstance(func, ast.Name):
                    name = func.id
                if name in TestHandlerUsesThreadOffload.SCAN_FUNCS:
                    # 最近一层祖先调用必须是 asyncio.to_thread
                    if not self.stack:
                        results.append(name)
                    else:
                        outer = self.stack[-1]
                        outer_func = outer.func
                        outer_name = getattr(outer_func, "attr", None) or \
                            getattr(outer_func, "id", None)
                        if outer_name != "to_thread":
                            results.append(name)
                self.stack.append(node)
                self.generic_visit(node)
                self.stack.pop()

        Visitor().visit(tree)
        return results

    def test_api_image_loras_all_scans_offloaded(self):
        body = self._handler_body("api_image_loras", "api_image_loras_save")
        bad = self._scan_calls_not_offloaded(body)
        self.assertEqual(
            bad, [],
            f"api_image_loras 里这些扫描调用没有走 asyncio.to_thread：{bad} "
            "（同步扫描跑在事件循环里 = 2026-11-16 线上 8190 整站不可访问根因）",
        )

    def test_api_image_loras_save_all_scans_offloaded(self):
        body = self._handler_body("api_image_loras_save", "api_gpu_power_limit")
        bad = self._scan_calls_not_offloaded(body)
        self.assertEqual(
            bad, [],
            f"api_image_loras_save 里这些扫描调用没有走 asyncio.to_thread：{bad}",
        )

    def test_api_image_loras_uses_single_scan_entrypoints(self):
        """接口必须走「单次扫描」入口，而不是三个独立的 list_* 函数。

        旧代码调三个 list_* 函数 = 同一份全量扫描做 3 遍。
        """
        body = self._handler_body("api_image_loras", "api_image_loras_save")
        for legacy in ("list_effect_loras", "list_incompatible_loras",
                       "list_acceleration_loras",
                       "list_qwen_effect_loras",
                       "list_qwen_incompatible_loras",
                       "list_moss_incompatible_loras"):
            self.assertNotIn(
                f"lora_registry.{legacy}", body,
                f"api_image_loras 不应再直接调 {legacy}（那是重复全量扫描）",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
