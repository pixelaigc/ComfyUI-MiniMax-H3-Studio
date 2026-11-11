# Agent 会话持久化与会话管理 —— 交付报告

- **日期**：2026-11-11
- **仓库**：`/root/projects/comfyui-minimax-h3-studio`
- **开发 worktree**：`/root/projects/h3studio-session-mgmt`
- **分支**：`local/feature/agent-session-mgmt`
- **基线**：`local/maintainer @ 7e98645`
- **提交**：`407f6e0` → `7b3ca35` → `00ed580` → `e617272` → `6dc2c27` → `b136d31` → `8c280aa`
- **合并到 local/maintainer**：`85f606b`
- **同步 /root/projects/h3studio-live**：`0d0d5d1`
- **线上部署**：已完成（8190 服务 active，22/22 自检 + 26/26 线上真机）

---

## 1. 根因

用户报告的现象是「刷新页面后交互历史丢失」。排查后确认：**根因不在后端落库，在前端从未接入会话库。**

### 1.1 后端其实一直在落库

`h3studio/agent_sessions.py`（297 行）是完整的 SQLite 持久化实现：

| 表 | 字段 | 作用 |
|---|---|---|
| `agent_sessions` | `id` / `created_at` / `updated_at` / `title` | 会话主表 |
| `agent_messages` | `session_id` / `role` / `content` / `tool_name` / `created_at` | 消息表 |

`server.py` 的 `api_agent_chat`（一次性）与 `api_agent_chat_stream`（SSE）**每次交互都会**：

```
store.create(session_id)           # 建/取会话
store.add_message(..., "user", message)      # 用户输入
history = store.history(...)                # 取历史喂给模型
store.add_message(..., "assistant", answer)  # 模型结论
store.add_message(..., "tool", ...)          # 工具调用轨迹
```

### 1.2 前端只把 session_id 当内存字符串

`web/agent.js` 里全部的会话相关代码只有 4 处：

```js
const state = { sessionId: "", ... };        // 第 17 行：内存字符串
body.session_id = state.sessionId            // 第 1245 行：发消息时带上
state.sessionId = data.session_id            // 第 1284 行：存回内存
state.sessionId = "";                        // 第 1548 行：「新建会话」= 清空内存
```

**没有**：刷新后恢复会话、拉会话列表、新建落库、删除会话、产物观测。

`GET /api/agent/sessions` / `GET /api/agent/sessions/{id}` / `DELETE /api/agent/sessions/{id}`
三个后端接口**早就存在**，但前端一个都没调过。

### 1.3 落库的信息也不够（第二层缺口）

即使前端接了接口，恢复出来的也只是「一堆字」：

- 工具调用只存了 `"create_job: ok"` 这种字符串 → **恢复不出任务卡**（不知道建了哪些任务、什么状态、缩略图在哪）
- 状态徽章（完成 / 截断 / 失败）没存 → **恢复后无法还原语义**
- 附件没存 → 用户传了哪几张图看不见

### 1.4 产物观测缺关联（第三层缺口）

`jobs` 表结构里**没有任何会话维度的字段**：

```
id | created_at | mode | params | status | prompt_id | progress | ...
```

于是「本次会话产物」有两种坏做法，都不能用：

1. **按时间窗猜**（「会话活跃期间创建的任务」）→ 会话之间串味，用户在会话 A 里看到会话 B 的任务；
2. **从 `agent_messages` 正文正则抠 job id** → 那是**模型写的文本**，模型可能编 id（这正是本项目之前修过的 bug），抠出来可能是假任务。

唯一可靠的做法是**建任务时就把会话 id 记进去**。

---

## 2. 改动清单

### 2.1 后端

| 文件 | 改动 | 关键点 |
|---|---|---|
| `h3studio/agent_sessions.py` | `agent_messages` 新增 `meta` 列 | **PRAGMA 幂等迁移**：`CREATE TABLE IF NOT EXISTS` 对已存在的表整句跳过，老库永远拿不到新列。用 `PRAGMA table_info` 查列名 + `ALTER TABLE ADD COLUMN`，已有就跳过 |
| | `list_sessions()` 补 `preview` + 空标题兜底 | 标题（40 字，列表用）与预览（120 字，摘要用）分开算，在库里一次算好，前端只管显示 |
| | `delete()` 返回值语义修正 | 原实现返回「删掉了几条**消息**」> 0 → 删**空会话**（刚建没发消息）返回 False，前端显示「删除失败」但会话其实已被删。改为返回「会话是否存在」 |
| | 新增 `rename()` / `title_of()` | 用户可以给会话起自己的名字 |
| | `raw_history()` 返回 `meta` | 坏 JSON 降级成空 dict（**不能**因为一条坏数据让整个历史打不开） |
| `server.py` | `jobs` 表新增 `session_id` 列 + 索引 | 同上的幂等迁移。`row_to_job` 用 `r.keys()` 兜底，老库直接读也不会 KeyError |
| | `create_job_record(mode, params, session_id="")` | 建任务时**当场**写入会话归属 |
| | 新增 `list_jobs_for_session(session_id)` | 产物观测的**唯一可信来源** |
| | 新增 `_agent_message_meta()` / `_agent_user_meta()` | 从 Agent 结果里抽最小可渲染结构（status / jobs / attachments / error / notice），不存完整响应体 |
| | 新增 `POST /api/agent/sessions` | 真正新建会话（原来前端只是清内存） |
| | `GET /api/agent/sessions` 补 `limit` | 默认 100、上限 500 |
| | `GET /api/agent/sessions/{id}` 补 `title` + `jobs` | 详情一次给全，前端少一次请求 |
| | 新增 `GET /api/agent/sessions/{id}/jobs` | 会话内任务（产物观测） |
| | 消息落库带 `meta` | 三处 `add_message` 调用点（一次性成功 / SSE 成功 / SSE 失败） |
| `h3studio/agent.py` | `AgentContext.session_id` | 上下文知道自己在哪个会话里 |
| | `_create_job_record()` 传 session_id | 用 `inspect.signature` 探测老服务端能不能接受第 3 个位置参数 |

### 2.2 前端

| 文件 | 改动 |
|---|---|
| `web/agent_sessions.js`（**新增** 约 460 行） | 会话管理层：列表 / 新建 / 删除 / 恢复 / 产物面板 / 抽屉 |
| `web/agent.js` | `restoreMessages()` 入口、会话 id 双向同步、产物面板同步钩子、`newSession()` 改为真落库 |
| `web/index.html` | 侧栏抽屉、会话列表、产物面板、菜单按钮、当前会话标题行 |
| `web/style.css` | 抽屉 / 会话条目 / 产物卡片样式 |
| `web/responsive.css` | 375px 窄屏规则（抽屉全宽覆盖、头部两行、产物单列） |

---

## 3. 接口清单

| 方法 | 路径 | 说明 | 请求体 | 返回 |
|---|---|---|---|---|
| POST | `/api/agent/sessions` | 新建会话 | `{title?}` | `{session_id, ok}` |
| GET | `/api/agent/sessions?limit=N&q=关键词` | 会话列表（`q` 搜索标题+任意消息正文） | — | `{sessions:[{id,title,preview,created_at,updated_at,message_count,artifact_count}],query}` |
| GET | `/api/agent/sessions/{id}` | 会话详情 | — | `{session_id,title,messages:[{role,content,tool_name,meta,created_at}],jobs:[...]}` |
| GET | `/api/agent/sessions/{id}/jobs` | 会话内任务（产物观测） | — | `{session_id,jobs:[...],count}` |
| DELETE | `/api/agent/sessions/{id}` | 删除会话（连同消息，级联清理产物**引用关系**） | — | `{deleted,ok,cleared_job_refs}` |

既有接口未改动：`POST /api/agent/chat`、`POST /api/agent/chat/stream`、`POST /api/agent/upload`、`GET /api/agent/tools`、`GET/POST /api/agent/memory`。

**兼容性**：`POST /api/agent/chat` 的请求体**新增可选** `session_id`（原来就有，沿用）；老前端不传时服务端自动新建会话，行为不变。

---

## 3.5 用户追加要求的实现

| 要求 | 实现 | 位置 |
|---|---|---|
| **会话列表搜索/过滤** | `GET /api/agent/sessions?q=关键词`，匹配**标题 + 任意消息正文**（标题只是首条消息前 40 字，用户后来聊的内容不在标题里，只搜标题等于搜不到）。前端 250ms 防抖 + 回车立即搜 | `agent_sessions.py::list_sessions(query=)` / `web/agent_sessions.js` |
| **列表带关联产物数** | `artifact_count` 字段。产物数要去 **jobs 表**数（会话库与任务库是两个库），由 `server.session_job_counts()` 一次 IN 查询补上 | `server.py::session_job_counts` |
| **删除时级联清理** | 删除会话 → 消息全删 + `jobs.session_id` **置空**。**任务记录本身留在生成库**（任务同时是生成库里的记录，带 outputs 产物路径，删记录会产生「文件还在、任务没了」的孤儿）。**素材库实体文件绝不碰**（在 `/mnt/usb2t/asset_library`，与会话无关） | `server.py::clear_session_job_refs` |
| **未完成任务实时刷新** | 产物面板**条件轮询**（4 秒）：面板开着 **且** 有 queued/running 的任务才轮询；面板关掉立刻停（不白白发请求） | `web/agent_sessions.js::startArtifactPolling` |
| **在生成库中定位** | 产物卡片上的「在生成库中定位」按钮 → 切到生成页 + 把 job id 写进 `location.hash` | `web/agent_sessions.js::locateJob` |

### 搜索的一个真 bug（我自己写出来的）

第一版搜索**没转义 LIKE 通配符**：用户搜 `%` 时 `LIKE '%%%'` 匹配**所有**会话 —— 搜索框看起来「什么都没输入却列出全部」。

修起来还踩了三个 SQLite 的坑（都是写完跑测试才发现的）：

1. SQLite 默认**不**把反斜杠当转义符 → 必须显式写 `ESCAPE '\'`；
2. `ESCAPE` 是 **LIKE 表达式**的修饰符，不是 `ORDER BY` 的 → 挂在 `ORDER BY` 后面直接语法错误；
3. 挂对位置后，消息正文那条 `LIKE` 也要单独挂 → 漏一条就还有一半的注入面。

现在 `%` 只匹配字面百分号、`_` 只匹配字面下划线（都有回归测试锁住）。

---

## 4. 会话数据落在哪里

| 数据 | 位置 | 说明 |
|---|---|---|
| 会话与消息 | `/root/projects/h3studio-live/data/agent_sessions.db` | SQLite，2 张表 `agent_sessions` / `agent_messages` |
| 会话↔任务关联 | `/root/projects/comfyui-minimax-h3-studio/data/jobs.db` 的 `jobs.session_id` 列 | 与任务库同库（任务本来就住这里） |
| 当前会话指针 | 浏览器 `localStorage` 的 `h3studio.agent.currentSession` | 只是「上次在哪个会话」的指针；真正的数据全在服务端 |

> **会话库与任务库是分开的两个 `.db`**：Agent 会话是多请求并发写，jobs 表是 worker 独占写；混在一张库里会让老数据的悬空引用把建表搞挂（素材库分库时踩过这个坑，见 `h3studio/assets.py` 的说明）。
>
> **测试时注意**：`/root/projects/h3studio-live/data/jobs.db` 是 0 字节空文件，**不是**生产任务库。生产任务库是 `/root/projects/comfyui-minimax-h3-studio/data/jobs.db`。

---

## 5. 页面刷新恢复的验证方式

### 5.1 手工验证

1. 打开 8190 的 Agent 页 → 发一条消息（比如「ping」）；
2. **F5 刷新页面**；
3. 预期：消息仍在（用户气泡 + Agent 回复），顶部显示「当前：…（N 条）」；
4. 点左上角 ☰ → 侧栏列出所有会话（标题 / 预览 / 时间 / 条数）；
5. 点另一个会话 → 切到那一段对话；再切回来 → 内容正确、不串味。

### 5.2 自动化验证（真浏览器）

`scripts/verify_session_mgmt_browser.py` —— 真浏览器 + 真 HTTP + 真 SQLite：

```
cd /root/projects/h3studio-session-mgmt
/home/ubuntu/comfyui/venv/bin/python scripts/verify_session_mgmt_browser.py
```

每档宽度（375 / 390 / 1280）验证 15 项：

| # | 检查项 | 断言 |
|---|---|---|
| 1 | 侧栏列出真实会话 | ≥ 2 项 |
| 2 | 会话标题有内容 | 全部非空 |
| 3 | 会话条目带时间与条数 | 每项含「条」 |
| 4 | **刷新后恢复会话历史** | 消息数 > 0 |
| 5 | 恢复出的消息有正文 | 非空文本 |
| 6 | 切换会话后内容正确 | 含目标会话的关键词 |
| 7 | **切换会话不串味** | 不含另一会话的关键词 |
| 8 | 产物面板列出任务 | ≥ 1 项 |
| 9 | 产物带任务 id | 含真实 job id |
| 10 | 产物带提示词 | 含 params.prompt |
| 11 | **新建交互落库** | 服务端会话数 +1 |
| 12 | **删除交互落库** | 服务端会话数 -1 |
| 13 | 无横向溢出 | `scrollWidth ≤ clientWidth + 1` |
| 14 | **console 0 错误** | 无 error 级 console |
| 15 | 0 失败请求 | 无 requestfailed |

**结果：45/45 通过**（3 档 × 15 项），console 0 错误、0 失败请求。

---

## 6. 产物观测入口的用法

1. Agent 页顶部右侧点 **「本次会话产物」** 按钮；
2. 面板展开，列出**当前会话**里 `create_job` 落库的所有任务：
   - 任务 id（等宽字体）
   - 状态徽章（已完成 / 排队中 / 生成中 / 失败）
   - 缩略图 / 预览（图片用 `<img>`，视频/音频用 `<video>`）
   - 提示词（最多 2 行）
   - 元信息（生成类型 + 创建时间）
3. 切换会话时面板自动跟着刷新（`loadSession` 里会重拉该会话的 jobs）。

**数据来源**：`GET /api/agent/sessions/{id}/jobs` → `server.list_jobs_for_session()` → 直接查 `jobs` 表的 `session_id` 列。

**为什么不用模型正文里的 id**：那是模型写的文本，模型可能编 id（本项目修过的 bug）。`jobs.session_id` 是建任务时服务端当场写入的，天然精确。

**删除会话不影响任务**：会话删除只丢对话历史，已生成的任务照旧在生成列表里（会话数据可丢弃，任务不可 —— 与素材库 409 保护是同一套取舍）。

---

## 7. 过程中抓到并修掉的问题

### 7.1 我自己引入的 bug（既有测试抓到）

**`FakeServer.create_job_record() takes 3 positional arguments but 4 were given`**

我给 `create_job_record` 加了 `session_id` 参数，既有测试里的假服务端只有 2 个参数 → 13 个既有测试挂了。

**第一版修法（错的）**：`try: creator(mode, params, sid) except TypeError: creator(mode, params)`。

**为什么错**：
1. `TypeError` 也可能从**函数体内部**抛出来 → 盲目重试会把真正的 bug 掩盖成「参数不对」；
2. Python 的 arity 报错文案是 `"takes 3 positional arguments but 4 were given"`，**根本不含参数名** → 想按 `"session_id" in str(exc)` 匹配匹配不上。

**正确修法**：`inspect.signature(creator)` 探测「能不能接受第 3 个位置参数」，探不出来时返回 False（**保守**：宁可丢会话归属，也不能把任务建不出来）。

> 这条是既有测试的功劳 —— 如果只跑我新写的测试，这个 bug 会一路带到线上。

### 7.2 真机验收抓到的 7 个问题（其中 3 个是**刷新后历史全没了**的真 bug）

| # | 问题 | 现象 | 根因 | 修法 |
|---|---|---|---|---|
| 1 | **init() 跑两遍** | **刷新后历史全没了**（`state.sessions` 被清空、`currentId` 被 `saveCurrentId("")` 抹掉） | 脚本有两条启动路径（`readyState === "loading"` 走 DOMContentLoaded，否则直接调），某些加载时序下**两条都会走到**。第二次 init 把第一次已经恢复好的状态**打回初始值** | `init()` 加幂等守卫 `_initialized` |
| 2 | 会话列表加载失败会清空已有会话 | 刷新后偶发显示「还没有交互记录」 | `loadSessions()` 的 catch 分支直接 `state.sessions = []`；刷新时一次后续加载失败（网络抖动）就把已经恢复好的会话抹了 | 失败时**保留**旧数据 + 提示错误 |
| 3 | `.agent-page` 缺定位上下文 | 抽屉（`position:absolute`）锚到**视口**而不是 Agent 页 → 盖住整个窗口（含顶栏） | 没有 `position:relative` | 补 `position: relative` |
| 4 | 抽屉挡住主区按钮 | 「本次会话产物」按钮点 30 秒点不动（`subtree intercepts pointer events`） | `translateX(-102%)` 把抽屉移出屏幕，但**它还在文档流里**，依然参与命中测试 | 关抽屉时 `visibility:hidden`（不用 `display:none`，那会丢过渡动画） |
| 5 | 按钮在加载期间是死的 | 点了菜单没反应 | `init()` 把所有 `addEventListener` 放在**两个 await 之后** | 事件绑定**同步前置**到 `init()` 开头 |
| 6 | 点「当前会话」会卡住主区 | 抽屉一直开着 | `sid === state.currentId` 时 `return` 直接跳过，关抽屉的代码没执行 | 先关抽屉再判断是否需要切换 |
| 7 | 切换会话时界面无响应 | 抽屉一直开着 | `await loadSession()` 放前面、`toggleDrawer(false)` 放后面 | 关抽屉**同步前置**，不等网络 |

> **问题 1 是最严重的一个，也是最难查的一个。**
> 它的表现是「偶发刷新后历史没了」—— 第一次跑验收是 45/45 全过，
> 加了 `position:relative` 之后突然变成 39/45。
> **光看代码看不出来**：两条启动路径单看都合理。
> 是加了 `console.log` 诊断才看到 `[dbg] INIT called` 出现了**两次**。
>
> 这条也说明了为什么「偶发」的问题必须留诊断日志 ——
> 没有它就只能靠猜，而猜出来的东西十有八九是错的。

### 7.3 真机验收脚本自身的坑（不算产品 bug，但同样费时间）

| # | 问题 | 现象 | 根因 | 修法 |
|---|---|---|---|---|
| 1 | 抽屉挡住主区按钮 | 「本次会话产物」按钮点 30 秒点不动（`subtree intercepts pointer events`） | `translateX(-102%)` 把抽屉移出屏幕，但**它还在文档流里**，依然参与命中测试 | 关抽屉时 `visibility:hidden`（不用 `display:none`，那会丢过渡动画） |
| 2 | 按钮在加载期间是死的 | 点了菜单没反应 | `init()` 把所有 `addEventListener` 放在**两个 await 之后** | 事件绑定**同步前置**到 `init()` 开头 |
| 3 | 点「当前会话」会卡住主区 | 抽屉一直开着 | `sid === state.currentId` 时 `return` 直接跳过，关抽屉的代码没执行 | 先关抽屉再判断是否需要切换 |
| 4 | 切换会话时界面无响应 | 抽屉一直开着 | `await loadSession()` 放前面、`toggleDrawer(false)` 放后面 | 关抽屉**同步前置**，不等网络 |

---

## 7.4 全量 pytest 对比暴露的 4 个新增失败（逐个定位修掉）

全量对比（基线 18 failed / 1620 passed vs 我的 22 failed / 1689 passed）暴露了
4 个**我自己引入**的回归。它们都不是「玄学」，每一个都定位到了具体一行：

| # | 回归 | 根因 | 修法 |
|---|---|---|---|
| 1 | **`.ag-head` 的 CSS 被整段替换**（严重视觉回归） | 我插入 `position: relative` 时用的是「**替换** `.ag-head` 规则」而不是「追加」，把原有的 `flex: 0 0 auto` / `display:flex` / `padding` / `border-bottom` 全弄丢了 —— 头部会被消息区压扁，窄屏下按钮被挤没 | 完整恢复原规则 + 追加 `position: relative`。被 `test_agent_review_fixes.py::test_composer_and_head_do_not_shrink` 抓到（那条测试专门守这个契约） |
| 2 | **老库没有 `session_id` 列 -> 500** | `test_agent_oneshot_e2e.py` 用**真实** `server.make_app()`，但它的 jobs 库是自建的 12 列结构且不调 `init_db()`。我的查询直接 `WHERE session_id=?` -> `OperationalError` -> `/api/agent/sessions/{id}` 500 -> 前端连对话历史都读不回来 | 服务端自己兜住：新增 `_jobs_table_has_session_column()`（按 DB_PATH 缓存列名判断），没有这一列时不按 session_id 过滤（等价于「没有产物」）。**没有改既有测试** —— 测试替身不该知道新字段 |
| 3 | **会话层 init 抹掉用户刚发的消息**（真 bug，会丢用户数据） | 会话管理层异步初始化，`init()` 调 `restoreMessages()` 而它是**整体替换** `state.messages`。init 期间用户发的消息会**凭空消失** | `restoreMessages` 支持 **merge 模式**：init 只补历史不删已在的；用户主动切换会话仍整体替换。恢复的消息带稳定 `_sid` 用于去重 |
| 4 | 验收脚本 `sqlite3` 只在函数内 import | `seed_sessions()` 里是局部 import，主流程用不到 -> `NameError` | 提到顶层 |

### 第 3 条的实测痕迹

它不是靠读代码想出来的 —— 是**一次性 E2E 测试（真 server + 真 Chromium）**
报「工具块没渲染」时，用一行 `page.evaluate` 打出 `window.AgentUI.state`
才发现 `messages.length === 0`：

```
'uiState': {'n': 0, 'msgs': []}
```

服务器返回的是**完全正确**的 JSON（`steps` 里 `list_modes` 齐全），
前端也**没有 JS 报错** —— 只是消息在渲染出来之前被 init 抹掉了。
这正是「真机验收比读代码有用」的例子。

---

## 8. 既有约束不回退

| 约束 | 验证方式 | 结果 |
|---|---|---|
| 真编造 job id 仍拦截 | `tests/test_agent_session_mgmt.py::test_fabricated_job_id_still_blocked_after_session_changes`（4 个子场景：编造 / 精确 / 抄写变体 / 格式不同） | ✅ |
| 步骤截断不误标失败 | `::test_truncated_status_not_failed_after_session_changes` | ✅ |
| 上传媒体必须先经素材库入库 + 预处理 | `tests/test_agent_media_upload.py`（66 条）全通过 | ✅ |
| 多图上限 9 张 | 同上 | ✅ |

---

## 9. 测试对比

### 9.1 全量 pytest（基线 vs 本分支）

| | 基线 `local/maintainer @ 7e98645` | 本分支 `8c280aa` | 差值 |
|---|---|---|---|
| 全量 pytest | 17 failed / 1623 passed / 6 skipped | 17 failed / 1697 passed / 6 skipped | **失败数相同（17）**，新增通过 **+74** |
| 失败集合差异 | — | `test_grid_flash.py::test_grid_height_stable_across_switch` | **负载敏感的 flake**（见下） |
| 新增单测 | — | 76 条（后端 23+23+26，前端契约 30） | +76 |
| Agent 相关 | 189 passed | 289 passed | +100 |
| 真机浏览器（临时实例） | — | **78/78**（375/390/1280 × 26 项） | 新增 |
| 真机浏览器（**线上 8190**） | — | **26/26** | 新增 |

> **一次「假新增失败」的排查记录（值得留档）**
> 第一轮全量对比跑出 **4 个新增失败**，全是行尾策略测试：
> `test_line_endings_policy_is_respected` / `test_blob_line_endings_are_crlf` /
> `test_crlf_files_have_crlf_in_index_and_worktree` / `test_worktree_bytes_are_crlf`。
>
> 排查过程：
> 1. 单独跑 -> **通过**；两文件一起跑 -> **通过**；四个测试一起 -> **通过**；
> 2. 查 `git ls-files --eol` -> 全部 `i/crlf w/crlf`，字节裸 LF = 0；
> 3. 查 `.gitattributes` -> 未被改动；
> 4. 查有没有测试写这些文件 -> 没有；
> 5. **对时间线** -> 全量「after」那一轮是 **19:55** 启动的，
>    而我恢复 `server.py` 的 CRLF 是 **20:59** —— **那一轮跑在了 LF 版本上**。
>
> 也就是说这 4 个失败测的是**我在跑测过程中改出来的中间态**，
> 不是最终代码的缺陷。修完后重跑 -> 零新增失败。
>
> **教训**：跑长测试的过程中改被测文件，跑出来的结果就没有意义 ——
> 必须「代码定稿 -> 再跑对比」。这也是为什么第二轮对比要重跑一遍。

### 9.1.1 唯一一条「差异失败」是 flake（已交叉复验 6 次）

失败集合 diff 只有一条：
`tests/test_grid_flash.py::TestGridFlash::test_grid_height_stable_across_switch`

它是**时序敏感的浏览器测试**（骨架卡 -> 真实卡片那一帧的网格总高度，
机器慢的时候骨架还没换掉就会量到不同高度）。交叉复验（同一台机器、
交替跑两个分支、各 3 次）：

| 轮次 | 本分支 | 基线 |
|---|---|---|
| #1 | passed | **failed** |
| #2 | passed | **failed** |
| #3 | **failed** | passed |

**两个分支都会翻** -> 与本次改动无关，是机器负载造成的 flake。
（另外两条 `test_music_flow_browser` 里的浏览器用例也有同样的负载敏感问题，
在基线上同样失败。）

所以结论是：**新增失败 0**。

---

### 9.2 新增回归测试（76 条）

| 文件 | 条数 | 覆盖 |
|---|---|---|
| `tests/test_agent_session_mgmt.py` | 23 | meta 列幂等迁移 / 坏 meta 降级 / 会话 CRUD / 标题 / **产物关联隔离** / **既有两项约束不回退** |
| `tests/test_agent_session_frontend.py` | 27 | DOM id / 事件绑定顺序 / 关抽屉先于 await / CSS 定位上下文 / **merge 模式** / 不从模型文本抠 job id |
| `tests/test_agent_session_search_cascade.py` | 26 | 搜索（标题/正文/大小写/无匹配/**LIKE 通配符转义**）/ 产物计数 / **级联清理保留任务与 outputs** / 前端契约 |

### 9.3 真机浏览器验收

**临时实例**（`scripts/verify_session_mgmt_browser.py`）—— 26 项 × 3 档 = **78/78**：
会话列表 / 标题 / 时间与条数 / **刷新后恢复历史** / 恢复出的正文 /
切换会话 / **不串味** / 产物面板 / 产物 id / 提示词 / **未完成任务标记** /
**定位入口** / **未完成任务状态实时刷新** / **产物数** / **搜索过滤** /
**搜不到为空** / **清空搜索恢复** / 新建落库 / 删除落库 /
**级联清理不删任务** / **级联清理只清引用** / 不溢出 / console 0 错误 / 0 失败请求

**线上 8190**（`scripts/verify_session_mgmt_live.py`）—— **26/26**：
会话列表 / 搜索 / 切换 / **刷新后历史仍在** / 产物面板 / 不溢出 /
console 0 错误 / 0 失败请求 / **会话数已还原（自检会话已清理）**

**线上自检脚本**（`scripts/live_selfcheck_session_mgmt.py`）—— **22/22**：
服务 active / 端点 200 / 会话 CRUD 全链路 / **两个生产库已迁移** / journal 无 error

---

## 10. 合并与上线自检

| 步骤 | 结果 |
|---|---|
| 合并到 `local/maintainer` | `85f606b`（`git merge --no-ff`，无冲突） |
| 同步 `/root/projects/h3studio-live` | `0d0d5d1` |
| `systemctl restart h3-studio.service` | **active** |
| 端点自检 | `/` 200 · `/api/agent/sessions` 200 · `POST /api/agent/sessions` 200 · `/api/agent/tools` 200 · `/api/jobs` 200 |
| journal | **0 error / 0 traceback**（重启后 30 分钟窗口） |
| 线上自检脚本 `scripts/live_selfcheck_session_mgmt.py` | **22/22 通过** |
| 线上真机验收 `scripts/verify_session_mgmt_live.py` | **26/26 通过**（375/390/1280，console 0 错误） |

### 生产库迁移确认（自检脚本实测）

| 库 | 迁移前 | 迁移后 | 数据完整性 |
|---|---|---|---|
| `/root/projects/h3studio-live/data/agent_sessions.db` | `agent_messages` 12 列（**无 meta**） | 13 列（**有 meta**） | 10 会话 / 69 消息**全部保留**；老会话能正常读出（meta 降级成空 dict） |
| `/root/projects/comfyui-minimax-h3-studio/data/jobs.db` | `jobs` 12 列（**无 session_id**） | 13 列（**有 session_id**）+ 索引 | 129 条任务**全部保留**；老任务 `session_id` 为空串（不是 NULL） |

> 迁移是在**生产库的真实副本**上验证过的（不是造的假库）：
> 复制生产库 -> 跑迁移 -> 核对行数/列名/老数据可读 -> 二次初始化确认幂等。

### 线上验收截图（27 张，已复制到工作区 `evidence/session-mgmt/`）

| 目录 | 内容 | 张数 |
|---|---|---|
| `evidence/session-mgmt/session-mgmt/` | 会话列表 / 搜索 / 刷新恢复 / 产物面板 / 未完成任务（pending） × 375/390/1280 | 15 |
| `evidence/session-mgmt/session-mgmt-live/` | 线上 8190 的会话列表 / 搜索 / 刷新恢复 / 产物面板 × 375/390/1280 | 12 |

---

## 11. 未修复 / 遗留项（如实列出）

### 11.1 本次范围内未做

| 项 | 原因 | 影响 |
|---|---|---|
| 会话标题**改名**的 UI 入口 | 后端 `rename()` / `title_of()` 已实现（`POST /api/agent/sessions` 的 `title` 参数也能建带标题的会话），但**前端没有改名输入框** | 用户只能用「首条消息前 40 字」做标题，不能手动改。功能完整、入口缺失 |
| 会话**导出 / 导入** | 本次需求未提 | — |
| 「本次会话产物」**跨会话汇总视图** | 本次需求只要求「单次会话内的产物」 | 想看全部会话的产物要去生成库 |
| 产物面板的**批量操作**（重跑/取消） | 本次需求只要求「查看 + 定位」 | 单个任务的重试/取消在生成库里有 |
| 搜索**分词**（中文按词） | 当前是 SQL `LIKE` 子串匹配 | 搜「赛博朋克」能命中；搜「赛博 朋克」（带空格）命中不了 |
| 会话列表**分页/虚拟滚动** | 当前一次拉 200 条 | 会话量到几百条以上需要分页 |

### 11.2 已知但未修的**既有**问题（与本次改动无关）

| 项 | 证据 |
|---|---|
| 生产生成列表里的 `/files/videos/*.mp4` 有 `net::ERR_ABORTED` | 实测：**只打开生产首页、完全不碰 Agent 功能**，同样有 6 个 ERR_ABORTED；且 `curl` 这些文件返回 HTTP 200。浏览器在 `<video>` 离开视口时主动取消预载。**本次线上验收的统计口径已排除它** |
| 全量 pytest 的 17 个既有失败 | 基线 `local/maintainer @ 7e98645` 上同样是 17 个（`test_studio.py::TestLoRARegistry` / `test_yue2_flow_review_fixes.py` 等），**与本次改动无关** |
| 部分浏览器用例在机器高负载时集体超时 | `load average 27+` 时前端首屏要 20 秒以上（测试注释里已记录）。本次跑全量对比时机器负载 30，属于环境因素 |

### 11.3 需要留意的**语义边界**

| 场景 | 当前行为 | 是否有提示 |
|---|---|---|
| 删会话 | 会话消息删除；**任务记录留在生成库**（只是不再归属会话）；**素材库文件不动** | 删除确认弹窗里写明了「已生成的任务不受影响」 |
| 删会话后看生成库 | 任务还在（生成库是独立视图） | — |
| 老会话（迁移前建的） | `meta` 列为空 -> 恢复出来是「纯文本对话」，**没有任务卡缩略图** | 无提示。这是迁移的固有限制：旧数据当时就没存结构化信息 |
| 会话库与任务库分属两个 `.db` | 产物数要跨库统计 | 无（内部实现，用户无感） |

---

