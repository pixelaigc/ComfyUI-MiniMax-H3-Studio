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
    // 当前的搜索关键词（2026-11-11 新增）。
    // 空串 = 不过滤。
    search: "",
    // 产物轮询句柄（未完成任务实时刷新状态）。
    artifactTimer: null,
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

  // ---------- URL 路由（Agent 会话） ----------
  //
  // 【为什么需要 —— 2026-11-13 用户需求「新建交互后立即跳转」】
  // 需求原文：「点击后无反应：实际应创建新 session 并**立即跳转到该新 session**
  // （前端路由与选中态同步更新），而不是停留在当前页面」。
  //
  // 本项目原本**没有任何 URL 路由**（app.js 里 0 处 location.hash 消费者），
  // 所以「跳转到新 session」在 URL 上是**无处可跳**。
  // 这里加一条**最小**路由：#/agent/<session_id>
  //   · 只在 Agent 会话变化时写；
  //   · 用 replaceState 而不是 pushState —— 新建会话不该在历史里堆一串
  //     「后退」陷阱（用户按一次后退应该回上一页，不是回上一个会话）；
  //   · 读取时**只认自己这个命名空间**（#/agent/...），
  //     不动别人写的 hash（比如 app.js 里 locateJob 的 #job=xxx）。
  const ROUTE_PREFIX = "#/agent/";

  // 把当前会话写进 URL（路由 + 选中态同步）。
  //
  // 【2026-11-15 修② · 媒体库类型过滤串台】只在**用户真的打开了 Agent 页**
  // 时才写路由。
  //
  // 【原来为什么是错的】
  //   syncRoute() 在 saveCurrentId() 里被无条件调用，而 saveCurrentId()
  //   在 **init() 恢复会话**时也会走 —— 于是**用户从没点过 Agent**，
  //   只是打开工作台，URL 就被写成 `#/agent/<某个会话id>`。
  //   这正是用户 2026-11-15 报的「我没访问过 agent，URL 却变成
  //   http://192.168.3.78:8190/#/agent/50269d359dfa4106」。
  //
  // 【它为什么还会串台媒体库】
  //   见 web/agent.js::show() 的说明（state.page 没被正确更新）
  //   —— 两个问题叠在一起，才出现「媒体库只剩视频」。
  //
  // 【现在的规则】
  //   只有「Agent 面板当前是显示状态」才写路由。
  //   后台恢复会话（页面刚加载、用户还没点 Agent）**不写**路由 ——
  //   那只是把上次的会话内容准备好，不是「用户正在 Agent 页」。
  function syncRoute(sessionId) {
    const sid = sessionId || "";
    // Agent 页当前可见吗？不可见就别改 URL。
    const agentPage = document.querySelector("#agentPage");
    const agentVisible = !!(agentPage && !agentPage.classList.contains("page-hidden"));
    if (!agentVisible) return;
    const target = sid ? ROUTE_PREFIX + encodeURIComponent(sid) : ROUTE_PREFIX;
    try {
      if (location.hash === target) return;
      // replaceState：不产生新的历史记录（见上面的说明）。
      history.replaceState(null, "", target);
    } catch (e) {
      // 某些环境禁用了 history API -> 退回 hash 赋值（至少有可视化反馈）。
      try { location.hash = target; } catch (e2) { /* 忽略 */ }
    }
  }

  // 从 URL 里读出会话 id（认自己的命名空间，别人的 hash 一律忽略）。
  function readRouteSessionId() {
    const hash = String(location.hash || "");
    if (!hash.startsWith(ROUTE_PREFIX)) return "";
    const raw = hash.slice(ROUTE_PREFIX.length);
    if (!raw) return "";
    try { return decodeURIComponent(raw); }
    catch (e) { return raw; }
  }

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
    // 【路由同步 —— 2026-11-13】见 syncRoute 的说明。
    // 放这里而不是散在各个调用点，是因为 saveCurrentId 是
    // 「当前会话变了」的**唯一**收敛点 —— 建会话、切会话、删会话都会经过它。
    syncRoute(state.currentId);
  }

  // 创建时间用**绝对日期**，不用相对时间。
  //
  // 【为什么不能用 formatTime —— 2026-11-12】
  // formatTime 是相对时间（"3 分钟前" / "昨天" / "3-12"）。
  // 相对时间对「最后更新时间」合适（用户关心的是「多久之前」）。
  // 但对「创建时间」不合适：会话创建于 3 天前，3 天后看就是 "3-12"，
  // 再过 5 个月就是 "3-12" —— 用户完全无法判断这是哪一年的 3-12。
  // 列表项空间有限，写绝对日期（2026-03-12）最省心也最不会误导。
  function formatDate(ts) {
    if (!ts) return "";
    const t = Number(ts) * 1000;
    if (!isFinite(t) || t <= 0) return "";
    const d = new Date(t);
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    const today = new Date();
    const isThisYear = yyyy === today.getFullYear();
    // 同年不显示年份（省 5 个字符）；跨年才显示。
    return isThisYear ? `${mm}-${dd}` : `${yyyy}-${mm}-${dd}`;
  }

  // 会话状态 -> 徽章样式类。
  //
  // 【状态语义 —— 2026-11-12】
  // 后端只产出「这一轮对话的收尾方式」：空会话 / 完成 / 已截断 / 失败 / 待补充。
  // 颜色与对话气泡里的状态徽章（agent.js 的 statusBadge）保持一致：
  //   完成 = 绿、已截断/待补充 = 黄、失败 = 红、空会话 = 灰。
  // 截断是**部分成功**（拿到了已完成的部分结果），所以是黄色不是红色。
  function statusClass(status) {
    switch (status) {
      case "completed": return "ok";
      case "truncated": return "warn";
      case "failed": return "bad";
      case "needs_clarification": return "warn";
      case "empty": return "empty";
      default: return "empty";   // 未知状态 -> 灰色（不假装知道）
    }
  }

  function readCurrentId() {
    try { return localStorage.getItem(STORAGE_KEY) || ""; }
    catch (e) { return ""; }
  }

  // ---------- 会话列表 ----------

  async function loadSessions() {
    try {
      // 搜索词走 query 参数（服务端用 LIKE 匹配标题 + 任意消息正文）。
      const q = state.search ? encodeURIComponent(state.search) : "";
      const path = "/api/agent/sessions?limit=200" + (q ? "&q=" + q : "");
      const data = await apiJSON("GET", path);
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
      const arts = Number(s.artifact_count || 0);
      // 【创建时间 —— 2026-11-12】需求要求列表同时展示「创建/最后更新时间」。
      // 后端一直有 created_at（之前前端丢弃了），现在显示绝对日期。
      const created = formatDate(s.created_at);
      // 【未完成任务数 —— 2026-11-12】
      // 文案必须指向**任务**，不能说会话「进行中」——
      // 服务端没有 per-session 运行注册表（AGENT_GATE 是全局信号量），
      // 这里统计的是 jobs.status，是任务层面的事实。
      const pending = Number(s.pending_count || 0);
      // 【状态徽章 —— 2026-11-12】后端给的 status_label 是中文标签，
      // 前端不自己翻译（免得两处显示成两个词）。
      const st = String(s.status || "");
      const stLabel = s.status_label || "";
      const stBadge = stLabel
        ? `<span class="ag-session-status ag-badge ag-badge-${statusClass(st)}">${esc(stLabel)}</span>`
        : "";
      return `<div class="ag-session-item${active}" data-session-id="${esc(s.id)}"
                   role="button" tabindex="0" title="${title}">
        <div class="ag-session-item-title">${title}</div>
        ${preview ? `<div class="ag-session-item-preview">${preview}</div>` : ""}
        <div class="ag-session-item-meta">
          <span class="ag-session-item-time">${esc(time)}</span>
          ${created ? `<span class="ag-session-item-created">建于 ${esc(created)}</span>` : ""}
          <span class="ag-session-item-count">${count} 条</span>
          ${arts ? `<span class="ag-session-item-arts" title="该会话创建的任务数（每个任务产出一个作品）">${arts} 产物</span>` : ""}
          ${pending ? `<span class="ag-session-item-pending" title="该会话下还没跑完的任务数">${pending} 个生成中</span>` : ""}
          ${stBadge}
        </div>
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
    let index = 0;
    for (const m of rawMessages || []) {
      const meta = m.meta || {};
      // 稳定 id：会话内第几条消息。用于 merge 模式下的去重
      // （同一条历史不能被加两次）。
      const msg = { role: m.role, content: m.content || "",
                    _sid: "hist-" + (index++) };
      if (m.role === "user") {
        if (Array.isArray(meta.attachments) && meta.attachments.length) {
          msg.attachments = meta.attachments;
        }
        out.push(msg);
        continue;
      }
      if (m.role === "tool") {
        // 工具消息恢复成工具轨迹里的一条（名字 + 结果摘要）。
        //
        // 【为什么必须**并进上一条 assistant 气泡**而不是各自成泡 —— 2026-11-13】
        // 原来每条 tool 消息都被 push 成**一个独立的 assistant 气泡**，
        // 每个气泡自带一个 .ag-tools 容器（margin-top:10px + padding:9px 11px
        // + border + 圆角）。
        //
        // 实测后果（真实浏览器量测）：
        //   实时路径（流式 tool_call）：2 个工具 -> 1 个容器，总高 104px
        //   恢复路径（会话还原）    ：2 个工具 -> 2 个容器，总高 142px
        // 同一段对话，**切换会话前后的「工具密度」差了 38px** —— 这正是
        // 用户报的「当前 session 密度正常，切换别的 session 再切回来密度会变」。
        //
        // 服务端的落库顺序是：assistant(答案) -> tool -> tool -> ...
        // 工具轨迹本来就属于**那条 assistant 消息**（见 server.py 的
        // add_message("assistant", ...) 后紧跟 add_message("tool", ...)），
        // 所以还原时把 tool 并进它前面的 assistant 气泡才是**忠实还原**，
        // 不是「凑」出来的。
        //
        // 判定「上一条是不是 assistant」用 out 的最后一条：
        //   - 是 assistant -> 并入它的 tools[]
        //   - 不是（user 之后直接跟 tool，或列表开头就是 tool）
        //     -> 自己开一个新气泡（保证工具不丢）
        const note = (m.content || "").split(":").slice(1).join(":").trim();
        const toolEntry = {
          name: m.tool_name || "(工具)",
          ok: note !== "error" && note !== "",
          ms: null,
          arguments: null,
          result: note || "ok",
        };
        const prev = out.length ? out[out.length - 1] : null;
        if (prev && prev.role === "assistant" && !prev._isUserTurnTail) {
          // 上一条是 assistant（它就是这批工具的归属消息）-> 并入。
          prev.tools = prev.tools || [];
          prev.tools.push(toolEntry);
        } else {
          out.push({
            role: "assistant",
            content: "",
            tools: [toolEntry],
          });
        }
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

  // 让 Agent 面板**切到**指定会话。
  //
  // 【为什么必须有这个入口 —— 2026-11-13 实测 BUG：「新建交互」点了没反应】
  // 原来 createSession() 只做了「会话层」的事：
  //   服务端建会话 -> saveCurrentId -> state.messages = [] -> 刷列表
  // 但**消息区**（#agMessages）里显示的还是**上一个会话**的消息，
  // 因为消息区的内容由 agent.js 的 state.messages 渲染，
  // 而 createSession 从没让 agent.js 也切过去。
  //
  // 实测（修复前，真实浏览器）：
  //   点「＋ 新建交互」-> 会话确实建出来了（侧栏高亮了新会话）
  //   但 URL 没变、AgentUI.state.sessionId 仍是空的、
  //   #agMessages 里还是旧会话的 3 条消息 —— 用户看到的是「点了没反应」。
  //
  // 现在统一走这里：会话 id 同步给 agent.js + 消息区清空并重绘。
  function switchAgentView(sessionId) {
    const agent = window.AgentUI;
    if (!agent || typeof agent.restoreMessages !== "function") return;
    // sessionId 传下去 -> agent.js 用它做折叠态 key 的作用域前缀，
    // 同时把消息区整段替换成这个会话的内容（这里是空的 = 新会话）。
    agent.restoreMessages([], { merge: false, sessionId: sessionId || "" });
  }

  // 【createSession 的选项】
  //   switchView  是否把 Agent 面板切到新会话（默认 true = 用户主动新建）
  //
  // 【为什么需要这个开关 —— 2026-11-13 修复自身引入的回归】
  // init() 在「一个会话都没有」时会调 createSession() 建一个空会话。
  // 如果那里也切视图（merge:false = 整段替换），就会把
  // **init 还没跑完时用户刚发的那条消息抹掉** ——
  // 这正是 restoreMessages 的 merge 模式当初要防的竞态
  // （见 agent.js restoreMessages 里的说明）。
  //
  // 所以：**只有用户主动新建时**才切视图；
  // init 里的自动建会话由 init 自己用 merge 模式接管。
  async function createSession(options) {
    const opts = options || {};
    const switchView = opts.switchView !== false;
    try {
      const data = await apiJSON("POST", "/api/agent/sessions", {});
      const sid = data.session_id || "";
      if (!sid) throw new Error("服务端没有返回会话 id");
      saveCurrentId(sid);
      state.messages = [];
      state.artifacts = [];
      // 【切到新会话 —— 2026-11-13】见 switchAgentView 的说明。
      // 少了这一步，消息区还停在旧会话 -> 用户看到「点了没反应」。
      if (switchView) switchAgentView(sid);
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

  // 产物的「分辨率 / 比例」标签。
  //
  // 【为什么不直接读 params.resolution —— 2026-11-12】
  // params.resolution 是**档位字符串**（"544p" / "768p" / "1mp"），
  // 而文生视频面板提交的是 width/height（真实像素），不写 resolution。
  // 于是面板实际跑 1344×768，卡片却显示「544p」—— 与产物对不上。
  //
  // 【取值优先级】
  //  1. job.resolution_info.label —— 服务端按**实际输出像素**算好的标签
  //     （唯一真源；由 reso_label.describe 产出）。有就直接用。
  //     这样等「生成库分辨率显示修复」那条分支合进来后，
  //     本面板**自动**显示准确值，不需要再改一遍。
  //  2. 退回 outputs 里带的宽高（如果有）。
  //  3. 退回请求参数 width/height + resolution 档位 + aspect。
  function artifactResolution(job) {
    const params = job.params || {};
    const outputs = Array.isArray(job.outputs) ? job.outputs : [];
    // 1) 服务端算好的标签（优先）
    const info = job.resolution_info;
    if (info && typeof info === "object" && info.label) {
      return String(info.label);
    }
    let w = 0;
    let h = 0;
    // 2) 实际输出尺寸
    for (const out of outputs) {
      if (out && out.width && out.height) { w = Number(out.width); h = Number(out.height); break; }
    }
    // 3) 请求参数
    if (!w || !h) {
      w = Number(params.width || 0);
      h = Number(params.height || 0);
    }
    const aspect = String(params.aspect || "").trim();
    const tier = String(params.resolution || "").trim();
    const parts = [];
    if (w && h) parts.push(`${w}×${h}`);
    if (tier) parts.push(tier);
    if (aspect) parts.push(aspect);
    return parts.join(" · ");
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
      const isVideo = kind === "video";
      const isAudio = kind === "audio";
      const isImage = kind === "image";
      // 图片产物：缩略图可点 -> 打开大图查看器（复用 app.js 已有的 lightbox）。
      // 视频/音频：缩略图本身是播放器，点它会开始播放，**不做**预览绑定。
      // 【2026-11-19 缺陷 2 修 —— 防御层】
      // status='done' 但**没有产物** -> 显示明确的失败占位，
      // 不留一个空缩略图位（深色背景上看起来就是「全黑图」）。
      const doneWithoutOutput = (job.status === "done" && !outputs.length);
      const thumb = url
        ? (isVideo
            ? `<video class="ag-artifact-thumb is-video" src="${esc(url)}" muted preload="metadata"></video>`
            : isAudio
              ? `<div class="ag-artifact-thumb is-audio">♪</div>`
              : `<button type="button" class="ag-artifact-thumb-btn" data-act="preview-artifact"
                          data-url="${esc(url)}" data-name="${esc(first.name || job.id)}"
                          title="点击查看大图">
                   <img class="ag-artifact-thumb" src="${esc(url)}" alt="${esc(job.id)}" loading="lazy">
                 </button>`)
        : (doneWithoutOutput
            ? `<div class="ag-artifact-thumb is-failed" title="生成结束但没有产物文件">⚠</div>`
            : `<div class="ag-artifact-thumb"></div>`);
      const prompt = job.params && job.params.prompt ? String(job.params.prompt) : "";
      const status = job.status || "queued";
      const label = job.gen_type_label || job.mode || "任务";
      // 未完成的任务（排队中/生成中）标出来 —— 面板会持续刷新它们的状态。
      const pending = status === "queued" || status === "running";
      const resolution = artifactResolution(job);
      const meta = [label, resolution, formatTime(job.created_at)].filter(Boolean).join(" · ");
      return `<div class="ag-artifact-item${pending ? " is-pending" : ""}"
                   data-job-id="${esc(job.id)}">
        <div class="ag-artifact-head">
          <span class="ag-artifact-id">${esc(job.id)}</span>
          <span class="ag-job-badge ag-badge-${status === "done" ? "ok" : (status === "failed" ? "bad" : "warn")}">${esc(status)}</span>
        </div>
        ${thumb}
        <div class="ag-artifact-prompt">${esc(prompt || label)}</div>
        <div class="ag-artifact-meta">${esc(meta)}</div>
        <div class="ag-artifact-actions">
          ${isImage && url ? `<button type="button" class="ag-artifact-locate" data-act="preview-artifact"
                data-url="${esc(url)}" data-name="${esc(first.name || job.id)}">查看大图</button>` : ""}
          <button type="button" class="ag-artifact-locate" data-act="locate-job"
                  data-job-id="${esc(job.id)}">在生成库中定位</button>
        </div>
      </div>`;
    }).join("");
    box.innerHTML = items;
  }

  // 未完成任务的状态轮询间隔（毫秒）。
  //
  // 【为什么是 4 秒】
  // 任务状态从 queued -> running -> done，实测几十秒到几分钟。
  // 4 秒足够跟得上，又不会把服务端打爆（面板开着时每 4 秒一次）。
  const ARTIFACT_POLL_MS = 4000;

  // 面板打开时是否该继续轮询。
  //
  // 【为什么要条件轮询而不是无条件 setInterval】
  // 面板关着的时候没必要轮询 —— 那是白白发请求。
  // 只有「面板开着」且「有未完成的任务」时才继续。
  function hasPendingArtifacts() {
    return state.artifacts.some(j => j.status === "queued" || j.status === "running");
  }

  function stopArtifactPolling() {
    if (state.artifactTimer) {
      clearInterval(state.artifactTimer);
      state.artifactTimer = null;
    }
  }

  function startArtifactPolling() {
    stopArtifactPolling();
    state.artifactTimer = setInterval(() => {
      if (!state.artifactsOpen) { stopArtifactPolling(); return; }
      if (!hasPendingArtifacts()) { stopArtifactPolling(); return; }
      refreshArtifacts();
    }, ARTIFACT_POLL_MS);
  }

  async function refreshArtifacts() {
    if (!state.currentId) {
      state.artifacts = [];
      renderArtifacts();
      stopArtifactPolling();
      return;
    }
    try {
      const data = await apiJSON("GET",
        "/api/agent/sessions/" + encodeURIComponent(state.currentId) + "/jobs");
      state.artifacts = Array.isArray(data.jobs) ? data.jobs : [];
      renderArtifacts();
      // 有未完成的任务 -> 继续轮询直到全部结束。
      if (state.artifactsOpen && hasPendingArtifacts()) {
        startArtifactPolling();
      } else {
        stopArtifactPolling();
      }
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
    if (open) {
      refreshArtifacts();
    } else {
      // 面板关了 -> 停掉轮询（别白白发请求）。
      stopArtifactPolling();
      // 【清空面板内容】
      // 关掉的面板如果还留着上一次的 DOM，用户看不到（display:none）
      // 但内容是**陈旧**的：下次打开的瞬间会先闪一下旧数据，
      // 而且自动化验收会误以为「面板还开着」——
      // 实测就是踩了这个：关面板后 querySelectorAll(".ag-artifact-item")
      // 还能查到旧卡片，导致「未完成任务实时刷新」的断言假通过。
      const body = $("#agArtifactsBody");
      if (body) body.innerHTML = "";
      state.artifacts = [];
    }
  }

  // 「在生成库中定位」—— 把用户带到生成列表里的那一条任务。
  //
  // 【原实现是假功能 —— 2026-11-12 实测发现】
  // 原来只做两件事：切到生成页 + 把任务 id 写进 location.hash。
  // 但**全仓没有任何地方读这个 hash**（grep 命中 0 处消费者），
  // 于是这个按钮的实际效果只是「切换了一下页面」，
  // 用户到了生成库还得自己翻找那条任务 —— 按钮写着「定位」却没定位。
  //
  // 【现在的做法】
  // 调 app.js 暴露的 H3Studio.locateJobInLibrary()：
  // 它走生成库既有的关键词筛选通道（服务端按 id LIKE 匹配），
  // 等列表渲染完成后滚动到那张卡片并高亮。
  // 用户还能在搜索框里看到并修改这个条件 —— 定位不是黑魔法。
  function locateJob(jobId) {
    if (!jobId) return;
    const bridge = window.H3Studio;
    const located = bridge && typeof bridge.locateJobInLibrary === "function"
      ? bridge.locateJobInLibrary(jobId)
      : false;
    if (!located) {
      // 兜底：老服务端 / app.js 还没加载 -> 至少切到生成页，
      // 并把 id 放进 hash（用户还能在搜索框里手动搜）。
      const nav = document.querySelector('.nav-item[data-mode="t2i"]')
        || document.querySelector(".nav-item");
      if (nav) nav.click();
      try {
        location.hash = "job=" + encodeURIComponent(jobId);
      } catch (e) {
        // 某些环境禁用了 hash 写入 —— 不影响主流程。
      }
    }
    const status = $("#agStatus");
    if (status) {
      status.textContent = located
        ? "已在生成库中定位任务 " + jobId + "。"
        : "已切到生成库，任务 " + jobId + " 可在生成列表中查看。";
    }
  }

  // 「查看大图」—— 复用生成库已经调好的大图查看器。
  //
  // 【为什么不自己写一个模态框 —— 2026-11-12】
  // app.js 的 lightbox 已经支持缩放 / 拖拽 / 适应屏幕 / 原始尺寸 /
  // 下载 / 新窗口打开，交互是调好的。自己再写一个只有「一张大图」的
  // 模态框，功能更少、样式还得再对一遍 —— 两份大图查看器必然漂移。
  function previewArtifact(url, name) {
    if (!url) return;
    const bridge = window.H3Studio;
    if (bridge && typeof bridge.lightboxShow === "function") {
      // 宽高传 0 -> lightbox 会在图片 load 后用 naturalWidth/Height 自适应。
      bridge.lightboxShow(url, name || "产物", 0, 0);
      return;
    }
    // 兜底：app.js 没加载 -> 新窗口打开原图。
    try {
      window.open(url, "_blank", "noopener");
    } catch (e) {
      // 弹窗被拦截 -> 至少给用户一个状态提示
      const status = $("#agStatus");
      if (status) status.textContent = "产物地址：" + url;
    }
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
    //   0. URL 路由里有 id（#/agent/<sid>）-> 以它为准（深链）
    //   1. localStorage 里记着 id -> 拉详情
    //   2. 没有 id 但服务端有会话 -> 默认选最近一个（按 updated_at 倒序的第一个）
    //   3. 一个会话都没有 -> 建一个空会话，让用户直接开聊
    //
    // 【为什么第 2 步要默认选最近的会话】
    // 用户刷新页面最常见的期待是「回到我刚才在聊的那个」。
    // 如果每次都落到空会话，用户得先点一次列表才能继续 —— 多余的一步。
    //
    // 【为什么 URL 优先于 localStorage —— 2026-11-13】
    // 需求要求「新建交互后立即跳转到该新 session（前端路由与选中态同步）」。
    // 路由既然写了，就必须**能被读回来** —— 否则刷新后路由指向 A、
    // 界面却显示 B，路由就成了摆设。
    let target = readRouteSessionId() || readCurrentId();
    if (!target && state.sessions.length) target = state.sessions[0].id;
    if (target) {
      const messages = await loadSession(target);
      if (agent && agent.restoreMessages) {
        // 【merge 模式】init 是异步的 —— 这期间用户可能已经发了消息。
        // 直接替换会把那条消息弄丢（实测踩过）。
        agent.restoreMessages(messages, { merge: true });
      }
    } else {
      // 【不切视图 —— 2026-11-13】
      // init 里的自动建会话不能切视图（会把用户刚发的消息抹掉，
      // 见 createSession 的 switchView 说明）。下面用 merge 模式接管。
      await createSession({ switchView: false });
      if (agent && agent.restoreMessages) {
        agent.restoreMessages([], { merge: true });
      }
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
        // 【会话作用域 —— 2026-11-13 工具密度修复】
        // loadSession(sid) 里已经 saveCurrentId(sid)，
        // 会话层的 state.currentId 已经是新会话；
        // agent.js 的 restoreMessages 会自己去问会话层要当前会话 id
        // （currentSessionId()），用它做折叠态 key 的作用域前缀。
        //
        // 以前 agent.js 的 state.sessionId 只在**发消息**时被赋值，
        // 切会话这条路从不更新它 —— 于是新旧会话的 m0-tool-0
        // 撞成同一个 key -> 折叠态跨会话串味 -> 工具区密度随切换乱跳。
        if (agent && agent.restoreMessages) {
          agent.restoreMessages(messages || []);
        }
      });
    }

    // 会话搜索（2026-11-11 新增需求）。
    //
    // 【为什么用 input 事件而不是 change】
    // change 只在失焦时触发 —— 用户一边打字一边看列表会觉得很迟钝。
    // input 事件每次按键都触发，但这里做了 **250ms 防抖**，
    // 否则打「赛博朋克」会发 4 次请求。
    const search = $("#agSessionSearch");
    if (search) {
      let timer = null;
      search.addEventListener("input", event => {
        state.search = event.target.value || "";
        if (timer) clearTimeout(timer);
        timer = setTimeout(() => { loadSessions(); }, 250);
      });
      // 回车立刻搜（不等防抖）
      search.addEventListener("keydown", event => {
        if (event.key === "Enter") {
          event.preventDefault();
          if (timer) clearTimeout(timer);
          loadSessions();
        }
      });
    }

    // 产物面板
    const artBtn = $("#agArtifactsBtn");
    if (artBtn) artBtn.addEventListener("click", () => toggleArtifacts());
    const artClose = $("#agArtifactsClose");
    if (artClose) artClose.addEventListener("click", () => toggleArtifacts(false));

    // 产物卡片上的「查看大图 / 在生成库中定位」（事件委托 —— 卡片是重绘出来的，
    // 不能在 renderArtifacts 里逐个绑定，否则每次重绘都要重新绑）。
    const artBody = $("#agArtifactsBody");
    if (artBody) {
      artBody.addEventListener("click", event => {
        // 图片产物 -> 打开大图查看器（复用 app.js 的 lightbox）
        const preview = event.target.closest("[data-act='preview-artifact']");
        if (preview) {
          event.preventDefault();
          event.stopPropagation();
          previewArtifact(preview.dataset.url, preview.dataset.name);
          return;
        }
        const btn = event.target.closest("[data-act='locate-job']");
        if (!btn) return;
        event.preventDefault();
        locateJob(btn.dataset.jobId);
      });
    }

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
