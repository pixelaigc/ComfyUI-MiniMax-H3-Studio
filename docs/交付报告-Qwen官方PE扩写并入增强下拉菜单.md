# 交付报告 · Qwen 官方 PE 提示词扩写并入「增强」下拉菜单

> 项目：ComfyUI-MiniMax-H3-Studio 工作台
> 源码仓库：`/root/projects/comfyui-minimax-h3-studio`
> 线上部署 worktree：`/root/projects/h3studio-live`（systemd `h3-studio.service`，端口 **8190**）
> 报告生成时间：2026-11-09

---

## 1. 需求与做法

**用户纠正**：Qwen-Image 2.1 的「官方 PE 提示词扩写」不应是独立勾选框，
必须并入现有「增强」按钮的**下拉列表**。涉及两个面板：
Qwen 图片编辑面板、Qwen-Image 2.1 文生图面板。
两处都不得再出现独立的「官方 PE 扩写」勾选框或独立按钮。

**做法**：照抄仓库里已有的同类实现（MOSS 结构化增强并入「增强」下拉）的模式，
不重新设计：

- 前端 `web/enhance-menu.js` 的增强方式注册表新增第 4 条 `qwen_pe`，
  与 `basic` / `refine` / `layout` 并列，可选其一；
- 后端新增 `POST /api/qwen-pe/expand`（`h3studio/qwen_pe.py` + `server.py`），
  走**官方 Qwen PE checkpoint 本身**，不是 Provider + 文本模板那套模拟；
- 结果复用**同一个增强结果弹窗**（`#enhanceModal` / `#enhanceResult`）与
  同一组「替换 / 追加」按钮，写回提示词框后可继续编辑，并进版本历史。

---

## 2. 改动文件清单

### 新增

| 文件 | 行数 | 内容 |
|---|---|---|
| `h3studio/qwen_pe.py` | 294 | 官方 PE 工作流构造、T2I/I2I 档位选择、参考图批处理、产物提取、thinking 剥离、有效性校验 |
| `tests/test_qwen_pe_enhance.py` | 573 | 27 项回归测试（本轮 +2 项护栏，见 §6） |
| `tests/verify_qwen_pe_live.py` | 222 | 真机验证脚本（8190 上的增强下拉 + 官方 PE 实跑） |
| `tests/verify_qwen_pe_i2i_live.py` | 116 | 真机验证脚本（带真实参考图的图片编辑档） |
| `tests/verify_lora_semantics_live.py` | 166 | LoRA 语义规则真机复核（本轮新增） |

### 修改

| 文件 | 改动 |
|---|---|
| `server.py` | 新增 `POST /api/qwen-pe/expand`；参考图上传拆出 `_upload_ref_images()`（**不**登记 `asset_refs`，预览不污染素材库、资产仍可删） |
| `web/enhance-menu.js` | 注册表新增 `qwen_pe`；`runQwenPe()` 路由到新接口；`ensureAvailable()` 模式可用性回落；官方 PE 走写回标记 |
| `web/index.html` | **删除**整个 `#imgPeField` 区块与 `#imgUsePe` 勾选框；缓存号 `qwen21-1`→`qwen21-2`、`enhancemenu1`→`enhancemenu2` |
| `web/app.js` | 删除勾选框渲染 / 事件 / 说明 DOM；保留内部 `state.imgQwen.usePe` 生成标志；删除随勾选框失效的死函数 |
| `web/providers.js` | `provState.lastEnhanceKind` + `applyQwenPeWritebackFlag()`：**写回后**才关闭二次扩写（仅预览不影响生成） |
| `h3studio/workflows.py` | Qwen 模型 `warn` 文案改写（见 §6 缺陷 1） |
| `tests/test_enhance_menu.py` `tests/test_moss_image.py` `tests/test_qwen_image.py` | 同步：不再要求已删除的勾选框，接受新增的第 4 个下拉项 |

---

## 3. 「增强」下拉最终选项列表

| # | id | 中文名 | 说明 | 可用模式 |
|---|---|---|---|---|
| 1 | `basic` | 普通增强 | 按所选模板把提示词扩写成完整描述 | 全部 |
| 2 | `refine` | 二次增强 | 把提示词里的物体逐个枚举并逐个细化 | 全部 |
| 3 | `layout` | 结构化增强（layout） | 生成 MOSS Layout JSON（对象清单 + 位置坐标）并译成最终文本提示词 | 仅 `t2i_moss` |
| 4 | **`qwen_pe`** | **qwen21图片官方增强PE** | 用 Qwen-Image 2.1 官方 PE 权重与官方 system prompt 扩写（文生图 / 图片编辑各一档） | **`t2i_qwen` + `i2i_qwen`** |

第 4 项在 Z-Image / MOSS 面板被**禁用**（带可读原因说明），切走 Qwen 面板时
主按钮会自动回落到普通增强（不会停在一个不可用的方式上）。

### 官方 PE 用的是官方权重本身

| 档位 | checkpoint | 官方采样参数 |
|---|---|---|
| 文生图 `t2i_qwen` | `qwen3.5_qwen_image_2.1_pe_t2i.int8_convrot.safetensors` | `presence_penalty=1.5`，`max_new_tokens/max_length=16256`，`thinking=True` |
| 图片编辑 `i2i_qwen` | `qwen3.5_qwen_image_2.1_pe_i2i.int8_convrot.safetensors` | `presence_penalty=0`，`max_new_tokens/max_length=24000`，`thinking=True` |

官方 system prompt 存于 `h3studio/skills/prompt-enhancer/assets/qwen21-pe-{t2i,i2i}.txt`。
**刻意不套** `validate.py` 的硬约束（那是 Z-Image 的规则，与官方 PE 冲突）。
`max_length` 不压到 4096 —— 官方文档警告此时 thinking 块吃光预算会静默返回空串。

---

## 4. 测试与真机验证

### 单元 / 回归

- `tests/test_qwen_pe_enhance.py`：**29 passed**（27 项原有 + 2 项本轮护栏）
- 全量 `pytest tests/`：见 §5 A/B 对比

### 真机（Playwright / Chromium，8190）

| 脚本 | 结果 | 关键实测 |
|---|---|---|
| `verify_qwen_pe_live.py` | **28/28 通过** | 文生图官方 PE 实跑 **32.2 秒 / 2053 字**英文结构化描述；写回后「已增强」标记出现、仍可编辑、出图不再二次扩写（`usePe=false`）；Z-Image / MOSS 面板该项被禁用；**console 0 报错** |
| `verify_qwen_pe_i2i_live.py` | **11/11 通过** | 走 PE-I2I 档、`pe_i2i` checkpoint、参考图 1 张；**91.9 秒 / 809 字**；PE **确实看到了参考图**（产物正确指出「the lighthouse (the main subject on the right side of the image)」）；单图按官方规定未加 `<image1>` 标签；**console 0 报错** |
| `verify_lora_semantics_live.py` | **19/19 通过** | LoRA 语义规则全部守住（详见 §7） |

截图与结果 JSON 存于：
`/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/qwen-pe-enhance-evidence/`
（11 张 PNG + 2 份 JSON，本轮重跑时间戳 15:29–15:32）

### 扩写前后文本（真实样例）

**文生图档**（原文：`一只橘猫坐在木质窗台上，午后阳光斜照，暖色调，浅景深`）

```
A warm, realistic photograph shows an orange tabby cat sitting on a rustic
wooden windowsill inside a sunlit room. The cat ...
```

**图片编辑档**（参考图 = 素材库里的灯塔图，原文为一条改色指令）

```json
{"action": "recolor",
 "target": "the lighthouse (the main subject on the right side of the image)",
 "details": "Recolor the entire lighthouse subject to orange: the white tower body becomes a bright o..."}
```

---

## 5. 全量 pytest 与基线 A/B 对比

### 对照点的构造（两次返工，最终版）

对照点构造方式：**当前 `local/maintainer` HEAD，逐个 `git revert` 掉本任务全部 PE 提交**
（`380c456` `8bd4d9e` `79eb5bf` `1c5b0b7`），保留并发任务的三基座效果 LoRA 目录
与音乐修复 —— 即「只去掉本任务改动」的最严格对照。

> **两次基线构造都踩过坑，记录下来免得后人重犯：**
> 1. 第一次用 `82d19b9`（PE 改动前）当基线 —— 对照组额外混入了并发 LoRA 任务
>    的改动（`z-image-effects` 目录把 Z-Image LoRA 数量从个位数推到 33 个，
>    触发 `test_scanner_only_returns_z_image_loras` 的「< 30」断言），
>    该失败**与本任务无关**。
> 2. 第二次为了避开冲突，直接 `git checkout 380c456^ -- web/app.js web/index.html`
>    取旧整文件 —— 结果把**并发音乐任务的修复也一起回退了**
>    （`isImageMode` 判定、`YuE2 音乐模型` 文案、`SheetSage2` 标注），
>    于是音乐相关 8 个用例假失败。
>
> 最终版用 `git revert` 逐提交回退 + 手工解冲突（冲突处**只**回退 PE 区块，
> 其余全取 HEAD），得到干净的对照点。

### 对照点校验

| 检查项 | 对照点 | 说明 |
|---|---|---|
| `web/enhance-menu.js` 含 `id: "qwen_pe"` | **0** | PE 下拉项已去掉 |
| `web/index.html` 含 `imgUsePe` 勾选框 | **1** | 独立 PE 勾选框回到改动前 |
| `h3studio/qwen_pe.py` | **不存在** | PE 模块已去掉 |
| `server.py` 含 `/api/qwen-pe` | **0** | PE 路由已去掉 |
| `h3studio/loras.py` 含 `z-image-effects` | **4** | 并发 LoRA 目录**保留** |
| `web/index.html` 含 `YuE2 音乐模型` / `SheetSage2` | **1 / 1** | 并发音乐修复**保留** |
| `web/app.js` 含 `const isImageMode = info.kind` | **1** | 并发音乐修复**保留** |

### 结果

| 侧 | 结果 | 失败项 |
|---|---|---|
| **特征分支**（含本任务 PE 改动） | **586 passed / 4 failed / 3 skipped** | `test_grid_flash::test_grid_height_stable_across_switch` + `test_qwen_refs_browser` × 3 |
| **对照点**（去掉本任务 PE 改动） | **554 passed / 3 failed / 3 skipped** | `test_qwen_refs_browser` × 3 |
| **差值** | passed **+32**（新增 PE 测试 29 项 + LoRA 语义脚本相关用例） | 失败项**完全被对照点覆盖** |

**4 项失败的性质：全部是 flaky，非回归。** 逐项实证：

- `test_qwen_refs_browser` × 3 —— `Page.goto: Timeout 30000ms exceeded … waiting until "networkidle"`，
  是 Playwright 等待网络空闲超时；在**对照点**与**特征分支**上隔离重跑**都通过**
  （3 passed），4 项一起重跑在特征分支也**全通过**（4 passed in 22.76s）。
  成因是全量跑时机器负载高（本机同时跑着另两套 pytest）。
- `test_grid_flash::test_grid_height_stable_across_switch` —— 测「骨架 → 真实卡片」
  一帧内的**网格高度不得抖动**，用固定 `wait_for_timeout(1200/1500)` 等时序，
  纯时序敏感，与 PE 面板无关；在**两个分支**上各隔离重跑 **3 次全部通过**。

**结论：本任务对仓库全量测试零真实回归。**

---

## 6. 本轮收尾发现并修掉的两个缺陷（均由本任务引入）

1. **死指引**：删掉独立 PE 勾选框后，Qwen 面板的告警文案仍写着
   「可在面板里关掉 PE 编码器」—— 用户会去找一个**已经不存在的勾选框**。
   改为：「…可先用『增强』下拉里的『qwen21图片官方增强PE』扩写一次并写回提示词框，
   写回后出图不再二次扩写。」
2. **死代码**：读取官方 PE 档位的辅助函数随档位徽标一起删掉后已无调用点，移除。

补两条回归护栏（断言打在**用户能看到的字符串**与**去注释后的代码**上，避免注释误报）：

- warn 文案不得再指向已删除的 PE 勾选框，且必须引到「增强」下拉的官方 PE 选项；
- `app.js` 可执行代码里不得残留独立 PE 入口标识或死函数。

> 这两条护栏第一次跑就抓到了我自己的注释里残留禁用词 —— 说明护栏有效。

---

## 7. LoRA 语义规则复核（未被破坏）

`tests/verify_lora_semantics_live.py` **19/19 通过**，规则逐条实测：

| 规则 | 实测结果 |
|---|---|
| 加速 LoRA **不出现**在用户可选列表 | `/api/image-loras` 三族实测：Z-Image 33 条 / Qwen 23 条 / MOSS 28 条，**kind 分布 100% 为 `effect`，加速类 0 条**（按权威 `kind` 字段判定，不靠文件名关键字猜 —— 效果 LoRA 文件名里常带 `Z_Image_Turbo` 这类底模名，用关键字会误判） |
| 效果 LoRA 选中后**自动应用推荐强度** | 推荐强度字段覆盖 33/33、23/23、28/28；前端 `app.js` 读取 `recommended_strength` |
| 触发词**自动追加**到最终提示词且**可改** | `merge_lora_triggers("a cat sitting on a sofa", [...])` → `a cat sitting on a sofa, triggera, triggerb`；用户改过的触发词以用户为准（`my_own_word` 生效、目录里的 `class1cpa1nt` 不出现）；空触发词不追加 |
| LoRA 节点**连 clip** | `LoraLoader`（非 `LoraLoaderModelOnly`）的 `clip` 输入已接；`_qwen_lora_chain` 内同样接 clip |

---

## 8. 提交号

| 分支 | 提交 |
|---|---|
| **`local/maintainer`（主线）** | **`1c5b0b7`** — fix(image): 官方 PE 并入「增强」下拉后的两处收尾<br>**`8f77d35`** — merge(image): 官方 PE 下拉收尾（合入 `1c5b0b7`）<br>`efe939f` — merge(image): 官方 PE 扩写并入「增强」下拉菜单<br>`99a616a` — merge(test): 补图片编辑档官方 PE 的真机验证脚本 |
| **`local/deploy/qwen-ref-assets-live`（线上部署）** | **`44f94d5`** — merge(deploy): 官方 PE 下拉收尾<br>`e47ce4d` — merge(deploy): Qwen 官方 PE 扩写并入「增强」下拉菜单<br>`9ae402b` — merge(deploy): 补图片编辑档官方 PE 的真机验证脚本 |
| 功能分支 | `local/feature/qwen-pe-enhance-menu`（`79eb5bf`）、`local/feature/qwen-pe-followup`（`1c5b0b7`） |

> 并发任务在主线/部署分支上又推进了几轮（LoRA 效果目录、LoKR 键名识别、YuE2 BF16 权重等），
> 两个分支 tip 会继续前移。**本任务的 5 个提交在两个分支的历史中均已核验存在**
> （`git merge-base --is-ancestor` 逐个确认）。

---

## 9. 是否已上线 8190

**是。**

- `h3-studio.service` → `active`，`WorkingDirectory=/root/projects/h3studio-live`，
  `GET http://127.0.0.1:8190/` → **200**
- 服务已在本轮改动后重启；`/api/image-options` 实测返回**新的 warn 文案**
- 线上 worktree HEAD 已含本任务全部提交，且并发任务
  （音乐 `e96c0ba`/`3209d3a`、素材库 `949e89f`、参考图 `a03e98f`、
  效果 LoRA 目录 `4a10be0`、LoKR 键名 `eddff33`）**全部仍在**，
  合并过程未使用 reset / stash / checkout 覆盖他人改动

---

## 10. 遗留问题

1. **官方 PE 慢且无进度条**：文生图 ~32 秒、图片编辑 ~92 秒。
   底层 `TextGenerate` 节点不吐中间进度，长任务期间界面只能等待。
2. **显存占用高**：PE 编码器权重单个约 8.9 GB，本机 4090D（48 GB）可跑；
   显存紧张时会 OOM（前端明确报错，不静默失败）。
3. **多图（≥2 张）输入的 `<image1>/<image2>` 标签路径未做真机实跑** ——
   工作流构造与单元测试已覆盖，只做了单图真机验证。
4. **并发 LoRA 任务的存量失败不在本任务范围**：
   `test_scanner_only_returns_z_image_loras`（Z-Image LoRA 数量被效果目录推到 33，
   触发该用例的「< 30」断言）与 `TestLoRAContextDamping` 2 项
   （`ctx_perturbation` 为 `None`）在对照点与特征分支上**同样失败**，
   属并发 LoRA 任务引入，本任务未触碰其代码。
5. **`test_grid_flash` / `test_qwen_refs_browser` 属时序 flaky**：
   前者用固定 `wait_for_timeout` 测帧间高度抖动，后者等 Playwright `networkidle`
   超时；在两个分支上隔离重跑均通过，但全量并发跑时仍会偶发失败。
   建议后续把它们改成事件驱动等待（等具体 DOM 状态而非固定延时）。
6. `web/index.html` 用 CRLF 行尾，Python 改写会破坏行尾；合并冲突基本都源于此。
   本轮已核对：`web/app.js` 与 `h3studio/workflows.py` 改动后仍是 100% CRLF、0 裸 LF。
7. `ekko_studio_update_session_tasks` 在本版 Web UI 里只有工具声明、**无实现**
   （`plan` toolset 仅导出 `update_plan`）；会话目标账本写入需走 stdio 直连路径。

---

## 11. 证据清单

工作区目录：
`/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/qwen-pe-enhance-evidence/`

| 文件 | 内容 |
|---|---|
| `01-qwen-t2i-panel-no-pe-checkbox.png` | 文生图面板：无独立 PE 勾选框 |
| `02-qwen-t2i-enhance-menu.png` | 文生图「增强」下拉展开，4 个选项并列 |
| `03-qwen-t2i-qwen-pe-selected.png` | 选中官方 PE，主按钮文案变「官方PE扩写」 |
| `04-qwen-t2i-official-pe-result-modal.png` | 官方 PE 扩写结果弹窗（meta 标明 PE-T2I 档 + checkpoint） |
| `05-qwen-t2i-prompt-filled-and-editable.png` | 写回提示词框 + 「已增强」标记 |
| `06-qwen-i2i-panel-no-pe-checkbox.png` | 图片编辑面板：无独立 PE 勾选框 |
| `07-qwen-i2i-enhance-menu.png` | 图片编辑「增强」下拉（含官方 PE 项） |
| `08-other-panels-qwen-pe-disabled.png` | Z-Image / MOSS 面板官方 PE 项被禁用 |
| `09-qwen-i2i-with-ref-image.png` | 图片编辑档已从素材库加入 1 张真实参考图 |
| `10-qwen-i2i-official-pe-result.png` | PE-I2I 结果（正确认出图里的灯塔） |
| `11-qwen-i2i-prompt-filled.png` | 图片编辑档写回提示词框 |
| `verify-results.json` / `verify-results-i2i.json` | 两轮真机验证的逐条断言结果（28/28、11/11） |
| `交付报告-Qwen官方PE扩写并入增强下拉菜单.md` | 本报告 |
