# 「声称 vs 实际」对照清单 · 内置 Agent 管理页默认参数 + MP 预算 × 百分比

生成时间：2026-11-20T20:35:57.086139

代码基线：`local/maintainer` @ `7436883` + 本轮改动
线上：`local/deploy/qwen-ref-assets-live` @ `9c5fbf80`（8190）

## 一、flf / r2v 默认参数（用户实测反馈：仍是老配置）

| 项 | 修正前**声称**（线上实测抓取） | 修正后**实际**（真实配置派生） | 判定 |
|---|---|---|---|
| **flf** 分辨率选项 | 544p, 768p（2 档） | **28 档**（288p…1440p） | ❌ 旧界面遗留 → ✅ 真实上限派生 |
| **flf** 分辨率默认 | 768p | 768p（标签语义；MP 预算才是权威值） | ✅ |
| **flf** resolution note | 544p = 0.4MP / 768p = 0.8MP | 按该模式的真实面积上限（2.0 MP）派生；也可以直接填下面的 MP 预算 × 百分比 | ❌ 写死旧口径 → ✅ 真实上限 |
| **flf** MP 预算字段 | （不存在） | **megapixels**（number，default=0.4，max=2.0） | ✅ 新增 |
| **flf** 百分比字段 | （不存在） | **resolution_percent**（number，default=100.0，max=200.0） | ✅ 新增 |
| **flf** 底模字段 | （不存在） | **model_key**（4 个选项，默认 fl2va_pruned_int8） | ✅ 新增（r2v 线上 400 的根因） |
| **r2v** 分辨率选项 | 544p, 768p（2 档） | **28 档**（288p…1440p） | ❌ 旧界面遗留 → ✅ 真实上限派生 |
| **r2v** 分辨率默认 | 768p | 768p（标签语义；MP 预算才是权威值） | ✅ |
| **r2v** resolution note | 544p = 0.4MP / 768p = 0.8MP | 按该模式的真实面积上限（2.0 MP）派生；也可以直接填下面的 MP 预算 × 百分比 | ❌ 写死旧口径 → ✅ 真实上限 |
| **r2v** MP 预算字段 | （不存在） | **megapixels**（number，default=0.4，max=2.0） | ✅ 新增 |
| **r2v** 百分比字段 | （不存在） | **resolution_percent**（number，default=100.0，max=200.0） | ✅ 新增 |
| **r2v** 底模字段 | （不存在） | **model_key**（3 个选项，默认 ref2va_pruned_int8） | ✅ 新增（r2v 线上 400 的根因） |

## 二、各 mode 的真实面积上限（MP 预算的基准）

| mode | 真实上限 | 32 对齐 | 单边 | 自定义宽高 | 来源 |
|---|---|---|---|---|---|
| t2v | 2.0 MP | 32 | 32~16384 | True | VIDEO_MODE_SIZE_POLICY（实时派生） |
| flf | 2.0 MP | 32 | 32~16384 | True | VIDEO_MODE_SIZE_POLICY（实时派生） |
| r2v | 2.0 MP | 32 | 32~16384 | True | VIDEO_MODE_SIZE_POLICY（实时派生） |
| t2i | 8.3 MP | 32 | 32~16384 | ✅ | 该族真实档位表 |
| t2i_qwen | 4.0 MP | 32 | 32~16384 | ✅ | 该族真实档位表 |
| i2i_qwen | 4.0 MP | 32 | 32~16384 | ✅ | 该族真实档位表 |
| t2i_moss | 4.0 MP | 32 | 32~16384 | ✅ | 该族真实档位表 |
| t2i_h3image | 16.0 MP | 32 | 32~16384 | ✅ | 该族真实档位表 |
| i2i_h3image | 16.0 MP | 32 | 32~16384 | ✅ | 该族真实档位表 |
| t2a / a2a（音乐） | **不适用**（无画面尺寸概念） | — | — | — | music_yue2 只读时长/采样 |

## 三、Agent 能力自述

### 修正前

```
- **视频分辨率**：可指定短边档位（288p … 1440p）（例如 480p = 864×480），也可直接给 width/height（宽高必须是 32 的倍数，单边 32~16384，总像素不超过 2.0 MP）。
```

### 修正后（线上实测 list_modes 返回）

```
分辨率用 **megapixels（MP 预算）× resolution_percent（%）**：上限 2.0 MP，100% = 用满。也可给 width/height（32 的倍数）或短边档位（如 480p = 0.4MP）。超上限自动缩放并说明原因。完整清单用 get_mode_schema 查。
```

## 四、存量默认值迁移（线上实测）

| mode | 存量存储值 | 读时迁移后 | 落库像素 | 像素是否不变 |
|---|---|---|---|---|
| flf | `resolution="768p"` | `megapixels=0.8, resolution_percent=100` | 1216×672（0.779 MP） | **是** |
| r2v | `resolution="768p"` | `megapixels=0.8, resolution_percent=100` | 1216×672（0.779 MP） | **是** |
| t2v | `megapixels=1.0` | `megapixels=1.0, resolution_percent=100` | 不变 | **是** |

> 迁移按**旧口径**（`workflows.RES_MP`）反查 —— 历史上「768p」有两个不同含义（旧 0.8MP=1216×672 / 新短边 1344×768）。

## 五、本轮修掉的真实缺陷（全部有线上/实测复现）

| # | 缺陷 | 复现方式 | 实际症状 | 修法 |
|---|---|---|---|---|
| 1 | flf/r2v 默认参数写死旧两档 | 线上 GET /api/agent/settings | 下拉只有 544p/768p | 按各 mode 真实上限派生（28 档） |
| 2 | flf/r2v 无底模字段 | **线上提交 r2v** | HTTP 400「模型…不支持参考生成（Ref2VA）通道」 | 补 model_key 字段，选项按 mode 过滤 |
| 3 | 面板提交路径**根本不套**管理页默认参数 | 读 server.py | 管理页配的默认值对面板点生成毫无作用 | 新增 `apply_gen_defaults_and_size`，挂在 prepare_job_params（公共点） |
| 4 | 提交前不解析尺寸 | POST /api/jobs（只传 megapixels） | HTTP 400「宽高无效：宽高必须数字」 | 归一前先跑 `size_spec.plan_size` |
| 5 | 显式百分比被默认 MP 预算盖掉 | **线上提交** `resolution_percent=50` | 落库 1216×672（默认 0.8MP）而非 1376×768 | merge_into_params 里二选一 |
| 6 | `max_pixels_for_mode` 读导入期快照 | 改 `VIDEO_MAX_CANVAS_PIXELS` | 同真源两套行为（1.0 vs 2.0 MP） | 复用 `size_policy_for_mode`（实时派生） |
| 7 | 显式宽高不吸附 32 倍数 | 提交 width=1900 height=1000 | 原样跑 1900×1000（%32≠0） | plan_size 与 resolve_size 都吸附 + 说明 |
