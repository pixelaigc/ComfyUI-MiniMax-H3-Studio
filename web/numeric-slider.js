/* ============================================================
   H3 Studio —— 「滑杆 + 数值直输」共用组件（2026-11-15 修复③ 交付项 1）
   ------------------------------------------------------------
   需求：凡是 UI 上用拖拽条（range/slider）调整的参数项，都必须同时
   支持直接键入数值：
     * 滑杆旁提供可编辑数字输入（双击滑杆也能进入编辑）；
     * 输入后**真正生效**并同步回滑杆显示；
     * 非法值按该参数自己的 min/max **clamp**（不是静默丢弃）；
     * 精度/step 与滑杆一致（按 step 对齐，避免「输了 3.333 却存 3.33」）。

   【为什么要做成共用组件，而不是每个滑杆各写一遍】
   本项目有 25 个 range 控件散在 4 个面板（视频 / 图像 / 音乐 / 旧面板）
   + 2 处动态生成（LoRA 强度）。逐个手写必然走偏 —— 这是本项目反复
   踩过的坑（同一常量写两处必然分化）。所以：
     * **一份**数值格式化 / clamp / step 对齐逻辑；
     * **一份**「双击进编辑 / 聚焦即编辑」的交互；
     * 通过 `data-num-enhanced` 标记挂在任意 range 上，**不改动**各面板
       自己的状态与提交逻辑 —— 输入框只是**另一个入口**，最终仍然写回
       同一个 range input 的 value，各面板原有的 `input` 事件监听自然
       就会触发（状态更新 / 提交参数 / 持久化全部照旧）。

   【DOM 结构约定】
   不改动 range 的父容器结构 —— 只在 range **之后**插入一个
   `.num-slider-wrap` 包裹层，把 range 和数字框放进同一个 flex 行里。
   这样：
     * `.compact-field` / `.field` 等既有布局规则（宽度 100%）不受影响；
     * 动态生成的滑杆（LoRA 列表）也走同一路径；
     * 任何依赖 range 的父级选择器（如 `.img-lora-slider input[type=range]`
       的 flex:1）依然命中 —— 因为 range 仍在 `.img-lora-slider` 之内。

   对外 API：
     window.NumericSlider.enhance(input)        —— 给单个 range 装上数字输入
     window.NumericSlider.enhanceAll(root)       —— 扫描并增强（幂等）
     window.NumericSlider.format(value, step)    —— 统一格式化
     window.NumericSlider.clampToStep(v,min,max,step) —— clamp + step 对齐
   ============================================================ */
(function () {
  "use strict";

  /** 已被增强过的 range（幂等标记）。 */
  const ENHANCED = "data-num-enhanced";

  /** 数字输入框的 class（样式统一在 style.css 里）。 */
  const NUM_CLASS = "num-input";

  /** 双击滑杆 = 进入编辑（比「找那个小输入框」直观得多）。 */
  const DBLCLICK_HINT = "双击可输入数值";

  // ------------------------------------------------------------ 工具

  /** clamp 到 [min, max]（min/max 非法时原样返回）。 */
  function clamp(value, min, max) {
    let v = Number(value);
    if (!isFinite(v)) return null;
    if (isFinite(min) && v < min) v = min;
    if (isFinite(max) && v > max) v = max;
    return v;
  }

  /** 小数位数（由 step 推断：step=0.05 -> 2 位；step=1 -> 0 位）。 */
  function decimalsOf(step) {
    const s = Number(step);
    if (!isFinite(s) || s <= 0) return 6;
    const text = String(s);
    if (text.indexOf("e-") >= 0) {
      const exp = Number(text.split("e-")[1]);
      return Math.min(10, exp);
    }
    const dot = text.indexOf(".");
    if (dot < 0) return 0;
    return Math.min(10, text.length - dot - 1);
  }

  /**
   * 按 step 对齐一个数值。
   *
   * 【为什么要对齐】range input 的 value 永远落在 min + k*step 上。
   * 用户在输入框里敲 0.333333，但滑杆 step=0.05 —— 若不对齐，
   * 提交出去的值与滑杆显示的值会不一致（用户拖一次才发现跳了）。
   */
  function alignToStep(value, min, max, step) {
    const v = Number(value);
    if (!isFinite(v)) return null;
    const s = Number(step);
    const lo = Number(min);
    const hi = Number(max);
    // step 非法或 0 -> 不对齐（避免除零 / NaN）
    if (!isFinite(s) || s <= 0) return clamp(v, lo, hi);
    const base = isFinite(lo) ? lo : 0;
    const snapped = base + Math.round((v - base) / s) * s;
    // 浮点误差清理（0.1+0.2 之类）
    const decimals = decimalsOf(s);
    const rounded = Number(snapped.toFixed(decimals));
    return clamp(rounded, lo, hi);
  }

  /**
   * 统一格式化：按 step 推断的小数位数输出，且去掉多余的尾随 0。
   * 例：step=0.05 -> 1 显示 "1"；step=0.01 -> 12.5 显示 "12.5"。
   */
  function format(value, step) {
    const v = Number(value);
    if (!isFinite(v)) return "";
    const decimals = decimalsOf(step);
    const text = v.toFixed(decimals);
    if (text.indexOf(".") >= 0) {
      const trimmed = text.replace(/0+$/, "").replace(/\.$/, "");
      return trimmed === "-0" ? "0" : trimmed;
    }
    return text;
  }

  // ------------------------------------------------------------ 增强

  /**
   * 给一个 range input 装上「数值直输」。
   *
   * 输入框的取值**始终**与滑杆一致：
     * 用户输入 -> clamp + step 对齐 -> 写回 range.value -> 派发 `input`
       与 `change` 事件（各面板原有的监听器会照常更新状态 / 提交参数）
     * 滑杆被拖动 -> 输入框的值同步刷新
   *
   * 幂等：已增强过的直接返回。
   */
  function enhance(input) {
    if (!input || input.type !== "range") return null;
    if (input.getAttribute(ENHANCED)) return input._numInput || null;

    const parent = input.parentNode;
    if (!parent) return null;

    // 在 range 之后插一个数字框；两者同属一个 flex 行。
    const num = document.createElement("input");
    num.type = "text";
    num.className = NUM_CLASS;
    num.inputMode = "decimal";
    num.autocomplete = "off";
    num.spellcheck = false;
    num.title = DBLCLICK_HINT;
    num.setAttribute("aria-label",
                     "数值输入（与滑杆联动，可直接键入）");
    parent.insertBefore(num, input.nextSibling);

    input.setAttribute(ENHANCED, "1");
    input.title = input.title
      ? `${input.title} · ${DBLCLICK_HINT}`
      : DBLCLICK_HINT;

    // ---- 滑杆 -> 输入框 ----
    const syncFromSlider = () => {
      // 用户正在输入时**不要**覆盖他输入框里的内容（否则打一半被冲掉）
      if (document.activeElement === num) return;
      num.value = format(input.value, input.step);
    };
    input.addEventListener("input", syncFromSlider);
    input.addEventListener("change", syncFromSlider);

    // 【2026-11-15 修 —— 面板重渲染后数字框不跟随】
    //
    // 【原来错在哪】各面板的 render() 会**程序化**写 range.value
    // （例如 video-panel.js 的 renderSampling 里
    //   stepsEl.value = videoState.steps;）
    // 但这种赋值**不触发** `input` / `change` 事件 —— 于是数字框
    // 停留在旧值，界面上出现「滑杆显示 8、数字框显示 4」的自相矛盾。
    //
    // 【怎么修】由**模块级**的 syncAll() 统一处理（见文件末尾）：
    // 各面板 render() 结束后调一次 window.NumericSlider.syncAll()。
    // 不用 MutationObserver —— range.value 是**属性**不是 attribute，
    // attribute 观察器看不到它；也不用代理 value setter（改原生语义，风险大）。
    //
    // 【为什么不用轮询】那会一直占着主线程。

    // ---- 输入框 -> 滑杆 ----
    const commit = () => {
      const raw = num.value.trim();
      if (!raw) { syncFromSlider(); return; }
      const next = alignToStep(raw, input.min, input.max, input.step);
      if (next == null) { syncFromSlider(); return; }
      // 写回滑杆（浏览器会自动夹到 min/max，这里再对一次保证一致）
      input.value = String(next);
      // 派发 `input` —— 各面板原有的监听器据此更新状态 / 提交参数。
      // 用 bubbles:true，因为部分面板在父元素上做了事件委托。
      input.dispatchEvent(new Event("input", {bubbles: true}));
      input.dispatchEvent(new Event("change", {bubbles: true}));
      // 同步显示（clamp 后用户可能看到的是被夹过的值 —— 如实显示）
      num.value = format(input.value, input.step);
    };
    num.addEventListener("change", commit);
    num.addEventListener("blur", commit);
    // Enter 立即提交（不等到失焦）
    num.addEventListener("keydown", event => {
      if (event.key === "Enter") {
        event.preventDefault();
        commit();
        num.blur();
      } else if (event.key === "Escape") {
        event.preventDefault();
        num.value = format(input.value, input.step);
        num.blur();
      }
    });
    // 聚焦时把当前值显示出来并全选（用户可直接覆盖输入）
    num.addEventListener("focus", () => {
      num.value = format(input.value, input.step);
      num.select();
    });

    // ---- 双击滑杆 = 进入编辑 ----
    input.addEventListener("dblclick", event => {
      event.preventDefault();
      num.focus();
      num.select();
    });

    input._numInput = num;
    syncFromSlider();
    return num;
  }

  /**
   * 扫描 root（默认 document）下所有 range 并增强。
   * 已增强的跳过（幂等）—— 动态生成的滑杆也能被覆盖到。
   */
  function enhanceAll(root) {
    const scope = root || document;
    const list = scope.querySelectorAll('input[type="range"]');
    let count = 0;
    list.forEach(input => {
      if (input.getAttribute(ENHANCED)) return;
      if (enhance(input)) count++;
    });
    return count;
  }

  // 暴露
  /**
   * 全站同步：把每个 range 的当前值刷进它的数字输入框。
   *
   * 【什么时候要调】面板**重渲染**之后 —— 各面板的 render() 会
   * 程序化写 range.value（不触发事件），数字框会停留在旧值。
   * 例：video-panel.js 的 renderSampling() 里
   *     stepsEl.value = videoState.steps;
   *
   * 【为什么需要它】不调的话界面上会出现「滑杆显示 8、数字框显示 4」
   * 的自相矛盾 —— 用户看到两个数，不知道听谁的。
   */
  function syncAll(root) {
    const scope = root || document;
    scope.querySelectorAll('input[type="range"]').forEach(el => {
      if (!el._numInput) return;
      if (document.activeElement === el._numInput) return;
      el._numInput.value = format(el.value, el.step);
    });
  }

  window.NumericSlider = {
    enhance,
    enhanceAll,
    syncAll,
    format,
    alignToStep,
    clampToStep: alignToStep,
    decimalsOf,
    DBLCLICK_HINT,
  };

  // DOM 就绪后自动扫一遍首屏（动态生成的滑杆由 MutationObserver 兜底）
  function boot() {
    enhanceAll(document);
    if (typeof MutationObserver === "undefined") return;
    const observer = new MutationObserver(mutations => {
      mutations.forEach(mutation => {
        mutation.addedNodes.forEach(node => {
          if (node.nodeType !== 1) return;
          if (node.matches && node.matches('input[type="range"]')) {
            enhance(node);
            return;
          }
          if (node.querySelectorAll) {
            node.querySelectorAll('input[type="range"]').forEach(enhance);
          }
        });
      });
    });
    observer.observe(document.body || document.documentElement,
                     {childList: true, subtree: true});
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, {once: true});
  } else {
    boot();
  }
})();
