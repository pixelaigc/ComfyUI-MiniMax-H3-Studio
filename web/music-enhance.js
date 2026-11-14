/* ============================================================
   YuE2 音乐增强（结构化风格 + 歌词）
   ------------------------------------------------------------
   【为什么单独一个文件】
   音乐面板的「增强」必须与生图面板**同一套组件与视觉**（用户要求：
   不另造一套）。所以本文件**复用**共用组件 web/enhance-menu-shared.js
   （renderEnhanceMenu / bindEnhanceMenu / positionMenu / resolveWriteTarget），
   自己只负责：
     * 路由到 POST /api/music-enhance
     * 结果回填到音乐面板的结构化风格区与歌词区
     * **注册写回能力契约**（供 providers.js / enhance-menu.js 解析写回目标）

   【与生图面板的关系：同一份注册表】
   增强方式注册表现在**只有一份** —— enhance-menu.js 的
   ENHANCE_METHODS，音乐路线那一项（yue2_struct）也在里面。
   （审查 P0-2 的根治要求：「targets() 与音乐增强方法注册表并入同一份
   增强体系」。）
   之前本文件另有一份 MUSIC_ENHANCE_METHODS，两边都渲染同一个
   #musicEnhanceMenu、都绑同一个 #musicEnhanceBtn -> 互相覆盖，
   实测音乐面板的下拉里出现的是图像路线的项。
   现在：菜单渲染 / 勾选 / 按钮文字 / 当前方式回显**全部**由统一注册表负责；
   本模块只提供「怎么执行」与「怎么写回」。

   【写回能力契约（审查 P0-1）】
   旧实现（enh-unify 分支）硬编码 `$("#musicStyle")` 作为音乐增强的
   写回目标。yue2-flow 把那个 textarea 改名成 #musicPrompt（音乐描述），
   并把「结构化风格」拆成 12 个独立字段 —— 合并后
   `document.querySelector("#musicStyle")` 返回 null，
   紧接着 `.value` 抛 TypeError（写回静默死掉）。
   现在音乐侧**显式注册**结构化契约：写回目标由本模块声明，
   任何调用方都通过 resolveWriteTarget() 拿目标，拿不到就给可读提示。

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

  // 【音乐侧不再有自己的增强方式注册表】
  //
  // yue2_struct 已并入**统一注册表**（enhance-menu.js 的 ENHANCE_METHODS）——
  // 那是审查 P0-2 的根治要求（「targets() 与音乐增强方法注册表并入同一份
  // 增强体系」）。
  //
  // 之前两边各有一份注册表、都渲染同一个 #musicEnhanceMenu、都绑同一个
  // #musicEnhanceBtn，结果互相覆盖：实测音乐面板的下拉里出现的是
  // basic/refine/layout/qwen_pe（图像路线的项），yue2_struct 根本不在里面。
  //
  // 现在：菜单渲染 / 勾选 / 按钮文字 / 当前方式回显**全部**由统一注册表负责；
  // 本模块只提供「怎么执行」（run）与「怎么写回」（契约）。



  // ------------------------------------------------------------------ 菜单

  /**
   * 当前选中的音乐增强方式（读统一注册表的状态）。
   *
   * 统一注册表（window.enhanceMethods）持有选中集合与渲染逻辑；
   * 这里只是**读**它，用于回显与「至少留一项」的判断。
   */
  function selectedMethods() {
    const unified = window.enhanceMethods;
    if (unified && typeof unified.getSelected === "function") {
      const ids = unified.getSelected();
      const list = unified.list || [];
      return list.filter(m => ids.indexOf(m.id) >= 0);
    }
    return [];
  }

  /**
   * 当前音乐面板的「当前增强方式」回显。
   *
   * 统一注册表已经负责渲染这条回显（enhance-menu.js 的
   * updateCurrentIndicator 会同时更新 #enhanceCurrent 与
   * #musicEnhanceCurrent）。这里保留一个空实现是为了兼容旧调用方
   * （app.js 仍会调 window.musicEnhance.updateButton()）。
   */
  function updateButton() {
    const unified = window.enhanceMethods;
    if (unified && typeof unified.refreshButton === "function") {
      unified.refreshButton();
    }
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
        // 用**活的**当前模式（getCurrentMode），不能用 getState().mode ——
        // 后者是持久化的那份，切了左侧标签不一定跟着变（会发错 t2a/a2a）。
        mode: (typeof music.getCurrentMode === "function"
               ? music.getCurrentMode() : music.getState().mode) || "t2a",
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

  // ------------------------------------------------------------------ 写回能力契约

  /**
   * 音乐侧的**写回能力契约**（结构化路线）。
   *
   * 契约由本模块声明，不依赖别的模块硬编码本项目的 DOM id：
   *   kind = "structured" -> 产物是「结构化风格 + 歌词」，不是一个输入框
   *   apply(obj)         -> 由本模块负责把结构化产物落到界面上
   *
   * 调用方（providers.js 的「替换/追加」按钮、enhance-menu.js 的二次增强）
   * 通过 window.EnhanceMenuShared.resolveWriteTarget(contract) 拿目标，
   * 拿不到时得到 { ok:false, reason } —— 不抛异常，给可读提示。
   */
  function getWriteContract() {
    return {
      kind: "structured",
      // 说明：写回目标缺失时给用户看的提示
      describe: () => "音乐面板的结构化风格区或歌词区不存在，无法写回",
      apply: (obj) => {
        const music = window.MusicUI;
        if (!music) return false;
        if (obj && obj.style) music.setStyleFields(obj.style);
        if (obj && obj.lyrics) {
          music.setLyrics(obj.lyrics.lyrics_override || obj.lyrics_text || obj.lyrics || "");
        }
        if (typeof music.markEnhanced === "function") music.markEnhanced();
        return true;
      },
    };
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
    // 【不再自己绑主按钮】审查 P0-2 返修实测发现的问题：
    // 增强体系合并后，enhance-menu.js 的统一注册表已经包含
    // yue2_struct 这一项，并**自己**绑了 #musicEnhanceBtn /
    // #musicEnhanceMenuBtn。如果本模块再绑一遍：
    //   - 主按钮会触发两次（图像注册表的 run + 音乐注册表的 run）
    //   - 菜单内容会被两套注册表互相覆盖
    //     （实测：音乐菜单被渲染成 basic/refine/layout/qwen_pe）
    // 现在**只保留一份绑定** —— 统一注册表（enhance-menu.js）负责，
    // 本模块只提供「怎么执行」（run）与「怎么写回」（契约）。
    //
    // 菜单的**渲染**也交给统一注册表（renderMenu 由 enhance-menu.js 调），
    // 本模块不再单独渲染一份，避免互相覆盖。

    // 【2026-11-14 需求 7】原先这里绑了 #musicStyleRegenBtn /
    // #musicLyricsRegenBtn（「基于当前内容重新生成」）。
    // 两个按钮都已从 HTML 移除 —— 想重新生成直接点上面的
    // 「生成风格+歌词」按钮即可（用户明确要求去掉这两个重复入口）。
    // 绑定代码一并删除，避免留下指向不存在 DOM 的死代码。
  }

  // ------------------------------------------------------------------ 启动

  function boot() {
    bind();
    if (typeof window !== "undefined") {
      window.MusicEnhance = {
        run: run,
        // 菜单渲染由统一注册表负责 —— 这里只是**代理**，
        // 旧调用方（app.js 的 window.musicEnhance.renderMenu()）仍可用。
        renderMenu: () => {
          const unified = window.enhanceMethods;
          if (unified && typeof unified.renderMenu === "function") unified.renderMenu();
        },
        updateButton: updateButton,
        getSelected: () => selectedMethods().map(m => m.id),
        list: (window.enhanceMethods && window.enhanceMethods.list) || [],
        // 写回能力契约（供 providers.js / enhance-menu.js 解析目标）
        getWriteContract: getWriteContract,
      };
      // 兼容旧名（上一轮交付里用的是 window.musicEnhance）
      window.musicEnhance = window.MusicEnhance;
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
