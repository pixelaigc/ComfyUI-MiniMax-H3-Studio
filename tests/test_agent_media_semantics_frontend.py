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
    def test_limitation_absent_for_a2a_and_flf_after_slot_dispatch(self):
        """
        a2a / flf **不再**有「Agent 建不了」的能力缺口提示。

        【为什么改这条断言 —— 2026-11-10 审查修复】
        旧版本这里断言「a2a / flf 必须有能力缺口说明」，
        那条断言**把错误行为锁死了**：
        前端硬编码 UNSUPPORTED_BY_AGENT = {a2a, flf}，理由是
        「Agent 的素材槽位恒为 ref_image（agent.py:534 固定写死）」。
        那条在旧基线上成立，但后端增强分支已把槽位改成分派制
        （agent_schema.MODE_SLOTS / mode_slot_plan）：
            a2a -> ref_audio
            flf -> first_frame（+ last_frame）
        合并后前端那条提示变成**反向阻断** ——
        后端明明能建，任务卡上却写「Agent 建不了，请到面板手动提交」。

        现在能力位**由后端下发**（GET /api/generation-types 的
        agent_asset_slots），前端只渲染服务端给的结论。
        """
        for mode in ("a2a", "flf"):
            lim = self.media(f"M.limitationFor('{mode}', {{}})")
            self.assertIsNone(lim,
                              f"{mode} 不该有能力缺口提示（后端已支持分派槽位）：{lim}")

    def test_agent_capability_bits_come_from_server(self):
        """
        服务端下发槽位计划后，前端按服务端结论判定能力缺口。

        - a2a 下发 ref_audio 通道 -> 有通道 -> 不提示
        - 某模式下发「无通道」但任务带了素材引用 -> 提示（引用放不进去）
        - 服务端没下发 -> 不提示（宁可少提示，也不要反向阻断）
        """
        payload = {
            "types": [
                {"key": "a2a", "label": "参考生音乐", "media": "audio",
                 "agent_asset_slots": {"single": ["ref_audio"], "multi": [],
                                       "max_per_slot": {"ref_audio": 1}}},
                {"key": "flf", "label": "首尾帧生成", "media": "video",
                 "agent_asset_slots": {"single": ["first_frame", "last_frame"],
                                       "multi": [],
                                       "max_per_slot": {"first_frame": 1,
                                                        "last_frame": 1}}},
            ]
        }
        self.page.evaluate("(p) => window.AgentMedia.applyServerAgentSlots(p)", payload)
        for mode in ("a2a", "flf"):
            lim = self.media(f"M.limitationFor('{mode}', {{}})")
            self.assertIsNone(lim, f"{mode} 有服务端下发的素材通道，不该提示：{lim}")
            slots = self.media(f"M.agentSlotsFor('{mode}')")
            self.assertIsNotNone(slots, f"{mode} 的槽位计划没被接住")
        # 服务端说「没有通道」但任务带了素材 -> 提示
        self.page.evaluate("""(p) => {
          window.AgentMedia.applyServerAgentSlots({types:[
            {key:'t2a', label:'文生音乐', media:'audio',
             agent_asset_slots:{single:[], multi:[], max_per_slot:{}}}
          ]});
        }""")
        lim = self.media("M.limitationFor('t2a', {asset_refs:[{asset_id:'a1'}]})")
        self.assertIsNotNone(lim, "服务端说没有通道但带了素材引用，必须提示")
        self.assertTrue(lim["reason"], "提示必须说明原因")
        self.assertTrue(lim["advice"], "提示必须给出可执行的下一步")

    def test_limitation_absent_for_supported_modes(self):
        for mode in ("t2i", "t2v", "r2v", "i2i_qwen", "t2a"):
            lim = self.media(f"M.limitationFor('{mode}', {{}})")
            self.assertIsNone(lim, f"{mode} 不该有能力缺口提示：{lim}")

    # -------------------------------------------------- 音乐卡不渲染 aspect
    def test_music_card_has_no_aspect_param(self):
        """
        音乐模式（t2a / a2a）是纯音频任务，**没有画面比例**。

        【为什么必须断言 —— 2026-11-10 审查修复】
        旧实现的 resolveAspect 没有音频守卫，无条件返回 "16:9"，
        于是参考生音乐的任务卡上渲染出「比例 16:9」这种无意义参数
        （后端 schema 里音乐模式压根没有 aspect 这个参数）。
        """
        for mode in ("t2a", "a2a"):
            res = self.media(f"M.resolveAspect('{mode}', '')")
            self.assertEqual(res["value"], "",
                             f"{mode} 不该有画面比例：{res}")
            params = self.media(f"M.effectiveParams('{mode}', {{aspect:'16:9'}})")
            keys = {p["key"] for p in params}
            self.assertNotIn("aspect", keys,
                             f"{mode} 的任务卡不该渲染比例：{params}")
            # 但时长必须照常渲染（音乐的真实参数是 max_duration）
            self.assertIn("duration", keys, f"{mode} 该渲染时长：{params}")

    def test_video_card_still_has_aspect(self):
        """视频/图像的比例照常渲染（别把守卫做过头）。"""
        params = self.media(
            "M.effectiveParams('t2v', {aspect:'16:9', duration:8})")
        keys = {p["key"] for p in params}
        self.assertIn("aspect", keys, f"视频该渲染比例：{params}")
        self.assertIn("duration", keys, f"视频该渲染时长：{params}")


if __name__ == "__main__":
    unittest.main()
