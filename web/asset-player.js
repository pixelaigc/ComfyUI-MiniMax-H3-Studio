/* ==================================================================
 * 素材库「播放器模式」（2026-11-14 需求 14）
 *
 * 【要解决的】
 * 素材库只能「看一张」，不能「连着播」。用户挑一堆素材（比如一段 A-roll
 * 配乐 + 若干张分镜图）后想顺序过一遍 / 当幻灯片放，现在做不到。
 *
 * 【三种播放模式（按素材类型自动分类）】
 *   图片播放模式：
 *     * 逐张播放，每张可配「停止时间」（秒），到点自动翻下一张
 *     * 可同时以「音频播放模式」的方式播放音频（图片 + 背景音）
 *     * 列表内循环播放
 *   音频播放模式：
 *     * 单曲循环 / 播放列表循环 / 播放列表随机
 *   视频播放模式：
 *     * 与音频播放模式一致（单曲循环 / 列表循环 / 列表随机）
 *
 * 【数据来源】
 * 素材库现有清单（/api/assets）—— 复用同一份数据，不另造。
 * 音频用 content_url（原始文件），图片用 preview_url（512 宽缩略图，
 * 幻灯片播放不需要原图，省流量）。
 *
 * 【为什么不用 <video> 播图片】
 * 图片幻灯片用 <img> + setTimeout 推进，比把图片塞进 video 更可控
 * （停止时间要精确到秒，且要能暂停/续播）。
 * ================================================================== */
(function () {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // 播放器的清单：按类型分组的素材 id 列表
  const PLAYLIST = {
    image: [],   // [{id,name,url,seconds}]
    audio: [],   // [{id,name,url}]
    video: [],   // [{id,name,url}]
  };

  // 当前播放状态
  const S = {
    mode: "image",        // image | audio | video
    imgIndex: 0,
    imgSeconds: 5,        // 每张图片的默认停止时间（秒）
    imgLoop: true,        // 列表内循环
    audioMode: "list",    // single | list | shuffle
    videoMode: "list",
    audioIndex: 0,
    videoIndex: 0,
    audioPlaying: false,  // 图片模式下的背景音
    audioEl: null,
    timer: null,
    loaded: false,
  };

  // ---------------------------------------------------------------- 取数据

  /** 从素材库清单里按类型挑素材。 */
  function collect(items) {
    PLAYLIST.image = [];
    PLAYLIST.audio = [];
    PLAYLIST.video = [];
    (items || []).forEach(a => {
      if (!a || a.deleted_at) return;
      const url = a.content_url || a.preview_url || "";
      if (!url) return;
      if (a.category === "image") {
        PLAYLIST.image.push({
          id: a.id, name: a.name || a.id,
          url: a.preview_url || url, seconds: S.imgSeconds,
        });
      } else if (a.category === "audio") {
        PLAYLIST.audio.push({ id: a.id, name: a.name || a.id, url });
      } else if (a.category === "video") {
        PLAYLIST.video.push({ id: a.id, name: a.name || a.id, url });
      }
    });
  }

  // ---------------------------------------------------------------- 渲染

  function open() {
    const overlay = $("#alPlayer");
    if (!overlay) return;
    const items = (window.ASSETLIB && window.ASSETLIB.getItems)
      ? window.ASSETLIB.getItems() : [];
    collect(items);
    overlay.hidden = false;
    render();
  }

  function close() {
    const overlay = $("#alPlayer");
    if (overlay) overlay.hidden = true;
    stopTimer();
    stopAudio();
  }

  function stopTimer() {
    if (S.timer) { clearTimeout(S.timer); S.timer = null; }
  }

  function stopAudio() {
    if (S.audioEl) {
      try { S.audioEl.pause(); } catch (e) { /* 已停止 */ }
      S.audioEl = null;
    }
    S.audioPlaying = false;
  }

  /** 分类清单（三个 tab + 每个 tab 的条目数）。 */
  function renderTabs() {
    const host = $("#alPlayerTabs");
    if (!host) return;
    const tabs = [
      ["image", "图片播放", PLAYLIST.image.length],
      ["audio", "音频播放", PLAYLIST.audio.length],
      ["video", "视频播放", PLAYLIST.video.length],
    ];
    host.innerHTML = tabs.map(([key, label, n]) => `
      <button type="button" class="al-ptab${S.mode === key ? " on" : ""}"
              data-mode="${key}"${n === 0 ? " disabled" : ""}>
        ${esc(label)} <b>${n}</b>
      </button>`).join("");
    host.querySelectorAll(".al-ptab").forEach(btn => {
      btn.addEventListener("click", () => {
        if (btn.disabled) return;
        S.mode = btn.getAttribute("data-mode");
        stopTimer(); stopAudio();
        render();
      });
    });
  }

  function render() {
    renderTabs();
    const body = $("#alPlayerBody");
    if (!body) return;
    if (S.mode === "image") renderImageMode(body);
    else if (S.mode === "audio") renderAudioMode(body);
    else renderVideoMode(body);
  }

  // ------------------------------ 图片播放模式

  function renderImageMode(body) {
    const list = PLAYLIST.image;
    if (!list.length) {
      body.innerHTML = `<div class="al-empty">素材库里还没有图片素材</div>`;
      return;
    }
    if (S.imgIndex >= list.length) S.imgIndex = 0;
    const cur = list[S.imgIndex];
    const audioList = PLAYLIST.audio;
    body.innerHTML = `
      <div class="al-player-stage">
        <img class="al-player-img" id="alPlayerImg" src="${esc(cur.url)}"
             alt="${esc(cur.name)}">
        <div class="al-player-caption">
          <b id="alPlayerCapName">${esc(cur.name)}</b>
          <span id="alPlayerCapIdx">${S.imgIndex + 1} / ${list.length}</span>
        </div>
      </div>
      <div class="al-player-controls">
        <button type="button" class="al-btn" id="alPPrev">◀ 上一张</button>
        <button type="button" class="al-btn primary" id="alPPlay">⏸ 暂停</button>
        <button type="button" class="al-btn" id="alPNext">下一张 ▶</button>
        <label class="al-player-field">
          <span>每张停止时间</span>
          <input type="number" id="alPSeconds" min="1" max="600" step="1"
                 value="${esc(cur.seconds)}"> 秒
        </label>
        <label class="al-player-check">
          <input type="checkbox" id="alPLoop"${S.imgLoop ? " checked" : ""}> 列表内循环
        </label>
        <label class="al-player-check">
          <input type="checkbox" id="alPAudio"${S.audioPlaying ? " checked" : ""}>
          同时播放音频（音频播放模式）
        </label>
      </div>
      <div class="al-player-list" id="alPList">
        ${list.map((it, i) => `
          <div class="al-player-item${i === S.imgIndex ? " on" : ""}" data-idx="${i}">
            <img src="${esc(it.url)}" alt="">
            <span title="${esc(it.name)}">${esc(it.name)}</span>
            <input type="number" class="al-pitem-sec" data-idx="${i}"
                   min="1" max="600" step="1" value="${esc(it.seconds)}"
                   title="这张显示多少秒">
          </div>`).join("")}
      </div>`;

    $("#alPPrev").addEventListener("click", () => { stepImage(-1); });
    $("#alPNext").addEventListener("click", () => { stepImage(1); });
    $("#alPPlay").addEventListener("click", () => { toggleImageTimer(); });
    $("#alPSeconds").addEventListener("change", e => {
      const v = Math.max(1, Math.min(600, Number(e.target.value) || 5));
      cur.seconds = v;
      e.target.value = v;
      // 【2026-11-14】改「每张停止时间」要**同步到列表里每一项** ——
      // 否则列表上的小输入框还显示旧值，而实际播放用的是新值，
      // 界面上就自相矛盾（实测：输入框显示 2，列表里还是 5）。
      // 语义：改顶部的框 = 改**当前这张**的停止时间。
      const idx = S.imgIndex;
      body.querySelectorAll(`.al-pitem-sec[data-idx="${idx}"]`).forEach(inp => {
        inp.value = v;
      });
      if (S.timer) startImageTimer();
    });
    $("#alPLoop").addEventListener("change", e => { S.imgLoop = e.target.checked; });
    $("#alPAudio").addEventListener("change", e => {
      if (e.target.checked) startBackgroundAudio();
      else stopAudio();
      render();
    });
    body.querySelectorAll(".al-player-item").forEach(el => {
      el.addEventListener("click", event => {
        if (event.target.closest("input")) return;
        S.imgIndex = Number(el.getAttribute("data-idx"));
        stopTimer(); render();
        startImageTimer();
      });
    });
    body.querySelectorAll(".al-pitem-sec").forEach(inp => {
      inp.addEventListener("click", event => event.stopPropagation());
      inp.addEventListener("change", e => {
        const i = Number(e.target.getAttribute("data-idx"));
        const v = Math.max(1, Math.min(600, Number(e.target.value) || 5));
        if (PLAYLIST.image[i]) PLAYLIST.image[i].seconds = v;
        e.target.value = v;
        // 改的是**当前这张**时，顶部的框也要跟着变（两个入口显示同一个值）
        if (i === S.imgIndex) {
          const top = $("#alPSeconds");
          if (top) top.value = v;
          if (S.timer) startImageTimer();
        }
      });
    });
    startImageTimer();
  }

  function stepImage(delta) {
    const list = PLAYLIST.image;
    if (!list.length) return;
    let i = S.imgIndex + delta;
    if (i < 0) i = S.imgLoop ? list.length - 1 : 0;
    if (i >= list.length) i = S.imgLoop ? 0 : list.length - 1;
    S.imgIndex = i;
    render();
  }

  function startImageTimer() {
    stopTimer();
    const list = PLAYLIST.image;
    if (!list.length) return;
    const cur = list[S.imgIndex];
    const seconds = Math.max(1, Math.min(600, Number(cur.seconds) || 5));
    S.timer = setTimeout(() => {
      if (S.imgIndex + 1 >= list.length && !S.imgLoop) {
        stopTimer();
        return;
      }
      stepImage(1);
    }, seconds * 1000);
  }

  function toggleImageTimer() {
    if (S.timer) { stopTimer(); }
    else { startImageTimer(); }
    const btn = $("#alPPlay");
    if (btn) btn.textContent = S.timer ? "⏸ 暂停" : "▶ 继续";
  }

  /** 图片模式下同时播放音频（走音频播放模式的列表循环语义）。 */
  function startBackgroundAudio() {
    const list = PLAYLIST.audio;
    if (!list.length) return;
    stopAudio();
    const el = new Audio(list[0].url);
    el.loop = S.audioMode === "single";
    S.audioEl = el;
    el.play().catch(() => {
      // 浏览器可能因自动播放策略拒绝 —— 如实提示，不静默
      const note = $("#alPlayerNote");
      if (note) note.textContent = "浏览器阻止了自动播放，请点一下播放按钮再试。";
    });
  }

  // ------------------------------ 音频 / 视频播放模式

  function renderListMode(body, kind) {
    const list = kind === "audio" ? PLAYLIST.audio : PLAYLIST.video;
    const modeKey = kind === "audio" ? "audioMode" : "videoMode";
    const indexKey = kind === "audio" ? "audioIndex" : "videoIndex";
    const label = kind === "audio" ? "音频" : "视频";
    if (!list.length) {
      body.innerHTML = `<div class="al-empty">素材库里还没有${label}素材</div>`;
      return;
    }
    if (S[indexKey] >= list.length) S[indexKey] = 0;
    const cur = list[S[indexKey]];
    const media = kind === "audio"
      ? `<audio class="al-player-media" id="alPMedia" src="${esc(cur.url)}" controls preload="metadata"></audio>`
      : `<video class="al-player-media" id="alPMedia" src="${esc(cur.url)}" controls preload="metadata"></video>`;
    body.innerHTML = `
      <div class="al-player-stage">
        ${media}
        <div class="al-player-caption">
          <b>${esc(cur.name)}</b>
          <span>${S[indexKey] + 1} / ${list.length}</span>
        </div>
      </div>
      <div class="al-player-controls">
        <button type="button" class="al-btn" id="alPPrev">◀ 上一${label === "音频" ? "首" : "段"}</button>
        <button type="button" class="al-btn" id="alPNext">下一${label === "音频" ? "首" : "段"} ▶</button>
        <label class="al-player-field">
          <span>播放模式</span>
          <select id="alPMode">
            <option value="single"${S[modeKey] === "single" ? " selected" : ""}>单曲循环</option>
            <option value="list"${S[modeKey] === "list" ? " selected" : ""}>播放列表循环</option>
            <option value="shuffle"${S[modeKey] === "shuffle" ? " selected" : ""}>播放列表随机</option>
          </select>
        </label>
      </div>
      <div class="al-player-list" id="alPList">
        ${list.map((it, i) => `
          <div class="al-player-item${i === S[indexKey] ? " on" : ""}" data-idx="${i}">
            <span class="al-player-item-name" title="${esc(it.name)}">${esc(it.name)}</span>
          </div>`).join("")}
      </div>`;

    const mediaEl = $("#alPMedia");
    $("#alPPrev").addEventListener("click", () => { stepList(kind, -1); });
    $("#alPNext").addEventListener("click", () => { stepList(kind, 1); });
    $("#alPMode").addEventListener("change", e => {
      S[modeKey] = e.target.value;
      if (mediaEl) mediaEl.loop = (e.target.value === "single");
    });
    body.querySelectorAll(".al-player-item").forEach(el => {
      el.addEventListener("click", () => {
        S[indexKey] = Number(el.getAttribute("data-idx"));
        render();
      });
    });
    if (mediaEl) {
      mediaEl.loop = (S[modeKey] === "single");
      // 播完自动推进（列表循环 / 随机）
      mediaEl.addEventListener("ended", () => {
        if (S[modeKey] === "single") { mediaEl.play(); return; }
        stepList(kind, 1);
      });
    }
  }

  function stepList(kind, delta) {
    const list = kind === "audio" ? PLAYLIST.audio : PLAYLIST.video;
    const indexKey = kind === "audio" ? "audioIndex" : "videoIndex";
    const modeKey = kind === "audio" ? "audioMode" : "videoMode";
    if (!list.length) return;
    let i;
    if (S[modeKey] === "shuffle") {
      i = Math.floor(Math.random() * list.length);
    } else {
      i = S[indexKey] + delta;
      if (i < 0) i = list.length - 1;
      if (i >= list.length) i = 0;
    }
    S[indexKey] = i;
    render();
    const media = $("#alPMedia");
    if (media) media.play().catch(() => { /* 自动播放被拦：用户再点一次即可 */ });
  }

  function renderAudioMode(body) { renderListMode(body, "audio"); }
  function renderVideoMode(body) { renderListMode(body, "video"); }

  // ---------------------------------------------------------------- 启动

  function bind() {
    const overlay = $("#alPlayer");
    if (!overlay) return;
    $("#alPlayerClose").addEventListener("click", close);
    overlay.addEventListener("click", event => {
      if (event.target === overlay) close();
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && overlay && !overlay.hidden) close();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }

  window.AssetPlayer = {
    open,
    close,
    render,
    state: S,
    playlist: PLAYLIST,
  };
})();
