# -*- coding: utf-8 -*-
"""H3 参考生视频 / 首尾帧生视频：UI 入口与参数语义补齐的回归测试。

【本文件覆盖 2026-11-15 这一轮交付的 7 条缺口】

  缺口 1  flf / r2v 面板补齐参考视频（≤3）与参考音频（≤3）素材库入口
  缺口 2  音轨三选一：前端写入 refvid/refaud + 选择结果真正传给后端
  缺口 3  参考图本地直传上限与后端 ≤9 对齐（不得静默截断）
  缺口 4  flf 首帧校验放宽为「仅首帧 / 仅尾帧 / 首+尾」
  缺口 5  flf / r2v 分辨率与比例真实写入 params.width/height 并生效
  缺口 6  _build_r2v_legacy 死代码清理 + 回归锁定
  缺口 7  e2e 脚本 65a7d36（2 分钟短片生产脚本）在树内且可运行

【为什么有一部分用 Node 真跑 JS 而不是字符串匹配】
「字符串在文件里出现过」≠「那条断言在守行为」。
前端那些纯函数（legacyVideoSize / audioTrackAvailability）
被**抽出来**用 Node 真跑源码喂真实输入、直接断言返回值 ——
变异测试（把函数改坏）会让本文件变红，说明断言真的在守。

运行：python3 -m pytest tests/test_h3_video_modes_ui.py -v
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

from h3studio import h3_audio_track as h3audio          # noqa: E402
from h3studio import h3_video_modes as h3modes          # noqa: E402
from h3studio import h3_video_size as vsize             # noqa: E402
from h3studio import workflows as wf                    # noqa: E402

APP_JS = (REPO / "web" / "app.js").read_text(encoding="utf-8")
VIDEO_PANEL_JS = (REPO / "web" / "video-panel.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "web" / "index.html").read_text(encoding="utf-8")


def _strip_js_comments(src: str) -> str:
    """把 JS 源码里的注释去掉（保留字符串字面量里的内容）。

    【为什么需要】本文件有些断言是「旧写法不许再出现」，
    而旧写法常常**留在注释里**（说明「原来错在哪」）。
    注释里出现不算缺陷 —— 只有**可执行代码**里出现才算。

    这里的实现是**够用就好**：处理 // 行注释、/* */ 块注释，
    并跳过引号内的内容。不追求完整的 JS 词法分析。
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
                out.append(src[i])
                if src[i] == "\\" and i + 1 < n:
                    out.append(src[i + 1])
                    i += 2
                    continue
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


# ------------------------------------------------------------------
# Node 真跑前端纯函数
# ------------------------------------------------------------------
def _extract_function(src: str, name: str) -> str:
    """从 JS 源码里取出某个顶层函数的完整定义（含花括号配平）。"""
    m = re.search(r"^function\s+%s\s*\(" % re.escape(name), src, re.M)
    assert m, "app.js 里找不到函数 %s" % name
    start = m.start()
    brace = src.index("{", m.end() - 1)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError("函数 %s 的花括号不配平" % name)


def _run_js(func_name: str, body: str, extra: str = "") -> str:
    """在 Node 里真跑 app.js 的某个函数，返回 stdout。"""
    node = shutil.which("node") or shutil.which("nodejs")
    assert node is not None, "本机没有 node"
    fn_src = _extract_function(APP_JS, func_name)
    tmp = Path(tempfile.mkdtemp(prefix="h3-vmodes-ui-"))
    try:
        run_js = tmp / "run.js"
        run_js.write_text(fn_src + "\n" + extra + "\n" + body + "\n",
                          encoding="utf-8")
        proc = subprocess.run([node, str(run_js)], capture_output=True,
                              text=True, timeout=60)
        if proc.returncode != 0:
            raise AssertionError(
                "Node 脚本失败 (exit %d):\nSTDOUT:\n%s\nSTDERR:\n%s"
                % (proc.returncode, proc.stdout, proc.stderr))
        return proc.stdout.strip()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------
# 缺口 5：分辨率 / 比例语义
# ------------------------------------------------------------------
class TestResolutionSemantics(unittest.TestCase):
    """缺口 5：面板选的分辨率/比例必须真实生效。"""

    def test_server_544p_16x9_is_not_1344x768(self):
        """544p + 16:9 -> 864×480（**不是** 1344×768）。

        这是「面板说谎」的根因：旧实现缺失宽高时硬回退 1344×768，
        而 1344×768 不是 544p 的任何合法组合。
        """
        width, height = vsize.dimensions_for("544p", "16:9", mode="r2v")
        self.assertEqual((width, height), (864, 480))
        self.assertNotEqual((width, height), (1344, 768))

    def test_server_768p_9x16_is_portrait(self):
        """768p + 9:16 -> 竖屏比例（width < height）。"""
        width, height = vsize.dimensions_for("768p", "9:16", mode="flf")
        self.assertLess(width, height)
        self.assertEqual(width, 672)
        self.assertEqual(height, 1216)

    def test_server_matches_official_resolution_selector_formula(self):
        """服务端算法必须与 ComfyUI 自带 ResolutionSelector 一致。

        实测源码（comfy_extras/nodes_resolution.py）：
            total = megapixels * 1024 * 1024
            scale = sqrt(total / (w_ratio * h_ratio))
            width  = round(w_ratio * scale / multiple) * multiple
        """
        import math
        from h3studio import video_models as vmodels
        for aspect, (wr, hr) in (("16:9", (16, 9)), ("9:16", (9, 16)),
                                 ("1:1", (1, 1)), ("4:3", (4, 3))):
            for mp in (0.4, 0.8):
                total = mp * 1024 * 1024
                scale = math.sqrt(total / (wr * hr))
                want_w = max(32, int(round(wr * scale / 32)) * 32)
                want_h = max(32, int(round(hr * scale / 32)) * 32)
                got_w, got_h = vmodels.video_dimensions(aspect, mp)
                self.assertEqual((got_w, got_h), (want_w, want_h),
                                 f"{aspect} @ {mp}MP 不符合官方公式")

    def test_server_uses_shared_video_dimensions_helper(self):
        """h3_video_size 必须复用 video_models.video_dimensions（不另写一套）。

        【为什么】同一个公式写两处必然走偏 —— 这正是本项目反复踩过的坑。
        """
        from h3studio import video_models as vmodels
        for res, mp in (("544p", 0.4), ("768p", 0.8)):
            for aspect in ("16:9", "9:16", "4:3", "1:1"):
                want_w, want_h = vmodels.video_dimensions(aspect, mp)
                limit = 0.8 * 1024 * 1024
                if want_w * want_h > limit:
                    want_w, want_h, _ = vmodels.normalize_video_size(
                        want_w, want_h, max_pixels=0.8)
                got_w, got_h = vsize.dimensions_for(res, aspect, mode="r2v")
                self.assertEqual((got_w, got_h), (want_w, want_h),
                                 f"{res} + {aspect} 与共享算法不一致")

    def test_area_cap_applied_for_square_aspect(self):
        """1:1 + 0.8MP 的原始值超上限 -> 必须按比例缩回上限内。"""
        width, height = vsize.dimensions_for("768p", "1:1", mode="r2v")
        self.assertLessEqual(width * height, int(0.8 * 1024 * 1024))
        self.assertEqual(width % 32, 0)
        self.assertEqual(height % 32, 0)

    def test_apply_size_writes_real_pixels_into_params(self):
        """归一后 params 里必须有真实的 width/height。"""
        params = vsize.apply_size({"resolution": "544p", "aspect": "9:16"},
                                  "r2v")
        self.assertEqual(params["width"], 480)
        self.assertEqual(params["height"], 864)

    def test_explicit_width_height_wins_over_tier(self):
        """已经有真实宽高时以它为准（工作流真正读的就是这两个键）。"""
        params = vsize.apply_size(
            {"resolution": "544p", "aspect": "16:9",
             "width": 1024, "height": 576}, "r2v")
        self.assertEqual((params["width"], params["height"]), (1024, 576))

    def test_workflow_builder_uses_resolved_size(self):
        """build_flf / build_r2v 拿到的宽高必须来自统一归一。

        【为什么这条重要】之前 build_* 里是 `int(params.get("width") or 1344)` ——
        缺宽高就硬回退 1344×768，面板选什么都不算数。
        """
        wf_flf = wf.build_flf({"job_id": "t", "prompt": "x",
                               "resolution": "544p", "aspect": "9:16",
                               "last_frame": "l.png"})
        cond = wf_flf["136"]["inputs"]
        self.assertEqual((cond["width"], cond["height"]), (480, 864))

        wf_r2v = wf.build_r2v({"job_id": "t", "prompt": "x",
                               "resolution": "544p", "aspect": "9:16",
                               "ref_images": ["a.png"]})
        cond = wf_r2v["136"]["inputs"]
        self.assertEqual((cond["width"], cond["height"]), (480, 864))

    def test_prepare_job_params_applies_size_for_flf_and_r2v(self):
        """服务端归一层（prepare_job_params）必须给 flf/r2v 也算宽高。

        【原来错在哪】normalize_t2v_params 只对 t2v 跑，
        flf/r2v 的 params 里没有 width/height -> 工作流硬回退 1344×768。
        """
        import server
        for mode, extra in (("flf", {"asset_refs": [
            {"asset_id": "a", "slot": "last_frame", "ordinal": 0}]}),
            ("r2v", {"asset_refs": [
                {"asset_id": "a", "slot": "ref_image", "ordinal": 0}]})):
            params = {"prompt": "x", "resolution": "544p", "aspect": "9:16"}
            server.prepare_job_params(mode, params, extra.get("asset_refs"),
                                      {})
            self.assertEqual((params.get("width"), params.get("height")),
                             (480, 864),
                             f"{mode} 模式没有归一出真实宽高")

    def test_frontend_legacy_size_matches_server(self):
        """前端那份镜像必须与服务端**逐格一致**。

        【为什么】两边公式不同就会出现「界面显示一个尺寸、产物另一个尺寸」
        —— 那正是这次要修的缺陷换个方向再来一遍。
        """
        cases = [("544p", "16:9"), ("544p", "9:16"), ("768p", "16:9"),
                 ("768p", "9:16"), ("768p", "1:1"), ("544p", "4:3")]
        payload = json.dumps(cases)
        out = _run_js(
            "legacyVideoSize",
            "const CASES = %s;\n"
            "console.log(JSON.stringify(CASES.map(c => legacyVideoSize(c[0], c[1], 'r2v'))));"
            % payload)
        got = json.loads(out.splitlines()[-1])
        for (res, aspect), js_size in zip(cases, got):
            py_w, py_h = vsize.dimensions_for(res, aspect, mode="r2v")
            self.assertEqual((js_size["width"], js_size["height"]),
                             (py_w, py_h),
                             f"前端与服务端对 {res} + {aspect} 的计算不一致")

    def test_frontend_submits_width_height_for_flf_r2v(self):
        """前端提交时必须带 width/height（否则服务端只能算档位）。

        【2026-11-15 修复③ 交付项 3】三个视频模式现在共用同一套面板，
        flf / r2v 的宽高由**共享面板**给出（window.VIDEO_PANEL.submitParams()），
        提交时写进 params.width / params.height；
        共享面板还没就绪时的兜底路径仍走 legacyVideoSize()。
        """
        self.assertIn("params.width = shared.width", APP_JS)
        self.assertIn("params.height = shared.height", APP_JS)
        # 兜底路径（共享面板未就绪）仍按旧公式算真实宽高
        self.assertIn("currentLegacyVideoSize().width", APP_JS)
        self.assertIn("currentLegacyVideoSize().height", APP_JS)

    def test_panel_shows_real_output_size(self):
        """面板必须显示真实像素（用户要能核对产物对不对）。

        【2026-11-15 修复③ 交付项 3】旧面板的「分辨率档位」区块
        （含 #legacySizeNote）**已删除** —— 首尾帧 / 参考生视频现在
        与文生视频共用同一套面板，真实像素由共享面板的
        「输出尺寸」区（#vidSizeHint / #vidSizeNote）显示。
        """
        # 共享面板的输出尺寸区必须存在（三个模式共用）
        self.assertIn('id="vidSizeHint"', INDEX_HTML)
        self.assertIn('id="vidSizeNote"', INDEX_HTML)
        # 旧控件确实已经移除（不得留两套尺寸控件）
        self.assertNotIn('id="legacySizeNote"', INDEX_HTML)
        self.assertNotIn('id="resSeg"', INDEX_HTML)
        # 共享面板里显示真实像素
        self.assertIn("vidSizeHint", VIDEO_PANEL_JS)
        self.assertIn("vidSizeNote", VIDEO_PANEL_JS)


# ------------------------------------------------------------------
# 缺口 1：参考视频 / 参考音频的素材库入口
# ------------------------------------------------------------------
class TestReferenceMediaSlots(unittest.TestCase):
    """缺口 1：面板必须有参考视频 / 参考音频的入口。"""

    def test_panel_renders_refvid_and_refaud_slots(self):
        """r2v 面板必须渲染 refvid0..2 / refaud0..2 槽位。"""
        self.assertIn('mk("refvid" + i, "参考视频 " + (i + 1), "video")',
                      APP_JS)
        self.assertIn('mk("refaud" + i, "参考音乐 " + (i + 1), "audio")',
                      APP_JS)

    def test_picker_categories_follow_slot_kind(self):
        """openPicker 的 categories 必须**按槽位类型**给，而不是写死 image。

        【原来错在哪】全仓只有 categories: ["image"] / ["audio"]，
        r2v 模式下没有任何 categories: ["video"] 的入口。
        """
        self.assertIn("categories: [kind], multiple: false, limit: 1,",
                      APP_JS)
        # 写死的 image 类别必须已经不存在于槽位选择器里
        picker_block = APP_JS[APP_JS.index("openPicker({"):
                              APP_JS.index("openPicker({") + 400]
        self.assertNotIn('categories: ["image"]', picker_block)

    def test_slot_accept_attribute_is_type_scoped(self):
        """文件选择框的 accept 必须按类型收窄。"""
        self.assertIn('inp.accept = kind === "video" ? "video/*"', APP_JS)
        self.assertIn(': kind === "audio" ? "audio/*" : "image/*";', APP_JS)

    def test_backend_accepts_local_reference_video_and_audio(self):
        """服务端必须有本地直传的接收与上传路径。"""
        import server
        src = (REPO / "server.py").read_text(encoding="utf-8")
        self.assertIn("file_refvid", src)
        self.assertIn("file_refaud", src)
        self.assertIn("save_uploaded_media", src)
        self.assertTrue(hasattr(server, "save_uploaded_media"))

    def test_reference_limits_match_node_schema(self):
        """上限必须与 T8 节点 schema 一致（图/9，视频/3，音频/3）。"""
        self.assertEqual(h3modes.MAX_REF_IMAGES, 9)
        self.assertEqual(h3modes.MAX_REF_VIDEOS, 3)
        self.assertEqual(h3modes.MAX_REF_AUDIOS, 3)

    def test_ref_video_node_gives_all_required_inputs(self):
        """VHS_LoadVideo 的 7 个 required 输入必须**全部**显式给。

        【这是实测踩到的坑】
        VHS_LoadVideo 的 schema required 有 7 项（/object_info 实测）：
            video / force_rate / custom_width / custom_height /
            frame_load_cap / skip_first_frames / select_every_nth
        ComfyUI 通过 API 提交工作流时**不套用** schema 默认值 ——
        漏任何一个 -> 提交被拒（"Required input is missing"）。

        实测：只给 video + format 时 ComfyUI 返回
            node_errors: {"1": {"errors": [
                {"type": "required_input_missing", "details": "custom_width"},
                ... 共 6 项 ]}}
        这条路径之前**从来没被真实跑过**（线上 24 条 r2v 全是图+音乐）。
        """
        wf_ = wf.build_r2v({"job_id": "t", "prompt": "x",
                            "ref_images": ["a.png"],
                            "ref_videos": ["v.mp4"]})
        node = wf_["136:301"]
        self.assertEqual(node["class_type"], "VHS_LoadVideo")
        required = ("video", "force_rate", "custom_width", "custom_height",
                    "frame_load_cap", "skip_first_frames", "select_every_nth")
        for key in required:
            self.assertIn(key, node["inputs"],
                          f"VHS_LoadVideo 缺 required 输入：{key}")

    def test_ref_audio_node_gives_required_inputs(self):
        """VHS_LoadAudioUpload 的 required 是 audio（实测）。"""
        wf_ = wf.build_r2v({"job_id": "t", "prompt": "x",
                            "ref_images": ["a.png"],
                            "ref_audios": ["m.flac"]})
        node = wf_["136:401"]
        self.assertEqual(node["class_type"], "VHS_LoadAudioUpload")
        self.assertIn("audio", node["inputs"])


# ------------------------------------------------------------------
# 缺口 2：音轨三选一
# ------------------------------------------------------------------
class TestAudioTrackChoiceFrontend(unittest.TestCase):
    """缺口 2：前端必须能真正选出三选一，并把它传给后端。"""

    def _availability(self, slots, files=None):
        """在 Node 里真跑 audioTrackAvailability，喂真实形状的 state。"""
        payload = json.dumps({"slots": slots, "files": files or {}},
                             ensure_ascii=False)
        out = _run_js(
            "audioTrackAvailability",
            "const S = %s;\n"
            "const state = {assetRefs: S.slots, files: S.files};\n"
            "console.log(JSON.stringify(audioTrackAvailability()));" % payload)
        return json.loads(out.splitlines()[-1])

    def test_no_reference_hides_both_options(self):
        """没有任何参考素材 -> (b)/(c) 都不出现。"""
        got = self._availability({})
        self.assertFalse(got["hasRefAudio"])
        self.assertFalse(got["hasVideoAudio"])

    def test_reference_audio_slot_enables_option_b(self):
        """refaud* 槽位（category=audio）-> (b) 出现。"""
        got = self._availability({"refaud0": {
            "asset_id": "a", "category": "audio"}})
        self.assertTrue(got["hasRefAudio"])
        self.assertFalse(got["hasVideoAudio"])

    def test_reference_video_slot_enables_option_c(self):
        """refvid* 槽位（category=**video**）-> (c) 出现。

        【这是原来的缺陷】旧判据是 `cat === "audio" && key.startsWith("refvid")`
        —— refvid 装的是视频，category 是 video，那个条件永远不成立，
        于是 (c) 这一项从来就没出现过。
        """
        got = self._availability({"refvid0": {
            "asset_id": "a", "category": "video"}})
        self.assertTrue(got["hasVideoAudio"])
        self.assertFalse(got["hasRefAudio"])

    def test_local_uploads_also_enable_options(self):
        """本地直传的参考音频 / 参考视频也算「有素材」。"""
        got = self._availability({}, files={"refaud0": "x", "refvid0": "y"})
        self.assertTrue(got["hasRefAudio"])
        self.assertTrue(got["hasVideoAudio"])

    def test_selected_source_is_submitted_to_backend(self):
        """选择结果必须真的提交给后端（params.audio_track_source）。"""
        self.assertIn("params.audio_track_source = state.audioTrackSource",
                      APP_JS)

    def test_backend_keeps_video_bg_when_reference_video_present(self):
        """服务端提交阶段不得把 (c) 改掉（有参考视频时）。"""
        import server
        params = {"prompt": "x", "audio_track_source": "video_bg",
                  "width": 864, "height": 480, "duration": 5}
        server.prepare_job_params(
            "r2v", params,
            [{"asset_id": "a", "slot": "ref_video", "ordinal": 0}], {})
        self.assertEqual(params["audio_track_source"], "video_bg")

    def test_backend_keeps_reference_when_reference_audio_present(self):
        """有参考音乐时 (b) 不得被改掉。"""
        import server
        params = {"prompt": "x", "audio_track_source": "reference",
                  "width": 864, "height": 480, "duration": 5}
        server.prepare_job_params(
            "r2v", params,
            [{"asset_id": "a", "slot": "ref_audio", "ordinal": 0}], {})
        self.assertEqual(params["audio_track_source"], "reference")

    def test_backend_falls_back_when_nothing_available(self):
        """既没有参考音乐也没有参考视频 -> (b)/(c) 归回生成的音乐。"""
        import server
        params = {"prompt": "x", "audio_track_source": "video_bg",
                  "width": 864, "height": 480, "duration": 5}
        server.prepare_job_params(
            "r2v", params,
            [{"asset_id": "a", "slot": "ref_image", "ordinal": 0}], {})
        self.assertEqual(params["audio_track_source"], "generated")

    def test_video_soundtrack_extraction_helper_exists(self):
        """(c) 需要真的从参考视频里抽音轨 -> 必须有那个实现。"""
        import server
        self.assertTrue(hasattr(server, "_extract_video_soundtrack"))

    def test_video_bg_track_uses_video_soundtrack_slot(self):
        """(c) 在工作流里接的必须是 ref_video_audios 那一路。"""
        wf_ = wf.build_r2v({"job_id": "t", "prompt": "x",
                            "ref_images": ["a.png"],
                            "ref_video_audios": ["vb.m4a"],
                            "audio_track_source": "video_bg"})
        audio_ref = wf_["91"]["inputs"]["audio"]
        node = wf_[audio_ref[0]]
        self.assertEqual(node["class_type"], "TrimAudioDuration")
        self.assertEqual(node["inputs"]["audio"], ["136:501", 0])


# ------------------------------------------------------------------
# 缺口 3：参考图本地直传上限
# ------------------------------------------------------------------
class TestReferenceImageLimit(unittest.TestCase):
    """缺口 3：本地直传上限必须与后端 ≤9 对齐，且不得静默截断。"""

    def test_frontend_submits_all_nine_slots(self):
        """前端必须提交 ref0..ref8 全部九张（原来只提交 3 张）。"""
        self.assertIn('for (let i = 0; i < REF_IMAGE_LIMIT; i++)',
                      APP_JS_CODE)
        self.assertIn('const f = state.files["ref" + i];', APP_JS_CODE)
        # 旧的「只取 3 张」写法必须已经消失（只看**代码**，不看注释）
        self.assertNotIn(
            "const refs = [state.files.ref0, state.files.ref1, state.files.ref2]",
            APP_JS_CODE)

    def test_frontend_limit_is_nine(self):
        self.assertIn("const REF_IMAGE_LIMIT = 9;", APP_JS)

    def test_backend_rejects_more_than_nine_instead_of_truncating(self):
        """超过 9 张必须**报错**，不能悄悄截断。"""
        import server
        params = {"prompt": "x", "width": 864, "height": 480, "duration": 5}
        with self.assertRaises(server.JobValidationError):
            server.prepare_job_params(
                "r2v", params, [],
                {"file_ref": [f"r{i}.png" for i in range(10)]})

    def test_backend_accepts_exactly_nine(self):
        """正好 9 张必须放行（节点上限就是 9）。"""
        import server
        params = {"prompt": "x", "width": 864, "height": 480, "duration": 5}
        server.prepare_job_params(
            "r2v", params, [],
            {"file_ref": [f"r{i}.png" for i in range(9)]})
        self.assertEqual(len(params["ref_images"]), 9)


# ------------------------------------------------------------------
# 缺口 4：flf 首帧校验放宽
# ------------------------------------------------------------------
class TestFlfFrameValidation(unittest.TestCase):
    """缺口 4：仅首帧 / 仅尾帧 / 首+尾 三种都能提交。"""

    def test_backend_accepts_last_frame_only(self):
        """只给尾帧 -> L2VA（尾帧生视频）。

        【注意】素材库来源的尾帧在**提交阶段**不会写进
        params["last_frame"] —— 那是 worker 里 _apply_asset_refs
        上传之后才落的。提交阶段只校验「槽位在不在」。
        """
        import server
        params = {"prompt": "x", "width": 864, "height": 480, "duration": 5}
        server.prepare_job_params(
            "flf", params,
            [{"asset_id": "a", "slot": "last_frame", "ordinal": 0}], {})
        # 不抛异常 = 放行；且不能凭空塞一个 first_frame
        self.assertNotIn("first_frame", params)

    def test_backend_accepts_first_frame_only(self):
        """只给首帧 -> I2VA。"""
        import server
        params = {"prompt": "x", "width": 864, "height": 480, "duration": 5}
        server.prepare_job_params(
            "flf", params,
            [{"asset_id": "a", "slot": "first_frame", "ordinal": 0}], {})
        self.assertNotIn("last_frame", params)

    def test_backend_accepts_local_last_frame_only(self):
        """本地直传的尾帧（file_last）必须放行。"""
        import server
        params = {"prompt": "x", "width": 864, "height": 480, "duration": 5}
        server.prepare_job_params(
            "flf", params, [], {"file_last": ["last.png"]})
        self.assertEqual(params["last_frame"], "last.png")
        self.assertNotIn("first_frame", params)

    def test_backend_rejects_no_frame_at_all(self):
        """一张都不给必须报错。"""
        import server
        params = {"prompt": "x", "width": 864, "height": 480, "duration": 5}
        with self.assertRaises(server.JobValidationError):
            server.prepare_job_params("flf", params, [], {})

    def test_frontend_no_longer_requires_first_frame(self):
        """前端不得再硬性要求首帧。

        【原来错在哪】`if (!hasFirst) { alert("首尾帧模式需要上传首帧图片"); return; }`
        —— 后端早就放宽成「给一张即可」，前端却把这条路堵死了。
        """
        self.assertNotIn('alert("首尾帧模式需要上传首帧图片")', APP_JS_CODE)
        self.assertIn("if (!hasFirst && !hasLast)", APP_JS_CODE)

    def test_task_types_map_to_frame_combinations(self):
        """首/尾/首+尾 -> I2VA / L2VA / FL2VA。"""
        base = {"job_id": "t", "prompt": "x", "width": 864, "height": 480,
                "duration": 5}
        self.assertEqual(
            wf.build_flf(dict(base, first_frame="f.png"))["136"]["inputs"]["task_type"],
            "I2VA")
        self.assertEqual(
            wf.build_flf(dict(base, last_frame="l.png"))["136"]["inputs"]["task_type"],
            "L2VA")
        self.assertEqual(
            wf.build_flf(dict(base, first_frame="f.png", last_frame="l.png"))
            ["136"]["inputs"]["task_type"], "FL2VA")


# ------------------------------------------------------------------
# 缺口 6：死代码清理
# ------------------------------------------------------------------
class TestDeadCodeRemoved(unittest.TestCase):
    """缺口 6：_build_r2v_legacy 必须被清掉并锁死。"""

    def test_legacy_builder_is_gone(self):
        """模块里不得再定义 _build_r2v_legacy。"""
        self.assertFalse(hasattr(wf, "_build_r2v_legacy"),
                         "_build_r2v_legacy 死代码还在（且带误导性注释）")

    def test_no_misleading_fallback_comment(self):
        """不得留有「回落旧路径」这类误导性说明。"""
        src = (REPO / "h3studio" / "workflows.py").read_text(encoding="utf-8")
        self.assertNotIn("回落到旧路径", src)
        self.assertNotIn("此时回落到旧路径", src)

    def test_build_r2v_goes_through_t8_path(self):
        """build_r2v 必须走 T8 条件节点（唯一真源）。"""
        wf_ = wf.build_r2v({"job_id": "t", "prompt": "x",
                            "ref_images": ["a.png"]})
        self.assertEqual(wf_["136"]["class_type"], h3modes.T8_CONDITIONING)


# ------------------------------------------------------------------
# 缺口 7：e2e 脚本在树内且可运行
# ------------------------------------------------------------------
class TestShortfilmScriptsInTree(unittest.TestCase):
    """缺口 7：65a7d36 的两个脚本必须在 local/maintainer 树内且可运行。"""

    def test_scripts_exist(self):
        for name in ("produce_h3_shortfilm.py", "finish_h3_shortfilm.py"):
            path = REPO / "scripts" / name
            self.assertTrue(path.is_file(), f"缺脚本 {name}")

    def test_scripts_are_syntactically_valid(self):
        """可运行 = 至少能被 python 解析。"""
        for name in ("produce_h3_shortfilm.py", "finish_h3_shortfilm.py"):
            path = REPO / "scripts" / name
            proc = subprocess.run(
                [sys.executable, "-m", "py_compile", str(path)],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0,
                             f"{name} 语法错误：{proc.stderr[-400:]}")

    def test_scripts_expose_help(self):
        """能被当命令行工具调用（--help 正常返回）。"""
        for name in ("produce_h3_shortfilm.py", "finish_h3_shortfilm.py"):
            path = REPO / "scripts" / name
            proc = subprocess.run([sys.executable, str(path), "--help"],
                                  capture_output=True, text=True, timeout=90)
            self.assertEqual(proc.returncode, 0,
                             f"{name} --help 失败：{proc.stderr[-400:]}")
            self.assertIn("--", proc.stdout)


if __name__ == "__main__":
    unittest.main()
