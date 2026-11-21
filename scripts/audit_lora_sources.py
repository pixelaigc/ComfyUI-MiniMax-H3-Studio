import sys, json
sys.path.insert(0, '/root/projects/h3s-flftruth')
from h3studio import video_models as vm, workflows as wf, gen_defaults as gd, agent_schema as ag

print("=== A) gen_defaults t2v 的 accel_lora 选项 ===")
fields, _ = gd._mode_fields("t2v")
for f in fields:
    if f["name"] == "accel_lora":
        for o in f["options"]:
            print("   ", o["value"])

print()
print("=== B) gen_defaults flf 的 lora 选项 ===")
fields, _ = gd._mode_fields("flf")
for f in fields:
    if f["name"] == "lora":
        for o in f["options"]:
            print("   ", o["value"])

print()
print("=== C) agent_schema.mode_loras('flf') ===")
print("   ", ag.mode_loras("flf"))

print()
print("=== D) 主面板 options_payload 的 accel 清单来源 ===")
p = vm.options_payload()
combos = p.get("accel_lora_combos") or p.get("accel_loras") or {}
print("   keys in payload:", [k for k in p if "lora" in k.lower()])
for k in ("accel_lora_combos", "accel_loras"):
    v = p.get(k)
    if v:
        print("   %s -> %d 项: %s" % (k, len(v), list(v)[:12] if isinstance(v, dict) else v[:12]))

print()
print("=== E) VIDEO_ACCEL_LORA_COMBOS 元数据字段名 ===")
for k, meta in list(vm.VIDEO_ACCEL_LORA_COMBOS.items())[:3]:
    print("   ", k, "->", sorted(meta.keys()))
