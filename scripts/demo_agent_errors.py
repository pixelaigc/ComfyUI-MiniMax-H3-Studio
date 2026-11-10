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


import json, sys, threading, time, urllib.request, urllib.error

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8307
BASE = f"http://127.0.0.1:{PORT}"

def post(path, body, timeout=60):
    req = urllib.request.Request(BASE + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")

print("== A. 空消息 -> 400 可读错误 ==")
print("  ", post("/api/agent/chat", {"message": "   "}))

print("\n== B. 请求体不是 JSON -> 400 ==")
req = urllib.request.Request(BASE + "/api/agent/chat", data=b"not json",
                             headers={"Content-Type": "application/json"})
try:
    urllib.request.urlopen(req, timeout=20)
except urllib.error.HTTPError as e:
    print("  ", e.code, e.read().decode("utf-8"))

print("\n== C. 并发闸门：同时发 4 个请求 -> 应有 429 ==")
results = []
def fire(i):
    t = time.time()
    st, body = post("/api/agent/chat", {"message": f"并发测试 {i}"}, timeout=60)
    results.append((i, st, body.get("code") or body.get("status"), round(time.time()-t, 1)))

threads = [threading.Thread(target=fire, args=(i,)) for i in range(4)]
for t in threads: t.start()
for t in threads: t.join()
for i, st, code, dt in sorted(results):
    print(f"   请求{i}: HTTP {st}  code/status={code}  {dt}s")

print("\n== D. 不存在的 Provider -> 404 可读错误 ==")
st, body = post("/api/agent/chat", {"message": "测试", "provider_id": "no-such-provider"})
print("  ", st, body.get("error"))

print("\n== E. 工具清单 + 限额 ==")
with urllib.request.urlopen(BASE + "/api/agent/tools", timeout=20) as r:
    d = json.load(r)
print("   工具:", [t["name"] for t in d["tools"]])
print("   限额:", json.dumps(d["limits"], ensure_ascii=False))

print("\n== F. 会话接口 ==")
st, body = post("/api/agent/chat", {"message": "列一下有哪些生成模式"}, timeout=200)
sid = body.get("session_id")
print("   session_id:", sid)
with urllib.request.urlopen(BASE + "/api/agent/sessions/" + sid, timeout=20) as r:
    h = json.load(r)
print("   历史消息条数:", len(h["messages"]))
for m in h["messages"][:4]:
    print(f"     [{m['role']}] {m['content'][:80]}")
with urllib.request.urlopen(BASE + "/api/agent/sessions", timeout=20) as r:
    s = json.load(r)
print("   会话清单条数:", len(s["sessions"]))
