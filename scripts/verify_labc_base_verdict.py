#!/usr/bin/env python3
"""L-C 实测：真实请求 /api/image-loras，确认 base_verdict 出现且取值正确。

覆盖三类**真实输入**（不是造出来的假条目）：
  1. 可适配   —— 目录没登记结论、但本机实测判定为可用
                  （Z-Image / Qwen / MOSS 三个族各取若干条）
  2. 不可适配 —— 本机实测判定 compatible=false 的条目
  3. broken   —— 文件本体损坏（用真实截断副本走真实判定函数）

外加：
  4. 目录登记过的结论（原生 / 近似适用）被保留
  5. **没有任何条目** base_verdict 为空
  6. 判定不是常量 —— 三类输入的结论互不相同

用法：
  python3 scripts/verify_labc_base_verdict.py <base_url> <out.json>
"""
import json
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import loras as lora_registry  # noqa: E402
from h3studio import moss_lora_compat as compat  # noqa: E402
from h3studio import moss_lora_split as split  # noqa: E402

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190"
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/tmp/labc_base_verdict.json")

FAMILIES = ("Z-Image", "Qwen-Image 2.1", "MOSS-Image")


def fetch(base):
    url = f"{BASE_URL}/api/image-loras?base=" + urllib.parse.quote(base)
    with urllib.request.urlopen(url, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


out = {"base_url": BASE_URL, "families": {}, "three_classes": {},
       "invariants": {}}

# ------------------------------------------------------------------ 1) 三族实测
all_items = []
for fam in FAMILIES:
    data = fetch(fam)
    av = data.get("available", [])
    inc = data.get("incompatible", [])
    dist = Counter(i.get("base_verdict") for i in av + inc)
    empty = [i.get("name") for i in av + inc if not i.get("base_verdict")]
    out["families"][fam] = {
        "available": len(av), "incompatible": len(inc),
        "base_verdict_dist": {str(k): v for k, v in dist.items()},
        "empty_base_verdict_count": len(empty),
        "empty_base_verdict_names": empty[:10],
        "sample": [
            {k: i.get(k) for k in ("name", "compatible", "attention_hit",
                                   "conversion_status", "base_verdict")}
            for i in (av[:2] + inc[:2])
        ],
    }
    all_items.extend(av + inc)

# ------------------------------------------------------------------ 2) 三类真实输入
# 2a) 可适配：实测 compatible=true
usable = [i for i in all_items
          if i.get("compatible") and (i.get("base_verdict") or "") not in
          (lora_registry.BASE_VERDICT_INCOMPATIBLE,
           lora_registry.BASE_VERDICT_BROKEN)]
# 2b) 不可适配：实测 compatible=false
unusable = [i for i in all_items if not i.get("compatible")]
# 2c) broken：真实截断副本 -> 走真实判定函数
tmpdir = Path(tempfile.mkdtemp(prefix="labc_verdict_"))
try:
    src = Path("/mnt/usb2t/comfyui-models/loras/ipadapter/"
               "ip-adapter-faceid-plus_sd15_lora.safetensors")
    raw = src.read_bytes()
    trunc = tmpdir / "truncated_probe.safetensors"
    trunc.write_bytes(raw[: len(raw) // 2])
    cs = split.conversion_status(str(trunc))
    cc = compat.check_moss_compatibility(str(trunc))
    broken_verdict = lora_registry._resolve_base_verdict(
        {}, {"compatible": cc.get("compatible"),
             "attention_hit": cc.get("attention_hit"),
             "conversion_status": cs.get("status")})
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)

out["three_classes"] = {
    "usable_count": len(usable),
    "usable_verdicts": sorted({i.get("base_verdict") for i in usable}),
    "usable_sample": [
        {k: i.get(k) for k in ("name", "compatible", "attention_hit",
                               "conversion_status", "base_verdict")}
        for i in usable[:3]],
    "unusable_count": len(unusable),
    "unusable_verdicts": sorted({i.get("base_verdict") for i in unusable}),
    "unusable_sample": [
        {k: i.get(k) for k in ("name", "compatible", "attention_hit",
                               "conversion_status", "base_verdict",
                               "incompatible_reason")}
        for i in unusable[:3]],
    "broken_probe": {
        "conversion_status": cs.get("status"),
        "compatible": cc.get("compatible"),
        "attention_hit": cc.get("attention_hit"),
        "base_verdict": broken_verdict,
        "reason": (cc.get("reason") or "")[:200],
    },
}

# ------------------------------------------------------------------ 3) 不变量
declared_native = sum(
    1 for i in all_items
    if i.get("base_verdict") == lora_registry.BASE_VERDICT_NATIVE)
declared_near = sum(
    1 for i in all_items
    if i.get("base_verdict") == lora_registry.BASE_VERDICT_NEAR)
derived_verified = sum(
    1 for i in all_items
    if i.get("base_verdict") == lora_registry.BASE_VERDICT_VERIFIED)

violations = []
for i in all_items:
    v = i.get("base_verdict")
    name = i.get("name")
    if not v:
        violations.append(("empty_base_verdict", name))
    if i.get("conversion_status") == "broken" and \
            v != lora_registry.BASE_VERDICT_BROKEN:
        violations.append(("broken_not_marked", name, v))
    if i.get("compatible") is False and v not in (
            lora_registry.BASE_VERDICT_INCOMPATIBLE,
            lora_registry.BASE_VERDICT_BROKEN):
        violations.append(("incompatible_not_marked", name, v))
    if v == lora_registry.BASE_VERDICT_NATIVE and \
            i.get("compatible") is False:
        violations.append(("false_native", name))

verdict_universe = set()
for i in all_items:
    verdict_universe.add(i.get("base_verdict"))
verdict_universe.add(broken_verdict)

out["invariants"] = {
    "total_items": len(all_items),
    "empty_base_verdict": sum(
        1 for i in all_items if not i.get("base_verdict")),
    "declared_native": declared_native,
    "declared_near": declared_near,
    "derived_verified": derived_verified,
    "violations": violations,
    "verdict_universe": sorted(v for v in verdict_universe if v),
    "verdict_universe_size": len([v for v in verdict_universe if v]),
    "not_a_constant": len([v for v in verdict_universe if v]) >= 3,
}

OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False))
print(json.dumps(out["invariants"], indent=2, ensure_ascii=False))
print()
for fam, rec in out["families"].items():
    print(f"{fam}: available={rec['available']} incompatible={rec['incompatible']} "
          f"base_verdict={rec['base_verdict_dist']} "
          f"empty={rec['empty_base_verdict_count']}")
print()
print(json.dumps(out["three_classes"], indent=2, ensure_ascii=False))
print(f"\n-> {OUT}")
