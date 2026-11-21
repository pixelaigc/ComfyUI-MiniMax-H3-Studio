"""12 枚加速 LoRA 的完整 metadata 对照，用于确定「官方档」的可验证判据。"""
import json
import os
import struct
import sys

sys.path.insert(0, '/root/projects/h3s-flftruth')
from h3studio import video_models as vm

DIRS = ["/home/ubuntu/comfyui/models/loras",
        "/mnt/usb2t/comfyui-models/loras",
        "/mnt/nvme1t_colorful/comfyui-models/loras"]


def locate(name):
    for d in DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


rows = {}
for key in sorted(vm.all_accel_lora_keys()):
    meta = vm.accel_lora_meta(key) or {}
    name = meta.get("name") or ""
    path = locate(name)
    md = {}
    keys = []
    if path:
        with open(path, "rb") as fh:
            n = struct.unpack("<Q", fh.read(8))[0]
            head = json.loads(fh.read(n))
        md = head.get("__metadata__") or {}
        keys = [k for k in head if k != "__metadata__"]
    rows[key] = {"name": name, "path": path, "metadata": md,
                 "tensor_prefixes": sorted({k.split(".")[0] for k in keys}),
                 "tensor_count": len(keys)}

print(json.dumps(rows, ensure_ascii=False, indent=1))
