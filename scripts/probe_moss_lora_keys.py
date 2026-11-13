#!/usr/bin/env python3
"""B 项取证：逐个 MOSS 系 LoRA 的真实 safetensors key 形态 vs 基座 state_dict key 逐一比对。

只读 safetensors header（不整体加载权重）。
比对口径与 h3studio/moss_lora_compat.check_moss_compatibility 完全一致：
  1. 归一前缀（lora_unet_ / 裸 layers. / transformer. -> diffusion_model.）
  2. 抽模块名（去载体后缀 hada_w1_a / alpha / hada_w2_b ...）
  3. 逐模块名去基座真实层键集合里查，命中才叫命中
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from h3studio import moss_lora_compat as mlc  # noqa: E402

LORA_ROOT = Path("/home/ubuntu/comfyui/models/_lora_source_peft")

CANDIDATES = [
    LORA_ROOT / "MOSS_image_MAX_ULTRA_hires_lora_test" / f"moss_hires_lora_test_v0.{v}.safetensors"
    for v in (1, 2, 3, 4)
] + [
    LORA_ROOT / "Moss-image-nsfw-uncensored-lora" / "pytorch_lora_weights.safetensors",
]

# LoHa 载体后缀（hada_w1_a / hada_w1_b / hada_w2_a / hada_w2_b / alpha）
CARRIERS = (".hada_w1_a", ".hada_w1_b", ".hada_w2_a", ".hada_w2_b", ".alpha",
            ".lokr_w1_a", ".lokr_w1_b", ".lokr_w2_a", ".lokr_w2_b", ".lokr_w2")


def module_of(key: str) -> str | None:
    for c in CARRIERS:
        if key.endswith(c):
            return key[: -len(c)]
    return key


def main() -> int:
    base_keys = mlc.moss_base_layer_keys()
    out: dict = {
        "base_key_count": len(base_keys) if base_keys else 0,
        "base_source": [str(p) for p in mlc.MOSS_BASE_ROOTS],
        "base_key_samples": sorted(base_keys)[:10] if base_keys else [],
        "loras": [],
    }
    print(f"基座层键数 = {out['base_key_count']}  来源={mlc.MOSS_BASE_ROOTS[0]}")
    print(f"基座样例: {out['base_key_samples'][:5]}")
    if not base_keys:
        print("!! 基座 index.json 不可读，无法比对")
        return 2

    for path in CANDIDATES:
        if not path.is_file():
            out["loras"].append({"path": str(path), "exists": False})
            print(f"\n### {path.name}: 不存在")
            continue
        header = mlc.read_safetensors_header(path)
        raw_keys = [k for k in header if k != "__metadata__"]

        # 归一 + 抽模块
        modules: dict[str, list[str]] = {}
        unmatched: list[str] = []
        for k in raw_keys:
            n = mlc.normalize_key(k)
            if n is None:
                unmatched.append(k)
                continue
            modules.setdefault(module_of(n) or n, []).append(k)

        hit = [m for m in modules if m in base_keys]
        miss = [m for m in modules if m not in base_keys]
        # 图像侧投影模块（与官方判定同口径）
        img_miss = [m for m in miss if ".proj." in m
                    and not m.startswith(mlc.TARGET_PREFIX + "text_embedder.")]
        img_total = [m for m in modules if ".proj." in m
                     and not m.startswith(mlc.TARGET_PREFIX + "text_embedder.")]

        entry = {
            "path": str(path),
            "exists": True,
            "size_bytes": path.stat().st_size,
            "raw_key_count": len(raw_keys),
            "raw_key_samples": raw_keys[:5],
            "module_count": len(modules),
            "base_hit_count": len(hit),
            "base_miss_count": len(miss),
            "base_hit_ratio": round(len(hit) / max(1, len(modules)), 4),
            "base_miss_samples": sorted(miss)[:8],
            "unmatched_raw_count": len(unmatched),
            "unmatched_raw_samples": unmatched[:6],
            "image_module_total": len(img_total),
            "image_module_hit": len([m for m in img_total if m in base_keys]),
            "image_module_miss": len(img_miss),
            "image_module_miss_samples": sorted(img_miss)[:8],
            "carrier_suffixes_seen": sorted({
                c for k in raw_keys for c in CARRIERS if k.endswith(c)
            }),
            "needs_normalize": mlc.check_moss_compatibility(path).get("needs_normalize"),
            "check_compatibility": mlc.check_moss_compatibility(path),
        }
        out["loras"].append(entry)

        print(f"\n### {path.name}  ({entry['size_bytes'] / 2**20:.1f} MiB)")
        print(f"  原始 key = {entry['raw_key_count']}, 归一后模块 = {entry['module_count']}")
        print(f"  原始样例: {entry['raw_key_samples'][:3]}")
        print(f"  载体后缀: {entry['carrier_suffixes_seen']}")
        print(f"  基座命中 {entry['base_hit_count']} / 未命中 {entry['base_miss_count']} "
              f"(命中率 {entry['base_hit_ratio']:.2%})")
        print(f"  图像侧投影模块 {entry['image_module_total']}: 命中 "
              f"{entry['image_module_hit']} / 未命中 {entry['image_module_miss']}")
        if entry["image_module_miss_samples"]:
            print(f"  未命中样例: {entry['image_module_miss_samples'][:3]}")
        if entry["unmatched_raw_samples"]:
            print(f"  无法归一的原始键样例: {entry['unmatched_raw_samples'][:3]}")
        cc = entry["check_compatibility"]
        print(f"  官方判定: compatible={cc.get('compatible')} qkv_shape={cc.get('qkv_shape')} "
              f"attention_hit={cc.get('attention_hit')} convertible={cc.get('convertible')} "
              f"needs_normalize={cc.get('needs_normalize')} verified={cc.get('verified')}")
        if cc.get("reason"):
            print(f"  reason: {cc['reason'][:160]}")

    dest = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/h3-fix-evidence")
    dest.mkdir(parents=True, exist_ok=True)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "moss_lora_key_shapes.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n证据写入 {dest / 'moss_lora_key_shapes.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
