# 内置多模态 Agent —— 前端增强

> 仓库：ComfyUI-MiniMax-H3-Studio
> 分支：`local/feature/agent-enh-fix`（合并 `agent-enh-frontend` + `agent-enh-backend`）
> 基线：`local/feature/builtin-agent` @ `17c9adb`
>
> 本文档记录：改了什么、为什么这么改、实测证据在哪、哪些是「按契约实现、待后端联调」。
>
> **2026-11-10 三轮综合审查后修订**（详见 §9「审查问题修复记录」）：
> ① 流式开关从 query 改到 **body**（原来两边对不上，流式 100% 不生效）
> ② 加**契约归一化层**（后端 `tool`/`args`/`summary` → 前端 `name`/`arguments`/`result`）
> ③ 澄清字段**多字段兼容** + 流式路径也构造澄清气泡
> ④ 一次性路径真的用 `steps[].args`（原来硬编码 `arguments: null`）
> ⑤ 能力位改由**后端下发**（原来硬编码「a2a/flf 建不了」，合并后变反向阻断）
> ⑥ 音乐卡**不渲染 aspect**（原来渲染无意义的「比例 16:9」）

---

## 0. 一页速查

| 交付项 | 状态 | 关键文件 |
|---|---|---|
| 1. 流式输出（SSE + 优雅降级） | ✅ **已修**（审查后 stream 载体对齐 + 跨侧测试锁死） | `web/agent.js` |
| 2. 工具调用可视化（可折叠） | ✅ **已修**（审查后参数/结果真的渲染出来了） | `web/agent.js` |
| 3. 任务卡与 Agent 联动 + token 失效修复 | ✅ 根因已定位，机制已换，已实测 | `web/agent.js` |
| 4. 窄屏适配（375/390/1280） | ✅ 三档实测，零溢出零 console 错误 | `web/style.css`、`web/responsive.css` |
| 5. 交互健壮性（错误/重试/澄清/滚动） | ✅ **已修**（审查后澄清快捷选项真的出现） | `web/agent.js` |
| 6. 媒体语义校正（9 mode / 实际生效值 / 静默回落） | ✅ **已修**（审查后能力位由后端驱动、音乐卡不渲染比例） | `web/agent-media.js`（新增） |

---

## 1. 流式输出

### 1.1 事件契约

新契约：`POST /api/agent/chat`，**请求体带 `stream: 1`**，SSE 事件类型：

| 事件 | 数据 | 前端行为 |
|---|---|---|
| `token` | `{text}` | **追加**到气泡正文（增量，不是覆盖） |
| `tool_call` | `{name, arguments, call_id?}` | 新建/更新工具轨迹条目（显示「进行中」） |
| `tool_result` | `{name, result, ok, ms?, error?, call_id?}` | 补上结果摘要 + 成功/失败态 |
| `job` | `{job_id, mode, label?, status?, prompt?, params?, summary?}` | 渲染/更新任务卡 |
| `done` | `{answer?, jobs?, assets?, prompt?, steps?, status?, clarify_options?, clarify_hint?}` | 收尾：用 `answer` 覆盖正文，补齐 jobs/assets/prompt |
| `error` | `{error, code?}` | 渲染错误块 + 重试入口 |

老契约（基线后端已有）：`POST /api/agent/chat/stream`，事件 `progress` / `result` / `error`。

#### 1.1.1 流式开关的载体（2026-11-10 审查修复）

**原来**：前端发 `POST /api/agent/chat?stream=1`（query 载体），
而后端只读 `body.stream` —— 两边对同一个参数的载体认知不同，
真实浏览器 12/12 请求拿到的都是 `application/json`，
**流式输出 100% 不生效**（界面从空串直接跳到整段结果）。

**现在**：前端把 `stream` 放进 **body**（`{message, session_id, stream: 1}`），
后端**同时**兼容 query（`?stream=1`）—— 两种写法命中同一条 SSE 契约。

> 后端兼容 query 不是冗余：已部署的旧前端发的是 query 载体，
> 只改前端会让旧前端继续坏。

#### 1.1.2 契约归一化层（2026-11-10 审查修复）

前端在 `applyStreamEvent` 之前加了一层**归一化适配**，
把后端字段名映射成前端内部字段：

| 内部字段 | 优先读 | 回退读 |
|---|---|---|
| `name` | `data.name` | `data.tool` |
| `arguments` | `data.arguments` | `data.args` |
| `result` | `data.result` | `data.summary` → `data.output` |

**为什么需要**：后端发 `{tool, ok, summary}`，前端原来读
`result`/`output`/`error`/`ms`/`call_id` —— 字段对不上，
工具结果**永远渲染不出来**、失败原因不显示、耗时不显示、
`call_id` 恒 null（同名工具并发会串卡）。

归一化层把差异**收敛在一处**，渲染层不用知道后端叫什么；
前端对**两种**命名都认（向后兼容更老的服务端）。

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

#### 5.2.1 澄清字段的多字段兼容（2026-11-10 审查修复）

**原来**：后端只发 `clarification`（一个字符串），
前端读 `clarify_options` / `clarify_hint` —— 契约对不上，
快捷选项 **100% 落空**（审查截图：蓝色区块里只有输入框，没有任何选项 chip）。

**现在**：

| 用途 | 优先读 | 回退读 |
|---|---|---|
| 问题文本 | `data.clarification` | `data.answer` |
| 快捷选项 | `data.clarify_options` | `data.options` |
| 提示 | `data.clarify_hint` | `data.hint` |

后端现在**同时**产出三个字段（见《接口契约》§1.2.1）。

#### 5.2.2 流式路径也要构造澄清气泡（2026-11-10 审查修复）

**原来**：构造澄清气泡的逻辑只在 `send()` 的**一次性 JSON 路径**里，
流式路径（`applyStreamEvent` 的 `done` 分支）完全没有 ——
于是走 SSE（也就是正常路径）时气泡**根本不出现**。

**现在**：逻辑提到 `buildClarify(data)`，两条路径共用；
后端的 `done` 事件也带上 `clarify_options` / `clarify_hint` / `clarification`。

> 这一条是**跨侧契约测试实测暴露**的 —— 审查清单里没有它，
> 是写跨侧测试时才发现的第 6 处断裂。

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

#### 6.2.1 音乐模式**不渲染比例**（2026-11-10 审查修复）

`t2a` / `a2a` 是**纯音频**任务，没有画面比例这回事
（后端 `mode_schema("t2a")["params"]` 里压根没有 `aspect`）。

**原来**：`resolveAspect` 没有音频守卫，无条件返回 `"16:9"` ——
于是参考生音乐的任务卡上渲染出「比例 16:9」这种**无意义**的参数，
而且那个值根本不是 Agent 设的。

**现在**：`resolveAspect` 对音频模式返回空值，`effectiveParams` 据此不渲染这一项。

### 6.3 不展示 Agent 传不了的参数

模型版本 / LoRA / 采样参数 / 负面词 / seed / sampler 等是**官方默认值** ——
展示成「用户设的」就是误导。`DISPLAYABLE_PARAMS` 白名单里没有它们。

> **2026-11-10 审查修复**：原来还有一份 `AGENT_SETTABLE_PARAMS`
> （「Agent 只能传 prompt/aspect/resolution/duration/batch_count/ref_images/asset_refs」），
> 注释说「事实：agent.py:501-556 只写入下列键」。
> 那条在**基线 7 参数时代**成立，后端扩到 23 参数后已过期，
> 而且这份清单**全仓零消费点**（web/ 与 tests/ 引用数都是 0）——
> 纯死代码 + 纯过期事实。现在**删除**，真实能力由服务端下发。

### 6.4 能力缺口给替代建议（**已改为后端驱动**）

**原来**：前端硬编码 `UNSUPPORTED_BY_AGENT = {a2a, flf}`，
理由是「Agent 的素材槽位恒为 `ref_image`（agent.py:534 写死）」。

**问题**：那条在旧基线上成立，但后端增强分支已把槽位改成分派制
（`agent_schema.MODE_SLOTS` / `mode_slot_plan`）：
`a2a → ref_audio`、`flf → first_frame`（+ `last_frame`）。
合并后前端那条提示变成**反向阻断** ——
后端明明能建，任务卡上却写「Agent 建不了，请到面板手动提交」。

**现在**：能力位**由后端下发**（`GET /api/generation-types` 的
`agent_asset_slots`），前端只渲染服务端给的结论：

| 情况 | 前端行为 |
|---|---|
| 服务端下发了槽位（`a2a` / `flf` / `r2v` / `i2i_qwen`） | **不提示**（Agent 有素材通道） |
| 服务端说「没有通道」但任务带了素材引用 | 提示「这次带的素材引用放不进去」+ 替代建议 |
| 服务端没下发（老服务端） | **不提示**（宁可少提示，也不要反向阻断） |
| `longfilm`（走 `/api/longfilms`） | 提示「请到工作台「长片拼接」页手动提交」 |

---

## 7. 验证

### 7.1 前端单测

| 文件 | 用例数 | 覆盖 |
|---|---|---|
| `tests/test_agent_frontend_enh.py` | 31 | 流式事件渲染（token 增量 / done / error / 老契约）、工具调用折叠与去重、任务卡渲染与轮询更新、错误与重试、澄清气泡、降级链分类、增量渲染不重建、滚动跟随 |
| `tests/test_agent_media_semantics_frontend.py` | 21 | 9 mode 白名单、longfilm 剔除、视频分辨率回落、音乐 max_duration、时长钳制、参数展示白名单、**能力位由后端下发**、**音乐卡无 aspect** |

> **文件名**：原 `tests/test_agent_media_semantics.py` 与后端分支**同名**
> （合并必冲突，6 处冲突标记）。已拆为
> `test_agent_media_semantics_backend.py`（后端 87 例）+ `test_agent_media_semantics_frontend.py`（前端 21 例），
> 两侧用例全部保留，且无同名测试类。

运行：
```bash
python3 -m pytest tests/test_agent_frontend_enh.py \
                 tests/test_agent_media_semantics_frontend.py -v
```

### 7.1.1 跨侧契约测试（**本次审查最关键的补强**）

`tests/test_agent_contract.py`（19 条）—— **测试数据来自真实后端产出，
断言打在真实前端代码上**：

| 契约 | 用例 | 数据来源 | 断言落点 |
|---|---|---|---|
| ① stream 载体 | 3 | 真起 `server.make_app()` + 前端真实请求形态 | `Content-Type` 含 `text/event-stream` |
| ② tool_result 字段 | 5 | 真 Agent 编排产出的 SSE 事件 | 真实 Chromium 里的 DOM |
| ③ 澄清字段 | 5 | 真 Agent 产出的澄清结果 | DOM 里必须出现选项 chip |
| ④ steps[].args | 3 | 真 Agent 产出的 `steps[]` | DOM 里必须出现「参数」行 |
| ⑤ 音乐 aspect | 3 | 真 `mode_schema("t2a")` + 真任务卡 | 音乐卡不得渲染「比例」 |

**为什么这样写**：审查实测 5 处契约断裂，**两侧测试各自全绿** ——
因为没有任何一条测试跨越前后端两侧（假覆盖）。
本文件的三条纪律就是为了根治它：
1. 测试数据必须来自**真实后端产出**（真起 app、真跑 Agent）；
2. 断言必须打在**真实前端代码**（真 Chromium + 真 `web/agent.js`）；
3. 端到端那条用**前端真实请求形态**打**真实 `server.make_app()`**。

**变异测试实测**（回退每个修复 → 对应命名断言变红）：

| 变异 | 变红的断言 |
|---|---|
| 后端不读 query 的 stream | `test_frontend_query_stream_request_returns_event_stream` |
| 后端不发 `call_id` | `test_backend_tool_result_carries_call_id` |
| 后端不产 `clarify_options` | `test_backend_result_has_clarify_fields` 等 3 条 |
| 前端不读 `summary` | `test_frontend_consumes_backend_tool_result_payload` 等 2 条 |
| 前端丢弃 `steps[].args` | `test_frontend_uses_steps_args` 等 2 条 |
| 前端音乐 aspect 无守卫 | `test_frontend_music_card_has_no_aspect` |

### 7.2 真实浏览器证据

```bash
python3 scripts/verify_agent_frontend_enh.py 8471 <证据目录>
```

脚本自起**独立端口**的 Studio 实例（临时数据目录，跑完即删），
**不碰 8190 线上服务，不碰任何 systemd unit**。

产出：三档宽度截图 + 每档 console 全量输出 + 横向溢出实测 + 输入框可达性。

实测结果（三档全部）：横向溢出 **0 px**、越界元素 **0 个**、输入框**可达**、console **0 条**。

### 7.3 任务卡实时更新 —— 端到端证据

```bash
python3 scripts/verify_jobcard_live_update.py 8473 <证据目录>
```

**没有任何桩**：真实 `server.py` + 真实 `web/` 前端 + 真实 `POST /api/jobs` 建任务 +
真实 `GET /api/jobs/{id}` 轮询。唯一「注入」的是把服务端真实存在的 `job_id`
交给前端（模拟 Agent 把 job_id 交给前端的那一步），之后状态更新全部由前端自己轮询完成。

实测结果（11/11 断言全绿）：

| 阶段 | 卡片徽标（DOM 实测） | 服务端 jobs 表 status |
|---|---|---|
| 初始 | `排队中` | `queued` |
| 运行中 | `生成中`（+ 进度「采样中 45%」） | `running` |
| 完成 | `已完成`（+ 产物缩略图，`naturalWidth > 0`） | `done` |

### 7.4 全量回归

`python3 -m pytest tests/ -q`，与基线 `17c9adb` 对比失败名单。

> **本轮的回归对比必须与「合并后未修复」视图比，不能与单侧分支比** ——
> 两条分支各自的全量回归数据来自**不同工作树**，不能直接相加。
> 详见《修复汇报》§回归对比。

---

## 8. 遗留项（**2026-11-10 审查后已清零**）

原前端分支登记的 7 条遗留项里，与「后端未实现契约」相关的 4 条
**已在本轮合并 + 修复中全部关闭**：

| # | 原遗留项 | 现状 |
|---|---|---|
| 1 | 新流式契约未接真后端 | ✅ **已关闭**。后端分支已提供该端点；本轮把 `stream` 载体对齐（body + query 双读），跨侧测试锁死 |
| 3 | 澄清提问的选项字段 | ✅ **已关闭**。后端现在产出 `clarify_options` / `clarify_hint`，前端多字段兼容读取 |
| 4 | `needs_clarification` 的选项生成 | ✅ **已关闭**。模型给（`respond` 新增入参）→ 后端按真实能力表兜底推断 → 前端渲染 |
| — | （新增）流式路径不构造澄清气泡 | ✅ **已关闭**。逻辑提到 `buildClarify()`，两条路径共用；`done` 事件带澄清字段 |

仍然存在的遗留项（**不是本轮范围**）：

| # | 项 | 说明 |
|---|---|---|
| 5 | **任务卡轮询间隔 3 秒** | 前端常量 `JOB_POLL_INTERVAL`。若后端加按 job 的事件源，可以改成推送 |
| 6 | **产物缩略图** | 依赖 `GET /api/jobs/{id}` 返回的 `outputs[{kind,file,name}]` |
| 7 | **任务卡轮询无终止条件** | 单个任务查不到会永久轮询（审查「重要 1」）。本轮未修 —— 属后端契约之外的独立问题 |

---

## 9. 改动清单

| 文件 | 类型 | 说明 |
|---|---|---|
| `web/agent.js` | 改 | 流式客户端 + 降级链 + 增量渲染 + 工具可视化 + 任务卡轮询 + 错误/重试 + 澄清气泡 + **契约归一化层** |
| `web/agent-media.js` | **新增** | 媒体语义真源（9 mode / 实际生效值 / 静默回落 / **后端驱动的能力位** / **音乐无 aspect**） |
| `web/style.css` | 改 | 工具块 / 任务卡 / 澄清气泡 / 回落高亮的样式 |
| `web/responsive.css` | 改 | 窄屏（≤700px）新组件规则 |
| `web/index.html` | 改 | 加一行 `<script src="agent-media.js">`（须在 agent.js 之前） |
| `web/app.js` | 改 | **仅一处**：`loadGenerationTypes()` 里把服务端的 `agent_asset_slots` 喂给 `AgentMedia.applyServerAgentSlots()` |
| `tests/test_agent_frontend_enh.py` | **新增** | 31 个前端用例 |
| `tests/test_agent_media_semantics_frontend.py` | **新增** | 21 个媒体语义用例（原 `test_agent_media_semantics.py` 改名） |
| `tests/test_agent_contract.py` | **新增** | 19 个**跨侧契约**用例（真实后端 → 真实前端） |
| `scripts/verify_agent_frontend_enh.py` | **新增** | 真实浏览器三档宽度证据采集脚本 |
| `scripts/verify_jobcard_live_update.py` | **新增** | 任务卡实时更新端到端证据脚本（无桩） |
