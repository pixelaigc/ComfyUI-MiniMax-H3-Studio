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
};

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
    // 默认选官方 PE 档（image）
    if (!provState.specId || !provState.agent.specs.find(s => s.id === provState.specId)) {
      provState.specId = "image";
    }
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
function renderEnhanceSpecSelect() {
  const sel = $("#enhanceSpec");
  if (!sel) return;
  const specs = provState.agent.specs || [];
  if (!specs.length) { sel.innerHTML = ""; sel.style.display = "none"; return; }
  sel.style.display = "";
  sel.innerHTML = specs.map(s =>
    `<option value="${esc(s.id)}"${s.id === provState.specId ? " selected" : ""}>${esc(s.label)}</option>`
  ).join("");
  sel.value = provState.specId;
}

$("#enhanceSpec").addEventListener("change", e => {
  provState.specId = e.target.value;
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
    });
    provState.lastEnhance = d;
    $("#enhanceResult").value = d.enhanced || "";
    $("#enhanceMeta").innerHTML = buildEnhanceMeta(d);
    $("#enhanceModal").hidden = false;
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
