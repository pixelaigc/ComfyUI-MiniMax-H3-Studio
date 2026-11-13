// Agent 媒体语义 —— 前端侧的模式/参数语义真源
//
// 【为什么单独一个文件】
//   任务卡要展示「**实际生效**的参数」，而不是 Agent 传进来的请求值。
//   后端对某些参数会**静默回落**（请求 1080p 实际落 768p；音乐传
//   duration 不生效、读的是 max_duration）。如果前端照抄请求值，
//   卡片上写「1080p / 120 秒」，用户看到成品却是 768p / 60 秒 ——
//   这就是误导。
//
//   所以这里把「请求值 -> 实际值」的映射写在前端，任务卡显示实际值，
//   并在发生回落时显式标注。**每一条都对应代码里的具体位置**
//   （见 docs/内置多模态Agent-前端增强.md 的对照表）。
//
// 【边界】
//   * 本文件**只做展示语义**，不下发任何参数。
//   * 模式白名单以服务端为准（见下方 AGENT_MODES）。
//   * 不展示 Agent 传不了的参数（模型版本/LoRA/采样参数/负面词…）——
//     那些是官方默认值，展示成「用户设的」是误导。
(function (global) {
  "use strict";

  // ------------------------------------------------------------------
  // 1. 模式白名单
  // ------------------------------------------------------------------
  //
  // 【事实】workflows.BUILDERS 共 9 个（h3studio/workflows.py:2234）：
  //   t2v / flf / r2v / t2i / t2i_qwen / i2i_qwen / t2i_moss / t2a / a2a
  //
  // 【坑】gen_types.GENERATION_TYPES 里**还登记了 longfilm**（长片拼接），
  //   但它走 POST /api/longfilms 自有链路，不在 server.JOB_MODES 里，
  //   Agent 的 create_job 建不了 —— 基线 Agent 曾因此误报 longfilm。
  //   所以前端**不**把它当 Agent 可下发的模式展示。
  //
  // 【真源策略】优先用服务端 GET /api/generation-types 的清单，
  //   再用这份常量兜底 / 剔除 longfilm。
  const AGENT_MODES = {
    t2i: { label: "Z-IMAGE 文生图", media: "image" },
    t2i_qwen: { label: "Qwen-Image 2.1 文生图", media: "image" },
    i2i_qwen: { label: "Qwen-Image 2.1 图片编辑", media: "image" },
    t2i_moss: { label: "MOSS-Image 文生图", media: "image" },
    // MiniMax-H3-Image：文生图 + 图片编辑（2026-11-13 新增）
    t2i_h3image: { label: "H3-Image 文生图", media: "image" },
    i2i_h3image: { label: "H3-Image 图片编辑", media: "image" },
    t2v: { label: "文生视频", media: "video" },
    flf: { label: "首尾帧生成", media: "video" },
    r2v: { label: "参考图生成", media: "video" },
    t2a: { label: "文生音乐", media: "audio" },
    a2a: { label: "参考生音乐", media: "audio" },
  };

  // Agent 建不了、需要人工在面板做的模式（用于给出替代建议）。
  const MANUAL_ONLY_MODES = {
    longfilm: {
      label: "长片拼接",
      reason: "长片拼接走独立的 /api/longfilms 链路，Agent 不能直接下发。",
      advice: "请到工作台「长片拼接」页手动提交（需 ≥2 个已完成的视频任务）。",
    },
  };

  // Agent 的素材槽位**按模式分派**（后端 agent_schema.MODE_SLOTS / mode_slot_plan）：
  //   a2a -> ref_audio（参考音频，1 条）
  //   flf -> first_frame（+ last_frame，可选）
  //   i2i_qwen / r2v -> ref_image（多条）
  //
  // 【为什么这里不再写死「Agent 建不了 a2a / flf」—— 2026-11-10 审查修复】
  // 旧版本这里有 UNSUPPORTED_BY_AGENT = {a2a, flf}，理由是
  // 「Agent 的素材槽位恒为 ref_image（agent.py:534 固定写死）」。
  // 那条**在旧基线上成立**，但后端增强分支已经把槽位改成分派制
  // （agent.py 的 _build_asset_refs + agent_schema.MODE_SLOTS）——
  // 合并后前端这条提示就变成了**反向阻断**：
  // 后端明明能建 a2a / flf，任务卡上却写「Agent 建不了，请到面板手动提交」。
  //
  // 【现在的做法 —— 能力位由后端下发】
  // 服务端在 GET /api/generation-types 的每个类型上带 agent_asset_slots，
  // 前端据此判断「这个模式 Agent 有没有素材通道」。
  // 服务端没给（老服务端）时**不显示**能力缺口提示 ——
  // 宁可少提示，也不要给一条与事实相反的阻塞提示。
  const UNSUPPORTED_BY_AGENT = {};

  // 后端下发的「Agent 素材槽位计划」缓存。
  // 结构：{mode: {single: [...], multi: [...], max_per_slot: {...}}}
  let SERVER_AGENT_SLOTS = {};

  // 用服务端下发的槽位计划刷新能力位。
  //
  // payload 来自 GET /api/generation-types：
  //   {types: [{key, label, media, agent_asset_slots: {...}}], ...}
  // 没有 agent_asset_slots 的类型 = 服务端没说 Agent 有素材通道
  // -> 记成空计划（不阻塞，因为 Agent 可能本来就不需要素材）。
  function applyServerAgentSlots(payload) {
    const list = (payload && payload.types) || [];
    const slots = {};
    (Array.isArray(list) ? list : []).forEach(t => {
      if (!t || !t.key) return;
      const plan = t.agent_asset_slots;
      if (plan && typeof plan === "object") {
        slots[t.key] = {
          single: Array.isArray(plan.single) ? plan.single : [],
          multi: Array.isArray(plan.multi) ? plan.multi : [],
          max_per_slot: plan.max_per_slot || {},
        };
      }
    });
    SERVER_AGENT_SLOTS = slots;
    return slots;
  }

  // 该模式在 Agent 侧的素材槽位计划（服务端下发优先）。
  function agentSlotsFor(mode) {
    if (SERVER_AGENT_SLOTS[mode]) return SERVER_AGENT_SLOTS[mode];
    return null;
  }

  // ------------------------------------------------------------------
  // 2. 分辨率：视频只有两档，其它值静默回落
  // ------------------------------------------------------------------
  //
  // 【事实】workflows.RES_MP = {"544p": 0.4, "768p": 0.8}（workflows.py:63）
  //   取值代码：`RES_MP.get(params.get("resolution", "768p"), 0.8)`
  //   → 任何非 544p/768p 的值**静默落成 768p**（workflows.py:320 / :409）。
  //
  // 【事实】图像分辨率按族回落默认档：
  //   Z-Image  Z_IMAGE_DEFAULT_TIER  = "1mp"
  //   Qwen     QWEN_IMAGE_DEFAULT_TIER = "1mp"
  //   MOSS     MOSS_IMAGE_DEFAULT_TIER = "1mp"（下限 0.25MP）
  const VIDEO_RESOLUTIONS = ["544p", "768p"];
  const VIDEO_DEFAULT_RESOLUTION = "768p";
  const IMAGE_DEFAULT_RESOLUTION = "1mp";

  // 各图像族的分辨率档位（workflows.py 的 *_RES_TIERS）
  const IMAGE_RESOLUTION_TIERS = {
    t2i: ["480p", "1mp", "720p", "1080p", "1440p", "2k", "4k"],
    t2i_qwen: ["480p", "1mp", "720p", "1080p", "1440p", "2k"],
    i2i_qwen: ["480p", "1mp", "720p", "1080p", "1440p", "2k"],
    t2i_moss: ["480p", "1mp", "720p", "1080p", "1440p", "2k"],
    // MiniMax-H3-Image：档位名**完全不同**（总像素 4/8/16MP），
    // 不是 480p/1mp 那一套 —— 写错会让 Agent 传一个不存在的档位。
    t2i_h3image: ["4mp", "8mp", "16mp"],
    i2i_h3image: ["4mp", "8mp", "16mp"],
  };

  // ------------------------------------------------------------------
  // 3. 音乐时长字段名不同：max_duration，不是 duration
  // ------------------------------------------------------------------
  //
  // 【事实】音乐工作流读 params["max_duration"]（music_yue2.py:328）
  //   `max_duration = _clamp_duration(params.get("max_duration", 60.0))`
  //   而 Agent 的 create_job 只暴露 `duration` —— 音乐任务下 duration 被忽略。
  //   钳制范围 [4, 360]（YUE2_MIN_DURATION / YUE2_MAX_DURATION）。
  const MUSIC_DEFAULT_DURATION = 60;
  const MUSIC_MIN_DURATION = 4;
  const MUSIC_MAX_DURATION = 360;

  // ------------------------------------------------------------------
  // 4. Agent 的 create_job 能传哪些参数
  // ------------------------------------------------------------------
  //
  // 【为什么这里不再维护一份白名单 —— 2026-11-10 审查修复】
  // 旧版本有一份 AGENT_SETTABLE_PARAMS = [prompt, aspect, resolution,
  // duration, batch_count, ref_images, asset_refs]，注释说
  // 「事实：agent.py:501-556 只写入下列键」。
  // 那条在**基线 7 参数时代**成立，后端扩到 23 参数后就过期了，
  // 而且这份清单**全仓没有任何消费点**（web/ 与 tests/ 引用数都是 0）——
  // 纯死代码 + 纯过期事实，留着只会误导下一个人。
  //
  // 现在**删除**它。真实能力由服务端下发：
  //   * 模式清单与素材通道 -> GET /api/generation-types
  //   * 参数取值集合       -> Agent 工具 get_mode_schema
  // 前端只做展示，不再猜「Agent 能设什么」。

  // 任务卡上**允许展示**的参数（白名单）。
  // 注意：刻意不含 model/steps/cfg/seed/lora/negative_prompt ——
  // 那些是官方默认值，展示出来会让人误以为是自己设的。
  const DISPLAYABLE_PARAMS = [
    "aspect", "resolution", "duration", "max_duration", "batch_count",
  ];

  const ASPECT_VALUES = ["1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9", "21:9"];

  // ------------------------------------------------------------------
  // 5. 对外接口
  // ------------------------------------------------------------------

  function isVideo(mode) {
    const info = AGENT_MODES[mode];
    return !!info && info.media === "video";
  }
  function isAudio(mode) {
    const info = AGENT_MODES[mode];
    return !!info && info.media === "audio";
  }
  function isImage(mode) {
    const info = AGENT_MODES[mode];
    return !!info && info.media === "image";
  }

  function modeLabel(mode) {
    if (!mode) return "";
    const info = AGENT_MODES[mode];
    if (info) return info.label;
    // 不在白名单里 —— 可能是 longfilm 这类「Agent 建不了」的，
    // 标出来比当成正常模式展示更诚实。
    const manual = MANUAL_ONLY_MODES[mode];
    if (manual) return manual.label + "（Agent 不能下发）";
    return mode;
  }

  // 解析「实际生效的分辨率」。
  // 返回 {value, requested, fallback, note}
  function resolveResolution(mode, requested) {
    const req = requested == null ? "" : String(requested);
    if (isVideo(mode)) {
      if (!req) {
        return { value: VIDEO_DEFAULT_RESOLUTION, requested: "", fallback: false,
                 note: "" };
      }
      if (VIDEO_RESOLUTIONS.indexOf(req) >= 0) {
        return { value: req, requested: req, fallback: false, note: "" };
      }
      return {
        value: VIDEO_DEFAULT_RESOLUTION,
        requested: req,
        fallback: true,
        note: `视频分辨率只有 ${VIDEO_RESOLUTIONS.join(" / ")}，请求 ${req} 实际按 ${VIDEO_DEFAULT_RESOLUTION} 生成`,
      };
    }
    if (isImage(mode)) {
      const tiers = IMAGE_RESOLUTION_TIERS[mode] || [];
      if (!req) {
        return { value: IMAGE_DEFAULT_RESOLUTION, requested: "", fallback: false, note: "" };
      }
      if (tiers.indexOf(req) >= 0) {
        return { value: req, requested: req, fallback: false, note: "" };
      }
      return {
        value: IMAGE_DEFAULT_RESOLUTION,
        requested: req,
        fallback: true,
        note: `${modeLabel(mode)} 不支持 ${req} 档位，实际按 ${IMAGE_DEFAULT_RESOLUTION} 生成`,
      };
    }
    // 音乐 / 未知：分辨率不适用
    return { value: req, requested: req, fallback: false, note: "" };
  }

  // 解析「实际生效的时长」。
  // 关键：音乐读 max_duration，Agent 传的 duration 对音乐无效。
  function resolveDuration(mode, params) {
    params = params || {};
    if (isAudio(mode)) {
      const reqMax = params.max_duration;
      const reqDuration = params.duration;
      let value;
      let requested;
      let fallback = false;
      let note = "";
      if (reqMax != null && reqMax !== "") {
        value = clampMusicDuration(Number(reqMax));
        requested = String(reqMax);
      } else {
        value = MUSIC_DEFAULT_DURATION;
        requested = "";
        if (reqDuration != null && reqDuration !== "") {
          // Agent 传了 duration —— 但音乐不读这个字段
          fallback = true;
          requested = String(reqDuration);
          note = `音乐时长字段是 max_duration，Agent 传的 duration=${requested} 不生效，实际按默认 ${MUSIC_DEFAULT_DURATION} 秒生成`;
        }
      }
      return { value, requested, fallback, note, unit: "秒" };
    }
    if (isVideo(mode)) {
      const req = params.duration;
      if (req == null || req === "") {
        return { value: 5, requested: "", fallback: false, note: "", unit: "秒" };
      }
      const value = Number(req);
      if (!isFinite(value)) {
        return { value: 5, requested: String(req), fallback: true,
                 note: `时长 ${req} 无效，实际按默认 5 秒生成`, unit: "秒" };
      }
      return { value, requested: String(req), fallback: false, note: "", unit: "秒" };
    }
    return { value: null, requested: "", fallback: false, note: "", unit: "" };
  }

  function clampMusicDuration(seconds) {
    if (!isFinite(seconds)) return MUSIC_DEFAULT_DURATION;
    return Math.max(MUSIC_MIN_DURATION, Math.min(MUSIC_MAX_DURATION, seconds));
  }

  // 解析比例（越界的比例在 Agent 工具层已被拦，服务端不校验）
  //
  // 注意区分两种「不是用户传的值」：
  //   * 没传 -> 用默认 16:9 —— 这是**默认**，不是「回落」，不该报警
  //   * 传了但非法 -> 回落 16:9 —— 这才是**回落**，必须让用户看见
  //
  // 【音频守卫 —— 2026-11-10 审查修复】
  // 音乐模式（t2a / a2a）是**纯音频**任务，没有画面比例这回事。
  // 旧实现无条件返回 "16:9"，于是参考生音乐的任务卡上渲染出
  // 「比例 16:9」这种**无意义**的参数 —— 而且那个值根本不是
  // Agent 设的（后端 schema 里音乐模式压根没有 aspect）。
  // 现在音频模式返回空值，调用方（effectiveParams）据此不渲染这一项。
  function resolveAspect(mode, requested) {
    if (isAudio(mode)) {
      // 音频模式没有画面比例 —— 即使参数里带了 aspect 也不认
      return { value: "", requested: "", fallback: false, note: "" };
    }
    const req = requested == null ? "" : String(requested);
    if (!req) return { value: "16:9", requested: "", fallback: false, note: "" };
    if (ASPECT_VALUES.indexOf(req) >= 0) {
      return { value: req, requested: req, fallback: false, note: "" };
    }
    return {
      value: "16:9",
      requested: req,
      fallback: true,
      note: `画面比例 ${req} 不在受支持范围内，实际按 16:9 生成`,
    };
  }

  // 生成任务卡要展示的「实际生效参数」列表。
  // 返回 [{key, label, value, fallback, note}]
  //
  // 【2026-11-11】分辨率 / 比例改用**服务端算好的 resolution_info**。
  // 原来这里读的是 params.resolution（档位字符串），而：
  //   * 文生视频新面板根本不写这个字段 -> 恒为遗留默认 "544p"；
  //   * 档位字符串还原不出真实像素（"1mp" 在 16:9 / 1:1 / 21:9 下各不相同）。
  // 服务端已按「产物实际像素优先」算好标签，这里直接用；
  // 服务端没给（旧版本）时退回原来的档位语义，保证不空。
  function effectiveParams(mode, params, job) {
    params = params || {};
    const out = [];

    const info = job && job.resolution_info;
    if (info && info.has_size) {
      // 有真实尺寸 -> 比例与分辨率都按实际像素来
      if (info.aspect) {
        out.push({ key: "aspect", label: "比例", value: info.aspect,
                   fallback: false, note: "" });
      }
      out.push({ key: "resolution", label: "分辨率", value: info.label,
                 fallback: false, note: info.note || "" });
    } else {
      const aspect = resolveAspect(mode, params.aspect);
      if (aspect.value) {
        out.push({ key: "aspect", label: "比例", value: aspect.value,
                   fallback: aspect.fallback, note: aspect.note });
      }
      const res = resolveResolution(mode, params.resolution);
      if (res.value) {
        out.push({ key: "resolution", label: "分辨率", value: res.value,
                   fallback: res.fallback, note: res.note });
      }
    }

    const dur = resolveDuration(mode, params);
    if (dur.value != null) {
      out.push({ key: "duration", label: "时长", value: `${dur.value} ${dur.unit}`,
                 fallback: dur.fallback, note: dur.note });
    }

    if (params.batch_count != null && Number(params.batch_count) > 1) {
      out.push({ key: "batch_count", label: "数量", value: `${params.batch_count} 张`,
                 fallback: false, note: "" });
    }

    return out;
  }

  // 该模式在 Agent 侧的能力缺口（用于提示替代方案，而不是只报错）
  //
  // 【语义，2026-11-10 审查修复后】
  // 只在**服务端明确说 Agent 缺这个槽位**时才提示。
  // 服务端下发 agent_asset_slots 时：
  //   * 有槽位（single/multi 非空）-> Agent 有素材通道 -> 不提示
  //   * 槽位计划为空且该模式**本来就需要素材** -> 提示
  // 服务端没下发（老服务端）-> **不提示**（宁可少提示，也不要给反向阻断提示）。
  function limitationFor(mode, params) {
    if (UNSUPPORTED_BY_AGENT[mode]) return UNSUPPORTED_BY_AGENT[mode];
    if (MANUAL_ONLY_MODES[mode]) return MANUAL_ONLY_MODES[mode];
    const plan = agentSlotsFor(mode);
    if (!plan) return null;               // 服务端没说 -> 不臆断
    const hasChannel = (plan.single && plan.single.length)
      || (plan.multi && plan.multi.length);
    if (hasChannel) return null;          // Agent 有素材通道 -> 没问题
    // 服务端说「这个模式没有素材通道」，但任务里却带了素材引用
    // -> 说明引用放不进去，给出替代建议。
    const refs = (params && (params.asset_refs || params.ref_images)) || [];
    if (refs.length) {
      return {
        reason: `服务端没有给「${modeLabel(mode)}」下发 Agent 素材通道，`
          + "这次带的素材引用放不进去。",
        advice: "请到对应面板手动提交这个任务，或去掉素材引用让 Agent 重新生成。",
      };
    }
    return null;
  }

  // 从服务端 /api/generation-types 拉清单，剔除 Agent 建不了的模式。
  // 拉不到就用内置常量（保证前端不因后端暂时不可用而空掉）。
  function resolveServerModes(payload) {
    const list = (payload && payload.types) || (payload && payload.generation_types) || [];
    const out = {};
    (Array.isArray(list) ? list : []).forEach(t => {
      if (!t || !t.key) return;
      if (MANUAL_ONLY_MODES[t.key]) return;   // Agent 建不了 -> 不展示
      out[t.key] = { label: t.label || t.key, media: t.media || "other" };
    });
    return Object.keys(out).length ? out : AGENT_MODES;
  }

  global.AgentMedia = {
    AGENT_MODES,
    MANUAL_ONLY_MODES,
    UNSUPPORTED_BY_AGENT,
    VIDEO_RESOLUTIONS,
    IMAGE_RESOLUTION_TIERS,
    MUSIC_DEFAULT_DURATION,
    MUSIC_MIN_DURATION,
    MUSIC_MAX_DURATION,
    DISPLAYABLE_PARAMS,
    ASPECT_VALUES,
    isVideo, isAudio, isImage,
    modeLabel,
    resolveResolution, resolveDuration, resolveAspect,
    effectiveParams,
    limitationFor,
    resolveServerModes,
    // 能力位由后端下发（2026-11-10 审查修复）
    applyServerAgentSlots,
    agentSlotsFor,
  };
})(typeof window !== "undefined" ? window : globalThis);
