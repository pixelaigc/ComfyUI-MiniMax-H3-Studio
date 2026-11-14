/**
 * web/job-meta.js —— 生成媒体库卡片的**元数据渲染层**（需求 1/2/3/5 + C1/C2/C3/C4）
 *
 * 【为什么单独一个文件】
 * 用户 2026-11-13 报的 BUG 根因是「前端取值时按位置/模糊匹配，而非按明确 key 读取」
 * —— 提示词字段名 `integrated_multimodal_description` 被当成 LoRA 显示了出来。
 * 修法不是「再小心一点」，而是**把取值口径钉死在一个地方**：
 *   * 参数只能来自 `job.model_info`（服务端算好的权威摘要）；
 *   * 只认**白名单**里的字段，白名单外的键（提示词 / 描述 / 工作流节点名）
 *     一律不渲染；
 *   * 取不到的值显示「—」，**绝不用别的字段顶替**。
 *
 * 本文件是纯函数 + 无 DOM 依赖的取值层，供单测直接调用
 * （web/app.js 里的 jobCard 只负责拼 HTML）。
 */
(() => {
  "use strict";

  // ------------------------------------------------------------------
  // 常量
  // ------------------------------------------------------------------

  /** 取不到值时的占位（用户要求：取不到的值显示「—」，不得用其它字段顶替） */
  const MISSING = "—";

  /**
   * 生成参数的**字段语义白名单**。
   *
   * 【铁律】只有这里列出的键才允许被当成「生成参数」渲染。
   * 提示词、描述、工作流节点名等**不在白名单里** -> 永远不会被误当参数显示。
   *
   * 与服务端 h3studio/job_meta.py 的 GENERATION_PARAM_FIELDS 一一对应
   * （服务端是唯一真源，这里是同一份清单的前端副本）。
   */
  const PARAM_FIELDS = [
    "steps", "sampler", "scheduler", "cfg",
    "shift_video", "shift_audio",
    "width", "height", "aspect", "resolution",
    "duration", "seed",
  ];

  /** 字段 -> 中文名 */
  const PARAM_LABEL = {
    steps: "步数",
    sampler: "采样器",
    scheduler: "调度器",
    cfg: "CFG",
    shift_video: "视频 shift",
    shift_audio: "音频 shift",
    width: "宽",
    height: "高",
    aspect: "画面比例",
    resolution: "分辨率档位",
    duration: "时长（秒）",
    seed: "种子",
  };

  /** 推荐参数字段 -> 中文名 */
  const REC_LABEL = {
    steps: "推荐步数",
    scheduler: "推荐调度器",
    sampler: "推荐采样器",
    shift_video: "推荐视频 shift",
    shift_audio: "推荐音频 shift",
    base_model: "推荐主模型",
  };

  /** 推荐参数里哪些是数值型（判定「未提供」不能靠 falsy） */
  const REC_NUMERIC = ["steps", "shift_video", "shift_audio"];

  /** 「作者未提供」的哨兵（与服务端 model_recommendations.NOT_PROVIDED 一致） */
  const NOT_PROVIDED = "not_provided";

  // ------------------------------------------------------------------
  // 基础工具
  // ------------------------------------------------------------------

  /** 把值转成可显示文本；空 -> MISSING。**不会**用别的字段顶替。 */
  function text(value) {
    if (value === null || value === undefined) return MISSING;
    if (typeof value === "number") {
      return Number.isFinite(value) ? String(value) : MISSING;
    }
    const s = String(value).trim();
    return s ? s : MISSING;
  }

  /** 数值型字段：0 是有效值，不能当空。 */
  function numText(value) {
    if (value === null || value === undefined || value === "") return MISSING;
    const n = Number(value);
    if (!Number.isFinite(n)) return MISSING;
    return String(n);
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;",
      '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  /**
   * 取任务的权威元数据摘要。
   *
   * 只信 `job.model_info`（服务端算好的）。它不存在时返回空对象 ——
   * **不在前端从 params 里猜**（那正是 BUG 的根因）。
   */
  function modelInfo(job) {
    const info = job && job.model_info;
    return (info && typeof info === "object") ? info : {};
  }

  // ------------------------------------------------------------------
  // LoRA（需求 C1：只能来自真实使用的 LoRA 列表）
  // ------------------------------------------------------------------

  /**
   * 该条目**真实使用**的 LoRA 列表。
   *
   * 【C1 的核心】LoRA 行只能来自任务真实使用的 LoRA：
   *   * `info.lora_label` —— 服务端按白名单算出的加速 / 效果 LoRA 显示名
   *     （脏值已在服务端被白名单挡掉）；
   *   * `info.fixed_lora_label` —— 工作流固定的那枚 Turbo LoRA。
   * **不读** `params.lora`（那正是脏值来源），也不从提示词里猜。
   *
   * @returns {Array<{label:string, strength:number|null, fixed:boolean}>}
   *          没有 LoRA 时返回**空数组**（前端据此显示「无」）。
   */
  function loraList(job) {
    const info = modelInfo(job);
    const out = [];
    const label = info.lora_label;
    if (label && String(label).trim()) {
      out.push({
        label: String(label).trim(),
        strength: info.lora_strength === null || info.lora_strength === undefined
          ? null : Number(info.lora_strength),
        fixed: false,
      });
    }
    const fixed = info.fixed_lora_label;
    if (fixed && String(fixed).trim()) {
      out.push({
        label: String(fixed).trim(),
        strength: info.fixed_lora_strength === null
          || info.fixed_lora_strength === undefined
          ? null : Number(info.fixed_lora_strength),
        fixed: true,
      });
    }
    // 效果 LoRA（可多选）—— 每项独立显示，带各自强度
    const effects = info.effect_lora_labels || [];
    for (const item of effects) {
      if (!item || !item.label) continue;
      const strength = item.strength === null || item.strength === undefined
        ? null : Number(item.strength);
      if (strength !== null && !Number.isFinite(strength)) continue;
      if (strength !== null && strength <= 0) continue;
      out.push({
        label: String(item.label).trim(),
        strength,
        fixed: false,
        effect: true,
      });
    }
    return out;
  }

  /**
   * LoRA 行的显示文本。
   *
   * 【C1】没有 LoRA 时显示「**未使用 LoRA**」
   * （2026-11-15 文案变更 —— 用户要求：「缺失时应显示『未使用 LoRA』
   * 而不是错名」；原来的「无」太短、也容易被读成「数据缺失」）。
   *
   * 【2026-11-15 · 口径统一】这里的值来自服务端 model_info，
   * 而 model_info 的 LoRA 字段已被「产物实际挂载清单」覆盖 ——
   * 三套取值路径（model_info / gen_params / 详情接口）现在同源，
   * 不会再出现「卡片一行说无、详情区说有」的情况。
   */
  function loraText(job) {
    const list = loraList(job);
    if (!list.length) return "未使用 LoRA";
    return list.map((item) => {
      // 效果 LoRA 标注「效果：」前缀 —— 与加速 LoRA 区分开
      const name = item.fixed ? `工作流固定：${item.label}`
        : (item.effect ? `效果：${item.label}` : item.label);
      if (item.strength === null || !Number.isFinite(item.strength)) return name;
      return `${name} ×${item.strength.toFixed(2)}`;
    }).join(" · ");
  }

  // ------------------------------------------------------------------
  // 生成参数（需求 C2）
  // ------------------------------------------------------------------

  /**
   * 按**白名单**取出生成参数。
   *
   * @returns {Array<{key:string, label:string, value:string}>}
   *          取不到的值 value === "—"（不顶替）。
   */
  function generationParams(job) {
    const info = modelInfo(job);
    const out = [];
    for (const key of PARAM_FIELDS) {
      const raw = info[key];
      const value = (key === "steps" || key === "shift_video"
        || key === "shift_audio" || key === "width"
        || key === "height" || key === "duration" || key === "cfg"
        || key === "seed")
        ? numText(raw)
        : text(raw);
      out.push({ key, label: PARAM_LABEL[key] || key, value });
    }
    return out;
  }

  /** 卡片上「采样」一行的合成文本（采样器 · 调度器），缺失显示「—」。 */
  function samplingText(job) {
    const info = modelInfo(job);
    const sampler = text(info.sampler);
    const scheduler = text(info.scheduler);
    return `${sampler} · ${scheduler}`;
  }

  /** 卡片上「步数」一行的合成文本（步数 · CFG），缺失显示「—」。 */
  function stepsText(job) {
    const info = modelInfo(job);
    return `${numText(info.steps)} · ${numText(info.cfg)}`;
  }

  /** 分辨率 / 尺寸文本：优先服务端算好的标签，否则用宽高拼。 */
  function sizeText(job) {
    const info = modelInfo(job);
    const w = numText(info.width);
    const h = numText(info.height);
    if (w !== MISSING && h !== MISSING) {
      const aspect = text(info.aspect);
      return aspect !== MISSING ? `${w}×${h} · ${aspect}` : `${w}×${h}`;
    }
    const reso = text(info.resolution);
    return reso;
  }

  /** 时长文本（秒）。 */
  function durationText(job) {
    const info = modelInfo(job);
    const d = numText(info.duration);
    return d === MISSING ? MISSING : `${d}s`;
  }

  /** 种子文本（空 -> 「随机」）。 */
  function seedText(job) {
    const info = modelInfo(job);
    const s = numText(info.seed);
    return s === MISSING ? "随机" : s;
  }

  // ------------------------------------------------------------------
  // 作者推荐参数（需求 1 / 2 / 5）
  // ------------------------------------------------------------------

  /**
   * 某份推荐参数的展示项。
   *
   * @param {object} rec   推荐参数对象（model_recommendation 等）
   * @returns {Array<{key,label,value,provided}>}
   *          provided=false 的项显示「作者未提供」。
   */
  function recItems(rec) {
    const source = rec && typeof rec === "object" ? rec : {};
    const out = [];
    for (const key of Object.keys(REC_LABEL)) {
      const raw = source[key];
      let provided = true;
      let value;
      if (raw === undefined || raw === null || raw === NOT_PROVIDED) {
        provided = false;
        value = "作者未提供";
      } else if (REC_NUMERIC.includes(key)) {
        value = numText(raw);
        provided = value !== MISSING;
        if (!provided) value = "作者未提供";
      } else {
        value = text(raw);
        provided = value !== MISSING;
        if (!provided) value = "作者未提供";
      }
      out.push({
        key,
        label: REC_LABEL[key],
        value,
        provided,
        source: provided ? recFieldSource(rec, key) : "",
      });
    }
    return out;
  }

  /**
   * 逐字段的来源标注（可追溯）。
   *
   * 一条记录的各字段可能来自**不同来源**（如 speed_slider：步数作者未提供，
   * 但 shift 是 ComfyUI 模型硬性值）。这里按 field_sources 逐项标注。
   */
  function recFieldSource(rec, key) {
    const src = rec && rec.field_sources ? rec.field_sources[key] : null;
    if (!src) return "";
    const labels = window.JOB_META_SOURCE_KIND_LABEL || {};
    const label = labels[src];
    return label ? `（${label}）` : "";
  }

  /** 推荐参数的来源说明（可追溯）。 */
  function recSource(rec) {
    const source = rec && typeof rec === "object" ? rec : {};
    const kind = source.source_kind || NOT_PROVIDED;
    const kindLabel = (window.JOB_META_SOURCE_KIND_LABEL || {})[kind] || kind;
    const where = source.source || "";
    const note = source.note || "";
    const parts = [`来源类型：${kindLabel}`];
    if (where) parts.push(`出处：${where}`);
    if (note) parts.push(note);
    return parts.join("\n");
  }

  // ------------------------------------------------------------------
  // 注意力机制（需求 3）
  // ------------------------------------------------------------------

  /**
   * 该条目生成时使用的注意力机制（**真实值**，来自任务元数据）。
   *
   * 优先服务端摘要 `model_info.attention_label`（与面板下拉逐字同源）；
   * 退化读 `params.attention` 的 key（老任务没有摘要时）。
   * 没有记录 -> 空字符串（前端据此不渲染这一行，**不编造**）。
   */
  function attentionText(job) {
    const info = modelInfo(job);
    let label = info.attention_label || "";
    if (!label) {
      const key = (job && job.params && job.params.attention) || "";
      if (key) label = String(key);
    }
    const speed = info.attention_speed_label || "";
    if (!label) return "";
    return speed ? `${label} · ${speed}` : label;
  }

  // ------------------------------------------------------------------
  // 导出
  // ------------------------------------------------------------------

  window.JOB_META = {
    MISSING,
    NOT_PROVIDED,
    PARAM_FIELDS,
    PARAM_LABEL,
    REC_LABEL,
    text,
    numText,
    escapeHtml,
    modelInfo,
    loraList,
    loraText,
    generationParams,
    samplingText,
    stepsText,
    sizeText,
    durationText,
    seedText,
    recItems,
    recSource,
    recFieldSource,
    attentionText,
  };
})();
