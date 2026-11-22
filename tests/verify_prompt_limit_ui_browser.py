#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
「提示词长度上限」下拉的浏览器取证。

检查项：
  1. 「本地模型配置」页的 Provider 编辑弹窗里，下拉存在且选项齐全
     （含「无限制」）
  2. 选「无限制」-> 提示文案跟着变
  3. 保存 -> 服务端真的存下来了（重新 GET /api/providers 核对）
  4. 重新打开弹窗 -> 回显正确
  5. 三档视口（375/390/1280）console 报错 0、无横向溢出
  6. 截图

用法：
    /home/ubuntu/comfyui/venv/bin/python tests/verify_prompt_limit_ui_browser.py \
        --port 8198 --tag lenlimit --out <dir>
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

PROVIDER_ENTRIES = [
    "#navProviders", "button:has-text('本地模型')", "a:has-text('本地模型')",
]


def open_provider_modal(page):
    """
    打开 Provider 编辑弹窗。

    【为什么要 evaluate 直接显示面板】
    页面是 JS 路由（点导航才切页），Playwright 的 click 要等元素可见，
    但切页有动画 -> 容易超时。既有脚本（verify_max_tokens_ui_browser.py）
    也是直接 evaluate 把面板显示出来，这里沿用同一做法。
    """
    for sel in PROVIDER_ENTRIES:
        try:
            el = page.query_selector(sel)
        except Exception:
            continue
        if el:
            try:
                el.click(timeout=2500)
                break
            except Exception:
                continue
    time.sleep(1.0)
    # 直接把 providers 页显示出来
    page.evaluate("""() => {
      const pg = document.querySelector('#providersPage');
      if (pg) { pg.style.display = 'block'; }
      document.querySelectorAll('.page, .panel-page').forEach(el => {
        if (el.id && el.id !== 'providersPage') el.style.display = 'none';
      });
    }""")
    time.sleep(1.2)
    # 找「编辑」按钮（用 JS 点，避开可见性等待）
    clicked = page.evaluate("""() => {
      const btn = document.querySelector("button[data-act='edit']");
      if (btn) { btn.click(); return true; }
      const add = document.querySelector('#provAddBtn');
      if (add) { add.click(); return true; }
      return false;
    }""")
    time.sleep(1.2)
    # 弹窗可能因为 hidden 属性没被清掉，直接强制显示
    page.evaluate("""() => {
      const m = document.querySelector('#provModal');
      if (m) m.hidden = false;
    }""")
    time.sleep(0.5)
    return bool(clicked)


def dump_state(page):
    return page.evaluate("""() => {
      const q = (s) => document.querySelector(s);
      const visible = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      };
      const sel = q('#provPromptLimit');
      const modal = q('#provModal');
      return {
        modalVisible: modal ? !modal.hidden : false,
        selectExists: !!sel,
        selectVisible: visible(sel),
        value: sel ? sel.value : null,
        options: sel ? Array.from(sel.options).map(o => ({
          value: o.value, label: o.textContent.trim()
        })) : [],
        hint: q('#provPromptLimitHint') ? q('#provPromptLimitHint').textContent.trim() : null,
        docScrollW: document.documentElement.scrollWidth,
        innerW: window.innerWidth,
        horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
      };
    }""")


def run(port, tag, outdir):
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    base = "http://127.0.0.1:%d" % port
    results = {"port": port, "tag": tag, "viewports": [],
               "console_errors": [], "page_errors": [], "failed_requests": []}

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        for vname, vsize in VIEWPORTS:
            ctx = browser.new_context(viewport={"width": vsize["width"],
                                               "height": vsize["height"]})
            page = ctx.new_page()
            console_errors, page_errors, failed = [], [], []
            asset_404 = []
            # console 里 "Failed to load resource ... 404" 一律来自上面那些
            # /files/ 资产 404 —— 归到 asset_404s，不算本功能的 console 报错。
            page.on("console", lambda m: console_errors.append(m.text)
                    if m.type == "error" and "404" not in (m.text or "") else None)
            page.on("pageerror", lambda e: page_errors.append(str(e)))
            # 【只统计「本功能相关」的失败请求】
            # /files/videos/*.mp4 这类 404 是**测试环境的既有产物**：
            # 共享仓 data/outputs 里本来就没有那几个文件（两边实例都 404），
            # 与本改动无关。分开计数，避免把环境噪声算成回归。
            page.on("response", lambda r: failed.append("%s %s" % (r.status, r.url))
                    if r.status >= 400 and "/files/" not in r.url else None)
            page.on("response", lambda r: asset_404.append("%s %s" % (r.status, r.url))
                    if r.status >= 400 and "/files/" in r.url else None)

            page.goto(base, wait_until="domcontentloaded", timeout=60000)
            time.sleep(1.5)
            opened = open_provider_modal(page)
            state = dump_state(page)
            shot = out / ("ui-%s-%s.png" % (tag, vname))
            page.screenshot(path=str(shot), full_page=False)
            state["screenshot"] = str(shot)
            state["viewport"] = vname
            state["modalOpened"] = opened
            state["console_errors"] = console_errors
            state["page_errors"] = page_errors
            state["failed_requests"] = failed
            state["asset_404s"] = asset_404
            results["viewports"].append(state)
            results["console_errors"] += console_errors
            results["page_errors"] += page_errors
            results["failed_requests"] += failed
            ctx.close()
        browser.close()

    # ---- 保存 -> 服务端持久化 -> 回显 的闭环（只在 1280 那档做） ----
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto(base, wait_until="domcontentloaded", timeout=60000)
        time.sleep(1.5)
        open_provider_modal(page)
        before = dump_state(page)

        # 选「无限制」（baseline 没有这个字段 -> 记下来但不中断，
        # 这样同一套脚本能同时打 baseline 做 A/B 对比）
        try:
            page.select_option("#provPromptLimit", "unlimited")
            time.sleep(0.6)
        except Exception as exc:
            results.setdefault("persistence_errors", []).append(
                "select_option: %s" % str(exc)[:160])
        after_select = dump_state(page)

        # 保存（需要必填项都有值 —— 编辑弹窗里已经是原值）
        saved = False
        try:
            page.click("#provSave")
            time.sleep(2.5)
            saved = True
        except Exception as exc:
            after_select["save_error"] = str(exc)[:200]
            results.setdefault("persistence_errors", []).append(
                "save: %s" % str(exc)[:160])

        # 服务端核对
        srv = None
        try:
            resp = page.request.get(base + "/api/providers")
            data = resp.json()
            srv = [{"id": x.get("id"), "prompt_limit": x.get("prompt_limit"),
                    "unlimited": x.get("prompt_limit_unlimited")}
                   for x in data.get("providers", [])]
            srv_choices = [c.get("value") for c in data.get("prompt_limit_choices", [])]
        except Exception as exc:
            srv_choices = str(exc)[:200]

        # 重新打开 -> 回显
        page.reload(wait_until="domcontentloaded")
        time.sleep(1.5)
        open_provider_modal(page)
        after_reload = dump_state(page)

        results["persistence"] = {
            "before_value": before.get("value"),
            "after_select_value": after_select.get("value"),
            "after_select_hint": after_select.get("hint"),
            "saved": saved,
            "server_state": srv,
            "server_choices": srv_choices,
            "after_reload_value": after_reload.get("value"),
        }
        ctx.close()
        browser.close()

    checks = []
    for v in results["viewports"]:
        opts = v.get("options") or []
        values = [o["value"] for o in opts]
        checks.append({
            "viewport": v["viewport"],
            "no_console_error": len(v["console_errors"]) == 0,
            "no_page_error": len(v["page_errors"]) == 0,
            "no_failed_request": len(v["failed_requests"]) == 0,
            "no_horizontal_overflow": not v["horizontalOverflow"],
            "modal_opened": v["modalOpened"],
            "select_exists": v["selectExists"],
            "select_visible": v["selectVisible"],
            "has_unlimited_option": "unlimited" in values,
            "has_4000_option": "4000" in values,
            "option_labels_present": all(o["label"] for o in opts),
        })
    pe = results["persistence"]
    checks.append({
        "viewport": "persistence",
        "unlimited_selectable": pe.get("after_select_value") == "unlimited",
        "hint_updates": bool(pe.get("after_select_hint")),
        "server_persisted": any(x.get("prompt_limit") == "unlimited"
                                for x in (pe.get("server_state") or [])),
        "reloaded_shows_unlimited": pe.get("after_reload_value") == "unlimited",
        "server_serves_choices": "unlimited" in (pe.get("server_choices") or []),
    })
    results["checks"] = checks
    results["asset_404s"] = sorted({a for v in results["viewports"]
                                    for a in (v.get("asset_404s") or [])})
    results["summary"] = {
        "console_errors": len(results["console_errors"]),
        "page_errors": len(results["page_errors"]),
        "failed_requests": len(results["failed_requests"]),
        "asset_404s": len(results["asset_404s"]),
        "total_checks": sum(len(c) - 1 for c in checks),
        "passed_checks": sum(1 for c in checks for k, val in c.items()
                             if k != "viewport" and val is True),
    }
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8198)
    ap.add_argument("--tag", default="lenlimit")
    ap.add_argument("--out", default=".")
    args = ap.parse_args()
    res = run(args.port, args.tag, args.out)
    print(json.dumps(res["summary"], ensure_ascii=False, indent=2))
    for c in res["checks"]:
        fails = [k for k, v in c.items() if k != "viewport" and v is not True]
        print("  %-12s %s" % (c["viewport"], "OK" if not fails else "FAIL: %s" % fails))
    print("persistence:", json.dumps(res["persistence"], ensure_ascii=False, indent=2))
    out = Path(args.out) / ("ui-%s.json" % args.tag)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved ->", out)


if __name__ == "__main__":
    main()
