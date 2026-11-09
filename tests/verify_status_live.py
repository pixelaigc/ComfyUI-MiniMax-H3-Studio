# -*- coding: utf-8 -*-
"""验收「右上角状态要正确」：用户正在输入时，状态栏仍要实时更新。

关键取舍：状态栏只写 #statusText 和一个圆点，不碰输入框/下拉框，
所以**不应该**被交互守卫推迟 —— 否则用户停在输入框不动时状态会变陈旧。

做法：焦点停在搜索框（模拟用户正在输入），观察状态栏文本是否仍在刷新。
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190"
OUT = Path("/tmp/h3verify")
OUT.mkdir(parents=True, exist_ok=True)


def main():
    report = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(2000)

        # 焦点停在输入框（用户「正在输入」）
        page.evaluate("""() => {
          const s = document.getElementById('doneSearch');
          s.focus(); s.value = '保持焦点';
          s.dispatchEvent(new Event('input', {bubbles:true}));
        }""")
        page.wait_for_timeout(1000)

        # 采样 12 秒内的状态栏文本 + 是否触发过 /api/system
        sys_calls = []
        page.on("request", lambda r: sys_calls.append(r.url)
                if "/api/system" in r.url else None)
        samples = []
        for _ in range(12):
            page.wait_for_timeout(1000)
            samples.append({
                "t": time.time(),
                "status": page.evaluate(
                    "() => (document.getElementById('statusText')||{}).textContent"),
                "focused": page.evaluate(
                    "() => document.activeElement ? document.activeElement.id : ''"),
                "input_value": page.evaluate(
                    "() => (document.getElementById('doneSearch')||{}).value"),
            })
        page.screenshot(path=str(OUT / "08_status_while_typing.png"))

        statuses = [s["status"] for s in samples]
        report["status_updates_while_typing"] = {
            "api_system_calls": len(sys_calls),
            "distinct_status_values": sorted(set(statuses)),
            "status_never_null": all(s is not None for s in statuses),
            "input_value_kept": len(set(s["input_value"] for s in samples)) == 1
                                and samples[0]["input_value"] == "保持焦点",
            "focus_kept": all(s["focused"] == "doneSearch" for s in samples),
            "samples": statuses,
        }
        report["pageerrors"] = errors
        (OUT / "status_live.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        browser.close()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    r = report["status_updates_while_typing"]
    ok = r["api_system_calls"] >= 3 and r["status_never_null"] and r["input_value_kept"]
    print("OVERALL:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
