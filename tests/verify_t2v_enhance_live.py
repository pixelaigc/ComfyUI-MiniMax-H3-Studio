# -*- coding: utf-8 -*-
"""
「文生视频」面板增强按钮 —— 真机浏览器验收脚本。

覆盖用户报的缺陷（2026-11-11，8190 线上）：
  面板=文生视频（minimax-h3），点提示词框旁的「✦ 增强」→ 弹窗
  「提示词增强失败（本机 Qwen 多模态）… 返回的是系统提示词复读 …」

验收项：
  1. 切到「文生视频」面板，「当前增强」回显 = 普通增强（视频导演提示词…）
  2. 提示词框填入港风提示词，点「✦ 增强」→ **不弹窗**（无 alert）
  3. 增强结果弹窗出现，#enhanceResult 有**非空且不含系统提示词原文**的内容
  4. 结果内容含 H3 三段式字段名（integrated_multimodal_description 等）
  5. console **零报错**、零失败请求
  6. 截图存证（面板 + 结果弹窗）

用法：
    python3 tests/verify_t2v_enhance_live.py --base http://127.0.0.1:8199/ \
        --out /path/to/evidence
退出码：0 = 全部通过；1 = 有失败项。
"""
import argparse
import json
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except Exception as exc:                                    # pragma: no cover
    print("playwright 缺失：%s" % exc)
    sys.exit(2)

USER_PROMPT = "电影。港风。90年代香港港口，亚洲年轻女性休闲散步"

#: 系统提示词里的**特征串** —— 增强结果里绝不能出现（出现即说明复读被写回）
SYSTEM_PROMPT_MARKERS = [
    "你是一位专业的 MiniMax-H3 视频导演",
    "## 输出结构（必须严格遵守，字段名与顺序都不能改）",
    "## integrated_multimodal_description 的写法",
    "## 硬约束",
]

#: H3 三段式字段名 —— 合法产物必须有
H3_FIELDS = ("integrated_multimodal_description", "overall_soundscape",
             "non_diegetic_music")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8199/")
    ap.add_argument("--out", default="/tmp/h3t2v-enh-verify")
    ap.add_argument("--viewport", default="1280x900")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    width, height = (int(x) for x in args.viewport.lower().split("x"))

    console_errors = []
    failed_requests = []
    alerts = []
    result = {"base": args.base, "checks": [], "alerts": alerts,
              "console_errors": console_errors,
              "failed_requests": failed_requests}

    def check(name, ok, detail=""):
        result["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)})
        print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  -- " + str(detail)) if detail else ""))
        return ok

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": width, "height": height})
        page.on("console", lambda m: console_errors.append(
            "%s: %s" % (m.type, m.text)) if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: console_errors.append("pageerror: %s" % e))
        page.on("requestfailed", lambda r: failed_requests.append(
            "%s %s -- %s" % (r.method, r.url, r.failure)))
        page.on("dialog", lambda d: (alerts.append(d.message), d.dismiss()))
        # console 里的 "Failed to load resource ... 404" **不带 URL**，
        # 所以要单独记录 404 响应，才能区分「既有 404」与「本次引入的报错」。
        http_404s = []
        page.on("response", lambda r: http_404s.append(
            "%s %s" % (r.status, r.url)) if r.status == 404 else None)

        # 用 domcontentloaded —— 页面有定时轮询，networkidle 永远不 settle
        page.goto(args.base, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

        # ---- 切到「文生视频」面板 -------------------------------------
        switched = False
        for sel in ("#mode-t2v", "[data-mode='t2v']", "text=文生视频"):
            try:
                el = page.query_selector(sel)
            except Exception:
                el = None
            if el:
                try:
                    el.click()
                    switched = True
                    break
                except Exception:
                    continue
        if not switched:
            # 兜底：直接调前端 API 切模式
            page.evaluate("""() => {
                const sel = document.querySelector('#mode');
                if (sel) { sel.value = 't2v'; sel.dispatchEvent(new Event('change')); }
            }""")
            switched = True
        page.wait_for_timeout(1500)
        check("切换到文生视频面板", switched)

        # ---- 当前增强方式回显 ----------------------------------------
        # 「当前增强：普通增强（视频导演提示词…）」里的**括号部分**来自
        # /api/enhancer 下发的模板清单 —— 页面刚切到 t2v 时清单还没到，
        # 回显会先只有「普通增强」。这里轮询等它稳定（最多 20 秒）。
        cur = ""
        deadline_ind = time.time() + 20
        while time.time() < deadline_ind:
            cur = page.eval_on_selector(
                "#enhanceCurrent", "el => el.textContent.trim()")
            if "视频导演提示词" in (cur or ""):
                break
            page.wait_for_timeout(800)
        check("当前增强回显含「普通增强」", "普通增强" in (cur or ""), cur)
        check("当前增强回显含视频导演提示词", "视频导演提示词" in (cur or ""), cur)

        # ---- 填提示词 -------------------------------------------------
        filled = page.evaluate("""(text) => {
            const el = document.querySelector('#prompt');
            if (!el) return false;
            el.value = text;
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            return true;
        }""", USER_PROMPT)
        check("填入提示词", filled)
        page.screenshot(path=str(out / "01-t2v-panel-before.png"))

        # ---- 点「✦ 增强」 ---------------------------------------------
        clicked = page.evaluate("""() => {
            const btn = document.querySelector('#enhanceBtn');
            if (!btn) return false;
            btn.scrollIntoView({block: 'center'});
            btn.click();
            return true;
        }""")
        check("点击增强按钮", clicked)

        # 等结果弹窗（成功时弹窗出现；失败时 alert 出现）
        deadline = time.time() + 420
        modal_open = False
        while time.time() < deadline:
            try:
                modal_open = page.evaluate(
                    """() => { const m = document.querySelector('#enhanceModal');
                        return !!(m && !m.hidden); }""")
            except Exception:
                modal_open = False
            if modal_open or alerts:
                break
            page.wait_for_timeout(1000)

        check("无 alert 弹窗（增强未失败）", not alerts, alerts)
        check("增强结果弹窗出现", modal_open)

        enhanced = ""
        if modal_open:
            enhanced = page.eval_on_selector(
                "#enhanceResult", "el => el.value || el.textContent || ''")
            meta = page.eval_on_selector(
                "#enhanceMeta", "el => el.textContent.trim()")
            page.screenshot(path=str(out / "02-t2v-enhance-result.png"))
            check("结果非空", bool(enhanced.strip()), "len=%d" % len(enhanced))
            check("结果含 H3 三段式字段",
                  all(f in enhanced for f in H3_FIELDS),
                  [f for f in H3_FIELDS if f not in enhanced])
            leaked = [m for m in SYSTEM_PROMPT_MARKERS if m in enhanced]
            check("结果不含系统提示词原文", not leaked, leaked)
            check("元信息显示 Provider", "本机" in (meta or "") or "Provider" in (meta or ""),
                  meta)
            result["enhanced"] = enhanced
            result["meta"] = meta
        else:
            page.screenshot(path=str(out / "02-t2v-enhance-FAILED.png"))

        # ---- console / 请求 -------------------------------------------
        # 注意：页面里 **/api/assets/ /api/jobs/ /api/providers/** 三个
        # 尾斜杠变体在 8190 线上**也是 404**（既有缺陷，与本次修复无关）。
        # 这里把它们单独归类，不计入「增强链路 console 报错」，
        # 否则验收分不清「本次引入的问题」与「历史遗留」。
        KNOWN_PREEXISTING_404 = ("/api/assets/", "/api/jobs/", "/api/providers/")
        preexisting = [u for u in http_404s
                       if any(k in u for k in KNOWN_PREEXISTING_404)]
        other_404 = [u for u in http_404s if u not in preexisting]
        real_console_errors = []
        for c in console_errors:
            if "favicon" in c.lower():
                continue
            # console 的 404 文本不带 URL —— 用数量对齐来区分：
            # 若 404 响应里除既有三项外没有别的，就认为这些 console 404
            # 都是既有项的产物。
            if "404" in c and not other_404:
                continue
            real_console_errors.append(c)
        check("增强链路 console 零报错", not real_console_errors,
              real_console_errors[:5])
        check("无失败请求", not failed_requests, failed_requests[:5])
        check("无新增 404（除既有 3 项）", not other_404, other_404[:5])
        result["preexisting_404_console"] = preexisting

        result["ok"] = all(c["ok"] for c in result["checks"])
        browser.close()

    (out / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n%s" % ("ALL PASS" if result.get("ok") else "HAS FAILURES"))
    print("证据目录：%s" % out)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
