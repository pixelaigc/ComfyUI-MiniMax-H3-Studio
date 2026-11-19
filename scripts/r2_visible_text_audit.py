#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R2：前端「可见注释 / 调试文案」专项真实浏览器取证。

【为什么必须用真实浏览器】
用户的原始要求是「**注释内容不得出现在用户可见界面里**」。
这��判定的唯一可靠依据是**渲染后 DOM 里到底有什么**，
而不是「源码里有没有这段文字」—— 源码里的注释当然存在，
关键是它有没有变成 DOM 文本节点 / 属性值。

【与第 6 轮 cr6_dom_audit.py 的区别】
cr6 聚焦 markdown 强调残留（`**xxx**`）。本轮（R2）范围更宽：
  * 开发注释标记：TODO / FIXME / HACK / WIP / XXX / 待实现 / 待办 …
  * 调试文案：调试 / debug / mock / stub / dummy / 测试用 / 假数据 …
  * 占位符：lorem ipsum / 待填 / ???
  * 内部术语：内部专用 / 仅供内部 / 开发用 / 接口返回 / 字段名 …
  * 机器可读标识：prompt_id / job_id / chain_id / asset_id / 栈信息 …
  * **源码位置回溯**：每条命中都回指到 web/ 下的具体文件+行，
    便于「改哪里」；不能只给一句结论。

【输出】
  evidence/20261119-r2-visible-text/
    dom_texts.txt        全部可见文本节点
    dom_attrs.txt        全部属性值
    scan_result.json     逐标记命中 + 源码回溯
    panel-*.png          每个面板截图
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv[1:]) else "http://127.0.0.1:8190"
OUT = Path(sys.argv[2] if len(sys.argv[2:]) else "evidence/20261119-r2-visible-text")
REPO = Path(__file__).resolve().parent.parent
OUT.mkdir(parents=True, exist_ok=True)

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
    ("assets", "素材库"),
]

#: (类别, 标记)。命中 = 用户可见界面里出现了这个标记。
MARKERS = [
    ("开发注释", r"\b(?:TODO|FIXME|HACK|WIP)\b"),
    ("开发注释", r"XXX"),
    ("开发注释", r"待实现|待办|未实现|待补|待填"),
    ("调试文案", r"调试"),
    ("调试文案", r"\bdebug\b"),
    ("调试文案", r"\bmock\b|\bstub\b|\bdummy\b"),
    ("调试文案", r"测试用|测试文案|假数据|模拟数据"),
    ("调试文案", r"内测|灰度"),
    ("占位符", r"lorem\s+ipsum"),
    ("占位符", r"\?{3,}"),
    ("内部术语", r"内部专用|仅供内部|内部使用|内部标识"),
    ("内部术语", r"开发用|开发阶段|实验性|临时方案"),
    ("内部术语", r"接口返回|后端返回|字段名"),
    ("机器标识", r"\bprompt_id\b"),
    ("机器标识", r"\bjob_id\b|\bjobId\b"),
    ("机器标识", r"\bchain_id\b|\bchainId\b"),
    ("机器标识", r"\basset_id\b"),
    ("机器标识", r"\bref_video_frame_count\b|\bchain_total_frames\b"),
    ("机器标识", r"\blatent_run_id\b|\bsource_latent_path\b"),
    ("栈信息", r"Traceback \(most recent call last\)|at Object\.\w+ \(|:\d+:\d+"),
    ("英文口癖", r"\bfor now\b|\btemp fix\b|\bworkaround\b|\bquick fix\b"),
]

#: 合法豁免 —— 这些是**正常的用户可读错误提示**，不是开发注释泄漏。
#: 例：ComfyUI 忙/不可达时提示「暂时无法探测本机 ComfyUI 能力」是给用户看的。
ALLOW_SUBSTRINGS = [
    "暂时无法确认",
    "暂时无法探测",
    "暂时无法",
]

MD_MARKER = re.compile(r"\*\*[^*\n]{1,60}\*\*")


def collect(page):
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
        for (const a of el.attributes) attrs.push(a.value);
      }
      return { texts, attrs, title: document.title };
    }"""
    )


def build_source_index() -> list[tuple[str, int, str]]:
    """web/ 下所有 (文件, 行号, 行文本) —— 用于把 DOM 文本回溯到源码。"""
    idx = []
    for p in sorted((REPO / "web").glob("*")):
        if p.suffix not in (".js", ".html", ".css"):
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, ln in enumerate(txt.split("\n"), 1):
            idx.append((str(p.relative_to(REPO)), i, ln))
    return idx


def trace_source(text: str, idx: list[tuple[str, int, str]]) -> list[str]:
    """把一段 DOM 文本回溯到源码位置（取该文本里最长的一个特征片段）。"""
    frag = text.strip()
    if len(frag) > 60:
        frag = frag[:60]
    if len(frag) < 3:
        return []
    hits = []
    for f, i, ln in idx:
        if frag in ln:
            hits.append(f"{f}:{i}")
            if len(hits) >= 4:
                break
    if not hits:
        # 退一步：用前 12 字
        short = frag[:12]
        for f, i, ln in idx:
            if short and short in ln:
                hits.append(f"{f}:{i}")
                if len(hits) >= 3:
                    break
    return hits


def main() -> int:
    from playwright.sync_api import sync_playwright

    console_errors: list[str] = []
    page_errors: list[str] = []
    all_texts: list[str] = []
    all_attrs: list[str] = []
    per_panel: list[dict] = []

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
                      if (mode === 'assets') {
                        const el = document.querySelector(
                          '[data-mode="assets"], #assetsPanel, [href="#assets"]');
                        if (el && el.click) { el.click(); return true; }
                        return false;
                      }
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
                  f"texts={len(data['texts']):5d} attrs={len(data['attrs']):5d}")

        browser.close()

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

    idx = build_source_index()

    def scan(seq, where):
        findings = []
        for text in seq:
            if any(a in text for a in ALLOW_SUBSTRINGS):
                continue
            for cat, pat in MARKERS:
                m = re.search(pat, text, re.I)
                if not m:
                    continue
                findings.append({
                    "category": cat, "marker": m.group(0), "where": where,
                    "text": text[:200],
                    "source": trace_source(text, idx),
                })
                break
        return findings

    findings = scan(texts, "visible_text") + scan(attrs, "attribute_value")
    md_found = []
    for s in texts + attrs:
        for m in MD_MARKER.findall(s):
            md_found.append({"marker": m, "text": s[:200],
                             "source": trace_source(s, idx)})

    result = {
        "base": BASE,
        "sampled_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "panels": per_panel,
        "sampling": {"texts": len(texts), "attrs": len(attrs)},
        "console_errors": console_errors,
        "page_errors": page_errors,
        "findings": findings,
        "markdown_bold": md_found,
    }
    (OUT / "scan_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n═══ 可见文本 {len(texts)} 条 / 属性值 {len(attrs)} 条 ═══")
    print(f"开发者措辞命中: {len(findings)} 条")
    for f in findings:
        print(f"  [{f['category']}] {f['marker']!r} ({f['where']})")
        print(f"     文本: {f['text'][:110]!r}")
        print(f"     源码: {f['source'] or '(未定位)'}")
    print(f"\nmarkdown **xxx** 残留: {len(md_found)} 条")
    for f in md_found[:12]:
        print(f"  {f['marker']!r}  src={f['source']}")
    print(f"\nconsole errors: {len(console_errors)}  pageerrors: {len(page_errors)}")
    for e in (console_errors + page_errors)[:10]:
        print("  ", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
