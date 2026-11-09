#!/usr/bin/env python3
"""对照实验：ERR_ABORTED 是不是本分支特有的资源路径问题。

在**未改动的基线**（/tmp/h3-base，local/maintainer 的音乐子系统基线）
上跑同样的「加载页面 -> 记录 requestfailed」，
对比两边的 failed request 形态。

判定：
  - 两边都有同样的 ERR_ABORTED -> 环境/浏览器行为，非本分支缺陷
  - 只有本分支有 -> 本分支的真实资源路径问题

用法：
  python3 tests/verify_audio_resource_abort.py --base http://127.0.0.1:8195
"""
import argparse
import json
import sys
import time

from playwright.sync_api import sync_playwright


def probe(base, width=1280, settle=6000):
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": width, "height": 900})
        page = ctx.new_page()
        failed = []
        errors = []
        page.on("requestfailed",
                lambda r: failed.append(f"{r.method} {r.url} :: {r.failure}"))
        page.on("console", lambda m: errors.append(f"{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.goto(base, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(settle)
        ctx.close()
        browser.close()
    return failed, errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8195")
    ap.add_argument("--baseline", default="http://127.0.0.1:8190")
    args = ap.parse_args()

    out = {}
    for name, base in (("mine", args.base), ("baseline", args.baseline)):
        failed, errors = probe(base)
        aborts = [f for f in failed if "ERR_ABORTED" in f]
        others = [f for f in failed if "ERR_ABORTED" not in f]
        out[name] = {
            "base": base,
            "total_failed": len(failed),
            "aborted": len(aborts),
            "other": others,
            "console_errors": errors,
        }
        print(f"[{name}] {base}")
        print(f"   failed total={len(failed)}  ERR_ABORTED={len(aborts)}  "
              f"其它={len(others)}")
        print(f"   console errors={len(errors)}")
        for o in others[:10]:
            print("   OTHER-FAILED:", o)
        for e in errors[:10]:
            print("   CONSOLE-ERR:", e)

    print("\n=== 结论 ===")
    same = out["mine"]["aborted"] > 0 and out["baseline"]["aborted"] > 0
    no_other = not out["mine"]["other"]
    if same and no_other:
        print("PASS: 两边都有同样的 ERR_ABORTED，且本分支无其它失败请求 "
              "-> 属浏览器媒体预载取消行为，非本分支资源路径缺陷")
        return 0
    if out["mine"]["other"]:
        print("FAIL: 本分支存在非 ERR_ABORTED 的失败请求，需排查:")
        for o in out["mine"]["other"]:
            print("   ", o)
        return 1
    print("INCONCLUSIVE: 基线未复现，需要人工确认")
    return 2


if __name__ == "__main__":
    sys.exit(main())
