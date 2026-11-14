"""前后端尺寸口径一致性探针（合并冲突处置的实测依据）。

对比三组「前端 normalizeImgSize 逻辑」vs「真实后端归一化函数」的输出，
证明合并后哪些模式族会出现「界面显示尺寸 != 实际提交尺寸」。

用法: python3 evidence_moss_live/parity_probe.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from h3studio import h3image as h3          # noqa: E402
from h3studio import workflows as wf         # noqa: E402


def js_round(x: float) -> int:
    """JS Math.round 语义（.5 向上）。"""
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


def fe_normalize(w, h, mult, max_mp, min_mp, stepdown, is_zimage=False):
    """复刻合并后 web/app.js 的 normalizeImgSize()。

    stepdown=True  -> 102adf0 版（带「退一格」while 循环）
    stepdown=False -> HEAD 版（纯比例缩放）
    """
    nw = max(mult, js_round(w / mult) * mult)
    nh = max(mult, js_round(h / mult) * mult)
    if min_mp:
        min_px = min_mp * 1024 * 1024
        if nw * nh < min_px:
            s = (min_px / float(nw * nh)) ** 0.5
            nw = max(mult, js_round(nw * s / mult) * mult)
            nh = max(mult, js_round(nh * s / mult) * mult)
    if is_zimage:
        return nw, nh
    if max_mp:
        max_px = max_mp * 1024 * 1024
        if nw * nh > max_px:
            s = (max_px / float(nw * nh)) ** 0.5
            sw = max(mult, js_round(nw * s / mult) * mult)
            sh = max(mult, js_round(nh * s / mult) * mult)
            if stepdown and sw * sh > max_px:
                while sw * sh > max_px and sw > mult and sh > mult:
                    sw -= mult
                    sh -= mult
            nw, nh = sw, sh
    return nw, nh


# --- 真实后端函数 ---
def be_moss(w, h):
    return wf.normalize_moss_image_size(w, h)[:2]


def be_qwen(w, h):
    return wf.normalize_qwen_image_size(w, h)[:2]


def be_h3(w, h):
    return h3.normalize_h3_image_size(w, h)[:2]


SIZES = [
    (1024, 1024), (1920, 1080), (2559, 1401), (2560, 1440),
    (4096, 4096), (8192, 4096), (6144, 3456), (2736, 1536),
    (5760, 3240), (10000, 10000),
]

FAMILIES = [
    # name, mult, max_mp, min_mp, backend, stepdown_ok
    ("MOSS",     16, 4.0,  0.25, be_moss, True),
    ("Qwen",     16, 4.2,  None, be_qwen, True),
    ("H3-Image", 32, 16.0, 1.0,  be_h3,   False),
]


def run():
    rows = []
    for fam, mult, max_mp, min_mp, backend, stepdown_ok in FAMILIES:
        for w, h in SIZES:
            be = backend(w, h)
            fe_sd = fe_normalize(w, h, mult, max_mp, min_mp, True)
            fe_plain = fe_normalize(w, h, mult, max_mp, min_mp, False)
            rows.append({
                "family": fam,
                "input": f"{w}x{h}",
                "backend": f"{be[0]}x{be[1]}",
                "fe_stepdown": f"{fe_sd[0]}x{fe_sd[1]}",
                "fe_plain": f"{fe_plain[0]}x{fe_plain[1]}",
                "stepdown_matches": fe_sd == tuple(be),
                "plain_matches": fe_plain == tuple(be),
            })
    return rows


if __name__ == "__main__":
    rows = run()
    hdr = f"{'family':9} {'input':12} {'backend':13} {'fe_stepdown':13} {'fe_plain':13} sd== be plain== be"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        flag = "" if r["stepdown_matches"] else "  <-- MISMATCH"
        print(f"{r['family']:9} {r['input']:12} {r['backend']:13} "
              f"{r['fe_stepdown']:13} {r['fe_plain']:13} "
              f"{str(r['stepdown_matches']):7} {str(r['plain_matches'])}{flag}")
    print()
    for fam, *_rest, stepdown_ok in [(f[0], f[1], f[2], f[3], f[4], f[5]) for f in FAMILIES]:
        sub = [r for r in rows if r["family"] == fam]
        sd_bad = [r for r in sub if not r["stepdown_matches"]]
        pl_bad = [r for r in sub if not r["plain_matches"]]
        print(f"{fam:9} stepdown_variants={len(sub)} "
              f"stepdown_mismatch={len(sd_bad)} plain_mismatch={len(pl_bad)}")
