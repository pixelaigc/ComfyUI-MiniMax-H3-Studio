/* H3 Studio 前端逻辑 */
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

const MODE_INFO = {
  t2v: { name: "文生视频", kind: "video" },
  flf: { name: "首尾帧", kind: "video" },
  r2v: { name: "参考生成", kind: "video" },
  pika: { name: "PIKA H3 Flex", kind: "video" },
  t2i: { name: "Z-IMAGE 文生图", kind: "image" },
  qwen_t2i: { name: "Qwen Image 2.1", kind: "image" },
  tts: { name: "中文文字转语音", kind: "audio" },
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
  if (mode === "flf" || mode === "pika") {
    $("#uploadTitle").textContent = mode === "pika"
      ? "可选：上传或拖入 PIKA 首帧与尾帧（两者都可省略）"
      : "上传首帧与尾帧，或从已完成图片拖入（尾帧可省略）";
    zone.style.display = "block";
    mk("first", "首帧"); mk("last", "尾帧");
  } else if (mode === "r2v") {
    $("#uploadTitle").textContent = "上传参考图，或从已完成图片拖入（1~3 张）";
    zone.style.display = "block";
    mk("ref0", "参考 1"); mk("ref1", "参考 2"); mk("ref2", "参考 3");
  } else {
    zone.style.display = "none";
  }
  const isTts = mode === "tts";
  const isPika = mode === "pika";
  const isQwen = mode === "qwen_t2i";
  const note = $("#modeNote");
  const notes = {
    pika: "PIKA H3 Flex LoRA · LCM 固定 6 步 · 支持纯文本或可选首帧/尾帧",
    qwen_t2i: "Qwen Image 2.1 INT8 · 25 步，适合中文语义与画面文字",
    tts: "本机 Windows SAPI 离线合成 · 无需联网、不消耗云服务额度",
  };
  note.hidden = !notes[mode];
  note.textContent = notes[mode] || "";
  $("#resField").style.display = isTts ? "none" : "";
  $("#aspectField").style.display = isTts ? "none" : "";
  $("#ttsFields").hidden = !isTts;
  $("#promptLabel").textContent = isTts ? "朗读文本" : "提示词";
  $("#prompt").placeholder = isTts ? "输入需要朗读的中文内容…" : "描述画面…支持多镜头分镜写法";
  $("#genBtn").textContent = isTts ? "生成语音" : (info.kind === "image" ? "生成图片" : "生成视频");
  // 文生图、PIKA 固定工作流及 TTS 隐藏不适用参数
  const isVideo = info.kind === "video";
  $("#durField").style.display = isVideo ? "" : "none";
  $("#modelField").style.display = isVideo && !isPika ? "" : "none";
  $("#stepsField").style.display = isVideo && !isPika ? "" : "none";
  $("#seedStrengthPair").style.display = isTts ? "none" : "";
  updateSeedStrengthLayout();
  // 时长标签
  $("#durVal").textContent = state.duration + "s";
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
  const showStrength = ["t2v", "flf", "r2v"].includes(state.mode) && state.model === "lora";
  $("#loraStrengthField").style.display = showStrength ? "" : "none";
  $("#seedStrengthPair").classList.toggle("seed-only", !showStrength);
}
$("#duration").addEventListener("input", e => {
  state.duration = parseInt(e.target.value);
  $("#durVal").textContent = state.duration + "s";
});
$("#ttsSpeed").addEventListener("input", e => { $("#ttsSpeedVal").textContent = Number(e.target.value).toFixed(2) + "×"; });
$("#ttsVolume").addEventListener("input", e => { $("#ttsVolumeVal").textContent = e.target.value + "%"; });

// ---------- 提交 ----------
$("#genBtn").addEventListener("click", async () => {
  const prompt = $("#prompt").value.trim();
  if (!prompt) { alert("请填写提示词"); return; }
  const mode = state.mode;
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
  if (mode === "tts") {
    params.voice = $("#ttsVoice").value;
    params.speed = Number($("#ttsSpeed").value);
    params.volume = Number($("#ttsVolume").value);
  }
  fd.append("params", JSON.stringify(params));
  try {
    if (mode === "flf" || mode === "pika") {
      if (mode === "flf" && !state.files.first) { alert("首尾帧模式需要上传首帧"); return; }
      if (state.files.first) fd.append("file_first", state.files.first);
      if (state.files.last) fd.append("file_last", state.files.last);
    } else if (mode === "r2v") {
      const refs = [state.files.ref0, state.files.ref1, state.files.ref2].filter(Boolean);
      if (!refs.length) { alert("参考模式至少上传 1 张参考图"); return; }
      refs.forEach(f => fd.append("file_ref", f));
    }
    const resp = await fetch("/api/jobs", { method: "POST", body: fd });
    const data = await resp.json();
    if (!resp.ok) { alert(data.error || "提交失败"); return; }
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

function jobModelLabel(job, p) {
  if (job.mode === "pika") return "PIKA Flex LoRA · 6步";
  if (job.mode === "qwen_t2i") return "Qwen Image 2.1 · 25步";
  return modelLabel(p);
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
  const audio = (job.outputs || []).find(o => o.kind === "audio");
  if (video) {
    preview = `<video src="${outputUrl(job, video)}" controls preload="metadata"></video>`;
  } else if (image) {
    preview = `<img src="${outputUrl(job, image)}">`;
  } else if (audio) {
    preview = `<audio src="${outputUrl(job, audio)}" controls preload="metadata"></audio>`;
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
      ${info.kind !== "audio" ? `<div class="row"><span>分辨率</span><b>${p.resolution || "768p"} · ${p.aspect || "16:9"}</b></div>` : `
      <div class="row"><span>声音</span><b>${escapeHtml((p.voice || "Microsoft Huihui Desktop").replace("Microsoft ", ""))}</b></div>`}
      ${info.kind === "video" ? `<div class="row"><span>时长</span><b>${p.duration || 5}s</b></div>
      <div class="row"><span>模型</span><b>${jobModelLabel(job, p)}</b></div>` : ""}
      ${info.kind === "audio" ? `<div class="row"><span>语速 / 音量</span><b>${Number(p.speed || 1).toFixed(2)}× · ${p.volume ?? 100}%</b></div>` : `<div class="row"><span>种子</span><b>${p.seed || "随机"}</b></div>`}`}
      <div class="row"><span>耗时</span><b>${job.status === "done" ? fmtElapsed(job.elapsed) : fmtElapsed(job.elapsed)}</b></div>
      ${job.mode !== "longfilm" ? `<button class="row prompt-row" data-act="prompt" title="点击查看并复制完整提示词"><span>提示词</span><b>${escapeHtml((p.prompt || "").slice(0, 80))}</b></button>` : ""}
    </div>`}
    <div class="job-actions">
      ${video || image || audio ? `<button data-act="download">⬇ 下载</button>` : ""}
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
    const f = video || image || audio;
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
    if (p.resolution) setSeg("#resSeg", p.resolution), state.res = p.resolution;
    if (p.aspect) setSeg("#aspectChips", p.aspect), state.aspect = p.aspect;
    if (p.duration) { state.duration = p.duration; $("#duration").value = p.duration; $("#durVal").textContent = p.duration + "s"; }
    if (p.steps) { state.steps = p.steps; $("#steps").value = p.steps; $("#stepsVal").textContent = p.steps; }
    if (p.seed) $("#seed").value = p.seed;
    if (p.model) { setSeg("#modelCards", p.model); state.model = p.model; $("#loraField").style.display = p.model === "lora" ? "" : "none"; updateSeedStrengthLayout(); }
    if (p.lora) { setSeg("#loraCards", p.lora); state.lora = p.lora; }
    if (p.voice) $("#ttsVoice").value = p.voice;
    if (p.speed) { $("#ttsSpeed").value = p.speed; $("#ttsSpeedVal").textContent = Number(p.speed).toFixed(2) + "×"; }
    if (p.volume != null) { $("#ttsVolume").value = p.volume; $("#ttsVolumeVal").textContent = p.volume + "%"; }
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
}

function showStitch() {
  state.page = "stitch";
  $$(".nav-item").forEach(x => x.classList.remove("active"));
  $("#navStitch").classList.add("active");
  $("#jobsArea").style.display = "";
  $("#panel").style.display = "none";
  $("#stitchPanel").style.display = "";
  $("#systemPage").style.display = "none";
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
  refreshHardware();
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
renderMode();
renderTabs();
renderStitchClips();
refreshJobs();
refreshSystem();
setInterval(refreshJobs, 2000);
setInterval(refreshSystem, 3000);
