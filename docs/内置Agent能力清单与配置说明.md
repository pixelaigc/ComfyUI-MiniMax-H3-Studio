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
（`AssertionError: '已创建任务' not found in '本机 ComfyUI 里没有节点 ModelAttentionBackend…'`）；
修后 → 69 passed。agent 相关全套 658 passed。

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

**11 passed**（含静态契约 6 条 + 浏览器真跑 1 条 + 其它）。

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

`tests/test_agent_paste_browser.py` —— **12 passed**（剪贴板粘贴静态契约 + 真浏览器真跑 + 管理面板契约）。

**agent 相关全套**（16 个测试文件）：
```
658 passed in 67.50s
```

### 9.2 全量回归与基线对比（如实说明）

全量套件（排除 5 个**浏览器/端到端**测试文件，它们要起真 Chromium + 真 ComfyUI，
耗时 30 分钟以上且本身有环境相关的抖动）：

| 树 | 结果 |
|---|---|
| **本次改动**（`local/feature/agent-power`） | `13 failed, 2556 passed, 7 skipped` |
| **基线**（`local/maintainer` f4bc747，同一批测试文件单独跑） | `18 failed, 32 passed`（同一批文件） |

**结论：这 13 个失败**全部**是基线就存在的**，与本次改动无关。逐条取证：

| 失败用例 | 归属 | 证据 |
|---|---|---|
| `test_continuation_retry.py` ×3 | 基线既有 | 基线同文件同样 3 条失败 |
| `test_grid_flash.py` ×2 | 基线既有 / 浏览器抖动 | 基线同文件同样失败 |
| `test_page_size.py` ×3 | 基线既有 / 浏览器抖动 | 基线同文件**失败更多**（15 条，全是 Playwright timeout） |
| `test_studio.py::test_scanner_only_returns_z_image_loras` | 基线既有（本机 LoRA 数 197 > 断言上限 120） | 基线同断言同样红 |
| `test_video_sampling_params.py::test_B5_...` | 基线既有（`steps: 6` 残留在 app.js） | 基线同断言同样红 |
| `test_yue2_flow_review_fixes.py::test_every_method_declares_write_contract` | 基线既有 | 基线同断言同样红 |

基线取的是**未被任何人改动的 `local/maintainer`（f4bc747）**，
用独立 worktree `/tmp/h3s-baseline-verify` 跑同一批测试文件得到。

**取证纪律**：这些失败**不是**「全绿」被当成了「有失败」——
它们是**基线就红**的问题，本次改动**没有新增**任何一个失败
（基线在同一批文件上失败**更多**：18 > 13）。
按 `agent-minimal-change` 的纪律，这些**不在本次审查目标范围内**的问题
**不擅自修**（改了就是超出用户拍板的范围），如实上报。

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
8. **全量套件有 13 条基线既有失败**（见 §9.2）。本次改动**没有新增**失败，
   但也没有去修那些不属于本次审查目标的问题（`agent-minimal-change` 纪律）。
9. **搜索引擎自检在 9898 不可达时 skip 而不是判红** —— 9898 是本机外部服务，
   它挂了不是本仓库的 bug。但**普通搜索**走的是同一个引擎，9898 挂了 Agent 就搜不了
   （会明确报「所有搜索引擎都没成功」并逐条列出原因）。
10. **`/api/video-options` 不能用来填「agent 管理」面板的下拉** —— 它会去探测本机
    ComfyUI，实测能到 60 秒以上。面板改用 `/api/agent/settings` 里带的
    `t2v_options`（本地常量清单，瞬时返回，实测 3 ms）。

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
| `tests/test_agent_paste_browser.py` | 剪贴板粘贴真浏览器验证（11 条） |
