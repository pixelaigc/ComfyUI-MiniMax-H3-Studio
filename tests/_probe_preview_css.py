# -*- coding: utf-8 -*-
"""临时实验：在真实 Chromium 里比较 .job-preview 的几种高度约束方案。

问题：.job-preview 的高度目前由内容（图片自然尺寸）决定，
      aspect-ratio: 16/9 被内容压过 —— 于是图片按原始像素尺寸撑开容器。
目标：容器高度与图片无关（固定尺寸），图片永远不超过容器。
"""
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8190"

# 候选方案
CANDIDATES = {
    "A_baseline": "",
    "B_abs_fill": """
      .job-preview img, .job-preview video {
        position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain;
      }
    """,
    "C_pad_ratio": """
      .job-preview { aspect-ratio: auto; height: 0; padding-bottom: 56.25%; overflow: hidden; }
      .job-preview img, .job-preview video {
        position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain;
      }
    """,
    "D_abs_fill_contain_ratio": """
      .job-preview { aspect-ratio: 16/9; overflow: hidden; min-height: 0; }
      .job-preview img, .job-preview video {
        position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain;
      }
    """,
}

PROBE = """
() => {
  const pb = document.querySelector('#jobsGrid .job-preview');
  const img = document.querySelector('#jobsGrid .job-preview img');
  const card = document.querySelector('#jobsGrid .job-card');
  if (!pb) return null;
  const r = pb.getBoundingClientRect();
  const ir = img ? img.getBoundingClientRect() : null;
  const cr = card.getBoundingClientRect();
  return {
    cardW: Math.round(cr.width),
    boxW: Math.round(r.width), boxH: Math.round(r.height),
    imgW: ir ? Math.round(ir.width) : null,
    imgH: ir ? Math.round(ir.height) : null,
    natural: img ? [img.naturalWidth, img.naturalHeight] : null,
  };
}
"""


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        page = ctx.new_page()
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card').length > 0", timeout=20000)
        page.wait_for_timeout(2500)
        for name, css in CANDIDATES.items():
            page.evaluate("""(css) => {
              let s = document.getElementById('__probe_css');
              if (!s) { s = document.createElement('style'); s.id='__probe_css'; document.head.appendChild(s); }
              s.textContent = css;
            }""", css)
            page.wait_for_timeout(400)
            print(name, page.evaluate(PROBE))
        browser.close()


if __name__ == "__main__":
    main()
