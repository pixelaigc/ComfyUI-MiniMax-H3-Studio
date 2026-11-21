"""官方 minimax_h3_v1.json（首尾帧模板）的采样链与 sigma shift 固定值。

目的：确定 flf 面板上哪些参数属于「官方固定值」，以及它们的真实数值。
"""
import json

P = "/home/ubuntu/nonebotqq/plugins/nonebot_plugin_novelai/workflows/minimax_h3_v1.json"
wf = json.load(open(P, encoding="utf-8"))

WANT = ("Sigma", "Shift", "Scheduler", "KSampler", "SelfLift", "Sampler",
        "ModelSampling", "Refiner", "CFG", "Guidance", "Resolution",
        "LoraLoader", "UNETLoader", "VAE", "Double")

print("=== 官方 v1.json 的模型链 / 采样链节点全量 ===")
for nid, node in sorted(wf.items(), key=lambda x: (len(x[0]), x[0])):
    if not isinstance(node, dict):
        continue
    ct = node.get("class_type", "")
    if not any(w.lower() in ct.lower() for w in WANT):
        continue
    ins = {k: v for k, v in node.get("inputs", {}).items() if not isinstance(v, list)}
    print("  %-6s %-46s %s" % (nid, ct, json.dumps(ins, ensure_ascii=False)[:260]))

print()
print("=== 全节点 class_type 清单（找 shift / cfg 的真实位置）===")
from collections import Counter
c = Counter(n.get("class_type", "?") for n in wf.values() if isinstance(n, dict))
for ct, n in c.most_common():
    print("   %-52s x%d" % (ct, n))
