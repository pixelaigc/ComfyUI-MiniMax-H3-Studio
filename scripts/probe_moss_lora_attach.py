"""【B 复验】MOSS-Image LoRA 在**真实 ComfyUI** 里的挂载验证（单文件快探）。

与 scripts/verify_moss_lora_attach.py 同源同路径（ComfyUI 真实
`comfy.lora.model_lora_keys_unet` + `load_lora` + `comfy.lora_convert.convert_lora`），
但只针对**指定文件**，不扫全量 —— 用于「改一处 → 验一处」的快速回归。

用法：
    /home/ubuntu/comfyui/venv/bin/python3 scripts/probe_moss_lora_attach.py <file> [<file>...]

输出：每个文件一行 JSON（patched / missing / missing_proj_qkv / verdict）。
verdict 判定：
    attached     = patched > 0 且 missing == 0（key-not-loaded 警告为 0）
    partial      = patched > 0 但 missing > 0（只挂上一部分 —— 半静默失效）
    not_attached = patched == 0（一个都挂不上）
    error        = 文件本体读不出（损坏 / 截断）
"""
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, "/home/ubuntu/comfyui")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import torch  # noqa: E402
from comfy import lora as comfy_lora  # noqa: E402
from comfy import lora_convert  # noqa: E402
from comfy import utils  # noqa: E402

MOSS_BASE = Path("/mnt/nvme1t_colorful/comfyui-models/MOSS-Image-Base/transformer")
_index = json.loads((MOSS_BASE / "diffusion_pytorch_model.safetensors.index.json").read_text())
_sdk = ["diffusion_model." + k for k in _index["weight_map"] if k.endswith(".weight")]


class FakeModel:
    def __init__(self, sdk):
        self._sdk = sdk
        self.model_config = type("C", (), {"unet_config": {"dim": 3072, "num_layers": 42}})()

    def state_dict(self):
        return {k: torch.zeros(1) for k in self._sdk}


KEY_MAP = comfy_lora.model_lora_keys_unet(FakeModel(_sdk), {})


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
    missing = cap.missing
    return {
        "patched": len(loaded),
        "missing": len(missing),
        "missing_proj_qkv": sum(1 for m in missing if ".proj.qkv." in m),
        "missing_sample": missing[:3],
    }


def main():
    files = sys.argv[1:]
    if not files:
        print(__doc__)
        return 1
    out = []
    for f in files:
        row = {"file": os.path.basename(f), "path": f}
        try:
            row["probe"] = probe(f)
            p = row["probe"]
            if p["patched"] > 0 and p["missing"] == 0:
                row["verdict"] = "attached"
            elif p["patched"] > 0:
                row["verdict"] = "partial"
            else:
                row["verdict"] = "not_attached"
        except Exception as exc:  # noqa: BLE001
            row["probe"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
            row["verdict"] = "error"
        out.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    Path(sys.argv[1]).parent  # noqa: B018
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
