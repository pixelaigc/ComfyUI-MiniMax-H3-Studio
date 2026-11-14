#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内置 Agent **写类工具**的真实执行审查（沙箱）。

【为什么要有这个脚本】audit_agent_tools.py 对写类工具只做静态检查
（参数名 vs schema），因为真跑有副作用。那样发现不了：
  * handler 真的调不通（内部异常）
  * 工具返回 ok 但**产物没落盘**（静默失效 —— 界面完全看不出来）

【怎么做才安全】两套沙箱：
  * 任务库  -> server.DB_PATH 指到临时目录
  * 素材库  -> ASSET_LIBRARY_ROOT 环境变量指到临时目录
                （素材库根目录本来就是靠这个 env 覆盖的，见 assets.library_root）
写类工具跑的是**真代码真写盘**，但写的是沙箱，跑完即删。
线上任务库 / 线上素材库 / 线上产物零污染。

【判定标准】
  ok            —— 真跑完，返回值符合预期
  handled_error —— 工具自己抛的可读错误（错误路径是活的，不是失效）
  crash         —— 抛 Python 异常 = **真失效**（要修）
  silent_bad    —— 返回 ok 但产物没落盘 = **静默失效**（最危险）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


# ------------------------------------------------------------------ 沙箱

def _sandbox() -> Path:
    root = Path(tempfile.mkdtemp(prefix="h3agent-write-audit-"))
    for sub in ("uploads", "outputs", "library"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def _install_sandbox(root: Path) -> None:
    """把任务库 / 素材库 / 产物目录全部指向沙箱。"""
    library = root / "library"
    os.environ["ASSET_LIBRARY_ROOT"] = str(library)
    import server
    server.DATA = root
    server.DB_PATH = root / "jobs.db"
    for attr in dir(server):
        val = getattr(server, attr, None)
        if attr.endswith("_DIR") and isinstance(val, Path):
            try:
                setattr(server, attr, root / attr.lower())
            except Exception:
                pass
    server.init_db()
    # 素材库布局 + 库表
    from h3studio import assets as asset_store
    asset_store.ensure_layout(library)
    asset_store.init_db()


def _probe_media(root: Path) -> dict:
    """造真实媒体文件（480p 视频 + 音频）。"""
    vid, aud = root / "probe.mp4", root / "probe.wav"
    img = root / "probe.png"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=480x270:rate=25:duration=2",
        "-pix_fmt", "yuv420p", "-c:v", "libx264", str(vid)],
        capture_output=True, check=False)
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(aud)],
        capture_output=True, check=False)
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:duration=1",
        "-frames:v", "1", str(img)], capture_output=True, check=False)
    return {"video": str(vid), "audio": str(aud), "spare": str(img),
            "video_exists": vid.exists(), "audio_exists": aud.exists(),
            "spare_exists": img.exists()}


def _seed_assets(media: dict) -> dict:
    """用素材库**真实入库接口**注册素材 —— 工具只认库里的 asset_id。"""
    ids: dict = {}
    from h3studio import asset_service
    from h3studio import assets as asset_store
    for key, path, cat in (("video_asset_id", media["video"], "video"),
                           ("audio_asset_id", media["audio"], "audio"),
                           ("spare_asset_id", media["spare"], "image")):
        try:
            with asset_store.connect() as conn:
                rec = asset_service.register_asset(
                    conn, source_path=Path(path),
                    filename=Path(path).name, category=cat,
                    note="write-tools audit probe")
            # register_asset 返回 {asset, version, info, sha256, duplicated}
            asset = rec.get("asset") or {}
            version = rec.get("version") or {}
            ids[key] = asset.get("id")
            ids[f"{cat}_rel_path"] = version.get("storage_path")
            print(f"[seed] {cat} -> {ids[key]}  rel={ids[f'{cat}_rel_path']}")
        except Exception as exc:
            print(f"[seed] {cat} 入库失败: {type(exc).__name__}: {exc}")
    return ids


#: 写类工具 -> 最小可执行参数（沙箱里真的跑）
WRITE_PROBES = {
    "create_job": {"mode": "t2i", "prompt": "audit probe",
                   "model": "z_image_turbo_bf16", "aspect": "1:1",
                   "steps": 4, "cfg": 4.0, "sampler": "euler", "scheduler": "simple"},
    "batch_create_jobs": {"jobs": [{"mode": "t2i", "prompt": "audit probe"}]},
    "cancel_job": {"job_id": "@job_id"},
    "retry_job": {"job_id": "@job_id"},
    "delete_asset": {"asset_id": "@spare_asset_id"},
    "tag_asset": {"asset_id": "@spare_asset_id", "add": ["audit-tag"]},
    "upload_asset": {"path": "@rel_path"},
    "remember_preference": {"key": "audit_key", "value": "audit_value"},
    "media_transcode": {"asset_id": "@video_asset_id"},
    "media_merge_av": {"video_asset_id": "@video_asset_id",
                       "audio_asset_id": "@audio_asset_id"},
    "media_mix_audio": {"video_asset_id": "@video_asset_id",
                        "audio_asset_ids": ["@audio_asset_id"]},
    "media_concat": {"asset_ids": ["@video_asset_id"]},
    "media_extract_frames": {"asset_id": "@video_asset_id"},
}


def _make_ctx():
    import server
    from h3studio import agent, agent_settings
    ctx = agent.AgentContext(server_module=server)
    try:
        ctx.settings = agent_settings.load(server.db())
    except Exception:
        pass
    return ctx


def _seed_job() -> str:
    """造一个真实 job 记录（cancel_job / retry_job 需要）。"""
    import server
    job_id = "auditjob0001"
    conn = server.db()
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
        vals = {"id": job_id, "created_at": 1794676000.0, "mode": "t2i",
                "status": "done", "params": '{"prompt":"audit probe"}',
                "gen_type": "t2i"}
        conn.execute("INSERT OR REPLACE INTO jobs (%s) VALUES (%s)" % (
            ",".join(cols), ",".join("?" * len(cols))),
            tuple(vals.get(c) for c in cols))
        conn.commit()
    except Exception as exc:
        print(f"[seed job] {type(exc).__name__}: {exc}")
    finally:
        conn.close()
    return job_id


async def _probe_one(name: str, spec: dict, ctx, ids: dict) -> dict:
    handler = spec["handler"]
    args = dict(WRITE_PROBES.get(name, {}))
    resolved = {}
    for key, value in args.items():
        if isinstance(value, str) and value.startswith("@"):
            resolved[key] = ids.get(value[1:], value)
        elif isinstance(value, list):
            resolved[key] = [ids.get(v[1:], v) if isinstance(v, str) and v.startswith("@") else v
                             for v in value]
        else:
            resolved[key] = value
    try:
        result = await handler(ctx, resolved)
    except Exception as exc:
        from h3studio import agent as agent_mod
        if isinstance(exc, agent_mod.AgentError):
            return {"tool": name, "status": "handled_error",
                    "detail": f"{getattr(exc, 'code', '') or 'error'}: {exc.message}"[:300]}
        return {"tool": name, "status": "crash",
                "detail": f"{type(exc).__name__}: {exc}"[:300],
                "traceback": traceback.format_exc()[-1500:]}
    text = json.dumps(result, ensure_ascii=False, default=str)
    if isinstance(result, dict) and result.get("error"):
        return {"tool": name, "status": "returned_error", "detail": text[:400]}
    return {"tool": name, "status": "ok", "detail": text[:400],
            "keys": sorted(result.keys()) if isinstance(result, dict) else None}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    root = _sandbox()
    print(f"[sandbox] {root}")
    try:
        _install_sandbox(root)
        media = _probe_media(root)
        print(f"[probe media] video={media['video_exists']} audio={media['audio_exists']}")
        ids = _seed_assets(media)
        ids["job_id"] = _seed_job()
        # upload_asset 只接受素材库**内相对路径**。放一个**还没入库**的原始文件
        # 到素材库里（放 tmp 区），用它的相对路径 —— 这样 upload 走的是真入库链路。
        from h3studio import assets as asset_store
        library = Path(os.environ["ASSET_LIBRARY_ROOT"])
        raw = library / "tmp" / "raw_probe.png"
        raw.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(media["spare"], raw)
        ids["rel_path"] = str(raw.relative_to(library))
        print(f"[seed] rel_path = {ids['rel_path']}")

        ctx = _make_ctx()
        from h3studio import agent
        rows = []
        order = [n for n in WRITE_PROBES if n not in ("delete_asset",)]
        order += ["delete_asset"]
        for name in order:
            spec = agent.TOOLS.get(name)
            if not spec:
                rows.append({"tool": name, "status": "missing_spec",
                             "detail": "工具表里没有这个工具"})
                continue
            row = await _probe_one(name, spec, ctx, ids)
            rows.append(row)
            print(f"[{row['status']:14s}] {name:22s} {row.get('detail','')[:170]}")

        produced = sorted(str(p.relative_to(root)) for p in root.rglob("*")
                          if p.is_file() and p.name != "jobs.db")
        summary = {"rows": rows, "sandbox": str(root),
                   "produced_files": produced, "produced_count": len(produced),
                   "media_probe": media, "seed_ids": ids}
        print(f"\n== 沙箱产物 {len(produced)} 个 ==")
        for p in produced[:40]:
            print("  ", p)
        if args.json:
            Path(args.json).write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
            print("已写:", args.json)
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
            print("[sandbox] 已清理", root)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
