"""【B 实测】MOSS-Image LoRA 在**真实 ComfyUI** 里的挂载验证。

用 ComfyUI 的真实代码路径（comfy/lora.py::model_lora_keys_unet +
load_lora + comfy/lora_convert.convert_lora）判定每个 MOSS 系 LoRA：
  * patched > 0 且 missing == 0 -> ✅ 真的挂上了
  * patched == 0               -> ❌ 一个都挂不上（静默失效）
  * missing > 0                -> ⚠️ 只挂上一部分

并且对比「修复前（原文件）」与「修复后（归一化派生文件）」。

输出 /tmp/moss_attach_verified.json
"""
import glob
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, "/home/ubuntu/comfyui")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import torch  # noqa: E402
from comfy import lora as comfy_lora  # noqa: E402
from comfy import lora_convert  # noqa: E402
from comfy import utils  # noqa: E402

from h3studio import moss_lora_split as split  # noqa: E402

MOSS_BASE = Path("/mnt/nvme1t_colorful/comfyui-models/MOSS-Image-Base/transformer")
index = json.loads((MOSS_BASE / "diffusion_pytorch_model.safetensors.index.json").read_text())
sdk = ["diffusion_model." + k for k in index["weight_map"] if k.endswith(".weight")]


class FakeModel:
    def __init__(self, sdk):
        self._sdk = sdk
        self.model_config = type("C", (), {"unet_config": {"dim": 3072, "num_layers": 42}})()

    def state_dict(self):
        return {k: torch.zeros(1) for k in self._sdk}


KEY_MAP = comfy_lora.model_lora_keys_unet(FakeModel(sdk), {})


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
    dirs = ["/mnt/usb2t/comfyui-models/loras/moss",
            "/mnt/usb2t/comfyui-models/loras/moss-effects"]
    files = []
    for d in dirs:
        files += sorted(glob.glob(f"{d}/*.safetensors"))
    files += sorted(glob.glob(
        "/home/ubuntu/comfyui/models/_lora_source_peft/**/*.safetensors", recursive=True))
    print(f"待测 {len(files)} 个 MOSS 系 LoRA", flush=True)

    tmpdir = tempfile.mkdtemp(prefix="moss-verify-")
    out = Path("/tmp/moss_attach_verified.json")
    report = []
    t0 = time.time()
    for i, f in enumerate(files):
        row = {"name": f, "file": os.path.basename(f)}
        try:
            row["before"] = probe(f)
        except Exception as exc:  # noqa: BLE001
            row["before"] = {"error": f"{type(exc).__name__}: {exc}"[:150]}
        try:
            r = split.normalize_lora(f, target_dir=Path(tmpdir))
            row["convert_ok"] = r["ok"]
            row["convert_changed"] = r.get("changed", False)
            row["convert_reason"] = r.get("reason", "")[:150]
            if r["ok"] and r.get("changed"):
                row["after"] = probe(r["target"])
            else:
                row["after"] = row["before"]
                row["used_original"] = True
        except Exception as exc:  # noqa: BLE001
            row["after"] = {"error": f"{type(exc).__name__}: {exc}"[:150]}

        b = row.get("before", {})
        a = row.get("after", {})
        if "error" in a:
            row["verdict"] = "error"
        elif a.get("patched", 0) > 0 and a.get("missing", 0) == 0:
            row["verdict"] = "attached"
        elif a.get("patched", 0) > 0:
            row["verdict"] = "partial"
        else:
            row["verdict"] = "not_attached"
        report.append(row)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        if (i + 1) % 10 == 0:
            ok = sum(1 for x in report if x["verdict"] == "attached")
            print(f"  .. {i+1}/{len(files)} ({time.time()-t0:.0f}s) attached={ok}",
                  flush=True)

    import collections
    counts = collections.Counter(x["verdict"] for x in report)
    print(f"\n总计 {len(report)}  用时 {time.time()-t0:.0f}s")
    print("判定分布:", dict(counts))
    fixed = [x for x in report if x.get("convert_changed")
             and x["verdict"] == "attached"
             and x["before"].get("missing", 0) > 0]
    print(f"\n被归一化**救活**的条目: {len(fixed)}")
    for x in fixed[:12]:
        print(f"  {x['file'][:56]:58} before m={x['before'].get('missing')} "
              f"-> after p={x['after'].get('patched')} m={x['after'].get('missing')}")
    still = [x for x in report if x["verdict"] in ("not_attached", "partial")]
    print(f"\n仍不完整的条目: {len(still)}")
    for x in still:
        print(f"  [{x['verdict']:13}] {x['file'][:52]:54} "
              f"p={x['after'].get('patched')} m={x['after'].get('missing')} "
              f"qkv={x['after'].get('missing_proj_qkv')}")
        print(f"        {x.get('convert_reason','')[:110]}")
    print(f"\n结果已存 {out}")


if __name__ == "__main__":
    main()
