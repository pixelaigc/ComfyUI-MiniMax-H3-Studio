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
NATIVE_REF = Path("/home/ubuntu/comfyui/models/checkpoints/yue2_3b_int8_convrot.safetensors")
OUT = Path("/home/ubuntu/comfyui/models/checkpoints/yue2_3b_bf16.safetensors")

NUM_LAYERS = 28


def hdr_keys(path: Path) -> list[str]:
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        h = json.loads(f.read(n))
    return [k for k in h if k != "__metadata__"]


def main() -> int:
    print("=== 载入源权重 ===")
    mot = safe_open(str(HF_MOT), framework="pt")
    vae = safe_open(str(HF_VAE), framework="pt")
    ref = safe_open(str(NATIVE_REF), framework="pt")
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
    print("=== 复用原生 ckpt 里的 yue2_tokenizer_json ===")
    tk_key = "text_encoders.yue2_tokenizer_json"
    tk = ref.get_tensor(tk_key)
    out[tk_key] = tk
    print(f"  {tk_key}: {tuple(tk.shape)} {tk.dtype}")

    # ---------------- VAE ----------------
    print("=== 构建 vae.* （m-a-p/YuE2-Vae，键名直接对应）===")
    for k in vae.keys():
        out["vae." + k] = vae.get_tensor(k)

    # ---------------- 键集合与原生参考对齐检查 ----------------
    print("\n=== 键集合对齐检查（与 native int8 ckpt 对比）===")
    ref_keys = set(ref.keys())
    out_keys = set(out.keys())
    missing = ref_keys - out_keys
    extra = out_keys - ref_keys
    # 原生 ckpt 里量化专有键（comfy_quant / weight_scale）在 bf16 版本里不存在
    quant_only = {k for k in missing if k.endswith(".comfy_quant") or k.endswith(".weight_scale")}
    real_missing = missing - quant_only
    print(f"  原生 int8 ckpt 键数 : {len(ref_keys)}")
    print(f"  新 bf16 ckpt 键数   : {len(out_keys)}")
    print(f"  缺失（排除量化专有）: {len(real_missing)}")
    for k in sorted(real_missing)[:20]:
        print("      MISSING:", k)
    print(f"  多余               : {len(extra)}")
    for k in sorted(extra)[:20]:
        print("      EXTRA  :", k)
    if real_missing or extra:
        print("  !! 键集合不一致，中止")
        return 1

    # ---------------- 逐值校验（与原生 int8 反量化后比对非量化张量） ----------------
    print("\n=== 逐值校验：BF16 非量化张量 vs 原生 int8 反量化 ===")
    checked = 0
    mismatched = []
    for k in sorted(out_keys):
        if k.endswith(".comfy_quant") or k.endswith(".weight_scale"):
            continue
        if k not in ref_keys:
            continue
        rv = ref.get_tensor(k)
        ov = out[k]
        if rv.dtype == torch.int8:
            # 原生量化键的命名是「.weight」->「.weight_scale」（替换，不是追加）
            scale_key = k[: -len(".weight")] + ".weight_scale"
            sc = ref.get_tensor(scale_key)
            rq = rv.float() * sc.float()
        else:
            rq = rv.float()
        if tuple(rq.shape) != tuple(ov.shape):
            mismatched.append((k, "shape", tuple(rq.shape), tuple(ov.shape)))
            continue
        # int8 反量化有量化误差，只要求量级一致；BF16 张量要求精确相等
        if rv.dtype == torch.int8:
            denom = max(rq.abs().mean().item(), 1e-9)
            rel = (rq - ov.float()).abs().mean().item() / denom
            if rel > 0.05:
                mismatched.append((k, "int8-relerr", rel, ""))
        else:
            if not torch.equal(rq, ov.float()):
                mismatched.append((k, "not-equal", "", ""))
        checked += 1
    print(f"  已校验张量数: {checked}   不一致: {len(mismatched)}")
    for m in mismatched[:15]:
        print("      MISMATCH:", m)

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
