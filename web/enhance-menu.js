/* ============================================================
   提示词增强方式注册表 + 「增强」多选下拉菜单
   ------------------------------------------------------------
   为什么要这一层：
   原来「增强」「二次增强」是两个**并列的独立按钮**，每加一种增强方式
   就要加一个按钮（面板越来越挤，交互也不统一）。
   现在统一成「增强」分裂按钮：
     主按钮  = 执行**当前选中的**增强方式
     右侧箭头 = 展开下拉菜单，切换增强方式

   增强方式注册表（ENHANCE_METHODS）就是唯一的真相来源：
     - 下拉菜单按它渲染
     - 主按钮按它路由
     - 面板的可见性按它决定（哪些方式只对特定模式可用）

   【多选（2026-11-09 用户要求）】
   下拉不再是单选：每一项前面带复选框，可以**同时选中多项**。
   - 点「增强」按钮 = 按**注册表顺序**依次执行选中的增强方式
   - 各项的「推荐参数」是**叠加**语义：后一项只覆盖它自己声明的键，
     不会清掉前一项已套用的参数（最终提交的是叠加后的结果）
   - 各项的「提示词片段」合并追加到最终提示词，互不覆盖、不互相截断
   - 合并后用户仍可在提示词框里手动编辑
   - 选中状态 + 已套用的推荐参数在界面上可见（勾选框 + 参数回显）

   【与结构化增强（layout）的关系】
   「结构化增强（layout）」只是注册表里的一条，调用的是既有的
   POST /api/moss-layout，产物弹窗与回填语义和其它方式一致。
   它**只在 MOSS-Image 面板可用**（其它图像模型不认 layout 坐标），
   所以在 Z-Image / Qwen-Image 面板下该选项会被禁用并说明原因。

   【与官方 PE（qwen_pe）的关系】
   官方 PE 是注册表里的一条**普通增强项**，与其它项同级、可多选。
   它自带两项可编辑字段：
     - systemPrompt：官方 PE 的扩写 system prompt（可编辑）
       -> 扩写与出图共用同一份文本
     - params（presence_penalty / max_new_tokens）：官方 PE 推荐档位
       -> 选中即自动套用，用户可再改
   ============================================================ */
(function () {
  "use strict";

  /**
   * 增强方式注册表。
   *
   * id          内部标识（路由用）
   * label       下拉里显示的名字
   * hint        一句话说明（悬停/副标题）
   * modes       该方式在哪些工作模式下可用；空数组 = 全部模式
   * busyFlag    该方式运行时把哪个 busy 标记置起来（决定「生成」按钮是否禁用）
   * buttonText  选中后主按钮上显示的文字
   * recommended 标注「推荐」（界面上显示 Recommended 徽标）
   * exclusive   true = 与其它项互斥（不能同时选中）
   * fields      该项自带的可编辑字段（渲染在展开区里）
   */
  const ENHANCE_METHODS = [
    {
      id: "basic",
      label: "普通增强",
      hint: "按所选模板把提示词扩写成完整描述",
      modes: [],
      busyFlag: "enhance",
      buttonText: "增强",
    },
    {
      id: "refine",
      label: "二次增强",
      hint: "把提示词里的物体逐个枚举并逐个细化",
      modes: [],
      busyFlag: "refine",
      buttonText: "二次增强",
    },
    {
      id: "layout",
      label: "结构化增强（layout）",
      hint: "生成 MOSS Layout JSON（对象清单 + 位置坐标），并翻译成最终文本提示词",
      // 只对 MOSS-Image 开放：layout 坐标是 MOSS 的能力，其它模型不认
      modes: ["t2i_moss"],
      busyFlag: "layout",
      buttonText: "结构化增强",
    },
    {
      id: "qwen_pe",
      label: "qwen21图片官方增强PE",
      hint: "用 Qwen-Image 2.1 官方 PE 权重与官方 system prompt 扩写（文生图 / 图片编辑各一档）",
      // 只对 Qwen-Image 2.1 开放：官方 PE checkpoint 是 Qwen-Image 2.1 专用的
      modes: ["t2i_qwen", "i2i_qwen"],
      busyFlag: "enhance",
      buttonText: "官方PE扩写",
      // 官方最佳实践 —— 界面上标 Recommended
      recommended: true,
      // 该项自带的可编辑字段：官方 PE 的 system prompt 与档位参数。
      // systemPrompt 走「扩写 + 出图」同一条链路；params 是 PE 节点参数。
      fields: [
        {
          key: "systemPrompt",
          label: "官方 PE system prompt",
          kind: "textarea",
          hint: "官方 PE 扩写所用的指令原文。编辑后，扩写与出图共用这一份文本。",
        },
        {
          key: "presencePenalty",
          label: "presence_penalty",
          kind: "number",
          hint: "官方 PE 档位参数：文生图 1.5 / 图片编辑 0。选中即自动套用，可再改。",
        },
        {
          key: "maxNewTokens",
          label: "max_new_tokens",
          kind: "number",
          hint: "官方 PE 档位参数：文生图 16256 / 图片编辑 24000。不能压到 4096（会静默返回空串）。",
        },
      ],
    },
  ];

  // 当前选中的增强方式 id 集合（多选）。默认只选「普通增强」。
  let selectedIds = ["basic"];

  const byId = {};
  ENHANCE_METHODS.forEach(m => { byId[m.id] = m; });

  /** 当前选中的方式（按注册表顺序 = 执行顺序）。 */
  function selectedMethods() {
    return ENHANCE_METHODS.filter(m => selectedIds.indexOf(m.id) >= 0);
  }

  /** 兼容旧调用方：返回「当前主方式」（多选时取第一个选中的）。 */
  function currentMethod() {
    const sel = selectedMethods();
    return sel[0] || byId.basic;
  }

  /** 某方式在当前模式下是否可用。 */
  function isAvailable(method) {
    if (!method.modes || !method.modes.length) return true;
    const mode = (typeof state !== "undefined" && state) ? (state.mode || "") : "";
    return method.modes.includes(mode);
  }

  /** 可用且被选中的方式（真正会被执行的那些）。 */
  function runnableMethods() {
    return selectedMethods().filter(isAvailable);
  }

  // ------------------------------------------------------------------ 状态

  const STATE_KEY = "h3studio.enhanceSelection.v1";

  /** 把选中集合与可编辑字段持久化到 localStorage（刷新后不丢）。 */
  function saveSelection() {
    try {
      localStorage.setItem(STATE_KEY, JSON.stringify({
        selected: selectedIds,
        fields: fieldValues(),
        touched: Array.from(fieldTouched),
      }));
    } catch (e) { /* 隐私模式下忽略 */ }
  }

  /** 读取持久化状态；损坏/缺失时退回默认值。 */
  function loadSelection() {
    try {
      const raw = localStorage.getItem(STATE_KEY);
      if (!raw) return;
      const data = JSON.parse(raw);
      if (data && Array.isArray(data.selected)) {
        // 只保留注册表里真实存在、且当前模式下可用的 id
        const valid = data.selected.filter(id => byId[id]);
        if (valid.length) selectedIds = valid;
      }
      if (data && data.fields && typeof data.fields === "object") {
        Object.keys(data.fields).forEach(key => {
          if (fieldValues()[key] !== undefined) fieldState[key] = data.fields[key];
        });
      }
      if (data && Array.isArray(data.touched)) {
        data.touched.forEach(key => fieldTouched.add(key));
      }
    } catch (e) { /* 忽略 */ }
  }

  // 各增强项的可编辑字段当前值。key = "<methodId>.<fieldKey>"
  const fieldState = {};

  // 用户**动过**的字段（哪怕改成空串）。
  // 动过的字段用用户的值（不静默回落默认值）；没动过的用默认值。
  const fieldTouched = new Set();

  /** 展开成扁平表：{"qwen_pe.systemPrompt": "...", ...} */
  function fieldValues() {
    const out = {};
    Object.keys(fieldState).forEach(k => { out[k] = fieldState[k]; });
    return out;
  }

  /** 某项某字段的当前值（未设置过 = undefined）。 */
  function fieldValue(methodId, fieldKey) {
    return fieldState[`${methodId}.${fieldKey}`];
  }

  /** 某项某字段是否被用户动过。 */
  function fieldIsTouched(methodId, fieldKey) {
    return fieldTouched.has(`${methodId}.${fieldKey}`);
  }

  /** 记录一次字段编辑（值 + 动过标记）。 */
  function setFieldValue(methodId, fieldKey, value) {
    fieldState[`${methodId}.${fieldKey}`] = value;
    fieldTouched.add(`${methodId}.${fieldKey}`);
  }

  /**
   * 某项某字段的**生效值**：
   *   动过 -> 用用户的值（哪怕是空串）
   *   没动 -> 用默认值（官方 PE 用官方档位 / 官方 system prompt）
   */
  function effectiveFieldValue(method, field) {
    if (fieldIsTouched(method.id, field.key)) {
      const v = fieldValue(method.id, field.key);
      return v === undefined || v === null ? "" : v;
    }
    const d = defaultFieldValue(method, field);
    return d === undefined ? "" : d;
  }

  /** 某项某字段的默认值：官方 PE 用官方档位；其余项 = undefined。 */
  function defaultFieldValue(method, field) {
    if (method.id === "qwen_pe" && field.key === "presencePenalty") {
      const p = currentQwenPeParams();
      return p.presence_penalty;
    }
    if (method.id === "qwen_pe" && field.key === "maxNewTokens") {
      const p = currentQwenPeParams();
      return p.max_new_tokens;
    }
    if (method.id === "qwen_pe" && field.key === "systemPrompt") {
      return officialPeSystemPrompt();
    }
    return undefined;
  }

  /** 当前模式对应的官方 PE 档位参数（presence_penalty / max_new_tokens）。 */
  function currentQwenPeParams() {
    const opts = (typeof QWEN_OPTIONS !== "undefined" && QWEN_OPTIONS)
      ? QWEN_OPTIONS : null;
    const table = opts && opts.peParams ? opts.peParams : null;
    const mode = (typeof state !== "undefined" && state) ? (state.mode || "") : "";
    const tier = mode === "i2i_qwen" ? "i2i" : "t2i";
    if (table && table[tier]) {
      return {
        presence_penalty: table[tier].presence_penalty,
        max_new_tokens: table[tier].max_new_tokens,
        label: table[tier].label,
      };
    }
    // 后端还没下发（未重启）时的兜底：官方卡值
    const fallback = tier === "i2i"
      ? { presence_penalty: 0, max_new_tokens: 24000, label: "PE-I2I（图片编辑）" }
      : { presence_penalty: 1.5, max_new_tokens: 16256, label: "PE-T2I（文生图）" };
    return fallback;
  }

  /**
   * 官方 PE 的 system prompt 原文。
   * 来源：/api/enhancer 下发的模板清单（enhancer.py 的 TEMPLATE_SPECS 里
   * qwen21-pe-t2i / qwen21-pe-i2i 两档的 builtin_system_prompt，
   * 用户在「本地模型配置」里改过则为 custom_system_prompt）。
   * 取不到就返回空串 —— 前端据此提示「后端未就绪」，不编造。
   */
  function officialPeSystemPrompt() {
    const mode = (typeof state !== "undefined" && state) ? (state.mode || "") : "";
    const specId = mode === "i2i_qwen" ? "qwen21-pe-i2i" : "qwen21-pe-t2i";
    const specs = (typeof provState !== "undefined" && provState
                   && provState.agent && provState.agent.specs)
      ? provState.agent.specs : null;
    if (!specs) return "";
    const spec = specs.find(s => s.id === specId);
    if (!spec) return "";
    return spec.custom_system_prompt || spec.builtin_system_prompt || "";
  }

  // ------------------------------------------------------------------ 菜单渲染

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g,
      c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /**
   * 菜单项的 HTML 模板 —— **图像面板与音乐面板共用**。
   *
   * 【为什么要抽出来】音乐面板的「增强」下拉必须与生图面板**同一套组件与视觉**
   * （用户要求：不另造一套）。所以模板只写一份，两个实例各自调用。
   */
  function enhanceMenuItemHtml(m, checked, ok) {
    return `<div class="enhance-menu-item${checked ? " checked" : ""}${ok ? "" : " unavailable"}" data-method="${esc(m.id)}">
        <label class="enhance-menu-check" title="${esc(m.hint)}">
          <input type="checkbox" data-method-check="${esc(m.id)}"${checked ? " checked" : ""}${ok ? "" : " disabled"}>
          <span class="enhance-menu-label">${esc(m.label)}</span>
          ${m.recommended ? '<span class="enhance-menu-badge rec">Recommended</span>' : ""}
          ${ok ? "" : '<span class="enhance-menu-badge">当前模式不可用</span>'}
        </label>
        <span class="enhance-menu-hint">${esc(m.hint)}</span>
        ${m.fields && m.fields.length ? renderFields(m) : ""}
      </div>`;
  }

  /**
   * 渲染一份菜单到指定容器 —— **图像面板与音乐面板共用**。
   *
   * cfg:
   *   methods      注册表（有序 = 渲染顺序 = 执行顺序）
   *   selectedIds  当前选中的 id 集合
   *   isAvailable  某项在当前模式下是否可用
   *   onToggle     勾选变化回调 (id, checked)
   *   onFieldEdit  可编辑字段变化回调 (methodId, fieldKey, value)
   *   renderFields 某项的可编辑字段区渲染（音乐侧目前没有可编辑字段）
   */
  function renderEnhanceMenu(menu, cfg) {
    menu.innerHTML = cfg.methods.map(m =>
      enhanceMenuItemHtml(m, cfg.selectedIds.indexOf(m.id) >= 0, cfg.isAvailable(m))
    ).join("");

    menu.querySelectorAll("input[data-method-check]").forEach(box => {
      box.addEventListener("change", () => {
        cfg.onToggle(box.dataset.methodCheck, box.checked);
      });
    });
    menu.querySelectorAll(".enhance-menu-field").forEach(el => {
      const onEdit = () => {
        cfg.onFieldEdit(el.dataset.method, el.dataset.field, el.value);
      };
      el.addEventListener("input", onEdit);
      el.addEventListener("change", onEdit);
    });
  }

  function renderMenu() {
    const menu = document.querySelector("#enhanceMenu");
    if (!menu) return;
    renderEnhanceMenu(menu, {
      methods: ENHANCE_METHODS,
      selectedIds: selectedIds,
      isAvailable: isAvailable,
      onToggle: toggleMethod,
      onFieldEdit: (methodId, fieldKey, value) => {
        setFieldValue(methodId, fieldKey, value);
        saveSelection();
        applySelectedParams();      // 参数即时套用并回显
        updateButton();
      },
    });
  }

  /**
   * 菜单定位在按钮下方；靠近视口右/下边缘时翻转，避免出屏。
   * —— 图像面板与音乐面板共用。
   */
  function positionMenu(menu, btn) {
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
   * —— **图像面板与音乐面板共用**。
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

  /** 某项的可编辑字段区（system prompt 文本框 + 档位参数）。 */
  function renderFields(method) {
    const blocks = method.fields.map(f => {
      const shown = effectiveFieldValue(method, f);
      const input = f.kind === "textarea"
        ? `<textarea class="enhance-menu-field" rows="4" data-method="${esc(method.id)}" data-field="${esc(f.key)}" title="${esc(f.hint)}">${esc(shown)}</textarea>`
        : `<input type="number" step="${f.key === "presencePenalty" ? "0.1" : "1"}" class="enhance-menu-field" data-method="${esc(method.id)}" data-field="${esc(f.key)}" value="${esc(shown)}" title="${esc(f.hint)}">`;
      return `<div class="enhance-menu-field-row">
        <span class="enhance-menu-field-label" title="${esc(f.hint)}">${esc(f.label)}</span>
        ${input}
      </div>`;
    }).join("");
    return `<div class="enhance-menu-fields">${blocks}</div>`;
  }

  // ------------------------------------------------------------------ 选中与路由

  /**
   * 选中/取消一种增强方式（多选）。
   * @param {string} id
   * @param {boolean} on  true=选中，false=取消
   */
  function toggleMethod(id, on) {
    const method = byId[id];
    if (!method) return;
    if (on && !isAvailable(method)) return;   // 不可用的方式不选中
    if (on) {
      if (selectedIds.indexOf(id) < 0) selectedIds.push(id);
    } else {
      if (selectedIds.length <= 1) {
        // 至少留一项：取消最后一项时保持原状
        // （否则用户点「增强」没反应 —— 比多留一项更糟）
        renderMenu();
        updateButton();
        const hint = document.querySelector("#enhanceHint");
        if (hint) {
          hint.textContent = "至少要保留一项增强方式";
          hint.className = "enhance-hint error";
        }
        return;
      }
      selectedIds = selectedIds.filter(x => x !== id);
    }
    saveSelection();
    renderMenu();
    updateButton();
    updatePanelVisibility();
    applySelectedParams();
    const hint = document.querySelector("#enhanceHint");
    if (hint) {
      hint.textContent = on ? "已选中：" + method.label : "已取消：" + method.label;
      hint.className = "enhance-hint ok";
    }
  }

  /**
   * 当前选中的方式在当前模式下是否仍可用；不可用则剔除。
   *
   * 模式切换时必须做这件事 —— 否则主按钮会停在一个「当前模式不可用」的
   * 方式上（比如在 Qwen 面板选了官方 PE 扩写，切到 Z-Image），
   * 用户点「增强」却什么都没发生（run() 直接 return）。
   */
  function ensureAvailable() {
    const before = selectedIds.length;
    selectedIds = selectedIds.filter(id => {
      const m = byId[id];
      return m && isAvailable(m);
    });
    if (!selectedIds.length) selectedIds = ["basic"];
    if (selectedIds.length !== before) {
      saveSelection();
      renderMenu();
      updateButton();
      updatePanelVisibility();
      return true;
    }
    return false;
  }

  /** 主按钮的文字随当前选中的方式变化。 */
  function updateButton() {
    const text = document.querySelector("#enhanceBtnText");
    const sel = runnableMethods();
    const method = sel[0] || byId.basic;
    if (text) {
      text.textContent = sel.length > 1 ? `增强（${sel.length} 项）` : method.buttonText;
    }
    const btn = document.querySelector("#enhanceBtn");
    if (btn) {
      btn.title = sel.length > 1
        ? "按选中顺序依次执行：" + sel.map(m => m.label).join(" → ")
        : method.hint;
    }
    // 对外同步「当前方式」：多选时取第一个选中的（兼容旧的单选调用方）
    if (typeof window !== "undefined") {
      window.enhanceMethods = window.enhanceMethods || {};
      window.enhanceMethods.current = (sel[0] || byId.basic).id;
      window.enhanceMethods.selected = selectedIds.slice();
    }
  }

  /**
   * 面板可见性：不同增强方式需要显示不同的辅助控件。
   *   - 结构化增强（layout）-> 显示「画面意图补充」栏
   *   - 二次增强           -> 显示「作用对象」切换
   *   - 其它方式           -> 隐藏它们
   *
   * 「版本历史」按钮是**所有增强方式共用**的（在 #refineBar 里），
   * 所以只藏 #refineTargetGroup 这一组，不能藏整个 #refineBar。
   */
  function updatePanelVisibility() {
    const sel = selectedIds;
    const layoutBar = document.querySelector("#mossLayoutBar");
    if (layoutBar) layoutBar.hidden = sel.indexOf("layout") < 0;
    const specSel = document.querySelector("#enhanceSpec");
    if (specSel) {
      // 模板下拉只对**走模板**的方式有意义：
      //   - 结构化增强（layout）走 /api/moss-layout，不用模板
      //   - 官方 PE 扩写走官方 checkpoint + 官方 system prompt，不用模板
      // 只要还有任意一项走模板，普通增强/二次增强就要能选模板 -> 显示。
      const usesTemplate = sel.some(id => id !== "layout" && id !== "qwen_pe");
      specSel.style.display = usesTemplate ? "" : "none";
    }
    const targetGroup = document.querySelector("#refineTargetGroup");
    if (targetGroup) targetGroup.hidden = sel.indexOf("refine") < 0;
  }

  // ------------------------------------------------------------------ 参数套用

  /**
   * 把选中各项的「推荐参数」叠加套用，并回显到界面。
   *
   * 叠加语义：后一项只覆盖它自己声明的键，不会清掉前一项已套用的参数。
   * 最终提交的参数 = 各项叠加后的结果。
   *
   * 官方 PE 的参数落到 state.imgQwen.peParams（出图时随 params 提交）；
   * 其它项目前没有独立参数，声明为空 —— 后续新增增强项时在这里加分支即可。
   */
  function applySelectedParams() {
    if (typeof state === "undefined" || !state || !state.imgQwen) return;
    const merged = {};
    const sel = runnableMethods();
    const hasPe = sel.some(m => m.id === "qwen_pe");
    sel.forEach(m => {
      if (m.id !== "qwen_pe") return;
      const dflt = currentQwenPeParams();
      // 生效值：用户动过用用户的，没动过用官方档位
      const ppRaw = effectiveFieldValue(m, { key: "presencePenalty" });
      const mnRaw = effectiveFieldValue(m, { key: "maxNewTokens" });
      if (ppRaw !== "" && ppRaw !== undefined && ppRaw !== null) {
        merged.presence_penalty = Number(ppRaw);
      } else if (dflt.presence_penalty !== undefined) {
        merged.presence_penalty = dflt.presence_penalty;
      }
      if (mnRaw !== "" && mnRaw !== undefined && mnRaw !== null) {
        merged.max_new_tokens = Number(mnRaw);
      } else if (dflt.max_new_tokens !== undefined) {
        merged.max_new_tokens = dflt.max_new_tokens;
      }
    });
    // 官方 PE 未被选中时清掉（不残留上一轮的参数）
    if (hasPe) {
      state.imgQwen.peParams = merged;
      // system prompt：用户改过就用用户的；没改过用官方内置的
      // （出图链路与手动扩写共用同一份 -> 扩写与出图走同一套规则）
      const peMethod = byId.qwen_pe;
      const sp = effectiveFieldValue(peMethod, { key: "systemPrompt" });
      state.imgQwen.peSystemPrompt = sp || "";
    } else {
      state.imgQwen.peParams = null;
      state.imgQwen.peSystemPrompt = "";
    }
    updateAppliedParamsNote();
  }

  /** 已套用参数的可见回显（「选中即自动应用参数」要让用户看得见）。 */
  function updateAppliedParamsNote() {
    const note = document.querySelector("#enhanceAppliedParams");
    if (!note) return;
    const sel = runnableMethods();
    const parts = [];
    sel.forEach(m => {
      if (m.id === "qwen_pe") {
        const p = (typeof state !== "undefined" && state && state.imgQwen
                   && state.imgQwen.peParams) ? state.imgQwen.peParams : null;
        if (p) {
          parts.push(`官方PE：presence_penalty=${p.presence_penalty}、max_new_tokens=${p.max_new_tokens}`);
        }
      }
    });
    note.textContent = parts.length ? ("已套用推荐参数 → " + parts.join("；")) : "";
    note.hidden = !parts.length;
  }

  // ------------------------------------------------------------------ 路由

  /**
   * 当前出图尺寸 —— layout 的坐标是绝对像素，必须与画布一致。
   *
   * 画布尺寸的**权威是后端** workflows.normalize_moss_image_size()
   * （16 倍数 + 单边 16~16384 + 总像素 0.25M~4M）。
   * 前端 app.js 的 normalizeImgSize() 只是它的一份副本 ——
   * 两份各写一遍必然走偏（这正是本项目反复出现过的「同一常量写两处」问题）。
   *
   * 所以前端**不复制算法**：直接把当前尺寸原样传给后端，
   * 由后端用权威规则归一后返回生效尺寸（响应里的 width/height）。
   * 前端只做最低限度兜底（取不到就退回 1024×1024）。
   */
  function currentCanvasSize() {
    let w = 1024, h = 1024;
    if (typeof imgCurrentSize === "function") {
      const s = imgCurrentSize();
      if (s && s.length >= 2) { w = Number(s[0]) || w; h = Number(s[1]) || h; }
    }
    return [w, h];
  }

  /**
   * 执行当前（或指定）的增强方式。
   *
   * 每个方式各自调自己的后端接口，各自处理成功/失败，
   * 但**回填语义一致**：结果进同一个弹窗、同一个「替换/追加」按钮组，
   * 回填后都标记为「已增强」并进版本历史。
   *
   * 【多选】按注册表顺序依次执行选中的每一项：
   *   - **链式输入**：后一项的输入 = 前一项的产出（真正的「依次应用」）
   *   - 片段合并：每一项产出的「提示词片段」合并追加，互不覆盖
   *   - 参数叠加：每一项的推荐参数叠加套用，后一项不清掉前一项
   */
  async function run(methodId) {
    // 兼容旧调用：run("layout") = 只跑这一项
    if (methodId) {
      return runOne(byId[methodId] || currentMethod());
    }
    const sel = runnableMethods();
    if (!sel.length) {
      setHint("请先在「增强」下拉里选中至少一项", "error");
      return;
    }
    if (sel.length === 1) return runOne(sel[0]);

    // 多项：按注册表顺序依次执行，链式输入 + 片段合并 + 参数叠加
    const merged = [];
    const metas = [];
    let anyPe = false;
    // 链式输入：第一项吃提示词框里的原文，后一项吃前一项的产出
    let input = document.querySelector("#prompt").value.trim();
    for (const method of sel) {
      const piece = await runOne(method, { collect: true, input });
      if (!piece) continue;
      if (piece.expanded) {
        merged.push(piece.expanded);
        input = piece.expanded;      // 下一项接着这一项的产出继续
      }
      if (piece.meta) metas.push(piece.meta);
      if (piece.isPe) anyPe = true;
    }
    if (!merged.length) return;
    const combined = mergeFragments(merged);
    document.querySelector("#enhanceResult").value = combined;
    document.querySelector("#enhanceMeta").innerHTML = metas.join("<br>");
    document.querySelector("#enhanceModal").hidden = false;
    // 【写回标记】合并结果里若含官方 PE 的产出，写回提示词框后
    // 出图就不该再二次扩写（提示词框里已经是官方 PE 结果）。
    // 这个标记必须在**所有项都跑完之后**统一设置 —— 否则先跑的普通增强
    // 会把后跑的官方 PE 标记冲掉（两处生效 = 出图扩写两次）。
    if (anyPe && typeof provState !== "undefined") {
      provState.lastEnhanceKind = "qwen_pe";
    }
    setHint(`已按顺序应用 ${merged.length} 项增强，合并后 ${combined.length} 字`, "ok");
  }

  /**
   * 合并多项增强的提示词片段。
   *
   * 规则（与 LoRA 触发词合并同一套思路）：
   *   - 各片段互不覆盖、不互相截断
   *   - 已存在的片段不重复追加
   *   - 顺序 = 执行顺序
   */
  function mergeFragments(parts) {
    const seen = new Set();
    const out = [];
    parts.forEach(p => {
      const text = String(p || "").trim();
      if (!text) return;
      if (seen.has(text)) return;
      seen.add(text);
      out.push(text);
    });
    return out.join("\n\n");
  }

  /**
   * 执行单个增强方式。
   * @param {object} method
   * @param {object} opts collect=true 时不直接写弹窗，只返回产物（供多选合并）
   */
  async function runOne(method, opts) {
    opts = opts || {};
    if (!isAvailable(method)) return null;
    if (method.id === "layout") {
      return runLayout(method, opts);
    }
    if (method.id === "qwen_pe") {
      return runQwenPe(method, opts);
    }
    if (method.id === "refine") {
      return runRefine(method, opts);
    }
    return runBasic(method, opts);
  }

  // ---- 普通增强（沿用既有 /api/enhance + 模板下拉） ----
  async function runBasic(method, opts) {
    const btn = document.querySelector("#enhanceBtn");
    // 多选时用链式输入（上一项的产出）；单选时用提示词框原文
    const prompt = (opts && opts.input !== undefined)
      ? String(opts.input || "").trim()
      : document.querySelector("#prompt").value.trim();
    if (!prompt) {
      setHint("请先输入要增强的提示词", "error");
      document.querySelector("#prompt").focus();
      return null;
    }
    btn.disabled = true;
    btn.classList.add("busy");
    promptEnhance.busy = true;
    updateActionButtons();
    setHint("正在增强…", "busy");
    try {
      const d = await apiJSON("POST", "/api/enhance", {
        prompt,
        mode: "replace",
        spec_id: provState.specId || "image",
        mode_ctx: enhanceCurrentMode(),
        model_key: enhanceCurrentModelKey(),
      });
      provState.lastEnhance = d;
      // 只有**单独**跑普通增强时才清标记；多选时由 run() 统一设置，
      // 否则先跑的普通增强会把后跑的官方 PE 标记冲掉。
      if (!opts.collect) provState.lastEnhanceKind = "";   // 普通增强，不是官方 PE
      if (d.spec_id && d.spec_id !== provState.specId) {
        provState.specId = d.spec_id;
        renderEnhanceSpecSelect();
      }
      const enhanced = d.enhanced || "";
      if (opts.collect) {
        return { expanded: enhanced, meta: buildEnhanceMeta(d) };
      }
      document.querySelector("#enhanceResult").value = enhanced;
      document.querySelector("#enhanceMeta").innerHTML = buildEnhanceMeta(d);
      document.querySelector("#enhanceModal").hidden = false;
      if (d.enhanced) {
        pushPromptVersion(0, prompt, "原始提示词");
        pushPromptVersion(1, d.enhanced, `模板：${d.spec_id || "image"}`, d.spec_id);
      }
      setHint(`已用「${d.provider}」扩写，${d.enhanced ? d.enhanced.length : 0} 字`, "ok");
      return { expanded: enhanced, meta: buildEnhanceMeta(d) };
    } catch (e) {
      const detail = e.payload && e.payload.error ? e.payload.error : e.message;
      const who = e.payload && e.payload.provider ? `（${e.payload.provider}）` : "";
      setHint(`增强失败：${detail}`, "error");
      alert(`提示词增强失败${who}\n\n${detail}\n\n请检查「本地模型配置」里的 Provider 是否可用。`);
      return null;
    } finally {
      btn.disabled = false;
      btn.classList.remove("busy");
      promptEnhance.busy = false;
      updateActionButtons();
    }
  }

  // ---- 二次增强（沿用既有 /api/enhance + image-refine 模板） ----
  async function runRefine(method, opts) {
    const btn = document.querySelector("#enhanceBtn");
    const text = document.querySelector("#enhanceBtnText");
    // 多选时用链式输入（上一项的产出）；单选时用既有语义
    // （refineTarget=1 时取「一次增强」版本，否则取提示词框原文）
    const source = (opts && opts.input !== undefined)
      ? String(opts.input || "").trim()
      : refineSourcePrompt();
    if (!source) {
      setHint("请先输入要细化的提示词", "error");
      document.querySelector("#prompt").focus();
      return null;
    }
    const targetLevel = promptVersions.refineTarget === 1 ? 1 : 0;
    btn.disabled = true;
    text.textContent = "正在细化…";
    promptVersions.busy = true;
    updateActionButtons();
    setHint("正在逐个物体细化…", "busy");
    try {
      const d = await apiJSON("POST", "/api/enhance", {
        prompt: source,
        mode: "replace",
        spec_id: "image-refine",
        mode_ctx: enhanceCurrentMode(),
        model_key: enhanceCurrentModelKey(),
      });
      if (d.spec_id) { provState.specId = d.spec_id; renderEnhanceSpecSelect(); }
      if (!opts.collect) provState.lastEnhanceKind = "";   // 二次增强，不是官方 PE
      const refined = d.enhanced || "";
      if (!refined) throw new Error("本地模型返回了空结果");
      if (opts.collect) {
        return { expanded: refined, meta: buildEnhanceMeta(d) };
      }
      document.querySelector("#prompt").value = refined;
      pushPromptVersion(2, refined, `模板：${d.spec_id || "image-refine"}`, d.spec_id);
      markPromptEnhanced();
      updatePromptBadge();
      renderPromptVersions();
      pushPromptVersion(targetLevel, source,
        targetLevel === 1 ? "一次增强（本次二次增强的输入）" : "原始提示词（本次二次增强的输入）");
      document.querySelector("#enhanceResult").value = refined;
      document.querySelector("#enhanceMeta").innerHTML = buildEnhanceMeta(d);
      document.querySelector("#enhanceModal").hidden = false;
      setHint(`二次增强完成（${d.provider}）：逐个物体细化 ${refined.length} 字`, "ok");
      return { expanded: refined, meta: buildEnhanceMeta(d) };
    } catch (e) {
      const detail = e.payload && e.payload.error ? e.payload.error : e.message;
      const who = e.payload && e.payload.provider ? `（${e.payload.provider}）` : "";
      setHint(`二次增强失败：${detail}`, "error");
      alert(`二次增强失败${who}\n\n${detail}\n\n请检查「本地模型配置」里的 Provider 是否可用。`);
      return null;
    } finally {
      promptVersions.busy = false;
      btn.disabled = false;
      updateButton();   // 多选：按钮文字取决于选中项数，不能只取第一项
      updateActionButtons();
    }
  }

  // ---- 结构化增强（layout）—— 调既有 /api/moss-layout ----
  async function runLayout(method, opts) {
    const btn = document.querySelector("#enhanceBtn");
    const text = document.querySelector("#enhanceBtnText");
    const prompt = document.querySelector("#prompt").value.trim();
    if (!prompt) {
      setHint("请先输入要增强的提示词", "error");
      document.querySelector("#prompt").focus();
      return null;
    }
    const intentEl = document.querySelector("#mossLayoutIntent");
    const intent = intentEl ? intentEl.value.trim() : "";
    const [w, h] = currentCanvasSize();
    btn.disabled = true;
    text.textContent = "生成中…";
    promptEnhance.busy = true;
    updateActionButtons();
    setHint("正在生成 MOSS Layout…", "busy");
    try {
      const d = await apiJSON("POST", "/api/moss-layout", {
        prompt, intent, width: w, height: h,
      });
      // 产物渲染交给 moss-layout.js（弹窗的归属方）——
      // 这里只管「取数据 -> 交给视图 -> 显示弹窗」
      const view = window.mossLayoutUI;
      if (!view || typeof view.renderResult !== "function") {
        throw new Error("结构化增强视图未就绪，请刷新页面后重试");
      }
      view.renderResult(d);
      view.openModal();
      setHint(`结构化增强完成（${d.provider}）：${d.layout.items.length} 个元素`, "ok");
      return { expanded: "", meta: "" };
    } catch (e) {
      const detail = e.payload && e.payload.error ? e.payload.error : e.message;
      const who = e.payload && e.payload.provider ? `（${e.payload.provider}）` : "";
      setHint(`结构化增强失败：${detail}`, "error");
      alert(`MOSS 结构化增强失败${who}\n\n${detail}\n\n`
        + `请检查「本地模型配置」里的 Provider 是否可用。`);
      return null;
    } finally {
      promptEnhance.busy = false;
      btn.disabled = false;
      updateButton();   // 多选：按钮文字取决于选中项数，不能只取第一项
      updateActionButtons();
    }
  }

  // ---- Qwen-Image 2.1 官方 PE 扩写 —— 调 /api/qwen-pe/expand ----
  //
  // 与其它增强方式的**回填语义完全一致**：
  //   结果进同一个弹窗（#enhanceResult）+ 同一个「替换/追加」按钮组，
  //   替换后标记为「已增强」并进版本历史，用户可继续编辑提示词。
  //
  // 差异只有两点：
  //   1. 不走 Provider 配置，走官方 PE checkpoint（ComfyUI TextGenerate）；
  //   2. 图片编辑模式下要把参考图一起送去 —— 官方 PE-I2I 工作流就是这么接的
  //      （BatchImagesNode -> TextGenerate.image），PE 才能「看着图」改写，
  //      并在输出里写 <image1>/<image2>。
  //
  // 【system prompt 与档位参数】
  //   这两项来自该增强项自带的可编辑字段（用户改过就以用户的为准），
  //   没改过就用官方内置值（后端 /api/enhancer 下发的模板 + 官方卡值）。
  //   同一份 system prompt 也随出图参数提交 -> 扩写与出图走同一套规则。
  async function runQwenPe(method, opts) {
    const btn = document.querySelector("#enhanceBtn");
    const text = document.querySelector("#enhanceBtnText");
    // 多选时用链式输入（上一项的产出）；单选时用提示词框原文
    const prompt = (opts && opts.input !== undefined)
      ? String(opts.input || "").trim()
      : document.querySelector("#prompt").value.trim();
    if (!prompt) {
      setHint("请先输入要增强的提示词", "error");
      document.querySelector("#prompt").focus();
      return null;
    }
    const mode = (typeof state !== "undefined" && state) ? (state.mode || "") : "";
    if (mode !== "t2i_qwen" && mode !== "i2i_qwen") {
      setHint("官方 PE 扩写只在 Qwen-Image 2.1 的文生图 / 图片编辑模式下可用", "error");
      return null;
    }
    // 参考图：素材库来源，顺序即 <imageN> 顺序（与出图链路同一份 payload）
    let refs = [];
    if (mode === "i2i_qwen" && window.QwenRefs
        && typeof window.QwenRefs.toPayload === "function") {
      refs = window.QwenRefs.toPayload();
    }
    // 可编辑字段：用户改过就带上，没改过就不传（后端用官方内置值）
    const peMethod = byId.qwen_pe;
    const sp = effectiveFieldValue(peMethod, { key: "systemPrompt" });
    const peParams = {};
    const pp = effectiveFieldValue(peMethod, { key: "presencePenalty" });
    const mn = effectiveFieldValue(peMethod, { key: "maxNewTokens" });
    if (pp !== "" && pp !== undefined && pp !== null) peParams.presence_penalty = Number(pp);
    if (mn !== "" && mn !== undefined && mn !== null) peParams.max_new_tokens = Number(mn);
    const payload = { prompt, mode, refs };
    if (sp !== undefined && sp !== null && String(sp).trim()) {
      payload.system_prompt = String(sp);
    }
    if (Object.keys(peParams).length) payload.pe_params = peParams;

    btn.disabled = true;
    text.textContent = "官方PE扩写中…";
    promptEnhance.busy = true;
    updateActionButtons();
    setHint("官方 PE 扩写中（9B 模型 + thinking，约需半分钟）…", "busy");
    try {
      const d = await apiJSON("POST", "/api/qwen-pe/expand", payload);
      const expanded = d.expanded || "";
      if (!expanded) throw new Error("官方 PE 返回了空结果");
      const meta = buildQwenPeMeta(d);
      if (opts.collect) {
        // 多选：只回报「这次是官方 PE 的」，写回标记由 run() 统一设置
        return { expanded, meta, isPe: true };
      }
      // 回填到弹窗（与其它增强方式同一个弹窗 / 同一组按钮）
      document.querySelector("#enhanceResult").value = expanded;
      document.querySelector("#enhanceMeta").innerHTML = meta;
      document.querySelector("#enhanceModal").hidden = false;
      // 记录「这次结果是官方 PE 的」—— 真正**写回提示词框**时
      // （用户在弹窗里点「替换提示词框 / 追加到原提示词」）
      // 才把出图时的二次扩写关掉。providers.js 那边消费这个标记。
      // （不能在拿到结果时就关：那只是弹窗里的预览，用户可能直接取消。）
      if (typeof provState !== "undefined") {
        provState.lastEnhanceKind = "qwen_pe";
      }
      // 进版本历史（与普通增强一致）：
      //   level 0 = 原始提示词，level 1 = 官方 PE 扩写结果。
      // 不做这一步的话，写回提示词框后「已增强」徽标不会出现
      // （徽标靠版本谱系判定级别，不是靠 markPromptEnhanced）。
      if (expanded) {
        pushPromptVersion(0, prompt, "原始提示词");
        pushPromptVersion(1, expanded,
          `官方 PE：${d.tier_label || d.tier || ""}`.trim(), "qwen21-pe");
      }
      setHint(`官方 PE 扩写完成（${d.tier_label || d.tier}）：${expanded.length} 字；`
        + `点「替换提示词框」写回后，出图将不再二次扩写`, "ok");
      return { expanded, meta };
    } catch (e) {
      const detail = e.payload && e.payload.error ? e.payload.error : e.message;
      setHint(`官方 PE 扩写失败：${detail}`, "error");
      alert(`Qwen 官方 PE 扩写失败\n\n${detail}\n\n`
        + `请确认 ComfyUI 正在运行，且官方 PE 权重（qwen3.5_qwen_image_2.1_pe_*.safetensors）已就位。`);
      return null;
    } finally {
      promptEnhance.busy = false;
      btn.disabled = false;
      updateButton();   // 多选：按钮文字取决于选中项数，不能只取第一项
      updateActionButtons();
    }
  }

  /** 官方 PE 扩写结果的 meta 行（与其它增强方式同一块区域）。 */
  function buildQwenPeMeta(d) {
    const parts = [];
    parts.push(`<b>官方 PE</b>：${esc(d.tier_label || d.tier || "-")}`);
    if (d.clip_name) {
      parts.push(`checkpoint：<code>${esc(d.clip_name)}</code>`);
    }
    if (d.ref_count) parts.push(`参考图：${Number(d.ref_count)} 张`);
    if (d.pe_params) {
      parts.push(`已套用参数：presence_penalty=${esc(d.pe_params.presence_penalty)}、`
        + `max_new_tokens=${esc(d.pe_params.max_new_tokens)}`);
    }
    parts.push(`字数：${(d.expanded || "").length}`);
    return parts.join(" · ");
  }

  function setHint(text, kind) {
    const hint = document.querySelector("#enhanceHint");
    if (!hint) return;
    hint.textContent = text;
    hint.className = "enhance-hint" + (kind ? " " + kind : "");
  }

  // ------------------------------------------------------------------ 绑定

  function bind() {
    const btn = document.querySelector("#enhanceBtn");
    if (btn) btn.addEventListener("click", () => run());

    // 菜单开合交互走**共用实现**（与音乐面板同一套组件）
    bindEnhanceMenu(document.querySelector("#enhanceMenu"),
                    document.querySelector("#enhanceMenuBtn"),
                    "enhanceSplit", renderMenu);
  }

  // ------------------------------------------------------------------ 启动

  function boot() {
    bind();
    loadSelection();
    ensureAvailable();
    updateButton();
    renderMenu();
    updatePanelVisibility();
    applySelectedParams();
    if (typeof window !== "undefined") {
      window.enhanceMethods = {
        current: selectedIds[0] || "basic",
        selected: selectedIds.slice(),
        list: ENHANCE_METHODS,
        toggle: toggleMethod,
        run: run,
        renderMenu: renderMenu,
        updatePanelVisibility: updatePanelVisibility,
        ensureAvailable: ensureAvailable,
        refreshButton: updateButton,
        mergeFragments: mergeFragments,
        getSelected: () => selectedIds.slice(),
        getFieldValue: fieldValue,
        getEffectiveFieldValue: (methodId, fieldKey) => {
          const m = byId[methodId];
          if (!m) return "";
          const f = (m.fields || []).find(x => x.key === fieldKey);
          return f ? effectiveFieldValue(m, f) : "";
        },
      };

      /**
       * 共用组件导出 —— 音乐面板（web/music-enhance.js）用**同一套**
       * 菜单渲染 / 定位 / 开合交互，不另造一套 UI。
       */
      window.EnhanceMenuShared = {
        renderEnhanceMenu: renderEnhanceMenu,
        positionMenu: positionMenu,
        bindEnhanceMenu: bindEnhanceMenu,
        enhanceMenuItemHtml: enhanceMenuItemHtml,
        esc: esc,
      };
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
