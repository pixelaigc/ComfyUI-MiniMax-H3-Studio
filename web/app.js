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

// 默认模式：整个应用只有这一个来源（导航高亮 / 参数面板 / state.mode 都用它）
const DEFAULT_MODE = "t2i";

const state = {
  // 默认进入「Z-IMAGE 文生图」。这里只是兜底初值 —— 启动时 activateMode()
  // 会用 DEFAULT_MODE 再赋一次，避免两处各写一份字面量后走偏。
  mode: DEFAULT_MODE,
  tab: "all",
  genType: "",
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
  // 素材库引用：{ 槽位: {asset_id, name, category} } —— 只存 id，不复制文件
  assetRefs: {},
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
    batchCount: 1,
    // LoRA 配置：[{name, strength, enabled}]，服务端持久化
    loras: [],
  },
};

// 生成类型清单，由 /api/generation-types 下发（后端真实注册的模式），
// 前端不硬编码任何类型名。
const GEN_TYPES = { types: [], registeredModes: [] };

// 图像模式的档位 / 比例 / 模型清单，由 /api/image-options 下发（服务端与本机
// ComfyUI 的实际权重、官方推荐参数一致），前端不硬编码模型文件名。
const IMG_OPTIONS = { tiers: [], aspects: [], models: {}, sizeMultiple: 16, maxDirectMp: 3 };
// 本机实际存在的 Z-Image LoRA（服务端下发），以及当前配置
// available = 效果类 LoRA（用户可选）
// acceleration = 加速/蒸馏类（工作流固定使用，不进下拉）
// available = 效果类 LoRA（用户可选，且已通过兼容性检测）
// acceleration = 加速/蒸馏类（工作流固定使用，不进下拉）
// incompatible = 效果类但与 Z-Image 底模不兼容（会出噪点，已排除）
const IMG_LORAS = { available: [], acceleration: [], incompatible: [], config: [] };
const IMG_ASPECT_NUM = {
  "1:1": [1, 1], "2:3": [2, 3], "3:2": [3, 2], "3:4": [3, 4],
  "4:3": [4, 3], "9:16": [9, 16], "16:9": [16, 9], "21:9": [21, 9],
};

// ---------- 侧栏切换 ----------
// 选中态与面板内容始终保持一致：同一个入口负责「打高亮 + 切面板」，
// 启动时、手动点击时都走它，避免两者不同步。
function activateMode(mode, navEl) {
  $$(".nav-item").forEach(x => x.classList.remove("active"));
  if (navEl) navEl.classList.add("active");
  state.mode = mode;
  showCreate();
  renderMode();
}

$$(".nav-item[data-mode]").forEach(el => el.addEventListener("click", () => {
  activateMode(el.dataset.mode, el);
}));

function renderMode() {
  // 模式切换后二次增强的作用对象 chips 状态要跟着刷新
  queueMicrotask(() => { try { renderPromptVersions(); } catch (e) { /* 忽略 */ } });
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
      delete state.assetRefs[key];
    };
    // 用素材库里的素材填这个槽位：只存 asset_id，不复制文件
    const fillFromAsset = (asset) => {
      slot.replaceChildren();
      const image = document.createElement("img");
      image.src = asset.preview_url || asset.content_url;
      image.alt = label;
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = label;
      const badge = document.createElement("span");
      badge.className = "tag asset-badge";
      badge.textContent = "素材库";
      slot.append(image, tag, badge);
      slot.classList.add("filled");
      state.files[key] = null;
      state.assetRefs[key] = {asset_id: asset.id, name: asset.name, category: asset.category};
    };
    inp.addEventListener("change", e => {
      const file = e.target.files[0];
      if (file) fillSlot(file);
    });
    slot.addEventListener("dragover", event => {
      const types = event.dataTransfer.types;
      if (!types.includes("application/x-h3-image") && !types.includes("application/x-h3-asset")) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
      slot.classList.add("drag-over");
    });
    slot.addEventListener("dragleave", () => slot.classList.remove("drag-over"));
    slot.addEventListener("drop", async event => {
      // 优先按 asset_id 引用（素材库拖过来的），无 asset_id 时回退旧的 blob 路径
      const assetRaw = event.dataTransfer.getData("application/x-h3-asset");
      if (assetRaw) {
        event.preventDefault();
        slot.classList.remove("drag-over");
        try {
          const payload = JSON.parse(assetRaw);
          const resp = await fetch(`/api/assets/${encodeURIComponent(payload.asset_id)}`);
          if (!resp.ok) throw new Error("素材不存在或已删除");
          const asset = await resp.json();
          const version = asset.current_version || {};
          fillFromAsset({
            id: asset.id, name: asset.name, category: asset.category,
            preview_url: asset.preview_url,
            content_url: asset.content_url || `/api/assets/${asset.id}/content`,
          });
        } catch (error) {
          alert("无法引用这个素材：" + error.message);
        }
        return;
      }
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
    slot.addEventListener("click", event => {
      if (event.target.closest(".pick-asset")) return;
      inp.click();
    });
    // 「从素材库选」按钮
    const pickBtn = document.createElement("button");
    pickBtn.type = "button";
    pickBtn.className = "pick-asset";
    pickBtn.textContent = "素材库";
    pickBtn.title = "从素材库选择一个素材填入这个位置";
    pickBtn.addEventListener("click", event => {
      event.stopPropagation();
      if (!window.ASSETLIB || !window.ASSETLIB.openPicker) {
        alert("素材库模块未加载");
        return;
      }
      window.ASSETLIB.openPicker({
        categories: ["image"], multiple: false, limit: 1,
        onDone: (ids) => {
          if (!ids.length) return;
          const assetId = ids[0];
          fetch(`/api/assets/${encodeURIComponent(assetId)}`).then(r => r.json()).then(asset => {
            fillFromAsset({
              id: asset.id, name: asset.name, category: asset.category,
              preview_url: asset.preview_url,
              content_url: `/api/assets/${asset.id}/content`,
            });
          }).catch(err => alert("读取素材失败：" + err.message));
        },
      });
    });
    slot.appendChild(pickBtn);
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
  // 模式变了 -> 增强模板下拉按新模式过滤，当前模板不适用则自动回落
  if (typeof refreshEnhanceSpecForMode === "function") refreshEnhanceSpecForMode();
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

// ---------- 生成批次（1-5） ----------
const IMG_BATCH_OPTIONS = [1, 2, 3, 4, 5];

function renderBatchChips() {
  const box = $("#imgBatchChips");
  if (!box) return;
  box.innerHTML = IMG_BATCH_OPTIONS.map(n =>
    `<button type="button" data-n="${n}" class="${n === state.img.batchCount ? "active" : ""}">${n} 张</button>`
  ).join("");
  box.querySelectorAll("button").forEach(btn => {
    btn.addEventListener("click", () => {
      state.img.batchCount = Number(btn.dataset.n);
      renderBatchChips();
    });
  });
  const hint = $("#imgBatchHint");
  if (hint) {
    hint.textContent = state.img.batchCount > 1
      ? `一次出 ${state.img.batchCount} 张 · 独立种子`
      : "一次出几张";
  }
}

// ---------- LoRA 面板 ----------
function imgLoraMeta(name) {
  return IMG_LORAS.available.find(l => l.name === name) || null;
}

function renderLoraSelect() {
  const sel = $("#imgLoraSelect");
  if (!sel) return;
  const used = new Set(state.img.loras.map(l => l.name));
  const options = IMG_LORAS.available.filter(l => !used.has(l.name));
  sel.innerHTML = options.length
    ? options.map(l => `<option value="${esc(l.name)}">${esc(l.display)}</option>`).join("")
    : `<option value="">${IMG_LORAS.available.length ? "全部已添加" : "本机未找到 Z-Image LoRA"}</option>`;
  sel.disabled = !options.length;
  $("#imgLoraAdd").disabled = !options.length;
}

// 被排除的 LoRA：显示名称 + 排除原因（让用户知道为什么少了它）
function renderExcludedLoras() {
  const box = $("#imgLoraExcluded");
  if (!box) return;
  const items = IMG_LORAS.incompatible || [];
  if (!items.length) {
    box.innerHTML = "";
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.innerHTML = `<div class="img-lora-excluded-title">已排除（与 Z-Image 底模不兼容）</div>` +
    items.map(it => `<div class="img-lora-excluded-item" title="${esc(it.incompatible_reason || "")}">
        <span class="img-lora-excluded-name">${esc(it.display)}</span>
        <span class="img-lora-excluded-reason">${esc(it.incompatible_reason || "兼容性检测未通过")}</span>
      </div>`).join("");
}

function renderLoraList() {
  const box = $("#imgLoraList");
  if (!box) return;
  renderExcludedLoras();
  if (!state.img.loras.length) {
    box.innerHTML = "";
    const accel = (IMG_LORAS.acceleration || []).length;
    const bad = (IMG_LORAS.incompatible || []).length;
    $("#imgLoraNote").textContent = IMG_LORAS.available.length
      ? `未添加效果 LoRA。可选本机已安装的效果类 Z-Image LoRA（${IMG_LORAS.available.length} 个）` +
        (accel ? `；另有 ${accel} 个加速/蒸馏类 LoRA 由工作流固定使用，不在此列表。` : "。") +
        (bad ? ` ${bad} 个 LoRA 因与底模不兼容被排除（见下方说明）。` : "") +
        " 强度 0 表示关闭。"
      : "本机未检测到可用的效果类 Z-Image LoRA 文件。";
    return;
  }
  box.innerHTML = state.img.loras.map((item, index) => {
    const meta = imgLoraMeta(item.name);
    const missing = !meta && !item.excluded;
    // 被排除（不兼容）—— 明确标出原因，避免用户以为能正常用
    const excluded = !!item.excluded;
    const rec = meta && Number(item.strength) === Number(meta.recommended_strength);
    const disabled = item.enabled === false || Number(item.strength) <= 0 || excluded;
    // 强度上限：权重过大的 LoRA 必须封顶（实测 1.0 出噪点、0.8 安全）
    const cap = Number(meta && meta.strength_cap !== undefined
                       ? meta.strength_cap
                       : (item.strength_cap !== undefined ? item.strength_cap : 1.5));
    const capVal = cap < 1.5 ? cap : 1.5;
    // context_refiner 阻尼：服务端会自动用阻尼后的派生 LoRA，
    // 强度可以保持 1.0（这是根因修复，比全局降强度更稳）
    const damp = meta && meta.ctx_damping !== undefined && meta.ctx_damping !== null
      ? Number(meta.ctx_damping) : 0;
    const ctxPert = meta && meta.ctx_perturbation !== undefined && meta.ctx_perturbation !== null
      ? Number(meta.ctx_perturbation) : 0;
    const dampNote = damp > 0
      ? `<span class="img-lora-tag damp" title="该 LoRA 对 context_refiner 的权重扰动较大（${(ctxPert * 100).toFixed(1)}%），该模块联合处理文本条件与潜变量，长提示词下会把采样推到发散。服务端已自动对 context_refiner 做 ${damp.toFixed(2)} 阻尼，强度可保持 1.0">已阻尼 ×${damp.toFixed(2)}</span>`
      : "";
    // 触发词：可编辑 —— 用户能改 / 删 / 手输；留空即不追加该 LoRA 的触发词
    const triggerText = item.trigger !== undefined && item.trigger !== null
      ? item.trigger : (meta && meta.trigger ? meta.trigger : "");
    const triggerBlock = `
      <div class="img-lora-trigger">
        <span class="lora-trigger-label" title="该 LoRA 贡献的触发词，会自动追加到提示词末尾">触发词</span>
        <input type="text" class="lora-trigger-input" value="${esc(triggerText)}"
               placeholder="${meta && meta.trigger ? esc(meta.trigger) : "（上游未标注触发词）"}"
               title="自动追加到最终提示词；可编辑或删除">
      </div>`;
    return `<div class="img-lora-item ${disabled ? "disabled" : ""} ${missing ? "missing" : ""} ${excluded ? "excluded" : ""}" data-idx="${index}">
      <div class="img-lora-head">
        <button type="button" class="img-lora-btn toggle" title="${excluded ? "该 LoRA 与底模不兼容，不能使用" : (disabled ? "启用" : "停用")}" ${excluded ? "disabled" : ""}>${disabled ? "○" : "●"}</button>
        <span class="img-lora-name" title="${esc(item.name)}">${esc(meta ? meta.display : (item.display || item.name))}</span>
        ${excluded ? `<span class="img-lora-tag excl">不兼容</span>` : ""}
        ${missing ? `<span class="img-lora-tag miss">文件缺失</span>` : ""}
        ${rec ? `<span class="img-lora-tag rec">推荐强度</span>` : ""}
        ${dampNote}
        <button type="button" class="img-lora-btn up" title="上移" ${index === 0 ? "disabled" : ""}>↑</button>
        <button type="button" class="img-lora-btn down" title="下移" ${index === state.img.loras.length - 1 ? "disabled" : ""}>↓</button>
        <button type="button" class="img-lora-btn del" title="删除">×</button>
      </div>
      <div class="img-lora-slider">
        <input type="range" class="strength" min="0" max="${capVal}" step="0.05" value="${Math.min(Number(item.strength), capVal)}" ${excluded ? "disabled" : ""} title="${cap < 1.0 ? `强度上限 ${cap}（该 LoRA 权重较大，强度过高会出彩色噪点）` : '强度 0-1.5，0 表示关闭'}">
        <span class="img-lora-strength">${Number(item.strength).toFixed(2)}</span>
        ${cap < 1.0 ? `<span class="img-lora-cap" title="该 LoRA 权重相对原权重过大，强度过高会破坏采样、实测出图为彩色噪点">上限 ${cap.toFixed(2)}</span>` : ""}
        ${damp > 0 ? `<span class="img-lora-cap damped" title="context_refiner 阻尼系数 ${damp.toFixed(2)}（服务端自动应用，强度不打折）">ctx 阻尼 ${damp.toFixed(2)}</span>` : ""}
      </div>
      ${excluded ? `<div class="img-lora-excl-reason">${esc(item.incompatible_reason || "与 Z-Image 底模不兼容")}</div>` : ""}
      ${triggerBlock}
      <div class="img-lora-meta">基础：${esc(meta ? meta.base : "—")}${meta && meta.strength_source ? " · " + esc(meta.strength_source) : ""}</div>
      ${damp > 0 ? `<div class="img-lora-meta">context_refiner 扰动 ${(ctxPert * 100).toFixed(1)}%，已自动阻尼至 ×${damp.toFixed(2)}（长提示词下避免彩色噪点，强度不打折）</div>` : ""}
      <div class="img-lora-meta"><button type="button" class="img-lora-info-btn" style="background:none;border:0;padding:0;color:var(--accent);cursor:pointer;font-size:10px;font-family:inherit">查看简易说明 ›</button></div>
    </div>`;
  }).join("");

  box.querySelectorAll(".img-lora-item").forEach(el => {
    const index = Number(el.dataset.idx);
    const item = state.img.loras[index];
    el.querySelector(".toggle").addEventListener("click", () => {
      item.enabled = item.enabled === false;
      saveLoraConfig();
    });
    el.querySelector(".up").addEventListener("click", () => {
      if (index === 0) return;
      [state.img.loras[index - 1], state.img.loras[index]] = [state.img.loras[index], state.img.loras[index - 1]];
      saveLoraConfig();
    });
    el.querySelector(".down").addEventListener("click", () => {
      if (index === state.img.loras.length - 1) return;
      [state.img.loras[index + 1], state.img.loras[index]] = [state.img.loras[index], state.img.loras[index + 1]];
      saveLoraConfig();
    });
    el.querySelector(".del").addEventListener("click", () => {
      state.img.loras.splice(index, 1);
      saveLoraConfig();
    });
    const slider = el.querySelector("input.strength");
    slider.addEventListener("input", () => {
      item.strength = Number(slider.value);
      el.querySelector(".img-lora-strength").textContent = item.strength.toFixed(2);
      el.classList.toggle("disabled", item.enabled === false || item.strength <= 0);
    });
    slider.addEventListener("change", () => saveLoraConfig());
    // 触发词可编辑：改动即写回 state（保存时发给服务端 + 生成时进提示词）
    const trigInput = el.querySelector("input.lora-trigger-input");
    if (trigInput) {
      trigInput.addEventListener("input", () => {
        item.trigger = trigInput.value;
      });
      trigInput.addEventListener("change", () => saveLoraConfig());
    }
    el.querySelector(".img-lora-info-btn").addEventListener("click", () => openLoraInfo(item.name));
  });
  const accelNote = (IMG_LORAS.acceleration || []).length
    ? ` 加速/蒸馏类 ${IMG_LORAS.acceleration.length} 个由工作流固定使用（不列出）。` : "";
  const badNote = (IMG_LORAS.incompatible || []).length
    ? ` ${IMG_LORAS.incompatible.length} 个不兼容 LoRA 已排除。` : "";
  $("#imgLoraNote").textContent =
    `已添加 ${state.img.loras.length} 个效果 LoRA（配置存在服务端，局域网共用）。${accelNote}${badNote}`;
}

// LoRA 配置持久化到服务端
let loraSaveTimer = null;
function saveLoraConfig() {
  renderLoraList();
  renderLoraSelect();
  clearTimeout(loraSaveTimer);
  loraSaveTimer = setTimeout(async () => {
    try {
      const resp = await fetch("/api/image-loras", {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({loras: state.img.loras}),
      });
      const data = await resp.json();
      if (!resp.ok) {
        // 不兼容的 LoRA 会被服务端拒绝 —— 必须让用户看到原因
        setEnhanceHint(`LoRA 配置保存失败：${data.error || resp.status}`, "error");
        alert(`LoRA 配置保存失败\n\n${data.error || resp.status}`);
        // 回滚到服务端实际保存的状态，避免界面与服务端不一致
        await loadImageLoras();
        return;
      }
      state.img.loras = (data.loras || []).map(l => ({
        name: l.name, strength: Number(l.strength), enabled: l.enabled !== false,
        trigger: l.trigger !== undefined && l.trigger !== null ? l.trigger : "",
        excluded: !!l.excluded,
        incompatible_reason: l.incompatible_reason || "",
        display: l.display || l.name,
        strength_cap: l.strength_cap !== undefined ? l.strength_cap : 1.5,
        ctx_damping: l.ctx_damping !== undefined ? l.ctx_damping : null,
        ctx_perturbation: l.ctx_perturbation !== undefined ? l.ctx_perturbation : null,
      }));
    renderLoraList();
      renderLoraSelect();
    } catch (e) {
      setEnhanceHint(`LoRA 配置保存失败：${e.message}`, "error");
    }
  }, 250);
}

function openLoraInfo(name) {
  const meta = imgLoraMeta(name);
  const body = $("#loraInfoBody");
  if (!meta) {
    body.innerHTML = `<div class="lora-scene">本机未找到该 LoRA 文件：<b>${esc(name)}</b>。请检查文件是否仍在原路径。</div>`;
  } else {
    const trigger = meta.trigger
      ? `<span class="lora-trigger">${esc(meta.trigger)}</span>` : "上游未标注触发词";
    body.innerHTML = `<dl>
      <dt>显示名</dt><dd>${esc(meta.display)}</dd>
      <dt>文件名</dt><dd>${esc(meta.name)}</dd>
      <dt>文件路径</dt><dd>${esc(meta.path)}</dd>
      <dt>大小</dt><dd>${meta.size_mb} MB</dd>
      <dt>基础模型</dt><dd>${esc(meta.base)}</dd>
      <dt>触发词</dt><dd>${trigger}</dd>
      <dt>推荐强度</dt><dd>${Number(meta.recommended_strength).toFixed(2)}${meta.strength_source ? "（" + esc(meta.strength_source) + "）" : ""}</dd>
      <dt>类型</dt><dd>${meta.kind === "acceleration" ? "加速 / 蒸馏类（工作流固定使用，不可选）" : "效果类（可自行选用）"}</dd>
      <dt>说明来源</dt><dd>${esc(meta.source || "—")}</dd>
    </dl>
    <div class="lora-scene">${esc(meta.scene || "（上游未提供场景说明）")}</div>`;
  }
  $("#loraInfoModal").hidden = false;
  document.body.classList.add("modal-open");
}

function closeLoraInfo() {
  $("#loraInfoModal").hidden = true;
  document.body.classList.remove("modal-open");
}

// 载入本机 LoRA 清单 + 已保存配置
async function loadImageLoras() {
  try {
    const resp = await fetch("/api/image-loras", {cache: "no-store"});
    if (!resp.ok) return;
    const data = await resp.json();
    IMG_LORAS.available = data.available || [];
    IMG_LORAS.acceleration = data.acceleration || [];
    IMG_LORAS.incompatible = data.incompatible || [];
    state.img.loras = (data.config || []).map(l => ({
      name: l.name, strength: Number(l.strength), enabled: l.enabled !== false,
      trigger: l.trigger !== undefined && l.trigger !== null ? l.trigger : "",
      // 被排除的（不兼容）也要显示出来并说明原因
      excluded: !!l.excluded,
      incompatible_reason: l.incompatible_reason || "",
      display: l.display || l.name,
      strength_cap: l.strength_cap !== undefined ? l.strength_cap : 1.5,
      ctx_damping: l.ctx_damping !== undefined ? l.ctx_damping : null,
      ctx_perturbation: l.ctx_perturbation !== undefined ? l.ctx_perturbation : null,
    }));
    const hint = $("#imgLoraHint");
    if (hint) hint.textContent = `本机 ${IMG_LORAS.available.length} 个可用`;
    renderLoraSelect();
    renderLoraList();
  } catch (e) { /* 静默：不阻断出图 */ }
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

  renderBatchChips();
  renderLoraSelect();
  renderLoraList();

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
  // 模型换了 -> 增强模板按新模型的模型族重新过滤
  if (typeof refreshEnhanceSpecForMode === "function") refreshEnhanceSpecForMode();
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
  // 视频模型换了 -> 增强模板按新模型的模型族重新过滤
  if (typeof refreshEnhanceSpecForMode === "function") refreshEnhanceSpecForMode();
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

// 提示词版本谱系：原始 / 一次增强 / 二次增强
//   versions[] 保留每一级的文本与产生方式，可查看、可回退、可再增强
const promptVersions = {
  // [{level: 0|1|2, label, text, source, specId, at}]
  versions: [],
  // 二次增强的作用对象：0=原始 / 1=一次增强
  refineTarget: 0,
  busy: false,
};

const PROMPT_LEVEL_LABEL = {0: "原始", 1: "一次增强", 2: "二次增强"};

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
  if (!badge) return;
  const level = currentPromptLevel();
  badge.hidden = level === 0;
  if (level > 0) {
    badge.textContent = PROMPT_LEVEL_LABEL[level] || "已增强";
    badge.classList.toggle("lv2", level === 2);
  }
}

// 当前提示词处于第几级增强（0=原始）
function currentPromptLevel() {
  const current = $("#prompt").value;
  let level = 0;
  for (const v of promptVersions.versions) {
    if (v.text === current) level = Math.max(level, v.level);
  }
  // 手动编辑过 -> 视为回到原始级
  if (promptEnhance.text !== null && promptEnhance.text !== current) level = 0;
  if (promptVersions.busy) return level;
  return level;
}

// 记录一个版本
function pushPromptVersion(level, text, source, specId) {
  const trimmed = (text || "").trim();
  if (!trimmed) return;
  const existing = promptVersions.versions.findIndex(v => v.level === level && v.text === trimmed);
  const entry = {level, label: PROMPT_LEVEL_LABEL[level] || `第${level}级`,
                 text: trimmed, source: source || "", specId: specId || "",
                 at: Date.now()};
  if (existing >= 0) promptVersions.versions[existing] = entry;
  else promptVersions.versions.push(entry);
  renderPromptVersions();
}

// 恢复某个版本到输入框
function restorePromptVersion(index) {
  const v = promptVersions.versions[index];
  if (!v) return;
  $("#prompt").value = v.text;
  // 恢复到该版本 = 该版本已被「采用」，标记为已增强（level>0）
  if (v.level > 0) markPromptEnhanced();
  else clearPromptEnhanced();
  updatePromptBadge();
  closePromptVersionModal();
}

// 版本历史弹窗
function openPromptVersionModal() {
  const box = $("#promptVersionList");
  if (!box) return;
  if (!promptVersions.versions.length) {
    box.innerHTML = `<div class="version-empty">还没有增强记录。先做一次「增强」或「二次增强」。</div>`;
  } else {
    const current = $("#prompt").value;
    box.innerHTML = promptVersions.versions.map((v, i) => `
      <div class="version-item ${v.text === current ? "current" : ""}">
        <div class="version-head">
          <span class="version-badge lv${v.level}">${esc(v.label)}</span>
          ${v.source ? `<span class="version-source">${esc(v.source)}</span>` : ""}
          ${v.text === current ? `<span class="version-current">当前</span>` : ""}
        </div>
        <div class="version-text">${esc(v.text.slice(0, 400))}${v.text.length > 400 ? "…" : ""}</div>
        <div class="version-actions">
          <button type="button" data-restore="${i}">恢复到输入框</button>
        </div>
      </div>`).join("");
    box.querySelectorAll("[data-restore]").forEach(btn => {
      btn.addEventListener("click", () => restorePromptVersion(Number(btn.dataset.restore)));
    });
  }
  $("#promptVersionModal").hidden = false;
  document.body.classList.add("modal-open");
}

function closePromptVersionModal() {
  const modal = $("#promptVersionModal");
  if (!modal || modal.hidden) return;
  modal.hidden = true;
  document.body.classList.remove("modal-open");
}

// 二次增强作用对象（原始 / 一次增强）
function renderRefineTargets() {
  const box = $("#refineTargetChips");
  if (!box) return;
  const hasL1 = promptVersions.versions.some(v => v.level === 1);
  const options = [
    {level: 0, label: "原始提示词", enabled: true},
    {level: 1, label: "一次增强后", enabled: hasL1},
  ];
  // 目标级别被删掉后回落
  if (!options.some(o => o.level === promptVersions.refineTarget && o.enabled)) {
    promptVersions.refineTarget = 0;
  }
  box.innerHTML = options.map(o =>
    `<button type="button" data-level="${o.level}" class="${promptVersions.refineTarget === o.level ? "active" : ""}" ${o.enabled ? "" : 'disabled title="还没有一次增强的结果"'}>${esc(o.label)}</button>`
  ).join("");
  box.querySelectorAll("button").forEach(btn => {
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      promptVersions.refineTarget = Number(btn.dataset.level);
      renderRefineTargets();
    });
  });
}

function renderPromptVersions() {
  updatePromptBadge();
  renderRefineTargets();
}

// 手动编辑提示词 -> 增强标记立即失效
$("#prompt").addEventListener("input", () => {
  promptEnhance.text = null;
  updatePromptBadge();
});

// 增强中 / 生成中互斥：避免并发重复提交
// 注意：一次增强与二次增强期间，生成按钮都必须禁用。
function updateActionButtons() {
  const enhanceBusy = promptEnhance.busy || promptVersions.busy;
  const busy = enhanceBusy || state.genBusy;
  const genBtn = $("#genBtn");
  const enhBtn = $("#enhanceBtn");
  const enhText = $("#enhanceBtnText");
  const refBtn = $("#refineBtn");
  const refText = $("#refineBtnText");
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
  if (refBtn) {
    refBtn.disabled = busy;
    refBtn.classList.toggle("busy", promptVersions.busy);
  }
  if (refText) refText.textContent = promptVersions.busy ? "正在细化…" : "二次增强";
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
  if (mode === "t2i" && currentPromptLevel() === 0) {
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
    // 生成批次：一次提交 N 张（服务端拆成 N 个独立批次）
    params.batch_count = state.img.batchCount;
    // LoRA：只提交启用且强度 > 0 的（服务端会再校验文件是否存在）
    // trigger 一起带上 —— 服务端据此把触发词追加进最终提示词
    params.loras = state.img.loras
      .filter(l => l.enabled !== false && Number(l.strength) > 0)
      .map(l => ({
        name: l.name,
        strength: Number(l.strength),
        trigger: l.trigger !== undefined && l.trigger !== null ? l.trigger : "",
      }));
  }
  fd.append("params", JSON.stringify(params));
  try {
    // 素材库引用：槽位 -> asset_id，随任务一起提交（服务端按槽位上传到 ComfyUI）
    const assetRefs = [];
    const SLOT_FOR_KEY = {
      first: "first_frame", last: "last_frame",
      ref0: "ref_image", ref1: "ref_image", ref2: "ref_image",
    };
    Object.entries(state.assetRefs || {}).forEach(([key, item], index) => {
      if (!item || !item.asset_id) return;
      assetRefs.push({
        asset_id: item.asset_id,
        slot: SLOT_FOR_KEY[key] || "ref_image",
        ordinal: index,
      });
    });
    if (assetRefs.length) fd.append("asset_refs", JSON.stringify(assetRefs));

    if (mode === "flf") {
      const hasFirst = state.files.first || (state.assetRefs && state.assetRefs.first);
      if (!hasFirst) { alert("首尾帧模式需要上传首帧图片"); return; }
      if (state.files.first) fd.append("file_first", state.files.first);
      if (state.files.last) fd.append("file_last", state.files.last);
    } else if (mode === "r2v") {
      const refs = [state.files.ref0, state.files.ref1, state.files.ref2].filter(Boolean);
      const assetCount = Object.keys(state.assetRefs || {}).filter(k => k.startsWith("ref")).length;
      if (!refs.length && !assetCount) { alert("参考模式至少需要 1 张参考图"); return; }
      refs.forEach(f => fd.append("file_ref", f));
    }
    const resp = await fetch("/api/jobs", { method: "POST", body: fd });
    const data = await resp.json();
    if (!resp.ok) { alert(data.error || "提交失败"); return; }
    trackJob(data.id);
    // 清空文件选择（保留提示词便于连做）
    state.files = { first: null, last: null, refs: [] };
    state.assetRefs = {};
    renderMode();
    state.tab = "active";
    renderTabs();
    refreshJobs();
  } catch (e) {
    alert("提交失败: " + e.message);
  }
});

// ---------- 任务列表 ----------
// ---------- 生成类型标签（服务端下发，可与状态标签叠加） ----------
function renderGenTypeTabs() {
  const box = $("#genTypeTabs");
  if (!box) return;
  const label = box.querySelector(".tabs-label");
  const buttons = [
    `<button data-type="" class="${state.genType ? "" : "active"}">全部类型</button>`,
    ...GEN_TYPES.types.map(t =>
      `<button data-type="${esc(t.key)}" class="${state.genType === t.key ? "active" : ""}" title="${esc(t.label)}">${esc(t.label)}</button>`
    ),
  ];
  box.innerHTML = "";
  if (label) box.appendChild(label);
  buttons.forEach(html => {
    const wrap = document.createElement("span");
    wrap.innerHTML = html;
    box.appendChild(wrap.firstElementChild);
  });
  box.querySelectorAll("button").forEach(btn => {
    btn.addEventListener("click", () => {
      state.genType = btn.dataset.type || "";
      renderGenTypeTabs();
      refreshJobs();
    });
  });
}

async function loadGenerationTypes() {
  try {
    const resp = await fetch("/api/generation-types", {cache: "no-store"});
    if (!resp.ok) return;
    const data = await resp.json();
    GEN_TYPES.types = data.types || [];
    GEN_TYPES.registeredModes = data.registered_modes || [];
    renderGenTypeTabs();
  } catch (e) { /* 静默：不阻断任务列表 */ }
}

// 状态标签组：只取带 data-st 的按钮
// 注意：类型标签组也带 .jobs-tabs class，不能用 `.jobs-tabs button`，
// 否则 renderTabs 会把类型标签的 active 状态一起清掉。
function renderTabs() {
  $$(".jobs-tabs button[data-st]").forEach(b =>
    b.classList.toggle("active", b.dataset.st === state.tab));
  $("#doneFilters").hidden = !["done", "all"].includes(state.tab);
}
$$(".jobs-tabs button[data-st]").forEach(b => b.addEventListener("click", () => {
  state.tab = b.dataset.st;
  if (["done", "longfilm", "all"].includes(state.tab)) state.donePage = 1;
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

// ---------- 生成结果图片查看器（lightbox） ----------
// 纯前端 + 现有 /files 路由，局域网所有客户端一致生效，历史图片同样适用。
const LIGHTBOX_MIN_SCALE = 0.05;
const LIGHTBOX_MAX_SCALE = 16;

const lightbox = {
  open: false,
  src: "",
  name: "",
  naturalWidth: 0,
  naturalHeight: 0,
  scale: 1,
  fitScale: 1,
  dragging: false,
  dragX: 0,
  dragY: 0,
  scrollX: 0,
  scrollY: 0,
};

// 按原始尺寸显示，但不超出视口；即：先适应屏幕，超过原始尺寸时不再放大
function lightboxComputeFitScale() {
  const stage = $("#lightboxStage");
  const pad = 36;
  const availW = Math.max(1, stage.clientWidth - pad);
  const availH = Math.max(1, stage.clientHeight - pad);
  const w = lightbox.naturalWidth || 1;
  const h = lightbox.naturalHeight || 1;
  return Math.min(availW / w, availH / h, 1);
}

function lightboxApplyScale(animate) {
  const img = $("#lightboxImage");
  if (animate) {
    img.classList.add("zooming");
    requestAnimationFrame(() => requestAnimationFrame(() => img.classList.remove("zooming")));
  }
  // 直接改 width/height（而不是 transform）：布局盒与视觉盒一致，
  // 滚动区域正确，且缩放后图片空白处的点击不会误判为「点遮罩」
  const w = Math.max(1, Math.round(lightbox.naturalWidth * lightbox.scale));
  const h = Math.max(1, Math.round(lightbox.naturalHeight * lightbox.scale));
  img.style.width = `${w}px`;
  img.style.height = `${h}px`;
  $("#lightboxZoom").textContent = `${Math.round(lightbox.scale * 100)}%`;
  $("#lightboxZoomOut").disabled = lightbox.scale <= LIGHTBOX_MIN_SCALE + 1e-6;
  $("#lightboxZoomIn").disabled = lightbox.scale >= LIGHTBOX_MAX_SCALE - 1e-6;
}

function lightboxSetScale(next, anchorClientX, anchorClientY) {
  const stage = $("#lightboxStage");
  const prev = lightbox.scale;
  const clamped = Math.min(LIGHTBOX_MAX_SCALE, Math.max(LIGHTBOX_MIN_SCALE, next));
  if (clamped === prev) return;

  // 以鼠标/按键为锚点缩放：保持锚点下的图内容位置不变
  if (anchorClientX != null && anchorClientY != null) {
    const rect = stage.getBoundingClientRect();
    const img = $("#lightboxImage");
    // 锚点在图片坐标系里的位置（缩放前后保持不变 -> 鼠标下的内容不跳动）
    const px = anchorClientX - rect.left - img.offsetLeft;
    const py = anchorClientY - rect.top - img.offsetTop;
    const ratio = clamped / prev;
    lightbox.scale = clamped;
    lightboxApplyScale(false);
    stage.scrollLeft = px * ratio + img.offsetLeft - (anchorClientX - rect.left);
    stage.scrollTop = py * ratio + img.offsetTop - (anchorClientY - rect.top);
  } else {
    lightbox.scale = clamped;
    lightboxApplyScale(true);
  }
}

function lightboxFit() {
  lightbox.scale = lightboxComputeFitScale();
  lightboxApplyScale(true);
  const stage = $("#lightboxStage");
  stage.scrollLeft = 0;
  stage.scrollTop = 0;
}

function lightboxActual() {
  lightbox.scale = 1;
  lightboxApplyScale(true);
}

function lightboxClose() {
  if (!lightbox.open) return;
  lightbox.open = false;
  $("#lightbox").hidden = true;
  document.body.classList.remove("modal-open");
  $("#lightboxImage").removeAttribute("src");
}

function lightboxShow(src, name, naturalWidth, naturalHeight) {
  lightbox.open = true;
  lightbox.src = src;
  lightbox.name = name || "图片";
  lightbox.naturalWidth = naturalWidth || 0;
  lightbox.naturalHeight = naturalHeight || 0;
  const modal = $("#lightbox");
  modal.hidden = false;
  document.body.classList.add("modal-open");
  $("#lightboxTitle").textContent = lightbox.name;
  $("#lightboxDownload").href = src;
  $("#lightboxDownload").setAttribute("download", lightbox.name);
  $("#lightboxOpenNew").href = src;
  const img = $("#lightboxImage");
  img.src = src;
  // 图片尺寸就绪后再按原始尺寸铺开
  const onLoad = () => {
    if (!lightbox.open) return;
    lightbox.naturalWidth = img.naturalWidth || naturalWidth || 0;
    lightbox.naturalHeight = img.naturalHeight || naturalHeight || 0;
    $("#lightboxDim").textContent = lightbox.naturalWidth && lightbox.naturalHeight
      ? `${lightbox.naturalWidth}×${lightbox.naturalHeight}` : "";
    lightboxFit();
  };
  if (img.complete && img.naturalWidth) onLoad();
  else img.addEventListener("load", onLoad, {once: true});
  img.removeEventListener("error", onLoad, {once: true});
}

$("#lightboxClose").addEventListener("click", lightboxClose);
$("#lightboxZoomIn").addEventListener("click", () => lightboxSetScale(lightbox.scale * 1.25));
$("#lightboxZoomOut").addEventListener("click", () => lightboxSetScale(lightbox.scale / 1.25));
$("#lightboxFit").addEventListener("click", lightboxFit);
$("#lightboxActual").addEventListener("click", lightboxActual);

// 点击遮罩关闭：只认「点到 lightbox 根或 stage 空白处」，
// 点到图片本身（含缩放后的空白）不关
$("#lightbox").addEventListener("click", event => {
  if (event.target === $("#lightbox") || event.target === $("#lightboxStage")) lightboxClose();
});

// 滚轮缩放
$("#lightboxStage").addEventListener("wheel", event => {
  if (!lightbox.open) return;
  event.preventDefault();
  const factor = event.deltaY < 0 ? 1.15 : 1 / 1.15;
  lightboxSetScale(lightbox.scale * factor, event.clientX, event.clientY);
}, {passive: false});

// 拖拽平移（图片大于视口时）
(() => {
  const stage = $("#lightboxStage");
  stage.addEventListener("mousedown", event => {
    if (!lightbox.open) return;
    if (event.button !== 0) return;
    lightbox.dragging = true;
    lightbox.dragX = event.clientX;
    lightbox.dragY = event.clientY;
    lightbox.scrollX = stage.scrollLeft;
    lightbox.scrollY = stage.scrollTop;
    stage.classList.add("dragging");
    event.preventDefault();
  });
  window.addEventListener("mousemove", event => {
    if (!lightbox.dragging) return;
    stage.scrollLeft = lightbox.scrollX - (event.clientX - lightbox.dragX);
    stage.scrollTop = lightbox.scrollY - (event.clientY - lightbox.dragY);
  });
  window.addEventListener("mouseup", () => {
    lightbox.dragging = false;
    stage.classList.remove("dragging");
  });
})();

// 键盘：Esc 关闭，+/- 缩放，0 适应屏幕，1 原始尺寸，方向键平移
document.addEventListener("keydown", event => {
  if (!lightbox.open) return;
  const stage = $("#lightboxStage");
  switch (event.key) {
    case "Escape": lightboxClose(); event.preventDefault(); break;
    case "+": case "=": lightboxSetScale(lightbox.scale * 1.25); event.preventDefault(); break;
    case "-": case "_": lightboxSetScale(lightbox.scale / 1.25); event.preventDefault(); break;
    case "0": lightboxFit(); event.preventDefault(); break;
    case "1": lightboxActual(); event.preventDefault(); break;
    case "ArrowLeft": stage.scrollLeft -= 60; event.preventDefault(); break;
    case "ArrowRight": stage.scrollLeft += 60; event.preventDefault(); break;
    case "ArrowUp": stage.scrollTop -= 60; event.preventDefault(); break;
    case "ArrowDown": stage.scrollTop += 60; event.preventDefault(); break;
    default: break;
  }
});

window.addEventListener("resize", () => {
  if (lightbox.open) lightboxFit();
});

// 单条任务内的批次结果：可展开/折叠的缩略图网格 + 每批次状态
// 折叠状态按 job.id 记忆，卡片被重建（实时刷新 / 批次状态变化）后依然保持。
const expandedBatches = new Set();

// 折叠三角朝向：展开 ▼ / 收纳 ▶。用显式字形而不是 CSS 旋转，
// 这样朝向和状态永远一一对应，读屏软件也能读出正确方向。
function batchCaret(open) {
  return open ? "▼" : "▶";
}

// 把折叠状态落到 DOM：标题行 class/aria + 网格 hidden
function applyBatchesState(card, open) {
  const btn = card.querySelector('[data-act="toggle-batches"]');
  if (btn) {
    btn.classList.toggle("open", open);
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    const caret = btn.querySelector(".caret");
    if (caret) caret.textContent = batchCaret(open);
  }
  const grid = card.querySelector("[data-batches]");
  if (grid) grid.hidden = !open;
}

function renderJobBatches(job) {
  const batches = job.batches || [];
  if (!batches.length) return "";
  const p = job.params || {};
  const doneCount = batches.filter(b => b.status === "done").length;
  const failedCount = batches.filter(b => b.status === "failed").length;
  const open = expandedBatches.has(job.id);
  const summary = failedCount
    ? `${doneCount}/${batches.length} 成功 · ${failedCount} 失败`
    : `${doneCount}/${batches.length} 完成`;
  const cells = batches.map(batch => {
    const out = (batch.outputs || []).find(o => o.kind === "image");
    const statusText = {done: "完成", failed: "失败", running: "生成中",
                        queued: "排队中", canceled: "已取消"}[batch.status] || batch.status;
    // 批次失败时即使残留了旧产物也不展示图 —— 状态优先，避免「失败却显示成图」
    const failed = batch.status === "failed";
    const thumb = out && !failed
      ? `<div class="job-batch-thumb"><img src="${outputUrl(job, out)}" alt="批次 ${batch.idx + 1}"><span class="zoom-hint">⤢</span></div>`
      : `<div class="job-batch-thumb ${failed ? "failed" : ""}">${
          failed ? (batch.error || "失败").slice(0, 60)
          : batch.status === "running" ? `${Math.round((batch.progress || 0) * 100)}%`
          : batch.status === "canceled" ? "已取消"
          : "排队中"}</div>`;
    return `<div class="job-batch" data-batch="${batch.id}">
      ${thumb}
      <div class="job-batch-foot">
        <span class="st ${batch.status}">#${batch.idx + 1} ${statusText}</span>
        <span class="seed" title="该批次种子">${batch.seed ?? "—"}</span>
      </div>
    </div>`;
  }).join("");
  // 标题行摘要：完成/失败数 + 出图尺寸 —— 折叠时也能一眼看出里面是什么
  const size = p.width && p.height ? ` · ${p.width}×${p.height}` : "";
  return `<button type="button" class="job-batches-toggle ${open ? "open" : ""}" data-act="toggle-batches" aria-expanded="${open ? "true" : "false"}">
      <span class="caret">${batchCaret(open)}</span> 批次结果（${summary}${size}）
    </button>
    <div class="job-batches" data-batches ${open ? "" : "hidden"}>${cells}</div>`;
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
    preview = `<img src="${outputUrl(job, image)}">
      <span class="zoom-hint" title="点击查看大图"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M7 2H2v5M9 2h5v5M9 14h5V9M7 14H2V9" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg></span>`;
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
    ${renderJobBatches(job)}
    <div class="job-actions">
      ${video || image ? `<button data-act="download">⬇ 下载</button>` : ""}
      ${image ? `<a data-act="open-new" href="${outputUrl(job, image)}" target="_blank" rel="noopener" title="在新窗口打开原图">↗ 新窗口打开</a>` : ""}
      ${video && job.status === "done" && state.page === "stitch" ? `<button data-act="add-stitch">＋ 拼接</button>` : ""}
      ${job.mode !== "longfilm" ? `<button data-act="reuse">复用参数</button>` : ""}
      ${!runningOrQueued ? `<button data-act="delete" class="danger">删除</button>` : ""}
    </div>`;

  if (image) {
    const previewBox = card.querySelector(".job-preview");
    previewBox.classList.add("has-image");
    const dragImage = card.querySelector(".job-preview img");
    const source = {url: outputUrl(job, image), name: String(image.file || "completed-image.png").split(/[\\/]/).pop()};
    dragImage.draggable = true;
    dragImage.title = "点击查看大图；也可拖到左侧参考图、首帧或尾帧位置";
    // 可聚焦 + 键盘可达：Enter / Space 同样能打开大图
    dragImage.tabIndex = 0;
    dragImage.setAttribute("role", "button");
    dragImage.setAttribute("aria-label", `查看大图：${source.name}`);
    const openLightbox = () =>
      lightboxShow(source.url, source.name, dragImage.naturalWidth, dragImage.naturalHeight);
    // 点击缩略图 -> 打开大图查看器
    dragImage.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
      openLightbox();
    });
    dragImage.addEventListener("keydown", event => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      openLightbox();
    });
    previewBox.addEventListener("click", event => {
      if (event.target !== previewBox) return;
      openLightbox();
    });
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
  // 批次结果展开/折叠（点标题行任意位置或三角都可以）
  card.querySelector('[data-act="toggle-batches"]')?.addEventListener("click", event => {
    event.stopPropagation();
    const open = !expandedBatches.has(job.id);
    if (open) expandedBatches.add(job.id); else expandedBatches.delete(job.id);
    applyBatchesState(card, open);
  });
  // 批次缩略图点击放大
  card.querySelectorAll(".job-batch-thumb img").forEach(el => {
    el.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
      const url = el.getAttribute("src");
      const name = url.split("/").pop();
      lightboxShow(url, name, el.naturalWidth, el.naturalHeight);
    });
  });
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
      if (p.batch_count) s.batchCount = Number(p.batch_count);
      if (Array.isArray(p.loras)) {
        s.loras = p.loras.map(l => ({
          name: l.name,
          strength: Number(l.strength),
          enabled: true,
          // 触发词沿用任务记录里的版本（用户可能改过）
          trigger: l.trigger !== undefined ? l.trigger : "",
        }));
      }
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
  bindMediaContextMenu(card, job);
  return card;
}

// ---------- 媒体库右键菜单：添加到素材库 ----------
// 覆盖两种来源：
//   * 有批次：产物挂在 job_batches 上（批次缩略图）
//   * 无批次：产物直接挂在 job 上（卡片预览图 / 视频）
function bindMediaContextMenu(card, job) {
  const openMenu = (event, payload) => {
    event.preventDefault();
    event.stopPropagation();
    const menu = $("#mediaContext");
    if (!menu) return;
    const label = payload.batch_id
      ? `批次 #${(payload.batch_index ?? 0) + 1} 的结果`
      : "这个结果";
    menu.innerHTML = `
      <button data-act="add-asset">添加到素材库（${label}）</button>
      <div class="ctx-sep"></div>
      <button data-act="download">下载原文件</button>`;
    menu.classList.add("open");
    // 定位到鼠标处，并防止贴边溢出
    menu.style.left = "0px";
    menu.style.top = "0px";
    const rect = menu.getBoundingClientRect();
    menu.style.left = Math.max(4, Math.min(event.clientX, window.innerWidth - rect.width - 8)) + "px";
    menu.style.top = Math.max(4, Math.min(event.clientY, window.innerHeight - rect.height - 8)) + "px";

    menu.querySelectorAll("button[data-act]").forEach(btn => {
      btn.addEventListener("click", async () => {
        menu.classList.remove("open");
        if (btn.dataset.act === "add-asset") {
          if (window.ASSETLIB && window.ASSETLIB.addFromMedia) {
            await window.ASSETLIB.addFromMedia({
              job_id: job.id,
              batch_id: payload.batch_id || null,
              file: payload.file || null,
            });
          } else {
            alert("素材库模块未加载");
          }
        } else if (btn.dataset.act === "download" && payload.file) {
          const a = document.createElement("a");
          a.href = outputUrl(job, { file: payload.file, kind: payload.kind || "image" });
          a.download = String(payload.file).split(/[\\/]/).pop();
          a.click();
        }
      });
    });
  };

  // 无批次：卡片上的图片 / 视频
  const image = (job.outputs || []).find(o => o.kind === "image");
  const video = (job.outputs || []).find(o => o.kind === "video");
  const previewImg = card.querySelector(".job-preview img");
  if (previewImg && image) {
    previewImg.addEventListener("contextmenu", event => openMenu(event, {
      file: image.file, kind: "image", batch_id: null,
    }));
  }
  const previewVideo = card.querySelector(".job-preview video");
  if (previewVideo && video) {
    previewVideo.addEventListener("contextmenu", event => openMenu(event, {
      file: video.file, kind: "video", batch_id: null,
    }));
  }

  // 有批次：批次缩略图
  card.querySelectorAll(".job-batch-thumb img").forEach(el => {
    const batchId = el.closest(".job-batch")?.dataset.batch;
    const batch = (job.batches || []).find(b => b.id === batchId);
    const out = (batch?.outputs || []).find(o => o.kind === "image")
      || (batch?.outputs || [])[0];
    if (!out) return;
    el.addEventListener("contextmenu", event => openMenu(event, {
      file: out.file, kind: out.kind || "image",
      batch_id: batchId, batch_index: batch?.idx ?? 0,
    }));
  });
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

// esc() 的正式定义在 providers.js（脚本顺序在后）。但 app.js 在启动阶段
// (renderPromptVersions -> renderRefineTargets) 就会调用它，若此处没有定义就会抛
// ReferenceError 并中断整个 app.js 顶层脚本（含末尾的 setInterval 注册）。
// 这里给一个与 providers.js 同语义的兜底定义，providers.js 加载后会被它覆盖。
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function jobSig(j) {
  // 批次状态/产物变化时也要重建卡片，否则批次网格不会刷新
  const batchSig = (j.batches || [])
    .map(b => `${b.idx}:${b.status}:${Math.round((b.progress || 0) * 100)}:${(b.outputs || []).length}:${b.error || ""}`)
    .join(",");
  return [j.id, j.status, Math.round((j.progress || 0) * 100), j.progress_text || "",
    (j.outputs || []).map(o => o.file).join(","), batchSig].join("|");
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
  // 「全部」也要分页：否则「全部类型」名义下只显示最新 8 条（可能全是同一类），
  // 其余分类翻不到，与「全部类型」的语义不符。
  if (!["done", "longfilm", "all"].includes(state.tab) || total <= DONE_PAGE_SIZE) {
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
    // 「全部」= 不按状态过滤；其余标签各自过滤
    const statuses = tab === "all" ? ["all"]
      : tab === "active" ? ["queued", "running"]
      : [tab === "longfilm" ? "done" : tab];
    const groups = await Promise.all(statuses.map(async status => {
      const query = new URLSearchParams({status});
      // 生成类型筛选（与状态叠加，服务端按 mode 过滤）
      if (state.genType) query.set("gen_type", state.genType);
      // 「已完成」「全部」都走服务端筛选 + 分页；「全部」不按状态过滤但保留筛选条件
      if (["done", "all"].includes(status)) {
        query.set("page", state.donePage);
        query.set("page_size", DONE_PAGE_SIZE);
        if (tab === "longfilm") query.set("media", "longfilm");
        else Object.entries(state.doneFilters).forEach(([key, value]) => { if (value) query.set(key, value); });
      }
      const response = await fetch("/api/jobs?" + query.toString(), {cache: "no-store"});
      if (!response.ok) throw new Error("HTTP " + response.status);
      const data = await response.json();
      // status=all 时服务端不按状态过滤，前端也不做二次过滤
      const jobs = status === "all" ? data.jobs : data.jobs.filter(job => job.status === status);
      return {jobs, total: data.total ?? jobs.length, page: data.page ?? 1};
    }));
    if (ticket !== jobsRequest || tab !== state.tab) return;
    if (["done", "longfilm", "all"].includes(tab)) {
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
// 状态文案全部中文，并与 ComfyUI 真实队列一致：
//   ComfyUI 在跑（不管是 Studio 提交的还是用户在 ComfyUI 里直接提交的）
//   -> 「生成中」；队列里还有待执行的 -> 「排队中」；
//   全空 -> 「空闲」。ComfyUI 连不上 -> 「ComfyUI 未连接」。
const STATUS_TEXT = {
  offline: "ComfyUI 未连接",
  busy: "生成中",
  queued: "排队中",
  idle: "空闲",
};

// 把状态数据映射成 {text, dot, title}（纯函数，便于单独断言）
function statusLabel(data) {
  const comfyRunning = data.comfy_running;
  const comfyPending = data.comfy_pending;
  const studioRunning = !!data.running;
  const studioQueued = Number(data.queued || 0) > 0;

  // comfy_running 为 null = 读不到 ComfyUI 队列（连接异常）
  const comfyUnknown = comfyRunning === null || comfyRunning === undefined;
  const busy = comfyUnknown ? studioRunning : (comfyRunning > 0 || studioRunning);
  const pending = comfyUnknown ? studioQueued
    : (comfyPending > 0 || studioQueued);

  if (data.comfyui_ok === false) {
    return {text: STATUS_TEXT.offline, dot: "dot dot-off", title: "连不上 ComfyUI"};
  }
  if (busy) {
    const n = Math.max(Number(comfyRunning || 0), 1);
    return {text: STATUS_TEXT.busy, dot: "dot dot-busy",
            title: `ComfyUI 正在生成（${n} 个在执行）`};
  }
  if (pending) {
    const n = Number(data.queued || 0) || Number(comfyPending || 0);
    return {text: n > 0 ? `${STATUS_TEXT.queued}（${n}）` : STATUS_TEXT.queued,
            dot: "dot dot-busy", title: "ComfyUI 队列中有待执行的任务"};
  }
  return {text: STATUS_TEXT.idle, dot: "dot dot-ok", title: "ComfyUI 空闲"};
}

function renderStatus(data) {
  const el = $("#statusText");
  const dot = $("#topStatus .dot");
  if (!el || !dot) return;
  const label = statusLabel(data);
  el.textContent = label.text;
  dot.className = label.dot;
  el.title = label.title;
}

async function refreshSystem() {
  try {
    const r = await (await fetch("/api/system", {cache: "no-store"})).json();
    renderStatus(r);
  } catch (e) { /* 忽略：保留上一次状态 */ }
}

// SSE：状态一变就推过来，不必等轮询
function startStatusStream() {
  if (typeof EventSource === "undefined") return;
  let source;
  try {
    source = new EventSource("/api/status/stream");
  } catch (e) {
    return;
  }
  source.onmessage = event => {
    try {
      const data = JSON.parse(event.data);
      // SSE 帧里没有 comfyui_ok：能收到帧就说明服务端连得上 ComfyUI
      renderStatus(Object.assign({comfyui_ok: true}, data));
    } catch (e) { /* 忽略坏帧 */ }
  };
  source.onerror = () => {
    // 断线由浏览器自动重连；这里不主动 close，避免连不上时刷日志
  };
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
  $("#assetsPage").style.display = "none";
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
  $("#assetsPage").style.display = "none";
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
  $("#assetsPage").style.display = "none";
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
  $("#assetsPage").style.display = "none";
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

// ---------- 显卡功耗上限（Power Limit）----------
// 硬件面板每 3 秒自动刷新会整块重绘，用户在下拉框里选好的目标值必须保留，
// 否则永远点不到「应用」。
const PL_PENDING = {};
// 结果提示同样要跨重绘存活：硬件面板每 3 秒重绘一次，提示写在 DOM 上会被抹掉。
const PL_MSG = {};
// 每张卡一个控件：读出当前值、显示可调范围、下拉选择目标值 + 应用。
// 安全约束：默认只允许下调；提高功耗需要二次确认。
function powerLimitOptions(g) {
  const info = g.power_limit_info || {};
  if (!info.supported) return [];
  const lo = Number(info.min), hi = Number(info.max);
  if (!isFinite(lo) || !isFinite(hi) || hi <= 0) return [];
  const cur = Number(info.current);
  // 步进 5W；小于 100W 的卡用 10W 步进，避免下拉项过长
  const step = hi - lo > 200 ? 5 : 10;
  const opts = [];
  for (let v = hi; v >= lo - 0.001; v -= step) {
    const val = Math.round(v);
    if (opts.length && Math.abs(opts[opts.length - 1].val - val) < 0.001) continue;
    opts.push({ val: val, cur: Math.abs(val - cur) < 0.001 });
  }
  if (!opts.length) opts.push({ val: Math.round(cur), cur: true });
  return opts;
}

function renderPowerLimitControl(g, cap) {
  const info = g.power_limit_info || {};
  const idx = g.index;
  if (!cap || !cap.available) {
    const reason = (cap && cap.reason) || "未检测到可调功耗上限能力";
    return `<div class="sys-pl">
      <div class="sys-pl-head"><span>功耗上限</span><b class="sys-pl-na">不可用</b></div>
      <div class="sys-pl-note">${escapeHtml(reason)}</div>
    </div>`;
  }
  if (!info.supported) {
    const reason = info.reason || "该显卡或当前驱动不支持修改功耗上限";
    return `<div class="sys-pl">
      <div class="sys-pl-head"><span>功耗上限</span><b class="sys-pl-na">不可用</b></div>
      <div class="sys-pl-note">${escapeHtml(reason)}</div>
    </div>`;
  }
  const opts = powerLimitOptions(g);
  const cur = Number(info.current);
  const sel = opts.find(o => o.cur);
  const curVal = sel ? sel.val : Math.round(cur);
  const pending = PL_PENDING[idx];
  const pendingVal = (pending != null && opts.some(o => o.val === pending)) ? pending : null;
  return `<div class="sys-pl" data-gpu-index="${idx}">
    <div class="sys-pl-head"><span>功耗上限</span><b class="sys-pl-cur" data-pl-cur="${idx}">${curVal} W</b></div>
    <div class="sys-pl-range">可调范围 ${Math.round(info.min)} – ${Math.round(info.max)} W（默认 ${Math.round(info.default)} W）</div>
    <div class="sys-pl-ctl">
      <select class="sys-pl-select" data-pl-select="${idx}" aria-label="GPU ${idx} 功耗上限">
        ${opts.map(o => `<option value="${o.val}"${(pendingVal != null ? o.val === pendingVal : o.cur) ? " selected" : ""}>${o.val} W</option>`).join("")}
      </select>
      <button type="button" class="sys-pl-btn" data-pl-apply="${idx}">应用</button>
    </div>
    <div class="sys-pl-msg${PL_MSG[idx] ? " " + PL_MSG[idx].kind : ""}" data-pl-msg="${idx}">${PL_MSG[idx] ? escapeHtml(PL_MSG[idx].text) : ""}</div>
  </div>`;
}

async function applyPowerLimit(index, watts, allowRaise) {
  const curEl = document.querySelector(`[data-pl-cur="${index}"]`);
  // 提示写进状态，随后重绘时由 renderPowerLimitControl 还原
  const setMsg = (text, kind) => {
    PL_MSG[index] = { text: text, kind: kind || "" };
    const el = document.querySelector(`[data-pl-msg="${index}"]`);
    if (el) {
      el.textContent = text;
      el.className = "sys-pl-msg" + (kind ? " " + kind : "");
    }
  };
  try {
    const resp = await fetch("/api/gpu/power-limit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ index: index, watts: watts, allow_raise: !!allowRaise }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.ok) {
      setMsg(data.message || data.error || `设置失败（HTTP ${resp.status}）`, "err");
      return;
    }
    delete PL_PENDING[index];
    if (curEl && data.current != null) curEl.textContent = `${Math.round(data.current)} W`;
    // 回读校验已由后端完成；刷新面板让范围/当前值同步。
    // 提示存在 PL_MSG 里，重绘后仍可见。
    setMsg(data.message || "已生效", "ok");
    await refreshHardware(true);
  } catch (e) {
    setMsg("请求失败：" + (e && e.message ? e.message : e), "err");
  }
}

// 事件委托：下拉框选择变化 → 记住目标值（防止自动刷新重置）
document.addEventListener("change", event => {
  const sel = event.target.closest("[data-pl-select]");
  if (!sel) return;
  const index = Number(sel.getAttribute("data-pl-select"));
  const val = Number(sel.value);
  if (isFinite(val)) {
    PL_PENDING[index] = val;
    delete PL_MSG[index];
  }
});

// 事件委托：应用按钮
document.addEventListener("click", event => {
  const btn = event.target.closest("[data-pl-apply]");
  if (!btn) return;
  const index = Number(btn.getAttribute("data-pl-apply"));
  const sel = document.querySelector(`[data-pl-select="${index}"]`);
  if (!sel) return;
  const watts = Number(sel.value);
  if (!isFinite(watts)) return;
  const curEl = document.querySelector(`[data-pl-cur="${index}"]`);
  const cur = curEl ? parseFloat(String(curEl.textContent)) : NaN;
  if (isFinite(cur) && watts > cur) {
    // 提高功耗上限：必须二次确认（安全约束）
    if (!confirm(`确认提高 GPU ${index} 的功耗上限？\n\n${cur} W → ${watts} W\n\n提高功耗会提升整机发热与供电压力，确认继续？`)) return;
    applyPowerLimit(index, watts, true);
  } else {
    applyPowerLimit(index, watts, false);
  }
});

function renderHardware(d) {
  const grid = $("#sysGrid");
  const cards = [];
  // 功耗上限整体能力（nvidia-smi 缺失 / 驱动不支持时 available=false）
  const plCap = d.power_limit || { available: false, reason: "未返回功耗上限能力信息" };

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
      <div class="sys-row"><span>驱动版本</span><b>${g.driver}</b></div>
      ${renderPowerLimitControl(g, plCap)}`));
  });
  if (!(d.gpus || []).length) {
    cards.push(card("GPU 显卡", "未检测", `<div class="sys-main">nvidia-smi 不可用</div>
      <div class="sys-row"><span>提示</span><b>请确认 NVIDIA 驱动已安装</b></div>`));
  }

  // CPU
  const c = d.cpu || {};
  const cpuName = c.name || c.model || "—";
  const hetero = c.heterogeneous && (c.models || []).length > 1;
  cards.push(card("CPU 处理器", c.percent != null ? Math.round(c.percent) + "%" : "", `
    <div class="sys-main">${escapeHtml(cpuName)}</div>
    <div class="${barClass(c.percent || 0)}"><div style="width:${c.percent || 0}%"></div></div>
    <div class="bar-label"><span>当前占用</span><span>${Math.round(c.percent || 0)}%</span></div>
    <div class="sys-row"><span>核心 / 线程</span><b>${c.cores} 核 / ${c.threads} 线程</b></div>
    <div class="sys-row"><span>当前频率</span><b>${c.freq ? c.freq + " MHz" : "—"}</b></div>
    ${c.architecture ? `<div class="sys-row"><span>架构</span><b>${escapeHtml(c.architecture)}</b></div>` : ""}
    ${c.vendor ? `<div class="sys-row"><span>厂商</span><b>${escapeHtml(c.vendor)}</b></div>` : ""}
    ${hetero ? (c.models || []).map(m => `<div class="sys-row"><span>${escapeHtml(m.name)}</span><b>${m.logical} 线程</b></div>`).join("") : ""}
    ${c.source ? `<div class="sys-row"><span>型号来源</span><b>${escapeHtml(c.source)}</b></div>` : ""}`));

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

// ---------- LoRA 面板交互 ----------
$("#imgLoraAdd")?.addEventListener("click", () => {
  const name = $("#imgLoraSelect").value;
  if (!name) return;
  const meta = imgLoraMeta(name);
  if (state.img.loras.length >= 8) {
    setEnhanceHint("最多 8 个 LoRA", "error");
    return;
  }
  state.img.loras.push({
    name,
    // 推荐强度自动应用（用户之后可手动改，上限由服务端按权重封顶）
    strength: meta ? Number(meta.recommended_strength) : 1.0,
    strength_cap: meta ? Number(meta.strength_cap !== undefined ? meta.strength_cap : 1.5) : 1.5,
    ctx_damping: meta ? (meta.ctx_damping !== undefined ? meta.ctx_damping : null) : null,
    ctx_perturbation: meta ? (meta.ctx_perturbation !== undefined ? meta.ctx_perturbation : null) : null,
    enabled: true,
    // 触发词自动带上上游标注的那个（用户可编辑 / 删除）
    trigger: meta && meta.trigger ? meta.trigger : "",
  });
  saveLoraConfig();
});

$("#loraInfoClose")?.addEventListener("click", closeLoraInfo);
$("#loraInfoDone")?.addEventListener("click", closeLoraInfo);
$("#loraInfoModal")?.addEventListener("click", event => {
  if (event.target === $("#loraInfoModal")) closeLoraInfo();
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && !$("#loraInfoModal").hidden) closeLoraInfo();
});

// ---------- 启动 ----------
// 启动脚本必须「逐项隔离」：任何一项抛异常都不能中断后面的项。
// 之前 esc() 未定义抛 ReferenceError 会中断整个顶层脚本，连带
// refreshJobs() / setInterval(refreshJobs, 2000) 都没注册 —— 表现是
// 标签显示「全部 / 全部类型」但任务列表空白，点一次才恢复。
// 这里每个步骤单独 try/catch，失败只记日志不阻断其余步骤。
function boot(step, fn) {
  try {
    fn();
  } catch (e) {
    console.error(`[启动] ${step} 失败:`, e);
  }
}

// 默认进入「Z-IMAGE 文生图」：导航高亮 + 参数面板 + 首屏任务列表
boot("默认模式", () => {
  const nav = $(`.nav-item[data-mode="${DEFAULT_MODE}"]`);
  if (nav) activateMode(DEFAULT_MODE, nav);
  else { state.mode = DEFAULT_MODE; showCreate(); renderMode(); }
});
boot("图像参数清单", loadImageOptions);
boot("图像 LoRA 清单", loadImageLoras);
boot("生成类型清单", loadGenerationTypes);
boot("模式面板", renderMode);
boot("状态标签", renderTabs);
boot("提示词版本", renderPromptVersions);   // 初始化二次增强「作用对象」切换（原始 / 一次增强后）
boot("长片片段", renderStitchClips);
boot("任务列表", refreshJobs);
boot("系统状态", refreshSystem);
boot("状态实时推送", startStatusStream);
setInterval(refreshJobs, 2000);
setInterval(refreshSystem, 3000);
