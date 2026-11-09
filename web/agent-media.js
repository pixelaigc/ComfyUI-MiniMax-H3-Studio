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

  // Agent 的素材槽位恒为 ref_image（agent.py:534 固定写死），
  // 所以 a2a（要 ref_audio）与 flf（要 first_frame）建不了。
  const UNSUPPORTED_BY_AGENT = {
    a2a: {
      reason: "Agent 的素材槽位固定为 ref_image，服务端只认 ref_audio。",
      advice: "请到音乐面板手动选参考音频提交，或先用 Agent 生成风格稿（t2a）。",
    },
    flf: {
      reason: "Agent 没有首帧/尾帧通道，素材只能作为 ref_image 传入。",
      advice: "请到视频面板上传首帧（或改用 r2v 参考图生成）。",
    },
  };

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
  // 4. Agent 的 create_job 只能传这些参数
  // ------------------------------------------------------------------
  //
  // 【事实】agent.py:501-556 只写入下列键：
  //   prompt / aspect / resolution / duration / batch_count /
  //   ref_images / asset_refs
  // 其它（model / steps / cfg / sampler / seed / lora / negative_prompt …）
  // Agent 传不了 → 任务卡**不要**展示成用户可设项。
  const AGENT_SETTABLE_PARAMS = [
    "prompt", "aspect", "resolution", "duration",
    "batch_count", "ref_images", "asset_refs",
  ];

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
  function resolveAspect(mode, requested) {
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
  function effectiveParams(mode, params) {
    params = params || {};
    const out = [];

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
  function limitationFor(mode, params) {
    if (UNSUPPORTED_BY_AGENT[mode]) return UNSUPPORTED_BY_AGENT[mode];
    if (MANUAL_ONLY_MODES[mode]) return MANUAL_ONLY_MODES[mode];
    // a2a / flf 已在上面；这里再检查「素材槽位不匹配」的组合：
    // Agent 传的素材全是 slot=ref_image，如果目标是音频/首尾帧就不成立。
    const refs = (params && (params.asset_refs || params.ref_images)) || [];
    if (isAudio(mode) && refs.length && !UNSUPPORTED_BY_AGENT[mode]) {
      return UNSUPPORTED_BY_AGENT.a2a;
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
    AGENT_SETTABLE_PARAMS,
    DISPLAYABLE_PARAMS,
    ASPECT_VALUES,
    isVideo, isAudio, isImage,
    modeLabel,
    resolveResolution, resolveDuration, resolveAspect,
    effectiveParams,
    limitationFor,
    resolveServerModes,
  };
})(typeof window !== "undefined" ? window : globalThis);
