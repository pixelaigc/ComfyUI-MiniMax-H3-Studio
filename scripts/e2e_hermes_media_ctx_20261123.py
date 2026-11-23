# -*- coding: utf-8 -*-
"""
端到端实测 · 媒体上下文一致性（A/B/C/D）—— **短上下文版**。

【与上一版 E2E 的区别】
上一版等 hermes **完整跑完**（视频 case 实际跑了 28 分钟，因为 hermes
真的去 ffmpeg 探视频了）—— 那既超出脚本超时，也没法批量复跑。

本版改成：**短超时 + 抓 stdout**。
  * hermes 调用超时压到 240s（可配置）
  * wrapper 同时落地 query-file **与** hermes 的 stdout/stderr
  * 「hermes 已读媒体」的直接证据 = stdout 里的**工具调用痕迹**
    （terminal / ffprobe / vision_analyze 的参数里出现素材路径）
  * 即使超时，也**照样**产出证据（query 原文 + 工具调用记录）——
    超时本身也是「hermes 在花时间读媒体」的证据

【实测口径】
  * 隔离素材库（ASSET_LIBRARY_ROOT 指向临时目录）—— 不碰用户媒体库
  * 独立端口 + 独立临时 sqlite（StudioServer 自带）
  * 真实 hermes CLI（profile=media）+ 真实 VLM（127.0.0.1:5000）
  * 参考图 / 视频 / 音频都是当场生成的合成素材
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

OUT = Path("/root/.hermes-web-ui/.ekko/workspace/default/"
           "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/"
           "evidence-hermes-media-ctx-20261123")
if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir(parents=True, exist_ok=True)

LIB = Path(tempfile.mkdtemp(prefix="h3studio-media-lib-"))
os.environ["ASSET_LIBRARY_ROOT"] = str(LIB)

QUERY_DUMP = OUT / "queries"
QUERY_DUMP.mkdir(parents=True, exist_ok=True)
STDOUT_DUMP = OUT / "stdout"
STDOUT_DUMP.mkdir(parents=True, exist_ok=True)

REAL_HERMES = Path("/usr/local/bin/hermes")
WRAP_DIR = Path(tempfile.mkdtemp(prefix="h3studio-hermes-wrap-"))
WRAPPER = WRAP_DIR / "hermes"
# wrapper：落地 query-file + stdout/stderr，再 exec 真 hermes
WRAPPER.write_text(f"""#!/bin/bash
QF=""
prev=""
for a in "$@"; do
  if [ "$prev" = "--query-file" ]; then QF="$a"; fi
  prev="$a"
done
N=$(ls -1 "{QUERY_DUMP}"/q-*.txt 2>/dev/null | wc -l)
IDX=$((N+1))
if [ -n "$QF" ] && [ -f "$QF" ]; then
  cp "$QF" "{QUERY_DUMP}/q-$IDX.txt"
  echo "$QF" > "{QUERY_DUMP}/q-$IDX.path"
fi
# 【关键】wrapper 必须用 `tee` 而不是 `>` 重定向 —— 服务端
# `_run_cli` 用的是 `capture_output=True`，它捕获的是 **wrapper 的 stdout**。
# 如果 wrapper 把 hermes 的 stdout 重定向到文件，服务端就收到**空输出**
# -> _extract_answer 解析失败 -> 502（实测踩到：rc=0、文件里有完整答案，
# 但服务端说「hermes 返回空内容」）。
"{REAL_HERMES}" "$@" 2> >(tee "{STDOUT_DUMP}/e-$IDX.txt" >&2) | tee "{STDOUT_DUMP}/o-$IDX.txt"
RC=${{PIPESTATUS[0]}}
echo "$RC" > "{STDOUT_DUMP}/o-$IDX.rc"
exit $RC
""", encoding="utf-8")
WRAPPER.chmod(0o755)


def make_probe_media():
    """当场生成参考图 / 参考视频 / 参考音频（不引用用户素材）。"""
    import numpy as np
    from PIL import Image, ImageDraw
    import soundfile as sf

    paths = {}
    img = Image.new("RGB", (768, 512), (32, 40, 60))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 370, 768, 512], fill=(104, 68, 42))
    d.rectangle([70, 230, 215, 370], fill=(214, 210, 200))
    d.rectangle([220, 230, 350, 370], fill=(58, 92, 162))
    d.ellipse([420, 268, 528, 372], fill=(238, 238, 242))
    d.ellipse([443, 282, 505, 312], fill=(126, 92, 68))
    d.rectangle([596, 196, 660, 370], fill=(72, 142, 92))
    d.ellipse([604, 166, 652, 212], fill=(198, 84, 86))
    d.polygon([(600, 150), (690, 150), (668, 40), (622, 40)], fill=(240, 236, 214))
    p1 = LIB / "probe_desk.png"
    img.save(p1)
    paths["image"] = p1

    vdir = LIB / "probe_vid_src"
    vdir.mkdir(parents=True, exist_ok=True)
    for i in range(30):
        fr = Image.new("RGB", (320, 240), (20 + i * 3, 30, 60))
        dd = ImageDraw.Draw(fr)
        dd.rectangle([0, 180, 320, 240], fill=(90, 60, 40))
        dd.rectangle([20 + i * 8, 100, 70 + i * 8, 180], fill=(220, 180, 90))
        fr.save(vdir / f"f{i:04d}.png")
    vid = LIB / "probe_clip.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-framerate", "10",
         "-i", str(vdir / "f%04d.png"), "-c:v", "libx264", "-pix_fmt", "yuv420p",
         str(vid)], check=True)
    paths["video"] = vid

    sr = 22050
    dur = 12.0
    t = np.linspace(0, dur, int(sr * dur), endpoint=False)
    sig = np.zeros_like(t)
    for i in range(int(dur / 0.5)):
        st = int(i * 0.5 * sr)
        ln = int(0.06 * sr)
        if st + ln < len(sig):
            env = np.exp(-np.linspace(0, 6, ln))
            sig[st:st + ln] += 0.5 * env * np.sin(2 * np.pi * 1800 * t[st:st + ln])
    for f, amp in ((110.0, 0.18), (130.81, 0.14), (164.81, 0.12)):
        sig += amp * np.sin(2 * np.pi * f * t)
    sig[len(sig) // 2:] *= 1.8
    sig = np.clip(sig, -0.99, 0.99)
    aud = LIB / "probe_bgm.wav"
    sf.write(aud, sig, sr)
    paths["audio"] = aud
    return paths


def seed_asset_library(media_paths):
    from h3studio import assets as A
    from h3studio import asset_service
    report = A.init_db()
    if not report.get("ok"):
        raise RuntimeError(f"隔离素材库初始化失败：{report}")
    conn = A.connect()
    out = {}
    try:
        specs = [
            ("image", media_paths["image"], "image/png", "image", "E2E 媒体探针图"),
            ("video", media_paths["video"], "video/mp4", "video", "E2E 媒体探针视频"),
            ("audio", media_paths["audio"], "audio/wav", "audio", "E2E 媒体探针音频"),
        ]
        for kind, path, mime, cat, name in specs:
            res = asset_service.register_asset(
                conn, source_path=Path(path), filename=Path(path).name,
                mime=mime, name=name, category=cat,
                note="e2e media ctx probe", source="upload")
            conn.commit()
            out[kind] = res["asset"]["id"]
    finally:
        conn.close()
    return out


CASES = [
    ("image", "t2i", "桌面静物场景", ["image"], 900),
    ("video", "t2v", "镜头缓缓扫过桌面上的静物", ["image", "video"], 900),
    ("audio", "t2a", "一段温暖的钢琴曲", ["image", "audio"], 900),
]

# 「hermes 真的去读了媒体」的工具调用痕迹关键词
MEDIA_READ_MARKERS = ("ffprobe", "ffmpeg", "vision_analyze", "probe_desk",
                      "probe_clip", "probe_bgm", "probe_desk.png")


def scan_stdout_for_media_reads(text: str) -> list[str]:
    """从 hermes stdout 里找「它去读了媒体」的痕迹（工具调用参数含素材路径）。"""
    hits = []
    for line in (text or "").split("\n"):
        low = line.lower()
        if any(m in low for m in MEDIA_READ_MARKERS):
            s = line.strip()
            if s and s not in hits:
                hits.append(s[:300])
    return hits[:20]


async def main():
    from test_frontend_defaults import StudioServer

    media_paths = make_probe_media()
    asset_ids = seed_asset_library(media_paths)
    print(f"隔离素材库: {LIB}")
    for k, v in asset_ids.items():
        print(f"  {k}: {v}")

    srv = StudioServer()
    srv.start()
    base = f"http://127.0.0.1:{srv.port}"

    evidence = {
        "port": srv.port, "library": str(LIB), "assets": asset_ids,
        "media_paths": {k: str(v) for k, v in media_paths.items()},
        "wrapper": str(WRAPPER), "cases": [],
    }
    try:
        import urllib.error
        import urllib.request

        def post(path, payload, timeout=900):
            req = urllib.request.Request(
                f"{base}{path}", data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

        def get(path, timeout=120):
            with urllib.request.urlopen(f"{base}{path}", timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))

        # ---- 把 hermes binary 指到 wrapper + 压低超时 ----
        put_payload = {"hermes": {"enabled": True, "binary": str(WRAPPER),
                                  "timeout": 900}}
        req = urllib.request.Request(
            f"{base}/api/enhancer", data=json.dumps(put_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="PUT")
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()
        cfg = get("/api/enhancer")
        evidence["hermes_config"] = {
            "enabled": cfg["hermes"]["enabled"],
            "profile": cfg["hermes"]["profile"],
            "binary": cfg["hermes"].get("binary"),
            "timeout": cfg["hermes"].get("timeout"),
        }
        print(f"\nhermes: profile={cfg['hermes']['profile']} "
              f"timeout={cfg['hermes'].get('timeout')}s "
              f"binary={cfg['hermes'].get('binary')}")
        assert cfg["hermes"].get("binary") == str(WRAPPER)

        for media, mode, prompt, kinds, tmo in CASES:
            print(f"\n=== [{media}] 媒体上下文增强 ===")
            before = set(p.name for p in QUERY_DUMP.glob("q-*.txt"))
            refs = [{"asset_id": asset_ids[k],
                     "slot": "ref_image" if k == "image" else k} for k in kinds]
            t0 = time.time()
            try:
                status, body = post("/api/hermes-enhance", {
                    "prompt": prompt, "media": media, "mode": "replace",
                    "asset_refs": refs}, timeout=tmo + 180)
            except Exception as exc:
                status, body = 0, {"error": f"{type(exc).__name__}: {exc}"}
            elapsed = round(time.time() - t0, 1)
            after = sorted(set(p.name for p in QUERY_DUMP.glob("q-*.txt")) - before)
            rec = {
                "media": media, "mode": mode, "status": status,
                "elapsed_sec": elapsed, "engine": body.get("engine"),
                "stage": body.get("stage"), "profile": body.get("profile"),
                "command": body.get("command"),
                "enhanced_len": len(body.get("enhanced") or ""),
                "scan": body.get("scan"), "error": body.get("error"),
                "code": body.get("code"), "query_files": after,
            }
            print(f"  http={status} {elapsed}s "
                  f"stage={body.get('stage')} len={rec['enhanced_len']}")
            if status == 200 and body.get("enhanced"):
                (OUT / f"enhanced-{media}.txt").write_text(
                    body["enhanced"], encoding="utf-8")
                rec["enhanced_head"] = body["enhanced"][:400]

            # ---- query 断言（A/B/D 的核心证据）----
            queries = []
            for qf in after:
                text = (QUERY_DUMP / qf).read_text(encoding="utf-8")
                idx = qf.split("-")[-1].split(".")[0]
                stdout_p = STDOUT_DUMP / f"o-{idx}.txt"
                stdout = stdout_p.read_text(encoding="utf-8",
                                            errors="ignore") if stdout_p.exists() else ""
                media_reads = scan_stdout_for_media_reads(stdout)
                q = {
                    "file": qf, "len": len(text),
                    "has_media_block": "# 素材位置" in text,
                    "has_ref_image": "  - 参考图1:" in text,
                    "has_ref_video": "  - 参考视频1:" in text,
                    "has_ref_audio": "  - 参考音频1:" in text,
                    "has_forbid_read_image": "不要用 vision_analyze 读参考图" in text,
                    "has_audio_numeric": "音频数值特征" in text,
                    "has_bpm": "BPM" in text,
                    "has_base64": "base64" in text or "data:image" in text,
                    "stdout_len": len(stdout),
                    "hermes_media_reads": media_reads,
                }
                q["evidence_lines"] = [
                    ln for ln in text.split("\n")
                    if ("参考图" in ln or "参考视频" in ln or "参考音频" in ln
                        or "音频数值特征" in ln or "BPM" in ln
                        or "调式" in ln or "段落" in ln)][:14]
                queries.append(q)
                print(f"  [{qf}] 位置块={q['has_media_block']} "
                      f"图={q['has_ref_image']} 视频={q['has_ref_video']} "
                      f"音频={q['has_ref_audio']} 禁读图={q['has_forbid_read_image']} "
                      f"数值={q['has_audio_numeric']} base64={q['has_base64']} "
                      f"stdout={q['stdout_len']}B 读媒体={len(media_reads)}处")
            rec["queries"] = queries
            evidence["cases"].append(rec)

        # ---- 落点验证 ----
        print("\n=== 落点验证 ===")
        from h3studio import music_yue2
        from h3studio import workflows
        WF_CASES = [
            ("t2i", "image", {"job_id": "e2e_mc_img", "width": 1024, "height": 1024,
                              "seed": 1, "steps": 8, "cfg": 1.0,
                              "model": "z_image_turbo_bf16"}),
            ("t2v", "video", {"job_id": "e2e_mc_vid", "width": 1280, "height": 720,
                              "seed": 1, "steps": 20, "cfg": 1.0, "shift": 5.0,
                              "model_key": "lora", "duration": 5}),
            ("t2a", "audio", {"job_id": "e2e_mc_aud",
                              "checkpoint": "yue2_3b_bf16", "seed": 1}),
        ]
        lands = []
        for mode, media, params in WF_CASES:
            src = OUT / f"enhanced-{media}.txt"
            if not src.exists():
                lands.append({"mode": mode, "ok": False,
                              "reason": "无增强产物（该能力本轮未产出）"})
                continue
            prompt = src.read_text(encoding="utf-8").strip()
            p = dict(params)
            p["prompt"] = prompt
            try:
                wf = (music_yue2.build_t2a(p) if mode == "t2a"
                      else workflows.BUILDERS[mode](p))
            except Exception as exc:
                lands.append({"mode": mode, "ok": False,
                              "reason": f"{type(exc).__name__}: {exc}"})
                continue
            found = []
            probe = prompt[:60]
            for nid, node in (wf or {}).items():
                for key, val in (node.get("inputs") or {}).items():
                    if isinstance(val, str) and probe and probe in val:
                        found.append({"node": nid, "input": key, "len": len(val)})
            lands.append({"mode": mode, "ok": bool(found), "nodes": found,
                          "prompt_len": len(prompt)})
            print(f"  {mode}: {'OK' if found else 'MISS'} {found}")
        evidence["workflow_landing"] = lands

        (OUT / "evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n证据: {OUT}")
    finally:
        srv.stop()
        print(f"隔离素材库: {LIB}")


if __name__ == "__main__":
    asyncio.run(main())
