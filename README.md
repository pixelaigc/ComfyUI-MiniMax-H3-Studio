# H3 Studio — 本地视频生成工作台

仿 [minimax-h3-studio](https://github.com/zxcHolmes/minimax-h3-studio) 的本地 Web 工作台，驱动本机 ComfyUI 出片。左侧选模式 → 中间调参数 → 右侧看任务卡片流，队列自动 FIFO 执行。零额外依赖：直接复用 ComfyUI 自带的 Python 运行。

> 仓库：https://github.com/pixelaigc/h3-studio · License: MIT

## 界面

![H3 Studio v1.00 — 主界面：左侧模式与参数面板，右侧已完成任务卡片流](docs/images/h3-studio-v1.00.png)

*H3 Studio v1.00 实机截图（作者本地生产环境）。左：模式与参数面板；右：已完成任务卡片流（此页为文生视频作品），可直接播放、下载、复用参数或删除。*

截图里可以看到：

- **顶栏**：`PixelAIGC MiniMax-H3 Studio v1.00`，右侧状态点显示「空闲」
- **任务区**：进行中 / 已完成 / 长片 / 失败 / 已取消分页，可按提示词与任务 ID 搜索，并按类型、比例、时长筛选

- **顶栏**：右侧实时显示「Generating, N 排队 / 空闲 / ComfyUI 未连接」
- **左侧栏**：视频（文生视频 / 首尾帧 / 参考生成 / 长片拼接）· 图像（Z-IMAGE 文生图）· 系统（硬件参数）· 释放显存
- **参数面板**：分辨率 544p/768p、模型卡（H3+LoRA / 标准 H3）、LoRA 选择与强度、步数 4~20、画面比例、时长 5~15s、种子
- **任务卡片**：排队/生成中进度条（与 ComfyUI 逐节点/逐采样步实时同步）、可播放预览、Cancel、下载、复用参数、删除
- **硬件参数页**：GPU（显存/利用率/温度/功耗/驱动）、CPU、内存、磁盘、软件环境（ComfyUI/PyTorch/Python/OS）、模型库分类统计，3 秒自动刷新

## 模式与模型映射

| 模式 | 工作流节点 | 说明 |
|---|---|---|
| 文生视频 t2v | `MiniMaxH3ImageToVideo` | 纯提示词，带原生音频 |
| 首尾帧 flf | `MiniMaxH3ImageToVideo` | first_frame / last_frame（尾帧可省略=图生视频） |
| 参考生成 r2v | `MiniMaxH3ReferenceToVideo` | 1~3 张参考图，主体一致性 |
| 文生图 t2i | Z-IMAGE-turbo | 8 步出图 |

**模型档位**（需已就位 MiniMax-H3 权重，见下方「模型准备」）：

| 选项 | unet | clip | 步数 |
|---|---|---|---|
| H3 + LoRA（ckpt850 / v4-step600） | `minimax_h3_*_pruned_int8_convrot` 或 nvfp4 | qwen3vl（随精度切换） | 4 |
| 标准 MiniMax-H3 | 同上 | 同上 | 20 |
| 精度切换 | int8 convrot（质量）/ nvfp4（省显存） | — | — |

## 运行

1. 先启动 ComfyUI（默认监听 `127.0.0.1:8188`）
2. 双击 **启动.bat**（自动探测 ComfyUI 自带 Python，找不到则回退系统 `python`，需 `aiohttp`）
3. 浏览器打开 `http://127.0.0.1:8190`（局域网手机也可访问 `http://<本机IP>:8190`）

首次使用先把 `config.example.json` 复制为 `config.json`，按你的环境修改：

```json
{
  "host": "0.0.0.0",
  "port": 8190,
  "comfyui_url": "http://127.0.0.1:8188",
  "data_dir": "./data",
  "models_dir": "<你的ComfyUI目录>/ComfyUI/models",
  "poll_interval": 1.5
}
```

> `models_dir` 仅用于「硬件参数」页的模型库统计，留空则跳过。

## 模型准备

下载 [Comfy-Org MiniMax-H3](https://huggingface.co/Comfy-Org/MiniMax-H3) 权重并放入对应目录：

- `diffusion_models/` — `minimax_h3_fl2va` / `minimax_h3_ref2va`（int8 convrot 或 nvfp4 精度）
- `text_encoders/` — qwen3vl（随精度切换）
- `loras/` — Turbo LoRA（如 ckpt850、v4-step600）
- `diffusion_models/` — Z-IMAGE-turbo（文生图模式）

## 数据

- `data/jobs.db` — 任务队列与历史（SQLite）
- `data/outputs/<job_id>/` — 成片 / 成图
- `data/uploads/` — 上传的首尾帧 / 参考图

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/jobs` | multipart 提交任务（mode + params JSON + 图片文件） |
| GET | `/api/jobs?status=` | 任务列表（queued/running/done/failed/canceled/all） |
| POST | `/api/jobs/{id}/cancel` | 取消（排队从队列删，运行中触发 interrupt） |
| DELETE | `/api/jobs/{id}` | 删除已完成记录及输出 |
| GET | `/api/system` | 队列与 ComfyUI 状态 |
| GET | `/api/hardware` | 本机硬件参数（GPU/CPU/内存/磁盘/软件/模型库） |
| POST | `/api/unload` | 释放 ComfyUI 显存 |

## 时长→帧数

与官方工作流一致：`max(5, round(秒*24)) + (5 - max(5, round(秒*24)) % 17) % 17`，24fps 对齐 17 帧约束。

## 致谢

- [zxcHolmes/minimax-h3-studio](https://github.com/zxcHolmes/minimax-h3-studio)（UI 灵感来源）
- [Comfy-Org MiniMax-H3](https://huggingface.co/Comfy-Org/MiniMax-H3) 模型
