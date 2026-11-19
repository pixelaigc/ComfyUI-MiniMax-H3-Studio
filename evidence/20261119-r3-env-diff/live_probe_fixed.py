
import sys, os, json, time, sqlite3, asyncio, subprocess, uuid
os.chdir("/root/projects/h3studio-live")
sys.path.insert(0, "/root/projects/h3studio-live")
import server as srv

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
    print(f"[probe] clips={len(clips)}")

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
            print(f"[probe] ffmpeg 已进 LONGFILM_PROCESSES: pid={proc.pid}")
            break
    if proc is None:
        print("[probe] ffmpeg 没起来"); return 2

    result = await srv.cancel_job_record(job_id)
    print(f"[probe] cancel_job_record -> {json.dumps(result, ensure_ascii=False)}")
    await asyncio.sleep(1.5)

    rc = proc.returncode
    alive = False
    try:
        os.kill(proc.pid, 0); alive = True
    except (ProcessLookupError, PermissionError):
        alive = False
    ps = subprocess.run(["bash","-c",f"ps -p {proc.pid} -o pid,cmd 2>/dev/null | tail -1"],capture_output=True,text=True).stdout.strip()
    print(f"[probe] ffmpeg returncode={rc} 进程仍存在={alive}")
    print(f"[probe] ps: {ps or '(已消失)'}")
    task.cancel()
    print("[probe] RESULT:", "PASS-进程已终止" if not alive else "FAIL-进程还在")
    return 0 if not alive else 3

import sys as _s
_s.exit(asyncio.get_event_loop().run_until_complete(main()))
