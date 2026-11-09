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
      </div>`;
      return;
    }
    box.innerHTML = state.messages.map(bubbleHtml).join("");
    box.scrollTop = box.scrollHeight;
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
    if (status) {
      status.textContent = busy
        ? "Agent 正在多步处理（本地模型较慢，通常 30~120 秒）…"
        : "";
    }
  }

  // ---------- 动作 ----------

  async function send() {
    if (state.busy) return;
    const input = $("#agInput");
    const text = (input.value || "").trim();
    if (!text) return;
    input.value = "";
    state.messages.push({ role: "user", content: text });
    renderMessages();
    setBusy(true);

    try {
      const data = await apiJSON("POST", "/api/agent/chat", {
        message: text,
        session_id: state.sessionId || undefined,
      });
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
      state.messages.push({
        role: "assistant",
        content: "",
        result: { status: "failed" },
        error: err.message,
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
    page.style.display = "block";
    const jobs = $("#jobsArea");
    const panel = $("#panel");
    const stitch = $("#stitchPanel");
    const system = $("#systemPage");
    const providers = $("#providersPage");
    const assets = $("#assetsPage");
    if (jobs) jobs.style.display = "none";
    if (panel) panel.style.display = "none";
    if (stitch) stitch.style.display = "none";
    if (system) system.style.display = "none";
    if (providers) providers.style.display = "none";
    if (assets) assets.style.display = "none";
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
