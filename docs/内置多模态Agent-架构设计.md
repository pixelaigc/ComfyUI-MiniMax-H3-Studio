# H3 Studio 内置多模态 Agent —— 架构设计

> 版本：v1（2026-11-09）
> 分支：`local/feature/builtin-agent`
> 范围：在 ComfyUI-MiniMax-H3-Studio 内建设一个**内置的**多模态 Agent，
> 专门处理音 / 视频 / 图像相关任务，作为项目内置能力存在（可被面板调用）。

---

## 1. 背景与问题定义

### 1.1 用户原始诉求

> 「本地模型光一问一答无法正确处理内容」，要求在本项目内建设一个**强力的内置 Agent**，
> 专门处理各种音 / 视频 / 图像相关任务，作为项目内置能力存在（可被面板调用）。

### 1.2 现状的三个具体缺口（源码事实，非推测）

| # | 缺口 | 源码依据 |
|---|---|---|
| 1 | **一问一答，没有工具**。`/api/enhance` 拿到 prompt 后直接调一次模型、拼一次校验就返回。全程没有任何「查素材库」「看有哪些模式」「下发任务」的通道。 | `server.py:2257-2399` `api_enhance` |
| 2 | **增强结果不能直接变成任务**。用户拿到一段扩写后的提示词，要自己切模式、自己填分辨率/比例/时长/种子、再点「生成」。中间每一步都是手工搬运。 | `web/app.js:1848-2023`（提交逻辑全在前端） |
| 3 | **多模态只用在「读图描述」上**。`analysis.py` 的三个 Analyzer（image/video/audio）确实读了图、抽了帧、探测了音频参数，但产出只是**一段描述文本**，不参与任何工作流参数决策。 | `h3studio/analysis.py:84-253` |

### 1.3 关键判断

本项目**不缺**能力，缺的是**编排层**：

- 本地模型 Provider 配置已经完备（`h3studio/providers.py`，4 种协议 + 多模态标记 + 默认项）
- 工作流拼装层已经完备（`h3studio/workflows.py:2234` `BUILDERS`，9 种生成模式）
- 素材库 API 已经完备（`h3studio/asset_api.py`，23 个路由 + 搜索/标签/分析/纠错）
- 提示词增强已经是一个「迷你 Agent」（`h3studio/enhancer.py`，有 Skill 目录、模板、参数、校验器）

所以本设计的核心是：**把上述已有能力编排成一个能多步决策的工具调用 Agent**，
而不是再造一套模型调用或工作流构造。

---

## 2. 现状调研结论（可复用能力边界）

### 2.1 本地模型接入层 —— `h3studio/llm.py`

| 能力 | 签名 | 复用方式 |
|---|---|---|
| 统一发消息拿文本 | `call_provider(provider, messages, images, options) -> str` | **直接复用**。统一 openai / llamacpp / ollama / comfyui 四种协议 |
| 多模态图片 | 同上的 `images=[路径]` 参数 | **直接复用**。OpenAI 走 data URL，Ollama 走 base64 裸串 |
| 连通性探测 | `check_provider(provider) -> {"ok","detail","models"}` | **直接复用** |
| 失败语义 | 抛 `ProviderError`，message 面向用户可直接读 | **直接复用** |

**实测（本机 `http://127.0.0.1:5000`，模型 `qwen3.8-flash-next-iq3_xxs`）**：

- ✅ 支持 **OpenAI 原生 `tools` / `tool_calls`** 协议（实测返回 `message.tool_calls`，`content` 为 null）
- ✅ `chat_template_kwargs: {"enable_thinking": False}` 生效（`reasoning_content` 变为 null）
- ⚠️ **单步耗时 15–110 秒**（4s / 17s / 30s / 51s / 110s 实测）—— 这决定了 Agent 的超时与步数上限
- ⚠️ **工具结果不满意时会重复调用同一个工具**（实测连续 6 步都在调 `search_assets`）—— 这决定了必须做重复检测
- ⚠️ **会用自然语言叙述「将要做什么」而不调工具**（实测原话：「我先增强提示词，再下发生成任务。」）—— 这决定了必须有「口头承诺防护」
- ⚠️ **回 tool 消息时倾向继续追问**（一次检索后连续追 2–3 轮）—— 这决定了要按「轮」而不是按「条」裁剪历史

> ⚠️ `llm.py` 现有的 `call_provider` **只提取 content 文本，丢弃 `tool_calls`**。
> Agent 需要工具调用，所以本设计**在 `llm.py` 里新增一个 `call_provider_with_tools`**，
> 与原函数并存（不改动原函数，避免影响增强/分析链路）。

### 2.2 Provider 配置层 —— `h3studio/providers.py`

- 表：`model_providers` / `model_provider_prefs`（服务端 SQLite `data/jobs.db`）
- 取默认：`resolve_default(conn, key, require_multimodal=False)`
  - `key="default"` → 普通默认模型
  - `key="default_multimodal"` → 多模态默认模型
- 完整 Provider（含 api_key 明文）：`get_provider_full(conn, provider_id)`

**Agent 复用**：不新建任何 Provider 配置表。Agent 用的就是「本地模型配置」页里那一份。

### 2.3 工作流拼装层 —— `h3studio/workflows.py`

```python
BUILDERS = {
    "t2v": build_t2v, "flf": build_flf, "r2v": build_r2v,
    "t2i": build_t2i, "t2i_qwen": build_qwen_t2i,
    "i2i_qwen": build_qwen_i2i, "t2i_moss": build_moss_t2i,
    "t2a": music_yue2.build_t2a, "a2a": music_yue2.build_a2a,
}
def build_workflow(mode, params): ...
```

**Agent 复用**：Agent **不自己拼工作流**。Agent 只产出 `mode` + `params`，
交给现有的 `/api/jobs` 通道去建任务 —— 后端已有 worker 会调 `build_workflow` 并提交 ComfyUI。

这样 Agent 的参数面天然受限于现有工作流的能力，不可能「臆造」出跑不通的模式。

### 2.4 素材库 —— `h3studio/asset_api.py` + `asset_repo.py`

- 搜索：`asset_repo.search_assets(conn, filters)`，`filters` 支持
  `q / category / subtype / tag / tags_any / tags_all / source / creator / sort / page / page_size`
- 条目内容含：`analysis_summary`（最近一次模型分析的前 120 字）+ `tags` + `analyzed` 标记
- 参考图元数据：`ref_api._ref_entry(conn, asset_id)` → 描述/标签/预览/失效标记

**Agent 复用**：素材检索工具直接调 `search_assets`，返回给模型的是**精简后的字段**
（id / name / category / description / tags），不是整行 JSON —— 减少 token 与模型误用字段。

### 2.5 生成类型注册表 —— `h3studio/gen_types.py`

`list_types()` → 前端类型标签清单；`registered_modes()` → 后端真实注册的模式。
**Agent 复用**：模式清单从 `workflows.BUILDERS` 取，**不在 Agent 里硬编码**。

### 2.6 提示词增强 —— `h3studio/enhancer.py`

已有「迷你 Agent」：模板表 `TEMPLATE_SPECS`、参数档 `LENGTH_PRESETS/SAMPLING_PRESETS`、
消息构造 `build_messages()`、输出清洗 `strip_wrapping/strip_thinking`、
硬约束校验 `validate_output()`。

**Agent 复用**：`enhance_prompt` 工具内部走 `enh.build_messages` + `llm.call_provider`，
与 `/api/enhance` **同一套模板与校验**。Agent 不另写一套扩写。

---

## 3. 架构设计

### 3.1 总体分层

```
┌─────────────────────────────────────────────────────────────┐
│  前端面板  web/agent.js  （左侧导航「内置 Agent」入口）      │
│    ├─ 对话区（多轮消息气泡）                                  │
│    ├─ 结构化结果卡（任务列表 / 素材命中 / 提示词产物）         │
│    └─ 「填入面板」按钮 → 把 Agent 的产物回填到创作面板        │
└──────────────────────────────┬──────────────────────────────┘
                               │ POST /api/agent/chat
┌──────────────────────────────▼──────────────────────────────┐
│  Agent 服务层  h3studio/agent.py                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ ① 编排器 Orchestrator                                 │  │
│  │   · 步骤上限 / 总超时 / 并发闸门                       │  │
│  │   · 重复工具调用检测（模型会原地打转）                  │  │
│  │   · 工具结果裁剪 + 回填                                │  │
│  ├───────────────────────────────────────────────────────┤  │
│  │ ② 工具注册表 Tool Registry                            │  │
│  │   search_assets / list_modes / enhance_prompt /        │  │
│  │   describe_asset / create_job / respond                │  │
│  ├───────────────────────────────────────────────────────┤  │
│  │ ③ 模型适配层 llm.call_provider_with_tools（新增）      │  │
│  │   复用 providers.get_provider_full / resolve_default   │  │
│  └───────────────────────────────────────────────────────┘  │
└───────────────┬───────────────────────────┬─────────────────┘
                │                           │
      ┌─────────▼──────────┐      ┌─────────▼─────────────────┐
      │ 既有能力（不重写）  │      │  既有能力（不重写）        │
      │ asset_repo 搜索     │      │  workflows.BUILDERS       │
      │ enhancer 扩写+校验  │      │  /api/jobs → Worker       │
      │ analysis 分析器     │      │  → build_workflow         │
      │ providers Provider │      │  → ComfyUI                │
      └────────────────────┘      └───────────────────────────┘
```

### 3.2 Agent 的五个组成部分

#### ① 模型接入（Model Adapter）

- 复用 `h3studio/llm.py` 的四协议抽象，**新增** `call_provider_with_tools()`：
  - 返回结构从「字符串」扩展为「`{content, tool_calls, raw}`」；
  - 原 `call_provider()` 保持原样（`enhancer`/`analysis` 链路零影响）；
  - 复用同一套 `_messages_to_openai` / `_messages_to_ollama` 转换；
  - `comfyui` 类型 Provider **不支持 tools** → 明确报错，不静默降级。
- Provider 选择：显式 `provider_id` > `default_multimodal`（需要读图时）> `default`。
- thinking 模型走 `chat_template_kwargs.enable_thinking=false`（`llm.py` 已有此逻辑，tools 路径沿用）。

#### ② 工具 / 函数调用（Tool Registry）

每个工具 = `{name, description, json_schema, handler}`。handler 签名统一为
`async (ctx, args) -> dict`（返回值即给模型的 tool 消息内容，**已序列化**）。

| 工具 | 作用 | 复用来源 | 副作用 |
|---|---|---|---|
| `search_assets` | 按关键词/类型检索素材库 | `asset_repo.search_assets` | 只读 |
| `list_modes` | 列出后端真实注册的生成模式与各自可选项 | `workflows.BUILDERS` + `gen_types` | 只读 |
| `describe_asset` | 取某个素材的详情 + 最近一次模型分析结论 | `asset_repo` + `ref_api._ref_entry` | 只读 |
| `enhance_prompt` | 用本项目增强模板扩写提示词（带硬约束校验） | `enhancer.build_messages` + `enh.validate_output` | 只读 |
| `create_job` | 按 mode + params 创建一个生成任务 | 与 `/api/jobs` **同一套建任务逻辑** | **写**（入队） |
| `respond` | 提交最终结构化结果 | — | 只读 |

**设计要点：**

- **工具集是白名单**。模型只能调用上表里的工具；未知工具名直接返回错误，不执行任何东西。
- **`create_job` 复用后端建任务函数**，不自己写 SQL、不自己拼 multipart。
  参数经 `workflows` 侧的模式白名单 + `enhancer` 侧档位白名单双重收敛，
  跑不通的组合在提交前就返回可读错误。
- **`create_job` 是唯一的写操作**，且只写「任务队列」，不直接调 ComfyUI ——
  与用户在面板点「生成」完全同一条路，因此产物在任务列表里可见、可取消。

#### ③ 上下文与多轮（Context & Multi-turn）

- **会话**：客户端持有 `session_id`（UUID），服务端在 `data/agent_sessions.db`
  存会话与消息历史。
- **历史裁剪**：只保留最近 `HISTORY_LIMIT`（默认 8）轮，
  且对历史里的 tool 消息做**结果裁剪**（每个工具结果最多 600 字符）——
  否则素材检索返回的大 JSON 会把上下文撑爆。
- **多轮**：同一 `session_id` 的下一次请求会把历史拼进 messages，
  上一轮「创建的任务」也会作为上下文摘要带进去，用户可以说「再生成一个竖屏的」。

#### ④ 结构化输出（Structured Output）

Agent 的最终产出**不是一段散文**，而是固定 schema：

```jsonc
{
  "session_id": "…",
  "status": "completed",         // completed | needs_clarification | failed
  "answer": "给用户看的中文结论",
  "jobs": [                       // 已下发的生成任务
    { "job_id": "…", "mode": "t2v", "label": "文生视频", "prompt": "…",
      "params": {…}, "status": "queued" }
  ],
  "assets": [ { "asset_id": "…", "name": "…", "category": "image" } ],
  "prompt": { "original": "…", "enhanced": "…" },   // 增强产物
  "plan": { "mode": "t2v", "params": {…} },        // 只组装未提交时
  "steps": [ { "step": 1, "tool": "search_assets", "ok": true, "ms": 812 } ],
  "usage": { "steps": 3, "elapsed_ms": 45123 }
}
```

**产出 schema 的三条硬规则：**

1. `jobs` 里的每个 id 都**必须**是服务端真实返回的（客户端拿不到假 id）。
2. `plan` 里不出现 `BUILDERS` 之外的 mode（生成模式白名单来自后端）。
3. 任何拿不到的字段一律省略，**不填占位假值**。

#### ⑤ 任务编排与错误处理（Orchestration & Error Handling）

| 约束 | 值 | 理由（均为实测得出） |
|---|---|---|
| 单次请求总超时 | 420 秒（`asyncio.wait_for` 兜住整轮） | 实测**成功**链路单步 15–110 秒、6 步共约 290 秒。给 300 秒会砍掉正常流程 |
| 单步模型超时 | 180 秒 | 与 `llm.CHAT_TIMEOUT` 一致 |
| 最大步骤数 | 8 | 实测一条完整成功链路要 6 轮（搜×2 → 查模式 → 扩写 → 建任务 → 交结论）。卡在 6 就一点余量没有 |
| 最大工具调用数 | 8 | 同上 |
| 并发上限 | **同时 2 个 Agent 会话**（`asyncio.Semaphore`） | 本地模型单卡，并发只会互相拖慢 |
| 重复调用检测 | 同名工具 + 相同参数：第 2 次**先给纠偏提示**，第 3 次才判定打转收尾 | 实测模型会连续 6 步调同一个工具；但第 2 次往往是合理重试，直接掐死会误伤 |
| 口头承诺防护 | 模型只叙述「接下来要做什么」却不调工具 → 推它继续调 | 实测原话「我先增强提示词，再下发生成任务。」—— 用户收到承诺而不是任务 |
| 工具异常 | 捕获 → 作为 tool 消息回给模型（不中断整轮） | 让模型有机会自己纠正参数 |
| 模型不可达 | 502 + `ProviderError` 原文 | 与 `/api/enhance` 一致 |
| 未配置 Provider | 400 + 明确提示去「本地模型配置」页 | 与 `/api/enhance` 一致 |
| `comfyui` 类型 Provider | 明确报错，不静默降级 | 该协议不支持 tools；降级会让 Agent 退化成「一问一答」—— 正是要解决的问题 |

**错误分类：**

| 类别 | HTTP | 响应字段 |
|---|---|---|
| 请求体非法 | 400 | `{"error": "…", "code": "bad_request"}` |
| 未配置本地模型 | 400 | `{"error": "…", "code": "no_provider"}` |
| 模型不可达 / 超时 | 502 | `{"error": "…", "code": "provider_error", "degraded": true}` |
| 步骤超限 | 200 | `{"status": "truncated", "steps": [...]}` —— **返回已完成的部分**，不丢结果 |
| 并发超限 | 429 | `{"error": "…", "code": "too_many_requests"}` |

**关键原则：超时/超限不是「挂死」，而是**返回**已完成步骤 + 可读原因。**

---

## 4. 接口说明

### 4.1 `POST /api/agent/chat`

**请求体：**

```jsonc
{
  "message": "素材库里有没有雨夜的素材？有的话用它做参考图生成一段 5 秒 16:9 的视频",
  "session_id": "可选；不给则新建会话",
  "provider_id": "可选；不给则用「本地模型配置」里的默认 Provider",
  "max_steps": 8          // 可选，默认 8，上限 8
}
```

**返回体（结构化输出，实际字段）：**

```jsonc
{
  "session_id": "33511fb8c8b64bd1",
  "status": "completed",        // completed | needs_clarification | truncated
  "answer": "给用户看的中文结论",
  "jobs": [{                    // 已下发的生成任务（job_id 由服务端生成）
    "job_id": "22ea12d894e9", "mode": "t2v", "label": "文生视频",
    "prompt": "…", "params": {…}, "asset_refs": [], "status": "queued",
    "summary": "…"
  }],
  "assets": [{ "asset_id": "…", "name": "…", "category": "image",
               "tags": [], "description": "…" }],
  "prompt": { "original": "…", "enhanced": "…", "spec_id": "video",
              "validation": {…} },         // 没有增强过就是 null
  "steps": [{ "step": 1, "tool": "search_assets", "ok": true, "ms": 30185,
              "args": "…", "error": null }],
  "usage": { "steps": 6, "tool_calls": 6, "elapsed_ms": 292197 },
  "provider": { "name": "…", "model": "…" },
  "provider_name": "…",
  "error": null                 // 截断/失败时的可读原因
}
```

**并发闸门**：同时最多 2 个 Agent 会话；超出的请求返回 **429**
（`{"error": "…", "code": "too_many_requests"}`），不会排队堆积。

**响应体：** 见 §3.2 ④ 的结构化输出。

### 4.2 `GET /api/agent/sessions/{id}`

取会话历史（前端刷新页面后恢复对话）。

### 4.3 `DELETE /api/agent/sessions/{id}`

清空会话。

### 4.4 `GET /api/agent/tools`

列出当前 Agent 的工具清单与 schema（前端展示 / 调试用）。

---

## 5. 主链路：自然语言意图 → 可执行生成任务

以「素材库里有没有雨夜的素材？有的话用它做参考图生成一段 5 秒 16:9 的视频」为例：

```
① 模型调用 search_assets(query="雨夜", category="image")
   → 复用 asset_repo.search_assets → 命中素材或未命中

② 模型调用 create_job(mode="r2v", prompt="…", aspect="16:9",
                       duration=5, asset_id="asset_xxx")
   → 服务端：
      · 校验 mode ∈ BUILDERS                        → 否则 400 可读错误
      · 校验 asset_id 存在且是图片                   → 否则 400 可读错误
      · 组 params（含 ref_images / aspect / duration）→ 复用 workflows 的默认值
      · 调 server 侧与 /api/jobs **同一套**建任务函数 → 写 jobs 表（queued）
   → 返回 {"job_id": "…", "status": "queued"}

③ 模型调用 respond(summary="…", job_ids=["…"])
   → 编排器把 job_id 映射成结构化 jobs 条目（含 mode 标签 / prompt / params）

④ 服务端返回结构化结果：answer + jobs[1] + steps[3] + usage
```

**生成的产物在任务列表里可见**（因为走的是同一个 jobs 表 + 同一个 Worker）。

---

## 5.1 实测记录（真实调用，非模拟）

> 测试实例：独立 worktree + 临时数据目录 + 独立端口 8307，**没有动线上 8190 服务**。

### 演示 A：素材检索 → 提示词增强 → 下发文生视频

请求：`POST /api/agent/chat`
```json
{"message": "素材库里有没有雨夜的素材？如果有，就用它做参考图，生成一段 5 秒、16:9 的文生视频。"}
```

模型实际走的 6 步（`steps` 轨迹原文）：

| 步 | 工具 | 耗时 | 结果 |
|---|---|---|---|
| 1 | `search_assets({"query":"雨夜"})` | 30.2s | 空（素材库确实没有雨夜素材） |
| 2 | `search_assets({"query":"rain night 夜晚 下雨"})` | 51.1s | 仍空 → 模型换关键词重试 |
| 3 | `list_modes({})` | 21.3s | 拿到后端真实注册的模式清单 |
| 4 | `enhance_prompt({...,"mode":"t2v"})` | 110.7s | 扩写 + 硬约束校验通过 |
| 5 | `create_job({"mode":"t2v","prompt":"...","aspect":"16:9","duration":5})` | 16.7s | **真实建任务** |
| 6 | `respond({"summary":"..."})` | 62.3s | 交结论 |

结构化结果（节选，完整见交付报告）：
```json
{
  "status": "completed",
  "answer": "素材库里没有找到「雨夜」相关的素材（换关键词二次检索仍为空）……我改用「文生视频」模式直接下发了任务……",
  "jobs": [{
    "job_id": "22ea12d894e9",
    "mode": "t2v",
    "label": "文生视频",
    "params": {"aspect": "16:9", "duration": 5.0, "gen_type": "t2v"},
    "status": "queued"
  }],
  "usage": {"steps": 6, "tool_calls": 6, "elapsed_ms": 292197}
}
```

**关键校验**（不能只看响应体 —— 响应里的 id 可能是模型编的）：
```
job_id=22ea12d894e9  在任务库中: mode=t2v status=running gen_type=t2v
  参数键: ['aspect', 'duration', 'gen_type', 'prompt']
```
→ 任务真的落进了 `jobs` 表，Worker 已取走（status=running），产物会出现在「生成」列表。

### 演示 B：多轮对话 + 素材命中 → 图片编辑 → 换时长换模式

| 轮 | 用户输入 | 模型行为 | 结果 |
|---|---|---|---|
| 1 | 「用『雷神三视图』做参考图，改成夜景雷雨造型，竖版 9:16」 | 搜 → 查详情 → 扩写 → `create_job(i2i_qwen)` → `respond` | job `ecd4791fb998`，mode=i2i_qwen，status=running |
| 2 | 「时长改成 8 秒再出一版」（同一 session_id） | 查模式 → `create_job(r2v, duration=8)` → `respond` | job `53a34aa3462f`，mode=r2v，duration=8.0 |

第 2 轮模型**自己判断**「图片编辑没有时长概念，要 8 秒就得走视频模式」，于是换成 r2v。
这正是「多轮 + 多步」相对「一问一答」的价值。

### 实测中暴露并已修复的 4 个真问题

| # | 问题 | 后果 | 修法 |
|---|---|---|---|
| 1 | `asset_refs` 没写进 `params` | 任务入队了但**参考图是空的**（静默失效，最难查） | 建任务前显式写回 `params["asset_refs"]` |
| 2 | `list_modes` 报了 `longfilm` | 模型拿到一个「看得到、用不了」的模式 | 模式白名单改用 `workflows.BUILDERS`（= `create_job` 的白名单） |
| 3 | `list_modes` 硬编码时长白名单 `[5,10,15]` | 模型据此以为「8 秒不支持」而**拒绝执行** | 改成 `duration_range=[5,15]` + 说明「任意秒数」 |
| 4 | 模型只做口头承诺不调工具 | 用户收到「我先增强提示词…」而不是任务 | 加「口头承诺防护」：推它继续调工具 |

### 前端面板实测（真实浏览器）

- 导航入口存在、点击后 Agent 页显示、创作区隐藏、切回创作页正常
- 375px 窄屏**横向溢出 0 像素**，发送按钮转为整行
- 浏览器 console **零 JS 错误**

---

## 6. 与既有能力的复用清单（明确「不重写」）

| 既有模块 | Agent 怎么用 | 复用的函数 |
|---|---|---|
| 本地模型 Provider | 取默认 / 指定模型，含 api_key | `providers.resolve_default` / `providers.get_provider_full` |
| LLM 调用 | 发消息、拿文本、thinking 处理 | `llm.call_provider`（+ 新增 `call_provider_with_tools`） |
| 提示词增强 | 扩写提示词 + 硬约束校验 | `enhancer.build_messages` / `enhancer.validate_output` / `enhancer.strip_thinking` |
| 素材库检索 | 关键词 / 标签 / 类型搜索 | `asset_repo.search_assets` |
| 素材详情 | 描述 / 标签 / 分析结论 | `asset_repo.latest_analysis` / `ref_api._ref_entry` |
| 生成模式注册表 | 模式白名单 + 标签 | `workflows.BUILDERS` / `gen_types.list_types` |
| 工作流参数默认值 | 各模式缺省参数 | `workflows.Z_IMAGE_MODELS` 等 |
| 建任务 + Worker | 真正下发生成 | `server.api_create` 的建任务段 + `Worker` |

**Agent 自己新增的只有三样**：
1. 工具注册表与编排器（`h3studio/agent.py`）
2. `llm.call_provider_with_tools`（tools 协议，原函数不动）
3. 会话存储（`h3studio/agent_sessions.py`，独立小库，不碰 jobs.db 表结构）

---

## 7. 已实现 / 未实现边界

### 7.1 本版本已实现

- [x] `POST /api/agent/chat`：多轮 / 多步工具调用编排
- [x] 6 个工具：`search_assets` / `list_modes` / `describe_asset` /
      `enhance_prompt` / `create_job` / `respond`
- [x] 结构化输出（jobs / assets / prompt / plan / steps / usage）
- [x] 会话持久化 + 历史裁剪 + 工具结果裁剪
- [x] 超时（总 300s / 单步 180s）、步骤上限（6）、并发闸门（2）、
      重复调用检测
- [x] 可读错误（不挂死）
- [x] 前端面板入口（左侧导航「内置 Agent」）：对话区 + 结果卡 +
      「填入创作面板」按钮

### 7.2 本版本**未**实现（明确不做，不臆造）

- ✗ **不做**真正的图像/视频/音频内容理解推理（不自己跑 VLM 抽帧分析）。
  素材的「内容描述」来自素材库**已有的**模型分析结果（`asset_analyses` 表）。
  Agent 的多模态定位是「**编排**音视频图像任务」，不是「自己看片」。
- ✗ **不做**Agent 自主决定跑 ComfyUI 直出结果。Agent 只创建任务，
  生成由既有 Worker 执行，用户在任务列表里看结果。
- ✗ **不做**素材的上传 / 删除 / 标签写入（只读检索 + 建任务）。
- ✗ **不做**长片拼接（`longfilm`）的自动编排。
- ✗ **不做**跨会话的长期记忆。
- ✗ **不做**流式（SSE）输出 —— 单次请求返回完整结构化结果。
- ✗ **不支持** `comfyui` 类型 Provider 走 tools 协议（该协议不支持工具调用，
  明确报错而不是静默降级）。

---

## 8. 后续可扩展点

1. **工具扩展**：新增 `analyze_asset`（触发素材库深度分析）、
   `compare_assets`、`suggest_params`（用本机 LoRA/档位表给参数建议）。
2. **流式**：把编排器的步骤事件推到 SSE，前端实时显示「正在检索素材…」。
3. **记忆**：把「用户偏好的风格/常用模式」落到 `agent_sessions.db` 的用户级表。
4. **批量**：Agent 一次规划多个任务（如「生成 3 个不同角度」）。
5. **工具鉴权**：`create_job` 目前对局域网客户端开放，与面板同权限；
   若将来需要，可加「Agent 专用额度」。

---

## 9. 文件清单

| 文件 | 作用 |
|---|---|
| `h3studio/agent.py` | Agent 服务层：工具注册表 + 编排器 + 会话逻辑 |
| `h3studio/agent_sessions.py` | 会话与消息历史存储（独立 SQLite） |
| `h3studio/llm.py` | **新增** `call_provider_with_tools()`（原函数不动） |
| `server.py` | **新增** `/api/agent/*` 路由 |
| `web/agent.js` | 前端 Agent 面板（对话区 + 结果卡 + 填入面板） |
| `web/index.html` | 新增导航入口 + 面板 DOM |
| `web/style.css` | Agent 面板样式 |
| `tests/test_agent.py` | Agent 编排器 / 工具 / 参数收敛 的回归测试 |
| `docs/内置多模态Agent-架构设计.md` | 本文档 |
