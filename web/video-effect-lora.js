/* ==================================================================
 * H3 文生视频面板 —— 效果 LoRA 选择器（可搜索 / 可多选 / 逐项强度）
 *
 * 【交互对齐文生图】
 * 文生图的 LoRA 区（#imgLoraField）是这套结构：
 *   下拉添加 -> 已选列表（每项：启停 / 排序 / 删除 / 强度滑杆 / 触发词）
 * 这里沿用同一套结构，只把「添加」那一步换成「输入即搜索 + 多选勾选」，
 * 因为本机 H3 效果 LoRA 有 27 枚，原生下拉翻找成本太高。
 *
 * 【数据来源】
 * /api/video-effect-loras —— 服务端扫盘 + safetensors 头实测（键绑定 +
 * 形状比对）得到的清单。前端**只渲染服务端下发的条目**，
 * ���渲染本地硬编码列表（那等于假选项）。
 *
 * 【与加速 LoRA 的关系】
 * 两者并列共存：加速 LoRA 改采样语义（20 步 -> 4 步），
 * 效果 LoRA 改画面风格。互不排斥。
 * ================================================================== */
(function () {
  "use strict";

  /** 服务端下发的效果 LoRA 清单（空 = 还没加载） */
  const EFFECT_STORE = {
    available: [],
    incompatible: [],
    model: "",
    loaderNode: "",
    loaderNote: "",
    sourceNote: "",
    // 选中项是否已经从 /api/video-config 恢复过。
    // 只有恢复过之后，才允许「按当前底模剔除不兼容项」——
    // 否则会误删尚未恢复的选中项（详见 loadEffectLoras 的注释）。
    restored: false,
    // 从 /api/video-config 读回来的原始选中项（等待清单到达后应用）
    pendingRestore: null,
  };

  /** 面板状态里已选的效果 LoRA（[{name, strength, enabled, trigger}]） */
  function selectedList() {
    const st = window.VIDEO_PANEL && window.VIDEO_PANEL.state;
    if (!st) return [];
    if (!Array.isArray(st.effectLoras)) st.effectLoras = [];
    return st.effectLoras;
  }

  function setSelected(list) {
    const st = window.VIDEO_PANEL && window.VIDEO_PANEL.state;
    if (st) st.effectLoras = list;
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g,
      c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function metaOf(name) {
    return EFFECT_STORE.available.find(x => x.name === name) || null;
  }

  /** 建议强度：作者给了就用作者的，否则 1.0（不是编造 —— 作者没写就是没写） */
  function recommendedStrength(item) {
    const rec = Number(item && item.recommended_strength);
    return rec > 0 ? rec : 1.0;
  }

  function strengthCap(item) {
    const cap = Number(item && item.strength_cap);
    return cap > 0 ? Math.min(cap, 2.0) : 1.5;
  }

  // ------------------------------------------------------------ 候选菜单

  /**
   * 候选列表的过滤：按「名称 / 功能 / 触发词 / 文件名」做不区分大小写的
   * 子串匹配。搜不到就如实说「没有匹配」，不偷偷展示全部。
   */
  function filterCandidates(keyword) {
    const kw = (keyword || "").trim().toLowerCase();
    const used = new Set(selectedList().map(x => x.name));
    const pool = EFFECT_STORE.available.filter(x => !used.has(x.name));
    if (!kw) return pool;
    return pool.filter(item => {
      const hay = [
        item.display || "",
        item.name || "",
        item.scene || "",
        item.trigger || "",
        item.directory || "",
      ].join(" ").toLowerCase();
      return hay.includes(kw);
    });
  }

  /**
   * 候选下拉的渲染（2026-11-14 需求 9：统一到文生图的原生下拉样式）。
   *
   * 【为什么改成原生 <select>】原先是「搜索框 + 自绘多选弹层」，
   * 视觉与交互跟同页的文生图 LoRA 区（#imgLoraField）不一致 ——
   * 一个是原生下拉 + 「添加」按钮，一个是自绘浮层 + 复选框。
   * 同一个页面里两套 LoRA 选择控件，用户要学两遍。
   *
   * 【搜索能力保留在哪】搜索框降级为「下拉的过滤器」：
   * 输入关键字 -> 只显示匹配项的原生下拉 -> 选一项 -> 点「添加」。
   * 27 枚 LoRA 依然能快速定位，但主交互与文生图完全一致。
   */
  function renderPickerMenu() {
    const sel = document.getElementById("vidEffectLoraSelect");
    const search = document.getElementById("vidEffectLoraSearch");
    const addBtn = document.getElementById("vidEffectLoraAdd");
    if (!sel) return;
    const kw = search ? search.value : "";
    const items = filterCandidates(kw);
    const total = EFFECT_STORE.available.length;

    if (!items.length) {
      const label = total
        ? (kw ? `没有匹配「${kw}」的效果 LoRA` : "全部已添加")
        : "本机未检测到可用的 H3 效果 LoRA 文件";
      sel.innerHTML = `<option value="">${esc(label)}</option>`;
      sel.disabled = true;
      if (addBtn) addBtn.disabled = true;
      return;
    }

    // 下拉选项文案与文生图一致：显示名 + 一句话功能说明（title 挂完整说明）
    sel.innerHTML = items.map(item => {
      const scene = (item.scene || "").split(/[。；\n]/)[0].trim();
      const brief = scene ? ` — ${scene.slice(0, 28)}` : "";
      const title = item.scene ? ` title="${esc(item.scene)}"` : "";
      const rec = recommendedStrength(item);
      return `<option value="${esc(item.name)}"${title}>${esc(item.display || item.name)}${esc(brief)}</option>`;
    }).join("");
    sel.disabled = false;
    if (addBtn) addBtn.disabled = false;
    // 供调试与测试核对（推荐强度等信息不在下拉文本里，避免撑太长）
    sel.dataset.recommendedStrength = String(recommendedStrength(items[0]));
  }

  function openPicker() {
    renderPickerMenu();
  }

  function closePicker() {
    // 原生下拉没有「浮层」需要关闭 —— 保留空实现以兼容旧调用点。
  }

  // ------------------------------------------------------------ 已选列表

  /**
   * 已选列表的渲染 —— 结构与文生图完全一致：
   * 启停按钮 / 名称 / 标签 / 上移下移 / 删除 + 强度滑杆 + 触发词输入框。
   */
  function renderEffectLoraList() {
    const box = document.getElementById("vidEffectLoraList");
    if (!box) return;
    const list = selectedList();
    if (!list.length) {
      box.innerHTML = "";
      renderEffectLoraNote();
      return;
    }
    box.innerHTML = list.map((item, index) => {
      const meta = metaOf(item.name);
      const disabled = item.enabled === false || Number(item.strength) <= 0;
      const cap = strengthCap(meta);
      const rec = meta ? recommendedStrength(meta) : 0;
      const isRec = rec > 0 && Number(item.strength) === rec;
      const triggerText = item.trigger !== undefined && item.trigger !== null
        ? item.trigger : (meta && meta.trigger ? meta.trigger : "");
      return `<div class="img-lora-item ${disabled ? "disabled" : ""}" data-idx="${index}">
        <div class="img-lora-head">
          <button type="button" class="img-lora-btn toggle" title="${disabled ? "启用" : "停用"}">${disabled ? "○" : "●"}</button>
          <span class="img-lora-name" title="${esc(item.name)}">${esc(meta ? (meta.display || item.name) : item.name)}</span>
          ${isRec ? `<span class="img-lora-tag rec">推荐强度</span>` : ""}
          ${meta && meta.adaln_spin ? `<span class="img-lora-tag" title="这枚 LoRA 有 ${meta.adaln_spin} 个 adaln 层对当前底模是空转">adaln 空转 ${meta.adaln_spin}</span>` : ""}
          <button type="button" class="img-lora-btn up" title="上移" ${index === 0 ? "disabled" : ""}>↑</button>
          <button type="button" class="img-lora-btn down" title="下移" ${index === list.length - 1 ? "disabled" : ""}>↓</button>
          <button type="button" class="img-lora-btn del" title="删除">×</button>
        </div>
        <div class="img-lora-slider">
          <input type="range" class="strength" min="0" max="${cap}" step="0.05"
                 value="${Math.min(Number(item.strength), cap)}"
                 title="强度 0-${cap}，0 表示关闭">
          <span class="img-lora-strength">${Number(item.strength).toFixed(2)}</span>
        </div>
        <div class="img-lora-trigger">
          <span class="lora-trigger-label" title="该 LoRA 贡献的触发词，会自动追加到提示词末尾">触发词</span>
          <input type="text" class="lora-trigger-input" value="${esc(triggerText)}"
                 placeholder="${meta && meta.trigger ? esc(meta.trigger) : "（上游未标注触发词）"}"
                 title="自动追加到最终提示词；可编辑或删除">
        </div>
        ${meta && meta.scene ? `<div class="img-lora-meta">${esc(meta.scene)}</div>` : ""}
      </div>`;
    }).join("");

    box.querySelectorAll(".img-lora-item").forEach(el => {
      const index = Number(el.dataset.idx);
      const item = selectedList()[index];
      if (!item) return;
      el.querySelector(".toggle").addEventListener("click", () => {
        item.enabled = item.enabled === false;
        renderEffectLoraList();
        saveEffectLoraConfig();
      });
      el.querySelector(".up").addEventListener("click", () => {
        if (index === 0) return;
        const list = selectedList();
        [list[index - 1], list[index]] = [list[index], list[index - 1]];
        renderEffectLoraList();
        saveEffectLoraConfig();
      });
      el.querySelector(".down").addEventListener("click", () => {
        const list = selectedList();
        if (index >= list.length - 1) return;
        [list[index + 1], list[index]] = [list[index], list[index + 1]];
        renderEffectLoraList();
        saveEffectLoraConfig();
      });
      el.querySelector(".del").addEventListener("click", () => {
        selectedList().splice(index, 1);
        renderEffectLoraList();
        saveEffectLoraConfig();
      });
      const slider = el.querySelector("input.strength");
      slider.addEventListener("input", () => {
        item.strength = Number(slider.value);
        el.querySelector(".img-lora-strength").textContent = item.strength.toFixed(2);
        el.classList.toggle("disabled", item.enabled === false || item.strength <= 0);
      });
      slider.addEventListener("change", () => saveEffectLoraConfig());
      const trig = el.querySelector("input.lora-trigger-input");
      if (trig) {
        trig.addEventListener("input", () => { item.trigger = trig.value; });
        trig.addEventListener("change", () => saveEffectLoraConfig());
      }
    });

    renderEffectLoraNote();
  }

  function renderEffectLoraNote() {
    const note = document.getElementById("vidEffectLoraNote");
    if (!note) return;
    const list = selectedList();
    const available = EFFECT_STORE.available.length;
    const blocked = EFFECT_STORE.incompatible.length;
    renderClearButton();
    if (!list.length) {
      note.textContent = available
        ? `未添加效果 LoRA。可选本机已安装的 H3 效果 LoRA（${available} 个）—— ` +
          `在上面的搜索框里按名称 / 功能 / 触发词筛选，可多选。` +
          (blocked ? ` 另有 ${blocked} 个与当前底模不兼容，已排除。` : "")
        : "本机未检测到可用的 H3 效果 LoRA 文件。";
      return;
    }
    const parts = [list.map(it => {
      const meta = metaOf(it.name);
      const label = meta ? (meta.display || meta.name) : it.name;
      return `「${label}」×${Number(it.strength).toFixed(2)}`;
    }).join(" + ")];
    parts.push("效果 LoRA 与加速 LoRA 并列共存（加速改采样语义，效果改画面风格）。");
    if (EFFECT_STORE.loaderNote) parts.push(EFFECT_STORE.loaderNote);
    note.textContent = parts.join(" ");
  }

  /**
   * 「清空全部」按钮的显隐。
   *
   * 【为什么必须有】多选交互如果没有全清，用户只能一项项点 ×。
   * 选到 5 枚再想全部撤销 —— 5 次点击。这是多选控件的基本要求。
   *
   * 没有选中任何项时整块隐藏（不占视觉空间）。
   */
  function renderClearButton() {
    const btn = document.getElementById("vidEffectLoraClear");
    if (!btn) return;
    const n = selectedList().length;
    btn.hidden = n === 0;
    btn.textContent = n > 1 ? `清空全部（${n}）` : "清空全部";
    btn.title = n ? `移除已选的 ${n} 个效果 LoRA` : "";
  }

  /**
   * 清空全部已选的效果 LoRA。
   *
   * 【清的是「已选列表」，不是「候选清单」】
   * 候选清单是本机扫出来的真实清单，不该被用户操作改掉；
   * 清空只影响用户自己勾选的那些（即 state.effectLoras）。
   */
  function clearAll() {
    const list = selectedList();
    if (!list.length) return;
    const n = list.length;
    list.splice(0, list.length);
    renderEffectLoraList();
    renderPickerMenu();
    saveEffectLoraConfig();
    return n;
  }

  function renderExcludedEffectLoras() {
    const box = document.getElementById("vidEffectLoraExcluded");
    if (!box) return;
    const items = EFFECT_STORE.incompatible || [];
    if (!items.length) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    box.hidden = false;
    box.innerHTML = `<div class="img-lora-excluded-title">已排除（与当前底模不兼容）</div>` +
      items.map(it => `<div class="img-lora-excluded-item" title="${esc(it.compatible_reason || "")}">
        <span class="img-lora-excluded-name">${esc(it.display || it.name)}</span>
        <span class="img-lora-excluded-reason">${esc(it.compatible_reason || "兼容性检测未通过")}</span>
      </div>`).join("");
  }

  // ------------------------------------------------------------ 选中回显

  /**
   * 应用从 /api/video-config 读回来的选中项。
   *
   * 【为什么不在 applyConfig 里直接做】
   * applyConfig() 由 VIDEO_PANEL.load() 调用，那时**清单还没到**
   * （清单是另一个接口）。如果那时就把选中项塞进 state，
   * 随后 loadEffectLoras() 拿到清单后做的「按当前底模剔除」，
   * 就无法区分「刚恢复的」和「本来就没有的」。
   *
   * 所以拆成两步：
   *   1. applyConfig 阶段 -> 存到 EFFECT_STORE.pendingRestore（不落地）
   *   2. 清单到达后 -> restoreSelection() 真正落地 + 剔除不兼容项
   * 这样时序确定，不依赖跨模块的 load 顺序。
   *
   * @param {Array} items 从配置里读到的 [{name, strength, enabled, trigger}]
   */
  function stageRestore(items) {
    EFFECT_STORE.pendingRestore = Array.isArray(items) ? items : null;
    // 清单已经在了 -> 立刻应用（后到的场景）
    if (EFFECT_STORE.available.length || EFFECT_STORE.model) {
      restoreSelection();
    }
  }

  /** 把 pendingRestore 真正落到 state.effectLoras，并剔除不兼容项。 */
  function restoreSelection() {
    const pending = EFFECT_STORE.pendingRestore;
    if (pending === null) {
      // 没有待恢复的内容（首次使用 / 配置里没有这个字段）
      EFFECT_STORE.restored = true;
      return;
    }
    const st = window.VIDEO_PANEL && window.VIDEO_PANEL.state;
    if (st) {
      const valid = new Set(EFFECT_STORE.available.map(x => x.name));
      st.effectLoras = pending
        .filter(it => it && it.name && valid.has(it.name))
        .map(it => ({
          name: String(it.name),
          strength: Number(it.strength) || 1.0,
          enabled: it.enabled !== false,
          trigger: it.trigger || "",
        }));
    }
    EFFECT_STORE.pendingRestore = null;
    EFFECT_STORE.restored = true;
    renderEffectLoraList();
  }

  // ------------------------------------------------------------ 持久化

  // 【2026-11-14 缺陷 B —— 落盘延迟修复（前端侧）】
  //
  // 【原症状】改完效果 LoRA 强度后，轮询 GET /api/video-config 要等
  // **18–25 秒**才看到新值；这段时间里用户刷新页面会看到**旧值**，
  // 表现为「回显没生效」。
  //
  // 【根因有两层，本文件修的是前端这一层】
  //   1. 服务端层：/api/jobs 同步阻塞事件循环（已在本轮另一处修复）
  //      -> PUT /api/video-config 排队 -> 秒级延迟。
  //   2. 前端层（本文件）：改动只是**排了一个 250ms 定时器**，
  //      若用户在这 250ms 内切走/刷新页面，变更**根本没发出去**。
  //      另外 VIDEO_PANEL.save() 内部还有一层 300ms 节流。
  //
  // 【本文件的做法】
  //   a) 保留短节流（滑杆连续拖动时不至于发几十个请求）；
  //   b) 记录「有未落盘变更」的状态；
  //   c) 在 pagehide / beforeunload / visibilitychange(hidden) 时
  //      **强制 flush** —— 用 fetch(keepalive:true) 直发 PUT，
  //      绕过 VIDEO_PANEL.save 的节流，保证页面关闭前变更已发出。

  let saveTimer = null;
  //: 是否有「已改但还没落盘」的变更（用于 flush 判断）。
  let pendingSave = false;

  function effectLoraPayload() {
    return selectedList().map(it => ({
      name: it.name,
      strength: Number(it.strength),
      enabled: it.enabled !== false,
      trigger: it.trigger || "",
    }));
  }

  function saveEffectLoraConfig() {
    renderEffectLoraList();
    const st = window.VIDEO_PANEL && window.VIDEO_PANEL.state;
    if (!st) return;
    pendingSave = true;
    // 存进视频面板自己的配置（局域网共用，与加速 LoRA 一致）
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => { flushEffectLoraConfig(); }, 120);
  }

  /**
   * 立刻把当前选中态落盘（不等节流）。
   *
   * @param {boolean} keepalive true = 页面正在卸载，用 keepalive 让
   *                        请求在页面关闭后仍能送达。
   */
  async function flushEffectLoraConfig(keepalive) {
    clearTimeout(saveTimer);
    saveTimer = null;
    const st = window.VIDEO_PANEL && window.VIDEO_PANEL.state;
    if (!st) return;
    try {
      const cfg = (typeof window.VIDEO_PANEL.collectConfig === "function")
        ? window.VIDEO_PANEL.collectConfig() : {};
      cfg.effect_loras = effectLoraPayload();
      await window.VIDEO_PANEL.save(cfg, { immediate: true, keepalive: !!keepalive });
      pendingSave = false;
    } catch (e) {
      /* 存不上不影响本次生成（提交时会带上） */
    }
  }

  // ---- 页面卸载/隐藏时的兜底 flush ------------------------------
  //
  // 【为什么必须有】用户改完强度**立刻刷新**是最高频的操作，
  // 而刷新 = 页面卸载 = 未落盘的变更可能丢失。
  // visibilitychange(hidden) 比 beforeunload 更可靠（移动端、切标签页
  // 都会触发，且不阻塞卸载），所以两个都挂上。
  function flushOnLeave() {
    if (!pendingSave) return;
    pendingSave = false;   // 防重复触发
    flushEffectLoraConfig(true);
  }
  window.addEventListener("pagehide", flushOnLeave);
  window.addEventListener("beforeunload", flushOnLeave);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flushOnLeave();
  });

  // ------------------------------------------------------------ 加载

  async function loadEffectLoras(modelKey) {
    const url = "/api/video-effect-loras"
      + (modelKey ? "?model=" + encodeURIComponent(modelKey) : "");
    try {
      const resp = await fetch(url);
      if (!resp.ok) return;
      const data = await resp.json();
      EFFECT_STORE.available = data.available || [];
      EFFECT_STORE.incompatible = data.incompatible || [];
      EFFECT_STORE.model = data.model || "";
      EFFECT_STORE.loaderNode = data.loader_node || "";
      EFFECT_STORE.loaderNote = data.loader_note || "";
      EFFECT_STORE.sourceNote = data.source_note || "";
      // 切了底模后，已选里不再兼容的要**剔除**（不是留着假装能用）。
      //
      // ⚠️ 【这里不能无条件剔除 —— 会误删刚恢复出来的选中项】
      // 选中回显的时序是：
      //   VIDEO_PANEL.load()  ->  applyConfig() 从 /api/video-config 恢复选中项
      //   ->  本函数 loadEffectLoras() 拿到清单 -> 剔除不兼容项
      // 如果本函数**先**跑（清单先到、恢复后到），剔除会把还没恢复的
      // 选中项当成「不存在的」删掉 —— 表现为「刷新后选中项随机丢失」。
      //
      // 所以只有当**已经恢复过**（appliedRestore 标记）时才剔除。
      // 该标记由 restoreSelection() 设置 —— 它在清单到达后**主动**执行恢复。
      const st = window.VIDEO_PANEL && window.VIDEO_PANEL.state;
      if (st && Array.isArray(st.effectLoras) && EFFECT_STORE.restored) {
        const valid = new Set(EFFECT_STORE.available.map(x => x.name));
        st.effectLoras = st.effectLoras.filter(it => valid.has(it.name));
      }
      // 清单到了 -> 把暂存的选中项真正落地（若 applyConfig 已经暂存过）。
      // 这一步顺带剔除与当前底模不兼容的项。
      if (EFFECT_STORE.pendingRestore !== null) {
        restoreSelection();
      } else {
        renderEffectLoraList();
      }
      renderExcludedEffectLoras();
      const hint = document.getElementById("vidEffectLoraHint");
      if (hint) {
        hint.textContent = EFFECT_STORE.available.length
          ? `可多选 · ${EFFECT_STORE.available.length} 个可用 · 与加速 LoRA 并存`
          : "本机无可用效果 LoRA";
      }
    } catch (e) {
      /* 网络失败时保持空清单（界面会显示「未检测到」） */
    }
  }

  // ------------------------------------------------------------ 事件绑定

  function bind() {
    const search = document.getElementById("vidEffectLoraSearch");
    if (search) {
      // 搜索框现在只是「下拉的过滤器」：输入即刷新下拉内容。
      search.addEventListener("input", () => {
        renderPickerMenu();
      });
      search.addEventListener("keydown", e => {
        if (e.key === "Escape") { search.value = ""; renderPickerMenu(); search.blur(); }
      });
    }
    // 「清空全部」
    const clearBtn = document.getElementById("vidEffectLoraClear");
    if (clearBtn) {
      clearBtn.addEventListener("click", () => { clearAll(); });
    }
    // 「添加」按钮：把下拉里选中的一项加进已选列表（与文生图同一套交互）
    const addBtn = document.getElementById("vidEffectLoraAdd");
    const sel = document.getElementById("vidEffectLoraSelect");
    if (addBtn && sel) {
      addBtn.addEventListener("click", () => {
        const name = sel.value;
        if (!name) return;
        const meta = metaOf(name);
        const rec = recommendedStrength(meta);
        selectedList().push({
          name,
          strength: rec,
          enabled: true,
          trigger: meta && meta.trigger ? meta.trigger : "",
        });
        renderEffectLoraList();
        renderPickerMenu();
        saveEffectLoraConfig();
      });
    }
  }

  // ------------------------------------------------------------ 导出

  window.VIDEO_EFFECT_LORA = {
    store: EFFECT_STORE,
    load: loadEffectLoras,
    bind,
    render: renderEffectLoraList,
    selected: selectedList,
    clearAll,
    stageRestore,
    restoreSelection,
    _debug: { filterCandidates, recommendedStrength, renderClearButton },
  };
})();
