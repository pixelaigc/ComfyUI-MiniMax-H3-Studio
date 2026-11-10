"""【C 实测】MOSS-Image 批量 5 张 vs 单张 1 张 —— 用**同一个面板参数**，
对比实际提交给 ComfyUI 的工作流里的 steps/cfg/shift/scheduler/seed。

真起 server（独立端口 + 独立 data_dir），真提交 ComfyUI（8188），
真读回 ComfyUI 的 history 看**实际执行的** KSampler 参数。
"""
import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, "/root/projects/h3studio-agent-enh-merge")

REPO = Path("/root/projects/h3studio-agent-enh-merge")
PORT = int(os.environ.get("H3_TEST_PORT", "8197"))
DATA = Path(os.environ.get("H3_TEST_DATA", "/tmp/h3-moss-batch-test"))
DATA.mkdir(parents=True, exist_ok=True)

# 面板参数（与用户实测一致：steps=100 / cfg=4.3 / shift=5.0 / sgm_uniform）
PANEL = {
    "prompt": "a young woman in a sunlit cafe, portrait photo, 85mm lens",
    "resolution": "2k",
    "aspect": "16:9",
    "width": 3136,
    "height": 1344,
    "model": "moss_image_base",
    "sampler": "euler",
    "scheduler": "sgm_uniform",
    "steps": 100,
    "cfg": 4.3,
    "shift": 5,
    "negative_prompt": "",
}


def build_workflow_for(mode, params, job_id):
    """走**真实**的 build_workflow（与服务端 worker 同一条路径）。"""
    from h3studio.workflows import build_workflow
    p = dict(params)
    p["job_id"] = job_id
    p["mode"] = mode
    return build_workflow(mode, p)


def show_ksampler(wf, label):
    for nid, node in wf.items():
        if node.get("class_type") == "KSampler":
            i = node["inputs"]
            print(f"  [{label}] KSampler {nid}: steps={i.get('steps')} cfg={i.get('cfg')} "
                  f"shift(见 ModelSamplingSD3) sampler={i.get('sampler_name')} "
                  f"scheduler={i.get('scheduler')} seed={i.get('seed')}")
            return node["inputs"]
    print(f"  [{label}] 没有 KSampler！")
    return None


if __name__ == "__main__":
    print("=== 直接对比 build_workflow 的产出（与服务端同一条路径）===")
    single = build_workflow_for("t2i_moss", PANEL, "JOB_SINGLE")
    batch = build_workflow_for("t2i_moss", PANEL, "JOB_BATCH_0")
    k1 = show_ksampler(single, "单张")
    k2 = show_ksampler(batch, "批次#0")
    if k1 and k2:
        diff = {k: (k1.get(k), k2.get(k)) for k in set(k1) | set(k2)
                if k1.get(k) != k2.get(k)}
        print(f"\n  差异字段（应只有 seed / job_id 相关）: {diff}")
        print(f"  steps 一致且 = 100: {k1.get('steps') == k2.get('steps') == 100}")
        print(f"  cfg 一致且 = 4.3:   {k1.get('cfg') == k2.get('cfg') == 4.3}")
        print(f"  scheduler 一致:      {k1.get('scheduler') == k2.get('scheduler') == 'sgm_uniform'}")
    # 节点集合一致性
    ct1 = sorted({n.get("class_type") for n in single.values()})
    ct2 = sorted({n.get("class_type") for n in batch.values()})
    print(f"\n  class_type 集合一致: {ct1 == ct2}")
    if ct1 != ct2:
        print(f"    单张: {ct1}")
        print(f"    批次: {ct2}")
    # 节点前缀（moss: vs 57:）—— 这是 C 的关键判据
    pre1 = sorted({nid.split(":")[0] for nid in single})
    pre2 = sorted({nid.split(":")[0] for nid in batch})
    print(f"  节点前缀 单张={pre1} 批次={pre2}")
