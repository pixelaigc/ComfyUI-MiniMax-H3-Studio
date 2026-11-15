/* ============================================================
   H3 Studio —— 媒体预览模态框（2026-11-15 修复③ 交付项 2）
   ------------------------------------------------------------
   需求：图片编辑类能力（H3-IMAGE 图片编辑 / Qwen-Image 2.1 图片编辑等）
   从素材库添加素材后，**点击已添加的媒体条目**应弹出模态框：
     * 图片：可放大观察（缩放 / 适应窗口 / 原始尺寸 / 滚轮缩放 / 拖拽平移）
     * 视频：可播放（播放 / 暂停 / 静音 / 进度 / 音量）
     * 音频 / 音乐：可播放（播放 / 暂停 / 静音 / 进度 / 音量）
   模态框可关闭、点遮罩可关、播放可控。

   【复用既有组件，不重复造轮子】
   图片部分**直接复用 app.js 的 lightbox**（#lightbox）：
   它已经调好了缩放 / 拖拽 / 适应屏幕 / 原始尺寸 / 下载 / 新窗口打开，
   并且 agent_sessions.js 已经是通过 window.H3Studio.lightboxShow 复用它的。
   自己再写一个「只有一张大图」的模态框，功能更少、样式还得再对一遍 ——
   两份大图查看器必然漂移。

   视频 / 音频部分没有现成组件（素材详情抽屉 #alDrawer 里的
   <video>/<audio> 是**内嵌**在抽屉里的，不能放大、也不是模态），
   所以这里做一个**媒体模态框**：图片分支转交给 lightbox，
   视频 / 音频分支用原生 <video>/<audio> 的 controls（浏览器原生控件
   自带播放 / 暂停 / 静音 / 进度 / 音量），并额外提供一个显式的
   「静音」按钮（原生控件在部分浏览器上静音按钮不明显）。

   对外 API：
     window.MediaPreview.show({url, kind, name, mime})  —— 打开模态框
     window.MediaPreview.showForAsset(asset)            —— 按素材对象打开
     window.MediaPreview.close()
   ============================================================ */
(function () {
  "use strict";

  const $ = s => document.querySelector(s);

  // ------------------------------------------------------------ 状态

  let currentMedia = null;     // 当前播放的 <video>/<audio> 元素
  let currentKind = "";        // "image" / "video" / "audio"

  // ------------------------------------------------------------ 分类

  /**
   * 从 URL / MIME / 素材类别判断媒体类型。
   *
   * 【为什么要多路判断】素材库里同一个条目的 preview_url 可能是 .png
   * （缩略图），但 content_url 指向的是原始媒体（可能是 .mp4）；
   * 而 API 返回的 category 字段才是权威。优先用 category，
   * 其次用 MIME，最后看扩展名。
   *
   * 【重要 —— 显式传入的 kind 优先】
   * 调用方（面板槽位、参考图列表）**已经知道**这个素材是什么类型
   * （槽位 kind = image / video / audio），那是权威信息。
   * 如果这里再按 URL / category 推断，就会出现：
   *   槽位说「这是视频」，而 URL 是 /api/assets/<id>/content
   *   （没有扩展名）-> 推断成 audio -> 弹出音频播放器。
   * 这是实测踩到的真实 bug（视频弹成了音频）。
   */
  function detectKind(url, category, mime, explicit) {
    const ex = String(explicit || "").toLowerCase();
    if (ex === "video" || ex === "audio" || ex === "image") return ex;
    const cat = String(category || "").toLowerCase();
    if (cat === "video" || cat === "audio" || cat === "image") return cat;
    const m = String(mime || "").toLowerCase();
    if (m.startsWith("video/")) return "video";
    if (m.startsWith("audio/")) return "audio";
    if (m.startsWith("image/")) return "image";
    const text = String(url || "").split("?")[0].toLowerCase();
    if (/\.(mp4|webm|mov|m4v|avi|mkv)$/.test(text)) return "video";
    if (/\.(mp3|wav|flac|ogg|oga|m4a|aac|opus)$/.test(text)) return "audio";
    return "image";
  }

  // ------------------------------------------------------------ 模态框

  function ensureModal() {
    let modal = $("#mediaPreviewModal");
    if (modal) return modal;

    modal = document.createElement("div");
    modal.className = "media-preview-modal";
    modal.id = "mediaPreviewModal";
    modal.hidden = true;
    modal.innerHTML = `
      <div class="media-preview-box" role="dialog" aria-modal="true"
           aria-labelledby="mediaPreviewTitle">
        <div class="media-preview-head">
          <b id="mediaPreviewTitle">媒体预览</b>
          <div class="media-preview-tools">
            <button type="button" id="mediaPreviewMute" hidden>静音</button>
            <button type="button" id="mediaPreviewClose" aria-label="关闭">×</button>
          </div>
        </div>
        <div class="media-preview-body" id="mediaPreviewBody"></div>
      </div>`;
    document.body.appendChild(modal);

    // 关闭按钮
    const closeBtn = $("#mediaPreviewClose");
    if (closeBtn) closeBtn.addEventListener("click", close);
    // 静音按钮（视频 / 音频）
    const muteBtn = $("#mediaPreviewMute");
    if (muteBtn) {
      muteBtn.addEventListener("click", () => {
        if (!currentMedia) return;
        currentMedia.muted = !currentMedia.muted;
        muteBtn.textContent = currentMedia.muted ? "取消静音" : "静音";
      });
    }
    // 点遮罩关闭（点在对话框内不关）
    modal.addEventListener("click", event => {
      if (event.target === modal) close();
    });
    // Esc 关闭
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && !modal.hidden) {
        // lightbox 自己管 Esc（它有自己的监听器），这里只在模态框开着时兜底
        if (!($("#lightbox") && !$("#lightbox").hidden)) close();
      }
    });
    return modal;
  }

  /** 停掉当前播放（切类型 / 关闭时必须停，否则声音继续响）。 */
  function stopCurrent() {
    if (currentMedia) {
      try { currentMedia.pause(); } catch (e) { /* ignore */ }
      try { currentMedia.removeAttribute("src"); currentMedia.load(); } catch (e) { /* ignore */ }
      currentMedia = null;
    }
  }

  // ------------------------------------------------------------ 打开

  /**
   * 打开媒体预览模态框。
   *
   * @param {object} opts
   *   url     —— 媒体地址（必填）
   *   kind    —— "image" / "video" / "audio"（不传则自动判断）
   *   name    —— 标题文字
   *   mime    —— MIME（辅助判断类型）
   *   category—— 素材类别（辅助判断类型）
   */
  function show(opts) {
    const o = opts || {};
    const url = o.url || o.content_url || o.preview_url;
    if (!url) return false;
    const kind = detectKind(url, o.category, o.mime, o.kind);

    // 【先清掉上一个媒体】否则从「视频预览」切到「图片预览」时，
    // 旧的 <video> 还留在模态框里（图片走 lightbox，模态框不会重绘）——
    // 用户看到的是「关掉大图后还杵着一个播放器」。
    closeModalOnly();

    // 图片：**直接复用 lightbox**（已有缩放 / 拖拽 / 适应 / 原始尺寸）
    if (kind === "image") {
      const bridge = window.H3Studio;
      if (bridge && typeof bridge.lightboxShow === "function") {
        bridge.lightboxShow(url, o.name || "图片", 0, 0);
        return true;
      }
      // 兜底：lightbox 还没加载 -> 自己弹一个最简模态框
      return showFallbackImage(url, o.name);
    }

    const modal = ensureModal();
    const body = $("#mediaPreviewBody");
    const title = $("#mediaPreviewTitle");
    const muteBtn = $("#mediaPreviewMute");
    if (!body) return false;

    stopCurrent();
    currentKind = kind;
    body.innerHTML = "";
    const titleText = o.name || (kind === "video" ? "视频预览" : "音频预览");
    if (title) title.textContent = titleText;

    const media = document.createElement(kind === "video" ? "video" : "audio");
    media.className = "media-preview-media";
    media.src = url;
    media.controls = true;
    media.preload = "metadata";
    media.autoplay = false;
    body.appendChild(media);
    currentMedia = media;

    if (muteBtn) {
      muteBtn.hidden = false;
      muteBtn.textContent = "静音";
    }
    modal.hidden = false;
    document.body.classList.add("modal-open");
    return true;
  }

  /** 按素材对象打开（素材库里拿到的 asset）。 */
  function showForAsset(asset) {
    if (!asset) return false;
    const url = asset.content_url
      || (asset.id ? `/api/assets/${encodeURIComponent(asset.id)}/content` : "")
      || asset.preview_url;
    return show({
      url,
      kind: asset.category,
      name: asset.name,
      mime: asset.mime,
    });
  }

  /** lightbox 不可用时的图片兜底（极简，不做缩放）。 */
  function showFallbackImage(url, name) {
    const modal = ensureModal();
    const body = $("#mediaPreviewBody");
    const title = $("#mediaPreviewTitle");
    const muteBtn = $("#mediaPreviewMute");
    stopCurrent();
    currentKind = "image";
    if (title) title.textContent = name || "图片预览";
    if (muteBtn) muteBtn.hidden = true;
    body.innerHTML = "";
    const img = document.createElement("img");
    img.className = "media-preview-image";
    img.src = url;
    img.alt = name || "图片预览";
    body.appendChild(img);
    modal.hidden = false;
    document.body.classList.add("modal-open");
    return true;
  }

  /**
   * 只关掉模态框本体（停播放 + 隐藏），不动滚动锁。
   *
   * 【为什么单独一个函数】切媒体类型时要**先**把上一个的
   * <video>/<audio> 从 DOM 里拿掉 —— 否则「视频预览」切到
   * 「图片预览」（图片走 lightbox）之后，模态框虽然 hidden，
   * 里面那个播放器还挂着，用户下次打开别的媒体时会看到残留。
   */
  function closeModalOnly() {
    const modal = $("#mediaPreviewModal");
    stopCurrent();
    currentKind = "";
    const body = $("#mediaPreviewBody");
    if (body) body.innerHTML = "";
    if (modal) modal.hidden = true;
  }

  /** 关闭模态框。 */
  function close() {
    const modal = $("#mediaPreviewModal");
    stopCurrent();
    currentKind = "";
    if (modal) modal.hidden = true;
    // 只有在没有别的模态框开着时才解锁滚动
    if (typeof window.H3Studio === "object"
        && typeof window.H3Studio.syncModalOpenClass === "function") {
      window.H3Studio.syncModalOpenClass();
    } else {
      document.body.classList.remove("modal-open");
    }
  }

  // ------------------------------------------------------------ 暴露

  window.MediaPreview = {
    show,
    showForAsset,
    close,
    detectKind,
    get currentKind() { return currentKind; },
  };
})();
