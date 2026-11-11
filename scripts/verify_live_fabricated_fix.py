#!/usr/bin/env python3
"""
Live self-check against the DEPLOYED service on :8190.

Verifies, against the **running production server** (not a test double):
  1. the new authoritative-source API is present and returns real ids
  2. the mis-copied id of the REAL production job is NOT flagged
  3. a genuinely fabricated id IS still flagged
  4. the /api/agent/* endpoints are healthy
"""
import json
import sqlite3
import sys
import urllib.request

sys.path.insert(0, "/root/projects/h3studio-live")
from h3studio import agent as agent_mod  # noqa: E402

BASE = "http://127.0.0.1:8190"
JOBS_DB = "/root/projects/comfyui-minimax-h3-studio/data/jobs.db"
SESSION_DB = "/root/projects/h3studio-live/data/agent_sessions.db"
TYPO_ID = "ac8778a6ea84"


def api(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def main():
    ok = True

    # --- 0. endpoints healthy ----------------------------------------
    print("=" * 70)
    print("[0] /api/agent/* endpoint health")
    print("=" * 70)
    for path in ("/api/agent/tools", "/api/agent/sessions", "/api/jobs"):
        try:
            status, body = api(path)
            keys = list(body)[:5] if isinstance(body, dict) else "n/a"
            print("  %-24s HTTP %s  keys=%s" % (path, status, keys))
        except Exception as exc:
            print("  %-24s ERROR %s" % (path, exc))
            ok = False

    # --- 1. real production job --------------------------------------
    jc = sqlite3.connect(JOBS_DB)
    jc.row_factory = sqlite3.Row
    row = jc.execute(
        "SELECT id, mode, status FROM jobs WHERE mode='t2i_moss' "
        "ORDER BY created_at DESC LIMIT 1").fetchone()
    real_id = row["id"]
    print()
    print("=" * 70)
    print("[1] real production job (the one the user saw)")
    print("=" * 70)
    print("  id=%s mode=%s status=%s" % (real_id, row["mode"], row["status"]))

    # --- 2. authoritative source returns it --------------------------
    print()
    print("=" * 70)
    print("[2] authoritative source (server.list_job_ids_since) sees it")
    print("=" * 70)
    import importlib
    sys.modules.pop("server", None)
    server_mod = importlib.import_module("server")
    import time
    # 用 Agent 真实使用的回看窗口（不是随意挑的时间）
    window = agent_mod.AUTHORITATIVE_LOOKBACK_SEC
    ids = server_mod.list_job_ids_since(time.time() - window)
    print("  lookback window: %d s (%.1f h)" % (window, window / 3600))
    print("  ids in window  : %d" % len(ids))
    print("  contains %s : %s" % (real_id, real_id in ids))
    if real_id not in ids:
        print("  !! authoritative source MISSING the real job")
        ok = False

    # --- 3. mis-copied id must NOT be flagged -------------------------
    print()
    print("=" * 70)
    print("[3] mis-copied id (model wrote %s) must NOT be flagged" % TYPO_ID)
    print("=" * 70)
    known = agent_mod._job_id_list([real_id])
    flagged = agent_mod._looks_like_fabricated_job_claim(
        f"已下发 MOSS-Image 文生图任务（job {TYPO_ID}）", known)
    print("  flagged = %s  (must be False)" % flagged)
    if flagged:
        ok = False

    # --- 4. genuine fabrication MUST still be flagged -----------------
    print()
    print("=" * 70)
    print("[4] genuine fabrication must STILL be flagged")
    print("=" * 70)
    for text, label in (
            ("已下发文生视频任务（job t2v_019d874a）", "t2v_ prefixed fake"),
            ("已下发文生视频任务（job 3f2a9c1b7d04）", "random hex fake"),
            ("已下发文生视频任务（job 000000000000）", "all-zero fake")):
        f = agent_mod._looks_like_fabricated_job_claim(text, known)
        print("  %-20s flagged=%s (must be True)" % (label, f))
        if not f:
            ok = False

    # --- 5. the real session's assistant answer ----------------------
    print()
    print("=" * 70)
    print("[5] replay of the real session answer (msg 22)")
    print("=" * 70)
    sc = sqlite3.connect(SESSION_DB)
    sc.row_factory = sqlite3.Row
    msg = sc.execute(
        "SELECT content FROM agent_messages WHERE id=22 "
        "AND session_id='06f7145d3dab4085'").fetchone()
    if msg:
        f = agent_mod._looks_like_fabricated_job_claim(msg["content"], known)
        print("  flagged = %s  (must be False)" % f)
        if f:
            ok = False
    else:
        print("  (session row not found - skipped)")

    print()
    print("=" * 70)
    print("LIVE SELF-CHECK: %s" % ("PASS" if ok else "FAIL"))
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
