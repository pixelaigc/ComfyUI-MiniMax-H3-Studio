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
    // 【2026-11-14】加速 LoRA 统一成一组可选项（不再分「固定枚/组合枚」）。
    // accel_loras 是**唯一真源**；fixed_accel_loras 保留为同内容别名
    // （老代码读它不会炸），accel_lora_combos 保留为空数组。
    accel_loras: [],
    fixed_accel_loras: [],
    accel_lora_combos: [],
    default_fixed_accel_lora: "",
    default_accel_lora: "",
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
    // 【2026-11-16 审查 B-5 修复】时长镜像值。
    //
    // 【为什么需要】submitParams() 原来写 `duration: state.duration`，
    // 裸引用 app.js 的全局 state —— 本文件是 IIFE，内部没有 state，
    // 那是一次跨文件的隐式耦合（app.js 一改名就 ReferenceError）。
    // 现在时长有自己的落点，由 app.js 在「用户改时长」和
    // 「复用老任务参数」两条路径上显式同步过来。
    duration: 5,
    megapixels: 0.4,
    custom: false,          // 用户手填过宽高
    width: null,
    height: null,
    rawWidth: null,         // 用户原始输入（未经吸附）
    rawHeight: null,
    sizePresetKey: "",      // 当前选中的尺寸预设（空 = 自定义）
    // 【2026-11-15 修复③ 交付项 3】当前面板服务的生成模式。
    //
    // 【为什么要有它】首尾帧（flf）/ 参考生视频（r2v）现在**共用**
    // 文生视频（t2v）这一套面板 —— 同一份渲染代码、同一套分块、
    // 同一套控件顺序。区别只在：
    //   * 尺寸上限（【2026-11-17】flf / r2v = t2v = 2.0MP，对齐 nonebot
    //     seg1/seg2 的 ResolutionSelector(megapixels=2)）；
    //   * 可选的底模（r2v 必须用 Ref2VA 权重）；
    //   * 本模式特有的素材字段（首帧 / 尾帧 / 参考图 / 参考视频 / 参考音乐）。
    //
    // 「快捷选分辨率」这类**额外布局元素**已按要求删除 —— flf / r2v
    // 不再有独立的档位卡片，而是与 t2v 一样用「尺寸预设下拉 + 宽高直填」。
    mode: "t2v",
    // 【2026-11-15 修复③ 交付项 3】逐模式的配置片段（见 modeConfigs()）。
    modeConfigs: {},
    // 【2026-11-15 修复③ 交付项 3】加速 LoRA 是**一个槽位**：选哪枚就只挂哪枚。
    // 空串 = 不挂加速 LoRA（走标准多步采样）。
    accelLora: "",
    accelLoraStrength: 1.0, // 该加速 LoRA 的强度
    // 效果 LoRA（可多选，与加速 LoRA 并列共存）
    // [{name, strength, enabled, trigger}] —— 顺序 = 界面上的排列顺序
    effectLoras: [],
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
      // 【2026-11-13 新增 —— 借鉴 nonebot H3 工作流的 H3SigmaRefiner】
      // sigma 精修：低噪段局部加密采样步（针对高速运动边缘颗粒 / 闪烁）。
      // 默认**关闭** —— nonebot 模板的 start_at_sigma=0.7 在本项目默认档
      // （steps=4 / beta）下是 no-op；节点 tooltip 推荐 2.0~3.5，这里取 2.5。
      sigma_refine: {
        enabled: false,
        extra_steps: 1,
        start_at_sigma: 2.5,
        end_at_sigma: 0.0,
        spacing: "cosine",
      },
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

  /**
   * 【2026-11-15 修复③ 交付项 3】当前模式下**可选**的底模清单。
   *
   * 【为什么按模式过滤】
   *   * r2v（参考生视频）必须用 **Ref2VA 权重** —— FL2VA 权重没有
   *     Ref2VA 通道，用它做参考生成等于「参考没生效」
   *     （服务端 model_for_mode() 会静默换权重，但那是**事后**换，
   *     面板上先不让用户选错才是对的）。
   *   * flf（首尾帧）用 FL2VA / 混合权重（它有 I2VA / FL2VA / L2VA
   *     三种任务类型）；Ref2VA 权重**没有**首尾帧通道。
   *   * t2v（文生视频）用 FL2VA / 混合权重。
   *
   * 【判据来源】服务端 /api/video-options 下发的 models 条目里带
   * `supports_ref2va` 标记（h3studio/video_models.py 的 VIDEO_MODELS）。
   * 前端不自己维护第二张映射表 —— 那正是本项目反复踩过的坑。
   */
  function modelsForMode(mode) {
    const m = mode || videoState.mode || "t2v";
    const all = Object.entries(VIDEO_OPTIONS.models || {});
    if (m === "r2v") {
      // 只保留支持 Ref2VA 的权重
      const ok = all.filter(([, meta]) => meta.supports_ref2va === true);
      return ok.length ? ok : all;
    }
    // flf / t2v：排除「只有 Ref2VA 通道」的权重
    const ok = all.filter(([, meta]) => meta.supports_ref2va !== true);
    return ok.length ? ok : all;
  }

  /**
   * 加速 LoRA 清单 —— **一个列表**（2026-11-14 重构）。
   *
   * 旧实现分两栏：fixed_accel_loras（不在下拉里、恒生效）+
   * accel_lora_combos（下拉里选的、额外叠加）。于是「选什么」不决定
   * 「加载什么」—— 用户报的「选了加速 LoRA 却没生效」就是这么来的。
   * 现在两栏合并，下拉里选的那枚就是工作流里唯一挂的那枚。
   */
  function accelLoraList() {
    if (Array.isArray(VIDEO_OPTIONS.accel_loras)
        && VIDEO_OPTIONS.accel_loras.length) {
      return VIDEO_OPTIONS.accel_loras;
    }
    // 兜底：服务端还没下发新字段时读老字段
    if (Array.isArray(VIDEO_OPTIONS.fixed_accel_loras)
        && VIDEO_OPTIONS.fixed_accel_loras.length) {
      return VIDEO_OPTIONS.fixed_accel_loras;
    }
    return [];
  }

  function accelMeta(key) {
    return accelLoraList().find(l => l.key === key) || null;
  }

  /**
   * 【2026-11-15 修 AC-1】按**当前挂的加速 LoRA** 重算采样档
   * （步数 / 采样器 / 调度器）。
   *
   * 【为什么必须有这个函数】
   * 后端 video_workflow._resolve_accel_chain 的默认档本来就是对的：
   *   挂了加速 LoRA -> 该 LoRA 的 recommended_steps / scheduler / sampler；
   *   没挂           -> 20 步标准多步采样（simple + res_multistep）。
   * 但那条分支只在 `params["steps"]` **为空**时才会走到。
   * 前端 `videoState.steps` 有初值（4），而且**每次提交都显式带上**，
   * 于是后端永远拿到一个非空 steps => 标准多步档那条分支**永远走不到**
   * => 用户选「不挂加速 LoRA」时，跑的仍然是 4 步蒸馏档（画面必然糊）。
   * 同一问题也出现在「从 ckpt850 切到 lightx2v_8step」：面板不会跳到 8 步。
   *
   * 【修法】前端在切换加速 LoRA 时，按服务端下发的同一份清单重算，
   * 保证「面板显示的采样档」与「工作流里实际生效的采样档」一致。
   * 标准多步档的三个值由 /api/video-options 的 sampling.*_without_accel 下发
   * （与后端 video_models 的常量同源，不在前端硬编码第二份）。
   */
  function recomputeSamplingForAccel() {
    const cfg = samplingCfg();
    const meta = videoState.accelLora ? accelMeta(videoState.accelLora) : null;
    if (meta) {
      const steps = Number(meta.recommended_steps);
      if (Number.isFinite(steps) && steps > 0) videoState.steps = steps;
      const sched = meta.recommended_scheduler;
      if (sched) videoState.scheduler = sched;
      const smp = meta.recommended_sampler;
      if (smp) videoState.sampler = smp;
    } else {
      // 不挂加速 LoRA -> 标准多步采样档（20 步 / simple / res_multistep）
      const steps = Number(cfg.steps_without_accel);
      if (Number.isFinite(steps) && steps > 0) videoState.steps = steps;
      const sched = cfg.scheduler_without_accel;
      if (sched) videoState.scheduler = sched;
      const smp = cfg.sampler_without_accel;
      if (smp) videoState.sampler = smp;
    }
    renderSampling();
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
      // 【2026-11-11】带上 mode —— 服务端按各模式自己的尺寸策略算。
      // 【2026-11-17】flf / r2v 也放宽到 2.0M（对齐 nonebot seg1/seg2
      // 的 ResolutionSelector(megapixels=2)），现与 t2v 同口径。
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
    // 【2026-11-15 修复③ 交付项 3】按**当前模式**的面积上限过滤。
    // 【2026-11-17】flf / r2v 的上限已放宽到 2.0MP（服务端
    // mode_size_policy 下发），与 t2v 同口径。
    // 超出上限的档位**不出现**在下拉里（不是列出来再置灰，
    // 那是「用户能选、选了必然被服务端改写」的假选项）。
    const cap = maxPixels();
    return (VIDEO_OPTIONS.size_presets || []).filter(p => {
      if (p.aspect !== aspect) return false;
      if (!cap) return true;
      return (p.width * p.height) <= cap;
    });
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

  /**
   * 作者推荐参数的**悬停提示**文本（需求 1 / 2 / 5）。
   *
   * 数据来自服务端 /api/video-options 的
   *   model_recommendations / accel_lora_recommendations
   * （见 h3studio/model_recommendations.py，每条都带来源）。
   *
   * 【铁律】作者没写的项显示「作者未提供」—— 绝不填一个猜测值。
   */
  function recTooltip(rec) {
    if (!rec || typeof rec !== "object") return "";
    const kindLabel = (VIDEO_OPTIONS.rec_source_kind_label || {})[rec.source_kind]
      || rec.source_kind || "";
    const fields = ["steps", "scheduler", "sampler", "shift_video",
                    "shift_audio", "base_model"];
    const labels = VIDEO_OPTIONS.rec_field_label || {};
    const parts = [];
    for (const key of fields) {
      const raw = rec[key];
      const label = labels[key] || key;
      if (raw === undefined || raw === null || raw === "not_provided") {
        parts.push(`${label}：作者未提供`);
      } else {
        parts.push(`${label}：${raw}${recFieldSource(rec, key)}`);
      }
    }
    const head = kindLabel ? `来源：${kindLabel}` : "";
    const where = rec.source ? `\n出处：${rec.source}` : "";
    const note = rec.note ? `\n${rec.note}` : "";
    return head + "\n" + parts.join("\n") + where + note;
  }

  /**
   * 逐字段的来源标注。
   *
   * 【为什么需要】一条记录的各字段可能来自**不同来源** —— 比如
   * speed_slider：步数/采样器/调度器**作者明写未提供**，但 shift 用的是
   * ComfyUI 的 H3 模型硬性值。只标一个整条级的来源，会把 shift 也说成
   * 「作者提供」。这里按字段各自的 field_sources 逐项标注。
   *
   * 形如：「推荐步数：4（作者提供）」「推荐视频 shift：6（作者提供）」
   */
  function recFieldSource(rec, field) {
    const src = rec && rec.field_sources ? rec.field_sources[field] : null;
    if (!src) return "";
    const labels = VIDEO_OPTIONS.rec_source_kind_label || {};
    const label = labels[src];
    return label ? `（${label}）` : "";
  }

  /** 某个主模型的推荐参数（没有登记就是 null）。 */
  function modelRecommendation(key) {
    const map = VIDEO_OPTIONS.model_recommendations || {};
    return map[key] || null;
  }

  /** 某枚加速 LoRA 的推荐参数（没有登记就是 null）。 */
  function accelRecommendation(key) {
    const map = VIDEO_OPTIONS.accel_lora_recommendations || {};
    return map[key] || null;
  }

  function renderModelSelect() {
    const sel = document.getElementById("vidModelSelect");
    if (!sel) return;
    sel.innerHTML = "";
    // 【2026-11-15 修复③ 交付项 3】按当前模式过滤底模
    // （r2v 只列 Ref2VA 权重；flf / t2v 排除 Ref2VA 专用权重）。
    const entries = modelsForMode();
    entries.forEach(([key, m]) => {
      const opt = document.createElement("option");
      opt.value = key;
      const status = m.status || (m.available === false ? "missing" : "available");
      const unavailable = status !== "available";
      opt.textContent = modelOptionLabel(m);
      opt.disabled = unavailable;
      opt.selected = key === videoState.model;
      // title 里带上**真实**的权重文件名与状态，便于排查
      // 【2026-11-13 需求 2/5】再附上作者推荐参数（步数/调度器/采样器/shift）。
      const rec = modelRecommendation(key);
      const recText = recTooltip(rec);
      opt.title = `${m.unet || ""}${m.status_note ? "\n" + m.status_note : ""}`
        + (recText ? `\n\n—— 作者推荐参数 ——\n${recText}` : "");
      sel.appendChild(opt);
    });
    // 当前选中的模型在**本模式**下不可用 -> 回落到第一个可用模型
    const model = currentModel();
    const curStatus = model
      ? (model.status || (model.available === false ? "missing" : "available"))
      : "missing";
    const modeModels = entries.map(([key]) => key);
    const inMode = modeModels.includes(videoState.model);
    if (!model || curStatus !== "available" || !inMode) {
      const fallbackKey = modeModels.find(k => {
        const m = VIDEO_OPTIONS.models[k];
        const st = m.status || (m.available === false ? "missing" : "available");
        return st === "available";
      }) || modeModels[0] || VIDEO_OPTIONS.default_model;
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

  /**
   * 加速 LoRA 的说明文案。
   *
   * 【2026-11-14 重写 —— 文案必须与实际行为一致】
   * 旧文案说「工作流固定使用 Turbo LoRA，这一枚始终生效、无法关闭，
   * 下拉里选的是额外叠加的第二枚」。那是**当时的真实行为**，但它正是
   * 用户报的缺陷本身（选了不生效）。现在加速 LoRA 变成一个槽位，
   * 所以文案也必须改成「你选哪枚，工作流就只挂哪枚」。
   *
   * 还要如实说明**加载节点**（哪些权重用哪个节点加载）——
   * 因为用错节点会导致 LoRA 静默失效（出片照跑但没生效）。
   */
  function renderFixedLoraNote() {
    const box = document.getElementById("vidFixedLoraNote");
    if (!box) return;
    const meta = videoState.accelLora ? accelMeta(videoState.accelLora) : null;
    if (!meta) {
      box.hidden = false;
      box.textContent = "当前「不挂」加速 LoRA —— 工作流走标准多步采样"
        + "（默认 20 步 / simple 调度器 / res_multistep 采样器）。"
        + "想要 4 步出片，请在上方下拉里选一枚加速 LoRA。";
      box.title = "";
      return;
    }
    box.hidden = false;
    const rec = accelRecommendation(videoState.accelLora);
    const recText = recTooltip(rec);
    box.title = recText || "";
    const loaderText = meta.loader === "LoraLoaderModelOnly"
      ? "核心 LoraLoaderModelOnly 加载"
      : "Turbo 节点加载";
    box.textContent = `当前加速 LoRA：「${meta.display}」`
      + `（${loaderText}，强度 ${Number(videoState.accelLoraStrength).toFixed(2)}）`
      + `—— 工作流里「只挂这一枚」，你选什么就加载什么。`
      + (meta.note ? `\n${meta.note}` : "")
      + (recText ? `\n作者推荐：${recText.split("\n").join(" · ")}` : "");
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
    return accelLoraList().filter(item => {
      if (item.available === false) return false;
      if (item.selectable === false) return false;
      if (item.compatible === false) return false;
      return true;
    });
  }

  function renderAccelLoraSelect() {
    const sel = document.getElementById("vidAccelLoraSelect");
    if (!sel) return;
    const selectable = selectableAccelLoras();

    sel.innerHTML = "";
    // 「不挂加速 LoRA」—— 工作流里**只挂你选的那一枚**，
    // 选这一项就是一枚都不挂（走标准多步采样）。
    const noneOpt = document.createElement("option");
    noneOpt.value = "";
    noneOpt.textContent = "不挂加速 LoRA（走标准多步采样）";
    sel.appendChild(noneOpt);

    selectable.forEach(item => {
      const opt = document.createElement("option");
      opt.value = item.key;
      opt.textContent = item.display;
      // 【2026-11-13 需求 1/5】title 里附上 LoRA 作者推荐的参数。
      const recText = recTooltip(accelRecommendation(item.key));
      // 附上**加载节点** —— 用错节点会导致 LoRA 静默失效
      // （出片照跑但没生效），用户需要能看见这一点。
      const loaderText = item.loader === "LoraLoaderModelOnly"
        ? "核心 LoraLoaderModelOnly" : "Turbo 节点";
      opt.title = `${item.name}｜推荐强度 ${item.recommended_strength}`
        + `｜加载节点：${loaderText}`
        + (recText ? `\n\n—— 作者推荐参数 ——\n${recText}` : "");
      sel.appendChild(opt);
    });

    // 当前选中的不再适配（切了主模型）-> 自动回落成「不挂」
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
    const all = accelLoraList();
    const selectable = selectableAccelLoras();
    const parts = [];

    const meta = videoState.accelLora ? accelMeta(videoState.accelLora) : null;
    if (meta) {
      const loaderText = meta.loader === "LoraLoaderModelOnly"
        ? "核心 LoraLoaderModelOnly" : "Turbo 节点";
      parts.push(`已选「${meta.display}」×${Number(videoState.accelLoraStrength).toFixed(2)}`
        + `（${loaderText}加载，工作流里只挂这一枚）`);
    } else {
      parts.push("未挂加速 LoRA —— 工作流走标准多步采样。");
    }
    // 被过滤掉的数量 + 原因（说清楚「为什么少了几项」）
    const blocked = all.length - selectable.length;
    if (blocked > 0) {
      const reasons = all
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

  // ------------------------------------------------------------ 效果 LoRA

  /**
   * 效果 LoRA 区块的渲染入口。
   *
   * 清单由**独立模块** `web/video-effect-lora.js` 负责（它自己调
   * /api/video-effect-loras）。这里只做两件事：
   *   1. 确保清单已按当前底模加载过
   *   2. 把已选列表渲染出来
   *
   * 【为什么要单独一个模块】效果 LoRA 的交互（搜索 + 多选勾选 +
   * 逐项滑杆 + 触发词）自成一块，塞进 video-panel.js 会让那个文件
   * 继续膨胀。模块之间只通过 window.VIDEO_EFFECT_LORA 通信。
   */
  function renderEffectLoras() {
    const eff = window.VIDEO_EFFECT_LORA;
    if (!eff) return;
    // 底模换了要重拉（兼容性随底模变）
    if (eff.store.model !== videoState.model) {
      eff.load(videoState.model);
    }
    eff.render();
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
      // 【2026-11-15 修 AC-1】「推荐步数」的基准是**当前加速 LoRA**
      // （有 LoRA -> 它的 recommended_steps；不挂 -> 20 步标准多步档），
      // 而不是底模的 steps（底模恒为 4 = 蒸馏档，写死它会一直显示错）。
      const accelMetaNow = videoState.accelLora ? accelMeta(videoState.accelLora) : null;
      const recommended = accelMetaNow
        ? (accelMetaNow.recommended_steps != null ? Number(accelMetaNow.recommended_steps) : null)
        : (cfg.steps_without_accel != null ? Number(cfg.steps_without_accel) : null);
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
    //
    // 【2026-11-16 修 D-6 —— 读错了层级，禁用判据恒不生效】
    // 后端把 `sigma_shift_available` 放在 payload **顶层**
    // （h3studio/video_models.py:2396），而 `cfg` 在这里是
    // `samplingCfg()` = `VIDEO_OPTIONS.sampling`（**子对象**）。
    // 旧实现读 `cfg.sigma_shift_available` -> 恒 undefined
    // -> `undefined !== false` -> **恒 true** -> 滑杆永远不禁用。
    //
    // 修法：优先读顶层（VIDEO_OPTIONS.sigma_shift_available），
    // 保留对 sampling 子对象的兼容（万一后端改位置）。
    const shiftAvailable = (VIDEO_OPTIONS.sigma_shift_available !== undefined
      ? VIDEO_OPTIONS.sigma_shift_available
      : cfg.sigma_shift_available) !== false;
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
    // ---- sigma 精修控件（2026-11-13 借鉴 nonebot H3 工作流）----
    const refineOk = cfg.sigma_refine_node_available !== false;
    const refineOn = !!(videoState.twopass.sigma_refine
      && videoState.twopass.sigma_refine.enabled);
    const refineBox = document.getElementById("vidSigmaRefineEnabled");
    if (refineBox) {
      refineBox.checked = refineOn;
      refineBox.disabled = !refineOk;
    }
    const refineHint = document.getElementById("vidSigmaRefineHint");
    if (refineHint) {
      refineHint.textContent = refineOk
        ? "低噪段加密采样（借鉴 nonebot H3 工作流）"
        : "本机 ComfyUI 没有 H3SigmaRefiner 节点，不可用";
    }
    const refineSteps = document.getElementById("vidSigmaRefineSteps");
    const refineStart = document.getElementById("vidSigmaRefineStart");
    const refineSpacing = document.getElementById("vidSigmaRefineSpacing");
    const refine = videoState.twopass.sigma_refine || {};
    if (refineSteps) {
      refineSteps.value = refine.extra_steps != null ? refine.extra_steps : 1;
      refineSteps.disabled = !refineOk || !refineOn;
      const label = document.getElementById("vidSigmaRefineStepsVal");
      if (label) label.textContent = refine.extra_steps != null ? refine.extra_steps : 1;
    }
    if (refineStart) {
      refineStart.value = refine.start_at_sigma != null ? refine.start_at_sigma : 2.5;
      refineStart.disabled = !refineOk || !refineOn;
      const label = document.getElementById("vidSigmaRefineStartVal");
      if (label) label.textContent = Number(refine.start_at_sigma != null
        ? refine.start_at_sigma : 2.5).toFixed(2);
    }
    if (refineSpacing) {
      refineSpacing.value = refine.spacing || "cosine";
      refineSpacing.disabled = !refineOk || !refineOn;
    }
    const tpSampler = document.getElementById("vidTwopassSampler");
    if (tpSampler) tpSampler.value = "euler";

    // 【2026-11-16 修 D-3 —— 主采样器下拉在二采开启时必须置灰】
    // SelfLift 节点只支持标准 Euler，所以二采开启时工作流里的
    // KSamplerSelect 是**写死 euler** 的（h3studio/video_workflow.py:779
    // `forced_sampler = "euler"`；flf/r2v 同理见 h3_video_modes.py:308）。
    //
    // 旧实现不管这个：主流采样器下拉 `#vidSamplerSelect` 照常可拖，
    // 用户选 dpm_2 -> 后端**保留**用户的 sampler（不报错、不夹回）
    // -> 落库与参数卡都显示「采样器 dpm_2」-> 实际跑的却是 euler。
    // **界面说谎**。
    //
    // 修法：二采开启时把主采样器下拉**置灰 + 说明原因**。
    // 不改后端（euler 是节点硬约束，不是可以协商的参数），
    // 改的是「让界面如实反映实际执行」。
    const mainSampler = document.getElementById("vidSamplerSelect");
    const twopassOn = !!videoState.twopass.enabled && nodeOk;
    if (mainSampler) {
      mainSampler.disabled = twopassOn;
      mainSampler.title = twopassOn
        ? "二采（SelfLift）只支持标准 Euler 采样器 —— 开启二采时实际使用 euler"
        : "";
    }
    const mainSamplerHint = document.getElementById("vidSamplerHint");
    if (mainSamplerHint && twopassOn) {
      mainSamplerHint.textContent = "二采固定 euler";
      mainSamplerHint.title = "二采（SelfLift）节点只支持标准 Euler 采样器";
    }

    if (note) {
      const bits = [cfg.note || ""];
      if (videoState.twopass.enabled) {
        bits.push(`一采 ${videoState.twopass.transition_step} 步 @ `
          + `${Number(videoState.twopass.lowres_scale).toFixed(2)}× 分辨率`);
        // 【2026-11-17】NFE 预算提示：高分辨率二采只剩 1 步时，
        // 段头会色档掉档（接续点闪烁）。服务端会自动夹到「高分辨率至少
        // 拿一半预算」，这里提前告诉用户会怎么夹。
        const _ts = Number(videoState.twopass.transition_step) || 0;
        const _steps = Number(videoState.steps) || 0;
        if (_steps > 0 && _ts > 0) {
          const _high = _steps - _ts;
          const _maxTs = Math.max(1, _steps - Math.ceil(_steps / 2));
          if (_ts > _maxTs) {
            bits.push(`（高分辨率二采只剩 ${_high} 步，`
              + `服务端会自动把一采步数夹到 ${_maxTs} 步，`
              + `否则段头会色档掉档＝接续点闪烁）`);
          }
        }
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

  function renderVideoPanel(mode) {
    // 【2026-11-15 修复③ 交付项 3】首尾帧（flf）/ 参考生视频（r2v）
    // 现在**共用**文生视频这一套面板 —— 同一份渲染代码、同一套分块、
    // 同一套控件顺序，只在必要处多出本模式特有的素材字段。
    //
    // 尺寸策略按 mode 取，不写死（服务端 /api/video-options 的
    // mode_size_policy 逐模式下发：t2v / flf / r2v 均为 2.0MP）。
    VIDEO_OPTIONS.mode = mode || videoState.mode || "t2v";
    videoState.mode = VIDEO_OPTIONS.mode;
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
      ["效果LoRA", renderEffectLoras],
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
    // 【2026-11-15 修复③】上面的渲染会**程序化**写 range.value
    // （不触发 input/change 事件），配套的数字输入框会停留在旧值 ——
    // 界面上出现「滑杆 8、数字框 4」的自相矛盾。
    // 这里统一同步一次（共用组件的入口，见 web/numeric-slider.js）。
    syncNumericInputs();
  }

  /** 把所有滑杆的当前值刷进配套的数字输入框（渲染后调用）。 */
  function syncNumericInputs() {
    const ns = window.NumericSlider;
    if (ns && typeof ns.syncAll === "function") {
      try { ns.syncAll(); } catch (e) { /* 忽略：不影响面板本身 */ }
    }
  }

  // ------------------------------------------------------------ 对外接口
  /**
   * 暴露给其它模块（首尾帧 / 参考生视频的旧面板）使用。
   *
   * 【为什么需要】flf / r2v 走的是旧面板（index.html 的 #modelCards /
   * #loraCards），但**加速 LoRA 必须与文生视频同一套语义**
   * （一个槽位、同一份清单、同一套兼容性过滤）——
   * 否则同一个 LoRA 在两个面板里行为不同，用户会以为「时灵时不灵」。
   *
   * 返回 [{key, display, name, recommended_strength, loader, loader_label, ...}]
   */
  function accelOptions() {
    return selectableAccelLoras().map(item => ({
      key: item.key,
      display: item.display,
      name: item.name,
      recommended_strength: item.recommended_strength,
      recommended_steps: item.recommended_steps,
      loader: item.loader,
      loader_label: item.loader_label,
      note: item.note,
      purpose: item.purpose,
    }));
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
      // 【2026-11-15 修复③ 交付项 3】按模式存**各自**的配置 ——
        // flf / r2v 与 t2v 共用同一套面板，但**尺寸上限不同**
        // （0.8MP vs 2.0MP），底模可选集也不同。
        // 存成 per-mode 的一份，回填时按当前模式取 ——
        // 否则切到 flf 看到的是 t2v 的 2.0MP 尺寸（超上限），
        // 切回 t2v 又被本模式的尺寸上限覆盖（白白浪费画质）。
        mode_configs: modeConfigs(),
      };
  }

  /**
   * 逐模式的配置片段（只存「与模式相关」的字段）。
   *
   * 【为什么要拆开存】
   * 三个视频模式共用一套面板，但：
   *   * 尺寸上限不同（按 mode_size_policy 逐模式取值）；
   *   * 底模可选集不同（r2v 只能用 Ref2VA 权重）；
   *   * 采样默认值不同（跟着实际挂的加速 LoRA 走）。
   * 用**一份**共享配置的话，切换模式会互相污染 ——
   * 在 t2v 选了 1920×1088，切到 flf 就超上限；
   * 切回 t2v 又被本模式的尺寸上限覆盖。
   */
  function modeConfigs() {
    const configs = (videoState.modeConfigs && typeof videoState.modeConfigs === "object")
      ? videoState.modeConfigs : {};
    const key = videoState.mode || "t2v";
    configs[key] = {
      model: videoState.model,
      aspect: videoState.aspect,
      megapixels: videoState.megapixels,
      custom: videoState.custom,
      width: videoState.width,
      height: videoState.height,
      rawWidth: videoState.rawWidth,
      rawHeight: videoState.rawHeight,
      size_preset_key: videoState.sizePresetKey,
      accel_lora: videoState.accelLora,
      accel_lora_strength: Number(videoState.accelLoraStrength),
      effect_loras: (videoState.effectLoras || []).map(it => ({
        name: it.name,
        strength: Number(it.strength),
        enabled: it.enabled !== false,
        trigger: it.trigger || "",
      })),
      attention: videoState.attention,
      twopass: { ...videoState.twopass,
        sigma_refine: { ...(videoState.twopass.sigma_refine || {}) },
      },
      steps: videoState.steps,
      sampler: videoState.sampler,
      scheduler: videoState.scheduler,
      shift_video: Number(videoState.shiftVideo),
      shift_audio: Number(videoState.shiftAudio),
    };
    return configs;
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

  /**
   * 【2026-11-15 修复③ 交付项 3】切换面板服务的生成模式。
   *
   * 首尾帧（flf）/ 参考生视频（r2v）与文生视频（t2v）**共用同一套面板**，
   * 切模式时要：
   *   1. 记住**上一个模式**的配置片段（否则切回来参数全丢）；
   *   2. 载入**新模式**的配置片段（没有就用默认值）；
   *   3. 按新模式的尺寸上限 / 底模可选集重算。
   *
   * 【为什么尺寸要重算】各模式的上限不同（见 mode_size_policy）。
   * 在 t2v 选了 1920×1088（2.0MP）切到 flf -> 超上限，
   * 服务端会按比例缩回去（面板说的和实际跑的不一样 —— 那正是
   * 「面板说谎」的老毛病）。所以这里**在切模式时就收进上限内**。
   */
  function setMode(mode) {
    const next = mode || "t2v";
    const prev = videoState.mode || "t2v";
    if (next === prev) {
      VIDEO_OPTIONS.mode = next;
      return;
    }
    // 1) 记住上一个模式
    if (videoState.modeConfigs) {
      const configs = videoState.modeConfigs;
      configs[prev] = {
        model: videoState.model,
        aspect: videoState.aspect,
        megapixels: videoState.megapixels,
        custom: videoState.custom,
        width: videoState.width,
        height: videoState.height,
        rawWidth: videoState.rawWidth,
        rawHeight: videoState.rawHeight,
        size_preset_key: videoState.sizePresetKey,
        accel_lora: videoState.accelLora,
        accel_lora_strength: Number(videoState.accelLoraStrength),
        effect_loras: (videoState.effectLoras || []).map(it => ({
          name: it.name,
          strength: Number(it.strength),
          enabled: it.enabled !== false,
          trigger: it.trigger || "",
        })),
        attention: videoState.attention,
        twopass: { ...videoState.twopass,
                   sigma_refine: { ...(videoState.twopass.sigma_refine || {}) } },
        steps: videoState.steps,
        sampler: videoState.sampler,
        scheduler: videoState.scheduler,
        shift_video: Number(videoState.shiftVideo),
        shift_audio: Number(videoState.shiftAudio),
      };
    }
    // 2) 载入新模式
    videoState.mode = next;
    VIDEO_OPTIONS.mode = next;
    const cfg = (videoState.modeConfigs || {})[next];
    if (cfg && typeof cfg === "object") {
      applyModeConfig(cfg);
    } else {
      // 新模式第一次用：清掉「上一个模式特有」的值，落到默认值。
      videoState.custom = false;
      videoState.width = null;
      videoState.height = null;
      videoState.rawWidth = null;
      videoState.rawHeight = null;
      videoState.sizePresetKey = "";
      videoState.sizeReasons = [];
    }
    // 3) 尺寸收进本模式的上限
    clampSizeToModeLimit();
  }

  /**
   * 把当前宽高收进**本模式**的面积上限（与 /api/video-size 同口径：
   * 超限就按比例缩小并吸附到 32 的倍数）。
   *
   * 【为什么前端也要做一遍】服务端 h3_video_size.apply_size 也会做 ——
   * 但那是**提交时**的静默修正。面板上先收进来，用户看到的尺寸
   * 就等于真正会跑的尺寸（不再「面板说一套、工作流跑另一套」）。
   */
  function clampSizeToModeLimit() {
    const cap = maxPixels();
    if (!cap) return;
    const [w, h] = currentSize();
    if (!w || !h) return;
    if (w * h <= cap) return;
    const mult = VIDEO_OPTIONS.size_multiple || 32;
    const scale = Math.sqrt(cap / (w * h));
    let nw = Math.max(mult, Math.round(w * scale / mult) * mult);
    let nh = Math.max(mult, Math.round(h * scale / mult) * mult);
    // 吸附后可能又超回上限 -> 逐次收一档
    while (nw * nh > cap && (nw > mult || nh > mult)) {
      if (nw >= nh && nw > mult) nw -= mult;
      else if (nh > mult) nh -= mult;
      else break;
    }
    videoState.width = nw;
    videoState.height = nh;
    videoState.rawWidth = nw;
    videoState.rawHeight = nh;
    videoState.sizePresetKey = presetKeyForSize(nw, nh);
    videoState.sizeReasons = [
      `已按 ${videoState.mode} 模式的上限 `
      + `${(cap / (1024 * 1024)).toFixed(1)}MP 收进 ${nw}×${nh}`,
    ];
  }

  function applyConfig(cfg) {
    if (!cfg || typeof cfg !== "object") return;
    // 【2026-11-15 修复③ 交付项 3】先把 mode_configs 里的**其它模式**
    // 的片段收进来（供切换模式时取用），再按**当前模式**回填。
    if (cfg.mode_configs && typeof cfg.mode_configs === "object") {
      videoState.modeConfigs = {
        ...(videoState.modeConfigs || {}),
        ...cfg.mode_configs,
      };
    }
    // 当前模式的专属片段（若有）优先于顶层字段 ——
    // 顶层字段是老配置（只有 t2v 一套）的兼容路径。
    //
    // 【2026-11-16 修 D-2 —— 顶层字段被 mode_configs 整块盖掉】
    // 旧实现：只要 mode_configs[当前模式] 存在，就用它并 return，
    // **完全忽略**调用方传入的顶层字段。
    // 于是「复用参数」（app.js 的 reuseConfigFromJob 传的是**平铺**的
    // steps/sampler/scheduler/shift_video/shift_audio）会被面板**自己上次**
    // 的 mode_configs 覆盖 -> 用户点「复用参数」看到的是面板旧状态，
    // 不是那条任务的参数。
    //
    // 实测（修复前，线上真实浏览器）：把面板改成 7 步 -> 点某条任务的
    // 「复用参数」（那条任务是 4 步）-> 面板仍是 7 步（期望 4）。
    //
    // 【修法】顶层字段**优先**；mode_configs 里当前模式的片段只补充
    // 顶层没给的字段（尺寸类字段顶层不给，只能靠 mode_configs）。
    // 「复用参数」走的就是顶层路径 -> 用户的意图被尊重；
    // 载入保存配置（saved）时顶层本来就没有采样档 ->
    // 仍会落到 mode_configs 的片段上，行为不变。
    const modeKey = videoState.mode || "t2v";
    const modeCfg = (videoState.modeConfigs || {})[modeKey];
    let merged = {};
    if (modeCfg && typeof modeCfg === "object") Object.assign(merged, modeCfg);
    // 顶层字段覆盖 mode_configs 的片段（顶层 = 调用方的显式意图）
    for (const key of Object.keys(cfg)) {
      if (key === "mode_configs") continue;
      merged[key] = cfg[key];
    }
    applyModeConfig(merged);
  }

  /** 把一份配置片段落到状态上（顶层字段与 mode_configs 片段共用）。 */
  function applyModeConfig(cfg) {
    if (!cfg || typeof cfg !== "object") return;
    if (cfg.model && VIDEO_OPTIONS.models[cfg.model]) videoState.model = cfg.model;
    if (cfg.aspect) videoState.aspect = cfg.aspect;
    // 【2026-11-16 审查 B-5 修复】时长回填 —— 与 app.js 的 state.duration
    // 保持一致（app.js 那边是唯一真源，这里是镜像）。
    if (cfg.duration != null) {
      const d = Number(cfg.duration);
      if (Number.isFinite(d) && d > 0) videoState.duration = d;
    }
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
    // 【2026-11-14】加速 LoRA 只有一个槽位。
    // 老配置里存的是 fixed_accel_lora（恒生效的那枚）—— 迁移成槽位值，
    // 这样老配置恢复后语义仍然对（那一枚就是要挂的那枚）。
    if (cfg.fixed_accel_lora && !cfg.accel_lora) {
      videoState.accelLora = cfg.fixed_accel_lora;
    }
    // 效果 LoRA（可多选）—— 复用参数时一并恢复
    //
    // ⚠️ 【这里只「暂存」，不直接落地】
    // 效果 LoRA 的清单是**另一个接口**（/api/video-effect-loras），
    // 此刻还没到。直接落地的话，随后清单到达时的「按当前底模剔除」
    // 无法区分「刚恢复的」和「本来就没有的」-> 会出现选中项随机丢失。
    // 所以交给效果 LoRA 模块在清单到达后应用（stageRestore / restoreSelection）。
    const eff = window.VIDEO_EFFECT_LORA;
    if (eff && typeof eff.stageRestore === "function") {
      eff.stageRestore(Array.isArray(cfg.effect_loras) ? cfg.effect_loras : []);
    } else if (Array.isArray(cfg.effect_loras)) {
      videoState.effectLoras = cfg.effect_loras
        .filter(it => it && it.name)
        .map(it => ({
          name: String(it.name),
          strength: Number(it.strength) || 1.0,
          enabled: it.enabled !== false,
          trigger: it.trigger || "",
        }));
    } else if (cfg.effect_loras === undefined) {
      // 老配置没有这个字段 -> 清空（不能残留上一张卡片的值）
      videoState.effectLoras = [];
    }
    // 【单选】新格式
    //
    // 【为什么用 `!== undefined` 判「显式给了」】`cfg.accel_lora` 为
    // 空串是**有意义的**（「不额外挂加速 LoRA」）—— 不能与「没给这个
    // 字段」混为一谈，否则「复用参数」一张没挂 LoRA 的卡片时，
    // 上一张卡片挂的 LoRA 会**残留**在面板上（混合态的又一种形态）。
    const hasNewAccel = cfg.accel_lora !== undefined;
    if (hasNewAccel) {
      videoState.accelLora = cfg.accel_lora || "";
      if (cfg.accel_lora_strength !== undefined) {
        videoState.accelLoraStrength = Number(cfg.accel_lora_strength);
      }
    }
    // 【兼容旧格式】老配置里存的是 accel_loras 数组（可能多项）——
    // 取第一项启用且强度 > 0 的，落到新的单选字段上。
    //
    // 【为什么要先归零】旧格式是「完整表达这个任务的加速 LoRA 配置」，
    // 不是「增量」。数组为空 = 这个任务**没有**额外加速 LoRA，
    // 此时必须把面板上残留的值清掉，否则会带上一张卡片的 LoRA。
    if (!hasNewAccel && Array.isArray(cfg.accel_loras)) {
      videoState.accelLora = "";
      const first = cfg.accel_loras.find(
        l => l && l.key && l.enabled !== false && Number(l.strength) > 0);
      if (first) {
        videoState.accelLora = first.key;
        videoState.accelLoraStrength = Number(first.strength || 1);
      }
    }
    if (cfg.attention) videoState.attention = cfg.attention;
    if (cfg.twopass && typeof cfg.twopass === "object") {
      if (cfg.twopass.sigma_refine && typeof cfg.twopass.sigma_refine === "object") {
        videoState.twopass.sigma_refine = {
          ...(videoState.twopass.sigma_refine || {}),
          ...cfg.twopass.sigma_refine,
        };
      }
      videoState.twopass = { ...videoState.twopass, ...cfg.twopass };
    }
    // 【2026-11-11】采样参数
    if (cfg.steps != null) videoState.steps = Number(cfg.steps) || videoState.steps;
    if (cfg.sampler) videoState.sampler = cfg.sampler;
    if (cfg.scheduler) videoState.scheduler = cfg.scheduler;
    if (cfg.shift_video != null) videoState.shiftVideo = Number(cfg.shift_video);
    if (cfg.shift_audio != null) videoState.shiftAudio = Number(cfg.shift_audio);
  }

  /**
   * 保存文生视频面板配置。
   *
   * @param {object} [override] 本次要写的配置（与 collectConfig() 合并）。
   * @param {object} [opts]
   * @param {boolean} [opts.immediate] true = **不等节流**，立刻发请求。
   *        （效果 LoRA 在页面卸载时要 flush，走这条路径。）
   * @param {boolean} [opts.keepalive] true = 用 fetch keepalive，
   *        让请求在页面关闭后仍能送达（最多 64KB，配置体积远小于此）。
   */
  function saveVideoConfig(override, opts) {
    const immediate = !!(opts && opts.immediate);
    const keepalive = !!(opts && opts.keepalive);
    const doSave = async () => {
      try {
        // 【2026-11-13 效果 LoRA】override 是可选的「本次要写的配置」。
        // 效果 LoRA 模块会传入它 —— 但**默认路径不传**，
        // 此时用 collectConfig() 现读状态（它已经包含 effect_loras）。
        const body = (override && typeof override === "object")
          ? { ...collectConfig(), ...override }
          : collectConfig();
        const resp = await fetch("/api/video-config", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          // 页面卸载时的兜底 flush：请求必须活过页面销毁。
          keepalive: keepalive,
        });
        if (!resp.ok) {
          const data = await resp.json().catch(() => ({}));
          console.warn("[视频面板] 配置保存失败:", data.error || resp.status);
        }
      } catch (e) {
        console.warn("[视频面板] 配置保存失败:", e.message);
      }
    };
    if (immediate) {
      clearTimeout(saveTimer);
      saveTimer = null;
      return doSave();
    }
    clearTimeout(saveTimer);
    saveTimer = setTimeout(doSave, 300);
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
      // 【2026-11-14】兜底清单也统一成一组（accel_loras）。
      if (!VIDEO_OPTIONS.accel_loras || !VIDEO_OPTIONS.accel_loras.length) {
        const fbList = Array.isArray(fb.accel_loras) ? fb.accel_loras
          : (Array.isArray(fb.fixed_accel_loras) ? fb.fixed_accel_loras : []);
        VIDEO_OPTIONS.accel_loras = fbList;
        VIDEO_OPTIONS.fixed_accel_loras = fbList;
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
      // 【2026-11-13】把来源类别标签表挂到 window —— web/job-meta.js 用它
      // 给推荐参数的**逐字段**标注来源（作者提供 / ComfyUI 模型硬性值 …）。
      window.JOB_META_SOURCE_KIND_LABEL = data.rec_source_kind_label || {};
      optionsReady = true;

      // 默认值
      if (!videoState.model) videoState.model = data.default_model || "";
      // 【2026-11-14】默认加速 LoRA 槽位 = 空（不挂）——
      // 「不挂」必须能选出来，所以不能默认给 ckpt850。
      if (!videoState.accelLora && data.default_accel_lora) {
        videoState.accelLora = data.default_accel_lora;
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
        if (cfgResp.ok) {
          const saved = await cfgResp.json();
          applyConfig(saved);
          // 【2026-11-15 修 AC-1】保存的配置里没有采样档（用户从没手动调过）
          // => 按当前加速 LoRA 的推荐档重算一次；带了就用用户的，不覆盖。
          //
          // 【2026-11-16 修 D-1】采样档存在 mode_configs[当前模式] 里
          // （顶层不写 steps）-> 守卫必须读那一层；旧实现读顶层 -> 恒真
          // -> 每次刷新都用推荐档覆盖用户改的值。
          // 详见 docs/MOSS-Image参数生效性修复报告-20261116.md。
          const savedModeKey = videoState.mode || "t2v";
          const savedModeCfg = (saved.mode_configs
                               && saved.mode_configs[savedModeKey]) || {};
          const savedSteps = savedModeCfg.steps != null
            ? savedModeCfg.steps : saved.steps;
          if (savedSteps == null) recomputeSamplingForAccel();
        }
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
      VIDEO_OPTIONS.accel_loras = data.accel_loras
        || data.fixed_accel_loras || data.accel_lora_combos || [];
      VIDEO_OPTIONS.fixed_accel_loras = VIDEO_OPTIONS.accel_loras;
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
    // 效果 LoRA（可多选）—— 与加速 LoRA 并列共存。
    // 只带**启用且强度 > 0** 的项（强度 0 = 关闭，服务端也会再过滤一次）。
    const effectLoras = (videoState.effectLoras || [])
      .filter(it => it && it.name && it.enabled !== false
                    && Number(it.strength) > 0)
      .map(it => ({
        name: it.name,
        strength: Number(it.strength),
        enabled: true,
        trigger: it.trigger || "",
      }));
    return {
      model_key: videoState.model,
      width: w,
      height: h,
      aspect: videoState.aspect,
      // 【2026-11-16 审查 B-5 修复】时长从**本模块自己的** videoState 读，
      // 不再裸引用 app.js 的全局 `state`。
      //
      // 【原来错在哪】本文件是 IIFE（顶层 `(() => { ... })();`），
      // 内部**没有** state 这个变量；1879 行写的 `state.duration` 实际是
      // 引用 app.js 里那个**模块级 const state**（script 作用域跨文件可见）。
      // 加载顺序还是反的（index.html 先加载 video-panel.js，后加载 app.js），
      // 只因 submitParams() 只在运行时调用才没触发 TDZ。
      // 任何一次「谁把 app.js 的 state 改名 / 包进 IIFE / 改成 let」的
      // 改动，都会让这一行立刻 ReferenceError -> submitParams() 抛错
      // -> 叠加 B-10 修好之前的「无顶层 try/catch」= 生成按钮彻底无响应。
      //
      // 【修法】时长是**视频面板自己的参数**，就该在 videoState 里。
      // app.js 那边在切换/复用参数时同步过来（见 setMode / applyConfig）。
      duration: Number(videoState.duration) || 0,
      // 【2026-11-11】采样参数 —— 真正传进工作流。
      // 注意：这里**不再**用全局 state.steps（旧面板的通用滑杆），
      // 视频面板有自己的步数控件（vidSteps），服务端会用这里的值。
      steps: videoState.steps,
      sampler: videoState.sampler,
      scheduler: videoState.scheduler,
      shift_video: Number(videoState.shiftVideo),
      shift_audio: Number(videoState.shiftAudio),
      // 【2026-11-16】种子只允许正整数 —— 校验实现见 web/seed-guard.js
      // （与后端 h3studio/seed_guard.py 同口径）。
      //
      // 【为什么这里原样带出、让上层拦】submitParams() 是**纯收集**
      // 函数。它若在这里把非法值「归一成 null」（= 随机），上层
      // （app.js 的提交入口）拿到的就是「随机」—— 非法值被**静默吞掉**，
      // 用户填了「abc」，系统当成「没填」，跑出一个随机结果。
      //
      // 真正的拦截在 app.js 的提交入口：它先校验 #seed，非法就报错
      // 并**不发起请求**；合法则被归一成数字再提交。
      seed: $("#seed").value.trim() || null,
      // 【2026-11-14】加速 LoRA 槽位（单选，最多一项；空 = 不挂）
      accel_loras: accel,
      // 【2026-11-15 修 AC-1】显式槽位字段（含空串 = 明确「不挂」）。
      //
      // 服务端 video_models.resolve_accel_lora_slot() 的换算链是：
      //   1. "accel_lora" 键**存在** -> 用它（空串 = 显式「不挂」）
      //   2. accel_loras 数组里有可用项 -> 取第一项
      //   3. fixed_accel_lora -> 再退到 lora -> 最后**默认 ckpt850**
      // 只提交 `accel_loras: []`（空数组）时第 2 步被跳过 -> 一路掉到兜底
      // -> 挂上了 ckpt850（用户明确选了「不挂」却被反转）。
      //
      // 【与 app.js 的分工】app.js 在提交入口也补一份（提交后写回 params，
      // 覆盖面板直传的路径）。这里补一份是为了让**面板自身**的提交体
      // 自洽（面板也用于首尾帧 / 参考生视频等旧面板路径）。
      accel_lora: videoState.accelLora || "",
      accel_lora_strength: Number(videoState.accelLoraStrength),
      // 效果 LoRA（可多选，与加速 LoRA 并列共存）
      effect_loras: effectLoras,
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
        // 【2026-11-13】sigma 精修（借鉴 nonebot H3 工作流的 H3SigmaRefiner）
        sigma_refine: {
          ...(videoState.twopass.sigma_refine || {}),
          enabled: !!(videoState.twopass.sigma_refine
            && videoState.twopass.sigma_refine.enabled),
          extra_steps: Number((videoState.twopass.sigma_refine || {}).extra_steps || 0),
          start_at_sigma: Number((videoState.twopass.sigma_refine || {})
            .start_at_sigma != null ? videoState.twopass.sigma_refine.start_at_sigma : 2.5),
          end_at_sigma: Number((videoState.twopass.sigma_refine || {})
            .end_at_sigma != null ? videoState.twopass.sigma_refine.end_at_sigma : 0),
          spacing: (videoState.twopass.sigma_refine || {}).spacing || "cosine",
        },
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
        // 效果 LoRA 的兼容性也随底模变 -> 重拉清单
        const eff = window.VIDEO_EFFECT_LORA;
        if (eff) eff.load(videoState.model);
        saveVideoConfig();
      });
    }

    // ---- 加速 LoRA（下拉单选）----
    const accelSel = document.getElementById("vidAccelLoraSelect");
    if (accelSel) {
      accelSel.addEventListener("change", e => {
        const previous = videoState.accelLora;
        videoState.accelLora = e.target.value || "";
        const meta = videoState.accelLora ? accelMeta(videoState.accelLora) : null;
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
            // 【2026-11-15 修 AC-1】切换加速 LoRA 时**同步重算采样档**
            // （步数 / 采样器 / 调度器），否则「选了 8 步 LoRA 仍跑 4 步」
            // 「不挂 LoRA 仍跑 4 步蒸馏档」两个坑都还在。
            // 与后端 video_workflow._resolve_accel_chain 的默认档同源。
            recomputeSamplingForAccel();
          }
        } else if (videoState.accelLora !== previous) {
          // 切到「不挂加速 LoRA」-> 标准多步采样档（20 步 / simple / res_multistep）
          recomputeSamplingForAccel();
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

    // ---- sigma 精修控件（2026-11-13 借鉴 nonebot H3 工作流）----
    const refineBox = document.getElementById("vidSigmaRefineEnabled");
    if (refineBox) {
      refineBox.addEventListener("change", () => {
        videoState.twopass.sigma_refine = {
          ...(videoState.twopass.sigma_refine || {}),
          enabled: refineBox.checked,
        };
        renderTwopass();
        saveVideoConfig();
      });
    }
    const refineSteps = document.getElementById("vidSigmaRefineSteps");
    if (refineSteps) {
      refineSteps.addEventListener("input", e => {
        videoState.twopass.sigma_refine = {
          ...(videoState.twopass.sigma_refine || {}),
          extra_steps: parseInt(e.target.value),
        };
        renderTwopass();
      });
      refineSteps.addEventListener("change", saveVideoConfig);
    }
    const refineStart = document.getElementById("vidSigmaRefineStart");
    if (refineStart) {
      refineStart.addEventListener("input", e => {
        videoState.twopass.sigma_refine = {
          ...(videoState.twopass.sigma_refine || {}),
          start_at_sigma: parseFloat(e.target.value),
        };
        renderTwopass();
      });
      refineStart.addEventListener("change", saveVideoConfig);
    }
    const refineSpacing = document.getElementById("vidSigmaRefineSpacing");
    if (refineSpacing) {
      refineSpacing.addEventListener("change", e => {
        videoState.twopass.sigma_refine = {
          ...(videoState.twopass.sigma_refine || {}),
          spacing: e.target.value,
        };
        renderTwopass();
        saveVideoConfig();
      });
    }
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
    // 【2026-11-15 修复③ 交付项 3】切换面板服务的生成模式
    // （t2v / flf / r2v 共用同一套面板）。
    setMode: setMode,
    // 【2026-11-15 修复③ 交付项 3】当前模式下可选的底模清单
    // （r2v 只列 Ref2VA 权重）—— 供 app.js 判断要不要显示模式说明。
    modelsForMode: modelsForMode,
    /**
     * 加速 LoRA 清单（已按当前底模过滤）。
     *
     * 【为什么对外暴露】首尾帧 / 参考生视频走的是旧面板
     * （index.html 的 #modelCards / #loraCards），但加速 LoRA 必须
     * 与文生视频**同一套语义**（一个槽位 / 同一份清单 / 同一套兼容性
     * 过滤）—— 否则同一个 LoRA 在两个面板里行为不同，
     * 用户会以为「时灵时不灵」。
     */
    accelOptions,
    // 【2026-11-16 审查 B-7 修复】currentSize 提为**正式 API**。
    //
    // 【原来错在哪】currentSize 只挂在 _debug 下，而 app.js 的提交前置
    // 闸门（接续分辨率一致性校验）在**生产代码**里读 window.VIDEO_PANEL._debug
    // —— 把「调试出口」当正常 API 用。后果：
    //   * _debug 一旦被当成「可以随便删的测试钩子」清掉，生产链路直接断；
    //   * 命名撒谎 —— 读它的人以为自己在读调试数据，其实读的是业务数据。
    // 现在 currentSize 是正式接口；_debug 只保留真正的测试钩子。
    currentSize,
    // 供验收脚本直接读状态（证明「界面值 == 提交值」）
    _debug: {
      selectableAccelLoras,
      presetsForAspect,
    },
  };
})();
