# -*- coding: utf-8 -*-
"""H3 视频面板本轮 7 项修复的回归测试（2026-11-12）。

用户本轮实测报出的 7 项，逐条落地并由本文件守住：

1. **移除注意力机制里的 sol-attn 渐进调度**（sol_scheduled）
   —— 用户实测画面有叠影。选项从服务端清单里整个删掉
   （不是置灰 —— 置灰等于「能选但会被服务端改写」，是假选项）。
   已下线的 key 走**明确降级**（报错说明为什么下线），不是静默失败。

2. **sol-attn 近似路由必须真能用**
   —— 服务端清单里它必须存在、指向真实节点；工作流拼装时
   真的把该节点接进模型链，且参数（tau/min_tokens/...）被**真正下发**
   （不是只连了节点却漏了参数）。
   节点不在本机时必须**明确报错**，不能静默忽略。

3. **DMAD LoRA 默认强度 = 1.00**（官方值）
   —— 服务端清单的 recommended_strength、前端切换 LoRA 时的回填值、
   落库的 params、以及卡片显示的强度，四处必须一致。

4. **时长上限放宽到 60 秒**（前端输入 + 后端校验）
   —— 超过 15 秒必须能提交；超上限**不静默截断**（明确报错）。

5. **注意力机制下拉的速度标注**
   —— 稀疏=最快 / sol=很快 / kitchen=一般 / sage=较慢 / pytorch=最慢。

6. **生成库 / 媒体库标注所用注意力机制**
   —— 服务端摘要带 attention_label；前端卡片渲染「注意力」行。

7. **修复「复用参数」不填分辨率**
   —— 回填宽高后，尺寸预设档位必须**同步重算**（不能停在上一条任务的档位）。

运行：python3 -m pytest tests/test_h3_panel_tune.py -v
"""
import json
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import job_meta                   # noqa: E402
from h3studio import video_models as VM          # noqa: E402
from h3studio import video_workflow as VW        # noqa: E402

WEB = REPO / "web"
PANEL_JS = (WEB / "video-panel.js").read_text(encoding="utf-8")
APP_JS = (WEB / "app.js").read_text(encoding="utf-8")
ASSETS_JS = (WEB / "assets.js").read_text(encoding="utf-8")
INDEX_HTML = (WEB / "index.html").read_text(encoding="utf-8")
FALLBACK_JS = (WEB / "video-panel-fallback.js").read_text(encoding="utf-8")


def _js_function_body(src: str, signature: str) -> str:
    """取某个 JS 函数的**函数体**（用于定位到函数内部再断言）。

    【为什么不能直接 assertIn(整文件)】字符串在文件里出现过
    ≠ 那条断言在守行为 —— 同一段文本可能在别的函数里也出现，
    那样变异测试不红、漏网也发现不了。
    """
    start = src.find(signature)
    assert start >= 0, "找不到函数：%s" % signature
    # 从签名处找到第一个「{」，再按花括号配平取函数体
    brace = src.find("{", start)
    assert brace >= 0, "函数没有花括号：%s" % signature
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[brace:i + 1]
    raise AssertionError("函数体括号不配平：%s" % signature)



# ==================================================================
# 真跑 JS 的 DOM shim（复用 tests/test_panel_fallback.py 的同一套）
# ==================================================================
# 【为什么用真跑 JS 而不是文本匹配】
# 文本断言（assertIn("0.65", src)）有两个致命问题：
#   1. 注释里写一句「旧代码是 0.65」就会误判；
#   2. 「字符串在文件里出现过」≠「那条断言在守行为」——
#      变异测试不红，漏网也发现不了。
# 真跑 JS 断言的是**行为**（切 LoRA 后强度是多少），改坏了就红。
_DOM_SHIM = r"""
class FakeElement {
  constructor(tag) {
    this.tagName = (tag || "div").toUpperCase();
    this.children = []; this._text = ""; this.className = "";
    this.hidden = false; this.disabled = false; this._value = "";
    this.title = ""; this.dataset = {}; this.style = {};
    this._listeners = {};
    this._classes = new Set();
    this.classList = {
      add: (...names) => names.forEach(n => this._classes.add(n)),
      remove: (...names) => names.forEach(n => this._classes.delete(n)),
      toggle: (name, force) => {
        const on = force === undefined ? !this._classes.has(name) : !!force;
        if (on) this._classes.add(name); else this._classes.delete(name);
        return on;
      },
      contains: name => this._classes.has(name),
    };
  }
  appendChild(c) { this.children.push(c); return c; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() {
    if (this.children.length) return this.children.map(c => c.textContent).join("");
    return this._text;
  }
  set innerHTML(v) { this._text = String(v); this._innerHTML = String(v); this.children = []; }
  get innerHTML() { return this._innerHTML !== undefined ? this._innerHTML : this._text; }
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
  addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); }
  dispatch(t, ev) { (this._listeners[t] || []).forEach(fn => fn(ev || {target: this})); }
  querySelectorAll() { return []; }
  querySelector() { return null; }
  get offsetParent() { return this.hidden ? null : {}; }
  getClientRects() { return this.hidden ? [] : [{}]; }
  get options() {
    if (this.tagName !== "SELECT") return undefined;
    return this.children.filter(c => c.tagName === "OPTION");
  }
}
const REGISTRY = {};
global.makeRegistry = function (ids) {
  ids.forEach(id => {
    const tag = /Select$/.test(id) ? "select"
             : /Width$|Height$|Seed$/.test(id) ? "input" : "div";
    REGISTRY[id] = new FakeElement(tag);
  });
  return REGISTRY;
};
global.REGISTRY = REGISTRY;
global.FakeElement = FakeElement;
global.fetch = async function (url) {
  const u = String(url);
  // 【2026-11-12】记录每次请求的 URL —— 用来断言「吸附请求里带的是
  // 两个合法数字」。旧实现只改一维时会把 null 发出去，服务端回
  // 「宽高必须是数字」，高度被静默清空。
  (global.FETCH_LOG = global.FETCH_LOG || []).push(u);
  if (u.indexOf("/api/video-options") === 0) {
    return { ok: true, status: 200, json: async () => global.VIDEO_OPTIONS_PAYLOAD };
  }
  if (u.indexOf("/api/video-size") === 0) {
    // 模拟服务端吸附：宽高必须是 32 的倍数
    const q = {};
    u.replace(/[?&]([^=&]+)=([^&]*)/g, (_, k, v) => { q[k] = decodeURIComponent(v); return ''; });
    const snap = n => Math.max(32, Math.round(Number(n) / 32) * 32);
    const reasons = [];
    let w = Number(q.width), h = Number(q.height);
    if (!Number.isFinite(w) || !Number.isFinite(h)) {
      return { ok: true, status: 200,
               json: async () => ({ width: 0, height: 0, reasons: ["宽高必须是数字"] }) };
    }
    const aw = snap(w), ah = snap(h);
    if (aw !== w) reasons.push("宽度 " + w + " 不是 32 的倍数 → " + aw);
    if (ah !== h) reasons.push("高度 " + h + " 不是 32 的倍数 → " + ah);
    return { ok: true, status: 200,
             json: async () => ({ width: aw, height: ah, reasons: reasons }) };
  }
  return { ok: true, status: 200, json: async () => ({}) };
};
global.window = global;
global.document = {
  getElementById: id => REGISTRY[id] || null,
  querySelector: () => null, querySelectorAll: () => [],
  createElement: tag => new FakeElement(tag),
  addEventListener: () => {},
};
global.$ = sel => REGISTRY[String(sel).replace(/^[#]/, "")] || null;
global.state = { duration: 5, steps: 4 };
"""

_PANEL_IDS = [
    "vidModelSelect", "vidModelNote", "vidModelWarn", "vidSizePresetSelect",
    "vidAccelLoraSelect", "vidAccelLoraNote", "vidAccelLoraStrength",
    "vidAccelLoraStrengthVal", "vidAccelLoraStrengthField",
    "vidAttentionSelect", "vidAttentionNote", "vidTwopassEnabled",
    "vidTwopassBody", "vidTwopassNote", "vidTwopassHint", "vidUpscalerSelect",
    "vidTransition", "vidTransitionVal", "vidLowres", "vidLowresVal",
    "vidRho", "vidRhoVal", "vidAspectChips", "vidWidth", "vidHeight",
    "vidSizeHint", "vidSizeNote", "vidSizeMode", "vidSizeAdjust",
    "vidSizePresetNote", "vidFixedLoraNote", "vidSizeReset", "seed",
    "staleVideoBanner",
]


def _server_payload() -> dict:
    """服务端 /api/video-options 的**真实**响应（离线取，不连 ComfyUI）。

    【为什么要自己拼一遍而不是直接 options_payload()】
    server.api_video_options 在 options_payload() 之后还会补一层
    「当前底模下每个加速 LoRA 的实测兼容性」：
        entry["compatible"] / entry["compat_reason"] / entry["selectable"]
    前端 selectableAccelLoras() 靠 selectable + compatible + available
    三个标记过滤下拉 —— 少了 selectable，测试里下拉就只有「不额外挂」一项，
    后面所有行为断言都测不到真东西。

    这里照 server 的算法补齐（同一个真源 accel_lora_compat_matrix）。
    """
    payload = VM.options_payload(node_available=lambda ct: True)
    model_key = payload["default_model"]
    matrix = VM.accel_lora_compat_matrix(model_key)
    combos = []
    for item in payload["accel_lora_combos"]:
        entry = dict(item)
        check = matrix.get(item["key"], {})
        entry["compatible"] = bool(check.get("compatible"))
        entry["compat_reason"] = check.get("reason", "")
        entry["selectable"] = bool(entry["compatible"]) and \
            entry.get("available", True)
        combos.append(entry)
    payload["accel_lora_combos"] = combos
    payload["selected_model"] = model_key
    return payload


def _node_panel(script: str, payload: dict | None = None,
                timeout: int = 60) -> dict:
    """把 video-panel.js 装进 DOM shim 跑一段脚本，返回最后一行 JSON。"""
    import shutil
    import subprocess
    node = shutil.which("node") or shutil.which("nodejs")
    assert node is not None, "本机没有 node"
    tmp = Path("/tmp/h3-panel-tune-test")
    tmp.mkdir(parents=True, exist_ok=True)
    shim = tmp / "shim.js"
    shim.write_text(_DOM_SHIM, encoding="utf-8")
    panel_js = (REPO / "web" / "video-panel.js").read_text(
        encoding="utf-8", errors="ignore")
    fallback_js = (REPO / "web" / "video-panel-fallback.js").read_text(
        encoding="utf-8", errors="ignore")
    # 【注意】下面这个值**已经是** JSON 文本，不能再 json.dumps 一次 ——
    # 那会把它变成一个「JSON 字符串字面量」，前端拿到的是字符串而不是对象，
    # 于是清单全是空的（表现：下拉里只剩「不额外挂加速 LoRA」一项）。
    payload_json = (json.dumps(payload) if payload is not None
                    else json.dumps(_server_payload()))
    full = f"""
global.makeRegistry({json.dumps(_PANEL_IDS)});
global.VIDEO_OPTIONS_PAYLOAD = {payload_json};
{fallback_js}
{panel_js}
{script}
"""
    # 【为什么写文件而不是 node -e】payload 里有 111 条 size_presets，
    # 整段拼起来超过 ARG_MAX -> OSError「Argument list too long」。
    # 写成临时 js 文件再 node 跑，避开命令行长度限制。
    run_file = tmp / "run.js"
    run_file.write_text(
        f"require({str(shim)!r});\n" + full, encoding="utf-8")
    proc = subprocess.run(
        [node, str(run_file)],
        capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise AssertionError(
            f"Node 脚本失败 (exit {proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


#: 加速 LoRA 切换的行为脚本（第 3 项）。
_DMAD_SWITCH_SCRIPT = r"""
(async () => {
  const errors = [];
  const origError = console.error;
  console.error = (...a) => { errors.push(String(a[0])); };
  try {
    await window.VIDEO_PANEL.load();
    // bind() 才会把 change 监听器挂上（load 只负责拉清单 + 渲染）
    window.VIDEO_PANEL.bind();
  } catch (e) { errors.push('THROWN:' + e.message); }
  console.error = origError;
  const panel = window.VIDEO_PANEL;
  const sel = document.getElementById('vidAccelLoraSelect');
  const setLora = key => {
    sel.value = key;
    sel.dispatch('change', { target: { value: key } });
  };
  const debug = {
    options: sel.options.map(o => o.value),
    stateLora: panel.state.accelLora,
    strength0: panel.state.accelLoraStrength,
    optsCombos: (panel.options.accel_lora_combos||[]).map(c => c.key + ':' + c.selectable),
    ready: panel.isReady(),
  };
  // 场景 1：先手动把强度调到 0.65，再切到 DMAD -> 必须变 1.00
  panel.state.accelLoraStrength = 0.65;
  setLora('dmad_4step_critic');
  const strength = panel.state.accelLoraStrength;
  const slider = document.getElementById('vidAccelLoraStrength').value;
  const label = document.getElementById('vidAccelLoraStrengthVal').textContent;
  const note = document.getElementById('vidAccelLoraNote').textContent;
  // 场景 2：切到「更好运动」-> 必须变成它自己的推荐值 0.8
  setLora('better_motion');
  const betterMotionStrength = panel.state.accelLoraStrength;
  // 场景 3：已经选了 DMAD 之后手动把强度改成 0.65，
  //          再「重新选一次同一枚」-> 必须保留用户的 0.65（不被莫名重置）
  setLora('dmad_4step_critic');
  panel.state.accelLoraStrength = 0.65;
  setLora('dmad_4step_critic');
  const reselectKeeps = panel.state.accelLoraStrength;
  console.log(JSON.stringify({
    errors, strength, slider, label, note, debug,
    betterMotionStrength, reselectKeeps,
  }));
})();
"""


#: 注意力机制下拉的行为脚本（第 1 / 5 项）。
_ATTENTION_DROPDOWN_SCRIPT = r"""
(async () => {
  const errors = [];
  const origError = console.error;
  console.error = (...a) => { errors.push(String(a[0])); };
  try {
    await window.VIDEO_PANEL.load();
    window.VIDEO_PANEL.bind();
  } catch (e) { errors.push('THROWN:' + e.message); }
  console.error = origError;
  const sel = document.getElementById('vidAttentionSelect');
  const opts = sel.options.map(o => ({ value: o.value, text: o.textContent }));
  console.log(JSON.stringify({
    errors, opts,
    note: document.getElementById('vidAttentionNote').textContent,
  }));
})();
"""

#: 「复用参数」分辨率回填的行为脚本（第 7 项）。
_REUSE_RESOLUTION_SCRIPT = r"""
(async () => {
  const errors = [];
  const origError = console.error;
  console.error = (...a) => { errors.push(String(a[0])); };
  try {
    await window.VIDEO_PANEL.load();
    window.VIDEO_PANEL.bind();
  } catch (e) { errors.push('THROWN:' + e.message); }
  console.error = origError;
  const panel = window.VIDEO_PANEL;
  // 先把面板当前的尺寸改成**另一档**（模拟用户之前用过 864x480）
  panel.state.width = 864;
  panel.state.height = 480;
  panel.state.custom = true;
  panel.state.sizePresetKey = '16:9_0.4';
  panel.render();
  // 再「复用参数」一条 1344x768 的老任务
  panel.applyConfig({
    model: 'fl2va_pruned_int8',
    aspect: '16:9',
    width: 1344, height: 768,
    rawWidth: 1344, rawHeight: 768,
    custom: true,
  });
  panel.render();
  const presetSel = document.getElementById('vidSizePresetSelect');
  const chosen = presetSel.options.find(o => o.selected);
  console.log(JSON.stringify({
    errors,
    width: panel.state.width,
    height: panel.state.height,
    presetKey: panel.state.sizePresetKey,
    presetValue: presetSel.value,
    presetText: chosen ? chosen.textContent : '',
    widthInput: document.getElementById('vidWidth').value,
    heightInput: document.getElementById('vidHeight').value,
    sizeHint: document.getElementById('vidSizeHint').textContent,
  }));
})();
"""

# ==================================================================
# 第 1 项：移除 sol-attn 渐进调度
# ==================================================================
class TestSolScheduledRemoved(unittest.TestCase):
    """第 1 项：sol-attn 渐进调度选项已移除。"""

    def test_not_in_server_manifest(self):
        self.assertNotIn("sol_scheduled", VM.VIDEO_ATTENTION_MODES,
                         "渐进调度必须从服务端清单里删掉（不是置灰）")
        self.assertIn("sol_scheduled", VM.VIDEO_REMOVED_ATTENTION_MODES,
                      "要保留在「已下线」表里，才能对老数据明确降级")

    def test_sol_approx_routing_kept(self):
        """用户要求保留「sol-attn 近似路由」。"""
        self.assertIn("sol", VM.VIDEO_ATTENTION_MODES)
        self.assertEqual(
            VM.VIDEO_ATTENTION_MODES["sol"]["node"],
            "MiniMaxH3MemoryEfficientSolAttentionPatch")

    def test_not_in_options_payload(self):
        payload = VM.options_payload()
        keys = [m["key"] for m in payload["attention_modes"]]
        self.assertNotIn("sol_scheduled", keys)
        self.assertIn("sol", keys)

    def test_not_in_fallback_manifest(self):
        """兜底清单也必须没有它 —— 否则服务端挂掉时它又冒出来了。"""
        self.assertNotIn("sol_scheduled", FALLBACK_JS)

    def test_not_in_node_probe_list(self):
        """节点探测名单里也去掉（面板不再问它）。"""
        import server as srv
        self.assertNotIn("MiniMaxH3ScheduledSolAttentionPatch",
                         srv._VIDEO_PANEL_NODES)

    def test_server_rejects_removed_mode_with_clear_reason(self):
        """老任务/老客户端传 sol_scheduled -> 明确报错说明为什么下线。"""
        import server as srv
        srv._COMFY_NODE_NAMES.update(
            VM.VIDEO_ATTENTION_MODES[k]["node"]
            for k in VM.VIDEO_ATTENTION_MODES
            if VM.VIDEO_ATTENTION_MODES[k].get("node"))
        base = {
            "prompt": "a red panda", "duration": 5,
            "model_key": "fl2va_pruned_int8", "width": 864, "height": 480,
            "steps": 4, "seed": 123, "accel_loras": [],
            "twopass": {"enabled": False},
        }
        out, err = srv.normalize_t2v_params(
            dict(base, attention="sol_scheduled"))
        self.assertTrue(err, "已下线的机制必须报错，不能静默接受")
        self.assertIn("已下线", err)
        self.assertIn("叠影", err, "报错要说明原因（用户实测画面有叠影）")

    def test_workflow_has_no_scheduled_node_path(self):
        """工作流拼装层不能再拼出渐进调度节点。"""
        params = {
            "job_id": "t", "prompt": "x", "duration": 5,
            "model_key": "fl2va_pruned_int8", "width": 864, "height": 480,
            "steps": 4, "seed": 1, "attention": "sol",
            "accel_loras": [],
        }
        wf = VW.build_t2v_panel(dict(params))
        classes = {n["class_type"] for n in wf.values()}
        self.assertNotIn("MiniMaxH3ScheduledSolAttentionPatch", classes)


# ==================================================================
# 第 2 项：sol-attn 近似路由真能用
# ==================================================================
class TestSolApproxRoutingUsable(unittest.TestCase):
    """第 2 项：sol-attn 近似路由真的接进工作流、参数真的下发。"""

    BASE = {
        "job_id": "soljob", "prompt": "a red panda", "duration": 5,
        "model_key": "fl2va_pruned_int8", "width": 864, "height": 480,
        "steps": 4, "seed": 7, "attention": "sol",
        "accel_loras": [],
    }

    def test_node_is_inserted_into_workflow(self):
        wf = VW.build_t2v_panel(dict(self.BASE))
        nodes = [(k, n) for k, n in wf.items()
                 if n["class_type"] == "MiniMaxH3MemoryEfficientSolAttentionPatch"]
        self.assertEqual(len(nodes), 1, "sol-attn 节点必须被接进工作流")
        node_id, node = nodes[0]
        # 模型链：patch 节点的输入必须接的是上游模型引用
        self.assertIsInstance(node["inputs"]["model"], list)

    def test_parameters_are_actually_delivered(self):
        """参数必须**逐项**下发 —— 只连节点不传参数 = 白接。"""
        wf = VW.build_t2v_panel(dict(self.BASE))
        node = next(n for n in wf.values()
                    if n["class_type"] == "MiniMaxH3MemoryEfficientSolAttentionPatch")
        expected = VM.VIDEO_ATTENTION_MODES["sol"]["inputs"]
        for key, value in expected.items():
            self.assertIn(key, node["inputs"],
                          "sol-attn 的 %s 参数没有下发到工作流" % key)
            self.assertEqual(node["inputs"][key], value)

    def test_tau_value_is_the_tuned_one(self):
        """tau=1.3（本机调优值，论文默认 1.0）。"""
        wf = VW.build_t2v_panel(dict(self.BASE))
        node = next(n for n in wf.values()
                    if n["class_type"] == "MiniMaxH3MemoryEfficientSolAttentionPatch")
        self.assertEqual(node["inputs"]["tau"], 1.3)
        self.assertEqual(node["inputs"]["min_tokens"], 4096)
        self.assertEqual(node["inputs"]["sink_conditioning"], "exact_kv")

    def test_node_present_in_real_comfyui_object_info(self):
        """本机 ComfyUI 必须真有这个节点（实测 /object_info）。"""
        import urllib.request
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:8188/object_info/"
                    "MiniMaxH3MemoryEfficientSolAttentionPatch", timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:      # pragma: no cover - 环境不可达时跳过
            self.skipTest("本机 ComfyUI 不可达：%s" % exc)
        self.assertIn("MiniMaxH3MemoryEfficientSolAttentionPatch", data)
        required = set(VM.VIDEO_ATTENTION_MODES["sol"]["inputs"])
        actual = set(data["MiniMaxH3MemoryEfficientSolAttentionPatch"]
                     ["input"]["required"])
        self.assertTrue(required.issubset(actual),
                        "本机节点的输入槽位与清单对不上：缺 %s"
                        % sorted(required - actual))

    def test_missing_node_gives_explicit_error(self):
        """本机没有该节点时必须明确报错，不能静默忽略（假控件）。"""
        import server as srv
        srv._COMFY_NODE_NAMES.discard(
            "MiniMaxH3MemoryEfficientSolAttentionPatch")
        base = {
            "prompt": "x", "duration": 5,
            "model_key": "fl2va_pruned_int8", "width": 864, "height": 480,
            "steps": 4, "seed": 1, "accel_loras": [],
            "twopass": {"enabled": False}, "attention": "sol",
        }
        out, err = srv.normalize_t2v_params(dict(base))
        self.assertTrue(err)
        self.assertIn("没有节点", err)


# ==================================================================
# 第 3 项：DMAD LoRA 默认强度 1.00
# ==================================================================
class TestDmadDefaultStrength(unittest.TestCase):
    """第 3 项：DMAD LoRA 默认强度 = 1.00，且各处一致。"""

    def test_server_manifest_recommends_one(self):
        meta = VM.VIDEO_ACCEL_LORA_COMBOS["dmad_4step_critic"]
        self.assertEqual(meta["recommended_strength"], 1.0)

    def test_options_payload_carries_one(self):
        payload = VM.options_payload()
        by_key = {c["key"]: c for c in payload["accel_lora_combos"]}
        self.assertEqual(by_key["dmad_4step_critic"]["recommended_strength"], 1.0)

    def test_fallback_manifest_carries_one(self):
        m = re.search(r"window\.VIDEO_PANEL_FALLBACK\s*=\s*(\{.*\});?\s*$",
                      FALLBACK_JS, re.S)
        self.assertIsNotNone(m)
        data = json.loads(m.group(1))
        by_key = {c["key"]: c for c in data["accel_lora_combos"]}
        self.assertEqual(by_key["dmad_4step_critic"]["recommended_strength"], 1.0)

    def test_switching_lora_resets_strength_to_recommended(self):
        """切到 DMAD 时强度必须回到 1.00（不能沿用上一枚的 0.65）。"""
        body = _js_function_body(PANEL_JS, 'accelSel.addEventListener("change"')
        self.assertIn("recommended_strength", body)
        self.assertIn("videoState.accelLora !== previous", body,
                      "必须按「是否换了另一枚 LoRA」判断，不能无条件保留旧强度")

    def test_server_side_has_no_065_for_dmad(self):
        """服务端清单里 DMAD 不得带 0.65。"""
        meta = VM.VIDEO_ACCEL_LORA_COMBOS["dmad_4step_critic"]
        self.assertNotEqual(meta.get("recommended_strength"), 0.65)
        self.assertEqual(meta.get("recommended_strength"), 1.0)

    def test_switching_to_dmad_resets_strength_to_one(self):
        """【真跑 JS】切到 DMAD 时强度必须变成 1.00（不是沿用 0.65）。

        这是本项最核心的行为：旧逻辑只在 strength 为 undefined 时才套用
        推荐值，于是「先把强度调到 0.65，再切 DMAD」会把 0.65 带过去。
        """
        out = _node_panel(_DMAD_SWITCH_SCRIPT)
        self.assertEqual(out["strength"], 1.0,
                         "切到 DMAD 后强度必须是 1.00，实际 %s" % out["strength"])
        self.assertEqual(out["slider"], "1",
                         "滑杆显示值也要是 1，实际 %s" % out["slider"])
        self.assertEqual(out["label"], "1.00")
        self.assertIn("×1.00", out["note"])

    def test_switching_to_another_lora_resets_to_its_recommended(self):
        """切到别的 LoRA 也要重置成**它自己**的推荐值。"""
        out = _node_panel(_DMAD_SWITCH_SCRIPT)
        self.assertEqual(out["betterMotionStrength"], 0.8,
                         "切到「更好运动」应重置为它的推荐值 0.8")

    def test_reselecting_same_lora_keeps_user_value(self):
        """同一枚 LoRA 反复切换时保留用户手动调过的值（不被莫名重置）。"""
        out = _node_panel(_DMAD_SWITCH_SCRIPT)
        self.assertEqual(out["reselectKeeps"], 0.65,
                         "选回同一枚 LoRA 时应保留用户值 0.65")

    def test_server_records_strength_one(self):
        """落库的 params 里 strength 必须是 1.00（服务端归一后）。"""
        import server as srv
        srv._COMFY_NODE_NAMES.update(
            VM.VIDEO_ATTENTION_MODES[k]["node"]
            for k in VM.VIDEO_ATTENTION_MODES
            if VM.VIDEO_ATTENTION_MODES[k].get("node"))
        base = {
            "prompt": "x", "duration": 5,
            "model_key": "fl2va_pruned_int8", "width": 864, "height": 480,
            "steps": 4, "seed": 1, "attention": "default",
            "twopass": {"enabled": False},
            "accel_loras": [{"key": "dmad_4step_critic", "enabled": True}],
        }
        out, err = srv.normalize_t2v_params(dict(base))
        self.assertEqual(err, "")
        self.assertEqual(out["accel_loras"][0]["strength"], 1.0)

    def test_card_shows_strength_one(self):
        """卡片显示的强度与落库值一致。"""
        info = job_meta.summarize("t2v", {
            "model_key": "fl2va_pruned_int8",
            "accel_loras": [{"key": "dmad_4step_critic", "strength": 1.0,
                             "enabled": True}],
        })
        self.assertEqual(info["lora_label"], "DMAD 4 步（critic 版）")
        self.assertEqual(info["lora_strength"], 1.0)


# ==================================================================
# 第 4 项：时长上限放宽到 60 秒
# ==================================================================
class TestDurationLimitRelaxed(unittest.TestCase):
    """第 4 项：时长上限 15 -> 60，超上限不静默截断。"""

    def test_constant_is_60(self):
        self.assertEqual(VM.VIDEO_DURATION_MAX, 60.0)
        self.assertLessEqual(VM.VIDEO_DURATION_MIN, 2.0)

    def test_front_end_slider_max_is_60(self):
        m = re.search(r'<input type="range" id="duration"[^>]*>', INDEX_HTML)
        self.assertIsNotNone(m, "找不到时长滑杆")
        tag = m.group(0)
        self.assertIn('max="60"', tag,
                      "前端滑杆上限必须是 60（原来 15）")
        self.assertNotIn('max="15"', tag)

    def test_server_accepts_60_seconds(self):
        import server as srv
        srv._COMFY_NODE_NAMES.update(
            VM.VIDEO_ATTENTION_MODES[k]["node"]
            for k in VM.VIDEO_ATTENTION_MODES
            if VM.VIDEO_ATTENTION_MODES[k].get("node"))
        base = {
            "prompt": "x", "model_key": "fl2va_pruned_int8",
            "width": 864, "height": 480, "steps": 4, "seed": 1,
            "attention": "default", "accel_loras": [],
            "twopass": {"enabled": False},
        }
        out, err = srv.normalize_t2v_params(dict(base, duration=60))
        self.assertEqual(err, "")
        self.assertEqual(out["duration"], 60.0)

    def test_server_accepts_30_seconds(self):
        """30 秒是用户明确提到的量级，必须能过。"""
        import server as srv
        srv._COMFY_NODE_NAMES.update(
            VM.VIDEO_ATTENTION_MODES[k]["node"]
            for k in VM.VIDEO_ATTENTION_MODES
            if VM.VIDEO_ATTENTION_MODES[k].get("node"))
        base = {
            "prompt": "x", "model_key": "fl2va_pruned_int8",
            "width": 864, "height": 480, "steps": 4, "seed": 1,
            "attention": "default", "accel_loras": [],
            "twopass": {"enabled": False},
        }
        out, err = srv.normalize_t2v_params(dict(base, duration=30))
        self.assertEqual(err, "")
        self.assertEqual(out["duration"], 30.0)

    def test_over_limit_is_rejected_not_truncated(self):
        """超过上限必须**报错**（不静默截断）。"""
        value, err = VM.normalize_video_duration(120)
        self.assertTrue(err, "超上限必须报错")
        self.assertIn("60", err)
        self.assertEqual(value, 120.0, "超上限的值不能被悄悄改小")

    def test_normalize_helper_accepts_valid_values(self):
        value, err = VM.normalize_video_duration(45)
        self.assertEqual(err, "")
        self.assertEqual(value, 45.0)

    def test_normalize_helper_rejects_garbage_without_error(self):
        value, err = VM.normalize_video_duration("abc")
        self.assertEqual(err, "", "非法输入回落默认值，不报错")
        self.assertEqual(value, VM.VIDEO_DURATION_DEFAULT)

    def test_workflow_frame_count_grows_with_duration(self):
        """时长真的进了工作流（帧数表达式节点的值 = 时长）。"""
        for dur in (15, 30, 60):
            wf = VW.build_t2v_panel({
                "job_id": "d", "prompt": "x", "duration": dur,
                "model_key": "fl2va_pruned_int8", "width": 864, "height": 480,
                "steps": 4, "seed": 1, "attention": "default",
                "accel_loras": [],
            })
            self.assertEqual(wf["105:111"]["inputs"]["value"], float(dur))

    def test_agent_duration_range_matches_backend(self):
        """Agent 侧的时长区间必须与服务端一致（60），不能还写 15。"""
        from h3studio import agent_schema
        self.assertEqual(agent_schema.VIDEO_DURATION_MAX, 60)
        self.assertEqual(agent_schema.VIDEO_DURATION_HARD_MAX, 60.0)

    def test_agent_list_modes_reports_60(self):
        import asyncio
        from h3studio import agent as agent_mod
        ctx = agent_mod.AgentContext(server_module=None, provider={})
        result = asyncio.run(
            agent_mod.TOOLS["list_modes"]["handler"](ctx, {}))
        self.assertEqual(result["video_duration_range"], [2, 60])


# ==================================================================
# 第 5 项：注意力机制下拉的速度标注
# ==================================================================
class TestAttentionSpeedLabels(unittest.TestCase):
    """第 5 项：下拉显示速度标注，且顺序符合用户给定。"""

    EXPECTED = {
        "sparse":  ("速度最快", 1),
        "sol":     ("速度很快", 2),
        "kitchen": ("速度一般", 3),
        "sage":    ("速度较慢", 4),
        "default": ("速度最慢", 5),
    }

    def test_every_mode_has_speed_label(self):
        for key in VM.VIDEO_ATTENTION_MODES:
            meta = VM.VIDEO_ATTENTION_MODES[key]
            self.assertTrue(meta.get("speed_label"),
                            "%s 缺少 speed_label" % key)
            self.assertIsInstance(meta.get("speed_rank"), int)

    def test_labels_match_user_specification(self):
        for key, (label, rank) in self.EXPECTED.items():
            meta = VM.VIDEO_ATTENTION_MODES[key]
            self.assertEqual(meta["speed_label"], label,
                             "%s 的速度标注不对（用户：稀疏最快 / sol 很快 / "
                             "kitchen 一般 / sage 较慢 / pytorch 最慢）" % key)
            self.assertEqual(meta["speed_rank"], rank)

    def test_speed_ranks_are_unique_and_cover_all_modes(self):
        ranks = sorted(m["speed_rank"] for m in VM.VIDEO_ATTENTION_MODES.values())
        self.assertEqual(ranks, list(range(1, len(VM.VIDEO_ATTENTION_MODES) + 1)))

    def test_options_payload_carries_speed_labels(self):
        payload = VM.options_payload()
        by_key = {m["key"]: m for m in payload["attention_modes"]}
        for key, (label, _rank) in self.EXPECTED.items():
            self.assertEqual(by_key[key]["speed_label"], label)

    def test_fallback_manifest_carries_speed_labels(self):
        m = re.search(r"window\.VIDEO_PANEL_FALLBACK\s*=\s*(\{.*\});?\s*$",
                      FALLBACK_JS, re.S)
        data = json.loads(m.group(1))
        by_key = {a["key"]: a for a in data["attention_modes"]}
        for key, (label, _rank) in self.EXPECTED.items():
            self.assertEqual(by_key[key]["speed_label"], label)

    def test_frontend_renders_speed_label_in_option_text(self):
        body = _js_function_body(PANEL_JS, "function attentionOptionLabel(")
        self.assertIn("speed_label", body)
        self.assertIn("item.speed_label", body,
                      "下拉文案必须用服务端下发的 speed_label")

    def test_frontend_uses_label_helper_in_render(self):
        body = _js_function_body(PANEL_JS, "function renderAttention(")
        self.assertIn("attentionOptionLabel(item)", body)

    def test_dropdown_behaviour_shows_speed_and_no_scheduled(self):
        """【真跑 JS】下拉里：有速度标注、没有渐进调度、保留 sol 近似路由。"""
        out = _node_panel(_ATTENTION_DROPDOWN_SCRIPT)
        self.assertEqual(out["errors"], [])
        texts = {o["value"]: o["text"] for o in out["opts"]}
        self.assertNotIn("sol_scheduled", texts,
                         "渐进调度必须从下拉里消失（不是置灰）")
        self.assertIn("sol", texts, "必须保留 sol-attn 近似路由")
        # 速度标注逐条核对
        self.assertIn("速度最快", texts["sparse"])
        self.assertIn("速度很快", texts["sol"])
        self.assertIn("速度一般", texts["kitchen"])
        self.assertIn("速度较慢", texts["sage"])
        self.assertIn("速度最慢", texts["default"])


# ==================================================================
# 第 6 项：生成库 / 媒体库标注注意力机制
# ==================================================================
class TestAttentionAnnotation(unittest.TestCase):
    """第 6 项：每个视频条目显示生成时实际使用的注意力机制。"""

    def test_summary_carries_attention_label(self):
        info = job_meta.summarize("t2v", {
            "model_key": "fl2va_pruned_int8", "attention": "sparse",
        })
        self.assertEqual(info["attention_key"], "sparse")
        self.assertEqual(info["attention_label"], "稀疏视频注意力（预算制）")
        self.assertEqual(info["attention_speed_label"], "速度最快")

    def test_summary_label_matches_panel_dropdown_verbatim(self):
        """卡片上的名字必须与面板下拉里那一项**逐字一致**。"""
        for key in VM.VIDEO_ATTENTION_MODES:
            info = job_meta.summarize("t2v", {
                "model_key": "fl2va_pruned_int8", "attention": key,
            })
            self.assertEqual(info["attention_label"],
                             VM.VIDEO_ATTENTION_MODES[key]["label"])

    def test_removed_mechanism_still_annotated_for_old_jobs(self):
        """老任务用的是已下线的机制 -> 也要如实标注（标「已下线」）。"""
        info = job_meta.summarize("t2v", {
            "model_key": "fl2va_pruned_int8", "attention": "sol_scheduled",
        })
        self.assertIn("已下线", info["attention_label"])
        self.assertFalse(info["attention_available"])

    def test_no_attention_recorded_gives_empty_label(self):
        """老任务没记录注意力机制 -> 不编造（label 为空）。"""
        info = job_meta.summarize("t2v", {"model_key": "fl2va_pruned_int8"})
        self.assertEqual(info["attention_key"], "")
        self.assertEqual(info["attention_label"], "")

    def test_summary_shape_is_stable_across_modes(self):
        """所有模式的摘要都必须带同一组注意力字段。"""
        for mode, params in (("t2v", {"model_key": "x"}),
                             ("flf", {}), ("r2v", {}),
                             ("t2i", {"model": "x"}), ("t2a", {})):
            info = job_meta.summarize(mode, params)
            for key in ("attention_key", "attention_label",
                        "attention_speed_label", "attention_available"):
                self.assertIn(key, info, "%s 摘要缺字段 %s" % (mode, key))

    def test_generation_card_renders_attention_row(self):
        """生成库卡片模板必须渲染「注意力」行。"""
        body = _js_function_body(APP_JS, "function jobCard(")
        self.assertIn("attentionRowHtml(job)", body)

    def test_attention_row_helper_exists_and_is_empty_safe(self):
        label_body = _js_function_body(APP_JS, "function attentionLabel(")
        self.assertIn("attention_label", label_body,
                      "显示文案必须取服务端摘要的 attention_label")
        row_body = _js_function_body(APP_JS, "function attentionRowHtml(")
        self.assertIn('if (!text) return ""', row_body,
                      "没有记录时整行不渲染（不显示「空」）")
        self.assertIn("attentionLabel(job)", row_body)

    def test_asset_library_records_attention_on_ingest(self):
        """素材库入库时要记下注意力机制（媒体库标注的数据来源）。"""
        src = (REPO / "h3studio" / "asset_service.py").read_text(encoding="utf-8")
        self.assertIn('"attention": params.get("attention")', src)
        self.assertIn("attention_label", src)

    def test_asset_card_shows_attention_row(self):
        body = _js_function_body(ASSETS_JS, "function attentionMetaRow(")
        self.assertIn("attention_label", body)
        self.assertIn('if (!label) return ""', body)


# ==================================================================
# 第 7 项：修复「复用参数」不填分辨率
# ==================================================================
class TestReuseParamsResolution(unittest.TestCase):
    """第 7 项：复用参数后分辨率（宽高 + 尺寸预设档位）正确回填。"""

    def test_apply_config_recomputes_size_preset_key(self):
        """applyConfig 回填宽高后必须**重算**档位（核心修复）。"""
        body = _js_function_body(PANEL_JS, "function applyConfig(")
        self.assertIn("presetKeyForSize", body,
                      "applyConfig 必须按宽高重算尺寸预设档位")
        self.assertIn("videoState.sizePresetKey = presetKeyForSize(",
                      body)

    def test_preset_key_helper_exists(self):
        body = _js_function_body(PANEL_JS, "function presetKeyForSize(")
        self.assertIn("presetsForAspect", body)
        self.assertIn("p.width", body)
        self.assertIn("p.height", body)

    def test_stale_preset_key_is_rejected(self):
        """配置里带的档位 key 与宽高不一致时，必须以宽高为准。"""
        body = _js_function_body(PANEL_JS, "function applyConfig(")
        self.assertIn("preset.width === videoState.width", body)
        self.assertIn("preset.height === videoState.height", body)

    def test_reuse_handler_passes_size_preset_key(self):
        """「复用参数」必须把档位 key 一起传下去。

        【2026-11-13 调整】这段解析逻辑已抽成纯函数 reuseConfigFromJob
        （这样才能被单测真正覆盖 —— 独立验收 P2 实测：
        处理器零断言时，把宽高删掉 60 条测试仍全绿）。
        所以断言的位置跟着搬到了新函数里。
        """
        body = _js_function_body(APP_JS, "function reuseConfigFromJob(")
        self.assertIn("size_preset_key", body,
                      "复用参数必须带 size_preset_key")
        handler = _js_function_body(APP_JS, 'card.querySelector(\'[data-act="reuse"]\')')
        self.assertIn("reuseConfigFromJob(", handler,
                      "处理器必须调用被测的纯函数")

    def test_reuse_handler_resolves_preset_from_server_manifest(self):
        """档位必须从服务端下发的 size_presets 里查（前端不自己编）。"""
        body = _js_function_body(APP_JS, "function reuseConfigFromJob(")
        self.assertIn("size_presets", body)
        self.assertIn("hit ? hit.key", body)

    def test_reuse_handler_also_restores_sampling_params(self):
        """复用参数要把采样参数一起带回去（步数/采样器/调度器/shift）。"""
        body = _js_function_body(APP_JS, "function reuseConfigFromJob(")
        for field in ("steps:", "sampler:", "scheduler:",
                      "shift_video:", "shift_audio:"):
            self.assertIn(field, body, "复用参数缺字段 %s" % field)

    def test_render_size_preset_uses_state_key(self):
        """渲染时用的是 state 里的档位（与 applyConfig 重算的一致）。"""
        body = _js_function_body(PANEL_JS, "function renderSizePresetSelect(")
        self.assertIn("videoState.sizePresetKey", body)

    def test_reuse_behaviour_backfills_resolution(self):
        """【真跑 JS】复用参数后：宽高 = 1344×768，档位下拉也必须对上。

        旧行为：宽高回填对了，但「尺寸预设」下拉仍停在**上一条**任务的
        档位（864×480）—— 用户看到「1344×768」配「480p · 864×480」，
        就以为分辨率没填上。
        """
        out = _node_panel(_REUSE_RESOLUTION_SCRIPT)
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["width"], 1344)
        self.assertEqual(out["height"], 768)
        self.assertEqual(out["widthInput"], "1344")
        self.assertEqual(out["heightInput"], "768")
        # 档位必须与宽高一致 —— 不能停在 864×480 那一档
        self.assertEqual(out["presetValue"], "16:9_0.98",
                         "尺寸预设档位没跟着宽高重算，实际 %s" % out["presetValue"])
        self.assertIn("1344×768", out["presetText"],
                      "档位文案必须显示 1344×768")
        self.assertIn("1344×768", out["sizeHint"])


# ==================================================================
# 第 7 项（补）：只改一维宽高时，另一维不能被静默清空
# ==================================================================
#: 只改宽度 -> 断言吸附请求里 height 是**当前生效值**而不是空。
_SNAP_ONE_DIM_SCRIPT = r"""
(async () => {
  const errors = [];
  const origError = console.error;
  console.error = (...a) => { errors.push(String(a[0])); };
  global.FETCH_LOG = [];
  try {
    await window.VIDEO_PANEL.load();
    window.VIDEO_PANEL.bind();
  } catch (e) { errors.push('THROWN:' + e.message); }
  console.error = origError;
  const panel = window.VIDEO_PANEL;
  // 当前生效尺寸：864x480
  panel.state.width = 864;
  panel.state.height = 480;
  panel.state.custom = true;
  panel.state.sizePresetKey = '16:9_0.4';
  panel.render();
  // 模拟用户只改宽度：1000（不是 32 的倍数）
  const wEl = document.getElementById('vidWidth');
  wEl.value = '1000';
  wEl.dispatch('input', { target: { value: '1000' } });
  await new Promise(r => setTimeout(r, 30));
  const sizeReqs = (global.FETCH_LOG || []).filter(u => u.indexOf('/api/video-size') === 0);
  console.log(JSON.stringify({
    errors,
    sizeReqs,
    lastReq: sizeReqs[sizeReqs.length - 1] || '',
    width: panel.state.width,
    height: panel.state.height,
    widthInput: wEl.value,
    heightInput: document.getElementById('vidHeight').value,
    sizeHint: document.getElementById('vidSizeHint').textContent,
    adjust: document.getElementById('vidSizeAdjust').textContent,
  }));
})();
"""


class TestSnapOneDimensionKeepsOther(unittest.TestCase):
    """只改一维宽高时，另一维必须保持不变（不能被静默清空）。

    旧行为：只改宽度 -> rawHeight 仍是 null -> 原样发给服务端 ->
    服务端 float(None) 失败 -> 回「宽高必须是数字」-> applySize 走
    width=0 分支 -> **高度被清空、两个框归零**，用户看不出发生了什么。
    """

    def test_snap_request_never_sends_empty_dimension(self):
        out = _node_panel(_SNAP_ONE_DIM_SCRIPT)
        self.assertEqual(out["errors"], [])
        self.assertTrue(out["sizeReqs"], "应该发出过一次吸附请求")
        req = out["lastReq"]
        self.assertNotIn("height=null", req,
                         "吸附请求里不能有空的高度：%s" % req)
        self.assertNotIn("height=undefined", req,
                         "吸附请求里不能有空的高度：%s" % req)
        self.assertIn("width=1000", req)

    def test_height_is_preserved_when_only_width_changes(self):
        out = _node_panel(_SNAP_ONE_DIM_SCRIPT)
        self.assertEqual(out["height"], 480,
                         "只改宽度时高度必须保持 480，实际 %s" % out["height"])
        self.assertEqual(out["heightInput"], "480")
        self.assertNotIn("宽高必须是数字", out["adjust"],
                         "不该出现误导性的「宽高必须是数字」：%s" % out["adjust"])

    def test_width_snaps_to_multiple_of_32(self):
        out = _node_panel(_SNAP_ONE_DIM_SCRIPT)
        self.assertEqual(out["width"], 992,
                         "1000 应被吸附成 992（32 的倍数），实际 %s" % out["width"])
        self.assertIn("1000", out["adjust"])


if __name__ == "__main__":
    unittest.main()
