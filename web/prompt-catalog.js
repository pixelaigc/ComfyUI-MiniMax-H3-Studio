/* ==================================================================
 * 「创意内容设计」面板 —— 文生图提示词构造器
 *
 * 【功能】
 *   1. 在共用参数面板 #imagePanel 里，给**全部 4 个文生图 mode**
 *      （t2i / t2i_qwen / t2i_moss / t2i_h3image）提供一个统一入口；
 *   2. 打开后按维度分组（可折叠 + 搜索过滤），每个子类一个下拉，
 *      默认「不指定」；人物主体按从头到脚逐级选，每一级都可跳过；
 *      前景 / 中景 / 远景是三个独立子类，可以同时选；
 *   3. 已选内容实时形成「原始提示词」预览；
 *   4. 组装结果按固定骨架顺序写回 #prompt。
 *
 * 【为什么不做成「增强方式」】
 * 增强方式注册表 ENHANCE_METHODS（web/enhance-menu.js）是**后端增强**
 * 的清单，加一项会牵动 tests/test_qwen_pe_enhance.py 的「下拉项 == 注册表项」
 * 断言。这里的定位是「提示词构造器」：拼出一段**原始提示词**，
 * 再交给用户既有的「增强 / 二次增强」链路去适配模型 ——
 * 模型适配能力留给既有增强链路，不在这里另造一套。
 *
 * 【为什么不改 web/app.js 的渲染主流程】
 * app.js 已 400KB+，并行任务也在动它。这个模块是独立文件，
 * 只在 #imagePanel 里挂一个宿主块，其余逻辑全在本文件内。
 * app.js 只被调用一次（导出两个写回用的版本函数）。
 *
 * 【写回为什么是「内联选择」而不是弹窗】
 * 移动端硬约束：任何状态下都要能访问生成媒体库与左侧收藏栏。
 * 弹层（position:fixed; inset:0）会盖住顶栏（汉堡按钮所在），
 * 于是「进面板后回不去」的老问题会重新出现。
 * 所以「覆盖 / 追加」的选择直接做在预览区里 —— 零覆盖层、零 z-index 风险。
 *
 * 【纪律】
 *   * 「不指定」= 真的不输出字段。绝不写 `no hair` / `not wearing shoes`
 *     这类否定式 —— 模型极可能画出字面意思。
 *   * 拼装结果**不含** (x:1.5) 之类权重语法。MiniMax H3 是否支持权重
 *     语法尚未取证，模型适配交给既有增强链路。
 *   * 写回 #prompt 时若输入框已有内容，必须给出「覆盖 / 追加」选择，
 *     绝不静默丢弃用户文本。
 *   * 硬互斥组合**阻止**（禁用 + 说明原因），软冲突**黄字提示**。
 *     两者都不静默合并。
 * ================================================================== */
(function () {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  // ------------------------------------------------------------------ 状态

  let CATALOG = null;          // 目录（来自 GET /api/prompt-catalog）
  let LOAD_ERROR = "";         // 加载失败原因（要如实显示）
  const picks = new Map();     // group_id -> item_id
  const customs = new Map();   // group_id -> 用户自行输入的文本（与词库选项一起参与组装）
  const collapsed = new Map(); // group_id -> bool（true = 折叠）
  let keyword = "";            // 搜索关键词
  let opened = false;          // 抽屉是否展开
  let pendingWrite = null;     // 待写回的文本（等待用户选覆盖/追加）

  const STORAGE_KEY = "h3studio.prompt-catalog-collapsed";

  // ------------------------------------------------------------------ 工具

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function loadCollapsed() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const obj = JSON.parse(raw);
      Object.keys(obj || {}).forEach(k => collapsed.set(k, !!obj[k]));
    } catch (e) { /* 折叠状态读不出来就当没折叠，不阻断 */ }
  }

  function saveCollapsed() {
    try {
      const obj = {};
      collapsed.forEach((v, k) => { obj[k] = v; });
      localStorage.setItem(STORAGE_KEY, JSON.stringify(obj));
    } catch (e) { /* 存不进去只是丢折叠状态，不阻断 */ }
  }

  /**
   * 把选项的英文片段整理成可以进提示词的一段文本。
   *
   * 【片段由服务端算好】目录里每条选项都带 `fragment` 字段 ——
   * 服务端已经处理过「a / b 并列取第一个」「淘汰项不输出」
   * 「D8.4 取 example 而不是类别名」这些规则。
   * 前端不再重复实现一遍（两份实现必然漂移）。
   */
  function fragmentOf(item) {
    if (!item || item.is_skip) return "";
    // 【中文优先】默认写进提示词的是中文片段（服务端算好的 zh_fragment）。
    // 英文片段只在「含英文片段版本」/选项详情里作为辅助信息出现。
    const frag = String(item.zh_fragment == null ? "" : item.zh_fragment).trim();
    return frag;
  }

  /** 英文辅助版本（不参与默认组装，仅供「含英文片段版本」复制）。 */
  function enFragmentOf(item) {
    if (!item || item.is_skip) return "";
    const frag = String(item.en_fragment == null ? item.fragment == null ? "" : item.fragment
                        : item.en_fragment).trim();
    return frag;
  }

  // ------------------------------------------------------------------ 索引

  let ITEM_BY_ID = new Map();
  let GROUP_BY_ID = new Map();

  function reindex() {
    ITEM_BY_ID = new Map();
    GROUP_BY_ID = new Map();
    if (!CATALOG) return;
    (CATALOG.dimensions || []).forEach(dim => {
      (dim.groups || []).forEach(g => {
        GROUP_BY_ID.set(g.id, g);
        (g.items || []).forEach(it => ITEM_BY_ID.set(it.id, it));
      });
    });
  }

  function labelOf(id) {
    const it = ITEM_BY_ID.get(id);
    return it ? it.label : id;
  }

  // ------------------------------------------------------------------ 冲突

  /**
   * 当前已选集合触发的冲突。
   *
   * hard —— 两侧都选了：物理上不可能同时成立，必须取消一侧。
   * soft —— 两侧都选了：可以共存，但会互相削弱。
   * cap  —— 同一范围内选太多：数量上限（limit==1 的按硬互斥处理）。
   */
  function detectConflicts() {
    const selected = selectedIds();
    const hard = [];
    const soft = [];
    const cap = [];
    (CATALOG.conflicts || []).forEach(rule => {
      const aHit = (rule.a || []).filter(id => selected.has(id));
      const bHit = (rule.b || []).filter(id => selected.has(id));
      if (rule.kind === "cap") {
        if (aHit.length > (rule.limit || 1)) {
          cap.push({
            rule,
            count: aHit.length,
            limit: rule.limit,
            names: aHit.map(labelOf),
          });
        }
        return;
      }
      if (aHit.length && bHit.length) {
        const entry = {
          rule,
          aIds: aHit, bIds: bHit,
          aNames: aHit.map(labelOf),
          bNames: bHit.map(labelOf),
        };
        if (rule.kind === "hard") hard.push(entry);
        else soft.push(entry);
      }
    });
    return {hard, soft, cap};
  }

  function selectedIds() {
    const set = new Set();
    picks.forEach(itemId => {
      if (!itemId) return;
      const item = ITEM_BY_ID.get(itemId);
      if (!item || item.is_skip) return;
      set.add(itemId);
    });
    return set;
  }

  /**
   * 某个候选选项是否被硬互斥禁用。
   * 返回 null = 可选；返回对象 = 被禁用 + 原因 + 冲突的另一侧。
   */
  function blockedReason(candidateId) {
    const candidate = ITEM_BY_ID.get(candidateId);
    if (!candidate || candidate.is_skip) return null;
    const selected = selectedIds();
    for (const rule of CATALOG.conflicts || []) {
      if (rule.kind === "hard") {
        const inA = (rule.a || []).includes(candidateId);
        const inB = (rule.b || []).includes(candidateId);
        if (!inA && !inB) continue;
        const others = inA ? (rule.b || []) : (rule.a || []);
        const hit = others.filter(id => selected.has(id));
        if (!hit.length) continue;
        return {rule, otherNames: hit.map(labelOf)};
      }
      if (rule.kind === "cap" && (rule.limit || 1) <= 1) {
        // 数量上限为 1 的（如「构图三族互斥」）= 硬约束
        if (!(rule.a || []).includes(candidateId)) continue;
        const already = (rule.a || []).filter(id => selected.has(id));
        if (already.length < (rule.limit || 1)) continue;
        return {rule, otherNames: already.map(labelOf)};
      }
    }
    return null;
  }

  // ------------------------------------------------------------------ 组装

  /**
   * 按骨架顺序拼装原始提示词。
   *
   * 顺序（任务书给的 5–6 段式骨架 + 调研 A 报告结论 7）：
   *   主体 → 造型 → 风格 → 场景 → 构图 → 光 → 色 → 氛围 → 质量 → 负向
   *
   * 只输出**真实片段**：跳过项不输出；没选就是空串（不产生垃圾片段）；
   * 不含 (x:1.5) 之类权重语法。
   *
   * 【用户自行输入的内容一起参与组装】（任务书 2026-11-21 新增约束）
   * 每个维度都有自由文本入口；自定义项按**它所属子类的骨架槽位**插入，
   * 排在同槽位的词库选项之后（用户自己写的东西不覆盖词库选项，只追加）。
   */
  function assemble(opts) {
    const useEn = !!(opts && opts.english);
    if (!CATALOG) return {text: "", parts: [], bySlot: {}, chosen: []};
    const slotIndex = new Map();
    (CATALOG.slots || []).forEach((s, i) => slotIndex.set(s.key, i));

    const chosen = [];
    picks.forEach((itemId, groupId) => {
      if (!itemId) return;
      const item = ITEM_BY_ID.get(itemId);
      if (!item || item.is_skip) return;
      const group = GROUP_BY_ID.get(groupId);
      if (!group || group.render === "rules") return;
      const frag = useEn ? enFragmentOf(item) : fragmentOf(item);
      if (!frag) return;          // 片段为空的选项（淘汰项等）不输出
      chosen.push({
        groupId, item, group, frag, custom: false,
        slot: group.slot || "style",
        slotIdx: slotIndex.has(group.slot) ? slotIndex.get(group.slot) : 99,
        order: group.order == null ? 0 : group.order,
      });
    });

    // 自定义内容：与词库选项一起进同槽位，排在词库选项之后（order+1）
    customs.forEach((text, groupId) => {
      const trimmed = String(text || "").trim();
      if (!trimmed) return;
      const group = GROUP_BY_ID.get(groupId);
      if (!group || group.render === "rules") return;
      chosen.push({
        groupId, item: null, group, frag: trimmed, custom: true,
        slot: group.slot || "style",
        slotIdx: slotIndex.has(group.slot) ? slotIndex.get(group.slot) : 99,
        order: (group.order == null ? 0 : group.order) + 1,
      });
    });

    // 排序：先按骨架槽位，再按组内顺序（构图 → 镜头），最后按组 id 稳定排序
    chosen.sort((x, y) => {
      if (x.slotIdx !== y.slotIdx) return x.slotIdx - y.slotIdx;
      if (x.order !== y.order) return x.order - y.order;
      return x.groupId < y.groupId ? -1 : (x.groupId > y.groupId ? 1 : 0);
    });

    const parts = [];
    const bySlot = {};
    chosen.forEach(c => {
      parts.push(c.frag);
      (bySlot[c.slot] = bySlot[c.slot] || []).push(c.frag);
    });

    return {text: parts.join(", "), parts, bySlot, chosen};
  }

  // ------------------------------------------------------------------ 渲染

  function panelEl() { return $("#promptCatalogPanel"); }

  function render() {
    // 兜底：宿主可能被别处的 DOM 重写弄丢 —— 先保证它还在。
    if (!panelEl()) {
      if (!ensureDom()) return;
    }
    const host = panelEl();
    if (!host) return;
    if (LOAD_ERROR) {
      host.innerHTML = `
        <div class="field pc-field">
          <div class="field-label">创意内容设计 <span class="muted">目录加载失败</span></div>
          <div class="pc-error">${esc(LOAD_ERROR)}</div>
          <button type="button" class="pc-btn" id="pcRetry">重新加载</button>
        </div>`;
      const retry = $("#pcRetry");
      if (retry) retry.addEventListener("click", () => load(true));
      return;
    }
    if (!CATALOG) {
      host.innerHTML = `
        <div class="field pc-field">
          <div class="field-label">创意内容设计 <span class="muted">加载中</span></div>
          <div class="pc-note">正在读取选项目录…</div>
        </div>`;
      return;
    }

    const counts = CATALOG.counts || {};
    const conflicts = detectConflicts();
    const assembled = assemble();
    const canWrite = assembled.parts.length > 0 && conflicts.hard.length === 0;

    host.innerHTML = `
      <div class="field pc-field" id="promptCatalogField">
        <div class="field-label">
          <span>创意内容设计</span>
          <span class="muted">${esc(counts.items || 0)} 个可选条目 · ${esc(counts.groups || 0)} 个分组</span>
        </div>
        <div class="pc-note">
          用下拉列表描述画面内容，组合成一段中文为主的原始提示词；每一项都可跳过，也可自行输入；写回后仍可照常用「增强」「二次增强」。
        </div>
        <button type="button" class="pc-open-btn" id="pcOpenBtn" aria-expanded="${opened ? "true" : "false"}">
          ${opened ? "收起设计面板" : "打开设计面板"}
        </button>
      </div>
      <div class="pc-drawer" id="promptCatalogDrawer"${opened ? "" : " hidden"}>
        <div class="pc-search-row">
          <input type="search" id="pcSearch" class="pc-search" placeholder="搜索选项（中文名或英文片段）" value="${esc(keyword)}">
          <button type="button" class="pc-clear" id="pcClearAll">清空</button>
        </div>
        <div class="pc-conflicts" id="pcConflicts"${conflicts.hard.length || conflicts.soft.length || conflicts.cap.length ? "" : " hidden"}></div>
        <div class="pc-groups" id="pcGroups"></div>
        <div class="pc-preview">
          <div class="pc-preview-head">
            <span>原始提示词预览</span>
            <span class="muted" id="pcPreviewCount">${assembled.parts.length} 段</span>
            <button type="button" class="pc-btn ghost pc-copy-en" id="pcCopyEn"${assembled.parts.length ? "" : " disabled"}>复制含英文片段版本</button>
          </div>
          <div class="pc-preview-text" id="pcPreviewText">${assembled.text
            ? esc(assembled.text)
            : "还没有选择任何内容 —— 选中的条目会按「主体 → 造型 → 风格 → 场景 → 构图 → 光 → 色 → 氛围」顺序出现在这里。"}</div>
          <div class="pc-write-row" id="pcWriteRow"></div>
        </div>
      </div>`;

    const openBtn = $("#pcOpenBtn");
    if (openBtn) {
      openBtn.addEventListener("click", () => {
        opened = !opened;
        render();
      });
    }
    const clearBtn = $("#pcClearAll");
    if (clearBtn) {
      clearBtn.addEventListener("click", () => {
        picks.clear();
        customs.clear();      // 「清空」也要清掉自行输入的内容
        pendingWrite = null;
        render();
      });
    }
    // 【英文片段只作为辅助信息保留】默认预览是中文；这里给一份
    // 「含英文片段版本」的复制入口，不改变写回 #prompt 的内容。
    const copyEn = $("#pcCopyEn");
    if (copyEn) {
      copyEn.addEventListener("click", async () => {
        const en = assemble({english: true}).text;
        if (!en) return;
        copyEn.textContent = "已复制英文版本";
        try {
          await navigator.clipboard.writeText(en);
        } catch (e) {
          // 剪贴板不可用（非安全上下文 / 旧浏览器）→ 退回可见文本，
          // 用户仍可手动选中复制，不静默失败
          const box = $("#pcPreviewText");
          if (box) box.textContent = en;
        }
        setTimeout(() => {
          const b = $("#pcCopyEn");
          if (b) b.textContent = "复制含英文片段版本";
        }, 2200);
      });
    }
    const search = $("#pcSearch");
    if (search) {
      search.addEventListener("input", e => {
        keyword = e.target.value || "";
        renderGroups();
        const again = $("#pcSearch");
        if (again) {
          again.focus();
          try { again.setSelectionRange(keyword.length, keyword.length); } catch (err) { /* 某些浏览器不支持 */ }
        }
      });
    }

    renderConflicts(conflicts);
    renderWriteRow(assembled, conflicts, canWrite);
    renderGroups();
  }

  function renderConflicts(conflicts) {
    const box = $("#pcConflicts");
    if (!box) return;
    const parts = [];
    conflicts.hard.forEach(c => {
      parts.push(`
        <div class="pc-conflict hard">
          <b>冲突（已阻止）</b>：${esc(c.rule.label)}
          <div class="pc-conflict-why">${esc(c.rule.reason)}。当前同时选择了
            「${esc(c.aNames.join("、"))}」与「${esc(c.bNames.join("、"))}」，请取消其中一个。</div>
        </div>`);
    });
    conflicts.soft.forEach(c => {
      parts.push(`
        <div class="pc-conflict soft">
          <b>提示</b>：${esc(c.rule.label)}
          <div class="pc-conflict-why">${esc(c.rule.reason)}。当前同时选择了
            「${esc(c.aNames.join("、"))}」与「${esc(c.bNames.join("、"))}」，
            可以共存但会互相削弱。</div>
        </div>`);
    });
    conflicts.cap.forEach(c => {
      parts.push(`
        <div class="pc-conflict cap">
          <b>超出上限</b>：${esc(c.rule.label)}
          <div class="pc-conflict-why">${esc(c.rule.reason)}。当前选了 ${c.count} 项
            （${esc(c.names.join("、"))}），上限 ${c.limit} 项。</div>
        </div>`);
    });
    box.innerHTML = parts.join("");
    box.hidden = !parts.length;
  }

  function renderWriteRow(assembled, conflicts, canWrite) {
    const row = $("#pcWriteRow");
    if (!row) return;
    const hint = $("#pcWriteHint");

    if (pendingWrite) {
      // 【覆盖 / 追加】二选一 —— 绝不静默丢弃用户已有文本
      row.innerHTML = `
        <div class="pc-write-choice">
          <div class="pc-write-choice-note">提示词框里已经有内容，请选择怎么写入：</div>
          <div class="pc-write-current">当前内容：${esc(pendingWrite.current)}</div>
          <div class="pc-write-btns">
            <button type="button" class="pc-btn primary" id="pcOverwrite">覆盖现有内容</button>
            <button type="button" class="pc-btn" id="pcAppend">追加到现有内容后面</button>
            <button type="button" class="pc-btn ghost" id="pcCancelWrite">取消</button>
          </div>
        </div>`;
      const ow = $("#pcOverwrite");
      const ap = $("#pcAppend");
      const cx = $("#pcCancelWrite");
      if (ow) ow.addEventListener("click", () => applyWrite(pendingWrite.text));
      if (ap) ap.addEventListener("click", () => applyWrite(`${pendingWrite.current}, ${pendingWrite.text}`));
      if (cx) cx.addEventListener("click", () => { pendingWrite = null; render(); });
      return;
    }

    const disabled = canWrite ? "" : " disabled";
    const reason = !assembled.parts.length
      ? "先选几项再写入"
      : (conflicts.hard.length ? "先解决冲突" : "");
    row.innerHTML = `
      <button type="button" class="pc-btn primary" id="pcWriteBtn"${disabled}>写入提示词框</button>
      <span class="muted" id="pcWriteHint">${esc(reason || (hint && hint.textContent) || "写回后可继续走增强链路")}</span>`;
    const btn = $("#pcWriteBtn");
    if (btn) btn.addEventListener("click", () => writeBack());
  }

  // ------------------------------------------------------------------ 分组

  function groupMatches(group) {
    if (!keyword.trim()) return true;
    const kw = keyword.trim().toLowerCase();
    if (group.name.toLowerCase().includes(kw)) return true;
    return (group.items || []).some(it =>
      String(it.label || "").toLowerCase().includes(kw) ||
      String(it.en || "").toLowerCase().includes(kw));
  }

  function itemMatches(item) {
    if (!keyword.trim()) return true;
    const kw = keyword.trim().toLowerCase();
    return String(item.label || "").toLowerCase().includes(kw) ||
      String(item.en || "").toLowerCase().includes(kw);
  }

  function renderGroups() {
    const host = $("#pcGroups");
    if (!host || !CATALOG) return;
    const html = [];
    (CATALOG.dimensions || []).forEach(dim => {
      (dim.groups || []).forEach(group => {
        if (group.render === "rules") return;      // D9 规则维度不进下拉
        if (!groupMatches(group)) return;
        const visibleItems = (group.items || []).filter(itemMatches);
        if (!visibleItems.length && keyword.trim()) return;
        const current = picks.get(group.id) || "";
        // 搜索时强制展开命中分组，否则搜到了却看不到
        const isCollapsed = keyword.trim() ? false : (collapsed.get(group.id) === true);
        html.push(renderGroup(group, dim, current, isCollapsed, visibleItems));
      });
    });
    host.innerHTML = html.join("") || `<div class="pc-note">没有匹配「${esc(keyword)}」的选项。</div>`;
    bindGroupEvents();
  }

  function renderGroup(group, dim, current, isCollapsed, visibleItems) {
    const selected = current ? ITEM_BY_ID.get(current) : null;
    const effective = selected && !selected.is_skip ? selected : null;
    const options = visibleItems.map(item => {
      const blocked = blockedReason(item.id);
      const disabled = blocked ? " disabled" : "";
      const label = item.label + (item.needs_test ? "（需实测）" : "");
      const title = item.example
        ? `${item.example}${blocked ? `｜与「${blocked.otherNames.join("、")}」冲突：${blocked.rule.reason}` : ""}`
        : "";
      const deprecated = item.status === "deprecated" ? "（不建议使用）" : "";
      const sel = item.id === current ? " selected" : "";
      return `<option value="${esc(item.id)}"${disabled}${sel} title="${esc(title)}">${esc(label)}${deprecated}</option>`;
    }).join("");

    const selectedHint = effective
      ? `<div class="pc-selected">已选：${esc(effective.label)}${effective.en ? ` · <code>${esc(effective.en)}</code>` : ""}</div>`
      : `<div class="pc-selected muted">未指定（这一项不会写进提示词）</div>`;

    // 【每个维度都允许自行输入】（任务书 2026-11-21 新增约束）
    // 自定义内容不被 UI 或校验拦掉：不做长度/词汇校验，也不与词库选项互斥。
    const customText = customs.get(group.id) || "";
    const customRow = group.allow_custom === false ? "" : `
          <div class="pc-custom-row">
            <input type="text" class="pc-custom" data-custom="${esc(group.id)}"
                   aria-label="${esc(group.name)} 自行输入"
                   placeholder="${esc(group.custom_placeholder || "自行输入这一项的内容（中文即可）")}"
                   value="${esc(customText)}">
            <button type="button" class="pc-clear pc-custom-clear" data-custom-clear="${esc(group.id)}"${customText ? "" : " hidden"}>清除</button>
          </div>
          <div class="pc-note pc-custom-note">自行输入的内容与上面的选项一起写进提示词；留空即跳过这一项。</div>`;

    // 【硬互斥的可见说明】只把选项 disabled 是不够的 ——
    // 原生 <select> 的 title 在移动端**根本不显示**（长按才有 tooltip，
    // 而且触屏上多数浏览器直接不弹）。用户只会看到「这个选项点不动」，
    // 不知道为什么被拦。
    // 所以：凡是「因为与当前已选项硬互斥而被禁用」的分组，
    // 都要在分组里挂一条**可见的中文说明**（不是 tooltip）。
    const blockedHere = [];
    (group.items || []).forEach(it => {
      if (it.is_skip) return;
      const b = blockedReason(it.id);
      if (b) blockedHere.push({item: it, blocked: b});
    });
    const blockedNote = blockedHere.length ? `
          <div class="pc-conflict pc-conflict-inline hard">
            <b>这一组有 ${blockedHere.length} 项与当前选择冲突，已禁用</b>
            ${blockedHere.map(x => `
              <div class="pc-conflict-why">「${esc(x.item.label)}」与
                「${esc(x.blocked.otherNames.join("、"))}」冲突：${esc(x.blocked.rule.reason)}。
                请先取消「${esc(x.blocked.otherNames.join("、"))}」再选它。</div>`).join("")}
          </div>` : "";

    const guideNote = group.render === "guide"
      ? `<div class="pc-note pc-guide">这一组是**写法对照清单**（保留 / 谨慎 / 淘汰），不是可选项；想自己写画质词请直接用在下面的自行输入里。</div>
          <div class="pc-guide-list">${(group.items || []).map(it => `
            <div class="pc-guide-item${it.status === "deprecated" ? " deprecated" : ""}">
              <b>${esc(it.label)}</b>
              <span class="muted">${esc(it.example || "")}</span>
            </div>`).join("")}</div>`
      : "";

    return `
      <div class="pc-group${isCollapsed ? " collapsed" : ""}" data-group="${esc(group.id)}">
        <button type="button" class="pc-group-head" data-toggle="${esc(group.id)}" aria-expanded="${isCollapsed ? "false" : "true"}">
          <span class="pc-group-name">${esc(group.name)}</span>
          <span class="muted">${esc(dim.name)} · 已选 ${effective ? 1 : 0}${customText ? " + 自行输入" : ""}</span>
          <span class="pc-caret" aria-hidden="true">${isCollapsed ? "▸" : "▾"}</span>
        </button>
        <div class="pc-group-body"${isCollapsed ? " hidden" : ""}>
          ${guideNote}
          ${group.render === "guide" ? "" : `<select class="img-select pc-select" data-select="${esc(group.id)}" aria-label="${esc(group.name)}">${options}</select>`}
          ${group.render === "guide" ? "" : selectedHint}${customRow}
          ${blockedNote}
          ${group.purpose ? `<div class="pc-note pc-purpose">${esc(group.purpose)}</div>` : ""}
        </div>
      </div>`;
  }

  function bindGroupEvents() {
    $$("#pcGroups [data-toggle]").forEach(btn => {
      btn.addEventListener("click", () => {
        const gid = btn.dataset.toggle;
        const next = !(collapsed.get(gid) === true);
        collapsed.set(gid, next);
        saveCollapsed();
        renderGroups();
      });
    });
    $$("#pcGroups select[data-select]").forEach(sel => {
      sel.addEventListener("change", () => {
        const gid = sel.dataset.select;
        const itemId = sel.value || "";
        if (!itemId) {
          picks.delete(gid);
          render();
          return;
        }
        const item = ITEM_BY_ID.get(itemId);
        if (!item) return;
        if (item.is_skip) {
          // 「不指定」= 真的不输出，不写否定式
          picks.delete(gid);
          render();
          return;
        }
        const blocked = blockedReason(itemId);
        if (blocked) {
          sel.value = picks.get(gid) || "";
          showConflictNotice(blocked, item);
          return;
        }
        picks.set(gid, itemId);
        render();
      });
    });
    // 【自行输入】每个维度的自由文本入口。
    // 输入即生效（不校验、不拦截），清空 = 跳过这一项。
    $$("#pcGroups [data-custom]").forEach(inp => {
      inp.addEventListener("input", () => {
        const gid = inp.dataset.custom;
        const text = String(inp.value || "").trim();
        if (text) customs.set(gid, inp.value);
        else customs.delete(gid);
        // 只重算预览/冲突区，不重绘分组 —— 重绘会让输入框失去焦点，
        // 中文输入法（IME）组字过程中被打断会直接打不了字。
        refreshPreviewAndConflicts();
        updateGroupHeadCount(gid);
      });
    });
    $$("#pcGroups [data-custom-clear]").forEach(btn => {
      btn.addEventListener("click", () => {
        const gid = btn.dataset.customClear;
        customs.delete(gid);
        render();
      });
    });
  }

  /** 只更新预览 + 冲突区 + 该组标题的计数（不重绘分组，保住输入焦点）。 */
  function refreshPreviewAndConflicts() {
    const conflicts = detectConflicts();
    const assembled = assemble();
    renderConflicts(conflicts);
    renderWriteRow(assembled, conflicts,
                   assembled.parts.length > 0 && conflicts.hard.length === 0);
    const previewText = $("#pcPreviewText");
    if (previewText) {
      previewText.textContent = assembled.text
        || "还没有选择任何内容 —— 选中的条目会按「主体 → 造型 → 风格 → 场景 → 构图 → 光 → 色 → 氛围」顺序出现在这里。";
    }
    const countEl = $("#pcPreviewCount");
    if (countEl) countEl.textContent = `${assembled.parts.length} 段`;
  }

  function updateGroupHeadCount(groupId) {
    const head = $(`#pcGroups .pc-group[data-group="${groupId}"] .pc-group-head .muted`);
    if (!head) return;
    const group = GROUP_BY_ID.get(groupId);
    const dim = group ? (CATALOG.dimensions.find(d => (d.groups || []).some(g => g.id === groupId)) || {name: ""}) : {name: ""};
    const hasPick = !!picks.get(groupId);
    const hasCustom = !!String(customs.get(groupId) || "").trim();
    head.textContent = `${dim.name} · 已选 ${hasPick ? 1 : 0}${hasCustom ? " + 自行输入" : ""}`;
  }

  function showConflictNotice(blocked, item) {
    const box = $("#pcConflicts");
    if (!box) return;
    const div = document.createElement("div");
    div.className = "pc-conflict hard";
    div.innerHTML = `
      <b>冲突（已阻止）</b>：${esc(blocked.rule.label)}
      <div class="pc-conflict-why">${esc(blocked.rule.reason)}。不能同时选择
        「${esc(blocked.otherNames.join("、"))}」与「${esc(item.label)}」，请先取消其中一个。</div>`;
    box.hidden = false;
    box.appendChild(div);
  }

  // ------------------------------------------------------------------ 写回

  /**
   * 把组装结果写回 #prompt。
   * 若输入框**已有**用户文本 ⇒ 必须给出「覆盖 / 追加」选择，绝不静默丢弃。
   */
  function writeBack() {
    const assembled = assemble();
    if (!assembled.parts.length) return;
    const conflicts = detectConflicts();
    if (conflicts.hard.length) return;      // 有硬冲突先解决
    const prompt = $("#prompt");
    if (!prompt) return;
    const existing = (prompt.value || "").trim();
    if (!existing) {
      applyWrite(assembled.text);
      return;
    }
    pendingWrite = {current: existing, text: assembled.text};
    render();
    const ow = $("#pcOverwrite");
    if (ow && ow.scrollIntoView) {
      try { ow.scrollIntoView({block: "nearest"}); } catch (e) { /* 老浏览器 */ }
    }
  }

  function applyWrite(text) {
    const prompt = $("#prompt");
    if (!prompt) return;
    prompt.value = text;
    prompt.dispatchEvent(new Event("input", {bubbles: true}));
    // 记录成 level 0（原始）—— 不打「已增强」徽标，
    // 否则会绕过 #enhanceGateModal 的「未增强拦截」。
    if (typeof window.__h3PushPromptVersion === "function") {
      window.__h3PushPromptVersion(0, text, "创意内容设计", "");
    }
    if (typeof window.__h3ClearPromptEnhanced === "function") {
      window.__h3ClearPromptEnhanced();
    }
    pendingWrite = null;
    render();
    const hint = $("#pcWriteHint");
    if (hint) hint.textContent = "已写入提示词框，可以直接点「增强」。";
  }

  // ------------------------------------------------------------------ 加载

  async function load(force) {
    if (CATALOG && !force) { render(); return; }
    LOAD_ERROR = "";
    render();
    try {
      const resp = await fetch("/api/prompt-catalog", {cache: "no-store"});
      if (!resp.ok) throw new Error("服务返回 " + resp.status);
      const data = await resp.json();
      if (!data || !Array.isArray(data.dimensions)) {
        throw new Error("目录数据格式不对");
      }
      CATALOG = data;
      reindex();
      LOAD_ERROR = "";
    } catch (e) {
      CATALOG = null;
      LOAD_ERROR = "读取选项目录失败：" + (e && e.message ? e.message : String(e));
    }
    render();
  }

  // ------------------------------------------------------------------ 挂载

  function ensureDom() {
    if (panelEl()) return true;
    const imagePanel = $("#imagePanel");
    if (!imagePanel) return false;
    const host = document.createElement("div");
    host.id = "promptCatalogPanel";
    host.className = "prompt-catalog-panel";
    // 放在 #imagePanel 末尾 —— 图像参数（分辨率/模型/LoRA）之下、
    // 提示词框之上：构造器的产物就是提示词，先看到它再写。
    imagePanel.appendChild(host);
    return true;
  }

  function boot() {
    if (!ensureDom()) return;
    loadCollapsed();
    load(false);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  window.PROMPT_CATALOG = {
    assemble,
    detectConflicts,
    blockedReason,
    fragmentOf,
    enFragmentOf,
    render,
    load,
    get picks() { return picks; },
    get customs() { return customs; },
    get catalog() { return CATALOG; },
    get opened() { return opened; },
  };
})();
