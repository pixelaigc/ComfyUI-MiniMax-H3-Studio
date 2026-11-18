# -*- coding: utf-8 -*-
"""接续段默认走标准采样路径（等价 nonebot minimax_h3_seg2）的契约测试。

【背景】
2026-11-18 实测：接续链在**段 2 内部 frame0→1** 出现硬切式色调跳变。
根因 = 二采（SelfLift）在接续段的表现，不是拼接缝问题。
nonebot 的 seg1/seg2 用**标准 SamplerCustomAdvanced**（无 SelfLift）
⇒ 「等价 seg2」= 接续段默认走标准采样。

【本文件守的不变量】
  I1. 接续段（continuation.source_job 非空）产出的工作流里
      **必须有 SamplerCustomAdvanced，且绝不能有 SelfLiftH3Sampler**。
      （t2v / flf / r2v 三路都守）
  I2. 普通段 / 首段仍然**允许**二采（不能把能力一刀切掉）。
  I3. 服务端归一层是**权威**：入参 twopass.enabled=true 的接续段，
      归一后 out["twopass"]["enabled"] 必须是 false，且留中文留痕。
  I4. 归一层与工作流层**同口径**（都看 source_job）—— 不允许分裂。
  I5. 显式口子：continuation.allow_twopass=true 时接续段可用二采，
      但必须留**警告**留痕（不能静默放行）。

【为什么这些断言不是恒真】
见 tests/mutation_twopass_seg2_policy.py —— 每个变异都会让对应断言变红。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from h3studio import h3_video_modes as hvm  # noqa: E402
from h3studio import twopass_policy as tp  # noqa: E402
from h3studio import video_workflow as vw  # noqa: E402


_BASE = {
    "model_key": "fl2va_pruned_int8",
    "width": 1280,
    "height": 736,
    "duration": 5,
    "seed": 123,
    "steps": 4,
    "sampler": "euler",
    "scheduler": "beta",
    "shift_video": 12.0,
    "shift_audio": 3.0,
    "attention": "default",
    "accel_lora": "",
    "accel_lora_strength": 1.0,
    "effect_loras": [],
    "cfg": 1.0,
    "aspect": "16:9",
    "job_id": "policytestjob0001",
}


def _cont(stage_index: int = 1) -> dict:
    """一个「续接段」的 continuation 片段。"""
    return {"source_job": "upstreamjob0001", "stage_index": stage_index,
            "chain_id": "chainpolicy0001"}


def _first_seg() -> dict:
    """首段的 continuation 片段（source_job 为空）。"""
    return {"source_job": "", "stage_index": 0, "chain_id": "chainpolicy0001"}


def _class_types(wf: dict) -> list:
    return [v.get("class_type") for v in wf.values() if isinstance(v, dict)]


class TestContinuationProducesStandardSampler(unittest.TestCase):
    """I1：接续段必须是标准采样节点、无 SelfLift。"""

    def _assert_standard(self, wf, label):
        cts = _class_types(wf)
        self.assertIn(
            "SamplerCustomAdvanced", cts,
            f"{label}: 接续段必须产出标准 SamplerCustomAdvanced")
        self.assertNotIn(
            "SelfLiftH3Sampler", cts,
            f"{label}: 接续段绝不能产出 SelfLiftH3Sampler")
        self.assertFalse(
            [c for c in cts if c and "SelfLift" in c],
            f"{label}: 接续段不能有任何 SelfLift 系节点")

    def test_t2v_continuation_is_standard_sampler(self):
        wf = vw.build_t2v_panel({
            **_BASE, "twopass": {"enabled": True}, "continuation": _cont()})
        self._assert_standard(wf, "t2v 接续段")

    def test_flf_continuation_is_standard_sampler(self):
        wf = hvm.build_flf({
            **_BASE, "twopass": {"enabled": True},
            "first_frame": "a.png", "continuation": _cont()})
        self._assert_standard(wf, "flf 接续段")

    def test_r2v_continuation_is_standard_sampler(self):
        wf = hvm.build_r2v({
            **_BASE, "twopass": {"enabled": True},
            "ref_images": ["a.png"], "model_key": "hybrid_fl2va_ref2va",
            "continuation": _cont()})
        self._assert_standard(wf, "r2v 接续段")

    def test_continuation_standard_sampler_wiring_matches_seg2(self):
        """接线姿势必须与 nonebot seg2 等价。

        nonebot `minimax_h3_seg2.json`：
          272 SamplerCustomAdvanced {noise:[273,0], guider:[274,0],
                                    sampler:[123,0], sigmas:[124,0],
                                    latent_image:["322",0]}
          273 RandomNoise / 274 BasicGuider{conditioning:["322",3]}
          123 KSamplerSelect / 124 BasicScheduler
        ⇒ 标准采样的 5 个节点必须齐、且 latent_image 必须接拷贝桥第 0 路。
        """
        wf = vw.build_t2v_panel({
            **_BASE, "twopass": {"enabled": True}, "continuation": _cont()})
        cts = _class_types(wf)
        for required in ("SamplerCustomAdvanced", "RandomNoise",
                         "BasicGuider", "KSamplerSelect", "BasicScheduler"):
            self.assertIn(required, cts, f"缺 {required}（seg2 等价性）")
        # 拷贝桥必须在，且 sampler 的 latent_image 接桥第 0 路
        self.assertIn("H3RelayCopyBridge", cts)
        sampler = next(v for v in wf.values()
                       if isinstance(v, dict)
                       and v.get("class_type") == "SamplerCustomAdvanced")
        self.assertEqual(sampler["inputs"]["latent_image"], ["300:bridge", 0],
                         "latent_image 必须接拷贝桥第 0 路（与 seg2 一致）")
        guider = next(v for v in wf.values()
                      if isinstance(v, dict)
                      and v.get("class_type") == "BasicGuider")
        self.assertEqual(guider["inputs"]["conditioning"], ["300:bridge", 3],
                         "conditioning 必须接拷贝桥第 4 路（钉帧写进 conditioning）")


class TestPlainSegmentKeepsTwopass(unittest.TestCase):
    """I2：普通段 / 首段不能被一刀切掉二采能力。"""

    def test_t2v_first_segment_still_allows_twopass(self):
        wf = vw.build_t2v_panel({
            **_BASE, "twopass": {"enabled": True}, "continuation": _first_seg()})
        self.assertIn("SelfLiftH3Sampler", _class_types(wf),
                      "首段必须仍然能用二采（能力不能被一刀切掉）")

    def test_t2v_plain_generation_allows_twopass(self):
        wf = vw.build_t2v_panel({**_BASE, "twopass": {"enabled": True}})
        self.assertIn("SelfLiftH3Sampler", _class_types(wf))

    def test_flf_plain_allows_twopass(self):
        wf = hvm.build_flf({
            **_BASE, "twopass": {"enabled": True}, "first_frame": "a.png"})
        self.assertIn("SelfLiftH3Sampler", _class_types(wf))

    def test_r2v_plain_allows_twopass(self):
        wf = hvm.build_r2v({
            **_BASE, "twopass": {"enabled": True}, "ref_images": ["a.png"],
            "model_key": "hybrid_fl2va_ref2va"})
        self.assertIn("SelfLiftH3Sampler", _class_types(wf))


class TestPolicyResolution(unittest.TestCase):
    """策略函数本身的判据。"""

    def test_continuation_is_deny(self):
        self.assertEqual(
            tp.resolve_twopass_policy(
                {"continuation": _cont(), "twopass": {"enabled": True}}),
            "deny")

    def test_first_segment_is_allow(self):
        self.assertEqual(
            tp.resolve_twopass_policy(
                {"continuation": _first_seg(), "twopass": {"enabled": True}}),
            "allow")

    def test_plain_is_allow(self):
        self.assertEqual(
            tp.resolve_twopass_policy({"twopass": {"enabled": True}}),
            "allow")

    def test_policy_uses_same_criterion_as_workflow_layer(self):
        """I4：归一层与工作流层同口径，不允许分裂。

        判据是 source_job 非空。首段 source_job 为空串 -> allow。
        """
        for cont in (_cont(), _first_seg()):
            params = {**_BASE, "continuation": cont,
                      "twopass": {"enabled": True}}
            policy = tp.resolve_twopass_policy(params)
            wf = vw.build_t2v_panel(dict(params))
            has_selflift = "SelfLiftH3Sampler" in _class_types(wf)
            if policy == "deny":
                self.assertFalse(has_selflift,
                                 "策略说 deny 但工作流里仍有 SelfLift")
            else:
                self.assertTrue(has_selflift,
                                "策略说 allow 但工作流里没有 SelfLift")

    def test_explicit_allow_flag_is_required(self):
        """I5：显式口子必须**显式**声明，缺省不得放行。"""
        self.assertFalse(tp.continuation_allows_twopass(
            {"continuation": _cont()}))
        self.assertFalse(tp.continuation_allows_twopass(
            {"continuation": {**_cont(), "allow_twopass": False}}))
        self.assertFalse(tp.continuation_allows_twopass(
            {"continuation": {**_cont(), "allow_twopass": "true"}}),
            "字符串 'true' 不是显式 True，必须拒绝")
        self.assertTrue(tp.continuation_allows_twopass(
            {"continuation": {**_cont(), "allow_twopass": True}}))


class TestChineseNotes(unittest.TestCase):
    """中文留痕必须说清「为什么」和「怎么办」。"""

    def test_disabled_note_mentions_cause_and_effect(self):
        note = tp.twopass_disabled_note("t2v", 1)
        self.assertIn("二采", note)
        self.assertIn("色档", note)
        self.assertIn("nonebot", note)
        self.assertIn("第 2 段", note)

    def test_warn_note_mentions_symptom(self):
        note = tp.twopass_warn_note("t2v")
        self.assertIn("色档", note)
        self.assertIn("关掉二采", note)

    def test_notes_are_not_empty(self):
        self.assertTrue(tp.twopass_disabled_note("t2v", 0).strip())
        self.assertTrue(tp.twopass_warn_note("t2v").strip())


class TestTwopassBudgetUnaffected(unittest.TestCase):
    """§4.2：NFE 预算护栏语义不得回归。"""

    def test_clamp_transition_step_semantics_unchanged(self):
        from h3studio import twopass_budget as tb
        self.assertEqual(tb.clamp_transition_step(4, 3), 2)
        self.assertEqual(tb.clamp_transition_step(4, 2), 2)
        self.assertEqual(tb.clamp_transition_step(7, 5), 3)
        self.assertEqual(tb.high_res_nfe(4, 2), 2)

    def test_default_path_does_not_touch_budget(self):
        """默认路径（标准采样）根本不走二采 -> 护栏不该被调用。

        判据：接续段产出的工作流里没有 SelfLift 节点，
        所以护栏的 transition_step 夹取对它没有任何影响。
        """
        wf = vw.build_t2v_panel({
            **_BASE, "twopass": {"enabled": True, "transition_step": 3},
            "continuation": _cont()})
        cts = _class_types(wf)
        self.assertNotIn("SelfLiftH3Sampler", cts)
        self.assertIn("SamplerCustomAdvanced", cts)


class TestServerNormalizationIsAuthoritative(unittest.TestCase):
    """I3：服务端归一层是权威 —— 入参开了二采也必须被关掉（接续段）。

    【为什么这条最重要】
    前端 `videoState.twopass.enabled` 的默认值确实是 false，但
    `/api/video-config`（服务端持久化）会把用户上次勾选的状态在每次
    页面加载时恢复回来 ⇒ 「默认关闭」只在**归一层**成立。
    """

    @staticmethod
    def _base(**over):
        p = {
            "model_key": "fl2va_pruned_int8",
            "width": 1280, "height": 736, "duration": 5,
            "seed": 123, "steps": 4, "sampler": "euler", "scheduler": "beta",
            "shift_video": 12.0, "shift_audio": 3.0, "attention": "default",
            "accel_lora": "", "accel_lora_strength": 1.0,
            "accel_loras": [], "effect_loras": [],
            "cfg": 1.0, "aspect": "16:9",
        }
        p.update(over)
        return p

    def test_continuation_with_twopass_true_gets_disabled(self):
        import server
        out, error = server.normalize_t2v_params(self._base(
            twopass={"enabled": True, "transition_step": 4},
            continuation=_cont()))
        self.assertEqual(error, "")
        self.assertFalse(out["twopass"]["enabled"],
                         "接续段的二采必须被归一层强制关闭")
        notes = " ".join(str(n) for n in (out.get("normalize_notes") or []))
        self.assertIn("二采", notes, "必须留中文留痕说明为什么关了")
        self.assertIn("标准采样", notes)

    def test_first_segment_twopass_true_stays_enabled(self):
        """首段**不能**被归一层关掉 —— 否则用户在高分辨率档位失去提速能力。"""
        import server
        out, error = server.normalize_t2v_params(self._base(
            twopass={"enabled": True}, continuation=_first_seg()))
        self.assertEqual(error, "")
        self.assertTrue(out["twopass"]["enabled"],
                        "首段的二采必须保留")

    def test_plain_generation_twopass_true_stays_enabled(self):
        import server
        out, error = server.normalize_t2v_params(
            self._base(twopass={"enabled": True}))
        self.assertEqual(error, "")
        self.assertTrue(out["twopass"]["enabled"])

    def test_explicit_allow_twopass_keeps_it_but_warns(self):
        """I5：显式口子放行时必须留**警告**留痕（不能静默放行）。"""
        import server
        out, error = server.normalize_t2v_params(self._base(
            twopass={"enabled": True},
            continuation={**_cont(), "allow_twopass": True}))
        self.assertEqual(error, "")
        self.assertTrue(out["twopass"]["enabled"],
                        "显式声明 allow_twopass 时必须真的放行")
        notes = " ".join(str(n) for n in (out.get("normalize_notes") or []))
        self.assertIn("色档", notes, "放行时必须警告段头色档风险")

    def test_continuation_with_twopass_false_untouched(self):
        """本来就没开二采的接续段 —— 不该产生任何二采相关留痕。"""
        import server
        out, error = server.normalize_t2v_params(self._base(
            twopass={"enabled": False}, continuation=_cont()))
        self.assertEqual(error, "")
        self.assertFalse(out["twopass"]["enabled"])
        notes = " ".join(str(n) for n in (out.get("normalize_notes") or []))
        self.assertNotIn("已按标准采样路径执行", notes,
                         "没开二采就不该留「关了二采」的痕迹")


if __name__ == "__main__":
    unittest.main()
