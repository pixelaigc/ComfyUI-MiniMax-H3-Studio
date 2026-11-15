#!/usr/bin/env python3
"""L-A 真实 ComfyUI 加载路径验证（v3 —— key_map 修正）。

⚠️ v1/v2 的探针 key_map 构造错了：ComfyUI 的
`model_lora_keys_unet(model, key_map)` 走的是 `model.state_dict()`，
而 **BaseModel.state_dict() 的键不带 `diffusion_model.` 前缀**
（加载时 `state_dict_prefix_replace` 已把前缀剥掉）。

v3 用**真实 ComfyUI 模型对象**来建 key_map —— 直接调
`comfy.sd.load_diffusion_model(path)`，拿到真的 ModelPatcher，
再 `comfy.lora.model_lora_keys_unet(model.model, {})`。
这是与线上 100% 相同的路径（sd.py:115 就是这么做的）。

代价：要真的加载底模（14GB / 20GB）到内存 -> 慢，且吃内存。
所以**一次加载，逐个 LoRA 复用**。
"""
import gc
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/ubuntu/comfyui")
sys.path.insert(0, "/root/projects/h3studio-labc")

import comfy.sd  # noqa: E402
from comfy import lora as comfy_lora, lora_convert, utils  # noqa: E402

LORAS = Path("/mnt/usb2t/comfyui-models/loras")
TARGETS = [
    LORAS / "AWPortrait-QW" / "AWPortrait-QW.safetensors",
    LORAS / "QIE-SRD" / "Qwen-Image-Edit-Semi-Realistic-Detailer-v1.safetensors",
    LORAS / "ipadapter" / "ip-adapter-faceid-plus_sd15_lora.safetensors",
]
#: 顺带带上一个**已知可用**的 MOSS LoRA 作为正对照
# 正对照：MOSS 的 LoRA（挂到 SDXL 上必然 0，但证明探针链路本身是活的）
CONTROL = LORAS / "ipadapter" / "ip-adapter-faceid-plusv2_sdxl_lora.safetensors"

BASES = {
    "qwen_image_2.1_bf16":
        "/home/ubuntu/comfyui/models/diffusion_models/qwen_image_2.1_bf16.safetensors",
    "sd_xl_base_unet_fp16":
        "/home/ubuntu/comfyui/models/diffusion_models/sd_xl_base_unet_fp16.safetensors",
    "Qwen_Image_Edit_2509_FP8":
        "/mnt/nvme1t_colorful/comfyui-models/diffusion_models/Qwen_Image_Edit_2509_FP8.safetensors",
}


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.missing = []

    def emit(self, record):
        msg = record.getMessage()
        if "lora key not loaded" in msg:
            self.missing.append(msg.split("lora key not loaded:")[-1].strip())


def attach(path, key_map):
    p = Path(path)
    try:
        raw = utils.load_torch_file(str(p), safe_load=True)
    except Exception as exc:
        return {"ERROR": f"{type(exc).__name__}: {exc}"}
    lora = lora_convert.convert_lora(raw)
    cap = _Cap()
    root = logging.getLogger()
    root.addHandler(cap)
    prev = root.level
    root.setLevel(logging.WARNING)
    try:
        loaded = comfy_lora.load_lora(lora, key_map, log_missing=True)
    finally:
        root.removeHandler(cap)
        root.setLevel(prev)
    return {"tensors": len(raw), "patched": len(loaded),
            "missing": len(cap.missing), "missing_sample": cap.missing[:2]}


out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
       "method": ("comfy.sd.load_diffusion_model -> "
                  "comfy.lora.model_lora_keys_unet(model.model, {}) -> "
                  "comfy.lora.load_lora(..., log_missing=True)  "
                  "（与线上 sd.py:115 完全同路径）"),
       "bases": {}}

for bname, bpath in BASES.items():
    if not Path(bpath).is_file():
        out["bases"][bname] = {"ERROR": "base missing"}
        continue
    print(f"[load] {bname} ...", flush=True)
    t0 = time.time()
    try:
        mp = comfy.sd.load_diffusion_model(bpath)
    except Exception as exc:
        out["bases"][bname] = {"ERROR": f"{type(exc).__name__}: {exc}"}
        continue
    key_map = comfy_lora.model_lora_keys_unet(mp.model, {})
    rec = {"base_path": bpath, "load_sec": round(time.time() - t0, 1),
           "keymap_size": len(key_map),
           "keymap_has_transformer_alias": any(
               k.startswith("transformer.") for k in key_map),
           "keymap_sample": sorted(key_map)[:6],
           "probes": {}}
    for t in TARGETS + [CONTROL]:
        rec["probes"][t.name] = attach(t, key_map)
    out["bases"][bname] = rec
    print(f"  keymap={len(key_map)} transformer_alias="
          f"{rec['keymap_has_transformer_alias']}", flush=True)
    del mp
    gc.collect()

print(json.dumps(out, indent=2, ensure_ascii=False))
Path(sys.argv[1]).write_text(json.dumps(out, indent=2, ensure_ascii=False))
print(f"\n-> {sys.argv[1]}")
