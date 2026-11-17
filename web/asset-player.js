/* ==================================================================
 * 素材库「播放器模式」（2026-11-14 需求 14 / A2 修订）
 *
 * 【要解决的】
 * 素材库只能「看一张」，不能「连着播」。用户挑一堆素材（比如一段 A-roll
 * 配乐 + 若干张分镜图）后想顺序过一遍 / 当幻灯片放，现在做不到。
 *
 * 【三种播放模式（按素材类型自动分类）】
 *   图片播放模式：
 *     * 逐张播放，每张停留「每张停止时间」（秒），到点自动翻下一张
 *     * 可同时以「音频播放模式」的方式播放音频（图片 + 背景音）
 *     * 列表内循环
 *   音频播放模式：
 *     * 单曲循环 / 播放列表循环 / 播放列表随机
 *   视频播放模式：
 *     * 与音频播放模式一致（单曲循环 / 列表循环 / 列表随机）
 *
 * 【数据来源：A2 修订 —— 清单来自「添加到播放器」，不再是当前页全部素材】
 *   原实现用 window.ASSETLIB.getItems() 拿**当前页**素材当播放列表，
 *   后果有三个：
 *     1. 播放的内容随筛选/翻页变化，用户不知道自己在播什么；
 *     2. 想要「先挑 5 张再连着播」根本做不到（只能全播）；
 *     3. 关掉重开素材库，列表就没了。
 *   现在播放列表 = 用户在素材库里主动「添加到播放器」的条目，
 *   存在服务端（player_playlists / player_entries 表），
 *   **重启服务后仍在**，顺序 = 添加顺序。
 *
 * 【A2 修订二：图片模式只保留顶部一个「每张停止时间」输入框】
 *   原实现每张图片下面还有一个小输入框（.al-pitem-sec），顶部也有一个。
 *   两个入口改的是同一个值，还经常显示不一致（实测：顶部显示 2，
 *   列表里还是 5）。现在只留顶部那一个，它的语义是
 *   「每张图片停留多少秒」—— 改一次，整份清单同步。
 *
 * 【A2 修订三：音乐集合播放要顺畅】
 *   原实现的三个真 bug：
 *     1. 背景音只取 PLAYLIST.audio[0]，且没有 ended 监听
 *        -> 播完一首就静默停止（用户描述：「音乐集合播放不顺畅」）；
 *     2. S.audioPlaying 从来没被置成 true（只有 stopAudio 会置 false）
 *        -> 勾选框状态与真实播放状态对不上；
 *     3. 随机模式可能抽到当前这首（等于「重播当前」而不是「换一首」）。
 *   现在：
 *     * 媒体元素的 ended 事件统一走 advanceIndex() 推进，
 *       三种模式（单曲 / 列表 / 随机）共用同一套逻辑；
 *     * audioPlaying 由媒体元素自己的 play / pause 事件驱动
 *       —— 标志位**必须**来自真实播放状态，不能自己编；
 *     * 随机模式下一首一定不等于当前这首。
 * ================================================================== */
(function () {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // 播放器的清单：按类型分组的素材条目
  const PLAYLIST = {
    image: [],   // [{id,name,url,seconds}]
    audio: [],   // [{id,name,url}]
    video: [],   // [{id,name,url}]
  };

  // 当前播放状态
  const S = {
    mode: "image",        // image | audio | video
    imgIndex: 0,
    imgSeconds: 5,        // 图片模式「每张停止时间」（秒）—— 清单级，全局唯一
    imgLoop: true,        // 列表内循环
    audioMode: "list",    // single | list | shuffle
    videoMode: "list",
    audioIndex: 0,
    videoIndex: 0,
    audioPlaying: false,  // 图片模式下的背景音是否在播（由 play/pause 事件驱动）
    bgEl: null,           // 背景音元素
    bgIndex: 0,           // 背景音在 PLAYLIST.audio 里的位置
    mediaEl: null,        // 当前 tab 的 <audio> / <video>
    mediaKind: "",        // "audio" | "video"
    playing: false,       // 当前 tab 的媒体是否在播
    timer: null,
    loaded: false,        // 清单是否成功拉到
    loading: false,
    loadError: "",
  };

  // ---------------------------------------------------------------- 纯逻辑
  // （这几个函数不碰 DOM，可以被 Node 直接跑 —— 见 tests/test_a23_player.py）

  /**
   * 自动推进（媒体 ended 事件触发）时的下一首序号。
   *
   * 【为什么必须抽成纯函数】
   *   「播完一首之后播哪一首」是整个播放器的核心语义，
   *   而且极易写错（随机抽到当前这首 / 列表到底不回绕）。
   *   抽成纯函数后可以直接跑断言，不依赖浏览器、不依赖计时器。
   *
   * @param current 当前序号
   * @param length  列表长度
   * @param mode    single | list | shuffle
   * @param randomFn 随机源（测试里注入固定值，保证可复现）
   */
  function advanceIndex(current, length, mode, randomFn) {
    if (!length || length < 1) return 0;
    if (mode === "shuffle") {
      if (length <= 1) return 0;
      const rand = typeof randomFn === "function" ? randomFn() : Math.random();
      let i = Math.floor(rand * length);
      if (!isFinite(i) || i < 0) i = 0;
      if (i >= length) i = length - 1;
      // 【关键】随机模式下一首**不能是当前这首** —— 否则用户听到的是
      // 「又播了一遍」，看起来就像随机模式没生效。
      if (i === current) i = (i + 1) % length;
      return i;
    }
    if (mode === "single") return current;   // 单曲循环由 el.loop=true 负责
    return (current + 1) % length;            // 列表循环：最后一首之后回第一首
  }

  /**
   * 手动「上一首 / 下一首」。
   * 与 advanceIndex 的区别：列表模式下要**按 delta** 走（上一首要往回），
   * 而自动推进永远是「下一首」。
   */
  function stepIndex(current, length, delta, mode, randomFn) {
    if (!length || length < 1) return 0;
    if (mode === "shuffle") return advanceIndex(current, length, mode, randomFn);
    let i = current + delta;
    if (i < 0) i = length - 1;
    if (i >= length) i = 0;
    return i;
  }

  /**
   * 把服务端返回的清单条目分到 image / audio / video 三个播放列表。
   *
   * 【为什么要在这里就把 url 定好】
   *   图片用 preview_url（512 宽缩略图，幻灯片不需要原图，省流量）；
   *   音视频用 content_url（原文件，必须原样播）。
   *   回收站里的素材（deleted=true）跳过 —— 用户的编排不丢，但不放出来播。
   */
  function collect(entries) {
    PLAYLIST.image = [];
    PLAYLIST.audio = [];
    PLAYLIST.video = [];
    (entries || []).forEach(entry => {
      if (!entry || entry.deleted) return;
      const id = entry.asset_id || entry.id;
      if (!id) return;
      const category = entry.category;
      const url = category === "image"
        ? (entry.preview_url || entry.content_url)
        : (entry.content_url || entry.preview_url);
      if (!url) return;
      const item = { id, name: entry.name || id, url, seconds: S.imgSeconds };
      if (category === "image") PLAYLIST.image.push(item);
      else if (category === "audio") PLAYLIST.audio.push(item);
      else if (category === "video") PLAYLIST.video.push(item);
    });
  }

  // ---------------------------------------------------------------- 取数据

  /**
   * 拉播放器清单。
   *
   * 【为什么不复用素材库列表接口】
   *   列表接口返回的是「当前筛选下的一页」，不是「用户挑好的那组」。
   *   播放器的清单必须由服务端单独存一份，才能跨页面、跨重启保留。
   */
  async function loadPlaylist() {
    S.loading = true;
    S.loadError = "";
    try {
      const resp = await fetch("/api/assets/player");
      if (!resp.ok) throw new Error(`读取播放器清单失败（HTTP ${resp.status}）`);
      const data = await resp.json();
      S.imgSeconds = Number(data.img_seconds) || 5;
      collect(data.entries || []);
      S.loaded = true;
    } catch (e) {
      S.loaded = false;
      S.loadError = (e && e.message) ? e.message : String(e);
      PLAYLIST.image = [];
      PLAYLIST.audio = [];
      PLAYLIST.video = [];
    }
    S.loading = false;
    return S.loaded;
  }

  // ---------------------------------------------------------------- 渲染

  function emptyHint(label) {
    if (S.loadError) return `播放器清单读取失败：${S.loadError}`;
    if (S.loading) return "正在读取播放器清单…";
    return `清单里还没有${label || ""}素材 —— 在素材库里勾选后点「添加到播放器」`;
  }

  /**
   * 打开时把当前 tab 落到**清单里真的有内容**的那一类。
   *
   * 【为什么必须有这一步 —— 2026-11-17 E2E 实测】
   * 原来 S.mode 恒为初始值 "image"。用户只往清单里加了一个**视频**，
   * 打开播放器看到的却是「图片播放」页签 + 「清单里还没有图片素材」
   * —— 明明清单里有 1 个视频，界面却说「清单是空的」。用户会以为
   * 「添加到播放器」没生效（真实反馈：素材库加不进播放器）。
   *
   * 规则（与 tab 的禁用态一致）：
   *   1. 当前 tab 有内容 -> 保持不动（用户刚切过就打开，尊重他的选择）；
   *   2. 当前 tab 没内容、但别的 tab 有 -> 切到那个有内容的 tab；
   *   3. 三个都没内容 -> 保持当前 tab（显示空态提示）。
   */
  function pickAvailableMode() {
    const has = (kind) => (
      kind === "image" ? PLAYLIST.image.length
        : kind === "audio" ? PLAYLIST.audio.length
          : PLAYLIST.video.length) > 0;
    if (has(S.mode)) return S.mode;
    if (has("image")) return "image";
    if (has("audio")) return "audio";
    if (has("video")) return "video";
    return S.mode;
  }

  async function open() {
    const overlay = $("#alPlayer");
    if (!overlay) return;
    overlay.hidden = false;
    // 先置 loading 再渲染 —— 否则第一帧会显示「清单是空的」，
    // 用户会以为没添加成功（其实只是还没拉完）。
    S.loading = true;
    render();
    await loadPlaylist();
    // 每次打开都从头开始 —— 上次的播放位置对「清单浏览」没有意义。
    S.imgIndex = 0;
    S.audioIndex = 0;
    S.videoIndex = 0;
    S.bgIndex = 0;
    // 清单拉回来之后，把 tab 落到真有内容的那一类（见 pickAvailableMode）。
    S.mode = pickAvailableMode();
    render();
  }

  function close() {
    const overlay = $("#alPlayer");
    if (overlay) overlay.hidden = true;
    stopTimer();
    stopBackgroundAudio();
    stopMedia();
  }

  function stopTimer() {
    if (S.timer) { clearTimeout(S.timer); S.timer = null; }
  }

  /** 播放器底部那行提示（自动播放被拦、清单为空等）。 */
  function note(text) {
    const box = $("#alPlayerNote");
    if (box) box.textContent = text || "";
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
        stopTimer();
        stopBackgroundAudio();
        stopMedia();
        render();
      });
    });
  }

  function render() {
    renderTabs();
    const body = $("#alPlayerBody");
    if (!body) return;
    if (S.mode === "image") renderImageMode(body);
    else if (S.mode === "audio") renderListMode(body, "audio");
    else renderListMode(body, "video");
  }

  // ------------------------------ 图片播放模式

  function renderImageMode(body) {
    const list = PLAYLIST.image;
    if (!list.length) {
      body.innerHTML = `<div class="al-empty">${esc(emptyHint("图片"))}</div>`;
      return;
    }
    if (S.imgIndex >= list.length) S.imgIndex = 0;
    const cur = list[S.imgIndex];
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
                 value="${esc(String(S.imgSeconds))}" title="每张图片停留多少秒（对整个清单生效）"> 秒
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
          </div>`).join("")}
      </div>`;

    $("#alPPrev").addEventListener("click", () => { stepImage(-1); });
    $("#alPNext").addEventListener("click", () => { stepImage(1); });
    $("#alPPlay").addEventListener("click", () => { toggleImageTimer(); });
    // 【A2】图片模式**只有**这一个停止时间入口。
    // 它的语义是「每张图片停留多少秒」，所以改一次要同步到清单里的每一张
    // （以前顶部框只改当前那张，列表里的小框还是旧值 —— 界面自相矛盾）。
    $("#alPSeconds").addEventListener("change", e => {
      const v = Math.max(1, Math.min(600, Number(e.target.value) || 5));
      S.imgSeconds = v;
      e.target.value = v;
      PLAYLIST.image.forEach(it => { it.seconds = v; });
      persistImgSeconds(v);
      if (S.timer) startImageTimer();
    });
    $("#alPLoop").addEventListener("change", e => { S.imgLoop = e.target.checked; });
    $("#alPAudio").addEventListener("change", e => {
      if (e.target.checked) startBackgroundAudio();
      else stopBackgroundAudio();
      syncAudioCheck();
    });
    body.querySelectorAll(".al-player-item").forEach(el => {
      el.addEventListener("click", event => {
        if (event.target.closest("input")) return;
        S.imgIndex = Number(el.getAttribute("data-idx"));
        stopTimer(); render();
        startImageTimer();
      });
    });
    startImageTimer();
  }

  /** 停止时间是清单级设置 —— 存服务端，重启后仍生效。 */
  function persistImgSeconds(value) {
    try {
      fetch("/api/assets/player/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ img_seconds: value }),
      }).catch(() => { /* 保存失败不打断播放 */ });
    } catch (e) { /* 老浏览器没有 fetch 也无所谓 */ }
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
    // 【为什么必须检查面板是否开着】
    // 素材库那边「添加到播放器」后会调 AssetPlayer.render() 刷新计数 ——
    // 那时播放器面板是**关着的**。如果这里不拦，renderImageMode 结尾的
    // startImageTimer() 会在后台悄悄跑起幻灯片计时器：
    // 面板关着，图片却在后台一张张翻（实测会这样）。
    const overlay = $("#alPlayer");
    if (!overlay || overlay.hidden) return;
    const cur = list[S.imgIndex];
    const seconds = Math.max(1, Math.min(600, Number(cur.seconds) || S.imgSeconds || 5));
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

  // ------------------------------ 图片模式的背景音
  //
  // 【为什么要单独一套，而不是复用音频 tab 的元素】
  //   两个场景是互斥的（看图片时听背景音 vs 专门听音频），
  //   状态混在一起会让「切 tab 时该停谁」变得含糊。
  //   但**推进逻辑必须共用** advanceIndex() —— 否则又会出现
  //   「背景音只播第一首」的老 bug。

  function startBackgroundAudio() {
    const list = PLAYLIST.audio;
    if (!list.length) {
      note("清单里还没有音频素材");
      S.audioPlaying = false;
      syncAudioCheck();
      return;
    }
    stopBackgroundAudio();
    if (S.bgIndex >= list.length) S.bgIndex = 0;
    const el = new Audio(list[S.bgIndex].url);
    S.bgEl = el;
    el.loop = (S.audioMode === "single");
    el.addEventListener("ended", () => {
      // 走与音频播放模式**完全相同**的推进逻辑
      S.bgIndex = advanceIndex(S.bgIndex, list.length, S.audioMode);
      const next = list[S.bgIndex];
      if (!next) return;
      el.setAttribute("src", next.url);
      el.load();
      const p = el.play();
      if (p && p.catch) p.catch(() => { /* 自动播放被拦：用户点一下播放即可 */ });
      note(`背景音：${next.name}`);
    });
    // 【关键】播放状态标志位必须来自媒体元素**真实**的事件，
    // 不能自己维护 —— 否则勾选框会和实际播放对不上（原 bug）。
    el.addEventListener("play", () => { S.audioPlaying = true; syncAudioCheck(); });
    el.addEventListener("pause", () => { S.audioPlaying = false; syncAudioCheck(); });
    const p = el.play();
    if (p && p.catch) {
      p.catch(() => {
        // 浏览器可能因自动播放策略拒绝 —— 如实提示，不静默
        note("浏览器阻止了自动播放，请点一下播放按钮再试。");
      });
    }
    note(`背景音：${list[S.bgIndex].name}`);
  }

  function stopBackgroundAudio() {
    if (S.bgEl) {
      try { S.bgEl.pause(); } catch (e) { /* 已停止 */ }
      try { S.bgEl.removeAttribute("src"); } catch (e) { /* 老浏览器没有 */ }
      S.bgEl = null;
    }
    S.audioPlaying = false;
    syncAudioCheck();
  }

  /** 把勾选框同步到真实的播放状态。 */
  function syncAudioCheck() {
    const box = $("#alPAudio");
    if (box) box.checked = !!S.audioPlaying;
  }

  // ------------------------------ 音频 / 视频播放模式

  function renderListMode(body, kind) {
    const list = kind === "audio" ? PLAYLIST.audio : PLAYLIST.video;
    const modeKey = kind === "audio" ? "audioMode" : "videoMode";
    const indexKey = kind === "audio" ? "audioIndex" : "videoIndex";
    const label = kind === "audio" ? "音频" : "视频";
    if (!list.length) {
      body.innerHTML = `<div class="al-empty">${esc(emptyHint(label))}</div>`;
      stopMedia();
      return;
    }
    if (S[indexKey] >= list.length) S[indexKey] = 0;
    const cur = list[S[indexKey]];
    // 【关键】先把媒体节点**摘下来**再重排 HTML。
    //
    // 【为什么必须先摘】
    //   body.innerHTML = ... 会把旧节点从文档里移除（isConnected 变 false），
    //   之后再想复用它就晚了 —— 元素虽然还在内存里，但已经被丢弃。
    //   先 appendChild 到一个游离容器里暂存，重排完再放回插槽，
    //   正在播的音频就**不会被打断**。
    //
    // 【为什么要复用同一个节点】
    //   原来每次切歌都重建 <audio>：播放中断、进度归零、控件状态丢失。
    //   这正是「播放不顺畅」的一部分。
    let mediaEl = null;
    if (S.mediaEl && S.mediaKind === kind) {
      mediaEl = S.mediaEl;
      if (mediaEl.parentNode) mediaEl.parentNode.removeChild(mediaEl);
    }
    body.innerHTML = `
      <div class="al-player-stage">
        <div class="al-player-media-slot" id="alPMediaSlot"></div>
        <div class="al-player-caption">
          <b id="alPlayerCapName">${esc(cur.name)}</b>
          <span id="alPlayerCapIdx">${S[indexKey] + 1} / ${list.length}</span>
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

    if (!mediaEl) {
      mediaEl = document.createElement(kind === "audio" ? "audio" : "video");
      mediaEl.className = "al-player-media";
      mediaEl.controls = true;
      mediaEl.preload = "metadata";
      mediaEl.id = "alPMedia";
    }
    const slot = $("#alPMediaSlot");
    if (slot) slot.appendChild(mediaEl);
    S.mediaEl = mediaEl;
    S.mediaKind = kind;

    $("#alPPrev").addEventListener("click", () => { stepList(kind, -1); });
    $("#alPNext").addEventListener("click", () => { stepList(kind, 1); });
    $("#alPMode").addEventListener("change", e => {
      S[modeKey] = e.target.value;
      // 单曲循环 = 交给媒体元素自己 loop；列表 / 随机 = 由 ended 事件推进
      mediaEl.loop = (e.target.value === "single");
    });
    body.querySelectorAll(".al-player-item").forEach(el => {
      el.addEventListener("click", () => {
        S[indexKey] = Number(el.getAttribute("data-idx"));
        playCurrentTrack(kind, true);
      });
    });
    if (mediaEl.getAttribute("src") !== cur.url) {
      mediaEl.setAttribute("src", cur.url);
      if (typeof mediaEl.load === "function") mediaEl.load();
    }
    mediaEl.loop = (S[modeKey] === "single");
    // 【A2】播完自动推进 —— 列表循环回第一首 / 随机挑下一首（不重复当前）。
    // 用标记避免重复绑 ended（同一个节点被复用时）。
    if (!mediaEl.dataset.a23Bound) {
      mediaEl.dataset.a23Bound = "1";
      mediaEl.addEventListener("ended", () => { onMediaEnded(kind); });
      mediaEl.addEventListener("play", () => { S.playing = true; });
      mediaEl.addEventListener("pause", () => { S.playing = false; });
    }
  }

  /**
   * 媒体播完 -> 推进到下一首。
   *
   * 【为什么必须在这里统一处理】
   *   少了这个监听，列表/随机模式播完一首就静默停止 ——
   *   用户看到的现象就是「音乐集合播放不顺畅」。
   */
  function onMediaEnded(kind) {
    const list = kind === "audio" ? PLAYLIST.audio : PLAYLIST.video;
    const modeKey = kind === "audio" ? "audioMode" : "videoMode";
    const indexKey = kind === "audio" ? "audioIndex" : "videoIndex";
    if (!list.length) return;
    if (S[modeKey] === "single") {
      // 单曲循环正常情况由 el.loop=true 自动重播（此时不会派发 ended）。
      // 这里只是兜底 —— 某些浏览器仍会派发 ended。
      const el = $("#alPMedia");
      if (el) {
        const p = el.play();
        if (p && p.catch) p.catch(() => { /* 被拦就等用户手动点 */ });
      }
      return;
    }
    S[indexKey] = advanceIndex(S[indexKey], list.length, S[modeKey]);
    playCurrentTrack(kind, true);
  }

  /**
   * 切到清单里的第 N 首并（可选）开始播放。
   *
   * 【为什么不整块重渲染】
   *   原来每次切歌都 body.innerHTML = ...，把 <audio> 元素整个换掉：
   *   控件状态丢失、正在播的媒体被打断、焦点丢失。
   *   现在只换 src + 局部更新标题和列表高亮 —— 播放器才「顺畅」。
   */
  function playCurrentTrack(kind, autoplay) {
    const list = kind === "audio" ? PLAYLIST.audio : PLAYLIST.video;
    const indexKey = kind === "audio" ? "audioIndex" : "videoIndex";
    if (!list.length) return;
    const cur = list[S[indexKey]];
    // 用 S.mediaEl 而不是重新查 DOM —— 重渲染后老节点被摘走过，
    // querySelector("#alPMedia") 仍能找到它（放回槽里了），
    // 但 S.mediaEl 更直接，也不会误抓到别的节点的同名 id。
    const mediaEl = S.mediaEl || $("#alPMedia");
    if (mediaEl) {
      if (mediaEl.getAttribute("src") !== cur.url) {
        mediaEl.setAttribute("src", cur.url);
        // 换源后必须 load —— 否则部分浏览器不会真的去拉新资源。
        if (typeof mediaEl.load === "function") mediaEl.load();
      }
      if (autoplay) {
        const p = mediaEl.play();
        if (p && p.catch) p.catch(() => {
          note("浏览器阻止了自动播放，请点一下播放按钮。");
        });
      }
    }
    updateTrackUi(kind);
  }

  /** 局部更新标题 / 序号 / 列表高亮（不重建 DOM）。 */
  function updateTrackUi(kind) {
    const list = kind === "audio" ? PLAYLIST.audio : PLAYLIST.video;
    const indexKey = kind === "audio" ? "audioIndex" : "videoIndex";
    if (!list.length) return;
    const cur = list[S[indexKey]];
    const nameEl = $("#alPlayerCapName");
    const idxEl = $("#alPlayerCapIdx");
    if (nameEl) nameEl.textContent = cur.name;
    if (idxEl) idxEl.textContent = `${S[indexKey] + 1} / ${list.length}`;
    const host = $("#alPList");
    if (host) {
      host.querySelectorAll(".al-player-item").forEach(el => {
        el.classList.toggle("on", Number(el.getAttribute("data-idx")) === S[indexKey]);
      });
    }
  }

  function stepList(kind, delta) {
    const list = kind === "audio" ? PLAYLIST.audio : PLAYLIST.video;
    const indexKey = kind === "audio" ? "audioIndex" : "videoIndex";
    const modeKey = kind === "audio" ? "audioMode" : "videoMode";
    if (!list.length) return;
    S[indexKey] = stepIndex(S[indexKey], list.length, delta, S[modeKey]);
    playCurrentTrack(kind, true);
  }

  /** 停掉当前 tab 的媒体元素（切 tab / 关面板时调用）。 */
  function stopMedia() {
    if (S.mediaEl) {
      try { S.mediaEl.pause(); } catch (e) { /* 已停止 */ }
      S.mediaEl = null;
      S.mediaKind = "";
    }
  }

  // ---------------------------------------------------------------- 启动

  function bind() {
    const overlay = $("#alPlayer");
    if (!overlay) return;
    $("#alPlayerClose").addEventListener("click", close);
    // 【A2】清空清单：清的是**服务端**那份（重启后依然为空）。
    const clearBtn = $("#alPlayerClear");
    if (clearBtn) {
      clearBtn.addEventListener("click", async () => {
        if (!window.confirm("清空播放器清单？\n\n（清单里所有素材都会被移出播放器，素材本身不受影响）")) {
          return;
        }
        try {
          const resp = await fetch("/api/assets/player/clear", { method: "POST" });
          if (!resp.ok) throw new Error(`清空失败（HTTP ${resp.status}）`);
          await loadPlaylist();
          S.imgIndex = 0;
          S.audioIndex = 0;
          S.videoIndex = 0;
          S.bgIndex = 0;
          render();
        } catch (e) {
          note((e && e.message) ? e.message : "清空失败");
        }
      });
    }
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
    // 纯逻辑导出（给测试用，也方便别处复用）
    advanceIndex,
    stepIndex,
    collect,
    loadPlaylist,
  };
})();
