# -*- coding: utf-8 -*-
"""
真实跑通一次文生音乐：prompt -> 增强 -> 结构化风格 + 歌词 -> 出音频。

用**真实浏览器**操作界面（不是直接调 API），走完整条用户路径：
  1. 点左侧「文生音乐」标签
  2. 在「音乐描述」里输入一句 prompt
  3. 点「增强」-> 得到结构化风格 + 歌词
  4. 点「生成」-> 真实提交任务
  5. 轮询任务直到 done，下载产物并核对音频参数（时长/采样率/声道）

产物落在本 worktree 的 data-dev/（**不写线上 8190 的库**）。

运行：python3 tests/verify_music_flow_e2e.py
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from playwright.sync_api import sync_playwright     # noqa: E402

BASE = "http://127.0.0.1:8195"
OUT = Path("/root/.hermes-web-ui/.ekko/workspace/default/"
           "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence")
PROMPT = "一首关于雨夜告别的日语慢歌，钢琴加弦乐，副歌要爆发"


def api(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return json.loads(r.read())


def wait_job(jid, timeout=1800):
    """轮询任务直到 done / failed。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        jobs = api("/api/jobs?limit=40")
        for j in jobs.get("jobs", jobs if isinstance(jobs, list) else []):
            if j.get("id") == jid:
                if j.get("status") in ("done", "failed", "canceled"):
                    return j
                print(f"  [{time.time()-t0:6.1f}s] {j.get('status')} "
                      f"progress={j.get('progress')}")
                break
        time.sleep(5)
    raise TimeoutError(f"任务 {jid} 超时")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    evidence = {"prompt": PROMPT}

    with sync_playwright() as pw:
        br = pw.chromium.launch(args=["--no-sandbox"])
        ctx = br.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
        page.on("console",
                lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto(BASE + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

        # 1) 点左侧「文生音乐」标签
        page.click('.nav-item[data-mode="t2a"]')
        page.wait_for_timeout(1200)
        print("[1] 已切到文生音乐标签")

        # 2) 输入 prompt
        page.fill("#musicPrompt", PROMPT)
        page.wait_for_timeout(300)
        print(f"[2] 已输入描述: {PROMPT}")

        # 3) 点「增强」-> 结构化风格 + 歌词
        page.click("#musicEnhanceBtn")
        print("[3] 等待增强（本地模型出结构化风格 + 歌词）…")
        page.wait_for_function(
            "() => { const s = document.querySelector('#musicSF_language');"
            " return s && s.value && s.value !== ''; }",
            timeout=300_000)
        page.wait_for_timeout(800)

        style_fields = page.evaluate(
            "() => window.MusicUI.getStyleFields()")
        lyrics = page.evaluate("() => window.MusicUI.getLyrics()")
        print("[3] 增强完成：")
        print("    style =", json.dumps(style_fields, ensure_ascii=False))
        print("    lyrics 前 120 字 =", lyrics[:120].replace("\n", " / "))
        evidence["style_fields"] = style_fields
        evidence["lyrics"] = lyrics

        # 4) 点「生成」
        page.click("#genBtn")
        page.wait_for_timeout(2500)
        print("[4] 已点「生成」")

        # 找到新任务
        jobs = api("/api/jobs?limit=20")
        lst = jobs.get("jobs", jobs if isinstance(jobs, list) else [])
        music_jobs = [j for j in lst if j.get("mode") == "t2a"]
        if not music_jobs:
            raise RuntimeError("没找到文生音乐任务")
        jid = music_jobs[0]["id"]
        print(f"    任务 id = {jid}")

        br.close()

    # 5) 等任务完成
    print("[5] 等待生成完成…")
    job = wait_job(jid)
    evidence["job"] = {"id": job.get("id"), "status": job.get("status"),
                       "elapsed": job.get("elapsed"),
                       "outputs": job.get("outputs"),
                       "error": job.get("error")}
    print(f"    状态 = {job.get('status')}  耗时 = {job.get('elapsed')}s")
    print(f"    产物 = {json.dumps(job.get('outputs'), ensure_ascii=False)}")
    if job.get("status") != "done":
        raise RuntimeError(f"任务失败: {job.get('error')}")

    # 6) 下载产物并核对音频参数
    out = job["outputs"][0]
    url = BASE + "/files/" + "/".join(
        urllib.parse.quote(p) for p in out["file"].split("/"))
    dest = OUT / "music-e2e-output.flac"
    with urllib.request.urlopen(url, timeout=300) as r:
        dest.write_bytes(r.read())
    evidence["output_path"] = str(dest)
    evidence["output_bytes"] = dest.stat().st_size
    print(f"[6] 产物已下载: {dest} ({dest.stat().st_size} bytes)")

    # ffprobe 核对
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=sample_rate,channels,codec_name",
         "-of", "json", str(dest)],
        capture_output=True, text=True)
    meta = json.loads(probe.stdout or "{}")
    evidence["probe"] = meta
    print("[6] ffprobe:", json.dumps(meta, ensure_ascii=False))

    # sha256
    sha = subprocess.run(["sha256sum", str(dest)], capture_output=True, text=True)
    evidence["sha256"] = sha.stdout.split()[0] if sha.stdout else ""
    print("[6] sha256:", evidence["sha256"])

    evidence["console_errors"] = errors
    (OUT / "music-e2e.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n证据已写入:", OUT / "music-e2e.json")
    return 0


if __name__ == "__main__":
    import urllib.parse     # noqa: E402  (放在这里避免顶部未用告警)
    sys.exit(main())
