// 内置多模态 Agent —— 前端面板
//
// 职责边界：
//   * 只做「发消息 + 渲染结构化结果 + 把产物回填到创作面板」
//   * 不在前端做任何智能判断 —— 模式白名单、参数收敛、参考图约束
//     全部在服务端（h3studio/agent.py + server.prepare_job_params）
//
// 为什么单独一个文件而不是塞进 app.js：
//   app.js 已经 3900+ 行，且是「创作页」的逻辑。Agent 面板是独立页面，
//   独立成文件后可以单独加载/单独排错，也避免 app.js 的启动异常
//   （历史上出现过一次 esc() 未定义导致整页脚本中断）波及 Agent。
(function () {
  const $ = s => document.querySelector(s);
  const $$ = s => [...document.querySelectorAll(s)];

  const state = {
    sessionId: "",
    busy: false,
    messages: [],       // {role, content, jobs?, assets?, prompt?, steps?}
    lastResult: null,
  };

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
      let data;
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

  // ---------- 渲染 ----------

  function statusBadge(result) {
    const map = {
      completed: ["完成", "ok"],
      needs_clarification: ["需要澄清", "warn"],
      truncated: ["已截断", "warn"],
      failed: ["失败", "bad"],
    };
    const [label, cls] = map[result.status] || ["未知", ""];
    return `<span class="ag-badge ag-badge-${cls}">${esc(label)}</span>`;
  }

  function renderJobs(jobs) {
    if (!jobs || !jobs.length) return "";
    const rows = jobs.map(job => `
      <div class="ag-job">
        <div class="ag-job-head">
          <b>${esc(job.label || job.mode)}</b>
          <code>${esc(job.job_id)}</code>
          <span class="ag-job-status">${esc(job.status || "queued")}</span>
        </div>
        ${job.prompt ? `<div class="ag-job-prompt">${esc(job.prompt)}</div>` : ""}
        ${job.summary ? `<div class="ag-job-note">${esc(job.summary)}</div>` : ""}
      </div>`).join("");
    return `<div class="ag-block">
      <div class="ag-block-title">已下发的生成任务（${jobs.length}）</div>
      ${rows}
    </div>`;
  }

  function renderAssets(assets) {
    if (!assets || !assets.length) return "";
    const rows = assets.map(a => `
      <li>
        <b>${esc(a.name || a.asset_id)}</b>
        <span class="ag-tag">${esc(a.category || "")}</span>
        ${a.tags && a.tags.length ? `<span class="ag-tag">${esc(a.tags.join("、"))}</span>` : ""}
        ${a.description ? `<div class="ag-asset-desc">${esc(a.description)}</div>` : ""}
      </li>`).join("");
    return `<div class="ag-block">
      <div class="ag-block-title">命中的素材（${assets.length}）</div>
      <ul class="ag-assets">${rows}</ul>
    </div>`;
  }

  function renderPrompt(prompt) {
    if (!prompt || !prompt.enhanced) return "";
    return `<div class="ag-block">
      <div class="ag-block-title">提示词增强产物</div>
      <div class="ag-prompt-orig">${esc(prompt.original || "")}</div>
      <div class="ag-prompt-new">${esc(prompt.enhanced || "")}</div>
    </div>`;
  }

  function renderSteps(steps) {
    if (!steps || !steps.length) return "";
    const rows = steps.map(s => {
      const name = s.tool || "（结论）";
      const cls = s.ok ? "ok" : "bad";
      const err = s.error ? ` — ${esc(s.error)}` : "";
      return `<li class="ag-step ag-step-${cls}">
        <span class="ag-step-n">${esc(s.step)}</span>
        <span class="ag-step-name">${esc(name)}</span>
        <span class="ag-step-ms">${esc(s.ms)}ms</span>
        ${err ? `<span class="ag-step-err">${err}</span>` : ""}
      </li>`;
    }).join("");
    return `<details class="ag-block ag-steps">
      <summary class="ag-block-title">工具调用轨迹（${steps.length}）</summary>
      <ul class="ag-step-list">${rows}</ul>
    </details>`;
  }

  function bubbleHtml(msg) {
    const isUser = msg.role === "user";
    const body = isUser
      ? `<div class="ag-bubble-text">${esc(msg.content)}</div>`
      : `${statusBadge(msg.result || {})}
         <div class="ag-bubble-text">${esc(msg.content || "")}</div>
         ${renderJobs(msg.jobs)}
         ${renderAssets(msg.assets)}
         ${renderPrompt(msg.prompt)}
         ${renderSteps(msg.steps)}
         ${msg.error ? `<div class="ag-error">${esc(msg.error)}</div>` : ""}
         ${renderFillButton(msg)}`;
    return `<div class="ag-msg ${isUser ? "ag-msg-user" : "ag-msg-bot"}">
      <div class="ag-msg-role">${isUser ? "我" : "Agent"}</div>
      <div class="ag-msg-body">${body}</div>
    </div>`;
  }

  // 「填入创作面板」—— 把 Agent 的产物回填到创作页，
  // 这样用户可以直接改参数再点生成（Agent 给的是初稿，不是黑盒）。
  function renderFillButton(msg) {
    const jobs = msg.jobs || [];
    const prompt = msg.prompt && msg.prompt.enhanced;
    if (!jobs.length && !prompt) return "";
    return `<div class="ag-actions">
      ${prompt ? `<button class="ag-act" data-act="fill-prompt" data-text="${esc(prompt)}">填入提示词</button>` : ""}
      ${jobs.length ? `<button class="ag-act" data-act="fill-job" data-job="${esc(jobs[0].job_id)}" data-mode="${esc(jobs[0].mode)}">填入创作面板</button>` : ""}
    </div>`;
  }

  function renderMessages() {
    const box = $("#agMessages");
    if (!box) return;
    if (!state.messages.length) {
      box.innerHTML = `<div class="ag-empty">
        <b>内置多模态 Agent</b>
        <p>用一句话说清你的创作意图，Agent 会自己查素材、选模式、组装参数，最后下发成可执行的生成任务。</p>
        <p class="ag-hint">例：「素材库里有没有雨夜的素材？有的话用它做参考图，生成一段 5 秒 16:9 的视频」</p>
        <p class="ag-hint">本地模型多步处理约需 1~5 分钟，处理期间下方会实时显示进度。</p>
      </div>`;
      return;
    }
    box.innerHTML = state.messages.map(bubbleHtml).join("");
    box.scrollTop = box.scrollHeight;
  }

  // ---------- 进度反馈 ----------
  //
  // 本地模型单步就要 20~120 秒，一条完整链路 4~6 分钟（审查实测 179 秒无反馈）。
  // 静默等待会被当成卡死，所以这里显示：
  //   · 当前阶段（正在检索素材库 / 正在增强提示词 / …）
  //   · 已用时间（每秒跳动，不是静态文案）
  //   · 步骤进度（第 N/M 步）
  // 阶段文案由**服务端**给（agent.PROGRESS_STAGES / TOOL_STAGE_TEXT），
  // 前端不自己编 —— 两边各写一套必然对不上。

  const progressState = {
    stage: "",
    detail: "",
    done: 0,
    total: 0,
    startedAt: 0,
    timer: null,
  };

  function fmtElapsed(ms) {
    const s = Math.floor(ms / 1000);
    if (s < 60) return `${s} 秒`;
    const m = Math.floor(s / 60);
    const rest = s % 60;
    return rest ? `${m} 分 ${rest} 秒` : `${m} 分`;
  }

  function renderProgress() {
    const box = $("#agStatus");
    if (!box) return;
    if (!progressState.stage) { box.textContent = ""; return; }
    const elapsed = progressState.startedAt
      ? fmtElapsed(Date.now() - progressState.startedAt) : "";
    const parts = [];
    parts.push(`<span class="ag-progress-dot" aria-hidden="true"></span>`);
    parts.push(`<span class="ag-progress-stage">${esc(progressState.detail || progressState.stage)}</span>`);
    if (elapsed) parts.push(`<span class="ag-progress-elapsed">已用 ${esc(elapsed)}</span>`);
    if (progressState.total > 1) {
      parts.push(`<span class="ag-progress-step">第 ${esc(progressState.done + 1)}/${esc(progressState.total)} 步</span>`);
    }
    box.innerHTML = `<span class="ag-progress">${parts.join("")}</span>`;
    box.classList.add("ag-status-busy");
  }

  function startProgress() {
    stopProgress();
    progressState.stage = "start";
    progressState.detail = "正在理解你的意图";
    progressState.done = 0;
    progressState.total = 0;
    progressState.startedAt = Date.now();
    renderProgress();
    // 每秒刷新一次已用时间 —— 静态文案会让人以为是死图
    progressState.timer = setInterval(renderProgress, 1000);
  }

  function stopProgress() {
    if (progressState.timer) { clearInterval(progressState.timer); progressState.timer = null; }
    progressState.stage = "";
    progressState.detail = "";
    progressState.startedAt = 0;
    const box = $("#agStatus");
    if (box) box.classList.remove("ag-status-busy");
  }

  function updateProgress(event) {
    if (!event) return;
    if (event.stage) progressState.stage = event.stage;
    if (event.detail) progressState.detail = event.detail;
    if (typeof event.done === "number") progressState.done = event.done;
    if (typeof event.total === "number" && event.total > 0) progressState.total = event.total;
    renderProgress();
  }

  function setBusy(busy) {
    state.busy = busy;
    const btn = $("#agSend");
    const input = $("#agInput");
    if (btn) {
      btn.disabled = busy;
      btn.textContent = busy ? "处理中…" : "发送";
    }
    if (input) input.disabled = busy;
    const status = $("#agStatus");
    if (status && !busy) {
      stopProgress();
      status.textContent = "";
    }
  }

  // ---------- 动作 ----------

  // 走 SSE 流式端点，把 progress 事件实时喂给进度条。
  // 若流式端点不可用（老服务端 / 代理不支持），自动退回一次性 JSON 接口。
  async function sendStream(body) {
    const resp = await fetch("/api/agent/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    if (!resp.ok) {
      const raw = await resp.text();
      let data;
      try { data = raw ? JSON.parse(raw) : {}; }
      catch (e) { data = { error: raw || ("请求失败（HTTP " + resp.status + "）") }; }
      const err = new Error(data.error || ("请求失败（HTTP " + resp.status + "）"));
      err.code = data.code || "";
      err.payload = data;
      throw err;
    }

    const ct = resp.headers.get("Content-Type") || "";
    if (!ct.includes("text/event-stream")) {
      // 服务端退回了普通 JSON（不该发生，但别崩）
      const data = await resp.json();
      return data;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let result = null;

    const handle = (event, data) => {
      if (event === "progress") { updateProgress(data); return; }
      if (event === "error") {
        const err = new Error(data.error || "Agent 执行失败");
        err.code = data.code || "";
        throw err;
      }
      if (event === "result") { result = data; }
    };

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // SSE 以空行分隔事件块
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const raw = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        let event = "message";
        const dataLines = [];
        for (const line of raw.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
        }
        if (!dataLines.length) continue;
        let data;
        try { data = JSON.parse(dataLines.join("\n")); }
        catch (e) { continue; }
        handle(event, data);
      }
    }
    if (!result) throw new Error("流式响应提前结束，没有拿到结果");
    return result;
  }

  async function send() {
    if (state.busy) return;
    const input = $("#agInput");
    const text = (input.value || "").trim();
    if (!text) return;
    input.value = "";
    state.messages.push({ role: "user", content: text });
    renderMessages();
    setBusy(true);
    startProgress();

    const body = {
      message: text,
      session_id: state.sessionId || undefined,
    };
    try {
      let data;
      try {
        data = await sendStream(body);
      } catch (streamErr) {
        // 流式不可用（服务端没有这个端点 / 代理不支持 / 网络中断）时退回一次性接口。
        // 但业务错误（带 code 的 4xx/5xx）不重试 —— 直接抛出，避免重复下发任务。
        const isBusiness = streamErr.code && streamErr.code !== "agent_internal_error";
        if (isBusiness) throw streamErr;
        data = await apiJSON("POST", "/api/agent/chat", body);
      }
      state.sessionId = data.session_id || state.sessionId;
      state.lastResult = data;
      state.messages.push({
        role: "assistant",
        content: data.answer || "",
        result: data,
        jobs: data.jobs || [],
        assets: data.assets || [],
        prompt: data.prompt || null,
        steps: data.steps || [],
        error: data.error || "",
      });
    } catch (err) {
      // 失败也要让用户看到「到底怎么了」—— 不能只留一个空白的忙碌状态。
      stopProgress();
      state.messages.push({
        role: "assistant",
        content: "",
        result: { status: "failed" },
        error: err.message || "未知错误",
      });
    } finally {
      setBusy(false);
      renderMessages();
    }
  }

  async function newSession() {
    state.sessionId = "";
    state.messages = [];
    state.lastResult = null;
    stopProgress();
    renderMessages();
    const status = $("#agStatus");
    if (status) status.textContent = "";
  }

  // 把 Agent 的任务回填到创作面板
  async function fillJob(jobId, mode) {
    try {
      const job = await apiJSON("GET", "/api/jobs/" + encodeURIComponent(jobId));
      // 切到创作页并选中对应模式
      const nav = document.querySelector(`.nav-item[data-mode="${mode}"]`);
      if (nav) nav.click();
      else {
        alert("该任务的模式在当前面板里没有对应入口：" + mode);
        return;
      }
      const prompt = $("#prompt");
      if (prompt && job.params && job.params.prompt) {
        prompt.value = job.params.prompt;
        prompt.dispatchEvent(new Event("input", { bubbles: true }));
      }
      const status = $("#agStatus");
      if (status) status.textContent = `已把任务 ${jobId} 的提示词填入创作面板，可改参数后直接生成。`;
    } catch (err) {
      alert("回填失败：" + err.message);
    }
  }

  function fillPrompt(text) {
    const nav = document.querySelector('.nav-item[data-mode="t2i"]');
    if (nav) nav.click();
    const prompt = $("#prompt");
    if (!prompt) { alert("找不到提示词输入框"); return; }
    prompt.value = text;
    prompt.dispatchEvent(new Event("input", { bubbles: true }));
    const status = $("#agStatus");
    if (status) status.textContent = "已把增强后的提示词填入创作面板。";
  }

  // ---------- 初始化 ----------

  function show() {
    const page = $("#agentPage");
    if (!page) return;
    // 显示 Agent 页：移除 .page-hidden，让 .agent-page 自己的 display:flex 生效。
    // （不要写 page.style.display="block" —— 内联 display 会覆盖 CSS 的
    //   display:flex，flex 布局塌掉后消息区不再收缩，输入框会被顶出视口。）
    page.classList.remove("page-hidden");
    page.style.removeProperty("display");
    const jobs = $("#jobsArea");
    const panel = $("#panel");
    const stitch = $("#stitchPanel");
    const system = $("#systemPage");
    const providers = $("#providersPage");
    const assets = $("#assetsPage");
    [jobs, panel, stitch, system, providers, assets].forEach(el => {
      if (el) { el.classList.add("page-hidden"); el.style.removeProperty("display"); }
    });
    $$(".nav-item").forEach(x => x.classList.remove("active"));
    const nav = $("#navAgent");
    if (nav) nav.classList.add("active");
    if (window.state) window.state.page = "agent";
    if (window.updateResponsiveChrome) window.updateResponsiveChrome();
    renderMessages();
  }

  function bind() {
    const nav = $("#navAgent");
    if (nav) nav.addEventListener("click", show);
    const btn = $("#agSend");
    if (btn) btn.addEventListener("click", send);
    const input = $("#agInput");
    if (input) {
      input.addEventListener("keydown", e => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          send();
        }
      });
    }
    const fresh = $("#agNew");
    if (fresh) fresh.addEventListener("click", newSession);
    const box = $("#agMessages");
    if (box) {
      box.addEventListener("click", e => {
        const btn = e.target.closest(".ag-act");
        if (!btn) return;
        if (btn.dataset.act === "fill-prompt") fillPrompt(btn.dataset.text || "");
        else if (btn.dataset.act === "fill-job") {
          fillJob(btn.dataset.job, btn.dataset.mode);
        }
      });
    }
  }

  // 暴露给 app.js（app.js 的 show* 系列需要能切到这个页）
  window.AgentUI = { show, state, renderMessages };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
