#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7：前端「用户可见注释 / 调试文案 / 占位符 / 内部黑话」DOM 级审计。

【为什么必须真浏览器 + 可见性过滤】
  * 源码里的注释不渲染；但注释一旦被写进 innerHTML / textContent
    就会**真的**显示给用户 —— 源码扫描抓不到，只有渲染后的 DOM 抓得到。
  * placeholder / title / aria-label / value / alt **不在文本节点里** ——
    遍历文本节点会漏掉它们，必须单独扫可见元素的属性值。
  * document.title 单独抓。
  * **可见性过滤是必须的**：本项目有大量**隐藏层**（提示词增强的系统提示词
    textarea、Agent 的系统提示词 textarea）装着带 markdown 强调的原文。
    不按可见性过滤会把它们误判成污染（上一轮审计工具就是没过滤）。

【与上一轮（r2_visible_text_audit.py）的差别】
  上一轮：全量文本节点（**不过滤可见性**）+ 22 类标记。
  本轮：① 按可见性过滤；② 增补「内部工程黑话 / 源码片段」标记；
        ③ 增补 toast / alert / 页面标题三道面；④ 覆盖 Agent 面板与生成库。

用法：
    /home/ubuntu/comfyui/venv/bin/python scripts/r7_dom_visible_audit.py \
        [base_url] [out_dir]
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv[1:]) else "http://127.0.0.1:8190"
OUT = Path(sys.argv[2] if len(sys.argv[2:]) else "evidence/r7-dom-visible-audit")
REPO = Path(__file__).resolve().parent.parent
OUT.mkdir(parents=True, exist_ok=True)

#: 面板清单（上一轮 13 档 + Agent + 生成库）。
PANELS = [
    ("t2v", "文生视频"), ("flf", "首尾帧生视频"), ("r2v", "参考生视频"),
    ("t2i", "文生图"), ("t2i_qwen", "文生图 Qwen"), ("i2i_qwen", "图片编辑 Qwen"),
    ("t2i_moss", "文生图 MOSS"), ("t2i_h3image", "文生图 H3-Image"),
    ("i2i_h3image", "图片编辑 H3-Image"), ("t2a", "文生音乐"),
    ("a2a", "音频编辑"), ("longfilm", "长片拼接"), ("assets", "素材库"),
    ("agent", "Agent 对话"), ("jobs", "生成库"),
]

#: (类别, 正则)。命中 = 用户可见界面里出现了这个标记。
MARKERS = [
    # --- 开发注释 ---
    ("开发注释", r"\b(?:TODO|FIXME|HACK|WIP|XXX)\b"),
    ("开发注释", r"待实现|待办|未实现|待补|待填|占位文字|占位文本|此处占位"),
    ("开发注释", r"lorem\s+ipsum|\?{3,}"),
    # --- 调试文案 ---
    ("调试文案", r"调试|\bdebug\b|\bmock\b|\bstub\b|\bdummy\b"),
    ("调试文案", r"测试用|测试文案|假数据|模拟数据|内测|灰度"),
    # --- 内部术语 / 内部黑话（本轮新增）---
    ("内部黑话", r"nonebot"),
    ("内部黑话", r"minimax_h3_seg\d|minimax_h3_v\d"),
    ("内部黑话", r"SelfLift|H3RelayLatentLoad|SamplerCustomAdvanced|"
                 r"BasicScheduler|noise_mask"),
    ("内部黑话", r"拷贝桥|钉住区|潜空间|latent"),
    ("内部黑话", r"段头断点比|灰阶|色档跳变"),
    # --- 机器可读标识 ---
    ("机器标识", r"\bprompt_id\b|\bjob_id\b|\bjobId\b|\bchain_id\b|\bchainId\b"),
    ("机器标识", r"\basset_id\b|\bref_video_frame_count\b|\bchain_total_frames\b"),
    ("机器标识", r"\blatent_run_id\b|\bsource_latent_path\b|\bstage_index\b"),
    # --- 源码 / 本机路径 ---
    ("源码片段", r"\.py:\d+(?:-\d+)?|\.js:\d+(?:-\d+)?|\.json:\d+(?:-\d+)?"),
    ("源码片段", r"(?:default_steps|default_scheduler|default_sampler)\s*="),
    ("源码片段", r"/(?:home|root|mnt|opt|usr|var)/"),
    # --- 栈信息 ---
    ("栈信息", r"Traceback \(most recent call last\)|at Object\.\w+ \(|:\d+:\d+"),
]

#: 合法豁免（**正常的用户可读文案**，不是开发注释泄漏）。
ALLOW_SUBSTRINGS = [
    "暂时无法确认", "暂时无法探测", "暂时无法",
]

#: markdown 强调残留（`**xxx**`）—— 本项目内部大量用它写注释。
MD_MARKER = re.compile(r"\*\*[^*\n]{1,60}\*\*")


def collect(page) -> dict:
    """抓可见文本 / 可见属性 / 页面标题。

    【可见性判据】元素自身有 client rects（非零尺寸）且没有 hidden。
    文本节点看它**父元素**的盒子。
    """
    return page.evaluate(
        """() => {
      const isVisible = (el) => {
        if (!el) return false;
        if (el.hidden) return false;
        const p = el.parentElement;
        if (p && p.hidden) return false;
        try {
          const st = window.getComputedStyle(el);
          if (st && (st.display === 'none' || st.visibility === 'hidden')) return false;
        } catch (e) { /* ignore */ }
        return !!(el.getClientRects().length);
      };
      const texts = [];
      const hiddenTexts = [];
      const walker = document.createTreeWalker(
        document.body, NodeFilter.SHOW_TEXT, null);
      let node;
      while ((node = walker.nextNode())) {
        const t = (node.nodeValue || '').trim();
        if (!t) continue;
        if (isVisible(node.parentElement)) texts.push(t);
        else hiddenTexts.push(t);
      }
      const attrs = [];
      for (const el of document.querySelectorAll('*')) {
        const vis = isVisible(el);
        for (const a of el.attributes) {
          if (!a.value || !a.value.trim()) continue;
          attrs.push({name: a.name, value: a.value.trim(), visible: vis});
        }
      }
      return {
        texts, hiddenTexts, attrs, title: document.title,
        html: document.documentElement.outerHTML,
      };
    }"""
    )


def scan(haystack: list, where: str) -> list:
    hits = []
    for item in haystack:
        text = item if isinstance(item, str) else item.get("value", "")
        if any(a in text for a in ALLOW_SUBSTRINGS):
            continue
        for cat, pat in MARKERS:
            m = re.search(pat, text)
            if not m:
                continue
            hits.append({"where": where, "category": cat,
                         "marker": m.group(0), "text": text[:240]})
            break
    return hits


def main() -> int:
    from playwright.sync_api import sync_playwright

    console_errors: list[str] = []
    page_errors: list[str] = []
    per_panel: list[dict] = []
    all_texts: list[str] = []
    all_attrs: list[dict] = []
    hidden_texts: list[str] = []
    titles: list[str] = []
    dialogs: list[str] = []
    toasts: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path="/usr/bin/google-chrome",
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()
        page.on("console", lambda m: console_errors.append(f"{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on("dialog", lambda d: (dialogs.append(f"{d.type}: {d.message}"),
                                     d.accept()))

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
                        const el = document.getElementById('navAssets');
                        if (el && el.click) { el.click(); return true; }
                        return false;
                      }
                      if (mode === 'agent') {
                        const el = document.getElementById('navAgent');
                        if (el && el.click) { el.click(); return true; }
                        return false;
                      }
                      if (mode === 'jobs') {
                        const el = document.getElementById('navLibrary')
                          || document.querySelector('[data-tab="jobs"]');
                        if (el && el.click) { el.click(); return true; }
                        return false;
                      }
                      if (typeof window.activateVideoMode === 'function') {
                        window.activateVideoMode(mode); return true;
                      }
                      return false;
                    }""",
                    key)
            except Exception as exc:  # pragma: no cover
                print(f"[warn] 切换面板 {key} 失败: {exc}")
            page.wait_for_timeout(1200)

            data = collect(page)
            all_texts.extend(data["texts"])
            hidden_texts.extend(data["hiddenTexts"])
            all_attrs.extend(data["attrs"])
            titles.append(data["title"])
            shot = OUT / f"panel-{key}.png"
            try:
                page.screenshot(path=str(shot), full_page=False)
            except Exception:
                pass
            per_panel.append({
                "mode": key, "label": label, "switched": switched,
                "visible_texts": len(data["texts"]),
                "hidden_texts": len(data["hiddenTexts"]),
                "attrs": len(data["attrs"]),
                "new_console_errors": len(console_errors) - before_err,
                "new_pageerrors": len(page_errors) - before_perr,
                "screenshot": str(shot),
            })
            print(f"[panel] {key:12s} switched={switched} "
                  f"visible={len(data['texts']):5d} hidden={len(data['hiddenTexts']):5d} "
                  f"attrs={len(data['attrs']):5d}")

        # 页面标题
        title_now = page.title()
        titles.append(title_now)

        # toast（接续流程的非阻塞提示，项目里有正式出口 window.__h3ContinuationToast）
        cont_toast = page.evaluate("() => window.__h3ContinuationToast || ''")
        if cont_toast:
            toasts.append(cont_toast)

        browser.close()

    def uniq(seq):
        seen, out = set(), []
        for item in seq:
            key = item if isinstance(item, str) else json.dumps(item, sort_keys=True)
            if key not in seen:
                seen.add(key)
                out.append(item)
        return out

    texts = uniq(all_texts)
    attrs = uniq(all_attrs)
    visible_attrs = [a for a in attrs if a["visible"]]
    hidden_texts = uniq(hidden_texts)

    (OUT / "dom_visible_texts.txt").write_text("\n".join(texts), encoding="utf-8")
    (OUT / "dom_visible_attrs.txt").write_text(
        "\n".join(f"{a['name']}={a['value']}" for a in visible_attrs),
        encoding="utf-8")
    (OUT / "dom_hidden_texts.txt").write_text("\n".join(hidden_texts),
                                              encoding="utf-8")

    findings = (scan(texts, "visible_text")
                + scan([a["value"] for a in visible_attrs], "visible_attribute")
                + scan(titles, "document_title")
                + scan(toasts, "toast")
                + scan(dialogs, "dialog"))

    md_hits = []
    for s in texts + [a["value"] for a in visible_attrs]:
        for m in MD_MARKER.findall(s):
            md_hits.append({"marker": m, "text": s[:240]})

    result = {
        "base": BASE,
        "sampled_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "panels": per_panel,
        "sampling": {"visible_texts": len(texts),
                     "visible_attrs": len(visible_attrs),
                     "hidden_texts": len(hidden_texts)},
        "console_errors": console_errors,
        "page_errors": page_errors,
        "dialogs": dialogs,
        "toasts": toasts,
        "titles": uniq(titles),
        "findings": findings,
        "markdown_bold": md_hits,
    }
    (OUT / "scan_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"可见文本 {len(texts)} 条 / 可见属性 {len(visible_attrs)} 条 / "
          f"隐藏文本 {len(hidden_texts)} 条")
    print(f"命中 {len(findings)} 条 / markdown 残留 {len(md_hits)} 条")
    for f in findings:
        print(f"  - [{f['category']}] {f['marker']!r} ({f['where']}) "
              f"{f['text'][:140]}")
    print(f"报告落盘：{OUT / 'scan_result.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
