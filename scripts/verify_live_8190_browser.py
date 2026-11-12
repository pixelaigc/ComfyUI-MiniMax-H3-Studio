#!/usr/bin/env python3
"""部署后线上验证：真实 Chromium 打 8190（真线上服务），三档宽度。

验证：
  * 页面加载 0 console error / 0 pageError
  * 增强下拉里 yue2_struct 的归属徽标 == 「YuE2 音乐专属」（本轮修的那条）
  * 三个徽标文案逐条读出
  * Agent 面板能打开、工具区就绪
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

BASE = "http://127.0.0.1:8190"
OUT = Path("/tmp/h3-fix-evidence")
SHOTS = Path("/root/.hermes-web-ui/.ekko/workspace/default/"
              "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/agent-enh-merge")
VIEWPORTS = [(375, 812), (390, 844), (1280, 900)]


async def main() -> int:
    from playwright.async_api import async_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    ev: dict = {"base": BASE, "viewports": {}, "console": [], "pageErrors": [], "shots": []}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        for w, h in VIEWPORTS:
            ctx = await browser.new_context(viewport={"width": w, "height": h})
            page = await ctx.new_page()
            page.on("console", lambda m, w=w: ev["console"].append(
                {"width": w, "type": m.type, "text": m.text[:300]}))
            page.on("pageerror", lambda e, w=w: ev["pageErrors"].append(
                {"width": w, "error": str(e)[:300]}))
            resp = await page.goto(BASE + "/", wait_until="domcontentloaded")
            await page.wait_for_timeout(3500)
            info = {
                "http_status": resp.status if resp else None,
            }
            # 打开增强下拉（图像面板），读归属徽标
            await page.evaluate("""() => {
              const n = document.querySelector('a.nav-item[data-mode="t2i"]');
              if (n) n.click();
            }""")
            await page.wait_for_timeout(1500)
            await page.evaluate("""() => {
              const b = document.querySelector('#enhanceMenuBtn');
              if (b) b.click();
            }""")
            await page.wait_for_timeout(1200)
            info["badges"] = await page.evaluate("""() => {
              const items = Array.from(document.querySelectorAll(
                '#enhanceMenu .enhance-menu-item[data-method]'));
              return items.map(i => ({
                method: i.getAttribute('data-method'),
                badge: (i.querySelector('.enhance-menu-badge')||{}).textContent||'',
                disabled: !!i.querySelector('input[type=checkbox]:disabled'),
              }));
            }""")
            shot = SHOTS / f"live-8190-{w}-enhance-menu.png"
            await page.screenshot(path=str(shot), full_page=True)
            ev["shots"].append(str(shot))
            # 单独截「增强下拉菜单」这个元素本身 —— 整页截图里菜单常常被
            # 裁掉一截，看不清徽标文案。
            try:
                menu_el = await page.query_selector("#enhanceMenu")
                if menu_el:
                    # 菜单内部可滚动 —— 徽标项在下面，先滚到底再截
                    await page.evaluate("""() => {
                      const m = document.querySelector('#enhanceMenu');
                      if (m) m.scrollTop = m.scrollHeight;
                    }""")
                    await page.wait_for_timeout(500)
                    mshot = SHOTS / f"live-8190-{w}-enhance-menu-el.png"
                    await menu_el.screenshot(path=str(mshot))
                    ev["shots"].append(str(mshot))
                    await page.evaluate("""() => {
                      const m = document.querySelector('#enhanceMenu');
                      if (m) m.scrollTop = 0;
                    }""")
                    # 单独截「YuE2 结构化风格 + 歌词」那一项 —— 徽标就在它右边
                    try:
                        item = await page.query_selector(
                            '#enhanceMenu .enhance-menu-item[data-method="yue2_struct"]')
                        if item:
                            await item.scroll_into_view_if_needed()
                            await page.wait_for_timeout(400)
                            ishot = SHOTS / f"live-8190-{w}-yue2-item.png"
                            await item.screenshot(path=str(ishot))
                            ev["shots"].append(str(ishot))
                    except Exception:
                        pass
            except Exception:
                pass
            # 关菜单 + 开 Agent 面板
            await page.evaluate("""() => {
              const b = document.querySelector('#enhanceMenuBtn');
              if (b) b.click();
              if (window.AgentUI && window.AgentUI.show) window.AgentUI.show();
            }""")
            await page.wait_for_timeout(1800)
            info["agent_ready"] = await page.evaluate(
                "() => !!(window.AgentUI && window.AgentUI.state)")
            shot2 = SHOTS / f"live-8190-{w}-agent.png"
            await page.screenshot(path=str(shot2), full_page=True)
            ev["shots"].append(str(shot2))
            ev["viewports"][str(w)] = info
            yue2 = next((b for b in info["badges"] if b["method"] == "yue2_struct"), None)
            print(f"  [{w}] http={info['http_status']} agent={info['agent_ready']} "
                  f"yue2_badge={yue2 and yue2['badge']!r}", flush=True)
            await ctx.close()
        await browser.close()

    ev["console_errors"] = [c for c in ev["console"] if c["type"] == "error"]
    ev["page_error_count"] = len(ev["pageErrors"])
    (OUT / "live_8190_browser.json").write_text(
        json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")

    # 判定：三个宽度的 yue2 徽标都必须 ==「YuE2 音乐专属」
    ok_badge = True
    for w, info in ev["viewports"].items():
        yue2 = next((b for b in info["badges"] if b["method"] == "yue2_struct"), None)
        badge = (yue2 or {}).get("badge", "")
        if "音乐专属" not in badge:
            ok_badge = False
            print(f"  !! [{w}] 徽标不对：{badge!r}")
    ev["verdict"] = {
        "yue2_badge_correct_all_widths": ok_badge,
        "console_errors": len(ev["console_errors"]),
        "page_errors": ev["page_error_count"],
        "agent_ready_all_widths": all(
            i.get("agent_ready") for i in ev["viewports"].values()),
    }
    (OUT / "live_8190_browser.json").write_text(
        json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== 线上浏览器验证判定 ===")
    print(json.dumps(ev["verdict"], ensure_ascii=False, indent=2))
    for s in ev["shots"]:
        print("  " + s)
    return 0 if (ok_badge and not ev["console_errors"]
                 and not ev["pageErrors"]) else 5


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
