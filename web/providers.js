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
  // 最近一次增强结果的**种类**（"qwen_pe" = 官方 PE 扩写）。
  // 写回提示词框时据此决定要不要关掉出图时的二次官方 PE 扩写。
  lastEnhanceKind: "",
  // 模板过滤上下文（服务端下发规则，前端只负责按当前上下文取用）
  modeFilters: {},        // { t2i: ["image","image-scalist",...], t2v: ["video"], ... }
  modeDefaults: {},       // { t2i: "image", t2v: "video", ... }
  modelFamilies: {},      // { z_image_turbo_bf16: "z_image", lora: "minimax_h3", ... }
  modeMedia: {},          // { t2i: "image", t2v: "video", ... } 模式 -> 媒体类型
  specIdByMode: {},       // { t2i: "image", ... } 各模式下用户最后选中的模板
  // 「二次增强」按媒体类型用不同模板（服务端 refine_specs / refine_spec_by_media 下发）
  refineSpecs: {},        // { t2i: "image-refine", t2a: "music-refine", ... }
  refineSpecByMedia: {},  // { image: "image-refine", audio: "music-refine", ... }
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
// MOSS-Image（t2i_moss）同理用 state.imgMoss.model ——
// 漏了它会掉到 state.model（视频的 MiniMax-H3），
// 于是 MOSS 模式下模板按视频模型族过滤，列出的是错的模板。
function enhanceCurrentModelKey() {
  if (typeof state === "undefined" || !state) return "";
  const mode = state.mode || "";
  if (mode === "t2i") return (state.img && state.img.model) || "";
  if (mode === "t2i_qwen" || mode === "i2i_qwen") {
    return (state.imgQwen && state.imgQwen.model) || "";
  }
  if (mode === "t2i_moss") {
    return (state.imgMoss && state.imgMoss.model) || "";
  }
  // 音乐（YuE2）：模型是 checkpoint key，取自音乐面板自己的状态。
  // 漏了它会掉到 state.model（视频的 MiniMax-H3），
  // 于是模型族过滤按 minimax_h3 走，YuE2 专用模板会被整条滤掉。
  if (mode === "t2a" || mode === "a2a") {
    const st = window.MusicUI && typeof window.MusicUI.getState === "function"
      ? window.MusicUI.getState() : null;
    return (st && st.checkpoint) || "yue2_3b_bf16";
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
      maxTokensBounds: d.max_tokens_bounds || {},
    };
    // 服务端下发的过滤规则（通用机制，新增模式/模型只改服务端）
    provState.modeFilters = d.mode_filters || {};
    provState.modeDefaults = d.mode_defaults || {};
    provState.modeMedia = d.mode_media || {};
    provState.modelFamilies = d.model_families || {};
    // 「二次增强」的模板按媒体类型选 —— 服务端下发，前端不硬编码
    provState.refineSpecs = d.refine_specs || {};
    provState.refineSpecByMedia = d.refine_spec_by_media || {};
    // 默认选官方 PE 档（image）
    if (!provState.specId || !provState.agent.specs.find(s => s.id === provState.specId)) {
      provState.specId = "image";
    }
    // 当前上下文下若模板不适用则回落 —— 保证下拉显示值与实际生效值一致
    alignSpecWithContext(enhanceCurrentMode(), enhanceCurrentModelKey());
    renderAgentConfig();
    // 增强模板清单到齐了 -> 「增强」多选列表里官方 PE 那一项的
    // system prompt 默认值（官方原文）才能取到。
    // 这一步同时把推荐参数套用到 state（选中即自动套用）。
    if (window.enhanceMethods && typeof window.enhanceMethods.renderMenu === "function") {
      window.enhanceMethods.renderMenu();
      window.enhanceMethods.ensureAvailable();
      // 模板清单到齐后要重算「当前增强方式」回显 ——
      // 否则它只能显示裸的模板 id（清单没到时查不到显示名）。
      if (typeof window.enhanceMethods.refreshButton === "function") {
        window.enhanceMethods.refreshButton();
      }
    }
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

  // 最大输出 token 上限 —— 可配置项，不再是硬编码 512/1024。
  // 边界由服务端下发（/api/enhancer 的 max_tokens_bounds），前端不另写常量。
  const bounds = provState.agent.maxTokensBounds || {};
  const ceilMin = Number(bounds.min ?? 256);
  const ceilMax = Number(bounds.max ?? 65536);
  const ceilVal = Number(params.max_tokens_ceiling ?? bounds.default ?? 32768);

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
      <label class="agent-field">
        <span>最大输出 token 上限</span>
        <input type="number" id="agentMaxTokens" min="${ceilMin}" max="${ceilMax}"
               step="256" value="${esc(ceilVal)}">
      </label>
      <button class="agent-save" id="agentSave">保存模板与参数</button>
    </div>
    <div class="agent-hint">
      最大输出 token 上限：${ceilMin} ~ ${ceilMax}，默认 ${esc(bounds.default ?? 32768)}。
      实际预算按**提示词长度自适应**（普通增强约 1.6 倍、二次增强约 6 倍），
      这个上限只是天花板 —— 调太大不会让模型多写，只会在真的被截断时多等一次。
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
  // 最大输出 token 上限：先在本地夹一次，后端还会再夹一次（两道闸）
  const bounds = provState.agent.maxTokensBounds || {};
  const ceilMin = Number(bounds.min ?? 256);
  const ceilMax = Number(bounds.max ?? 65536);
  let ceil = Number($("#agentMaxTokens").value);
  if (!Number.isFinite(ceil)) ceil = Number(bounds.default ?? 32768);
  ceil = Math.max(ceilMin, Math.min(ceilMax, Math.round(ceil)));
  $("#agentMaxTokens").value = ceil;

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
        max_tokens_ceiling: ceil,
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

// 【模板选择已并入「增强」下拉】原来这里有一个独立的 #enhanceSpec「增强模板」
// 下拉，与「增强方式」下拉语义重叠（两者列的是同一组东西），让人困惑。
// 现在模板选择**长在对应的增强方式下面**（见 web/enhance-menu.js 的
// renderSpecPicker）—— 每个面板仍然只有一个增强入口。
//
// 保留下来的只有两件事：
//   1. alignSpecWithContext —— 模式/模型切换后回落到适用模板
//      （「本地模型配置」页的模板编辑框仍用它决定当前编辑哪一条）
//   2. enhanceSpecsForContext —— 当前上下文可用的模板清单（增强下拉要用）

// 模式切换 / 模型切换后调用：重新对齐当前上下文适用的模板
function refreshEnhanceSpecForMode() {
  const mode = enhanceCurrentMode();
  const modelKey = enhanceCurrentModelKey();
  const before = provState.specId;
  const { changed, specId } = alignSpecWithContext(mode, modelKey);
  if (changed) {
    const spec = (provState.agent.specs || []).find(s => s.id === specId);
    setEnhanceHint(
      `已切换到「${mode || "当前"}」模式，模板自动回落到「${spec ? spec.label : specId}」`, "ok");
  }
  return before !== provState.specId;
}

// 首次进入时拉一次 Agent 配置（模板清单供「增强」下拉里的模板选择器使用）
loadAgentConfig();

// 「增强」主按钮与下拉菜单的绑定、以及三种增强方式的路由，统一放在
// web/enhance-menu.js 里（那里有一份增强方式注册表，是唯一真相来源）。
// 这里只保留「模板下拉」与「结果弹窗的通用处理」——
// 前者决定普通增强用哪个模板，后者决定各方式的回填语义一致。

// 二次增强的输入来源：由 promptVersions.refineTarget 决定
// 0=原始提示词 / 1=一次增强后的提示词
// （enhance-menu.js 的二次增强路由会调它）
function refineSourcePrompt() {
  const target = promptVersions.refineTarget;
  if (target === 1) {
    // 取最近一次「一次增强」版本
    const l1 = promptVersions.versions.filter(v => v.level === 1);
    if (l1.length) return l1[l1.length - 1].text;
  }
  return $("#prompt").value.trim();
}

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
  // 要素保留率：原文里的画面要素有多少被扩写结果保留下来
  //
  // 【2026-11-10 修复】cov.applicable=false 时**不显示**这个数字 ——
  // 那是「原文与产出书写系统不同」（如 moss-image 模板官方口径就是
  // 「默认用英文描述画面」，中文原文 vs 英文产出），逐字比对必然 0%，
  // 显示 0% 会让用户误以为「要素全丢了」。
  const cov = d.coverage;
  if (cov && cov.total && cov.applicable !== false) {
    const pct = Math.round((cov.ratio ?? 0) * 100);
    html += `<div class="enhance-warn">原文要素保留率 ${pct}%（${cov.total - (cov.missing_count || 0)}/${cov.total} 项）</div>`;
    if (cov.missing && cov.missing.length) {
      html += `<div class="enhance-warn">未逐字命中的原文片段：${cov.missing.map(esc).join("、")}</div>`;
    }
  }
  if (d.finish_reason) {
    html += `<div class="enhance-warn">finish_reason：${esc(d.finish_reason)}</div>`;
  }
  if (d.elements && d.elements.length) {
    html += `<div class="enhance-elems">bbox 元素：${d.elements.map(e =>
      esc(e.id) + " " + esc(JSON.stringify(e.bbox))).join(" · ")}</div>`;
  }
  return html;
}

$("#enhanceModalClose").addEventListener("click", () => { $("#enhanceModal").hidden = true; });
$("#enhanceCancel").addEventListener("click", () => { $("#enhanceModal").hidden = true; });

// 回填后如果这次结果是**官方 PE 扩写**（Qwen-Image 2.1），
// 就要关掉出图时的二次扩写 ——
//   提示词框里已经是官方 PE 的结果了，再扩一遍会：
//     1) 白花一次 9B 推理（约 30~60 秒）；
//     2) 把用户基于官方 PE 结果做的后续编辑冲掉。
// 标记由 enhance-menu.js 在拿到官方 PE 结果时打上。
function applyQwenPeWritebackFlag() {
  if (provState.lastEnhanceKind !== "qwen_pe") return;
  if (typeof state === "undefined" || !state || !state.imgQwen) return;
  state.imgQwen.usePe = false;
  provState.lastEnhanceKind = "";
}

// ------------------------------------------------------------------
// 增强结果写回：按**能力契约**解析目标，而不是硬编码 DOM id
// ------------------------------------------------------------------
//
// 【为什么要改成契约】审查 P0-1：
//   enh-unify 的旧实现硬编码 `$("#musicStyle")`（音乐「风格描述」框）。
//   yue2-flow 把那个 textarea 改名成 #musicPrompt（音乐描述），
//   并把「结构化风格」拆成 12 个独立字段 —— 合并后
//   `document.querySelector("#musicStyle")` 返回 **null**，
//   写回会静默抛 `TypeError: Cannot read properties of null (reading 'value')`。
//
// 【契约语义】
//   kind = "text"       -> 产物是一段文本，写回一个输入框
//   kind = "structured" -> 产物是结构化对象（音乐：12 风格字段 + 歌词）
//   目标缺失时**不抛异常**，而是返回可读原因，由调用方提示用户。
//
// 音乐侧的能力契约由 web/music-enhance.js 注册（结构化路线），
// 图像/视频侧就是提示词框（文本路线）。
// 两条路线互不依赖对方的 DOM id —— 合并时不需要再对齐 id。
// 文本路线的写回目标：**候选列表**（先新后旧）。
//   图像/视频 = 提示词框 #prompt
//   音乐      = 音乐描述框 —— #musicPrompt（yue2-flow 命名）
//               与 #musicStyle（enh-unify 命名）是**同一语义的两个名字**，
//               任一存在即可用；都不存在时给可读提示（不抛异常）。
/**
 * 文本路线的写回契约（兜底）。
 *
 * 【按能力契约 · 审查 P0-1】
 * 首选**统一注册表**声明的契约（window.enhanceMethods.getWriteContract）——
 * 增强方式自己声明自己的写回目标与形态，调用方不再硬编码 id。
 * 注册表还没就绪时，才退回下面这份静态候选表。
 */
const ENHANCE_WRITE_TARGETS = {
  text: {
    kind: "text",
    target: "#prompt",
    targets: ["#prompt", "#musicPrompt", "#musicStyle"],
    describe: () => "本面板找不到可写回的输入框",
  },
};

/**
 * 当前上下文的写回契约。
 *
 * 【为什么先问注册表】增强方式自己声明写回契约（text / structured），
 * 所以「当前方式是哪一项」决定契约形态：
 *   - 音乐面板的结构化增强 -> 结构化契约（风格字段 + 歌词）
 *   - 其它（文本扩写类）    -> 文本契约（当前面板的文本输入框）
 *
 * 【2026-11-10 修复 · 真机回归】曾经无条件用音乐的结构化契约，
 * 导致**图像/视频面板**的写回也被解析成 kind:"structured"：
 *   1. writeBackEnhanceResult() 命中 structured 分支直接 return false
 *      -> 「替换/追加」按钮**点了没反应**（静默失败）；
 *   2. 徽标不显示、版本等级恒为 0（内容压根没写回提示词框）。
 */
function enhanceWriteContract() {
  // 1) 优先用注册表按「当前增强方式」解析出的契约
  const unified = window.enhanceMethods;
  if (unified && typeof unified.getWriteContract === "function") {
    const c = unified.getWriteContract();
    if (c) return c;
  }
  // 2) 兜底：只有**音乐面板**才用结构化契约，其余面板一律走文本契约。
  //    判据用现有的 isMusicMode()（app.js 已定义，判的是 state.mode）。
  if (typeof isMusicMode === "function" && isMusicMode()) {
    const music = window.MusicEnhance && typeof window.MusicEnhance.getWriteContract === "function"
      ? window.MusicEnhance.getWriteContract() : null;
    if (music) return music;
  }
  return ENHANCE_WRITE_TARGETS.text;
}

/**
 * 解析写回目标。目标缺失时返回 { ok:false, reason } —— **不抛异常**。
 */
function resolveEnhanceWriteTarget() {
  const shared = window.EnhanceMenuShared;
  const contract = enhanceWriteContract();
  if (!shared || typeof shared.resolveWriteTarget !== "function") {
    return { ok: false, reason: "增强共用组件未就绪（enhance-menu-shared.js 未加载）" };
  }
  return shared.resolveWriteTarget(contract);
}

/**
 * 写回增强结果到当前契约声明的目标。
 * @returns {boolean} 是否写回成功
 */
function writeBackEnhanceResult(text, mode) {
  const target = resolveEnhanceWriteTarget();
  if (!target.ok) {
    setEnhanceHint("写回失败：" + target.reason, "error");
    return false;
  }
  if (target.kind === "structured") {
    // 结构化路线（音乐）：产物是「12 个风格字段 + 歌词」，不是一个文本框。
    // 这类增强**不走**「替换提示词框 / 追加」弹窗 —— 它在增强完成时
    // 直接落到结构化风格区与歌词区（由 music-enhance.js 的契约负责）。
    // 所以这里明确拒绝，而不是静默丢弃。
    setEnhanceHint("音乐的结构化增强直接写入结构化风格区与歌词区，不走文本写回", "error");
    return false;
  }
  const el = target.element;
  if (!el) {
    setEnhanceHint("写回失败：目标输入框不存在", "error");
    return false;
  }
  if (mode === "append") {
    const original = el.value.trim();
    el.value = original ? `${original}\n\n${text}` : text;
  } else {
    el.value = text;
  }
  return true;
}

$("#enhanceReplace").addEventListener("click", () => {
  const text = $("#enhanceResult").value.trim();
  if (!text) return;
  if (!writeBackEnhanceResult(text, "replace")) return;
  $("#enhanceModal").hidden = true;
  // 回填后的内容即「已增强」版本：标记它，让生成时不再弹未增强确认
  markPromptEnhanced();
  applyQwenPeWritebackFlag();
  setEnhanceHint("已替换提示词框内容（已标记为已增强）", "ok");
});

$("#enhanceAppend").addEventListener("click", () => {
  const text = $("#enhanceResult").value.trim();
  if (!text) return;
  if (!writeBackEnhanceResult(text, "append")) return;
  $("#enhanceModal").hidden = true;
  markPromptEnhanced();
  applyQwenPeWritebackFlag();
  setEnhanceHint("已追加到原提示词（原文保留，已标记为已增强）", "ok");
});
