/* ============================================================
   文生视频面板 —— **静态兜底清单**（离线 / 后端异常时用）
   ------------------------------------------------------------
   为什么需要这个文件：
     2026-11-11 用户实机报「文生视频面板下拉全部空白」。
     根因：loadVideoOptions() 在 /api/video-options 失败时直接进 catch，
     renderVideoPanel() **从未执行** —— 于是尺寸预设、模型、加速 LoRA、
     注意力机制**全部**是空下拉（控件在、选项没有），
     只有一个空的 .img-warn 元素还带着橙色背景挂在模型框下面。

     这份清单让面板在拿不到服务端时**仍能渲染出可用的选项**，
     而不是一片空白。

   ⚠️ 形状必须与服务端 h3studio/video_models.py 的 options_payload() **完全一致**
     （size_presets 是**扁平 list**，不是按 aspect 分组的 dict ——
     形状错了会在 renderSizePresetSelect 里抛
     "object is not iterable"，反而把面板搞崩）。

   ⚠️ 这不是「模型一定可用」的承诺 —— 兜底态下 status 一律是
     "unknown"，界面上会明确提示「未能确认，可用性未知」。
   ============================================================ */
window.VIDEO_PANEL_FALLBACK = {
  "size_presets": [
    {
      "key": "1:1_0.2",
      "label": "448p · 448×448",
      "aspect": "1:1",
      "megapixels": 0.2,
      "width": 448,
      "height": 448,
      "short_edge": 448
    },
    {
      "key": "1:1_0.3",
      "label": "576p · 576×576",
      "aspect": "1:1",
      "megapixels": 0.3,
      "width": 576,
      "height": 576,
      "short_edge": 576
    },
    {
      "key": "1:1_0.4",
      "label": "640p · 640×640",
      "aspect": "1:1",
      "megapixels": 0.4,
      "width": 640,
      "height": 640,
      "short_edge": 640
    },
    {
      "key": "1:1_0.5",
      "label": "736p · 736×736",
      "aspect": "1:1",
      "megapixels": 0.5,
      "width": 736,
      "height": 736,
      "short_edge": 736
    },
    {
      "key": "2:3_0.2",
      "label": "384p · 384×576",
      "aspect": "2:3",
      "megapixels": 0.2,
      "width": 384,
      "height": 576,
      "short_edge": 384
    },
    {
      "key": "2:3_0.3",
      "label": "448p · 448×672",
      "aspect": "2:3",
      "megapixels": 0.3,
      "width": 448,
      "height": 672,
      "short_edge": 448
    },
    {
      "key": "2:3_0.4",
      "label": "544p · 544×800",
      "aspect": "2:3",
      "megapixels": 0.4,
      "width": 544,
      "height": 800,
      "short_edge": 544
    },
    {
      "key": "2:3_0.5",
      "label": "576p · 576×896",
      "aspect": "2:3",
      "megapixels": 0.5,
      "width": 576,
      "height": 896,
      "short_edge": 576
    },
    {
      "key": "2:3_0.6",
      "label": "640p · 640×960",
      "aspect": "2:3",
      "megapixels": 0.6,
      "width": 640,
      "height": 960,
      "short_edge": 640
    },
    {
      "key": "2:3_0.7",
      "label": "704p · 704×1056",
      "aspect": "2:3",
      "megapixels": 0.7,
      "width": 704,
      "height": 1056,
      "short_edge": 704
    },
    {
      "key": "2:3_0.8",
      "label": "736p · 736×1120",
      "aspect": "2:3",
      "megapixels": 0.8,
      "width": 736,
      "height": 1120,
      "short_edge": 736
    },
    {
      "key": "3:2_0.2",
      "label": "384p · 576×384",
      "aspect": "3:2",
      "megapixels": 0.2,
      "width": 576,
      "height": 384,
      "short_edge": 384
    },
    {
      "key": "3:2_0.3",
      "label": "448p · 672×448",
      "aspect": "3:2",
      "megapixels": 0.3,
      "width": 672,
      "height": 448,
      "short_edge": 448
    },
    {
      "key": "3:2_0.4",
      "label": "544p · 800×544",
      "aspect": "3:2",
      "megapixels": 0.4,
      "width": 800,
      "height": 544,
      "short_edge": 544
    },
    {
      "key": "3:2_0.5",
      "label": "576p · 896×576",
      "aspect": "3:2",
      "megapixels": 0.5,
      "width": 896,
      "height": 576,
      "short_edge": 576
    },
    {
      "key": "3:2_0.6",
      "label": "640p · 960×640",
      "aspect": "3:2",
      "megapixels": 0.6,
      "width": 960,
      "height": 640,
      "short_edge": 640
    },
    {
      "key": "3:2_0.7",
      "label": "704p · 1056×704",
      "aspect": "3:2",
      "megapixels": 0.7,
      "width": 1056,
      "height": 704,
      "short_edge": 704
    },
    {
      "key": "3:2_0.8",
      "label": "736p · 1120×736",
      "aspect": "3:2",
      "megapixels": 0.8,
      "width": 1120,
      "height": 736,
      "short_edge": 736
    },
    {
      "key": "3:4_0.2",
      "label": "384p · 384×544",
      "aspect": "3:4",
      "megapixels": 0.2,
      "width": 384,
      "height": 544,
      "short_edge": 384
    },
    {
      "key": "3:4_0.3",
      "label": "480p · 480×640",
      "aspect": "3:4",
      "megapixels": 0.3,
      "width": 480,
      "height": 640,
      "short_edge": 480
    },
    {
      "key": "3:4_0.4",
      "label": "576p · 576×736",
      "aspect": "3:4",
      "megapixels": 0.4,
      "width": 576,
      "height": 736,
      "short_edge": 576
    },
    {
      "key": "3:4_0.5",
      "label": "640p · 640×832",
      "aspect": "3:4",
      "megapixels": 0.5,
      "width": 640,
      "height": 832,
      "short_edge": 640
    },
    {
      "key": "3:4_0.6",
      "label": "672p · 672×928",
      "aspect": "3:4",
      "megapixels": 0.6,
      "width": 672,
      "height": 928,
      "short_edge": 672
    },
    {
      "key": "3:4_0.7",
      "label": "736p · 736×992",
      "aspect": "3:4",
      "megapixels": 0.7,
      "width": 736,
      "height": 992,
      "short_edge": 736
    },
    {
      "key": "4:3_0.2",
      "label": "384p · 544×384",
      "aspect": "4:3",
      "megapixels": 0.2,
      "width": 544,
      "height": 384,
      "short_edge": 384
    },
    {
      "key": "4:3_0.3",
      "label": "480p · 640×480",
      "aspect": "4:3",
      "megapixels": 0.3,
      "width": 640,
      "height": 480,
      "short_edge": 480
    },
    {
      "key": "4:3_0.4",
      "label": "576p · 736×576",
      "aspect": "4:3",
      "megapixels": 0.4,
      "width": 736,
      "height": 576,
      "short_edge": 576
    },
    {
      "key": "4:3_0.5",
      "label": "640p · 832×640",
      "aspect": "4:3",
      "megapixels": 0.5,
      "width": 832,
      "height": 640,
      "short_edge": 640
    },
    {
      "key": "4:3_0.6",
      "label": "672p · 928×672",
      "aspect": "4:3",
      "megapixels": 0.6,
      "width": 928,
      "height": 672,
      "short_edge": 672
    },
    {
      "key": "4:3_0.7",
      "label": "736p · 992×736",
      "aspect": "4:3",
      "megapixels": 0.7,
      "width": 992,
      "height": 736,
      "short_edge": 736
    },
    {
      "key": "9:16_0.2",
      "label": "352p · 352×608",
      "aspect": "9:16",
      "megapixels": 0.2,
      "width": 352,
      "height": 608,
      "short_edge": 352
    },
    {
      "key": "9:16_0.3",
      "label": "416p · 416×736",
      "aspect": "9:16",
      "megapixels": 0.3,
      "width": 416,
      "height": 736,
      "short_edge": 416
    },
    {
      "key": "9:16_0.4",
      "label": "480p · 480×864",
      "aspect": "9:16",
      "megapixels": 0.4,
      "width": 480,
      "height": 864,
      "short_edge": 480
    },
    {
      "key": "9:16_0.5",
      "label": "544p · 544×960",
      "aspect": "9:16",
      "megapixels": 0.5,
      "width": 544,
      "height": 960,
      "short_edge": 544
    },
    {
      "key": "9:16_0.6",
      "label": "608p · 608×1056",
      "aspect": "9:16",
      "megapixels": 0.6,
      "width": 608,
      "height": 1056,
      "short_edge": 608
    },
    {
      "key": "9:16_0.7",
      "label": "640p · 640×1152",
      "aspect": "9:16",
      "megapixels": 0.7,
      "width": 640,
      "height": 1152,
      "short_edge": 640
    },
    {
      "key": "9:16_0.8",
      "label": "672p · 672×1216",
      "aspect": "9:16",
      "megapixels": 0.8,
      "width": 672,
      "height": 1216,
      "short_edge": 672
    },
    {
      "key": "9:16_0.9",
      "label": "736p · 736×1280",
      "aspect": "9:16",
      "megapixels": 0.9,
      "width": 736,
      "height": 1280,
      "short_edge": 736
    },
    {
      "key": "9:16_0.98",
      "label": "768p · 768×1344",
      "aspect": "9:16",
      "megapixels": 0.98,
      "width": 768,
      "height": 1344,
      "short_edge": 768
    },
    {
      "key": "16:9_0.2",
      "label": "352p · 608×352",
      "aspect": "16:9",
      "megapixels": 0.2,
      "width": 608,
      "height": 352,
      "short_edge": 352
    },
    {
      "key": "16:9_0.3",
      "label": "416p · 736×416",
      "aspect": "16:9",
      "megapixels": 0.3,
      "width": 736,
      "height": 416,
      "short_edge": 416
    },
    {
      "key": "16:9_0.4",
      "label": "480p · 864×480",
      "aspect": "16:9",
      "megapixels": 0.4,
      "width": 864,
      "height": 480,
      "short_edge": 480
    },
    {
      "key": "16:9_0.5",
      "label": "544p · 960×544",
      "aspect": "16:9",
      "megapixels": 0.5,
      "width": 960,
      "height": 544,
      "short_edge": 544
    },
    {
      "key": "16:9_0.6",
      "label": "608p · 1056×608",
      "aspect": "16:9",
      "megapixels": 0.6,
      "width": 1056,
      "height": 608,
      "short_edge": 608
    },
    {
      "key": "16:9_0.7",
      "label": "640p · 1152×640",
      "aspect": "16:9",
      "megapixels": 0.7,
      "width": 1152,
      "height": 640,
      "short_edge": 640
    },
    {
      "key": "16:9_0.8",
      "label": "672p · 1216×672",
      "aspect": "16:9",
      "megapixels": 0.8,
      "width": 1216,
      "height": 672,
      "short_edge": 672
    },
    {
      "key": "16:9_0.9",
      "label": "736p · 1280×736",
      "aspect": "16:9",
      "megapixels": 0.9,
      "width": 1280,
      "height": 736,
      "short_edge": 736
    },
    {
      "key": "16:9_0.98",
      "label": "768p · 1344×768",
      "aspect": "16:9",
      "megapixels": 0.98,
      "width": 1344,
      "height": 768,
      "short_edge": 768
    },
    {
      "key": "21:9_0.2",
      "label": "288p · 704×288",
      "aspect": "21:9",
      "megapixels": 0.2,
      "width": 704,
      "height": 288,
      "short_edge": 288
    },
    {
      "key": "21:9_0.3",
      "label": "352p · 864×352",
      "aspect": "21:9",
      "megapixels": 0.3,
      "width": 864,
      "height": 352,
      "short_edge": 352
    },
    {
      "key": "21:9_0.4",
      "label": "416p · 992×416",
      "aspect": "21:9",
      "megapixels": 0.4,
      "width": 992,
      "height": 416,
      "short_edge": 416
    },
    {
      "key": "21:9_0.5",
      "label": "480p · 1120×480",
      "aspect": "21:9",
      "megapixels": 0.5,
      "width": 1120,
      "height": 480,
      "short_edge": 480
    },
    {
      "key": "21:9_0.6",
      "label": "512p · 1216×512",
      "aspect": "21:9",
      "megapixels": 0.6,
      "width": 1216,
      "height": 512,
      "short_edge": 512
    },
    {
      "key": "21:9_0.7",
      "label": "576p · 1312×576",
      "aspect": "21:9",
      "megapixels": 0.7,
      "width": 1312,
      "height": 576,
      "short_edge": 576
    },
    {
      "key": "21:9_0.8",
      "label": "608p · 1408×608",
      "aspect": "21:9",
      "megapixels": 0.8,
      "width": 1408,
      "height": 608,
      "short_edge": 608
    },
    {
      "key": "21:9_0.9",
      "label": "640p · 1472×640",
      "aspect": "21:9",
      "megapixels": 0.9,
      "width": 1472,
      "height": 640,
      "short_edge": 640
    },
    {
      "key": "21:9_0.98",
      "label": "672p · 1536×672",
      "aspect": "21:9",
      "megapixels": 0.98,
      "width": 1536,
      "height": 672,
      "short_edge": 672
    }
  ],
  "models": [
    {
      "key": "fl2va_pruned_int8",
      "label": "MiniMax-H3 FL2VA · 剪枝 int8（默认）",
      "unet": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
      "note": "剪枝版 int8（20.9 GB），H3 Turbo 4 步的常规选择。剪枝把 AdaLN 输入压到 8 维，Turbo LoRA 的 AdaLN 部分由ComfyUI-MiniMax-H3-Turbo 在运行期重新注入。",
      "location": "/mnt/nvme1t_colorful/comfyui-models/diffusion_models",
      "size_mb": 20970,
      "steps": 4,
      "sampler": "euler",
      "scheduler": "beta",
      "cfg": 1.0,
      "available": true,
      "status": "unknown",
      "warn": "权重在外接盘 /mnt/nvme1t_colorful —— 盘未挂载时 ComfyUI 下拉里根本没有这个名字，提交会被拒绝。"
    },
    {
      "key": "fl2va_int8",
      "label": "MiniMax-H3 FL2VA · 完整 int8（保真最高）",
      "unet": "minimax_h3_fl2va_int8_convrot.safetensors",
      "note": "未剪枝 int8（34.0 GB），AdaLN 仍是完整的 2688 维，Turbo LoRA 的层结构 259/259 全部命中（实测），保真最高。",
      "location": "/home/ubuntu/comfyui/models/diffusion_models",
      "size_mb": 34039,
      "steps": 4,
      "sampler": "euler",
      "scheduler": "beta",
      "cfg": 1.0,
      "available": true,
      "status": "unknown",
      "warn": "体积接近完整版的一半，显存占用比剪枝版高约 12 GB。"
    },
    {
      "key": "fl2va_pruned_w4a8",
      "label": "MiniMax-H3 FL2VA · 剪枝 w4a8（最省显存）",
      "unet": "minimax_h3_fl2va_pruned_w4a8_mixed.safetensors",
      "note": "剪枝 + 4bit 激活（12.5 GB），显存最省。注意它把隐藏维折成 2688 —— LightX2V 系加速 LoRA 的 qkv 层维度对不上（实测 200/208 形状不匹配），只能用 Turbo 家族。",
      "location": "/mnt/nvme1t_colorful/comfyui-models/diffusion_models",
      "size_mb": 12541,
      "steps": 4,
      "sampler": "euler",
      "scheduler": "beta",
      "cfg": 1.0,
      "available": true,
      "status": "unknown",
      "warn": "与 LightX2V / DMAD 系加速 LoRA 不兼容（实测形状不匹配），只能配 Turbo 家族加速 LoRA。"
    },
    {
      "key": "fl2va_bf16",
      "label": "MiniMax-H3 FL2VA · BF16（官方原始精度）",
      "unet": "minimax_h3_fl2va_bf16.safetensors",
      "note": "官方原始 BF16 权重（66.3 GB），层结构与 Turbo LoRA 259/259 命中。",
      "location": "/mnt/nvme1t_colorful/comfyui-models/diffusion_models",
      "size_mb": 66280,
      "steps": 4,
      "sampler": "euler",
      "scheduler": "beta",
      "cfg": 1.0,
      "available": true,
      "status": "unknown",
      "warn": "48 GB 显存的卡单靠这个权重放不下（实测 66.3 GB），只适合多卡或换机器时选。"
    },
    {
      "key": "hybrid_fl2va_ref2va",
      "label": "MiniMax-H3 混合版 · FL2VA+Ref2VA",
      "unet": "minimax_h3_hybrid_fl2va_ref2va_b30-49-int8.safetensors",
      "note": "把文生视频与参考生成合进一个权重的剪枝 int8 版（20.9 GB）。Turbo LoRA 259 个层里 208 个直接命中、51 个 AdaLN 由运行期注入。",
      "location": "/mnt/nvme1t_colorful/comfyui-models/diffusion_models",
      "size_mb": 20970,
      "steps": 4,
      "sampler": "euler",
      "scheduler": "beta",
      "cfg": 1.0,
      "available": true,
      "status": "unknown",
      "warn": "外接盘权重；参考通道（Ref2VA）在文生视频面板里不暴露。"
    }
  ],
  "default_model": "fl2va_pruned_int8",
  "attention_modes": [
    {
      "key": "default",
      "label": "默认（PyTorch 稠密注意力）",
      "node": null,
      "purpose": ""
    },
    {
      "key": "kitchen",
      "label": "Comfy Kitchen INT8 量化注意力",
      "node": "ModelAttentionBackend",
      "purpose": ""
    },
    {
      "key": "sage",
      "label": "SageAttention 显存优化",
      "node": "MiniMaxH3MemoryEfficientSageAttentionPatch",
      "purpose": ""
    },
    {
      "key": "sol",
      "label": "Sol-Attn 近似路由",
      "node": "MiniMaxH3MemoryEfficientSolAttentionPatch",
      "purpose": ""
    },
    {
      "key": "sol_scheduled",
      "label": "Sol-Attn 渐进调度（首尾密度不同）",
      "node": "MiniMaxH3ScheduledSolAttentionPatch",
      "purpose": ""
    },
    {
      "key": "sparse",
      "label": "稀疏视频注意力（预算制）",
      "node": "H3SparseAttention",
      "purpose": ""
    }
  ],
  "default_attention": "default",
  "accel_lora_combos": [
    {
      "key": "lightx2v_4step_768p",
      "display": "LightX2V 4 步（768p 档）",
      "name": "h3_lightx2v/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors",
      "purpose": "LightX2V 的 4 步蒸馏权重，针对 768p 短边训练，在该分辨率下比 Turbo ckpt850 更稳。",
      "recommended_strength": 1.0,
      "available": true
    },
    {
      "key": "lightx2v_4step_sla_768p",
      "display": "LightX2V SLA 4 步（768p 档）",
      "name": "minimax_h3_fl2v_turbo_4step_v0.1_768p_sla_comfyui_bf16.safetensors",
      "purpose": "LightX2V 的 SLA（Segment Level Attention）变体，4 步同时保住长时一致性。",
      "recommended_strength": 1.0,
      "available": true
    },
    {
      "key": "lightx2v_8step",
      "display": "LightX2V 8 步",
      "name": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
      "purpose": "8 步档蒸馏：比 4 步慢一倍，但细节与运动更稳，适合最终成片而不是快速试片。",
      "recommended_strength": 1.0,
      "available": true
    },
    {
      "key": "dmad_4step_critic",
      "display": "DMAD 4 步（critic 版）",
      "name": "dmad_minimax_h3_4step_full_critic_comfyui.safetensors",
      "purpose": "DMAD 蒸馏 + 判别器 critic：4 步里质量更接近多步采样。",
      "recommended_strength": 1.0,
      "available": true
    },
    {
      "key": "speed_slider",
      "display": "速度滑杆（快/慢运动）",
      "name": "h3/h3_speed_slider_1.1.safetensors",
      "purpose": "概念 LoRA：只 patch token_refiner 的 fc2，可把画面运动整体调快或调慢（负向词 'slow motion.'）。",
      "recommended_strength": 1.0,
      "available": true
    },
    {
      "key": "better_motion",
      "display": "更好运动（720x1280 档 / 15-30 步）",
      "name": "h3/h3_better_motion_720x1280_w0.4-0.8_s15-30_vpakarinen_v1.safetensors",
      "purpose": "针对 720x1280 竖屏、15~30 步训练的运动增强 LoRA，改善大动作与镜头运动。",
      "recommended_strength": 0.8,
      "available": true
    }
  ],
  "fixed_accel_loras": [
    {
      "key": "ckpt850",
      "display": "Turbo 4 步 · ckpt850（工作流固定）",
      "name": "minimax_h3_turbo_4step_ema_ckpt850.safetensors",
      "purpose": "H3 Turbo 4 步路径的固定加速 LoRA：把 20 步压到 4 步。",
      "recommended_strength": 1.0,
      "available": true
    },
    {
      "key": "v4step600",
      "display": "Turbo 4 步 · v4-step600（工作流固定）",
      "name": "minimax_h3_turbo_v4_step600_ema.safetensors",
      "purpose": "同族的另一枚 EMA 权重，官方 4 步档的推荐版本。",
      "recommended_strength": 1.0,
      "available": true
    }
  ],
  "default_fixed_accel_lora": "ckpt850",
  "aspects": [
    {
      "key": "1:1",
      "ratio": [
        1,
        1
      ]
    },
    {
      "key": "2:3",
      "ratio": [
        2,
        3
      ]
    },
    {
      "key": "3:2",
      "ratio": [
        3,
        2
      ]
    },
    {
      "key": "3:4",
      "ratio": [
        3,
        4
      ]
    },
    {
      "key": "4:3",
      "ratio": [
        4,
        3
      ]
    },
    {
      "key": "9:16",
      "ratio": [
        9,
        16
      ]
    },
    {
      "key": "16:9",
      "ratio": [
        16,
        9
      ]
    },
    {
      "key": "21:9",
      "ratio": [
        21,
        9
      ]
    }
  ],
  "size_multiple": 32,
  "size_min": 32,
  "size_max": 16384,
  "mp_table": [
    0.2,
    0.3,
    0.4,
    0.5,
    0.6,
    0.7,
    0.8,
    0.9,
    0.98,
    1.0,
    1.2,
    1.5,
    1.8,
    2.0
  ],
  "default_mp": 0.4
};
