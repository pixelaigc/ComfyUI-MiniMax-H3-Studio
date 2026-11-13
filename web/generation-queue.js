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
  }

  function itemHtml(item, inQueue) {
    const cls = inQueue
      ? (item.status === "running" ? "is-running" : "is-queued")
      : "is-recent";
    const stateText = QUEUE_STATE_TEXT[item.status] || item.status || "";
    // 排队中的显示「第 N 位」；运行中的显示 ▶；终态显示 ·
    const posText = inQueue
      ? (item.status === "running" ? "▶" : String(item.position || ""))
      : "·";
    const title = item.prompt || "";
    const cancelable = inQueue;   // 终态任务不能取消
    return `<li class="queue-item ${cls}" data-jid="${esc(item.id || "")}">`
      + `<span class="queue-pos">${esc(posText)}</span>`
      + `<span class="queue-label">${esc(item.label || item.mode || "")}</span>`
      + `<span class="queue-prompt" title="${esc(title)}">${esc(title)}</span>`
      + `<span class="queue-state">${esc(stateText)}</span>`
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
        const collapsed = list.style.display === "none";
        list.style.display = collapsed ? "" : "none";
        toggle.setAttribute("aria-expanded", collapsed ? "true" : "false");
        toggle.textContent = collapsed ? "明细" : "收起";
      });
    }
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
  };
})();
