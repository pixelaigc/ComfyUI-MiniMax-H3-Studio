# -*- coding: utf-8 -*-
"""【验收取证 · selflift 卡片无「接续」按钮 + 「接续」配置区块】

证明两件事（都是**真实浏览器**实测，不是代码断言）：

  A. 二采（SelfLift）产物的生成库卡片**不出现**「接续」按钮，
     而普通文生视频产物**出现** —— 两者同屏对照。

  B. 「接续上下文」配置区块：
     * 区块**只由生成库**点「接续」添加 —— 面板里**没有**任何
       「新增/添加」入口，只有一个「清除」；
     * 点接续后内容被**自动回填**（模型/尺寸/时长/步数/种子…），
       而**提示词留空**给用户写下一段。

用法：
    python3 tests/verify_selflift_no_continue.py \
        --base http://127.0.0.1:8206 --out evidence/continuation-selflift

产物：
    <out>/selflift-375-gallery.png       375px 生成库（同屏：selflift 卡 + 普通卡）
    <out>/selflift-1280-gallery.png      1280px 生成库
    <out>/selflift-375-card-zoom.png     375px selflift 卡片放大（按钮区）
    <out>/selflift-1280-card-zoom.png    1280px selflift 卡片放大
    <out>/normal-375-card-zoom.png       375px 普通卡放大（有「接续」）
    <out>/normal-1280-card-zoom.png      1280px 普通卡放大（有「接续」）
    <out>/continuation-panel-375.png     375px 接续上下文区（已回填）
    <out>/continuation-panel-1280.png    1280px 接续上下文区（已回填）
    <out>/report.json                    机器可读报告
"""

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from playwright.sync_api import sync_playwright
except Exception:
    print("需要 playwright")
    raise

WIDTHS = (375, 1280)


def accept_base(default="http://127.0.0.1:8199"):
    """验收实例的 base URL。

    端口写在 data-accept/PORT（启动脚本自动顺延时写的）——
    8199 可能被并发 agent 占用，所以不能硬编码。
    """
    port_file = Path(__file__).resolve().parent.parent / "data-accept" / "PORT"
    if port_file.is_file():
        try:
            port = port_file.read_text(encoding="utf-8").strip()
            if port.isdigit():
                return f"http://127.0.0.1:{port}"
        except Exception:
            pass
    return default


def _get(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw else {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=accept_base(),
                    help="验收实例（scripts/run_live_8199_d1.sh，端口见 data-accept/PORT）")
    ap.add_argument("--out", default=str(REPO / "evidence" / "continuation-selflift"))
    ap.add_argument("--selflift-job", default="")
    ap.add_argument("--normal-job", default="")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    report = {"base": base, "steps": [], "console_errors": [],
              "page_errors": [], "failed_requests": [],
              # 卡片 <video preload="metadata"> 被 Chrome 读完元数据后主动中止
              # 的媒体流 —— 浏览器正常行为，非服务端错误（另列，不计为失败）
              "media_preload_aborts": [],
              "shots": []}

    def log(step, **kw):
        report["steps"].append({"step": step, **kw})
        print(f"[acc] {step}: {json.dumps(kw, ensure_ascii=False)}", flush=True)

    def shot(page, name):
        p = out / name
        page.screenshot(path=str(p), full_page=False)
        report["shots"].append(str(p))
        return p

    # ------------------------------------------------------------------
    # 先确认服务端判定（权威）
    # ------------------------------------------------------------------
    sl_id = args.selflift_job
    nm_id = args.normal_job
    if not sl_id or not nm_id:
        jobs = _get(f"{base}/api/jobs?status=all&page=1&page_size=50").get("jobs", [])
        for j in jobs:
            info = j.get("continuation_info") or {}
            p = j.get("params") or {}
            tw = p.get("twopass") or {}
            if tw.get("enabled") and not sl_id:
                sl_id = j["id"]
            elif info.get("can_continue") and not nm_id:
                nm_id = j["id"]
    sl = _get(f"{base}/api/jobs/{sl_id}") if sl_id else {}
    nm = _get(f"{base}/api/jobs/{nm_id}") if nm_id else {}
    log("server_verdict",
        selflift_job=sl_id,
        selflift_can_continue=(sl.get("continuation_info") or {}).get("can_continue"),
        selflift_reason=(sl.get("continuation_info") or {}).get("reason"),
        normal_job=nm_id,
        normal_can_continue=(nm.get("continuation_info") or {}).get("can_continue"))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        try:
            for width in WIDTHS:
                ctx = browser.new_context(viewport={"width": width, "height": 900})
                page = ctx.new_page()
                errors, perrs, failed = [], [], []
                page.on("pageerror", lambda e, s=perrs: s.append(str(e)))
                page.on("console", lambda m, s=errors:
                        s.append(f"{m.type}: {m.text}") if m.type == "error" else None)
                page.on("requestfailed", lambda r, s=failed:
                        s.append(f"{r.url} {r.failure}"))
                page.goto(base, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)

                # 只看「已完成」
                page.evaluate("""() => {
                  const b = document.querySelector('[data-st="done"]');
                  if (b) b.click();
                }""")
                page.wait_for_timeout(1200)
                try:
                    page.wait_for_function(
                        "() => document.querySelectorAll("
                        "'#jobsGrid .job-card:not(.job-skeleton)').length > 0",
                        timeout=30000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)

                # ---- 两张卡分别的按钮清单（服务端权威 + DOM 实际） ----
                cards = page.evaluate("""(ids) => {
                  const out = {};
                  for (const [tag, jid] of Object.entries(ids)) {
                    if (!jid) continue;
                    const c = document.querySelector(
                      `.job-card[data-jid="${jid}"]`);
                    if (!c) { out[tag] = null; continue; }
                    const btns = Array.from(c.querySelectorAll('button[data-act]'))
                      .map(b => b.dataset.act);
                    out[tag] = {
                      jid,
                      has_continue: !!c.querySelector('[data-act="continue"]'),
                      buttons: btns,
                    };
                  }
                  return out;
                }""", {"selflift": sl_id, "normal": nm_id})
                log(f"cards_{width}", **cards)

                # 生成库整屏（两卡同屏）
                shot(page, f"selflift-{width}-gallery.png")

                # ---- selflift 卡片放大（按钮区） ----
                if sl_id:
                    try:
                        page.locator(f'.job-card[data-jid="{sl_id}"]').screenshot(
                            path=str(out / f"selflift-{width}-card-zoom.png"))
                        report["shots"].append(
                            str(out / f"selflift-{width}-card-zoom.png"))
                    except Exception as exc:
                        log(f"selflift_zoom_failed_{width}", error=str(exc))
                # ---- 普通卡放大 ----
                if nm_id:
                    try:
                        page.locator(f'.job-card[data-jid="{nm_id}"]').screenshot(
                            path=str(out / f"normal-{width}-card-zoom.png"))
                        report["shots"].append(
                            str(out / f"normal-{width}-card-zoom.png"))
                    except Exception as exc:
                        log(f"normal_zoom_failed_{width}", error=str(exc))

                # ---- 点普通卡的「接续」-> 接续上下文区 ----
                if nm_id:
                    try:
                        page.click(f'.job-card[data-jid="{nm_id}"] '
                                   f'[data-act="continue"]')
                        page.wait_for_timeout(2000)
                        # 窄屏：配置区是抽屉，要先打开
                        if width < 768:
                            page.evaluate("""() => {
                              const fab = document.getElementById('panelFab');
                              if (fab) fab.click();
                            }""")
                            page.wait_for_timeout(1200)
                        # 滚到接续上下文区
                        page.evaluate("""() => {
                          const ctx = document.getElementById('continuationPanel');
                          const panel = document.getElementById('panel');
                          if (!ctx || !panel) return;
                          const c = ctx.getBoundingClientRect();
                          const p = panel.getBoundingClientRect();
                          panel.scrollTop += (c.top - p.top - 12);
                        }""")
                        page.wait_for_timeout(700)
                        panel_shot = out / f"continuation-panel-{width}.png"
                        try:
                            page.locator("#continuationPanel").screenshot(
                                path=str(panel_shot))
                        except Exception:
                            el = page.query_selector("#panel")
                            if el:
                                el.screenshot(path=str(panel_shot))
                        if panel_shot.is_file():
                            report["shots"].append(str(panel_shot))

                        # 区块内容 + 有没有「新增」入口
                        panel = page.evaluate("""() => {
                          const ctx = document.getElementById('continuationPanel');
                          if (!ctx) return null;
                          const btns = Array.from(ctx.querySelectorAll('button'))
                            .map(b => (b.textContent || '').trim())
                            .filter(Boolean);
                          return {
                            hidden: ctx.hidden,
                            text: (ctx.innerText || '').trim(),
                            buttons: btns,
                            has_add: btns.some(b =>
                              /新增|添加/.test(b)),
                          };
                        }""")
                        log(f"continuation_panel_{width}", **panel)
                    except Exception as exc:
                        log(f"continue_click_failed_{width}", error=str(exc))

                report["console_errors"].extend(errors)
                report["page_errors"].extend(perrs)
                # 【关于 failed_requests】卡片里的 <video preload="metadata">
                # 让 Chrome 读完元数据后**主动中止**媒体流（net::ERR_ABORTED）。
                # 这是浏览器的正常行为，不是服务端错误 —— 实测同一 URL
                # 手动 fetch 返回 200 且字节完整。下面把媒体类与其它类分开记。
                media_aborts = [f for f in failed
                                if "/files/" in f and "ERR_ABORTED" in f]
                real_failures = [f for f in failed if f not in media_aborts]
                report["failed_requests"].extend(real_failures)
                report["media_preload_aborts"] = report.get(
                    "media_preload_aborts", []) + media_aborts
                log(f"request_failures_{width}",
                    media_preload_aborts=len(media_aborts),
                    real_failures=len(real_failures),
                    real_failure_list=real_failures)
                ctx.close()
        finally:
            browser.close()

    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[acc] 报告: {out / 'report.json'}")
    print(f"[acc] console errors: {len(report['console_errors'])}")
    print(f"[acc] page errors   : {len(report['page_errors'])}")
    print(f"[acc] failed reqs   : {len(report['failed_requests'])}")
    for e in report["console_errors"][:10]:
        print("   ✗", e)
    for e in report["page_errors"][:10]:
        print("   ! pageerror", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
