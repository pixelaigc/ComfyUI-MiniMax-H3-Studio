#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给已完成的 r2v 接续链补算 `video_drop`（用生产代码同一个函数）。

【为什么需要】
`video_drop` 是**工作流构建期**的判定，不落库 ——
落库的 `ref_videos` 是**请求参数**，工作流层在 video_drop 时会把它清空。
所以要证明「段2 确实没带参考视频」，必须：
  1. 落库观测：`ref_video_frame_count` / `chain_total_frames`
  2. 用**生产代码同一个函数**复算判定（不复制逻辑）

【为什么可以事后补算】
判定只依赖落库的 params（chain_total_frames / ref_video_frame_count /
audio_track_mode），与生成时的随机性无关 ⇒ 事后复算与线上当时的结果一致。

用法：
  python3 tools/r2v_backfill_video_drop.py --json /tmp/r2v_after.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from h3studio import h3_video_modes as hvm  # noqa: E402


def recompute(params: dict) -> dict:
    info = hvm.cont_service.chain_info(params)
    if not info:
        return {"applicable": False, "reason": "no continuation info"}
    slice_info = hvm._continuation_media_slice(params)
    return {
        "applicable": True,
        "video_skip_frames": slice_info.get("video_skip_frames"),
        "video_drop": bool(slice_info.get("video_drop")),
        "chain_total_frames": int(info.get("chain_total_frames") or 0),
        "ref_video_frame_count": int(info.get("ref_video_frame_count") or 0),
        "audio_skip_frames": slice_info.get("skip_frames"),
        "audio_active": slice_info.get("active"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True, help="r2v_chain_runner 的 --json-out 文件")
    ap.add_argument("--db", default="/root/projects/comfyui-minimax-h3-studio/data/jobs.db")
    args = ap.parse_args()

    import sqlite3
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    rows = json.load(open(args.json))
    for r in rows:
        for key, jid in (("seg1_fields", r.get("seg1_job")),
                         ("seg2_fields", r.get("seg2_job")),
                         ("ctrl_fields", r.get("ctrl_job"))):
            if not jid or key not in r:
                continue
            raw = con.execute("SELECT params FROM jobs WHERE id LIKE ?",
                              (jid + "%",)).fetchone()
            if not raw or not raw[0]:
                continue
            params = json.loads(raw[0])
            r[key]["video_drop_recomputed"] = recompute(params)
        print(f"seed {r['seed']}:")
        for key in ("seg1_fields", "seg2_fields", "ctrl_fields"):
            if key not in r:
                continue
            v = r[key].get("video_drop_recomputed") or {}
            print(f"   {key:14} video_drop={v.get('video_drop')} "
                  f"video_skip_frames={v.get('video_skip_frames')} "
                  f"C_n={v.get('chain_total_frames')} "
                  f"ref_frames={v.get('ref_video_frame_count')} "
                  f"audio_active={v.get('audio_active')}")
    con.close()

    with open(args.json, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)
    print(f"\n[写回] {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
