/* ============================================================
   提示词增强方式注册表 + 「增强」下拉菜单
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
   新增一种增强方式 = 在注册表里加一条，前后端不用改。

   【与结构化增强（layout）的关系】
   「结构化增强（layout）」只是注册表里的一条，调用的是既有的
   POST /api/moss-layout，产物弹窗与回填语义和其它方式一致。
   它**只在 MOSS-Image 面板可用**（其它图像模型不认 layout 坐标），
   所以在 Z-Image / Qwen-Image 面板下该选项会被禁用并说明原因。
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
    },
  ];

  // 当前选中的增强方式（默认普通增强）
  let currentId = "basic";

  const byId = {};
  ENHANCE_METHODS.forEach(m => { byId[m.id] = m; });

  function currentMethod() {
    return byId[currentId] || byId.basic;
  }

  /** 某方式在当前模式下是否可用。 */
  function isAvailable(method) {
    if (!method.modes || !method.modes.length) return true;
    const mode = (typeof state !== "undefined" && state) ? (state.mode || "") : "";
    return method.modes.includes(mode);
  }

  // ------------------------------------------------------------------ 菜单渲染

  function renderMenu() {
    const menu = document.querySelector("#enhanceMenu");
    if (!menu) return;
    menu.innerHTML = ENHANCE_METHODS.map(m => {
      const ok = isAvailable(m);
      const active = m.id === currentId;
      return `<button type="button" role="menuitem" data-method="${esc(m.id)}"`
        + ` class="${active ? "active" : ""}"${ok ? "" : " disabled"}`
        + ` title="${esc(m.hint)}">`
        + `<span class="enhance-menu-label">${esc(m.label)}</span>`
        + `<span class="enhance-menu-hint">${esc(m.hint)}</span>`
        + (ok ? "" : `<span class="enhance-menu-badge">当前模式不可用</span>`)
        + `</button>`;
    }).join("");
    menu.querySelectorAll("button[data-method]").forEach(btn => {
      btn.addEventListener("click", () => {
        if (btn.disabled) return;
        selectMethod(btn.dataset.method);
        closeMenu();
      });
    });
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g,
      c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // ------------------------------------------------------------------ 选中与路由

  /**
   * 选中一种增强方式。
   * @param {string} id
   * @param {boolean} silent  true = 只切换不跑（默认）；false = 切换后立刻执行
   */
  function selectMethod(id, silent) {
    const method = byId[id];
    if (!method) return;
    if (!isAvailable(method)) return;      // 不可用的方式不切换
    currentId = id;
    updateButton();
    renderMenu();
    updatePanelVisibility();
    if (typeof window !== "undefined") {
      window.enhanceMethods = window.enhanceMethods || {};
      window.enhanceMethods.current = id;
    }
    if (!silent) {
      const hint = document.querySelector("#enhanceHint");
      if (hint) {
        hint.textContent = "已切换增强方式：" + method.label;
        hint.className = "enhance-hint ok";
      }
    }
  }

  /**
   * 当前选中的方式在当前模式下是否仍可用；不可用则回落到普通增强。
   *
   * 模式切换时必须做这件事 —— 否则主按钮会停在一个「当前模式不可用」的
   * 方式上（比如在 Qwen 面板选了官方 PE 扩写，切到 Z-Image），
   * 用户点「增强」却什么都没发生（run() 直接 return）。
   */
  function ensureAvailable() {
    const method = currentMethod();
    if (isAvailable(method)) return false;
    selectMethod("basic", true);
    return true;
  }

  /** 主按钮的文字随当前方式变化。 */
  function updateButton() {
    const text = document.querySelector("#enhanceBtnText");
    const method = currentMethod();
    if (text) text.textContent = method.buttonText;
    const btn = document.querySelector("#enhanceBtn");
    if (btn) btn.title = method.hint;
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
    const layoutBar = document.querySelector("#mossLayoutBar");
    if (layoutBar) layoutBar.hidden = currentId !== "layout";
    const specSel = document.querySelector("#enhanceSpec");
    if (specSel) {
      // 模板下拉对结构化增强无意义（那条链路不走模板），
      // 官方 PE 扩写同理（走官方 checkpoint + 官方 system prompt，不走模板）
      specSel.style.display =
        (currentId === "layout" || currentId === "qwen_pe") ? "none" : "";
    }
    const targetGroup = document.querySelector("#refineTargetGroup");
    if (targetGroup) targetGroup.hidden = currentId !== "refine";
  }

  // ------------------------------------------------------------------ 菜单开合

  function openMenu() {
    const menu = document.querySelector("#enhanceMenu");
    const btn = document.querySelector("#enhanceMenuBtn");
    if (!menu || !btn) return;
    renderMenu();
    menu.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    positionMenu(menu, btn);
  }

  function closeMenu() {
    const menu = document.querySelector("#enhanceMenu");
    const btn = document.querySelector("#enhanceMenuBtn");
    if (!menu) return;
    menu.hidden = true;
    if (btn) btn.setAttribute("aria-expanded", "false");
  }

  function toggleMenu() {
    const menu = document.querySelector("#enhanceMenu");
    if (!menu) return;
    if (menu.hidden) openMenu(); else closeMenu();
  }

  /** 菜单定位在按钮下方；靠近视口右/下边缘时翻转，避免出屏。 */
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
   */
  async function run(methodId) {
    const method = byId[methodId || currentId] || currentMethod();
    if (!isAvailable(method)) return;
    if (method.id === "layout") {
      return runLayout(method);
    }
    if (method.id === "qwen_pe") {
      return runQwenPe(method);
    }
    if (method.id === "refine") {
      return runRefine(method);
    }
    return runBasic(method);
  }

  // ---- 普通增强（沿用既有 /api/enhance + 模板下拉） ----
  async function runBasic(method) {
    const btn = document.querySelector("#enhanceBtn");
    const prompt = document.querySelector("#prompt").value.trim();
    if (!prompt) {
      setHint("请先输入要增强的提示词", "error");
      document.querySelector("#prompt").focus();
      return;
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
      provState.lastEnhanceKind = "";   // 普通增强，不是官方 PE
      if (d.spec_id && d.spec_id !== provState.specId) {
        provState.specId = d.spec_id;
        renderEnhanceSpecSelect();
      }
      document.querySelector("#enhanceResult").value = d.enhanced || "";
      document.querySelector("#enhanceMeta").innerHTML = buildEnhanceMeta(d);
      document.querySelector("#enhanceModal").hidden = false;
      if (d.enhanced) {
        pushPromptVersion(0, prompt, "原始提示词");
        pushPromptVersion(1, d.enhanced, `模板：${d.spec_id || "image"}`, d.spec_id);
      }
      setHint(`已用「${d.provider}」扩写，${d.enhanced ? d.enhanced.length : 0} 字`, "ok");
    } catch (e) {
      const detail = e.payload && e.payload.error ? e.payload.error : e.message;
      const who = e.payload && e.payload.provider ? `（${e.payload.provider}）` : "";
      setHint(`增强失败：${detail}`, "error");
      alert(`提示词增强失败${who}\n\n${detail}\n\n请检查「本地模型配置」里的 Provider 是否可用。`);
    } finally {
      btn.disabled = false;
      btn.classList.remove("busy");
      promptEnhance.busy = false;
      updateActionButtons();
    }
  }

  // ---- 二次增强（沿用既有 /api/enhance + image-refine 模板） ----
  async function runRefine(method) {
    const btn = document.querySelector("#enhanceBtn");
    const text = document.querySelector("#enhanceBtnText");
    const source = refineSourcePrompt();
    if (!source) {
      setHint("请先输入要细化的提示词", "error");
      document.querySelector("#prompt").focus();
      return;
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
      provState.lastEnhanceKind = "";   // 二次增强，不是官方 PE
      const refined = d.enhanced || "";
      if (!refined) throw new Error("本地模型返回了空结果");
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
    } catch (e) {
      const detail = e.payload && e.payload.error ? e.payload.error : e.message;
      const who = e.payload && e.payload.provider ? `（${e.payload.provider}）` : "";
      setHint(`二次增强失败：${detail}`, "error");
      alert(`二次增强失败${who}\n\n${detail}\n\n请检查「本地模型配置」里的 Provider 是否可用。`);
    } finally {
      promptVersions.busy = false;
      btn.disabled = false;
      text.textContent = currentMethod().buttonText;
      updateActionButtons();
    }
  }

  // ---- 结构化增强（layout）—— 调既有 /api/moss-layout ----
  async function runLayout(method) {
    const btn = document.querySelector("#enhanceBtn");
    const text = document.querySelector("#enhanceBtnText");
    const prompt = document.querySelector("#prompt").value.trim();
    if (!prompt) {
      setHint("请先输入要增强的提示词", "error");
      document.querySelector("#prompt").focus();
      return;
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
    } catch (e) {
      const detail = e.payload && e.payload.error ? e.payload.error : e.message;
      const who = e.payload && e.payload.provider ? `（${e.payload.provider}）` : "";
      setHint(`结构化增强失败：${detail}`, "error");
      alert(`MOSS 结构化增强失败${who}\n\n${detail}\n\n`
        + `请检查「本地模型配置」里的 Provider 是否可用。`);
    } finally {
      promptEnhance.busy = false;
      btn.disabled = false;
      text.textContent = currentMethod().buttonText;
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
  async function runQwenPe(method) {
    const btn = document.querySelector("#enhanceBtn");
    const text = document.querySelector("#enhanceBtnText");
    const prompt = document.querySelector("#prompt").value.trim();
    if (!prompt) {
      setHint("请先输入要增强的提示词", "error");
      document.querySelector("#prompt").focus();
      return;
    }
    const mode = (typeof state !== "undefined" && state) ? (state.mode || "") : "";
    if (mode !== "t2i_qwen" && mode !== "i2i_qwen") {
      setHint("官方 PE 扩写只在 Qwen-Image 2.1 的文生图 / 图片编辑模式下可用", "error");
      return;
    }
    // 参考图：素材库来源，顺序即 <imageN> 顺序（与出图链路同一份 payload）
    let refs = [];
    if (mode === "i2i_qwen" && window.QwenRefs
        && typeof window.QwenRefs.toPayload === "function") {
      refs = window.QwenRefs.toPayload();
    }
    btn.disabled = true;
    text.textContent = "官方PE扩写中…";
    promptEnhance.busy = true;
    updateActionButtons();
    setHint("官方 PE 扩写中（9B 模型 + thinking，约需半分钟）…", "busy");
    try {
      const d = await apiJSON("POST", "/api/qwen-pe/expand", {
        prompt, mode, refs,
      });
      const expanded = d.expanded || "";
      if (!expanded) throw new Error("官方 PE 返回了空结果");
      // 回填到弹窗（与其它增强方式同一个弹窗 / 同一组按钮）
      document.querySelector("#enhanceResult").value = expanded;
      document.querySelector("#enhanceMeta").innerHTML = buildQwenPeMeta(d);
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
    } catch (e) {
      const detail = e.payload && e.payload.error ? e.payload.error : e.message;
      setHint(`官方 PE 扩写失败：${detail}`, "error");
      alert(`Qwen 官方 PE 扩写失败\n\n${detail}\n\n`
        + `请确认 ComfyUI 正在运行，且官方 PE 权重（qwen3.5_qwen_image_2.1_pe_*.safetensors）已就位。`);
    } finally {
      promptEnhance.busy = false;
      btn.disabled = false;
      text.textContent = currentMethod().buttonText;
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

    const menuBtn = document.querySelector("#enhanceMenuBtn");
    if (menuBtn) {
      menuBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        toggleMenu();
      });
    }
    // 点别处关菜单
    document.addEventListener("click", (e) => {
      const menu = document.querySelector("#enhanceMenu");
      if (!menu || menu.hidden) return;
      if (menu.contains(e.target)) return;
      if (e.target.closest && e.target.closest("#enhanceSplit")) return;
      closeMenu();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeMenu();
    });
    // 窗口尺寸变化时菜单位置会失效 -> 关掉（下次打开会重新定位）
    window.addEventListener("resize", () => closeMenu());
  }

  // ------------------------------------------------------------------ 启动

  function boot() {
    bind();
    updateButton();
    renderMenu();
    updatePanelVisibility();
    // 模式切换后：当前方式若在新模式下不可用，回落到普通增强
    const prev = currentMethod();
    if (!isAvailable(prev)) selectMethod("basic", true);
    else updatePanelVisibility();
    if (typeof window !== "undefined") {
      window.enhanceMethods = {
        current: currentId,
        list: ENHANCE_METHODS,
        select: selectMethod,
        run: run,
        renderMenu: renderMenu,
        updatePanelVisibility: updatePanelVisibility,
        ensureAvailable: ensureAvailable,
      };
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
