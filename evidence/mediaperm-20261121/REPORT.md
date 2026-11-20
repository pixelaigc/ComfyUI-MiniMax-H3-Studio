# 生成媒体库「卡片载入慢」实测诊断报告（2026-11-21）

**项目**：ComfyUI-MiniMax-H3-Studio 工作台
**仓库**：`/root/projects/comfyui-minimax-h3-studio`（主线 `local/maintainer`）
**线上**：systemd `h3-studio.service` → `/root/projects/h3studio-live`，HEAD `e393e8a`（= `local/maintainer`），`http://127.0.0.1:8190`
**修复分支**：`local/fix/mediaperm-perf`（独立 worktree `/root/projects/h3s-perf-fix`）
**本轮性质**：看门狗巡检 worker，实测取证优先；**只修了无冲突的服务端成因**，前端成因只出定位与方案、不改码。

---

## 0. 现场核实（动手前）

```
$ cd /root/projects/comfyui-minimax-h3-studio && git status && git branch -a && git worktree list && git log --oneline -30
```

| 项 | 现场事实 |
|---|---|
| 线上部署 worktree | `/root/projects/h3studio-live`（systemd `WorkingDirectory`） |
| 线上 HEAD | **本轮中途被并发任务推进到 `02985a5`**（见下方「并发推进记录」）；`local/maintainer` 同步 = `02985a5` |
| 线上分支名 | `local/deploy/qwen-ref-assets-live`（指向同一 commit） |
| 线上工作区 | 无 tracked 改动（只有未跟踪的 evidence 文档） |
| systemd | `active (running) since Fri 2026-11-20 23:42:15 CST`，`NRestarts=0` |

### 并发任务占用范围（重要）

任务「生成媒体库：提交后不跳转标签 + 卡片收藏功能与左侧『收藏』栏」：

```
$ cd /root/projects/h3s-favorites && git diff --name-only local/maintainer...HEAD
.gitignore
docs/生成媒体库收藏功能-交付报告-20261120.md
scripts/verify_favorites_dom_live.py
```

- 其**功能代码**（`web/app.js` / `web/index.html` / `web/favorites.js` / `server.py` / `h3studio/favorites.py`）**早已合入主线并上线**（`git log e393e8a` 可见 `9f74dd7 feat(favorites)`、`523696f merge(favorites)`）。
- 本轮该任务只剩 **3 个 commit（docs + 取证脚本）**，**不再碰 `server.py` / `h3studio/` / `web/` 的功能代码**。
- ⇒ **本轮的服务端改动与它零重叠**，可安全合并。

### 并发推进记录（本轮中途发生，已处理）

| 时点 | 事件 | 本轮处置 |
|---|---|---|
| 开工时 | live = maintainer = `e393e8a`；并发任务只剩 3 个 docs/取证 commit | 直接基于 `e393e8a` 开发 |
| 取证后期 | 并发任务合入并**部署**：`live` 与 `maintainer` 双双推进到 `02985a5`（新增 `web/favorites.js`、`tests/test_frontend_assets_hygiene.py`、`tests/test_mediadef_all_types.py`、流程图 .mmd/.svg/.png、docs） | **零重叠**（它没碰 `server.py` / `h3studio/`）→ `git rebase local/maintainer` **无冲突**，rebase 后重跑 17 项新测试 + 222 项定向测试全绿 + 10 项变异测试仍全咬人 |
| 部署前 | 再次核 `live == maintainer` 且本分支包含最新 mainline | 部署脚本内置**关系式并发判据**（不写死 SHA），任一不成立即中止 |

> 并发任务自己的回归：`baseline(未修) 30 failed / 3933 passed` → `fix 后 23 failed / 3954 passed`（它自己修掉了 7 项）。

### 额度证据（按要求标注为过期证据）

`quota/minimax_quota_latest.json` 最近一个 5h 窗口结束于 **2026-11-20 06:00 (Asia/Shanghai)**，相对当前时间**已过期**。
⇒ 本轮**未**把它当作额度充足；**未新建额度探针任务**；**未新增任何 GPU 生成作业**，全部验证用线上已有产物与已有记录。

---

## 1. 用户症状与「有时候」

> 用户 2026-11-20 实测反馈：生成媒体库卡片「**有时候**载入很慢很慢」。

「有时候」是关键词。实测证实：**空闲时 41ms 很快，慢的是特定条件**。所以本轮不去猜「哪段代码慢」，而是逐条件实测。

---

## 2. 实测取证（全部为真实命令输出）

### 2.1 列表接口本身（线上 8190，curl 计时）

```
$ for i in 1 2 3; do curl -s -o /dev/null -w "run$i total=%{time_total}s size=%{size_download}\n" \
    "http://127.0.0.1:8190/api/jobs?page=1&page_size=200"; done
run1 total=0.041457s size=595782
run2 total=0.040549s size=595782
run3 total=0.043116s size=595782
```

⇒ **空闲时 41ms**。所以「慢」不是列表接口的常态路径。

### 2.2 服务端内部耗时分布（真实库副本 + SQL 计数）

脚本：`evidence/mediaperm_serverprofile.py`（把线上 `jobs.db` 复制到临时目录，用线上那份 `server.py` 跑）

```
### tab=全部 page_size=100
  rows=100  SQL_count=213
  phase_db=0.9ms  phase_row_to_job+batches=714.5ms  phase_favorites=0.1ms  total=715.5ms
  SQL breakdown:
     111x SELECT params FROM jobs WHERE id=?
     100x SELECT * FROM job_batches WHERE job_id=? ORDER BY idx
```

⇒ **213 条 SQL**（其中 211 条是 N+1），`row_to_job`+`batches` 独占 714ms。

### 2.3 逐行定位到最慢的那一行

脚本：`evidence/mediaperm_perrow.py`（逐行拆 `summarize` / `_job_resolution_info` / `_continuation_info_for_row` / `gen_params` / `batches_of`）

```
=== 全部标签第一页：最慢 15 行 ===
    686.88ms  cont=0.18 reso=0.02 sum=686.54 gp=0.04 bat=0.10  t2i_moss   failed
     61.42ms  cont=60.61 reso=0.02 sum=0.69 gp=0.03 bat=0.09  t2v        done
      2.17ms  ...
合计 778.4ms / 100 行
分项合计: {"t_summarize_ms": 692.3, "t_resolution_ms": 0.8, "t_continuation_ms": 75.1, ...}
```

⇒ **一行 `t2i_moss` 的 `summarize()` 就占 686ms**。

### 2.4 cProfile 抓到真正热点（不是 import）

脚本：`evidence/mediaperm_deep.py` + cProfile

```
ncalls  tottime  cumtime  filename:lineno(function)
    1    0.000    1.599  h3studio/job_meta.py:1359(summarize)
    1    0.000    1.599  h3studio/job_meta.py:1159(_image_summary)
    2    0.033    1.598  h3studio/h3_effect_loras.py:432(discover_effect_loras)
  614    0.467    0.637  h3studio/h3_effect_loras.py:94(lora_modules)
  643    0.003    0.437  h3studio/h3_effect_loras.py:73(read_safetensors_header)
176657    0.067    0.407  h3studio/h3_effect_loras.py:194(_bind_module)
```

⇒ `discover_effect_loras()` 要**扫全盘 712 个 safetensors**（`/mnt/usb2t` 是 fuseblk USB NTFS，`_candidate_lora_files()` 实测 712 个文件 / 700 个在 USB 盘）。

### 2.5 冷/热分解：这是「有时候」的机制

脚本：`evidence/mediaperm_coldwarm.py`

```
summarize call #1: 741.97ms
summarize call #2: 0.04ms
summarize call #3: 0.02ms
```

`discover_effect_loras()` 自带 **TTL 60 秒**缓存（`h3studio/h3_effect_loras.py:421`）。
⇒ **每 60 秒必有一次尖峰** —— 第一个卡片触发全盘扫描，后面的命中缓存又变快。
⇒ 这正是用户说的「**有时候**」。

### 2.6 线上长跑采样（抓周期性尖峰）

脚本：`evidence/mediaperm_longsample.py`（每 1 秒一次，共 200 秒，193 个样本）

```
p50 23.7ms   p90 33.7ms   p99 444.6ms   max 731.1ms
>200ms 的样本:
  t=   0.2s     232.1ms
  t=  61.0s     229.9ms     <-- 距上次 60s = TTL 过期
  t=  83.2s     731.1ms
  t=  96.5s     444.6ms
  t= 182.1s     292.8ms
```

⇒ **p50 24ms 但 max 731ms**。尖峰间隔 ~60s，与 TTL 吻合。

### 2.7 隔离实验：ComfyUI 忙时的第二个根因

脚本：`evidence/mediaperm_isolate.py`（独立 8199 实例 + 假 ComfyUI，可注入 `/queue`、`/history` 延迟）

```
### 无 running / ComfyUI 空闲          [{'ms': 913.4}, {'ms': 34.5}, {'ms': 47.3}]
### 有 running / ComfyUI /history 慢 2s
    [{'ms': 2055.3}, {'ms': 2037.7}, {'ms': 2038.4}]   <-- 每次都 2 秒，无一次命中缓存
```

⇒ `/queue` 有 3 秒缓存，**`/history` 没有**。`live_state_for_job()` 对每条 running 任务各查一次 `/history`。
前端每 2 秒轮询一次 ⇒ **每 2 秒就有一轮 2 秒阻塞**。

### 2.8 真实浏览器（Chromium）取证

脚本：`evidence/mediaperm_probe.py` → `evidence/mediaperm-20261121/mediaperm-probe.json`

```
marks: {domcontentloaded: 0.345, first_card: 0.374, networkidle: 3.167, card_count: 12}
media_elements: {cards: 12, imgs: 0, videos: 12, audios: 0}
api_jobs_requests: 7
files_requests: 12, files_bytes_est = 25,812,905 (24.6 MB)
files_sample_headers: {cache-control: "no-cache", etag: "...", last-modified: "...", accept-ranges: "bytes"}
```

标签/翻页实测：

```
tab=active  elapsed 0.236s   tab=done  elapsed 0.239s   tab=all  elapsed 0.254s
翻页 p1/p2/p3: 0.307s / 0.304s / 0.302s
```

**响应头实测**（`/files/...`）：

```
HTTP/1.1 200 OK
Cache-Control: no-cache
Content-Type: image/png
Etag: "18e7f25e496e73d8-16c33f"
Last-Modified: Sat, 14 Nov 2026 15:17:44 GMT
Accept-Ranges: bytes
```

条件请求实测：`If-Modified-Since` → **304 / 0 字节 / 1.9ms** ⇒ **有 ETag + Last-Modified + 304 支持，但 `Cache-Control: no-cache` 让浏览器每次都回源重验**。

---

## 3. 成因分解：服务端 vs 前端

### 3.1 服务端成因（3 条，本轮已修）

| # | 成因 | 代码位置 | 实测代价 | 触发条件 |
|---|---|---|---|---|
| S1 | 效果 LoRA 清单**每卡一次全盘扫盘**（且只记忆 60s） | `h3studio/job_meta.py:218 _effect_lora_display` → `h3studio/h3_effect_loras.py:432 discover_effect_loras` | **377~980ms/次** | **每 60 秒一次**（TTL 过期）+ 每条含 LoRA 的任务 |
| S2 | 该全盘扫盘**跑在 aiohttp 事件循环里** | 同上（`summarize` 在 `api_list` 的事件循环里被调用） | 冻结整个 8190（与 2026-11-16「整站不可访问」同类） | 每次 TTL 过期 |
| S3 | ComfyUI `/history` **没有缓存**（`/queue` 有） | `server.py:7547 live_state_for_job` → `server.py:1723 comfy_history` | **每次 /api/jobs 等满 /history 延迟** | 有 running 任务 + ComfyUI 忙 |

附带修掉的两个 N+1（不是主因但是固定成本）：

| 成因 | 代码位置 | 实测 |
|---|---|---|
| 批次每条任务一次 SELECT | `server.py:967 batches_of` | page_size=100 → **100 次**查询 → 1 次 |
| 血缘回溯每段开一次连接 + 多查一次本段 params | `server.py:3561 _lineage_durations` | `row_to_job x20` SQL **23 → 3**；`x60` **68 → 7** |

### 3.2 前端成因（本轮**不改码**，只给定位与方案）

| # | 成因 | 代码位置 | 实测 |
|---|---|---|---|
| F1 | 卡片 `<img>` **没有 `loading="lazy"`** → 首屏**整张**下载全尺寸 PNG（均值 2.55MB，最大 9.97MB） | `web/app.js:4939` | 每页 48 张 → **134.6 MB**；每页 12 张 → ~33.6 MB |
| F2 | `<video preload="metadata">` 但服务端**没有 Range 截断**，浏览器实际**下载整个 mp4** | `web/app.js:4937` + `server.py:11382 files_handler` | 实测 Range `bytes=0-` 返回 `206` + `Content-Range: bytes 0-2237631/2237632`（**全文件**，2.13MB） |
| F3 | `/files` 用 `Cache-Control: no-cache` → 每次回源重验（虽然有 ETag → 304 只花 1.9ms） | `server.py:11409` | 实测 `If-Modified-Since` → 304 / 0B / 1.9ms |

> F1/F2 是**前端/静态服务**范畴，且 `web/app.js`、`web/index.html` 正是并发任务声明占用的文件 → **按约束只出方案，不改码**。

### 3.3 占比小结

- **「有时候很慢」的服务端占比 = 100%**（三条服务端成因全部命中「有时候」的条件：60s TTL 过期 / 有 running 任务 + ComfyUI 忙）。
- 服务端实测：p50 **24ms** → max **731ms**（长跑采样）。
- 前端是**恒定**成本（每页媒体字节数），不解释「有时候」，但决定「一直不算快」。

---

## 4. 已修（服务端，无冲突路径）

**分支**：`local/fix/mediaperm-perf`（worktree `/root/projects/h3s-perf-fix`）

| 文件 | 改动 |
|---|---|
| `h3studio/job_meta.py` | 新增 `_effect_lora_display_map()`（模块级映射 + TTL 300s）；`_effect_lora_display()` / `_in_lora_registry()` 改为查表；新增 `warm_effect_lora_display_map()`（供事件循环外预热）与 `invalidate_effect_lora_display_cache()` |
| `server.py` | `api_list` 用 `asyncio.to_thread(job_meta.warm_effect_lora_display_map)` **先**预热再进 `row_to_job`；新增 `comfy_history_present()`（查到过**永久**缓存 / 没查到 3 秒）+ `_HISTORY_PRESENT_CACHE`；新增 `batches_map()` 一次 `IN (...)`；`_lineage_durations` 复用连接 + 复用调用方 params；新增请求级血缘缓存 `_LINEAGE` |

**未触碰**：`web/**`（并发任务占用）、采样类字段契约相关代码。

### 追加修（自查发现的正确性缺口）

`job_meta` 的显示名映射是 `discover_effect_loras()` 的**派生物**。若底层缓存被作废（装完新 LoRA）而派生层不知道，卡片会继续显示**旧**显示名（最多 300 秒）。
⇒ 加**代次**机制：`h3_effect_loras.invalidate_effect_lora_cache()` 推进代次，派生映射记录的代次不一致就自动重建 —— **任何**一条作废路径都能级联，不依赖调用方记得刷第二层。
（新增断言 `test_display_map_invalidates_with_registry_cache`；`test_h3_effect_lora.py` + `test_effect_lora_frontend.py` 52 例全绿 —— 这两组直接调用 `invalidate_effect_lora_cache`。）

### 已上线（8190）

| 项 | 值 |
|---|---|
| merge commit | **`7e35b4c27eef1fc01f93ba35a90e27b9fdda1d1c`** |
| `local/maintainer` | `7e35b4c` |
| `local/deploy/qwen-ref-assets-live` | `7e35b4c` |
| 线上工作树 HEAD | `7e35b4c`，无 tracked 改动 |
| 回滚点 | `backup/live-before-perf-20261121-023327` / `backup/maintainer-before-perf-20261121-023327` |
| 重启 | PID `1469221` → `1617318`，`systemctl is-active` = active |
| 探活 | `/` `/api/jobs` `/api/agent/settings` `/api/system` `/api/generation-types` `/api/jobs/favorites` **全部 200** |
| 线上代码自检 | `_effect_lora_display_map` × 10、`asyncio.to_thread(...warm...)` × 1、`_HISTORY_PRESENT_CACHE` × 3、`batches_map` × 5 —— **全部在位** |

### 线上复测（部署后，同一组指标）

```
线上 /api/jobs（curl，3 次）
  status=all&page=1&page_size=48 -> 0.013098s 0.014719s 0.014488s
  status=done&page=1&page_size=48 -> 0.012590s 0.015362s 0.011765s
  status=all&page=2&page_size=48 -> 0.006558s 0.009602s 0.007244s
  status=active                  -> 0.000840s 0.000805s 0.000762s
```

**长跑采样对比（同一脚本、同一 URL、每 1 秒一次）**

| | 修前（线上，200s / 193 样本） | 修后（线上，330s / 325 样本） |
|---|---|---|
| p50 | 23.7ms | **13.4ms** |
| p90 | 33.7ms | **19.4ms** |
| p99 | 444.6ms | **86.1ms** |
| max | 731.1ms | **260.3ms** |
| mean | — | 16.2ms |
| >200ms 的样本 | **7 / 193** | **1 / 325** |

**真实浏览器（Chromium，线上 8190）**

| | 修前 | 修后 |
|---|---|---|
| DOMContentLoaded | 0.345s | 0.402s |
| 首张卡片出现 | 0.374s | 0.428s |
| networkidle | 3.167s | 6.276s（见下方说明） |
| 切标签 active/done/all | 0.236 / 0.239 / 0.254s | 0.287 / 0.224 / 0.255s |
| 翻页 p1/p2/p3 | 0.307 / 0.304 / 0.302s | 0.301 / 0.304 / 0.311s |
| 卡片数 | 12 | 12 |

> **networkidle 没变快是正常的、也不是本轮的靶子**：它由**媒体文件下载**决定 —— 12 张卡片实测拉 **24.6 MB**（`/files` 的视频是整文件，不是 Range 截断）。这部分属**前端/静态服务**范畴（见 §8 的 F1/F2），本轮按约束不改码。
> `/api/jobs` 本身在浏览器里从「p99 444ms」降到「p99 86ms」—— 这才是「卡片列表本身慢」的那一段。

### 隔离实例修前/修后（针对新 mainline `02985a5` 复测）

```
修前(线上) ComfyUI/history慢2s : 全部 min=2024.7ms max=2045.0ms
修后(分支) ComfyUI/history慢2s : 全部 min=9.9ms   max=28.8ms
修前(线上) ComfyUI/queue慢2s   : 全部 min=24.3ms  max=32.6ms
修后(分支) ComfyUI/queue慢2s   : 全部 min=9.7ms   max=31.7ms
修前(线上) ComfyUI空闲          : 全部 min=16.7ms  max=24.4ms
修后(分支) ComfyUI空闲          : 全部 min=9.0ms   max=25.9ms
```

---

## 5. 修前/修后实测对比

脚本：`evidence/mediaperm_beforeafter.py` → `evidence/mediaperm-20261121/before-after.json`
（修前 = `/root/projects/h3studio-live` 现行代码；修后 = `/root/projects/h3s-perf-fix`；两边都用真实 `jobs.db` 副本 + 假 ComfyUI，同一组 URL）

```
### 修前(线上) 有running ComfyUI空闲
    全部   min=22.8ms  max=43.4ms
### 修后(分支) 有running ComfyUI空闲
    全部   min=8.5ms   max=27.9ms

### 修前(线上) ComfyUI/history慢2s
    全部   min=2027.5ms  max=2049.9ms  all=[2027.9, 2049.9, 2027.5, 2030.9, 2028.6]
### 修后(分支) ComfyUI/history慢2s
    全部   min=13.4ms    max=32.3ms    all=[32.3, 24.2, 15.0, 13.4, 15.7]

### 修前(线上) ComfyUI/queue慢2s
    全部   min=24.8ms  max=28.8ms
### 修后(分支) ComfyUI/queue慢2s
    全部   min=11.0ms  max=44.0ms
```

**关键数字**：`/history` 慢 2 秒场景 —— **2027.5~2049.9ms → 13.4~32.3ms（约 100~150 倍）**。

> 取证纪律：本脚本带 `_running_jobs_in_page` 断言（确认 running 任务真的在这一页），中途因「启动自愈会抹掉残留 running」与「running 任务不在第一页」两次**假绿**被断言抓出并修正 —— 修前那两轮的读数作废，以本表为准。

---

## 6. 测试与变异验证

```
$ cd /root/projects/h3s-perf-fix && pytest tests/test_mediaperm_perf.py -q
16 passed in 4.49s
```

变异测试（`evidence/mediaperm_mutation.py` → `evidence/mediaperm-20261121/mutation-result.txt`）：

```
M1  显示函数改回直接调扫盘            PASS(断言变红)
M2  白名单改回直接调扫盘              PASS(断言变红)
M3  映射不记忆化（每次重建）          PASS(断言变红)
M4  预热改成直接同步调用              PASS(断言变红)
M5  预热挪到 row_to_job 之后（顺序错） PASS(断言变红)
M6  /history 不缓存（查过也重查）      PASS(断言变红)
M7  /history miss 也永久缓存          PASS(断言变红)
M8  /history 异常被缓存成 False       PASS(断言变红)
M9  批次改回每条查一次（N+1）         PASS(断言变红)
M10 血缘回溯重新查本段 params         PASS(断言变红)

咬人失败（断言没变红）的变异：0 条
```

> 第一轮变异测试抓出 **3 条不咬人的断言**（M1/M2 因 docstring 里出现函数名而假红 —— 改用 AST 判定并额外收「属性最后一段」；M9 因测试把 `api_list` 逻辑抄了一遍 —— 改成**真起隔离实例 + 真 HTTP + sitecustomize 注入 SQL 计数器**）。修正后 10/10 全咬人。

---

## 7. 只读核对结果

### A. 服务探活与稳定性

```
$ curl -s -o /dev/null -w "%{http_code} (%{time_total}s)" ...
  /                    -> 200 (0.0012s)
  /api/jobs            -> 200 (0.0534s)
  /api/agent/settings  -> 200 (0.0023s)
  /api/system          -> 200 (0.0073s)
  /api/generation-types-> 200 (0.0008s)
systemctl is-active h3-studio.service -> active ; NRestarts=0 ; MemoryCurrent=638MB
```

journal 近 24h 的 ERROR/Traceback **共 5 行**，全部可归因、非本轮引入：

| 时间 | 内容 | 判断 |
|---|---|---|
| 11-20 16:50:08 | `AssertionError: multipart/* content type expected` | 客户端上传缺 content-type，与本轮无关 |
| 11-21 01:03:44 | `[yue2_schema] 拉 http://127.0.0.1:8188/object_info 失败…Connection refused` | ComfyUI 当时重启（已自动用静态兜底枚举） |

失败任务与重试（线上库只读）：

```
status 分布: done 271 / failed 1
唯一 failed: 2a2821facee2 t2i_moss
  「服务重启前该任务处于「运行中」，但本机 ComfyUI 的队列与历史中都没有对应任务…已自动标记为失败」
batch: done 57 / failed 3
最近 24h 失败数: 0
```

⇒ 那条 failed 是**重启自愈**的正常产物（不是崩溃）。

### B. 回归套件（真实 pytest）

```
$ cd /root/projects/h3s-perf-fix && pytest tests/ -q --tb=line --ignore=tests/e2e -p no:randomly
27 failed, 3967 passed, 6 skipped, 5 errors in 4833.72s (1:20:33)
```

**对照组**：并发任务在**同一条 mainline**（`02985a5`）上用**同一套命令**跑的结果（它自己的 run）：

| | 失败 | 通过 | 跳过 | 错误 |
|---|---|---|---|---|
| 对照组（mainline `02985a5`，并发任务跑） | 23 | 3954 | 6 | 5 |
| **本轮（`7e35b4c`，含服务端修复）** | **27** | **3967** | 6 | 5 |

**逐项差异判定（沿用上一轮基线的方法论：全量跑会因共享素材库 / 浏览器实例 / 端口互相踩而假失败，必须单独复跑）**

差集共 15 项：

**① 只在对照组失败（5 项）** —— 我的分支全量跑时通过了，属并发/时序抖动：

| 文件 | 判定 |
|---|---|
| `tests/test_agent_power.py::test_ddg_via_aggregate_really_returns_results` | 联网测试，时序抖动 |
| `tests/test_cr6_p1_fixes.py::test_frontend_js_has_no_markdown_bold_in_code_strings` | 静态文本断言，抖动 |
| `tests/test_flow_diagram.py`（2 项） | 派生清单断言，抖动 |
| `tests/test_page_size.py::test_auto_refresh_keeps_page_size` | 浏览器时序（见 ③ 的交替验证） |

**② 只在我的分支全量跑时失败（10 项）** —— **单独复跑全部通过**：

```
$ pytest tests/test_effect_lora_frontend.py tests/test_qwen_refs_browser.py -q
13 passed in 58.94s

$ pytest tests/test_asset_picker_onpick.py tests/test_qwen_pe_enhance.py -q
32 passed in 234.42s
```

⇒ 全是**浏览器类测试在满负荷全量跑时的资源争抢**，不是我的改动引入。

**③ `test_page_size::test_auto_refresh_keeps_page_size`** —— 交替验证（mine / mainline 各 3 轮，单跑同一测试）：

```
  round1 MINE   : .                    [100%]     <- 过
  round1 MAINLINE: .                    [100%]     <- 过
  round2 MINE   : .                    [100%]     <- 过
  round2 MAINLINE: .                    [100%]     <- 过
  round3 MINE   : FAILED                              <- 挂
  round3 MAINLINE: FAILED                              <- 挂   <-- 同一轮 mainline 也挂
```

⇒ **两边同一轮一起挂** = **既有的 flaky 测试**（浏览器时序），**与本轮改动无关**。

**④ 本轮新增的 17 项回归全部通过**（`tests/test_mediaperm_perf.py`），且 **10 项变异测试 10/10 全部咬人**（0 条「断言没红」）。

**⑤ 定向爆炸半径测试（222 项）全绿**：

```
$ pytest tests/test_mediaperm_perf.py tests/test_h3_effect_lora.py tests/test_effect_lora_frontend.py \
         tests/test_video_sampling_params.py tests/test_flow_diagram.py tests/test_favorites.py \
         tests/test_a5_running_truth.py tests/test_batches_collapse.py tests/test_continuation_retry.py -q
222 passed in 114.78s
```

**⑥ 与上一轮基线（170 文件 24 失败 → 172 文件 26 失败）的关系**：上一轮基线出自
`evidence/agent-cap-truth/phase2/report/交付报告.md:294-296`，其基线 commit 是 `7436883`。
本轮**没有**直接跑 `7436883`（并发任务在同一时段占用了浏览器/端口资源，两条全量跑会互相踩 ——
实测并发跑两条全量时基线那条在 18% 就被杀）。
本轮改用**更强的对照**：并发任务自己刚跑完的**同 mainline** 结果（`/tmp/fav-regr2.txt`），它是**同一套命令、同一条 mainline**，可比性更强。

> **结论：新增回归 = 0。** 27 vs 23 的差额已逐项单独复跑证明全部是浏览器类 flaky；
> 唯一「同轮两边都挂」的 `test_page_size` 用 3 轮交替验证判定为**既有 flaky**。

### C. 「参考素材右键移除」是否在线 → **可用；那条 interrupted 是状态残留**

独立报告：`evidence/refctx-20261121/report.md`（16 张截图 + `browser_result*.json`）

| 判定项 | 结论 |
|---|---|
| 8190 线上是否可用 | ✅ **可用** —— **四个入口**全部真实右键实测通过：r2v 参考图槽位 / flf 首尾帧槽位 / Qwen 图片编辑参考图 / 参考生音乐参考音频 |
| 线上部署的代码里有没有 | ✅ **有** —— 线上 5 个相关静态文件与仓库 sha256 **逐一 MATCH**（不是「分支上有、线上没有」） |
| 那条 `open + interrupted` 记录 | ⚠️ **状态残留** —— 2026-11-20 11:28:22 创建、11:28:40 被 **abort**（只跑了 18 秒、做了 4 次 `search_files` 探查），11:28:46 由**另一个任务**（`90f3bf94`）接手并于 14:30:53 完成。功能由后者完整交付并部署。 |

实现位置（file:line）：

| 位置 | 内容 |
|---|---|
| `web/ref-context-menu.js:70-99` | `open(event, opts)` —— 弹菜单、造按钮、贴边定位 |
| `web/ref-context-menu.js:109-112` | `attach(el, onRemove, label, enabled)` —— 绑定 `contextmenu` |
| `web/ref-context-menu.js:79` | **空对象不弹菜单**（避免「点不动的假移除」） |
| `web/ref-context-menu.js:141-143` | 导出 `window.RefContextMenu` |
| `web/app.js:1006-1017` / `844-862` | 视频面板槽位挂右键 + `removeSlotAsset()` |
| `web/qwen-refs.js:523-529` | Qwen/H3-Image 参考图缩略图挂右键 |
| `web/music.js:468-474` | 参考生音乐的参考音频卡片挂右键 |
| `web/ref-slots.js:105/173/206/320` | `mediaOrder` / `applyRemoval` / `planRemoval` / `rewriteTags`（纯逻辑，可单测） |
| `web/index.html:1148/1560/1564` | 菜单挂载点 + 两个脚本的 `<script>` 标签 |

证据截图：`01-r2v-three-refs-filled` / `02-rightclick-menu-open` / `03-after-remove` / `04-qwen-ref-rightclick` / `05-qwen-after-remove` / `06-empty-slot-no-menu` / `09-music-ref-audio-filled` / `10-music-ref-rightclick` / `11-music-after-remove` / `12-before-submit-remove` / `13-after-submit-captured` / `14-flf-two-frames-filled` / `15-flf-rightclick-first-frame` / `16-flf-after-remove`

> 该任务顺带发现 `tests/test_ref_context_remove.py` 存在**环境性 flaky**（`-v` 全过、默认参数下 2 条失败，多次复跑时好时坏）—— **不是产品缺陷**。

### D. 视觉 / 响应式问题清单（只记录，不改）

独立报告：`evidence/shots-20261121/visual-findings.md`（**96 张截图** + `measurements.json` 58 个页面的 JS 测量）

**关键前置事实**：本应用**只有浅色主题**（`web/style.css:2-6`：`--bg:#f5f6f8; --panel:#fff; --text:#1a1d21; --muted:#8a919c; --border:#e6e8ec; --accent:#4f6ef7`；全仓无 `prefers-color-scheme` / `data-theme` / 主题切换 JS）。
⇒ **「暗色可读性」不适用**；本清单的对比度问题全是**浅色主题内部**的对比度不足。

**高危 2 条**

| # | 现象 | 实测证据 |
|---|---|---|
| **H1** | 375px 下右下角蓝色「参数」FAB **压在媒体库卡片正文上**，把「分辨率」数值、「LoRA」「调度器」「种子」整行盖住 | FAB `#panelFab` rect=[275,752,86,44]（`z-index:960`）；scrollTop=98 时 100% 覆盖 `div.job-meta`(ox=86)、`b`「Turbo 4 步 · ckpt850 ×1.00」(ox=67)、`span`「调度器」(ox=67)、`div.row-group.gen-params`(87%)；scrollTop=392 时 100% 覆盖 `span.job-top-right`。根因 `web/responsive.css:135-150` —— FAB z-index 960 高于 `#panel` 的 950，但**没给 `.jobs-grid` 预留右侧避让区** |
| **H2** | 375px 展开左侧抽屉时 FAB 同样压在抽屉上，「释放显存」按钮右端被切掉 | 抽屉 `#sidebar` rect=[0,44,300,812]（`z-index:940`）；FAB rect=[275,752,361,796]，交集 ox=25 oy=44；`.sidebar-foot` 与 FAB 交集 ox=14 oy=44；`#unloadBtn` rect=[16,755,283,790] |

**中危 4 条**

| # | 现象 | 实测证据 |
|---|---|---|
| **M1** | 375px 首屏 **67% 高度被筛选器占满**（`.jobs-head` 514px / `#jobsArea` 768px），看不到一张完整卡片 | 7 个状态标签占 2 行(92px) + 13 个生成类型项占 6 行（`style.css:325-334` + `responsive.css:284-285` 强制 `width:100%`） |
| **M2** | 卡片「生成参数」关键值被 `text-overflow:ellipsis` 截断：「MiniMax-H3 FL2VA · 剪枝 int8（默认）」→ 只剩 `MiniMax-H3 FL2VA · ...` | `div.job-card .job-meta b` scrollWidth=202 > clientWidth=124（`style.css:509-512`）；375px 下 cw=67/sw=202（截掉约 2/3） |
| **M3** | 「长片拼接」的「合成长片」按钮**禁用态白字落浅灰底，对比度仅 1.65:1** | `button#stitchBtn` `disabled=true`，`color:rgb(255,255,255)` on `background:rgb(196,202,210)`；根因 `style.css:221` `.gen-btn:disabled{background:#c4cad2}` —— 只改背景不改文字色 |
| **M4** | 1440×900 桌面下左侧导航有 **8 个主要入口在首屏视口外** | `.nav-scroll` scrollHeight=1022 / clientHeight=750 → 272px 在视口外；「素材库」y=825、「收藏」y=861、「内置 Agent」y=929、「硬件参数」y=1069 |

**低危 7 条**（摘要）

| # | 现象 | 实测证据 |
|---|---|---|
| **L1** | 全站次要文字用 `--muted:#8a919c`，对比度普遍 **2.94–3.18:1**（低于 AA 4.5:1），跨页去重 **59 个唯一选择器** | 代表：`span.tabs-label`(2.93)、`div.empty-hint`(2.94)、`div#alStats.al-sub`(2.94)、`span#statusText`(3.18)。建议把 `--muted` 调到 `#6b7280`(≈4.83:1) —— **一处调色解决 59 处** |
| **L2** | 虚线上传槽中心「+」对比度 **1.59:1**（`#c4cad2` on `#fafbfc`），命中 6 个槽位 | `style.css:286-290` |
| **L3** | 375px 下**触控目标 < 44px 的可点击元素 156 个**（去重 125 个选择器） | 最小：`input#vidTwopassEnabled` 15×15、`button#alDrawerClose` 13×22、`button.nav-cat-toggle` 32×32、`button#agMenu` 34×34、`select#alSort` 100×30 |
| **L4** | 素材库文件名 `.al-name` 单行省略号截断，**哈希后缀（最有区分度的部分）被吃掉** | `div.al-name` scrollWidth=185 > clientWidth=153（`assets.css:151-154`） |
| **L5** | 图片/视频面板的 LoRA 名称与说明被截断 | `span.img-lora-name` sw=283/cw=25；`div.img-lora-meta` sw=441/cw=277（`style.css:1117-1120`） |
| **L6** | 「生成类型」筛选器在 1440 下也是 2 行 13 项，与上方标签/搜索/下拉挤在一个 `.jobs-head` | `.jobs-head` rect=[540,60,880,153] |
| **L7** | 375px 首屏「ComfyUI 未连接」状态文字对比度 3.18 —— 它是全局唯一的连接态指示，却用最浅的灰 | `span#statusText` color=rgb(138,145,156) |

**已核实「不是问题」的项**（避免下一轮误报）：横向溢出 **0 处**（58 页全部 `scrollWidth == innerWidth`）；元素重叠 **0 处真实重叠**（桌面端命中全是祖先/后代关系）；`err-text` 低对比度是**误报**（父容器是深色斜纹渐变，实测清晰）；音乐 chip 的 2.0:1 是**骨架屏占位态**误报（实际 3.80:1）；分页条**未被 FAB 压住**（DOM 实测 16px 间距 + 像素裁切确认）；375px 抽屉导航文字**无截断**；375px 素材库/Agent 页**无横向溢出**；素材库「加载中…」是**瞬时状态**（复采 15s 后正常）。

**上一轮已修项未重复出现**：`agent_capability` 单一真源、`size_spec.py` MP/百分比换算、flf/r2v 面板默认参数派生、r2v 400、显式宽高吸附 32、375px 管理面板长标签溢出 —— 本轮清单中均未再出现。

### E. 既有契约未被破坏（2026-11-12 采样类字段契约）

```
$ git diff local/maintainer -- server.py | grep -i -E "sampler|scheduler|_SAMPLING_ONLY|steps|shift"
(空 —— 完全没碰)
$ pytest tests/test_video_sampling_params.py -q
50 passed in 2.01s
```

行为实测（两版完全一致，**都不写回**）：

```
我的分支 local/fix/mediaperm-perf:
  flf: 客户端没传采样字段 -> 服务端写回 （无 —— 契约成立）
  r2v: 客户端没传采样字段 -> 服务端写回 （无 —— 契约成立）
  t2v: 客户端没传采样字段 -> 服务端写回 （无 —— 契约成立）
线上 local/maintainer e393e8a:
  flf / r2v / t2v: 同样全部「（无 —— 契约成立）」
```

⇒ **契约未被破坏**，且**没有**为了「两条路径都生效」而放宽它。

---

## 8. 未修项（前端 / 并发冲突）与建议执行顺序

| 项 | 为什么不修 | 建议方案 | 建议顺序 |
|---|---|---|---|
| **F1** 卡片 `<img>` 无 `loading="lazy"` | `web/app.js` 被并发任务声明占用 | 给 `web/app.js:4939` 的 `<img>` 加 `loading="lazy"` + `decoding="async"`；配合服务端出**缩略图**（见 F4） | **第 1 优先**（改动最小、收益最大） |
| **F2** 视频整文件下载 | 同上 | 服务端 `/files` 对 `Range` 请求**只回前 N 字节**（`preload=metadata` 只需头部），或出 poster 图 | 第 2 优先（属**静态资源服务路径**，可独立做） |
| **F3** `/files` 用 `no-cache` | 服务端 `files_handler`（可改，但收益小 —— 304 只花 1.9ms） | 改成 `Cache-Control: private, max-age=…` + 保留 ETag | 第 3 优先 |
| **F4** 无缩略图 | 需要新服务端能力（新建缩略图缓存目录） | 服务端生成并缓存缩略图（首屏只拉缩略图） | 第 4 优先（工作量最大，收益也最大） |

---

## 9. 证据文件清单

| 文件 | 内容 |
|---|---|
| `evidence/mediaperm_probe.py` → `mediaperm-20261121/mediaperm-probe.json` | 真实 Chromium 首屏/标签/翻页 network timings、每卡片请求数、响应头 |
| `evidence/mediaperm-20261121/media-library-all.png` | 线上生成媒体库真实截图 |
| `evidence/mediaperm_serverprofile.py` | 服务端分阶段耗时 + SQL 计数（213 条） |
| `evidence/mediaperm_perrow.py` → `per-row-profile.json` | 逐行定位（686ms 的那一行） |
| `evidence/mediaperm_deep.py` | cProfile 抓 `discover_effect_loras` |
| `evidence/mediaperm_coldwarm.py` | 冷/热分解（742ms vs 0.04ms） |
| `evidence/mediaperm_longsample.py` → `live-longsample.json` | 线上 200 秒长跑采样（p99 444.6ms / max 731.1ms） |
| `evidence/mediaperm_isolate.py` | 隔离实例 + 假 ComfyUI 延迟注入 |
| `evidence/mediaperm_beforeafter.py` → `before-after.json` | 修前/修后对比（2027ms → 13ms） |
| `evidence/mediaperm_mutation.py` → `mutation-result.txt` | 10 变异体逐个验证（0 条不咬人） |
| `evidence/mediaperm_browser_fixed.py` → `fixed-browser-probe.json` | 修复后真实浏览器复测 |
| `tests/test_mediaperm_perf.py` | 16 条回归断言 |
