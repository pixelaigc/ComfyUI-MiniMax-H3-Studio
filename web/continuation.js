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
  // 素材回填（首尾帧 / 参考生视频）
  // ------------------------------------------------------------------
  // 【为什么需要】
  // 接续的语义是「接着上一段继续」—— 上一段的参考图 / 参考音乐应该
  // **自动带过来**，用户只写下一段提示词。
  // 素材以**素材库引用**（asset_refs）的形态记录在任务参数里，
  // 回填时按「槽位 -> 面板槽位 key」的映射还原成 state.assetRefs。
  //
  // 【映射口径 —— 必须与服务端 / 提交侧一致】
  // 服务端 slot 名 -> 面板槽位 key：
  //   first_frame -> first        last_frame  -> last
  //   ref_image   -> ref0..ref8  ref_video   -> refvid0..2
  //   ref_audio   -> refaud0..2
  // 提交侧（app.js 的 SLOT_FOR_KEY / GROUP_OF_KEY）用的是**同一张表**。
  //
  // 【为什么按 ordinal 分组】
  // ref_image 有 9 个槽位、ref_video 3 个、ref_audio 3 个 ——
  // ordinal 是**组内序号**（服务端 _apply_asset_refs 就是这么排的）。
  // 还原时按 slot 分组、组内按 ordinal 排序，才不会把「参考图 3」
  // 填到「参考图 1」的槽位上。
  const SLOT_KEY_FOR_SERVER_SLOT = {
    first_frame: "first",
    last_frame: "last",
  };

  const SLOT_KEY_GROUP = {
    ref_image: (ordinal) => "ref" + ordinal,
    ref_video: (ordinal) => "refvid" + ordinal,
    ref_audio: (ordinal) => "refaud" + ordinal,
  };

  /** 素材库引用 -> 面板槽位 key -> {asset_id, name, category}。 */
  function mapAssetRefsToSlotKeys(refs, mode) {
    const out = {};
    if (!Array.isArray(refs) || !refs.length) return out;
    // 先按 slot 分组，组内按 ordinal 排序。
    const grouped = {};
    refs.forEach((item) => {
      if (!item || !item.asset_id) return;
      const slot = String(item.slot || "");
      const ordinal = Number(item.ordinal || 0);
      if (!grouped[slot]) grouped[slot] = [];
      grouped[slot].push({ asset_id: item.asset_id, ordinal });
    });
    Object.keys(grouped).forEach((slot) => {
      const list = grouped[slot].sort((a, b) => a.ordinal - b.ordinal);
      if (SLOT_KEY_FOR_SERVER_SLOT[slot]) {
        // 首帧 / 尾帧：只取第一个
        const key = SLOT_KEY_FOR_SERVER_SLOT[slot];
        out[key] = { asset_id: list[0].asset_id };
      } else if (SLOT_KEY_GROUP[slot]) {
        list.forEach((entry) => {
          const key = SLOT_KEY_GROUP[slot](entry.ordinal);
          out[key] = { asset_id: entry.asset_id };
        });
      }
    });
    return out;
  }

  // ------------------------------------------------------------------
  // 工具
  // ------------------------------------------------------------------
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // 轻量 toast（复用素材库的 .al-toast 样式）。
  //
  // 【为什么不依赖全局 toast】项目里没有全局 window.toast /
  // window.showToast —— 原实现会一路退到 alert()，而 alert 是**阻塞式**
  // 的：它会卡住整个页面直到用户点掉。对自动化测试尤其致命（页面卡死，
  // 后续断言全部超时）。
  //
  // 所以这里自带一个实现，**永不阻塞**。
  function toast(message) {
    const text = String(message == null ? "" : message);
    let box = document.getElementById("h3ContToast");
    if (!box) {
      box = document.createElement("div");
      box.id = "h3ContToast";
      box.className = "al-toast";
      document.body.appendChild(box);
    }
    box.textContent = text;
    box.className = "al-toast show";
    clearTimeout(box._timer);
    box._timer = setTimeout(() => { box.className = "al-toast"; }, 4200);
    // 顺带把文本挂到 body 上，方便验收脚本直接读（不必截屏认字）。
    window.__h3ContinuationToast = text;
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
  // 「能不能接续」要同时看：模式、潜空间文件在不在、
  // 有没有链信息。这三样只有服务端知道（潜空间在 ComfyUI 的磁盘上，
  // 前端看不到）。前端自己判断必然和服务端不一致 —— 于是出现
  // 「按钮显示了但点了报错」或者「按钮不显示但其实能接」。
  //
  // 【为什么不额外要求 capability.available】
  // 服务端的逐任务判定（continuation_info.can_continue）**已经是权威**：
  // 它检查了模式、潜空间文件在不在。而潜空间文件**只有 Relay-Kit
  // 能落盘** —— 文件在，就说明节点装过。
  // （二采（selflift）也走同一套判定：二采**支持**接续，
  //   唯一额外要求是「二采段自己有落盘潜空间」—— 那也是服务端查盘。）
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
    // 【为什么要带 duration】用户点「接续」时面板上填的时长就是
    // **下一段**的请求时长。把它传给服务端，服务端才能算出
    // 「接完这一段之后成片会有多长」（按 1 秒重叠口径 + 实际帧数）。
    // 不带的话只能算到当前这一段，显示的成片时长会少一段。
    let nextSeconds = 0;
    try {
      const appState = window.__h3State;
      nextSeconds = Number((appState && appState.duration) || 0);
    } catch (e) {
      nextSeconds = 0;
    }
    const query = nextSeconds > 0 ? `?duration=${nextSeconds}` : "";
    const resp = await fetch(
      `/api/jobs/${encodeURIComponent(jobId)}/continuation${query}`,
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
    // 【2026-11-16】接续支持的模式：t2v / flf / r2v。
    // 切到**来源任务自己的**模式 —— 硬切 t2v 会让首尾帧 / 参考生
    // 的产物落到文生视频面板（素材槽位根本不存在）。
    const mode = String(ctx.mode || "t2v");

    if (typeof window.activateVideoMode === "function") {
      window.activateVideoMode(mode);
    } else if (panel && typeof panel.render === "function") {
      panel.render(mode);
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
      if (typeof panel.render === "function") panel.render(mode);
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

    // ---- 素材回填（首尾帧 / 参考生视频）----
    //
    // 【为什么在这里做】切模式会清空槽位状态（app.js 的
    // resetSlotsForMode），所以必须在**切完之后**再回填。
    //
    // 【回填什么】原始参考图 / 首帧 / 尾帧 / 参考视频 / 参考音乐。
    // 参考音频回填的是**原始整条** —— 「1s 空白 ⊕ 切片」是服务端
    // 在生成时按接续时序算出来的，不需要（也不该）在这里造文件。
    //
    // 【为什么不在这里 await】applyToPanel 是同步的；素材回填需要
    // 网络请求。start() 里 await 一次即可（见那里的调用）。
    //
    // ---- 音轨来源回填（首尾帧 / 参考生视频）----
    //
    // 【为什么必须回填】音轨来源决定「这条链用生成的音乐还是外部
    // 音轨」，而它是**以链首段为准**的。切模式时音轨下拉会被复位
    // 成「使用生成的音乐」—— 那会让接续段的音轨与上一段对不上。

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

  /**
   * 把上一段的素材（参考图 / 首帧 / 尾帧 / 参考视频 / 参考音乐）
   * 回填到面板槽位。
   *
   * 【为什么走素材库引用】槽位里只可能是素材库引用（本地直传通道
   * 已关闭，见 app.js 的 mk() 注释）。所以回填的也是 asset_id ——
   * 前端不需要、也不应该碰 ComfyUI 里的文件。
   *
   * 【为什么异步】要按 asset_id 查素材名与类别（填进槽位的标签），
   * 那是网络请求。
   */
  async function applyAssetRefs(ctx, mode) {
    const refs = ctx.asset_refs;
    if (!Array.isArray(refs) || !refs.length) return 0;
    const appState = window.__h3State;
    if (!appState || !appState.assetRefs) return 0;

    const mapped = mapAssetRefsToSlotKeys(refs, mode);
    const ids = Object.keys(mapped).map((k) => mapped[k].asset_id);
    if (!ids.length) return 0;

    // 查素材元数据（名字 / 类别）—— 查不到就跳过那一条（不编造）。
    const details = {};
    await Promise.all(ids.map(async (id) => {
      try {
        const resp = await fetch(`/api/assets/${encodeURIComponent(id)}`,
          { cache: "no-store" });
        if (!resp.ok) return;
        const asset = await resp.json();
        details[id] = {
          asset_id: asset.id,
          name: asset.name,
          category: asset.category,
        };
      } catch (e) { /* 查不到就跳过 —— 不编造素材信息 */ }
    }));

    let filled = 0;
    Object.keys(mapped).forEach((key) => {
      const info = details[mapped[key].asset_id];
      if (!info) return;
      appState.assetRefs[key] = info;
      filled += 1;
    });

    // 音轨来源下拉要重算（有没有参考音乐 / 参考视频决定了选项）
    if (typeof window.renderAudioTrackSelect === "function") {
      window.renderAudioTrackSelect();
    }
    // 重渲染面板，让槽位显示出来。
    //
    // 【关键】必须用「保留槽位状态」的那一版 —— renderMode() 内部会
    // 调 resetSlotsForMode 把 state.assetRefs 整块清空，
    // 那样刚回填进去的素材就被自己清掉了（实测：槽位为空）。
    if (typeof window.renderModeKeepSlots === "function") {
      window.renderModeKeepSlots();
    } else if (typeof window.renderMode === "function") {
      window.renderMode();
    }
    return filled;
  }

  /**
   * 回填音轨来源（首尾帧 / 参考生视频）。
   *
   * 【口径 —— 以链首段为准】服务端下发 ctx.audio.track_mode，
   * 它就是整条链的音轨来源。切模式时前端把它复位了，这里复原。
   */
  function applyAudioTrack(ctx, mode) {
    const audio = ctx.audio || {};
    const trackMode = String(audio.track_mode || "");
    if (!trackMode) return;
    const appState = window.__h3State;
    if (appState) appState.audioTrackSource = trackMode;
    const select = document.getElementById("audioTrackSelect");
    if (select) {
      // 先确保选项里有它（素材回填后才可能出现），再选中。
      if (typeof window.renderAudioTrackSelect === "function") {
        window.renderAudioTrackSelect();
      }
      select.value = trackMode;
    }
    if (typeof window.renderAudioTrackSelect === "function") {
      window.renderAudioTrackSelect();
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
      ${renderDurationRows(current.duration_plan)}
      ${renderAudioRows(current.audio)}
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

  /**
   * 音频接续时序的**只读展示**（首尾帧 / 参考生视频）。
   *
   * 【为什么必须显示】音画对齐是这套功能里最容易被误解的部分 ——
   * 用户必须能看见「这一段的参考音频从第几秒接入、前置多少空白」，
   * 否则「为什么这一段的音画差了一点」无从判断。
   */
  function renderAudioRows(audio) {
    if (!audio || !audio.track_mode) return "";
    const rows = [];
    const label = audio.track_mode === "reference" ? "参考音乐"
      : audio.track_mode === "video_bg" ? "参考视频的音轨" : "生成的音乐";
    rows.push(`<div class="row"><span>音轨来源</span><b>${esc(label)}</b></div>`);
    if (audio.track_mode === "reference" || audio.track_mode === "video_bg") {
      rows.push(`<div class="row"><span>本段音频起点</span>`
        + `<b>${esc(Number(audio.segment_start || 0).toFixed(3))}s</b></div>`);
      rows.push(`<div class="row"><span>本段音频时长</span>`
        + `<b>${esc(Number(audio.segment_duration || 0).toFixed(3))}s</b></div>`);
      rows.push(`<div class="row"><span>前导空白</span>`
        + `<b>${esc(Number(audio.lead_blank || 0).toFixed(1))}s</b></div>`);
    }
    const notes = (audio.notes || []).map(n =>
      `<div class="continuation-note">${esc(n)}</div>`).join("");
    const warns = (audio.warnings || []).map(n =>
      `<div class="continuation-note">⚠ ${esc(n)}</div>`).join("");
    return rows.join("") + notes + warns;
  }

  /**
   * 成片时长账的**只读展示**（2026-11-17 更正后新增；
   * 2026-11-17 收口后只显示**一个**数）。
   *
   * 【为什么只显示一个数】
   * 用户口径是：成片总时长 = Σ(各段请求时长) - (段数-1) × 1 秒。
   * 上一轮面板并列显示「按用户口径算的」和「实测会得到的」两个数，
   * 是因为那时成片**真的**比口径长（差 +0.4167s / +0.5000s）。
   * 现在成片拼接会做尾裁（server 侧 `_concat_chain_video` 的
   * `trim_tail_frames`），成片帧数**精确**命中用户口径
   * （5s→5s = 216 帧 / 9.000s；5s→5s→8s = 384 帧 / 16.000s），
   * 两个数**完全相等** —— 并列显示反而会让人以为还有差异。
   *
   * 所以现在：
   *   * 只显示「成片时长」一个数（= 成片真实帧数 / 24）；
   *   * 若 `compensation.exact` 为 false（残差非 0），
   *     才额外显示「残差多少帧 / 多少秒」+ **来自哪一条上游硬约束**，
   *     绝不静默给近似值。
   *
   * @param {object} plan 服务端下发的 duration_plan
   */
  function renderDurationRows(plan) {
    if (!plan || !plan.segments || !plan.segments.length) return "";
    const rows = [];
    const comp = plan.compensation || {};
    const segs = plan.segments.map(s => {
      const ov = s.overlap_frames
        ? `（重叠 ${s.overlap_frames} 帧 = ${Number(s.overlap_frames / 24).toFixed(4)}s）`
        : "";
      return `第${s.index + 1}段 请求 ${s.requested_seconds}s → 交付 `
        + `${s.delivered_frames} 帧 = ${Number(s.delivered_seconds).toFixed(4)}s${ov}`;
    });
    // 【唯一口径】成片时长 = 成片真实帧数 / 24
    rows.push(`<div class="row"><span>成片时长</span>`
      + `<b>${esc(Number(plan.total_seconds).toFixed(3))}s`
      + `（${esc(plan.total_frames)} 帧）</b></div>`);
    // 残差非 0 时才说残差 —— 并写清来自哪一条上游硬约束
    if (!comp.exact) {
      const residual = Number(comp.residual_frames || 0);
      rows.push(`<div class="row"><span>⚠ 与「请求时长之和 − 每段 1 秒重叠」相差</span>`
        + `<b>${esc(Math.abs(residual))} 帧`
        + `（${esc((Math.abs(residual) / 24).toFixed(4))}s）</b></div>`);
    }
    const segList = `<div class="continuation-note">分段：`
      + esc(segs.join("；")) + `</div>`;
    const notes = (plan.notes || []).map(n =>
      `<div class="continuation-note">${esc(n)}</div>`).join("");
    return rows.join("") + segList + notes;
  }

  function clear() {
    current = null;
    renderPanel();
  }

  // ------------------------------------------------------------------
  // 「复用参数」与「接续」的互斥（2026-11-12 修）
  // ------------------------------------------------------------------
  //
  // 【原来为什么会形成混合态】
  // 点「接续」后 context 挂在面板上；用户又点**另一张**卡片的「复用参数」，
  // 复用参数只改左侧控件（尺寸/模型/步数…），**不清 context**。
  // 于是提交时：params 来自 B 卡片、continuation 来自 A 卡片。
  //
  // 后果（实测）：本段按 B 的分辨率跑，接续却要接 A 的潜空间 ——
  // Relay-Kit 的拷贝桥直接 raise「拷贝桥禁止跨分辨率：上段 768x448 ≠
  // 本段 1024x576」。用户看到的是一次「生成失败」，完全不知道是
  // 「接续」和「复用参数」打架。
  //
  // 【为什么选「复用参数时清掉接续上下文」】
  // 两个动作的语义本来就互斥：
  //   * 复用参数 = 拿一张卡片的**全部参数**（含提示词）独立重生成一段；
  //   * 接续     = 拿一段的**潜空间**当接力棒，续下一段。
  // 用户点「复用参数」= 表达「我要照这张重新生成」—— 此时留着别的
  // 段落的接力棒只会制造混合态。**清掉它**是最符合直觉的语义。
  //
  // 反过来（点「接续」覆盖「复用参数」）不成立：复用参数已经把控件
  // 填好了，接续又会用上一段的配置盖回去 —— 用户点「接续」时看到的
  // 是「上一段的配置」，那正是接续该有的样子。
  //
  // 【服务端还有一道闸】即使前端状态被绕过（例如直接调 API），
  // 服务端也会校验本段与上游的分辨率是否一致，不一致就明确报错。
  function clearForReuse(reason) {
    if (!current) return false;
    const sourceId = (current.source || {}).job_id || "";
    current = null;
    renderPanel();
    toast(reason
      || `已清除接续上下文（来源「${sourceId}」）—— 「复用参数」是独立重生成，不与接续混用。`);
    return true;
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
      // 【2026-11-16】素材回填是**异步**的（要按 asset_id 查素材名）——
      // 等它完成再提示，否则用户看到「已回填」但槽位还是空的。
      const materialCount = await applyAssetRefs(ctx, String(ctx.mode || "t2v"));
      applyAudioTrack(ctx, String(ctx.mode || "t2v"));
      const total = ctx.total_seconds ? `，当前累计 ${ctx.total_seconds}s` : "";
      const mats = materialCount
        ? `，已回填 ${materialCount} 项素材` : "";
      toast(`已回填接续上下文（${(ctx.summary || []).length} 项${total}${mats}）。`
        + `写下一段提示词即可继续生成。`);
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (e) {
      // 明确的中文提示 —— 不静默失败。
      toast("接续失败：" + (e.message || e));
    }
  }

  // ------------------------------------------------------------------
  // 提交前的**最后一道闸**：接续上下文与当前面板尺寸是否一致
  // ------------------------------------------------------------------
  //
  // 【为什么要有这道闸】前端「复用参数清 context」已经解决了大部分
  // 混合态，但用户**自己改尺寸**（手填宽高）同样能造成不一致 ——
  // 上一段 768×448、用户改成 1024×576 再生成，一样会撞拷贝桥。
  //
  // 这里在提交前比对一次，不一致就**当场提示并中止提交**：
  // 比起让用户等一次生成（几十秒）才看到失败，体验好得多。
  //
  // 【为什么不自己改尺寸】用户手填的尺寸是他的明确意图；
  // 静默改掉等于替用户做决定。只提示、不改。
  //
  // 返回 true = 可以提交；false = 已提示并中止。
  function checkSubmitConsistency(panelSize) {
    if (!current) return true;
    const source = current.source || {};
    const params = current.params || {};
    const srcW = Number(params.width || 0);
    const srcH = Number(params.height || 0);
    const dstW = Number((panelSize && panelSize[0]) || 0);
    const dstH = Number((panelSize && panelSize[1]) || 0);
    if (!srcW || !srcH || !dstW || !dstH) return true;
    if (srcW === dstW && srcH === dstH) return true;
    toast(`接续被拦下：接续自「${source.job_id || ""}」（${srcW}×${srcH}），`
      + `当前面板是 ${dstW}×${dstH} —— 潜空间桥不能跨分辨率接续。`
      + `请把输出尺寸改成 ${srcW}×${srcH}，或点「清除」改用独立生成。`);
    return false;
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
    clearForReuse,
    checkSubmitConsistency,
    renderPanel,
    state,
    // 【2026-11-17 新增】跨段提示词历史（供「增强」用）。
    //
    // 用户需求：接续时点「增强」要把**此前所有段落**的提示词一并
    // 提供给增强模型，保证跨段内容一致性。
    //
    // 【为什么由前端转交而不是前端自己去查】前端不知道其它任务的
    // id，也无权访问 DB —— 历史必须由服务端在接续上下文里下发。
    //
    // 【返回什么】`[{stage, prompt}, ...]`（按段号顺序，含本段自己）。
    // 增强菜单拿到后会剔除本段再传给 /api/enhance。
    promptHistory() {
      if (!current) return [];
      const list = current.prompt_history;
      if (!Array.isArray(list)) return [];
      return list
        .filter((item) => item && typeof item === "object")
        .map((item) => ({
          stage: Number(item.stage || 0),
          prompt: String(item.prompt || "").trim(),
        }))
        .filter((item) => item.prompt);
    },
    // 供验收脚本直接读（证明「点接续 -> 上下文真的回填了」）
    _debug: {
      get current() { return current; },
      get capability() { return capability; },
    },
  };
})();
