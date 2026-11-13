# 验收证据索引 — 文生视频接续（t2v）

> 本证据包验收的是「文生视频接续（t2v）」功能本身，不是 H3 视频面板的 7 项修复。

- 分支：`local/feature/t2v-continue`　仓库：`/root/projects/h3studio-t2v-continue`
- 生成时间：2026-11-13T19:08:46+08:00
- 机器可读版：`evidence/acceptance/INDEX.json`
- 验收实例：8206 / 8199（独立实例，8190 线上未动）

---

## C1　兄弟分支落盘互不覆盖

**结论**：每个分支一个专属目录（run_id 由该段自己的 job_id 派生），目录不同 -> 结构上不可能互相覆盖

| 分支 | job_id | run_id | stage 文件（绝对路径） | 盘上文件 | 指纹 |
|---|---|---|---|---|---|
| **B1** | `a5d446b1605e` | `h3studio_sa5d446b1605e` | `/home/ubuntu/comfyui/output/relay_kit/h3studio_sa5d446b1605e/stage_00000.safetensors` | stage_00000.safetensors, stage_00000_cb5250d8.safetensors | cb5250d8 |
| **B2** | `e87e76812bf0` | `h3studio_se87e76812bf0` | `/home/ubuntu/comfyui/output/relay_kit/h3studio_se87e76812bf0/stage_00000.safetensors` | stage_00000.safetensors | （未生成读回副本） |

- B1 与 B2 的落盘目录**不同** -> 结构上不可能互相覆盖（不是靠「先后顺序不撞」的运气约束）
- 全部 5 段（chain_root `f1569b50dee7`）：

| job_id | stage | source_job | run_id |
|---|---|---|---|
| `f1569b50dee7` | 0 | `—` | `h3studio_sf1569b50dee7` |
| `a5d446b1605e` | 1 | `f1569b50dee7` | `h3studio_sa5d446b1605e` |
| `e87e76812bf0` | 1 | `f1569b50dee7` | `h3studio_se87e76812bf0` |
| `c09b1c43cff1` | 2 | `a5d446b1605e` | `h3studio_sc09b1c43cff1` |
| `c534c11e3c3a` | 3 | `c09b1c43cff1` | `h3studio_sc534c11e3c3a` |

## C2　接续首帧归属（对照已判定的 2.08 vs 38.65）

**结论**：C1 首帧来自 B1（正确分支），与 B2 显著拉开

| 比较 | 灰度 MAE |
|---|---|
| C1首帧 vs **B1末帧**（正确分支） | **5.785** |
| C1首帧 vs **B2末帧**（兄弟分支） | **63.988** |
| 偏离倍数 | **11.06×** |

**缺陷态对照**（D1 调研报告实测）：D1 调研报告实测的缺陷态：C1首帧 vs B1末帧 = 2.08，vs B2末帧 = 38.65 —— 即接到了兄弟分支 B2，偏离 18.6 倍

- 缺陷态：C1首帧 vs B1末帧 = **2.08**，vs B2末帧 = **38.65**（偏离 18.58×）—— 即接到了兄弟分支 B2
- 修复后：5.785 vs 63.988（偏离 11.06×）—— 接的是 B1，方向已反转

**执行图级主判据**（ComfyUI 真正跑的那张图 —— 确定性，不依赖模型表现）：

- C1 实际读回：`/home/ubuntu/comfyui/output/relay_kit/h3studio_sa5d446b1605e/stage_00000_cb5250d8.safetensors`
- B1 落盘 run_id：`h3studio_sa5d446b1605e`　B2 落盘 run_id：`h3studio_se87e76812bf0`
- 读的是 B1：**True**（B2 的目录从未被读）
- 外观锚**显式传入**：True　`ref_anchor_latent = ['300:anchor_load', 0]`
- 锚读回路径：`/home/ubuntu/comfyui/output/relay_kit/h3studio_sf1569b50dee7/stage_00000_418e1249.safetensors`

**对比图**（绝对路径）：

- 三联图_B1末_B2末_C1首：`/root/projects/h3studio-t2v-continue/evidence/acceptance/frames/triptych-B1last-B2last-C1first.png`
- B1末帧：`/root/projects/h3studio-t2v-continue/evidence/acceptance/frames/B1-v-h3-studio-000020-last.png`
- B2末帧：`/root/projects/h3studio-t2v-continue/evidence/acceptance/frames/B2-v-h3-studio-000022-last.png`
- C1首帧：`/root/projects/h3studio-t2v-continue/evidence/acceptance/frames/C1-v-h3-studio-000024-first.png`

## C3　链账本不按 chain_id 线性拼接

**结论**：服务端按 source_job 指针回溯血缘，每条分支只含自己的前序段；B2 不出现在 B1 分支的完整视频里

| job_id | stage | 血缘段序列 | 各段帧数 | 各段时长(s) | 完整视频帧数 | 期望 | 完整视频时长(s) |
|---|---|---|---|---|---|---|---|
| `f1569b50dee7` | 0 | v-h3-studio-000019.mp4 | [124] | [5.167] | None | 124 | None |
| `a5d446b1605e` | 1 | v-h3-studio-000019.mp4 + v-h3-studio-000020.mp4 | [124, 102] | [5.167, 4.25] | 226 | 226 | 9.45 |
| `e87e76812bf0` | 1 | v-h3-studio-000019.mp4 + v-h3-studio-000022.mp4 | [124, 102] | [5.167, 4.25] | 226 | 226 | 9.45 |
| `c09b1c43cff1` | 2 | v-h3-studio-000019.mp4 + v-h3-studio-000020.mp4 + v-h3-studio-000024.mp4 | [124, 102, 102] | [5.167, 4.25, 4.25] | 328 | 328 | 13.7 |
| `c534c11e3c3a` | 3 | v-h3-studio-000019.mp4 + v-h3-studio-000020.mp4 + v-h3-studio-000024.mp4 + v-h3-studio-000026.mp4 | [124, 102, 102, 102] | [5.167, 4.25, 4.25, 4.25] | 430 | 430 | 17.95 |

- **B2 的段（v-h3-studio-000022.mp4）只出现在 B2 分支**，不出现在 B1/C1/D1 任何一条血缘里
- 各分支完整视频的绝对路径见 `INDEX.json` 的 `C3_链账本不按chain_id线性拼接.各分支[*].完整视频文件`

## C4　内容指纹机制 —— 与 nonebot 咏唱 seg 工作流对照

**结论**：与 nonebot 咏唱 h3 的 _latent_fingerprint 同一口径：文件大小 + 头8MB + 尾8MB 进 md5，取前8位；副本命名 stage_<N>_<指纹>.safetensors；内容变->指纹变->路径变->explicit_path 变->ComfyUI 节点缓存必然失效

| 维度 | nonebot 咏唱 h3 | 本项目 |
|---|---|---|
| 指纹算法 | `_latent_fingerprint` | `latent_fingerprint` |
| 读取范围 | 头 8MB + 尾 8MB + **文件大小**进哈希 | 同 |
| 指纹长度 | `md5[:8]` | `md5[:8]` |
| 副本命名 | `stage_<N>_<指纹>.safetensors` | 同 |
| 破缓存原理 | 内容变->指纹变->路径变->`explicit_path` 变->缓存 key 变 | 同 |
| 旧副本清理 | 清同 `stage_<N>_*` 非当前指纹 | `cleanup_fingerprint_copies` 同规则 |
| 落盘坐标制度 | `run_id = long_id` + 长视频级互斥锁（链线性） | `run_id = h3studio_s<job_id>`（每段一目录，链可分叉） |

**落盘制度为何不同**：nonebot 是「一部片子一个目录 + 长视频级互斥锁」（链线性）；本项目链允许分叉，互斥锁挡不住「先后两次」的分叉，所以用「每段一个专属目录」= 类型层面的不变量

**代码位置**：

- `latent_fingerprint`：`h3studio/continuation_service.py:254`
- `fingerprinted_stage_path`：`h3studio/continuation_service.py:286`
- `ensure_fingerprinted_copy`：`h3studio/continuation_service.py:304`
- `cleanup_fingerprint_copies`：`h3studio/continuation_service.py:336`
- `调用点_读回路径`：`server.py:2013-2032（_prepare_continuation_params）`

**nonebot 参考位置**：

- `_latent_fingerprint`：`/home/ubuntu/nonebotqq/plugins/nonebot_plugin_novelai/h3_chain_video.py:1348`
- `指纹副本写入`：`/home/ubuntu/nonebotqq/plugins/nonebot_plugin_novelai/h3_chain_video.py:1485-1500`
- `prev_latent_explicit_path`：`/home/ubuntu/nonebotqq/plugins/nonebot_plugin_novelai/h3_chain_video.py:1523`
- `参考工作流`：`['/home/ubuntu/nonebotqq/plugins/nonebot_plugin_novelai/workflows/minimax_h3_v1.json', '/home/ubuntu/nonebotqq/plugins/nonebot_plugin_novelai/workflows/minimax_h3_seg1.json', '/home/ubuntu/nonebotqq/plugins/nonebot_plugin_novelai/workflows/minimax_h3_seg2.json']`

**本机实际指纹副本**（绝对路径）：

- `/home/ubuntu/comfyui/output/relay_kit/h3studio_sf1569b50dee7/stage_00000.safetensors`　指纹=418e1249　指纹副本=True　4827267 字节
- `/home/ubuntu/comfyui/output/relay_kit/h3studio_sf1569b50dee7/stage_00000_418e1249.safetensors`　指纹=418e1249　指纹副本=True　4827267 字节
- `/home/ubuntu/comfyui/output/relay_kit/h3studio_sa5d446b1605e/stage_00000.safetensors`　指纹=cb5250d8　指纹副本=True　4827267 字节
- `/home/ubuntu/comfyui/output/relay_kit/h3studio_sa5d446b1605e/stage_00000_cb5250d8.safetensors`　指纹=cb5250d8　指纹副本=True　4827267 字节
- `/home/ubuntu/comfyui/output/relay_kit/h3studio_se87e76812bf0/stage_00000.safetensors`　指纹=8b2773a5　指纹副本=True　4827267 字节
- `/home/ubuntu/comfyui/output/relay_kit/h3studio_sc09b1c43cff1/stage_00000.safetensors`　指纹=208432ea　指纹副本=True　4827267 字节
- `/home/ubuntu/comfyui/output/relay_kit/h3studio_sc09b1c43cff1/stage_00000_208432ea.safetensors`　指纹=208432ea　指纹副本=True　4827267 字节
- `/home/ubuntu/comfyui/output/relay_kit/h3studio_sc534c11e3c3a/stage_00000.safetensors`　指纹=070fdf79　指纹副本=True　4827267 字节

## C5　两轮真实接续

**结论**：A -> B1 -> C1 -> D1 四段真实跑通；第 1 轮完整视频 = A+B1+C1；第 2 轮对完整视频再接续 = A+B1+C1+D1

| job_id | stage | source_job | 分段帧数 | 分段时长(s) | 完整视频帧数 | 完整视频时长(s) |
|---|---|---|---|---|---|---|
| `f1569b50dee7` | 0 | `—` | 124 | 5.167 | —（首段无拼接） | — |
| `a5d446b1605e` | 1 | `f1569b50dee7` | 102 | 4.25 | 226 | 9.45 |
| `e87e76812bf0` | 1 | `f1569b50dee7` | 102 | 4.25 | 226 | 9.45 |
| `c09b1c43cff1` | 2 | `a5d446b1605e` | 102 | 4.25 | 328 | 13.7 |
| `c534c11e3c3a` | 3 | `c09b1c43cff1` | 102 | 4.25 | 430 | 17.95 |

- 帧数账：A(124) + B1(102) + C1(102) = 328　/　328 + D1(102) = 430
- 逐段视频绝对路径见 `INDEX.json` 的 `C5_两轮真实接续.逐段[*].分段视频文件` / `.完整视频文件`
- ⚠ 视频**不入库**（`.gitignore` 排除 `*.mp4`），索引给的是绝对路径

## C6　selflift（二采）卡片**不出现**「接续」按钮

**结论**：二采（SelfLift）产物服务端判定 can_continue=false，卡片**不渲染**「接续」按钮；普通 t2v 产物则渲染

- selflift job `f3a65a82b9db`：`can_continue = False`
- 原因：二采（SelfLift 渐进分辨率采样）的潜空间与标准采样路径不同源，接续会出现接缝位移 —— 二采产物不支持接续。
- 普通 t2v job `c3ac49a9cd05`：`can_continue = True`

**DOM 按钮清单**（两档宽度实测）：

| 宽度 | 卡片 | 有「接续」按钮 | 全部按钮 |
|---|---|---|---|
| 375px | selflift | **False** | ['prompt', 'download', 'reuse', 'delete'] |
| 375px | normal | **True** | ['prompt', 'download', 'continue', 'reuse', 'delete'] |
| 1280px | selflift | **False** | ['prompt', 'download', 'reuse', 'delete'] |
| 1280px | normal | **True** | ['prompt', 'download', 'continue', 'reuse', 'delete'] |

**截图**（绝对路径）：

- `/root/projects/h3studio-t2v-continue/evidence/continuation-selflift/selflift-375-gallery.png`
- `/root/projects/h3studio-t2v-continue/evidence/continuation-selflift/selflift-375-card-zoom.png`
- `/root/projects/h3studio-t2v-continue/evidence/continuation-selflift/normal-375-card-zoom.png`
- `/root/projects/h3studio-t2v-continue/evidence/continuation-selflift/continuation-panel-375.png`
- `/root/projects/h3studio-t2v-continue/evidence/continuation-selflift/selflift-1280-gallery.png`
- `/root/projects/h3studio-t2v-continue/evidence/continuation-selflift/selflift-1280-card-zoom.png`
- `/root/projects/h3studio-t2v-continue/evidence/continuation-selflift/normal-1280-card-zoom.png`
- `/root/projects/h3studio-t2v-continue/evidence/continuation-selflift/continuation-panel-1280.png`

## C7　「接续」配置区块 —— 只能由生成库添加

**结论**：接续上下文区块里**只有「清除」**，没有任何「新增/添加」入口 —— 只能由生成库卡片点「接续」进来；点进来后配置被自动回填，提示词留空给用户写下一段

**区块实测**（点普通卡的「接续」之后）：

| 宽度 | 区块按钮 | 有新增/添加入口 | 区块隐藏 |
|---|---|---|---|
| 375px | ['清除'] | **False** | False |
| 1280px | ['清除'] | **False** | False |

**代码位置**：

- `HTML_区块`：`web/index.html（#continuationPanel）`
- `JS_渲染与回填`：`web/continuation.js（renderPanel / start / clear）`
- `JS_入口按钮`：`web/app.js:3228（canShowButton -> data-act="continue"）`
- `JS_点击处理`：`web/app.js:3348`
- `服务端_上下文接口`：`server.py:3075（GET /api/jobs/{id}/continuation）`
- `服务端_接续上下文组装`：`h3studio/continuation_service.py:493（build_continuation_context）`

**截图**（绝对路径）：

- `/root/projects/h3studio-t2v-continue/evidence/continuation-selflift/continuation-panel-375.png`
- `/root/projects/h3studio-t2v-continue/evidence/continuation-selflift/continuation-panel-1280.png`

## C8　pytest 命令与结果

**接续相关**：121 passed in 60.65s (0:01:00)

```bash
cd /root/projects/h3studio-t2v-continue && /home/ubuntu/comfyui/venv/bin/python -m pytest tests/test_continuation_branch.py tests/test_continuation_server.py tests/test_continuation.py tests/test_continuation_retry.py tests/test_continuation_frontend.py tests/test_continuation_mixed_state.py tests/test_continuation_capability.py -q --no-header -p no:cacheprovider
```

**全量**：3 failed, 2289 passed, 4 skipped in 1515.35s (0:25:15)

```bash
cd /root/projects/h3studio-t2v-continue && /home/ubuntu/comfyui/venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider
```

**全量失败的 3 项**（逐项判定）：

- `test_studio.py::TestLoRARegistry::test_scanner_only_returns_z_image_loras`
  - 稳定存量失败（maintainer 基线同样失败，与接续无关）
- `test_yue2_flow_review_fixes.py::TestWriteContractDeclaration::test_every_method_declares_write_contract`
  - 稳定存量失败（maintainer 基线同样失败，与接续无关）
- `test_page_size.py::TestPageSize::test_pager_boundary_buttons_disabled`
  - 时序 flake —— 单独跑 3 次全过（22 passed x3）；该文件 0 处引用接续代码；本轮未改任何生产代码

## C9　375 / 1280 两档浏览器截图 + console / pageError 统计

**两档宽度**：[375, 1280]

**截图目录**（绝对路径）：

- `/root/projects/h3studio-t2v-continue/evidence/continuation-selflift`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-selflift/continuation-panel-1280.png`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-selflift/continuation-panel-375.png`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-selflift/normal-1280-card-zoom.png`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-selflift/normal-375-card-zoom.png`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-selflift/report.json`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-selflift/selflift-1280-card-zoom.png`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-selflift/selflift-1280-gallery.png`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-selflift/selflift-375-card-zoom.png`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-selflift/selflift-375-gallery.png`
- `/root/projects/h3studio-t2v-continue/evidence/continuation-d1`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-d1/browser_report.json`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-d1/cont-1280-01-gallery.png`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-d1/cont-1280-02-backfilled.png`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-d1/cont-1280-02b-context-panel.png`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-d1/cont-1280-02c-left-panel.png`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-d1/cont-1280-03-prompt.png`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-d1/cont-375-01-gallery.png`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-d1/cont-375-02-backfilled.png`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-d1/cont-375-02b-context-panel.png`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-d1/cont-375-02c-left-panel.png`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-d1/cont-375-03-prompt.png`
- `/root/projects/h3studio-t2v-continue/evidence/continuation-d1-four`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-d1-four/B1-v-h3-studio-000020-last.png`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-d1-four/B2-v-h3-studio-000022-last.png`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-d1-four/B2f-v-h3-studio-000022-first.png`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-d1-four/B2l-v-h3-studio-000022-last.png`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-d1-four/C1-v-h3-studio-000024-first.png`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-d1-four/graph_evidence.json`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-d1-four/report.json`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-d1-four/triptych-B1last-B2last-C1first.png`
  - `/root/projects/h3studio-t2v-continue/evidence/continuation-d1-four/verify.log`

**报错统计**：

- `continuation-selflift(report.json)`
  - console_errors：0
  - page_errors：0
  - real_failed_requests：0
  - media_preload_aborts：4
  - media_preload_aborts说明：卡片 <video preload="metadata"> 被 Chrome 读完元数据后主动中止媒体流（net::ERR_ABORTED）—— 浏览器正常行为，非服务端错误（同一 URL 手动 fetch 返回 200 且字节完整）
- `continuation-d1(browser_report.json)`
  - console_errors：0
  - page_errors：0
  - failed_requests：0
  - shots：10
