#!/usr/bin/env python3
"""8190 线上探活（验收口径）+ 真实浏览器 console/pageErrors + 增强注册表项数。

【覆盖】
  1. 首页 200
  2. 9 个 API 全部 200（不含只读性错误的 POST-only 接口）
  3. 13 个静态资源全部 200
  4. 真实 Chromium 打 8190，三档宽度：console error = 0、pageError = 0
  5. 增强注册表项数（前端 ENHANCE_METHODS 的真实条数 + 每一项的 id）

【只读探活】
  不改任何线上状态；只 GET。

用法：
    python3 scripts/verify_acceptance_live_8190.py [证据输出目录]
"""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8190"
OUT_DIR = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/h3-acceptance-live")
#: 截图目录跟证据目录**同源** —— 上一轮复验判过「证据文件不在报告所述路径」，
#: 根因就是截图目录硬编码在别处、报告里写的又是第三个路径。
#: 现在统一：证据目录 = OUT_DIR，截图目录 = OUT_DIR/screenshots。
SHOTS = OUT_DIR / "screenshots"
WIDTHS = [(375, 812), (390, 844), (1280, 900)]

#: 9 个 API（验收口径：全部 200）
APIS = [
    "/api/system",
    "/api/jobs",
    "/api/generation-types",
    "/api/image-options",
    "/api/image-loras",
    "/api/providers",
    "/api/enhancer",
    "/api/assets",
    "/api/music-options",
]

#: 13 个静态资源（验收口径：全部 200）
STATIC = [
    "app.js", "style.css", "index.html", "music.js", "providers.js",
    "assets.js", "moss-layout.js", "enhance-menu.js", "music-enhance.js",
    "agent.js", "agent-media.js", "responsive.css", "video-panel.js",
]


def http_code(url: str) -> tuple[int, str]:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, (r.headers.get("Content-Type", "") or "")
    except urllib.error.HTTPError as e:
        return e.code, "HTTPError"
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"[:120]


async def browser_check() -> dict:
    from playwright.async_api import async_playwright

    out: dict = {"console": [], "pageErrors": [], "viewports": {}, "shots": []}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        for width, height in WIDTHS:
            ctx = await browser.new_context(viewport={"width": width, "height": height})
            page = await ctx.new_page()
            page.on("console", lambda m, w=width: out["console"].append(
                {"width": w, "type": m.type, "text": m.text[:300]}))
            page.on("pageerror", lambda e, w=width: out["pageErrors"].append(
                {"width": w, "error": str(e)[:300]}))
            resp = await page.goto(BASE + "/", wait_until="domcontentloaded")
            await page.wait_for_timeout(3500)
            info: dict = {"http_status": resp.status if resp else None}
            # 增强注册表（前端真实注册表）
            # 增强注册表在 enhance-menu.js 的 boot() 里挂上 —— 等它就绪
            try:
                page.wait_for_function(
                    "() => window.enhanceMethods && "
                    "window.enhanceMethods.list && "
                    "window.enhanceMethods.list.length > 0",
                    timeout=15000)
            except Exception:
                pass
            info["enhance_registry"] = await page.evaluate("""() => {
              const m = window.enhanceMethods;
              const list = (m && m.list && m.list.length) ? m.list : [];
              return {
                count: list.length,
                ids: list.map(x => x.id),
                labels: list.map(x => x.label),
                current: m && m.current ? m.current : null,
              };
            }""")
            # 增强下拉实际渲染出的项数
            await page.evaluate("""() => {
              const n = document.querySelector('a.nav-item[data-mode="t2i"]');
              if (n) n.click();
            }""")
            await page.wait_for_timeout(1200)
            await page.evaluate("""() => {
              const b = document.querySelector('#enhanceMenuBtn');
              if (b) b.click();
            }""")
            await page.wait_for_timeout(1000)
            info["enhance_menu_items"] = await page.evaluate("""() => {
              const items = Array.from(document.querySelectorAll(
                '#enhanceMenu .enhance-menu-item[data-method]'));
              return {count: items.length,
                      ids: items.map(i => i.getAttribute('data-method')),
                      labels: items.map(i => {
                        const l = i.querySelector('.enhance-menu-label');
                        return l ? (l.textContent||'').trim() : '';
                      })};
            }""")
            shot = SHOTS / f"acc-live-8190-{width}.png"
            await page.screenshot(path=str(shot), full_page=True)
            out["shots"].append(str(shot))
            info["shot"] = str(shot)
            # Agent 面板
            await page.evaluate("() => window.AgentUI && window.AgentUI.show()")
            await page.wait_for_timeout(1500)
            info["agent_ready"] = await page.evaluate(
                "() => !!(window.AgentUI && window.AgentUI.state)")
            ashot = SHOTS / f"acc-live-8190-{width}-agent.png"
            await page.screenshot(path=str(ashot), full_page=True)
            out["shots"].append(str(ashot))
            out["viewports"][str(width)] = info
            await ctx.close()
        await browser.close()
    return out


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)

    ev: dict = {"base": BASE, "apis": {}, "static": {}, "index": {}}
    code, ctype = http_code(BASE + "/")
    ev["index"] = {"url": "/", "status": code, "content_type": ctype}
    print(f"  / -> {code}")

    for api in APIS:
        code, ctype = http_code(BASE + api)
        ev["apis"][api] = {"status": code, "content_type": ctype}
        print(f"  {api:<26} -> {code}")

    for f in STATIC:
        code, ctype = http_code(f"{BASE}/static/{f}")
        ev["static"][f] = {"status": code, "content_type": ctype}
        print(f"  /static/{f:<20} -> {code}")

    ev["browser"] = asyncio.run(browser_check())
    console_errors = [c for c in ev["browser"]["console"] if c["type"] == "error"]
    ev["verdict"] = {
        "index_200": ev["index"]["status"] == 200,
        "api_all_200": all(v["status"] == 200 for v in ev["apis"].values()),
        "api_count": len(ev["apis"]),
        "api_failed": [k for k, v in ev["apis"].items() if v["status"] != 200],
        "static_all_200": all(v["status"] == 200 for v in ev["static"].values()),
        "static_count": len(ev["static"]),
        "static_failed": [k for k, v in ev["static"].items() if v["status"] != 200],
        "console_errors": len(console_errors),
        "page_errors": len(ev["browser"]["pageErrors"]),
        "console_error_samples": console_errors[:5],
        "enhance_registry_count": ev["browser"]["viewports"]["1280"]
                                      ["enhance_registry"]["count"],
        "enhance_registry_ids": ev["browser"]["viewports"]["1280"]
                                 ["enhance_registry"]["ids"],
        "enhance_menu_item_count": ev["browser"]["viewports"]["1280"]
                                    ["enhance_menu_items"]["count"],
        "agent_ready_all_widths": all(
            v["agent_ready"] for v in ev["browser"]["viewports"].values()),
    }
    (OUT_DIR / "acceptance_live_8190.json").write_text(
        json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n===== 8190 线上探活判定 =====")
    print(json.dumps(ev["verdict"], ensure_ascii=False, indent=2))
    print(f"\n证据目录：{OUT_DIR}")
    ok = (ev["verdict"]["index_200"] and ev["verdict"]["api_all_200"]
          and ev["verdict"]["static_all_200"]
          and not console_errors and not ev["browser"]["pageErrors"])
    return 0 if ok else 5


if __name__ == "__main__":
    raise SystemExit(main())
