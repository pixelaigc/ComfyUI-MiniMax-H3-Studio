# P2 SelfLift 接续 —— 实现计划（2026-11-17）

## 已复核的结论（独立验证，非采信历史摘要）

### 结论 1：报告 §4.3.1 的「17k+5 网格对齐没问题」成立 ✅
独立复算（不加载权重，只读线上真实落盘的 safetensors 头）：
用项目自己的 `align_frame_count()` / `video_latent_t()` / `tail_aligned()`
扫 `data/jobs.db` 全部 done 任务里「二采 + 有落盘潜空间」的样本：

| job_id | mode | 尺寸 | 时长 | 帧格 fc | 期望 T | 实际 T | 吻合 | tail_aligned(fc,22) |
|---|---|---|---|---|---|---|---|---|
| 16ee031dc86c | t2v | 1920x1088 | 10.0 | 243 | 72 | 72 | ✅ | ✅ |
| 86e4205774c2 | t2v | 1920x1088 | 10.0 | 243 | 72 | 72 | ✅ | ✅ |
| 45ff07cc62f0 | t2v | 1280x736 | 5.0 | 124 | 37 | 37 | ✅ | ✅ |
| 12e2d1c606da | t2v | 1280x736 | 5.0 | 124 | 37 | 37 | ✅ | ✅ |

4/4 全部吻合 `video_latent_t(align_frame_count(dur*24))`，`tail_aligned(fc,22)` 全 True。
→ **不需要改 comfyui 的网格对齐**。

### 结论 2：SelfLift 原生支持拷贝桥的噪声掩码 ✅（关键，此前报告未覆盖）
`comfyui-SelfLift/nodes.py`：
- `_validate_latent_input()` (nodes.py:116-155) 读 `latent_image["noise_mask"]`，
  接受 `[B,1,T,H,W]`，并按 latent 网格 resize → 与 CopyBridge 输出的
  `vmask`（relay_core.py:2864-2876，shape `[B,1,total_t,H,W]`）**同构**。
- `masked_model()` (nodes.py:280-297) 对**低分辨率段与高分辨率段都**装
  `_mask_blend_fn` post-CFG 钩子 → 钉住区零重绘在二采的**两段**都成立。
- `nodes.py:444-445` 保留区用 `video_anchor`（未抬升的真实内容）还原 ——
  拷贝区是逐位拷贝内容，不是抬升结果。
→ **SelfLift 消费拷贝桥产物是可行的接法**（与 nonebot v1 的
`171.latent_image <- ["322",0]`、`171.positive <- ["322",3]` 同姿势）。

### 结论 3：必须改的 3 处（H3-Studio 侧），不需要改 comfyui
1. `h3studio/continuation.py:313-315`：selflift 闸门。
2. `h3studio/video_workflow.py:492` 与 `h3studio/h3_video_modes.py:425`：
   `not use_twopass` 条件 —— 二采路径下接续分支被整体跳过。
3. `_twopass_block()` 的 `positive` 来源：必须从
   `[cond_node_id, 0]` 改成桥第 4 路 `["300:bridge", 3]`（nonebot v1 姿势），
   否则二采采样器拿不到拷贝桥钉过的 conditioning。

## 改动清单（计划）

### A. `h3studio/continuation.py`
- `continuation_allowed()`：selflift 不再无条件拒绝。
  改为**能力探测**：只有当「上游二采段有落盘潜空间」时允许；
  原因文案要具体（说清是哪一种不满足）。
- 新增 `selflift_continuation_reason()` 之类，产出**具体中文**。

### B. `h3studio/video_workflow.py`（t2v 二采接续）
- 放开 `492` 的 `not use_twopass`。
- `_twopass_block()` 增加 `cond_ref` 形参：接续段用
  `["300:bridge", 3]`，非接续段用 `[cond_node_id, 0]`。
- `300:save.latent` 保持 `samples_ref`（= `["171",0]`，采样结果），
  **不**照抄 nonebot v1 的 `["322",0]`。

### C. `h3studio/h3_video_modes.py`（flf/r2v 二采接续）
- 同 B。条件节点 id 是 `136`。

### D. `server.py`
- `_prepare_continuation_params()`：上游二采 → 现在允许；
  但要校验**上游二采段的潜空间确实在**（且分辨率一致）。
- 二采段的 `300:save` 落盘坐标：二采段本来就不建链
  （`server.py:3689` 的注释说「二采照样建链、照样落盘」—— 需核实）。

### E. 音频续接时序（P1）对 selflift 同样适用
- selflift + 参考音频 → 段音频切片 + 1s 空白（采样用）、
  成片用原始整条。**复用 P1 已实现的 `_inherit_audio_continuation` /
  `_prepare_audio_continuation`**（因为它们按链首段的音轨决策继承）。

### F. 前端
- 「接续」按钮对 selflift 生效 → 服务端 `can_continue` 为真即可
  （前端已完全听服务端）。
- 回填：二采开关**必须回填为开**（否则续接段变普通采样 = 静默退化）。
  → `web/continuation.js` 的 `applyToPanel` 已回填 `twopass: params.twopass`，
  需核实 VIDEO_PANEL.applyConfig 是否真的把 twopass.enabled 应用回去。

## 必须实测的验收项（§4.3.2 判别法）
- 位移指标：接缝前后帧间差 vs 段内平均帧间差。
- 模糊指标：钉住区锐度（拉普拉斯方差）vs 新生成区锐度。
- 对照组：同参数非二采。
- 结论若为「清晰度台阶」→ 调 context_frames（22→39/56）或 mask_mode
  （hard→ramp），**仍不需要改 comfyui**。

## 边界（必须明确拒绝，不静默退化）
- 上游二采段**没有落盘潜空间** → 明确中文报错。
- 二采段分辨率与下一段不一致 → 明确中文报错。
- 二采的 `highres_tiling=true` + noise_mask → SelfLift 节点会 raise
  （nodes.py:244-245）→ 服务端要在提交前拦下并给中文提示。
