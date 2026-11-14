#!/usr/bin/env python3
"""
把 m-a-p/YuE2-3B（HF 布局 bf16）+ m-a-p/YuE2-Vae 转成 ComfyUI 原生单文件 ckpt 布局。

布局映射（已用远端 Comfy-Org bf16 逐字节验证 EXACT）：
  model.diffusion_model.*  <- HF 的 **NAR** 分支（声学 transformer）
  text_encoders.model.*    <- HF 的 **AR** 分支（文本/语义 LLM）
  text_encoders.yue2_tokenizer_json <- 复用现有原生 ckpt 里的同一份 tokenizer
  vae.*                    <- m-a-p/YuE2-Vae（键名结构完全一致）

合并规则（远端 bf16 逐值验证 EXACT）：
  qkv_proj.weight = cat([q_proj, k_proj, v_proj], dim=0)   # q|k|v
  gate_up_proj.weight = cat([gate_proj, up_proj], dim=0)   # gate|up
"""
from __future__ import annotations

import json
import shutil
import struct
import sys
from pathlib import Path

import torch
from safetensors import safe_open
from safetensors.torch import save_file

HF_MOT = Path("/home/ubuntu/comfyui/models/yue2/YuE2-3B/model.safetensors")
HF_VAE = Path("/home/ubuntu/comfyui/models/yue2/YuE2-Vae/model.safetensors")
OUT = Path("/home/ubuntu/comfyui/models/checkpoints/yue2_3b_bf16.safetensors")

# 【2026-11-09】int8 参考权重已从本机删除（用户拍板：只用 BF16）。
# 本脚本改为**自足**：不再依赖任何 int8 文件。
#   * tokenizer 从 ComfyUI 自己的 YuE2 文本编码器导出
#   * 键集合/逐值校验改为对着**已有的 bf16 产物**做（若存在）或跳过
TOKENIZER_CACHE = Path("/home/ubuntu/comfyui/models/yue2/YuE2-tokenizer/yue2_tokenizer_json.bin")

NUM_LAYERS = 28


def _load_tokenizer() -> torch.Tensor:
    """拿 yue2_tokenizer_json 的原始字节。

    优先用缓存文件（第一次运行时导出），否则从 ComfyUI 自己的
    YuE2 文本编码器导出 —— 它内置了 tokenizer JSON 的副本。
    """
    if TOKENIZER_CACHE.is_file():
        data = TOKENIZER_CACHE.read_bytes()
        return torch.frombuffer(bytearray(data), dtype=torch.uint8).clone()
    sys.path.insert(0, "/home/ubuntu/comfyui")
    from comfy.text_encoders.yue2 import YuE2Tokenizer  # noqa: E402
    import comfy.text_encoders.yue2 as cy  # noqa: E402

    # ComfyUI 的 yue2 tokenizer 是从 checkpoint 里的 "yue2_tokenizer_json"
    # 张量还原的；这里用本机的 YuE2 文本编码器实例拿它的 tokenizer JSON。
    # YuE2Tokenizer 需要 tokenizer_data["yue2_tokenizer_json"]（bytes）。
    from comfy.sd import CLIP  # noqa: E402
    import comfy.model_management as mm  # noqa: E402

    # 从已装好的 ComfyUI 原生 YuE2 文本编码器里取（它读 models/text_encoders）
    # 退路：用 m-a-p 仓库里的 qwen.tiktoken 不行（格式不同），所以走
    # ComfyUI 自己的 tokenizer 资源。
    tk_json = _extract_tokenizer_json_from_comfy()
    data = tk_json.encode("utf-8")
    TOKENIZER_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TOKENIZER_CACHE.write_bytes(data)
    return torch.frombuffer(bytearray(data), dtype=torch.uint8).clone()


def _extract_tokenizer_json_from_comfy() -> str:
    """从 ComfyUI 已装的 YuE2 文本编码器资源里导出 tokenizer JSON 文本。"""
    # ComfyUI 把 yue2 tokenizer 放在 text_encoders/yue2/ 或走 checkpoint。
    # 本机路径：/home/ubuntu/comfyui/comfy/text_encoders/yue2.py 里的 YuE2Tokenizer
    # 直接读它引用的内置资源。
    import comfy.text_encoders.yue2 as cy
    import inspect
    src = inspect.getsource(cy)
    # YuE2Tokenizer.__init__ 从 tokenizer_data["yue2_tokenizer_json"] 取 bytes
    # 我们需要原始 JSON 文本 —— 它来自本机的 yue2 文本编码器 checkpoint。
    # 该 checkpoint 由 ComfyUI 从 checkpoints/ 的 yue2 ckpt 里读出来，
    # 所以直接扫 checkpoints/ 找带 yue2_tokenizer_json 的文件。
    from safetensors import safe_open
    import glob
    for ck in glob.glob("/home/ubuntu/comfyui/models/checkpoints/yue2*.safetensors"):
        with safe_open(ck, framework="pt") as f:
            if "text_encoders.yue2_tokenizer_json" in f.keys():
                return f.get_tensor("text_encoders.yue2_tokenizer_json").numpy().tobytes().decode("utf-8")
    raise FileNotFoundError("找不到含 yue2_tokenizer_json 的 checkpoint")


def main() -> int:
    print("=== 载入源权重 ===")
    mot = safe_open(str(HF_MOT), framework="pt")
    vae = safe_open(str(HF_VAE), framework="pt")
    print(f"  HF MOT tensors : {len(mot.keys())}")
    print(f"  HF VAE tensors : {len(vae.keys())}")

    out: dict[str, torch.Tensor] = {}

    # ---------------- MOT（声学 transformer）<- NAR 分支 ----------------
    print("\n=== 构建 model.diffusion_model.* （NAR 分支）===")
    P = "model.diffusion_model."
    out[P + "latent_pos_embed.pe"] = mot.get_tensor("latent_pos_embed.pe")
    out[P + "vae2llm.weight"] = mot.get_tensor("vae2llm.weight")
    out[P + "vae2llm.bias"] = mot.get_tensor("vae2llm.bias")
    out[P + "llm2vae.weight"] = mot.get_tensor("llm2vae.weight")
    out[P + "llm2vae.bias"] = mot.get_tensor("llm2vae.bias")
    out[P + "model.norm.weight"] = mot.get_tensor("model.norm.weight")
    for i in (0, 2):
        out[P + f"time_embedder.mlp.{i}.weight"] = mot.get_tensor(f"time_embedder.mlp.{i}.weight")
        out[P + f"time_embedder.mlp.{i}.bias"] = mot.get_tensor(f"time_embedder.mlp.{i}.bias")

    for i in range(NUM_LAYERS):
        b = f"model.layers.{i}."
        d = P + b
        # NAR 分支的 layernorm
        out[d + "input_layernorm.weight"] = mot.get_tensor(b + "nar_input_layernorm.weight")
        out[d + "post_attention_layernorm.weight"] = mot.get_tensor(b + "nar_pre_mlp_layernorm.weight")
        # NAR 分支的 attention（qkv 合并）
        out[d + "self_attn.q_norm.weight"] = mot.get_tensor(b + "nar_self_attn.q_norm.weight")
        out[d + "self_attn.k_norm.weight"] = mot.get_tensor(b + "nar_self_attn.k_norm.weight")
        out[d + "self_attn.qkv_proj.weight"] = torch.cat(
            [
                mot.get_tensor(b + "nar_self_attn.q_proj.weight"),
                mot.get_tensor(b + "nar_self_attn.k_proj.weight"),
                mot.get_tensor(b + "nar_self_attn.v_proj.weight"),
            ],
            dim=0,
        )
        out[d + "self_attn.o_proj.weight"] = mot.get_tensor(b + "nar_self_attn.o_proj.weight")
        # NAR 分支的 mlp（gate_up 合并）
        out[d + "mlp.gate_up_proj.weight"] = torch.cat(
            [
                mot.get_tensor(b + "nar_mlp.gate_proj.weight"),
                mot.get_tensor(b + "nar_mlp.up_proj.weight"),
            ],
            dim=0,
        )
        out[d + "mlp.down_proj.weight"] = mot.get_tensor(b + "nar_mlp.down_proj.weight")

    # ---------------- TE（文本/语义 LLM）<- AR 分支 ----------------
    print("=== 构建 text_encoders.model.* （AR 分支）===")
    T = "text_encoders.model."
    out[T + "embed_tokens.weight"] = mot.get_tensor("model.embed_tokens.weight")
    out[T + "lm_head.weight"] = mot.get_tensor("lm_head.weight")
    out[T + "norm.weight"] = mot.get_tensor("model.norm.weight")
    for i in range(NUM_LAYERS):
        # TE 的原生布局是**扁平**的 `layers.N.*`（没有中间的 `model.` 一层）——
        # 与 MOT 的 `model.layers.N.*` 不同，别混。
        b = f"model.layers.{i}."
        d = T + f"layers.{i}."
        out[d + "input_layernorm.weight"] = mot.get_tensor(b + "input_layernorm.weight")
        out[d + "post_attention_layernorm.weight"] = mot.get_tensor(b + "post_attention_layernorm.weight")
        out[d + "self_attn.q_norm.weight"] = mot.get_tensor(b + "self_attn.q_norm.weight")
        out[d + "self_attn.k_norm.weight"] = mot.get_tensor(b + "self_attn.k_norm.weight")
        out[d + "self_attn.qkv_proj.weight"] = torch.cat(
            [
                mot.get_tensor(b + "self_attn.q_proj.weight"),
                mot.get_tensor(b + "self_attn.k_proj.weight"),
                mot.get_tensor(b + "self_attn.v_proj.weight"),
            ],
            dim=0,
        )
        out[d + "self_attn.o_proj.weight"] = mot.get_tensor(b + "self_attn.o_proj.weight")
        out[d + "mlp.gate_up_proj.weight"] = torch.cat(
            [
                mot.get_tensor(b + "mlp.gate_proj.weight"),
                mot.get_tensor(b + "mlp.up_proj.weight"),
            ],
            dim=0,
        )
        out[d + "mlp.down_proj.weight"] = mot.get_tensor(b + "mlp.down_proj.weight")

    # ---------------- tokenizer ----------------
    print("=== 生成 yue2_tokenizer_json（从 ComfyUI 文本编码器导出）===")
    tk_key = "text_encoders.yue2_tokenizer_json"
    tk = _load_tokenizer()
    out[tk_key] = tk
    print(f"  {tk_key}: {tuple(tk.shape)} {tk.dtype}")

    # ---------------- VAE ----------------
    print("=== 构建 vae.* （m-a-p/YuE2-Vae，键名直接对应）===")
    for k in vae.keys():
        out["vae." + k] = vae.get_tensor(k)

    # ---------------- 键集合自检 ----------------
    # 【2026-11-09】int8 参考权重已删除，改成**自足**校验：
    #   1) 必须包含 ComfyUI 原生检测所依赖的 5 个 yue2 键
    #   2) 必须包含 VAE 与 tokenizer 键
    #   3) 若已存在同名 bf16 产物，则做逐字节比对（幂等性检查）
    print("\n=== 键集合自检 ===")
    out_keys = set(out.keys())
    required = [
        "model.diffusion_model.vae2llm.weight",
        "model.diffusion_model.llm2vae.weight",
        "model.diffusion_model.latent_pos_embed.pe",
        "model.diffusion_model.model.layers.0.self_attn.qkv_proj.weight",
        "model.diffusion_model.time_embedder.mlp.0.weight",
        "text_encoders.model.embed_tokens.weight",
        "text_encoders.model.lm_head.weight",
        "text_encoders.yue2_tokenizer_json",
    ]
    missing_req = [k for k in required if k not in out_keys]
    print(f"  产物键数     : {len(out_keys)}")
    print(f"  必需键缺失   : {len(missing_req)}")
    for k in missing_req:
        print("      MISSING:", k)
    if missing_req:
        print("  !! 缺少 ComfyUI 原生检测必需的键，中止")
        return 1
    if not any(k.startswith("vae.") for k in out_keys):
        print("  !! 缺少 vae.* 键，中止")
        return 1

    # 幂等性：若目标文件已存在，逐张量比对（应完全一致）
    if OUT.is_file():
        print("\n=== 幂等性检查：与已存在的产物逐张量比对 ===")
        with safe_open(str(OUT), framework="pt") as prev:
            prev_keys = set(prev.keys())
            if prev_keys != out_keys:
                print(f"  !! 键集合与已有产物不同（{len(prev_keys)} vs {len(out_keys)}），中止")
                return 1
            diff = 0
            for k in sorted(out_keys):
                if not torch.equal(prev.get_tensor(k), out[k].to(prev.get_tensor(k).dtype)):
                    diff += 1
                    if diff <= 5:
                        print("      DIFF:", k)
            print(f"  不一致张量数: {diff}")
            if diff:
                print("  !! 同样输入却产出不同结果，转换不可复现，中止")
                return 1
        print("  -> 与已有产物完全一致 ✅")

    # ---------------- 落盘 ----------------
    print("\n=== 落盘 ===")
    out = {k: v.contiguous().clone() for k, v in out.items()}
    # 所有张量统一 BF16（VAE 保持 F32 —— 原生 Comfy-Org bf16 版本 VAE 也是 F32）
    for k in list(out.keys()):
        if k.startswith("vae.") or k == tk_key:
            continue
        out[k] = out[k].to(torch.bfloat16)
    tmp = OUT.with_suffix(".safetensors.tmp")
    save_file(out, str(tmp), metadata={"format": "pt", "source": "m-a-p/YuE2-3B+YuE2-Vae",
                                       "yue2_format": "1", "vae_source": "YuE2-Vae",
                                       "license": "CC-BY-NC-4.0"})
    shutil.move(str(tmp), str(OUT))
    print(f"  -> {OUT}  ({OUT.stat().st_size} bytes, {OUT.stat().st_size/2**30:.2f} GiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
