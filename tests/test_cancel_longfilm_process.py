# -*- coding: utf-8 -*-
"""第 3 轮 C 项：任务取消路径的 longfilm 缺陷回归测试。

【缺陷（2026-11-19 第 3 轮代码审查发现）】
longfilm（长片拼接）**不经过 ComfyUI** —— 它是
`asyncio.create_subprocess_exec` 起的 **ffmpeg 子进程**
（server.py 的 `run_longfilm`）。

服务端有**两条**取消实现：

| 实现 | 入口 | longfilm 分支 |
|---|---|:--:|
| `api_cancel` | HTTP `DELETE /api/jobs/{id}`（面板） | ✅ 有 |
| `cancel_job_record` | Agent 的 `cancel_job` 工具 | ❌ **没有** |

⇒ 后果：Agent 走的那条路径**只**把 DB 状态改成 `canceled`，
**ffmpeg 进程继续跑到结束** —— 用户以为已终止，实际还在烧 CPU
（合成一段长片可能耗几分钟）。

【本文件的断言】
1) `cancel_job_record` 对 longfilm 任务**必须**终止 ffmpeg 进程
   （用假进程对象，不真起 ffmpeg）。
2) 两条取消路径**必须共用**同一个实现（`_terminate_longfilm_process`）
   —— 两份实现必然漂移，这正是本缺陷的成因。
3) 幂等：连续取消两次 longfilm 不报错（第二次进程已不在表里）。

【变异自证】
  M1 把 `cancel_job_record` 的 longfilm 分支去掉 -> 断言必须红
  M2 把 `_terminate_longfilm_process` 改成什么都不做 -> 断言必须红
"""
from __future__ import annotations

import ast
import inspect
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _server_module():
    """导入 server 模块（它 import 一堆东西，失败就跳过）。"""
    import importlib
    try:
        return importlib.import_module("server")
    except Exception as exc:  # noqa: BLE001
        import pytest
        pytest.skip(f"server 模块导入失败（本机依赖不全）: {exc}")


class _FakeProcess:
    """假的长片 ffmpeg 进程对象（只实现 _terminate_longfilm_process 用到的部分）。"""

    def __init__(self, alive: bool = True):
        self.returncode = None if alive else 0
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15


def test_terminate_longfilm_process_terminates_live_process():
    """在跑的进程必须被 terminate()。"""
    server = _server_module()
    proc = _FakeProcess(alive=True)
    server.LONGFILM_PROCESSES["job_terminate_test"] = proc
    try:
        result = server._terminate_longfilm_process("job_terminate_test")
        assert result is True, "必须返回 True（确实终止了一个进程）"
        assert proc.terminated is True, "ffmpeg 进程没有被 terminate()"
    finally:
        server.LONGFILM_PROCESSES.pop("job_terminate_test", None)


def test_terminate_longfilm_process_handles_missing_and_dead():
    """进程不在表里 / 已结束 -> 返回 False，不抛异常（幂等）。"""
    server = _server_module()
    assert server._terminate_longfilm_process("job_not_exist_xyz") is False
    proc = _FakeProcess(alive=False)
    server.LONGFILM_PROCESSES["job_dead_test"] = proc
    try:
        # 已结束的进程：returncode 非 None -> 不重复 terminate
        assert server._terminate_longfilm_process("job_dead_test") is True
        assert proc.terminated is False, "已结束的进程不应再被 terminate"
    finally:
        server.LONGFILM_PROCESSES.pop("job_dead_test", None)


def test_cancel_job_record_kills_longfilm_process():
    """**核心断言**：Agent 的 cancel_job 路径必须终止 longfilm 的 ffmpeg。

    用真实的 `cancel_job_record`，但把 DB 与进程表都替换成测试替身
    —— 不连真库、不起真 ffmpeg。
    """
    server = _server_module()

    job_id = "job_cancel_longfilm_test"
    proc = _FakeProcess(alive=True)
    server.LONGFILM_PROCESSES[job_id] = proc

    class _Row(dict):
        pass

    row = _Row(status="running", prompt_id=None, mode="longfilm")

    class _Conn:
        """模拟 DB 连接：第一次读出 running，之后 UPDATE 成 canceled。"""

        def __init__(self):
            self.updated = False

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, args=()):
            if "UPDATE jobs SET status='canceled'" in sql:
                self.updated = True
            return self

        def fetchone(self):
            if self.updated:
                return _Row(status="canceled", prompt_id=None, mode="longfilm")
            return row

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    original_db = server.db
    original_lock = server.WORKER.submit_lock

    class _FakeLock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    server.db = lambda: _Ctx()
    server.WORKER.submit_lock = _FakeLock()
    try:
        import asyncio
        result = asyncio.run(server.cancel_job_record(job_id))
    finally:
        server.db = original_db
        server.WORKER.submit_lock = original_lock
        server.LONGFILM_PROCESSES.pop(job_id, None)

    assert proc.terminated is True, (
        "Agent 的 cancel_job 必须终止 longfilm 的 ffmpeg 进程 —— "
        "缺陷现象：只改 DB 状态，进程继续跑到结束")
    assert result.get("status") == "canceled"
    assert result.get("terminated_process") is True


def test_both_cancel_paths_share_one_implementation():
    """两条取消路径**必须共用** `_terminate_longfilm_process`。

    【为什么这条也要锁】—— 缺陷的成因就是**两份实现漂移**：
    `api_cancel` 有一份内联的 longfilm 处理，`cancel_job_record` 没有。
    只补 `cancel_job_record` 而不让两边共用，下次改一边又会漂移。
    """
    server = _server_module()
    src = (REPO / "server.py").read_text(encoding="utf-8")

    # api_cancel 里必须调共用函数，不能再有内联的 LONGFILM_PROCESSES 访问
    api_src = inspect.getsource(server.api_cancel)
    assert "_terminate_longfilm_process" in api_src, (
        "api_cancel 必须调用共用的 _terminate_longfilm_process")
    assert "LONGFILM_PROCESSES.get" not in api_src, (
        "api_cancel 里还有内联的 LONGFILM_PROCESSES 访问 —— "
        "两份实现会再次漂移")

    cancel_src = inspect.getsource(server.cancel_job_record)
    assert "_terminate_longfilm_process" in cancel_src, (
        "cancel_job_record 必须调用共用的 _terminate_longfilm_process")

    # 整个 server.py 里只应有一处直接访问 LONGFILM_PROCESSES 的终止逻辑
    tree = ast.parse(src)
    direct = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "get":
            v = node.value
            if isinstance(v, ast.Name) and v.id == "LONGFILM_PROCESSES":
                direct.append(node.lineno)
    # 只有 _terminate_longfilm_process 内部那一处（+ 写入处不算 get）
    assert len(direct) <= 1, (
        f"LONGFILM_PROCESSES.get 出现在多处 {direct} —— "
        "终止逻辑又散开了")


def test_cancel_job_record_longfilm_branch_exists():
    """源码级：cancel_job_record 里必须有 longfilm 分支（防回归）。"""
    src = (REPO / "server.py").read_text(encoding="utf-8")
    server = _server_module()
    fn_src = inspect.getsource(server.cancel_job_record)
    assert 'mode == "longfilm"' in fn_src, (
        "cancel_job_record 里没有 longfilm 分支 —— "
        "Agent 取消长片任务时 ffmpeg 进程不会被终止")
