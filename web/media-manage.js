/* ==================================================================
 * 生成媒体库「管理」（2026-11-14 需求 11）
 *
 * 【功能】
 * 在「生成媒体库」页加一个「管理」按钮，弹出的界面支持三种清理口径：
 *   1. 清理本工作台内的所有生成媒体库产物
 *   2. 清理指定月数之前的历史产物（月份可配）
 *   3. 清理失败 / 取消的产物
 *
 * 【安全设计】
 *   * 每次都先「预览」再「执行」—— 执行按钮上写清楚会删多少条/多少文件；
 *   * 执行前再 confirm 一次（不可逆操作）；
 *   * 服务端要求 confirm=true，且只动本项目的 jobs / job_batches 表
 *     与本项目 outputs 目录 —— 不碰 ComfyUI 自身产物、不碰素材库。
 * ================================================================== */
(function () {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  let current = { scope: "all", months: 1 };

  async function api(path, options = {}) {
    const resp = await fetch(path, options);
    let body = {};
    try { body = await resp.json(); } catch (e) { /* 非 JSON */ }
    if (!resp.ok) {
      const err = new Error(body.error || `请求失败（HTTP ${resp.status}）`);
      err.payload = body;
      err.status = resp.status;
      throw err;
    }
    return body;
  }

  function scopeValue() {
    const el = document.querySelector('input[name="mgScope"]:checked');
    return el ? el.value : "all";
  }

  function monthsValue() {
    const el = $("#mgMonths");
    const v = Number(el ? el.value : 1);
    return Number.isFinite(v) && v > 0 ? Math.floor(v) : 1;
  }

  function open() {
    const modal = $("#mediaManageModal");
    if (!modal) return;
    modal.hidden = false;
    document.body.classList.add("modal-open");
    refreshPreview();
  }

  function close() {
    const modal = $("#mediaManageModal");
    if (!modal) return;
    modal.hidden = true;
    // 只开这一个框，但通用逻辑要看全部框 —— 复用 app.js 的同一套判断，
    // 这里自己做一份最小实现（app.js 的 syncModalOpenClass 不是全局的）。
    const anyOpen = ["promptModal", "loraInfoModal", "enhanceGateModal",
                     "detailModal", "mediaManageModal"].some(id => {
      const el = document.getElementById(id);
      return !!(el && !el.hidden);
    });
    document.body.classList.toggle("modal-open", anyOpen);
  }

  async function refreshPreview() {
    const box = $("#mgPreview");
    if (!box) return;
    current.scope = scopeValue();
    current.months = monthsValue();
    const q = new URLSearchParams({ scope: current.scope,
                                    months: String(current.months) });
    box.textContent = "正在统计…";
    try {
      const info = await api("/api/media/manage?" + q.toString());
      box.innerHTML = `<div class="mg-stat">
          将清理 <b>${esc(info.jobs)}</b> 条任务记录、
          <b>${esc(info.files)}</b> 个产物文件
        </div>
        <div class="mg-note">只影响本工作台（不含 ComfyUI 自身的产物目录，不含素材库）。
          正在运行 / 排队中的任务不会被清理。</div>`;
    } catch (e) {
      box.innerHTML = `<div class="mg-stat error">统计失败：${esc(e.message)}</div>`;
    }
  }

  async function run() {
    current.scope = scopeValue();
    current.months = monthsValue();
    const label = {
      all: "本工作台内的所有生成媒体库产物",
      older: `${current.months} 个月之前的历史产物`,
      failed: "失败 / 取消的产物",
    }[current.scope];
    if (!window.confirm(
      `确认清理：${label}？\n\n` +
      `这会删除对应的任务记录与产物文件，「不可撤销」。\n` +
      `（只影响本工作台，不含 ComfyUI 自身产物与素材库）`)) {
      return;
    }
    const btn = $("#mgRunBtn");
    if (btn) { btn.disabled = true; btn.textContent = "清理中…"; }
    try {
      const r = await api("/api/media/manage", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          scope: current.scope, months: current.months, confirm: true,
        }),
      });
      window.alert(
        `清理完成：\n\n` +
        `删除任务记录 ${r.deleted_jobs} 条\n` +
        `删除产物文件 ${r.deleted_files} 个\n` +
        (r.skipped_running ? `跳过进行中的任务 ${r.skipped_running} 条\n` : "") +
        (r.deleted_batches ? `删除批次记录 ${r.deleted_batches} 条` : ""));
      close();
      // 让列表刷新 —— 走 app.js 的公开入口
      if (window.APP && typeof window.APP.refreshJobs === "function") {
        window.APP.refreshJobs();
      } else {
        window.dispatchEvent(new CustomEvent("h3:jobs-changed"));
      }
    } catch (e) {
      window.alert("清理失败：" + (e.message || e));
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "确认清理"; }
    }
  }

  function bind() {
    const btn = $("#mediaManageBtn");
    if (btn && !btn.dataset.bound) {
      btn.dataset.bound = "1";
      btn.addEventListener("click", open);
    }
    const closeBtn = $("#mediaManageClose");
    if (closeBtn && !closeBtn.dataset.bound) {
      closeBtn.dataset.bound = "1";
      closeBtn.addEventListener("click", close);
    }
    const runBtn = $("#mgRunBtn");
    if (runBtn && !runBtn.dataset.bound) {
      runBtn.dataset.bound = "1";
      runBtn.addEventListener("click", run);
    }
    document.querySelectorAll('input[name="mgScope"]').forEach(radio => {
      if (radio.dataset.bound) return;
      radio.dataset.bound = "1";
      radio.addEventListener("change", refreshPreview);
    });
    const months = $("#mgMonths");
    if (months && !months.dataset.bound) {
      months.dataset.bound = "1";
      months.addEventListener("change", refreshPreview);
    }
    const modal = $("#mediaManageModal");
    if (modal && !modal.dataset.bound) {
      modal.dataset.bound = "1";
      modal.addEventListener("click", event => {
        if (event.target === modal) close();
      });
    }
    document.addEventListener("keydown", event => {
      const m = $("#mediaManageModal");
      if (event.key === "Escape" && m && !m.hidden) close();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }

  window.MediaManager = { open, close, refreshPreview, run };
})();
