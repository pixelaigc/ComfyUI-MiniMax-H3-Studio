/* H3 Studio 资产素材库 —— 前端逻辑（web/assets.js）
 *
 * 与 app.js / providers.js 平行，独立作用域，只用 window.ASSETLIB 暴露少量入口。
 * app.js 通过 window.ASSETLIB.showAssets() 切换到素材库页；
 * app.js 的媒体库右键菜单通过 window.ASSETLIB.addFromMedia() 入库。
 */
(() => {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => [...document.querySelectorAll(s)];

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const CATEGORY_LABEL = {
    image: "图片", video: "视频", audio: "音频",
    doc: "文档", workflow: "工作流", other: "其它",
  };
  const SUBTYPE_LABEL = {
    photo: "通用图片", character: "角色定义图", storyboard: "分镜画稿",
    clip: "视频片段", reference_motion: "动作参考",
    music: "音乐", voice: "配音", sfx: "音效",
    prompt: "提示词稿", storyboard_script: "分镜稿",
    comfy_json: "工作流 JSON", subtitle: "字幕", binary: "附件",
  };
  const SOURCE_LABEL = {
    upload: "Web 上传", job_output: "媒体库添加",
    import: "导入", comfy_workflow: "工作流", plan_shot: "分镜",
  };
  const CATEGORY_ICON = {
    image: "🖼", video: "🎬", audio: "♪",
    doc: "📄", workflow: "⚙", other: "📦",
  };

  const state = {
    filters: {
      category: "", tag: "", tags_any: "", q: "", source: "",
      in_trash: "", referenced: "", sort: "created_desc",
      page: 1, page_size: 60,
    },
    items: [],
    total: 0,
    allTags: [],
    current: null,        // 当前详情抽屉的素材
    picker: null,        // 选择器状态
    uploads: new Map(),  // 上传进度
  };

  // ---------------------------------------------------------------- 工具

  function toast(message, kind = "") {
    let box = $("#alToast");
    if (!box) {
      box = document.createElement("div");
      box.id = "alToast";
      box.className = "al-toast";
      document.body.appendChild(box);
    }
    box.textContent = message;
    box.className = "al-toast show " + kind;
    clearTimeout(box._timer);
    box._timer = setTimeout(() => { box.className = "al-toast " + kind; }, 3200);
  }

  function fmtBytes(bytes) {
    if (bytes == null) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = Number(bytes), index = 0;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
    return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
  }

  function fmtDuration(sec) {
    if (sec == null) return "—";
    const total = Math.round(Number(sec));
    const m = Math.floor(total / 60), s = total % 60;
    return m ? `${m}:${String(s).padStart(2, "0")}` : `${s}s`;
  }

  function fmtTime(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  async function api(path, options = {}) {
    const resp = await fetch(path, options);
    let body = {};
    try { body = await resp.json(); } catch (e) { /* 非 JSON 响应 */ }
    if (!resp.ok) {
      const err = new Error(body.error || `请求失败（HTTP ${resp.status}）`);
      err.payload = body;
      err.status = resp.status;
      throw err;
    }
    return body;
  }

  // ---------------------------------------------------------------- 列表

  function buildQuery() {
    const params = new URLSearchParams();
    Object.entries(state.filters).forEach(([key, value]) => {
      if (value !== "" && value != null) params.set(key, value);
    });
    return params.toString();
  }

  async function loadAssets() {
    const grid = $("#alGrid");
    if (grid) grid.innerHTML = `<div class="al-empty">加载中…</div>`;
    try {
      const data = await api("/api/assets?" + buildQuery());
      state.items = data.assets || [];
      state.total = data.total || 0;
      renderAssets();
      renderPagination();
    } catch (e) {
      if (grid) grid.innerHTML = `<div class="al-empty">加载失败：${esc(e.message)}</div>`;
    }
  }

  async function loadTags() {
    try {
      const data = await api("/api/assets/tags");
      state.allTags = data.tags || [];
      renderTagPicker();
    } catch (e) { /* 标签清单失败不阻塞主流程 */ }
  }

  function thumbFor(asset) {
    if (asset.preview_url) {
      return `<img src="${esc(asset.preview_url)}" alt="${esc(asset.name)}" loading="lazy">`;
    }
    return `<span class="ph">${CATEGORY_ICON[asset.category] || "📦"}</span>`;
  }

  function renderAssets() {
    const grid = $("#alGrid");
    if (!grid) return;
    if (!state.items.length) {
      const msg = state.filters.in_trash === "1"
        ? "回收站是空的"
        : (state.filters.q || state.filters.tag
          ? "没有匹配的素材，换个筛选条件试试"
          : "素材库还是空的 —— 点右上角「上传素材」，或从右侧生成结果右键「添加到素材库」");
      grid.innerHTML = `<div class="al-empty">${esc(msg)}</div>`;
      return;
    }
    grid.innerHTML = state.items.map(asset => {
      const tags = asset.tags || [];
      const shown = tags.slice(0, 3).map(t => `<span class="al-tag">${esc(t)}</span>`).join("");
      const more = tags.length > 3 ? `<span class="al-tag more">+${tags.length - 3}</span>` : "";
      const badge = asset.category === "video" && asset.duration_sec != null
        ? `<span class="dur">${esc(fmtDuration(asset.duration_sec))}</span>` : "";
      const refBadge = asset.ref_count > 0
        ? `<span class="badge right" title="被引用 ${asset.ref_count} 次">引用 ${asset.ref_count}</span>` : "";
      const analyzed = asset.analyzed
        ? `<span class="badge" title="已有模型分析">已分析</span>` : "";
      const inTrash = asset.deleted_at ? `<span class="badge" style="background:rgba(200,50,55,.85)">回收站</span>` : "";
      return `<div class="al-card" data-id="${esc(asset.id)}" draggable="true">
        <div class="al-thumb">${thumbFor(asset)}${analyzed}${inTrash}${refBadge}${badge}</div>
        <div class="al-card-body">
          <div class="al-name" title="${esc(asset.name)}">${esc(asset.name)}</div>
          <div class="al-meta">
            <span>${esc(CATEGORY_LABEL[asset.category] || asset.category)}</span>
            <span>${esc(fmtBytes(asset.bytes))}</span>
            ${asset.width ? `<span>${asset.width}×${asset.height}</span>` : ""}
            ${asset.duration_sec != null && asset.category !== "video"
              ? `<span>${esc(fmtDuration(asset.duration_sec))}</span>` : ""}
          </div>
          <div class="al-tags">${shown}${more}</div>
        </div>
      </div>`;
    }).join("");
    bindAssetCards();
  }

  function bindAssetCards() {
    $$("#alGrid .al-card").forEach(card => {
      const assetId = card.dataset.id;
      card.addEventListener("click", () => openDrawer(assetId));
      card.addEventListener("contextmenu", event => {
        event.preventDefault();
        showAssetContext(event, assetId);
      });
      // 拖到任务参数面板 = 以 asset_id 引用（不复制文件）
      card.addEventListener("dragstart", event => {
        const asset = state.items.find(a => a.id === assetId);
        if (!asset) return;
        event.dataTransfer.setData("application/x-h3-asset", JSON.stringify({
          asset_id: asset.id, name: asset.name, category: asset.category,
          subtype: asset.subtype, version: asset.latest_version,
        }));
        event.dataTransfer.effectAllowed = "copy";
      });
    });
  }

  function renderPagination() {
    const pager = $("#alPager");
    if (!pager) return;
    const pageSize = state.filters.page_size;
    const pages = Math.max(1, Math.ceil(state.total / pageSize));
    if (pages <= 1) { pager.innerHTML = ""; pager.hidden = true; return; }
    pager.hidden = false;
    const current = state.filters.page;
    const buttons = [];
    for (let i = Math.max(1, current - 2); i <= Math.min(pages, current + 2); i += 1) {
      buttons.push(`<button data-page="${i}" class="${i === current ? "active" : ""}">${i}</button>`);
    }
    pager.innerHTML = `
      <button data-page="${current - 1}" ${current <= 1 ? "disabled" : ""}>上一页</button>
      ${buttons.join("")}
      <button data-page="${current + 1}" ${current >= pages ? "disabled" : ""}>下一页</button>
      <span class="page-summary">共 ${state.total} 个 · 第 ${current}/${pages} 页</span>`;
    pager.querySelectorAll("button[data-page]").forEach(btn => {
      btn.addEventListener("click", () => {
        state.filters.page = Number(btn.dataset.page) || 1;
        loadAssets();
      });
    });
  }

  // ---------------------------------------------------------------- 标签筛选

  function renderTagPicker() {
    const menu = $("#alTagMenu");
    if (!menu) return;
    const tags = state.allTags || [];
    if (!tags.length) {
      menu.innerHTML = `<div style="padding:8px 10px;color:var(--muted);font-size:12px">还没有标签</div>`;
      return;
    }
    menu.innerHTML = tags.map(item => `
      <label><input type="checkbox" data-tag="${esc(item.tag)}"
        ${state.filters.tags_any === item.tag ? "checked" : ""}>
        <span>${esc(item.tag)}</span><span class="tag-count">${item.count}</span></label>`).join("");
    menu.querySelectorAll("input[data-tag]").forEach(box => {
      box.addEventListener("change", () => {
        state.filters.tags_any = box.checked ? box.dataset.tag : "";
        state.filters.page = 1;
        loadAssets();
      });
    });
  }

  function renderChips() {
    const box = $("#alCatChips");
    if (!box) return;
    const cats = ["", "image", "video", "audio", "doc", "workflow", "other"];
    box.innerHTML = cats.map(cat => {
      const label = cat === "" ? "全部" : (CATEGORY_LABEL[cat] || cat);
      return `<button class="al-chip ${state.filters.category === cat ? "active" : ""}"
        data-cat="${cat}">${esc(label)}</button>`;
    }).join("");
    box.querySelectorAll("button[data-cat]").forEach(btn => {
      btn.addEventListener("click", () => {
        state.filters.category = btn.dataset.cat;
        state.filters.page = 1;
        renderChips();
        loadAssets();
      });
    });
  }

  // ---------------------------------------------------------------- 详情抽屉

  async function openDrawer(assetId) {
    const drawer = $("#alDrawer");
    const mask = $("#alDrawerMask");
    if (!drawer) return;
    drawer.classList.add("open");
    mask.classList.add("open");
    $("#alDrawerBody").innerHTML = `<div class="al-empty">加载中…</div>`;
    try {
      const asset = await api("/api/assets/" + encodeURIComponent(assetId));
      state.current = asset;
      renderDrawer(asset);
    } catch (e) {
      $("#alDrawerBody").innerHTML = `<div class="al-err">加载失败：${esc(e.message)}</div>`;
    }
  }

  function closeDrawer() {
    const drawer = $("#alDrawer");
    const mask = $("#alDrawerMask");
    if (drawer) drawer.classList.remove("open");
    if (mask) mask.classList.remove("open");
    state.current = null;
  }

  function renderDrawer(asset) {
    const body = $("#alDrawerBody");
    if (!body) return;
    const version = asset.current_version || {};
    const analysis = asset.latest_analysis;
    const refs = asset.references || { refs: [], count: 0 };

    const preview = asset.preview_url
      ? `<img src="${esc(asset.preview_url)}" alt="${esc(asset.name)}">`
      : (asset.category === "video"
        ? `<video src="${esc(asset.content_url)}" controls preload="metadata"></video>`
        : `<span class="ph">${CATEGORY_ICON[asset.category] || "📦"}</span>`);

    const tagsHtml = (asset.tags || []).map(tag =>
      `<span class="al-tag">${esc(tag)}<button data-rmtag="${esc(tag)}" title="删除标签">×</button></span>`
    ).join("");

    const refsHtml = refs.refs.length
      ? refs.refs.map(ref => `<div class="al-ref"><span>${esc(ref.label || ref.ref_id)}</span>
          <span class="slot">${esc(ref.slot)}</span></div>`).join("")
      : `<div class="al-ref"><span>还没有任务引用这个素材</span></div>`;

    const sourceMeta = asset.source_meta || {};
    const metaRows = Object.entries(sourceMeta).filter(([, v]) =>
      v !== null && v !== undefined && v !== "" && typeof v !== "object");
    const metaHtml = metaRows.length
      ? metaRows.map(([k, v]) => `<div class="kv"><span>${esc(k)}</span>
          <b title="${esc(String(v))}">${esc(String(v))}</b></div>`).join("")
      : "";

    body.innerHTML = `
      <div class="al-preview-box">${preview}</div>

      <div>
        <div class="al-section-title">名称与标签</div>
        <div class="al-inline" style="margin-bottom:8px">
          <input class="al-input" id="alNameInput" value="${esc(asset.name)}" placeholder="素材名称">
          <button class="al-btn" id="alSaveName">保存</button>
        </div>
        <div class="al-taglist" id="alTagList">${tagsHtml}</div>
        <div class="al-inline">
          <input class="al-input" id="alTagInput" placeholder="输入标签后回车（逗号分隔多个）">
          <button class="al-btn" id="alAddTag">添加</button>
        </div>
      </div>

      <div>
        <div class="al-section-title">元数据</div>
        <div class="al-kv">
          <div class="kv"><span>类型</span><b>${esc(CATEGORY_LABEL[asset.category] || asset.category)} / ${esc(SUBTYPE_LABEL[asset.subtype] || asset.subtype)}</b></div>
          <div class="kv"><span>来源</span><b>${esc(SOURCE_LABEL[asset.source] || asset.source)}</b></div>
          <div class="kv"><span>格式 / 容器</span><b>${esc(version.container || version.mime || "—")}</b></div>
          <div class="kv"><span>文件大小</span><b>${esc(fmtBytes(version.bytes))}</b></div>
          ${version.width ? `<div class="kv"><span>尺寸</span><b>${version.width}×${version.height}</b></div>` : ""}
          ${version.duration_sec != null ? `<div class="kv"><span>时长</span><b>${esc(fmtDuration(version.duration_sec))}</b></div>` : ""}
          ${version.fps ? `<div class="kv"><span>帧率</span><b>${version.fps} fps</b></div>` : ""}
          ${version.codec ? `<div class="kv"><span>视频/图像编码</span><b>${esc(version.codec)}</b></div>` : ""}
          ${version.audio_codec ? `<div class="kv"><span>音频编码</span><b>${esc(version.audio_codec)}</b></div>` : ""}
          ${version.sample_rate ? `<div class="kv"><span>采样率</span><b>${version.sample_rate} Hz</b></div>` : ""}
          ${version.channels ? `<div class="kv"><span>声道</span><b>${version.channels}</b></div>` : ""}
          <div class="kv"><span>创建时间</span><b>${esc(fmtTime(asset.created_at))}</b></div>
          <div class="kv"><span>内容哈希</span><b title="${esc(version.sha256 || "")}">${esc((version.sha256 || "").slice(0, 16))}</b></div>
          <div class="kv"><span>被引用次数</span><b>${refs.count}</b></div>
          <div class="kv"><span>版本数</span><b>${asset.version_count}</b></div>
        </div>
        ${version.transcode_reason
          ? `<div class="al-err" style="margin-top:8px">⚠ ${esc(version.transcode_reason)}</div>` : ""}
        ${metaHtml ? `<div class="al-kv" style="margin-top:10px">${metaHtml}</div>` : ""}
      </div>

      <div>
        <div class="al-section-title">模型分析</div>
        <div class="al-analysis-rev">
          <span>${analysis
            ? `第 ${analysis.revision} 版 · ${esc(analysis.provider_name || "")} · ${esc(fmtTime(analysis.created_at))}`
            : "尚未分析"}</span>
          <button class="al-btn" id="alAnalyzeBtn">深度分析</button>
        </div>
        ${asset.category === "audio" ? `
        <div class="al-inline" style="margin-top:6px;gap:8px;align-items:center">
          <label class="al-inline" style="gap:5px;cursor:pointer">
            <input type="checkbox" id="alDeepAudio">
            <span style="font-size:12px">深度音频管线（BPM/乐器/人声/ASR）</span>
          </label>
          <span style="font-size:11px;color:var(--muted)" id="alDeepHint"></span>
        </div>` : ""}
        <div id="alAnalysisStatus" hidden></div>
        <div class="al-analysis" id="alAnalysisText">${esc(analysis ? analysis.text : "")}</div>
        <div class="al-inline" style="margin-top:8px">
          <textarea class="al-textarea" id="alCorrectionInput" rows="2"
            placeholder="对分析结果追问或纠正，例如「不是椭圆形，是菱形」"></textarea>
        </div>
        <div class="al-inline" style="margin-top:6px">
          <button class="al-btn primary" id="alCorrectBtn">提交纠错</button>
          ${(asset.analyses || []).length > 1
            ? `<button class="al-btn" id="alHistoryBtn">查看修订历史（${asset.analyses.length}）</button>` : ""}
        </div>
      </div>

      <div>
        <div class="al-section-title">引用（被谁使用）</div>
        <div class="al-reflist">${refsHtml}</div>
      </div>

      <div class="al-inline" style="flex-wrap:wrap">
        <a class="al-btn" href="${esc(asset.content_url)}" target="_blank" rel="noopener">下载原文件</a>
        <button class="al-btn" id="alRestoreBtn" ${asset.deleted_at ? "" : "hidden"}>还原</button>
        <button class="al-btn danger" id="alDeleteBtn">删除</button>
      </div>`;

    bindDrawer(asset);
  }

  // ---------------------------------------------------------------- 分析状态轮询

  //: 当前正在轮询的任务（同一时刻只盯一个抽屉）
  let _pollTimer = null;

  function renderAnalysisStatus(data) {
    const box = $("#alAnalysisStatus");
    if (!box) return;
    const status = data.status || "idle";
    if (status === "idle" || !status) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    const pct = Math.round((data.progress || 0) * 100);
    const stage = data.stage || "";
    const msg = data.message || "";
    const cls = status === "failed" ? "err" : (status === "done" ? "ok" : "run");
    box.hidden = false;
    box.className = "al-anstatus " + cls;
    box.innerHTML = `
      <div class="al-anstatus-row">
        <span>${esc(statusLabel(status))}${stage ? " · " + esc(stage) : ""}</span>
        <span>${pct}%</span>
      </div>
      <div class="al-anstatus-bar"><i style="width:${pct}%"></i></div>
      ${msg ? `<div class="al-anstatus-msg">${esc(msg)}</div>` : ""}`;
  }

  function statusLabel(status) {
    return ({ queued: "排队中", running: "分析中", done: "已完成",
              failed: "失败" })[status] || status;
  }

  /**
   * 轮询分析任务状态。
   * 深度音频分析要跑几分钟，所以这里轮询而不是等接口返回。
   */
  function startStatusPolling(assetId, jobId) {
    stopStatusPolling();
    const tick = async () => {
      try {
        const q = jobId ? `?job_id=${encodeURIComponent(jobId)}` : "";
        const data = await api(
          `/api/assets/${encodeURIComponent(assetId)}/analysis-status${q}`);
        if (state.current && state.current.id !== assetId) return; // 抽屉换了
        renderAnalysisStatus(data);
        const status = data.status || "idle";
        if (status === "done") {
          toast("分析完成", "ok");
          loadAssets();
          openDrawer(assetId);
          return;
        }
        if (status === "failed") {
          toast(data.message || "分析失败", "err");
          loadAssets();
          openDrawer(assetId);
          return;
        }
        _pollTimer = setTimeout(tick, 2000);
      } catch (e) {
        // 轮询失败不打断任务，只是这次不更新
        _pollTimer = setTimeout(tick, 3000);
      }
    };
    tick();
  }

  function stopStatusPolling() {
    if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
  }

  function bindDrawer(asset) {
    const on = (sel, event, fn) => {
      const node = $(sel);
      if (node) node.addEventListener(event, fn);
    };

    on("#alSaveName", "click", async () => {
      const name = $("#alNameInput").value.trim();
      if (!name) { toast("名称不能为空", "err"); return; }
      try {
        await api("/api/assets/" + encodeURIComponent(asset.id), {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        });
        toast("已保存", "ok");
        loadAssets(); loadTags();
        openDrawer(asset.id);
      } catch (e) { toast(e.message, "err"); }
    });

    const addTag = async () => {
      const raw = $("#alTagInput").value;
      const tags = raw.split(/[,，]/).map(t => t.trim()).filter(Boolean);
      if (!tags.length) { toast("请输入标签", "err"); return; }
      try {
        await api(`/api/assets/${encodeURIComponent(asset.id)}/tags`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tags }),
        });
        $("#alTagInput").value = "";
        toast("标签已添加", "ok");
        loadAssets(); loadTags();
        openDrawer(asset.id);
      } catch (e) { toast(e.message, "err"); }
    };
    on("#alAddTag", "click", addTag);
    on("#alTagInput", "keydown", event => { if (event.key === "Enter") addTag(); });

    $$("#alTagList button[data-rmtag]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const tag = btn.dataset.rmtag;
        try {
          await api(`/api/assets/${encodeURIComponent(asset.id)}/tags/${encodeURIComponent(tag)}`,
            { method: "DELETE" });
          toast("标签已删除", "ok");
          loadAssets(); loadTags();
          openDrawer(asset.id);
        } catch (e) { toast(e.message, "err"); }
      });
    });

    on("#alAnalyzeBtn", "click", async () => {
      const btn = $("#alAnalyzeBtn");
      const deepBox = $("#alDeepAudio");
      const deep = !!(deepBox && deepBox.checked);
      btn.disabled = true;
      btn.textContent = "提交中…";
      try {
        const result = await api(`/api/assets/${encodeURIComponent(asset.id)}/analyze`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ deep }),
        });
        if (result.already_running) {
          toast("该素材已有分析任务在进行中", "");
        } else {
          toast("分析任务已提交，后台执行中", "ok");
        }
        startStatusPolling(asset.id, result.job_id);
      } catch (e) {
        toast(e.message, "err");
        btn.disabled = false;
        btn.textContent = "深度分析";
      }
    });

    on("#alCorrectBtn", "click", async () => {
      const question = $("#alCorrectionInput").value.trim();
      if (!question) { toast("请填写要纠正或追问的内容", "err"); return; }
      const btn = $("#alCorrectBtn");
      btn.disabled = true;
      btn.textContent = "提交中…";
      try {
        const result = await api(`/api/assets/${encodeURIComponent(asset.id)}/corrections`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question }),
        });
        if (result.already_running) {
          toast("该素材已有分析任务在进行中", "");
        } else {
          toast("纠错任务已提交，后台执行中", "ok");
        }
        startStatusPolling(asset.id, result.job_id);
      } catch (e) {
        toast(e.message, "err");
        btn.disabled = false;
        btn.textContent = "提交纠错";
      }
    });

    on("#alHistoryBtn", "click", async () => {
      try {
        const data = await api(`/api/assets/${encodeURIComponent(asset.id)}/analyses`);
        const rows = (data.analyses || []).map(item =>
          `<div class="al-ref" style="display:block">
             <b style="color:var(--text)">第 ${item.revision} 版</b>
             <span style="float:right">${esc(fmtTime(item.created_at))}</span>
             <div style="margin-top:4px;white-space:pre-wrap">${esc((item.text || "").slice(0, 600))}</div>
           </div>`).join("");
        toast("修订历史见下方");
        const box = $("#alAnalysisText");
        if (box) box.innerHTML = rows || "（无历史）";
      } catch (e) { toast(e.message, "err"); }
    });

    on("#alDeleteBtn", "click", async () => {
      try {
        await api("/api/assets/" + encodeURIComponent(asset.id), { method: "DELETE" });
        toast("已移到回收站", "ok");
        closeDrawer();
        loadAssets(); loadTags();
      } catch (e) {
        if (e.status === 409) {
          const refs = (e.payload && e.payload.refs) || [];
          const lines = refs.slice(0, 6).map(r => `${r.label || r.ref_id}（${r.slot}）`);
          alert(`这个素材正在被引用，不能直接删除：\n\n${lines.join("\n")}\n\n`
            + "提示：可以先删除引用它的任务，或等引用解除后再删。");
        } else {
          toast(e.message, "err");
        }
      }
    });

    on("#alRestoreBtn", "click", async () => {
      try {
        await api(`/api/assets/${encodeURIComponent(asset.id)}/restore`, { method: "POST" });
        toast("已还原", "ok");
        loadAssets();
        openDrawer(asset.id);
      } catch (e) { toast(e.message, "err"); }
    });
  }

  // ---------------------------------------------------------------- 右键菜单（素材库卡片）

  function showAssetContext(event, assetId) {
    const menu = $("#alContext");
    if (!menu) return;
    const asset = state.items.find(a => a.id === assetId);
    menu.innerHTML = `
      <button data-act="open">查看详情</button>
      <button data-act="copy-id">复制素材 ID</button>
      <button data-act="download">下载原文件</button>
      <div class="ctx-sep"></div>
      <button data-act="analyze">深度分析</button>
      <button data-act="delete" class="danger">删除</button>`;
    menu.classList.add("open");
    positionMenu(menu, event.clientX, event.clientY);

    menu.querySelectorAll("button[data-act]").forEach(btn => {
      btn.addEventListener("click", async () => {
        menu.classList.remove("open");
        const act = btn.dataset.act;
        if (act === "open") openDrawer(assetId);
        else if (act === "copy-id") {
          try { await navigator.clipboard.writeText(assetId); toast("已复制素材 ID", "ok"); }
          catch (e) { toast(assetId); }
        } else if (act === "download" && asset) {
          window.open(asset.content_url, "_blank");
        } else if (act === "analyze") {
          try {
            toast("分析中…");
            await api(`/api/assets/${encodeURIComponent(assetId)}/analyze`, {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({}),
            });
            toast("分析完成", "ok");
            loadAssets();
          } catch (e) { toast(e.message, "err"); }
        } else if (act === "delete") {
          try {
            await api("/api/assets/" + encodeURIComponent(assetId), { method: "DELETE" });
            toast("已移到回收站", "ok");
            loadAssets(); loadTags();
          } catch (e) {
            if (e.status === 409) {
              const refs = (e.payload && e.payload.refs) || [];
              alert("这个素材正在被引用，不能删除：\n\n"
                + refs.slice(0, 6).map(r => `${r.label || r.ref_id}（${r.slot}）`).join("\n"));
            } else toast(e.message, "err");
          }
        }
      });
    });
  }

  function positionMenu(menu, x, y) {
    menu.style.left = "0px";
    menu.style.top = "0px";
    const rect = menu.getBoundingClientRect();
    const left = Math.min(x, window.innerWidth - rect.width - 8);
    const top = Math.min(y, window.innerHeight - rect.height - 8);
    menu.style.left = Math.max(4, left) + "px";
    menu.style.top = Math.max(4, top) + "px";
  }

  // ---------------------------------------------------------------- 上传

  async function uploadFiles(fileList) {
    const files = [...fileList];
    if (!files.length) return;
    // 上传时的标签/备注（页面上那两个输入框）
    const tagInput = $("#alUploadTags");
    const noteInput = $("#alUploadNote");
    const options = {
      tags: tagInput ? tagInput.value.trim() : "",
      note: noteInput ? noteInput.value.trim() : "",
    };
    const box = $("#alUploads");
    const ids = [];
    files.forEach(file => {
      const id = "up_" + Math.random().toString(36).slice(2);
      ids.push(id);
      state.uploads.set(id, { name: file.name, state: "排队中", percent: 0, kind: "" });
    });
    renderUploads();

    for (let i = 0; i < files.length; i += 1) {
      const id = ids[i], file = files[i];
      const entry = state.uploads.get(id);
      try {
        const body = new FormData();
        // 选项字段必须**先于**文件提交：
        // 服务端第一遍读完整表单再统一入库，选项在文件之后也能生效；
        // 但放在前面更符合「先说清参数、再传文件」的语义，也便于排查。
        if (options.name) body.append("name", options.name);
        if (options.category) body.append("category", options.category);
        if (options.subtype) body.append("subtype", options.subtype);
        if (options.tags) body.append("tags", options.tags);
        if (options.note) body.append("note", options.note);
        body.append("file", file, file.name);
        const resp = await fetch("/api/assets", { method: "POST", body });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || "上传失败");
        const okCount = (data.results || []).filter(r => r.ok).length;
        const dupCount = (data.results || []).filter(r => r.duplicated).length;
        entry.state = dupCount
          ? `已存在，复用原素材${okCount ? `（另 ${okCount} 个成功）` : ""}`
          : `已入库${okCount > 1 ? `（共 ${okCount} 个）` : ""}`;
        entry.kind = "ok";
        entry.percent = 100;
        if ((data.errors || []).length) {
          entry.state += `；${data.errors.length} 个失败`;
          entry.kind = "err";
        }
      } catch (e) {
        entry.state = e.message;
        entry.kind = "err";
        entry.percent = 100;
      }
      renderUploads();
    }
    loadAssets(); loadTags();
    toast("上传处理完成", "ok");
  }

  function renderUploads() {
    const box = $("#alUploads");
    if (!box) return;
    const entries = [...state.uploads.entries()];
    if (!entries.length) { box.innerHTML = ""; box.hidden = true; return; }
    box.hidden = false;
    box.innerHTML = entries.map(([id, entry]) => `
      <div class="al-upload">
        <div class="up-row">
          <span class="up-name" title="${esc(entry.name)}">${esc(entry.name)}</span>
          <span class="up-state ${entry.kind}">${esc(entry.state)}</span>
        </div>
        <div class="up-bar"><div style="width:${entry.percent}%"></div></div>
      </div>`).join("");
  }

  // ---------------------------------------------------------------- 素材选择器

  function openPicker(options) {
    const mask = $("#alPickerMask");
    if (!mask) return;
    state.picker = Object.assign({
      categories: [], slots: [], multiple: true, limit: 9,
      picked: [], onDone: null,
    }, options || {});
    mask.classList.add("open");
    loadPickerItems();
  }

  function closePicker() {
    const mask = $("#alPickerMask");
    if (mask) mask.classList.remove("open");
    state.picker = null;
  }

  async function loadPickerItems() {
    const body = $("#alPickerBody");
    const cfg = state.picker || {};
    const cats = cfg.categories || [];
    const params = new URLSearchParams({ limit: "200" });
    if (cats.length === 1) params.set("category", cats[0]);
    body.innerHTML = `<div class="al-empty">加载中…</div>`;
    try {
      const data = await api("/api/assets/picker?" + params.toString());
      let items = data.assets || [];
      if (cats.length > 1) {
        items = items.filter(a => cats.includes(a.category));
      }
      body.innerHTML = items.length ? items.map(asset => `
        <div class="al-card ${cfg.picked.includes(asset.id) ? "picked" : ""}" data-id="${esc(asset.id)}">
          <div class="al-thumb">${asset.preview_url
            ? `<img src="${esc(asset.preview_url)}" alt="${esc(asset.name)}" loading="lazy">`
            : `<span class="ph">${CATEGORY_ICON[asset.category] || "📦"}</span>`}</div>
          <div class="al-card-body">
            <div class="al-name" title="${esc(asset.name)}">${esc(asset.name)}</div>
            <div class="al-meta"><span>${esc(CATEGORY_LABEL[asset.category] || asset.category)}</span>
              ${asset.duration_sec != null ? `<span>${esc(fmtDuration(asset.duration_sec))}</span>` : ""}</div>
          </div>
        </div>`).join("")
        : `<div class="al-empty">没有可选的素材</div>`;
      body.querySelectorAll(".al-card").forEach(card => {
        card.addEventListener("click", () => {
          const id = card.dataset.id;
          if (!cfg.multiple) {
            cfg.picked = [id];
          } else if (cfg.picked.includes(id)) {
            cfg.picked = cfg.picked.filter(x => x !== id);
          } else {
            if (cfg.picked.length >= (cfg.limit || 9)) {
              toast(`最多只能选 ${cfg.limit || 9} 个`, "err");
              return;
            }
            cfg.picked.push(id);
          }
          card.classList.toggle("picked", cfg.picked.includes(id));
          updatePickerFoot();
        });
      });
      updatePickerFoot();
    } catch (e) {
      body.innerHTML = `<div class="al-empty">加载失败：${esc(e.message)}</div>`;
    }
  }

  function updatePickerFoot() {
    const foot = $("#alPickerFoot");
    const cfg = state.picker || {};
    if (foot) {
      foot.innerHTML = `<span class="hint">已选 ${cfg.picked.length} / ${cfg.limit || 9} 个</span>
        <span class="al-inline">
          <button class="al-btn" id="alPickerCancel">取消</button>
          <button class="al-btn primary" id="alPickerOk">确定使用</button>
        </span>`;
      $("#alPickerCancel").addEventListener("click", closePicker);
      $("#alPickerOk").addEventListener("click", () => {
        const picked = cfg.picked.slice();
        const done = cfg.onDone;
        closePicker();
        if (done) done(picked);
      });
    }
  }

  // ---------------------------------------------------------------- 媒体库右键入库

  /**
   * 从生成结果（媒体库）添加到素材库。
   * payload = {job_id, batch_id?, file?, name?}
   */
  async function addFromMedia(payload) {
    try {
      const result = await api("/api/assets/import/from-media", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      toast(result.message || "已添加到素材库", "ok");
      loadAssets(); loadTags();
      return result;
    } catch (e) {
      toast(e.message, "err");
      return null;
    }
  }

  // ---------------------------------------------------------------- 页面切换

  async function showAssets() {
    if (typeof window.showCreate === "function") {
      // 复用主面板的隐藏逻辑：素材库页独占主区域
      window.showCreate();
    }
    const page = $("#assetsPage");
    if (page) page.style.display = "flex";
    const jobs = $("#jobsArea");
    if (jobs) jobs.style.display = "none";
    const panel = $("#panel");
    if (panel) panel.style.display = "none";
    const stitch = $("#stitchPanel");
    if (stitch) stitch.style.display = "none";
    const system = $("#systemPage");
    if (system) system.style.display = "none";
    const providers = $("#providersPage");
    if (providers) providers.style.display = "none";
    $$(".nav-item").forEach(x => x.classList.remove("active"));
    const nav = $("#navAssets");
    if (nav) nav.classList.add("active");
    if (window.ASSETLIB_setPage) window.ASSETLIB_setPage("assets");
    await loadAssets();
    await loadTags();
    await loadStats();
  }

  async function loadStats() {
    const box = $("#alStats");
    if (!box) return;
    try {
      const data = await api("/api/assets/stats");
      const cat = (data.by_category || []).map(c =>
        `${CATEGORY_LABEL[c.category] || c.category} ${c.count}`).join(" · ");
      box.textContent = `共 ${data.total_count} 个素材 · ${fmtBytes(data.total_bytes)}`
        + (cat ? ` · ${cat}` : "")
        + ` · 已分析 ${data.analyzed_count} 个`
        + ` · 回收站 ${data.trash_count} 个`;
    } catch (e) {
      box.textContent = "素材库状态读取失败：" + e.message;
    }
  }

  // ---------------------------------------------------------------- 初始化

  function bindStatic() {
    const nav = $("#navAssets");
    if (nav) nav.addEventListener("click", showAssets);

    const search = $("#alSearch");
    if (search) {
      let timer = null;
      search.addEventListener("input", () => {
        clearTimeout(timer);
        timer = setTimeout(() => {
          state.filters.q = search.value.trim();
          state.filters.page = 1;
          loadAssets();
        }, 260);
      });
    }

    const uploadBtn = $("#alUploadBtn");
    if (uploadBtn) uploadBtn.addEventListener("click", () => $("#alFileInput").click());
    const fileInput = $("#alFileInput");
    if (fileInput) {
      fileInput.addEventListener("change", event => {
        uploadFiles(event.target.files);
        event.target.value = "";
      });
    }

    const drop = $("#alDrop");
    if (drop) {
      ["dragenter", "dragover"].forEach(evt => drop.addEventListener(evt, event => {
        event.preventDefault();
        drop.classList.add("over");
      }));
      ["dragleave", "drop"].forEach(evt => drop.addEventListener(evt, event => {
        event.preventDefault();
        drop.classList.remove("over");
      }));
      drop.addEventListener("drop", event => {
        if (event.dataTransfer.files && event.dataTransfer.files.length) {
          uploadFiles(event.dataTransfer.files);
        }
      });
    }

    const sourceSel = $("#alSource");
    if (sourceSel) {
      sourceSel.addEventListener("change", () => {
        state.filters.source = sourceSel.value;
        state.filters.page = 1;
        loadAssets();
      });
    }
    const sortSel = $("#alSort");
    if (sortSel) {
      sortSel.addEventListener("change", () => {
        state.filters.sort = sortSel.value;
        state.filters.page = 1;
        loadAssets();
      });
    }
    const trashBtn = $("#alTrashBtn");
    if (trashBtn) {
      trashBtn.addEventListener("click", () => {
        const on = state.filters.in_trash === "1";
        state.filters.in_trash = on ? "" : "1";
        state.filters.page = 1;
        trashBtn.classList.toggle("active", !on);
        trashBtn.textContent = on ? "回收站" : "查看回收站";
        loadAssets();
      });
    }
    const refBtn = $("#alRefBtn");
    if (refBtn) {
      refBtn.addEventListener("click", () => {
        const on = state.filters.referenced === "1";
        state.filters.referenced = on ? "" : "1";
        state.filters.page = 1;
        refBtn.classList.toggle("active", !on);
        loadAssets();
      });
    }

    const tagToggle = $("#alTagToggle");
    const tagPick = $("#alTagPick");
    if (tagToggle && tagPick) {
      tagToggle.addEventListener("click", event => {
        event.stopPropagation();
        tagPick.classList.toggle("open");
      });
      document.addEventListener("click", event => {
        if (!tagPick.contains(event.target)) tagPick.classList.remove("open");
      });
    }

    const drawerClose = $("#alDrawerClose");
    if (drawerClose) drawerClose.addEventListener("click", closeDrawer);
    const drawerMask = $("#alDrawerMask");
    if (drawerMask) drawerMask.addEventListener("click", closeDrawer);

    const pickerMask = $("#alPickerMask");
    if (pickerMask) {
      pickerMask.addEventListener("click", event => {
        if (event.target === pickerMask) closePicker();
      });
    }

    document.addEventListener("click", event => {
      const menu = $("#alContext");
      if (menu && !menu.contains(event.target)) menu.classList.remove("open");
      const ctx = $("#mediaContext");
      if (ctx && !ctx.contains(event.target)) ctx.classList.remove("open");
    });

    document.addEventListener("keydown", event => {
      if (event.key !== "Escape") return;
      closeDrawer();
      closePicker();
      const menu = $("#alContext");
      if (menu) menu.classList.remove("open");
      const ctx = $("#mediaContext");
      if (ctx) ctx.classList.remove("open");
    });
  }

  function boot() {
    bindStatic();
    renderChips();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  // 对外接口（供 app.js 调用）
  window.ASSETLIB = {
    showAssets,
    addFromMedia,
    openPicker,
    loadAssets,
    toast,
    esc,
  };
})();
