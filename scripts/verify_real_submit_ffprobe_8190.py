#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真实提交 + ffprobe 可播验证（2026-11-21）。

【验证什么】
1. 用**增强后的提示词**（带 <Picture N> 参考编号）提交一个 **≤5 秒**
   的真实 H3 视频任务（flf 面板，首尾帧两张参考图）；
2. 等任务跑完，拿到产物；
3. ffprobe 验证产物**可播**（容器 / 编码 / 分辨率 / 时长 / 帧数）；
4. ffmpeg 整段解码，验证**没有坏帧**；
5. 核对**落库的 params** 与面板一致（提示词含参考编号、素材引用齐全）。

【为什么必须真跑一遍】增强出来的提示词最终要**真的送进 ComfyUI 出片**。
只在 API 层证明「标签对了」不够 —— 必须证明「带标签的提示词能出片，
且片能播」。

用法：python3 scripts/verify_real_submit_ffprobe_8190.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

BASE = os.environ.get("H3_BASE", "http://127.0.0.1:8190")
OUT = Path(os.environ.get(
    "H3_EVIDENCE_DIR",
    "/root/projects/h3s-fix-reflabel-syntax/evidence/real-submit-8190"))
OUT.mkdir(parents=True, exist_ok=True)

#: 提交时长上限（秒）—— 用户要求「≤5 秒」
DURATION = 5


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def curl_json(method, path, data=None, form=None, timeout=120):
    """发一个 JSON 或 multipart/form-data 请求，返回解析后的 JSON。

    【为什么自己拼 multipart】本机 requests 未必可用，而 aiohttp 只在
    服务端进程里。这里用标准库 urllib + 手写 multipart 编码 ——
    只有两个纯文本字段（mode / params / asset_refs），不需要文件上传，
    所以编码很简单。
    """
    import urllib.request
    url = f"{BASE}{path}"
    if form is not None:
        # form = [(name, value), ...]
        boundary = "----H3StudioVerifyBoundary7MA4YWxkTrZu0gW"
        chunks = []
        for name, value in form:
            chunks.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode("utf-8"))
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(chunks)
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Content-Type",
                       f"multipart/form-data; boundary={boundary}")
    else:
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def ffprobe(path):
    proc = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return {"_error": proc.stderr[-500:]}
    return json.loads(proc.stdout)


def decode_check(path):
    """整段解码，看有没有坏帧。"""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True, timeout=600)
    return {
        "returncode": proc.returncode,
        "stderr": proc.stderr[-2000:],
        "clean": proc.returncode == 0 and not proc.stderr.strip(),
    }


def find_artifact(job_id):
    """从任务详情里找产物路径。"""
    detail = curl_json("GET", f"/api/jobs/{job_id}")
    (OUT / f"job-{job_id}.json").write_text(
        json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
    return detail


def pick_assets(n=2):
    items = curl_json("GET", "/api/assets?limit=60&category=image")
    assets = items.get("items") or items.get("assets") or []
    picks = []
    for a in assets:
        if a.get("id") and not a.get("deleted_at"):
            picks.append(a["id"])
        if len(picks) >= n:
            break
    return picks


def main():
    log(f"读取素材（取 {2} 张图片）")
    assets = pick_assets(2)
    if len(assets) < 2:
        print("!! 素材库里的图片不足 2 张，无法做首尾帧提交")
        return 1
    log(f"素材：{assets}")

    # ---- 1) 先走增强，拿到带参考编号的提示词 ----
    log("调用 /api/enhance 拿带参考编号的提示词")
    enh = curl_json("POST", "/api/enhance", data={
        "prompt": "镜头从女孩慢慢推向远景，保持主体一致",
        "mode": "replace",
        "spec_id": "video-director",
        "mode_ctx": "flf",
        "asset_refs": [
            {"asset_id": assets[0], "slot": "first_frame", "ordinal": 0},
            {"asset_id": assets[1], "slot": "last_frame", "ordinal": 0},
        ],
    }, timeout=900)
    prompt = (enh.get("enhanced") or "").strip()
    rl = enh.get("reference_labels") or {}
    log(f"增强完成：{len(prompt)} 字，reference_labels="
        f"{json.dumps(rl, ensure_ascii=False)}")
    if not prompt:
        print("!! 增强结果为空")
        return 1
    missing = [x for x in (rl.get("expected") or [])
               if x not in (rl.get("found") or [])]
    if missing:
        print(f"!! 增强结果缺参考标签 {missing}")
        return 1

    # ---- 2) 真实提交（≤5 秒）----
    log(f"提交真实任务（duration={DURATION}s）")
    import uuid
    params = {
        "prompt": prompt,
        "duration": DURATION,
        "model_key": "fl2va_pruned_int8",
        "aspect": "16:9",
        "megapixels": 0.4,
        "resolution_percent": 100.0,
    }
    # 【素材怎么传】flf 面板的参考图走 **asset_refs**（素材库引用）
    # 这条通道 —— server.py 的 _apply_asset_refs 会把它解析成 ComfyUI
    # 文件名并落到 params.first_frame / last_frame。
    # 直接传 first_frame 路径会被「外部媒体路径」闸门拒掉。
    asset_refs = [
        {"asset_id": assets[0], "slot": "first_frame", "ordinal": 0},
        {"asset_id": assets[1], "slot": "last_frame", "ordinal": 0},
    ]
    boundary = uuid.uuid4().hex
    form = []
    form.append(("mode", "flf"))
    form.append(("params", json.dumps(params, ensure_ascii=False)))
    form.append(("asset_refs", json.dumps(asset_refs, ensure_ascii=False)))
    job = curl_json("POST", "/api/jobs", form=form, timeout=180)
    job_id = job.get("id")
    log(f"任务 id = {job_id}  status = {job.get('status')}")

    # ---- 3) 等任务完成 ----
    deadline = time.time() + 3600
    detail = None
    while time.time() < deadline:
        time.sleep(10)
        detail = find_artifact(job_id)
        st = detail.get("status")
        log(f"  任务状态 = {st}")
        if st in ("done", "failed", "error", "cancelled"):
            break
    if detail is None:
        print("!! 没拿到任务详情")
        return 1

    status = detail.get("status")
    log(f"最终状态 = {status}")
    if status != "done":
        print(f"!! 任务未成功：{status}")
        print(json.dumps(detail, ensure_ascii=False)[:1500])
        return 1

    # ---- 4) 落库 params 核对 ----
    # 【读哪一份 params】任务详情里有两个 params：
    #   * gen_params —— 给人看的**展示**参数（模型名 / 步数 / 尺寸…）
    #   * params    —— **落库的原始参数**（prompt / asset_refs / 尺寸…）
    # 素材引用在 params 里（gen_params 里没有），所以必须读 params。
    params_db = detail.get("params") or {}
    db_prompt = (params_db.get("prompt") or "")
    db_asset_refs = params_db.get("asset_refs") or []
    log(f"落库 prompt 长度 = {len(db_prompt)}")
    log(f"落库 asset_refs = {json.dumps(db_asset_refs, ensure_ascii=False)}")
    print()
    print("=== 落库核对 ===")
    ok = True
    for label in (rl.get("expected") or []):
        hit = label in db_prompt
        print(f"  落库 prompt 含 {label}: {hit}")
        if not hit:
            ok = False
    slots = sorted({r.get("slot") for r in db_asset_refs})
    print(f"  落库 asset_refs 槽位 = {slots}")
    if len(db_asset_refs) != 2:
        print("  !! 落库素材引用不是 2 项")
        ok = False
    else:
        print("  落库素材引用 2 项齐全 ✓")
    # 落库的 first_frame / last_frame 必须真的指向素材库文件
    for slot in ("first_frame", "last_frame"):
        val = params_db.get(slot)
        print(f"  落库 {slot} = {val}")
        if not val:
            print(f"  !! 落库 {slot} 为空")
            ok = False

    # ---- 5) 找产物 + ffprobe ----
    # 【产物路径在哪】/api/jobs/{id} 的 outputs 是 list，每项形如
    #   {"kind": "video", "file": "v-h3-studio-000211.mp4", "name": ...}
    # file 是**相对于产物根目录**的相对路径（实测 8190 的产物根在
    # h3studio-live 下），所以拼绝对路径时要试两个根。
    outputs = detail.get("outputs") or []
    if isinstance(outputs, dict):
        outputs = [outputs]
    rel = ""
    for it in outputs:
        if isinstance(it, str):
            rel = it
            break
        if isinstance(it, dict):
            kind = (it.get("kind") or "").lower()
            if kind in ("video", "image", "audio", ""):
                rel = it.get("file") or it.get("name") or it.get("path") or ""
                if rel:
                    break
    log(f"产物相对路径 = {rel}")
    if not rel:
        print("!! 没找到产物路径，outputs =", json.dumps(outputs, ensure_ascii=False)[:500])
        return 1

    # 【产物根目录在哪】server.py 里：
    #     DATA = Path(CFG["data_dir"]); OUT_DIR = DATA / "outputs"
    # 而 config.json 的 data_dir 指向的是**共享数据目录**
    # （/root/projects/comfyui-minimax-h3-studio/data）——
    # 不是 live worktree 自己的目录（8190 与主仓共用一份 data）。
    # 所以产物相对路径要拼到 config 的 data_dir/outputs 下。
    roots = []
    cfg_path = Path("/root/projects/h3studio-live/config.json")
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            data_dir = cfg.get("data_dir")
            if data_dir:
                roots.append(Path(data_dir) / "outputs")
        except Exception as exc:
            log(f"读 config.json 失败：{exc}")
    roots += [Path("/root/projects/h3studio-live/data/outputs"),
              Path("/root/projects/h3studio-live/outputs"),
              Path("/root/projects/h3studio-live")]
    p = None
    if rel.startswith("/"):
        p = Path(rel)
    else:
        for root in roots:
            cand = root / rel
            if cand.is_file():
                p = cand
                break
    if p is None:
        # 最后兜底：在几个已知根里按文件名搜
        import subprocess as sp
        for root in roots:
            try:
                out = sp.run(["find", str(root), "-name", Path(rel).name,
                              "-type", "f"],
                             capture_output=True, text=True, timeout=60).stdout
                if out.strip():
                    p = Path(out.strip().splitlines()[0])
                    break
            except Exception:
                pass
    if p is None or not p.is_file():
        print(f"!! 产物文件不存在：{rel}")
        print("候选根：", [str(r) for r in roots])
        return 1
    log(f"产物路径 = {p}  大小 = {p.stat().st_size} 字节")

    probe = ffprobe(p)
    (OUT / "ffprobe.json").write_text(
        json.dumps(probe, ensure_ascii=False, indent=2), encoding="utf-8")
    vstream = next((s for s in probe.get("streams", [])
                    if s.get("codec_type") == "video"), {})
    fmt = probe.get("format", {})
    dur = float(fmt.get("duration") or 0)
    log(f"ffprobe: {fmt.get('format_name')} / {vstream.get('codec_name')} "
        f"/ {vstream.get('width')}x{vstream.get('height')} / {dur:.3f}s")

    dec = decode_check(p)
    (OUT / "ffmpeg-decode-check.json").write_text(
        json.dumps(dec, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"ffmpeg 整段解码：clean={dec['clean']} rc={dec['returncode']}")

    print()
    print("=== 判定 ===")
    if dur <= 0:
        print("  !! 时长为 0")
        ok = False
    if dur > DURATION + 1.0:
        print(f"  !! 时长 {dur:.3f}s 超过 {DURATION}s 上限（容差 1s）")
        ok = False
    else:
        print(f"  时长 {dur:.3f}s ≤ {DURATION}s ✓")
    if vstream.get("codec_name") != "h264":
        print(f"  !! 视频编码不是 h264：{vstream.get('codec_name')}")
        ok = False
    else:
        print(f"  视频编码 h264 ✓（{vstream.get('width')}x{vstream.get('height')}）")
    if not dec["clean"]:
        print(f"  !! 整段解码有报错：{dec['stderr'][:400]}")
        ok = False
    else:
        print("  整段解码无错 ✓")

    # 拷贝产物到证据目录
    dst = OUT / p.name
    dst.write_bytes(p.read_bytes())
    log(f"产物已复制到 {dst}")

    summary = {
        "job_id": job_id,
        "status": status,
        "duration_sec_requested": DURATION,
        "assets": assets,
        "enhance_reference_labels": rl,
        "db_prompt": db_prompt,
        "db_asset_refs": db_asset_refs,
        "artifact_path": str(p),
        "artifact_bytes": p.stat().st_size,
        "ffprobe": {"format_name": fmt.get("format_name"),
                    "codec": vstream.get("codec_name"),
                    "width": vstream.get("width"),
                    "height": vstream.get("height"),
                    "duration": dur,
                    "nb_frames": vstream.get("nb_frames")},
        "decode_clean": dec["clean"],
        "verdict_ok": ok,
    }
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"证据已写入 {OUT / 'summary.json'}")

    print()
    print("=== 真实提交 + ffprobe", "通过" if ok else "失败", "===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
