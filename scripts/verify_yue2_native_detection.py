#!/usr/bin/env python3
"""用 ComfyUI 自己的检测代码验证新生成的 bf16 ckpt 能被原生节点识别。"""
import json
import sys

sys.path.insert(0, "/home/ubuntu/comfyui")

import comfy.model_detection  # noqa: E402
import comfy.supported_models  # noqa: E402
import comfy.utils  # noqa: E402

CKPT = "/home/ubuntu/comfyui/models/checkpoints/yue2_3b_bf16.safetensors"

print("=== ComfyUI 原生检测 ===")
sd, metadata = comfy.utils.load_torch_file(CKPT, return_metadata=True)
print(f"  加载张量数: {len(sd)}")
print(f"  metadata  : {json.dumps(metadata, ensure_ascii=False)[:200]}")

prefix = comfy.model_detection.unet_prefix_from_state_dict(sd)
print(f"  unet_prefix_from_state_dict -> {prefix!r}")

cfg = comfy.model_detection.model_config_from_unet(sd, prefix, metadata=metadata)
print(f"  model_config_from_unet -> {cfg}")
if cfg is None:
    print("  !! 检测失败")
    sys.exit(1)

print(f"  模型类      : {type(cfg).__name__}")
print(f"  unet_config : {cfg.unet_config}")
print(f"  量化配置    : {cfg.quant_config}")
print(f"  支持精度    : {cfg.supported_inference_dtypes}")
print(f"  vae 前缀    : {cfg.vae_key_prefix}")
print(f"  te 前缀     : {cfg.text_encoder_key_prefix}")

# yue2 检测键逐条确认
need = ["vae2llm.weight", "llm2vae.weight", "latent_pos_embed.pe",
        "model.layers.0.self_attn.qkv_proj.weight", "time_embedder.mlp.0.weight"]
print("\n  yue2 检测键（带前缀）:")
allok = True
for k in need:
    ok = (prefix + k) in sd
    allok &= ok
    print(f"    {prefix+k}: {ok}")
print(f"  -> 全部命中: {allok}")

# VAE / CLIP 键
print("\n  VAE 键（剥 vae. 前缀后）:")
vae_stripped = {k[4:] for k in sd if k.startswith("vae.")}
for probe in ["decoder.layers.1.layers.0.beta",
              "decoder.layers.6.layers.1.weight_v",
              "decoder.layers.2.layers.1.weight_v"]:
    print(f"    {probe}: {probe in vae_stripped}")

te = {k[len("text_encoders.model."):] for k in sd if k.startswith("text_encoders.model.")}
print("\n  CLIP/TE 键:")
print(f"    yue2_tokenizer_json (顶层): {'text_encoders.yue2_tokenizer_json' in sd}")
print(f"    embed_tokens.weight       : {'embed_tokens.weight' in te}")
print(f"    lm_head.weight             : {'lm_head.weight' in te}")
print(f"    norm.weight                : {'norm.weight' in te}")

# clip_target 能否解出
clip_target = cfg.clip_target(state_dict=sd)
print(f"\n  clip_target -> {type(clip_target.clip).__name__} / tokenizer {type(clip_target.tokenizer).__name__}")

ok = allok and cfg is not None
print("\n=== 结论:", "原生可识别 ✅" if ok else "原生不可识别 ❌", "===")
sys.exit(0 if ok else 1)
