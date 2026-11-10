# -*- coding: utf-8 -*-
"""
真实任务实跑 —— 用**真实 ComfyUI** 出一张图，验证 Agent 链路真的能跑通。

【为什么不用 mock】
本轮的前端验证脚本用 mock 模型（快、确定）。
但「Agent 建任务 -> ComfyUI 真的出图 -> 产物落库 -> 任务卡显示」
这条**完整链路**必须用真模型 + 真 ComfyUI 验一次 ——
否则只能证明「契约对齐」，证明不了「东西真的能生成」。

【与线上 8190 的关系】
**不碰 8190 线上服务，不碰任何 systemd unit。**
本脚本在**独立端口**（默认 8197）+ **独立 data_dir** 起自己的实例，
连的是**同一个 ComfyUI**（8188）—— ComfyUI 是共享资源，不是本轮的服务。

用法：
    python3 scripts/live_real_task.py [证据输出目录]
"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

OUT_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/agent-real-task")
OUT_DIR.mkdir(parents=True, exist_ok=True)

PORT = 8197
COMFYUI = "http://127.0.0.1:8188"


def free_port(preferred: int) -> int:
    for port in (preferred, 0):
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
        except OSError:
            continue
    raise RuntimeError("找不到空闲端口")


class Studio:
    """独立端口 + 独立 data_dir 的 Studio 实例（真实 server.py）。"""

    def __init__(self, port: int):
        self.port = port
        self.tmp = Path(tempfile.mkdtemp(prefix="h3studio-realtask-"))
        self.data = self.tmp / "data"
        self.proc = None
        self.base = f"http://127.0.0.1:{port}"

    def _copy_providers(self):
        """把线上实例的 Provider 配置**复制**到本实例（只读，不改线上）。

        【为什么不自己造一个 Provider】
        本地模型 Provider 的配置（base_url / model / api_key）只存在于
        线上实例的 jobs.db 里。Agent 必须连真实的本地模型才能跑。
        这里**只读**线上库，把 provider 行 INSERT 到本实例的库 ——
        线上库**一个字都不改**。
        """
        live_db = Path("/root/projects/comfyui-minimax-h3-studio/data/jobs.db")
        if not live_db.exists():
            return False
        src = sqlite3.connect(f"file:{live_db}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        try:
            rows = src.execute(
                "SELECT * FROM model_providers WHERE enabled=1").fetchall()
            prefs = src.execute(
                "SELECT * FROM model_provider_prefs").fetchall()
        finally:
            src.close()
        if not rows:
            return False
        dst_path = self.data / "jobs.db"
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(dst_path)
        dst.row_factory = sqlite3.Row
        # 表可能还不存在（服务端启动时才建）—— 这里自己建一份
        # （字段与 server.init_db() 的 schema 一致）。
        dst.executescript(
            """
            CREATE TABLE IF NOT EXISTS model_providers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
                base_url TEXT NOT NULL, llm_base_url TEXT DEFAULT '',
                use_proxy INTEGER DEFAULT 0, proxy_base_url TEXT DEFAULT '',
                model TEXT NOT NULL, api_key TEXT DEFAULT '',
                multimodal INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1,
                created_at REAL);
            CREATE TABLE IF NOT EXISTS model_provider_prefs (
                key TEXT PRIMARY KEY, value TEXT);
            """)
        cols = [d[1] for d in dst.execute(
            "PRAGMA table_info(model_providers)").fetchall()]
        for row in rows:
            data = {c: row[c] for c in cols if c in row.keys()}
            names = ",".join(cols)
            marks = ",".join("?" for _ in cols)
            dst.execute(f"INSERT OR REPLACE INTO model_providers ({names}) "
                        f"VALUES ({marks})", [data[c] for c in cols])
        for row in prefs:
            dst.execute("INSERT OR REPLACE INTO model_provider_prefs "
                        "(key, value) VALUES (?,?)",
                        (row["key"], row["value"]))
        dst.commit()
        dst.close()
        print(f"== 已从线上库**只读复制** {len(rows)} 个 Provider ==")
        return True

    def start(self):
        shutil.copytree(REPO / "h3studio", self.tmp / "h3studio")
        shutil.copy2(REPO / "server.py", self.tmp / "server.py")
        shutil.copytree(REPO / "web", self.tmp / "web")
        cfg = {
            "host": "127.0.0.1", "port": self.port,
            "comfyui_url": COMFYUI,
            "data_dir": str(self.data),
            "models_dir": "/home/ubuntu/comfyui/models",
            "poll_interval": 1.5,
        }
        (self.tmp / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        env = dict(os.environ)
        env["H3STUDIO_DATA_DIR"] = str(self.data)
        env["AGENT_SESSION_DB"] = str(self.data / "agent_sessions.db")
        env["AGENT_MEMORY_DB"] = str(self.data / "agent_memory.db")
        env["PYTHONPATH"] = str(self.tmp)
        self._copy_providers()
        self.proc = subprocess.Popen(
            [sys.executable, "server.py"], cwd=str(self.tmp), env=env,
            stdout=open(OUT_DIR / "server_stdout.log", "w", encoding="utf-8"),
            stderr=subprocess.STDOUT)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), 1):
                    return True
            except OSError:
                time.sleep(0.3)
        return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read_jobs(self):
        path = self.data / "jobs.db"
        if not path.exists():
            return []
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, mode, status, params, outputs, error, progress, "
            "progress_text, created_at, finished_at FROM jobs "
            "ORDER BY created_at DESC").fetchall()
        conn.close()
        out = []
        for row in rows:
            try:
                params = json.loads(row["params"] or "{}")
            except Exception:
                params = {}
            try:
                outputs = json.loads(row["outputs"] or "[]")
            except Exception:
                outputs = []
            out.append({
                "id": row["id"], "mode": row["mode"], "status": row["status"],
                "params": params, "outputs": outputs, "error": row["error"],
                "progress": row["progress"], "progress_text": row["progress_text"],
                "created_at": row["created_at"], "finished_at": row["finished_at"],
            })
        return out

    def read_sessions(self):
        path = self.data / "agent_sessions.db"
        if not path.exists():
            return []
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT session_id, role, content FROM agent_messages "
                "ORDER BY id").fetchall()
        except Exception:
            rows = []
        conn.close()
        return [{"session_id": r["session_id"], "role": r["role"],
                 "content": str(r["content"])[:600]} for r in rows]


def main():
    import urllib.error
    import urllib.request

    # ---- 1. 起独立实例 ----
    port = free_port(PORT)
    studio = Studio(port)
    if not studio.start():
        print("!! Studio 启动失败")
        print((OUT_DIR / "server_stdout.log").read_text()[-3000:])
        studio.stop()
        return 1
    print(f"== 独立实例：{studio.base}（data_dir={studio.data}）==")

    result = {}
    try:
        # ---- 2. 找可用的本地模型 Provider ----
        with urllib.request.urlopen(studio.base + "/api/providers", timeout=30) as r:
            providers = json.loads(r.read().decode())
        enabled = [p for p in providers.get("providers", [])
                   if p.get("enabled") and p.get("multimodal")]
        if not enabled:
            enabled = [p for p in providers.get("providers", []) if p.get("enabled")]
        print(f"== 可用 Provider：{[p['id'] for p in enabled]} ==")
        result["providers"] = [
            {"id": p["id"], "name": p.get("name"), "model": p.get("model"),
             "multimodal": p.get("multimodal")} for p in enabled]
        if not enabled:
            print("!! 没有可用 Provider")
            studio.stop()
            return 1
        provider_id = enabled[0]["id"]

        # ---- 3. 真跑一次 Agent（一次性 JSON，避免 SSE 读超时）----
        message = ("用 Z-IMAGE 文生图模式生成一张 16:9 的图："
                   "一只橘猫坐在洒满阳光的木窗台上，暖色调，浅景深，"
                   "胶片颗粒感，高细节")
        payload = json.dumps({"message": message,
                              "provider_id": provider_id}, ensure_ascii=False).encode()
        req = urllib.request.Request(
            studio.base + "/api/agent/chat", data=payload, method="POST",
            headers={"Content-Type": "application/json"})
        print(f"== 发请求（真实模型，可能要几分钟）==")
        started = time.time()
        with urllib.request.urlopen(req, timeout=1200) as r:
            raw = r.read().decode("utf-8", errors="replace")
            ct = r.headers.get("Content-Type", "")
        elapsed = int((time.time() - started) * 1000)
        body = json.loads(raw)
        print(f"   耗时 {elapsed}ms  ct={ct}")
        print(f"   status={body.get('status')}")
        print(f"   answer={str(body.get('answer'))[:200]}")
        result["agent_response"] = {
            "elapsed_ms": elapsed, "content_type": ct,
            "status": body.get("status"), "answer": body.get("answer"),
            "jobs": body.get("jobs"), "steps": body.get("steps"),
            "error": body.get("error"), "code": body.get("code"),
        }
        job_ids = [j.get("job_id") for j in (body.get("jobs") or [])]
        print(f"   job_ids={job_ids}")

        # ---- 4. 等任务真的跑到终态 ----
        print("== 等 ComfyUI 出图 ==")
        final = None
        deadline = time.time() + 900
        while time.time() < deadline:
            with urllib.request.urlopen(
                    studio.base + f"/api/jobs/{job_ids[0]}", timeout=30) as r:
                job = json.loads(r.read().decode())
            print(f"   status={job.get('status')} "
                  f"progress={job.get('progress')} "
                  f"{job.get('progress_text') or ''}")
            if job.get("status") in ("done", "failed", "canceled"):
                final = job
                break
            time.sleep(6)
        result["final_job"] = final

        # ---- 5. 读真实 SQLite ----
        result["sqlite_jobs"] = studio.read_jobs()
        result["sqlite_sessions"] = studio.read_sessions()
        (OUT_DIR / "live_real_task.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        # ---- 6. 断言 ----
        failures = []
        if body.get("status") != "completed":
            failures.append(f"Agent 状态不是 completed：{body.get('status')} "
                            f"error={body.get('error')}")
        if not job_ids:
            failures.append("没有创建任何任务")
        elif not final:
            failures.append("任务没跑到终态（超时）")
        else:
            if final.get("status") != "done":
                failures.append(f"任务终态不是 done：{final.get('status')} "
                                f"error={final.get('error')}")
            if not final.get("outputs"):
                failures.append("任务没有产物 outputs")
            else:
                print(f"   产物：{final['outputs']}")
        if not result["sqlite_jobs"]:
            failures.append("SQLite 里没有任务记录")
        if not result["sqlite_sessions"]:
            failures.append("SQLite 里没有会话记录")

        print("\n===== 断言结果 =====")
        if failures:
            for f in failures:
                print("FAIL", f)
        else:
            print("全部通过（真实模型建任务 -> ComfyUI 出图 -> 产物落库 -> SQLite 有记录）")
        print(f"\n证据目录：{OUT_DIR}")
        return 1 if failures else 0
    finally:
        studio.stop()


if __name__ == "__main__":
    sys.exit(main())
