<div align="center">

# ComfyUI-MiniMax-H3 Studio — 本地视频生成工作台

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE) ![Platform: Windows](https://img.shields.io/badge/Platform-Windows-blue.svg) ![Model: MiniMax H3](https://img.shields.io/badge/Model-MiniMax%20H3-purple.svg) ![Version: v1.00](https://img.shields.io/badge/Version-v1.00-orange.svg)

**不用学节点图，也能用 MiniMax H3 在本地出片。**

本地 Web 工作台，驱动本机 ComfyUI 生成视频与图片。左侧选模式 → 中间调参数 → 右侧看任务卡片流，队列自动 FIFO 执行。零额外依赖，直接复用 ComfyUI 自带的 Python 运行。UI 灵感来源：[zxcHolmes/minimax-h3-studio](https://github.com/zxcHolmes/minimax-h3-studio)。

</div>

![ComfyUI-MiniMax-H3 Studio v1.00 主界面](docs/images/h3-studio-v1.00.png)

<p align="center"><sub>v1.00 实机截图（作者本地生产环境）· 左：模式与参数面板 · 右：已完成任务卡片流</sub></p>

## 它解决什么痛点

- **界面门槛高** — ComfyUI 节点图对新手不友好，参数散落在十几个节点里；这里把已验证的工作流藏到后台，只留选模式、写提示词、调参数、点生成。
- **生产资产难管** — 成片不再散落在输出目录里找不回：每次生成都是任务卡片，带提示词与参数，可搜索、筛选、播放、下载、一键复用参数。
- **长片合成麻烦** — 分段生成后不必再进剪辑软件：内置长片拼接一键成片，还能让 AI 智能体一句话生成整条长片（见文末）。

## 功能一览

- **文生视频 / 首尾帧 / 参考生成** — MiniMax H3，Turbo LoRA 加速（默认 6 步）或标准 H3（20 步）
- **Z-IMAGE 文生图**
- **任务队列与卡片流** — 进度与 ComfyUI 实时同步，进行中 / 已完成 / 长片 / 失败 / 已取消分类管理
- **长片拼接** — 片段拖入、一键成片（ffmpeg）
- **硬件参数页** — GPU / CPU / 内存 / 硬盘 / 软件环境 / 模型库六项实时状态，附一键释放显存

## 界面

- **顶栏**：`PixelAIGC ComfyUI-MiniMax-H3 Studio v1.00`，右侧实时显示「Generating, N 排队 / 空闲 / ComfyUI 未连接」
- **左侧栏**：视频（文生视频 / 首尾帧 / 参考生成 / 长片拼接）· 图像（Z-IMAGE 文生图）· 系统（硬件参数）· 释放显存
- **参数面板**：分辨率 544p/768p、模型卡（H3+LoRA / 标准 H3）、LoRA 选择与强度、步数 4~20、画面比例、时长 5~15s、种子
- **任务卡片**：排队/生成中进度条、可播放预览、Cancel、下载、复用参数、删除；可按提示词与任务 ID 搜索，按类型、比例、时长筛选
- **硬件参数页**：GPU（显存/利用率/温度/功耗/驱动）、CPU、内存、磁盘、软件环境（ComfyUI/PyTorch/Python/OS）、模型库分类统计，3 秒自动刷新

## 模式与模型映射

| 模式 | 工作流节点 | 说明 |
|---|---|---|
| 文生视频 t2v | `MiniMaxH3ImageToVideo` | 纯提示词，带原生音频 |
| 首尾帧 flf | `MiniMaxH3ImageToVideo` | first_frame / last_frame（尾帧可省略=图生视频） |
| 参考生成 r2v | `MiniMaxH3ReferenceToVideo` | 1~3 张参考图，主体一致性 |
| 文生图 t2i | Z-IMAGE-turbo | 8 步出图 |

**模型档位**（需已就位 MiniMax-H3 权重，见下方「前置准备：ComfyUI 与模型权重」）：

| 选项 | unet | clip | 步数 |
|---|---|---|---|
| H3 + LoRA（ckpt850 / v4-step600） | `minimax_h3_*_pruned_int8_convrot` 或 nvfp4 | qwen3vl（随精度切换） | 4 |
| 标准 MiniMax-H3 | 同上 | 同上 | 20 |
| 精度切换 | int8 convrot（质量）/ nvfp4（省显存） | — | — |

## 前置准备：ComfyUI 与模型权重

H3 Studio 不是独立的生成引擎，它驱动本机已经装好的 ComfyUI 出片。开始前备齐下面几样：

1. **ComfyUI**：已安装、能正常启动（默认监听 `127.0.0.1:8188`），且带有 MiniMax H3 相关节点（`MiniMaxH3ImageToVideo` / `MiniMaxH3ReferenceToVideo`）；缺节点时先更新 ComfyUI。
2. **MiniMax H3 权重**（放入 ComfyUI 的 `models/` 对应目录）：
   - `diffusion_models/` — `minimax_h3_fl2va_*`、`minimax_h3_ref2va_*`（int8 convrot 或 nvfp4 二选一，与文本编码器精度配套）
   - `text_encoders/` — `qwen3vl_32b_minimax_h3_*`（int4 convrot 或 nvfp4 awq）
   - `vae/` — `minimax_h3_video_vae_fp16.safetensors`、`minimax_h3_audio_vae_fp32.safetensors`
3. **Turbo LoRA**（放入 `models/loras/`）：`minimax_h3_turbo_4step_ema_ckpt850.safetensors`、`minimax_h3_turbo_v4_step600_ema.safetensors`；选「H3 + LoRA」档位时要用，只用标准 H3 档位可暂缺。
4. **Z-IMAGE 权重**（文生图模式要用，同样放入 `models/` 对应目录）：
   - `diffusion_models/` — `z_image_turbo_bf16.safetensors`
   - `text_encoders/` — `qwen_3_4b.safetensors`
   - `vae/` — `ae.safetensors`
5. **ffmpeg**：仅长片拼接需要，装好并加入 PATH。

> H3 权重从哪下、总控参数怎么调，见配套仓库 [ComfyUI-MiniMax-H3-Workflow](https://github.com/pixelaigc/ComfyUI-MiniMax-H3-Workflow)：那边是同一套 H3 生产的 ComfyUI 节点工作流，附 H3 权重文件的官方下载链接与目录表；本仓库是它的可视化工作台，两边配合使用。

## 安装与运行

1. **把本仓库放到任意目录**，路径不限，例如 `D:\h3s`。两种办法任选：

   **办法一：git 克隆（复制即用）**

   ```bat
   git clone https://github.com/pixelaigc/ComfyUI-MiniMax-H3-Studio.git D:\h3s
   cd D:\h3s
   ```

   以后更新只需在该目录执行 `git pull`。没装 git 也可以在仓库页面点 Code → Download ZIP，解压到 `D:\h3s`，效果一样。

   **办法二：交给 AI 智能体个性化安装**

   把下面这段原样粘贴给你的 AI 智能体（如 Muse、Cursor 等），它会按你这台电脑的实际情况完成安装：

   ```text
   请根据我本地电脑的实际情况，把 ComfyUI-MiniMax-H3 Studio 安装到 D:\h3s：
   仓库地址：https://github.com/pixelaigc/ComfyUI-MiniMax-H3-Studio
   要求：
   1) 先检查本机 ComfyUI 的安装目录和运行状态（默认 127.0.0.1:8188），再克隆仓库到 D:\h3s（目录已存在则先确认里面是什么，不要直接覆盖）；
   2) 按仓库 README 的「前置准备」逐项核对 MiniMax H3、Z-IMAGE 权重与 Turbo LoRA 是否已在 ComfyUI 的 models 对应目录中，缺哪个明确告诉我，不要未经我同意就下载大文件；
   3) 把 config.example.json 复制为 config.json，models_dir 填我本机的实际路径；如果我的 ComfyUI 不在 D:\ComfyUI，把 启动.bat 里的 COMFY_DIR 改成实际目录；
   4) 检查 ffmpeg 是否已安装并在 PATH 中（长片拼接要用），没有就告诉我怎么装；
   5) 最后先启动 ComfyUI，再双击 D:\h3s\启动.bat，确认浏览器能打开 http://127.0.0.1:8190，并向我报告每一步的结果和还缺什么。
   ```
2. **首次使用先建配置**：把 `config.example.json` 复制一份、改名为 `config.json`，按你的环境修改：

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

3. **先启动 ComfyUI**：确认它已在 `127.0.0.1:8188` 运行，且上面的 H3 / Z-IMAGE 权重都已就位。Studio 顶栏会实时显示 ComfyUI 的连接状态，连不上先解决这边。
4. **双击仓库目录下的 `启动.bat`**：脚本会自动探测 ComfyUI 自带的 Python 运行 `server.py`（零额外依赖）。如果你的 ComfyUI 不在脚本默认的 `D:\ComfyUI`，先用记事本把 `启动.bat` 里的 `COMFY_DIR` 改成你的实际目录；没启动 ComfyUI 时脚本会先提醒，并询问是否仍要启动 Studio。
5. **浏览器打开 `http://127.0.0.1:8190`** 即可使用；局域网内的手机也可以访问 `http://<本机IP>:8190`。

**关闭 Studio**：服务是在后台独立运行的，关掉浏览器页面、关掉启动窗口都不会停它（启动窗口按任意键消失后，服务照样在跑）。要真正关闭，先查 8190 端口的进程号，再结束它：

```bat
netstat -ano | findstr :8190
taskkill /PID <LISTENING 那一行的进程号> /F
```

认准 `LISTENING` 那行的 PID，别误杀 `ESTABLISHED` 另一端的号码（那是浏览器）。也可以在任务管理器里结束对应的 python 进程。注意这只关 Studio，ComfyUI（8188）是另一个程序，要单独关闭。

## 数据与输出目录

所有数据都在安装目录的 `data\` 下（`config.json` 的 `data_dir`，默认即仓库目录下的 `data`）。生成完成后，成片会从 ComfyUI 复制到这里统一管理，以 `D:\h3s` 为例：

| 内容 | 目录 | 文件名 |
|---|---|---|
| 生成的视频 | `D:\h3s\data\outputs\videos\` | `v-h3-studio-000001.mp4`（顺序编号） |
| 长片拼接的成片 | `D:\h3s\data\outputs\videos\` | `v-l-000001.mp4` |
| 生成的图片（Z-IMAGE） | `D:\h3s\data\outputs\images\` | `i-zimage-000001.png`（顺序编号） |
| 上传的首尾帧 / 参考图 | `D:\h3s\data\uploads\` | — |
| 任务记录（提示词、参数、状态） | `D:\h3s\data\jobs.db` | SQLite 数据库 |

在界面里删任务卡片，对应文件会一并删除；直接去上面目录里取文件也行，拷走不影响任务记录。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/jobs` | multipart 提交任务（mode + params JSON + 图片文件） |
| POST | `/api/longfilms` | 长片拼接：提交已生成片段，ffmpeg 拼接为一条长片 |
| GET | `/api/jobs?status=` | 任务列表（queued/running/done/failed/canceled/all） |
| POST | `/api/jobs/{id}/cancel` | 取消（排队从队列删，运行中触发 interrupt） |
| DELETE | `/api/jobs/{id}` | 删除已完成记录及输出 |
| GET | `/api/system` | 队列与 ComfyUI 状态 |
| GET | `/api/hardware` | 本机硬件参数（GPU/CPU/内存/磁盘/软件/模型库） |
| POST | `/api/unload` | 释放 ComfyUI 显存 |

## 时长→帧数

与官方工作流一致：`max(5, round(秒*24)) + (5 - max(5, round(秒*24)) % 17) % 17`，24fps 对齐 17 帧约束。

## 长片拼接

把已经生成的片段接成一条长片，不用再开剪辑软件：

1. 左侧栏进入「长片拼接」
2. 把要拼接的视频片段拖入左侧的框中，按先后顺序排好
3. 一键拼接，输出一条完整的长片

拼接在本机由 ffmpeg 完成，不经过 ComfyUI，也不重新生成画面。**前提：本机已安装 ffmpeg 并加入系统 PATH**（命令行输入 `ffmpeg -version` 能显示版本号即可）。没装 ffmpeg 只是拼接不可用，其他生成模式不受影响。

## 进阶：一句话让智能体生成整条长片

Studio 带 HTTP API（见上方「HTTP API」），能访问你本机 `127.0.0.1:8190` 的 AI 智能体可以直接把它当制作平台用：一句话说清主题和规格，智能体按 H3 提示词规范把长片拆成每段 5 秒的分段提示词，逐段提交（队列自动 FIFO 执行），全部生成后用长片拼接接成一条成片返回给你。

示例（把下面这段粘贴给你的智能体即可）：

```text
http://127.0.0.1:8190/ 以这里为制作平台，根据H3提示词的写作规范，制作30秒韩国天团美少女跳舞的视频，背景是黑色背景的舞台，炫酷的灯光，以特写、中景、局部、脸部、手部特写为主，大师级运镜，每段5秒，一共6段，用默认方式，16:9，没有背景音乐，生成后拼接成一个30秒视频，返回给我
```

换成你自己的片子时，改这几处就行：**主题内容、总时长、每段秒数（常用 5 秒）、段数（= 总时长 ÷ 每段秒数）、画面比例、要不要背景音乐**。要点：

- 智能体必须能访问运行 Studio 的这台电脑（智能体装在本机最稳妥）；
- Studio 和 ComfyUI 要先开着（见「安装与运行」），长片拼接还要求 ffmpeg 已装好；
- 段数越多耗时越长：每段都是真实生成一遍，智能体会排队等全部完成再拼接，适合挂机批量出长片。

## 硬件参数与释放显存

左侧栏「系统 → 硬件参数」是整台机器的实时状态页，3 秒自动刷新，分六块：

| 板块 | 显示内容 |
|---|---|
| GPU | 显卡型号、显存占用 / 总量、利用率、温度、功耗、驱动版本 |
| CPU | 型号、物理核 / 线程数、使用率、当前频率 |
| 内存 | 总量 / 已用 / 可用与使用率，含交换文件 |
| 硬盘 | 各分区的总量 / 已用 / 剩余与使用率 |
| 软件环境 | 操作系统、ComfyUI / PyTorch / Python 版本、CUDA 设备 |
| 模型库 | 按分类统计 `models` 目录中权重文件的数量与占用空间（需在 `config.json` 里填 `models_dir`，留空则跳过） |

出片前先看一眼显存够不够、生成时盯一下温度和功耗、排查问题时核对软件版本，都在这页。

**释放显存**：左侧栏底部的「释放显存」按钮，点击后向 ComfyUI 发送卸载请求，把常驻显存的模型卸掉、释放显存占用。暂时不生成、要把显卡让给其他软件时点一下即可；下次生成时模型会自动重新加载，只是第一段会稍慢。

## 致谢

- [zxcHolmes/minimax-h3-studio](https://github.com/zxcHolmes/minimax-h3-studio)（UI 灵感来源）
- [Comfy-Org MiniMax-H3](https://huggingface.co/Comfy-Org/MiniMax-H3) 模型
