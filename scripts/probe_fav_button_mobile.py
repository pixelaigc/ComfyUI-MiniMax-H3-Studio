# -*- coding: utf-8 -*-
"""移动端「收藏」可达性：长按右键菜单 vs 卡片底部收藏按钮。

【为什么两条路径都要测】
  · 长按右键菜单：图片卡可行，视频卡不可行（<video> 吞 touch 事件）
  · 卡片底部「☆ 收藏」按钮：纯 DOM 按钮，两种卡都可行
  ⇒ 移动端用户至少有**一条**可靠路径。

判据：按钮真实可点（Playwright 命中测试）+ 收藏真的落库
（★ 角标出现 + /api/jobs?favorites=1 能查到）。

运行：
    python3 scripts/probe_fav_button_mobile.py --base http://127.0.0.1:8917 \
        --out evidence/mobile-nav/favbtn-after --width 375 --height 812
"""
import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8917")
    ap.add_argument("--out", default=str(REPO / "evidence" / "mobile-nav" / "favbtn-after"))
    ap.add_argument("--width", type=int, default=375)
    ap.add_argument("--height", type=int, default=812)
    args = ap.parse_args()
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    base = args.base.rstrip("/")
    r = {"base": base, "viewport": [args.width, args.height]}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": args.width, "height": args.height},
                                  is_mobile=True, has_touch=True, device_scale_factor=2)
        page = ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(base + "/", wait_until="domcontentloaded")
        page.wait_for_function(
            """() => {
                 const cards = Array.from(
                   document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
                 return cards.some(c => c.querySelector(
                   '.job-preview img, .job-preview video'));
               }""",
            timeout=30000)
        page.wait_for_timeout(800)

        # 找一张「已完成」且有媒体的卡
        idx = page.evaluate("""() => {
          const cards = Array.from(
            document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
          for (let i = 0; i < cards.length; i++) {
            const c = cards[i];
            const media = c.querySelector('.job-preview img, .job-preview video');
            const btn = c.querySelector('[data-act="favorite-toggle"]');
            if (media && btn) return i;
          }
          return -1;
        }""")
        r["card_index"] = idx
        if idx < 0:
            r["error"] = "找不到带收藏按钮的媒体卡"
            browser.close()
            print(json.dumps(r, ensure_ascii=False, indent=2))
            return

        jid = page.evaluate("(i) => document.querySelectorAll('#jobsGrid .job-card[data-jid]')[i].dataset.jid", idx)
        r["job_id"] = jid
        r["media_tag"] = page.evaluate("""(i) => {
          const c = document.querySelectorAll('#jobsGrid .job-card[data-jid]')[i];
          const m = c.querySelector('.job-preview img, .job-preview video');
          return m ? m.tagName : null;
        }""", idx)

        # 把卡的底部滚进视口
        page.evaluate("""(i) => {
          const c = document.querySelectorAll('#jobsGrid .job-card[data-jid]')[i];
          const area = document.querySelector('#jobsArea');
          const cr = c.getBoundingClientRect();
          const ar = area.getBoundingClientRect();
          area.scrollTop += (cr.top - ar.top) - ar.height / 3;
        }""", idx)
        page.wait_for_timeout(600)

        btn_state = page.evaluate("""(i) => {
          const c = document.querySelectorAll('#jobsGrid .job-card[data-jid]')[i];
          const b = c.querySelector('[data-act="favorite-toggle"]');
          if (!b) return {found: false};
          const r = b.getBoundingClientRect();
          const x = Math.round(r.left + r.width/2), y = Math.round(r.top + r.height/2);
          const t = (x>=0&&y>=0&&x<=innerWidth&&y<=innerHeight)
            ? document.elementFromPoint(x, y) : null;
          return {found: true, text: b.textContent.trim(),
                  rect: {top: Math.round(r.top), left: Math.round(r.left),
                         w: Math.round(r.width), h: Math.round(r.height)},
                  inViewport: r.top >= 0 && r.bottom <= innerHeight,
                  hitIsSelf: t ? (t === b || b.contains(t)) : false,
                  hitTag: t ? t.tagName : null};
        }""", idx)
        r["button_before"] = btn_state
        page.screenshot(path=str(outdir / "FB1-before-click.png"))

        # 真实点击（Playwright 命中测试）
        btn = page.query_selector(
            '#jobsGrid .job-card[data-jid="%s"] [data-act="favorite-toggle"]' % jid)
        if btn is None:
            r["error"] = "取不到收藏按钮"
        else:
            try:
                btn.click(timeout=6000)
                r["click_ok"] = True
            except Exception as e:
                r["click_ok"] = False
                r["click_error"] = str(e).split("\n")[0][:200]
            page.wait_for_timeout(2500)

        r["badge_count_after"] = page.evaluate(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid] .job-fav-badge').length")
        r["button_after"] = page.evaluate("""(i) => {
          const c = document.querySelectorAll('#jobsGrid .job-card[data-jid]')[i];
          const b = c.querySelector('[data-act="favorite-toggle"]');
          return b ? {text: b.textContent.trim(), on: b.classList.contains('is-on'),
                      pressed: b.getAttribute('aria-pressed')} : null;
        }""", idx)
        page.screenshot(path=str(outdir / "FB2-after-click.png"))

        # 服务端是否真的落库
        # 【为什么用 /api/jobs/favorites 而不是 /api/jobs?favorites=1】
        # 后者走的是**列表**接口 —— 它会被页面 2 秒轮询的刷新节奏影响，
        # 实测读到的可能是上一轮的数据。前者是收藏专用的实时接口。
        def read_server():
            d = page.request.get(base + "/api/jobs/favorites").json()
            return d.get("total", 0), jid in (d.get("ids") or [])

        def wait_server(want_has, timeout_ms=15000):
            """等服务端状态与期望一致。
            【为什么必须等】收藏是**乐观更新**的：界面先变 ★，
            POST 才在后台跑。实测一次 POST 要 0.7~17 秒
            （浏览器对同一 host 只有 6 条并发连接）。
            不等就读，读到的是**旧值** —— 我第一版探针就是这么
            误判「服务端没落库」的。"""
            deadline = time.time() + timeout_ms / 1000.0
            last = None
            while time.time() < deadline:
                last = read_server()
                if last[1] == want_has:
                    return last, True
                time.sleep(300)
            return last, False
        (r["server_favorites_count"], r["server_has_this_job"]), r["server_settled"] = \
            wait_server(True)

        # 再点一次 —— 应该取消收藏
        # 【为什么要重新取按钮句柄】乐观更新会重渲染卡片，
        # 旧的 ElementHandle 会指向已脱离文档的节点。
        btn2 = page.query_selector(
            '#jobsGrid .job-card[data-jid="%s"] [data-act="favorite-toggle"]' % jid)
        if btn2:
            btn2.click(timeout=6000)
            page.wait_for_timeout(3500)
        r["badge_count_after_toggle_off"] = page.evaluate(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid] .job-fav-badge').length")
        (r["server_favorites_after_toggle_off"], r["server_still_has_this_job"]), \
            r["server_settled_off"] = wait_server(False)
        page.screenshot(path=str(outdir / "FB3-after-toggle-off.png"))

        r["errors"] = errs[:10]
        browser.close()

    (outdir / "favbtn.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print("OUT:", outdir)


if __name__ == "__main__":
    main()
