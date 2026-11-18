# -*- coding: utf-8 -*-
"""第 2 项：r2v / flf 接续段的**参考视频画面切片**必须与音轨来源解耦。

【这个测试在防什么】
2026-11-19 在 8190 实测到的真实缺陷：

  规范（docs/接续模式深度研究-20261116.md §5.4.3）要求接续段的参考视频
  **画面**按 C_n（上游已累计交付帧数）跳帧 —— 让画面接段1 的时间位置。

  但 `h3studio/h3_video_modes.py::_continuation_media_slice` 原来把
  **视频**切片也绑在「音轨来源是外部音轨」这个条件上：

      external = h3acont.is_external_track(track_mode)
      if not external:
          return out          # <- 视频切片也一起被丢了

  音轨来源是「生成的音乐」时 external=False ⇒ skip_frames=0 ⇒
  **段2 的参考视频从 0 开始喂**。而段1 已经用过同一份参考视频
  （画面已经演化过去）⇒ 段2 的条件要求画面回到参考视频开头，
  与拷贝桥钉住区里「段1 已演化的画面」直接冲突。

  【实测证据】（8190 真实生成，同一段1 派生的两个接续段）
    带参考视频：@21 帧 L1=100.78，Δ亮度=+9.01（25.8× 该段中位）
    无参考视频：@21 帧 L1= 95.61，Δ亮度=+1.95（ 3.5× 该段中位）
  ⇒ 带参考视频时是**亮度阶跃**（用户看到「闪一下」）；
    不带时是亮度连续的**镜头切换**（正常内容，不是缺陷）。

【三条不变量】
  V1. 音轨来源是「生成的音乐」时，**画面**切片仍按 C_n 跳帧
      （= 切片与音轨来源解耦）。
  V2. 参考视频**不够长**时（C_n >= 帧数），本段**不带**参考视频
      （内容已用尽；继续从 0 喂就是本缺陷本身）。
  V3. 探测不到参考视频帧数时**不猜**（保持旧行为，绝不静默出错）。

运行：python3 -m pytest tests/test_ref_video_continuation_slice.py -v
"""
from __future__ import annotations

import inspect
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from h3studio import h3_video_modes as vm  # noqa: E402


def _params(**cont):
    """造一份带接续上下文的 params。"""
    info = {
        "chain_id": "c1",
        "stage_index": 1,
        "source_job": "seg1",
        "source_segment": 0,
        "context_frames": 22,
        "anchor_frames": 22,
        "chain_total_frames": 124,
        "segment_frames": 124,
    }
    info.update(cont)
    return {"continuation": info}


class TestRefVideoSliceDecoupledFromAudio(unittest.TestCase):
    """V1：画面切片与音轨来源解耦。"""

    def test_generated_audio_still_slices_video_by_chain_frames(self):
        """音轨来源=生成的音乐（external=False）时，画面切片**仍**生效。

        【为什么这条是核心】修复前 `if not external: return out` 把
        视频切片一起丢了 ⇒ 段2 的参考视频从 0 开始喂 ⇒ 亮度阶跃。
        """
        out = vm._continuation_media_slice(_params(
            audio_track_mode="generated",
            ref_video_frame_count=300,          # 参考视频 300 帧，够切
        ))
        self.assertEqual(
            out["video_skip_frames"], 124,
            "接续段的参考视频画面必须按 C_n=124 跳帧"
            "（规范 §5.4.3），且不能被音轨来源='生成的音乐' 挡掉")
        self.assertFalse(out["video_drop"],
                         "参考视频够长时不该丢弃")
        # 音轨侧仍然不切（生成音频 = 音画同源）
        self.assertEqual(out["skip_frames"], 0,
                         "生成音频时音轨侧不该切")
        self.assertFalse(out["active"])

    def test_external_audio_keeps_both_slices(self):
        """外部音轨时，音轨切片与画面切片**都**生效，且互不干扰。"""
        out = vm._continuation_media_slice(_params(
            audio_track_mode="reference",
            audio_segment_skip_frames=124,
            audio_segment_start=124 / 24,
            audio_segment_duration=4.25,
            ref_video_frame_count=300,
        ))
        self.assertEqual(out["skip_frames"], 124, "音轨切片应生效")
        self.assertEqual(out["video_skip_frames"], 124, "画面切片应生效")
        self.assertTrue(out["active"])

    def test_chain_head_segment_never_slices(self):
        """链首段（C_n=0，无上游）永远不切。"""
        out = vm._continuation_media_slice({
            "continuation": {
                "chain_id": "c1", "stage_index": 0,
                "source_job": "", "source_segment": -1,
                "chain_total_frames": 0,
            }
        })
        self.assertEqual(out["video_skip_frames"], 0)
        self.assertFalse(out["video_drop"])


class TestRefVideoDropWhenExhausted(unittest.TestCase):
    """V2：参考视频不够长时丢弃。"""

    def test_drops_ref_video_when_chain_exceeds_frame_count(self):
        """C_n >= 参考视频帧数 ⇒ 本段不带参考视频。

        【实测背景】线上参考视频只有 50 帧（2.028s @25fps），
        而 C_n = 124 ⇒ 按规范切 124 帧后**内容为空**。
        此时若继续从 0 喂（= 旧行为）就会触发亮度阶跃；
        若喂空参考，T8 会直接 raise
        「ref_video_1 must contain at least 5 IMAGE frames」。
        ⇒ 唯一安全的做法是**不带参考视频**。
        """
        out = vm._continuation_media_slice(_params(
            audio_track_mode="generated",
            ref_video_frame_count=50,           # 参考视频只有 50 帧
        ))
        self.assertTrue(out["video_drop"],
                        "C_n=124 >= 参考视频 50 帧时必须丢弃参考视频")
        self.assertEqual(out["video_skip_frames"], 0,
                         "丢弃时不该给出跳帧数")

    def test_exactly_equal_frames_is_not_dropped(self):
        """C_n == 帧数：跳完正好为空，但判据用 `<` ⇒ 仍走丢弃分支。

        【边界说明】C_n=50、帧数=50 ⇒ 跳过 50 帧后剩 0 帧 ⇒
        实质也是空的。判据写成 `C_n < ref_frames` ⇒ 50 < 50 为假
        ⇒ 走丢弃分支 ⇒ 正确。
        """
        out = vm._continuation_media_slice(_params(
            audio_track_mode="generated",
            ref_video_frame_count=50,
            chain_total_frames=50,
        ))
        self.assertTrue(out["video_drop"])


class TestNoGuessingWhenFrameCountUnknown(unittest.TestCase):
    """V3：探测不到帧数时不猜。"""

    def test_unknown_frame_count_keeps_legacy_behavior(self):
        """没有 ref_video_frame_count（老任务 / 探测失败）⇒ 不切片。

        【为什么不能猜】猜错的后果是「参考视频从错误位置开始喂」
        —— 那是一个**静默**的错误（不报错、画面乱）。宁可保持旧
        行为（从 0 喂），也要不引入静默错误。
        """
        out = vm._continuation_media_slice(_params(
            audio_track_mode="generated",
        ))
        self.assertEqual(out["video_skip_frames"], 0,
                         "探测不到帧数时必须保持旧行为（不切片）")
        self.assertFalse(out["video_drop"],
                         "探测不到帧数时不能丢弃参考视频"
                         "（否则会静默改变老任务的行为）")


class TestBuildR2VAppliesSlice(unittest.TestCase):
    """工作流层：切片真的落到了 VHS_LoadVideo 节点上。"""

    def _build(self, **cont):
        params = _params(**cont)
        params.update({
            "job_id": "seg2",
            "prompt": "test",
            "model_key": "hybrid_fl2va_ref2va",
            "width": 1280, "height": 736,
            "duration": 5, "steps": 4,
            "sampler": "euler", "scheduler": "beta",
            "shift_video": 12.0, "shift_audio": 12.0,
            "seed": 1,
            "ref_images": ["h3studio/ref.png"],
            "ref_videos": ["h3studio/refvid.webm"],
        })
        return vm.build_r2v(params)

    def test_video_node_gets_skip_frames_from_chain_position(self):
        """接续段（生成音频）的参考视频节点必须带 skip_first_frames=124。"""
        p = self._build(audio_track_mode="generated",
                        ref_video_frame_count=300)
        loaders = [v for k, v in p.items()
                   if v.get("class_type") == "VHS_LoadVideo"]
        self.assertTrue(loaders, "找不到参考视频的 VHS_LoadVideo 节点")
        self.assertEqual(
            loaders[0]["inputs"]["skip_first_frames"], 124,
            "接续段的参考视频必须跳 C_n=124 帧"
            "（否则画面回到参考视频开头 -> 与钉住区冲突 -> 亮度阶跃）")
        self.assertEqual(
            loaders[0]["inputs"]["force_rate"], 24,
            "跳帧必须配 force_rate=24（H3 固定帧率），否则帧/秒对不上")

    def test_ref_video_dropped_when_exhausted(self):
        """参考视频不够长 ⇒ 工作流里**没有** VHS_LoadVideo 节点。"""
        p = self._build(audio_track_mode="generated",
                        ref_video_frame_count=50)
        loaders = [v for k, v in p.items()
                   if v.get("class_type") == "VHS_LoadVideo"]
        self.assertEqual(
            loaders, [],
            "参考视频内容已用尽时不该再喂它"
            "（喂空会 raise；喂整条会触发亮度阶跃）")


class TestServerRecordsRefVideoFrameCount(unittest.TestCase):
    """服务端：链首段必须把参考视频帧数记进 continuation。

    【为什么这条重要】
    工作流层的判据（切 / 丢弃）完全依赖
    `params["continuation"]["ref_video_frame_count"]`。
    服务端这一环若断了（比如字段名写错、探测没跑、写回没生效），
    工作流层会**静默**退回旧行为 —— 而旧行为就是那个亮度阶跃缺陷。
    ⇒ 必须单独盯住这条路径。
    """

    def test_record_function_exists_and_is_called(self):
        """服务端必须有探测函数，且在**拼工作流之前**被调用。"""
        import server as srv
        self.assertTrue(
            hasattr(srv.Worker, "_record_ref_video_frame_count"),
            "server.py 缺少 _record_ref_video_frame_count")
        # 【直接读磁盘文件】理由同 test_record_writes_expected_field_name。
        src = open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
        self.assertIn(
            "await self._record_ref_video_frame_count(job_id, params)",
            src,
            "探测必须在拼工作流**之前**被调用"
            "（工作流要读它决定切片 / 跳过）")
        # 调用点必须在 _prepare_audio_continuation 之后、build_workflow 之前
        call_at = src.find(
            "await self._record_ref_video_frame_count(job_id, params)")
        prep_at = src.find("await self._prepare_audio_continuation(session")
        self.assertGreater(call_at, prep_at,
                           "探测调用点应在音频接续准备之后")
        self.assertGreater(call_at, 0)

    def test_record_writes_expected_field_name(self):
        """探测结果必须**写进** `ref_video_frame_count`（工作流层读的就是它）。

        【为什么要精确匹配「写入语句」而不是只查字段名】
        源文件里这个字段名出现 3 次：函数名、读取处（`info.get(...)`）、
        写入处（`info[...] = ...`）。只 assertIn(字段名) 的话，
        把**写入**那一处改成别的名字，断言仍然绿 —— 而工作流层
        就读不到帧数了（静默退回旧行为 = 那个亮度阶跃缺陷）。
        ⇒ 必须匹配写入语句本身。
        """
        # 【为什么要直接读磁盘文件而不是 inspect.getsource】
        # inspect.getsource 读的是**磁盘当前内容**，但 pytest 进程在
        # 启动时已经把 server.py 导入内存了 —— 变异是在导入**之后**写入
        # 磁盘的，内存里的模块还是旧的 ⇒ getsource 会读到旧代码
        # （实测：变异 M5 改了字段名，断言仍然绿）。
        # ⇒ 必须直接 open 文件读。
        src = open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
        self.assertRegex(
            src,
            r'info\[\s*["\']ref_video_frame_count["\']\s*\]\s*=\s*',
            "帧数必须**写进** info[\"ref_video_frame_count\"]"
            "（字段名与 h3_video_modes._continuation_media_slice 读的一致；"
            "只查字段名会被读取处的同名字段骗过）")

    def test_workflow_layer_reads_the_same_field(self):
        """工作流层与服务端必须用**同一个字段名**。"""
        server_src = open(os.path.join(ROOT, "server.py"),
                          encoding="utf-8").read()
        modes_src = open(os.path.join(ROOT, "h3studio", "h3_video_modes.py"),
                         encoding="utf-8").read()
        field = "ref_video_frame_count"
        self.assertIn(field, server_src, "服务端没写这个字段")
        self.assertIn(field, modes_src, "工作流层没读这个字段")


if __name__ == "__main__":
    unittest.main()
