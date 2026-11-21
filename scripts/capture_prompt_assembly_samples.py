# -*- coding: utf-8 -*-
"""组装样本取证：三组真实「原始提示词」组装结果（纯中文 / 只填部分 / 含自定义）。

【为什么单独一个脚本】任务书要求「给出一组实际证据」——
纯中文选项的组装结果、仅填部分维度（其余省略）的组装结果、
含自行输入自定义项的组装结果。这三组必须来自**真实浏览器里的真实操作**，
不能是手写的期望值。脚本跑完把结果落成 JSON + markdown，供报告引用。

用法：
    python3 scripts/capture_prompt_assembly_samples.py <base_url> <out_dir>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8391"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else
           "/root/projects/h3studio-creative-prompt/evidence/prompt-catalog/samples")
OUT.mkdir(parents=True, exist_ok=True)


def open_designer(page):
    page.wait_for_selector("#pcOpenBtn", timeout=30000)
    page.evaluate("""() => {
      const panel = document.querySelector('#panel');
      const btn = document.querySelector('#pcOpenBtn');
      if (panel && btn) panel.scrollTop = btn.offsetTop - 80;
    }""")
    page.wait_for_timeout(300)
    page.click("#pcOpenBtn")
    page.wait_for_timeout(800)


def clear(page):
    page.evaluate("""() => {
      window.PROMPT_CATALOG.picks.clear();
      window.PROMPT_CATALOG.customs.clear();
      window.PROMPT_CATALOG.render();
    }""")
    page.wait_for_timeout(400)


def pick(page, label, prefix):
    r = page.evaluate("""([label, prefix]) => {
      const s = Array.from(document.querySelectorAll('#pcGroups select[data-select]'))
        .find(x => x.getAttribute('aria-label') === label);
      if (!s) return 'NO_SELECT:' + label;
      const o = Array.from(s.options).find(x => x.textContent.trim().startsWith(prefix));
      if (!o) return 'NO_OPTION:' + prefix;
      if (o.disabled) return 'DISABLED:' + o.title;
      s.value = o.value;
      s.dispatchEvent(new Event('change', {bubbles: true}));
      return 'OK';
    }""", [label, prefix])
    page.wait_for_timeout(300)
    return r


def custom(page, label, text):
    r = page.evaluate("""([label, text]) => {
      const i = Array.from(document.querySelectorAll('#pcGroups [data-custom]'))
        .find(x => x.getAttribute('aria-label') === label + ' 自行输入');
      if (!i) return 'NO_INPUT:' + label;
      i.value = text;
      i.dispatchEvent(new Event('input', {bubbles: true}));
      return 'OK';
    }""", [label, text])
    page.wait_for_timeout(300)
    return r


def snapshot(page):
    return page.evaluate("""() => ({
      parts: window.PROMPT_CATALOG.assemble().parts,
      text: window.PROMPT_CATALOG.assemble().text,
      en_parts: window.PROMPT_CATALOG.assemble({english: true}).parts,
      preview: (document.querySelector('#pcPreviewText') || {}).innerText || '',
    })""")


# 三组样本：(编号, 标题, [(子类名, 选项前缀, 是否自定义, 自定义文本)])
SAMPLES = [
    ("01-zh-only", "纯中文选项（只从词库里选，全部用中文显示名）", [
        ("3.0 主体类别（顶层，决定后面哪些子维度可用）", "人物（单人）", False, ""),
        ("3.3 发型与发色", "长直发", False, ""),
        ("3.5 上装", "衬衫", False, ""),
        ("1.1 动漫 / 二次元", "赛璐璐动画", False, ""),
        ("4.1 场景类型", "森林", False, ""),
        ("5.A 经典构图", "三分法", False, ""),
        ("2.2 景别", "中景", False, ""),
        ("7.1 自然光时段与天气光", "黄金时刻", False, ""),
        ("6.1 配色方案", "互补色", False, ""),
        ("8.1 氛围与情绪", "宁静", False, ""),
    ]),
    ("02-partial", "只填部分维度（其余 43 个分组全部「不指定」跳过）", [
        ("3.0 主体类别（顶层，决定后面哪些子维度可用）", "人物（单人）", False, ""),
        ("7.1 自然光时段与天气光", "黄金时刻", False, ""),
    ]),
    ("03-custom", "含自行输入自定义项（词库选项 + 自定义内容一起组装）", [
        ("3.0 主体类别（顶层，决定后面哪些子维度可用）", "人物（单人）", False, ""),
        ("4.4 远景 / 背景", "", True, "远景是雾中的高压电塔"),
        ("1.1 动漫 / 二次元", "赛璐璐动画", False, ""),
        ("1.1 动漫 / 二次元", "", True, "复古漫画网点"),
    ]),
]

results = []
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True,
                           args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = b.new_context(viewport={"width": 1440, "height": 950})
    page = ctx.new_page()
    page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3500)
    page.click('.nav-item[data-mode="t2i"]')
    page.wait_for_timeout(1200)
    open_designer(page)

    for sid, title, steps in SAMPLES:
        clear(page)
        ops = []
        for label, opt, is_custom, text in steps:
            if is_custom:
                ops.append((label, custom(page, label, text)))
            else:
                ops.append((label, pick(page, label, opt)))
        snap = snapshot(page)
        # 真的写回 #prompt（走覆盖/追加链路）
        page.click("#pcWriteBtn")
        page.wait_for_timeout(700)
        wrote = page.evaluate("""() => {
          const p = document.querySelector('#prompt');
          return {value: p.value,
                  hasChoice: !!document.querySelector('#pcOverwrite')};
        }""")
        if wrote["hasChoice"]:
            page.click("#pcOverwrite")
            page.wait_for_timeout(600)
            wrote = page.evaluate(
                "() => ({value: document.querySelector('#prompt').value, hasChoice: false})")
        snap["prompt_after_writeback"] = wrote["value"]
        snap["ops"] = ops
        snap["id"] = sid
        snap["title"] = title
        results.append(snap)
        print(f"--- {sid} ---")
        print("parts:", snap["parts"])
        print("text :", snap["text"])
        print("write:", snap["prompt_after_writeback"])
        print("en   :", snap["en_parts"])
    ctx.close()
    b.close()

(OUT / "assembly-samples.json").write_text(
    json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

md = ["# 「创意内容设计」三组真实组装结果（真实浏览器取证）", ""]
for r in results:
    md += [f"## {r['id']} —— {r['title']}", "",
           f"- 组装片段（{len(r['parts'])} 段）：" + "、".join(f"`{p}`" for p in r["parts"]),
           f"- 原始提示词：`{r['text']}`",
           f"- 写回 #prompt 后：`{r['prompt_after_writeback']}`",
           f"- 英文片段辅助版本：" + "、".join(f"`{p}`" for p in r["en_parts"]),
           ""]
(OUT / "assembly-samples.md").write_text("\n".join(md), encoding="utf-8")
print("WROTE", OUT / "assembly-samples.json")
print("WROTE", OUT / "assembly-samples.md")
