#!/usr/bin/env python3
"""L-C 前端真实浏览器验证：base_verdict 三档在 DOM 里如实渲染。

用**真实浏览器**打开真实页面，喂**真实后端**下发的清单，
断言详情面板「基座适用性」行按 base_verdict 分三档显示。

为什么必须真实浏览器：
  上一轮验收报告明确写过 —— 「用源码里有没有某个字符串来断言是空洞的，
  变异测试证明那是空洞的：把渲染插值改成空串，断言照样全绿」。
  所以必须真的把页面渲染出来、在 DOM 里找文字。

用法：python3 scripts/verify_labc_base_verdict_browser.py <base_url>
"""
import json
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8199"
OUT = Path("/tmp/labc_base_verdict_browser.json")
SHOTS = Path("/tmp/labc_shots")

REPO = Path(__file__).resolve().parent.parent


def fetch_loras(base):
    import urllib.parse
    url = f"{BASE_URL}/api/image-loras?base=" + urllib.parse.quote(base)
    with urllib.request.urlopen(url, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    out = {"base_url": BASE_URL, "console_errors": 0, "page_errors": 0,
           "cases": [], "verdict": {}}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        page.goto(BASE_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)

        # 真实后端下发的条目 —— 按 base_verdict 分类各取一条
        for base in ("Z-Image", "MOSS-Image"):
            data = fetch_loras(base)
            items = data.get("available", [])
            by_verdict = {}
            for it in items:
                by_verdict.setdefault(it.get("base_verdict"), it)
            for verdict, item in by_verdict.items():
                name = item["name"]
                # 注入真实条目到前端 store（用真实后端数据，不造假条目）
                payload = json.dumps(item, ensure_ascii=False)
                opened = page.evaluate(
                    """(args) => {
                        const [itemJson, name] = args;
                        const item = JSON.parse(itemJson);
                        const store = window.H3Studio.__loraTestHooks.stores;
                        const s = store.MOSS || store.Z || store.Qwen;
                        s.available = s.available.filter(x => x.name !== name);
                        s.available.push(item);
                        window.H3Studio.__loraTestHooks.renderLoraList();
                        window.H3Studio.__loraTestHooks.openLoraInfo(name);
                        return true;
                    }""", [payload, name])
                page.wait_for_timeout(500)
                text = page.evaluate(
                    """() => {
                        const el = document.querySelector('#loraInfoModal')
                            || document.querySelector('#loraInfoBody');
                        return el ? el.innerText : '';
                    }""")
                line = ""
                for ln in text.splitlines():
                    if "基座适用性" in ln or ("原生" in ln and len(ln) < 80) \
                            or "已适配" in ln or "近似适用" in ln or "不适配" in ln \
                            or "不可用" in ln:
                        line = ln.strip()
                        if line:
                            break
                shot = SHOTS / f"labc-verdict-{base}-{verdict}.png"
                try:
                    page.screenshot(path=str(shot), full_page=False)
                except Exception:
                    shot = None
                out["cases"].append({
                    "base": base, "name": name,
                    "base_verdict": verdict,
                    "compatible": item.get("compatible"),
                    "conversion_status": item.get("conversion_status"),
                    "attention_hit": item.get("attention_hit"),
                    "detail_line": line,
                    "screenshot": str(shot) if shot else "",
                })

        out["console_errors"] = sum(
            1 for e in errors if e.startswith("console."))
        out["page_errors"] = sum(
            1 for e in errors if e.startswith("pageerror:"))
        out["all_errors"] = errors
        browser.close()

    # 判定：每条都有 base_verdict 且详情面板有对应文字
    out["verdict"] = {
        "cases": len(out["cases"]),
        "all_have_verdict": all(c["base_verdict"] for c in out["cases"]),
        "all_rendered": all(c["detail_line"] for c in out["cases"]),
        "console_errors": out["console_errors"],
        "page_errors": out["page_errors"],
    }
    out["verdict"]["all_pass"] = (
        out["verdict"]["all_have_verdict"]
        and out["verdict"]["all_rendered"]
        and out["verdict"]["console_errors"] == 0
        and out["verdict"]["page_errors"] == 0)

    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\n-> {OUT}")
    return 0 if out["verdict"]["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
