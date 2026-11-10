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
    with urllib.request.urlopen(req, timeout=500) as r:
        return json.load(r), time.time() - t

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.load(r)

MSG1 = "素材库里那个「雷神三视图」用得上吗？用它做参考图，改成夜景雷雨中的造型，竖版 9:16。"
print("=" * 78); print("【第 1 轮】", MSG1); print("=" * 78)
d1, t1 = post("/api/agent/chat", {"message": MSG1})
print(f"[{t1:.0f}s] status={d1['status']}")
print("answer:", d1["answer"][:400])
print("assets:", json.dumps(d1.get("assets"), ensure_ascii=False)[:400])
print("jobs:", json.dumps(d1.get("jobs"), ensure_ascii=False)[:700])
for s in d1.get("steps", []):
    print(f"  {s['step']}. {s.get('tool')} ok={s.get('ok')} {s.get('ms')}ms")
sid = d1["session_id"]

print()
print("=" * 78); print("【第 2 轮】同一会话追问：把时长改成 8 秒"); print("=" * 78)
d2, t2 = post("/api/agent/chat", {"message": "时长改成 8 秒再出一版", "session_id": sid})
print(f"[{t2:.0f}s] status={d2['status']}")
print("answer:", d2["answer"][:400])
print("jobs:", json.dumps(d2.get("jobs"), ensure_ascii=False)[:700])
for s in d2.get("steps", []):
    print(f"  {s['step']}. {s.get('tool')} ok={s.get('ok')} {s.get('ms')}ms")

print()
print("=" * 78); print("校验：两个任务是否都在任务库里"); print("=" * 78)
for j in (d1.get("jobs") or []) + (d2.get("jobs") or []):
    try:
        real = get("/api/jobs/" + j["job_id"])
        print(f"  {j['job_id']} mode={real['mode']} status={real['status']} "
              f"gen_type={real.get('gen_type')}")
        print(f"    params: {json.dumps(real['params'], ensure_ascii=False)[:400]}")
    except Exception as e:
        print(f"  {j['job_id']} 查询失败: {e}")
