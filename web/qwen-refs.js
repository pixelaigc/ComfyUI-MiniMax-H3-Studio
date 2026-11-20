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

  // ---------- 占位语法档案（profile） ----------
  //
  // 【为什么要参数化】
  // 这个模块原本只服务 Qwen-Image 2.1（占位标记 `<imageN>`、上限 10 张）。
  // MiniMax-H3-Image 用的是**另一套**语法：
  //   * 占位标记 `<Picture N>`（官方文档：the prompt addresses each one by its label）
  //   * 上限 16 张（官方文档：up to 16 input images）
  // 两套语法不能混用 —— 用 Qwen 的正则去扫 H3-Image 的 `<Picture 1>` 会扫不到，
  // 于是「用户写了合法标记」被判成「缺少占位标记」-> 误拒。
  //
  // 所以这里把「语法 + 上限 + 文案」抽成 profile，由 app.js 在切换模式时
  // 调 setProfile() 切换。**一份实现，两套语义**。
  const PROFILES = {
    qwen: {
      key: "qwen",
      maxRefs: 10,
      // 官方 PE-I2I system prompt：多图必须用 <image1>..<imageN>
      placeholder: i => `<image${i + 1}>`,
      regex: /<image(\d+)>/g,
      // 官方模板的说法，用于界面文案
      markerName: "<imageN>",
      // 官方能力上限（用于说明文案）
      officialCap: 10,
      nodeSlots: 16,
      // 【官方 PE-I2I system prompt 原文】"the rewritten instruction MUST use
      //  <image1>, <image2>, ..." -> 多图时占位标记是**强制**的。
      requirePlaceholders: true,
      singleTagAdvice:
        "只有 1 张参考图：官方 PE-I2I 模板要求单图「不要」用 <imageN> 标签，会以自然表述引用。",
      multiTagAdvice:
        "图序即语义：第 N 张对应提示词里的 <imageN>，官方 PE-I2I 模板要求多图必须用 <imageN> 引用。",
    },
    h3image: {
      key: "h3image",
      maxRefs: 16,
      // 官方文档：<Picture 1>..<Picture N>，按图片在提示词里的称呼引用
      placeholder: i => `<Picture ${i + 1}>`,
      regex: /<Picture\s*(\d+)\s*>/gi,
      markerName: "<Picture N>",
      officialCap: 16,
      nodeSlots: 100,      // ComfyUI 侧 io.Autogrow max=100
      // 【官方语义是「用 <Picture N> 引用」而非「必须引用」】
      // 官方文档原文：the prompt addresses each one by its label。
      // 标签是**建议** —— 用户写「把图1里的猫换成雪豹」这种自然语言
      // 也是合法的，不能因为没写 <Picture N> 就误拒。
      requirePlaceholders: false,
      singleTagAdvice:
        "只有 1 张参考图：可以直接用自然表述描述它（<Picture 1> 标记是可选的）。",
      multiTagAdvice:
        "图序即语义：第 N 张对应提示词里的 <Picture N>，官方文档要求用 <Picture N> 引用每张图。",
    },
  };

  // 当前生效的 profile（默认 Qwen —— 保持既有行为不变）
  let profile = PROFILES.qwen;

  /** 切换占位语法档案。由 app.js 在模式切换时调用。 */
  function setProfile(key) {
    const p = PROFILES[key];
    if (!p) return false;
    profile = p;
    return true;
  }

  function maxRefs() {
    return profile.maxRefs;
  }

  // ---------- 状态 ----------
  // refs: [{asset_id, version, name, description, description_source, tags,
  //         created_at, preview_url, missing}]
  let refs = [];
  let sortBusy = false;

  // ---------- 占位槽位模型 ----------
  //
  // 【不变式】提示词里的 <imageN> 永远指向「参考图列表的第 N 张」。
  //
  // 但列表会变（移除 / 排序 / 拖拽）。变更时不能按「出现顺序」重编 ——
  // 那样「删掉第 2 张」会把 <image3> 留在提示词里变成悬空标记，
  // 而模型只收到 2 张图，编号与插槽彻底错位。
  //
  // 做法：变更前后各拍一张「布局快照」（uid -> 槽位号），
  // 用「旧槽位 -> 新槽位」的单遍替换重写提示词：
  //   · 旧槽位的图还在 -> 换成新槽位号
  //   · 旧槽位的图被删了 -> **整个标记删掉**（不留悬空）
  //   · 提示词里本来就没有对应槽位的标记（用户手写超出的号）-> 原样留着，
  //     交给 validate() 报「多出 <imageN>」，不由这里偷偷改写
  //
  // uid 不可枚举，不会进 JSON payload（见 toPayload）。
  let uidSeq = 0;

  function ensureUid(ref) {
    if (!ref || typeof ref !== "object") return "";
    if (!ref.__uid) {
      Object.defineProperty(ref, "__uid", {
        value: "u" + (++uidSeq), enumerable: false, writable: true,
      });
    }
    return ref.__uid;
  }

  /** 拍一张布局快照：uid -> 槽位号（下标 + 1）。 */
  function captureLayout() {
    const layout = new Map();
    refs.forEach((ref, i) => layout.set(ensureUid(ref), i + 1));
    return layout;
  }

  // ---------- 工具 ----------
  function esc(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /** 给所有参考图发身份标记（uid）。 */
  function syncSlots() {
    refs.forEach(ensureUid);
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
    return profile.placeholder(index);
  }

  function parsePlaceholders(text) {
    const found = new Set();
    let m;
    const re = new RegExp(profile.regex.source, profile.regex.flags);
    while ((m = re.exec(text || "")) !== null) found.add(parseInt(m[1], 10));
    return [...found].sort((a, b) => a - b);
  }

  function renumber(text, mapping) {
    // 单遍替换：每个占位按它**原本**的序号查表。
    // 不能用「先替换大的再替换小的」—— 那会连环替换
    // （先把 <image3> 变成 <image2>，再被当成 <image2> 变成 <image1>）。
    if (!mapping || !Object.keys(mapping).length) return text || "";
    const re = new RegExp(profile.regex.source, profile.regex.flags);
    return (text || "").replace(re, (whole, num) => {
      const next = mapping[Number(num)];
      return next === undefined ? whole : profile.placeholder(next - 1);
    });
  }

  // ---------- 对外：读写参考图列表 ----------

  function getRefs() {
    return refs.slice();
  }

  function setRefs(list) {
    refs = (list || []).slice(0, maxRefs());
    refs.forEach(ensureUid);
    syncSlots();
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
    const max = maxRefs() - refs.length;
    if (max <= 0) {
      alert(`参考图最多 ${maxRefs()} 张，已达上限，请先移除一些。`);
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
    const room = maxRefs() - refs.length;
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
    refs = refs.concat(added).slice(0, maxRefs());
    // 新加入的图要发身份标记（uid）—— 后续移除/排序按身份追踪
    syncSlots();
    render();
    if (overflow > 0) {
      alert(`参考图最多 ${maxRefs()} 张，超出的 ${overflow} 张未添加。`);
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
    const oldLayout = captureLayout();
    refs.splice(index, 1);
    // 剩余参考图重新编号：占位标记整体重排，保持连续无空洞，
    // 被移除那张图的标记直接删掉（不留悬空 <imageN>）。
    reindexPrompt(oldLayout);
    render();
  }

  // ---------- 排序 ----------

  /**
   * 用服务端返回的列表替换 refs，但**保留每张图的身份（uid）**。
   *
   * 为什么必须做：服务端返回的是**新对象**（JSON 反序列化出来的），
   * 如果直接替换，这些图会拿到全新的 uid -> 槽位表对不上 ->
   * reindexPrompt() 会把提示词里的占位标记全删掉（明明只是排了个序）。
   * 用 asset_id 做桥接，把旧 uid 搬到新对象上。
   */
  function adoptSorted(newList) {
    const uidByAsset = new Map();
    refs.forEach(r => { uidByAsset.set(r.asset_id, ensureUid(r)); });
    return (newList || []).map(item => {
      const old = uidByAsset.get(item.asset_id);
      if (old) {
        Object.defineProperty(item, "__uid", {
          value: old, enumerable: false, writable: true,
        });
      } else {
        ensureUid(item);
      }
      return item;
    });
  }

  async function smartSort(promptText) {
    if (refs.length < 2) return;
    const oldLayout = captureLayout();
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
      reindexPrompt(oldLayout);
      render();
      alert("按匹配度排序不可用，已降级为「入库时间倒序」：" + e.message);
      return;
    }
    sortBusy = false;
    const before = refs.map(r => r.asset_id).join(",");
    refs = adoptSorted((data.refs || refs).slice(0, maxRefs()));
    const after = refs.map(r => r.asset_id).join(",");
    if (before !== after) reindexPrompt(oldLayout);
    render();
  }

  function move(from, to) {
    if (from === to) return;
    const oldLayout = captureLayout();
    const list = refs.slice();
    const moved = list.splice(from, 1)[0];
    list.splice(to, 0, moved);
    refs = list;
    reindexPrompt(oldLayout);
    render();
  }

  // ---------- 提示词占位同步 ----------

  /**
   * 提示词里的占位标记要跟参考图顺序对齐。
   *
   * @param {Map|null} oldLayout 变更**前**的布局快照（uid -> 旧槽位）。
   *   传 null = 首次渲染 / 用户没动过列表 -> 不改写提示词。
   *
   * 做法：用「旧槽位 -> 新槽位」的单遍替换重写。
   *   · 旧槽位的图还在   -> 换成新槽位号
   *   · 旧槽位的图被删了 -> **整个标记删掉**（不留悬空 <imageN>）
   *   · 提示词里有列表之外的号（用户手写超出的）-> 原样留着，
   *     交给 validate() 报「多出 <imageN>」，不由这里偷偷改写
   *
   * 单遍替换（不能用「先替换大的再替换小的」，会连环替换）。
   */
  function reindexPrompt(oldLayout) {
    const box = $("#prompt");
    if (!box) return;
    const text = box.value || "";
    if (!parsePlaceholders(text).length) return;
    if (!oldLayout || !oldLayout.size) return;

    // 旧槽位 -> uid -> 新槽位
    const newSlotByUid = captureLayout();
    const mapping = {};          // 旧槽位 -> 新槽位
    const removedSlots = new Set();
    oldLayout.forEach((oldSlot, uid) => {
      const ns = newSlotByUid.get(uid);
      if (ns === undefined) removedSlots.add(oldSlot);
      else mapping[oldSlot] = ns;
    });

    // 单遍替换：被移除的槽位 -> 标记删掉
    const re = new RegExp(profile.regex.source, profile.regex.flags);
    box.value = (text || "").replace(re, (whole, num) => {
      const n = Number(num);
      if (removedSlots.has(n)) return "";
      const next = mapping[n];
      return next === undefined ? whole : profile.placeholder(next - 1);
    });
    // 清理删除标记后留下的多余空格 / 连续空行
    box.value = box.value.replace(/[ \t]{2,}/g, " ").replace(/\n{3,}/g, "\n\n");
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
    if (refs.length > maxRefs()) {
      errors.push(`参考图最多 ${maxRefs()} 张，当前 ${refs.length} 张`);
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
      if (profile.requirePlaceholders) {
        // ---- Qwen-Image 2.1：官方 PE-I2I 多图模板要求写全 <image1>..<imageN> ----
        const missing = expected.filter(x => !found.includes(x));
        const extra = found.filter(x => !expected.includes(x));
        if (!found.length) {
          errors.push(`有 ${refs.length} 张参考图，提示词里必须有 ${expected.map(n => placeholderFor(n - 1)).join(" ")} 占位标记（官方要求用 ${profile.markerName} 引用每张图）`);
        } else if (missing.length || extra.length) {
          const parts = [];
          if (missing.length) parts.push(`缺少 ${missing.map(n => placeholderFor(n - 1)).join("、")}`);
          if (extra.length) parts.push(`多出 ${extra.map(n => placeholderFor(n - 1)).join("、")}`);
          errors.push(`提示词里的占位标记与参考图数量不一致（${refs.length} 张参考图，应为 1..${refs.length} 连续）：${parts.join("，")}`);
        }
      } else {
        // ---- H3-Image：官方语义是「用 <Picture N> 引用」而非「必须引用」。
        //      只拦「悬空引用」（引用了不存在的第 N 张）。
        //      没写标记 -> 只是建议，不拦（服务端同样只拦悬空引用）。
        const extra = found.filter(n => n > refs.length);
        if (extra.length) {
          errors.push(`提示词里引用了不存在的参考图：${extra.map(n => placeholderFor(n - 1)).join("、")}（当前只有 ${refs.length} 张）`);
        }
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
      hint.textContent = refs.length ? `${refs.length} / ${maxRefs()} 张` : `最多 ${maxRefs()} 张`;
    }
    const addBtn = $("#imgRefAdd");
    if (addBtn) addBtn.disabled = refs.length >= maxRefs();
    const sortBtn = $("#imgRefSort");
    if (sortBtn) {
      sortBtn.disabled = refs.length < 2 || sortBusy;
      sortBtn.textContent = sortBusy ? "排序中…" : "按匹配度排序";
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
    // 【2026-11-20 参考素材右键移除】右键缩略图 -> 菜单里选「移除」。
    //
    // 【为什么必须走 removeAt 而不是直接 splice】removeAt 做了三件事：
    //   1. 移除条目；
    //   2. 重新编号（后续条目前移，不留空洞）；
    //   3. 同步提示词里的占位标记（被移除那条的标记删掉，
    //      其余按「旧槽位 -> 新槽位」单遍重写）。
    // 直接 splice 就会留下悬空的 <imageN> —— 编号与实际插槽彻底错位。
    //
    // 【菜单本体在 web/ref-context-menu.js】三个模块共用一份实现，
    // 这里只提供「移除」的语义。
    if (window.RefContextMenu && typeof window.RefContextMenu.attach === "function") {
      box.querySelectorAll(".img-ref-item").forEach(item => {
        window.RefContextMenu.attach(item, () => {
          removeAt(Number(item.dataset.idx));
        }, "移除这张参考图");
      });
    }
    // 【2026-11-15 修复③ 交付项 2】点击已添加的媒体条目 -> 弹出预览模态框。
    //
    // 【为什么图片编辑类能力要这个】从素材库添加素材后，用户**看不到**素材
    // 长什么样 —— 缩略图只有 100px 出头，想看清细节只能去素材库翻。
    // 点一下就放大 / 播放，是「确认我选对了素材」的基本操作。
    //
    // 【复用既有组件】图片走 window.H3Studio.lightboxShow（app.js 已有的
    // 大图查看器，支持缩放 / 拖拽 / 适应屏幕 / 原始尺寸）；
    // 视频 / 音频走 window.MediaPreview 的模态框。
    //
    // 【不与删除/拖拽冲突】点 × 移除与拖拽排序各自 stopPropagation，
    // 只有点在缩略图本体上才开预览。
    box.querySelectorAll(".img-ref-item").forEach(item => {
      item.addEventListener("click", e => {
        if (e.target.closest("[data-del]")) return;
        const index = Number(item.dataset.idx);
        const ref = refs[index];
        if (!ref || ref.missing) return;
        const preview = window.MediaPreview;
        if (!preview || typeof preview.show !== "function") return;
        preview.show({
          url: ref.content_url
            || (ref.asset_id
                ? `/api/assets/${encodeURIComponent(ref.asset_id)}/content`
                : ref.preview_url),
          kind: ref.category || "image",
          name: ref.name || `参考图 ${index + 1}`,
          mime: ref.mime,
        });
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
    const cap = maxRefs();
    const slots = profile.nodeSlots;
    const parts = [];
    if (n >= cap) {
      parts.push(`已达上限 ${cap} 张（模型官方能力上限 ${profile.officialCap} 张；ComfyUI 节点侧有 ${slots} 个 image 插槽，本面板取两者较小值 ${cap}）。`);
    } else if (n) {
      parts.push(`还可以再加 ${cap - n} 张（上限 ${cap} 张，模型官方能力 ${profile.officialCap} 张）。`);
    } else {
      parts.push(`至少 1 张、最多 ${cap} 张（模型官方能力上限 ${profile.officialCap} 张）。`);
    }
    parts.push("参考图「强制来自素材库」：先在「素材库」页上传入库，再从这里选择。");
    if (n >= 2) {
      parts.push(profile.multiTagAdvice);
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
    if (details) {
      details.hidden = refs.length === 0;
      // 参考图有描述要**始终可见**（需求 B1：把描述/标签带入并在 UI 显示），
      // 折叠起来用户就看不到模型到底拿到了什么描述。
      if (refs.length) details.open = true;
    }
    if (!refs.length) { wrap.innerHTML = ""; return; }

    if (markerNote) {
      const multi = refs.length >= 2;
      markerNote.innerHTML = multi
        ? `当前占位标记：${refs.map((r, i) => `<code>${esc(placeholderFor(i))}</code>`).join("、")}（与送入模型的第 1…${refs.length} 张参考图一一对应）。`
        : profile.singleTagAdvice;
    }

    wrap.innerHTML = refs.map((r, i) => {
      const src = r.description_source || "empty";
      const srcLabel = src === "analysis" ? "素材库描述"
        : src === "user" ? "手动补充" : "素材库未提供描述";
      const cls = r.missing ? "img-ref-desc-row missing" : "img-ref-desc-row";
      // 【2026-11-15 修②】文本框的行数按描述长度给。
      //
      // 【原来为什么只有 2 行】原来写死 rows="2"，而素材库的分析描述是
      // **多小节长文本**（实测 300~5000 字）—— 2 行只露出开头一小截，
      // 用户看到的就是「只有第一行」，以为描述被系统截断了。
      //
      // 【现在怎么给行数】
      //   按换行行数与字符数共同估算，短描述仍然紧凑（2~4 行），
      //   长描述给足行数并允许滚动 —— 描述**完整可读、可编辑**。
      const descText = r.description || "";
      const lineCount = descText.split("\n").length;
      const rows = Math.max(2, Math.min(18, lineCount + 1));
      return `
        <div class="${cls}">
          <span class="img-ref-desc-tag" title="${esc(srcLabel)}">${esc(placeholderFor(i))}</span>
          <textarea data-desc-idx="${i}" rows="${rows}" spellcheck="false"
            placeholder="${r.missing ? "素材已失效，无法编辑" : "素材库未提供描述 —— 请手动补充这张图的内容描述"}"
            ${r.missing ? "disabled" : ""}>${esc(descText)}</textarea>
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
    setProfile, maxRefs,
    placeholderFor, parsePlaceholders, renumber,
  };
})();
