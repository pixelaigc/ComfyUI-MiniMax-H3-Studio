# -*- coding: utf-8 -*-
"""【真实浏览器】接续 + 复用参数的混合态 —— 复现-修复对照。

复刻独立复验发现缺陷的那条路径，并在**真实浏览器**里验证修复：

  1. 生成库卡片上点「接续」-> 左侧出现「接续上下文」（来源 A）
  2. 再点**另一张**卡片的「复用参数」-> 左侧控件变成 B 的参数
  3. 【修复前】接续上下文仍挂着 -> 提交 = B 的参数 + A 的接力棒（混合态）
     【修复后】接续上下文被清掉 -> 提交 = 纯 B 的参数（独立重生成）
  4. 提交载荷里**不得**再有 continuation 字段
  5. 控制台 0 报错、0 失败请求

两档宽度：375 / 1280。

用法：
    python3 tests/verify_mixed_state_browser.py \
      --base http://127.0.0.1:8202 --out evidence/continuation-mixed-state
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from playwright.sync_api import sync_playwright
except Exception:
    print("未安装 playwright")
    sys.exit(2)

WIDTHS = (375, 1280)


def http_get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode())


def http_multipart(url, fields):
    b = "----h3mixbrowser"
    body = b""
    for k, v in fields.items():
        body += f"--{b}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        body += str(v).encode("utf-8") + b"\r\n"
    body += f"--{b}--\r\n".encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())


def wait_done(base, jid, timeout=1800):
    dl = time.time() + timeout
    while time.time() < dl:
        j = http_get(f"{base}/api/jobs/{jid}")
        if j.get("status") in ("done", "failed", "canceled"):
            return j
        time.sleep(3)
    raise TimeoutError(jid)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8202")
    ap.add_argument("--out", default=str(REPO / "evidence" /
                                         "continuation-mixed-state"))
    args = ap.parse_args()
    base = args.base.rstrip("/")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    report = {"base": base, "widths": {}, "console_errors": [],
              "failed_requests": []}

    # ------------------------------------------------------------------
    # 造两张卡片：A（768×448，接续源）、B（1024×576，复用参数源）
    # ------------------------------------------------------------------
    print("=== 造两张卡片 ===")
    cards = {}
    for tag, (w, h), prompt, seed in (
        ("A", (768, 448),
         "A lone swordsman in black robes in a moonlit bamboo forest",
         4242),
        ("B", (1024, 576),
         "A red lantern festival at night with crowds walking",
         9999),
    ):
        params = {
            "prompt": prompt, "model_key": "fl2va_int8",
            "width": w, "height": h, "duration": 5, "steps": 4,
            "sampler": "euler", "scheduler": "simple", "seed": seed,
        }
        jid = http_multipart(f"{base}/api/jobs",
                             {"mode": "t2v",
                              "params": json.dumps(params)})["id"]
        print(f"  {tag} = {jid}（{w}×{h}）…")
        job = wait_done(base, jid)
        print(f"    status={job.get('status')}")
        if job.get("status") != "done":
            print("生成失败：", job.get("error"))
            return 2
        cards[tag] = jid
    report["cards"] = cards

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        try:
            for width in WIDTHS:
                print(f"\n=== {width}px ===")
                ctx = browser.new_context(viewport={"width": width,
                                                   "height": 900})
                page = ctx.new_page()
                errors = []
                failed = []
                page.on("pageerror", lambda e, errs=errors: errs.append(str(e)))
                page.on("console", lambda m, errs=errors:
                        errs.append(m.text) if m.type == "error" else None)
                page.on("response", lambda r, bad=failed:
                        bad.append(f"{r.status} {r.url}") if r.status >= 400
                        else None)

                page.goto(f"{base}/", wait_until="domcontentloaded")
                page.wait_for_timeout(3000)

                step = {"width": width}

                # ---- 1) 找到 A 卡片并点「接续」----
                a_ok = page.evaluate(
                    """(jid) => {
                      const cards = [...document.querySelectorAll('.job-card')];
                      const card = cards.find(c => c.dataset.jid === jid);
                      if (!card) return false;
                      const btn = card.querySelector('[data-act="continue"]');
                      if (!btn) return false;
                      btn.click();
                      return true;
                    }""", cards["A"])
                page.wait_for_timeout(1800)
                step["clicked_continue_on_A"] = a_ok
                ctx_state = page.evaluate(
                    """() => {
                      const c = window.Continuation && window.Continuation.state();
                      const cur = c && c.current;
                      return {
                        hasContext: !!cur,
                        sourceJob: cur ? (cur.source || {}).job_id : "",
                        srcWidth: cur ? Number((cur.params||{}).width||0) : 0,
                        srcHeight: cur ? Number((cur.params||{}).height||0) : 0,
                        panelVisible: (() => {
                          const p = document.getElementById('continuationPanel');
                          return !!(p && !p.hidden);
                        })(),
                        panelText: (document.getElementById('continuationPanel')
                          || {}).innerText || '',
                      };
                    }""")
                step["after_continue"] = ctx_state
                print(f"  点接续后: context={ctx_state['hasContext']} "
                      f"来源={ctx_state['sourceJob']} "
                      f"面板可见={ctx_state['panelVisible']}")
                page.screenshot(path=str(out / f"mix-{width}-01-after-continue.png"),
                                full_page=True)

                # ---- 2) 点 B 卡片的「复用参数」----
                b_ok = page.evaluate(
                    """(jid) => {
                      const cards = [...document.querySelectorAll('.job-card')];
                      const card = cards.find(c => c.dataset.jid === jid);
                      if (!card) return false;
                      const btn = card.querySelector('[data-act="reuse"]');
                      if (!btn) return false;
                      btn.click();
                      return true;
                    }""", cards["B"])
                page.wait_for_timeout(1500)
                step["clicked_reuse_on_B"] = b_ok
                after_reuse = page.evaluate(
                    """() => {
                      const c = window.Continuation && window.Continuation.state();
                      const cur = c && c.current;
                      const p = document.getElementById('continuationPanel');
                      const panel = window.VIDEO_PANEL;
                      const size = panel && panel._debug
                        ? panel._debug.currentSize() : null;
                      return {
                        // 【关键断言】接续上下文必须已被清掉
                        contextStillPresent: !!cur,
                        panelHidden: !!(p && p.hidden),
                        panelText: (p || {}).innerText || '',
                        panelSize: size,
                        prompt: (document.getElementById('prompt')||{}).value || '',
                        seed: (document.getElementById('seed')||{}).value || '',
                        toast: window.__h3ContinuationToast || '',
                      };
                    }""")
                step["after_reuse"] = after_reuse
                print(f"  点复用参数后: context 仍在={after_reuse['contextStillPresent']}"
                      f" 尺寸={after_reuse['panelSize']}")
                page.screenshot(path=str(out / f"mix-{width}-02-after-reuse.png"),
                                full_page=True)

                # ---- 3) 提交载荷里不得再有 continuation ----
                submitted = page.evaluate(
                    """async () => {
                      const fd = new FormData();
                      const orig = window.fetch;
                      let captured = null;
                      window.fetch = function (url, opts) {
                        if (opts && opts.method === 'POST'
                            && String(url).includes('/api/jobs')
                            && opts.body instanceof FormData) {
                          const raw = opts.body.get('params');
                          if (raw) captured = JSON.parse(raw);
                        }
                        return orig.apply(this, arguments);
                      };
                      document.getElementById('prompt').value =
                        'MIXED STATE BROWSER PROBE';
                      const btn = document.getElementById('genBtn');
                      if (!btn) { window.fetch = orig; return {noButton: true}; }
                      btn.click();
                      await new Promise(r => setTimeout(r, 3000));
                      window.fetch = orig;
                      return {captured};
                    }""")
                params = (submitted or {}).get("captured") or {}
                # 【为什么要断言 captured 非空】否则「提交载荷里没有
                # continuation」这条断言是**空转**的 —— 没抓到载荷时
                # 它照样通过，等于什么都没证明。
                step["payload_captured"] = bool(params)
                step["submit_payload"] = {
                    "has_continuation": "continuation" in params,
                    "width": params.get("width"),
                    "height": params.get("height"),
                    "prompt": params.get("prompt"),
                    "seed": params.get("seed"),
                }
                print(f"  提交载荷: captured={bool(params)} "
                      f"continuation={step['submit_payload']['has_continuation']}"
                      f" {step['submit_payload']['width']}×{step['submit_payload']['height']}")
                page.screenshot(path=str(out / f"mix-{width}-03-submitted.png"),
                                full_page=True)

                # ---- 4) 断言 ----
                step["assertions"] = {
                    # 【防空转】必须真的抓到了提交载荷，否则下面几条
                    # 「载荷里没有 X」的断言什么都没证明。
                    "payload_was_captured": step["payload_captured"],
                    # 复用参数后接续上下文必须被清掉（这是修复的核心）
                    "context_cleared_after_reuse":
                        not after_reuse["contextStillPresent"],
                    "context_panel_hidden": after_reuse["panelHidden"],
                    # 提交载荷不得带 continuation
                    "no_continuation_in_payload":
                        not step["submit_payload"]["has_continuation"],
                    # 面板尺寸应是 B 的（复用参数生效）
                    "panel_size_is_B":
                        list(after_reuse["panelSize"] or []) == [1024, 576],
                    # 提示词应是 B 的（复用参数会带提示词）
                    "prompt_from_B": "red lantern" in (after_reuse["prompt"] or ""),
                }
                print(f"  断言: {json.dumps(step['assertions'], ensure_ascii=False)}")

                step["console_errors"] = list(errors)
                step["failed_requests"] = list(failed)
                report["widths"][str(width)] = step
                report["console_errors"].extend(errors)
                report["failed_requests"].extend(failed)
                ctx.close()
        finally:
            browser.close()

    all_pass = all(
        all(w["assertions"].values()) for w in report["widths"].values())
    report["no_console_errors"] = not report["console_errors"]
    report["no_failed_requests"] = not report["failed_requests"]
    report["verdict_pass"] = bool(all_pass and report["no_console_errors"]
                                 and report["no_failed_requests"])

    out_file = out / "mixed_state_browser_report.json"
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n报告: {out_file}")
    print(f"控制台报错: {len(report['console_errors'])}")
    print(f"失败请求: {len(report['failed_requests'])}")
    print(f"结论: {'通过 ✅' if report['verdict_pass'] else '未通过 ❌'}")
    return 0 if report["verdict_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
