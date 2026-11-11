# -*- coding: utf-8 -*-
"""文生视频面板**前端逻辑**的真实执行测试（Node + DOM shim）。

为什么要这个文件：
  之前的断言全是「源码里有没有某个字符串」，那种测试**抓不住逻辑退化**
  —— 实测把 `const status = m.status || ...` 改成 `const unavailable =
  m.available === false`，字符串断言全绿，但行为已经错了
  （status 字段被忽略，外置未挂载的项不会被正确禁用/标注）。

  这里用 Node 把 video-panel.js 真的跑起来：造一个最小 DOM，
  把 /api/video-options 的返回喂进去，断言**渲染出来的东西**。

不依赖 npm 包（不装 jsdom）：自己写一个够用的 DOM shim
（createElement / appendChild / textContent / disabled / value / innerHTML）。

运行：python3 -m pytest tests/test_video_panel_model_dom.py -v
"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

NODE = shutil.which("node") or shutil.which("nodejs")


# ------------------------------------------------------------------
# DOM shim + video-panel.js 载入器
# ------------------------------------------------------------------
_DOM_SHIM = r"""
// ---- 极简 DOM shim（够 video-panel.js 的模型区用）----
class FakeElement {
  constructor(tag) {
    this.tagName = (tag || "div").toUpperCase();
    this.children = [];
    this._text = "";
    this.className = "";
    this.hidden = false;
    this.disabled = false;
    this._value = "";
    this.title = "";
    this.dataset = {};
    this.style = {};
    this._listeners = {};
  }
  appendChild(child) { this.children.push(child); return child; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() {
    if (this.children.length) {
      return this.children.map(c => c.textContent).join("");
    }
    return this._text;
  }
  set innerHTML(v) { this._text = ""; this.children = []; }
  get innerHTML() { return this._text; }
  // 真 DOM 的 select：value 跟随「selected 的那个 option」
  get value() {
    if (this.tagName === "SELECT") {
      const hit = this.children.find(c => c.selected);
      if (hit) return hit.value;
    }
    return this._value;
  }
  set value(v) {
    this._value = String(v == null ? "" : v);
    if (this.tagName === "SELECT") {
      this.children.forEach(c => {
        if (c.tagName === "OPTION") c.selected = (c.value === this._value);
      });
    }
  }
  addEventListener(type, fn) {
    (this._listeners[type] = this._listeners[type] || []).push(fn);
  }
  dispatch(type, ev) {
    (this._listeners[type] || []).forEach(fn => fn(ev || {target: this}));
  }
  querySelectorAll() { return []; }
  querySelector() { return null; }
}

const REGISTRY = {};

global.makeRegistry = function (ids) {
  ids.forEach(id => {
    // 真 DOM 里这些 id 对应的是 select / input —— tagName 会影响行为
    // （select 的 value 跟随 selected option），所以这里按名字推断。
    const tag = /Select$/.test(id) ? "select"
             : /Width$|Height$|Seed$/.test(id) ? "input"
             : "div";
    REGISTRY[id] = new FakeElement(tag);
  });
  return REGISTRY;
};
global.REGISTRY = REGISTRY;
global.FakeElement = FakeElement;

// ---- 模拟 fetch：/api/video-options 返回指定 payload ----
// 注意：必须挂在 global 上（下面 fetch 闭包读的就是 global.VIDEO_OPTIONS_PAYLOAD）
global.VIDEO_OPTIONS_PAYLOAD = null;
global.fetch = async function (url) {
  if (String(url).indexOf("/api/video-options") === 0) {
    return {
      ok: true,
      status: 200,
      json: async () => global.VIDEO_OPTIONS_PAYLOAD,
    };
  }
  return { ok: true, status: 200, json: async () => ({}) };
};

global.window = global;
global.document = {
  getElementById: id => REGISTRY[id] || null,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: tag => new FakeElement(tag),
  addEventListener: () => {},
};
global.$ = sel => REGISTRY[sel.replace(/^[#]/, "")] || null;
global.state = { duration: 5, steps: 4 };
"""


def _run_node(script: str, timeout: int = 60) -> dict:
    """在 Node 里跑一段脚本，返回 stdout 解析出的 JSON。"""
    node = NODE
    assert node is not None, "node 不可用"
    tmp = Path("/tmp/h3-model-dom-test")
    tmp.mkdir(parents=True, exist_ok=True)
    shim = tmp / "shim.js"
    shim.write_text(_DOM_SHIM, encoding="utf-8")
    proc = subprocess.run(
        [node, "--input-type=commonjs", "-e",
         f"require({str(shim)!r});\n" + script],
        capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise AssertionError(
            f"Node 脚本失败 (exit {proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    out = proc.stdout.strip()
    if not out:
        raise AssertionError(f"Node 没有输出：\n{proc.stderr}")
    return json.loads(out.splitlines()[-1])


def _load_panel_js() -> str:
    return (REPO / "web" / "video-panel.js").read_text(
        encoding="utf-8", errors="ignore")


def _render(payload: dict, ids=None) -> dict:
    """把指定 payload 喂给 video-panel.js，渲染模型区，返回可断言的状态。"""
    ids = ids or ["vidModelSelect", "vidModelNote", "vidModelWarn",
                  "vidModelSourceNote", "vidAccelLoraSelect",
                  "vidAccelLoraNote", "vidAccelLoraStrength",
                  "vidAccelLoraStrengthVal", "vidAccelLoraStrengthField",
                  "vidAttentionSelect", "vidAttentionNote",
                  "vidTwopassEnabled", "vidTwopassBody", "vidTwopassNote",
                  "vidTwopassHint", "vidUpscalerSelect", "vidTransition",
                  "vidTransitionVal", "vidLowres", "vidLowresVal",
                  "vidRho", "vidRhoVal", "vidAspectChips", "vidWidth",
                  "vidHeight", "vidSizeHint", "vidSizeNote", "vidSizeMode",
                  "vidSizeAdjust", "vidSizePresetSelect", "vidSizePresetNote",
                  "vidFixedLoraNote", "vidSizeReset", "seed"]
    script = f"""
makeRegistry({json.dumps(ids)});
VIDEO_OPTIONS_PAYLOAD = {json.dumps(payload)};
{_load_panel_js()}
(async () => {{
  await window.VIDEO_PANEL.load();
  const sel = REGISTRY["vidModelSelect"];
  const opts = sel.children.map(o => ({{
    value: o.value,
    text: o.textContent,
    disabled: o.disabled,
    title: o.title,
  }}));
  console.log(JSON.stringify({{
    selected: sel.value,
    options: opts,
    note: REGISTRY["vidModelNote"].textContent,
    warn: REGISTRY["vidModelWarn"].textContent,
    sourceNote: REGISTRY["vidModelSourceNote"].textContent,
    sourceClass: REGISTRY["vidModelSourceNote"].className,
    stateModel: window.VIDEO_PANEL.state.model,
    submitted: window.VIDEO_PANEL.submitParams().model_key,
  }}));
}})();
"""
    return _run_node(script)


# ------------------------------------------------------------------
# 构造 payload（与服务端 options_payload 同形）
# ------------------------------------------------------------------
def _model(key, label, status, note, warn="", comfy_listed=None,
           unet=None, external=False):
    return {
        "key": key,
        "label": label,
        "status": status,
        "available": status == "available",
        "status_note": note,
        "warn": warn,
        "comfy_listed": comfy_listed,
        "on_external_disk": external,
        "unet": unet or (key + ".safetensors"),
        "size_mb": 1024,
        "location": "/mnt/nvme1t_colorful/comfyui-models/diffusion_models"
                    if external else "/home/ubuntu/comfyui/models/diffusion_models",
        "note": "说明",
        "steps": 4, "sampler": "euler", "scheduler": "beta", "cfg": 1.0,
    }


def _payload(models, source="comfyui_api", error="", counts=None):
    return {
        "models": models,
        "default_model": models[0]["key"] if models else "",
        "fixed_accel_loras": [],
        "accel_lora_combos": [],
        "default_fixed_accel_lora": "",
        "attention_modes": [],
        "default_attention": "",
        "twopass": {"upscalers": [], "upscaler_default": "",
                    "node_available": True},
        "aspects": [{"key": "16:9", "ratio": [16, 9]}],
        "size_multiple": 32, "size_min": 32, "size_max": 16384,
        "mp_table": [0.4], "default_mp": 0.4,
        "size_presets": [],
        "accel_lora_single_select": True, "accel_lora_default": "",
        "accel_lora_compat": {},
        "model_list_source": source,
        "model_list_error": error,
        "model_list_counts": counts or {"diffusion_models": 151, "loras": 686},
    }


@unittest.skipIf(NODE is None, "本机没有 node")
class TestVideoPanelModelRendering(unittest.TestCase):
    """前端渲染行为（真跑 JS，不是查字符串）。"""

    def test_available_model_renders_plain_label(self):
        """可用模型：下拉里不加任何「不可用」后缀，红字不出现否定断言。"""
        payload = _payload([
            _model("m1", "MiniMax-H3 FL2VA · 剪枝 int8（默认）", "available",
                   "ComfyUI 模型列表已收录该权重，可直接选用。",
                   comfy_listed=True),
        ])
        out = _render(payload)
        self.assertEqual(len(out["options"]), 1)
        self.assertEqual(out["options"][0]["text"], "MiniMax-H3 FL2VA · 剪枝 int8（默认）")
        self.assertFalse(out["options"][0]["disabled"])
        self.assertNotIn("不可用", out["options"][0]["text"])
        self.assertNotIn("外置未挂载", out["options"][0]["text"])

    def test_available_model_warn_has_no_negative_claim(self):
        """可用模型的红字里**不能**出现「不可选 / 未挂载 / 获取不到」。"""
        payload = _payload([
            _model("m1", "模型A", "available",
                   "ComfyUI 模型列表已收录该权重，可直接选用。",
                   warn="体积比较大", comfy_listed=True),
        ])
        out = _render(payload)
        for phrase in ("获取不到", "未挂载", "不可选", "根本没有"):
            self.assertNotIn(phrase, out["warn"],
                             f"可用模型的红字不该出现「{phrase}」")
        # 但与可用性无关的固有提醒要留着
        self.assertIn("体积比较大", out["warn"])
        self.assertIn("已确认在 ComfyUI 模型列表中可用", out["warn"])

    def test_external_missing_is_disabled_and_labelled(self):
        """外置未挂载：该项必须**禁用**且标注「外置未挂载，当前不可选」。"""
        payload = _payload([
            _model("m1", "模型A", "external_missing",
                   "权重位于外置盘 /mnt/x，但 ComfyUI 的模型列表里没有它 —— "
                   "当前不可选。", comfy_listed=False, external=True),
        ])
        out = _render(payload)
        self.assertTrue(out["options"][0]["disabled"])
        self.assertIn("外置未挂载，当前不可选", out["options"][0]["text"])

    def test_external_missing_note_is_shown(self):
        """不可用时红字要显示服务端的 status_note（如实说明）。"""
        note = ("权重位于外置盘 /mnt/x，但 ComfyUI 的模型列表里没有它 —— "
                "外置盘可能未挂载或未扫描到，当前不可选。")
        payload = _payload([
            _model("m1", "模型A", "external_missing", note,
                   comfy_listed=False, external=True),
        ])
        out = _render(payload)
        self.assertIn("外置盘", out["warn"])
        self.assertIn("当前不可选", out["warn"])

    def test_unavailable_model_is_not_selected(self):
        """当前选中的模型不可用 -> 自动回落到**可用**的那一个。"""
        payload = _payload([
            _model("bad", "外置权重", "external_missing", "外置未挂载。",
                   comfy_listed=False, external=True),
            _model("good", "本机权重", "available", "已收录。",
                   comfy_listed=True),
        ])
        out = _render(payload)
        self.assertEqual(out["selected"], "good")
        self.assertEqual(out["stateModel"], "good")

    def test_dropdown_value_equals_submitted_model_key(self):
        """核心不变量：**下拉显示的 == 提交用的**。"""
        payload = _payload([
            _model("m1", "模型A", "available", "已收录。", comfy_listed=True),
            _model("m2", "模型B", "available", "已收录。", comfy_listed=True),
        ])
        out = _render(payload)
        self.assertEqual(out["selected"], out["submitted"],
                         "下拉选中的值与提交用的 model_key 必须一致")
        self.assertEqual(out["selected"], out["stateModel"])

    def test_status_field_drives_disabled_state(self):
        """禁用状态由 status 决定 —— 即使 available 字段被写错。

        这条专门防「前端只看 m.available」的退化：
        服务端 status=external_missing 但 available 误写为 true 时，
        前端仍必须禁用该项。
        """
        payload = _payload([
            _model("m1", "模型A", "external_missing", "外置未挂载。",
                   comfy_listed=False, external=True),
        ])
        payload["models"][0]["available"] = True      # 故意写错
        out = _render(payload)
        self.assertTrue(out["options"][0]["disabled"],
                        "status=external_missing 时必须禁用（不能只看 available）")

    def test_source_note_shows_api_counts(self):
        """正常态：来源说明显示 ComfyUI API + 条目数。"""
        payload = _payload([
            _model("m1", "模型A", "available", "已收录。", comfy_listed=True),
        ])
        out = _render(payload)
        self.assertIn("ComfyUI 模型列表 API", out["sourceNote"])
        self.assertIn("151", out["sourceNote"])
        self.assertEqual(out["sourceClass"], "img-note")

    def test_source_note_warns_on_fallback(self):
        """降级态：红字说明用的是磁盘扫描 + 原因。"""
        payload = _payload([
            _model("m1", "模型A", "available", "已收录。", comfy_listed=True),
        ], source="filesystem_fallback",
           error="连不上 ComfyUI（Connection refused）")
        out = _render(payload)
        self.assertIn("未能连上 ComfyUI 模型列表", out["sourceNote"])
        self.assertIn("Connection refused", out["sourceNote"])
        self.assertIn("磁盘扫描", out["sourceNote"])
        self.assertEqual(out["sourceClass"], "img-warn")

    def test_fallback_error_is_truncated_in_ui(self):
        """降级原因很长时，界面只显示第一条（完整原因留给接口）。"""
        long_err = ("A: 连不上 ComfyUI（http://x/models/diffusion_models）："
                    "ClientConnectorError: 详情1；"
                    "B: 连不上 ComfyUI（http://x/models/loras）：详情2；"
                    "C: UNETLoader 详情3")
        payload = _payload([
            _model("m1", "模型A", "available", "已收录。", comfy_listed=True),
        ], source="filesystem_fallback", error=long_err)
        out = _render(payload)
        self.assertIn("A: 连不上 ComfyUI", out["sourceNote"])
        self.assertNotIn("C: UNETLoader", out["sourceNote"],
                         "界面上不该塞三条完整错误")
        self.assertLess(len(out["sourceNote"]), 400,
                        "降级提示不该无限长")

    def test_no_claim_of_unavailable_when_comfy_lists_model(self):
        """用户原始缺陷的反向断言：ComfyUI 认得时，界面**不得**说拿不到。"""
        payload = _payload([
            _model("fl2va_pruned_int8", "MiniMax-H3 FL2VA · 剪枝 int8（默认）",
                   "available", "ComfyUI 模型列表已收录该权重，可直接选用。",
                   comfy_listed=True, external=True),
        ])
        out = _render(payload)
        blob = out["warn"] + out["sourceNote"] + out["options"][0]["text"]
        for phrase in ("获取不到", "没有这个单卡", "根本没有这个名字"):
            self.assertNotIn(phrase, blob,
                             f"ComfyUI 已收录的模型不该被说成「{phrase}」")


if __name__ == "__main__":
    unittest.main()
