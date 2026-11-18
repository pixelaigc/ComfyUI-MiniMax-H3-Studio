# -*- coding: utf-8 -*-
"""第 6 轮审查：前端「可见注释/调试文案」污染的真实浏览器取证。

【为什么必须用真实浏览器】
本轮要求「每条结论须给出『取值后 DOM 实际不含该文本』的取证证据
（截图或 DOM 快照），不得只做代码阅读推断」。
所以这里：
  1. 用 Playwright + Chromium 打开**线上** 8190；
  2. 遍历全部面板，抓取**渲染后**的全部文本节点 + 属性值；
  3. 对每个可疑标记做命中判定；
  4. 输出 DOM 快照 + 截图 + 结论 JSON。

【可疑标记的来源】
  * 词法扫描（scripts/cr5_scan_text_candidates.py 同款思路）扫源码里的
    「开发者措辞」与 **markdown 强调残留**（`**xxx**`）；
  * 第 6 轮新增代码（接续 / 分辨率）里人工标注的可疑点。

【输出】
  docs/evidence-cr6/
    dom_texts.txt        全部可见文本节点（每行一条）
    dom_attrs.txt        全部属性值（每行一条）
    scan_result.json     逐标记命中与否
    panel_*.png          每个面板截图
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "docs/evidence-cr6")
OUT.mkdir(parents=True, exist_ok=True)

#: 12 个面板（第 5 轮已验证的清单）
PANELS = [
    ("t2v", "文生视频"),
    ("flf", "首尾帧视频"),
    ("r2v", "参考生视频"),
    ("t2i", "文生图"),
    ("t2i_qwen", "文生图 Qwen"),
    ("i2i_qwen", "图片编辑 Qwen"),
    ("t2i_moss", "文生图 MOSS"),
    ("t2i_h3image", "文生图 H3-Image"),
    ("i2i_h3image", "图片编辑 H3-Image"),
    ("t2a", "文生音乐"),
    ("a2a", "音频编辑"),
    ("longfilm", "长片拼接"),
]

#: 开发者措辞标记（第 5 轮沿用）。
DEV_MARKERS = [
    "TODO", "FIXME", "XXX", "HACK", "DEBUG",
    "待实现", "待办", "占位符", "调试", "内部标识",
    "临时方案", "先这样", "测试用", "test-only", "内部用",
]

#: **markdown 强调残留** —— 本轮重点。
#: 理由：项目大量用 **xxx** 写注释与内部文档，
#: 一旦某段 **xxx** 进了 innerHTML / textContent 就是**用户可见的脏文案**。
MD_MARKER = re.compile(r"\*\*[^*\n]{1,60}\*\*")

#: 已知「**合法**」的 **xxx** 出现位置（纯注释行、开发者日志）。
#: 只有不在这些位置的才算污染（这里先按标记逐个判定，不预先豁免）。
KNOWN_OK_SUBSTRINGS = [
    "（模型连工具名都看不到，不会反复尝试）",
]


def collect(page):
    """抓取渲染后的全部文本节点 + 属性值。"""
    return page.evaluate(
        """() => {
      const texts = [];
      const attrs = [];
      const walker = document.createTreeWalker(
        document.body, NodeFilter.SHOW_TEXT, null);
      let node;
      while ((node = walker.nextNode())) {
        const t = (node.nodeValue || "").trim();
        if (t) texts.push(t);
      }
      const all = document.querySelectorAll("*");
      for (const el of all) {
        for (const a of el.attributes) {
          attrs.push(a.value);
        }
      }
      return { texts, attrs, title: document.title,
               html: document.documentElement.outerHTML.length };
    }"""
    )


def main() -> int:
    from playwright.sync_api import sync_playwright

    console_errors: list[str] = []
    page_errors: list[str] = []
    all_texts: list[str] = []
    all_attrs: list[str] = []
    per_panel: list[dict] = []
    md_hits: dict[str, list[str]] = {}
    dev_hits: dict[str, list[str]] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path="/usr/bin/google-chrome",
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()
        page.on("console", lambda m: console_errors.append(f"{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        page.goto(BASE, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2500)

        for key, label in PANELS:
            before_err = len(console_errors)
            before_perr = len(page_errors)
            switched = False
            try:
                switched = page.evaluate(
                    """(mode) => {
                      if (typeof window.activateVideoMode === 'function') {
                        window.activateVideoMode(mode); return true;
                      }
                      return false;
                    }""",
                    key,
                )
            except Exception as exc:  # pragma: no cover
                print(f"[warn] 切换面板 {key} 失败: {exc}")
            page.wait_for_timeout(900)
            data = collect(page)
            all_texts.extend(data["texts"])
            all_attrs.extend(data["attrs"])
            shot = OUT / f"panel-{key}.png"
            try:
                page.screenshot(path=str(shot), full_page=False)
            except Exception:
                pass
            per_panel.append({
                "mode": key, "label": label, "switched": switched,
                "texts": len(data["texts"]), "attrs": len(data["attrs"]),
                "new_console_errors": len(console_errors) - before_err,
                "new_pageerrors": len(page_errors) - before_perr,
                "screenshot": str(shot),
            })
            print(f"[panel] {key:14s} switched={switched} "
                  f"texts={len(data['texts']):5d} attrs={len(data['attrs']):5d} "
                  f"err+{len(console_errors)-before_err}")

        browser.close()

    # 去重但保序
    def uniq(seq):
        seen, out = set(), []
        for item in seq:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out

    texts = uniq(all_texts)
    attrs = uniq(all_attrs)
    (OUT / "dom_texts.txt").write_text("\n".join(texts), encoding="utf-8")
    (OUT / "dom_attrs.txt").write_text("\n".join(attrs), encoding="utf-8")

    # ---- 标记判定 ----
    def scan(seq, label):
        out = {}
        for marker in DEV_MARKERS:
            hits = [s for s in seq if marker in s]
            out[f"dev:{marker}"] = {"where": label, "count": len(hits),
                                    "samples": hits[:5]}
        md_found = []
        for s in seq:
            for m in MD_MARKER.findall(s):
                md_found.append((m, s[:160]))
        out["markdown_bold"] = {
            "where": label, "count": len(md_found),
            "samples": [m for m, _ in md_found[:12]],
            "contexts": [c for _, c in md_found[:12]],
        }
        return out

    result = {
        "base": BASE,
        "sampled_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "panels": per_panel,
        "sampling": {"texts": len(texts), "attrs": len(attrs)},
        "console_errors": console_errors,
        "page_errors": page_errors,
        "text_scan": scan(texts, "visible_text"),
        "attr_scan": scan(attrs, "attribute_value"),
        "title": "",
    }
    (OUT / "scan_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    md = result["text_scan"]["markdown_bold"]
    print("\n=== markdown **xxx** 残留（可见文本）===")
    print("命中数:", md["count"])
    for s in md["samples"]:
        print("  ", s)
    dev_total = sum(v["count"] for k, v in result["text_scan"].items()
                    if k.startswith("dev:"))
    print("开发者措辞命中数（可见文本）:", dev_total)
    for k, v in result["text_scan"].items():
        if k.startswith("dev:") and v["count"]:
            print("  ", k, v["count"], v["samples"][:2])
    print("\n=== 属性值里的 markdown 残留 ===")
    print("命中数:", result["attr_scan"]["markdown_bold"]["count"])
    for s in result["attr_scan"]["markdown_bold"]["samples"][:12]:
        print("  ", s)
    print("\nconsole errors:", len(console_errors), " pageerrors:", len(page_errors))
    for e in console_errors[:10]:
        print("  ", e)
    for e in page_errors[:10]:
        print("  ", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
