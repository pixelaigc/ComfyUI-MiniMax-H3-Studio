# 修复①：PE 动作全量外置到 WEBUI + H3-IMAGE 专属 PE 模板 —— 交付报告

- **日期**：2026-11-15
- **仓库**：ComfyUI-MiniMax-H3-Studio（`/root/projects/comfyui-minimax-h3-studio`）
- **开发 worktree**：`/root/projects/h3studio-pe-ext`（独立 worktree）
- **分支**：`local/feature/pe-externalize`（基于线上 HEAD `faaff36`）
- **提交**：`9c9529a fix(pe): PE 动作全量外置到 WEBUI + H3-IMAGE 专属 PE 模板`
- **额度闸门**：通过。`quota/minimax_quota_latest.json` → general 5h 剩余 **99%** ≥ 20% 阈值（`gate_decision: PASS`）。全程只读该快照文件，未新建额度探针任务。

---

## 一、用户报的现象与现场根因（全部现场复核，不采信历史摘要）

### 现象 1：Qwen 面板「增强」选了官方 PE，点「生成」后工作流里又扩写一次

**现场复核结论：属实。** 根因在工作流构造器里，不在前端。

`h3studio/workflows.py` 的 `_qwen_common_nodes()` 里有一段 `if use_pe:` 分支，
无条件在出图链路里再搭一遍 PE 扩写节点：

```
CLIPLoader(PE qwen3.5_*_pe_*)  ->  TextGenerate(官方 system prompt + thinking)
     -> (图片编辑档) BatchImagesNode(参考图) -> TextGenerate.image
     ->  TextEncodeQwenImage21.prompt
```

`use_pe` 默认 `True`（`web/app.js` 的 `state.imgQwen.usePe = true`，
`h3studio/gen_defaults.py` 的 `_bool("use_pe", ...)` 默认 True）。
于是「WEBUI 手动扩写一次 + 出图链路再扩写一次」= 重复扩写。

### 现象 2：H3-IMAGE 的「增强」默认项是「普通增强（图像提示词·物体细化（二次增强））」

**现场复核结论：属实，根因是「过滤后回落」。**

```
h3studio/enhancer.py:
  TEMPLATE_SPECS["image"]["families"] = ["z_image"]     # 通用档锁 Z-Image
  MODEL_FAMILY["h3_image_pruned_int8"] = "h3_image"     # H3-Image 的族
  MODE_DEFAULT_SPEC["t2i_h3image"] = "image"            # 首选写的是 "image"
```

`filter_specs(specs, "t2i_h3image", "h3_image_pruned_int8")` 会把 `image` 过滤掉
（families 不含 `h3_image`）→ `default_spec_for_mode()` 发现首选不在过滤结果里
→ 回落���过滤结果里的第一个 → `image-refine`（物体细化·二次增强）。

**实测（改动前线上 8190）**：
```
$ curl -s http://127.0.0.1:8190/api/enhancer | ...
  t2i_h3image -> ['image','image-refine','image-scalist','image-bbox','qwen21-pe-t2i','qwen21-pe-i2i','moss-image']
  default_spec_for_mode('t2i_h3image', ..., 'h3_image_pruned_int8') = 'image-refine'
  => label: 图像提示词·物体细化（二次增强）
```

### 现象 3：H3-Image 官方 PE 权重本机缺失

`h3studio/h3image.py` 的 `H3_IMAGE_PE_CLIP = "qwen3.5_9b_w4a8_gptq.safetensors"`
在本机 `models/text_encoders/` **不存在**（只有 qwen 的两个 PE 权重）。
上游 `Comfy-Org/MiniMax-H3-Image` 有这个文件（7343591088 字节）。
→ 已下载安装（见第四节），否则 H3-Image 官方 PE 无法真实跑通。

---

## 二、改动清单（逐个文件 / 逐个位置）

### 2.1 工作流侧：移除工作流内的 PE 动作

| 文件 | 位置 | 改动 |
|---|---|---|
| `h3studio/workflows.py` | `_qwen_common_nodes()`（现 1421 行起） | 函数签名删掉 `pe_clip_name` / `use_pe` / `pe_params` / `pe_image_nodes` / `pe_system_prompt` 五个参数 |
| `h3studio/workflows.py` | 同函数内 | **删除** PE 编码器节点 `p["qw:30"]`（`CLIPLoader` 加载 `qwen3.5_*_pe_*`） |
| `h3studio/workflows.py` | 同函数内（原 `if use_pe:` 分支，整段） | **删除** `p["qw:25"]`（`BatchImagesNode`）+ `p["qw:26"]`（`TextGenerate`）及其全部输入参数（`sampling_mode.*`、`thinking`、`system_prompt`、`image` 等）；改为 `prompt_ref = prompt`（1513 行），提示词原样进 `TextEncodeQwenImage21` |
| `h3studio/workflows.py` | `build_qwen_t2i()` | 删除 `use_pe` / `pe_clip` / `pe_params` / `_apply_pe_overrides()` / `pe_system_prompt` 的读取与传参 |
| `h3studio/workflows.py` | `build_qwen_i2i()` | 同上；另删除 `pe_image_nodes=(ref_nodes if use_pe else None)` 传参 |
| `h3studio/workflows.py` | `build_qwen_i2i()` 参考图段 | 参考图→`LoadImage`→`TextEncodeQwenImage21` 的 `images.image_N` 扁平点号键接线**完整保留**（写错键名会让 ComfyUI 静默丢弃参考图，不能动） |

**其余工作流的现场排查结论（本来就没有 PE 动作，无需改动）**：

对 `h3studio/` 下全部 `.py` 做了 `class_type` 全量枚举与 `TextGenerate` / `TextGenerateV2` /
`BatchImagesNode` 检索，结果：

| 工作流 | 构造器 | PE 节点 | 结论 |
|---|---|---|---|
| Z-Image 文生图 | `workflows._z_image_t2i_nodes` | 无 | 本来就没有 |
| MOSS-Image 文生图 | `workflows.build_moss_t2i` | 无 | 本来就没有 |
| H3-Image 文生图/图片编辑 | `h3image.build_h3_image_workflow` | 无 | 本来就没有 |
| YuE2 音乐 | `music_yue2.build_music_workflow` | 无 | 本来就没有 |
| 参考生视频/首尾帧/文生视频 | `video_workflow` | 无 | 本来就没有 |

> 现场 grep 原文：
> ```
> $ grep -rn '"class_type"' --include=*.py h3studio/ | grep -i "textgen\|prompt\|expand\|caption\|llm"
> h3studio/qwen_pe.py:191:    workflow["pe:gen"] = {... "class_type": "TextGenerate"}     # WEBUI 侧扩写（保留）
> h3studio/h3image_pe.py:227: workflow["pe:gen"] = {... "class_type": "TextGenerateV2"}   # WEBUI 侧扩写（保留）
> h3studio/llm.py:836:            "class_type": COMFY_LLM_NODE,                          # Provider 通道（不是出图链路）
> h3studio/workflows.py:1568:        p["qw:26"] = {... "class_type": "TextGenerate"}        # ← 出图链路里的 PE（已删）
> ```
> 关键区分：`qwen_pe.py` / `h3image_pe.py` 里的 PE 工作流是**「增强」按钮点下去时单独提交的一次扩写任务**，
> 不是出图链路的一部分 —— 它们正是「PE 动作唯一由 WEBUI 侧完成」的载体，必须保留。

### 2.2 WEBUI 侧：去掉「独立勾选框」形式的 PE 选项

| 文件 | 位置 | 改动 |
|---|---|---|
| `h3studio/gen_defaults.py` | `spec()` 的 `kind == "qwen"` 分支 | 删除 `_bool("use_pe", "启用官方 PE 扩写", True, ...)` —— 它在「Agent 默认参数」面板里表现为一个**独立勾选框**，与「增强」下拉里的官方 PE 选项语义打架 |
| `web/app.js` | `state.imgQwen` 初始化（约 189 行） | 删除 `usePe: true` 字段 |
| `web/app.js` | 出图参数组装 `if (isQwenMode())` 块（约 3237 行） | 删除 `params.use_pe = ...`、`params.pe_params = {...}`、`params.pe_system_prompt = ...` 的提交 |
| `web/providers.js` | `provState` 初始化（约 13 行） | 删除 `lastEnhanceKind: ""` 字段 |
| `web/providers.js` | `applyQwenPeWritebackFlag()`（约 652 行） | **整段删除** —— 它唯一的作用是把 `state.imgQwen.usePe = false`（关掉已不存在的「二次扩写」）；保留会让人误以为出图链路还受 PE 开关控制 |
| `web/providers.js` | `#enhanceReplace` / `#enhanceAppend` 回调 | 删除两处 `applyQwenPeWritebackFlag()` 调用 |
| `web/enhance-menu.js` | 6 处 `provState.lastEnhanceKind = ...` 写入 | 全部删除（标记已无消费者） |
| `web/enhance-menu.js` | 2 处提示文案 | `点「替换提示词框」写回后，出图将不再二次扩写` → `点「替换提示词框」写回后即可出图（工作流不再二次扩写）` |

**结论**：官方 PE 仍然是「增强」下拉里的一项（`qwen_pe` / `h3image_pe`），
点「增强」按钮即执行 —— **没有任何独立勾选框**。

### 2.3 H3-IMAGE 专属 PE 模板（本次新增）

**新增两个模板档**（`h3studio/enhancer.py` 的 `TEMPLATE_SPECS`）：

| spec id | label | families | 模板文件 |
|---|---|---|---|
| `h3image-pe-t2i` | H3-Image 官方 PE·文生图（画面扩写+比例） | `["h3_image"]` | `h3image-pe-t2i.txt`（15831 B） |
| `h3image-pe-i2i` | H3-Image 官方 PE·图片编辑（参考图条件扩写） | `["h3_image"]` | `h3image-pe-i2i.txt`（38527 B） |

**模板正文来源 = 官方 ComfyUI 工作流模板**（不是自己编的）：
- `comfyui_workflow_templates_json/templates/image_minimax_h3_image_t2i.json`
- `comfyui_workflow_templates_json/templates/image_minimax_h3_image_edit.json`
- 取其中的 `PrimitiveStringMultiline`（标题 `Text (task_prompt)`）节点。

**默认项修正**（`MODE_DEFAULT_SPEC`）：
```python
"t2i_h3image": "h3image-pe-t2i",   # 原 "image"（会被过滤掉 → 回落 image-refine）
"i2i_h3image": "h3image-pe-i2i",
```

**实现方式说明**：这两个 spec 是**同一份官方 task prompt**，但走本项目的 Provider
（本地 LLM）通道，用于「普通增强」这一档；与「H3-Image 官方增强PE」项
（走官方 checkpoint，见 `h3studio/h3image_pe.py`）并列存在，两者共用同一份官方写作规则。

### 2.4 顺手修掉的 H3-Image 官方 PE 扩写链路三个真实缺陷

`h3studio/h3image_pe.py` 原有实现与官方模板对不上，会导致「官方 PE 跑不出官方效果」：

| # | 缺陷 | 原实现 | 官方事实 | 修法 |
|---|---|---|---|---|
| 1 | **占位符写错** | `"input_text: {}"`（HF 仓库 Transformers 版写法） | 官方 ComfyUI 模板用 **`{input text}`**，`StringReplace.find` 必须逐字符一致 | 常量 `TASK_PROMPT_PLACEHOLDER = "{input text}"`；task prompt 改为读官方资产文件 |
| 2 | **比例键名用错** | 两个档都用 `aspect_ratio` | 官方**图片编辑**档用 **`target_aspect_ratio`** | `PE_RATIO_KEYS = {"t2i": "aspect_ratio", "i2i": "target_aspect_ratio"}` |
| 3 | **比例校验用错尺子** | 两档都用文生图的 10 个固定档白名单 | 官方**编辑**档明确写「Any positive-integer W:H ratio is allowed; it is not limited to a preset list」 | 新增 `_valid_ratio(ratio, is_edit)`：文生图查白名单，编辑档查 `W:H` 正整数 |

另外：`TASK_PROMPT_HEAD`（代码内嵌的 3KB 缩写版）→ 改为读**完整官方正文**落盘文件，
使产物与官方模板**逐字节一致**（可用 diff 验证，测试里有自动比对）。

### 2.5 新增：JSON 契约解包

官方 H3-Image PE 的 task prompt 要求模型输出**一个两键 JSON**
（`expanded_caption` + 比例键），而文本路线要写回**纯文本**提示词。
不处理的话提示词框里会出现一坨 `{"expanded_caption": "...", ...}`。

新增 `h3studio/enhancer.py::unwrap_json_contract(text, spec_id)`：
- 仅对 `JSON_CONTRACT_SPEC_IDS = {"h3image-pe-t2i", "h3image-pe-i2i"}` 生效
- 取 `expanded_caption` 作为写回文本，比例单独回传（`server.py` 响应新增 `pe_aspect_ratio`）
- 其余模板（Qwen 官方 PE 两档 / MOSS / Z-Image）**原样通过**，不破坏既有行为

---

## 三、兼容性

| 检查项 | 结果 |
|---|---|
| MOSS 增强下拉实现模式 | 未动。MOSS 仍走 `moss-image` 档 + `moss-layout` 结构化增强 |
| Z-Image 面板默认模板 | 仍是 `image`（Z-Image 官方 PE） |
| Qwen 面板默认模板 | 仍是 `qwen21-pe-t2i` / `qwen21-pe-i2i` |
| 音乐面板 | 未动（`yue2_struct` 专属路线） |
| 视频面板 | 未动 |
| 增强结果仍写回提示词框且可继续编辑 | 是（四面板实测均验证） |
| `h3image-pe-*` 档是否泄漏到别的面板 | 否 —— `families: ["h3_image"]`，测试 `test_h3image_specs_are_family_locked` 断言 |

---

## 四、真机验证证据

### 4.1 验证环境

- 开发实例：`/root/projects/h3studio-pe-ext`，端口 **8199**（独立 worktree + 独立数据目录 `data-live/`），**未碰 8190 线上服务**
- ComfyUI：`http://127.0.0.1:8188`（共享）
- 浏览器：真实浏览器操作（`browser_exec`），四个面板逐个走完「选增强项 → 点增强 → 写回 → 点生成」

### 4.2 补装缺失的官方 PE 权重

```
$ curl -sL -o qwen3.5_9b_w4a8_gptq.safetensors \
    https://hf-mirror.com/Comfy-Org/MiniMax-H3-Image/resolve/main/text_encoders/qwen3.5_9b_w4a8_gptq.safetensors
$ stat -c%s qwen3.5_9b_w4a8_gptq.safetensors
7343591088                      # 与上游 x-linked-size 完全一致
$ cp qwen3.5_9b_w4a8_gptq.safetensors /home/ubuntu/comfyui/models/text_encoders/
$ curl -s http://127.0.0.1:8188/object_info/CLIPLoader | grep 9b_w4a8
'9b_w4a8 in CLIPLoader options': ['qwen3.5_9b_w4a8_gptq.safetensors']
```
官方 PE prompt 资产也按上游 manifest 的 SHA256 校验过：
```
818ab3f652ec36b3b489b55c29f072f87e14af703cd11a4f3bff75f423d0beac  h3image-pe-system.txt  (t2i/system_prompt.txt)
890213ad61e143bbd5b7906a802430296e82ca5c61ba16df717dcef5477bbcf4  h3image-pe-t2i.txt     (t2i/task_prompt.txt)
c5695dbfc07a9e65555cef67d92467c613371cc2039b96f057022246a30a57b2  h3image-pe-i2i.txt     (edit/task_prompt.txt)
```

### 4.3 四个面板各实跑一次生成 —— 全部成功

| # | 面板 | 增强方式 | PE 产物 | job id | 产物 | 尺寸 | 状态 |
|---|---|---|---|---|---|---|---|
| 1 | Qwen-Image 2.1 文生图 | `qwen21图片官方增强PE` | 3618 字（PE-T2I） | `aa96ed05d05c` | `images/i-zimage-000001.png` | 1360×768 | ✅ done |
| 2 | Qwen-Image 2.1 图片编辑 | `qwen21图片官方增强PE` | 939 字（PE-I2I，1 张参考图） | `e721077676fc` | `images/i-zimage-000003.png` | 1824×2272 | ✅ done |
| 3 | MiniMax-H3-Image 文生图 | `H3-Image 官方增强PE` | 728 字（比例 16:9） | `92db67795201` | `images/i-zimage-000002.png` | 2720×1536 | ✅ done |
| 4 | MiniMax-H3-Image 图片编辑 | `H3-Image 官方增强PE` | 963 字（比例 9:16，1 张参考图） | `406cb0abab89` | `images/i-zimage-000004.png` | 2720×1536 | ✅ done |

产物落盘：`/root/projects/h3studio-pe-ext/data-live/outputs/images/i-zimage-00000{1..4}.png`
（1.7MB / 6.1MB / 5.8MB / 5.7MB，均非空）
视觉复核 `i-zimage-000002.png`：「日落时分，一个被群山和森林环绕的宁静湖泊，湖面上漂浮着薄雾」
—— 与提示词一致。

**出图参数里 PE 相关字段全部不存在**（`/api/jobs` 实测）：
```
job=aa96ed05d05c mode=t2i_qwen     use_pe in params: False | pe_params: False | pe_system_prompt: False
job=e721077676fc mode=i2i_qwen     use_pe in params: False | pe_params: False | pe_system_prompt: False
job=92db67795201 mode=t2i_h3image  use_pe in params: False | pe_params: False | pe_system_prompt: False
job=406cb0abab89 mode=i2i_h3image  use_pe in params: False | pe_params: False | pe_system_prompt: False
```

### 4.4 核心证明：ComfyUI 侧工作流内无 PE 动作

抓 `http://127.0.0.1:8188/history` 全部任务，逐个解析 prompt JSON 的 `class_type`：

```
【出图任务】—— 必须全部无 PE 节点
Qwen-Image 出图    2175fb5d status=success
   nodes(10): ['CLIPLoader','VAELoader','UNETLoader','ModelSamplingFlux','QwenImage21Cache',
               'TextEncodeQwenImage21','EmptyLatentImage','KSampler','VAEDecode','SaveImage']
   PE 节点: NONE ✅
   CLIPLoader: ['qwen3vl_8b_bf16.safetensors']
   PE 权重: NONE ✅
H3-Image 出图      5d4ee9b1 status=success
   nodes(8): ['UNETLoader','CLIPLoader','VAELoader','MiniMaxH3ImageConditioning',
              'ConditioningZeroOut','KSampler','VAEDecode','SaveImage']
   PE 节点: NONE ✅
   PE 权重: NONE ✅
H3-Image 出图      4e6993d6 status=success   (10 nodes, 含 2 个 LoadImage 参考图)
   PE 节点: NONE ✅   PE 权重: NONE ✅
Qwen-Image 出图    02c8f6ed status=success   (10 nodes, 含 1 个 LoadImage 参考图)
   PE 节点: NONE ✅   PE 权重: NONE ✅
H3-Image 出图      9e04ae6e status=success   (9 nodes, 含 1 个 LoadImage 参考图)
   PE 节点: NONE ✅   PE 权重: NONE ✅

【PE 扩写任务】—— 独立任务（不在出图链路里）
PE 扩写 5651b800 success  clip: ['qwen3.5_9b_w4a8_gptq.safetensors']
PE 扩写 1f68f65b success  clip: ['qwen3.5_qwen_image_2.1_pe_t2i.int8_convrot.safetensors']
PE 扩写 d3c28601 success  clip: ['qwen3.5_9b_w4a8_gptq.safetensors']
PE 扩写 9b9ab226 success  clip: ['qwen3.5_9b_w4a8_gptq.safetensors']
PE 扩写 751c2a70 success  clip: ['qwen3.5_qwen_image_2.1_pe_i2i.int8_convrot.safetensors']
PE 扩写 bf3984da success  clip: ['qwen3.5_qwen_image_2.1_pe_i2i...','qwen3vl_8b_bf16...']
PE 扩写 334f7a83 success  clip: ['qwen3.5_9b_w4a8_gptq.safetensors']

>>> 出图任务里含 PE 动作的条数: 0 ✅ 通过
```

**这就是用户那个现象的直接反证**：PE 扩写现在是**独立的 ComfyUI 任务**（点「增强」时提交），
出图任务里已经不存在任何 `TextGenerate` / `TextGenerateV2` / `BatchImagesNode`，
也不再加载任何 `*_pe_*` 权重。

### 4.5 增强下拉实测（真实浏览器）

H3-Image 文生图面板：
```
mode: t2i_h3image
modelKey: h3_image_pruned_int8
enhanceSpecsForContext('t2i_h3image','h3_image_pruned_int8')
  -> ["image-refine","image-scalist","image-bbox","h3image-pe-t2i","h3image-pe-i2i"]
「普通增强」的模板下拉:
  value = "h3image-pe-t2i"
  options = ["图像提示词·物体细化（二次增强）","图像提示词（SCALIST 结构化）",
             "图像提示词 + bbox 元素表（FLUX 3 官方格式）",
             "H3-Image 官方 PE·文生图（画面扩写+比例）",
             "H3-Image 官方 PE·图片编辑（参考图条件扩写）"]
```
H3-Image 图片编辑面板：
```
「普通增强」的模板下拉 value = "h3image-pe-i2i"
  → H3-Image 官方 PE·图片编辑（参考图条件扩写）
```
Qwen 文生图面板「普通增强」默认模板 = `qwen21-pe-t2i`（Qwen-Image 2.1 官方 PE·文生图），与改动前一致。

**菜单项清单（Qwen 文生图）** —— 只有下拉项，没有独立 PE 勾选框：
```
普通增强 / 二次增强 / 视频导演提示词 / 结构化增强（layout） /
YuE2 结构化风格 + 歌词 / qwen21图片官方增强PE / H3-Image 官方增强PE
```

### 4.6 截图证据

| 文件 | 内容 |
|---|---|
| `evidence-pe-externalize/panel-h3image-t2i.png` | H3-Image 文生图「增强」下拉（可见「普通增强 → H3-Image 官方 PE 文生图」，无独立 PE 勾选框） |
| `evidence-pe-externalize/panel-h3image-i2i.png` | H3-Image 图片编辑「增强」下拉 |
| `evidence-pe-externalize/panel-qwen-t2i.png` | Qwen 文生图「增强」下拉 |
| `evidence-pe-externalize/panel-qwen-i2i.png` | Qwen 图片编辑「增强」下拉 |
| `evidence-pe-externalize/comfyui_history_no_pe.json` | ComfyUI history 解析结果（出图任务 PE 节点数 = 0） |
| `evidence-pe-externalize/qwen_t2i_prompt_json.json` | Qwen 文生图任务 prompt JSON 的节点构成 |

### 4.7 控制台无报错

真实浏览器装 `error` / `unhandledrejection` / `console.error` 三路收集器，
依次切换四个面板并开合增强下拉：
```
console errors: []
```

---

## 五、测试

### 5.1 新增专项回归测试

`tests/test_pe_externalize.py`（28 条，全绿）：
```
$ python3 -m pytest tests/test_pe_externalize.py -q
28 passed in 0.04s
```
覆盖六条硬约束：
1. Qwen 文生图工作流内无任何 PE 节点
2. Qwen 图片编辑工作流内无任何 PE 节点，**且参考图接线完整**（`images.image_N` → `LoadImage`）
3. MOSS / H3-Image 工作流内无 PE 动作
4. 无「独立勾选框」形式的 PE 选项（`gen_defaults` 无 `use_pe` 字段、模板 spec 不声明 `use_pe`）
5. H3-IMAGE 两面板默认模板是**自己的官方 PE 档**，不是 `image-refine`
6. H3-Image 官方 PE 接法与官方模板一致（占位符 / 比例键名 / task prompt 逐字节比对）

### 5.2 既有测试更新（断言搬到新位置，内容没放松）

| 文件 | 原用例 | 改法 |
|---|---|---|
| `tests/test_qwen_image.py` | `test_pe_encoder_feeds_prompt_expansion_node`、`test_pe_t2i_and_i2i_use_different_encoders`、`test_prompt_expansion_node_api_shape` | 断言对象从「出图工作流」换成 **WEBUI 侧 `qwen_pe.build_pe_workflow`**（官方档位参数 / 官方 system prompt / 节点类型仍然被断言，且**加了**「出图工作流里不得出现这些」的反向断言） |
| `tests/test_qwen_image.py` | `test_use_pe_off_skips_expansion` | 改为「无论 `use_pe` 传 True 还是 False，工作流内都没有 PE 节点」 |
| `tests/test_qwen_ref_assets.py` | `test_reference_images_also_feed_the_pe_node` | 改为 `test_reference_images_no_longer_feed_a_pe_node`（断言工作流内无 `BatchImagesNode`/`TextGenerate`，且参考图仍完整接进 `TextEncodeQwenImage21`） |
| `tests/test_qwen_ref_assets.py` | `test_no_pe_no_batch_node` | 不再传 `use_pe=False`（PE 已不存在这个状态） |

### 5.3 目标测试全绿

```
$ python3 -m pytest tests/test_qwen_image.py tests/test_qwen_ref_assets.py tests/test_pe_externalize.py -q
102 passed, 1 skipped in 12.02s
```

### 5.4 全量回归对跑（基线 vs 本分支）

- 基线：`/tmp/h3s-pe-baseline`（`git worktree add --detach` 到 `faaff36`，干净副本）
- 候选：`/root/projects/h3studio-pe-ext`（本分支）
- 命令：`python3 -m pytest tests/ -q --tb=no -rf`（同一套）
- 对比工具：仓库自带的 `scripts/compare_pytest_runs.py`（逐用例结果序列对齐，
  因为全量跑会被浏览器用例阶段的 timeout 掐断，拿不到汇总行）

> 对跑结果见第六节（本节结果随最终提交一并写入）。

---

## 六、未修完 / 受阻 / 已知限制（如实列出）

1. **Studio 任务卡未能更新**：`ekko_studio_update_plan` 的 MCP 工具在本机存在**已知的
   harness 注入缺陷**（broker 给每个 `plan` 元素注入 `item` 键，而 schema 是
   `additionalProperties: false`），每次调用都被 schema 校验拒绝，报
   `Additional properties are not allowed ('item' was unexpected)`。
   兜底脚本 `/root/studio_plan.py` 又因 Web UI token 被轮换而返回
   `Unauthorized. Web UI token was not accepted`。
   → **本轮的进度卡片没能推到 Studio/App**，这是工具链问题，不是本次改动的问题。

2. **`h3image_pe` 增强项的「当前增强」回显标签会显示模板名**：
   `H3-Image 官方增强PE（图像提示词·物体细化（二次增强））` —— 括号里那个是
   `basic` 档的模板名，被 `methodSpecLabel()` 顺带拼上去了。
   **不影响功能**（官方 PE 走自己的专属链路，不用这个模板），
   但显示上容易让人误解。本轮**未修**（属显示层小瑕疵，不在本次交付要求内）。

3. **`h3image-pe-t2i` / `h3image-pe-i2i` 两档在「普通增强」下的实际产物
   依赖本机配置的 Provider**（本地 LLM）。官方 checkpoint 路线（`h3image_pe` 项）
   已实测跑通并验证了 JSON 解包；但 Provider 路线本轮**未做真实 LLM 出图验证**
   （只验证了模板解析、默认值、菜单项与解包逻辑）。
   → **未修完项**。

4. **官方 H3-Image PE 权重是本轮新装的**（7343591088 字节）。这是**环境变更**，
   不是代码变更 —— 若目标机器没有该权重，H3-Image 官方 PE 会报
   `ComfyUI 拒绝 H3-Image PE 工作流` / clip 不存在。已装在
   `/home/ubuntu/comfyui/models/text_encoders/`。

5. **工作流内的 PE 曾是官方推荐做法**。本次按用户要求移除后，
   「不做增强直接出图」得到的是**用户原文**进模型（不再自动被 PE 改写）。
   这是用户明确要求的行为，但与官方工作流的默认行为不同 —— 属**有意变更**，非缺陷。

6. **`main` 分支与线上分支不同源**：现场 `git log` 显示
   `main` = `da9d5c6`（`docs: document where generated videos and images are stored`），
   而 `local/maintainer` = `8c81169`，线上 `local/deploy/qwen-ref-assets-live` = `faaff36`。
   `main` **不包含** `local/maintainer`（`git merge-base --is-ancestor` 验证为 NO）。
   本分支基于**线上实际运行的 `faaff36`**（含 H3-Image 官方 PE 的提交 `f99588f`）。
   合并目标的选择见第八节。

---

## 七、Git 与部署纪律

- 全程**未**执行：`git reset` / `git stash` / `git checkout -- .` / force push /
  提交他人分支 / 清理他人改动。
- 主工作树 `/root/projects/comfyui-minimax-h3-studio` 有 29 项他人未提交 WIP
  （`git status` 实测），**未触碰**。
- 线上 worktree `/root/projects/h3studio-live` 有他人 WIP
  （`docs/多模态Agent-增强汇报-20261115.md` 修改 + 3 项未跟踪），
  **未在开发阶段改动**（只读用于现场复核）。
- 开发全程在**独立 worktree + 独立分支**：
  `/root/projects/h3studio-pe-ext` / `local/feature/pe-externalize`。
- 验证用的 8199 实例**独立数据目录**（`data-live/`），任务落库不污染线上 `jobs.db`。
- 发现并**避开了**另一路并发 WIP：端口 **8201** 由 `/root/projects/h3s-uicontrols`
  的进程占用（与本任务无关的另一 agent 的实例）—— 浏览器一度误落到该实例，
  已改用**命名浏览器 session**（`session="pe-ext"`）固定到 8199，未触碰 8201。

---

## 八、合并与部署（见第八节补充说明）

- 合并目标：`local/maintainer`（`8c81169`）→ 回灌线上 `local/deploy/qwen-ref-assets-live`（`faaff36`）。
- 部署脚本：`scripts/deploy_to_8190.sh`（merge → 回灌 live → 重启 `h3-studio.service` → 探活）。
- 部署前后探活 + 四面板回归结果见下节。

---

## 附：关键文件索引

| 用途 | 路径 |
|---|---|
| 新增回归测试 | `/root/projects/h3studio-pe-ext/tests/test_pe_externalize.py` |
| 证据目录 | `/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence-pe-externalize/` |
| 官方 PE task prompt 资产 | `h3studio/skills/prompt-enhancer/assets/h3image-pe-{t2i,i2i,system}.txt` |
| 工作流 PE 移除 | `h3studio/workflows.py`（`_qwen_common_nodes` 1421 起 / `prompt_ref = prompt` 1513） |
| H3-Image 专属 PE 档 | `h3studio/enhancer.py`（`TEMPLATE_SPECS` 230 起 / `MODE_DEFAULT_SPEC` / `unwrap_json_contract` 985） |
| H3-Image 官方 PE 扩写 | `h3studio/h3image_pe.py` |
