# -*- coding: utf-8 -*-
"""R7（2026-11-19 第 7 轮审查）：五个真实缺陷的回归测试。

【本轮发现的问题清单（全部本轮新发现）】
  B1【严重】显式放行标记 allow_twopass 在服务端归一时被丢弃
      -> 用户勾选（并二次确认）的「接续段也用二采」**没有生效**，
         而归一说明还谎称「按你的显式设置走了二采路径」。
  B2【严重】参考视频帧数 ref_video_frame_count 沿链丢失
      -> 2026-11-19 的「参考视频按 C_n 跳帧」修复在**真实接续链上
         等于没生效**（第 3 轮那个「亮度阶跃」缺陷的触发条件仍成立）。
  B3【严重】产物回写把加速 LoRA 写成**权重文件名**而不是目录 key
      -> retry 出来的工作流**一个加速 LoRA 节点都没有**，
         但步数仍是蒸馏档（8 步）=> 8 步采样的非蒸馏权重
         = 「一条不存在的路径」，且**全程不报错**。
  B4【中等】畸形 continuation 参数把 HTTP 打成 500
      （context_frames / anchor_frames / duration 直接 int()/float()）。
  B5【中等】前端把「nonebot」「BasicScheduler」这类内部黑话
      显示给用户。

【断言纪律】
  * 期望值全是**字面量**，不从被测函数反推。
  * 每条断言由「命名该行为的那条断言」抓住。
  * 断言**不自指**。
  * 修复前必须红（见 tests/mutation_r7_review_fixes.py）。

运行：python3 -m pytest tests/test_r7_review_fixes.py -v
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server as srv                                    # noqa: E402
from h3studio import artifact_meta as am                # noqa: E402
from h3studio import continuation as cont              # noqa: E402
from h3studio import h3_video_modes as h3m             # noqa: E402
from h3studio import twopass_policy as tp              # noqa: E402
from h3studio import video_models as vmodels           # noqa: E402
from h3studio import video_workflow as vworkflow       # noqa: E402


class _TempServerMixin:
    """把 server 的数据目录指到临时目录，不碰真实库。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="h3r7-"))
        self._orig = {
            "DATA": srv.DATA, "OUT_DIR": srv.OUT_DIR,
            "VIDEO_DIR": srv.VIDEO_DIR, "UPLOAD_DIR": srv.UPLOAD_DIR,
            "IMAGE_DIR": srv.IMAGE_DIR, "AUDIO_DIR": srv.AUDIO_DIR,
            "DB_PATH": srv.DB_PATH,
        }
        srv.DATA = self.tmp
        srv.OUT_DIR = self.tmp / "outputs"
        srv.VIDEO_DIR = srv.OUT_DIR / "videos"
        srv.UPLOAD_DIR = self.tmp / "uploads"
        srv.IMAGE_DIR = srv.OUT_DIR / "images"
        srv.AUDIO_DIR = srv.OUT_DIR / "audio"
        srv.DB_PATH = self.tmp / "jobs.db"
        for d in (srv.OUT_DIR, srv.VIDEO_DIR, srv.UPLOAD_DIR,
                  srv.IMAGE_DIR, srv.AUDIO_DIR):
            d.mkdir(parents=True, exist_ok=True)
        srv.init_db()

    def tearDown(self):
        srv.DATA = self._orig["DATA"]
        srv.OUT_DIR = self._orig["OUT_DIR"]
        srv.VIDEO_DIR = self._orig["VIDEO_DIR"]
        srv.UPLOAD_DIR = self._orig["UPLOAD_DIR"]
        srv.IMAGE_DIR = self._orig["IMAGE_DIR"]
        srv.AUDIO_DIR = self._orig["AUDIO_DIR"]
        srv.DB_PATH = self._orig["DB_PATH"]
        shutil.rmtree(self.tmp, ignore_errors=True)


def _base_params(**extra):
    base = {
        "prompt": "p", "model_key": "fl2va_pruned_int8",
        "width": 1088, "height": 1920, "duration": 5,
        "steps": 8, "sampler": "euler", "scheduler": "beta",
        "accel_lora": "dmad_4step_critic", "accel_lora_strength": 1.0,
        "accel_loras": [{"key": "dmad_4step_critic", "strength": 1.0,
                         "enabled": True}],
    }
    base.update(extra)
    return base


# ======================================================================
# B1：allow_twopass 在归一时被丢弃
# ======================================================================
class AllowTwopassPreservedTest(_TempServerMixin, unittest.TestCase):
    """用户显式勾选（并二次确认）的「接续段也用二采」必须真的生效。"""

    def _chain(self) -> tuple[str, str]:
        seg1 = srv.create_job_record("t2v", _base_params())
        run_id = cont.segment_run_id(seg1)
        stage = srv.cont_service.stage_path(run_id, 0)
        stage.parent.mkdir(parents=True, exist_ok=True)
        stage.write_bytes(b"fake-latent")
        return seg1, run_id

    def test_allow_twopass_survives_server_normalization(self):
        """核心断言：归一后 continuation 里必须还有 allow_twopass=True。"""
        seg1, _ = self._chain()
        params = _base_params(twopass={"enabled": True})
        params["continuation"] = {
            "source_job": seg1, "stage_index": 1,
            "allow_twopass": True,
        }
        err = srv._prepare_continuation_params(params)
        self.assertEqual(err, "", "接续参数准备不该报错")
        self.assertIs(params["continuation"].get("allow_twopass"), True,
                      "allow_twopass 被服务端归一丢弃了 —— 用户勾的「接续段"
                      "也用二采」等于没勾")

    def test_workflow_layer_sees_warn_not_deny(self):
        """工作流层必须看到 warn（= 真的走二采），而不是 deny。"""
        seg1, _ = self._chain()
        params = _base_params(twopass={"enabled": True})
        params["continuation"] = {
            "source_job": seg1, "stage_index": 1, "allow_twopass": True,
        }
        srv._prepare_continuation_params(params)
        use, disposition = tp.resolve_effective_twopass(params)
        self.assertTrue(use, "显式放行后必须真的走二采")
        self.assertEqual(disposition, "warn",
                         "显式放行的处置方式必须是 warn（带警告放行）")

    def test_no_allow_means_still_denied(self):
        """反向断言：没勾就仍然是 deny（不能被 F1 顺手放开）。"""
        seg1, _ = self._chain()
        params = _base_params(twopass={"enabled": True})
        params["continuation"] = {"source_job": seg1, "stage_index": 1}
        srv._prepare_continuation_params(params)
        use, disposition = tp.resolve_effective_twopass(params)
        self.assertFalse(use, "没显式勾选时接续段仍必须禁用二采")
        self.assertEqual(disposition, "deny")


# ======================================================================
# B2：ref_video_frame_count 沿链丢失
# ======================================================================
class RefVideoFrameCountInheritedTest(_TempServerMixin, unittest.TestCase):
    """参考视频帧数必须沿链继承，第 2 段才能按 C_n 跳帧。"""

    def _chain_with_ref(self) -> tuple[str, dict]:
        seg1 = srv.create_job_record("r2v", _base_params(ref_videos=["r.mp4"]))
        run_id = cont.segment_run_id(seg1)
        stage = srv.cont_service.stage_path(run_id, 0)
        stage.parent.mkdir(parents=True, exist_ok=True)
        stage.write_bytes(b"fake-latent")
        with srv.db() as conn:
            row = conn.execute("SELECT params FROM jobs WHERE id=?",
                               (seg1,)).fetchone()
            p1 = json.loads(row["params"])
            p1["continuation"]["ref_video_frame_count"] = 300
            conn.execute("UPDATE jobs SET params=? WHERE id=?",
                         (json.dumps(p1, ensure_ascii=False), seg1))
        return seg1, p1

    def test_context_carries_ref_video_frame_count(self):
        """核心断言：接续上下文必须下发参考视频帧数。"""
        seg1, p1 = self._chain_with_ref()
        ctx = srv.cont_service.build_continuation_context("r2v", p1, seg1)
        self.assertEqual(ctx["continuation"].get("ref_video_frame_count"), 300,
                         "接续上下文没下发参考视频帧数 -> 第 2 段无法按 C_n 跳帧")

    def test_second_segment_inherits_frame_count(self):
        """核心断言：第 2 段归一后必须继承上游探测到的帧数。"""
        seg1, p1 = self._chain_with_ref()
        ctx = srv.cont_service.build_continuation_context("r2v", p1, seg1)
        params = _base_params(ref_videos=["r.mp4"])
        params["continuation"] = dict(ctx["continuation"])
        err = srv._prepare_continuation_params(params)
        self.assertEqual(err, "", "接续参数准备不该报错")
        self.assertEqual(
            params["continuation"].get("ref_video_frame_count"), 300,
            "第 2 段没继承参考视频帧数 -> 参考视频又从 0 开始喂")

    def test_second_segment_actually_skips_frames(self):
        """后果级断言：工作流层算出的画面跳帧数必须等于 C_n。"""
        seg1, p1 = self._chain_with_ref()
        ctx = srv.cont_service.build_continuation_context("r2v", p1, seg1)
        params = _base_params(ref_videos=["r.mp4"])
        params["continuation"] = dict(ctx["continuation"])
        srv._prepare_continuation_params(params)
        slice_info = h3m._continuation_media_slice(params)
        chain_total = params["continuation"].get("chain_total_frames")
        self.assertGreater(int(chain_total or 0), 0,
                            "第 2 段的 C_n 必须 > 0（否则这条断言无意义）")
        self.assertEqual(slice_info.get("video_skip_frames"),
                         int(chain_total),
                         "第 2 段的参考视频画面跳帧数必须等于 C_n —— "
                         "2026-11-19 的「画面切片与音轨解耦」修复在真实链上生效")

    def test_unknown_frame_count_is_not_guessed(self):
        """反向断言：探测不到帧数时仍然不猜（保持 0）。"""
        seg1 = srv.create_job_record("r2v", _base_params(ref_videos=["r.mp4"]))
        run_id = cont.segment_run_id(seg1)
        stage = srv.cont_service.stage_path(run_id, 0)
        stage.parent.mkdir(parents=True, exist_ok=True)
        stage.write_bytes(b"fake-latent")
        ctx = srv.cont_service.build_continuation_context(
            "r2v", {"continuation": {
                "chain_id": seg1, "stage_index": 0, "source_job": "",
                "source_segment": -1, "latent_run_id": run_id,
                "latent_stage": 0, "context_frames": 22, "anchor_frames": 22,
                "chain_total_frames": 0, "segment_frames": 124,
            }}, seg1)
        params = _base_params(ref_videos=["r.mp4"])
        params["continuation"] = dict(ctx["continuation"])
        srv._prepare_continuation_params(params)
        slice_info = h3m._continuation_media_slice(params)
        self.assertEqual(slice_info.get("video_skip_frames"), 0,
                         "探测不到帧数时不得猜帧数")


# ======================================================================
# B3：产物回写把加速 LoRA 写成权重文件名
# ======================================================================
class ArtifactAccelKeyTest(unittest.TestCase):
    """回写的加速 LoRA 必须是**目录 key**（工作流层认得的那个）。"""

    def _extracted_dmad(self) -> dict:
        return {
            "steps": 8, "scheduler": "beta", "sampler": "euler", "cfg": 1.0,
            "seed": 7, "width": 1088, "height": 1920,
            "model_file": "minimax_h3_fl2va_pruned_int8.safetensors",
            "loras": [{
                "name": "dmad_minimax_h3_4step_full_critic_comfyui",
                "raw_name": "dmad_minimax_h3_4step_full_critic_comfyui.safetensors",
                "strength": 1.0, "loader": "LoraLoaderModelOnly",
            }],
        }

    def test_accel_lora_is_the_catalog_key(self):
        """核心断言：accel_lora 必须是清单里的 key，不是权重文件名。"""
        params: dict = {"prompt": "x"}
        am.apply_to_params(params, self._extracted_dmad())
        self.assertEqual(params.get("accel_lora"), "dmad_4step_critic",
                         "accel_lora 必须是目录 key（工作流层认得的那个），"
                         "不能是权重文件名")
        self.assertEqual(params.get("fixed_accel_lora"), "dmad_4step_critic")
        self.assertEqual(params["accel_loras"][0]["key"], "dmad_4step_critic")

    def test_accel_lora_resolves_to_a_real_lora(self):
        """后果级断言：回写后的值必须能被加速 LoRA 清单认出来。"""
        params: dict = {"prompt": "x"}
        am.apply_to_params(params, self._extracted_dmad())
        self.assertIsNotNone(
            vmodels.accel_lora_meta(str(params.get("accel_lora") or "")),
            "回写后的 accel_lora 认不出来 -> 工作流会静默不挂加速 LoRA")

    def test_workflow_layer_still_resolves_the_chain(self):
        """后果级断言：工作流层必须仍能解析出那枚加速 LoRA。"""
        params: dict = {"prompt": "x"}
        am.apply_to_params(params, self._extracted_dmad())
        chain = vworkflow._resolve_accel_chain(params, {"unet": "x.safetensors"})
        self.assertTrue(chain,
                        "回写之后工作流层解析不出加速 LoRA 链 -> "
                        "retry 出来的工作流不挂任何加速 LoRA")

    def test_legacy_weight_name_slot_is_still_resolved(self):
        """**存量数据**（accel_lora 存的是权重文件名）必须仍能解析。

        【为什么这条独立】F5 让新回写的数据写的是目录 key；
        但线上**已经落库**的 9 条 r2v 任务里，
        `params["accel_lora"]` 存的是权重文件名
        （dmad_minimax_h3_4step_full_critic_comfyui）。
        工作流层若不归一，retry 这批任务就静默不挂加速 LoRA。
        """
        legacy = {"accel_lora": "dmad_minimax_h3_4step_full_critic_comfyui",
                  "accel_lora_strength": 1.0, "steps": 8}
        key, _ = vmodels.resolve_accel_lora_slot(legacy)
        self.assertEqual(key, "dmad_4step_critic",
                         "存量数据里的权重文件名必须被归一成目录 key")
        chain = vworkflow._resolve_accel_chain(legacy,
                                               {"unet": "x.safetensors"})
        self.assertTrue(chain,
                        "存量数据（权重名形态）解析不出加速 LoRA 链 -> "
                        "retry 这批任务会静默不挂加速 LoRA")

    def test_unknown_weight_does_not_write_a_broken_slot(self):
        """反向断言：认不出来的权重**不得**写进槽位（会静默挂掉 LoRA）。"""
        params: dict = {"prompt": "x"}
        am.apply_to_params(params, {
            "steps": 8, "scheduler": "beta", "sampler": "euler",
            "loras": [{"name": "totally_unknown_lora",
                       "raw_name": "totally_unknown_lora.safetensors",
                       "strength": 1.0, "loader": "LoraLoaderModelOnly"}],
        })
        self.assertNotEqual(params.get("accel_lora"),
                            "totally_unknown_lora",
                            "认不出来的权重名不得写进 accel_lora 槽位")
        self.assertNotIn("accel_lora", params,
                         "认不出来的权重不得覆盖已有的 accel_lora 槽位")


# ======================================================================
# B4：畸形 continuation 参数不得打成 500
# ======================================================================
class MalformedContinuationTest(_TempServerMixin, unittest.TestCase):
    """畸形参数必须回落到默认值，而不是抛异常。"""

    def _chain(self) -> str:
        seg1 = srv.create_job_record("t2v", _base_params())
        run_id = cont.segment_run_id(seg1)
        stage = srv.cont_service.stage_path(run_id, 0)
        stage.parent.mkdir(parents=True, exist_ok=True)
        stage.write_bytes(b"fake-latent")
        return seg1

    def _prepare(self, cont_extra: dict, **over) -> str:
        seg1 = self._chain()
        params = _base_params(**over)
        params["continuation"] = {"source_job": seg1, "stage_index": 1}
        params["continuation"].update(cont_extra)
        return srv._prepare_continuation_params(params)

    def test_string_context_frames_does_not_raise(self):
        """context_frames 传字符串不得抛 ValueError。"""
        err = self._prepare({"context_frames": "abc"})
        self.assertEqual(err, "", "畸形 context_frames 应回落到默认值而不是报错")

    def test_list_context_frames_does_not_raise(self):
        """context_frames 传数组不得抛 TypeError。"""
        err = self._prepare({"context_frames": [1, 2]})
        self.assertEqual(err, "")

    def test_string_anchor_frames_does_not_raise(self):
        """anchor_frames 传字符串不得抛 ValueError。"""
        err = self._prepare({"anchor_frames": "xyz"})
        self.assertEqual(err, "")

    def test_string_duration_does_not_raise(self):
        """duration 传字符串不得抛 ValueError。"""
        err = self._prepare({}, duration="abc")
        self.assertEqual(err, "")

    def test_malformed_falls_back_to_default_window(self):
        """后果级断言：畸形值必须回落到默认钉住窗口（不是 0）。"""
        seg1 = self._chain()
        params = _base_params()
        params["continuation"] = {"source_job": seg1, "stage_index": 1,
                                  "context_frames": "abc"}
        srv._prepare_continuation_params(params)
        self.assertEqual(params["continuation"]["context_frames"],
                         cont.DEFAULT_CONTEXT_FRAMES,
                         "畸形 context_frames 必须回落到默认值")


# ======================================================================
# B5：前端可见文案里的内部黑话
# ======================================================================
class FrontendVisibleTextHygieneTest(unittest.TestCase):
    """前端不得把内部工程黑话显示给用户。

    【口径 —— 用真解析器，不用行扫描】
    行扫描会把「行尾 // 注释」「HTML 注释块」当成可见文本（假阳性），
    也可能漏掉跨行的字符串拼接。
    这里走 scripts/r7_extract_visible_strings.cjs：
      * JS  -> acorn 真 AST（注释不在 AST 里）=> Literal / TemplateElement
        就是**会被送进 DOM 的静态文本**；
      * HTML -> 去掉注释块 + <script>/<style> 整块，再抓文本节点与
        title / placeholder / aria-label / alt / value 属性值。
    """

    _CACHE: dict = {}

    @classmethod
    def _visible(cls) -> dict:
        if cls._CACHE:
            return cls._CACHE
        script = ROOT / "scripts" / "r7_extract_visible_strings.cjs"
        if not script.exists():
            raise unittest.SkipTest("缺少 scripts/r7_extract_visible_strings.cjs")
        import subprocess
        proc = subprocess.run(
            ["node", str(script), str(ROOT / "web")],
            capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            raise unittest.SkipTest(
                f"无法抽取可见字符串（node/acorn 不可用?）：{proc.stderr[-300:]}")
        try:
            cls._CACHE = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise unittest.SkipTest(f"可见字符串抽取结果不是合法 JSON：{exc}")
        return cls._CACHE

    @classmethod
    def _all_visible_strings(cls) -> list[tuple[str, str]]:
        data = cls._visible()
        out: list[tuple[str, str]] = []
        for group in ("js", "html"):
            for fname, texts in (data.get(group) or {}).items():
                for text in texts:
                    out.append((f"{group}/{fname}", text))
        return out

    def test_extractor_is_not_vacuous(self):
        """护栏：抽取器本身不能空跑（否则下面几条断言形同虚设）。"""
        rows = self._all_visible_strings()
        self.assertGreater(len(rows), 500,
                           f"只抽到 {len(rows)} 条可见字符串 —— 抽取器失效")

    def test_no_nonebot_in_user_visible_strings(self):
        """「nonebot」是第三方插件名，不该出现在用户界面文案里。"""
        offenders = [(f, t) for f, t in self._all_visible_strings()
                     if "nonebot" in t]
        self.assertFalse(offenders,
                         "前端可见文案里出现了内部黑话 'nonebot': "
                         f"{offenders[:5]}")

    def test_no_comfy_node_class_names_in_user_visible_strings(self):
        """「BasicScheduler」「KSamplerSelect」是节点类名，不该显示给用户。"""
        node_names = ("BasicScheduler", "KSamplerSelect",
                      "SamplerCustomAdvanced", "SelfLiftH3Sampler",
                      "MiniMaxH3SigmaShift")
        offenders = [(f, t) for f, t in self._all_visible_strings()
                     if any(n in t for n in node_names)]
        self.assertFalse(offenders,
                         "前端可见文案里出现了节点类名: {offenders[:5]}")

    def test_no_workflow_source_code_in_recommendation_source(self):
        """「出处」不得夹带源码片段（如 default_steps = 4 if ...）。"""
        from h3studio import model_recommendations as mrec
        pattern = re.compile(r"default_steps|default_scheduler|default_sampler"
                             r"\s*=|lora\s*==\s*['\"]")
        offenders = []
        for key, rec in mrec.model_recommendations_payload().items():
            for field in ("source", "note"):
                text = str(rec.get(field) or "")
                if pattern.search(text):
                    offenders.append(f"model.{key}.{field}: {text[:120]}")
        for key, rec in mrec.accel_lora_recommendations_payload().items():
            for field in ("source", "note"):
                text = str(rec.get(field) or "")
                if pattern.search(text):
                    offenders.append(f"accel.{key}.{field}: {text[:120]}")
        self.assertFalse(offenders,
                         "「出处」/「说明」里夹带了源码片段: {offenders}")


class BackendVisibleTextHygieneTest(unittest.TestCase):
    """【第 7 轮追加】后端返回给前端的**说明文案**也不得含内部黑话。

    【为什么单独一个类】
    上一版的 `FrontendVisibleTextHygieneTest` 只扫 `web/` 的**静态**字符串。
    但后端也会把说明文案发给前端渲染（`/api/video-options` 的
    `cfg_fixed_reason`、`twopass.note` …）—— 那些**不在 web/ 里**，
    静态扫描抓不到 ⇒ 第 7 轮第一轮就漏掉了 3 条，部署后 DOM 复审才抓到。

    【判定口径】
    只查**会被用户看到的文案**：
      * 后端模块里的中文展示文案（VIDEO_TWOPASS["note"] /
        VIDEO_CFG_FIXED_REASON / recommendations 的 source+note）
      * `node_available("SelfLiftH3Sampler")` 这类**节点类名参数**不算 ——
        前端拿去查本机节点可用性，不会显示给用户
    """

    #: 会显示给用户的节点类名黑话
    NODE_CLASS_NAMES = ("BasicScheduler", "KSamplerSelect",
                        "SamplerCustomAdvanced", "SelfLiftH3Sampler",
                        "MiniMaxH3SigmaShift", "BasicGuider", "SelfLift")
    #: 内部术语
    JARGON = ("潜空间放大器", "nonebot")

    def _backend_user_facing_strings(self) -> list[tuple[str, str]]:
        """收集后端会发给前端、且**会被显示**的文案。"""
        from h3studio import video_models as vm
        out: list[tuple[str, str]] = []
        out.append(("VIDEO_TWOPASS.note", str(vm.VIDEO_TWOPASS.get("note") or "")))
        out.append(("VIDEO_CFG_FIXED_REASON", str(vm.VIDEO_CFG_FIXED_REASON or "")))
        # 面板配置里所有会直接渲染的字符串字段
        for field in ("cfg_fixed_reason", "note", "hint", "tip", "desc",
                      "description", "label", "title"):
            val = vm.VIDEO_TWOPASS.get(field)
            if isinstance(val, str):
                out.append((f"VIDEO_TWOPASS.{field}", val))
        return [(k, v) for k, v in out if v]

    def test_backend_note_has_no_node_class_names(self):
        """后端说明文案不得出现 ComfyUI 节点类名。"""
        offenders = [(k, t) for k, t in self._backend_user_facing_strings()
                     if any(n in t for n in self.NODE_CLASS_NAMES)]
        self.assertFalse(
            offenders,
            "后端发给前端的说明文案里出现了节点类名: "
            f"{[(k, t[:120]) for k, t in offenders]}")

    def test_backend_note_has_no_internal_jargon(self):
        """后端说明文案不得出现内部术语（潜空间放大器 / nonebot）。"""
        offenders = [(k, t) for k, t in self._backend_user_facing_strings()
                     if any(j in t for j in self.JARGON)]
        self.assertFalse(
            offenders,
            "后端发给前端的说明文案里出现了内部术语: "
            f"{[(k, t[:120]) for k, t in offenders]}")

    def test_frontend_html_has_no_internal_jargon(self):
        """web/ 的静态 HTML 文本也不得出现「潜空间放大器」这类内部术语。"""
        idx = ROOT / "web" / "index.html"
        raw = idx.read_text(encoding="utf-8")
        # 剥离 HTML 注释块后再查（注释不是用户可见文案）
        import re as _re
        stripped = _re.sub(r"<!--.*?-->", "", raw, flags=_re.S)
        for term in self.JARGON:
            self.assertNotIn(
                term, stripped,
                f"web/index.html 的可见文本里出现了内部术语 {term!r}")

    def test_backend_payload_really_reaches_frontend(self):
        """护栏：确认上面查的这些文案**真的会**发给前端。

        【为什么】如果这些字段其实没被任何接口返回，那上面的断言就是
        「对着一段死代码断言」—— 修复它也不会改变用户看到的界面。
        这条断言锁住「它们确实在载荷里」。
        """
        from h3studio import video_models as vm
        payload = vm.options_payload()
        self.assertIsInstance(payload, dict)
        blob = json.dumps(payload, ensure_ascii=False, default=str)
        self.assertIn("cfg_fixed_reason", blob,
                      "/api/video-options 载荷里没有 cfg_fixed_reason —— "
                      "上面的断言就只是在检查死代码")
        self.assertIn("twopass", blob)
        # 载荷里的这些文案同样不得含黑话（端到端口径）
        offenders = [(k, t) for k, t in self._backend_user_facing_strings()
                     if any(n in t for n in self.NODE_CLASS_NAMES)
                     or any(j in t for j in self.JARGON)]
        self.assertFalse(offenders, f"载荷文案仍含黑话: {offenders}")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
