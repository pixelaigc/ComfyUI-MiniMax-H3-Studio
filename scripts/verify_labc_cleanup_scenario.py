#!/usr/bin/env python3
"""L-B 实测：真实磁盘上的「过期派生文件」场景（前后文件清单证据）。

场景（在**真实机器**的 LoRA 目录里）：
  A. 有效派生文件（源文件在、mtime 不更新）-> 必须**保留**
  B. 过期派生文件（源文件比派生文件新）    -> 必须**被清理**
  C. 孤儿派生文件（源文件已删）            -> 必须**被清理**

触发方式：**真实的派生文件生成路径**（ensure_normalized），
不是直接调 cleanup 函数 —— 这样才证明「调用点真的接上了」。

为什么探针文件放在真实目录里：
  `ensure_normalized` 内部调 `cleanup_stale_derived()` 时**不传目录**
  -> 它清的是**真实机器**的 LoRA 目录（comfyui_lora_dirs()）。
  所以「触发路径真的会清理」只能在真实目录里验证。
  探针文件放在专属子目录 `_labc_probe/`，跑完即删。

输出：/tmp/labc_cleanup_scenario.json（前后清单 + 判定）
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import moss_lora_split as split  # noqa: E402

LORAS = Path("/mnt/usb2t/comfyui-models/loras")
PROBE_DIR = LORAS / "_labc_probe"


def snapshot(root: Path) -> dict:
    """列出 root 下所有派生文件（含 mtime）。"""
    out = {}
    for p in sorted(root.rglob(f"{split.DERIVED_PREFIX}*")):
        if p.is_file():
            out[str(p.relative_to(root))] = {
                "size_bytes": p.stat().st_size,
                "mtime": p.stat().st_mtime,
            }
    return out


def main():
    result = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    # 真实机器上的有效派生文件清单（触发前后对比）
    real_before = snapshot(LORAS)
    result["real_before_count"] = len(real_before)
    result["real_before_list"] = sorted(real_before)

    # --- 造探针 ---
    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        # A. 有效派生（源文件在、mtime 相同）
        a_src = PROBE_DIR / "valid_probe.safetensors"
        a_der = PROBE_DIR / split.derived_name("valid_probe.safetensors")
        a_src.write_bytes(b"S" * 64)
        a_der.write_bytes(b"D" * 64)
        t_same = time.time() - 3600
        os.utime(a_src, (t_same, t_same))
        os.utime(a_der, (t_same, t_same))

        # B. 过期派生（源文件比派生文件新）
        b_src = PROBE_DIR / "stale_probe.safetensors"
        b_der = PROBE_DIR / split.derived_name("stale_probe.safetensors")
        b_src.write_bytes(b"S" * 64)
        b_der.write_bytes(b"D" * 64)
        t_old = time.time() - 7200
        t_new = time.time() - 60
        os.utime(b_der, (t_old, t_old))
        os.utime(b_src, (t_new, t_new))

        # C. 孤儿派生（源文件不存在）
        c_der = PROBE_DIR / split.derived_name("orphan_probe.safetensors")
        c_der.write_bytes(b"D" * 64)

        result["probe_before"] = {
            "valid_probe_exists": a_der.is_file(),
            "stale_probe_exists": b_der.is_file(),
            "orphan_probe_exists": c_der.is_file(),
            "real_derived_count": len(snapshot(LORAS)),
        }

        # --- 触发：真实的派生文件生成路径 ---
        from safetensors.torch import save_file
        import torch
        # 用真实的 MOSS 形态键（lora_unet_ 前缀 + layers.N.proj.*），
        # 这样 normalize_lora 能成功 -> 走到 cleanup 调用点。
        tensors = {}
        for layer in range(2):
            for proj in ("q", "k", "v", "o"):
                tensors[f"lora_unet_layers_{layer}_proj_{proj}.alpha"] = \
                    torch.zeros(())
                tensors[f"lora_unet_layers_{layer}_proj_{proj}.lora_A.weight"] = \
                    torch.zeros(4, 4)
                tensors[f"lora_unet_layers_{layer}_proj_{proj}.lora_B.weight"] = \
                    torch.zeros(4, 4)
        trig_src = PROBE_DIR / "trigger_probe.safetensors"
        save_file(tensors, str(trig_src), metadata={"format": "pt"})
        os.utime(trig_src, (time.time() - 3600, time.time() - 3600))

        r = split.ensure_normalized(trig_src)
        result["trigger"] = {
            "fn": "ensure_normalized",
            "result": {k: str(v) for k, v in r.items()},
            "note": "走到这一步 == ensure_normalized 内部的 cleanup 调用点生效",
        }

        # --- 触发后 ---
        result["probe_after"] = {
            "valid_probe_exists": a_der.is_file(),
            "stale_probe_exists": b_der.is_file(),
            "orphan_probe_exists": c_der.is_file(),
            "real_derived_count": len(snapshot(LORAS)),
        }
    finally:
        shutil.rmtree(PROBE_DIR, ignore_errors=True)

    real_after = snapshot(LORAS)
    result["real_after_count"] = len(real_after)
    result["real_after_list"] = sorted(real_after)
    result["real_lost"] = sorted(set(real_before) - set(real_after))

    result["verdict"] = {
        "valid_derived_kept": result["probe_after"]["valid_probe_exists"],
        "stale_derived_removed": not result["probe_after"]["stale_probe_exists"],
        "orphan_derived_removed": not result["probe_after"]["orphan_probe_exists"],
        "real_derived_kept": not result["real_lost"],
        "real_before_count": result["real_before_count"],
        "real_after_count": result["real_after_count"],
    }
    result["verdict"]["all_pass"] = all([
        result["verdict"]["valid_derived_kept"],
        result["verdict"]["stale_derived_removed"],
        result["verdict"]["orphan_derived_removed"],
        result["verdict"]["real_derived_kept"],
    ])

    out = Path("/tmp/labc_cleanup_scenario.json")
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(json.dumps({k: v for k, v in result.items()
                      if k not in ("real_before_list", "real_after_list")},
                     indent=2, ensure_ascii=False))
    print(f"\n-> {out}")
    return 0 if result["verdict"]["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
