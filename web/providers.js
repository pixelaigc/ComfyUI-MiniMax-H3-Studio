/* H3 Studio —— 本地模型配置面板 + 提示词增强迷你 Agent（前端） */

// 全局状态：Provider 列表 / 默认项 / 增强 Agent 配置
const provState = {
  providers: [],
  types: [],
  defaultId: null,
  defaultMmId: null,
  editingId: null,        // null = 新增
  agent: { specs: [], params: {}, lengthPresets: {} },
  specId: "image",
  lastEnhance: null,      // 最近一次增强结果，供回填用
  // 模板过滤上下文（服务端下发规则，前端只负责按当前上下文取用）
  modeFilters: {},        // { t2i: ["image","image-scalist",...], t2v: ["video"], ... }
  modeDefaults: {},       // { t2i: "image", t2v: "video", ... }
  modelFamilies: {},      // { z_image_turbo_bf16: "z_image", lora: "minimax_h3", ... }
  modeMedia: {},          // { t2i: "image", t2v: "video", ... } 模式 -> 媒体类型
  specIdByMode: {},       // { t2i: "image", ... } 各模式下用户最后选中的模板
};

// 当前工作模式
function enhanceCurrentMode() {
  if (typeof state === "undefined" || !state) return "";
  return state.mode || "";
}

// 当前选中的模型 key（图像模式用各自的图像模型，视频模式用 MiniMax-H3 两档）
// 注意：Qwen-Image 2.1 的两个模式（t2i_qwen / i2i_qwen）用的是
// state.imgQwen.model，不是 state.img.model —— 取错会让模型族过滤失效，
// 于是 Qwen 模式下也会列出 Z-Image 专属模板。
function enhanceCurrentModelKey() {
  if (typeof state === "undefined" || !state) return "";
  const mode = state.mode || "";
  if (mode === "t2i") return (state.img && state.img.model) || "";
  if (mode === "t2i_qwen" || mode === "i2i_qwen") {
    return (state.imgQwen && state.imgQwen.model) || "";
  }
  return state.model || "";
}

/**
 * 按「媒体类型 + 模型族」过滤模板（与服务端 h3studio/enhancer.py 同规则）。
 * 媒体类型由模式决定，模型族由当前模型决定 —— 两条都匹配才算候选。
 */
function enhanceSpecsForContext(mode, modelKey) {
  const all = provState.agent.specs || [];
  if (!mode) return all;
  const ids = provState.modeFilters[mode];
  if (!Array.isArray(ids) || !ids.length) return all;
  const byId = {};
  all.forEach(s => { byId[s.id] = s; });
  let list = ids.map(id => byId[id]).filter(Boolean);
  // 媒体类型过滤：模板 media 必须与当前模式的媒体一致
  const media = provState.modeMedia[mode];
  if (media) list = list.filter(s => !s.media || s.media === media);
  // 模型族过滤：模板 families 含 "*" 或含当前模型族
  const family = provState.modelFamilies[modelKey];
  if (family) {
    list = list.filter(s => {
      const fams = s.families || [];
      if (!fams.length) return true;      // 未声明 = 通用模板
      if (fams.indexOf("*") >= 0) return true;
      return fams.indexOf(family) >= 0;
    });
  }
  return list;
}

// 该上下文下的默认模板：优先 modeDefaults，其次候选列表第一个
function enhanceDefaultSpecForContext(mode, modelKey, available) {
  const list = available || enhanceSpecsForContext(mode, modelKey);
  if (!list.length) return null;
  const preferred = provState.modeDefaults[mode];
  if (preferred && list.some(s => s.id === preferred)) return preferred;
  return list[0].id;
}

/**
 * 切换模式 / 模型后重新对齐模板：
 *   - 当前模板仍适用 -> 保持
 *   - 当前模板不适用 -> 回落到该上下文的默认 / 首个可用模板
 * 返回 { changed, specId }，changed 表示是否发生了回落。
 */
function alignSpecWithContext(mode, modelKey) {
  const available = enhanceSpecsForContext(mode, modelKey);
  if (!available.length) return { changed: false, specId: provState.specId };
  const ids = available.map(s => s.id);
  if (ids.includes(provState.specId)) {
    provState.specIdByMode[mode] = provState.specId;
    return { changed: false, specId: provState.specId };
  }
  // 优先回到该模式上次用过的模板，其次模式默认
  const remembered = provState.specIdByMode[mode];
  let next = (remembered && ids.includes(remembered)) ? remembered : null;
  if (!next) next = enhanceDefaultSpecForContext(mode, modelKey, available);
  if (!next) return { changed: false, specId: provState.specId };
  const changed = next !== provState.specId;
  provState.specId = next;
  provState.specIdByMode[mode] = next;
  return { changed, specId: next };
}

const PROVIDER_TYPE_LABELS = {
  openai: "OpenAI 兼容 API",
  ollama: "Ollama",
  llamacpp: "llama.cpp server",
  comfyui: "ComfyUI 本地节点",
};

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function apiJSON(method, path, body) {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const resp = await fetch(path, opt);
  const raw = await resp.text();
  let data;
  try { data = raw ? JSON.parse(raw) : {}; }
  catch (e) { data = { error: raw || "服务返回了非 JSON 内容" }; }
  if (!resp.ok) {
    const err = new Error(data.error || `请求失败（HTTP ${resp.status}）`);
    err.status = resp.status;
    err.payload = data;
    throw err;
  }
  return data;
}

// ------------------------------------------------------------------ 页面切换
// 注意：showCreate / showStitch / showSystem 定义在 app.js，
// 这里只绑定本页入口，页面互斥由 app.js 里各 show* 函数负责隐藏 providersPage。
$("#navProviders").addEventListener("click", showProviders);

// ------------------------------------------------------------------ Provider 列表
async function loadProviders() {
  const sub = $("#provSub");
  sub.textContent = "加载中…";
  try {
    const d = await apiJSON("GET", "/api/providers");
    provState.providers = d.providers || [];
    provState.types = d.types || [];
    provState.defaultId = d.default_provider_id;
    provState.defaultMmId = d.default_multimodal_provider_id;
    renderProviders();
    const def = provState.providers.find(p => p.id === provState.defaultId);
    sub.textContent = provState.providers.length
      ? `共 ${provState.providers.length} 个 Provider · 默认：${def ? def.name : "未设置"}`
      : "尚未添加任何 Provider —— 添加一个本地模型后才能使用提示词增强";
  } catch (e) {
    sub.textContent = "读取失败：" + e.message;
    $("#provList").innerHTML =
      `<div class="prov-empty error">无法读取 Provider 配置：${esc(e.message)}</div>`;
  }
}

function renderProviders() {
  const list = $("#provList");
  if (!provState.providers.length) {
    list.innerHTML = `<div class="prov-empty">还没有配置 Provider。
      点击右上角「＋ 添加 Provider」添加本机的 OpenAI 兼容 API / Ollama / llama.cpp / ComfyUI 节点。</div>`;
  } else {
    list.innerHTML = provState.providers.map(p => {
      const isDefault = p.id === provState.defaultId;
      const isMmDefault = p.id === provState.defaultMmId;
      const badges = [];
      if (isDefault) badges.push(`<span class="prov-badge default">默认</span>`);
      if (isMmDefault) badges.push(`<span class="prov-badge mm">默认多模态</span>`);
      if (p.multimodal) badges.push(`<span class="prov-badge">多模态</span>`);
      if (!p.enabled) badges.push(`<span class="prov-badge off">已停用</span>`);
      return `
      <div class="prov-item${p.enabled ? "" : " disabled"}">
        <div class="prov-item-main">
          <div class="prov-item-head">
            <b>${esc(p.name)}</b>
            ${badges.join("")}
          </div>
          <div class="prov-item-meta">
            <span class="prov-type">${esc(p.type_label || PROVIDER_TYPE_LABELS[p.type] || p.type)}</span>
            <code>${esc(p.base_url)}</code>
            <code>${esc(p.model)}</code>
            ${p.has_api_key ? `<span class="prov-key">Key ${esc(p.api_key_masked)}</span>` : ""}
          </div>
        </div>
        <div class="prov-item-actions">
          <button data-act="test" data-id="${esc(p.id)}">测试</button>
          <button data-act="default" data-id="${esc(p.id)}">设为默认</button>
          <button data-act="mmdefault" data-id="${esc(p.id)}">设为默认多模态</button>
          <button data-act="edit" data-id="${esc(p.id)}">编辑</button>
          <button data-act="del" data-id="${esc(p.id)}" class="danger">删除</button>
        </div>
        <div class="prov-test-result" id="test_${esc(p.id)}"></div>
      </div>`;
    }).join("");
  }

  // 默认项概览
  const def = provState.providers.find(p => p.id === provState.defaultId);
  const mm = provState.providers.find(p => p.id === provState.defaultMmId);
  $("#provDefaults").innerHTML = `
    <div class="prov-default-item">
      <span>默认 Provider</span>
      <b>${def ? esc(def.name) : "未设置"}</b>
    </div>
    <div class="prov-default-item">
      <span>默认多模态模型</span>
      <b>${mm ? esc(mm.name) : "未设置"}</b>
    </div>`;
}

// Provider 列表按钮事件（事件委托）
$("#provList").addEventListener("click", async e => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.dataset.id;
  const act = btn.dataset.act;
  const p = provState.providers.find(x => x.id === id);
  if (!p) return;

  if (act === "edit") { openProvModal(p); return; }

  if (act === "del") {
    if (!confirm(`删除 Provider「${p.name}」？该操作不可撤销。`)) return;
    try {
      await apiJSON("DELETE", `/api/providers/${id}`);
      await loadProviders();
    } catch (err) { alert("删除失败：" + err.message); }
    return;
  }

  if (act === "default" || act === "mmdefault") {
    try {
      await apiJSON("POST", `/api/providers/${id}/default`,
        { multimodal: act === "mmdefault" });
      await loadProviders();
    } catch (err) { alert("设置失败：" + err.message); }
    return;
  }

  if (act === "test") {
    const box = $(`#test_${CSS.escape(id)}`);
    box.innerHTML = `<span class="prov-testing">检测中…</span>`;
    try {
      const r = await apiJSON("POST", `/api/providers/${id}/test`);
      box.innerHTML = r.ok
        ? `<span class="ok">✓ ${esc(r.detail)}${r.models && r.models.length ? "：" + esc(r.models.slice(0, 5).join(", ")) : ""}</span>`
        : `<span class="err">✗ ${esc(r.detail)}</span>`;
    } catch (err) {
      box.innerHTML = `<span class="err">✗ 检测失败：${esc(err.message)}</span>`;
    }
  }
});

// ------------------------------------------------------------------ Provider 编辑弹窗
function openProvModal(p) {
  provState.editingId = p ? p.id : null;
  $("#provModalTitle").textContent = p ? "编辑 Provider" : "添加 Provider";
  $("#provName").value = p ? p.name : "";
  $("#provType").value = p ? p.type : "openai";
  $("#provUrl").value = p ? p.base_url : "";
  $("#provLlmUrl").value = p ? (p.llm_base_url || "") : "";
  $("#provModel").value = p ? p.model : "";
  $("#provKey").value = "";
  $("#provKey").placeholder = p && p.has_api_key
    ? `已保存（${p.api_key_masked}），留空表示不修改` : "留空表示无需鉴权";
  $("#provKeyHint").textContent = p && p.has_api_key
    ? "为保护密钥，列表不显示明文。留空即保持原密钥不变。" : "";
  $("#provMultimodal").checked = p ? p.multimodal : false;
  $("#provEnabled").checked = p ? p.enabled : true;
  $("#provFormErr").hidden = true;
  toggleLlmUrlField();
  $("#provModal").hidden = false;
  setTimeout(() => $("#provName").focus(), 30);
}

// ComfyUI 类型才需要单独填「LLM 服务地址」
function toggleLlmUrlField() {
  const isComfy = $("#provType").value === "comfyui";
  $("#provLlmUrlField").style.display = isComfy ? "" : "none";
}
$("#provType").addEventListener("change", toggleLlmUrlField);

function closeProvModal() {
  $("#provModal").hidden = true;
  $("#provFormErr").hidden = true;
}

$("#provAddBtn").addEventListener("click", () => openProvModal(null));
$("#provModalClose").addEventListener("click", closeProvModal);
$("#provCancel").addEventListener("click", closeProvModal);

$("#provSave").addEventListener("click", async () => {
  const errBox = $("#provFormErr");
  const payload = {
    name: $("#provName").value.trim(),
    type: $("#provType").value,
    base_url: $("#provUrl").value.trim(),
    llm_base_url: $("#provLlmUrl").value.trim(),
    model: $("#provModel").value.trim(),
    api_key: $("#provKey").value.trim(),
    multimodal: $("#provMultimodal").checked,
    enabled: $("#provEnabled").checked,
  };
  errBox.hidden = true;
  // 前端先做一遍必填校验，后端还会再校验一次
  const missing = [];
  if (!payload.name) missing.push("显示名");
  if (!payload.base_url) missing.push("Base URL");
  if (!payload.model) missing.push("模型名");
  if (!payload.base_url || !/^https?:\/\//i.test(payload.base_url))
    missing.push("Base URL 需以 http:// 或 https:// 开头");
  if (missing.length) {
    errBox.textContent = "请填写：" + missing.join("、");
    errBox.hidden = false;
    return;
  }
  const btn = $("#provSave");
  btn.disabled = true;
  btn.textContent = "保存中…";
  try {
    if (provState.editingId) {
      await apiJSON("PUT", `/api/providers/${provState.editingId}`, payload);
    } else {
      await apiJSON("POST", "/api/providers", payload);
    }
    closeProvModal();
    await loadProviders();
  } catch (e) {
    errBox.textContent = e.message;
    errBox.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = "保存";
  }
});

$("#provRefresh").addEventListener("click", () => { loadProviders(); loadAgentConfig(); });

// ------------------------------------------------------------------ 增强 Agent 配置（模板 + 参数）
async function loadAgentConfig() {
  const box = $("#provAgent");
  try {
    const d = await apiJSON("GET", "/api/enhancer");
    provState.agent = {
      specs: d.specs || [],
      params: d.params || {},
      lengthPresets: d.length_presets || {},
      samplingPresets: d.sampling_presets || {},
      skillDir: d.skill_dir || "",
    };
    // 服务端下发的过滤规则（通用机制，新增模式/模型只改服务端）
    provState.modeFilters = d.mode_filters || {};
    provState.modeDefaults = d.mode_defaults || {};
    provState.modeMedia = d.mode_media || {};
    provState.modelFamilies = d.model_families || {};
    // 默认选官方 PE 档（image）
    if (!provState.specId || !provState.agent.specs.find(s => s.id === provState.specId)) {
      provState.specId = "image";
    }
    // 当前上下文下若模板不适用则回落 —— 保证下拉显示值与实际生效值一致
    alignSpecWithContext(enhanceCurrentMode(), enhanceCurrentModelKey());
    renderAgentConfig();
    renderEnhanceSpecSelect();
  } catch (e) {
    box.innerHTML = `<div class="prov-empty error">读取增强 Agent 配置失败：${esc(e.message)}</div>`;
  }
}

function renderAgentConfig() {
  const box = $("#provAgent");
  const specs = provState.agent.specs || [];
  const params = provState.agent.params || {};
  const presets = provState.agent.lengthPresets || {};
  const sampling = provState.agent.samplingPresets || {};
  const spec = specs.find(s => s.id === provState.specId) || specs[0];
  if (!spec) { box.innerHTML = `<div class="prov-empty">没有可用的增强模板</div>`; return; }
  provState.specId = spec.id;

  const lengthOpts = Object.entries(presets).map(([k, v]) =>
    `<option value="${esc(k)}"${params.length === k ? " selected" : ""}>${esc(v.label)}（${esc(v.hint || "")}）</option>`
  ).join("");

  const samplingOpts = Object.entries(sampling).map(([k, v]) =>
    `<option value="${esc(k)}"${params.sampling === k ? " selected" : ""}>${esc(v.label)}（${esc(v.hint || "")}）</option>`
  ).join("");

  // 已自定义的模板优先显示自定义正文，否则显示内置模板正文（都可编辑）
  const sysText = spec.customized
    ? (spec.custom_system_prompt || "")
    : (spec.builtin_system_prompt || "");
  const userText = spec.customized
    ? (spec.custom_user_template || "")
    : (spec.builtin_user_template || "");

  box.innerHTML = `
    <div class="agent-row">
      <label class="agent-field">
        <span>模板</span>
        <select id="agentSpec">${specs.map(s =>
          `<option value="${esc(s.id)}"${s.id === spec.id ? " selected" : ""}>${esc(s.label)}${s.customized ? "（已自定义）" : ""}</option>`).join("")}</select>
      </label>
      <label class="agent-field">
        <span>长度</span>
        <select id="agentLength">${lengthOpts}</select>
      </label>
      <label class="agent-field">
        <span>采样</span>
        <select id="agentSampling">${samplingOpts}</select>
      </label>
      <button class="agent-save" id="agentSave">保存模板与参数</button>
    </div>
    <div class="agent-dims">${esc(spec.source || "")} · 覆盖维度：${(spec.dimensions || []).map(d =>
      `<span class="agent-dim">${esc(d)}</span>`).join("")}</div>
    <label class="agent-field block">
      <span>System 提示词模板（可编辑）</span>
      <textarea id="agentSys" rows="10">${esc(sysText)}</textarea>
    </label>
    <label class="agent-field block">
      <span>User 模板（必须包含 {prompt} 或 {original}）</span>
      <textarea id="agentUser" rows="3">${esc(userText)}</textarea>
    </label>
    <div class="agent-hint">硬约束由服务端校验器强制：禁止质量词（masterpiece/8k/best quality）、禁止权重语法（(word:1.2)）、禁止 thinking 残留。</div>`;

  $("#agentSpec").addEventListener("change", e => {
    provState.specId = e.target.value;
    renderAgentConfig();
  });
  $("#agentSave").addEventListener("click", saveAgentConfig);
}

async function saveAgentConfig() {
  const btn = $("#agentSave");
  const sysVal = $("#agentSys").value;
  const userVal = $("#agentUser").value;
  if (userVal.trim() && !userVal.includes("{prompt}") && !userVal.includes("{original}")) {
    alert("User 模板必须包含 {prompt} 或 {original} 占位符");
    return;
  }
  btn.disabled = true;
  btn.textContent = "保存中…";
  try {
    await apiJSON("PUT", "/api/enhancer", {
      specs: {
        [provState.specId]: {
          system_prompt: sysVal,
          user_template: userVal,
        },
      },
      params: {
        length: $("#agentLength").value,
        sampling: $("#agentSampling").value,
      },
    });
    btn.textContent = "已保存 ✓";
    await loadAgentConfig();
  } catch (e) {
    alert("保存失败：" + e.message);
    btn.textContent = "保存模板与参数";
  } finally {
    btn.disabled = false;
    setTimeout(() => { if (btn) btn.textContent = "保存模板与参数"; }, 1600);
  }
}

// 用户重新编辑提示词时清掉上一次的错误提示
$("#prompt").addEventListener("input", () => {
  const hint = $("#enhanceHint");
  if (hint.classList.contains("error")) {
    hint.textContent = "用本地模型扩写提示词";
    hint.className = "enhance-hint";
  }
});

// ------------------------------------------------------------------ 提示词增强（图像页）
function setEnhanceHint(text, kind) {
  const hint = $("#enhanceHint");
  hint.textContent = text;
  hint.className = "enhance-hint" + (kind ? " " + kind : "");
}

// 图像页的模板下拉 —— 与「本地模型配置」页共用同一份 provState.specId
// 候选项按当前上下文（模式 + 模型）过滤：
//   图像模式只显示图像模板，视频模式只显示视频模板，音频模式只显示音频模板
function renderEnhanceSpecSelect() {
  const sel = $("#enhanceSpec");
  if (!sel) return;
  const mode = enhanceCurrentMode();
  const modelKey = enhanceCurrentModelKey();
  const specs = enhanceSpecsForContext(mode, modelKey);
  if (!specs.length) { sel.innerHTML = ""; sel.style.display = "none"; return; }
  sel.style.display = "";
  sel.innerHTML = specs.map(s =>
    `<option value="${esc(s.id)}"${s.id === provState.specId ? " selected" : ""}>${esc(s.label)}</option>`
  ).join("");
  // 下拉显示值必须与实际生效值一致：不在候选里的 id 一律回落到上下文默认
  const ids = specs.map(s => s.id);
  if (!ids.includes(provState.specId)) {
    const fallback = enhanceDefaultSpecForContext(mode, modelKey, specs);
    if (fallback) provState.specId = fallback;
  }
  sel.value = provState.specId;
  sel.setAttribute("data-mode", mode || "");
  sel.setAttribute("data-model", modelKey || "");
}

// 模式切换 / 模型切换后调用：重新过滤下拉 + 回落不适用模板
function refreshEnhanceSpecForMode() {
  const mode = enhanceCurrentMode();
  const modelKey = enhanceCurrentModelKey();
  const before = provState.specId;
  const { changed, specId } = alignSpecWithContext(mode, modelKey);
  renderEnhanceSpecSelect();
  if (changed) {
    const spec = (provState.agent.specs || []).find(s => s.id === specId);
    setEnhanceHint(
      `已切换到「${mode || "当前"}」模式，模板自动回落到「${spec ? spec.label : specId}」`, "ok");
  }
  return before !== provState.specId;
}

$("#enhanceSpec").addEventListener("change", e => {
  provState.specId = e.target.value;
  const mode = enhanceCurrentMode();
  provState.specIdByMode[mode] = e.target.value;
  setEnhanceHint("已切换模板：" + e.target.selectedOptions[0].textContent, "ok");
});

// 首次进入时拉一次 Agent 配置，把模板清单喂给下拉框
loadAgentConfig().then(renderEnhanceSpecSelect);

$("#enhanceBtn").addEventListener("click", async () => {
  const btn = $("#enhanceBtn");
  const prompt = $("#prompt").value.trim();
  if (!prompt) {
    setEnhanceHint("请先输入要增强的提示词", "error");
    $("#prompt").focus();
    return;
  }
  btn.disabled = true;
  btn.classList.add("busy");
  promptEnhance.busy = true;
  updateActionButtons();
  setEnhanceHint("正在增强…", "busy");
  try {
    const d = await apiJSON("POST", "/api/enhance", {
      prompt,
      mode: "replace",
      spec_id: provState.specId || "image",
      // 当前上下文：后端用它做模板适用性兜底，保证执行模板与下拉显示一致
      mode_ctx: enhanceCurrentMode(),
      model_key: enhanceCurrentModelKey(),
    });
    provState.lastEnhance = d;
    // 服务端可能因模式回落换了模板 —— 同步下拉显示，避免「显示 A 实际用 B」
    if (d.spec_id && d.spec_id !== provState.specId) {
      provState.specId = d.spec_id;
      renderEnhanceSpecSelect();
    }
    $("#enhanceResult").value = d.enhanced || "";
    $("#enhanceMeta").innerHTML = buildEnhanceMeta(d);
    $("#enhanceModal").hidden = false;
    // 记录版本谱系：原始(0) -> 一次增强(1)，供「版本历史」查看与再增强
    if (d.enhanced) {
      pushPromptVersion(0, prompt, "原始提示词");
      pushPromptVersion(1, d.enhanced, `模板：${d.spec_id || "image"}`, d.spec_id);
    }
    setEnhanceHint(`已用「${d.provider}」扩写，${d.enhanced ? d.enhanced.length : 0} 字`, "ok");
  } catch (e) {
    // 失败必须可见 —— 本地模型不可达时不静默
    const detail = e.payload && e.payload.error ? e.payload.error : e.message;
    const who = e.payload && e.payload.provider ? `（${e.payload.provider}）` : "";
    setEnhanceHint(`增强失败：${detail}`, "error");
    alert(`提示词增强失败${who}\n\n${detail}\n\n请检查「本地模型配置」里的 Provider 是否可用。`);
  } finally {
    btn.disabled = false;
    btn.classList.remove("busy");
    promptEnhance.busy = false;
    updateActionButtons();
  }
});

// ---------- 二次增强（物体逐个枚举 + 逐个细化） ----------
// 作用对象由 promptVersions.refineTarget 决定：0=原始提示词 / 1=一次增强后的提示词
function refineSourcePrompt() {
  const target = promptVersions.refineTarget;
  if (target === 1) {
    // 取最近一次「一次增强」版本
    const l1 = promptVersions.versions.filter(v => v.level === 1);
    if (l1.length) return l1[l1.length - 1].text;
  }
  return $("#prompt").value.trim();
}

$("#refineBtn")?.addEventListener("click", async () => {
  const btn = $("#refineBtn");
  const text = $("#refineBtnText");
  const source = refineSourcePrompt();
  if (!source) {
    setEnhanceHint("请先输入要细化的提示词", "error");
    $("#prompt").focus();
    return;
  }
  const targetLevel = promptVersions.refineTarget === 1 ? 1 : 0;
  btn.disabled = true;
  text.textContent = "正在细化…";
  promptVersions.busy = true;
  updateActionButtons();
  setEnhanceHint("正在逐个物体细化…", "busy");
  try {
    const d = await apiJSON("POST", "/api/enhance", {
      prompt: source,
      mode: "replace",
      // 二次增强固定走「物体细化」模板；服务端仍会按 media/families 过滤校验
      spec_id: "image-refine",
      mode_ctx: enhanceCurrentMode(),
      model_key: enhanceCurrentModelKey(),
    });
    if (d.spec_id) { provState.specId = d.spec_id; renderEnhanceSpecSelect(); }
    const refined = d.enhanced || "";
    if (!refined) throw new Error("本地模型返回了空结果");
    // 回填到输入框
    $("#prompt").value = refined;
    // 记录为第 2 级版本；保留原始 / 一次增强版本
    pushPromptVersion(2, refined, `模板：${d.spec_id || "image-refine"}`, d.spec_id);
    markPromptEnhanced();
    updatePromptBadge();
    renderPromptVersions();
    // 来源版本也要留在谱系里（供「版本历史」查看）
    pushPromptVersion(targetLevel, source, targetLevel === 1 ? "一次增强（本次二次增强的输入）" : "原始提示词（本次二次增强的输入）");
    $("#enhanceResult").value = refined;
    $("#enhanceMeta").innerHTML = buildEnhanceMeta(d);
    $("#enhanceModal").hidden = false;
    setEnhanceHint(`二次增强完成（${d.provider}）：逐个物体细化 ${refined.length} 字`, "ok");
  } catch (e) {
    const detail = e.payload && e.payload.error ? e.payload.error : e.message;
    const who = e.payload && e.payload.provider ? `（${e.payload.provider}）` : "";
    setEnhanceHint(`二次增强失败：${detail}`, "error");
    alert(`二次增强失败${who}\n\n${detail}\n\n请检查「本地模型配置」里的 Provider 是否可用。`);
  } finally {
    promptVersions.busy = false;
    btn.disabled = false;
    text.textContent = "二次增强";
    updateActionButtons();
  }
});

// ---------- 版本历史弹窗 ----------
$("#promptVersionBtn")?.addEventListener("click", openPromptVersionModal);
$("#promptVersionClose")?.addEventListener("click", closePromptVersionModal);
$("#promptVersionDone")?.addEventListener("click", closePromptVersionModal);
$("#promptVersionModal")?.addEventListener("click", e => {
  if (e.target.id === "promptVersionModal") closePromptVersionModal();
});

// 结果弹窗的元信息：模型 / 模板 / 采样参数 / 校验结果
function buildEnhanceMeta(d) {
  const p = d.params || {};
  const v = d.validation || {};
  const bits = [
    `模型：<b>${esc(d.provider || "—")}</b>`,
    `模板：${esc(d.spec_id || "—")}`,
    `采样：${esc(p.sampling || "—")} / ${esc(p.temperature ?? "—")} / top_p ${esc(p.top_p ?? "—")}`,
    `max_new_tokens：${esc(p.max_tokens ?? "—")}`,
    `原文 ${d.original ? d.original.length : 0} 字 → 扩写 ${d.enhanced ? d.enhanced.length : 0} 字`,
  ];
  let html = bits.join(" · ");
  if (v.warnings && v.warnings.length) {
    html += `<div class="enhance-warn">⚠ ${v.warnings.map(esc).join("；")}</div>`;
  }
  if (v.stats && v.stats.words) {
    html += `<div class="enhance-warn">词数约 ${v.stats.words}</div>`;
  }
  if (d.elements && d.elements.length) {
    html += `<div class="enhance-elems">bbox 元素：${d.elements.map(e =>
      esc(e.id) + " " + esc(JSON.stringify(e.bbox))).join(" · ")}</div>`;
  }
  return html;
}

$("#enhanceModalClose").addEventListener("click", () => { $("#enhanceModal").hidden = true; });
$("#enhanceCancel").addEventListener("click", () => { $("#enhanceModal").hidden = true; });

$("#enhanceReplace").addEventListener("click", () => {
  const text = $("#enhanceResult").value.trim();
  if (!text) return;
  $("#prompt").value = text;
  $("#enhanceModal").hidden = true;
  // 回填后的内容即「已增强」版本：标记它，让生成时不再弹未增强确认
  markPromptEnhanced();
  setEnhanceHint("已替换提示词框内容（已标记为已增强）", "ok");
});

$("#enhanceAppend").addEventListener("click", () => {
  const text = $("#enhanceResult").value.trim();
  if (!text) return;
  const original = $("#prompt").value.trim();
  $("#prompt").value = original ? `${original}\n\n${text}` : text;
  $("#enhanceModal").hidden = true;
  markPromptEnhanced();
  setEnhanceHint("已追加到原提示词（原文保留，已标记为已增强）", "ok");
});
