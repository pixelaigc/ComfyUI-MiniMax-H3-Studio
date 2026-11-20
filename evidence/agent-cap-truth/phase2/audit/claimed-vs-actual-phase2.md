=== 本轮对照清单生成时间锚点 ===
代码基线：local/maintainer @ 7436883 + 本轮改动

## 一、flf / r2v 默认参数：声称 vs 实际

| 项 | 修正前声称（面板实际下发） | 修正后实际（真实配置派生） | 判定 |
|---|---|---|---|
| **flf** 分辨率选项 | [544p, 768p] | 28 档：288p…1440p | ❌ 旧界面遗留 → ✅ 真实上限派生 |
| **flf** 分辨率默认 | 768p | 768p | ✅（标签语义，MP 预算才是权威值） |
| **flf** resolution note | 544p = 0.4MP / 768p = 0.8MP | 按该模式的真实面积上限（2.0 MP）派生；也可以直接填下面的 MP 预算 × 百分比 | ❌ 写死旧口径 → ✅ 真实上限 |
| **flf** MP 预算字段 | (字段不存在) | megapixels（number，default=0.4, max=2.0） | ✅ 新增 |
| **flf** 百分比字段 | (字段不存在) | resolution_percent（number，default=100.0, max=200.0） | ✅ 新增 |
| **r2v** 分辨率选项 | [544p, 768p] | 28 档：288p…1440p | ❌ 旧界面遗留 → ✅ 真实上限派生 |
| **r2v** 分辨率默认 | 768p | 768p | ✅（标签语义，MP 预算才是权威值） |
| **r2v** resolution note | 544p = 0.4MP / 768p = 0.8MP | 按该模式的真实面积上限（2.0 MP）派生；也可以直接填下面的 MP 预算 × 百分比 | ❌ 写死旧口径 → ✅ 真实上限 |
| **r2v** MP 预算字段 | (字段不存在) | megapixels（number，default=0.4, max=2.0） | ✅ 新增 |
| **r2v** 百分比字段 | (字段不存在) | resolution_percent（number，default=100.0, max=200.0） | ✅ 新增 |

## 二、各 mode 的真实面积上限（MP 预算的基准）

| mode | 真实上限 | 32 对齐 | 单边范围 | 支持自定义宽高 | 来源 |
|---|---|---|---|---|---|
| t2v | 2.0 MP | 32 | 32~16384 | True | VIDEO_MODE_SIZE_POLICY |
| flf | 2.0 MP | 32 | 32~16384 | True | VIDEO_MODE_SIZE_POLICY |
| r2v | 2.0 MP | 32 | 32~16384 | True | VIDEO_MODE_SIZE_POLICY |
| t2i | 8.3 MP | 32 | 32~16384 | True | 该族真实档位表 |
| t2i_qwen | 4.0 MP | 32 | 32~16384 | True | 该族真实档位表 |
| t2i_moss | 4.0 MP | 32 | 32~16384 | True | 该族真实档位表 |
| t2i_h3image | 16.0 MP | 32 | 32~16384 | True | 该族真实档位表 |
| t2a / a2a（音乐） | 不适用（无画面尺寸） | — | — | — | music_yue2 只读时长/采样参数 |

## 三、Agent 能力自述：修正前 vs 修正后

### 修正前（硬编码档位白名单）

```
- **视频分辨率**：可指定短边档位（288p, 352p, ..., 1440p）（例如 480p = 864×480），也可直接给 width/height（宽高必须是 32 的倍数，单边 32~16384，总像素不超过 2.0 MP）。
```

### 修正后（读真实配置 + MP 预算 × 百分比）

```
- **视频分辨率**：统一用 **megapixels（MP 预算）× resolution_percent（百分比）** 表达 —— megapixels 是该模式的真实面积上限（当前 2.0 MP），resolution_percent 是相对这个上限的百分比（100% = 2.0 MP）。也可以直接给 width/height（宽高必须是 32 的倍数，单边 32~16384）。换算后宽高会自动对齐到 32 的倍数；超出真实上限时**自动缩到可用值并说明调整原因**（原始请求值 → 生效值 → 原因），不会静默改写。短边档位（288p, 352p, 384p, 416p, 448p, 480p, 512p, 544p, 576p, 608p, 640p, 672p, 704p, 736p, 768p, 800p, 832p, 864p, 896p, 928p, 960p, 1024p, 1088p, 1120p, 1184p, 1248p, 1376p, 1440p）（例如 480p = 864×480）仅作显示标签，用 get_mode_schema 看当前真实可用集合。- **视频时长**：2~60 秒（整数）。超上限会被明确拒绝并给出上限值。- **视频底模**（model_key）：fl2va_pruned_int8, fl2va_int8, fl2va_pruned_w4a8, fl2va_bf16, ref2va_pruned_int8, ref2va_int8, hybrid_fl2va_ref2va。参考生视频（r2v）必须用支持 Ref2VA 的权重：ref2va_pruned_int8, ref2va_int8, hybrid_fl2va_ref2va。- **注意力机制**（attention）：default, kitchen, sage, sol, sparse。- **加速 LoRA**：ckpt850, v4step600, lightx2v_4step_768p, lightx2v_4step_sla_768p, lightx2v_8step, dmad_4step_critic, speed_slider, better_motion, turbo_4step_cn, turbo_4step_cn_ema, lightx2v_4step_v01_comfy, lightx2v_sla_768p_root（另有 none = 不用加速）。- **精度**（precision）：int8, nvfp4。- **采样器 / 调度器**：sampler 68 种、scheduler 14 种，用 get_mode_schema 看清单。
```

## 四、存量默认值：声称 vs 实际（线上库实测）

线上库路径：`/root/projects/comfyui-minimax-h3-studio/data/jobs.db` -> 表 `model_provider_prefs` / key `agent_settings` / 字段 `gen_defaults`

| mode | 存量存储值（修正前） | 读时迁移后 | 像素是否不变 |
|---|---|---|---|
| flf | `resolution="768p"` | `megapixels=0.8, resolution_percent=100` | 是（0.8MP @16:9 = 1216×672） |
| r2v | `resolution="768p"` | `megapixels=0.8, resolution_percent=100` | 是（0.8MP @16:9 = 1216×672） |
| t2v | `megapixels=1.0` | `megapixels=1.0, resolution_percent=100` | 是（不变） |

> **口径说明**：存量 `768p` 是**旧口径**（`workflows.RES_MP` = 0.8 MP -> 1216×672），与**新口径**的短边档位 768p（1344×768 = 0.984 MP）不是同一族东西。迁移按旧口径反查，保证用户已配好的像素**不变**。

## 五、本轮修掉的真实缺陷（实测复现）

| # | 缺陷 | 复现 | 修法 |
|---|---|---|---|
| 1 | `max_pixels_for_mode` 读导入期快照 | 改 `VIDEO_MAX_CANVAS_PIXELS` -> `size_policy_for_mode` 报新值、`max_pixels_for_mode` 仍报旧值 | 改为复用 `size_policy_for_mode`（实时派生） |
| 2 | 显式宽高不吸附到 32 倍数 | 提交 1900×1000 -> 原样跑 1900×1000 | `h3_video_size.resolve_size` 增加 32 对齐 + 单边钳制 + 说明 |
| 3 | flf/r2v 默认参数写死旧两档 | 管理页下拉只有 544p/768p | 按各 mode 真实上限派生（`size_presets_for_aspect`） |
| 4 | t2v 默认 megapixels 与线上存量不一致 | 字段默认 0.4 / 线上存量 1.0 | 字段默认改从 `VIDEO_DEFAULT_MP` 真源派生 |
