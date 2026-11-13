# -*- coding: utf-8 -*-
"""「复用参数」处理器路径的回归测试（2026-11-13，独立验收 P1/P2 修复轮）。

【背景：上一轮为什么漏了】
上一轮的专项测试只覆盖了 ``applyConfig`` 层（video-panel.js 的配置应用），
**没有覆盖 app.js 里点「复用参数」的处理器**。独立验收据此报出 P1：
内置 Agent 创建的老任务（params 里没有 width/height、model_key 也为空）
点「复用参数」后分辨率**完全不回填** —— 停在 640×352、档位停在「自定义」。
验收还实测：把复用处理器的 width/height 改成 undefined（等于删掉这项功能），
60 条专项测试**仍然全绿** —— 这就是 P2（处理器这条分支零断言保护）。

【本文件的做法】
把 app.js 里「复用参数」的**配置解析**抽成纯函数 ``reuseConfigFromJob``
（无 DOM 依赖），本文件用 **Node 真跑这段源码**，喂真实形状的 job/params，
直接断言回填结果：

* 老任务（params 无 width/height、model_key 为空）
  -> 宽高必须从 ``job.resolution_info`` 兜底回填，且档位同步对上；
* 新任务（params 有 width/height）
  -> 仍然优先用 params（不被 resolution_info 覆盖）；
* 两边都没有 -> 留空（不编造尺寸）。

**为什么是真跑 JS 而不是文本匹配**：见 tests/test_h3_panel_tune.py 顶部的说明 ——
「字符串在文件里出现过」≠「那条断言在守行为」，变异测试不红就等于漏网。
本文件的断言直接吃 ``reuseConfigFromJob`` 的**返回值**。

运行：python3 -m pytest tests/test_reuse_resolution_fallback.py -v
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

WEB = REPO / "web"
APP_JS_PATH = WEB / "app.js"
APP_JS = APP_JS_PATH.read_text(encoding="utf-8")


def _extract_function(src: str, name: str) -> str:
    """从 JS 源码里取出某个顶层函数的**完整定义**（含花括号配平）。

    【为什么自己抽而不是 eval 整个 app.js】app.js 有 5000 行、顶层就绑了一堆
    DOM 事件监听，直接在 Node 里跑会炸在一堆无关的依赖上。
    这里只取被测函数本身 —— **跑的是线上真实代码**，不是复刻品。
    """
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


def _run_reuse(cases: list[dict]) -> list[dict]:
    """在 Node 里真跑 app.js 的 reuseConfigFromJob，返回每个 case 的结果。

    cases 里的每个元素形如：
        {"job": {...}, "params": {...}, "panel": {...}}
    """
    node = shutil.which("node") or shutil.which("nodejs")
    assert node is not None, "本机没有 node"

    fn_src = _extract_function(APP_JS, "reuseConfigFromJob")
    tmp = Path(tempfile.mkdtemp(prefix="h3-reuse-test-"))
    try:
        run_js = tmp / "run.js"
        run_js.write_text(
            fn_src
            + "\n"
            + "const CASES = " + json.dumps(cases, ensure_ascii=False) + ";\n"
            + "const out = CASES.map(c => {\n"
            + "  try {\n"
            + "    const cfg = reuseConfigFromJob(c.job, c.params, c.panel);\n"
            + "    return {ok: true, cfg};\n"
            + "  } catch (e) {\n"
            + "    return {ok: false, error: String(e && e.message || e)};\n"
            + "  }\n"
            + "});\n"
            + "console.log(JSON.stringify(out));\n",
            encoding="utf-8")
        proc = subprocess.run([node, str(run_js)], capture_output=True,
                              text=True, timeout=60)
        if proc.returncode != 0:
            raise AssertionError(
                "Node 脚本失败 (exit %d):\nSTDOUT:\n%s\nSTDERR:\n%s"
                % (proc.returncode, proc.stdout, proc.stderr))
        return json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


#: 服务端下发的 size_presets 清单里跟本测试相关的几档（真实形状）。
PRESETS = [
    {"key": "16:9_0.4", "aspect": "16:9", "width": 864, "height": 480,
     "label": "480p · 864×480", "megapixels": 0.4},
    {"key": "16:9_0.98", "aspect": "16:9", "width": 1344, "height": 768,
     "label": "768p · 1344×768", "megapixels": 0.98},
    {"key": "16:9_0.75", "aspect": "16:9", "width": 1216, "height": 672,
     "label": "672p · 1216×672", "megapixels": 0.75},
]


def _panel(state_extra: dict | None = None) -> dict:
    state = {"aspect": "16:9", "width": 640, "height": 352,
             "sizePresetKey": "", "custom": True}
    state.update(state_extra or {})
    return {"state": state, "options": {"size_presets": PRESETS}}


class TestReuseConfigResolutionFallback(unittest.TestCase):
    """P1：老任务（params 无宽高）的复用回填必须靠 resolution_info 兜底。"""

    def test_old_agent_job_falls_back_to_resolution_info(self):
        """内置 Agent 创建的老任务：params 无 width/height、model_key 为空。

        这是独立验收报的线上 job 544bdfef23f3 / 78bf9c1af648 的真实形状：
        params = {aspect, duration, gen_type, prompt, resolution}
        job.resolution_info = {width:1216, height:672, source:"actual", ...}
        """
        job = {
            "id": "544bdfef23f3",
            "mode": "t2v",
            "params": {},
            "resolution_info": {
                "width": 1216, "height": 672, "tier": "672p",
                "aspect": "16:9", "pixels": "1216×672",
                "label": "672p · 1216×672 · 16:9",
                "source": "actual", "note": "", "has_size": True,
            },
        }
        params = {"aspect": "16:9", "duration": 10.0, "gen_type": "t2v",
                  "prompt": "a red panda", "resolution": "544p"}
        out = _run_reuse([{"job": job, "params": params, "panel": _panel()}])[0]
        self.assertTrue(out["ok"], "函数抛异常了：%s" % out.get("error"))
        cfg = out["cfg"]
        self.assertEqual(cfg.get("width"), 1216,
                         "宽必须从 resolution_info 兜底回填，实际 %s" % cfg.get("width"))
        self.assertEqual(cfg.get("height"), 672,
                         "高必须从 resolution_info 兜底回填，实际 %s" % cfg.get("height"))
        self.assertEqual(cfg.get("rawWidth"), 1216)
        self.assertEqual(cfg.get("rawHeight"), 672)
        # 档位也必须同步对上（不能停在「自定义」）
        self.assertEqual(cfg.get("size_preset_key"), "16:9_0.75",
                         "档位没跟着宽高反查，实际 %r" % cfg.get("size_preset_key"))

    def test_old_job_with_empty_model_key_still_backfills(self):
        """model_key 为空**不能**再成为跳过回填的理由。

        旧实现：`if (job.mode === "t2v" && p.model_key && window.VIDEO_PANEL)`
        —— model_key 一空，整个宽高/采样回填分支整个被跳过。
        """
        job = {"mode": "t2v", "resolution_info": {"width": 1216, "height": 672,
                                                   "aspect": "16:9"}}
        params = {"aspect": "16:9", "duration": 10.0, "gen_type": "t2v"}
        out = _run_reuse([{"job": job, "params": params, "panel": _panel()}])[0]
        self.assertTrue(out["ok"])
        cfg = out["cfg"]
        self.assertEqual(cfg.get("width"), 1216)
        self.assertEqual(cfg.get("height"), 672)
        # model 为空不该影响宽高回填
        self.assertIsNone(cfg.get("model"))

    def test_params_take_priority_over_resolution_info(self):
        """新任务仍优先用 params.width/height（不被 resolution_info 覆盖）。"""
        job = {"mode": "t2v", "resolution_info": {"width": 1216, "height": 672,
                                                   "aspect": "16:9"}}
        params = {"aspect": "16:9", "width": 1344, "height": 768,
                  "model_key": "hybrid_fl2va_ref2va"}
        out = _run_reuse([{"job": job, "params": params, "panel": _panel()}])[0]
        cfg = out["cfg"]
        self.assertEqual(cfg.get("width"), 1344)
        self.assertEqual(cfg.get("height"), 768)
        self.assertEqual(cfg.get("size_preset_key"), "16:9_0.98")

    def test_no_size_anywhere_leaves_width_undefined(self):
        """两边都没有尺寸 -> 留空（不编造尺寸），applyConfig 会保持面板现状。"""
        job = {"mode": "t2v", "resolution_info": {"has_size": False}}
        params = {"aspect": "16:9", "duration": 10.0}
        out = _run_reuse([{"job": job, "params": params, "panel": _panel()}])[0]
        cfg = out["cfg"]
        self.assertIsNone(cfg.get("width"))
        self.assertIsNone(cfg.get("height"))
        self.assertEqual(cfg.get("size_preset_key"), "")


class TestReuseHandlerBranchCondition(unittest.TestCase):
    """处理器分支条件本身（P1 的另一半）。"""

    def test_branch_not_gated_on_model_key(self):
        """回填分支不能再被 `p.model_key` 卡住。"""
        handler = _extract_function(APP_JS, "jobCard")
        m = re.search(r'if\s*\(\s*job\.mode\s*===\s*"t2v"[^)]*\)', handler)
        assert m, "找不到 t2v 回填分支的条件"
        cond = m.group(0)
        self.assertNotIn("p.model_key", cond,
                         "回填分支不能以 model_key 为门槛：%s" % cond)
        self.assertIn("window.VIDEO_PANEL", cond,
                      "仍然必须要求视频面板存在：%s" % cond)

    def test_handler_calls_the_extracted_function(self):
        """处理器必须调用被测的纯函数（保证测试真的守住了它）。"""
        handler = _extract_function(APP_JS, "jobCard")
        self.assertIn("reuseConfigFromJob(job, p, window.VIDEO_PANEL)", handler,
                      "处理器没有调用 reuseConfigFromJob —— 测试会守不住行为")
        self.assertIn("window.VIDEO_PANEL.applyConfig(reuseCfg)", handler)


class TestReuseSamplingParamsStillReused(unittest.TestCase):
    """上一轮加的采样参数回填不能被这次重构弄丢。"""

    def test_sampling_params_are_carried(self):
        params = {"aspect": "16:9", "width": 1344, "height": 768,
                  "steps": 8, "sampler": "dpmpp_2m_sde",
                  "scheduler": "beta", "shift_video": 12, "shift_audio": 3,
                  "attention": "sol", "fixed_accel_lora": "ckpt850"}
        job = {"mode": "t2v"}
        out = _run_reuse([{"job": job, "params": params, "panel": _panel()}])[0]
        cfg = out["cfg"]
        for key in ("steps", "sampler", "scheduler", "shift_video",
                    "shift_audio", "attention", "fixed_accel_lora"):
            self.assertEqual(cfg.get(key), params[key],
                             "%s 没被复用" % key)


if __name__ == "__main__":
    unittest.main()
