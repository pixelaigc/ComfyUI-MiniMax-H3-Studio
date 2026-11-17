# -*- coding: utf-8 -*-
"""
仓库根 conftest —— **只**做两件事：

1. 注册 E2E 测试集的命令行参数；
2. 注册 ``e2e`` 标记。

【为什么参数必须在这里注册】
pytest 只对「初始路径」的 conftest 调用 ``pytest_addoption``。
跑 ``pytest tests/`` 时，``tests/e2e/conftest.py`` 是**子目录**的 conftest，
它的 ``pytest_addoption`` 不会被调用 —— 于是 ``--e2e-isolated`` 变成
「unrecognized arguments」，整个收集直接中断（实测报
``ValueError: no option named 'e2e_isolated'``）。

所以：参数在这里注册，**装置**留在 tests/e2e/conftest.py。

【这个文件不改变任何既有测试的行为】
它只增加参数与标记；不注册任何 fixture、不改收集逻辑。
"""
from __future__ import annotations

import os
import time


def pytest_addoption(parser):
    group = parser.getgroup("e2e")
    group.addoption("--e2e-base", action="store",
                    default=os.environ.get("H3STUDIO_E2E_BASE",
                                           "http://127.0.0.1:8190"),
                    help="被测 Studio 的 base URL（默认线上 8190）")
    group.addoption("--e2e-isolated", action="store_true",
                    help="起一个隔离的 Studio 实例（独立端口 + 独立数据目录）")
    group.addoption("--e2e-real-comfy", action="store_true",
                    help="用真实 ComfyUI（会真的占 GPU 跑模型，默认关闭）")
    group.addoption("--e2e-headed", action="store_true",
                    help="有头模式（需要 DISPLAY；默认 headless）")
    group.addoption("--e2e-run-id", action="store", default="",
                    help="本次运行的证据目录名（默认按时间戳）")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "e2e: 真实浏览器端到端用例（tests/e2e/）")
