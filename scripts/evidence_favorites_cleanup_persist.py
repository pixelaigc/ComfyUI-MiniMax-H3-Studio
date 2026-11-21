"""④ 清理跳过收藏（全部清理路径）+ ⑤ 取消收藏 + 持久化（重启后仍在）。

【为什么单独一支】上一支 abcdef 里 ⑤ 没跑到（收藏视图里找不到「未收藏」的卡，
因为收藏视图只显示收藏过的卡 —— 这是设计如此，不是缺陷）。
④ 也只做了预览，没真的执行清理。

【这一支做什么】
  ④ A) 三条清理路径的**预览**都报告 skipped_favorited
     B) 真执行一次「按时间」清理 ⇒ 收藏项仍在、计数正确、产物文件仍在
     C) 真执行一次「全部」清理 ⇒ 收藏项仍在
  ⑤ 在**媒体库「全部」标签**（不是收藏视图）里右键一条**已收藏**的卡
     → 菜单显示「取消收藏」→ 点它 → DB 少一条 → 该卡重新纳入清理范围
  P) 重启 h3-studio.service 后，收藏仍在（落库 + 重启双重取证）

【硬约束】不删除任何产物文件（清理会跳过收藏）；不新增生成作业。
"""
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path("/root/projects/h3s-favorites/evidence/favorites-cleanup-persist")
OUT.mkdir(parents=True, exist_ok=True)
BASE = "http://127.0.0.1:8190"
DB = "/root/projects/comfyui-minimax-h3-studio/data/jobs.db"
DATA = Path("/root/projects/comfyui-minimax-h3-studio/data")

facts = {}


def api(path, method="GET", body=None):
    cmd = ["curl", "-s", "-m", "120", "-X", method, BASE + path]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    try:
        return json.loads(out)
    except Exception:
        return {"raw": out[:400]}


def fav_ids():
    c = sqlite3.connect(DB)
    ids = [r[0] for r in c.execute("SELECT job_id FROM job_favorites")]
    c.close()
    return ids


def fav_files_on_disk():
    """收藏项对应的产物文件是否还在磁盘上（清理不能删它们）。

    【数据结构】文件不在单独的 job_files 表里，而是 `jobs.outputs` 那一列
    的 JSON 数组：'[{"kind":"video","file":"videos/xxx.mp4",...}]'。
    """
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    out = {}
    for r in c.execute("SELECT job_id FROM job_favorites"):
        jid = r["job_id"]
        files = []
        row = c.execute("SELECT outputs FROM jobs WHERE id=?", (jid,)).fetchone()
        if row and row["outputs"]:
            try:
                outs = json.loads(row["outputs"])
            except (TypeError, ValueError):
                outs = []
            for o in outs:
                fp = DATA / "outputs" / o.get("file", "")
                files.append({"file": o.get("file"), "kind": o.get("kind"),
                              "exists": fp.exists(),
                              "size": fp.stat().st_size if fp.exists() else 0})
        out[jid] = files
    c.close()
    return out


def run_isolated_cleanup(prod_fav_ids):
    """在**隔离实例**上真跑清理，证明「已收藏的被跳过」。

    【为什么要隔离】真实清理会删文件。生产库上 scope=all&months=0 的预览是
    「会删 400 jobs / 473 files」—— 那是用户的全部媒体库，绝不能碰。

    【隔离怎么做】复制 DB + outputs 到临时目录，用独立端口起一个 server.py，
    config.json 指到临时 data_dir ⇒ 与生产完全隔离。
    （和 tests/test_mobile_nav_regression_guards.py 的 live_server fixture 同思路。）
    """
    import shutil
    import socket
    import tempfile

    REPO = Path("/root/projects/h3s-favorites")
    tmp = Path(tempfile.mkdtemp(prefix="h3s-favcleanup-"))
    out = {"tmp_dir": str(tmp)}
    proc = None
    try:
        # 1) 复制 DB 与产物文件
        for db in DATA.glob("*.db"):
            shutil.copy2(db, tmp / db.name)
        src_out = DATA / "outputs"
        dst_out = tmp / "outputs"
        if src_out.exists():
            shutil.copytree(src_out, dst_out, symlinks=True)
        # 2) 写独立 config.json
        cfg_src = REPO / "config.json"
        cfg = json.loads(cfg_src.read_text(encoding="utf-8")) if cfg_src.exists() else {}
        cfg["data_dir"] = str(tmp)
        (tmp / "config.json").write_text(json.dumps(cfg, ensure_ascii=False),
                                         encoding="utf-8")
        # 3) 起实例
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        env = dict(os.environ)
        env["H3STUDIO_PORT"] = str(port)
        env["PYTHONPATH"] = str(REPO)
        proc = subprocess.Popen(
            ["/home/ubuntu/comfyui/venv/bin/python", "server.py"], cwd=str(REPO),
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{port}"

        def iso_api(path, method="GET", body=None):
            cmd = ["curl", "-s", "-m", "120", "-X", method, base + path]
            if body is not None:
                cmd += ["-H", "Content-Type: application/json",
                        "-d", json.dumps(body)]
            raw = subprocess.run(cmd, capture_output=True, text=True).stdout
            try:
                return json.loads(raw)
            except Exception:
                return {"raw": raw[:300]}

        ready = False
        for _ in range(120):
            code = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-m", "10", "-w", "%{http_code}",
                 base + "/api/version"], capture_output=True, text=True).stdout.strip()
            if code == "200":
                ready = True
                break
            time.sleep(1)
        out["instance_ready"] = ready
        if not ready:
            out["error"] = "隔离实例起不来"
            return out

        # 4) 隔离库里的收藏集合
        def iso_fav_ids():
            c = sqlite3.connect(tmp / "jobs.db")
            ids = [r[0] for r in c.execute("SELECT job_id FROM job_favorites")]
            c.close()
            return set(ids)

        def iso_fav_files():
            c = sqlite3.connect(tmp / "jobs.db")
            c.row_factory = sqlite3.Row
            res = {}
            for r in c.execute("SELECT job_id FROM job_favorites"):
                files = []
                row = c.execute("SELECT outputs FROM jobs WHERE id=?",
                                (r["job_id"],)).fetchone()
                if row and row["outputs"]:
                    try:
                        outs = json.loads(row["outputs"])
                    except (TypeError, ValueError):
                        outs = []
                    for o in outs:
                        fp = tmp / "outputs" / o.get("file", "")
                        files.append({"file": o.get("file"), "exists": fp.exists()})
                res[r["job_id"]] = files
            c.close()
            return res

        ids_before = iso_fav_ids()
        files_before = iso_fav_files()
        out["iso_db_favorites_before"] = len(ids_before)
        out["iso_files_before"] = sum(len(v) for v in files_before.values())

        # 5) 三条路径：先预览再执行
        for scope, months, tag in (("all", 1, "all_1m"),
                                    ("all", 0, "all_0m"),
                                    ("failed", 6, "failed_6m")):
            prev = iso_api(f"/api/media/manage?scope={scope}&months={months}")
            res = iso_api("/api/media/manage", "POST",
                          {"scope": scope, "months": months, "confirm": True})
            ids_after = iso_fav_ids()
            files_after = iso_fav_files()
            out[tag] = {
                "preview": {k: prev.get(k) for k in ("jobs", "files",
                                                     "skipped_favorited")},
                "execute": {k: res.get(k) for k in ("jobs_removed", "files_removed",
                                                   "skipped_favorited", "removed")
                            if k in res},
                "execute_raw_keys": list(res.keys())[:12],
                "favorites_before": len(ids_before),
                "favorites_after": len(ids_after),
                "favorites_lost": sorted(ids_before - ids_after),
                "fav_files_missing_after": sum(
                    1 for v in files_after.values() for f in v if not f["exists"]),
            }
            ids_before = ids_after
            files_before = files_after

        # 6) 取消收藏后应重新纳入清理范围
        if ids_before:
            victim = sorted(ids_before)[0]
            iso_api(f"/api/jobs/{victim}/favorite", "POST", {"favorited": False})
            ids_now = iso_fav_ids()
            prev = iso_api("/api/media/manage?scope=all&months=0")
            out["after_unfavorite_reenters_cleanup"] = {
                "victim": victim,
                "still_favorited": victim in ids_now,
                "preview_jobs_all_0m": prev.get("jobs"),
                "skipped_favorited_after": prev.get("skipped_favorited"),
            }
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        shutil.rmtree(tmp, ignore_errors=True)
    return out


# ================= ④ 三条清理路径的预览（生产库，只读不删）====================
# 【重要】生产库上**绝不执行真实清理**：
#   scope=all & months=0 的预览是「会删 400 jobs / 473 files」——
#   那就是用户的全部媒体库。需求里明确「不得删除用户已有的生成产物」。
#   ⇒ 生产库只做**预览**（preview 不删任何东西），证明 skip 逻辑生效；
#     真实清理在**隔离实例**（DB 副本 + 独立端口）上做。
facts["04_previews"] = {}
for scope, months in (("all", 1), ("older", 3), ("failed", 6)):
    r = api(f"/api/media/manage?scope={scope}&months={months}")
    facts["04_previews"][scope] = {
        "jobs": r.get("jobs"), "files": r.get("files"),
        "skipped_favorited": r.get("skipped_favorited"),
    }
# 最激进的路径也只预览，不执行
facts["04_preview_all_0m"] = api("/api/media/manage?scope=all&months=0")
facts["04_preview_all_0m"] = {k: facts["04_preview_all_0m"].get(k) for k in
                               ("jobs", "files", "skipped_favorited")}

before_ids = set(fav_ids())
before_files = fav_files_on_disk()
facts["04_db_before"] = len(before_ids)
facts["04_files_before"] = sum(len(v) for v in before_files.values())
facts["04_missing_before"] = sum(
    1 for v in before_files.values() for f in v if not f["exists"])

# ================= ④ B/C) 真实清理 —— 在**隔离实例**上做 =================
# 【为什么要隔离】真实清理会**删文件**。生产库不能碰。
#   隔离实例 = 复制一份 DB 到临时目录 + 独立端口 + 独立 outputs 目录
#   （和 tests/test_mobile_nav_regression_guards.py 的 live_server fixture 同思路）。
facts["04_isolated"] = run_isolated_cleanup(before_ids)

# ================= ⑤ 在「全部」标签里取消收藏 =================
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True, args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
    p = ctx.new_page()

    def _on_dialog(d):
        facts.setdefault("dialogs", []).append(d.message[:160])
        d.accept()

    p.on("dialog", _on_dialog)
    p.goto(BASE + "/", wait_until="domcontentloaded", timeout=90000)
    p.wait_for_selector("#jobsGrid .job-card[data-jid]", timeout=60000)
    p.wait_for_timeout(2500)

    # 切到「全部」标签（收藏视图里只有收藏过的卡，找不到未收藏的）
    p.evaluate("""()=>{const b=document.querySelector('.jobs-tabs button[data-st="all"]');
                    if(b) b.click();}""")
    p.wait_for_timeout(2500)
    facts["05_tab"] = p.evaluate(
        """()=>(document.querySelector('.jobs-tabs button[data-st].active')||{}).dataset?.st""")

    # 找一条**已收藏**的卡
    target = p.evaluate(
        """()=>{for(const c of document.querySelectorAll('#jobsGrid .job-card[data-jid]')){
             if(c.querySelector('.job-fav-badge')) return c.dataset.jid;}
           return null;}""")
    facts["05_target_favorited_card"] = target
    if target:
        p.evaluate("(jid)=>{const c=document.querySelector('.job-card[data-jid=\"'+jid+'\"]');"
                   " const t=c.querySelector('.job-preview img, .job-preview video,"
                   " .job-preview audio');"
                   " if(t) t.scrollIntoView({block:'center'});}", target)
        p.wait_for_timeout(600)
        t2 = p.query_selector(f'.job-card[data-jid="{target}"] .job-preview img, '
                              f'.job-card[data-jid="{target}"] .job-preview video, '
                              f'.job-card[data-jid="{target}"] .job-preview audio')
        if t2:
            bb = t2.bounding_box()
            p.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2,
                          button="right")
            p.wait_for_timeout(900)
            facts["05_menu_items"] = p.evaluate(
                """()=>{const m=document.querySelector('#mediaContext');
                   return {items:Array.from(m.querySelectorAll('button[data-act]'))
                             .map(e=>({act:e.dataset.act,
                                       text:String(e.textContent||'').replace(/\\s+/g,' ').trim()}))};}""")
            p.screenshot(path=str(OUT / "05-1-right-click-shows-unfavorite.png"))
            btn = p.query_selector('#mediaContext button[data-act="favorite"]')
            if btn:
                btn.click()
                p.wait_for_timeout(2500)
        facts["05_db_has_target_after"] = target in fav_ids()
        p.screenshot(path=str(OUT / "05-2-after-unfavorite.png"))
    p.keyboard.press("Escape")
    ctx.close()
    b.close()

facts["05_db_count"] = len(fav_ids())

# ================= P) 重启后仍在 =================
pre_restart = sorted(fav_ids())
subprocess.run(["systemctl", "restart", "h3-studio.service"], capture_output=True)
time.sleep(20)
for _ in range(40):
    code = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-m", "60", "-w", "%{http_code}",
         BASE + "/api/jobs/favorites"],
        capture_output=True, text=True).stdout.strip()
    if code == "200":
        break
    time.sleep(3)
post_restart = sorted(fav_ids())
api_after = api("/api/jobs/favorites?page=1&page_size=50")
facts["P_persistence"] = {
    "service_active": subprocess.run(["systemctl", "is-active", "h3-studio.service"],
                                     capture_output=True, text=True).stdout.strip(),
    "nrestarts": subprocess.run(
        ["systemctl", "show", "h3-studio.service", "-p", "NRestarts", "--value"],
        capture_output=True, text=True).stdout.strip(),
    "db_before_restart": len(pre_restart),
    "db_after_restart": len(post_restart),
    "same_set": pre_restart == post_restart,
    "api_total_after_restart": api_after.get("total"),
}
# 重启后收藏项的产物文件仍在？
files3 = fav_files_on_disk()
facts["P_persistence"]["files_missing_after_restart"] = sum(
    1 for v in files3.values() for f in v if not f["exists"])
facts["P_persistence"]["files_total_after_restart"] = sum(
    len(v) for v in files3.values())

json.dump(facts, open(OUT / "cleanup-persist-facts.json", "w"),
          ensure_ascii=False, indent=1)

for k, v in facts.items():
    print("  %-38s %s" % (k, json.dumps(v, ensure_ascii=False)[:240]))
print("  证据目录:", OUT)
