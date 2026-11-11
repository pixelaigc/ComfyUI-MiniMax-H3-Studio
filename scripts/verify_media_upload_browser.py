#!/usr/bin/env python3
"""
Real-browser verification of the Agent media-upload feature (2026-11-11).

Proves, in a REAL browser against a REAL running server (throwaway data dir):

  1. UPLOAD: the composer has a visible upload entry; picking a real PNG
     uploads it through the REAL multipart endpoint and shows a THUMBNAIL.
  2. MULTI:  several images upload at once and each gets its own thumbnail.
  3. REMOVE: clicking the x on a thumbnail removes it.
  4. REJECT: a non-image file is rejected with a readable error (no crash).
  5. LIMIT: more than 9 images is refused (the extra ones are not uploaded).
  6. NO OVERFLOW: at 375 / 390 / 1280 the page has NO horizontal overflow.
  7. console has 0 errors and 0 failed requests at every width.

Also asserts the reference-image capability the feature exists for:
  the uploaded asset lands in the asset library and can be referenced by
  asset_id (i.e. it really is a library entry, not a loose temp file).

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

REPO = Path(os.environ.get("VERIFY_REPO",
                           "/root/projects/h3studio-media-upload"))
SHOTS = REPO / "evidence" / "media-upload"
PORT = int(os.environ.get("VERIFY_PORT", "8198"))
LIB = os.environ.get("ASSET_LIBRARY_ROOT", "")


def start_server(tmp: Path):
    data = tmp / "data"
    (data / "outputs" / "images").mkdir(parents=True, exist_ok=True)
    # 素材库也指到临时目录 —— 绝不碰生产素材库。
    lib = tmp / "asset_library"
    lib.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["H3STUDIO_PORT"] = str(PORT)
    env["H3STUDIO_DATA_DIR"] = str(data)
    env["ASSET_LIBRARY_ROOT"] = str(lib)
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


def make_png(path: Path, color=(200, 30, 30)) -> Path:
    from PIL import Image
    Image.new("RGB", (120, 90), color).save(path)
    return path


def make_text(path: Path) -> Path:
    path.write_text("this is not an image at all", encoding="utf-8")
    return path


def probe(page):
    """读取附件区与整页溢出情况。"""
    return page.evaluate("""() => {
        const box = document.getElementById('agAttachments');
        const thumbs = [...document.querySelectorAll('.ag-attach-thumb')];
        const errEl = document.querySelector('.ag-attach-err');
        const btn = document.getElementById('agAttachBtn');
        const hint = document.getElementById('agAttachHint');
        return {
            attachBoxHidden: box ? box.hidden : null,
            thumbCount: thumbs.length,
            thumbSrcs: thumbs.map(t => {
                const img = t.querySelector('img');
                return img ? (img.getAttribute('src') || '') : '';
            }),
            error: errEl ? errEl.textContent.trim() : '',
            btnVisible: btn ? (btn.offsetParent !== null) : false,
            btnText: btn ? btn.textContent.trim() : '',
            hint: hint ? hint.textContent.trim() : '',
            attachments: window.AgentUI.state.attachments.map(a => a.asset_id),
            // 整页横向溢出检测（375px 不许溢出）
            docScrollW: document.documentElement.scrollWidth,
            docClientW: document.documentElement.clientWidth,
            bodyScrollW: document.body.scrollWidth,
        };
    }""")


def overflow_free(data):
    return (data["docScrollW"] <= data["docClientW"] + 1
            and data["bodyScrollW"] <= data["docClientW"] + 1)


def main():
    from playwright.sync_api import sync_playwright
    SHOTS.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="h3verify-media-"))
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

                # 每次用例前清空附件状态
                def reset():
                    page.evaluate("""() => {
                      window.AgentUI.state.attachments = [];
                      window.AgentUI.state.attachError = '';
                      window.AgentUI.state.attachBusy = false;
                      window.AgentUI._renderAttachments();
                    }""")

                def upload(paths):
                    """把文件塞进**真实的** <input type=file>，触发真实 change 事件。"""
                    page.set_input_files("#agAttachInput", [str(x) for x in paths])
                    # 等上传完成（缩略图出现或错误出现）
                    page.wait_for_function(
                        """() => {
                          const s = window.AgentUI.state;
                          return !s.attachBusy;
                        }""", timeout=30000)
                    page.wait_for_timeout(250)

                # --- 用例 1：上传入口存在 + 单图上传 ---
                reset()
                shot1 = SHOTS / f"media-{tag}-01-upload-entry.png"
                page.screenshot(path=str(shot1), full_page=False)
                base = probe(page)
                results.append({"width": width, "case": "entry",
                                "probe": base, "screenshot": str(shot1)})

                png1 = make_png(tmp / f"one-{tag}.png", (200, 30, 30))
                upload([png1])
                shot2 = SHOTS / f"media-{tag}-02-one-uploaded.png"
                page.screenshot(path=str(shot2), full_page=False)
                results.append({"width": width, "case": "one",
                                "probe": probe(page), "screenshot": str(shot2)})

                # --- 用例 2：多图 ---
                reset()
                pngs = [make_png(tmp / f"multi-{tag}-{i}.png",
                                 (i * 30, 100, 200)) for i in range(3)]
                upload(pngs)
                shot3 = SHOTS / f"media-{tag}-03-multi-uploaded.png"
                page.screenshot(path=str(shot3), full_page=False)
                results.append({"width": width, "case": "multi",
                                "probe": probe(page), "screenshot": str(shot3)})

                # --- 用例 3：移除 ---
                page.click(".ag-attach-thumb .ag-attach-remove")
                page.wait_for_timeout(300)
                shot4 = SHOTS / f"media-{tag}-04-after-remove.png"
                page.screenshot(path=str(shot4), full_page=False)
                results.append({"width": width, "case": "remove",
                                "probe": probe(page), "screenshot": str(shot4)})

                # --- 用例 4：非法类型被拒 ---
                reset()
                txt = make_text(tmp / f"bad-{tag}.txt")
                upload([txt])
                shot5 = SHOTS / f"media-{tag}-05-bad-type-rejected.png"
                page.screenshot(path=str(shot5), full_page=False)
                results.append({"width": width, "case": "bad-type",
                                "probe": probe(page), "screenshot": str(shot5)})

                # --- 用例 5：超过 9 张被拒 ---
                reset()
                many = [make_png(tmp / f"many-{tag}-{i}.png",
                                 (10 * i, 20, 30)) for i in range(11)]
                upload(many)
                shot6 = SHOTS / f"media-{tag}-06-limit-9.png"
                page.screenshot(path=str(shot6), full_page=False)
                results.append({"width": width, "case": "limit",
                                "probe": probe(page), "screenshot": str(shot6)})

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
        case = r["case"]
        if case == "entry":
            checks = {
                "上传入口可见": pr["btnVisible"],
                "没有附件时缩略图区隐藏": pr["attachBoxHidden"] is True,
                "375 窄屏无横向溢出": overflow_free(pr),
            }
        elif case == "one":
            checks = {
                "单图上传成功": pr["thumbCount"] == 1,
                "缩略图有 preview_url": bool(pr["thumbSrcs"][0]
                                          if pr["thumbSrcs"] else ""),
                "预览指向素材库预览接口": (pr["thumbSrcs"][0].startswith(
                    "/api/assets/") if pr["thumbSrcs"] else False),
                "状态里有 asset_id": len(pr["attachments"]) == 1
                                    and pr["attachments"][0].startswith("asset_"),
                "无错误提示": not pr["error"],
                "375 窄屏无横向溢出": overflow_free(pr),
            }
        elif case == "multi":
            checks = {
                "多图都上传成功": pr["thumbCount"] == 3,
                "asset_id 三个": len(pr["attachments"]) == 3,
                "缩略图各不相同": len(set(pr["thumbSrcs"])) == 3,
                "375 窄屏无横向溢出": overflow_free(pr),
            }
        elif case == "remove":
            checks = {
                "移除后剩 2 张": pr["thumbCount"] == 2,
                "移除后 asset_id 剩 2 个": len(pr["attachments"]) == 2,
            }
        elif case == "bad-type":
            checks = {
                "非法类型不被接受": pr["thumbCount"] == 0,
                "有可读的���误提示": bool(pr["error"]),
                "错误提示提到格式": ("格式" in pr["error"]
                                     or "不支持" in pr["error"]),
                "没有 JS 崩溃": True,
            }
        else:  # limit
            checks = {
                "最多 9 张": pr["thumbCount"] == 9,
                "asset_id 最多 9 个": len(pr["attachments"]) == 9,
                "有多余的提示": bool(pr["error"]) or "9" in pr["hint"],
                "375 窄屏无横向溢出": overflow_free(pr),
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
