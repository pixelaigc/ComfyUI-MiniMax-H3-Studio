# -*- coding: utf-8 -*-
"""核验：新增「收藏」导航项后，375px 高视口下「硬件参数」仍能滚到底看到。

【为什么要单独核验】tests/test_sidebar_fold.py 有一条
「375px 高视口下滚到底后 #navSystem 完整可见」的断言 ——
我在「素材」分组里加了一项「收藏」，**侧栏内容变高了**，
理论上可能把「硬件参数」顶出可视区。

在满负载的全量回归里这条断言报过一次红（单独重跑 3 次都过），
所以这里用**确定性**的方式量一次：直接读 DOM 几何，
不依赖滚动动画与时序。
"""
import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from playwright.sync_api import sync_playwright   # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


port = free_port()
tmp = Path(tempfile.mkdtemp(prefix="fav-sidebar-"))
shutil_dst = tmp
import shutil
shutil.copytree(REPO / "h3studio", tmp / "h3studio")
shutil.copy2(REPO / "server.py", tmp / "server.py")
shutil.copytree(REPO / "web", tmp / "web")
data_dir = tmp / "data"
data_dir.mkdir(parents=True)
conn = sqlite3.connect(data_dir / "jobs.db")
conn.executescript("""
CREATE TABLE jobs (id TEXT PRIMARY KEY, created_at REAL, mode TEXT, params TEXT,
 status TEXT, prompt_id TEXT, progress REAL DEFAULT 0, progress_text TEXT,
 error TEXT, outputs TEXT, started_at REAL, finished_at REAL);
CREATE TABLE job_batches (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, idx INTEGER NOT NULL,
 seed INTEGER, status TEXT, prompt_id TEXT, progress REAL DEFAULT 0,
 progress_text TEXT, error TEXT, outputs TEXT, created_at REAL,
 started_at REAL, finished_at REAL);
""")
now = time.time()
conn.execute(
    "INSERT INTO jobs (id, created_at, mode, params, status, progress,"
    " progress_text, outputs) VALUES ('s1', ?, 't2i', '{}', 'done', 1.0, '', '[]')",
    (now,))
conn.commit()
conn.close()
(tmp / "config.json").write_text(json.dumps({
    "host": "127.0.0.1", "port": port,
    "comfyui_url": "http://127.0.0.1:8188",
    "data_dir": str(data_dir),
    "models_dir": "/home/ubuntu/comfyui/models",
    "poll_interval": 1.5}), encoding="utf-8")

proc = subprocess.Popen([sys.executable, "server.py"], cwd=str(tmp),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(60):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            break
    except OSError:
        time.sleep(0.3)

pw = sync_playwright().start()
browser = pw.chromium.launch(args=["--no-sandbox"])
page = browser.new_page(viewport={"width": 375, "height": 375})
page.goto("http://127.0.0.1:%d/" % port, wait_until="domcontentloaded")
page.wait_for_timeout(2500)
page.evaluate("() => document.getElementById('navToggle').click()")
page.wait_for_timeout(1200)

st = page.evaluate("""() => {
  const el = document.getElementById('navScroll');
  const before = {scrollH: el.scrollHeight, clientH: el.clientHeight};
  el.scrollTop = el.scrollHeight;
  const sys = document.querySelector('.nav-item#navSystem').getBoundingClientRect();
  const fav = document.querySelector('.nav-item#navFavorites').getBoundingClientRect();
  const assets = document.querySelector('.nav-item#navAssets').getBoundingClientRect();
  const er = el.getBoundingClientRect();
  return {
    before, scrollTop: el.scrollTop,
    systemVisible: sys.top >= er.top - 1 && sys.bottom <= er.bottom + 1,
    systemRect: {top: sys.top, bottom: sys.bottom},
    favRect: {top: fav.top, bottom: fav.bottom},
    assetsRect: {top: assets.top, bottom: assets.bottom},
    elRect: {top: er.top, bottom: er.bottom},
    favText: document.querySelector('.nav-item#navFavorites').textContent.trim(),
    favVisible: fav.top >= er.top - 1 && fav.bottom <= er.bottom + 1,
  };
}""")
print(json.dumps(st, ensure_ascii=False, indent=2))
ok = st["systemVisible"] and st["scrollTop"] > 0 and st["before"]["scrollH"] > st["before"]["clientH"]
print("\n375px 高视口下滚到底可见「硬件参数」:", ok)
print("「收藏」项也在可视区内:", st["favVisible"])

browser.close()
pw.stop()
proc.terminate()
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if ok else 1)
