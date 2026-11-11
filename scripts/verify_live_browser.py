#!/usr/bin/env python3
"""
Browser verification against the **LIVE** :8190 instance (deployed code).

Confirms the deployed build shows:
  * real job -> green 完成 badge, task card + thumbnail, no red bar
  * fabricated job id -> red 失败 badge + red bar
  * console 0 errors
at 375 / 390 / 1280.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from playwright.sync_api import sync_playwright  # noqa: E402

SHOTS = Path("/root/projects/h3studio-fabricated-fix/evidence/fabricated-job-fix")
BASE = "http://127.0.0.1:8190"
REAL_ID = "ac8778a06a04"

RED_BAR_TEXT = ("模型声称已下发任务但实际没有创建任务记录"
                "（报告的 job id 是编造的），已拦截。")

SUCCESS_RESULT = {
    "status": "completed",
    "answer": ("已用 Moss 模型重新下发同一张港风夜景图：t2i_moss"
               "（MOSS-Image 文生图）｜比例 21:9 超宽画幅｜分辨率 720p。"),
    "jobs": [{
        "job_id": REAL_ID, "mode": "t2i_moss",
        "label": "MOSS-Image 文生图", "status": "done",
        "outputs": [{"kind": "image", "file": "images/i-zimage-000134.png",
                     "name": "moss.png"}],
    }],
    "steps": [],
}

FAIL_RESULT = {
    "status": "failed",
    "answer": "已下发文生视频任务（job t2v_019d874a）",
    "jobs": [], "steps": [],
    "error": RED_BAR_TEXT, "code": "fabricated_job_id",
}


def probe(page):
    return page.evaluate("""() => {
        const all = [...document.querySelectorAll('.ag-msg-bot')];
        const last = all[all.length - 1];
        const scope = last || document;
        return {
            badges: [...scope.querySelectorAll('.ag-badge')]
                .map(e => e.textContent.trim()),
            error_bars: [...scope.querySelectorAll('.ag-error-text')]
                .map(e => e.textContent.trim()),
            job_cards: scope.querySelectorAll('.ag-job').length,
            thumbs: scope.querySelectorAll('.ag-job-thumb img').length,
            jobs_title: (scope.querySelector('.ag-jobs .ag-block-title')
                          || {}).textContent || '',
            thumb_loaded: [...scope.querySelectorAll('.ag-job-thumb img')]
                .every(i => i.complete && i.naturalWidth > 0),
        };
    }""")


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    console_errors = []
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        for width, height, tag in ((375, 812, "375"),
                                   (390, 844, "390"),
                                   (1280, 900, "1280")):
            ctx = browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=2)
            page = ctx.new_page()
            errs = []
            failed = []
            page.on("console", lambda m: errs.append(m.text)
                    if m.type == "error" else None)
            page.on("pageerror", lambda e: errs.append(str(e)))
            page.on("response", lambda r: failed.append(f"{r.status} {r.url}")
                    if r.status >= 400 else None)
            page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector("#navAgent", timeout=30000)
            page.wait_for_timeout(1200)
            page.evaluate("() => window.AgentUI.show()")
            page.wait_for_timeout(400)

            page.evaluate(
                """(r) => window.AgentUI._injectRenderedMessage(r, 'done')""",
                SUCCESS_RESULT)
            page.wait_for_timeout(700)
            ok_probe = probe(page)
            ok_shot = SHOTS / f"live-{tag}-01-real-job-success.png"
            page.screenshot(path=str(ok_shot))

            page.evaluate(
                """(r) => window.AgentUI._injectRenderedMessage(r, 'error')""",
                FAIL_RESULT)
            page.wait_for_timeout(700)
            fail_probe = probe(page)
            fail_shot = SHOTS / f"live-{tag}-02-fabricated-blocked.png"
            page.screenshot(path=str(fail_shot))

            results.append({
                "width": width, "real_job": ok_probe,
                "real_job_screenshot": str(ok_shot),
                "fabricated": fail_probe,
                "fabricated_screenshot": str(fail_shot),
                "failed_requests": failed,
            })
            console_errors.extend(errs)
            ctx.close()
        browser.close()

    ok = True
    for r in results:
        rj, rf = r["real_job"], r["fabricated"]
        checks = {
            "real: 完成 badge": "完成" in rj["badges"],
            "real: 无失败 badge": "失败" not in rj["badges"],
            "real: 无红条": not rj["error_bars"],
            "real: 任务卡 1 张": rj["job_cards"] == 1,
            "real: 缩略图 1 张": rj["thumbs"] == 1,
            "real: 缩略图真的加载出来": rj["thumb_loaded"],
            "real: 任务标题含（1）": "（1）" in rj["jobs_title"],
            "fabricated: 失败 badge": "失败" in rf["badges"],
            "fabricated: 有红条": bool(rf["error_bars"]),
            "fabricated: 红条含「编造」": any("编造" in e
                                            for e in rf["error_bars"]),
            "fabricated: 无任务卡": rf["job_cards"] == 0,
        }
        r["checks"] = checks
        r["all_pass"] = all(checks.values())
        ok = ok and r["all_pass"]

    report = {"base": BASE, "results": results,
              "console_errors": console_errors, "all_pass": ok}
    (SHOTS / "live_browser_verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if (ok and not console_errors) else 1


if __name__ == "__main__":
    sys.exit(main())
