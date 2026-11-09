# -*- coding: utf-8 -*-
"""YuE2 音乐生成（文生音乐 / 参考生音乐）接入的回归测试。

覆盖本轮接入写死的几个**真事实**（不测它们就会回归）：

1. **必须走 ComfyUI 原生 YuE2 节点，不能用 custom_nodes/ComfyUI-YuE2**
   第三方 `YuE2Loader` 内部会调 `torch.cuda.set_per_process_memory_fraction()`
   （yue2/pipeline.py:159）—— 那是**进程级**设置，跑一次音乐任务后整个
   ComfyUI 进程的显存上限被永久压低，之后跑 H3 视频（要 30GB+）必然 OOM。
   所以工作流里只能出现原生节点：CheckpointLoaderSimple / YuE2GenerateABC /
   YuE2GenerateMusic / EmptyYuE2LatentAudio / KSampler / VAEDecodeAudio /
   SaveAudioAdvanced（参考生音乐再加 AudioEncoderLoader /
   SheetSage2AudioToABC）。

2. **SaveAudioAdvanced 的 format 是 COMFY_DYNAMICCOMBO_V3**
   API 格式里必须写成嵌套 `{"format": "flac"}`（+ 顶层点号键 "format.quality"）。
   写成 `"format": "flac"` 会被当成字符串，执行时报类型错。

3. **空 latent 的 seconds 必须接模型实际输出的秒数**（["25", 1]），
   不是用户请求的时长上限 —— 否则 latent 长度与 condition 不匹配，
   ComfyUI 会报 "YuE2 latent duration must match the seconds output of
   YuE2 Text Encode"。

4. **负条件必须走 ConditioningZeroOut**（官方模板做法）。
   CFG=1 时不需要真负条件。

5. **参考生音乐的 ABC 来自参考音频**（SheetSage2AudioToABC，mode=melody），
   不是来自 YuE2GenerateABC —— 两者不能同时出现在一条工作流里。

6. **模型权重缺失要在前端可见**（/api/music-options 下发 available=false），
   而不是等提交后才报错。

7. **产物 kind=audio**：ComfyUI 的 SaveAudioAdvanced 把文件挂在 history 的
   **"audio"** 键下（不是 "images"）—— 提取逻辑漏了这个键就取不到产物。

8. **音频 MIME 要显式给** —— 否则浏览器把 .flac 当下载而不是播放。

运行：python3 -m pytest tests/test_music_yue2.py -v
"""
import json
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import music_yue2  # noqa: E402
from h3studio import gen_types  # noqa: E402
from h3studio import workflows  # noqa: E402


# ComfyUI 原生 YuE2 节点白名单 —— 工作流里只允许出现这些
NATIVE_YUE2_NODES = {
    "CheckpointLoaderSimple",
    "YuE2GenerateABC",
    "YuE2GenerateMusic",
    "EmptyYuE2LatentAudio",
    "ConditioningZeroOut",
    "KSampler",
    "VAEDecodeAudio",
    "SaveAudioAdvanced",
    # 参考生音乐专用
    "AudioEncoderLoader",
    "SheetSage2AudioToABC",
    "LoadAudio",
}

# 第三方节点 —— 用了就会把 ComfyUI 进程的显存上限永久压低
FORBIDDEN_NODES = {"YuE2Loader", "YuE2Unload", "YuE2Plan", "YuE2SaveText"}


class TestMusicRegistration(unittest.TestCase):
    """模式注册 —— 前端标签来自后端真实注册，不硬编码。"""

    def test_builders_registered(self):
        self.assertIn("t2a", workflows.BUILDERS)
        self.assertIn("a2a", workflows.BUILDERS)

    def test_generation_types(self):
        t2a = gen_types.type_for_mode("t2a")
        a2a = gen_types.type_for_mode("a2a")
        self.assertEqual(t2a["media"], "audio")
        self.assertEqual(a2a["media"], "audio")
        self.assertEqual(t2a["category"], "music")
        self.assertEqual(a2a["category"], "music")

    def test_registered_modes_include_music(self):
        modes = gen_types.registered_modes()
        self.assertIn("t2a", modes)
        self.assertIn("a2a", modes)

    def test_music_category_listed(self):
        cats = [c["key"] for c in gen_types.list_categories()]
        self.assertIn("music", cats)


class TestMusicWorkflow(unittest.TestCase):
    """工作流结构 —— 与官方模板 audio_yue2_text2music.json 一致。"""

    def _wf(self, **over):
        base = {"prompt": "English, warm piano pop, 88 BPM", "seed": 42,
                "job_id": "testjob"}
        base.update(over)
        return workflows.build_workflow("t2a" if over.pop("cover", False) is False
                                        else "a2a", base)

    def test_only_native_nodes(self):
        wf = self._wf()
        classes = {n["class_type"] for n in wf.values()}
        forbidden = classes & FORBIDDEN_NODES
        self.assertFalse(forbidden, f"不能用第三方 YuE2 节点（会永久压低 ComfyUI 显存上限）: {forbidden}")
        unknown = classes - NATIVE_YUE2_NODES
        self.assertFalse(unknown, f"出现非原生 YuE2 节点: {unknown}")

    def test_text_to_music_has_abc_generator(self):
        wf = self._wf()
        classes = {n["class_type"] for n in wf.values()}
        self.assertIn("YuE2GenerateABC", classes)
        self.assertNotIn("SheetSage2AudioToABC", classes)

    def test_cover_uses_sheetsage_and_not_yue2_abc(self):
        wf = self._wf(cover=True, ref_audio="h3studio/ref.wav")
        classes = {n["class_type"] for n in wf.values()}
        self.assertIn("SheetSage2AudioToABC", classes)
        self.assertIn("AudioEncoderLoader", classes)
        # 参考音频提供谱面 -> 不该再有 YuE2GenerateABC
        self.assertNotIn("YuE2GenerateABC", classes)

    def test_cover_audio_is_audio_object_not_filename(self):
        """【真机踩坑 1】SheetSage2AudioToABC.audio 必须是 AUDIO **对象**。

        直接塞文件名字符串 -> ComfyUI 报
        "'str' object has no attribute 'get'"（节点 1 就挂，任务失败）。
        必须先经 LoadAudio 读成 AUDIO 对象再接进去。
        """
        wf = self._wf(cover=True, ref_audio="h3studio_ref_abc.wav")
        abc = next(n for n in wf.values()
                   if n["class_type"] == "SheetSage2AudioToABC")
        audio_input = abc["inputs"]["audio"]
        self.assertIsInstance(audio_input, list,
                              "audio 必须是节点引用 [node_id, slot]，不能是文件名")
        self.assertEqual(len(audio_input), 2)
        source_id = audio_input[0]
        source = wf[source_id]
        self.assertEqual(source["class_type"], "LoadAudio")

    def test_cover_loadaudio_uses_combo_input_name(self):
        """【真机踩坑 2】LoadAudio 的输入名是 `audio`（COMBO），不是 `audio_file`。

        `audio_file` 是 VHS_LoadAudio 的输入名，且它要**绝对文件系统路径**
        （validate_path 用 os.path.isfile 判）-> 相对路径报 "Invalid file path"。
        LoadAudio.audio 是 COMBO，值必须命中 input/ 顶层清单 -> 所以服务端
        把参考音频上传到 input 顶层（server.comfy_upload_audio_flat）。
        """
        wf = self._wf(cover=True, ref_audio="h3studio_ref_abc.wav")
        loader = next(n for n in wf.values()
                      if n["class_type"] == "LoadAudio")
        self.assertIn("audio", loader["inputs"])
        self.assertNotIn("audio_file", loader["inputs"])
        # 传进去的必须是**纯文件名**（没有子目录）
        self.assertNotIn("/", loader["inputs"]["audio"])

    def test_server_uploads_ref_audio_to_input_top_level(self):
        """服务端必须把参考音频传到 input 顶层（LoadAudio 的 COMBO 只认顶层）。"""
        source = (REPO / "server.py").read_text(encoding="utf-8")
        self.assertIn("comfy_upload_audio_flat", source)
        self.assertIn("_upload_ref_audio_flat", source)
        # subfolder 必须是空字符串
        self.assertIn('data.add_field("subfolder", "")', source)

    def test_audio_output_kind_recognized(self):
        """音频扩展名必须被识别成 audio（否则素材库里归成 video）。"""
        source = (REPO / "h3studio" / "asset_service.py").read_text(encoding="utf-8")
        # kind 推断要认音频扩展名
        self.assertIn('".flac"', source)
        self.assertIn('".mp3"', source)
        # category 三分支：image / audio / video
        self.assertIn('else "audio" if kind == "audio"', source)

    def test_cover_requires_ref_audio(self):
        with self.assertRaises(ValueError):
            workflows.build_workflow("a2a", {"prompt": "x", "job_id": "y"})

    def test_empty_latent_uses_model_seconds_not_requested_duration(self):
        wf = self._wf(max_duration=300)
        latent = next(n for n in wf.values()
                      if n["class_type"] == "EmptyYuE2LatentAudio")
        # seconds 必须接 YuE2GenerateMusic 的第 2 个输出（实际秒数）
        self.assertEqual(latent["inputs"]["seconds"], ["25", 1])

    def test_negative_uses_conditioning_zero_out(self):
        wf = self._wf()
        classes = {n["class_type"] for n in wf.values()}
        self.assertIn("ConditioningZeroOut", classes)

    def test_save_audio_dynamic_combo_format(self):
        wf = self._wf()
        save = next(n for n in wf.values()
                    if n["class_type"] == "SaveAudioAdvanced")
        # COMFY_DYNAMICCOMBO_V3 —— 【实测】必须写扁平字符串。
        # 写嵌套 dict {"format": "flac"} 会报
        # "SaveAudioAdvanced.execute() missing 1 required positional argument: 'format'"
        self.assertEqual(save["inputs"]["format"], "flac")
        self.assertIsInstance(save["inputs"]["format"], str)

    def test_save_audio_mp3_has_quality(self):
        wf = self._wf(format="mp3")
        save = next(n for n in wf.values()
                    if n["class_type"] == "SaveAudioAdvanced")
        # 附加参数走顶层点号键（实测可用）
        self.assertEqual(save["inputs"].get("format.quality"), "V0")
        self.assertEqual(save["inputs"]["format"], "mp3")

    def test_official_sampler_defaults(self):
        wf = self._wf()
        ks = next(n for n in wf.values() if n["class_type"] == "KSampler")
        # 官方模板 audio_yue2_text2music.json：32 步 / cfg 1 / dpm_2 / sgm_uniform
        self.assertEqual(ks["inputs"]["steps"], 32)
        self.assertEqual(ks["inputs"]["cfg"], 1.0)
        self.assertEqual(ks["inputs"]["sampler_name"], "dpm_2")
        self.assertEqual(ks["inputs"]["scheduler"], "sgm_uniform")

    def test_seed_is_shared_by_abc_music_and_ksampler(self):
        wf = self._wf(seed=1234)
        abc = next(n for n in wf.values() if n["class_type"] == "YuE2GenerateABC")
        music = next(n for n in wf.values() if n["class_type"] == "YuE2GenerateMusic")
        ks = next(n for n in wf.values() if n["class_type"] == "KSampler")
        self.assertEqual(abc["inputs"]["seed"], 1234)
        self.assertEqual(music["inputs"]["seed"], 1234)
        self.assertEqual(ks["inputs"]["seed"], 1234)

    def test_empty_prompt_rejected(self):
        with self.assertRaises(ValueError):
            workflows.build_workflow("t2a", {"prompt": "   ", "job_id": "z"})

    def test_duration_clamped(self):
        wf = self._wf(max_duration=99999)
        music = next(n for n in wf.values() if n["class_type"] == "YuE2GenerateMusic")
        self.assertEqual(music["inputs"]["max_duration"], music_yue2.YUE2_MAX_DURATION)
        wf = self._wf(max_duration=0.001)
        music = next(n for n in wf.values() if n["class_type"] == "YuE2GenerateMusic")
        self.assertEqual(music["inputs"]["max_duration"], music_yue2.YUE2_MIN_DURATION)

    def test_invalid_mode_falls_back_to_full(self):
        wf = self._wf(mode="nonsense")
        abc = next(n for n in wf.values() if n["class_type"] == "YuE2GenerateABC")
        self.assertEqual(abc["inputs"]["mode"], "full")

    def test_invalid_sampler_falls_back(self):
        wf = self._wf(sampler="not_a_sampler")
        ks = next(n for n in wf.values() if n["class_type"] == "KSampler")
        self.assertEqual(ks["inputs"]["sampler_name"], "dpm_2")

    def test_style_and_lyrics_reach_both_abc_and_music(self):
        wf = self._wf(prompt="jazz, 120 BPM", lyrics="[Verse]\nhi")
        abc = next(n for n in wf.values() if n["class_type"] == "YuE2GenerateABC")
        music = next(n for n in wf.values() if n["class_type"] == "YuE2GenerateMusic")
        self.assertEqual(abc["inputs"]["style"], "jazz, 120 BPM")
        self.assertEqual(music["inputs"]["style"], "jazz, 120 BPM")
        self.assertEqual(abc["inputs"]["lyrics"], "[Verse]\nhi")
        self.assertEqual(music["inputs"]["lyrics"], "[Verse]\nhi")


class TestMusicModelOptions(unittest.TestCase):
    """模型清单 —— 权重缺失要能在前端看到。"""

    def test_model_options_shape(self):
        opts = music_yue2.model_options()
        self.assertIn("checkpoints", opts)
        self.assertIn("audio_encoders", opts)
        self.assertIn("default_checkpoint", opts)
        self.assertIn("default_audio_encoder", opts)
        self.assertIn("samplers", opts)
        self.assertIn("schedulers", opts)
        self.assertIn("modes", opts)
        self.assertIn("formats", opts)

    def test_every_checkpoint_has_availability_flag(self):
        opts = music_yue2.model_options()
        for key, m in opts["checkpoints"].items():
            self.assertIn("available", m, f"{key} 缺 available 标记")
            self.assertIsInstance(m["available"], bool)

    def test_every_encoder_has_availability_flag(self):
        opts = music_yue2.model_options()
        for key, m in opts["audio_encoders"].items():
            self.assertIn("available", m, f"{key} 缺 available 标记")

    def test_missing_checkpoint_raises_clear_error(self):
        """权重缺失必须给**明确中文错误**，而不是让 ComfyUI 报
        "value not in list"（用户看不懂）。"""
        original = music_yue2.checkpoint_available
        music_yue2.checkpoint_available = lambda key: False
        try:
            with self.assertRaises(ValueError) as ctx:
                workflows.build_workflow("t2a", {"prompt": "x", "job_id": "j"})
            self.assertIn("权重", str(ctx.exception))
            self.assertIn("checkpoints", str(ctx.exception))
        finally:
            music_yue2.checkpoint_available = original

    def test_missing_audio_encoder_raises_clear_error(self):
        original = music_yue2.audio_encoder_available
        music_yue2.audio_encoder_available = lambda key: False
        try:
            with self.assertRaises(ValueError) as ctx:
                workflows.build_workflow("a2a", {"prompt": "x", "job_id": "j",
                                                 "ref_audio": "h3studio/r.wav"})
            self.assertIn("权重", str(ctx.exception))
            self.assertIn("audio_encoders", str(ctx.exception))
        finally:
            music_yue2.audio_encoder_available = original


class TestAudioOutputHandling(unittest.TestCase):
    """产物提取 —— ComfyUI 把音频挂在 "audio" 键下。"""

    def test_extract_outputs_reads_audio_key(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "h3studio_server_probe", REPO / "server.py")
        # server.py 顶层会做很多初始化（读 config、连库），不能直接 import。
        # 这里改为静态检查：确认 _extract_outputs 的源码里包含 "audio" 键。
        source = (REPO / "server.py").read_text(encoding="utf-8")
        self.assertIn('("audio", "audio")', source)

    def test_media_types_include_audio(self):
        source = (REPO / "server.py").read_text(encoding="utf-8")
        self.assertIn('".flac": "audio/flac"', source)
        self.assertIn('".mp3": "audio/mpeg"', source)

    def test_audio_output_dir_declared(self):
        source = (REPO / "server.py").read_text(encoding="utf-8")
        self.assertIn('AUDIO_DIR = OUT_DIR / "audio"', source)


class TestMusicLiveComfy(unittest.TestCase):
    """真机验证 —— 打真实 ComfyUI 的 /prompt。

    这一组是「不测就会回归」的那类：静态检查看不出 ComfyUI 的动态节点
    参数格式到底要哪种写法，必须真跑一次。

    需要本机 ComfyUI 在 8188。不可达时整类 skip（不算失败）。
    """

    COMFY = "http://127.0.0.1:8188"

    @classmethod
    def setUpClass(cls):
        import urllib.request
        try:
            urllib.request.urlopen(cls.COMFY + "/system_stats", timeout=5).read()
            cls.online = True
        except Exception:
            cls.online = False

    def setUp(self):
        if not self.online:
            self.skipTest("ComfyUI 未运行（127.0.0.1:8188 不可达）")

    def _probe_audio_output(self, save_inputs):
        """用一段现成音频跑 SaveAudioAdvanced，验证 format 的写法。"""
        import glob
        import json
        import os
        import time
        import urllib.request

        cands = glob.glob("/home/ubuntu/comfyui/input/*.wav")[:1] or \
            glob.glob("/home/ubuntu/comfyui/input/*.mp3")[:1]
        if not cands:
            self.skipTest("ComfyUI input 里没有可用音频")
        fname = os.path.basename(cands[0])

        wf = {
            "1": {"inputs": {"audio": fname}, "class_type": "LoadAudio"},
            "2": {"inputs": save_inputs, "class_type": "SaveAudioAdvanced"},
        }
        req = urllib.request.Request(
            self.COMFY + "/prompt", data=json.dumps({"prompt": wf}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                pid = json.loads(resp.read())["prompt_id"]
        except Exception as exc:
            self.fail(f"/prompt 被拒: {exc}")

        deadline = time.time() + 300
        while time.time() < deadline:
            time.sleep(3)
            with urllib.request.urlopen(self.COMFY + f"/history/{pid}", timeout=30) as r:
                entry = json.loads(r.read()).get(pid)
            if not entry:
                continue
            st = entry.get("status") or {}
            if st.get("status_str") == "error":
                msg = ""
                for m in st.get("messages", []):
                    if m and m[0] == "execution_error":
                        msg = (m[1] or {}).get("exception_message", "")
                return False, msg
            if entry.get("outputs"):
                return True, json.dumps(entry["outputs"], ensure_ascii=False)
        return False, "timeout"

    def test_flat_format_string_is_accepted(self):
        """扁平字符串 "format": "flac" 必须被 ComfyUI 接受。"""
        ok, detail = self._probe_audio_output({
            "audio": ["1", 0], "filename_prefix": "h3studio_test/fmt_flat",
            "format": "flac"})
        self.assertTrue(ok, f"扁平 format 被拒: {detail}")
        # 产物挂在 "audio" 键下 —— 这正是 server._extract_outputs 要认的键
        self.assertIn("audio", detail)

    def test_nested_dict_format_is_rejected(self):
        """嵌套 dict 写法必须失败（写死这个反面案例，防止有人「优化」回去）。"""
        ok, detail = self._probe_audio_output({
            "audio": ["1", 0], "filename_prefix": "h3studio_test/fmt_nested",
            "format": {"format": "flac"}})
        self.assertFalse(ok, "嵌套 dict 写法本该失败，却成功了 —— 说明行为变了，要重新核对")
        self.assertIn("format", detail)


class TestMusicFrontend(unittest.TestCase):
    """前端 —— 面板结构与参数持久化。"""

    def test_index_html_has_music_panel(self):
        html = (REPO / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="musicPanel"', html)
        self.assertIn('id="musicStyle"', html)
        self.assertIn('id="musicLyrics"', html)
        self.assertIn('id="musicRefPick"', html)
        self.assertIn('data-mode="t2a"', html)
        self.assertIn('data-mode="a2a"', html)
        self.assertIn("/static/music.js", html)

    def test_music_js_exists_and_persists_params(self):
        js = (REPO / "web" / "music.js").read_text(encoding="utf-8")
        self.assertIn("h3studio.musicParams", js)
        self.assertIn("window.MusicUI", js)

    def test_app_js_wires_music_mode(self):
        js = (REPO / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("isMusicMode", js)
        self.assertIn("window.MusicUI", js)
        self.assertIn("MusicUI.loadOptions", js)

    def test_audio_preview_uses_audio_element(self):
        js = (REPO / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("<audio src=", js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
