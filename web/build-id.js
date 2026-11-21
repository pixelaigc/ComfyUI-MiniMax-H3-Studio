/* ============================================================
   构建版本标识（2026-11-21）
   ------------------------------------------------------------
   【为什么需要】
   前一轮用户报「选完生成类型回不去 / 看不到生成媒体库」，
   而在 8190 上怎么测都复现不了。真正的卡点是：
   **无法确认用户手机上的页面是不是当前这一版代码**。

   没有版本标识时，排查只能靠猜：
     「是不是缓存了？」「是不是访问了别的端口？」「是不是我记错了？」
   ⇒ 只能给出一堆**无法证伪**的替代解释。

   有了它，用户看一眼顶栏就知道自己跑的是哪一版，
   下一次反馈能直接对照，不必再猜。

   【显示在哪：两处】
   1) **顶栏**（.brand-name 内）—— commit 短哈希，7 个字符。
      【为什么只放 commit】
      实测顶栏在 320px 下的富余空间只有 ~33px
      （汉堡 36 + 品牌 147 + v1.00 27 + 状态灯 39 + 内边距 20）。
      放「commit + 静态资源指纹」两段会被压到 0 宽（实测 w=0），
      放不下就等于没有。而 commit 短哈希是**最有辨识度**的那一段。
   2) **系统页「构建版本」卡片** —— 完整信息：
      commit / 静态资源版本指纹 / 服务启动时间 / 部署目录。
      【为什么不都塞顶栏】顶栏空间不够；系统页是既有的设置区，
      放那儿不新增任何导航分组（左侧仍是 7 个顶层分组）。

   【接口】GET /api/version（服务端 h3studio/build_info.py）
   【失败怎么办】接口 404 / 报错 ⇒ 显示「—」并标灰，
   **绝不影响页面其它功能**（这只是一行小字，不能成为故障源）。
   ============================================================ */
(function () {
  "use strict";

  const info = {
    commit: null,
    assets_id: null,
    built_at: null,
    served_from: null,
  };

  function el(id) {
    return document.getElementById(id);
  }

  function fmtTime(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
           " " + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  /** 顶栏：只显示 commit 短哈希。 */
  function renderTop() {
    const box = el("buildId");
    const val = el("buildIdValue");
    if (!box || !val) return;
    if (!info.commit) {
      val.textContent = "—";
      box.classList.add("is-unknown");
      box.title = "取不到构建版本（服务端未提供 /api/version）";
      return;
    }
    val.textContent = info.commit;
    box.classList.remove("is-unknown");
    const t = ["提交：" + info.commit];
    if (info.assets_id) t.push("静态资源版本：" + info.assets_id);
    if (info.built_at) t.push("服务启动：" + fmtTime(info.built_at));
    if (info.served_from) t.push("目录：" + info.served_from);
    t.push("完整信息见「系统 → 硬件参数」页的「构建版本」卡片");
    box.title = t.join("\n");
  }

  /** 系统页：完整信息。 */
  function renderDetail() {
    const box = el("buildIdDetail");
    if (!box) return;
    if (!info.commit) {
      box.textContent = "取不到构建版本（服务端未提供 /api/version）";
      return;
    }
    const rows = [
      ["提交", info.commit],
      ["静态资源版本", info.assets_id || "—"],
      ["服务启动", fmtTime(info.built_at)],
      ["部署目录", info.served_from || "—"],
    ];
    box.innerHTML = rows.map((r) =>
      '<div class="build-id-row"><span class="build-id-k">' +
      escapeHtml(r[0]) + '</span><span class="build-id-v">' +
      escapeHtml(String(r[1])) + "</span></div>").join("");
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function render() {
    renderTop();
    renderDetail();
  }

  async function refresh() {
    try {
      const resp = await fetch("/api/version", { cache: "no-store" });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const data = await resp.json();
      info.commit = data.commit || null;
      info.assets_id = data.assets_id || null;
      info.built_at = data.built_at || null;
      info.served_from = data.served_from || null;
      render();
      return info;
    } catch (e) {
      // 取不到就显示「—」，但**不抛**、不影响页面
      info.commit = null;
      render();
      return null;
    }
  }

  window.BuildId = { info, refresh, render };

  function boot() {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", refresh);
    } else {
      refresh();
    }
  }

  try {
    boot();
  } catch (e) {
    console.error("[build-id] 初始化失败:", e);
  }
})();
