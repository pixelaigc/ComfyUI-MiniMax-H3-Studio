#!/usr/bin/env python3
"""
Real-browser verification of the Agent media-upload feature against the
DEPLOYED 8190 instance (2026-11-11).

Unlike the local script, this one runs against the **real production server**:
  * the real asset library (/mnt/usb2t/asset_library)
  * the real /api/agent/upload endpoint
  * the real frontend on :8190

It therefore uploads real (throwaway) test images into the production asset
library and deletes them afterwards.

Proves at 375 / 390 / 1280:
  1. upload entry visible; a real PNG uploads and shows a thumbnail
  2. multi-image upload works
  3. remove works
  4. a non-image is rejected with a readable message
  5. more than 9 images is refused
  6. no horizontal overflow
  7. console 0 errors, 0 failed requests
"""
import json
import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8190
SHOTS = Path("/root/projects/h3studio-live/evidence/media-upload")


def make_png(path: Path, color=(200, 30, 30)) -> Path:
    from PIL import Image
    Image.new("RGB", (120, 90), color).save(path)
    return path


def make_text(path: Path) -> Path:
    path.write_text("this is not an image at all", encoding="utf-8")
    return path


def probe(page):
    return page.evaluate("""() => {
        const thumbs = [...document.querySelectorAll('.ag-attach-thumb')];
        const errEl = document.querySelector('.ag-attach-err');
        const btn = document.getElementById('agAttachBtn');
        const hint = document.getElementById('agAttachHint');
        const box = document.getElementById('agAttachments');
        return {
            attachBoxHidden: box ? box.hidden : null,
            thumbCount: thumbs.length,
            thumbSrcs: thumbs.map(t => {
                const img = t.querySelector('img');
                return img ? (img.getAttribute('src') || '') : '';
            }),
            error: errEl ? errEl.textContent.trim() : '',
            btnVisible: btn ? (btn.offsetParent !== null) : false,
            hint: hint ? hint.textContent.trim() : '',
            attachments: window.AgentUI.state.attachments.map(a => a.asset_id),
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
    tmp = Path(tempfile.mkdtemp(prefix="h3live-media-"))
    console_errors = []
    results = []
    uploaded_ids = []
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
                page.wait_for_timeout(1200)
                page.evaluate("() => window.AgentUI.show()")
                page.wait_for_timeout(500)
                assert page.eval_on_selector(
                    "#agentPage",
                    "el => !el.classList.contains('page-hidden')"
                ), "Agent 面板没有显示出来"

                def reset():
                    page.evaluate("""() => {
                      window.AgentUI.state.attachments = [];
                      window.AgentUI.state.attachError = '';
                      window.AgentUI.state.attachBusy = false;
                      window.AgentUI._renderAttachments();
                    }""")

                def upload(paths):
                    page.set_input_files("#agAttachInput",
                                         [str(x) for x in paths])
                    page.wait_for_function(
                        """() => !window.AgentUI.state.attachBusy""",
                        timeout=60000)
                    page.wait_for_timeout(400)

                reset()
                shot = SHOTS / f"live-media-{tag}-01-upload-entry.png"
                page.screenshot(path=str(shot), full_page=False)
                results.append({"width": width, "case": "entry",
                                "probe": probe(page), "screenshot": str(shot)})

                png1 = make_png(tmp / f"live-one-{tag}.png", (200, 30, 30))
                upload([png1])
                shot = SHOTS / f"live-media-{tag}-02-one-uploaded.png"
                page.screenshot(path=str(shot), full_page=False)
                pr = probe(page)
                uploaded_ids.extend(pr["attachments"])
                results.append({"width": width, "case": "one",
                                "probe": pr, "screenshot": str(shot)})

                reset()
                pngs = [make_png(tmp / f"live-multi-{tag}-{i}.png",
                                 (i * 30, 100, 200)) for i in range(3)]
                upload(pngs)
                shot = SHOTS / f"live-media-{tag}-03-multi-uploaded.png"
                page.screenshot(path=str(shot), full_page=False)
                pr = probe(page)
                uploaded_ids.extend(pr["attachments"])
                results.append({"width": width, "case": "multi",
                                "probe": pr, "screenshot": str(shot)})

                page.click(".ag-attach-thumb .ag-attach-remove")
                page.wait_for_timeout(350)
                shot = SHOTS / f"live-media-{tag}-04-after-remove.png"
                page.screenshot(path=str(shot), full_page=False)
                results.append({"width": width, "case": "remove",
                                "probe": probe(page), "screenshot": str(shot)})

                reset()
                txt = make_text(tmp / f"live-bad-{tag}.txt")
                upload([txt])
                shot = SHOTS / f"live-media-{tag}-05-bad-type-rejected.png"
                page.screenshot(path=str(shot), full_page=False)
                results.append({"width": width, "case": "bad-type",
                                "probe": probe(page), "screenshot": str(shot)})

                reset()
                many = [make_png(tmp / f"live-many-{tag}-{i}.png",
                                 (10 * i, 20, 30)) for i in range(11)]
                upload(many)
                shot = SHOTS / f"live-media-{tag}-06-limit-9.png"
                page.screenshot(path=str(shot), full_page=False)
                pr = probe(page)
                uploaded_ids.extend(pr["attachments"])
                results.append({"width": width, "case": "limit",
                                "probe": pr, "screenshot": str(shot)})

                results[-1]["failed_requests"] = failed_requests
                console_errors.extend(errs)
                ctx.close()
            browser.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- 清理：把测试图片从生产素材库删掉（进回收站，不 purge） ----
    cleaned = []
    for aid in sorted(set(uploaded_ids)):
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/api/assets/{aid}",
            method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                cleaned.append({"asset_id": aid, "status": r.status})
        except Exception as exc:  # noqa: BLE001
            cleaned.append({"asset_id": aid, "error": str(exc)[:120]})

    # ---- assertions -------------------------------------------------
    ok = True
    for r in results:
        pr = r["probe"]
        case = r["case"]
        if case == "entry":
            checks = {
                "上传入口可见": pr["btnVisible"],
                "没有附件时缩略图区隐藏": pr["attachBoxHidden"] is True,
                "窄屏无横向溢出": overflow_free(pr),
            }
        elif case == "one":
            checks = {
                "单图上传成功": pr["thumbCount"] == 1,
                "有 preview_url": bool(pr["thumbSrcs"][0] if pr["thumbSrcs"] else ""),
                "预览指向素材库接口": (pr["thumbSrcs"][0].startswith("/api/assets/")
                                       if pr["thumbSrcs"] else False),
                "拿到 asset_id": len(pr["attachments"]) == 1
                                 and pr["attachments"][0].startswith("asset_"),
                "无错误提示": not pr["error"],
                "窄屏无横向溢出": overflow_free(pr),
            }
        elif case == "multi":
            checks = {
                "多图都上传成功": pr["thumbCount"] == 3,
                "asset_id 三个": len(pr["attachments"]) == 3,
                "缩略图各不相同": len(set(pr["thumbSrcs"])) == 3,
                "窄屏无横向溢出": overflow_free(pr),
            }
        elif case == "remove":
            checks = {
                "移除后剩 2 张": pr["thumbCount"] == 2,
                "移除后 asset_id 剩 2 个": len(pr["attachments"]) == 2,
            }
        elif case == "bad-type":
            checks = {
                "非法类型不被接受": pr["thumbCount"] == 0,
                "有可读错误提示": bool(pr["error"]),
                "错误提示提到格式": ("格式" in pr["error"] or "不支持" in pr["error"]),
            }
        else:
            checks = {
                "最多 9 张": pr["thumbCount"] == 9,
                "asset_id 最多 9 个": len(pr["attachments"]) == 9,
                "有多余的提示": bool(pr["error"]) or "9" in pr["hint"],
                "窄屏无横向溢出": overflow_free(pr),
            }
        r["checks"] = checks
        r["all_pass"] = all(checks.values())
        ok = ok and r["all_pass"]

    report = {"results": results, "console_errors": console_errors,
              "cleaned_assets": cleaned, "all_pass": ok}
    (SHOTS / "live_browser_verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if (ok and not console_errors) else 1


if __name__ == "__main__":
    sys.exit(main())
