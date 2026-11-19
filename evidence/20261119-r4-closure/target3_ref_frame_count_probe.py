# -*- coding: utf-8 -*-
"""目标3 取证：_record_ref_video_frame_count 失败结果是否缓存 / 是否重复探测。"""
import json, os, shutil, sys, tempfile, asyncio
from pathlib import Path

REPO = Path("/root/projects/h3s-merge-20261119")
sys.path.insert(0, str(REPO))
import server as srv

tmp = tempfile.mkdtemp(prefix="r3probe3-")
srv.DB_PATH = Path(tmp) / "jobs.db"
srv.init_db()

# 造一个真实的「ComfyUI input」文件
inp = Path("/home/ubuntu/comfyui/input/h3studio")
inp.mkdir(parents=True, exist_ok=True)
fake = inp / "r3probe_ref.mp4"
fake.write_bytes(b"FAKE-VIDEO-BYTES" * 100)
print("fake exists:", fake.is_file())

calls = []
def counting_probe(p):
    calls.append(str(p))
    return 0    # 模拟「探测不到帧数」（ffprobe 失败）
srv._probe_video_frame_count = counting_probe

info3 = {"chain_id": "C3", "stage_index": 0, "source_job": "",
         "source_segment": -1, "context_frames": 22,
         "anchor_frames": 22, "chain_total_frames": 0,
         "segment_frames": 124}
p3 = {"prompt": "x", "duration": 5, "continuation": dict(info3),
      "ref_videos": ["h3studio/r3probe_ref.mp4"]}

w = srv.Worker.__new__(srv.Worker)
async def run3():
    for _ in range(3):
        await srv.Worker._record_ref_video_frame_count(w, "JOB3", p3)
asyncio.run(run3())

res = {
    "probe_calls_for_3_invocations": len(calls),
    "calls": calls,
    "ref_video_frame_count_in_params": p3["continuation"].get("ref_video_frame_count"),
    "continuation_keys": sorted(p3["continuation"].keys()),
    "db_row_written": False,
}
with srv.db() as conn:
    row = conn.execute("SELECT params FROM jobs WHERE id=?", ("JOB3",)).fetchone()
    res["db_row_written"] = row is not None

# 对照：探测成功时会被记录吗？
calls.clear()
srv._probe_video_frame_count = lambda p: (calls.append(str(p)), 123)[1]
p4 = {"prompt": "x", "duration": 5, "continuation": dict(info3),
      "ref_videos": ["h3studio/r3probe_ref.mp4"]}
w = srv.Worker.__new__(srv.Worker)
async def run4():
    for _ in range(3):
        await srv.Worker._record_ref_video_frame_count(w, "JOB4", p4)
asyncio.run(run4())
res["success_case_probe_calls"] = len(calls)
res["success_case_value"] = p4["continuation"].get("ref_video_frame_count")

fake.unlink(missing_ok=True)
shutil.rmtree(tmp, ignore_errors=True)
print(json.dumps(res, ensure_ascii=False, indent=2))
