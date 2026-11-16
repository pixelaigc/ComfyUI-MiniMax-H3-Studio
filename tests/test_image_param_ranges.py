# -*- coding: utf-8 -*-
"""图像族采样参数量程回归测试（2026-11-16 用户报的 MOSS-Image BUG）。

【用户报的 BUG】
「文生图 → MOSS-Image」面板把「采样步数」拖到 100、数值框显示 100，
但生成队列里的参数卡显示「步数/CFG 50 / 3」—— 实际跑的是默认 50 步。

【根因】
后端归一层 `h3studio/gen_normalize.py` 用一套**扁平的全局区间**
（IMAGE_STEPS_MAX=60 / IMAGE_CFG_MIN=1.0 / IMAGE_SHIFT_MIN=1.0 /
 IMAGE_SHIFT_MAX=7.0）去卡**所有**图像族；
而前端滑杆的量程是**按族分别设**的（MOSS: steps 1~100 / cfg 0~10 /
shift 0~10）。于是 MOSS 的 steps=100 被后端判「越界」-> 静默回落
模型推荐值 50，面板显示 100、实际跑 50。

**根因类别 = 「前端允许、后端不允许」** —— 两套量程各写一份，必然漂移。

【本测试锁住的契约】
  R1  各族的合法量程按族取（`IMAGE_PARAM_RANGES`），
      面板允许的值**后端必须原样接受**（不得回落）。
  R2  真正越界的值（999 步）**仍然**回落 + 留说明（加固不放松）。
  R3  区间只有**一份真源**：前端滑杆量程由 /api/image-options 的
      `param_ranges` 下发，前端不得再硬编码另一套。
  R4  Z-Image >=3MP 放大路径的第一段**必须用用户的采样参数**
      （旧实现写死 steps=8 / cfg=1.0）。
  R5  归一说明（normalize_notes）必须**下发到前端** ——
      否则「参数被服务端改过」这件事在界面上是静默的。

【关键纪律 —— 断言必须能抓住回归】
每条断言都用**具体的坏值 / 具体的好值**，不是「不等于 None」这种自指断言。
每条断言都点名**是哪一族**，避免「一族修好、另一族被改坏」。

运行：python3 -m pytest tests/test_image_param_ranges.py -v
"""

import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server as S  # noqa: E402
from h3studio import gen_normalize as gn  # noqa: E402
from h3studio import h3image  # noqa: E402
from h3studio import workflows as W  # noqa: E402


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class TestRangeTableIsSingleSource(unittest.TestCase):
    """R3：区间只有一份真源，且覆盖全部四个族。"""

    def test_all_four_families_are_defined(self):
        """四个族都要有量程 —— 少一族 = 那一族又用回扁平的默认区间。"""
        for family in ("zimage", "qwen", "moss", "h3image"):
            self.assertIn(family, gn.IMAGE_PARAM_RANGES,
                          f"{family} 族没有量程定义")
            ranges = gn.IMAGE_PARAM_RANGES[family]
            for field in ("steps", "cfg", "shift"):
                self.assertIn(field, ranges,
                              f"{family} 族缺 {field} 的量程")
                r = ranges[field]
                self.assertLess(r["min"], r["max"],
                                f"{family}.{field} 的 min 必须小于 max")
                self.assertGreater(r["step"], 0,
                                   f"{family}.{field} 的 step 必须为正")

    def test_model_tables_are_tagged_with_family(self):
        """模型表里的每个模型都要带 param_family 标记。

        【为什么这条重要】归一函数在调用方不传 family 时会读
        model["param_family"]。漏标一条 = 该模型又用回 Z-Image 的
        默认量程 —— 正是本次 BUG 的形态。
        """
        cases = [
            (W.Z_IMAGE_MODELS, "zimage"),
            (W.QWEN_IMAGE_MODELS, "qwen"),
            (W.MOSS_IMAGE_MODELS, "moss"),
            (h3image.H3_IMAGE_MODELS, "h3image"),
        ]
        for table, expected in cases:
            for key, model in table.items():
                self.assertEqual(model.get("param_family"), expected,
                                 f"模型 {key} 的 param_family 标记不对")

    def test_server_exposes_param_ranges_to_frontend(self):
        """/api/image-options 必须下发 param_ranges（前端量程的真源）。"""
        import asyncio
        resp = asyncio.run(S.api_image_options(None))
        # api_image_options 返回的是 aiohttp Response —— 要从它的 body 取 JSON
        payload = json.loads(resp.text)
        ranges = payload.get("param_ranges")
        self.assertIsInstance(ranges, dict, "param_ranges 必须下发")
        self.assertEqual(ranges, gn.IMAGE_PARAM_RANGES,
                         "下发的量程必须与后端真源逐字一致")
        for family in ("zimage", "qwen", "moss", "h3image"):
            self.assertIn(family, ranges)

    def test_frontend_no_longer_hardcodes_ranges(self):
        """前端不得再硬编码滑杆量程（那正是本次 BUG 的另一半）。

        【断言怎么写才抓得住】
        不能只搜 ``stepsEl.max = "100"`` 这种**旧写法** ——
        变异可能换成任何形状的硬编码（比如直接写一个字面量对象）。
        所以这里断言的是**更本质**的东西：
          1. 滑杆的 min/max/step **只能**从服务端下发的量程来
             （即代码里必须经由 imgParamRanges() 拿量程）；
          2. 代码里不得出现任何「把量程写死成字面量」的赋值。
        """
        js = _read("web/app.js")

        # 1) 滑杆量程必须来自 imgParamRanges()（服务端下发）
        self.assertIn("function imgParamRanges(", js,
                      "前端缺少 imgParamRanges() —— 量程没有统一出口")
        self.assertRegex(
            js,
            r"const\s+ranges\s*=\s*imgParamRanges\(\)",
            "滑杆量程必须从 imgParamRanges() 取，不能另起一份")

        # 2) 不得出现「滑杆 min/max/step = 字面量」的任何写法
        offenders = re.findall(
            r'(?:steps|cfg|shift)El\.(?:min|max|step)\s*=\s*'
            r'(?:"[^"]*"|\'[^\']*\'|\d+(?:\.\d+)?)', js)
        self.assertEqual(offenders, [],
                         "前端仍有硬编码量程：%s" % offenders)

        # 3) 也不得把整份量程写成字面量对象绕过 imgParamRanges()
        self.assertNotIn("IMG_PARAM_RANGES_FALLBACK", js,
                         "前端不应再维护一份本地量程表 —— "
                         "两份量程必然漂移（本次 BUG 的根因）")
        # 服务端没下发时，imgParamRanges() 必须返回 null（而不是编一份）
        self.assertRegex(
            js,
            r"function\s+imgParamRanges\(\)[^{]*\{[^}]*return\s+null",
            "服务端没下发量程时 imgParamRanges() 必须返回 null，"
            "不能自己编一份")


class TestPanelValuesSurviveNormalization(unittest.TestCase):
    """R1：面板允许的值，后端必须原样接受（本次 BUG 的核心断言）。"""

    def test_moss_steps_100_is_accepted(self):
        """【本次用户报的 BUG 的最小复现】MOSS steps=100 不得被夹回 50。"""
        params = {"prompt": "x", "model": "moss_image_base",
                  "steps": 100, "cfg": 3, "shift": 5,
                  "sampler": "euler", "scheduler": "sgm_uniform"}
        S.prepare_job_params("t2i_moss", params, [], None)
        self.assertEqual(params["steps"], 100,
                         "MOSS-Image 面板允许 steps=100，后端却夹回了 %r"
                         % params["steps"])
        self.assertNotIn("normalize_notes", params,
                         "合法值不该产生归一说明")

    def test_moss_cfg_and_shift_at_panel_range_edge(self):
        """MOSS 面板 cfg/shift 量程是 0~10 —— 边界值必须被接受。"""
        params = {"prompt": "x", "model": "moss_image_base",
                  "steps": 50, "cfg": 10, "shift": 10}
        S.prepare_job_params("t2i_moss", params, [], None)
        self.assertEqual(params["cfg"], 10.0)
        self.assertEqual(params["shift"], 10.0)
        self.assertNotIn("normalize_notes", params)

    def test_qwen_shift_069_default_is_not_flagged(self):
        """Qwen 内置 shift=0.69 —— 旧代码把它当「越界」（下限 1.0）并回落。"""
        params = {"prompt": "x", "model": "qwen_image_2.1_bf16",
                  "steps": 40, "cfg": 1, "shift": 0.69}
        S.prepare_job_params("t2i_qwen", params, [], None)
        self.assertEqual(params["shift"], 0.69,
                         "Qwen 内置 shift 被归一层改掉了")
        self.assertNotIn("normalize_notes", params)

    def test_qwen_cfg_zero_is_accepted(self):
        """Qwen 面板 CFG 量程 0~8 —— 0 是合法的。"""
        params = {"prompt": "x", "model": "qwen_image_2.1_bf16",
                  "steps": 40, "cfg": 0, "shift": 0.69}
        S.prepare_job_params("t2i_qwen", params, [], None)
        self.assertEqual(params["cfg"], 0.0)

    def test_h3image_shift_zero_is_accepted(self):
        """H3-Image 面板 shift 量程 0~3 —— 0 合法（旧代码下限 1.0 会夹它）。"""
        params = {"prompt": "x", "model": "h3_image_pruned_int8",
                  "steps": 6, "cfg": 1, "shift": 0}
        S.prepare_job_params("t2i_h3image", params, [], None)
        self.assertEqual(params["shift"], 0.0)

    def test_every_family_accepts_its_own_range_top(self):
        """逐族：面板量程的上界值必须被接受。

        【为什么这条最重要】它是「同款缺陷会不会在别的族复发」的总闸门 ——
        任何一族的量程被改窄，这条就红。
        """
        cases = [
            ("t2i", "z_image_bf16", W.Z_IMAGE_MODELS),
            ("t2i_qwen", "qwen_image_2.1_bf16", W.QWEN_IMAGE_MODELS),
            ("t2i_moss", "moss_image_base", W.MOSS_IMAGE_MODELS),
            ("t2i_h3image", "h3_image_pruned_int8", h3image.H3_IMAGE_MODELS),
        ]
        for mode, model_key, table in cases:
            family = table[model_key]["param_family"]
            ranges = gn.IMAGE_PARAM_RANGES[family]
            params = {"prompt": "x", "model": model_key,
                      "steps": ranges["steps"]["max"],
                      "cfg": ranges["cfg"]["max"],
                      "shift": ranges["shift"]["max"]}
            S.prepare_job_params(mode, params, [], None)
            self.assertEqual(params["steps"], ranges["steps"]["max"],
                             "%s 族的面板上界步数被夹了" % family)
            self.assertEqual(params["cfg"], ranges["cfg"]["max"],
                             "%s 族的面板上界 CFG 被夹了" % family)
            self.assertEqual(params["shift"], ranges["shift"]["max"],
                             "%s 族的面板上界 shift 被夹了" % family)


class TestOutOfRangeStillClamped(unittest.TestCase):
    """R2：加固不能放松 —— 真正越界的值仍然回落 + 留说明。"""

    def test_moss_steps_999_still_falls_back(self):
        params = {"prompt": "x", "model": "moss_image_base", "steps": 999}
        S.prepare_job_params("t2i_moss", params, [], None)
        model = W.MOSS_IMAGE_MODELS["moss_image_base"]
        self.assertEqual(params["steps"], model["steps"])
        self.assertTrue(params.get("normalize_notes"),
                        "越界值必须留下「改了什么」的说明")

    def test_moss_negative_steps_still_falls_back(self):
        params = {"prompt": "x", "model": "moss_image_base", "steps": -5}
        S.prepare_job_params("t2i_moss", params, [], None)
        model = W.MOSS_IMAGE_MODELS["moss_image_base"]
        self.assertEqual(params["steps"], model["steps"])

    def test_illegal_sampler_still_falls_back(self):
        """R2 的采样器侧：白名单校验不能因为改了量程而放松。"""
        params = {"prompt": "x", "model": "moss_image_base",
                  "sampler": "根本不存在的采样器"}
        S.prepare_job_params("t2i_moss", params, [], None)
        model = W.MOSS_IMAGE_MODELS["moss_image_base"]
        self.assertEqual(params["sampler"], model["sampler"])


class TestFamilyIsolation(unittest.TestCase):
    """一族放宽不能放宽另一族（否则 Z-Image 的越界保护就没了）。"""

    def test_moss_range_does_not_relax_zimage(self):
        """MOSS 允许 steps=100，Z-Image 仍然只允许 <=60。"""
        params = {"prompt": "x", "model": "z_image_bf16", "steps": 100}
        S.prepare_job_params("t2i", params, [], None)
        model = W.Z_IMAGE_MODELS["z_image_bf16"]
        self.assertEqual(params["steps"], model["steps"],
                         "Z-Image 的步数上限被 MOSS 的量程放宽了")

    def test_zimage_range_does_not_relax_moss(self):
        """反向：Z-Image 的上限 60 不该成为 MOSS 的上限。"""
        params = {"prompt": "x", "model": "moss_image_base", "steps": 100}
        S.prepare_job_params("t2i_moss", params, [], None)
        self.assertEqual(params["steps"], 100)

    def test_h3image_twelve_is_accepted_but_thirteen_is_not(self):
        """H3-Image 上限 12（官方 6 步蒸馏的对照空间）。"""
        ok = {"prompt": "x", "model": "h3_image_pruned_int8", "steps": 12}
        S.prepare_job_params("t2i_h3image", ok, [], None)
        self.assertEqual(ok["steps"], 12)

        bad = {"prompt": "x", "model": "h3_image_pruned_int8", "steps": 13}
        S.prepare_job_params("t2i_h3image", bad, [], None)
        self.assertNotEqual(bad["steps"], 13,
                            "H3-Image 的步数上限 12 被放宽了")


class TestZImageUpscaleUsesUserParams(unittest.TestCase):
    """R4：Z-Image >=3MP 放大路径的第一段必须用用户的采样参数。

    【旧实现的缺陷】放大路径内部把「57:3」节点写死 steps=8 / cfg=1.0 /
    res_multistep / simple，用户的步数 / CFG / 采样器 / 调度器 / shift
    在 >=3MP 档位上**全部无效**（面板显示 30 步，实际跑 8 步）。
    """

    def _upscale_workflow(self, **overrides):
        params = {"job_id": "probe", "model": "z_image_bf16",
                  "steps": 30, "cfg": 4.0, "sampler": "euler",
                  "scheduler": "simple", "shift": 3.0,
                  "resolution": "2k", "aspect": "16:9",
                  "seed": 1, "prompt": "x"}
        params.update(overrides)
        return W.build_t2i(params)

    def test_upscale_first_stage_uses_user_steps(self):
        wf = self._upscale_workflow()
        first = wf["57:3"]["inputs"]
        self.assertEqual(first["steps"], 30,
                         "放大路径第一段没用用户的步数（仍是写死的 8）")

    def test_upscale_first_stage_uses_user_cfg(self):
        wf = self._upscale_workflow()
        self.assertEqual(wf["57:3"]["inputs"]["cfg"], 4.0,
                         "放大路径第一段没用用户的 CFG（仍是写死的 1.0）")

    def test_upscale_first_stage_uses_user_sampler_and_scheduler(self):
        wf = self._upscale_workflow()
        first = wf["57:3"]["inputs"]
        self.assertEqual(first["sampler_name"], "euler")
        self.assertEqual(first["scheduler"], "simple")

    def test_upscale_first_stage_uses_user_shift(self):
        wf = self._upscale_workflow(shift=5.0)
        self.assertEqual(wf["57:11"]["inputs"]["shift"], 5.0,
                         "放大路径没用用户的 shift（仍是写死的 3.0）")

    def test_direct_path_still_uses_user_params(self):
        """回归护栏：<=3MP 的直出路径行为不变。"""
        wf = self._upscale_workflow(resolution="1mp")
        self.assertEqual(wf["57:3"]["inputs"]["steps"], 30)
        self.assertEqual(wf["57:3"]["inputs"]["cfg"], 4.0)

    def test_upscale_second_stage_keeps_official_params(self):
        """第二段（57:75）是官方放大链的固定工艺参数，不该被用户参数改掉。

        【为什么】低 denoise + 强采样器是官方模板的放大链设计；
        改了会破坏放大链预期。
        """
        wf = self._upscale_workflow()
        second = wf["57:75"]["inputs"]
        self.assertEqual(second["steps"], W.Z_IMAGE_UPSCALE["steps"])
        self.assertEqual(second["cfg"], W.Z_IMAGE_UPSCALE["cfg"])
        self.assertEqual(second["sampler_name"], W.Z_IMAGE_UPSCALE["sampler"])


class TestNormalizeNotesReachFrontend(unittest.TestCase):
    """R5：归一说明必须下发到前端（否则「被改过」这件事是静默的）。"""

    def test_gen_params_includes_normalize_notes(self):
        notes = ["steps：100 不是合法取值，已改用 50"]
        params = {"prompt": "x", "model": "moss_image_base", "steps": 50,
                  "normalize_notes": notes}
        gp = S.job_meta.gen_params("t2i_moss", params)
        self.assertEqual(gp.get("normalize_notes"), notes,
                         "归一说明没有下发到 gen_params")

    def test_gen_params_normalize_notes_defaults_to_empty_list(self):
        gp = S.job_meta.gen_params("t2i_moss", {"prompt": "x"})
        self.assertEqual(gp.get("normalize_notes"), [],
                         "没有归一说明时应该是空列表，不是 None")

    def test_frontend_detail_modal_renders_normalize_notes(self):
        """前端详情弹层必须把「参数已调整」显示出来。"""
        js = _read("web/app.js")
        self.assertIn("normalize_notes", js,
                      "前端从未读取 normalize_notes —— 归一说明是静默的")
        self.assertIn("参数已调整", js,
                      "详情弹层没有「参数已调整」这一行")


class TestMossWorkflowReceivesParams(unittest.TestCase):
    """端到端：面板值 -> 后端 params -> 工作流节点输入。"""

    def test_moss_workflow_ksampler_receives_steps_100(self):
        """MOSS-Image 的 KSampler 节点必须收到 steps=100。"""
        params = {"job_id": "probe", "model": "moss_image_base",
                  "steps": 100, "cfg": 3, "shift": 5,
                  "sampler": "euler", "scheduler": "sgm_uniform",
                  "resolution": "1mp", "aspect": "16:9",
                  "seed": 2081206678357, "prompt": "x"}
        wf = W.build_moss_t2i(params)
        ks = wf["moss:16"]["inputs"]
        self.assertEqual(ks["steps"], 100)
        self.assertEqual(ks["cfg"], 3.0)
        self.assertEqual(ks["sampler_name"], "euler")
        self.assertEqual(ks["scheduler"], "sgm_uniform")
        self.assertEqual(ks["seed"], 2081206678357)

    def test_moss_shift_reaches_model_sampling_node(self):
        params = {"job_id": "probe", "model": "moss_image_base",
                  "steps": 50, "cfg": 3, "shift": 5,
                  "sampler": "euler", "scheduler": "sgm_uniform",
                  "resolution": "1mp", "aspect": "16:9",
                  "seed": 1, "prompt": "x"}
        wf = W.build_moss_t2i(params)
        self.assertEqual(wf["moss:15"]["inputs"]["shift"], 5.0)


if __name__ == "__main__":
    unittest.main()
