/* ============================================================
   增强体系共用组件（EnhanceMenuShared）
   ------------------------------------------------------------
   【为什么单独抽一个文件】
   审查发现：enh-unify 分支与 yue2-flow 分支**各自重写**了
   web/enhance-menu.js 的同一块逻辑，且方向相反：
     - enh-unify：重构成「带 targets() 多面板路由的单体」
     - yue2-flow：重构成「可复用组件 EnhanceMenuShared」
   结果是同一个文件 29 个真实冲突块，**无法自动合并**。

   正确做法（也是本文件的职责）：
     把**共用的渲染 / 定位 / 开合**逻辑抽到这个独立文件，
     两条路线都用它 —— 冲突面自然消失。

   本文件只放**纯展示层**的东西（不依赖 state / provState / 业务）：
     renderEnhanceMenu   渲染一份菜单到容器
     positionMenu        菜单定位（贴按钮，翻转避免出屏）
     bindEnhanceMenu     开合交互（箭头点开 / 点别处关 / Esc 关）
     enhanceMenuItemHtml 单个菜单项的 HTML
     renderFieldsHtml    某项自带的可编辑字段区
     esc                 HTML 转义
     resolveWriteTarget  **按能力契约**解析增强结果的写回目标

   【增强体系的两条路线（合并后的形态）】
     图像 / 视频面板 -> enh-unify 路线：普通增强 / 二次增强 /
                        结构化增强(layout) / 官方 PE，产物是**一段文本**，
                        写回「提示词框」。
     音乐面板        -> yue2-flow 路线：YuE2 结构化风格 + 歌词，
                        产物是**结构化对象**（12 个风格字段 + 歌词），
                        写回「结构化风格字段组 + 歌词框」。
   两者的写回目标由**能力契约**决定（见 resolveWriteTarget），
   任何一方都不再硬编码 DOM id。
   ============================================================ */
(function () {
  "use strict";

  /** HTML 转义。 */
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g,
      c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /**
   * 某增强方式自带的可编辑字段区（system prompt 文本框 + 档位参数）。
   *
   * cfg.renderField(field, method) -> HTML 字符串
   *   不传则用默认渲染（textarea / number）。
   */
  function renderFieldsHtml(method, cfg) {
    const fields = (method && method.fields) || [];
    if (!fields.length) return "";
    const c = cfg || {};
    const blocks = fields.map(f => {
      const shown = c.valueOf ? c.valueOf(method, f) : (f.value || "");
      const input = c.renderField
        ? c.renderField(f, shown)
        : (f.kind === "textarea"
          ? `<textarea class="enhance-menu-field" rows="4" data-method="${esc(method.id)}" data-field="${esc(f.key)}" title="${esc(f.hint)}">${esc(shown)}</textarea>`
          : `<input type="number" step="${f.key === "presencePenalty" ? "0.1" : "1"}" class="enhance-menu-field" data-method="${esc(method.id)}" data-field="${esc(f.key)}" value="${esc(shown)}" title="${esc(f.hint)}">`);
      return `<div class="enhance-menu-field-row">
        <span class="enhance-menu-field-label" title="${esc(f.hint)}">${esc(f.label)}</span>
        ${input}
      </div>`;
    }).join("");
    return `<div class="enhance-menu-fields">${blocks}</div>`;
  }

  /**
   * 菜单项的 HTML 模板 —— **所有面板共用**。
   *
   * @param {object} m      增强方式（注册表项）
   * @param {boolean} checked 是否勾选
   * @param {boolean} ok     当前模式下是否可用
   * @param {object} opts    { unavailableText, specHtml, renderField, valueOf }
   *   unavailableText  不可用时的原因文案（说清楚「它属于哪个功能」）
   *   specHtml         该项自带的模板选择器 HTML（可选）
   *
   * 【审查 P3-1】不可用的项**不再显示 Recommended 徽标** ——
   * 「推荐」与「本面板不可用」并列会让人以为该面板也推荐它，
   * 读起来有歧义。推荐徽标只在**可用**时才有意义。
   */
  function enhanceMenuItemHtml(m, checked, ok, opts) {
    const o = opts || {};
    const why = o.unavailableText || "当前模式不可用";
    const specTag = o.specHtml || "";
    const recBadge = (m.recommended && ok)
      ? '<span class="enhance-menu-badge rec">Recommended</span>' : "";
    return `<div class="enhance-menu-item${checked ? " checked" : ""}${ok ? "" : " unavailable"}" data-method="${esc(m.id)}">
        <label class="enhance-menu-check" title="${esc(m.hint)}">
          <input type="checkbox" data-method-check="${esc(m.id)}"${checked ? " checked" : ""}${ok ? "" : " disabled"}>
          <span class="enhance-menu-label">${esc(m.label)}</span>
          ${specTag}
          ${recBadge}
          ${ok ? "" : `<span class="enhance-menu-badge">${esc(why)}</span>`}
        </label>
        <span class="enhance-menu-hint">${esc(m.hint)}</span>
        ${renderFieldsHtml(m, o)}
      </div>`;
  }

  /**
   * 渲染一份菜单到指定容器 —— **所有面板共用**。
   *
   * cfg:
   *   methods       注册表（有序 = 渲染顺序 = 执行顺序）
   *   selectedIds   当前选中的 id 集合
   *   isAvailable   某项在当前模式下是否可用
   *   onToggle      勾选变化回调 (id, checked)
   *   onFieldEdit   可编辑字段变化回调 (methodId, fieldKey, value)
   *   onSpecChange  模板就地切换回调 (methodId, specId)
   *   unavailableText(method) -> 不可用原因文案
   *   specHtml(method) -> 该项的模板选择器 HTML
   *   renderField / valueOf  自定义可编辑字段渲染
   */
  function renderEnhanceMenu(menu, cfg) {
    if (!menu) return;
    const c = cfg || {};
    menu.innerHTML = (c.methods || []).map(m =>
      enhanceMenuItemHtml(m,
        (c.selectedIds || []).indexOf(m.id) >= 0,
        c.isAvailable ? c.isAvailable(m) : true,
        {
          unavailableText: c.unavailableText ? c.unavailableText(m) : "",
          specHtml: c.specHtml ? c.specHtml(m) : "",
          renderField: c.renderField,
          valueOf: c.valueOf,
        })
    ).join("");

    menu.querySelectorAll("input[data-method-check]").forEach(box => {
      box.addEventListener("change", () => {
        if (c.onToggle) c.onToggle(box.dataset.methodCheck, box.checked);
      });
    });
    // 模板就地切换：改了立刻套用
    menu.querySelectorAll("select[data-spec-method]").forEach(sel => {
      sel.addEventListener("click", e => e.stopPropagation());
      sel.addEventListener("change", () => {
        if (c.onSpecChange) c.onSpecChange(sel.dataset.specMethod, sel.value);
      });
    });
    menu.querySelectorAll(".enhance-menu-field").forEach(el => {
      const onEdit = () => {
        if (c.onFieldEdit) c.onFieldEdit(el.dataset.method, el.dataset.field, el.value);
      };
      el.addEventListener("input", onEdit);
      el.addEventListener("change", onEdit);
    });
  }

  /**
   * 菜单定位在按钮下方；靠近视口右/下边缘时翻转，避免出屏。
   * —— 所有面板共用。
   */
  function positionMenu(menu, btn) {
    if (!menu || !btn) return;
    const r = btn.getBoundingClientRect();
    const mw = menu.offsetWidth || 260;
    const mh = menu.offsetHeight || 160;
    let left = r.left;
    if (left + mw > window.innerWidth - 8) {
      left = Math.max(8, window.innerWidth - mw - 8);
    }
    let top = r.bottom + 6;
    if (top + mh > window.innerHeight - 8) {
      top = Math.max(8, r.top - mh - 6);
    }
    menu.style.left = left + "px";
    menu.style.top = top + "px";
  }

  /**
   * 绑定菜单的开合交互（点箭头开/关、点别处关、Esc 关、窗口变化关）
   * —— 所有面板共用。
   */
  function bindEnhanceMenu(menu, btn, splitId, onOpen) {
    if (!menu) return;
    if (btn) {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (menu.hidden) {
          if (typeof onOpen === "function") onOpen();
          menu.hidden = false;
          btn.setAttribute("aria-expanded", "true");
          positionMenu(menu, btn);
        } else {
          menu.hidden = true;
          btn.setAttribute("aria-expanded", "false");
        }
      });
    }
    document.addEventListener("click", (e) => {
      if (menu.hidden) return;
      if (menu.contains(e.target)) return;
      if (splitId && e.target.closest && e.target.closest(`#${splitId}`)) return;
      menu.hidden = true;
      if (btn) btn.setAttribute("aria-expanded", "false");
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") menu.hidden = true;
    });
    // 窗口尺寸变化时菜单位置会失效 -> 关掉（下次打开会重新定位）
    window.addEventListener("resize", () => { menu.hidden = true; });
  }

  // ------------------------------------------------------------------
  // 能力契约：增强结果的写回目标
  // ------------------------------------------------------------------

  /**
   * 「能力契约」= 一个增强方式**声明**它能写回什么，而不是别人硬编码它的 DOM id。
   *
   * 契约字段：
   *   kind        "text"（一段文本，写回一个输入框）
   *              | "structured"（结构化对象，写回一组字段 + 歌词框）
   *   target      kind=text 时的输入框选择器
   *   targets     kind=text 时的**候选列表**（先新后旧）—— 同一个语义
   *              可能有两个名字（如 #musicPrompt / #musicStyle），
   *              任一存在即可用，避免跨分支合并后又拿到 null
   *   apply(text) kind=text 时的写回动作（默认写 target.value）
   *   apply(obj)  kind=structured 时的写回动作
   *   describe()  给用户看的说明（不可用 / 缺目标时的提示）
   *
   * 为什么要这么设计：
   *   enh-unify 的旧实现硬编码 `$("#musicStyle")`。
   *   yue2-flow 把那个 textarea 改名成 #musicPrompt（音乐描述），
   *   并把「结构化风格」拆成 12 个独立字段 —— 合并后
   *   `document.querySelector("#musicStyle")` 返回 null，
   *   写回会静默抛 TypeError（.value of null）。
   *
   *   改成契约后：
   *     - 每条路线自己声明写回目标（音乐侧声明结构化契约）
   *     - 目标缺失时**不抛异常**，而是给出可读提示
   *     - 两条路线合并时不需要再对齐某个具体的 DOM id
   */
  function resolveWriteTarget(contract) {
    if (!contract) {
      return { ok: false, reason: "该增强方式没有声明写回目标" };
    }
    if (contract.kind === "structured") {
      if (typeof contract.apply !== "function") {
        return { ok: false, reason: "结构化写回契约缺少 apply()" };
      }
      return { ok: true, kind: "structured", apply: contract.apply };
    }
    // kind === "text"：按候选列表逐个找（先新后旧）
    const candidates = [];
    if (Array.isArray(contract.targets)) candidates.push(...contract.targets);
    if (contract.target) candidates.push(contract.target);
    let el = null;
    let used = "";
    for (const sel of candidates) {
      if (!sel) continue;
      const found = document.querySelector(sel);
      if (found) { el = found; used = sel; break; }
    }
    if (!el) {
      const why = contract.describe
        ? contract.describe()
        : `写回目标 ${candidates[0] || "(未声明)"} 不存在`;
      return { ok: false, reason: why };
    }
    return {
      ok: true,
      kind: "text",
      element: el,
      selector: used,
      apply: (text) => {
        if (typeof contract.apply === "function") {
          contract.apply(text, el);
          return;
        }
        el.value = text;
      },
    };
  }

  window.EnhanceMenuShared = {
    esc: esc,
    renderEnhanceMenu: renderEnhanceMenu,
    enhanceMenuItemHtml: enhanceMenuItemHtml,
    renderFieldsHtml: renderFieldsHtml,
    positionMenu: positionMenu,
    bindEnhanceMenu: bindEnhanceMenu,
    resolveWriteTarget: resolveWriteTarget,
  };
})();
