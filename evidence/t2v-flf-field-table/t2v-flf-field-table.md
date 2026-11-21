# t2v / flf / r2v 面板字段对照表

字段表真源：`h3studio/gen_defaults.py::_mode_fields(mode)`（与服务端 `/api/generation-types` 下发给前端的同一份）。

| 字段 | 文生视频（t2v） | 首尾帧生视频（flf） | 参考生视频（r2v） | 语义归类 |
|---|---|---|---|---|
| `model_key` | `enum` 底模（7 项） | `enum` 底模（4 项） | `enum` 底模（3 项） | 其他 |
| `attention` | `enum` 注意力机制（5 项） | — | — | 其他 |
| `accel_lora` | `enum` 加速 LoRA 组合（10 项） | — | — | 加速 LoRA |
| `aspect` | `enum` 画面比例（8 项） | `enum` 画面比例（8 项） | `enum` 画面比例（8 项） | 尺寸 |
| `megapixels` | `number` MP 预算（百万像素） | `number` MP 预算（百万像素） | `number` MP 预算（百万像素） | 尺寸 |
| `resolution_percent` | `number` MP 预算百分比（%） | `number` MP 预算百分比（%） | `number` MP 预算百分比（%） | 尺寸 |
| `duration` | `number` 时长（秒） | `number` 时长（秒） | `number` 时长（秒） | 其他 |
| `steps` | `integer` 采样步数 | — | — | 固定项（只读） |
| `sampler` | `enum` 采样器（68 项） | — | — | 固定项（只读） |
| `scheduler` | `enum` 调度器（14 项） | — | — | 固定项（只读） |
| `shift_video` | `number` 视频 sigma shift | — | — | 固定项（只读） |
| `shift_audio` | `number` 音频 sigma shift | — | — | 固定项（只读） |
| `lora` | — | `enum` 加速 LoRA（3 项） | `enum` 加速 LoRA（3 项） | 加速 LoRA |
| `lora_strength` | — | `number` 加速 LoRA 强度 | `number` 加速 LoRA 强度 | 加速 LoRA |
| `sampling_fixed` | — | `fixed` 采样档位（不可改） | `fixed` 采样档位（不可改） | 固定项（只读） |
| `shift_fixed` | — | `fixed` sigma shift（不可改） | `fixed` sigma shift（不可改） | 固定项（只读） |
| `precision_fixed` | — | `fixed` 底模精度（不可改） | `fixed` 底模精度（不可改） | 固定项（只读） |