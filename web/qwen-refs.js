/* H3 Studio —— Qwen-Image 2.1「图片编辑」的参考图（素材库来源）
 *
 * 与 app.js 的关系：app.js 通过 window.QwenRefs 调它。
 * 单独成文件的原因：app.js 正被并发任务修改（移动端适配 / LoRA 面板），
 * 参考图这块改动集中在一个文件里，冲突面最小。
 *
 * 三条硬规则（来自需求 + 官方 PE-I2I 语义）：
 *   1. 参考图**只能**来自素材库 —— 没有本地上传通道；
 *   2. 占位标记 <imageN> 的 N 必须与实际送进 ComfyUI 的 image 插槽顺序一致；
 *   3. 移除参考图后重新编号，连续无空洞，提示词里不能留悬空标记。
 *
 * 占位标记用官方的 <imageN> 而不是中文 <参考图N>：
 *   官方 PE-I2I system prompt 明确要求多图必须用 <image1>/<image2>，
 *   且单图（N=1）不要用标签。ComfyUI 的 qwen_image21.py 也用 "<image{n}>"
 *   作为视觉块前缀。
 */
(function () {
  "use strict";

  const $ = s => document.querySelector(s);

  // ---------- 常量 ----------
  const MAX_REFS = 10;          // 模型官方能力 10 张（与后端 ref_images.MAX_REF_IMAGES 一致）
  const PLACEHOLDER_RE = /<image(\d+)>/g;

  // ---------- 状态 ----------
  // refs: [{asset_id, version, name, description, description_source, tags,
  //         created_at, preview_url, missing}]
  let refs = [];
  let sortBusy = false;

  // ---------- 工具 ----------
  function esc(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function api(path, options) {
    return fetch(path, options || {}).then(async resp => {
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        const err = new Error(data.error || `请求失败（HTTP ${resp.status}）`);
        err.payload = data;
        throw err;
      }
      return data;
    });
  }

  function placeholderFor(index) {
    return `<image${index + 1}>`;
  }

  function parsePlaceholders(text) {
    const found = new Set();
    let m;
    const re = new RegExp(PLACEHOLDER_RE.source, "g");
    while ((m = re.exec(text || "")) !== null) found.add(parseInt(m[1], 10));
    return [...found].sort((a, b) => a - b);
  }

  function renumber(text, mapping) {
    // 单遍替换：每个占位按它**原本**的序号查表。
    // 不能用「先替换大的再替换小的」—— 那会连环替换
    // （先把 <image3> 变成 <image2>，再被当成 <image2> 变成 <image1>）。
    if (!mapping || !Object.keys(mapping).length) return text || "";
    return (text || "").replace(/<image(\d+)>/g, (whole, num) => {
      const next = mapping[Number(num)];
      return next === undefined ? whole : `<image${next}>`;
    });
  }

  // ---------- 对外：读写参考图列表 ----------

  function getRefs() {
    return refs.slice();
  }

  function setRefs(list) {
    refs = (list || []).slice(0, MAX_REFS);
    render();
  }

  function clear() {
    refs = [];
    render();
  }

  function count() {
    return refs.length;
  }

  // ---------- 添加：从素材库选择 ----------

  function openLibraryPicker() {
    const max = MAX_REFS - refs.length;
    if (max <= 0) {
      alert(`参考图最多 ${MAX_REFS} 张，已达上限，请先移除一些。`);
      return;
    }
    if (!window.ASSETLIB || !window.ASSETLIB.openPicker) {
      alert("素材库模块未加载，无法选择参考图。请刷新页面重试。");
      return;
    }
    window.ASSETLIB.openPicker({
      categories: ["image"],
      multiple: true,
      limit: max,
      onDone: (ids) => { if (ids && ids.length) addAssets(ids); },
    });
  }

  async function addAssets(ids) {
    const room = MAX_REFS - refs.length;
    const chosen = ids.slice(0, room);
    const overflow = ids.length - chosen.length;
    if (!chosen.length) return;
    let data;
    try {
      data = await api("/api/refs/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refs: chosen.map(id => ({ asset_id: id })) }),
      });
    } catch (e) {
      alert("读取素材信息失败：" + e.message);
      return;
    }
    const existing = new Set(refs.map(r => r.asset_id));
    const added = [];
    (data.refs || []).forEach(item => {
      if (existing.has(item.asset_id)) return;   // 同一素材不重复加
      added.push(item);
    });
    refs = refs.concat(added).slice(0, MAX_REFS);
    render();
    if (overflow > 0) {
      alert(`参考图最多 ${MAX_REFS} 张，超出的 ${overflow} 张未添加。`);
    }
    // 素材库没给描述 -> 提示用户补写（不编造描述）
    const noDesc = added.filter(r => !r.description);
    if (noDesc.length) {
      const detail = $("#imgRefDetail");
      if (detail) detail.open = true;
    }
  }

  // ---------- 移除 ----------

  function removeAt(index) {
    if (index < 0 || index >= refs.length) return;
    refs.splice(index, 1);
    // 剩余参考图重新编号：占位标记整体重排，保持连续无空洞
    reindexPrompt();
    render();
  }

  // ---------- 排序 ----------

  async function smartSort(promptText) {
    if (refs.length < 2) return;
    sortBusy = true;
    render();
    let data;
    try {
      data = await api("/api/refs/sort", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refs, prompt: promptText || "" }),
      });
    } catch (e) {
      // 排序失败**不能挡住用户** —— 降级为确定性基础排序
      sortBusy = false;
      const sorted = refs.slice().sort((a, b) =>
        (b.created_at || 0) - (a.created_at || 0));
      refs = sorted;
      reindexPrompt();
      render();
      alert("智能排序不可用，已降级为「入库时间倒序」：" + e.message);
      return;
    }
    sortBusy = false;
    const before = refs.map(r => r.asset_id).join(",");
    refs = (data.refs || refs).slice(0, MAX_REFS);
    const after = refs.map(r => r.asset_id).join(",");
    if (before !== after) reindexPrompt();
    render();
  }

  function move(from, to) {
    if (from === to) return;
    const list = refs.slice();
    const moved = list.splice(from, 1)[0];
    list.splice(to, 0, moved);
    refs = list;
    reindexPrompt();
    render();
  }

  // ---------- 提示词占位同步 ----------

  /**
   * 提示词里的占位标记要跟参考图顺序对齐。
   * 做法：把提示词里出现的占位序号，按「当前参考图顺序」重排；
   * 若提示词里没有占位（用户没写），不动它 —— 提交时服务端会统一注入。
   */
  function reindexPrompt() {
    const box = $("#prompt");
    if (!box) return;
    const text = box.value || "";
    const found = parsePlaceholders(text);
    if (!found.length) return;
    // 现有占位序号（升序）映射到新的连续序号 1..N
    const mapping = {};
    found.forEach((old, i) => { mapping[old] = i + 1; });
    box.value = renumber(text, mapping);
  }

  /**
   * 生成带占位标记的提示词预览（给用户看「模型实际会收到什么」）。
   * 不改写用户的提示词输入框 —— 只是预览。
   */
  function buildPromptPreview(baseText) {
    const valid = refs.filter(r => !r.missing);
    if (!valid.length) return "";
    const multi = valid.length >= 2;
    const lines = valid.map((ref, i) => {
      const desc = (ref.description || "").trim();
      if (!multi) return desc || ref.name || "";
      return `${placeholderFor(i)} ${desc}`.trim();
    }).filter(Boolean);
    if (!lines.length) return "";
    const base = (baseText || "").trim();
    return base ? `${lines.join("\n")}\n${base}` : lines.join("\n");
  }

  /**
   * 提交前一致性校验：占位符与图片数量必须匹配。
   * 返回 null = 通过；否则返回错误信息数组。
   */
  function validate(promptText) {
    const errors = [];
    if (refs.length < 1) {
      errors.push("图片编辑至少需要 1 张参考图（参考图必须来自素材库）");
    }
    if (refs.length > MAX_REFS) {
      errors.push(`参考图最多 ${MAX_REFS} 张，当前 ${refs.length} 张`);
    }
    refs.forEach((ref, i) => {
      if (!ref.asset_id) {
        errors.push(`第 ${i + 1} 张参考图没有素材库 ID`);
      }
      if (ref.missing) {
        errors.push(`第 ${i + 1} 张参考图的素材已从素材库删除（${ref.name || ref.asset_id}），请移除后再生成`);
      }
    });
    if (refs.length >= 2) {
      const found = parsePlaceholders(promptText || "");
      const expected = [];
      for (let i = 1; i <= refs.length; i++) expected.push(i);
      const missing = expected.filter(x => !found.includes(x));
      const extra = found.filter(x => !expected.includes(x));
      if (!found.length) {
        errors.push(`有 ${refs.length} 张参考图，提示词里必须有 ${expected.map(n => `<image${n}>`).join(" ")} 占位标记（官方 PE-I2I 多图模板要求用 <imageN> 引用每张图）`);
      } else if (missing.length || extra.length) {
        const parts = [];
        if (missing.length) parts.push(`缺少 ${missing.map(n => `<image${n}>`).join("、")}`);
        if (extra.length) parts.push(`多出 ${extra.map(n => `<image${n}>`).join("、")}`);
        errors.push(`提示词里的占位标记与参考图数量不一致（${refs.length} 张参考图，应为 1..${refs.length} 连续）：${parts.join("，")}`);
      }
    }
    return errors.length ? errors : null;
  }

  /** 提交给后端的参考图元数据。 */
  function toPayload() {
    return refs.map((r, i) => ({
      asset_id: r.asset_id,
      version: r.version,
      name: r.name,
      description: r.description || "",
      description_source: r.description_source || "empty",
      tags: r.tags || [],
      created_at: r.created_at,
      preview_url: r.preview_url,
      missing: !!r.missing,
      ordinal: i,
    }));
  }

  // ---------- 渲染 ----------

  function render() {
    const box = $("#imgRefList");
    if (!box) return;
    const hint = $("#imgRefHint");
    if (hint) {
      hint.textContent = refs.length ? `${refs.length} / ${MAX_REFS} 张` : `最多 ${MAX_REFS} 张`;
    }
    const addBtn = $("#imgRefAdd");
    if (addBtn) addBtn.disabled = refs.length >= MAX_REFS;
    const sortBtn = $("#imgRefSort");
    if (sortBtn) {
      sortBtn.disabled = refs.length < 2 || sortBusy;
      sortBtn.textContent = sortBusy ? "排序中…" : "智能排序";
    }

    box.innerHTML = refs.map((r, i) => {
      const noDesc = !r.description && !r.missing;
      const cls = ["img-ref-item"];
      if (r.missing) cls.push("missing");
      else if (noDesc) cls.push("no-desc");
      const title = r.missing
        ? `参考图 ${i + 1}：素材已从素材库删除，请移除`
        : `参考图 ${i + 1}（${placeholderFor(i)}）：${r.description || r.name || "素材库未提供描述"}`;
      const badge = r.missing
        ? `<span class="img-ref-badge missing">已失效</span>`
        : (r.description ? "" : `<span class="img-ref-badge">待补描述</span>`);
      return `
        <div class="${cls.join(" ")}" data-idx="${i}" draggable="true" title="${esc(title)}">
          ${r.preview_url
            ? `<img src="${esc(r.preview_url)}" alt="参考图 ${i + 1}">`
            : `<div class="img-ref-ph"></div>`}
          <span class="img-ref-idx">${i + 1}</span>
          ${badge}
          <button type="button" class="img-ref-del" data-del="${i}" title="移除这张参考图">×</button>
        </div>`;
    }).join("");

    // 删除
    box.querySelectorAll("[data-del]").forEach(btn => {
      btn.addEventListener("click", e => {
        e.stopPropagation();
        removeAt(Number(btn.dataset.del));
      });
    });
    // 拖拽排序
    box.querySelectorAll(".img-ref-item").forEach(item => {
      item.addEventListener("dragstart", e => {
        item.classList.add("dragging");
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", item.dataset.idx);
      });
      item.addEventListener("dragend", () => {
        item.classList.remove("dragging");
        box.querySelectorAll(".img-ref-item").forEach(x => x.classList.remove("drag-over"));
      });
      item.addEventListener("dragover", e => {
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        item.classList.add("drag-over");
      });
      item.addEventListener("dragleave", () => item.classList.remove("drag-over"));
      item.addEventListener("drop", e => {
        e.preventDefault();
        item.classList.remove("drag-over");
        const from = Number(e.dataTransfer.getData("text/plain"));
        const to = Number(item.dataset.idx);
        if (Number.isNaN(from) || from === to) return;
        move(from, to);
      });
    });

    renderNotes();
    renderDescEditor();
  }

  function renderNotes() {
    const note = $("#imgRefNote");
    if (!note) return;
    const n = refs.length;
    const missing = refs.filter(r => r.missing).length;
    const noDesc = refs.filter(r => !r.description && !r.missing).length;
    const slots = 16;
    const parts = [];
    if (n >= MAX_REFS) {
      parts.push(`已达上限 ${MAX_REFS} 张（模型官方能力上限 10 张；ComfyUI 节点侧有 ${slots} 个 image 插槽，本面板取两者较小值 ${MAX_REFS}）。`);
    } else if (n) {
      parts.push(`还可以再加 ${MAX_REFS - n} 张（上限 ${MAX_REFS} 张，模型官方能力 10 张）。`);
    } else {
      parts.push(`至少 1 张、最多 ${MAX_REFS} 张（模型官方能力上限 10 张）。`);
    }
    parts.push("参考图**强制来自素材库**：先在「素材库」页上传入库，再从这里选择。");
    if (n >= 2) {
      parts.push("图序即语义：第 N 张对应提示词里的 <imageN>，官方 PE-I2I 模板要求多图必须用 <imageN> 引用。");
    }
    if (missing) parts.push(`⚠ 有 ${missing} 张参考图的素材已从素材库删除，请移除后再生成。`);
    if (noDesc) parts.push(`有 ${noDesc} 张素材库未提供描述，请手动补充（不会替你编造描述）。`);
    note.innerHTML = parts.map(esc).join("<br>");
  }

  function renderDescEditor() {
    const wrap = $("#imgRefDescList");
    const details = $("#imgRefDetail");
    const markerNote = $("#imgRefMarkerNote");
    if (!wrap) return;
    if (details) details.hidden = refs.length === 0;
    if (!refs.length) { wrap.innerHTML = ""; return; }

    if (markerNote) {
      const multi = refs.length >= 2;
      markerNote.innerHTML = multi
        ? `当前占位标记：${refs.map((r, i) => `<code>&lt;image${i + 1}&gt;</code>`).join("、")}（与送入模型的第 1…${refs.length} 张参考图一一对应）。`
        : `只有 1 张参考图：官方 PE-I2I 模板要求单图**不要**用 <imageN> 标签，会以自然表述引用。`;
    }

    wrap.innerHTML = refs.map((r, i) => {
      const src = r.description_source || "empty";
      const srcLabel = src === "analysis" ? "素材库描述"
        : src === "user" ? "手动补充" : "素材库未提供描述";
      const cls = r.missing ? "img-ref-desc-row missing" : "img-ref-desc-row";
      return `
        <div class="${cls}">
          <span class="img-ref-desc-tag" title="${esc(srcLabel)}">${placeholderFor(i)}</span>
          <textarea data-desc-idx="${i}" rows="2"
            placeholder="${r.missing ? "素材已失效，无法编辑" : "素材库未提供描述 —— 请手动补充这张图的内容描述"}"
            ${r.missing ? "disabled" : ""}>${esc(r.description || "")}</textarea>
        </div>`;
    }).join("");

    wrap.querySelectorAll("[data-desc-idx]").forEach(ta => {
      ta.addEventListener("change", e => {
        const idx = Number(e.target.dataset.descIdx);
        if (refs[idx]) {
          refs[idx].description = e.target.value;
          refs[idx].description_source = e.target.value.trim() ? "user" : "empty";
          render();
        }
      });
    });
  }

  // ---------- 对外接口 ----------
  window.QwenRefs = {
    getRefs, setRefs, clear, count,
    openLibraryPicker, addAssets,
    removeAt, smartSort, move,
    reindexPrompt, buildPromptPreview, validate, toPayload,
    render,
    MAX_REFS,
    placeholderFor, parsePlaceholders, renumber,
  };
})();
