# -*- coding: utf-8 -*-
"""文生视频「接续生成」的回归测试。

【这个测试在防什么】
接续是个**跨 4 层**的功能（前端按钮 -> 后端上下文 -> 工作流接线 ->
潜空间落盘/读回），任何一层写错都会出下面几种坏结果：

  1. **看起来成功，其实没接上** —— 最坏的一种。工作流里没接桥，
     但界面显示「接续成功」，用户拿到的是一段独立的视频。
     → 断言必须检查**工作流里真的有桥节点**。
  2. **接缝整体位移** —— 钉住窗口不在 5+17k 网格上，或段长不对齐
     5 步周期边界。→ 断言帧数换算。
  3. **二采产物也能接续** —— 用户需求明确排除 selflift。
     二采潜空间与标准采样不同源，接上去会位移。
  4. **潜空间缺失时静默失败** —— 用户需求要求「明确中文提示」。
  5. **段号算错** —— 无限接续依赖段号严格 +1。

【断言纪律】
  * 期望值全部是**字面量**（本机实测的网格数字、节点名），
    不是从被测函数自己的输出反推。
  * 断言要由「命名该行为的那条断言」抓住。

运行：python3 -m pytest tests/test_continuation.py -v
"""

import json
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import continuation as cont            # noqa: E402
from h3studio import continuation_service as cs      # noqa: E402
from h3studio import video_workflow as vw            # noqa: E402


# ------------------------------------------------------------------
# 帧数换算（协议层）
# ------------------------------------------------------------------
class TestFrameMath(unittest.TestCase):
    """帧数换算必须与本机 ComfyUI / Relay-Kit 的实测公式一致。"""

    def test_pixel_frames_matches_relay_kit_cycle(self):
        """latent 步 -> 像素帧：按 (1,4,4,4,4) 循环。"""
        # 1 步 -> 1 帧；2 步 -> 5 帧；3 步 -> 9 帧；4 步 -> 13；5 步 -> 17
        self.assertEqual(cont.pixel_frames(1), 1)
        self.assertEqual(cont.pixel_frames(2), 5)
        self.assertEqual(cont.pixel_frames(3), 9)
        self.assertEqual(cont.pixel_frames(4), 13)
        self.assertEqual(cont.pixel_frames(5), 17)
        self.assertEqual(cont.pixel_frames(7), 22)   # 22 帧窗口 = 7 个 latent 步
        self.assertEqual(cont.pixel_frames(37), 124)  # 5s 段 = 37 步 = 124 帧

    def test_steps_for_frames_rejects_off_grid(self):
        """不在网格上的帧数切不出来 -> 返回 None（不能悄悄挪一格）。"""
        self.assertEqual(cont.steps_for_frames(5), 2)
        self.assertEqual(cont.steps_for_frames(22), 7)
        self.assertEqual(cont.steps_for_frames(39), 12)
        self.assertEqual(cont.steps_for_frames(124), 37)
        # 6 帧不是整步窗口（5 步覆盖 5 帧，6 步覆盖 9 帧）
        self.assertIsNone(cont.steps_for_frames(6))
        self.assertIsNone(cont.steps_for_frames(10))

    def test_align_frame_count_snaps_to_17k_plus_5(self):
        """段长必须落在 17k+5 网格。"""
        self.assertEqual(cont.align_frame_count(124), 124)
        self.assertEqual(cont.align_frame_count(243), 243)
        self.assertEqual(cont.align_frame_count(362), 362)
        self.assertEqual(cont.align_frame_count(120), 124)
        self.assertEqual(cont.align_frame_count(121), 124)
        self.assertEqual(cont.align_frame_count(125), 141)

    def test_video_latent_t_matches_comfyui_formula(self):
        """latent 步数必须与 comfy_extras 的 video_latent_t 一致。"""
        # (fc-5)//17*5 + 2
        self.assertEqual(cont.video_latent_t(5), 2)
        self.assertEqual(cont.video_latent_t(22), 7)
        self.assertEqual(cont.video_latent_t(124), 37)
        self.assertEqual(cont.video_latent_t(243), 72)
        self.assertEqual(cont.video_latent_t(362), 107)

    def test_valid_context_windows(self):
        """钉住窗口只认 5+17k 网格。"""
        for good in (5, 22, 39, 56, 73, 90, 107, 124):
            self.assertTrue(cont.valid_context_frames(good),
                            f"{good} 应该是合法窗口")
        for bad in (6, 10, 21, 23, 30):
            self.assertFalse(cont.valid_context_frames(bad),
                             f"{bad} 不该是合法窗口")

    def test_snap_context_frames_rounds_down(self):
        """吸附只能向下 —— 向上会切不出整步窗口。"""
        self.assertEqual(cont.snap_context_frames(22), 22)
        self.assertEqual(cont.snap_context_frames(23), 22)
        self.assertEqual(cont.snap_context_frames(38), 22)
        self.assertEqual(cont.snap_context_frames(39), 39)
        self.assertEqual(cont.snap_context_frames(100), 90)
        self.assertEqual(cont.snap_context_frames(3), 0)  # 比最小窗口还小

    def test_tail_alignment_for_standard_segments(self):
        """尾段起点必须落在 5 步周期边界，否则接缝整体位移。"""
        for fc in (124, 243, 362):
            for win in (5, 22, 39, 56):
                self.assertTrue(cont.tail_aligned(fc, win),
                                f"段长 {fc} / 窗口 {win} 应该对齐")

    def test_continuation_frames_ledger(self):
        """帧数账：本段总帧 - 钉住帧 = 交付帧。"""
        ledger = cont.continuation_frames(124, 22)
        self.assertEqual(ledger["frame_count"], 124)
        self.assertEqual(ledger["latent_steps"], 37)
        self.assertEqual(ledger["context_frames"], 22)
        self.assertEqual(ledger["context_steps"], 7)
        self.assertEqual(ledger["trim_frames"], 22)
        self.assertEqual(ledger["delivered_frames"], 102)
        self.assertAlmostEqual(ledger["total_seconds"], 102 / 24, places=3)


# ------------------------------------------------------------------
# 可接续性判定
# ------------------------------------------------------------------
class TestContinuationAllowed(unittest.TestCase):
    """selflift 必须被排除 —— 用户需求明确要求。"""

    def test_selflift_is_excluded(self):
        ok, reason = cont.continuation_allowed("t2v", {"twopass": {"enabled": True}})
        self.assertFalse(ok, "二采产物不能接续")
        self.assertIn("二采", reason)
        self.assertTrue(reason.strip(), "必须给出原因，不能是空串")

    def test_normal_t2v_is_allowed(self):
        ok, reason = cont.continuation_allowed("t2v", {"twopass": {"enabled": False}})
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_non_video_modes_rejected(self):
        for mode in ("t2i", "t2i_qwen", "t2a", "longfilm"):
            ok, reason = cont.continuation_allowed(mode, {})
            self.assertFalse(ok, f"{mode} 不该支持接续")
            self.assertTrue(reason.strip())


# ------------------------------------------------------------------
# 上下文组装
# ------------------------------------------------------------------
class TestContextBuilding(unittest.TestCase):
    """回填内容：全部配置/参数/模型/素材，但**不含提示词**。"""

    def _params(self):
        return {
            "model_key": "fl2va_int8",
            "fixed_accel_lora": "turbo_v4",
            "accel_loras": [{"key": "dmad4", "strength": 1.0, "enabled": True}],
            "attention": "sage",
            "twopass": {"enabled": False},
            "width": 768, "height": 432,
            "aspect": "16:9", "duration": 5,
            "steps": 4, "sampler": "euler", "scheduler": "simple",
            "shift_video": 12.0, "shift_audio": 3.0, "cfg": 1.0,
            "seed": 12345,
            "prompt": "一段旧提示词，不该被回填",
        }

    def test_prompt_is_never_included(self):
        ctx = cont.build_context(self._params())
        self.assertEqual(ctx["prompt"], "", "提示词必须为空（用户自己写下一段）")
        self.assertNotIn("prompt", ctx["params"],
                         "params 里也不该有 prompt 键")

    def test_all_config_fields_are_included(self):
        ctx = cont.build_context(self._params())
        for field in ("model_key", "fixed_accel_lora", "accel_loras",
                      "attention", "twopass", "width", "height", "aspect",
                      "duration", "steps", "sampler", "scheduler",
                      "shift_video", "shift_audio", "cfg", "seed"):
            self.assertIn(field, ctx["params"], f"{field} 必须被回填")

    def test_summary_is_human_readable(self):
        ctx = cont.build_context(self._params())
        labels = {row["label"] for row in ctx["summary"]}
        self.assertIn("模型", labels)
        self.assertIn("输出尺寸", labels)
        self.assertIn("采样步数", labels)
        # 尺寸必须是 w×h
        size = [r for r in ctx["summary"] if r["label"] == "输出尺寸"][0]
        self.assertEqual(size["value"], "768×432")

    def test_empty_params_gives_empty_context(self):
        ctx = cont.build_context({})
        self.assertEqual(ctx["params"], {})
        self.assertEqual(ctx["summary"], [])


# ------------------------------------------------------------------
# 链 id 与落盘路径
# ------------------------------------------------------------------
class TestChainPaths(unittest.TestCase):
    """落盘与读回必须指向**同一个**目录 —— 单一真源。"""

    def test_chain_run_id_prefixed(self):
        """run_id 必须带 h3studio_ 前缀（避免和手工 Relay-Kit 目录撞车）。"""
        self.assertEqual(cont.chain_run_id("abc123"), "h3studio_abc123")

    def test_chain_run_id_sanitizes(self):
        """非法字符换成 _（不是删除）—— 否则 a/b 与 ab 会撞目录。"""
        self.assertEqual(cont.chain_run_id("a/b"), "h3studio_a_b")
        self.assertEqual(cont.chain_run_id("ab"), "h3studio_ab")
        self.assertNotEqual(cont.chain_run_id("a/b"), cont.chain_run_id("ab"))

    def test_workflow_and_service_use_same_run_id(self):
        """工作流拼装与服务层必须算出同一个 run_id。"""
        chain = "CHAIN_X"
        state = vw._continuation_state({"continuation": {"chain_id": chain}})
        self.assertEqual(state["run_id"], cs.chain_run_id(chain))

    def test_stage_relpath_matches_relay_kit_layout(self):
        """落盘路径必须与 Relay-Kit 的 _stage_path 完全一致。"""
        self.assertEqual(cs.stage_relpath("h3studio_abc", 0),
                         "relay_kit/h3studio_abc/stage_00000.safetensors")
        self.assertEqual(cs.stage_relpath("h3studio_abc", 12),
                         "relay_kit/h3studio_abc/stage_00012.safetensors")


# ------------------------------------------------------------------
# 工作流接线（最关键：防「看起来成功其实没接上」）
# ------------------------------------------------------------------
class TestWorkflowWiring(unittest.TestCase):
    """接续段的工作流里必须真的有桥，且接对位置。"""

    def _base(self):
        return dict(model_key="fl2va_int8", width=768, height=432, duration=5,
                    steps=4, seed=1, prompt="p", sampler="euler",
                    scheduler="simple")

    def _cont(self, stage=1):
        return dict(chain_id="CH1", stage_index=stage, source_job="J0",
                    source_segment=stage - 1, context_frames=22,
                    anchor_frames=22)

    def test_normal_generation_has_no_bridge(self):
        """普通生成不该有桥 —— 加了会改变普通生成的出图结果。"""
        p = vw.build_t2v_panel({**self._base(), "job_id": "J1"})
        self.assertNotIn("300:bridge", p)
        self.assertNotIn("300:load", p)
        self.assertNotIn("300:trim", p)

    def test_continuation_has_bridge_nodes(self):
        """接续段必须有：读回 -> 拷贝桥 -> 裁重叠 -> 落盘。"""
        p = vw.build_t2v_panel({**self._base(), "job_id": "J2",
                                "continuation": self._cont()})
        for nid, cls in (("300:load", "H3RelayLatentLoad"),
                       ("300:bridge", "H3RelayCopyBridge"),
                       ("300:trim", "H3RelayTrimAV"),
                       ("300:save", "H3RelayLatentSave")):
            self.assertIn(nid, p, f"接续段必须有节点 {nid}")
            self.assertEqual(p[nid]["class_type"], cls)

    def test_bridge_sits_between_latent_and_sampler(self):
        """拷贝桥必须在 latent 与采样器之间（否则钉住区不起作用）。"""
        p = vw.build_t2v_panel({**self._base(), "job_id": "J2",
                                "continuation": self._cont()})
        # 采样器的 latent_image 必须指向拷贝桥
        self.assertEqual(p["14"]["inputs"]["latent_image"], ["300:bridge", 0])
        # 拷贝桥的 latent 输入必须来自条件节点（不是硬编码）
        self.assertEqual(p["300:bridge"]["inputs"]["latent"], ["105:104", 1])

    def test_guider_uses_bridged_conditioning(self):
        """引导器必须用拷贝桥第 4 路（钉帧后的 conditioning）。"""
        p = vw.build_t2v_panel({**self._base(), "job_id": "J2",
                                "continuation": self._cont()})
        self.assertEqual(p["16"]["inputs"]["conditioning"], ["300:bridge", 3])

    def test_trim_sits_after_decode(self):
        """裁重叠必须在 decode 之后，且音画同裁。"""
        p = vw.build_t2v_panel({**self._base(), "job_id": "J2",
                                "continuation": self._cont()})
        self.assertEqual(p["300:trim"]["inputs"]["images"], ["10", 0])
        self.assertEqual(p["300:trim"]["inputs"]["audio"], ["23", 0])
        self.assertEqual(p["300:trim"]["inputs"]["trim_frames"], ["300:bridge", 2])
        # 合成视频必须用裁完的画面 + 裁完的音频（红线：音画不同步）
        self.assertEqual(p["91"]["inputs"]["images"], ["300:trim", 0])
        self.assertEqual(p["91"]["inputs"]["audio"], ["300:trim", 1])

    def test_save_node_takes_sampler_output(self):
        """落盘必须存**采样器的输出**（钉住区之后的完整段）。"""
        p = vw.build_t2v_panel({**self._base(), "job_id": "J2",
                                "continuation": self._cont()})
        self.assertEqual(p["300:save"]["inputs"]["latent"], ["14", 0])

    def test_bridge_mask_mode_is_hard(self):
        """钉住区必须零重绘（hard 掩码）—— 否则复现发糊/漂移。"""
        p = vw.build_t2v_panel({**self._base(), "job_id": "J2",
                                "continuation": self._cont()})
        self.assertEqual(p["300:bridge"]["inputs"]["mask_mode"], "hard")
        self.assertTrue(p["300:bridge"]["inputs"]["pin_audio"])

    def test_bridge_and_save_share_same_run_id(self):
        """读回与落盘必须同一个 run_id —— 否则写 A 读 B。"""
        p = vw.build_t2v_panel({**self._base(), "job_id": "J2",
                                "continuation": self._cont()})
        self.assertEqual(p["300:load"]["inputs"]["run_id"],
                         p["300:save"]["inputs"]["run_id"])
        self.assertEqual(p["300:bridge"]["inputs"]["run_id"],
                         p["300:save"]["inputs"]["run_id"])

    def test_load_reads_previous_stage(self):
        """H3RelayLatentLoad 的 stage_index 语义是「读第 N-1 段」。"""
        p = vw.build_t2v_panel({**self._base(), "job_id": "J2",
                                "continuation": self._cont(stage=3)})
        # 本段段号 3 -> 节点读第 2 段（节点自己 idx = stage-1）
        self.assertEqual(p["300:load"]["inputs"]["stage_index"], 3)
        self.assertEqual(p["300:bridge"]["inputs"]["stage_index"], 3)

    def test_appearance_anchor_enabled_from_stage_1(self):
        """第 2 段起要读链首段当外观锚（防长程漂移）。"""
        p = vw.build_t2v_panel({**self._base(), "job_id": "J2",
                                "continuation": self._cont(stage=1)})
        self.assertEqual(p["300:bridge"]["inputs"]["ref_anchor_stage"], 0)

    def test_selflift_never_gets_bridge(self):
        """二采不走接续桥（潜空间不同源）。"""
        params = {**self._base(), "job_id": "J2", "continuation": self._cont()}
        params["twopass"] = {"enabled": True, "transition_step": 3,
                             "lowres_scale": 0.5, "rho": 0.0, "w_min": 0.5,
                             "w_max": 1.0, "upscaler_model": "none", "cfg": 1.0}
        p = vw.build_t2v_panel(params)
        self.assertNotIn("300:bridge", p, "二采不该接拷贝桥")
        self.assertNotIn("300:load", p)

    def test_off_grid_context_window_is_snapped(self):
        """非网格窗口必须吸附到网格值（否则节点会 raise）。"""
        p = vw.build_t2v_panel({**self._base(), "job_id": "J2",
                                "continuation": dict(self._cont(),
                                                     context_frames=30)})
        self.assertEqual(p["300:bridge"]["inputs"]["context_frames"], 22)


# ------------------------------------------------------------------
# 服务层：可接续状态与上下文
# ------------------------------------------------------------------
class TestServiceState(unittest.TestCase):
    """潜空间缺失 / 二采 / 老任务都要**明确**拒绝，不能静默。"""

    def test_missing_latent_gives_chinese_reason(self):
        """潜空间不在 -> 明确中文提示（用户需求明确要求）。"""
        params = {"continuation": {"chain_id": "NOPE", "stage_index": 0}}
        state = cs.continuation_state("t2v", params, "J9")
        self.assertFalse(state["can_continue"])
        self.assertTrue(state["reason"].strip(), "必须给出原因")
        self.assertIn("潜空间", state["reason"])

    def test_selflift_state_refused_with_reason(self):
        params = {"twopass": {"enabled": True},
                  "continuation": {"chain_id": "C", "stage_index": 0}}
        state = cs.continuation_state("t2v", params, "J9")
        self.assertFalse(state["can_continue"])
        self.assertIn("二采", state["reason"])

    def test_legacy_task_without_chain_is_refused(self):
        """上线前的历史任务没有链信息 -> 不能接续，但要说清楚。"""
        state = cs.continuation_state("t2v", {}, "OLD")
        self.assertFalse(state["can_continue"])
        self.assertTrue(state["reason"].strip())

    def test_non_video_mode_refused(self):
        state = cs.continuation_state("t2i", {"continuation": {"chain_id": "C"}}, "J")
        self.assertFalse(state["can_continue"])
        self.assertTrue(state["reason"].strip())


if __name__ == "__main__":
    unittest.main()
