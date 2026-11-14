#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""目标 1 的**实测**：真跑一次超过 8 步的工具调用链路 + 负控。

【为什么要单独跑，不靠单元测试】
单元测试证明「断言会红」；这个脚本证明「**本机此刻**真的跑得完」。
它调的是**真实的** `h3studio.agent.run_agent`（真循环、真工具分发、
真步数上限），只把**模型**换成假的（否则要真的连 Qwen 桥跑十几轮）。

【负控】同一份链路，把上限压回 8 -> 必须被截断。
否则「跑完了」可能只是因为链路变短了。

输出：最后一行是 JSON（供采集脚本解析）。
用法：python3 scripts/step_limit_probe.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.chdir(REPO)

import server as srv                                   # noqa: E402
from h3studio import agent as agent_mod                # noqa: E402

ROUNDS = 12          # > 8，故意超过旧上限


def _fake_provider():
    """假的模型层：每次都要调工具，凑够 ROUNDS 轮才给结论。"""
    calls = {"n": 0}

    async def fake(provider, messages, tools, options=None, images=None):
        index = calls["n"]
        calls["n"] += 1
        if index >= ROUNDS:
            return {"content": f"任务已完成：共 {ROUNDS} 步",
                    "tool_calls": [], "raw": {}}
        return {
            "content": None,
            "tool_calls": [{
                "id": f"c{index}",
                "name": "get_mode_schema",
                "arguments": {"mode": "t2v" if index % 2 else "t2i_qwen"},
            }],
            "raw": {},
        }
    return fake


async def _run(max_steps):
    fake = _fake_provider()
    original = agent_mod.llm_client.call_provider_with_tools
    agent_mod.llm_client.call_provider_with_tools = fake
    try:
        return await agent_mod.run_agent(
            srv, f"跑一条超过 {ROUNDS - 4} 步的任务", max_steps=max_steps)
    finally:
        agent_mod.llm_client.call_provider_with_tools = original


async def main():
    full = await _run(agent_mod.MAX_STEPS)
    capped = await _run(8)
    return {
        "max_steps_constant": agent_mod.MAX_STEPS,
        "rounds_requested": ROUNDS,
        "status": full.get("status"),
        "rounds": (full.get("usage") or {}).get("steps"),
        "error": full.get("error"),
        "status_at_8": capped.get("status"),
        "rounds_at_8": (capped.get("usage") or {}).get("steps"),
        "error_at_8": capped.get("error"),
    }


if __name__ == "__main__":
    try:
        res = asyncio.run(main())
    except Exception as exc:
        res = {"fatal": repr(exc)}
    print(json.dumps(res, ensure_ascii=False))
    sys.exit(0)
