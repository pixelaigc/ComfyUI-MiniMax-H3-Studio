/* H3 Studio —— 「已添加的参考素材」右键菜单（移除入口）
 *
 * 【为什么单独一个文件】
 * 参考素材分布在三个互不相干的模块里：
 *   * web/qwen-refs.js —— Qwen-Image 2.1 / H3-Image 的「图片编辑」参考图
 *     （缩略图 + 序号徽标 + 描述编辑区）；
 *   * web/app.js       —— 首尾帧 / 参考生视频的素材槽位
 *     （.up-slot：首帧 / 尾帧 / 参考图 / 参考视频 / 参考音乐）；
 *   * web/music.js     —— 参考生音乐的参考音频。
 * 三个模块各自管自己的状态与渲染，但「右键 -> 移除」是**同一个交互**。
 * 各写一份必然漂移（文案不一致、菜单定位不一致、Esc 行为不一致）。
 * 所以这里抽成**一份实现**，三个模块只提供「移除回调」。
 *
 * 【为什么要右键，而不只靠已有的 × 按钮】
 *   * 视频面板的素材槽位（.up-slot）**根本没有**移除入口 ——
 *     槽位填满后想换素材只能手动去素材库删，或者切模式清空整块。
 *   * Qwen 参考图虽有 × 按钮，但缩略图只有 ~100px，右键菜单能同时给出
 *     「这个条目是什么」的上下文（名称 / 序号 / 占位标记）。
 *   * 用户需求明确要求「右键菜单里选移除」，不是隐藏入口。
 *
 * 【不变式 —— 移除后必须同步的东西】
 *   1. 参考素材列表本身（少一条）；
 *   2. 组内排序编号（后续条目要前移，不能留空洞）；
 *   3. 提示词里的占位引用（被移除那条的标记要删掉，不能留悬空）。
 * 这三件事由**调用方**的回调负责（不同模块的编号语义不同），
 * 本模块只负责「菜单出现在哪、点了之后调谁」。
 */
(function () {
  "use strict";

  const $ = s => document.querySelector(s);

  // 菜单挂载点（index.html 里的 <div class="ctx-menu" id="refContextMenu">）
  const MENU_ID = "refContextMenu";

  let closeHandler = null;

  function menuEl() {
    return $("#" + MENU_ID);
  }

  /**
   * 贴边保护：菜单宽高随内容变化，必须先量再定位，
   * 否则贴近窗口右/下边缘时会溢出屏幕（菜单被裁掉，用户看不到「移除」）。
   */
  function positionMenu(menu, x, y) {
    menu.style.left = "0px";
    menu.style.top = "0px";
    const rect = menu.getBoundingClientRect();
    const left = Math.max(4, Math.min(x, window.innerWidth - rect.width - 8));
    const top = Math.max(4, Math.min(y, window.innerHeight - rect.height - 8));
    menu.style.left = left + "px";
    menu.style.top = top + "px";
  }

  function closeMenu() {
    const menu = menuEl();
    if (menu) menu.classList.remove("open");
  }

  /**
   * 打开右键菜单。
   *
   * @param {MouseEvent} event 右键事件
   * @param {{label?: string, onRemove: Function, enabled?: Function}} opts
   *   label    —— 菜单里那一项的文案（默认「移除」）
   *   onRemove —— 点击该项后的回调（**同步**执行；异常如实抛，不吞）
   *   enabled  —— 可选：返回 false 时**不打开菜单**（对象不存在可移除项时）
   */
  function open(event, opts) {
    const menu = menuEl();
    if (!menu) return;
    if (!opts || typeof opts.onRemove !== "function") {
      console.error("[参考素材右键菜单] 缺少 onRemove 回调，无法打开菜单");
      return;
    }
    // 「这一项现在**不可**移除」-> 直接不弹菜单。
    // 空槽位右键弹一个「移除」出来是假动作（点了什么也不会发生）。
    if (typeof opts.enabled === "function" && !opts.enabled()) return;
    // 右键的默认行为（选中文字 / 弹出浏览器菜单）必须挡住，
    // 否则菜单会被浏览器原生菜单顶掉。
    event.preventDefault();
    event.stopPropagation();

    const label = opts.label || "移除";
    menu.innerHTML = "";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "danger";
    btn.dataset.act = "remove-ref";
    btn.textContent = label;
    btn.addEventListener("click", () => {
      closeMenu();
      opts.onRemove();
    });
    menu.appendChild(btn);
    menu.classList.add("open");
    positionMenu(menu, event.clientX, event.clientY);
  }

  /**
   * 给一个元素装上「右键 -> 移除」。
   *
   * @param {Element} el       目标元素（参考素材条目）
   * @param {Function} onRemove 移除回调
   * @param {string} label      菜单项文案
   * @param {Function} [enabled] 可选：返回 false 时不弹菜单
   */
  function attach(el, onRemove, label, enabled) {
    if (!el) return;
    el.addEventListener("contextmenu", event => open(event, {onRemove, label, enabled}));
  }

  function bindGlobalClose() {
    // 点菜单之外 -> 关掉
    document.addEventListener("click", event => {
      const menu = menuEl();
      if (menu && !menu.contains(event.target)) closeMenu();
    });
    // Esc -> 关掉（与素材库 / 媒体库右键菜单同一套行为）
    document.addEventListener("keydown", event => {
      if (event.key === "Escape") closeMenu();
    });
    // 右键点到别处 -> 旧的菜单必须消失（否则两个菜单同时开着）
    document.addEventListener("contextmenu", event => {
      const menu = menuEl();
      if (menu && !menu.contains(event.target)) closeMenu();
    });
  }

  function boot() {
    bindGlobalClose();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  window.RefContextMenu = {
    open, attach, close: closeMenu,
  };
})();
