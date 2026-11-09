/* ============================================================
   YuE2 音乐增强（结构化风格 + 歌词）
   ------------------------------------------------------------
   【为什么单独一个文件】
   音乐面板的「增强」必须与生图面板**同一套组件与视觉**（用户要求：
   不另造一套）。所以本文件**复用** enhance-menu.js 导出的共用实现
   （renderEnhanceMenu / bindEnhanceMenu / positionMenu），
   自己只负责：
     * 音乐侧的增强项注册表（当前只有一项：YuE2 结构化风格 + 歌词）
     * 路由到 POST /api/music-enhance
     * 结果回填到音乐面板的结构化风格区与歌词区

   【与生图面板的关系】
   生图面板的注册表在 enhance-menu.js 的 ENHANCE_METHODS 里，
   音乐面板的注册表在这里 MUSIC_ENHANCE_METHODS 里 ——
   两份注册表**内容不同**（音乐要产出结构化风格+歌词，不是文本扩写），
   但**组件实现是同一份**（enhance-menu.js 导出的共用函数）。

   【链路对应 nonebot 插件的咏唱文生音乐】
     POST /api/music-enhance
       -> 服务端 h3studio/yue2_schema.py build_system_prompt()
          （枚举从 ComfyUI /object_info 运行时拉）
       -> 本地模型产出 JSON
       -> normalize_and_validate() 归一化 + 校验
       -> music_style.build_style_prompt() 合成最终 style 串
   ============================================================ */
(function () {
  "use strict";

  const $ = s => document.querySelector(s);

  // ------------------------------------------------------------------ 注册表

  /**
   * 音乐侧的增强方式注册表。
   *
   * 字段语义与 enhance-menu.js 的 ENHANCE_METHODS 一致 —— 因为渲染用的是
   * **同一个** renderEnhanceMenu()（共用组件）。
   */
  const MUSIC_ENHANCE_METHODS = [
    {
      id: "yue2_struct",
      label: "YuE2 结构化风格 + 歌词",
      hint: "把描述拆成 YuE2 的结构化风格字段（语言/曲风/情绪/人声/乐器/BPM/能量/意象）并生成配套歌词",
      modes: [],              // 音乐面板专属注册表 -> 恒可用
      busyFlag: "enhance",
      buttonText: "生成风格+歌词",
      recommended: true,
    },
  ];

  // 当前选中的 id 集合（音乐侧目前只有一项，仍按多选语义实现以便将来扩展）
  let selectedIds = ["yue2_struct"];

  const STATE_KEY = "h3studio.musicEnhanceSelection.v1";

  function saveSelection() {
    try {
      localStorage.setItem(STATE_KEY, JSON.stringify({ selected: selectedIds }));
    } catch (e) { /* 隐私模式忽略 */ }
  }

  function loadSelection() {
    try {
      const raw = localStorage.getItem(STATE_KEY);
      if (!raw) return;
      const data = JSON.parse(raw);
      if (data && Array.isArray(data.selected)) {
        const valid = data.selected.filter(id =>
          MUSIC_ENHANCE_METHODS.some(m => m.id === id));
        if (valid.length) selectedIds = valid;
      }
    } catch (e) { /* 忽略 */ }
  }

  // ------------------------------------------------------------------ 菜单

  function isAvailable() {
    // 音乐面板专属注册表 -> 恒可用
    return true;
  }

  function renderMenu() {
    const menu = $("#musicEnhanceMenu");
    if (!menu) return;
    const shared = window.EnhanceMenuShared;
    if (!shared) {
      // 共用组件没加载（脚本顺序问题）—— 明确报错，不静默失败
      setHint("增强组件未就绪（enhance-menu.js 未加载），请刷新页面", "error");
      return;
    }
    shared.renderEnhanceMenu(menu, {
      methods: MUSIC_ENHANCE_METHODS,
      selectedIds: selectedIds,
      isAvailable: isAvailable,
      onToggle: toggleMethod,
      onFieldEdit: () => { /* 音乐侧当前没有可编辑字段 */ },
    });
  }

  function toggleMethod(id, on) {
    if (on) {
      if (selectedIds.indexOf(id) < 0) selectedIds.push(id);
    } else {
      if (selectedIds.length <= 1) {
        // 至少留一项：取消最后一项时保持原状
        setHint("至少要保留一项增强方式", "error");
        return;
      }
      selectedIds = selectedIds.filter(x => x !== id);
    }
    saveSelection();
    renderMenu();
    updateButton();
  }

  function updateButton() {
    const text = $("#musicEnhanceBtnText");
    const sel = selectedMethods();
    const method = sel[0] || MUSIC_ENHANCE_METHODS[0];
    if (text) text.textContent = sel.length > 1 ? `增强（${sel.length} 项）` : method.buttonText;
    const btn = $("#musicEnhanceBtn");
    if (btn) btn.title = method.hint;
  }

  function selectedMethods() {
    return MUSIC_ENHANCE_METHODS.filter(m => selectedIds.indexOf(m.id) >= 0);
  }

  // ------------------------------------------------------------------ 路由

  /**
   * 执行增强。
   *
   * @param {object} opts
   *   base      以当前内容为底稿（true = 修改当前结果；false = 从零生成）
   *   intent    覆盖目标：'all' | 'style' | 'lyrics'
   */
  async function run(opts) {
    opts = opts || {};
    const sel = selectedMethods();
    if (!sel.length) {
      setHint("请先在「增强」下拉里选中至少一项", "error");
      return;
    }
    const btn = $("#musicEnhanceBtn");
    const music = window.MusicUI;
    if (!music) {
      setHint("音乐面板模块未加载", "error");
      return;
    }

    const promptEl = $("#musicPrompt");
    const prompt = promptEl ? promptEl.value.trim() : "";
    const styleFields = music.getStyleFields() || {};
    const hasStyle = Object.keys(styleFields).length > 0;
    const lyrics = music.getLyrics() || "";
    const hasLyrics = lyrics.trim().length > 0;
    const base = opts.base === true ? (hasStyle || hasLyrics) : false;

    if (!prompt && !base) {
      setHint("请先输入音乐描述", "error");
      if (promptEl) promptEl.focus();
      return;
    }

    if (btn) btn.disabled = true;
    music.setBusy(true);
    setHint("正在生成结构化风格与歌词…", "busy");
    try {
      const payload = {
        prompt: prompt,
        mode: music.getState().mode || "t2a",
      };
      if (base) {
        // 以当前内容为底稿 —— 用户改了什么就在「修改意图」里说什么
        payload.base = "当前内容";
        payload.style = styleFields;
        payload.lyrics = lyrics;
      }
      const d = await apiJSON("POST", "/api/music-enhance", payload);
      applyResult(d, opts.intent || "all");
      const fixes = (d.fixes || []).length;
      setHint(
        `已生成结构化风格与歌词（${d.provider || "本地模型"}）`
        + (fixes ? `，自动纠正 ${fixes} 处` : ""),
        "ok");
    } catch (e) {
      const detail = e.payload && e.payload.error ? e.payload.error : e.message;
      setHint(`增强失败：${detail}`, "error");
      alert(`YuE2 音乐增强失败\n\n${detail}\n\n请检查「本地模型配置」里的 Provider 是否可用。`);
    } finally {
      if (btn) btn.disabled = false;
      music.setBusy(false);
      updateButton();
    }
  }

  /**
   * 回填增强结果。
   *
   * intent = 'all'    -> 风格与歌词都回填
   * intent = 'style'  -> 只回填风格（歌词保持用户当前编辑的内容）
   * intent = 'lyrics' -> 只回填歌词（风格保持用户当前编辑的内容）
   */
  function applyResult(d, intent) {
    const music = window.MusicUI;
    if (!music) return;
    if ((intent === "all" || intent === "style") && d.style) {
      music.setStyleFields(d.style);
    }
    if ((intent === "all" || intent === "lyrics") && d.lyrics) {
      music.setLyrics(d.lyrics.lyrics_override || d.lyrics_text || "");
    }
    music.markEnhanced();
    // 自动修复 / 质量问题展示在风格区说明里（用户能看到模型被纠正了什么）
    const note = $("#musicStyleStructNote");
    if (note) {
      const fixes = d.fixes || [];
      const problems = d.problems || [];
      const parts = [];
      if (fixes.length) parts.push(`自动纠正 ${fixes.length} 处：${fixes.slice(0, 3).join("；")}`);
      if (problems.length) parts.push(`提示：${problems.slice(0, 2).join("；")}`);
      note.textContent = parts.length
        ? parts.join("　|　")
        : "增强后这里会自动填好结构化风格字段，可以逐项改，改完再生成。";
    }
  }

  // ------------------------------------------------------------------ 工具

  function setHint(text, kind) {
    const hint = $("#musicEnhanceHint");
    if (!hint) return;
    hint.textContent = text;
    hint.className = "enhance-hint" + (kind ? " " + kind : "");
  }

  async function apiJSON(method, path, body) {
    const opt = { method, headers: { "Content-Type": "application/json" } };
    if (body !== undefined) opt.body = JSON.stringify(body);
    const resp = await fetch(path, opt);
    const raw = await resp.text();
    let data;
    try { data = raw ? JSON.parse(raw) : {}; }
    catch (e) { data = { error: raw || "服务返回了非 JSON 内容" }; }
    if (!resp.ok) {
      const err = new Error(data.error || `请求失败（HTTP ${resp.status}）`);
      err.status = resp.status;
      err.payload = data;
      throw err;
    }
    return data;
  }

  // ------------------------------------------------------------------ 绑定

  function bind() {
    const btn = $("#musicEnhanceBtn");
    if (btn) btn.addEventListener("click", () => run());

    const menu = $("#musicEnhanceMenu");
    const menuBtn = $("#musicEnhanceMenuBtn");
    // 菜单开合走**共用实现**（与生图面板同一套组件）
    const shared = window.EnhanceMenuShared;
    if (shared) {
      shared.bindEnhanceMenu(menu, menuBtn, "musicEnhanceSplit", renderMenu);
    }

    // 「基于当前内容重新生成」
    const styleRegen = $("#musicStyleRegenBtn");
    if (styleRegen) {
      styleRegen.addEventListener("click", () => run({ base: true, intent: "style" }));
    }
    const lyricsRegen = $("#musicLyricsRegenBtn");
    if (lyricsRegen) {
      lyricsRegen.addEventListener("click", () => run({ base: true, intent: "lyrics" }));
    }
  }

  // ------------------------------------------------------------------ 启动

  function boot() {
    loadSelection();
    bind();
    updateButton();
    renderMenu();
    if (typeof window !== "undefined") {
      window.musicEnhance = {
        run: run,
        renderMenu: renderMenu,
        updateButton: updateButton,
        getSelected: () => selectedIds.slice(),
        list: MUSIC_ENHANCE_METHODS,
      };
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
