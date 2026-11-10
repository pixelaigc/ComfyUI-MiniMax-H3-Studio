/* ============================================================
   文生视频（MiniMax-H3）参数面板
   ------------------------------------------------------------
   与「图像」面板的关系：结构完全一致 —— 清单由服务端下发
   （/api/video-options），前端只渲染 + 提交，不硬编码任何文件名。

   本模块负责：
     * 宽高直填 + 服务端权威吸附（显示「输入值 → 实际使用值」+ 原因）
     * 模型选择（本机实测可用清单）
     * 加速 LoRA 组合（可多选；固定加速 LoRA 不在这里）
     * 注意力机制（本机 ComfyUI 里真有的节点才可选）
     * 二采（SelfLift）开关与参数
     * 配置持久化（/api/video-config，局域网共用，与图像面板同一套机制）
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
    fixedAccelLora: "",
    accelLoras: [],         // [{key, strength, enabled}]
    attention: "",
    twopass: {
      enabled: false,
      transition_step: 3,
      lowres_scale: 0.5,
      rho: 0.0,
      w_min: 0.5,
      w_max: 1.0,
      upscaler_model: "",
    },
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
      const resp = await fetch(
        `/api/video-size?width=${encodeURIComponent(rawW)}&height=${encodeURIComponent(rawH)}`,
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
    if (!snapped || !snapped.width) return;
    videoState.width = snapped.width;
    videoState.height = snapped.height;
    videoState.sizeReasons = snapped.reasons || [];
    renderSizeFields();
  }

  // ------------------------------------------------------------ 模型

  function renderModelSelect() {
    const sel = document.getElementById("vidModelSelect");
    if (!sel) return;
    sel.innerHTML = "";
    Object.entries(VIDEO_OPTIONS.models).forEach(([key, m]) => {
      const opt = document.createElement("option");
      opt.value = key;
      const unavailable = m.available === false;
      opt.textContent = unavailable
        ? m.label + "（权重缺失，不可用）" : m.label;
      opt.disabled = unavailable;
      opt.selected = key === videoState.model;
      sel.appendChild(opt);
    });
    // 当前选中的模型不可用 -> 回落到默认
    const model = currentModel();
    if (!model || model.available === false) {
      const fallback = VIDEO_OPTIONS.models[VIDEO_OPTIONS.default_model];
      if (fallback) {
        videoState.model = VIDEO_OPTIONS.default_model;
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
      if (warn) warn.textContent = "";
      return;
    }
    if (note) {
      const bits = [model.note || ""];
      if (model.size_mb) bits.push(`本机体积 ${(model.size_mb / 1024).toFixed(1)} GB`);
      if (model.location) bits.push(`位置 ${model.location}`);
      note.textContent = bits.filter(Boolean).join(" · ");
    }
    if (warn) warn.textContent = model.warn || "";
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
      + `（${meta.purpose}），不随上面的勾选变化。`;
  }

  function renderAccelLoraList() {
    const box = document.getElementById("vidAccelLoraList");
    const note = document.getElementById("vidAccelLoraNote");
    if (!box) return;
    const combos = VIDEO_OPTIONS.accel_lora_combos;
    if (!combos.length) {
      box.innerHTML = "";
      if (note) note.textContent = "本机未检测到可用的加速 LoRA 组合。";
      return;
    }
    // 已选的放前面（用户勾过的），其余按清单顺序
    const chosen = new Set(videoState.accelLoras.filter(l => l.enabled !== false)
      .map(l => l.key));
    box.innerHTML = combos.map(item => {
      const on = chosen.has(item.key);
      const incompatible = item.compatible === false;
      const missing = item.available === false;
      const disabled = incompatible || missing;
      const meta = videoState.accelLoras.find(l => l.key === item.key);
      const strength = meta ? Number(meta.strength) : Number(item.recommended_strength || 1);
      const rec = Number(item.recommended_strength || 1);
      const reasons = [];
      if (missing) reasons.push("本机缺少该文件");
      if (incompatible) reasons.push(item.compat_reason || "与当前底模不兼容");
      return `<div class="img-lora-item ${disabled ? "disabled" : ""} ${incompatible ? "excluded" : ""}">
        <div class="img-lora-head">
          <button type="button" class="img-lora-btn toggle" data-key="${esc(item.key)}"
            title="${disabled ? esc(reasons.join("；")) : (on ? "取消勾选" : "勾选启用")}"
            ${disabled ? "disabled" : ""}>${on ? "●" : "○"}</button>
          <span class="img-lora-name" title="${esc(item.name)}">${esc(item.display)}</span>
          ${Math.abs(strength - rec) < 1e-6 ? '<span class="img-lora-tag rec">推荐强度</span>' : ""}
          ${incompatible ? '<span class="img-lora-tag excl">不兼容</span>' : ""}
          ${missing ? '<span class="img-lora-tag miss">文件缺失</span>' : ""}
        </div>
        <div class="img-lora-slider">
          <input type="range" class="strength" data-key="${esc(item.key)}"
            min="0" max="1.5" step="0.05" value="${Math.min(strength, 1.5)}"
            ${disabled ? "disabled" : ""} title="强度 0-1.5，0 表示关闭">
          <span class="img-lora-strength">${strength.toFixed(2)}</span>
        </div>
        <div class="img-lora-meta">${esc(item.purpose || "")}</div>
        <div class="img-lora-meta">${esc(item.note || "")}</div>
        ${reasons.length ? `<div class="img-lora-excl-reason">${esc(reasons.join("；"))}</div>` : ""}
      </div>`;
    }).join("");

    box.querySelectorAll("button.toggle").forEach(btn => {
      btn.addEventListener("click", () => {
        const key = btn.dataset.key;
        const existing = videoState.accelLoras.find(l => l.key === key);
        if (existing) {
          existing.enabled = existing.enabled === false;
        } else {
          const meta = accelComboMeta(key);
          videoState.accelLoras.push({
            key,
            strength: Number(meta && meta.recommended_strength || 1),
            enabled: true,
          });
        }
        renderAccelLoraList();
        saveVideoConfig();
      });
    });
    box.querySelectorAll("input.strength").forEach(slider => {
      slider.addEventListener("input", () => {
        const key = slider.dataset.key;
        let item = videoState.accelLoras.find(l => l.key === key);
        if (!item) {
          item = { key, strength: 1, enabled: true };
          videoState.accelLoras.push(item);
        }
        item.strength = Number(slider.value);
        item.enabled = item.strength > 0;
        const label = slider.parentElement.querySelector(".img-lora-strength");
        if (label) label.textContent = item.strength.toFixed(2);
      });
      slider.addEventListener("change", () => {
        renderAccelLoraList();
        saveVideoConfig();
      });
    });

    const selected = videoState.accelLoras.filter(l => l.enabled !== false
      && Number(l.strength) > 0);
    if (note) {
      const parts = [`已选 ${selected.length} 个加速 LoRA 组合`];
      if (selected.length) {
        parts.push("：" + selected.map(l => {
          const meta = accelComboMeta(l.key);
          return `${meta ? meta.display : l.key} ×${Number(l.strength).toFixed(2)}`;
        }).join("、"));
      }
      const blocked = combos.filter(c => c.compatible === false).length;
      if (blocked) parts.push(`（另有 ${blocked} 个与当前底模不兼容，已置灰）`);
      note.textContent = parts.join(" ");
    }
  }

  // ------------------------------------------------------------ 注意力

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
      opt.textContent = unavailable
        ? item.label + "（本机无此节点，不可用）" : item.label;
      opt.disabled = unavailable;
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
        sizeTyping = false;
        renderAspectChips();
        renderSizeFields();
        saveVideoConfig();
      });
      chips.appendChild(b);
    });
  }

  // ------------------------------------------------------------ 总渲染

  function renderVideoPanel() {
    if (!optionsReady) return;
    renderModelSelect();
    renderAspectChips();
    renderSizeFields();
    renderFixedLoraNote();
    renderAccelLoraList();
    renderAttention();
    renderTwopass();
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
      fixed_accel_lora: videoState.fixedAccelLora,
      accel_loras: videoState.accelLoras.map(l => ({
        key: l.key, strength: Number(l.strength), enabled: l.enabled !== false,
      })),
      attention: videoState.attention,
      twopass: { ...videoState.twopass },
    };
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
    if (cfg.fixed_accel_lora) videoState.fixedAccelLora = cfg.fixed_accel_lora;
    if (Array.isArray(cfg.accel_loras)) {
      videoState.accelLoras = cfg.accel_loras
        .filter(l => l && l.key)
        .map(l => ({
          key: l.key,
          strength: Number(l.strength || 1),
          enabled: l.enabled !== false,
        }));
    }
    if (cfg.attention) videoState.attention = cfg.attention;
    if (cfg.twopass && typeof cfg.twopass === "object") {
      videoState.twopass = { ...videoState.twopass, ...cfg.twopass };
    }
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

  async function loadVideoOptions() {
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
      // 恢复保存的配置
      try {
        const cfgResp = await fetch("/api/video-config", { cache: "no-store" });
        if (cfgResp.ok) applyConfig(await cfgResp.json());
      } catch (e) { /* 没有保存过配置，用默认值 */ }

      renderVideoPanel();
    } catch (e) {
      console.error("[视频面板] 载入清单失败:", e.message);
      const banner = document.getElementById("staleVideoBanner");
      if (banner) {
        banner.hidden = false;
        banner.innerHTML = "视频面板清单载入失败：<b>" + esc(e.message)
          + "</b>。请确认后端已重启（<code>systemctl restart h3-studio</code>）。";
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
      renderAccelLoraList();
    } catch (e) { /* 忽略：清单保持上一次的结果 */ }
  }

  // ------------------------------------------------------------ 提交参数

  function submitParams() {
    const [w, h] = currentSize();
    return {
      model_key: videoState.model,
      width: w,
      height: h,
      aspect: videoState.aspect,
      duration: state.duration,
      steps: state.steps,
      seed: $("#seed").value.trim() || null,
      fixed_accel_lora: videoState.fixedAccelLora,
      accel_loras: videoState.accelLoras
        .filter(l => l.enabled !== false && Number(l.strength) > 0)
        .map(l => ({ key: l.key, strength: Number(l.strength), enabled: true })),
      attention: videoState.attention,
      twopass: {
        enabled: !!videoState.twopass.enabled,
        transition_step: videoState.twopass.transition_step,
        lowres_scale: videoState.twopass.lowres_scale,
        rho: videoState.twopass.rho,
        w_min: videoState.twopass.w_min,
        w_max: videoState.twopass.w_max,
        upscaler_model: videoState.twopass.upscaler_model,
      },
    };
  }

  // ------------------------------------------------------------ 事件绑定

  function bind() {
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
        sizeTyping = false;
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
  };
})();
