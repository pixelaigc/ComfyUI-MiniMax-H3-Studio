/* H3 Studio 音乐面板（YuE2）：文生音乐 / 参考生音乐
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
    default_checkpoint: "yue2_3b_int8_convrot",
    default_audio_encoder: "sheetsage2_bf16",
    sampler: "dpm_2",
    scheduler: "sgm_uniform",
    steps: 32,
    cfg: 1.0,
    samplers: [],
    schedulers: [],
    modes: ["full", "melody"],
    music_sampling: { temperature: 1.0, top_p: 0.95, top_k: 100, repetition_penalty: 1.2 },
    max_duration: 360,
    min_duration: 4,
    default_duration: 60,
    formats: ["flac", "mp3", "opus"],
    default_format: "flac",
    loaded: false,
  };

  // 参数持久化（记住上次设置）—— localStorage 不可用时静默降级
  const STORE_KEY = "h3studio.musicParams";
  const DEFAULT_STATE = {
    mode: "t2a",
    checkpoint: "",
    audio_encoder: "",
    plan: "full",
    duration: 60,
    steps: 32,
    cfg: 1.0,
    sampler: "dpm_2",
    scheduler: "sgm_uniform",
    format: "flac",
    style: "",
    lyrics: "",
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
      OPTIONS.max_duration = data.max_duration || OPTIONS.max_duration;
      OPTIONS.min_duration = data.min_duration || OPTIONS.min_duration;
      OPTIONS.default_duration = data.default_duration || OPTIONS.default_duration;
      OPTIONS.formats = data.formats || ["flac", "mp3"];
      OPTIONS.default_format = data.default_format || OPTIONS.default_format;
      OPTIONS.loaded = true;

      // 首次进入时把默认值对齐服务端推荐
      if (!st.checkpoint) st.checkpoint = OPTIONS.default_checkpoint;
      if (!st.audio_encoder) st.audio_encoder = OPTIONS.default_audio_encoder;
      if (!st.sampler) st.sampler = OPTIONS.sampler;
      if (!st.scheduler) st.scheduler = OPTIONS.scheduler;
      if (!st.duration) st.duration = OPTIONS.default_duration;
      if (!st.format) st.format = OPTIONS.default_format;
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

    // 生成模式 chips
    $$("#musicModeChips button").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.v === mode);
    });
    const isCover = mode === "a2a";
    const modeNote = $("#musicModeNote");
    if (modeNote) {
      modeNote.textContent = isCover
        ? "参考生音乐（翻唱）：参考音频只提供旋律线，YuE2 按下面的风格描述与歌词重新编曲、重新演唱；"
          + "原曲的和弦与编曲不会被沿用。参考音频需先从素材库选择。"
        : "文生音乐：YuE2 先按风格描述与歌词规划一份乐谱（ABC），再据此生成带人声与伴奏的完整歌曲。";
    }

    // 模型
    const modelSel = $("#musicModelSelect");
    if (modelSel) {
      modelSel.innerHTML = "";
      Object.entries(OPTIONS.checkpoints).forEach(([key, m]) => {
        const opt = document.createElement("option");
        opt.value = key;
        const unavailable = m.available === false;
        opt.textContent = unavailable ? m.label + "（权重缺失，不可用）" : m.label;
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
        opt.textContent = unavailable ? m.label + "（权重缺失，不可用）" : m.label;
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

    // 时长
    const dur = $("#musicDuration");
    if (dur) {
      dur.min = String(Math.max(10, Math.ceil(OPTIONS.min_duration)));
      dur.max = String(OPTIONS.max_duration);
      dur.step = "10";
      dur.value = st.duration;
    }
    const durVal = $("#musicDurationVal");
    if (durVal) durVal.textContent = st.duration + "s";

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
    const styleBox = $("#musicStyle");
    if (styleBox && document.activeElement !== styleBox && !styleBox.dataset.touched) {
      styleBox.value = st.style || "";
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
      + "（仅提取旋律线，成曲时长另受上方「最长时长」限制）";
  }

  // ---------------------------------------------------------------- 提交

  function promptValue() {
    const box = $("#musicStyle");
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
      checkpoint: st.checkpoint,
      audio_encoder: st.audio_encoder,
      max_duration: st.duration,
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
    };
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

    on("#musicModelSelect", "change", e => {
      st.checkpoint = e.target.value; saveState(); render();
    });
    on("#musicEncoderSelect", "change", e => {
      st.audio_encoder = e.target.value; saveState(); render();
    });
    on("#musicDuration", "input", e => {
      st.duration = Number(e.target.value);
      const v = $("#musicDurationVal");
      if (v) v.textContent = st.duration + "s";
      saveState();
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
    const styleBox = $("#musicStyle");
    if (styleBox) {
      styleBox.addEventListener("input", () => {
        styleBox.dataset.touched = "1";
        st.style = styleBox.value;
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

    // 生成模式切换（面板内的两个 chip）
    $$("#musicModeChips button").forEach(btn => {
      btn.addEventListener("click", () => {
        const mode = btn.dataset.v;
        st.mode = mode;
        saveState();
        // 交给 app.js 的模式切换入口，保持导航高亮 + 面板一致
        const navEl = document.querySelector(`.nav-item[data-mode="${mode}"]`);
        if (navEl) navEl.click();
        else render();
      });
    });

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
  };
})();
