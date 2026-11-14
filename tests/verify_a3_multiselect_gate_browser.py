#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A3 的**浏览器**验证：素材库多选必须由「多选」按钮触发。

【为什么必须用真浏览器】「复选框默认不显示、点了按钮才出现」是**渲染**层的事
—— 单元测试只能验源码里有那个分支，验不了「用户看到的卡片上有没有复选框」。
这里起真 server.py（假 ComfyUI，不占 GPU）+ 真 chromium，
进素材库页 -> 数卡片里的复选框 -> 点「多选」-> 再数。

【要锁的四条】
  1. 默认（未点按钮）：卡片里**一个**复选框都没有；
  2. 点「多选」按钮后：每张卡片都出现复选框；
  3. 按钮本身有可见的状态变化（文案 / aria-pressed）；
  4. 勾选状态一致：勾 N 张 -> 「已选 N 项」计数一致。

运行：python3 tests/verify_a3_multiselect_gate_browser.py
"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:
    sync_playwright = None
    HAS_PLAYWRIGHT = False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    if not HAS_PLAYWRIGHT:
        print("SKIP：未安装 playwright")
        return 0

    failures = []

    def check(name, cond, detail=""):
        status = "PASS" if cond else "FAIL"
        print(f"[{status}] {name}" + (f"  —— {detail}" if detail else ""))
        if not cond:
            failures.append(name)

    # ---- 素材库库（沙箱）----
    library = Path(tempfile.mkdtemp(prefix="h3-a3-library-"))
    os.environ["ASSET_LIBRARY_ROOT"] = str(library)

    tmp = Path(tempfile.mkdtemp(prefix="h3-a3-browser-"))
    data_dir = tmp / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    port = free_port()
    cfg = {"host": "127.0.0.1", "port": port,
           "comfyui_url": "http://127.0.0.1:9",  # 不会被调用
           "data_dir": str(data_dir), "models_dir": "",
           "poll_interval": 1.5}
    (tmp / "config.json").write_text(json.dumps(cfg, ensure_ascii=False),
                                     encoding="utf-8")
    shutil.copytree(REPO / "h3studio", tmp / "h3studio")
    shutil.copy2(REPO / "server.py", tmp / "server.py")
    shutil.copytree(REPO / "web", tmp / "web")

    # ---- 往素材库里塞几张真素材（服务端会渲染成卡片）----
    from h3studio import assets as A
    from h3studio import asset_service
    # 【为什么要 3 个**不同内容**的文件】素材库按 sha256 去重 ——
    # 同一张图入库 3 次只会得到 1 条记录（实测），卡片就只有 1 张。
    pngs = []
    for i in range(3):
        png = tmp / f"seed{i}.png"
        # 每张图内容必须不同（颜色不同）—— 否则 sha256 一样，
        # 素材库按内容去重 -> 只入库 1 条 -> 卡片只有 1 张。
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                        f"color=c=0x{i * 40 + 10:06x}:s=64x64:d=1",
                        "-frames:v", "1", str(png)],
                       capture_output=True, check=False)
        pngs.append(png)
    A.ensure_layout(library)
    A.init_db()
    ids = []
    with A.connect() as conn:
        for i, png in enumerate(pngs):
            rec = asset_service.register_asset(
                conn, source_path=png, filename=f"seed{i}.png",
                category="image", note="a3 多选验证")
            ids.append((rec.get("asset") or {}).get("id"))
    print(f"[seed] 素材库 {len(ids)} 条：{ids}")

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["ASSET_LIBRARY_ROOT"] = str(library)
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=str(tmp),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=env)
    pw = browser = None
    try:
        deadline = time.time() + 40
        up = False
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    up = True
                    break
            except OSError:
                time.sleep(0.2)
        check("服务启动", up, f"端口 {port}")
        if not up:
            return 1

        pw = sync_playwright().start()
        browser = pw.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")

        # 进素材库页
        try:
            page.click("#navAssets", timeout=15000)
        except Exception:
            pass
        try:
            page.wait_for_selector("#alGrid .al-card", timeout=20000)
        except Exception:
            pass
        time.sleep(1.5)

        cards = page.query_selector_all("#alGrid .al-card")
        check("素材卡片已渲染", len(cards) >= 3, f"{len(cards)} 张卡")

        # ① 默认：复选框一个都没有
        boxes_before = page.query_selector_all("#alGrid .al-selbox")
        check("默认不显示复选框", len(boxes_before) == 0,
              f"默认有 {len(boxes_before)} 个复选框（应为 0）")

        # 按钮存在
        btn = page.query_selector("#alMultiToggle")
        check("存在「多选」按钮", btn is not None,
              "缺 #alMultiToggle")
        if btn is None:
            return 1
        label_before = (btn.inner_text() or "").strip()
        pressed_before = btn.get_attribute("aria-pressed")

        # ② 点「多选」-> 复选框出现
        btn.click()
        time.sleep(1.2)
        boxes_after = page.query_selector_all("#alGrid .al-selbox")
        check("点按钮后卡片出现复选框", len(boxes_after) >= 3,
              f"点后有 {len(boxes_after)} 个复选框")

        # ③ 按钮状态变化
        label_after = (btn.inner_text() or "").strip()
        pressed_after = btn.get_attribute("aria-pressed")
        check("按钮状态有变化", label_after != label_before
              or pressed_after != pressed_before,
              f"文案 {label_before!r} -> {label_after!r}；"
              f"aria-pressed {pressed_before!r} -> {pressed_after!r}")

        # ④ 勾选一致
        for box in boxes_after[:2]:
            try:
                box.click()
            except Exception:
                pass
        time.sleep(0.8)
        count_text = (page.inner_text("#alSelCount") or "").strip() if page.query_selector("#alSelCount") else ""
        checked = page.eval_on_selector_all(
            "#alGrid .al-selbox",
            "els => els.filter(e => e.checked).length")
        check("勾选状态一致", checked == 2 and "2" in count_text,
              f"DOM 勾了 {checked} 个，选择条写着 {count_text!r}")

        # ⑤ 再点一次 -> 复选框消失（退出多选模式）
        page.query_selector("#alMultiToggle").click()
        time.sleep(1.2)
        boxes_off = page.query_selector_all("#alGrid .al-selbox")
        check("退出多选后复选框消失", len(boxes_off) == 0,
              f"退出后还有 {len(boxes_off)} 个复选框")

        return 1 if failures else 0
    finally:
        if browser:
            browser.close()
        if pw:
            pw.stop()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(library, ignore_errors=True)
        print("\n== 汇总 ==")
        print(f"  FAIL: {len(failures)}")
        for name in failures:
            print("   -", name)


if __name__ == "__main__":
    sys.exit(main())
