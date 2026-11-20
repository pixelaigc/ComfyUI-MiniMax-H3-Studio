#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看门狗第 7 轮 —— C 维度的**可量化**审美取证（2026-11-21）。

【为什么用计算而不是看截图】
本轮截图已产出（见 /root/evidence/watchdog7-*/），但 vision_analyze
服务连续超时（3 次实测 Request timed out），无法用视觉模型主观描述。
所以本脚本改用**可复算的量化指标**取审美证据：

  * 对比度：WCAG 2.1 相对亮度公式，算前景/背景对比度（4.5:1 正文 /
    3:1 大字 / 非文本控件）
  * 间距一致性：同类控件的 padding / gap 分布（找出离群值）
  * 触控尺寸：移动端可点击元素的命中框尺寸（< 32px 记为缺陷）
  * 层级：标题 / 正文的字号 + 字重分布
  * 溢出：文档级横向溢出 + 元素越界
  * 暗/亮：实际采样页面背景色

用法:
    python3 scripts/watchdog7_c_metrics.py --out /root/evidence/watchdog7-c
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

BASE = os.environ.get("H3_BASE", "http://127.0.0.1:8190")

VIEWS = [
    ("desktop-1280", 1280, 900),
    ("mobile-375", 375, 780),
    ("mobile-390", 390, 844),
]

JS = r"""
() => {
  const parseRGB = (s) => {
    const m = (s||'').match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(',').map(x => parseFloat(x.trim()));
    return {r:p[0], g:p[1], b:p[2], a: p.length>3 ? p[3] : 1};
  };
  const lum = (c) => {
    const f = (v) => { v/=255; return v<=0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); };
    return 0.2126*f(c.r) + 0.7152*f(c.g) + 0.0722*f(c.b);
  };
  const ratio = (a,b) => {
    if (!a || !b) return null;
    const la = lum(a), lb = lum(b);
    const hi = Math.max(la,lb), lo = Math.min(la,lb);
    return Math.round(((hi+0.05)/(lo+0.05))*100)/100;
  };
  const bgOf = (el) => {
    let n = el;
    while (n && n !== document.documentElement) {
      const c = parseRGB(getComputedStyle(n).backgroundColor);
      if (c && c.a > 0.5) return c;
      n = n.parentElement;
    }
    return parseRGB(getComputedStyle(document.body).backgroundColor) || {r:0,g:0,b:0,a:1};
  };
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight;
  };
  const out = {
    viewport: {w: innerWidth, h: innerHeight},
    docBg: getComputedStyle(document.body).backgroundColor,
    contrast: [],      // {tag, cls, text, fg, bg, ratio, size, weight, kind}
    spacing: {},       // 控件 padding/gap 分布
    typography: {},    // 字号/字重分布
    tapTargets: [],
    overflow: [],
  };
  const texts = [];
  const tagKinds = new Set(['BUTTON','A','INPUT','SELECT','TEXTAREA','LABEL','H1','H2','H3','H4','P','SPAN','LI','TD','TH']);
  for (const el of document.querySelectorAll('*')) {
    if (!vis(el)) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity) < 0.15) continue;
    const r = el.getBoundingClientRect();
    // 1) 对比度（只看直接含文本的元素）
    const own = Array.from(el.childNodes).some(n => n.nodeType === 3 && n.textContent.trim().length > 1);
    if (own && tagKinds.has(el.tagName)) {
      const fg = parseRGB(cs.color); const bg = bgOf(el);
      const size = parseFloat(cs.fontSize); const weight = parseInt(cs.fontWeight) || 400;
      const cr = ratio(fg, bg);
      const large = size >= 24 || (size >= 18.66 && weight >= 700);
      const need = large ? 3.0 : 4.5;
      const text = (el.textContent||'').trim().slice(0,40);
      if (text) {
        out.contrast.push({
          tag: el.tagName, id: el.id, cls: (el.className||'').toString().slice(0,60),
          text, fg: cs.color, bg: `rgb(${bg.r},${bg.g},${bg.b})`,
          ratio: cr, size, weight, need, kind: large ? 'large' : 'body',
          pass: cr === null ? null : cr >= need
        });
      }
    }
    // 2) 触控尺寸
    if (el.matches('button,a,[role=button],input,select,textarea')) {
      if (r.height < 32 || r.width < 32) {
        out.tapTargets.push({tag: el.tagName, id: el.id,
          cls: (el.className||'').toString().slice(0,60),
          w: Math.round(r.width), h: Math.round(r.height),
          text: (el.textContent||'').trim().slice(0,30)});
      }
    }
    // 3) 横向溢出（排除左侧负坐标的抽屉/侧栏 —— 那是设计上的离屏）
    const vw = document.documentElement.clientWidth;
    if (r.right > vw + 2 && r.left >= 0) {
      out.overflow.push({tag: el.tagName, id: el.id,
        cls: (el.className||'').toString().slice(0,60),
        left: Math.round(r.left), right: Math.round(r.right), vw,
        text: (el.textContent||'').trim().slice(0,30)});
    }
  }
  // 4) 间距分布
  const topN = (arr, n) => {
    const m = new Map();
    for (const x of arr) m.set(x, (m.get(x)||0)+1);
    return Array.from(m.entries()).sort((a,b)=>b[1]-a[1]).slice(0,n);
  };
  const pads = [];
  const gaps = [];
  for (const el of document.querySelectorAll('button, .btn, input, select, textarea, .card, .panel')) {
    if (!vis(el)) continue;
    const cs = getComputedStyle(el);
    pads.push([cs.paddingTop, cs.paddingBottom, cs.paddingLeft, cs.paddingRight].join(' '));
  }
  for (const el of document.querySelectorAll('*')) {
    if (!vis(el)) continue;
    const cs = getComputedStyle(el);
    if (cs.gap && cs.gap !== 'normal') gaps.push(cs.gap);
  }
  out.spacing.pad_variants = topN(pads, 12);
  out.spacing.gap_variants = topN(gaps, 12);
  // 5) 字号/字重分布
  const sizes = [], weights = [];
  for (const el of document.querySelectorAll('h1,h2,h3,h4,h5,h6,p,span,div,button,label,li,a')) {
    if (!vis(el)) continue;
    const own = Array.from(el.childNodes).some(n => n.nodeType === 3 && n.textContent.trim().length>1);
    if (!own) continue;
    const cs = getComputedStyle(el);
    sizes.push(cs.fontSize); weights.push(cs.fontWeight);
  }
  out.typography.fontSizes = topN(sizes, 14);
  out.typography.fontWeights = topN(weights, 8);
  return out;
}
"""


def summarize(metrics: dict) -> dict:
    contrast = metrics.get("contrast", [])
    fails = [c for c in contrast if c["pass"] is False]
    return {
        "viewport": metrics.get("viewport"),
        "docBg": metrics.get("docBg"),
        "textNodesChecked": len(contrast),
        "contrastFailures": len(fails),
        "contrastFailureRate": round(len(fails) / len(contrast), 4) if contrast else None,
        "worstFailures": sorted(
            fails, key=lambda c: c["ratio"])[:15],
        "tapTargetUnder32": len(metrics.get("tapTargets", [])),
        "tapTargetSamples": metrics.get("tapTargets", [])[:15],
        "horizontalOverflow": len(metrics.get("overflow", [])),
        "overflowSamples": metrics.get("overflow", [])[:10],
        "spacing": metrics.get("spacing"),
        "typography": metrics.get("typography"),
    }


def run(out_dir: Path) -> int:
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    all_summaries = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox",
                                         "--disable-dev-shm-usage"])
        for tag, width, height in VIEWS:
            ctx = browser.new_context(viewport={"width": width,
                                               "height": height})
            page = ctx.new_page()
            page.goto(BASE, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3500)
            metrics = page.evaluate(JS)
            (out_dir / f"metrics-{tag}.json").write_text(
                json.dumps(metrics, ensure_ascii=False, indent=2),
                encoding="utf-8")
            s = summarize(metrics)
            s["view"] = tag
            all_summaries.append(s)
            ctx.close()
        browser.close()

    (out_dir / "summary.json").write_text(
        json.dumps(all_summaries, ensure_ascii=False, indent=2),
        encoding="utf-8")

    for s in all_summaries:
        print(f"=== {s['view']} ===")
        print(f"  背景色: {s['docBg']}")
        print(f"  文本节点数: {s['textNodesChecked']}  "
              f"对比度不达标: {s['contrastFailures']} "
              f"({(s['contrastFailureRate'] or 0)*100:.1f}%)")
        for f in s["worstFailures"][:6]:
            print(f"    [{f['ratio']} < {f['need']}] {f['tag']}.{f['cls'][:34]} "
                  f"'{f['text'][:22]}'")
        print(f"  触控目标 <32px: {s['tapTargetUnder32']}")
        print(f"  横向溢出元素: {s['horizontalOverflow']}")
    print(f"\n输出: {out_dir}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/root/evidence/watchdog7-c")
    args = ap.parse_args()
    sys.exit(run(Path(args.out)))
