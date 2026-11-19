#!/usr/bin/env python3
"""直接走 stdio 调 ekko-studio-mcp.mjs plan 提交任务卡。

【为什么不用 tool_call】harness 会在 plan[] 的每一项注入一个 'item' 键，
而 schema 是 additionalProperties:false -> 必然校验失败（项目 AGENTS.md
已记录这个坑）。stdio 直连不走那个注入路径。

用法：python3 _plan_push.py <context_id> <explanation> <plan.json>
"""
import json
import subprocess
import sys

CTX = sys.argv[1]
EXPLAIN = sys.argv[2]
PLAN = json.load(open(sys.argv[3]))

payload = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
        "name": "ekko_studio_update_plan",
        "arguments": {
            "context_id": CTX,
            "explanation": EXPLAIN,
            "plan": PLAN,
        },
    },
}

env = {
    "ELECTRON_RUN_AS_NODE": "1",
    "HERMES_WEB_UI_URL": "http://127.0.0.1:8648",
    "HERMES_WEB_UI_HOME": "/root/.hermes-web-ui",
    "HERMES_WEBUI_STATE_DIR": "/root/.hermes-web-ui",
    "HERMES_WEB_UI_PROFILE": "default",
    "HERMES_MCP_TOOLSET": "plan",
}
import os
env = {**os.environ, **env}

proc = subprocess.run(
    ["/root/.nvm/versions/node/v24.16.0/bin/node",
     "/root/.nvm/versions/node/v24.16.0/lib/node_modules/hermes-web-ui/bin/ekko-studio-mcp.mjs",
     "plan"],
    input=json.dumps(payload) + "\n",
    capture_output=True, text=True, env=env, timeout=120,
)
out = proc.stdout.strip()
try:
    data = json.loads(out.splitlines()[-1])
    content = data.get("result", {}).get("content", [])
    for item in content:
        print(item.get("text", "")[:400])
    if data.get("result", {}).get("isError"):
        print("IS_ERROR")
except Exception as exc:
    print("RAW:", out[:1500], "ERR:", exc, proc.stderr[:400])
