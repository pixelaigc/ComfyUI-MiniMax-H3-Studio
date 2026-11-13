#!/usr/bin/env python3
"""H3 效果 LoRA —— 一键验证脚本（可复跑的完整证据）。

跑完输出 4 段证据：
  1. 发现机制：清单来自真实扫盘（每条都对应磁盘上的真实文件）
  2. 兼容性：键绑定 + 形状比对（两层判据，对真实底模权重头）
  3. 工作流注入：LoraLoaderModelOnly 节点 + strength_model 参数映射
  4. 服务端闸门：脏值被拒绝的原因

用法：
    cd /root/projects/h3studio-effect-lora
    python3 docs/effect-lora-evidence/verify_effect_lora.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from h3studio import h3_effect_lora_docs as doc_parser  # noqa: E402
from h3studio import h3_effect_loras as effect_registry  # noqa: E402
from h3studio import job_meta  # noqa: E402
from h3studio import video_models as vmodels  # noqa: E402
from h3studio import video_workflow as vw  # noqa: E402

OK = "PASS"
BAD = "FAIL"


def line(char="-", n=78):
    return char * n


def section(title):
    print()
    print(line("="))
    print(title)
    print(line("="))


def main() -> int:
    failures: list[str] = []

    # ------------------------------------------------------------ 1
    section("1. 发现机制 —— 清单来自真实扫盘")
    items = effect_registry.discover_effect_loras()
    print(f"发现效果 LoRA: {len(items)} 枚")
    if not items:
        print(f"  {BAD} 本机没有发现任何效果 LoRA")
        failures.append("discover")
    for entry in items:
        path = Path(entry["path"])
        exists = path.is_file()
        if not exists:
            failures.append(f"file-missing:{entry['name']}")
        print(f"  {OK if exists else BAD} {entry['name']:56s} "
              f"key={entry['key_format']:16s} mods={entry['modules']:4d}")

    # ------------------------------------------------------------ 2
    section("2. 兼容性 —— 键绑定 + 形状比对（真实底模权重头）")
    model = vmodels.VIDEO_MODELS[vmodels.VIDEO_DEFAULT_MODEL]
    unet = vmodels.find_weight(model["unet"])
    print(f"底模: {model['unet']}")
    print(f"路径: {unet}")
    compatible = 0
    for entry in items:
        check = effect_registry.check_effect_lora_compat(
            Path(entry["path"]), unet)
        good = check["compatible"]
        if good:
            compatible += 1
        else:
            failures.append(f"incompatible:{entry['name']}")
        print(f"  {OK if good else BAD} {entry['name'][:52]:52s} "
              f"命中 {check['shape_ok']:4d}/{check['total']:<4d} "
              f"不匹配 {check['shape_mismatch']:3d} 缺层 {check['missing']:3d} "
              f"adaln空转 {check['adaln_spin']:3d}")
    print(f"\n兼容: {compatible}/{len(items)}")

    # ------------------------------------------------------------ 3
    section("3. 工作流注入 —— LoraLoaderModelOnly + strength 映射")
    if len(items) >= 2:
        first, second = items[0], items[1]
        params = {
            "job_id": "verify",
            "model_key": vmodels.VIDEO_DEFAULT_MODEL,
            "width": 768, "height": 1344, "duration": 5, "seed": 1,
            "prompt": "a cat walking",
            "fixed_accel_lora": "ckpt850",
            "effect_loras": [
                {"name": first["name"], "strength": 0.55, "enabled": True,
                 "trigger": "3dsrx"},
                {"name": second["name"], "strength": 0.70, "enabled": True,
                 "trigger": "DY"},
            ],
        }
        prompt = vw.build_t2v_panel(params)
        loaders = sorted(
            [(nid, n) for nid, n in prompt.items()
             if n.get("class_type") == "LoraLoaderModelOnly"],
            key=lambda kv: kv[0])
        print(f"生成 LoraLoaderModelOnly 节点: {len(loaders)} 个")
        for nid, node in loaders:
            print(f"  {nid}  lora_name={node['inputs']['lora_name']}")
            print(f"       strength_model={node['inputs']['strength_model']}")
            print(f"       model={node['inputs']['model']}")
        if len(loaders) != 2:
            failures.append("workflow-nodes")
        else:
            if abs(loaders[0][1]["inputs"]["strength_model"] - 0.55) > 1e-6:
                failures.append("strength-1")
            if abs(loaders[1][1]["inputs"]["strength_model"] - 0.70) > 1e-6:
                failures.append("strength-2")
            if loaders[1][1]["inputs"]["model"] != [loaders[0][0], 0]:
                failures.append("chain-order")

        print("\n模型链（采样器 -> 底模）：")
        cur = prompt["9"]["inputs"]["model"]
        while True:
            nid, _ = cur
            node = prompt[nid]
            label = (node.get("inputs", {}).get("lora_name")
                     or node.get("inputs", {}).get("unet_name") or "")
            print(f"  {nid:10s} {node['class_type']:26s} {label}")
            nxt = node.get("inputs", {}).get("model")
            if not nxt:
                break
            cur = nxt

        text = prompt["105:104"]["inputs"]["prompt"]
        print(f"\n提示词（触发词已合并）: {text}")
        if "3dsrx" not in text or "DY" not in text:
            failures.append("trigger-merge")

    # ------------------------------------------------------------ 4
    section("4. 服务端闸门 —— 脏值被拒绝")
    import server
    base = {
        "job_id": "verify", "model_key": vmodels.VIDEO_DEFAULT_MODEL,
        "width": 768, "height": 1344, "duration": 5, "prompt": "x",
        "seed": 1, "fixed_accel_lora": "ckpt850",
    }
    cases = [
        ("正常值", [{"name": items[0]["name"], "strength": 0.55,
                     "enabled": True, "trigger": "t"}], True),
        ("不存在的文件", [{"name": "h3/nope.safetensors", "strength": 1.0}],
         False),
        ("非数字强度", [{"name": items[0]["name"], "strength": "abc"}], False),
        ("图像 LoRA（键结构不对）",
         [{"name": "z-image-effects/AWPortrait-Z.safetensors",
           "strength": 1.0}], False),
    ]
    for label, eff, should_pass in cases:
        out, error = server.normalize_t2v_params(dict(base, effect_loras=eff))
        passed = (error == "")
        mark = OK if passed == should_pass else BAD
        if mark == BAD:
            failures.append(f"gate:{label}")
        print(f"  {mark} {label:24s} -> {error or '通过'}")

    # ------------------------------------------------------------ 5
    section("5. 任务卡片摘要 —— 显示用了哪几枚 + 强度")
    if len(items) >= 2:
        summary = job_meta.summarize("t2v", {
            "model_key": vmodels.VIDEO_DEFAULT_MODEL,
            "fixed_accel_lora": "ckpt850",
            "effect_loras": [
                {"name": items[0]["name"], "strength": 0.55, "enabled": True},
                {"name": items[1]["name"], "strength": 0.70, "enabled": True},
            ],
        })
        labels = summary.get("effect_lora_labels") or []
        print(json.dumps(labels, ensure_ascii=False, indent=2))
        if len(labels) != 2:
            failures.append("summary")

    # ------------------------------------------------------------ 结论
    section("结论")
    if failures:
        print(f"{BAD} {len(failures)} 项未通过：")
        for f in failures:
            print(f"   - {f}")
        return 1
    print(f"{OK} 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
