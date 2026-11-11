# -*- coding: utf-8 -*-
"""文生视频面板「接口失败时下拉不能空白」的回归测试。

用户 2026-11-11 21:06 实机报缺陷：8190 页面「文生视频」面板前端崩了，
尺寸预设 / 输出尺寸 / MiniMax-H3 模型 / 加速 LoRA / 注意力机制
**全部空白**（控件在、选项没有），模型框下面还挂了一条橙色空条。

根因（实测复现，见交付报告）：
  1. video-panel.js 的 loadVideoOptions() 在 /api/video-options 失败时
     直接进 catch，**renderVideoPanel() 从未执行** -> 全下拉空白；
  2. .img-warn 自带橙色背景 + padding + 边框，**空内容也渲染成橙条**；
  3. 服务端 refresh_comfy_node_names() 串行问 6 个节点 × 10 秒超时，
     ComfyUI 卡住时 /api/video-options 要 **66 秒**才返回 —— 浏览器先放弃。

本文件锁死这几条不再回归：

1. **接口失败/空返回时，各下拉仍必须有选项**
   - 尺寸预设 / 模型 / 加速 LoRA / 注意力机制 / 画面比例 chips
2. **失败时不得抛 JS 异常**（page error 必须为空）
3. **失败时必须有明确错误提示**（banner 可见 + 文本含原因）
4. **空 .img-warn 不得渲染成橙色空条**（hidden 或高度为 0）
5. **一个区块渲染失败不得阻断其它区块**
6. **服务端节点探测不得串行拖慢**
   - ComfyUI 挂住时 /api/video-options 必须在**几秒内**返回
7. **兜底清单与服务端清单形状一致**
   - size_presets 是**扁平 list**（形状错会在 renderSizePresetSelect
     里抛 "object is not iterable"）

运行：python3 -m pytest tests/test_panel_fallback.py -v
"""
import json
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import video_models as VM       # noqa: E402


# ------------------------------------------------------------------
# 1. 前端源码契约
# ------------------------------------------------------------------
class TestFrontendFallbackContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (REPO / "web" / "video-panel.js").read_text(
            encoding="utf-8", errors="ignore")
        cls.app = (REPO / "web" / "app.js").read_text(
            encoding="utf-8", errors="ignore")
        cls.html = (REPO / "web" / "index.html").read_text(
            encoding="utf-8", errors="ignore")

    def test_fallback_manifest_file_exists_and_loaded(self):
        """兜底清单文件必须在，且 index.html 要引它（否则永远拿不到）。"""
        self.assertTrue((REPO / "web" / "video-panel-fallback.js").is_file(),
                        "缺 web/video-panel-fallback.js")
        self.assertIn("video-panel-fallback.js", self.html,
                      "index.html 没引兜底清单脚本")

    def test_catch_block_still_renders(self):
        """catch 里必须调用 renderVideoPanel() —— 否则下拉全空。"""
        # 取出 catch 块，确认里面有 renderVideoPanel
        idx = self.js.find("async function loadVideoOptions")
        self.assertGreater(idx, 0, "找不到 loadVideoOptions")
        body = self.js[idx:idx + 4000]
        catch_at = body.find("} catch (e) {")
        self.assertGreater(catch_at, 0, "loadVideoOptions 没有 catch")
        catch_body = body[catch_at:catch_at + 2500]
        self.assertIn("renderVideoPanel()", catch_body,
                      "catch 分支里没有 renderVideoPanel() —— "
                      "接口失败时面板不会被渲染（下拉全空）")

    def test_catch_block_sets_options_ready(self):
        """catch 里必须把 optionsReady 置 true，否则 renderVideoPanel 直接 return。"""
        idx = self.js.find("async function loadVideoOptions")
        body = self.js[idx:idx + 4000]
        catch_at = body.find("} catch (e) {")
        catch_body = body[catch_at:catch_at + 2500]
        self.assertIn("optionsReady = true", catch_body,
                      "catch 分支没有置 optionsReady=true —— "
                      "renderVideoPanel() 会直接 return（下拉全空）")

    def test_fallback_manifest_is_applied(self):
        self.assertIn("applyFallbackManifest", self.js)
        self.assertIn("VIDEO_PANEL_FALLBACK", self.js)

    def test_fallback_notice_is_rendered(self):
        """兜底态必须有明确的错误提示，不能静默。"""
        self.assertIn("renderFallbackNotice", self.js)
        self.assertIn("内置兜底清单", self.js)

    def test_render_steps_are_independently_guarded(self):
        """每个区块独立 try/catch —— 一个失败不阻断其它。"""
        idx = self.js.find("function renderVideoPanel")
        self.assertGreater(idx, 0)
        body = self.js[idx:idx + 2000]
        self.assertIn("steps.forEach", body,
                      "renderVideoPanel 没有逐段保护 —— "
                      "一处抛错后面全部下拉都不渲染")
        self.assertIn("catch (e)", body)

    def test_empty_warn_is_hidden(self):
        """空 .img-warn 必须隐藏 —— 否则渲染成橙色空条。"""
        idx = self.js.find("function renderModelNote")
        self.assertGreater(idx, 0)
        body = self.js[idx:idx + 1500]
        self.assertIn("warn.hidden", body,
                      "renderModelNote 没有隐藏空的 .img-warn —— "
                      "会留下一条橙色空条")

    def test_image_panel_warn_also_hidden(self):
        """图片面板的 #imgModelWarn 有同样的问题，一并修。"""
        self.assertIn("imgWarn.hidden", self.app)

    def test_fallback_manifest_shape_is_list(self):
        """兜底清单的 size_presets 必须是**扁平 list**（与服务端同形）。"""
        src = (REPO / "web" / "video-panel-fallback.js").read_text(
            encoding="utf-8", errors="ignore")
        m = re.search(r"window\.VIDEO_PANEL_FALLBACK\s*=\s*(\{.*\});?\s*$",
                      src, re.S)
        self.assertIsNotNone(m, "兜底清单不是合法 JSON")
        data = json.loads(m.group(1))
        self.assertIsInstance(data["size_presets"], list,
                              "size_presets 必须是 list（服务端同形）")
        self.assertTrue(data["size_presets"],
                        "size_presets 不能为空")
        for item in data["size_presets"]:
            for key in ("key", "label", "aspect", "width", "height"):
                self.assertIn(key, item, f"兜底预设缺字段 {key}")

    def test_fallback_models_have_keys(self):
        src = (REPO / "web" / "video-panel-fallback.js").read_text(
            encoding="utf-8", errors="ignore")
        m = re.search(r"window\.VIDEO_PANEL_FALLBACK\s*=\s*(\{.*\});?\s*$",
                      src, re.S)
        data = json.loads(m.group(1))
        self.assertTrue(data["models"], "兜底模型清单为空")
        for item in data["models"]:
            self.assertIn("key", item)
            self.assertIn("label", item)
            self.assertIn("unet", item)

    def test_fallback_covers_all_server_models(self):
        """兜底清单必须覆盖服务端登记的**全部**底模。"""
        src = (REPO / "web" / "video-panel-fallback.js").read_text(
            encoding="utf-8", errors="ignore")
        m = re.search(r"window\.VIDEO_PANEL_FALLBACK\s*=\s*(\{.*\});?\s*$",
                      src, re.S)
        data = json.loads(m.group(1))
        fb_keys = {m2["key"] for m2 in data["models"]}
        srv_keys = set(VM.VIDEO_MODELS.keys())
        self.assertEqual(srv_keys - fb_keys, set(),
                         f"兜底清单漏了模型：{srv_keys - fb_keys}")

    def test_fallback_size_presets_match_server(self):
        """兜底预设必须与服务端逐条相同（不能手抄错）。"""
        src = (REPO / "web" / "video-panel-fallback.js").read_text(
            encoding="utf-8", errors="ignore")
        m = re.search(r"window\.VIDEO_PANEL_FALLBACK\s*=\s*(\{.*\});?\s*$",
                      src, re.S)
        data = json.loads(m.group(1))
        self.assertEqual(data["size_presets"], VM.all_size_presets(),
                         "兜底预设与服务端不一致 —— 尺寸会算错")


# ------------------------------------------------------------------
# 2. 服务端：ComfyUI 挂住时不能拖慢接口
# ------------------------------------------------------------------
class TestComfyProbeIsNotSerial(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = (REPO / "server.py").read_text(encoding="utf-8",
                                                 errors="ignore")

    def test_refresh_uses_gather(self):
        """节点探测必须并发（asyncio.gather），不能串行。"""
        idx = self.src.find("async def refresh_comfy_node_names")
        self.assertGreater(idx, 0, "找不到 refresh_comfy_node_names")
        body = self.src[idx:idx + 3000]
        self.assertIn("asyncio.gather", body,
                      "refresh_comfy_node_names 没有并发 —— "
                      "ComfyUI 挂住时接口要等 6 倍超时")

    def test_no_serial_probe_loop(self):
        """**不能有串行的 for 循环逐个 await**。

        为什么单独查：源码里只要**别处**还有一句 asyncio.gather，
        「有没有 gather」的字符串检查就会通过 —— 但真机上仍然是串行
        （实测变异后 22 项测试全绿，接口照样要 60 秒）。
        所以这里直接查「有没有 `for ... : await _probe(...)` 这种串行写法」。
        """
        idx = self.src.find("async def refresh_comfy_node_names")
        body = self.src[idx:idx + 3000]
        # 串行写法：for 循环体里直接 await
        serial = re.findall(r"for\s+\w+\s+in\s+_VIDEO_PANEL_NODES\s*:\s*\n"
                            r"\s*(?:.*\n){0,3}?\s*await\s+_probe", body)
        self.assertEqual(serial, [],
                         f"仍有串行探测：{serial} —— ComfyUI 挂住时接口会被拖死")

    def test_probe_timeout_is_short(self):
        """单个探测超时必须短（<= 5 秒），否则接口仍然会被拖死。"""
        idx = self.src.find("async def refresh_comfy_node_names")
        body = self.src[idx:idx + 3000]
        m = re.search(r"ClientTimeout\(total=(\d+)\)", body)
        self.assertIsNotNone(m, "找不到 ClientTimeout")
        self.assertLessEqual(int(m.group(1)), 5,
                              f"探测超时 {m.group(1)} 秒太长 —— "
                              "ComfyUI 挂住时接口仍会被拖死")

    def test_probe_keeps_last_known_good(self):
        """探测失败要保留上一次成功的缓存，不要清空。"""
        idx = self.src.find("async def refresh_comfy_node_names")
        body = self.src[idx:idx + 3000]
        self.assertIn("if found:", body,
                      "探测失败时没有区分「全挂」与「真没有」—— "
                      "会把已知可用的节点缓存清空")


class TestProbeTimingBehaviour(unittest.IsolatedAsyncioTestCase):
    """**真跑** refresh_comfy_node_names，量它到底多久返回。

    这是「ComfyUI 挂住时接口不能被拖死」的**行为级**断言 ——
    源码检查抓不住「别处还有 gather」这种伪装。
    """

    async def test_hung_comfyui_returns_quickly(self):
        """ComfyUI 接受连接但永不响应时，探测必须在几秒内返回。"""
        import asyncio
        import socket
        import threading
        import time

        # 假 ComfyUI：接受连接但永不回应（模拟 ComfyUI 启动中/卡住）
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(16)
        port = srv.getsockname()[1]
        held = []

        def accept_loop():
            while True:
                try:
                    c, _ = srv.accept()
                    held.append(c)
                except OSError:
                    break

        threading.Thread(target=accept_loop, daemon=True).start()

        sys.path.insert(0, str(REPO))
        import importlib
        import server as server_mod
        importlib.reload(server_mod)
        # 指向假 ComfyUI
        server_mod.COMFY = f"http://127.0.0.1:{port}"
        server_mod._COMFY_NODE_NAMES = set()
        server_mod._COMFY_NODES_TS = 0.0

        try:
            t0 = time.time()
            await server_mod.refresh_comfy_node_names(force=True)
            elapsed = time.time() - t0
            # 串行 6×10 秒 = 60 秒；并发 1×3 秒 = 3 秒
            self.assertLess(elapsed, 10.0,
                            f"探测耗时 {elapsed:.1f} 秒 —— 串行了？"
                            "（并发应为 ~3 秒，串行会是 ~60 秒）")
        finally:
            srv.close()
            for c in held:
                try:
                    c.close()
                except OSError:
                    pass


# ------------------------------------------------------------------
# 3. 真浏览器行为（Node + DOM shim）
# ------------------------------------------------------------------
_DOM_SHIM = r"""
class FakeElement {
  constructor(tag) {
    this.tagName = (tag || "div").toUpperCase();
    this.children = []; this._text = ""; this.className = "";
    this.hidden = false; this.disabled = false; this._value = "";
    this.title = ""; this.dataset = {}; this.style = {};
    this._listeners = {};
    this._classes = new Set();
    // 真 DOM 的 classList
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
  set innerHTML(v) {
    this._text = String(v);
    this._innerHTML = String(v);
    this.children = [];
  }
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
  // 真 DOM：select.options 是它内部的 <option> 集合
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

// ---- 可控的 fetch ----
global.FETCH_MODE = "ok";
global.fetch = async function (url) {
  const u = String(url);
  if (u.indexOf("/api/video-options") === 0) {
    if (global.FETCH_MODE === "fail") throw new Error("Failed to fetch");
    if (global.FETCH_MODE === "500") {
      return { ok: false, status: 500, json: async () => ({}) };
    }
    return { ok: true, status: 200, json: async () => global.VIDEO_OPTIONS_PAYLOAD };
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


def _run_node(script: str, timeout: int = 60) -> dict:
    import shutil
    import subprocess
    node = shutil.which("node") or shutil.which("nodejs")
    assert node is not None, "本机没有 node"
    tmp = Path("/tmp/h3-panel-fallback-test")
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
    return json.loads(proc.stdout.strip().splitlines()[-1])


IDS = ["vidModelSelect", "vidModelNote", "vidModelWarn", "vidSizePresetSelect",
       "vidAccelLoraSelect", "vidAccelLoraNote", "vidAccelLoraStrength",
       "vidAccelLoraStrengthVal", "vidAccelLoraStrengthField",
       "vidAttentionSelect", "vidAttentionNote", "vidTwopassEnabled",
       "vidTwopassBody", "vidTwopassNote", "vidTwopassHint", "vidUpscalerSelect",
       "vidTransition", "vidTransitionVal", "vidLowres", "vidLowresVal",
       "vidRho", "vidRhoVal", "vidAspectChips", "vidWidth", "vidHeight",
       "vidSizeHint", "vidSizeNote", "vidSizeMode", "vidSizeAdjust",
       "vidSizePresetNote", "vidFixedLoraNote", "vidSizeReset", "seed",
       "staleVideoBanner"]


def _render(mode: str, payload=None) -> dict:
    """按 mode 载入面板，返回各下拉的选项数与提示状态。"""
    panel_js = (REPO / "web" / "video-panel.js").read_text(
        encoding="utf-8", errors="ignore")
    fallback_js = (REPO / "web" / "video-panel-fallback.js").read_text(
        encoding="utf-8", errors="ignore")
    script = f"""
global.makeRegistry({json.dumps(IDS)});
global.FETCH_MODE = {json.dumps(mode)};
global.VIDEO_OPTIONS_PAYLOAD = {json.dumps(payload or {})};
{fallback_js}
{panel_js}
(async () => {{
  const errors = [];
  const origError = console.error;
  console.error = (...a) => {{ errors.push(String(a[0])); }};
  try {{ await window.VIDEO_PANEL.load(); }} catch (e) {{
    errors.push('THROWN:' + e.message);
  }}
  console.error = origError;
  const sel = id => document.getElementById(id);
  const cnt = id => {{ const e = sel(id); return e && e.options ? e.options.length : -1; }};
  console.log(JSON.stringify({{
    errors: errors,
    sizePresets: cnt('vidSizePresetSelect'),
    models: cnt('vidModelSelect'),
    accelLoras: cnt('vidAccelLoraSelect'),
    attention: cnt('vidAttentionSelect'),
    aspectChips: sel('vidAspectChips') ? sel('vidAspectChips').children.length : -1,
    width: sel('vidWidth') ? sel('vidWidth').value : '',
    height: sel('vidHeight') ? sel('vidHeight').value : '',
    warnText: sel('vidModelWarn') ? sel('vidModelWarn').textContent : '',
    warnHidden: sel('vidModelWarn') ? sel('vidModelWarn').hidden : null,
    bannerHidden: sel('staleVideoBanner') ? sel('staleVideoBanner').hidden : null,
    bannerText: sel('staleVideoBanner') ? sel('staleVideoBanner').textContent : '',
    ready: window.VIDEO_PANEL.isReady(),
    submitModelKey: window.VIDEO_PANEL.submitParams().model_key,
  }}));
}})();
"""
    return _run_node(script)


@unittest.skipIf(
    not (__import__("shutil").which("node") or __import__("shutil").which("nodejs")),
    "本机没有 node")
class TestPanelRendersOnApiFailure(unittest.TestCase):
    """接口失败时，面板**必须**仍然渲染出选项（真跑 JS）。"""

    def test_connection_refused_keeps_all_dropdowns_populated(self):
        out = _render("fail")
        self.assertGreater(out["sizePresets"], 1,
                           f"尺寸预设没有选项：{out}")
        self.assertGreater(out["models"], 0,
                           f"模型下拉没有选项：{out}")
        self.assertGreater(out["accelLoras"], 1,
                           f"加速 LoRA 没有选项：{out}")
        self.assertGreater(out["attention"], 0,
                           f"注意力机制没有选项：{out}")
        self.assertGreater(out["aspectChips"], 0,
                           f"画面比例 chips 为空：{out}")

    def test_connection_refused_raises_no_js_error(self):
        """不能抛未捕获异常（page error 必须为空）。"""
        out = _render("fail")
        thrown = [e for e in out["errors"] if e.startswith("THROWN:")]
        self.assertEqual(thrown, [], f"抛了未捕获异常：{thrown}")
        # 「object is not iterable」= 兜底清单与服务端形状不一致
        not_iterable = [e for e in out["errors"]
                        if "not iterable" in e]
        self.assertEqual(not_iterable, [],
                         f"兜底清单形状与服务端不一致：{not_iterable}")

    def test_connection_refused_shows_clear_error(self):
        out = _render("fail")
        self.assertFalse(out["bannerHidden"],
                         "失败时必须有可见的错误提示，不能静默")
        self.assertIn("Failed to fetch", out["bannerText"])
        self.assertIn("兜底", out["bannerText"])

    def test_http_500_keeps_all_dropdowns_populated(self):
        out = _render("500")
        self.assertGreater(out["sizePresets"], 1)
        self.assertGreater(out["models"], 0)
        self.assertGreater(out["accelLoras"], 1)
        self.assertGreater(out["attention"], 0)

    def test_http_500_shows_clear_error(self):
        out = _render("500")
        self.assertFalse(out["bannerHidden"])
        self.assertIn("HTTP 500", out["bannerText"])

    def test_panel_is_ready_and_submittable_after_failure(self):
        """兜底态下仍要能提交（服务端会再校验一次）。"""
        out = _render("fail")
        self.assertTrue(out["ready"], "兜底态下 panel 仍应就绪")
        self.assertTrue(out["submitModelKey"],
                        "兜底态下应能选出一个模型并提交")
        self.assertEqual(out["width"], "864")
        self.assertEqual(out["height"], "480")

    def test_empty_warn_not_shown_when_no_warning(self):
        """没有告警内容时，.img-warn 必须是隐藏的（不留橙色空条）。"""
        # 服务端正常返回、但模型没有 warn 字段
        payload = {
            "models": [{"key": "m1", "label": "模型A", "unet": "a.safetensors",
                        "available": True, "note": "说明"}],
            "default_model": "m1",
            "size_presets": [{"key": "16:9_0.4", "label": "480p",
                              "aspect": "16:9", "width": 864, "height": 480,
                              "megapixels": 0.4}],
            "attention_modes": [{"key": "default", "label": "默认", "node": None}],
            "default_attention": "default",
            "accel_lora_combos": [],
            "fixed_accel_loras": [],
            "aspects": [{"key": "16:9", "ratio": [16, 9]}],
        }
        out = _render("ok", payload)
        self.assertTrue(out["warnHidden"],
                        "无告警内容时 .img-warn 必须隐藏（否则是橙色空条）")
        self.assertEqual(out["warnText"], "")


if __name__ == "__main__":
    unittest.main()
