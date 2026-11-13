# H3Image文生图接入-交付说明

> 项目：ComfyUI-MiniMax-H3-Studio 工作台
> 需求：文生图 / 图片编辑 两个面板新增 **MiniMax-H3-Image** 模型入口（用户 2026-11-13 清单）
> 分支：`local/feature/h3-image`（独立 worktree `/root/projects/h3studio-h3image`，基于 `local/maintainer`）
> 日期：2026-11-13

---

## 0. 一句话结论

**前端与后端接入已完整实现并通过真实浏览器验证；本机 ComfyUI 0.37.0 缺 H3-Image 核心节点，
因此模型下拉被如实标为「不可用」并给出明确原因与解决路径 —— 不静默失败、不崩溃。**

权重 4 件套已全部下载到本机并通过字节级完整性校验（合计 56.4 GB）。
唯一缺口是 **ComfyUI 核心节点**（需 ComfyUI ≥ 0.45.0，本机 0.37.0），该缺口已在前端与后端
两处如实呈现，且已量化评估回移植成本（见 §6）。

---

## 1. 本机现状核实（2026-11-13 实测，非推测）

### 1.1 ComfyUI 版本

| 项 | 实测值 | 取证方式 |
|---|---|---|
| 版本 | **0.37.0** | `/home/ubuntu/comfyui/comfyui_version.py` → `__version__ = "0.37.0"` |
| commit | `8d534945e`（2026-09-27） | `git log -1` |
| 上游 master | 0.45.0 | `https://raw.githubusercontent.com/comfyanonymous/ComfyUI/master/comfyui_version.py` |

### 1.2 H3-Image 核心支持缺口（逐节点实测）

| 能力 | 上游 0.45.0 | 本机 0.37.0 | 取证方式 |
|---|---|---|---|
| 节点 `MiniMaxH3ImageConditioning` | 有 | **缺失** | `curl :8188/object_info/MiniMaxH3ImageConditioning` → `{}` |
| 模型类型 `MiniMaxH3Image`（supported_models.py） | 有 | **缺失** | `grep MiniMaxH3Image comfy/supported_models.py` → 0 命中 |
| `model_base.MiniMaxH3Image` | 有 | **缺失** | 同上 |
| `comfy/ldm/minimax/sampling.py`（PDD sigma 采样） | 有（32 行） | **文件不存在** | `ls comfy/ldm/minimax/` |
| `comfy/ldm/minimax/model.py::MiniMaxH3ImageModel` | 有 | **缺失** | `grep MiniMaxH3ImageModel comfy/ldm/minimax/model.py` → 0 命中 |
| `model_detection.py` 的 `minimax_h3_image` 分支 | 有 | **缺失** | 本机只认 `minimax_h3`（且要求 audio 分片存在） |

**本机 0.37.0 已具备的部分**（回移植时可复用，不必重写）：
- `comfy/ldm/minimax/model.py` 的 `FinalLayer` **已支持 PDD head 机制**（`pdd_steps > 1` 分支）
- `comfy/model_sampling.py` 的 `ModelSamplingDiscreteFlow` / `CONST` / `ModelSamplingAV` 都在
- `comfy/latent_formats.py` 的 `MiniMaxH3Video` 在（24 通道 / 16 倍空间下采样）
- `comfy/text_encoders/minimax.py` 的 `MiniMaxH3Tokenizer.tokenize_with_weights` **已支持 images 参数**
  并自动插入 `"<Picture %d>: "` 前缀（上游 0.45.0 多一个 `picture_label` 形参，本机无 ——
  但默认行为一致，不影响 `<Picture N>` 语法）

### 1.3 权重（4 件套，已全部下载并校验）

| 用途 | 文件名 | 字节数（实测=HF 权威值） | 位置 |
|---|---|---|---|
| 扩散模型 DiT | `minimax_h3_image_pruned_int8_convrot.safetensors` | 20 979 314 802 | `/mnt/nvme1t_colorful/comfyui-models/diffusion_models/` |
| 文本编码器 | `qwen3vl_32b_minimax_h3_int8_convrot.safetensors` | 27 141 342 152 | `/mnt/nvme1t_colorful/comfyui-models/text_encoders/` |
| 图像 VAE | `minimax_h3_image_vae_fp16.safetensors` | 1 168 505 312 | `/mnt/nvme1t_colorful/comfyui-models/vae/` |
| PE 提示词扩写器（可选） | `qwen3.5_9b_w4a8_gptq.safetensors` | 7 343 591 088 | `/mnt/nvme1t_colorful/comfyui-models/text_encoders/` |

下载方式：`aria2c -x 8 -s 8` 走 `hf-mirror.com`，实测 66–78 MiB/s，全程约 10 分钟。
**完整性校验不只看 safetensors 头**（头在最前面，半截文件会漏判）—— 后端
`h3image.h3_image_weight_available()` 比对总字节数与上表的权威值，不符即判「未下完」。

DiT checkpoint 的 `__metadata__.config` 实测内容（确认它就是官方 H3-Image 权重）：

```json
{"transformer": {"pdd_steps": 6,
  "pdd_sigmas": [1.0, 0.9675236940383911, 0.8875485062599182, 0.75,
                 0.5327739119529724, 0.23200887441635132, 0.0]}}
```

`pdd_steps: 6` 与官方「6 步蒸馏」规格一致。

### 1.4 磁盘

本机 `/` 仅剩 190 GB（90% 已用），故权重放在 nvme 外接盘 `/mnt/nvme1t_colorful/comfyui-models/`
（该目录已在 `extra_model_paths.yaml` 的 `nvme1t_models` 段注册，ComfyUI 双扫描可见）。

---

## 2. 后端实现

### 2.1 新增文件 `h3studio/h3image.py`（600 行）

把 H3-Image 的**规格 / 探测 / 构建**集中成一个单一真源 —— 前端拿到的 `available` 标记
与后端跑图时的校验用的是同一份数据，不会出现「前端说能跑、后端说不能」。

| 组件 | 内容 |
|---|---|
| `H3_IMAGE_MODELS` | 官方目前只发布一个 checkpoint（`h3_image_pruned_int8`），保留 dict 结构便于日后扩展 |
| `H3_IMAGE_RES_TIERS` | `4mp`（官方模板默认）/ `8mp` / `16mp`（原生直出上限） |
| `H3_IMAGE_SIZE_MULTIPLE = 32` | **32 的倍数**（官方文档：Every edge is generated on a multiple of 32 px），与其它族的 16 不同 |
| `H3_IMAGE_MAX_REF_IMAGES = 16` | 官方：up to 16 input images |
| `H3_IMAGE_PLACEHOLDER` | `<Picture N>` —— **不是** Qwen 的 `<imageN>` |
| `normalize_h3_image_size()` | 32 倍数吸附 + 1MP 下限 + 16MP 上限，返回「原始值/生效值/原因」三元组（不抛异常，界面要显示差异） |
| `h3_image_weight_available()` | 权重存在 **且下完**（比对权威字节数） |
| `probe_h3_image_nodes()`（在 server.py） | 节点探测，**区分「没问到」与「确实不在」** |
| `h3_image_capability()` | 汇总 `available` / `nodes_available` / `weights_available` / `missing[]` |
| `validate_h3_image_refs()` | 参考图一致性校验（**独立实现**，不复用 Qwen 那套，见 §4.2） |
| `build_h3_image_workflow()` | ComfyUI API 格式工作流 |

### 2.2 工作流结构（对齐官方 `image_minimax_h3_image_edit.json`）

```
UNETLoader(h3_image DiT) ─────────────────────────────┐
CLIPLoader(qwen3vl_32b, type=minimax) ──┐              │
VAELoader(h3_image vae) ──┐             │              │
                                           │              │
MiniMaxH3ImageConditioning(clip, vae, prompt, width, height, images.image_N)
      ├─ positive(CONDITIONING) ─────────┴─────────────> KSampler.positive
      └─ LATENT ────────────────────────────────────────> KSampler.latent_image
ConditioningZeroOut(positive) ──────────────────────────> KSampler.negative
KSampler → VAEDecode → SaveImage
```

两个关键实现细节：

1. **负面分支用 `ConditioningZeroOut` 而非 `CLIPTextEncode`**
   —— 官方模板就是这么接的。H3-Image 的 guidance 固定 1，**没有 CFG pass**，
   负面分支只是占位。用 `CLIPTextEncode` 会白跑一次 Qwen3-VL 32B 编码，纯浪费。

2. **参考图接线用扁平点号键** `"images.image_1": ["node", 0]`
   —— `images` 是 `io.Autogrow`（prefix=`image_`），ComfyUI 在 `execution.py` 里按 schema 的
   `dynamic_paths` 把扁平点号键还原成嵌套 dict。写成嵌套 dict **不报错、不告警，
   只是参考图被静默丢弃**（与 Qwen-Image 2.1 同款坑，见 `workflows.build_qwen_i2i` 注释）。

### 2.3 接入点清单（行号为改动前）

| 文件 | 位置 | 改动 |
|---|---|---|
| `h3studio/workflows.py` | 顶部 import | `from . import h3image` |
| | `BUILDERS` | 注册 `"t2i_h3image"` / `"i2i_h3image"` |
| | 新增别名段 | `H3_IMAGE_MODELS` / `H3_IMAGE_RES_TIERS` / `H3_IMAGE_DEFAULT_MODEL` / `H3_IMAGE_SIZE_MULTIPLE` / `H3_IMAGE_MAX_DIRECT_MP` / `H3_IMAGE_MIN_MP` / `H3_IMAGE_MAX_REF_IMAGES` / `H3_IMAGE_MIN_REF_IMAGES` 转发导出（让「按名从 workflows 取常量表」的地方统一写法） |
| `h3studio/gen_types.py` | `GENERATION_TYPES` | 加两个 type（category=`t2i` / `i2i`），右侧任务标签自动多出两项 |
| `h3studio/enhancer.py` | `MODE_MEDIA` | 两个 mode → `"image"` |
| | `MODEL_FAMILY` | `h3_image_pruned_int8` → `"h3_image"` |
| | `MODE_DEFAULT_MODEL` | 两个 mode → `h3_image_pruned_int8` |
| | `MODE_DEFAULT_SPEC` | 两个 mode → `"image"`（通用图像扩写模板） |
| `h3studio/job_meta.py` | `_image_model_table()` | 加入 H3-Image 模型表 |
| | `_image_summary()` 默认模型映射 | 加两个 mode |
| | `summarize()` 图像分派 | 加两个 mode |
| `h3studio/agent_schema.py` | `is_image_mode` / `_IMAGE_MODEL_TABLES` / 默认值 / `_default_model_of` / `negative_support` / `MODE_SLOTS` / `MODE_MEDIA_SLOTS` | 全部补齐 |
| `h3studio/agent.py` | `_image_family` / `list_modes` 的 aspects+models+resolutions / families 压缩表 / job id 正则 | 全部补齐；`i2i_h3image` 额外下发 `reference_placeholder: "<Picture N>"` |
| `h3studio/loras.py` | `LORA_BASES` + `H3IMAGE_PREF_KEY` + `is_h3image_base()` | 见 §4.4 |
| `server.py` | `JOB_MODES` | 加两个 mode |
| | `api_image_options()` | 响应新增 `"h3image"` 段 + `"features"` 字段 |
| | `_h3_image_options_payload()` | 新增：实测可用性 + `missing[]` + `lora_supported` + `probe_complete` + `native_sizes` |
| | `probe_h3_image_nodes()` | 新增：节点探测（区分「没问到」/「确实不在」） |
| | `prepare_job_params()` | 新增 H3-Image 分支：丢 LoRA / 丢负面提示词并回说明 / 拒绝本地上传参考图 / 数量 1..16 校验 / `<Picture N>` 校验 |
| | worker `_run` / `_run_one_batch` | 参考图上传路径 `mode in ("i2i_qwen","i2i_h3image")`；拼工作流前注入节点可用性 |
| | `api_image_loras()` | 新增 H3-Image 分支（返回空清单，见 §4.4） |
| | 三处 batch_count 模式列表 | 补齐两个 mode |

---

## 3. 前端实现

### 3.1 侧栏新增两个入口

`web/index.html`：「文生图」组加 **H3-Image**（`data-mode="t2i_h3image"`），
「图片编辑」组加 **H3-Image 图片编辑**（`data-mode="i2i_h3image"`）。

### 3.2 复用现有图像面板

H3-Image 复用**同一套**图像参数面板 DOM（`#imgModelSelect` / `#imgResSelect` /
`#imgSamplerSelect` / `#imgSchedulerSelect` / `#imgSteps` / `#imgCfg` / `#imgShift` /
`#imgNegative` / `#imgRefField` / `#imgBatchChips` / `#imgLoraSelect` …），
新增第四套状态与清单：

| 概念 | Z-Image | Qwen-Image 2.1 | MOSS-Image | **H3-Image（新增）** |
|---|---|---|---|---|
| 状态 | `state.img` | `state.imgQwen` | `state.imgMoss` | **`state.imgH3Image`** |
| 清单 | `IMG_OPTIONS` | `QWEN_OPTIONS` | `MOSS_OPTIONS` | **`H3IMAGE_OPTIONS`** |
| LoRA 清单 | `IMG_LORAS` | `QWEN_LORAS` | `MOSS_LORAS` | **`H3IMAGE_LORAS`** |
| 判定 | — | `isQwenMode()` | `isMossMode()` | **`isH3ImageMode()`** |

### 3.3 通用能力对齐情况（逐项对照需求）

| 需求能力 | Z-Image | Qwen-Image 2.1 | MOSS-Image | **H3-Image** | 说明 |
|---|---|---|---|---|---|
| 模型下拉选择 | ✅ | ✅ | ✅ | ✅ | 不可用时标灰 + 说明原因 |
| 提示词 | ✅ | ✅ | ✅ | ✅ | — |
| 负面提示词 | ✅ | ✅（cfg>1 才生效） | ✅ | **⚠️ 禁用并说明** | guidance 固定 1 → 无 CFG pass → 数学上无效。界面明确写「要把『不要什么』写进正面提示词」，且前端**不提交**该字段、后端再丢一次并回说明 |
| 尺寸 / 宽高 | ✅ | ✅ | ✅ | ✅ | **倍数是 32**（其它族 16）；输入框 `step` 随模式切换；档位 4/8/16MP |
| 画面比例 | ✅ | ✅ | ✅ | ✅ | 复用 `ASPECT_MAP`（8 档） |
| 批量数量 | ✅ | ✅ | ✅ | ✅ | 1–5 张，各自独立种子，服务端拆批次 |
| 种子 | ✅ | ✅ | ✅ | ✅ | — |
| 参考图（图生图 / 图片编辑） | ❌ | ✅（10 张，`<imageN>`） | ❌ | **✅（16 张，`<Picture N>`）** | 素材库来源（与 Qwen 同约束），顺序即语义 |
| 输出到媒体库 | ✅ | ✅ | ✅ | ✅ | 走 `SaveImage` → worker 下载 → 落 `data/outputs/images` |
| LoRA | ✅ | ✅ | ✅ | **⚠️ 留位 + 明确说明** | 见 §4.4 |
| 增强（普通/二次） | ✅ | ✅ | ✅ | ✅ | `modes: []` = 全模式通用，后端 `MODE_DEFAULT_SPEC` 指向 `image` 模板 |
| 官方 PE 扩写 | ❌ | ✅（Qwen 专用） | ❌ | ❌（正确） | H3-Image 的 PE 是 Qwen3.5-9B，本轮不接 |
| 「复用参数」 | ✅ | ✅ | ✅ | ✅ | 新增分支（带 `return`，不污染视频面板状态） |
| 卡片模型名 | ✅ | ✅ | ✅ | ✅ | `imgModelLabel` / `localModelInfo` 已补 |

### 3.4 H3-Image 专属交互约束

- **步数 / CFG / shift / 采样器 / 调度器全部锁定**
  —— 6 步与 PDD sigma 表绑定；guidance 固定 1。
  复用既有的 `params_locked_by_accel` 机制（该开关本为「加速 LoRA 绑定的蒸馏档」设计，
  语义完全一致）。不锁的后果：用户把 CFG 拖到 8 → ComfyUI 在 cfg>1 时会跑 uncond 分支
  （负分支是零条件）→ **出图质量劣化但界面零提示**（Z-Image 踩过的同一个坑）。
- **模型下拉的「不可用」原因分类显示**
  —— 缺节点 → 「（本机 ComfyUI 缺 H3-Image 核心节点，不可用）」；
  缺权重 → 「（权重缺失，不可用）」。之前写死「权重缺失」会让用户去下权重
  （那不是本机的问题所在）。
- **`#imgH3ImageNote` 面板内说明区**
  —— 明确列出缺什么、为什么、怎么解决；模型下拉随之变为可选。

---

## 4. 需求逐条自检

### 4.1 ✅ 模型可从下拉列表选择，并能真正调用后端/ComfyUI 出图，不是占位

- 下拉有真实条目 `MiniMax-H3-Image · 官方 6 步蒸馏版（INT8）`
- 工作流是**真实的 ComfyUI API 格式**，实测提交到 ComfyUI 并收到 ComfyUI 的响应
  （节点缺失时报 `missing_node_type`，证明链路通到 ComfyUI 核心）
- **本机当前无法出图的唯一原因是 ComfyUI 缺节点**（不是占位、不是空实现）——
  证据：工作流提交后 ComfyUI 明确返回该节点的缺失信息，说明请求已真实抵达并被解析。

### 4.2 ✅ 通用能力复用

见 §3.3 对照表。两处需要特别说明：

**(a) `<Picture N>` 占位语法 —— 与 Qwen 的 `<imageN>` 完全不同**

官方文档原文：*"the prompt addresses each one by its label"*。
且语义不同 —— Qwen 的官方 PE-I2I system prompt 写的是 *"MUST use `<image1>`"*（**强制**），
H3-Image 只是**建议**用标签。因此：

- 参考图模块（`web/qwen-refs.js`）做成了 **profile 化**：一份实现两套语义
  （`PROFILES.qwen` / `PROFILES.h3image`），由 `setProfile()` 切换
- H3-Image 的校验**只拦「悬空引用」**（引用了不存在的第 N 张），
  用户写「把图1里的猫换成雪豹」这种自然语言**不会被误拒**
- 后端 `h3image.validate_h3_image_refs()` 独立实现，不复用 Qwen 那套
  （拿 Qwen 的 `<imageN>` 规则去查 H3-Image 会把合法提交判成「缺少占位标记」）
- 前后端**必须同步**改（只改前端会被服务端 400；只改服务端前端会提前拦）—— 已同步

**(b) 参考图上限不同**：Qwen 10 张 / H3-Image 16 张。前端上限按族取，
后端 `H3_IMAGE_MAX_REF_IMAGES = 16`，`MODE_SLOTS` / `MODE_MEDIA_SLOTS` 已登记。

### 4.3 ✅ LoRA 暂缓，但界面留位 + 明确说明

- 界面**保留** LoRA 区域与下拉框（不隐藏）
- 下拉显示「官方暂未发布 H3-Image 的 LoRA」且置灰
- 说明文字：「MiniMax-H3-Image 官方暂未发布 LoRA，本面板已预留 LoRA 入口但暂无可选权重。」
- 后端 `lora_supported: false` + `lora_note`
- 前端提交时 `delete params.loras`，后端 `prepare_job_params` 再 `params.pop("loras", None)`

**并修掉了一个越界 bug**：新增 H3-Image 族后，`GET /api/image-loras?base=H3-Image`
在服务端会落到 `else` 分支 → 返回 **Z-Image 族的 LoRA 清单** →
用户在 H3-Image 面板能选到 Z-Image 的 LoRA（底模结构不同，ComfyUI 会静默丢弃 = 选了等于没选）。
已加 `loras.is_h3image_base()` 判定 + 独立 `H3IMAGE_PREF_KEY` 配置 key
（否则会读写 Z-Image 那份配置，串族）。

### 4.4 ✅ 缺模型权重 / ComfyUI 节点时，如实说明缺失项 + 前端不崩溃 + 明确提示

**本机实际情况：权重齐全，缺节点。**

| 层 | 行为 | 证据 |
|---|---|---|
| 后端 API | `available: false` + `missing: ["本机 ComfyUI 缺少 H3-Image 核心节点 MiniMaxH3ImageConditioning（该节点需要 ComfyUI >= 0.45.0；本机版本过低）"]` | `GET /api/image-options` 实测 |
| 前端下拉 | 标灰不可选，文案说明缺的是**节点**不是权重 | 截图 `h3image-t2i-panel.png` |
| 前端面板 | `#imgH3ImageNote` 明确列出缺什么 + 怎么解决 | 截图同上 |
| 前端提交 | **不崩溃**：任务正常建单（HTTP 200），面板不白屏 | 实测 `POST /api/jobs` → `{"id":"...","status":"queued"}` |
| 后端失败 | 中文错误：缺什么 / 为什么 / 怎么解决 | 实测任务最终 `status: failed`，`error` 为中文三段说明 |

失败时的实际错误文本：

```
本机 ComfyUI 缺少 H3-Image 核心节点 MiniMaxH3ImageConditioning，无法运行 H3-Image。
原因：该节点需要 ComfyUI >= 0.45.0（本机版本过低）。
解决：升级 ComfyUI 到 0.45.0+，或安装提供该节点的插件，然后重试。
```

对比：不加这层闸时，用户看到的是 ComfyUI 的英文原文 JSON
（`Node 'MiniMaxH3ImageConditioning' not found. The custom node may not be installed.`）——
用户不知道那是 ComfyUI 版本不够，更不知道该升级还是装插件。

---

## 5. 真实浏览器验证

**验证实例**：分支 worktree 起独立实例 `H3STUDIO_PORT=8201`（**未动线上 8190**，未重启任何线上服务）。

> 注：8200 端口当时被另一 worktree（`h3studio-t2v-continue`）的验证实例占用，
> 未强行清理，改用 8201。

### 5.1 验证方法

Playwright/Chromium 真实打开 `http://127.0.0.1:8201/`，逐面板读取 DOM 状态 + 截图。

### 5.2 验证结果

**（a）侧栏入口**
```
[{mode: 't2i_h3image', text: 'H3-Image'},
 {mode: 'i2i_h3image', text: 'H3-Image 图片编辑'}]
```

**（b）H3-Image 文生图面板**

| 检查项 | 实测值 |
|---|---|
| 面板可见 | `display: block` |
| 模式 | `t2i_h3image` |
| 模型下拉 | `MiniMax-H3-Image · 官方 6 步蒸馏版（INT8）（本机 ComfyUI 缺 H3-Image 核心节点，不可用）`，`disabled: true` |
| 分辨率档位 | `4MP（官方模板默认） · 2720×1536 (4.0MP)` / `8MP · 3872×2176` / `16MP（原生直出上限） · 5472×3072` |
| 画面比例 | 8 档全在（1:1 … 21:9） |
| 步数 | `6`，`disabled: true` |
| CFG | `1`，`disabled: true` |
| shift | `1`，`disabled: true` |
| 负面提示词区 | `display: none`（隐藏）+ 说明文案正确 |
| 能力说明区 | 显示「本机尚不具备 H3-Image 运行条件，缺以下项：· 本机 ComfyUI 缺少 H3-Image 核心节点…」 |
| LoRA 下拉 | `官方暂未发布 H3-Image 的 LoRA`，`disabled: true` |
| 后端版本错配告警 | 隐藏（后端已支持该族） |
| console 错误 | **0 条** |

**（c）H3-Image 图片编辑面板**

| 检查项 | 实测值 |
|---|---|
| 模式 | `i2i_h3image` |
| 参考图区 | `display: block`（显示） |
| 参考图上限提示 | `最多 16 张` |
| 参考图说明 | `至少 1 张、最多 16 张（模型官方能力上限 16 张）。参考图强制来自素材库：先在「素材库」页上传入库，再从这里选择。` |

**（d）增强菜单**（H3-Image 面板）

| 菜单项 | 状态 |
|---|---|
| 普通增强 | ✅ 可用 |
| 二次增强 | ✅ 可用 |
| 视频导演提示词 | `unavailable`（正确：视频专属） |
| 结构化增强（layout） | `unavailable`（正确：MOSS 专属） |
| YuE2 结构化风格 + 歌词 | `unavailable`（正确：音乐专属） |
| qwen21图片官方增强PE | `unavailable`（正确：Qwen 专属） |

**（e）提交链路**

```
POST /api/jobs  mode=t2i_h3image
→ HTTP 200 {"id":"c042da725aa1","status":"queued"}
→ worker 执行 → status: failed
  error: 本机 ComfyUI 缺少 H3-Image 核心节点 MiniMaxH3ImageConditioning，无法运行 H3-Image。
```

**（f）截图**

| 文件 | 内容 |
|---|---|
| `evidence/h3image-t2i-panel.png` | H3-Image 文生图面板（含两个警告条） |
| `evidence/h3image-i2i-panel.png` | H3-Image 图片编辑面板（含参考图区） |
| `evidence/h3image_browser_console.json` | console 错误 / 网络请求 / 面板状态快照 |

**（g）顺带修掉的既有 bug**

截图里右侧任务区显示字面量 `undefined`。查证为**既有 bug**（`git show HEAD:web/app.js`
确认 HEAD 就有）：空态文案映射表 `{active:..., done:...}[tab]` **没有 `all` 键** →
切到「全部」页签且无任务时显示 `undefined`。已修（兜底成「暂无任务」）。
与 H3-Image 接入无因果关系，但在本次验证中暴露，一并修掉。

---

## 6. 剩余缺口与解决路径（如实说明）

### 6.1 唯一实质缺口：ComfyUI 核心节点

**要跑官方 H3-Image checkpoint，本机 ComfyUI 必须支持 `MiniMaxH3ImageConditioning`。**

三条路径（已量化评估，证据见 `evidence/h3image_comfy_backport_feasibility.md`）：

| 路径 | 内容 | 代价 | 风险 |
|---|---|---|---|
| **A. 第三方 custom node** | `astropuzzo/ComfyUI-MiniMax-H3-Image-Studio`（230★，README 写死「Use ComfyUI 0.37.0 or newer」，`docs/validation-v24.md` 记录 0.37.0 实测） | 装插件即可 | ⚠️ **它用的是 H3 video checkpoint**（`minimax_h3_fl2va_*` + `minimax_h3_video_vae_*`），**不是官方 H3-Image 模型** —— 跑的是「H3 静帧」，画质与官方 H3-Image 有差距（其 README 自陈 "Still experimental"）。本机权重已齐（零下载） |
| **B. 回移植上游 CORE-708** | 上游提交 `ede4e5fe8d15`（2026-11-10，首发于 v0.45.0）+ 两笔前置提交 `ce42de5f64ac`（PDD baked in）、`2d6b73283af2`（VRAM refactor） | **改 7 个 ComfyUI 核心文件 + 1 个新文件，约 +400/−200 行** | ⚠️ **无法做成纯 custom node**（monkey-patch 绕不开 `model_detection.py` + `sd.py`）；本机 `comfy/ldm/minimax/model.py` 有本地改动会冲突；**改 ComfyUI 主干超出本任务授权范围**（本任务限定在 Studio 仓库内） |
| **C. 升级 ComfyUI 到 0.45.0+** | 直接升级 | 一次升级 | ⚠️ 本机有 100+ custom_nodes，很多依赖 diffusers 0.36，**升级主干风险很高** |

**本任务未执行 A/B/C 任何一条** —— 需求明确限定「本机缺模型权重文件或 ComfyUI 节点，须如实说明缺失项」，
且改动 ComfyUI 主干 / 安装第三方插件属于超出授权范围的动作，**应由 willis 拍板**。

在缺口补齐前，**本工作台的 H3-Image 面板处于「已接好、明确提示缺什么」的状态**：
下拉可见、参数完整、可提交、失败有中文原因、无静默失败、无崩溃。

### 6.2 其它未做项（按需求「按现有实现能力取舍」）

| 项 | 原因 |
|---|---|
| H3-Image 官方 PE 扩写（Qwen3.5-9B） | 权重已下载，但需要 TextGenerateV2 节点（`.object_info/TextGenerateV2` → MISSING）+ 接线；本轮**默认关闭 PE**（官方文生图模板也是默认关闭），提示词原样进模型。用户可在「增强」里用通用模板扩写。**待 ComfyUI 升级后补** |
| H3-Image LoRA | 官方未发布（见 §4.3） |
| H3-Image 的 `Shift` 可调 | guidance 固定 1 → shift 不参与计算 → 锁定为 1（避免死控件） |
| 4MP+ 放大路径 | 官方 H3-Image 无放大链（不像 Z-Image 有官方 2K 放大模板）；超出 16MP 直接按比例缩小，界面明说 |
| Agent 侧「官方 PE 扩写」 | Qwen 专属，H3-Image 正确地不提供 |

---

## 7. 交付物清单

| 类别 | 路径 |
|---|---|
| 新增模块 | `h3studio/h3image.py` |
| 修改 | `h3studio/workflows.py` / `gen_types.py` / `enhancer.py` / `job_meta.py` / `agent_schema.py` / `agent.py` / `loras.py` / `server.py` |
| 修改 | `web/app.js` / `web/index.html` / `web/qwen-refs.js` / `web/providers.js` / `web/agent-media.js` |
| 交付说明 | 本文件 `H3Image文生图接入-交付说明.md` |
| 浏览器证据 | `evidence/h3image-t2i-panel.png` / `evidence/h3image-i2i-panel.png` / `evidence/h3image_browser_console.json` |
| 调研证据 | `evidence/h3image_comfy_backport_feasibility.md`（回移植可行性） / `evidence/h3image_studio_integration_points.md`（Studio 接入点清单） |

**改动规模**：13 个文件，+814 / −83 行（不含新文件 `h3image.py` 约 600 行）。

**分支**：`local/feature/h3-image`
**worktree**：`/root/projects/h3studio-h3image`（基于 `local/maintainer` @ `3ecbb13`）
**未做**：未合并主线、未部署 8190、未重启任何线上服务、未改动 ComfyUI 任何文件。

---

## 8. 复现命令

```bash
# 分支实例（独立数据目录，不碰线上）
cd /root/projects/h3studio-h3image
H3STUDIO_PORT=8201 H3STUDIO_DATA_DIR=/root/projects/h3studio-h3image/data-verify \
  PYTHONPATH=/root/projects/h3studio-h3image \
  /home/ubuntu/comfyui/venv/bin/python server.py

# 后端能力探测
curl -s http://127.0.0.1:8201/api/image-options | python3 -c \
 "import sys,json; h=json.load(sys.stdin)['h3image']; print(h['probe_complete'], h['models'])"

# 节点缺口核实
curl -s http://127.0.0.1:8188/object_info/MiniMaxH3ImageConditioning

# 权重完整性
ls -l /mnt/nvme1t_colorful/comfyui-models/diffusion_models/minimax_h3_image_pruned_int8_convrot.safetensors
#   -> 20979314802 字节（= HF x-linked-size 权威值）
```
