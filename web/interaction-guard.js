/* ============================================================
   交互守卫 —— 定时刷新不得打断用户操作
   ------------------------------------------------------------
   为什么单独一个文件：app.js / assets.js / providers.js 三个脚本
   各自有刷新路径，守卫逻辑必须只有一份实现，否则三处各写一遍
   必然走偏（这正是本项目反复出现过的「同一常量写三处」问题）。

   要解决的问题（全部由 tests/probe_*.py 在真实浏览器里复现过）：
     1. 刷新整块重绘 DOM -> 原生 <select> 下拉被换掉、展开状态丢失、
        焦点回到 body；
     2. 刷新把节点在容器里「搬家」（appendChild 已有节点 = 移动）
        -> 焦点丢失、视频播放被打断；
     3. 刷新覆盖用户正在输入的内容。

   对外接口（window.H3UI）：
     isUserInteracting(root?)  用户此刻是否正在交互
     deferRefresh(key, fn)     交互期间把刷新推迟，交互结束后自动补跑
     captureFocus()            焦点快照（含用户输入内容）
     restoreFocus(snapshot)    刷新后把焦点/输入内容还原
     preserveScroll(el)        记下滚动位置
     restoreScroll(el, token)  还原滚动位置
     withPreserved(root, fn)   跑一次会改 DOM 的刷新，同时保住焦点与滚动
     setHTMLIfChanged(el, html)内容没变就不动 DOM（消除无谓重建）
   ============================================================ */
(function () {
  "use strict";

  // ---------- 自绘弹出层（展开中 = 用户正在用它） ----------
  const POPUP_SELECTORS = [
    ".al-tagpick.open",      // 素材库「按标签筛选」
    ".al-context.open",      // 素材库卡片右键菜单
    ".ctx-menu.open",        // 媒体库卡片右键菜单
    ".al-picker-mask.open",  // 素材选择器
    ".al-drawer.open",       // 素材详情抽屉
    ".panel.open",           // 窄屏参数抽屉
    ".sidebar.open",         // 窄屏侧栏抽屉
  ];

  // 这些 input 类型不是「正在输入」，用户只是在点/拖
  const NON_TYPING_INPUT_TYPES = new Set([
    "range", "checkbox", "radio", "button", "submit", "reset", "file", "hidden", "color",
  ]);

  // 可作锚点的唯一属性（页面内唯一，用它定位比 nth-child 稳）
  const UNIQUE_ATTRS = ["id", "data-jid", "data-pl-select", "data-id"];

  // ---------------------------------------------------------------- 交互判定

  function isTextEntry(el) {
    if (!el || el.nodeType !== 1) return false;
    if (el.isContentEditable) return true;
    const tag = el.tagName;
    if (tag === "TEXTAREA") return true;
    if (tag === "INPUT") return !NON_TYPING_INPUT_TYPES.has((el.type || "text").toLowerCase());
    return false;
  }

  // 原生 <select> 的下拉是否处于展开状态。
  // Chromium 支持 select:open；Firefox 不支持时 matches() 抛错 -> 按「未展开」处理。
  function selectIsOpen(el) {
    if (!el || el.tagName !== "SELECT") return false;
    try { return el.matches(":open"); } catch (e) { return false; }
  }

  function openSelect(root) {
    const scope = root || document;
    let list;
    try { list = scope.querySelectorAll("select"); } catch (e) { return null; }
    for (const s of list) if (selectIsOpen(s)) return s;
    return null;
  }

  function openPopup(root) {
    const scope = root || document;
    for (const sel of POPUP_SELECTORS) {
      const el = scope.querySelector(sel);
      if (el) return el;
    }
    return null;
  }

  /**
   * 用户此刻是否正在交互。
   * 四种信号任一成立即算「正在交互」：
   *   1. 原生下拉**展开**（<select> 的弹层，:open）—— 刷新会让它关闭、位置丢失
   *   2. 焦点在文本输入控件（正在打字）—— 刷新会覆盖已输入的内容
   *   3. 焦点在 <select> 上但未展开 —— 刷新会把它换掉（选中值由守卫还原）
   *   4. 自绘弹出层 / 抽屉处于展开态
   *
   * 注意第 1 条只认「展开」不认「有焦点」：
   * 只展开的 select 必须推迟刷新（否则弹层当场关闭）；
   * 仅聚焦的 select 不推迟 —— 否则用户一直点着筛选框，实时刷新就永远停摆。
   */
  function isUserInteracting(root) {
    if (openSelect(root)) return true;
    const active = document.activeElement;
    if (isTextEntry(active)) return true;
    if (openPopup(root)) return true;
    return false;
  }

  // ---------------------------------------------------------------- 焦点快照

  function anchorOf(el) {
    let node = el;
    while (node && node.nodeType === 1) {
      for (const attr of UNIQUE_ATTRS) {
        const value = node.getAttribute ? node.getAttribute(attr) : null;
        if (value) return {node: node, attr: attr, value: value};
      }
      node = node.parentElement;
    }
    return null;
  }

  function anchorSelector(anchor) {
    if (anchor.attr === "id") return "#" + CSS.escape(anchor.value);
    return "[" + anchor.attr + '="' + CSS.escape(anchor.value) + '"]';
  }

  // 从锚点到目标节点的相对路径（用 nth-of-type 区分同标签兄弟）
  function relativePath(from, to) {
    const parts = [];
    let node = to;
    while (node && node !== from && node.nodeType === 1) {
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const sameTag = Array.prototype.filter.call(
          parent.children, function (c) { return c.tagName === node.tagName; });
        if (sameTag.length > 1) {
          part += ":nth-of-type(" + (sameTag.indexOf(node) + 1) + ")";
        }
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(" > ");
  }

  function cssPath(el) {
    if (!el || el.nodeType !== 1) return null;
    if (el === document.body || el === document.documentElement) return null;
    const anchor = anchorOf(el);
    if (!anchor) return null;
    const rel = relativePath(anchor.node, el);
    return rel ? anchorSelector(anchor) + " > " + rel : anchorSelector(anchor);
  }

  /**
   * 抓一份焦点快照：路径 + 用户已输入的内容 + 光标位置。
   * 刷新把 DOM 换掉之后，用它把「同一个控件」找回来并还原。
   */
  function captureFocus() {
    const el = document.activeElement;
    if (!el || el === document.body || el === document.documentElement) return null;
    const path = cssPath(el);
    if (!path) return null;
    const snap = {path: path, tag: el.tagName};
    if (el.tagName === "SELECT") {
      snap.value = el.value;
    } else if (isTextEntry(el)) {
      snap.value = el.value;
      if (typeof el.selectionStart === "number") {
        snap.selectionStart = el.selectionStart;
        snap.selectionEnd = el.selectionEnd;
      }
    }
    return snap;
  }

  function restoreFocus(snap) {
    if (!snap) return false;
    let el = null;
    try { el = document.querySelector(snap.path); } catch (e) { el = null; }
    if (!el) return false;
    // 用户已输入的内容优先：刷新不许覆盖
    if (snap.value != null && "value" in el && el.value !== snap.value) {
      try { el.value = snap.value; } catch (e) { /* ignore */ }
    }
    try { el.focus({preventScroll: true}); } catch (e) { /* ignore */ }
    if (snap.selectionStart != null && typeof el.setSelectionRange === "function") {
      try { el.setSelectionRange(snap.selectionStart, snap.selectionEnd); } catch (e) { /* ignore */ }
    }
    return true;
  }

  // ---------------------------------------------------------------- 滚动位置

  function preserveScroll(el) {
    if (!el) return null;
    return {top: el.scrollTop, left: el.scrollLeft};
  }

  function restoreScroll(el, token) {
    if (!el || !token) return;
    if (el.scrollTop !== token.top) el.scrollTop = token.top;
    if (el.scrollLeft !== token.left) el.scrollLeft = token.left;
  }

  // ---------------------------------------------------------------- 组合工具

  /**
   * 跑一次会改 DOM 的刷新，同时保住焦点与滚动位置。
   * root：滚动容器（不传则不保滚动）。
   */
  function withPreserved(root, fn) {
    const focus = captureFocus();
    const scroll = preserveScroll(root);
    try {
      fn();
    } finally {
      restoreScroll(root, scroll);
      restoreFocus(focus);
    }
  }

  /**
   * 内容没变就不动 DOM。
   * 定时刷新里大量「每 2 秒重建一次完全相同的 HTML」，
   * 节点被换掉 = 焦点丢失 / 下拉关闭 / 视频重播。
   */
  function setHTMLIfChanged(el, html) {
    if (!el) return false;
    const next = String(html);
    if (el.dataset && el.dataset.h3Sig === next) return false;
    el.innerHTML = next;
    if (el.dataset) el.dataset.h3Sig = next;
    return true;
  }

  /**
   * 写 DOM 之前的最后一道闸。
   *
   * 为什么需要它：scheduleRefresh() 在**发起**刷新时判断「用户是否在交互」，
   * 但刷新是异步的 —— 判断通过之后、数据返回之前，用户可能刚好展开了下拉。
   * 这时如果照常写 DOM，下拉会被当场关闭。
   *
   * 用法：
   *   if (window.H3UI && !window.H3UI.canWriteDOM("jobs", refreshJobs)) return;
   *   ... 写 DOM ...
   *
   * 语义：**只做判断与登记，不代为执行**。
   *   返回 true  = 可以写；
   *   返回 false = 本轮跳过，并把 retryFn 登记为「交互结束后补跑」。
   */
  function canWriteDOM(deferKey, retryFn) {
    if (!openSelect()) return true;
    // 有原生下拉正开着 —— 绝对不能动 DOM。
    // 把整个刷新重新登记为待补跑，交互结束后由守卫模块统一执行。
    if (deferKey && typeof retryFn === "function") deferRefresh(deferKey, retryFn);
    return false;
  }

  // ---------------------------------------------------------------- 延迟刷新

  const deferred = new Map();     // key -> fn
  let watchTimer = null;

  function flushDeferred() {
    if (!deferred.size) return;
    if (isUserInteracting()) return;    // 还在交互，继续等
    const jobs = Array.from(deferred.entries());
    deferred.clear();
    for (const entry of jobs) {
      try { entry[1](); } catch (e) { console.error("[交互守卫] 延迟刷新失败:", entry[0], e); }
    }
  }

  function ensureWatcher() {
    if (watchTimer) return;
    watchTimer = setInterval(function () {
      if (!deferred.size) { clearInterval(watchTimer); watchTimer = null; return; }
      flushDeferred();
    }, 250);
  }

  /**
   * 用户正在交互时，把刷新推迟到交互结束（焦点离开 / 下拉关闭）之后。
   * 同一个 key 只会保留最后一次（后写的覆盖先写的），避免堆积。
   */
  function deferRefresh(key, fn) {
    deferred.set(key, fn);
    ensureWatcher();
  }

  window.H3UI = {
    isUserInteracting: isUserInteracting,
    isTextEntry: isTextEntry,
    selectIsOpen: selectIsOpen,
    openSelect: openSelect,
    openPopup: openPopup,
    captureFocus: captureFocus,
    restoreFocus: restoreFocus,
    preserveScroll: preserveScroll,
    restoreScroll: restoreScroll,
    withPreserved: withPreserved,
    setHTMLIfChanged: setHTMLIfChanged,
    canWriteDOM: canWriteDOM,
    deferRefresh: deferRefresh,
    flushDeferred: flushDeferred,
  };
})();
