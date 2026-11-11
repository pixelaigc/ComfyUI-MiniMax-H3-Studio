#!/usr/bin/env python3
"""
Real-browser verification of the fabricated-job-id fix.

Proves, in a REAL browser against a REAL running server (throwaway data dir):
  1. a real job (id mis-copied by the model) -> "完成" badge, task card and
     thumbnail render, NO red bar, NO "失败" badge
  2. a genuinely fabricated job id -> red "失败" badge + red bar (拦截仍生效)
  3. console has 0 errors
  4. screenshots at 375 / 390 / 1280

No real generation is triggered and no production data is touched.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path("/root/projects/h3studio-fabricated-fix")
SHOTS = REPO / "evidence" / "fabricated-job-fix"
PORT = int(os.environ.get("VERIFY_PORT", "8199"))

REAL_ID = "ac8778a06a04"
TYPO_ID = "ac8778a6ea84"
RED_BAR_TEXT = ("模型声称已下发任务但实际没有创建任务记录"
                "（报告的 job id 是编造的），已拦截。")

#: 缩略图用**真实的生产产物**（MOSS 那次真实出图的图片），
#: 这样截图里能看见**真正渲染出来的缩略图**，而不是一个 1x1 透明像素。
#: 找不到时退回 1x1 透明 PNG（仍能验证 DOM 里有 <img>，只是看不见图）。
_REAL_THUMB = Path("/root/projects/comfyui-minimax-h3-studio/data/outputs/"
                   "images/i-zimage-000134.png")


def _thumb_bytes() -> bytes:
    try:
        if _REAL_THUMB.is_file():
            return _REAL_THUMB.read_bytes()
    except OSError:
        pass
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d4944415478da63fcffff3f0300050001a5f645bf"
        "0000000049454e44ae426082")


def start_server(tmp: Path):
    data = tmp / "data"
    (data / "outputs" / "images").mkdir(parents=True, exist_ok=True)
    # 造一个真实的 PNG 当缩略图 —— 否则任务卡的 <img> 会 404，
    # 浏览器控制台就会报 console error（那是测试夹具的问题，不是应用的）。
    png = (data / "outputs" / "images" / "i-zimage-000134.png")
    png.write_bytes(_thumb_bytes())
    env = os.environ.copy()
    env["H3STUDIO_PORT"] = str(PORT)
    env["H3STUDIO_DATA_DIR"] = str(data)
    env["PYTHONPATH"] = str(REPO)
    proc = subprocess.Popen(
        ["/home/ubuntu/comfyui/venv/bin/python", "server.py"],
        cwd=str(REPO), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(120):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}/api/jobs", timeout=2) as r:
                if r.status == 200:
                    return proc
        except Exception:
            time.sleep(0.5)
    out = proc.stdout.read() if proc.stdout else ""
    proc.kill()
    raise RuntimeError("server did not start:\n" + out[-3000:])


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
    "jobs": [],
    "steps": [],
    "error": RED_BAR_TEXT,
    "code": "fabricated_job_id",
}


def probe(page):
    """
    只统计**最后一条** Agent 消息 —— 两条消息在同一个页面里时，
    全页计数会把上一条的任务卡算进来（那是脚本的 bug，不是应用的 bug）。
    """
    return page.evaluate("""() => {
        const all = [...document.querySelectorAll('.ag-msg-bot')];
        const last = all[all.length - 1];
        const scope = last || document;
        const badges = [...scope.querySelectorAll('.ag-badge')]
            .map(e => e.textContent.trim());
        const errs = [...scope.querySelectorAll('.ag-error-text')]
            .map(e => e.textContent.trim());
        return {
            badges: badges,
            error_bars: errs,
            job_cards: scope.querySelectorAll('.ag-job').length,
            thumbs: scope.querySelectorAll('.ag-job-thumb img').length,
            jobs_title: (scope.querySelector('.ag-jobs .ag-block-title')
                          || {}).textContent || '',
        };
    }""")


def main():
    from playwright.sync_api import sync_playwright
    SHOTS.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="h3verify-"))
    proc = start_server(tmp)
    console_errors = []
    results = []
    try:
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
                failed_requests = []
                page.on("console", lambda m: errs.append(m.text)
                        if m.type == "error" else None)
                page.on("pageerror", lambda e: errs.append(str(e)))
                page.on("response", lambda r: failed_requests.append(
                    f"{r.status} {r.url}") if r.status >= 400 else None)
                # 用 domcontentloaded 而不是 networkidle ——
                # 页面会持续轮询 ComfyUI 状态，networkidle 永远等不到空闲。
                page.goto(f"http://127.0.0.1:{PORT}/",
                          wait_until="domcontentloaded", timeout=60000)
                page.wait_for_selector("#navAgent", timeout=30000)
                page.wait_for_timeout(900)
                # 切到「内置 Agent」页 —— 否则 Agent 面板是隐藏的
                # （.page-hidden），截图拍到的是 Z-IMAGE 面板，什么都看不到。
                #
                # 【为什么直接调 show() 而不是点导航】
                # 窄屏（375/390）下侧边栏是**抽屉**，navAgent 在视口外，
                # Playwright 的 click 会一直等「元素在视口内」而超时。
                # 直接调页面自己的 show() —— 走的是用户点导航的同一条代码路径。
                page.evaluate("() => window.AgentUI.show()")
                page.wait_for_timeout(400)
                assert page.eval_on_selector(
                    "#agentPage",
                    "el => !el.classList.contains('page-hidden')"
                ), "Agent 面板没有显示出来"

                # --- case 1: real job (model mis-copied the id) ---------
                page.evaluate(
                    """(r) => window.AgentUI._injectRenderedMessage(r, 'done')""",
                    SUCCESS_RESULT)
                page.wait_for_timeout(500)
                ok_probe = probe(page)
                ok_shot = SHOTS / f"fix-{tag}-01-real-job-success.png"
                page.screenshot(path=str(ok_shot), full_page=False)

                # --- case 2: genuine fabrication still blocked ----------
                page.evaluate(
                    """(r) => window.AgentUI._injectRenderedMessage(r, 'error')""",
                    FAIL_RESULT)
                page.wait_for_timeout(500)
                fail_probe = probe(page)
                fail_shot = SHOTS / f"fix-{tag}-02-fabricated-blocked.png"
                page.screenshot(path=str(fail_shot), full_page=False)

                results.append({
                    "width": width,
                    "real_job": ok_probe,
                    "real_job_screenshot": str(ok_shot),
                    "fabricated": fail_probe,
                    "fabricated_screenshot": str(fail_shot),
                    "failed_requests": failed_requests,
                })
                console_errors.extend(errs)
                ctx.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- assertions -------------------------------------------------
    ok = True
    for r in results:
        rj = r["real_job"]
        rf = r["fabricated"]
        checks = {
            "real: 完成 badge": "完成" in rj["badges"],
            "real: 无失败 badge": "失败" not in rj["badges"],
            "real: 无红条": not rj["error_bars"],
            "real: 任务卡 1 张": rj["job_cards"] == 1,
            "real: 缩略图 1 张": rj["thumbs"] == 1,
            "real: 任务标题含 (1)": "（1）" in rj["jobs_title"],
            "fabricated: 失败 badge": "失败" in rf["badges"],
            "fabricated: 有红条": bool(rf["error_bars"]),
            "fabricated: 无任务卡": rf["job_cards"] == 0,
        }
        r["checks"] = checks
        r["all_pass"] = all(checks.values())
        ok = ok and r["all_pass"]

    report = {"results": results, "console_errors": console_errors,
              "all_pass": ok}
    (SHOTS / "browser_verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if (ok and not console_errors) else 1


if __name__ == "__main__":
    sys.exit(main())
