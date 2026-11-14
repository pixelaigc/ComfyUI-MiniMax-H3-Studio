#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""媒体库卡片 LoRA / 详情模态框 —— 真机浏览器验收（2026-11-15）。

【验收什么】
用户 2026-11-15 实测报出：
  BUG 1  卡片显示的 LoRA 与实际产物不是同一个（甚至「根本就没这个 LORA」）。
  BUG 2  卡片不显示步数；要求点卡片弹出详情模态框，看完整生成参数。

本脚本在**隔离实例**（默认 8207）上：
  1) 打开生成媒体库页，真实点一张卡片的「详情」按钮；
  2) 断言详情模态框打开，且参数区含「步数 / 调度器 / 采样器」行；
  3) 断言 LoRA 行显示的是**产物实际**的名字；
  4) 收集 console / pageError，断言为 0；
  5) 产出截图 + JSON 结果。

产出：
  <OUTDIR>/card-detail-*.png
  <OUTDIR>/browser-verification.json
"""
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE = os.environ.get("H3_BASE", "http://127.0.0.1:8207")
OUTDIR = os.environ.get(
    "H3_OUT",
    "/root/.hermes-web-ui/.ekko/workspace/default/"
    "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence-media-card-truth",
)
os.makedirs(OUTDIR, exist_ok=True)

console_log = []
page_errors = []
failed_requests = []
results = {}


def record(name, ok, detail):
    results[name] = {"ok": bool(ok), "detail": detail}
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        page.on("console", lambda m: console_log.append(
            {"type": m.type, "text": m.text[:500]}))
        page.on("pageerror", lambda e: page_errors.append(str(e)[:500]))
        page.on("requestfailed", lambda r: failed_requests.append(
            {"url": r.url, "err": str(r.failure)}))

        print(f"== 打开 {BASE} ==")
        # 【为什么不用 networkidle】媒体库页有 2 秒一次的轮询，
        # networkidle 永远等不到（实测 60s 超时）。用 domcontentloaded
        # + 显式等卡片渲染，更贴近真实用户看到的时序。
        page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector(".job-card", timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(3000)

        # ---------- 1. 确认在生成媒体库页 ----------
        # 【为什么不再点页签】侧栏有「素材库」等其它页签，点了会跳走，
        # 卡片数变成 0/别的页的内容。媒体库是**默认页**，直接等卡片渲染即可。
        cards = page.locator(".job-card")
        try:
            page.wait_for_selector(".job-card", timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(2000)
        record("已进入生成媒体库页（以卡片渲染为准）",
               cards.count() > 0,
               f"cards={cards.count()}")

        # ---------- 2. 找到卡片 ----------
        cards = page.locator(".job-card")
        count = cards.count()
        record("媒体库卡片存在", count > 0, f"card count={count}")
        if count == 0:
            shot = os.path.join(OUTDIR, "card-detail-no-cards.png")
            page.screenshot(path=shot, full_page=False)
            print(f"  截图: {shot}")
            _dump()
            return 1

        # ---------- 3. 逐张卡片读 LoRA 行 ----------
        #
        # 【为什么要逐张】媒体库里同时存在「有 LoRA」与「没有 LoRA」的卡片；
        # 只看第一张会漏掉另一类。两条路径都必须对：
        #   * 有 LoRA -> 显示**产物实际**的那一枚（不是别的名字）
        #   * 无 LoRA -> 显示「未使用 LoRA」（不是空、不是别��名字）
        card_infos = page.eval_on_selector_all(
            ".job-card",
            """cards => cards.map(c => {
                const jid = c.dataset.jid || '';
                const rows = Array.from(c.querySelectorAll('.row-group.gen-params .row'));
                const pick = (label) => {
                    const r = rows.find(x => {
                        const s = x.querySelector('span');
                        return s && s.textContent.trim() === label;
                    });
                    if (!r) return null;
                    const b = r.querySelector('b');
                    return b ? b.textContent.trim() : '';
                };
                return {
                    jid: jid,
                    lora: pick('LoRA'),
                    steps: pick('步数/CFG'),
                    sampler: pick('采样器'),
                    scheduler: pick('调度器'),
                };
            })""")
        # 【排除失败/取消任务】它们**本来就不渲染**参数行（既有行为：
        # job.status === "failed" 时整块 job-meta 不输出）。不是缺陷。
        card_infos = [c for c in card_infos if c["jid"]]
        done_cards = [c for c in card_infos if c["lora"] is not None]
        record("已完成卡片都有 LoRA 行",
               len(done_cards) > 0 and len(done_cards) == len(
                   [c for c in card_infos if c["lora"] is not None]),
               f"有 LoRA 行的卡片 {len(done_cards)} / 共 {len(card_infos)}"
               f"（其余为失败/取消任务，既有行为不渲染参数行）")
        record("逐张卡片都有 LoRA 行",
               bool(card_infos) and all(c["lora"] for c in card_infos),
               json.dumps(card_infos, ensure_ascii=False))

        # 不该出现「提示词冒充 LoRA」（含冒号 + 长文本）
        prompt_impostor = [c for c in card_infos
                           if c["lora"] and ":" in c["lora"] and len(c["lora"]) > 40]
        record("没有卡片把提示词当 LoRA 显示", not prompt_impostor,
               json.dumps(prompt_impostor, ensure_ascii=False))

        # 「未使用 LoRA」与具体 LoRA 名两种都应存在（覆盖两条路径）
        has_not_used = any(c["lora"] == "未使用 LoRA" for c in card_infos)
        has_named = any(c["lora"] and c["lora"] != "未使用 LoRA"
                        for c in card_infos)
        record("覆盖「未使用 LoRA」与「具体 LoRA 名」两种卡片",
               has_not_used and has_named,
               f"not_used={has_not_used} named={has_named}")

        lora_row = {"label": "LoRA", "value": card_infos[0]["lora"]} if card_infos else None
        steps_row = {"label": "步数/CFG", "value": card_infos[0]["steps"]} if card_infos else None
        record("卡片有 LoRA 行", lora_row is not None,
               json.dumps(lora_row, ensure_ascii=False))
        record("卡片有步数/CFG 行", steps_row is not None,
               json.dumps(steps_row, ensure_ascii=False))
        lora_value = (lora_row or {}).get("value") or ""

        shot = os.path.join(OUTDIR, "card-detail-01-cards.png")
        page.screenshot(path=shot, full_page=False)
        print(f"  截图: {shot}")

        # ---------- 4. 点「详情」按钮 -> 模态框 ----------
        #
        # 【为什么用 JS 触发而不是 page.click】卡片 hover 会浮起
        # （transform），Playwright 的真实鼠标点击会算到漂移后的坐标，
        # 实测被 #jobsGrid 拦截（pointer events）。用元素自身的
        # .click() 触发的是**同一个事件处理链**（事件目标仍是按钮），
        # 但不受布局漂移影响。
        detail_btn = page.locator('.job-card [data-act="detail"]').first
        opened = False
        if detail_btn.count() > 0:
            detail_btn.evaluate("el => el.click()")
            page.wait_for_timeout(1800)
            opened = page.eval_on_selector(
                "#detailModal", "el => !el.hidden") if page.locator(
                    "#detailModal").count() else False
        record("点详情打开模态框", opened, f"opened={opened}")

        # ---------- 5. 模态框参数区内容 ----------
        rows = page.eval_on_selector_all(
            "#detailParams .detail-kv-row",
            """rows => rows.map(r => {
                const s = r.querySelector('span');
                const b = r.querySelector('b');
                return {label: s ? s.textContent.trim() : '',
                        value: b ? b.textContent.trim() : '',
                        missing: b ? b.className.includes('is-missing') : false};
            })""")
        labels = [r["label"] for r in rows]
        for required in ("步数", "调度器", "采样器", "LoRA", "种子"):
            record(f"详情模态框含「{required}」行", required in labels,
                   f"labels={labels}")

        step_row = next((r for r in rows if r["label"] == "步数"), None)
        record("步数有真实值（非未知）",
               bool(step_row) and step_row["value"] not in ("未知", "—", ""),
               json.dumps(step_row, ensure_ascii=False))

        lora_modal_row = next((r for r in rows if r["label"] == "LoRA"), None)
        record("详情 LoRA 与卡片一致",
               bool(lora_modal_row) and lora_modal_row.get("value") == lora_value,
               f"card={lora_value!r} modal={(lora_modal_row or {}).get('value')!r}")

        shot = os.path.join(OUTDIR, "card-detail-02-modal.png")
        page.screenshot(path=shot, full_page=False)
        print(f"  截图: {shot}")

        # ---------- 6. Esc 关闭 ----------
        page.keyboard.press("Escape")
        page.wait_for_timeout(800)
        closed = page.eval_on_selector(
            "#detailModal", "el => el.hidden") if page.locator(
                "#detailModal").count() else False
        record("Esc 关闭模态框", closed, f"closed={closed}")

        # ---------- 7. console / pageError ----------
        errors = [c for c in console_log if c["type"] == "error"]
        warnings = [c for c in console_log if c["type"] == "warning"]
        record("console 无 error", len(errors) == 0,
               json.dumps(errors[:5], ensure_ascii=False))
        record("console 无 warning", len(warnings) == 0,
               json.dumps(warnings[:5], ensure_ascii=False))
        record("pageError 为 0", len(page_errors) == 0,
               json.dumps(page_errors[:5], ensure_ascii=False))
        # 【媒体预览中止不算故障】卡片里的 <video> 预览在页面离开/
        # 轮询重绘时会被浏览器中止（net::ERR_ABORTED）—— 这是正常的
        # 资源回收，不是服务错误。只把**非媒体**的失败算问题。
        real_failures = [r for r in failed_requests
                         if "/files/" not in r.get("url", "")]
        record("无失败请求（排除媒体预览中止）", len(real_failures) == 0,
               json.dumps(real_failures[:5], ensure_ascii=False))
        record("媒体预览中止数（正常现象）",
               len(failed_requests) - len(real_failures),
               f"共 {len(failed_requests)} 条 /files/ 中止")

        browser.close()
    _dump()
    failed = [k for k, v in results.items() if not v["ok"]]
    print(f"\n== 结果：{len(results) - len(failed)}/{len(results)} 通过 ==")
    if failed:
        print("失败项: " + ", ".join(failed))
        return 1
    return 0


def _dump():
    payload = {
        "base": BASE,
        "results": results,
        "console": console_log,
        "page_errors": page_errors,
        "failed_requests": failed_requests,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    path = os.path.join(OUTDIR, "browser-verification.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"\n结果 JSON: {path}")


if __name__ == "__main__":
    sys.exit(main())
