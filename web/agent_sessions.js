// 内置 Agent —— 会话管理（持久化 / 列表 / 新建 / 删除 / 产物观测）
//
// 职责边界：
//   * 只管「会话的存取与展示」—— 消息恢复、会话列表、产物面板。
//   * 不碰消息的**产生**逻辑（那是 agent.js 的 send 链路）。
//   * 复用现有 API 风格（fetch + JSON），不另造一套传输层。
//
// 【为什么单独一个文件而不是塞进 agent.js】
// agent.js 已经 1800 行，且承载着「发消息 + 流式渲染」的核心链路。
// 会话管理是**旁路功能**（不进消息区、不参与增量渲染），
// 独立成文件后：
//   1) 出错时不会把 agent.js 的启动异常带崩（历史上 app.js 的 esc() 未定义
//      就曾导致整页脚本中断）；
//   2) 可以单独排错、单独加载。
//
// 【为什么必须有这一层 —— 2026-11-11 实测】
// 刷新页面后交互历史全丢。排查发现：
//   * 后端其实**一直在落库**（h3studio/agent_sessions.py 是 SQLite 持久化，
//     server.py 的 api_agent_chat 每次都 add_message）；
//   * 前端只把 session_id 当成一个**内存里的字符串**（state.sessionId），
//     刷新后既不恢复会话、也不拉列表、没有新建/删除入口。
// 所以根因在前端「没有接会话库」，不是「没落库」。
// （另一个坑：落库的信息也不够 —— 工具调用只存了 "create_job: ok" 这种
//  字符串，恢复不出任务卡。现在 meta 列存了结构化信息。）
(function () {
  const $ = s => document.querySelector(s);

  const state = {
    // 会话清单（服务端返回）
    sessions: [],
    // 当前会话 id
    currentId: "",
    // 当前会话的消息（已转成 agent.js 的消息结构）
    messages: [],
    // 产物面板是否展开
    artifactsOpen: false,
    // 抽屉是否展开
    drawerOpen: false,
    // 当前会话的任务
    artifacts: [],
  };

  // 当前会话 id 的持久化键。
  //
  // 【为什么用 localStorage 而不是只靠 URL】
  // 用户刷新页面是「回到刚才那个会话」的典型场景 —— URL 带参数也能做到，
  // 但 localStorage 对本项目更合适：Agent 页是个独立 tab 式页面，
  // 不该往 URL 里塞状态（URL 会被分享、被收藏）。
  const STORAGE_KEY = "h3studio.agent.currentSession";

  // ---------- 工具 ----------

  function esc(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function apiJSON(method, path, body) {
    const opts = { method, headers: { "Content-Type": "application/json" } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    return fetch(path, opts).then(async resp => {
      const raw = await resp.text();
      let data = {};
      try { data = raw ? JSON.parse(raw) : {}; }
      catch (e) { data = { error: raw || "服务端返回了非 JSON 响应" }; }
      if (!resp.ok) {
        const err = new Error(data.error || ("请求失败（HTTP " + resp.status + "）"));
        err.code = data.code || "";
        err.payload = data;
        throw err;
      }
      return data;
    });
  }

  // 时间格式化：列表里显示「刚刚 / 5 分钟前 / 昨天 / 3-12」。
  //
  // 【为什么要自己格式化而不是用 toLocaleString】
  // toLocaleString 依赖浏览器 locale，中文环境下可能输出
  // 「2026/11/11 17:30:00」这种又长又占地方的东西。
  // 侧栏空间窄，要的是「一眼能认出来」的短格式。
  function formatTime(ts) {
    if (!ts) return "";
    const t = Number(ts) * 1000;
    if (!isFinite(t) || t <= 0) return "";
    const now = Date.now();
    const diff = now - t;
    if (diff < 0) return "";
    const min = Math.floor(diff / 60000);
    if (min < 1) return "刚刚";
    if (min < 60) return min + " 分钟前";
    const hour = Math.floor(min / 60);
    if (hour < 24) return hour + " 小时前";
    const day = Math.floor(hour / 24);
    if (day === 1) return "昨天";
    if (day < 7) return day + " 天前";
    const d = new Date(t);
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    return mm + "-" + dd;
  }

  function saveCurrentId(id) {
    state.currentId = id || "";
    try {
      if (id) localStorage.setItem(STORAGE_KEY, id);
      else localStorage.removeItem(STORAGE_KEY);
    } catch (e) {
      // localStorage 可能被禁用（隐私模式）—— 不能因此让整个页面挂掉。
    }
  }

  function readCurrentId() {
    try { return localStorage.getItem(STORAGE_KEY) || ""; }
    catch (e) { return ""; }
  }

  // ---------- 会话列表 ----------

  async function loadSessions() {
    try {
      const data = await apiJSON("GET", "/api/agent/sessions?limit=200");
      state.sessions = Array.isArray(data.sessions) ? data.sessions : [];
    } catch (err) {
      // 【不要在这里把已有会话清空】
      // 刷新页面时，第一次 loadSessions 可能还在飞，
      // 而 loadSession 已经恢复了消息 —— 此时一个**后续**的
      // loadSessions 失败（网络抖动）会把 state.sessions 清成 [],
      // 连带 renderSessionList 显示「还没有交互记录」。
      // 保留旧数据 + 提示错误，比清空更接近用户的真实状态。
      setStatus("会话列表加载失败：" + (err.message || "未知错误"));
    }
    renderSessionList();
    return state.sessions;
  }

  function renderSessionList() {
    const box = $("#agSessionList");
    if (!box) return;
    if (!state.sessions.length) {
      box.innerHTML = `<div class="ag-session-empty">还没有交互记录。<br>发一条消息就会自动建一个。</div>`;
      return;
    }
    const items = state.sessions.map(s => {
      const active = s.id === state.currentId ? " ag-active" : "";
      const title = esc(s.title || "新会话");
      const preview = s.preview ? esc(s.preview) : "";
      const time = formatTime(s.updated_at);
      const count = Number(s.message_count || 0);
      return `<div class="ag-session-item${active}" data-session-id="${esc(s.id)}"
                   role="button" tabindex="0" title="${title}">
        <div class="ag-session-item-title">${title}</div>
        ${preview ? `<div class="ag-session-item-preview">${preview}</div>` : ""}
        <div class="ag-session-item-meta">${esc(time)} · ${count} 条</div>
        <button type="button" class="ag-session-del" data-act="delete-session"
                data-session-id="${esc(s.id)}" title="删除这个交互">&times;</button>
      </div>`;
    }).join("");
    box.innerHTML = items;
  }

  function setStatus(text) {
    const el = $("#agStatus");
    if (el) el.textContent = text || "";
  }

  // ---------- 会话详情 / 恢复 ----------

  // 把服务端返回的消息结构转成 agent.js 内部的消息结构。
  //
  // 【关键：结构必须和 agent.js 渲染层期望的一致】
  // agent.js 的 bubbleHtml 读的是 msg.result.status / msg.jobs / msg.error 等，
  // 所以这里把 meta 里的结构化信息**还原**成这些字段。
  function messagesFromServer(rawMessages) {
    const out = [];
    for (const m of rawMessages || []) {
      const meta = m.meta || {};
      const msg = { role: m.role, content: m.content || "" };
      if (m.role === "user") {
        if (Array.isArray(meta.attachments) && meta.attachments.length) {
          msg.attachments = meta.attachments;
        }
        out.push(msg);
        continue;
      }
      if (m.role === "tool") {
        // 工具消息恢复成工具轨迹里的一条（名字 + 结果摘要）。
        const note = (m.content || "").split(":").slice(1).join(":").trim();
        out.push({
          role: "assistant",
          content: "",
          tools: [{
            name: m.tool_name || "(工具)",
            ok: note !== "error" && note !== "",
            ms: null,
            arguments: null,
            result: note || "ok",
          }],
        });
        continue;
      }
      // assistant：还原状态徽章 / 任务卡 / 附件 / 错误
      const result = {};
      if (meta.status) result.status = meta.status;
      if (meta.error) result.error = meta.error;
      if (meta.notice) result.notice = meta.notice;
      msg.result = result;
      if (meta.jobs && meta.jobs.length) {
        msg.jobs = meta.jobs.map(j => Object.assign({}, j, {
          job_id: j.job_id || "",
          mode: j.mode || "",
          status: j.status || "queued",
          outputs: j.outputs || [],
          params: j.params || {},
          error: j.error || "",
        }));
      }
      if (meta.attachments && meta.attachments.length) {
        msg.attachments = meta.attachments;
      }
      if (meta.error) msg.error = meta.error;
      out.push(msg);
    }
    return out;
  }

  async function loadSession(sessionId) {
    if (!sessionId) {
      state.messages = [];
      state.artifacts = [];
      renderCurrentSessionLabel();
      return [];
    }
    try {
      const data = await apiJSON("GET", "/api/agent/sessions/" + encodeURIComponent(sessionId));
      state.messages = messagesFromServer(data.messages);
      state.artifacts = Array.isArray(data.jobs) ? data.jobs : [];
      saveCurrentId(sessionId);
      renderCurrentSessionLabel();
      renderArtifacts();
      return state.messages;
    } catch (err) {
      if (err.code === "not_found") {
        // 会话被删了（另一标签页删的）-> 清掉本地记录，退回新会话。
        saveCurrentId("");
        state.messages = [];
        state.artifacts = [];
        renderCurrentSessionLabel();
        renderArtifacts();
        return [];
      }
      setStatus("会话加载失败：" + (err.message || "未知错误"));
      return [];
    }
  }

  function renderCurrentSessionLabel() {
    const el = $("#agCurrentSession");
    if (!el) return;
    if (!state.currentId) {
      el.textContent = "";
      return;
    }
    const item = state.sessions.find(s => s.id === state.currentId);
    const title = item ? (item.title || "新会话") : "当前会话";
    const count = item ? Number(item.message_count || 0) : state.messages.length;
    el.textContent = "当前：" + title + "（" + count + " 条）";
  }

  // ---------- 新建 / 删除 ----------

  async function createSession() {
    try {
      const data = await apiJSON("POST", "/api/agent/sessions", {});
      const sid = data.session_id || "";
      if (!sid) throw new Error("服务端没有返回会话 id");
      saveCurrentId(sid);
      state.messages = [];
      state.artifacts = [];
      await loadSessions();
      renderCurrentSessionLabel();
      renderArtifacts();
      setStatus("已新建交互。");
      return sid;
    } catch (err) {
      setStatus("新建交互失败：" + (err.message || "未知错误"));
      return "";
    }
  }

  async function deleteSession(sessionId) {
    if (!sessionId) return false;
    try {
      await apiJSON("DELETE", "/api/agent/sessions/" + encodeURIComponent(sessionId));
      await loadSessions();
      if (state.currentId === sessionId) {
        saveCurrentId("");
        state.messages = [];
        state.artifacts = [];
        renderCurrentSessionLabel();
        renderArtifacts();
      }
      setStatus("已删除交互。");
      return true;
    } catch (err) {
      setStatus("删除交互失败：" + (err.message || "未知错误"));
      return false;
    }
  }

  // ---------- 产物观测 ----------

  // 产物链接：与 agent.js 的 outputUrl 同一套规则（job_id/相对路径 -> /files/...）
  //
  // 【为什么不能另写一套】
  // 两套规则必然漂移 —— 一套把视频当图片渲染、另一套按图片渲染，
  // 用户就会看到「打不开的缩略图」。这里与 agent.js 保持完全一致。
  function outputUrl(jobId, output) {
    const relative = String((output && output.file) || "").replace(/\\/g, "/");
    if (!relative) return "";
    const path = relative.includes("/") ? relative : jobId + "/" + relative;
    return "/files/" + path.split("/").map(encodeURIComponent).join("/");
  }

  function renderArtifacts() {
    const box = $("#agArtifactsBody");
    if (!box) return;
    if (!state.currentId) {
      box.innerHTML = `<div class="ag-artifacts-empty">还没有选择会话。</div>`;
      return;
    }
    if (!state.artifacts.length) {
      box.innerHTML = `<div class="ag-artifacts-empty">本次会话还没有产生任务。</div>`;
      return;
    }
    const items = state.artifacts.map(job => {
      const outputs = Array.isArray(job.outputs) ? job.outputs : [];
      const first = outputs[0];
      const url = first ? outputUrl(job.id, first) : "";
      const kind = first ? String(first.kind || "") : "";
      const isVideo = kind === "video" || kind === "audio";
      const thumb = url
        ? (isVideo
            ? `<video class="ag-artifact-thumb is-video" src="${esc(url)}" muted preload="metadata"></video>`
            : `<img class="ag-artifact-thumb" src="${esc(url)}" alt="${esc(job.id)}" loading="lazy">`)
        : `<div class="ag-artifact-thumb"></div>`;
      const prompt = job.params && job.params.prompt ? String(job.params.prompt) : "";
      const status = job.status || "queued";
      const label = job.gen_type_label || job.mode || "任务";
      return `<div class="ag-artifact-item">
        <div class="ag-artifact-head">
          <span class="ag-artifact-id">${esc(job.id)}</span>
          <span class="ag-job-badge ag-badge-${status === "done" ? "ok" : (status === "failed" ? "bad" : "warn")}">${esc(status)}</span>
        </div>
        ${thumb}
        <div class="ag-artifact-prompt">${esc(prompt || label)}</div>
        <div class="ag-artifact-meta">${esc(label)} · ${esc(formatTime(job.created_at))}</div>
      </div>`;
    }).join("");
    box.innerHTML = items;
  }

  async function refreshArtifacts() {
    if (!state.currentId) {
      state.artifacts = [];
      renderArtifacts();
      return;
    }
    try {
      const data = await apiJSON("GET",
        "/api/agent/sessions/" + encodeURIComponent(state.currentId) + "/jobs");
      state.artifacts = Array.isArray(data.jobs) ? data.jobs : [];
      renderArtifacts();
    } catch (err) {
      state.artifacts = [];
      renderArtifacts();
    }
  }

  function toggleArtifacts(force) {
    const panel = $("#agArtifacts");
    if (!panel) return;
    const open = force == null ? panel.hidden : !!force;
    panel.hidden = !open;
    state.artifactsOpen = open;
    if (open) refreshArtifacts();
  }

  // ---------- 抽屉 ----------

  function toggleDrawer(force) {
    const page = $("#agentPage");
    const backdrop = $("#agDrawerBackdrop");
    if (!page) return;
    const open = force == null ? !page.classList.contains("ag-drawer-open") : !!force;
    page.classList.toggle("ag-drawer-open", open);
    // 【抽屉关掉后必须真的「不可点」—— 2026-11-11 实测踩过】
    // CSS 里抽屉是 translateX(-102%) 移出屏幕的，但**它还在文档流里**，
    // 依然能挡住主区按钮的 pointer events（translate 不改变命中测试）。
    // 实测：点了会话之后抽屉视觉上关掉了，但「本次会话产物」按钮
    // 点了 30 秒都点不动 —— 报 "subtree intercepts pointer events"。
    //
    // 修法：关掉时加 visibility:hidden（配合 CSS 的 transition），
    // 让它彻底退出命中测试；打开时恢复。
    // （不能用 display:none —— 那会让开合没有过渡动画。）
    state.drawerOpen = open;
    if (backdrop) {
      // 桌面端抽屉不挡主区 -> 不显示遮罩（只有窄屏才需要）。
      const narrow = window.matchMedia("(max-width: 700px)").matches;
      backdrop.hidden = !(open && narrow);
    }
    const drawer = $("#agDrawer");
    if (drawer) {
      drawer.setAttribute("aria-hidden", open ? "false" : "true");
      drawer.classList.toggle("ag-drawer-hidden", !open);
    }
  }

  // ---------- 启动 ----------

  // init 只跑一次。
  //
  // 【为什么必须幂等 —— 2026-11-11 实测踩过】
  // 这个脚本有两条启动路径：
  //   1) document.readyState === "loading" -> 等 DOMContentLoaded
  //   2) 否则直接 init()
  // 在某些加载时序下**两条都会走到**（脚本在 DOMContentLoaded 之后才被
  // 解析，但 readyState 还没翻转的窗口期）。
  //
  // 后果不是「多跑一遍没关系」，而是**状态被打回初始值**：
  // 第一次 init 已经 loadSessions 拿到 2 个会话 + loadSession 恢复了消息，
  // 第二次 init 又跑一遍，中途 state.sessions 被清空、
  // currentId 被 saveCurrentId("") 抹掉 ——
  // 用户看到的现象就是「刷新后历史全没了」。
  //
  // 幂等守卫是这里唯一正确的修法。
  let _initialized = false;

  async function init() {
    if (_initialized) return;
    _initialized = true;
    const agent = window.AgentUI;
    // 【先绑事件，再拉数据 —— 2026-11-11 实测踩过】
    // 原来是把所有 addEventListener 放在 init() 的**最后**，
    // 而 init() 开头有两个 await（loadSessions / loadSession）。
    // 在这两个网络请求期间，页面上的按钮看起来是「死的」——
    // 用户点了菜单没反应，自动化验收也会静默等到超时。
    //
    // 正确做法：事件绑定是**同步**的、不依赖任何数据，
    // 所以先绑；数据回来后再渲染。
    bindEvents(agent);
    // 加载会话清单
    await loadSessions();

    // 恢复当前会话：
    //   1. localStorage 里记着 id -> 拉详情
    //   2. 没有 id 但服务端有会话 -> 默认选最近一个（按 updated_at 倒序的第一个）
    //   3. 一个会话都没有 -> 建一个空会话，让用户直接开聊
    //
    // 【为什么第 2 步要默认选最近的会话】
    // 用户刷新页面最常见的期待是「回到我刚才在聊的那个」。
    // 如果每次都落到空会话，用户得先点一次列表才能继续 —— 多余的一步。
    let target = readCurrentId();
    if (!target && state.sessions.length) target = state.sessions[0].id;
    if (target) {
      const messages = await loadSession(target);
      if (messages && messages.length && agent && agent.restoreMessages) {
        agent.restoreMessages(messages);
      } else if (agent && agent.restoreMessages) {
        agent.restoreMessages([]);
      }
    } else {
      await createSession();
      if (agent && agent.restoreMessages) agent.restoreMessages([]);
    }
    renderCurrentSessionLabel();
  }

  // 事件绑定（同步、不依赖数据 —— 见 init 里的说明）。
  function bindEvents(agent) {
    const menu = $("#agMenu");
    if (menu) menu.addEventListener("click", () => toggleDrawer());
    const close = $("#agDrawerClose");
    if (close) close.addEventListener("click", () => toggleDrawer(false));
    const backdrop = $("#agDrawerBackdrop");
    if (backdrop) backdrop.addEventListener("click", () => toggleDrawer(false));
    const drawerNew = $("#agDrawerNew");
    if (drawerNew) drawerNew.addEventListener("click", async () => {
      await createSession();
      toggleDrawer(false);
    });

    // 会话列表：切换 / 删除（事件委托）
    const list = $("#agSessionList");
    if (list) {
      list.addEventListener("click", async event => {
        const del = event.target.closest("[data-act='delete-session']");
        if (del) {
          event.stopPropagation();
          const sid = del.dataset.sessionId;
          // 二次确认 —— 删除不可撤销。
          // 【为什么用 confirm 而不是自己弹窗】confirm 是浏览器原生、
          // 不会被页面样式破坏、也不会漏掉「Escape 取消」。
          const ok = window.confirm(
            "确定删除这个交互吗？\n\n对话记录会被永久删除（已生成的任务不受影响）。");
          if (!ok) return;
          await deleteSession(sid);
          return;
        }
        const item = event.target.closest("[data-session-id]");
        if (!item) return;
        const sid = item.dataset.sessionId;
        if (!sid) return;
        // 【先关抽屉，再加载 —— 2026-11-11 实测踩过】
        // 原来是把 loadSession(await) 放前面、toggleDrawer(false) 放后面。
        // loadSession 是网络请求（几十到几百 ms），这段时间抽屉一直开着，
        // 盖住主区 —— 用户看到「点了会话，但界面没反应」。
        // 而且在自动化验收里会直接卡死：下一步要点「本次会话产物」按钮，
        // 但那个按钮被**还开着的抽屉**挡住了 pointer events。
        // 关抽屉必须**同步前置**，不能等网络。
        //
        // 【为什么「点当前会话」也要关抽屉】
        // 原实现在 `sid === state.currentId` 时直接 return —— 于是
        // 「点当前会话（不切换）」会让抽屉**一直开着**，
        // 主区被挡住、什么都点不动。
        // 点当前会话 = 「我要看这个会话」-> 关抽屉是合理且必要的。
        toggleDrawer(false);
        if (sid === state.currentId) return;
        const messages = await loadSession(sid);
        if (agent && agent.restoreMessages) agent.restoreMessages(messages || []);
      });
    }

    // 产物面板
    const artBtn = $("#agArtifactsBtn");
    if (artBtn) artBtn.addEventListener("click", () => toggleArtifacts());
    const artClose = $("#agArtifactsClose");
    if (artClose) artClose.addEventListener("click", () => toggleArtifacts(false));

    // 窄屏遮罩状态跟随窗口宽度变化（旋转 / 缩放）
    window.addEventListener("resize", () => {
      if (!state.drawerOpen) return;
      const narrow = window.matchMedia("(max-width: 700px)").matches;
      const backdrop = $("#agDrawerBackdrop");
      if (backdrop) backdrop.hidden = !narrow;
    });
  }

  // 对外接口（给 agent.js 用）
  window.AgentSessions = {
    init,
    loadSessions,
    loadSession,
    createSession,
    deleteSession,
    refreshArtifacts,
    toggleArtifacts,
    toggleDrawer,
    state,
    // agent.js 在建完任务后调用，刷新产物面板
    onJobsCreated: function () {
      if (state.artifactsOpen) refreshArtifacts();
    },
  };

  // 页面加载后启动
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
