#!/usr/bin/env python3
"""
基线 vs 修复 —— 对**同一批真实模型产物**的判定对比（可复现脚本）。

用途：证明「这次修复真的改变了判定结果」，而不是「本来就过」。
做法：
  1. 用**基线 worktree**（未修复）的 enhancer + validate 判定
  2. 用**修复 worktree**的 enhancer + validate 判定
  3. 逐项对比，输出 JSON + 表格

用法：
    python3 tests/compare_baseline_vs_fix.py \
        --baseline /root/projects/h3studio-baseline-echo \
        --fixed    /root/projects/h3studio-echo-fix \
        --samples  /tmp/collect_*.txt
"""
import argparse
import glob
import importlib.util
import json
import os
import subprocess
import sys


def _run_one(root, spec_id, path, original):
    code = (
        "import json,sys;"
        "sys.path.insert(0, %r);"
        "from h3studio import enhancer as enh;"
        "spec = enh.get_spec(%r);"
        "raw = open(%r, encoding='utf-8').read();"
        "parsed = enh.strip_wrapping(enh.strip_thinking(raw));"
        "sp = spec['system_prompt'];"
        "echo = bool(enh.looks_like_echo(parsed, sp));"
        "v = enh.validate_output(parsed, %r, spec.get('validate_mode', 'pe'));"
        "print(json.dumps({'parsed_len': len(parsed), 'echo': echo,"
        " 'valid_ok': v.get('ok'), 'errors': v.get('errors', []),"
        " 'warnings': v.get('warnings', [])}, ensure_ascii=False))"
        % (root, spec_id, path, original)
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=180)
    if out.returncode != 0:
        return {"error": out.stderr.strip()[-400:]}
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        return {"error": "unparsable: " + out.stdout[-300:]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--fixed", required=True)
    ap.add_argument("--samples", nargs="+", required=True)
    ap.add_argument("--original", default="电影。港风。90年代香港港口，亚洲年轻女性休闲散步")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    paths = []
    for pat in args.samples:
        paths.extend(sorted(glob.glob(pat)))
    if not paths:
        print("no sample files matched")
        return 1

    rows = []
    print("%-18s | %-34s | %s" % ("样本", "基线（未修复）", "修复后"))
    print("-" * 110)
    for path in paths:
        name = os.path.basename(path).replace("collect_", "").replace(".txt", "")
        base = _run_one(args.baseline, name, path, args.original)
        fixed = _run_one(args.fixed, name, path, args.original)
        rows.append({"sample": name, "path": path,
                     "baseline": base, "fixed": fixed})
        b = ("echo=%s valid=%s" % (base.get("echo"), base.get("valid_ok"))
             if "error" not in base else "ERR " + base["error"][:40])
        f = ("echo=%s valid=%s" % (fixed.get("echo"), fixed.get("valid_ok"))
             if "error" not in fixed else "ERR " + fixed["error"][:40])
        print("%-18s | %-34s | %s" % (name, b, f))
        if "error" not in base and base.get("errors"):
            print("   基线 errors: %s" % base["errors"])
        if "error" not in fixed and fixed.get("errors"):
            print("   修复后 errors: %s" % fixed["errors"])

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)
        print("\nwrote %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
