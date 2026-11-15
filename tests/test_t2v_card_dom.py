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
FUNCTIONS = ["jobModelInfo", "videoOptions", "localModelInfo", "loraRowHtml",
             "modelLabel", "loraLabel",
             # 【2026-11-13】loraLabel 现在会调 isLoraKeyLike 做前端兜底白名单
             # （挡住「提示词冒充 LoRA」）。它是 loraLabel 的直接依赖，
             # 必须一起抠出来 —— 否则 loraLabel 在 Node 里会 ReferenceError。
             "isLoraKeyLike"]

_EXTRA_JS = """
// ---- app.js 里被测函数依赖的极简替身 ----
// IMG_OPTIONS / MOSS_OPTIONS / QWEN_OPTIONS 是 app.js 顶层的清单容器，
// 被测函数会读它们；这里给出与服务端同形的最小清单。
const IMG_OPTIONS = { models: {
  "z_image_turbo_bf16": { label: "Z-Image-Turbo · 官方加速版" },
} };
const MOSS_OPTIONS = { models: {} };
const QWEN_OPTIONS = { models: {} };
// 【重要】这里**刻意不**定义 VIDEO_OPTIONS ——
// 在真浏览器里 web/video-panel.js 的 `const VIDEO_OPTIONS` 是 **IIFE 内部**的，
// app.js（另一个 <script>）根本看不到它。
// 如果这里给它定义一个全局，就等于替被测代码把 bug 遮住了。
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

    def test_only_fixed_lora_shows_only_that_one(self):
        """没额外挂加速 LoRA -> 只列**工作流固定**的那枚。

        【2026-11-13 契约变更 —— 需求 C1】
        旧契约：显示「空」标记（表示「用户没额外挂」）。
        新契约（用户 2026-11-13 C1 原文）：
          「LoRA 行只能来自任务真实使用的 LoRA 列表（可含名称+强度），
            没有 LoRA 时显示「无」」
        -> 本例**确实用了**一枚 LoRA（工作流固定的 Turbo），
           所以列表非空 -> 显示那一枚，**不再**显示「空」标记。
        「无」只用于**一个 LoRA 都没有**的情况。
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
        self.assertNotIn("空", out["lora"],
                        "新契约下不再用「空」标记")
        self.assertNotIn("DMAD", out["lora"])
        # 固定那枚仍要标明（它确实被用了）
        self.assertIn("工作流固定", out["lora"])
        # 仍然渲染这一行（用户能看见实际用了哪枚）
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
window.VIDEO_PANEL = {{ options: {json.dumps(video_options)} }};
console.log(JSON.stringify({{
  model: modelLabel({json.dumps(job)}),
  lora: loraLabel({json.dumps(job)}),
}}));
""")
        self.assertIn("混合版", out["model"])
        self.assertNotIn("ckpt850", out["model"])
        self.assertIn("DMAD 4 步（critic 版）", out["lora"])

    def test_local_fallback_does_not_crash_without_video_panel(self):
        """本地兜底**不能**依赖全局变量。

        【真实踩过的坑】清单在 web/video-panel.js 里是 IIFE 内部的
        `const VIDEO_OPTIONS` —— 不是全局。第一版兜底直接写 VIDEO_OPTIONS，
        在浏览器里抛 ReferenceError，把整个 app.js 带崩
        （实测症状：生成库的骨架卡片不出现，boot() 把异常静默吞掉）。

        这条断言用**真浏览器**跑不到（app.js 是顶层脚本，异常会中断整个文件），
        所以这里在 Node 里模拟「全局不存在」的情形：
        兜底必须**不抛异常**，并返回空摘要而不是崩掉。
        """
        job = {"mode": "t2v", "params": HYBRID_PARAMS}
        out = _run_node(f"""
// 刻意**不**设置 window.VIDEO_PANEL —— 模拟清单还没加载完的情形
console.log(JSON.stringify({{
  model: modelLabel({json.dumps(job)}),
  lora: loraLabel({json.dumps(job)}),
}}));
""")
        # 关键：不抛异常、且返回可渲染的字符串
        self.assertIsInstance(out["model"], str)
        self.assertIsInstance(out["lora"], str)

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

    def test_qwen_cards_must_render_model_row(self):
        """Qwen 系卡片**必须有**「模型」行。

        【实测抓到的错 —— 子代理审计 M3】卡片模板的图像分支原写死
        `job.mode === "t2i" || job.mode === "t2i_moss"`，
        而 Qwen-Image 2.1 的两个模式（t2i_qwen / i2i_qwen）落到最后的
        else 分支 —— 那里只渲染 `info.kind === "video"` 的内容，
        于是 Qwen 卡片**整块没有模型行**：
        面板有真实模型下拉、服务端也读、落库也有，唯独展示层缺失。

        判据：四个图像模式必须共用 IMAGE_MODES 这个常量，
        不许再手写两个 mode 的字面量。
        """
        import re
        from test_t2v_card_dom import _APP_JS
        code = re.sub(r"//[^\n]*", "", _APP_JS)
        # 卡片模板里不得再出现「只判 t2i / t2i_moss」的写法
        self.assertNotIn('job.mode === "t2i" || job.mode === "t2i_moss"',
                         code)
        # 必须用 IMAGE_MODES（覆盖全部四个图像模式）
        self.assertIn("IMAGE_MODES.includes(job.mode)", code)

    def test_image_mode_without_lora_shows_none(self):
        """图像模式没加效果 LoRA -> 显示「无」（新契约 C1）。

        【2026-11-13 契约变更 —— 需求 C1】
        旧契约：图像模式**不渲染** LoRA 行（图像的效果 LoRA 是
        「有就加、没有就不加」的可选列表，每张图挂一行是噪声）。
        新契约（用户 2026-11-13 C1 原文）：
          「生成媒体库卡片（以及**视频/图片/音频各类卡片**）的元数据
            必须按字段语义白名单渲染 …… 没有 LoRA 时显示「无」」
        -> 各类卡片的口径统一：没有 LoRA 就显示「无」，
           不再按模式分叉（分叉本身就是「按位置判断」的老毛病）。
        """
        info = {"model_key": "z_image_turbo_bf16",
                "model_label": "Z-Image-Turbo · 官方加速版",
                "lora_label": "", "lora_strength": None,
                "fixed_lora_label": "", "fixed_lora_name": ""}
        job = {"mode": "t2i", "params": {"model": "z_image_turbo_bf16"},
               "model_info": info}
        out = _run_node(f"""
console.log(JSON.stringify({{
  lora: loraLabel({json.dumps(job)}),
  row: loraRowHtml(loraLabel({json.dumps(job)})),
}}));
""")
        self.assertEqual(out["lora"], "无")
        self.assertIn("无", out["row"])
        self.assertIn("<span>LoRA</span>", out["row"],
                      "图像卡也要有 LoRA 行（显示「无」）")

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
        """抠出提交层的 params 构造块（含 flf/r2v 的 lora 分支）。

        【2026-11-15 修复③ 交付项 3 更新】
        三个视频模式共用同一套面板后，提交层的结构变成：
            if (mode === "t2v" && ...) { ... }
            else if (mode === "flf" || mode === "r2v") { ... }   ← lora 分支在这
            if (IMAGE_MODES.includes(mode)) { ... }
        旧的 `end` 标记（IMAGE_MODES 那行）落在 flf/r2v 分支**之后**，
        会把要断言的分支整段切掉 —— 所以改用「params.lora 赋值之后」
        作为终点。终点取图片编辑的参考图提交块起点。
        """
        start = _APP_JS.index("const params = {\n    prompt,")
        # 终点：图片编辑（i2i_*）的参考图提交块 —— 它在 flf/r2v 分支之后。
        end = _APP_JS.index('if (mode === "i2i_qwen" || mode === "i2i_h3image") {',
                            start)
        return _APP_JS[start:end]

    @staticmethod
    def _strip_comments(src: str) -> str:
        code = re.sub(r"//[^\n]*", "", src)
        return re.sub(r"/\*.*?\*/", "", code, flags=re.S)

    def test_legacy_lora_only_in_flf_r2v_branch(self):
        """params.lora 赋值必须落在 flf / r2v 的守卫块**内部**。

        判据：块里每一条 `params.lora` 赋值语句，都必须出现在
        flf / r2v 的守卫块之后，且该守卫本身存在。
        守卫被删掉（= 又变成对所有模式无条件写）时，本条断言必须红。

        【2026-11-15 修复③ 交付项 3 更新】
        守卫块的写法从 `if (mode === "flf" || mode === "r2v") {`
        变成了 `} else if (mode === "flf" || mode === "r2v") {`
        —— 因为三个视频模式现在共用同一套面板，t2v 那一支自己也是一个
        `if` 分支，flf/r2v 成了它的 `else if`。
        语义**完全不变**：仍然只有 flf / r2v 写 params.lora。
        """
        code = self._strip_comments(self._submit_block())
        guard = 'else if (mode === "flf" || mode === "r2v") {'
        self.assertIn(
            guard, code,
            "flf/r2v 的守卫必须存在 —— 没有它，遗留 lora 字段会回到所有模式")
        guard_at = code.index(guard)
        # 只认**遗留**字段 params.lora / params.lora_strength ——
        # params.loras（图像模式的**效果 LoRA 数组**，复数）是另一个字段，
        # 不能被当成遗留字段误判。
        lora_lines = [ln.strip() for ln in code.splitlines()
                      if re.search(r"params\.lora(_strength)?\b", ln)
                      and "params.loras" not in ln]
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
        guard = 'else if (mode === "flf" || mode === "r2v") {'
        self.assertIn(guard, code)
        outside = code[:code.index(guard)]
        # 只查**遗留**字段（params.loras 是图像模式的效果 LoRA 数组，
        # 与遗留的 params.lora 不是一回事）。
        self.assertIsNone(re.search(r"params\.lora(_strength)?\b", outside),
                          "守卫块之前不允许出现遗留的 params.lora")


if __name__ == "__main__":
    unittest.main()
