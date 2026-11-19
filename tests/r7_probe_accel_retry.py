#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7 探针 3：retry（重试）路径上的加速 LoRA 是否被静默丢弃。

【怀疑的对象】
  artifact_meta.apply_to_params 在 worker 回写产物元数据时写：
      params["accel_lora"]        = first.get("name")   # <- 权重**文件名**
      params["fixed_accel_lora"]  = first.get("name")
      params["accel_loras"][0].key= first.get("key")
  而 video_models.accel_lora_meta(key) 的清单里存的是**友好 key**
  （dmad_4step_critic / ckpt850 …），不是权重文件名。

  ⇒ 回写之后 params["accel_lora"] 变成了权重文件名。
  ⇒ retry_job 把 params 整份拷过来 -> 再建任务 -> 工作流层
     _resolve_accel_chain -> accel_lora_meta(权重文件名) -> None
     -> **静默不挂加速 LoRA**（不报错！），步数却还是蒸馏档（8 步）
     => 跑的是「8 步采样的非蒸馏权重」= 一条不存在的路径。

只读探针，不改任何被审文件。
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import artifact_meta, video_models as vmodels  # noqa: E402
from h3studio import video_workflow as vworkflow  # noqa: E402

print("=" * 72)
print("【1】清单里 DMAD 那一枚的 key / name")
meta = vmodels.accel_lora_meta("dmad_4step_critic")
print("  accel_lora_meta('dmad_4step_critic') ->")
if meta:
    print("    key  =", "dmad_4step_critic")
    print("    name =", meta.get("name"))
    print("    display =", meta.get("display"))
    print("    recommended_steps =", meta.get("recommended_steps"))
print("  accel_lora_meta('dmad_minimax_h3_4step_full_critic_comfyui') ->",
      vmodels.accel_lora_meta("dmad_minimax_h3_4step_full_critic_comfyui"))

print()
print("=" * 72)
print("【2】artifact_meta.apply_to_params 回写后 params 变成什么样")
params = {
    "accel_lora": "dmad_4step_critic",
    "accel_lora_strength": 1.0,
    "accel_loras": [{"key": "dmad_4step_critic", "strength": 1.0,
                     "enabled": True}],
    "steps": 8, "sampler": "euler", "scheduler": "beta",
}
extracted = {
    "steps": 8, "scheduler": "beta", "sampler": "euler", "cfg": 1.0,
    "seed": 123, "width": 1088, "height": 1920,
    "model_file": "x.safetensors",
    "loras": [{
        "name": "dmad_minimax_h3_4step_full_critic_comfyui",
        "raw_name": "dmad_minimax_h3_4step_full_critic_comfyui.safetensors",
        "strength": 1.0,
        "loader": "LoraLoaderModelOnly",
    }],
}
artifact_meta.apply_to_params(params, extracted)
print("  params['accel_lora']       =", repr(params.get("accel_lora")))
print("  params['fixed_accel_lora'] =", repr(params.get("fixed_accel_lora")))
print("  params['accel_loras'][0]   =", params.get("accel_loras"))
print("  params['steps']            =", params.get("steps"))

print()
print("=" * 72)
print("【3】retry（整份拷贝 params）之后，工作流层还挂不挂加速 LoRA")
retry_params = json.loads(json.dumps(params))   # retry_job 就是这么拷的
key, strength = vmodels.resolve_accel_lora_slot(retry_params)
print("  resolve_accel_lora_slot ->", repr(key), strength)
print("  accel_lora_meta(该 key) ->", vmodels.accel_lora_meta(key))
chain = vworkflow._resolve_accel_chain(retry_params, {"unet": "x.safetensors"})
print("  _resolve_accel_chain ->", chain)
if not chain:
    print("  ==> 加速 LoRA 链为空：**工作流里一���加速 LoRA 都不挂**")
    print("      但 params['steps'] =", retry_params.get("steps"),
          "（蒸馏档步数）⇒ 8 步采样的非蒸馏权重 = 一条不存在的路径")
    print("      且整条链路**没有任何报错** —— 静默降级。")

print()
print("=" * 72)
print("【4】对照组：面板正常提交（friendly key）")
ok_params = {"accel_lora": "dmad_4step_critic", "accel_lora_strength": 1.0,
             "steps": 8}
chain_ok = vworkflow._resolve_accel_chain(ok_params, {"unet": "x.safetensors"})
print("  _resolve_accel_chain ->", chain_ok)

shutil.rmtree(tempfile.gettempdir(), ignore_errors=True) if False else None
