# -*- coding: utf-8 -*-
"""
C 组前端修复的**真实执行**测试（Node + 极简 DOM shim）。

【为什么不用字符串断言】
「源码里有没有某个字符串」这种断言抓不住逻辑退化 ——
把白名单改成不过滤、把「无」改成空字符串，字符串断言全绿但行为是错的。
所以这里**真的把 web/job-meta.js 跑起来**（它是纯函数，无 DOM 依赖），
喂进真实形状的 job 对象，断言**渲染出来的文本**。

【覆盖】
  C1  LoRA 行只能来自真实使用的 LoRA；提示词字段名绝不能被当成 LoRA；
      没有 LoRA 时显示「无」。
  C2  卡片补齐模型/步数/调度器/采样器/尺寸/时长/seed；取不到显示「—」，
      **不得用别的字段顶替**。
  C3  详情模态框的渲染内容：完整参数 + 推荐参数 + 完整提示词；
      缺失字段显式标注。
  C4  复用参数与详情展示的参数集一致（都取权威摘要）。

运行：python3 -m pytest tests/test_job_meta_frontend.py -v
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

JOB_META_JS = (REPO / "web" / "job-meta.js").read_text(encoding="utf-8")
JOB_DETAIL_JS = (REPO / "web" / "job-detail.js").read_text(encoding="utf-8")
_APP_JS = (REPO / "web" / "app.js").read_text(encoding="utf-8")


def _extract_function(src: str, name: str) -> str:
    """抠出一个顶层 function 的源码（括号配对扫描，不用正则）。"""
    match = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    if not match:
        raise AssertionError(f"找不到 function {name}")
    start = match.start()
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
    raise AssertionError(f"function {name} 括号没配平")


#: app.js 里要抠出来测的函数
_APP_FUNCTIONS = ["jobModelInfo", "videoOptions", "localModelInfo",
                   "loraRowHtml", "modelLabel", "loraLabel",
                   "reuseConfigFromJob"]

#: job-detail.js 里要抠出来测的函数。
#: 注意：这些函数在 job-detail.js 的 **IIFE 内部**，不是全局 ——
#: 所以要把整个 IIFE 的函数体抠出来执行（见 _DETAIL_BODY）。
_DETAIL_FUNCTIONS = ["meta", "info", "t", "n", "kv", "group", "loraBlock",
                     "recBlock", "renderBody", "findJobById"]


def _detail_body(src: str) -> str:
    """把 job-detail.js 的 IIFE 拆开，让内部函数进入当前作用域。

    做法：把 `(() => {` 换成空、`})();` 换成空 —— 函数体里的
    `function` 声明与 `const` 就都变成当前作用域的顶层绑定。
    （只用于测试，不改线上文件。）
    """
    start = src.find("(() => {")
    if start < 0:
        raise AssertionError("job-detail.js 里找不到 IIFE")
    end = src.find("})();")
    if end < 0:
        raise AssertionError("job-detail.js 里找不到 IIFE 结尾")
    return src[:start] + src[start + len("(() => {"):end] + src[end + len("})();"):]

_PRELUDE = """
global.window = global;
global.document = { querySelector: () => null, addEventListener: () => {} };
global.MODE_INFO = { t2v: { name: "文生视频", kind: "video" },
                     t2a: { name: "文生音乐", kind: "audio" } };
// app.js 里的 escapeHtml（与线上同语义）
function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
"""


def _run_node(script: str, timeout: int = 60) -> dict:
    assert NODE is not None, "node 不可用"
    full = (_PRELUDE
            + "\n" + JOB_META_JS
            + "\n" + _detail_body(JOB_DETAIL_JS)
            + "\n" + "\n".join(_extract_function(_APP_JS, n) for n in _APP_FUNCTIONS)
            + "\n" + script)
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
GOOD_SUMMARY = {
    "model_key": "hybrid_fl2va_ref2va",
    "model_label": "MiniMax-H3 混合版 · FL2VA+Ref2VA",
    "model_unet": "minimax_h3_hybrid_fl2va_ref2va_b30-49-int8.safetensors",
    "lora_label": "DMAD 4 步（critic 版）",
    "lora_strength": 1.0,
    "lora_name": "dmad_minimax_h3_4step_full_critic_comfyui.safetensors",
    "lora_key": "dmad_4step_critic",
    "fixed_lora_label": "Turbo 4 步 · ckpt850（工作流固定）",
    "fixed_lora_name": "minimax_h3_turbo_4step_ema_ckpt850.safetensors",
    "fixed_lora_key": "ckpt850",
    "fixed_lora_strength": 1.0,
    "source": "t2v_panel",
    "attention_key": "sparse",
    "attention_label": "稀疏视频注意力（预算制）",
    "attention_speed_label": "速度最快",
    "steps": 4,
    "sampler": "euler",
    "scheduler": "beta",
    "shift_video": 12.0,
    "shift_audio": 3.0,
    "width": 1344,
    "height": 768,
    "aspect": "16:9",
    "duration": 5,
    "seed": 4242,
}

BOGUS = "integrated_multimodal_description"


class TestC1FrontendLora(unittest.TestCase):
    """C1：LoRA 行的取值口径。"""

    def test_prompt_field_name_never_rendered_as_lora(self):
        """即使 job.params.lora 是提示词字段名，也**不能**显示成 LoRA。"""
        job = {
            "id": "x1", "mode": "t2v", "created_at": 1700000000,
            "params": {"lora": BOGUS, "prompt": f"{BOGUS}: [Shot 1] ..."},
            # 服务端摘要里没有 LoRA（脏值已被白名单挡掉）
            "model_info": {"model_label": "MiniMax-H3 混合版 · FL2VA+Ref2VA"},
        }
        out = _run_node(f"""
const job = {json.dumps(job)};
const label = loraLabel(job);
const row = loraRowHtml(label);
console.log(JSON.stringify({{label, row}}));
""")
        self.assertNotIn(BOGUS, out["label"],
                         "提示词字段名被当成 LoRA 显示了")
        self.assertNotIn(BOGUS, out["row"])
        # 【2026-11-15 文案变更】「无」->「未使用 LoRA」（用户要求）。
        # 行为保护不变：提示词字段名**仍然不得**被当成 LoRA 显示。
        self.assertEqual(out["label"], "未使用 LoRA",
                         "没有 LoRA 时必须显示「未使用 LoRA」")
        self.assertIn("未使用 LoRA", out["row"])

    def test_real_lora_is_shown_with_strength(self):
        job = {
            "id": "x2", "mode": "t2v", "created_at": 1700000000,
            "params": {},
            "model_info": GOOD_SUMMARY,
        }
        out = _run_node(f"""
const job = {json.dumps(job)};
console.log(JSON.stringify({{
  label: loraLabel(job),
  row: loraRowHtml(loraLabel(job)),
  list: window.JOB_META.loraList(job),
}}));
""")
        self.assertIn("DMAD 4 步（critic 版）", out["label"])
        self.assertIn("×1.00", out["label"])
        self.assertIn("工作流固定：Turbo 4 步 · ckpt850", out["label"])
        self.assertEqual(len(out["list"]), 2)
        self.assertFalse(out["list"][0]["fixed"])
        self.assertTrue(out["list"][1]["fixed"])

    def test_lora_row_html_shows_none_when_empty(self):
        out = _run_node("""
console.log(JSON.stringify({
  empty: loraRowHtml(""),
  nul: loraRowHtml(null),
  undef: loraRowHtml(undefined),
  ok: loraRowHtml("DMAD"),
}));
""")
        # 【2026-11-15 文案变更】用户要求：「缺失时应显示『未使用 LoRA』
        # 而不是错名」。原来的「无」太短、也容易被读成「数据缺失」。
        for key in ("empty", "nul", "undef"):
            self.assertIn("未使用 LoRA", out[key],
                          f"{key} 应显示「未使用 LoRA」")
            self.assertNotIn("空", out[key])
        self.assertIn("DMAD", out["ok"])


class TestC2FrontendParams(unittest.TestCase):
    """C2：卡片补齐生成参数。"""

    def test_all_required_fields_present(self):
        job = {"id": "x3", "mode": "t2v", "params": {}, "model_info": GOOD_SUMMARY}
        out = _run_node(f"""
const job = {json.dumps(job)};
console.log(JSON.stringify({{
  params: window.JOB_META.generationParams(job),
  sampling: window.JOB_META.samplingText(job),
  steps: window.JOB_META.stepsText(job),
  size: window.JOB_META.sizeText(job),
  duration: window.JOB_META.durationText(job),
  seed: window.JOB_META.seedText(job),
}}));
""")
        keys = {item["key"] for item in out["params"]}
        for required in ("steps", "sampler", "scheduler", "width", "height",
                         "duration", "seed"):
            self.assertIn(required, keys, f"缺少参数 {required}")
        self.assertEqual(out["sampling"], "euler · beta")
        self.assertEqual(out["steps"], "4 · —")
        self.assertEqual(out["size"], "1344×768 · 16:9")
        self.assertEqual(out["duration"], "5s")
        self.assertEqual(out["seed"], "4242")

    def test_missing_values_are_dash_not_borrowed(self):
        """取不到的显示「—」，不得用别的字段顶替。"""
        job = {
            "id": "x4", "mode": "t2v",
            "params": {"prompt": f"{BOGUS}: ...", "duration": 9},
            "model_info": {"model_label": "X", "duration": 9},
        }
        out = _run_node(f"""
const job = {json.dumps(job)};
console.log(JSON.stringify({{
  sampling: window.JOB_META.samplingText(job),
  steps: window.JOB_META.stepsText(job),
  params: window.JOB_META.generationParams(job),
}}));
""")
        self.assertEqual(out["sampling"], "— · —")
        self.assertEqual(out["steps"], "— · —")
        for item in out["params"]:
            if item["key"] in ("sampler", "scheduler", "steps", "cfg"):
                self.assertEqual(item["value"], "—",
                                 f"{item['key']} 不该被别的字段顶替")

    def test_prompt_never_appears_in_params(self):
        job = {
            "id": "x5", "mode": "t2v",
            "params": {"prompt": f"{BOGUS}: [Shot 1] ...", "negative_prompt": "KSamplerSelect"},
            "model_info": {"model_label": "X"},
        }
        out = _run_node(f"""
const job = {json.dumps(job)};
console.log(JSON.stringify(window.JOB_META.generationParams(job)));
""")
        blob = json.dumps(out, ensure_ascii=False)
        self.assertNotIn(BOGUS, blob)
        self.assertNotIn("KSamplerSelect", blob)


class TestC3DetailModal(unittest.TestCase):
    """C3：详情模态框的内容。"""

    def test_detail_body_has_all_required_sections(self):
        job = {
            "id": "job-123", "mode": "t2v", "created_at": 1700000000,
            "gen_type_label": "文生视频",
            "params": {"prompt": "完整提示词内容 ABC"},
            "model_info": dict(GOOD_SUMMARY, model_recommendation={
                "steps": "not_provided", "scheduler": "not_provided",
                "sampler": "not_provided", "shift_video": 12.0,
                "shift_audio": 3.0, "base_model": "not_provided",
                "source_kind": "comfyui", "source": "/path/to/doc.md:1-2",
                "note": "作者未给出采样参数"}),
        }
        out = _run_node(f"""
const job = {json.dumps(job)};
console.log(JSON.stringify({{html: renderBody(job)}}));
""")
        html = out["html"]
        for label in ("模型与采样", "LoRA（任务真实使用）", "尺寸与种子",
                      "注意力机制", "完整提示词", "任务来源",
                      "主模型推荐参数"):
            self.assertIn(label, html, f"详情缺少分组：{label}")
        # 关键字段都要出现
        self.assertIn("DMAD 4 步（critic 版）", html)
        self.assertIn("×1.00", html)
        self.assertIn("euler", html)
        self.assertIn("beta", html)
        self.assertIn("12", html)     # shift
        self.assertIn("1344×768", html)
        self.assertIn("4242", html)    # seed
        self.assertIn("稀疏视频注意力（预算制）", html)
        self.assertIn("完整提示词内容 ABC", html)
        self.assertIn("job-123", html)
        # 「作者未提供」要显式出现
        self.assertIn("作者未提供", html)
        # 来源可追溯
        self.assertIn("/path/to/doc.md:1-2", html)

    def test_missing_fields_marked_explicitly(self):
        job = {"id": "j2", "mode": "t2v", "params": {},
               "model_info": {"model_label": "X"}}
        out = _run_node(f"""
const job = {json.dumps(job)};
console.log(JSON.stringify({{html: renderBody(job)}}));
""")
        html = out["html"]
        # 缺失项显示「—」
        self.assertIn("—", html)
        self.assertIn("is-missing", html)


class TestC4ReuseConsistency(unittest.TestCase):
    """C4：复用参数与详情展示的参数集一致。"""

    def test_reuse_reads_authoritative_summary(self):
        job = {
            "id": "j3", "mode": "t2v",
            "params": {"model": "lora", "lora": BOGUS,
                       "steps": None, "sampler": None, "scheduler": None},
            "model_info": GOOD_SUMMARY,
        }
        out = _run_node(f"""
const job = {json.dumps(job)};
const cfg = reuseConfigFromJob(job, job.params, {{ state: {{}}, options: {{}} }});
console.log(JSON.stringify(cfg));
""")
        cfg = out
        self.assertEqual(cfg["steps"], 4)
        self.assertEqual(cfg["sampler"], "euler")
        self.assertEqual(cfg["scheduler"], "beta")
        self.assertEqual(cfg["shift_video"], 12.0)
        self.assertEqual(cfg["shift_audio"], 3.0)
        self.assertEqual(cfg["width"], 1344)
        self.assertEqual(cfg["height"], 768)
        self.assertEqual(cfg["aspect"], "16:9")
        # LoRA 连强度一起复用
        self.assertEqual(cfg["accel_lora"], "dmad_4step_critic")
        self.assertEqual(cfg["accel_lora_strength"], 1.0)
        self.assertEqual(cfg["fixed_accel_lora"], "ckpt850")
        self.assertEqual(cfg["attention"], "sparse")
        self.assertEqual(cfg["seed"], 4242)
        # 脏值绝不能进复用配置
        self.assertNotEqual(cfg.get("accel_lora"), BOGUS)


if __name__ == "__main__":
    unittest.main()
