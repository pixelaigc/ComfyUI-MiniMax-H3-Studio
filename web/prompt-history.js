/* ============================================================
   提示词历史（Prompt History）—— 各功能面板的提示词留痕浮层
   ------------------------------------------------------------
   【它解决什么】
   每个带提示词的面板（MOSS-Image / Z-Image / Qwen-Image 2.1 文生图与图片编辑 /
   文生视频 / YuE2 音乐）都能查看自己这个面板用过的历史提示词。
   每条记录同时带【原始提示词】与【增强后的提示词】，
   点一下就填回当前面板的提示词框。

   【为什么浮层挂到 body】
   窄屏（≤1100px）下 #panel 是 `position: fixed` + `transform: translateY(0)`
   （web/responsive.css）。**CSS 规定：带 transform 的祖先会成为 fixed 定位的
   包含块** —— 浮层若挂在面板里，它的 top 不再相对视口，而是相对那个被
   transform 的 #panel 的滚动内容盒，于是整块弹到视口上方（实测 −979px）。
   这正是此前「增强」下拉出框问题的同一个根因。
   修法与 enhance-menu.js 一致：打开时 append 到 document.body，
   关闭时挂回原位 —— 既不受 transform 包含块影响，也不受父级 overflow 裁剪。

   【面板隔离】
   面板 key 就是后端真实注册的模式名（t2i / t2i_qwen / i2i_qwen / t2i_moss /
   t2v / flf / r2v / t2a / a2a），查询时带上 panel_key —— MOSS 的历史不会
   出现在 Z-Image 面板里，反之亦然。
   注意：#prompt 这个输入框是图像/视频各模式**共用**的，所以隔离完全靠
   panel_key（= state.mode），不靠 DOM。

   【回填目标】
   按「能力契约」解析当前面板的提示词输入框：
     * 图像 / 视频面板 -> #prompt
     * 音乐面板          -> #musicPrompt
   找不到就明确提示，不静默失败。

   【命名注意】
   本模块的状态对象叫 phState，**不能叫 state** ——
   app.js 顶层有 `const state = {...}`，同名会把它遮蔽掉，
   于是「当前模式」永远读不到。
   ============================================================ */
(function () {
  "use strict";

  const $ = s => document.querySelector(s);
  const $$ = s => [...document.querySelectorAll(s)];

  /** 单次拉取的条数（滚动加载时每次再拉这么多）。 */
  const PAGE_SIZE = 20;

  /** 浮层状态。 */
  const phState = {
    activeBtn: null,     // 打开浮层的那个按钮（面板滚动时要跟着 reposition）
    panelKey: "",        // 当前面板（= 后端模式名）
    inputSelector: "",   // 回填目标选择器
    label: "提示词",     // 该输入框在面板里的名字（提示文案用）
    items: [],           // 已加载的记录
    offset: 0,           // 下一批的 offset
    total: 0,            // 该面板的总条数
    hasMore: false,
    loading: false,
  };

  // ---------------------------------------------------------------- 工具

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g,
      c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function fmtTime(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    const p = n => String(n).padStart(2, "0");
    return `${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  /** 摘要：单行 + 限长。 */
  function summarize(text, max) {
    const t = String(text || "").replace(/\s+/g, " ").trim();
    if (!t) return "";
    return t.length > max ? t.slice(0, max) + "…" : t;
  }

  /**
   * 轻量 toast（复用素材库的 .al-toast 样式）。
   * 不依赖 assets.js —— 素材库页没加载时也得有反馈。
   */
  function toast(message, kind) {
    let box = $("#phToast");
    if (!box) {
      box = document.createElement("div");
      box.id = "phToast";
      box.className = "al-toast";
      document.body.appendChild(box);
    }
    box.textContent = message;
    box.className = "al-toast show " + (kind || "");
    clearTimeout(box._timer);
    box._timer = setTimeout(() => { box.className = "al-toast " + (kind || ""); }, 2600);
  }

  // ---------------------------------------------------------------- 面板解析

  /**
   * 当前面板的「提示词输入框契约」。
   *
   * 面板 key 用 **state.mode** —— 它就是后端注册的模式名，
   * 也正是后端写入历史时用的 panel_key，两边天然对齐。
   */
  function resolvePanel() {
    const app = window.__h3state ? window.__h3state() : null;
    const mode = (app && app.mode) ? app.mode : "";
    const music = mode === "t2a" || mode === "a2a";
    return {
      panelKey: mode,
      inputSelector: music ? "#musicPrompt" : "#prompt",
      label: music ? "音乐描述" : "提示词",
    };
  }

  // ---------------------------------------------------------------- 数据

  async function fetchPage(panelKey, offset) {
    const query = new URLSearchParams({
      panel_key: panelKey,
      limit: String(PAGE_SIZE),
      offset: String(offset),
    });
    const resp = await fetch("/api/prompt-history?" + query.toString(), { cache: "no-store" });
    if (!resp.ok) {
      let detail = "读取失败";
      try {
        const body = await resp.json();
        detail = body.error || detail;
      } catch (e) { /* 保持默认文案 */ }
      throw new Error(detail);
    }
    return resp.json();
  }

  // ---------------------------------------------------------------- 渲染

  function renderItem(item) {
    const enhanced = item.enhanced_prompt || "";
    const hasEnhanced = !!enhanced && enhanced !== item.original_prompt;
    const count = Number(item.use_count || 1);
    return `
      <div class="ph-item" data-id="${esc(item.id)}">
        <div class="ph-item-head">
          <span class="ph-time">${esc(fmtTime(item.timestamp))}</span>
          ${item.model_key ? `<span class="ph-model">${esc(item.model_key)}</span>` : ""}
          ${count > 1 ? `<span class="ph-count">用过 ${count} 次</span>` : ""}
          ${item.source === "enhance" ? `<span class="ph-src">增强</span>` : ""}
        </div>
        <div class="ph-prompt-row">
          <span class="ph-tag">原始</span>
          <span class="ph-text" title="${esc(item.original_prompt)}">${esc(summarize(item.original_prompt, 120))}</span>
        </div>
        <div class="ph-prompt-row">
          <span class="ph-tag ph-tag-enh">${hasEnhanced ? "增强" : "未增强"}</span>
          <span class="ph-text${hasEnhanced ? "" : " ph-text-none"}" title="${esc(enhanced || "")}">${esc(hasEnhanced ? summarize(enhanced, 120) : "本次未走增强流程")}</span>
        </div>
        <div class="ph-item-actions">
          <button type="button" data-fill="original" data-id="${esc(item.id)}">填入原始</button>
          <button type="button" data-fill="enhanced" data-id="${esc(item.id)}"${hasEnhanced ? "" : " disabled title=\"这条记录没有增强后的提示词\""}>填入增强</button>
        </div>
      </div>`;
  }

  function bindItemActions() {
    $$("#phList [data-fill]").forEach(btn => {
      btn.addEventListener("click", () => {
        const item = phState.items.find(x => String(x.id) === btn.dataset.id);
        if (!item) return;
        const which = btn.dataset.fill;
        const text = which === "enhanced" ? item.enhanced_prompt : item.original_prompt;
        if (!text) {
          toast("这条记录没有可填入的内容", "err");
          return;
        }
        fillPrompt(text, which);
      });
    });
  }

  function render() {
    const list = $("#phList");
    if (!list) return;
    if (!phState.items.length) {
      list.innerHTML = `<div class="ph-empty">这个面板还没有提示词历史。<br>提交一次生成或跑一次增强就会出现在这里。</div>`;
      $("#phMore").hidden = true;
      $("#phCount").textContent = "";
      return;
    }
    list.innerHTML = phState.items.map(renderItem).join("");
    $("#phMore").hidden = !phState.hasMore;
    $("#phCount").textContent = `${phState.items.length} / ${phState.total}`;
    bindItemActions();
  }

  /**
   * 把文本写回当前面板的提示词输入框。
   *
   * 写回后要**触发 input 事件** —— 否则：
   *   * #prompt 的「手动编辑 -> 增强标记失效」逻辑不会跑
   *   * 音乐面板的 st.prompt 不会被更新（下次刷新会被旧值覆盖）
   */
  function fillPrompt(text, which) {
    const el = $(phState.inputSelector);
    if (!el) {
      toast(`找不到${phState.label}输入框，无法填入`, "err");
      return;
    }
    el.value = text;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.focus();
    // 填入增强版 = 用户采用了一个**增强结果** -> 标记为已增强。
    //
    // 【为什么不能只调 markPromptEnhanced()】
    // 「已增强」徽标的显隐由 currentPromptLevel() 判定，而它是**谱系驱动**的：
    // 只有内容出现在 promptVersions.versions 里才算「增强级」。
    // 从历史填进来的内容不在谱系里 —— 于是 markPromptEnhanced() 刚设的
    // promptEnhance.text 会被 currentPromptLevel() 的「手动编辑过 -> 视为原始级」
    // 规则覆盖成 0，徽标不亮，生成时还会弹「提示词尚未增强」确认框。
    //
    // 所以这里把填进来的文本**写进版本谱系**（level 1 = 一次增强），
    // 让徽标、生成拦截、版本历史三者保持一致。
    if (which === "enhanced") {
      if (typeof pushPromptVersion === "function") {
        pushPromptVersion(1, text, "从提示词历史填入");
      }
      if (typeof markPromptEnhanced === "function") markPromptEnhanced();
      if (typeof updatePromptBadge === "function") updatePromptBadge();
      if (window.MusicUI && typeof window.MusicUI.markEnhanced === "function") {
        window.MusicUI.markEnhanced();
      }
    } else {
      // 填入原始版 -> 回到原始级（徽标熄灭，与「未增强直出」一致）
      if (typeof pushPromptVersion === "function") {
        pushPromptVersion(0, text, "从提示词历史填入");
      }
      if (typeof clearPromptEnhanced === "function") clearPromptEnhanced();
      if (typeof updatePromptBadge === "function") updatePromptBadge();
    }
    toast(which === "enhanced" ? "已填入增强后的提示词" : "已填入原始提示词", "ok");
    close();
  }

  // ---------------------------------------------------------------- 浮层

  const POPOVER_HOME = new WeakMap();

  /** 挂到 body（脱离 transform 包含块 + 父级 overflow 裁剪）。 */
  function mountToBody(pop) {
    if (!pop) return;
    if (pop.parentElement === document.body) return;
    POPOVER_HOME.set(pop, pop.parentElement);
    document.body.appendChild(pop);
    pop.style.zIndex = "1400";
  }

  function restoreHome(pop) {
    if (!pop) return;
    const home = POPOVER_HOME.get(pop);
    if (!home || pop.parentElement === home) return;
    home.appendChild(pop);
    pop.style.zIndex = "";
    POPOVER_HOME.delete(pop);
  }

  /**
   * 定位浮层 —— 贴按钮，靠近视口边缘时翻转 / 夹紧，**绝不越界**。
   *
   * 与 enhance-menu-shared.js 的 positionMenu 同思路，但额外做了：
   *   * 宽度自适应（视口 - 16px）
   *   * 高度自适应（视口内最大高度，超出内部滚动）
   */
  function positionPopover(pop, btn) {
    if (!pop || !btn) return;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const margin = 8;
    const gap = 6;

    pop.style.maxWidth = Math.max(240, vw - margin * 2) + "px";
    pop.style.maxHeight = Math.max(200, vh - margin * 2) + "px";

    const r = btn.getBoundingClientRect();
    const pw = pop.offsetWidth || 320;
    const ph = pop.offsetHeight || 320;

    // 水平：默认靠左对齐按钮；右侧越界则左移到视口内
    let left = r.left;
    if (left + pw > vw - margin) left = vw - pw - margin;
    if (left < margin) left = margin;

    // 垂直：默认在按钮下方；下方放不下则翻到上方
    let top = r.bottom + gap;
    if (top + ph > vh - margin) {
      top = r.top - gap - ph;
    }
    // 【关键】最终夹紧：无论上面算出什么，浮层必须**完整落在视口内**。
    //   只判「翻到上方」是不够的 —— 按钮本身可能在视口下方（面板里要滚动），
    //   此时「上方」也放不下，若不夹紧就会整块弹到视口外（实测 top=787 > 577）。
    if (top + ph > vh - margin) top = vh - ph - margin;
    if (top < margin) top = margin;

    pop.style.left = left + "px";
    pop.style.top = top + "px";
  }

  async function open(btn) {
    const pop = $("#phPopover");
    if (!pop) return;
    const panel = resolvePanel();
    if (!panel.panelKey) {
      toast("当前面板还没有标识，无法读取历史", "err");
      return;
    }
    phState.activeBtn = btn;
    phState.panelKey = panel.panelKey;
    phState.inputSelector = panel.inputSelector;
    phState.label = panel.label;
    phState.items = [];
    phState.offset = 0;
    phState.total = 0;
    phState.hasMore = false;
    phState.loading = false;

    $("#phTitle").textContent = panel.label + "历史";
    $("#phList").innerHTML = `<div class="ph-empty">正在读取…</div>`;
    pop.hidden = false;
    mountToBody(pop);
    positionPopover(pop, btn);

    try {
      const data = await fetchPage(phState.panelKey, 0);
      phState.items = data.items || [];
      phState.total = data.total || 0;
      phState.hasMore = !!data.has_more;
      phState.offset = phState.items.length;
      render();
      // 内容到位后**重新定位** —— 打开时浮层是空的（矮），
      // 填完记录后变高，不重定位就会溢出视口。
      positionPopover(pop, phState.activeBtn);
    } catch (e) {
      $("#phList").innerHTML = `<div class="ph-empty ph-empty-err">读取失败：${esc(e.message)}</div>`;
      toast("读取提示词历史失败：" + e.message, "err");
    }
  }

  function close() {
    const pop = $("#phPopover");
    if (!pop || pop.hidden) return;
    pop.hidden = true;
    restoreHome(pop);
  }

  async function loadMore() {
    if (phState.loading || !phState.hasMore) return;
    phState.loading = true;
    const btn = $("#phMore");
    if (btn) btn.disabled = true;
    try {
      const data = await fetchPage(phState.panelKey, phState.offset);
      phState.items = phState.items.concat(data.items || []);
      phState.total = data.total || phState.total;
      phState.hasMore = !!data.has_more;
      phState.offset = phState.items.length;
      render();
    } catch (e) {
      toast("加载更多失败：" + e.message, "err");
    } finally {
      phState.loading = false;
      const b = $("#phMore");
      if (b) b.disabled = false;
    }
  }

  // ---------------------------------------------------------------- 绑定

  function bind() {
    // 每个面板的「历史」入口按钮
    $$("[data-ph-open]").forEach(btn => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const pop = $("#phPopover");
        if (!pop) return;
        if (!pop.hidden) { close(); return; }
        open(btn);
      });
    });

    const pop = $("#phPopover");
    if (pop) {
      pop.addEventListener("click", (e) => e.stopPropagation());
      const closeBtn = $("#phClose");
      if (closeBtn) closeBtn.addEventListener("click", close);
      const moreBtn = $("#phMore");
      if (moreBtn) moreBtn.addEventListener("click", loadMore);
    }

    // 点别处关闭
    document.addEventListener("click", () => close());

    // Esc 关闭
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") close();
    });

    // 窗口尺寸变化 -> 定位失效，关掉（下次打开重新定位）
    window.addEventListener("resize", () => close());

    // 参数面板是可滚动的（overflow-y:auto）—— 面板滚动时按钮会移动，
    // 而浮层是 position:fixed（相对视口），不跟着动就会「飘」在原地。
    // 所以面板一滚动就重新定位。scroll 事件不冒泡，用捕获阶段监听。
    document.addEventListener("scroll", () => {
      const pop = $("#phPopover");
      if (!pop || pop.hidden || !phState.activeBtn) return;
      positionPopover(pop, phState.activeBtn);
    }, true);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }

  window.PromptHistory = { open, close, resolvePanel };
})();
