# 内置 Agent 能力清单与配置说明

> **项目**：ComfyUI-MiniMax-H3-Studio（内置多模态 Agent 工作台）
> **分支**：`local/feature/agent-power`（worktree `/root/projects/h3studio-agent-power`，基线 `local/maintainer`）
> **产出日期**：2026-11-14
> **对应用户审查目标**：4 / 5 / 6 / 7（内置 agent 全面检查与增强）
>
> 【本文件的用法】
> * 「是否内置」= 代码在本仓库里（不需要额外装插件）；
> * 「默认启用」= 装好后开箱即用的状态；
> * 「外部依赖」= 需要本机额外具备什么（ffmpeg 路径 / 搜索引擎地址）；
> * 「验证证据」= 真跑出来的结果，不是设计意图。

---

## 0. 一页速查

| # | 审查目标 | 状态 | 关键落点 |
|---|---|---|---|
| 1 | 工具调用步数上限 8 → ≥50（前后端一致） | ✅ 已实现并验证 | `h3studio/agent.py`（MAX_STEPS / MAX_TOOL_CALLS / TOTAL_TIMEOUT）、`h3studio/agent_settings.py`（硬顶 50） |
| 2 | 新增 ffmpeg 音视频能力（合并/拼接、转码、抽帧、混音） | ✅ 已实现并验证（真跑 ffmpeg） | `h3studio/agent_media_tools.py` + `h3studio/agent_extra_tools.py` |
| 3 | 浏览器交互 agent 支持剪贴板粘贴媒体 | ✅ 已实现并验证（真浏览器） | `web/agent.js`（handlePaste） |
| 4 | 额外能力可开关 + 搜索引擎可配置（DDG / 9898）+ 连通性自检 | ✅ 已实现并验证 | `h3studio/agent_settings.py`、`h3studio/agent_websearch.py`、`h3studio/agent_codeexec.py`、`server.py`（/api/agent/settings*）、`web/agent-manage.js` |
| 5 | 文生视频默认 comfy kitchen 注意力 + dmad 加速 LoRA，且参数可改 | ✅ 已实现并验证（工作流里真出现该节点与该 LoRA） | `h3studio/agent_settings.py`（DEFAULT_T2V_DEFAULTS）、`server.py`（_merge_t2v_defaults / prepare_agent_t2v_params） |
| 6 | 产出本能力清单 | ✅ 本文件 | — |

**总体**：工具总数 23 → **32**；模型可见工具数按能力开关在 **25 / 31** 之间浮动。

---

## 1. 能力总表（32 个工具）

### 1.1 项目内置能力（23 个，常驻可用）

这些是工作台原有的核心能力，**不依赖任何外部服务**，也不受能力开关影响。

| 工具名 | 能力 | 是否内置 | 默认启用 | 外部依赖 | 调用方式 | 验证证据 |
|---|---|---|---|---|---|---|
| `search_assets` | 素材检索 | 是 | 是 | 无（素材库） | 参数 `query` / `category` / `limit` | `tests/test_agent_backend_enh.py` 工具注册与 schema 断言 |
| `list_modes` | 模式清单 | 是 | 是 | 无 | 无参数 | 同上 |
| `describe_asset` | 素材详情 | 是 | 是 | 无 | `asset_id` | 同上 |
| `enhance_prompt` | 提示词增强 | 是 | 是 | 无（本地模板） | `prompt` / `mode` | 同上 |
| `create_job` | 下发生成任务 | 是 | 是 | ComfyUI（8188） | `mode` / `prompt` / 可选参数 | `tests/test_agent.py::TestAgentJobValidation`（真实落库） |
| `respond` | 提交结论 | 是 | 是 | 无 | `summary` | 同上 |
| `upload_asset` | 素材入库 | 是 | 是 | 无（素材库） | `path`（库内相对路径） | `tests/test_agent_backend_enh.py` |
| `delete_asset` | 删素材 | 是 | 是 | 无 | `asset_id` / `purge` | 同上 |
| `tag_asset` | 素材标签 | 是 | 是 | 无 | `asset_id` + `add/remove/replace` | 同上 |
| `list_asset_tags` | 标签清单 | 是 | 是 | 无 | `limit` | 同上 |
| `cancel_job` | 取消任务 | 是 | 是 | 无 | `job_id` | 同上 |
| `retry_job` | 重试任务 | 是 | 是 | 无 | `job_id` / `overrides` | 同上 |
| `batch_create_jobs` | 批量建任务 | 是 | 是 | ComfyUI（8188） | `jobs[]` | 同上 |
| `list_jobs` | 任务历史 | 是 | 是 | 无（jobs.db） | `status`/`mode`/`since_hours`/`limit` | 同上 |
| `get_job_status` | 任务进度 | 是 | 是 | 无（jobs.db） | `job_id` | 同上 |
| `list_prompt_presets` | 提示词预设清单 | 是 | 是 | 无 | 无参数 | 同上 |
| `get_prompt_preset` | 预设详情 | 是 | 是 | 无 | `spec_id` | 同上 |
| `describe_mode_params` | 模式参数说明 | 是 | 是 | 无 | `mode` / `field` | 同上 |
| `get_mode_schema` | 模式 schema | 是 | 是 | 无 | `mode` | 同上 |
| `recall_memory` | 跨会话偏好 | 是 | 是 | 无（agent_memory.db） | `query`/`scope`/`limit` | 同上 |
| `search_conversations` | 历史对话检索 | 是 | 是 | 无（agent_sessions.db） | `query` | 同上 |
| `search_asset_history` | 素材历史 | 是 | 是 | 无（素材库） | `asset_id`/`query` | 同上 |
| `remember_preference` | 记住偏好 | 是 | 是 | 无 | `key`/`value`/`scope` | 同上 |

### 1.2 额外能力（9 个，由「agent 管理」的能力开关控制）

**关键机制**：关掉的能力，其工具**不会出现在给模型的工具清单里**（模型连工具名都看不到，
不会反复尝试一个用不了的工具）。这与「工具还在、调用时报错」是两种不同的行为。

| 工具名 | 能力 key | 是否内置 | 默认启用 | 外部依赖 | 调用方式 | 验证证据 |
|---|---|---|---|---|---|---|
| `read_file` | `file_read` | 是 | ✅ 开 | 无（受 `file_roots` 白名单限制） | `path` / `max_bytes` | `tests/test_agent_power.py::TestFileRead`（真读文件 + 越界拒绝） |
| `web_search` | `web_search` | 是 | ✅ 开 | 网络可达的搜索引擎（DDG / 9898 / 自定义） | `query` / `count` | `tests/test_agent_power.py::TestWebSearch`（真打 9898，断言有真实标题+链接） |
| `run_code` | `code_exec` | 是 | ⛔ **关** | 无 | `code` / `timeout_sec` | `tests/test_agent_power.py::TestCodeExec`（真跑 Python，含超时 kill） |
| `media_probe` | `media_tools` | 是 | ✅ 开 | ffmpeg / ffprobe | `asset_id` | `tests/test_agent_power.py::TestMediaToolsReal`（ffprobe 真读时长/分辨率/编码） |
| `media_concat` | `media_tools` | 是 | ✅ 开 | ffmpeg / ffprobe | `asset_ids[]` / `output_name` | 同上（2 段真实视频拼成 5 秒产物，文件落盘可查） |
| `media_transcode` | `media_tools` | 是 | ✅ 开 | ffmpeg / ffprobe | `asset_id` / `output_format` / `video_codec` / `audio_codec` / `crf` | 同上（真实产出 .webm） |
| `media_extract_frames` | `media_tools` | 是 | ✅ 开 | ffmpeg / ffprobe | `asset_id` / `interval_sec` / `max_frames` | 同上（真实抽出 N 帧，每帧独立入库） |
| `media_mix_audio` | `media_tools` | 是 | ✅ 开 | ffmpeg / ffprobe | `video_asset_id` / `audio_asset_ids[]` / `volumes[]` / `replace` | 同上（真实混音产物） |
| `media_merge_av` | `media_tools` | 是 | ✅ 开 | ffmpeg / ffprobe | `video_asset_id` / `audio_asset_id` | 同上（真实 AV 合流产物） |

**`code_exec` 为什么默认关闭**：这是本清单里**唯一有副作用**的能力（执行任意 Python）。
默认打开等于「模型一句话就能在本机跑代码」。开启后仍有边界：独立进程 + 超时 kill
（整进程组）+ 输出裁剪 + 工作目录受限。开启时「agent 管理」面板会显示醒目黄色告警。

---

## 2. 能力开关（4 个）

| 能力 | 说明 | 默认 | 外部依赖 |
|---|---|---|---|
| **文件读取** `file_read` | 读工作台/仓库/素材库派生目录下的文本文件（查配置、日志、脚本） | 开 | 无（受 `file_roots` 路径白名单限制） |
| **外部搜索** `web_search` | 按配置的搜索引擎做外部检索 | 开 | 网络 + 搜索引擎地址 |
| **代码生成与执行** `code_exec` | 运行一段 Python 并拿回 stdout/stderr/退出码 | **关** | 无 |
| **ffmpeg 音视频处理** `media_tools` | 合并拼接 / 转码 / 抽帧 / 混音 / AV 合流 | 开 | 本机 ffmpeg / ffprobe |

### 2.1 配置项一览（`/api/agent/settings`）

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `max_steps` | int | `50` | 步数上限（1~50，硬顶见 `agent_settings.MAX_STEPS_HARD_CAP`） |
| `capabilities` | object | 见上表 | 4 个能力开关 |
| `search_engines` | array | 3 条（见 §3） | 搜索引擎清单，每条 `{id,name,type,url,method,enabled}` |
| `t2v_defaults` | object | 见 §5 | 文生视频默认参数 |
| `file_roots` | array | `[]` | 允许 `read_file` 读取的目录（绝对路径，最多 8 个；空 = 不允许任何路径） |
| `code_exec_timeout_sec` | int | `30` | 代码执行超时（5~300 秒） |
| `ffmpeg_path` | string | `""` | ffmpeg 可执行文件路径；留空 = 从 PATH 找 |

**存储位置**：服务端 SQLite `jobs.db` 的 `model_provider_prefs` 表，key = `agent_settings`。
局域网所有客户端共用同一份配置（不依赖浏览器 localStorage），改完**立即生效**、无需重启服务。

### 2.2 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/agent/settings` | 读配置 + 能力清单 + 限额 + 依赖探测（ffmpeg/ffprobe 路径） |
| PUT | `/api/agent/settings` | 写配置（非法值自动回落默认值，不整体拒绝） |
| POST | `/api/agent/settings/test-engine` | 搜索引擎连通性自检（**真发一次请求**） |
| GET | `/api/agent/tools` | 工具清单（含每个工具的 `capability` 与 `enabled`） |

### 2.3 前端入口

内置 Agent 页右上角 **「⚙ 管理」** 按钮 → 弹窗「内置 Agent 管理」，分四块：
步数上限 / 额外能力 / 外部搜索引擎 / 文生视频默认参数。

实现：`web/agent-manage.js` + `web/index.html`（模态框）+ `web/style.css`（样式）。

---

## 3. 外部搜索引擎配置

### 3.1 支持的引擎类型

| type | 协议 | 说明 |
|---|---|---|
| `openwebui` | POST JSON `{"query","count"}` → `{"results":[...]}` | 局域网 9898 聚合引擎的通用协议 |
| `ddg` | GET `?q=...` → HTML，解析 `result__a` / `result__snippet` | DuckDuckGo 裸 HTML 端点 |
| `custom` | POST JSON 或 GET `?q=&count=` → JSON 数组或 `{results/items/data/...}` | 用户自填的任意 HTTP 接口 |

### 3.2 默认清单（3 条，都可改 URL / 类型 / 方法 / 启用状态）

| id | 名称 | type | URL | 默认 |
|---|---|---|---|---|
| `local_9898` | 局域网聚合引擎（9898） | `openwebui` | `http://127.0.0.1:9898/api/openwebui/tools/search_general` | ✅ 开 |
| `ddg` | DuckDuckGo（经 9898 代理） | `custom` | `http://127.0.0.1:9898/api/openwebui/search/ddg` | ✅ 开 |
| `ddg_direct` | DuckDuckGo（直连，可能被反爬拦截） | `ddg` | `https://html.duckduckgo.com/html/` | ⛔ 关 |

**为什么 DDG 走 9898 代理**：实测本机直连 `https://html.duckduckgo.com/html/?q=...`
返回 **HTTP 202 + anomaly challenge**（反爬拦截），解析不到任何结果；
而 9898 聚合引擎背后**有** DDG 源，并提供单引擎入口
`/api/openwebui/search/ddg`（实测能出真实结果）。所以默认指向 9898 的 DDG 端点 ——
既满足「支持接入 DDG」，又真的能出结果。`ddg_direct` 保留为备用（换个网络环境 /
反爬策略变化时直接切过去），这同时也是「DDG 是可**填写**的 URL 而非硬编码」的证据。

**局域网访问**：默认 URL 写的是 `127.0.0.1`（本机自用）。
从别的机器访问本 Studio 时，把 URL 改成 `http://192.168.3.78:9898/api/openwebui/tools/search_general` 即可。

### 3.3 连通性自检

`POST /api/agent/settings/test-engine`，body `{"engine_id": "local_9898"}` 或 `{"engine": {...}}`。

**是真发一次请求**（query = 「H3 Studio 连通性测试」），不是只校验 URL 格式。

实测输出：

```json
{
  "ok": true,
  "engine": "局域网聚合引擎（9898）",
  "engine_type": "openwebui",
  "url": "http://127.0.0.1:9898/api/openwebui/tools/search_general",
  "result_count": 1,
  "note": "连通性正常（真发了一次请求）",
  "status": 200
}
```

失败时返回 502 + 每个引擎的失败原因（逐条列出），不吞错。

### 3.4 引擎回退策略

按配置顺序**逐个**尝试，第一个有结果的引擎即返回（**不并发打所有引擎** —— 那是浪费外部资源）。
全部失败时报错并逐条列出原因（`code=all_engines_failed`）。

---

## 4. ffmpeg 音视频处理能力

### 4.1 工具与对应 ffmpeg 能力

| 工具 | 能力 | 底层 ffmpeg 手法 |
|---|---|---|
| `media_probe` | 探测参数 | `ffprobe -show_format -show_streams` |
| `media_concat` | 合并/拼接 | concat demuxer（参数一致时免重编码）+ filter_complex 统一画布（不一致时） |
| `media_transcode` | 转码 | `-c:v`/`-c:a` + CRF / 码率 |
| `media_extract_frames` | 抽帧 | `-vf fps=1/N,scale=768:-2` |
| `media_mix_audio` | 混音 | `amix` + `aresample` + `aformat(fltp/stereo)` + `volume` |
| `media_merge_av` | 音视频合流 | `-map 0:v -map 1:a`（视频流 `-c:v copy` 不重编码） |

### 4.2 移植自「咏唱」插件的工程思路

参考源：`/home/ubuntu/nonebotqq/plugins/nonebot_plugin_novelai/`
（`h3_audio_video_merge.py`、`h3_audio_edit.py`、`h3_yue2_remix.py`、`h3_executor.py`）

移植的是**工程思路**，不是 nonebot 的耦合代码（事件对象 / 会话状态机 / QQ 文件下载 /
用户交互菜单 / 触发词一律不移植）：

1. **先 ffprobe 再拼命令** —— 时长/流信息一律实测，不用「猜」参数。
2. **concat 前统一视频时间刻度** —— 各源 `time_base` 不同（1/12288 vs 1/90000）时，
   `concat` + `-c copy` 会把 32 秒的片子拉成 236 秒。先无损 remux
   （`-c copy` + `-video_track_timescale 153600`）再拼。
3. **音轨存在性不齐先补等长静音** —— 否则 concat 时长对不齐。
4. **混音前统一采样率/声道/采样格式** —— `amix` 要求各路布局一致。
5. **分辨率不一致先统一画布** —— `scale(decrease)` + `pad` + `setsar` + `fps`。
6. **视频流与音轨分两步做** —— 采样率不一致时一步做音轨会漂移。

### 4.3 安全边界

* 输入**只能来自素材库**（`asset_id`），不接受任意本机路径；
* ffmpeg 参数**全部由代码拼装**，不接受模型传入的任意命令行字符串；
* 用 `create_subprocess_exec`（list 参数，**不做 shell 拼接**）—— 没有 shell 注入面；
* 产物写进素材库 `derived` 区，由 `asset_service.register_asset` 统一入库去重 + 出预览；
* 单条命令超时（默认 900 秒），超时后 kill 进程；
* stderr 只取尾部 1200 字符回给模型。

### 4.4 本机依赖

| 依赖 | 路径 | 实测 |
|---|---|---|
| ffmpeg | `/usr/bin/ffmpeg` | `ffmpeg version 7.1.1-1ubuntu4.2` |
| ffprobe | `/usr/bin/ffprobe` | 同版本 |

路径可在「agent 管理」的 `ffmpeg_path` 里改（留空则从 PATH 找；
ffprobe 会优先用与 ffmpeg 同目录的那一份）。

---

## 5. 文生视频默认参数（MiniMax H3）

### 5.1 默认值

| 参数 | 默认 | 真实来源 |
|---|---|---|
| 底模 `model_key` | `fl2va_pruned_int8` | `video_models.VIDEO_MODELS` |
| **注意力机制** `attention` | **`kitchen`**（Comfy Kitchen INT8 量化注意力） | `video_models.VIDEO_ATTENTION_MODES["kitchen"]`，节点 `ModelAttentionBackend`，inputs `{"attention": "comfy kitchen attention"}` |
| **加速 LoRA** `accel_lora` | **`dmad_4step_critic`**（DMAD 4 步 critic） | `video_models.VIDEO_ACCEL_LORA_COMBOS["dmad_4step_critic"]`，文件 `dmad_minimax_h3_4step_full_critic_comfyui.safetensors` |
| 画面比例 `aspect` | `16:9` | `video_models.VIDEO_ASPECTS` |
| 总像素 `megapixels` | `0.4`（→ 864×480） | `video_models.VIDEO_DEFAULT_MP` |
| 时长 `duration` | `5.0` 秒 | `video_models.VIDEO_DURATION_DEFAULT` |

**每一项都可以改** —— 在「agent 管理 → 文生视频默认参数」里改任意一项，
改完立即生效，不用重启服务。非法值自动回落默认值（不整体拒绝配置）。

### 5.2 实测：默认值真的进了工作流

Agent 下发 t2v 任务时，服务端把默认值合并进 params（**不覆盖模型显式给的**），
再走与面板**同一套** `normalize_t2v_params` 归一（校验 LoRA 文件存在、注意力节点在本机、宽高不超上限）。

合并结果（实测）：

```json
{"prompt": "雨夜街道", "model_key": "fl2va_pruned_int8",
 "attention": "kitchen",
 "accel_loras": [{"key": "dmad_4step_critic", "strength": 1.0, "enabled": true}],
 "aspect": "16:9", "duration": 5.0, "width": 864, "height": 480}
```

归一后（实测）：`resolution=480p` / `steps=4` / 无错误。

**生成的工作流里真的出现了这两个东西**（实测 `build_workflow("t2v", params)`）：

```
138:1 MiniMaxH3TurboLoRA  {"model": ["138:0", 0],
      "lora_name": "dmad_minimax_h3_4step_full_critic_comfyui.safetensors",
      "strength": 1.0, "low_vram": true}
100   ModelAttentionBackend {"model": ["138:1", 0],
      "attention": "comfy kitchen attention"}
```

即：**kitchen 注意力节点在链上、DMAD LoRA 权重在链上、且注意力挂在加速 LoRA 之后**
（顺序与面板一致：底模 → 加速 LoRA → 效果 LoRA → 注意力 → sigma shift）。

### 5.3 为什么必须走「新面板路径」

注意力机制与 dmad 只存在于 `video_workflow.build_t2v_panel`（新路径）；
旧路径 `workflows._h3_video_workflow` 只有 `ckpt850`/`v4step600` 两枚固定 LoRA，
**没有注意力机制**。所以 Agent 的 t2v 必须带上 `model_key` + `width/height` 才走新路径 ——
这正是 `_merge_t2v_defaults` 补齐这些字段的原因。

### 5.4 顺带修掉的一个真实缺陷：「本机缺节点」的假阴性

**现象**：`create_job(t2v)` 报
「本机 ComfyUI 里没有节点 ModelAttentionBackend（Comfy Kitchen INT8 量化注意力）」，
但该节点**实测恒在本机**（`/object_info` 里存在）。

**根因**：`normalize_t2v_params` 原来的判据是 `node not in _COMFY_NODE_NAMES`。
而 `_COMFY_NODE_NAMES` 只有在**成功探测过一次** ComfyUI 之后才有值 ——
内置 Agent 这条路不经过面板（不调 `/api/video-options`），第一次进来时它可能是**空集**。
于是「**还没探测过**」被误判成「**本机没有这个节点**」。

**修法（区分三态）**：

| 探测状态 | 判定 | 动作 |
|---|---|---|
| 探测**完整**且节点不在 | 真缺节点 | 拒绝（明确报错） |
| 没探测过 / 探测**不完整** | 问不到 ≠ 不存在 | 放行（让 ComfyUI 自己报错） |

这与 worker 对 H3-Image 的既有口径一致
（`params["_h3image_nodes_available"] = None if not probe_ok else nodes_ok`）。
`SelfLiftH3Sampler` 的同类判定一并修。

**同时**：`prepare_agent_t2v_params` 改用 `refresh_comfy_node_names(force=True)` ——
默认会复用 60 秒缓存，而缓存可能是**残缺**的（ComfyUI 忙时超时 → 只问到部分节点）。
force=True 每次多问 11 个节点（每个 1 ms，实测几十毫秒），换掉「假缺节点」值得。

**回归证据**：修前 `tests/test_agent.py tests/test_agent_contract.py` 组合跑 → 1 failed
（「AssertionError: '已创建任务' not found in '本机 ComfyUI 里没有节点 ModelAttentionBackend…」」）；
修后 → 69 passed。

### 5.5 三态语义的判定 + 负向对照（本轮补齐）

上一轮把三态语义写进了实现，但**只给 attention 侧补了负控**（twopass 侧只有正控没有负控），
而且**没有任何测试碰过 `_COMFY_PROBE_COMPLETE` 的计算逻辑** ——
三态断言全都**手动**把标志设成 True / False，绕过了计算。

**语义判定结论：新语义（三态）正确且更安全。**

| 论据 | 证据 |
|---|---|
| 旧语义有**真实触发路径** | 内置 Agent 不经面板（不调 `/api/video-options`），首次进来 `_COMFY_NODE_NAMES` 是空集 -> 旧判据报「本机 ComfyUI 里没有节点 ModelAttentionBackend」，而该节点**实测恒在**：`curl /object_info/ModelAttentionBackend` -> HTTP 200 |
| 新语义**没有放松**真缺节点的拒绝路径 | 变异 M3/M4 证明：把节点校验改成 `if False:`（完全不校验）-> 对应断言立刻变红（见下表） |

**本轮补的 4 条测试**：

| 测试 | 作用 |
|---|---|
| `test_allows_twopass_when_probe_incomplete` | twopass 侧**负控**（原来**没有** —— 若把 twopass 三态退回旧语义，套件一条都不会红） |
| `test_rejects_attention_without_node_even_when_other_nodes_present` | 正控加强：集合**非空**但缺目标节点仍必须拒绝（否则「集合为空才放行」的错误实现也能骗过负控） |
| `test_rejects_twopass_without_node_even_when_other_nodes_present` | 同上，twopass 侧 |
| `tests/test_node_probe_completeness.py`（6 条） | 用**可控的假 ComfyUI**（真 HTTP / 真超时 / 真 500）直接测标志取值：问全 -> True；问到了但节点不在 -> **仍 True**；超时/500/连不上 -> False；端到端验证「不完整 -> 放行」「完整 + 真缺 -> 拒绝」 |

**变异验证**（`tests/mutation_check_t2v_node_probe.py`，**5/5 全被抓住**）：

| 变异 | 含义 | 变红条数 |
|---|---|---|
| M1 | 注意力侧退回旧语义（去掉 `_COMFY_PROBE_COMPLETE` 前提） | 2 |
| M2 | 二采侧退回旧语义 | 2 |
| M3 | 注意力侧**完全不校验**节点（静默接受本机没有的机制 = 假控件） | 4 |
| M4 | 二采侧完全不校验节点 | 2 |
| M5 | 探测完整标志恒为 False（三态退化成「永远放行」，真缺节点也放过） | 3 |

**M5 第一轮是 0 条变红** —— 那暴露了**真实缺口**：标志的计算逻辑从没被测过。
补上 `tests/test_node_probe_completeness.py` 之后 M5 变红 3 条。
（若没有这一轮，M5 就是一个**逃逸的变异** —— 标志恒为 False 时真缺节点也不再拒绝，
面板给出假控件、任务建成了跑到 ComfyUI 才炸。）

证据：`docs/agent-power/evidence/T1_mutation_node_probe.txt`、
`T1_three_state_tests.txt`。

---

## 6. 步数上限与总时限

| 常量 | 值 | 位置 |
|---|---|---|
| `MAX_STEPS` | **50** | `h3studio/agent.py` |
| `MAX_TOOL_CALLS` | **120** | `h3studio/agent.py` |
| `TOTAL_TIMEOUT` | **2400 秒（40 分钟）** | `h3studio/agent.py` |
| `MAX_STEPS_HARD_CAP` | **50** | `h3studio/agent_settings.py` |

**总时限为什么必须跟着抬**：单步实测 20~120 秒（本地小模型 + 大工具 schema）。
50 步 × 平均 48 秒 = 2400 秒。如果总时限留在 420 秒，那么第 8 步之后无论步数上限多大
都跑不完 —— 「50 步上限」就成了纸面数字。
这条不变量由测试锁定：`TOTAL_TIMEOUT >= MAX_STEPS * 48`。

**前后端一致**：
* 后端硬顶 `agent_settings.MAX_STEPS_HARD_CAP == agent_mod.MAX_STEPS == 50`（测试断言）；
* 前端「agent 管理」面板的步数输入框 `max` 属性由服务端下发（`/api/agent/settings` 的
  `limits.max_steps_cap`），前端**不硬编码**上限；
* 每次请求的步数上限走 `server._resolve_max_steps()`：以「agent 管理」里的配置为准，
  请求里带 `max_steps` 时以请求为准但仍被夹在配置上限内。

### 6.1 验证：超过 8 步的任务能跑完

`tests/test_agent_power.py::TestStepLimit`：

| 用例 | 断言 | 结果 |
|---|---|---|
| `test_a_task_over_8_steps_completes` | 12 轮工具调用后 → `status=completed` 且 `usage.steps >= 12` | ✅ |
| `test_same_task_would_be_truncated_at_8` | **负控**：同样 12 轮、但 `max_steps=8` → `status=truncated` | ✅ |

第二条是**负控**：它证明上面的 `completed` 不是因为链路变短了，而是因为上限真的放开了。

**语义反转的旧测试已同步改写**（并做变异验证）：
`tests/test_agent.py::TestAgentLimits::test_limits_are_bounded` 原来断言
`MAX_STEPS <= 10` / `TOTAL_TIMEOUT <= 900`，现在断言 `MAX_STEPS >= 50 且 <= 50` /
`TOTAL_TIMEOUT <= 3600`，并新增 `test_total_timeout_covers_the_step_budget`。
**变异验证**：把 `MAX_STEPS` 改回 8 → 新断言 RED（`AssertionError: 8 not greater than or equal to 50`）；
把 `TOTAL_TIMEOUT` 改回 420 → 新不变量 RED（`AssertionError: 420.0 not greater than or equal to 2400`）。
还原后 42 passed。

---

## 7. 剪贴板粘贴媒体（浏览器交互 agent）

### 7.1 行为

在 Agent 聊天输入框里 **Ctrl+V**：

* 剪贴板里有**媒体**（图片/音频/视频文件）→ 拦截 paste 事件，取出文件，
  走**与「📎 媒体」上传完全相同**的入库链路（→ 素材库 → asset_id → 附件卡片）；
* 剪贴板里只有**纯文本** → **不拦截**，走浏览器默认粘贴（用户粘一段话不会没反应）。

### 7.2 取媒体的优先级

1. `clipboardData.items` 里的**文件条目**（`item.getAsFile()`）—— 首选，真文件；
2. `clipboardData.files` —— 退路；
3. `items` 里 `image/*` 的 **dataURL 字符串** —— 再退路，手动转成 `File`。

### 7.3 验证

`tests/test_agent_paste_browser.py::TestPasteInRealBrowser`：
**真 Chromium + 真 HTTP 端点**（真起 `server.make_app()` 生产路由）。

流程：在真实浏览器里用 `new DataTransfer()` 塞一个 `image/png` 的 `File`，
构造 `new ClipboardEvent("paste", {clipboardData: dt})` 派发到 `#agInput` →

* 断言请求真的打到了 `/api/agent/upload`（实测 fetch 记录：`'/api/agent/upload'`）；
* 断言附件区真的出现了 `.ag-attach-thumb` 卡片，且带 `data-asset-id`；
* 断言服务端素材库里真的能查到这张图（`/api/assets` 返回的名字含 `clipboard_pasted`）。

**14 passed**（含静态契约 + 浏览器真跑 + 管理面板契约）。

**真浏览器实测细节（2026-11-14 复测）**：

| 项 | 实测值 |
|---|---|
| 上传响应 | HTTP 200，`{"ok":true,"results":[{"asset_id":"asset_40582ef2ec2d","name":"clipboard_pasted.png","analyzed":true,...}],"errors":[],"uploaded":1,"failed":0}` |
| 前端出卡片耗时 | **85.1 秒**（图片走**同步**多模态分析：服务端 `_preprocess_asset`） |
| 附件卡片 | `.ag-attach-thumb` 出现，`data-asset-id = asset_40582ef2ec2d` |

**等待窗口为什么是 420 秒（2026-11-14 实测修正）**：
图片上传走**同步**多模态分析，本机 Qwen 桥实测：

* 空闲时 ~60~90 秒（实测 85.1 秒）；
* **并发 / 模型热时可超过 240 秒** —— 实测复现过一轮 400 秒仍未返回
  （服务端在正常跑，前端 hint 停在「正在入库素材库…」）。

原来写 240 秒 -> 环境一抖就**假红**（2026-11-14 实测复现过一次）。
放宽到 420 秒，让这条断言**只被真缺陷打红**，不被环境抖动打红。
注意：这条测的是「粘贴 -> 上传 -> 入库 -> 渲染卡片」整条链路，
它**本来就慢**（本地推理），不是性能回归。

**音视频粘贴**：走**异步**分析 job（深度音频管线 / 视频抽帧多模态），
上传立刻返回，前端轮询 `/api/agent/media/{id}/status`。
（实测音频深度分析 ~600 秒 / 116 秒素材 —— 所以**必须**异步。）

证据：`docs/agent-power/evidence/T3_paste_browser_diag.json`（含逐 5 秒采样）、
`T3_paste_browser.png`。

### 7.4 顺带抓到一个真实缺陷：注释里的 `image/*` 吞掉了半份 agent.js

**这是本次改动里最隐蔽的一个 bug，值得单独记。**

**症状**：`tests/test_agent_legacy_fixes.py::test_no_undefined_function_calls_in_whole_agent_js`
报「agent.js 里引用了未定义的函数：`['apiJSON','esc','upsertJob','consumeSse',...]`」——
**几十个函数定义全部「消失」**。

**根因**：新增的粘贴代码块里有一行**注释**：

```js
//  3) 退路是 items 里的 string（image/* 的 base64 dataURL）——
```

其中的 `/*` 被 JS 引擎当成**块注释开始**，一直吞到下一个 `*/`（400 多行之后）——
于是后半份 `agent.js` 全部被当成注释。

**为什么浏览器不报错**：整段被当注释，语法上**完全合法**，
页面能加载，但 Agent 相关逻辑全哑（无语法错、无运行时错，只是代码不存在了）。

**为什么不是「我改了测试」**：这条测试是基线就有的，它抓的是**真问题**。
修法是改注释（不写 `image/*`，改写「image 类型 MIME」），**不是**改测试。

**取证**：

| 项 | 修前 | 修后 |
|---|---|---|
| `agent.js` 里 `/*` / `*/` 计数 | 6 / 4（**不配对**） | 4 / 4 |
| 剥离注释后能识别到的函数定义数 | 25 | 72（与基线一致） |
| `tests/test_agent_legacy_fixes.py` | 1 failed | 28 passed |

**教训（已写进代码注释）**：JS 注释里**不要写 `/*` 或 `*/`** ——
即使是「`image/*` 这种 MIME 通配符」这种很自然的写法。


---

## 8. 代码执行能力（`code_exec`）

| 项 | 口径 |
|---|---|
| 默认状态 | **关闭**（需用户在「agent 管理」里显式开启；开启时面板显示黄色告警） |
| 执行方式 | `subprocess` 起**独立进程**（不 eval 在服务端进程里 —— 崩了会带走服务） |
| 超时 | 默认 30 秒（可配 5~300）；超时后 `os.killpg` **整进程组** kill（不留孤儿） |
| 输出上限 | stdout / stderr 各 4000 字符 |
| 源码上限 | 20000 字符 |
| 工作目录 | 未指定时用系统临时目录（自动清理） |

实测（`tests/test_agent_power.py::TestCodeExec`）：

| 用例 | 断言 | 结果 |
|---|---|---|
| `test_real_execution_returns_stdout` | 真跑 `print(6*7)` → `exit_code=0`，stdout 含 `42` | ✅ |
| `test_nonzero_exit_is_reported` | `sys.exit(3)` → `exit_code=3` | ✅ |
| `test_timeout_kills_the_process` | `time.sleep(30)` + timeout=2 → `timed_out=True` | ✅ |
| `test_empty_code_is_rejected` | 空代码 → `CodeExecError` | ✅ |

---

## 9. 能力清单自检（自动化）

### 9.1 本次新增的测试

`tests/test_agent_power.py` —— **45 passed**，覆盖：

| 测试类 | 覆盖目标 |
|---|---|
| `TestStepLimit` | 步数上限 50 / 工具预算 / 总时限不变量 / 前后端硬顶一致 / 超 8 步任务跑完 / 负控 |
| `TestCapabilityGate` | 4 个能力登记 / code_exec 默认关 / 工具清单按开关过滤 / handler 二次闸门 / 开启后真能跑 |
| `TestCodeExec` | 真执行 / 非零退出 / 超时 kill / 空代码拒绝 |
| `TestFileRead` | 真读文件 / 越界拒绝 / 空 roots 拒绝 / 路径穿越拒绝 |
| `TestWebSearch` | 9898 真出结果 / DDG 源真出结果 / 自检真发请求 / 坏 URL 报错 / 非 http 拒绝 / 无引擎报错 |
| `TestMediaToolsReal` | 6 个 ffmpeg 工具**真跑**（探测/拼接/转码/抽帧/混音/合流）+ 不存在的素材报错 |
| `TestT2vDefaults` | 默认 kitchen + dmad / 指向真实注册项 / 可改任意项 / 非法值回落 |
| `TestSettingsPersistence` | 配置存进真 SQLite 并读回 / 无配置时回默认值 |

`tests/test_agent_paste_browser.py` —— **13 passed**（剪贴板粘贴静态契约 + 真浏览器真跑 + 管理面板契约）；
其中真 Chromium 那条见 §7.5 的**如实说明**。

`tests/test_node_probe_completeness.py` —— **6 passed**（本轮新增），
直接测 `_COMFY_PROBE_COMPLETE` 这个标志**本身算得对不对**（详见 §5.4）。

`tests/mutation_check_t2v_node_probe.py` —— 变异验证脚本，**5/5 全被抓住**（详见 §5.4）。

**agent 相关全套**（27 个测试文件，`scripts/run_agent_regression.sh` 固定清单）：
```
改动树 (local/feature/agent-power, HEAD 76c733e): 901 passed, 0 failed
基线树 (local/maintainer f4bc747):                 845 passed, 0 failed
新增失败差集 = 0
```

**为什么必须用固定脚本跑两边**：基线树**没有**本分支新增的测试文件
（`test_agent_power.py` / `test_node_probe_completeness.py`），
手敲清单容易漏文件 -> 差集数字不可信。脚本会显式剔除并**打印** SKIP 项，
两边跑**同一份清单**。

**通过数差 56 的逐项分解**（每一项都可解释，无「意外通过」）：

| 项 | 数量 | 来源 |
|---|---|---|
| 基线 25 个共有文件 | 845 | — |
| `tests/test_agent.py` +1 | 846 | 步数上限断言 8->50 / 超时 420->2400 |
| `tests/test_t2v_panel.py` +4 | 850 | 上轮 +1 负控（attention）；本轮 +3（twopass 负控 + 2 条正控加强） |
| `tests/test_agent_power.py` +45 | 895 | 本分支新增文件 |
| `tests/test_node_probe_completeness.py` +6 | 901 | 本轮新增文件 |
| **合计** | **901** | 与实测一致 |

证据：`docs/agent-power/evidence/T2_changed_tree.txt`、
`T2_baseline_tree.txt`、`T2_diff_summary.txt`。

### 9.2 全量回归与基线对比（如实说明）

全量套件（排除 7 个**浏览器/端到端**测试文件 —— 它们要起真 Chromium + 真 ComfyUI，
耗时 30 分钟以上且本身有环境相关的抖动）：

| 树 | 结果 |
|---|---|
| **本次改动**（`local/feature/agent-power`，HEAD `d1a5f78`） | `3 failed, 2538 passed, 4 skipped` |
| **基线**（`local/maintainer` f4bc747，**未被任何人改动**） | 同一批文件上失败**更多**（18） |

**最终只剩 3 条失败，全部是基线就存在的**（逐条在基线上复现过）：

| 失败用例 | 为什么基线就红 | 证据 |
|---|---|---|
| `test_studio.py::test_scanner_only_returns_z_image_loras` | 本机 Z-Image 相关 LoRA 实测 **197 个** > 断言上限 120（素材库在变多，断言没跟着变） | 基线同断言同样红 |
| `test_video_sampling_params.py::test_B5_...` | 基线的 `web/app.js` 里仍残留 `steps: 6`（H3-Image 族初值，测试按「全文件无 6」断言） | 基线同断言同样红 |
| `test_yue2_flow_review_fixes.py::test_every_method_declares_write_contract` | 基线的 `director` 增强方式块没声明 write 契约 | 基线同断言同样红 |

基线取自**独立 worktree** `/tmp/h3s-baseline-verify`（`git worktree add --detach local/maintainer`），
跑**同一批**测试文件得到。

另外 4 条属于**浏览器抖动**（`test_page_size.py` / `test_grid_flash.py` /
`test_continuation_retry.py`），在基线上失败**更多**（Playwright timeout 15 条），
两边都不是稳定失败 —— 单独跑时可通过。

**取证纪律**：这些失败**不是**「全绿」被当成了「有失败」——
它们是**基线就红**的问题，本次改动**没有新增**任何一个失败
（基线在同一批文件上失败**更多**：18 > 3）。
按 `agent-minimal-change` 的纪律，这些**不在本次审查目标范围内**的问题
**不擅自修**（改了就是超出用户拍板的范围），如实上报。

### 9.3 本次改动引入的缺陷与修复（全部有变异/负控证据）

本次改动过程中**自己引入又修掉**的两个真实缺陷，都留了防回归测试：

| 缺陷 | 引入原因 | 后果 | 修复 | 防回归 |
|---|---|---|---|---|
| **注释里的 MIME 通配符开假块注释** | 新增的粘贴代码块里有一行注释写了 `image/*`，其中的 `/*` 被 JS 引擎当成块注释开始 | 后半份 `agent.js`（400+ 行）全被吞掉 —— 语法合法、浏览器**不报错**、页面能加载，但所有函数定义消失 | 注释改成「image 类型 MIME」 | `tests/test_agent_paste_browser.py::test_no_stray_block_comment_delimiters`（锁死 `/*` 与 `*/` 必须配对）+ `test_agent_legacy_fixes.py` 原有守门 |
| **节点探测不完整时误报「本机缺节点」** | `normalize_t2v_params` 用 `node not in _COMFY_NODE_NAMES` 判定，而该缓存在「还没探测过」时是空集 | Agent 建 t2v 任务报「本机 ComfyUI 里没有节点 ModelAttentionBackend」，但节点**实测恒在** | 区分三态：探测**完整**才敢断言缺节点；并让 Agent 路径 `force=True` 重探 | `tests/test_t2v_panel.py::test_allows_attention_when_probe_incomplete`（负控）+ `test_rejects_attention_without_node`（正控）。**变异验证**：去掉 `_COMFY_PROBE_COMPLETE` 条件 → 负控 RED（`AssertionError: 探测不完整时不该报缺节点`） |

---

## 10. 已知边界（如实说明）

1. **DDG 裸 HTML 直连被反爬拦**（HTTP 202 + anomaly challenge）。默认走 9898 的 DDG 源；
   `ddg_direct` 保留为备用。
2. **代码执行没有沙箱**。超时 + 输出裁剪 + 独立进程组是边界，但**没有**文件/网络隔离。
   这是「默认关闭 + 显式告警」的原因，不是「已经安全」。
3. **`file_roots` 默认是空的** —— 文件读取能力默认开着，但**不配目录就什么都读不了**。
   需要用户自己填允许目录。
4. **音视频工具的输入是素材库 asset_id**，不是任意路径。要处理本机文件需先入库。
5. **总时限 2400 秒**意味着一次长任务可能挂 40 分钟。前端 SSE 侧的超时是
   `SSE_MAX_MS = 10 分钟`（`web/agent.js`）—— **前端会先于后端超时断开**。
   这是既有行为，本次未改（改它属于另外一件事：前端超时需要与后端总时限对齐）。
6. **前端「agent 管理」面板的搜索引擎类型下拉**只列 3 种类型；
   新增类型要改 `h3studio/agent_settings.py::SEARCH_ENGINE_TYPES` 与前端下拉。
7. **步数上限 50 是硬顶**，用户可在管理面板调低（1~50）但不能调高。
8. **全量套件有 3 条基线既有失败**（见 §9.2）。本次改动**没有新增**失败，
   但也没有去修那些不属于本次审查目标的问题（`agent-minimal-change` 纪律）。
9. **搜索引擎自检在 9898 不可达时 skip 而不是判红** —— 9898 是本机外部服务，
   它挂了不是本仓库的 bug。但**普通搜索**走的是同一个引擎，9898 挂了 Agent 就搜不了
   （会明确报「所有搜索引擎都没成功」并逐条列出原因）。
10. **`/api/video-options` 不能用来填「agent 管理」面板的下拉** —— 它会去探测本机
    ComfyUI，实测能到 60 秒以上。面板改用 `/api/agent/settings` 里带的
    `t2v_options`（本地常量清单，瞬时返回，实测 3 ms）。
11. **图片粘贴/上传是「同步多模态分析」** —— 上传请求要等分析跑完才返回
    （实测 60~90 秒，模型热时更久）。这是既有设计（图片量小、必须拿到描述），
    但它意味着**上传接口是慢接口**：代理/CDN 层的短超时会打断它。
    音视频已改成异步 job（实测音频深度分析 ~600 秒）。
12. **`_COMFY_PROBE_COMPLETE` 只有三态，没有「部分缺节点」的中间态** ——
    探测不完整时一律**放行**（交给 ComfyUI 自己报错）。
    代价：ComfyUI 恰好在探测中途挂掉时，可能出现「任务建成了、跑起来才报错」。
    这是刻意取舍（宁可 ComfyUI 报错，也不可误报「本机缺节点」把功能整个藏掉）。
13. **DDG 直连（`ddg_direct`）默认禁用** —— 本机被 anomaly challenge 拦截（HTTP 202）。
    自检会**如实报错**（不会谎报连通）。可用路径是 `ddg`（经 9898 代理）。
14. **管理面板的「文生视频默认参数」下拉只列本机注册表里的选项** ——
    底模列表来自 `video_models` 的静态注册表，**不是**本机 ComfyUI 的实时模型清单。
    真实可用性由服务端在 `prepare_agent_t2v_params` 里最终校验
    （模型文件存在 + LoRA 兼容性 + 注意力节点在本机）。
15. **`code_exec` 的超时是「墙钟超时」**（默认 30 秒，可配），
    超时后**杀掉整个进程组** —— 但如果用户代码自己 fork 出守护进程并脱离进程组，
    那就杀不掉。这是「没有沙箱」的必然结果（见边界 2）。

---

## 10bis. 本轮（收尾轮）的运行时实测证据

> 采集器：`scripts/collect_agent_power_evidence.py`
> 每个断言的**期望值写成字面量**，从真实响应取值比对，任何一条不符即**非零退出**。
> 隔离实例：`H3STUDIO_PORT=8091` + `H3STUDIO_DATA_DIR=/tmp/agp-data`。
> **线上 8190 未动**（实测 `tool_count=23` / `limits.max_steps=8` / service active）。

**46 / 46 全绿**，逐条：

| # | 断言 | 实测值 |
|---|---|---|
| 1.1 | `limits.max_steps >= 50` | 50（`max_steps_cap=50`、`max_tool_calls=120`、`total_timeout=2400.0`） |
| 1.2 | 前后端硬顶一致 | `limits.max_steps_cap=50`，`settings.max_steps=50` |
| 1.3 | 源码 `MAX_STEPS` | `MAX_STEPS = 50` |
| 1.4 | 前端声明上限 | `web/agent-manage.js` 含 50 |
| 1.5 | **超 8 步任务跑完** | `status=completed`，实际 **13 步** |
| 1.6 | 步数 > 8 | 13 > 8 |
| 1.7 | **负控**：上限压回 8 | `status_at_8=truncated`，error=「已达到步骤上限（8 步），返回已完成的部分结果」 |
| 2.1 | 本机 ffmpeg | `ffmpeg version 7.1.1-1ubuntu4.2` |
| 2.2 | 6 个 ffmpeg 工具注册 | `media_concat / media_extract_frames / media_merge_av / media_mix_audio / media_probe / media_transcode` |
| 2.3 | 上传端点收音视频 | 视频 2 个 + 音频 1 个，返回 asset_id |
| 2.4 | `media_probe` 真探测 | `duration=1.0`、`has_video=True`、`320x240` |
| 2.5 | `media_concat` 真产物 | `asset_df266f49f99b`，**5007 bytes**，`duration=2.023023` |
| 2.6 | `media_extract_frames` | 抽 3 帧，首帧 `asset_abcd917d2d71` |
| 2.7 | `media_transcode` -> mkv | `asset_b32fee5edd9a`，2570 bytes |
| 2.8 | `media_mix_audio` | `asset_fae94a7af743`，**51403 bytes** |
| 2.9 | `media_merge_av` | `asset_c7b3bbcc3be1`，20656 bytes |
| 3.1~3.5 | 粘贴静态契约 | paste 监听 / `clipboardData.items` / `.files` / `audio/*` / `video/*` 全部命中 |
| 3.6 | **图片真实入库** | `asset_eb43277e4e6b`，`kind=image` |
| 4.1 | 4 项能力可配 | `code_exec / file_read / media_tools / web_search`（`code_exec` 默认 **enabled=False**） |
| 4.2~4.4b | 搜索引擎 | 3 条，types=`openwebui / custom / ddg`，每条都有可填 URL，2 条指向 9898 |
| 4.5 | 聚合引擎自检 | `ok=true`、`status=200`、`result_count=1`、「连通性正常（真发了一次请求）」 |
| 4.6 | **DDG（经 9898）自检** | `ok=true`、`status=200`、`result_count=1` |
| 4.6b | DDG 直连**如实报错** | `ok=false`，error=「DuckDuckGo 返回 HTTP 202」（**不谎报连通**） |
| 4.7 | 禁用能力真的生效 | `enabled_tool_count 31 -> 30`，且 `web_search` 工具 `enabled=false` |
| 4.8 | 重新启用 | 30 -> 31（恢复原值） |
| 4.9 | **外部搜索真出结果** | `engine=局域网聚合引擎（9898）`、`total=3`、首条含真实 URL |
| 5.1~5.3 | t2v 默认 | `attention=kitchen` / `accel_lora=dmad_4step_critic` / `model_key=fl2va_pruned_int8` |
| 5.4~5.5 | 默认参数过归一 | `err=''`，归一后 `attention=kitchen` |
| 5.6~5.7 | **默认参数可改可改回** | 改成 `default` 持久化成功 -> 恢复 `kitchen` 成功 |

**管理面板真浏览器**（`scripts/verify_manage_panel_browser.py`）—— **17 / 17 全绿**：

| 断言 | 实测 |
|---|---|
| 管理按钮可点 + 弹窗可见 | `#agManageBtn` 点击后 `#agManageModal` 可见 |
| 4 个能力开关渲染 | `file_read / web_search / code_exec / media_tools`；`code_exec` **未勾选** |
| 搜索引擎 URL 输入框 | 3 个：9898 聚合 / 9898 DDG 代理 / DDG 直连 |
| 步数上限 | `#agmMaxSteps` value=**50**、max=**50**、min=1（前端硬顶 = 后端硬顶） |
| 注意力下拉 | 5 项 `default/kitchen/sage/sol/sparse`，当前值 **kitchen** |
| 加速 LoRA 下拉 | 6 项，当前值 **dmad_4step_critic** |
| 模型下拉 | 5 项，含 `fl2va_pruned_int8` |
| 改值 -> 保存 -> 读回 | 服务端读回 `attention=default`（**真的持久化**） |
| 页面无 JS 报错 | `errors=[]` |

截图经 `vision_analyze` **视觉核验**（截图文件名不算证据）：
`T3_manage_panel.png` 可见步数 50 / 总时限 2400 秒 / 4 个能力开关（代码执行未勾选）；
`T3_manage_t2v.png` 可见「外部搜索引擎」三条 + 「文生视频默认参数（MiniMax H3）」
四个下拉框（底模 / 注意力机制 / 加速 LoRA / 画面比例）。

> **一个取证教训**：第一版三张截图的 sha256 **完全相同** —— 说明都没截到弹窗
> 下半部分（「文生视频默认参数」在弹窗下方，不滚动就看不到）。
> 已加「滚动到该区块再截」+ 可见性断言。**截图文件名不是证据。**

证据文件（绝对路径）：

```
/root/projects/h3studio-agent-power/docs/agent-power/evidence/
  T1_mutation_node_probe.txt        5/5 变异全被抓住
  T1_three_state_tests.txt          三态测试 108 passed
  T2_changed_tree.txt                改动树 901 passed / 0 failed
  T2_baseline_tree.txt               基线树 845 passed / 0 failed
  T2_diff_summary.txt                差集分解（新增失败 = 0）
  T3_runtime_evidence.txt            46/46 逐条断言输出
  T3_runtime/                        media_tools_results.json + evidence_results.json + media_outputs/
  T3_paste_browser_diag.json         粘贴逐 5 秒采样 + 上传响应体
  T3_paste_browser.png               粘贴后界面
  T3_manage_panel.json               管理面板 17/17
  T3_manage_panel.png / _engines.png / _t2v.png
```


---

## 11. 相关文件

| 文件 | 作用 |
|---|---|
| `h3studio/agent.py` | 编排器：MAX_STEPS / MAX_TOOL_CALLS / TOTAL_TIMEOUT、工具清单过滤、t2v 默认参数入口 |
| `h3studio/agent_settings.py` | **配置单一真源**：能力清单、搜索引擎、t2v 默认值、归一、持久化 |
| `h3studio/agent_extra_tools.py` | 额外能力工具的注册 + 能力闸门 |
| `h3studio/agent_media_tools.py` | ffmpeg 音视频处理实现（真实调用本机 ffmpeg） |
| `h3studio/agent_websearch.py` | 外部搜索引擎适配（ddg / openwebui / custom）+ 连通性自检 |
| `h3studio/agent_codeexec.py` | 代码执行（独立进程 + 超时 kill） |
| `server.py` | `/api/agent/settings*` 端点、`_resolve_max_steps`、`_merge_t2v_defaults`、`prepare_agent_t2v_params` |
| `web/agent-manage.js` | 「agent 管理」面板前端 |
| `web/agent.js` | 剪贴板粘贴（handlePaste） |
| `web/index.html` / `web/style.css` | 管理按钮 / 模态框 / 样式 |
| `tests/test_agent_power.py` | 能力清单自检（45 条） |
| `tests/test_agent_paste_browser.py` | 剪贴板粘贴真浏览器验证（14 条） |
| `tests/test_node_probe_completeness.py` | 节点探测定律**直接**测试（6 条，本轮新增） |
| `tests/mutation_check_t2v_node_probe.py` | 三态语义变异验证（5 项，本轮新增） |
| `scripts/run_agent_regression.sh` | agent 回归固定清单（改动树 / 基线树**跑同一份**） |
| `scripts/collect_agent_power_evidence.py` | 六项目标运行时证据采集器（46 条断言） |
| `scripts/step_limit_probe.py` | 步数上限实测（>8 步跑完 + max_steps=8 负控） |
| `scripts/diag_paste_browser.py` | 粘贴链路真浏览器诊断（逐 5 秒采样 + 上传响应体） |
| `scripts/verify_manage_panel_browser.py` | 管理面板真浏览器验证（17 条断言 + 截图） |
| `docs/agent-power/NOTES.md` | 跨上下文续接笔记（已完成 / 待办 / 关键数字 / 踩坑） |
| `docs/agent-power/evidence/` | 全部原始证据输出 |
