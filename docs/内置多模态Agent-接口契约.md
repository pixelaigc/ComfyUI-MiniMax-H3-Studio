# 内置多模态 Agent —— 后端接口契约

> 对应分支：`local/feature/agent-enh-backend`
> 基线：`local/feature/builtin-agent`（提交 a455c7e）
> 本文覆盖**后端**提供的全部 HTTP 接口、工具名、入参、返回值与错误码。
> 前端（`web/`）由另一条线负责，本文只定义后端给出的**数据形状**。

---

## 0. 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/agent/chat` | Agent 主入口。`stream=1` 时返回 SSE |
| POST | `/api/agent/chat/stream` | Agent 主入口（SSE）。与上面请求体完全一致 |
| GET | `/api/agent/tools` | 工具清单 + 限额 + 事件类型 |
| GET | `/api/agent/memory` | 读取长期记忆（跨会话用户偏好） |
| POST | `/api/agent/memory` | 写入一条长期记忆 |
| GET | `/api/agent/sessions` | 会话清单 |
| GET | `/api/agent/sessions/{id}` | 某会话的完整消息历史 |
| DELETE | `/api/agent/sessions/{id}` | 清空会话 |

---

## 1. POST /api/agent/chat

### 1.1 请求体

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `message` | string | 是 | 用户意图，上限 4000 字 |
| `session_id` | string | 否 | 会话 ID。不给则新建；多轮对话靠它串联 |
| `provider_id` | string | 否 | 指定本地模型 Provider。不给则用「本地模型配置」里的默认 |
| `max_steps` | int | 否 | 最大步骤数。默认与上限都是 `8` |
| `stream` | 1/true/yes/on | 否 | `1` = 用 SSE 流式返回（等价于 `/api/agent/chat/stream`） |

### 1.2 响应（一次性 JSON，`stream` 不为 1）

```jsonc
{
  "session_id": "a1b2c3",
  "status": "completed",      // completed | needs_clarification | truncated | failed
  "answer": "已创建 1 个文生视频任务…",   // 给用户看的中文结论
  "jobs": [                   // 已创建的任务（job_id 由服务端生成，模型编不出来）
    {
      "job_id": "3f2a9c1b7d04",
      "mode": "t2v",
      "label": "文生视频",
      "prompt": "雷神手持战锤…",
      "params": {"aspect": "16:9", "duration": 8, "prompt": "…"},
      "asset_refs": [{"asset_id": "asset_xxx", "slot": "ref_image", "ordinal": 0}],
      "status": "queued",
      "summary": "一句话说明"
    }
  ],
  "assets": [ /* 命中的素材：asset_id / name / category / tags / description / analyzed */ ],
  "prompt": { "original": "…", "enhanced": "…", "spec_id": "image", "validation_ok": true },
  "steps": [
    { "step": 1, "tool": "search_assets", "ok": true, "ms": 1200,
      "args": "{\"query\":\"雷神\"}", "error": null, "note": null }
  ],
  "usage": { "steps": 4, "tool_calls": 5, "elapsed_ms": 213000 },
  "provider": { "name": "本机 Qwen 多模态", "model": "qwen3.8-flash-next-iq3_xxs" },
  "provider_name": "本机 Qwen 多模态",
  "error": null,              // 失败/截断原因（可读中文）
  "code": null,               // 失败时的结构化错误码（见 §5）
  "clarification": "要竖屏还是横屏？",  // 仅 needs_clarification 时出现
  "memory": { "used": true }  // 是否注入了记忆库偏好
}
```

### 1.3 状态语义

| status | 含义 | 前端该做什么 |
|---|---|---|
| `completed` | 正常完成 | 渲染结论 + 任务卡 |
| `needs_clarification` | Agent 在提问，等用户补充 | 显示 `clarification`，把用户输入带回同一 `session_id` |
| `truncated` | 达到步数/工具数上限或总超时 | 展示已完成的部分 + `error` 原因 |
| `failed` | 模型什么都没做成 / 被安全策略拒绝 | 展示 `error` 与 `code`，**不要**当作有任务 |

---

## 2. SSE 事件契约

`stream=1` 或 `POST /api/agent/chat/stream` 时返回 `text/event-stream`。

格式：
```
event: <事件名>
data: <JSON>

```

`data` 里同时带 `type` 字段（值等于事件名），所以前端既可以按 `event:` 监听，
也可以统一用 `onmessage` 读 `data.type` 分派。

### 2.1 事件类型

| 事件 | data 字段 | 说明 |
|---|---|---|
| `progress` | `stage` `detail` `done` `total` `elapsed_ms` | 阶段进度（长耗时期间保持界面有反馈） |
| `token` | `text` | 增量文本（模型输出的正文片段） |
| `tool_call` | `tool` `args` | 模型发起了一个工具调用（工具名 + 参数） |
| `tool_result` | `tool` `ok` `summary` | 工具结果摘要（一句话，可直接显示） |
| `job` | `job_id` `mode` `label` `status` `params` `summary` `prompt` (+`outputs` `thumbnail`) | **任务卡数据**，前端据此渲染卡片并开始轮询 |
| `done` | `answer` `status` `jobs` | Agent 编排结束（**不是**流的最后一个事件，后面还有 `result`） |
| `error` | `error` `code` | 错误（可读文案 + 错误码） |
| `result` | 完整 JSON | 流的**最后一个**事件，结构与一次性接口完全相同（向后兼容基线前端） |

### 2.2 事件顺序（典型成功链路）

```
progress(start)
progress(thinking)  →  tool_call → tool_result
progress(thinking)  →  tool_call → tool_result
progress(thinking)  →  tool_call → tool_result
progress(thinking)  →  tool_call(create_job) → tool_result
job                ← 任务卡（create_job 之后立即发）
progress(thinking)  →  tool_call(respond) → tool_result
token              ← 最终答复文本
progress(done)
done               ← Agent 收尾（answer + status + jobs）
result             ← 完整 JSON（与一次性接口同结构）
```

**收尾事件说明（重要）**

流的最后一个事件是 **`result`**，不是 `done`。原因是**向后兼容**：
基线的 `/api/agent/chat/stream` 原本就以 `result` 事件收尾（携带完整 JSON），
现有前端 `web/agent.js` 的 `handle(event, data)` 靠它拿到最终结果。
所以本轮**保留** `result` 作为最后一个事件，`done` 作为「Agent 逻辑收尾」事件
紧跟在其前。

| 事件 | 语义 | 前端该做什么 |
|---|---|---|
| `done` | Agent 编排结束（answer / status / jobs） | 更新状态徽标；**不要**在这里关流 |
| `result` | 完整 JSON（一次性接口的同结构） | 渲染最终结果；**在这里**关流 |

失败时不会发 `result`，而是发 `error` 事件后关流（见下）。

**失败链路**

```
progress(start)
progress(thinking)
error             ← {error, code}，随后关流（**没有** result）
```

前端必须同时处理这两种收尾：收到 `done` 后**继续等** `result` 或 `error`，
收到 `error` 直接关流。

> 失败时**不会**再发 `result` —— `result` 是「成功收尾」信号。
> 基线前端靠 `result` 判定「任务完成」，失败后再补一个 `result` 会让它误判成功。

**每个事件的 `data` 都带 `type` 字段**（值等于事件名），
所以前端既可以按 `event:` 监听，也可以统一用 `onmessage` 读 `data.type` 分派。

### 2.3 `job` 事件的数据形状

```jsonc
{
  "job_id": "3f2a9c1b7d04",   // 服务端真实 job id（前端用它轮询 /api/jobs/{id}）
  "mode": "t2v",
  "label": "文生视频",
  "status": "queued",         // queued | running | done | failed | canceled
  "params": { "aspect": "16:9", "duration": 8, "prompt": "…" },
  "summary": "一句话说明",
  "prompt": "雷神手持战锤…",
  "outputs": [],              // 有产物时才有（kind / file / name）
  "thumbnail": "/files/…"     // 有缩略图时才有
}
```

> 产物路径与缩略图**只在真实存在时给**，不给占位假值。
> 前端轮询 `/api/jobs/{job_id}` 可拿到更完整的 `outputs` / `progress` / `progress_text`。

---

## 3. 工具清单（23 个）

工具的 JSON Schema 由 `GET /api/agent/tools` 实时返回 —— **前端不要硬编码**。

### 3.1 既有 6 个

| 工具 | 入参 | 返回 |
|---|---|---|
| `search_assets` | `query`(必) `category` `limit` | `{total, assets[], hint}`；素材描述已做注入隔离 |
| `list_modes` | — | `{modes[], aspects[], video_duration_range, music_duration_range, notes}` |
| `describe_asset` | `asset_id`(必) | 素材详情（描述已隔离） |
| `enhance_prompt` | `prompt`(必) `mode` | `{original, enhanced, spec_id, validation_ok}` |
| `create_job` | `mode`(必) `prompt`(必) `aspect` `duration` `resolution` `batch_count` `asset_id` `summary` | `{job_id, mode, status, label}` |
| `respond` | `summary`(必) `job_ids` `asset_ids` `needs_clarification` | `{ok:true}` |

### 3.2 素材类（4 个，新增）

| 工具 | 入参 | 返回 | 错误码 |
|---|---|---|---|
| `upload_asset` | `path`(必，相对素材库目录) `name` `category` `tags[]` `note` | `{ok, asset:{asset_id,name,category,subtype,tags,bytes,duplicated}, hint}` | `path_forbidden` `file_not_found` `file_too_large` `bad_request` |
| `delete_asset` | `asset_id`(必) `purge`(bool) | `{ok, asset_id, purged, deleted_at}` | `asset_not_found` `asset_in_use` |
| `tag_asset` | `asset_id`(必) `add[]` `remove[]` `replace[]` | `{ok, asset_id, tags[]}` | `asset_not_found` `bad_request` |
| `list_asset_tags` | `limit` | `{total, tags:[{tag,count}], hint}` | — |

> `upload_asset` **只接受素材库目录内的相对路径**。绝对路径与 `../` 越界一律拒绝
> （`path_forbidden`）。

### 3.3 任务类（5 个，新增）

| 工具 | 入参 | 返回 | 错误码 |
|---|---|---|---|
| `cancel_job` | `job_id`(必) | `{job_id, status, ok, message}` | `bad_request` |
| `retry_job` | `job_id`(必) `overrides`(object) | `{job_id, mode, status, retried_from, label}` | `job_not_found` `invalid_params` |
| `batch_create_jobs` | `jobs[]`(必，≤10) | `{ok, created[], failed[], created_count, failed_count}` | `batch_failed` `invalid_params` |
| `list_jobs` | `status` `mode` `since_hours` `limit` | `{total, jobs[], hint}` | `bad_request` |
| `get_job_status` | `job_id`(必) | 见下 | `job_not_found` |

`get_job_status` 返回（**轮询友好**）：

```jsonc
{
  "job_id": "3f2a9c1b7d04",
  "mode": "t2v",
  "label": "文生视频",
  "status": "running",       // queued | running | done | failed | canceled
  "progress": 0.42,
  "progress_text": "采样中",
  "error": null,
  "outputs": [ { "kind": "video", "file": "…", "name": "…" } ],
  "params": { … },
  "created_at": 1794258000.0,
  "elapsed": 12.3,
  "poll_after_ms": 3000,     // 建议的下次轮询间隔；终态为 0
  "is_terminal": false,      // 终态（done/failed/canceled）为 true
  "batches": [ … ]           // 批次任务才有
}
```

### 3.4 提示词类（2 个，新增）

| 工具 | 入参 | 返回 | 错误码 |
|---|---|---|---|
| `list_prompt_presets` | — | `{total, presets:[{spec_id,label,media,modes[],validate_mode}], hint}` | — |
| `get_prompt_preset` | `spec_id`(必) | `{spec_id,label,media,modes[],validate_mode,system_prompt,template,params}` | `preset_not_found` |

> 预设列表**等于** `enhancer.TEMPLATE_SPECS` 的真实注册项，不多不少。

### 3.5 模式类（2 个，新增）

| 工具 | 入参 | 返回 | 错误码 |
|---|---|---|---|
| `describe_mode_params` | `mode`(必) `field` | 单字段时 `{mode, field, spec}`；否则整个 mode schema | `bad_request` |
| `get_mode_schema` | `mode`(必) | `{mode,label,media,params{...},defaults{},requires[],notes{}}` | `bad_request` |

schema 里每个参数的取值集合**都带 `source` 字段标明出处**（可追溯，不是硬编码白名单）：

```jsonc
"params": {
  "aspect":    { "type":"string", "values":["1:1","2:3",…], "default":"16:9",
                 "source":"workflows.ASPECT_MAP" },
  "resolution":{ "type":"string", "values":["544p","768p"], "default":"768p",
                 "source":"workflows.RES_MP" },
  "duration":  { "type":"number", "min":5, "max":15, "step":1, "default":5,
                 "source":"web/index.html#duration 滑块区间" },
  "lora":      { "type":"string", "values":["none","ckpt850","v4step600"],
                 "default":"ckpt850", "source":"workflows.LORAS" }
}
```

### 3.6 记忆/历史类（4 个，新增）

| 工具 | 入参 | 返回 | 错误码 |
|---|---|---|---|
| `recall_memory` | `query` `scope` `limit` | `{total, memories:[{key,scope,value,source,updated_at}], scope}` | — |
| `remember_preference` | `key`(必) `value`(必) `scope` | `{ok,key,scope,value,created}` | `prompt_injection_detected` `bad_request` |
| `search_conversations` | `query`(必) `session_id` `limit` | `{total, results:[{session_id,session_title,role,tool_name,snippet,created_at}], query, hint}` | `bad_request` |
| `search_asset_history` | `asset_id` / `query` `limit` | 传 `asset_id` → `{total:1, assets:[{asset_id,name,tags,versions[],analyses[],used_by[]}]}`；传 `query` → `{total, assets[]}` | `asset_not_found` `bad_request` |

`scope` 取值：`global`（跨会话，默认）或 `session:<id>`（仅该会话）。

---

## 4. 记忆接口

### 4.1 GET /api/agent/memory

Query：`query`（记忆键关键词，可选）、`scope`（可选）、`limit`（默认 20，上限 100）。

```jsonc
{ "memories": [ … ], "total": 3,
  "stats": { "total": 3, "global": 2, "session_scoped": 1,
             "max_entries": 500, "max_value_chars": 2000 } }
```

### 4.2 POST /api/agent/memory

Body：`{"key": "preferred_aspect", "value": "9:16", "scope": "global"}`

成功：`{"key":…,"scope":…,"value":…,"created":true,"evicted":0}`
失败（HTTP 400）：`{"error":"…","code":"prompt_injection_detected","matches":[…]}`

---

## 5. 错误码总表

| code | HTTP | 含义 |
|---|---|---|
| `bad_request` | 400 | 参数缺失/格式错 |
| `invalid_params` | 400 | 参数**取值**非法。响应带 `issues[]`（含 `allowed` 合法值集合 + `suggestions` 可执行替代方案） |
| `invalid_aspect` / `invalid_model` / `invalid_resolution` / `invalid_duration` / `invalid_lora` | 400 | 单字段取值非法（同上结构，`issues[].field` 区分） |
| `unknown_mode` | 400 | 模式不在 `workflows.BUILDERS` 里。`allowed` 给出真实支持的模式 |
| `too_many_refs` | 400 | 参考图超过上限 |
| `ref_required` / `ref_must_come_from_library` | 400 | 缺参考图 / 参考图必须来自素材库 |
| `path_forbidden` | 400 | 路径越界（只允许素材库目录内相对路径） |
| `file_not_found` / `file_too_large` | 404 / 413 | 上传相关 |
| `asset_not_found` / `job_not_found` / `preset_not_found` | 404 | 目标不存在 |
| `asset_in_use` | 409 | 素材正被引用，无法删除 |
| `prompt_injection_detected` | 400 | 内容含提示注入，拒绝写入记忆 |
| `sensitive_content` | — | 敏感内容请求被拒（`status=failed`，`refusal` 给拒绝话术） |
| `no_tool_call` | — | 模型只做口头承诺，未真正调用工具（`status=failed`） |
| `no_provider` | 400 | 没有可用 Provider |
| `provider_error` | 502 | 本地模型调用失败 |
| `too_many_requests` | 429 | 并发超限（当前 2 个会话在跑） |
| `agent_internal_error` | 500 | 内部异常 |

---

## 6. 限额（`GET /api/agent/tools` → `limits`）

| 项 | 值 | 理由 |
|---|---|---|
| `max_steps` | 8 | 实测一条成功链路要 6 轮（search×2 → list_modes → enhance → create → respond），留 2 轮余量 |
| `max_tool_calls` | 8 | 与步数同量级，防单轮内疯狂调用 |
| `total_timeout` | 420 s | 单步实测 20~120 s，4~5 步就要 4~6 分钟 |
| `step_timeout` | 180 s | 单次模型调用上限 |
| `max_concurrent` | 2 | 本地模型单卡，并发只会互相拖慢 |
| `no_tool_retry` | 1 | 强制重试一轮（多一轮 = 多付 20~120 s） |
| `tool_retry` | 1 | 只重试「读类」工具的「非参数类」失败；写类不重试（防重复副作用） |
| `repeat_limit` | 3 | 同名同参第 2 次给纠偏提示，第 3 次判定打转收尾 |

记忆限额（`GET /api/agent/tools` → `memory_limits`）：
`max_entries=500`、`max_value_chars=2000`、`max_read_limit=100`。
超限时**淘汰最旧的条目**（不是报错），并返回 `evicted` 计数。

---

## 7. 前端对接注意事项

1. **不要硬编码工具清单或参数取值** —— 一律从 `GET /api/agent/tools` 与
   `get_mode_schema` 取。后端保证这些值来自真实配置。
2. **`job_id` 只能用后端给的** —— 模型自报的 job_id 一律无效。后端已保证
   `jobs[]` 里的 id 是服务端真实生成的。
3. **流式收尾**：最后收到的是 `result`（完整 JSON），`done` 在它前面。
   失败时收到的是 `error`（没有 `result`）。两种都要处理。
4. **轮询任务进度**用 `/api/jobs/{job_id}`（前端已有）或让 Agent 调
   `get_job_status`。`poll_after_ms` 给出建议间隔。
5. **`needs_clarification` 时把用户输入带回同一个 `session_id`** —— 后端已把
   提问写进会话历史，下一轮会自动带上上下文。
6. **`status=failed` 时不要渲染任务卡** —— `code=no_tool_call` 表示什么都没做成。
