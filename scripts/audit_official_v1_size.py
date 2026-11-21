"""官方 minimax_h3_v1.json（首尾帧模板）的真实尺寸/采样表达。

目的：决定 flf 面板该保留哪「一套」尺寸表达 —— 判据是官方模板的节点，
不是我们的界面习惯。
"""
import json

P = "/home/ubuntu/nonebotqq/plugins/nonebot_plugin_novelai/workflows/minimax_h3_v1.json"
wf = json.load(open(P, encoding="utf-8"))

print("=== 尺寸相关节点 ===")
for nid, node in sorted(wf.items(), key=lambda x: (len(x[0]), x[0])):
    if not isinstance(node, dict):
        continue
    ct = node.get("class_type", "")
    ins = node.get("inputs", {})
    interesting = {k: v for k, v in ins.items()
                   if not isinstance(v, list) and any(
                       s in k.lower() for s in
                       ("width", "height", "megapixel", "resolution",
                        "ratio", "aspect", "arc", "length", "fps",
                        "frame", "steps", "shift", "strength_model",
                        "seed", "denoise", "unet_name", "lora_name"))}
    if interesting or any(s in ct.lower() for s in ("resolution", "size", "sampler", "loraloader", "unet", "shift")):
        print("  %-6s %-42s %s" % (nid, ct, json.dumps(interesting, ensure_ascii=False)[:230]))
