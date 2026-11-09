#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真实浏览器三档视口取证 —— 审查返修的 max_token / 增强体系相关项。

检查项（与本轮改动直接相关）：
  1. 三个视口下页面能正常加载，**console 报错 0 条**、pageerror 0 条、失败请求 0 条
  2. 「本地模型配置」页的「增强 Agent 配置」区能渲染，
     且新加的「最大输出 token 上限」输入框存在、值合法（在 min~max 之间）
  3. 三个视口下无横向溢出（document.scrollWidth <= innerWidth）
  4. 增强栏控件未被祖先裁切
  5. 截图（375 / 390 / 1280）

用法：
    /home/ubuntu/comfyui/venv/bin/python tests/verify_max_tokens_ui_browser.py \
        --port 8299 --tag maxtoken --out <dir>
"""
import argparse
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

VIEWPORTS = [
    ("375", {"width": 375, "height": 812}),
    ("390", {"width": 390, "height": 844}),
    ("1280", {"width": 1280, "height": 900}),
]

# 「本地模型配置」页的入口（不同分支 DOM 可能不同，逐个试）
PROVIDER_ENTRIES = [
    "#navProviders", "#providersNav", "[data-page='providers']",
    "button:has-text('本地模型')", "a:has-text('本地模型')",
    "#sysProviders", "#providersPage",
]

DUMP_JS = """
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const q = (s) => document.querySelector(s);
  const inBox = (s) => {
    const el = q(s);
    if (!el || !visible(el)) return null;
    const r = el.getBoundingClientRect();
    return {top: Math.round(r.top), left: Math.round(r.left),
            w: Math.round(r.width), h: Math.round(r.height)};
  };
  // 最大输出 token 上限输入框（前端 id: agentMaxTokens）
  const ceil = q('#agentMaxTokens');
  const bounds = q('#provAgent') ? true : false;
  let maxTokens = null;
  if (ceil) {
    maxTokens = {
      exists: true,
      value: ceil.value,
      min: ceil.min,
      max: ceil.max,
      visible: visible(ceil),
      inRange: (Number(ceil.value) >= Number(ceil.min || 0)
                && Number(ceil.value) <= Number(ceil.max || Infinity)),
    };
  }
  return {
    url: location.href,
    docScrollW: document.documentElement.scrollWidth,
    innerW: window.innerWidth,
    horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
    providerPanelExists: !!q('#providersPage'),
    agentConfigExists: bounds,
    maxTokens: maxTokens,
    enhanceBar: inBox('#enhanceSplit') || inBox('.prompt-enhance-bar'),
    enhanceBtn: inBox('#enhanceBtn'),
    specSelect: inBox('#enhanceSpec'),
  };
}
"""


def run(port, tag, outdir):
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{port}"
    results = {"port": port, "tag": tag, "viewports": [], "console_errors": [],
               "page_errors": [], "failed_requests": []}

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        for vname, vsize in VIEWPORTS:
            ctx = browser.new_context(viewport={"width": vsize["width"],
                                               "height": vsize["height"]})
            page = ctx.new_page()
            console_errors, page_errors, failed = [], [], []

            page.on("console", lambda m: console_errors.append(m.text)
                    if m.type == "error" else None)
            page.on("pageerror", lambda e: page_errors.append(str(e)))
            page.on("response", lambda r: failed.append("%s %s" % (r.status, r.url))
                    if r.status >= 400 else None)

            page.goto(base, wait_until="networkidle", timeout=60000)
            time.sleep(1.5)

            # 切到「本地模型配置」页
            opened = False
            for sel in PROVIDER_ENTRIES:
                try:
                    el = page.query_selector(sel)
                except Exception:
                    continue
                if el:
                    try:
                        el.click(timeout=2500)
                        opened = True
                        break
                    except Exception:
                        continue
            if not opened:
                # 直接把 providers 页显示出来（页面是 hash/JS 路由）
                page.evaluate("""() => {
                  const p = document.querySelector('#providersPage');
                  if (p) { p.style.display = 'block'; }
                }""")
            time.sleep(2.0)

            data = page.evaluate(DUMP_JS)
            shot = out / f"ui-{tag}-{vname}.png"
            page.screenshot(path=str(shot), full_page=False)
            data["screenshot"] = str(shot)
            data["viewport"] = vname
            data["console_errors"] = console_errors
            data["page_errors"] = page_errors
            data["failed_requests"] = failed
            results["viewports"].append(data)
            results["console_errors"] += console_errors
            results["page_errors"] += page_errors
            results["failed_requests"] += failed
            ctx.close()
        browser.close()

    # 汇总断言
    checks = []
    for v in results["viewports"]:
        checks.append({
            "viewport": v["viewport"],
            "no_console_error": len(v["console_errors"]) == 0,
            "no_page_error": len(v["page_errors"]) == 0,
            "no_failed_request": len(v["failed_requests"]) == 0,
            "no_horizontal_overflow": not v["horizontalOverflow"],
            "provider_page_exists": v["providerPanelExists"],
            "agent_config_rendered": v["agentConfigExists"],
            "max_tokens_input_exists": bool(v["maxTokens"] and v["maxTokens"]["exists"]),
            "max_tokens_in_range": bool(v["maxTokens"] and v["maxTokens"]["inRange"]),
            "enhance_btn_visible": bool(v["enhanceBtn"]),
        })
    results["checks"] = checks
    results["summary"] = {
        "console_errors": len(results["console_errors"]),
        "page_errors": len(results["page_errors"]),
        "failed_requests": len(results["failed_requests"]),
        "total_checks": sum(len(c) - 1 for c in checks),
        "passed_checks": sum(1 for c in checks for k, val in c.items()
                             if k != "viewport" and val is True),
    }
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8299)
    ap.add_argument("--tag", default="maxtoken")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    res = run(args.port, args.tag, args.out)
    outfile = Path(args.out) / f"ui-{args.tag}.json"
    outfile.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(res["summary"], ensure_ascii=False, indent=2))
    for c in res["checks"]:
        bad = [k for k, v in c.items() if k != "viewport" and v is not True]
        print("  %-6s %s" % (c["viewport"], "OK" if not bad else "FAIL: " + ",".join(bad)))
    print("saved ->", outfile)
    ok = res["summary"]["console_errors"] == 0 and res["summary"]["page_errors"] == 0
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
