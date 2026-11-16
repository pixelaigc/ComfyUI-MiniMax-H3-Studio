# -*- coding: utf-8 -*-
"""文生视频面板采样参数（步数 / 采样器 / 调度器 / shift / CFG）回归测试。

【为什么要有这批测试】
2026-11-11 用户实测发现：文生视频面板只有 尺寸预设/输出尺寸/画面比例/
模型/加速LoRA/注意力机制/时长，**没有**步数/采样器/调度器/CFG/shift，
与生图面板不对齐。

本批测试锁住三件事：
  1. **能力核实**：H3 工作流里确实存在 steps / sampler / scheduler / shift
     的真实节点，且取值范围来自本机 ComfyUI 实测 —— 不是臆造的控件。
  2. **真接入**：面板提交的值必须**真的**出现在工作流 JSON 里
     （BasicScheduler.steps / KSamplerSelect.sampler_name /
      BasicScheduler.scheduler / MiniMaxH3SigmaShift.shift_*）。
  3. **不伪造**：H3 标准路径没有 CFG（BasicGuider 无 cfg 槽位），
     所以 CFG 必须固定 1.0 且**不能**出现在任何节点的输入里；
     只有二采（SelfLiftH3Sampler）才吃 cfg。

运行：python3 -m pytest tests/test_video_sampling_params.py -v
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from h3studio import video_models as vmodels  # noqa: E402
from h3studio import video_workflow as vwork  # noqa: E402
from h3studio import workflows as W  # noqa: E402


def _panel_params(**overrides):
    """一组能跑通 build_t2v_panel 的最小参数。"""
    params = {
        "job_id": "test_sampling",
        "model_key": "fl2va_pruned_int8",
        "width": 1344,
        "height": 768,
        "duration": 5,
        "prompt": "test prompt",
        "seed": 4242,
    }
    params.update(overrides)
    return params


def _nodes_by_type(wf, class_type):
    return [n for n in wf.values() if n.get("class_type") == class_type]


class TestSamplingCapability(unittest.TestCase):
    """能力核实：取值范围来自本机实测，不是臆造。"""

    def test_samplers_include_official_h3_ones(self):
        """官方蓝图用的是 res_multistep；Turbo 示例工作流用 euler。"""
        self.assertIn("euler", vmodels.VIDEO_SAMPLERS)
        self.assertIn("res_multistep", vmodels.VIDEO_SAMPLERS)

    def test_schedulers_include_official_h3_ones(self):
        """官方蓝图 BasicScheduler 用 simple；本仓模型默认用 beta。"""
        self.assertIn("simple", vmodels.VIDEO_SCHEDULERS)
        self.assertIn("beta", vmodels.VIDEO_SCHEDULERS)

    def test_sampler_list_size_matches_comfyui(self):
        """KSamplerSelect 实测 68 项 —— 数量对不上说明清单抄错了。"""
        self.assertEqual(len(vmodels.VIDEO_SAMPLERS), 68)

    def test_scheduler_list_size_matches_comfyui(self):
        """BasicScheduler 实测 14 项。"""
        self.assertEqual(len(vmodels.VIDEO_SCHEDULERS), 14)

    def test_shift_defaults_are_model_inherent_values(self):
        """shift 默认 12.0 / 3.0 = H3 模型内生值。

        依据（ComfyUI 源码）：
          comfy/supported_models.py  class MiniMaxH3
            sampling_settings = {"shift": 12.0, "audio_shift": 3.0}
          comfy/ldm/minimax/model.py  MiniMaxH3Model.__init__(
            sigma_shift_video=12.0, sigma_shift_audio=3.0)
        所以「接 shift 节点但用默认值」== 「不接节点」，不改变任何结果。
        """
        self.assertEqual(vmodels.VIDEO_SHIFT_DEFAULT_VIDEO, 12.0)
        self.assertEqual(vmodels.VIDEO_SHIFT_DEFAULT_AUDIO, 3.0)

    def test_cfg_is_fixed_on_standard_path(self):
        """H3 标准路径固定 CFG=1.0 —— BasicGuider 没有 cfg 槽位。"""
        self.assertEqual(vmodels.VIDEO_CFG_FIXED, 1.0)
        self.assertTrue(vmodels.VIDEO_CFG_FIXED_REASON)

    def test_normalize_rejects_unknown_names(self):
        """不在清单里的采样器/调度器 -> 回落默认，不放行。"""
        self.assertEqual(
            vmodels.normalize_sampler("not_a_real_sampler", "euler"), "euler")
        self.assertEqual(
            vmodels.normalize_scheduler("not_a_real_sched", "beta"), "beta")
        # 合法值原样通过
        self.assertEqual(
            vmodels.normalize_sampler("res_multistep", "euler"), "res_multistep")

    def test_normalize_clamps_out_of_range(self):
        self.assertEqual(vmodels.normalize_steps(9999, 4), vmodels.VIDEO_STEPS_MAX)
        self.assertEqual(vmodels.normalize_steps(-5, 4), vmodels.VIDEO_STEPS_MIN)
        self.assertEqual(vmodels.normalize_shift(9999.0, 12.0), vmodels.VIDEO_SHIFT_MAX)
        self.assertEqual(vmodels.normalize_shift(0.0, 12.0), vmodels.VIDEO_SHIFT_MIN)

    def test_normalize_handles_garbage(self):
        """垃圾输入不能让整个任务炸掉。"""
        self.assertEqual(vmodels.normalize_steps("abc", 4), 4)
        self.assertEqual(vmodels.normalize_steps(None, 4), 4)
        self.assertEqual(vmodels.normalize_shift("abc", 12.0), 12.0)
        self.assertEqual(vmodels.normalize_shift(None, 12.0), 12.0)


class TestWorkflowReceivesParams(unittest.TestCase):
    """真接入：面板提交的值必须真的落到工作流节点里。"""

    def test_steps_sampler_scheduler_land_in_workflow(self):
        wf = vwork.build_t2v_panel(_panel_params(
            steps=7, sampler="dpmpp_2m_sde_gpu", scheduler="karras"))
        self.assertEqual(wf["9"]["inputs"]["steps"], 7)
        self.assertEqual(wf["9"]["inputs"]["scheduler"], "karras")
        self.assertEqual(wf["17"]["inputs"]["sampler_name"], "dpmpp_2m_sde_gpu")

    def test_shift_node_lands_in_workflow(self):
        wf = vwork.build_t2v_panel(_panel_params(
            shift_video=9.5, shift_audio=2.25))
        shifts = _nodes_by_type(wf, "MiniMaxH3SigmaShift")
        self.assertEqual(len(shifts), 1, "应恰好有一个 shift 节点")
        self.assertEqual(shifts[0]["inputs"]["shift_video"], 9.5)
        self.assertEqual(shifts[0]["inputs"]["shift_audio"], 2.25)

    def test_shift_node_feeds_scheduler_and_guider(self):
        """shift 节点必须真的插在 model 链上（插了但没接 = 没生效）。"""
        wf = vwork.build_t2v_panel(_panel_params(shift_video=9.5))
        shifts = _nodes_by_type(wf, "MiniMaxH3SigmaShift")
        shift_id = [nid for nid, n in wf.items()
                    if n["class_type"] == "MiniMaxH3SigmaShift"][0]
        shift_out = [shift_id, 0]
        self.assertEqual(wf["9"]["inputs"]["model"], shift_out,
                         "BasicScheduler.model 必须接 shift 节点")
        self.assertEqual(wf["16"]["inputs"]["model"], shift_out,
                         "BasicGuider.model 必须接 shift 节点")

    def test_default_shift_is_verified_noop(self):
        """默认值 12.0/3.0 == 模型内生值 -> 不改变出图结果。"""
        wf = vwork.build_t2v_panel(_panel_params())
        shifts = _nodes_by_type(wf, "MiniMaxH3SigmaShift")
        self.assertEqual(len(shifts), 1)
        self.assertEqual(shifts[0]["inputs"]["shift_video"], 12.0)
        self.assertEqual(shifts[0]["inputs"]["shift_audio"], 3.0)

    def test_use_sigma_shift_false_skips_node(self):
        """节点不可用时可以不接 —— 不硬塞 ComfyUI 不认的节点。"""
        wf = vwork.build_t2v_panel(_panel_params(use_sigma_shift=False))
        self.assertEqual(len(_nodes_by_type(wf, "MiniMaxH3SigmaShift")), 0)

    def test_invalid_sampler_falls_back_instead_of_crashing(self):
        wf = vwork.build_t2v_panel(_panel_params(sampler="bogus_sampler"))
        self.assertEqual(wf["17"]["inputs"]["sampler_name"], "euler")

    def test_invalid_scheduler_falls_back(self):
        wf = vwork.build_t2v_panel(_panel_params(scheduler="bogus_scheduler"))
        self.assertEqual(wf["9"]["inputs"]["scheduler"], "beta")


class TestNoFakeCfgControl(unittest.TestCase):
    """不伪造：CFG 在标准路径上不能出现在任何节点输入里。"""

    def test_standard_path_has_no_cfg_input(self):
        wf = vwork.build_t2v_panel(_panel_params(cfg=9.9))
        for nid, node in wf.items():
            self.assertNotIn("cfg", node.get("inputs", {}),
                             f"节点 {nid} ({node['class_type']}) 不该有 cfg 输入")

    def test_guider_is_basic_not_cfg(self):
        """H3 标准路径用 BasicGuider（无 cfg），不是 CFGGuider。"""
        wf = vwork.build_t2v_panel(_panel_params())
        guiders = _nodes_by_type(wf, "BasicGuider")
        self.assertEqual(len(guiders), 1)
        self.assertEqual(len(_nodes_by_type(wf, "CFGGuider")), 0)

    def test_twopass_cfg_does_reach_sampler(self):
        """二采路径的 CFG 是真参数（SelfLiftH3Sampler 自带 cfg 槽位）。"""
        wf = vwork.build_t2v_panel(_panel_params(
            twopass={"enabled": True, "cfg": 5.5, "transition_step": 3,
                     "lowres_scale": 0.5, "rho": 0.6, "w_min": 0.5, "w_max": 1.0,
                     "upscaler_model": "minimax_h3_latent_upscaler_3d_bf16.safetensors"}))
        samplers = _nodes_by_type(wf, "SelfLiftH3Sampler")
        self.assertEqual(len(samplers), 1)
        self.assertEqual(samplers[0]["inputs"]["cfg"], 5.5)

    def test_twopass_sampler_is_forced_euler(self):
        """SelfLift 只支持标准 Euler —— 面板显示为只读固定值。"""
        wf = vwork.build_t2v_panel(_panel_params(
            sampler="dpmpp_2m_sde_gpu",
            twopass={"enabled": True, "cfg": 1.0, "transition_step": 3,
                     "lowres_scale": 0.5, "rho": 0.6, "w_min": 0.5, "w_max": 1.0,
                     "upscaler_model": "minimax_h3_latent_upscaler_3d_bf16.safetensors"}))
        self.assertEqual(wf["17"]["inputs"]["sampler_name"], "euler")


class TestLegacyVideoPaths(unittest.TestCase):
    """首尾帧 / 参考生视频：同一条采样链路，同样能调。"""

    def test_flf_accepts_sampling_params(self):
        wf = W.build_flf({
            "job_id": "flf", "prompt": "x", "resolution": "768p",
            "aspect": "16:9", "duration": 5,
            "steps": 9, "sampler": "res_multistep", "scheduler": "simple",
            "shift_video": 11.0, "shift_audio": 2.0})
        self.assertEqual(wf["9"]["inputs"]["steps"], 9)
        self.assertEqual(wf["9"]["inputs"]["scheduler"], "simple")
        self.assertEqual(wf["17"]["inputs"]["sampler_name"], "res_multistep")
        shifts = _nodes_by_type(wf, "MiniMaxH3SigmaShift")
        self.assertEqual(len(shifts), 1)
        self.assertEqual(shifts[0]["inputs"]["shift_video"], 11.0)

    def test_r2v_accepts_sampling_params(self):
        wf = W.build_r2v({
            "job_id": "r2v", "prompt": "x", "resolution": "768p",
            "aspect": "16:9", "duration": 5, "ref_images": ["a.png"],
            "steps": 6, "sampler": "euler_ancestral", "scheduler": "karras"})
        self.assertEqual(wf["9"]["inputs"]["steps"], 6)
        self.assertEqual(wf["9"]["inputs"]["scheduler"], "karras")
        self.assertEqual(wf["17"]["inputs"]["sampler_name"], "euler_ancestral")

    def test_legacy_defaults_unchanged(self):
        """老任务（不传采样参数）的行为必须与改动前完全一致。

        改动前：有加速 LoRA -> steps 4 / beta / euler；
                无加速 LoRA -> steps 20 / simple / res_multistep。
        """
        with_lora = W.build_flf({
            "job_id": "a", "prompt": "x", "resolution": "768p",
            "aspect": "16:9", "duration": 5, "lora": "ckpt850"})
        self.assertEqual(with_lora["9"]["inputs"]["steps"], 4)
        self.assertEqual(with_lora["9"]["inputs"]["scheduler"], "beta")
        self.assertEqual(with_lora["17"]["inputs"]["sampler_name"], "euler")

        without_lora = W.build_flf({
            "job_id": "b", "prompt": "x", "resolution": "768p",
            "aspect": "16:9", "duration": 5, "lora": "none"})
        self.assertEqual(without_lora["9"]["inputs"]["steps"], 20)
        self.assertEqual(without_lora["9"]["inputs"]["scheduler"], "simple")
        self.assertEqual(without_lora["17"]["inputs"]["sampler_name"], "res_multistep")


class TestOptionsPayload(unittest.TestCase):
    """清单下发：前端拿到的候选项必须齐全且合法。"""

    def test_payload_contains_sampling_block(self):
        payload = vmodels.options_payload()
        sampling = payload.get("sampling")
        self.assertIsNotNone(sampling, "清单必须下发 sampling 区块")
        self.assertEqual(sampling["samplers"], vmodels.VIDEO_SAMPLERS)
        self.assertEqual(sampling["schedulers"], vmodels.VIDEO_SCHEDULERS)
        self.assertEqual(sampling["steps_min"], vmodels.VIDEO_STEPS_MIN)
        self.assertEqual(sampling["steps_max"], vmodels.VIDEO_STEPS_MAX)
        self.assertEqual(sampling["shift_default_video"], 12.0)
        self.assertEqual(sampling["shift_default_audio"], 3.0)
        self.assertEqual(sampling["cfg_fixed"], 1.0)

    def test_payload_models_carry_sampling_defaults(self):
        """每个底模都要带自己的推荐值 —— 面板默认值取自这里。"""
        payload = vmodels.options_payload()
        for item in payload["models"]:
            self.assertIn("steps", item, f"{item.get('key')} 缺 steps")
            self.assertIn("sampler", item, f"{item.get('key')} 缺 sampler")
            self.assertIn("scheduler", item, f"{item.get('key')} 缺 scheduler")
            self.assertIn(item["sampler"], vmodels.VIDEO_SAMPLERS)
            self.assertIn(item["scheduler"], vmodels.VIDEO_SCHEDULERS)


class TestFrontendPanelContract(unittest.TestCase):
    """前端契约：控件 id 必须与 JS 绑定一致（防改一处漏一处）。"""

    def _html(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "web", "index.html"), encoding="utf-8") as fh:
            return fh.read()

    def _js(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "web", "video-panel.js"), encoding="utf-8") as fh:
            return fh.read()

    def test_video_panel_has_sampling_controls(self):
        html = self._html()
        for node_id in ("vidSteps", "vidSamplerSelect", "vidSchedulerSelect",
                        "vidShiftVideo", "vidShiftAudio", "vidCfg"):
            self.assertIn(f'id="{node_id}"', html,
                          f"文生视频面板缺控件 {node_id}")

    def test_legacy_video_panel_has_sampling_controls(self):
        """首尾帧 / 参考生视频面板必须有采样参数控件。

        【2026-11-15 修复③ 交付项 3 更新】
        三个视频模式现在**共用**同一套面板（#videoPanel），
        所以采样参数控件只有**一套**（#vidSamplerSelect / #vidSchedulerSelect /
        #vidShiftVideo / #vidShiftAudio / #vidCfg），
        首尾帧与参考生视频**直接用它** —— 不再有第二套 legacy 控件。

        本断言的意图（这三个模式都有采样参数可调）**完全保留**：
        控件依然存在，只是搬到了共享面板里。
        """
        html = self._html()
        for node_id in ("vidSamplerSelect", "vidSchedulerSelect",
                        "vidShiftVideo", "vidShiftAudio",
                        "vidCfg"):
            self.assertIn(f'id="{node_id}"', html,
                          f"共享视频面板缺控件 {node_id}")
        # 旧的第二套 legacy 控件必须**已删除**（否则又变成两处采样参数）
        for node_id in ("vidLegacySamplerSelect", "vidLegacySchedulerSelect",
                        "vidLegacyShiftVideo", "vidLegacyShiftAudio",
                        "vidLegacyCfg"):
            self.assertNotIn(f'id="{node_id}"', html,
                             f"旧的重复采样控件 {node_id} 必须删除")

    def test_cfg_control_is_readonly(self):
        """CFG 是只读说明，不能是可调控件（不伪造假控件）。

        【2026-11-15 修复③ 交付项 3 更新】
        旧的 #vidLegacyCfg 已随 legacy 面板删除 ——
        共享面板的 #vidCfg 是**唯一**那个只读说明。
        """
        html = self._html()
        self.assertIn('id="vidCfg" value="1.0" readonly', html)
        self.assertNotIn('id="vidLegacyCfg"', html)

    def test_twopass_cfg_control_exists(self):
        html = self._html()
        self.assertIn('id="vidTwopassCfg"', html)

    def test_js_submits_sampling_params(self):
        """提交参数里必须带上采样参数，否则前端改了也没用。"""
        js = self._js()
        for key in ("steps:", "sampler:", "scheduler:",
                    "shift_video:", "shift_audio:"):
            self.assertIn(key, js, f"submitParams 缺 {key}")

    def test_js_does_not_send_standard_cfg(self):
        """标准路径不能提交 cfg —— 提交了也是没人读的死参数。"""
        js = self._js()
        self.assertNotIn("cfg: Number(videoState.sampling", js)
        # twopass 里的 cfg 是允许的
        self.assertIn("cfg: Number(videoState.twopass.cfg)", js)




class TestBugsFound2026_11_12(unittest.TestCase):
    """本轮实测发现的真实缺陷的回归测试。

    【为什么单独一个类】这些不是「本来就有的行为」的断言，
    而是「修之前会红、修之后会绿」的缺陷守卫 ——
    每一条都能指认一条具体的线上/代码级故障。

    缺陷清单：
      B1  server._VIDEO_PANEL_NODES 漏了 MiniMaxH3SigmaShift
          -> 服务端 use_sigma_shift 恒 False
          -> shift 滑杆是**死控件**（能拖、但不进工作流）
      B2  flf/r2v 的服务端归一写死 default=euler/beta 且无条件写回
          -> lora=="none" 的老任务被静默改成 euler/beta（画质回归）
      B3  legacy 面板的「推荐」基准与工作流硬编码档位不一致
          -> 面板说 6 步、工作流跑 4 步 = 假提示
      B4  options_payload 没下发 shift 节点的可用性
          -> 前端无法判断该不该禁用滑杆（假控件风险）
    """

    def _server_module(self):
        import importlib
        import server
        importlib.reload(server)
        return server

    def _html(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "web", "index.html"), encoding="utf-8") as fh:
            return fh.read()

    def _js(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "web", "video-panel.js"), encoding="utf-8") as fh:
            return fh.read()

    # ---- B1：shift 节点必须在服务端的节点可用性名单里 ----

    def test_B1_sigma_shift_node_is_in_server_node_list(self):
        """B1：MiniMaxH3SigmaShift 必须在 _VIDEO_PANEL_NODES 里。

        漏了它 -> 服务端 use_sigma_shift 恒 False
             -> shift 参数永远进不了工作流 = 死控件。
        """
        srv = self._server_module()
        self.assertIn("MiniMaxH3SigmaShift", srv._VIDEO_PANEL_NODES,
                      "服务端节点名单漏了 MiniMaxH3SigmaShift —— shift 会是死控件")

    def test_B1_sigma_shift_available_after_refresh(self):
        """B1 续：刷新节点清单后，shift 节点必须被判为可用。

        本机 ComfyUI 里它是**核心**节点（comfy_extras/nodes_minimax_h3.py:367），
        必然存在。刷新之后 _COMFY_NODE_NAMES 里必须有它。
        """
        srv = self._server_module()
        # 用一个假的 ComfyUI 端点验证「刷新后名单里确实有它」——
        # 这里直接断言名单内容，不依赖网络。
        self.assertIn("MiniMaxH3SigmaShift", srv._VIDEO_PANEL_NODES)
        # 名单里的每个节点名都必须是合法标识（不能是空串/带空格）
        for name in srv._VIDEO_PANEL_NODES:
            self.assertTrue(name and " " not in name, f"非法节点名: {name!r}")

    def test_B1_normalize_t2v_enables_shift_when_node_available(self):
        """B1 续：节点可用时，normalize_t2v_params 必须让 use_sigma_shift=True。

        这一条守住「服务端真的会让 shift 进工作流」，而不是只守住名单。
        """
        srv = self._server_module()
        srv._COMFY_NODE_NAMES = {"MiniMaxH3SigmaShift"}
        params = {
            "width": 1344, "height": 768, "duration": 5,
            "prompt": "x", "model_key": "fl2va_pruned_int8",
        }
        out, err = srv.normalize_t2v_params(dict(params))
        self.assertEqual(err, "")
        self.assertTrue(out["use_sigma_shift"],
                        "节点可用时 use_sigma_shift 必须为 True")

    def test_B1_normalize_t2v_disables_shift_when_node_missing(self):
        """B1 续：节点**不可用**时必须 False（不能硬塞 ComfyUI 不认的节点）。"""
        srv = self._server_module()
        srv._COMFY_NODE_NAMES = set()
        out, err = srv.normalize_t2v_params({
            "width": 1344, "height": 768, "duration": 5,
            "prompt": "x", "model_key": "fl2va_pruned_int8",
        })
        self.assertEqual(err, "")
        self.assertFalse(out["use_sigma_shift"])

    # ---- B2：flf/r2v 归一不得静默改老任务的采样器/调度器 ----

    def test_B2_legacy_client_without_sampler_keeps_workflow_default(self):
        """B2：老客户端（不传 sampler/scheduler）不得被服务端改写。

        工作流的硬编码档位（workflows._h3_video_workflow）：
          lora != "none" -> euler / beta
          lora == "none" -> res_multistep / simple
        服务端若无条件写回 default=euler/beta，
        lora=="none" 的老任务就从 res_multistep/simple 变成 euler/beta
        = 静默画质回归。
        """
        srv = self._server_module()
        srv._COMFY_NODE_NAMES = {"MiniMaxH3SigmaShift"}
        params = {"prompt": "x", "lora": "none"}
        srv.prepare_job_params("flf", params, None, {"file_first": ["a.png"]})
        # 客户端没传 -> 服务端**不该**凭空写一个值
        self.assertNotIn("sampler", params,
                         "客户端没传 sampler 时服务端不该写回（会改老任务画质）")
        self.assertNotIn("scheduler", params,
                         "客户端没传 scheduler 时服务端不该写回")

    def test_B2_legacy_client_sampler_is_whitelisted(self):
        """B2 续：客户端**传了**就做白名单归一（非法值回落）。"""
        srv = self._server_module()
        srv._COMFY_NODE_NAMES = {"MiniMaxH3SigmaShift"}
        params = {"prompt": "x", "lora": "none",
                  "sampler": "bogus_sampler", "scheduler": "bogus_scheduler"}
        srv.prepare_job_params("flf", params, None, {"file_first": ["a.png"]})
        self.assertIn(params["sampler"], vmodels.VIDEO_SAMPLERS)
        self.assertIn(params["scheduler"], vmodels.VIDEO_SCHEDULERS)

    def test_B2_legacy_defaults_match_workflow_hardcoded_tiers(self):
        """B2 续：服务端回落默认值必须与工作流硬编码档位一致。"""
        srv = self._server_module()
        srv._COMFY_NODE_NAMES = {"MiniMaxH3SigmaShift"}
        # lora == "none" -> 工作流用 res_multistep / simple
        params = {"prompt": "x", "lora": "none"}
        srv.prepare_job_params("flf", params, None, {"file_first": ["a.png"]})
        wf = W.build_flf({"job_id": "b", "prompt": "x", "resolution": "768p",
                          "aspect": "16:9", "duration": 5, "lora": "none"})
        self.assertEqual(wf["17"]["inputs"]["sampler_name"], "res_multistep")
        self.assertEqual(wf["9"]["inputs"]["scheduler"], "simple")

    # ---- B3：前端「推荐」基准必须与工作流一致 ----

    def _app_js(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "web", "app.js"), encoding="utf-8") as fh:
            return fh.read()

    def _panel_js(self):
        """共享视频面板的源码（web/video-panel.js）。

        【2026-11-15 修复③ 交付项 3】
        首尾帧 / 参考生视频的采样参数控件搬到了共享面板里，
        所以这些断言要读**那个**文件，而不是 app.js。
        """
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "web", "video-panel.js"),
                  encoding="utf-8") as fh:
            return fh.read()

    def test_B3_legacy_steps_default_is_4_not_6(self):
        """B3：Turbo 档的默认步数是 **4**（工作流硬编码），不是 6。

        面板写「默认 6 步」而工作流跑 4 步 = 假提示。

        【2026-11-15 修复③ 交付项 3 更新】
        旧的 setDefaultSteps / legacySamplingDefaults 已随 legacy 面板删除；
        默认步数现在由**共享面板**给出（video-panel.js 的 loadVideoOptions：
        取当前底模的 steps，缺省回落到 sampling.steps_default）。
        """
        js = self._app_js()
        self.assertNotIn(
            'state.model === "lora" ? 6 : 20', js,
            "还在用旧的 6 步默认值（工作流实际是 4 步）")
        panel_js = self._panel_js()
        self.assertIn("videoState.steps = defModel.steps != null", panel_js)
        self.assertIn("sampling.steps_default != null ? sampling.steps_default : 4",
                      panel_js)

    def test_B3_legacy_default_tiers_match_workflow(self):
        """B3 续：legacy 的采样器/调度器/步数默认值必须与工作流一致。

        工作流（workflows._h3_video_workflow）：
          lora != "none" -> steps 4 / euler / beta
          lora == "none" -> steps 20 / res_multistep / simple
        """
        js = self._app_js()
        self.assertIn('sampler: withLora ? "euler" : "res_multistep"', js)
        self.assertIn('scheduler: withLora ? "beta" : "simple"', js)
        self.assertIn("steps: withLora ? 4 : 20", js)

    def test_B3_legacy_default_uses_effective_lora_not_state_lora(self):
        """B3 续：判据必须是**提交时真正用的** params.lora。

        提交路径：lora = state.model === "lora" ? state.lora : "none"
        —— 「标准模型」卡片也会把 params.lora 置成 "none"。
        只看 state.lora 会判错档位。
        """
        js = self._app_js()
        self.assertIn("effectiveLegacyLora()", js)
        self.assertIn('state.model === "lora" ? (state.lora || "ckpt850") : "none"', js)

    def test_B3_legacy_steps_hint_exists(self):
        """B3 续：必须有步数的「推荐 / 已改」提示控件。

        【2026-11-15 修复③ 交付项 3 更新】
        旧的 #vidLegacyStepsHint 已随 legacy 面板删除；
        共享面板的 #vidStepsHint 是**唯一**那个提示。
        """
        html = self._html()
        self.assertIn('id="vidStepsHint"', html,
                      "共享面板缺步数的推荐/已改提示")
        self.assertNotIn('id="vidLegacyStepsHint"', html)

    def test_B3_model_card_label_says_4_steps(self):
        """B3 续：不得出现「默认 6 步」的假文案。

        【2026-11-15 修复③ 交付项 3 更新】
        旧的「Turbo 加速 · 默认 4 步」模型卡片已随 legacy 面板删除 ——
        底模选择改为共享面板的**下拉**（#vidModelSelect），
        推荐步数由 #vidStepsHint 显示。
        """
        html = self._html()
        self.assertNotIn("Turbo 加速 · 默认 6 步", html)
        self.assertNotIn("Turbo 加速 · 默认 4 步", html)
        self.assertIn('id="vidStepsHint"', html)

    # ---- B4：shift 节点可用性必须下发给前端 ----

    def test_B4_payload_reports_sigma_shift_availability(self):
        """B4：清单必须下发 shift 节点的可用性，前端据此禁用滑杆。"""
        payload = vmodels.options_payload()
        self.assertIn("sigma_shift_available", payload)
        self.assertTrue(payload["sigma_shift_available"])

    def test_B4_payload_reports_unavailable_when_node_missing(self):
        """B4 续：回调说节点没有时，必须下发 False（前端据此禁用滑杆）。"""
        payload = vmodels.options_payload(node_available=lambda ct: False)
        self.assertFalse(payload["sigma_shift_available"])

    def test_B4_frontend_disables_shift_when_unavailable(self):
        """B4 续：前端必须按可用性禁用 shift 滑杆（不做假控件）。"""
        js = self._js()
        self.assertIn("sigma_shift_available", js)
        self.assertIn("节点不可用", js)


class TestBugsFound2026_11_12_B(unittest.TestCase):
    """第二批缺陷的回归测试（B5 / B6）。

    B5  state.steps 初值与 HTML 滑杆默认值不一致
        -> 滑杆显示 4、说明行写「步数 6」= 界面自相矛盾
    B6  legacy 采样参数下拉的竞态
        -> 清单 fetch 未返回就切模式 -> 下拉只剩 1 项且永不修复
    """

    def _app_js(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "web", "app.js"), encoding="utf-8") as fh:
            return fh.read()

    def _panel_js(self):
        """共享视频面板的源码（web/video-panel.js）。

        【2026-11-15 修复③ 交付项 3】
        首尾帧 / 参考生视频的采样参数控件搬到了共享面板里，
        所以这些断言要读**那个**文件，而不是 app.js。
        """
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "web", "video-panel.js"),
                  encoding="utf-8") as fh:
            return fh.read()

    def _html(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "web", "index.html"), encoding="utf-8") as fh:
            return fh.read()

    def test_B5_state_steps_initial_matches_workflow_default(self):
        """B5：步数的唯一真源是视频面板的 videoState.steps，state 不再持有它。

        【2026-11-16 第 4 轮审查 · B-11 修复后的契约】
        旧断言是「state.steps 初值必须是 4（不是 6）」，钉的是一个
        **写死的常量**（state.steps 全仓零写入）。它有两个问题：
          1. 写死的 4 在**降级路径**会被当成真实步数提交
             （8190 实测：面板显示 50、提交 4）；
          2. `assertNotIn("  steps: 6,", js)` 是**子串匹配**——
             H3-Image 的 `steps: 6,`（缩进 4 空格）也含这个子串，
             所以这条断言在基线就会误报失败（实测 6de4a42 同样失败）。

        现在的契约（修复后）：
          * app.js 的 state 里**不再有** steps 字段；
          * 步数真源在 web/video-panel.js 的 videoState.steps；
          * 提交时由 VIDEO_PANEL.submitParams() 带出，
            降级路径显式取 window.VIDEO_PANEL.state.steps。
        """
        js = self._app_js()
        # 1) state 里不再持有 steps（顶层 state 对象的字段里没有它）
        self.assertNotRegex(js, r"(?m)^  steps: \d+,",
                            "app.js 的 state 里仍有写死的 steps —— "
                            "步数真源必须是视频面板的 videoState.steps")
        # 2) 步数真源在视频面板
        vp = self._panel_js()
        self.assertRegex(vp, r"(?m)^\s+steps:\s*\d+,",
                         "web/video-panel.js 的 videoState 里没有 steps —— "
                         "步数真源必须是 videoState.steps")

    def test_B5_html_slider_default_matches_state(self):
        """B5 续：HTML 滑杆的初始 value 必须与 state.steps 一致（都=4）。

        【2026-11-15 修复③ 交付项 3 更新】
        旧的 #steps 滑杆已随 legacy 面板删除；
        共享面板的 #vidSteps 初始值是 4，与 state.steps 一致。
        """
        html = self._html()
        self.assertIn('id="vidSteps" min="1" max="50" step="1" value="4"', html)
        self.assertIn('id="vidStepsVal">4</b>', html)

    def test_B6_legacy_options_rerender_after_fetch(self):
        """B6：清单 fetch 完成后必须重渲染（共享）面板。

        否则「fetch 未返回就先切到 flf/r2v」会留下一个只有 1 项的下拉，
        而且永远不会修复。

        【2026-11-15 修复③ 交付项 3 更新】
        旧实现是 app.js 的 loadVideoSamplingOptions -> renderLegacyVideoSampling()；
        采样清单现在由**共享面板**（web/video-panel.js 的 loadVideoOptions）
        统一加载并渲染，三个视频模式共用这一套。

        【为什么断言的是函数体而不是整份文件】
        断言整份文件包含它会「永远绿」—— 变异测试实测过：
        删掉函数体里的那处调用，整份文件仍然包含它，断言不会红。
        → 必须**定位到那个函数体内部**再断言。
        """
        panel_js = self._panel_js()
        # 取出 loadVideoOptions 的函数体
        start = panel_js.find("async function loadVideoOptions()")
        self.assertNotEqual(start, -1, "找不到 loadVideoOptions")
        end = panel_js.find("\n  }", start)
        body = panel_js[start:end]
        self.assertIn(
            "renderVideoPanel(",
            body,
            "loadVideoOptions 拿到清单后没有重渲染面板（竞态未修）")
        # 且必须是在清单赋值**之后**（VIDEO_OPTIONS 赋值 -> renderVideoPanel）
        # 【2026-11-15 修复③ 交付项 3 更新】
        # 清单变量名从 VIDEO_SAMPLING_OPTIONS 变成 VIDEO_OPTIONS
        # （共享面板统一用 VIDEO_OPTIONS 收 /api/video-options 的全部字段），
        # 重渲染入口从 renderLegacyVideoSampling() 变成 renderVideoPanel()。
        self.assertLess(body.find("VIDEO_OPTIONS ="),
                        body.find("renderVideoPanel("),
                        "重渲染必须在清单赋值之后（否则还是拿旧值渲染）")

    def test_B6_legacy_select_falls_back_when_options_empty(self):
        """B6 续：候选清单为空时不能把下拉清成 0 项。

        【2026-11-15 修复③ 交付项 3 更新】
        旧的 fillVideoSelect 随 legacy 面板删除；
        共享面板的 renderSampling 里对采样器 / 调度器有同样的保底逻辑
        （current 不在 values 里 -> 追加一个 option）。
        """
        panel_js = self._panel_js()
        self.assertIn("if (!list.includes(videoState.sampler))", panel_js)
        self.assertIn("if (!list.includes(videoState.scheduler))", panel_js)


if __name__ == "__main__":
    unittest.main()
