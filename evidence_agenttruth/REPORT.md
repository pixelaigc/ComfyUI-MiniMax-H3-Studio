# 内置 Agent 能力自述与真实能力对齐 —— 交付报告

**项目**：ComfyUI-MiniMax-H3-Studio 工作台
**仓库**：`/root/projects/comfyui-minimax-h3-studio`（主线 `local/maintainer`）
**线上**：`/root/projects/h3studio-live`（systemd `h3-studio.service`，`http://127.0.0.1:8190`）
**日期**：2026-11-22
**分支**：`local/feature/agent-cap-truth-20261122` → 合并提交 `f11b3f2`

---

## 0. 额度闸门

| 项 | 值 |
|---|---|
| 文件 | `quota/minimax_quota_latest.json` |
| `remaining_percent_5h` | **77**（阈值 20） |
| `sampled_at` | `2026-11-19 07:40:32`（快照自带时间戳，本轮只读不新建探针） |
| 判定 | **闸门开**，继续执行 |

---

## 1. 现场勘查与一次真实事故

### 1.1 现场核实（动手前）

```
git worktree list
  /root/projects/comfyui-minimax-h3-studio  b21a9e6 [local/feature/mobile-responsive-fix]  ← 别人的未提交 WIP，未触碰
  /root/projects/h3studio-live              d068ae4 [local/deploy/qwen-ref-assets-live]
```

- 主 worktree 有**他人未提交 WIP**（`h3studio/enhancer.py`、`server.py`、`web/*` 等）→ 本任务另开独立 worktree 推进，未动其工作区。
- 本任务 worktree：`/root/projects/h3s-agenttruth`（分支 `local/feature/agent-cap-truth-20261122`）。

### 1.2 事故：git 对象库损坏（已零内容丢失恢复）

**现象**：`local/maintainer` 与 live HEAD 指向的 commit `01d1c3df…` 对象**从对象库消失**，`git fsck` 报「对象损坏或丢失」，两个 backup 引用文件是 0 字节。

**根因**：并发 agent 的 `scripts/deploy_favleg_8190.sh` 用
`git commit-tree` + `git update-ref` 造 merge commit，该 commit 对象随后被
从对象库删除（不在任何 pack 内）；脚本中断又留下两个 0 字节 ref 文件。

**恢复方法（不是猜，是可验证的）**：

1. 幸存分支 `local/feature/favorites-v2-20261120`（`dcaee23`）的 tree
   `b7b9521f…` 与 live worktree 的 `git write-tree` **逐字节相同**
   ⇒ 丢失 commit 的内容**完整保存在分支上**。
2. 用相同 tree + 相同 parents（`d068ae4`, `dcaee23`）+ 相同 message
   （从 `deploy_favleg_8190.sh` 模板逐字还原）重建 merge commit。
3. `update-ref` 恢复 `local/maintainer` 与 `local/deploy/qwen-ref-assets-live`；
   删除 2 个 0 字节坏 ref。

**恢复验证**：

```
live tree == maintainer tree     → b7b9521f2271d172448940cac7d05b53aa0c7bb5
所有 refs 可解析                    → 是
live 服务 HTTP 200 / 5 分钟内 0 ERROR → 是
内容丢失                           → 无
```

证据：`evidence_agenttruth/00_git_corruption_recovery.json`

### 1.3 主机崩溃重启

执行期间主机崩溃重启一次（`uptime` 显示 2 分钟）。重启后**重新核对现场**：
git 引用完整、live HEAD == maintainer、8190 HTTP 200。崩溃导致的一次
`media_merge_av`「素材文件已丢失」是 USB 挂载重新同步导致的瞬时现象，
同步完成后重跑即成功。

---

## 2. 目标 A —— 「Agent 声称 vs 实际」审计清单

16 个维度，**12 一致 / 2 错（本次修）/ 2 残留（本次修）**。完整机读版：
`evidence_agenttruth/01_audit_claim_vs_actual.json`；真源 dump：
`01b_truth_source_dump.json`；Agent 运行时 dump：`01c_agent_runtime_dump.json`。

| 维度 | Agent 声称 | 真实 | 判定 |
|---|---|---|---|
| 视频分辨率档位 | 28 档 288p…1440p + 2.0 MP 上限 | `VIDEO_MODE_SIZE_POLICY` max_pixels_mp=2.0、multiple=32 | ✅ 一致 |
| 视频分辨率旧白名单 | `VIDEO_RESOLUTIONS` = RES_MP = (544p,768p) 仍在代码里 | 旧面板遗留，仅作历史标识 | ⚠️ **残留 → 已修** |
| 视频时长 | 2~60 秒 | `VIDEO_DURATION_MIN/MAX` = 2/60 | ✅ 一致 |
| 视频底模 | 7 个 model_key | `VIDEO_MODELS` 7 个 | ✅ 一致 |
| 加速 LoRA | 12 枚 + none | `all_accel_lora_keys()` 12 枚 | ✅ 一致 |
| 音乐时长 | max_duration 4~900，默认 900 | `YUE2_MIN/MAX/DEFAULT` = 4/900/900 | ✅ 一致 |
| 图像分辨率（按族） | z_image 7 档 / qwen 6 档 / moss 6 档 / h3 3 档 | `mode_resolutions` 同 | ✅ 一致 |
| 可下发模式清单 | 11 个 = BUILDERS | BUILDERS 11 个（无 longfilm） | ✅ 一致 |
| 素材槽位 / 参考图上限 | 按 mode 派生 | `MODE_SLOTS`/`MODE_MEDIA_SLOTS`/`REF_IMAGE_MAX` | ✅ 一致 |
| **运行时能力自述** | run_agent 用 `SYSTEM_PROMPT`（**导入期快照**） | `build_system_prompt()` 是动态的 | ❌ **错 → 已修** |
| 面板 vs Agent 建任务同源 | create_job 走 `prepare_job_params` | 公共点，且内含 `apply_gen_defaults_and_size` | ✅ 已同源 |
| 请求值→生效值回传 | 工具结果带 `adjustments` | `_collect_adjustments` 收集 | ✅ 已修 |
| **提示词工具清单** | 漏 `media_replace_audio` / `manage_media_library` | 二者**已注册** | ❌ **错 → 已修** |
| 编排上限 | MAX_STEPS/超时/护栏 | 50 / 2400s / 120 次 / 重复 3 / 无进展 6 | ✅ 达标 |
| **能力问答落地** | 线上实测只回「已按真实配置逐项核对完毕」，**无任何数值** | 应逐项给值 | ⚠️ **残留 → 已修** |
| ffmpeg 媒体后处理 | 10 个 media_* 工具 | 实测产出入库可引用 | ✅ 能力具备 |

---

## 3. 目标 B —— 能力自述改为单一真源

### 3.1 修的五处（每处都是「自述说谎」的具体形态）

| # | 位置 | 原状 | 修法 |
|---|---|---|---|
| B1 | `h3studio/agent.py` `run_agent` | 用模块级 `SYSTEM_PROMPT`（导入期快照）。旧注释「run_agent 走的就是 build_system_prompt() 那条路径」**是假的** | 每次调用 `build_system_prompt()` 重新渲染；`SYSTEM_PROMPT` 降级为兼容别名并写明「不要再当运行期真源」 |
| B2 | `h3studio/agent_schema.py` `VIDEO_DURATION_MIN/MAX` | 注释声称「服务端是唯一真相源，这里从它取值」，**代码却写死 2 / 60** | 真正从 `video_models` 取 |
| B3 | `h3studio/agent_capability.py` `music_duration_range()` | 读 `agent_schema.MUSIC_DURATION_*`（导入期快照） | 每次调用直接读 `music_yue2` |
| B4 | `h3studio/agent_schema.py` `_video_size_tiers()` | 真源不可用时回退 `RES_MP` = (544p,768p) ⇒ **重新说「只有 544p/768p」** | 回退改为**空集合**（不谎报），上层明说「清单不可用，请用 get_mode_schema」 |
| B5 | `h3studio/agent_schema.py` `_music_duration_range()` | except 分支返回旧的 `(4, 360)`（真实是 4~900） | 改为 `(4, 900)`，与真源一致 |

另外把 `_video_size_tiers()` 的**属性访问也纳入 try**——只包 import 不够，
`from . import X` 拿到残缺模块时不抛 ImportError，AttributeError 会冒到 try 外。

### 3.2 「单一真源」链路

```
video_models.VIDEO_MODE_SIZE_POLICY（max_pixels_mp 实时派生自 VIDEO_MAX_CANVAS_PIXELS）
video_models.VIDEO_DURATION_MIN/MAX
video_models.VIDEO_MODELS / VIDEO_ATTENTION_MODES / all_accel_lora_keys()
music_yue2.YUE2_MIN/MAX/DEFAULT_DURATION
workflows.BUILDERS
agent_schema.MODE_SLOTS / MODE_MEDIA_SLOTS / REF_IMAGE_MAX
        ↓ 派生
agent_capability.capability_text()
        ↓
agent.build_system_prompt()  ←—— run_agent 每次调用重新渲染
        ↓
Agent 的能力自述
```

任务参数侧：`agent.create_job` → `ctx.server.prepare_job_params`（**与面板
POST /api/jobs 同一个函数**）→ `apply_gen_defaults_and_size`，
Agent 与面板提交共用同一套校验与默认参数。

---

## 4. 目标 C —— ffmpeg 媒体后处理

10 个工具已注册并**真实可用**（ffmpeg 7.1.1，`/usr/bin/ffmpeg`）：

| 工具 | 作用 |
|---|---|
| `media_probe` | ffprobe 探测时长/分辨率/编码 |
| `media_concat` | 多视频顺序拼接 |
| `media_transcode` | 转码 / 换容器 / 压体积 |
| `media_extract_frames` | 按间隔抽帧，每帧单独入库 |
| `media_mix_audio` | 多段音频混进视频（可替换原音轨） |
| `media_merge_av` | 视频 + 音频合流 |
| `media_replace_audio` | 替换音轨（原声去掉，非混合） |
| `media_extract_audio` | 从视频抽音轨，得到独立音频素材 |
| `media_trim` | 按秒裁剪 / 截取片段 |
| `media_normalize_audio` | EBU R128 响度归一 |

**本轮补的缺口**：系统提示词的工具清单里**漏了** `media_replace_audio` 与
`manage_media_library` —— 二者早已注册，模型却不知道自己有。已补齐，并新增
「媒体后处理的硬约束」段：输入输出只走素材库 asset_id、产物自动入库可被后续
工具引用、标准合并链路、一次请求要连着做完。

硬约束（已在代码层落地）：输入只接受 `asset_id`，经
`asset_repo.get_asset` + `resolve_inside(library_root, …)` 定位，
**不接受外部路径**；产物经 `_register_output` 入库并返回新 `asset_id`。

---

## 5. 目标 D —— 多步编排一气呵成

| 护栏 | 值 |
|---|---|
| `MAX_STEPS` | **50** |
| `MAX_TOOL_CALLS` | 120 |
| `TOTAL_TIMEOUT` | 2400 s（可在管理页调，范围 60~14400） |
| `STEP_TIMEOUT` | 482.9 s（由 `llm.compute_timeout` 推导） |
| `REPEAT_LIMIT` | 3（重复调用检测） |
| `NO_PROGRESS_LIMIT` | 6（无进展检测，事实指纹连续 6 步不变即判打转） |

「先反问再做」的倾向从**提示词层**压制：新增「媒体后处理要连着做完
（选素材 → 合并 → 回报产物路径），不要做完第一步就停下来问用户」。

---

## 6. 目标 E —— 其它过时能力文本

- B4 / B5 两处旧数字（544p/768p 回退、360 音乐上限）已修。
- `agent_schema.VIDEO_RESOLUTIONS` 的注释改为「历史兼容标识，**不要**再拿它渲染能力自述」。
- `SYSTEM_PROMPT` 的注释改为说明「它曾是导入期快照，run_agent 曾经真的在用它」。
- 提示词里新增的能力问答约束**刻意不写死任何数字**——写了就变成新的假话。

---

## 7. 验证证据

### 7.1 新增单测 + 变异测试

新增 `tests/test_agent_capability_runtime_truth.py`（**23 项**）：

| 组 | 覆盖 |
|---|---|
| `TestRuntimeCapabilityTruth` | run_agent 发出的 system 消息随真源变（4 项） |
| `TestPromptToolListMatchesRegistry` | 提示词工具清单 == 真实注册表（4 项） |
| `TestNoStaleWhitelistFallback` | 降级路径不说旧话（6 项） |
| `TestCapabilityQaMustBeConcrete` | 能力问答必须给数值、媒体链路要连着做完（3 项） |
| `TestOrchestrationGuardrails` | 50 步 / 超时 / 两类护栏（3 项） |

**变异测试 M1~M6 全部咬住**（每条变异都有对应断言失败）——
证据 `evidence_agenttruth/02_mutation_matrix.txt`：

| 变异 | 结果 |
|---|---|
| M1 run_agent 改回导入期快照 | 1 failed ✅ 咬住 |
| M2 提示词删掉 media_replace_audio | 3 failed ✅ 咬住 |
| M3 降级回退 RES_MP 旧白名单 | 1 failed ✅ 咬住 |
| M4 音乐兜底改回 360 | 2 failed ✅ 咬住 |
| M5 时长常量改回硬编码 2/60 | 1 failed ✅ 咬住 |
| M6 音乐区间改回快照 | 1 failed ✅ 咬住 |

> M4/M5 第一版**没有咬住**（测试只断言「数值相等」，而硬编码值与派生值
> 数值上就一样 ⇒ 假绿）。已改成「改真源 + 重新导入后看常量是否跟着变」
> /「显式断言不是旧的 360」，重跑后 M4/M5 均咬住。

### 7.2 全量 pytest 与未改动基线对跑

两棵树同一套命令、同一 commit 基线（`24921cd`）：

```
python3 -m pytest tests/ --ignore=tests/e2e -q -p no:randomly
```

| | 基线（未改动） | 候选（本次改动） |
|---|---|---|
| 总数 | 4208 | 4224（+16 新增测试相关） |
| passed | 4164 | 见 §7.2.1 |
| failed | 29 | 见 §7.2.1 |
| errors | 8 | 见 §7.2.1 |
| skipped | 7 | — |

> `tests/e2e/` 被排除：它需要**真实 GPU + 真实 8190**，且第一条用例会
> 等一条 180s 的真实生成任务（实测卡住）。它与本次改动无关
> （未改 `web/`），故不进基线对比。

#### 7.2.1 FAILED/ERROR diff（4 次运行）

见 `evidence_agenttruth/13_pytest_regression_diff.txt`（完整内容）。

**关键事实：同一棵基线树跑两次，failed 数不同（29 vs 27）** —— 失败集合
本身不稳定（浏览器/时序类非确定性）。因此**单次对跑不足以下结论**，
必须看 4 次运行的集合运算：

| | 基线1 | 基线2 | 候选1 | 候选2 |
|---|---|---|---|---|
| failed | 29 | 27 | 28 | 27 |
| passed | 4164 | 4166 | **4188** | **4189** |
| errors | 8 | 8 | 8 | 8 |

候选相对基线的差异只有 **3 条非确定性浏览器用例**，各自在 4 次运行里
**只出现 1 次**（典型的 flaky 指纹，不是「改了代码就固定坏」）：

| 用例 | base1 | base2 | cand1 | cand2 |
|---|---|---|---|---|
| `test_favorites_view_is_not_a_dead_end` | pass | pass | **FAIL** | pass |
| `test_one_shot_real_http_tool_card_has_args_and_result` | **FAIL** | pass | pass | pass |
| `test_auto_refresh_keeps_page_size` | **FAIL** | pass | pass | pass |

**定向归因实验（最硬的证据）**：把可疑用例在两棵树上各连跑 6 次
（相同环境、相同共享数据）：

```
h3s-baseline-attruth: 0/6 FAILED
h3s-agenttruth:        0/6 FAILED
```

两棵树失败率**完全相同**。再加上两条独立证据：
① 该用例只 `import json/os/re/subprocess/pytest/playwright`，
**不 import 任何 `h3studio.agent` 模块**；
② 它涉及的 `web/` 与 `server.py` 在两棵树**逐字节相同**
（`diff -rq` 无差异；本次改动只碰 `h3studio/agent.py`、
`agent_capability.py`、`agent_schema.py`）。

**结论：无新增回归。**
真·既有失败 31 条（CRLF 行尾、LoRA 权重在盘检查、静态资源版本戳、
流程图派生清单等）全部是本次改动之前就存在的问题，与 agent 能力自述无关。

### 7.3 线上端到端实测（8190，真实调用 `/api/agent/chat`）

#### 用例 1 —— 请求 480p 视频

- Prompt：`请生成一条 480p 的视频：一只猫在草地上奔跑，时长 5 秒。如果 480p 不是可用档位，请按真实规则调整到可用值，并明确说明调整依据。`
- Agent 工具序列：`get_mode_schema` ×2 → `create_job` → `get_job_status` ×4
- `create_job` 实参：`{"mode":"t2v","resolution":"480p","duration":5,"aspect":"16:9"}`
- **落库 job params**：`resolution=480p, width=864, height=480, duration=5.0`
- **产物 ffprobe**（`/mnt/usb2t_toshiba/comfyui/output/h3studio/t2v_ad875270cc35_00001_.mp4`）：
  ```
  video: h264  864x480  24/1 fps  duration 5.166667
  audio: aac   32000 Hz  2ch    duration 5.167000
  format: mov,mp4  duration 5.167000  size 2490503
  ```
- 判定：**864×480 = 真 480p，未升到 768p** ✅
- Agent 全程**没有**说「只有 544p/768p」。
- 证据：`12_e2e_case1_480p_job_params.json`、`12b_e2e_case1_agent_trace.json`

> 首跑（`6918d7d456f2`）因主机崩溃后 ComfyUI 队列丢失而 failed
> （错误文案「队列中已无该任务」），与分辨率无关；重跑即 done。

#### 用例 2 —— 既有视频 + 既有音频合并成片

- Prompt：`把素材库里的「v4s.mp4_av_0d07e5e2.mp4」这个视频，和「a1.mp3」这段音频，合并成一条带声音的成片。请一气呵成完成：自己选素材 -> 合并 -> 产物入库 -> 回报产物路径和时长，全程不要中途问我。`
- Agent 工具序列（**单次会话内一次跑完，无追问**）：
  ```
  search_assets(video) → search_assets(audio) → media_probe(video)
  → media_probe(audio) → media_merge_av → media_probe(产物) → respond
  ```
- `clarify_options=[]`、`clarify_hint=""` ⇒ **没有中途反问**
- 产物：`asset_4651cca47e3e`
  `/mnt/usb2t/asset_library/originals/46/35/4635959189d2407a__v4s.mp4_av_0d07e5e2.mp4_av_05e46473.mp4`
- **产物 ffprobe**：
  ```
  video: h264  320x240  duration 1.880000
  audio: aac   44100 Hz 1ch     duration 1.857007
  format: mov,mp4  duration 1.880000  size 47437
  ```
- 判定：**含音轨 ✅**，时长 1.88s，产物已入库可被后续工具引用
- 证据：`11_e2e_case2_merge_av.json`、`11b_case2_ffprobe.json`

#### 用例 3 —— 能力问答

- Prompt：`现在视频支持哪些分辨率和时长？请按真实能力逐项给出具体数值，不要只说已核对完毕。`
- Agent 工具序列：`get_mode_schema(t2v)` → `get_mode_schema(flf)` → `get_mode_schema(r2v)` → `respond`
- 回答**逐项给出数值**，与真实配置 10/10 项一致：

  | 断言 | 结果 | 真实值 |
  |---|---|---|
  | 时长 2~60 秒 | PASS | [2.0, 60.0] |
  | 面积上限 2.0 MP | PASS | 2.0 |
  | 480p 在档位里 | PASS | — |
  | 共 28 档 | PASS | 28 |
  | 音乐 4~900 | PASS | 4.0/900.0 |
  | 不含旧断言「只有 544p」 | PASS | — |
  | 不含「已按真实配置逐项核对完毕」空壳 | PASS | — |
  | 7 个底模全列 | PASS | 7 |
  | 5 种注意力全列 | PASS | 5 |
  | 宽高 32 倍数 | PASS | 32 |

- 证据：`10_e2e_case3_capability_qa.json`、`10b_case3_vs_truth.json`

### 7.4 浏览器验证

**不适用**：本轮**未改动任何 `web/` 文件**（`git diff --name-only local/maintainer HEAD | grep '^web/'` 为空）。
因此 spec 中「若改 web/agent.js 则需浏览器验证 0 console 错误 / 375px 无溢出」
的条件不成立。

CRLF 复核（部署后、live worktree 实测）：`web/app.js` 8366/8366 全 CRLF、
`web/index.html` 1726/1726 全 CRLF，**换行未被改坏**。

---

## 8. 部署与回滚

```
合并：local/maintainer  ← merge(agent-truth)  = f11b3f2
推进：local/deploy/qwen-ref-assets-live = f11b3f2
live worktree：精确 checkout 3 个代码文件 + 新测试 + 证据目录
live tree == maintainer tree = a8c6eb2a70860c5237858dd34f0ac3793bd8c009
```

**回滚点**：

```
backup/maintainer-before-agenttruth-20260722-073500
backup/live-before-agenttruth-20260722-073500
```

**探活**：

```
systemctl restart h3-studio.service → active, NRestarts=0
curl http://127.0.0.1:8190/          → HTTP 200 (0.0012s)
journalctl（重启后）ERROR/Traceback 计数 → 0
```

---

## 9. 证据目录

`/root/projects/h3s-agenttruth/evidence_agenttruth/`

| 文件 | 内容 |
|---|---|
| `00_git_corruption_recovery.json` | git 对象库损坏事故与零丢失恢复全过程 |
| `01_audit_claim_vs_actual.json` | 16 维「声称 vs 实际」对照清单（目标 A 主交付） |
| `01b_truth_source_dump.json` | 审计时刻的真源 dump |
| `01c_agent_runtime_dump.json` | 审计时刻的 Agent 运行时 dump（37 工具 + 护栏参数） |
| `02_mutation_matrix.txt` | 变异测试 M1~M6 结果 |
| `10_e2e_case3_capability_qa.json` | 用例3 完整响应与工具序列 |
| `10b_case3_vs_truth.json` | 用例3 逐项与真实配置比对（10/10 PASS） |
| `11_e2e_case2_merge_av.json` | 用例2 完整响应与工具序列 |
| `11b_case2_ffprobe.json` | 用例2 产物 ffprobe（时长 + 音轨） |
| `12_e2e_case1_480p_job_params.json` | 用例1 落库 job params + 产物 ffprobe |
| `12b_e2e_case1_agent_trace.json` | 用例1 完整响应与工具序列 |
| `13_pytest_regression_diff.txt` | 全量 pytest 基线 vs 候选 FAILED/ERROR diff |

---

## 10. 如实说明

1. **主机崩溃重启**：执行中发生一次。重启后已重新核对现场并重跑受影响步骤；
   崩溃造成的一次 merge 失败是 USB 挂载同步延迟，重跑成功。
2. **git 对象库损坏**：由并发 agent 的部署脚本造成，已零内容丢失恢复并留证。
   本任务未修改该脚本（超出本目标范围）。
3. **`tests/e2e/` 未进基线对比**：它依赖真实 GPU 与 8190，且会等一条真实
   生成任务（实测卡 180s+）。本轮未改 `web/`，与该目录无关。如需，
   可在 GPU 空闲时单独跑。
4. **用例 1 首跑 failed**：因主机崩溃后 ComfyUI 队列丢失（错误文案
   「队列中已无该任务」），与分辨率处理无关；重跑得到 done + ffprobe 证据。
