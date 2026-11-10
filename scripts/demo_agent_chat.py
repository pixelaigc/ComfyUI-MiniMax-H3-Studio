# -*- coding: utf-8 -*-
"""
内置 Agent 真实调用演示脚本。

用法：
    python3 scripts/demo_agent_chat.py [端口] [自定义意图]
    python3 scripts/demo_agent_multiturn.py [端口]
    python3 scripts/demo_agent_errors.py [端口]
    python3 scripts/demo_agent_sessions.py [端口]
    python3 scripts/verify_agent_ui.py [端口]

需要一个**已启动**的 Studio 实例。默认端口 8307（测试实例），
线上服务是 8190 —— 演示前请确认目标实例上已配置好本地模型 Provider。

脚本会**验证任务是否真的落进任务库**（不能只看响应体 ——
响应里的 job_id 可能是模型编的）。
"""


import json, sys, time, urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8307
BASE = f"http://127.0.0.1:{PORT}"

def post(path, body):
    req = urllib.request.Request(BASE + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=400) as r:
        return json.load(r), time.time() - t

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.load(r)

MESSAGE = (sys.argv[2] if len(sys.argv) > 2 else
           "素材库里有没有雨夜的素材？如果有，就用它做参考图，生成一段 5 秒、16:9 的文生视频。")

print("=" * 78)
print("请求 POST /api/agent/chat")
print("  message:", MESSAGE)
print("=" * 78)
data, dt = post("/api/agent/chat", {"message": MESSAGE})
print(f"\n[耗时 {dt:.1f}s]  HTTP 200")
print(json.dumps(data, ensure_ascii=False, indent=2))

print("\n" + "=" * 78)
print("校验：任务是否真的落到任务库（而不是只在响应里编了个 id）")
print("=" * 78)
for job in data.get("jobs") or []:
    try:
        real = get("/api/jobs/" + job["job_id"])
        print(f"  job_id={job['job_id']}  在任务库中: mode={real['mode']} "
              f"status={real['status']} gen_type={real.get('gen_type')}")
        print(f"    提示词: {real['params'].get('prompt','')[:90]}")
        print(f"    参数键: {sorted(real['params'].keys())}")
    except Exception as exc:
        print(f"  job_id={job['job_id']} 查询失败: {exc}")
