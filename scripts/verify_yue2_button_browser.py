#!/usr/bin/env python3
"""D 项取证：真实浏览器点击「★生成风格+歌词」按钮，抓 network/console + 截图。

【交互事实（2026-11-12 实测确认）】
「生成风格+歌词」是 **enhance-menu.js 里 yue2_struct 这一项的 buttonText**，
不是独立的 DOM 按钮：
  * 下拉菜单项的 label 是「YuE2 结构化风格 + 歌词」
  * 选中它之后，主按钮 #musicEnhanceBtn 的文案被 updateButton() 改成
    「生成风格+歌词」（enhance-menu.js:942）
  * 点主按钮 -> runOne(method) -> method.kind === "yue2-struct"
    -> runYue2Struct() -> window.MusicEnhance.run()
    -> POST /api/music-enhance -> applyResult() 回填结构化风格 + 歌词

所以真实点击路径是：**开下拉 -> 选「YuE2 结构化风格 + 歌词」-> 点主按钮**。

【真实度】
  * 真起 server.py（独立端口 ≥8197，独立 data_dir）
  * 真 Chromium
  * 前端 DOM / 事件 / 请求构造 / 回填 / 渲染 全是真实代码路径
  * 只 mock **后端 LLM 推理**那一步（/api/music-enhance 的响应体），
    避免依赖本机大模型一次真实推理；请求本身由前端真实发出
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = Path("/tmp/h3-fix-evidence")
SHOTS = Path("/root/.hermes-web-ui/.ekko/workspace/default/"
              "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/agent-enh-merge")
PORT = int(os.environ.get("H3_TEST_PORT", "8197"))
DATA_DIR = Path(os.environ.get("H3_TEST_DATA", "/tmp/h3-yue2-d-e2e"))

MOCK_STYLE = {
    "language": "zh", "vocal_type": "female", "genre": "pop",
    "mood": "warm", "instrumentation": "guitar,piano",
    "bpm": 92, "energy": "medium", "imagery": "夕阳下的海边",
    "structure": "verse-chorus", "vocal_color": "clear",
    "language_style": "mandarin", "mix": "none",
}
MOCK_LYRICS = ("[Verse]\n海风吹过黄昏的岸线\n"
               "[Chorus]\n我们把回忆留在潮汐里")


def _slim(ev: dict) -> dict:
    """只保留 POST /api/music-enhance（排除静态资源同名误匹配）。"""
    return [r for r in ev["requests"]
            if "music-enhance" in r["url"] and r["method"] == "POST"]


async def main() -> int:
    from playwright.async_api import async_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)

    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "config.json").write_text(json.dumps({
        "data_dir": str(DATA_DIR),
        "models_dir": "/home/ubuntu/comfyui/models",
    }, ensure_ascii=False), encoding="utf-8")

    env = os.environ.copy()
    env["H3STUDIO_PORT"] = str(PORT)
    env["H3STUDIO_DATA_DIR"] = str(DATA_DIR)
    env["PORT"] = str(PORT)
    srv = subprocess.Popen(
        ["/home/ubuntu/comfyui/venv/bin/python", "server.py"],
        cwd=str(REPO), env=env,
        stdout=open("/tmp/h3-fix-evidence/yue2_d_server.log", "wb"),
        stderr=subprocess.STDOUT,
    )
    print(f"server pid={srv.pid} port={PORT}", flush=True)
    base = f"http://127.0.0.1:{PORT}"

    import urllib.request
    ready = False
    for _ in range(90):
        try:
            with urllib.request.urlopen(base + "/", timeout=3) as r:
                if r.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(1)
    if not ready:
        srv.terminate()
        print("!! server 未就绪")
        return 2
    print("server ready", flush=True)

    ev: dict = {"port": PORT, "console": [], "pageErrors": [],
                "requests": [], "responses": [], "shots": []}

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=["--no-sandbox"])
            ctx = await browser.new_context(viewport={"width": 1280, "height": 1000})
            page = await ctx.new_page()
            page.on("console", lambda m: ev["console"].append(
                {"type": m.type, "text": m.text[:300]}))
            page.on("pageerror", lambda e: ev["pageErrors"].append(str(e)[:300]))
            page.on("request", lambda r: ev["requests"].append(
                {"method": r.method, "url": r.url, "post_data": (r.post_data or "")[:600]}))
            page.on("response", lambda r: ev["responses"].append(
                {"status": r.status, "url": r.url}))

            await page.goto(base + "/", wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)

            # 干净状态（清 localStorage，避免上一次会话的风格字段污染 before 快照）
            await page.evaluate("() => { try { localStorage.clear(); } catch (e) {} }")
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)

            # 进入音乐页
            nav = await page.query_selector('a.nav-item[data-mode="t2a"]')
            if nav is None:
                ev["error"] = "找不到音乐导航"
                await browser.close()
                return 3
            await nav.click()
            ev["nav_selector"] = 'a.nav-item[data-mode="t2a"]'
            await page.wait_for_timeout(2000)

            await page.fill("#musicPrompt",
                            "一首温暖的华语流行歌，讲述夕阳下海边的回忆，吉他与钢琴伴奏，92 BPM")
            await page.wait_for_timeout(300)
            await page.screenshot(path=str(SHOTS / "yue2-d-01-music-panel.png"),
                                  full_page=True)
            ev["shots"].append(str(SHOTS / "yue2-d-01-music-panel.png"))

            # mock 后端 LLM 响应（前端路径保持真实）
            async def route_music(route):
                await route.fulfill(
                    status=200, content_type="application/json",
                    body=json.dumps({
                        "style": MOCK_STYLE,
                        "lyrics": {"template": "verse-chorus",
                                   "lyrics_override": MOCK_LYRICS},
                        "style_text": "zh, female, pop, warm, guitar/piano, 92 BPM",
                        "lyrics_text": MOCK_LYRICS,
                        "fixes": [], "problems": [], "provider": "mock-d-verify",
                    }, ensure_ascii=False))
            await page.route("**/api/music-enhance", route_music)

            def snapshot():
                return page.evaluate("""() => {
                  const q = (s) => document.querySelector(s);
                  const grid = q('#musicStyleFieldsGrid');
                  const fields = {};
                  if (grid) {
                    grid.querySelectorAll('select,input,textarea').forEach(el => {
                      const k = el.getAttribute('data-field') || el.id || el.name;
                      if (k) fields[k] = el.value;
                    });
                  }
                  return {
                    mainBtnText: (q('#musicEnhanceBtnText')||{}).textContent || null,
                    styleFields: fields,
                    stylePreview: (q('#musicStyleFieldsPreview')||{}).innerText || null,
                    styleNote: (q('#musicStyleStructNote')||{}).textContent || null,
                    lyrics: (q('#musicLyrics')||{}).value || null,
                    hint: (q('#musicEnhanceHint')||{}).textContent || null,
                    current: (q('#musicEnhanceCurrent')||{}).textContent || null,
                    badgeHidden: (q('#musicPromptBadge')||{}).hidden,
                    styleFieldsVisible: !!q('#musicStyleFields')
                      && q('#musicStyleFields').style.display !== 'none',
                  };
                }""")

            before = await snapshot()
            ev["before"] = before

            # ---- 步骤 1：打开增强下拉，选「YuE2 结构化风格 + 歌词」 ----
            await page.click("#musicEnhanceMenuBtn")
            await page.wait_for_timeout(900)
            await page.screenshot(path=str(SHOTS / "yue2-d-02-enhance-menu.png"),
                                  full_page=True)
            ev["shots"].append(str(SHOTS / "yue2-d-02-enhance-menu.png"))

            menu_item = page.locator("#musicEnhanceMenu").get_by_text(
                "YuE2 结构化风格 + 歌词", exact=False).first
            if not await menu_item.count():
                ev["error"] = "下拉里找不到「YuE2 结构化风格 + 歌词」"
                await browser.close()
                return 4
            await menu_item.click()
            await page.wait_for_timeout(1200)
            ev["selected_menu_label"] = (await menu_item.inner_text()).strip()

            after_select = await snapshot()
            ev["after_select"] = after_select
            ev["main_button_text_after_select"] = after_select.get("mainBtnText")
            await page.screenshot(path=str(SHOTS / "yue2-d-03-selected.png"),
                                  full_page=True)
            ev["shots"].append(str(SHOTS / "yue2-d-03-selected.png"))

            # ---- 步骤 2：点主按钮（此时文案应为「生成风格+歌词」）----
            btn_text = after_select.get("mainBtnText") or ""
            await page.click("#musicEnhanceBtn")
            await page.wait_for_timeout(3000)

            after = await snapshot()
            ev["after"] = after
            shot = SHOTS / "yue2-d-04-after-click.png"
            await page.screenshot(path=str(shot), full_page=True)
            ev["shots"].append(str(shot))

            await browser.close()
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=15)
        except Exception:
            srv.kill()

    ev["music_enhance_requests"] = _slim(ev)
    ev["music_enhance_responses"] = [
        r for r in ev["responses"]
        if "music-enhance" in r["url"] and r["status"] == 200
        and "/static/" not in r["url"]]

    before, after = ev["before"], ev["after"]
    style_fields = after.get("styleFields") or {}
    non_empty = {k: v for k, v in style_fields.items() if str(v).strip()}
    ok_click = len(ev["music_enhance_requests"]) >= 1
    ok_btn_label = "生成风格+歌词" in (ev.get("main_button_text_after_select") or "")
    ok_style = len(non_empty) >= 5
    ok_lyrics = bool((after.get("lyrics") or "").strip())
    ok_noerr = not ev["pageErrors"]
    ev["verdict"] = {
        "main_button_text_after_select": ev.get("main_button_text_after_select"),
        "main_button_label_is_生成风格+歌词": ok_btn_label,
        "request_sent": ok_click,
        "request_count": len(ev["music_enhance_requests"]),
        "request_post_data": (ev["music_enhance_requests"][0]["post_data"]
                              if ev["music_enhance_requests"] else None),
        "style_fields_filled": len(non_empty),
        "style_field_sample": dict(list(non_empty.items())[:8]),
        "style_fields_expanded": after.get("styleFieldsVisible"),
        "lyrics_filled": ok_lyrics,
        "lyrics_preview": (after.get("lyrics") or "")[:120],
        "style_note": after.get("styleNote"),
        "hint": after.get("hint"),
        "badge_shown": after.get("badgeHidden") is False,
        "no_page_errors": ok_noerr,
        "page_errors": ev["pageErrors"],
        "console_errors": [c for c in ev["console"] if c["type"] == "error"],
        "before_style_fields_filled": len([1 for v in (before.get('styleFields') or {}).values() if str(v).strip()]),
        "before_lyrics": (before.get("lyrics") or "")[:60],
    }
    (OUT / "yue2_d_button_evidence.json").write_text(
        json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n===== D 项判定 =====")
    print(json.dumps(ev["verdict"], ensure_ascii=False, indent=2))
    print(f"截图 = {ev['shots']}")
    return 0 if (ok_click and ok_btn_label and ok_style and ok_lyrics and ok_noerr) else 5


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
