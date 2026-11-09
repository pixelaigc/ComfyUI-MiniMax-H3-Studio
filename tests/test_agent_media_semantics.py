# -*- coding: utf-8 -*-
"""
Agent 媒体语义前端层 —— 回归测试。

锁定三条**会误导用户**的展示规则：

1. **模式白名单只有 9 个**
   【事实】workflows.BUILDERS = t2v/flf/r2v/t2i/t2i_qwen/i2i_qwen/t2i_moss/t2a/a2a
   （workflows.py:2234）。longfilm 走 /api/longfilms 自有链路，Agent 建不了 ——
   基线 Agent 曾误报 longfilm，前端不得把它当可下发模式展示。

2. **静默回落必须可见**
   【事实】视频分辨率只有 544p/768p（RES_MP，workflows.py:63），
   其它值 `RES_MP.get(x, 0.8)` 静默落 768p（workflows.py:320/:409）。
   【事实】音乐时长字段是 max_duration（music_yue2.py:328），默认 60，
   Agent 传的 duration 对音乐**不生效**。
   → 任务卡要显示**实际生效值**，并把回落标注出来。

3. **不展示 Agent 传不了的参数**
   【事实】Agent 的 create_job 只写 prompt/aspect/resolution/duration/
   batch_count/ref_images/asset_refs（agent.py:501-556）。
   模型版本/LoRA/采样参数/负面词/seed 是官方默认值 ——
   展示成「用户设的」就是误导。

运行：python3 -m pytest tests/test_agent_media_semantics.py -v
"""
import json
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

WEB = REPO / "web"


class TestAgentMediaStatic(unittest.TestCase):
    """静态契约：文件在、模式清单对、参数白名单对。"""

    def test_media_module_exists(self):
        self.assertTrue((WEB / "agent-media.js").is_file(),
                        "缺少 web/agent-media.js（媒体语义真源）")

    def test_media_module_loaded_before_agent_js(self):
        """加载顺序：agent-media.js 必须在 agent.js 之前。"""
        html = (WEB / "index.html").read_text(encoding="utf-8")
        self.assertIn("agent-media.js", html, "index.html 没引入 agent-media.js")
        self.assertLess(html.index("agent-media.js"), html.index("agent.js?v="),
                        "agent-media.js 必须先于 agent.js 加载"
                        "（agent.js 的任务卡渲染要用 AgentMedia）")

    def test_mode_whitelist_has_exactly_nine_and_no_longfilm(self):
        """白名单 9 个，且不含 longfilm。"""
        src = (WEB / "agent-media.js").read_text(encoding="utf-8")
        for mode in ("t2v", "flf", "r2v", "t2i", "t2i_qwen", "i2i_qwen",
                     "t2i_moss", "t2a", "a2a"):
            self.assertIn(f"{mode}:", src, f"模式白名单缺 {mode}")
        # longfilm 只能出现在 MANUAL_ONLY_MODES 里（标注「Agent 不能下发」）
        self.assertIn("MANUAL_ONLY_MODES", src,
                      "缺少 MANUAL_ONLY_MODES（Agent 建不了的模式）")
        self.assertIn("longfilm", src,
                      "longfilm 必须被显式标记为 Agent 不能下发")

    def test_server_modes_filter_removes_longfilm(self):
        """从 /api/generation-types 拉清单时必须剔除 longfilm。"""
        src = (WEB / "agent-media.js").read_text(encoding="utf-8")
        self.assertRegex(
            src,
            r"if \(MANUAL_ONLY_MODES\[t\.key\]\) return;",
            "resolveServerModes 必须剔除 Agent 建不了的模式"
            "（gen_types 里登记了 longfilm，但它走 /api/longfilms 自有链路）")

    def test_displayable_params_exclude_agent_cannot_set(self):
        """展示白名单不得含 steps/cfg/seed/model/lora/negative_prompt。"""
        src = (WEB / "agent-media.js").read_text(encoding="utf-8")
        m = self._extract_block(src, "DISPLAYABLE_PARAMS")
        self.assertIsNotNone(m, "找不到 DISPLAYABLE_PARAMS")
        for forbidden in ("steps", "cfg", "seed", "model", "lora",
                          "negative_prompt", "sampler", "scheduler"):
            self.assertNotIn(forbidden, m,
                             f"DISPLAYABLE_PARAMS 不该含 {forbidden}"
                             f"（Agent 传不了，展示成用户设的是误导）")

    @staticmethod
    def _extract_block(src: str, name: str) -> str:
        start = src.find(name)
        if start < 0:
            return None
        end = src.find("]", start)
        return src[start:end + 1] if end > 0 else None


@unittest.skipUnless(HAS_PLAYWRIGHT, "未安装 playwright")
class TestAgentMediaBehaviour(unittest.TestCase):
    """
    真实浏览器里跑 AgentMedia，断言**判定结果**。

    为什么用浏览器而不是直接 require 这段 JS：
      Node 没有 DOM，但 AgentMedia 是纯函数模块 —— 本可以直接用 Node 跑。
      这里坚持用浏览器是为了**同一条加载路径**：
      页面里 <script src=agent-media.js> 加载的，和测试里断言的是同一份文件，
      避免「测的是另一份拷贝」。
    """

    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])
        cls.page = cls.browser.new_context().new_page()
        cls.page.set_content("<!DOCTYPE html><html><body></body></html>")
        cls.page.add_script_tag(path=str(WEB / "agent-media.js"))
        cls.page.wait_for_timeout(100)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def media(self, expr):
        return self.page.evaluate("() => { const M = window.AgentMedia; return " + expr + "; }")

    # -------------------------------------------------- 模式白名单
    def test_nine_modes_and_longfilm_marked_manual_only(self):
        modes = self.media("Object.keys(M.AGENT_MODES)")
        self.assertEqual(len(modes), 9, f"模式数不对：{modes}")
        self.assertNotIn("longfilm", modes, "longfilm 不该在 Agent 可下发模式里")
        self.assertIn("longfilm", self.media("Object.keys(M.MANUAL_ONLY_MODES)"))
        # longfilm 的标签必须带「Agent 不能下发」
        label = self.media("M.modeLabel('longfilm')")
        self.assertIn("不能下发", label, f"longfilm 标签没标注限制：{label}")

    def test_server_mode_list_drops_longfilm(self):
        """服务端清单里有 longfilm 时必须被剔除。"""
        payload = {"types": [
            {"key": "t2i", "label": "Z-IMAGE 文生图", "media": "image"},
            {"key": "longfilm", "label": "长片拼接", "media": "video"},
            {"key": "t2v", "label": "文生视频", "media": "video"},
        ]}
        result = self.page.evaluate("(p) => window.AgentMedia.resolveServerModes(p)", payload)
        self.assertNotIn("longfilm", result, f"没剔除 longfilm：{result}")
        self.assertIn("t2i", result)
        self.assertIn("t2v", result)

    # -------------------------------------------------- 视频分辨率回落
    def test_video_resolution_1080p_falls_back_to_768p(self):
        """视频传 1080p -> 实际 768p，且必须标注回落。"""
        res = self.media("M.resolveResolution('t2v', '1080p')")
        self.assertEqual(res["value"], "768p", f"视频分辨率没回落：{res}")
        self.assertEqual(res["requested"], "1080p")
        self.assertTrue(res["fallback"], "回落必须被标记")
        self.assertIn("1080p", res["note"], f"回落说明里没有请求值：{res}")

    def test_video_resolution_544p_is_kept(self):
        res = self.media("M.resolveResolution('t2v', '544p')")
        self.assertEqual(res["value"], "544p")
        self.assertFalse(res["fallback"], "544p 是合法档位，不该标回落")

    def test_video_resolution_default_is_768p(self):
        res = self.media("M.resolveResolution('t2v', '')")
        self.assertEqual(res["value"], "768p")
        self.assertFalse(res["fallback"], "没传是默认值，不是回落")

    # -------------------------------------------------- 音乐时长字段
    def test_music_duration_uses_max_duration_not_duration(self):
        """音乐传 duration 不生效 -> 实际 60 秒，且必须标注。"""
        res = self.media("M.resolveDuration('t2a', {duration: 120})")
        self.assertEqual(res["value"], 60, f"音乐时长没落回默认：{res}")
        self.assertTrue(res["fallback"], "Agent 传 duration 不生效必须被标记")
        self.assertIn("max_duration", res["note"], f"说明里没点明字段名：{res}")

    def test_music_max_duration_is_used_when_given(self):
        res = self.media("M.resolveDuration('t2a', {max_duration: 180})")
        self.assertEqual(res["value"], 180)
        self.assertFalse(res["fallback"], "传了 max_duration 就是它生效，不该标回落")

    def test_music_duration_is_clamped(self):
        """超上限收敛到 360，超下限收敛到 4。"""
        self.assertEqual(self.media("M.resolveDuration('t2a', {max_duration: 999}).value"), 360)
        self.assertEqual(self.media("M.resolveDuration('t2a', {max_duration: 1}).value"), 4)

    def test_video_duration_uses_duration(self):
        """视频的 duration 是有效的（只有音乐不是）。"""
        res = self.media("M.resolveDuration('t2v', {duration: 12})")
        self.assertEqual(res["value"], 12)
        self.assertFalse(res["fallback"], "视频 duration 生效，不该标回落")

    # -------------------------------------------------- 参数展示
    def test_effective_params_shows_actual_values(self):
        params = self.media(
            "M.effectiveParams('t2v', {aspect:'16:9', resolution:'1080p', duration:5})")
        by_key = {p["key"]: p for p in params}
        self.assertEqual(by_key["resolution"]["value"], "768p",
                         f"分辨率展示的不是实际值：{params}")
        self.assertTrue(by_key["resolution"]["fallback"])
        self.assertEqual(by_key["duration"]["value"], "5 秒")
        self.assertEqual(by_key["aspect"]["value"], "16:9")
        self.assertFalse(by_key["aspect"]["fallback"], "比例没传时是默认值，不是回落")

    def test_effective_params_excludes_non_settable(self):
        params = self.media(
            "M.effectiveParams('t2i', {aspect:'16:9', resolution:'1mp', steps:25,"
            " cfg:4.0, seed:7, model:'z_image_bf16', negative_prompt:'nsfw',"
            " lora:'x', sampler:'euler'})")
        keys = {p["key"] for p in params}
        self.assertEqual(keys, {"aspect", "resolution"},
                         f"展示了不该展示的参数：{params}")

    # -------------------------------------------------- 能力缺口
    def test_limitation_for_a2a_and_flf(self):
        """Agent 建不了 a2a / flf —— 必须给替代建议，不能只报错。"""
        for mode in ("a2a", "flf"):
            lim = self.media(f"M.limitationFor('{mode}', {{}})")
            self.assertIsNotNone(lim, f"{mode} 缺能力缺口说明")
            self.assertTrue(lim["reason"], f"{mode} 缺原因说明")
            self.assertTrue(lim["advice"], f"{mode} 缺替代建议")
            # 建议必须指向**可执行的人工路径**（面板 / 换一种模式），
            # 不能只是一句「做不到」。
            self.assertTrue(
                any(k in lim["advice"] for k in ("面板", "请到", "或先用", "改用")),
                f"{mode} 的建议没有可执行的下一步：{lim['advice']}")

    def test_limitation_absent_for_supported_modes(self):
        for mode in ("t2i", "t2v", "r2v", "i2i_qwen", "t2a"):
            lim = self.media(f"M.limitationFor('{mode}', {{}})")
            self.assertIsNone(lim, f"{mode} 不该有能力缺口提示：{lim}")


if __name__ == "__main__":
    unittest.main()
