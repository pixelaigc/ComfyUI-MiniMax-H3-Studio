"""端到端出图验证：用户选 D1（融合 qkv）提交 MOSS 工作流，看 LoRA 是否真生效。

【为什么必须真出图】
前面所有探针都是「用 comfy.lora.load_lora 挂权重，看 patched/missing」。
但用户真正关心的是：**出图时 LoRA 有没有真的起作用**。

做法：
  1) 通过 8190 的 /api/moss-layout（或直接调 build_moss_t2i）生成工作流，
     里面带 D1
  2) 读出工作流里 LoraLoaderModelOnly 节点的 lora_name
  3) 提交给 ComfyUI 真跑一单
  4) 对比「带 D1」与「不带 D1」两张图的像素差异
     —— 如果 LoRA 没生效，两张图会**完全一致**（同 seed）
     —— 如果生效，会有可测差异

用法：python3 /tmp/e2e_moss_render.py <out.json>
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

STUDIO = "http://127.0.0.1:8190"
COMFY = "http://127.0.0.1:8188"
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/e2e_moss_render.json")

D1 = "moss-effects/moss_image_sft_look_lora.safetensors"


def api(url, payload=None, timeout=180):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def poll(prompt_id, timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            h = api(f"{COMFY}/history/{prompt_id}", timeout=30)
        except Exception:
            time.sleep(2)
            continue
        if prompt_id in h:
            out = h[prompt_id]
            if out.get("status", {}).get("completed") or out.get("outputs"):
                return out
        time.sleep(2)
    raise TimeoutError(f"prompt {prompt_id} 超时")


def build_workflow(with_d1):
    """直接用服务端代码生成工作流（与服务端真实路径一致）。"""
    sys.path.insert(0, "/root/projects/h3studio-live")
    from h3studio import workflows as W
    params = {
        "job_id": "e2e-probe",
        "prompt": "a portrait photo of a woman, studio lighting, 8k",
        "negative_prompt": "",
        "seed": 20261115,
        "aspect": "1:1",
        "tier": "1mp",
        "steps": 6,
        "cfg": 1.0,
        "loras": ([{"name": D1, "strength": 1.0, "enabled": True, "trigger": ""}]
                  if with_d1 else []),
    }
    res = W.build_moss_t2i(params)
    wf = res[0] if isinstance(res, tuple) else res
    return wf


def lora_nodes(wf):
    out = []
    for nid, node in wf.items():
        if node.get("class_type") == "LoraLoaderModelOnly":
            out.append({"id": nid, "lora_name": node["inputs"].get("lora_name"),
                        "strength": node["inputs"].get("strength_model")})
    return out


def image_diff(a_path, b_path):
    """两张图的像素级差异（0 = 完全一样 = LoRA 没生效）。"""
    try:
        from PIL import Image, ImageChops
        import numpy as np
    except Exception as exc:
        return {"error": f"PIL/numpy 不可用: {exc}"}
    a = Image.open(a_path).convert("RGB")
    b = Image.open(b_path).convert("RGB")
    if a.size != b.size:
        b = b.resize(a.size)
    diff = ImageChops.difference(a, b)
    arr = np.asarray(diff).astype("float64")
    return {
        "mean_abs_diff": round(float(arr.mean()), 4),
        "max_abs_diff": int(arr.max()),
        "changed_pixel_ratio": round(float((arr.sum(axis=2) > 0).mean()), 6),
        "identical": bool(arr.sum() == 0),
    }


def run_one(with_d1, tag):
    wf = build_workflow(with_d1)
    nodes = lora_nodes(wf)
    client_id = f"e2e-{tag}"
    # ComfyUI 需要 client_id 才能取回图片
    if isinstance(wf, dict) and "client_id" not in wf:
        wf["client_id"] = client_id
    resp = api(f"{COMFY}/prompt", {"prompt": wf, "client_id": client_id})
    pid = resp.get("prompt_id")
    hist = poll(pid)
    outs = hist.get("outputs", {})
    paths = []
    for node_out in outs.values():
        for img in node_out.get("images", []) or []:
            paths.append(f"{COMFY}/view?filename={img['filename']}"
                         f"&subfolder={img.get('subfolder', '')}"
                         f"&type={img.get('type', 'output')}")
    local = []
    for p in paths:
        try:
            with urllib.request.urlopen(p, timeout=60) as r:
                data = r.read()
            lp = Path(f"/tmp/e2e_{tag}_{len(local)}.png")
            lp.write_bytes(data)
            local.append(str(lp))
        except Exception as exc:
            print(f"取图失败 {p}: {exc}")
    return {"lora_nodes": nodes, "prompt_id": pid, "images": local,
            "history_outputs": {k: list(v.get("images", []) or [])
                                for k, v in outs.items()}}


def main():
    report = {}
    print("=== A: 不带 LoRA（基准）===", flush=True)
    report["without"] = run_one(False, "without")
    print(json.dumps(report["without"], ensure_ascii=False)[:300], flush=True)
    print("\n=== B: 带 D1 ===", flush=True)
    report["with_d1"] = run_one(True, "with_d1")
    print(json.dumps(report["with_d1"], ensure_ascii=False)[:300], flush=True)

    a = report["without"]["images"]
    b = report["with_d1"]["images"]
    if a and b:
        report["diff"] = image_diff(a[0], b[0])
        report["verdict"] = {
            "lora_node_lora_name": [n["lora_name"] for n in report["with_d1"]["lora_nodes"]],
            "images_identical": report["diff"].get("identical"),
            "loRA_had_effect": (not report["diff"].get("identical")
                                and report["diff"].get("changed_pixel_ratio", 0) > 0),
        }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report.get("verdict", {}), ensure_ascii=False, indent=2))
    print(f"\n结果已存 {OUT}")


if __name__ == "__main__":
    main()
