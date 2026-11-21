# -*- coding: utf-8 -*-
"""看一眼媒体库卡片在窄屏下的真实 DOM（预览区里到底有什么元素）。

运行：
    python3 scripts/dump_card_dom.py --base http://127.0.0.1:8917 --width 375
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8917")
    ap.add_argument("--width", type=int, default=375)
    ap.add_argument("--height", type=int, default=812)
    args = ap.parse_args()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": args.width, "height": args.height},
                                  is_mobile=True, has_touch=True)
        page = ctx.new_page()
        page.goto(args.base.rstrip("/") + "/", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length >= 0",
            timeout=20000)
        page.wait_for_timeout(2500)
        info = page.evaluate("""() => {
          const cards = Array.from(
            document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
          return cards.map((c, i) => ({
            i: i,
            jid: c.dataset.jid,
            previewClass: c.querySelector('.job-preview')?.className || null,
            previewInner: (c.querySelector('.job-preview')?.innerHTML || '')
              .replace(/\\s+/g, ' ').slice(0, 220),
            mediaTags: Array.from(c.querySelectorAll('.job-preview *'))
              .map(e => e.tagName).slice(0, 5),
            hasImg: !!c.querySelector('.job-preview img'),
            hasVideo: !!c.querySelector('.job-preview video'),
            hasAudio: !!c.querySelector('.job-preview audio'),
            hasBatchThumb: !!c.querySelector('.job-batch-thumb img'),
            rect: (() => { const r = c.getBoundingClientRect();
              return {top: Math.round(r.top), left: Math.round(r.left),
                      w: Math.round(r.width), h: Math.round(r.height)}; })(),
          }));
        }""")
        print(json.dumps(info, ensure_ascii=False, indent=1))
        browser.close()


if __name__ == "__main__":
    main()
