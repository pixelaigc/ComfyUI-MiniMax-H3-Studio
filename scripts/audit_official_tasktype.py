"""官方模板的条件节点 task_type —— 确认哪个文件是首尾帧（FL2VA）模板。"""
import json
import os

W = "/home/ubuntu/nonebotqq/plugins/nonebot_plugin_novelai/workflows"

for fn in sorted(os.listdir(W)):
    if not fn.endswith(".json") or ".bak" in fn:
        continue
    wf = json.load(open(os.path.join(W, fn), encoding="utf-8"))
    if not isinstance(wf, dict):
        continue
    print("=== %s ===" % fn)
    for nid, node in wf.items():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type", "")
        if "Conditioning" in ct or "ToVideo" in ct or "Reference" in ct:
            ins = {k: v for k, v in node.get("inputs", {}).items()
                   if not isinstance(v, list)}
            print("   %-6s %-46s %s" % (nid, ct, json.dumps(ins, ensure_ascii=False)[:200]))
    loras = [(nid, n.get("inputs", {}).get("lora_name"), n.get("inputs", {}).get("strength_model"))
             for nid, n in wf.items()
             if isinstance(n, dict) and "ora" in n.get("class_type", "")]
    print("   LoRA:", loras)
    print()

# h3_executor 的模式 -> 模板文件映射
print("=== h3_executor.py 里 mode/quality -> 模板文件 ===")
import re
src = open("/home/ubuntu/nonebotqq/plugins/nonebot_plugin_novelai/h3_executor.py", encoding="utf-8").read()
for m in re.finditer(r'"(q=\d+|[a-z_]+)"\s*:\s*\{[^{}]*?"file"\s*:\s*"([^"]+)"', src):
    print("   %-12s -> %s" % (m.group(1), m.group(2)))
