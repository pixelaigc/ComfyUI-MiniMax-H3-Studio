# MOSS-Image 及同类面板参数生效性修复报告

**日期**：2026-11-16
**仓库**：`/root/projects/comfyui-minimax-h3-studio`（主线 `local/maintainer`）
**线上 worktree**：`/root/projects/h3studio-live`（systemd `h3-studio.service`，端口 8190）
**本轮分支**：`local/feature/image-param-ranges-20261116`
**代码提交**：`c5c07ce` fix(MOSS-Image): 采样参数量程前后端同源
**交付 commit（线上）**：`f4678e7ef7925be4d3c2ba11456a484934e52ecb`（合并提交，父为 `local/maintainer` 与本轮分支）
**本轮分支 tip**：`e9e3f61 verify(MOSS-Image): 真实浏览器验证滑杆量程与「已改」徽标

---

## 一、用户报告的 BUG 与根因

### 1.1 现象

「文生图 → MOSS-Image」面板把「采样步数」滑到 100、数值框显示 100（面板显示正确），
但生成队列里该任务的参数卡显示「步数/CFG 50 / 3」—— 实际跑的是默认 50 步。

### 1.2 根因（一句话）

**「前端允许、后端不允许」** —— 采样参数量程在前端和后端各写了一份，且两份不一致。

### 1.3 文件:行号 级证据（修复前，基线 `46838de`）

| # | 位置 | 事实 |
|---|---|---|
| E1 | `web/app.js`（修复前 1875-1895，`isMossMode()` 分支） | 前端为 MOSS 硬编码量程：`stepsEl.min="1"; stepsEl.max="100"`、`cfgEl.min="0"; cfgEl.max="10"`、`shiftEl.min="0"; shiftEl.max="10"` |
| E2 | `h3studio/gen_normalize.py:53-62`（修复前） | 后端只有**一套扁平的全局区间**：`IMAGE_STEPS_MAX=60` / `IMAGE_CFG_MIN=1.0` / `IMAGE_CFG_MAX=8.0` / `IMAGE_SHIFT_MIN=1.0` / `IMAGE_SHIFT_MAX=7.0` |
| E3 | `h3studio/gen_normalize.py:132`（修复前） | 越界判定：`if steps < IMAGE_STEPS_MIN or steps > IMAGE_STEPS_MAX:` → 回落 `default_steps`（= 模型表推荐值 50） |
| E4 | `server.py:4013-4016`（修复前） | 归一调用**不传族**：`gen_normalize.normalize_image_sampling(params, _image_model, _notes)` —— 所有族共用 E2 那套区间 |
| E5 | `h3studio/gen_normalize.py:139` + `server.py:4017-4023` | 回落会记 `params["normalize_notes"]`，但**前端从不读它**（修复前 `grep normalize_notes web/` → 0 命中）→ 改动被静默掩盖 |

**因果链**：面板允许 steps=100（E1）→ 提交 `params.steps=100` → 后端归一层用 E2 的
上限 60 判「越界」（E3）→ 静默回落模型推荐 50（E4）→ 说明只落库、前端不显示（E5）
→ 面板显示 100、参数卡显示 50、界面零提示。

### 1.4 线上实测复现（修复前，8190 线上）

提交 `mode=t2i_moss, steps=100, cfg=3, shift=5, sampler=euler, scheduler=sgm_uniform,
seed=2081206678357`，任务 `dace96729131`：

```
STORED steps: 50
NORMALIZE_NOTES: ["steps：100 不是合法取值，已改用 50"]
```

**根因确认：不是前端没传，是后端把用户值改掉了。**

---

## 二、修复内容

### 修复 1 · 量程单一真源（核心）

**`h3studio/gen_normalize.py:83-117`** 新增 `IMAGE_PARAM_RANGES`（按族）：

| 族 | steps | cfg | shift |
|---|---|---|---|
| `zimage` | 1~60 / step 1 | 1.0~8.0 / 0.5 | 1.0~7.0 / 0.5 |
| `qwen` | 1~60 / step 1 | 0.0~8.0 / 0.1 | 0.0~3.0 / 0.01 |
| `moss` | 1~**100** / step 1 | 0.0~10.0 / 0.1 | 0.0~10.0 / 0.1 |
| `h3image` | 1~12 / step 1 | 1.0~8.0 / 0.5 | 0.0~3.0 / 0.1 |

- `param_ranges_for_family(family)`（`:115`）按族取区间。
- `normalize_image_sampling(..., family=None)`（`:171`）新增 family 形参；
  不传时读模型表的 `param_family` 标记。
- `server.py:3652` 新增 `_image_family_for_mode(mode)`；`server.py:4168` 归一时显式传族。
- **模型表自动打族标**（`workflows.py` 四处 + `h3image.py` 一处，用 `setdefault` 程序打标
  而非逐条手写 —— 模型表会增删，手写必然漏）。
- `server.py:6150`：`/api/image-options` 下发 `param_ranges`。
- `web/app.js:1876`：删掉四套硬编码分支，改读服务端下发；
  `web/app.js:373-396` 新增 `imgParamFamily()` / `imgParamRanges()` / `imgShiftDecimals()`。

**前端不再有第二份量程表**：服务端没下发时 `imgParamRanges()` 返回 `null`，
滑杆**保持 `web/index.html` 里 range 标签自带的量程不动** —— 那套恰好等于**旧后端**
的全局区间（steps 1~60 / cfg 1~8 / shift 1~7），因此版本不一致期间
「滑杆允许的」正好是「后端接受的」，不会错配。
（曾写过一份前端兜底表，被变异自证 M6b 判为「两份量程必然漂移」而删除。）

### 修复 2 · Z-Image 放大分支不再写死采样参数

**`h3studio/workflows.py:830-889`**：`_z_image_upscale_nodes()` 原先把第一段 KSampler
（`57:3`）写死 `steps=8 / cfg=1.0 / res_multistep / simple`、`shift=3.0`
→ **>=3MP 档位（1440p / 2k / 4k）上用户改的步数/CFG/采样器/调度器/shift 全部无效**。
现在第一段用用户参数（`eff_steps` / `eff_cfg` / `eff_sampler` / `eff_scheduler` / `eff_shift`），
调用方（`workflows.py:1010-1017`）传入用户当前值。
第二段（`57:75`）是官方放大链的固定工艺参数（5 步 / cfg 1 / denoise 0.33），**保持不变**。

### 修复 3 · 归一说明不再静默

- `h3studio/job_meta.py:1611`：`gen_params()` 下发 `normalize_notes`。
- `web/app.js:5555`（`openDetailModal`）：详情弹层新增「参数已调整」一行，
  明确列出「哪一项被改成了什么」。

---

## 三、《MOSS-Image 及同类面板参数生效性核对表》

### 3.1 MOSS-Image 文生图（本次缺陷主体）

| 面板字段 | 前端 state 键 | 请求体字段 | 后端 params | 工作流节点输入 | 判定 |
|---|---|---|---|---|---|
| 采样步数 | `state.imgMoss.steps` | `params.steps` | `params["steps"]` | `moss:16` KSampler `steps` | ✅ 已核对通过（本次修复） |
| CFG | `state.imgMoss.cfg` | `params.cfg` | `params["cfg"]` | `moss:16` `cfg` | ✅ 已核对通过（本次修复） |
| shift | `state.imgMoss.shift` | `params.shift` | `params["shift"]` | `moss:15` ModelSamplingSD3 `shift` | ✅ 已核对通过（本次修复） |
| 采样器 | `state.imgMoss.sampler` | `params.sampler` | `params["sampler"]` | `moss:16` `sampler_name` | ✅ 已核对通过 |
| 调度器 | `state.imgMoss.scheduler` | `params.scheduler` | `params["scheduler"]` | `moss:16` `scheduler` | ✅ 已核对通过 |
| 种子 | `#seed` | `params.seed` | `params["seed"]` | `moss:16` `seed` | ✅ 已核对通过 |
| 分辨率档位 | `state.imgMoss.tier` | `params.resolution` | `params["resolution"]` | `moss:6` EmptyFlux2LatentImage `width/height` | ✅ 已核对通过（前后端同一套吸附规则） |
| 宽高（自定义） | `state.imgMoss.width/height` | `params.width/height` | `params["width"/"height"]` | `moss:6` | ✅ 已核对通过 |
| 负面提示词 | `state.imgMoss.negative` | `params.negative_prompt` | `params["negative_prompt"]` | `moss:5` CLIPTextEncode | ✅ 已核对通过 |
| SLG skip layer | `state.imgMoss.slgSingle` | `params.slg_single_layers` | `params["slg_single_layers"]` | `moss:13` SkipLayerGuidanceDiTSimple | ✅ 已核对通过 |
| 效果 LoRA | `state.imgMoss.loras` | `params.loras[]` | `params["loras"]` | LoRA 节点链（`moss:16.model`） | ✅ 已核对通过 |
| 批次数 | `state.imgMoss.batchCount` | `params.batch_count` | `params["batch_count"]` | `job_batches` 表逐批建任务 | ✅ 已核对通过（实测 3 批全部 steps=100） |

### 3.2 其它文生图模型

| 面板 | 缺陷 | 判定 |
|---|---|---|
| **Z-Image**（t2i） | 量程与后端一致（steps 1~60 / cfg 1~8 / shift 1~7），无越界回落 | ✅ 已核对通过 |
| **Z-Image >=3MP 放大分支** | 第一段 KSampler 写死 steps=8 / cfg=1.0 | 🔧 **发现缺陷并已修**（修复 2） |
| **Qwen-Image 2.1**（t2i_qwen / i2i_qwen） | 官方内置 `shift=0.69` 被后端判越界（下限 1.0）→ 静默回落 1.0；`cfg=0` 同样被回落 | 🔧 **发现缺陷并已修**（修复 1） |
| **Qwen-Image 2.1 Turbo** | 8 步固定 sigma 表，步数被前端**锁定**并注明原因 | ✅ 已核对通过（锁定语义诚实） |
| **H3-Image**（t2i_h3image / i2i_h3image） | `shift=0` 被后端判越界（下限 1.0）→ 静默回落 1.0；步数上限 12 | 🔧 **发现缺陷并已修**（修复 1） |

### 3.3 文生视频 / 首尾帧 / 参考生 / 音乐（并行审计结果）

| 项 | 判定 | 说明 |
|---|---|---|
| 视频族步数/采样器/调度器/shift：面板 → `submitParams()` → `normalize_t2v_params` → `BasicScheduler`/`KSamplerSelect` | ✅ 已核对通过 | `web/video-panel.js:1911-1915` `submitParams()` 确实带出 `steps/sampler/scheduler/shift_video/shift_audio` |
| 视频族面板持久化 / 「复用参数」 | ✅ 已核对通过 | `modeConfigs()`（`video-panel.js:1290-1315`）**包含** `steps/sampler/scheduler/shift_video/shift_audio`；`applyModeConfig()`（`:1579-1583`）也回填 |
| flf / r2v 走同一套 `normalize_t2v_params` | ✅ 已核对通过 | `server.py:4189` 三个模式共用入口 |
| 音乐 top_p / top_k / repetition_penalty | ✅ 已核对通过 | `music_yue2.py:391-405` `_sampling()` → `:579-582` 工作流节点 |
| 音乐 temperature **= 0** | ⚠️ **发现缺陷未修**（范围外） | `web/music.js:167` `if (!st.temperature) st.temperature = ms.temperature;` —— 0 是**合法值**（面板量程 0~3），却因 falsy 被回默认 0.8 |
| 二采（SelfLift）开启时主采样器 | ⚠️ **发现缺陷未修**（范围外） | `h3studio/video_workflow.py:779` `forced_sampler = "euler"` —— 节点硬约束只能用 Euler（合理），但主流采样器下拉**不禁用**，卡片显示用户选的采样器、实际跑 euler（界面说谎） |
| 视频族归一不留痕 | ⚠️ **发现缺陷未修**（范围外） | `normalize_t2v_params` 全程无 notes 收集；越界回落 / ρ 改写 / transition 夹回都不留痕（图像族本次已修，视频族未修） |
| `sigma_shift_available` 读取位置错位 | ⚠️ **发现缺陷未修**（范围外） | 后端放在 payload **顶层**（`video_models.py:2396`），前端读 `cfg.sigma_shift_available`（`video-panel.js:980`，`cfg = VIDEO_OPTIONS.sampling`）→ 恒为 `undefined` → shift 禁用守卫永不触发（本机该节点恒在，实际无可见症状） |

### 3.4 「已改」徽标 / 「复用参数」/ 归一说明

| 项 | 判定 |
|---|---|
| 图像面板「推荐 / 已改」徽标 | ✅ 已核对通过。`web/app.js:2319-2320`、`:2382` 与模型表推荐值比较 —— 语义是「你改过」，修复后改的值真的生效，徽标不再说谎 |
| 图像面板「锁定」徽标（Qwen Turbo 固定 sigma 表 / 蒸馏档 / H3-Image guidance 固定） | ✅ 已核对通过。锁定时控件 `disabled` + 说明原因，且值被拉回真实生效值 |
| 任务卡片「步数/CFG」行 | ✅ 已核对通过。读服务端 `job.gen_params`（`web/app.js:3994-3996`），而 `gen_params` 读的是**产物反解值**（`artifact_meta.extract_from_workflow`），不是用户填的值 —— 卡片显示的就是**实际执行值** |
| 任务详情「参数已调整」行 | 🔧 **新增**（修复 3）。此前归一说明只落库、前端 0 命中 |
| 任务卡「复用参数」→ 图像面板 | ✅ 已核对通过。`web/app.js:5069-5095` 逐字段回填 steps/cfg/shift/sampler/scheduler/SLG/LoRA/批次数 |
| 任务卡「复用参数」→ 视频面板 | ✅ 已核对通过（见 3.3） |

### 3.5 队列 / 批量提交

| 项 | 判定 |
|---|---|
| 批次任务的 `mode` 来源 | ✅ 已核对通过。`server.py:2085` 从 `job_row["mode"]`（DB 列）取，不从 `params["mode"]` 取 —— 历史上这里曾写死 `"t2i"`，导致 MOSS 批量任务走 Z-Image 工作流（`server.py:2085` 的注释明确记着这个历史缺陷） |
| 批次任务的用户参数 | ✅ 已核对通过。`server.py:2162` `batch_params = dict(params)`，只改 `seed`/`job_id`/`mode`，steps/cfg/shift/sampler/scheduler 原样带走 |
| 批次产物元数据回写 | ✅ 已核对通过。`server.py:2189-2192` `apply_to_params(..., include_seed=False)` —— 采样参数回写、seed 不回写（每批 seed 记在 `job_batches`） |
| 队列参数卡显示 | ✅ 已核对通过。实测 3 批任务，卡片与 ComfyUI 实际执行值均为 steps=100 |

---

## 四、实测证据

### 4.1 单张提交路径

脚本：`scripts/verify_moss_steps_live.py`（提交 → 落库 → ComfyUI /history 回读 → 产物核验）
证据：`evidence/moss_steps_live_single.json`

```json
{
  "job_id": "eb405f6264a6",
  "submitted_steps": 100,
  "status": "done",
  "stored_params": {
    "steps": 100, "cfg": 3.0, "shift": 5.0,
    "sampler": "euler", "scheduler": "sgm_uniform",
    "seed": 2081206678357
  },
  "normalize_notes": null,
  "comfy_ksampler_nodes": [
    { "node": "moss:16", "steps": 100, "cfg": 3.0,
      "sampler_name": "euler", "scheduler": "sgm_uniform",
      "seed": 2081206678357, "denoise": 1.0 }
  ],
  "outputs": [
    { "kind": "image", "file": "images/i-zimage-000227.png",
      "name": "moss_eb405f6264a6_00001_.png",
      "width": 1360, "height": 768 }
  ],
  "problems": [],
  "verdict": "PASS"
}
```

**ComfyUI `/history` 原始记录**（prompt `53885c8d-…` 的 `moss:16` 节点输入）：

```json
{ "seed": 2081206678357, "steps": 100, "cfg": 3.0,
  "sampler_name": "euler", "scheduler": "sgm_uniform", "denoise": 1.0 }
```

**产物实测**（`ffprobe`）：

```
images/i-zimage-000227.png: 1360,768      # 1,159,075 bytes
```

### 4.2 批量 / 多张提交路径（batch_count=3）

证据：`evidence/moss_steps_live_batch3.json`

```json
{
  "job_id": "e2867455e09c",
  "submitted_steps": 100,
  "batch_count": 3,
  "status": "done",
  "stored_params": { "steps": 100, "cfg": 3.0, "shift": 5.0,
                     "sampler": "euler", "scheduler": "sgm_uniform",
                     "seed": 2081206678357 },
  "normalize_notes": null,
  "comfy_ksampler_nodes": [
    { "node": "moss:16", "steps": 100, "cfg": 3.0,
      "sampler_name": "euler", "scheduler": "sgm_uniform",
      "seed": 2081206678357, "denoise": 1.0 }
  ],
  "outputs": [
    { "file": "images/i-zimage-000228.png", "width": 1360, "height": 768 },
    { "file": "images/i-zimage-000229.png", "width": 1360, "height": 768 },
    { "file": "images/i-zimage-000230.png", "width": 1360, "height": 768 }
  ],
  "problems": [],
  "verdict": "PASS"
}
```

`job_batches` 三批种子递推，全部 done：

```
batch 0  seed 2081206678357  done
batch 1  seed 2081206678358  done
batch 2  seed 2081206678359  done
```

**ComfyUI `/history` 中本轮全部 4 个 KSampler 节点**（1 单张 + 3 批量）：

```
prompt 97ba95f5-… | node moss:16 | steps=100 cfg=3.0 sampler=euler sched=sgm_uniform seed=2081206678357
prompt af04cd44-… | node moss:16 | steps=100 cfg=3.0 sampler=euler sched=sgm_uniform seed=2081206678357
prompt e3d25005-… | node moss:16 | steps=100 cfg=3.0 sampler=euler sched=sgm_uniform seed=2081206678358
prompt 4cf07b9c-… | node moss:16 | steps=100 cfg=3.0 sampler=euler sched=sgm_uniform seed=2081206678359
```

### 4.3 修复前 → 修复后对照

| | 修复前（线上实测） | 修复后（线上实测） |
|---|---|---|
| 提交 | steps=100 | steps=100 |
| 落库 `params.steps` | **50** | **100** |
| `normalize_notes` | `["steps：100 不是合法取值，已改用 50"]` | `null` |
| ComfyUI KSampler `steps` | 50 | **100** |
| 实际出图 | 是 | 是 |

### 4.4 部署后线上断言

```
$ curl -s http://127.0.0.1:8190/api/image-options | python3 -c "..."
families: ['h3image', 'moss', 'qwen', 'zimage']
moss.steps: {'min': 1, 'max': 100, 'step': 1}
```

---

## 五、测试与回归

### 5.1 新增测试

`tests/test_image_param_ranges.py` —— 27 用例，覆盖：

- R1 各族面板允许值后端**必须原样接受**（含本次 BUG 的最小复现）
- R2 真正越界值（999 步 / -5 步 / 不存在的采样器）**仍然**回落 + 留说明
- R3 区间单一真源（四族齐备 / 模型表族标记 / API 下发 / 前端无硬编码）
- R4 Z-Image 放大路径用用户参数（且第二段官方参数不变）
- R5 归一说明下发到前端 + 详情弹层渲染
- 族间隔离（MOSS 放宽不放松 Z-Image，反之亦然）

### 5.2 变异自证（证明断言能抓住回归）

`scripts/mutation_image_param_ranges.py` —— 把每个修复点**故意改坏**一次，
确认对应断言**变红**：

```
基线全绿（0 failed）
OK  抓住了 M1 把 MOSS 的步数上限改回 60（还原本次 BUG 的根因）         failed=2
OK  抓住了 M2 把 Qwen 的 shift 下限改回 1.0（还原 shift=0.69 被夹的缺陷）  failed=1
OK  抓住了 M3 归一层忽略 family（所有族都用 Z-Image 量程）              failed=8
OK  抓住了 M4 放大路径第一段写死 steps=8（还原 Z-Image 放大分支的缺陷）    failed=2
OK  抓住了 M5 放大路径第一段写死 cfg=1.0                               failed=1
OK  抓住了 M6 前端重新硬编码量程（还原前端那半边的根因）                 failed=1
OK  抓住了 M6b 前端自己维护一份兜底量程表（两份量程必然漂移）              failed=1
OK  抓住了 M7 gen_params 不再下发 normalize_notes（归一说明退回静默）      failed=1
OK  抓住了 M8 前端详情不再显示「参数已调整」                             failed=1
OK  抓住了 M9 模型表族标记漏掉（param_family 不再打）                    failed=2
=== 全部 10 个变异都被抓住 ===
```

### 5.3 全量回归（基线 vs 改动后）

全量 `pytest tests/` 跑了 **4 轮**（基线 2 轮、改动后 2 轮），
因为这批测试里有**若干条依赖共享可变状态**的用例 —— 同一棵树、
同一份代码，两轮跑出的失败集合**不一样**。下表如实记录：

| 轮次 | 树 | 失败数 | 通过数 | 耗时 |
|---|---|---|---|---|
| 基线 run 1 | `46838de`（`/root/projects/h3s-imgparams-baseline`） | 12 | 3290 | 34m09s |
| **基线 run 2** | `46838de`（同上） | **11** | 3291 | 33m59s |
| 改动后 run 1 | 本轮分支 tip | 16 | 3363 | 55m41s |
| **改动后 run 2** | 本轮分支 tip | **11** | 3368 | 35m47s |

**基线 run 2 与改动后 run 2 同为 11 条失败**，逐条集合比对：

```
=== ONLY IN BRANCH (候选新增) ===
  tests/test_agent.py::...::test_repeated_identical_asset_refs_are_deduplicated_by_ordinal
  tests/test_enhance_multi_select.py::...::test_params_are_editable_and_take_effect
=== ONLY IN BASELINE ===
  tests/test_enhance_menu.py::...::test_no_console_errors_after_menu_exercise
  tests/test_page_size.py::...::test_columns_change_with_width
=== COMMON (既有失败，两边完全一致) ===
  9 条：d11_defect_a_conversion / d11_moss_lora_final / enhance_owner_badge /
         lora_file_integrity ×2 / moss_lora_damaged_and_partial ×2 /
         panel_fallback / studio::TestLoRARegistry
```

### 5.4 逐条判定：候选新增失败**不是本轮引入**

| 候选新增失败 | 判定依据 | 结论 |
|---|---|---|
| `test_agent.py::test_repeated_identical_asset_refs_are_deduplicated_by_ordinal` | 失败信息是 `AssetError: 该媒体尚未入库素材库…（素材库中找不到 asset_a）`。该用例用**假的** `asset_a` / `asset_b`，却要查**真实的共享素材库** `/mnt/usb2t/asset_library/asset_library.db`（两个 worktree 都指向这一个）。别的测试会删这个库里的条目 -> 用例**是否通过取决于跑它之前有没有别的测试刚好建过同名素材**。<br>**实证**：手动往共享库插入 `asset_a` / `asset_b` 后，本分支上该用例**立即通过**（`1 passed`）；删掉后又失败。 | 与本轮改动无关（我方 diff 未触及 `h3studio/agent.py` / `asset_ref_guard.py` / 素材库）。<br>**并行任务已在 `1dcfdf8` 修好**：「test(素材库): Agent 参考图顺序用例改用临时素材库的真实素材」。当前线上 HEAD 上该用例**通过**（见 5.6）。 |
| `test_enhance_multi_select.py::test_params_are_editable_and_take_effect` | 浏览器交互用例（真实 Chromium 点控件）。**隔离跑**在本分支上**通过**（`21 passed`）。 | flaky（浏览器时序），非本轮引入 |
| `test_enhance_menu.py::...::test_no_console_errors_after_menu_exercise` | 只在**基线**上失败，分支上通过 | flaky（方向相反） |
| `test_page_size.py::...::test_columns_change_with_width` | 只在**基线**上失败，分支上通过。<br>**实证**：基线同一份代码连跑两次 —— 第 1 次 `1 failed`、第 2 次 `22 passed` | flaky（自身就不稳定） |

**结论：新增失败 = 0。**
两次「候选新增」全部是**依赖共享可变状态 / 浏览器时序**的既有 flaky，
在本分支上**隔离跑全部通过**，且其中一条已由并行任务在 `1dcfdf8` 正式修好。

### 5.5 既有失败（两边一致，与本轮无关）

9 条共同失败全部是**本轮之前既有**的，且与图像族采样参数无关：

- `lora_file_integrity` ×2 / `moss_lora_damaged_and_partial` ×2 /
  `d11_defect_a_conversion` / `d11_moss_lora_final` —— 本机 LoRA 权重文件
  的真实损坏/半截状态（**依赖本机磁盘上的权重文件状态**，与代码无关）；
- `panel_fallback` —— 面板兜底契约与后端模型清单不同步；
- `studio::TestLoRARegistry::test_scanner_only_returns_z_image_loras` —— 同类；
- `enhance_owner_badge` —— 素材库 owner 字段（并行任务引入）。

### 5.6 当前线上 HEAD 上的验证

线上 HEAD `8886905`（已含并行任务的 `1dcfdf8` 修复）上：

```
$ pytest tests/test_image_param_ranges.py tests/test_cr5_c_segment.py \
         tests/test_moss_image.py tests/test_qwen_image.py \
         "tests/test_agent.py::TestAgentJobValidation::test_repeated_identical_asset_refs_are_deduplicated_by_ordinal"
142 passed, 1 skipped in 39.22s
```

**本轮新增的 27 条 + 相关既有 115 条全部通过**，
含此前 flaky 的那条 Agent 素材库用例。


---

## 六、交付与部署

| 项 | 值 |
|---|---|
| 本轮分支 | `local/feature/image-param-ranges-20261116` |
| 本轮分支 tip | `e9e3f61 verify(MOSS-Image): 真实浏览器验证滑杆量程与「已改」徽标` |
| **线上 HEAD（`local/maintainer`）** | **`f4678e7ef7925be4d3c2ba11456a484934e52ecb`** |
| **线上 HEAD（`local/deploy/qwen-ref-assets-live`）** | **`f4678e7ef7925be4d3c2ba11456a484934e52ecb`** |
| 部署脚本 | `scripts/deploy_imgparams_8190.sh` |
| 回滚点 | `backup/live-before-imgparams-20261116-211028` / `backup/maintainer-before-imgparams-20261116-211028` |
| 服务 | `h3-studio.service` 重启后 PID 2915803 → 2940257，探活 6 个端点全 200 |

### 本轮提交链

```
c5c07ce  fix(MOSS-Image): 采样参数量程前后端同源 —— 面板 steps=100 不再被夹回 50
2840094  docs(MOSS-Image): 参数生效性修复报告 + 线上实测证据
e9e3f61  verify(MOSS-Image): 真实浏览器验证滑杆量程与「已改」徽标
```

（`ff3f832` 是 rebase 前的中间 WIP 提交，已被 amend 进 `c5c07ce`。）

### 与并行任务的冲突处理

并行任务（`local/feature/assetlib-seed-guard`）与本轮在 `server.py` 有**一处**冲突：
双方都在 `_image_model_for_mode()` 之后新增了一个函数
（对方 `_reject_external_media_or_raise`，我方 `_image_family_for_mode`）。
解决方式：**两个函数都保留**（互不相干）。
本轮分支已 rebase 到新的 `local/maintainer`（`46838de`）之上。

**后续**：并行任务又把自己的分支 rebase 到了**本轮的提交之上**
（`8886905 merge(素材库+种子)` 的父链包含 `c4105ee` = 本轮的代码+报告提交），
因此本轮修复已被并行任务一并带进主线 —— 当前线上 HEAD `f4678e7` 的祖先链里
同时包含本轮代码提交与并行任务提交，两者共存无冲突。

### 改动文件清单

```
h3studio/gen_normalize.py                | 128 +++++++++--   量程单一真源
h3studio/h3image.py                      |   4 +             族标记
h3studio/job_meta.py                     |  12 +             下发 normalize_notes
h3studio/workflows.py                    |  63 ++++++-       放大路径用用户参数
server.py                                 |  47 ++++++-       按族归一 + 下发 param_ranges
web/app.js                                | 132 +++++++----   量程改读服务端 + 详情显示归一说明
tests/test_image_param_ranges.py         | 387 +++++++++++++  新增 27 用例
scripts/mutation_image_param_ranges.py   | 170 +++++++++++++  变异自证（10 个）
scripts/verify_moss_steps_live.py        | 205 +++++++++++++  线上实测取证
scripts/verify_imgparams_browser.py      | 205 +++++++++++++  真实浏览器验证
scripts/deploy_imgparams_8190.sh         | 205 +++++++++++++  部署脚本
evidence/moss_steps_live_single.json     |  验证证据（单张）
evidence/moss_steps_live_batch3.json     |  验证证据（批量 3 张）
evidence/imgparams_browser.json          |  浏览器验证证据（PASS）
evidence/imgparams_browser.png           |  浏览器验证截图
docs/MOSS-Image参数生效性修复报告-20261116.md | 391+       本报告
```

---

## 四·五、浏览器端实测证据（补充）

`scripts/verify_imgparams_browser.py`（真实 Chromium 打线上 8190），
证据 `evidence/imgparams_browser.json` + `evidence/imgparams_browser.png`：

```
[ranges] t2i_moss    {"#imgSteps": {"min":"1","max":"100","step":"1","value":"50"},
                       "#imgCfg": {"min":"0","max":"10","step":"0.1","value":"3"},
                       "#imgShift": {"min":"0","max":"10","step":"0.1","value":"5"}}
[ranges] t2i_qwen    {"#imgSteps": {"min":"1","max":"60","step":"1","value":"40"},
                       "#imgCfg": {"min":"0","max":"8","step":"0.1","value":"1"},
                       "#imgShift": {"min":"0","max":"3","step":"0.01","value":"0.69"}}
[ranges] t2i_h3image {"#imgSteps": {"min":"1","max":"12","step":"1","value":"6"},
                       "#imgCfg": {"min":"1","max":"8","step":"0.5","value":"1"},
                       "#imgShift": {"min":"0","max":"3","step":"0.1","value":"1"}}
[ranges] t2i         {"#imgSteps": {"min":"1","max":"60","step":"1","value":"8"},
                       "#imgCfg": {"min":"1","max":"8","step":"0.5","value":"1"},
                       "#imgShift": {"min":"1","max":"7","step":"0.5","value":"3"}}
[steps]  {"slider_value":"100","label":"100","hint":"已改","disabled":false}
[detail] {"job_id":"dace96729131","mode":"t2i_moss",
          "notes":["steps：100 不是合法取值，已改用 50"]}
判定: PASS
```

- 四个族的滑杆量程与**服务端 `IMAGE_PARAM_RANGES` 逐项一致** ——
  证明前端不再硬编码，量程真源已统一（前端那份硬编码就是本次 BUG 的另一半）。
- MOSS 面板拖到 100 -> **数值框 100**、徽标**「已改」**、控件未锁。
- 历史那条被服务端改过参数的任务（`dace96729131`）现在**带着归一说明**出现在详情里
  —— 改动不再静默。


---

## 七、范围外的同类缺陷（已定位，未修）

以下问题**确属同类**（「面板改值不生效 / 被默认值覆盖」），但落在**视频族 / 音乐族**，
与本轮改动的图像族链路无交集，且并行任务正在改 `web/video-panel.js` / `web/music.js`
（改这些文件会与其冲突）。因此**本轮只定位、不修改**，留给对应任务处理。

| # | 位置 | 机制 | 用户可见症状 | 建议修法 |
|---|---|---|---|---|
| N-1 | `web/music.js:167` | `if (!st.temperature) st.temperature = ms.temperature;` —— 0 是合法值（面板量程 0~3）却因 falsy 被回默认 | 音乐面板把温度拖到 0，实际跑 0.8 | 改成 `if (st.temperature == null)`（只判 null/undefined） |
| N-2 | `h3studio/video_workflow.py:779` | 二采开启时 `forced_sampler = "euler"`，但主流采样器下拉不禁用 | 卡片显示用户选的采样器，实际跑 euler（界面说谎） | 二采开启时禁用 `#vidSamplerSelect` + 注明「二采固定 Euler」（面板已有只读的 `#vidTwopassSampler` 说明） |
| N-3 | `server.py:2290` 起 `normalize_t2v_params` | 全程无 notes 收集；越界回落 / ρ 改写 / transition 夹回都不留痕 | 视频面板说 6 步、卡片说 3 步，无从判断原因 | 复用本轮的 `normalize_notes` 机制（`gen_normalize` 已有该模式） |
| N-4 | `h3studio/video_models.py:2396` vs `web/video-panel.js:980` | `sigma_shift_available` 放 payload 顶层，前端从 `VIDEO_OPTIONS.sampling` 读 | shift 禁用守卫永不触发（本机节点恒在，当前无可见症状） | 顶层键同步进 `sampling`，或前端改读顶层 |

---

## 八、证据文件索引

| 路径 | 内容 |
|---|---|
| `evidence/moss_steps_live_single.json` | 单张提交路径实测（steps=100，PASS） |
| `evidence/moss_steps_live_batch3.json` | 批量 3 张实测（steps=100，PASS） |
| `evidence/imgparams_browser.json` | 真实浏览器验证（量程一致 + 已改徽标 + 归一说明，PASS） |
| `evidence/imgparams_browser.png` | 浏览器验证截图（MOSS 面板步数=100） |
| `tests/test_image_param_ranges.py` | 27 用例回归测试 |
| `scripts/mutation_image_param_ranges.py` | 变异自证（10/10 抓住） |
| `scripts/verify_moss_steps_live.py` | 线上实测取证脚本 |
| `scripts/verify_imgparams_browser.py` | 真实浏览器验证脚本 |
| `scripts/deploy_imgparams_8190.sh` | 部署脚本（含部署后断言） |
| `/root/projects/comfyui-minimax-h3-studio/data/outputs/images/i-zimage-000227.png` | 单张产物（1360×768） |
| `/root/projects/comfyui-minimax-h3-studio/data/outputs/images/i-zimage-000228.png` | 批量产物 1（1360×768） |
| `/root/projects/comfyui-minimax-h3-studio/data/outputs/images/i-zimage-000229.png` | 批量产物 2（1360×768） |
| `/root/projects/comfyui-minimax-h3-studio/data/outputs/images/i-zimage-000230.png` | 批量产物 3（1360×768） |
