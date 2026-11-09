# -*- coding: utf-8 -*-
"""验收素材库的实时刷新：后台新增素材 -> 素材库自动出现新卡片。

做法：直接往素材库里注册一个新素材（真实 API：POST /api/assets 上传一张小图），
等素材库轮询周期（4 秒），确认新卡片自动出现在素材库网格里。

用法: python3 tests/verify_assets_live.py [base_url]
"""
import io
import json
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190"
OUT = Path("/tmp/h3verify")
OUT.mkdir(parents=True, exist_ok=True)

# 1x1 PNG
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


def upload_asset(name):
    """用 multipart 上传一张 1x1 PNG，返回素材 id。

    响应格式：{"results": [{"ok":true, "asset_id": "asset_xxx", ...}], ...}
    """
    boundary = "----h3probe" + str(int(time.time()))
    body = io.BytesIO()
    def field(k, v):
        body.write(f"--{boundary}\r\n".encode())
        body.write(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        body.write(v.encode() if isinstance(v, str) else v)
        body.write(b"\r\n")
    field("tags", "实时探针")
    body.write(f"--{boundary}\r\n".encode())
    body.write(f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'.encode())
    body.write(b"Content-Type: image/png\r\n\r\n")
    body.write(PNG)
    body.write(b"\r\n")
    body.write(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        BASE + "/api/assets", data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode())
    ids = []
    for item in (payload.get("results") or []):
        if item.get("ok") and item.get("asset_id"):
            ids.append(item["asset_id"])
    return ids


def main():
    report = {}
    try:
        ids = upload_asset("liveprobe_asset.png")
        report["upload_result"] = {"ok": bool(ids), "ids": ids}
        new_id = ids[0] if ids else None
    except Exception as e:
        report["upload_result"] = {"ok": False, "error": str(e)}
        new_id = None

    if not new_id:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("OVERALL: FAIL (upload failed)")
        return 1

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1500)
        page.evaluate("() => { const b = document.getElementById('navAssets'); if (b) b.click(); }")
        page.wait_for_timeout(3000)
        before = page.evaluate("""() => {
          const g = document.getElementById('alGrid');
          return {count: g ? g.querySelectorAll('.al-card').length : 0,
                  has: !!(g && g.querySelector('[data-id="%s"]'))};
        }""" % new_id)
        page.screenshot(path=str(OUT / "07_assets_live_before.png"))

        appeared = False
        waited = 0
        while waited < 20000:
            page.wait_for_timeout(500)
            waited += 500
            has = page.evaluate(
                """() => !!(document.getElementById('alGrid')
                    && document.getElementById('alGrid').querySelector('[data-id="%s"]'))"""
                % new_id)
            if has:
                appeared = True
                break
        after = page.evaluate("""() => {
          const g = document.getElementById('alGrid');
          return {count: g ? g.querySelectorAll('.al-card').length : 0,
                  has: !!(g && g.querySelector('[data-id="%s"]'))};
        }""" % new_id)
        page.screenshot(path=str(OUT / "07_assets_live_after.png"))
        report["asset_library_live_update"] = {
            "count_before": before["count"], "count_after": after["count"],
            "new_card_appeared": appeared, "waited_ms": waited,
        }
        report["pageerrors"] = errors
        (OUT / "assets_live.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        browser.close()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("OVERALL:", "PASS" if appeared else "FAIL")
    return 0 if appeared else 1


if __name__ == "__main__":
    sys.exit(main())
