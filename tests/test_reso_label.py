# -*- coding: utf-8 -*-
"""
生成库卡片「分辨率 / 画面比例」标签的回归测试。

【这个测试锁住的是什么】
2026-11-11 用户在 8190 实测报出的缺陷：

  左侧文生视频面板：尺寸预设 = 768p · 1344×768；输出尺寸 = 1344×768；
  画面比例 16:9；提交参数 768p·1344×768；时长 15s。
  但右侧生成库卡片显示「分辨率 544p · 16:9」。

  **544p 不是 1344×768 的任何合法档位** —— 这不是「显示得不准」，
  而是「显示了一个与产物毫无关系的数」。

【真实根因（已用 8190 实机数据证实）】
  * 文生视频新面板只提交 width/height，**不写 resolution**；
  * 任务记录里的 params.resolution 因此保留了前端全局 state 的
    **遗留默认值 "544p"**（web/app.js 的 `state.res`）；
  * 卡片渲染读的就是 `p.resolution`（web/app.js 的 jobCard），
    于是显示 "544p · 16:9"。

  实机证据：任务 d2264172d2a6 的 params 是
    {"resolution": "544p", "width": 1344, "height": 768, "aspect": "16:9"}
  而 ffprobe 那个产物 v-h3-studio-000008.mp4 的真实分辨率是 1344×768。

【修复后的契约】
  1. 卡片分辨率 = **实际输出像素**优先，档位与比例都由实际像素反算；
  2. 1344×768 -> "768p · 1344×768 · 16:9"；
  3. 请求值 != 实际值时，必须显式标注差异；
  4. 各面板 / 各任务类型（文生视频 / 文生图 / Qwen / MOSS / 音乐）
     走**同一套**计算；
  5. 视频面板自己列出的官方预设，标签必须与卡片算出来的一致。

运行：python3 -m pytest tests/test_reso_label.py -v
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from h3studio import reso_label as RL          # noqa: E402
from h3studio import video_models as VM        # noqa: E402


# ==================================================================
# 1. 核心用例：1344×768 -> 768p · 16:9（用户报出的那个缺陷）
# ==================================================================
class TestCoreResolutionLabel(unittest.TestCase):
    """用户报出的缺陷本身 + 它的直接近邻"""

    def test_1344x768_is_768p_16_9(self):
        """1344×768 必须显示 768p · 16:9，绝不是 544p。"""
        params = {"resolution": "544p", "width": 1344, "height": 768,
                  "aspect": "16:9"}
        info = RL.describe(params, {"width": 1344, "height": 768})
        self.assertEqual(info["tier"], "768p")
        self.assertEqual(info["aspect"], "16:9")
        self.assertEqual(info["pixels"], "1344×768")
        self.assertEqual(info["label"], "768p · 1344×768 · 16:9")
        self.assertEqual(info["source"], "actual")

    def test_stale_544p_tier_never_leaks_into_label(self):
        """
        回归锁：params.resolution 是遗留值 "544p" 时，
        卡片**绝不能**把 "544p" 显示出来（只要有真实像素）。
        """
        params = {"resolution": "544p", "width": 1344, "height": 768,
                  "aspect": "16:9"}
        info = RL.describe(params, {"width": 1344, "height": 768})
        self.assertNotIn("544p", info["label"])
        self.assertNotIn("544p", info["tier"])

    def test_request_only_still_ignores_stale_tier(self):
        """没有产物实际像素时，也不能用遗留的 544p —— 用请求宽高算。"""
        params = {"resolution": "544p", "width": 1344, "height": 768,
                  "aspect": "16:9"}
        info = RL.describe(params, None)
        self.assertEqual(info["source"], "request")
        self.assertEqual(info["tier"], "768p")
        self.assertNotIn("544p", info["label"])

    def test_tier_is_short_edge_verbatim(self):
        """
        档位名 = 短边像素，**原样用**。
        官方预设的短边 352/416/480/544/608/640/672/736/768 都不是
        10 的倍数 —— 四舍五入会让 768 变成 770，与面板预设对不上。
        """
        self.assertEqual(RL.tier_for_pixels(1344, 768), "768p")
        self.assertEqual(RL.tier_for_pixels(768, 1344), "768p")
        self.assertEqual(RL.tier_for_pixels(960, 544), "544p")
        self.assertEqual(RL.tier_for_pixels(608, 352), "352p")
        self.assertEqual(RL.tier_for_pixels(736, 416), "416p")


# ==================================================================
# 2. 非 16:9 / 竖图 / 极端比例
# ==================================================================
class TestAspectVariants(unittest.TestCase):
    """比例边界：竖图、方图、超宽、非标准比例"""

    def test_portrait_9x16(self):
        info = RL.describe({"width": 768, "height": 1344, "aspect": "9:16"},
                           {"width": 768, "height": 1344})
        self.assertEqual(info["tier"], "768p")
        self.assertEqual(info["aspect"], "9:16")
        self.assertEqual(info["label"], "768p · 768×1344 · 9:16")

    def test_vertical_2x9_ratio_is_not_a_known_aspect(self):
        """
        竖图 2:9 —— 不在受支持比例里（面板只有 1:1…21:9 这 8 种）。
        卡片必须给出一个**诚实**的标签，而不是硬套一个不对的比例。
        """
        info = RL.describe({"width": 768, "height": 3456, "aspect": ""},
                           {"width": 768, "height": 3456})
        self.assertEqual(info["aspect"], "2:9")
        self.assertEqual(info["tier"], "768p")
        self.assertIn("768×3456", info["label"])

    def test_square(self):
        info = RL.describe({"width": 1024, "height": 1024, "aspect": "1:1"},
                           {"width": 1024, "height": 1024})
        self.assertEqual(info["aspect"], "1:1")
        self.assertEqual(info["tier"], "1024p")

    def test_ultrawide_21x9(self):
        info = RL.describe({"width": 1568, "height": 672, "aspect": "21:9"},
                           {"width": 1568, "height": 672})
        self.assertEqual(info["aspect"], "21:9")
        self.assertEqual(info["tier"], "672p")

    def test_non_16_9_4x3(self):
        info = RL.describe({"width": 640, "height": 480, "aspect": "4:3"},
                           {"width": 640, "height": 480})
        self.assertEqual(info["aspect"], "4:3")
        self.assertEqual(info["tier"], "480p")

    def test_arbitrary_ratio_falls_back_to_reduced_fraction(self):
        """既不是已知比例、也不整除 -> 约分成最简整数比，不编造。"""
        info = RL.describe({"width": 1000, "height": 700, "aspect": ""},
                           {"width": 1000, "height": 700})
        self.assertEqual(info["aspect"], "10:7")


# ==================================================================
# 3. 请求值 vs 实际输出值（需求第 4 条）
# ==================================================================
class TestRequestedVsActual(unittest.TestCase):
    """请求尺寸 != 实际输出尺寸时的行为"""

    def test_actual_wins_over_request(self):
        """实际输出与请求不同 -> 卡片显示实际的，并标注差异。"""
        params = {"width": 2560, "height": 1400, "aspect": "16:9",
                  "resolution": "1mp"}
        info = RL.describe(params, {"width": 2560, "height": 1408})
        self.assertEqual(info["width"], 2560)
        self.assertEqual(info["height"], 1408)
        self.assertEqual(info["source"], "actual")
        self.assertIn("2560×1408", info["label"])
        # 必须说明与请求值的差异
        self.assertTrue(info["note"], "请求值与实际值不同却没有任何标注")
        self.assertIn("2560×1400", info["note"])
        self.assertIn("2560×1408", info["note"])

    def test_no_difference_no_note(self):
        """请求值 == 实际值 -> 不该有多余的差异提示。"""
        params = {"width": 1344, "height": 768, "aspect": "16:9"}
        info = RL.describe(params, {"width": 1344, "height": 768})
        self.assertEqual(info["note"], "")

    def test_edit_mode_output_may_differ_entirely(self):
        """
        图片编辑（i2i_qwen）实测：请求 1360×768 出了 2048×2048。
        这种情况下卡片必须显示实际值 + 明确差异。
        """
        params = {"width": 1360, "height": 768, "aspect": "16:9",
                  "resolution": "1mp"}
        info = RL.describe(params, {"width": 2048, "height": 2048})
        self.assertEqual(info["width"], 2048)
        self.assertEqual(info["height"], 2048)
        self.assertEqual(info["aspect"], "1:1")
        self.assertTrue(info["note"])


# ==================================================================
# 4. 缺数据时的诚实降级
# ==================================================================
class TestMissingData(unittest.TestCase):
    """没有尺寸可依据时，不许编造"""

    def test_no_size_at_all_keeps_raw_tier(self):
        """只有档位字符串（老任务）-> 原样带出，不编造像素。"""
        info = RL.describe({"resolution": "1mp", "aspect": "16:9"}, None)
        self.assertFalse(info["has_size"])
        self.assertIsNone(info["width"])
        self.assertIsNone(info["height"])
        self.assertEqual(info["tier"], "1mp")
        self.assertEqual(info["aspect"], "16:9")
        self.assertEqual(info["label"], "1mp · 16:9")

    def test_invalid_width_height_ignored(self):
        """非法宽高（0 / None / 字符串）不能被当成真实尺寸。"""
        for bad in (0, None, "", "abc", -5, True):
            info = RL.describe({"width": bad, "height": bad}, None)
            self.assertFalse(info["has_size"],
                             f"非法宽高 {bad!r} 被当成了真实尺寸")

    def test_partial_size_not_used(self):
        """只有宽没有高 -> 不算尺寸（半截数字比没有数字更误导）。"""
        info = RL.describe({"width": 1344}, None)
        self.assertFalse(info["has_size"])


# ==================================================================
# 5. 与视频面板官方预设的一致性
# ==================================================================
class TestMatchesVideoPanelPresets(unittest.TestCase):
    """
    卡片算出来的标签，必须与**视频面板自己列出的预设**一致。

    否则用户会看到：面板说「768p · 1344×768」，卡片说「1340p · 1344×768」
    —— 两边对不上，看起来就是系统算错了。
    """

    def test_all_official_presets_match(self):
        mismatch = []
        total = 0
        for aspect in ("16:9", "9:16", "21:9", "1:1", "4:3", "3:2", "2:3", "3:4"):
            for preset in VM.size_presets_for_aspect(aspect):
                total += 1
                tier = RL.tier_for_pixels(preset["width"], preset["height"])
                got = RL.aspect_for_pixels(preset["width"], preset["height"],
                                           preferred=aspect)
                panel_tier = preset["label"].split(" · ")[0]
                if tier != panel_tier or got != aspect:
                    mismatch.append(
                        f"{aspect} {preset['label']} -> card {tier} {got}")
        self.assertEqual(mismatch, [],
                         f"卡片标签与面板预设对不上（{total} 项）：\n"
                         + "\n".join(mismatch))
        self.assertGreater(total, 40, "预设清单异常少，测试可能没跑到东西")


# ==================================================================
# 6. 各任务类型走同一套计算（需求第 3 条）
# ==================================================================
class TestAllTaskTypes(unittest.TestCase):
    """
    文生视频 / 文生图 / Qwen / MOSS / 音乐 —— 同一套规则。

    音乐没有画面尺寸 -> 不该出现「分辨率」行。
    """

    def test_video_mode(self):
        info = RL.describe({"width": 1344, "height": 768, "aspect": "16:9"},
                           {"width": 1344, "height": 768})
        self.assertEqual(info["label"], "768p · 1344×768 · 16:9")

    def test_zimage_mode(self):
        info = RL.describe({"width": 1360, "height": 768, "aspect": "16:9",
                            "resolution": "1mp"},
                           {"width": 1360, "height": 768})
        self.assertEqual(info["aspect"], "16:9")
        self.assertIn("1360×768", info["label"])

    def test_qwen_mode(self):
        info = RL.describe({"width": 1360, "height": 768, "aspect": "16:9",
                            "resolution": "1mp"},
                           {"width": 1360, "height": 768})
        self.assertIn("1360×768", info["label"])
        # 1mp 这个档位字符串不能出现在标签里（档位与像素是两条语义）
        self.assertNotIn("1mp", info["label"])

    def test_moss_mode(self):
        info = RL.describe({"width": 3136, "height": 1344, "aspect": "21:9",
                            "resolution": "2k"},
                           {"width": 3136, "height": 1344})
        self.assertEqual(info["aspect"], "21:9")
        self.assertIn("3136×1344", info["label"])
        self.assertNotIn("2k", info["label"])

    def test_audio_mode_has_no_size(self):
        """音乐任务没有画面尺寸 -> 不编造。"""
        info = RL.describe({"prompt": "一段旋律", "duration": 60}, None)
        self.assertFalse(info["has_size"])
        self.assertEqual(info["label"], "")


# ==================================================================
# 7. 服务端接线（真实代码，不是复制品）
# ==================================================================
class TestServerWiring(unittest.TestCase):
    """服务端必须真的把标签下发到 /api/jobs"""

    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server

    def test_row_to_job_includes_resolution_info(self):
        """row_to_job 必须带 resolution_info。"""
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, created_at REAL, mode TEXT,"
            " params TEXT, status TEXT, progress REAL, progress_text TEXT,"
            " error TEXT, outputs TEXT, started_at REAL, finished_at REAL)")
        conn.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("j1", 1.0, "t2v",
             json.dumps({"resolution": "544p", "width": 1344, "height": 768,
                         "aspect": "16:9"}),
             "done", 1.0, "", None,
             json.dumps([{"kind": "video", "file": "videos/x.mp4",
                          "name": "x.mp4", "width": 1344, "height": 768}]),
             1.0, 2.0))
        row = conn.execute("SELECT * FROM jobs WHERE id='j1'").fetchone()
        job = self.server.row_to_job(row)
        self.assertIn("resolution_info", job)
        info = job["resolution_info"]
        self.assertEqual(info["label"], "768p · 1344×768 · 16:9")
        self.assertNotIn("544p", info["label"])

    def test_list_job_records_carries_resolution_info(self):
        """Agent 的任务列表也要带上（否则 Agent 卡片仍是旧值）。"""
        import inspect
        src = inspect.getsource(self.server.list_job_records)
        self.assertIn("resolution_info", src,
                      "list_job_records 没带 resolution_info —— "
                      "Agent 任务卡会继续显示遗留的 544p")

    def test_normalize_t2v_params_writes_real_tier(self):
        """
        文生视频归一后，params.resolution 必须被写成真实档位。

        这是「从源头止血」：参数本身不再说谎。
        """
        params = {"width": 1344, "height": 768, "aspect": "16:9",
                  "resolution": "544p", "model_key": "", "duration": 15,
                  "steps": 6, "seed": None}
        out, error = self.server.normalize_t2v_params(params)
        # 底模权重不在本机时会报错 —— 那不影响 resolution 这条断言
        if not error:
            self.assertEqual(out["resolution"], "768p",
                             "params.resolution 仍写着遗留档位")


# ==================================================================
# 8. 前端接线（静态契约）
# ==================================================================
class TestFrontendWiring(unittest.TestCase):
    """前端卡片必须用服务端算好的标签，不再直接读 params.resolution"""

    def test_jobcard_uses_resolution_info(self):
        src = (REPO / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("job.resolution_info", src,
                      "jobCard 没读 resolution_info —— 卡片仍会用旧档位")
        self.assertIn("localResolutionInfo", src,
                      "缺少服务端是旧版本时的兜底")

    def test_jobcard_no_longer_renders_raw_resolution_field(self):
        """
        回归锁：卡片里不能再出现 `p.resolution || "768p"` 这种写法。
        它就是「544p」错误的直接来源。
        """
        src = (REPO / "web" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn('p.resolution || "768p"', src,
                         "卡片仍在直接渲染 params.resolution —— "
                         "文生视频的遗留 544p 会再次出现")

    def test_agent_media_uses_resolution_info(self):
        src = (REPO / "web" / "agent-media.js").read_text(encoding="utf-8")
        self.assertIn("resolution_info", src,
                      "Agent 任务卡没读 resolution_info")

    def test_agent_js_passes_job_to_effective_params(self):
        src = (REPO / "web" / "agent.js").read_text(encoding="utf-8")
        self.assertIn("effectiveParams(mode, params, job)", src,
                      "agent.js 没把 job 传给 effectiveParams —— "
                      "Agent 卡拿不到 resolution_info")


if __name__ == "__main__":
    unittest.main()
