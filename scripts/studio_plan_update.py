#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 stdio 直连的方式更新 Studio 任务卡（绕开 harness 注入的 `item` 键）。

【为什么走 stdio】
harness 注入的 ekko_studio_update_plan 必然带一个 `item` 键，而 schema 是
additionalProperties:false -> 必然参数校验失败。这是 harness 侧 bug，
跟传的内容无关（AGENTS.md 已记录）。

【为什么要用脚本而不是 shell】
JSON-RPC 的 payload 里有大量花括号，用 shell 拼 f-string 会被 Python 的
f-string 语法卡住。写成脚本最稳。
"""
from __future__ import annotations

import json
import os
import subprocess

CONTEXT_ID = "a14966bf-d9b2-42ee-8c0c-999c126ad425"

NODE = "/root/.nvm/versions/node/v24.16.0/bin/node"
MCP = ("/root/.nvm/versions/node/v24.16.0/lib/node_modules/"
       "hermes-web-ui/bin/ekko-studio-mcp.mjs")

PLAN = [
    {"id": "quota", "status": "completed",
     "step": "核实现场并读额度快照过闸门（remaining_percent_5h=77% ≥ 20%）"},
    {"id": "locate", "status": "completed",
     "step": "逐处定位 30 分钟硬超时实现（后端 worker / running_truth 判定层 / 前端轮询 / 队列核查）"},
    {"id": "locate-oom", "status": "completed",
     "step": "定位 OOM 被误判成功的根因（判定顺序 + .job-preview 近黑底色）"},
    {"id": "fix-timeout", "status": "completed",
     "step": "修缺陷 1：视频类取消时长判失败，阈值按媒体类型可配置，保留队列丢失判定"},
    {"id": "fix-oom", "status": "completed",
     "step": "修缺陷 2：失败优先判定 + 产物真实存在校验 + 失败占位不留黑图"},
    {"id": "regression", "status": "completed",
     "step": "补回归用例并做变异测试（断言能抓住原始缺陷）"},
    {"id": "deploy", "status": "completed",
     "step": "合并主线并部署 8190，重启服务、六个接口全部 200、策略 video=0"},
    {"id": "evidence", "status": "completed",
     "step": "取真实证据：长时视频仍为进行中 + OOM 失败卡片无黑图（浏览器截图）"},
    {"id": "full-regression", "status": "in_progress",
     "step": "跑全量 pytest 基线对比（基线 fcb6057 vs 候选分支）"},
]


def main() -> int:
    payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "ekko_studio_update_plan",
            "arguments": {
                "context_id": CONTEXT_ID,
                "explanation": "两项缺陷的代码修复、部署与取证已完成，正在跑最后一轮全量 pytest 基线对比。",
                "plan": PLAN,
            },
        },
    }
    env = {
        **os.environ,
        "ELECTRON_RUN_AS_NODE": "1",
        "HERMES_WEB_UI_URL": "http://127.0.0.1:8648",
        "HERMES_WEB_UI_HOME": "/root/.hermes-web-ui",
        "HERMES_WEBUI_STATE_DIR": "/root/.hermes-web-ui",
        "HERMES_WEB_UI_PROFILE": "coding",
        "HERMES_MCP_TOOLSET": "plan",
    }
    proc = subprocess.run(
        [NODE, MCP, "plan"],
        input=json.dumps(payload) + "\n",
        capture_output=True, text=True, timeout=60, env=env)
    out = proc.stdout.strip().splitlines()
    print(out[-1] if out else proc.stderr[-500:])
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
