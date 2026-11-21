# -*- coding: utf-8 -*-
"""移动端完整操作序列取证（需求 C）。

序列（严格按用户实际操作路径）：
  ① 初始态（媒体库）
  ② 打开「生成类型选择」抽屉
  ③ 进入某个生成类型（视频 —— 动态渲染项）
  ④ 返回「生成类型选择」（抽屉重新打开）
  ⑤ 切换另一个类型（图片 —— 静态项）
  ⑥ 打开「生成媒体库」并浏览卡片
  ⑦ 打开「收藏」栏
  ⑧ 打开卡片详情模态框
  ⑨ 收藏一张卡（★ 出现）
  ⑩ 收藏视图里能看到它

每一步都截图 + 记录关键事实（抽屉/面板状态、命中测试结果）。

运行：
    python3 scripts/capture_mobile_journey.py --base http://127.0.0.1:8917 \
        --out evidence/mobile-nav/journey-after --width 375 --height 812
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright  # noqa: E402

STATE_JS = r"""
() => {
  const hit = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return {ok: false, why: 'no-element'};
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return {ok: false, why: 'zero-size'};
    const x = Math.round(r.left + r.width / 2);
    const y = Math.round(r.top + r.height / 2);
    if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) {
      return {ok: false, why: 'off-viewport'};
    }
    const t = document.elementFromPoint(x, y);
    return {ok: t === el || el.contains(t), why: 'checked',
            hitId: t ? t.id : null};
  };
  const sb = document.querySelector('#sidebar');
  const pn = document.querySelector('#panel');
  const fab = document.querySelector('#panelFab');
  const vis = (el) => {
    if (!el) return false;
    if (el.hidden) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const activeTab = document.querySelector('.jobs-tabs button.active');
  const activeNav = document.querySelector('#sidebar .nav-item.active, #sidebar .nav-model-item.active');
  return {
    drawerOpen: sb ? sb.classList.contains('open') : null,
    panelOpen: pn ? pn.classList.contains('open') : null,
    fabVisible: vis(fab),
    toggleVisible: vis(document.querySelector('#navToggle')),
    drawerCloseVisible: vis(document.querySelector('#navDrawerClose')),
    cardCount: document.querySelectorAll('#jobsGrid .job-card[data-jid]').length,
    favBadgeCount: document.querySelectorAll('#jobsGrid .job-card[data-jid] .job-fav-badge').length,
    activeTab: activeTab ? (activeTab.dataset.st || '') : null,
    activeNav: activeNav ? (activeNav.textContent || '').trim().slice(0, 22) : null,
    detailOpen: (() => { const m = document.querySelector('#detailModal');
                          return m ? !m.hidden : false; })(),
    hamburger: hit('#navToggle'),
    firstCard: hit('#jobsGrid .job-card[data-jid]'),
    favButton: hit('#jobsGrid .job-card[data-jid] [data-act="favorite-toggle"]'),
  };
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8917")
    ap.add_argument("--out", default=str(REPO / "evidence" / "mobile-nav" / "journey-after"))
    ap.add_argument("--width", type=int, default=375)
    ap.add_argument("--height", type=int, default=812)
    ap.add_argument("--prefix", default="after")
    args = ap.parse_args()
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    base = args.base.rstrip("/")
    steps = []
    n = [0]

    def snap(page, name, note):
        n[0] += 1
        fname = "%s-%02d-%s" % (args.prefix, n[0], name)
        page.screenshot(path=str(outdir / (fname + ".png")))
        st = page.evaluate(STATE_JS)
        steps.append({"step": n[0], "name": name, "note": note,
                      "shot": fname + ".png", "state": st})
        print("[%02d] %-26s %s" % (n[0], name,
                                   "drawer=%s panel=%s cards=%s tab=%s nav=%s"
                                   % (st["drawerOpen"], st["panelOpen"],
                                      st["cardCount"], st["activeTab"], st["activeNav"])))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": args.width, "height": args.height},
                                  is_mobile=True, has_touch=True, device_scale_factor=2)
        page = ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        page.goto(base + "/", wait_until="domcontentloaded")
        page.wait_for_function(
            """() => {
                 const cards = Array.from(
                   document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
                 return cards.some(c => c.querySelector(
                   '.job-preview img, .job-preview video'));
               }""",
            timeout=30000)
        page.wait_for_timeout(1500)

        # ① 初始态
        snap(page, "initial", "初始态：生成媒体库可见")

        # ② 打开「生成类型选择」
        try:
            page.click("#navToggle", timeout=6000)
            page.wait_for_timeout(700)
            snap(page, "type-picker-open", "打开生成类型选择抽屉（新增标题行 + × 按钮）")
        except Exception as e:
            # 【取证脚本要能跑完「修前」的现场】
            # 修前的死路会让某一步直接点不动 —— 那是**证据**，
            # 不能让脚本异常退出（那样就没有后续截图了）。
            snap(page, "type-picker-open-FAILED", "点汉堡失败：%s" % str(e)[:120])

        # ③ 进入某个生成类型（动态渲染的视频项）
        page.evaluate("""() => {
          const el = document.querySelector('#sidebar .nav-model-item[data-mode="t2v"]');
          if (el) el.click();
        }""")
        page.wait_for_timeout(1200)
        snap(page, "after-dynamic-type", "进入「文生视频」（动态项）—— 抽屉应自动收起")

        # ④ 返回「生成类型选择」
        try:
            page.click("#navToggle", timeout=6000)
            page.wait_for_timeout(700)
            snap(page, "back-to-type-picker", "返回生成类型选择（抽屉可再次打开）")
        except Exception as e:
            snap(page, "back-to-type-picker-FAILED",
                 "返回类型选择失败：%s" % str(e)[:120])

        # ⑤ 切换另一个类型（静态项：Z-IMAGE 文生图）
        # 【为什么要先滚进视口】抽屉里的「文生图」分组在视频分组下方，
        # 375×812 下它在视口外 —— Playwright 的 click 会因为
        # "element is outside of the viewport" 而超时。
        # 这不是缺陷（用户可以滚），但取证脚本要模拟真实操作。
        page.evaluate("""(i) => {
          const el = document.querySelector('#sidebar .nav-item[data-mode="t2i"]');
          if (!el) return;
          const s = document.querySelector('#navScroll');
          const r = el.getBoundingClientRect();
          const sr = s.getBoundingClientRect();
          s.scrollTop += (r.top - sr.top) - sr.height / 3;
        }""")
        page.wait_for_timeout(500)
        try:
            page.click('#sidebar .nav-item[data-mode="t2i"]', timeout=6000)
            page.wait_for_timeout(1200)
            snap(page, "after-static-type", "切换到「Z-IMAGE 文生图」（静态项）")
        except Exception as e:
            snap(page, "after-static-type-FAILED",
                 "切换类型失败：%s" % str(e)[:120])

        # ⑥ 打开生成媒体库（滚到卡片）
        page.evaluate("""() => {
          const area = document.querySelector('#jobsArea');
          area.scrollTop = 0;
        }""")
        page.wait_for_timeout(700)
        snap(page, "media-library", "生成媒体库：卡片可见")

        # ⑦ 打开「收藏」栏
        try:
            page.click("#navToggle", timeout=6000)
            page.wait_for_timeout(600)
            page.evaluate("""() => {
              const s = document.querySelector('#navScroll');
              s.scrollTop = s.scrollHeight;
            }""")
            page.wait_for_timeout(600)
            snap(page, "drawer-scrolled-to-favorites", "抽屉滚到底，「收藏」入口可见")
            page.click("#navFavorites", timeout=6000)
            page.wait_for_timeout(1800)
            snap(page, "favorites-view", "进入「收藏」视图")
        except Exception as e:
            snap(page, "favorites-view-FAILED", "进收藏视图失败：%s" % str(e)[:120])

        # ⑧ 回到「全部」标签并打开卡片详情
        page.evaluate("""() => {
          const b = document.querySelector(".jobs-tabs button[data-st='all']");
          if (b) b.click();
        }""")
        page.wait_for_timeout(1800)
        idx = page.evaluate("""() => {
          const cards = Array.from(
            document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
          for (let i = 0; i < cards.length; i++) {
            if (cards[i].querySelector('[data-act="detail"]')) return i;
          }
          return -1;
        }""")
        if idx >= 0:
            page.evaluate("""(i) => {
              const cards = Array.from(
                document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
              const b = cards[i].querySelector('[data-act="detail"]');
              if (b) b.scrollIntoView({block: 'center'});
            }""", idx)
            page.wait_for_timeout(600)
            page.evaluate("""(i) => {
              const cards = Array.from(
                document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
              const b = cards[i].querySelector('[data-act="detail"]');
              if (b) b.click();
            }""", idx)
            page.wait_for_timeout(1200)
        snap(page, "card-detail", "打开卡片详情模态框")
        # 关掉详情
        page.evaluate("""() => {
          const b = document.querySelector('#detailModalClose');
          if (b) b.click();
        }""")
        page.wait_for_timeout(700)

        # ⑨ 收藏一张卡
        try:
            idx = page.evaluate("""() => {
              const cards = Array.from(
                document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
              for (let i = 0; i < cards.length; i++) {
                if (cards[i].querySelector('[data-act="favorite-toggle"]')) return i;
              }
              return -1;
            }""")
            if idx >= 0:
                page.evaluate("""(i) => {
                  const cards = Array.from(
                    document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
                  const b = cards[i].querySelector('[data-act="favorite-toggle"]');
                  if (!b) return;
                  const area = document.querySelector('#jobsArea');
                  const br = b.getBoundingClientRect();
                  const ar = area.getBoundingClientRect();
                  area.scrollTop += (br.top - ar.top) - ar.height / 3;
                }""", idx)
                page.wait_for_timeout(700)
                jid = page.evaluate(
                    "(i) => document.querySelectorAll('#jobsGrid .job-card[data-jid]')[i].dataset.jid",
                    idx)
                btn = page.query_selector(
                    '#jobsGrid .job-card[data-jid="%s"] [data-act="favorite-toggle"]' % jid)
                if btn:
                    btn.click(timeout=6000)
                page.wait_for_timeout(2500)
                snap(page, "favorited-card", "点「☆ 收藏」→ ★ 出现")
            else:
                snap(page, "favorited-card-SKIPPED", "没有带收藏按钮的卡（修前：卡片上根本没有这个按钮）")
        except Exception as e:
            snap(page, "favorited-card-FAILED", "收藏失败：%s" % str(e)[:120])

        # ⑩ 收藏视图里能看到它
        try:
            page.click("#navToggle", timeout=6000)
            page.wait_for_timeout(600)
            page.evaluate("""() => {
              const s = document.querySelector('#navScroll');
              s.scrollTop = s.scrollHeight;
            }""")
            page.wait_for_timeout(500)
            page.click("#navFavorites", timeout=6000)
            page.wait_for_timeout(1800)
            snap(page, "favorites-view-with-item", "收藏视图里能看到刚收藏的那张")
        except Exception as e:
            snap(page, "favorites-view-with-item-FAILED",
                 "回收藏视图失败：%s" % str(e)[:120])

        browser.close()

    report = {"base": base, "viewport": [args.width, args.height],
              "errors": errs[:10], "steps": steps}
    (outdir / ("journey-%s.json" % args.prefix)).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nOUT:", outdir)
    print("errors:", errs[:5])


if __name__ == "__main__":
    main()
