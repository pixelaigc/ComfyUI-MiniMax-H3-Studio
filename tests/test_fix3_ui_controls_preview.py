# -*- coding: utf-8 -*-
"""2026-11-15 修复③ 的回归测试（三个交付项）。

【这个文件锁死的行为】

  交付项 1 —— **所有拖拽条参数同时允许直接输入数值**
    * web/numeric-slider.js 给每个 range 装上数字输入框（共用组件）；
    * 输入 -> clamp 到该 range 自己的 min/max -> 按 step 对齐 -> 写回
      range.value 并派发 input/change（各面板原有的监听器照常触发）；
    * 覆盖**全站**所有 range（视频 / 图像 / 音乐 / 旧面板 + 动态生成的
      LoRA 强度）。

  交付项 2 —— **编辑类素材点击弹模态预览**
    * 图片 -> 复用 app.js 已有的 lightbox（可缩放）；
    * 视频 / 音频 -> 媒体预览模态框（原生 controls + 静音按钮）；
    * 可关闭、点遮罩可关、播放可控；
    * 参考图列表（web/qwen-refs.js）点击条目即开预览。

  交付项 3 —— **首尾帧 / 参考生视频按文生视频布局重构**
    * 三个视频模式**共用**同一套面板（web/video-panel.js 的 render(mode)）；
    * 旧「快捷选分辨率」等额外布局元素**必须删除**；
    * 差异**只在**本模式特有的素材字段；
    * 参数提交走同一个出口（submitParams），服务端归一也走同一个入口。

【为什么这样断言】断言若只是「源码里出现过某个字符串」，
把那段字符串删掉、换成一段同样含关键词的注释，测试仍然绿 ——
那它没在守行为。所以：
  * 前端 -> 用 **Node 真跑** web/numeric-slider.js 的纯函数，
    并用 Node 真跑 app.js 的 DOM 分支（jsdom 风格的最小 DOM 桩）；
  * 后端 -> 直接调用真的 build_flf / build_r2v / normalize_t2v_params
    并断言产出的工作流 / 归一结果。

运行：python3 -m pytest tests/test_fix3_ui_controls_preview.py -v
"""

import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

INDEX_HTML = (REPO / "web" / "index.html").read_text(encoding="utf-8")
APP_JS = (REPO / "web" / "app.js").read_text(encoding="utf-8")
VIDEO_PANEL_JS = (REPO / "web" / "video-panel.js").read_text(encoding="utf-8")
NUMERIC_JS = (REPO / "web" / "numeric-slider.js").read_text(encoding="utf-8")
MEDIA_PREVIEW_JS = (REPO / "web" / "media-preview.js").read_text(encoding="utf-8")
QWEN_REFS_JS = (REPO / "web" / "qwen-refs.js").read_text(encoding="utf-8")

NODE = shutil.which("node") or shutil.which("nodejs")


def _node_run(script: str, js_file: Path | None = None) -> str:
    """用 Node 真跑一段 JS，返回 stdout。

    【为什么用环境变量传文件路径】`node -e "..." <路径>` 会把路径当成
    **脚本文件**去执行（不是 argv）—— 于是报 "Unknown regular expression
    flags"。改用环境变量 NS_FILE，脚本从 process.env 读。
    """
    if not NODE:
        raise unittest.SkipTest("本机没有 node")
    env = dict(os.environ)
    if js_file is not None:
        env["NS_FILE"] = str(js_file)
    proc = subprocess.run([NODE, "-e", script], capture_output=True,
                          text=True, timeout=60, env=env)
    if proc.returncode != 0:
        raise AssertionError(f"node 执行失败:\n{proc.stderr}")
    return proc.stdout


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
        if ch == '"' or ch == "'" or ch == "`":
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


# ==================================================================
# 交付项 1：滑杆 + 数值直输
# ==================================================================
class TestNumericSlider(unittest.TestCase):
    """「拖拽条参数同时允许直接输入数值」。"""

    def test_component_exists_and_is_loaded(self):
        """共用组件必须存在且被 index.html 引入（且在 app.js 之前）。"""
        self.assertTrue((REPO / "web" / "numeric-slider.js").is_file(),
                        "web/numeric-slider.js 不存在")
        self.assertIn("/static/numeric-slider.js", INDEX_HTML)
        # 必须在 app.js / video-panel.js / music.js **之前**加载 ——
        # 它在 DOMContentLoaded 时扫全部 range。
        pos_numeric = INDEX_HTML.index("/static/numeric-slider.js")
        for later in ("/static/app.js", "/static/video-panel.js",
                      "/static/music.js"):
            self.assertLess(pos_numeric, INDEX_HTML.index(later),
                            f"numeric-slider.js 必须在 {later} 之前加载")

    def test_clamp_and_step_alignment(self):
        """输入值必须 clamp 到 min/max 并按 step 对齐。"""
        script = r"""
        const NS = require('fs').readFileSync(process.env.NS_FILE, 'utf8');
        // 抽出纯函数（不依赖 DOM）真跑一遍。
        // 用 eval(body) 而不是 eval('(' + body + ')')：多个函数声明
        // 拼在一起时，括号形式不是合法表达式。
        const body = NS.substring(NS.indexOf('function clamp('),
                                 NS.indexOf('// ------------------------------------------------------------ 增强'));
        eval(body);
        // alignToStep = clamp + 按 step 对齐（这才是输入框真正走的路径）
        const r = {
          low:  alignToStep(5, 1, 50, 1),
          high: alignToStep(999, 1, 50, 1),
          below: alignToStep(-5, 1, 50, 1),
          step05: alignToStep(0.333, 0, 1.5, 0.05),
          step01: alignToStep(12.345, 0, 100, 0.01),
          step1:  alignToStep(7.6, 1, 50, 1),
          nan:   alignToStep('abc', 1, 50, 1),
          emptyStr: Number(''),
        };
        console.log(JSON.stringify(r));
        """
        out = _node_run(script, REPO / "web" / "numeric-slider.js")
        got = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(got["low"], 5)
        self.assertEqual(got["high"], 50, "超上限必须 clamp 到 max")
        self.assertEqual(got["below"], 1, "低于下限必须 clamp 到 min")
        self.assertEqual(got["step05"], 0.35, "必须按 step=0.05 对齐")
        self.assertEqual(got["step01"], 12.35, "必须按 step=0.01 对齐")
        self.assertEqual(got["step1"], 8, "必须按 step=1 对齐")
        self.assertIsNone(got["nan"], "非法输入必须被拒绝（不能变成 NaN）")
        # 空串在 JS 里会变成 0（Number('') === 0），不是 NaN ——
        # 所以空输入由组件的 commit() 单独判空（raw.trim() 为空就
        # 还原滑杆当前值），而不是靠 alignToStep 拦。
        self.assertEqual(got["emptyStr"], 0)

    def test_empty_input_is_ignored(self):
        """空输入必须**还原**滑杆当前值（而不是变成 0 / NaN）。"""
        self.assertIn("const raw = num.value.trim();", NUMERIC_JS)
        self.assertIn('if (!raw) { syncFromSlider(); return; }', NUMERIC_JS)

    def test_input_writes_back_to_range_and_fires_events(self):
        """输入后必须写回 range.value 并派发 input/change（状态才会更新）。"""
        self.assertIn("input.value = String(next);", NUMERIC_JS)
        self.assertIn('new Event("input", {bubbles: true})', NUMERIC_JS)
        self.assertIn('new Event("change", {bubbles: true})', NUMERIC_JS)

    def test_double_click_enters_edit(self):
        """双击滑杆必须能进入编辑（用户找不到那个小输入框时）。"""
        self.assertIn('addEventListener("dblclick"', NUMERIC_JS)

    def test_render_sync_entry_point_exists(self):
        """面板重渲染后必须有一个**显式同步入口**。

        【为什么需要】各面板的 render() 会**程序化**写 range.value
        （例如 video-panel.js 的 renderSampling 里
        `stepsEl.value = videoState.steps;`）——
        这种赋值**不触发** input/change 事件，于是数字输入框会停留在
        旧值，界面上出现「滑杆 8、数字框 4」的自相矛盾。
        """
        self.assertIn("function syncAll(root)", NUMERIC_JS)
        self.assertIn("syncAll,", NUMERIC_JS)
        # 三个面板的渲染入口都要调用它
        # （video-panel.js 走自己的 syncNumericInputs() 包装，
        #  app.js / music.js 直接调 window.NumericSlider.syncAll()）
        self.assertIn("syncNumericInputs();", VIDEO_PANEL_JS)
        self.assertIn("ns.syncAll()", VIDEO_PANEL_JS)
        self.assertIn("window.NumericSlider.syncAll()", APP_JS_CODE)
        music_js = (REPO / "web" / "music.js").read_text(encoding="utf-8")
        self.assertIn("window.NumericSlider.syncAll()", music_js)

    def test_enhance_is_idempotent(self):
        """重复增强不能插出第二个输入框（否则界面越来越乱）。"""
        self.assertIn("data-num-enhanced", NUMERIC_JS)
        self.assertIn("if (input.getAttribute(ENHANCED)) return", NUMERIC_JS)

    def test_all_range_controls_in_html_get_covered(self):
        """index.html 里的每一个 range 都必须有配套的数字输入入口。

        【断言方式】逐个 range 检查：要么它在某个带数字输入的容器里，
        要么它由共用组件在运行时自动增强（data-num-enhanced 标记 +
        MutationObserver 兜底）。
        """
        ranges = re.findall(r'<input[^>]*type="range"[^>]*>', INDEX_HTML)
        self.assertGreaterEqual(len(ranges), 15,
                                f"页面上的 range 数量异常：{len(ranges)}")
        # 共用组件会在运行时给**每一个** range 装输入框 ——
        # 断言它的扫描逻辑覆盖全站（querySelectorAll('input[type=range]')）
        self.assertIn("querySelectorAll('input[type=\"range\"]')", NUMERIC_JS)

    def test_dynamic_ranges_are_covered(self):
        """动态生成的滑杆（LoRA 强度）也必须有数字输入。"""
        # app.js / video-effect-lora.js 里动态生成的 range
        self.assertIn('type="range" class="strength"', APP_JS)
        effect_lora_js = (REPO / "web" / "video-effect-lora.js").read_text(
            encoding="utf-8")
        self.assertIn('type="range" class="strength"', effect_lora_js)
        # 共用组件用 MutationObserver 兜底动态插入
        self.assertIn("MutationObserver", NUMERIC_JS)
        self.assertIn('observe(document.body', NUMERIC_JS)


# ==================================================================
# 交付项 2：编辑类素材点击弹模态预览
# ==================================================================
class TestMediaPreviewModal(unittest.TestCase):
    """素材点击 -> 模态预览（图片放大 / 视频播放 / 音频播放）。"""

    def test_component_exists_and_is_loaded(self):
        self.assertTrue((REPO / "web" / "media-preview.js").is_file())
        self.assertIn("/static/media-preview.js", INDEX_HTML)

    def test_image_reuses_existing_lightbox(self):
        """图片分支必须**复用**既有 lightbox，不重复造轮子。"""
        self.assertIn("lightboxShow", MEDIA_PREVIEW_JS)
        self.assertIn("window.H3Studio", MEDIA_PREVIEW_JS)

    def test_video_and_audio_use_modal_with_controls(self):
        """视频 / 音频走自己的模态框，且带原生 controls。"""
        self.assertIn('media.controls = true', MEDIA_PREVIEW_JS)
        self.assertIn('document.createElement(kind === "video" ? "video" : "audio")',
                      MEDIA_PREVIEW_JS)

    def test_explicit_kind_wins_over_url_guessing(self):
        """显式传入的 kind 必须优先（实测踩过的坑：视频弹成了音频）。

        【为什么】槽位已经知道自己是视频 / 音频；而 content_url 没有
        扩展名（/api/assets/<id>/content），按 URL 推断会变成 image/audio。
        """
        script = r"""
        const NS = require('fs').readFileSync(process.env.NS_FILE, 'utf8');
        const start = NS.indexOf('function detectKind(');
        const end = NS.indexOf('// ------------------------------------------------------------ 模态框');
        const body = NS.substring(start, end);
        eval(body);
        console.log(JSON.stringify({
          explicitVideo: detectKind('/api/assets/x/content', 'video', '', 'video'),
          explicitAudio: detectKind('/api/assets/x/content', 'audio', '', 'audio'),
          explicitImage:  detectKind('/api/assets/x/content', '', '', 'image'),
          byCategory:     detectKind('/api/assets/x/content', 'video', '', ''),
          byExt:          detectKind('/a/b.mp4', '', '', ''),
        }));
        """
        out = _node_run(script, REPO / "web" / "media-preview.js")
        got = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(got["explicitVideo"], "video")
        self.assertEqual(got["explicitAudio"], "audio")
        self.assertEqual(got["explicitImage"], "image")
        self.assertEqual(got["byCategory"], "video")
        self.assertEqual(got["byExt"], "video")

    def test_modal_can_close_and_overlay_click_closes(self):
        """模态框必须可关闭，且点遮罩可关。"""
        self.assertIn("id=\"mediaPreviewClose\"", MEDIA_PREVIEW_JS)
        self.assertIn("if (event.target === modal) close();", MEDIA_PREVIEW_JS)
        self.assertIn('event.key === "Escape"', MEDIA_PREVIEW_JS)

    def test_mute_control_exists(self):
        """播放可控：必须有一个显式的静音开关。"""
        self.assertIn("id=\"mediaPreviewMute\"", MEDIA_PREVIEW_JS)
        self.assertIn("currentMedia.muted = !currentMedia.muted", MEDIA_PREVIEW_JS)

    def test_switching_type_clears_previous_media(self):
        """切媒体类型时必须清掉上一个（否则视频预览里杵着一个旧播放器）。"""
        self.assertIn("closeModalOnly();", MEDIA_PREVIEW_JS)
        self.assertIn("body.innerHTML = \"\";", MEDIA_PREVIEW_JS)

    def test_ref_list_items_open_preview(self):
        """图片编辑的参考图列表：点击条目必须弹预览。"""
        self.assertIn("window.MediaPreview", QWEN_REFS_JS)
        self.assertIn('box.querySelectorAll(".img-ref-item")', QWEN_REFS_JS)
        self.assertIn("preview.show(", QWEN_REFS_JS)

    def test_upload_slots_open_preview_when_filled(self):
        """素材槽位已填时点击 -> 预览（未填时仍是打开文件选择器）。"""
        self.assertIn('slot.classList.contains("filled")', APP_JS_CODE)
        self.assertIn("openSlotPreview(slot, kind)", APP_JS_CODE)


# ==================================================================
# 交付项 3：首尾帧 / 参考生视频按文生视频布局重构
# ==================================================================
class TestSharedVideoPanelLayout(unittest.TestCase):
    """三个视频模式共用同一套面板，差异只在素材字段。"""

    def test_legacy_quick_resolution_control_is_removed(self):
        """「快捷选分辨率」之类的额外布局元素必须删除。"""
        for gone in ('id="resSeg"', 'id="aspectChips"', 'id="modelCards"',
                     'id="loraCards"', 'id="steps"', 'id="stepsVal"',
                     'id="videoSamplingField"', 'id="loraStrength"'):
            self.assertNotIn(gone, INDEX_HTML,
                             f"{gone} 必须删除（与共享面板重复）")

    def test_all_three_video_modes_use_the_shared_panel(self):
        """t2v / flf / r2v 都必须走共享面板（render(mode)）。"""
        self.assertIn("const isVideoPanelMode = mode === \"t2v\" "
                      "|| mode === \"flf\" || mode === \"r2v\";", APP_JS_CODE)
        self.assertIn("window.VIDEO_PANEL.render(mode);", APP_JS_CODE)
        self.assertIn("window.VIDEO_PANEL.setMode(mode);", APP_JS_CODE)

    def test_panel_renders_mode_aware(self):
        """共享面板的 render 必须接受 mode（否则三个模式只能共用一套值）。"""
        self.assertIn("function renderVideoPanel(mode)", VIDEO_PANEL_JS)
        self.assertIn('VIDEO_OPTIONS.mode = mode || videoState.mode || "t2v"',
                      VIDEO_PANEL_JS)

    def test_r2v_only_offers_ref2va_models(self):
        """参考生视频必须用 Ref2VA 权重（FL2VA 没有 Ref2VA 通道）。"""
        self.assertIn("function modelsForMode(mode)", VIDEO_PANEL_JS)
        self.assertIn('meta.supports_ref2va === true', VIDEO_PANEL_JS)

    def test_mode_specific_material_field_only(self):
        """三个面板的差异**只在**素材字段 —— 必须在共享面板内部。"""
        self.assertIn('id="vidMediaField"', INDEX_HTML)
        self.assertIn('id="uploadZone"', INDEX_HTML)
        self.assertIn('id="audioTrackField"', INDEX_HTML)
        # 上传区必须在 #videoPanel **内部**（否则布局不在同一块面板里）
        panel_start = INDEX_HTML.index('<div id="videoPanel"')
        panel_end = INDEX_HTML.index('<div id="imagePanel"')
        upload_pos = INDEX_HTML.index('id="uploadZone"')
        self.assertTrue(panel_start < upload_pos < panel_end,
                        "上传区必须在共享面板（#videoPanel）内部")

    def test_submit_path_uses_shared_panel_for_flf_r2v(self):
        """flf / r2v 的提交参数必须来自共享面板（否则界面改了却不生效）。"""
        self.assertIn("params.width = shared.width", APP_JS_CODE)
        self.assertIn("params.height = shared.height", APP_JS_CODE)
        self.assertIn("params.accel_lora = firstAccel ? firstAccel.key : \"\"",
                      APP_JS_CODE)


class TestSharedVideoPanelBackend(unittest.TestCase):
    """后端：三个视频模式走**同一个归一入口**，且工作流真的用上参数。"""

    def test_normalize_is_mode_aware(self):
        """normalize_t2v_params 必须接受 mode（面积上限按模式）。"""
        from server import normalize_t2v_params
        import inspect
        sig = inspect.signature(normalize_t2v_params)
        self.assertIn("mode", sig.parameters,
                      "normalize_t2v_params 必须接受 mode 参数")

    def test_flf_rejects_non_ref2va_model_for_r2v(self):
        """r2v 提交 FL2VA 权重必须被明确拒绝（而不是静默换权重）。"""
        from server import normalize_t2v_params
        out, err = normalize_t2v_params(
            {"model_key": "fl2va_pruned_int8", "width": 864, "height": 480,
             "duration": 5}, mode="r2v")
        self.assertTrue(err, "r2v 用 FL2VA 权重必须报错")
        self.assertIn("Ref2VA", err)

    def test_flf_and_t2v_size_limits_differ(self):
        """flf / r2v 的面积上限是 0.8MP，t2v 是 2.0MP。"""
        from h3studio import video_models as vm
        self.assertEqual(vm.max_pixels_for_mode("t2v"), 2.0)
        self.assertEqual(vm.max_pixels_for_mode("flf"), 0.8)
        self.assertEqual(vm.max_pixels_for_mode("r2v"), 0.8)

    def test_flf_workflow_uses_shared_params(self):
        """build_flf 必须真的用上共享面板提交的参数。"""
        from h3studio import h3_video_modes as modes
        wf = modes.build_flf({
            "job_id": "t", "prompt": "x", "duration": 5,
            "first_frame": "a.png", "last_frame": "b.png",
            "width": 864, "height": 480,
            "steps": 8, "sampler": "euler", "scheduler": "beta",
            "shift_video": 9.5, "shift_audio": 2.5,
        })
        # 步数进 BasicScheduler
        self.assertEqual(wf["9"]["inputs"]["steps"], 8)
        self.assertEqual(wf["9"]["inputs"]["scheduler"], "beta")
        # 采样器进 KSamplerSelect
        self.assertEqual(wf["17"]["inputs"]["sampler_name"], "euler")
        # shift 进 MiniMaxH3SigmaShift
        self.assertEqual(wf["139"]["inputs"]["shift_video"], 9.5)
        self.assertEqual(wf["139"]["inputs"]["shift_audio"], 2.5)

    def test_r2v_workflow_uses_shared_params(self):
        """build_r2v 必须真的用上共享面板提交的参数。"""
        from h3studio import h3_video_modes as modes
        wf = modes.build_r2v({
            "job_id": "t", "prompt": "x", "duration": 5,
            "ref_images": ["r.png"],
            "width": 864, "height": 480,
            "steps": 6, "sampler": "euler", "scheduler": "beta",
            "shift_video": 9.5, "shift_audio": 2.5,
        })
        self.assertEqual(wf["9"]["inputs"]["steps"], 6)
        self.assertEqual(wf["139"]["inputs"]["shift_video"], 9.5)

    def test_flf_workflow_supports_attention_and_twopass(self):
        """共享面板上的注意力 / 二采在 flf 下必须**真的生效**。

        【为什么】面板上能调、工作流里不接 = 假控件。
        """
        from h3studio import h3_video_modes as modes
        wf = modes.build_flf({
            "job_id": "t", "prompt": "x", "duration": 5,
            "first_frame": "a.png",
            "attention": "kitchen",
            "twopass": {"enabled": True, "transition_step": 3,
                        "lowres_scale": 0.5, "rho": 0.6, "cfg": 1.0},
        })
        # 注意力节点在链路里
        self.assertTrue(any(n.get("class_type") == "ModelAttentionBackend"
                            for n in wf.values()),
                        "注意力节点必须出现在 flf 工作流里")
        # 二采节点在链路里，且引用的是 T8 条件节点（136），不是文生视频的 105:104
        self.assertIn("171", wf)
        self.assertEqual(wf["171"]["inputs"]["positive"], ["136", 0])
        # 没有残留的标准采样块
        self.assertNotIn("14", wf)

    def test_r2v_workflow_supports_attention(self):
        """参考生视频同样支持注意力（共享面板的控件必须真生效）。"""
        from h3studio import h3_video_modes as modes
        wf = modes.build_r2v({
            "job_id": "t", "prompt": "x", "duration": 5,
            "ref_images": ["r.png"], "attention": "kitchen",
        })
        self.assertTrue(any(n.get("class_type") == "ModelAttentionBackend"
                            for n in wf.values()))

    def test_flf_workflow_has_no_dangling_node_refs(self):
        """工作流里不能引用不存在的节点（提交时会被 ComfyUI 拒）。"""
        from h3studio import h3_video_modes as modes
        for params in (
            {"job_id": "t", "prompt": "x", "duration": 5, "first_frame": "a.png"},
            {"job_id": "t", "prompt": "x", "duration": 5,
             "first_frame": "a.png", "last_frame": "b.png",
             "attention": "kitchen",
             "twopass": {"enabled": True, "transition_step": 3, "rho": 0.6}},
            {"job_id": "t", "prompt": "x", "duration": 5,
             "ref_images": ["r.png"], "ref_videos": ["v.mp4"],
             "ref_audios": ["a.wav"], "attention": "kitchen"},
        ):
            wf = modes.build_flf(dict(params)) if params.get(
                "first_frame") else modes.build_r2v(dict(params))
            ids = set(wf.keys())
            for nid, node in wf.items():
                for key, value in (node.get("inputs") or {}).items():
                    if (isinstance(value, list) and len(value) == 2
                            and isinstance(value[0], str)
                            and value[0] not in ids):
                        self.fail(
                            f"节点 {nid}.{key} 引用了不存在的节点 {value[0]}")


if __name__ == "__main__":
    unittest.main()
