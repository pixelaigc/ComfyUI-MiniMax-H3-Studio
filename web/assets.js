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
    // 【2026-11-14 需求 13】多选集合（素材 id）。
    // 用 Set 而不是数组：增删查都是 O(1)，且天然去重。
    selected: new Set(),
    // 【2026-11-15 A3 补漏】多选**模式**开关。
    //
    // 【为什么要有这个开关】需求原话：「提供「多选」按钮，点击后卡片才显示
    // 复选框，默认不显示」。原来的实现把复选框**直接写进卡片 HTML**，
    // 于是每张卡默认都带一个勾选框 —— 与需求相反，而且用户点卡片
    // （看详情）时会被勾选框抢走点击。
    // 现在复选框**默认不渲染**；点「多选」按钮进入多选模式后才渲染。
    multiMode: false,
  };

  // ---------------------------------------------------------------- 多选（需求 13）

  /**
   * 多选状态的唯一入口：改完必须调 renderSelection() 把界面同步上。
   *
   * 【为什么要多选】素材库一屏 60 张，逐张点删除不现实。
   * 勾选 -> 顶部出现「已选 N 项 / 取消选择 / 全选当前页 / 删除所选」。
   */
  function selectedIds() {
    return Array.from(state.selected);
  }

  function toggleSelected(assetId, on) {
    if (!assetId) return;
    const has = state.selected.has(assetId);
    const want = on === undefined ? !has : !!on;
    if (want) state.selected.add(assetId);
    else state.selected.delete(assetId);
    renderSelection();
  }

  function clearSelection() {
    state.selected.clear();
    renderSelection();
  }

  /**
   * 【2026-11-15 A3 补漏】多选模式开关。
   *
   * 【语义】默认关闭 -> 卡片**不渲染**复选框；打开 -> 卡片渲染复选框。
   * 关闭时**清空**已选（否则退出多选模式后卡片上留着「已选」高亮，
   * 用户会以为还在多选中）。
   *
   * 【为什么必须重渲染网格】复选框是卡片 HTML 的一部分（见卡片模板），
   * 开关一变就得重新画一遍卡片 —— 只切 CSS class 做不到「不渲染」。
   */
  function setMultiMode(on) {
    const next = !!on;
    if (state.multiMode === next) return;
    state.multiMode = next;
    if (!next) state.selected.clear();
    renderSelection();
    // 网格重画：复选框是卡片 HTML 的一部分，多选模式一变就要重建卡片。
    // （assetSig 里带了 multiMode -> 增量渲染会重建所有卡片。）
    renderAssets();
  }

  function toggleMultiMode() {
    setMultiMode(!state.multiMode);
  }

  /** 全选当前页（当前筛选下已加载的那些）。 */
  function selectAllVisible() {
    (state.items || []).forEach(a => state.selected.add(a.id));
    renderSelection();
  }

  /**
   * 把多选状态同步到界面：
   *   * 顶部选择条（已选 N 项 + 三个按钮）
   *   * 每张卡上的勾选框状态
   */
  function renderSelection() {
    const bar = $("#alSelBar");
    const count = $("#alSelCount");
    const n = state.selected.size;
    // 【2026-11-15 A3 补漏】多选模式下选择条**始终可见**（哪怕还没勾任何），
    // 否则用户点了「多选」按钮却看不到任何变化，会以为按钮坏了。
    // 非多选模式下仍然「没勾选就隐藏」—— 不占视觉空间。
    if (bar) bar.hidden = !(state.multiMode || n > 0);
    if (count) count.textContent = `已选 ${n} 项`;
    // 多选按钮自身的状态（文案 + aria-pressed + 高亮）
    const multiToggle = $("#alMultiToggle");
    if (multiToggle) {
      multiToggle.setAttribute("aria-pressed", state.multiMode ? "true" : "false");
      multiToggle.textContent = state.multiMode ? "多选中 ✓" : "多选";
      multiToggle.classList.toggle("primary", state.multiMode);
    }
    // 同步每张卡的勾选态
    $$("#alGrid .al-card").forEach(card => {
      const id = card.dataset.id;
      const box = card.querySelector(".al-selbox");
      if (box) box.checked = state.selected.has(id);
      card.classList.toggle("selected", state.selected.has(id));
    });
  }

  /**
   * 批量删除（需求 13）。
   *
   * 【为什么要逐个删而不是一个批量接口】
   * 服务端只有单条删除接口（DELETE /api/assets/{id}）。
   * 逐条调用的好处：服务端对「被引用」的素材会返回 409 + 引用清单，
   * 逐条调能**如实告诉用户哪几条删不掉、为什么**。
   *
   * 【确认】删之前必须确认 —— 这是不可逆操作（进回收站，可还原）。
   */
  async function deleteSelected() {
    const ids = selectedIds();
    if (!ids.length) return;
    const names = ids.map(id => {
      const a = (state.items || []).find(x => x.id === id);
      return a ? a.name : id;
    });
    const preview = names.slice(0, 8).join("、")
      + (names.length > 8 ? ` 等 ${names.length} 项` : "");
    if (!window.confirm(`确认删除选中的 ${ids.length} 项素材？\n\n${preview}\n\n（会移到回收站，可从回收站还原）`)) {
      return;
    }
    let ok = 0;
    const blocked = [];
    for (const id of ids) {
      try {
        await api("/api/assets/" + encodeURIComponent(id), { method: "DELETE" });
        ok++;
      } catch (e) {
        if (e.status === 409) {
          const refs = (e.payload && e.payload.refs) || [];
          const a = (state.items || []).find(x => x.id === id);
          blocked.push(`${a ? a.name : id}（被 ${refs.length} 个任务引用）`);
        } else {
          blocked.push(`${id}（${e.message || "删除失败"}）`);
        }
      }
    }
    state.selected.clear();
    renderSelection();
    loadAssets();
    if (blocked.length) {
      toast(`已删除 ${ok} 项；${blocked.length} 项没删掉：${blocked.slice(0, 3).join("；")}`, "err");
      alert("以下素材没能删除：\n\n" + blocked.join("\n")
        + "\n\n被任务引用的素材需要先删除引用它的任务。");
    } else {
      toast(`已删除 ${ok} 项素材（已移到回收站）`, "ok");
    }
  }

  // ---------------------------------------------------------------- 播放器清单（A2）
  //
  // 【为什么「添加到播放器」必须落到服务端】
  //   播放列表是用户**主动**挑出来的一组东西，必须：
  //     * 跨页面（关掉素材库再回来还在）
  //     * 跨重启（服务重启后还在）
  //     * 有序（添加顺序 = 播放顺序）
  //   如果只存在前端的 Set 里，一刷新就没了 —— 这正是老播放器
  //   「播放列表 = 当前页全部素材」的病根。

  /**
   * 把选中的素材加进播放器清单。
   *
   * 【为什么逐个调接口而不是一个批量接口】
   *   服务端接口本身就是批量的（asset_ids 数组），这里一次调用即可。
   *   返回的 added/skipped/missing 要**如实**告诉用户 ——
   *   「3 项已在清单中，1 项不存在」比笼统的「已添加」有用得多。
   */
  async function addToPlayer(assetIds) {
    const ids = (assetIds || []).filter(Boolean);
    if (!ids.length) { toast("没有可添加的素材", "err"); return null; }
    try {
      const result = await api("/api/assets/player/add", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ asset_ids: ids }),
      });
      toast(result.message || "已添加到播放器", "ok");
      // 清单变了，播放器面板里的计数也要跟着变（如果它正开着）。
      if (window.AssetPlayer && window.AssetPlayer.loadPlaylist) {
        await window.AssetPlayer.loadPlaylist();
        window.AssetPlayer.render();
      }
      return result;
    } catch (e) {
      toast(e.message || "添加到播放器失败", "err");
      return null;
    }
  }

  /** 把选中的素材从播放器清单移除。 */
  async function removeFromPlayer(assetIds) {
    const ids = (assetIds || []).filter(Boolean);
    if (!ids.length) { toast("没有可移除的素材", "err"); return null; }
    try {
      const result = await api("/api/assets/player/remove", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ asset_ids: ids }),
      });
      toast(`已从播放器移除 ${(result.removed || []).length} 项`, "ok");
      if (window.AssetPlayer && window.AssetPlayer.loadPlaylist) {
        await window.AssetPlayer.loadPlaylist();
        window.AssetPlayer.render();
      }
      return result;
    } catch (e) {
      toast(e.message || "移除失败", "err");
      return null;
    }
  }

  /** 多选条上的「添加到播放器」：把当前选中的全部加进去。 */
  async function addSelectedToPlayer() {
    const ids = selectedIds();
    if (!ids.length) { toast("请先勾选素材", "err"); return; }
    await addToPlayer(ids);
  }

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

  // ---------------------------------------------------------------- 独立查看窗口

  /**
   * 在**独立窗口**（覆盖式查看器）里观察 / 播放单个素材。
   *
   * 【为什么复用既有组件，而不是自己写一套】
   * 生成媒体库早就有这个能力了，它的实现是：
   *   * 图片 -> app.js 的 #lightbox（可缩放 / 拖拽 / 适应屏幕 / 原始尺寸 /
   *     滚轮缩放 / 下载 / 新窗口打开 / Esc 关闭）
   *   * 视频 / 音频 -> web/media-preview.js 的模态框（原生 controls：
   *     播放 / 暂停 / 静音 / 进度 / 音量）
   * 那个入口是 `window.MediaPreview.show({url, kind, name})`
   * （见 web/media-preview.js 的对外 API）。
   * 素材库**直接调它** —— 于是两边的视觉与操作习惯完全一致
   * （同一个 lightbox、同一套关闭方式含 Esc），
   * 而不是另起一个「功能更少、样式还得再对一遍」的互斥实现。
   *
   * 【kind 必须显式传】
   * content_url 是 /api/assets/<id>/content（**没有扩展名**），
   * 按 URL 猜类型会猜错。素材的 category 是权威值 ——
   * 这正是 media-preview.js 里 detectKind 的注释写明的那条坑。
   *
   * 【不误触发其它动作】
   * 这个函数**只**打开查看器：不改多选集合、不动播放器清单、
   * 不引用到生成面板。调用方（卡片 / 抽屉 / 右键菜单）负责
   * 把自己的事件 stopPropagation 掉。
   */
  function openAssetViewer(asset) {
    if (!asset) return false;
    const preview = window.MediaPreview;
    if (!preview || typeof preview.show !== "function") {
      toast("独立查看器未加载", "err");
      return false;
    }
    // 图片 / 视频 / 音频都有原文件地址；其它类型（文档 / 工作流）没有可播内容。
    const url = asset.content_url
      || (asset.id ? `/api/assets/${encodeURIComponent(asset.id)}/content` : "")
      || asset.preview_url;
    if (!url) {
      toast("该素材没有可查看的文件", "err");
      return false;
    }
    return preview.show({
      url,
      kind: asset.category,
      name: asset.name,
      mime: asset.mime,
      category: asset.category,
    });
  }

  /** 能否在独立窗口里看（图片 / 视频 / 音频）。 */
  function isViewableAsset(asset) {
    if (!asset) return false;
    return asset.category === "image"
      || asset.category === "video"
      || asset.category === "audio";
  }

  /**
   * 独立查看器当前是否开着。
   *
   * 【为什么需要这个判据】
   * 独立查看器有两套 DOM：图片走 app.js 的 #lightbox，
   * 视频 / 音频走 media-preview.js 的 #mediaPreviewModal。
   * 素材库自己的 Esc 处理器必须知道「上面盖着一个查看器」，
   * 否则按 Esc 会把底下的抽屉一起关掉（见 bindStatic 里的注释）。
   */
  function isIndependentViewerOpen() {
    const lightbox = $("#lightbox");
    if (lightbox && !lightbox.hidden) return true;
    const modal = $("#mediaPreviewModal");
    if (modal && !modal.hidden) return true;
    return false;
  }

  /**
   * 「按 Esc 那一刻，查看器是不是开着」。
   *
   * 【为什么不能直接在冒泡阶段查 isIndependentViewerOpen()】
   * 三个 Escape 监听器都挂在 document 上，而且都是**冒泡**阶段：
   *   * app.js          —— 监听 #lightbox（图片分支）
   *   * assets.js       —— 关抽屉 / 选择器 / 右键菜单
   *   * media-preview.js—— 监听 #mediaPreviewModal（视频/音频分支）
   * 脚本在 index.html 里的顺序是 media-preview.js(1567) -> app.js(1626)
   * -> assets.js(1631)，即 **app.js 先注册**。
   * 冒泡阶段按注册顺序执行 ⇒ app.js 的处理器**先**跑，
   * 它已经把 `lightbox.hidden = true` 了 —— 等 assets.js 的处理器跑到时
   * 再去查，得到的已经是「没开」。
   * ⇒ 结果：用户在图片查看器里按 Esc，抽屉也被一起关掉。
   *
   * 【解法】在**捕获阶段**先记一份快照。
   * 捕获阶段一定早于任何冒泡阶段的监听器（无论注册顺序），
   * 所以那一刻 lightbox 还开着 —— 快照是准的。
   */
  let viewerOpenAtEscape = false;

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
    // 「加载中…」占位只在**首次**（还没有任何卡片）时铺。
    // 原来每次刷新都先清空网格再铺占位 —— 刷新期间列表整个消失，
    // 滚动位置归零、已展开的卡片被抹掉。
    if (grid && !grid.querySelector(".al-card")) {
      grid.innerHTML = `<div class="al-empty">加载中…</div>`;
    }
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

  /**
   * 素材详情里的「注意力机制」行（2026-11-12 新增）。
   *
   * 【为什么】用户要求「媒体库/生成库标注所用注意力机制」。
   * 素材库里从生成结果入库的视频条目，source_meta.attention 记着
   * 生成时用的机制 key，attention_label 是它的显示名
   * （入库时由服务端 job_meta 按清单算好，逐字与面板下拉一致）。
   * 没有记录的老素材 -> **整行不渲染**（不编造）。
   */
  function attentionMetaRow(sourceMeta) {
    const meta = sourceMeta || {};
    const label = meta.attention_label || "";
    if (!label) return "";
    return `<div class="kv"><span>注意力机制</span>
      <b title="${esc(label)}">${esc(label)}</b></div>`;
  }

  function thumbFor(asset) {
    if (asset.preview_url) {
      return `<img src="${esc(asset.preview_url)}" alt="${esc(asset.name)}" loading="lazy">`;
    }
    return `<span class="ph">${CATEGORY_ICON[asset.category] || "📦"}</span>`;
  }

  // 单张素材卡的 HTML（同时用于增量渲染与首次渲染）
  function assetCardHtml(asset) {
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
    // 多选勾选框：**只在多选模式下**才渲染。
    // 默认不显示 —— 用户点「多选」按钮进入多选模式后才出现。
    // 放在缩略图左下角，单独一个 .al-selbox-wrap，
    // 避免和卡片的「点开详情」冲突。
    //
    // 【独立查看入口 · 2026-11-21】
    // 缩略图右上角的「放大」按钮 —— 点它在**独立窗口**里看这个素材。
    //
    // 【为什么放缩略图上而不是只放抽屉里】
    // 需求要求「卡片与抽屉两条路径上都可用」，且「不得只藏在隐藏入口里」。
    // 缩略图上的按钮是**常驻可见**的（不靠 hover 显隐 —— 触屏没有 hover）。
    //
    // 【位置为什么不放四角】
    // 缩略图四个角已被占满：
    //   左上 .al-thumb .badge      = 「已分析」
    //   右上 .al-thumb .badge.right= 「引用 N」
    //   右下 .al-thumb .dur        = 「时长」（视频）
    //   左下 .al-selbox-wrap       = 「多选勾选框」
    // ⇒ 放在**右上角偏下**（top: 30px），落在「引用 N」徽章下方，不重叠。
    const viewBtn = isViewableAsset(asset)
      ? `<button class="al-view-btn" type="button" data-act="view"
                 title="在独立窗口查看（放大观察 / 播放）"
                 aria-label="在独立窗口查看 ${esc(asset.name)}">
           <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M7 2H2v5M9 2h5v5M9 14h5V9M7 14H2V9"
                 fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
         </button>` : "";
    return `<div class="al-card${state.selected.has(asset.id) ? " selected" : ""}" data-id="${esc(asset.id)}" draggable="true">
      <div class="al-thumb">${thumbFor(asset)}${analyzed}${inTrash}${refBadge}${badge}${viewBtn}
        ${state.multiMode ? `<label class="al-selbox-wrap" title="选中（可多选后批量删除）" onclick="return false;">
          <input type="checkbox" class="al-selbox" data-sel-id="${esc(asset.id)}"
                 ${state.selected.has(asset.id) ? "checked" : ""}
                 aria-label="选择 ${esc(asset.name)}">
        </label>` : ""}
      </div>
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
  }

  // 素材卡的内容签名：只有签名变了才重建这张卡。
  // 签名覆盖所有会显示在卡面上的字段 —— 少了任何一项都会导致
  // 「内容变了但没重绘」。
  function assetSig(asset) {
    return [
      asset.id, asset.name, asset.category, asset.bytes,
      asset.width || "", asset.height || "", asset.duration_sec ?? "",
      asset.ref_count || 0, asset.analyzed ? 1 : 0, asset.deleted_at || "",
      (asset.tags || []).join(","),
      // 【2026-11-14 需求 13】选中态也要进签名 ——
      // 否则「勾选后卡片被重建」时，勾选框会回到未选的样子
      // （增量渲染只在签名变化时重建卡片）。
      state.selected.has(asset.id) ? "sel" : "",
      // 【2026-11-15 A3 补漏】多选模式也要进签名 ——
      // 增量渲染只在签名变化时重建卡片；不进签名的话，
      // 开关多选模式后旧卡片会被复用，**复选框不会出现**。
      state.multiMode ? "multi" : "",
    ].join("|");
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
    // 增量渲染：按 data-id 做稳定 key，内容没变的卡片**保留原节点**。
    // 原实现是整块 innerHTML 重建 —— 每次刷新都把整页卡片换掉：
    //   焦点丢失、滚动位置跳、右键菜单锚定的卡片消失。
    const existing = new Map();
    // 非卡片节点（空态提示 / 加载中占位）先清掉，否则下标会算错
    for (const node of [...grid.children]) {
      if (node.dataset && node.dataset.id) existing.set(node.dataset.id, node);
      else node.remove();
    }
    const seen = new Set();
    const freshCards = [];
    state.items.forEach(asset => {
      seen.add(asset.id);
      const sig = assetSig(asset);
      let el = existing.get(asset.id);
      if (el && el.dataset.sig !== sig) {
        const fresh = document.createElement("div");
        fresh.innerHTML = assetCardHtml(asset);
        const node = fresh.firstElementChild;
        el.replaceWith(node);
        el = node;
        freshCards.push(el);
      } else if (!el) {
        const fresh = document.createElement("div");
        fresh.innerHTML = assetCardHtml(asset);
        el = fresh.firstElementChild;
        freshCards.push(el);
      }
      el.dataset.sig = sig;
      // 只有「位置真的错了」才搬动节点：位置正确 = 原样不动 = 焦点不丢。
      const atIndex = Array.prototype.indexOf.call(grid.children, el);
      if (atIndex !== seen.size - 1) {
        grid.insertBefore(el, grid.children[seen.size - 1] || null);
      }
    });
    // 已删除的卡片直接移除
    existing.forEach((el, id) => { if (!seen.has(id)) el.remove(); });
    // 只给**新建/重建**的卡片绑事件 —— 已有卡片保留原来的监听器，
    // 否则每次刷新都会再叠一层监听器（点一下触发两次）。
    bindAssetCards(freshCards);
  }

  // 给素材卡绑事件。
  // cards：不传 = 绑定网格里的全部卡片（首次渲染用）；
  // 传列表 = 只给这些「新建/重建」的卡绑（增量刷新用）。
  //
  // 为什么要区分：增量刷新会复用旧卡节点，旧节点上已经有监听器了。
  // 如果每次刷新都重新 bind 全部卡片，监听器会一层层叠上去 ——
  // 点一次卡片触发 N 次 openDrawer。
  function bindAssetCards(cards) {
    const list = cards || $$("#alGrid .al-card");
    list.forEach(card => {
      if (card.dataset.h3Bound) return;      // 已绑过就别再绑
      card.dataset.h3Bound = "1";
      const assetId = card.dataset.id;
      card.addEventListener("click", () => openDrawer(assetId));
      // 【独立查看入口 · 2026-11-21】缩略图上的「放大」按钮。
      //
      // 【必须 stopPropagation】否则这个 click 会冒泡到卡片的 click 处理器
      // -> 顺手把**抽屉也打开**了 —— 用户只想看大图，却多出一个抽屉。
      //
      // 【不碰多选】这个按钮不是勾选框，不改 state.selected。
      // 【不碰播放器】不调 addToPlayer。
      const viewBtn = card.querySelector(".al-view-btn");
      if (viewBtn) {
        viewBtn.addEventListener("click", event => {
          event.preventDefault();
          event.stopPropagation();
          const asset = state.items.find(a => a.id === assetId);
          if (!asset) return;
          openAssetViewer(asset);
        });
      }
      // 【2026-11-14 需求 13】勾选框：点它只切换选中，**不开详情**。
      // （勾选框在 .al-thumb 里，click 会冒泡到卡片 -> 打开详情抽屉，
      //   所以必须 stopPropagation。）
      //
      // 【为什么用 change 而不是 click 来改状态】
      // input[type=checkbox] 的 checked 在 click 处理器里**已经是新值**
      // （浏览器先改 checkedness 再派发 click），但如果此时调用
      // preventDefault()，浏览器会在处理器跑完之后**把 checked 还原**。
      // 于是：Set 里记了选中、DOM 上的框却显示没勾 —— 界面与状态不一致
      // （实测：selectedCards=3 但 checkedBoxes=2）。
      //
      // 正确做法：click 只做 stopPropagation（不阻止默认行为），
      // 真正改状态用 change —— 它在 checkedness 最终确定后才派发。
      const selBox = card.querySelector(".al-selbox");
      if (selBox) {
        selBox.addEventListener("click", event => {
          event.stopPropagation();
        });
        selBox.addEventListener("change", event => {
          event.stopPropagation();
          toggleSelected(assetId, selBox.checked);
        });
      }
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
    const current = state.filters.page;
    const buttons = [];
    for (let i = Math.max(1, current - 2); i <= Math.min(pages, current + 2); i += 1) {
      buttons.push(`<button data-page="${i}" class="${i === current ? "active" : ""}">${i}</button>`);
    }
    const html = `
      <button data-page="${current - 1}" ${current <= 1 ? "disabled" : ""}>上一页</button>
      ${buttons.join("")}
      <button data-page="${current + 1}" ${current >= pages ? "disabled" : ""}>下一页</button>
      <span class="page-summary">共 ${state.total} 个 · 第 ${current}/${pages} 页</span>`;
    // 内容没变就不动 DOM —— 换了节点等于把「上一页/下一页」换掉。
    const changed = window.H3UI
      ? window.H3UI.setHTMLIfChanged(pager, html)
      : (pager.innerHTML = html, true);
    pager.hidden = false;
    if (changed) {
      pager.querySelectorAll("button[data-page]").forEach(btn => {
        btn.addEventListener("click", () => {
          state.filters.page = Number(btn.dataset.page) || 1;
          loadAssets();
        });
      });
    }
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
    // 标签下拉里每一项的勾选状态由 state.filters.tags_any 决定。
    // 原来每次都整块重建 —— 用户正要点某个标签时，节点被换掉，
    // 点击落空、展开的菜单也会被顶掉。
    // 现在：内容没变就不动 DOM；变了也只改「哪些项被勾上」。
    const html = tags.map(item => `
      <label><input type="checkbox" data-tag="${esc(item.tag)}"
        ${state.filters.tags_any === item.tag ? "checked" : ""}>
        <span>${esc(item.tag)}</span><span class="tag-count">${item.count}</span></label>`).join("");
    const changed = window.H3UI
      ? window.H3UI.setHTMLIfChanged(menu, html)
      : (menu.innerHTML = html, true);
    if (changed) {
      menu.querySelectorAll("input[data-tag]").forEach(box => {
        box.addEventListener("change", () => {
          state.filters.tags_any = box.checked ? box.dataset.tag : "";
          state.filters.page = 1;
          loadAssets();
        });
      });
    }
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

    // 【2026-11-14 需求 4】详情面板要能**直接看到 / 播放**素材本身。
    //
    // 【原来的缺陷】预览只按「有没有 preview_url」二分：
    //   有 preview_url -> <img>（图片能看）
    //   没有           -> 只给 video 放播放器，**音频**掉进兜底分支
    //                    -> 只有一个 📦 占位符，**音频根本没法播**。
    // 而「没有 preview_url」在音频上是常态（缩略图只给图片生成）。
    //
    // 【现在的做法】按 category 分派：
    //   image -> <img> 用 preview_url（512 宽缩略图，省流量）
    //   video -> <video> 用 content_url（原始文件）
    //   audio -> <audio> 用 content_url（原始文件）
    //   其它 -> 占位符
    // 三类都能在详情里直接看 / 播。
    const contentUrl = asset.content_url || "";
    let preview;
    if (asset.category === "video") {
      preview = contentUrl
        ? `<video class="al-preview-video" src="${esc(contentUrl)}" controls preload="metadata"></video>`
        : `<span class="ph">🎬</span>`;
    } else if (asset.category === "audio") {
      preview = contentUrl
        ? `<audio class="al-preview-audio" src="${esc(contentUrl)}" controls preload="metadata"></audio>`
        : `<span class="ph">🎵</span>`;
    } else if (asset.preview_url) {
      preview = `<img class="al-preview-img" src="${esc(asset.preview_url)}"
                      alt="${esc(asset.name)}">`;
    } else if (contentUrl) {
      // 图片没有缩略图但有原图 —— 退而用原图
      preview = `<img class="al-preview-img" src="${esc(contentUrl)}"
                      alt="${esc(asset.name)}">`;
    } else {
      preview = `<span class="ph">${CATEGORY_ICON[asset.category] || "📦"}</span>`;
    }

    const tagsHtml = (asset.tags || []).map(tag =>
      `<span class="al-tag">${esc(tag)}<button data-rmtag="${esc(tag)}" title="删除标签">×</button></span>`
    ).join("");

    const refsHtml = refs.refs.length
      ? refs.refs.map(ref => `<div class="al-ref"><span>${esc(ref.label || ref.ref_id)}</span>
          <span class="slot">${esc(ref.slot)}</span></div>`).join("")
      : `<div class="al-ref"><span>还没有任务引用这个素材</span></div>`;

    const sourceMeta = asset.source_meta || {};
    // 【2026-11-12】attention / attention_label 由下面的「注意力机制」行
    // 单独展示（带中文名），不在这里以原始 key 重复出现。
    const metaRows = Object.entries(sourceMeta).filter(([k, v]) =>
      k !== "attention" && k !== "attention_label"
      && v !== null && v !== undefined && v !== "" && typeof v !== "object");
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
          ${attentionMetaRow(sourceMeta)}
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
        ${isViewableAsset(asset) ? `
        <button class="al-btn primary" id="alViewBtn"
                title="在独立窗口里放大观察 / 播放（Esc 关闭）">
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M7 2H2v5M9 2h5v5M9 14h5V9M7 14H2V9"
                fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
          独立窗口查看
        </button>` : ""}
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

    // 【独立查看入口 · 2026-11-21】抽屉里的「独立窗口查看」按钮。
    // 打开的是**覆盖式查看器**，不是抽屉 —— 抽屉本身保持开着，
    // 关掉查看器后用户还在详情页（这是抽屉 + 独立窗口并存的常规体验）。
    //
    // 【不误触发其它动作】只调 openAssetViewer：
    // 不改多选集合、不动播放器清单、不引用到生成面板。
    on("#alViewBtn", "click", event => {
      event.preventDefault();
      openAssetViewer(asset);
    });

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
    // 添加到播放器清单：播放器只播用户主动挑的条目
    menu.innerHTML = `
      <button data-act="open">查看详情</button>
      ${asset && isViewableAsset(asset)
        ? `<button data-act="view">独立窗口查看</button>` : ""}
      <button data-act="copy-id">复制素材 ID</button>
      <button data-act="download">下载原文件</button>
      <div class="ctx-sep"></div>
      <button data-act="add-player">▶ 添加到播放器</button>
      <button data-act="remove-player">✕ 从播放器移除</button>
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
        // 【独立查看入口 · 2026-11-21】右键菜单项。
        // 只打开查看器 —— 不加入播放器、不删除、不改多选。
        else if (act === "view" && asset) openAssetViewer(asset);
        else if (act === "copy-id") {
          try { await navigator.clipboard.writeText(assetId); toast("已复制素材 ID", "ok"); }
          catch (e) { toast(assetId); }
        } else if (act === "download" && asset) {
          window.open(asset.content_url, "_blank");
        } else if (act === "add-player") {
          await addToPlayer([assetId]);
        } else if (act === "remove-player") {
          await removeFromPlayer([assetId]);
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
    if (page) { page.classList.remove("page-hidden"); page.style.removeProperty("display"); }
    const jobs = $("#jobsArea");
    if (jobs) { jobs.classList.add("page-hidden"); jobs.style.removeProperty("display"); }
    const panel = $("#panel");
    if (panel) { panel.classList.add("page-hidden"); panel.style.removeProperty("display"); }
    const stitch = $("#stitchPanel");
    if (stitch) { stitch.classList.add("page-hidden"); stitch.style.removeProperty("display"); }
    const system = $("#systemPage");
    if (system) { system.classList.add("page-hidden"); system.style.removeProperty("display"); }
    const providers = $("#providersPage");
    if (providers) { providers.classList.add("page-hidden"); providers.style.removeProperty("display"); }
    const agentPage = $("#agentPage");
    if (agentPage) { agentPage.classList.add("page-hidden"); agentPage.style.removeProperty("display"); }
    $$(".nav-item").forEach(x => x.classList.remove("active"));
    const nav = $("#navAssets");
    if (nav) nav.classList.add("active");
    if (window.ASSETLIB_setPage) window.ASSETLIB_setPage("assets");
    // 素材库页没有参数面板 —— 窄屏的「参数」浮动按钮要收起来
    if (typeof window.updateResponsiveChrome === "function") window.updateResponsiveChrome();
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

    // ---- 多选（2026-11-14 需求 13）----
    // 【2026-11-15 A3 补漏】多选模式开关按钮。
    const multiToggle = $("#alMultiToggle");
    if (multiToggle) {
      multiToggle.addEventListener("click", () => toggleMultiMode());
      // 进入多选模式时按钮本身要给出状态（aria-pressed + 文案），
      // 否则用户不知道现在是「多选中」还是「浏览中」。
      multiToggle.setAttribute("aria-pressed", state.multiMode ? "true" : "false");
      multiToggle.textContent = state.multiMode ? "多选中 ✓" : "多选";
      multiToggle.classList.toggle("primary", state.multiMode);
    }
    const selClear = $("#alSelClear");
    if (selClear) selClear.addEventListener("click", () => clearSelection());
    const selAll = $("#alSelAll");
    if (selAll) selAll.addEventListener("click", () => selectAllVisible());
    const selDelete = $("#alSelDelete");
    if (selDelete) selDelete.addEventListener("click", () => { deleteSelected(); });
    // 【A2】多选条上的「添加到播放器」：把勾选的全部加进播放清单。
    const selPlayer = $("#alSelPlayer");
    if (selPlayer) selPlayer.addEventListener("click", () => { addSelectedToPlayer(); });
    // 【需求 14】播放器模式入口。
    //
    // 【为什么要先关抽屉】素材详情抽屉（#alDrawer）是**右侧浮层**，
    // 展开时盖住页面右侧 —— 而「播放器模式」按钮正好在右上角那排。
    // 实测：抽屉开着时点「播放器模式」按钮，click 被抽屉里的 <video>
    // 拦截（Playwright 报 "subtree intercepts pointer events"），
    // 用户也会遇到「点了没反应」。
    // 打开播放器前先关抽屉，两个浮层不叠在一起。
    const playerBtn = $("#alPlayerBtn");
    if (playerBtn && window.AssetPlayer) {
      playerBtn.addEventListener("click", () => {
        closeDrawer();
        window.AssetPlayer.open();
      });
    }

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

    // 【独立查看窗口 · 2026-11-21】捕获阶段先记快照。
    //
    // 必须在**捕获阶段**记 —— 冒泡阶段里 app.js 的 lightbox 处理器
    // 已经把 lightbox 藏起来了，那时再查就晚了（详见
    // viewerOpenAtEscape 的注释）。
    document.addEventListener("keydown", event => {
      if (event.key !== "Escape") return;
      viewerOpenAtEscape = isIndependentViewerOpen();
    }, true);

    document.addEventListener("keydown", event => {
      if (event.key !== "Escape") return;
      // 【独立查看窗口 · 2026-11-21】查看器开着时，Esc 只关**查看器**，
      // 不能顺手把底下的抽屉也关了。
      //
      // 【为什么这里必须挡】三个 Escape 监听器都挂在 document 上：
      //   * app.js          —— 监听 #lightbox（图片分支）
      //   * media-preview.js—— 监听 #mediaPreviewModal（视频/音频分支）
      //   * assets.js（本行）—— 关抽屉 / 选择器 / 右键菜单
      // 它们的注册顺序：app.js 先（脚本靠前）、assets.js 次、
      // media-preview.js 最后（ensureModal 是**懒加载**的，第一次打开时
      // 才注册监听器）。所以 Esc 一定会**同时**打到本行。
      // 不挡的话：用户在查看器里按 Esc -> 查看器关了，抽屉也跟着关了
      // -> 用户「只是想关掉大图继续看详情」，却连详情页都被踢掉了。
      //
      // 【判据】用捕获阶段记下的快照（viewerOpenAtEscape），
      // 不是此刻再查 —— 此刻查已经晚了一步（app.js 已把 lightbox 关了）。
      if (viewerOpenAtEscape) return;
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
    startLiveRefresh();
  }

  // ---------------------------------------------------------------- 素材库实时刷新
  //
  // 用户要求：「生成完成后素材库仍要能实时出现新内容」——
  // 所以素材库必须有轮询（原来只有用户手动操作才刷新）。
  //
  // 但轮询不能打断交互：
  //   1. 用户正在交互（原生下拉展开 / 焦点在输入框 / 弹出层展开）-> 本轮推迟；
  //   2. 推迟的那一轮在交互结束后补跑（守卫模块负责触发）；
  //   3. 渲染走增量路径（稳定 key），只补新增/变化的卡片。
  //
  // 页面不在素材库页时完全不轮询 —— 不做无谓请求。
  const LIVE_REFRESH_MS = 4000;

  function onAssetsPage() {
    const page = $("#assetsPage");
    if (!page) return false;
    if (page.classList.contains("page-hidden")) return false;
    return page.offsetParent !== null;
  }

  async function liveRefreshTick() {
    if (!onAssetsPage()) return;
    const guard = window.H3UI;
    if (guard && guard.isUserInteracting($("#assetsPage"))) {
      // 用户正在操作素材库 —— 推迟本轮，交互结束后自动补跑
      guard.deferRefresh("assets", liveRefreshTick);
      return;
    }
    const page = $("#assetsPage");
    const scroll = guard ? guard.preserveScroll(page) : null;
    // 最后一道闸：写 DOM 的那一刻如果有原生下拉正开着，本轮跳过。
    // （上面的 isUserInteracting 判断与 fetch 之间有时间差。）
    if (guard && !guard.canWriteDOM("assets", () => liveRefreshTick())) return;
    await loadAssets();
    await loadStats();
    if (guard) guard.restoreScroll(page, scroll);
  }

  function startLiveRefresh() {
    // 【2026-11-22 请求风暴修】用带抖动/退避/页面不可见暂停的调度器。
    // 老写法是裸 setInterval(liveRefreshTick, 4000)。
    if (window.H3Poll) {
      if (window.__assetLiveTimer) window.__assetLiveTimer.stop();
      window.__assetLiveTimer = window.H3Poll("assets-live", liveRefreshTick,
                                              LIVE_REFRESH_MS);
      return;
    }
    if (window.__assetLiveTimer) return;
    window.__assetLiveTimer = setInterval(liveRefreshTick, LIVE_REFRESH_MS);
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
    // 【2026-11-14 需求 14】素材库播放器模式需要「按 id 打开某张素材详情」
    // （玩家面板里点某一项要能跳到详情）。openDrawer 原本是模块私有的，
    // 这里导出为公开 API。
    openDrawer,
    // 【2026-11-14 需求 13】多选批量删除需要「当前已加载的素材清单」。
    getItems: () => (state.items || []).slice(),
    // 【A2】播放器清单：对外暴露增删，供其它模块（如媒体库右键）复用。
    addToPlayer,
    removeFromPlayer,
    // 【2026-11-21 独立查看窗口】对外暴露「在独立窗口看某个素材」。
    // 素材库自己的三条入口（卡片按钮 / 抽屉按钮 / 右键菜单）都走它，
    // 导出是为了让别的模块（如 agent 面板）也能用同一套查看体验，
    // 而不必各自写一份（两份实现必然漂移）。
    openAssetViewer,
    isViewableAsset,
  };
})();
