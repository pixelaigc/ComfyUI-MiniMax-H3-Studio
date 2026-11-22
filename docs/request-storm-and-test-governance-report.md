# 请求风暴 + 测试集治理 —— 交付报告

**日期**：2026-11-22 ~ 2026-11-23
**项目**：ComfyUI-MiniMax-H3-Studio（线上 worktree `/root/projects/h3studio-live`，8190）
**修复分支**：`/root/projects/h3studio-reqstorm-fix`（`fix/reqstorm-20261122`）
**线上分支**：`local/deploy/qwen-ref-assets-live` @ `306c7b0`

---

## 0. TL;DR

| 项 | 修复前 | 修复后 | 证据 |
|---|---|---|---|
| 8190 → 8188 请求速率（同等客户端负载） | **1.945 req/s** | **0.796 req/s** | `perf/ab3_old.pcap` / `ab3_new.pcap` |
| 其中 `/queue` | 1.945 req/s | **0.473 req/s** | 同上 |
| 8190 持有到 8188 的连接数 | 每请求新建（FIN-WAIT 堆积） | **2 条**（共享池） | `ss -tnp` |
| 8188 响应延迟 p99 | 7.4 ms | **3.9 ms** | `perf/ab3_latency.txt` |
| 顶栏状态翻转（90s DOM 采样） | **6 次 / 296 采样** | **1 次 / 180 采样**（仅初始加载） | `perf/dom/post_fix_dom_90s.json` |
| `pytest tests/ -q` 全量耗时 | **942.57s (15m42s)** | **53.83s (0m54s)** | `testgov/timing_full_raw.txt` |
| 默认集用例数 | 4344 | 2178 passed / 2143 skipped | 同上 |
| 线上 jobs 表被测试污染 | **3 条**（`x`/`next`） | **0 条** | 已在 00:02 清理 |
| 媒体库无主产物文件（§5.7） | **2 个**（`v-h3-studio-000254/000255.mp4`） | **0 个**（移入 `backup/orphan-media-20261123-0130/`，未删） | §5.7 逐秒/逐帧对拍 |
| 媒体目录逃逸护栏（§5.8） | 无 | `server.py` 启动即检查 + 4 条护栏自测 | `28 passed`（原 24 + 新 4） |

---

## 1. 请求风暴：根因与取证

### 1.1 现场取证（修复前）

tcpdump 采样（`/tmp/reqstorm/pre-multi-client.pcap`，71.6s 窗口，9 个客户端）：

```
HTTP 请求总数 = 262  ->  3.66 req/s
/queue          187   2.61 req/s     <- 主放大源
/history/{id}    47   0.66 req/s
/system_stats    27   0.38 req/s
```

`ss` 连续 30 秒稳定显示 **8190 上挂着 9 条 SSE 连接**。

### 1.2 三个放大源（文件:行号）

| # | 位置 | 问题 | 放大倍数 |
|---|---|---|---|
| **A** | `server.py` `api_status_stream` → `status_snapshot` → `fetch_comfy_queue` | 每条 SSE **各自**每秒打一次 `/queue` | **N 条 SSE = N req/s**（实测 9 倍） |
| **B** | `server.py` `api_system` | 每次调用 `async with ClientSession(...)` **新建会话 + 新 TCP 连接**，独立打 `/system_stats` 与 `/queue` | 每 3 秒一条新连接，FIN-WAIT 堆积 |
| **C** | `h3studio/yue2_schema.py` `get_enums()` 的 except 分支 | 失败时**不更新 `_cache["at"]`** → 缓存永不新鲜 → **每次失败立刻重拉全量 `/object_info`** | 全量实测 **37.5 MB / 3~10 s / 7129 节点**；journal 当日 16 次 fallback 警告 |

pcap 里 3 条并发全量 `/object_info` 连接，各持续 **10.55s / 7.15s / 14.84s**，
服务端分别发送约 **22.5 MB / 29.1 MB / 16.3 MB**。

### 1.3 修复

**新增 `h3studio/comfy_client.py`（383 行）** —— 单一权威客户端：

- **会话复用**：全进程**一个** `ClientSession`（`TCPConnector(limit=16)`）；
- **TTL 缓存**：`/queue` 2s、`/system_stats` 3s、全量 `/object_info` 15min；
- **single-flight**：同 key 并发只打 1 次上游；
- **失败退避**：指数退避 + 抖动，**失败不立即重试**；
- 区分「不可达」（`None`）与「空结果」，不把不可达伪装成队列为空。

**`server.py` 改动**：

- `fetch_comfy_queue` / `fetch_comfy_queue_ids` / `api_system` / `status_snapshot` 全部走共享客户端；
- SSE 改为共用一份 `_STATUS_STREAM_SNAPSHOT`（刷新频率**与连接数无关**）；
- 新增 `_comfy_health_gate`：连续失败 3 次才判「未连接」，一次成功即恢复（迟滞）；
- `refresh_comfy_node_names` 复用共享 session；
- shutdown 时 `await comfy_client.close_session()`。

**`h3studio/yue2_schema.py`**：

- 改用**按节点名**问 `/object_info/<ClassName>`（实测 **1.2 KB / 1 ms**，替代 37.5 MB 全量）；
- 失败时**也**写缓存（`OBJECT_INFO_FAIL_TTL = 60s`），杜绝「失败即立刻重试」；
- 老版本 ComfyUI 不支持按名问时**回落**全量拉取。

**新增 `web/poll-scheduler.js`（124 行）** —— 统一轮询调度器 `window.H3Poll`：

- 递归 `setTimeout`（上一轮**完成**后才排下一轮，**永不重叠**）；
- ±20% 抖动（多标签页相位打散）；
- 失败指数退避（上限 30s），成功复位；
- `document.hidden` 时跳过，重新可见立即补跑；
- 同 key 去重。

**`web/app.js`** —— 单一写入口 `applyStatusSnapshot()` + `scheduledPoll()`。

---

## 2. 顶栏状态高频闪烁：根因与修复

### 2.1 现象（浏览器 DOM 60 秒采样，修复前）

```
296 采样 / 6 次翻转：
  0.00s 生成中
 24.01s 空闲
 33.17s ComfyUI 未连接
 33.37s 空闲
 42.10s ComfyUI 未连接
 55.72s 空闲
```

### 2.2 根因（两个写回源互相覆盖，且都没有迟滞）

1. `setInterval(refreshSystem, 3000)` 每 3 秒拉 `/api/system`，
   **一次探测失败就写「ComfyUI 未连接」**；
2. `EventSource('/api/status/stream')` 的 SSE 帧里**硬编码**
   `comfyui_ok: true`（老代码 `Object.assign({comfyui_ok: true}, data)`），
   于是 SSE **每秒**又写回「正常」。

两源交替写同一个 `#statusText` → 用户看到高频闪烁。

### 2.3 修复

- **服务端权威**：`comfyui_ok` 由服务端判，带迟滞（连续失败 3 次）；
- **单一写入口**：所有状态更新走 `applyStatusSnapshot()`，
  按快照新鲜度仲裁（**旧的不能覆盖新的**）；
- **不再注入** `comfyui_ok: true`；
- SSE 帧与轮询响应携带**同一个**权威值。

### 2.4 修复后实测

`perf/dom/post_fix_dom_90s.json`：

```json
{"total_samples": 180, "transitions": [{"t": 0.51, "text": "空闲"}]}
```

**180 次采样，1 次翻转**（仅页面初始加载），**0 次「未连接」**。
对照修复前的 296 采样 / 6 次翻转 —— **状态抖动消除**。

---

## 3. A/B 受控对照（修复前 vs 修复后）

**方法**：两个隔离实例跑**完全相同**的客户端行为
（3s 轮询 `/api/system` + 1s 轮询 `/api/queue` + 2s 轮询 `/api/jobs` + 1 条 SSE），
各自 60 秒，tcpdump 抓 loopback 8188，**按源端口过滤**只数该实例的流量。

```
OLD-8301: 113 客户端请求 / 0 错误
NEW-8302: 113 客户端请求 / 0 错误     <- 负载完全一致
```

### 3.1 请求速率（`perf/ab3_old.pcap` / `ab3_new.pcap`）

| 路径 | OLD（修复前） | NEW（修复后） | 变化 |
|---|---|---|---|
| `/queue` | 193 次 / **1.945 req/s** | 47 次 / **0.473 req/s** | **-75.7%** |
| `/system_stats` | 0 | 32 次 / 0.322 req/s | 新增（原本被 `/queue` 掩盖） |
| **合计** | **1.945 req/s** | **0.796 req/s** | **-59.1%（2.44×）** |

### 3.2 响应延迟（`perf/ab3_latency.txt`）

| | OLD | NEW |
|---|---|---|
| n | 60 | 60 |
| p50 | 0.4 ms | 0.4 ms |
| p90 | 1.0 ms | **0.8 ms** |
| p99 / max | 7.4 ms | **3.9 ms** |

### 3.3 CPU

| | OLD 期间 | NEW 期间 |
|---|---|---|
| ComfyUI 平均 CPU | 83.6% | 82.8% |
| 8190 平均 CPU | — | 1.0% |

> **诚实说明**：ComfyUI CPU 在两轮之间**没有显著下降**。
> 原因是本机同时还有其它任务在跑生成（journal 显示 21:18 有 8 个 t2v 任务在跑、
> 21:36 有 SelfLift/upscaler 在跑），**背景负载主导了 CPU 读数**。
> CPU 对照**不足以**单独证明修复效果 —— 真正干净的证据是
> **请求速率 2.44× 下降**和**连接数从「每请求新建」降到 2 条**。

### 3.4 正常轮询基线 vs 异常风暴

| 分类 | 判据 | 数值 |
|---|---|---|
| **正常基线** | 单个页面打开时的最小轮询 | `/queue` ≈ 0.5 req/s（1s 轮询 ÷ 2s TTL）、`/system_stats` ≈ 0.33 req/s（3s 轮询 ÷ 3s TTL）、`/history` 仅在 worker 查历史时 |
| **异常风暴** | 修复前实测 | `/queue` **2.61 req/s**（9 条 SSE × 1 Hz）、`/object_info` 全量 **3~10 s 一次 / 37.5 MB** |
| **修复后** | 同等客户端负载 | `/queue` **0.473 req/s**、`/system_stats` 0.322 req/s、**全量 `/object_info` 0 次**（改为按节点名，1.2 KB） |

---

## 4. 测试集治理

### 4.1 实测基线与改后

| | 改前 | 改后 |
|---|---|---|
| 命令 | `pytest tests/ -q --durations=0` | `pytest tests/ -q` |
| 耗时 | **942.57s (15m42s)** | **53.83s (0m54s)** |
| 用例数 | 4344 collected | 2191 passed / 2143 skipped |
| 结果 | 63 failed / 2075 passed / 2026 skipped / 198 errors | 26 failed / 2191 passed / 2143 skipped / 8 errors |
| **提速** | — | **17.5×** |

改前的耗时分布（`testgov/timing_breakdown.json`）：
**40 个文件占了 920.2s / 935.6s = 98%**。
最慢单条：`test_studio.py::TestLoRACompatibility::test_perturbation_threshold_triggers_strength_cap`
= **237.04s**（`list_z_image_loras(deep_analysis=True)` 逐个加载 safetensors）。

### 4.2 分类（依据实测，不是拍脑袋）

| 档位 | 判定规则 | 数量 |
|---|---|---|
| **fast**（默认集） | 不 import playwright 且实测 < 1.0s | ~116 文件 |
| **slow** | import playwright（41）∪ 实测单文件 ≥ 1.0s（40） | **81 文件** |
| **live** | 静态扫描命中（去注释 + 去 docstring，只看**会执行**的代码） | **8 文件** |
| **legacy** | 断言与现状实现不符（功能已删/已重构） | **0 文件** |

**legacy 是空集 —— 原判 3 个经实测全部撤销**：

| 文件 | 原判 | 实测 | 结论 |
|---|---|---|---|
| `tests/test_a23_player.py` | legacy | **31 passed in 0.29s** | 撤销，改判 fast |
| `tests/test_mediaperm_perf.py` | legacy | **16 passed**（2 条读线上库被护栏拦下） | 撤销，改判 fast |
| `tests/test_visible_text_pollution.py` | legacy | **17 passed**（+1 条连线上 8190 被拦下） | 撤销，改判 fast |

> 「过时」是关于**断言与实现的关系**，不是关于「文件老不老 / 跑得慢不慢」。
> 实测全绿且快的文件不是 legacy —— 踢出默认集等于白丢有效的回归保护。

### 4.3 护栏（`conftest.py`）

| 护栏 | 触发条件 | 行为 |
|---|---|---|
| 静态扫描 | 用例源码含线上 URL / 线上 data_dir（**去注释 + 去 docstring + 去行尾注释**） | setup 阶段 fail-fast |
| live 标记 | 标了 `@pytest.mark.live` 但没 `--run-live` | fail-fast（提示加 `--run-live`） |
| e2e 隔离 | e2e 用例没 `--e2e-isolated` | fail-fast |
| 环境变量 | `H3STUDIO_DATA_DIR` 指向线上数据目录 | 会话级 fail-fast |
| **写库护栏** | 用例期间 `sqlite3.connect` 打开了线上库 | teardown 阶段 fail-fast |
| legacy 不收集 | `LEGACY_TEST_FILES` | `pytest_ignore_collect`（连 import 都不做） |

**护栏自测 24 条全绿**（`tests/test_guardrail.py`），每条 fail-fast 分支都有真触发用例
（pytester 子进程 + 拷贝真护栏），断言「真报错 + 报错信息含关键字」。

### 4.4 剩余失败 = 改动前既有

改后剩余 26 failed / 8 errors，**全部是改动前就存在的失败**
（对照 `testgov/timing_full_raw.txt` 改前全量跑的同文件失败计数）：

| 文件 | 改前失败数 | 改后 |
|---|---|---|
| `test_asset_independent_viewer.py` | 2 | 2 |
| `test_cr6_p1_fixes.py` | 1 | 1 |
| `test_e2e_gen_flow_contract.py` | 1 | 1 |
| `test_enhance_owner_badge.py` | 1 | 1 |
| `test_flow_diagram.py` | 2 | 2 |
| `test_frontend_assets_hygiene.py` | 2 | 2 |
| `test_lora_ea.py` | 2 | 2 |
| `test_yue2_flow_review_fixes.py` | 2 | 2 |

其余（`test_apicreate_content_type.py` 7 条、`test_a5_running_truth.py` 4 条、
`test_mode_switch_slot_isolation.py` 8 errors 等）在改前的
`timing_full_raw.txt` 里同样失败 —— **本任务未引入任何新失败**。

### 4.5 推荐用法

```bash
pytest tests/ -q                              # 默认：只跑 fast 子集（53.83s）
pytest tests/ -q -m "not slow"                # 同上（显式）
pytest tests/ -q -m slow                      # 慢用例（Playwright / 重 IO）
pytest tests/ -q -m live --run-live           # 碰线上的用例（需显式开闸）
pytest tests/ -q -m e2e --run-live --e2e-isolated   # 浏览器端到端
pytest tests/ -q --collect-legacy             # 连 legacy 一起收（当前为空集）
```

> **live / e2e 用例在隔离环境外会被护栏拒绝执行** —— 这是设计意图，不是 bug。
> 它们的默认 base 是**线上** 8190，跑它们必须显式开闸 + 起隔离实例。

---

## 5. 线上污染事故（用户 2026-11-22 发现并追问）

### 5.1 现象

8190「生成媒体库」出现 2 张 11/22 21:12 的「文生视频」卡片，
提示词分别是 `next` 和 `x`，采样器 euler、加速 LoRA = Turbo 4 步·ckpt850。

### 5.2 取证：确认是测试产生的

```
sqlite> select id, mode, status, created_at, json_extract(params,'$.prompt')
         from jobs where created_at between 1795353149.40 and 1795353149.55;

575dcb24209d  t2v  done  1795353149.45994  x
f0c0f07edebe  t2v  done  1795353149.47383  next
b853d427c1f2  t2v  done  1795353149.47739  x
```

**三条记录落在 20 毫秒内** —— 脚本行为，不是人工操作。

`chain_id` / `stage_index` 与 `tests/test_continuation_retry.py`
的 3 次 `_make_job()` 调用**一一对应**：

| 线上记录 | prompt | chain_id | stage | 对应测试调用 |
|---|---|---|---|---|
| `575dcb24209d` | `x` | 自己 | 0 | `_make_job({"prompt": "x", "duration": 5})` |
| `f0c0f07edebe` | `next` | `575dcb24209d` | 1 | `_make_job({"prompt": "next", "continuation": ...})` |
| `b853d427c1f2` | `x` | 自己 | 0 | 第二个测试的 `_make_job({"prompt": "x", ...})` |

**根因**：该文件的 `setUp` 只隔离了 `srv.OUT_DIR`，**没隔离 `srv.DB_PATH`**
→ `srv.create_job_record()` 直接写进线上
`/root/projects/comfyui-minimax-h3-studio/data/jobs.db`。

> 该文件里**没有任何线上 URL / 线上路径字面量** ——
> 它写的是 `srv.DB_PATH`（由 config.json 决定的模块常量）。
> **静态扫描看不见这种「间接指向」**，所以必须有运行时检查。

### 5.3 排查范围：其它测试文件是否也写线上库

| 文件 | 判定 | 依据 |
|---|---|---|
| `test_agent_session_search_cascade.py` | 安全 | `H3STUDIO_DATA_DIR` 在 import server **之前**设为 tmp |
| `test_agent_media_upload.py` | 安全 | 同上（文件注释里明写「必须先设好再 import」） |
| `test_agent_session_mgmt.py` | 安全 | 同上 |
| `test_cont_seg_audio.py` | 安全 | 只读，无 `create_job_record` / `INSERT` |
| `test_cont_selflift.py` | 安全 | 无写库调用 |
| **`test_continuation_retry.py`** | **不安全（已修）** | **`DB_PATH` 未隔离** |

### 5.4 清理（只删确认属于测试的记录）

**备份**（清理前）：
```
/root/projects/h3studio-live/backup/live-jobs-before-testgov-cleanup-20261123-0002/
  jobs.db            (2994176 bytes, 107 行)
  jobs_full_dump.sql (470657 bytes, 全量 SQL)
```

**删除**（走应用自己的 API，含媒体文件清理）：
```
DELETE /api/jobs/575dcb24209d  -> {"deleted": "575dcb24209d"}  HTTP=200
DELETE /api/jobs/f0c0f07edebe  -> {"deleted": "f0c0f07edebe"}  HTTP=200
DELETE /api/jobs/b853d427c1f2  -> {"deleted": "b853d427c1f2"}  HTTP=200
```

**结果**：
- jobs 表 **107 → 104 条**（删除 3 条）；
- 4 个媒体文件（`v-h3-studio-000250/51/52/53.mp4`）由该接口一并清理；
- **交叉验证（用户内容零损失）**：
  - 这 4 个文件**未被任何其它 job 引用**（`outputs LIKE` 计数 = 0）；
  - `job_favorites` 里这 3 个 id：**0 行**；
  - `prompt_history` 里 prompt 为 `x`/`next`：**0 行**；
  - `job_batches` 里这 3 个 id：**0 行**。

### 5.5 是否仍在继续产生

**否。** 排查时的 `ps` 结果：
- 用户提到的 pid 224485 **已不存在**；
- 当时在跑的 pytest 只有 `h3studio-prompt-enhance` 的
  `tests/test_enhance_menu.py`（它起自己的 `StudioServer`，连自己的端口，**不碰线上**）；
- 本任务所有 pytest 进程均已结束。

**清理后持续监控**：跑完整默认集期间每 12 秒查一次线上库，
`jobs=104 / short_prompt=0` **全程未变**。

### 5.6 防复发（三层）

1. **源文件修好**：`test_continuation_retry.py` 的 `setUp` 现在同时隔离
   `srv.DB_PATH` + `srv.DATA`（指向 tmp 并 `init_db()`），`tearDown` 复原。
   > **为什么不能只设 `H3STUDIO_DATA_DIR`**：`server` 模块在 **import 时**
   > 就把 `DB_PATH = DATA / "jobs.db"` 算成了模块级常量，
   > 而该文件在顶部就 import 了 server —— 那时环境变量还没设。
   > 只能直接改模块属性。
2. **写库护栏**：盯 `sqlite3.connect`（所有 sqlite 访问的唯一入口）——
   能抓到「用例体内 import server 后立刻写库」的场景。
3. **扫描器精度提升**：去注释从「整行」升级为「tokenize 精确列区间切」，
   **行尾注释**里的线上路径不再误判。

**护栏专项验证**（故意触发一次）：
```
$ cat > tests/test_deliberate_tmp.py <<'EOF'
# guard-exempt: 演示写库护栏（静态扫描放行，写库护栏拦截）
import sqlite3
def test_deliberately_opens_live():
    c = sqlite3.connect("/root/projects/comfyui-minimax-h3-studio/data/jobs.db")
    c.close()
EOF
$ pytest tests/test_deliberate_tmp.py -q
[护栏] 测试往**线上任务库**写了记录：
        ['/root/projects/comfyui-minimax-h3-studio/data/jobs.db']
1 passed, 1 error in 0.01s
线上库 before=104 after=104      <- 记录器只记录，不写入
```

---

## 6. 部署记录

### 6.1 部署方式（house pattern）

`git commit-tree` + `update-ref` —— **没有切换 live 分支**。

| commit | 内容 | 线上分支 |
|---|---|---|
| `89550cc` | fix(reqstorm)：请求风暴 + 顶栏抖动 | ✅ 已部署 |
| `306c7b0` | test(testgov)：测试集瘦身 + 护栏 | ✅ 已部署 |

**并发安全闸门**：部署前核对 `local/deploy/qwen-ref-assets-live` 与本轮起始 commit 一致，
不一致**立即中止**。

### 6.2 线上已有他人未提交 WIP —— 保护措施

线上 `server.py` 有其他并发任务的 WIP（区域 9088-11962），
与本任务的改动（区域 36-12565）**行范围零重叠**（脚本验证 `OVERLAPS: NONE`）。

采用 **3-way merge**（`git merge-file`）而非直接 checkout：

```
base   = live HEAD (8d24e5a)
theirs = live 工作区（他人 WIP）
mine   = 本分支
```

结果：**server.py 合并后同时包含两边的改动**
（`comfy_client` 引用 14 处 + 他人 `api_video_size_plan` 2 处，changed_vs_HEAD = 329 行
= 他人 57 + 我的 272）。

**部署后逐项核验他人 WIP 未被覆盖**：
```
✅ h3studio/agent_settings.py      （与备份逐字节相同）
✅ h3studio/gen_defaults.py
✅ h3studio/accel_lora_policy.py
✅ h3studio/video_models.py
✅ h3studio/video_panel_params.py
```

### 6.3 回滚点

| 路径 | 内容 |
|---|---|
| `backup/live-before-reqstorm-20261122-224830/` | 请求风暴部署前的 6 个文件 + `their-wip.patch`（583 行） |
| `backup/live-before-testgov-20261123-000843/` | 测试治理部署前的 conftest + test 文件 |
| `backup/live-before-testgov2-20261123-001327/` | 写库护栏二次部署前的 conftest + test 文件 |
| `backup/live-jobs-before-testgov-cleanup-20261123-0002/` | **线上 jobs.db 文件 + 全量 SQL dump**（清理前 107 行） |

### 6.4 CRLF 行尾污染（已修）

编辑 `web/app.js` 时行尾被写成 LF → git diff 显示 **8458 增 / 8366 删**，
实际改动只有 **99 增 / 7 删**。

修法：把 CRLF 转回去（8458 行全部 CRLF），保留真实改动。
commit `6d6de5b`。当前 diff：**99 / 7**。

### 6.5 服务重启

只重启了 `h3-studio.service`（8190）。**未重启 ComfyUI（8188）、未重启主机。**

---

## 7. 最终验证

### 7.1 四个 URL（2026-11-23 00:26:06）

```
HTTP=200  time=0.002194s  http://127.0.0.1:8188/
HTTP=200  time=0.001729s  http://127.0.0.1:8190/
HTTP=200  time=0.001524s  http://192.168.3.78:8188/
HTTP=200  time=0.001372s  http://192.168.3.78:8190/
HTTP=200  time=0.003742s  http://127.0.0.1:8188/system_stats
HTTP=200  time=0.001005s  http://127.0.0.1:8190/api/system
```

监听地址：`LISTEN 0.0.0.0:8188`（pid 136086）、`LISTEN 0.0.0.0:8190`（pid 1162342）——
**都绑 0.0.0.0**，LAN 可达。

### 7.2 journal 错误计数（本 boot）

| unit | 计数 | 说明 |
|---|---|---|
| `comfyui.service` | 1742 | **改动前既有**，主要是 `ACEModelLoader`/`ACELoRALoader` 读缺失的 `models/TTS/ACE-Step-v1-3.5B` 目录 |
| `h3-studio.service` | 9 | **改动前既有**，全是 `ComfyUI 提交失败: prompt_outputs_failed_validation`（8 条在 21:18:36，1 条在 22:49:20 = 重启瞬间），**与本次修复无关** |
| `Unclosed client session` | **0** | 共享会话正确关闭（修复前有） |
| 护栏「拒绝执行」 | 0 | 线上服务不跑 pytest，正常 |

### 7.3 浏览器 UI（`perf/dom/post_fix_8190_ui.png`）

- 顶栏状态：**空闲**，`__h3StatusApplied = {"seq":2,"source":"sse","text":"空闲"}`；
- `window.H3Poll`：**function**（调度器已加载）；
- 媒体库：**24 张卡片**正常渲染（含文生音乐 / Z-IMAGE 文生图 / 参考图生成）；
- 页面 build 号：**`306c7b0`** = 已部署的 commit。

---

## 8. 已知限制

1. **CPU 对照不充分** —— 本机同时有其它任务在跑生成，ComfyUI CPU 读数（83.6% vs 82.8%）
   被背景负载主导。真正干净的证据是**请求速率 2.44× 下降**与**连接数降到 2 条**。
2. **默认集仍有 26 failed / 8 errors** —— 全部是改动前既有（本任务未引入新失败），
   但它们**仍然失败**，说明这些是真实的产品/环境缺陷，未在本任务范围内修复。
3. **legacy 档位为空集** —— 逐个实测后没有找到真正「断言与现状实现不符」的文件。
   分类表保留了 legacy 档位与加入规则，供后续新文件使用。
4. **`_TRACKED_DB_WRITES` / `_CURRENT_ITEM` 只在 pytest 进程内** —— 记录器不写任何数据，
   只在内存里记路径字符串。
5. **写库护栏只拦「sqlite 连接」** —— 如果将来有测试通过**非 sqlite** 路径
   写线上文件（如直接 open() 写 jobs.db 的 WAL），护栏看不见。
6. **A/B 的 OLD 实例（8301）跑的是 8d24e5a** —— 它与线上 8190 的其它 WIP 不同，
   但请求风暴相关的代码路径完全一致，A/B 结论对该部分有效。
7. **e2e 套件默认连线上** —— 护栏已把它归为 live 且强制 `--e2e-isolated`，
   但 `tests/e2e/e2e_lib.py` 的 `DEFAULT_BASE_URL` 常量本身仍是线上地址；
   要彻底隔离应改这个默认值（未做，超出本任务范围）。

---

## 9. 证据索引

```
evidence/reqstorm-20261122/
├── perf/
│   ├── ab3_old.pcap / ab3_new.pcap      ← A/B 对照 pcap（决定性证据）
│   ├── ab3_old_client.json / ab3_new_client.json  ← 客户端负载记录（各 113 请求 0 错误）
│   ├── ab3_latency.txt                   ← 延迟对照
│   ├── analyze_pcap.py / analyze_pcap_by_port.py  ← 分析脚本
│   ├── ab_client.py                      ← A/B 客户端
│   ├── post-fix.pcap                     ← 线上 8190 修复后 60s 抓包
│   └── dom/
│       ├── post_fix_dom_90s.json         ← 90s DOM 采样（180 采样 / 1 翻转）
│       └── post_fix_8190_ui.png          ← UI 截图（24 卡片 / 空闲 / build 306c7b0）
└── testgov/
    ├── timing_full_raw.txt               ← 改前全量实测（942.57s）
    ├── timing_breakdown.json             ← 逐文件耗时
    ├── default_set_final.txt             ← 改后默认集实测（60.35s）
    ├── default_set_after_guardfix.txt    ← 加护栏后（53.83s）
    └── baseline_clean_head.txt           ← clean HEAD 基线
```

---

## 10. 后续纪律（用户明确要求）

1. 本任务所有验证一律在**隔离环境**（独立 worktree + 独立端口 + 独立 sqlite）进行；
2. **不得**向线上 8190/8188 提交任何真实生成任务；
3. **不得**在 `/root/projects/h3studio-live` 线上 worktree 跑 pytest；
4. 需要跑 live / e2e 用例时：显式 `--run-live` + `--e2e-isolated`，
   且必须先确认目标是隔离实例。

### 5.7 残留的两个无主视频文件（000254 / 000255）—— 来源定死

**现象**：清理 3 条测试记录后，媒体库目录里仍有两个文件没有任何任务记录认领：

```
v-h3-studio-000254.mp4  1081500 B  mtime 2026-11-22 21:33:08.705  124 帧 = 5.1667s
v-h3-studio-000255.mp4  2486485 B  mtime 2026-11-22 21:33:10.179  238 帧 = 9.9167s
```

（帧数/时长由 `ffprobe` 实测，非推算。）

**线上库对这两个文件的引用 = 0**（四张表逐一查，2026-11-23 01:26）：

```
jobs 0 | job_batches 0 | job_favorites 0 | prompt_history 0
```

**写入方（定死）**：一个「隔离实例」——它的 `data_dir` 是临时目录，
但那个临时目录里的 `outputs` 是**指向线上媒体库的符号链接**：

```
$ ls -l /tmp/h3len-test/outputs
lrwxrwxrwx 1 root root 53 Nov 22 21:31 /tmp/h3len-test/outputs ->
    /root/projects/comfyui-minimax-h3-studio/data/outputs

$ grep -E 'port|data_dir' /tmp/h3len-base/config.json
  "port": 8197,
  "comfyui_url": "http://127.0.0.1:8188",
  "data_dir": "/tmp/h3len-test",
```

它的 `jobs.db` 是线上库在 **21:15:57** 的**快照副本**
（`stat` 的 birth = `2026-11-22 21:15:57.451`，与 `/tmp/h3len-run2` 的时刻一致），
副本里带着当时还污染着线上库的 3 条测试行（`x` ×3、`next` ×1）。

**逐秒、逐帧吻合**（副本库 vs 磁盘文件）：

```
$ sqlite3 /tmp/h3len-test/jobs.db "select finished_at, outputs from jobs
      where id='f0c0f07edebe';"
finished_at = 1795354390.67317  -> 2026-11-22 21:33:10
outputs = [{"file": "videos/v-h3-studio-000255.mp4", "is_full": true},
           {"file": "videos/v-h3-studio-000254.mp4", "name": "t2v_f0c0f07edebe_00001_.mp4"}]
continuation.segment_rel = "videos/v-h3-studio-000254.mp4"
prompt = "next"     prompt_id = e86bb41c-…（与线上原记录完全相同）
```

- 000254 = 本段（实测 124 帧，ComfyUI 产物名 `t2v_f0c0f07edebe_00001_.mp4`）
- 000255 = 接续成片（实测 238 帧，`is_full`，由 `_finalize_continuation_impl` 拼接产出）
- 文件 mtime 21:33:08.705 / 21:33:10.179 与副本里 `finished_at` 21:33:10 **同一秒**

**代码路径（行号 = 21:33 实际运行的线上部署 `/root/projects/h3studio-live/server.py`；
本分支加了 53 行启动检查后，同名函数行号 +53）**：

| 步骤 | 位置（线上部署行号） |
|---|---|
| 实例启动时做「残留 running」自愈 | `server.py:12713` → `_heal_residual_running()` `server.py:705` |
| 捞出 `status='running'` 的行 | `_residual_running_rows()` `server.py:472` |
| 副本里那条 job 在快照时刻是 `running`（它 21:16:21 才在线上跑完）→ 被**接管** | `_adopt_residual_running()` `server.py:591`，下载在 `server.py:648` |
| 下载产物 → 分配新文件名 | `comfy_download_output()` `server.py:1512` → `_next_output_path()` `server.py:1491` |
| 接续收尾：登记本段 + 拼完整视频（产出 `is_full`） | `_finalize_continuation_impl()` `server.py:3852`，被 `server.py:578`（`_finalize_residual_with_outputs`）与 `server.py:679`（`_adopt_residual_running`）调用 |

**为什么文件名是 000254/000255**：`_next_output_path` 取「目录里现有最高编号 + 1」。
21:33 时线上 `videos/` 里最高是 `000253`（000250-000253 直到 11-23 00:02 才被删），
所以新分配正好是 000254、000255。**编号本身就是「写进了线上媒体库」的证据。**

**为什么线上永远看不到这两条卡片**：产物写进了线上媒体库，
任务行写进了副本库 —— **产物与记录分属两处**，线上没有任何一行能认领它们。
写库护栏（§5.6 第 2 层）只看得见「行写进线上库」，看不见「文件写进线上媒体库」。

**处置（可回滚，未删）**：先复制并核对 md5，一致后移入隔离目录。

```
$ cp -a v-h3-studio-000254.mp4 <隔离目录>/   # md5 45a6a6a2112e9d5c456cc7f065479f09 两侧一致
$ cp -a v-h3-studio-000255.mp4 <隔离目录>/   # md5 71af6469d01731599dd61c0a2ca8641a 两侧一致
$ mv v-h3-studio-000254.mp4 v-h3-studio-000255.mp4 <隔离目录>/

隔离目录：/root/projects/h3studio-live/backup/orphan-media-20261123-0130/
线上媒体库：videos/ 61 个文件，最高编号回到 000249（用户内容零改动）
线上 jobs 表：104 行（未变）
```

**是否仍在产生**：否。当前存活的 Studio 实例只有 8190（生产，`data_dir` = 线上目录），
没有任何实例的 `outputs` 是指向线上媒体库的符号链接（全目录扫描，2026-11-23 01:31）。

### 5.8 防复发：媒体目录不得逃出 data_dir（本轮新增）

**1) `server.py` 启动即检查**（`_assert_data_dir_self_contained()`，`server.py:380`）：
`OUT_DIR` / `UPLOAD_DIR` 解析后必须仍在 `DATA` 解析后的目录之内，否则启动失败并打印真实指向。
`DATA` 自己是指向线上的符号链接时**放行** —— 那是「行与产物同源」的显式选择，
该不该用线上目录由写库护栏与静态扫描判断，不由这条检查判断。

**2) `scripts/run_isolated.sh` 默认值改掉**：不传 `data_dir` 时不再默认用线上目录，
改为新建 `/tmp/h3s-iso-data-*`；并拒绝不存在的 / 符号链接形式的 `data_dir`。

**3) 护栏自测**（`tests/test_guardrail.py` 新增 4 条，1 负例 + 3 对照）：

| 用例 | 场景 | 期望 |
|---|---|---|
| `test_data_dir_guard_rejects_outputs_symlink_escaping` | **事故本身**：`outputs` → 线上媒体库 | 退出码 ≠ 0，报错含「符号链接」与真实越界目标 |
| `test_data_dir_guard_allows_fully_isolated_data_dir` | 真目录 data_dir | 放行，且 server 自建媒体目录 |
| `test_data_dir_guard_allows_whole_data_dir_symlink` | 整个 data_dir 显式指向线上 | 放行 |
| `test_data_dir_guard_allows_production_data_dir` | 线上生产配置 | 放行（不得把 8190 拦挂） |

**实测**（仓库 runner = `/home/ubuntu/comfyui/venv/bin/python3 -m pytest`，pytest 8.4.2）：

```
新增 4 条：          4 passed, 24 deselected in 0.92s
完整护栏文件：      28 passed in 20.57s        （原 24 + 新 4）
肇事的 test_continuation_retry.py： 15 passed in 0.13s
每次运行后独立 data_dir 内容：空（未写线上）
```

> ⚠ **工具坑（实测，值得记住）**：用 hermes venv 的 `python3`（**pytest 9.1.1**）跑本仓库测试，
> 会在**加载 conftest 阶段**直接崩：
> `PluginValidationError: … pytest_collect_file … Argument(s) {'path'} are declared in the hookimpl but can not be found in the hookspec`
> （`conftest.py:673`，pytest 9 已移除该参数）。本仓库的实际 runner 是
> `/home/ubuntu/comfyui/venv/bin/python3`（**pytest 8.4.2**）。同一份 conftest，
> 换个解释器就是「全部测试跑不起来」——别把它误判成代码坏了。
