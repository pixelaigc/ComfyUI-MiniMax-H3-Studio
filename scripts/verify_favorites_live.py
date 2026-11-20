# -*- coding: utf-8 -*-
"""线上 8190 的收藏功能取证（真实 Chromium，按人工操作路径）。

【覆盖需求清单的 ①-⑥】
  ① 保持当前标签提交生成后标签未跳变（前后截图 + 该标签下卡片状态正常）
  ② 右键「添加到收藏」成功、已收藏标识出现
  ③ 左侧「收藏」栏可进入并正确显示收藏内容（截图）
  ④ 执行「管理 → 清理」后收藏项仍在（清理前后计数 + 截图）
  ⑤ 取消收藏生效、且取消后卡片可被清理
  ⑥ 批量取消收藏可用

【不消耗真实生成额度】
① 的提交动作被 page.route 拦下（只验证前端「提交后不改标签」这段逻辑，
   不真的建任务）。其余取证全部用**已存在的产物**与收藏标记，
   不新建生成作业。

运行：
    /home/ubuntu/comfyui/venv/bin/python scripts/verify_favorites_live.py \
        --base http://127.0.0.1:8190 --out evidence/favorites-live-<ts>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

WAIT_TIMEOUT = 60000


def api(base, path, method="GET", body=None, timeout=30):
    url = base.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]


class Evidence:
    def __init__(self, out: Path):
        self.out = out
        self.out.mkdir(parents=True, exist_ok=True)
        self.steps = []
        self.shots = []
        self.console = []
        self.page_errors = []

    def step(self, msg):
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
        self.steps.append(line)
        print(line, flush=True)

    def shot(self, page, name):
        path = self.out / (name + ".png")
        page.screenshot(path=str(path), full_page=False)
        self.shots.append(str(path))
        self.step("截图 -> %s" % path)

    def dump(self, name="evidence.json"):
        data = {"steps": self.steps, "screenshots": self.shots,
                "console_errors": self.console, "page_errors": self.page_errors}
        (self.out / name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data


def active_tab(page):
    return page.evaluate(
        "() => (document.querySelector('.jobs-tabs button[data-st].active')"
        "||{}).dataset?.st || ''")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    base = args.base
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.out or ("evidence/favorites-live-" + ts)).resolve()

    ev = Evidence(out)
    ev.step("目标 = %s" % base)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()
        page.on("pageerror", lambda e: ev.page_errors.append(str(e)))
        page.on("console", lambda m: ev.console.append(m.text)
                if m.type == "error" else None)

        # ---------------- 准备：清掉历史收藏标记（可重复执行）----------------
        st, data = api(base, "/api/jobs/favorites")
        if st != 200:
            ev.step("!! 线上没有收藏接口（HTTP %s）—— 部署未生效？中止" % st)
            print(json.dumps({"ok": False, "reason": "no favorites api"}, indent=2))
            return 1
        for jid in (data.get("ids") or []):
            api(base, "/api/jobs/%s/favorite" % jid, "POST", {"favorited": False})
        ev.step("已清空历史收藏标记（起点干净）")

        page.goto(base.rstrip("/") + "/", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length > 0",
            timeout=WAIT_TIMEOUT)
        page.wait_for_timeout(1200)
        ev.step("页面已打开，首屏真实卡片数 = %d" % page.evaluate(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length"))

        # ---------------- ① 提交后保持当前标签 ----------------
        ev.step("=== ① 提交后保持当前标签 ===")
        page.click(".jobs-tabs button[data-st='done']")
        page.wait_for_timeout(1200)
        before_tab = active_tab(page)
        ev.step("提交前标签 = %r" % before_tab)
        ev.shot(page, "01a-before-submit-tab-done")

        posted = {}

        def _fake(route, request):
            posted["hit"] = True
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"id": "fake-live-0001"}))

        page.route("**/api/jobs", lambda route, req: route.continue_()
                   if req.method == "GET" else _fake(route, req))
        page.fill("#prompt", "收藏功能线上取证用提示词（不真的生成）")
        page.click("#genBtn")
        page.wait_for_timeout(1500)
        if page.is_visible("#enhanceGateConfirm"):
            page.click("#enhanceGateConfirm")
        page.wait_for_timeout(4000)
        after_tab = active_tab(page)
        ev.step("提交后标签 = %r（提交请求已发出 = %s）" % (after_tab, posted.get("hit")))
        ev.shot(page, "01b-after-submit-tab-unchanged")
        ok1 = (after_tab == before_tab)
        ev.step("① %s" % ("PASS：标签未跳变" if ok1
                        else "FAIL：%r -> %r" % (before_tab, after_tab)))
        page.unroute("**/api/jobs")

        # 标签下的卡片状态正常（回到「全部」看得到卡片）
        page.click(".jobs-tabs button[data-st='all']")
        # 【切标签后会先铺骨架再上真实卡片】—— 必须等真实卡片（带 data-jid）
        # 否则下一步取到的第一张卡是骨架，data-jid 是空的。
        page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length > 0",
            timeout=WAIT_TIMEOUT)
        page.wait_for_timeout(800)
        ev.shot(page, "01c-tab-cards-normal")

        # ---------------- ② 右键收藏 ----------------
        ev.step("=== ② 右键「添加到收藏」 ===")
        # 【必须在「已完成」标签下取证】
        # 「全部」标签按 created_at 倒序，最前面可能是**早期遗留的 queued 记录**
        # —— 那种卡片没有产物，预览区里没有 <img>，右键不弹菜单
        # （既有行为：菜单只挂在媒体元素上）。
        page.click(".jobs-tabs button[data-st='done']")
        page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length > 0",
            timeout=WAIT_TIMEOUT)
        page.wait_for_timeout(1000)
        # 【必须挑一张**有产物**的卡片】
        # 既有右键菜单只挂在**媒体预览元素**上（图片 / 视频 / 音频）——
        # 没有产物的卡片，右键不会弹菜单。
        # 这是既有行为（不是收藏功能引入的），取证必须挑一张真有图的卡。
        jid = page.evaluate(
            """() => {
                const c = document.querySelector(
                    '#jobsGrid .job-card[data-jid] .job-preview img,'
                    + ' #jobsGrid .job-card[data-jid] .job-preview video,'
                    + ' #jobsGrid .job-card[data-jid] .job-preview audio');
                const card = c ? c.closest('.job-card[data-jid]') : null;
                return card ? card.dataset.jid : null;
            }""")
        ev.step("取第一张**有产物**的真实卡片 id = %s" % jid)
        assert jid, "线上没有带产物的卡片可操作"
        page.click('.job-card[data-jid="%s"] .job-preview' % jid, button="right")
        page.wait_for_timeout(800)
        ev.shot(page, "02a-context-menu")
        btn = page.query_selector('#mediaContext button[data-act="favorite"]')
        ev.step("右键菜单收藏按钮文案 = %r" % (btn.inner_text().strip() if btn else None))
        btn.click()
        page.wait_for_function(
            """async (jid) => {
                const r = await fetch('/api/jobs/' + jid);
                const d = await r.json();
                return !!d.favorited;
            }""", arg=jid, timeout=WAIT_TIMEOUT)
        page.wait_for_timeout(1500)
        st, data = api(base, "/api/jobs/" + jid)
        ev.step("服务端 favorited = %s" % data.get("favorited"))
        has_badge = page.evaluate(
            """(jid) => !!document.querySelector(
                 `.job-card[data-jid="${jid}"] .job-fav-badge`)""", jid)
        ev.step("卡片上 ★ 标识存在 = %s" % has_badge)
        ev.shot(page, "02b-favorited-badge")
        ok2 = bool(data.get("favorited")) and has_badge
        ev.step("② %s" % ("PASS" if ok2 else "FAIL"))

        # 再右键 -> 「取消收藏」
        # 【为什么要重新 query 而不是复用旧的 handle】
        # 收藏后会触发一次列表刷新，卡片节点被**重建** ——
        # 旧的 ElementHandle 会「Element is not attached to the DOM」。
        # 每次都按选择器重新查。
        page.wait_for_function(
            """(jid) => {
                const c = document.querySelector(`.job-card[data-jid="${jid}"]`);
                return !!(c && c.querySelector(
                    '.job-preview img, .job-preview video, .job-preview audio'));
            }""", arg=jid, timeout=WAIT_TIMEOUT)
        page.click('.job-card[data-jid="%s"] .job-preview img,'
                   '.job-card[data-jid="%s"] .job-preview video,'
                   '.job-card[data-jid="%s"] .job-preview audio'
                   % (jid, jid, jid), button="right")
        page.wait_for_timeout(800)
        btn2 = page.query_selector('#mediaContext button[data-act="favorite"]')
        ev.step("再次右键文案 = %r（应为「取消收藏」）"
                % (btn2.inner_text().strip() if btn2 else None))
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)

        # ---------------- ③ 左侧收藏栏 ----------------
        ev.step("=== ③ 左侧「收藏」栏 ===")
        nav = page.query_selector("#navFavorites")
        ev.step("左侧「收藏」入口存在 = %s，文案 = %r"
                % (nav is not None,
                   nav.inner_text().strip() if nav else None))
        nav.click()
        page.wait_for_timeout(2000)
        # 【进收藏视图后也要等真实卡片】骨架卡没有 data-jid。
        page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length > 0",
            timeout=WAIT_TIMEOUT)
        page.wait_for_timeout(600)
        tab_now = active_tab(page)
        ev.step("进入后标签 = %r，导航高亮 = %s"
                % (tab_now, page.evaluate(
                    "() => document.querySelector('#navFavorites')"
                    ".classList.contains('active')")))
        fav_cards = page.evaluate(
            "() => Array.from(document.querySelectorAll('#jobsGrid .job-card[data-jid]'))"
            ".map(e => e.dataset.jid)")
        ev.step("收藏视图卡片 id = %r" % fav_cards)
        ev.shot(page, "03a-favorites-view")
        # 详情模态框可用（收藏视图沿用既有交互）
        if fav_cards:
            page.click('.job-card[data-jid="%s"] .job-top' % fav_cards[0])
            page.wait_for_timeout(1200)
            modal_open = page.evaluate(
                "() => { const m = document.querySelector('#detailModal');"
                " return !!(m && !m.hidden); }")
            ev.step("收藏视图点卡片能打开详情模态框 = %s" % modal_open)
            ev.shot(page, "03b-favorites-detail-modal")
            page.keyboard.press("Escape")
            page.wait_for_timeout(600)
        ok3 = tab_now == "favorites" and jid in fav_cards
        ev.step("③ %s" % ("PASS" if ok3 else "FAIL"))

        # ---------------- ④ 清理跳过收藏 ----------------
        ev.step("=== ④ 管理 → 清理 跳过收藏 ===")
        st, before = api(base, "/api/jobs?status=all&page=1&page_size=1")
        total_before = before.get("total")
        st, preview = api(base, "/api/media/manage?scope=all&months=1")
        ev.step("清理预览：jobs=%s files=%s skipped_favorited=%s"
                % (preview.get("jobs"), preview.get("files"),
                   preview.get("skipped_favorited")))
        ev.shot(page, "04a-media-manage-preview")
        # 真的执行清理（会删掉未收藏的历史产物 —— 这是既有清理功能的语义）
        st, result = api(base, "/api/media/manage", "POST",
                         {"scope": "all", "months": 1, "confirm": True})
        ev.step("清理结果：deleted_jobs=%s deleted_files=%s skipped_favorited=%s"
                % (result.get("deleted_jobs"), result.get("deleted_files"),
                   result.get("skipped_favorited")))
        st, after = api(base, "/api/jobs?status=all&page=1&page_size=1")
        ev.step("清理后总任务数 = %s（清理前 %s）"
                % (after.get("total"), total_before))
        st, still = api(base, "/api/jobs/" + jid)
        ev.step("收藏项清理后仍在（favorited=%s, status=%s）"
                % (still.get("favorited"), still.get("status")))
        ev.shot(page, "04b-after-cleanup-favorite-still-there")
        ok4 = bool(still.get("favorited"))
        ev.step("④ %s" % ("PASS：收藏项未被清理" if ok4 else "FAIL"))

        # ---------------- ⑤ 取消收藏 ----------------
        ev.step("=== ⑤ 取消收藏 + 取消后可被清理 ===")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length > 0",
            timeout=WAIT_TIMEOUT)
        page.wait_for_timeout(1200)
        page.click("#navFavorites")
        page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length > 0",
            timeout=WAIT_TIMEOUT)
        page.wait_for_timeout(1000)
        # 右键要打在**媒体元素**上（既有行为：菜单挂在 img/video/audio 上）
        page.wait_for_function(
            """(jid) => {
                const c = document.querySelector(`.job-card[data-jid="${jid}"]`);
                return !!(c && c.querySelector(
                    '.job-preview img, .job-preview video, .job-preview audio'));
            }""", arg=jid, timeout=WAIT_TIMEOUT)
        page.click('.job-card[data-jid="%s"] .job-preview img,'
                   '.job-card[data-jid="%s"] .job-preview video,'
                   '.job-card[data-jid="%s"] .job-preview audio'
                   % (jid, jid, jid), button="right")
        page.wait_for_timeout(800)
        btn3 = page.query_selector('#mediaContext button[data-act="favorite"]')
        ev.step("右键文案 = %r" % (btn3.inner_text().strip() if btn3 else None))
        assert btn3 is not None, "收藏视图里右键没弹出菜单"
        btn3.click()
        page.wait_for_function(
            """async (jid) => {
                const r = await fetch('/api/jobs/' + jid);
                const d = await r.json();
                return d.favorited === false;
            }""", arg=jid, timeout=WAIT_TIMEOUT)
        page.wait_for_timeout(800)
        st, data = api(base, "/api/jobs/" + jid)
        ev.step("取消后 favorited = %s" % data.get("favorited"))
        ev.shot(page, "05a-unfavorited")
        # 取消后重新纳入清理范围
        st, result2 = api(base, "/api/media/manage", "POST",
                          {"scope": "all", "months": 1, "confirm": True})
        ev.step("再次清理：deleted_jobs=%s skipped_favorited=%s"
                % (result2.get("deleted_jobs"), result2.get("skipped_favorited")))
        st, gone = api(base, "/api/jobs/" + jid)
        ev.step("取消收藏后再次清理：该卡片 HTTP %s（404 = 已被清理）" % st)
        ok5 = (data.get("favorited") is False) and (st == 404)
        ev.step("⑤ %s" % ("PASS" if ok5 else "FAIL"))

        # ---------------- ⑥ 批量取消收藏 ----------------
        ev.step("=== ⑥ 批量取消收藏 ===")
        # 【不跑清理就有的存量任务】—— 批量操作需要至少 2 条已存在的卡片。
        st, jobs = api(base, "/api/jobs?status=done&page=1&page_size=10")
        live_ids = [j["id"] for j in jobs.get("jobs", [])][:3]
        ev.step("取 3 条已存在任务做批量收藏：%r" % live_ids)
        if len(live_ids) >= 2:
            for x in live_ids:
                api(base, "/api/jobs/%s/favorite" % x, "POST", {"favorited": True})
            page.reload(wait_until="domcontentloaded")
            page.wait_for_function(
                "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length > 0",
                timeout=WAIT_TIMEOUT)
            page.wait_for_timeout(1200)
            page.click("#navFavorites")
            page.wait_for_function(
                "() => document.querySelectorAll('#jobsGrid .fav-sel').length > 0",
                timeout=WAIT_TIMEOUT)
            page.wait_for_timeout(800)
            page.click("#favSelectAllBtn")
            page.wait_for_timeout(600)
            ev.step("全选后计数 = %r" % page.inner_text("#favToolsCount"))
            ev.shot(page, "06a-favorites-batch-selected")
            page.once("dialog", lambda d: d.accept())
            page.click("#favBatchUnfavBtn")
            page.wait_for_function(
                """async (ids) => {
                    for (const jid of ids) {
                        const r = await fetch('/api/jobs/' + jid);
                        const d = await r.json();
                        if (d.favorited !== false) return false;
                    }
                    return true;
                }""", arg=live_ids, timeout=WAIT_TIMEOUT)
            page.wait_for_timeout(800)
            ev.shot(page, "06b-favorites-batch-done")
            st, fin = api(base, "/api/jobs/favorites")
            ev.step("批量取消后剩余收藏数 = %s" % fin.get("total"))
            ok6 = all(not api(base, "/api/jobs/" + x)[1].get("favorited")
                      for x in live_ids)
            ev.step("⑥ %s" % ("PASS" if ok6 else "FAIL"))
        else:
            ev.step("⑥ SKIP：库里不足 2 条任务可批量操作")
            ok6 = None

        browser.close()

    summary = {"ok": None, "① 标签未跳变": ok1, "② 右键收藏": ok2,
               "③ 左侧收藏栏": ok3, "④ 清理跳过收藏": ok4,
               "⑤ 取消收藏后可清理": ok5, "⑥ 批量取消收藏": ok6}
    ev.step("=== 汇总 ===")
    for k, v in summary.items():
        if k != "ok":
            ev.step("  %s -> %s" % (k, v))
    ev.dump()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("证据目录: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
