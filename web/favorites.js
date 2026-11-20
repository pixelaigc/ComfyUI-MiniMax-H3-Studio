/* ==================================================================
 * 生成媒体库「收藏」（2026-11-20 需求 2 / 3 / 5）
 *
 * 【功能】
 *   1. 卡片右键「添加到收藏 / 取消收藏」（入口在 app.js 的右键菜单里，
 *      真正的请求与状态更新在这里）；
 *   2. 左侧导航「收藏」入口 -> 收藏视图（state.tab = "favorites"）；
 *   3. 收藏视图的批量管理：全选本页 / 清空选择 / 批量取消收藏。
 *
 * 【为什么要单独一个文件】
 * app.js 已经 390KB。这块逻辑独立、内聚、可单测，
 * 而且它同时被「卡片右键」「左侧导航」「收藏工具条」三处调用 ——
 * 独立成模块才能保证三处走的是**同一份**实现，不会各自漂移。
 *
 * 【语义纪律 —— 取消收藏 ≠ 删除产物】
 *   取消收藏 = 只解除收藏标记。任务记录、产物文件、文件索引一律不动。
 *   删除产物 = 既有删除语义（DELETE /api/jobs/{id}），会删记录与文件。
 *   两者是**不同的接口**（/favorite vs DELETE /api/jobs/{id}），
 *   按钮文案、颜色、二次确认提示全部不同 —— 用户不可能混淆。
 *
 * 【为什么收藏是「任务级」】
 * 生成媒体库的一张卡片 = 一个任务（job）。批次卡片与批次缩略图
 * 都指向同一个 job.id。所以收藏的对象是「这张卡片」，
 * 无论用户右键的是批次缩略图还是卡片预览图，收藏的都是同一个任务。
 * ================================================================== */
(function () {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  // ------------------------------------------------------------------ 提示条

  /**
   * 轻量提示条（收藏操作的成败反馈）。
   *
   * 【为什么不复用 assets.js 的 alToast】那是素材库页的私有实现，
   * 收藏视图在**生成媒体库**里 —— 复用它会让两个模块互相依赖。
   * 这里给一份等价的小实现（同样的类名约定，样式写在 style.css 里）。
   */
  function toast(message, kind) {
    let box = $("#favToast");
    if (!box) {
      box = document.createElement("div");
      box.id = "favToast";
      document.body.appendChild(box);
    }
    box.textContent = message;
    box.className = "fav-toast show" + (kind ? " " + kind : "");
    clearTimeout(box._timer);
    box._timer = setTimeout(() => { box.className = "fav-toast" + (kind ? " " + kind : ""); }, 3200);
  }

  // ------------------------------------------------------------------ 接口

  async function api(path, options) {
    const resp = await fetch(path, options);
    let body = {};
    try { body = await resp.json(); } catch (e) { /* 非 JSON */ }
    if (!resp.ok) {
      throw new Error(body.error || ("请求失败（HTTP " + resp.status + "）"));
    }
    return body;
  }

  /**
   * 收藏 / 取消收藏**单个**任务。
   *
   * @param {string} jobId 任务 id
   * @param {boolean} favorited 目标状态
   * @returns {Promise<boolean>} 是否成功
   */
  async function toggleFavorite(jobId, favorited) {
    const id = String(jobId || "").trim();
    if (!id) {
      toast("收藏失败：任务 id 为空", "err");
      return false;
    }
    // 【先本地生效，再等服务端】
    //
    // 【为什么不能等服务端回来才改界面】实测线上一次 POST /favorite 要
    // 0.7~17 秒（浏览器对同一 host 只有 6 条并发连接，页面后台的
    // /api/image-loras 要扫本机 LoRA 目录，会把连接占满）。
    // 等服务端回来再刷新，用户会盯着卡片等好几秒才看到 ★ 出现 ——
    // 看起来像「点了没反应」。
    //
    // 所以这里**先**把卡片上的 ★ 改掉（乐观更新），
    // 服务端回来后**再**刷新列表对齐真值。
    // 【失败会回滚】请求失败时把 ★ 改回去，并给出明确提示 ——
    // 不能让界面显示「已收藏」而服务端其实没有。
    applyBadge(id, favorited);
    try {
      const r = await api("/api/jobs/" + encodeURIComponent(id) + "/favorite", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ favorited: !!favorited }),
      });
      // 成功后刷新当前视图 —— 卡片的 ★ 标识、右键菜单文案、
      // 收藏视图的列表都要跟着变。服务端是唯一真源，前端不猜。
      refreshList();
      toast(favorited ? "已加入收藏" : "已取消收藏（产物文件保留）",
            favorited ? "ok" : "");
      return r.favorited === favorited;
    } catch (e) {
      // 【失败态要有明确提示】不能静默失败 ——
      // 用户点了没反应就会以为功能坏了。
      applyBadge(id, !favorited);   // 回滚乐观更新
      toast("收藏操作失败：" + (e.message || e), "err");
      return false;
    }
  }

  /**
   * 就地更新某张卡片上的 ★ 标识（乐观更新）。
   *
   * 【为什么不就地更新右键菜单的文案】菜单在点完按钮后就关掉了，
   * 下次右键会重新按最新的 job 对象渲染 —— 那时文案自然是对的。
   */
  function applyBadge(jobId, favorited) {
    const card = document.querySelector(
      '.job-card[data-jid="' + String(jobId).replace(/"/g, '\\"') + '"]');
    if (!card) return;
    const top = card.querySelector(".job-top-right");
    if (!top) return;
    let badge = top.querySelector(".job-fav-badge");
    if (favorited && !badge) {
      badge = document.createElement("span");
      badge.className = "job-fav-badge";
      badge.setAttribute("title", "已收藏（清理时会被跳过）");
      badge.setAttribute("aria-label", "已收藏");
      badge.textContent = "★";
      top.insertBefore(badge, top.firstChild);
    } else if (!favorited && badge) {
      badge.remove();
    }
  }

  /**
   * 批量取消收藏。
   *
   * 【与「删除产物」严格区分】这里只调 /api/jobs/favorite/batch，
   * 提示文案也明说「不会删除任何生成文件」。
   */
  async function batchUnfavorite(jobIds) {
    const ids = (jobIds || []).filter(Boolean);
    if (!ids.length) {
      toast("请先勾选要取消收藏的卡片", "warn");
      return false;
    }
    if (!window.confirm(
      "确认取消收藏这 " + ids.length + " 个条目？\n\n" +
      "这**只解除收藏标记**，生成文件与任务记录都会保留。\n" +
      "（要删除产物请用卡片上的「删除」按钮）")) {
      return false;
    }
    try {
      const r = await api("/api/jobs/favorite/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_ids: ids, favorited: false }),
      });
      const failed = Object.keys(r.results || {})
        .filter(k => r.results[k] === "not_found");
      // 选中的条目已经从收藏视图里消失 -> 清空选择态。
      state.favSelected.clear();
      refreshList();
      if (failed.length) {
        toast("已取消收藏 " + (ids.length - failed.length) + " 项；"
              + failed.length + " 项不存在（可能已被删除）", "warn");
      } else {
        toast("已取消收藏 " + ids.length + " 项（生成文件保留）", "ok");
      }
      return true;
    } catch (e) {
      toast("批量取消收藏失败：" + (e.message || e), "err");
      return false;
    }
  }

  // ------------------------------------------------------------------ 视图

  function state() {
    return window.__h3State || null;
  }

  function refreshList() {
    if (window.refreshJobs) window.refreshJobs();
  }

  /**
   * 渲染收藏视图的批量管理条。
   *
   * 【只在收藏标签下出现】其它视图不该显示这组按钮 ——
   * 「批量取消收藏」在非收藏视图里没有意义（那些卡片本来就没收藏）。
   */
  function renderTools() {
    const bar = $("#favTools");
    if (!bar) return;
    const s = state();
    const inFav = !!s && s.tab === "favorites";
    bar.hidden = !inFav;
    if (!inFav) return;
    const n = s.favSelected.size;
    const count = $("#favToolsCount");
    if (count) {
      count.textContent = n ? ("已选 " + n + " 项") : "未选择";
    }
    const batchBtn = $("#favBatchUnfavBtn");
    if (batchBtn) {
      batchBtn.disabled = n === 0;
      batchBtn.classList.toggle("primary", n > 0);
    }
  }

  /**
   * 当前页（收藏视图）里所有**真实**卡片的 id。
   *
   * 【为什么要排除骨架卡】骨架卡的 id 是 `__skeleton__N`（假的）——
   * 把它们当成真实 id 提交给批量接口，服务端会回 not_found，
   * 用户看到的是「取消收藏失败」这种莫名其妙的提示。
   * 骨架卡上的选择框已经不再渲染（见 app.js 的 jobCard），
   * 这里再挡一次 —— 便宜的保险，防止将来有人又给骨架加了控件。
   */
  function currentPageIds() {
    return $$("#jobsGrid .job-card")
      .filter(el => !el.classList.contains("job-skeleton"))
      .map(el => el.dataset.jid)
      .filter(id => id && id.indexOf("__skeleton__") !== 0);
  }

  /** 勾选 / 取消勾选一张卡片。 */
  function setSelected(jobId, checked) {
    const s = state();
    if (!s || !jobId) return;
    if (checked) s.favSelected.add(jobId);
    else s.favSelected.delete(jobId);
    renderTools();
  }

  /** 全选 / 取消全选本页。 */
  function toggleSelectAll() {
    const s = state();
    if (!s) return;
    const ids = currentPageIds();
    const allSelected = ids.length > 0 && ids.every(id => s.favSelected.has(id));
    ids.forEach(id => {
      if (allSelected) s.favSelected.delete(id);
      else s.favSelected.add(id);
    });
    // 复选框的 checked 状态要立刻同步（不刷新整个网格 ——
    // 刷新会打断正在播放的视频）。
    $$("#jobsGrid .fav-sel").forEach(box => {
      box.checked = !allSelected && s.favSelected.has(box.dataset.jid);
    });
    renderTools();
  }

  function clearSelection() {
    const s = state();
    if (!s) return;
    s.favSelected.clear();
    $$("#jobsGrid .fav-sel").forEach(box => { box.checked = false; });
    renderTools();
  }

  // ------------------------------------------------------------------ 绑定

  function bind() {
    // ---- 收藏视图里的复选框（事件委托：卡片是增量渲染的）----
    const grid = $("#jobsGrid");
    if (grid && !grid.dataset.favBound) {
      grid.dataset.favBound = "1";
      grid.addEventListener("change", event => {
        const box = event.target.closest(".fav-sel");
        if (!box) return;
        setSelected(box.dataset.jid, box.checked);
      });
    }

    // ---- 收藏工具条 ----
    const allBtn = $("#favSelectAllBtn");
    if (allBtn && !allBtn.dataset.bound) {
      allBtn.dataset.bound = "1";
      allBtn.addEventListener("click", toggleSelectAll);
    }
    const clearBtn = $("#favClearSelBtn");
    if (clearBtn && !clearBtn.dataset.bound) {
      clearBtn.dataset.bound = "1";
      clearBtn.addEventListener("click", clearSelection);
    }
    const batchBtn = $("#favBatchUnfavBtn");
    if (batchBtn && !batchBtn.dataset.bound) {
      batchBtn.dataset.bound = "1";
      batchBtn.addEventListener("click", () => {
        const s = state();
        batchUnfavorite(s ? Array.from(s.favSelected) : []);
      });
    }

    // ---- 左侧「收藏」导航入口 ----
    const nav = $("#navFavorites");
    if (nav && !nav.dataset.bound) {
      nav.dataset.bound = "1";
      nav.addEventListener("click", () => {
        // 【入口在哪】app.js 导出的 window.H3Studio.showFavorites ——
        // 它负责切标签 + 切页 + 导航高亮，是**唯一**的实现处。
        // 这里不自己写一份「切到收藏」的逻辑（两份必然漂移）。
        if (window.H3Studio && typeof window.H3Studio.showFavorites === "function") {
          window.H3Studio.showFavorites();
          return;
        }
        // 兜底：直接切标签（正常路径走 H3Studio.showFavorites）。
        const s = state();
        if (!s) return;
        s.tab = "favorites";
        s.donePage = 1;
        s.favSelected.clear();
        if (window.showCreate) window.showCreate();
        if (window.renderTabs) window.renderTabs();
        refreshList();
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }

  window.FAVORITES = {
    toggleFavorite,
    batchUnfavorite,
    renderTools,
    toggleSelectAll,
    clearSelection,
    toast,
  };
})();
