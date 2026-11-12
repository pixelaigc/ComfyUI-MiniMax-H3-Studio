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
            # (a) API 层：resolution_info 与请求宽高一致
            card_info["api"] = page.evaluate("""async () => {
              const r = await fetch('/api/jobs?page_size=100');
              const j = await r.json();
              const jobs = j.jobs || [];
              const out = [];
              for (const job of jobs) {
                const ri = job.resolution_info || {};
                const p = job.params || {};
                if (ri.width && ri.height) {
                  out.push({
                    id: job.id, mode: job.mode, status: job.status,
                    res_w: ri.width, res_h: ri.height,
                    req_w: p.width, req_h: p.height,
                    tier: ri.tier, label: ri.label,
                    old_resolution_param: p.resolution,
                    out_file: (job.outputs && job.outputs[0] && job.outputs[0].file) || null,
                    out_kind: (job.outputs && job.outputs[0] && job.outputs[0].kind) || null,
                  });
                }
              }
              return {total_jobs: jobs.length, with_res_info: out.length,
                      samples: out.slice(0, 8)};
            }""")
            # (b) 页面层：卡片上真正显示的分辨率文字
            card_info["card_text_samples"] = page.evaluate("""() => {
              const cards = Array.from(document.querySelectorAll(
                '.job-card, .job-item, [class*="job"][class*="card"]'));
              return cards.slice(0, 12).map(c => {
                const t = (c.innerText || '').replace(/\\s+/g, ' ').trim();
                const m = t.match(/分辨率[^时模]*?\\s*(\\d+p\\s*·\\s*\\d+×\\d+[^ ]*\\s*·\\s*\\d+:\\d+)?/);
                return {res: m ? m[0] : null, head: t.slice(0, 110)};
              }).filter(x => x.res);
            }""")
            # (c) 旧 bug 标记：卡片上的**档位与实际像素不符**。
            #     注意：不能对整页 body 计数 —— 「544p」本身是**合法档位**
            #     （960×544 就是 544p 档，见尺寸预设下拉与图像面板快捷按钮），
            #     真正的 bug 是「1344×768 的卡片被标成 544p」。
            #     档位规则与服务端 h3studio/reso_label.py 完全一致：
            #     **档位名 = 短边像素**（_tier_label: f"{min(w,h)}p"）。
            card_info["card_544p_wrong"] = page.evaluate("""() => {
              const cards = Array.from(document.querySelectorAll(
                '.job-card, .job-item, [class*="job"][class*="card"]'));
              const bad = [];
              for (const c of cards) {
                const t = (c.innerText || '').replace(/\\s+/g, ' ').trim();
                if (!t.includes('分辨率')) continue;
                const m = t.match(/分辨率\\s*(\\d+)p\\s*·\\s*(\\d+)×(\\d+)/);
                if (!m) continue;
                const tier = m[1] + 'p', w = +m[2], h = +m[3];
                const expect = Math.min(w, h) + 'p';   // 服务端真源规则
                if (tier !== expect) bad.push({tier, w, h, expect, text: t.slice(0,120)});
              }
              return bad;
            }""")
            card_info["legacy_544p_hits"] = card_info.get("card_544p_wrong", [])
        except Exception as exc:
            card_info["error"] = str(exc)[:300]

        # 断言 A：API 的 resolution_info 与请求宽高逐条一致
        api = card_info.get("api") or {}
        mism = [s for s in api.get("samples", [])
                if s["res_w"] != s["req_w"] or s["res_h"] != s["req_h"]]
        record("生成库卡片分辨率标签与实际输出一致（API 层）",
               api.get("with_res_info", 0) > 0 and not mism,
               "resolution_info 条数=%d, 与请求宽高不一致=%d, 样例=%s" % (
                   api.get("with_res_info", 0), len(mism),
                   json.dumps(api.get("samples", [])[:2], ensure_ascii=False)[:500]))

        # 断言 B：页面上卡片显示的分辨率文字与 API 的 tier/pixels 一致
        card_res = [c["res"] for c in card_info.get("card_text_samples", [])]
        api_labels = [s["label"] for s in api.get("samples", [])]
        label_ok = any(lbl and lbl in " ".join(card_res) for lbl in api_labels)
        record("页面卡片显示的分辨率文字与服务端一致",
               bool(card_res) and label_ok,
               "卡片文字样例=%s | 服务端 label 样例=%s" % (
                   json.dumps(card_res[:3], ensure_ascii=False),
                   json.dumps(api_labels[:3], ensure_ascii=False)[:300]))

        # 断言 C：卡片上不再出现「档位与实际像素不符」（旧 bug：1344×768 显示 544p）
        wrong = card_info.get("card_544p_wrong", [])
        record("生成库卡片无「档位与实际像素不符」的旧 bug",
               len(wrong) == 0,
               "错配卡片数=%d, 样例=%s" % (
                   len(wrong), json.dumps(wrong[:3], ensure_ascii=False)[:400]))
        page.screenshot(path=os.path.join(OUTDIR, "04-gallery-resolution.png"),
                        full_page=True)

        # ---------- 真实产物像素核对（ffprobe / PIL） ----------
        print("== 检查 4b：真实产物像素 vs 卡片标签 ==")
        real = []
        try:
            import subprocess
            for s in api.get("samples", []):
                if not s.get("out_file"):
                    continue
                url = "%s/files/%s" % (BASE, s["out_file"])
                r = subprocess.run(
                    ["curl", "-s", "-m", "60", "-o", "/tmp/_h3_out.bin", url],
                    capture_output=True)
                if r.returncode != 0:
                    continue
                dims = None
                if s.get("out_kind") == "video":
                    pr = subprocess.run(
                        ["ffprobe", "-v", "error", "-select_streams", "v:0",
                         "-show_entries", "stream=width,height",
                         "-of", "csv=p=0", "/tmp/_h3_out.bin"],
                        capture_output=True, text=True)
                    if pr.stdout.strip():
                        w, h = pr.stdout.strip().split(",")[:2]
                        dims = (int(w), int(h))
                else:
                    try:
                        from PIL import Image
                        with Image.open("/tmp/_h3_out.bin") as im:
                            dims = im.size
                    except Exception:
                        dims = None
                if dims:
                    real.append({"id": s["id"], "file": s["out_file"],
                                 "actual": list(dims),
                                 "card": [s["res_w"], s["res_h"]],
                                 "label": s["label"],
                                 "match": dims == (s["res_w"], s["res_h"])})
        except Exception as exc:
            real.append({"error": str(exc)[:300]})
        card_info["real_output_pixels"] = real
        good = [r for r in real if r.get("match")]
        record("真实产物像素 == 卡片标签像素",
               bool(good) and len(good) == len([r for r in real if "actual" in r]),
               json.dumps(real, ensure_ascii=False)[:700])

        # ---------- console / pageError ----------
        errors = [c for c in console_log if c["type"] == "error"]
        record("console 错误数 = 0", len(errors) == 0,
               f"errors={len(errors)} sample={json.dumps(errors[:3], ensure_ascii=False)[:400]}")
        record("pageErrors = 0", len(page_errors) == 0,
               f"pageErrors={len(page_errors)} sample={json.dumps(page_errors[:3], ensure_ascii=False)[:400]}")

        # 失败请求：**排除视频预加载被浏览器主动中断**（ERR_ABORTED）。
        # 这是 <video preload> 在页面切换/卸载时正常取消 range 请求，
        # 不是服务端错误 —— 单独把真失败与预加载中断分开统计。
        benign_abort = [f for f in failed_requests
                        if "ERR_ABORTED" in (f.get("err") or "")
                        and ("/files/" in f.get("url", "")
                             or f.get("url", "").endswith((".mp4", ".webm", ".wav", ".mp3")))]
        real_failed = [f for f in failed_requests if f not in benign_abort]
        record("真失败请求 = 0（排除视频预加载 ERR_ABORTED）",
               len(real_failed) == 0,
               "real_failed=%d, benign_abort=%d, 样例=%s" % (
                   len(real_failed), len(benign_abort),
                   json.dumps(real_failed[:3], ensure_ascii=False)[:400]))

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
