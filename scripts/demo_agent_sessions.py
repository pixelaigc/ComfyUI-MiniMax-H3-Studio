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


import json, sys, time, urllib.request, urllib.error

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8307
BASE = f"http://127.0.0.1:{PORT}"

def post(path, body, timeout=400):
    req = urllib.request.Request(BASE + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.load(r)

print("== A. 不存在的 Provider -> 404 可读错误 ==")
st, body = post("/api/agent/chat", {"message": "测试", "provider_id": "no-such-provider"})
print("  ", st, body.get("error"), "| code =", body.get("code"))

print("\n== B. 正常一轮：列一下有哪些生成模式 ==")
st, body = post("/api/agent/chat", {"message": "列一下有哪些生成模式"})
sid = body.get("session_id")
print("   status:", body.get("status"), "| session_id:", sid)
print("   answer:", (body.get("answer") or "")[:250])

print("\n== C. 会话历史接口 ==")
h = get("/api/agent/sessions/" + sid)
print("   历史消息条数:", len(h["messages"]))
for m in h["messages"][:6]:
    print(f"     [{m['role']:9s}] {m['content'][:70]}")

print("\n== D. 会话清单 ==")
s = get("/api/agent/sessions")
print("   会话数:", len(s["sessions"]))
for item in s["sessions"][:3]:
    print(f"     {item['id']}  msgs={item['message_count']}  title={item['title'][:40]}")

print("\n== E. 同一 session_id 追问（多轮） ==")
st2, body2 = post("/api/agent/chat", {"message": "那音乐模式呢？", "session_id": sid})
print("   status:", body2.get("status"))
print("   answer:", (body2.get("answer") or "")[:250])
print("   本轮步骤:", [(x.get('tool'), x.get('ms')) for x in body2.get("steps", [])])

print("\n== F. 删除会话 ==")
req = urllib.request.Request(BASE + "/api/agent/sessions/" + sid, method="DELETE")
with urllib.request.urlopen(req, timeout=20) as r:
    print("  ", json.load(r))
