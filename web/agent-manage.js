/* H3 Studio —— 内置 Agent「agent 管理」面板（前端）
 *
 * 【这一块管什么】
 *  1) 步数上限 / 总时限
 *  2) 额外能力开关（文件读取 / 外部搜索 / 代码执行 / ffmpeg 音视频）
 *  3) 外部搜索引擎配置（DDG / 局域网 9898 聚合引擎 / 自定义接口）+ 连通性自检
 *  4) **按生成类型分别配置的默认参数**（2026-11-14 需求 A4）
 *     —— 每个生成类型一份：Z-IMAGE 文生图 / MOSS-Image 文生图 /
 *     YuE2 音乐生成 / MiniMax H3 文生视频 / ……
 *
 * 【为什么单独一个文件】
 * agent.js 是聊天交互（2400+ 行），providers.js 是本地模型配置。
 * 「agent 管理」是第三块语义，混进前两个都会让它们更难读。
 *
 * 【数据来源】
 * 全部来自服务端 /api/agent/settings —— 前端不硬编码任何取值集合
 * （合法值、默认值、能力清单、每个生成类型的字段规格都由服务端下发）。
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
    // 【A4】按生成类型分别配置的默认参数 + 字段规格
    genDefaults: {},
    genSpec: {},
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
      // 【A4】每个生成类型一份的默认参数 + 字段规格。
      //
      // 【为什么两个都要】genDefaults 是「当前生效值」（每个生成类型一份），
      // genSpec 是「这个生成类型有哪些参数、合法取值是什么」——
      // 控件全部按 spec 渲染，前端不硬编码任何取值集合。
      state.genDefaults = d.gen_defaults || {};
      state.genSpec = d.gen_defaults_spec || {};
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
      // 【2026-11-14 需求 A4】原来这里还有一个独立的「文生视频默认参数」区块，
      // 现在它并入 renderGenDefaults() 的「文生视频」一节（沿用历史控件 id：
      // agmT2vModel / agmT2vAttention / agmT2vAccel）。
      // 【为什么不留第二份控件】两份控件指向同一份配置 = 两个真相源，
      // 用户改 A 处、B 处显示旧值，是典型的「看起来能改但存不进去」。
      renderGenDefaults(),
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
          关掉的能力，其工具「不会」出现在给模型的工具清单里
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
          <code>http://192.168.3.78:9898/api/openwebui/tools/search_general</code>。
          连通性自检会「真发一次请求」，不是只校验 URL 格式。
        </div>
      </div>`;
  }

  // ------------------------------------------------------------------ A4
  // 按生成类型分别配置的默认参数

  // t2v 的历史控件 id（浏览器验收脚本 scripts/verify_manage_panel_browser.py
  // 按这些 id 取值）—— 保留它们，避免改动既有验收脚本。
  const LEGACY_T2V_IDS = {
    model_key: "agmT2vModel",
    attention: "agmT2vAttention",
    accel_lora: "agmT2vAccel",
    aspect: "agmT2vAspect",
    megapixels: "agmT2vMp",
    duration: "agmT2vDuration",
  };

  function fieldId(mode, name) {
    if (mode === "t2v" && LEGACY_T2V_IDS[name]) return LEGACY_T2V_IDS[name];
    return `agmGen_${mode}_${name}`;
  }

  // 【下拉值要能落到「当前生效值」上】
  // 可选值清单可能与库里存的旧值不一致（模型下线 / 清单更新）——
  // 这时把当前值**兜底**进选项里，否则用户看到的下拉会是空的，
  // 以为配置丢了。
  function withCurrent(options, current) {
    const items = (options || []).slice();
    const text = current == null ? "" : String(current);
    if (text && !items.some(o => String(o.value) === text)) {
      items.unshift({ value: text, label: text + "（已下线 / 不在当前清单）" });
    }
    return items;
  }

  function renderField(mode, field, value) {
    const id = fieldId(mode, field.name);
    const label = field.label || field.name;
    const note = field.note ? ` title="${esc(field.note)}"` : "";
    const type = field.type || "string";
    // 【只读说明字段（type == "fixed"）】
    // 服务端明确说「这一项由工作流/模型固定，不可改」——
    // 那就**不给可编辑控件**。给一个 disabled 的输入框会被理解成
    // 「灰着但也许能改」，与「不可改」是两种话。
    // 这里渲染成一行纯文本说明：值是多少、由谁决定。
    if (type === "fixed" || field.readonly) {
      const shown = value == null ? (field.default == null ? "" : field.default) : value;
      return `
        <div class="agm-field agm-field-fixed">
          <span>${esc(label)}</span>
          <b class="agm-fixed-value" id="${esc(fieldId(mode, field.name))}">${esc(shown)}</b>
          ${field.note ? `<div class="agm-field-note">${esc(field.note)}</div>` : ""}
        </div>`;
    }
    let control = "";
    if (type === "enum") {
      const options = withCurrent(field.options, value)
        .map(o => `<option value="${esc(o.value)}"${String(o.value) === String(value) ? " selected" : ""}>${esc(o.label)}</option>`)
        .join("");
      control = `<select id="${esc(id)}"${note}>${options}</select>`;
    } else if (type === "number" || type === "integer") {
      const min = field.min != null ? ` min="${esc(field.min)}"` : "";
      const max = field.max != null ? ` max="${esc(field.max)}"` : "";
      const step = field.step != null ? ` step="${esc(field.step)}"` : " step=\"1\"";
      control = `<input type="number" id="${esc(id)}"${min}${max}${step}`
        + ` value="${esc(value == null ? "" : value)}"${note}>`;
    } else if (type === "boolean") {
      control = `<input type="checkbox" id="${esc(id)}"${value ? " checked" : ""}${note}>`;
    } else {
      control = `<input type="text" id="${esc(id)}" value="${esc(value == null ? "" : value)}"${note}>`;
    }
    const hint = field.note ? `<div class="agm-field-note">${esc(field.note)}</div>` : "";
    return `
      <label class="agm-field">
        <span>${esc(label)}</span>
        ${control}
        ${hint}
      </label>`;
  }

  function renderGenDefaults() {
    const spec = state.genSpec || {};
    const modes = Object.keys(spec);
    if (!modes.length) {
      return '<div class="agm-section"><div class="agm-hint">服务端未下发生成类型规格</div></div>';
    }
    const sections = modes.map(mode => {
      const info = spec[mode] || {};
      const label = info.label || mode;
      const supported = !!info.supported;
      const values = (state.genDefaults || {})[mode] || {};
      const fields = info.fields || [];
      if (!supported) {
        // 【为什么要显式标注「无参数」而不是留空白】
        // 留空白 = 用户以为「这里还没配」；显式标注 = 用户知道
        // 这个生成类型**本来就没有**可调的生成参数。
        return `
          <details class="agm-gen">
            <summary class="agm-gen-head">
              <span class="agm-gen-title">${esc(label)}</span>
              <span class="agm-tag">${esc(mode)}</span>
              <span class="agm-tag req">无可调参数</span>
            </summary>
            <div class="agm-gen-body">
              <div class="agm-hint">${esc(info.note || "该生成类型没有可调的生成参数。")}</div>
            </div>
          </details>`;
      }
      const controls = fields.map(f => {
        const value = values[f.name] !== undefined ? values[f.name] : f.default;
        return renderField(mode, f, value);
      }).join("");
      let note = info.note ? `<div class="agm-hint">${esc(info.note)}</div>` : "";
      if (mode === "t2v") {
        // 【兼容说明】这一节的控件沿用**历史 id**（agmT2vModel /
        // agmT2vAttention / agmT2vAccel / agmT2vAspect / agmT2vMp /
        // agmT2vDuration）—— 外部验收脚本与老脚本按这些 id 取值，
        // 改了会让它们失效。值与「t2v_defaults」是同一份。
        note += `<div class="agm-hint">这一节的控件沿用历史 id`
          + `（agmT2vModel / agmT2vAttention / agmT2vAccel / agmT2vAspect /`
          + ` agmT2vMp / agmT2vDuration），与老配置项 <code>t2v_defaults</code>`
          + ` 是同一份数据 —— 保存时两边同步。</div>`;
      }
      // 【为什么 t2v 默认展开】它是历史上唯一的默认参数入口，
      // 折叠起来会让用户以为「文生视频的默认参数不见了」。
      const open = mode === "t2v" ? " open" : "";
      return `
        <details class="agm-gen"${open}>
          <summary class="agm-gen-head">
            <span class="agm-gen-title">${esc(label)}</span>
            <span class="agm-tag">${esc(mode)}</span>
          </summary>
          <div class="agm-gen-body">
            <div class="agm-row">${controls}</div>
            ${note}
          </div>
        </details>`;
    }).join("");
    return `
      <div class="agm-section">
        <div class="agm-section-title">按生成类型配置默认参数</div>
        <div class="agm-hint">
          每个生成类型一份默认参数。Agent 建任务时：<b>用户/模型显式给的参数优先</b>，
          没给的用这里的值；再没有才用工作流内置默认值。
          改完点「保存配置」即生效（不用重启服务）。
        </div>
        <div class="agm-gen-list">${sections}</div>
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

    // ---- A4：按生成类型分别配置的默认参数 ----
    //
    // 【为什么要按 spec 的字段表来读】服务端下发的字段规格是**唯一真源**，
    // 前端不硬编码任何字段名/类型/取值 —— 加一个新生成类型时，
    // 前端一行代码都不用改。
    const spec = state.genSpec || {};
    const genDefaults = {};
    Object.keys(spec).forEach(mode => {
      const info = spec[mode] || {};
      if (!info.supported) return;
      // 【先带上存量值，再用可编辑控件覆盖】
      //
      // 【为什么必须先带上】字段表里现在有两类字段**没有可编辑控件**：
      //   * type == "fixed"（只读说明：采样档位 / sigma shift / 精度）
      //   * 已经不再作为控件暴露、但存量配置里还躺着的 legacy 字段
      //     （旧的分辨率档位 / 底模精度 / steps / sampler / shift …）
      // 如果这里只收集「有控件的字段」，用户点一次「保存配置」就会把
      // 那些值从配置里**抹掉** —— 用户会以为「我配过的值凭空消失了」。
      // 用户 2026-11-21 明确要求：只从 UI 移除，不删除已保存的记录。
      const values = { ...((state.genDefaults || {})[mode] || {}) };
      (info.fields || []).forEach(f => {
        if (f.type === "fixed" || f.readonly) return;   // 只读说明不提交
        const el = $("#" + CSS.escape(fieldId(mode, f.name)));
        if (!el) return;
        values[f.name] = readField(f, el);
      });
      genDefaults[mode] = values;
    });
    settings.gen_defaults = genDefaults;

    // ---- 兼容镜像：老的 t2v_defaults ----
    //
    // 【为什么还要写它】老客户端 / 老脚本只认 t2v_defaults。
    // 服务端会把两者同步（写入时以 gen_defaults 为准，
    // 但**显式给出**的老字段优先），所以这里照旧提交一份。
    //
    // 【注意】t2v 的控件用的是**历史 id**（agmT2vModel 等），
    // 所以上面的采集已经把值读进 gen_defaults.t2v 了 ——
    // 这里只是把它投影成老结构（6 个字段）。
    const t2vValues = genDefaults.t2v || {};
    settings.t2v_defaults = {
      model_key: t2vValues.model_key || (state.t2v || {}).model_key || "",
      attention: t2vValues.attention || (state.t2v || {}).attention || "",
      accel_lora: t2vValues.accel_lora || (state.t2v || {}).accel_lora || "",
      aspect: t2vValues.aspect || (state.t2v || {}).aspect || "",
      megapixels: t2vValues.megapixels != null
        ? t2vValues.megapixels : (state.t2v || {}).megapixels,
      duration: t2vValues.duration != null
        ? t2vValues.duration : (state.t2v || {}).duration,
    };

    return settings;
  }

  function readField(field, el) {
    const type = field.type || "string";
    if (type === "enum") return el.value;
    if (type === "number" || type === "integer") {
      const raw = el.value;
      if (raw === "" || raw == null) return field.default;
      const num = Number(raw);
      if (!Number.isFinite(num)) return field.default;
      return type === "integer" ? Math.round(num) : num;
    }
    if (type === "boolean") return !!el.checked;
    return el.value;
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
    bindDerivedPixels();
  }

  // ------------------------------------------------------------------
  // 「生效像素」只读派生项 —— 随 MP 预算 / 百分比 / 比例实时更新
  //
  // 【为什么需要】三个视频面板共用「画面比例 × MP 预算 × 百分比」
  // 这一套尺寸表达（服务端字段表，见 h3studio/video_panel_params.py）。
  // 「生效像素」是这套表达的**派生结果**，用户改预算时必须立刻看到
  // 「我填的 0.8 MP 到底等于多少像素」—— 否则又是一处「面板说一套、
  // 产物另一套」。
  //
  // 【为什么走服务端】换算公式在 ``size_spec.plan_size``（与建任务路径
  // **同一份**）。前端自己算 = 第二份公式 = 必然漂移。
  // 端点：``GET /api/video-size-plan?mode=&aspect=&megapixels=&percent=``
  //
  // 【为什么只在本地改、不落库】派生项是 ``type == "fixed"``，
  // 服务端不会把它写进 params（它由其它三项算出）。
  function bindDerivedPixels() {
    const spec = state.genSpec || {};
    Object.keys(spec).forEach(mode => {
      const info = spec[mode] || {};
      const names = (info.fields || []).map(f => f.name);
      if (!names.includes("derived_pixels")) return;
      if (!names.includes("megapixels")) return;

      const controls = ["aspect", "megapixels", "resolution_percent"]
        .map(n => $("#" + CSS.escape(fieldId(mode, n))))
        .filter(Boolean);
      if (!controls.length) return;

      let timer = null;
      const refresh = () => {
        const aspectEl = $("#" + CSS.escape(fieldId(mode, "aspect")));
        const mpEl = $("#" + CSS.escape(fieldId(mode, "megapixels")));
        const pctEl = $("#" + CSS.escape(fieldId(mode, "resolution_percent")));
        const outEl = $("#" + CSS.escape(fieldId(mode, "derived_pixels")));
        if (!outEl) return;
        const aspect = aspectEl ? aspectEl.value : "16:9";
        const megapixels = mpEl ? mpEl.value : "";
        const percent = pctEl ? pctEl.value : "";
        if (megapixels === "" && percent === "") {
          outEl.textContent = "—";
          return;
        }
        const url = "/api/video-size-plan"
          + "?mode=" + encodeURIComponent(mode)
          + "&aspect=" + encodeURIComponent(aspect)
          + "&megapixels=" + encodeURIComponent(megapixels)
          + "&percent=" + encodeURIComponent(percent);
        fetch(url).then(r => r.json()).then(d => {
          if (!d || d.error) {
            outEl.textContent = "—";
            return;
          }
          outEl.textContent = d.label || "—";
        }).catch(() => { outEl.textContent = "—"; });
      };

      controls.forEach(el => {
        const evt = (el.tagName === "SELECT") ? "change" : "input";
        el.addEventListener(evt, () => {
          if (timer) clearTimeout(timer);
          timer = setTimeout(refresh, 200);
        });
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
      state.genDefaults = d.gen_defaults || state.settings.gen_defaults || {};
      if (d.gen_defaults_spec) state.genSpec = d.gen_defaults_spec;
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
