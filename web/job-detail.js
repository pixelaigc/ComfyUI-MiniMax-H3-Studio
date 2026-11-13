/**
 * web/job-detail.js —— 生成媒体库条目「详情」模态框（需求 C3 / C4 / C5）
 *
 * 【为什么单独一个文件】
 * C3 要展示的是**完整生成参数**，字段很多、逻辑也独立（开/关/Esc/遮罩/
 * 焦点管理/定时刷新不打断）。塞进 app.js 会让那个 229KB 的文件更难维护，
 * 也让这段逻辑没法独立单测。
 *
 * 【C3 的硬要求（逐条对应实现）】
 *   1. 点击卡片任意信息区 / 「详情」按钮 -> 弹模态框；
 *   2. 展示完整生成参数：模型、步数、调度器、采样器、shift、LoRA 列表+强度、
 *      尺寸、时长、seed、完整提示词、注意力机制、任务来源与工作流名、创建时间；
 *   3. 字段缺失显式标注（「—」/「作者未提供」/「未记录」）；
 *   4. 支持关闭按钮、Esc、点击遮罩关闭；
 *   5. **不与列表上的下载 / 复用参数 / 删除按钮冲突** ——
 *      卡片信息区的点击处理只挂在 `.job-meta` 上，`.job-actions` 里的按钮
 *      不冒泡到那里（事件目标判定 + stopPropagation 双保险）。
 *
 * 【C5】375 / 390 / 1280px 三档可用；小屏模态框不溢出（CSS 断点）。
 * 列表定时刷新不打断已打开的模态框 —— 模态框的内容在**打开时**取一次快照，
 * 刷新列表只重绘网格，不会动模态框（模态框不在网格里）。
 */
(() => {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;",
    '"': "&quot;", "'": "&#39;",
  }[c]));

  const MISSING = "—";

  /** 模态框当前展示的条目快照（打开时取一次）。 */
  let current = null;
  let previousFocus = null;

  // ------------------------------------------------------------------
  // 取值（口径统一在 web/job-meta.js —— 与卡片完全一致）
  // ------------------------------------------------------------------

  function meta() {
    return window.JOB_META || null;
  }

  function info(job) {
    const m = meta();
    if (m && typeof m.modelInfo === "function") return m.modelInfo(job);
    const raw = job && job.model_info;
    return (raw && typeof raw === "object") ? raw : {};
  }

  function t(value) {
    if (value === null || value === undefined) return MISSING;
    const s = String(value).trim();
    return s ? s : MISSING;
  }

  function n(value) {
    if (value === null || value === undefined || value === "") return MISSING;
    const num = Number(value);
    return Number.isFinite(num) ? String(num) : MISSING;
  }

  // ------------------------------------------------------------------
  // 渲染
  // ------------------------------------------------------------------

  function kv(pairs) {
    const rows = pairs.map(([label, value, raw]) => {
      const missing = value === MISSING || value === null || value === undefined
        || value === "";
      const text = missing ? MISSING : String(value);
      const cls = missing ? ' class="is-missing"' : "";
      const title = raw === undefined || raw === null ? "" : String(raw);
      const titleAttr = title && title !== text
        ? ` title="${esc(title)}"` : "";
      return `<span>${esc(label)}</span><b${cls}${titleAttr}>${esc(text)}</b>`;
    }).join("");
    return `<div class="job-detail-kv">${rows}</div>`;
  }

  function group(title, inner) {
    return `<div class="job-detail-group"><div class="job-detail-group-title">${esc(title)}</div>${inner}</div>`;
  }

  /** LoRA 列表（名称 + 强度），明确区分「工作流固定」那一枚。 */
  function loraBlock(job) {
    const m = meta();
    const list = (m && typeof m.loraList === "function")
      ? m.loraList(job)
      : [];
    if (!list.length) {
      return kv([["LoRA", MISSING]]);
    }
    const chips = list.map((item) => {
      const name = item.fixed ? `工作流固定：${item.label}` : item.label;
      const strength = (item.strength === null || item.strength === undefined
        || !Number.isFinite(item.strength)) ? "" : ` ×${item.strength.toFixed(2)}`;
      return `<span class="job-detail-recchip">${esc(name + strength)}</span>`;
    }).join("");
    return `<div class="job-detail-reclist">${chips}</div>`;
  }

  /** 作者推荐参数（需求 1 / 2 / 5）—— 卡片之外也能看到。 */
  function recBlock(rec, title) {
    const m = meta();
    if (!m || typeof m.recItems !== "function") return "";
    const items = m.recItems(rec);
    const chips = items.map((it) => {
      const cls = it.provided ? "job-detail-recchip" : "job-detail-recchip is-missing";
      // 逐字段的来源标注（如「（作者提供）」/「（ComfyUI 模型硬性值）」）
      const src = it.source ? ` ${it.source}` : "";
      const title = it.source ? ` title="${esc(it.source)}"` : "";
      return `<span class="${cls}"${title}>${esc(it.label)}：${esc(it.value)}${esc(src)}</span>`;
    }).join("");
    const source = (m && typeof m.recSource === "function") ? m.recSource(rec) : "";
    const sourceHtml = source
      ? `<div class="job-detail-source">${esc(source)}</div>` : "";
    return group(title, `<div class="job-detail-reclist">${chips}</div>${sourceHtml}`);
  }

  function renderBody(job) {
    const i = info(job);
    const p = job.params || {};
    const m = meta();

    // ---- 模型 / 采样 ----
    const modelName = t(i.model_label);
    const modelFile = t(i.model_unet);
    const sampling = m && typeof m.samplingText === "function"
      ? m.samplingText(job) : MISSING;
    const steps = m && typeof m.stepsText === "function"
      ? m.stepsText(job) : MISSING;

    const modelGroup = group("模型与采样", kv([
      ["模型", modelName, modelFile],
      ["权重文件", modelFile],
      ["采样器 / 调度器", sampling],
      ["步数 / CFG", steps],
      ["视频 shift", n(i.shift_video)],
      ["音频 shift", n(i.shift_audio)],
    ]));

    // ---- LoRA ----
    const loraGroup = group("LoRA（任务真实使用）", loraBlock(job));

    // ---- 尺寸 / 时长 / seed ----
    const size = m && typeof m.sizeText === "function"
      ? m.sizeText(job) : MISSING;
    const duration = m && typeof m.durationText === "function"
      ? m.durationText(job) : MISSING;
    const seed = m && typeof m.seedText === "function"
      ? m.seedText(job) : MISSING;
    const sizeGroup = group("尺寸与种子", kv([
      ["分辨率", size],
      ["分辨率档位", t(i.resolution)],
      ["画面比例", t(i.aspect)],
      ["时长", duration],
      ["种子", seed],
    ]));

    // ---- 注意力机制（需求 3） ----
    const attention = (m && typeof m.attentionText === "function")
      ? m.attentionText(job) : "";
    const attentionGroup = group("注意力机制（生成时实际使用）", kv([
      ["注意力", attention || MISSING],
    ]));

    // ---- 提示词 ----
    const prompt = p.prompt || "";
    const promptGroup = group(
      job.mode === "t2a" || job.mode === "a2a" ? "风格描述与歌词" : "完整提示词",
      prompt
        ? `<div class="job-detail-prompt">${esc(prompt)}</div>`
        : kv([["提示词", MISSING]]));

    // ---- 任务来源 / 工作流名 / 创建时间 ----
    const modeName = (window.MODE_INFO && window.MODE_INFO[job.mode]
      && window.MODE_INFO[job.mode].name) || job.mode || MISSING;
    const genLabel = job.gen_type_label || MISSING;
    const workflowName = t(i.source);
    const sourceGroup = group("任务来源", kv([
      ["生成类型", genLabel],
      ["模式", modeName],
      ["任务 ID", t(job.id)],
      ["摘要来源", workflowName],
      ["创建时间", job.created_at
        ? new Date(job.created_at * 1000).toLocaleString() : MISSING],
      ["耗时", job.elapsed === null || job.elapsed === undefined
        ? MISSING : `${Math.round(job.elapsed)}s`],
    ]));

    // ---- 作者推荐参数（需求 1 / 2 / 5） ----
    const recGroup =
      recBlock(i.model_recommendation, "主模型推荐参数（作者 / ComfyUI 硬性值）") +
      recBlock(i.lora_recommendation, "加速 LoRA 推荐参数（作者）") +
      recBlock(i.fixed_lora_recommendation, "工作流固定 LoRA 推荐参数（作者）");

    return modelGroup + loraGroup + sizeGroup + attentionGroup
      + promptGroup + sourceGroup + recGroup;
  }

  // ------------------------------------------------------------------
  // 打开 / 关闭
  // ------------------------------------------------------------------

  function open(job) {
    const modal = $("#jobDetailModal");
    const body = $("#jobDetailBody");
    const title = $("#jobDetailTitle");
    if (!modal || !body) return;
    // 【C5】打开时取**快照** —— 列表定时刷新只重绘网格，
    // 不会打断已打开的模态框（模态框不在网格里，也不订阅刷新）。
    current = job;
    body.innerHTML = renderBody(job);
    if (title) {
      const modeName = (window.MODE_INFO && window.MODE_INFO[job.mode]
        && window.MODE_INFO[job.mode].name) || job.mode || "任务";
      title.textContent = `${modeName} · 生成参数详情`;
    }
    previousFocus = document.activeElement;
    modal.hidden = false;
    document.body.classList.add("job-detail-open");
    const closeBtn = $("#jobDetailClose");
    if (closeBtn) closeBtn.focus();
  }

  function close() {
    const modal = $("#jobDetailModal");
    if (!modal || modal.hidden) return;
    modal.hidden = true;
    document.body.classList.remove("job-detail-open");
    current = null;
    if (previousFocus && typeof previousFocus.focus === "function") {
      try { previousFocus.focus(); } catch (e) { /* 元素已消失 */ }
    }
    previousFocus = null;
  }

  function isOpen() {
    const modal = $("#jobDetailModal");
    return !!(modal && !modal.hidden);
  }

  // ------------------------------------------------------------------
  // 事件绑定（只绑一次）
  // ------------------------------------------------------------------

  let bound = false;

  function bind() {
    if (bound) return;
    bound = true;
    document.addEventListener("click", (event) => {
      const target = event.target;
      if (!target || typeof target.closest !== "function") return;
      // 【C3】遮罩关闭 —— 只认遮罩本身，不认面板内部
      if (target.id === "jobDetailBackdrop") {
        event.preventDefault();
        close();
        return;
      }
      // 关闭按钮
      if (target.id === "jobDetailClose") {
        event.preventDefault();
        close();
        return;
      }
      // 「详情」按钮（卡片上的）
      const detailBtn = target.closest('[data-act="detail"]');
      if (detailBtn) {
        event.preventDefault();
        event.stopPropagation();
        const card = detailBtn.closest(".job-card");
        const jid = card && card.dataset ? card.dataset.jid : null;
        const job = jid ? findJobById(jid) : null;
        if (job) open(job);
        return;
      }
      // 卡片信息区点击 -> 也弹模态框（用户要求「点击卡片任意信息区」）
      const metaBox = target.closest(".job-meta");
      if (metaBox) {
        // 【C3】不与按钮冲突：.job-actions 里的按钮不在 .job-meta 里；
        // 这里再排一次「提示词」按钮（它在 .job-meta 里，自己有独立行为）。
        if (target.closest('[data-act="prompt"]')) return;
        if (target.closest("button, a")) return;
        const card = metaBox.closest(".job-card");
        const jid = card && card.dataset ? card.dataset.jid : null;
        const job = jid ? findJobById(jid) : null;
        if (job) {
          event.preventDefault();
          open(job);
        }
      }
    });
    // Esc 关闭
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && isOpen()) {
        event.preventDefault();
        close();
      }
    });
  }

  /** 从当前缓存的任务列表里找一条。 */
  function findJobById(jid) {
    const list = window.JOB_CACHE || [];
    for (const job of list) {
      if (job && job.id === jid) return job;
    }
    return null;
  }

  // ------------------------------------------------------------------
  // 导出
  // ------------------------------------------------------------------

  window.JOB_DETAIL = {
    open,
    close,
    isOpen,
    bind,
    renderBody,
    findJobById,
  };

  // 页面加载后自动绑一次（DOMContentLoaded 已过就直接绑）
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
