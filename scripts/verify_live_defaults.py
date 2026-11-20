# -*- coding: utf-8 -*-
"""
8190 线上验证：flf / r2v 真实落库 job params + 默认参数下发接口。

【为什么要有这个脚本】单测跑在临时实例上；这个脚本跑在**线上服务**
上，用**真实 HTTP** 打真实接口，然后查**线上库**里落库的 params ——
那是用户实际会看到的东西。

用法：
    python3 verify_live_defaults.py <base_url> <out_dir>
"""
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/live_verify")
OUT.mkdir(parents=True, exist_ok=True)

#: 线上库（服务配置里的 data_dir）
LIVE_DB = "/root/projects/comfyui-minimax-h3-studio/data/jobs.db"

PNG_1PX = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
           b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
           b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00"
           b"\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def _http(method, path, data=None, headers=None):
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 【为什么要带上正文】400/500 的**原因在正文里** ——
        # 只看状态码根本不知道是「宽高无效」还是「底模不支持」。
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(
            f"{method} {path} -> HTTP {exc.code}: {body}") from exc


def upload_image():
    boundary = uuid.uuid4().hex
    name = f"live-verify-{uuid.uuid4().hex[:8]}.png"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files"; '
        f'filename="{name}"\r\n'
        f"Content-Type: image/png\r\n\r\n").encode("utf-8") + PNG_1PX + (
        f"\r\n--{boundary}--\r\n").encode("utf-8")
    data = _http("POST", "/api/assets", body, {
        "Content-Type": f"multipart/form-data; boundary={boundary}"})
    for item in data.get("results") or []:
        if item.get("ok") and item.get("asset_id"):
            return item["asset_id"]
    raise AssertionError(f"上传失败：{data}")


def delete_asset(asset_id):
    try:
        req = urllib.request.Request(
            f"{BASE}/api/assets/{asset_id}", method="DELETE")
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except Exception:
        pass


def post_job(mode, params, asset_refs=None):
    boundary = uuid.uuid4().hex
    parts = []

    def field(name, value):
        parts.append(
            (f"--{boundary}\r\n"
             f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
             f"{value}\r\n").encode("utf-8"))

    field("mode", mode)
    field("params", json.dumps(params, ensure_ascii=False))
    if asset_refs:
        field("asset_refs", json.dumps(asset_refs, ensure_ascii=False))
    body = b"".join(parts) + f"--{boundary}--\r\n".encode("utf-8")
    return _http("POST", "/api/jobs", body, {
        "Content-Type": f"multipart/form-data; boundary={boundary}"})


def read_job(job_id):
    conn = sqlite3.connect(LIVE_DB)
    try:
        row = conn.execute(
            "SELECT id, mode, status, params FROM jobs WHERE id=?",
            (job_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"id": row[0], "mode": row[1], "status": row[2],
            "params": json.loads(row[3] or "{}")}


def main():
    report = {"base": BASE, "steps": []}

    # ---- 1) 默认参数下发接口 ----
    settings = _http("GET", "/api/agent/settings")
    spec = settings.get("gen_defaults_spec") or {}
    stored = settings.get("gen_defaults") or {}
    report["settings_endpoint"] = {
        "flf_stored": stored.get("flf"),
        "r2v_stored": stored.get("r2v"),
        "flf_spec_fields": {
            f["name"]: {"type": f["type"], "default": f.get("default"),
                        "max": f.get("max"), "note": f.get("note"),
                        "options": [o["value"] for o in (f.get("options") or [])]}
            for f in (spec.get("flf") or {}).get("fields") or []
            if f["name"] in ("resolution", "megapixels", "resolution_percent",
                             "duration")},
        "r2v_spec_fields": {
            f["name"]: {"type": f["type"], "default": f.get("default"),
                        "max": f.get("max"), "note": f.get("note"),
                        "options": [o["value"] for o in (f.get("options") or [])]}
            for f in (spec.get("r2v") or {}).get("fields") or []
            if f["name"] in ("resolution", "megapixels", "resolution_percent",
                             "duration")},
    }
    (OUT / "settings_endpoint.json").write_text(
        json.dumps(report["settings_endpoint"], ensure_ascii=False, indent=2),
        encoding="utf-8")

    # ---- 2) flf / r2v 各一条真实落库 job ----
    #
    # 【为什么 flf 与 r2v 的素材不同】
    #   * flf（首尾帧）需要**图片**素材 -> 本脚本上传一张（并在
    #     finally 里清理 —— 共享素材库纪律）。
    #   * r2v（参考生）需要**参考视频**素材 -> 用素材库里**既有的**
    #     视频素材（不新建，避免留下无法清理的测试资产）。
    asset_id = upload_image()
    video_id = None
    try:
        assets = _http("GET", "/api/assets?limit=60")
        items = assets.get("items") or assets.get("assets") or []
        videos = [a for a in items if a.get("category") == "video"]
        if videos:
            video_id = videos[0]["id"]
        report["ref_video_asset"] = video_id

        for mode, params, refs, label in (
            ("flf", {"prompt": "线上验证：首尾帧默认参数落库",
                     "asset_id": asset_id},
             [{"asset_id": asset_id, "slot": "first_frame", "ordinal": 0}],
             "flf"),
            ("r2v", {"prompt": "线上验证：参考生默认参数落库",
                     "asset_id": video_id},
             ([{"asset_id": video_id, "slot": "ref_video", "ordinal": 0}]
              if video_id else None),
             "r2v"),
        ):
            if mode == "r2v" and not video_id:
                report["steps"].append({
                    "label": label, "skipped": "素材库里没有可用的视频素材",
                })
                continue
            result = post_job(mode, params, asset_refs=refs)
            job_id = result.get("id")
            time.sleep(0.5)
            job = read_job(job_id) if job_id else None
            entry = {"label": label, "job_id": job_id,
                     "created": result, "db_row": job}
            report["steps"].append(entry)
            (OUT / f"job_{label}.json").write_text(
                json.dumps(entry, ensure_ascii=False, indent=2),
                encoding="utf-8")
            print(f"[{label}] job_id={job_id}")
            if job:
                p = job["params"]
                print(f"  width={p.get('width')} height={p.get('height')} "
                      f"megapixels={p.get('megapixels')} "
                      f"duration={p.get('duration')} "
                      f"resolution={p.get('resolution')}")
                print(f"  size_adjust_reasons={p.get('size_adjust_reasons')}")
    finally:
        delete_asset(asset_id)

    (OUT / "live_verify.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== 落库摘要 ===")
    for entry in report["steps"]:
        row = entry.get("db_row") or {}
        p = row.get("params") or {}
        print(f"{entry['label']}: {p.get('width')}×{p.get('height')} "
              f"MP={p.get('megapixels')} dur={p.get('duration')}")


if __name__ == "__main__":
    main()
