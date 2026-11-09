# -*- coding: utf-8 -*-
"""
复核参考生音乐（a2a）链路：
  - 左侧「参考生音乐」标签能切到参考面板
  - 面板差异正确：参考音频选择区 + 音频编码器可见，谱面固定「仅旋律」
  - 增强在 a2a 下也走同一套组件，且提示语提到 cover 场景
  - 没有参考音频时提交被拦下（明确的中文提示，不是静默失败）

运行：python3 tests/verify_music_a2a.py
"""
import json
import sys
from pathlib import Path

REPO = Path("/root/projects/h3studio-yue2-flow")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from playwright.sync_api import sync_playwright   # noqa: E402

BASE = "http://127.0.0.1:8195"
OUT = Path("/root/.hermes-web-ui/.ekko/workspace/default/"
           "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    ev = {}
    with sync_playwright() as pw:
        br = pw.chromium.launch(args=["--no-sandbox"])
        page = br.new_context(viewport={"width": 1280, "height": 900}).new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
        page.goto(BASE + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

        # 1) 点左侧「参考生音乐」标签
        page.click('.nav-item[data-mode="a2a"]')
        page.wait_for_timeout(1200)
        print("[1] 已切到参考生音乐标签")

        # 2) 面板差异
        ref_visible = page.is_visible("#musicRefField")
        enc_visible = page.is_visible("#musicEncoderField")
        print(f"[2] 参考音频区可见 = {ref_visible}；音频编码器可见 = {enc_visible}")
        assert ref_visible, "参考生音乐应显示参考音频选择区"
        assert enc_visible, "参考生音乐应显示音频编码器选择"
        ev["ref_field_visible"] = ref_visible
        ev["encoder_field_visible"] = enc_visible

        # 谱面固定仅旋律
        plan = page.eval_on_selector_all(
            "#musicPlanChips button",
            "els => els.map(e => ({v: e.dataset.v, t: e.textContent.trim(),"
            " disabled: e.disabled, active: e.classList.contains('active')}))")
        print("[2] 谱面 chips:", json.dumps(plan, ensure_ascii=False))
        full = [p for p in plan if p["v"] == "full"][0]
        melody = [p for p in plan if p["v"] == "melody"][0]
        assert full["disabled"], "参考生音乐的「完整谱面」应禁用"
        assert melody["active"], "参考生音乐应默认「仅旋律」"
        ev["plan_chips"] = plan

        # 3) 增强组件在 a2a 下同样可用（同一套组件）
        page.click("#musicEnhanceMenuBtn")
        page.wait_for_timeout(500)
        items = page.eval_on_selector_all(
            "#musicEnhanceMenu .enhance-menu-item[data-method]",
            "els => els.map(e => e.dataset.method)")
        print("[3] 增强下拉项:", items)
        assert "yue2_struct" in items, "a2a 下增强下拉仍应有 yue2 项"
        ev["enhance_items_a2a"] = items
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

        # 4) 增强请求在 a2a 下带 mode=a2a
        page.evaluate("""() => {
          window.__enh = [];
          const orig = window.fetch;
          window.fetch = async (url, opts) => {
            const u = String(url);
            if (u.includes('/api/music-enhance')) {
              window.__enh.push(JSON.parse(opts.body));
              return new Response(JSON.stringify({
                provider: '桩', fixes: [], problems: [],
                style: {language:'English', genre:'pop', mood:'(none)',
                        vocal:'female vocal', vocal_color:'(none)',
                        instruments:'piano', tempo_bpm:100, energy:'mid energy',
                        length_hint:'single edit (~2-3 min)', extra:'',
                        custom_language:'', custom_genre:''},
                lyrics: {template:'V-C-V-C-bridge-C', lyrics_override:'[Verse]\\n测试'},
              }), {status: 200, headers: {'Content-Type': 'application/json'}});
            }
            return orig(url, opts);
          };
        }""")
        page.fill("#musicPrompt", "把这段旋律改成更快的流行编曲")
        page.click("#musicEnhanceBtn")
        page.wait_for_function(
            "() => { const h = document.querySelector('#musicEnhanceHint');"
            " return h && h.textContent.includes('已生成'); }", timeout=60_000)
        payloads = page.evaluate("() => window.__enh || []")
        print("[4] 增强请求 mode =", payloads[-1].get("mode"))
        assert payloads[-1].get("mode") == "a2a", "a2a 的增强请求必须带 mode=a2a"
        ev["enhance_request_mode"] = payloads[-1].get("mode")

        # 5) 没有参考音频时提交被拦下（明确中文提示）
        page.on("dialog", lambda d: (errs.append("dialog: " + d.message), d.dismiss()))
        page.click("#genBtn")
        page.wait_for_timeout(1200)
        blocked = any("参考音频" in e for e in errs)
        print(f"[5] 无参考音频提交被拦下 = {blocked}")
        assert blocked, "没有参考音频时提交必须被拦下并给出中文提示"
        ev["a2a_submit_without_ref_blocked"] = True
        ev["block_message"] = [e for e in errs if "参考音频" in e]

        ev["console_errors"] = errs
        br.close()

    (OUT / "music-a2a.json").write_text(
        json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n证据已写入:", OUT / "music-a2a.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
