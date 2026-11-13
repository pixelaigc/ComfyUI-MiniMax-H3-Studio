# 可实现性核查产出说明

本目录是 2026-11-14「功能可实现性核查」（审查目标 2）的核查工具与证据。

## 工具（可复跑）

| 文件 | 作用 |
|---|---|
| `comfy_probe_proxy.py` | ComfyUI **观测代理**（默认 :8211）。原样透明转发到真实 ComfyUI，只把 `POST /prompt` 的 workflow 图落盘到 `comfy-submissions/`。用于证明 UI 参数**真的进入了 ComfyUI 图**，而不是只改了前端状态。 |
| `p0_probe.py` | P0 疑点探针：`manual_sigmas` 是否真被消费、音频深度分析 backend 状态、enhancer 模板内容、`negative_prompt_ignored` 是否有前端出口。 |
| `upscale_path_probe.py` | 直接调 `workflows.build_t2i()`，对比 1MP 直出 vs ≥3MP 放大路径的图内采样参数。 |

## 证据（`comfy-submissions/`）

| 文件 | 内容 |
|---|---|
| `prompt-001.json` | Z-Image 文生图，steps=13/cfg=4.5/shift=6.5/dpmpp_3m_sde/karras/1024×576 → **全部进图**（PASS 对照组） |
| `prompt-002.json` | H3 文生视频，steps=7/shift_video=9/shift_audio=4/dpm_2_ancestral/sgm_uniform/768×448 → **全部进图** |
| `prompt-003.json` | Z-Image + 效果 LoRA → 图里有真实 `LoraLoader` 节点（`57:400`），strength=1.0 |
| `prompt-004.json` | **Qwen-Image 2.1 Turbo，UI 设 steps=40 → 图里完全没有 steps 键**，只有 9 段固定 `ManualSigmas`（假控件证据） |
| `prompt-005.json` | **Z-Image 4K 档，用户设 steps=37/cfg=6.5 → 图里 KSampler 是 steps=8/cfg=1.0/res_multistep/simple**（被写死覆盖证据） |
| `prompt-006.json` | Z-Image 1MP 档，同样用户参数 → KSampler steps=37/cfg=6.5/dpmpp_3m_sde/karras（与 prompt-005 同参数不同路径的对照） |
| `prompt-007.json` | YuE2 文生音乐，steps=12/cfg=2.5/dpm_2/sgm_uniform/temp=0.85/top_p=0.88/top_k=42/rep=1.05 → **全部进图** |

## 复跑方法

```bash
# 1) 起观测代理
PROBE_UPSTREAM=http://127.0.0.1:8188 \
PROBE_LOG_DIR=<repo>/evidence/feasibility/comfy-submissions \
  /home/ubuntu/comfyui/venv/bin/python evidence/feasibility/comfy_probe_proxy.py 8211

# 2) 让独立实例指向代理（config.json 的 comfyui_url 改成 http://127.0.0.1:8211）
#    再起实例：H3STUDIO_PORT=8210 H3STUDIO_DATA_DIR=<独立 data 目录> python server.py

# 3) 在浏览器点「生成」，代理会把 ComfyUI 收到的图落盘到 comfy-submissions/
```

> 注意：实例必须用**独立端口 + 独立 data_dir**（`H3STUDIO_PORT` / `H3STUDIO_DATA_DIR`），
> 否则会和线上 8190 共用 `jobs.db`，把别人的任务写进自己的库。
