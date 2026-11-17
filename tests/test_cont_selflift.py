# -*- coding: utf-8 -*-
"""P2：二采（SelfLift）接续的回归测试。

【这个测试在防什么】
用户明确要求「selflift 是否可以接续」。原来的实现是**硬拒**
（continuation.py 里「二采潜空间不同源、不支持接续」）。2026-11-17
独立复核后**推翻了那个结论**，本测试锁住新结论与新接线：

  1. **静默退化成普通生成** —— 最坏的一种失败。二采段如果只是把
     `not use_twopass` 去掉、但 SelfLift 采样器没接拷贝桥，
     工作流会「看起来接上了」但实际采样器拿的是没桥过的 latent。
     → 断言工作流里**真的**有 300:load / 300:bridge / 300:trim / 300:save。
  2. **positive 没走桥第 4 路** —— 二采采样器拿条件节点直出的话，
     它看不到上一段的钉帧（minimax_keyframes），接缝处取景会漂。
     → 断言 `171.positive == ["300:bridge", 3]`。
  3. **网格不对齐** —— 二采落盘的 latent 时间步必须落在
     `video_latent_t(align_frame_count(时长))`，且尾段切片起始落在
     5 步周期边界（否则是整体位移的接缝）。
     → 断言用**真实落盘样本的实测数字**（10s→72、5s→37）。
  4. **落盘存错东西** —— 必须存**采样器的输出**（171 的第 0 路），
     不是桥的输出（nonebot v1 是直通场景，存桥输出没有意义）。
  5. **二采段缺潜空间时的中文报错** —— 必须具体，不能是
     「服务端未能给出具体原因」。

【断言纪律】
  * 期望值全是**字面量**（本机实测的网格数字、节点名），
    不是从被测函数自己的输出反推。
  * 断言由「命名该行为的那条断言」抓住。

运行：python3 -m pytest tests/test_cont_selflift.py -v
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
from h3studio import h3_video_modes as hvm           # noqa: E402


# ------------------------------------------------------------------
# 公共夹具
# ------------------------------------------------------------------
def _cont_params(job_id="J2", chain="J1"):
    """一段「有上游」的接续参数。

    【为什么普通生成也要带 continuation】服务端在提交时
    （create_job_record）就给**每一个**视频任务建链 ——
    链首段也要落盘自己的潜空间，才能被继续接续。
    工作流是 worker 之后才拼的，那时 params 里**一定有** continuation。
    → 所以「普通生成」也有 300:save，只是没有 300:load/bridge/trim。
    """
    return {
        "continuation": {
            "chain_id": chain,
            "stage_index": 1,
            "source_job": chain,
            "source_segment": 0,
            "source_latent_run_id": "h3studio_s%s" % chain,
            "source_latent_stage": 0,
            "source_latent_path": "/out/relay_kit/h3studio_s%s/stage_00000.safetensors" % chain,
            "anchor_latent_run_id": "h3studio_s%s" % chain,
            "anchor_latent_stage": 0,
            "anchor_latent_path": "/out/relay_kit/h3studio_s%s/stage_00000.safetensors" % chain,
            "latent_run_id": "h3studio_s%s" % job_id,
            "latent_stage": 0,
            "context_frames": 22,
            "anchor_frames": 22,
            "chain_total_frames": 124,
            "segment_frames": 124,
        },
    }


def _chain_head_params(job_id="J1"):
    """链首段参数：有链信息、但**没有上游**（source_job 为空）。"""
    info = _cont_params(job_id)["continuation"]
    info["chain_id"] = job_id
    info["stage_index"] = 0
    info["source_job"] = ""
    info["source_segment"] = -1
    info["source_latent_run_id"] = ""
    info["source_latent_stage"] = 0
    info["source_latent_path"] = ""
    info["anchor_latent_run_id"] = ""
    info["anchor_latent_stage"] = 0
    info["anchor_latent_path"] = ""
    info["chain_total_frames"] = 0
    info["segment_frames"] = 124
    return {"continuation": info}


def _t2v_base(job_id="J1", **extra):
    params = {
        "job_id": job_id,
        "mode": "t2v",
        "prompt": "一只猫在跑",
        "model_key": "minimax_h3_t2v",
        "duration": 5.0,
        "width": 1280,
        "height": 736,
        "steps": 4,
        "seed": 12345,
        "sampler": "euler",
        "scheduler": "beta",
    }
    params.update(extra)
    return params


def _twopass(**extra):
    """二采参数（面板上的二采区块）。"""
    tw = {
        "enabled": True,
        "transition_step": 5,
        "lowres_scale": 0.75,
        "rho": 0.0,
        "w_min": 0.8,
        "w_max": 1.0,
        "upscaler_model": "minimax_h3_latent_upscaler_3d_fp16.safetensors",
        "cfg": 1.0,
    }
    tw.update(extra)
    return tw


# ------------------------------------------------------------------
# 1. 服务端闸门：二采不再无条件被拒
# ------------------------------------------------------------------
class TestSelfliftGate(unittest.TestCase):
    """二采**可以**接续 —— 但闸门不能是「一律拒绝」。"""

    def test_selflift_mode_is_not_blanket_rejected(self):
        """二采产物不该再被「潜空间不同源」这种理由一刀切拒绝。

        旧实现返回 False + 「二采产物不支持接续」。
        新实现：模式判定通过；「上游有没有落盘潜空间」交给服务层查盘。
        """
        allowed, reason = cont.continuation_allowed(
            "t2v", {"twopass": {"enabled": True}})
        self.assertTrue(allowed,
                        "二采应允许接续（网格对齐已实测通过），实际被拒：%s" % reason)
        self.assertEqual(reason, "")

    def test_non_video_mode_still_rejected(self):
        """非视频模式仍然拒绝（它们没有 AV latent）。"""
        allowed, reason = cont.continuation_allowed("image", {})
        self.assertFalse(allowed)
        self.assertIn("不是视频生成产物", reason)

    def test_selflift_enabled_detects_both_forms(self):
        """二采开关的两种写法都要认。"""
        self.assertTrue(cont.selflift_enabled({"twopass": {"enabled": True}}))
        self.assertTrue(cont.selflift_enabled({"selflift": True}))
        self.assertFalse(cont.selflift_enabled({"twopass": {"enabled": False}}))
        self.assertFalse(cont.selflift_enabled({}))

    def test_missing_reason_has_no_stale_selflift_claim(self):
        """缺潜空间的原因文案不能还说「二采不支持接续」—— 那已经不成立。"""
        reason = cont.missing_reason("/out", "h3studio_sJ1", 0)
        self.assertNotIn("二采不支持接续", reason)
        self.assertIn("复用参数", reason)


# ------------------------------------------------------------------
# 2. 工作流接线：二采真的走桥（不是静默退化）
# ------------------------------------------------------------------
class TestSelfliftWorkflowBridge(unittest.TestCase):
    """二采接续段的工作流里必须**真的有桥**。"""

    def _t2v_selflift_cont(self, job_id="J2"):
        p = _t2v_base(job_id, twopass=_twopass())
        p.update(_cont_params(job_id))
        return p

    def test_selflift_continuation_has_bridge_nodes(self):
        """二采接续段必须有：读回 -> 拷贝桥 -> 裁重叠 -> 落盘。"""
        p = vw.build_t2v_panel(self._t2v_selflift_cont())
        for nid, cls in (("300:load", "H3RelayLatentLoad"),
                         ("300:bridge", "H3RelayCopyBridge"),
                         ("300:trim", "H3RelayTrimAV"),
                         ("300:save", "H3RelayLatentSave")):
            self.assertIn(nid, p, "二采接续段必须有节点 %s" % nid)
            self.assertEqual(p[nid]["class_type"], cls)

    def test_selflift_sampler_consumes_bridge_latent(self):
        """SelfLift 采样器的 latent_image 必须吃**拷贝桥第 0 路**。

        这是 nonebot minimax_h3_v1.json 的 171 节点姿势：
          latent_image = ["322", 0]
        不接桥 = 二采采样器拿的是本段空 latent = 钉住区完全不起作用
        = 静默退化成普通生成。
        """
        p = vw.build_t2v_panel(self._t2v_selflift_cont())
        self.assertEqual(p["171"]["inputs"]["latent_image"], ["300:bridge", 0])

    def test_selflift_positive_uses_bridge_fourth_output(self):
        """SelfLift 的 positive 必须走**拷贝桥第 4 路**。

        nonebot v1: positive = ["322", 3]（桥第 4 路 = 钉帧后的 conditioning）。
        走条件节点直出的话，二采段看不到上一段的画面钉帧 → 取景漂。
        """
        p = vw.build_t2v_panel(self._t2v_selflift_cont())
        self.assertEqual(p["171"]["inputs"]["positive"], ["300:bridge", 3])

    def test_selflift_guider_uses_bridge_fourth_output(self):
        """引导器也要用桥第 4 路（与采样器一致）。"""
        p = vw.build_t2v_panel(self._t2v_selflift_cont())
        self.assertEqual(p["16"]["inputs"]["conditioning"], ["300:bridge", 3])

    def test_selflift_negative_still_zeroes_condition_node(self):
        """负面条件仍接条件节点 —— 桥第 4 路带的是**钉帧**不是引导。

        钉帧只该约束构图，不该当引导，所以 negative 零化条件节点是对的
        （与 nonebot v1 的 320 一致）。
        """
        p = vw.build_t2v_panel(self._t2v_selflift_cont())
        self.assertEqual(p["320"]["inputs"]["conditioning"], ["105:104", 0])
        self.assertEqual(p["171"]["inputs"]["negative"], ["320", 0])

    def test_selflift_save_takes_sampler_output_not_bridge(self):
        """落盘必须存**采样器的输出**（171 第 0 路），不是桥的输出。

        nonebot v1 存 ["322",0] 是因为它的 322 是**直通桥**（无上游时
        桥原样返回）。本项目续接段有真实拷贝，存桥输出就丢了新采样内容。
        """
        p = vw.build_t2v_panel(self._t2v_selflift_cont())
        self.assertEqual(p["300:save"]["inputs"]["latent"], ["171", 0])
        self.assertNotEqual(p["300:save"]["inputs"]["latent"], ["300:bridge", 0])

    def test_selflift_trim_sits_after_decode(self):
        """二采接续段也要裁钉住区（音画同裁），否则缝处会重播 0.9s。"""
        p = vw.build_t2v_panel(self._t2v_selflift_cont())
        self.assertEqual(p["300:trim"]["inputs"]["images"], ["10", 0])
        self.assertEqual(p["300:trim"]["inputs"]["audio"], ["23", 0])
        self.assertEqual(p["300:trim"]["inputs"]["trim_frames"], ["300:bridge", 2])
        # 【2026-11-17】画面多走一跳「跨段统计匹配」（300:post）：
        #   它只改画质、不改帧数、不动音频 → 音频仍直接取 300:trim。
        self.assertEqual(p["300:post"]["inputs"]["images"], ["300:trim", 0])
        self.assertEqual(p["91"]["inputs"]["images"], ["300:post", 0])
        self.assertEqual(p["91"]["inputs"]["audio"], ["300:trim", 1])

    def test_selflift_bridge_reads_upstream_explicit_path(self):
        """读回必须用上游的**权威坐标**（explicit_path），不能按段号推。"""
        p = vw.build_t2v_panel(self._t2v_selflift_cont())
        load = p["300:load"]["inputs"]
        self.assertEqual(load["explicit_path"],
                         "/out/relay_kit/h3studio_sJ1/stage_00000.safetensors")
        self.assertEqual(load["run_id"], "h3studio_sJ1")

    def test_selflift_save_writes_own_directory(self):
        """落盘写**本段自己的**目录（兄弟分支不互相覆盖）。"""
        p = vw.build_t2v_panel(self._t2v_selflift_cont())
        self.assertEqual(p["300:save"]["inputs"]["run_id"], "h3studio_sJ2")
        self.assertNotEqual(p["300:save"]["inputs"]["run_id"],
                            p["300:load"]["inputs"]["run_id"])

    def test_selflift_bridge_mask_is_hard(self):
        """钉住区必须零重绘（hard 掩码）。"""
        p = vw.build_t2v_panel(self._t2v_selflift_cont())
        self.assertEqual(p["300:bridge"]["inputs"]["mask_mode"], "hard")
        self.assertTrue(p["300:bridge"]["inputs"]["pin_audio"])

    def test_plain_selflift_generation_has_no_bridge(self):
        """普通二采生成**不该**有桥 —— 加了会改变既有二采出图结果。

        链首段没有上游（source_job 为空）→ 只有 300:save，没有桥。
        """
        p = vw.build_t2v_panel(_t2v_base("J1", twopass=_twopass(),
                                         **_chain_head_params("J1")))
        self.assertNotIn("300:bridge", p)
        self.assertNotIn("300:load", p)
        self.assertNotIn("300:trim", p)
        # 但**要**落盘（二采段能产出接力棒）
        self.assertIn("300:save", p)

    def test_plain_selflift_positive_is_condition_node(self):
        """非接续段的二采 positive 仍走条件节点（行为不变）。"""
        p = vw.build_t2v_panel(_t2v_base("J1", twopass=_twopass(),
                                         **_chain_head_params("J1")))
        self.assertEqual(p["171"]["inputs"]["positive"], ["105:104", 0])


# ------------------------------------------------------------------
# 3. flf / r2v 的二采接续（条件节点是 136）
# ------------------------------------------------------------------
class TestSelfliftF2vModes(unittest.TestCase):
    """首尾帧 / 参考生的二采接续。"""

    def _mode_params(self, mode, job_id="J2"):
        params = {
            "job_id": job_id,
            "mode": mode,
            "prompt": "一只猫在跑",
            "model_key": ("minimax_h3_flf" if mode == "flf"
                          else "minimax_h3_ref2va"),
            "duration": 5.0,
            "width": 1280,
            "height": 736,
            "steps": 4,
            "seed": 12345,
            "sampler": "euler",
            "scheduler": "beta",
            "twopass": _twopass(),
        }
        # 参考媒体（flf 要首尾帧；r2v 要参考图）
        if mode == "flf":
            params["first_frame"] = "first.png"
            params["last_frame"] = "last.png"
        else:
            params["ref_images"] = ["ref0.png"]
        params.update(_cont_params(job_id))
        return params

    def _plain_mode_params(self, mode, job_id="J1"):
        params = {
            "job_id": job_id,
            "mode": mode,
            "prompt": "一只猫在跑",
            "model_key": ("minimax_h3_flf" if mode == "flf"
                          else "minimax_h3_ref2va"),
            "duration": 5.0,
            "width": 1280,
            "height": 736,
            "steps": 4,
            "seed": 12345,
            "sampler": "euler",
            "scheduler": "beta",
            "twopass": _twopass(),
        }
        if mode == "flf":
            params["first_frame"] = "first.png"
            params["last_frame"] = "last.png"
        else:
            params["ref_images"] = ["ref0.png"]
        params.update(_chain_head_params(job_id))
        return params

    def test_flf_selflift_continuation_has_bridge(self):
        """首尾帧二采接续段必须有桥。"""
        p = hvm.build_flf(self._mode_params("flf"))
        for nid in ("300:load", "300:bridge", "300:trim", "300:save"):
            self.assertIn(nid, p, "首尾帧二采接续段必须有节点 %s" % nid)

    def test_r2v_selflift_continuation_has_bridge(self):
        """参考生二采接续段必须有桥。"""
        p = hvm.build_r2v(self._mode_params("r2v"))
        for nid in ("300:load", "300:bridge", "300:trim", "300:save"):
            self.assertIn(nid, p, "参考生二采接续段必须有节点 %s" % nid)

    def test_f2v_selflift_positive_uses_bridge(self):
        """flf/r2v 的二采 positive 也走桥第 4 路。"""
        for mode, builder in (("flf", hvm.build_flf),
                              ("r2v", hvm.build_r2v)):
            p = builder(self._mode_params(mode))
            self.assertEqual(p["171"]["inputs"]["positive"], ["300:bridge", 3],
                             "%s 的二采 positive 应走桥第 4 路" % mode)
            self.assertEqual(p["171"]["inputs"]["latent_image"], ["300:bridge", 0],
                             "%s 的二采 latent 应走桥第 0 路" % mode)

    def test_f2v_selflift_save_takes_sampler_output(self):
        """flf/r2v 二采落盘也存采样器输出。"""
        for mode, builder in (("flf", hvm.build_flf),
                              ("r2v", hvm.build_r2v)):
            p = builder(self._mode_params(mode))
            self.assertEqual(p["300:save"]["inputs"]["latent"], ["171", 0],
                             "%s 的二采落盘应存采样器输出" % mode)

    def test_f2v_plain_selflift_has_no_bridge(self):
        """普通 flf/r2v 二采生成不该有桥（行为不变）。"""
        for mode, builder in (("flf", hvm.build_flf),
                              ("r2v", hvm.build_r2v)):
            p = builder(self._plain_mode_params(mode))
            self.assertNotIn("300:bridge", p, "%s 普通二采不该有桥" % mode)


# ------------------------------------------------------------------
# 4. 网格对齐（报告 §4.3.1 的结论，独立复核）
# ------------------------------------------------------------------
class TestSelfliftGridAlignment(unittest.TestCase):
    """二采落盘的 latent 时间步必须落在 17k+5 网格上。

    期望值是**真实落盘样本的实测数字**（2026-11-17 独立复核）：
      10s 段 → latent T=72；5s 段 → latent T=37。
    这些数字是从线上真实二采任务的 safetensors header 直接读出来的，
    不是从被测函数反推的。
    """

    def test_selflift_latent_t_matches_measured_samples(self):
        """二采落盘的 latent 时间步 == video_latent_t(align_frame_count(时长))。"""
        # 实测：10.0s -> fc=243 -> T=72
        self.assertEqual(cont.align_frame_count(240), 243)
        self.assertEqual(cont.video_latent_t(243), 72)
        # 实测：5.0s -> fc=124 -> T=37
        self.assertEqual(cont.align_frame_count(120), 124)
        self.assertEqual(cont.video_latent_t(124), 37)

    def test_selflift_tail_slice_is_step_aligned(self):
        """尾段切片的起始步必须落在 5 步周期边界（否则是位移型接缝）。

        实测：二采落盘样本的 tail_aligned(fc, 22) 全部 True。
        """
        for fc in (243, 124):
            self.assertTrue(cont.tail_aligned(fc, 22),
                            "fc=%d 的 22 帧窗口应落在 5 步周期边界" % fc)

    def test_selflift_context_window_is_on_grid(self):
        """钉住窗口 22 帧必须是合法网格值。"""
        self.assertTrue(cont.valid_context_frames(22))
        self.assertEqual(cont.steps_for_frames(22), 7)

    def test_selflift_continuation_frame_math(self):
        """二采接续段的帧数账（裁掉钉住区）与标准路径一致。"""
        info = cont.continuation_frames(124, 22)
        self.assertEqual(info["frame_count"], 124)
        self.assertEqual(info["latent_steps"], 37)
        self.assertEqual(info["context_frames"], 22)
        self.assertEqual(info["context_steps"], 7)
        self.assertEqual(info["trim_frames"], 22)
        self.assertEqual(info["delivered_frames"], 102)


# ------------------------------------------------------------------
# 5. 服务层：二采段缺潜空间时的**具体**中文报错
# ------------------------------------------------------------------
class TestSelfliftServiceReason(unittest.TestCase):
    """报错必须具体，不能是「服务端未能给出具体原因」。"""

    def test_selflift_missing_latent_reason_is_specific(self):
        """二采段找不到潜空间时，原因要说清「二采本身支持接续，
        但接力棒不在盘上」—— 不能让用户误以为二采不支持接续。"""
        orig = cs.stage_exists
        cs.stage_exists = lambda run_id, stage: False
        try:
            state = cs.continuation_state(
                "t2v",
                {"duration": 5.0,
                 "twopass": {"enabled": True},
                 "continuation": {"chain_id": "J1", "stage_index": 0,
                                  "latent_run_id": "h3studio_sJ1",
                                  "latent_stage": 0}},
                "J1")
        finally:
            cs.stage_exists = orig

        self.assertFalse(state["can_continue"])
        reason = state.get("reason") or ""
        self.assertNotEqual(reason, "", "必须给出原因")
        self.assertNotIn("服务端未能给出具体原因", reason)
        # 具体点：说清是二采、说清缺的是潜空间、给可执行的下一步
        self.assertIn("二采", reason)
        self.assertIn("潜空间", reason)
        self.assertIn("复用参数", reason)
        # 不能再说「二采不支持接续」（那个结论已被推翻）
        self.assertNotIn("二采不支持接续", reason)

    def test_non_selflift_missing_latent_reason_unchanged(self):
        """普通段缺潜空间的原因文案不受影响。"""
        orig = cs.stage_exists
        cs.stage_exists = lambda run_id, stage: False
        try:
            state = cs.continuation_state(
                "t2v",
                {"duration": 5.0,
                 "continuation": {"chain_id": "J1", "stage_index": 0,
                                  "latent_run_id": "h3studio_sJ1",
                                  "latent_stage": 0}},
                "J1")
        finally:
            cs.stage_exists = orig
        self.assertFalse(state["can_continue"])
        self.assertNotEqual(state.get("reason"), "")

    def test_selflift_with_latent_on_disk_is_continuable(self):
        """二采段**有**落盘潜空间时 → 可以接续（能力探测通过）。"""
        orig = cs.stage_exists
        cs.stage_exists = lambda run_id, stage: True
        try:
            state = cs.continuation_state(
                "t2v",
                {"duration": 5.0,
                 "twopass": {"enabled": True},
                 "continuation": {"chain_id": "J1", "stage_index": 0,
                                  "latent_run_id": "h3studio_sJ1",
                                  "latent_stage": 0}},
                "J1")
        finally:
            cs.stage_exists = orig
        self.assertTrue(state["can_continue"],
                        "二采段有落盘潜空间时应可接续，原因：%s"
                        % state.get("reason"))
        self.assertEqual(state.get("reason"), "")


# ------------------------------------------------------------------
# 6. 音频续接时序对二采同样适用（复用 P1 已实现的链路）
# ------------------------------------------------------------------
class TestSelfliftAudioContinuation(unittest.TestCase):
    """二采 + 参考音频 → 与标准路径同一套音频续接时序。"""

    def test_audio_plan_is_mode_independent(self):
        """音频时序是**链级**的，不看模式 —— 二采沿用同一套。

        口径：`t_a_start(n) = C_n / 24`，C_n 是整数帧，无浮点累计误差。
        """
        from h3studio import h3_audio_continuation as hac
        plan = hac.segment_audio_plan(
            consumed_frames=56,
            segment_frames=56,
            context_frames=22,
            source_duration=10.0,
        )
        # 第 2 段（C_n=56）的音频起点 = 56/24
        self.assertAlmostEqual(plan["segment_start"], 56 / 24.0, places=6)
        self.assertEqual(plan["skip_frames"], 56)
        self.assertAlmostEqual(plan["lead_blank"], 1.0, places=6)
        # 采样用音频 = 1s 空白 + 交付段时长
        self.assertAlmostEqual(plan["sampling_duration"],
                               1.0 + plan["segment_duration"], places=6)

    def test_audio_plan_second_segment_not_zero(self):
        """第 2 段的音频起点不能是 0（否则重听整条参考音频）。"""
        from h3studio import h3_audio_continuation as hac
        plan = hac.segment_audio_plan(
            consumed_frames=124,
            segment_frames=124,
            context_frames=22,
            source_duration=60.0,
        )
        self.assertNotAlmostEqual(plan["segment_start"], 0.0, places=6)
        self.assertAlmostEqual(plan["segment_start"], 124 / 24.0, places=6)
        self.assertEqual(plan["skip_frames"], 124)


# ------------------------------------------------------------------
# 7. 前端：二采开关必须被回填（否则续接段静默变标准采样）
# ------------------------------------------------------------------
class TestSelfliftFrontendRestore(unittest.TestCase):
    """前端回填二采开关。"""

    def test_continuation_js_restores_twopass(self):
        """web/continuation.js 必须把 params.twopass 传给面板。"""
        src = (REPO / "web" / "continuation.js").read_text(encoding="utf-8")
        self.assertIn("twopass: params.twopass", src,
                      "接续回填必须带上 twopass，否则二采续接段会变标准采样")

    def test_video_panel_applies_twopass_config(self):
        """面板的 applyModeConfig 必须应用 twopass 字段。"""
        src = (REPO / "web" / "video-panel.js").read_text(encoding="utf-8")
        self.assertIn("cfg.twopass", src,
                      "面板必须应用 twopass 配置")

    def test_video_panel_submits_twopass_enabled(self):
        """面板提交体必须带上 twopass.enabled。"""
        src = (REPO / "web" / "video-panel.js").read_text(encoding="utf-8")
        self.assertIn("enabled: !!videoState.twopass.enabled", src,
                      "面板提交必须带上二采开关")


if __name__ == "__main__":
    unittest.main()
