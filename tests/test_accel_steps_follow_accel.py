# -*- coding: utf-8 -*-
"""AC-1 回归：切换「加速 LoRA」后，采样档（步数/采样器/调度器）必须跟着变。

【缺陷来源 —— 上一轮独立验收的 AC-1（次要但真实）】
线上 8190 实测（`minimax-h3 文生视频`面板）：
  1. 默认 `vidAccelLoraSelect` = `dmad_4step_critic`，`vidSteps` = 8
  2. 手动把加速 LoRA 切成 `ckpt850`（推荐 4 步）-> `vidSteps` **仍是 8**
  3. 再切成空串（=「不挂加速 LoRA」）-> `vidSteps` **仍是 8**
     ⇒ 「不挂加速 LoRA」时跑的仍是 4 步蒸馏档的步数，配非蒸馏采样必然糊。

【根因（两层）】
  A. 前端 `web/video-panel.js`：`videoState.steps` 只在用户手动拖滑块时变化，
     切换加速 LoRA 时**不重算**；而且它每次提交都**显式带上** steps
     ⇒ 服务端 `params["steps"]` 永远非空。
  B. 服务端 `server.py::normalize_t2v_params`：
     `normalize_steps(params.get("steps"), int(model.get("steps", 4)))`
     —— `model["steps"]` 恒为 4（每个 VIDEO_MODELS 条目都写死），
     与「当前挂了哪枚加速 LoRA / 有没有挂」无关。
     ⇒ 后端 `video_workflow._resolve_accel_chain` 里那条**正确**的分支
     （无 LoRA -> 20 步 / simple / res_multistep）**永远走不到**。

【本批测试锁住什么】
  1. 服务端默认档：不挂加速 LoRA -> 20 步 / simple / res_multistep；
     挂 ckpt850 -> 4 步 / beta / euler；挂 lightx2v_8step -> 8 步。
  2. 工作流真落地：这些步数/采样器/调度器真的进到 ComfyUI 的
     BasicScheduler.steps / KSamplerSelect.sampler_name / BasicScheduler.scheduler。
  3. 前端真重算：`vidAccelLoraSelect` change 时 `vidSteps` 跟着变
     （含「切到不挂」-> 20 步）。
  4. 用户显式指定的值**不被覆盖**（params 里有 steps 时以用户为准）。

运行：python3 -m pytest tests/test_accel_steps_follow_accel.py -v
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from h3studio import video_models as vmodels  # noqa: E402
from h3studio import video_workflow as vwork  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL_JS = os.path.join(REPO, "web", "video-panel.js")


def _base_params(**overrides):
    params = {
        "job_id": "ac1-steps",
        "model_key": "fl2va_pruned_int8",
        "width": 864,
        "height": 480,
        "duration": 2,
        "prompt": "ac1 prompt",
        "seed": 4242,
    }
    params.update(overrides)
    return params


def _nodes_by_type(wf, class_type):
    return [n for n in wf.values() if n.get("class_type") == class_type]


class TestServerDefaultSampling(unittest.TestCase):
    """服务端默认采样档跟着实际挂的加速 LoRA 走。"""

    def test_01_without_accel_uses_standard_multistep_profile(self):
        """不挂加速 LoRA -> 20 步 / simple / res_multistep（非蒸馏档）。"""
        import server
        # 面板「不挂」的真实提交形状：空数组 + **显式空串槽位**
        out, error = server.normalize_t2v_params(
            _base_params(accel_loras=[], accel_lora=""))
        self.assertEqual(error, "")
        self.assertEqual(out["accel_lora"], "")
        self.assertEqual(out["steps"], vmodels.VIDEO_STEPS_WITHOUT_ACCEL)
        self.assertEqual(out["steps"], 20)
        self.assertEqual(out["scheduler"], vmodels.VIDEO_SCHEDULER_WITHOUT_ACCEL)
        self.assertEqual(out["sampler"], vmodels.VIDEO_SAMPLER_WITHOUT_ACCEL)

    def test_01b_explicit_empty_slot_also_means_no_accel(self):
        """显式 accel_lora='' 同样 = 不挂（20 步标准多步档）。"""
        import server
        out, error = server.normalize_t2v_params(_base_params(accel_lora=""))
        self.assertEqual(error, "")
        self.assertEqual(out["accel_lora"], "")
        self.assertEqual(out["steps"], 20)
        self.assertEqual(out["scheduler"], "simple")
        self.assertEqual(out["sampler"], "res_multistep")

    def test_01c_absent_slot_keeps_legacy_default(self):
        """**什么都没传** -> 保持老行为（默认 ckpt850 / 4 步）。

        「没传」与「明确不挂」必须区分：老任务靠默认值复现，
        混为一谈就是在撒谎。
        """
        import server
        out, error = server.normalize_t2v_params(_base_params())
        self.assertEqual(error, "")
        self.assertEqual(out["accel_lora"], "ckpt850")
        self.assertEqual(out["steps"], 4)

    def test_02_with_ckpt850_uses_4step_distill_profile(self):
        """挂 ckpt850 -> 4 步 / beta / euler（蒸馏档）。"""
        import server
        out, error = server.normalize_t2v_params(
            _base_params(accel_loras=[{"key": "ckpt850", "strength": 1.0}]))
        self.assertEqual(error, "")
        self.assertEqual(out["accel_lora"], "ckpt850")
        self.assertEqual(out["steps"], 4)
        self.assertEqual(out["scheduler"], "beta")
        self.assertEqual(out["sampler"], "euler")

    def test_03_with_8step_lora_uses_8steps(self):
        """挂 8 步 LoRA -> 8 步（不是恒 4 步）。"""
        import server
        out, error = server.normalize_t2v_params(
            _base_params(accel_loras=[{"key": "lightx2v_8step", "strength": 1.0}]))
        self.assertEqual(error, "")
        self.assertEqual(out["steps"], 8)

    def test_04_user_explicit_steps_is_not_overridden(self):
        """用户手动指定 steps 时以用户为准（不被默认值覆盖）。"""
        import server
        out, error = server.normalize_t2v_params(
            _base_params(accel_loras=[{"key": "ckpt850", "strength": 1.0}],
                         steps=12))
        self.assertEqual(error, "")
        self.assertEqual(out["steps"], 12)

    def test_05_workflow_actually_carries_the_steps(self):
        """步数真的进到 ComfyUI 工作流的 BasicScheduler.steps。"""
        wf = vwork.build_t2v_panel(
            _base_params(accel_loras=[{"key": "ckpt850", "strength": 1.0}]))
        sched = _nodes_by_type(wf, "BasicScheduler")
        self.assertEqual(len(sched), 1)
        self.assertEqual(sched[0]["inputs"]["steps"], 4)
        self.assertEqual(sched[0]["inputs"]["scheduler"], "beta")
        samplers = _nodes_by_type(wf, "KSamplerSelect")
        self.assertEqual(len(samplers), 1)
        self.assertEqual(samplers[0]["inputs"]["sampler_name"], "euler")

    def test_06_workflow_without_accel_carries_standard_profile(self):
        """不挂 LoRA 时工作流里是 20 步 / simple / res_multistep。

        注意参数形状：面板「不挂」提交的是**空数组** accel_loras: []。
        """
        wf = vwork.build_t2v_panel(
            _base_params(accel_loras=[], accel_lora=""))
        sched = _nodes_by_type(wf, "BasicScheduler")
        self.assertEqual(sched[0]["inputs"]["steps"], 20)
        self.assertEqual(sched[0]["inputs"]["scheduler"], "simple")
        samplers = _nodes_by_type(wf, "KSamplerSelect")
        self.assertEqual(samplers[0]["inputs"]["sampler_name"], "res_multistep")

    def test_07_video_options_payload_carries_without_accel_defaults(self):
        """/api/video-options 必须把标准多步档下发给前端（前端据此重算）。"""
        payload = vmodels.options_payload()
        sampling = payload.get("sampling") or {}
        self.assertEqual(sampling.get("steps_without_accel"), 20)
        self.assertEqual(sampling.get("scheduler_without_accel"), "simple")
        self.assertEqual(sampling.get("sampler_without_accel"), "res_multistep")


class TestSlotResolution(unittest.TestCase):
    """槽位解析：空数组 = 明确不挂；键不存在 = 老行为默认。"""

    def test_10_explicit_empty_string_means_no_accel(self):
        """显式 accel_lora="" = 「不挂」（前端 50fc0e2 补的键）。"""
        key, _ = vmodels.resolve_accel_lora_slot({"accel_lora": ""})
        self.assertEqual(key, "")

    def test_11_absent_key_means_legacy_default(self):
        """**完全没有**加速信息 -> 默认 ckpt850（老任务重放语义，不变）。

        这条是 50fc0e2 钉死的契约：后端判据不动，只在前端补显式键。
        改后端会让老任务重放出来的画面变样 —— 那比原缺陷更严重。
        """
        key, _ = vmodels.resolve_accel_lora_slot({})
        self.assertEqual(key, vmodels.VIDEO_FIXED_ACCEL_LORA_DEFAULT)

    def test_12_empty_array_without_explicit_key_keeps_legacy_default(self):
        """只有空数组、没有显式键 -> 仍走默认（后端语义未改）。"""
        key, _ = vmodels.resolve_accel_lora_slot({"accel_loras": []})
        self.assertEqual(key, vmodels.VIDEO_FIXED_ACCEL_LORA_DEFAULT)

    def test_13_array_with_item_means_that_item(self):
        key, _ = vmodels.resolve_accel_lora_slot(
            {"accel_loras": [{"key": "ckpt850", "strength": 1.0, "enabled": True}]})
        self.assertEqual(key, "ckpt850")

    def test_14_real_frontend_shape_resolves_to_no_accel(self):
        """前端的真实提交形状（空数组 + 显式空串）-> 必须认成「不挂」。"""
        key, _ = vmodels.resolve_accel_lora_slot(
            {"accel_loras": [], "accel_lora": ""})
        self.assertEqual(key, "")


class TestFrontendRecompute(unittest.TestCase):
    """前端：切换加速 LoRA 时真的重算采样档。"""

    @classmethod
    def setUpClass(cls):
        with open(PANEL_JS, encoding="utf-8") as fh:
            cls.src = fh.read()

    def test_20_recompute_function_exists(self):
        self.assertIn("function recomputeSamplingForAccel", self.src)

    def test_21_accel_change_handler_calls_recompute(self):
        """change 处理器里必须调用重算函数。"""
        m = re.search(
            r'accelSel\.addEventListener\("change".*?\n\s*\}\);\n', self.src, re.S)
        self.assertIsNotNone(m, "找不到加速 LoRA 的 change 处理器")
        handler = m.group(0)  # type: ignore[union-attr]
        self.assertIn("recomputeSamplingForAccel()", handler)

    def test_22_recompute_uses_without_accel_defaults(self):
        """重算函数里必须用到服务端下发的标准多步档（不硬编码 20）。"""
        m = re.search(r"function recomputeSamplingForAccel.*?\n  \}", self.src, re.S)
        self.assertIsNotNone(m)
        body = m.group(0)  # type: ignore[union-attr]
        self.assertIn("steps_without_accel", body)
        self.assertIn("scheduler_without_accel", body)
        self.assertIn("sampler_without_accel", body)
        self.assertIn("recommended_steps", body)

    def test_23_no_hardcoded_20_in_recompute(self):
        """重算函数里不得写死 20（必须来自服务端清单）。"""
        m = re.search(r"function recomputeSamplingForAccel.*?\n  \}", self.src, re.S)
        self.assertIsNotNone(m)
        body = m.group(0)  # type: ignore[union-attr]
        self.assertNotIn("= 20", body)
        self.assertNotIn("steps = 20", body)

    def test_25_submit_sends_explicit_accel_slot(self):
        """提交参数里必须带**显式** accel_lora（含空串）。

        否则面板的「不挂加速 LoRA」会被服务端兜底成 ckpt850。
        """
        m = re.search(r"function submitParams\(\).*?\n  \}", self.src, re.S)
        self.assertIsNotNone(m)
        body = m.group(0)  # type: ignore[union-attr]
        self.assertIn("accel_lora:", body)
        self.assertIn("accel_lora_strength:", body)

    def test_24_hint_uses_accel_recommended_steps(self):
        """「推荐/已改」提示的基准是当前加速 LoRA 的推荐步数。"""
        m = re.search(r"if \(stepsHint\) \{.*?\n    \}", self.src, re.S)
        self.assertIsNotNone(m)
        body = m.group(0)  # type: ignore[union-attr]
        self.assertIn("recommended_steps", body)
        self.assertIn("steps_without_accel", body)


if __name__ == "__main__":
    unittest.main()
