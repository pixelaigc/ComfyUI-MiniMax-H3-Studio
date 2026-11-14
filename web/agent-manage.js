/* H3 Studio —— 内置 Agent「agent 管理」面板（前端）
 *
 * 【这一块管什么】
 *  1) 步数上限（1~50）
 *  2) 额外能力开关（文件读取 / 外部搜索 / 代码执行 / ffmpeg 音视频）
 *  3) 外部搜索引擎配置（DDG / 局域网 9898 聚合引擎 / 自定义接口）+ 连通性自检
 *  4) 文生视频默认参数（comfy kitchen 注意力 + dmad 加速 LoRA，可改任意一项）
 *
 * 【为什么单独一个文件】
 * agent.js 是聊天交互（2400+ 行），providers.js 是本地模型配置。
 * 「agent 管理」是第三块语义，混进前两个都会让它们更难读。
 *
 * 【数据来源】
 * 全部来自服务端 /api/agent/settings —— 前端不硬编码任何取值集合
 * （合法值、默认���、能力清单都由服务端下发）。
 */
(function () {
  "use strict";

  const state = {
    settings: null,
    capabilities: {},
    engines: [],
    t2v: {},
    limits: {},
    deps: {},
    options: { models: [], attentions: [], accels: [], aspects: [], mps: [] },
    loading: false,
    saving: false,
  };

  function $(sel) { return document.querySelector(sel); }
  function esc(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // 本地提示条。
  //
  // 【为什么不能直接用 toast()】app.js / assets.js 里的 toast 是各自
  // IIFE 里的**模块私有**函数（不在 window 上）—— 本文件拿不到。
  // 直接写 toast(...) 会抛 "toast is not defined"（真浏览器实测过）。
  // 所以这里自己实现一份，样式复用 .al-toast。
  function toast(message, kind) {
    let box = document.getElementById("agmToast");
    if (!box) {
      box = document.createElement("div");
      box.id = "agmToast";
      document.body.appendChild(box);
    }
    box.textContent = String(message || "");
    box.className = "al-toast show " + (kind || "");
    clearTimeout(box._timer);
    box._timer = setTimeout(() => { box.className = "al-toast " + (kind || ""); },
                           3200);
  }

  async function apiJSON(method, path, body) {
    const opts = { method, headers: { "Content-Type": "application/json" } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const resp = await fetch(path, opts);
    const raw = await resp.text();
    let data;
    try { data = raw ? JSON.parse(raw) : {}; }
    catch (e) { data = { error: raw || "服务端返回了非 JSON 响应" }; }
    if (!resp.ok) {
      const err = new Error(data.error || ("请求失败（HTTP " + resp.status + "）"));
      err.code = data.code || "";
      throw err;
    }
    return data;
  }

  // ------------------------------------------------------------------ 载入

  async function load() {
    state.loading = true;
    render();
    try {
      const d = await apiJSON("GET", "/api/agent/settings");
      state.settings = d.settings || {};
      state.capabilities = d.capabilities || {};
      state.engines = d.search_engines || [];
      state.t2v = d.t2v_defaults || {};
      state.limits = d.limits || {};
      state.deps = d.dependencies || {};
      state.loading = false;
      // 可选值清单随同一次响应下发（本地常量，瞬时）—— 不再单独等一个
      // 会去探测 ComfyUI 的慢端点。
      await loadOptions(d.t2v_options);
    } catch (e) {
      state.settings = null;
      state.loadError = e.message || String(e);
      state.loading = false;
    }
    render();
  }

  // 文生视频的可选值清单（模型 / 注意力 / 加速 LoRA / 比例）。
  //
  // 【为什么不走 /api/video-options】
  // 那个端点会去**探测本机 ComfyUI**（拉模型列表 + 问节点是否存在），
  // 实测能到 60 秒以上 —— 用来填下拉会让面板长时间空白。
  // 这里改用 /api/agent/settings 里带的 t2v_options（**本地常量清单**，瞬时返回）。
  // 可用性/兼容性的实测判断仍然由服务端在真正建任务前做（prepare_agent_t2v_params）。
  async function loadOptions(options) {
    const d = options || {};
    state.options = {
      models: (d.models || []).map(m => ({
        key: m.key, label: m.label || m.key,
      })),
      attentions: (d.attentions || []).map(a => ({
        key: a.key, label: a.label || a.key, note: a.note || "",
      })),
      accels: (d.accels || []).map(a => ({
        key: a.key, label: a.label || a.key,
      })),
      aspects: (d.aspects || []).map(a => ({ key: a.key, label: a.label || a.key })),
      mps: [],
    };
  }

  // ------------------------------------------------------------------ 渲染

  function render() {
    const host = $("#agManageBody");
    if (!host) return;
    if (state.loading && !state.settings) {
      host.innerHTML = '<div class="prov-empty">读取 agent 管理配置…</div>';
      return;
    }
    if (!state.settings) {
      host.innerHTML = `<div class="prov-empty error">读取失败：${esc(state.loadError || "未知原因")}</div>`;
      return;
    }
    host.innerHTML = [
      renderLimits(),
      renderCapabilities(),
      renderEngines(),
      renderT2vDefaults(),
    ].join("");
    bind();
  }

  function renderLimits() {
    const cap = Number(state.limits.max_steps_cap || 50);
    const cur = Number(state.settings.max_steps || cap);
    // 【2026-11-14 需求 6】总时限现在是**可编辑**的 ——
    // 以前这个 input 是 disabled 的（灰着改不了），值还是写死的常量。
    // 现在读的是「当前生效值」，区间由服务端下发（limits.total_timeout_min/max）。
    const timeoutMin = Number(state.limits.total_timeout_min || 60);
    const timeoutMax = Number(state.limits.total_timeout_max || 14400);
    const timeout = Math.round(Number(
      state.settings.total_timeout != null
        ? state.settings.total_timeout
        : (state.limits.total_timeout || 2400)));
    return `
      <div class="agm-section">
        <div class="agm-section-title">工具调用步数上限</div>
        <div class="agm-row">
          <label class="agm-field">
            <span>最大步骤数</span>
            <input type="number" id="agmMaxSteps" min="1" max="${cap}"
                   step="1" value="${esc(cur)}">
          </label>
          <label class="agm-field">
            <span>总时限（秒）</span>
            <input type="number" id="agmTimeout" min="${timeoutMin}" max="${timeoutMax}"
                   step="1" value="${esc(timeout)}">
          </label>
        </div>
        <div class="agm-hint">
          范围 1 ~ ${esc(cap)}。总时限随步数自动放大（单步实测 20~120 秒），
          步数上限与总时限必须匹配，否则上限只是纸面数字。<br>
          总时限可调范围 ${esc(timeoutMin)} ~ ${esc(timeoutMax)} 秒，
          改完点「保存配置」即生效（单次 Agent 运行超过该时限会返回已完成的部分结果）。
        </div>
      </div>`;
  }

  function renderCapabilities() {
    const rows = Object.values(state.capabilities || {});
    if (!rows.length) {
      return '<div class="agm-section"><div class="agm-hint">没有能力清单</div></div>';
    }
    const items = rows.map(c => `
      <div class="agm-cap">
        <label class="agm-cap-toggle">
          <input type="checkbox" data-cap="${esc(c.key)}"
                 ${c.enabled ? "checked" : ""}>
          <span class="agm-cap-name">${esc(c.label)}</span>
        </label>
        <div class="agm-cap-desc">${esc(c.description || "")}</div>
        <div class="agm-cap-req">
          ${c.builtin ? '<span class="agm-tag builtin">内置</span>' : ""}
          <span class="agm-tag req">依赖：${esc(c.requires || "无")}</span>
        </div>
      </div>`).join("");
    const codeExec = rows.find(c => c.key === "code_exec");
    const warn = codeExec && codeExec.enabled
      ? '<div class="agm-warn">代码执行已启用 —— 模型可以在本机运行 Python。'
        + '只在你信任当前会话时开启。</div>'
      : "";
    return `
      <div class="agm-section">
        <div class="agm-section-title">额外能力</div>
        ${warn}
        <div class="agm-cap-list">${items}</div>
        <div class="agm-hint">
          关掉的能力，其工具**不会**出现在给模型的工具清单里
          （模型连工具名都看不到，不会反复尝试）。
        </div>
      </div>`;
  }

  function renderEngines() {
    const rows = (state.engines || []).map(e => `
      <div class="agm-engine" data-engine-id="${esc(e.id)}">
        <div class="agm-engine-head">
          <label class="agm-engine-toggle">
            <input type="checkbox" data-eng-enabled="${esc(e.id)}"
                   ${e.enabled ? "checked" : ""}>
            <span>${esc(e.name || e.id)}</span>
          </label>
          <span class="agm-tag">${esc(e.type || "custom")}</span>
          <button type="button" class="agm-btn" data-eng-test="${esc(e.id)}">连通性自检</button>
          <button type="button" class="agm-btn danger" data-eng-del="${esc(e.id)}">删除</button>
        </div>
        <div class="agm-row">
          <label class="agm-field wide">
            <span>名称</span>
            <input type="text" data-eng-name="${esc(e.id)}"
                   value="${esc(e.name || "")}">
          </label>
          <label class="agm-field wide">
            <span>接口 URL</span>
            <input type="text" data-eng-url="${esc(e.id)}"
                   value="${esc(e.url || "")}">
          </label>
          <label class="agm-field">
            <span>类型</span>
            <select data-eng-type="${esc(e.id)}">
              <option value="openwebui"${e.type === "openwebui" ? " selected" : ""}>9898 聚合引擎</option>
              <option value="ddg"${e.type === "ddg" ? " selected" : ""}>DuckDuckGo</option>
              <option value="custom"${e.type === "custom" ? " selected" : ""}>自定义 HTTP</option>
            </select>
          </label>
          <label class="agm-field">
            <span>方法</span>
            <select data-eng-method="${esc(e.id)}">
              <option value="POST"${e.method === "POST" ? " selected" : ""}>POST</option>
              <option value="GET"${e.method === "GET" ? " selected" : ""}>GET</option>
            </select>
          </label>
        </div>
        <div class="agm-engine-test" id="agmTest_${esc(e.id)}"></div>
      </div>`).join("");
    return `
      <div class="agm-section">
        <div class="agm-section-title">外部搜索引擎</div>
        <div class="agm-engine-list">${rows}</div>
        <button type="button" class="agm-btn" id="agmAddEngine">＋ 添加搜索引擎</button>
        <div class="agm-hint">
          「9898 聚合引擎」默认指向本机
          <code>http://127.0.0.1:9898/api/openwebui/tools/search_general</code>；
          局域网其它机器访问本 Studio 时把它改成
          <code>http://192.168.3.78:9898/...</code>。
          连通性自检会**真发一次请求**，不是只校验 URL 格式。
        </div>
      </div>`;
  }

  function renderT2vDefaults() {
    const t = state.t2v || {};
    const o = state.options || {};
    // 【下拉值要能落到「当前生效值」上】
    // 可选值清单是异步拉来的（/api/video-options 会探测 ComfyUI，可能 60 秒+），
    // 第一次渲染时清单还没到 —— 这时如果下拉是空的，**当前生效值就没了**，
    // 用户会以为配置丢了。所以：把当前值**兜底**进选项里。
    const withCurrent = (list, current, label) => {
      const items = (list || []).slice();
      if (current && !items.some(i => i.key === current)) {
        items.unshift({ key: current, label: label || current });
      }
      return items;
    };
    const modelOpts = withCurrent(o.models, t.model_key).map(m =>
      `<option value="${esc(m.key)}"${t.model_key === m.key ? " selected" : ""}>${esc(m.label)}</option>`
    ).join("");
    const attnOpts = withCurrent(o.attentions, t.attention).map(a =>
      `<option value="${esc(a.key)}"${t.attention === a.key ? " selected" : ""}>${esc(a.label)}</option>`
    ).join("");
    const accelOpts = withCurrent(o.accels, t.accel_lora).map(a =>
      `<option value="${esc(a.key)}"${t.accel_lora === a.key ? " selected" : ""}>${esc(a.label)}</option>`
    ).join("");
    const aspectOpts = withCurrent(o.aspects, t.aspect).map(a =>
      `<option value="${esc(a.key)}"${t.aspect === a.key ? " selected" : ""}>${esc(a.key)}</option>`
    ).join("");
    const mp = Number(t.megapixels || 0.4);
    const dur = Number(t.duration || 5);
    return `
      <div class="agm-section">
        <div class="agm-section-title">文生视频默认参数（MiniMax H3）</div>
        <div class="agm-row">
          <label class="agm-field">
            <span>底模</span>
            <select id="agmT2vModel">${modelOpts}</select>
          </label>
          <label class="agm-field">
            <span>注意力机制</span>
            <select id="agmT2vAttention">${attnOpts}</select>
          </label>
          <label class="agm-field">
            <span>加速 LoRA</span>
            <select id="agmT2vAccel">${accelOpts}</select>
          </label>
          <label class="agm-field">
            <span>画面比例</span>
            <select id="agmT2vAspect">${aspectOpts}</select>
          </label>
          <label class="agm-field">
            <span>总像素（MP）</span>
            <input type="number" id="agmT2vMp" min="0.2" max="2" step="0.1"
                   value="${esc(mp)}">
          </label>
          <label class="agm-field">
            <span>时长（秒）</span>
            <input type="number" id="agmT2vDuration" min="2" max="60" step="1"
                   value="${esc(dur)}">
          </label>
        </div>
        <div class="agm-hint">
          默认 = comfy kitchen 注意力机制 + dmad 加速 LoRA（用户 2026-11-13 审查目标 5）。
          每一项都可以改 —— 改完立即生效，不用重启服务。
        </div>
      </div>`;
  }

  // ------------------------------------------------------------------ 收集

  function collect() {
    const settings = JSON.parse(JSON.stringify(state.settings || {}));
    const steps = Number(($("#agmMaxSteps") || {}).value);
    if (Number.isFinite(steps)) settings.max_steps = steps;

    // 【2026-11-14 需求 6】总时限：读输入框 -> 校验区间 -> 写进提交体。
    // 校验在**前端**做一次（给出即时反馈），服务端 normalize() 还会再夹一次
    // （前端校验只是 UX，服务端才是权威）。
    const timeoutEl = $("#agmTimeout");
    if (timeoutEl) {
      const min = Number(state.limits.total_timeout_min || 60);
      const max = Number(state.limits.total_timeout_max || 14400);
      const raw = Number(timeoutEl.value);
      if (Number.isFinite(raw)) {
        if (raw < min || raw > max) {
          // 越界就夹回去（而不是发一个非法值让服务端夹）——
          // 用户能立刻在输入框里看到被夹成的值。
          const clamped = Math.max(min, Math.min(max, Math.round(raw)));
          timeoutEl.value = String(clamped);
          toast(`总时限已调整到 ${clamped} 秒（允许范围 ${min}~${max}）`, "warn");
          settings.total_timeout = clamped;
        } else {
          settings.total_timeout = Math.round(raw);
        }
      }
    }

    settings.capabilities = settings.capabilities || {};
    document.querySelectorAll("[data-cap]").forEach(el => {
      settings.capabilities[el.getAttribute("data-cap")] = !!el.checked;
    });

    const engines = (state.engines || []).map(e => {
      const id = e.id;
      const nameEl = document.querySelector(`[data-eng-name="${CSS.escape(id)}"]`);
      const urlEl = document.querySelector(`[data-eng-url="${CSS.escape(id)}"]`);
      const typeEl = document.querySelector(`[data-eng-type="${CSS.escape(id)}"]`);
      const methodEl = document.querySelector(`[data-eng-method="${CSS.escape(id)}"]`);
      const enabledEl = document.querySelector(`[data-eng-enabled="${CSS.escape(id)}"]`);
      return {
        id: id,
        name: nameEl ? nameEl.value : e.name,
        url: urlEl ? urlEl.value : e.url,
        type: typeEl ? typeEl.value : e.type,
        method: methodEl ? methodEl.value : e.method,
        enabled: enabledEl ? enabledEl.checked : e.enabled,
      };
    });
    settings.search_engines = engines;

    settings.t2v_defaults = settings.t2v_defaults || {};
    const pick = (id, key, cast) => {
      const el = $(id);
      if (!el) return;
      const value = cast(el.value);
      if (value !== null && value !== undefined && !Number.isNaN(value)) {
        settings.t2v_defaults[key] = value;
      }
    };
    pick("#agmT2vModel", "model_key", v => String(v));
    pick("#agmT2vAttention", "attention", v => String(v));
    pick("#agmT2vAccel", "accel_lora", v => String(v));
    pick("#agmT2vAspect", "aspect", v => String(v));
    pick("#agmT2vMp", "megapixels", v => Number(v));
    pick("#agmT2vDuration", "duration", v => Number(v));

    return settings;
  }

  // ------------------------------------------------------------------ 交互

  function bind() {
    const saveBtn = $("#agmSave");
    if (saveBtn && !saveBtn.dataset.bound) {
      saveBtn.dataset.bound = "1";
      saveBtn.addEventListener("click", save);
    }
    const addBtn = $("#agmAddEngine");
    if (addBtn && !addBtn.dataset.bound) {
      addBtn.dataset.bound = "1";
      addBtn.addEventListener("click", () => {
        state.engines = (state.engines || []).concat([{
          id: "engine_" + ((state.engines || []).length + 1),
          name: "新搜索引擎", type: "custom",
          url: "http://127.0.0.1:9898/api/openwebui/tools/search_general",
          method: "POST", enabled: false,
        }]);
        render();
      });
    }
    document.querySelectorAll("[data-eng-test]").forEach(btn => {
      if (btn.dataset.bound) return;
      btn.dataset.bound = "1";
      btn.addEventListener("click", () => testEngine(btn.getAttribute("data-eng-test")));
    });
    document.querySelectorAll("[data-eng-del]").forEach(btn => {
      if (btn.dataset.bound) return;
      btn.dataset.bound = "1";
      btn.addEventListener("click", () => {
        const id = btn.getAttribute("data-eng-del");
        state.engines = (state.engines || []).filter(e => e.id !== id);
        render();
      });
    });
  }

  async function save() {
    const btn = $("#agmSave");
    if (state.saving) return;
    state.saving = true;
    if (btn) { btn.disabled = true; btn.textContent = "保存中…"; }
    try {
      const d = await apiJSON("PUT", "/api/agent/settings", collect());
      state.settings = d.settings || state.settings;
      state.capabilities = d.capabilities || state.capabilities;
      state.engines = state.settings.search_engines || state.engines;
      state.t2v = state.settings.t2v_defaults || state.t2v;
      toast("已保存 Agent 管理配置", "ok");
    } catch (e) {
      toast("保存失败：" + (e.message || e), "error");
    } finally {
      state.saving = false;
      if (btn) { btn.disabled = false; btn.textContent = "保存配置"; }
      render();
    }
  }

  async function testEngine(id) {
    const box = $("#agmTest_" + id);
    if (box) box.innerHTML = '<span class="agm-testing">检测中…</span>';
    const draft = (state.engines || []).find(e => e.id === id);
    const urlEl = document.querySelector(`[data-eng-url="${CSS.escape(id)}"]`);
    const typeEl = document.querySelector(`[data-eng-type="${CSS.escape(id)}"]`);
    const methodEl = document.querySelector(`[data-eng-method="${CSS.escape(id)}"]`);
    const engine = {
      id: id,
      name: draft ? draft.name : id,
      url: urlEl ? urlEl.value : (draft ? draft.url : ""),
      type: typeEl ? typeEl.value : (draft ? draft.type : "custom"),
      method: methodEl ? methodEl.value : (draft ? draft.method : "POST"),
      enabled: true,
    };
    try {
      const r = await apiJSON("POST", "/api/agent/settings/test-engine", { engine });
      if (box) {
        box.innerHTML = `<span class="agm-test-ok">✓ ${esc(r.note || "连通")}`
          + `（命中 ${esc(r.result_count || 0)} 条）</span>`;
      }
    } catch (e) {
      const payload = e.payload || {};
      if (box) {
        box.innerHTML = `<span class="agm-test-fail">✗ ${esc(payload.error || e.message)}</span>`;
      }
    }
  }

  // ------------------------------------------------------------------ 入口

  function open() {
    const modal = $("#agManageModal");
    if (!modal) return;
    modal.hidden = false;
    load();
  }

  function close() {
    const modal = $("#agManageModal");
    if (modal) modal.hidden = true;
  }

  function init() {
    const btn = $("#agManageBtn");
    if (btn) btn.addEventListener("click", open);
    const closeBtn = $("#agManageClose");
    if (closeBtn) closeBtn.addEventListener("click", close);
    const modal = $("#agManageModal");
    if (modal) {
      modal.addEventListener("click", event => {
        if (event.target === modal) close();
      });
    }
  }

  window.AgentManage = {
    init: init,
    open: open,
    close: close,
    load: load,
    _state: state,
    _collect: collect,
    _testEngine: testEngine,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
