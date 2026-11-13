/* ============================================================
   左侧栏：类别折叠 + 独立滚动（2026-11-13）
   ------------------------------------------------------------
   【需求原文】
     1) 最左侧每个类别标签可点击展开/收纳其子类别，
        折叠状态持久化到 localStorage，刷新后保持；
     2) 最左侧列必须可上下滚动（独立滚动容器），
        小分辨率浏览器（如 375/390px 高）也能看到最下面几项，
        不能被 overflow:hidden 截断。

   【为什么要单独一个文件（而不是塞进 app.js / video-nav.js）】
     折叠是**视图状态**，与「选中哪个模式」是两件独立的事：
       - 选中态  -> app.js 的 activateMode() + video-nav.js 的 syncActive()
       - 折叠态  -> 本文件（localStorage key: h3studio.sidebarFold）
     混在一起会互相打架：
       · 点分组标题顺手把模式切了（用户只想收纳，不想切模式）；
       · 切模式顺手把用户折好的分组展开了（折叠状态不持久）。
     两套状态各管各的，互不干扰。

   【DOM 约定】
     · 顶层分组标题：<button class="nav-group" data-nav-key="...">
       折叠范围 = 它之后、下一个 .nav-group 之前的
       .nav-item（入口项）与 .nav-categories（视频两级容器）。
     · 视频类别（第二级）：.nav-cat > .nav-cat-head / .nav-cat-models
       折叠开关是 .nav-cat-toggle —— 必须与 .nav-cat-head **平级**，
       不能塞进 head 里面：button 里套 button 是非法 HTML，
       浏览器会把内层按钮从可交互树里摘掉，点击就落在 head 上。

   【对外接口】window.SidebarFold
     state        当前折叠状态（{groups:{}, cats:{}}）
     isCollapsed(kind, key)
     toggle(kind, key) / setCollapsed(kind, key, collapsed)
     refresh()    重新按状态渲染一遍（视频类别重渲染后用）
   ============================================================ */
(function () {
  "use strict";

  const STORAGE_KEY = "h3studio.sidebarFold";

  /** 折叠状态：{groups:{key:true=已折叠}, cats:{key:true=已折叠}} */
  const state = { groups: {}, cats: {} };

  let scrollEl = null;      // .nav-scroll（滚动容器）
  let videoBox = null;      // #navVideoCategories（视频两级容器）
  let observer = null;

  // ------------------------------------------------------------ 持久化

  function load() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object") return;
      if (parsed.groups && typeof parsed.groups === "object") {
        Object.keys(parsed.groups).forEach(k => {
          state.groups[k] = !!parsed.groups[k];
        });
      }
      if (parsed.cats && typeof parsed.cats === "object") {
        Object.keys(parsed.cats).forEach(k => {
          state.cats[k] = !!parsed.cats[k];
        });
      }
    } catch (e) {
      // 存储不可用（隐私模式 / 配额满）：静默降级为「不持久化」
      state.groups = {};
      state.cats = {};
    }
  }

  function save() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch (e) { /* 静默降级：功能不受影响，只是刷新后不保持 */ }
  }

  // ------------------------------------------------------------ 工具

  /** 取某个分组标题下面的直属内容（入口项 + 视频容器）。 */
  function groupChildren(groupBtn) {
    const out = [];
    let node = groupBtn.nextElementSibling;
    while (node) {
      if (node.classList && node.classList.contains("nav-group")) break;
      if (node.classList
          && (node.classList.contains("nav-item")
              || node.classList.contains("nav-categories"))) {
        out.push(node);
      }
      node = node.nextElementSibling;
    }
    return out;
  }

  function isCollapsed(kind, key) {
    const bucket = kind === "cat" ? state.cats : state.groups;
    return !!bucket[key];
  }

  // ------------------------------------------------------------ 渲染

  /** 顶层分组：折叠/展开它的入口项。 */
  function applyGroups() {
    if (!scrollEl) return;
    scrollEl.querySelectorAll(".nav-group[data-nav-key]").forEach(btn => {
      const key = btn.dataset.navKey;
      const collapsed = isCollapsed("group", key);
      btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
      btn.classList.toggle("collapsed", collapsed);
      groupChildren(btn).forEach(el => {
        el.classList.toggle("is-folded", collapsed);
      });
    });
  }

  /** 视频类别：确保每个 .nav-cat 都有一个折叠开关，并按状态折叠模型列表。 */
  function applyCats() {
    if (!videoBox) return;
    videoBox.querySelectorAll(".nav-cat").forEach(catEl => {
      const key = catEl.dataset.category;
      const collapsed = isCollapsed("cat", key);
      const head = catEl.querySelector(".nav-cat-head");
      const models = catEl.querySelector(".nav-cat-models");

      // 开关按钮（与 head 平级）
      let toggle = catEl.querySelector(".nav-cat-toggle");
      if (!toggle) {
        toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "nav-cat-toggle";
        toggle.dataset.category = key;
        // 放在 head 之后、模型列表之前（grid 定位决定最终位置）
        if (head && head.nextElementSibling) {
          catEl.insertBefore(toggle, head.nextElementSibling);
        } else {
          catEl.appendChild(toggle);
        }
      }
      const label = ((catEl.querySelector(".nav-cat-label") || {}).textContent || key).trim();
      const caret = collapsed ? "▸" : "▾";
      // 【幂等】只在值真的变了才写 —— 否则每次写都会产生 childList 变更，
      // 触发 MutationObserver -> 再次 applyCats -> 死循环。
      if (toggle.textContent !== caret) toggle.textContent = caret;
      toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
      toggle.setAttribute("aria-label",
        collapsed ? `展开「${label}」下的模型` : `收起「${label}」下的模型`);
      toggle.title = collapsed ? "展开子项" : "收起子项";
      toggle.disabled = !!(head && head.disabled);

      if (models) models.classList.toggle("is-folded", collapsed);
      // head 的 aria-expanded 表达「该类别的模型列表是否展开」——
      // 这是**折叠**语义，由本模块独家维护。
      // 【为什么不让 video-nav.js 管】那边 syncActive() 会把 head 的
      // aria-expanded 写成「是否选中」，两处各写一份必然打架：
      // 切一次模式就会把用户折好的类别重新展开。
      if (head) head.setAttribute("aria-expanded", collapsed ? "false" : "true");
    });
  }

  function refresh() {
    applyGroups();
    applyCats();
  }

  // ------------------------------------------------------------ 交互

  function setCollapsed(kind, key, collapsed) {
    const bucket = kind === "cat" ? state.cats : state.groups;
    bucket[key] = !!collapsed;
    save();
    refresh();
  }

  function toggle(kind, key) {
    setCollapsed(kind, key, !isCollapsed(kind, key));
  }

  // ------------------------------------------------------------ 展开（用户显式操作时）

  /**
   * 展开某个分组 / 类别。
   *
   * 【为什么只在用户显式操作时调用】
   *   启动恢复路径（VideoNav.applyDefault / activateMode）**不能**调它 ——
   *   否则「刷新后保持折叠状态」这条需求会被打破：
   *   启动时默认模式 t2i 落在「文生图」分组里，一调 ensureExpanded
   *   就把用户折好的分组又展开了。
   *   所以调用点只有两处：用户点了分组标题 / 用户点了具体入口项。
   */
  function ensureExpanded(kind, key) {
    if (!key) return false;
    if (!isCollapsed(kind, key)) return false;
    setCollapsed(kind, key, false);
    return true;
  }

  /**
   * 找到某个入口元素所属的顶层分组 key。
   *
   * 【为什么不能用祖先链找】顶层分组标题（.nav-group）和它的入口项（.nav-item）
   *   是**兄弟**关系（都在 .nav-scroll 里平铺），不是父子 ——
   *   沿 parentElement 往上走永远撞不到 .nav-group。
   *   正确做法：先往上走到 .nav-scroll 的**直接子节点**，
   *   再在 .nav-scroll 的子节点序列里找「位于它之前的最后一个 .nav-group」。
   *
   * 【为什么要找「直接子节点」而不是直接比 el】
   *   视频模型项（.nav-model-item）嵌在 .nav-cat-models > .nav-cat >
   *   #navVideoCategories 里，el 本身不是 .nav-scroll 的子节点 ——
   *   直接比永远不命中，扫描会一路走到最后一个分组，
   *   结果是「点了视频模型项却展开了系统分组」。
   */
  function groupKeyOf(el) {
    if (!el || !scrollEl) return "";
    // 往上走到 .nav-scroll 的直接子节点
    let node = el;
    while (node && node.parentElement && node.parentElement !== scrollEl) {
      node = node.parentElement;
    }
    if (!node || node.parentElement !== scrollEl) return "";
    // 在 .nav-scroll 的子节点序列里找「该节点之前最近的 .nav-group」
    let groupBtn = null;
    for (const child of Array.from(scrollEl.children)) {
      if (child === node) break;
      if (child.classList && child.classList.contains("nav-group")
          && child.dataset.navKey) {
        groupBtn = child;
      }
    }
    return groupBtn ? groupBtn.dataset.navKey : "";
  }

  /**
   * 展开某个入口元素所在的分组（以及它所属的视频类别）。
   * 用于「用户点了某个入口项，但那个入口项所在分组正折着」的场景 ——
   * 不展开的话，选中态落在看不见的地方，用户会以为没选中。
   */
  function ensureExpandedForElement(el) {
    if (!el) return false;
    let changed = false;
    const cat = el.closest ? el.closest(".nav-cat") : null;
    if (cat && cat.dataset.category) {
      changed = ensureExpanded("cat", cat.dataset.category) || changed;
    }
    const groupKey = groupKeyOf(el);
    if (groupKey) {
      changed = ensureExpanded("group", groupKey) || changed;
    }
    return changed;
  }

  function init() {
    scrollEl = document.getElementById("navScroll");
    videoBox = document.getElementById("navVideoCategories");
    if (!scrollEl && !videoBox) return;

    load();

    if (scrollEl) {
      // 分组标题点击 = 展开/收纳（不切模式、不改选中态）
      scrollEl.addEventListener("click", event => {
        const btn = event.target.closest(".nav-group[data-nav-key]");
        if (!btn || !scrollEl.contains(btn)) return;
        event.preventDefault();
        toggle("group", btn.dataset.navKey);
      });
    }

    if (videoBox) {
      // 视频类别的折叠开关：与 head 平级，单独处理
      videoBox.addEventListener("click", event => {
        const btn = event.target.closest(".nav-cat-toggle");
        if (!btn) return;
        event.preventDefault();
        event.stopPropagation();
        toggle("cat", btn.dataset.category);
      });
      // 视频类别是异步渲染的（web/video-nav.js 拉 /api/video-categories），
      // 渲染完会整体替换容器内容 -> 用观察器把折叠状态重新贴上去。
      if (typeof MutationObserver !== "undefined") {
        observer = new MutationObserver(() => { applyCats(); });
        observer.observe(videoBox, { childList: true, subtree: true });
      }
    }

    refresh();
  }

  window.SidebarFold = {
    state,
    init,
    refresh,
    isCollapsed,
    setCollapsed,
    toggle,
    ensureExpanded,
    ensureExpandedForElement,
    groupKeyOf,
    STORAGE_KEY,
  };

  // 脚本在 </body> 前加载，DOM 已就绪；DOMContentLoaded 时再兜一次初始化。
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
