# 内置多模态 Agent —— 交付报告

> 项目：ComfyUI-MiniMax-H3-Studio（OmniStudio）
> 分支：`local/feature/builtin-agent`
> worktree：`/root/projects/h3studio-builtin-agent`
> 日期：2026-11-09
> 设计文档：[`docs/内置多模态Agent-架构设计.md`](../docs/内置多模态Agent-架构设计.md)

---

## 0. 一句话结论

在项目内建成一个**内置的多模态 Agent**：用一句自然语言描述创作意图，
它自己查素材库 → 选生成模式 → 扩写提示词 → **组装现有 ComfyUI 工作流参数 →
下发成真实可执行的生成任务**，并返回结构化结果。

**主链路已跑通并验证**：意图 → 6 步工具调用 → 任务落库 → Worker 接手（status=running）
→ 产物出现在「生成」列表。**没有改动线上 8190 服务与 systemd unit。**

---

## 1. 为什么需要它（现状的三个具体缺口）

不是「模型不够强」，是**缺编排层**。本项目已有的能力其实很全：

| 已有能力 | 位置 | 现状问题 |
|---|---|---|
| 本地模型 Provider（4 种协议 + 多模态标记） | `h3studio/providers.py` | ✅ 完备 |
| 工作流拼装（9 种生成模式） | `h3studio/workflows.py` | ✅ 完备 |
| 素材库（23 个路由 + 搜索/标签/分析/纠错） | `h3studio/asset_api.py` | ✅ 完备 |
| 提示词增强迷你 Agent（模板/参数/校验器） | `h3studio/enhancer.py` | ✅ 完备 |
| **一问一答的调用入口** | `server.py:api_enhance` | ❌ **拿 prompt 调一次模型就返回**，全程没有工具通道 |
| **参数搬运全靠手工** | `web/app.js` 提交逻辑 | ❌ 扩写结果要自己切模式/填参数/点生成 |
| **多模态只用于「读图描述」** | `h3studio/analysis.py` | ❌ 三个 Analyzer 产出只是一段描述文本，不参与任何参数决策 |

所以本工作**不重造模型调用、不重造工作流、不重造素材检索**，
只加一层**编排**：工具注册表 + 编排器 + 结构化输出。

---

## 2. 架构

```
前端面板 web/agent.js（左侧导航「内置 Agent」）
    ↓ POST /api/agent/chat
Agent 服务层 h3studio/agent.py
    ├─ ① 模型接入   llm.call_provider_with_tools（新增，原 call_provider 不动）
    ├─ ② 工具注册表 6 个工具，白名单制
    ├─ ③ 上下文/多轮 agent_sessions.py（独立库，按轮裁剪）
    ├─ ④ 结构化输出 jobs / assets / prompt / steps / usage
    └─ ⑤ 编排与错误处理 超时 420s / 步骤 8 / 并发 2 / 重复检测
    ↓
既有能力（全部复用，一个都没重写）
    asset_repo 检索 · enhancer 扩写+校验 · workflows.BUILDERS 模式白名单
    server.prepare_job_params + create_job_record（与面板点「生成」同一套）
```

### 6 个工具

| 工具 | 作用 | 复用来源 | 副作用 |
|---|---|---|---|
| `search_assets` | 按关键词/类型检索素材 | `asset_repo.search_assets` | 只读 |
| `list_modes` | 列出**能实际下发**的生成模式 | `workflows.BUILDERS` | 只读 |
| `describe_asset` | 素材详情 + 已有分析结论 | `asset_repo` + `ref_api._ref_entry` | 只读 |
| `enhance_prompt` | 用工作台增强模板扩写 + 硬约束校验 | `enhancer.*` + `llm.call_provider` | 只读 |
| `create_job` | 创建生成任务（唯一写操作） | `server.prepare_job_params` + `create_job_record` | **写任务队列** |
| `respond` | 提交最终结论 | — | 只读 |

### 复用清单（明确「不重写」）

- 本地模型：`providers.resolve_default` / `get_provider_full`
- LLM 调用：`llm.call_provider_with_tools`（**新增**，原 `call_provider` 一行未动，
  增强与分析链路零影响）
- 提示词增强：`enhancer.build_messages` / `validate_output` / `strip_thinking`
  （与「增强」按钮同一套模板与校验）
- 素材检索：`asset_repo.search_assets`
- 参考图元数据：`ref_api._ref_entry`（与面板选参考图同一套描述语义）
- 模式白名单：`workflows.BUILDERS`
- 建任务：`server.prepare_job_params`（校验）+ `server.create_job_record`（落库）
  —— **抽成共享函数**，面板 `/api/jobs` 与 Agent `create_job` 走同一条路

---

## 3. 关键接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/agent/chat` | 主入口。`{message, session_id?, provider_id?, max_steps?}` |
| GET | `/api/agent/tools` | 工具清单 + schema + 限额（调试用） |
| GET | `/api/agent/sessions` | 会话列表 |
| GET | `/api/agent/sessions/{id}` | 会话完整消息历史 |
| DELETE | `/api/agent/sessions/{id}` | 清空会话 |

**结构化输出**（前端按这个渲染，字段稳定）：

```jsonc
{
  "session_id": "…",
  "status": "completed | needs_clarification | truncated",
  "answer": "给用户看的中文结论",
  "jobs":    [{ "job_id", "mode", "label", "prompt", "params", "asset_refs", "status", "summary" }],
  "assets":  [{ "asset_id", "name", "category", "tags", "description", "analyzed" }],
  "prompt":  { "original", "enhanced", "spec_id", "validation" } | null,
  "steps":   [{ "step", "tool", "ok", "ms", "args", "error", "note" }],
  "usage":   { "steps", "tool_calls", "elapsed_ms" },
  "provider": { "name", "model" },
  "error":   "截断/失败时的可读原因 | null"
}
```

### 契约保证（3 条硬规则）

1. `jobs[]` 里的 `job_id` **必须**是服务端 `create_job_record` 真实生成的 —— 模型编不出来。
   （有回归测试：模型自报 `FAKE-ID-123` 不会进 `jobs[]`。）
2. `mode` **必须**在 `workflows.BUILDERS` 里 —— 面板能建的模式，Agent 才能建。
3. 拿不到的字段**一律省略**，不填占位假值。

---

## 4. 质量与安全约束

| 约束 | 值 | 依据 |
|---|---|---|
| 单次请求总超时 | 420 秒（`asyncio.wait_for` 兜整轮） | 实测成功链路 6 步共约 290 秒 |
| 单步模型超时 | 180 秒 | 与 `llm.CHAT_TIMEOUT` 一致 |
| 最大步骤数 / 工具调用数 | 8 / 8 | 实测完整成功链路要 6 轮 |
| 并发上限 | 同时 2 个会话，超出 429 | 本地模型单卡 |
| 重复调用检测 | 第 2 次给纠偏提示，第 3 次判定打转 | 实测模型会连续 6 步调同一工具 |
| 口头承诺防护 | 只叙述不调工具 → 推它继续调 | 实测原话「我先增强提示词，再下发生成任务。」 |
| 工具异常 | 捕获后作为 tool 消息回给模型 | 让模型自己纠正参数 |
| 未知工具 | 明确报错，不执行任何东西 | 工具集是白名单 |
| `comfyui` 类型 Provider | 明确报错 | 该协议不支持 tools；静默降级会让 Agent 退化成「一问一答」 |

**失败不挂死**：超时/超限返回**已完成的部分** + 可读原因（`status: "truncated"`），
而不是抛异常或无限等待。

---

## 5. 真实调用演示（实际响应）

### 演示 A —— 素材检索 → 扩写 → 下发文生视频

```bash
curl -s -X POST http://127.0.0.1:8307/api/agent/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"素材库里有没有雨夜的素材？如果有，就用它做参考图，生成一段 5 秒、16:9 的文生视频。"}'
```

模型实际走的 6 步（`steps` 原文）：

| 步 | 工具 | 耗时 | 结果 |
|---|---|---|---|
| 1 | `search_assets({"query":"雨夜"})` | 30.2s | 空（库里确实没有雨夜素材） |
| 2 | `search_assets({"query":"rain night 夜晚 下雨"})` | 51.1s | 仍空 → 换关键词重试 |
| 3 | `list_modes({})` | 21.3s | 拿到真实注册的模式清单 |
| 4 | `enhance_prompt({...,"mode":"t2v"})` | 110.7s | 扩写 + 硬约束校验通过 |
| 5 | `create_job({"mode":"t2v","aspect":"16:9","duration":5,...})` | 16.7s | **真实建任务** |
| 6 | `respond({"summary":"..."})` | 62.3s | 交结论 |

响应（节选）：

```json
{
  "status": "completed",
  "answer": "素材库里没有找到「雨夜」相关的素材（换关键词二次检索仍为空），所以无法用参考图生成。我改用「文生视频」模式直接下发了任务：一段 5 秒、16:9 的雨夜城市街道镜头……",
  "jobs": [{
    "job_id": "22ea12d894e9",
    "mode": "t2v",
    "label": "文生视频",
    "params": {"aspect": "16:9", "duration": 5.0, "gen_type": "t2v"},
    "asset_refs": [],
    "status": "queued",
    "summary": "雨夜城市街道霓虹倒影的文生视频（无参考素材，纯文本生成）"
  }],
  "prompt": {
    "original": "雨夜城市街道，湿滑路面反射霓虹灯光，行人撑伞走过，电影感氛围",
    "enhanced": "镜头缓慢向前推进，低角度贴近地面拍摄一条雨夜的城市街道。沥青路面完全被雨水覆盖，呈现出镜面般的反光质感……",
    "spec_id": "video",
    "validation": {"ok": true, "errors": [], "warnings": ["……"]}
  },
  "usage": {"steps": 6, "tool_calls": 6, "elapsed_ms": 292197},
  "provider": {"name": "本机 Qwen 多模态", "model": "qwen3.8-flash-next-iq3_xxs"}
}
```

**关键校验**（不能只看响应体 —— 响应里的 id 可能是模型编的）：

```
job_id=22ea12d894e9  在任务库中: mode=t2v status=running gen_type=t2v
  参数键: ['aspect', 'duration', 'gen_type', 'prompt']
```

→ 任务真的落进了 `jobs` 表，Worker 已取走（`status=running`），产物会出现在「生成」列表。

### 演示 B —— 多轮对话 + 素材命中 → 图片编辑 → 换时长换模式

同一 `session_id` 连续两轮：

| 轮 | 用户输入 | 模型行为 | 结果 |
|---|---|---|---|
| 1 | 「用『雷神三视图』做参考图，改成夜景雷雨造型，竖版 9:16」 | 搜 → 查详情 → 扩写 → `create_job(i2i_qwen)` → `respond` | job `ecd4791fb998`，mode=i2i_qwen，status=running，params 含 `ref_images`（素材库来源） |
| 2 | 「时长改成 8 秒再出一版」 | 查模式 → `create_job(r2v, duration=8)` → `respond` | job `53a34aa3462f`，mode=r2v，duration=8.0 |

第 2 轮模型**自己判断**「图片编辑没有时长概念，要 8 秒就得走视频模式」，于是换成 r2v。
这正是「多轮 + 多步」相对「一问一答」的价值。

### 前端面板实测（真实浏览器，Playwright + Chromium）

- 左侧导航有「内置 Agent」入口，点击后 Agent 页显示、创作区隐藏、导航高亮同步
- 空态提示、输入框、发送按钮均正常
- 切回创作页 → Agent 页正确隐藏，创作区恢复
- **375px 窄屏横向溢出 0 像素**，发送按钮转整行
- 浏览器 console **零 JS 错误**

---

## 6. 实测暴露并已修复的 4 个真问题

| # | 问题 | 后果 | 修法 |
|---|---|---|---|
| 1 | `asset_refs` 没写进 `params` | 任务入队了但**参考图是空的**（静默失效，最难查） | 建任务前显式写回 `params["asset_refs"]` |
| 2 | `list_modes` 报了 `longfilm` | 模型拿到一个「看得到、用不了」的模式 | 模式白名单改用 `workflows.BUILDERS`（= `create_job` 的白名单） |
| 3 | `list_modes` 硬编码时长白名单 `[5,10,15]` | 模型据此以为「8 秒不支持」而**拒绝执行** | 改成 `duration_range=[5,15]` + 说明「任意秒数」 |
| 4 | 模型只做口头承诺不调工具 | 用户收到「我先增强提示词…」而不是任务 | 加「口头承诺防护」：推它继续调工具 |

第 1 条是**静默失效**类 bug（任务成功入队但参数是空的），单靠读响应体发现不了 ——
是回归测试 `test_repeated_identical_asset_refs_are_deduplicated_by_ordinal` 抓到的。

---

## 7. 测试

**新增** `tests/test_agent.py` —— **38 个用例，全部通过**：

```
38 passed in 0.31s
```

覆盖的都是**会咬人的地方**，不是「跑通就算」：

| 组 | 覆盖内容 |
|---|---|
| 工具注册表 | 6 个工具都在；schema 是 OpenAI 格式；`create_job` 的 mode 枚举 == `workflows.BUILDERS`；未知工具无可执行 handler |
| 参数收敛 | 未知模式 / 空提示词 / 非法比例 / i2i_qwen 无参考图 / a2a 无参考音频 → 全部带可读错误拒绝，且**不落库**；多参考图保持顺序与 ordinal |
| 编排器 | 未知工具不执行；重复调用第 2 次给纠偏提示、第 3 次才收尾；`respond` 后立即结束（不再多花 60~120s）；工具调用上限；**job_id 必须来自服务端**（模型自报的假 id 进不来）；输出结构稳定；mode 必在 BUILDERS 内 |
| 口头承诺防护 | 识别「我先…」「接下来…」等叙述；不误判真实结论；用完工具后只做承诺会被推着继续调工具 |
| list_modes | 时长是区间不是假白名单；报出来的模式都能被 `create_job` 建 |
| 会话存储 | 创建幂等；**按轮裁剪而不是按条**；裁剪后首条必须是 user；标题取首条用户消息；删除清空；超长消息截断 |
| 限额 | 超时/步数/工具数/并发都是有限值；重复上限 ≥ 3 |

**全量回归**（排除两个需真实浏览器/大环境的套件）：

```
python3 -m pytest tests/ -q --ignore=tests/test_qwen_image.py --ignore=tests/test_frontend_defaults.py
```

见 §9 的基线对比结论。

---

## 8. 已实现 / 未实现边界

### 已实现

- [x] `POST /api/agent/chat`：多轮 / 多步工具调用编排
- [x] 6 个工具（白名单）
- [x] 结构化输出（jobs / assets / prompt / steps / usage / provider / error）
- [x] 会话持久化 + 按轮裁剪 + 工具结果裁剪（600 字符上限）
- [x] 超时 420s、单步 180s、步骤 8、并发 2、重复调用检测、口头承诺防护
- [x] 可读错误（不挂死）
- [x] 前端面板：导航入口 + 对话区 + 结构化结果卡 + 「填入创作面板」按钮
- [x] 375px 窄屏适配

### **未**实现（明确不做，不臆造）

- ✗ **不做**真正的音/视频/图像内容理解推理。素材的「内容描述」来自素材库**已有的**
  模型分析结果（`asset_analyses` 表）。Agent 的多模态定位是「**编排**音视频图像任务」，
  不是「自己看片」。要自己看片需要接 `analysis.py` 的深度管线（另立任务）。
- ✗ **不做**Agent 自主跑 ComfyUI 直出结果。Agent 只创建任务，生成由既有 Worker 执行。
- ✗ **不做**素材的上传 / 删除 / 标签写入（只读检索 + 建任务）。
- ✗ **不做**长片拼接（`longfilm`）的自动编排 —— 它不走 `workflows.BUILDERS`，
  所以**刻意不**出现在 `list_modes` 里（避免给模型一个用不了的模式）。
- ✗ **不做**跨会话的长期记忆（会话历史只在本会话内有效）。
- ✗ **不做**流式（SSE）输出 —— 单次请求返回完整结构化结果。
- ✗ **不支持** `comfyui` 类型 Provider 走 tools 协议（明确报错，不静默降级）。

---

## 9. 后续可扩展点

1. **工具扩展**：`analyze_asset`（触发素材库深度分析，纳入「自己看片」能力）、
   `compare_assets`、`suggest_params`（用本机 LoRA/档位表给参数建议）。
2. **流式**：把编排器的步骤事件推到 SSE，前端实时显示「正在检索素材…」。
   （现在前端只能看到「处理中…」一句话。）
3. **记忆**：把「用户偏好的风格/常用模式」落到用户级表。
4. **批量**：一次规划多个任务（如「生成 3 个不同角度」）。
5. **工具鉴权**：`create_job` 目前与面板同权限，可加「Agent 专用额度」。

---

## 10. 文件清单

| 文件 | 增/改 | 作用 |
|---|---|---|
| `h3studio/agent.py` | **新增** | Agent 服务层：工具注册表 + 编排器 + 结构化输出 |
| `h3studio/agent_sessions.py` | **新增** | 会话与消息历史存储（独立 SQLite `data/agent_sessions.db`） |
| `h3studio/llm.py` | 改 | **新增** `call_provider_with_tools()`；原 `call_provider` 一行未动 |
| `server.py` | 改 | **新增** `/api/agent/*` 路由；**抽出** `prepare_job_params` + `create_job_record` 供面板与 Agent 共用 |
| `web/agent.js` | **新增** | 前端 Agent 面板 |
| `web/index.html` | 改 | 导航入口 + 面板 DOM + `agent.js` 引用 |
| `web/app.js` | 改 | `showAgent()` 页面切换；其它页面切走时隐藏 Agent 页 |
| `web/assets.js` | 改 | 素材库页切走时隐藏 Agent 页 |
| `web/style.css` | 改 | Agent 面板样式 |
| `web/responsive.css` | 改 | Agent 面板窄屏适配 |
| `tests/test_agent.py` | **新增** | 38 个回归用例 |
| `docs/内置多模态Agent-架构设计.md` | **新增** | 架构设计文档 |

**没有改动**：`h3studio/workflows.py`（工作流拼装层）、`h3studio/enhancer.py`、
`h3studio/asset_repo.py`、`h3studio/analysis.py` 等既有能力模块 —— 全部**复用**，未重写。

---

## 11. 部署说明

当前**只提交在分支 `local/feature/builtin-agent`**，**未合并到 `local/maintainer`**，
**未重启线上 8190 服务**（按要求）。

上线步骤（由 willis 决定执行）：

1. 合并分支到 `local/maintainer`（或直接部署该分支）。
2. 重启 `h3-studio.service`（前端静态文件立即生效，后端需要重启）。
3. 确认「本地模型配置」里已配置并启用了至少一个**支持 tools 协议**的 Provider
   （OpenAI 兼容 / Ollama / llama.cpp）。
   `comfyui` 类型不支持工具调用，会明确报错。
4. 新增数据文件 `data/agent_sessions.db`（会话历史，可随时删除，不影响生成任务）。
