#!/usr/bin/env python3
"""L-C 前端真实浏览器验证：base_verdict 三档在 DOM 里如实渲染。

用**真实浏览器**打开真实页面，喂**真实后端**下发的清单，
断言详情面板「基座适用性」行按 base_verdict 分档显示。

为什么必须真实浏览器：
  上一轮验收报告明确写过 —— 「用源码里有没有某个字符串来断言是空洞的，
  变异测试证明那是空洞的：把渲染插值改成空串，断言照样全绿」。
  所以必须真的把页面渲染出来、在 DOM 里找文字。

【关键】前端 `imgLoraMeta()` 按**当前模式**取 store
（`imgLoraCtx()`：Z-Image / Qwen / MOSS 各一份），
所以必须**先切到对应模式**，再注入条目、再打开详情。

用法：python3 scripts/verify_labc_base_verdict_browser.py <base_url>
"""
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8199"
OUT = Path("/tmp/labc_base_verdict_browser.json")
SHOTS = Path("/tmp/labc_shots")

#: base -> (前端模式名, 前端 store key)
BASE_TO_MODE = {
    "Z-Image": ("t2i", "Z"),
    "Qwen-Image 2.1": ("t2i_qwen", "Qwen"),
    "MOSS-Image": ("t2i_moss", "MOSS"),
}


def fetch_loras(base):
    url = f"{BASE_URL}/api/image-loras?base=" + urllib.parse.quote(base)
    with urllib.request.urlopen(url, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    out = {"base_url": BASE_URL, "console_errors": 0, "page_errors": 0,
           "cases": [], "verdict": {}, "all_errors": []}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        page.goto(BASE_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(7000)

        for base, (mode, store_key) in BASE_TO_MODE.items():
            # 1) 切到对应模式（前端按模式选 store）
            page.evaluate(
                """(mode) => { window.activateVideoMode ? null : null;
                    if (window.H3Studio && window.H3Studio.__loraTestHooks) {
                        // 直接切模式：复用页面的 activateMode（若暴露）
                    }
                }""", mode)
            # 切模式：用页面 DOM 上的导航项点击（真实用户路径）
            clicked = page.evaluate(
                """(mode) => {
                    const items = [...document.querySelectorAll('[data-mode]')];
                    const el = items.find(x => x.getAttribute('data-mode') === mode);
                    if (el) { el.click(); return true; }
                    return false;
                }""", mode)
            page.wait_for_timeout(1200)

            data = fetch_loras(base)
            items = data.get("available", [])
            by_verdict = {}
            for it in items:
                by_verdict.setdefault(it.get("base_verdict"), it)

            for verdict, item in by_verdict.items():
                name = item["name"]
                payload = json.dumps(item, ensure_ascii=False)
                ok = page.evaluate(
                    """(args) => {
                        const [itemJson, name, storeKey] = args;
                        const item = JSON.parse(itemJson);
                        const store = window.H3Studio.__loraTestHooks.stores[storeKey];
                        if (!store) return 'NO_STORE';
                        store.available = store.available.filter(x => x.name !== name);
                        store.available.push(item);
                        window.H3Studio.__loraTestHooks.renderLoraList();
                        window.H3Studio.__loraTestHooks.openLoraInfo(name);
                        return 'OK';
                    }""", [payload, name, store_key])
                page.wait_for_timeout(600)
                text = page.evaluate(
                    """() => {
                        const el = document.querySelector('#loraInfoBody');
                        return el ? el.innerText : '';
                    }""")
                # 精确取「基座适用性」那一行（dl 里 dt/dd 相邻）
                verdict_text = page.evaluate(
                    """() => {
                        const dts = [...document.querySelectorAll('#loraInfoBody dt')];
                        const dt = dts.find(x => x.textContent.includes('基座适用性'));
                        return dt && dt.nextElementSibling
                            ? dt.nextElementSibling.textContent.trim() : '';
                    }""")
                shot = SHOTS / f"labc-verdict-{base}-{verdict}.png"
                try:
                    page.screenshot(path=str(shot))
                except Exception:
                    shot = None
                out["cases"].append({
                    "base": base, "mode": mode, "store": store_key,
                    "mode_clicked": clicked, "inject": ok,
                    "name": name,
                    "base_verdict": verdict,
                    "compatible": item.get("compatible"),
                    "conversion_status": item.get("conversion_status"),
                    "attention_hit": item.get("attention_hit"),
                    "verdict_text": verdict_text,
                    "body_has_verdict": verdict in (text or ""),
                    "screenshot": str(shot) if shot else "",
                })

        out["console_errors"] = sum(
            1 for e in errors if e.startswith("console."))
        out["page_errors"] = sum(
            1 for e in errors if e.startswith("pageerror:"))
        out["all_errors"] = errors
        browser.close()

    out["verdict"] = {
        "cases": len(out["cases"]),
        "all_have_verdict": all(c["base_verdict"] for c in out["cases"]),
        "all_rendered": all(c["verdict_text"] for c in out["cases"]),
        "all_match_api": all(c["body_has_verdict"] for c in out["cases"]),
        "console_errors": out["console_errors"],
        "page_errors": out["page_errors"],
    }
    out["verdict"]["all_pass"] = (
        out["verdict"]["all_have_verdict"]
        and out["verdict"]["all_rendered"]
        and out["verdict"]["all_match_api"]
        and out["verdict"]["console_errors"] == 0
        and out["verdict"]["page_errors"] == 0)

    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\n-> {OUT}")
    return 0 if out["verdict"]["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
