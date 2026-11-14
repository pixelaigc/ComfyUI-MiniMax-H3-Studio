"""端到端判定缺陷 A：用户选 D1 提交 MOSS 工作流，ComfyUI 真的挂上了吗？

【为什么必须端到端】
`/api/image-loras` 下发 compatible=true 只说明「结构判定通过」。
用户真正的路径是：选 D1 -> 提交 -> workflows.py 走 ensure_normalized
-> 工作流里挂 lora_name=<某个文件> -> ComfyUI 真正 load_lora。

如果 ensure_normalized 成功转换并把派生文件挂上去，那么：
  * D1 以 compatible=true 下发是**正确**的（它确实可用）
  * 上一轮独立验收说的「半静默失效」只在「绕过转换直接挂原文件」时成立

本脚本做两件事：
  1) 走**真实的服务端代码路径**（h3studio.workflows 的 MOSS LoRA 建链）
     生成工作流，看 lora_name 指向原文件还是派生文件
  2) 用**真实 ComfyUI 的 comfy.lora.load_lora** 挂载那个最终文件，
     看 patched / missing / key-not-loaded 警告

用法：python3 /tmp/e2e_d1_attach.py <out.json>
"""
import json
import logging
import sys
import tempfile
from pathlib import Path

REPO = "/root/projects/h3studio-live"
sys.path.insert(0, "/home/ubuntu/comfyui")
sys.path.insert(0, REPO)

import torch  # noqa: E402
from comfy import lora as comfy_lora  # noqa: E402
from comfy import lora_convert  # noqa: E402
from comfy import utils  # noqa: E402

from h3studio import loras as lora_registry  # noqa: E402
from h3studio import moss_lora_split as split  # noqa: E402

MOSS_BASE = Path("/mnt/nvme1t_colorful/comfyui-models/MOSS-Image-Base/transformer")
_index = json.loads((MOSS_BASE / "diffusion_pytorch_model.safetensors.index.json").read_text())
SDK = ["diffusion_model." + k for k in _index["weight_map"] if k.endswith(".weight")]


class FakeModel:
    def __init__(self, sdk):
        self._sdk = sdk
        self.model_config = type("C", (), {"unet_config": {"dim": 3072, "num_layers": 42}})()

    def state_dict(self):
        return {k: torch.zeros(1) for k in self._sdk}


KEY_MAP = comfy_lora.model_lora_keys_unet(FakeModel(SDK), {})


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.missing = []

    def emit(self, record):
        msg = record.getMessage()
        if "lora key not loaded" in msg:
            self.missing.append(msg.split("lora key not loaded:")[-1].strip())


def probe(path):
    raw = utils.load_torch_file(str(path), safe_load=True)
    lora = lora_convert.convert_lora(raw)
    cap = _Cap()
    root = logging.getLogger()
    root.addHandler(cap)
    prev = root.level
    root.setLevel(logging.WARNING)
    try:
        loaded = comfy_lora.load_lora(lora, KEY_MAP, log_missing=True)
    finally:
        root.removeHandler(cap)
        root.setLevel(prev)
    return {"patched": len(loaded), "missing": len(cap.missing),
            "missing_proj_qkv": sum(1 for m in cap.missing if ".proj.qkv." in m),
            "missing_sample": cap.missing[:3]}


def main():
    out_path = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/e2e_d1.json")
    D1 = "moss-effects/moss_image_sft_look_lora.safetensors"
    report = {"d1_name": D1}

    resolved, path = lora_registry.resolve_lora(D1)
    report["resolved_name"] = resolved
    report["original_path"] = str(path) if path else None
    if not path:
        print("本机没有该 LoRA，无法验证")
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        return

    # ---- 第 1 步：走服务端真实代码路径（ensure_normalized）----
    norm = split.ensure_normalized(path)
    report["ensure_normalized"] = {
        "path": str(norm.get("path")) if norm.get("path") else None,
        "derived": bool(norm.get("derived")),
        "reason": norm.get("reason", ""),
    }
    effective_rel, effective_abs = lora_registry.lora_name_for_path(norm["path"]), norm["path"]
    report["workflow_lora_name"] = effective_rel
    report["workflow_lora_name_is_original"] = (effective_rel == D1)

    # ---- 第 2 步：真实 ComfyUI 挂载「工作流实际会挂的那个文件」----
    report["attach_original"] = probe(path)
    report["attach_effective"] = probe(effective_abs)

    # ---- 第 3 步：判定 ----
    eff = report["attach_effective"]
    orig = report["attach_original"]
    report["verdict"] = {
        "user_path_fully_attaches": eff.get("patched", 0) > 0 and eff.get("missing", 0) == 0,
        "user_path_key_not_loaded_warnings": eff.get("missing", 0),
        "original_file_alone_would_silently_fail": (
            orig.get("patched", 0) > 0 and orig.get("missing", 0) > 0),
        "original_file_alone_missing": orig.get("missing", 0),
        "effective_patched": eff.get("patched", 0),
        "effective_missing": eff.get("missing", 0),
    }
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
