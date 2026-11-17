# -*- coding: utf-8 -*-
"""二采 NFE 预算护栏的回归测试（2026-11-17 接缝色档残缺陷）。

【这个测试在防什么】
二采（twopass）接续链在段2/段1 交界处有一次肉眼可见的闪烁。
受控复现（线上真实产物，老提示词+老种子）：
  * 旧档（steps=4, transition_step=3）接续比值 **15.372x**
  * 非二采对照只有 **1.622x**
  * 段2 **内部**帧0->1 的 RGB L1 差 13.75（断点在段2 内部，不在拼接点）

根因（ComfyUI 日志的 `[SelfLift plan]` 行，决定性证据）：
    steps=4 transition_step=3 -> low_nfe=3 high_nfe=1 sigma_resume=0.7207
SelfLift 把 NFE 预算切成「低分辨率 ts 步 + 高分辨率 (steps-ts) 步」。
默认档 **高分辨率二采只剩 1 步 NFE**（起始 sigma 0.72）——
低分辨率一采在降采样空间定下的色档/细节档，抬升回来后没有预算收敛
⇒ 段头几帧色档 + 细节档双重掉档（肉眼 = 闪一下）。

修法（Studio 侧，零 comfyui 改动）：
把 transition_step 夹到「高分辨率至少拿一半 NFE 预算」的上限。
实测 transition_step=2：接续比值 15.372x -> 2.996x，段2 内部断点 13.75 -> 1.08。

【断言纪律】
  * 期望值是**字面量**（夹后的步数、节点名、参数值），
    不是从被测函数输出反推。
  * 断言要由「命名该行为的那条断言」抓住。
  * **不许**弱化既有断言 —— 本文件只**新增**。

运行：python3 -m pytest tests/test_twopass_budget.py -v
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import twopass_budget as tb        # noqa: E402
from h3studio import video_workflow as vw        # noqa: E402


_TP = {"enabled": True, "transition_step": 3, "lowres_scale": 0.75,
       "rho": 0.0, "w_min": 0.8, "w_max": 1.0,
       "upscaler_model": "minimax_h3_latent_upscaler_3d_fp16.safetensors",
       "cfg": 1.0}


def _panel(steps=4, transition_step=3, **extra):
    params = {
        "job_id": "J1", "mode": "t2v", "prompt": "一只橘猫在草地上奔跑",
        "model_key": "fl2va_pruned_int8",
        "duration": 5.0, "width": 1280, "height": 736,
        "steps": steps, "seed": 20261117, "sampler": "euler",
        "scheduler": "beta",
        "twopass": dict(_TP, transition_step=transition_step),
    }
    params.update(extra)
    return vw.build_t2v_panel(params)


class TestHighResNfeArithmetic(unittest.TestCase):
    """高分辨率 NFE = 总 NFE - 低分辨率 NFE（SelfLift 的切表语义）。"""

    def test_high_res_nfe_is_steps_minus_transition(self):
        """高分辨率 NFE = steps - transition_step。"""
        self.assertEqual(tb.high_res_nfe(4, 3), 1)
        self.assertEqual(tb.high_res_nfe(4, 2), 2)
        self.assertEqual(tb.high_res_nfe(7, 5), 2)
        self.assertEqual(tb.high_res_nfe(7, 3), 4)

    def test_defect_budget_is_one_step(self):
        """缺陷档（steps=4, ts=3）的高分辨率 NFE 就是 1 —— 这就是根因。"""
        self.assertEqual(tb.high_res_nfe(4, 3), 1,
                         "默认档高分辨率二采只剩 1 步 NFE = 缺陷的机制")

    def test_high_res_nfe_never_negative(self):
        """ts 超过 steps 时不能返回负数。"""
        self.assertEqual(tb.high_res_nfe(4, 9), 0)


class TestClampTransitionStep(unittest.TestCase):
    """夹取规则：高分辨率至少拿一半预算。"""

    def test_defect_budget_gets_clamped_to_two(self):
        """steps=4 / ts=3 必须被夹到 2（高分辨率 1 步 -> 2 步）。"""
        self.assertEqual(tb.clamp_transition_step(4, 3), 2)

    def test_already_balanced_is_untouched(self):
        """steps=4 / ts=2 已经是 2/2 平衡，不该被动。"""
        self.assertEqual(tb.clamp_transition_step(4, 2), 2)

    def test_smaller_transition_is_untouched(self):
        """ts 比上限更小 = 高分辨率预算更充裕，不该被改。"""
        self.assertEqual(tb.clamp_transition_step(4, 1), 1)
        self.assertEqual(tb.clamp_transition_step(7, 1), 1)
        self.assertEqual(tb.clamp_transition_step(7, 2), 2)

    def test_seven_steps_transition_five_gets_clamped_to_three(self):
        """steps=7 / ts=5（高分辨率只剩 2 步）必须夹到 3（高分辨率 4 步）。"""
        self.assertEqual(tb.clamp_transition_step(7, 5), 3)

    def test_clamp_never_exceeds_high_res_budget(self):
        """夹取后的档位，高分辨率 NFE 必须 >= 总 NFE 的一半。

        【断言纪律】期望值是**字面量**（ceil(steps*0.5)），
        不是从被测函数输出反推。
        """
        for steps in range(2, 25):
            for ts in range(1, steps + 2):
                got = tb.clamp_transition_step(steps, ts)
                high = tb.high_res_nfe(steps, got)
                want_min = -(-steps // 2)      # ceil(steps/2)
                self.assertGreaterEqual(
                    high, want_min,
                    f"steps={steps} ts={ts} -> 夹到 {got} 后高分辨率只有 {high} 步，"
                    f"少于一半预算 {want_min}")

    def test_clamp_is_idempotent(self):
        """夹过一次再夹，结果不变（不能来回抖动）。"""
        for steps in (2, 3, 4, 5, 7, 8, 10, 20):
            for ts in range(1, steps + 1):
                once = tb.clamp_transition_step(steps, ts)
                twice = tb.clamp_transition_step(steps, once)
                self.assertEqual(once, twice,
                                 f"steps={steps} ts={ts} 夹取不幂等")


class TestWorkflowNodeInput(unittest.TestCase):
    """节点实际下发的那一行必须已经是夹过的。"""

    def test_node_receives_clamped_transition_step(self):
        """steps=4 / ts=3 -> 节点 171 的 transition_step 必须是 2。"""
        p = _panel(steps=4, transition_step=3)
        self.assertEqual(p["171"]["class_type"], "SelfLiftH3Sampler")
        self.assertEqual(
            p["171"]["inputs"]["transition_step"], 2,
            "节点必须收到夹过的 transition_step=2（旧值 3 会让高分辨率只剩 1 步 NFE）")

    def test_node_receives_balanced_transition_step(self):
        """steps=4 / ts=2 -> 节点收到 2（本来就不该被动）。"""
        p = _panel(steps=4, transition_step=2)
        self.assertEqual(p["171"]["inputs"]["transition_step"], 2)

    def test_node_receives_three_when_seven_steps(self):
        """steps=7 / ts=5 -> 节点收到 3。"""
        p = _panel(steps=7, transition_step=5)
        self.assertEqual(p["171"]["inputs"]["transition_step"], 3)

    def test_small_transition_untouched_in_node(self):
        """ts=1 不会被夹（高分辨率预算充足）。"""
        p = _panel(steps=4, transition_step=1)
        self.assertEqual(p["171"]["inputs"]["transition_step"], 1)

    def test_twopass_off_has_no_sampler_node(self):
        """关掉二采时不应出现 SelfLift 节点（更不该动用户的其它参数）。"""
        params = {
            "job_id": "J1", "mode": "t2v", "prompt": "x",
            "model_key": "fl2va_pruned_int8",
            "duration": 5.0, "width": 1280, "height": 736,
            "steps": 4, "seed": 1, "sampler": "euler", "scheduler": "beta",
            "twopass": {"enabled": False},
        }
        p = vw.build_t2v_panel(params)
        self.assertNotIn("171", p,
                         "二采关闭时不应有 SelfLiftH3Sampler 节点")


class TestServerNormalizeTraceability(unittest.TestCase):
    """服务端归一必须**留痕**（用户要看得见参数被改了）。"""

    def test_normalize_clamps_and_records_note(self):
        """归一后 transition_step=2，且 normalize_notes 里有说明。"""
        import server
        params = {
            "mode": "t2v", "prompt": "x", "model_key": "fl2va_pruned_int8",
            "duration": 5.0, "width": 1280, "height": 736,
            "steps": 4, "seed": 1, "sampler": "euler", "scheduler": "beta",
            "twopass": dict(_TP),
        }
        result = server.normalize_t2v_params(params)
        out = result[0] if isinstance(result, tuple) else result
        self.assertEqual(out["twopass"]["transition_step"], 2)
        notes = out.get("normalize_notes") or []
        joined = " ".join(str(n) for n in notes)
        self.assertIn("transition_step", joined,
                      "夹取必须留痕（用户要看得见一采步数被改了）")
        self.assertIn("NFE", joined,
                      "留痕要说明原因（高分辨率二采的 NFE 预算不足）")

    def test_normalize_leaves_balanced_value_untouched(self):
        """已经是 2 的值不该被改，也不该产生多余留痕。"""
        import server
        params = {
            "mode": "t2v", "prompt": "x", "model_key": "fl2va_pruned_int8",
            "duration": 5.0, "width": 1280, "height": 736,
            "steps": 4, "seed": 1, "sampler": "euler", "scheduler": "beta",
            "twopass": dict(_TP, transition_step=2),
        }
        result = server.normalize_t2v_params(params)
        out = result[0] if isinstance(result, tuple) else result
        self.assertEqual(out["twopass"]["transition_step"], 2)
        notes = " ".join(str(n) for n in (out.get("normalize_notes") or []))
        self.assertNotIn("NFE", notes,
                         "没有发生夹取就不该留下夹取说明")

    def test_twopass_off_untouched(self):
        """二采关闭时完全不碰 twopass 参数。"""
        import server
        params = {
            "mode": "t2v", "prompt": "x", "model_key": "fl2va_pruned_int8",
            "duration": 5.0, "width": 1280, "height": 736,
            "steps": 4, "seed": 1, "sampler": "euler", "scheduler": "beta",
            "twopass": {"enabled": False},
        }
        result = server.normalize_t2v_params(params)
        out = result[0] if isinstance(result, tuple) else result
        self.assertEqual(out["twopass"], {"enabled": False})


if __name__ == "__main__":
    unittest.main()
