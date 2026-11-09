#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立实跑实例（8199）的真实 LLM 调用验证脚本。

覆盖任务要求的三个场景：
  1. 正常建任务   —— 给出落库记录（job id / mode / status / params 来自真实 SQLite）
  2. 参数非法被纠正 —— 用户给非法值（30 秒 / 21:10 比例），看 Agent 是否被
     校验器纠正并用合法值建任务
  3. 澄清式多轮   —— 第一轮关键参数缺失，Agent 用 respond 提问；
     第二轮补充后继续完成

每个场景都把**真实请求体 + 真实响应体**写到 evidence 目录。

用法：python3 scripts/live_agent_check.py [场景名...]
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = os.environ.get("AGENT_BASE", "http://127.0.0.1:8199")
DATA = Path(os.environ.get("H3STUDIO_DATA_DIR", str(ROOT / "data-live")))
OUT = ROOT / "evidence" / "agent-enh"
OUT.mkdir(parents=True, exist_ok=True)

#: 请求超时（秒）。真实本地模型单步 20~120 秒，整条链路可能好几分钟。
TIMEOUT = 900


def post(path: str, payload: dict) -> dict:
    url = f"{BASE}{path}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    started = time.time()
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read().decode("utf-8")
    return {"status": resp.status, "elapsed_ms": int((time.time() - started) * 1000),
            "body": json.loads(raw) if raw else {}}


def get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=60) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def read_job(job_id: str) -> dict:
    """从**真实 SQLite** 读任务记录（不信任 API 响应）。"""
    db_path = DATA / "jobs.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row:
        return {}
    params = {}
    try:
        params = json.loads(row["params"] or "{}")
    except Exception:
        pass
    return {"id": row["id"], "mode": row["mode"], "status": row["status"],
            "created_at": row["created_at"], "params": params,
            "progress": row["progress"], "progress_text": row["progress_text"],
            "error": row["error"], "outputs": row["outputs"]}


def scenario_normal() -> dict:
    """场景 1：正常建任务。"""
    message = ("用素材库里的「雷神三视图 · 定妆照」做参考，"
               "生成一个 16:9 横屏、8 秒的文生视频，"
               "提示词：雷神手持战锤站在雪山之巅，风雪漫天。")
    req = {"message": message, "session_id": os.environ.get("SESS_NORMAL", "live-normal")}
    resp = post("/api/agent/chat", req)
    jobs = resp["body"].get("jobs") or []
    evidence = {
        "scenario": "正常建任务",
        "request": req,
        "response_status": resp["status"],
        "response_elapsed_ms": resp["elapsed_ms"],
        "response": resp["body"],
        "jobs_from_response": jobs,
        "jobs_from_sqlite": [read_job(j["job_id"]) for j in jobs],
    }
    (OUT / "live_scenario_1_normal.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    return evidence


def scenario_invalid_params() -> dict:
    """场景 2：参数非法被纠正（30 秒 + 21:10 比例）。"""
    message = ("做一个 30 秒、21:10 比例的竖屏视频，"
               "提示词：一只猫在雨中的霓虹街道上奔跑。")
    req = {"message": message, "session_id": os.environ.get("SESS_INVALID", "live-invalid")}
    resp = post("/api/agent/chat", req)
    jobs = resp["body"].get("jobs") or []
    steps = resp["body"].get("steps") or []
    evidence = {
        "scenario": "参数非法被纠正",
        "request": req,
        "response_status": resp["status"],
        "response_elapsed_ms": resp["elapsed_ms"],
        "response": resp["body"],
        "validation_errors_in_steps": [s for s in steps if s.get("error")],
        "jobs_from_response": jobs,
        "jobs_from_sqlite": [read_job(j["job_id"]) for j in jobs],
    }
    (OUT / "live_scenario_2_invalid_params.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    return evidence


def scenario_clarification() -> dict:
    """场景 3：澄清式多轮（第一轮缺关键参数 -> 提问 -> 第二轮续）。"""
    sid = os.environ.get("SESS_CLARIFY", "live-clarify")
    first_req = {"message": "帮我做个赛博朋克城市的视频", "session_id": sid}
    first = post("/api/agent/chat", first_req)

    # 把第一轮的提问告诉 Agent（模拟用户在界面上回答）
    answer = "9:16 竖屏，10 秒"
    second_req = {"message": answer, "session_id": sid}
    second = post("/api/agent/chat", second_req)

    jobs = second["body"].get("jobs") or []
    # 会话历史（证明第一轮的提问真的落库了）
    try:
        history = get(f"/api/agent/sessions/{sid}")
    except Exception as exc:
        history = {"error": str(exc)}
    evidence = {
        "scenario": "澄清式多轮",
        "turn1_request": first_req,
        "turn1_response": first["body"],
        "turn1_status": first["body"].get("status"),
        "turn1_clarification": first["body"].get("clarification"),
        "turn2_request": second_req,
        "turn2_response": second["body"],
        "turn2_status": second["body"].get("status"),
        "session_history": history,
        "jobs_from_response": jobs,
        "jobs_from_sqlite": [read_job(j["job_id"]) for j in jobs],
    }
    (OUT / "live_scenario_3_clarification.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    return evidence


def scenario_stream() -> dict:
    """
    附加：SSE 流式事件序列验证。

    刻意用一个**会真的建任务**的请求 —— 只有建了任务才会出现 `job` 事件，
    否则验证不到任务卡数据这一段（前端最关心的就是它）。
    """
    req = {"message": "用素材库里的「雷神三视图 · 定妆照」做参考，"
                      "生成一个 16:9、8 秒的文生视频，"
                      "提示词：雷神手持战锤站在雪山之巅。",
           "session_id": os.environ.get("SESS_STREAM", "live-stream"),
           "stream": 1}
    url = f"{BASE}/api/agent/chat"
    body = json.dumps(req, ensure_ascii=False).encode("utf-8")
    http_req = urllib.request.Request(url, data=body, method="POST",
                                      headers={"Content-Type": "application/json"})
    events = []
    raw_lines = []
    started = time.time()
    with urllib.request.urlopen(http_req, timeout=TIMEOUT) as resp:
        buf = ""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk.decode("utf-8", errors="replace")
            raw_lines.append(chunk.decode("utf-8", errors="replace"))
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                name = "message"
                data_lines = []
                for line in block.split("\n"):
                    if line.startswith("event:"):
                        name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                if not data_lines:
                    continue
                try:
                    data = json.loads("\n".join(data_lines))
                except Exception:
                    continue
                events.append({"event": name, "data": data})
    evidence = {
        "scenario": "SSE 流式事件序列",
        "request": req,
        "elapsed_ms": int((time.time() - started) * 1000),
        "event_names": [e["event"] for e in events],
        "event_count": len(events),
        "events": events,
    }
    (OUT / "live_scenario_4_stream_events.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    return evidence


SCENARIOS = {
    "normal": scenario_normal,
    "invalid": scenario_invalid_params,
    "clarification": scenario_clarification,
    "stream": scenario_stream,
}


def main():
    names = sys.argv[1:] or list(SCENARIOS)
    summary = {}
    for name in names:
        fn = SCENARIOS.get(name)
        if fn is None:
            print(f"未知场景 {name}，可选：{list(SCENARIOS)}")
            continue
        print(f"\n=== 场景：{name} ===", flush=True)
        try:
            evidence = fn()
            summary[name] = {
                "ok": True,
                "status": evidence.get("response_status")
                or evidence.get("turn2_status"),
                "jobs": len(evidence.get("jobs_from_response") or []),
                "elapsed_ms": evidence.get("response_elapsed_ms")
                or evidence.get("elapsed_ms"),
            }
            print(json.dumps(summary[name], ensure_ascii=False), flush=True)
        except Exception as exc:
            summary[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            print(json.dumps(summary[name], ensure_ascii=False), flush=True)
    (OUT / "live_scenario_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== 汇总 ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
