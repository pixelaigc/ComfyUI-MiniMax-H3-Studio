#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用**线上生产参数**直接向 ComfyUI 提交一条 r2v 任务，捕获**真实 OOM** 的
history 条目（ComfyUI 侧真实报错，不是构造的）。

【为什么绕过 studio 直接打 ComfyUI】

生产上那两条 OOM 样本（628d92cfc062 / 86db94915467）的参数是现成的：
    r2v / 1920x1088 / 15s & 20s / steps=8 / hybrid_fl2va_ref2va
    refs = [ref_image asset_d706cdfa6101, ref_audio asset_13cb1ba4dcd9]
    audio_track_source = reference

用**同一套素材 + 同一分辨率 + 同样时长**直接提交到 ComfyUI，
ComfyUI 会真的去分配显存 -> 真的 OOM -> history 里留下
    status.status_str = 'error'
    status.messages   = [['execution_error', {... 'out of memory' ...}]]
    outputs           = {...}   <- 已执行完节点的输出（可能只有文本节点）

这正是**原缺陷的触发条件**：旧逻辑只看 `hist.get("outputs")` 非空
-> 判「成功」；新逻辑先看 status -> 判失败。

【重要】这一条会**真的把显存打满**。本机 48GB 卡上，1920x1088 x 15s
的 r2v（带参考音频）会触发 OOM —— ComfyUI 会**报** OOM（history
里留下 error 条目），但**进程本身通常活着**（这是我们要的形态：
服务端能拿到完整的 OOM 原因，而不是「连接被拒」）。

脚本会：
  1. 用线上生产参数拼出真实工作流（走 h3studio 自己的 builder）；
  2. POST 到 ComfyUI /prompt；
  3. 轮询 /history/{prompt_id} 直到 status 落定；
  4. 把**真实拿到的** history 条目存成 JSON，供 evidence_oom_failure_e2e.py
     回放（那条脚本会用它走线上 studio 的 worker 代码路径）。

用法：python3 scripts/capture_real_oom_history.py [--duration 15]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

LIVE = Path("/root/projects/h3studio-live")
COMFY = "http://127.0.0.1:8188"
OUT = Path("/root/projects/h3s-merge-20261119/evidence/gen-status-20261119")

REF_IMAGE = "asset_d706cdfa6101"
REF_AUDIO = "asset_13cb1ba4dcd9"


def get_json(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def post_json(url, payload, timeout=120):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def build_r2v_prompt(duration: float, ref_images, ref_audios) -> dict:
    """用**线上生产参数**拼出真实工作流（走 h3studio 自己的 builder）。

    ref_images / ref_audios 必须是**已经上传到 ComfyUI input** 的文件名 ——
    builder 的 ref_images 槽位收的就是 ComfyUI 侧的文件名。
    """
    sys.path.insert(0, str(LIVE))
    from h3studio import h3_video_modes  # noqa: E402
    params = {
        "prompt": "A calm portrait in soft studio light, slow camera push-in.",
        "model_key": "hybrid_fl2va_ref2va",
        "width": 1920,
        "height": 1088,
        "duration": duration,
        "steps": 8,
        "cfg": 1.0,
        "shift_video": 12.0,
        "shift_audio": 2.0,
        "audio_track_source": "reference",
        "ref_images": list(ref_images),
        "ref_audios": list(ref_audios),
        # _build_common 用 job_id 派生潜空间落盘坐标（接续链用）。
        # 这里只是为拿到可提交的 prompt，不做接续，给一个固定值即可。
        "job_id": "oomreplay0001",
    }
    prompt = h3_video_modes.build_r2v(params)
    return prompt


def upload_refs():
    """把线上那两个素材上传到 ComfyUI input，返回 (ref_images, ref_audios)。"""
    import asyncio
    sys.path.insert(0, str(LIVE))
    from aiohttp import ClientSession
    from h3studio import asset_repo as _repo
    from h3studio import assets as _assets
    import server as studio_server

    with _assets.connect() as conn:
        paths = {}
        for asset_id in (REF_IMAGE, REF_AUDIO):
            version = _repo.current_version(conn, asset_id)
            path = _assets.resolve_inside(_assets.library_root(),
                                         version["storage_path"])
            paths[asset_id] = path

    async def _run():
        async with ClientSession() as session:
            img_name = await studio_server.comfy_upload_image(
                session, paths[REF_IMAGE])
            safe_id = REF_AUDIO.replace("_", "-")[-12:]
            aud_name = await studio_server.comfy_upload_audio_flat(
                session, paths[REF_AUDIO],
                f"h3studio_ref_{safe_id}{paths[REF_AUDIO].suffix.lower()}")
            return img_name, aud_name

    return asyncio.run(_run())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=15.0)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    print("═══ 拼装真实 r2v 工作流（线上生产参数）═══")
    print("  上传参考素材到 ComfyUI input ...")
    img_name, aud_name = upload_refs()
    print(f"  ref_image = {img_name}")
    print(f"  ref_audio = {aud_name}")
    prompt = build_r2v_prompt(args.duration, [img_name], [aud_name])
    print(f"  节点数: {len(prompt)}")

    print("═══ 提交到 ComfyUI ═══")
    resp = post_json(f"{COMFY}/prompt", {"prompt": prompt})
    if "error" in resp:
        print("  !! 提交失败:", json.dumps(resp, ensure_ascii=False)[:800])
        return 1
    prompt_id = resp.get("prompt_id")
    print(f"  prompt_id = {prompt_id}")

    print("═══ 等待执行落定 ═══")
    deadline = time.time() + 1800
    entry = None
    while time.time() < deadline:
        time.sleep(10)
        try:
            h = get_json(f"{COMFY}/history/{prompt_id}")
        except Exception:
            continue
        entry = h.get(prompt_id)
        if not entry:
            continue
        st = entry.get("status") or {}
        if st.get("status_str"):
            print(f"  status_str = {st.get('status_str')!r}")
            print(f"  outputs keys = {list((entry.get('outputs') or {}).keys())}")
            break
    else:
        print("  !! 30 分钟内没落定")
        return 1

    st = entry.get("status") or {}
    print("\n═══ 真实 OOM 的 messages ═══")
    for m in st.get("messages") or []:
        if isinstance(m, (list, tuple)) and len(m) > 1 and m[0] == "execution_error":
            body = m[1]
            print(f"  node_type = {body.get('node_type')}")
            print(f"  exception_type = {body.get('exception_type')}")
            print(f"  exception_message = {str(body.get('exception_message'))[:400]!r}")

    out = OUT / "oom_history_real.json"
    out.write_text(json.dumps(entry, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n  已存: {out}")

    is_error = st.get("status_str") == "error"
    has_oom = "out of memory" in json.dumps(entry, ensure_ascii=False).lower()
    print(f"  status_str=='error' : {is_error}")
    print(f"  文本里含 out of memory : {has_oom}")
    print(f"  outputs 非空（旧逻辑会误判成功的条件）: {bool(entry.get('outputs'))}")
    return 0 if is_error else 1


if __name__ == "__main__":
    sys.exit(main())
