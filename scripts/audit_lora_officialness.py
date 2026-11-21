"""读全部加速 LoRA 权重的真实 safetensors metadata + 官方工作流模板挂载记录。

判据必须来自磁盘字节，不是代码注释。
"""
import json
import os
import struct
import sys

sys.path.insert(0, '/root/projects/h3s-flftruth')
from h3studio import video_models as vm

CANDIDATE_DIRS = [
    "/home/ubuntu/comfyui/models/loras",
    "/mnt/usb2t/comfyui-models/loras",
    "/mnt/nvme1t_colorful/comfyui-models/loras",
]

OFFICIAL_WORKFLOWS = "/home/ubuntu/nonebotqq/plugins/nonebot_plugin_novelai/workflows"


def read_header(path):
    with open(path, "rb") as fh:
        n = struct.unpack("<Q", fh.read(8))[0]
        return json.loads(fh.read(n))


def locate(name):
    for d in CANDIDATE_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


# ---- 1) 官方工作流模板真实挂载的 LoRA ----
mounted = {}
for fn in sorted(os.listdir(OFFICIAL_WORKFLOWS)):
    if not fn.endswith(".json"):
        continue
    p = os.path.join(OFFICIAL_WORKFLOWS, fn)
    try:
        wf = json.load(open(p, encoding="utf-8"))
    except Exception:
        continue
    if not isinstance(wf, dict):
        continue
    for nid, node in wf.items():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type", "")
        if "ora" in ct:
            ln = node.get("inputs", {}).get("lora_name", "")
            if ln:
                mounted.setdefault(os.path.basename(ln), []).append(
                    "%s#%s %s strength=%s" % (fn, nid, ct,
                                              node.get("inputs", {}).get("strength_model")))

print("=== 官方 H3 工作流模板真实挂载的 LoRA ===")
for name, hits in sorted(mounted.items()):
    print("  %-52s %s" % (name, hits))

print()
print("=== 各加速 LoRA 权重的真实 metadata ===")
rows = []
for key in sorted(vm.all_accel_lora_keys()):
    meta = vm.accel_lora_meta(key) or {}
    name = meta.get("name") or ""
    path = locate(name)
    print("  key=%s" % key)
    print("     name=%s" % name)
    print("     path=%s" % (path or "NOT FOUND"))
    if not path:
        rows.append((key, name, None, None, False))
        continue
    head = read_header(path)
    md = head.get("__metadata__") or {}
    tensors = [k for k in head if k != "__metadata__"]
    print("     tensors=%d size=%.1fMB" % (len(tensors), os.path.getsize(path) / 1e6))
    for k in ("base_model", "license", "model", "source", "converted_layout",
              "sampling", "format"):
        if k in md:
            print("     metadata.%-17s = %s" % (k, str(md[k])[:120]))
    other = {k: str(v)[:80] for k, v in md.items()
             if k not in ("base_model", "license", "model", "source",
                          "converted_layout", "sampling", "format")}
    if other:
        print("     metadata(其它)=%s" % other)
    print("     官方模板挂载=%s" % (mounted.get(os.path.basename(name)) or "否"))
    rows.append((key, name, md, path, os.path.basename(name) in mounted))

print()
print("=== 判定汇总 ===")
for key, name, md, path, is_mounted in rows:
    md = md or {}
    official_meta = ("minimax-h3" in str(md.get("base_model", "")).lower()
                     or "minimax h3" in str(md.get("license", "")).lower())
    third_party = "lightx2v" in name.lower() or "lightx2v" in str(md.get("source", "")).lower()
    print("  %-26s mounted=%-5s official_meta=%-5s third_party=%s" % (
        key, is_mounted, official_meta, third_party))
