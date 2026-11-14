# -*- coding: utf-8 -*-
"""
A23 浏览器验证脚本（**不是** pytest —— 这是手工/半自动的实机验证）。

【为什么要有这个】
  pytest 覆盖的是「接口返回什么」和「纯逻辑算得对不对」。
  但 A2-1/A2-2/A2-3 说的是**界面上看到什么**：
    * 勾选素材 -> 点「添加到播放器」-> 播放器里能看到这几条
    * 图片模式只有一个「每张停止时间」输入框（不是每张一个）
    * 音频列表/随机模式能自动推进
  这些必须**真在浏览器里点一遍**才算数。

【踩过的坑】
  1. Playwright 的 **Sync API 不能在 asyncio 事件循环里跑**
     （实测报 "It looks like you are using Playwright Sync API inside the
     asyncio loop"）。所以：aiohttp 服务跑在**后台线程**里（自带 loop），
     Playwright sync API 跑在主线程 —— 两条线程各有一个 loop，互不干扰。
  2. 固定端口容易被占（实测 8899 被别的服务占了 -> OSError）。
     所以用 socket 绑 0 让系统分配一个空闲端口。

【怎么跑】
  python3 tests/verify_a23_player_browser.py

【注意】
  用的是**临时素材库**（ASSET_LIBRARY_ROOT 指到临时目录），
  不会碰 /mnt/usb2t 上的真实素材库。
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TMP = Path(tempfile.mkdtemp(prefix="a23-browser-"))
os.environ["ASSET_LIBRARY_ROOT"] = str(TMP / "library")

from aiohttp import web  # noqa: E402
from h3studio import asset_api  # noqa: E402
from h3studio import assets as A  # noqa: E402


def build_app() -> web.Application:
    app = web.Application()
    asset_api.register(app, server_module=None)

    async def index(request):
        return web.FileResponse(REPO / "web" / "index.html")

    app.router.add_get("/", index)
    app.router.add_static("/static", REPO / "web")
    return app


def start_server() -> tuple[str, threading.Thread]:
    """
    在后台线程里起 aiohttp 服务。

    【为什么要单独线程】
    Playwright sync API 要求「当前线程没有正在运行的 asyncio 事件循环」。
    主线程要留给 Playwright，服务只能换一条线程 —— 那条线程自带 loop，
    两边互不干扰。
    """
    ready = threading.Event()
    holder = {}

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app = build_app()
        runner = web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        site = web.TCPSite(runner, "127.0.0.1", port)
        loop.run_until_complete(site.start())
        holder["base"] = f"http://127.0.0.1:{port}"
        holder["loop"] = loop
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(timeout=30)
    return holder["base"], thread


def make_assets(base: str):
    """造 2 张**不同**的图 + 1 段音频，走**真实上传接口**。"""
    from PIL import Image
    # 【为什么要两张不同的图】
    # 素材库按 sha256 去重 —— 传两个字节一样的 png 会被合并成**一个**素材，
    # 于是「勾选 2 张 -> 清单 2 条」的前提就不成立（实测只得到 1 条）。
    pngs = []
    for i, color in enumerate([(200, 60, 60), (60, 200, 90)]):
        path = TMP / f"img{i}.png"
        Image.new("RGB", (64 + i, 48), color).save(path)
        pngs.append(path)
    mp3 = TMP / "b.mp3"
    try:
        from h3studio import media_probe
        subprocess.check_output([
            media_probe.FFMPEG, "-y", "-v", "error", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=1", "-ac", "2",
            "-c:a", "libmp3lame", "-b:a", "128k", str(mp3)],
            stderr=subprocess.STDOUT, timeout=120)
    except Exception as exc:
        print("!! ffmpeg 造 mp3 失败（音频用例会跳过）:", exc)

    def upload(path, mime):
        boundary = "----a23"
        body = (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
                f"Content-Type: {mime}\r\n\r\n").encode()
        body += path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"{base}/api/assets", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())

    image_ids = []
    for path in pngs:
        result = upload(path, "image/png")
        image_ids += [r["asset_id"] for r in result["results"] if r["ok"]]
    audio_id = None
    if mp3.exists() and mp3.stat().st_size > 0:
        result = upload(mp3, "audio/mpeg")
        audio_id = next((r["asset_id"] for r in result["results"] if r["ok"]), None)
    return image_ids, audio_id


def main():
    A.init_db()
    base, _thread = start_server()
    print("服务已起:", base)

    from playwright.sync_api import sync_playwright
    results = {}
    try:
        image_ids, audio_id = make_assets(base)
        print("上传素材: images=%s audio=%s" % (image_ids, audio_id))

        with sync_playwright() as p:
            browser = p.chromium.launch(
                args=["--autoplay-policy=no-user-gesture-required"])
            page = browser.new_page(viewport={"width": 1440, "height": 960})
            errors = []
            page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
            page.on("console", lambda m: errors.append(f"console.error: {m.text}")
                    if m.type == "error" else None)

            page.goto(base, wait_until="networkidle")
            page.wait_for_timeout(1200)

            # ---- 打开素材库 ----
            page.click("#navAssets")
            page.wait_for_timeout(1500)

            # ---- A2-1：勾选前两张 -> 点「添加到播放器」 ----
            boxes = page.query_selector_all("#alGrid .al-card .al-selbox")
            print("网格里的卡片数:", len(boxes))
            boxes[0].click()
            boxes[1].click()
            page.wait_for_timeout(400)
            print("多选条可见:", page.is_visible("#alSelBar"),
                  "| 文案:", page.inner_text("#alSelCount"))

            # 记下**实际勾选**了哪两个素材 id —— 网格按「最新优先」排序，
            # 勾到的可能是 1 图 + 1 音，不能假设前两张都是图。
            selected_ids = page.eval_on_selector_all(
                "#alGrid .al-card .al-selbox",
                "els => els.filter(e => e.checked).map(e => e.dataset.selId)")
            print("勾选的素材 id:", selected_ids)

            page.click("#alSelPlayer")
            page.wait_for_timeout(1200)
            if page.query_selector("#alToast"):
                print("toast:", page.inner_text("#alToast"))

            with urllib.request.urlopen(f"{base}/api/assets/player", timeout=30) as resp:
                state = json.loads(resp.read().decode())
            playlist_ids = [e["asset_id"] for e in state["entries"]]
            print("服务端清单条数:", len(state["entries"]),
                  "| ids:", playlist_ids, "| counts:", state["counts"])
            # 【核心断言】清单里的 id **恰好**等于勾选的 id —— 不多不少。
            results["A2-1 清单只含主动添加的条目"] = (
                sorted(playlist_ids) == sorted(selected_ids) and len(playlist_ids) == 2)

            # ---- 打开播放器 ----
            page.click("#alPlayerBtn")
            page.wait_for_timeout(1500)
            print("播放器 tab 文案:",
                  page.inner_text("#alPlayerTabs").replace("\n", " "))

            # 图片模式只有**一个**停止时间输入框
            top_seconds = page.query_selector_all("#alPSeconds")
            per_item = page.query_selector_all(".al-pitem-sec")
            print("#alPSeconds 个数:", len(top_seconds),
                  "| .al-pitem-sec 个数:", len(per_item))
            results["A2-2 图片模式只有一个停止时间输入框"] = (
                len(top_seconds) == 1 and len(per_item) == 0)

            # 改停止时间 -> 持久化到服务端
            page.fill("#alPSeconds", "3")
            page.dispatch_event("#alPSeconds", "change")
            page.wait_for_timeout(900)
            with urllib.request.urlopen(f"{base}/api/assets/player", timeout=30) as resp:
                state2 = json.loads(resp.read().decode())
            print("改停止时间后服务端 img_seconds:", state2["img_seconds"])
            results["A2-2 停止时间持久化"] = (state2["img_seconds"] == 3.0)

            # ---- A2-3：音频模式 ----
            if audio_id:
                page.click("#alPlayerClose")
                page.wait_for_timeout(300)
                req = urllib.request.Request(
                    f"{base}/api/assets/player/add",
                    data=json.dumps({"asset_ids": [audio_id]}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=30) as resp:
                    json.loads(resp.read().decode())
                page.click("#alPlayerBtn")
                page.wait_for_timeout(1200)
                page.click('.al-ptab[data-mode="audio"]')
                page.wait_for_timeout(900)
                has_media = page.query_selector("#alPMedia") is not None
                print("音频 tab 有媒体元素:", has_media)
                results["A2-3 音频模式可进入"] = has_media
                options = page.eval_on_selector(
                    "#alPMode", "el => Array.from(el.options).map(o => o.value)")
                print("播放模式选项:", options)
                results["A2-3 三种播放模式齐全"] = (
                    set(options) == {"single", "list", "shuffle"})

                # 媒体节点复用：切歌时**不重建** <audio>（重建 = 播放被打断）
                reused = page.evaluate("""() => {
                  const el = window.AssetPlayer.state.mediaEl;
                  const before = el;
                  window.AssetPlayer.render();
                  const after = window.AssetPlayer.state.mediaEl;
                  return before === after;
                }""")
                print("重渲染后媒体节点被复用:", reused)
                results["A2-3 重渲染不重建媒体节点"] = reused

                # 推进逻辑：真跑一次 advanceIndex（浏览器里的那份实现）
                seq = page.evaluate("""() => {
                  const out = [];
                  let cur = 0;
                  for (let k = 0; k < 4; k++) {
                    cur = window.AssetPlayer.advanceIndex(cur, 3, "list");
                    out.push(cur);
                  }
                  const shuf = [];
                  let c = 0;
                  for (let k = 0; k < 5; k++) {
                    c = window.AssetPlayer.advanceIndex(c, 3, "shuffle");
                    shuf.push(c);
                  }
                  return { list: out, shuffle: shuf };
                }""")
                print("浏览器内推进: 列表模式", seq["list"], "| 随机模式", seq["shuffle"])
                results["A2-3 列表模式自动推进并回绕"] = (seq["list"] == [1, 2, 0, 1])
                results["A2-3 随机模式不重复当前"] = all(
                    i != (0 if k == 0 else seq["shuffle"][k - 1])
                    for k, i in enumerate(seq["shuffle"]))
            else:
                print("（没有音频素材，跳过音频用例）")

            page.screenshot(path=str(TMP / "a23-player.png"))
            print("截图:", TMP / "a23-player.png")
            print("页面错误:", errors[:5] if errors else "无")
            browser.close()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    print("\n===== 实测结果 =====")
    for key, value in results.items():
        print(f"{'PASS' if value else 'FAIL'}  {key}")
    ok = all(results.values())
    print("全部通过" if ok else "有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
