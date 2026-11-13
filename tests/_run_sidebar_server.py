#!/usr/bin/env python3
"""起一个临时端口的 Studio 实例（供验收脚本用），保持常驻直到被 kill。

用法: python3 tests/_run_sidebar_server.py <port>
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


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else free_port()
    tmp = Path(tempfile.mkdtemp(prefix="h3studio-sidebar-"))
    data_dir = tmp / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    shutil.copytree(REPO / "h3studio", tmp / "h3studio")
    shutil.copy2(REPO / "server.py", tmp / "server.py")
    shutil.copytree(REPO / "web", tmp / "web")

    db_path = data_dir / "jobs.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, created_at REAL, mode TEXT, params TEXT,
            status TEXT, prompt_id TEXT, progress REAL DEFAULT 0,
            progress_text TEXT, error TEXT, outputs TEXT,
            started_at REAL, finished_at REAL
        );
        CREATE TABLE IF NOT EXISTS job_batches (
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL, idx INTEGER NOT NULL,
            seed INTEGER, status TEXT, prompt_id TEXT, progress REAL DEFAULT 0,
            progress_text TEXT, error TEXT, outputs TEXT,
            created_at REAL, started_at REAL, finished_at REAL
        );
    """)
    now = time.time()
    modes = ["t2i", "t2v", "flf", "r2v", "longfilm"]
    statuses = ["done", "done", "done", "failed", "canceled"]
    for i in range(25):
        conn.execute(
            "INSERT INTO jobs (id, created_at, mode, params, status, progress,"
            " progress_text, outputs) VALUES (?,?,?,?,?,?,?,?)",
            (f"seed{i:04d}", now - i * 60, modes[i % 5],
             json.dumps({"title": f"种子任务 {i}", "aspect": "16:9",
                         "prompt": f"seed prompt {i}"}, ensure_ascii=False),
             statuses[i % 5], 1.0 if statuses[i % 5] == "done" else 0.0,
             "", "[]"))
    conn.commit()
    conn.close()

    cfg = {
        "host": "127.0.0.1",
        "port": port,
        "comfyui_url": "http://127.0.0.1:8188",
        "data_dir": str(data_dir),
        "models_dir": "/home/ubuntu/comfyui/models",
        "poll_interval": 1.5,
    }
    (tmp / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    proc = subprocess.Popen([sys.executable, "server.py"], cwd=str(tmp),
                            stdout=open("/tmp/h3sidebar_server.log", "w"),
                            stderr=subprocess.STDOUT)
    Path("/tmp/h3sidebar_server.pid").write_text(str(proc.pid))
    Path("/tmp/h3sidebar_server.tmp").write_text(str(tmp))
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                print(f"READY port={port} pid={proc.pid} tmp={tmp}")
                return 0
        except OSError:
            time.sleep(0.2)
    print("FAILED to start")
    return 1


if __name__ == "__main__":
    sys.exit(main())
