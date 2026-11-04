/* H3 Studio 前端逻辑 */
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

const MODE_INFO = {
  t2v: { name: "文生视频", kind: "video" },
  flf: { name: "首尾帧", kind: "video" },
  r2v: { name: "参考生成", kind: "video" },
  t2i: { name: "Z-IMAGE 文生图", kind: "image" },
};

const state = {
  mode: "t2v",
  tab: "active",
  page: "create",
  res: "768p",
  model: "lora",
  lora: "v4step600",
  loraStrength: 1.0,
  aspect: "16:9",
  duration: 5,
  steps: 4,
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
  const mk = (key, label, multi) => {
    const slot = document.createElement("div");
    slot.className = "up-slot";
    slot.innerHTML = `+<span class="tag">${label}</span>`;
    const inp = document.createElement("input");
    inp.type = "file"; inp.accept = "image/*";
    inp.addEventListener("change", e => {
      const f = e.target.files[0];
      if (!f) return;
      const url = URL.createObjectURL(f);
      slot.innerHTML = `<img src="${url}"><span class="tag">${label}</span>`;
      slot.classList.add("filled");
      state.files[key] = f;
    });
    slot.appendChild(inp);
    slot.addEventListener("click", () => inp.click());
    row.appendChild(slot);
  };
  if (mode === "flf") {
    $("#uploadTitle").textContent = "上传首帧与尾帧（尾帧可省略）";
    zone.style.display = "block";
    mk("first", "首帧"); mk("last", "尾帧");
  } else if (mode === "r2v") {
    $("#uploadTitle").textContent = "上传参考图（1~3 张，保持主体一致）";
    zone.style.display = "block";
    mk("ref0", "参考 1"); mk("ref1", "参考 2"); mk("ref2", "参考 3");
  } else {
    zone.style.display = "none";
  }
  // 文生图隐藏时长 / 模型 / 步数
  const isVideo = info.kind === "video";
  $("#durField").style.display = isVideo ? "" : "none";
  $("#modelField").style.display = isVideo ? "" : "none";
  $("#stepsField").style.display = isVideo ? "" : "none";
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
  const def = state.model === "lora" ? 4 : 20;
  state.steps = def;
  $("#steps").value = def;
  $("#stepsVal").textContent = def;
}
$("#duration").addEventListener("input", e => {
  state.duration = parseInt(e.target.value);
  $("#durVal").textContent = state.duration + "s";
});

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
}
$$(".jobs-tabs button").forEach(b => b.addEventListener("click", () => {
  state.tab = b.dataset.st;
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
    preview = `<video src="/files/${job.id}/${video.file}" controls preload="metadata"></video>`;
  } else if (image) {
    preview = `<img src="/files/${job.id}/${image.file}">`;
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
    ? `<button class="cancel-btn" data-act="cancel">✕ Cancel</button>`
    : `<span>${fmtTime(job.created_at)}</span>`;

  card.dataset.jid = job.id;
  card.innerHTML = `
    <div class="job-top"><span>${info.name} · ${fmtTime(job.created_at)}</span>${topRight}</div>
    <div class="job-preview">${preview}</div>
    ${job.status === "failed" ? "" : `<div class="job-meta">
      <div class="row"><span>模式</span><b>${info.name}</b></div>
      <div class="row"><span>分辨率</span><b>${p.resolution || "768p"} · ${p.aspect || "16:9"}</b></div>
      ${info.kind === "video" ? `<div class="row"><span>时长</span><b>${p.duration || 5}s</b></div>
      <div class="row"><span>模型</span><b>${modelLabel(p)}</b></div>` : ""}
      <div class="row"><span>种子</span><b>${p.seed || "随机"}</b></div>
      <div class="row"><span>耗时</span><b>${job.status === "done" ? fmtElapsed(job.elapsed) : fmtElapsed(job.elapsed)}</b></div>
      <div class="row" style="max-width:100%"><span style="flex:0 0 auto">提示词</span><b style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml((p.prompt || "").slice(0, 60))}</b></div>
    </div>`}
    <div class="job-actions">
      ${video || image ? `<button data-act="download">⬇ 下载</button>` : ""}
      <button data-act="reuse">复用参数</button>
      ${!runningOrQueued ? `<button data-act="delete" class="danger">删除</button>` : ""}
    </div>`;

  card.querySelector('[data-act="cancel"]')?.addEventListener("click", async () => {
    await fetch(`/api/jobs/${job.id}/cancel`, { method: "POST" });
    refreshJobs();
  });
  card.querySelector('[data-act="delete"]')?.addEventListener("click", async () => {
    if (!confirm("删除该记录及输出文件？")) return;
    await fetch(`/api/jobs/${job.id}`, { method: "DELETE" });
    refreshJobs();
  });
  card.querySelector('[data-act="download"]')?.addEventListener("click", () => {
    const f = video || image;
    const a = document.createElement("a");
    a.href = `/files/${job.id}/${f.file}`;
    a.download = f.name;
    a.click();
  });
  card.querySelector('[data-act="reuse"]')?.addEventListener("click", () => {
    // 找到对应模式侧栏项并切换
    $(`.nav-item[data-mode="${job.mode}"]`)?.click();
    $("#prompt").value = p.prompt || "";
    if (p.resolution) setSeg("#resSeg", p.resolution), state.res = p.resolution;
    if (p.aspect) setSeg("#aspectChips", p.aspect), state.aspect = p.aspect;
    if (p.duration) { state.duration = p.duration; $("#duration").value = p.duration; $("#durVal").textContent = p.duration + "s"; }
    if (p.steps) { state.steps = p.steps; $("#steps").value = p.steps; $("#stepsVal").textContent = p.steps; }
    if (p.seed) $("#seed").value = p.seed;
    if (p.model) { setSeg("#modelCards", p.model); state.model = p.model; $("#loraField").style.display = p.model === "lora" ? "" : "none"; }
    if (p.lora) { setSeg("#loraCards", p.lora); state.lora = p.lora; }
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
  return card;
}

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

async function refreshJobs() {
  if (state.page === "system") return;
  try {
    let url = state.tab === "active" ? "/api/jobs?status=all" : `/api/jobs?status=${state.tab}`;
    const resp = await fetch(url);
    const data = await resp.json();
    let jobs = data.jobs || [];
    if (state.tab === "active") jobs = jobs.filter(j => ["queued", "running"].includes(j.status));
    const grid = $("#jobsGrid");

    if (!jobs.length) {
      const hint = `<div class="empty-hint">${
        state.tab === "active" ? "暂无进行中的任务，左侧选个模式开始创作吧" :
        state.tab === "done" ? "还没有已完成的生成记录" : "没有失败记录，一切正常 🎉"}</div>`;
      if (grid.firstElementChild?.classList?.contains("empty-hint")) {
        if (grid.firstElementChild.textContent !== hint.replace(/<[^>]+>/g, "")) grid.innerHTML = hint;
      } else {
        grid.innerHTML = hint;
      }
      return;
    }

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
  } catch (e) { /* 静默重试 */ }
}

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
  $("#systemPage").style.display = "none";
}

function showSystem() {
  state.page = "system";
  $$(".nav-item").forEach(x => x.classList.remove("active"));
  $("#navSystem").classList.add("active");
  $("#jobsArea").style.display = "none";
  $("#panel").style.display = "none";
  $("#systemPage").style.display = "block";
  refreshHardware();
}

$("#navSystem").addEventListener("click", showSystem);
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
refreshJobs();
refreshSystem();
setInterval(refreshJobs, 2000);
setInterval(refreshSystem, 3000);
