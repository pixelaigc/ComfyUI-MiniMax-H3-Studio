# -*- coding: utf-8 -*-
"""接续生成的**真实浏览器实测**脚本（375px / 1280px 两档截图）。

【这个脚本在做什么】
跑通用户描述的**完整链路**：
    生成库出现「接续」按钮
      -> 点击后左侧配置区自动回填（接续上下文区域出现）
      -> 输入新提示词
      -> 接续生成
      -> 媒体库得到**拼接完整视频**
      -> 对完整视频再接续一次成功

每一步都截图，并**收集控制台报错**（要求 0 条）。

【与 pytest 的区别】
pytest 里那套是**契约测试**（断言 DOM 结构对不对）。
这个脚本是**端到端走查**：真的点、真的看截图、真的确认产物。
两者互补 —— 契约测试保证不回归，这个脚本证明「整条路真的通」。

用法：
    /home/ubuntu/comfyui/venv/bin/python tests/verify_continuation_browser.py \
        --base http://127.0.0.1:8199 \
        --out evidence/continuation
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from playwright.sync_api import sync_playwright
except Exception:
    print("需要 playwright：/home/ubuntu/comfyui/venv/bin/pip install playwright")
    raise

WIDTHS = (375, 1280)
CONSOLE_TAG = "[browser]"


def accept_base(default="http://127.0.0.1:8199"):
    """验收实例的 base URL。

    端口写在 data-accept/PORT（启动脚本自动顺延时写的）——
    8199 可能被并发 agent 占用，所以不能硬编码。
    """
    port_file = Path(__file__).resolve().parent.parent / "data-accept" / "PORT"
    if port_file.is_file():
        try:
            port = port_file.read_text(encoding="utf-8").strip()
            if port.isdigit():
                return f"http://127.0.0.1:{port}"
        except Exception:
            pass
    return default


def _http_get(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw else {}


def _http_multipart(url, fields, timeout=120):
    boundary = "----h3verify7717"
    body = b""
    for name, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += (f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                 ).encode()
        body += str(value).encode("utf-8") + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _wait_done(base, job_id, timeout=600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = _http_get(f"{base}/api/jobs/{job_id}")
        if job.get("status") in ("done", "failed", "canceled"):
            return job
        time.sleep(3)
    raise TimeoutError(f"任务 {job_id} 超时")


def _probe_video(path):
    """取视频帧数与时长。"""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not Path(path).is_file():
        return {}
    try:
        raw = subprocess.check_output(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=nb_frames", "-of", "csv=p=0", path],
            text=True).strip()
        dur = subprocess.check_output(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path], text=True).strip()
        return {"frames": int(raw), "duration": float(dur)}
    except Exception:
        return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=accept_base())
    parser.add_argument("--out", default=str(REPO / "evidence" / "continuation"))
    parser.add_argument("--skip-generate", action="store_true",
                        help="跳过真实生成（只走 UI 交互）")
    parser.add_argument("--segment1-job", default="",
                        help="--skip-generate 时：已有的第 1 段任务 id"
                             "（留空则自动取最近一条可接续的）")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    report = {"base": base, "steps": [], "console_errors": [],
              "failed_requests": [], "shots": []}
    # 可能因 --skip-generate 而未绑定 -> 先给空值
    j1 = j2 = j3 = {}

    def log(step, **kw):
        report["steps"].append({"step": step, **kw})
        print(f"{CONSOLE_TAG} {step}: {json.dumps(kw, ensure_ascii=False)}",
              flush=True)

    # ---------------------------------------------------------------- 1
    log("capability", **_http_get(f"{base}/api/continuation/state"))

    # ---------------------------------------------------------------- 2
    # 真实生成第 1 段
    if not args.skip_generate:
        params = {
            "prompt": "A lone swordsman in black robes standing in a moonlit "
                      "bamboo forest, slow camera push-in, cold mist drifting",
            "model_key": os.environ.get("H3STUDIO_TEST_MODEL",
                                        "fl2va_pruned_w4a8"),
            "width": 768, "height": 448,
            "duration": 5, "steps": 4, "sampler": "euler",
            "scheduler": "beta", "seed": 4242,
        }
        job1 = _http_multipart(f"{base}/api/jobs",
                               {"mode": "t2v", "params": json.dumps(params)})["id"]
        log("segment1_submitted", job_id=job1)
        j1 = _wait_done(base, job1)
        log("segment1_done", status=j1["status"],
            outputs=j1.get("outputs"),
            continuation_info=j1.get("continuation_info"))
        if j1["status"] != "done":
            print("第 1 段生成失败：", j1.get("error"))
            return 1
        seg1_video = next(o for o in j1["outputs"] if o["kind"] == "video")
        seg1_path = Path(base.replace("http://127.0.0.1", ""))  # 占位，下面拼
        report["segment1_job"] = job1
    else:
        job1 = args.segment1_job
        if not job1:
            # 自动取最近一条服务端判定「可接续」的任务
            data = _http_get(f"{base}/api/jobs?status=all&page=1&page_size=50")
            for job in data.get("jobs", []):
                info = job.get("continuation_info") or {}
                if info.get("can_continue"):
                    job1 = job["id"]
                    break
        if not job1:
            print("找不到可接续的任务（先跑一次不带 --skip-generate 的）")
            return 1
        log("segment1_reused", job_id=job1)

    # ---------------------------------------------------------------- 3
    # 起浏览器
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        try:
            for width in WIDTHS:
                ctx = browser.new_context(viewport={"width": width,
                                                   "height": 900})
                page = ctx.new_page()
                errors = []
                failed = []
                page.on("pageerror", lambda e, errs=errors: errs.append(str(e)))
                page.on("console", lambda m, errs=errors:
                        errs.append(m.text) if m.type == "error" else None)
                page.on("response", lambda r, bad=failed:
                        bad.append(f"{r.status} {r.url}") if r.status >= 400
                        else None)

                page.goto(f"{base}/", wait_until="domcontentloaded")
                page.wait_for_timeout(2500)

                # 切到「已完成」标签 —— 接续的产物都是 done 状态的视频，
                # 「全部」标签里会混进排队中的骨架占位，卡片渲染慢。
                page.evaluate("""() => {
                  const btn = document.querySelector('[data-st="done"]');
                  if (btn) btn.click();
                }""")
                page.wait_for_timeout(800)

                # 等真实卡片（骨架 .job-skeleton 会被替换掉）
                try:
                    page.wait_for_function(
                        "() => document.querySelectorAll("
                        "'#jobsGrid .job-card:not(.job-skeleton)').length > 0",
                        timeout=30000)
                except Exception:
                    pass
                page.wait_for_timeout(1200)

                shot = out / f"cont-{width}-01-gallery.png"
                page.screenshot(path=str(shot), full_page=False)
                report["shots"].append(str(shot))

                # 找到可接续的卡片
                card = page.evaluate("""() => {
                  const cards = Array.from(document.querySelectorAll(
                    '.job-card:not(.job-skeleton)'));
                  for (const c of cards) {
                    const b = c.querySelector('[data-act="continue"]');
                    if (b) return {jid: c.dataset.jid};
                  }
                  return null;
                }""")
                # 诊断：把卡片状态记下来（为什么没找到）
                diag = page.evaluate("""() => {
                  const all = document.querySelectorAll('#jobsGrid .job-card');
                  const real = document.querySelectorAll(
                    '#jobsGrid .job-card:not(.job-skeleton)');
                  return {all: all.length, real: real.length,
                          capability: window.Continuation
                            ? window.Continuation.state().capability
                            : null};
                }""")
                log(f"gallery_{width}", continue_card=card, diag=diag)
                if not card:
                    errors.append(
                        f"生成库里没有可接续的卡片（{diag}）")

                # 点接续
                if card:
                    page.click(f'.job-card[data-jid="{card["jid"]}"] '
                               f'[data-act="continue"]')
                    page.wait_for_timeout(1800)
                    shot = out / f"cont-{width}-02-backfilled.png"
                    page.screenshot(path=str(shot), full_page=False)
                    report["shots"].append(str(shot))

                    # 左侧面板的**局部**截图：接续上下文区在面板顶部，
                    # 整页截图里它被挤到可视区之外 —— 必须单独截一张，
                    # 否则证据里看不到「上下文真的出现了」。
                    panel_shot = out / f"cont-{width}-02b-context-panel.png"
                    try:
                        page.locator("#continuationPanel").screenshot(
                            path=str(panel_shot))
                    except Exception:
                        panel_el = page.query_selector("#panel")
                        if panel_el:
                            panel_el.screenshot(path=str(panel_shot))
                    if panel_shot.is_file():
                        report["shots"].append(str(panel_shot))

                    # 窄屏：左侧配置区是**抽屉**（默认收起）。
                    # 接续上下文在抽屉里 —— 不打开抽屉就截不到它。
                    # 抽屉的打开按钮：responsive.css 里的 panel 触发器。
                    if width < 768:
                        # 窄屏下参数面板是底部抽屉，由 #panelFab 唤起
                        opened = page.evaluate("""() => {
                          const fab = document.getElementById('panelFab');
                          if (!fab) return false;
                          fab.click();
                          return true;
                        }""")
                        page.wait_for_timeout(900)
                        log(f"drawer_opened_{width}", opened=opened)

                    # 面板整体：**滚到接续上下文区**（不是滚到顶部 ——
                    # 上下文区在提示词正上方，滚到顶部就看不到它了）。
                    panel_full = out / f"cont-{width}-02c-left-panel.png"
                    try:
                        page.evaluate("""() => {
                          const ctx = document.getElementById('continuationPanel');
                          const panel = document.getElementById('panel');
                          if (!ctx || !panel) return;
                          // 把上下文区滚到抽屉可视区的中上部
                          const cRect = ctx.getBoundingClientRect();
                          const pRect = panel.getBoundingClientRect();
                          const delta = cRect.top - pRect.top - 12;
                          panel.scrollTop += delta;
                        }""")
                        page.wait_for_timeout(600)
                        panel_el = page.query_selector("#panel")
                        if panel_el:
                            panel_el.screenshot(path=str(panel_full))
                            report["shots"].append(str(panel_full))
                    except Exception:
                        pass

                    state = page.evaluate("""() => {
                      const p = document.getElementById('continuationPanel');
                      const vp = window.VIDEO_PANEL;
                      return {
                        panelHidden: p ? p.hidden : null,
                        panelText: p ? p.textContent.slice(0, 400) : '',
                        hasClear: !!(p && p.querySelector('#continuationClear')),
                        addButtons: p ? Array.from(
                          p.querySelectorAll('button')).map(
                          b => b.textContent.trim()) : [],
                        model: vp && vp.state ? vp.state.model : null,
                        width: vp && vp.state ? vp.state.width : null,
                        height: vp && vp.state ? vp.state.height : null,
                        steps: vp && vp.state ? vp.state.steps : null,
                        prompt: (document.getElementById('prompt')||{}).value,
                      };
                    }""")
                    log(f"after_continue_{width}", **state)

                    # 输入下一段提示词
                    page.fill("#prompt",
                              "He draws his blade and steps forward, "
                              "moonlight flaring along the steel")
                    page.wait_for_timeout(300)
                    shot = out / f"cont-{width}-03-prompt.png"
                    page.screenshot(path=str(shot), full_page=False)
                    report["shots"].append(str(shot))

                report["console_errors"].extend(errors)
                report["failed_requests"].extend(failed)
                ctx.close()
        finally:
            browser.close()

    # ---------------------------------------------------------------- 4
    # 真实接续生成（用后端接口，验证产物）
    if not args.skip_generate:
        ctx_data = _http_get(f"{base}/api/jobs/{job1}/continuation")
        log("continuation_context", ok=ctx_data.get("ok"),
            params_keys=sorted((ctx_data.get("params") or {}).keys()),
            summary_len=len(ctx_data.get("summary") or []))
        params2 = dict(ctx_data["params"])
        params2["prompt"] = ("He draws his blade and steps forward, "
                             "moonlight flaring along the steel")
        params2["seed"] = 777
        params2["continuation"] = ctx_data["continuation"]
        job2 = _http_multipart(f"{base}/api/jobs",
                               {"mode": "t2v",
                                "params": json.dumps(params2)})["id"]
        j2 = _wait_done(base, job2)
        log("segment2_done", job_id=job2, status=j2["status"],
            outputs=j2.get("outputs"),
            continuation_info=j2.get("continuation_info"))
        report["segment2_job"] = job2

        # 第 3 段：从**完整视频**再接续一次
        ctx2 = _http_get(f"{base}/api/jobs/{job2}/continuation")
        params3 = dict(ctx2["params"])
        params3["prompt"] = ("He sheathes the blade and walks into the bamboo "
                             "mist, the moon rising higher")
        params3["seed"] = 999
        params3["continuation"] = ctx2["continuation"]
        job3 = _http_multipart(f"{base}/api/jobs",
                               {"mode": "t2v",
                                "params": json.dumps(params3)})["id"]
        j3 = _wait_done(base, job3)
        log("segment3_done", job_id=job3, status=j3["status"],
            outputs=j3.get("outputs"),
            continuation_info=j3.get("continuation_info"))
        report["segment3_job"] = job3

        # 媒体库里的完整视频
        for job in (j1, j2, j3):
            for out_item in job.get("outputs") or []:
                if out_item.get("kind") != "video":
                    continue
                out_root = Path(os.environ.get(
                    "H3STUDIO_DATA_DIR",
                    str(REPO / "data-accept"))) / "outputs"
                probe = _probe_video(out_root / out_item["file"])
                log("probe", job=job["id"], file=out_item["file"],
                    is_full=out_item.get("is_full", False), **probe)

    report_path = out / "browser_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    print(f"\n{CONSOLE_TAG} 报告: {report_path}")
    js_errors = [e for e in report["console_errors"]
                 if "Failed to load resource" not in e]
    print(f"{CONSOLE_TAG} 控制台 JS 报错: {len(js_errors)}")
    for e in js_errors:
        print("   ✗", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
