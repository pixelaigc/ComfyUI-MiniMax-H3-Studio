#!/usr/bin/env python3
"""核查 Z-Image 放大路径（>=3MP）是否覆盖用户参数 —— 直接调 builder 打印图。"""
import json
import sys
import traceback

sys.path.insert(0, "/root/projects/h3studio-feasibility")

from h3studio import workflows  # noqa: E402

CASES = [
    ("1MP 直出路径 (1360x768)", {"width": 1360, "height": 768}),
    ("3.5MP 1440p (2560x1440)", {"width": 2560, "height": 1440}),
    ("4.2MP 2K (2800x1568)", {"width": 2800, "height": 1568}),
    ("8.3MP 4K (3936x2208)", {"width": 3936, "height": 2208}),
]

USER = {"steps": 37, "cfg": 6.5, "sampler": "dpmpp_3m_sde", "scheduler": "karras", "shift": 8.0}

out = {}
for label, wh in CASES:
    params = {"prompt": "FEASIBILITY-UPSCALE-PROBE", "seed": 123456789,
              "loras": [], "accel_lora": None, "job_id": "probe0001"}
    params.update(USER)
    params.update(wh)
    try:
        g = workflows.build_t2i(params)
    except Exception as exc:
        out[label] = {"error": f"{type(exc).__name__}: {exc}",
                      "trace": traceback.format_exc()[-900:]}
        print(f"[{label}] BUILD FAILED: {type(exc).__name__}: {exc}")
        print(traceback.format_exc()[-900:])
        continue

    ks = None
    upscale_nodes = []
    for nid, node in g.items():
        ct = node.get("class_type", "")
        if ct == "KSampler":
            ks = (nid, node["inputs"])
        if ct.startswith("ImageScale") or ct.startswith("ImageUpscale") or ct.startswith("Upscale"):
            upscale_nodes.append((nid, ct))
    info = {
        "node_count": len(g),
        "ksampler": {"id": ks[0], "steps": ks[1].get("steps"), "cfg": ks[1].get("cfg"),
                     "sampler_name": ks[1].get("sampler_name"),
                     "scheduler": ks[1].get("scheduler"), "denoise": ks[1].get("denoise")} if ks else None,
        "upscale_nodes": upscale_nodes,
        "has_upscale_chain": bool(upscale_nodes),
        "shift_node": [(nid, node["inputs"].get("shift"))
                       for nid, node in g.items()
                       if node.get("class_type") == "ModelSamplingAuraFlow"],
        "latent_wh": [(nid, node["inputs"].get("width"), node["inputs"].get("height"))
                      for nid, node in g.items()
                      if node.get("class_type") in ("EmptySD3LatentImage", "EmptyLatentImage")],
    }
    out[label] = info
    print(f"\n[{label}] nodes={info['node_count']} upscale_chain={info['has_upscale_chain']}")
    print(f"   KSampler: {info['ksampler']}")
    print(f"   shift节点: {info['shift_node']}")
    print(f"   latent尺寸: {info['latent_wh']}")
    if info["has_upscale_chain"]:
        verdict = "用户参数被写死覆盖" if (info["ksampler"] and info["ksampler"]["steps"] != USER["steps"]) else "参数保留"
        print(f"   >>> 判定: {verdict}")

with open("/root/projects/h3studio-feasibility/evidence/feasibility/upscale_path_probe.json",
          "w", encoding="utf-8") as fh:
    json.dump({"user_params": USER, "cases": out}, fh, ensure_ascii=False, indent=1)
print("\n[saved] upscale_path_probe.json")
