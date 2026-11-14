/* ============================================================
   交互流程图入口（2026-11-15 D12 线上可达）
   ------------------------------------------------------------
   【为什么要有这个模块】
   D12 的 5 套交互流程图（总览 / 创作链路 / 素材库 / 内置Agent / 系统配置）
   与《交互流程图说明.md》早就随 D12 进了仓库，但线上 404 —— 用户在界面里
   没有任何入口能看到「这个工作台到底怎么用」。本模块负责：
     1. 从 /api/docs/flows 拿清单（**前端不硬编码文件名**）；
     2. 点入口 -> 打开流程图面板，列出 5 套图；
     3. 点某套图 -> 在面板里直接看 PNG；
     4. 每套图都能点开对应的 .mmd 源文件（看 Mermaid 原文）与
        说明文档。

   【为什么清单走接口而不是写死在前端】
   流程图文件改名/增删时，写死的清单会静默指向不存在的路径 ——
   用户点了 404，却没有任何报错。走接口时服务端按**磁盘实际内容**下发，
   文件不在就不下发，前端也就不会列出坏链接。

   【为什么不复用 lightbox】
   lightbox 只能放**一张**图。流程图是「5 套图 + 每套图的源文件」的
   列表式浏览，用 lightbox 会让用户「一次只能看一张、看完要自己记住
   上一张叫什么」。这里用独立面板，一屏看全 5 套，点哪套看哪套。

   【对外接口】
     window.FlowDocs.open()      打开面板
     window.FlowDocs.close()     关闭面板
     window.FlowDocs.load()      拉清单（测试可直接调用）
   ============================================================ */
(function () {
  "use strict";

  const $ = s => document.querySelector(s);

  let manifest = null;        // 服务端下发的清单
  let loading = false;

  // ---------------------------------------------------------------- 数据

  async function load() {
    if (loading) return manifest;
    loading = true;
    try {
      const resp = await fetch("/api/docs/flows", { cache: "no-store" });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      manifest = await resp.json();
    } catch (e) {
      manifest = { diagrams: [], guide: null, docs_index_url: "/docs", error: String(e && e.message || e) };
    } finally {
      loading = false;
    }
    return manifest;
  }

  // ---------------------------------------------------------------- 渲染

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function renderBody() {
    const box = $("#fdBody");
    if (!box) return;
    const m = manifest || { diagrams: [], guide: null };

    if (m.error) {
      box.innerHTML = '<div class="fd-empty">流程图清单读取失败：' + esc(m.error) + '</div>';
      return;
    }
    const list = m.diagrams || [];
    if (!list.length) {
      box.innerHTML = '<div class="fd-empty">当前部署里没有找到交互流程图文件。</div>';
      return;
    }

    // 5 套图：缩略图 + 标题 + 源文件链接
    const cards = list.map(d => {
      const srcLink = d.mmd
        ? '<a class="fd-link" href="' + esc(d.mmd) + '" target="_blank" rel="noopener">源文件 .mmd</a>'
        : '<span class="fd-link disabled">无源文件</span>';
      const svgLink = d.svg
        ? '<a class="fd-link" href="' + esc(d.svg) + '" target="_blank" rel="noopener">SVG</a>'
        : "";
      return '<figure class="fd-card" data-fd-png="' + esc(d.png) + '" data-fd-title="' + esc(d.title) + '">'
        + '<img class="fd-thumb" src="' + esc(d.png) + '" alt="' + esc(d.title) + ' 流程图" loading="lazy">'
        + '<figcaption class="fd-cap">'
        + '<b class="fd-title">' + esc(d.title) + '</b>'
        + '<span class="fd-links">' + srcLink + svgLink + '</span>'
        + '</figcaption></figure>';
    }).join("");

    const guide = m.guide
      ? '<div class="fd-guide"><a href="' + esc(m.guide.url) + '" target="_blank" rel="noopener">'
        + '📖 ' + esc(m.guide.name) + '</a></div>'
      : "";
    const all = '<div class="fd-guide"><a href="/docs" target="_blank" rel="noopener">📁 打开全部文档目录</a></div>';

    box.innerHTML = guide + all + '<div class="fd-grid">' + cards + '</div>';
  }

  // ---------------------------------------------------------------- 打开/关闭

  function open() {
    const modal = $("#fdModal");
    if (!modal) return;
    modal.hidden = false;
    // 滚动锁走 app.js 的统一维护（window.H3Studio.syncModalOpenClass）——
    // 自己 add("modal-open") 会在别的模态框也开着时把锁状态搞乱。
    syncLock(true);
    renderBody();
    load().then(renderBody);
  }

  function close() {
    const modal = $("#fdModal");
    if (!modal) return;
    modal.hidden = true;
    syncLock(false);
  }

  /** 统一维护 body.modal-open（有 app.js 的实现就用它的，没有就退化为本地开关）。 */
  function syncLock() {
    if (window.H3Studio && typeof window.H3Studio.syncModalOpenClass === "function") {
      window.H3Studio.syncModalOpenClass();
      return;
    }
    if (document.getElementById("fdModal") && document.getElementById("fdModal").hidden) {
      document.body.classList.remove("modal-open");
    } else {
      document.body.classList.add("modal-open");
    }
  }

  // ---------------------------------------------------------------- 绑定

  function bind() {
    const nav = $("#navFlowDocs");
    if (nav) nav.addEventListener("click", open);

    const btn = $("#fdOpenBtn");
    if (btn) btn.addEventListener("click", open);

    const x = $("#fdClose");
    if (x) x.addEventListener("click", close);

    const done = $("#fdDone");
    if (done) done.addEventListener("click", close);

    const modal = $("#fdModal");
    if (modal) {
      modal.addEventListener("click", event => {
        if (event.target === modal) close();
      });
    }

    document.addEventListener("keydown", event => {
      if (event.key === "Escape") {
        const m = $("#fdModal");
        if (m && !m.hidden) close();
      }
    });

    // 点缩略图 -> 在面板里放大显示（不开新窗口）
    document.addEventListener("click", event => {
      const card = event.target.closest(".fd-card");
      if (!card) return;
      const png = card.getAttribute("data-fd-png");
      const title = card.getAttribute("data-fd-title");
      if (!png) return;
      const stage = $("#fdStage");
      const img = $("#fdStageImg");
      if (!stage || !img) return;
      img.src = png;
      img.alt = (title || "") + " 流程图（大图）";
      stage.hidden = false;
    });

    const closeStage = $("#fdStageClose");
    if (closeStage) {
      closeStage.addEventListener("click", () => {
        const stage = $("#fdStage");
        if (stage) stage.hidden = true;
      });
    }
  }

  // ---------------------------------------------------------------- 导出

  window.FlowDocs = {
    open: open,
    close: close,
    load: load,
    render: renderBody,
    get manifest() { return manifest; },
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
