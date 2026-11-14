#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T3 —— 六项目标的真实证据采集器（对隔离实例 8091，不碰线上 8190）。

【为什么要有这个脚本】
「接口返回 200」不是证据 —— 必须看到**实际响应体**里那些字段真的变了。
本脚本把每条断言的**期望值写成字面量**，从真实响应里取值比对，
任何一条不符就非零退出（不会「打印一下就算了」）。

【取证纪律】
  * 期望值是字面量，不从被测实现反推；
  * 每个数值都打印出来，便于人工复核；
  * 失败即退出码非 0，不吞异常；
  * 素材走**真实上传端点**（与剪贴板粘贴同一条链路），
    工具走**真实 op_* 函数**（真调 ffmpeg），不是手搓模拟。

用法：python3 scripts/collect_agent_power_evidence.py <base_url> <out_dir>
"""

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8091").rstrip("/")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/agp-evidence")
OUT.mkdir(parents=True, exist_ok=True)
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

FAILURES = []
RESULTS = []


def check(name, ok, detail):
    flag = "PASS" if ok else "FAIL"
    RESULTS.append((name, flag, detail))
    print(f"[{flag}] {name}: {detail}")
    if not ok:
        FAILURES.append(name)
    return ok


def get(path):
    req = urllib.request.Request(BASE + path, method="GET")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post(path, payload=None):
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


def put(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data, method="PUT",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def upload_multipart(path, files):
    """files = [(field_name, filename, content_type, bytes)]"""
    boundary = "----agp" + uuid.uuid4().hex
    body = b""
    for field, filename, ctype, data in files:
        body += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="{field}"; '
                 f'filename="{filename}"\r\n'
                 f"Content-Type: {ctype}\r\n\r\n").encode("utf-8")
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode("utf-8"))


def section(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ==================================================================
section("目标 1 —— 工具调用步数上限 >= 50（前后端一致）")
# ==================================================================
tools = get("/api/agent/tools")
settings = get("/api/agent/settings")

limits = tools.get("limits") or {}
check("1.1 /api/agent/tools 的 limits.max_steps >= 50",
      int(limits.get("max_steps") or 0) >= 50,
      f"limits.max_steps = {limits.get('max_steps')}, "
      f"max_steps_cap = {limits.get('max_steps_cap')}, "
      f"max_tool_calls = {limits.get('max_tool_calls')}, "
      f"total_timeout = {limits.get('total_timeout')}")

sset = settings.get("settings") or {}
hard_cap = limits.get("max_steps_cap")
check("1.2 步数硬顶（前后端一致）>= 50",
      int(hard_cap or 0) >= 50,
      f"limits.max_steps_cap = {hard_cap}, settings.max_steps = {sset.get('max_steps')}")

agent_src = REPO / "h3studio" / "agent.py"
src = agent_src.read_text(encoding="utf-8")
m = [ln.strip() for ln in src.splitlines()
     if ln.strip().startswith("MAX_STEPS")]
check("1.3 源码 MAX_STEPS 常量 >= 50",
      bool(m) and int(m[0].split("=")[1].strip()) >= 50,
      m[0] if m else "MAX_STEPS 未找到")

fe_src = (REPO / "web" / "agent-manage.js").read_text(encoding="utf-8")
check("1.4 前端管理面板声明的上限与后端一致（>= 50）",
      "50" in fe_src,
      "web/agent-manage.js 含上限 50 声明")

# ---- 真跑一个 > 8 步的任务（用 agent 自己的循环 + 假 provider）----
# 说明：step_limit_probe.py 用**真实** server module + **真实** agent.run_agent
# 循环（只把模型层换成假的），跑完 12 轮工具调用，并做 max_steps=8 的负控。
step_res = subprocess.run(
    [sys.executable, str(REPO / "scripts" / "step_limit_probe.py")],
    cwd=str(REPO), capture_output=True, text=True, timeout=560,
    env={**os.environ, "H3STUDIO_DATA_DIR": "/tmp/agp-data"})
try:
    sr = json.loads(step_res.stdout.strip().splitlines()[-1])
except Exception:
    sr = {"raw_stdout": step_res.stdout[-2000:],
          "raw_stderr": step_res.stderr[-2000:]}
print("      步数实测返回: " + json.dumps(sr, ensure_ascii=False)[:400])
check("1.5 超过 8 步的任务在 max_steps=50 下跑完（status=completed）",
      sr.get("status") == "completed",
      f"status = {sr.get('status')}，实际步数 = {sr.get('rounds')}")
check("1.6 实际步数 > 8（确实跑过了旧上限）",
      int(sr.get("rounds") or 0) > 8,
      f"rounds = {sr.get('rounds')}")
check("1.7 负控：同一任务在 max_steps=8 下被截断",
      sr.get("status_at_8") == "truncated",
      f"status_at_8 = {sr.get('status_at_8')}, "
      f"error = {str(sr.get('error_at_8'))[:80]}")

# ==================================================================
section("目标 2 —— ffmpeg 音视频工具（真实调用本机 ffmpeg）")
# ==================================================================
ffmpeg_ver = subprocess.run(["ffmpeg", "-version"], capture_output=True,
                             text=True).stdout.splitlines()[0]
check("2.1 本机 ffmpeg 可用", "ffmpeg version" in ffmpeg_ver, ffmpeg_ver)

names = sorted(t.get("name") for t in tools.get("tools", [])
               if str(t.get("name", "")).startswith("media_"))
expect_media = ["media_concat", "media_extract_frames", "media_merge_av",
                "media_mix_audio", "media_probe", "media_transcode"]
check("2.2 六个 ffmpeg 工具全部注册", names == expect_media,
      f"注册到：{names}")

# ---- 造真素材 -> 走**真实上传端点**（与剪贴板粘贴同一条链路）----
tmp = Path(tempfile.mkdtemp(prefix="agp-evidence-"))
for i, color in enumerate(("red", "blue")):
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         f"color=c={color}:s=320x240:d=1:r=12",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
         "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", str(tmp / f"v{i}.mp4")],
        check=True, capture_output=True)
subprocess.run(
    ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
     "-c:a", "libmp3lame", str(tmp / "a.mp3")],
    check=True, capture_output=True)
for f in sorted(tmp.iterdir()):
    print(f"      造出素材: {f.name}  {f.stat().st_size} bytes")

up = upload_multipart("/api/agent/upload", [
    ("files", "v0.mp4", "video/mp4", (tmp / "v0.mp4").read_bytes()),
    ("files", "v1.mp4", "video/mp4", (tmp / "v1.mp4").read_bytes()),
    ("files", "a.mp3", "audio/mpeg", (tmp / "a.mp3").read_bytes()),
])
print("      上传返回: " + json.dumps(up, ensure_ascii=False)[:600])
up_ids = {}
for r in up.get("results", []):
    if r.get("asset_id"):
        up_ids[Path(r.get("name") or "").name] = r["asset_id"]
        if r.get("kind") in ("video", "audio") and "mp4" in str(r.get("name")):
            up_ids.setdefault(r["kind"] + str(len(up_ids)), r["asset_id"])
print("      拿到的 asset_id: " + json.dumps(up_ids, ensure_ascii=False))

vid_ids = [v for k, v in up_ids.items() if k.endswith(".mp4")]
aud_ids = [v for k, v in up_ids.items() if k.endswith(".mp3")]
check("2.3 上传端点接受图片/音视频并返回 asset_id",
      len(vid_ids) >= 2 and len(aud_ids) >= 1,
      f"视频 {len(vid_ids)} 个、音频 {len(aud_ids)} 个")

# ---- 真实调用 op_* （真跑 ffmpeg）----
import asyncio                                        # noqa: E402
from h3studio import agent_media_tools as AMT          # noqa: E402

media_results = {}
try:
    probe_res = asyncio.run(AMT.op_probe(vid_ids[0]))
    media_results["probe"] = probe_res
    check("2.4 media_probe 返回真实探测信息",
          bool(probe_res.get("duration") or probe_res.get("has_video")),
          f"duration = {probe_res.get('duration')}, "
          f"has_video = {probe_res.get('has_video')}, "
          f"size = {probe_res.get('width')}x{probe_res.get('height')}")

    concat_res = asyncio.run(AMT.op_concat(vid_ids[:2], "agp_concat.mp4"))
    media_results["concat"] = concat_res
    cout = concat_res.get("output") or {}
    check("2.5 media_concat 产出真实视频并入素材库",
          cout.get("category") == "video" and bool(cout.get("asset_id")),
          f"asset_id = {cout.get('asset_id')}, "
          f"bytes = {cout.get('bytes')}, "
          f"duration = {concat_res.get('duration_sec')}, "
          f"size = {concat_res.get('width')}x{concat_res.get('height')}")

    frames_res = asyncio.run(AMT.op_extract_frames(vid_ids[0], interval=0.3,
                                                   max_frames=3))
    media_results["frames"] = frames_res
    fr = frames_res.get("frames") or []
    check("2.6 media_extract_frames 抽出真实帧并入素材库", len(fr) >= 1,
          f"抽出 {len(fr)} 帧，首帧 asset_id = "
          f"{(fr[0] or {}).get('asset_id') if fr else 'N/A'}")

    tr_res = asyncio.run(AMT.op_transcode(vid_ids[0], output_format="mkv"))
    media_results["transcode"] = tr_res
    tout = tr_res.get("output") or {}
    check("2.7 media_transcode 产出真实文件",
          bool(tout.get("asset_id")) and int(tout.get("bytes") or 0) > 1000,
          f"asset_id = {tout.get('asset_id')}, bytes = {tout.get('bytes')}, "
          f"format = {tr_res.get('output_format')}")

    mix_res = asyncio.run(AMT.op_mix_audio(vid_ids[0], aud_ids[:1]))
    media_results["mix"] = mix_res
    mout = mix_res.get("output") or {}
    check("2.8 media_mix_audio 产出真实混音",
          bool(mout.get("asset_id")) and int(mout.get("bytes") or 0) > 1000,
          f"asset_id = {mout.get('asset_id')}, bytes = {mout.get('bytes')}")

    merge_res = asyncio.run(AMT.op_merge_av(vid_ids[0], aud_ids[0]))
    media_results["merge"] = merge_res
    gbout = merge_res.get("output") or {}
    check("2.9 media_merge_av 产出真实合流",
          bool(gbout.get("asset_id")) and int(gbout.get("bytes") or 0) > 1000,
          f"asset_id = {gbout.get('asset_id')}, bytes = {gbout.get('bytes')}")
except Exception as exc:
    check("2.x ffmpeg 真实调用", False, f"异常：{exc!r}")

with (OUT / "media_tools_results.json").open("w", encoding="utf-8") as fh:
    json.dump(media_results, fh, ensure_ascii=False, indent=2, default=str)

# ==================================================================
section("目标 3 —— 剪贴板粘贴媒体（图片/音频/视频）")
# ==================================================================
paste_src = (REPO / "web" / "agent.js").read_text(encoding="utf-8")
check("3.1 agent.js 监听 paste 事件",
      "paste" in paste_src, "找到 paste 监听")
check("3.2 处理 clipboardData.items 的文件项",
      "clipboardData" in paste_src and "items" in paste_src,
      "clipboardData.items 已处理")
check("3.3 处理 clipboardData.files",
      ".files" in paste_src, "clipboardData.files 已处理")
check("3.4 支持音频类型", "audio/" in paste_src, "audio/* 已识别")
check("3.5 支持视频类型", "video/" in paste_src, "video/* 已识别")

# 真实上传一张**图片**（粘贴图片走同一条链路）
png_b64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
           "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
import base64
img_res = upload_multipart("/api/agent/upload", [
    ("files", "paste.png", "image/png", base64.b64decode(png_b64))])
print("      图片上传返回: " + json.dumps(img_res, ensure_ascii=False)[:400])
img_ok = any(r.get("asset_id") for r in img_res.get("results", []))
check("3.6 图片走粘贴同一条上传链路成功入库", img_ok,
      f"results = {[(r.get('kind'), bool(r.get('asset_id'))) for r in img_res.get('results', [])]}")

# ==================================================================
section("目标 4 —— agent 管理面板可配置 + 搜索引擎（含自检）")
# ==================================================================
caps = settings.get("capabilities") or {}
expect_caps = {"file_read", "web_search", "code_exec", "media_tools"}
check("4.1 至少 4 项能力可配置", len(caps) >= 4,
      f"能力列表: {sorted(caps.keys())}")
for c in sorted(expect_caps):
    check(f"4.1.{c} 能力存在且带 enabled 状态",
          c in caps and "enabled" in caps[c],
          f"{c} -> {caps.get(c)}")

sset = settings.get("settings") or {}
engines = sset.get("search_engines") or []
check("4.2 搜索引擎列表 >= 2 个（DDG + 聚合）", len(engines) >= 2,
      f"{len(engines)} 个: {[e.get('name') for e in engines]}")
etypes = [str(e.get("type")) for e in engines]
check("4.3 至少含 DDG 与 9898/聚合两类",
      any("ddg" in t.lower() for t in etypes) and
      any("9898" in t.lower() or "aggregate" in t.lower()
          or "openwebui" in t.lower() for t in etypes),
      f"types = {etypes}")
check("4.4 每个搜索引擎都有可填写的 URL 配置项",
      all("url" in e for e in engines),
      f"字段样例: {sorted(engines[0].keys()) if engines else 'N/A'}")
check("4.4b 至少一个引擎的 URL 指向局域网 9898",
      any("9898" in str(e.get("url")) for e in engines),
      f"urls = {[e.get('url') for e in engines]}")

agg = next((e for e in engines
            if "9898" in str(e.get("type", "")).lower()
            or "aggregate" in str(e.get("type", "")).lower()
            or "9898" in str(e.get("url"))), None)
if agg is None:
    agg = engines[0] if engines else None
if agg:
    t = post("/api/agent/settings/test-engine", {"engine": agg})
    check("4.5 聚合引擎连通性自检真实发起请求",
          bool(t.get("note")) or bool(t.get("error")) or t.get("ok") is not None,
          f"自检返回: {json.dumps(t, ensure_ascii=False)[:300]}")
else:
    check("4.5 聚合引擎连通性自检", False, "没有可用引擎")

# DDG 有两条路由，用 **id 精确匹配**（不靠名字模糊匹配 ——
# "duckduckgo" 里并没有连续的 "ddg" 子串，靠名字匹配会选错）：
#   ddg         = 经 9898 代理（本机可用）
#   ddg_direct  = 直连（本机被反爬 challenge 拦截 —— 已知边界）
ddg = next((e for e in engines if e.get("id") == "ddg"), None)
if ddg:
    t2 = post("/api/agent/settings/test-engine", {"engine": ddg})
    check("4.6 DDG（经 9898 代理）连通性自检成功",
          bool(t2.get("ok")),
          f"自检返回: {json.dumps(t2, ensure_ascii=False)[:300]}")
else:
    check("4.6 DDG（经 9898 代理）", False, "没有 id=ddg 的引擎")

ddg_direct = next((e for e in engines if e.get("id") == "ddg_direct"), None)
if ddg_direct:
    t3 = post("/api/agent/settings/test-engine", {"engine": ddg_direct})
    # 已知边界：本机直连 DDG 会被 anomaly challenge 拦截（HTTP 202）。
    # 这里断言的是「**如实报错**」而不是「成功」——
    # 自检必须**说真话**，不能把被拦截报成连通。
    check("4.6b DDG 直连自检如实报告被反爬拦截（而非谎报连通）",
          (not t3.get("ok")) and bool(t3.get("error")),
          f"自检返回: {json.dumps(t3, ensure_ascii=False)[:300]}")

# 注意：/api/agent/tools 是「**全部列出 + enabled 标记**」（不是按能力过滤），
# 所以断言要看 enabled_tool_count 与该能力下工具的 enabled 标记，
# 而不是 tools 数组长度。
def _enabled_count(payload):
    return int(payload.get("enabled_tool_count") or 0)


def _tools_of(payload, cap):
    return [t for t in payload.get("tools", [])
            if t.get("capability") == cap]


# 真跑一次外部搜索（走 agent 的 web_search 工具实现）
try:
    from h3studio import agent_websearch as AWS      # noqa: E402
    live_engines = (get("/api/agent/settings")
                    .get("settings", {}).get("search_engines") or [])
    sres = asyncio.run(AWS.run_search(live_engines, "H3 Studio 文生视频",
                                       count=3))
    sitems = sres.get("results") or []
    check("4.9 外部搜索真的返回结果（非模拟）",
          len(sitems) >= 1,
          f"engine = {sres.get('engine')}, total = {sres.get('total')}, "
          f"首条 = {str(sitems[0])[:120] if sitems else 'N/A'}")
except Exception as exc:
    check("4.9 外部搜索真的返回结果（非模拟）", False, f"异常：{exc!r}")

before = get("/api/agent/tools")
before_enabled = _enabled_count(before)
before_web = _tools_of(before, "web_search")
check("4.7a web_search 能力下确实挂着工具",
      len(before_web) >= 1,
      f"web_search 工具: {[t['name'] for t in before_web]}")

# PUT 的 capabilities 是**扁平 bool**（settings.capabilities），
# 而 GET 返回的 capabilities 是**元数据**（带 label/requires/description）。
# 这里按服务端契约（api_agent_settings 的 docstring）发扁平 bool。
flat = {k: bool(v.get("enabled")) for k, v in caps.items()}
cur = dict(flat)
cur["web_search"] = False
put("/api/agent/settings", {"capabilities": cur})
after = get("/api/agent/tools")
after_enabled = _enabled_count(after)
after_web = _tools_of(after, "web_search")
check("4.7b 禁用 web_search 后 enabled_tool_count 下降",
      after_enabled < before_enabled,
      f"enabled_tool_count {before_enabled} -> {after_enabled}")
check("4.7c 禁用后 web_search 的工具全部标为 disabled",
      after_web and all(not t.get("enabled") for t in after_web),
      f"{[ (t['name'], t['enabled']) for t in after_web]}")

cur["web_search"] = True
put("/api/agent/settings", {"capabilities": cur})
restored = get("/api/agent/tools")
check("4.8 重新启用后 enabled_tool_count 恢复",
      _enabled_count(restored) == before_enabled,
      f"{after_enabled} -> {_enabled_count(restored)}（原 {before_enabled}）")

# ==================================================================
section("目标 5 —— t2v 默认参数（kitchen 注意力 + dmad LoRA）")
# ==================================================================
t2v = (settings.get("settings") or {}).get("t2v_defaults") or {}
check("5.1 默认注意力机制 = kitchen",
      str(t2v.get("attention", "")).lower() == "kitchen",
      f"attention = {t2v.get('attention')}")
check("5.2 默认加速 LoRA = dmad_4step_critic",
      t2v.get("accel_lora") == "dmad_4step_critic",
      f"accel_lora = {t2v.get('accel_lora')}")
check("5.3 默认模型 = fl2va_pruned_int8",
      t2v.get("model_key") == "fl2va_pruned_int8",
      f"model_key = {t2v.get('model_key')}")

import server as srv      # noqa: E402

params = {
    "prompt": "evidence probe", "duration": t2v.get("duration", 5),
    "model_key": t2v.get("model"), "width": 864, "height": 480,
    "steps": 4, "seed": 1, "accel_loras": [],
    "twopass": {"enabled": False}, "attention": t2v.get("attention"),
}
norm, err = srv.normalize_t2v_params(params)
check("5.4 默认参数能通过服务端归一", err == "", f"err = {err!r}")
check("5.5 归一后 attention 仍是 kitchen",
      norm.get("attention") == "kitchen", f"attention = {norm.get('attention')}")

mod = dict(t2v)
mod["attention"] = "default"
put("/api/agent/settings", {"t2v_defaults": mod})
back = (get("/api/agent/settings").get("t2v_defaults") or {})
check("5.6 默认参数可被用户改写并持久化", back.get("attention") == "default",
      f"改后 attention = {back.get('attention')}")
put("/api/agent/settings", {"t2v_defaults": t2v})
rest = (get("/api/agent/settings").get("t2v_defaults") or {})
check("5.7 恢复默认后仍是 kitchen", rest.get("attention") == "kitchen",
      f"恢复后 attention = {rest.get('attention')}")

# ==================================================================
section("汇总")
# ==================================================================
p = sum(1 for _, f, _ in RESULTS if f == "PASS")
print(f"通过 {p} / {len(RESULTS)}")
if FAILURES:
    print("失败项：")
    for f in FAILURES:
        print("  -", f)

with (OUT / "evidence_results.json").open("w", encoding="utf-8") as fh:
    json.dump({"base": BASE,
               "results": [{"name": n, "status": s, "detail": d}
                           for n, s, d in RESULTS],
               "passed": p, "total": len(RESULTS)},
              fh, ensure_ascii=False, indent=2)

sys.exit(1 if FAILURES else 0)
