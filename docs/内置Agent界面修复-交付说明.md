# 内置 Agent 界面修复 — 交付说明

> 仓库：`/root/projects/comfyui-minimax-h3-studio`
> 分支：`local/feature/agent-ui-fix`（基于 `local/maintainer`，commit `d3c6f79`）
> 独立 worktree：`/root/projects/h3studio-agent-ui`
> 提交：`c85513b` fix(agent-ui): 快捷选项作用域定位 + 新建交互跳转 + 工具密度一致性
> 日期：2026-11-13

---

## 0. 交付边界（遵守仓库纪律）

| 项 | 状态 |
|---|---|
| 独立 worktree | ✅ `/root/projects/h3studio-agent-ui`，未碰共享仓库 `/root/projects/comfyui-minimax-h3-studio` |
| 分支 | ✅ `local/feature/agent-ui-fix`，只在自己分支提交 |
| 他人未提交 WIP | ✅ 未动（`/root/projects/comfyui-minimax-h3-studio` 的 `M README.md` 等 10 个改动 + 10 个未跟踪文件原样保留） |
| `local/maintainer` | ✅ 未改 |
| 线上 8190 / `h3-studio.service` | ✅ 未部署、未重启（`/root/projects/h3studio-live` 仍在 `local/deploy/qwen-ref-assets-live`，工作区干净） |
| `git reset` / `stash` / `checkout -- .` / `force push` | ✅ 一次都没用 |

改动文件（3 个，全在前端）：

```
web/agent.js          | 228 ++++++++++++++++++++++++++++++-----
web/agent_sessions.js | 164 +++++++++++++++++++++++++++++-----
web/style.css         |  14 ++++
3 files changed, 377 insertions(+), 29 deletions(-)
```

---

## 1. 需求 2（优先级最高）：快捷选项填到错误的消息输入框

### 1.1 根因

`web/agent.js` 里「需确认点」的点击处理用的是**消息区里的第一个**匹配元素：

```js
// 修复前 —— web/agent.js
} else if (act === "clarify-answer") {
  const value = btn.dataset.value || "";
  const field = box.querySelector(".ag-clarify-input");   // ← 全局取第一个
  if (field) { field.value = value; field.focus(); }
} else if (act === "clarify-send") {
  const field = box.querySelector(".ag-clarify-input");     // ← 同样取第一个
  ...
}
```

`box` 是整个消息区 `#agMessages`。页面上有两条 Agent 消息各自带一组快捷选项时，
`box.querySelector(".ag-clarify-input")` **永远命中第一条**（最上面那条消息的输入框），
与「用户点了哪条消息的选项」完全无关 —— 这正是截图里
「点下面那条的选项，上面那条的输入框被填成『16:9 横屏·标红圆角边框开场』」的原因。

`clarify-send`（发送回答）走的是同一个全局选择器，所以**发送**也会读到别的消息的输入框。

### 1.2 修复：严格作用域定位

每个选项按钮、输入框、发送按钮都带上**所属消息的序号**，点击时从**被点击的元素自己**
沿 DOM 向上找自己的作用域容器：

```js
// 修复后 —— web/agent.js
function renderClarify(msg, msgIndex) {
  const q = msg.clarify;
  if (!q) return "";
  const mi = msgIndex == null ? 0 : msgIndex;
  const options = (q.options || []).map((opt, i) => `
    <button type="button" class="ag-clarify-opt" data-act="clarify-answer"
            data-msg-index="${mi}" data-value="${esc(opt)}">${esc(opt)}</button>`).join("");
  ...
  return `<div class="ag-block ag-clarify" data-clarify-scope="${mi}">
    ...
    <input type="text" class="ag-clarify-input" data-msg-index="${mi}" ...>
    <button type="button" class="ag-act" data-act="clarify-send"
            data-msg-index="${mi}">发送回答</button>
  </div>`;
}

// 找出「这个选项自己所属的那条澄清气泡」的输入框
function clarifyInputOf(btn) {
  if (!btn) return null;
  // 1) 优先：从按钮自身所在的 .ag-clarify 容器里取（最可靠 —— DOM 作用域）
  const scope = btn.closest(".ag-clarify");
  if (scope) {
    const field = scope.querySelector(".ag-clarify-input");
    if (field) return field;
  }
  // 2) 兜底：用 data-msg-index 定位
  const idx = btn.getAttribute("data-msg-index");
  if (idx != null && idx !== "") {
    const host = btn.closest("[data-msg-index]") || document;
    const field = host.querySelector(
      `.ag-clarify-input[data-msg-index="${CSS.escape(idx)}"]`);
    if (field) return field;
  }
  return null;
}
```

点击处理改为：

```js
} else if (act === "clarify-answer") {
  const value = btn.dataset.value || "";
  const field = clarifyInputOf(btn);          // ← 自己的作用域
  if (field) {
    field.value = value;
    field.focus();
    // 已选项高亮 + 本组其余选项禁用（只影响本组）
    const scope = btn.closest(".ag-clarify");
    if (scope) {
      scope.querySelectorAll(".ag-clarify-opt").forEach(opt => {
        opt.disabled = true;
        opt.classList.toggle("ag-clarify-opt-picked", opt === btn);
      });
    }
  }
} else if (act === "clarify-send") {
  const field = clarifyInputOf(btn);          // ← 自己的作用域
  ...
}
```

要点：

- **双重保障**：主路径靠 DOM 作用域（`closest(".ag-clarify")`），
  兜底靠 `data-msg-index` 序号 —— 即使将来 DOM 结构变了也不会串填。
- **自动聚焦**：`field.focus()` 保留，并且焦点落在**自己那条**的输入框（断言 a4）。
- **选项点击后清空/禁用本组**：已选项加 `.ag-clarify-opt-picked` 高亮，
  本组其余 `disabled`；**只影响本组**（作用域限定在同一个 `.ag-clarify` 容器内）。
- **样式**（`web/style.css`）：踩过一个 CSS 特异性坑 ——
  `.ag-clarify-opt:disabled` 特异性 (0,2,0) 高于 `.ag-clarify-opt-picked` (0,1,0)，
  会让「已选中」的按钮反而显示成最淡的（视觉上像禁用）。
  已改为 `:disabled:not(.ag-clarify-opt-picked)` + 双类名 `.ag-clarify-opt.ag-clarify-opt-picked`。

---

## 2. 需求 1.1：「新建交互」点击后无反应

### 2.1 根因

`web/agent_sessions.js` 的 `createSession()` 只做了**会话层**的事：

```js
// 修复前
async function createSession() {
  const data = await apiJSON("POST", "/api/agent/sessions", {});
  const sid = data.session_id || "";
  saveCurrentId(sid);
  state.messages = [];        // ← 只清了会话层自己的 state
  state.artifacts = [];
  await loadSessions();
  renderCurrentSessionLabel();
  renderArtifacts();
  setStatus("已新建交互。");
  return sid;
}
```

但**消息区**（`#agMessages`）的内容由 `agent.js` 的 `state.messages` 渲染，
`createSession` 从没让 `agent.js` 也切过去 —— 旧会话的消息**继续显示在页面上**。

实测（修复前，真实 Chromium）：

```
sid:  "" -> ""                        ← agent.js 的 sessionId 从没被更新
messages_before: 0 -> messages_after: 3   ← 旧消息一条没少
url:  http://127.0.0.1:38419/ -> http://127.0.0.1:38419/   ← URL 完全没变
```

所以用户看到的是「点了没反应」：会话确实建了（侧栏多了一条），
但主区域还是老样子，URL 也没跳。

另有一个隐性根因：`agent.js` 的 `state.sessionId` **只在发消息时被赋值**，
切会话那条路（`loadSession` → `restoreMessages`）**从不更新它** ——
这是需求 1.2「工具密度不一致」的另一半根因（见第 3 节）。

### 2.2 修复

**(a) 切视图**（`web/agent_sessions.js`）：

```js
function switchAgentView(sessionId) {
  const agent = window.AgentUI;
  if (!agent || typeof agent.restoreMessages !== "function") return;
  agent.restoreMessages([], { merge: false, sessionId: sessionId || "" });
}

async function createSession(options) {
  const opts = options || {};
  const switchView = opts.switchView !== false;
  ...
  saveCurrentId(sid);
  state.messages = [];
  state.artifacts = [];
  if (switchView) switchAgentView(sid);   // ← 关键：让消息区也切过去
  await loadSessions();
  ...
}
```

`switchView` 这个开关是**修复自身引入的回归**时加的：
`init()` 在「一个会话都没有」时会调 `createSession()` 建空会话，
如果那里也整段替换视图，会把 **init 还没跑完时用户刚发的那条消息抹掉** ——
正是 `restoreMessages` 的 merge 模式当初要防的竞态。
所以：**只有用户主动新建时**才切视图，init 里由 init 自己用 merge 模式接管。

**(b) 路由与选中态同步**（`web/agent_sessions.js`）：

本项目原本**没有任何 URL 路由**（`app.js` 里 `location.hash` 消费者为 0），
「跳转到新 session」在 URL 上无处可跳。补了一条**最小**路由：

```js
const ROUTE_PREFIX = "#/agent/";

function syncRoute(sessionId) {
  const sid = sessionId || "";
  const target = sid ? ROUTE_PREFIX + encodeURIComponent(sid) : ROUTE_PREFIX;
  try {
    if (location.hash === target) return;
    history.replaceState(null, "", target);   // replaceState：不堆历史陷阱
  } catch (e) {
    try { location.hash = target; } catch (e2) { /* 忽略 */ }
  }
}

function readRouteSessionId() {
  const hash = String(location.hash || "");
  if (!hash.startsWith(ROUTE_PREFIX)) return "";   // 只认自己的命名空间
  const raw = hash.slice(ROUTE_PREFIX.length);
  if (!raw) return "";
  try { return decodeURIComponent(raw); } catch (e) { return raw; }
}
```

- 挂在 `saveCurrentId()` 里 —— 它是「当前会话变了」的**唯一收敛点**
  （建会话 / 切会话 / 删会话都会经过它）。
- `init()` 恢复时 **URL 优先于 localStorage**（`readRouteSessionId() || readCurrentId()`），
  路由既然写了就必须能被读回来，否则刷新后路由指向 A、界面却显示 B。

---

## 3. 需求 1.2：工具密度切换 session 后不一致

### 3.1 根因（**两个**，缺一不可）

**根因 a：恢复路径与实时路径的 DOM 结构不同 —— 密度本身就不同**

`web/agent_sessions.js` 的 `messagesFromServer()` 把**每一条** `role=tool` 消息
各自 push 成**一个独立的 assistant 气泡**，每个气泡自带一个 `.ag-tools` 容器：

```js
// 修复前
if (m.role === "tool") {
  const note = (m.content || "").split(":").slice(1).join(":").trim();
  out.push({                       // ← 每条 tool 一个新气泡
    role: "assistant",
    content: "",
    tools: [{ name: ..., ok: ..., ms: null, arguments: null, result: ... }],
  });
  continue;
}
```

而实时路径（流式 `tool_call`）是**一条 assistant 消息带一个 tools 数组**，
渲染成**一个** `.ag-tools` 容器。

真实浏览器量测（同一段对话，2 个工具）：

| 路径 | 工具容器数 | 工具区总高 |
|---|---|---|
| 实时（流式 tool_call） | 1 | **104px** |
| 恢复（会话还原） | 2 | **142px** |

差 38px —— 这就是「当前 session 密度正常，切换别的 session 再切回来密度会变」。
`.ag-tools` 容器带 `margin-top:10px` + `padding:9px 11px` + border + 圆角，
每个容器都要付一遍这份边距。

**根因 b：`data-detail-key` 跨会话撞 key —— 折叠态串味**

```js
// 修复前
data-detail-key="m${mi}-tool-${i}"     // 只有消息序号
```

消息序号在**每个会话里都从 0 开始**，所以两个会话的第一条消息共享同一个 key
（`m0-tool-0`）。`captureDetailsState` / `restoreDetailsState` 于是把
**上一个会话**的折叠态贴到**新会话**的同名折叠块上。

实测（修复前）：在会话 A 展开 `m0-tool-0` → 切到会话 B → B 的 `m0-tool-0` **也是展开的**。
展开的折叠块一多，工具区自然就「变松」了 —— 密度观感随会话乱跳。

另外，折叠态原来只有一个**全局 map**：切会话时旧会话的 key 被清掉，
新会话渲染出来的 key 也是 `m0-tool-0` 但 map 里已没有 → 恢复不了 → 一律回默认展开态。

### 3.2 修复

**(a) tool 消息并入所属 assistant 气泡**（`web/agent_sessions.js`）：

```js
if (m.role === "tool") {
  const note = (m.content || "").split(":").slice(1).join(":").trim();
  const toolEntry = {
    name: m.tool_name || "(工具)",
    ok: note !== "error" && note !== "",
    ms: null, arguments: null,
    result: note || "ok",
  };
  const prev = out.length ? out[out.length - 1] : null;
  if (prev && prev.role === "assistant" && !prev._isUserTurnTail) {
    prev.tools = prev.tools || [];
    prev.tools.push(toolEntry);       // ← 并进它所属的那条 assistant 消息
  } else {
    out.push({ role: "assistant", content: "", tools: [toolEntry] });
  }
  continue;
}
```

这不只是「为了好看」—— 服务端的落库顺序本来就是
`assistant(答案) → tool → tool → ...`（见 `server.py` 的
`add_message("assistant", ...)` 后紧跟 `add_message("tool", ...)`），
工具轨迹本来就属于**那条 assistant 消息**。所以并入是**忠实还原**，不是凑数。
（`prev` 不是 assistant 时仍自开气泡，保证工具不丢。）

**(b) detail key 加会话作用域前缀**（`web/agent.js`）：

```js
function detailScope() {
  const sid = state.sessionId || state._ephemeralScope || "";
  if (!sid) {
    state._ephemeralScope = state._ephemeralScope
      || ("tmp-" + Math.random().toString(36).slice(2, 10));
    return state._ephemeralScope + "-";
  }
  return sid + "-";
}
// renderTools / renderSteps 里：
data-detail-key="${scope}m${mi}-tool-${i}"
data-detail-key="${scope}m${mi}-tools"
data-detail-key="${detailScope()}m${mi}-steps"
```

修复后实测 key：`sess-alpha-m1-tool-0` / `sess-bravo-m1-tool-0` —— 跨会话不再撞。

**(c) 折叠态按会话分桶**（`web/agent.js`）：

```js
const detailsStateBySession = new Map();

function currentDetailsBucket() {
  const key = state.sessionId || "__none__";
  if (!detailsStateBySession.has(key)) detailsStateBySession.set(key, new Map());
  return detailsStateBySession.get(key);
}

function captureDetailsState(root) {
  const map = new Map();
  if (!root) return map;
  root.querySelectorAll("details[data-detail-key]").forEach(d => {
    map.set(d.getAttribute("data-detail-key"), d.open);
  });
  const bucket = currentDetailsBucket();
  map.forEach((v, k) => bucket.set(k, v));
  return map;
}

function restoreDetailsState(root, map) {
  if (!root) return;
  const bucket = currentDetailsBucket();
  root.querySelectorAll("details[data-detail-key]").forEach(d => {
    const key = d.getAttribute("data-detail-key");
    if (bucket.has(key)) { d.open = bucket.get(key); return; }   // 桶优先
    if (map && map.has(key)) d.open = map.get(key);              // map 兜底
  });
}
```

「桶优先、map 兜底」的分工：
- **map**（本次重绘前从 DOM 抓的）→ 同一会话内的**重绘**（流式 token 到达、任务状态刷新）
  必须用它，否则用户展开的工具行会弹回去；
- **桶**（该会话历史上记住的）→ **切回该会话**时用它，
  否则离开时展开的工具行回到默认收起态 → 密度又变了。

**(d) 会话作用域的读取与切换**（`web/agent.js`）：

```js
function currentSessionId() {
  const sessions = window.AgentSessions;
  if (sessions && sessions.state) return sessions.state.currentId || "";
  return null;                       // 问不到（会话层还没加载）
}

function applySessionScope(nextSid) {
  if (nextSid === null || nextSid === undefined) return;   // 问不到 -> 不动
  const prev = state.sessionId || "";
  if ((nextSid || "") === prev) return;                    // 没变 -> 不动
  const box = $("#agMessages");
  if (box) captureDetailsState(box);   // 先把旧会话的折叠态存进旧桶
  state.sessionId = nextSid || "";
  state._ephemeralScope = "";
}
```

- **顺序很重要**：必须**先抓旧会话的折叠态、再改 sessionId**。
  反过来的话，DOM 里明明还是旧会话的折叠块，却会被存进新会话的桶 →
  切回来时恢复成别的会话的状态。
- **作用域从会话层读，不靠调用方传**：`agent_sessions.js` 在 `loadSession()` 里
  已经更新了 `state.currentId`，直接问它就行 —— 少一个
  「每个调用点都必须记得传 sessionId」的约定 = 少一个漏传导致串味的机会。
  `opts.sessionId` 仍被支持（测试与显式调用可覆盖）。
- `restoreMessages` 里调用 `applySessionScope(scopeSid)`。
- `newSession()`（agent.js 的「新会话」按钮）里也补了**清 sessionId 之前**先抓折叠态。

---

## 4. 验证（真实浏览器 / 真实 server.py）

### 4.1 验证环境

- **真实 Chromium**（Playwright，`--no-sandbox`），视口 1280×900
- **真实 `server.py`**（`tests/test_frontend_defaults.py` 的 `StudioServer`：
  独立端口 + 独立临时 data_dir + 真实 SQLite 会话库）
- 会话数据**直接写服务端 SQLite**（按 `server.py` 的落库形状构造），
  切换会话走**真实 UI**（点会话列表项），不是调内部函数
- 脚本：`evidence/verify_agent_ui.py`
- 证据目录：`evidence/evidence_verify/`

### 4.2 结果：**18/18 断言通过**（连续跑 3 次均全绿）

```
=== a) 快捷选项作用域定位 ===
  [PASS] a1 页面存在两组快捷选项  (共 5 个)
  [PASS] a2 两个澄清输入框初始为空  (['', ''])
    点击的选项: 16:9 横屏·标红圆角边框开场
    点击后输入框: ['', '16:9 横屏·标红圆角边框开场']
  [PASS] a3 只填了第二条消息的输入框
  [PASS] a4 焦点落在第二条消息的输入框  (index=1)
  [PASS] a5 已选项被高亮且本组其余禁用
  [PASS] a5b 已选中的按钮计算样式与其它按钮明显不同
  [PASS] a6 跨会话后仍不串填  (['', ''] -> ['', '16:9 横屏·标红圆角边框开场'])

=== b) 新建交互 -> 创建 + 跳转 ===
    URL: http://127.0.0.1:53965/#/agent/sess-alpha -> http://127.0.0.1:53965/#/agent/772822e9b95d410c
    sid: sess-alpha -> 772822e9b95d410c
    消息数: 2 -> 0
    选中: [] -> ['772822e9b95d410c']
  [PASS] b1 URL 变了（路由跳转到新 session）
  [PASS] b2 URL 指向新 session
  [PASS] b3 AgentUI.sessionId 切到新 session
  [PASS] b4 侧栏选中态切到新 session
  [PASS] b5 消息区已清空（旧会话消息不再残留）

=== c) 工具密度一致性 ===
    会话A 基准: toolBlocks=1 toolRows=2 totalToolAreaH=104 blockH=[104] rowH=[29,29]
    展开一行后: toolBlocks=1 toolRows=2 totalToolAreaH=135 blockH=[135] rowH=[60,29] rowOpen=[true,false]
    第 1 轮切回A: toolBlocks=1 toolRows=2 totalToolAreaH=135 blockH=[135] rowH=[60,29] rowOpen=[true,false]
    第 2 轮切回A: toolBlocks=1 toolRows=2 totalToolAreaH=135 blockH=[135] rowH=[60,29] rowOpen=[true,false]
    第 3 轮切回A: toolBlocks=1 toolRows=2 totalToolAreaH=135 blockH=[135] rowH=[60,29] rowOpen=[true,false]
  [PASS] c1 往返 3 次后工具区几何完全一致
  [PASS] c2 展开态被保留（切回来还是展开的）
  [PASS] c3 恢复路径与实时路径密度一致（同为 1 个工具容器）
  [PASS] c4 折叠态 key 带会话作用域（跨会话不撞）
  [PASS] d1 无 pageerror
  [PASS] d2 无 console error
```

### 4.3 修复前 / 修复后对照（同一套真实浏览器量测）

| 指标 | 修复前 | 修复后 |
|---|---|---|
| **A** 点第二条消息的选项后，两个输入框的值 | `['16:9 横屏·标红圆角边框开场', '']` ❌ 填错 | `['', '16:9 横屏·标红圆角边框开场']` ✅ |
| **B** 点「新建交互」后 URL | 不变（`http://…/`）❌ | `#/agent/<新 sid>` ✅ |
| **B** 点「新建交互」后 `AgentUI.state.sessionId` | `""` ❌ | 新 sid ✅ |
| **B** 点「新建交互」后消息区残留旧消息 | 2 条 ❌ | 0 条 ✅ |
| **C** 恢复路径 2 个工具的工具容器数 / 总高 | 2 个 / 142px ❌ | 1 个 / 104px ✅（与实时路径一致）|
| **C** 会话 A 展开工具行 → 切 B → 切回 A | `rowOpen=[true]`（串味）❌ | 恢复成离开前的样子 ✅ |
| **C** 折叠态 key 跨会话 | `m1-tool-0` 两会话相同 ❌ | `sess-alpha-…` / `sess-bravo-…` ✅ |

### 4.4 回归测试

```
# Agent 相关全量
715 passed, 1468 deselected, 8 subtests passed in 90.63s

# 会话/契约/前端相关
136 passed in 35.27s
```

其中 2 条**契约测试**（`tests/test_agent_session_search_cascade.py`）曾因我的改动
把 `opts.merge` 推出其 1400 字符文本窗口而失败。处理方式**不是削弱断言**，
而是把「merge 分支」代码提到 `restoreMessages` 靠前位置（语义等价、更好读），
并把长解释下沉到 `applySessionScope` 辅助函数。现在全部通过。

### 4.5 证据文件路径

| 文件 | 内容 |
|---|---|
| `evidence/evidence_verify/a-clarify-scoped.png` | 两条带选项的 Agent 消息；点第二条的选项后，**只有**第二条输入框被填，第一条仍是占位符 |
| `evidence/evidence_verify/a-cross-session.png` | 切换到另一个 session 后同样不串填 |
| `evidence/evidence_verify/b-new-session-jump.png` | 点「新建交互」后 URL 变成 `#/agent/<新 sid>`、消息区已清空、侧栏选中新会话 |
| `evidence/evidence_verify/c-density-stable.png` | 往返切换 3 次后的工具区（几何完全一致） |
| `evidence/evidence_verify/verify-summary.json` | 18 条断言的机器可读结果 |
| `evidence/evidence_verify/console.log` | console 记录（**空** = 无 error） |
| `evidence/evidence_verify/pageerrors.log` | pageerror 记录（**空** = 无 JS 异常） |
| `evidence/evidence_before/A-crossfill.json` | **修复前**取证：串填复现 |
| `evidence/evidence_before/B-new-session.json` | **修复前**取证：URL 不变 / sid 未更新 / 旧消息残留 |
| `evidence/evidence_before/A-before-fix-crossfill.png` | 修复前串填截图 |
| `evidence/evidence_before/B-before-fix-new-session.png` | 修复前新建交互无跳转截图 |
| `evidence/repro_before.py` | 修复前复现脚本（A/B/C） |
| `evidence/repro_faithful.py` | 走真实服务端会话库的忠实复现/验证脚本 |
| `evidence/diag_density.py` | 密度诊断脚本（实时路径 vs 恢复路径 + 跨会话折叠态泄漏） |
| `evidence/verify_agent_ui.py` | **正式验收脚本**（18 条断言） |

---

## 5. 未做 / 待确认

- **未部署**：按要求未合并主线、未部署 8190、未重启 `h3-studio.service`。
  合并与上线需 willis 拍板。
- **URL 路由是新增的最小实现**（`#/agent/<sid>`）：
  本项目原本没有路由体系。这条只覆盖 Agent 会话，且只认自己的命名空间，
  不影响 app.js 里既有的 `#job=xxx`（那个 hash 全仓仍无消费者，是既有问题）。
- **折叠态桶用内存 `Map` 而非 localStorage**：折叠态是临时 UI 状态，
  页面刷新后回到默认展开态是合理语义。若需要跨刷新保留，可另行提出。
- **服务端 `meta` 未存 `clarify`**：澄清态（快捷选项）目前只在**当前会话的内存**里，
  刷新后不会恢复（会话还原时 `messagesFromServer` 不产出 `msg.clarify`）。
  这是**既有行为**，本次未改动，也不在本次三个需求范围内；
  如需「刷新后仍可点快捷选项」，需要后端在 `meta` 里补存 clarify 字段 —— 可另开一条。
