# -*- coding: utf-8 -*-
"""接续点的**画质域**修复：跨段统计匹配（2026-11-17 接续点闪烁缺陷）。

【这个测试在防什么】
用户实测反馈：接续点（约 4–5 秒处）有一次**明显闪烁**。

取证结论（线上真实产物，帧级证据，见 docs 报告）：
  * 钉住区**逐位保住** —— 段2 钉住区 vs 段1 尾 7 步：max 差 4.8e-7
    （float32 舍入量级）→ **拷贝桥本身没问题**。
  * 但接续点的**色档跳变**：二采 ON 时是段内量级的 **29.52x**，
    同参数二采 OFF 时只有 **5.48x**（唯一变量 = 二采开关）。
  * 机理：SelfLift 的低分辨率段把钉住区内容**降采样**后当锚点
    （comfyui-SelfLift/nodes.py:299-306 `low_video = interpolate(...)`），
    抬升再回高分辨率 → 新生成区继承了低分辨率段的色档，
    于是缝头几帧是「低色档过渡区」。

【修法】在「裁重叠」之后加一跳 `H3RelayPost`（画质域）：
  * `match_prev=0.5`：把段头 12 帧的逐通道色档/曝光**对齐到 guide**
    （guide = 300:trim 第 4 路 prev_tail = 钉住区末帧 = 上段末帧）。
  * 节点自己的 tooltip 明写：「目标：压『成片缝上的亮度阶跃』」
    +「真正的用武之地是**拷贝桥**（那里 prev_tail 才是真·上段末帧）」。
  * `match_prev_stats_frames=1`：与单帧 guide 同口径 ⇒ 首帧被拉向 guide
    且**不会越过它**（0 = 旧口径 ⇒ 首帧被推过 guide、缝上凭空多出阶跃，
    节点文档记的真渲染实测 ×12.6）。
  * **帧数守恒、不动音频、零采样开销**。

【断言纪律】
  * 期望值是**字面量**（节点名、参数值），不是从被测函数输出反推。
  * 断言要由「命名该行为的那条断言」抓住。

运行：python3 -m pytest tests/test_cont_seam_quality.py -v
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import continuation as cont            # noqa: E402
from h3studio import video_workflow as vw            # noqa: E402
from h3studio import h3_video_modes as hvm           # noqa: E402


def _cont_params(job_id="J2", chain="J1"):
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
    info = _cont_params(job_id)["continuation"]
    info.update({"chain_id": job_id, "stage_index": 0, "source_job": "",
                  "source_segment": -1, "source_latent_run_id": "",
                  "source_latent_path": "", "anchor_latent_run_id": "",
                  "anchor_latent_path": "", "chain_total_frames": 0})
    return {"continuation": info}


def _t2v(job_id="J2", twopass=None, **extra):
    params = {
        "job_id": job_id, "mode": "t2v", "prompt": "一只猫在跑",
        "model_key": "fl2va_pruned_int8", "duration": 5.0,
        "width": 1280, "height": 736, "steps": 4, "seed": 12345,
        "sampler": "euler", "scheduler": "beta",
    }
    if twopass:
        params["twopass"] = twopass
    params.update(extra)
    return params


_TWOPASS = {"enabled": True, "transition_step": 3, "lowres_scale": 0.75,
             "rho": 0.0, "w_min": 0.8, "w_max": 1.0,
             "upscaler_model": "minimax_h3_latent_upscaler_3d_fp16.safetensors",
             "cfg": 1.0}


class TestSeamQualityNodeWiring(unittest.TestCase):
    """接续段必须经过「跨段统计匹配」（300:post）。"""

    def test_t2v_continuation_has_post_node(self):
        """t2v 接续段必须有 300:post（H3RelayPost）。"""
        p = vw.build_t2v_panel(_t2v("J2", _TWOPASS, **_cont_params()))
        self.assertIn("300:post", p, "接续段必须有跨段统计匹配节点")
        self.assertEqual(p["300:post"]["class_type"], "H3RelayPost")

    def test_flf_continuation_has_post_node(self):
        """flf 接续段必须有 300:post。"""
        params = _t2v("J2", _TWOPASS, **_cont_params())
        params["mode"] = "flf"
        params["model_key"] = "fl2va_pruned_int8"
        params["first_frame"] = "first.png"
        params["last_frame"] = "last.png"
        p = hvm.build_flf(params)
        self.assertIn("300:post", p, "首尾帧接续段必须有跨段统计匹配节点")
        self.assertEqual(p["300:post"]["class_type"], "H3RelayPost")

    def test_r2v_continuation_has_post_node(self):
        """r2v 接续段必须有 300:post。"""
        params = _t2v("J2", _TWOPASS, **_cont_params())
        params["mode"] = "r2v"
        params["model_key"] = "ref2va_pruned_int8"
        params["ref_images"] = ["ref0.png"]
        p = hvm.build_r2v(params)
        self.assertIn("300:post", p, "参考生接续段必须有跨段统计匹配节点")
        self.assertEqual(p["300:post"]["class_type"], "H3RelayPost")

    def test_post_takes_trimmed_images_and_prev_tail_guide(self):
        """300:post 的画面来自裁重叠、guide 来自 trim 的第 4 路。

        guide 必须是 prev_tail（钉住区末帧 = 上段末帧）——
        节点文档明确：拷贝桥路线下 prev_tail 才是**真·上段末帧**。
        """
        p = vw.build_t2v_panel(_t2v("J2", _TWOPASS, **_cont_params()))
        post = p["300:post"]["inputs"]
        self.assertEqual(post["images"], ["300:trim", 0])
        self.assertEqual(post["guide"], ["300:trim", 3])

    def test_post_match_prev_is_enabled_with_safe_guardrails(self):
        """match_prev 必须 > 0，且护栏（增益/偏移上限）保持节点推荐值。"""
        p = vw.build_t2v_panel(_t2v("J2", _TWOPASS, **_cont_params()))
        post = p["300:post"]["inputs"]
        self.assertGreater(post["match_prev"], 0.0,
                           "match_prev 必须开启，否则缝上的色档阶跃不会被压")
        self.assertEqual(post["match_prev_gain_max"], 1.15)
        self.assertEqual(post["match_prev_offset_max"], 0.06)

    def test_post_stats_frames_is_one(self):
        """match_prev_stats_frames 必须是 1（与单帧 guide 同口径）。

        节点文档：0 = 旧口径 -> 首帧被推过 guide、缝上凭空多出阶跃
        （真渲染实测 ×12.6），**仅作对照档，勿用于产线**。
        """
        p = vw.build_t2v_panel(_t2v("J2", _TWOPASS, **_cont_params()))
        self.assertEqual(p["300:post"]["inputs"]["match_prev_stats_frames"], 1)

    def test_post_does_not_touch_timeline_or_audio(self):
        """300:post 只改画质：帧数守恒、不动音频。

        → 音频仍然直接取 300:trim（不能经过 post）。
        """
        p = vw.build_t2v_panel(_t2v("J2", _TWOPASS, **_cont_params()))
        self.assertEqual(p["91"]["inputs"]["audio"], ["300:trim", 1],
                         "音频必须直接取裁重叠后的音轨（post 不动音频）")
        self.assertEqual(p["91"]["inputs"]["images"], ["300:post", 0])

    def test_post_other_quality_items_stay_off(self):
        """其余画质域项保持 0 —— 不引入额外处理，也不改时间轴。"""
        p = vw.build_t2v_panel(_t2v("J2", _TWOPASS, **_cont_params()))
        post = p["300:post"]["inputs"]
        for key in ("lowfreq_pull", "hist_match", "wb_match",
                    "settle_sharpen", "settle_auto"):
            self.assertEqual(post[key], 0,
                             f"{key} 应保持 0（只开 match_prev）")

    def test_plain_generation_has_no_post_node(self):
        """普通生成（无上游）不该有 300:post —— 改了会改变既有出片结果。"""
        p = vw.build_t2v_panel(_t2v("J1", _TWOPASS, **_chain_head_params()))
        self.assertNotIn("300:post", p,
                         "链首段（无上游）不该有跨段统计匹配")


class TestSeamQualityRegressionGuard(unittest.TestCase):
    """把「为什么是 match_prev=0.5」锁成可核对的数字。"""

    def test_match_prev_strength_is_half(self):
        """match_prev 强度 = 0.5。

        依据：Relay-Kit 节点文档的推荐档「0.4–1.0」（先试 0.6），
        取 0.5 是**保守**档 —— 它只对齐统计量、不复制姿态，
        护栏（gain_max=1.15 / offset_max=0.06）保证不把已通过的内容改坏。
        """
        p = vw.build_t2v_panel(_t2v("J2", _TWOPASS, **_cont_params()))
        self.assertEqual(p["300:post"]["inputs"]["match_prev"], 0.5)

    def test_match_prev_frames_covers_seam_window(self):
        """作用帧数必须覆盖缝头（12 帧，与 nonebot v1 的 326 节点一致）。"""
        p = vw.build_t2v_panel(_t2v("J2", _TWOPASS, **_cont_params()))
        self.assertEqual(p["300:post"]["inputs"]["match_prev_frames"], 12)

    def test_context_frames_unchanged(self):
        """本次修复**不**动钉住窗口（22 帧）—— 只加画质域一跳。"""
        p = vw.build_t2v_panel(_t2v("J2", _TWOPASS, **_cont_params()))
        self.assertEqual(p["300:bridge"]["inputs"]["context_frames"], 22)
        self.assertEqual(p["300:trim"]["inputs"]["settle_frames"], 0)
        self.assertEqual(p["300:trim"]["inputs"]["seam_ghost"], 0)


if __name__ == "__main__":
    unittest.main()
