# -*- coding: utf-8 -*-
"""第5轮代码审查 C 段回归测试（C-5 / C-6 / C-9 / C-10 / C-11 / C-13 / C-14）。

【这批测试锁住的契约 —— 全部来自第4轮报告 C 段的遗留项】

  C-5 / C-6  图像模式的 steps / cfg / shift / sampler / scheduler **必须归一**
              （白名单来自各模型表自己的 samplers / schedulers 字段）
  C-9        Agent 建 flf / r2v 任务**必须**走服务端归一，且按各自 mode 的口径
  C-10       build_t2v_panel 缺宽高**不得**产出 0×0 latent（必须明确报错）
  C-11       死常量 / 死函数确实已从代码里消失
  C-13       _frame_expr_node 只有**一份**真源（不再三份分叉）
  C-14 / B-14  web/app.js 的 bindSeg 已删除，且全仓无调用点

【关键纪律 —— 断言必须能抓住回归】
每条断言都用**具体的坏值**（不存在的采样器 / 9999 步 / -1 cfg），
而不是「不等于 None」这种自指断言。
mutation 自证见 scripts/mutation_cr5_c_segment.py。

运行：python3 -m pytest tests/test_cr5_c_segment.py -v
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
from h3studio import h3_video_size  # noqa: E402
from h3studio import video_models as vm  # noqa: E402
from h3studio import video_workflow as vw  # noqa: E402
from h3studio import workflows as W  # noqa: E402


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class TestC5C6ImageSamplingNormalize(unittest.TestCase):
    """C-5 / C-6：图像模式的采样参数必须归一。"""

    def test_illegal_sampler_falls_back_to_model_default(self):
        """非法采样器 -> 回落该模型的官方推荐值，并留下说明。"""
        params = {"prompt": "x", "model": "z_image_turbo_bf16",
                  "sampler": "根本不存在的采样器"}
        S.prepare_job_params("t2i", params, [], None)
        model = W.Z_IMAGE_MODELS["z_image_turbo_bf16"]
        self.assertEqual(params["sampler"], model["sampler"])
        self.assertTrue(params.get("normalize_notes"),
                        "归一必须留下「改了什么」的说明")

    def test_illegal_scheduler_falls_back(self):
        params = {"prompt": "x", "model": "z_image_turbo_bf16",
                  "scheduler": "no_such_scheduler"}
        S.prepare_job_params("t2i", params, [], None)
        model = W.Z_IMAGE_MODELS["z_image_turbo_bf16"]
        self.assertEqual(params["scheduler"], model["scheduler"])

    def test_out_of_range_steps_falls_back(self):
        """steps=9999 必须被夹回模型推荐值，而不是原样送进工作流。"""
        params = {"prompt": "x", "model": "z_image_turbo_bf16", "steps": 9999}
        S.prepare_job_params("t2i", params, [], None)
        model = W.Z_IMAGE_MODELS["z_image_turbo_bf16"]
        self.assertEqual(params["steps"], model["steps"])

    def test_negative_cfg_falls_back(self):
        params = {"prompt": "x", "model": "z_image_turbo_bf16", "cfg": -1}
        S.prepare_job_params("t2i", params, [], None)
        model = W.Z_IMAGE_MODELS["z_image_turbo_bf16"]
        self.assertEqual(params["cfg"], model["cfg"])

    def test_zero_shift_falls_back(self):
        params = {"prompt": "x", "model": "z_image_turbo_bf16", "shift": 0}
        S.prepare_job_params("t2i", params, [], None)
        model = W.Z_IMAGE_MODELS["z_image_turbo_bf16"]
        self.assertEqual(params["shift"], model["shift"])

    def test_legal_values_are_untouched(self):
        """契约 R1：合法值**一个字节都不动**（老任务重放语义安全）。"""
        legal = {"prompt": "x", "model": "z_image_turbo_bf16",
                 "sampler": "euler", "scheduler": "simple",
                 "steps": 8, "cfg": 1.0, "shift": 3.0}
        params = dict(legal)
        S.prepare_job_params("t2i", params, [], None)
        for key, value in legal.items():
            self.assertEqual(params[key], value,
                             f"{key} 被归一层改动了 —— 合法值必须原样保留")
        self.assertNotIn("normalize_notes", params,
                         "合法值不该产生任何归一说明")

    def test_whitelist_comes_from_model_table(self):
        """白名单必须来自**模型表自己** —— 采样器不在模型表清单里就该被拒。

        【为什么这条重要】新造一份白名单常量 = 与模型表漂移的种子。
        这条断言让「白名单来源」这件事**可验证**，而不是靠注释声明。
        """
        turbo = W.Z_IMAGE_MODELS["z_image_turbo_bf16"]
        # 找一个 Turbo 不支持、但 Base 支持的调度器
        base_only = [s for s in W.Z_IMAGE_MODELS["z_image_bf16"]["schedulers"]
                     if s not in turbo["schedulers"]]
        self.assertTrue(base_only, "前置条件：Turbo 与 Base 的调度器清单应不同")
        params = {"prompt": "x", "model": "z_image_turbo_bf16",
                  "scheduler": base_only[0]}
        S.prepare_job_params("t2i", params, [], None)
        self.assertEqual(params["scheduler"], turbo["scheduler"],
                         "Turbo 上用了 Base 才支持的调度器 —— 白名单没生效")

    def test_all_image_modes_are_covered(self):
        """每一个图像模式都必须走归一（不能漏掉某一族）。"""
        cases = [
            ("t2i", "z_image_turbo_bf16", W.Z_IMAGE_MODELS),
            ("t2i_qwen", None, W.QWEN_IMAGE_MODELS),
            ("t2i_moss", None, W.MOSS_IMAGE_MODELS),
            ("t2i_h3image", None, None),
        ]
        for mode, model_key, table in cases:
            if table is None:
                from h3studio import h3image as _h3img
                table = _h3img.H3_IMAGE_MODELS
            key = model_key or next(iter(table))
            params = {"prompt": "x", "model": key, "sampler": "bad_sampler_xyz"}
            S.prepare_job_params(mode, params, [], None)
            self.assertIn(params["sampler"], table[key]["samplers"],
                          f"{mode} 模式的采样器未被归一（当前值 "
                          f"{params['sampler']!r}）")

    def test_i2i_qwen_is_covered(self):
        """图片编辑（i2i_qwen）也必须归一 —— 它需要参考图才能过 prepare_job_params。

        【为什么单独测】i2i_qwen 没有参考图会被 prepare_job_params 拒掉
        （ref_required），走不到归一那一步 —— 所以这里直接测归一函数本身。
        """
        model = W.QWEN_IMAGE_MODELS[next(iter(W.QWEN_IMAGE_MODELS))]
        params = {"sampler": "bad_sampler_xyz"}
        notes = []
        gn.normalize_image_sampling(params, model, notes)
        self.assertEqual(params["sampler"], model["sampler"])

    def test_normalize_sampling_unit_level(self):
        """直接测归一函数本身（不经 prepare_job_params）。"""
        model = W.Z_IMAGE_MODELS["z_image_bf16"]
        params = {"steps": "abc", "cfg": 0.0, "shift": 1e9,
                  "sampler": "nope", "scheduler": "nope"}
        notes = []
        gn.normalize_image_sampling(params, model, notes)
        self.assertEqual(params["steps"], model["steps"])
        self.assertEqual(params["sampler"], model["sampler"])
        self.assertEqual(params["scheduler"], model["scheduler"])
        self.assertEqual(params["shift"], model["shift"])
        self.assertEqual(params["cfg"], model["cfg"])
        self.assertEqual(len(notes), 5, "五处非法值应各记一条说明")


class TestC9AgentVideoNormalize(unittest.TestCase):
    """C-9：Agent 建 flf / r2v 任务必须走归一，且按各自 mode 的口径。"""

    def test_prepare_agent_t2v_params_accepts_mode(self):
        """服务端归一入口必须有 mode 形参（C-9 的根因修复点）。"""
        import inspect
        sig = inspect.signature(S.prepare_agent_t2v_params)
        self.assertIn("mode", sig.parameters,
                      "prepare_agent_t2v_params 必须接受 mode 形参 —— "
                      "否则 Agent 走 flf/r2v 时仍按 t2v 的口径判")

    def test_flf_uses_flf_area_limit(self):
        """flf 的面积上限是 2.0MP（2026-11-17 对齐 nonebot seg），与 t2v 同。

        【历史】此前这里断言 flf 是 0.8MP、「1280×960 超 flf 上限必须被拦」。
        那个 0.8MP 是把**旧面板**的档位表误当成生成侧硬约束 ——
        实测上游 T8 / 核心节点均无面积门禁（reference_area_policy=
        warning_only_no_area_gate），nonebot seg1/seg2 用 2.0MP。

        现在这条守的是**真正的上限**：2.0MP 放行、3.5MP 仍被拦，
        且错误文案点名「首尾帧」。
        """
        big = {"prompt": "x", "model_key": "fl2va_pruned_int8",
               "width": 1280, "height": 960, "duration": 5}
        _, err_t2v = S.normalize_t2v_params(dict(big), mode="t2v")
        _, err_flf = S.normalize_t2v_params(dict(big), mode="flf")
        self.assertEqual(err_t2v, "", "1280×960 在 2.0MP 限内")
        self.assertEqual(err_flf, "", "1280×960 在 flf 的 2.0MP 限内，也必须放行")

        # 3.5MP 必须被两个模式同时拦下，且文案点名模式
        huge = {"prompt": "x", "model_key": "fl2va_pruned_int8",
                "width": 2560, "height": 1440, "duration": 5}
        _, err_huge_flf = S.normalize_t2v_params(dict(huge), mode="flf")
        self.assertTrue(err_huge_flf, "2560×1440 超 2.0MP，必须被拦")
        self.assertIn("首尾帧", err_huge_flf,
                      "错误文案要说清是首尾帧的上限（口径按 mode）")
        self.assertIn("2.0M", err_huge_flf, "错误文案必须写出 2.0M 这个上限")

    def test_r2v_rejects_fl2va_only_model(self):
        """r2v 必须 Ref2VA 权重；FL2VA-only 权重必须被拦。

        【为什么尺寸取 960×544】用**在 2.0MP 限内**的尺寸，确保测的是通道检查
        而不是尺寸检查（尺寸检查会先拦下超限值，掩盖通道错误）。
        """
        fl2va_only = [k for k, v in vm.VIDEO_MODELS.items()
                      if not v.get("supports_ref2va")]
        self.assertTrue(fl2va_only, "前置条件：应有 FL2VA-only 权重")
        params = {"prompt": "x", "model_key": fl2va_only[0],
                  "width": 960, "height": 544, "duration": 5}
        _, err = S.normalize_t2v_params(params, mode="r2v")
        self.assertTrue(err, "r2v 选 FL2VA 权重必须被拦（参考会没生效）")
        self.assertIn("Ref2VA", err)

    def test_r2v_accepts_ref2va_model(self):
        """对照：Ref2VA 权重在合规尺寸下必须放行。"""
        ref2va = [k for k, v in vm.VIDEO_MODELS.items()
                  if v.get("supports_ref2va")]
        self.assertTrue(ref2va, "前置条件：应有 Ref2VA 权重")
        params = {"prompt": "x", "model_key": ref2va[0],
                  "width": 960, "height": 544, "duration": 5}
        _, err = S.normalize_t2v_params(params, mode="r2v")
        self.assertEqual(err, "", f"Ref2VA 权重在 r2v 下必须放行（当前：{err}）")

    def test_agent_creates_flf_job_with_normalization(self):
        """Agent 建 flf 任务时确实调用了带 mode 的归一入口。"""
        import inspect
        src = _read("h3studio/agent.py")
        # 抓 create_job 里那一段：模式判定必须覆盖 flf / r2v
        m = re.search(r'if mode in \("t2v", "flf", "r2v"\):\s*\n'
                      r'\s*prepare_t2v = getattr\(ctx\.server,'
                      r' "prepare_agent_t2v_params", None\)', src)
        self.assertIsNotNone(m,
                             "agent.py 里 Agent 建任务的归一分支必须覆盖 "
                             "t2v / flf / r2v 三个视频模式")
        # 且必须把 mode 传下去
        m2 = re.search(r'await prepare_t2v\(params, mode=mode\)', src)
        self.assertIsNotNone(m2,
                             "Agent 必须把 mode 传给服务端归一入口，"
                             "否则 flf/r2v 仍按 t2v 口径判")

    def test_agent_default_params_still_t2v_only(self):
        """默认参数**只**对 t2v 套（不能给 flf/r2v 塞 t2v 的底模）。"""
        src = _read("h3studio/agent.py")
        m = re.search(r'if mode != "t2v":\s*\n\s*try:\s*\n'
                      r'\s*from \. import gen_defaults', src)
        self.assertIsNotNone(m,
                             "用户级默认参数（gen_defaults）必须仍只对非 t2v "
                             "模式套用 —— t2v 走服务端归一入口")


class TestC10ZeroSizeLatent(unittest.TestCase):
    """C-10：build_t2v_panel 缺宽高不得产出 0×0 latent。"""

    def test_missing_size_raises_instead_of_zero_latent(self):
        params = {"job_id": "x", "duration": 5,
                  "model_key": "fl2va_pruned_int8", "prompt": "p"}
        with self.assertRaises(ValueError) as ctx:
            vw.build_t2v_panel(params)
        message = str(ctx.exception)
        self.assertIn("尺寸", message)
        self.assertIn("宽高", message,
                      "错误文案要告诉用户缺的是宽高")

    def test_no_zero_latent_in_workflow(self):
        """产物工作流里不允许出现 width=0 / height=0。"""
        params = {"job_id": "x", "duration": 5,
                  "model_key": "fl2va_pruned_int8", "prompt": "p"}
        try:
            wf = vw.build_t2v_panel(params)
        except ValueError:
            return  # 明确报错也满足契约（不产出 0×0）
        for node in wf.values():
            inputs = node.get("inputs") or {}
            if "width" in inputs or "height" in inputs:
                self.assertGreater(int(inputs.get("width") or 0), 0)
                self.assertGreater(int(inputs.get("height") or 0), 0)

    def test_normal_size_still_works(self):
        """正常参数不受影响（回归保护）。"""
        params = {"job_id": "x", "duration": 5, "width": 1344, "height": 768,
                  "model_key": "fl2va_pruned_int8", "prompt": "p"}
        wf = vw.build_t2v_panel(params)
        cond = [n for n in wf.values()
                if n.get("class_type") == "MiniMaxH3ImageToVideo"]
        self.assertTrue(cond)
        self.assertEqual(cond[0]["inputs"]["width"], 1344)
        self.assertEqual(cond[0]["inputs"]["height"], 768)

    def test_replay_fallback_uses_same_formula_as_panel(self):
        """老任务重放：档位+比例 -> 宽高，必须与面板**同一套**公式。"""
        params = {"resolution": "544p", "aspect": "16:9"}
        notes = []
        gn.normalize_video_size_fallback(params, "flf", notes)
        expected_w, expected_h = h3_video_size.dimensions_for("544p", "16:9",
                                                             mode="flf")
        self.assertEqual(params["width"], expected_w)
        self.assertEqual(params["height"], expected_h)

    def test_replay_fallback_refuses_to_invent_size(self):
        """连档位与比例都没有时**不得编造尺寸**（宁可报错）。"""
        params = {}
        notes = []
        with self.assertRaises(ValueError):
            gn.normalize_video_size_fallback(params, "t2v", notes)
        self.assertNotIn("width", params)
        self.assertNotIn("height", params)

    def test_replay_fallback_keeps_existing_size(self):
        """已有宽高时**不得**改写（R1：合法值不动）。"""
        params = {"width": 1344, "height": 768, "resolution": "544p"}
        notes = []
        gn.normalize_video_size_fallback(params, "t2v", notes)
        self.assertEqual(params["width"], 1344)
        self.assertEqual(params["height"], 768)


class TestC11C13DeadCode(unittest.TestCase):
    """C-11 / C-13：死常量与重复实现已清理。"""

    def test_dead_constants_removed(self):
        """C-11 / C-12：确认无外部调用方的死常量已从代码里删除。"""
        dead = {
            "h3studio/video_models.py": [
                "VIDEO_MAX_PIXELS_LITERAL", "VIDEO_DURATION_MIN_OLD",
                "VIDEO_DURATION_MAX_OLD", "VIDEO_MAX_PIXELS",
            ],
        }
        for rel, names in dead.items():
            src = _read(rel)
            for name in names:
                # 允许出现在注释里，但不允许有「赋值定义」
                self.assertIsNone(
                    re.search(rf"^\s*{name}\s*[:=]", src, re.M),
                    f"{rel} 里的死常量 {name} 仍存在（应已删除）")

    def test_frame_expr_node_has_single_source(self):
        """C-13：帧数表达式节点只有**一份真源**（frame_expr.py）。

        【为什么允许三处都有 _frame_expr_node 这个名字】
        三处保留的是**同名薄包装**（一行转发到 frame_expr.frame_expr_node），
        供各模块本地调用不必改 import 路径。
        真正要锁住的是「**实现**只有一份」——
        即：frame_expr.py 之外不得再出现 ComfyMathExpression 的
        「expression」字面量或 PrimitiveFloat 时长节点的构造。
        """
        source_of_truth = "h3studio/frame_expr.py"
        self.assertTrue(os.path.exists(os.path.join(ROOT, source_of_truth)),
                        f"唯一真源模块 {source_of_truth} 必须存在")

        # 除真源外，任何文件都不得再自己写帧数表达式
        formula = "max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17"
        for rel in ("h3studio/workflows.py", "h3studio/video_workflow.py",
                    "h3studio/h3_video_modes.py"):
            src = _read(rel)
            self.assertNotIn(formula, src,
                             f"{rel} 仍在硬编码帧数公式 —— 真源只能是 "
                             f"{source_of_truth}")

    def test_frame_expr_outputs_are_identical(self):
        """C-13：三处 _frame_expr_node 产出的节点必须**完全一致**。

        【为什么这条比「只有一份实现」更强】
        即使三份实现长得一样，只要**有一份**的分叉（比如 workflows.py 那份
        写死 5 秒），用户就会在某条路径上拿到不同的帧数。
        所以这里直接断言：**三处产出逐字节相等**。
        """
        from h3studio import frame_expr as fe
        from h3studio import h3_video_modes as hvm
        from h3studio import video_workflow as vwork
        from h3studio import workflows as wf

        expected = fe.frame_expr_node("L", "D", 7.0)
        for name, fn in (("video_workflow", vwork._frame_expr_node),
                         ("h3_video_modes", hvm._frame_expr_node),
                         ("workflows", wf._frame_expr_node)):
            got = fn("L", "D", 7.0)
            self.assertEqual(got, expected,
                             f"{name} 的 _frame_expr_node 与真源不一致 —— 分叉了")

    def test_frame_expr_duration_is_required(self):
        """【C-13 的根因断言】旧 workflows.py 那份写死 5 秒 —— 必须不再存在。

        旧实现没有 duration 形参，靠调用方事后覆盖
        ``expr["105:111"]["inputs"]["value"] = duration``。
        现在 duration 必填 -> 「忘了覆盖」在类型层面就不可能再发生。
        """
        from h3studio import workflows as wf
        src = _read("h3studio/workflows.py")
        self.assertNotIn('expr["105:111"]["inputs"]["value"] = duration', src,
                         "旧的事后覆盖写法必须消失（时长必须一次传对）")
        # workflows.py 的薄包装必须有 duration 形参
        import inspect
        sig = inspect.signature(wf._frame_expr_node)
        self.assertIn("duration", sig.parameters)
        self.assertIs(sig.parameters["duration"].default, 5,
                      "duration 的默认值应保留 5（老调用方兼容），"
                      "但调用方**必须**显式传真实时长")

    def test_frame_expr_formula_is_identical_everywhere(self):
        """帧数公式**只**在真源里出现 —— 其余文件不得硬编码（分叉检测）。

        【为什么断言方向是「只在真源有」而不是「每个文件都有」】
        合并后的形态是：frame_expr.py 持有公式，其余三处是一行转发。
        所以正确的方向是「公式**只**出现在真源」——
        如果某个文件又自己写了一遍公式，那才是分叉。
        """
        formula = "max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17"
        truth = _read("h3studio/frame_expr.py")
        self.assertIn(formula, truth, "真源必须持有公式")

        for rel in ("h3studio/workflows.py", "h3studio/video_workflow.py",
                    "h3studio/h3_video_modes.py"):
            src = _read(rel)
            self.assertNotIn(formula, src,
                             f"{rel} 又硬编码了一遍帧数公式 —— "
                             "公式只允许存在于 h3studio/frame_expr.py")


class TestC14BindSegRemoved(unittest.TestCase):
    """C-14 / B-14：web/app.js 的 bindSeg 已删除且无调用点。

    【为什么断言用正则而不是子串】
    删除说明本身要在注释里提到 ``bindSeg(sel, key, cb)``（要说明删的是什么），
    所以裸子串 ``bindSeg(`` 会命中注释 —— 那是假阳性。
    这里用**代码形态**的正则：只匹配真正的函数定义与真正的调用。
    """

    #: 真正的函数定义：function bindSeg(
    RE_DEF = re.compile(r"^\s*function\s+bindSeg\s*\(", re.M)
    #: 真正的调用点：bindSeg( 前面不是注释行、不是反引号
    RE_CALL = re.compile(r"^[^/]*\bbindSeg\s*\(", re.M)

    def test_bind_seg_not_defined(self):
        src = _read("web/app.js")
        self.assertIsNone(self.RE_DEF.search(src),
                          "bindSeg 必须从 web/app.js 删除")

    def test_bind_seg_has_no_call_site(self):
        """全仓（含 web/ 其它文件与 index.html）不得有 bindSeg 调用点。"""
        web_dir = os.path.join(ROOT, "web")
        for name in sorted(os.listdir(web_dir)):
            if not name.endswith((".js", ".html")):
                continue
            src = _read(os.path.join("web", name))
            self.assertIsNone(self.RE_DEF.search(src),
                              f"web/{name} 仍定义 bindSeg")
            self.assertIsNone(self.RE_CALL.search(src),
                              f"web/{name} 仍调用 bindSeg —— "
                              "删除定义前必须先清调用点")

    def test_bind_seg_not_referenced_in_tests_or_scripts(self):
        """测试与脚本里也不该再有 bindSeg 的调用（注释提及不算）。"""
        for rel in ("tests", "scripts"):
            base = os.path.join(ROOT, rel)
            if not os.path.isdir(base):
                continue
            for name in sorted(os.listdir(base)):
                if not name.endswith(".py"):
                    continue
                src = _read(os.path.join(rel, name))
                # 本测试文件自身会提到 bindSeg（断言目标）—— 排除自己
                if name == os.path.basename(__file__):
                    continue
                self.assertIsNone(self.RE_CALL.search(src),
                                  f"{rel}/{name} 仍调用 bindSeg")


if __name__ == "__main__":
    unittest.main()
