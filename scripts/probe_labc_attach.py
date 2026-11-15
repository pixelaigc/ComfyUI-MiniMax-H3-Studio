#!/usr/bin/env python3
"""L-A 真实 ComfyUI 加载路径验证（修正版）。

⚠️ 第一版探针有 bug：FakeModel.state_dict() 必须用 **diffusion_model. 前缀**
的键（ComfyUI 的 `model_lora_keys_unet` 只登记 `diffusion_model.*` 开头的键），
否则 key_map 是空的 -> 一切都判成 missing。

本探针走 ComfyUI 真实代码路径：
  comfy.lora.model_lora_keys_unet(model, key_map) -> comfy.lora.load_lora(...)
"""
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, "/home/ubuntu/comfyui")
sys.path.insert(0, "/root/projects/h3studio-labc")

import torch  # noqa: E402
from comfy import lora as comfy_lora, lora_convert, utils  # noqa: E402


class FakeModel:
    """模拟 ComfyUI 的 BaseModel：state_dict 的键带 diffusion_model. 前缀。"""

    def __init__(self, weight_keys):
        self._sdk = ["diffusion_model." + k for k in weight_keys]
        self.model_config = type(
            "C", (), {"unet_config": {"dim": 3072, "num_layers": 42}})()

    def state_dict(self):
        return {k: torch.zeros(1) for k in self._sdk}


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.missing = []

    def emit(self, record):
        msg = record.getMessage()
        if "lora key not loaded" in msg:
            self.missing.append(msg.split("lora key not loaded:")[-1].strip())


def keymap_from_unet(path):
    raw = utils.load_torch_file(str(path), safe_load=True)
    model = FakeModel(list(raw.keys()))
    return comfy_lora.model_lora_keys_unet(model, {})


def probe(path, key_map):
    p = Path(path)
    if not p.exists():
        return {"path": str(p), "exists": False}
    try:
        raw = utils.load_torch_file(str(p), safe_load=True)
    except Exception as exc:
        return {"path": str(p), "exists": True,
                "size_bytes": p.stat().st_size,
                "ERROR": f"{type(exc).__name__}: {exc}"}
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
    return {"path": str(p), "exists": True,
            "size_bytes": p.stat().st_size,
            "tensors": len(raw),
            "patched": len(loaded),
            "missing": len(cap.missing),
            "missing_sample": cap.missing[:3]}


BASES = {
    "qwen_image_2.1_bf16":
        "/home/ubuntu/comfyui/models/diffusion_models/qwen_image_2.1_bf16.safetensors",
    "Qwen_Image_Edit_2509_FP8":
        "/mnt/nvme1t_colorful/comfyui-models/diffusion_models/Qwen_Image_Edit_2509_FP8.safetensors",
    "sd_xl_base_unet_fp16":
        "/home/ubuntu/comfyui/models/diffusion_models/sd_xl_base_unet_fp16.safetensors",
}

CANDIDATES = [
    "/tmp/labc_dl/Qwen-Image-Edit-Semi-Realistic-Detailer-v1.safetensors",
    "/tmp/labc_dl/ip-adapter-faceid-plus_sd15_lora.safetensors",
    "/mnt/usb2t/comfyui-models/loras/AWPortrait-QW/AWPortrait-QW_1.0.safetensors",
    "/mnt/usb2t/comfyui-models/loras/ipadapter/ip-adapter-faceid-plusv2_sd15_lora.safetensors",
]

out = {}
for bname, bpath in BASES.items():
    if not Path(bpath).is_file():
        out[bname] = {"ERROR": "base missing"}
        continue
    km = keymap_from_unet(bpath)
    out[bname] = {
        "keymap_size": len(km),
        "keymap_sample": sorted(km)[:5],
        "probes": {Path(c).name: probe(c, km) for c in CANDIDATES},
    }
print(json.dumps(out, indent=2, ensure_ascii=False))
Path(sys.argv[1]).write_text(json.dumps(out, indent=2, ensure_ascii=False))
