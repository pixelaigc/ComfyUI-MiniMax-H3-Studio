# 内置多模态 Agent —— 前端增强

> 仓库：ComfyUI-MiniMax-H3-Studio
> 分支：`local/feature/agent-enh-frontend`（基线 `local/feature/builtin-agent` @ `17c9adb`）
> 范围：**仅前端**。`h3studio/` 下的 Python 后端文件一行未改（后端由另一条分支负责）。
>
> 本文档记录：改了什么、为什么这么改、实测证据在哪、哪些是「按契约实现、待后端联调」。

---

## 0. 一页速查

| 交付项 | 状态 | 关键文件 |
|---|---|---|
| 1. 流式输出（SSE + 优雅降级） | ✅ 按新契约实现 + 降级链已实测 | `web/agent.js` |
| 2. 工具调用可视化（可折叠） | ✅ 已实测截图 | `web/agent.js` |
| 3. 任务卡与 Agent 联动 + token 失效修复 | ✅ 根因已定位，机制已换，已实测 | `web/agent.js` |
| 4. 窄屏适配（375/390/1280） | ✅ 三档实测，零溢出零 console 错误 | `web/style.css`、`web/responsive.css` |
| 5. 交互健壮性（错误/重试/澄清/滚动） | ✅ 已实测 | `web/agent.js` |
| 6. 媒体语义校正（9 mode / 实际生效值 / 静默回落） | ✅ 已实测 | `web/agent-media.js`（新增） |

---

## 1. 流式输出

### 1.1 事件契约

新契约：`POST /api/agent/chat?stream=1`，SSE 事件类型：

| 事件 | 数据 | 前端行为 |
|---|---|---|
| `token` | `{text}` | **追加**到气泡正文（增量，不是覆盖） |
| `tool_call` | `{name, arguments, call_id?}` | 新建/更新工具轨迹条目（显示「进行中」） |
| `tool_result` | `{name, result, ok, ms?, error?, call_id?}` | 补上结果摘要 + 成功/失败态 |
| `job` | `{job_id, mode, label?, status?, prompt?, params?, summary?}` | 渲染/更新任务卡 |
| `done` | `{answer?, jobs?, assets?, prompt?, steps?, status?}` | 收尾：用 `answer` 覆盖正文，补齐 jobs/assets/prompt |
| `error` | `{error, code?}` | 渲染错误块 + 重试入口 |

老契约（基线后端已有）：`POST /api/agent/chat/stream`，事件 `progress` / `result` / `error`。

### 1.2 降级链

```
新契约 SSE  ──失败(端点不存在)──▶  老契约 SSE  ──失败──▶  一次性 JSON
```

**降级只在「端点不存在 / 不支持流式」时发生**（`shouldFallback()`）：

| 情况 | 是否降级 | 理由 |
|---|---|---|
| HTTP 404 / 405 / 415 / 501 | ✅ 降级 | 端点不存在或不支持流式 |
| 无 code、无 status 的网络错误 | ✅ 降级 | 代理/网络层不支持流式 |
| 带业务 `code`（`provider_error` / `too_many_requests` / `agent_internal_error` / `bad_request` …） | ❌ **不降级** | 服务端真的执行失败了 |
| HTTP 5xx 带 code | ❌ **不降级** | 同上 |

> **这条是踩坑换来的。** 早先版本对所有错误都降级，结果「服务端炸了」会先失败 → 降级到老契约 → 再降级到一次性 JSON，最后可能「成功」返回。用户看到「成功」但任务一个都没建成 —— **静默失败，最难查**。
> 回归测试：`tests/test_agent_frontend_enh.py::test_business_failure_is_not_masked_by_fallback`（断言只发出 1 次请求）与 `::test_endpoint_missing_falls_back_to_legacy_stream`（断言 404 时降级且能渲染）。

### 1.3 增量渲染（性能）

`token` 事件走**轻量路径**：只改文本节点的 `textContent`。
`tool_call` / `tool_result` / `job` / `done` 走**结构路径**：只重建**正在流式的那一条气泡**。

两条路径都不动其它气泡的 DOM 节点。判据见回归测试
`test_long_conversation_does_not_rebuild_every_node`：给第 0 条气泡打标记，走真实增量路径后标记必须还在（还在 = 节点没被换掉 = 走的不是全量重绘）。

> 早先版本每来一个 token 就 `innerHTML =` 重建整个消息列表 —— O(消息数) 的 DOM 操作。几百条消息时会把主线程打满，表现为打字卡顿 / 滚动掉帧，而且会连带干掉用户正在聚焦的澄清输入框。

### 1.4 折叠态保持

任何重绘都会重建 `innerHTML`，`<details>` 的 `open` 是**元素状态**，重建后默认收起 —— 用户刚展开的工具轨迹会在下一个 token 到来时「弹回去」。

解法：`captureDetailsState()` / `restoreDetailsState()`，用 `data-detail-key` 做键。
键里必须带**消息序号**（`m<序号>-tool-<序号>`），否则不同消息里的 `tool-0` 会撞成同一个 key，展开 A 消息的工具会把 B 消息的也一起展开。

---

## 2. 工具调用可视化

每个工具调用渲染成一块可折叠的 `<details>`：

- **工具名**（`search_assets` / `create_job` / …）
- **成功/失败态**：`✓`（绿）/ `✕`（红）
- **关键参数**：`query=雨夜 · mode=t2v`
- **结果摘要**：JSON 摘要，超 240 字符截断
- **耗时**：`42ms`

容器「工具调用 (N)」默认展开 —— 折起来等于把「Agent 干了什么」藏起来。

> **去重**：`renderTools`（流式驱动，有参数/结果/成功态）与 `renderSteps`（一次性结果驱动，只有工具名+耗时）展示的是同一件事。同时渲染会出现「工具调用 (2)」和「工具调用轨迹 (2)」两个几乎一样的块 —— **这个问题是看截图发现的**，不是读代码发现的。
> 现在：有 `msg.tools` 就只渲染它，没有才退回 `msg.steps`。
> 回归测试：`test_tool_container_is_open_and_trace_is_not_duplicated`。

---

## 3. 任务卡与 Agent 联动 —— 重点：token 失效根因

### 3.1 根因（实测，不是推测）

`.model-run-token` 是 **JWT，TTL = 1 小时**：

```
alg: HS256
payload: {sub:"1", username:"admin", role:"super_admin",
          type:"access", aud:"hermes-studio",
          iat: 1794258040, exp: 1794261640}     ← exp = iat + 3600
```

Web UI 启动时把当时的 access token 写进 `profiles/<profile>/.model-run-token`，**此后不再刷新**。实测本机 9 个 profile：

| profile | 剩余有效期 | 状态 |
|---|---|---|
| coding | 54.8 min | 未过期 |
| media | 55.0 min | 未过期 |
| research | 55.0 min | 未过期 |
| default | −2402.8 min | **已过期** |
| av | −5103.2 min | **已过期** |
| writing | −1404.9 min | **已过期** |
| ops | −5355.8 min | **已过期** |
| game | −3616.4 min | **已过期** |
| kuaishou | −1265.5 min | **已过期** |

6/9 已过期。

**消费点**：`hermes-web-ui/bin/ekko-studio-mcp.mjs:104` 的 `readProfileToken()` 读这个文件作为 MCP 请求的 bearer token。token 过期后：

1. MCP 工具路径必然失败（除 schema 注入 `item` 的老问题外，多一层 401）；
2. 通过它驱动的**任务卡状态永远不更新** —— 卡片停在初始状态，用户以为任务死了。

### 3.2 修复机制（不依赖那条链路）

**服务端下发 job id + 前端轮询工作台自己的任务状态接口。**

```
Agent 下发任务
   └─▶ 前端拿到 job_id
          └─▶ 轮询 GET /api/jobs/{id}        ← 工作台自己的 SQLite，同一份状态
                 ├─ 无 token
                 ├─ 无过期
                 └─ 轮询到终态（done/failed/canceled）自动停
```

- 轮询间隔 3 秒；只轮询**未终态**的任务；
- 任务卡上另有「刷新」按钮，用户可立即手动拉一次；
- 产物出来后自动渲染缩略图（图片 `<img>` / 视频 `<video>` / 音频链接）。

**为什么不复用 Studio 任务卡**：那条链路要过 `.model-run-token`（1 小时即失效），且 MCP 侧还有 schema 注入 `item` 的独立问题。前端不背这个依赖。

**硬约束（写进回归测试）**：`web/agent.js` 的**代码**（去注释后）里不得出现 `model-run-token` 或 `Authorization`。
> 注释里解释「为什么不用 token」是必要的，所以静态扫描必须先剥注释 —— 判据函数 `strip_comments()` 在 `tests/test_agent_frontend_enh.py` 里，且它本身处理了模板字符串、正则字面量、字符串字面量三种「引号不是字符串起点」的坑。

**实测证据**：`test_job_card_status_updates_from_polling` —— 喂一条 `queued` 任务，轮询后变 `running`（卡片显示「生成中」），再轮询到 `done`（显示「已完成」并渲染出产物缩略图）。

---

## 4. 窄屏适配

三档（375 / 390 / 1280）实测，**横向溢出 0 px，无越界元素，输入框均可达，console 零输出**。

窄屏新增规则（`web/responsive.css` 的 `@media (max-width: 700px)` 段）：

| 选择器 | 原因 |
|---|---|
| `.ag-tool-head { flex-wrap: wrap }` | 长工具名 + 耗时不能挤在一行 |
| `.ag-tool-args code, .ag-tool-result code { display:block; max-width:100% }` | 长参数/结果必须换行，不能撑出横向滚动条 |
| `.ag-clarify-reply { flex-direction: column }` | 澄清输入框 + 按钮在窄屏改成上下排 |
| `.ag-clarify-opt { white-space: normal }` | 长选项不能被省略号截断 |
| `.ag-job-thumbs .ag-job-thumb { 56px }` | 缩略图缩小，避免换行挤压 |

---

## 5. 交互健壮性

### 5.1 错误与重试

- 失败渲染 `.ag-error` 块：可读错误文案 + **「重试」按钮**。
- 重试的请求体挂在**消息自己**身上（`msg.retryRequest`），不是全局 `state`。
  > 全局的话：先失败一次、再成功一次之后，早先那条失败消息的重试按钮会在下次重绘时消失 —— 用户明明还想重试它。
- 点重试会把上次那条消息填回 `#agInput` 再发。
  > `send()` 从 `#agInput` 读内容，重试时输入框已被清空 —— 不填回去就是发一条空消息。

### 5.2 澄清式提问

`status === "needs_clarification"` → 渲染成**可回答的提问气泡**：
问题文案 + 可选快捷项（`clarify_options`）+ 自由输入框 + 发送按钮。
点快捷项会把答案填进输入框（可改后再发）。

### 5.3 滚动

- 只有**用户本来就贴着底部**时才自动跟随；向上翻阅历史不会被强行拽回底部。
- 回归测试 `test_scrolling_only_follows_when_at_bottom`。

---

## 6. 媒体语义校正（按《Agent 媒体语义与提示词策略》基线）

新增 `web/agent-media.js` —— 前端侧的模式/参数语义真源。

### 6.1 模式白名单只有 9 个

```
t2v / flf / r2v / t2i / t2i_qwen / i2i_qwen / t2i_moss / t2a / a2a
```

`longfilm` **不在其中** —— 它走 `POST /api/longfilms` 自有链路，不在 `server.JOB_MODES` 里，Agent 的 `create_job` 建不了（基线 Agent 曾误报 longfilm）。

处理：
- `AGENT_MODES` 只列 9 个；
- `MANUAL_ONLY_MODES.longfilm` 标注「Agent 不能下发」，标签带「（Agent 不能下发）」；
- `resolveServerModes()` 从 `GET /api/generation-types` 拉清单时**剔除 longfilm**（`gen_types.GENERATION_TYPES` 里登记了它，但 Agent 用不了）；
- `list_modes` 工具结果展示时也过滤掉 longfilm。

### 6.2 只展示「实际生效值」，静默回落必须可见

| 情况 | 代码事实 | 卡片上的表现 |
|---|---|---|
| 视频分辨率只支持 544p / 768p | `RES_MP = {"544p":0.4,"768p":0.8}`（workflows.py:63）；`RES_MP.get(x, 0.8)` 静默落 768p（:320/:409） | 显示「分辨率 **768p**」+ 橙色高亮 + 警告条「视频分辨率只有 544p / 768p，请求 1080p 实际按 768p 生成」 |
| 音乐时长字段是 `max_duration` | `params.get("max_duration", 60.0)`（music_yue2.py:328），Agent 传的 `duration` 不生效 | 显示「时长 **60 秒**」+ 警告条「音乐时长字段是 max_duration，Agent 传的 duration=120 不生效，实际按默认 60 秒生成」 |
| 音乐时长钳制 | `[YUE2_MIN_DURATION=4, YUE2_MAX_DURATION=360]` | 超出收敛到 4 / 360 |
| 比例不在 8 个之内 | `ASPECT_MAP` 只有 8 个 | 回落 16:9 + 警告 |

**「默认值」与「回落」是两回事**：
- 没传 `aspect` → 用默认 16:9，这是**默认**，不报警；
- 传了非法的 `aspect` → 回落 16:9，这是**回落**，必须报警。

### 6.3 不展示 Agent 传不了的参数

Agent 的 `create_job` 只能写 `prompt` / `aspect` / `resolution` / `duration` / `batch_count` / `ref_images` / `asset_refs`（agent.py:501-556）。

模型版本 / LoRA / 采样参数 / 负面词 / seed / sampler 等是**官方默认值** —— 展示成「用户设的」就是误导。`DISPLAYABLE_PARAMS` 白名单里没有它们。

### 6.4 能力缺口给替代建议

Agent 的素材槽位恒为 `ref_image`（agent.py:534 写死），所以：

| 模式 | 缺口 | 卡片上的建议 |
|---|---|---|
| `a2a`（参考生音乐） | 服务端只认 `slot="ref_audio"` | 请到音乐面板手动选参考音频提交，或先用 Agent 生成风格稿（`t2a`） |
| `flf`（首尾帧） | Agent 没有首帧/尾帧通道 | 请到视频面板上传首帧（或改用 `r2v` 参考图生成） |
| `longfilm` | 走 `/api/longfilms` | 请到工作台「长片拼接」页手动提交 |

---

## 7. 验证

### 7.1 前端单测

| 文件 | 用例数 | 覆盖 |
|---|---|---|
| `tests/test_agent_frontend_enh.py` | 31 | 流式事件渲染（token 增量 / done / error / 老契约）、工具调用折叠与去重、任务卡渲染与轮询更新、错误与重试、澄清气泡、降级链分类、增量渲染不重建、滚动跟随 |
| `tests/test_agent_media_semantics.py` | 18 | 9 mode 白名单、longfilm 剔除、视频分辨率回落、音乐 max_duration、时长钳制、参数展示白名单、能力缺口建议 |

运行：
```bash
python3 -m pytest tests/test_agent_frontend_enh.py tests/test_agent_media_semantics.py -v
```

### 7.2 真实浏览器证据

```bash
python3 scripts/verify_agent_frontend_enh.py 8471 <证据目录>
```

脚本自起**独立端口**的 Studio 实例（临时数据目录，跑完即删），
**不碰 8190 线上服务，不碰任何 systemd unit**。

产出：三档宽度截图 + 每档 console 全量输出 + 横向溢出实测 + 输入框可达性。

### 7.3 全量回归

`python3 -m pytest tests/ -q`，与基线 `17c9adb` 对比失败名单，确认零新增回归。
（对比结果见交付汇报。）

---

## 8. 遗留项（按契约实现，待后端联调）

| # | 项 | 说明 |
|---|---|---|
| 1 | **新流式契约未接真后端** | 基线后端只有 `POST /api/agent/chat/stream`（`progress`/`result` 事件）。本分支按任务书的新契约（`POST /api/agent/chat?stream=1` + `token`/`tool_call`/`tool_result`/`job`/`done`/`error`）实现了客户端，**后端分支需提供该端点**。当前跑在基线后端上会自动降级到老契约，功能正常（已实测）。 |
| 2 | **`job` 事件的字段名** | 前端接受 `job_id` 或 `id`，`label` 可选（缺省时按 mode 查 `AgentMedia.AGENT_MODES`）。后端下发时建议带 `label`。 |
| 3 | **澄清提问的选项字段** | 前端读 `clarify_options`（数组）与 `clarify_hint`（字符串）。后端若用别的字段名，需要对齐。 |
| 4 | **`needs_clarification` 的选项生成** | 前端只负责渲染；**选项从哪来**（模型给？后端推断？）需后端定。 |
| 5 | **任务卡轮询间隔 3 秒** | 前端常量 `JOB_POLL_INTERVAL`。若后端有 SSE 推送（`/api/status/stream` 已在仓库里，但只推全局队列状态，不带 job_id），可以改成推送 —— 但那需要后端加按 job 的事件源。 |
| 6 | **产物缩略图** | 依赖 `GET /api/jobs/{id}` 返回的 `outputs[{kind,file,name}]`。已按基线 `row_to_job` 的实际结构实现。 |
| 7 | **`web/agent.js` 未改 `app.js`** | 按分工约束只动 `web/agent.js` + 新增 `web/agent-media.js` + `web/style.css` / `web/responsive.css` / `web/index.html`（仅加一行 script 标签）。**未改 `web/app.js`**，零冲突风险。 |

---

## 9. 改动清单

| 文件 | 类型 | 说明 |
|---|---|---|
| `web/agent.js` | 改 | 流式客户端 + 降级链 + 增量渲染 + 工具可视化 + 任务卡轮询 + 错误/重试 + 澄清气泡 |
| `web/agent-media.js` | **新增** | 媒体语义真源（9 mode / 实际生效值 / 静默回落 / 能力缺口） |
| `web/style.css` | 改 | 工具块 / 任务卡 / 澄清气泡 / 回落高亮的样式 |
| `web/responsive.css` | 改 | 窄屏（≤700px）新组件规则 |
| `web/index.html` | 改 | 加一行 `<script src="agent-media.js">`（须在 agent.js 之前） |
| `tests/test_agent_frontend_enh.py` | **新增** | 31 个前端用例 |
| `tests/test_agent_media_semantics.py` | **新增** | 18 个媒体语义用例 |
| `scripts/verify_agent_frontend_enh.py` | **新增** | 真实浏览器证据采集脚本 |

**未改**：`h3studio/**`（Python 后端）、`web/app.js`、`server.py`、任何 systemd unit。
