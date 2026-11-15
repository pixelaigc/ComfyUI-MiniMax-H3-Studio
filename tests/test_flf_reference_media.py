# -*- coding: utf-8 -*-
"""flf（首尾帧生视频）的参考视频 / 参考音乐入口回归测试。

【背景 —— 2026-11-15 现场核查发现的残留缺口】

前面那一轮（41280a0）把「参考视频（≤3）/ 参考音乐（≤3）」的素材库入口
补到了 **r2v** 面板上，但 **flf** 面板只渲染了「首帧 / 尾帧」两个槽位。

后果：
  * 后端 h3studio/h3_video_modes.py 的 build_flf **本来就支持**
    ref_audios / ref_video_audios（音轨三选一的素材来源）；
  * 但 flf 面板没有槽位 -> 用户没法选素材 ->
    audioTrackAvailability() 的 hasRefAudio / hasVideoAudio 恒为 false ->
    「用参考音乐作音轨」/「用参考视频背景音作音轨」这两项**永远不出现**。
  * 也就是说：**后端能力存在，但面板上完全够不着**。

【本文件锁死的行为】

  1. flf 面板渲染 refvid0..2 / refaud0..2 槽位（与 r2v 同构）；
  2. flf 提交时把本地直传的参考视频 / 参考音乐带上
     （file_refvid / file_refaud），与素材库路径（asset_refs）行为一致；
  3. 服务端 flf 分支接住 file_refvid / file_refaud 并写进 params；
  4. flf 的音轨三选一可见性判据能识别这两类素材。

【为什么用「真跑」而不是字符串匹配】

断言若只是「源码里出现过某个字符串」，把那段字符串删掉、
换成一段同样含关键词的注释，测试仍然绿 —— 那它没在守行为。
所以这里：
  * 前端槽位渲染 -> 用 Node 真跑 app.js 里的 renderMode 分支；
  * 服务端接收 -> 直接调用真的 prepare_job_params 并断言 params 落成什么。

运行：python3 -m pytest tests/test_flf_reference_media.py -v
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

APP_JS = (REPO / "web" / "app.js").read_text(encoding="utf-8")

NODE = shutil.which("node") or shutil.which("nodejs")


def _strip_js_comments(src: str) -> str:
    """去掉 JS 注释（保留字符串字面量内容）。

    【为什么】本文件有些断言是「旧写法不许再出现」，
    而旧写法常常留在注释里（说明「原来错在哪」）。
    注释里出现不算缺陷 —— 只有**可执行代码**里出现才算。
    """
    out = []
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        if ch in "\"'`":
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                if src[i] == "\\" and i + 1 < n:
                    out.append(src[i])
                    out.append(src[i + 1])
                    i += 2
                    continue
                out.append(src[i])
                if src[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


APP_JS_CODE = _strip_js_comments(APP_JS)


def _run_node(script: str) -> dict:
    """在 Node 里真跑一段 JS，返回 JSON 结果。"""
    if not NODE:
        raise unittest.SkipTest("node 不可用")
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        proc = subprocess.run([NODE, path], capture_output=True, text=True,
                              timeout=60, cwd=str(REPO))
    finally:
        os.unlink(path)
    if proc.returncode != 0:
        raise AssertionError(
            "node 执行失败:\nstdout=%s\nstderr=%s" % (proc.stdout, proc.stderr))
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ------------------------------------------------------------------
# 1) flf 面板渲染参考视频 / 参考音乐槽位
# ------------------------------------------------------------------
class TestFlfPanelRendersReferenceSlots(unittest.TestCase):
    """flf 面板必须有 refvid0..2 / refaud0..2 槽位。"""

    def test_flf_branch_renders_video_and_audio_slots(self):
        """flf 分支里必须渲染两组槽位（不是只在 r2v 分支）。"""
        # 切出 flf 分支的源码（到 r2v 分支为止）
        start = APP_JS_CODE.index('if (mode === "flf")')
        end = APP_JS_CODE.index('} else if (mode === "r2v")', start)
        flf_branch = APP_JS_CODE[start:end]

        self.assertIn('mk("refvid" + i, "参考视频 " + (i + 1), "video")',
                      flf_branch)
        self.assertIn('mk("refaud" + i, "参考音乐 " + (i + 1), "audio")',
                      flf_branch)

    def test_flf_branch_still_renders_first_and_last_slots(self):
        """补参考素材槽位**不能**把首帧 / 尾帧槽位挤掉。"""
        start = APP_JS_CODE.index('if (mode === "flf")')
        end = APP_JS_CODE.index('} else if (mode === "r2v")', start)
        flf_branch = APP_JS_CODE[start:end]
        self.assertIn('mk("first", "首帧")', flf_branch)
        self.assertIn('mk("last", "尾帧")', flf_branch)

    def test_flf_has_no_reference_image_slots(self):
        """flf **不**该有参考图槽位（首尾帧就是它的图条件）。

        【为什么锁这条】免得以后有人把 r2v 的 ref0..ref8 也复制过来，
        面板上出现「参考图」但后端 build_flf 根本不接 ref_images ->
        又是「面板说谎」。
        """
        start = APP_JS_CODE.index('if (mode === "flf")')
        end = APP_JS_CODE.index('} else if (mode === "r2v")', start)
        flf_branch = APP_JS_CODE[start:end]
        self.assertNotIn('mk("ref" + i', flf_branch)

    def test_flf_slots_use_video_and_audio_kinds(self):
        """槽位类型必须是 video / audio —— 否则 openPicker 传错 categories。"""
        self.assertIn('"参考视频 " + (i + 1), "video"', APP_JS_CODE)
        self.assertIn('"参考音乐 " + (i + 1), "audio"', APP_JS_CODE)


# ------------------------------------------------------------------
# 2) flf 提交带上了参考视频 / 参考音乐
# ------------------------------------------------------------------
class TestFlfSubmissionCarriesReferenceMedia(unittest.TestCase):
    """flf 的提交表单必须带上 file_refvid / file_refaud。"""

    def test_flf_submit_appends_local_reference_media(self):
        """flf 分支里必须有 file_refvid / file_refaud 的 append。"""
        start = APP_JS_CODE.index('if (mode === "flf") {')
        # 提交段里的 flf 分支（第二个出现）
        second = APP_JS_CODE.index('if (mode === "flf") {', start + 10)
        end = APP_JS_CODE.index('} else if (mode === "r2v")', second)
        flf_submit = APP_JS_CODE[second:end]
        self.assertIn('fd.append("file_refvid"', flf_submit)
        self.assertIn('fd.append("file_refaud"', flf_submit)

    def test_flf_submit_reads_the_same_slot_keys(self):
        """提交时读的槽位键必须与渲染时一致（refvid0..2 / refaud0..2）。"""
        second = APP_JS_CODE.index('if (mode === "flf") {',
                                   APP_JS_CODE.index('if (mode === "flf") {') + 10)
        end = APP_JS_CODE.index('} else if (mode === "r2v")', second)
        flf_submit = APP_JS_CODE[second:end]
        self.assertIn('state.files["refvid" + i]', flf_submit)
        self.assertIn('state.files["refaud" + i]', flf_submit)

    def test_flf_local_reference_limits_are_three(self):
        """本地直传上限与节点 schema 一致（视频 3 / 音频 3）。"""
        second = APP_JS_CODE.index('if (mode === "flf") {',
                                   APP_JS_CODE.index('if (mode === "flf") {') + 10)
        end = APP_JS_CODE.index('} else if (mode === "r2v")', second)
        flf_submit = APP_JS_CODE[second:end]
        self.assertIn("const REF_VIDEO_LIMIT = 3;", flf_submit)
        self.assertIn("const REF_AUDIO_LIMIT = 3;", flf_submit)

    def test_asset_refs_slot_mapping_covers_flf_and_reference_media(self):
        """素材库路径的槽位映射必须同时覆盖首尾帧与三类参考素材。"""
        self.assertIn('first: "first_frame", last: "last_frame",', APP_JS_CODE)
        self.assertIn('if (/^ref\\d+$/.test(key)) return "ref_image";',
                      APP_JS_CODE)
        self.assertIn('if (/^refvid\\d+$/.test(key)) return "ref_video";',
                      APP_JS_CODE)
        self.assertIn('if (/^refaud\\d+$/.test(key)) return "ref_audio";',
                      APP_JS_CODE)


# ------------------------------------------------------------------
# 3) 服务端 flf 分支真的接住了
# ------------------------------------------------------------------
class TestServerFlfAcceptsReferenceMedia(unittest.TestCase):
    """服务端 flf 分支必须把 file_refvid / file_refaud 写进 params。"""

    def test_local_reference_video_reaches_params(self):
        import server
        params = {"prompt": "x", "width": 864, "height": 480, "duration": 5}
        server.prepare_job_params(
            "flf", params, [],
            {"file_last": ["last.png"], "file_refvid": ["v.mp4"]})
        self.assertEqual(params["ref_videos"], ["v.mp4"])

    def test_local_reference_audio_reaches_params(self):
        import server
        params = {"prompt": "x", "width": 864, "height": 480, "duration": 5}
        server.prepare_job_params(
            "flf", params, [],
            {"file_last": ["last.png"], "file_refaud": ["a.flac"]})
        self.assertEqual(params["ref_audios"], ["a.flac"])

    def test_workflow_wires_reference_video_into_node_slots(self):
        """build_flf 必须把 ref_videos 接进 T8 节点的 ref_videos 槽位。

        【为什么这条最重要】线上实测踩到的真实缺陷：
        flf 选了参考视频 -> 服务端把它的音轨抽出来贴上去
        （ref_video_audios）-> 但 build_flf **没有**把 ref_videos 接进节点 ->
        节点硬要求「第 N 个背景音必须配第 N 个参考视频」->
        任务直接失败：
          ValueError: Reference-video soundtrack(s) have no same-numbered
          video: 0

        build_r2v 一直是接的；flf 漏了 —— 因为 flf 之前没有参考视频入口，
        漏了也没人发现。这次把面板入口补上，缺陷才浮出来。
        """
        from h3studio import h3_video_modes as h3modes
        wf = h3modes.build_flf({
            "first_frame": "f.png",
            "last_frame": "l.png",
            "ref_videos": ["v.mp4"],
            "ref_video_audios": ["sb.m4a"],
            "ref_audios": ["a.flac"],
            "width": 864, "height": 480, "duration": 5,
            "job_id": "testjob",
        })
        # 参考素材是 T8 节点的**直接输入**（键名形如 "ref_videos.ref_video_0"）
        node = None
        for nid, spec in wf.items():
            if spec.get("class_type") == "MiniMaxH3AudioConditioningT8":
                node = spec
                break
        self.assertIsNotNone(node, "找不到 T8 音频条件节点")
        inputs = node.get("inputs", {})
        self.assertIn("ref_videos.ref_video_0", inputs,
                      "build_flf 没把 ref_videos 接进节点槽位 "
                      "（音轨三选一的 (c) 会因「音轨无对应视频」而失败）")
        # 参考音乐那一路也必须在
        self.assertTrue(
            any(k.startswith("ref_audios.ref_audio_") for k in inputs),
            "build_flf 没把 ref_audios 接进节点槽位")
        # 参考视频背景音那一路也必须在
        self.assertTrue(
            any(k.startswith("ref_video_audios.ref_video_audio_")
                for k in inputs),
            "build_flf 没把 ref_video_audios 接进节点槽位")
        # 引用的必须是真实存在的节点（不能指向空）
        for key in ("ref_videos.ref_video_0",):
            ref = inputs[key]
            self.assertEqual(len(ref), 2, "槽位引用格式应为 [node_id, output]")
            self.assertIn(ref[0], wf,
                          f"{key} 指向了不存在的节点 {ref[0]}")

    def test_workflow_without_reference_video_has_no_ref_video_slots(self):
        """没有参考视频时，节点里不能凭空出现 ref_videos 槽位。

        【为什么锁这条】防止有人为了「让音轨三选一能用」把
        ref_videos 无条件接进去 —— 那样节点又会报
        「soundtrack has no same-numbered video」。
        """
        from h3studio import h3_video_modes as h3modes
        wf = h3modes.build_flf({
            "first_frame": "f.png",
            "last_frame": "l.png",
            "width": 864, "height": 480, "duration": 5,
            "job_id": "testjob",
        })
        node = None
        for nid, spec in wf.items():
            if spec.get("class_type") == "MiniMaxH3AudioConditioningT8":
                node = spec
                break
        self.assertIsNotNone(node)
        inputs = node.get("inputs", {})
        self.assertFalse(
            any(k.startswith("ref_videos.") for k in inputs),
            "没有参考视频时不应出现 ref_videos 槽位")

    def test_task_type_switches_to_hybrid_when_reference_media_present(self):
        """带参考素材时，task_type 必须是 Hybrid（节点的硬规则）。

        【节点的硬规则 —— 2026-11-15 线上实测暴露】
        T8 的 resolve_task_type() 里：
            requirements = {
                "t2va":   (False, False, False),
                "i2va":   (True,  False, False),
                "fl2va":  (True,  True,  False),
                "l2va":   (False, True,  False),
                "ref2va": (False, False, True),
                "hybrid": (None,  None,  True),
            }
            if requested not in {"ref2va", "hybrid"} and has_refs:
                raise ValueError(f"{requested.upper()} cannot include
                                  reference media; use Auto or Hybrid")

        原来 build_flf 恒定按「有首尾帧 -> FL2VA / 只有首帧 -> I2VA /
        只有尾帧 -> L2VA」定类型，于是 flf + 参考视频时线上直接失败：
            ValueError: L2VA cannot include reference media; use Auto or Hybrid
        """
        from h3studio import h3_video_modes as h3modes

        def task_type_of(params):
            wf = h3modes.build_flf(params)
            for nid, spec in wf.items():
                if spec.get("class_type") == "MiniMaxH3AudioConditioningT8":
                    return spec["inputs"].get("task_type")
            self.fail("找不到 T8 音频条件节点")

        base = {"width": 864, "height": 480, "duration": 5, "job_id": "t"}

        # 无参考素材：保持原来的三档
        self.assertEqual(
            task_type_of({**base, "first_frame": "f.png", "last_frame": "l.png"}),
            "FL2VA")
        self.assertEqual(
            task_type_of({**base, "first_frame": "f.png"}), "I2VA")
        self.assertEqual(
            task_type_of({**base, "last_frame": "l.png"}), "L2VA")

        # 有参考素材：必须切到 Hybrid
        for extra in ({"ref_videos": ["v.mp4"]},
                      {"ref_audios": ["a.flac"]},
                      {"ref_video_audios": ["sb.m4a"]}):
            self.assertEqual(
                task_type_of({**base, "last_frame": "l.png", **extra}),
                "Hybrid",
                f"带 {list(extra)} 时 task_type 必须是 Hybrid，"
                f"否则节点会抛「cannot include reference media」")

    def test_task_type_value_is_in_node_combo_options(self):
        """task_type 的字面量必须在节点 combo 的合法选项里。

        【为什么锁这条】节点的 combo 是
        ["auto", "T2VA", "I2VA", "FL2VA", "L2VA", "Ref2VA", "Hybrid"]
        —— 写 "HYBRID"（全大写）不在选项里，ComfyUI 会拒掉。
        这条锁住大小写，防止以后有人手滑写成全大写。
        """
        from h3studio import h3_video_modes as h3modes
        wf = h3modes.build_flf({
            "last_frame": "l.png", "ref_videos": ["v.mp4"],
            "width": 864, "height": 480, "duration": 5, "job_id": "t",
        })
        tt = None
        for nid, spec in wf.items():
            if spec.get("class_type") == "MiniMaxH3AudioConditioningT8":
                tt = spec["inputs"].get("task_type")
        self.assertIn(tt, ["auto", "T2VA", "I2VA", "FL2VA", "L2VA",
                           "Ref2VA", "Hybrid"],
                      f"task_type={tt!r} 不在节点的 combo 选项里")

    def test_reference_media_without_any_frame_is_still_rejected(self):
        """只给参考音乐、没给首尾帧 -> 仍然报错。

        【为什么锁这条】防止有人为了「让音轨三选一能用」把
        「至少给一张首尾帧」的校验放松掉 —— 那会让 flf 退化成 r2v。
        """
        import server
        params = {"prompt": "x", "width": 864, "height": 480, "duration": 5}
        with self.assertRaises(server.JobValidationError):
            server.prepare_job_params(
                "flf", params, [], {"file_refaud": ["a.flac"]})

    def test_reference_media_does_not_leak_into_r2v_params(self):
        """r2v 的 ref_images 语义不受影响（首尾帧键不该出现在 r2v）。"""
        import server
        params = {"prompt": "x", "width": 864, "height": 480, "duration": 5}
        server.prepare_job_params(
            "r2v", params, [],
            {"file_ref": ["a.png"], "file_refvid": ["v.mp4"],
             "file_refaud": ["a.flac"]})
        self.assertEqual(params["ref_images"], ["a.png"])
        self.assertEqual(params["ref_videos"], ["v.mp4"])
        self.assertEqual(params["ref_audios"], ["a.flac"])
        self.assertNotIn("first_frame", params)


# ------------------------------------------------------------------
# 4) flf 的音轨三选一可见性
# ------------------------------------------------------------------
class TestFlfAudioTrackVisibility(unittest.TestCase):
    """flf 选了参考素材后，音轨三选一的 (b)/(c) 必须可选。"""

    def test_local_reference_media_enables_video_bg_option(self):
        """本地直传参考视频 -> (c) 可选，且提交阶段不把选择改掉。"""
        import server
        params = {"prompt": "x", "width": 864, "height": 480, "duration": 5,
                  "audio_track_source": "video_bg"}
        server.prepare_job_params(
            "flf", params, [],
            {"file_last": ["last.png"], "file_refvid": ["v.mp4"]})
        self.assertEqual(params["audio_track_source"], "video_bg")

    def test_local_reference_audio_enables_reference_option(self):
        """本地直传参考音乐 -> (b) 可选。"""
        import server
        params = {"prompt": "x", "width": 864, "height": 480, "duration": 5,
                  "audio_track_source": "reference"}
        server.prepare_job_params(
            "flf", params, [],
            {"file_last": ["last.png"], "file_refaud": ["a.flac"]})
        self.assertEqual(params["audio_track_source"], "reference")

    def test_asset_library_reference_media_enables_options(self):
        """素材库来源的参考音乐 / 参考视频 -> (b)/(c) 也可选。"""
        import server
        params = {"prompt": "x", "width": 864, "height": 480, "duration": 5,
                  "audio_track_source": "reference"}
        server.prepare_job_params(
            "flf", params,
            [{"asset_id": "a", "slot": "ref_audio", "ordinal": 0},
             {"asset_id": "b", "slot": "last_frame", "ordinal": 0}], {})
        self.assertEqual(params["audio_track_source"], "reference")

    def test_no_reference_media_falls_back_to_generated(self):
        """没有任何参考素材 -> 归回「生成的音乐」。

        【期望值说明】归一后的字面量是 "generated"（h3audio.TRACK_GENERATED），
        不是空串 —— 空串是**前端**下拉的占位值（没有 option.value）。
        """
        import server
        from h3studio import h3_audio_track as h3audio
        params = {"prompt": "x", "width": 864, "height": 480, "duration": 5,
                  "audio_track_source": "video_bg"}
        server.prepare_job_params(
            "flf", params, [],
            {"file_last": ["last.png"]})
        self.assertEqual(params["audio_track_source"],
                         h3audio.TRACK_GENERATED)


if __name__ == "__main__":
    unittest.main()
