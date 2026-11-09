/* ============================================================
   MOSS 结构化增强（layout）—— 产物弹窗
   ------------------------------------------------------------
   【入口在哪】
   结构化增强**不再是独立按钮** —— 它是「增强」下拉菜单里的一个增强方式
   （见 web/enhance-menu.js 的 ENHANCE_METHODS 注册表）。
   主按钮选中「结构化增强（layout）」后点「增强」，就会调 POST /api/moss-layout。

   本文件只负责**产物弹窗**：展示 layout JSON（可查看/编辑/复制）与
   确定性翻译出的最终文本提示词，并处理回填。
   入口、路由、busy 状态全部由 enhance-menu.js 统一管 —— 这里不重复。

   与其它增强方式保持一致的回填语义：
     产物文本 -> 写进提示词框 -> 标记「已增强」-> 进版本历史
   ============================================================ */
(function () {
  "use strict";

  // 最近一次结构化增强的产物（弹窗关闭后可再次打开查看）
  let lastResult = null;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g,
      c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // ------------------------------------------------------------------ 渲染

  /**
   * 展示产物。
   * @param {object} d  /api/moss-layout 的响应
   */
  function renderResult(d) {
    lastResult = d;
    const meta = document.querySelector("#mossLayoutMeta");
    if (meta) {
      meta.innerHTML = `模型：<b>${esc(d.provider || "—")}</b> · `
        + `画布：${esc(d.width)}×${esc(d.height)} · `
        + `元素：${esc(d.layout && d.layout.items ? d.layout.items.length : 0)} 个 · `
        + `文本提示词 ${esc((d.final_prompt || "").length)} 字`
        + `<div class="enhance-warn">Layout JSON 为官方 MOSS 格式`
        + `（bbox_coord_space=abs_centralized，bbox_format=yxyx，坐标以画布中心为原点）。`
        + `最终文本提示词由这份 layout 确定性翻译而来，可直接送 CLIPTextEncode。</div>`;
    }
    const promptEl = document.querySelector("#mossLayoutPrompt");
    if (promptEl) promptEl.value = d.final_prompt || "";
    const jsonEl = document.querySelector("#mossLayoutJson");
    if (jsonEl) jsonEl.value = d.layout_json || "";
    const draftEl = document.querySelector("#mossLayoutDraft");
    if (draftEl) draftEl.value = JSON.stringify(d.draft || {}, null, 2);
    const hint = document.querySelector("#mossLayoutHint");
    if (hint) {
      // 画布尺寸可能被后端吸附过（16 倍数 / 像素区间）—— 有变化要说明，
      // 否则用户会以为 layout 的坐标跟自己填的尺寸对不上。
      const notes = d.size_notes || [];
      hint.textContent = notes.length
        ? `画布尺寸已按 MOSS 规则归一：${notes.join("；")}。`
        : "文本框内容可直接编辑；回填时会把当前编辑后的内容写进提示词框。";
    }
    // 默认停在「最终文本提示词」页
    selectTab("prompt");
    const viewBtn = document.querySelector("#mossLayoutViewBtn");
    if (viewBtn) viewBtn.disabled = false;
  }

  function selectTab(tab) {
    const map = {
      prompt: "#mossLayoutPrompt",
      layout: "#mossLayoutJson",
      draft: "#mossLayoutDraft",
    };
    Object.keys(map).forEach(key => {
      const el = document.querySelector(map[key]);
      if (el) el.hidden = key !== tab;
    });
    document.querySelectorAll("#mossLayoutModal .moss-layout-tabs button").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.tab === tab);
    });
  }

  function openModal() {
    const modal = document.querySelector("#mossLayoutModal");
    if (!modal) return;
    modal.hidden = false;
    document.body.classList.add("modal-open");
  }

  function closeModal() {
    const modal = document.querySelector("#mossLayoutModal");
    if (!modal) return;
    modal.hidden = true;
    document.body.classList.remove("modal-open");
  }

  // ------------------------------------------------------------------ 复制

  async function copyText(text, okMessage) {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setHint(okMessage, "ok");
    } catch (e) {
      // 剪贴板不可用（http / 无权限）时降级为选中文本，用户可手动复制
      const area = document.querySelector("#mossLayoutPrompt");
      if (area) {
        area.focus();
        area.select();
      }
      setHint("复制失败，已选中文本，请手动复制（Ctrl+C）", "error");
    }
  }

  function setHint(text, kind) {
    const hint = document.querySelector("#enhanceHint");
    if (!hint) return;
    hint.textContent = text;
    hint.className = "enhance-hint" + (kind ? " " + kind : "");
  }

  // ------------------------------------------------------------------ 绑定

  function bind() {
    document.querySelectorAll("#mossLayoutModal .moss-layout-tabs button").forEach(b => {
      b.addEventListener("click", () => selectTab(b.dataset.tab));
    });

    const close = document.querySelector("#mossLayoutClose");
    if (close) close.addEventListener("click", closeModal);
    const cancel = document.querySelector("#mossLayoutCancel");
    if (cancel) cancel.addEventListener("click", closeModal);

    const modal = document.querySelector("#mossLayoutModal");
    if (modal) {
      modal.addEventListener("click", e => {
        if (e.target === modal) closeModal();
      });
    }
    document.addEventListener("keydown", e => {
      if (e.key === "Escape" && modal && !modal.hidden) closeModal();
    });

    const copyJson = document.querySelector("#mossLayoutCopyJson");
    if (copyJson) {
      copyJson.addEventListener("click", () => {
        const el = document.querySelector("#mossLayoutJson");
        copyText(el ? el.value : "", "已复制 Layout JSON 到剪贴板");
      });
    }
    const copyPrompt = document.querySelector("#mossLayoutCopyPrompt");
    if (copyPrompt) {
      copyPrompt.addEventListener("click", () => {
        const el = document.querySelector("#mossLayoutPrompt");
        copyText(el ? el.value : "", "已复制文本提示词到剪贴板");
      });
    }

    // 回填：与其它增强方式一致的语义 —— 写进提示词框 + 标记已增强 + 进版本历史
    const apply = document.querySelector("#mossLayoutApply");
    if (apply) {
      apply.addEventListener("click", () => {
        const el = document.querySelector("#mossLayoutPrompt");
        const text = el ? el.value.trim() : "";
        if (!text) {
          setHint("最终文本提示词是空的，无法回填", "error");
          return;
        }
        const promptEl = document.querySelector("#prompt");
        if (promptEl) promptEl.value = text;
        closeModal();
        if (typeof markPromptEnhanced === "function") markPromptEnhanced();
        if (typeof pushPromptVersion === "function") {
          pushPromptVersion(1, text, "MOSS 结构化增强（layout 翻译）", "moss-layout");
        }
        setHint("已回填结构化增强的文本提示词（已标记为已增强）", "ok");
      });
    }

    // 「查看产物」按钮：重新打开上一次的结果
    const viewBtn = document.querySelector("#mossLayoutViewBtn");
    if (viewBtn) {
      viewBtn.disabled = true;
      viewBtn.addEventListener("click", () => {
        if (!lastResult) {
          setHint("还没有结构化增强产物，先在「增强」下拉里选「结构化增强（layout）」", "error");
          return;
        }
        openModal();
      });
    }
  }

  // ------------------------------------------------------------------ 启动

  function boot() {
    bind();
    if (typeof window !== "undefined") {
      window.mossLayoutUI = {
        renderResult: renderResult,
        openModal: openModal,
        closeModal: closeModal,
        selectTab: selectTab,
        getLastResult: () => lastResult,
      };
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
