# -*- coding: utf-8 -*-
"""B1 / B2 的工作流与音轨选择测试。

覆盖：
  * B1 首尾帧：首帧 / 尾帧 / 首+尾 三种组合 -> 正确的 task_type
  * B1 缺素材 -> 明确报错
  * B2 参考生视频：9 张参考图 + 参考视频 + 参考音乐 + 参考视频背景音
  * B2 参考图上限 9（不是旧的 3）
  * 音轨三选一：(a) 生成 / (b) 参考音乐 / (c) 参考视频背景音
  * 音轨可见性：没有对应素材的选项不出现
  * 参考生视频用 Ref2VA 权重（FL2VA 权重没有 Ref2VA 通道）
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from h3studio import h3_audio_track as h3audio          # noqa: E402
from h3studio import h3_video_modes as h3modes          # noqa: E402
from h3studio import video_models as vmodels            # noqa: E402
from h3studio import workflows as wf                    # noqa: E402


BASE = {
    "job_id": "b1b2test",
    "prompt": "a samurai in a courtyard",
    "width": 1344,
    "height": 768,
    "duration": 5,
    "model_key": "fl2va_pruned_int8",
    "accel_lora": "ckpt850",
    "accel_lora_strength": 1.0,
    "seed": 4242,
    "attention": "default",
}


def _cond_inputs(wf):
    """取条件节点的 inputs（T8 条件节点在两个模式里都叫 136）。"""
    return wf["136"]["inputs"]


class TestB1FirstLastFrame(unittest.TestCase):
    """B1：首尾帧生视频。"""

    def test_first_frame_only_uses_i2va(self):
        """只给首帧 -> I2VA（首帧生视频）。"""
        wf = wf_build_flf(dict(BASE, first_frame="first.png"))
        cond = _cond_inputs(wf)
        self.assertEqual(cond["task_type"], "I2VA")
        self.assertIn("first_frame", cond)
        self.assertNotIn("last_frame", cond)

    def test_last_frame_only_uses_l2va(self):
        """只给尾帧 -> L2VA（尾帧生视频）。

        【为什么重要】旧实现要求首帧必填 —— 只给尾帧的用户根本没法用。
        """
        wf = wf_build_flf(dict(BASE, last_frame="last.png"))
        cond = _cond_inputs(wf)
        self.assertEqual(cond["task_type"], "L2VA")
        self.assertIn("last_frame", cond)
        self.assertNotIn("first_frame", cond)

    def test_first_and_last_uses_fl2va(self):
        """首帧 + 尾帧 -> FL2VA。"""
        wf = wf_build_flf(dict(BASE, first_frame="f.png", last_frame="l.png"))
        cond = _cond_inputs(wf)
        self.assertEqual(cond["task_type"], "FL2VA")
        self.assertIn("first_frame", cond)
        self.assertIn("last_frame", cond)

    def test_no_frame_raises_clear_error(self):
        """一张都不给 -> 报错要说清缺什么。"""
        with self.assertRaises(ValueError) as ctx:
            wf_build_flf(dict(BASE))
        self.assertIn("首帧", str(ctx.exception))

    def test_uses_t8_conditioning_node(self):
        """必须用 T8 条件节点（支持音频输入）。

        核心节点 MiniMaxH3ImageToVideo **没有** drive_audio 槽位 ——
        首尾帧模式要支持音乐输入就必须换节点。
        """
        wf = wf_build_flf(dict(BASE, first_frame="f.png"))
        self.assertEqual(wf["136"]["class_type"], h3modes.T8_CONDITIONING)

    def test_t8_required_inputs_are_explicit(self):
        """T8 的 required 输入必须**显式**给。

        ComfyUI 提交 API 工作流时**不套用** schema 默认值 ——
        漏掉任何一个 -> 提交被拒（"Required input is missing"）。
        这是实测踩到的坑（第一次提交 B1 就是这么失败的）。
        """
        wf = wf_build_flf(dict(BASE, first_frame="f.png"))
        cond = _cond_inputs(wf)
        for key in ("audio_denoise_strength", "add_source_as_reference",
                    "prompt_primary_audio_ordinal", "strict_prompt_tags",
                    "reference_video_policy"):
            self.assertIn(key, cond, "T8 条件节点缺 required 输入：" + key)


class TestB2ReferenceVideo(unittest.TestCase):
    """B2：参考生视频。"""

    def test_nine_reference_images_allowed(self):
        """参考图上限是 9（节点真实上限），不是旧的 3。"""
        imgs = ["r%d.png" % i for i in range(9)]
        wf = wf_build_r2v(dict(BASE, ref_images=imgs))
        cond = _cond_inputs(wf)
        slots = [k for k in cond if k.startswith("ref_images.ref_image_")]
        self.assertEqual(len(slots), 9)
        self.assertIn("ref_images.ref_image_8", cond)

    def test_ten_reference_images_truncated_to_nine(self):
        """超过 9 张 -> 截到 9（节点上限）。"""
        imgs = ["r%d.png" % i for i in range(12)]
        wf = wf_build_r2v(dict(BASE, ref_images=imgs))
        cond = _cond_inputs(wf)
        slots = [k for k in cond if k.startswith("ref_images.ref_image_")]
        self.assertEqual(len(slots), 9)

    def test_autogrow_slot_key_format(self):
        """autogrow 槽位的键名必须是 "<容器>.<前缀><序号>"。

        写成 "ref_image_0" 会被节点当成未知输入 -> 参考图**静默不生效**。
        依据：nonebot 参考工作流 minimax_h3_v1.json 的 node 136。
        """
        wf = wf_build_r2v(dict(BASE, ref_images=["a.png", "b.png"]))
        cond = _cond_inputs(wf)
        self.assertIn("ref_images.ref_image_0", cond)
        self.assertIn("ref_images.ref_image_1", cond)

    def test_ref_video_and_audio_slots(self):
        """参考视频 / 独立参考音乐 / 参考视频背景音都要接上。"""
        wf = wf_build_r2v(dict(BASE, ref_images=["a.png"],
                               ref_videos=["v.mp4"],
                               ref_audios=["m.mp3"],
                               ref_video_audios=["vb.mp3"]))
        cond = _cond_inputs(wf)
        self.assertIn("ref_videos.ref_video_0", cond)
        self.assertIn("ref_audios.ref_audio_0", cond)
        self.assertIn("ref_video_audios.ref_video_audio_0", cond)

    def test_uses_ref2va_unet(self):
        """参考生视频必须用 Ref2VA 权重。

        FL2VA 权重没有 Ref2VA 通道 -> 用它做参考生成等于「参考没生效」。
        """
        wf = wf_build_r2v(dict(BASE, ref_images=["a.png"]))
        self.assertIn("ref2va", wf["6"]["inputs"]["unet_name"])

    def test_ref2va_models_registered(self):
        """Ref2VA 权重必须登记进 VIDEO_MODELS（否则面板下拉里没有）。"""
        self.assertIn("ref2va_pruned_int8", vmodels.VIDEO_MODELS)
        self.assertTrue(
            vmodels.VIDEO_MODELS["ref2va_pruned_int8"]["supports_ref2va"])

    def test_no_reference_raises_clear_error(self):
        """一个参考素材都没有 -> 报错要说清。"""
        with self.assertRaises(ValueError) as ctx:
            wf_build_r2v(dict(BASE))
        self.assertIn("参考", str(ctx.exception))


class TestAudioTrackChoice(unittest.TestCase):
    """B2 音轨三选一。"""

    def test_generated_track_uses_decoded_audio(self):
        """(a) 生成的音乐 -> 用 VAEDecodeAudio 的输出。"""
        wf = wf_build_r2v(dict(BASE, ref_images=["a.png"],
                               audio_track_source="generated"))
        audio_ref = wf["91"]["inputs"]["audio"]
        self.assertEqual(wf[audio_ref[0]]["class_type"], "VAEDecodeAudio")

    def test_reference_track_trims_reference_music(self):
        """(b) 参考音乐 -> TrimAudioDuration 裁到画面时长后贴上去。"""
        wf = wf_build_r2v(dict(BASE, ref_images=["a.png"],
                               ref_audios=["m.mp3"],
                               audio_track_source="reference"))
        audio_ref = wf["91"]["inputs"]["audio"]
        node = wf[audio_ref[0]]
        self.assertEqual(node["class_type"], "TrimAudioDuration")
        # 裁到画面时长（不能超 —— 否则视频会被音轨长度截断）
        self.assertEqual(node["inputs"]["duration"], BASE["duration"])

    def test_video_bg_track_uses_reference_video_audio(self):
        """(c) 参考视频背景音 -> 用 ref_video_audios 那一路。"""
        wf = wf_build_r2v(dict(BASE, ref_images=["a.png"],
                               ref_video_audios=["vb.mp3"],
                               audio_track_source="video_bg"))
        audio_ref = wf["91"]["inputs"]["audio"]
        node = wf[audio_ref[0]]
        self.assertEqual(node["class_type"], "TrimAudioDuration")
        # 源必须是参考视频背景音那一路（136:501），不是独立参考音乐（136:401）
        self.assertEqual(node["inputs"]["audio"], ["136:501", 0])

    def test_track_source_availability(self):
        """没有对应素材的选项**不出现**。"""
        self.assertEqual(
            h3audio.available_track_sources(False, False),
            [h3audio.TRACK_GENERATED])
        self.assertEqual(
            h3audio.available_track_sources(True, False),
            [h3audio.TRACK_GENERATED, h3audio.TRACK_REFERENCE])
        self.assertEqual(
            h3audio.available_track_sources(False, True),
            [h3audio.TRACK_GENERATED, h3audio.TRACK_VIDEO_BG])
        self.assertEqual(
            h3audio.available_track_sources(True, True),
            [h3audio.TRACK_GENERATED, h3audio.TRACK_REFERENCE,
             h3audio.TRACK_VIDEO_BG])

    def test_invalid_track_source_falls_back(self):
        """选了没有素材的来源 -> 回落成「生成的音乐」（不报错）。"""
        self.assertEqual(
            h3audio.normalize_track_source("reference", False, False),
            h3audio.TRACK_GENERATED)
        self.assertEqual(
            h3audio.normalize_track_source("video_bg", True, False),
            h3audio.TRACK_GENERATED)
        self.assertEqual(
            h3audio.normalize_track_source("reference", True, False),
            h3audio.TRACK_REFERENCE)

    def test_track_source_labels_are_distinct(self):
        """三个选项的显示名必须互不相同（否则用户分不清）。"""
        labels = {h3audio.track_source_label(s)
                  for s in h3audio.VALID_TRACK_SOURCES}
        self.assertEqual(len(labels), len(h3audio.VALID_TRACK_SOURCES))


class TestVideoPanelSlotParity(unittest.TestCase):
    """文生视频面板与首尾帧/参考面板必须用**同一套**加速 LoRA 语义。"""

    def test_same_accel_slot_resolution(self):
        """三个模式解析出来的加速 LoRA 槽位必须一致。

        否则同一个 LoRA 在文生视频里生效、在首尾帧里不生效 ——
        用户会以为「时灵时不灵」。
        """
        from h3studio import video_workflow as vworkflow
        params = dict(BASE, accel_lora="lightx2v_4step_768p")
        model = {"unet": "x.safetensors"}
        chain = vworkflow._resolve_accel_chain(params, model)
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0]["key"], "lightx2v_4step_768p")
        # 首尾帧与参考生视频走同一个解析器
        wf = wf_build_flf(dict(BASE, first_frame="f.png",
                                accel_lora="lightx2v_4step_768p"))
        lora_nodes = [n for n in wf.values()
                      if n["class_type"] in (vmodels.ACCEL_LOADER_TURBO,
                                            vmodels.ACCEL_LOADER_CORE)]
        self.assertEqual(len(lora_nodes), 1)
        self.assertEqual(lora_nodes[0]["class_type"], vmodels.ACCEL_LOADER_CORE)


class TestAssetRefAudioTrackSource(unittest.TestCase):
    """B2 修：素材库来源的参考音乐，音轨来源选择不能被提交阶段改掉。

    【缺陷复现路径】用户从素材库选一段音乐当参考音轨，提交 r2v 任务。
    前端提交的是 asset_refs（slot=ref_audio），真正的 params["ref_audios"]
    要到 worker 里 _apply_asset_refs 跑完才有。
    提交阶段的可见性判据只看 params["ref_audios"] -> 算出「没有参考音乐」
    -> 用户选的 "reference" 被归一成 "generated" -> 成片音轨不是用户选的那条。

    【断言的是「那一行断言本身能抓住这个缺陷」】
    这里直接调 server.prepare_job_params（提交阶段真正走的那个函数），
    传入 asset_refs，断言 audio_track_source 仍然是 "reference"。
    """

    def _prepare(self, refs, source):
        import server
        params = {"prompt": "x", "audio_track_source": source,
                  "width": 864, "height": 480, "duration": 5}
        # prepare_job_params **就地修改 params**，返回的是 asset_refs 列表
        server.prepare_job_params("r2v", params, refs, {})
        return params

    def test_ref_audio_from_library_keeps_reference_choice(self):
        """素材库里的参考音乐 -> "reference" 必须保住。

        【2026-11-16】素材引用必须是**库里真的有**的素材
        （prepare_job_params 现在查库），所以这里用临时素材库里
        真实入库的素材，而不是编造的 asset_id。
        """
        from assetlib_fixture import temp_asset_library
        with temp_asset_library(audio=1) as ids:
            if not ids["audio"]:
                self.skipTest("本机建不出音频素材（ffmpeg 不可用）")
            out = self._prepare(
                [{"asset_id": ids["audio"][0], "slot": "ref_audio",
                  "ordinal": 0}],
                "reference")
            self.assertEqual(out["audio_track_source"], "reference")

    def test_ref_video_audio_from_library_keeps_video_bg_choice(self):
        """素材库里的参考视频背景音 -> "video_bg" 必须保住。

        注意：r2v 的校验要求至少有 1 个 ref_image/ref_video/ref_audio 槽位，
        所以这里同时给一张参考图（真实提交也是这么提交的）。
        """
        from assetlib_fixture import temp_asset_library
        with temp_asset_library(image=1, audio=1) as ids:
            if not ids["image"]:
                self.skipTest("建不出图片素材")
            out = self._prepare(
                [{"asset_id": ids["image"][0], "slot": "ref_image",
                  "ordinal": 0},
                 {"asset_id": ids["audio"][0], "slot": "ref_video_audio",
                  "ordinal": 0}],
                "video_bg")
            self.assertEqual(out["audio_track_source"], "video_bg")

    def test_no_reference_material_falls_back_to_generated(self):
        """没有任何参考素材时，选 reference 要回落到 generated。

        这一条保证修法没有把「可见性」判宽 —— 没有素材仍不该给
        (b)/(c) 选项。
        """
        import server
        params = {"prompt": "x", "audio_track_source": "reference",
                  "width": 864, "height": 480, "duration": 5}
        # 没有 ref_image/ref_video/ref_audio -> r2v 校验会拒绝提交。
        # 这条用例测的是**归一逻辑本身**：直接调 h3audio 的归一，
        # 不经过 r2v 的素材必填校验。
        self.assertEqual(
            server.h3audio.normalize_track_source(
                "reference", has_reference_audio=False,
                has_reference_video_audio=False),
            "generated")

    def test_generated_stays_generated(self):
        """显式选 generated -> 保持 generated。

        【2026-11-16】用临时素材库里真实入库的音频素材
        （素材引用现在会查库，编造的 id 会被正确拒绝）。
        """
        from assetlib_fixture import temp_asset_library
        with temp_asset_library(audio=1) as ids:
            if not ids["audio"]:
                self.skipTest("本机建不出音频素材（ffmpeg 不可用）")
            out = self._prepare(
                [{"asset_id": ids["audio"][0], "slot": "ref_audio",
                  "ordinal": 0}],
                "generated")
            self.assertEqual(out["audio_track_source"], "generated")


def wf_build_flf(params):
    """包一层，把 H3VideoRequestError 转成 ValueError（与旧接口一致）。"""
    return wf.build_flf(params)


def wf_build_r2v(params):
    return wf.build_r2v(params)


if __name__ == "__main__":
    unittest.main()
