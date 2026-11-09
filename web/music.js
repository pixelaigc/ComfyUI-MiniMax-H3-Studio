/* H3 Studio 音乐面板（YuE2）：文生音乐 / 参考生音乐
 *
 * 【2026-11-09 重设计 —— 用户需求逐条对应】
 * 1. 文生音乐 / 参考生音乐是**左侧两个独立标签**（.nav-item[data-mode]）。
 *    面板顶部**不再有任何「生成模式」切换**（旧的 #musicModeChips /
 *    #musicModeNote 已从 index.html 删除），只读 state.mode。
 * 2. 流程：音乐描述框 -> 「增强」按钮（+下拉）-> 增强后产出
 *    **结构化风格** 与 **歌词** 两份结果。
 *    增强按钮/下拉复用**与生图面板完全同一套组件**
 *    （.enhance-split / .enhance-btn / .enhance-menu），
 *    见 web/music-enhance.js（它用 enhance-menu.js 导出的
 *    window.EnhanceMenuShared）。
 * 3. 移除「最长时长」参数 -> index.html 里的 #musicDuration 已删除，
 *    后端 music_yue2 也不再按时长上限截断（生成自然完成）。
 * 5. 结果形态：增强后可逐项编辑结构化风格 / 歌词；
 *    也可「基于当前内容重新生成/修改」（#musicStyleRegenBtn /
 *    #musicLyricsRegenBtn）。
 *
 * 独立成文件（不塞进 app.js）的原因：app.js 已 3800 行，音乐面板自成一块
 * 逻辑（模型清单 / 参考音频选择 / 参数持久化），单独放更清楚，也避免和
 * 并发的其它前端改动撞同一段代码。
 *
 * 与 app.js 的接口约定：
 *   window.MusicUI.render()          渲染面板（renderMode 调）
 *   window.MusicUI.collect()         收集参数（提交时调）→ {params, assetRefs}
 *   window.MusicUI.loadOptions()     拉 /api/music-options
 *   window.MusicUI.isMusicMode()     当前是否音乐模式
 *   window.MusicUI.promptValue()     当前「风格描述」（提交时当 prompt 用）
 */
(() => {
  "use strict";

  const $ = s => document.querySelector(s);
  const $$ = s => [...document.querySelectorAll(s)];

  const MUSIC_MODES = ["t2a", "a2a"];

  // 参数清单（由 /api/music-options 下发，前端不硬编码模型文件名）
  const OPTIONS = {
    checkpoints: {},
    audio_encoders: {},
    default_checkpoint: "yue2_3b_bf16",
    default_audio_encoder: "sheetsage2_bf16",
    sampler: "dpm_2",
    scheduler: "sgm_uniform",
    steps: 32,
    cfg: 1.0,
    samplers: [],
    schedulers: [],
    modes: ["full", "melody"],
    music_sampling: { temperature: 0.8, top_p: 0.9, top_k: 50, repetition_penalty: 1.05 },
    formats: ["flac", "mp3", "opus"],
    default_format: "flac",
    // 结构化风格（对齐第三方 YuE2StylePrompt）
    style_fields: [],
    style_defaults: {},
    style_enums: {},
    lyric_templates: [],
    lyric_template_default: "V-C-V-C-bridge-C",
    loaded: false,
  };

  // 参数持久化（记住上次设置）—— localStorage 不可用时静默降级
  const STORE_KEY = "h3studio.musicParams";
  const DEFAULT_STATE = {
    mode: "t2a",
    checkpoint: "",
    audio_encoder: "",
    plan: "full",
    steps: 32,
    cfg: 1.0,
    sampler: "dpm_2",
    scheduler: "sgm_uniform",
    format: "flac",
    // 音乐描述（增强的输入；也是没有结构化风格时的 style 来源）
    prompt: "",
    lyrics: "",
    // 结构化风格的 12 个字段（键 = style_<field>）
    style_fields: {},
    lyrics_template: "V-C-V-C-bridge-C",
    seed: "",
    temperature: 1.0,
    top_p: 0.95,
    top_k: 100,
    repetition_penalty: 1.2,
  };

  let st = loadState();

  function loadState() {
    try {
      const raw = localStorage.getItem(STORE_KEY);
      if (!raw) return { ...DEFAULT_STATE };
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object") return { ...DEFAULT_STATE };
      return { ...DEFAULT_STATE, ...parsed };
    } catch (e) {
      return { ...DEFAULT_STATE };
    }
  }

  function saveState() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(st));
    } catch (e) { /* 存储不可用，忽略 */ }
  }

  // ---------------------------------------------------------------- 状态

  function currentMode() {
    const app = window.__h3state ? window.__h3state() : null;
    if (app && MUSIC_MODES.includes(app.mode)) return app.mode;
    return st.mode;
  }

  function isMusicMode(mode) {
    return MUSIC_MODES.includes(mode || currentMode());
  }

  // 参考音频：只存 asset_id（素材库引用），不复制文件
  let refAudio = null;   // {asset_id, name, category}

  // ---------------------------------------------------------------- 拉清单

  async function loadOptions() {
    try {
      const resp = await fetch("/api/music-options", { cache: "no-store" });
      if (!resp.ok) return false;
      const data = await resp.json();
      OPTIONS.checkpoints = data.checkpoints || {};
      OPTIONS.audio_encoders = data.audio_encoders || {};
      OPTIONS.default_checkpoint = data.default_checkpoint || OPTIONS.default_checkpoint;
      OPTIONS.default_audio_encoder = data.default_audio_encoder || OPTIONS.default_audio_encoder;
      OPTIONS.sampler = data.sampler || OPTIONS.sampler;
      OPTIONS.scheduler = data.scheduler || OPTIONS.scheduler;
      OPTIONS.steps = data.steps || OPTIONS.steps;
      OPTIONS.cfg = data.cfg !== undefined ? data.cfg : OPTIONS.cfg;
      OPTIONS.samplers = data.samplers || [];
      OPTIONS.schedulers = data.schedulers || [];
      OPTIONS.modes = data.modes || ["full", "melody"];
      OPTIONS.music_sampling = data.music_sampling || OPTIONS.music_sampling;
      OPTIONS.formats = data.formats || ["flac", "mp3"];
      OPTIONS.default_format = data.default_format || OPTIONS.default_format;
      // 结构化风格（对齐第三方 YuE2StylePrompt）
      OPTIONS.style_fields = data.style_fields || [];
      OPTIONS.style_defaults = data.style_defaults || {};
      OPTIONS.style_enums = data.style_enums || {};
      OPTIONS.lyric_templates = data.lyric_templates || [];
      OPTIONS.lyric_template_default = data.lyric_template_default || OPTIONS.lyric_template_default;
      OPTIONS.loaded = true;

      // 首次进入时把默认值对齐服务端推荐
      if (!st.checkpoint) st.checkpoint = OPTIONS.default_checkpoint;
      if (!st.audio_encoder) st.audio_encoder = OPTIONS.default_audio_encoder;
      if (!st.sampler) st.sampler = OPTIONS.sampler;
      if (!st.scheduler) st.scheduler = OPTIONS.scheduler;
      if (!st.format) st.format = OPTIONS.default_format;
      if (!st.lyrics_template) st.lyrics_template = OPTIONS.lyric_template_default;
      const ms = OPTIONS.music_sampling;
      if (!st.temperature) st.temperature = ms.temperature;
      if (!st.top_p) st.top_p = ms.top_p;
      if (!st.top_k) st.top_k = ms.top_k;
      if (!st.repetition_penalty) st.repetition_penalty = ms.repetition_penalty;
      saveState();
      return true;
    } catch (e) {
      return false;
    }
  }

  // ---------------------------------------------------------------- 渲染

  function render() {
    const mode = currentMode();
    if (!isMusicMode(mode)) return;

    // 【2026-11-09】面板内不再有「生成模式」切换 ——
    // 文生音乐 / 参考生音乐是**左侧两个独立标签**，这里只读当前 mode。
    const isCover = mode === "a2a";

    // 模型
    const modelSel = $("#musicModelSelect");
    if (modelSel) {
      modelSel.innerHTML = "";
      Object.entries(OPTIONS.checkpoints).forEach(([key, m]) => {
        const opt = document.createElement("option");
        opt.value = key;
        const unavailable = m.available === false;
        opt.textContent = unavailable ? m.label + "（本机无原生格式权重，不可用）" : m.label;
        opt.disabled = unavailable;
        opt.selected = key === st.checkpoint;
        modelSel.appendChild(opt);
      });
      if (!modelSel.options.length) {
        const opt = document.createElement("option");
        opt.textContent = "（后端未就绪或无权重）";
        opt.disabled = true;
        modelSel.appendChild(opt);
      }
    }
    const curModel = OPTIONS.checkpoints[st.checkpoint];
    const modelNote = $("#musicModelNote");
    const modelWarn = $("#musicModelWarn");
    if (modelNote) {
      modelNote.textContent = curModel ? (curModel.note || "") : "";
    }
    if (modelWarn) {
      const missing = curModel && curModel.available === false;
      modelWarn.textContent = missing
        ? "本机缺少该权重文件，无法使用该版本。已自动切回可用版本。"
        : (curModel ? (curModel.warn || "") : "");
      modelWarn.style.display = (modelWarn.textContent) ? "" : "none";
    }
    // 当前选中的模型不可用 -> 回落到默认
    if (curModel && curModel.available === false) {
      st.checkpoint = OPTIONS.default_checkpoint;
    }

    // 音频编码器（仅参考生音乐需要）
    const encField = $("#musicEncoderField");
    if (encField) encField.style.display = isCover ? "" : "none";
    const encSel = $("#musicEncoderSelect");
    if (encSel) {
      encSel.innerHTML = "";
      Object.entries(OPTIONS.audio_encoders).forEach(([key, m]) => {
        const opt = document.createElement("option");
        opt.value = key;
        const unavailable = m.available === false;
        opt.textContent = unavailable ? m.label + "（本机无原生格式权重，不可用）" : m.label;
        opt.disabled = unavailable;
        opt.selected = key === st.audio_encoder;
        encSel.appendChild(opt);
      });
      if (!encSel.options.length) {
        const opt = document.createElement("option");
        opt.textContent = "（后端未就绪或无权重）";
        opt.disabled = true;
        encSel.appendChild(opt);
      }
    }
    const curEnc = OPTIONS.audio_encoders[st.audio_encoder];
    const encNote = $("#musicEncoderNote");
    if (encNote) encNote.textContent = curEnc ? (curEnc.note || "") : "";
    if (curEnc && curEnc.available === false) {
      st.audio_encoder = OPTIONS.default_audio_encoder;
    }

    // 参考音频区（仅参考生音乐）
    const refField = $("#musicRefField");
    if (refField) refField.style.display = isCover ? "" : "none";
    renderRefAudio();

    // 结构化风格（对齐第三方 YuE2StylePrompt）
    renderStyleFields();

    // 谱面规划 chips
    const planChips = $("#musicPlanChips");
    if (planChips) {
      planChips.innerHTML = "";
      OPTIONS.modes.forEach(modeKey => {
        const b = document.createElement("button");
        b.type = "button";
        b.dataset.v = modeKey;
        b.textContent = modeKey === "full" ? "完整谱面（旋律+和弦）" : "仅旋律（适合翻唱）";
        // 参考生音乐：SheetSage2 转谱固定输出「仅旋律」谱面，
        // 「完整谱面」在这里拿不到和弦符号 -> 禁用并说明原因，
        // 不能给用户一个点了不生效的选项。
        if (isCover && modeKey === "full") {
          b.disabled = true;
          b.title = "参考音频转谱固定产出仅旋律谱面，无法附加和弦符号";
        }
        b.classList.toggle("active", modeKey === (isCover ? "melody" : st.plan));
        b.addEventListener("click", () => {
          if (b.disabled) return;
          st.plan = modeKey;
          saveState();
          render();
        });
        planChips.appendChild(b);
      });
    }
    const planNote = $("#musicPlanNote");
    const planHint = $("#musicModeHint");
    if (planHint) planHint.textContent = "ABC Planning · 音乐生成前的乐谱规划";
    if (planNote) {
      if (isCover) {
        planNote.textContent =
          "参考生音乐固定用「仅旋律」谱面：SheetSage2 从参考音频只转出旋律线，不含和弦符号，"
          + "伴奏与和声由 YuE2 依据风格描述重新生成。这就是官方 cover 模板的做法。";
      } else {
        planNote.textContent = st.plan === "melody"
          ? "仅旋律：只规划旋律线，伴奏由 YuE2 自由生成 —— 适合想要更大编曲自由度的写法。"
          : "完整谱面：先规划「旋律 + 和弦」，再据此生成音乐 —— 文生音乐推荐这个。";
      }
    }

    // 步数 / CFG
    const steps = $("#musicSteps");
    if (steps) { steps.min = "1"; steps.max = "100"; steps.step = "1"; steps.value = st.steps; }
    const stepsVal = $("#musicStepsVal");
    if (stepsVal) stepsVal.textContent = st.steps;
    const cfg = $("#musicCfg");
    if (cfg) { cfg.min = "0"; cfg.max = "8"; cfg.step = "0.5"; cfg.value = st.cfg; }
    const cfgVal = $("#musicCfgVal");
    if (cfgVal) cfgVal.textContent = Number(st.cfg).toFixed(1);

    // 采样器 / 调度器
    fillSelect($("#musicSamplerSelect"), OPTIONS.samplers, st.sampler);
    fillSelect($("#musicSchedulerSelect"), OPTIONS.schedulers, st.scheduler);

    // 输出格式
    const fmtChips = $("#musicFormatChips");
    if (fmtChips) {
      fmtChips.innerHTML = "";
      OPTIONS.formats.forEach(fmt => {
        const b = document.createElement("button");
        b.type = "button";
        b.dataset.v = fmt;
        b.textContent = fmt.toUpperCase();
        b.classList.toggle("active", fmt === st.format);
        b.addEventListener("click", () => {
          st.format = fmt;
          saveState();
          render();
        });
        fmtChips.appendChild(b);
      });
    }

    // 高级采样参数
    const ms = OPTIONS.music_sampling;
    const temp = $("#musicTemp");
    if (temp) { temp.min = "0"; temp.max = "3"; temp.step = "0.05"; temp.value = st.temperature; }
    const tempVal = $("#musicTempVal");
    if (tempVal) tempVal.textContent = Number(st.temperature).toFixed(2);
    const topP = $("#musicTopP");
    if (topP) { topP.min = "0.05"; topP.max = "1"; topP.step = "0.01"; topP.value = st.top_p; }
    const topPVal = $("#musicTopPVal");
    if (topPVal) topPVal.textContent = Number(st.top_p).toFixed(2);
    const topK = $("#musicTopK");
    if (topK) { topK.min = "1"; topK.max = "500"; topK.step = "1"; topK.value = st.top_k; }
    const topKVal = $("#musicTopKVal");
    if (topKVal) topKVal.textContent = st.top_k;
    const rep = $("#musicRep");
    if (rep) { rep.min = "0.1"; rep.max = "2"; rep.step = "0.01"; rep.value = st.repetition_penalty; }
    const repVal = $("#musicRepVal");
    if (repVal) repVal.textContent = Number(st.repetition_penalty).toFixed(2);

    // 文本域：只在用户没在编辑时回填（避免打断输入）
    const promptBox = $("#musicPrompt");
    if (promptBox && document.activeElement !== promptBox && !promptBox.dataset.touched) {
      promptBox.value = st.prompt || "";
    }
    const lyricsBox = $("#musicLyrics");
    if (lyricsBox && document.activeElement !== lyricsBox && !lyricsBox.dataset.touched) {
      lyricsBox.value = st.lyrics || "";
    }
    const seedBox = $("#musicSeed");
    if (seedBox && document.activeElement !== seedBox) seedBox.value = st.seed || "";
  }

  function fillSelect(sel, values, current) {
    if (!sel) return;
    sel.innerHTML = "";
    (values || []).forEach(v => {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v;
      opt.selected = v === current;
      sel.appendChild(opt);
    });
    if (current && !(values || []).includes(current)) {
      const opt = document.createElement("option");
      opt.value = current;
      opt.textContent = current + "（自定义）";
      opt.selected = true;
      sel.appendChild(opt);
    }
  }

  function renderRefAudio() {
    const box = $("#musicRefBox");
    const note = $("#musicRefNote");
    if (!box) return;
    box.innerHTML = "";
    if (!refAudio) {
      const empty = document.createElement("div");
      empty.className = "img-note";
      empty.textContent = "尚未选择参考音频";
      box.appendChild(empty);
      if (note) note.textContent =
        "从素材库选 1 段音频（wav/mp3/flac/m4a/ogg）。这段音频只用来提取旋律线，"
        + "原曲的编曲和人声不会被沿用；音频越短，转谱越快。";
      return;
    }
    const card = document.createElement("div");
    card.className = "img-ref-card";
    const name = document.createElement("span");
    name.className = "tag";
    name.textContent = refAudio.name || refAudio.asset_id;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "img-ref-sort";
    remove.textContent = "移除";
    remove.addEventListener("click", () => {
      refAudio = null;
      renderRefAudio();
    });
    card.append(name, remove);
    box.appendChild(card);
    if (note) note.textContent = "已选：" + (refAudio.name || refAudio.asset_id)
      + "（仅提取旋律线；成曲时长由歌词段落与 BPM 决定）";
  }

  // ------------------------------------------------------- 结构化风格

  // 第三方 YuE2StylePrompt 的字段顺序（后端 /api/music-options 下发）
  const STYLE_FIELD_LABELS = {
    language: "语言",
    genre: "曲风",
    mood: "情绪",
    vocal: "人声",
    vocal_color: "人声音色",
    instruments: "乐器",
    tempo_bpm: "速度 BPM",
    energy: "能量",
    length_hint: "时长意向",
    extra: "补充描述",
    custom_language: "自定义语言",
    custom_genre: "自定义曲风",
  };
  // 纯器乐时人声音色要一起清成 (none)（第三方 yue2_schema.py:397-402）
  const INSTRUMENTAL = "(instrumental / no lead vocal)";
  // length_hint -> 英文描述（第三方 ComfyUI-YuE2/nodes.py:173）
  const STYLE_LENGTH_HINT_TEXT = {
    "model decides": "",
    "short clip (~45-75s)": "short song, about one minute",
    "single edit (~2-3 min)": "radio-length song, about two to three minutes",
    "full song (~3.5-5 min)": "full-length song, about four minutes",
    "very long (may truncate)": "long-form song with extended sections",
  };

  function styleValue(field) {
    const v = st.style_fields ? st.style_fields[field] : undefined;
    if (v === undefined || v === null) {
      const d = OPTIONS.style_defaults ? OPTIONS.style_defaults[field] : "";
      return d === undefined || d === null ? "" : d;
    }
    return v;
  }

  function setStyleValue(field, value) {
    if (!st.style_fields) st.style_fields = {};
    st.style_fields[field] = value;
  }

  function renderStyleFields() {
    const grid = $("#musicStyleFieldsGrid");
    if (!grid || !OPTIONS.style_fields || !OPTIONS.style_fields.length) return;
    grid.innerHTML = "";

    OPTIONS.style_fields.forEach(field => {
      const wrap = document.createElement("div");
      wrap.className = "music-sf-item";

      const label = document.createElement("div");
      label.className = "music-sf-label";
      label.textContent = STYLE_FIELD_LABELS[field] || field;
      wrap.appendChild(label);

      const enums = (OPTIONS.style_enums || {})[field];
      const isEnum = Array.isArray(enums) && enums.length > 0;

      if (isEnum) {
        const sel = document.createElement("select");
        sel.className = "img-select";
        sel.id = "musicSF_" + field;
        const cur = String(styleValue(field));
        let matched = false;
        enums.forEach(opt => {
          const o = document.createElement("option");
          o.value = opt;
          o.textContent = opt;
          if (opt === cur) { o.selected = true; matched = true; }
          sel.appendChild(o);
        });
        if (!matched) {
          const o = document.createElement("option");
          o.value = cur; o.textContent = cur; o.selected = true;
          sel.appendChild(o);
        }
        sel.addEventListener("change", e => {
          setStyleValue(field, e.target.value);
          if (field === "vocal" && e.target.value === INSTRUMENTAL) {
            // 纯器乐：人声音色一并清成 (none)，否则拼出 "warm instrumental"
            setStyleValue("vocal_color", "(none)");
          }
          if (field === "language" && e.target.value !== "(custom / none)") {
            setStyleValue("custom_language", "");
          }
          if (field === "genre" && e.target.value !== "(custom / none)") {
            setStyleValue("custom_genre", "");
          }
          saveState(); render();
        });
        wrap.appendChild(sel);
      } else {
        const inp = document.createElement("input");
        inp.type = (field === "tempo_bpm") ? "number" : "text";
        inp.className = "img-input";
        inp.id = "musicSF_" + field;
        inp.value = styleValue(field);
        if (field === "tempo_bpm") { inp.min = "0"; inp.max = "220"; }
        inp.addEventListener("input", e => {
          setStyleValue(field, field === "tempo_bpm"
            ? (Number(e.target.value) || 0) : e.target.value);
          saveState(); updateStylePreview();
        });
        wrap.appendChild(inp);
      }
      grid.appendChild(wrap);
    });

    updateStylePreview();
  }

  function updateStylePreview() {
    const box = $("#musicStyleFieldsPreview");
    if (!box) return;
    // 与后端 build_style_prompt 同序的预览（后端为最终准绳）
    const parts = [];
    const lang = styleValue("language");
    if (String(lang).startsWith("(custom")) {
      const c = String(styleValue("custom_language") || "").trim();
      if (c) parts.push(c);
    } else if (lang && !String(lang).startsWith("(")) parts.push(lang);
    const genre = styleValue("genre");
    if (String(genre).startsWith("(custom")) {
      const c = String(styleValue("custom_genre") || "").trim();
      if (c) parts.push(c);
    } else if (genre && !String(genre).startsWith("(")) parts.push(genre);
    const mood = styleValue("mood");
    if (mood && !String(mood).startsWith("(")) parts.push(String(mood));
    const vocal = styleValue("vocal");
    const vcolor = styleValue("vocal_color");
    if (vocal && !String(vocal).startsWith("(")) {
      parts.push((vcolor && !String(vcolor).startsWith("(") ? vcolor + " " : "") + String(vocal));
    } else if (vcolor && !String(vcolor).startsWith("(")) {
      parts.push(String(vcolor) + " instrumental");
    }
    const inst = String(styleValue("instruments") || "").trim();
    if (inst) parts.push(inst);
    const bpm = Number(styleValue("tempo_bpm") || 0);
    if (bpm > 0) parts.push(bpm + " BPM");
    const energy = styleValue("energy");
    if (energy && !String(energy).startsWith("(")) parts.push(String(energy));
    const hint = STYLE_LENGTH_HINT_TEXT[String(styleValue("length_hint"))];
    if (hint) parts.push(hint);
    const extra = String(styleValue("extra") || "").trim();
    if (extra) parts.push(extra);

    box.textContent = parts.length
      ? "拼接结果：" + parts.join(", ")
      : "（全留空则只用上面的自由文本风格描述）";
  }

  function toggleStyleFields() {
    const box = $("#musicStyleFields");
    const btn = $("#musicStyleFieldsToggle");
    if (!box) return;
    const show = box.style.display === "none" || !box.style.display;
    box.style.display = show ? "" : "none";
    if (btn) btn.textContent = show ? "收起" : "展开";
  }

  // ---------------------------------------------------------------- 提交

  function promptValue() {
    const box = $("#musicPrompt");
    return box ? box.value.trim() : "";
  }

  function collect() {
    const mode = currentMode();
    const isCover = mode === "a2a";
    const params = {
      prompt: promptValue(),
      lyrics: ($("#musicLyrics") || {}).value || "",
      // 参考生音乐：谱面来自 SheetSage2 转谱，固定「仅旋律」
      // （SheetSage2 不输出和弦符号，与 full 谱面不兼容 —— 后端也会强制纠正）
      mode: isCover ? "melody" : st.plan,
      // 谱面模式单独走 plan_mode：params.mode 是 HTTP 生成模式（t2a/a2a），
      // 两者语义不同，不能共用一个键（否则会被 server.py 覆盖）。
      plan_mode: isCover ? "melody" : st.plan,
      checkpoint: st.checkpoint,
      audio_encoder: st.audio_encoder,
      steps: st.steps,
      cfg: st.cfg,
      sampler: st.sampler,
      scheduler: st.scheduler,
      format: st.format,
      seed: (($("#musicSeed") || {}).value || "").trim() || null,
      music_temperature: st.temperature,
      music_top_p: st.top_p,
      music_top_k: st.top_k,
      music_repetition_penalty: st.repetition_penalty,
      lyrics_template: st.lyrics_template || OPTIONS.lyric_template_default,
    };
    // 结构化风格 12 字段（后端与自由文本合并）
    if (st.style_fields) {
      Object.keys(st.style_fields).forEach(k => {
        params["style_" + k] = st.style_fields[k];
      });
    }
    const assetRefs = [];
    if (mode === "a2a" && refAudio) {
      assetRefs.push({ asset_id: refAudio.asset_id, slot: "ref_audio", ordinal: 0 });
    }
    return { params, assetRefs, mode };
  }

  // ---------------------------------------------------------------- 事件绑定

  function bind() {
    const on = (sel, ev, fn) => {
      const el = $(sel);
      if (el) el.addEventListener(ev, fn);
    };

    on("#musicStyleFieldsToggle", "click", () => {
      toggleStyleFields();
    });
    on("#musicModelSelect", "change", e => {
      st.checkpoint = e.target.value; saveState(); render();
    });
    on("#musicEncoderSelect", "change", e => {
      st.audio_encoder = e.target.value; saveState(); render();
    });
    on("#musicSteps", "input", e => {
      st.steps = Number(e.target.value);
      const v = $("#musicStepsVal");
      if (v) v.textContent = st.steps;
      saveState();
    });
    on("#musicCfg", "input", e => {
      st.cfg = Number(e.target.value);
      const v = $("#musicCfgVal");
      if (v) v.textContent = Number(st.cfg).toFixed(1);
      saveState();
    });
    on("#musicSamplerSelect", "change", e => {
      st.sampler = e.target.value; saveState();
    });
    on("#musicSchedulerSelect", "change", e => {
      st.scheduler = e.target.value; saveState();
    });
    on("#musicTemp", "input", e => {
      st.temperature = Number(e.target.value);
      const v = $("#musicTempVal");
      if (v) v.textContent = Number(st.temperature).toFixed(2);
      saveState();
    });
    on("#musicTopP", "input", e => {
      st.top_p = Number(e.target.value);
      const v = $("#musicTopPVal");
      if (v) v.textContent = Number(st.top_p).toFixed(2);
      saveState();
    });
    on("#musicTopK", "input", e => {
      st.top_k = Number(e.target.value);
      const v = $("#musicTopKVal");
      if (v) v.textContent = st.top_k;
      saveState();
    });
    on("#musicRep", "input", e => {
      st.repetition_penalty = Number(e.target.value);
      const v = $("#musicRepVal");
      if (v) v.textContent = Number(st.repetition_penalty).toFixed(2);
      saveState();
    });

    // 文本域：记住内容 + 标记「用户动过」（之后不再自动回填）
    const promptBox = $("#musicPrompt");
    if (promptBox) {
      promptBox.addEventListener("input", () => {
        promptBox.dataset.touched = "1";
        st.prompt = promptBox.value;
        saveState();
      });
    }
    const lyricsBox = $("#musicLyrics");
    if (lyricsBox) {
      lyricsBox.addEventListener("input", () => {
        lyricsBox.dataset.touched = "1";
        st.lyrics = lyricsBox.value;
        saveState();
      });
    }
    const seedBox = $("#musicSeed");
    if (seedBox) {
      seedBox.addEventListener("input", () => {
        seedBox.dataset.touched = "1";
        st.seed = seedBox.value;
        saveState();
      });
    }

    // 参考音频选择
    on("#musicRefPick", "click", () => {
      if (!window.ASSETLIB || !window.ASSETLIB.openPicker) {
        alert("素材库模块未加载");
        return;
      }
      window.ASSETLIB.openPicker({
        categories: ["audio"], multiple: false, limit: 1,
        onDone: (ids) => {
          if (!ids || !ids.length) return;
          const assetId = ids[0];
          fetch(`/api/assets/${encodeURIComponent(assetId)}`)
            .then(r => r.json())
            .then(asset => {
              refAudio = { asset_id: asset.id, name: asset.name, category: asset.category };
              renderRefAudio();
            })
            .catch(err => alert("读取素材失败：" + err.message));
        },
      });
    });
  }

  // ---------------------------------------------------------------- 对外接口

  window.MusicUI = {
    render,
    collect,
    promptValue,
    isMusicMode,
    loadOptions,
    bind,
    getState: () => st,
    // 供 app.js 的 musicModelLabel 查 checkpoint key -> 显示名
    getOptions: () => OPTIONS,
    setMode(mode) { st.mode = mode; saveState(); },

    // ---- 增强侧（web/music-enhance.js 调用）----
    /** 结构化风格的 12 个字段（增强产物；用户可逐项编辑） */
    getStyleFields: () => ({ ...(st.style_fields || {}) }),
    /** 回填结构化风格（增强产物）。填完自动展开折叠区，让用户看得见。 */
    setStyleFields: (fields) => {
      st.style_fields = (fields && typeof fields === "object") ? { ...fields } : {};
      saveState();
      const box = $("#musicStyleFields");
      if (box) box.style.display = "";
      const btn = $("#musicStyleFieldsToggle");
      if (btn) btn.textContent = "收起";
      renderStyleFields();
    },
    /** 歌词正文 */
    getLyrics: () => {
      const box = $("#musicLyrics");
      return box ? box.value : (st.lyrics || "");
    },
    /** 回填歌词正文 */
    setLyrics: (text) => {
      st.lyrics = text || "";
      const box = $("#musicLyrics");
      if (box) box.value = st.lyrics;
      saveState();
    },
    /** 提示栏（增强状态提示） */
    setHint: (text, kind) => {
      const hint = $("#musicEnhanceHint");
      if (!hint) return;
      hint.textContent = text;
      hint.className = "enhance-hint" + (kind ? " " + kind : "");
    },
    /** busy 态（禁用增强按钮） */
    setBusy: (busy) => {
      const btn = $("#musicEnhanceBtn");
      if (btn) btn.disabled = !!busy;
      const caret = $("#musicEnhanceMenuBtn");
      if (caret) caret.disabled = !!busy;
    },
    /** 标记「已增强」徽标 */
    markEnhanced: () => {
      const badge = $("#musicPromptBadge");
      if (badge) badge.hidden = false;
    },
  };
})();
