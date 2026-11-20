# -*- coding: utf-8 -*-
"""
线上 8190：「MP 预算 × 百分比」的 3 个真实用例（提交 -> 落库 params）。

用例：
  A 合法值原样放行        megapixels=0.4
  B 需吸附到 32 倍数      megapixels=0.41（换算后仍 864×480）
  C 超 MP 上限自动下调    megapixels=4.0（-> 1920×1088）

【为什么要真提交】用户要的是「原始请求值 -> 生效值 -> 调整原因」的
完整回传。落库的 params 就是**用户最终会看到的**那份数据。

【关于 GPU】提交后 ComfyUI 可能排队较久；本脚本只断言**落库 params**
（提交时算好），不等生成完成 —— 生成完成与否不影响本轮要验的东西。

用法：python3 verify_live_mp_cases.py <base_url> <out_dir>
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
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/live_mp")
OUT.mkdir(parents=True, exist_ok=True)

LIVE_DB = "/root/projects/comfyui-minimax-h3-studio/data/jobs.db"

PNG_1PX = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
           b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
           b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00"
           b"\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")

CASES = [
    ("A_legal_pass_through", "megapixels=0.4（合法值原样放行）",
     {"megapixels": 0.4}),
    ("B_snap_to_32", "megapixels=0.41（换算后吸附到 32 的倍数）",
     {"megapixels": 0.41}),
    ("C_over_cap_auto_reduced", "megapixels=4.0（超 2.0MP 上限，自动下调）",
     {"megapixels": 4.0}),
    ("D_percent_50", "resolution_percent=50（真实上限的 50%）",
     {"resolution_percent": 50}),
    ("E_percent_150_over_cap", "resolution_percent=150（超上限，自动下调）",
     {"resolution_percent": 150}),
]


def _http(method, path, data=None, headers=None):
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(
            f"{method} {path} -> HTTP {exc.code}: {body}") from exc


def upload_image():
    boundary = uuid.uuid4().hex
    name = f"mp-case-{uuid.uuid4().hex[:8]}.png"
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
    report = {"base": BASE, "cases": []}
    asset_id = upload_image()
    try:
        for key, label, extra in CASES:
            params = {"prompt": f"MP 用例 {key}", "asset_id": asset_id,
                      "duration": 2.0, **extra}
            result = post_job(
                "flf", params,
                asset_refs=[{"asset_id": asset_id, "slot": "first_frame",
                             "ordinal": 0}])
            job_id = result.get("id")
            time.sleep(0.4)
            job = read_job(job_id) if job_id else None
            p = (job or {}).get("params") or {}
            entry = {
                "key": key, "label": label,
                "requested": extra,
                "job_id": job_id,
                "effective_width": p.get("width"),
                "effective_height": p.get("height"),
                "effective_megapixels": p.get("megapixels"),
                "effective_resolution": p.get("resolution"),
                "size_adjust_reasons": p.get("size_adjust_reasons"),
                "status": (job or {}).get("status"),
            }
            report["cases"].append(entry)
            print(f"[{key}] {label}")
            print(f"   原始请求 : {extra}")
            print(f"   生效宽高 : {p.get('width')} x {p.get('height')}"
                  f"  MP={p.get('megapixels')}")
            print(f"   调整原因 : {p.get('size_adjust_reasons')}")
    finally:
        delete_asset(asset_id)

    (OUT / "mp_cases_live.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== 汇总 ===")
    for c in report["cases"]:
        print(f"{c['key']:26s} {c['requested']} -> "
              f"{c['effective_width']}x{c['effective_height']} "
              f"MP={c['effective_megapixels']}")


if __name__ == "__main__":
    main()
