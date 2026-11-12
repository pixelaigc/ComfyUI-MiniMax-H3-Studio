// 接续生成（continuation）的前端模块。
//
// 【职责边界】
//   * 生成库卡片上的「接续」按钮（显示/隐藏由服务端下发的
//     continuation_info 决定 —— 前端不自己判断能不能接续）。
//   * 左侧配置区的「接续上下文」只读区域：展示接续来源、回填了什么、
//     并提供「清除」按钮。
//   * 点「接续」后调 GET /api/jobs/{id}/continuation 拿回上下文，
//     再回填到左侧配置区。
//
// 【为什么单独一个模块，而不是塞进 app.js】
// app.js 已经 3800+ 行。接续是一条完整的独立链路（按钮 -> 取上下文 ->
// 回填 -> 展示 -> 清除），把它抽出来才能：
//   * 与「复用参数」逻辑**互不干扰**（两者都要写左侧配置区）；
//   * 单独测（window.Continuation 可被验收脚本直接调）。
//
// 【与「复用参数」的区别 —— 必须讲清楚】
//   * 复用参数：把**提示词**也带过去，用户改一改重新生成 —— 独立的一段。
//   * 接续：把**提示词除外**的一切带过去，用户只写下一段提示词 ——
//     上一段被潜空间桥真正接上（零重编码），拼出完整视频。
// 所以接续**不写提示词**（用户自己写），复用参数**写提示词**。

(() => {
  // ------------------------------------------------------------------
  // 状态
  // ------------------------------------------------------------------
  // 当前接续上下文（点过「接续」之后）。null = 没在接续模式。
  let current = null;
  // 本机是否具备接续能力（Relay-Kit 节点在不在）。
  let capability = { available: false, reason: "" };

  const state = () => ({ current, capability });

  // ------------------------------------------------------------------
  // 工具
  // ------------------------------------------------------------------
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function toast(message) {
    // 与项目里其它提示一致：优先用既有的全局提示函数，没有就退回 alert。
    if (typeof window.showToast === "function") {
      window.showToast(message);
    } else if (typeof window.toast === "function") {
      window.toast(message);
    } else {
      alert(message);
    }
  }

  // ------------------------------------------------------------------
  // 能力探测
  // ------------------------------------------------------------------
  async function loadCapability() {
    try {
      const resp = await fetch("/api/continuation/state", { cache: "no-store" });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const data = await resp.json();
      capability = {
        available: !!data.available,
        reason: data.reason || "",
        nodes: data.nodes || {},
      };
    } catch (e) {
      capability = { available: false, reason: "接续状态探测失败：" + e.message };
    }
    return capability;
  }

  // ------------------------------------------------------------------
  // 卡片按钮：能不能显示「接续」
  // ------------------------------------------------------------------
  // 【为什么完全听服务端的】
  // 「能不能接续」要同时看：模式、是不是二采、潜空间文件在不在、
  // 有没有链信息。这四样只有服务端知道（潜空间在 ComfyUI 的磁盘上，
  // 前端看不到）。前端自己判断必然和服务端不一致 —— 于是出现
  // 「按钮显示了但点了报错」或者「按钮不显示但其实能接」。
  //
  // 【为什么不额外要求 capability.available】
  // 服务端的逐任务判定（continuation_info.can_continue）**已经是权威**：
  // 它检查了模式、二采、潜空间文件在不在。而潜空间文件**只有 Relay-Kit
  // 能落盘** —— 文件在，就说明节点装过。
  //
  // 如果这里再叠一层 capability.available，就多了一个**异步竞态**：
  // 能力探测是页面加载时异步做的，首屏渲染可能发生在它返回之前 ——
  // 于是按钮在首屏不显示，等探测回来才出现（实测 1280px 那档就踩到了：
  // 卡片已渲染但按钮还没出现，因为刷新比探测慢）。
  // 所以：**以服务端逐任务判定为准，能力探测只用于提示。**
  function canShowButton(job) {
    if (!job || job.status !== "done") return false;
    const info = job.continuation_info;
    if (!info) return false;
    return !!info.can_continue;
  }

  function buttonTitle(job) {
    const info = (job && job.continuation_info) || {};
    if (info.can_continue) {
      const total = info.total_seconds
        ? `（当前累计 ${info.total_seconds}s）` : "";
      return "把这一段的全部配置回填到左侧，接着写下一段提示词继续生成" + total;
    }
    return info.reason || "这一段不能接续";
  }

  // ------------------------------------------------------------------
  // 取上下文 + 回填
  // ------------------------------------------------------------------
  async function fetchContext(jobId) {
    const resp = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/continuation`,
      { cache: "no-store" });
    let data = {};
    try { data = await resp.json(); } catch (e) { data = {}; }
    if (!resp.ok || !data.ok) {
      throw new Error(data.error || data.reason || "取接续上下文失败");
    }
    return data;
  }

  // 把上下文回填到左侧配置区。
  //
  // 【与「复用参数」的冲突点及处理】
  // 两者都写：VIDEO_PANEL 的状态、时长、步数、种子。
  // 本函数**只**在点「接续」时调用，与「复用参数」是互斥的两条路径
  // （用户在卡片上点哪个就执行哪个），所以不会互相覆盖。
  // 唯一共用的是 VIDEO_PANEL.applyConfig —— 那是**唯一**的写入口，
  // 复用参数也走它，行为一致。
  function applyToPanel(ctx) {
    const params = ctx.params || {};
    const panel = window.VIDEO_PANEL;

    // 切到文生视频面板（接续只对文生视频开放）
    if (typeof window.activateVideoMode === "function") {
      window.activateVideoMode("t2v");
    } else if (panel && typeof panel.render === "function") {
      panel.render();
    }

    if (panel && typeof panel.applyConfig === "function") {
      const cfg = {
        model: params.model_key,
        aspect: params.aspect,
        width: params.width,
        height: params.height,
        rawWidth: params.width,
        rawHeight: params.height,
        custom: true,
        fixed_accel_lora: params.fixed_accel_lora,
        accel_loras: Array.isArray(params.accel_loras) ? params.accel_loras : [],
        attention: params.attention,
        twopass: params.twopass,
        steps: params.steps,
        sampler: params.sampler,
        scheduler: params.scheduler,
        shift_video: params.shift_video,
        shift_audio: params.shift_audio,
      };
      panel.applyConfig(cfg);
      if (typeof panel.render === "function") panel.render();
    }

    // 时长 / 步数 / 种子在 app.js 的全局 state 里（不在 VIDEO_PANEL 里）
    const appState = window.__h3State;
    if (appState) {
      if (params.duration) {
        appState.duration = params.duration;
        const slider = document.getElementById("duration");
        if (slider) slider.value = params.duration;
        const label = document.getElementById("durVal");
        if (label) label.textContent = params.duration + "s";
      }
      if (params.steps) {
        appState.steps = params.steps;
        const steps = document.getElementById("steps");
        if (steps) steps.value = params.steps;
        const stepsVal = document.getElementById("stepsVal");
        if (stepsVal) stepsVal.textContent = params.steps;
      }
    }
    // 种子：接续不该沿用上一段的种子（同一 seed + 同一 latent 会复现同一段）。
    // 所以这里**清空**种子，让用户自己填或随机。
    const seedInput = document.getElementById("seed");
    if (seedInput) seedInput.value = "";

    // 提示词：接续**不**回填上一段的提示词 —— 用户要写下一段。
    // 但要把提示词框清空并聚焦，明确告诉用户「这里写下一段」。
    const promptBox = document.getElementById("prompt");
    if (promptBox) {
      promptBox.value = "";
      promptBox.focus();
      promptBox.scrollIntoView({ block: "center", behavior: "smooth" });
    }
    // 接续上下文区也要滚进可视区 —— 它在提示词正上方，
    // 用户点完「接续」必须一眼看到「回填了什么」。
    const contextBox = document.getElementById("continuationPanel");
    if (contextBox && typeof contextBox.scrollIntoView === "function") {
      contextBox.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  // ------------------------------------------------------------------
  // 「接续上下文」只读区域
  // ------------------------------------------------------------------
  function renderPanel() {
    const box = document.getElementById("continuationPanel");
    if (!box) return;
    if (!current) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    box.hidden = false;
    const source = current.source || {};
    const summary = current.summary || [];
    const rows = summary.map(row =>
      `<div class="row"><span>${esc(row.label)}</span><b>${esc(row.value)}</b></div>`
    ).join("");
    const total = current.total_seconds
      ? `<div class="row"><span>当前累计时长</span><b>${esc(current.total_seconds)}s</b></div>`
      : "";
    box.innerHTML = `
      <div class="continuation-head">
        <span class="continuation-title">接续上下文</span>
        <button type="button" class="continuation-clear" id="continuationClear"
                title="清除接续上下文（不影响已生成的视频）">清除</button>
      </div>
      <div class="continuation-source">
        接续自生成库里的「${esc(source.job_id || "")}」${source.video_name
          ? "（" + esc(source.video_name) + "）" : ""}
      </div>
      ${total}
      ${rows}
      <div class="continuation-note">
        提示词未回填 —— 请在下方输入**下一段**的提示词后点生成。
        生成结果会自动拼成完整视频。
      </div>`;
    const clearBtn = document.getElementById("continuationClear");
    if (clearBtn) {
      clearBtn.addEventListener("click", () => {
        clear();
        toast("已清除接续上下文");
      });
    }
  }

  function clear() {
    current = null;
    renderPanel();
  }

  // ------------------------------------------------------------------
  // 主入口：点「接续」
  // ------------------------------------------------------------------
  async function start(job) {
    if (!job) return;
    try {
      const ctx = await fetchContext(job.id);
      applyToPanel(ctx);
      current = ctx;
      renderPanel();
      const total = ctx.total_seconds ? `，当前累计 ${ctx.total_seconds}s` : "";
      toast(`已回填接续上下文（${(ctx.summary || []).length} 项${total}）。写下一段提示词即可继续生成。`);
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (e) {
      // 明确的中文提示 —— 不静默失败。
      toast("接续失败：" + (e.message || e));
    }
  }

  // ------------------------------------------------------------------
  // 对外接口
  // ------------------------------------------------------------------
  window.Continuation = {
    loadCapability,
    canShowButton,
    buttonTitle,
    start,
    clear,
    renderPanel,
    state,
    // 供验收脚本直接读（证明「点接续 -> 上下文真的回填了」）
    _debug: {
      get current() { return current; },
      get capability() { return capability; },
    },
  };
})();
