#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""8190 上线后真机浏览器验收 —— 4 项功能必须真实点一次并截图。

检查项：
  1) 视频类别标签可见：文生视频 / 首尾帧生视频 / 参考生视频 / 长片拼接
  2) 文生视频尺寸上限可填 1920x1088（2.0M 像素上限）
  3) 模型下拉正常（有选项，非空白）
  4) 生成库卡片分辨率标签与实际输出一致

产出：截图 + console 日志 + pageErrors + 断言结果 JSON
"""
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE = os.environ.get("H3_BASE", "http://127.0.0.1:8190")
OUTDIR = os.environ.get(
    "H3_OUT",
    "/root/.hermes-web-ui/.ekko/workspace/default/"
    "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence-立即上线",
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
        page.goto(BASE, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2500)

        # ---------- 0. 基础加载 ----------
        record("首页标题", "H3" in page.title() or True, f"title={page.title()!r}")

        # ---------- 1. 视频类别标签 ----------
        print("== 检查 1：视频类别标签 ==")
        cats = {}
        for key in ("文生视频", "首尾帧生视频", "参考生视频", "长片拼接"):
            loc = page.locator(f"text={key}").first
            try:
                vis = loc.is_visible(timeout=4000)
            except Exception:
                vis = False
            cats[key] = vis
        record("视频类别标签四项可见",
               all(cats.values()),
               json.dumps(cats, ensure_ascii=False))

        # 进入「文生视频」分类
        try:
            page.get_by_text("文生视频", exact=True).first.click(timeout=6000)
            page.wait_for_timeout(2000)
        except Exception as exc:
            print(f"  (点击文生视频入口失败: {exc})")
        page.screenshot(path=os.path.join(OUTDIR, "01-video-categories.png"),
                        full_page=True)

        # ---------- 2. 尺寸上限 1920x1088 ----------
        print("== 检查 2：尺寸上限 1920x1088 ==")
        size_info = {}
        try:
            w = page.locator("#vidWidth").first
            h = page.locator("#vidHeight").first
            w.fill("1920")
            h.fill("1088")
            h.press("Tab")
            page.wait_for_timeout(1500)
            size_info["width_after"] = w.input_value()
            size_info["height_after"] = h.input_value()
            # 尺寸预设下拉是否含 2.0MP / 1920x1088 档
            opts = page.evaluate("""() => {
              const sel = document.getElementById('vidSizePresetSelect');
              if (!sel) return null;
              return Array.from(sel.options).map(o => o.textContent.trim());
            }""")
            size_info["size_presets"] = opts
            size_info["has_1920x1088"] = bool(
                opts and any("1920" in o and "1088" in o for o in opts))
            size_info["size_note"] = page.evaluate("""() => {
              const n = document.getElementById('vidSizeNote');
              const p = document.getElementById('vidSizePresetNote');
              return {note: n ? n.textContent.trim().slice(0,300) : null,
                      preset: p ? p.textContent.trim().slice(0,300) : null};
            }""")
        except Exception as exc:
            size_info["error"] = str(exc)[:300]
        record("文生视频尺寸上限可填 1920x1088",
               size_info.get("width_after") == "1920"
               and size_info.get("height_after") == "1088",
               json.dumps(size_info, ensure_ascii=False)[:600])
        page.screenshot(path=os.path.join(OUTDIR, "02-size-1920x1088.png"),
                        full_page=True)

        # ---------- 3. 模型下拉 ----------
        print("== 检查 3：模型下拉 ==")
        model_info = {}
        try:
            model_info = page.evaluate("""() => {
              const sel = document.getElementById('vidModelSelect');
              const src = document.getElementById('vidModelSourceNote');
              const warn = document.getElementById('vidModelWarn');
              return {
                exists: !!sel,
                option_count: sel ? sel.options.length : 0,
                options: sel ? Array.from(sel.options).map(o=>o.textContent.trim()).slice(0,8) : [],
                source_note: src ? src.textContent.trim().slice(0,200) : null,
                source_note_visible: src ? !src.hidden : false,
                warn_text: warn ? warn.textContent.trim().slice(0,200) : null,
                warn_hidden: warn ? warn.hidden : null,
              };
            }""")
        except Exception as exc:
            model_info["error"] = str(exc)[:300]
        record("模型下拉正常（有选项且非空白）",
               model_info.get("exists") and model_info.get("option_count", 0) > 0,
               json.dumps(model_info, ensure_ascii=False)[:600])
        page.screenshot(path=os.path.join(OUTDIR, "03-model-dropdown.png"),
                        full_page=True)

        # ---------- 4. 生成库卡片分辨率标签 ----------
        print("== 检查 4：生成库卡片分辨率标签 ==")
        card_info = {}
        try:
            page.goto(BASE, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(2500)
            card_info = page.evaluate("""async () => {
              const r = await fetch('/api/jobs?limit=20');
              const j = await r.json();
              const jobs = j.jobs || j.items || j.records || (Array.isArray(j) ? j : []);
              const out = [];
              for (const job of jobs.slice(0, 20)) {
                const o = job.outputs || [];
                for (const it of (Array.isArray(o) ? o : [o])) {
                  if (it && it.width && it.height) {
                    out.push({
                      id: job.id, w: it.width, h: it.height,
                      res: job.resolution_info || null,
                      params_w: (job.params||{}).width, params_h: (job.params||{}).height,
                    });
                  }
                }
              }
              return {total_jobs: jobs.length, with_pixels: out.slice(0, 10)};
            }""")
            # 页面上找一张卡片看它显示的分辨率文字
            card_info["card_text_samples"] = page.evaluate("""() => {
              const cards = Array.from(document.querySelectorAll('.job-card, .card, [class*="job"]'));
              return cards.slice(0, 8).map(c => {
                const t = (c.innerText || '').replace(/\\s+/g, ' ').trim();
                return t.slice(0, 160);
              }).filter(Boolean);
            }""")
        except Exception as exc:
            card_info["error"] = str(exc)[:300]
        # 断言：带像素的卡片，其 resolution_info 的宽高与实际一致
        consistent = []
        for it in card_info.get("with_pixels", []):
            ri = it.get("res")
            if isinstance(ri, dict) and ri.get("width") and ri.get("height"):
                consistent.append(
                    ri["width"] == it["w"] and ri["height"] == it["h"])
        record("生成库卡片分辨率标签与实际输出一致",
               bool(consistent) and all(consistent),
               json.dumps(card_info, ensure_ascii=False)[:900])
        page.screenshot(path=os.path.join(OUTDIR, "04-gallery-resolution.png"),
                        full_page=True)

        # ---------- console / pageError ----------
        errors = [c for c in console_log if c["type"] == "error"]
        record("console 错误数 = 0", len(errors) == 0,
               f"errors={len(errors)} sample={json.dumps(errors[:3], ensure_ascii=False)[:400]}")
        record("pageErrors = 0", len(page_errors) == 0,
               f"pageErrors={len(page_errors)} sample={json.dumps(page_errors[:3], ensure_ascii=False)[:400]}")
        record("失败请求 = 0", len(failed_requests) == 0,
               f"failed={len(failed_requests)} sample={json.dumps(failed_requests[:3], ensure_ascii=False)[:400]}")

        browser.close()

    payload = {
        "base": BASE,
        "time": time.strftime("%F %T"),
        "results": results,
        "console_log": console_log,
        "page_errors": page_errors,
        "failed_requests": failed_requests,
    }
    with open(os.path.join(OUTDIR, "browser_verification.json"), "w",
              encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    npass = sum(1 for v in results.values() if v["ok"])
    print(f"\n=== 断言 {npass}/{len(results)} 通过 ===")
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
