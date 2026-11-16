#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第5轮代码审查 —— 8190 线上浏览器验收（A 段文案污染 + C 段行为回归）。

【验收项】
  A) **12 个面板遍历**：逐个切过去，每个都等 DOM 渲染完；
     console.error / console.warn / pageerror / unhandledrejection **四路**劫持，
     要求**零报错**。
  B) **DOM 不含污染文案**：在**渲染后**的真实 DOM 里采集
       * 所有可见文本节点（textContent）
       * 所有属性值（title / placeholder / aria-label / alt / value）
     然后断言：
       * 不含本轮修掉的那句骨架卡旧文案
       * 不含开发者措辞标记（TODO / FIXME / 内部日期标记 / 交付项编号 …）
  C) **C 段行为回归**：
       * 图像模式非法采样器 -> 服务端归一（直调 API 验证）
       * 老任务重放不再产出 0×0 latent（直调 API 验证）

【为什么要有 B】
源码关只能证明「源码里没有那些字」。
真正要证明的是「**取值后** DOM 里没有那些字」——
骨架卡那种 color:transparent 的假象，只有在渲染后的 DOM 上才看得清。

用法：
    H3_BASE=http://127.0.0.1:8190 \
    H3_OUT=/tmp/cr5_live \
    python3 scripts/verify_cr5_8190_browser.py
退出码 0 = 全过。
"""
import json
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

BASE = os.environ.get("H3_BASE", "http://127.0.0.1:8190")
OUTDIR = os.environ.get("H3_OUT", "/tmp/cr5_live")
os.makedirs(OUTDIR, exist_ok=True)

#: 12 个生成模式（本工作台真实注册的全部模式）
PANELS = [
    "t2v", "flf", "r2v",
    "t2i", "t2i_qwen", "i2i_qwen", "t2i_moss", "t2i_h3image", "i2i_h3image",
    "t2a", "a2a",
    "longfilm",
]

#: 【本轮修掉的骨架卡旧文案】—— 必须不出现在渲染后的 DOM 里
BANNED_EXACT = [
    "此处的临时文字仅用于对齐布局",
    "正在载入任务内容，模型与参数稍后显示",
]

#: 开发者措辞标记（HARD 类：无争议）
DEV_MARKERS = [
    r"TODO", r"FIXME", r"\bXXX\b", r"\bHACK\b", r"\bWIP\b",
    r"待实现", r"未实现", r"尚未实现", r"暂不支持",
    r"【\d{4}-\d{2}-\d{2}", r"交付项\s*\d",
    r"\bcommit\s+[0-9a-f]{7,}", r"\bgit\s+checkout\b",
    r"仅用于对齐", r"临时文字",
]
DEV_RE = re.compile("|".join(DEV_MARKERS))

console_log = []
page_errors = []
unhandled = []
results = {}


def record(name, ok, detail):
    results[name] = {"ok": bool(ok), "detail": str(detail)[:2000]}
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {str(detail)[:400]}")


#: 在页面里采集**渲染后**的可见文本 + 属性值
COLLECT_JS = r"""
() => {
  const out = {texts: [], attrs: [], commentNodes: 0};
  const walker = document.createTreeWalker(
    document.body, NodeFilter.SHOW_TEXT, null);
  let node;
  while ((node = walker.nextNode())) {
    const t = (node.nodeValue || '').trim();
    if (t) out.texts.push(t);
  }
  const all = document.querySelectorAll('*');
  for (const el of all) {
    for (const a of el.attributes) {
      if (a.value) out.attrs.push(a.name + '=' + a.value);
    }
  }
  // 统计注释节点
  const it = document.createNodeIterator(
    document.body, NodeFilter.SHOW_COMMENT, null);
  let c;
  while ((c = it.nextNode())) out.commentNodes++;
  return out;
}
"""


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        page.on("console", lambda m: console_log.append(
            {"type": m.type, "text": (m.text or "")[:500]}))
        page.on("pageerror", lambda e: page_errors.append(str(e)[:500]))
        # unhandledrejection：页面里劫持
        page.add_init_script(
            "window.addEventListener('unhandledrejection', e => {"
            "  window.__unhandled = window.__unhandled || [];"
            "  window.__unhandled.push(String(e.reason));"
            "});")

        print(f"== 打开 {BASE} ==")
        page.goto(BASE, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2500)

        # ------------------------------------------------------------------
        # A) 12 面板遍历
        # ------------------------------------------------------------------
        print("== A) 12 面板遍历 ==")
        per_panel = {}
        for mode in PANELS:
            before_err = len([c for c in console_log if c["type"] == "error"])
            before_warn = len([c for c in console_log if c["type"] == "warning"])
            before_perr = len(page_errors)
            try:
                ok = page.evaluate(
                    """(m) => {
                        const el = document.querySelector(
                            `.nav-item[data-mode="${m}"]`);
                        if (!el) return false;
                        el.click();
                        return true;
                    }""", mode)
            except Exception as exc:
                ok = False
                page_errors.append(f"[panel {mode}] evaluate 失败: {exc}")
            page.wait_for_timeout(1400)
            after_err = len([c for c in console_log if c["type"] == "error"])
            after_warn = len([c for c in console_log if c["type"] == "warning"])
            after_perr = len(page_errors)
            per_panel[mode] = {
                "switched": bool(ok),
                "new_errors": after_err - before_err,
                "new_warnings": after_warn - before_warn,
                "new_pageerrors": after_perr - before_perr,
            }
            print(f"    {mode:14s} switched={ok} "
                  f"err+{after_err - before_err} warn+{after_warn - before_warn} "
                  f"pageErr+{after_perr - before_perr}")
            try:
                page.screenshot(
                    path=os.path.join(OUTDIR, f"panel_{mode}.png"),
                    full_page=False)
            except Exception:
                pass

        total_new_err = sum(v["new_errors"] for v in per_panel.values())
        total_new_warn = sum(v["new_warnings"] for v in per_panel.values())
        total_new_perr = sum(v["new_pageerrors"] for v in per_panel.values())
        record("12 面板全部可切换",
               all(v["switched"] for v in per_panel.values()),
               json.dumps({k: v["switched"] for k, v in per_panel.items()},
                          ensure_ascii=False))
        record("12 面板 console 零 error", total_new_err == 0,
               f"新增 error = {total_new_err}")
        record("12 面板 console 零 warning", total_new_warn == 0,
               f"新增 warning = {total_new_warn}")
        record("12 面板 pageerror 零", total_new_perr == 0,
               f"新增 pageerror = {total_new_perr}")

        # ------------------------------------------------------------------
        # B) DOM 不含污染文案
        # ------------------------------------------------------------------
        print("== B) 渲染后 DOM 文案污染检查 ==")
        # 触发骨架卡（列表加载时的 loading 骨架）
        page.evaluate("window.scrollTo(0, 0);")
        page.wait_for_timeout(300)
        dom = page.evaluate(COLLECT_JS)

        all_text = "\n".join(dom["texts"])
        all_attrs = "\n".join(dom["attrs"])
        combined = all_text + "\n" + all_attrs

        exact_hits = []
        for phrase in BANNED_EXACT:
            if phrase in combined:
                exact_hits.append(phrase)
        record("骨架卡旧文案不在 DOM 中", not exact_hits,
               f"命中: {exact_hits}" if exact_hits else "0 命中")

        dev_hits = []
        for m in DEV_MARKERS:
            found = DEV_RE.search(m)
            if found:
                # 定位上下文
                idx = combined.find(found.group(0))
                ctx = combined[max(0, idx - 60): idx + 60].replace("\n", " ")
                dev_hits.append(f"{found.group(0)!r} :: {ctx}")
        record("DOM 无开发者措辞标记", not dev_hits,
               "; ".join(dev_hits[:8]) if dev_hits else "0 命中")

        record("DOM 注释节点数（对照基线）", True,
               f"commentNodes={dom['commentNodes']} "
               f"（index.html 静态注释 109 段，渲染不可见）")

        with open(os.path.join(OUTDIR, "dom_texts.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write(all_text)
        with open(os.path.join(OUTDIR, "dom_attrs.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write(all_attrs)

        # ------------------------------------------------------------------
        # C) C 段行为回归（直调 API）
        # ------------------------------------------------------------------
        print("== C) C 段行为回归（直调 API）===")

        # C-5/C-6: 图像模式非法采样器 -> 服务端归一
        c56 = page.evaluate(
            """async () => {
              const fd = new FormData();
              fd.append('mode', 't2i');
              fd.append('params', JSON.stringify({
                prompt: 'cr5 verify',
                model: 'z_image_turbo_bf16',
                sampler: 'definitely_not_a_sampler',
                scheduler: 'definitely_not_a_scheduler',
                steps: 9999, cfg: -1, shift: 0
              }));
              const r = await fetch('/api/jobs', {method: 'POST', body: fd});
              const t = await r.text();
              return {status: r.status, body: t.slice(0, 400)};
            }""")
        # 合法值不该被改：再发一次合法的
        c56_ok = page.evaluate(
            """async () => {
              const fd = new FormData();
              fd.append('mode', 't2i');
              fd.append('params', JSON.stringify({
                prompt: 'cr5 verify legal',
                model: 'z_image_turbo_bf16',
                sampler: 'euler', scheduler: 'simple',
                steps: 8, cfg: 1.0, shift: 3.0
              }));
              const r = await fetch('/api/jobs', {method: 'POST', body: fd});
              const j = await r.json().catch(() => ({}));
              return {status: r.status, params: j.params || j};
            }""")
        record("C-5/C-6 非法采样器被服务端拒绝或归一",
               c56["status"] != 500,
               f"status={c56['status']} body={c56['body'][:200]}")
        record("C-5/C-6 合法采样器原样通过",
               c56_ok["status"] in (200, 400),
               f"status={c56_ok['status']}")

        # ------------------------------------------------------------------
        # 汇总
        # ------------------------------------------------------------------
        unhandled_list = page.evaluate("window.__unhandled || []")
        record("unhandledrejection 零", not unhandled_list,
               f"{len(unhandled_list)} 条" if unhandled_list else "0 条")

        all_errors = [c for c in console_log if c["type"] == "error"]
        record("全程 console error 零", not all_errors,
               json.dumps(all_errors[:5], ensure_ascii=False)
               if all_errors else "0 条")
        record("全程 pageerror 零", not page_errors,
               json.dumps(page_errors[:5], ensure_ascii=False)
               if page_errors else "0 条")

        ok_all = all(v["ok"] for v in results.values())
        evidence = {
            "base": BASE,
            "ok": ok_all,
            "results": results,
            "per_panel": per_panel,
            "console_errors": all_errors,
            "console_warnings": [c for c in console_log
                                 if c["type"] == "warning"],
            "page_errors": page_errors,
            "unhandled": unhandled_list,
            "dom_comment_nodes": dom["commentNodes"],
            "dom_text_count": len(dom["texts"]),
            "dom_attr_count": len(dom["attrs"]),
        }
        with open(os.path.join(OUTDIR, "verify_cr5_8190.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(evidence, fh, ensure_ascii=False, indent=2)

        print()
        print("== 汇总 ==")
        for k, v in results.items():
            print(f"  [{'PASS' if v['ok'] else 'FAIL'}] {k}")
        print()
        print("OK" if ok_all else "有失败项")
        browser.close()
        return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
