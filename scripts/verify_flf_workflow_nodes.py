# -*- coding: utf-8 -*-
"""从**真实工作流 prompt**证明 LoRA 接线正确（比日志更硬）。

判据：把本轮 flf 任务用的真实参数喂给 ``h3_video_modes.build_flf``，
然后**逐节点**列出模型链上的 LoRA 节点与采样节点，
证明：
  * LoRA 真的进了节点（不是被忽略的字段）；
  * 加载节点类型与权重键名形态匹配（Turbo 系 -> MiniMaxH3TurboLoRA）；
  * 宽高 / 时长 / 采样档位都进了对应节点。

用法：python3 verify_flf_workflow_nodes.py <out_json>
"""
import json
import sys

sys.path.insert(0, "/root/projects/h3s-flftruth")
from h3studio import h3_video_modes as hm   # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/flf_nodes.json"

#: 本轮线上真实落库的 params（见 evidence/.../generation/flf_live_job.json）
REAL_PARAMS = {
    "mode": "flf",
    "job_id": "f6441e31decc",
    "prompt": "首尾帧验证：一朵花在镜头前缓慢开放，暖色调",
    "first_frame": "h3studio/1340147a65e6d028__e2e-sample-3561685-52885.png",
    "last_frame": "h3studio/4282aa75a429df99__i-zimage-000221.png",
    "width": 1216,
    "height": 672,
    "megapixels": 0.779,
    "resolution_percent": 100.0,
    "aspect": "16:9",
    "duration": 5.0,
    "model_key": "fl2va_pruned_int8",
    "accel_lora": "ckpt850",
    "accel_lora_strength": 1.0,
    "accel_lora_loader": "MiniMaxH3TurboLoRA",
    "lora": "ckpt850",
    "lora_strength": 1.0,
    "artifact_model_file": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    "audio_track_source": "generated",
}

WATCH = {
    "UNETLoader": "底模权重",
    "MiniMaxH3TurboLoRA": "加速 LoRA（Turbo 节点）",
    "LoraLoaderModelOnly": "加速 LoRA（核心节点）",
    "MiniMaxH3AudioConditioningT8": "尺寸 + 时长 + 任务类型",
    "BasicScheduler": "采样档位（步数 / 调度器）",
    "KSamplerSelect": "采样器",
    "MiniMaxH3SigmaShift": "sigma shift",
}


def main():
    params = dict(REAL_PARAMS)
    wf = hm.build_flf(params)

    nodes = []
    for nid, node in sorted(wf.items()):
        ct = node.get("class_type", "")
        if not any(k in ct for k in WATCH):
            continue
        inputs = {k: v for k, v in node.get("inputs", {}).items()
                  if not isinstance(v, list)}
        nodes.append({
            "node_id": nid,
            "class_type": ct,
            "role": WATCH.get(ct, ""),
            "inputs": inputs,
        })

    lora_nodes = [n for n in nodes if "LoRA" in n["class_type"]]
    cond = next((n for n in nodes
                 if n["class_type"] == "MiniMaxH3AudioConditioningT8"), None)
    sched = next((n for n in nodes
                  if n["class_type"] == "BasicScheduler"), None)

    result = {
        "source": "用线上真实落库 params 重新构建工作流（h3studio/h3_video_modes.py:804 build_flf）",
        "real_params": REAL_PARAMS,
        "nodes": nodes,
        "assertions": {
            "lora_node_present": len(lora_nodes) == 1,
            "lora_node_is_turbo": bool(lora_nodes)
            and lora_nodes[0]["class_type"] == "MiniMaxH3TurboLoRA",
            "lora_weight_is_ckpt850": bool(lora_nodes)
            and "ckpt850" in str(lora_nodes[0]["inputs"].get("lora_name", "")),
            "lora_strength_1": bool(lora_nodes)
            and abs(float(lora_nodes[0]["inputs"].get("strength", 0)) - 1.0) < 1e-6,
            "unet_is_fl2va_pruned_int8": any(
                n["class_type"] == "UNETLoader"
                and "fl2va_pruned_int8" in str(n["inputs"].get("unet_name", ""))
                for n in nodes),
            "size_1216x672": bool(cond)
            and cond["inputs"].get("width") == 1216
            and cond["inputs"].get("height") == 672,
            "task_type_fl2va": bool(cond)
            and cond["inputs"].get("task_type") == "FL2VA",
            "scheduler_steps_4": bool(sched)
            and sched["inputs"].get("steps") == 4,
        },
    }
    result["all_pass"] = all(result["assertions"].values())
    text = json.dumps(result, ensure_ascii=False, indent=2)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)


if __name__ == "__main__":
    main()
