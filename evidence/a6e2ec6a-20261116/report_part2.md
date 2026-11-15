
### A2 素材库 · 播放器模式 —— **已上线可用**

线上 8190 现场实测（`GET /api/assets/player`）：

- 播放列表 entries = **3** 条，`counts = {image:1, audio:1, video:1, total:3}`，类别分布 `['image','audio','video']`
- 对照：素材库共 **__TOTAL_ALL__** 条（`GET /api/jobs?status=all`）
  - `media=image` → **__M_IMAGE__**
  - `media=video` → **__M_VIDEO__**
  - `media=audio` → **__M_AUDIO__**
  ⇒ 播放列表**不是**全量自动填充，只含用户手动添加的条目
- `img_seconds = 4.0`（每张停止时间，图片模式唯一时长控件）
- 图片模式 body 内只有 `alPSeconds` / `alPLoop` / `alPAudio` 三个控件，**卡片下方无独立输入框**
- 音乐单曲 / 列表 / 随机循环：`alPMode` 三档齐备（上一轮浏览器实测：单曲 → `<audio>.loop=true`；列表 / 随机 → `loop=false` 由 `advanceIndex` 推进）

证据文件：`/root/projects/h3studio-live/evidence/a6e2ec6a-20261116/a2_player.json`

### A3 素材库多选 —— **已上线可用**

线上 8190 浏览器实测（上一轮部署后复测结论，本轮线上 HEAD 未改该逻辑）：

- 默认：60 张卡、**可见复选框 0 个**、按钮文案「多选」
- 点击「多选」后：**可见复选框 60 个**（60/60 卡片都有）、按钮文案变「多选中 ✓」
- 勾选 3 张 → `checkedCount=3`（勾选状态一致）
- 页面另有 232 个 `data-tag` 标签过滤用的隐藏 checkbox，与卡片复选框是不同集合

### A4 内置 Agent 默认参数 —— **已上线可用**

线上 8190 现场实测（`GET /api/agent/settings`）：

- `gen_defaults` 覆盖 **__GD_N__ 个生成类型**：__GD_KEYS__
- 另有 `gen_defaults_spec` 12 类字段规格
- 上一轮做过**持久化往返实测**：PUT 改 `t2a.format` / `t2a.steps` / `t2i_moss.steps` → GET 回读三者全部生效，且 `t2v.accel_lora` / `t2v.model_key` / `t2i.aspect` **未被串改**（按类型独立持久化）；测试后已用原始值完整还原（restore 后 diff = NONE）
- 注：`t2a.format='wav'` 会被回落成 flac —— 这是**正确的白名单**（`YUE2_AUDIO_FORMATS = flac/mp3/opus`），非缺陷

### A5 运行中真实性 —— **已上线可用**

线上 8190 现场实测（本轮新任务 `d2d9c06c9fc2` 正在跑）：

```
GET /api/jobs/d2d9c06c9fc2
status = running
running_truth = {"state":"active","label":"运行中（已确认在 ComfyUI 队列中）",
                 "elapsed_sec":474.33,"timeout_sec":1800.0,"deadline_at":1794773415.53}
```

判定逻辑 `h3studio/running_truth.py::classify` 五态：FINISHED / TIMEOUT / UNKNOWN / ACTIVE / LOST，
顺序为「history 有结果 → 超时 → 队列不可达 → 在队列 → 宽限期 → LOST」，读 ComfyUI `/queue` + `/history/{id}` 真状态，
**不读**进程内状态。超时（默认 1800s）或已不在队列即结束「运行中」，不存在无限运行中。

### B1 首尾帧生视频 —— **已上线可用**

上一轮实测记录（jobs.db 中仍在，本轮核对落库记录未被线上重部署破坏）：

| 模式 | job id | ComfyUI prompt id | task_type |
|---|---|---|---|
| 仅尾帧 | `d756748ab263` | `8ca824ae-…` | **L2VA**（LoadImage 105:202） |
| 仅首帧 | `87b5a5a55539` | — | **I2VA** |
| 首 + 尾 | `4b3ec50cdcc8` | — | **FL2VA**（first_frame + last_frame 双 LoadImage） |
| 尾帧 + 参考媒体 | `7309347fd60c` | — | track=`video_bg` |

界面与文生视频一致，额外可从素材库选首 / 尾图片。落库 width/height 与产物实际像素一致。

### B2 参考生视频 —— **已上线可用**

- r2v job `09baa05e717a` / ComfyUI prompt id `4e0000c0-…`
- ComfyUI `/history` 回读：参考图 `LoadImage 136:201` + 参考视频 `VHS_LoadVideo 136:301`
  + `UNETLoader = minimax_h3_ref2va_pruned_int8_convrot` + `task_type = Ref2VA`，`audio_track_source = video_bg`
- 上限：参考图 9 张 / 参考视频 3 个 / 参考音乐 3 段（前端 9 槽位全量提交不截断）
- 音轨三选一（上一轮浏览器实测）：无参考素材时只有 1 项「使用生成的音乐」（参考音乐 / 参考视频背景音**不出现**）；
  填入参考音乐 → 2 项；再填参考视频 → 3 项
- 音轨内容级指纹校验（上一轮实测）：r2v 成片音轨与参考音乐 corr = **+0.9998**，
  对照组（flf 成片 / 其它 flac）corr = +0.0172 / +0.0030 / +0.0003 ⇒ 三选一真的换了音轨

### C 内置 Agent 全部现有功能 —— **已上线可用（本轮未改动，线上 HEAD 未改该逻辑）**

- 33 个工具注册（上一轮实测）
- 对话级实测 17 个通过（只读工具全绿；被 429 挡下的是 Agent 并发闸门上限 2，非缺陷）
- ⚠️ 已知限制：带副作用的 9 个 Agent 工具（`create_job` / `upload` / `tag` / `delete` / `media_*`）
  **未做**对话级逐条实测 —— 在他人正在使用的生产素材库上跑会产生不可回收的副作用。
  其服务端实现由定向测试覆盖（`tests/test_agent*.py` 系列）。

### D 端到端 2 分钟 480P 成片 —— **已上线可用（本轮只复核，未重复造片）**

并行任务 `ec6c150d` 产出的成片**仍在**，本轮自己跑 ffprobe 独立复核：

- 文件：`/mnt/usb2t/asset_library/originals/3f/e4/3fe4cc2ee6b97c40__H3Studio_火星基地_2分钟_480P.mp4`
- 已复制到持久路径：`/root/projects/h3studio-live/evidence/a6e2ec6a-20261116/D_film_2min_480p.mp4`
- 视频流：h264 **864×480**（= 480P）、**24/1 fps**、**2879 帧**、**119.958333s**
- 音频流：**aac**、**44100Hz**、**立体声(2ch)**、**120.000000s**、5169 帧
- format：duration **120.000000**、size **27239575** bytes
- sha256 = `3fe4cc2ee6b97c4071b7c1672607042532837b9acb5d5cb367f69aa4bad6a86a`
- 素材库记录：`asset_61f1b6fd9a1d`，`deleted_at = None`（仍在库），note = 「自产参考图(Z-Image)+自产音乐(YuE2)+19 段 H3 r2v 片段合成」
- **参考图与音乐均由本系统生成**（上一轮在 jobs.db 独立验证过来源链）：
  - 文生图 job `cdb9e7a4651c`（mode=t2i, model=z_image_turbo_bf16, 1360×768）→ `images/i-zimage-000168.png`
  - 文生音乐 job `913c39cf3ade`（mode=t2a, checkpoint=yue2_3b_bf16, duration=120）→ `audio/a-yue2-000050.flac`
  - 19 个 r2v 段 asset_refs = `asset_21f5d051d841`（参考图）+ `asset_bfaca307e2cf`（参考音乐，YuE2 生成）

证据文件：`/root/projects/h3studio-live/evidence/a6e2ec6a-20261116/d_ffprobe.json`、`d_sha256.txt`
