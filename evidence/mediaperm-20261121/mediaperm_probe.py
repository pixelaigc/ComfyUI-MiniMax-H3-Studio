#!/usr/bin/env python3
"""生成媒体库「卡片载入慢」实测取证（只读）。

用真实 Chromium 打开线上 8190，采集：
  * 首屏 DOMContentLoaded / load / 卡片出现时间
  * 每个 /api/jobs 请求的 timing（浏览器 network timings）
  * 每个卡片产生的额外请求（img / video / audio / range）
  * 响应头（Cache-Control / ETag / Last-Modified）
  * 各标签（全部 / 进行中 / 已完成 / 生成类型）与翻页的耗时
输出 JSON 到 evidence/ 目录。

用法: python3 mediaperm_probe.py <outdir>
"""
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8190"
OUT = sys.argv[1] if len(sys.argv) > 1 else "."

os.makedirs(OUT, exist_ok=True)

reqs = []


def main():
    t0 = time.time()
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()

        def on_response(resp):
            try:
                r = resp.request
                timing = {}
                try:
                    t = resp.request.timing
                    timing = {
                        "dns": t.get("dnsEnd", 0) - t.get("dnsStart", 0),
                        "connect": t.get("connectEnd", 0) - t.get("connectStart", 0),
                        "send": t.get("sendEnd", 0) - t.get("sendStart", 0),
                        "wait": t.get("receiveHeadersEnd", 0) - t.get("sendEnd", 0),
                        "total": t.get("receiveHeadersEnd", 0) - t.get("requestStart", 0),
                    }
                except Exception:
                    pass
                h = {}
                try:
                    for k in ("cache-control", "etag", "last-modified",
                              "content-length", "content-type", "accept-ranges"):
                        v = resp.headers.get(k)
                        if v:
                            h[k] = v
                except Exception:
                    pass
                reqs.append({
                    "t_rel": round(time.time() - t0, 3),
                    "method": r.method,
                    "url": resp.url,
                    "status": resp.status,
                    "type": r.resource_type,
                    "timing": timing,
                    "headers": h,
                    "from_cache": r.timing.get("requestStart", 0) == 0 if False else None,
                })
            except Exception:
                pass

        page.on("response", on_response)

        marks = {}
        page.goto(BASE + "/", wait_until="domcontentloaded")
        marks["domcontentloaded"] = round(time.time() - t0, 3)
        page.wait_for_selector(".job-card", timeout=30000)
        marks["first_card"] = round(time.time() - t0, 3)
        # 等所有图片请求发完
        page.wait_for_load_state("networkidle", timeout=30000)
        marks["networkidle"] = round(time.time() - t0, 3)

        n_cards = page.eval_on_selector_all(".job-card", "els => els.length")
        marks["card_count"] = n_cards

        # 每张卡里的媒体元素
        media = page.evaluate("""() => {
          const cards = [...document.querySelectorAll('.job-card')];
          const per = cards.map((c, i) => ({
            idx: i,
            imgs: c.querySelectorAll('img').length,
            videos: c.querySelectorAll('video').length,
            audios: c.querySelectorAll('audio').length,
            srcs: [...c.querySelectorAll('img,video,audio')].map(e => e.getAttribute('src')),
          }));
          const tot = {
            cards: cards.length,
            imgs: per.reduce((a,b)=>a+b.imgs,0),
            videos: per.reduce((a,b)=>a+b.videos,0),
            audios: per.reduce((a,b)=>a+b.audios,0),
          };
          return {total: tot, per};
        }""")

        # 切标签
        tab_results = {}
        for st in ("active", "done", "all"):
            t = time.time()
            try:
                page.click(f'.jobs-tabs button[data-st="{st}"]', timeout=5000)
                page.wait_for_timeout(200)
                page.wait_for_load_state("networkidle", timeout=30000)
            except Exception as e:
                tab_results[st] = {"error": str(e)[:200]}
                continue
            tab_results[st] = {
                "elapsed": round(time.time() - t, 3),
                "cards": page.eval_on_selector_all(".job-card", "els => els.length"),
            }

        # 翻页
        page_results = []
        for pno in (1, 2, 3):
            t = time.time()
            try:
                page.evaluate("""(p) => {
                  const inp = document.querySelector('#donePager input, #jobsPagination input[type=number]');
                  if (inp) { inp.value = p; inp.dispatchEvent(new Event('change', {bubbles:true})); }
                }""", pno)
                page.wait_for_timeout(300)
                page.wait_for_load_state("networkidle", timeout=30000)
            except Exception as e:
                page_results.append({"page": pno, "error": str(e)[:200]})
                continue
            page_results.append({
                "page": pno,
                "elapsed": round(time.time() - t, 3),
                "cards": page.eval_on_selector_all(".job-card", "els => els.length"),
            })

        # 生成类型筛选
        gen_type_buttons = page.evaluate(
            """() => [...document.querySelectorAll('#genTypeTabs button')]
                 .map(b => ({key: b.dataset.gt || b.dataset.genType || b.textContent.trim(), html: b.outerHTML.slice(0,200)}))"""
        )

        page.screenshot(path=os.path.join(OUT, "media-library-all.png"), full_page=False)

        browser.close()

    # 汇总
    api = [r for r in reqs if "/api/jobs" in r["url"]]
    files = [r for r in reqs if "/files/" in r["url"]]
    other = [r for r in reqs if "/api/jobs" not in r["url"] and "/files/" not in r["url"]]

    summary = {
        "marks": marks,
        "media_elements": media["total"],
        "api_jobs_requests": len(api),
        "api_jobs_detail": [
            {"url": r["url"], "status": r["status"], "timing": r["timing"], "t_rel": r["t_rel"]}
            for r in api
        ],
        "files_requests": len(files),
        "files_total_ms": round(sum(r["timing"].get("total", 0) for r in files), 1),
        "files_bytes_est": sum(
            int(r["headers"].get("content-length", 0) or 0) for r in files),
        "files_sample_headers": files[0]["headers"] if files else None,
        "other_requests": len(other),
        "other_breakdown": {},
        "tab_results": tab_results,
        "page_results": page_results,
        "gen_type_buttons": gen_type_buttons,
        "all_requests": reqs,
    }
    from collections import Counter
    summary["other_breakdown"] = {
        f"{t} {u}": c for (t, u), c in Counter(
            (r["type"], r["url"].split("?")[0]) for r in other).items()}
    summary["files_breakdown"] = dict(Counter(r["type"] for r in files))

    with open(os.path.join(OUT, "mediaperm-probe.json"), "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in summary.items() if k != "all_requests"},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
