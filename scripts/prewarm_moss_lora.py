#!/usr/bin/env python3
"""MOSS-Image LoRA 归一化派生文件**预热**（缺陷 A 收口的必要一步）。

【为什么需要这个脚本】
`check_moss_compatibility` 现在要求融合 qkv 形态的 LoRA **转换已验证**
才发 compatible=true —— 因为「结构兼容」不等于「用户能用」：
融合 qkv 直接挂上去会被 ComfyUI 静默丢弃（半静默失效）。

「转换已验证」的证据是**派生文件确实存在且完整**。
本脚本把本机所有融合形态的 MOSS LoRA 批量转换出派生文件，
让这些条目从「未验证」变成「已验证」，从而可以安全地以 compatible=true 下发。

【代价】
每个文件读全量权重 + 写出等大的派生文件。本机实测 36 条合计 6.64 GB，
USB 盘上约 10-20 分钟。**中断安全**：已生成的会被下次复用（reused）。

【用法】
python3 scripts/prewarm_moss_lora.py [--out <dir>] [--only <name 子串>]

输出：<out>/prewarm_moss_lora.json（逐条结果）+ stdout 进度
"""
import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import loras as lora_registry           # noqa: E402
from h3studio import moss_lora_split as split          # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/prewarm-moss")
    ap.add_argument("--only", default="", help="只处理名字含该子串的条目")
    ap.add_argument("--repo", default=str(REPO))
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ⚠️ **不能只扫 list_moss_effect_loras()**：
    # 那张表只含 compatible=true 的条目，而「融合形态 + 转换未验证」的条目
    # 现在会被判成 compatible=false -> 已经不在表里了 ->
    # 预热脚本就永远找不到它们 -> 永远预热不出来（循环依赖）。
    #
    # 所以这里用 `list_moss_image_loras()`（**不过滤 compatible** 的全量清单），
    # 与 compatible 判定解耦。派生文件（h3studio_mossnorm__）已被清单层过滤掉。
    items = lora_registry.list_moss_image_loras()
    targets = []
    skipped = []
    for item in items:
        name = item.get("name") or ""
        rel, path = lora_registry.resolve_lora(name)
        if not path:
            skipped.append({"name": name, "reason": "本机找不到文件"})
            continue
        if args.only and args.only not in name:
            continue
        status = split.conversion_status(path)
        if status["status"] == split.STATUS_DIRECT:
            skipped.append({"name": name, "reason": "分离形态，无需转换"})
            continue
        if status["status"] == split.STATUS_DERIVED:
            skipped.append({"name": name, "reason": "派生文件已存在且完整"})
            continue
        if status["status"] == split.STATUS_BROKEN:
            skipped.append({"name": name, "reason": "文件本体损坏，无法转换"})
            continue
        targets.append(str(path))

    print(f"待转换 {len(targets)} 个；跳过 {len(skipped)} 个（无需转换/已存在）",
          flush=True)
    if not targets:
        print("没有需要转换的条目。")
        return 0

    t0 = time.time()

    def progress(done, total, path, ok, reason):
        el = time.time() - t0
        mark = "OK  " if ok else "FAIL"
        print(f"[{done}/{total}] {mark} {Path(path).name[:58]:60} "
              f"{el:.0f}s {reason[:60]}", flush=True)

    result = split.prewarm_derived(targets, progress=progress)
    result["skipped"] = skipped
    result["elapsed_sec"] = round(time.time() - t0, 1)
    result["targets"] = targets

    out_file = out_dir / "prewarm_moss_lora.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n转换 {result['converted']} / 复用 {result['reused']} / "
          f"失败 {result['failed']}，用时 {result['elapsed_sec']}s")
    print(f"结果已存 {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
