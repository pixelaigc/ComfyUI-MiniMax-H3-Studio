# -*- coding: utf-8 -*-
"""接续面板（首尾帧 / 参考生视频 / 文生视频）的**用户可见文案**审计。

【这个脚本在防什么】
第 2 项交付要求：「检查该面板用户可见界面无注释/TODO/调试文案/占位符泄漏
（DOM 文本、placeholder、title/aria-label、toast/提示语、页面标题、错误提示均需覆盖）」。

【为什么必须用真实浏览器】
「用户可见」只能靠**渲染后的 DOM** 判定：
  * 源码里的 `// 注释` 不渲染 —— 但注释一旦被写进 innerHTML / textContent
    就会**真的显示给用户**（源码读不出来，只有 DOM 抓得到）；
  * `placeholder` / `title` / `aria-label` **不在文本节点里** ——
    遍历文本节点会漏掉它们，必须单独抓属性值；
  * `document.title` 是页面标题，单独抓。

【审计口径 —— 六道面】
  1. 全部可见文本节点
  2. 全部属性值（placeholder / title / aria-label / value / alt ...）
  3. 页面标题 document.title
  4. toast / 提示语（接续流程弹出来的）
  5. 错误提示（alert 文本）
  6. markdown 强调残留（`**xxx**` —— 本项目内部大量用它写注释，
     一旦进了 DOM 就是脏文案）

【判定词表】
与 scripts/cr5_scan_text_candidates.py 同源（开发者措辞），
另加 markdown 残留与占位符措辞。

【为什么「XXX」不在词表里】
实测 DOM 里确有 `Mystic-XXX V6` 等命中 —— 那是**真实的 LoRA 模型名**
（z-image-effects/Mystic-XXX-ZIT-V6.safetensors），不是开发者措辞。
把它当污染 = 假阳性。所以词表里不含裸 XXX。

用法：
    /home/ubuntu/comfyui/venv/bin/python tests/verify_cont_panel_text_clean.py
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:
    HAS_PLAYWRIGHT = False

from test_frontend_defaults import StudioServer  # noqa: E402

#: 开发者措辞 —— 与 cr5 扫描器同源，但**不含**裸 XXX（那是 LoRA 模型名）。
DEV_MARKERS = [
    "TODO", "FIXME", "HACK", "DEBUG",
    "待实现", "待办", "调试", "内部标识",
    "临时方案", "先这样", "测试用", "test-only", "内部用",
    "占位文字", "占位文本", "此处占位",
    "内部实现", "内部逻辑", "仅供内部", "内部测试", "内部状态", "内部调试",
    "待定", "待联调", "待对接", "待补充", "待完善", "未实现", "尚未实现",
    "先别", "暂时先", "先这么", "先这样",
    "mock", "dummy", "stub", "假数据", "模拟数据",
    "Traceback", "stacktrace",
    "这个函数", "该函数", "注意这里", "看这里", "改这里", "删掉这行",
    "忘了写", "没写完",
]

#: markdown 强调残留（`**xxx**`）—— 本项目内部大量用它写注释。
MD_MARKER = re.compile(r"\*\*[^*\n]{1,60}\*\*")

#: 接续相关的关键词 —— 命中就说明审计面覆盖到了这块。
CONT_KEYWORDS = ("接续", "二采", "色档跳变")


def seed_job(server, job_id: str, mode: str, twopass_enabled: bool = True) -> None:
    """造一条可接续记录（让接续流程能真的跑起来）。"""
    from h3studio import continuation_service as cs

    run_id = cs.segment_run_id(job_id)
    stage = cs.stage_path(run_id, 0)
    stage.parent.mkdir(parents=True, exist_ok=True)
    stage.write_bytes(b"fake-av-latent")
    params = {
        "prompt": "一只橘猫在旧书桌上走过",
        "model_key": "fl2va_int8",
        "width": 768, "height": 448, "duration": 5, "steps": 4,
        "sampler": "euler", "scheduler": "simple", "gen_type": mode,
        "twopass": {"enabled": bool(twopass_enabled)},
        "continuation": {
            "chain_id": job_id, "stage_index": 0, "source_job": "",
            "source_segment": -1, "latent_run_id": run_id, "latent_stage": 0,
            "context_frames": 22, "anchor_frames": 22,
            "chain_total_frames": 0, "segment_frames": 124,
        },
    }
    with sqlite3.connect(server.data_dir / "jobs.db") as conn:
        conn.execute(
            "INSERT OR REPLACE INTO jobs "
            "(id, created_at, mode, params, status, progress, outputs) "
            "VALUES (?,?,?,?,?,?,?)",
            (job_id, time.time(), mode, json.dumps(params, ensure_ascii=False),
             "done", 1.0,
             json.dumps([{"kind": "video", "file": "videos/fake.mp4",
                          "name": "fake.mp4"}], ensure_ascii=False)))


def collect(page) -> dict:
    """抓全部六道面。

    【为什么要过滤可见性】
    文本节点遍历会把**隐藏容器**里的内容也抓出来 —— 实测 DOM 里有两处
    textarea（增强菜单的系统提示词 / Agent 的系统提示词）装着
    `h3studio/skills/prompt-enhancer/assets/*.txt` 的原文（含 `# 标题`
    与 `**强调**`），但它们在 w=0 h=0 的隐藏层里，**用户看不到**。
    不按可见性过滤就会把它们误判成「污染」。

    ⇒ 判据：文本节点的**父元素**必须有实际渲染盒（offsetParent 或
      client rects 非空）且自身没被 hidden。
    """
    return page.evaluate("""() => {
      const isVisible = (el) => {
        if (!el) return false;
        if (el.hidden) return false;
        const p = el.parentElement;
        if (!p) return false;
        if (p.hidden) return false;
        return !!(el.getClientRects().length);
      };
      const texts = [];
      const walker = document.createTreeWalker(
        document.body, NodeFilter.SHOW_TEXT, null);
      let node;
      while ((node = walker.nextNode())) {
        const t = (node.nodeValue || '').trim();
        if (!t) continue;
        // 【可见性判据】文本节点自身没有盒子，看它**父元素**的盒子。
        if (!isVisible(node.parentElement)) continue;
        texts.push(t);
      }
      const attrs = [];
      const visibleAttrs = [];
      for (const el of document.querySelectorAll('*')) {
        const vis = isVisible(el);
        for (const a of el.attributes) {
          if (a.value && a.value.trim()) {
            const rec = {name: a.name, value: a.value.trim(), visible: vis};
            attrs.push(rec);
            if (vis) visibleAttrs.push(rec);
          }
        }
      }
      return {
        texts: texts,
        attrs: attrs,
        visibleAttrs: visibleAttrs,
        title: document.title,
      };
    }""")


def scan(haystack: list, label: str) -> list:
    """在文本列表里找开发者措辞 / markdown 残留。"""
    hits = []
    for item in haystack:
        text = item if isinstance(item, str) else item.get("value", "")
        for marker in DEV_MARKERS:
            if marker in text:
                hits.append({"where": label, "marker": marker, "text": text[:220]})
        m = MD_MARKER.search(text)
        if m:
            hits.append({"where": label, "marker": "markdown", "text": text[:220]})
    return hits


def main() -> int:
    if not HAS_PLAYWRIGHT:
        print("需要 playwright")
        return 2
    server = StudioServer(seed_jobs=0)
    assert server.start(), "Studio 实例启动失败"
    report = {}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox"])
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            dialogs = []
            toasts = []

            def _on_dialog(d):
                dialogs.append(f"{d.type}: {d.message}")
                d.accept()

            page.on("dialog", _on_dialog)
            page.goto(f"http://127.0.0.1:{server.port}/",
                      wait_until="domcontentloaded")
            page.wait_for_timeout(2500)

            # 【toast 的抓法】continuation.js 自带一个非阻塞 toast，
            # 并把文本挂在 window.__h3ContinuationToast 上「方便验收脚本
            # 直接读」。⇒ 用这个正式出口，不自己猜 DOM 容器。

            for mode in ("t2v", "flf", "r2v"):
                job_id = f"txt_{mode}_0001"
                seed_job(server, job_id, mode)
                page.evaluate(
                    "() => { if (window.Continuation) window.Continuation.clear(); }")
                page.wait_for_timeout(250)
                page.evaluate(
                    """async (jid) => {
                         const r = await fetch('/api/jobs/' + jid + '/continuation');
                         const d = await r.json();
                         await window.Continuation.start(
                           { id: jid, mode: d.mode, status: 'done',
                             video_name: 'p.mp4' });
                       }""",
                    job_id)
                page.wait_for_timeout(2200)

                data = collect(page)
                hits = []
                hits += scan(data["texts"], "visible_text")
                hits += scan([a for a in data["visibleAttrs"]], "attribute_value")
                # 页面标题
                hits += scan([data["title"]], "document_title")

                # 【toast 怎么抓】continuation.js 自带一个非阻塞 toast
                # （项目里**没有**全局 window.toast），并把文本挂在
                # window.__h3ContinuationToast 上「方便验收脚本直接读」。
                # ⇒ 用这个正式出口，别自己猜 DOM 容器。
                cont_toast = page.evaluate(
                    "() => window.__h3ContinuationToast || ''")
                hits += scan([cont_toast] if cont_toast else [], "toast")

                # 错误提示：读 alert 收集器
                hits += scan(dialogs, "alert")

                report[mode] = {
                    "text_count": len(data["texts"]),
                    "attr_count": len(data["attrs"]),
                    "title": data["title"],
                    "toast": cont_toast,
                    "dialogs": list(dialogs),
                    "hits": hits,
                    "coverage": {
                        kw: sum(1 for t in data["texts"] if kw in t)
                        for kw in CONT_KEYWORDS
                    },
                }
            browser.close()
    finally:
        server.stop()

    out_dir = REPO / "evidence" / "cont-panel-text-audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(len(v["hits"]) for v in report.values())
    for mode, rec in report.items():
        print(f"[{mode}] 文本 {rec['text_count']} 条 / 属性 {rec['attr_count']} 条"
              f" / 命中 {len(rec['hits'])} 条 / 覆盖 {rec['coverage']}")
        for hit in rec["hits"][:10]:
            print(f"    - [{hit['where']}] {hit['marker']}: {hit['text'][:120]}")
    print()
    print(f"合计命中：{total}")
    print(f"报告落盘：{out_dir / 'report.json'}")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
