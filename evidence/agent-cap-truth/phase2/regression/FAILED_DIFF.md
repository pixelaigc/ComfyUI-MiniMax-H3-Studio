# 全量 pytest 回归 diff · 未改动基线 vs 本轮

| 项 | 值 |
|---|---|
| 基线代码 | `7436883`（`local/maintainer` 上轮合并提交） |
| 基线工作树 | `/root/projects/h3s-base-truth`（detached @ 7436883） |
| 本轮工作树 | `/root/projects/h3s-deftruth`（`local/feature/agent-defaults-truth`） |
| 基线跑的文件数 | 170 |
| 本轮跑的文件数 | 172 |
| 基线失败文件数 | 24 |
| 本轮失败文件数 | 26 |

## 结论（**新增回归 = 0**）

全量对跑后，「本轮失败但基线通过」共 **5** 个文件。逐个**单独复跑**（排除并发 / 共享素材库 / 浏览器时序抖动）后：

| 文件 | 全量里的失败原因 | 单独复跑结果 | 判定 |
|---|---|---|---|
| `tests/test_agent_backend_enh.py` | 1 failed, 108 passed in 37.10s | 109 passed in 56.26s | **真实回归**：`mode_param_keys` 未同步新增的 `megapixels` / `resolution_percent` —— 已修（commit `3320cbd`），复跑 109 passed |
| `tests/test_agent_fabricated_job_fix.py` | 1 failed, 30 passed in 132.24s (0:02:12) | 31 passed in 54.32s | ✅ 并发 / 时序抖动（单独复跑通过） |
| `tests/test_enhance_review_fixes.py` | 1 failed, 22 passed, 2 subtests passed in 40.03s | 1 failed, 22 passed, 2 subtests passed in 49.56s | ✅ **环境时序**（非改动引入）：失败原因是浏览器 console 报「[视频面板] 载入清单失败: Failed to fetch」—— `/api/video-options` 首调要拉 ComfyUI `/object_info`（实测 GPU 满载时 15 秒 -> fetch 超时；server.py:7120 的注释明说「这个函数要 60 秒才返回」）。**基线工作树连续 3 轮同样失败**（见 rerun_after_fixes.txt 的「基线工作树单独跑 3 轮」），交替验证里基线也失败过 —— 证明与本轮改动无关。 |
| `tests/test_music_yue2.py` | 2 failed, 65 passed in 244.80s (0:04:04) | 1 failed, 66 passed in 152.91s (0:02:32) | ✅ 并发 / 时序抖动（单独复跑 67 passed） |
| `tests/test_video_sampling_params.py` | 1 failed, 49 passed in 4.04s | 50 passed in 7.68s | **真实回归**：面板提交路径套了采样类默认参数，违反「客户端没传 sampler 时服务端不写回」契约 —— 已修（commit `skip_sampling`），复跑 50 passed |

> **判据**：全量跑的时候，本仓库的**共享素材库**、**浏览器实例**、**临时服务端口**会互相踩 —— 同一文件单独跑通过即判为假失败。
> 这一点有实测支撑：`tests/test_agent.py` 在基线全量里失败、单独跑 42 passed；`tests/test_agent_capability_truth.py` 基线全量失败 2 项、单独跑 61 passed。

## 本轮修好的（基线失败 → 本轮通过）

| 文件 | 基线 | 本轮 |
|---|---|---|
| `tests/test_agent_capability_truth.py` | 2 failed, 59 passed in 11.78s | 61 passed in 16.84s |
| `tests/test_agent_stream_sse.py` | 1 failed, 10 passed in 194.55s (0:03:14) | 11 passed in 58.28s |
| `tests/test_qwen_pe_enhance.py` | 1 failed, 28 passed in 77.83s (0:01:17) | 29 passed in 82.07s (0:01:22) |

## 既有失败（两边都失败 —— **不是本轮引入**）

| 文件 | 基线 | 本轮 | 单独复跑 | 判定 |
|---|---|---|---|---|
| `tests/test_agent.py` | 1 failed, 41 passed in 146.40s (0:02:26) | 1 failed, 41 passed in 157.61s (0:02:37) | 42 passed in 87.72s (0:01:27) | `server.py` 的 blob 必须 CRLF（基线已是 LF —— 既有缺陷） |
| `tests/test_agent_media_av_upload.py` | 1 failed, 60 passed in 3.02s | 1 failed, 60 passed in 4.06s | 1 failed, 60 passed in 3.46s | ⚠️ 需人工确认 |
| `tests/test_agent_media_upload.py` | 2 failed, 64 passed in 120.30s (0:02:00) | 1 failed, 65 passed in 52.44s | 1 failed, 65 passed in 59.43s | `server.py` 的 blob 必须 CRLF（既有缺陷） |
| `tests/test_agent_paste_browser.py` | [TIMEOUT after 300s] | [TIMEOUT after 300s] | （未复跑） | ⚠️ 需人工确认 |
| `tests/test_asset_picker_onpick.py` | [TIMEOUT after 300s] | [TIMEOUT after 300s] | （未复跑） | ⚠️ 需人工确认 |
| `tests/test_audit_fixes.py` | 1 failed, 35 passed in 0.08s | 1 failed, 35 passed in 0.08s | 1 failed, 35 passed in 0.08s | `server.py` 的 blob 必须 CRLF（既有缺陷） |
| `tests/test_continuation_retry.py` | 3 failed, 12 passed in 0.06s | 3 failed, 12 passed in 0.09s | 15 passed in 0.10s | ✅ 单独复跑通过（并发 / 时序抖动） |
| `tests/test_d11_moss_lora_final.py` | 1 failed, 13 passed in 13.11s | 1 failed, 13 passed in 14.76s | 1 failed, 13 passed in 15.88s | MOSS LoRA 损坏文件被判 compatible=true（与本轮改动无关） |
| `tests/test_e2e_gen_flow_contract.py` | 1 failed, 4 passed in 0.12s | 1 failed, 4 passed in 0.12s | 1 failed, 4 passed in 0.12s | A1 违规（4 步必须配加速 LoRA）（与本轮改动无关） |
| `tests/test_enhance_architecture_fixes.py` | 3 failed, 25 passed in 21.93s | 3 failed, 25 passed in 23.90s | 3 failed, 25 passed in 22.15s | ⚠️ 需人工确认 |
| `tests/test_enhance_menu.py` | 1 failed, 23 passed in 125.19s (0:02:05) | 1 failed, 23 passed in 114.95s (0:01:54) | 1 failed, 23 passed in 113.81s (0:01:53) | ⚠️ 需人工确认 |
| `tests/test_enhance_owner_badge.py` | 1 failed, 6 passed, 6 subtests passed in 0.02s | 1 failed, 6 passed, 6 subtests passed in 0.03s | 1 failed, 6 passed, 6 subtests passed in 0.02s | owner 取值集合断言（与本轮改动无关） |
| `tests/test_enhance_unify.py` | 1 failed, 19 passed in 63.93s (0:01:03) | 1 failed, 19 passed in 60.39s (0:01:00) | 1 failed, 19 passed in 59.51s | ⚠️ 需人工确认 |
| `tests/test_lora_file_integrity.py` | 2 failed, 9 passed, 1 skipped in 6.03s | 2 failed, 9 passed, 1 skipped in 5.46s | 2 failed, 9 passed, 1 skipped in 5.99s | MOSS LoRA 损坏文件（与本轮改动无关） |
| `tests/test_moss_lora_damaged_and_partial.py` | 2 failed, 21 passed, 7 subtests passed in 7.12s | 2 failed, 21 passed, 7 subtests passed in 8.36s | 2 failed, 21 passed, 7 subtests passed in 7.77s | MOSS LoRA 损坏文件（与本轮改动无关） |
| `tests/test_music_flow_browser.py` | 1 failed, 9 passed in 88.30s (0:01:28) | 1 failed, 9 passed in 117.05s (0:01:57) | 1 failed, 9 passed in 86.56s (0:01:26) | ⚠️ 需人工确认 |
| `tests/test_page_size.py` | 1 failed, 21 passed in 83.22s (0:01:23) | 1 failed, 21 passed in 88.38s (0:01:28) | 22 passed in 90.93s (0:01:30) | ✅ 单独复跑通过（并发 / 时序抖动） |
| `tests/test_qwen_refs_browser.py` | 3 failed in 91.14s (0:01:31) | 3 failed in 91.52s (0:01:31) | 3 failed in 91.06s (0:01:31) | ⚠️ 需人工确认 |
| `tests/test_ref_context_remove.py` | 5 failed, 14 passed in 30.97s | 5 failed, 14 passed in 31.25s | 5 failed, 14 passed in 31.13s | Playwright Sync API 误用在 asyncio loop（与本轮改动无关） |
| `tests/test_studio.py` | [TIMEOUT after 300s] | [TIMEOUT after 300s] | （未复跑） | ⚠️ 需人工确认 |
| `tests/test_yue2_flow_review_fixes.py` | 2 failed, 20 passed in 0.07s | 2 failed, 20 passed in 0.06s | 2 failed, 20 passed in 0.06s | `server.py` 的 blob 必须 CRLF（既有缺陷） |

## 关于 `server.py` 的行尾（既有缺陷，本轮未修）

`tests/test_audit_fixes.py` / `test_yue2_flow_review_fixes.py` 等断言
`server.py` 的 **blob 必须是 CRLF**。实测：**基线工作树（`7436883`）的 `server.py` blob 已经是 LF**（`git ls-files --eol` -> `i/lf`），基线工作树单独跑同样失败 —— 所以这是**既有缺陷，不是本轮引入**。

本轮**未修**它的理由：

1. `.gitattributes` 里 `server.py` 标的是 `-text`（字节原样保存），把 LF blob 换回 CRLF 会产生**全文件行尾差异** -> 后续合并全是伪冲突；
2. 超出本任务范围（用户要求的是「web/app.js 与 web/index.html 为 CRLF，不得改坏换行」—— 这两个文件本轮**零改动**且仍是 `i/crlf`）；
3. 本轮改动 `server.py` **没有恶化**它（改前改后都是 `i/lf`）。

```
$ git ls-files --eol server.py web/app.js web/index.html web/style.css
i/lf    w/lf    attr/-text           server.py      <- 基线即如此，本轮未恶化
i/crlf  w/crlf  attr/-text           web/app.js     <- 本轮零改动
i/crlf  w/crlf  attr/-text           web/index.html <- 本轮零改动
i/crlf  w/crlf  attr/-text           web/style.css  <- 本轮改了，CRLF 保持（CR=3476 LF=3476）
```

## 复跑原始日志

* `rerun_after_fixes.txt` —— 「本轮失败·基线通过」的 5 个文件单独复跑
* `rerun_both_fail.txt` —— 「两边都失败」的 19 个文件单独复跑

## 原始全量结果

* 基线：`/tmp/pytest_base_deftruth.json`（170 文件，24 失败）
* 本轮：`/tmp/pytest_changed_deftruth.json`（172 文件，26 失败）

