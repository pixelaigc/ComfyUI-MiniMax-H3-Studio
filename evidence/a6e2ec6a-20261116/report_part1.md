# H3-Studio 收口报告 · a6e2ec6a · 2026-11-16

> 生成时间：__NOW__（Asia/Shanghai）
> 本轮性质：**核对 + 补缺 + 上线 + 交出终稿**（不重复 A/B/C/D 的出片）
> 线上部署：worktree `/root/projects/h3studio-live`（分支 `__BRANCH__`），systemd `h3-studio.service`，端口 8190
> 主线 `local/maintainer` = `__MAINT__`
> 线上 HEAD = `__LIVE__`
> 8190 探活：HTTP **200**，service **__ACTIVE__**，PID **__PID__**
> 额度闸门：工作区 `quota/minimax_quota_latest.json` 采样 2026-11-15 22:02:46，general 5h 剩余 **42%**（≥20%，闸门开）

---

## 一、逐条三态结论

> 判定口径：**只认当前线上 HEAD 的现场代码 + 8190 现场实测**。
> 上一轮（`941b3742`）的结论**不直接采信** —— 线上期间已被他人重新部署过
> （含修复③ `90a8914`、审计修复 `703addf` / `50fc0e2` / `0f68c79`），故逐条重测。

### A1 H3 文生视频：LoRA / UNET 参数化 —— **已上线可用（本轮修复了一处 in-scope 缺陷）**

**本轮发现并修复的缺陷（AC-1，次要但真实，属 A1 范围）**

上一轮独立验收记录的 AC-1：切换「加速 LoRA」后，**面板的步数 / 采样器 / 调度器不重算**；
且选「不挂加速 LoRA」时仍跑 **4 步蒸馏档**（非蒸馏权重配蒸馏步数 → 画面糊）。

- 根因 1（前端）：`web/video-panel.js` 的 `videoState.steps` 只在用户手动拖滑块时变化，切换加速 LoRA 时不重算。
- 根因 2（服务端）：`server.py::normalize_t2v_params` 的默认值恒取 `model["steps"]`（每个 `VIDEO_MODELS` 条目都写死 **4** = 蒸馏档），与「当前挂了哪枚 / 有没有挂」无关 → 后端 `video_workflow._resolve_accel_chain` 里那条**正确**的 20 步 / simple / res_multistep 分支**永远走不到**。
- 根因 3（槽位表达）：面板「不挂」此前只提交 `accel_loras: []`（无显式槽位键）→ `resolve_accel_lora_slot` 四条分支全不命中 → 落到兜底 `ckpt850`。用户明确选了「不挂」，工作流里却挂着一枚 4 步蒸馏 LoRA。

**修法（与并行 worker 的 `50fc0e2`「C-2 前端修」口径一致，后端判据不动）**

| 位置 | 改动 |
|---|---|
| `web/video-panel.js` | 新增 `recomputeSamplingForAccel()`：切 LoRA 时按 `recommended_steps` / `recommended_scheduler` / `recommended_sampler` 重算；切「不挂」时用服务端下发的标准多步档 |
| `web/video-panel.js` | `submitParams()` 显式提交 `accel_lora`（含空串）+ `accel_lora_strength` |
| `web/video-panel.js` | 「推荐/已改」提示的基准由**底模**改为**当前加速 LoRA** |
| `h3studio/video_models.py` | `/api/video-options` 的 `sampling` 下发 `steps_without_accel` / `scheduler_without_accel` / `sampler_without_accel`（与后端常量同源，前端不硬编码第二份） |
| `server.py` | 默认采样档跟着**实际挂的加速 LoRA**走（无 LoRA → 20 步 / simple / res_multistep） |
| `web/index.html` | bump `app.js` / `video-panel.js` 的 `?v=` 版本号（否则浏览器吃旧缓存，修复上不了线） |

**修复 commit**

- `d6a51fb` fix(a1): 加速 LoRA 切换后重算采样档（AC-1）+ 显式表达「不挂」槽位
- `cd7f175` fix(web): bump 前端静态资源版本号（app.js / video-panel.js → ac1steps1）
- 合并进主线：`6fbc1c3`；部署到线上：`7428485`

**线上实测（8190，浏览器真实交互）**

| 加速 LoRA | 面板步数 | 调度器 | 采样器 | 提示 |
|---|---|---|---|---|
| 「不挂」（默认） | **20** | **simple** | **res_multistep** | 推荐 |
| `ckpt850` | **4** | **beta** | **euler** | 推荐 |
| `lightx2v_8step` | **8** | **beta** | **euler** | 推荐 |
| 切回「不挂」 | **20** | **simple** | **res_multistep** | 推荐 |

线上接口佐证（`/api/video-options` → `sampling`）：
`steps_without_accel=20`、`scheduler_without_accel=simple`、`sampler_without_accel=res_multistep`。

**真实提交验证（本轮新提交的一次线上任务）**

- job `d2d9c06c9fc2` / ComfyUI prompt id `ca62a515-9d11-4192-a4df-f2af40d98dd9`
- 落库 params：`accel_lora=None`（**未挂任何 LoRA**）、`accel_loras=[]`、`steps=20`、`scheduler=simple`、`sampler=res_multistep`
- 对照：修复前同一路径必然得到 `accel_lora='ckpt850'` + `steps=4` + `beta` + `euler`。上一轮验收的 A1 组合 2 就是这个形态 —— `a1_case2_history.txt` 里 `UNETLoader=minimax_h3_fl2va_bf16` + **无 LoRA 节点**，却仍是 `BasicScheduler{scheduler:beta, steps:4}`：那正是本缺陷在真实工作流里的样子。
- 截图：`/root/projects/h3studio-live/evidence/a6e2ec6a-20261116/AC1_accel_steps_recompute_live.png`

**结论：已上线可用**（A1 的 LoRA/UNET 槽位下发本身在上一轮已验证并保持有效；本轮补掉了「切换不重算 + 不挂被反转」这个真实缺陷）。
