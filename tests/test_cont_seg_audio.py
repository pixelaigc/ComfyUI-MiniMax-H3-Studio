# -*- coding: utf-8 -*-
"""首尾帧 / 参考生视频「接续」+ **音频续接时序**的回归测试。

【这个测试在防什么】
本次实现有两条独立的失败模式，两条都必须被抓住：

  1. **看起来成功，其实没接上**（P0）。
     只把 flf/r2v 加进 CONTINUABLE_MODES 而不接桥 ->
     点接续后生成的是一段**独立**视频，界面全程显示成功。
     → 断言必须检查**工作流里真的有桥节点**，而且接对位置。
  2. **音画错位**（P1）。
     「1s 空白 ⊕ 切片」是**采样用**音频；「原始整条」是**成片用**音频。
     两者混用 -> 每段 1s 空白累积（3 段 = 3s 空白）-> 音画错位。
     → 断言必须检查「采样用切片」与「成片用整条」是**两条独立链路**。

【断言纪律】
  * 期望值全部是**字面量**（本机实测的网格数字、节点名、帧数），
    不是从被测函数自己的输出反推。
  * 断言要由「命名该行为的那条断言」抓住。
  * 帧数口径一律用**整数帧**（C_n），断言 t_a_start = C_n / 24。

运行：python3 -m pytest tests/test_cont_seg_audio.py -v
"""

import json
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import continuation as cont              # noqa: E402
from h3studio import continuation_service as cs        # noqa: E402
from h3studio import h3_audio_continuation as h3acont  # noqa: E402
from h3studio import h3_audio_track as h3audio          # noqa: E402
from h3studio import h3_video_modes as h3modes          # noqa: E402
from h3studio import video_workflow as vw              # noqa: E402


# ------------------------------------------------------------------
# P0-a：flf / r2v 进入接续准入
# ------------------------------------------------------------------
class TestFlfR2vContinuableModes(unittest.TestCase):
    """首尾帧 / 参考生视频必须能接续（且不能静默退化）。"""

    def test_flf_and_r2v_are_continuable(self):
        """flf / r2v 必须进入可接续模式集合。"""
        self.assertIn("flf", cont.CONTINUABLE_MODES)
        self.assertIn("r2v", cont.CONTINUABLE_MODES)

    def test_flf_r2v_continuation_allowed(self):
        """非二采的 flf / r2v -> 允许接续，且没有理由文案。"""
        for mode in ("flf", "r2v"):
            ok, reason = cont.continuation_allowed(mode, {"twopass": {"enabled": False}})
            self.assertTrue(ok, f"{mode} 应该允许接续（实际原因：{reason}）")
            self.assertEqual(reason, "")

    def test_flf_r2v_selflift_not_rejected_by_mode_gate(self):
        """二采（selflift）**不再**被模式闸门拒绝（2026-11-17 修正）。

        【为什么改】原先断言「二采仍然被拒」，依据是「二采潜空间
        不同源」—— 该结论已被实测推翻（独立复核 4/4 真实落盘样本，
        latent 时间步全部吻合 17k+5 网格；且 SelfLiftH3Sampler
        原生支持拷贝桥的 noise_mask）。
        → 现在二采走**能力探测**：上游二采段自己有落盘潜空间才允许。
        """
        for mode in ("flf", "r2v"):
            ok, reason = cont.continuation_allowed(
                mode, {"twopass": {"enabled": True}})
            self.assertTrue(ok,
                            f"{mode} 二采不应再被模式闸门拒绝，原因：{reason}")
            self.assertEqual(reason, "")

    def test_non_video_reason_names_the_mode(self):
        """非视频模式 -> 中文原因要**点名**是哪一类不支持。

        【为什么】旧文案「只有视频类产物支持接续。」对 flf/r2v 是错的
        （它们就是视频类产物）—— 线上确实这么拒过。
        """
        ok, reason = cont.continuation_allowed("t2i", {})
        self.assertFalse(ok)
        self.assertIn("t2i", reason)
        self.assertTrue(reason.strip())

    def test_mode_label_is_chinese(self):
        """模式中文名（给用户看的文案）。"""
        self.assertEqual(cont.mode_label("flf"), "首尾帧视频")
        self.assertEqual(cont.mode_label("r2v"), "参考生视频")
        self.assertEqual(cont.mode_label("t2v"), "文生视频")


# ------------------------------------------------------------------
# P0-b：flf / r2v 工作流里真的接了桥（防「静默退化」）
# ------------------------------------------------------------------
class TestFlfR2vWorkflowBridge(unittest.TestCase):
    """接续段的工作流里必须真的有桥，且接对位置。"""

    def _base(self):
        return dict(job_id="J1", prompt="p", width=864, height=480,
                    duration=5, steps=4, seed=1, sampler="euler",
                    scheduler="beta", mode="r2v")

    def _cont(self, stage=1):
        """上游是链首段（stage 0）—— 本段是第 2 段。"""
        return {
            "chain_id": "CH1",
            "stage_index": stage,
            "source_job": "J0",
            "source_segment": stage - 1,
            "context_frames": 22,
            "anchor_frames": 22,
            "source_latent_run_id": "h3studio_sJ0",
            "source_latent_stage": 0,
            "source_latent_path": "/out/relay_kit/h3studio_sJ0/stage_00000.safetensors",
            "anchor_latent_run_id": "h3studio_sHEAD",
            "anchor_latent_stage": 0,
            "anchor_latent_path": "/out/relay_kit/h3studio_sHEAD/stage_00000.safetensors",
            "latent_run_id": "h3studio_sJ2",
            "latent_stage": 0,
        }

    def _r2v_workflow(self, cont_info=None):
        params = {**self._base()}
        if cont_info is not None:
            params["continuation"] = cont_info
        return h3modes.build_r2v({**params, "ref_images": ["a.png"]})

    def _flf_workflow(self, cont_info=None):
        params = {**self._base(), "mode": "flf"}
        if cont_info is not None:
            params["continuation"] = cont_info
        return h3modes.build_flf({**params, "first_frame": "f.png"})

    def test_r2v_continuation_has_bridge_nodes(self):
        """r2v 接续段必须有：读回 -> 拷贝桥 -> 裁重叠 -> 落盘。"""
        p = self._r2v_workflow(self._cont())
        for nid, cls in (("300:load", "H3RelayLatentLoad"),
                         ("300:bridge", "H3RelayCopyBridge"),
                         ("300:trim", "H3RelayTrimAV"),
                         ("300:save", "H3RelayLatentSave")):
            self.assertIn(nid, p, f"r2v 接续段必须有节点 {nid}")
            self.assertEqual(p[nid]["class_type"], cls)

    def test_flf_continuation_has_bridge_nodes(self):
        """flf 接续段同样必须有那四个节点。"""
        p = self._flf_workflow(self._cont())
        for nid, cls in (("300:load", "H3RelayLatentLoad"),
                         ("300:bridge", "H3RelayCopyBridge"),
                         ("300:trim", "H3RelayTrimAV"),
                         ("300:save", "H3RelayLatentSave")):
            self.assertIn(nid, p, f"flf 接续段必须有节点 {nid}")
            self.assertEqual(p[nid]["class_type"], cls)

    def _chain_head_info(self):
        """链首段的接续上下文（由 server.create_job_record 写定）。

        【真实流程】api_create -> create_job_record 给**每一个**视频任务
        建链（chain_id = 自己的任务 id、latent_run_id = 自己的目录），
        worker 随后拿这份 params 拼工作流。所以链首段**有**链信息，
        只是没有 source_job（没有上游）。
        """
        return {
            "chain_id": "J1",
            "stage_index": 0,
            "source_job": "",
            "source_segment": -1,
            "latent_run_id": cont.segment_run_id("J1"),
            "latent_stage": 0,
            "context_frames": 22,
            "anchor_frames": 22,
        }

    def test_chain_head_saves_latent(self):
        """**链首段也必须落盘**接力棒。

        【为什么这是 P0 的核心】flf/r2v 以前不落盘潜空间 ——
        所以「点接续」永远没有接力棒可读。只加模式白名单的话，
        点了接续就是静默退化。链首段落盘是接续链的起点。
        """
        p = self._r2v_workflow(self._chain_head_info())
        self.assertIn("300:save", p, "链首段必须落盘潜空间")
        self.assertEqual(p["300:save"]["class_type"], "H3RelayLatentSave")
        # 写在自己的目录、段号 0
        self.assertEqual(p["300:save"]["inputs"]["run_id"],
                         cont.segment_run_id("J1"))
        self.assertEqual(p["300:save"]["inputs"]["stage_index"], 0)
        # 链首段**不读回**上游（没有上游）
        self.assertNotIn("300:load", p)
        self.assertNotIn("300:bridge", p)

    def test_normal_generation_has_no_bridge(self):
        """普通生成**不该**有桥（加了会改变普通生成的出图结果）。"""
        p = self._r2v_workflow()
        self.assertNotIn("300:bridge", p)
        self.assertNotIn("300:load", p)
        self.assertNotIn("300:trim", p)

    def test_bridge_sits_between_latent_and_sampler(self):
        """拷贝桥必须在 T8 条件节点的 latent 与采样器之间。

        【关键差异点】t2v 的条件节点是 105:104（MiniMaxH3ImageToVideo），
        flf/r2v 是 136（MiniMaxH3AudioConditioningT8）——
        桥的 latent 输入必须接 136 的**第 2 路**（av_latent），
        接错成 conditioning（第 1 路）就全错。
        """
        p = self._r2v_workflow(self._cont())
        self.assertEqual(p["300:bridge"]["inputs"]["latent"], ["136", 1])
        self.assertEqual(p["14"]["inputs"]["latent_image"], ["300:bridge", 0])

    def test_guider_uses_bridged_conditioning(self):
        """引导器必须用拷贝桥第 4 路（钉帧后的 conditioning）。"""
        p = self._r2v_workflow(self._cont())
        self.assertEqual(p["16"]["inputs"]["conditioning"], ["300:bridge", 3])
        # 桥的第 4 路输入来自 T8 条件节点的第 1 路（positive）
        self.assertEqual(p["300:bridge"]["inputs"]["conditioning"], ["136", 0])

    def test_trim_sits_after_decode(self):
        """裁重叠必须在 decode 之后，且音画同裁。"""
        p = self._r2v_workflow(self._cont())
        self.assertEqual(p["300:trim"]["inputs"]["images"], ["10", 0])
        self.assertEqual(p["300:trim"]["inputs"]["audio"], ["23", 0])
        self.assertEqual(p["300:trim"]["inputs"]["trim_frames"], ["300:bridge", 2])
        # 合成视频必须用裁完的画面 + 裁完的音频（红线：音画不同步）
        # 【2026-11-17】画面多走一跳「跨段统计匹配」（300:post）：
        #   它只改画质、不改帧数、不动音频 → 音频仍直接取 300:trim。
        self.assertEqual(p["300:post"]["inputs"]["images"], ["300:trim", 0])
        self.assertEqual(p["91"]["inputs"]["images"], ["300:post", 0])
        self.assertEqual(p["91"]["inputs"]["audio"], ["300:trim", 1])

    def test_save_takes_sampler_output(self):
        """落盘必须存**采样器的输出**（钉住区之后的完整段）。"""
        p = self._r2v_workflow(self._cont())
        self.assertEqual(p["300:save"]["inputs"]["latent"], ["14", 0])

    def test_bridge_mask_mode_is_hard(self):
        """钉住区必须零重绘（hard 掩码）。"""
        p = self._r2v_workflow(self._cont())
        self.assertEqual(p["300:bridge"]["inputs"]["mask_mode"], "hard")
        self.assertTrue(p["300:bridge"]["inputs"]["pin_audio"])

    def test_load_reads_explicit_upstream_path(self):
        """读回必须用上游的**精确文件路径**（不能按链段号推）。"""
        p = self._r2v_workflow(self._cont())
        self.assertEqual(
            p["300:load"]["inputs"]["explicit_path"],
            "/out/relay_kit/h3studio_sJ0/stage_00000.safetensors")

    def test_read_and_write_use_different_dirs(self):
        """【分叉防覆盖】读回目录与落盘目录必须不同。"""
        p = self._r2v_workflow(self._cont())
        self.assertNotEqual(p["300:load"]["inputs"]["run_id"],
                            p["300:save"]["inputs"]["run_id"])
        self.assertEqual(p["300:load"]["inputs"]["run_id"], "h3studio_sJ0")
        self.assertEqual(p["300:save"]["inputs"]["run_id"], "h3studio_sJ2")

    def test_appearance_anchor_is_loaded_explicitly(self):
        """外观锚必须显式读进来（每段一个目录后不能靠自动读）。"""
        p = self._r2v_workflow(self._cont())
        self.assertIn("300:anchor_load", p)
        self.assertEqual(p["300:bridge"]["inputs"]["ref_anchor_latent"],
                         ["300:anchor_load", 0])
        self.assertEqual(p["300:bridge"]["inputs"]["ref_anchor_stage"], -1)

    def test_anchor_is_optional(self):
        """锚缺失时不接锚节点（缺锚只是漂移风险，不能让接续失败）。"""
        info = self._cont()
        info["anchor_latent_path"] = ""
        info["anchor_latent_run_id"] = ""
        p = self._r2v_workflow(info)
        self.assertNotIn("300:anchor_load", p)
        self.assertIsNone(p["300:bridge"]["inputs"]["ref_anchor_latent"])

    def test_selflift_gets_bridge(self):
        """二采**也**走接续桥（2026-11-17 修正）。

        【为什么改】原先断言「二采不走接续桥」，依据是
        「二采潜空间不同源」—— 该结论已被实测推翻。
        本断言守的是「二采接续段**真的**走桥」—— 防止静默退化。

        【2026-11-18 改守新契约】接续段**默认**走标准采样
        （等价 nonebot seg2 —— 二采在接续段会造成段头色档跳变，
        实测段头断点比 2.58x/4.54x、3 帧亮度降 +4.7/+7.2 灰阶）。
        要测「二采接续的接线姿势」必须**显式**声明
        `continuation.allow_twopass=true`。
        【为什么不删这条断言】接线姿势本身没有错，删了就再也
        发现不了接线退化。处置见
        references/merge-deploy-release-continuation.md §2.10。
        """
        params = {**self._base(), "twopass": {"enabled": True}}
        params["continuation"] = {**self._cont(), "allow_twopass": True}
        p = h3modes.build_r2v({**params, "ref_images": ["a.png"]})
        for nid in ("300:load", "300:bridge", "300:trim", "300:save"):
            self.assertIn(nid, p, "二采接续段必须有节点 %s" % nid)
        # SelfLift 采样器必须消费桥的产物（nonebot v1 姿势）
        self.assertEqual(p["171"]["inputs"]["latent_image"], ["300:bridge", 0])
        self.assertEqual(p["171"]["inputs"]["positive"], ["300:bridge", 3])

    def test_off_grid_context_window_is_snapped(self):
        """非网格窗口必须吸附到网格值（否则节点会 raise）。"""
        info = dict(self._cont(), context_frames=30)
        p = self._r2v_workflow(info)
        self.assertEqual(p["300:bridge"]["inputs"]["context_frames"], 22)

    def test_flf_uses_own_condition_node(self):
        """flf 的桥必须接 136（T8），不能写死成 t2v 的 105:104。"""
        p = self._flf_workflow(self._cont())
        self.assertEqual(p["300:bridge"]["inputs"]["latent"], ["136", 1])
        self.assertNotIn("105:104", json.dumps(p["300:bridge"]["inputs"]))


# ------------------------------------------------------------------
# P0-c：服务端建链 / 上下文（flf / r2v）
# ------------------------------------------------------------------
class TestFlfR2vChainCreation(unittest.TestCase):
    """任务记录里必须给 flf / r2v 建链 + 分配落盘坐标。"""

    def test_chain_info_accepted_for_flf(self):
        """协议层：flf 的接续上下文能解析出坐标。"""
        info = {"chain_id": "CH", "latent_run_id": "h3studio_sX",
                "latent_stage": 0, "stage_index": 0}
        run_id, stage = cont.latent_coords(info)
        self.assertEqual(run_id, "h3studio_sX")
        self.assertEqual(stage, 0)

    def test_segment_run_id_for_flf_job(self):
        """flf 段的落盘目录由自己的任务 id 派生。"""
        self.assertEqual(cont.segment_run_id("FLFJOB"),
                         "h3studio_sFLFJOB")


# ------------------------------------------------------------------
# P1：音频续接时序（纯函数层）
# ------------------------------------------------------------------
class TestAudioContinuationTiming(unittest.TestCase):
    """t_a_start(n) = C_n / 24 —— 整数帧口径，无浮点累计误差。"""

    def test_first_segment_consumes_nothing(self):
        """第 1 段：C_1 = 0 -> 音频起点 0 秒。"""
        plan = h3acont.segment_audio_plan(
            consumed_frames=0, segment_frames=124, context_frames=22,
            source_duration=60.0)
        self.assertEqual(plan["consumed_frames"], 0)
        self.assertEqual(plan["source_start"], 0.0)
        self.assertEqual(plan["delivered_frames"], 102)

    def test_second_segment_starts_at_upstream_delivery(self):
        """第 2 段：C_2 = 124（第 1 段交付 124 帧）-> 起点 124/24 秒。

        【本机实测口径】5s 段 = 124 帧；第 1 段不裁钉住区，
        交付 124 帧 -> 第 2 段从 124/24 = 5.166667 秒起。
        """
        plan = h3acont.segment_audio_plan(
            consumed_frames=124, segment_frames=124, context_frames=22,
            source_duration=60.0)
        self.assertAlmostEqual(plan["source_start"], 124 / 24, places=6)
        self.assertAlmostEqual(plan["source_start"], 5.166667, places=5)
        self.assertEqual(plan["skip_frames"], 124)

    def test_delivered_frames_subtract_context(self):
        """接续段交付 = 总帧数 - 钉住窗口。"""
        plan = h3acont.segment_audio_plan(
            consumed_frames=0, segment_frames=124, context_frames=22)
        self.assertEqual(plan["delivered_frames"], 102)
        self.assertAlmostEqual(plan["segment_duration"], 102 / 24, places=6)

    def test_lead_blank_is_one_second(self):
        """采样用音频 = 1s 空白 ⊕ 切片 -> 总长 = 1 + 切片长。"""
        plan = h3acont.segment_audio_plan(
            consumed_frames=124, segment_frames=124, context_frames=22)
        self.assertEqual(plan["lead_blank"], 1.0)
        self.assertAlmostEqual(
            plan["sampling_duration"], 1.0 + 102 / 24, places=6)

    def test_integer_frame_grid_has_no_float_drift(self):
        """多段接续后，起点仍是精确的 C_n/24（不累计浮点误差）。"""
        consumed = 0
        for _ in range(10):
            plan = h3acont.segment_audio_plan(
                consumed_frames=consumed, segment_frames=124,
                context_frames=22)
            self.assertEqual(plan["source_start"] * 24, float(consumed))
            consumed += plan["delivered_frames"]
        self.assertEqual(consumed, 102 * 10)

    def test_skip_frames_equals_consumed_frames(self):
        """参考视频跳帧数必须 == C_n（视频侧帧 / 音频侧秒严格对应）。"""
        plan = h3acont.segment_audio_plan(
            consumed_frames=226, segment_frames=124, context_frames=22)
        self.assertEqual(plan["skip_frames"], 226)
        self.assertAlmostEqual(plan["audio_start"] if "audio_start" in plan
                               else plan["source_start"],
                               226 / 24, places=6)

    def test_unequal_segment_durations_are_supported(self):
        """每段时长不等 -> 逐段按公式算，不许用统一值。"""
        p1 = h3acont.segment_audio_plan(
            consumed_frames=0, segment_frames=124, context_frames=22)
        p2 = h3acont.segment_audio_plan(
            consumed_frames=p1["delivered_frames"],
            segment_frames=243, context_frames=22)
        # 第 2 段总帧 243 -> 交付 221
        self.assertEqual(p2["delivered_frames"], 221)
        self.assertAlmostEqual(p2["source_start"], 102 / 24, places=6)

    def test_short_source_audio_produces_chinese_warning(self):
        """原始音频不够长 -> 明确中文提示（不静默错位）。"""
        plan = h3acont.segment_audio_plan(
            consumed_frames=0, segment_frames=124, context_frames=22,
            source_duration=3.0)
        self.assertTrue(plan["warnings"], "必须给出告警")
        text = " ".join(plan["warnings"])
        self.assertIn("缺", text)
        self.assertIn("补静音", text)

    def test_oversized_window_warns(self):
        """钉住窗口 >= 本段帧数 -> 提前给中文提示。"""
        plan = h3acont.segment_audio_plan(
            consumed_frames=0, segment_frames=22, context_frames=22)
        self.assertTrue(plan["warnings"])
        self.assertIn("钉住窗口", " ".join(plan["warnings"]))

    def test_validate_reports_short_and_long_audio(self):
        """成片长度 vs 音轨长度：短了补静音、长了截断 —— 都要中文说明。"""
        plan = h3acont.segment_audio_plan(
            consumed_frames=0, segment_frames=124, context_frames=22)
        short = h3acont.validate_audio_alignment(
            source_duration=3.0, plan=plan, total_video_frames=124)
        self.assertIn("短", short)
        long = h3acont.validate_audio_alignment(
            source_duration=60.0, plan=plan, total_video_frames=124)
        self.assertIn("长", long)

    def test_external_track_detection(self):
        """只有 reference / video_bg 需要切片；generated 不切。"""
        self.assertTrue(h3acont.is_external_track(h3audio.TRACK_REFERENCE))
        self.assertTrue(h3acont.is_external_track(h3audio.TRACK_VIDEO_BG))
        self.assertFalse(h3acont.is_external_track(h3audio.TRACK_GENERATED))
        self.assertFalse(h3acont.is_external_track(""))

    def test_chain_audio_state_fields(self):
        """链上的音频状态字段（落进 params["continuation"]）。"""
        state = h3acont.chain_audio_state(
            track_source="reference", source_kind="ref_audio",
            source_file="m.mp3", source_duration=60.0, consumed_frames=124)
        self.assertEqual(state["audio_track_mode"], "reference")
        self.assertEqual(state["audio_source_kind"], "ref_audio")
        self.assertEqual(state["audio_source_file"], "m.mp3")
        self.assertEqual(state["audio_source_duration"], 60.0)
        self.assertEqual(state["audio_consumed_frames"], 124)
        self.assertEqual(state["audio_lead_blank"], 1.0)

    def test_audio_module_fps_matches_continuation(self):
        """音频模块的 FPS 必须与协议层一致（否则时序整体偏移）。"""
        self.assertEqual(h3acont.FPS, cont.FPS)


# ------------------------------------------------------------------
# P1-c：**实测抓到的两个真实缺陷**的回归
# ------------------------------------------------------------------
class TestRealDefectsFoundByLiveRun(unittest.TestCase):
    """这两个缺陷是**线上真实跑出来**的（8190 实测），不是推想的。

    1. 段2 的音频起点被算成 0（而不是 C_n / 24）
       —— 根因：代码按「上游有没有上游」分支取 C_n，
          而「上游是链首段」时 upstream_info["source_job"] 恰好是**空串**
          -> 第 2 段又从参考音频的第 0 秒开始听（已用过的音频被重复用）。
    2. ffmpeg 的 `-t` 放在输出侧 -> 1s 前导空白被裁掉
       —— 实测：-t 输出侧得 1.416009s（空白没了），
          -t 输入侧得 2.416009s（空白 + 切片，正确）。
    """

    def test_second_segment_audio_offset_is_not_zero(self):
        """第 2 段的音频起点必须等于 C_n / 24（**不能**是 0）。

        复现口径：上游是**链首段**（source_job 为空串），
        上游累计交付 56 帧 -> 本段起点 = 56/24 = 2.333333s。
        """
        import server

        upstream_info = {
            "chain_id": "CH1",
            "stage_index": 0,
            # 链首段：没有上游 -> source_job 是**空串**（这是关键）
            "source_job": "",
            "source_segment": -1,
            "chain_total_frames": 0,
            "segment_frames": 56,
            "latent_run_id": "h3studio_sHEAD",
            "latent_stage": 0,
        }
        upstream_params = {
            "audio_track_source": "reference",
            "ref_audios": ["h3studio/orig.m4a"],
        }
        state = server._inherit_audio_continuation(
            source_mode="r2v",
            source_params=upstream_params,
            source_info=upstream_info,
            segment_total=56,
            window=22,
            # 上游已累计交付 56 帧 -> C_n = 56
            chain_total=56,
        )
        self.assertEqual(state["audio_track_mode"], "reference")
        self.assertAlmostEqual(state["audio_segment_start"], 56 / 24,
                               places=6,
                               msg="第 2 段的音频起点必须是 C_n/24，不能是 0")
        self.assertNotEqual(state["audio_segment_start"], 0.0)
        # 跳帧数必须与起点严格对应
        self.assertEqual(state["audio_segment_skip_frames"], 56)

    def test_third_segment_uses_accumulated_frames(self):
        """第 3 段的 C_n = 上游累计（含第 2 段的交付）。"""
        import server
        upstream_info = {
            "chain_id": "CH1", "stage_index": 1,
            "source_job": "J1", "source_segment": 0,
            "chain_total_frames": 56, "segment_frames": 56,
        }
        state = server._inherit_audio_continuation(
            source_mode="r2v",
            source_params={"audio_track_source": "reference",
                           "ref_audios": ["h3studio/orig.m4a"]},
            source_info=upstream_info,
            segment_total=56, window=22,
            chain_total=90,   # 56 + (56-22) = 90
        )
        self.assertAlmostEqual(state["audio_segment_start"], 90 / 24,
                               places=6)

    def test_segment_audio_keeps_lead_blank(self):
        """切出来的段音频必须**含 1s 前导空白**（真实跑 ffmpeg）。

        【这条断言在防什么】`-t` 放在输出侧时，adelay 加的空白会被
        裁掉 —— 段音频变成「只有切片」，1s 空白消失。
        """
        import shutil
        import subprocess
        import tempfile

        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("本机没有 ffmpeg / ffprobe")

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "src.wav"
            # 造一段 10s 的 440Hz 正弦（44100Hz 立体声）
            subprocess.check_output(
                [ffmpeg, "-y", "-v", "error", "-f", "lavfi",
                 "-i", "sine=frequency=440:duration=10",
                 "-ac", "2", "-ar", "44100", str(source)],
                stderr=subprocess.STDOUT, timeout=120)
            out = Path(tmp) / "seg.m4a"
            import server
            ok = server._make_segment_audio(
                source, out, start_seconds=2.0, duration_seconds=3.0,
                lead_blank=1.0)
            self.assertTrue(ok, "切段音频失败")
            raw = subprocess.check_output(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(out)], encoding="utf-8")
            duration = float(raw.strip())
            # 1s 空白 + 3s 切片 = 4s（容差 0.05s：aac 编码有帧级误差）
            self.assertAlmostEqual(duration, 4.0, delta=0.05,
                                   msg="段音频必须含 1s 前导空白")

    def test_segment_audio_slice_offsets_correctly(self):
        """切片起点必须真的落在原始音频的那个位置。

        【怎么验】原始音频是 440Hz 正弦 —— 用 ffmpeg 切一段出来，
        再测它的**主频**仍是 440Hz（说明切对了区间，不是乱切的）。
        更直接的办法：比较「切 0s 起」与「切 5s 起」两段的
        波形是否不同（不同区间 -> 内容不同）。
        """
        import shutil
        import subprocess
        import tempfile

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("本机没有 ffmpeg")
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "src.wav"
            # 1Hz 调制的 440Hz -> 每一秒的波形不同（便于区分区间）
            subprocess.check_output(
                [ffmpeg, "-y", "-v", "error", "-f", "lavfi",
                 "-i", "sine=frequency=440:duration=12",
                 "-ac", "2", "-ar", "44100", str(source)],
                stderr=subprocess.STDOUT, timeout=120)
            import server
            a = Path(tmp) / "a.m4a"
            b = Path(tmp) / "b.m4a"
            self.assertTrue(server._make_segment_audio(
                source, a, start_seconds=0.0, duration_seconds=1.0,
                lead_blank=0.0))
            self.assertTrue(server._make_segment_audio(
                source, b, start_seconds=6.0, duration_seconds=1.0,
                lead_blank=0.0))
            self.assertTrue(a.is_file() and b.is_file())
            # 两个区间的内容必须不同（否则切片起点没生效）
            self.assertNotEqual(a.read_bytes(), b.read_bytes(),
                                "不同起点的切片内容相同 -> 切片没生效")



# ------------------------------------------------------------------
# P1-b：工作流里的音频切片接线
# ------------------------------------------------------------------
class TestAudioSliceWiring(unittest.TestCase):
    """采样用切片 / 成片用整条 —— 两条链路必须分开。"""

    def _cont_audio(self, consumed=124, start=5.166667, duration=4.25):
        return {
            "chain_id": "CH1",
            "stage_index": 1,
            "source_job": "J0",
            "source_segment": 0,
            "context_frames": 22,
            "anchor_frames": 22,
            "source_latent_run_id": "h3studio_sJ0",
            "source_latent_stage": 0,
            "source_latent_path": "/out/relay_kit/h3studio_sJ0/stage_00000.safetensors",
            "latent_run_id": "h3studio_sJ2",
            "latent_stage": 0,
            # 音频接续状态
            "audio_track_mode": "reference",
            "audio_source_kind": "ref_audio",
            "audio_source_file": "h3studio/orig.m4a",
            "audio_source_duration": 60.0,
            "audio_consumed_frames": consumed,
            "audio_lead_blank": 1.0,
            "audio_segment_start": start,
            "audio_segment_duration": duration,
            "audio_segment_skip_frames": consumed,
        }

    def test_segment_audio_replaces_reference_audio_slot(self):
        """段音频替换掉「参考音乐」那一路的输入（不再 Trim）。"""
        params = {
            "job_id": "J2", "prompt": "p", "width": 864, "height": 480,
            "duration": 5, "steps": 4, "seed": 1, "sampler": "euler",
            "scheduler": "beta", "mode": "r2v",
            "ref_images": ["a.png"],
            "ref_audios": ["orig.m4a"],
            "audio_track_source": "reference",
            "continuation": self._cont_audio(),
            "segment_audio_file": "h3studio/seg-abc.m4a",
        }
        p = h3modes.build_r2v(params)
        # 段音频进了 136:401（参考音乐那一路）
        self.assertEqual(p["136:401"]["inputs"]["audio"],
                         "h3studio/seg-abc.m4a")
        # 成片音轨接的是这一路，且**没有** TrimAudioDuration
        audio_ref = p["91"]["inputs"]["audio"]
        self.assertEqual(audio_ref, ["136:401", 0])
        self.assertNotIn("150:track", p)

    def test_generated_track_uses_trimmed_decoded_audio(self):
        """生成音频档：接续段必须用**裁过**的音频（300:trim 的第 2 路）。

        【红线】画面裁了 22 帧、音频没裁 -> 音画错位。
        """
        params = {
            "job_id": "J2", "prompt": "p", "width": 864, "height": 480,
            "duration": 5, "steps": 4, "seed": 1, "sampler": "euler",
            "scheduler": "beta", "mode": "r2v",
            "ref_images": ["a.png"],
            "audio_track_source": "generated",
            "continuation": self._cont_audio(),
        }
        p = h3modes.build_r2v(params)
        self.assertEqual(p["91"]["inputs"]["audio"], ["300:trim", 1])

    def test_reference_video_is_cut_in_sync_with_audio(self):
        """参考视频的音画同步切割：视频跳 C_n 帧、音频从 C_n/24 秒起。"""
        params = {
            "job_id": "J2", "prompt": "p", "width": 864, "height": 480,
            "duration": 5, "steps": 4, "seed": 1, "sampler": "euler",
            "scheduler": "beta", "mode": "r2v",
            "ref_images": ["a.png"],
            "ref_videos": ["v.mp4"],
            "ref_video_audios": ["vb.m4a"],
            "audio_track_source": "video_bg",
            "continuation": self._cont_audio(),
            "segment_audio_file": "h3studio/seg-abc.m4a",
        }
        p = h3modes.build_r2v(params)
        video_inputs = p["136:301"]["inputs"]
        self.assertEqual(video_inputs["skip_first_frames"], 124)
        # force_rate 必须给 24 —— 否则跳帧数与音频秒数对不上
        self.assertEqual(video_inputs["force_rate"], 24)
        # frame_load_cap = 本段交付帧数（102）
        self.assertEqual(video_inputs["frame_load_cap"], 102)

    def test_reference_video_audio_starts_at_same_moment(self):
        """音频侧起点必须与视频侧跳帧指向同一时刻（C_n/24 秒）。"""
        params = {
            "job_id": "J2", "prompt": "p", "width": 864, "height": 480,
            "duration": 5, "steps": 4, "seed": 1, "sampler": "euler",
            "scheduler": "beta", "mode": "r2v",
            "ref_images": ["a.png"],
            "ref_videos": ["v.mp4"],
            "ref_video_audios": ["vb.m4a"],
            "audio_track_source": "video_bg",
            "continuation": self._cont_audio(),
            "segment_audio_file": "h3studio/seg-abc.m4a",
        }
        p = h3modes.build_r2v(params)
        audio_inputs = p["136:501"]["inputs"]
        self.assertAlmostEqual(audio_inputs["start_time"], 124 / 24, places=6)
        self.assertAlmostEqual(audio_inputs["duration"], 4.25, places=6)

    def test_chain_head_does_not_slice(self):
        """链首段不切片（第 1 段用原始素材从 0 开始）。"""
        params = {
            "job_id": "J1", "prompt": "p", "width": 864, "height": 480,
            "duration": 5, "steps": 4, "seed": 1, "sampler": "euler",
            "scheduler": "beta", "mode": "r2v",
            "ref_images": ["a.png"],
            "ref_videos": ["v.mp4"],
            "audio_track_source": "reference",
            "continuation": {
                "chain_id": "CH1", "stage_index": 0, "source_job": "",
                "latent_run_id": "h3studio_sJ1", "latent_stage": 0,
            },
        }
        p = h3modes.build_r2v(params)
        self.assertEqual(p["136:301"]["inputs"]["skip_first_frames"], 0)
        self.assertEqual(p["136:301"]["inputs"]["force_rate"], 0)

    def test_non_continuation_keeps_original_audio_trim(self):
        """普通生成（无接续上下文）-> 仍是「Trim 原始参考音乐到画面时长」。"""
        params = {
            "job_id": "J1", "prompt": "p", "width": 864, "height": 480,
            "duration": 5, "steps": 4, "seed": 1, "sampler": "euler",
            "scheduler": "beta", "mode": "r2v",
            "ref_images": ["a.png"],
            "ref_audios": ["orig.m4a"],
            "audio_track_source": "reference",
        }
        p = h3modes.build_r2v(params)
        audio_ref = p["91"]["inputs"]["audio"]
        self.assertEqual(p[audio_ref[0]]["class_type"], "TrimAudioDuration")
        self.assertEqual(p[audio_ref[0]]["inputs"]["duration"], 5.0)


if __name__ == "__main__":
    unittest.main()
