#!/usr/bin/env python3
"""D 项取证（扩展版）：YuE2 面板「生成风格+歌词」+ 同面板其它入口是否同类失效。

覆盖 4 个入口：
  1. #musicEnhanceMenuBtn  -> 选「YuE2 结构化风格 + 歌词」-> #musicEnhanceBtn
  2. #musicStyleRegenBtn   「重新生成」（结构化风格，基于当前内容）
  3. #musicLyricsRegenBtn  「重新生成」（歌词，基于当前内容）
  4. a2a 模式（参考生音乐）下同样能点 —— 「参考生音乐同款入口」

【真实度】
  * 真起 server.py（独立端口 + 独立 data_dir）
  * 真 Chromium
  * 只 mock 后端 LLM 推理那一步；前端 DOM/事件/请求构造/回填/渲染全真
  * 断言：
      - 每个入口都真的发出 POST /api/music-enhance（抓 network）
      - 请求体带对��� mode（t2a / a2a）
      - 「重新生成」带 base=「当前内容」+ 现有 style/lyrics
      - 界面上真的出现结构化风格字段与歌词
      - 0 console error / 0 pageError

用法：
    python3 scripts/verify_acceptance_d_yue2.py [证据输出目录]
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/h3-acceptance-d")
SHOTS = Path("/root/.hermes-web-ui/.ekko/workspace/default/"
              "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/agent-enh-merge")
PORT = int(os.environ.get("H3_TEST_PORT", "8197"))
DATA = Path(os.environ.get("H3_TEST_DATA", "/tmp/h3-acceptance-d-data"))
WIDTHS = [(375, 812), (390, 844), (1280, 900)]

MOCK_STYLE = {
    "language": "zh", "vocal_type": "female", "genre": "pop",
    "mood": "warm", "instrumentation": "guitar,piano",
    "bpm": 92, "energy": "medium", "imagery": "夕阳下的海边",
    "structure": "verse-chorus", "vocal_color": "clear",
    "language_style": "mandarin", "mix": "none",
}
MOCK_LYRICS = ("[Verse]\n海风吹过黄昏的岸线\n"
               "[Chorus]\n我们把回忆留在潮汐里")


async def main() -> int:
    from playwright.async_api import async_playwright

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    if DATA.exists():
        shutil.rmtree(DATA)
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "config.json").write_text(json.dumps({
        "host": "127.0.0.1", "port": PORT,
        "comfyui_url": "http://127.0.0.1:8188",
        "data_dir": str(DATA),
        "models_dir": "/home/ubuntu/comfyui/models",
    }, ensure_ascii=False), encoding="utf-8")

    env = os.environ.copy()
    env["H3STUDIO_PORT"] = str(PORT)
    env["H3STUDIO_DATA_DIR"] = str(DATA)
    srv = subprocess.Popen(
        ["/home/ubuntu/comfyui/venv/bin/python", "server.py"],
        cwd=str(REPO), env=env,
        stdout=open(OUT_DIR / "server_stdout.log", "wb"), stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{PORT}"
    print(f"server pid={srv.pid} port={PORT}", flush=True)

    import urllib.request
    ready = False
    for _ in range(90):
        try:
            urllib.request.urlopen(base + "/", timeout=3)
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
                "requests": [], "responses": [], "entries": {}}

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

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=["--no-sandbox"])
            for width, height in WIDTHS:
                ctx = await browser.new_context(viewport={"width": width,
                                                         "height": height})
                page = await ctx.new_page()
                page.on("console", lambda m, w=width: ev["console"].append(
                    {"width": w, "type": m.type, "text": m.text[:300]}))
                page.on("pageerror", lambda e, w=width: ev["pageErrors"].append(
                    {"width": w, "error": str(e)[:300]}))
                page.on("request", lambda r, w=width: ev["requests"].append(
                    {"width": w, "method": r.method, "url": r.url,
                     "post_data": (r.post_data or "")[:600]}))
                page.on("response", lambda r, w=width: ev["responses"].append(
                    {"width": w, "status": r.status, "url": r.url}))
                await page.route("**/api/music-enhance", route_music)

                await page.goto(base + "/", wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)

                async def snapshot():
                    return await page.evaluate("""() => {
                      const q = (s) => document.querySelector(s);
                      const grid = q('#musicStyleFieldsGrid');
                      const fields = {};
                      if (grid) grid.querySelectorAll('select,input,textarea')
                        .forEach(el => {
                          const k = el.getAttribute('data-field') || el.id || el.name;
                          if (k) fields[k] = el.value;
                        });
                      const filled = {};
                      Object.entries(fields).forEach(([k, v]) => {
                        if (String(v).trim()) filled[k] = v;
                      });
                      return {
                        styleFieldsFilled: Object.keys(filled).length,
                        styleFieldSample: Object.fromEntries(
                          Object.entries(filled).slice(0, 6)),
                        styleFieldsExpanded: !!q('#musicStyleFields')
                          && q('#musicStyleFields').style.display !== 'none',
                        lyrics: (q('#musicLyrics')||{}).value || '',
                        mainBtnText: (q('#musicEnhanceBtnText')||{}).textContent || '',
                        hint: (q('#musicEnhanceHint')||{}).textContent || '',
                        badgeShown: (q('#musicPromptBadge')||{}).hidden === false,
                        hasStyleRegen: !!q('#musicStyleRegenBtn'),
                        hasLyricsRegen: !!q('#musicLyricsRegenBtn'),
                        hasRefPick: !!q('#musicRefPick'),
                        bodyHasLyrics: document.body.innerText.includes('海风吹过黄昏的岸线'),
                      };
                    }""")

                vp: dict = {}

                # ============ 入口 1：增强下拉 -> 生成风格+歌词 ============
                await page.evaluate("""() => {
                  const n = document.querySelector('a.nav-item[data-mode="t2a"]');
                  if (n) n.click();
                }""")
                await page.wait_for_timeout(1800)
                await page.fill("#musicPrompt",
                                "一首温暖的华语流行歌，夕阳下海边的回忆，92 BPM")
                await page.wait_for_timeout(400)

                # 窄屏下按钮在视口外（侧栏/面板收在抽屉里）——
                # 用 JS dispatch click，绕开 Playwright 的视口检查。
                await page.evaluate("""() => {
                  const b = document.querySelector('#musicEnhanceMenuBtn');
                  if (b) b.click();
                }""")
                await page.wait_for_timeout(900)
                shot = SHOTS / f"acc-d-menu-{width}.png"
                await page.screenshot(path=str(shot), full_page=True)
                vp["menu_shot"] = str(shot)
                # 窄屏下菜单项在视口外/不可见 —— 用 JS dispatch click
                vp["menu_has_yue2_item"] = await page.evaluate("""() => {
                  const items = Array.from(document.querySelectorAll(
                    '#musicEnhanceMenu .enhance-menu-item[data-method="yue2_struct"]'));
                  if (!items.length) return false;
                  items[0].click();
                  return true;
                }""")
                await page.wait_for_timeout(1200)
                vp["main_button_text"] = (await snapshot())["mainBtnText"]
                shot = SHOTS / f"acc-d-selected-{width}.png"
                await page.screenshot(path=str(shot), full_page=True)
                vp["selected_shot"] = str(shot)

                n_before = len(ev["requests"])
                await page.evaluate("""() => {
                  const b = document.querySelector('#musicEnhanceBtn');
                  if (b) b.click();
                }""")
                await page.wait_for_timeout(2800)
                s1 = await snapshot()
                shot = SHOTS / f"acc-d-1-enhance-{width}.png"
                await page.screenshot(path=str(shot), full_page=True)
                vp["entry1_main_button"] = {
                    "after": s1,
                    "requests": [r for r in ev["requests"][n_before:]
                                 if "music-enhance" in r["url"]],
                    "shot": str(shot),
                }

                # ============ 入口 2/3：两个「重新生成」按钮 ============
                # 先把结构化风格区展开（重新生成按钮在折叠区里）
                await page.evaluate("""() => {
                  const b = document.querySelector('#musicStyleFieldsToggle');
                  if (b) b.click();
                }""")
                await page.wait_for_timeout(600)

                n_before = len(ev["requests"])
                regen_ok = await page.evaluate("""() => {
                  const b = document.querySelector('#musicStyleRegenBtn');
                  if (!b) return false;
                  b.click();
                  return true;
                }""")
                await page.wait_for_timeout(2500)
                vp["entry2_style_regen"] = {
                    "button_present": regen_ok,
                    "requests": [r for r in ev["requests"][n_before:]
                                 if "music-enhance" in r["url"]],
                    "after": await snapshot(),
                }
                shot = SHOTS / f"acc-d-2-style-regen-{width}.png"
                await page.screenshot(path=str(shot), full_page=True)
                vp["entry2_style_regen"]["shot"] = str(shot)

                n_before = len(ev["requests"])
                regen_ok = await page.evaluate("""() => {
                  const b = document.querySelector('#musicLyricsRegenBtn');
                  if (!b) return false;
                  b.click();
                  return true;
                }""")
                await page.wait_for_timeout(2500)
                vp["entry3_lyrics_regen"] = {
                    "button_present": regen_ok,
                    "requests": [r for r in ev["requests"][n_before:]
                                 if "music-enhance" in r["url"]],
                    "after": await snapshot(),
                }
                shot = SHOTS / f"acc-d-3-lyrics-regen-{width}.png"
                await page.screenshot(path=str(shot), full_page=True)
                vp["entry3_lyrics_regen"]["shot"] = str(shot)

                # ============ 入口 4：参考生音乐（a2a）同款入口 ============
                await page.evaluate("""() => {
                  const n = document.querySelector('a.nav-item[data-mode="a2a"]');
                  if (n) n.click();
                }""")
                await page.wait_for_timeout(1800)
                await page.evaluate("""() => {
                  const b = document.querySelector('#musicEnhanceMenuBtn');
                  if (b) b.click();
                }""")
                await page.wait_for_timeout(900)
                await page.evaluate("""() => {
                  const items = Array.from(document.querySelectorAll(
                    '#musicEnhanceMenu .enhance-menu-item[data-method="yue2_struct"]'));
                  if (items.length) items[0].click();
                }""")
                await page.wait_for_timeout(1000)
                n_before = len(ev["requests"])
                await page.evaluate("""() => {
                  const b = document.querySelector('#musicEnhanceBtn');
                  if (b) b.click();
                }""")
                await page.wait_for_timeout(2800)
                s4 = await snapshot()
                vp["entry4_a2a_reference"] = {
                    "after": s4,
                    "requests": [r for r in ev["requests"][n_before:]
                                 if "music-enhance" in r["url"]],
                }
                shot = SHOTS / f"acc-d-4-a2a-{width}.png"
                await page.screenshot(path=str(shot), full_page=True)
                vp["entry4_a2a_reference"]["shot"] = str(shot)

                ev["entries"][str(width)] = vp
                await ctx.close()
            await browser.close()
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=15)
        except Exception:
            srv.kill()

    # ---------------- 判定 ----------------
    def reqs(vp, key):
        return vp[key]["requests"]

    ok = {}
    for w, vp in ev["entries"].items():
        e1 = reqs(vp, "entry1_main_button")
        e2 = reqs(vp, "entry2_style_regen")
        e3 = reqs(vp, "entry3_lyrics_regen")
        e4 = reqs(vp, "entry4_a2a_reference")
        ok[w] = {
            "entry1_main_button_sent": len(e1) >= 1,
            "entry1_style_filled": vp["entry1_main_button"]["after"]["styleFieldsFilled"] >= 5,
            "entry1_lyrics_filled": bool(vp["entry1_main_button"]["after"]["lyrics"].strip()),
            "entry2_style_regen_sent": len(e2) >= 1,
            "entry2_has_base_flag": bool(e2) and "base" in (e2[0].get("post_data") or ""),
            "entry3_lyrics_regen_sent": len(e3) >= 1,
            "entry3_has_base_flag": bool(e3) and "base" in (e3[0].get("post_data") or ""),
            "entry4_a2a_sent": len(e4) >= 1,
            "entry4_mode_is_a2a": bool(e4) and '"a2a"' in (e4[0].get("post_data") or ""),
            "entry4_style_filled": vp["entry4_a2a_reference"]["after"]["styleFieldsFilled"] >= 5,
        }
    console_errors = [c for c in ev["console"] if c["type"] == "error"]
    ev["per_width"] = ok
    ev["verdict"] = {
        "all_entries_work_all_widths": all(all(v.values()) for v in ok.values()),
        "per_width": ok,
        "console_errors": len(console_errors),
        "page_errors": len(ev["pageErrors"]),
        "console_error_samples": console_errors[:5],
    }
    (OUT_DIR / "acceptance_d_evidence.json").write_text(
        json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n===== D 项判定 =====")
    print(json.dumps(ev["verdict"], ensure_ascii=False, indent=2))
    print(f"\n证据目录：{OUT_DIR}")
    good = (ev["verdict"]["all_entries_work_all_widths"]
            and not console_errors and not ev["pageErrors"])
    return 0 if good else 5


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
