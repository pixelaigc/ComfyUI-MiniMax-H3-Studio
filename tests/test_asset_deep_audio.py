# -*- coding: utf-8 -*-
"""
资产素材库 —— 深度音频分析管线测试。

覆盖：
  1. **统一接口调度**：素材库通过 AudioAnalyzer 的可插拔后端接到 analyze_audio.py
  2. **不二次转码**：深度分析前后源文件 md5/mtime 必须不变
  3. **异步任务**：触发分析立刻返回 job，状态流转 queued -> running -> done/failed
  4. **结果入库**：分析文本落到 asset_analyses.text（与图片/视频同一处存储），
     结构化数据落到 extra
  5. **失败可观测**：阶段失败要如实记录，不能误报「全绿」
  6. **GPU 选卡**：按空闲显存挑卡并用 UUID 指定
  7. **接口级**：触发 / 轮询 / 去重排队 的 HTTP 行为

**不跑真实深度分析**（那要几分钟 + ComfyUI venv 依赖）——
这里用一个假的后端脚本验证**调度契约**，真实链路在真机验证里跑。

运行：python3 -m pytest tests/test_asset_deep_audio.py -v
"""
import asyncio
import json
import os
import shutil
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from h3studio import analysis, analysis_jobs, asset_repo, asset_service  # noqa: E402
from h3studio import assets as A  # noqa: E402
from h3studio import audio_deep  # noqa: E402
from h3studio.assets import LibraryError  # noqa: E402


# ------------------------------------------------------------------ 假后端脚本

#: 模拟 analyze_audio.py 的行为：接受同样的 CLI 参数，输出同样结构的 JSON。
#: 用一个「会改动源文件」的开关来验证素材库的源文件保护确实生效。
FAKE_SCRIPT = textwrap.dedent('''
    #!/usr/bin/env python3
    import argparse, json, os, sys, time
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--out")
    ap.add_argument("--stages", default="bpm")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--asr-model", default="medium")
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--llm-base-url")
    ap.add_argument("--llm-model")
    ap.add_argument("--studio-url")
    ap.add_argument("--no-separation", action="store_true")
    ap.add_argument("--max-seconds", type=float)
    args = ap.parse_args()

    path = os.path.abspath(args.audio)
    # 模拟「脚本意外改了源文件」的场景（靠环境变量触发）
    if os.environ.get("FAKE_MUTATE_SOURCE"):
        with open(path, "ab") as fh:
            fh.write(b"\\x00MUTATED")

    # 模拟慢任务
    time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))

    result = {
        "file": path,
        "analyzed_at": "2026-11-08T18:00:00+0800",
        "source_untouched": True,
        "media": {"ok": True, "container": "wav", "duration": 116.657052,
                  "size_bytes": 20578348, "bit_rate": 1411203,
                  "codec": "pcm_s16le", "sample_rate": 44100, "channels": 2,
                  "bits_per_sample": 16},
        "errors": [],
        "bpm": {"engine": "librosa", "bpm": 151.999, "n_beats": 282,
                "bpm_candidates": [76.0, 152.0, 304.0],
                "beat_regularity": 0.9426, "bar_seconds_4_4": 1.579,
                "essentia": {"bpm": 151.714, "confidence": 0.8,
                             "diff_vs_librosa": 0.285},
                "local_tempo": {"min": 78.3, "max": 161.5, "std": 18.34}},
        "instruments": {"device": "cpu",
                        "instrument_summary": [
                            {"name": "choir", "score": 11.66, "weight": 0.3877},
                            {"name": "human singing voice", "score": 10.73, "weight": 0.3567}],
                        "mood_summary": [
                            {"name": "calm relaxing ambient music", "score": 2.17}],
                        "engines": {"clap": {"frames": [], "n_frames": 45, "seconds": 8.01},
                                    "ast": {"frames": [], "n_frames": 45, "seconds": 30.9}}},
        "vocals": {"vad": {"engine": "silero-v5.1", "model_source": "local-cache",
                           "n_frames": 7290, "mean_prob": 0.3266,
                           "voiced_ratio": 0.2997, "speech_seconds": 34.97,
                           "seconds": 51.91,
                           "segments": [[0.05, 0.16], [12.5, 16.62]]},
                   "separation": {"engine": "htdemucs", "device": "cpu",
                                  "stems_rms": {"vocals": 0.11, "other": 0.2},
                                  "stem_ratio": {"vocals": 0.35, "other": 0.65},
                                  "has_vocals": True, "seconds": 80.9}},
        "asr": {"engine": "medium", "model": "medium", "language": "zh",
                "language_probability": 0.99, "rtf": 0.0194,
                "infer_seconds": 4.56, "duration": 235.373,
                "segments": [{"id": 0, "start": 12.5, "end": 16.62,
                              "text": "感受停在我发短的指尖"}],
                "full_text": "感受停在我发短的指尖"},
        "llm": {"model": "qwen3.8-flash-next-iq3_xxs", "seconds": 3.54,
                "analysis": "{\\"mood\\": \\"calm relaxing\\"}"},
        "total_seconds": 73.74,
    }
    if os.environ.get("FAKE_ASR_FAIL"):
        result["asr"] = {"engine": "medium", "error": "CUDA out of memory"}
    if os.environ.get("FAKE_STAGE_ERRORS"):
        # 模拟「顶层 errors 为空但阶段内部失败」—— 实测踩过 GPU OOM 的坑
        result["errors"] = []
        result["instruments"]["engines"]["ast"] = {
            "error": "OutOfMemoryError: CUDA out of memory"}
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False)
''')


def _write_fake_script(directory: Path) -> Path:
    path = directory / "fake_analyze_audio.py"
    path.write_text(FAKE_SCRIPT, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


# ------------------------------------------------------------------ 测试脚手架


class DeepAudioTestBase(unittest.TestCase):
    """每个用例独立素材库 + 独立假脚本。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="assetlib-deep-"))
        self.root = self.tmp / "library"
        self.src = self.tmp / "src"
        self.src.mkdir(parents=True, exist_ok=True)
        self.script = _write_fake_script(self.tmp)

        self._old_root = os.environ.get("ASSET_LIBRARY_ROOT")
        self._old_script = os.environ.get("H3_AUDIO_ANALYZER_SCRIPT")
        self._old_python = os.environ.get("H3_AUDIO_ANALYZER_PYTHON")
        os.environ["ASSET_LIBRARY_ROOT"] = str(self.root)
        os.environ["H3_AUDIO_ANALYZER_SCRIPT"] = str(self.script)
        os.environ["H3_AUDIO_ANALYZER_PYTHON"] = sys.executable
        self.addCleanup(self._restore_env)
        A.init_db()

    def _restore_env(self):
        for key, old in (("ASSET_LIBRARY_ROOT", self._old_root),
                         ("H3_AUDIO_ANALYZER_SCRIPT", self._old_script),
                         ("H3_AUDIO_ANALYZER_PYTHON", self._old_python)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def conn(self):
        return A.connect()

    def make_wav(self, name="music.wav", payload=b"RIFFfakewavdata") -> Path:
        path = self.src / name
        path.write_bytes(payload)
        return path

    def register(self, path: Path, name=None):
        """注册一个素材，返回素材 dict（register_asset 的返回是 {"asset": ...}）。"""
        with self.conn() as conn:
            result = asset_service.register_asset(
                conn, source_path=path, filename=name or path.name)
        return result["asset"]


# ------------------------------------------------------------------ 1. 统一接口调度


class TestAudioDeepBackendContract(DeepAudioTestBase):
    """素材库 -> 可插拔后端 -> analyze_audio.py 的调用契约。"""

    def test_environment_check_reports_ok(self):
        env = audio_deep.check_environment()
        self.assertTrue(env["ok"], f"环境自检应通过：{env.get('reason')}")
        self.assertEqual(env["script"], str(self.script))

    def test_environment_check_reports_missing_script(self):
        os.environ["H3_AUDIO_ANALYZER_SCRIPT"] = "/nonexistent/analyze_audio.py"
        env = audio_deep.check_environment()
        self.assertFalse(env["ok"])
        self.assertIn("找不到", env["reason"])

    def test_run_analyzer_returns_script_json(self):
        src = self.make_wav()
        payload = audio_deep.run_analyzer(src, stages=["bpm"])
        self.assertEqual(payload["bpm"]["bpm"], 151.999)
        self.assertEqual(payload["bpm"]["essentia"]["bpm"], 151.714)
        self.assertEqual(payload["bpm"]["bpm_candidates"], [76.0, 152.0, 304.0])
        self.assertTrue(payload["_source_untouched_verified"])

    def test_render_text_is_chinese_and_complete(self):
        src = self.make_wav()
        payload = audio_deep.run_analyzer(src, stages=["bpm", "asr"])
        text = audio_deep.render_text(payload)
        # 七个章节都在
        for heading in ("音频基本信息", "节拍与速度", "乐器与音色",
                        "人声检测与分离", "语音识别", "语义补充分析", "分析过程说明"):
            self.assertIn(heading, text)
        # 关键数值都渲染出来了
        self.assertIn("151.999", text)
        self.assertIn("151.714", text)
        self.assertIn("76.0, 152.0, 304.0", text)   # BPM 候选值
        self.assertIn("0.9426", text)               # 节拍规整度
        self.assertIn("感受停在我发短的指尖", text)  # ASR 文本
        self.assertIn("00:12.50", text)             # 时间戳格式化
        # 乐器标签做了中译
        self.assertIn("合唱", text)
        self.assertIn("人声演唱", text)

    def test_to_extra_keeps_structured_data(self):
        src = self.make_wav()
        payload = audio_deep.run_analyzer(src, stages=["bpm", "asr"])
        extra = audio_deep.to_extra(payload)
        self.assertEqual(extra["bpm"]["bpm"], 151.999)
        self.assertEqual(extra["bpm"]["essentia_bpm"], 151.714)
        self.assertEqual(extra["bpm"]["bpm_candidates"], [76.0, 152.0, 304.0])
        self.assertEqual(extra["bpm"]["beat_regularity"], 0.9426)
        self.assertIn("min", extra["bpm"]["local_tempo"])
        self.assertEqual(extra["asr"]["engine"], "medium")
        self.assertEqual(extra["asr"]["segments"][0]["text"],
                         "感受停在我发短的指尖")
        self.assertEqual(extra["vocals"]["voiced_ratio"], 0.2997)
        self.assertTrue(extra["source_untouched"])

    def test_analyze_returns_text_and_extra(self):
        src = self.make_wav()
        result = audio_deep.analyze(src, {"stages": ["bpm"]})
        self.assertIn("节拍与速度", result["text"])
        self.assertEqual(result["extra"]["bpm"]["bpm"], 151.999)
        self.assertIn("raw", result)


# ------------------------------------------------------------------ 2. 不二次转码


class TestSourceNeverMutated(DeepAudioTestBase):
    """深度分析绝不改源文件。"""

    def test_source_md5_unchanged_after_analysis(self):
        src = self.make_wav()
        before = audio_deep.file_signature(src)
        audio_deep.analyze(src, {"stages": ["bpm"]})
        after = audio_deep.file_signature(src)
        self.assertEqual(before["md5"], after["md5"], "分析不得改动源文件内容")
        self.assertEqual(before["size"], after["size"])
        self.assertEqual(before["mtime"], after["mtime"], "分析不得改动源文件 mtime")

    def test_source_mutation_is_detected_and_rejected(self):
        """脚本真的改了源文件时，素材库必须报错而不是默默接受。"""
        src = self.make_wav()
        os.environ["FAKE_MUTATE_SOURCE"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_MUTATE_SOURCE", None)
        with self.assertRaises(audio_deep.DeepAudioError) as ctx:
            audio_deep.run_analyzer(src, stages=["bpm"])
        self.assertEqual(ctx.exception.code, "audio_source_mutated")
        self.assertIn("修改了源音频文件", ctx.exception.message)

    def test_source_signature_helper_fields(self):
        src = self.make_wav()
        sig = audio_deep.file_signature(src)
        self.assertEqual(len(sig["md5"]), 32)
        self.assertEqual(sig["size"], src.stat().st_size)
        self.assertEqual(sig["mtime"], int(src.stat().st_mtime))


# ------------------------------------------------------------------ 3. 失败可观测


class TestStageFailureVisibility(DeepAudioTestBase):
    """阶段内部失败不能被顶层 errors[] 掩盖。"""

    def test_top_level_errors_empty_but_stage_failed_is_surfaced(self):
        os.environ["FAKE_STAGE_ERRORS"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_STAGE_ERRORS", None)
        src = self.make_wav()
        payload = audio_deep.run_analyzer(src, stages=["bpm", "instruments"])
        # 脚本顶层 errors 确实是空的（这正是实测踩到的坑）
        self.assertEqual(payload.get("errors"), [])
        # 但素材库必须把阶段内的失败收集出来
        problems = audio_deep.collect_stage_errors(payload)
        self.assertTrue(problems, "阶段内部失败必须被收集")
        self.assertTrue(any("ast" in p for p in problems))
        # 渲染文本里也要写明
        text = audio_deep.render_text(payload)
        self.assertIn("以下阶段失败", text)

    def test_asr_failure_is_reported(self):
        os.environ["FAKE_ASR_FAIL"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_ASR_FAIL", None)
        src = self.make_wav()
        payload = audio_deep.run_analyzer(src, stages=["bpm", "asr"])
        problems = audio_deep.collect_stage_errors(payload)
        self.assertTrue(any("asr" in p for p in problems))
        extra = audio_deep.to_extra(payload)
        self.assertTrue(extra["errors"])

    def test_clean_run_has_no_problems(self):
        src = self.make_wav()
        payload = audio_deep.run_analyzer(src, stages=["bpm"])
        self.assertEqual(audio_deep.collect_stage_errors(payload), [])
        text = audio_deep.render_text(payload)
        self.assertIn("所有阶段执行成功", text)


# ------------------------------------------------------------------ 4. GPU 选卡


class TestGpuSelection(DeepAudioTestBase):
    """GPU 必须按 UUID 指定，且要挑空闲的卡。"""

    def test_pick_gpu_returns_none_without_nvidia_smi(self):
        old = shutil.which
        try:
            shutil.which = lambda name: None if name == "nvidia-smi" else old(name)
            self.assertIsNone(audio_deep.pick_gpu(["asr"]))
            self.assertIn("CPU", audio_deep.gpu_selection_hint(["asr"]))
        finally:
            shutil.which = old

    def test_pick_gpu_skips_busy_cards(self):
        gpus = [
            {"index": 0, "uuid": "GPU-busy", "free_mb": 500.0, "total_mb": 49140.0},
            {"index": 1, "uuid": "GPU-free", "free_mb": 19000.0, "total_mb": 23040.0},
        ]
        old = audio_deep.list_gpus
        try:
            audio_deep.list_gpus = lambda: gpus
            picked = audio_deep.pick_gpu(["asr"])
            self.assertIsNotNone(picked)
            self.assertEqual(picked["uuid"], "GPU-free",
                             "必须跳过显存被占满的卡，选空闲的")
            self.assertEqual(picked["index"], 1)
        finally:
            audio_deep.list_gpus = old

    def test_pick_gpu_returns_none_when_all_busy(self):
        gpus = [{"index": 0, "uuid": "GPU-x", "free_mb": 100.0, "total_mb": 49140.0}]
        old = audio_deep.list_gpus
        try:
            audio_deep.list_gpus = lambda: gpus
            self.assertIsNone(audio_deep.pick_gpu(["asr"]),
                              "所有卡都不够时必须退回 CPU 而不是硬撞 cuda:0")
            hint = audio_deep.gpu_selection_hint(["asr"])
            self.assertIn("退回 CPU", hint)
        finally:
            audio_deep.list_gpus = old

    def test_bpm_only_needs_no_gpu(self):
        """只有 bpm 阶段时不需要占卡。"""
        old = audio_deep.list_gpus
        try:
            audio_deep.list_gpus = lambda: [
                {"index": 0, "uuid": "GPU-x", "free_mb": 100.0, "total_mb": 100.0}]
            self.assertIsNone(audio_deep.pick_gpu(["bpm"]))
        finally:
            audio_deep.list_gpus = old

    def test_vram_table_keys_match_script_stage_names(self):
        """回归：显存表的键必须与 analyze_audio.py 的 --stages 取值一致。

        实测踩过：表里写的是子引擎名（clap/ast/vad/separation），
        而脚本的阶段名是 instruments/vocals —— 匹配不到导致需求恒为 0，
        于是永远退回 CPU，GPU 加速形同虚设。

        这里校验的是**真实**编排脚本（fake 脚本没有 STAGES 声明）。
        """
        real_script = Path("/root/projects/audio-analysis/analyze_audio.py")
        if not real_script.is_file():
            self.skipTest("真实编排脚本不在本机")
        text = real_script.read_text(encoding="utf-8", errors="replace")
        self.assertIn("STAGES = [", text, "脚本应声明 STAGES")
        block = text.split("STAGES = [", 1)[1].split("]", 1)[0]
        script_stages = {item.strip().strip('"').strip("'")
                          for item in block.replace("\n", " ").split(",")
                          if item.strip()}
        script_stages = {s for s in script_stages if s}
        self.assertTrue(script_stages, "应能从脚本解析出阶段名")
        # 每个脚本阶段都要在显存表里有条目
        for stage in script_stages:
            self.assertIn(stage, audio_deep.STAGE_VRAM_MB,
                          f"显存表缺少脚本阶段「{stage}」")
        # 反向：显存表里不能有脚本不认识的键（子引擎名就是这种错）
        for stage in audio_deep.STAGE_VRAM_MB:
            self.assertIn(stage, script_stages,
                          f"显存表里的「{stage}」不是脚本的阶段名"
                          "（可能是子引擎名，会导致显存需求算成 0）")

    def test_gpu_needed_for_heavy_stages(self):
        """instruments/vocals/asr 这三个重阶段必须真的要求显存。"""
        for stage in ("instruments", "vocals", "asr"):
            self.assertGreater(audio_deep.STAGE_VRAM_MB[stage], 0,
                               f"{stage} 是 GPU 阶段，显存需求不能是 0")
        for stage in ("bpm", "llm"):
            self.assertEqual(audio_deep.STAGE_VRAM_MB[stage], 0,
                             f"{stage} 是纯 CPU 阶段")


# ------------------------------------------------------------------ 5. 异步任务


class TestAsyncAnalysisJob(DeepAudioTestBase):
    """触发 -> 轮询 -> 完成的完整状态流转。"""

    def test_start_analysis_returns_job_immediately(self):
        src = self.make_wav()
        asset = self.register(src)
        job = analysis_jobs.start_analysis(
            asset["id"], "audio", mode="analyze",
            options={"deep": True, "stages": ["bpm"]})
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["kind"], "audio")
        self.assertEqual(job["mode"], "analyze")
        self.assertIsNotNone(job["id"])

    def test_job_reaches_done_and_writes_analysis(self):
        src = self.make_wav()
        asset = self.register(src)
        job = analysis_jobs.start_analysis(
            asset["id"], "audio", mode="analyze",
            options={"deep": True, "stages": ["bpm"]})
        # 等后台线程跑完
        deadline = time.time() + 60
        while time.time() < deadline:
            with self.conn() as conn:
                current = asset_repo.get_job(conn, job["id"])
            if current and current["status"] in ("done", "failed"):
                break
            time.sleep(0.2)
        with self.conn() as conn:
            current = asset_repo.get_job(conn, job["id"])
            analyses = asset_repo.analyses_of(conn, asset["id"])
        self.assertEqual(current["status"], "done",
                         f"任务应完成，实际 message={current.get('message')}")
        self.assertEqual(current["progress"], 1.0)
        # 结果落到 asset_analyses —— 与图片/视频的分析文本同一处存储
        self.assertTrue(analyses, "应写入分析记录")
        record = analyses[0]
        self.assertEqual(record["kind"], "audio")
        self.assertIn("节拍与速度", record["text"])
        self.assertEqual(record["extra"]["analyzer_mode"], "deep_audio_pipeline")
        self.assertTrue(record["extra"]["source_untouched"])

    def test_failed_job_is_recorded(self):
        src = self.make_wav()
        asset = self.register(src)
        # 让脚本直接不存在 -> 任务应失败而不是静默
        os.environ["H3_AUDIO_ANALYZER_SCRIPT"] = "/nonexistent/x.py"
        job = analysis_jobs.start_analysis(
            asset["id"], "audio", mode="analyze",
            options={"deep": True})
        deadline = time.time() + 30
        while time.time() < deadline:
            with self.conn() as conn:
                current = asset_repo.get_job(conn, job["id"])
            if current and current["status"] in ("done", "failed"):
                break
            time.sleep(0.2)
        with self.conn() as conn:
            current = asset_repo.get_job(conn, job["id"])
        self.assertEqual(current["status"], "failed")
        self.assertTrue(current["message"])

    def test_active_job_blocks_duplicate(self):
        src = self.make_wav()
        asset = self.register(src)
        os.environ["FAKE_SLEEP"] = "5"
        self.addCleanup(os.environ.pop, "FAKE_SLEEP", None)
        analysis_jobs.start_analysis(asset["id"], "audio",
                                     options={"deep": True, "stages": ["bpm"]})
        time.sleep(0.6)
        with self.conn() as conn:
            active = asset_repo.active_job_of(conn, asset["id"])
        self.assertIsNotNone(active, "在跑的任务应能被查到")

    def test_job_status_payload_shape(self):
        src = self.make_wav()
        asset = self.register(src)
        analysis_jobs.start_analysis(asset["id"], "audio",
                                     options={"deep": True, "stages": ["bpm"]})
        with self.conn() as conn:
            data = analysis_jobs.job_status(conn, asset["id"])
        self.assertIn(data["status"], ("queued", "running", "done", "failed"))
        self.assertIn("job", data)
        self.assertIn("history", data)
        self.assertIsInstance(data["history"], list)


# ------------------------------------------------------------------ 6. 接口级


class TestDeepAudioApi(DeepAudioTestBase):
    """HTTP 接口层。"""

    def _client(self):
        from aiohttp.test_utils import TestClient, TestServer
        import server as server_mod

        class _Fake:
            pass

        app = server_mod.make_app() if hasattr(server_mod, "make_app") else None
        if app is None:
            self.skipTest("server.make_app 不可用")
        return app

    def test_analyze_endpoint_returns_job(self):
        import aiohttp
        from aiohttp.test_utils import TestClient, TestServer
        import server as server_mod

        src = self.make_wav()
        asset = self.register(src)
        app = server_mod.make_app()

        async def run():
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    f"/api/assets/{asset['id']}/analyze",
                    json={"deep": True, "stages": ["bpm"]})
                self.assertEqual(resp.status, 200)
                data = await resp.json()
                self.assertIn("job_id", data)
                self.assertIn(data["status"], ("queued", "running", "done"))
                # 轮询接口可用
                resp2 = await client.get(
                    f"/api/assets/{asset['id']}/analysis-status")
                self.assertEqual(resp2.status, 200)
                data2 = await resp2.json()
                self.assertIn("status", data2)
                self.assertIn("history", data2)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
