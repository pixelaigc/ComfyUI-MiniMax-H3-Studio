# -*- coding: utf-8 -*-
"""视频族 / 音乐族面板参数生效性回归测试（2026-11-16 并行审计 D-1~D-8）。

【这一批锁住的契约】
  D-1  视频面板**持久化**不得丢掉用户改过的步数/采样器/调度器
       （刷新页面后仍是用户的值）
  D-2  「复用参数」必须回填**被复用那条任务**的参数，
       而不是面板自己上次的配置
  D-3  二采开启时主采样器下拉必须置灰（工作流里写死 euler）
  D-4  二采 transition_step 被步数档夹回时必须**留痕**
  D-5  二采 rho=0 被改成 0.6 时必须**留痕**
  D-6  shift 滑杆的「节点不可用」禁用判据必须读对层级
  D-7  音乐温度 **0** 是合法值，不得被 falsy 判空回默认
  D-8  视频族的越界回落 / 静默改写必须记进 normalize_notes

【为什么这些都要测】
D-1/D-2 与本次 MOSS-Image 的 BUG 是**同一形态**：
面板上显示的值 ≠ 真正生效/提交的值。
它们比 MOSS 那条更隐蔽 —— MOSS 是「后端改」，这两条是「前端自己改」。

【断言纪律】
每条断言都指名**具体字段 + 具体坏值/好值**，不是「不等于 None」这种自指断言。
前端断言用「源码里这段逻辑必须长什么样」的形式（纯函数无法在 pytest 里跑 DOM），
并配合**真实浏览器**的端到端验证脚本 scripts/verify_video_param_persist.py。

运行：python3 -m pytest tests/test_video_param_effectiveness.py -v
"""

import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server as S  # noqa: E402
from h3studio import video_models as vm  # noqa: E402


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _base_video_params(**overrides):
    """一份合法的视频面板参数（用真实的默认模型 key）。"""
    p = {
        "prompt": "x",
        "model_key": vm.VIDEO_DEFAULT_MODEL,
        "width": 1344,
        "height": 768,
        "duration": 5,
    }
    p.update(overrides)
    return p


class TestD1PersistenceKeepsUserSampling(unittest.TestCase):
    """D-1：刷新页面不得把用户改过的采样档打回默认。"""

    def test_collect_config_top_level_has_no_steps(self):
        """前提锁定：采样档存在 mode_configs 里，**顶层不写** steps。

        【为什么锁这条】它解释了 D-1 的根因：载入时的守卫读的是
        顶层 `saved.steps` -> 恒 undefined -> 条件恒真。
        若哪天有人把 steps 加回顶层，这条会红，提醒同步改守卫。
        """
        js = _read("web/video-panel.js")
        block = re.search(r"function collectConfig\(\)\s*\{(.*?)\n  \}", js, re.S)
        self.assertIsNotNone(block, "找不到 collectConfig()")
        body = block.group(1)
        # 顶层字段里不应出现 steps（只该在 mode_configs 里）
        self.assertNotIn("steps:", body,
                         "collectConfig() 顶层不该写 steps（采样档按模式存）")
        self.assertIn("mode_configs:", body,
                      "collectConfig() 必须存 mode_configs")

    def test_mode_configs_carries_sampling_fields(self):
        """mode_configs 的每个模式片段必须带采样档。"""
        js = _read("web/video-panel.js")
        block = re.search(r"function modeConfigs\(\)\s*\{(.*?)\n  \}", js, re.S)
        self.assertIsNotNone(block, "找不到 modeConfigs()")
        body = block.group(1)
        for field in ("steps", "sampler", "scheduler", "shift_video",
                      "shift_audio"):
            self.assertIn(field + ":", body,
                          "mode_configs 缺 %s —— 刷新后采样档会丢" % field)

    def test_load_guard_reads_mode_scoped_steps(self):
        """【核心断言】载入时的守卫必须读 mode_configs[当前模式] 的 steps。

        旧实现 `if (saved.steps == null) recomputeSamplingForAccel();`
        读顶层 -> 恒真 -> 每次刷新都用 accel LoRA 推荐档覆盖用户值。
        """
        js = _read("web/video-panel.js")
        guard = re.search(
            r"const\s+savedSteps\s*=\s*([\s\S]{0,200}?)\n\s*if\s*\(", js)
        self.assertIsNotNone(guard, "找不到 savedSteps 守卫")
        body = guard.group(1)
        self.assertIn("savedModeCfg.steps", body,
                      "守卫必须取当前模式片段里的 steps")
        # 取 mode_configs 的那一行也要在
        self.assertRegex(
            js, r"savedModeCfg\s*=\s*\(\s*saved\.mode_configs",
            "savedModeCfg 必须从 saved.mode_configs 取")
        # 旧的恒真守卫不能还在
        self.assertNotIn("if (saved.steps == null) recomputeSamplingForAccel();",
                         js,
                         "旧的恒真守卫仍在 —— 刷新会把用户改的采样档打回默认")


class TestD2ReuseParamsWins(unittest.TestCase):
    """D-2：「复用参数」必须回填被复用任务的参数。"""

    def test_apply_config_top_level_overrides_mode_configs(self):
        """【核心断言】applyConfig() 里顶层字段必须**覆盖** mode_configs。

        旧实现：只要 mode_configs[当前模式] 存在就用它并 return，
        顶层字段被完全忽略 -> 复用参数被面板自己的旧配置整块覆盖。
        """
        js = _read("web/video-panel.js")
        block = re.search(r"function applyConfig\(cfg\)\s*\{([\s\S]*?)\n  \}",
                          js)
        self.assertIsNotNone(block, "找不到 applyConfig()")
        body = block.group(1)
        # 顶层字段必须被合并进最终生效的配置
        self.assertRegex(
            body, r"merged\[key\]\s*=\s*cfg\[key\]",
            "applyConfig() 必须让顶层字段覆盖 mode_configs 的片段")
        self.assertIn("applyModeConfig(merged)", body,
                      "applyConfig() 必须把合并后的配置交给 applyModeConfig()")
        # 旧的「用 modeCfg 就 return」写法不能还在
        self.assertNotRegex(
            body, r"applyModeConfig\(modeCfg\);\s*\n\s*return;",
            "旧的「mode_configs 优先并 return」写法仍在 -> 顶层字段被忽略")

    def test_apply_config_merged_is_mutable(self):
        """【回归护栏】merged 不能被**整体重新赋值**。

        【为什么需要这条 —— 2026-11-16 实测踩到的坑】
        D-2 的第一版实现写成::

            const merged = {};
            if (modeCfg && ...) merged = { ...modeCfg };   // <- 给 const 赋值
            for (...) merged[key] = cfg[key];

        给 const **重新赋值** ->
        `TypeError: Assignment to constant variable.`
        -> applyConfig() 抛异常 -> **整段配置恢复静默失效**
        （外层 try/catch 把异常吞了，页面无任何报错），
        表现就是「刷新后用户改的步数又变回默认」。

        【断言怎么写才抓得住】
        注意区分两种赋值：
          * ``merged[key] = v``  —— 给**属性**赋值，const 对象是允许的；
          * ``merged = v``        —— 给**变量本身**赋值，const 禁止。
        本条盯的是第二种（真正会抛的那一种）。
        """
        js = _read("web/video-panel.js")
        block = re.search(r"function applyConfig\(cfg\)\s*\{([\s\S]*?)\n  \}",
                          js)
        self.assertIsNotNone(block, "找不到 applyConfig()")
        body = block.group(1)
        # 找到 merged 的声明
        decl = re.search(r"(const|let|var)\s+merged\s*=", body)
        self.assertIsNotNone(decl, "找不到 merged 的声明")
        self.assertNotEqual(
            decl.group(1), "const",
            "merged 被声明为 const —— 后续整体重新赋值会抛 "
            "TypeError，整段配置恢复静默失效（改成 let）")
        # 且不允许出现「整体重新赋值」这一形态（排除声明行本身）
        decl_assign = re.search(r"(?:const|let|var)\s+merged\s*=\s*", body)
        decl_end = decl_assign.end() if decl_assign else 0
        reassign = []
        for m in re.finditer(r"(?<![\w.\[])merged\s*=\s*[^=]", body):
            if m.start() >= decl_end:
                reassign.append(m.group(0))
        self.assertEqual(
            reassign, [],
            "不允许对 merged 做整体重新赋值（只允许 merged[key]=... 属性赋值）")

    def test_reuse_config_carries_sampling_fields(self):
        """reuseConfigFromJob 必须带齐采样字段（app.js 侧）。"""
        js = _read("web/app.js")
        block = re.search(r"function reuseConfigFromJob\([^)]*\)\s*\{([\s\S]*?)\n\}",
                          js)
        self.assertIsNotNone(block, "找不到 reuseConfigFromJob()")
        body = block.group(1)
        for field in ("steps", "sampler", "scheduler", "shift_video",
                      "shift_audio"):
            self.assertIn(field, body,
                          "reuseConfigFromJob 缺 %s -> 复用参数带不回来" % field)


class TestD3TwopassDisablesMainSampler(unittest.TestCase):
    """D-3：二采开启时主采样器下拉必须置灰。"""

    def test_render_twopass_disables_main_sampler(self):
        js = _read("web/video-panel.js")
        block = re.search(r"function renderTwopass\(\)\s*\{([\s\S]*?)\n  \}",
                          js)
        self.assertIsNotNone(block, "找不到 renderTwopass()")
        body = block.group(1)
        self.assertIn("vidSamplerSelect", body,
                      "renderTwopass() 必须处理主采样器下拉")
        self.assertRegex(body, r"mainSampler\.disabled\s*=\s*twopassOn",
                         "二采开启时主采样器下拉必须置灰")

    def test_workflow_forces_euler_when_twopass(self):
        """前提锁定：二采的工作流确实写死 euler（所以界面必须置灰）。"""
        src = _read("h3studio/video_workflow.py")
        self.assertRegex(src, r"forced_sampler\s*=\s*\"euler\"",
                         "二采的工作流应写死 euler")


class TestD4D5D8NormalizeNotes(unittest.TestCase):
    """D-4 / D-5 / D-8：视频族的静默改写必须留痕。"""

    def test_out_of_range_steps_is_noted(self):
        params = _base_video_params(steps=999)
        out, err = S.normalize_t2v_params(params, "t2v")
        self.assertEqual(err, "")
        self.assertEqual(out["steps"], 50)
        notes = out.get("normalize_notes") or []
        self.assertTrue(notes, "steps 越界必须留归一说明")
        self.assertTrue(any("steps" in n for n in notes),
                        "说明里必须点名 steps：%s" % notes)

    def test_illegal_sampler_is_noted(self):
        params = _base_video_params(sampler="根本不存在的采样器")
        out, err = S.normalize_t2v_params(params, "t2v")
        notes = out.get("normalize_notes") or []
        self.assertTrue(any("sampler" in n for n in notes),
                        "非法采样器必须留归一说明：%s" % notes)

    def test_legal_values_leave_no_notes(self):
        """契约 R1：合法值一个字节都不动，也不该产生任何说明。"""
        params = _base_video_params(steps=30, sampler="euler", scheduler="simple")
        out, err = S.normalize_t2v_params(params, "t2v")
        self.assertEqual(err, "")
        self.assertEqual(out["steps"], 30)
        self.assertEqual(out["sampler"], "euler")
        self.assertEqual(out["scheduler"], "simple")
        self.assertNotIn("normalize_notes", out,
                         "合法值不该产生归一说明")

    def test_twopass_transition_clamp_is_noted(self):
        """D-4：transition_step 被步数档夹回要留痕。"""
        params = _base_video_params(
            steps=4,
            twopass={"enabled": True, "transition_step": 6, "cfg": 2.0})
        out, err = S.normalize_t2v_params(params, "t2v")
        self.assertEqual(err, "")
        twopass = out.get("twopass") or {}
        self.assertEqual(twopass.get("transition_step"), 3,
                         "步数档 4 步时 transition 上限是 3")
        notes = out.get("normalize_notes") or []
        self.assertTrue(any("transition_step" in n for n in notes),
                        "transition 被夹回必须留归一说明：%s" % notes)

    def test_twopass_rho_rewrite_is_noted(self):
        """D-5：rho=0 且不用放大器 -> 0.6，必须留痕。"""
        params = _base_video_params(
            steps=30,
            twopass={"enabled": True, "rho": 0.0, "upscaler_model": "none",
                     "cfg": 2.0})
        out, err = S.normalize_t2v_params(params, "t2v")
        twopass = out.get("twopass") or {}
        self.assertEqual(twopass.get("rho"), 0.6)
        notes = out.get("normalize_notes") or []
        self.assertTrue(any("rho" in n for n in notes),
                        "rho 被改写必须留归一说明：%s" % notes)

    def test_notes_reach_gen_params(self):
        """归一说明必须下发到前端（否则又是静默的）。"""
        params = _base_video_params(steps=999)
        out, _ = S.normalize_t2v_params(params, "t2v")
        gp = S.job_meta.gen_params("t2v", out)
        self.assertTrue(gp.get("normalize_notes"),
                        "视频族归一说明没下发到 gen_params")

    def test_frontend_renders_normalize_notes(self):
        """前端详情弹层必须显示「参数已调整」（两族共用这一行）。"""
        js = _read("web/app.js")
        self.assertIn("normalize_notes", js,
                      "前端从不读 normalize_notes —— 归一说明是静默的")
        self.assertIn("参数已调整", js,
                      "详情弹层没有「参数已调整」这一行")


class TestD6SigmaShiftAvailability(unittest.TestCase):
    """D-6：shift 禁用判据必须读对层级。"""

    def test_frontend_reads_top_level_availability(self):
        js = _read("web/video-panel.js")
        self.assertRegex(
            js, r"VIDEO_OPTIONS\.sigma_shift_available",
            "前端必须读 payload **顶层**的 sigma_shift_available")
        self.assertNotRegex(
            js, r"const\s+shiftAvailable\s*=\s*cfg\.sigma_shift_available",
            "旧的读法恒为 undefined -> 禁用判据永不触发")

    def test_backend_places_it_at_top_level(self):
        src = _read("h3studio/video_models.py")
        self.assertRegex(src, r'payload\["sigma_shift_available"\]\s*=',
                         "后端应把 sigma_shift_available 放在 payload 顶层")


class TestD7MusicTemperatureZero(unittest.TestCase):
    """D-7：音乐温度 0 是合法值，不得被回默认。"""

    def test_music_js_does_not_use_falsy_check_for_temperature(self):
        js = _read("web/music.js")
        # 只看**可执行代码**（去掉注释）—— 否则注释里写的
        # 「旧实现用 `if (!st.temperature)`」会被误判成代码还在。
        code_only = re.sub(r"//[^\n]*", "", js)
        code_only = re.sub(r"/\*.*?\*/", "", code_only, flags=re.S)
        self.assertNotIn("if (!st.temperature)", code_only,
                         "温度用 falsy 判空 -> 合法值 0 被回默认")
        self.assertIn("if (st.temperature == null)", code_only,
                      "温度必须用显式 null 判空")

    def test_other_music_sampling_fields_also_use_explicit_null(self):
        js = _read("web/music.js")
        code_only = re.sub(r"//[^\n]*", "", js)
        code_only = re.sub(r"/\*.*?\*/", "", code_only, flags=re.S)
        for field in ("top_p", "top_k", "repetition_penalty"):
            self.assertNotIn("if (!st.%s)" % field, code_only,
                             "%s 也应改成显式 null 判空（量程可能放宽到 0）" % field)

    def test_music_temp_slider_allows_zero(self):
        """前提锁定：温度滑杆 min=0 -> 0 是合法取值。"""
        html = _read("web/index.html")
        m = re.search(r'id="musicTemp"[^>]*min="([^"]+)"', html)
        self.assertIsNotNone(m, "找不到 #musicTemp")
        self.assertEqual(float(m.group(1)), 0.0,
                         "温度滑杆下界变了 -> D-7 的前提需重新评估")


class TestVideoSamplingStillReachesWorkflow(unittest.TestCase):
    """回归护栏：视频族的采样参数**仍然**真实到达工作流。

    【为什么要有】D-1/D-2/D-3/D-6 都改了视频面板 ——
    必须证明「改完之后采样参数照样送到工作流」，而不是改坏了。
    """

    def test_steps_reach_basic_scheduler(self):
        from h3studio import video_workflow as vw
        params = _base_video_params(steps=30, sampler="euler",
                                    scheduler="simple", shift_video=5.0,
                                    shift_audio=2.0)
        params["job_id"] = "probe"
        params["model_key"] = vm.VIDEO_DEFAULT_MODEL
        params["accel_lora"] = ""
        wf = vw.build_t2v_panel(params)
        sched = None
        sampler = None
        for node in wf.values():
            ct = (node.get("class_type") or "").lower()
            if ct == "basicscheduler":
                sched = node["inputs"]
            if ct == "ksamplerselect":
                sampler = node["inputs"].get("sampler_name")
        self.assertIsNotNone(sched, "工作流里没有 BasicScheduler")
        self.assertEqual(sched.get("steps"), 30)
        self.assertEqual(sched.get("scheduler"), "simple")
        self.assertEqual(sampler, "euler")


if __name__ == "__main__":
    unittest.main()
