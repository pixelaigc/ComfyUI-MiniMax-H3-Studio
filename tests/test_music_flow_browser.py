# -*- coding: utf-8 -*-
"""
yue2 音乐面板的**真实浏览器**验证（Playwright + Chromium）。

覆盖用户要求的 6 条：
  1. 文生音乐 / 参考生音乐是**左侧两个独立标签**，面板内**无模式切换**
  2. 音乐描述框 + 「增强」按钮（与生图面板同一套组件）+ 下拉里有 yue2 专用项
  3. 面板内**没有「最长时长」控件**
  5. 增强后产出**结构化风格 + 歌词**，两者可编辑、可「重新生成」
  6. 增强按钮/下拉的 class 与生图面板**完全一致**（同一套组件）

同时在 375 / 390 / 1280 三档宽度截图，并记录 console 报错与失败请求。

运行：python3 -m pytest tests/test_music_flow_browser.py -v
需要：playwright（chromium）。缺失时整类 skip，不算失败。
"""
import json
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:                                   # pragma: no cover
    sync_playwright = None
    HAS_PLAYWRIGHT = False

from test_frontend_defaults import StudioServer   # noqa: E402

# 截图输出目录
SHOTS = Path("/root/.hermes-web-ui/.ekko/workspace/default/"
              "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/screenshots")

# 三档宽度（用户要求至少这三档）
VIEWPORTS = [(375, 812), (390, 844), (1280, 900)]


@unittest.skipUnless(HAS_PLAYWRIGHT, "需要 playwright（chromium）")
class TestMusicFlowBrowser(unittest.TestCase):
    """真实浏览器：两个标签 × 三档宽度 × 增强交互。"""

    @classmethod
    def setUpClass(cls):
        cls.server = StudioServer(seed_jobs=0)
        assert cls.server.start(), "Studio 实例启动失败"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])
        cls.ctx = cls.browser.new_context(viewport={"width": 1280, "height": 900})
        cls.page = cls.ctx.new_page()
        cls.console_errors = []
        cls.failed_requests = []
        cls.page.on("pageerror", lambda e: cls.console_errors.append("pageerror: " + str(e)))
        cls.page.on("console",
                    lambda m: cls.console_errors.append(m.text) if m.type == "error" else None)
        cls.page.on("requestfailed",
                    lambda r: cls.failed_requests.append(
                        f"{r.method} {r.url} -> {r.failure}"))
        cls.page.on("response",
                    lambda r: cls.failed_requests.append(
                        f"HTTP {r.status} {r.request.method} {r.url}")
                    if r.status >= 400 else None)
        cls.base = f"http://127.0.0.1:{cls.server.port}/"
        cls.page.goto(cls.base, wait_until="domcontentloaded")
        cls.page.wait_for_timeout(2500)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.stop()

    # ---------------------------------------------------------------- 工具
    def reset(self):
        self.page.goto(self.base, wait_until="domcontentloaded")
        self.page.wait_for_timeout(1800)
        self.page.evaluate(
            "() => localStorage.removeItem('h3studio.musicParams')")
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_timeout(1500)

    # 断点（responsive.css）：≤1100px 参数面板收进底部抽屉
    DRAWER_BP = 1100

    def open_sidebar(self):
        """
        窄屏：侧栏是抽屉，先点汉堡把它滑进来。

        【要点】参数面板抽屉打开时，#panelMask 会盖住整屏拦截点击
        （实测 playwright 报 "panelMask intercepts pointer events"）。
        所以先关掉参数抽屉，再开侧栏。
        """
        self.close_param_panel()
        if self.page.viewport_size["width"] >= 900:
            return
        in_view = self.page.evaluate(
            "() => { const s = document.querySelector('#sidebar');"
            " return s ? s.getBoundingClientRect().x >= 0 : false; }")
        if not in_view:
            self.page.click("#navToggle")
            self.page.wait_for_timeout(600)

    def open_param_panel(self):
        """窄屏：参数面板是底部抽屉（#panelFab 浮动按钮），点开它。"""
        if self.page.viewport_size["width"] > self.DRAWER_BP:
            return
        fab = self.page.query_selector("#panelFab")
        if fab and fab.is_visible():
            fab.click()
            self.page.wait_for_timeout(700)

    def close_param_panel(self):
        """关掉参数抽屉（点遮罩）。窄屏下遮罩会拦截所有点击。"""
        self.page.evaluate(
            "() => { const m = document.querySelector('#panelMask');"
            " if (m && m.classList.contains('open')) m.click(); }")
        self.page.wait_for_timeout(400)

    def switch_mode(self, mode):
        """点左侧标签切换模式（窄屏要先滑出侧栏）。"""
        self.open_sidebar()
        self.page.click(f'.nav-item[data-mode="{mode}"]')
        self.page.wait_for_timeout(900)

    # ---------------------------------------------------------------- 用例

    def test_two_independent_left_tabs(self):
        """【要求 1】文生音乐 / 参考生音乐是左侧两个独立标签。"""
        self.reset()
        for mode, label in (("t2a", "文生音乐"), ("a2a", "参考生音乐")):
            el = self.page.query_selector(f'.nav-item[data-mode="{mode}"]')
            self.assertIsNotNone(el, f"左侧缺少 {mode} 标签")
            self.assertIn(label, el.inner_text())
        # 点标签能切到对应面板
        self.switch_mode("t2a")
        self.assertTrue(self.page.is_visible("#musicPanel"))
        self.switch_mode("a2a")
        self.assertTrue(self.page.is_visible("#musicPanel"))

    def test_no_mode_switch_inside_music_panel(self):
        """【要求 1】面板内**没有**任何模式切换控件。"""
        self.reset()
        self.switch_mode("t2a")
        self.assertEqual(
            self.page.evaluate(
                "() => document.querySelectorAll('#musicPanel .chips[id*=Mode]').length"),
            0, "音乐面板里不该再有模式切换 chips")
        self.assertEqual(
            self.page.evaluate(
                "() => document.querySelectorAll('#musicModeChips').length"),
            0, "音乐面板里不该再有 #musicModeChips")
        # 面板里也不该有 select 形式的模式下拉
        labels = self.page.eval_on_selector_all(
            "#musicPanel .field-label", "els => els.map(e => e.textContent.trim())")
        self.assertNotIn("生成模式", labels, "面板里不该再有「生成模式」下拉")

    def test_no_duration_control(self):
        """【要求 3】面板里没有「最长时长」。"""
        self.reset()
        self.switch_mode("t2a")
        self.assertEqual(
            self.page.evaluate(
                "() => document.querySelectorAll('#musicDuration').length"),
            0, "音乐面板里不该再有 #musicDuration")
        body = self.page.inner_text("#musicPanel")
        self.assertNotIn("最长时长", body, "面板里不该再出现「最长时长」")

    def test_enhance_uses_same_component_as_image_panel(self):
        """
        【要求 6】增强按钮/下拉与生图面板**同一套组件**（class 完全一致）。

        注意：要在**图像模式**下取生图面板的对照 —— 音乐模式下
        #promptField 是隐藏的（提示词框搬进了音乐面板），
        隐藏容器里的元素仍可被 querySelectorAll 找到，但
        #enhanceSplit 的父级 hidden 不影响计数 —— 这里直接用
        「两边都有同样的 class」来判定「同一套组件」。
        """
        self.reset()
        # 先切到图像模式取生图面板的对照组
        self.switch_mode("t2i")
        # 注意：#enhanceSplit **自己**就是 .enhance-split 元素，
        # 所以要用「自身或后代」的匹配，不能写成后代选择器。
        for cls_name in ("enhance-split", "enhance-btn", "enhance-menu"):
            img = self.page.evaluate(
                "cls => document.querySelectorAll("
                "'#enhanceSplit.' + cls + ', #enhanceSplit .' + cls).length",
                cls_name)
            self.assertGreater(img, 0, f"生图面板没有 .{cls_name}")
        # 再切到音乐模式取音乐面板的对照组
        self.switch_mode("t2a")
        for cls_name in ("enhance-split", "enhance-btn", "enhance-menu"):
            music = self.page.evaluate(
                "cls => document.querySelectorAll("
                "'#musicEnhanceSplit.' + cls + ', #musicEnhanceSplit .' + cls).length",
                cls_name)
            self.assertGreater(music, 0, f"音乐面板没有 .{cls_name}（应复用同一套组件）")
        # 结构一致：主按钮 + 箭头 + 菜单容器
        self.assertEqual(
            self.page.evaluate(
                "() => document.querySelectorAll('#enhanceSplit > *').length"),
            self.page.evaluate(
                "() => document.querySelectorAll('#musicEnhanceSplit > *').length"),
            "两个面板的增强组件结构应完全一致")

    def test_music_enhance_menu_has_yue2_item(self):
        """【要求 2】下拉里有 yue2 专用增强项，且用的是同一套菜单项 class。"""
        self.reset()
        self.switch_mode("t2a")
        self.open_param_panel()
        self.page.click("#musicEnhanceMenuBtn")
        self.page.wait_for_timeout(500)
        items = self.page.eval_on_selector_all(
            "#musicEnhanceMenu .enhance-menu-item[data-method]",
            "els => els.map(e => ({id: e.dataset.method,"
            " label: e.querySelector('.enhance-menu-label').textContent.trim(),"
            " checked: !!(e.querySelector('input[data-method-check]')||{}).checked}))")
        self.assertTrue(items, "音乐增强下拉应有选项")
        self.assertTrue(any("YuE2" in i["label"] for i in items),
                        f"下拉里应有 yue2 专用增强项，实际: {items}")
        self.assertTrue(any(i["checked"] for i in items),
                        "默认应选中一项")
        # 菜单项用的是生图面板同一套 class
        self.assertTrue(self.page.is_visible("#musicEnhanceMenu .enhance-menu-check"))
        self.page.keyboard.press("Escape")

    def test_enhance_produces_structured_style_and_lyrics(self):
        """
        【要求 2 / 5】点「增强」-> 产出结构化风格 + 歌词两份结果，
        两者都可编辑，且都有「重新生成」入口。
        """
        self.reset()
        self.switch_mode("t2a")
        # 用路由桩返回可验证的产物
        self.page.evaluate("""() => {
          window.__calls = [];
          const orig = window.fetch;
          window.fetch = async (url, opts) => {
            const u = String(url);
            window.__calls.push(u);
            if (u.includes('/api/music-enhance')) {
              return new Response(JSON.stringify({
                provider: '桩', mode: 't2a', fixes: [], problems: [],
                style: {
                  language: 'Japanese', genre: 'piano pop', mood: 'bittersweet',
                  vocal: 'female vocal', vocal_color: 'breathy',
                  instruments: 'acoustic piano, sustained strings',
                  tempo_bpm: 76, energy: 'mid energy',
                  length_hint: 'full song (~3.5-5 min)',
                  extra: '雨夜告别', custom_language: '', custom_genre: '',
                },
                lyrics: {
                  template: 'V-C-V-C-bridge-C',
                  lyrics_override: '[Verse]\\n雨还在下\\n\\n[Chorus]\\n再见',
                },
                style_text: 'Japanese, piano pop, ...',
                lyrics_text: '[Verse]\\n雨还在下\\n\\n[Chorus]\\n再见',
              }), {status: 200, headers: {'Content-Type': 'application/json'}});
            }
            return orig(url, opts);
          };
        }""")
        self.open_param_panel()
        self.page.fill("#musicPrompt", "一首关于雨夜告别的日语慢歌")
        self.page.click("#musicEnhanceBtn")
        # 等提示栏出现「已生成」—— 不能等 #musicSF_language 非空：
        # 那个下拉一上来就有默认值（English），增强还没回来就已经满足。
        self.page.wait_for_function(
            "() => { const h = document.querySelector('#musicEnhanceHint');"
            " return h && h.textContent.includes('已生成'); }",
            timeout=60_000)
        self.page.wait_for_timeout(600)

        calls = self.page.evaluate("() => window.__calls || []")
        self.assertTrue(any("/api/music-enhance" in c for c in calls),
                        "点增强必须调用 /api/music-enhance")

        # 结构化风格：字段区可见且被填上
        self.assertTrue(self.page.is_visible("#musicStyleFields"),
                        "增强后结构化风格区应展开")
        self.assertEqual(
            self.page.input_value("#musicSF_language"), "Japanese")
        self.assertEqual(
            self.page.input_value("#musicSF_genre"), "piano pop")
        self.assertEqual(
            self.page.input_value("#musicSF_tempo_bpm"), "76")
        # 拼接预览
        preview = self.page.inner_text("#musicStyleFieldsPreview")
        self.assertIn("Japanese", preview)
        self.assertIn("76 BPM", preview)

        # 歌词：已回填
        lyrics = self.page.input_value("#musicLyrics")
        self.assertIn("[Verse]", lyrics)
        self.assertIn("雨还在下", lyrics)

        # 两个「重新生成」入口都在
        self.assertTrue(self.page.is_visible("#musicStyleRegenBtn"))
        self.assertTrue(self.page.is_visible("#musicLyricsRegenBtn"))

    def test_structured_fields_are_editable(self):
        """【要求 5】结构化风格的字段可逐项编辑，改动进入提交参数。"""
        self.reset()
        self.switch_mode("t2a")
        self.open_param_panel()
        self.page.click("#musicStyleFieldsToggle")
        self.page.wait_for_timeout(400)
        self.page.select_option("#musicSF_genre", "jazz")
        self.page.wait_for_timeout(400)
        params = self.page.evaluate("() => window.MusicUI.collect().params")
        self.assertEqual(params.get("style_genre"), "jazz",
                         "改过的字段必须进入提交参数")

    def test_regen_uses_current_content_as_base(self):
        """【要求 5】「重新生成」= 以当前内容为底稿（请求里带 base + 现有内容）。"""
        self.reset()
        self.switch_mode("t2a")
        self.page.evaluate("""() => {
          window.__payloads = [];
          const orig = window.fetch;
          window.fetch = async (url, opts) => {
            const u = String(url);
            if (u.includes('/api/music-enhance')) {
              window.__payloads.push(JSON.parse(opts.body));
              return new Response(JSON.stringify({
                provider: '桩', fixes: [], problems: [],
                style: {language:'English', genre:'pop', mood:'(none)',
                        vocal:'female vocal', vocal_color:'(none)',
                        instruments:'piano', tempo_bpm:90, energy:'mid energy',
                        length_hint:'single edit (~2-3 min)', extra:'',
                        custom_language:'', custom_genre:''},
                lyrics: {template:'V-C-V-C-bridge-C', lyrics_override:'[Verse]\\n新'},
              }), {status: 200, headers: {'Content-Type': 'application/json'}});
            }
            return orig(url, opts);
          };
        }""")
        # 先填一点现有内容
        self.open_param_panel()
        self.page.fill("#musicLyrics", "[Verse]\n旧内容")
        self.page.click("#musicStyleFieldsToggle")
        self.page.wait_for_timeout(300)
        self.page.select_option("#musicSF_genre", "jazz")
        self.page.wait_for_timeout(300)

        self.page.click("#musicLyricsRegenBtn")
        self.page.wait_for_timeout(2000)
        payloads = self.page.evaluate("() => window.__payloads || []")
        self.assertTrue(payloads, "「重新生成」应发起请求")
        p = payloads[-1]
        self.assertEqual(p.get("base"), "当前内容",
                         "「重新生成」必须带 base（以当前内容为底稿）")
        self.assertIn("旧内容", json.dumps(p, ensure_ascii=False),
                      "「重新生成」必须把当前歌词作为上下文")
        self.assertEqual(p.get("style", {}).get("genre"), "jazz",
                         "「重新生成」必须把当前结构化风格作为上下文")

    def test_enhance_request_carries_live_mode(self):
        """
        增强请求里的 mode 必须是**当前生效**的模式。

        【回归】曾经踩过的坑：请求体用 `music.getState().mode` ——
        那是**持久化**的那份（localStorage 里的 st.mode），切了左侧标签
        不一定跟着变。实测在「参考生音乐」面板下发出的是 mode=t2a，
        后端就会按文生音乐的措辞去生成（参考音频场景的提示语丢失）。
        """
        self.reset()
        for mode in ("t2a", "a2a"):
            self.switch_mode(mode)
            self.open_param_panel()
            self.page.evaluate("""() => {
              window.__modes = [];
              const orig = window.fetch;
              window.fetch = async (url, opts) => {
                const u = String(url);
                if (u.includes('/api/music-enhance')) {
                  window.__modes.push(JSON.parse(opts.body).mode);
                  return new Response(JSON.stringify({
                    provider: '桩', fixes: [], problems: [],
                    style: {language:'English', genre:'pop', mood:'(none)',
                            vocal:'female vocal', vocal_color:'(none)',
                            instruments:'piano', tempo_bpm:90, energy:'mid energy',
                            length_hint:'single edit (~2-3 min)', extra:'',
                            custom_language:'', custom_genre:''},
                    lyrics: {template:'V-C-V-C-bridge-C',
                              lyrics_override:'[Verse]' + String.fromCharCode(10) + 'X'},
                  }), {status: 200, headers: {'Content-Type': 'application/json'}});
                }
                return orig(url, opts);
              };
            }""")
            self.page.fill("#musicPrompt", "测试描述")
            self.page.click("#musicEnhanceBtn")
            self.page.wait_for_function(
                "() => { const h = document.querySelector('#musicEnhanceHint');"
                " return h && h.textContent.includes('已生成'); }",
                timeout=60_000)
            modes = self.page.evaluate("() => window.__modes || []")
            self.assertTrue(modes, f"{mode} 下增强请求没发出")
            self.assertEqual(modes[-1], mode,
                             f"在 {mode} 面板下，增强请求的 mode 应为 {mode}")

    def test_screenshots_at_three_widths(self):
        """【交付要求】两个标签 × 三档宽度截图，并记录 console / 失败请求。"""
        self.reset()
        SHOTS.mkdir(parents=True, exist_ok=True)
        report = []
        for mode, label in (("t2a", "文生音乐"), ("a2a", "参考生音乐")):
            for w, h in VIEWPORTS:
                # 顺序很重要：先切**宽度**再切模式。
                # 反过来的话，窄屏下侧栏是抽屉，标签点不到。
                self.page.set_viewport_size({"width": w, "height": h})
                self.page.wait_for_timeout(400)
                self.switch_mode(mode)
                self.open_param_panel()
                self.page.wait_for_timeout(800)
                path = SHOTS / f"music-{mode}-{w}x{h}.png"
                self.page.screenshot(path=str(path), full_page=True)
                self.assertTrue(path.exists() and path.stat().st_size > 0,
                                f"截图没生成: {path}")
                report.append(str(path))
        # console / 失败请求（截图全程）
        self.assertEqual(self.console_errors, [],
                         f"console 报错: {self.console_errors}")
        self.assertEqual(self.failed_requests, [],
                         f"失败请求: {self.failed_requests}")
        # 写到证据目录
        ev = SHOTS.parent
        (ev / "browser-console-and-network.json").write_text(
            json.dumps({
                "console_errors": self.console_errors,
                "failed_requests": self.failed_requests,
                "screenshots": report,
                "viewports": [{"width": w, "height": h} for w, h in VIEWPORTS],
            }, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
