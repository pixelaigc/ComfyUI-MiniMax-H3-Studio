# c9 修复报告 · a6e2ec6a · 2026-11-16（超时预算修复轮）

> 生成时间：__NOW__（Asia/Shanghai）
> 范围：**只收口验收项 c9**（内置 Agent 对话链路超时预算），不重跑 A1–A5 / B1 / B2 / C / D
> 线上：`/root/projects/h3studio-live`（`local/deploy/qwen-ref-assets-live`），systemd `h3-studio.service`，端口 8190
> 主线 `local/maintainer` = `__MAINT__`
> 线上 HEAD = `__LIVE__`
> 8190 探活：HTTP **200**，service **__ACTIVE__**，PID **__PID__**
> 额度闸门：工作区快照 `quota/minimax_quota_latest.json` 采样 2026-11-15 22:02:46 general 5h 剩余 42%（≥20%，闸门开）；
> 本轮只读直查（未新建额度探针任务）：`general` 当前 5h 窗口剩余 **46%**（窗口 2026-11-16 05:00→10:00）

---

## 一、c9 结论

**c9 = 通过。** 内置 Agent 对话链路（`POST /api/agent/chat`）已恢复可用：
修正版部署后线上顺序抽样 **4 次全部 HTTP 200 / status=completed / error=null**（67307 / 61387 / 219466 / 8031 ms），
真实工具调用链路也跑通（`list_modes` + `respond` 两次工具调用，返回真实的 11 个生成模式清单）。

**根因不是「上游忽略 enable_thinking=false」**（该假设被本轮实测推翻），
而是**共享单槽 Provider 的排队时间没有被算进工作台的超时预算**。

---

## 二、根因证据（只读定位 + 直连探测）

### 2.1 复现失败现象

修复前（线上 HEAD `efa0201`）的抽样：10 次抽样中成功 1、超时 4、并发闸门 429 共 3
（证据：`c9_sample1/2/3.json`、`c9_seq1/2.json`、`c9_q1/2/3.json`）。
超时样本的耗时是 60s / 170s / 170s / 170s / 170s —— **全都撞在超时上限上**。

### 2.2 直连上游（工作台真实 payload 形状）—— 关键实验

用**工作台实际发出去的 payload**（tools + `chat_template_kwargs.enable_thinking=false` +
`max_tokens=1024`）直连 `http://127.0.0.1:5000/v1/chat/completions`：

```
HTTP 200  time=180.073419 s
```

响应体（`rootcause_direct_body.json`）：

| 字段 | 值 | 含义 |
|---|---|---|
| `content` | `"pong"` | 正常返回 |
| `reasoning_content` 长度 | **0** | ⇒ **`enable_thinking=false` 是被上游尊重的**（推翻「上游忽略该字段」的假设） |
| `usage.prompt_tokens` / `completion_tokens` | 245 / 2 | 极小的生成量 |
| `timings.prompt_ms` | 2138.1 | prompt 处理 |
| `timings.predicted_ms` | 51.9 | 生成 |
| **实际算力合计** | **≈ 2.2 秒** | |
| **HTTP 端到端** | **180.07 秒** | ⇒ **178 秒全是排队** |

### 2.3 上游日志（同一时刻）—— 排队铁证

```
[strata] done: 10515 tokens in 220 s (48.4 tok/s)      ← 前一个请求占着单槽 220 秒
[strata] thinking limited to 24000 of max_tokens 32000 so the answer has room
[strata] reading the prompt: 238 of 245 tokens, 2 s so far
[strata] done: 2 tokens in 2 s (60.9 tok/s) (stop)      ← 我们的请求实际只用 2 秒
```

⇒ **上游是单槽串行**（`serve/server.py`：`batch = 0  # --batch: the engine's batch slots (0: one request at a time)`），
别人的请求占槽 220 秒，我们的请求排在后面等了 178 秒。

**旧的 180 秒超时恰好卡在排队时长上 ⇒ 必然超时。**

### 2.4 验收方给出的定位（部分成立，部分需修正）

| 验收方结论 | 本轮核实 |
|---|---|
| `agent.py` 对话轮传 `timeout=STEP_TIMEOUT=180s`、`max_tokens=1024` | ✅ 属实 |
| `llm.py compute_timeout` = `120+max_tokens*0.12`（1024→243s） | ✅ 公式属实，但**这条路径根本没被 Agent 用到** |
| 「243s 小于上游所需，必然超时」 | ⚠️ 方向对但归因不准：Agent 走的是**工具调用**路径，那条路径的 timeout 被**写死成 180**（比 243 更小）；且上游所需不是「243s」而是「排队 178s + 算力 2.2s」 |

---

## 三、工作台侧的三处缺陷（本轮全部修掉）

### 缺陷 1：工具调用路径的 timeout 根本没走 `compute_timeout()`

`h3studio/llm.py` 三条路径原本是：

| 路径 | 修复前 | 修复后 |
|---|---|---|
| `call_provider()`（一问一答） | `compute_timeout(max_tokens)` ✅ | 不变 |
| `call_provider_with_tools()`（**Agent 走这条**） | `options.get("timeout", 180)` ❌ **写死** | `compute_timeout(max_tokens) + QUEUE_WAIT_ALLOWANCE` |
| `_replay_stream_text()`（流式重放） | `options.get("timeout", 180)` ❌ **写死** | 同上 |

⇒ `compute_timeout` 的计算**根本没生效**，实际卡死在 180 秒。

### 缺陷 2：共享单槽 Provider 的排队时间没有算进超时预算（真根因）

新增 `QUEUE_WAIT_ALLOWANCE = 240.0`（实测排队 178 秒的覆盖值）。

**关键设计决定：排队余量只加在「工具调用」路径，不进 `compute_timeout`。**
原因：`compute_timeout` 有一条既有契约
（`tests/test_max_tokens.py::test_ceiling_does_not_inflate_timeout`）——
「本次给 800 token → 超时 < 300 秒」，且超时必须**只跟本次预算成正比**。
一问一答路径自己独占推理服务、不与别人抢槽，排队时间不适用；
把它加进去会既破坏契约、又让小预算请求白等 4 分钟。
（第一版实现把余量放进 `compute_timeout`，实测把 `compute_timeout(800)` 抬到 416 破坏了契约，
已改回 —— 见 §五 回归。）

### 缺陷 3：超时文案把原因说错了

原文案：「模型可能未加载或过慢」→ 实际主因是「排队等待前面的请求」，文案误导排障。
两条超时路径的文案都补上「或在**排队等待**前面的请求（共享单槽 Provider）」+ 实际超时上限秒数。

### 改动清单

| 文件 | 改动 |
|---|---|
| `h3studio/llm.py` | 新增 `QUEUE_WAIT_ALLOWANCE = 240.0`；`call_provider_with_tools()` / `_replay_stream_text()` 的 timeout 改为 `compute_timeout(max_tokens) + QUEUE_WAIT_ALLOWANCE`（调用方显式传 timeout 时仍以调用方为准）；两条超时文案补「排队等待」+ 实际上限秒数 |
| `h3studio/agent.py` | `STEP_TIMEOUT` 由写死 180 改为 `llm_client.compute_timeout(1024) + llm_client.QUEUE_WAIT_ALLOWANCE` = **482.88 秒**；两处 provider 调用不再显式传写死的 timeout |

**数值对照**

| 口径 | 修复前 | 修复后 |
|---|---|---|
| `compute_timeout(800)`（一问一答，既有契约 <300） | 216 | 216（**不变**） |
| `compute_timeout(1024)`（一问一答） | 242.88 | 242.88（**不变**） |
| 工具调用路径 timeout（Agent 实际用） | **180** | **482.88** |

---

## 四、线上实测（8190，修正版部署后）

### 4.1 对话链路抽样 —— 4/4 成功

`POST /api/agent/chat` body `{"message":"ping"}`，顺序抽样（timeout 560s）：

| 样本 | HTTP | 耗时(ms) | status | error | steps |
|---|---|---|---|---|---|
| 1 | **200** | 67307 | completed | null | 2 |
| 2 | **200** | 61387 | completed | null | 2 |
| 3 | **200** | 219466 | completed | null | 2 |
| 4 | **200** | 8031 | completed | null | 1 |

修复前同一路径：10 次抽样中成功 1、超时 4、429 共 3。

### 4.2 真实工具调用（不是 ping）

请求：「请用 list_modes 工具列出本工作台支持的生成模式」

```
HTTP 200  elapsed_ms=183176
status=completed  error=null  steps=2  tool_calls=2
  step 1: tool=list_modes  ok=True   ms=6088    summary=已完成
  step 2: tool=respond    ok=True   ms=177087  summary=已给出结论
provider = qwen3.8-flash-next-iq3_xxs  (本机 Qwen 多模态)
```

answer（真实数据，非编造）：
> 已列出本工作台全部 11 个生成模式：视频类 t2v / flf / r2v；图像类 t2i(Z-IMAGE) / t2i_qwen / i2i_qwen / t2i_moss / t2i_h3image / i2i_h3image；音频类 t2a / a2a。同时给出通用边界：8 个固定比例、视频时长 2~60s 且分辨率仅 544p/768p、音乐时长用 max_duration（4~360s）、负面提示词仅在 t2i 与 t2i_moss 生效。需要某个模式的详细参数可继续问我。

⇒ 对话链路 + 工具注册 + 工具执行 + 结果解析**全链路打通**。

### 4.3 修复前后的对照（同一部署、同一上游）

| | 修复前 | 修复后 |
|---|---|---|
| 对话链路抽样 | 10 次：成功 1 / 超时 4 / 429×3 | 4 次：**成功 4 / 失败 0** |
| 工具调用链路 | 0 次成功（全部 180s 超时） | **1 次成功**（2 次工具调用 + 真实数据） |

---

## 五、回归与变异检查

### 5.1 新增回归测试

`tests/test_agent_provider_timeout_budget.py`（9 条）锁住：

1. 工具调用路径的 timeout 必须由 `compute_timeout(max_tokens)` 推导（不得写死 180）
2. 流式重放路径同样不得写死 180
3. 整个 `llm.py` 不得再有写死的 180 兜底
4. 调用方显式传 timeout 时仍以调用方为准
5. **每一条**超时文案都必须点明「排队/等待」（两条路径各查一次）
6. Agent 不得再把写死的 timeout 传给 Provider、不得再用 `STEP_TIMEOUT = 180.0`
7. 排队余量参数必须存在且为正
8. Agent 的 `STEP_TIMEOUT` 必须 ≥ `compute_timeout + 排队余量`
9. 排队余量**只**在工具调用路径：`compute_timeout` 保持纯「随预算缩放」语义
   （`compute_timeout(800)` 必须 < 300 —— 既有契约），但工具路径必须 > 480

### 5.2 变异检查（RED → GREEN，每处都真被抓住）

| 变异 | 结果 |
|---|---|
| 工具路径 timeout 改回写死 180 | **2 条红**（`test_01` / `test_03`） |
| `QUEUE_WAIT_ALLOWANCE` 改回 0 | **2 条红**（`test_11` / `test_13`） |
| Agent `STEP_TIMEOUT` 去掉排队余量 | **1 条红**（`test_12`） |
| 恢复修复 | **9 passed** |

### 5.3 与线上 HEAD 基线两树隔离对跑（新增失败 0 条）

用例集：`tests/test_max_tokens.py` + `tests/test_agent_contract.py` +（fix 树另加）`tests/test_agent_provider_timeout_budget.py`

| 树 | 结果 |
|---|---|
| 基线（`efa0201`，不含新测试文件） | **111 passed / 0 failed** |
| 修复树 | **120 passed / 0 failed** |
| 失败名单 `diff` | **IDENTICAL（新增失败 0 条）** |

差值 +9 正是本轮新增的 9 条回归用例。

> 说明：`tests/test_agent.py` 等集成用例会**真实调用**本机模型，
> 在上游被别人的大 prompt 占槽时会长时间挂起（基线跑曾 600s timeout），
> 故本轮两树对跑采用**确定性用例集**（不含真实模型调用），
> 真实模型调用改由 §四 的线上抽样单独覆盖。

### 5.4 既有契约回归（本次修复的第一版曾破坏，已修正）

第一版把 `QUEUE_WAIT_ALLOWANCE` 放进 `compute_timeout`，
实测把 `compute_timeout(800)` 从 216 抬到 **416**，
直接破坏 `tests/test_max_tokens.py::test_ceiling_does_not_inflate_timeout`
（要求 `compute_timeout(800) < 300`）。
修正版改为「排队余量只加在工具调用路径」，该用例恢复通过（84 条全绿）。

---

## 六、commit 与上线记录

| 项 | 值 |
|---|---|
| 修复 commit（本轮唯一的代码改动） | `3629c2e`（分支 `local/feature/r5-accel-steps-20261115`） |
| 合并进主线 | `local/maintainer` = `__MAINT__` |
| 部署到线上 | `local/deploy/qwen-ref-assets-live` = `__LIVE__` |
| `merge-tree --write-tree` | 退出码 0（第一次合并冲突：测试文件 add/add，**手工**取修正版解决，未用 `--ours`/`--theirs`；`grep -c '<<<<<<<'` = 0） |
| 8190 探活 | HTTP **200** / service **__ACTIVE__** / PID **__PID__** |
| 回滚点 | 合并前主线 `5075e55`、线上 `efa0201` |

**合规**：全程在独立 worktree `/root/projects/h3s-r5`（分支 `local/feature/r5-accel-steps-20261115`）
与独立合并 worktree `/root/projects/h3s-r5-merge` 上开发；
未执行 `git reset` / `git stash` / `git checkout -- .` / force push，未触碰他人分支与未提交 WIP。

---

## 七、证据清单（仓库内持久路径）

目录：`/root/projects/h3studio-live/evidence/c9-retest-20261116/`

### 根因证据
| 文件 | 内容 |
|---|---|
| `rootcause_direct_body.json` | **直连上游**（工作台真实 payload）的完整响应体（HTTP 200 / 180.07s / timings 2138+52ms / reasoning_content 长度 0） |
| `upstream_log_window.txt` | 上游日志窗口（`done: 10515 tokens in 220 s` → `done: 2 tokens in 2 s` 的排队铁证） |
| `longprobe_body.json` / `longprobe.json.meta` | 早期长超时探针（90.5s 成功；实际算力 0.5s） |
| `thr_*.meta` | 4 组 payload 隔离实验（有无 tools / 有无 thinking 控制，全部超时） |

### 修复前基线
| 文件 | 内容 |
|---|---|
| `c9_sample1/2/3.json` + body | 修复前并行抽样（429 / **200** / 超时） |
| `c9_seq1/2.json` | 修复前顺序抽样（均 170s 超时） |
| `c9_q1/2/3.json` + body | 修复前快速抽样（60s 超时 / 429 / 429） |

### 修复后线上实测
| 文件 | 内容 |
|---|---|
| `final_s1/2/3/4.json` + body | **修正版部署后 4 次顺序抽样（全部 HTTP 200）** |
| `final_sampling_summary.json` | 抽样汇总（4 次 / 200×4 / failed 0 / 耗时列表） |
| `fix_s1/2/3.json` + body | 第一版部署后的 3 次抽样（200×3） |
| `fix_toolcall.json` + `fix_toolcall_body.json` | **真实工具调用**（list_modes + respond，2 步 2 调用，返回 11 个模式的真实数据） |

### 回归对跑
| 文件 | 内容 |
|---|---|
| `reg_base3.meta` / `.log` / `.fails` | 基线树：111 passed / 0 failed |
| `reg_fix3.meta` / `.log` / `.fails` | 修复树：120 passed / 0 failed |
| `run_regression_compare2.sh` | 两树隔离对跑脚本（可重跑） |

### 可重跑脚本
`run_samples.sh`（并行抽样）/ `run_seq.sh`（顺序）/ `run_quick.sh`（快速）/
`run_thinking_probe.sh`（thinking 隔离实验）/ `run_reasoning_budget.sh` /
`run_longprobe.sh`（长超时探针）/ `run_fixed_seq.sh` / `run_final_seq.sh` / `run_toolcall.sh`

上一轮验收轮原始证据（未动）：
`/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/.studio-acceptance/604c8339-b8c5-4ff0-a8e3-4b17fe67a597/1/验收报告-a6e2ec6a-20261116.md`
（以及 `72a20ead-…` 那一轮的报告与 `/root/projects/h3studio-live/evidence/acceptance-72a20ead-20261116/`）

---

## 八、已知限制与未完成项

1. **本轮未触发验收轮**（按指令：验收由宿主自动安排；本轮只交出修复 + 证据 + 报告）。
2. **排队余量 240 秒是按单次实测定的**，不是统计意义上的上界。
   若上游出现单次占槽 > 4 分钟的请求（实测见过 65K prompt + 24K 思考预算 ≈ 5 分钟），
   仍可能超时；此时上游侧的并发/排队才是下一层问题（工作台已无更多可做）。
3. **工作台侧未改上游**：上游是共享单槽推理服务，本轮**未修改**其任何配置或代码
   （按指令不修改上游推理服务）。若要根治排队，应在上游侧开多槽或隔离 Agent 的 Provider。
4. **未重跑 A1–A5 / B1 / B2 / C / D 的开发与出片**（范围纪律）；c1–c8、c10 的上一轮结论未被改写。
5. **C 项 9 个带副作用的 Agent 工具未做对话级逐条实测**（在他人正在使用的生产素材库上跑会产生不可回收的副作用）——
   本轮的工具调用实测用的是**只读**工具 `list_modes`，无副作用。
6. `tests/test_agent.py` 等真实调用模型的集成用例在本轮未纳入两树对跑
   （上游占槽时长时间挂起，见 §5.3 说明）；其覆盖由 §四 的线上抽样承担。
