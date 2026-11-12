/* ============================================================
   文生视频（MiniMax-H3）参数面板
   ------------------------------------------------------------
   与「图像」面板的关系：结构完全一致 —— 清单由服务端下发
   （/api/video-options），前端只渲染 + 提交，不硬编码任何文件名。

   本模块负责：
     * 尺寸预设下拉（服务端下发合法组合）+ 宽高直填 + 权威吸附
       （显示「输入值 → 实际使用值」+ 原因）
     * 模型选择（本机实测可用清单）
     * 加速 LoRA **下拉单选**（一个主模型只能挑一个加速 LoRA），
       且**按当前主模型做适配过滤**（不适配的根本不出现）
     * 注意力机制（本机 ComfyUI 里真有的节点才可选）
     * 二采（SelfLift）开关与参数
     * 配置持久化（/api/video-config，局域网共用，与图像面板同一套机制）

   【2026-11-11 改造】
     1. 尺寸预设下拉 —— 之前只能手填宽高
     2. 加速 LoRA 多选列表 -> 下拉单选
     3. 加速 LoRA 按主模型适配过滤（服务端实测矩阵）
     4. 去掉生成按钮下方重复的 LORA 强度控件（见 web/index.html）
   ============================================================ */
(function () {
  "use strict";

  /** 视频面板的可选项清单（服务端下发） */
  const VIDEO_OPTIONS = {
    models: {},
    default_model: "",
    fixed_accel_loras: [],
    accel_lora_combos: [],
    default_fixed_accel_lora: "",
    attention_modes: [],
    default_attention: "",
    twopass: {},
    aspects: [],
    size_multiple: 32,
    size_min: 32,
    size_max: 16384,
    mp_table: [],
    default_mp: 0.4,
    // 【2026-11-11 放宽】文生视频上限 2.0M 像素（服务端下发，别在前端写死）
    max_pixels: 2 * 1024 * 1024,
    max_pixels_mp: 2,
    // 各模式各自的尺寸策略（不一刀切套用 2.0M）
    mode_size_policy: {},
    size_presets: [],        // 服务端下发的合法尺寸预设（含宽高）
    accel_lora_single_select: true,
    accel_lora_default: "",
    accel_lora_compat: {},   // {model_key: {lora_key: {compatible, reason, ...}}}
  };

  /** 视频面板状态（与图像面板的 state.img 同级，互不污染） */
  const videoState = {
    model: "",
    aspect: "16:9",
    megapixels: 0.4,
    custom: false,          // 用户手填过宽高
    width: null,
    height: null,
    rawWidth: null,         // 用户原始输入（未经吸附）
    rawHeight: null,
    sizePresetKey: "",      // 当前选中的尺寸预设（空 = 自定义）
    fixedAccelLora: "",
    accelLora: "",          // 【单选】当前选中的加速 LoRA key（空 = 不额外挂）
    accelLoraStrength: 1.0, // 【单选】该加速 LoRA 的强度
    attention: "",
    twopass: {
      enabled: false,
      transition_step: 3,
      lowres_scale: 0.5,
      rho: 0.0,
      w_min: 0.5,
      w_max: 1.0,
      upscaler_model: "",
      cfg: 1.0,          // 【2026-11-11】二采专用 CFG（SelfLiftH3Sampler.cfg）
    },
    // ---- 采样参数（与生图面板同级）----
    // 取值全部来自服务端下发的 sampling 清单（/api/video-options），
    // 前端不硬编码采样器/调度器名单 —— 那份清单来自本机 ComfyUI 的
    // /object_info（KSamplerSelect 68 项 / BasicScheduler 14 项）。
    steps: 4,
    sampler: "",
    scheduler: "",
    shiftVideo: 12.0,
    shiftAudio: 3.0,
  };

  const ASPECT_NUM = {
    "1:1": [1, 1], "2:3": [2, 3], "3:2": [3, 2], "3:4": [3, 4],
    "4:3": [4, 3], "9:16": [9, 16], "16:9": [16, 9], "21:9": [21, 9],
  };

  let sizeTyping = false;      // 用户正在输入（不要覆盖输入框）
  let optionsReady = false;
  let saveTimer = null;

  // ------------------------------------------------------------ 工具

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g,
      c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function currentModel() {
    return VIDEO_OPTIONS.models[videoState.model] || null;
  }

  function fixedAccelMeta() {
    return VIDEO_OPTIONS.fixed_accel_loras.find(
      l => l.key === videoState.fixedAccelLora) || null;
  }

  function accelComboMeta(key) {
    return VIDEO_OPTIONS.accel_lora_combos.find(l => l.key === key) || null;
  }

  // ------------------------------------------------------------ 尺寸

  /** 当前模式的上限（像素），用于提示文案。 */
  function maxPixels() {
    const policy = (VIDEO_OPTIONS.mode_size_policy || {})[VIDEO_OPTIONS.mode || "t2v"];
    if (policy && policy.max_pixels) return policy.max_pixels;
    return VIDEO_OPTIONS.max_pixels || 2 * 1024 * 1024;
  }

  function formatMaxPixels() {
    const px = maxPixels();
    return `${(px / (1024 * 1024)).toFixed(1)}M 像素（${px.toLocaleString("en-US")} px）`;
  }

  /** 比例 + 总像素 -> 像素宽高（与 ComfyUI 的 ResolutionSelector 同一算法） */
  function autoSize(aspect, megapixels) {
    const [wr, hr] = ASPECT_NUM[aspect] || [16, 9];
    const mult = VIDEO_OPTIONS.size_multiple || 32;
    const total = (megapixels || 0.4) * 1024 * 1024;
    const scale = Math.sqrt(total / (wr * hr));
    let w = Math.max(mult, Math.round(wr * scale / mult) * mult);
    let h = Math.max(mult, Math.round(hr * scale / mult) * mult);
    return [w, h];
  }

  /**
   * 宽高吸附 —— **服务端权威**。
   *
   * 前端不复制吸附算法（同一常量写两处必然走偏，这是本项目反复踩过的坑）：
   * 直接把用户输入发给 /api/video-size，由后端算好「生效值 + 原因」，
   * 前端只负责显示。
   */
  async function snapSize(rawW, rawH) {
    try {
      // 【2026-11-12 修 —— 只改一维时另一维变 null】
      // 旧实现把 null 原样发给服务端 -> 服务端 float(None) 失败 ->
      // 返回「宽高必须是数字」-> applySize 走 width=0 分支 ->
      // **用户只改了宽度，高度却被静默清空**，两个框都归零。
      // 现在：某一维没有有效输入时，用**当前生效值**补上，
      // 保证请求里永远是两个合法数字（另一维保持不变）。
      const [curW, curH] = currentSize();
      const w = (rawW != null && rawW > 0) ? rawW : curW;
      const h = (rawH != null && rawH > 0) ? rawH : curH;
      // 【2026-11-11】带上 mode —— 服务端按各模式自己的尺寸策略算
      // （t2v 放宽到 2.0M；flf / r2v 仍是 0.8MP 档位）。
      const mode = encodeURIComponent(VIDEO_OPTIONS.mode || "t2v");
      const resp = await fetch(
        `/api/video-size?width=${encodeURIComponent(w)}&height=${encodeURIComponent(h)}&mode=${mode}`,
        { cache: "no-store" });
      if (!resp.ok) return null;
      return await resp.json();
    } catch (e) {
      return null;
    }
  }

  function currentSize() {
    if (videoState.width && videoState.height) {
      return [videoState.width, videoState.height];
    }
    return autoSize(videoState.aspect, videoState.megapixels);
  }

  /** 当前比例下的合法尺寸预设（服务端下发的清单里筛） */
  function presetsForAspect(aspect) {
    return (VIDEO_OPTIONS.size_presets || []).filter(p => p.aspect === aspect);
  }

  /**
   * 渲染尺寸预设下拉。
   *
   * 清单来自服务端（/api/video-options 的 size_presets），每一条都已按
   * H3 原生画布的硬约束（面积 <= 768*1344、短边 <= 768、32 的倍数）裁过 ——
   * 下拉里出现的都是真能跑的组合。
   */
  function renderSizePresetSelect() {
    const sel = document.getElementById("vidSizePresetSelect");
    if (!sel) return;
    const presets = presetsForAspect(videoState.aspect);
    const prev = sel.value;
    sel.innerHTML = "";

    // 「自定义」项：用户手填宽高后这里要能反映出来
    const customOpt = document.createElement("option");
    customOpt.value = "";
    customOpt.textContent = "自定义（手动填宽高）";
    sel.appendChild(customOpt);

    presets.forEach(p => {
      const opt = document.createElement("option");
      opt.value = p.key;
      opt.textContent = p.label;
      opt.title = `${p.width}×${p.height} · ${p.megapixels}MP`;
      sel.appendChild(opt);
    });

    // 回显：优先用 state 里记的 preset key；没有就按当前宽高反查
    let want = videoState.sizePresetKey;
    if (!want || !presets.some(p => p.key === want)) {
      const [w, h] = currentSize();
      const hit = presets.find(p => p.width === w && p.height === h);
      want = hit ? hit.key : "";
    }
    sel.value = want;
    videoState.sizePresetKey = want;

    const note = document.getElementById("vidSizePresetNote");
    if (note) {
      if (!want) {
        note.textContent = "当前是手填尺寸。选一个预设可一键回到 H3 原生画布内的标准分辨率。";
      } else {
        const p = presets.find(x => x.key === want);
        note.textContent = p
          ? `官方档位 ${p.megapixels}MP · ${p.width}×${p.height}`
            + `（上限 ${formatMaxPixels()}，本项在限内）`
          : "";
      }
    }
  }

  function renderSizeFields() {
    const [w, h] = currentSize();
    if (!sizeTyping) {
      const wi = document.getElementById("vidWidth");
      const hi = document.getElementById("vidHeight");
      if (wi) wi.value = w;
      if (hi) hi.value = h;
    }
    const hint = document.getElementById("vidSizeHint");
    if (hint) hint.textContent = `${w}×${h}`;
    const mp = (w * h) / (1024 * 1024);
    const note = document.getElementById("vidSizeNote");
    if (note) {
      const parts = [`${mp.toFixed(2)} MP`];
      // 【2026-11-11】把上限显式写出来 —— 用户看得见天花板在哪。
      parts.push(`上限 ${formatMaxPixels()}`);
      if (videoState.custom) parts.push("自定义尺寸，点「自动」可恢复按比例计算的尺寸");
      note.textContent = parts.join(" · ");
    }
    const modeTag = document.getElementById("vidSizeMode");
    if (modeTag) {
      modeTag.classList.toggle("hidden", !videoState.custom);
      modeTag.textContent = videoState.custom ? "自定义" : "";
    }
    // 吸附说明（原因由服务端给出）
    const adj = document.getElementById("vidSizeAdjust");
    const reasons = videoState.sizeReasons || [];
    if (adj) {
      if (reasons.length) {
        adj.hidden = false;
        adj.textContent = "已自动调整：" + reasons.join("；");
      } else {
        adj.hidden = true;
        adj.textContent = "";
      }
    }
  }

  async function applySize(rawW, rawH) {
    const snapped = await snapSize(rawW, rawH);
    if (!snapped) return;
    videoState.sizeReasons = snapped.reasons || [];
    if (!snapped.width || !snapped.height) {
      // 【2026-11-11】服务端判定超限（strict）时返回 width=0 + 原因。
      // 这里把原因原样显示，**不静默吞掉**（需求：超限要给出明确提示）。
      videoState.width = null;
      videoState.height = null;
      videoState.sizePresetKey = "";
      renderSizePresetSelect();
      renderSizeFields();
      return;
    }
    videoState.width = snapped.width;
    videoState.height = snapped.height;
    // 手填之后就不再是任何预设 —— 预设下拉要切到「自定义」
    const presets = presetsForAspect(videoState.aspect);
    const hit = presets.find(p => p.width === snapped.width && p.height === snapped.height);
    videoState.sizePresetKey = hit ? hit.key : "";
    renderSizePresetSelect();
    renderSizeFields();
  }

  // ------------------------------------------------------------ 模型
  //
  // 【2026-11-11 改造】下拉的每一项都由**服务端下发的数据**驱动：
  //   * 能不能选 —— 服务端用 ComfyUI 的模型列表 API 判定的 status
  //     （available / external_missing / missing / unknown）；
  //   * 显示什么 —— 服务端算好的 status_note（如实说明，不是写死常量）。
  //
  // 原来这里只认 `m.available`，且红字取自静态的 `m.warn` —— 那句
  // 「盘未挂载时 ComfyUI 下拉里根本没有这个名字」是**写死的常量**，
  // 在盘已挂载、ComfyUI 已收录权重的情况下依然整句显示，
  // 让用户误以为「获取不到模型」（用户 2026-11-11 报的缺陷）。

  /** 模型项在下拉里的显示文案：不可用时追加**如实**的状态后缀。 */
  function modelOptionLabel(m) {
    const status = m.status || (m.available === false ? "missing" : "available");
    if (status === "available") return m.label;
    if (status === "external_missing") return m.label + "（外置未挂载，当前不可选）";
    if (status === "unknown") return m.label + "（未能确认，可用性未知）";
    return m.label + "（权重缺失，不可用）";
  }

  function renderModelSelect() {
    const sel = document.getElementById("vidModelSelect");
    if (!sel) return;
    sel.innerHTML = "";
    Object.entries(VIDEO_OPTIONS.models).forEach(([key, m]) => {
      const opt = document.createElement("option");
      opt.value = key;
      const status = m.status || (m.available === false ? "missing" : "available");
      const unavailable = status !== "available";
      opt.textContent = modelOptionLabel(m);
      opt.disabled = unavailable;
      opt.selected = key === videoState.model;
      // title 里带上**真实**的权重文件名与状态，便于排查
      opt.title = `${m.unet || ""}${m.status_note ? "\n" + m.status_note : ""}`;
      sel.appendChild(opt);
    });
    // 当前选中的模型不可用 -> 回落到**第一个可用**的模型
    const model = currentModel();
    const curStatus = model
      ? (model.status || (model.available === false ? "missing" : "available"))
      : "missing";
    if (!model || curStatus !== "available") {
      const fallbackKey = Object.keys(VIDEO_OPTIONS.models).find(k => {
        const m = VIDEO_OPTIONS.models[k];
        const st = m.status || (m.available === false ? "missing" : "available");
        return st === "available";
      }) || VIDEO_OPTIONS.default_model;
      if (fallbackKey && VIDEO_OPTIONS.models[fallbackKey]) {
        videoState.model = fallbackKey;
        sel.value = videoState.model;
      }
    }
    renderModelNote();
  }

  function renderModelNote() {
    const model = currentModel();
    const note = document.getElementById("vidModelNote");
    const warn = document.getElementById("vidModelWarn");
    if (!model) {
      if (note) note.textContent = "";
      // 【2026-11-11】没有内容时**把元素藏掉**：
      // .img-warn 自带橙色背景 + padding + 边框，空内容也会渲染成
      // 一条橙色的空条（用户 21:06 实报的「模型框下面挂着橙色告警条」）。
      if (warn) { warn.textContent = ""; warn.hidden = true; }
      return;
    }
    const status = model.status
      || (model.available === false ? "missing" : "available");
    if (note) {
      const bits = [model.note || ""];
      if (model.size_mb) bits.push(`本机体积 ${(model.size_mb / 1024).toFixed(1)} GB`);
      if (model.location) bits.push(`位置 ${model.location}`);
      note.textContent = bits.filter(Boolean).join(" · ");
    }
    // 红字区域：**只显示当前真实状态**。
    //   - status_note 是服务端按 ComfyUI 模型列表现算的，**不谎称**「获取不到模型」；
    //   - 静态 warn 只保留与可用性无关的固有提醒（显存 / 兼容性）。
    // 合并说明：panel-fallback 分支在此处额外要求「空内容必须把元素藏掉」
    //（.img-warn 自带橙底，空内容也会渲染成一条空橙条），
    // 故保留 hidden 逻辑 —— 两条规则同时满足。
    if (warn) {
      const parts = [];
      if (status !== "available" && model.status_note) {
        parts.push(model.status_note);
      } else if (status === "available" && model.comfy_listed === true) {
        parts.push("已确认在 ComfyUI 模型列表中可用。");
      }
      if (model.warn) parts.push(model.warn);
      warn.textContent = parts.join(" ");
      warn.hidden = !warn.textContent;
    }
  }

  /**
   * 模型清单的来源说明（显示在模型区顶部）。
   * 「下拉里为什么少了一个模型」永远有答案：
   *   - 正常：清单来自 ComfyUI 的模型列表 API（带条目数）
   *   - 降级：连不上 ComfyUI，如实说明当前用的是磁盘扫描 + 原因
   */
  function renderModelSourceNote() {
    const box = document.getElementById("vidModelSourceNote");
    if (!box) return;
    const src = VIDEO_OPTIONS.model_list_source;
    const counts = VIDEO_OPTIONS.model_list_counts || {};
    if (src === "comfyui_api") {
      const n = counts.diffusion_models || 0;
      const l = counts.loras || 0;
      box.textContent = `清单来源：ComfyUI 模型列表 API（${n} 个扩散模型 / ${l} 个 LoRA）`;
      box.className = "img-note";
      box.hidden = false;
      return;
    }
    const err = VIDEO_OPTIONS.model_list_error || "未知原因";
    // 原因可能很长（三个接口各一条）—— 界面上只留第一条 + 截断，
    // 完整原因在 /api/video-options 的 model_list_error 里。
    const short = err.split("；")[0].slice(0, 160);
    box.textContent = `未能连上 ComfyUI 模型列表（${short}）—— 当前清单来自本机磁盘扫描，可用性可能与 ComfyUI 下拉框不一致。`;
    box.className = "img-warn";
    box.hidden = false;
  }

  // ------------------------------------------------------------ 加速 LoRA

  function renderFixedLoraNote() {
    const box = document.getElementById("vidFixedLoraNote");
    if (!box) return;
    const meta = fixedAccelMeta();
    if (!meta) {
      box.hidden = true;
      box.textContent = "";
      return;
    }
    box.hidden = false;
    box.textContent = `工作流固定使用加速 LoRA「${meta.display}」`
      + `（${meta.purpose}），不随上面的选择变化。`;
  }

  /**
   * 当前主模型下**可选**的加速 LoRA 清单。
   *
   * 过滤判据（全部来自服务端实测，见 h3studio/video_models.py）：
   *   * 文件在本机（available）
   *   * 与当前底模的层结构对得上（compatible —— 逐层比对权重头）
   * 不满足的一律**不出现**在下拉里（不是置灰 —— 置灰等于「能选但会
   * 被服务端改写」，那是假选项）。
   */
  function selectableAccelLoras() {
    const combos = VIDEO_OPTIONS.accel_lora_combos || [];
    return combos.filter(item => {
      if (item.available === false) return false;
      if (item.selectable === false) return false;
      if (item.compatible === false) return false;
      return true;
    });
  }

  function renderAccelLoraSelect() {
    const sel = document.getElementById("vidAccelLoraSelect");
    if (!sel) return;
    const combos = VIDEO_OPTIONS.accel_lora_combos || [];
    const selectable = selectableAccelLoras();

    sel.innerHTML = "";
    // 「不额外挂加速 LoRA」—— 工作流里已经有固定的那枚 Turbo LoRA
    const noneOpt = document.createElement("option");
    noneOpt.value = "";
    noneOpt.textContent = "不额外挂加速 LoRA（仅用工作流固定的 Turbo）";
    sel.appendChild(noneOpt);

    selectable.forEach(item => {
      const opt = document.createElement("option");
      opt.value = item.key;
      opt.textContent = item.display;
      opt.title = `${item.name}｜推荐强度 ${item.recommended_strength}`;
      sel.appendChild(opt);
    });

    // 当前选中的不再适配（切了主模型）-> 自动回落成「不额外挂」
    if (videoState.accelLora && !selectable.some(x => x.key === videoState.accelLora)) {
      videoState.accelLora = "";
    }
    sel.value = videoState.accelLora;

    renderAccelLoraNote();
    renderAccelLoraStrength();
  }

  function renderAccelLoraNote() {
    const note = document.getElementById("vidAccelLoraNote");
    if (!note) return;
    const combos = VIDEO_OPTIONS.accel_lora_combos || [];
    const selectable = selectableAccelLoras();
    const parts = [];

    const meta = videoState.accelLora ? accelComboMeta(videoState.accelLora) : null;
    if (meta) {
      parts.push(`已选「${meta.display}」×${Number(videoState.accelLoraStrength).toFixed(2)}`);
      if (meta.note) parts.push(meta.note);
    } else {
      parts.push("未额外挂加速 LoRA（工作流固定的 Turbo LoRA 仍然生效）。");
    }
    // 被过滤掉的数量 + 原因（说清楚「为什么少了几项」）
    const blocked = combos.length - selectable.length;
    if (blocked > 0) {
      const reasons = combos
        .filter(c => !selectable.some(s => s.key === c.key))
        .map(c => {
          if (c.available === false) return `${c.display}（本机缺少文件）`;
          return `${c.display}（${c.compat_reason || "与当前底模不兼容"}）`;
        });
      parts.push(`当前底模下另有 ${blocked} 个加速 LoRA 不适配，已从列表剔除：`
        + reasons.join("；"));
    }
    note.textContent = parts.join(" ");
  }

  function renderAccelLoraStrength() {
    const slider = document.getElementById("vidAccelLoraStrength");
    const label = document.getElementById("vidAccelLoraStrengthVal");
    const field = document.getElementById("vidAccelLoraStrengthField");
    const has = !!videoState.accelLora;
    if (field) field.style.display = has ? "" : "none";
    if (slider) {
      slider.value = videoState.accelLoraStrength;
      slider.disabled = !has;
    }
    if (label) label.textContent = Number(videoState.accelLoraStrength).toFixed(2);
  }

  // ------------------------------------------------------------ 注意力

  /**
   * 注意力机制下拉。
   *
   * 【2026-11-12 两处调整】
   *   1. **速度标注** —— 每项后面带「速度最快 / 速度很快 / 速度一般 /
   *      速度较慢 / 速度最慢」。标注来自服务端下发的 `speed_label`
   *      （与 video_models.VIDEO_ATTENTION_MODES 同源），前端不自己编。
   *   2. **选项已下线的不出现** —— 「Sol-Attn 渐进调度」（sol_scheduled）
   *      已从服务端清单里删掉（用户实测画面有叠影），所以这里根本
   *      不会渲染它。**不是置灰** —— 置灰等于「能选但会被服务端改写」，
   *      那是假选项。
   */
  function attentionOptionLabel(item) {
    const base = item.label || item.key;
    const speed = item.speed_label ? ` · ${item.speed_label}` : "";
    const unavailable = item.available === false;
    return unavailable ? base + "（本机无此节点，不可用）" + speed : base + speed;
  }

  function renderAttention() {
    const sel = document.getElementById("vidAttentionSelect");
    const note = document.getElementById("vidAttentionNote");
    if (!sel) return;
    const modes = VIDEO_OPTIONS.attention_modes;
    sel.innerHTML = "";
    modes.forEach(item => {
      const opt = document.createElement("option");
      opt.value = item.key;
      const unavailable = item.available === false;
      opt.textContent = attentionOptionLabel(item);
      opt.disabled = unavailable;
      opt.title = item.note || "";
      opt.selected = item.key === videoState.attention;
      sel.appendChild(opt);
    });
    const current = modes.find(m => m.key === videoState.attention);
    if (!current || current.available === false) {
      const fallback = modes.find(m => m.key === VIDEO_OPTIONS.default_attention
        && m.available !== false) || modes.find(m => m.available !== false);
      if (fallback) {
        videoState.attention = fallback.key;
        sel.value = fallback.key;
      }
    }
    const active = modes.find(m => m.key === videoState.attention);
    if (note) note.textContent = active ? (active.note || "") : "";
  }

  // ------------------------------------------------------------ 采样参数

  /**
   * 采样参数区（步数 / 采样器 / 调度器 / shift / CFG 只读说明）。
   *
   * 与生图面板同一套交互：滑块 + 下拉 + 「推荐 / 已改」提示。
   * 所有候选值都来自服务端下发的 `sampling` 清单 ——
   * 前端不硬编码采样器/调度器名单（那份清单实测自本机 ComfyUI 的
   * /object_info：KSamplerSelect 68 项、BasicScheduler 14 项）。
   */
  function samplingCfg() {
    return VIDEO_OPTIONS.sampling || {};
  }

  function renderSampling() {
    const cfg = samplingCfg();
    const model = currentModel();

    // ---- 步数 ----
    const stepsEl = document.getElementById("vidSteps");
    const stepsVal = document.getElementById("vidStepsVal");
    const stepsHint = document.getElementById("vidStepsHint");
    if (stepsEl) {
      const min = cfg.steps_min || 1;
      const max = cfg.steps_max || 50;
      stepsEl.min = String(min);
      stepsEl.max = String(max);
      stepsEl.value = videoState.steps;
    }
    if (stepsVal) stepsVal.textContent = videoState.steps;
    if (stepsHint) {
      const recommended = model && model.steps != null ? Number(model.steps) : null;
      stepsHint.textContent = recommended == null
        ? "—"
        : (Number(videoState.steps) === recommended ? "推荐" : "已改");
    }

    // ---- 采样器 ----
    const samplerSel = document.getElementById("vidSamplerSelect");
    const samplerHint = document.getElementById("vidSamplerHint");
    if (samplerSel) {
      const list = cfg.samplers || [];
      samplerSel.innerHTML = "";
      list.forEach(name => {
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        opt.selected = name === videoState.sampler;
        samplerSel.appendChild(opt);
      });
      if (!list.includes(videoState.sampler)) {
        const opt = document.createElement("option");
        opt.value = videoState.sampler || "";
        opt.textContent = videoState.sampler || "—";
        samplerSel.appendChild(opt);
      }
      samplerSel.value = videoState.sampler;
    }
    if (samplerHint) {
      const recommended = model && model.sampler ? model.sampler : null;
      samplerHint.textContent = recommended == null
        ? "—"
        : (videoState.sampler === recommended ? "推荐" : "已改");
    }

    // ---- 调度器 ----
    const schedSel = document.getElementById("vidSchedulerSelect");
    const schedHint = document.getElementById("vidSchedulerHint");
    if (schedSel) {
      const list = cfg.schedulers || [];
      schedSel.innerHTML = "";
      list.forEach(name => {
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        opt.selected = name === videoState.scheduler;
        schedSel.appendChild(opt);
      });
      if (!list.includes(videoState.scheduler)) {
        const opt = document.createElement("option");
        opt.value = videoState.scheduler || "";
        opt.textContent = videoState.scheduler || "—";
        schedSel.appendChild(opt);
      }
      schedSel.value = videoState.scheduler;
    }
    if (schedHint) {
      const recommended = model && model.scheduler ? model.scheduler : null;
      schedHint.textContent = recommended == null
        ? "—"
        : (videoState.scheduler === recommended ? "推荐" : "已改");
    }

    // ---- shift（视频 / 音频）----
    const svEl = document.getElementById("vidShiftVideo");
    const saEl = document.getElementById("vidShiftAudio");
    const svVal = document.getElementById("vidShiftVideoVal");
    const saVal = document.getElementById("vidShiftAudioVal");
    const svHint = document.getElementById("vidShiftVideoHint");
    const saHint = document.getElementById("vidShiftAudioHint");
    const shiftMin = cfg.shift_min != null ? cfg.shift_min : 0.01;
    const shiftMax = cfg.shift_max != null ? cfg.shift_max : 100;
    if (svEl) {
      svEl.min = String(shiftMin);
      svEl.max = String(shiftMax);
      svEl.value = videoState.shiftVideo;
    }
    if (saEl) {
      saEl.min = String(shiftMin);
      saEl.max = String(shiftMax);
      saEl.value = videoState.shiftAudio;
    }
    if (svVal) svVal.textContent = Number(videoState.shiftVideo).toFixed(2);
    if (saVal) saVal.textContent = Number(videoState.shiftAudio).toFixed(2);
    const defVideo = cfg.shift_default_video != null ? cfg.shift_default_video : 12.0;
    const defAudio = cfg.shift_default_audio != null ? cfg.shift_default_audio : 3.0;
    if (svHint) {
      svHint.textContent = Number(videoState.shiftVideo) === Number(defVideo)
        ? "推荐" : "已改";
    }
    if (saHint) {
      saHint.textContent = Number(videoState.shiftAudio) === Number(defAudio)
        ? "推荐" : "已改";
    }

    // 【2026-11-12】shift 节点本机不可用时 —— 滑杆必须**禁用**，
    // 不能留一个「能拖但服务端会丢掉」的假控件。
    // （本机实测该节点是 ComfyUI 核心节点恒在，但按「节点真在」原则处理。）
    const shiftAvailable = cfg.sigma_shift_available !== false;
    [svEl, saEl].forEach(el => { if (el) el.disabled = !shiftAvailable; });
    if (svHint) {
      svHint.textContent = shiftAvailable ? svHint.textContent : "节点不可用";
      svHint.title = shiftAvailable ? "" : "本机 ComfyUI 没有 MiniMaxH3SigmaShift 节点";
    }
    if (saHint) {
      saHint.textContent = shiftAvailable ? saHint.textContent : "节点不可用";
      saHint.title = shiftAvailable ? "" : "本机 ComfyUI 没有 MiniMaxH3SigmaShift 节点";
    }

    // ---- CFG：标准路径是**只读说明**（H3 没有 CFG 槽位）----
    const cfgEl = document.getElementById("vidCfg");
    const cfgVal = document.getElementById("vidCfgVal");
    const cfgHint = document.getElementById("vidCfgHint");
    const cfgNote = document.getElementById("vidCfgFixedNote");
    const fixed = cfg.cfg_fixed != null ? cfg.cfg_fixed : 1.0;
    if (cfgEl) cfgEl.value = Number(fixed).toFixed(1);
    if (cfgVal) cfgVal.textContent = Number(fixed).toFixed(1);
    if (cfgHint) cfgHint.textContent = "H3 固定";
    if (cfgNote) cfgNote.textContent = cfg.cfg_fixed_reason || "";

    // ---- 说明行：实际生效值 ----
    const note = document.getElementById("vidSamplingNote");
    if (note) {
      const bits = [
        `步数 ${videoState.steps} → BasicScheduler.steps`,
        `采样器 ${videoState.sampler || "—"} → KSamplerSelect.sampler_name`,
        `调度器 ${videoState.scheduler || "—"} → BasicScheduler.scheduler`,
        `shift ${Number(videoState.shiftVideo).toFixed(2)} / `
          + `${Number(videoState.shiftAudio).toFixed(2)} → MiniMaxH3SigmaShift`,
      ];
      note.textContent = bits.join(" · ");
    }
  }

  // ------------------------------------------------------------ 二采

  function renderTwopass() {
    const enabledBox = document.getElementById("vidTwopassEnabled");
    const body = document.getElementById("vidTwopassBody");
    const note = document.getElementById("vidTwopassNote");
    const hint = document.getElementById("vidTwopassHint");
    const cfg = VIDEO_OPTIONS.twopass || {};
    if (!enabledBox) return;

    const nodeOk = cfg.node_available !== false;
    enabledBox.checked = !!videoState.twopass.enabled;
    enabledBox.disabled = !nodeOk;
    if (body) body.hidden = !videoState.twopass.enabled || !nodeOk;

    if (hint) {
      hint.textContent = nodeOk
        ? "低分辨率一采 + 高分辨率二采"
        : "本机 ComfyUI 没有 SelfLiftH3Sampler 节点，不可用";
    }

    // 潜空间放大器下拉
    const upSel = document.getElementById("vidUpscalerSelect");
    if (upSel) {
      const upscalers = cfg.upscalers || [];
      const current = videoState.twopass.upscaler_model || cfg.upscaler_default || "";
      upSel.innerHTML = "";
      upscalers.forEach(name => {
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name === "none" ? "不使用（直接提升）" : name;
        opt.selected = name === current;
        upSel.appendChild(opt);
      });
      if (!upscalers.includes(current)) {
        const opt = document.createElement("option");
        opt.value = current || "none";
        opt.textContent = current || "none";
        upSel.appendChild(opt);
        upSel.value = current;
      }
    }

    const transition = document.getElementById("vidTransition");
    const lowres = document.getElementById("vidLowres");
    const rho = document.getElementById("vidRho");
    if (transition) {
      transition.value = videoState.twopass.transition_step;
      const label = document.getElementById("vidTransitionVal");
      if (label) label.textContent = videoState.twopass.transition_step;
    }
    if (lowres) {
      lowres.value = videoState.twopass.lowres_scale;
      const label = document.getElementById("vidLowresVal");
      if (label) label.textContent = Number(videoState.twopass.lowres_scale).toFixed(2);
    }
    if (rho) {
      rho.value = videoState.twopass.rho;
      const label = document.getElementById("vidRhoVal");
      if (label) label.textContent = Number(videoState.twopass.rho).toFixed(2);
    }

    // 二采 CFG（SelfLiftH3Sampler.cfg）—— H3 链路里唯一真正可调的 CFG
    const tpCfg = document.getElementById("vidTwopassCfg");
    if (tpCfg) {
      tpCfg.value = videoState.twopass.cfg;
      const label = document.getElementById("vidTwopassCfgVal");
      if (label) label.textContent = Number(videoState.twopass.cfg).toFixed(1);
    }
    // 二采采样器：SelfLift 只支持标准 Euler（实测自 comfyui-SelfLift/nodes.py），
    // 所以这里是**只读固定值**，不是可调控件 —— 不伪造假下拉。
    const tpSampler = document.getElementById("vidTwopassSampler");
    if (tpSampler) tpSampler.value = "euler";

    if (note) {
      const bits = [cfg.note || ""];
      if (videoState.twopass.enabled) {
        bits.push(`一采 ${videoState.twopass.transition_step} 步 @ `
          + `${Number(videoState.twopass.lowres_scale).toFixed(2)}× 分辨率`);
        if (Number(videoState.twopass.rho) <= 0
          && videoState.twopass.upscaler_model === "none") {
          bits.push("（ρ=0 且不用放大器会被节点拒绝，服务端会自动把 ρ 提到 0.6）");
        }
        bits.push("二采固定用 Euler 采样器（SelfLift 只支持标准 Euler）。");
      }
      note.textContent = bits.filter(Boolean).join(" ");
    }
  }

  // ------------------------------------------------------------ 比例

  function renderAspectChips() {
    const chips = document.getElementById("vidAspectChips");
    if (!chips) return;
    chips.innerHTML = "";
    (VIDEO_OPTIONS.aspects || []).forEach(item => {
      const b = document.createElement("button");
      b.type = "button";
      b.dataset.v = item.key;
      b.textContent = item.key;
      b.title = `${item.ratio[0]}:${item.ratio[1]}`;
      if (item.key === videoState.aspect) b.classList.add("active");
      b.addEventListener("click", () => {
        videoState.aspect = item.key;
        videoState.custom = false;
        videoState.width = null;
        videoState.height = null;
        videoState.rawWidth = null;
        videoState.rawHeight = null;
        videoState.sizeReasons = [];
        videoState.sizePresetKey = "";
        sizeTyping = false;
        renderAspectChips();
        renderSizePresetSelect();
        renderSizeFields();
        saveVideoConfig();
      });
      chips.appendChild(b);
    });
  }

  // ------------------------------------------------------------ 总渲染

  function renderVideoPanel() {
    // 【2026-11-11】视频面板是 t2v 专用的（首尾帧 / 参考生视频走各自的老面板），
    // 但仍把 mode 记下来 —— 尺寸策略按 mode 取，不写死。
    VIDEO_OPTIONS.mode = "t2v";
    if (!optionsReady) return;
    // 【2026-11-11】每一段独立 try/catch —— **一个区块渲染失败不能
    // 阻断其它区块**（原来任何一处抛错，后面的下拉全都不渲染，
    // 于是整块面板空掉）。这正是「模型列表 API 失败不得阻断
    // 尺寸预设 / LoRA / 注意力机制等其它下拉」的实现方式。
    // 合并说明：本区块同时承接 panel-fallback 的逐段 try/catch 与
    // t2v-sampler 新增的「采样参数」区块（renderSampling）——
    // 两者语义可叠加，缺一不可，故两段都保留。
    // model-list-fix 分支在此处额外调用 renderModelSourceNote()
    // （显示「清单来源：ComfyUI 模型列表 API」）—— 独立于其它区块，
    // 同样纳入 try/catch 保护，一并保留。
    const steps = [
      ["清单来源", renderModelSourceNote],
      ["模型", renderModelSelect],
      ["画面比例", renderAspectChips],
      ["尺寸预设", renderSizePresetSelect],
      ["输出尺寸", renderSizeFields],
      ["固定加速LoRA", renderFixedLoraNote],
      ["加速LoRA", renderAccelLoraSelect],
      ["注意力", renderAttention],
      ["采样参数", renderSampling],
      ["二采", renderTwopass],
    ];
    steps.forEach(([name, fn]) => {
      try {
        fn();
      } catch (e) {
        console.error(`[视频面板] 渲染「${name}」失败:`, e);
      }
    });
  }

  // ------------------------------------------------------------ 配置持久化

  function collectConfig() {
    return {
      model: videoState.model,
      aspect: videoState.aspect,
      megapixels: videoState.megapixels,
      custom: videoState.custom,
      width: videoState.width,
      height: videoState.height,
      rawWidth: videoState.rawWidth,
      rawHeight: videoState.rawHeight,
      size_preset_key: videoState.sizePresetKey,
      fixed_accel_lora: videoState.fixedAccelLora,
      // 【单选】只存一个加速 LoRA + 它的强度
      accel_lora: videoState.accelLora,
      accel_lora_strength: Number(videoState.accelLoraStrength),
      attention: videoState.attention,
      twopass: { ...videoState.twopass },
      // 【2026-11-11】采样参数
      steps: videoState.steps,
      sampler: videoState.sampler,
      scheduler: videoState.scheduler,
      shift_video: Number(videoState.shiftVideo),
      shift_audio: Number(videoState.shiftAudio),
    };
  }

  /**
   * 尺寸预设 key 的**反查**：按当前宽高在预设清单里找对应项。
   *
   * 【为什么需要它 —— 2026-11-12 修「复用参数不填分辨率」】
   * applyConfig() 从老任务恢复宽高时，只改了 state.width/height，
   * **没有重算 sizePresetKey**。于是：
   *   - 左侧「输出尺寸」输入框显示的是回填后的宽高（对的）
   *   - 但「尺寸预设」下拉仍停在**上一次**的档位（错的）
   * 两者对不上 —— 用户看到「1344×768」配「480p · 864×480」。
   * renderSizePresetSelect() 里虽然有一句「按当前宽高反查」的兜底，
   * 但它只在 `!want || !presets.some(p => p.key === want)` 时才触发 ——
   * 而 want 是**上一个任务的合法 key**，命中了 → 兜底不生效 → 旧档位留下。
   *
   * 修法：applyConfig() 回填宽高后**强制重算** sizePresetKey
   * （用同一个反查函数，逻辑只有一份）。
   */
  function presetKeyForSize(width, height) {
    if (!width || !height) return "";
    const hit = presetsForAspect(videoState.aspect).find(
      p => p.width === Number(width) && p.height === Number(height));
    return hit ? hit.key : "";
  }

  function applyConfig(cfg) {
    if (!cfg || typeof cfg !== "object") return;
    if (cfg.model && VIDEO_OPTIONS.models[cfg.model]) videoState.model = cfg.model;
    if (cfg.aspect) videoState.aspect = cfg.aspect;
    if (cfg.megapixels) videoState.megapixels = Number(cfg.megapixels);
    if (cfg.custom !== undefined) videoState.custom = !!cfg.custom;
    if (cfg.width) videoState.width = Number(cfg.width);
    if (cfg.height) videoState.height = Number(cfg.height);
    if (cfg.rawWidth) videoState.rawWidth = Number(cfg.rawWidth);
    if (cfg.rawHeight) videoState.rawHeight = Number(cfg.rawHeight);
    // 【2026-11-12 修】回填宽高后**必须重算**尺寸预设档位 ——
    // 否则「尺寸预设」下拉会停在上一条任务的档位上，与宽高对不上。
    // 优先用配置里带的 size_preset_key（老配置可能有）；
    // 但它必须与当前宽高一致，否则以宽高反查的结果为准。
    if (cfg.size_preset_key) {
      const preset = presetsForAspect(videoState.aspect).find(
        p => p.key === cfg.size_preset_key);
      if (preset && preset.width === videoState.width
          && preset.height === videoState.height) {
        videoState.sizePresetKey = cfg.size_preset_key;
      } else {
        videoState.sizePresetKey = presetKeyForSize(
          videoState.width, videoState.height);
      }
    } else {
      videoState.sizePresetKey = presetKeyForSize(
        videoState.width, videoState.height);
    }
    if (cfg.fixed_accel_lora) videoState.fixedAccelLora = cfg.fixed_accel_lora;
    // 【单选】新格式
    if (cfg.accel_lora !== undefined) {
      videoState.accelLora = cfg.accel_lora || "";
    }
    if (cfg.accel_lora_strength !== undefined) {
      videoState.accelLoraStrength = Number(cfg.accel_lora_strength);
    }
    // 【兼容旧格式】老配置里存的是 accel_loras 数组（可能多项）——
    // 取第一项启用且强度 > 0 的，落到新的单选字段上。
    if (Array.isArray(cfg.accel_loras) && cfg.accel_loras.length) {
      const first = cfg.accel_loras.find(
        l => l && l.key && l.enabled !== false && Number(l.strength) > 0);
      if (first && !cfg.accel_lora) {
        videoState.accelLora = first.key;
        videoState.accelLoraStrength = Number(first.strength || 1);
      }
    }
    if (cfg.attention) videoState.attention = cfg.attention;
    if (cfg.twopass && typeof cfg.twopass === "object") {
      videoState.twopass = { ...videoState.twopass, ...cfg.twopass };
    }
    // 【2026-11-11】采样参数
    if (cfg.steps != null) videoState.steps = Number(cfg.steps) || videoState.steps;
    if (cfg.sampler) videoState.sampler = cfg.sampler;
    if (cfg.scheduler) videoState.scheduler = cfg.scheduler;
    if (cfg.shift_video != null) videoState.shiftVideo = Number(cfg.shift_video);
    if (cfg.shift_audio != null) videoState.shiftAudio = Number(cfg.shift_audio);
  }

  function saveVideoConfig() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
      try {
        const resp = await fetch("/api/video-config", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(collectConfig()),
        });
        if (!resp.ok) {
          const data = await resp.json().catch(() => ({}));
          console.warn("[视频面板] 配置保存失败:", data.error || resp.status);
        }
      } catch (e) {
        console.warn("[视频面板] 配置保存失败:", e.message);
      }
    }, 300);
  }

  // ------------------------------------------------------------ 加载清单
  //
  // 【2026-11-11 修复 —— 下拉空白】
  // 原来这里 catch 掉异常就**什么都不做**：
  //   fetch 失败 -> 直接进 catch -> renderVideoPanel() 从未执行
  // -> 尺寸预设 / 模型 / 加速 LoRA / 注意力机制**全是空下拉**
  //   （控件在、选项没有），用户完全没法用。
  // 触发场景实测：ComfyUI 正在启动（object_info 超时）时，
  // /api/video-options 要 66 秒才返回，浏览器先放弃。
  //
  // 现在：**失败也要渲染**。
  //   * 拿不到服务端清单 -> 用 web/video-panel-fallback.js 里的
  //     静态兜底清单（同源实测数据），保证每个下拉都有选项；
  //   * 同时显示明确的错误条，告诉用户「当前是兜底清单，
  //     服务端没连上」—— 不静默、不假装正常；
  //   * 其它面板（尺寸预设 / 加速 LoRA / 注意力）**互不阻断**：
  //     各自的数据缺失只影响自己。

  /** 取静态兜底清单（脚本没加载时返回 null）。 */
  function fallbackManifest() {
    return window.VIDEO_PANEL_FALLBACK || null;
  }

  /**
   * 用静态兜底清单填 VIDEO_OPTIONS。
   * 只填**缺的那部分** —— 服务端给了的以服务端为准。
   *
   * 【健壮性】整个函数**不能抛** —— 它跑在「已经出过一次错」的路径上，
   * 再抛一次就会让整个面板初始化中断（比空下拉更糟）。
   */
  function applyFallbackManifest() {
    let fb = null;
    try {
      fb = fallbackManifest();
    } catch (e) {
      fb = null;
    }
    if (!fb || typeof fb !== "object") return false;
    try {
      // models：服务端没给或给了空 -> 用兜底
      if (!VIDEO_OPTIONS.models || !Object.keys(VIDEO_OPTIONS.models).length) {
        const dict = {};
        (fb.models || []).forEach(m => {
          if (m && m.key) dict[m.key] = m;
        });
        VIDEO_OPTIONS.models = dict;
        VIDEO_OPTIONS.default_model = fb.default_model || "";
      }
      if (!VIDEO_OPTIONS.size_presets || !VIDEO_OPTIONS.size_presets.length) {
        // 服务端的 size_presets 是**扁平 list**（每条带 aspect 字段）——
        // 兜底清单必须同形，否则 renderSizePresetSelect 里
        // presets.filter(...) 会抛 "object is not iterable"。
        VIDEO_OPTIONS.size_presets = Array.isArray(fb.size_presets)
          ? fb.size_presets : [];
      }
      if (!VIDEO_OPTIONS.attention_modes || !VIDEO_OPTIONS.attention_modes.length) {
        VIDEO_OPTIONS.attention_modes = Array.isArray(fb.attention_modes)
          ? fb.attention_modes : [];
        VIDEO_OPTIONS.default_attention = fb.default_attention || "default";
      }
      if (!VIDEO_OPTIONS.accel_lora_combos || !VIDEO_OPTIONS.accel_lora_combos.length) {
        VIDEO_OPTIONS.accel_lora_combos = Array.isArray(fb.accel_lora_combos)
          ? fb.accel_lora_combos : [];
      }
      if (!VIDEO_OPTIONS.fixed_accel_loras || !VIDEO_OPTIONS.fixed_accel_loras.length) {
        VIDEO_OPTIONS.fixed_accel_loras = Array.isArray(fb.fixed_accel_loras)
          ? fb.fixed_accel_loras : [];
        VIDEO_OPTIONS.default_fixed_accel_lora = fb.default_fixed_accel_lora || "";
      }
      if (!VIDEO_OPTIONS.aspects || !VIDEO_OPTIONS.aspects.length) {
        VIDEO_OPTIONS.aspects = Array.isArray(fb.aspects) ? fb.aspects : [];
      }
      if (!VIDEO_OPTIONS.size_multiple) VIDEO_OPTIONS.size_multiple = fb.size_multiple || 32;
      if (!VIDEO_OPTIONS.size_min) VIDEO_OPTIONS.size_min = fb.size_min || 32;
      if (!VIDEO_OPTIONS.size_max) VIDEO_OPTIONS.size_max = fb.size_max || 16384;
      if (!VIDEO_OPTIONS.mp_table || !VIDEO_OPTIONS.mp_table.length) {
        VIDEO_OPTIONS.mp_table = Array.isArray(fb.mp_table) ? fb.mp_table : [];
      }
      if (!VIDEO_OPTIONS.default_mp) VIDEO_OPTIONS.default_mp = fb.default_mp || 0.4;
      return true;
    } catch (e) {
      console.error("[视频面板] 兜底清单填充失败:", e);
      return false;
    }
  }

  /** 兜底态的错误条：说清楚「现在显示的是兜底清单」而不是假装正常。 */
  function renderFallbackNotice(reason) {
    const box = document.getElementById("staleVideoBanner");
    if (!box) return;
    box.hidden = false;
    // 不改 class —— 保留 .stale-backend-banner 的既有样式
    box.innerHTML = "视频面板清单载入失败：<b>" + esc(reason)
      + "</b>。已切换到<b>内置兜底清单</b>（下拉仍可选择），"
      + "但服务端无法校验本机权重 —— 提交可能失败。"
      + "请确认后端与 ComfyUI 已就绪（<code>systemctl status h3-studio comfyui</code>）。";
  }

  async function loadVideoOptions() {
    let reason = "";
    try {
      const resp = await fetch("/api/video-options", { cache: "no-store" });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const data = await resp.json();
      Object.assign(VIDEO_OPTIONS, data);
      VIDEO_OPTIONS.models = data.models || [];
      // models 下发的是数组 -> 转成 key -> meta 的字典
      const dict = {};
      (data.models || []).forEach(m => { dict[m.key] = m; });
      VIDEO_OPTIONS.models = dict;
      optionsReady = true;

      // 默认值
      if (!videoState.model) videoState.model = data.default_model || "";
      if (!videoState.fixedAccelLora) {
        videoState.fixedAccelLora = data.default_fixed_accel_lora || "";
      }
      if (!videoState.attention) {
        videoState.attention = data.default_attention || "";
      }
      if (!videoState.twopass.upscaler_model) {
        videoState.twopass.upscaler_model =
          (data.twopass && data.twopass.upscaler_default) || "";
      }
      if (!videoState.megapixels) {
        videoState.megapixels = data.default_mp || 0.4;
      }
      // 【2026-11-11】采样参数默认值 —— 取自服务端下发的 sampling 清单
      // 与**当前底模**的推荐值（VIDEO_MODELS 里每个模型都带
      // steps / sampler / scheduler）。所以面板默认显示的就是
      // 「这个底模实际会用的值」，而不是拍脑袋的数。
      const sampling = data.sampling || {};
      const defModel = VIDEO_OPTIONS.models[videoState.model] || {};
      if (!videoState.steps) {
        videoState.steps = defModel.steps != null
          ? Number(defModel.steps)
          : (sampling.steps_default != null ? sampling.steps_default : 4);
      }
      if (!videoState.sampler) {
        videoState.sampler = defModel.sampler
          || (sampling.samplers && sampling.samplers[0]) || "";
      }
      if (!videoState.scheduler) {
        videoState.scheduler = defModel.scheduler
          || (sampling.schedulers && sampling.schedulers[0]) || "";
      }
      if (videoState.shiftVideo == null
        || videoState.shiftVideo === 12.0) {
        videoState.shiftVideo = sampling.shift_default_video != null
          ? sampling.shift_default_video : 12.0;
      }
      if (videoState.shiftAudio == null
        || videoState.shiftAudio === 3.0) {
        videoState.shiftAudio = sampling.shift_default_audio != null
          ? sampling.shift_default_audio : 3.0;
      }
      if (videoState.twopass.cfg == null
        || videoState.twopass.cfg === 1.0) {
        videoState.twopass.cfg = sampling.cfg_twopass_default != null
          ? sampling.cfg_twopass_default : 1.0;
      }
      // 恢复保存的配置
      try {
        const cfgResp = await fetch("/api/video-config", { cache: "no-store" });
        if (cfgResp.ok) applyConfig(await cfgResp.json());
      } catch (e) { /* 没有保存过配置，用默认值 */ }

      renderVideoPanel();
    } catch (e) {
      // 【关键】失败也必须渲染 —— 否则整个面板是空的
      console.error("[视频面板] 载入清单失败:", e.message);
      reason = e.message || String(e);
      optionsReady = true;                 // 让 renderVideoPanel() 能跑
      const usedFallback = applyFallbackManifest();
      // 兜底态：模型项标记为「可用性未知」，且默认选中第一个
      if (!videoState.model) {
        videoState.model = VIDEO_OPTIONS.default_model
          || (Object.keys(VIDEO_OPTIONS.models || {})[0] || "");
      }
      if (!videoState.attention) {
        videoState.attention = VIDEO_OPTIONS.default_attention || "default";
      }
      if (!videoState.megapixels) {
        videoState.megapixels = VIDEO_OPTIONS.default_mp || 0.4;
      }
      try {
        renderVideoPanel();
      } catch (renderErr) {
        console.error("[视频面板] 兜底渲染也失败:", renderErr);
      }
      if (usedFallback) {
        renderFallbackNotice(reason);
      } else {
        const banner = document.getElementById("staleVideoBanner");
        if (banner) {
          banner.hidden = false;
          banner.innerHTML = "视频面板清单载入失败：<b>" + esc(reason)
            + "</b>，且没有内置兜底清单可用。请确认后端已启动"
            + "（<code>systemctl restart h3-studio</code>）。";
        }
      }
    }
  }

  /** 切模型时重新拉清单（加速 LoRA 的兼容性随底模变） */
  async function refreshAccelCompat() {
    if (!optionsReady) return;
    try {
      const resp = await fetch(
        `/api/video-options?model=${encodeURIComponent(videoState.model)}`,
        { cache: "no-store" });
      if (!resp.ok) return;
      const data = await resp.json();
      VIDEO_OPTIONS.accel_lora_combos = data.accel_lora_combos || [];
      if (data.accel_lora_compat) {
        VIDEO_OPTIONS.accel_lora_compat = data.accel_lora_compat;
      }
      renderAccelLoraSelect();
    } catch (e) { /* 忽略：清单保持上一次的结果 */ }
  }

  // ------------------------------------------------------------ 提交参数

  function submitParams() {
    const [w, h] = currentSize();
    const accel = [];
    if (videoState.accelLora && Number(videoState.accelLoraStrength) > 0) {
      accel.push({
        key: videoState.accelLora,
        strength: Number(videoState.accelLoraStrength),
        enabled: true,
      });
    }
    return {
      model_key: videoState.model,
      width: w,
      height: h,
      aspect: videoState.aspect,
      duration: state.duration,
      // 【2026-11-11】采样参数 —— 真正传进工作流。
      // 注意：这里**不再**用全局 state.steps（旧面板的通用滑杆），
      // 视频面板有自己的步数控件（vidSteps），服务端会用这里的值。
      steps: videoState.steps,
      sampler: videoState.sampler,
      scheduler: videoState.scheduler,
      shift_video: Number(videoState.shiftVideo),
      shift_audio: Number(videoState.shiftAudio),
      seed: $("#seed").value.trim() || null,
      fixed_accel_lora: videoState.fixedAccelLora,
      // 【单选】最多一项
      accel_loras: accel,
      attention: videoState.attention,
      twopass: {
        enabled: !!videoState.twopass.enabled,
        transition_step: videoState.twopass.transition_step,
        lowres_scale: videoState.twopass.lowres_scale,
        rho: videoState.twopass.rho,
        w_min: videoState.twopass.w_min,
        w_max: videoState.twopass.w_max,
        upscaler_model: videoState.twopass.upscaler_model,
        // 二采 CFG（SelfLiftH3Sampler.cfg）—— 标准路径没有 CFG，
        // 这是 H3 链路里唯一真正生效的 CFG。
        cfg: Number(videoState.twopass.cfg),
      },
    };
  }

  // ------------------------------------------------------------ 事件绑定

  function bind() {
    // ---- 尺寸预设下拉 ----
    const presetSel = document.getElementById("vidSizePresetSelect");
    if (presetSel) {
      presetSel.addEventListener("change", e => {
        const key = e.target.value;
        videoState.sizePresetKey = key || "";
        if (!key) {
          // 选了「自定义」-> 回到按比例自动算的尺寸
          videoState.custom = false;
          videoState.width = null;
          videoState.height = null;
          videoState.rawWidth = null;
          videoState.rawHeight = null;
          videoState.sizeReasons = [];
          sizeTyping = false;
          renderSizePresetSelect();
          renderSizeFields();
          saveVideoConfig();
          return;
        }
        const p = presetsForAspect(videoState.aspect).find(x => x.key === key);
        if (!p) return;
        videoState.aspect = p.aspect;
        videoState.custom = false;
        videoState.width = p.width;
        videoState.height = p.height;
        videoState.rawWidth = null;
        videoState.rawHeight = null;
        videoState.megapixels = p.megapixels;
        videoState.sizeReasons = [];
        sizeTyping = false;
        renderAspectChips();
        renderSizePresetSelect();
        renderSizeFields();
        saveVideoConfig();
      });
    }

    const widthInput = document.getElementById("vidWidth");
    const heightInput = document.getElementById("vidHeight");
    if (widthInput) {
      widthInput.addEventListener("input", () => {
        const v = parseInt(widthInput.value);
        videoState.rawWidth = Number.isFinite(v) && v > 0 ? v : null;
        if (videoState.rawWidth !== null) videoState.custom = true;
        sizeTyping = true;
        applySize(videoState.rawWidth, videoState.rawHeight);
      });
    }
    if (heightInput) {
      heightInput.addEventListener("input", () => {
        const v = parseInt(heightInput.value);
        videoState.rawHeight = Number.isFinite(v) && v > 0 ? v : null;
        if (videoState.rawHeight !== null) videoState.custom = true;
        sizeTyping = true;
        applySize(videoState.rawWidth, videoState.rawHeight);
      });
    }
    ["vidWidth", "vidHeight"].forEach(id => {
      const el = document.getElementById(id);
      if (!el) return;
      const commit = () => {
        sizeTyping = false;
        applySize(videoState.rawWidth, videoState.rawHeight).then(saveVideoConfig);
      };
      el.addEventListener("change", commit);
      el.addEventListener("blur", commit);
    });
    const reset = document.getElementById("vidSizeReset");
    if (reset) {
      reset.addEventListener("click", () => {
        videoState.custom = false;
        videoState.width = null;
        videoState.height = null;
        videoState.rawWidth = null;
        videoState.rawHeight = null;
        videoState.sizeReasons = [];
        videoState.sizePresetKey = "";
        sizeTyping = false;
        renderSizePresetSelect();
        renderSizeFields();
        saveVideoConfig();
      });
    }

    const modelSel = document.getElementById("vidModelSelect");
    if (modelSel) {
      modelSel.addEventListener("change", e => {
        videoState.model = e.target.value;
        renderModelNote();
        refreshAccelCompat();
        saveVideoConfig();
      });
    }

    // ---- 加速 LoRA（下拉单选）----
    const accelSel = document.getElementById("vidAccelLoraSelect");
    if (accelSel) {
      accelSel.addEventListener("change", e => {
        const previous = videoState.accelLora;
        videoState.accelLora = e.target.value || "";
        const meta = videoState.accelLora ? accelComboMeta(videoState.accelLora) : null;
        if (meta) {
          // 【2026-11-12 修 —— DMAD 默认强度必须是 1.00】
          //
          // 旧逻辑：只有 strength 是 undefined/null 时才套用推荐值。
          // 于是用户之前把强度调到 0.65，再切到 DMAD —— **0.65 被沿用**，
          // 面板上显示「DMAD 4 步（critic 版）×0.65」，落库也是 0.65。
          // 但 DMAD 的官方值是 1.00（safetensors 元数据：
          //   sampling = 4 steps, video shift 12, audio shift 2,
          //   1344x768, 124 frames at 24 fps, no CFG
          //   lora_alpha = 128, alpha = rank everywhere (scale 1.0)
          // —— 权重本身就是按 scale 1.0 训的，0.65 会明显偏离）。
          //
          // 新逻辑：**切换到另一枚 LoRA 时，强度重置为那枚的推荐值**。
          // 只有「同一枚 LoRA 反复切换」（比如选回同一项）才保留用户
          // 手动调过的值 —— 那样用户微调后不会莫名其妙被重置。
          if (videoState.accelLora !== previous) {
            videoState.accelLoraStrength = Number(
              meta.recommended_strength != null ? meta.recommended_strength : 1);
          }
        }
        renderAccelLoraNote();
        renderAccelLoraStrength();
        saveVideoConfig();
      });
    }
    const accelStrength = document.getElementById("vidAccelLoraStrength");
    if (accelStrength) {
      accelStrength.addEventListener("input", e => {
        videoState.accelLoraStrength = parseFloat(e.target.value);
        const label = document.getElementById("vidAccelLoraStrengthVal");
        if (label) label.textContent = videoState.accelLoraStrength.toFixed(2);
      });
      accelStrength.addEventListener("change", () => {
        renderAccelLoraNote();
        saveVideoConfig();
      });
    }

    const attSel = document.getElementById("vidAttentionSelect");
    if (attSel) {
      attSel.addEventListener("change", e => {
        videoState.attention = e.target.value;
        renderAttention();
        saveVideoConfig();
      });
    }

    const tpBox = document.getElementById("vidTwopassEnabled");
    if (tpBox) {
      tpBox.addEventListener("change", () => {
        videoState.twopass.enabled = tpBox.checked;
        renderTwopass();
        saveVideoConfig();
      });
    }
    const transition = document.getElementById("vidTransition");
    if (transition) {
      transition.addEventListener("input", e => {
        videoState.twopass.transition_step = parseInt(e.target.value);
        renderTwopass();
      });
      transition.addEventListener("change", saveVideoConfig);
    }
    const lowres = document.getElementById("vidLowres");
    if (lowres) {
      lowres.addEventListener("input", e => {
        videoState.twopass.lowres_scale = parseFloat(e.target.value);
        renderTwopass();
      });
      lowres.addEventListener("change", saveVideoConfig);
    }
    const rho = document.getElementById("vidRho");
    if (rho) {
      rho.addEventListener("input", e => {
        videoState.twopass.rho = parseFloat(e.target.value);
        renderTwopass();
      });
      rho.addEventListener("change", saveVideoConfig);
    }
    const upscaler = document.getElementById("vidUpscalerSelect");
    if (upscaler) {
      upscaler.addEventListener("change", e => {
        videoState.twopass.upscaler_model = e.target.value;
        renderTwopass();
        saveVideoConfig();
      });
    }
    // 二采 CFG
    const tpCfg = document.getElementById("vidTwopassCfg");
    if (tpCfg) {
      tpCfg.addEventListener("input", e => {
        videoState.twopass.cfg = parseFloat(e.target.value);
        renderTwopass();
      });
      tpCfg.addEventListener("change", saveVideoConfig);
    }

    // ---- 采样参数（步数 / 采样器 / 调度器 / shift）----
    const steps = document.getElementById("vidSteps");
    if (steps) {
      steps.addEventListener("input", e => {
        videoState.steps = parseInt(e.target.value);
        renderSampling();
      });
      steps.addEventListener("change", saveVideoConfig);
    }
    const sampler = document.getElementById("vidSamplerSelect");
    if (sampler) {
      sampler.addEventListener("change", e => {
        videoState.sampler = e.target.value;
        renderSampling();
        saveVideoConfig();
      });
    }
    const scheduler = document.getElementById("vidSchedulerSelect");
    if (scheduler) {
      scheduler.addEventListener("change", e => {
        videoState.scheduler = e.target.value;
        renderSampling();
        saveVideoConfig();
      });
    }
    const shiftVideo = document.getElementById("vidShiftVideo");
    if (shiftVideo) {
      shiftVideo.addEventListener("input", e => {
        videoState.shiftVideo = parseFloat(e.target.value);
        renderSampling();
      });
      shiftVideo.addEventListener("change", saveVideoConfig);
    }
    const shiftAudio = document.getElementById("vidShiftAudio");
    if (shiftAudio) {
      shiftAudio.addEventListener("input", e => {
        videoState.shiftAudio = parseFloat(e.target.value);
        renderSampling();
      });
      shiftAudio.addEventListener("change", saveVideoConfig);
    }
  }

  // ------------------------------------------------------------ 导出

  window.VIDEO_PANEL = {
    state: videoState,
    options: VIDEO_OPTIONS,
    isReady: () => optionsReady,
    render: renderVideoPanel,
    load: loadVideoOptions,
    bind: bind,
    submitParams: submitParams,
    applyConfig: applyConfig,
    collectConfig: collectConfig,
    save: saveVideoConfig,
    // 供验收脚本直接读状态（证明「界面值 == 提交值」）
    _debug: {
      selectableAccelLoras,
      presetsForAspect,
      currentSize,
    },
  };
})();
