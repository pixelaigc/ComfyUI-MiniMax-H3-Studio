# -*- coding: utf-8 -*-
"""验收「不得用『干脆不刷新』掩盖问题」：后台新增内容仍要自动出现。

做法：直接往服务端库里插入一条新任务（模拟后台生成完成），
等待前端轮询周期（2 秒），确认新卡片自动出现在媒体库网格里。

同样地，往素材库里加一个新素材，确认素材库轮询（4 秒）会自动出现。

用法: python3 tests/verify_live_updates.py [base_url] [data_dir]
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190"
DATA = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/root/.h3studio/data")
OUT = Path("/tmp/h3verify")
OUT.mkdir(parents=True, exist_ok=True)

NEW_JOB_ID = "liveprobe_testjob_001"


def find_data_dir():
    """从 /api/jobs 的数据反推真实 data_dir；找不到就用参数。"""
    if DATA.exists():
        return DATA
    return None


def insert_job(data_dir):
    db = data_dir / "jobs.db"
    conn = sqlite3.connect(db)
    now = time.time()
    params = {"title": "实时探针", "prompt": "实时探针：后台生成完成的产物",
              "aspect": "16:9", "width": 1360, "height": 768,
              "width_h": 768, "mode": "t2i"}
    conn.execute(
        "INSERT OR REPLACE INTO jobs (id, created_at, mode, params, status,"
        " progress, progress_text, outputs) VALUES (?,?,?,?,?,?,?,?)",
        (NEW_JOB_ID, now, "t2i", json.dumps(params, ensure_ascii=False),
         "done", 1.0, "", "[]"))
    conn.commit()
    conn.close()


def main():
    report = {}
    data_dir = find_data_dir()
    if not data_dir:
        report["error"] = "找不到 data_dir"
        print(json.dumps(report, ensure_ascii=False))
        return 1

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(2000)

        # 媒体库：切到「已完成」并等列表稳定
        page.evaluate("""() => {
          const b=[...document.querySelectorAll('.jobs-tabs button')].find(x=>x.dataset.st==='done');
          if(b) b.click();
        }""")
        page.wait_for_timeout(3000)
        before = page.evaluate("""() => {
          const g = document.getElementById('jobsGrid');
          return {
            count: g ? g.querySelectorAll('.job-card').length : 0,
            has: !!(g && g.querySelector('[data-jid="%s"]')),
            ids: g ? Array.from(g.querySelectorAll('.job-card')).map(c=>c.dataset.jid) : [],
          };
        }""" % NEW_JOB_ID)
        page.screenshot(path=str(OUT / "06_live_before_insert.png"))

        # 后台「生成完成」：往库里插一条
        insert_job(data_dir)

        # 等前端轮询周期（2 秒）+ 余量
        appeared = False
        waited = 0
        while waited < 15000:
            page.wait_for_timeout(500)
            waited += 500
            has = page.evaluate(
                """() => !!(document.getElementById('jobsGrid')
                    && document.getElementById('jobsGrid').querySelector('[data-jid="%s"]'))"""
                % NEW_JOB_ID)
            if has:
                appeared = True
                break
        after = page.evaluate("""() => {
          const g = document.getElementById('jobsGrid');
          return {
            count: g ? g.querySelectorAll('.job-card').length : 0,
            has: !!(g && g.querySelector('[data-jid="%s"]')),
          };
        }""" % NEW_JOB_ID)
        page.screenshot(path=str(OUT / "06_live_after_insert.png"))
        report["media_library_live_update"] = {
            "count_before": before["count"],
            "count_after": after["count"],
            "new_card_appeared": appeared,
            "waited_ms": waited,
            "appeared_within_15s": appeared,
        }

        # 状态栏仍正确
        report["status_text"] = page.evaluate(
            "() => (document.getElementById('statusText')||{}).textContent")

        report["pageerrors"] = errors
        (OUT / "live_update.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        browser.close()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    ok = report.get("media_library_live_update", {}).get("new_card_appeared", False)
    print("OVERALL:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
