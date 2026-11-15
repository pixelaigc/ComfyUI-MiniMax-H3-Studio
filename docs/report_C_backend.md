# H3-Studio 后端代码审查报告（完整版）

**范围**：`/root/projects/h3s-cr-20261115/h3studio/`（69 个 .py，49,837 行）+ `server.py`（8,883 行）+ `web/` 前端对照
**分支**：`local/feature/code-review-20261115`（基线 `8bcc3d0`）
**方法**：AST 静态扫描 + 真实执行复现（Python 直调后端函数、aiohttp 起真服务打真请求、Node 直跑前端源码）
**只读**：未修改任何仓库文件，未 commit / restart / deploy。所有验证脚本写在 `/tmp/` 下，仓库工作区保持干净。

> 本文件是前一轮被截断报告的**完整重建**。所有行号、结论、复现输出均在本次重新逐条核验过；下文对每条标注「已确认」或「可疑未证实」，并在末尾附「已排查但确认无问题的怀疑点」。

---

## 〇、复核时与主线状态相关的关键事实（先读）

审查基线是 worktree `h3s-cr-20261115`（HEAD `8bcc3d0`）。复核时各 ref 的真实状态（`git` 实测）：

| ref | HEAD | C-1 覆盖代码 `params.width = legacySize.width` | 说明 |
|---|---|---|---|
| `local/maintainer`（主线） | `a383099` | **仍存在**（app.js:3341） | 未修 |
| live 部署 `/root/projects/h3studio-live` | `8bcc3d0` | **仍存在**（app.js:3170） | 未修，线上 8190 正在跑这段 |
| `local/feature/code-review-20261115` | `293e593` | **已改**（改为 `params.resolution = legacyTierForPixels(...)`） | 修复只在这个 feature 分支上 |
| 主工作树 `comfyui-minimax-h3-studio` HEAD | `b21a9e6`（2026-11-09，陈旧） | 无该代码 | 与主线不同步 |

**结论**：任务交接里说「C-1 已被主线修掉了」——**复核不成立**。修复 commit `293e593`（作者 hermes-coding，2026-11-15 22:32）**既不在 `local/maintainer` 也不在 live 8bcc3d0**（`git merge-base --is-ancestor 293e593 local/maintainer` = NO，`... 8bcc3d0` = NO）。线上 8190 当前仍是「面板说一套、产物另一套」的状态。C-1 必须按「**仍待合并/部署**」处理，不能当作已解决。

---

## 一、缺陷总表（17 条）

| # | 文件:行号 | 问题 | 严重级 | 状态 |
|---|---|---|---|---|
| C-1 | `web/app.js:3168-3172` + `web/app.js:2548-2549` | flf/r2v 提交的真实宽高被**已删除的旧面板的陈旧 state** 覆盖 | **Critical** | 已确认（主线/live 仍未修） |
| C-2 | `h3studio/video_models.py:2037-2050` + `server.py:2374` | t2v 的「不挂加速 LoRA」被静默反转成「挂 ckpt850」 | **Critical** | 已确认（live 仍在） |
| C-3 | `server.py:8560-8561` | `/files/` 路径穿越（`str.startswith` 前缀判定） | **High** | 已确认 |
| C-4 | `server.py:4738-4740` | `api_delete` 同一套 `startswith` 判定 → 越界 `unlink` | **High** | 已确认 |
| C-5 | `h3studio/workflows.py:939-941` | 图像模式 steps/cfg/shift 无归一 → 非法值直接进工作流 | **High** | 已确认 |
| C-6 | `h3studio/workflows.py:937-938` | 图像模式 sampler/scheduler 无白名单 → 非法名进 KSampler | **High** | 已确认 |
| C-7 | `server.py:4759` | `int(request.query.get("months"))` 无 try → 500 | **Medium** | 已确认 |
| C-8 | `h3studio/video_models.py:140-144` + 调用点 `server.py:2287-2292` | flf/r2v 超限时错误文案硬编码「文生视频上限 2.0M」，实为 0.8MP | **Medium** | 已确认 |
| C-9 | `h3studio/agent.py:1437` + `server.py:7616` | Agent 路径只对 t2v 跑 `normalize_t2v_params`，flf/r2v 不走完整归一 | **Medium** | 已确认（描述已修正，见正文） |
| C-10 | `h3studio/video_workflow.py:339-340` | `build_t2v_panel` 缺 width/height 时静默产出 **0×0** latent | **Medium** | 已确认 |
| C-11 | 多处（见明细） | 一批 `ALLOWED_*`/常量/函数零引用（死代码） | **Low** | 已确认（已剔除 2 个误报） |
| C-12 | `h3studio/video_models.py:133,137,672-673` | `VIDEO_MAX_PIXELS_LITERAL` / `VIDEO_MAX_PIXELS` / `VIDEO_DURATION_*_OLD` 死常量 | **Low** | 已确认 |
| C-13 | `workflows.py:260-277` + `h3_video_modes.py:68-84` + `video_workflow.py:36-51` | `_frame_expr_node` 三份重复实现（已分叉） | **Low** | 已确认 |
| C-14 | `web/app.js:2471-2478` | `bindSeg` 零调用点（DOM 已删） | **Low** | 已确认 |
| C-15 | `h3studio/analysis_jobs.py:130-137` | `progress_cb` 内连接泄漏（异常时 `c.close()` 被跳过） | **Low** | 已确认 |
| C-16 | `h3studio/agent_settings.py:182,284` | `default_t2v_defaults` / `normalize_t2v_defaults` 零引用 | **Low** | 已确认 |
| C-17 | `h3studio/assets.py:279-284` | `atomic_move` 跨盘回退非原子 | **Low** | 已确认 |

---

## 二、逐条详细说明

### 【Critical】C-1 flf/r2v 的宽高被已删除控件的陈旧 state 覆盖

**位置**：`web/app.js:3168-3172`（覆盖点）、`web/app.js:2548-2549`（数据源 `currentLegacyVideoSize`）、`web/app.js:2517-2545`（`legacyVideoSize`）、`web/app.js:2471-2478`（唯一写入者 `bindSeg`，零调用点）。

基线 `8bcc3d0` 的提交链路（`git show 8bcc3d0:web/app.js` 实测）：

```js
// 共享面板分支先写入用户真实宽高：
if (shared.width) params.width = shared.width;      // 用户真实选择
...
// 紧接着（同一段内）无条件覆盖：
const legacySize = currentLegacyVideoSize();         // 读 state.res / state.aspect
if (legacySize) {
  params.width = legacySize.width;                   // ← 把上面覆盖掉
  params.height = legacySize.height;
}
```

`currentLegacyVideoSize()` → `legacyVideoSize(state.res, state.aspect, state.mode)`，而 `state.res` / `state.aspect`：
- 初始值写死在 `app.js:128`（`res: "544p"`）与 `app.js:132`（`aspect: "16:9"`）；
- **全仓从未被写过**：`grep "state\.res *=" web/` 零命中（只有初始化），`grep "state\.aspect *="` 只命中其它对象（video-panel 自己的 `videoState.aspect`、复用参数分支写的是 `s.aspect` 而非 `state.aspect`）；
- 唯一写入者 `bindSeg()`（`app.js:2471`）**零调用点** —— 对应 DOM `#resSeg` / `#aspectChips` 已随「修复③」删除；
- 共享面板 `web/video-panel.js` 用自己的 `videoState.*`，**从不写回 app.js 的 `state`**。

于是 `legacyVideoSize("544p","16:9", mode)` 恒返回 864×480（16:9）。

**复现**（Node 直跑 app.js 抽出的真实函数 + 复刻赋值顺序）：用户在共享面板选 9:16，最终 `params.width/height = 864×480`、`params.aspect = 9:16` —— 宽高与 aspect 自相矛盾。服务端 `h3_video_size.resolve_size`（`h3_video_size.py:148`）优先级 1 是「params 里已有 width/height 就直接采用」，因此**不会**用 aspect 纠正，工作流实跑 864×480。

**用户影响**：首尾帧 / 参考生视频面板选 9:16 或选「608p·16:9」（1088×608），产物恒为 16:9 的 864×480 —— 「面板说一套、产物另一套」，正是该段注释本身要消灭的缺陷在 flf/r2v 上的翻版。

**建议修复**：删除 `web/app.js:3168-3172` 的 `params.width/height = legacySize.*` 回写，只保留用真实像素反算档位标签（`legacyTierForPixels`）。feature 分支 `293e593` 已这样改：
```js
const legacySize = currentLegacyVideoSize();
if (legacySize) {
  params.resolution = legacyTierForPixels(
    Number(params.width) || 0, Number(params.height) || 0);   // 不再回写 width/height
}
```
**但此修复尚未合并进 `local/maintainer`，也未部署到 live 8190。** 需要主线 agent 把 `293e593` 合入并重新部署。配套回归测试 `tests/test_flf_r2v_size_not_overridden.py`（用 node 真跑 `legacyVideoSize`/`legacyTierForPixels` 并断言最终宽高）已在 worktree 里（未跟踪状态），合并时应一并纳入。

---

### 【Critical】C-2 t2v 的「不挂加速 LoRA」被静默反转成「挂 ckpt850」

**位置**：`h3studio/video_models.py:2037`（`resolve_accel_lora_slot` 的判据）、`server.py:2374`（`normalize_t2v_params` 调用它）、`web/video-panel.js:1800-1807`（前端把「不挂」编码成空数组）。

**机制**：前端 `submitParams()` 对「不挂」提交 `accel_loras: []`，**不写 `accel_lora` 键**（`video-panel.js:1834` 注释「空 = 不挂」；`app.js` 的 t2v 分支 `3072-3075` 直接 `Object.assign(params, VIDEO_PANEL.submitParams())`，也不补 `accel_lora`）。而 `resolve_accel_lora_slot` 的显式槽位判据是**键存在**：

```python
# video_models.py:2037 —— 只有 "accel_lora" 这个键存在才当「用户显式表达过」
if "accel_lora" in params:
    ...
    if not key: return "", 1.0          # 空串 = 不挂（flf/r2v 走这条，正确）
# 老格式 accel_loras 数组：空数组 → for 循环不执行 → 继续往下
for item in params.get("accel_loras") or []:   # 空 → 不进入
    ...
# ...老格式 fixed_accel_lora / lora 都没有...
# video_models.py:2140 —— 兜底
return VIDEO_FIXED_ACCEL_LORA_DEFAULT, 1.0     # = "ckpt850"
```

即：t2v 只给空 `accel_loras` 而不给 `accel_lora` 键 → 所有分支都不命中 → 落到兜底 **ckpt850**。「不挂」被反转成「挂 4 步蒸馏 LoRA」。

**复现**（Python 直调，基线代码）：
```
resolve_accel_lora_slot({"accel_loras": []})            -> ('ckpt850', 1.0)   # 用户选了「不挂」
resolve_accel_lora_slot({"accel_lora": ""})             -> ('', 1.0)          # flf/r2v 显式空串，正确
resolve_accel_lora_slot({"accel_loras": [], "lora":"none"}) -> ('', 1.0)     # 有 lora=none 才正确
```
经完整归一层 `server.normalize_t2v_params`（真实 model_key `fl2va_pruned_int8`）：
```
err = ''
accel_lora       = 'ckpt850'        # ← 用户选了「不挂」
accel_loras      = [{'key':'ckpt850','strength':1.0,'enabled':True}]
fixed_accel_lora = 'ckpt850'
```
经工作流构建：t2v 的 Turbo LoRA 节点（138:0）**存在**（ckpt850 被挂上）；flf 的同名节点**不存在**（flf 正确）。

**用户影响**：文生视频面板下拉选「不挂加速 LoRA（走标准多步采样）」，服务端却挂上 ckpt850（4 步蒸馏档）。用户以为在跑 20 步标准采样，实际跑 4 步蒸馏 —— 出片质量、步数、耗时全部与选择相反。同时 `video-panel.js:600-604` 的界面文案「当前不挂加速 LoRA —— 工作流走标准多步采样（默认 20 步 / simple / res_multistep）」变成**假文案**。**flf/r2v 不受影响**（`app.js:3126` 显式写了 `params.accel_lora = ""`），所以是「同一个共享面板，t2v 坏、flf/r2v 好」的诡异不一致。

**建议修复**（二选一，推荐 A）：
- **A（前端，语义最准，改动最小）**：在 `web/app.js` 的 t2v 分支（`Object.assign(params, VIDEO_PANEL.submitParams())` 之后）补一行，让 t2v 也显式表达「不挂」——与 `app.js:3123-3128` flf/r2v 已有写法一致：
  ```js
  const accelList = params.accel_loras || [];
  const first = accelList.find(it => it && it.key && it.enabled !== false && Number(it.strength) > 0);
  params.accel_lora = first ? first.key : "";
  params.accel_lora_strength = first ? Number(first.strength) : 1.0;
  ```
- **B（后端）**：在 `video_models.py:2052`（`accel_loras` 循环）之前加一条：`if "accel_loras" in params:` 且过滤后为空 → `return "", 1.0`。注意 B 会改变「老任务落库的 `accel_loras: []`」的重放语义，需一并评估历史任务复用。

---

### 【High】C-3 `/files/` 路径穿越

**位置**：`server.py:8558-8561`。

```python
full = (OUT_DIR / path).resolve()
if not str(full).startswith(str(OUT_DIR.resolve())) or not full.is_file():
    raise web.HTTPNotFound()
```

`startswith` 是**字符串前缀**判定，不是路径边界判定。`OUT_DIR` 的同级目录 `outputs_secret` 通过检查（实测 `full = .../data/outputs_secret/creds.txt`，`startswith = True`，而 `relative_to` 正确拒绝）。aiohttp 3.14.3 真服务实测 `{path:.+}`：
```
/files/ok.txt                          -> 200
/files/../outputs_secret/creds.txt     -> 404      # aiohttp 规范化掉了裸 ..
/files/..%2Foutputs_secret%2Fcreds.txt -> 200      # %2F 编码逃逸成功
/files/%2e%2e%2Foutputs_secret%2Fcreds.txt -> 200  # %2e 编码同样逃逸
```

**用户影响**：服务默认监听 `0.0.0.0:8190`（`server.py:189`、`8865`；`config.example.json:"host":"0.0.0.0"`），**全站无任何鉴权中间件**（`grep middleware server.py` 零命中）。局域网内任意客户端可用 `%2F` 读取 `OUT_DIR` 同级目录的任意文件。

**建议修复**：`server.py:8561` 改用仓库里已有的正确判据（`h3studio/assets.py:268-275` 的 `resolve_inside` 就是这个模式）：
```python
try:
    full.relative_to(OUT_DIR.resolve())
except ValueError:
    raise web.HTTPNotFound()
```

---

### 【High】C-4 `api_delete` 同款判定 → 越界删文件

**位置**：`server.py:4734-4740`。
```python
target = (OUT_DIR / relative).resolve()
if str(target).startswith(str(OUT_DIR.resolve())) and target.is_file():
    target.unlink()
```
实测同一前缀目录会通过 `startswith` 并被 `unlink`。`relative` 来自 `jobs.outputs`（DB JSON）。正常路径由服务端生成不可控，但 `api_delete` 是用户可调 API，且 `outputs` 字段历史上被写入过外部脏值（见 `server.py:3531-3561` 的 LoRA 白名单闸门说明）。一旦 DB 出现 `../` 前缀的 file 值，删除任务即越界删文件。**修法同 C-3**：`target.relative_to(OUT_DIR.resolve())` 抛 `ValueError` 即拒绝。（对照：`media_cleanup._safe_unlink` `media_cleanup.py:50-62` 已用 `resolve()+relative_to`，是正确写法，可作范本。）

---

### 【High】C-5 图像模式 steps/cfg/shift 无任何归一

**位置**：`h3studio/workflows.py:939-941`。
```python
steps = int(params.get("steps") or model["steps"])            # 无 try
cfg   = float(params.get("cfg") ...)                          # 无 try
shift = float(params.get("shift") ...)                        # 无 try
```
`prepare_job_params`（`server.py:3502`）对图像模式**没有**任何数值归一（视频模式才有 `normalize_steps`/`normalize_shift`，见 `server.py:2645-2660`）。实测 `build_workflow("t2i", {...,"steps":"abc"})` → `ValueError invalid literal for int()`。

**用户影响**：`POST /api/jobs {"mode":"t2i","params":{"steps":"abc"}}` → 任务**入库成功**（HTTP 200）→ worker 跑到 `build_workflow` 才抛 → 用户看到归一后的通用「生成失败」，拿不到有用信息；脏值还留在 `jobs.params` 污染媒体库卡片。**建议修复**：在 `prepare_job_params` 里为图像模式补数值归一（与 `video_models.normalize_steps` 同口径），并在 `workflows.py:939-941` 用 try/except 兜底为模型默认值。

---

### 【High】C-6 图像模式 sampler/scheduler 无白名单

**位置**：`h3studio/workflows.py:937-938`。实测非法名**原样进工作流**：
```
57:3 KSampler {'sampler_name': 'not_a_sampler', 'scheduler': 'not_a_sched', ...}
```
视频模式有 `video_models.normalize_sampler`/`normalize_scheduler`（`server.py:3622-3626` 调用），图像模式**完全没有**。**用户影响**：`sampler_name` 是 ComfyUI `KSamplerSelect` 的 COMBO 值，非法值让 ComfyUI 提交时整图拒绝（"value not in list"），任务白跑一轮。**建议修复**：`workflows.py:937-938` 按 `model["sampler"]`/`model["scheduler"]` 白名单回落。

---

### 【Medium】C-7 `months` 参数未捕获 → 500

**位置**：`server.py:4759`（GET 分支），同函数 POST 分支 `server.py:4777-4780` **有** try/except，GET 没有。实测直调 `api_media_manage(GET, {"months":"abc","scope":"older"})` → 抛 `ValueError` → HTTP 500 + 堆栈进日志。**用户影响**：`GET /api/media/manage?months=abc` 直接报错，媒体库「清理预览」在手动改 URL 时崩。**建议修复**：把 4759 改成与 4777-4780 相同的 try/except 形态（默认 0）。

---

### 【Medium】C-8 flf/r2v 超限错误文案硬编码「文生视频上限 2.0M」

**位置**：文案常量 `h3studio/video_models.py:140-144`（`VIDEO_MAX_PIXELS_MESSAGE`），调用点 `server.py:2287-2292`。
```python
VIDEO_MAX_PIXELS_MESSAGE = ("文生视频分辨率上限为 2.0M 像素（{limit} px）。...")
```
flf/r2v 实际上限是 0.8MP（`max_pixels_for_mode("flf")=0.8`）。实测 `normalize_video_size(1344,768, max_pixels=0.8, strict_over_limit=True)` 返回：
```
文生视频分辨率上限为 2.0M 像素（838,860 px）。当前请求 1,032,192 px（1344×768），超出上限。
```
文案说「2.0M」括号里却是 838,860（=0.8MP），且称「文生视频」而用户在做首尾帧。**建议修复**：把常量改为带 `{limit_mp}`/`{mode_label}` 占位，由 `normalize_video_size` 按实际 `max_pixels` 与 mode 填充。

---

### 【Medium】C-9 Agent 建任务只对 t2v 跑 `normalize_t2v_params`

**位置**：`h3studio/agent.py:1437` `if mode == "t2v":` → `prepare_agent_t2v_params`（`server.py:7616`，内部硬编码 `normalize_t2v_params(merged)` 用默认 `mode="t2v"`）。

**复核修正（相对前一轮描述）**：前一轮写「flf/r2v 完全无归一 / 不做宽度上限校验」——**这条不准确**。Agent 的 flf/r2v 仍经 `agent.py:1584` 调 `prepare_job_params`，其 flf/r2v 分支（`server.py:3593-3633`）会调 `h3_video_size.apply_size`（`server.py:3609`）——实测 `apply_size({1920×1080},"flf")` → 缩到 1184×704 并记 `size_adjust_reasons`。所以**宽高是有归一的**。

真正缺失的是 `normalize_t2v_params` 独有的几项：加速 LoRA 白名单校验、注意力节点存在性检查、`twopass` 参数夹取、`use_sigma_shift` 探测——这些 flf/r2v 经 Agent 路径**不走**（面板路径也不走，但面板路径由前端保证）。因此风险面比前一轮描述的小，但「Agent 建 flf/r2v 与面板不同源」这一不一致仍成立。**建议修复**：`agent.py:1437` 改为 `if mode in ("t2v","flf","r2v"):`，并给 `prepare_agent_t2v_params` 加 `mode` 形参、按模式传给 `normalize_t2v_params`（该函数签名已支持 `mode` 参数，`server.py:2259`）。

---

### 【Medium】C-10 `build_t2v_panel` 缺宽高时产出 0×0 latent

**位置**：`h3studio/video_workflow.py:339-340`。`normalize_video_size(None,None)` 返回 `(0,0,["宽高必须是数字"])`，**不抛异常**。实测 `build_t2v_panel({...无 width/height...})` → `105:104 MiniMaxH3ImageToVideo width/height = 0 0`。**用户影响**：任何绕过 `normalize_t2v_params` 直接调 `build_workflow("t2v",...)` 的路径（Agent 老客户端、自定义脚本、`server.py:2024` 接续拼接）都会构造 0×0 latent，ComfyUI 侧报错或产出黑图。**建议修复**：`video_workflow.py:340` 之后加 `if width <= 0 or height <= 0: raise ValueError("文生视频缺少有效宽高")`。

---

### 【Low】C-11 / C-12 / C-14 / C-16 死代码

方法：AST 提定义 → 全仓（含 `tests/`）正则检索符号 → 排除定义行自身；命中数 = 1 即只有定义处。**已剔除前一轮的 2 个误报**：`ALLOWED_OUTPUT_SUFFIXES`（`agent_media_tools.py:73`）与 `ALLOWED_SUFFIXES`（`asset_api.py:49`）**并非死代码** —— 分别在 `agent_media_tools.py:271/487/490`、`asset_api.py:235` 被真实引用。

确认零引用（命中数=1）：

| 符号 | 位置 |
|---|---|
| `SUBTYPES` / `SOURCES` | `h3studio/assets.py:47` / `:57` |
| `VIDEO_MAX_PIXELS_LITERAL` | `h3studio/video_models.py:133`（`VIDEO_MAX_PIXELS`(`:137`) 本身也仅 1 处引用=定义） |
| `VIDEO_DURATION_MIN_OLD` / `_MAX_OLD` | `h3studio/video_models.py:672-673` |
| `default_t2v_defaults` / `normalize_t2v_defaults` | `h3studio/agent_settings.py:182` / `:284` |
| `MODE_DEFAULT_MODEL` | `h3studio/enhancer.py:364` |
| `VIDEO_MODES_WITH_LEGACY_LORA` | `h3studio/job_meta.py:415` |
| `GENERATION_PARAM_LABEL` | `h3studio/job_meta.py:1193` |
| `REF_AUDIO_MAX` | `h3studio/agent_schema.py:797` |
| `cleanup_scope_name` | `h3studio/media_cleanup.py:166` |
| `track_source_note` | `h3studio/h3_audio_track.py:89` |
| `DOCS_DIRNAME` | `h3studio/docs_static.py:44` |
| `PE_MODE_KEYS` | `h3studio/agent_enhance.py:65` |
| `COMPAT_SAFE_STRENGTH_CAP` | `h3studio/loras.py:391` |
| `YUE2_DEFAULT_DURATION` / `YUE2_SEMANTIC_MAX_TOKENS` | `h3studio/music_yue2.py:191` / `:149` |
| `REF_LABEL` | `h3studio/ref_images.py:41` |
| `POLL_INTERVAL_SEC` / `VIDEO_MAGIC_TABLE` | `h3studio/agent_media.py:1141` / `:210` |
| `SWITCH_SCRIPT` | `h3studio/audio_deep.py:45` |
| `CAPABILITY_REQUIRES` | `h3studio/agent_settings.py:104` |
| `REFUSAL_TEXT` | `h3studio/agent_safety.py:583` |
| `MIN_LINES_PER_SECTION` | `h3studio/yue2_schema.py:131` |
| `REC_NUMERIC_FIELDS` | `h3studio/model_recommendations.py:626` |
| `_COMFYUI_ALIAS_PREFIXES` | `h3studio/moss_lora_compat.py:546` |
| `COMFY_LLM_NODE_ALT` | `h3studio/llm.py:74` |
| `STAGE_LABELS` | `h3studio/analysis_jobs.py:67` |
| `_CONTEXT_LABELS` | `h3studio/continuation.py:460` |
| `Z_IMAGE_ACCEL_PARAMS` | `h3studio/workflows.py:204` |
| `QWEN_IMAGE_CLIP_PLAIN` | `h3studio/workflows.py:1065` |
| `bindSeg` | `web/app.js:2471`（DOM 已删，零调用点） |

**用户影响**：无直接运行时影响（这些定义不参与任何执行路径），但会误导后续维护者以为存在可用的公共 API / 可配置项（如 `MODE_DEFAULT_MODEL`、`COMPAT_SAFE_STRENGTH_CAP` 看起来像功能开关，实际无人读取）。
**建议修复**：删除上表所列定义（含 `web/app.js:2471-2478` 的 `bindSeg` 整段）；删除前逐个确认不在下表「动态注册例外」内。

**注意（本仓库最大陷阱）**：`h3studio/agent.py:627` 的 `_tool_list_modes` 与 `:1680` 的 `_tool_respond` **不是死代码** —— 被 `@tool` 装饰器动态注册进 `agent.TOOLS`（`agent.py:357-367`）。既有《死代码与冗余清理报告》§4.1 已登记此陷阱，删除前必须区分「静态零引用」与「装饰器动态注册」。

---

### 【Low】C-13 `_frame_expr_node` 三份重复实现

| 位置 | 形态 | 风险 |
|---|---|---|
| `h3studio/workflows.py:260-277` | 硬编码 `"value": 5` | 靠调用方 `workflows.py:435` 事后改写 `expr["105:111"]["inputs"]["value"] = duration` 补救；若有人直接调用而忘补写，时长恒为 5 秒 |
| `h3studio/h3_video_modes.py:68-84` | `float(duration)` 参数 | 与第三份表达式字符串完全相同 |
| `h3studio/video_workflow.py:36-51` | `float(duration)` 参数，用 `_FRAME_EXPR` 常量 | —— |

三份表达式字符串完全相同（`"max(5, round(a*24)) + (5 - (max(5,round(a*24))%17))%17"`），但接口签名已分叉（第一份无 `duration` 参数）。
**用户影响**：当前无直接错误（调用方 `workflows.py:435` 有补写），但三副本已分叉意味着「改一处修 bug，另两处不跟着修」——一旦某次只改 `video_workflow.py` 的表达式（例如调整 17 帧对齐规则），经 `workflows.py` 路径生成的视频仍是旧规则，出现同参数不同时长。
**建议修复**：删除 `workflows.py:260-277` 与 `h3_video_modes.py:68-84` 的本地副本，改为 `from .video_workflow import _frame_expr_node`，并把 `workflows.py:435` 的事后改写改为在构造时直接传入 `duration`。

---

### 【Low】C-15 `progress_cb` 内连接泄漏

**位置**：`h3studio/analysis_jobs.py:130-137`。
```python
def progress_cb(stage, frac):
    try:
        c = _library_conn()
        ...
        asset_repo.update_job(c, job_id, stage=label, progress=frac)
        c.close()          # ← update_job 抛异常时这行被跳过，连接泄漏
    except Exception:
        pass
```
**建议修复**：改成 `with _library_conn() as c:` 或 try/finally 包裹 close。

**用户影响**：分析任务（视频/音频深度分析）进度高频更新时若某次写库失败，连接不归还 → SQLite 连接数缓慢累积；长批量分析任务可能触发 `database is locked` / 连接耗尽，表现为分析任务卡住。低频、非致命，但在批量分析场景可复现。

---

### 【Low】C-17 跨盘回退非原子

**位置**：`h3studio/assets.py:279-284`。
```python
try: os.replace(source, target)
except OSError:
    shutil.copy2(source, target)
    source.unlink(missing_ok=True)
```
跨盘时 `copy2`+`unlink` 非原子 —— 进程在两步之间被杀会留下半个文件。同盘（`os.replace`）是原子的，主路径正确。**建议**：跨盘回退时 copy 到 `target.with_suffix(".tmp")` 再 `os.replace`。

---

## 三、已排查但确认无问题的怀疑点

| 怀疑点 | 验证方式 | 结论 |
|---|---|---|
| `except Exception: pass` 造成静默数据丢失 | AST 遍历全部 64 处 body 恰为 `pass` 的 handler（70 个文件），逐个读上下文 | **均安全**。全部属于「旁路功能不阻断主流程」（进度回调、提示词历史记账、流式重放、LoRA 目录扫描容错）。真正涉及用户可见数据的两处（`job_meta.py:145-166` LoRA display 回退、`video_models.py:1988-1995` 白名单查询）都有后续分支兜底，失败时返回 `name`/`False` 而非崩溃 |
| 裸 `except:` | AST 扫 `ExceptHandler.type is None` | **零命中** |
| 用户可控路径拼进文件系统操作 | 全仓扫 `os.path.join`/`Path(`/`resolve()` | 素材库链路**安全**：`assets.py:268-275` 的 `resolve_inside` 用 `relative_to` 做边界；`_upload_ref_images`（`server.py:1385-1416`）经 `resolve_inside` 校验后才读 |
| `/files/` 的 `..` 能否到达 | aiohttp 3.14.3 真服务 + 4 种编码变体实测 | 裸 `..`/`%2e%2e` 被 aiohttp 规范化掉；**但 `%2F` 编码可逃逸** → 已列为 C-3 |
| 模块级可变全局状态被多线程共享 | AST 扫模块级 List/Dict/Set + 函数内 `global` | **无真实风险**。`_STORE`/`_SNAPSHOT`/`ENHANCE_GATE` 的读改写都在同一事件循环内（单线程），无 await 间隙；`agent_memory`/`agent_sessions` 用 `sqlite3.connect(timeout=15)` 独立连接 |
| 临时文件写入原子性 | 逐个检查 `write_text`/`write_bytes` | 素材库链路**正确**：`asset_service.py:33` 写临时区 → `atomic_move` rename；`moss_lora_split.py:53-72`、`server.py:1423/1447/1464/1478` 均 tmp+replace |
| `_next_output_path` 序号分配竞争 | 读调用点 | 下载循环是串行 await；`_concat_chain_video`(`v-h3-studio-`) 与 `run_longfilm`(`v-l-`) 前缀不同不冲突。**无 TOCTOU** |
| `_concat_chain_video` 阻塞事件循环 | `server.py:2024` | 是 `await asyncio.to_thread(...)`，**已正确 offload** |
| `subprocess` 阻塞事件循环 | `grep subprocess server.py` | 3 处：`:1198`（ffprobe 30s 超时）、`:2798`（ffmpeg，已在 to_thread 内）、`:4041`（ffprobe 15s）。**可疑未证实**：`probe_output_size`/`annotate_outputs_with_size`（`server.py:1210/1240`，含同步 ffprobe）在 `ws_progress_listener`(`:1488` async) 内被**直接调用**（`:1879/2028/2080`），未 offload —— 单次 ffprobe 有 timeout 上限，影响有限；要证实是否真拖慢事件循环，需压测并发产物落库时的 ws 延迟 |
| `aiohttp.FormData` 文件对象是否泄漏 | 真服务实测请求后检查 `fh.closed` | aiohttp 内部关闭（`server.py:801/821/935` 无泄漏） |
| 前端「传了但后端不读」的参数 | 逐条对照 `video-panel.js:submitParams()` 与 `normalize_t2v_params`/`build_t2v_panel` | 参数名全部对齐（model_key/width/height/aspect/duration/steps/sampler/scheduler/shift_*/seed/accel_loras/effect_loras/attention/twopass.*）。**唯一不一致是 `accel_lora` 键缺失 → C-2** |
| 前端「后端读了但前端不传」的参数 | 同上 | `use_sigma_shift`(`server.py:2664`)、`cfg` 前端不传 —— **有意**（服务端由 `_COMFY_NODE_NAMES` 判定；标准路径 CFG 固定 1.0） |
| 二采 CFG（`twopass.cfg`）是否在 flf/r2v 生效 | 构造 `twopass.cfg=55.0` 跑 `build_flf` | **生效**：二采 SelfLiftH3Sampler cfg=55.0（`_twopass_block` 优先取 `twopass.cfg`） |
| 三段式提示词 / `<imageN>` 占位注入 | 读 `ref_images.normalize_refs` + `inject_reference_block` | 正常，`asset_id` 锚定素材库，无路径可控性 |
| `api_media_manage` 的 `scope` 白名单 | 读 `server.py:4760` | 有（all/older/failed），POST 分支 `:4774` 同样有 |
| 媒体库清理是否越界删文件 | 读 `media_cleanup.py:50-79` | **安全**：`_safe_unlink` 用 `resolve()`+`relative_to` |
| `enhancer.MODE_DEFAULT_MODEL` 是否被 `api_enhancer_config` 用 | 读 `server.py:6726-6754` | 用的是 `enh.MODE_DEFAULT_SPEC`，不是 `MODE_DEFAULT_MODEL` —— 后者确为死代码 |
| `_tool_list_modes`/`_tool_respond` 是否死代码 | 读 `agent.py:357-367` `@tool` 装饰器 | **不是** —— 动态注册进 `agent.TOOLS` |
| 图像模式 `prepare_job_params` 是否其实有归一 | grep `normalize_*` 调用点 | 图像模式确无数值/白名单归一（仅视频模式有 `server.py:2645-2660`、flf/r2v 有 `:3622-3630`）→ 支撑 C-5/C-6 |

---

## 四、可疑但未证实（需补充证据才能定性）

| 项 | 现状 | 还需要什么才能证实 |
|---|---|---|
| `annotate_outputs_with_size` 内同步 ffprobe 阻塞事件循环 | 已确认它在 async `ws_progress_listener` 内被直接调用、未 offload；未确认实际是否造成可观测卡顿 | 并发产物落库时的 ws 延迟压测（对比 offload 前后） |
| C-9 对 Agent 建 flf/r2v 的真实危害范围 | 已确认宽高经 `apply_size` 有归一；未确认缺 LoRA 白名单/注意力校验是否真能构造出坏任务 | 用 Agent 工具入口构造一个「LoRA 与底模不兼容」的 flf 任务，看是否被放行 |
| 历史脏 `jobs.outputs`（C-4 触发前提）| 已确认判定逻辑越界；未确认现网 DB 里是否真有 `../` 前缀值 | `SELECT outputs FROM jobs WHERE file LIKE '../%'` 现网扫描 |

---

## 五、建议修复优先级

1. **立刻（用户可见错误产出）**：C-1（先把 feature 分支 `293e593` 合入 `local/maintainer` 并部署 8190）、C-2（前端补 `accel_lora` 显式槽位，方案 A）。
2. **本轮（安全）**：C-3 / C-4 —— 同一处 `relative_to` 改法，改两处。
3. **本轮（健壮性）**：C-5 / C-6 图像模式参数归一、C-7 `months` try/except、C-10 0×0 拦截。
4. **清理（低风险）**：C-11 / C-12 / C-14 / C-16 死代码批量删除（**先排除 `@tool` 动态注册项**）；C-13 三份 `_frame_expr_node` 合并。
5. **文档/文案层**：C-8 错误文案参数化、C-9 Agent 路径补归一、C-15 连接泄漏、C-17 跨盘原子性。

**未修改任何仓库文件**；所有验证脚本写在 `/tmp/` 下（`t_norm2.py`、`t_flf2.py`、`t_node2.js`、`t_500b.py`、`t_img.py`、`t_path.py`、`t_0x0.py` 等），仓库工作区保持干净。
