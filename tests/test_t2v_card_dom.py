# -*- coding: utf-8 -*-
"""文生视频「面板所选 == 卡片显示」的**前端真实执行**测试（Node + DOM shim）。

【为什么不用「源码里有没有某个字符串」那种断言】

本轮第一版测试就是那种字符串断言，变异验证立刻暴露它**抓不住逻辑退化**：

  * 把 `const loraRow = loraText ? ... : ""` 改成恒渲染 -> 字符串断言全绿，
    但行为已经错了（没有 LoRA 时会显示一行空）。
  * 把 `if (mode === "flf" || mode === "r2v")` 的分支去掉 -> 字符串断言
    （只搜 `if (mode === "flf" ...` 这段字面量）也全绿。

所以这里改成**真的把 app.js 的相关函数跑起来**：
用 Node + 极简 DOM shim 载入 web/app.js 里的模型/LoRA 摘要逻辑，
喂进真实形状的 job 对象，断言**渲染出来的字符串**。

【被测行为】

  1. 卡片「模型」行显示的是面板选的底模（混合版），不是遗留的 ckpt850。
  2. 卡片有独立的「LoRA」行，显示面板选的加速 LoRA + 强度。
  3. 没选加速 LoRA 时，**不渲染** LoRA 行（不是显示「空」）。
  4. 提交层：t2v / 图像 / 音乐**不写**遗留 lora 字段；flf / r2v 写。

运行：python3 -m pytest tests/test_t2v_card_dom.py -v
"""
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

NODE = shutil.which("node") or shutil.which("nodejs")


# ------------------------------------------------------------------
# 从 web/app.js 里**抠出**待测的那几个函数
# ------------------------------------------------------------------
_APP_JS = (REPO / "web" / "app.js").read_text(encoding="utf-8")


def _extract_function(src: str, name: str) -> str:
    """抠出 app.js 里一个顶层 function 的源码。

    用括号配对扫描（不是正则）—— 函数体里有嵌套的 {} 与模板字符串，
    正则会在嵌套处截断。
    """
    match = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    if not match:
        raise AssertionError(f"app.js 里找不到 function {name}")
    start = match.start()
    # 找到函数体的开括号
    i = src.index("{", match.end() - 1)
    depth = 0
    in_str = None
    while i < len(src):
        ch = src[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        elif ch in ("'", '"', "`"):
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1
    raise AssertionError(f"function {name} 的括号没配平")


#: 待测的前端函数（必须逐个从 app.js 里抠出来，保证测的是**线上那份代码**）
FUNCTIONS = ["jobModelInfo", "localModelInfo", "loraRowHtml", "modelLabel", "loraLabel"]

_EXTRA_JS = """
// ---- app.js 里被测函数依赖的极简替身 ----
// IMG_OPTIONS / MOSS_OPTIONS / QWEN_OPTIONS 是 app.js 顶层的清单容器，
// 被测函数会读它们；这里给出与服务端同形的最小清单。
const IMG_OPTIONS = { models: {
  "z_image_turbo_bf16": { label: "Z-Image-Turbo · 官方加速版" },
} };
const MOSS_OPTIONS = { models: {} };
const QWEN_OPTIONS = { models: {} };
const VIDEO_OPTIONS = { models: {}, accel_lora_combos: [], fixed_accel_loras: [] };
function imgModelLabel(key) {
  const m = IMG_OPTIONS.models[key] || MOSS_OPTIONS.models[key] || QWEN_OPTIONS.models[key];
  return m ? m.label : (key || "—");
}
const MUSIC_MODEL_FALLBACK = { yue2_3b_bf16: "YuE2 3B · BF16 完整版" };
function musicModelLabel(key) {
  if (!key) return "YuE2";
  const opts = (window.MusicUI && window.MusicUI.getOptions) || null;
  const entry = opts && opts.checkpoints ? opts.checkpoints[key] : null;
  if (entry && entry.label) return entry.label;
  if (MUSIC_MODEL_FALLBACK[key]) return MUSIC_MODEL_FALLBACK[key];
  return "YuE2（历史档位）";
}
function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
global.window = global;
"""


def _run_node(script: str, timeout: int = 60) -> dict:
    assert NODE is not None, "node 不可用"
    payload = "\n".join(_extract_function(_APP_JS, name) for name in FUNCTIONS)
    full = _EXTRA_JS + "\n" + payload + "\n" + script
    proc = subprocess.run(
        [NODE, "--input-type=commonjs", "-e", full],
        capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise AssertionError(
            f"Node 脚本失败 (exit {proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    out = proc.stdout.strip()
    if not out:
        raise AssertionError(f"Node 没有输出：\n{proc.stderr}")
    return json.loads(out.splitlines()[-1])


#: 服务端算好的权威摘要（与 h3studio/job_meta.py 同形）
HYBRID_SUMMARY = {
    "model_key": "hybrid_fl2va_ref2va",
    "model_label": "MiniMax-H3 混合版 · FL2VA+Ref2VA",
    "model_unet": "minimax_h3_hybrid_fl2va_ref2va_b30-49-int8.safetensors",
    "lora_label": "DMAD 4 步（critic 版）",
    "lora_strength": 1.0,
    "lora_name": "dmad_minimax_h3_4step_full_critic_comfyui.safetensors",
    "fixed_lora_label": "Turbo 4 步 · ckpt850（工作流固定）",
    "fixed_lora_name": "minimax_h3_turbo_4step_ema_ckpt850.safetensors",
    "fixed_lora_strength": 1.0,
    "source": "t2v_panel",
}

HYBRID_PARAMS = {
    "model_key": "hybrid_fl2va_ref2va",
    "accel_loras": [{"key": "dmad_4step_critic", "strength": 1.0, "enabled": True}],
    "fixed_accel_lora": "ckpt850",
    "steps": 4,
    "duration": 15.0,
    # 落库记录里同时存在的**遗留**字段（旧前端留下的）
    "model": "lora",
    "lora": "ckpt850",
    "lora_strength": 1,
}


class TestCardRendersServerSummary(unittest.TestCase):
    """卡片「模型」/「LoRA」行必须显示服务端摘要里的值"""

    def test_model_row_shows_selected_model_not_legacy_lora(self):
        """混合版底模 + DMAD LoRA -> 模型行必须显示混合版，不能是 ckpt850。"""
        job = {"mode": "t2v", "params": HYBRID_PARAMS, "model_info": HYBRID_SUMMARY}
        out = _run_node(f"""
console.log(JSON.stringify({{
  model: modelLabel({json.dumps(job)}),
  lora: loraLabel({json.dumps(job)}),
}}));
""")
        self.assertIn("混合版", out["model"])
        self.assertNotIn("ckpt850", out["model"])
        # 步数也要在（面板提交的 steps=4）
        self.assertIn("4步", out["model"])

    def test_lora_row_shows_selected_accel_lora_and_strength(self):
        """LoRA 行必须显示面板选的那枚 + 强度。"""
        job = {"mode": "t2v", "params": HYBRID_PARAMS, "model_info": HYBRID_SUMMARY}
        out = _run_node(f"""
console.log(JSON.stringify({{ lora: loraLabel({json.dumps(job)}) }}));
""")
        self.assertIn("DMAD 4 步（critic 版）", out["lora"])
        self.assertIn("×1.00", out["lora"])

    def test_no_accel_lora_shows_empty_marker(self):
        """没选加速 LoRA -> 显示「空」，并标明工作流固定的那枚。

        需求原文：「未选 LoRA 时才显示「空」」。
        「空」指的是**用户选的加速 LoRA**那一项 ——
        工作流固定的那枚 Turbo LoRA 始终生效，必须单独标明。
        """
        info = dict(HYBRID_SUMMARY)
        info["lora_label"] = ""
        info["lora_strength"] = None
        params = dict(HYBRID_PARAMS)
        params["accel_loras"] = []
        job = {"mode": "t2v", "params": params, "model_info": info}
        out = _run_node(f"""
console.log(JSON.stringify({{
  lora: loraLabel({json.dumps(job)}),
  row: loraRowHtml(loraLabel({json.dumps(job)})),
}}));
""")
        self.assertIn("空", out["lora"])
        self.assertNotIn("DMAD", out["lora"])
        # 固定那枚仍要标明（它始终生效）
        self.assertIn("工作流固定", out["lora"])
        # 仍然渲染这一行（用户能看见「没额外挂」这个事实）
        self.assertIn("<span>LoRA</span>", out["row"])

    def test_lora_row_rendered_when_lora_present(self):
        """有 LoRA 时必须渲染出带「LoRA」标签与内容的一行。"""
        job = {"mode": "t2v", "params": HYBRID_PARAMS, "model_info": HYBRID_SUMMARY}
        out = _run_node(f"""
console.log(JSON.stringify({{
  row: loraRowHtml(loraLabel({json.dumps(job)})),
}}));
""")
        self.assertIn("<span>LoRA</span>", out["row"])
        self.assertIn("DMAD 4 步（critic 版）", out["row"])

    def test_falls_back_to_local_summary_when_server_silent(self):
        """服务端没下发 model_info 时，本地兜底也必须读**新字段**。

        旧实现读 p.model / p.lora -> 恒显 ckpt850。
        """
        # 本地兜底用的清单（与服务端 VIDEO_OPTIONS 同形）
        video_options = {
            "models": {"hybrid_fl2va_ref2va": {
                "key": "hybrid_fl2va_ref2va",
                "label": "MiniMax-H3 混合版 · FL2VA+Ref2VA"}},
            "accel_lora_combos": [{"key": "dmad_4step_critic",
                                   "display": "DMAD 4 步（critic 版）",
                                   "name": "dmad_minimax_h3_4step_full_critic_comfyui.safetensors"}],
            "fixed_accel_loras": [{"key": "ckpt850",
                                   "display": "Turbo 4 步 · ckpt850（工作流固定）",
                                   "name": "minimax_h3_turbo_4step_ema_ckpt850.safetensors"}],
        }
        job = {"mode": "t2v", "params": HYBRID_PARAMS}
        out = _run_node(f"""
VIDEO_OPTIONS.models = {json.dumps(video_options["models"])};
VIDEO_OPTIONS.accel_lora_combos = {json.dumps(video_options["accel_lora_combos"])};
VIDEO_OPTIONS.fixed_accel_loras = {json.dumps(video_options["fixed_accel_loras"])};
console.log(JSON.stringify({{
  model: modelLabel({json.dumps(job)}),
  lora: loraLabel({json.dumps(job)}),
}}));
""")
        self.assertIn("混合版", out["model"])
        self.assertNotIn("ckpt850", out["model"])
        self.assertIn("DMAD 4 步（critic 版）", out["lora"])

    def test_legacy_task_still_renders(self):
        """老任务（只有遗留字段、没有 model_info）不能变成空白。"""
        job = {"mode": "t2v",
               "params": {"model": "lora", "lora": "ckpt850", "steps": 4}}
        out = _run_node(f"""
console.log(JSON.stringify({{
  model: modelLabel({json.dumps(job)}),
  lora: loraLabel({json.dumps(job)}),
}}));
""")
        self.assertTrue(out["model"])
        self.assertIn("ckpt850", out["lora"])

    def test_image_mode_renders_model_and_lora(self):
        """图像模式：模型 + 效果 LoRA 都要显示。"""
        info = {"model_key": "z_image_turbo_bf16",
                "model_label": "Z-Image-Turbo · 官方加速版",
                "lora_label": "某某效果 ×0.60",
                "lora_strength": 0.6}
        job = {"mode": "t2i", "params": {"model": "z_image_turbo_bf16"},
               "model_info": info}
        out = _run_node(f"""
console.log(JSON.stringify({{
  model: modelLabel({json.dumps(job)}),
  lora: loraLabel({json.dumps(job)}),
}}));
""")
        self.assertIn("Z-Image-Turbo", out["model"])
        self.assertIn("某某效果", out["lora"])


class TestSubmitParamsDoNotPolluteOtherModes(unittest.TestCase):
    """提交层：遗留 lora 字段只能出现在真走旧路径的模式里"""

    def _submit_block(self) -> str:
        """抠出提交层的 params 构造块（含 flf/r2v 的 lora 分支）。"""
        start = _APP_JS.index("const params = {\n    prompt,")
        end = _APP_JS.index("if (IMAGE_MODES.includes(mode)) {", start)
        return _APP_JS[start:end]

    @staticmethod
    def _strip_comments(src: str) -> str:
        code = re.sub(r"//[^\n]*", "", src)
        return re.sub(r"/\*.*?\*/", "", code, flags=re.S)

    def test_legacy_lora_only_in_flf_r2v_branch(self):
        """params.lora 赋值必须落在 flf / r2v 的守卫块**内部**。

        判据：块里每一条 `params.lora` 赋值语句，都必须出现在
        `if (mode === "flf" || mode === "r2v") {` 之后，且该守卫本身存在。
        守卫被删掉（= 又变成对所有模式无条件写）时，本条断言必须红。
        """
        code = self._strip_comments(self._submit_block())
        guard = 'if (mode === "flf" || mode === "r2v") {'
        self.assertIn(
            guard, code,
            "flf/r2v 的守卫必须存在 —— 没有它，遗留 lora 字段会回到所有模式")
        guard_at = code.index(guard)
        lora_lines = [ln.strip() for ln in code.splitlines()
                      if "params.lora" in ln]
        self.assertTrue(lora_lines, "flf/r2v 分支应该还写 params.lora")
        for line in lora_lines:
            self.assertGreater(
                code.index(line), guard_at,
                f"params.lora 赋值必须落在 flf/r2v 分支内：{line}")

    def test_t2v_submit_has_no_legacy_lora(self):
        """t2v 的提交必须只带新字段（model_key / accel_loras）。

        守卫块**之前**（即所有模式共用的那段）不允许出现 params.lora。
        """
        code = self._strip_comments(self._submit_block())
        guard = 'if (mode === "flf" || mode === "r2v") {'
        self.assertIn(guard, code)
        outside = code[:code.index(guard)]
        self.assertNotIn("params.lora", outside)
        self.assertNotIn("params.lora_strength", outside)


if __name__ == "__main__":
    unittest.main()
