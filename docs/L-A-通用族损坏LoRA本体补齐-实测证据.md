# L-A 实测证据：通用族 3 个损坏 LoRA 本体补齐

生成时间：2026-11-15
执行环境：/root/projects/h3studio-labc（分支 local/feature/legacy-lo-cleanup）

---

## 1. 定位（脚本逐一定位，不靠印象）

扫描脚本：`scripts/scan_damaged_loras.py`（在 live worktree 上跑，扫 712 个 safetensors）

```
scanned_total: 712
damaged_total: 3
```

| # | 路径 | 磁盘字节数 | 头部声明字节数 | 缺失字节 | 错误类型 |
|---|---|---|---|---|---|
| 1 | `/mnt/usb2t/comfyui-models/loras/AWPortrait-QW/AWPortrait-QW.safetensors` | **0** | 无法解析（连 8 字节头都没有） | — | `header_too_short`（0 字节空文件） |
| 2 | `/mnt/usb2t/comfyui-models/loras/QIE-SRD/Qwen-Image-Edit-Semi-Realistic-Detailer-v1.safetensors` | **0** | 无法解析（连 8 字节头都没有） | — | `header_too_short`（0 字节空文件） |
| 3 | `/mnt/usb2t/comfyui-models/loras/ipadapter/ip-adapter-faceid-plus_sd15_lora.safetensors` | **50,818,772** | 51,059,544（头长 39,760 + 数据区 51,019,784） | **240,772** | `truncated_body` + `SafetensorError: incomplete metadata, file not fully covered` |

**与 D2 同型**：#1/#2 是 0 字节空文件（下载/拷贝未开始）；#3 是「头完好、数据区被截断」。

> 注：仓库 `h3studio/loras.py:405-408` 早就记录了这 3 个文件名与字节数，但**只记录、没有修**。

---

## 2. 上游来源判定（逐个确认，含确切 URL + 字节数 + 校验值）

### #1 AWPortrait-QW.safetensors

- 上游仓库：`https://huggingface.co/Shakker-Labs/AWPortrait-QW`（另有镜像 `h1233211/AWPortrait-QW`，两者 etag 相同）
- 上游**没有** `AWPortrait-QW.safetensors` 这个文件名，只有 `AWPortrait-QW_1.0.safetensors`
  → `HEAD .../AWPortrait-QW.safetensors` = **HTTP 404**
- 本机已存在 `AWPortrait-QW_1.0.safetensors`（472,055,792 B），其 SHA256 =
  `9c1d8a829bcd5fedf5274cf27a683d37be9fcd3f1a2296ca66ee135d251ff7b3`
  与上游 `x-linked-etag: "9c1d8a829bcd5fedf5274cf27a683d37be9fcd3f1a2296ca66ee135d251ff7b3"` **完全一致**
- 判定：0 字节文件是同内容文件的**下载残留**（文件名少了 `_1.0`）→ 用同源完整文件补齐

### #2 Qwen-Image-Edit-Semi-Realistic-Detailer-v1.safetensors

- 上游 HF：`https://huggingface.co/rzgar/Qwen-Image-Edit-semi-realistic-detailer/resolve/main/Qwen-Image-Edit-Anime-Semi-Realistic-Detailer-v1.safetensors`
  - `x-linked-size: 755106832`
  - `x-linked-etag: "5b376624ff76cf94a587b95f01b75871aa57e41d313ea2f6cbfe8755b2293f91"`
- 上游 CivitAI（权威出处）：model `2129322` / version `2408631`
  - 文件名 `Qwen-Image-Edit-Semi-Realistic-Detailer-v1.safetensors`
  - `sizeKB = 737409.015625` → 755,106,832 字节
  - `SHA256 = 5B376624FF76CF94A587B95F01B75871AA57E41D313EA2F6CBFE8755B2293F91`
- **HF 镜像与 CivitAI 权威 SHA256 逐字节一致** → 判定为同一文件
- 下载实测：755,106,832 B / SHA256 `5b376624ff76cf94a587b95f01b75871aa57e41d313ea2f6cbfe8755b2293f91` ✅

### #3 ip-adapter-faceid-plus_sd15_lora.safetensors

- 上游：`https://huggingface.co/h94/IP-Adapter-FaceID/resolve/main/ip-adapter-faceid-plus_sd15_lora.safetensors`
  - `x-linked-size: 51059544`
  - `x-linked-etag: "3f00341d11e5e7b5aadf63cbdead09ef82eb28669156161cf1bfc2105d4ff1cd"`
- 下载实测：51,059,544 B / SHA256 `3f00341d11e5e7b5aadf63cbdead09ef82eb28669156161cf1bfc2105d4ff1cd` ✅

---

## 3. 补齐结果（原子替换 `cp` → `.tmp_install` → `mv`）

脚本：`scripts/restore_damaged_loras.sh`；日志：`/tmp/labc_restore.log`

| 文件 | 补齐前 | 补齐后 | SHA256 | 上游权威值一致 |
|---|---|---|---|---|
| AWPortrait-QW.safetensors | 0 B | **472,055,792 B** | `9c1d8a82…ff7b3` | ✅ |
| Qwen-Image-Edit-Semi-Realistic-Detailer-v1.safetensors | 0 B | **755,106,832 B** | `5b376624…93f91` | ✅ |
| ip-adapter-faceid-plus_sd15_lora.safetensors | 50,818,772 B | **51,059,544 B** | `3f00341d…ff1cd` | ✅ |

补齐后本体完整性（`scripts/verify_labc_lora_attach.py`）：

| 文件 | 磁盘字节 | 头部声明完整长度 | 缺失字节 | 张量数 | `safetensors.safe_open()` |
|---|---|---|---|---|---|
| AWPortrait-QW.safetensors | 472,055,792 | 472,055,792 | **0** | 1440 | ✅ ok |
| Qwen-Image-Edit-…-Detailer-v1.safetensors | 755,106,832 | 755,106,832 | **0** | 960 | ✅ ok |
| ip-adapter-faceid-plus_sd15_lora.safetensors | 51,059,544 | 51,059,544 | **0** | 256 | ✅ ok |

---

## 4. 真实 ComfyUI 加载路径验证（patches attached / missing）

脚本：`scripts/probe_labc_attach_real.py`
方法（**与线上完全同路径**，见 `comfy/sd.py:115`）：

```
comfy.sd.load_diffusion_model(<底模>)
  → comfy.lora.model_lora_keys_unet(model.model, {})
  → comfy.lora.load_lora(lora, key_map, log_missing=True)
```

证据文件：
- `/tmp/labc_attach_qie.json`（Qwen-Image-Edit 2509 + QI21 底模）
- `/tmp/labc_attach_final2.json`（SDXL + Qwen-Image-Edit 2509 + 正对照）
- `/tmp/labc_attach_real.json`（QI21 + SDXL）

### 4.1 在**各自真实目标底模**上（决定性证据）

底模 `Qwen_Image_Edit_2509_FP8`
（`/mnt/nvme1t_colorful/comfyui-models/diffusion_models/Qwen_Image_Edit_2509_FP8.safetensors`，
keymap_size=6281，`keymap_has_transformer_alias=true`）：

| LoRA | 张量数 | **patched** | **missing** |
|---|---|---|---|
| `AWPortrait-QW.safetensors` | 1440 | **720** | **0** |
| `Qwen-Image-Edit-Semi-Realistic-Detailer-v1.safetensors` | 960 | **480** | **0** |

→ **两条都满足 patches attached > 0 且 missing = 0。**

### 4.2 正对照（证明探针链路本身是活的，不是「什么都没挂上」）

底模 `sd_xl_base_unet_fp16`：

| LoRA | 张量数 | patched | missing |
|---|---|---|---|
| `ip-adapter-faceid-plusv2_sdxl_lora.safetensors`（同族，未损坏） | 1680 | **560** | **0** |
| `ip-adapter-faceid-plus_sd15_lora.safetensors`（本次补齐） | 256 | 64 | 128 |

### 4.3 #3 的真实结论：**本机客观没有它需要的底模**

`ip-adapter-faceid-plus_sd15_lora` 是 **SD1.5** 专用的 IP-Adapter LoRA
（键名 `lora_unet_down_blocks_0_attentions_0_transformer_blocks_0_attn1_to_k.*`）。

本机**全盘穷举**所有 ComfyUI 模型根（`extra_model_paths.yaml` 声明的 6 个 + 默认目录）的
`unet/` `diffusion_models/` `checkpoints/`：

| 候选 | 状态 |
|---|---|
| `iclight_sd15_fc.safetensors` | **自身也损坏**：254,990,739 B / 声明 1,719,148,312 B，**缺 1,464,157,573 B** |
| `iclight_sd15_fbc.safetensors` | **自身也损坏**：228,503,422 B / 声明 1,719,171,352 B，**缺 1,490,667,930 B** |

→ **本机没有任何可用的 SD1.5 底模**，所以这个 LoRA 在本机**没有可挂载的目标**。
补齐本体已做到（字节数 = 头部声明，缺 0 字节，`safe_open` 通过），
但「patches attached > 0」这条**在当前环境客观不可达成** —— 这是**如实报告的阻塞项**，
不是伪造，也不是占位。

> 这两个 `iclight_sd15_*` 自身损坏，是**另一个独立缺陷**（它们不在任何族的
> 效果目录里，从未进入用户可选列表），**不在本任务范围**（本任务只做 L-A/L-B/L-C 三项），
> 如实记录但未擅自扩大范围去修。

---

## 5. `/api/image-loras` 不再下发损坏文件（compatible=true 的坏文件 = 0）

实测（`scripts/verify_labc_lora_attach.py` 的 `api` 段）：

| base | available | incompatible | **available 内 compatible=false** | 3 个损坏文件命中数 |
|---|---|---|---|---|
| Z-Image | 195 | 0 | **0** | **0** |
| Qwen-Image 2.1 | 67 | 0 | **0** | **0** |
| MOSS-Image | 139 | 0 | **0** | **0** |

**为什么命中数是 0**：这 3 个文件在**通用目录**（`AWPortrait-QW/`、`QIE-SRD/`、`ipadapter/`），
不属于任何族的**效果专属目录**（`z-image-effects/`、`qwen21-effects/`、`moss-effects/`），
文件名也不含任何族 marker → 从设计上就不进任何族的可选列表。
（已用 `_looks_like_z_image_layers()` / marker 命中检查逐个确认，见定位脚本。）

**fail-closed 行为保持**：人为截断副本仍判
`conversion_status=broken` / `compatible=false` / `base_verdict=不可用`，
并给出如实原因「实际 25,529,772 字节，头部声明需要 51,059,544 字节，缺 25,529,772 字节」。

---

## 6. 已知限制与阻塞项（如实记录）

1. **`ip-adapter-faceid-plus_sd15_lora.safetensors` 无法在本机实测 patches attached > 0**：
   本机无 SD1.5 底模（两个候选 `iclight_sd15_*` 自身损坏）。本体已补齐并通过 SHA256 +
   完整性校验；挂载验证阻塞在「没有目标底模」这一客观事实上。
2. **两个 `iclight_sd15_*` 文件自身损坏**（各缺 ~1.47 GB）—— 独立缺陷，
   本任务范围外，未擅自修。
3. 上述 3 个 LoRA **不在本工作台任何一个可选列表里**（通用目录 + 非族目录 + 无族 marker），
   所以本轮补齐属于「消除磁盘上的损坏本体」，而不是「让用户多一个可选项」。
   若将来它们被移入族专属目录，底模缺失问题才会显现。
4. 未做：#1/#2 的**完整出图对比**（它们的目标底模 `Qwen_Image_Edit_2509_FP8` 不在本工作台
   的模型清单里 —— 工作台 Qwen 底模是 `qwen_image_2.1_bf16`）。本任务目标是
   「文件本体可用 + 真实挂载 patched/missing 数值」，这两点已实测闭环。
