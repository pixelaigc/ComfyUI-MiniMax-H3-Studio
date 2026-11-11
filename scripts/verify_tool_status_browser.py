#!/usr/bin/env python3
"""
Real-browser verification of the tool-level + truncated-status fix (2026-11-11).

Proves, in a REAL browser against a REAL running server (throwaway data dir):

  1. TOOL-LEVEL: a create_job that really created a job renders ✓ (green),
     never ✗.  (The user's report claimed 5 create_job calls were marked ✗.)
  2. TOOL-LEVEL: a create_job that really failed (no job in DB) renders ✗ (red)
     -- truthfulness must NOT be lost.
  3. TRUNCATED:  "已达��步骤上限（8 步）" shows the yellow 「已截断」 badge AND a
     NEUTRAL notice bar -- and NO red error bar.
  4. FAILED:     a real failure still shows the red 「失败」 badge + red error bar.
  5. console has 0 errors; 375 / 390 / 1280 all pass.

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

REPO = Path(os.environ.get("VERIFY_REPO", "/root/projects/h3studio-fabricated-fix"))
SHOTS = REPO / "evidence" / "tool-status-fix"
PORT = int(os.environ.get("VERIFY_PORT", "8197"))

REAL_ID = "ac8778a06a04"

TRUNCATED_TEXT = "已达到步骤上限（8 步），返回已完成的部分结果"


def start_server(tmp: Path):
    data = tmp / "data"
    (data / "outputs" / "images").mkdir(parents=True, exist_ok=True)
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


#: 工具级：真的建出了任务 -> 工具块必须是 ✓
TOOL_OK_RESULT = {
    "status": "completed",
    "answer": "已用 qwen-image21 下发任务。",
    "jobs": [{
        "job_id": REAL_ID, "mode": "t2i_qwen",
        "label": "Qwen-Image 2.1 文生图", "status": "queued",
    }],
    "steps": [{
        "step": 1, "tool": "create_job", "ok": True, "ms": 12,
        "args": '{"mode": "t2i_qwen", "prompt": "海边日落"}',
        "summary": f"已创建任务 {REAL_ID}（t2i_qwen）",
        "call_id": "c1", "note": "", "error": None,
    }],
}

#: 工具级：真的失败了（参数非法，库里没任务）-> 工具块必须是 ✗
TOOL_BAD_RESULT = {
    "status": "completed",
    "answer": "参数不对，没能建任务。",
    "jobs": [],
    "steps": [{
        "step": 1, "tool": "create_job", "ok": False, "ms": 8,
        "args": '{"mode": "t2v", "prompt": "x", "aspect": "21:10"}',
        "summary": "参数校验未通过：aspect 不是合法取值",
        "call_id": "c1", "note": "",
        "error": "参数校验未通过：aspect 不是合法取值",
    }],
}

#: 步骤上限截断 -> 黄色徽章 + 中性条，**没有**红条
TRUNCATED_RESULT = {
    "status": "truncated",
    "answer": TRUNCATED_TEXT,
    "jobs": [],
    "steps": [],
    "error": TRUNCATED_TEXT,
    "notice": TRUNCATED_TEXT,
}

#: 真失败 -> 红色徽章 + 红条（拦截不得回退）
FAILED_RESULT = {
    "status": "failed",
    "answer": "模型声称已下发任务但实际没有创建任何任务。",
    "jobs": [],
    "steps": [],
    "error": ("模型声称已下发任务但实际没有创建任何任务"
              "（报告的 job id 是编造的），已拦截。"),
    "code": "fabricated_job_id",
}


def probe(page):
    """只统计最后一条 Agent 消息。"""
    return page.evaluate("""() => {
        const all = [...document.querySelectorAll('.ag-msg-bot')];
        const last = all[all.length - 1];
        const scope = last || document;
        const badges = [...scope.querySelectorAll('.ag-badge')]
            .map(e => e.textContent.trim());
        const errs = [...scope.querySelectorAll('.ag-error-text')]
            .map(e => e.textContent.trim());
        const notices = [...scope.querySelectorAll('.ag-notice-text')]
            .map(e => e.textContent.trim());
        const tools = [...scope.querySelectorAll('.ag-tool')].map(t => ({
            name: (t.querySelector('.ag-tool-name')||{}).textContent || '',
            state: (t.querySelector('.ag-tool-state')||{}).textContent || '',
            isBad: t.classList.contains('ag-tool-bad'),
        }));
        return {
            badges: badges,
            error_bars: errs,
            notice_bars: notices,
            tools: tools,
            job_cards: scope.querySelectorAll('.ag-job').length,
        };
    }""")


def main():
    from playwright.sync_api import sync_playwright
    SHOTS.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="h3verify-tool-"))
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
                page.goto(f"http://127.0.0.1:{PORT}/",
                          wait_until="domcontentloaded", timeout=60000)
                page.wait_for_selector("#navAgent", timeout=30000)
                page.wait_for_timeout(900)
                page.evaluate("() => window.AgentUI.show()")
                page.wait_for_timeout(400)
                assert page.eval_on_selector(
                    "#agentPage",
                    "el => !el.classList.contains('page-hidden')"
                ), "Agent 面板没有显示出来"

                def clear_messages():
                    """清空上一条用例的消息，让每张截图只反映当前用例。"""
                    page.evaluate("""() => {
                      window.AgentUI.state.messages = [];
                      window.AgentUI.state.sessionId = '';
                      window.AgentUI.state.lastResult = null;
                      window.AgentUI.state.streaming = null;
                      window.AgentUI.state.busy = false;
                      document.getElementById('agMessages').innerHTML = '';
                    }""")

                # 【每个用例：注入 -> 探测 -> **立刻**截图】
                # 截图必须在注入后立刻拍 —— 否则多条消息堆在页面上，
                # 截图里会同时出现黄色条和红色条，看的人分不清
                # 哪个属于哪个用例（实测踩过：vision 读图时把两条都读出来）。
                for payload, name, shot in (
                        (TOOL_OK_RESULT, "tool-ok",
                         SHOTS / f"tool-{tag}-01-createjob-ok.png"),
                        (TOOL_BAD_RESULT, "tool-bad",
                         SHOTS / f"tool-{tag}-02-createjob-bad.png"),
                        (TRUNCATED_RESULT, "truncated",
                         SHOTS / f"tool-{tag}-03-truncated-neutral.png"),
                        (FAILED_RESULT, "failed",
                         SHOTS / f"tool-{tag}-04-failed-red.png")):
                    clear_messages()
                    page.evaluate(
                        """(r) => window.AgentUI._injectRenderedMessage(r, 'done')""",
                        payload)
                    page.wait_for_timeout(400)
                    data = probe(page)
                    page.screenshot(path=str(shot), full_page=False)
                    results.append({
                        "width": width, "case": name, "probe": data,
                        "screenshot": str(shot),
                    })

                results[-1]["failed_requests"] = failed_requests
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
        pr = r["probe"]
        if r["case"] == "tool-ok":
            checks = {
                "工具级真成功: 工具块存在": len(pr["tools"]) == 1,
                "工具级真成功: 显示 ✓": pr["tools"][0]["state"] == "✓",
                "工具级真成功: 不是 bad 类": not pr["tools"][0]["isBad"],
                "工具级真成功: 无红条": not pr["error_bars"],
            }
        elif r["case"] == "tool-bad":
            checks = {
                "工具级真失败: 显示 ✗": pr["tools"][0]["state"] == "✕",
                "工具级真失败: 是 bad 类": pr["tools"][0]["isBad"],
            }
        elif r["case"] == "truncated":
            checks = {
                "截断: 徽章是已截断": "已截断" in pr["badges"],
                "截断: 无失败徽章": "失败" not in pr["badges"],
                "截断: **无红条**": not pr["error_bars"],
                "截断: 有中性提示条": len(pr["notice_bars"]) == 1,
                "截断: 中性条文案正确": TRUNCATED_TEXT in (pr["notice_bars"][0]
                                                    if pr["notice_bars"] else ""),
            }
        else:
            checks = {
                "真失败: 徽章是失败": "失败" in pr["badges"],
                "真失败: 有红条": len(pr["error_bars"]) == 1,
                "真失败: 无中性条": not pr["notice_bars"],
            }
        r["checks"] = checks
        r["all_pass"] = all(checks.values())
        ok = ok and r["all_pass"]

    report = {"results": results, "console_errors": console_errors,
              "all_pass": ok}
    (SHOTS / "browser_verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if (ok and not console_errors) else 1


if __name__ == "__main__":
    sys.exit(main())
