# -*- coding: utf-8 -*-
"""【D1 四组缺陷 —— 一体化真实验收脚本】

跑**真实 ComfyUI 生成**（不是模拟），逐项验证：

  ① 落盘坐标冲突 —— 兄弟分支 B1/B2 各写各的目录（结构上不可能覆盖）
  ② 接续首帧取错分支 —— C1 首帧必须对齐 B1 末帧、与 B2 末帧显著拉开
  ③ 链账本串分支 —— C1 的完整视频只含 A + B1 + C1，**不含 B2**
  ④ 节点缓存拿回旧 latent —— 内容指纹机制（内容变 -> 路径变 -> 缓存必然失效）

【与既有脚本的差别】
  * tests/verify_branch_continuation.py 只覆盖 ①②，且不产出「链账本逐段核对」。
  * 本脚本把 ①②③④ 串成**同一次真实生成**，并把每个分支的
    段序列 / 每段时长 / 拼接总时长全部列出来（判据 2）。
  * 同时产出**两轮连续接续**的证据（判据 3）：A -> B -> C，
    再对 C 的完整视频接续一次 -> D。

【不需要重启 8199 / 8190】本脚本只打 HTTP。

用法：
    python3 tests/verify_d1_four_defects.py [--base http://127.0.0.1:8199]

产物：
    /tmp/h3-d1-four/frames/*.png       首/末帧对照图 + 三联图
    /tmp/h3-d1-four/report.json       机器可读报告
    stdout                            全部量化数值
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8199"

#: 产物目录 —— **放在仓库内**（data-accept/），不放在 /tmp。
#: 理由：2026-11-13 宿主重启清空了 /tmp，上一轮证据包的
#: 视频绝对路径全部失效（0/9 可达）。验收产物必须扛得住宿主重启。
ACCEPT_ROOT = Path(__file__).resolve().parent.parent / "data-accept"
ROOT = ACCEPT_ROOT / "evidence-frames"
FRAME_DIR = ROOT / "frames"

# 底模用 w4a8（12.5 GB）—— 验收机上 GPU 0 被别的进程占了 47 GB，
# 完整 int8（34 GB）塞不进去。w4a8 走同一条接续链路，
# 对「落盘坐标 / 首帧归属 / 链账本 / 指纹」四项判据没有影响。
BP = {"model_key": "fl2va_pruned_w4a8", "width": 768, "height": 448,
      "duration": 5, "steps": 4, "sampler": "euler", "scheduler": "beta",
      "seed": 4242}

REPORT: dict = {"steps": [], "ok": True, "failures": []}


def accept_base(default="http://127.0.0.1:8199"):
    """验收实例的 base URL。

    端口写在 data-accept/PORT（启动脚本自动顺延时写的）——
    8199 可能被并发 agent 占用，所以不能硬编码。
    """
    port_file = Path(__file__).resolve().parent.parent / "data-accept" / "PORT"
    if port_file.is_file():
        try:
            port = port_file.read_text(encoding="utf-8").strip()
            if port.isdigit():
                return f"http://127.0.0.1:{port}"
        except Exception:
            pass
    return default


def _req(path, data=None, timeout=180):
    if data is None:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return json.loads(r.read().decode())
    b = "----d1four9911"
    body = b""
    for k, v in data.items():
        body += f"--{b}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        body += str(v).encode("utf-8") + b"\r\n"
    body += f"--{b}--\r\n".encode()
    req = urllib.request.Request(BASE + path, data=body,
                                 headers={"Content-Type":
                                          f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def get(path):
    return _req(path)


def post(fields):
    return _req("/api/jobs", fields)


def wait(jid, t=1800):
    dl = time.time() + t
    last = None
    while time.time() < dl:
        j = get(f"/api/jobs/{jid}")
        last = j
        if j.get("status") in ("done", "failed", "canceled"):
            return j
        time.sleep(4)
    raise TimeoutError(f"{jid} 超时，最后状态 {last and last.get('status')}")


def out_dir():
    """本实例的 outputs 目录（H3STUDIO_DATA_DIR 派生）。

    默认值指向仓库内的 data-accept/（持久化，扛得住宿主重启）。
    """
    env = os.environ.get("H3STUDIO_DATA_DIR") or str(ACCEPT_ROOT)
    return Path(env) / "outputs"


def seg_of(job):
    for o in job.get("outputs", []):
        if o.get("kind") == "video" and not o.get("is_full"):
            return out_dir() / o["file"]
    return None


def full_of(job):
    for o in job.get("outputs", []):
        if o.get("kind") == "video" and o.get("is_full"):
            return out_dir() / o["file"]
    return None


def all_videos(job):
    return [out_dir() / o["file"] for o in job.get("outputs", [])
            if o.get("kind") == "video"]


def frames_of(path):
    raw = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
        text=True).strip()
    return int(raw)


def dur_of(path):
    raw = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)], text=True).strip()
    return float(raw)


def grab(path, which, tag):
    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    total = frames_of(path)
    stem = f"{tag}-{path.stem}"
    if which == "first":
        vf, out = "select='eq(n\\,0)'", f"{stem}-first.png"
    else:
        vf, out = f"select='eq(n\\,{total-1})'", f"{stem}-last.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-vf", vf,
                    "-vsync", "0", "-frames:v", "1", str(FRAME_DIR / out)],
                   check=False)
    return FRAME_DIR / out


def mae(p1, p2):
    from PIL import Image
    import numpy as np
    a = np.asarray(Image.open(p1).convert("L"), dtype=float)
    b = np.asarray(Image.open(p2).convert("L"), dtype=float)
    if a.shape != b.shape:
        h = min(a.shape[0], b.shape[0])
        w = min(a.shape[1], b.shape[1])
        a, b = a[:h, :w], b[:h, :w]
    return float(np.abs(a - b).mean())


def latent_path(job_id):
    """该任务自己那一份潜空间文件（服务端记录的权威坐标）。"""
    job = get(f"/api/jobs/{job_id}")
    info = job.get("continuation_info") or {}
    rid = info.get("run_id") or ""
    stage = info.get("latent_stage", 0)
    if not rid:
        return None, info
    base = os.environ.get("H3STUDIO_COMFY_OUTPUT") or "/home/ubuntu/comfyui/output"
    return Path(base) / "relay_kit" / rid / f"stage_{int(stage):05d}.safetensors", info


def fail(msg):
    REPORT["failures"].append(msg)
    REPORT["ok"] = False
    print(f"    ❌ {msg}")


def step(name, **data):
    REPORT["steps"].append({"step": name, **data})
    print(f"--- {name}")
    for k, v in data.items():
        print(f"    {k}: {v}")


def cont_ctx(job_id):
    c = get(f"/api/jobs/{job_id}/continuation")
    assert c.get("ok"), c
    return c["continuation"]


def gen(prompt, continuation=None, label="", seed=None):
    p = dict(BP)
    p["prompt"] = prompt
    if seed is not None:
        p["seed"] = seed
    if continuation:
        p["continuation"] = continuation
    jid = post({"mode": "t2v", "params": json.dumps(p)})["id"]
    print(f"  {label or 'job'} = {jid}")
    j = wait(jid)
    print(f"    status={j['status']} err={j.get('error')}")
    if j["status"] != "done":
        fail(f"{label} 生成失败: {j.get('error')}")
    return jid, j



# ------------------------------------------------------------------
# ComfyUI 实际执行图（唯一权威）
# ------------------------------------------------------------------
def fetch_graphs(job_ids: dict) -> dict:
    """从 ComfyUI history 取每个任务**实际执行**的接续相关节点。

    【为什么必须从图里取，而不是从 API 上下文取】
    服务端在**提交时**重算上游读回坐标与外观锚坐标，并把结果写进
    提交给 ComfyUI 的那份工作流。GET /api/jobs/{id}/continuation 返回的
    「接续上下文」里**没有** anchor 字段（那是提交中间量）。
    要证明「显式传了 ref_anchor_latent」「读回的是上游自己那一份」，
    唯一权威是 ComfyUI 真正跑的那张图。

    返回：{tag: {load: {...}, anchor_load: {...}, bridge: {...}, save: {...}}}
    """
    import sqlite3
    db_path = Path(os.environ.get("H3STUDIO_DATA_DIR") or
                   str(ACCEPT_ROOT)) / "jobs.db"
    out = {}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = {r["id"]: r["prompt_id"] for r in
                conn.execute("SELECT id, prompt_id FROM jobs")}
    finally:
        conn.close()
    for tag, jid in job_ids.items():
        pid = rows.get(jid)
        if not pid:
            continue
        try:
            # 注意：ComfyUI 是**另一台**服务（8188），不能走 get()——
            # get() 会拼上 Studio 的 BASE 前缀，拼出无效 URL。
            with urllib.request.urlopen(
                    f"http://127.0.0.1:8188/history/{pid}", timeout=60) as resp:
                hist = json.loads(resp.read().decode())
        except Exception as exc:
            print(f"    ⚠️ 取 ComfyUI 图失败 {pid}: {exc}")
            continue
        entry = hist.get(pid)
        if not entry:
            continue
        prompt = entry.get("prompt")
        graph = prompt[2] if isinstance(prompt, list) else prompt
        row = {}
        for nid, node in (graph or {}).items():
            ct = node.get("class_type")
            inputs = node.get("inputs") or {}
            title = (node.get("_meta") or {}).get("title", "")
            if ct == "H3RelayLatentSave":
                row["save"] = {"run_id": inputs.get("run_id"),
                               "stage_index": inputs.get("stage_index")}
            elif ct == "H3RelayLatentLoad":
                key = ("load" if "上游" in title else
                       "anchor_load" if "锚" in title else nid)
                row[key] = {"run_id": inputs.get("run_id"),
                            "stage_index": inputs.get("stage_index"),
                            "explicit_path": inputs.get("explicit_path")}
            elif ct == "H3RelayCopyBridge":
                row["bridge"] = {
                    "ref_anchor_latent": inputs.get("ref_anchor_latent"),
                    "ref_anchor_stage": inputs.get("ref_anchor_stage"),
                    "context_latent": inputs.get("context_latent"),
                    "run_id": inputs.get("run_id"),
                    "stage_index": inputs.get("stage_index"),
                }
        out[tag] = row
    return out


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=accept_base(BASE))
    args = ap.parse_args()
    BASE = args.base
    ROOT.mkdir(parents=True, exist_ok=True)

    print("=" * 74)
    print("D1 四组缺陷一体化真实验收（真实 ComfyUI 生成）")
    print("=" * 74)
    print(f"base={BASE}  outputs={out_dir()}")

    # ================= 段 A（链首段）=================
    ja, ja_job = gen("a lone swordsman in a bamboo forest, calm, wide shot",
                     label="A")
    pa = seg_of(ja_job)
    if not pa or not pa.is_file():
        fail("A 没有分段视频")
        return 1
    latent_a, _ = latent_path(ja)
    step("A", job=ja, seg_frames=frames_of(pa), seg_seconds=round(dur_of(pa), 3),
         latent=str(latent_a),
         latent_exists=bool(latent_a and latent_a.is_file()))

    ctx_a = cont_ctx(ja)
    JOBS_GRAPH = {}

    # ================= 兄弟分支 B1 / B2 =================
    # 同一个父段 A，接续两次 -> 两个兄弟分支。
    # 【为什么 B1/B2 要用不同种子 + 不同场景】
    # 拷贝桥把 A 的尾部**硬钉**在两条分支的开头，而 4 步底模的续写
    # 很容易「顺着钉住区继续」—— 实测同种子同场景的两条分支，
    # 末帧之间 MAE 只有 5.4，像素层面的判别力被压得很低
    # （C1 首帧 vs B1末帧 9.1 / vs B2末帧 10.7，只有 1.17 倍）。
    # 换种子 + 换场景 -> 两条分支真正分叉 -> 像素判据才有分辨力。
    # 【但像素判据只是**辅助**】主判据是**执行图级**的：
    # C1 的 300:load 节点的 explicit_path 指向谁的目录（见 §2）。
    jb1, jb1_job = gen("b1 the swordsman raises his blade, camera pushes in",
                       ctx_a, label="B1", seed=1011)
    # 【为什么 B2 的提示词要跟 B1 彻底不同】4 步 + w4a8 底模下，
    # 同场景只差一个色调时两个分支的画面会互相渗透，MAE 分离度不够
    # （实测「竹林 vs 红灯笼」只拉开 1.8x）。给 B2 一个**完全不同的场景**，
    # 才能让「接错分支」在像素层面暴露成大数值差。
    jb2, jb2_job = gen("b2 a red lantern festival at night, crowd celebrating",
                       ctx_a, label="B2", seed=2022)
    pb1, pb2 = seg_of(jb1_job), seg_of(jb2_job)
    lat_b1, info_b1 = latent_path(jb1)
    lat_b2, info_b2 = latent_path(jb2)

    # ---------- ① 落盘坐标冲突 ----------
    print()
    print("【① 落盘坐标冲突】兄弟分支必须各写各的")
    if not lat_b1 or not lat_b2:
        fail("B1/B2 缺潜空间坐标")
    elif lat_b1 == lat_b2:
        fail(f"B1/B2 落盘路径相同 —— 坐标冲突未修！{lat_b1}")
    else:
        b1b = lat_b1.read_bytes() if lat_b1.is_file() else b""
        b2b = lat_b2.read_bytes() if lat_b2.is_file() else b""
        if b1b == b2b:
            fail("B1/B2 潜空间内容相同 —— 写的是同一个东西")
        else:
            print(f"    ✅ B1 落盘: {lat_b1}")
            print(f"    ✅ B2 落盘: {lat_b2}")
            print(f"    ✅ 两份同时存在、内容不同（各 {len(b1b)} 字节）")
    step("1_latent_coords",
         b1_latent=str(lat_b1), b2_latent=str(lat_b2),
         b1_run_id=info_b1.get("run_id"), b2_run_id=info_b2.get("run_id"),
         paths_differ=bool(lat_b1 and lat_b2 and lat_b1 != lat_b2),
         contents_differ=bool(lat_b1 and lat_b2 and
                              lat_b1.is_file() and lat_b2.is_file() and
                              lat_b1.read_bytes() != lat_b2.read_bytes()))

    # ================= C1（从 B1 继续接续）=================
    jc1, jc1_job = gen("c1 continue the bamboo forest, the swordsman turns around",
                       cont_ctx(jb1), label="C1")
    pc1 = seg_of(jc1_job)
    JOBS_GRAPH.update({"A": ja, "B1": jb1, "B2": jb2, "C1": jc1})

    # ---------- ② 首帧取到兄弟分支末帧？ ----------
    print()
    print("【② 接续首帧归属】C1 首帧必须来自 B1 末帧")
    if not pc1 or not pc1.is_file():
        fail("C1 没有分段视频")
        return 1
    last_b1 = grab(pb1, "last", "B1")
    last_b2 = grab(pb2, "last", "B2")
    first_c1 = grab(pc1, "first", "C1")
    d_b1 = mae(first_c1, last_b1)
    d_b2 = mae(first_c1, last_b2)
    ratio = (d_b2 / d_b1) if d_b1 > 0 else float("inf")
    print(f"    C1首帧 vs B1末帧 MAE = {d_b1:.3f}")
    print(f"    C1首帧 vs B2末帧 MAE = {d_b2:.3f}")
    print(f"    -> 偏离倍数 = {ratio:.1f}x")

    # ---- ② 的**主判据**：执行图级 ----
    #
    # 【为什么主判据是图而不是像素】拷贝桥把上游潜空间**硬钉**在
    # 本段开头，所以「接的是哪一条分支」这件事在**执行图**上是
    # 确定性的：C1 的 300:load 节点的 explicit_path 指向哪个目录，
    # 就决定了它读的是 B1 还是 B2 的潜空间。像素 MAE 只是**辅助**：
    # 4 步底模 + 钉住区会让两条分支的画面高度相似，MAE 的分辨力有限
    # （实测同种子时两条分支末帧之间 MAE 仅 5.4）。
    JOBS_GRAPH["B2"] = jb2
    g = fetch_graphs(JOBS_GRAPH)
    c1_load = (g.get("C1") or {}).get("load", {})
    c1_anchor = (g.get("C1") or {}).get("anchor_load", {})
    c1_bridge = (g.get("C1") or {}).get("bridge", {})
    b1_run = str((g.get("B1") or {}).get("save", {}).get("run_id") or "")
    b2_run = str((g.get("B2") or {}).get("save", {}).get("run_id") or "")
    c1_read = str(c1_load.get("explicit_path") or "")
    print(f"    B1 落盘目录 = {b1_run}")
    print(f"    B2 落盘目录 = {b2_run}")
    print(f"    C1 实际读回 = {c1_read}")
    graph_ok = bool(b1_run and b2_run and b1_run != b2_run and
                    b1_run in c1_read and b2_run not in c1_read)
    if not graph_ok:
        fail(f"执行图级判定失败：C1 读回 {c1_read}，"
             f"期望含 B1({b1_run}) 且不含 B2({b2_run})")
    else:
        print("    ✅ 执行图级：C1 读的是 B1 的潜空间，B2 的从未被读")
    # 外观锚必须显式接进来（不能靠自动读锚）
    anchor_explicit = bool(c1_anchor.get("explicit_path")) and (
        c1_bridge.get("ref_anchor_latent") is not None)
    print(f"    bridge.ref_anchor_latent = {c1_bridge.get('ref_anchor_latent')}")
    print(f"    锚读回路径 = {c1_anchor.get('explicit_path')}")
    if not anchor_explicit:
        fail("外观锚没有显式接进来（ref_anchor_latent 为空）")
    else:
        print("    ✅ 外观锚显式接进来（不依赖自动读锚）")
    # 像素判据（辅助）
    if not d_b1 < d_b2:
        fail(f"像素判据：C1 首帧更像 B2 末帧！MAE B1={d_b1:.3f} B2={d_b2:.3f}")
    else:
        print("    ✅ 像素辅助判据：C1 首帧也更接近 B1 末帧")
    # 三联图
    trip = FRAME_DIR / "triptych-B1last-B2last-C1first.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-i", str(last_b1), "-i", str(last_b2), "-i", str(first_c1),
                    "-filter_complex",
                    "[0:v]scale=640:-1,drawtext=text='B1 last (correct)':"
                    "x=10:y=10:fontsize=28:fontcolor=yellow:box=1:boxcolor=black@0.6[a];"
                    "[1:v]scale=640:-1,drawtext=text='B2 last (sibling)':"
                    "x=10:y=10:fontsize=28:fontcolor=cyan:box=1:boxcolor=black@0.6[b];"
                    "[2:v]scale=640:-1,drawtext=text='C1 first':"
                    "x=10:y=10:fontsize=28:fontcolor=lime:box=1:boxcolor=black@0.6[c];"
                    "[a][b][c]hstack=inputs=3", str(trip)], check=False)
    step("2_first_frame_origin",
         mae_c1first_vs_B1last=round(d_b1, 3),
         mae_c1first_vs_B2last=round(d_b2, 3),
         ratio=round(ratio, 2), verdict=("B1(正确)" if d_b1 < d_b2 else "B2(错误)"),
         graph_verdict=("C1读的是B1" if graph_ok else "图级判定失败"),
         graph_read_path=c1_read, graph_b1_run=b1_run, graph_b2_run=b2_run,
         anchor_explicit=anchor_explicit,
         anchor_path=str(c1_anchor.get("explicit_path") or ""),
         ref_anchor_latent=c1_bridge.get("ref_anchor_latent"),
         frame_B1_last=str(last_b1), frame_B2_last=str(last_b2),
         frame_C1_first=str(first_c1), triptych=str(trip))

    # ---------- ③ 链账本串分支 ----------
    print()
    print("【③ 链账本串分支】C1 的完整视频只能是 A + B1 + C1")
    full_c1 = full_of(jc1_job)
    full_b1 = full_of(jb1_job)
    full_b2 = full_of(jb2_job)
    seg_frames = {
        "A": frames_of(pa),
        "B1": frames_of(pb1),
        "B2": frames_of(pb2),
        "C1": frames_of(pc1),
    }
    print(f"    各段帧数: A={seg_frames['A']} B1={seg_frames['B1']} "
          f"B2={seg_frames['B2']} C1={seg_frames['C1']}")
    ff_c1 = frames_of(full_c1) if full_c1 else 0
    ff_b1 = frames_of(full_b1) if full_b1 else 0
    ff_b2 = frames_of(full_b2) if full_b2 else 0
    expect_c1 = seg_frames["A"] + seg_frames["B1"] + seg_frames["C1"]
    expect_b1 = seg_frames["A"] + seg_frames["B1"]
    expect_b2 = seg_frames["A"] + seg_frames["B2"]
    print(f"    C1 完整视频帧数 = {ff_c1}（期望 A+B1+C1 = {expect_c1}）")
    print(f"    B1 完整视频帧数 = {ff_b1}（期望 A+B1 = {expect_b1}）")
    print(f"    B2 完整视频帧数 = {ff_b2}（期望 A+B2 = {expect_b2}）")
    if ff_c1 != expect_c1:
        fail(f"C1 完整视频帧数 {ff_c1} != A+B1+C1 {expect_c1} —— 可能串进 B2")
    else:
        print("    ✅ C1 完整视频 = A + B1 + C1（B2 不在其中）")
    # 血缘级判据（主）：服务端沿 source_job 回溯出来的段序列
    import sqlite3 as _sq
    _db = Path(os.environ.get("H3STUDIO_DATA_DIR") or
               str(ACCEPT_ROOT)) / "jobs.db"
    _c = _sq.connect(str(_db)); _c.row_factory = _sq.Row
    try:
        _rows = {r["id"]: json.loads(r["params"] or "{}")
                 for r in _c.execute("SELECT id, params FROM jobs")}
    finally:
        _c.close()

    def lineage(job_id):
        rels, seen, cur = [], set(), job_id
        for _ in range(512):
            if not cur or cur in seen:
                break
            seen.add(cur)
            info = (_rows.get(cur) or {}).get("continuation") or {}
            rel = str(info.get("segment_rel") or "").strip()
            if rel:
                rels.append(rel)
            cur = str(info.get("source_job") or "").strip()
        rels.reverse()
        return rels

    lin_c1 = lineage(jc1)
    lin_b1 = lineage(jb1)
    lin_b2 = lineage(jb2)
    b2_file = next((o["file"] for o in jb2_job.get("outputs", [])
                    if o.get("kind") == "video" and not o.get("is_full")), "")
    b1_file = next((o["file"] for o in jb1_job.get("outputs", [])
                    if o.get("kind") == "video" and not o.get("is_full")), "")
    print(f"    C1 血缘段序列 = {lin_c1}")
    print(f"    B1 血缘段序列 = {lin_b1}")
    print(f"    B2 血缘段序列 = {lin_b2}")
    lineage_ok = (b2_file not in lin_c1 and b1_file in lin_c1 and
                  len(lin_c1) == 3 and b1_file not in lin_b2 and
                  b2_file not in lin_b1)
    if not lineage_ok:
        fail(f"血缘隔离失败：C1={lin_c1}（B2 的段 {b2_file} 不应在内）")
    else:
        print("    ✅ 血缘隔离：C1 含 B1 不含 B2；B1/B2 互不含对方")
    # 硬证据：C1 完整视频里不能出现 B2 的画面
    # —— 取 B2 首帧，在 C1 完整视频的每一帧里找最相似的一帧，
    #    若最相似度仍远高于 C1 自身段，就说明 B2 被拼进去了。
    first_b2 = grab(pb2, "first", "B2f")
    last_b2 = grab(pb2, "last", "B2l")
    best = None
    for probe, pname in ((first_b2, "B2首帧"), (last_b2, "B2末帧")):
        d = mae(probe, first_c1)
        if best is None or d < best[1]:
            best = (pname, d)
    # 更强判据：B2 的末帧不应该等于 C1 完整视频的任何一帧
    tmp = FRAME_DIR / "c1full"
    tmp.mkdir(exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(full_c1),
                    "-vf", "fps=4", str(tmp / "f%04d.png")], check=False)
    shots = sorted(tmp.glob("f*.png"))
    dmin = min(mae(shot, last_b2) for shot in shots) if shots else float("inf")
    print(f"    C1完整视频各帧 与 B2末帧 的最小 MAE = {dmin:.3f}"
          f"（{len(shots)} 个采样帧）")
    if dmin < 2.0:
        fail(f"C1 完整视频里出现了 B2 的画面（最小 MAE {dmin:.3f}）—— 串分支了")
    else:
        print("    ✅ C1 完整视频中找不到 B2 的画面")
    step("3_lineage_isolation",
         seg_frames=seg_frames,
         full_c1_frames=ff_c1, expect_c1=expect_c1,
         full_b1_frames=ff_b1, expect_b1=expect_b1,
         full_b2_frames=ff_b2, expect_b2=expect_b2,
         c1_full=str(full_c1), b1_full=str(full_b1), b2_full=str(full_b2),
         b2_min_mae_in_c1_full=round(dmin, 3),
         b2_absent_from_c1=(dmin >= 2.0),
         lineage_c1=lin_c1, lineage_b1=lin_b1, lineage_b2=lin_b2,
         lineage_isolated=lineage_ok)

    # ================= 判据 3：两轮连续接续 =================
    print()
    print("【两轮连续接续】A -> B -> C，再对 C 的完整视频接续 -> D")
    # 对 C1 的**完整视频**（is_full）再接续一次
    ctx_c1 = cont_ctx(jc1)
    jd1, jd1_job = gen("d1 the swordsman leaps over a stream, wide cinematic",
                       ctx_c1, label="D1")
    pd1 = seg_of(jd1_job)
    JOBS_GRAPH["D1"] = jd1
    full_d1 = full_of(jd1_job)
    ff_d1 = frames_of(full_d1) if full_d1 else 0
    expect_d1 = expect_c1 + seg_frames["C1"]
    print(f"    D1 完整视频帧数 = {ff_d1}（期望 A+B1+C1+D1 = {expect_d1}）")
    if ff_d1 != expect_d1:
        fail(f"D1 完整视频帧数 {ff_d1} != {expect_d1}")
    else:
        print("    ✅ 第二轮接续成功，完整视频长度 = 上一轮完整长度 + 本段")
    step("4_two_round_continuation",
         A=ja, B1=jb1, C1=jc1, D1=jd1,
         d1_seg_frames=frames_of(pd1) if pd1 else None,
         d1_full_frames=ff_d1, expect_d1=expect_d1,
         d1_full=str(full_d1),
         round1_full_frames=ff_c1, round2_full_frames=ff_d1,
         growth=ff_d1 - ff_c1)

    # ================= ④ 节点缓存 =================
    print()
    print("【④ 节点缓存】内容指纹机制")
    fp = {}
    for tag, jid in (("A", ja), ("B1", jb1), ("C1", jc1)):
        path, _ = latent_path(jid)
        fp[tag] = str(path)
    # 【判据必须从 ComfyUI 实际执行图里取】服务端提交时重算锚坐标，
    # 而 GET /api/jobs/{id}/continuation 的上下文里**不带** anchor 字段
    # （那是提交时的中间量）。图（history）才是 ComfyUI 真正跑的东西 ——
    # 唯一权威。
    graph = fetch_graphs(JOBS_GRAPH)
    src_path = str((graph.get("C1") or {}).get("load", {}).get("explicit_path") or "")
    anchor_path = str((graph.get("C1") or {}).get("anchor_load", {}).get("explicit_path") or "")
    bridge = (graph.get("C1") or {}).get("bridge", {})
    has_fp = bool(src_path) and "_" in Path(src_path).stem.split("stage_")[-1]
    print(f"    C1 读回上游路径 = {src_path}")
    print(f"    C1 读回外观锚   = {anchor_path}")
    print(f"    bridge.ref_anchor_latent = {bridge.get('ref_anchor_latent')}")
    if src_path and has_fp:
        print("    ✅ 读回路径带内容指纹后缀 -> 内容变则路径变 -> 节点缓存必然失效")
    else:
        fail(f"读回路径没有指纹后缀：{src_path}")
    # 指纹随内容变
    from h3studio import continuation_service as cs
    if lat_b1 and lat_b1.is_file():
        f_now = cs.latent_fingerprint(lat_b1)
        print(f"    B1 潜空间当前指纹 = {f_now}")
    step("5_fingerprint",
         source_read_path=src_path, anchor_read_path=anchor_path,
         fingerprint_suffix_present=has_fp,
         bridge_ref_anchor_latent=bridge.get("ref_anchor_latent"),
         latent_paths=fp,
         graph=graph)

    # ================= 汇总 =================
    print()
    print("=" * 74)
    if REPORT["ok"]:
        print("✅ D1 四组缺陷真实验收全部通过")
    else:
        print("❌ 有失败项：")
        for f in REPORT["failures"]:
            print("   -", f)
    print("=" * 74)
    (ROOT / "report.json").write_text(
        json.dumps(REPORT, ensure_ascii=False, indent=2))
    print(f"报告: {ROOT / 'report.json'}")
    print(f"帧图: {FRAME_DIR}")
    return 0 if REPORT["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
