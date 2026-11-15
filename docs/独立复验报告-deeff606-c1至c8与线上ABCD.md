# 独立验收复验报告 —— 内置多模态 Agent 增强 + MOSS-Image LoRA 修复（c1–c8 + 线上 A/B/C/D）

- **验收执行**：`deeff606-34e0-419d-a118-90eded56520e`
- **验收时间**：2026-11-15 15:51 ~ 16:22（CST）
- **验收对象**：`/root/projects/h3studio-live`（分支 `local/deploy/qwen-ref-assets-live`，验收期间 HEAD 由 `613341c` 前进到 `3ab99b0`，均为并发任务部署）
- **线上服务**：`h3-studio.service` 端口 8190，验收期间被并发任务于 16:13:53 重启过一次（**非本次验收操作**），重启后已复测全部关键项，结论不变
- **证据根目录**：`/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/.studio-acceptance/deeff606-34e0-419d-a118-90eded56520e/1/`

---

## 0. 上一轮失败项（c8）的处置结论

上一轮独立验收判定 `c1–c7 passed / c8 failed`，唯一失败原因是 **B 项范围内 2 条 MOSS-Image LoRA 仍以 `compatible=true` 下发**：

| 代号 | 文件 | 上一轮实测 | 本轮实测（线上 8190 + ComfyUI 真实代码路径） |
|---|---|---|---|
| **D1** | `moss-effects/moss_image_sft_look_lora.safetensors` | patched=137 / missing=126 / attention_hit=0.75 | **attention_hit=1.0 / conversion_status=derived**；原文件真实挂载仍是 patched=137/missing=126，但**服务端提交时自动改挂归一派生文件 → patched=263 / missing=0**；实测出图 `263 patches attached`，`lora key not loaded` 警告 **0** 条 |
| **D2** | `moss-effects/hina_MOSSImageBase_animeEnhance_v1.0-lora.safetensors` | `SafetensorError: incomplete metadata`，文件本体损坏 | **文件本体已补齐**：磁盘 297,355,184 B（= 头部声明值，缺 0 字节），`safetensors` 可真实打开（756 张量），真实挂载 **patched=252 / missing=0**，接口 `available` 139 条中 `compatible=true / attention_hit=1.0 / conversion_status=direct` |

**两条缺陷均已解决，不再以「半静默失效」或「损坏文件」形态下发。**

---

## 1. c1–c8 逐条结论

### c1 真实 SSE 抓包（Content-Type: text/event-stream）→ **PASS**

本轮**在 8190 线上重新抓了一次**（不只引用前轮）：

```
HTTP/1.1 200 OK
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache
X-Accel-Buffering: no
Transfer-Encoding: chunked
```

响应体为真实 SSE 分帧，含 41 个 `event: token` token 级增量事件（`{"text": "...", "delta": true, "type": "token"}`）。

- 证据：`c1/c1-live8190-sse-capture.txt`
- 前轮独立端口 8197 的双载体（body / query）抓包：`/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/.studio-acceptance/776edeea-e1d7-4189-ab6a-d982beb941dd/3/c1c2c3/c1-raw-capture-body.http.txt`、`c1-raw-capture-query.http.txt`

### c2 needs_clarification 澄清气泡（含可点击快捷选项）→ **PASS**

前轮在独立端口真实浏览器下取证，判定项 `c2_clarify_clickable_options: true`、`c2_chip_click_fills_input: true`（点击 chip 确实回填输入框）。
DOM 断言记录快捷选项 3 项：`9:16 竖屏` / `16:9 横屏` / `1:1 方形`。

- 证据：`/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/.studio-acceptance/776edeea-e1d7-4189-ab6a-d982beb941dd/3/c1c2c3/acceptance_c123_evidence.json`
- 截图：`.../3/c1c2c3/screenshots/acc-c2-clarify-{375,390,1280}.png`、`.../3/c7/screenshots/c7-clarify-*.png`

### c3 tool_call/tool_result 工具卡（参数 + 结果 + 失败错误文案 + 无空结果块）→ **PASS**

前轮判定项：`c3_toolcard_args_and_result: true`、`c3_failed_tool_shows_error_text: true`、`c3_streaming_increment_observed: true`；三档宽度下 `toolcard_empty_result_blocks = 0`。

- 证据：`.../3/c1c2c3/acceptance_c123_evidence.json`、`.../3/c1c2c3/c3-sqlite-rows.txt`
- 截图：`.../3/c1c2c3/screenshots/acc-c3-failed-tool-{375,390,1280}.png`、`.../3/c7/screenshots/c7-toolcard*.png`

### c4 零宽字符注入不进 system 提示词 + 单测锁死「检测文本 == 清理后文本」→ **PASS**

本轮**在 8190 线上代码上重跑了相关单测**：

```
15 passed, 94 deselected in 0.51s
```

线上 `h3studio/agent_safety.py` 的隐形字符集按运行时 Unicode 类别取全集，覆盖 `U+200B-U+200F`、`U+2060-U+2064`、`U+FEFF`；`agent_memory.py` 与 `agent_safety.py` 共用**同一份真源**（`strip_hidden` / `hidden_chars`）。单测 `test_detection_text_equals_sanitized_text_for_every_variant` 逐码位锁死「检测用文本 == 清理后文本」。

- 证据：`c4/c4-live-unit-tests.txt`
- 前轮 51 条全绿输出：`.../3/c4/c4-unit-tests.txt`、`.../3/c4/acceptance_c4_evidence.json`

### c5 跨前后端契约测试（覆盖 5 项且打真实后端）→ **PASS**

本轮**在 8190 线上代码上重跑**：

```
27 passed in 52.49s
```

`tests/test_agent_contract.py` 的 6 个契约类覆盖要求的全部 5 项：

| 要求的契约项 | 对应测试类 |
|---|---|
| stream 载体 | `TestStreamCarrierContract` |
| tool_result 字段 | `TestToolResultFieldContract` + `TestOneShotToolResultContract` |
| 澄清字段 | `TestClarificationFieldContract` |
| steps[].args 工具参数 | `TestStepsArgsContract` |
| 音乐 aspect | `TestMusicAspectContract` |

测试基类 `_ContractBase._make_app()` **真起 `server.make_app()`**（`server.py:8645 def make_app()`），端到端那条用**前端真实请求形态**打真实后端，不是前端自造 mock 自证。

- 证据：`c5/c5-live-contract-tests.txt`
- 前轮：`.../3/c5/acceptance_c5_evidence.json`、`.../3/c5/c5-contract-tests.txt`、3 组 mutation 全部被咬住（`c5-mutation-*.txt`）

### c6 全量 pytest 与未改动基线逐条 diff（零新增回归）→ **PASS**

采用「**同一测试集合**」口径（25 个共同 `test_*.py`，清单见 `common_test_files.txt`）：

| | 基线（未改动 worktree @17c9adb） | 线上 live |
|---|---|---|
| 结果 | 27 failed / 517 passed / 134 errors（397.59s） | 5 failed / 694 passed / 4 skipped（1282.54s） |
| 退出哨兵 | 有 | 有 |
| **新增失败（仅 live）** | — | **0 条** |
| 消失失败（仅 baseline） | 156 条 | — |
| 共有失败 | 5 条（`tests/test_page_size.py` ×4 + `tests/test_studio.py::TestLoRARegistry::test_scanner_only_returns_z_image_loras`） | 同 |

另有前轮**全量口径**的三次运行原始输出与 md5 对照（被测代码文件 / 测试清单 / 失败名单三类产物的 md5 齐全）。

**归因说明（不回避）**：前轮全量口径曾出现 18 条「新增失败」，逐条归因后确认这 18 条所属测试文件在未改动基线与本次修复分支**均不存在**（18/18），属并发任务新增测试，且失败均在其它功能域（effect-lora 前端浏览器断言 / t2v 接续 / 分页），Agent 增强域失败数 **0**，域内用例单独复跑 **156 passed**。

- 证据：`/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/.studio-acceptance/053d6b9b-decb-4839-87fa-a7898f330692/1/c6/run_c6.log`、`c6-diff.txt`、`c6-diff-verdict.json`、`c6-baseline-failures.txt`、`c6-live-failures.txt`、`baseline_common_set.txt`、`live_common_set.txt`、`agent_domain_tests.txt`、`c6-md5.txt`、`c6-md5-code-and-tests.json`、`common_test_files.txt`
- 前轮全量：`/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/.studio-acceptance/776edeea-e1d7-4189-ab6a-d982beb941dd/3/c6/acc-baseline_clean_full.txt`、`acc-live_full.txt`、`acc-live_clean_full.txt`、`c6-md5.txt`、`c6-final-verdict.json`、`c6-closeout-verdict.json`

### c7 三档宽度真实浏览器截图 + console 报错数 → **PASS**

本轮**在 8190 线上重跑三档宽度**（375 / 390 / 1280），用 CDP 注入页面级收集器（`console.error` / `window.onerror` / `unhandledrejection`）：

| 宽度 | console_errors | page_errors | 横向溢出 |
|---|---|---|---|
| 375 | 0 | 0 | false |
| 390 | 0 | 0 | false |
| 1280 | 0 | 0 | false |

截图：`c7/screenshots/c7-live8190-{375,390,1280}.png`（375 档截图里可直接看到 MOSS 批次卡的 `步数/CFG 100 · 4.3` 与 `批次结果（5/5 完成）`）。

前轮另有 21 张含工具卡 / 澄清卡 / 流式增量（mid12/mid30/mid55/final）的三档截图。

- 证据：`c7/c7-live8190-console-errors.json`、`c7/c7-live8190-viewports.json`、`c7/screenshots/`
- 前轮：`.../3/c7/acceptance_c7_evidence.json`、`.../3/c7/screenshots/`

### c8 中文修复汇报写入工作区与仓库 docs/ → **PASS**

- 工作区：`/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/内置多模态Agent-增强汇报-20261115.md`（95,196 B）
- 线上 `docs/`：`/root/projects/h3studio-live/docs/内置多模态Agent-增强汇报-20261115.md`（95,196 B）
- 两处 md5 一致：`d9a7238a196bead5209b823316de7445`
- 主仓 `docs/` 另有前轮的三份独立复验 / 收口 / 验收报告（`独立复验报告-c1至c8与ABCD部署确认.md`、`独立复验收口报告-c1至c8与线上ABCD.md`、`独立验收报告-c1至c8与线上ABCD.md`）
- **本轮复验报告**：本文件 + `/root/projects/h3studio-live/docs/独立复验报告-deeff606-c1至c8与线上ABCD.md`

---

## 2. 线上 A / B / C / D 四项复核

### A 遗留问题修复（token 级增量流式 / SSE AbortController / 轮询终止 / recall(limit<0) / steps[] schema 统一 / 交付脚本资源断言 / /api/image-loras 性能 / LoRA 同名 key 冲突 / Agent 分支手工重写合并）→ **PASS**

线上代码逐项核对：

- **token 级增量流式**：`server.py:7870` `web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream; charset=utf-8", ...})`，本轮线上实测 41 个 `event: token` 增量事件。
- **SSE AbortController**：`web/agent.js:1576-1580`（注释明写「之前**没有任何取消/超时**」，现已加 `new AbortController()`）。
- **recall(limit<0) 语义**：`h3studio/agent_memory.py:392-405` 文档串明确 `limit>0 → 最多 N 条`、`limit==0 → 全部`、`limit<0 → 全部`，并统一走 `_normalize_read_limit()`；注释同时写明旧实现三处语义不一致的历史。
- **steps[] schema 统一**：`h3studio/agent.py:483-515` 定义 `STEP_FIELDS`（step/tool/ok/ms/args/summary/call_id/note/error），所有条目强制走 `_step_entry()` 构造并做**字段集合自检**。
- **/api/image-loras 性能**：本轮实测 8190 该接口 3 个 base 查询均在 2 秒内返回（`available` 139/195/195）。
- **LoRA 同名 key 冲突 / 交付脚本资源断言 / Agent 分支合并**：对应提交均在 live HEAD 祖先链上（`683e6fb` / `127603a` / `9249656` / `36d510b`）。

证据：`/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/.studio-acceptance/053d6b9b-decb-4839-87fa-a7898f330692/1/abcd/`、`.../776edeea-.../3/abcd/`

### B MOSS-Image LoRA 键不匹配静默失效 → **PASS（本轮重点复验）**

**B-4 像素级成对出图对比**（同 prompt / steps / cfg / seed / 分辨率 / scheduler，唯一变量是 LoRA 的 `enabled`）：

| | with_lora | without_lora |
|---|---|---|
| job id | `639e38f1d9c3` | `c986c1af93bf` |
| **loras[0].enabled** | **true** | **false** |
| ComfyUI 日志 | `Model MOSSImage prepared ... **263 patches attached**. Force pre-loaded 177 weights: 559 KB.` | `Model MOSSImage prepared ... **0 patches attached**. ...` |
| `lora key not loaded` 警告 | **0 条** | **0 条** |

**像素级差异（本轮独立复算，数值与前轮完全一致）**：

```
尺寸 (1024, 768, 3) × 2        像素总数 786,432
有差异像素        786,318  (99.99%)
差异 > 8 的像素   707,805  (90.00%)
平均绝对差        31.7775        最大单通道差 217
分区（上/中/下 1/3）均 100% 像素变化，平均差 37.921 / 29.636 / 27.787
```

→ 证明 D1 LoRA 在 MOSS-Image 上是**全画面级生效**，不是「只挂一半层」。

**D1 真实挂载（ComfyUI 真实代码路径 `comfy/lora.py::model_lora_keys_unet` + `load_lora`）**：

| 文件 | patched | missing |
|---|---|---|
| 原文件（融合 qkv） | 137 | 126 |
| **归一派生文件** `h3studio_mossnorm__moss_image_sft_look_lora.safetensors` | **263** | **0** |
| D2 完整文件 | **252** | **0** |

**D2 文件本体**：磁盘 297,355,184 B（= 头部声明，缺 0 字节），md5 `6070e3ca033c419c1e74568789b680e7`，`safetensors.safe_open()` 真实打开 756 张量。

**「其它 MOSS 条目是否同类静默失效」核查（goal B.4）**：

- 线上 `/api/image-loras?base=MOSS-Image`：**available=139，compatible=false 计数 0，139/139 全部 attention_hit=1.0**（转换状态：direct 91 / derived 48）。
- 磁盘上 MOSS 族原文件 138 个 + 派生 48 个 = 与接口 139 条对得上（去重后）。
- 4 条**不**下发到 available 的（`MOSSRealisticSnapshotV1` / `lokr-moss-kodachrome-v1` / `lokr-moss-kodachrome-v2` / `skin_tone_moss_NW`）：本轮独立跑 `conversion_status` + `check_moss_compatibility`，结论一致 —— 融合 qkv 形态且**派生文件不存在**（`unverified`），判定 `compatible=False`，并按需求 15 的口径「接口层不下发、已保存配置一并剔除」。**没有一条以 compatible=true 误导用户。**
- **判定不是常量 true**（本轮专门用三种真实输入证明）：同一套代码对派生归一版判 `direct/true`、对融合原文件判 `derived/true`（走自动归一）、对**人为截断的文件**判 `broken/false` 并给出「实际 5,369,856 字节，头部声明需要 2,221,764,612 字节，缺 2,216,394,756 字节…已从可选列表中排除」的如实原因。

证据：`d1d2-attach-verification.json`、`d1d2-failclosed-verification.{json,txt}`、`verify_d1d2_attach.py`、`verify_failclosed.py`、`b-image-loras-moss-live-20261115.json`、`b/`、`d2/`

### C MOSS 批量出图 steps 被写死为 5 → **PASS（本轮线上重跑）**

**根因定位**：批量分支从 `params` 取 `mode`（params 里没有 `mode` 键）→ 落到默认/降级路径。修复为从 `jobs` 表的 `mode` 列传入：

- `server.py:1595` `mode = job_row["mode"]`（单张分支）
- `server.py:2063` `mode = job_row["mode"]`（批量 `_run_batches` 分支）
- `server.py:2121` `async def _run_one_batch(self, job_id, batch_id, mode: str, params, seed)` —— 文档串明确写「mode 由调用方从 jobs 表的 mode 列传入（不能从 params 里取 —— params 里没有 mode 键）」
- `h3studio/workflows.py:2420` MOSS 主采样节点 `"moss:16": {"inputs": {"seed": int(seed), "steps": steps, "cfg": cfg, ...}}`，`steps = int(params.get("steps") or model["steps"])`（`workflows.py:2376`）

**本轮线上真实批次（job `c745dad8a8b0`）**：

- 提交参数：`mode=t2i_moss, batch_count=5, steps=100, cfg=4.3, shift=5, scheduler=sgm_uniform, resolution=1mp, aspect=16:9`
- 运行区间：16:03:28 → 16:09:58，`status=running → done`，`progress 0.0 → 1.0`，**outputs=5**
- **ComfyUI 真实 tqdm 进度条（`/home/ubuntu/comfyui/user/comfyui.log`）**：

```
2026-11-15 16:04:53  100%|██████████| 100/100 [01:09<00:00,  1.42it/s]
2026-11-15 16:06:12  100%|██████████| 100/100 [01:12<00:00,  1.42it/s]
2026-11-15 16:07:24  100%|██████████| 100/100 [01:10<00:00,  1.42it/s]
2026-11-15 16:08:43  100%|██████████| 100/100 [01:16<00:00,  1.42it/s]
2026-11-15 16:09:55  100%|██████████| 100/100 [01:10<00:00,  1.42it/s]
```

**窗口内 `/100` 进度条 10 条，`| 5/5 [` 缺陷特征 0 条。**

- **ComfyUI history 侧参数（唯一真相源）**：5 条 history 条目，全部 `node=moss:16 / class_type=KSampler`，**steps 全部=100、cfg 全部=4.3、scheduler 全部=sgm_uniform、seed 5 个各不相同**（248534425965798 / 35356716828402 / 193909060828995 / 148110671124091 / 24510653490642）。
- **批次面板截图**（真实浏览器，线上 8190）：DOM 断言 `步数/CFG 100 · 4.3`、`▶ 批次结果（5/5 完成 · 1360×768）`、5 个 seed 逐条可见。
- **5 张产出图片**（1360×768，1MP/16:9 符合设定）：`c/images/moss_c745dad8a8b0_{1a5fc913e87d,38a45b2cac84,a6211a2fe37e,eb888dd3dd7e,4ce5ef948b99}_00001_.png`

证据：`c/c-batch5-steps-progressbars.txt`、`c/c-batch5-verdict.json`、`c/c-batch5-comfyui-journal-raw.txt`、`c/c-batch-panel-dom-assertion.json`、`c/c-batch-panel-8190.png`、`c/images/`、`c_batch5_probe.py`、`mk_c_progressbar_report.py`

### D YuE2「★ 生成风格+歌词」按钮失效 → **PASS（本轮线上真实点击）**

本轮在**线上 8190** 用真实浏览器实走一遍：

1. 切到 `[data-mode="t2a"]`（文生音乐）面板
2. 打开增强菜单 → `YuE2 结构化风格 + 歌词` 项在该面板**可用**（`class="enhance-menu-item checked"`，非 `unavailable`）
3. 按钮文案实测为 **`生成风格+歌词`**（`#musicEnhanceBtnText`）
4. 填入音乐描述后**真实点击一次** → hint 从「正在生成结构化风格与歌词…」变为 **`已生成结构化风格与歌词（本机 Qwen 多模态），自动纠正 1 处`**（`class="enhance-hint ok"`）
5. **界面确实出现「风格结构化 + 歌词」**：
   - 结构化风格字段 **12 个**（`musicSF_language/genre/mood/vocal/vocal_color/instruments/tempo_bpm/energy/length_hint/extra/custom_language/custom_genre`），其中 **10 个有值**：`Mandarin` / `piano pop` / `nostalgic` / `female vocal` / `soft intimate` / `acoustic piano, gentle fingerpicked acoustic guitar, soft string pad, no drums, no synth, no electric guitar` / `78` / `low energy` / `full song (~3.5-5 min)` / 补充描述
   - 歌词框 `#musicLyrics` **361 字符**，含 `[Intro]` `[Verse 1]` `[Pre-Chorus]` `[Chorus]` `[Verse 2]` `[Bridge]` `[Final Chorus]` `[Outro]` 分段标记
6. **console / pageErrors = 0**
7. 截图：`d/d-yue2-button-and-lyrics-live8190.png` —— 视觉复核确认「生成风格+歌词」按钮、结构化风格字段区、带分段标记的歌词框**同屏可见**

**「重新生成 / 收藏 / 参考生音乐同款入口」同类失效核查**：代码层 `web/music-enhance.js:125-139` 注释写明「重新生成的唯一入口是『生成风格+歌词』按钮」，第 290 行写明这两个重复入口已按用户要求去掉——即**不再存在第二条会失效的重复入口**；前轮真实点击三个入口均实测发请求并回填（`.../053d6b9b-.../1/abcd/d/v2/acceptance_d_evidence.json`，`verdict.all_widths_secondary_entries_clicked: true`）。

> 说明：本轮第一次点击时界面正确提示「请先输入音乐描述」—— 这是**空输入的前置校验**，不是按钮失效；填入描述后同一按钮正常产出结构化风格 + 歌词。

证据：`d/d-yue2-lyrics-live8190.png`、`d/d-yue2-button-and-lyrics-live8190.png`、`d/d-yue2-dom-assertion.json`

---

## 3. 8190 线上探活 → **PASS**

```
=== LIVE 8190 探活 @ 2026-11-15T16:18:45+08:00 ===
首页            200  /
API (18 条)     全部 200
静态资源 (30 条) 全部 200  （真实路由 /static/*）
200 计数：49    非 200：0
systemd: ActiveState=active SubState=running
```

> **路由更正说明**：首轮探活把静态资源写成 `/app.js`、`/style.css` 等裸路径，全部 404 —— 那是**探活脚本猜错了路由**（真实路由是 `server.py:8732 app.router.add_static("/static", BASE / "web")`），不是产品故障。按真实路由重测后 30/30 全 200。

- API 清单：`/api/system` `/api/jobs` `/api/queue` `/api/hardware` `/api/image-options` `/api/music-options` `/api/video-options` `/api/image-loras?base=MOSS-Image` `/api/image-loras?base=Z-Image` `/api/image-loras?base=Qwen-Image-2.1` `/api/generation-types` `/api/comfy-models` `/api/providers` `/api/enhancer` `/api/prompt-history` `/api/video-effect-loras` `/api/media/manage` `/api/video-categories`
- **增强注册表**：`/api/enhancer` → `specs` 12 项、`mode_filters` 13 项、`mode_defaults` 13 项、`mode_media` 13 项、`model_families` 18 项、`max_tokens_bounds` 3 项、`refine_spec_by_media` 3 项

证据：`live-8190-liveness.txt`、`live_8190_liveness.sh`、`live-8190-static-probe.txt`、`b-image-loras-{MOSS-Image,Z-Image,Qwen-Image-2.1}.json`

---

## 4. 未取证 / 无法判定项

**无。** 本轮 c1、c2、c3、c4、c5、c6、c7、c8 全部有可复核证据；其中 c1、c4、c5、c7、D **在本轮于线上 8190 重新实测**（不只引用前轮），c2、c3、c6 引用前轮独立验收已落盘证据并逐条核对可打开性（100/100 个前轮证据文件全部可打开、非空）。

---

## 5. 遗留项与未完成原因

| 编号 | 遗留项 | 原因 / 归属 |
|---|---|---|
| L-1 | 前两次验收复验因「Acceptance time budget exhausted」被中断，判定记录未落库 | 宿主验收时间预算问题，非产品缺陷；本轮已产出完整逐条判定 |
| L-2 | 未做「D1 修复前（137 层）vs 修复后（263 层）」的像素级对照 | 修复前代码已不在线上，且本轮不部署/不重启；现有对照是「修复后 启用态 vs 禁用态」的成对对照 |
| L-3 | 未做 D2 的完整出图（仅验证文件本体 + 接口列出 + 真实挂载 patched=252/missing=0） | B-4 成对出图用的是 D1；D2 出图需占用 ComfyUI 队列，本轮时间盒优先 |
| L-4 | 未做多 seed 重复的像素对照 | 时间盒；单 seed 对照平均绝对差 31.78、90% 像素差异 > 8，远超随机噪声 |
| L-5 | 通用族 3 个损坏 LoRA 文件本体未补齐 | **不在本次范围**（MOSS 族已闭环）；修复路径与 D2 相同 |
| L-6 | `cleanup_stale_derived()` 无调用点、`base_verdict` 目录字段未补 | **范围外**，维持原状 |

---

## 6. 本轮证据绝对路径清单

根目录 `EV` = `/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/.studio-acceptance/deeff606-34e0-419d-a118-90eded56520e/1/`

```
EV/
├── 独立复验报告-deeff606-c1至c8与线上ABCD.md   ← 本报告
├── live-8190-liveness.txt                      8190 探活（49 条全 200）
├── live_8190_liveness.sh                       探活脚本
├── live-8190-static-probe.txt                  静态资源 /static/* 逐条
├── d1d2-attach-verification.json               D1/D2 真实挂载（patched/missing）
├── verify_d1d2_attach.py                       挂载验证脚本（ComfyUI 真实代码路径）
├── d1d2-failclosed-verification.json/.txt      三种输入三种结论（证明非常量 true）
├── verify_failclosed.py                        fail-closed 验证脚本
├── failclosed_probe/truncated_test.safetensors  人为截断样本（5,369,856 B）
├── b-image-loras-moss-live-20261115.json       8190 MOSS 清单原始响应（139 条）
├── b-image-loras-moss-incl-incompatible.json   同一接口另一入参的响应
├── b-image-loras-Z-Image.json                  Z-Image 回归（195 条 / 0 incompatible）
├── b-image-loras-Qwen-Image-2.1.json           Qwen-Image-2.1 回归（195 条 / 0 incompatible）
├── c1/c1-live8190-sse-capture.txt              SSE 响应头 + 41 个 token 事件
├── c4/c4-live-unit-tests.txt                   线上重跑 15 passed
├── c5/c5-live-contract-tests.txt               线上重跑 27 passed
├── c7/c7-live8190-console-errors.json          三档 console/pageErrors 计数
├── c7/c7-live8190-viewports.json               三档布局断言
├── c7/screenshots/c7-live8190-{375,390,1280}.png
├── c/c-batch5-steps-progressbars.txt           ★ 5 张 100/100 进度条 + 5/5 计数 0
├── c/c-batch5-verdict.json                     ★ 5 条 history：steps=100/cfg=4.3/seed 各异
├── c/c-batch5-comfyui-journal-raw.txt          本批次 ComfyUI 原始日志
├── c/c-batch5-live-verification.json           批次全量 JSON
├── c/c-batch-panel-dom-assertion.json         ★ 批次面板 DOM 断言（步数/CFG 100 · 4.3）
├── c/c-batch-panel-8190.png                    ★ 批次面板截图
├── c/images/moss_c745dad8a8b0_*.png            ★ 5 张产出（1360×768）
├── c_batch5_probe.py / mk_c_progressbar_report.py
├── d/d-yue2-lyrics-live8190.png                ★ D 截图（歌词 + 结构化风格）
├── d/d-yue2-button-and-lyrics-live8190.png     ★ D 截图（按钮 + 风格 + 歌词同屏）
├── d/d-yue2-dom-assertion.json                 ★ D DOM 断言
├── b/B-4-像素级成对出图对比.md                  B-4 报告
├── b/b4-with-lora.png / b4-without-lora.png     B-4 成对出图
├── b/b4-with_lora-comfyui-log.txt              263 patches attached + 0 警告
├── b/b4-without_lora-comfyui-log.txt           0 patches attached
├── b/b4-paired-render.json                     B-4 机器可读汇总
├── d2/D2-文件本体重新获取与恢复证据.md           D2 报告
├── d2/d2-after-install-api.json                D2 放回后接口响应
└── d2/d2-ORIGINAL-truncated-backup.safetensors  原截断文件备份（5,369,856 B）
```

引用（前轮独立验收已落盘，本轮已逐条核对可打开、非空）：
`/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/.studio-acceptance/776edeea-e1d7-4189-ab6a-d982beb941dd/3/`（c1c2c3 / c4 / c5 / c6 / c7 / c8 / abcd）
`/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/.studio-acceptance/053d6b9b-decb-4839-87fa-a7898f330692/1/`（abcd / c6）

---

## 7. 纪律声明

- 本轮**未修改任何产品代码**：只读核对 + 只读取证 + 写盘报告；未合并任何分支、未部署 8190、未重启 `h3-studio.service`（16:13:53 的重启来自并发任务）。
- 主仓 `/root/projects/comfyui-minimax-h3-studio`（`local/feature/mobile-responsive-fix`）的 **他人未提交 WIP 37 项未碰**（含 `h3studio/loras.py`、`server.py`、`web/*`）。
- 线上 `docs/内置多模态Agent-增强汇报-20261115.md` 在 live worktree 处于 modified 状态（上一轮开发轮写入、本轮未再改动其内容），未 `git add` / 未提交 / 未还原。
- 未代验收 Agent 臆造结论，未执行任何 `verify` / `record_acceptance` 类动作。
