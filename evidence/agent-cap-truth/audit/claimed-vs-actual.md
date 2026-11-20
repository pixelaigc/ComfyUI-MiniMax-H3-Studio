# 目标 A 审计：「Agent 声称 vs 实际」对照清单

- 审计对象：`/root/projects/h3s-agent-cap`（分支 `local/feature/agent-cap-truth`，基线 commit `b9a6424`）
- 审计时间：2026-11-20
- 方法：静态阅读现场代码 + `python3 -c` 导入模块打印**运行时真实值** + 导入 `server.py` 实测 `normalize_t2v_params` / `prepare_job_params`
- 未采信任何历史摘要或旧文档；所有「实际」列都由现场代码/运行时取值确认

## 0. 术语

- **声称** = Agent 会说出口的能力陈述（系统提示词 / 工具描述 / 工具返回值）
- **实际** = 服务端真实能力（常量表 + 归一函数 + 工作流实际读取的 params 键）

---

## 1. 视频分辨率（用户直接指出的缺陷）

| # | 声称位置 | 声称内容 | 实际 | 判定 |
|---|---|---|---|---|
| 1.1 | `h3studio/agent.py:311`（SYSTEM_PROMPT） | 「分辨率**只有 544p / 768p**（传 1080p 会被静默落成 768p，所以不要传）」 | `video_models.VIDEO_MODE_SIZE_POLICY`（`video_models.py:1132-1200`）对 t2v/flf/r2v 均写明 `max_pixels_mp: 2.0`、`custom_size: True`；`size_presets_for_aspect` 给出 0.2~2.0 MP 共 14 档（16:9 下 = 352p…1088p，含 **480p = 864×480**）；`normalize_t2v_params`（`server.py:2884-2905`）接受 **任意 width/height**，按 32 倍数吸附，超 2.0MP 才报错 | **过时 + 错误**。真实支持自定义宽高与 2.0MP 内任意短边（288p~1440p），「只有两档」是旧面板 `#resSeg` 的遗留，被误当成生成侧硬约束（该误判已在 `video_models.py:1150-1180` 注释里被自己承认） |
| 1.2 | `h3studio/agent.py:732-733`（`list_modes` 返回 `resolution_note`） | 「只有 544p / 768p 两档。传其它值会被静默落成 768p，请不要传」 | 同上 | **过时 + 错误**，且这条文本会直接回传给模型当事实 |
| 1.3 | `h3studio/agent.py:1195`（`create_job` schema `resolution.description`） | 「分辨率档位。视频只有 544p / 768p；图像按族取各自档位」 | 同上 | **过时 + 错误** |
| 1.4 | `h3studio/agent.py:883`（`list_modes` 顶层 `video_resolutions`） | `"544p,768p"` | 真实可选档位远多于此 | **过时**（且 `RES_MP` 的 MP 值 0.4/0.8 与 `video_dimensions` 实际算出的 864×480 / 1216×672 语义不同，`544p` 实际对应 960×544） |
| 1.5 | `h3studio/agent.py:136`（代码注释） | 「视频只有 544p/768p」 | — | 注释过时（不影响运行时，但会误导后续维护） |
| 1.6 | `h3studio/agent.py:672` | 「面板滑块范围」 | 面板滑块已是 2~60 秒（`agent_schema.py:56-58` 注明 2026-11-12 放宽） | 措辞过时，但数值取自 `agent_schema.VIDEO_DURATION_MIN/MAX`=2/60，**数值正确** |
| 1.7 | `h3studio/agent_schema.py:65` | `VIDEO_RESOLUTIONS = tuple(wf.RES_MP.keys())` | 同 1.1 | **过时**：这是 Agent 侧「视频合法分辨率档位集合」的真源，却被旧白名单污染；`mode_resolutions("t2v")` 返回 `['544p','768p']` |
| 1.8 | `h3studio/agent_schema.py:920-921`（`mode_schema` 的 `source`） | `"workflows.RES_MP"` | 真实来源是 `video_models.VIDEO_MODE_SIZE_POLICY` + `size_presets_for_aspect` | **过时**（来源标注错误，误导模型与维护者） |
| 1.9 | `h3studio/agent_schema.py:1273-1284`（`validate_params` 的 resolution 硬校验） | 非 `mode_resolutions(mode)` 的值一律报 `invalid_resolution` | 真实可接受任意 2.0MP 内宽高 | **错误（会误杀合法请求）**：Agent 传 `resolution="480p"` 会被自己拦下 |
| 1.10 | `tests/test_agent_media_semantics_backend.py:219-221` | 断言 `result["video_resolutions"] == "544p,768p"` | — | **锁死了过时行为**，改真源必然打破该测试（需同步更新为「派生自真实策略」） |

**用户原话对应的根因**：Agent 说「视频分辨率只有 544p / 768p，请求 480p 实际按 768p 生成」—— 真实情况是
- Agent 自己把 `480p` 判成非法（`agent_schema.validate_params` → `invalid_resolution`），或
- 传了 `resolution` 但服务端 `normalize_t2v_params` 用 `width/height` 覆盖 → 卡片显示与实际像素脱节。

真实能力：**480p = 0.4MP**，16:9 下 **864×480**，完全在 2.0MP 上限内，可正常下发。

---

## 2. 视频时长

| # | 声称位置 | 声称 | 实际 | 判定 |
|---|---|---|---|---|
| 2.1 | `h3studio/agent.py:311` | 「视频：时长 5~15 秒」 | `video_models.VIDEO_DURATION_MIN/MAX = 2.0 / 60.0`（`video_models.py:689-690`）；`normalize_video_duration(100)` 实测返回错误文案「文生视频时长上限为 60 秒」（**拒绝**，不静默截断）；`normalize_video_duration(1)` → 2.0 | **过时**（数值取自常量因而「说错话」） |
| 2.2 | `h3studio/agent_schema.py:1442`（代码注释） | 「VIDEO_DURATION_MIN/MAX 就是 5/15」 | 实际是 2/60 | 注释过时 |
| 2.3 | `h3studio/agent.py:668-673`（`list_modes.duration_range`） | `[2, 60]` | 同 2.1 | 数值**正确**（取自常量） |
| 2.4 | `h3studio/agent.py:1190-1191`（`create_job` schema） | 「时长（秒），**仅视频模式**（5~15）」 | 2~60 | **过时**（硬编码字面量，非派生） |
| 2.5 | `h3studio/agent_schema.py:931-933`（`mode_schema` 的 `source`） | `"web/index.html#duration 滑块区间"` | 真实来源是 `video_models.VIDEO_DURATION_MIN/MAX` | **过时**（标注指向 HTML 而不是真源常量） |

---

## 3. 音乐时长

| # | 声称位置 | 声称 | 实际 | 判定 |
|---|---|---|---|---|
| 3.1 | `h3studio/agent.py:313-314`（SYSTEM_PROMPT） | 「音乐：时长字段是 **max_duration**（4~360 秒，默认 60）」 | `music_yue2.YUE2_MIN_DURATION=4.0` / `YUE2_MAX_DURATION=900.0` / `YUE2_DEFAULT_DURATION=900.0`（`music_yue2.py:189-191`）；`agent_schema.mode_duration_range("t2a")` 实测返回 **(4, 900)** | **错误**：上限与默认值都是错的（360→900，60→900） |
| 3.2 | `h3studio/agent.py:711`（`list_modes` 音乐 `duration_range`） | `[4, 360]` 硬编码 | 实际 (4, 900) | **错误** |
| 3.3 | `h3studio/agent.py:714-716`（`duration_note`） | 「范围 4~360，默认 60」 | 默认 900 | **错误** |
| 3.4 | `h3studio/agent.py:719`（`music_params.max_duration`） | 「4~360，默认 60」 | 同上 | **错误** |
| 3.5 | `h3studio/agent.py:881`（顶层 `music_duration_range`） | `[4, 360]` | (4, 900) | **错误** |
| 3.6 | `h3studio/agent.py:1192-1193`（`create_job` schema `max_duration`） | 「**仅音乐模式**（4~360，默认 60）」 | 4~900，默认 900 | **错误** |
| 3.7 | `h3studio/agent_schema.py:936` / `:1033`（`mode_schema` 的 `source`） | `"h3studio.music_yue2._clamp_duration"` | 真实函数名是 `music_yue2._resolve_duration`（`music_yue2.py:309`） | **错误**（引用了不存在的符号） |
| 3.8 | `h3studio/agent.py:312`（SYSTEM_PROMPT 谱面/格式） | `plan_mode`（full / melody）、`format` flac/mp3/opus | `MUSIC_PLAN_MODES`/`MUSIC_FORMATS` 实测与之一致 | 正确 |

---

## 4. 视频底模 / LoRA / 注意力 / 精度

| # | 声称位置 | 声称 | 实际 | 判定 |
|---|---|---|---|---|
| 4.1 | `h3studio/agent.py:311-312`（SYSTEM_PROMPT） | 「加速 LoRA 用 lora 参数（none / ckpt850 / v4step600）」 | `video_models.VIDEO_ACCEL_LORA_COMBOS` 实测 10 项（`lightx2v_4step_768p` / `lightx2v_4step_sla_768p` / `lightx2v_8step` / `dmad_4step_critic` / `speed_slider` / `better_motion` / `turbo_4step_cn` / `turbo_4step_cn_ema` / `lightx2v_4step_v01_comfy` / `lightx2v_sla_768p_root`）；`all_accel_lora_keys()` = `VIDEO_ACCEL_LORAS` + combos；`workflows.LORAS` = {ckpt850, v4step600} 是**旧面板路径**的集合 | **过时**：Agent 只知道旧路径的 3 档，看不到新面板的 10 个加速 LoRA（而 `agent_settings` 默认就是 `dmad_4step_critic`） |
| 4.2 | `h3studio/agent.py:737-741`（`list_modes.video_params.lora`） | 「加速 LoRA：none / ckpt850（默认，4 步）/ v4step600」 | 同上；线上 `/api/agent/settings` 的 t2v 默认 `accel_lora: dmad_4step_critic` | **过时 + 默认值错误** |
| 4.3 | `h3studio/agent.py:883`（顶层 `notes`） | 「视频 LoRA 只有 none,ckpt850,v4step600」 | 同上 | **过时** |
| 4.4 | `h3studio/agent.py:1198-1199`（`create_job` schema `lora`） | 「视频加速 LoRA（none / ckpt850 / v4step600）」 | 同上 | **过时** |
| 4.5 | `h3studio/agent.py:1200-1201`（`create_job` schema `precision`） | 「视频精度：int8（默认）/ nvfp4」 | `agent_schema.VIDEO_PRECISIONS` = `['int8']`（实测）；`nvfp4` 在新路径不适用 | **错误**（宣称了不存在的可选值） |
| 4.6 | 全局 | Agent 从未提及视频**底模** `model_key`（7 个：fl2va_pruned_int8 / fl2va_int8 / fl2va_pruned_w4a8 / fl2va_bf16 / ref2va_pruned_int8 / ref2va_int8 / hybrid_fl2va_ref2va） | `video_models.VIDEO_MODELS` 实测 7 项；`agent_settings` 默认 `fl2va_pruned_int8` | **缺失**：模型无法知道可换底模，也无法知道 r2v 必须用 Ref2VA |
| 4.7 | 全局 | Agent 从未提及**注意力机制**（default / kitchen / sage / sol / sparse 五种；`sol_scheduled` 已被移除） | `video_models.VIDEO_ATTENTION_MODES` 实测 5 项 + `VIDEO_REMOVED_ATTENTION_MODES` 1 项 | **缺失** |
| 4.8 | `h3studio/agent.py:1203-1204`（`create_job` schema `cfg`） | 「CFG。图像模式；Qwen 负面提示词需 cfg>1 才生效」 | 视频模式 `cfg` **固定 1.0 不可配**（`normalize_t2v_params` 不写 cfg，H3 标准路径 BasicGuider 无 cfg 槽位） | 基本正确（但视频 cfg 的不可配没说明） |
| 4.9 | `h3studio/agent.py:1214-1217`（`create_job` schema `negative_prompt`） | 「**只在 t2i_moss 与 t2i+Z-Image Base 生效**」 | `agent_schema.negative_support` 实测：Qwen 族 `supported=True` 但默认 cfg=1 需抬到 >1 才 `effective`；MOSS `effective=True`；Z-Image 逐模型不同 | **部分过时**：漏掉「Qwen 抬 cfg 后生效」这条（该信息在 `list_modes.negative_ok` 里但系统提示词说反了倾向） |

---

## 5. 图像模式

| # | 声称位置 | 声称 | 实际 | 判定 |
|---|---|---|---|---|
| 5.1 | `h3studio/agent.py:314-316` | 「图像：分辨率档位按族不同（t2i / t2i_qwen / t2i_moss 各有一套）。超上限时 t2i 会自动走 2K 放大路径，另两族没有（会直出）」 | 实测族清单为 **5 族**：z_image / qwen_image / moss_image / h3_image（`t2i_h3image` + `i2i_h3image`）。`mode_resolutions` 实测：t2i=[1mp,480p,720p,1080p,1440p,2k,4k]、qwen=[1mp…2k]、moss=[1mp…2k]、h3image=[4mp,8mp,16mp] | **过时**：漏了 H3-Image 族（4mp/8mp/16mp） |
| 5.2 | `h3studio/agent.py:733-741` 附近的 mode 条目 | 每个 mode 都给 `models`/`resolutions` | 实测**正确**（`wf.Z_IMAGE_MODELS` 等真实表） | 正确 |
| 5.3 | `h3studio/agent.py:1196-1197`（`create_job` schema `model`） | 「模型版本（图像模式）。合法值用 get_mode_schema 查」 | 正确 | 正确 |
| 5.4 | 全局 | 未提及图像模式**参考图上限** | `agent_schema.REF_IMAGE_MAX` 实测随 mode 不同（`_ref_image_range()` 派生）；i2i_h3image 官方 16 张、r2v 9 张、i2i_qwen 10 张 | **缺失**（`list_modes` 里 i2i_h3image 写了「最多 16 张」是硬编码字面量，不是派生） |
| 5.5 | `h3studio/agent_schema.py:960-966`（`mode_schema` batch_count） | `max: 4` | `server.MAX_BATCH_COUNT = 5`（`server.py:2770`） | **错误**（上限少 1） |

---

## 6. 画面比例

| # | 声称 | 实际 | 判定 |
|---|---|---|---|
| 6.1 | `h3studio/agent.py:310`「比例：8 个固定值 1:1 / 2:3 / 3:2 / 3:4 / 4:3 / 9:16 / 16:9 / 21:9」 | `wf.ASPECT_MAP` 实测正是这 8 个；`video_models.VIDEO_ASPECTS` 同 8 个 | **正确** |
| 6.2 | `h3studio/agent.py:310` 未说明音乐模式无比例 | `agent_schema.mode_aspects("t2a")` = `[]` | 正确（`list_modes` 已正确处理） |

---

## 7. mode 列表

| # | 声称位置 | 声称 | 实际 | 判定 |
|---|---|---|---|---|
| 7.1 | `h3studio/agent.py:644-650`（`list_modes` docstring） | 「模式白名单 = create_job 的白名单 = workflows.BUILDERS」 | `wf.BUILDERS` 实测 11 项：t2v / flf / r2v / t2i / t2i_qwen / i2i_qwen / t2i_moss / t2i_h3image / i2i_h3image / t2a / a2a；`server.JOB_MODES` = 同 11 项 | **正确** |
| 7.2 | SYSTEM_PROMPT **未列举任何 mode 名** | 模型只能靠 `list_modes` 工具才知道有哪些模式 | **缺失**：能力自述缺 mode 清单（用户问「支持哪些模式」时模型必须先调工具） |
| 7.3 | `h3studio/agent.py:658`（`gen_types.registered_modes()` 被刻意排除 longfilm） | 「longfilm 不在 BUILDERS 里，create_job 建不了」 | `server.JOB_MODES` 不含 longfilm，`gen_types.registered_modes()` 含 | 正确（刻意设计，有注释说明） |

---

## 8. 素材引用规则

| # | 声称位置 | 声称 | 实际 | 判定 |
|---|---|---|---|---|
| 8.1 | `h3studio/agent.py:319-320`（SYSTEM_PROMPT 素材槽位段） | 「参考图给 asset_id；首尾帧用 first_frame / last_frame；参考生音乐用 ref_audio（只能 1 段）。素材必须来自素材库」 | `agent_schema.MODE_SLOTS` / `MODE_MEDIA_SLOTS` 实测与之一致（r2v 额外吃 ref_video/ref_audio） | 正确，但**漏了 r2v 的 ref_video/ref_audio**（`list_modes` 里有，系统提示词里没有） |
| 8.2 | 全局 | 未提及「素材必须是**素材库里已入库**的素材」（`asset_ref_guard.check_asset_refs` 会查库并报「该媒体尚未入库素材库」） | `server.prepare_job_params` 首段就是 `check_asset_refs` | **缺失**（模型会传自己编的 id） |
| 8.3 | 全局 | 未提及 `params` 媒体通道的**绝对路径拦截**（`check_params_media_references`） | 同上 | 缺失但属安全侧，Agent 侧本就不该传路径 |
| 8.4 | `h3studio/agent.py:1145`（`ref_video` schema） | 「r2v 参考生视频模式用，最多 3 个」 | `agent_schema.REF_VIDEO_MAX = 3` | 正确 |

---

## 9. 编排约束（目标 D 前提）

| # | 项 | 现场真实值 | 位置 | 判定 |
|---|---|---|---|---|
| 9.1 | 最大步骤数 | `MAX_STEPS = 50` | `agent.py:126` | **已达标**（用户要求的「至少 50 步」已满足）。但 `agent_settings.MAX_STEPS_HARD_CAP = 50` 且线上配置 `max_steps: 50` |
| 9.2 | 最大工具调用数 | `MAX_TOOL_CALLS = 120` | `agent.py:135` | 充足 |
| 9.3 | 单步超时 | `STEP_TIMEOUT = compute_timeout(1024) + QUEUE_WAIT_ALLOWANCE` | `agent.py:110-113` | 由 llm 层派生 |
| 9.4 | 总超时 | `TOTAL_TIMEOUT = 2400.0`（40 分钟），可配范围 `[60, 14400]` | `agent.py:79`、`agent_settings.py:65-66` | 充足。**但** `server._resolve_total_timeout()` 读的是管理页配置，线上实测值需确认 |
| 9.5 | 重复调用检测 | `REPEAT_LIMIT = 3`：第 2 次同参给 nudge，第 3 次同参收尾 | `agent.py:150`、`agent.py:3994-4030` | 存在。**但** `repeat_signature` 是**全局单一变量**，跨工具比较 —— 工具 A 的参数签名与工具 B 的签名不会相等，所以实际是「连续同工具同参」判定，符合注释 |
| 9.6 | 无进展检测 | **不存在**。只有「同参重复」与「无工具调用」两类 | `agent.py` 全文 | **缺失**：模型若换参数反复搜索（如 search_assets 换 5 个关键词都没结果），可以合法地烧掉 50 步 |
| 9.7 | 「先反问再做」引导 | SYSTEM_PROMPT 有整节「## 关键参数缺失时（澄清式提问）」（`agent.py:299-306`），明确要求「**不要瞎猜，也不要硬着头皮做**」；`respond` 工具的 `needs_clarification`/`clarify_options`/`clarify_hint` 三个参数（`agent.py:1710-1730`） | `agent.py:299-306`、`1710-1730` | **这是「合并音视频没一气呵成」的直接诱因**：本地小模型看到「工具调用预算」+「关键参数缺失先反问」的指令，会把「哪条视频？哪条音频？」当成「关键参数缺失」而先反问。实测线上会话已有澄清气泡 |
| 9.8 | 工具调用预算的硬编码陈述 | SYSTEM_PROMPT:「工具调用预算是 8 次，全花在搜索上就没预算下发任务了」（`agent.py:293`） | `MAX_TOOL_CALLS = 120`、`MAX_STEPS = 50` | **过时**：数字早就是 8 的 15 倍，模型却被告知只有 8 次 → 严重抑制探索，倾向于早收尾/早反问 |
| 9.9 | 「同一工具不要连续重复调用超过 2 次」 | `REPEAT_LIMIT = 3`（第 3 次才收尾） | `agent.py:291` vs `agent.py:150` | 轻微不一致（提示说 2，代码容忍 3） |
| 9.10 | 工具结果裁剪 | `_clip(text, limit=2000)`：头尾保留，JSON 结构不散 | `agent.py:3438-3452` | 正确 |
| 9.11 | create_job 错误回传 | 服务端 `prepare_job_params` 抛错 → 转 `AgentError(message, code)` → 作为 tool 消息回给模型，模型看得到原因 | `agent.py:1662-1670` | **机制正常**。但**参数被服务端纠正时**（如宽高被缩放），`prepare_job_params` 只写 `params["size_adjust_reasons"]`，**不报错** → 模型看不到「被改成了什么」 |
| 9.12 | 归一后的 params 是否回传模型 | `params = normalized` 后进 `ctx.jobs[].params`，但工具返回值只有 `job_id/mode/status/label` | `agent.py:1690-1700` | **缺失**：模型建完任务**看不到实际生效的 width/height/duration**，无法向用户说明「你要的 480p 实际是 X×Y」 |

---

## 10. 媒体后处理能力（目标 C 前提）

| # | 项 | 现状 | 判定 |
|---|---|---|---|
| 10.1 | 已有工具 | `media_probe` / `media_concat` / `media_transcode` / `media_extract_frames` / `media_mix_audio` / `media_merge_av`（`agent_extra_tools.py:199-365`） | 已有 6 个 |
| 10.2 | 能力开关 | `media_tools` 默认 **True**，线上实测 `media_tools: true` | 不是开关问题 |
| 10.3 | 缺的能力 | **抽取音轨**（只取音频成独立音频文件）、**时长裁剪/片段截取**、**音量归一**（loudnorm） | **缺失**（目标 C 要求） |
| 10.4 | 「替换音轨」的语义 | `media_mix_audio(replace=True)` 实测把原音作为第 0 路一起进 `amix`（`normalize=0`）→ 是「混进去」不是「替换掉」 | **语义不符**：用户说「替换音轨」时，产物仍含原声 |
| 10.5 | 产物入库 | `_register_output` → `asset_service.register_asset`（去重/元数据/预览） | **已正确** |
| 10.6 | 输入闸门 | `_resolve_asset_path` 强制走素材库（asset_id → 素材库路径），不接受外部路径 | **已正确** |
| 10.7 | 失败处理 | `_run_ffmpeg` 返回 stderr 尾部 1200 字；`TemporaryDirectory` 自动清理；失败抛 `MediaToolError(code=...)` | 基本正确，但**没有产物大小>0 校验**、**没有产物 probe 复验**（子代理调研指出咏唱实现有） |

---

## 11. 结论（缺陷分级）

### P0（直接导致用户报的两个缺陷）
- **P0-1**：Agent 的能力自述（系统提示词 + list_modes + create_job schema）里分辨率/时长/LoRA/音乐时长全是**硬编码字面量**，与真实配置脱节。用户问「支持哪些分辨率」得到的是过时答案。涉及 `agent.py:293,311-316,672,732-733,883,1190-1201`。
- **P0-2**：系统提示词的「工具调用预算是 8 次」+「关键参数缺失先反问，不要硬着头皮做」两处，直接导致多步请求（合并音视频）**先反问 → 用户催促 → 才继续**，且探索预算被压制到不敢试。

### P1（能力自述不完整 / 与真源不一致）
- **P1-1**：视频底模 7 个、注意力机制 5 种、加速 LoRA 10 项从未出现在任何能力自述里。
- **P1-2**：`agent_schema.VIDEO_RESOLUTIONS`（544p/768p）是 Agent 侧校验真源，会**误杀**合法的 480p 等请求。
- **P1-3**：音乐时长上限 360→900、默认 60→900，4 处硬编码错误。
- **P1-4**：参数被服务端纠正后模型**看不到**纠正结果（无 `adjustments` 回传）。
- **P1-5**：素材引用规则缺「必须已入库素材库」「r2v 可吃视频/音频」。

### P2（次要 / 注释与来源标注）
- `agent_schema.py:936,1033` 引用不存在的 `music_yue2._clamp_duration`（真实是 `_resolve_duration`）。
- `agent_schema.py:960-966` batch_count max=4，实际 5。
- `agent_schema.py:1442` 注释「5/15」过时。
- `agent.py:136`、`agent_schema.py:920-921` 来源标注过时。
- `agent.py:291` 提示说「不要超过 2 次」，代码容忍 3 次。
- `tests/test_agent_media_semantics_backend.py:219-221` 锁死了过时的 `video_resolutions == "544p,768p"`。

---

## 12. 修复方向（对应目标 B/E）

1. 新建**单一真源模块**（如 `h3studio/agent_capability.py`），从 `video_models` / `workflows` / `agent_schema` / `music_yue2` 派生：
   - 每个 mode 的合法分辨率**表达方式**（档位字符串 + 自定义宽高 + 面积上限 + 32 对齐规则）
   - 视频时长区间（2~60）、音乐时长区间（4~900，默认 900）
   - 视频底模清单 / 注意力清单 / 加速 LoRA 清单 / 精度清单 / 采样器 / 调度器
   - 图像各族模型与档位、参考图上限、负面提示词矩阵
   - 素材槽位与上限
2. `SYSTEM_PROMPT` 改为**运行时拼装**（模板 + 真源渲染），删掉所有硬编码能力文本。
3. `list_modes` / `get_mode_schema` / `create_job` 的 schema 描述全部改为**从真源生成**。
4. `agent_schema.validate_params` 的 resolution 校验改为**接受真实合法表达**（含自定义宽高），不再误杀。
5. `create_job` 工具返回值增加 `adjustments`（服务端纠正项），让模型能向用户解释「已自动调整成 X」。
6. 编排侧：删除「8 次预算」硬编码与「先反问」强制引导，改为「能在一次对话内完成的多步请求直接做完；只有真正缺少且无法合理默认的关键输入才提问」，并补**无进展检测**。
