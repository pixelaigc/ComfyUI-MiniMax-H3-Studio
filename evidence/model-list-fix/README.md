# 文生视频面板「模型清单」证据包

生成时间：2026-11-11
分支：`local/feature/model-list-fix`
worktree：`/root/projects/h3studio-model-list-fix`
验证实例：端口 **8191**（本分支代码，`H3STUDIO_PORT=8191`）
ComfyUI：端口 **8188**（未改动，ComfyUI 0.37.0）

> 说明：线上 8190（`h3-studio.service`）**未重启、未部署**，仍是改动前的代码。
> 本包所有接口与截图证据均来自 8191 的分支实例。
> 8190 的对应接口（未修复版）返回仍带旧的红字，**这是预期**。

---

## 1. 根因（只读定位结论）

| 项 | 结论 |
|---|---|
| 前端面板文件 | `web/video-panel.js`（`renderModelSelect` / `renderModelNote`）+ `web/index.html`（`#vidModelSelect` / `#vidModelNote` / `#vidModelWarn`） |
| 后端清单来源 | `h3studio/video_models.py`（`VIDEO_MODELS` 常量 + `list_models()` + `model_available()`） |
| 接口 | `GET /api/video-options`（`server.py::api_video_options`） |
| **那句红字从哪来** | `h3studio/video_models.py` 里 `VIDEO_MODELS["fl2va_pruned_int8"]["warn"]` 的**写死常量** |
| **它怎么判定「外硬盘权重不在下拉框」** | **根本没判定**。那句 warn 是无条件显示的字符串，跟 `available` 字段无任何逻辑关系 |
| **是否调用过 ComfyUI 模型列表 API** | **从未调用**。`model_available()` 只扫本地目录（`extra_model_paths.yaml` 声明的路径 + 写死的默认目录），从不超过问 ComfyUI |
| 面板 vs 提交 | 两条独立路径：面板看 `m.available`（扫盘），提交走 `normalize_t2v_params` → `model_available()`（也扫盘）—— 同源但都**不是** ComfyUI 的事实 |

---

## 2. ComfyUI 模型列表 API —— 实际接口与返回结构

### 2.1 实测可用的接口（ComfyUI 0.37.0）

| 接口 | 实测结果 | 返回结构 |
|---|---|---|
| `GET /models/diffusion_models` | **200**，7596 B | `["DarkBeastZ6-....safetensors", ...]` — **扁平字符串数组**，含子目录前缀（如 `h3/xxx.safetensors`） |
| `GET /models/loras` | **200**，39708 B | 同上，字符串数组（686 条） |
| `GET /object_info/UNETLoader` | **200**，8122 B | `{"UNETLoader": {"input": {"required": {"unet_name": [[...151 个选项...], {...}], ...}}}}` |
| `GET /models/unet` | **404** | `unet` 是 `diffusion_models` 的旧别名，HTTP 路由**不做**映射 |

**两个视角交叉验证**：目录视角（`/models/*`）与节点视角（`/object_info/<Node>.input.required.<slot>`）
实测**完全一致**，各 151 条 —— 这正是 ComfyUI 界面下拉框的数据来源
（`folder_paths.get_filename_list()`，同源）。

### 2.2 直接证据

```bash
$ curl -s http://127.0.0.1:8188/models/diffusion_models | python3 -c "import json,sys; print(len(json.load(sys.stdin)))"
151

$ curl -s http://127.0.0.1:8188/models/diffusion_models | grep -o 'minimax_h3[^"]*'
minimax_h3_fl2va_bf16.safetensors
minimax_h3_fl2va_int8_convrot.safetensors
minimax_h3_fl2va_pruned_int8_convrot.safetensors
minimax_h3_fl2va_pruned_w4a8_mixed.safetensors
minimax_h3_hybrid_fl2va_ref2va_b30-49-int8.safetensors
minimax_h3_ref2va_int8_convrot.safetensors
minimax_h3_ref2va_pruned_int8_convrot.safetensors
minimax_h3_ref2va_pruned_w4a8_mixed.safetensors
```

原始返回已存档：`curl/comfyui-direct-diffusion_models.json`、
`curl/comfyui-direct-object_info-UNETLoader.json`、`curl/comfyui-direct-loras.json`。

### 2.3 H3 权重实际存放路径（要求 4 的答案）

**结论：H3 权重全部真实存在，且 ComfyUI 已收录，无需修配置。**

```
$ mount | grep nvme1t_colorful
/dev/nvme2n1p1 on /mnt/nvme1t_colorful type ext4 (rw,noatime)

$ df -h /mnt/nvme1t_colorful
/dev/nvme2n1p1  938G  604G  287G  68%  /mnt/nvme1t_colorful     ← 已挂载

$ ls -la /mnt/nvme1t_colorful/comfyui-models/diffusion_models/ | grep minimax_h3
-rw-r--r-- 1 root root 66280487368 Sep  9 06:52 minimax_h3_fl2va_bf16.safetensors
-rw-r--r-- 1 root root 20970379616 Aug  5 09:14 minimax_h3_fl2va_pruned_int8_convrot.safetensors
-rw-r--r-- 1 root root 12540858008 Aug 13 10:37 minimax_h3_fl2va_pruned_w4a8_mixed.safetensors
-rw-r--r-- 1 root root 20970379632 Aug  5 14:50 minimax_h3_hybrid_fl2va_ref2va_b30-49-int8.safetensors
-rw-r--r-- 1 root root 34038894550 Aug  6 22:14 minimax_h3_ref2va_int8_convrot.safetensors

$ ls -la /home/ubuntu/comfyui/models/diffusion_models/ | grep minimax_h3
-rw-r--r-- 1 root root 34038892334 Aug  6 22:15 minimax_h3_fl2va_int8_convrot.safetensors   ← 唯一在本地盘的

$ grep -n "nvme1t_models" -A2 /home/ubuntu/comfyui/extra_model_paths.yaml
nvme1t_models:
    base_path: /mnt/nvme1t_colorful/comfyui-models/
```

**外置盘已挂载、`extra_model_paths.yaml` 已正确声明、ComfyUI 已收录** ——
所以那句「盘未挂载 / ComfyUI 下拉框确实没有这个单卡」**前提不成立，结论错误**。

---

## 3. 改造后：接口返回（8191 分支实例）

### 3.1 `GET /api/video-options`

```bash
$ curl -s http://127.0.0.1:8191/api/video-options | python3 -c "
import json,sys; d=json.load(sys.stdin)
print(d['model_list_source'], d['model_list_counts'], repr(d['model_list_error']))
for m in d['models']: print(' ', m['key'], m['status'], m['comfy_listed'])"
```

```
comfyui_api {'diffusion_models': 151, 'unet_enum': 151, 'loras': 686} ''
  fl2va_pruned_int8       available True
  fl2va_int8              available True
  fl2va_pruned_w4a8       available True
  fl2va_bf16              available True
  hybrid_fl2va_ref2va     available True
```

每条模型新增字段：
- `status` —— `available` / `external_missing` / `missing` / `unknown`（**运行时现算**）
- `comfy_listed` —— ComfyUI 下拉框里到底有没有这个名字
- `on_external_disk` —— 权重是否在外置盘
- `status_note` —— 按 status 现算的**如实**提示（不再是写死常量）

### 3.2 `GET /api/comfy-models`（新增诊断端点）

```bash
$ curl -s http://127.0.0.1:8191/api/comfy-models | python3 -c "
import json,sys; d=json.load(sys.stdin)
print(d['ok'], d['folder'], d['count'])
for m in d['panel_models']: print(' ', m['key'], 'listed=', m['listed'])"
```

```
True diffusion_models 151
  fl2va_pruned_int8 listed= True
  fl2va_int8 listed= True
  fl2va_pruned_w4a8 listed= True
  fl2va_bf16 listed= True
  hybrid_fl2va_ref2va listed= True
```

原始返回：`curl/comfy-models-diffusion_models.json`、`curl/comfy-models-loras.json`、
`curl/video-options.json`。

---

## 4. 前端参数链路验证（实机浏览器）

证据：`report.json`（完整）+ `model-list-{375,390,1280}.png` / `-zoom.png`（截图）

| 视口 | 下拉选中值 | 提交参数 model_key | 一致 | 横向溢出 | console 错误 | 页面异常 | 控件可见 |
|---|---|---|---|---|---|---|---|
| 375 | `fl2va_int8` | `fl2va_int8` | ✅ | **0 px** | 0 | 0 | ✅ |
| 390 | `fl2va_int8` | `fl2va_int8` | ✅ | **0 px** | 0 | 0 | ✅ |
| 1280 | `fl2va_int8` | `fl2va_int8` | ✅ | **0 px** | 0 | 0 | ✅ |

- **下拉项全部来自 ComfyUI 清单**：5 项全部 `comfy_listed=true`（`/api/comfy-models` 可复核）
- **切换模型后仍一致**：`after_switch` 三档均为
  `selected == submitModelKey == stateModel`
- **禁用项标注**：无禁用项（全部可用）；逻辑上不可用项会被标注
  「外置未挂载，当前不可选」并禁用（见 `tests/test_video_panel_model_dom.py`）
- **来源说明常驻**：`清单来源：ComfyUI 模型列表 API（151 个扩散模型 / 686 个 LoRA）`
- **红字区**：`已确认在 ComfyUI 模型列表中可用。` + 固有提醒（显存/兼容性）
- **禁止出现的断言**：`获取不到` / `尚未挂载` / `根本没有这个名字` /
  `下拉框确实没有` / `没有这个单卡` / `提交会被拒绝` ——
  三档视口**全部 0 命中**

---

## 5. 降级路径（ComfyUI 不可达时）

`GET /api/video-options` 此时返回：

```json
{"model_list_source": "filesystem_fallback",
 "model_list_error": "diffusion_models: 连不上 ComfyUI（...）：ClientConnectorError: ...；loras: ...；UNETLoader: ..."}
```

前端显示（红字，如实说明，**不谎称「获取不到模型」**）：
> 未能连上 ComfyUI 模型列表（<原因>）—— 当前清单来自本机磁盘扫描，可用性可能与 ComfyUI 下拉框不一致。

模型项降级标注：`本机磁盘扫描到该权重（当前未能连上 ComfyUI 模型列表，可用性待确认）。`

---

## 6. 文件清单

```
evidence/model-list-fix/
├── model-list-375.png / -zoom.png        375px 视口截图
├── model-list-390.png / -zoom.png        390px 视口截图
├── model-list-1280.png / -zoom.png      1280px 视口截图
├── report.json                           浏览器验收完整报告
└── curl/
    ├── comfyui-direct-diffusion_models.json   ComfyUI 原始返回（目录视角）
    ├── comfyui-direct-object_info-UNETLoader.json  ComfyUI 原始返回（节点视角）
    ├── comfyui-direct-loras.json         ComfyUI 原始 LoRA 列表
    ├── comfy-models-diffusion_models.json 新端点返回
    ├── comfy-models-loras.json           新端点返回
    └── video-options.json                面板清单返回
```
