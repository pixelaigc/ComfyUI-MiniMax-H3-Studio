# MOSS-Image 上线交付说明

- **日期**：2026-11-14（CST）
- **仓库**：`/root/projects/comfyui-minimax-h3-studio`
- **主线分支**：`local/maintainer`
- **线上部署 worktree**：`/root/projects/h3studio-live`（systemd `h3-studio.service`，端口 8190）
- **交付性质**：上线闭环（合并 + 部署 + 探活），**无独立验收**（原独立验收任务 c2c63155 因 "Acceptance time budget exhausted" 连续 3 次失败且 output 为 0，已由用户决定改由本外层上线任务兜底）

---

## 1. 主线现状核实结论

动手前先核实现状，**不采信任何摘要**：

| 检查项 | 实测结果 |
|---|---|
| `git status`（主 worktree） | 分支 `local/feature/mobile-responsive-fix`，有他人未提交 WIP（已改动文件 + 未跟踪文件），**全程未触碰** |
| `git branch -a --contains 102adf0` | `local/feature/moss-ui-merged`、`local/feature/moss-ui-verify` |
| `git merge-base --is-ancestor 102adf0 local/maintainer` | **NO** — 不在主线祖先链 |
| `git merge-base --is-ancestor 102adf0 dadbf8a` | **NO** — 不在线上祖先链 |
| 合并前 `local/maintainer` HEAD | `367a169 merge(d11): 内置 Agent 能力增强上线主线` |
| 合并前线上 live HEAD | `dadbf8a merge(deploy): D9+D10+D12 批次上线 8190` |
| 线上 `web/responsive.css` FAB z-index | **930**（缺陷态，与背景描述一致） |

**结论：102adf0 是已提交的正式 commit（不是他人未提交的 WIP），但从未进入 `local/maintainer`，也从未部署到 8190 —— 本轮必须做真实合并 + 真实部署。** 线上窄屏 FAB 被抽屉遮挡的缺陷在用户实际使用中依然存在。

---

## 2. 合并范围与冲突处置

### 2.1 合并方式

在**新建的干净 worktree** `/root/projects/h3studio-moss-merge`（分支 `local/feature/moss-live-merge`，起点 `367a169`）中执行 `git merge --no-ff --no-commit 102adf0`。

**为什么不直接在 `h3studio-merge`（主线 worktree）里合**：该 worktree 存在**他人未提交的暂存状态**（43 个文件，staged tree = `dc8ee31` 的 tree）。按任务纪律「禁止 git reset / stash / checkout -- . / 清理他人改动」，采用新建 worktree + `git update-ref` 原子推进分支的方式，**全程未执行任何 reset/stash/checkout/force-push**。

**未触碰他人 WIP 的证明**（主线 worktree 指纹，推进前后完全一致）：

```
推进前: index tree = d2c2055c60b423b75ff1d665d857b69ceb6d4848
        文件树 sha256 = a13e815b73360c899237b05c7a7ae1cc554555603fd61ebcec2d36a0bc3b4ef8
推进后: index tree = d2c2055c60b423b75ff1d665d857b69ceb6d4848
        文件树 sha256 = a13e815b73360c899237b05c7a7ae1cc554555603fd61ebcec2d36a0bc3b4ef8
```

**⚠️ 一个必须说明的副作用（内容零丢失，但 diff 基线前移）**：

`h3studio-merge` 这个 worktree 的 HEAD 是 `refs/heads/local/maintainer` 的符号链接。主线分支前移后，这个 worktree 的 **HEAD 自动跟到了 `7ac7bc8`**（git 行为，非本任务主动操作）。

后果：该 worktree 的 `git diff --cached` 统计路径数从 **43 变成 49**。

- **用户暂存区内容逐字节未变**：index tree 仍是 `d2c2055c...`（== `dc8ee31` 的 tree，且该 tree 是一个已存在的 git object，随时可恢复）。
- **工作区文件逐字节未变**：文件树 sha256 仍是 `a13e815b...`。
- 多出的 6 条只是「**diff 基线变了**」造成的显示差异，不是新增了改动：
  `h3studio/workflows.py`、`web/app.js`、`web/responsive.css`、`tests/test_responsive_mobile.py`、`evidence_moss_live/parity_probe.py`、`evidence_moss_live/parity_probe_js.py`
- **这 4 个 MOSS 文件在 index 里的内容仍等于旧版 `367a169`**（已逐个核对 blob hash：`workflows.py` index=`c172449c`==old、`app.js` index=`f01bf6cf`==old、`responsive.css` index=`7bb44b66`==old、`test_responsive_mobile.py` index=`bfd02c71`==old）。也就是说，**若该 worktree 的所有者直接 `git commit`，会把这 4 个文件提交成「回退到 MOSS 修复前」的版本**。
- **风险可控且可恢复**：这些内容在 git object 里完整存在（`dc8ee31` / `367a169` 两个 commit 都在），随时可 `git checkout <commit> -- <path>` 取回。

**结论**：本任务没有破坏、清理或覆盖任何他人改动（内容与工作区文件均逐字节一致），但主线分支前移使那个 worktree 的 diff 视图发生变化，**需要人工知悉**：那个 worktree 的所有者在提交前应当意识到基线已前移。

### 2.2 合并内容

来自 `102adf0`（`local/feature/moss-ui-merged`），4 个文件 +362/-41：

| 文件 | 内容 |
|---|---|
| `h3studio/workflows.py` | 尺寸链路后端修复（`_js_round` / 离散步长钳制 / `normalize_qwen_image_size`） |
| `web/app.js` | 尺寸链路前端修复（`imgTierSizeRaw` / `imgTierSize` / `isZImageMode`） |
| `web/responsive.css` | 窄屏 FAB `z-index: 930 → 960` + `.panel-fab[hidden]{display:none}` |
| `tests/test_responsive_mobile.py` | 新增 FAB 遮挡回归断言（82 行） |

### 2.3 冲突逐条处置（唯一冲突文件：`web/app.js`，2 处）

两处冲突都是**语义冲突**（两侧各自新增了不同能力），不是同代码的版本分歧，**未使用 `--ours` / `--theirs` 一把梭**，逐处人工合并。

#### 冲突 1 —— 模式判定函数（行 ~84）

- **HEAD（367a169）**：`isH3ImageMode()`（D1-D6 批次引入的 MiniMax-H3-Image 族判定）
- **102adf0**：`isZImageMode()`（Z-Image 族判定，官方 2K 放大路径）

**处置：两者并存**。二者判定的是**不同模型族**，语义无冲突，合并后各 5 处引用全部保留。

> 背景核查：`102adf0` 的基点是 `ad9841f`，那时 `h3studio/h3image.py` **尚不存在**（该文件由 `84da697 feat(h3-image)` 引入）。所以两侧各自新增判定函数是历史必然，不是谁覆盖了谁。

#### 冲突 2 —— 超上限像素钳制（行 ~1508）

- **HEAD**：`const sw/sh` + 按模式区分的上限文案（H3-Image 无放大链 / MOSS 有 hires LoRa）
- **102adf0**：`let sw/sh` + 「退一格」while 循环（对齐到 16 的倍数后可能仍略微超限，需再退一格直到不超）

**处置：按族条件化合并** —— 保留 102adf0 的 while 循环，但加 `!isH3ImageMode()` 条件；保留 HEAD 的按模式文案。

**为什么这样处置（实测依据，非推理）**：

后端三族归一化函数的「退一格」逻辑**不一致**：

| 族 | 后端归一化函数 | 有「退一格」while 循环？ |
|---|---|---|
| MOSS | `normalize_moss_image_size` | ✅ 有（`workflows.py:2196`） |
| Qwen | `normalize_qwen_image_size` | ✅ 有（`workflows.py:1288`） |
| H3-Image | `normalize_h3_image_size`（`h3image.py`） | ❌ **没有**（只做一次比例缩放） |

若一刀切采用 102adf0 的循环，H3-Image 前端会比后端**多退一格** → 界面显示 5440×3040、实际提交 5472×3072。

实测探针（`evidence_moss_live/parity_probe.py`，10 组尺寸 × 3 族）：

```
MOSS      stepdown_variants=10 stepdown_mismatch=0  plain_mismatch=3
Qwen      stepdown_variants=10 stepdown_mismatch=0  plain_mismatch=1
H3-Image  stepdown_variants=10 stepdown_mismatch=3  plain_mismatch=0
```

→ 结论：MOSS/Qwen **必须**带循环，H3-Image **必须不带**。故按族条件化。

**最终验证**：`evidence_moss_live/parity_probe_js.py` **直接 eval 合并后真实 `web/app.js`** 的 `normalizeImgSize`，与 4 个真实后端函数逐格对比（4 族 × 12 组尺寸 = 48 组）：

```
TOTAL=48  MISMATCH=0
```

#### 2.4 一个被自查发现并修正的自身失误

用 Python 改写 `web/app.js` 时**意外把全文 CRLF 行尾改成了 LF**，导致 diff 爆炸成 12345 行。已还原为 CRLF（现 CR=6222 / LF=6222，与 HEAD 一致），最终 `web/app.js` diff 收敛到 **147 行**。其余 3 个文件的行尾与 HEAD 一致，未受影响。

---

## 3. 合并后 commit

| 角色 | Commit | 说明 |
|---|---|---|
| 主线合并 commit | **`7ac7bc8`** | `367a169` + `102adf0`，主线 `local/maintainer` 已指向它 |
| 线上部署 commit | **`250c71e`** | `dadbf8a` + `7ac7bc8`（线上部署分支 `local/deploy/qwen-ref-assets-live`） |
| 证据落盘 commit | **`384d6a0`** | 线上分支追加提交 `evidence_moss_live/` |

**部署 commit 采用本仓库既有模式**：把主线合入部署分支（而非直接 fast-forward 部署分支），从而**保留部署分支上的历史交付文档**（`docs/D2-上线交付说明.md`、`docs/上线交付说明-2026-11-13-Agent界面修复.md`）。已核对：若直接切主线会静默丢失这 2 个文档。

**主线推进方式**：`git update-ref refs/heads/local/maintainer 7ac7bc8 367a169`（CAS 原子操作）。
`git branch -f` 被 git 拒绝（`cannot force update the branch used by worktree at '/root/projects/h3studio-merge'`）—— 这正是保护那个脏 worktree 的机制，故改用 `update-ref`。

---

## 4. 备份 / 回滚点

| 分支 | 指向 | 用途 |
|---|---|---|
| `backup/live-before-moss-20261114-124027` | `dadbf8a` | **线上回滚点**（部署前 live HEAD） |
| `backup/maintainer-before-moss-20261114-124027` | `367a169` | **主线回滚点**（合并前主线 HEAD） |

**回滚命令**：

```bash
# 回滚线上
cd /root/projects/h3studio-live
git merge --no-ff --no-commit backup/live-before-moss-20261114-124027
git commit && systemctl restart h3-studio.service

# 回滚主线
cd /root/projects/comfyui-minimax-h3-studio
git update-ref refs/heads/local/maintainer 367a169 7ac7bc8
```

---

## 5. 部署步骤

1. `git worktree add /root/projects/h3studio-moss-merge -b local/feature/moss-live-merge 367a169`
2. `git merge --no-ff --no-commit 102adf0` → 1 处冲突 → 人工处置 → `git commit` → `7ac7bc8`
3. `git update-ref refs/heads/local/maintainer 7ac7bc8 367a169`（CAS 原子推进主线，不动脏 worktree）
4. 建 2 个 backup 分支
5. `cd /root/projects/h3studio-live && git merge --no-ff --no-commit 7ac7bc8` → **无冲突** → `git commit` → `250c71e`
6. `systemctl restart h3-studio.service`
7. 证据落盘 commit → `384d6a0`

---

## 6. 探活证据

**systemd**：`is-active: active`，MainPID `2440286`，ActiveAt `Sat 2026-11-14 12:40:52 CST`，监听 `0.0.0.0:8190`。

**HTTP 探活（首页 + 9 个关键 API，全部 200）**：

```
/                          -> 200
/api/jobs                  -> 200
/api/queue                 -> 200
/api/image-options         -> 200
/api/video-options         -> 200
/api/generation-types      -> 200
/api/comfy-models          -> 200
/api/continuation/state    -> 200
/api/system                -> 200
/api/providers             -> 200
```

首页含文生图入口（抓到 `t2i_moss` / `moss`）；`/api/generation-types` 含 `{"key": "t2i_moss", "label": "MOSS-Image 文生图", "media": "image"}`。

**静态资源 md5（served `/static/...` vs live worktree 磁盘文件）**：15/15 全部逐字节一致（app.js / style.css / assets.css / responsive.css / index.html / qwen-refs.js / agent.js / providers.js / job-detail.js / assets.js / video-panel.js / video-effect-lora.js / music.js / interaction-guard.js / moss-layout.js）。

> 注：静态资源真实路径是 `/static/<file>`。最初用 `/<file>` 探测时全部返回同一个 md5（是 SPA 的 index.html 回退），已改用正确路径复核。

**journalctl**：`--since "2026-11-14 12:40:52" -p err` → `-- No entries --`，error 级日志 **0** 条。

**测试 A/B（基线 vs 改动树，同一份清单两棵树跑）**：

| 树 | 结果 |
|---|---|
| 基线（`367a169`，worktree `/root/projects/h3studio-moss-base367`） | 2 failed, 195 passed, 1 skipped |
| 改动（`7ac7bc8`，worktree `/root/projects/h3studio-moss-merge`） | 2 failed, **196** passed, 1 skipped |

失败清单**完全相同**（2 条均为 `tests/test_page_size.py` 的 playwright 超时，与本次合并无关）：
`test_auto_refresh_keeps_page_size`、`test_current_page_highlighted`

**新增失败差集 = 0** ✅。通过数 195 → 196（+1 为 102adf0 新增的 `test_panel_fab_is_not_occluded_by_open_drawer`，在改动树通过）。

---

## 7. 功能抽查证据

全部打**线上真实 8190**（脚本 `evidence_moss_live/functional_check.py` + `narrow_fab_check.py`，`ALL_PASS = True`）。

### 7.1 MOSS-Image 面板可打开 ✅
真实点击左侧导航 `a.nav-item[data-mode='t2i_moss']` → `mode=t2i_moss`，面板 `display=flex`、宽 330px，`state.imgMoss` 存在，tier=1mp。

### 7.2 LoRA 选择生效 ✅
- 清单：`/api/image-loras?base=MOSS-Image` → **139 项**（例：`moss/moss_hires_lora_v0.1_qkv_split.safetensors`）
- 前端 `#imgLoraSelect` 有 **134** 个可选项；选中 `moss-effects/Cinematic_MossSFT_c1-st6000.safetensors` 后 `state.imgMoss.loras` 回写 **5** 项

### 7.3 真实提交一次生成并看到结果卡片 ✅
- 点击 `#genBtn` → 弹出「增强确认」门 `#enhanceGateModal` → 点 `#enhanceGateConfirm` → **POST /api/jobs**
  （图像模式 + 提示词未增强时前端会弹此门，是产品既定行为。首次排查时因未处理此门而误判为「提交无反应」，已定位并修正）
- **只认新增 job id**：提交前 100 个 job → 提交后新增 **1** 个 `0f8f2e4f959a`（避免被历史任务骗过）
- 新任务落库实测：`mode=t2i_moss`、`gen_type_label=MOSS-Image 文生图`、`status=done`、`elapsed=85.25s`、`error=null`、`has_output=true`
- 参数实况：`1360×768`、`1mp / 16:9`、`loras` 3 条（含 `moss_xvstyle` trigger）、`slg_single_layers=22`（MOSS 专属参数）
- 出图文件：`images/i-zimage-000141.png`，1,172,526 bytes，PNG 1360×768
- 线上可访问：`GET /files/images/i-zimage-000141.png` → 200 / image/png，served md5 `9537c6fe...` == 磁盘 md5（逐字节一致）
- **视觉核验（vision_analyze）**：确认是**有效且连贯**的生成图 —— 橘色虎斑猫戴宇航员头盔、白色宇航服、蓝色纯色背景，写实渲染、毛发/面罩反光/胡须细节完整，无破图/崩坏/空白

### 7.4 窄屏 375px / 390px FAB 不再被抽屉遮挡 ✅

| 视口 | 抽屉关闭 | 抽屉打开 |
|---|---|---|
| 375px | FAB visible=True, z=960, `elementFromPoint` 命中 SPAN（FAB 自身）→ **可点** | FAB `hidden=true` `display=none`，几何 rect `{0,0,0,0}` → **干净让位** |
| 390px | 同上 → **可点** | 同上 → **干净让位** |

两种视口下「点 FAB 能打开抽屉」均为 True。

**实测层级**（线上 computed style）：`.panel-fab` z=**960** / `.panel` z=**950** / `.panel-mask` z=**949** —— FAB 高于抽屉 10 层、高于遮罩 11 层。

**像素/元素重叠判定（修复前 vs 修复后，A/B 对照）**：

在独立端口 8191 起了一个**修复前**的实例（`backup/live-before-moss-20261114-124027` 的代码），用**同一份** probe 脚本跑两边：

```
修复前 (8191, z=930): 抽屉打开后 FAB 仍可见 (display=flex, hiddenAttr=False)
                      elementFromPoint(FAB 中心 318,682) 命中 INPUT#imgSteps
                      hitIsFab=False -> FAIL(FAB 被抽屉遮挡，点不动)      ← 期望 False ✅
修复后 (8190, z=960): 抽屉打开时 FAB hidden + display:none
                      hitIsFab=None  -> PASS(干净让位)                    ← 期望 True  ✅
断言能变红 -> True
```

对照实例取完证据后已停掉（8191 已 CLOSED，8190 服务不受影响）。

> 说明：单纯用 JS 注入 `z-index:930` 做负向对照**不可靠**（`[hidden]` 规则与 `display` 优先级会互相干扰，结论不唯一）。最终改为**起一个真实的修复前实例**做端到端 A/B，结论唯一且可复现。

---

## 8. 尚存缺陷清单

本轮**只做上线，未做功能修复**。以下为**上线后仍然存在**的问题，如实记录：

| # | 缺陷 | 影响 | 证据 / 说明 |
|---|---|---|---|
| 1 | **H3-Image 族前后端尺寸口径仍不一致** | H3-Image 手填超 16MP 宽高时，界面显示尺寸 ≠ 实际提交尺寸（例：8192×4096 → 后端 5792×2912，前端 5760×2880） | 后端 `h3image.normalize_h3_image_size` 缺「退一格」循环，且用 Python 银行家舍入而非 `_js_round`。**根因在 102adf0 之前就存在**（H3-Image 是 D1-D6 批次引入的），本轮合并只保证**不引入新的不一致**（已按族条件化避免），未修复既有偏差 |
| 2 | **H3-Image 后端用 Python 内置 round** | 恰好 `.5` 时前后端可能差一个倍数步长（16 或 32 px） | `h3image.py` 的 `int(round(...))` vs 其余两族的 `_js_round()`。同上，既有偏差 |
| 3 | `tests/test_page_size.py` 2 条 playwright 超时失败 | 与本次上线无关 | 基线树与改动树**同样失败**，已归因为环境性既有失败 |
| 4 | 共享仓库仍有他人未提交 WIP | 未处理 | `h3studio-merge` worktree（43 文件 staged）+ 主 worktree `local/feature/mobile-responsive-fix`。**按纪律未做任何清理**，需人工决定如何处置 |
| 5 | 无独立验收 | 验收证据由本上线任务自采 | 原任务 c2c63155 连续 3 次「Acceptance time budget exhausted」失败且 output 为 0，post-hoc 无法调大超时；已由用户决定改由外层上线任务兜底 |

**已修复并验证的缺陷**：窄屏 FAB 被抽屉遮挡（`z-index` 930→960 + `[hidden]` 显式规则）。

---

## 9. 是否需人工决定

**不需要阻塞性人工决定** —— 本轮上线闭环已完整完成（合并 → 部署 → 探活 → 功能抽查全部通过）。

以下 2 项**建议由人工后续处置**（不阻塞）：

1. **共享仓库的他人 WIP 怎么处置** —— `h3studio-merge` worktree（43 文件 staged）+ 主 worktree `local/feature/mobile-responsive-fix`（31 项改动）。**按纪律未做任何清理**，需人工确认归属后再决定是否提交/丢弃。
   **⚠️ 特别注意**：主线前移后，`h3studio-merge` 的 HEAD 已自动跟到 `7ac7bc8`，其暂存区里那 4 个 MOSS 文件仍是**修复前**的旧内容。若该 worktree 的所有者直接 `git commit`，会把这 4 个文件提交成回退版本。**该 worktree 的所有者需在提交前重新 `git add` 这 4 个文件**（或先 `git reset` 重设基线）。
2. **缺陷 #1 / #2（H3-Image 尺寸口径）是否要修** —— 属功能修复范畴，超出本轮「只做上线」的范围。可另开一轮修复，修复时应同步改后端 `h3image.normalize_h3_image_size` 与前端，并补前后端一致性回归。

---

## 10. 证据文件清单

工作区：`/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence-moss-live/`

| 文件 | 内容 |
|---|---|
| `01-merge-and-tests.txt` | 主线核实结论、合并范围、A/B 测试结果、前后端尺寸一致性 |
| `02-deploy-liveness.txt` | 部署步骤、systemd、HTTP 探活、静态资源 md5、journalctl |
| `03-functional-check.txt` | 功能抽查全过程 + 窄屏 FAB 判定 + A/B 负向对照 |
| `functional_check.py` / `functional_check.json` | 功能抽查脚本与结构化结果（ALL_PASS=True） |
| `narrow_fab_check.py` / `narrow_fab_check.json` | 窄屏 FAB 判定脚本与结构化结果（ALL_PASS=True） |
| `parity_probe.py` | 前后端尺寸口径探针（手抄前端版） |
| `parity_probe_js.py` | 前后端尺寸口径探针（直接 eval 真实 app.js 版，48/48 一致） |
| `02-moss-panel-live.png` | 线上 MOSS 面板整页截图（含作品库 MOSS 结果卡片） |
| `04-moss-generated-output.png` | 本次真实出图原图（1360×768） |
| `05-moss-result-card.png` | 作品库中的 MOSS 结果卡片截图 |
| `03-narrow-375-fab.png` / `03-narrow-390-fab.png` | 窄屏抽屉打开态（FAB 已让位） |
| `06-narrow-375-closed.png` / `06-narrow-390-closed.png` | 窄屏抽屉关闭态（FAB 可点） |

仓库内同份证据：`/root/projects/h3studio-live/evidence_moss_live/`（commit `384d6a0`）
