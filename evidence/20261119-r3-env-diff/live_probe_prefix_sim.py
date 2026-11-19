
import sys, os, json, time, sqlite3, asyncio, subprocess, uuid
os.chdir("/root/projects/h3studio-live")
sys.path.insert(0, "/root/projects/h3studio-live")
import server as srv

# ═══ 对照组：把 cancel_job_record 换成「修复前」的版本（无 longfilm 分支）═══
ORIG = srv.cancel_job_record
async def pre_fix_cancel(job_id: str) -> dict:
    """模拟 91f5060 的实现：只改 DB 状态，不终止 ffmpeg。"""
    async with srv.WORKER.submit_lock:
        with srv.db() as conn:
            r = conn.execute("SELECT status, prompt_id, mode FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not r:
            raise ValueError(f"任务不存在：{job_id}")
        if r["status"] not in ("running", "queued"):
            return {"job_id": job_id, "status": r["status"], "ok": True}
        pid = r["prompt_id"]
        if pid:
            # 原实现会向 ComfyUI 撤单；longfilm 没有 prompt_id，这里无事可做
            pass
        with srv.db() as conn:
            conn.execute("UPDATE jobs SET status='canceled', finished_at=? WHERE id=? AND status IN ('queued','running','failed')",
                         (time.time(), job_id))
            status = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"]
    return {"job_id": job_id, "status": status, "ok": True}
srv.cancel_job_record = pre_fix_cancel

async def main():
    DB = "/root/projects/comfyui-minimax-h3-studio/data/jobs.db"
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute("SELECT id, mode, outputs FROM jobs WHERE mode IN ('t2v','flf','r2v') AND status='done' ORDER BY rowid DESC LIMIT 400").fetchall()
    con.close()
    clips, seen = [], set()
    for jid, mode, outs in rows:
        try: o = json.loads(outs or "[]")
        except Exception: continue
        for x in o:
            f = x.get("file","")
            if f.startswith("videos/") and f not in seen:
                seen.add(f); clips.append({"job_id":jid,"file":f,"name":os.path.basename(f)})
    clips = clips[:60]
    job_id = uuid.uuid4().hex[:12]
    params = {"title":"probe","intro":"probe","clips":clips,"duration":None}
    with srv.db() as conn:
        conn.execute("INSERT INTO jobs (id, created_at, mode, params, status, progress_text) VALUES (?,?,?,?, 'queued', '等待合成')",
                     (job_id, time.time(), "longfilm", json.dumps(params, ensure_ascii=False)))
    task = asyncio.get_event_loop().create_task(srv.run_longfilm(job_id))
    proc = None
    for _ in range(80):
        await asyncio.sleep(0.25)
        proc = srv.LONGFILM_PROCESSES.get(job_id)
        if proc is not None:
            print(f"[probe] ffmpeg pid={proc.pid}")
            break
    if proc is None:
        print("[probe] ffmpeg 没起来"); return 2
    result = await srv.cancel_job_record(job_id)
    print(f"[probe] 修复前 cancel_job_record -> {json.dumps(result, ensure_ascii=False)}")
    await asyncio.sleep(1.5)
    alive = False
    try:
        os.kill(proc.pid, 0); alive = True
    except (ProcessLookupError, PermissionError):
        alive = False
    ps = subprocess.run(["bash","-c",f"ps -p {proc.pid} -o pid,cmd 2>/dev/null | tail -1"],capture_output=True,text=True).stdout.strip()
    print(f"[probe] 进程仍存在={alive}  ps: {ps or '(已消失)'}")
    # 清理：把残留的 ffmpeg 杀掉
    try: proc.kill()
    except Exception: pass
    task.cancel()
    print("[probe] RESULT:", "缺陷复现-进程仍在" if alive else "未复现")
    return 0
import sys as _s
_s.exit(asyncio.get_event_loop().run_until_complete(main()))
