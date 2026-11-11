/* ============================================================
   视频类别 → 模型 的两级导航（2026-11-11）
   ------------------------------------------------------------
   【为什么要改】
   原来左侧「视频」下面是平铺的四个入口（文生视频 / 首尾帧 / 参考生成 /
   长片拼接），每个自带模型。需求要求改成两级：
     1) 第一级「视频类别」：固定四项，顺序与文案不可变 ——
        文生视频 / 首尾帧生视频 / 参考生视频 / 长片拼接
     2) 第二级：该类别下的具体模型项

   【单一真源】
   类别 → 模型的映射**只有服务端一份**（h3studio/video_categories.py），
   前端从 GET /api/video-categories 拿清单。
   本文件**不再**维护任何 mode/model 映射表 ——
   两处各写一份映射表是本项目反复踩过的坑（前端说能建、后端说不能，
   就是这么来的）。

   【向后兼容】
   老 URL / 老参数 / 老任务记录里的 mode（t2v / flf / r2v / longfilm）
   一个字都没改。类别只是给它们套了一层分组视图：
     - `legacy_modes`（mode -> 类别）由服务端下发，用来把老 mode
       反查回它该落在哪个类别下；
     - 老任务卡片的「复用」按钮照样能 `.nav-item[data-mode=...]` 点中。

   【状态管理】
     - 默认选中：服务端下发 default_category / default_model。
     - 持久化：localStorage 记住「上次用的类别 + 该类别下上次用的模型项」。
     - 切换类别时的模型回退规则（需求原文）：
         ① 该类别上次用过的模型项 → ② 该类别首个可用模型项 → ③ 禁用并提示
       ① 由 localStorage 的 per-category 记忆实现；
       ② 由服务端下发的 models 列表顺序 + available 位实现；
       ③ 在本文件里落地（无可用模型项时禁用类别入口并给出提示）。
   ============================================================ */
(function () {
  "use strict";

  const STORAGE_KEY = "h3studio.videoNav";

  /** 服务端下发的清单（GET /api/video-categories） */
  const DATA = {
    categories: [],          // [{key, label, media, models:[...], default_model, modes}]
    models: [],              // 扁平模型项（带 category）
    legacy_modes: {},        // {mode: category}
    legacy_labels: {},       // {mode: 旧显示名}
    default_category: "",
    default_model: "",
  };

  /** 当前状态：类别 + 模型项 key（不是 mode！） */
  const navState = {
    category: "",
    modelKey: "",
    /** 每个类别上次用过的模型项 key —— 回退规则①的落点 */
    lastModelByCategory: {},
    ready: false,
  };

  let onSelect = null;        // 选中回调 (category, modelKey, mode) => void
  let onUnavailable = null;   // 类别无可用模型时的提示回调 (category) => void

  // ------------------------------------------------------------ 工具

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g,
      c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function loadPersisted() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return {};
      const parsed = JSON.parse(raw);
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (e) { return {}; }
  }

  function persist() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        category: navState.category,
        modelKey: navState.modelKey,
        lastModelByCategory: navState.lastModelByCategory,
      }));
    } catch (e) { /* 存储不可用：静默降级，不影响功能 */ }
  }

  function categoryByKey(key) {
    return DATA.categories.find(c => c.key === key) || null;
  }

  function modelByKey(key) {
    for (const cat of DATA.categories) {
      const hit = (cat.models || []).find(m => m.key === key);
      if (hit) return hit;
    }
    return null;
  }

  /** 该类别下**可用**的模型项（available 不为 false）。 */
  function availableModels(category) {
    const cat = categoryByKey(category);
    if (!cat) return [];
    return (cat.models || []).filter(m => m.available !== false);
  }

  /**
   * 切换类别时的模型回退规则（需求原文）：
   *   ① 该类别上次用过的模型项
   *   ② 该类别首个可用模型项
   *   ③ 禁用并提示
   */
  function resolveModelForCategory(category) {
    const usable = availableModels(category);
    if (!usable.length) return "";               // ③ 无可用模型项
    // ① 上次用过的
    const remembered = navState.lastModelByCategory[category];
    if (remembered) {
      const hit = usable.find(m => m.key === remembered);
      if (hit) return hit.key;
    }
    // ② 首个可用
    return usable[0].key;
  }

  // ------------------------------------------------------------ 渲染

  /** 渲染左侧「视频」分组的两级结构。 */
  function render(container) {
    if (!container) return;
    container.innerHTML = "";
    if (!DATA.categories.length) return;

    DATA.categories.forEach(cat => {
      const usable = availableModels(cat.key);
      const hasModels = usable.length > 0;

      // ---- 第一级：类别标签 ----
      const catEl = document.createElement("div");
      catEl.className = "nav-cat";
      catEl.dataset.category = cat.key;
      catEl.setAttribute("role", "group");
      catEl.setAttribute("aria-label", cat.label);

      const head = document.createElement("button");
      head.type = "button";
      head.className = "nav-cat-head";
      head.dataset.category = cat.key;
      head.dataset.role = "category";
      head.disabled = !hasModels;
      head.title = hasModels
        ? `${cat.label}（${usable.length} 个模型）`
        : `${cat.label}：本机没有可用的模型`;
      head.innerHTML =
        `<span class="nav-cat-label">${esc(cat.label)}</span>`
        + `<span class="nav-cat-caret" aria-hidden="true">${hasModels ? "▾" : "—"}</span>`;
      catEl.appendChild(head);

      // ---- 第二级：模型项 ----
      const list = document.createElement("div");
      list.className = "nav-cat-models";
      list.dataset.category = cat.key;
      (cat.models || []).forEach(model => {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "nav-item nav-model-item";
        // data-mode 保留老 mode —— 老任务卡片的「复用」按钮按
        // `.nav-item[data-mode=...]` 点选，必须还能命中。
        item.dataset.mode = model.mode;
        item.dataset.category = cat.key;
        item.dataset.modelKey = model.key;
        item.textContent = model.label;
        item.title = model.note || model.label;
        if (model.available === false) {
          item.disabled = true;
          item.classList.add("unavailable");
          item.textContent = model.label + "（权重缺失，不可用）";
        }
        list.appendChild(item);
      });
      catEl.appendChild(list);

      container.appendChild(catEl);
    });
  }

  /** 高亮当前类别与模型项。 */
  function syncActive(container) {
    if (!container) return;
    container.querySelectorAll(".nav-cat").forEach(catEl => {
      const key = catEl.dataset.category;
      const isActive = key === navState.category;
      catEl.classList.toggle("active", isActive);
      const head = catEl.querySelector(".nav-cat-head");
      if (head) {
        head.classList.toggle("active", isActive);
        head.setAttribute("aria-expanded", isActive ? "true" : "false");
      }
    });
    container.querySelectorAll(".nav-model-item").forEach(item => {
      const isActive = item.dataset.category === navState.category
        && item.dataset.modelKey === navState.modelKey;
      item.classList.toggle("active", isActive);
    });
  }

  // ------------------------------------------------------------ 选择

  function select(category, modelKey, opts) {
    const options = opts || {};
    const cat = categoryByKey(category);
    if (!cat) return false;
    const usable = availableModels(category);
    if (!usable.length) {
      // ③ 禁用并提示
      if (onUnavailable) onUnavailable(category);
      return false;
    }
    let key = modelKey;
    if (!key || !usable.some(m => m.key === key)) {
      key = resolveModelForCategory(category);
    }
    if (!key) {
      if (onUnavailable) onUnavailable(category);
      return false;
    }
    const model = usable.find(m => m.key === key);
    navState.category = category;
    navState.modelKey = key;
    navState.lastModelByCategory[category] = key;
    persist();
    if (!options.silent && onSelect) {
      onSelect(category, key, model.mode, model);
    }
    return true;
  }

  /** 从一个**老 mode** 选中对应的类别 + 模型项（向后兼容入口）。 */
  function selectByMode(mode) {
    const category = DATA.legacy_modes[mode];
    if (!category) return false;
    const cat = categoryByKey(category);
    if (!cat) return false;
    const model = (cat.models || []).find(m => m.mode === mode);
    return select(category, model ? model.key : "", { silent: true });
  }

  // ------------------------------------------------------------ 初始化

  async function load() {
    try {
      const resp = await fetch("/api/video-categories", { cache: "no-store" });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const data = await resp.json();
      DATA.categories = data.categories || [];
      DATA.models = data.models || [];
      DATA.legacy_modes = data.legacy_modes || {};
      DATA.legacy_labels = data.legacy_labels || {};
      DATA.default_category = data.default_category || "";
      DATA.default_model = data.default_model || "";
    } catch (e) {
      console.error("[视频导航] 载入类别清单失败:", e.message);
      DATA.categories = [];
      return false;
    }

    // 恢复持久化状态
    const saved = loadPersisted();
    if (saved && typeof saved.lastModelByCategory === "object"
        && saved.lastModelByCategory) {
      navState.lastModelByCategory = { ...saved.lastModelByCategory };
    }
    navState.ready = true;
    return true;
  }

  /** 绑定点击（事件委托，动态渲染的节点也能响应）。 */
  function bind(container) {
    if (!container) return;
    container.addEventListener("click", event => {
      const item = event.target.closest(".nav-model-item");
      if (item) {
        if (item.disabled) return;
        event.preventDefault();
        select(item.dataset.category, item.dataset.modelKey);
        return;
      }
      const head = event.target.closest(".nav-cat-head");
      if (head) {
        if (head.disabled) {
          event.preventDefault();
          select(head.dataset.category, "");
          return;
        }
        event.preventDefault();
        // 点类别标题 = 选中该类别（模型按回退规则选）
        select(head.dataset.category, "");
      }
    });
  }

  /** 首屏默认选中：持久化 > 服务端默认。 */
  function applyDefault() {
    const saved = loadPersisted();
    let category = saved && saved.category ? saved.category : DATA.default_category;
    let modelKey = saved && saved.modelKey ? saved.modelKey : "";
    if (!categoryByKey(category)) {
      category = DATA.default_category;
      modelKey = "";
    }
    if (!modelKey && category === DATA.default_category) {
      modelKey = DATA.default_model;
    }
    select(category, modelKey, { silent: true });
  }

  window.VideoNav = {
    DATA,
    state: navState,
    load,
    bind,
    render,
    syncActive,
    select,
    selectByMode,
    applyDefault,
    availableModels,
    resolveModelForCategory,
    categoryByKey,
    modelByKey,
    set onSelect(fn) { onSelect = fn; },
    set onUnavailable(fn) { onUnavailable = fn; },
  };
})();
