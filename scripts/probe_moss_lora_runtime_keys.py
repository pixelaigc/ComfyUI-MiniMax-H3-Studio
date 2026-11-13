#!/usr/bin/env python3
"""B 项取证（生产目录口径）：遍历**运行时** MOSS LoRA 目录，逐个读真实 safetensors header，
与 MOSS 基座真实层键集合逐一比对，输出可复核的 JSON 证据。

口径与 h3studio/moss_lora_compat.check_moss_compatibility 一致：
  归一前缀 -> 抽模块名（去 LoHa/LoKR 载体后缀）-> 逐模块查基座键集合
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from h3studio import moss_lora_compat as mlc  # noqa: E402

RUNTIME_DIRS = [
    Path("/mnt/usb2t/comfyui-models/loras/moss-effects"),
    Path("/mnt/usb2t/comfyui-models/loras/moss"),
]

CARRIERS = (".hada_w1_a", ".hada_w1_b", ".hada_w2_a", ".hada_w2_b", ".alpha",
            ".lokr_w1_a", ".lokr_w1_b", ".lokr_w2_a", ".lokr_w2_b", ".lokr_w2",
            ".lora_A", ".lora_B")


def module_of(key: str) -> str:
    """用**生产代码自己的** module_name_of 抽模块名（不另造载体后缀表）。

    h3studio.moss_lora_compat._ALL_CARRIER_SUFFIXES 已覆盖
    lora_A.weight / lora_A.default.weight / lora_down.weight / lora_up.weight /
    hada_w1_a / lokr_w1 ... 等全部载体形态，本探针必须与判定口径一致。
    """
    return mlc.module_name_of(key) or key


def probe(path: Path, base_keys: set[str]) -> dict:
    header = mlc.read_safetensors_header(path)
    raw = [k for k in header if k != "__metadata__"]
    modules: dict[str, list[str]] = {}
    unmatched: list[str] = []
    for k in raw:
        n = mlc.normalize_key(k)
        if n is None:
            unmatched.append(k)
            continue
        modules.setdefault(module_of(n), []).append(k)
    hit = [m for m in modules if m in base_keys]
    miss = [m for m in modules if m not in base_keys]
    img = [m for m in modules
           if ".proj." in m and not m.startswith(mlc.TARGET_PREFIX + "text_embedder.")]
    img_hit = [m for m in img if m in base_keys]
    img_miss = [m for m in img if m not in base_keys]
    fused = sorted(m for m in img_miss if m.endswith(".proj.qkv"))
    other = sorted(m for m in img_miss if not m.endswith(".proj.qkv"))
    cc = mlc.check_moss_compatibility(path)
    return {
        "file": path.name,
        "dir": path.parent.name,
        "path": str(path),
        "size_mib": round(path.stat().st_size / 2**20, 1),
        "raw_key_count": len(raw),
        "module_count": len(modules),
        "base_hit": len(hit),
        "base_miss": len(miss),
        "base_hit_ratio": round(len(hit) / max(1, len(modules)), 4),
        "image_modules": len(img),
        "image_hit": len(img_hit),
        "image_miss": len(img_miss),
        "image_miss_fused_qkv": len(fused),
        "image_miss_other": len(other),
        "image_miss_other_samples": other[:5],
        "unmatched_raw": len(unmatched),
        "unmatched_raw_samples": unmatched[:5],
        "raw_key_samples": raw[:4],
        "carriers_seen": sorted({c for k in raw for c in mlc._ALL_CARRIER_SUFFIXES
                                 if k.endswith("." + c)}),
        "official": {
            "compatible": cc.get("compatible"),
            "qkv_shape": cc.get("qkv_shape"),
            "attention_hit": cc.get("attention_hit"),
            "convertible": cc.get("convertible"),
            "needs_normalize": cc.get("needs_normalize"),
            "verified": cc.get("verified"),
            "reason": (cc.get("reason") or "")[:200],
        },
    }


def main() -> int:
    base_keys = mlc.moss_base_layer_keys()
    if not base_keys:
        print("!! 基座键不可读")
        return 2
    out = {
        "base_key_count": len(base_keys),
        "base_source": str(mlc.MOSS_BASE_ROOTS[0]),
        "base_index": str(mlc.MOSS_BASE_ROOTS[0] / "transformer"
                          / "diffusion_pytorch_model.safetensors.index.json"),
        "base_key_samples": sorted(base_keys)[:8],
        "runtime_dirs": [str(d) for d in RUNTIME_DIRS],
        "loras": [],
    }
    print(f"基座真实层键 = {len(base_keys)}  ({out['base_index']})")
    for d in RUNTIME_DIRS:
        if not d.is_dir():
            print(f"  (跳过不存在的目录 {d})")
            continue
        files = sorted(d.glob("*.safetensors"))
        print(f"\n===== {d}  ({len(files)} 个 safetensors) =====")
        for f in files:
            e = probe(f, base_keys)
            out["loras"].append(e)
            o = e["official"]
            flag = "OK " if o["compatible"] else "NO "
            print(f"  {flag}{e['file'][:52]:<52} {e['size_mib']:>7} MiB "
                  f"keys={e['raw_key_count']:<5} mod={e['module_count']:<4} "
                  f"命中={e['base_hit']:<4} 未命中={e['base_miss']:<4} "
                  f"img={e['image_hit']}/{e['image_modules']} "
                  f"fused_miss={e['image_miss_fused_qkv']} "
                  f"shape={o['qkv_shape']} conv={o['convertible']}")
            if e["image_miss_other_samples"]:
                print(f"      非 qkv 未命中样例: {e['image_miss_other_samples'][:3]}")
            if e["unmatched_raw"]:
                print(f"      无法归一的原始键 {e['unmatched_raw']}: {e['unmatched_raw_samples'][:2]}")
            if o["reason"]:
                print(f"      reason: {o['reason'][:150]}")

    dest = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/h3-fix-evidence")
    dest.mkdir(parents=True, exist_ok=True)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "moss_lora_runtime_key_shapes.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    n_ok = sum(1 for x in out["loras"] if x["official"]["compatible"])
    print(f"\n合计 {len(out['loras'])} 个运行时 MOSS LoRA，官方判定兼容 {n_ok} 个")
    print(f"证据写入 {dest / 'moss_lora_runtime_key_shapes.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
