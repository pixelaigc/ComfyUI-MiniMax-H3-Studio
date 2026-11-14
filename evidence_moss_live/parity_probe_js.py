"""从**合并后的真实 web/app.js** 里抽 normalizeImgSize 逻辑跑前后端一致性。

比 parity_probe.py 的手抄版更可信：它直接 eval 线上要跑的 JS。

用法: python3 evidence_moss_live/parity_probe_js.py
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from h3studio import h3image as h3          # noqa: E402
from h3studio import workflows as wf         # noqa: E402

APP_JS = os.path.join(ROOT, "web", "app.js")


def extract_function(src, name):
    """抓出完整 JS 函数源码（含 function 头与参数表）。"""
    m = re.search(r"^function\s+%s\s*\(" % re.escape(name), src, re.M)
    if not m:
        raise RuntimeError("function %s not found" % name)
    i = src.index("{", m.start())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    raise RuntimeError("unbalanced braces for %s" % name)


def build_js_harness():
    src = open(APP_JS, encoding="utf-8").read()
    norm = extract_function(src, "normalizeImgSize")
    ctx_builder = extract_function(src, "imgCtx")
    # normalizeImgSize 依赖 imgCtx()；imgCtx 依赖 isH3ImageMode/isMossMode/isQwenMode
    # 和 state/OPTIONS。这里用最小替身，只保留「族判定 + 倍数 + 上限」。
    harness = """
const STATE = { mode: "__MODE__" };
const H3IMAGE_MODES = ["t2i_h3image", "i2i_h3image"];
const MOSS_MODES = ["t2i_moss"];
const QWEN_MODES = ["t2i_qwen", "i2i_qwen"];
function isH3ImageMode(mode) { return H3IMAGE_MODES.includes(mode || STATE.mode); }
function isMossMode(mode) { return MOSS_MODES.includes(mode || STATE.mode); }
function isQwenMode(mode) { return QWEN_MODES.includes(mode || STATE.mode); }
function isZImageMode(mode) { const m = mode || STATE.mode; return m === "t2i"; }
"""
    # imgCtx 的真实实现依赖很多常量，这里替换成一个参数化版本。
    harness += """
function imgCtx() {
  return {
    opts: { sizeMin: 16, sizeMax: 16384 },
    multiple: __MULT__,
    maxMp: __MAXMP__,
    minMp: __MINMP__,
  };
}
"""
    harness += norm + "\n"
    harness += """
const out = normalizeImgSize(__W__, __H__);
console.log(JSON.stringify([out[0], out[1]]));
"""
    # 包进 IIFE：node -e 顶层不允许 return
    return "(function(){\n" + harness + "})();"


def run_js(mode, mult, max_mp, min_mp, w, h):
    js = build_js_harness()
    js = (js.replace("__MODE__", mode)
            .replace("__MULT__", str(mult))
            .replace("__MAXMP__", str(max_mp))
            .replace("__MINMP__", "null" if min_mp is None else str(min_mp))
            .replace("__W__", str(w))
            .replace("__H__", str(h)))
    res = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError("node failed: %s" % res.stderr)
    out = json.loads(res.stdout.strip().splitlines()[-1])
    return (out[0], out[1])


FAMILIES = [
    ("MOSS", "t2i_moss", 16, 4.0, 0.25, lambda w, h: wf.normalize_moss_image_size(w, h)[:2]),
    ("Qwen", "t2i_qwen", 16, 4.2, None, lambda w, h: wf.normalize_qwen_image_size(w, h)[:2]),
    ("H3-Image", "t2i_h3image", 32, 16.0, 1.0, lambda w, h: h3.normalize_h3_image_size(w, h)[:2]),
    ("Z-Image", "t2i", 16, 3.0, None, lambda w, h: wf.normalize_z_image_size(w, h)[:2]),
]

SIZES = [
    (1024, 1024), (1920, 1080), (2559, 1401), (2560, 1440), (4096, 4096),
    (8192, 4096), (6144, 3456), (2736, 1536), (5760, 3240), (10000, 10000),
    (2048, 2048), (1536, 864),
]


def main():
    rows = []
    for fam, mode, mult, max_mp, min_mp, backend in FAMILIES:
        for w, h in SIZES:
            be = backend(w, h)
            try:
                fe = run_js(mode, mult, max_mp, min_mp, w, h)
            except Exception as exc:  # noqa: BLE001
                fe = ("ERR", str(exc))
            rows.append({
                "family": fam, "input": f"{w}x{h}",
                "backend": f"{be[0]}x{be[1]}",
                "frontend": f"{fe[0]}x{fe[1]}",
                "match": fe == tuple(be),
            })
    hdr = f"{'family':9} {'input':12} {'backend':13} {'frontend(merged js)':19} match"
    print(hdr)
    print("-" * len(hdr))
    bad = 0
    for r in rows:
        flag = "" if r["match"] else "  <-- MISMATCH"
        if not r["match"]:
            bad += 1
        print(f"{r['family']:9} {r['input']:12} {r['backend']:13} {r['frontend']:19} {r['match']}{flag}")
    print()
    print("TOTAL=%d  MISMATCH=%d" % (len(rows), bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
