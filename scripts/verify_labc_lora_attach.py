#!/usr/bin/env python3
"""L-A 实测：3 个补齐后的 LoRA 本体 + 真实 ComfyUI 加载路径。

覆盖：
  1) 本体完整性（磁盘字节 / 头部声明字节 / 缺字节 / safe_open）
  2) **真实 ComfyUI 加载路径**（comfy.lora.model_lora_keys_unet + load_lora）
     -> patched / missing 数值
  3) /api/image-loras 列表里不再有损坏文件被当作 compatible=true 下发
  4) fail-closed：人为截断的文件仍被判 broken / compatible=false
"""
import json
import logging
import struct
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/home/ubuntu/comfyui")
sys.path.insert(0, "/root/projects/h3studio-labc")

import torch  # noqa: E402
from comfy import lora as comfy_lora, lora_convert, utils  # noqa: E402

from h3studio import loras as lora_registry  # noqa: E402
from h3studio import moss_lora_compat as compat  # noqa: E402
from h3studio import moss_lora_split as split  # noqa: E402

LORAS = Path("/mnt/usb2t/comfyui-models/loras")

TARGETS = [
    LORAS / "AWPortrait-QW" / "AWPortrait-QW.safetensors",
    LORAS / "QIE-SRD" / "Qwen-Image-Edit-Semi-Realistic-Detailer-v1.safetensors",
    LORAS / "ipadapter" / "ip-adapter-faceid-plus_sd15_lora.safetensors",
]

#: 本机存在的底模（用于真实挂载验证）
BASES = {
    "qwen_image_2.1_bf16":
        "/home/ubuntu/comfyui/models/diffusion_models/qwen_image_2.1_bf16.safetensors",
    "Qwen_Image_Edit_2509_FP8":
        "/mnt/nvme1t_colorful/comfyui-models/diffusion_models/Qwen_Image_Edit_2509_FP8.safetensors",
    "sd_xl_base_unet_fp16":
        "/home/ubuntu/comfyui/models/diffusion_models/sd_xl_base_unet_fp16.safetensors",
}


class FakeModel:
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
    return comfy_lora.model_lora_keys_unet(FakeModel(list(raw.keys())), {})


def body_integrity(path):
    rec = {"path": str(path), "size_bytes": path.stat().st_size,
           "header_declared_bytes": None, "declared_total_bytes": None,
           "missing_bytes": None, "tensors": None, "safe_open": None}
    try:
        with open(path, "rb") as f:
            n = struct.unpack("<Q", f.read(8))[0]
            rec["header_declared_bytes"] = n
            meta = json.loads(f.read(n).decode("utf-8"))
            max_end = 0
            for k, v in meta.items():
                if k == "__metadata__" or not isinstance(v, dict):
                    continue
                try:
                    max_end = max(max_end, int(v["data_offsets"][1]))
                except Exception:
                    pass
            rec["tensors"] = len([k for k in meta if k != "__metadata__"])
            rec["declared_total_bytes"] = 8 + n + max_end
            rec["missing_bytes"] = rec["declared_total_bytes"] - rec["size_bytes"]
    except Exception as exc:
        rec["safe_open"] = f"{type(exc).__name__}: {exc}"
        return rec
    try:
        from safetensors import safe_open
        with safe_open(str(path), framework="pt") as f:
            list(f.keys())
        rec["safe_open"] = "ok"
    except Exception as exc:
        rec["safe_open"] = f"{type(exc).__name__}: {exc}"
    return rec


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
            "missing": len(cap.missing), "missing_sample": cap.missing[:3]}


out = {"targets": {}, "attach": {}, "api": {}, "failclosed": {}}

# ---- 1) 本体完整性 ----
for t in TARGETS:
    out["targets"][str(t)] = body_integrity(t)

# ---- 2) 真实 ComfyUI 加载路径 ----
for bname, bpath in BASES.items():
    if not Path(bpath).is_file():
        out["attach"][bname] = {"ERROR": "base missing"}
        continue
    km = keymap_from_unet(bpath)
    out["attach"][bname] = {
        "keymap_size": len(km),
        "probes": {t.name: attach(t, km) for t in TARGETS},
    }

# ---- 3) /api/image-loras：损坏文件不得以 compatible=true 下发 ----
def api_fetch(base):
    import urllib.parse
    import urllib.request
    url = ("http://127.0.0.1:8190/api/image-loras?base="
           + urllib.parse.quote(base))
    with urllib.request.urlopen(url, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


DAMAGED_KEYS = ("AWPortrait-QW", "Semi-Realistic-Detailer", "ip-adapter-faceid")
for base in ("Z-Image", "Qwen-Image 2.1", "MOSS-Image"):
    try:
        data = api_fetch(base)
    except Exception as exc:
        out["api"][base] = {"ERROR": f"{type(exc).__name__}: {exc}"}
        continue
    av = data.get("available", [])
    inc = data.get("incompatible", [])
    hits = [i for i in av + inc
            if any(k in (i.get("name") or "") for k in DAMAGED_KEYS)]
    out["api"][base] = {
        "available": len(av), "incompatible": len(inc),
        "available_compatible_false": sum(
            1 for i in av if not i.get("compatible", True)),
        "damaged_hits": [
            {k: i.get(k) for k in ("name", "compatible", "conversion_status",
                                   "base_verdict", "incompatible_reason")}
            for i in hits],
    }

# ---- 4) fail-closed：人为截断的文件仍判 broken / compatible=false ----
probe = Path("/tmp/labc_truncated_probe.safetensors")
src = TARGETS[2]
raw = src.read_bytes()
probe.write_bytes(raw[: len(raw) // 2])
out["failclosed"] = {
    "probe": str(probe),
    "probe_size": probe.stat().st_size,
    "conversion_status": split.conversion_status(str(probe)).get("status"),
    "conversion_reason": split.conversion_status(str(probe)).get("reason"),
    "compatible": compat.check_moss_compatibility(str(probe)).get("compatible"),
    "reason": compat.check_moss_compatibility(str(probe)).get("reason"),
}
probe.unlink()

print(json.dumps(out, indent=2, ensure_ascii=False))
Path(sys.argv[1]).write_text(json.dumps(out, indent=2, ensure_ascii=False))
print(f"\n-> {sys.argv[1]}")
