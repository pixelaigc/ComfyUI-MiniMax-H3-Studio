# -*- coding: utf-8 -*-
"""【D1 真实链路复现】同源分支接续 —— 潜空间不互相覆盖、读回自己上游。

复现验收报告里的缺陷路径（真实 ComfyUI 生成，非模拟）：
    1. 生成段 A（链首段）
    2. 对 A 点「接续」两次 -> B1 / B2（两个兄弟分支）
    3. 检查：两份潜空间文件**同时存在**且**内容不同**
    4. 再对 B1 接续 -> C1
    5. 检查：C1 的首帧来自 **B1 末帧**，不是 B2 末帧
       （灰度 MAE：与 B1 的差应远小于与 B2 的差）

用法：
    python3 tests/verify_branch_continuation.py [--base http://127.0.0.1:8199]

产物：
    /tmp/h3-branch-frames/*.png   三联对照帧
    stdout                       全部量化数值
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8199"
DATA = Path("/tmp/h3t2v-branch-data")
OUT = DATA / "outputs"
FRAME_DIR = Path("/tmp/h3-branch-frames")

BP = {"model_key": "fl2va_int8", "width": 768, "height": 448, "duration": 5,
      "steps": 4, "sampler": "euler", "scheduler": "simple", "seed": 4242}


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=90) as r:
        return json.loads(r.read().decode())


def post(fields):
    b = "----branch9911"
    body = b""
    for k, v in fields.items():
        body += f"--{b}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        body += str(v).encode("utf-8") + b"\r\n"
    body += f"--{b}--\r\n".encode()
    req = urllib.request.Request(BASE + "/api/jobs", data=body,
                                 headers={"Content-Type":
                                          f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())


def wait(jid, t=1200):
    dl = time.time() + t
    while time.time() < dl:
        j = get(f"/api/jobs/{jid}")
        if j.get("status") in ("done", "failed", "canceled"):
            return j
        time.sleep(3)
    raise TimeoutError(jid)


def seg_of(job):
    """取该 job 的**分段**视频（不是 is_full 拼接版）。"""
    for o in job.get("outputs", []):
        if o.get("kind") == "video" and not o.get("is_full"):
            return OUT / o["file"]
    return None


def full_of(job):
    for o in job.get("outputs", []):
        if o.get("kind") == "video" and o.get("is_full"):
            return OUT / o["file"]
    return None


def frames_of(path):
    raw = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
        text=True).strip()
    return int(raw)


def grab(path, which):
    """抽首帧 / 末帧为 png。"""
    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    total = frames_of(path)
    stem = FRAME_DIR / path.stem
    if which == "first":
        vf = "select='eq(n\\,0)'"
        out = f"{stem}-first.png"
    else:
        vf = f"select='eq(n\\,{total-1})'"
        out = f"{stem}-last.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(path),
                    "-vf", vf, "-vsync", "0", "-frames:v", "1", out],
                   check=False)
    return Path(out)


def mae(p1, p2):
    """两张图的平均绝对差（灰度）。"""
    from PIL import Image
    import numpy as np
    a = np.asarray(Image.open(p1).convert("L"), dtype=float)
    b = np.asarray(Image.open(p2).convert("L"), dtype=float)
    if a.shape != b.shape:
        h = min(a.shape[0], b.shape[1] * 0 + b.shape[0])
        w = min(a.shape[1], b.shape[1])
        a, b = a[:h, :w], b[:h, :w]
    return float(np.abs(a - b).mean())


def latent_path(job_id):
    """该任务自己那一份潜空间文件（服务端记录的权威坐标）。"""
    with urllib.request.urlopen(BASE + "/api/jobs/" + job_id, timeout=60) as r:
        job = json.loads(r.read().decode())
    info = job.get("continuation_info") or {}
    rid = info.get("run_id") or ""
    stage = info.get("latent_stage", 0)
    if not rid:
        return None, info
    import os
    base = os.environ.get("H3STUDIO_COMFY_OUTPUT") or "/home/ubuntu/comfyui/output"
    return Path(base) / "relay_kit" / rid / f"stage_{int(stage):05d}.safetensors", info


def need_latent(path, label):
    """断言潜空间文件存在，并返回它。"""
    assert path is not None, f"{label} 没有潜空间坐标"
    assert path.is_file(), f"{label} 潜空间文件缺失: {path}"
    return path


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()
    BASE = args.base

    report = {"steps": [], "ok": True}

    def step(name, data):
        report["steps"].append({"step": name, **data})
        print(f"--- {name}")
        for k, v in data.items():
            print(f"    {k}: {v}")

    print("=" * 72)
    print("D1 真实链路复现：同源分支接续")
    print("=" * 72)

    # ---- 1. 段 A（链首段）----
    a = dict(BP); a["prompt"] = "a swordsman in a bamboo forest, calm, wide shot"
    ja = post({"mode": "t2v", "params": json.dumps(a)})["id"]
    print(f"A = {ja}")
    ra = wait(ja)
    print(f"  A status={ra['status']} err={ra.get('error')}")
    if ra["status"] != "done":
        sys.exit("A 生成失败")
    pa = seg_of(ra)
    print(f"  A 段视频: {pa}  frames={frames_of(pa)}")

    ctx_a = get(f"/api/jobs/{ja}/continuation")
    assert ctx_a.get("ok"), ctx_a
    latent_a, info_a = latent_path(ja)
    latent_a = need_latent(latent_a, "A")
    step("A", {"job": ja, "seg_frames": frames_of(pa),
                "latent": str(latent_a), "latent_exists": latent_a.is_file(),
                "latent_bytes": latent_a.stat().st_size})

    # ---- 2. 从 A 接续两次 -> B1 / B2 ----
    b1 = dict(BP); b1["prompt"] = "b1 the swordsman raises his blade, camera pushes in"
    b1["continuation"] = ctx_a["continuation"]
    jb1 = post({"mode": "t2v", "params": json.dumps(b1)})["id"]
    print(f"B1 = {jb1}")
    rb1 = wait(jb1)
    print(f"  B1 status={rb1['status']} err={rb1.get('error')}")

    b2 = dict(BP); b2["prompt"] = "b2 a red lantern festival at night, crowd celebrating"
    b2["continuation"] = ctx_a["continuation"]
    jb2 = post({"mode": "t2v", "params": json.dumps(b2)})["id"]
    print(f"B2 = {jb2}")
    rb2 = wait(jb2)
    print(f"  B2 status={rb2['status']} err={rb2.get('error')}")

    pb1, pb2 = seg_of(rb1), seg_of(rb2)
    latent_b1, _ = latent_path(jb1)
    latent_b2, _ = latent_path(jb2)
    latent_b1 = need_latent(latent_b1, "B1")
    latent_b2 = need_latent(latent_b2, "B2")
    print(f"  B1 潜空间: {latent_b1}  exists={latent_b1.is_file()}")
    print(f"  B2 潜空间: {latent_b2}  exists={latent_b2.is_file()}")

    # 【关键断言 1】两份文件同时存在、路径不同、内容不同
    assert latent_b1 != latent_b2, "B1/B2 潜空间路径相同 —— 落盘坐标冲突！"
    b1_bytes = latent_b1.read_bytes()
    b2_bytes = latent_b2.read_bytes()
    assert b1_bytes != b2_bytes, "B1/B2 潜空间内容相同 —— 写的是同一个东西！"
    step("B1_B2", {
        "b1_latent": str(latent_b1), "b2_latent": str(latent_b2),
        "b1_bytes": len(b1_bytes), "b2_bytes": len(b2_bytes),
        "both_exist": True, "contents_differ": True,
        "b1_frames": frames_of(pb1), "b2_frames": frames_of(pb2),
        "b1_full": str(full_of(rb1)), "b2_full": str(full_of(rb2)),
    })

    # ---- 3. 对 B1 再接续 -> C1 ----
    ctx_b1 = get(f"/api/jobs/{jb1}/continuation")
    assert ctx_b1.get("ok"), ctx_b1
    c1 = dict(BP); c1["prompt"] = "c1 continue the bamboo forest, the swordsman turns around"
    c1["continuation"] = ctx_b1["continuation"]
    jc1 = post({"mode": "t2v", "params": json.dumps(c1)})["id"]
    print(f"C1 = {jc1}")
    rc1 = wait(jc1)
    print(f"  C1 status={rc1['status']} err={rc1.get('error')}")
    if rc1["status"] != "done":
        sys.exit("C1 生成失败")
    pc1 = seg_of(rc1)

    # ---- 4. 画面级判定：C1 首帧来自 B1 还是 B2 ----
    last_b1 = grab(pb1, "last")
    last_b2 = grab(pb2, "last")
    first_c1 = grab(pc1, "first")
    d_b1 = mae(first_c1, last_b1)
    d_b2 = mae(first_c1, last_b2)
    verdict = "B1(正确)" if d_b1 < d_b2 else "B2(错误!)"
    print()
    print(f"    C1首帧 vs B1末帧 MAE = {d_b1:.2f}")
    print(f"    C1首帧 vs B2末帧 MAE = {d_b2:.2f}")
    print(f"    -> C1 接的是 {verdict}")

    latent_c1, _ = latent_path(jc1)
    full_c1 = full_of(rc1)
    step("C1", {
        "job": jc1, "seg_frames": frames_of(pc1),
        "latent": str(latent_c1),
        "latent_exists": bool(latent_c1 and latent_c1.is_file()),
        "full": str(full_c1),
        "full_frames": frames_of(full_c1) if full_c1 else None,
        "mae_vs_B1_last": round(d_b1, 3),
        "mae_vs_B2_last": round(d_b2, 3),
        "verdict": verdict,
        "frames_b1": str(last_b1), "frames_b2": str(last_b2),
        "frames_c1": str(first_c1),
    })

    # ---- 断言 ----
    assert d_b1 < d_b2, f"C1 接的是 B2 而不是 B1！MAE B1={d_b1:.2f} B2={d_b2:.2f}"
    ratio = d_b2 / d_b1 if d_b1 > 0 else float("inf")
    print(f"\n✅ C1 正确接续自 B1（与 B2 的偏离倍数 = {ratio:.1f}x）")

    # 完整视频帧数账：A(124) + B1(102) + C1(102) = 328
    if full_c1:
        ff = frames_of(full_c1)
        print(f"    完整视频帧数 = {ff}（帧数账 124 + 102 + 102 = 328）")
        assert ff == 328, f"完整视频帧数 {ff} 与帧数账 328 不符"

    report["result"] = {"ok": True, "verdict": verdict,
                         "mae_b1": d_b1, "mae_b2": d_b2, "ratio": ratio}
    Path("/tmp/h3-branch-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2))
    print("\n报告: /tmp/h3-branch-report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
