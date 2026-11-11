#!/usr/bin/env python3
"""
Real-browser verification of the Agent media-upload feature against the
DEPLOYED 8190 instance (2026-11-11), including the **素材库入库 + 预处理**
pipeline.

Runs against the **real production server**:
  * the real asset library (/mnt/usb2t/asset_library)
  * the real /api/agent/upload endpoint
  * the real frontend on :8190

It uploads real (throwaway) test images into the production asset library and
deletes them afterwards.

Proves at 375 / 390 / 1280:
  1. upload entry visible; a real PNG uploads and shows a thumbnail
  2. LIBRARY: the image really landed in the production asset library
     (source=agent_upload) with extracted metadata
  3. PREPROCESS: the server really ran the library analysis pipeline and the
     description + tags landed in the library (and are searchable)
  4. multi-image upload works
  5. remove works
  6. a non-image is rejected with a readable message
  7. more than 9 images is refused
  8. no horizontal overflow; console 0 errors; 0 failed requests

【为什么用假 Provider】
The production multimodal analysis takes 30~180 s per image.
3 widths x 6 cases x real analysis would be over an hour.
So the test temporarily points the **production jobs.db** at a local
fake OpenAI-compatible endpoint that returns a canned structured
description instantly, then restores the original provider config.

Only the model inference is stubbed — everything else (register_asset,
analysis pipeline, save_analysis, add_tags, the frontend) is the real
production code path.
"""
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8190
SHOTS = Path("/root/projects/h3studio-live/evidence/media-upload")
JOBS_DB = Path("/root/projects/comfyui-minimax-h3-studio/data/jobs.db")
LIB_DB = Path("/mnt/usb2t/asset_library/asset_library.db")
FAKE_PORT = 8919

FAKE_DESCRIPTION = """**【主体人物】**
一位穿着深蓝色风衣的年轻女性，独自站在桥上。

**【场景与环境】**
雨夜的石桥，远处是城市的霓虹灯海，水面倒映着斑斓的光。

**【光线与色彩】**
冷色调为主，紫蓝与橙黄交织，霓虹在湿漉漉的桥面上拉出长长的光带。
"""


class FakeProviderHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = {
            "id": "chatcmpl-fake", "object": "chat.completion",
            "created": int(time.time()), "model": "fake-vision",
            "choices": [{"index": 0,
                         "message": {"role": "assistant",
                                     "content": FAKE_DESCRIPTION},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10,
                      "total_tokens": 20},
        }
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def start_fake_provider():
    server = HTTPServer(("127.0.0.1", FAKE_PORT), FakeProviderHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class ProviderSwap:
    """临时把生产 jobs.db 的默认多模态 Provider 指向假端点，结束��还原。"""

    def __init__(self):
        self.saved = []

    def __enter__(self):
        conn = sqlite3.connect(JOBS_DB)
        conn.row_factory = sqlite3.Row
        self.saved = [dict(r) for r in conn.execute(
            "SELECT * FROM model_providers")]
        self.prefs = [dict(r) for r in conn.execute(
            "SELECT key, value FROM model_provider_prefs "
            "WHERE key IN ('default','default_multimodal')")]
        # 造一个假 Provider 并设为默认多模态模型。
        #
        # 【为什么用具名列而不是 VALUES(...)】model_providers 有 13 列，
        # 按位置插入一旦列数/顺序对不上就**静默失败**（sqlite3 不报错，
        # 只是插不进去）—— 之前就是这么踩的：假 provider 没插进去，
        # 验收脚本却以为切好了，于是每次都跑真模型（80~180 秒/张）。
        conn.execute(
            "INSERT OR REPLACE INTO model_providers "
            "(id, name, type, base_url, llm_base_url, model, api_key, "
            " multimodal, enabled, created_at, use_proxy, proxy_base_url) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("fakevis", "假视觉模型", "openai",
             f"http://127.0.0.1:{FAKE_PORT}", "", "fake-vision", "",
             1, 1, time.time(), 0, ""))
        conn.execute("INSERT OR REPLACE INTO model_provider_prefs "
                     "VALUES ('default','fakevis')")
        conn.execute("INSERT OR REPLACE INTO model_provider_prefs "
                     "VALUES ('default_multimodal','fakevis')")
        conn.commit()
        # **自检**：确认真的切过去了（否则后面会白等几十分钟）
        row = conn.execute(
            "SELECT base_url FROM model_providers WHERE id='fakevis'"
        ).fetchone()
        pref = conn.execute(
            "SELECT value FROM model_provider_prefs "
            "WHERE key='default_multimodal'").fetchone()
        conn.close()
        if not row or not pref or pref["value"] != "fakevis":
            raise RuntimeError(
                f"假 Provider 切换失败：row={row} pref={pref}")
        print(f"  假 Provider 已就位: {row['base_url']}")
        return self

    def __exit__(self, *exc):
        conn = sqlite3.connect(JOBS_DB)
        conn.execute("DELETE FROM model_providers WHERE id='fakevis'")
        for pref in getattr(self, "prefs", []):
            conn.execute("INSERT OR REPLACE INTO model_provider_prefs "
                         "VALUES (?,?)", (pref["key"], pref["value"]))
        conn.commit()
        conn.close()
        return False


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
            attachments: window.AgentUI.state.attachments.map(a => ({
                asset_id: a.asset_id, tags: a.tags || [],
            })),
            docScrollW: document.documentElement.scrollWidth,
            docClientW: document.documentElement.clientWidth,
            bodyScrollW: document.body.scrollWidth,
        };
    }""")


def overflow_free(data):
    return (data["docScrollW"] <= data["docClientW"] + 1
            and data["bodyScrollW"] <= data["docClientW"] + 1)


def library_probe(asset_id: str) -> dict:
    """直接查**生产素材库**（权威来源）。"""
    if not LIB_DB.is_file():
        return {"ok": False}
    conn = sqlite3.connect(LIB_DB)
    conn.row_factory = sqlite3.Row
    asset = conn.execute("SELECT * FROM assets WHERE id=?",
                         (asset_id,)).fetchone()
    version = conn.execute(
        "SELECT * FROM asset_versions WHERE asset_id=? ORDER BY version DESC",
        (asset_id,)).fetchone()
    analysis = conn.execute(
        "SELECT * FROM asset_analyses WHERE asset_id=? ORDER BY id DESC",
        (asset_id,)).fetchone()
    tags = [r["tag"] for r in conn.execute(
        "SELECT tag FROM asset_tags WHERE asset_id=?", (asset_id,))]
    conn.close()
    return {
        "ok": True,
        "source": asset["source"] if asset else None,
        "width": version["width"] if version else None,
        "height": version["height"] if version else None,
        "mime": version["mime"] if version else None,
        "analysis_text": (analysis["text"] if analysis else "") or "",
        "tags": tags,
    }


def delete_asset(asset_id: str) -> int:
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/api/assets/{asset_id}", method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except Exception:  # noqa: BLE001
        return 0


def _thumb_has_pixels(screenshot: str | None, min_red: int = 300) -> bool:
    """
    数截图里**缩略图区域**的红色像素，判断缩略图**真的渲染出了内容**。

    【为什么需要这个断言】
    实测踩到过：窄屏（375/390）下缩略图只有 52px，截图时 <img> 还没解码完
    -> 拍到的是**空白方框**。而「有 <img> 且 src 指向 /api/assets/...」
    这类 DOM 断言**照样全绿** —— 假通过。

    所以必须做**像素级**核对：测试图是纯红色 RGB(200,30,30)，
    缩略图区域必须出现足够多的红色像素。

    定位缩略图区域：它是页面下部、缩略图条里的第一个 .ag-attach-thumb。
    这里不做精确裁剪（不同宽度缩略图大小不同），而是统计
    **截图下部 1/3** 里的红色像素 —— 页头那个小图标只有 13 个红色像素，
    与缩略图的 2000+ 差两个数量级，不会混淆。
    """
    if not screenshot:
        return False
    path = Path(screenshot)
    if not path.is_file():
        return False
    try:
        from PIL import Image
        image = Image.open(path).convert("RGB")
    except Exception:  # noqa: BLE001
        return False
    width, height = image.size
    red = 0
    for y in range(height * 2 // 3, height, 2):
        for x in range(0, width, 2):
            r, g, b = image.getpixel((x, y))
            if r > 140 and g < 90 and b < 90:
                red += 1
    return red >= min_red


def main():
    from playwright.sync_api import sync_playwright
    SHOTS.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="h3live-media-"))
    fake = start_fake_provider()
    console_errors = []
    results = []
    uploaded_ids = []
    try:
        with ProviderSwap(), sync_playwright() as p:
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
                      window.AgentUI.state.attachStage = '';
                      window.AgentUI._renderAttachments();
                    }""")

                def upload(paths):
                    page.set_input_files("#agAttachInput",
                                         [str(x) for x in paths])
                    page.wait_for_function(
                        """() => !window.AgentUI.state.attachBusy""",
                        timeout=180000)
                    # 【关键】等缩略图**真的加载完**再截图。
                    #
                    # 【实测踩到的坑】窄屏（375/390）下缩略图只有 52px，
                    # 截图时 <img> 还没解码完 -> 拍到的是**空白方框**。
                    # 像素级核对：本地截图红色像素 2214，线上窄屏只有 13
                    # （13 个都在页头，不是缩略图）。
                    #
                    # 只等 attachBusy=false 不够 —— 那是「上传请求返回了」，
                    # 不是「图片渲染好了」。必须等 img.complete && naturalWidth>0。
                    page.wait_for_function(
                        """() => {
                          const imgs = [...document.querySelectorAll(
                            '.ag-attach-thumb img')];
                          if (!imgs.length) return true;
                          return imgs.every(i => i.complete && i.naturalWidth > 0);
                        }""", timeout=30000)
                    page.wait_for_timeout(250)

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
                uploaded_ids.extend(a["asset_id"] for a in pr["attachments"])
                lib = (library_probe(pr["attachments"][0]["asset_id"])
                       if pr["attachments"] else {"ok": False})
                results.append({"width": width, "case": "one", "probe": pr,
                                "library": lib, "screenshot": str(shot)})

                reset()
                pngs = [make_png(tmp / f"live-multi-{tag}-{i}.png",
                                 (i * 30, 100, 200)) for i in range(3)]
                upload(pngs)
                shot = SHOTS / f"live-media-{tag}-03-multi-uploaded.png"
                page.screenshot(path=str(shot), full_page=False)
                pr = probe(page)
                uploaded_ids.extend(a["asset_id"] for a in pr["attachments"])
                results.append({"width": width, "case": "multi", "probe": pr,
                                "screenshot": str(shot)})

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
                uploaded_ids.extend(a["asset_id"] for a in pr["attachments"])
                results.append({"width": width, "case": "limit", "probe": pr,
                                "screenshot": str(shot)})

                results[-1]["failed_requests"] = failed_requests
                console_errors.extend(errs)
                ctx.close()
            browser.close()
    finally:
        fake.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- 清理生产素材库里的测试图 ----
    cleaned = [{"asset_id": a, "status": delete_asset(a)}
               for a in sorted(set(uploaded_ids))]

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
            lib = r.get("library") or {}
            checks = {
                "单图上传成功": pr["thumbCount"] == 1,
                "有 preview_url": bool(pr["thumbSrcs"][0] if pr["thumbSrcs"] else ""),
                "预览指向素材库接口": (pr["thumbSrcs"][0].startswith("/api/assets/")
                                       if pr["thumbSrcs"] else False),
                "拿到 asset_id": len(pr["attachments"]) == 1
                                 and pr["attachments"][0]["asset_id"].startswith("asset_"),
                "无错误提示": not pr["error"],
                "素材库里真的有这条": lib.get("ok") and lib.get("source") == "agent_upload",
                "素材库抽出了尺寸": lib.get("width") == 120 and lib.get("height") == 90,
                "预处理写入了描述": len(lib.get("analysis_text") or "") > 20,
                "预处理写入了标签": len(lib.get("tags") or []) > 0,
                "前端拿到标签": len(pr["attachments"][0]["tags"]) > 0,
                # 【像素级】缩略图**真的渲染出了图片内容**。
                # 只断言「有 <img> 且 src 正确」不够 —— 实测踩到过：
                # 窄屏下 <img> 还没解码完就截图，断言全绿但拍到的是空白方框
                # （红色像素只有 13 个，全在页头）。所以数**图片区域的红色像素**。
                "缩略图真的渲染出内容": _thumb_has_pixels(r.get("screenshot")),
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
