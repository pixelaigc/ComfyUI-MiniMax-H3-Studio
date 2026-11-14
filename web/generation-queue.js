// -*- coding: utf-8 -*-
/**
 * 生成队列（2026-11-13 队列化需求）
 *
 * 【要解决的问题】
 * 旧行为：点「生成」后按钮变成「生成中…」并被 disabled，用户**无法继续添加**
 * 新的生成任务 —— 界面看起来像卡死了。
 *
 * 【后端本来就有的东西】
 * 后端早就是真正的队列：建任务时 status='queued'，Worker 单协程循环按
 * created_at 顺序取一个跑完再取下一个（server.py 的 Worker._loop /
 * _next_queued）。取消接口 /api/jobs/{id}/cancel 也已经支持取消排队中的任务。
 *
 * 【本模块负责的】
 * 1) 队列面板：显示执行顺序、每条的状态（排队中 / 运行中 / 成功 / 失败），
 *    并提供取消按钮。
 * 2) 队列快照的定时刷新。
 *
 * 【按钮态为什么不在这里做】
 * 「生成」按钮还要看「提示词增强中」这个状态（promptEnhance.busy），
 * 而那两个变量是 app.js 的模块内私有常量 —— 本模块拿不到。
 * 所以按钮态统一由 app.js 的 updateActionButtons() 负责，
 * 本模块通过 onChange 回调通知它「队列变了，请重算按钮」。
 *
 * 【队列通道的统一性】
 * 所有生成器（文生图 / 图片编辑 / 文生视频 / 首尾帧 / 参考生视频 / 文生音乐 /
 * 参考生音乐）都走 POST /api/jobs -> create_job_record -> status='queued'
 * -> Worker 串行执行。**没有分通道**：本模块也不按 mode 分流队列，
 * 与后端的单队列语义保持一致。
 */
(function () {
  "use strict";

  // 本模块**先于** app.js 加载，所以不能依赖 app.js 里的全局函数
  // （app.js 的 esc() 在本文件执行时还不存在）。
  // 这里给一份等价实现 —— 语义必须与 app.js / providers.js 的 esc 一致。
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  let queueData = null;              // 最近一次 /api/queue 的结果
  let onChange = null;               // 队列变化回调（app.js 用来重算按钮态）
  let lastSubmitAt = 0;              // 上次提交时间戳（防连点刷屏）
  const MIN_SUBMIT_INTERVAL_MS = 800;

  // ------------------------------------------------------------------
  // 明细区的展开态（2026-11-14 需求 1）
  //
  // 【为什么默认收起】生成队列面板在右侧「生成媒体库」里常驻，
  // 明细列表会把媒体库首屏往下推 —— 用户默认只想知道「排第几」，
  // 不想每次都看一长串明细。
  //
  // 【为什么用模块级变量而不是 DOM style】面板在「队列为空」时会被
  // 整体 hidden 掉又放出来（render() 每秒跑一次），如果展开态只存在
  // DOM 的 inline style 上，任何一次重绘都可能把它重置。放在模块级
  // 变量里 = 「本次会话内记住用户的选择」，重绘不影响。
  let detailExpanded = false;        // 默认收起

  // ------------------------------------------------------------------ 取数

  /**
   * 拉一次队列快照并渲染。
   */
  async function refresh() {
    try {
      const resp = await fetch("/api/queue", {cache: "no-store"});
      if (!resp.ok) return;
      const data = await resp.json();
      queueData = data;
      render(data);
      if (typeof onChange === "function") {
        try { onChange(data); } catch (e) { /* 回调失败不影响渲染 */ }
      }
    } catch (e) {
      /* 网络抖动：保留上一次快照，下一轮再试 */
    }
  }

  // ------------------------------------------------------------------ 渲染

  const QUEUE_STATE_TEXT = {
    running: "运行中",
    queued: "排队中",
    done: "成功",
    failed: "失败",
    canceled: "已取消",
  };

  // ------------------------------------------------------------------
  // 「运行中」的真实状态（A5 需求）
  //
  // 【为什么前端也要兜底】
  // 服务端负责判定「任务是不是真的还在 ComfyUI 里跑」，但前端**不能**
  // 在服务端没给出判定时永远显示「运行中」—— 那正是要消灭的缺陷：
  //   * 服务重启后 DB 里的 status='running' 残留 -> 卡片永远转圈；
  //   * ComfyUI 侧任务已丢失 -> 界面还在说「运行中」，用户干等。
  //
  // 【前端只做「服务端已授权」的兜底，不自己瞎猜】
  // 判定依据**全部**来自服务端下发的字段：
  //   job.running_truth.state  —— active / lost / timeout / unknown
  //   job.running_truth.timeout_sec —— 运行上限（秒）
  //   job.running_truth.deadline_at —— 超时时刻（epoch 秒）
  //   job.started_at           —— 开始时间（老服务端也有）
  //   job.running_timeout_sec  —— 运行上限（顶层冗余一份，便于兼容）
  // 前端**不**自己写死「跑了几分钟算超时」—— 阈值是服务端的知识。
  //
  // 【老服务端兼容】服务端没有 running_truth 时，退回最朴素的判定：
  // 用 started_at + running_timeout_sec 算是否超限；两者都没有就
  // 保持「运行中」（不猜）。

  /** 服务端下发的运行上限（秒）；拿不到就返回 null（表示「不知道」）。 */
  function timeoutSecOf(job) {
    if (!job) return null;
    const fromTruth = job.running_truth && job.running_truth.timeout_sec;
    const value = Number(
      fromTruth !== undefined && fromTruth !== null && fromTruth !== ""
        ? fromTruth
        : job.running_timeout_sec);
    if (!Number.isFinite(value) || value <= 0) return null;
    return value;
  }

  /**
   * 算出这条任务**实际该显示什么**状态。
   *
   * 返回 {state, text}：
   *   state —— "running" | "timeout" | "unknown" | "lost"
   *   text  —— 给用户看的中文
   */
  function truthOf(item) {
    const truth = item && item.running_truth ? item.running_truth : null;
    const state = truth && truth.state ? String(truth.state) : "";
    if (state === "lost") {
      return {state: "lost", text: "已丢失"};
    }
    if (state === "timeout") {
      return {state: "timeout", text: "已超时"};
    }
    if (state === "unknown") {
      return {state: "unknown", text: "状态未知"};
    }
    if (state === "active") {
      return {state: "running", text: "运行中"};
    }
    // 服务端没给判定（老服务端 / 非运行态）-> 用最朴素的本地兜底：
    // 只有在「有上限 + 有开始时间」时才敢判定超时。
    if (!item || item.status !== "running") {
      return {state: item ? item.status : "", text: ""};
    }
    const limit = timeoutSecOf(item);
    const started = Number(item.started_at);
    if (limit && Number.isFinite(started) && started > 0) {
      const elapsed = (Date.now() / 1000) - started;
      if (elapsed > limit) {
        return {state: "timeout", text: "已超时"};
      }
    }
    return {state: "running", text: "运行中"};
  }

  function render(data) {
    const panel = document.getElementById("queuePanel");
    const list = document.getElementById("queueList");
    const summary = document.getElementById("queueSummary");
    if (!panel || !list || !summary) return;

    const items = data.items || [];
    const recent = data.recent || [];
    const total = items.length;

    // 队列里一条都没有、近期也没有完成记录 —— 整个面板隐藏
    if (!total && !recent.length) {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;

    // 摘要：一眼看清「跑了几个 / 排着几个」
    const parts = [];
    if (total) {
      const running = items.filter(i => i.status === "running").length;
      parts.push(running ? `运行中 ${running} · 排队 ${total - running}` : `排队 ${total}`);
      // 【A5】有状态异常时在摘要里点出来 —— 队列面板是用户最常看的位置。
      const abnormal = items.filter(i => {
        const t = truthOf(i);
        return t.state === "timeout" || t.state === "lost" || t.state === "unknown";
      });
      if (abnormal.length) {
        parts.push(`状态异常 ${abnormal.length}`);
      }
    }
    if (recent.length) {
      const done = recent.filter(i => i.status === "done").length;
      const failed = recent.filter(i => i.status === "failed").length;
      if (done || failed) {
        parts.push(`最近完成 ${done}${failed ? ` · 失败 ${failed}` : ""}`);
      }
    }
    summary.textContent = parts.join("　") || "队列为空";

    // 明细列表：先列排队中的（按执行顺序），再列刚跑完的
    const rows = [];
    items.forEach(item => { rows.push(itemHtml(item, true)); });
    recent.forEach(item => { rows.push(itemHtml(item, false)); });
    list.innerHTML = rows.join("");
    bindCancelButtons(list);
    // 每秒重绘都重新套一次展开态 —— 保证「用户展开后本次会话内保持」
    applyDetailState();
  }

  /**
   * 把模块级的展开态套到 DOM 上（列表显隐 + 按钮文案 + aria-expanded）。
   *
   * 单独抽出来是因为 render() 每秒跑一次会整体重写 list.innerHTML，
   * 重写后 inline style 会丢 —— 必须在每次渲染后重新应用。
   */
  function applyDetailState() {
    const list = document.getElementById("queueList");
    const toggle = document.getElementById("queueToggle");
    if (!list || !toggle) return;
    list.style.display = detailExpanded ? "" : "none";
    toggle.setAttribute("aria-expanded", detailExpanded ? "true" : "false");
    toggle.textContent = detailExpanded ? "收起" : "明细";
    toggle.setAttribute("title", detailExpanded ? "收起队列明细" : "展开队列明细");
  }

  function itemHtml(item, inQueue) {
    // 【A5】状态文案按「真实判定」来，而不是只看 job.status。
    // 终态（done/failed/canceled）沿用老文案；running 走 truthOf()。
    const truth = truthOf(item);
    const isRunningLike = truth.state === "running" || truth.state === "timeout"
      || truth.state === "unknown" || truth.state === "lost";
    const cls = inQueue
      ? (isRunningLike ? "is-running" : "is-queued")
      : "is-recent";
    const stateText = inQueue && isRunningLike
      ? truth.text
      : (QUEUE_STATE_TEXT[item.status] || item.status || "");
    // 排队中的显示「第 N 位」；运行中的显示 ▶；终态显示 ·
    const posText = inQueue
      ? (isRunningLike ? "▶" : String(item.position || ""))
      : "·";
    const title = item.prompt || "";
    const cancelable = inQueue;   // 终态任务不能取消
    // 状态异常（已超时 / 已丢失 / 状态未知）时给 title 说明原因 ——
    // 用户一眼能点开看到发生了什么。
    const stateTitle = isRunningLike && truth.state !== "running"
      ? ` title="${esc(truth.state === "timeout"
          ? "已超过运行上限仍未完成"
          : (truth.state === "lost"
              ? "ComfyUI 队列与历史中已无该任务（可能被取消或丢失）"
              : "暂时无法确认任务是否仍在运行"))}"`
      : "";
    // 状态异常的条目加一个 class，让 CSS 能把它标成警示色。
    const warnCls = (isRunningLike && truth.state !== "running")
      ? ` is-warn-${esc(truth.state)}` : "";
    return `<li class="queue-item ${cls}${warnCls}" data-jid="${esc(item.id || "")}">`
      + `<span class="queue-pos">${esc(posText)}</span>`
      + `<span class="queue-label">${esc(item.label || item.mode || "")}</span>`
      + `<span class="queue-prompt" title="${esc(title)}">${esc(title)}</span>`
      + `<span class="queue-state"${stateTitle}>${esc(stateText)}</span>`
      + (cancelable
        ? `<button type="button" class="queue-cancel" data-qid="${esc(item.id || "")}">取消</button>`
        : "")
      + `</li>`;
  }

  function bindCancelButtons(list) {
    list.querySelectorAll(".queue-cancel").forEach(button => {
      button.addEventListener("click", async () => {
        const id = button.dataset.qid;
        if (!id) return;
        button.disabled = true;
        button.textContent = "取消中…";
        try {
          const resp = await fetch(`/api/jobs/${encodeURIComponent(id)}/cancel`,
                                   {method: "POST"});
          if (!resp.ok) {
            const body = await resp.json().catch(() => ({}));
            throw new Error(body.error || "取消失败");
          }
          await refresh();
        } catch (err) {
          button.disabled = false;
          button.textContent = "取消";
          alert(err.message);
        }
      });
    });
  }

  // ------------------------------------------------------------------ 提交节流

  /**
   * 提交前的节流：极短时间内的重复点击直接忽略。
   *
   * 【为什么需要】按钮不再被禁用之后，用户可能连点两下 ->
   * 队列里多出一条**完全相同**的任务。这是误操作，不是队列化的本意。
   * 800ms 足够区分「故意连做多个」与「手抖连点」。
   */
  function shouldAcceptSubmit() {
    const now = Date.now();
    if (lastSubmitAt && now - lastSubmitAt < MIN_SUBMIT_INTERVAL_MS) return false;
    lastSubmitAt = now;
    return true;
  }

  // ------------------------------------------------------------------ 初始化

  function init() {
    const toggle = document.getElementById("queueToggle");
    const list = document.getElementById("queueList");
    if (toggle && list) {
      toggle.addEventListener("click", () => {
        // 反转模块级状态 —— 不是「读 DOM 再改 DOM」。
        // 读 DOM 的写法在 render() 重写 innerHTML 后会失同步。
        detailExpanded = !detailExpanded;
        applyDetailState();
      });
    }
    // 初始就是收起（detailExpanded 默认 false）
    applyDetailState();
    refresh();
    // 队列比任务列表变化更快 —— 1 秒一次
    setInterval(() => refresh(), 1000);
  }

  // ------------------------------------------------------------------ 对外接口

  window.GenerationQueue = {
    init: init,
    refresh: refresh,
    shouldAcceptSubmit: shouldAcceptSubmit,
    set onChange(fn) { onChange = fn; },
    get data() { return queueData; },
    get pendingCount() { return queueData ? (queueData.items || []).length : 0; },
    // 【A5】把「这条任务到底在不在跑」的判定暴露出去，
    // 让 app.js 的卡片渲染也能用**同一套**口径（而不是各写一份）。
    //
    // 主线在 app.js 的 jobCard() 里补这一行即可：
    //   const truth = window.GenerationQueue.truthOf(job);
    //   // truth.state: "running" | "timeout" | "unknown" | "lost" | 其它
    //   // truth.text : 对应的中文（"运行中" / "已超时" / "状态未知" / "已丢失"）
    // 然后在 progress-text 里用 truth.text 代替写死的「生成中」。
    truthOf: truthOf,
  };
})();
