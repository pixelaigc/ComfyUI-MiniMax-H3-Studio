# -*- coding: utf-8 -*-
"""文生视频「面板所选模型 / LoRA == 生成记录显示」的回归测试。

【这个测试在防什么】

用户 2026-11-12 实测报出的缺陷：
    面板选：底模 = MiniMax-H3 混合版 · FL2VA+Ref2VA
            加速 LoRA = DMAD 4 步（critic 版），强度 1.00
    卡片显：模型 = 「H3 + ckpt850 · 4步」
            LoRA = 「空」

根因（两处）：
  1. **前端渲染层**读遗留字段 p.model / p.lora，而新面板提交的是
     model_key / accel_loras —— 于是恒显旧路径的 ckpt850。
  2. **前端提交层**对所有模式无条件写 `lora = state.lora`（遗留全局值
     恒为 "ckpt850"）—— 于是落库记录里同时躺着正确与错误两套字段。

修法：
  * 服务端新增 h3studio/job_meta.py，按任务真实 params 算出**权威**的
    模型 / LoRA 摘要，随 job 下发（job["model_info"]）。
  * 前端卡片只渲染这份摘要，并新增「LoRA」行；没有 LoRA 时**整行不渲染**。

【断言纪律】

  * 断言必须由「命名该行为的那条断言」抓住 —— 不能拿被测函数自己的
    输出算期望值。所以这里的期望值全部是**字面量**（面板下拉里的原文、
    本机权重文件名），不是从 job_meta 里反推的。
  * 变异验证：改坏 job_meta 的取值逻辑（换字段 / 吞掉 LoRA）必须让
    本文件变红 —— 见 tests/mutation_check_t2v_meta.py。

运行：python3 -m pytest tests/test_t2v_meta_consistency.py -v
"""
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import job_meta          # noqa: E402
from h3studio import video_models as VM  # noqa: E402

APP_JS = (REPO / "web" / "app.js").read_text(encoding="utf-8")


def strip_js_comments(src: str) -> str:
    """剥掉 JS 注释（// 行注释 与 /* */ 块注释）。

    为什么需要：这次修复把旧实现**保留在注释里**（作为「原来错在哪」的
    说明）。如果断言直接搜原文，注释里的旧代码会让测试误报。
    断言必须只针对**真正会执行的代码** —— 否则测试测的是注释，
    不是行为。

    做法：逐字符扫描，处理字符串字面量（' " `）与转义，
    只删除注释部分，保留代码原样。
    """
    out = []
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                c = src[i]
                out.append(c)
                if c == "\\":
                    if i + 1 < n:
                        out.append(src[i + 1])
                        i += 2
                        continue
                    i += 1
                    continue
                i += 1
                if c == quote:
                    break
            continue
        out.append(ch)
        i += 1
    return "".join(out)


APP_JS_CODE = strip_js_comments(APP_JS)


class TestT2VModelLoraSummary(unittest.TestCase):
    """服务端摘要：必须反映面板真实选择的模型 / 加速 LoRA / 强度"""

    def test_hybrid_model_and_dmad_lora(self):
        """用户报出的那个组合：混合版底模 + DMAD 4 步（critic 版）×1.00"""
        params = {
            "model_key": "hybrid_fl2va_ref2va",
            "accel_loras": [{"key": "dmad_4step_critic", "strength": 1.0,
                             "enabled": True}],
            "fixed_accel_lora": "ckpt850",
            "steps": 4,
        }
        info = job_meta.summarize("t2v", params)
        # 模型：面板下拉里的原文
        self.assertEqual(info["model_key"], "hybrid_fl2va_ref2va")
        self.assertEqual(info["model_label"], "MiniMax-H3 混合版 · FL2VA+Ref2VA")
        # 权重文件名：可与 ComfyUI prompt JSON 逐字核对
        self.assertEqual(info["model_unet"],
                         "minimax_h3_hybrid_fl2va_ref2va_b30-49-int8.safetensors")
        # 加速 LoRA：面板下拉里的原文 + 强度
        self.assertEqual(info["lora_label"], "DMAD 4 步（critic 版）")
        self.assertEqual(info["lora_strength"], 1.0)
        self.assertEqual(info["lora_name"],
                         "dmad_minimax_h3_4step_full_critic_comfyui.safetensors")
        # 工作流固定的那枚
        self.assertEqual(info["fixed_lora_label"], "Turbo 4 步 · ckpt850（工作流固定）")
        self.assertEqual(info["fixed_lora_name"],
                         "minimax_h3_turbo_4step_ema_ckpt850.safetensors")

    def test_summary_never_reports_legacy_lora_key(self):
        """摘要里的模型不能是旧路径的遗留显示串。

        旧实现恒显「H3 + ckpt850 · 4步」—— 那正是用户报的 bug。
        """
        params = {
            "model_key": "hybrid_fl2va_ref2va",
            "accel_loras": [{"key": "dmad_4step_critic", "strength": 1.0,
                             "enabled": True}],
            "fixed_accel_lora": "ckpt850",
            # 落库记录里同时存在的**遗留**字段（前端旧实现的产物）
            "model": "lora",
            "lora": "ckpt850",
            "lora_strength": 1,
        }
        info = job_meta.summarize("t2v", params)
        self.assertNotIn("ckpt850 · ", info["model_label"])
        self.assertNotEqual(info["model_label"], "H3 + ckpt850 · 4步")
        self.assertIn("混合版", info["model_label"])
        # 加速 LoRA 必须是用户选的那枚，而不是遗留字段里的 ckpt850
        self.assertEqual(info["lora_label"], "DMAD 4 步（critic 版）")
        # 遗留字段不能污染「用户选的加速 LoRA」那一项：
        # 遗留 lora = ckpt850 是**固定**加速 LoRA，不是用户勾选的那枚。
        self.assertNotEqual(info["lora_label"], "ckpt850")
        self.assertNotEqual(info["lora_name"],
                            "minimax_h3_turbo_4step_ema_ckpt850.safetensors")

    def test_pure_fl2va_model(self):
        """纯 H3 底模（无混合版）也要正确。"""
        params = {"model_key": "fl2va_pruned_int8",
                  "accel_loras": [], "fixed_accel_lora": "ckpt850"}
        info = job_meta.summarize("t2v", params)
        self.assertEqual(info["model_label"], "MiniMax-H3 FL2VA · 剪枝 int8（默认）")
        self.assertEqual(info["model_unet"],
                         "minimax_h3_fl2va_pruned_int8_convrot.safetensors")

    def test_ckpt850_fixed_lora_path(self):
        """ckpt850 档：面板选的是**固定**加速 LoRA（v4step600 / ckpt850）。"""
        params = {"model_key": "fl2va_int8",
                  "fixed_accel_lora": "v4step600",
                  "accel_loras": []}
        info = job_meta.summarize("t2v", params)
        self.assertEqual(info["fixed_lora_label"], "Turbo 4 步 · v4-step600（工作流固定）")
        self.assertEqual(info["fixed_lora_name"],
                         "minimax_h3_turbo_v4_step600_ema.safetensors")

    def test_no_accel_lora_means_empty_label(self):
        """未选加速 LoRA 时，lora_label 必须是**空串**（服务端摘要层）。

        「显示成『空』」这件事发生在**前端文案层**（loraLabel），
        服务端摘要保持空串，这样调用方能区分
        「没有 LoRA」与「有 LoRA 但没名字」。
        """
        params = {"model_key": "fl2va_pruned_int8",
                  "accel_loras": [], "fixed_accel_lora": "ckpt850"}
        info = job_meta.summarize("t2v", params)
        self.assertEqual(info["lora_label"], "")
        self.assertIsNone(info["lora_strength"])

    def test_disabled_accel_lora_is_ignored(self):
        """enabled=False 的加速 LoRA 不算「选了」。"""
        params = {"model_key": "fl2va_pruned_int8",
                  "accel_loras": [{"key": "dmad_4step_critic", "strength": 1.0,
                                   "enabled": False}],
                  "fixed_accel_lora": "ckpt850"}
        info = job_meta.summarize("t2v", params)
        self.assertEqual(info["lora_label"], "")

    def test_strength_is_preserved(self):
        """非 1.0 的强度必须原样带出（用户可调强度控件）。"""
        params = {"model_key": "hybrid_fl2va_ref2va",
                  "accel_loras": [{"key": "dmad_4step_critic", "strength": 0.65,
                                   "enabled": True}],
                  "fixed_accel_lora": "ckpt850"}
        info = job_meta.summarize("t2v", params)
        self.assertEqual(info["lora_strength"], 0.65)

    def test_legacy_video_path_still_works(self):
        """老任务（只有遗留字段）也能算出摘要 —— 历史卡片不能变空白。"""
        info = job_meta.summarize("t2v", {"model": "lora", "lora": "ckpt850",
                                          "lora_strength": 1.0, "steps": 4})
        self.assertEqual(info["source"], "legacy_video")
        self.assertEqual(info["model_label"], "MiniMax-H3 + LoRA")
        self.assertEqual(info["lora_label"], "ckpt850")

    def test_legacy_standard_h3_has_no_lora(self):
        """老路径「标准 H3」（lora=none）-> LoRA 为空。"""
        info = job_meta.summarize("t2v", {"model": "standard", "lora": "none"})
        self.assertEqual(info["lora_label"], "")
        self.assertEqual(info["model_label"], "标准 MiniMax-H3")

    def test_flf_r2v_use_legacy_path(self):
        """首尾帧 / 参考生视频走旧路径 -> 摘要走 legacy_video。"""
        for mode in ("flf", "r2v"):
            info = job_meta.summarize(mode, {"model": "lora", "lora": "ckpt850"})
            self.assertEqual(info["source"], "legacy_video", mode)
            self.assertEqual(info["lora_label"], "ckpt850", mode)

    def test_image_mode_model(self):
        """图像模式的模型显示名（来自 workflows 的真实常量表）。"""
        info = job_meta.summarize("t2i", {"model": "z_image_turbo_bf16"})
        self.assertEqual(info["source"], "image")
        self.assertEqual(info["model_label"], "Z-Image-Turbo · 官方加速版")
        self.assertEqual(info["lora_label"], "")

    def test_image_mode_with_lora(self):
        """图像模式带效果 LoRA 时，摘要里要带 LoRA 显示名 + 强度。

        【2026-11-13 修 —— 为什么 LoRA 名字换了】
        原来这里用的是合成名 "z-image-effects/zzz_test.safetensors" ——
        那个文件**本机并不存在**。C1 的白名单闸门（落库端 + 显示端）
        按「本机真实 LoRA 清单」判定，合成名自然被当成脏值挡掉
        （这正是闸门该做的事）。

        现在改用**本机真实存在**的 LoRA：
        "Kook_Zimage_瑶光.safetensors"（在 loras.LORA_CATALOG 里，
        也是图像面板下拉里的真实选项）。

        【断言为什么这么写】
        「LoRA 摘要要带显示名 + 强度」这条**行为契约**不变；
        只是输入换成一个真实存在的权重 —— 否则这条用例测的是
        「不存在的 LoRA 该不该显示」，而不是「真实 LoRA 该不该显示」。
        """
        info = job_meta.summarize("t2i", {
            "model": "z_image_turbo_bf16",
            "loras": [{"name": "Kook_Zimage_瑶光.safetensors",
                       "strength": 0.6, "enabled": True}],
        })
        self.assertIn("×0.60", info["lora_label"])

    def test_music_mode_model(self):
        info = job_meta.summarize("t2a", {"checkpoint": "yue2_3b_bf16"})
        self.assertEqual(info["source"], "music")
        self.assertEqual(info["model_label"], "YuE2 3B · BF16 完整版")

    def test_unknown_model_key_is_shown_as_is(self):
        """未知 key -> 如实显示 key 本身，不编造名字。"""
        info = job_meta.summarize("t2v", {"model_key": "totally_unknown_key"})
        self.assertEqual(info["model_label"], "totally_unknown_key")

    def test_legacy_job_without_model_shows_workflow_default(self):
        """老任务没记 model -> 摘要必须反映**工作流实际用的默认权重**。

        【真实场景】内置 Agent 建的老任务常常根本没记 model，
        工作流走的是自己的默认值（workflows._h3_video_workflow:
        `lora = params.get("lora", "ckpt850")`、UNET["fl2va"][precision]）。
        这时摘要不能是空白 —— 用户仍然需要知道实际用了什么。
        """
        from h3studio import workflows as wf
        info = job_meta.summarize("t2v", {"precision": "int8"})
        self.assertTrue(info["model_label"])
        self.assertIn("默认档", info["model_label"])
        self.assertEqual(info["model_unet"],
                         wf.UNET["fl2va"]["int8"])
        # 加速 LoRA：工作流默认挂了 ckpt850
        self.assertEqual(info["lora_label"], "ckpt850")
        self.assertEqual(info["lora_name"],
                         wf.LORAS["ckpt850"])
        self.assertEqual(info["lora_strength"], 1.0)

    def test_legacy_job_with_explicit_none_lora_stays_empty(self):
        """明确记了 lora=none 的任务，LoRA 仍是空（不被默认值覆盖）。"""
        info = job_meta.summarize("t2v", {"model": "standard", "lora": "none"})
        self.assertEqual(info["lora_label"], "")

    def test_r2v_default_model_is_ref2va_not_fl2va(self):
        """r2v 的默认底模是 **Ref2VA**，不是 FL2VA。

        【实测抓到的错】workflows.build_r2v:477 用的是
        `unet_name: UNET["ref2va"][precision]`，而 t2v/flf 用 UNET["fl2va"]。
        摘要若不分 mode，r2v 卡片会显示成 FL2VA —— 底模说错，
        与本次要修的「记录必须反映实际用的权重」同源。
        """
        from h3studio import workflows as wf
        info = job_meta.summarize("r2v", {"precision": "int8"})
        self.assertEqual(info["model_key"], "ref2va")
        self.assertEqual(info["model_unet"], wf.UNET["ref2va"]["int8"])
        self.assertIn("Ref2VA", info["model_label"])
        self.assertNotIn("FL2VA", info["model_label"])
        # t2v/flf 仍然是 fl2va
        for mode in ("t2v", "flf"):
            other = job_meta.summarize(mode, {"precision": "int8"})
            self.assertEqual(other["model_key"], "fl2va", mode)
            self.assertEqual(other["model_unet"], wf.UNET["fl2va"]["int8"], mode)

    def test_image_job_without_model_shows_workflow_default(self):
        """图像老任务没记 model -> 用该模式工作流的默认底模。"""
        from h3studio import workflows as wf
        info = job_meta.summarize("t2i", {})
        self.assertEqual(info["model_key"], wf.Z_IMAGE_DEFAULT_MODEL)
        self.assertTrue(info["model_label"])

    def test_music_job_without_checkpoint_shows_default(self):
        """音乐老任务没记 checkpoint -> 用默认 checkpoint。"""
        from h3studio import music_yue2
        info = job_meta.summarize("t2a", {})
        self.assertEqual(info["model_key"],
                         music_yue2.YUE2_DEFAULT_CHECKPOINT)


class TestFrontendRendersSummary(unittest.TestCase):
    """前端：卡片必须渲染服务端摘要，且有独立的「LoRA」行"""

    def test_card_reads_model_info_not_legacy_fields(self):
        """前端不再直接读 p.lora 来拼模型名。

        断言只针对**可执行代码**（剥掉注释）—— 修复说明里保留了旧实现
        作为「原来错在哪」的说明，注释里出现旧代码是正常的。
        """
        # 旧的错误实现（必须已不存在于**代码**里）
        self.assertNotIn('p.lora === "ckpt850" ? "ckpt850" : "v4-step600"',
                         APP_JS_CODE)
        # 新实现：读服务端下发的 model_info
        self.assertIn("job.model_info", APP_JS_CODE)
        self.assertIn("function jobModelInfo(job)", APP_JS_CODE)

    def test_card_has_lora_row(self):
        """卡片模板里必须有「LoRA」行。"""
        self.assertIn('<span>LoRA</span>', APP_JS_CODE)

    def test_lora_row_omitted_when_empty(self):
        """没有 LoRA 时整行不渲染（不是显示「空」）。

        渲染逻辑已抽成独立函数 loraRowHtml（见 test_t2v_card_dom.py 的
        **真实执行**断言）；这里只做一道结构性检查，确保卡片用的是
        那个函数，而不是自己内联拼一行。
        """
        self.assertIn("const loraRow = loraRowHtml(loraLabel(job));",
                      APP_JS_CODE)
        self.assertIn("function loraRowHtml(loraText)", APP_JS_CODE)

    def test_frontend_does_not_write_legacy_lora_for_all_modes(self):
        """提交层不能对**所有模式**无条件写遗留 lora 字段。

        旧实现：
            lora: state.model === "lora" ? state.lora : "none",
            lora_strength: state.loraStrength,
        这两行出现在**通用** params 构造里 -> 图像/音乐/文生视频都被污染。
        现在只在 flf / r2v 分支里写。
        """
        self.assertNotIn(
            'lora: state.model === "lora" ? state.lora : "none",\n'
            '    lora_strength: state.loraStrength,',
            APP_JS_CODE)
        self.assertIn('if (mode === "flf" || mode === "r2v") {', APP_JS_CODE)
        self.assertIn('params.lora = state.model === "lora" ? state.lora : "none";',
                      APP_JS_CODE)


class TestServerWiresModelInfo(unittest.TestCase):
    """服务端：每个 job 都带上 model_info"""

    def test_row_to_job_includes_model_info(self):
        src = (REPO / "server.py").read_text(encoding="utf-8")
        self.assertIn('"model_info": job_meta.summarize(mode, job_params),', src)

    def test_job_meta_module_imported(self):
        src = (REPO / "server.py").read_text(encoding="utf-8")
        self.assertIn("from h3studio import job_meta", src)


class TestPanelAndSummaryAgree(unittest.TestCase):
    """面板清单与摘要必须同源（显示名逐字一致）"""

    def test_summary_labels_match_panel_catalogue(self):
        for key, meta in VM.VIDEO_MODELS.items():
            info = job_meta.summarize("t2v", {"model_key": key})
            self.assertEqual(info["model_label"], meta["label"], key)
            self.assertEqual(info["model_unet"], meta["unet"], key)

    def test_accel_lora_labels_match_panel_catalogue(self):
        for key, meta in VM.VIDEO_ACCEL_LORA_COMBOS.items():
            info = job_meta.summarize("t2v", {
                "model_key": "fl2va_pruned_int8",
                "accel_loras": [{"key": key, "strength": 1.0, "enabled": True}],
            })
            self.assertEqual(info["lora_label"], meta["display"], key)
            self.assertEqual(info["lora_name"], meta["name"], key)


if __name__ == "__main__":
    unittest.main()
