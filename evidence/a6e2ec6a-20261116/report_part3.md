
---

## 二、本轮改动与上线记录

| 项 | 值 |
|---|---|
| 修复 commit（本轮唯一代码改动） | `d6a51fb` + `cd7f175` |
| 特性分支 | `local/feature/r5-accel-steps-20261115`（基于线上 HEAD `477eff3` rebase） |
| 合并进主线 | `local/maintainer` = `__MAINT__` |
| 部署到线上 | `local/deploy/qwen-ref-assets-live` = `__LIVE__` |
| `merge-tree --write-tree` | 两次退出码均为 **0**（干净可合，无冲突） |
| 8190 探活 | HTTP **200** / service **__ACTIVE__** / PID **__PID__** / `app.js?v=ac1steps1` / `video-panel.js?v=ac1steps1` |
| 线上静态资源校验 | `GET /static/video-panel.js` 含 `recomputeSamplingForAccel` **4** 处（修复已生效，非缓存旧文件） |
| 回滚点 | 合并前主线 `a383099`、线上 `477eff3` |

### 回归对跑（与线上 HEAD `477eff3` 基线逐条两树隔离复跑）

- 定向用例集：`test_accel_steps_follow_accel.py`（新增 20 条）+ `test_video_sampling_params.py`
  + `test_accel_lora_loader_aware.py` + `test_t2v_panel.py` + `test_t2v_panel_fix.py`
  + `test_h3_panel_tune.py` + `test_media_card_detail.py` + `test_mediadef_all_types.py`
  + `test_accel_lora_none_not_inverted.py`
- **基线（477eff3）：8 failed / 246 passed**
- **本分支：8 failed / 246 passed**
- ⇒ **新增失败 0 条**。8 条失败在两棵树中**逐条完全一致**，均为基线既有失败
  （`test_video_sampling_params.py` 6 条：老 flf/r2v 路径要求首尾帧图片的构造口径；
  `test_h3_panel_tune.py` 2 条：`dmad_4step_critic` 在本机清单里不存在 → KeyError）。
- 变异检查（三处修复分别撤销，均被测试抓住）：
  1. 撤销前端 `recomputeSamplingForAccel()` 调用 → `test_21` 红
  2. 撤销服务端默认步数改法 → `test_01` / `test_01b` / `test_03` 三条红
  3. 撤销槽位空数组语义改法 → 曾触发 `test_accel_lora_none_not_inverted` 红
     ⇒ **据此回退该处后端改动**，改为与并行 worker `50fc0e2` 一致的前端修法
     （后端「完全没有加速信息 → 默认 ckpt850」是老任务重放语义，动它会让老任务画面变样）

---

## 三、并行 worker 修复 ①②④ 的线上落地核对（本轮只读核对，未改动其代码）

| 修复 | 内容 | 线上 HEAD 是否包含 | 核对方式 |
|---|---|---|---|
| ① | PE 全量外置 + H3-IMAGE 专属 PE 模板 | **是** | `git merge-base --is-ancestor 01fe952 HEAD` = YES；`h3studio/enhancer.py` 含 `h3image-pe-t2i` / `h3image-pe-i2i` 专属模板档；`server.py` 含 `POST /api/h3image-pe/expand` 与 `POST /api/qwen-pe/expand`；`h3studio/h3image_pe.py` 含 `use_default_template` |
| ② | 媒体库类型过滤串台 + 图片编辑素材描述只传第一行 | **是** | `git merge-base --is-ancestor a383099 HEAD` = YES；`web/app.js` 长片拼接改用自己的候选池 `collectStitchCandidates` / `renderStitchCandidatePool`，不再对共享媒体库强加 `media=video`；`h3studio/ref_api.py` 有 `DESCRIPTION_MAX_CHARS` 截断标记 |
| ④ | 稀疏注意力 FinalLayer 参数不匹配 | **是（但根因在工作台之外）** | 报告落盘 `docs/修复④-稀疏注意力FinalLayer参数不匹配-交付报告-20261115.md`；根因在第三方插件 H3-Optimizations 的 FinalLayer 兼容层（契约探测写死旧名 `shifts`，ComfyUI 0.45.3 改名 `grids`），**工作台本身无需代码改动**；插件侧已修（`/home/ubuntu/comfyui/custom_nodes/H3-Optimizations` HEAD `2bc9ac5`，含 `fix(final-layer): 支持 ComfyUI 0.45.3 的 grids 契约` + 端到端回归）；`comfyui.service` 在插件修复后已重启（插件文件 mtime 20:22、ComfyUI 重启 20:38） |

> 说明：①②④ 均由并行 worker 提交并已在主线/线上，本轮**未改动**其代码，只做只读核对与落地确认。

---

## 四、已知限制

1. **A1 本轮新任务的成片尚未落盘**：job `d2d9c06c9fc2`（prompt id `ca62a515-9d11-4192-a4df-f2af40d98dd9`）提交后一直排在 ComfyUI 队列里
   （单线程 worker，前面有其它 agent 的任务），本轮收尾时仍为 `running`。
   但**本缺陷的关键证据已经拿到**：落库 params 已是 `accel_lora=None` + `steps=20` + `simple` + `res_multistep`
   （修复前必然是 `ckpt850` + `4` + `beta` + `euler`），且浏览器侧四档切换实测全绿。
   成片路径待任务完成后可查 `videos/v-h3-studio-*.mp4`。
2. **C 项带副作用的 9 个 Agent 工具未做对话级逐条实测** —— 在他人正在使用的生产素材库上跑会产生不可回收的副作用；服务端实现由定向测试覆盖。
3. **A2/A3/B1/B2/C 的浏览器级证据沿用上一轮**（`.studio-acceptance/941b3742-…/1/` 下的截图与 history 文本）：
   本轮线上 HEAD 未改动这几条的实现（仅 AC-1 相关文件与前端版本号有变），故未重复跑浏览器交互；
   本轮对这几条做的是**接口层现场复核**（A2 播放列表 / 媒体库分类计数 / A4 gen_defaults / A5 running_truth / A1 jobs.db 记录）。
4. **线上被他人持续重新部署**：本轮上线后 `local/deploy/qwen-ref-assets-live` 仍可能被他人再次推进；
   本报告的「线上 HEAD」只对 `__LIVE__` 这个 commit 成立。
5. 本轮**未签署任何验收结论**，c1–c10 未被改写；验收由宿主另派验证轮。
6. 全量 pytest 与基线对跑采用**逐条两树隔离复跑**（定向用例集），未跑全量套件 —— 全量耗时超本轮 30 分钟收尾预算。

---

## 五、证据清单（全部落仓库内持久路径）

目录：`/root/projects/h3studio-live/evidence/a6e2ec6a-20261116/`

| 文件 | 内容 |
|---|---|
| `AC1_accel_steps_recompute_live.png` | **A1 本轮修复**：线上 8190 文生视频面板，8 步 LoRA 选中态（步数/调度器/采样器已随 LoRA 重算） |
| `a1_nolora_submit.json` | A1 本轮真实提交的线上任务返回（job `d2d9c06c9fc2`） |
| `a2_player.json` | A2 播放列表服务端返回（entries=3 / counts / img_seconds） |
| `d_ffprobe.json` | D 项成片 ffprobe 原始输出（时长/分辨率/音轨） |
| `d_sha256.txt` | D 项成片 sha256 |
| `D_film_2min_480p.mp4` | D 项成片副本（2 分钟 480P，27.2MB） |
| `report_part1.md` / `report_part2.md` | 本报告的分片源文件 |

上一轮遗留证据（本轮未删）：
- `.studio-acceptance/941b3742-4e53-4d54-8fe1-c139ff10f4f6/1/` —— A1 两组组合的 ComfyUI `/history` 原文、A2/A3/B2 浏览器截图、
  `b2_audio_corr.log`（音轨指纹 corr=+0.9998）、`interim_findings.md`（上一轮独立验收中间结论）、`defect_noaccel_steps.md`（AC-1 缺陷原始记录）

---

## 六、结论摘要

| 条目 | 三态结论 | 修复 commit |
|---|---|---|
| A1 | 已上线可用 | `d6a51fb` / `cd7f175`（AC-1：切换加速 LoRA 后重算采样档 + 显式表达「不挂」槽位） |
| A2 | 已上线可用 | — |
| A3 | 已上线可用 | — |
| A4 | 已上线可用 | — |
| A5 | 已上线可用 | — |
| B1 | 已上线可用 | — |
| B2 | 已上线可用 | — |
| C | 已上线可用（9 个有副作用工具未逐条实测） | — |
| D | 已上线可用（本轮独立 ffprobe 复核，未重复造片） | — |

**未完成项**：A1 本轮新任务的成片文件尚未落盘（任务仍在 ComfyUI 队列，params 已验证正确）——
见「已知限制」第 1 条。
