/* H3 Studio 前端逻辑 */
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

const MODE_INFO = {
  t2v: { name: "文生视频", kind: "video" },
  flf: { name: "首尾帧", kind: "video" },
  r2v: { name: "参考生成", kind: "video" },
  t2i: { name: "Z-IMAGE 文生图", kind: "image" },
  longfilm: { name: "长片", kind: "video" },
};

const state = {
  mode: "t2v",
  tab: "done",
  page: "create",
  res: "544p",
  model: "lora",
  lora: "ckpt850",
  loraStrength: 1.0,
  aspect: "16:9",
  duration: 5,
  steps: 6,
  donePage: 1,
  doneFilters: { keyword: "", media: "", aspect: "", duration: "" },
  stitchClips: [],
  files: { first: null, last: null, refs: [] },
  timer: null,
  genBusy: false,
  // 图像模式（Z-Image 文生图）专属状态
  img: {
    tier: "1mp",
    aspect: "16:9",
    width: null,
    height: null,
    model: "z_image_turbo_bf16",
    steps: 8,
    cfg: 1,
    shift: 3,
    sampler: "res_multistep",
    scheduler: "simple",
    negative: "",
  },
};

// 图像模式的档位 / 比例 / 模型清单，由 /api/image-options 下发（服务端与本机
// ComfyUI 的实际权重、官方推荐参数一致），前端不硬编码模型文件名。
const IMG_OPTIONS = { tiers: [], aspects: [], models: {}, sizeMultiple: 16, maxDirectMp: 3 };
const IMG_ASPECT_NUM = {
  "1:1": [1, 1], "2:3": [2, 3], "3:2": [3, 2], "3:4": [3, 4],
  "4:3": [4, 3], "9:16": [9, 16], "16:9": [16, 9], "21:9": [21, 9],
};

// ---------- 侧栏切换 ----------
$$(".nav-item[data-mode]").forEach(el => el.addEventListener("click", () => {
  $$(".nav-item").forEach(x => x.classList.remove("active"));
  el.classList.add("active");
  state.mode = el.dataset.mode;
  showCreate();
  renderMode();
}));

function renderMode() {
  const mode = state.mode;
  const info = MODE_INFO[mode];
  // 上传区
  const zone = $("#uploadZone"), row = $("#uploadRow");
  row.innerHTML = "";
  const mk = (key, label) => {
    const slot = document.createElement("div");
    slot.className = "up-slot";
    slot.innerHTML = `+<span class="tag">${label}</span>`;
    const inp = document.createElement("input");
    inp.type = "file"; inp.accept = "image/*";
    const fillSlot = (file, previewUrl) => {
      const url = previewUrl || URL.createObjectURL(file);
      slot.replaceChildren();
      const image = document.createElement("img");
      image.src = url;
      image.alt = label;
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = label;
      slot.append(image, tag);
      slot.classList.add("filled");
      state.files[key] = file;
    };
    inp.addEventListener("change", e => {
      const file = e.target.files[0];
      if (file) fillSlot(file);
    });
    slot.addEventListener("dragover", event => {
      if (!event.dataTransfer.types.includes("application/x-h3-image")) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
      slot.classList.add("drag-over");
    });
    slot.addEventListener("dragleave", () => slot.classList.remove("drag-over"));
    slot.addEventListener("drop", async event => {
      const raw = event.dataTransfer.getData("application/x-h3-image");
      if (!raw) return;
      event.preventDefault();
      slot.classList.remove("drag-over");
      slot.classList.add("loading");
      try {
        const source = JSON.parse(raw);
        const response = await fetch(source.url, {cache: "no-store"});
        if (!response.ok) throw new Error("图片读取失败");
        const blob = await response.blob();
        const file = new File([blob], source.name || "completed-image.png", {type: blob.type || "image/png"});
        fillSlot(file, source.url);
      } catch (error) {
        alert("无法引用这张图片：" + error.message);
      } finally {
        slot.classList.remove("loading");
      }
    });
    slot.addEventListener("click", () => inp.click());
    row.appendChild(slot);
  };
  if (mode === "flf") {
    $("#uploadTitle").textContent = "上传首帧与尾帧，或从已完成图片拖入（尾帧可省略）";
    zone.style.display = "block";
    mk("first", "首帧"); mk("last", "尾帧");
  } else if (mode === "r2v") {
    $("#uploadTitle").textContent = "上传参考图，或从已完成图片拖入（1~3 张）";
    zone.style.display = "block";
    mk("ref0", "参考 1"); mk("ref1", "参考 2"); mk("ref2", "参考 3");
  } else {
    zone.style.display = "none";
  }
  // 文生图隐藏时长 / 模型 / 步数
  const isVideo = info.kind === "video";
  $("#durField").style.display = isVideo ? "" : "none";
  // 图像页用自己的「分辨率档位 + 画面比例」联动控件，顶部快捷分辨率卡片不显示
  $("#resField").style.display = isVideo ? "" : "none";
  $("#modelField").style.display = isVideo ? "" : "none";
  $("#stepsField").style.display = isVideo ? "" : "none";
  // 图像模式用自己的一套比例/分辨率控件，隐藏视频模式的那套
  $("#aspectField").style.display = isVideo ? "" : "none";
  $("#imagePanel").style.display = isVideo ? "none" : "";
  if (!isVideo) renderImagePanel();
  updateSeedStrengthLayout();
  updateActionButtons();
  // 时长标签
  $("#durVal").textContent = state.duration + "s";
}

// ---------- 图像模式（Z-Image 文生图） ----------
// 档位 + 比例 -> 像素宽高。规则与后端 z_image_dimensions 一致：
// 档位值给到**短边**，横图给高度、竖图给宽度，宽高对齐到 8 的倍数。
function imgAutoSize(tier, aspect) {
  // 与后端 z_image_dimensions / ComfyUI ResolutionSelector 同一算法：
  //   total = megapixels * 1024^2; scale = sqrt(total / (wr*hr))
  //   w = round(wr*scale/mult)*mult;  h = round(hr*scale/mult)*mult
  // multiple 用 16（Z-Image 要求宽高是 16 的倍数）。
  const tierOpt = IMG_OPTIONS.tiers.find(t => t.key === tier);
  const megapixels = (tierOpt && tierOpt.megapixels) || 1.0;
  const mult = IMG_OPTIONS.sizeMultiple || 16;
  const [wr, hr] = IMG_ASPECT_NUM[aspect] || [16, 9];
  const total = megapixels * 1024 * 1024;
  const scale = Math.sqrt(total / (wr * hr));
  let w = Math.round(wr * scale / mult) * mult;
  let h = Math.round(hr * scale / mult) * mult;
  w = Math.max(mult, w);
  h = Math.max(mult, h);
  return [w, h];
}

function imgCurrentSize() {
  const s = state.img;
  if (s.width && s.height) return [s.width, s.height];
  return imgAutoSize(s.tier, s.aspect);
}

// 档位下拉：显示具体像素，例如 "1080P · 1920×1088 (2.0MP)"
function imgTierLabel(tier) {
  const [w, h] = imgAutoSize(tier.key, state.img.aspect);
  const mp = (w * h) / (1024 * 1024);
  const up = mp > (IMG_OPTIONS.maxDirectMp || 3);
  return `${tier.label} · ${w}×${h} (${mp.toFixed(1)}MP)${up ? " · 放大" : ""}`;
}

function renderImagePanel() {
  const s = state.img;
  const model = IMG_OPTIONS.models[s.model];

  // 分辨率档位
  const resSel = $("#imgResSelect");
  resSel.innerHTML = "";
  IMG_OPTIONS.tiers.forEach(tier => {
    const opt = document.createElement("option");
    opt.value = tier.key;
    opt.textContent = imgTierLabel(tier);
    opt.selected = tier.key === s.tier;
    resSel.appendChild(opt);
  });

  // 比例
  const chips = $("#imgAspectChips");
  chips.innerHTML = "";
  IMG_OPTIONS.aspects.forEach(aspect => {
    const b = document.createElement("button");
    b.type = "button";
    b.dataset.v = aspect.key;
    b.textContent = aspect.key;
    b.title = aspect.label;
    if (aspect.key === s.aspect) b.classList.add("active");
    b.addEventListener("click", () => {
      s.aspect = aspect.key;
      // 换比例后旧的自动尺寸作废，回到档位自动值
      s.width = null; s.height = null;
      renderImagePanel();
    });
    chips.appendChild(b);
  });

  // 模型
  const modelSel = $("#imgModelSelect");
  modelSel.innerHTML = "";
  Object.entries(IMG_OPTIONS.models).forEach(([key, m]) => {
    const opt = document.createElement("option");
    opt.value = key;
    opt.textContent = m.label;
    opt.selected = key === s.model;
    modelSel.appendChild(opt);
  });

  // 采样器 / 调度器：列表按模型版本区分（Turbo 不给 karras/exponential 调度器）
  const fillSelect = (sel, values, current) => {
    sel.innerHTML = "";
    (values || []).forEach(v => {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v;
      opt.selected = v === current;
      sel.appendChild(opt);
    });
    if (current && !(values || []).includes(current)) {
      // 当前值不在该版本的推荐列表里（比如换了模型），补上并保持选中
      const opt = document.createElement("option");
      opt.value = current;
      opt.textContent = current + "（自定义）";
      opt.selected = true;
      sel.appendChild(opt);
    }
  };
  fillSelect($("#imgSamplerSelect"), model && model.samplers, s.sampler);
  fillSelect($("#imgSchedulerSelect"), model && model.schedulers, s.scheduler);

  // 步数 / CFG / shift
  $("#imgSteps").value = s.steps;
  $("#imgStepsVal").textContent = s.steps;
  $("#imgCfg").value = s.cfg;
  $("#imgCfgVal").textContent = Number(s.cfg).toFixed(1);
  $("#imgShift").value = s.shift;
  $("#imgShiftVal").textContent = s.shift;

  // 负面提示词：Turbo 在 CFG=1 下负面被跳过（samplers.py:610）-> 禁用
  const negSupported = !!(model && model.supports_negative);
  $("#imgNegField").style.display = negSupported ? "" : "none";
  $("#imgNegNote").textContent = negSupported
    ? "Base 非蒸馏，负面提示词生效（官方强烈建议）"
    : "当前版本为蒸馏模型：CFG=1 时 ComfyUI 会跳过 uncond 分支，负面提示词数学上无效，已禁用。";
  if (negSupported) $("#imgNegative").value = s.negative || "";

  updateImageSizeFields();
  updateImageModelNote();
}

function updateImageSizeFields() {
  const [w, h] = imgCurrentSize();
  $("#imgWidth").value = w;
  $("#imgHeight").value = h;
  $("#imgSizeHint").textContent = `${w}×${h}`;
  const mp = (w * h) / (1024 * 1024);
  const up = mp > (IMG_OPTIONS.maxDirectMp || 3);
  const manual = state.img.width && state.img.height;
  const parts = [`${mp.toFixed(2)} MP`];
  if (up) parts.push("超过 3MP，改走「生成 + 官方 2K 放大」路径");
  if (manual) parts.push("已手动微调宽高，点「自动」可恢复档位计算值");
  $("#imgSizeManualNote").textContent = parts.join(" · ");
}

// 切模型时把该版本的官方推荐值同步到各控件（推荐值来自服务端下发的清单）
function applyModelDefaults(modelKey) {
  const m = IMG_OPTIONS.models[modelKey];
  if (!m) return;
  const s = state.img;
  s.model = modelKey;
  s.steps = m.steps;
  s.cfg = m.cfg;
  s.shift = m.shift;
  s.sampler = m.sampler;
  s.scheduler = m.scheduler;
}

function updateImageModelNote() {
  const m = IMG_OPTIONS.models[state.img.model];
  if (!m) return;
  $("#imgModelNote").textContent = m.note;
  if (m.warn) $("#imgModelWarn").textContent = m.warn;
  // 推荐值高亮：当前值等于该模型推荐值时打「推荐」标记
  $("#imgSamplerHint").textContent = state.img.sampler === m.sampler ? "推荐" : "已改";
  $("#imgSchedulerHint").textContent = state.img.scheduler === m.scheduler ? "推荐" : "已改";
  $("#imgStepsHint").textContent = state.img.steps === m.steps ? "推荐" : "已改";
  $("#imgCfgHint").textContent = Number(state.img.cfg) === Number(m.cfg) ? "推荐" : "已改";
  $("#imgShiftHint").textContent = Number(state.img.shift) === Number(m.shift) ? "推荐" : "已改";
}

$("#imgResSelect").addEventListener("change", e => {
  state.img.tier = e.target.value;
  state.img.width = null; state.img.height = null;
  renderImagePanel();
});
$("#imgModelSelect").addEventListener("change", e => {
  applyModelDefaults(e.target.value);
  renderImagePanel();
});
$("#imgSamplerSelect").addEventListener("change", e => {
  state.img.sampler = e.target.value;
  updateImageModelNote();
});
$("#imgSchedulerSelect").addEventListener("change", e => {
  state.img.scheduler = e.target.value;
  updateImageModelNote();
});
$("#imgSteps").addEventListener("input", e => {
  state.img.steps = parseInt(e.target.value);
  $("#imgStepsVal").textContent = state.img.steps;
  updateImageModelNote();
});
$("#imgCfg").addEventListener("input", e => {
  state.img.cfg = parseFloat(e.target.value);
  $("#imgCfgVal").textContent = state.img.cfg.toFixed(1);
  updateImageModelNote();
});
$("#imgShift").addEventListener("input", e => {
  state.img.shift = parseFloat(e.target.value);
  $("#imgShiftVal").textContent = state.img.shift;
  updateImageModelNote();
});
$("#imgNegative").addEventListener("input", e => {
  state.img.negative = e.target.value;
});
$("#imgWidth").addEventListener("input", e => {
  const v = parseInt(e.target.value);
  state.img.width = Number.isFinite(v) && v > 0 ? v : null;
  updateImageSizeFields();
});
$("#imgHeight").addEventListener("input", e => {
  const v = parseInt(e.target.value);
  state.img.height = Number.isFinite(v) && v > 0 ? v : null;
  updateImageSizeFields();
});
$("#imgSizeReset").addEventListener("click", () => {
  state.img.width = null; state.img.height = null;
  renderImagePanel();
});

// 拉取服务端下发的档位 / 比例 / 模型清单
async function loadImageOptions() {
  try {
    const resp = await fetch("/api/image-options", {cache: "no-store"});
    if (!resp.ok) return;
    const data = await resp.json();
    IMG_OPTIONS.tiers = data.tiers || [];
    IMG_OPTIONS.aspects = data.aspects || [];
    IMG_OPTIONS.models = data.models || {};
    IMG_OPTIONS.sizeMultiple = data.size_multiple || 16;
    IMG_OPTIONS.maxDirectMp = data.max_direct_mp || 3;
    if (data.default_model && IMG_OPTIONS.models[data.default_model]) {
      state.img.model = data.default_model;
    }
    if (data.default_tier) state.img.tier = data.default_tier;
    const known = IMG_OPTIONS.aspects.some(a => a.key === state.img.aspect);
    if (!known && IMG_OPTIONS.aspects.length) state.img.aspect = IMG_OPTIONS.aspects[0].key;
    applyModelDefaults(state.img.model);
    if (state.mode === "t2i") renderImagePanel();
  } catch (e) { /* 忽略：保持内置默认值 */ }
}

// ---------- 通用分段选择 ----------
function bindSeg(sel, key, cb) {
  $$(sel + " button").forEach(b => b.addEventListener("click", () => {
    $$(sel + " button").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    state[key] = b.dataset.v;
    if (cb) cb();
  }));
}
bindSeg("#resSeg", "res");
bindSeg("#aspectChips", "aspect");
bindSeg("#modelCards", "model", () => {
  $("#loraField").style.display = state.model === "lora" ? "" : "none";
  updateSeedStrengthLayout();
  setDefaultSteps();
});
bindSeg("#loraCards", "lora");
$("#loraStrength").addEventListener("input", e => {
  state.loraStrength = parseFloat(e.target.value);
  $("#loraStrengthVal").textContent = state.loraStrength.toFixed(2);
});
$("#steps").addEventListener("input", e => {
  state.steps = parseInt(e.target.value);
  $("#stepsVal").textContent = state.steps;
});
function setDefaultSteps() {
  const def = state.model === "lora" ? 6 : 20;
  state.steps = def;
  $("#steps").value = def;
  $("#stepsVal").textContent = def;
}
function updateSeedStrengthLayout() {
  const showStrength = MODE_INFO[state.mode].kind === "video" && state.model === "lora";
  $("#loraStrengthField").style.display = showStrength ? "" : "none";
  $("#seedStrengthPair").classList.toggle("seed-only", !showStrength);
}
$("#duration").addEventListener("input", e => {
  state.duration = parseInt(e.target.value);
  $("#durVal").textContent = state.duration + "s";
});

// ---------- 提示词增强状态（图像页未增强拦截） ----------
// 记录「当前提示词已被增强」的时刻：用户手动编辑过就视为未增强。
const promptEnhance = { text: null, busy: false };

function promptIsEnhanced() {
  return promptEnhance.text !== null && $("#prompt").value === promptEnhance.text;
}

function markPromptEnhanced() {
  promptEnhance.text = $("#prompt").value;
  updatePromptBadge();
}

function clearPromptEnhanced() {
  promptEnhance.text = null;
  updatePromptBadge();
}

function updatePromptBadge() {
  const badge = $("#promptEnhancedBadge");
  if (badge) badge.hidden = !promptIsEnhanced();
}

// 手动编辑提示词 -> 增强标记立即失效
$("#prompt").addEventListener("input", () => {
  promptEnhance.text = null;
  updatePromptBadge();
});

// 增强中 / 生成中互斥：避免并发重复提交
function updateActionButtons() {
  const busy = promptEnhance.busy || state.genBusy;
  const genBtn = $("#genBtn");
  const enhBtn = $("#enhanceBtn");
  const enhText = $("#enhanceBtnText");
  if (genBtn) {
    genBtn.disabled = busy;
    genBtn.classList.toggle("busy", state.genBusy);
    genBtn.textContent = state.genBusy ? "生成中…" : "生成";
  }
  if (enhBtn) {
    enhBtn.disabled = busy;
    enhBtn.classList.toggle("busy", promptEnhance.busy);
  }
  if (enhText) enhText.textContent = promptEnhance.busy ? "正在增强…" : "增强";
}

// ---------- 未增强拦截：确认用原始提示词生成 / 取消中止 ----------
let gateResolve = null;

function askEnhanceGate() {
  const modal = $("#enhanceGateModal");
  if (!modal) return Promise.resolve(true);
  modal.hidden = false;
  document.body.classList.add("modal-open");
  return new Promise(resolve => { gateResolve = resolve; });
}

function closeEnhanceGate(confirmed) {
  const modal = $("#enhanceGateModal");
  if (!modal || modal.hidden) return;
  modal.hidden = true;
  document.body.classList.remove("modal-open");
  const fn = gateResolve;
  gateResolve = null;
  if (fn) fn(!!confirmed);
}

$("#enhanceGateConfirm").addEventListener("click", () => closeEnhanceGate(true));
$("#enhanceGateCancel").addEventListener("click", () => closeEnhanceGate(false));
$("#enhanceGateClose").addEventListener("click", () => closeEnhanceGate(false));
$("#enhanceGateModal").addEventListener("click", event => {
  if (event.target === $("#enhanceGateModal")) closeEnhanceGate(false);
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && !$("#enhanceGateModal").hidden) closeEnhanceGate(false);
});

// ---------- 生成中跟踪：任务到达终态后恢复按钮 ----------
const pendingJobs = new Set();
const JOB_POLL_MAX_MS = 30 * 60 * 1000;
let jobPollStartedAt = 0;

function trackJob(jobId) {
  if (!jobId) return;
  pendingJobs.add(jobId);
  if (!jobPollStartedAt) jobPollStartedAt = Date.now();
  state.genBusy = true;
  updateActionButtons();
  pollPendingJobs();
}

async function pollPendingJobs() {
  if (!pendingJobs.size) {
    state.genBusy = false;
    jobPollStartedAt = 0;
    updateActionButtons();
    return;
  }
  // 安全上限：极端情况下（后端不回写状态）也不要永久锁死按钮
  if (Date.now() - jobPollStartedAt > JOB_POLL_MAX_MS) {
    pendingJobs.clear();
    state.genBusy = false;
    jobPollStartedAt = 0;
    updateActionButtons();
    return;
  }
  for (const id of [...pendingJobs]) {
    try {
      const resp = await fetch(`/api/jobs/${id}`, {cache: "no-store"});
      if (!resp.ok) continue;
      const job = await resp.json();
      if (!["queued", "running"].includes(job.status)) pendingJobs.delete(id);
    } catch (e) { /* 网络抖动则下一轮再试 */ }
  }
  state.genBusy = pendingJobs.size > 0;
  if (!state.genBusy) jobPollStartedAt = 0;
  updateActionButtons();
  if (pendingJobs.size) setTimeout(pollPendingJobs, 1500);
}

// ---------- 提交 ----------
$("#genBtn").addEventListener("click", async () => {
  if (state.genBusy || promptEnhance.busy) return;
  const prompt = $("#prompt").value.trim();
  if (!prompt) { alert("请填写提示词"); return; }
  const mode = state.mode;
  // 图像模式：提示词未增强时先确认
  //   确认 -> 用用户原始提示词继续生成；取消/关闭 -> 中止本次生成（不自动开始增强）
  if (mode === "t2i" && !promptIsEnhanced()) {
    const ok = await askEnhanceGate();
    if (!ok) return;
  }
  const fd = new FormData();
  fd.append("mode", mode);
  const params = {
    prompt,
    resolution: state.res,
    aspect: state.aspect,
    duration: state.duration,
    steps: state.steps,
    model: state.model,
    lora: state.model === "lora" ? state.lora : "none",
    lora_strength: state.loraStrength,
    seed: $("#seed").value.trim() || null,
  };
  if (mode === "t2i") {
    // 图像模式：把档位 / 比例 / 实际宽高 / Z-Image 采样参数一起带上去。
    // resolution 复用同一个字段，图像模式的值是分辨率档位（1mp / 480p…4k）。
    const [w, h] = imgCurrentSize();
    params.resolution = state.img.tier;
    params.aspect = state.img.aspect;
    params.width = w;
    params.height = h;
    params.model = state.img.model;
    params.sampler = state.img.sampler;
    params.scheduler = state.img.scheduler;
    params.steps = state.img.steps;
    params.cfg = state.img.cfg;
    params.shift = state.img.shift;
    params.negative_prompt = state.img.negative || "";
  }
  fd.append("params", JSON.stringify(params));
  try {
    if (mode === "flf") {
      if (!state.files.first) { alert("首尾帧模式需要上传首帧"); return; }
      fd.append("file_first", state.files.first);
      if (state.files.last) fd.append("file_last", state.files.last);
    } else if (mode === "r2v") {
      const refs = [state.files.ref0, state.files.ref1, state.files.ref2].filter(Boolean);
      if (!refs.length) { alert("参考模式至少上传 1 张参考图"); return; }
      refs.forEach(f => fd.append("file_ref", f));
    }
    const resp = await fetch("/api/jobs", { method: "POST", body: fd });
    const data = await resp.json();
    if (!resp.ok) { alert(data.error || "提交失败"); return; }
    trackJob(data.id);
    // 清空文件选择（保留提示词便于连做）
    state.files = { first: null, last: null, refs: [] };
    renderMode();
    state.tab = "active";
    renderTabs();
    refreshJobs();
  } catch (e) {
    alert("提交失败: " + e.message);
  }
});

// ---------- 任务列表 ----------
function renderTabs() {
  $$(".jobs-tabs button").forEach(b => b.classList.toggle("active", b.dataset.st === state.tab));
  $("#doneFilters").hidden = state.tab !== "done";
}
$$(".jobs-tabs button").forEach(b => b.addEventListener("click", () => {
  state.tab = b.dataset.st;
  if (["done", "longfilm"].includes(state.tab)) state.donePage = 1;
  showCreate();
  renderTabs();
  refreshJobs();
}));

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtElapsed(sec) {
  if (sec == null) return "—";
  return sec < 90 ? `${Math.round(sec)}s` : `${Math.floor(sec / 60)}m${Math.round(sec % 60)}s`;
}

function modelLabel(p) {
  const steps = p.steps || (p.model === "standard" ? 20 : 4);
  if (p.model === "standard") return `标准 H3 · ${steps}步`;
  return `H3 + ${p.lora === "ckpt850" ? "ckpt850" : "v4-step600"} · ${steps}步`;
}

// 图像模式：模型版本的可读名（清单来自 /api/image-options）
function imgModelLabel(key) {
  const m = IMG_OPTIONS.models[key];
  return m ? m.label : (key || "—");
}

function outputUrl(job, output) {
  const relative = String(output.file || "").replace(/\\/g, "/");
  const path = relative.includes("/") ? relative : `${job.id}/${relative}`;
  return "/files/" + path.split("/").map(encodeURIComponent).join("/");
}

function jobCard(job) {
  const p = job.params || {};
  const info = MODE_INFO[job.mode] || { name: job.mode };
  const card = document.createElement("div");
  card.className = "job-card";
  const runningOrQueued = ["queued", "running"].includes(job.status);
  const prog = job.status === "queued" ? 0 : (job.status === "running" ? Math.min(job.progress || 0.03, 0.98) : 1);
  const pctText = job.status === "queued" ? "排队中…"
    : `${(job.progress_text || "生成中")} · ${Math.round(prog * 100)}%`;

  let preview = "";
  const video = (job.outputs || []).find(o => o.kind === "video");
  const image = (job.outputs || []).find(o => o.kind === "image");
  if (video) {
    preview = `<video src="${outputUrl(job, video)}" controls preload="metadata"></video>`;
  } else if (image) {
    preview = `<img src="${outputUrl(job, image)}">`;
  } else if (runningOrQueued) {
    preview = `<div class="progress-wrap">
      <div class="progress-bar"><div style="width:${Math.round(prog * 100)}%"></div></div>
      <div class="progress-text">${pctText}</div>
    </div>`;
  } else if (job.status === "failed") {
    preview = `<div class="progress-wrap"><div class="err-text">✕ ${job.error || "生成失败"}</div></div>`;
  } else if (job.status === "canceled") {
    preview = `<div class="progress-wrap"><div class="progress-text">已取消</div></div>`;
  }

  const topRight = runningOrQueued
    ? `<button class="cancel-btn" data-act="cancel">取消</button>`
    : `<span>${fmtTime(job.created_at)}</span>`;

  card.dataset.jid = job.id;
  card.innerHTML = `
    <div class="job-top"><span>${job.mode === "longfilm" ? "长片 · " + escapeHtml(p.title || "未命名") : info.name} · ${fmtTime(job.created_at)}</span>${topRight}</div>
    <div class="job-preview">${preview}</div>
    ${job.status === "failed" ? "" : `<div class="job-meta">
      ${job.mode === "longfilm" ? `
      <div class="row"><span>标题</span><b>${escapeHtml(p.title || "未命名")}</b></div>
      <div class="row"><span>片段</span><b>${(p.clips || []).length} 个</b></div>
      <div class="row longfilm-intro"><span>简介</span><b>${escapeHtml((p.intro || "—").slice(0, 80))}</b></div>` : `
      <div class="row"><span>模式</span><b>${info.name}</b></div>
      ${job.mode === "t2i" ? `<div class="row"><span>分辨率</span><b>${p.width || "—"}×${p.height || "—"} · ${p.aspect || "16:9"}</b></div>
      <div class="row"><span>模型</span><b>${escapeHtml(imgModelLabel(p.model))}</b></div>
      <div class="row"><span>采样</span><b>${p.sampler || "—"} · ${p.scheduler || "—"}</b></div>
      <div class="row"><span>步数/CFG</span><b>${p.steps || "—"} · ${p.cfg ?? "—"}</b></div>` : `
      <div class="row"><span>分辨率</span><b>${p.resolution || "768p"} · ${p.aspect || "16:9"}</b></div>
      ${info.kind === "video" ? `<div class="row"><span>时长</span><b>${p.duration || 5}s</b></div>
      <div class="row"><span>模型</span><b>${modelLabel(p)}</b></div>` : ""}`}
      <div class="row"><span>种子</span><b>${p.seed || "随机"}</b></div>`}
      <div class="row"><span>耗时</span><b>${job.status === "done" ? fmtElapsed(job.elapsed) : fmtElapsed(job.elapsed)}</b></div>
      ${job.mode !== "longfilm" ? `<button class="row prompt-row" data-act="prompt" title="点击查看并复制完整提示词"><span>提示词</span><b>${escapeHtml((p.prompt || "").slice(0, 80))}</b></button>` : ""}
    </div>`}
    <div class="job-actions">
      ${video || image ? `<button data-act="download">⬇ 下载</button>` : ""}
      ${video && job.status === "done" && state.page === "stitch" ? `<button data-act="add-stitch">＋ 拼接</button>` : ""}
      ${job.mode !== "longfilm" ? `<button data-act="reuse">复用参数</button>` : ""}
      ${!runningOrQueued ? `<button data-act="delete" class="danger">删除</button>` : ""}
    </div>`;

  if (image) {
    const dragImage = card.querySelector(".job-preview img");
    const source = {url: outputUrl(job, image), name: String(image.file || "completed-image.png").split(/[\\/]/).pop()};
    dragImage.draggable = true;
    dragImage.title = "拖到左侧参考图、首帧或尾帧位置";
    dragImage.addEventListener("dragstart", event => {
      event.dataTransfer.effectAllowed = "copy";
      event.dataTransfer.setData("application/x-h3-image", JSON.stringify(source));
      event.dataTransfer.setData("text/uri-list", new URL(source.url, location.href).href);
      card.classList.add("dragging-image");
    });
    dragImage.addEventListener("dragend", () => card.classList.remove("dragging-image"));
  }
  if (video && job.status === "done") {
    const source = {jobId: job.id, file: video.file, name: String(video.file || "video.mp4").split(/[\\/]/).pop(),
      url: outputUrl(job, video), duration: Number(p.duration || 0), title: p.title || info.name};
    const dragVideo = card.querySelector(".job-preview video");
    dragVideo.draggable = false;
    card.draggable = true;
    card.title = "整张卡片可拖到左侧长片拼接区";
    card.addEventListener("dragstart", event => {
      if (event.target.closest("button")) { event.preventDefault(); return; }
      event.dataTransfer.effectAllowed = "copy";
      event.dataTransfer.setData("application/x-h3-video", JSON.stringify(source));
      card.classList.add("dragging-image");
    });
    card.addEventListener("dragend", () => card.classList.remove("dragging-image"));
    card.querySelector('[data-act="add-stitch"]')?.addEventListener("click", () => {
      state.stitchClips.push(source);
      renderStitchClips();
      $("#stitchPanel").scrollTo({top: $("#stitchPanel").scrollHeight, behavior: "smooth"});
    });
  }

  card.querySelector('[data-act="cancel"]')?.addEventListener("click", async event => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "正在取消…";
    try {
      const resp = await fetch(`/api/jobs/${job.id}/cancel`, {method:"POST"});
      if (!resp.ok) throw new Error((await resp.json()).error || "取消失败");
      await refreshJobs();
    } catch (err) {
      button.disabled = false;
      button.textContent = "取消";
      alert(err.message);
    }
  });
  card.querySelector('[data-act="delete"]')?.addEventListener("click", async () => {
    if (!confirm("删除该记录及输出文件？")) return;
    await fetch(`/api/jobs/${job.id}`, { method: "DELETE" });
    refreshJobs();
  });
  card.querySelector('[data-act="download"]')?.addEventListener("click", () => {
    const f = video || image;
    const a = document.createElement("a");
    a.href = outputUrl(job, f);
    a.download = f.file.split(/[\\/]/).pop();
    a.click();
  });
  card.querySelector('[data-act="prompt"]')?.addEventListener("click", () => openPromptModal(p.prompt || ""));
  card.querySelector('[data-act="reuse"]')?.addEventListener("click", () => {
    // 找到对应模式侧栏项并切换
    $(`.nav-item[data-mode="${job.mode}"]`)?.click();
    $("#prompt").value = p.prompt || "";
    clearPromptEnhanced();
    if (job.mode === "t2i") {
      // 图像模式：还原分辨率档位 / 比例 / 实际宽高 / Z-Image 采样参数
      const s = state.img;
      if (p.resolution) s.tier = p.resolution;
      if (p.aspect) s.aspect = p.aspect;
      if (p.width && p.height) { s.width = p.width; s.height = p.height; }
      if (p.model && IMG_OPTIONS.models[p.model]) applyModelDefaults(p.model);
      if (p.sampler) s.sampler = p.sampler;
      if (p.scheduler) s.scheduler = p.scheduler;
      if (p.steps) s.steps = p.steps;
      if (p.cfg != null) s.cfg = p.cfg;
      if (p.shift != null) s.shift = p.shift;
      if (p.negative_prompt != null) s.negative = p.negative_prompt;
      renderImagePanel();
    } else {
      if (p.resolution) setSeg("#resSeg", p.resolution), state.res = p.resolution;
      if (p.aspect) setSeg("#aspectChips", p.aspect), state.aspect = p.aspect;
    }
    if (p.duration) { state.duration = p.duration; $("#duration").value = p.duration; $("#durVal").textContent = p.duration + "s"; }
    if (p.steps && job.mode !== "t2i") { state.steps = p.steps; $("#steps").value = p.steps; $("#stepsVal").textContent = p.steps; }
    if (p.seed) $("#seed").value = p.seed;
    if (p.model && job.mode !== "t2i") { setSeg("#modelCards", p.model); state.model = p.model; $("#loraField").style.display = p.model === "lora" ? "" : "none"; updateSeedStrengthLayout(); }
    if (p.lora) { setSeg("#loraCards", p.lora); state.lora = p.lora; }
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
  return card;
}

function openPromptModal(prompt) {
  $("#promptModalText").value = prompt;
  $("#promptModal").hidden = false;
  document.body.classList.add("modal-open");
}

function closePromptModal() {
  $("#promptModal").hidden = true;
  document.body.classList.remove("modal-open");
}

$("#promptModalClose").addEventListener("click", closePromptModal);
$("#promptModal").addEventListener("click", event => {
  if (event.target === $("#promptModal")) closePromptModal();
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && !$("#promptModal").hidden) closePromptModal();
});
$("#promptCopy").addEventListener("click", async () => {
  const textArea = $("#promptModalText");
  const button = $("#promptCopy");
  try {
    await navigator.clipboard.writeText(textArea.value);
  } catch (_) {
    textArea.focus();
    textArea.select();
    document.execCommand("copy");
  }
  button.textContent = "已复制";
  setTimeout(() => { button.textContent = "复制提示词"; }, 1200);
});

function setSeg(sel, val) {
  $$(sel + " button").forEach(b => b.classList.toggle("active", b.dataset.v === val));
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function jobSig(j) {
  return [j.id, j.status, Math.round((j.progress || 0) * 100), j.progress_text || "",
    (j.outputs || []).map(o => o.file).join(",")].join("|");
}

let jobsRequest = 0;
let displayedTab = null;
const DONE_PAGE_SIZE = 8;
let filterDebounce = null;

function applyDoneFilters() {
  state.doneFilters = {
    keyword: $("#doneSearch").value.trim(),
    media: $("#doneMedia").value,
    aspect: $("#doneAspect").value,
    duration: $("#doneDuration").value,
  };
  state.donePage = 1;
  refreshJobs();
}

$("#doneSearch").addEventListener("input", () => {
  clearTimeout(filterDebounce);
  filterDebounce = setTimeout(applyDoneFilters, 280);
});
["#doneMedia", "#doneAspect", "#doneDuration"].forEach(selector => {
  $(selector).addEventListener("change", applyDoneFilters);
});
$("#doneFilterReset").addEventListener("click", () => {
  $("#doneSearch").value = "";
  $("#doneMedia").value = "";
  $("#doneAspect").value = "";
  $("#doneDuration").value = "";
  applyDoneFilters();
});

function renderDonePagination(total, currentPage) {
  const pager = $("#jobsPagination");
  if (!["done", "longfilm"].includes(state.tab) || total <= DONE_PAGE_SIZE) {
    pager.hidden = true;
    pager.innerHTML = "";
    return;
  }
  const totalPages = Math.max(1, Math.ceil(total / DONE_PAGE_SIZE));
  const pages = [];
  for (let page = 1; page <= totalPages; page++) {
    if (page === 1 || page === totalPages || Math.abs(page - currentPage) <= 1) pages.push(page);
  }
  const controls = [`<button data-page="${currentPage - 1}" ${currentPage === 1 ? "disabled" : ""}>上一页</button>`];
  let previous = 0;
  pages.forEach(page => {
    if (previous && page - previous > 1) controls.push('<span class="page-summary">…</span>');
    controls.push(`<button data-page="${page}" class="${page === currentPage ? "active" : ""}">${page}</button>`);
    previous = page;
  });
  controls.push(`<button data-page="${currentPage + 1}" ${currentPage === totalPages ? "disabled" : ""}>下一页</button>`);
  controls.push(`<span class="page-summary">共 ${total} 个作品</span>`);
  pager.innerHTML = controls.join("");
  pager.hidden = false;
  pager.querySelectorAll("button[data-page]").forEach(button => button.addEventListener("click", () => {
    if (button.disabled) return;
    state.donePage = Number(button.dataset.page);
    refreshJobs();
    $("#jobsArea").scrollTo({top: 0, behavior: "smooth"});
  }));
}

async function refreshJobs() {
  if (state.page === "system") return;
  const tab = state.tab;
  const ticket = ++jobsRequest;
  const grid = $("#jobsGrid");
  if (displayedTab !== tab) {
    grid.textContent = "正在读取任务…";
    displayedTab = tab;
  }
  try {
    const statuses = tab === "active" ? ["queued", "running"] : [tab === "longfilm" ? "done" : tab];
    const groups = await Promise.all(statuses.map(async status => {
      const query = new URLSearchParams({status});
      if (status === "done") {
        query.set("page", state.donePage);
        query.set("page_size", DONE_PAGE_SIZE);
        if (tab === "longfilm") query.set("media", "longfilm");
        else Object.entries(state.doneFilters).forEach(([key, value]) => { if (value) query.set(key, value); });
      }
      const response = await fetch("/api/jobs?" + query.toString(), {cache: "no-store"});
      if (!response.ok) throw new Error("HTTP " + response.status);
      const data = await response.json();
      return {jobs: data.jobs.filter(job => job.status === status), total: data.total ?? data.jobs.length, page: data.page ?? 1};
    }));
    if (ticket !== jobsRequest || tab !== state.tab) return;
    if (["done", "longfilm"].includes(tab)) {
      state.donePage = groups[0].page;
      renderDonePagination(groups[0].total, state.donePage);
    } else {
      renderDonePagination(0, 1);
    }
    const jobs = [...new Map(groups.flatMap(group => group.jobs).map(job => [job.id, job])).values()]
      .sort((a,b) => (b.status === "running") - (a.status === "running") || b.created_at - a.created_at);
    if (!jobs.length) {
      grid.innerHTML = '<div class="empty-hint">' + ({active:"当前没有排队或生成中的任务", done:"暂无已完成的任务", longfilm:"暂无已合成的长片", failed:"暂无失败的任务", canceled:"暂无已取消的任务"}[tab]) + '</div>';
      return;
    }
    for (const node of [...grid.childNodes]) if (!node.dataset?.jid) node.remove();

    // 增量更新：数据未变化的卡片不重建，避免打断视频播放
    const existing = new Map();
    [...grid.children].forEach(el => { if (el.dataset?.jid) existing.set(el.dataset.jid, el); });
    const seen = new Set();
    jobs.forEach(j => {
      seen.add(j.id);
      const sig = jobSig(j);
      let el = existing.get(j.id);
      if (el && el.dataset.sig !== sig) {
        const fresh = jobCard(j);
        el.replaceWith(fresh);
        el = fresh;
      } else if (!el) {
        el = jobCard(j);
      }
      el.dataset.sig = sig;
      // appendChild 对已存在节点是"移动"，不销毁元素，视频播放不中断
      grid.appendChild(el);
    });
    existing.forEach((el, id) => { if (!seen.has(id)) el.remove(); });
  } catch (e) { if (ticket === jobsRequest && tab === state.tab) grid.textContent = "任务列表读取失败，正在重试…"; }
}

// ---------- 长片拼接 ----------
function renderStitchClips() {
  const list = $("#stitchList");
  list.innerHTML = "";
  state.stitchClips.forEach((clip, index) => {
    const item = document.createElement("div");
    item.className = "stitch-clip";
    item.draggable = true;
    item.dataset.index = index;
    item.innerHTML = `<span class="stitch-index">${index + 1}</span>
      <video class="stitch-thumb" src="${clip.url}" muted preload="metadata"></video>
      <span class="stitch-clip-info"><b>${escapeHtml(clip.title || clip.name)}</b><span>${clip.duration ? clip.duration + " 秒" : "视频片段"}</span></span>
      <button class="stitch-remove" title="移除">×</button>`;
    item.querySelector("video").draggable = false;
    item.querySelector(".stitch-remove").addEventListener("click", () => {
      state.stitchClips.splice(index, 1);
      renderStitchClips();
    });
    item.addEventListener("dragstart", event => {
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("application/x-h3-clip-index", String(index));
      item.classList.add("dragging");
    });
    item.addEventListener("dragend", () => item.classList.remove("dragging"));
    item.addEventListener("dragover", event => {
      if (!event.dataTransfer.types.includes("application/x-h3-clip-index")) return;
      event.preventDefault();
      item.classList.add("drag-target");
    });
    item.addEventListener("dragleave", () => item.classList.remove("drag-target"));
    item.addEventListener("drop", event => {
      const from = Number(event.dataTransfer.getData("application/x-h3-clip-index"));
      if (!Number.isInteger(from)) return;
      event.preventDefault();
      item.classList.remove("drag-target");
      const [moved] = state.stitchClips.splice(from, 1);
      state.stitchClips.splice(index, 0, moved);
      renderStitchClips();
    });
    list.appendChild(item);
  });
  const total = state.stitchClips.reduce((sum, clip) => sum + Number(clip.duration || 0), 0);
  $("#stitchSummary").textContent = state.stitchClips.length
    ? `已选择 ${state.stitchClips.length} 个片段${total ? ` · 预计 ${total} 秒` : ""}`
    : "尚未选择片段";
  $("#stitchBtn").disabled = state.stitchClips.length < 2;
}

const stitchDrop = $("#stitchDrop");
stitchDrop.addEventListener("dragover", event => {
  if (!event.dataTransfer.types.includes("application/x-h3-video")) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
  stitchDrop.classList.add("drag-over");
});
stitchDrop.addEventListener("dragleave", () => stitchDrop.classList.remove("drag-over"));
stitchDrop.addEventListener("drop", event => {
  const raw = event.dataTransfer.getData("application/x-h3-video");
  if (!raw) return;
  event.preventDefault();
  stitchDrop.classList.remove("drag-over");
  try {
    state.stitchClips.push(JSON.parse(raw));
    renderStitchClips();
  } catch (_) {
    alert("无法读取这个视频片段");
  }
});

$("#stitchBtn").addEventListener("click", async () => {
  const title = $("#longfilmTitle").value.trim();
  const intro = $("#longfilmIntro").value.trim();
  if (!title) { alert("请填写长片标题"); return; }
  if (state.stitchClips.length < 2) { alert("至少拖入 2 个视频片段"); return; }
  const button = $("#stitchBtn");
  button.disabled = true;
  button.textContent = "正在提交…";
  try {
    const response = await fetch("/api/longfilms", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({title, intro,
        duration: state.stitchClips.reduce((sum, clip) => sum + Number(clip.duration || 0), 0),
        clips: state.stitchClips.map(clip => ({job_id: clip.jobId, file: clip.file, name: clip.name}))})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "提交失败");
    state.stitchClips = [];
    $("#longfilmTitle").value = "";
    $("#longfilmIntro").value = "";
    renderStitchClips();
    state.tab = "longfilm";
    renderTabs();
    refreshJobs();
  } catch (error) {
    alert("长片合成提交失败：" + error.message);
  } finally {
    button.textContent = "合成长片";
    button.disabled = state.stitchClips.length < 2;
  }
});

// ---------- 状态栏 ----------
async function refreshSystem() {
  try {
    const r = await (await fetch("/api/system")).json();
    const el = $("#statusText");
    const dot = $("#topStatus .dot");
    if (!r.comfyui_ok) {
      el.textContent = "ComfyUI 未连接";
      dot.className = "dot dot-off";
      return;
    }
    if (r.running) {
      const queuedTxt = r.queued > 0 ? `，${r.queued} 个排队` : "";
      el.textContent = `Generating${queuedTxt}`;
      dot.className = "dot dot-busy";
    } else if (r.queued > 0) {
      el.textContent = `${r.queued} 个排队中`;
      dot.className = "dot dot-busy";
    } else {
      el.textContent = "空闲";
      dot.className = "dot dot-ok";
    }
  } catch (e) { /* 忽略 */ }
}

$("#unloadBtn").addEventListener("click", async () => {
  if (!confirm("让 ComfyUI 释放模型显存？")) return;
  await fetch("/api/unload", { method: "POST" });
});

// ---------- 硬件参数页 ----------
function showCreate() {
  state.page = "create";
  $("#jobsArea").style.display = "";
  $("#panel").style.display = "";
  $("#stitchPanel").style.display = "none";
  $("#systemPage").style.display = "none";
  $("#providersPage").style.display = "none";
}

function showStitch() {
  state.page = "stitch";
  $$(".nav-item").forEach(x => x.classList.remove("active"));
  $("#navStitch").classList.add("active");
  $("#jobsArea").style.display = "";
  $("#panel").style.display = "none";
  $("#stitchPanel").style.display = "";
  $("#systemPage").style.display = "none";
  $("#providersPage").style.display = "none";
  state.tab = "done";
  state.doneFilters.media = "video";
  $("#doneMedia").value = "video";
  renderTabs();
  renderStitchClips();
  refreshJobs();
}

function showSystem() {
  state.page = "system";
  $$(".nav-item").forEach(x => x.classList.remove("active"));
  $("#navSystem").classList.add("active");
  $("#jobsArea").style.display = "none";
  $("#panel").style.display = "none";
  $("#stitchPanel").style.display = "none";
  $("#systemPage").style.display = "block";
  $("#providersPage").style.display = "none";
  refreshHardware();
}

// 本地模型配置页（实现见 providers.js）
function showProviders() {
  state.page = "providers";
  $$(".nav-item").forEach(x => x.classList.remove("active"));
  $("#navProviders").classList.add("active");
  $("#jobsArea").style.display = "none";
  $("#panel").style.display = "none";
  $("#stitchPanel").style.display = "none";
  $("#systemPage").style.display = "none";
  $("#providersPage").style.display = "block";
  loadProviders();
  loadAgentConfig();
}

$("#navSystem").addEventListener("click", showSystem);
$("#navStitch").addEventListener("click", showStitch);
$("#sysRefresh").addEventListener("click", () => { refreshHardware(true); });

function fmtBytes(b) {
  if (b == null) return "—";
  const gb = b / 1073741824;
  if (gb >= 1) return gb.toFixed(1) + " GB";
  return (b / 1048576).toFixed(0) + " MB";
}

function fmtUptime(sec) {
  if (!sec) return "—";
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  return (d ? d + " 天 " : "") + (h ? h + " 小时 " : "") + m + " 分";
}

function barClass(pct) {
  return pct >= 90 ? "bar danger" : pct >= 70 ? "bar warn" : "bar";
}

function card(title, badge, body) {
  return `<div class="sys-card">
    <div class="sys-card-title"><span>${title}</span>${badge ? `<span class="badge">${badge}</span>` : ""}</div>
    ${body}
  </div>`;
}

function renderHardware(d) {
  const grid = $("#sysGrid");
  const cards = [];

  // GPU
  (d.gpus || []).forEach(g => {
    const pct = g.vram_total ? Math.round(g.vram_used / g.vram_total * 100) : 0;
    cards.push(card("GPU 显卡", g.temp ? `${Math.round(g.temp)}°C` : "", `
      <div class="sys-main">${g.name}</div>
      <div class="bar ${pct >= 90 ? "danger" : pct >= 70 ? "warn" : ""}"><div style="width:${pct}%"></div></div>
      <div class="bar-label"><span>显存 ${fmtBytes(g.vram_used)} / ${fmtBytes(g.vram_total)}</span><span>${pct}%</span></div>
      <div class="sys-row"><span>可用显存</span><b>${fmtBytes(g.vram_free)}</b></div>
      <div class="sys-row"><span>GPU 利用率</span><b>${Math.round(g.util)}%</b></div>
      <div class="sys-row"><span>温度</span><b>${Math.round(g.temp)} °C</b></div>
      <div class="sys-row"><span>功耗</span><b>${Math.round(g.power)} W / ${Math.round(g.power_limit)} W</b></div>
      <div class="sys-row"><span>核心频率</span><b>${g.clock || "—"} MHz</b></div>
      <div class="sys-row"><span>驱动版本</span><b>${g.driver}</b></div>`));
  });
  if (!(d.gpus || []).length) {
    cards.push(card("GPU 显卡", "未检测", `<div class="sys-main">nvidia-smi 不可用</div>
      <div class="sys-row"><span>提示</span><b>请确认 NVIDIA 驱动已安装</b></div>`));
  }

  // CPU
  const c = d.cpu || {};
  cards.push(card("CPU 处理器", c.percent != null ? Math.round(c.percent) + "%" : "", `
    <div class="sys-main">${c.name || "—"}</div>
    <div class="${barClass(c.percent || 0)}"><div style="width:${c.percent || 0}%"></div></div>
    <div class="bar-label"><span>当前占用</span><span>${Math.round(c.percent || 0)}%</span></div>
    <div class="sys-row"><span>核心 / 线程</span><b>${c.cores} 核 / ${c.threads} 线程</b></div>
    <div class="sys-row"><span>当前频率</span><b>${c.freq ? c.freq + " MHz" : "—"}</b></div>`));

  // 内存
  const r = d.ram || {};
  cards.push(card("内存", r.percent != null ? Math.round(r.percent) + "%" : "", `
    <div class="sys-main">${fmtBytes(r.used)} / ${fmtBytes(r.total)}</div>
    <div class="${barClass(r.percent || 0)}"><div style="width:${r.percent || 0}%"></div></div>
    <div class="bar-label"><span>已用</span><span>${Math.round(r.percent || 0)}%</span></div>
    <div class="sys-row"><span>可用</span><b>${fmtBytes(r.available)}</b></div>
    <div class="sys-row"><span>虚拟内存</span><b>${fmtBytes(r.swap_used)} / ${fmtBytes(r.swap_total)}</b></div>`));

  // 磁盘
  const disks = (d.disks || []).map(dk => `
    <div class="sys-main" style="font-size:13px;margin-bottom:6px">${dk.device} · ${fmtBytes(dk.total)}</div>
    <div class="${barClass(dk.percent)}"><div style="width:${dk.percent}%"></div></div>
    <div class="bar-label"><span>已用 ${Math.round(dk.percent)}%</span><span>剩余 ${fmtBytes(dk.free)}</span></div>`).join('<div style="height:12px"></div>');
  cards.push(card("磁盘", `${(d.disks || []).length} 个分区`, disks || "—"));

  // 软件环境
  const sf = d.software || {};
  const os = d.os || {};
  cards.push(card("软件环境", "", `
    <div class="sys-row"><span>ComfyUI</span><b>v${sf.comfyui || "—"}</b></div>
    <div class="sys-row"><span>PyTorch</span><b>${sf.pytorch || "—"}</b></div>
    <div class="sys-row"><span>ComfyUI Python</span><b>${(sf.python || "").split(" ")[0] || "—"}</b></div>
    <div class="sys-row"><span>Studio Python</span><b>${sf.studio_python}</b></div>
    <div class="sys-row"><span>操作系统</span><b>${os.platform || "—"}</b></div>
    <div class="sys-row"><span>计算机名</span><b>${os.hostname || "—"}</b></div>
    <div class="sys-row"><span>已运行</span><b>${fmtUptime(Date.now() / 1000 - (os.boot || 0))}</b></div>`));

  // 模型库
  const models = d.models || [];
  const totalSize = models.reduce((s, m) => s + m.size, 0);
  cards.push(card("模型库", fmtBytes(totalSize), models.length
    ? models.map(m => `<div class="model-line"><span>${m.name}</span><b>${m.count} 个 · ${fmtBytes(m.size)}</b></div>`).join("")
      + `<div class="sys-row" style="margin-top:8px"><span>目录</span><b style="font-size:11px">${d.models_dir || "—"}</b></div>`
    : `<div class="sys-row"><span>目录</span><b style="font-size:11px">${d.models_dir || "—"}</b></div>
       <div class="sys-row"><span>状态</span><b>未扫描到模型</b></div>`));

  grid.innerHTML = cards.join("");
  $("#sysSub").textContent = `刷新于 ${new Date().toLocaleTimeString("zh-CN")} · 每 3 秒自动刷新`;
}

async function refreshHardware(force) {
  if (state.page !== "system" && !force) return;
  try {
    const d = await (await fetch("/api/hardware")).json();
    renderHardware(d);
    state.hwTimer = Date.now();
  } catch (e) { /* ignore */ }
}
setInterval(() => { if (state.page === "system") refreshHardware(); }, 3000);

// ---------- 启动 ----------
loadImageOptions();
renderMode();
renderTabs();
renderStitchClips();
refreshJobs();
refreshSystem();
setInterval(refreshJobs, 2000);
setInterval(refreshSystem, 3000);
