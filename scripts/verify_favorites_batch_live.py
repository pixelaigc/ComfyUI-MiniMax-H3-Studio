# -*- coding: utf-8 -*-
"""线上 8190 的 ⑥ 批量取消收藏取证（独立脚本，不依赖清理流程）。

【为什么单独一个脚本】
需求 4 的取证（管理 -> 清理）会真的删掉线上未收藏的历史产物 ——
把它和 ⑥ 放在一起跑，⑥ 就没有可操作的存量卡片了（实测就是 SKIP）。
所以 ⑥ 单独取证：只用「收藏 / 取消收藏」两个接口，不碰清理。

运行：
    /home/ubuntu/comfyui/venv/bin/python scripts/verify_favorites_batch_live.py \
        --base http://127.0.0.1:8190 --out evidence/favorites-batch-live-<ts>
"""
from __future__ import annotations

import argparse
import json
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    base = args.base
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.out or ("evidence/favorites-batch-live-" + ts)).resolve()
    out.mkdir(parents=True, exist_ok=True)
    steps = []
    shots = []

    def step(msg):
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
        steps.append(line)
        print(line, flush=True)

    def shot(page, name):
        p = out / (name + ".png")
        page.screenshot(path=str(p), full_page=False)
        shots.append(str(p))
        step("截图 -> %s" % p)

    # 清干净起点
    st, data = api(base, "/api/jobs/favorites")
    if st != 200:
        step("!! 线上没有收藏接口（HTTP %s）—— 部署未生效？中止" % st)
        return 1
    for jid in (data.get("ids") or []):
        api(base, "/api/jobs/%s/favorite" % jid, "POST", {"favorited": False})
    step("已清空历史收藏标记（起点干净）")

    st, jobs = api(base, "/api/jobs?status=done&page=1&page_size=12")
    ids = [j["id"] for j in jobs.get("jobs", [])][:3]
    step("取 3 条已完成的存量任务：%r" % ids)
    if len(ids) < 2:
        step("!! 库里不足 2 条已完成任务，无法取证")
        return 1
    for jid in ids:
        api(base, "/api/jobs/%s/favorite" % jid, "POST", {"favorited": True})
    st, fav = api(base, "/api/jobs/favorites")
    step("服务端收藏数 = %s（ids=%r）" % (fav.get("total"), fav.get("ids")))

    ok = False
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(base.rstrip("/") + "/", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length > 0",
            timeout=WAIT_TIMEOUT)
        page.wait_for_timeout(1200)

        # 进左侧「收藏」栏
        nav = page.query_selector("#navFavorites")
        step("左侧「收藏」入口存在 = %s" % (nav is not None))
        nav.click()
        page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .fav-sel').length > 0",
            timeout=WAIT_TIMEOUT)
        page.wait_for_timeout(800)
        tab = page.evaluate(
            "() => (document.querySelector('.jobs-tabs button[data-st].active')"
            "||{}).dataset?.st || ''")
        step("进入后标签 = %r" % tab)
        sel_count = page.evaluate(
            "() => document.querySelectorAll('#jobsGrid .fav-sel').length")
        step("收藏视图里的选择框数 = %d" % sel_count)
        shot(page, "06a-favorites-view-with-selectboxes")

        # 手动勾选前 2 个（比「全选本页」更可控，也能证明逐个勾选）
        boxes = page.query_selector_all("#jobsGrid .fav-sel")
        for i in range(min(2, len(boxes))):
            boxes[i].check()
            page.wait_for_timeout(300)
        page.wait_for_timeout(600)
        count_text = page.inner_text("#favToolsCount")
        step("勾选 2 个后计数 = %r" % count_text)
        shot(page, "06b-batch-selected")

        # 批量取消收藏
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
            }""", arg=ids[:2], timeout=WAIT_TIMEOUT)
        page.wait_for_timeout(800)
        shot(page, "06c-batch-done")

        st, fin = api(base, "/api/jobs/favorites")
        step("批量取消后剩余收藏数 = %s（ids=%r）" % (fin.get("total"), fin.get("ids")))
        still = [j for j in ids[:2] if api(base, "/api/jobs/" + j)[1].get("favorited")]
        step("仍处于收藏态的 id = %r" % still)
        ok = not still
        step("⑥ %s" % ("PASS：批量取消收藏生效" if ok else "FAIL"))
        step("控制台错误 = %r" % errors[-5:])
        browser.close()

    (out / "evidence.json").write_text(json.dumps(
        {"steps": steps, "screenshots": shots, "result": ok},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"⑥ 批量取消收藏": ok}, ensure_ascii=False))
    print("证据目录: %s" % out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
