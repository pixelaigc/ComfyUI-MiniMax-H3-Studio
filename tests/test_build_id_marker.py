# -*- coding: utf-8 -*-
"""构建版本标识 —— 回归守卫（2026-11-21）。

【为什么需要这条】
前一轮用户报「选完生成类型回不去 / 看不到媒体库」，
我在 8190 上怎么测都复现不了。**真正的卡点不是「测不出来」，
而是「无法确认用户手机上的页面是不是当前这一版」** ——
没有版本标识时，排查只能靠猜，替代解释全都无法证伪。

所以这次把「版本可见」做成了**可测的产品能力**，并加上守卫：
  · 接口存在且返回真实 commit（不是硬编码）
  · UI 上有一行可见的版本号
  · 位置在侧栏底部（滚动区外 ⇒ 矮视口也能看到）
  · **不新增导航分组**（7 个顶层分组的契约不得被破坏）
  · 接口挂掉时降级为「—」，**不影响页面其它功能**

【为什么这几条都要守】
  · 「接口存在」—— 没有它，UI 永远显示「—」，等于没做
  · 「返回真实 commit」—— 硬编码一个字符串最省事，但那等于假信息，
    比没有还糟（用户会拿它去对照，反而更困惑）
  · 「滚动区外」—— 放进 #navScroll 的话，375×667 这种矮视口
    用户得先滚动才看得到，恰好在最需要确认的时候看不到
  · 「不新增导航分组」—— 上一轮刚把「收藏」塞进「素材」组，
    再加第 8 个分组会破坏既有契约
  · 「降级不报错」—— 这只是一行小字，不能成为故障源
"""
import json
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def live_server():
    port = _free_port()
    tmp = Path(f"/tmp/h3s-buildid-{port}")
    tmp.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["H3STUDIO_PORT"] = str(port)
    env["PYTHONPATH"] = str(REPO)
    cfg = REPO / "config.json"
    if cfg.exists():
        c = json.loads(cfg.read_text(encoding="utf-8"))
        c["data_dir"] = str(tmp)
        (tmp / "config.json").write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=str(REPO), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.25)
    else:
        proc.kill()
        pytest.skip("h3studio 实例起不来")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(tmp, ignore_errors=True)


import os  # noqa: E402  （放在 fixture 之后是因为只有这里用得到）


# ---------------------------------------------------------------- 接口

def test_version_endpoint_returns_real_commit(live_server):
    """GET /api/version 必须返回**真实**的 commit —— 不是硬编码字符串。"""
    import urllib.request
    with urllib.request.urlopen(live_server + "/api/version", timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    assert data.get("commit"), f"commit 为空: {data}"
    assert data["commit"] != "unknown", f"拿不到 git commit: {data}"
    # 必须是短哈希形状（7~12 位十六进制）
    assert re.fullmatch(r"[0-9a-f]{7,12}", data["commit"]), \
        f"commit 不像短哈希: {data['commit']}"
    # 真实 commit 应该就是当前 worktree 的 HEAD
    head = subprocess.check_output(
        ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
        encoding="utf-8", timeout=10).strip()
    assert data["commit"] == head, \
        f"接口返回的 commit {data['commit']} != worktree HEAD {head}"
    assert data.get("assets_id"), "assets_id 为空"
    assert data.get("built_at"), "built_at 为空"


def test_assets_fingerprint_changes_when_any_version_changes():
    """指纹必须对**任何**静态资源的 ?v= 变化敏感。

    【为什么】commit 变了但某个 ?v= 没变 ⇒ 浏览器可能还在用旧文件。
    指纹就是用来发现「资源清单变了但 commit 没变」这类异常的。
    """
    from h3studio import build_info as b
    base = {"a.js": "v1", "b.css": "v2"}
    f1 = b.assets_fingerprint(base)
    f2 = b.assets_fingerprint({"a.js": "v1", "b.css": "v3"})
    f3 = b.assets_fingerprint({"a.js": "v1", "b.css": "v2"})
    assert f1 != f2, "改了 ?v= 指纹却没变 ⇒ 指纹对版本变化不敏感"
    assert f1 == f3, "同样的清单指纹却不同 ⇒ 指纹不稳定"
    assert len(f1) == 8, f"指纹长度应为 8: {f1}"


def test_assets_fingerprint_is_not_a_version_number():
    """指纹不能是「某个版本号」。

    【实测踩过的坑】第一版取 max(versions) 当指纹，得到 ``vparams7``
    —— 一个 2026-07 的老版本号，看起来像「新版本」，毫无指示意义。
    """
    from h3studio import build_info as b
    fp = b.assets_fingerprint({"x.js": "vparams7", "y.js": "mobnav20261121a"})
    assert fp not in ("vparams7", "mobnav20261121a"), \
        f"指纹直接用了版本号: {fp}"


# ---------------------------------------------------------------- UI

def test_version_marker_exists_in_dom():
    """index.html 里必须有版本号的 DOM 落点。"""
    html = (REPO / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="buildId"' in html, "index.html 里没有 #buildId"
    assert 'id="buildIdValue"' in html, "index.html 里没有 #buildIdValue"


def test_version_marker_is_visible_without_any_interaction():
    """版本号必须在**顶栏**里 —— 不需要点任何东西就能看到。

    【为什么不能在侧栏底部】
    侧栏在窄屏是**抽屉**（默认收起、translateX(-100%)），
    放那儿用户得先点汉堡才看得到 ——
    而「确认自己跑的是哪一版」恰恰应该**不用任何交互**就能看到。
    【实测踩过的坑】第一版放在 .sidebar-foot，
    375×667 下它的 rect.left = **-250**（整个在视口外）⇒ 根本看不见。
    """
    html = (REPO / "web" / "index.html").read_text(encoding="utf-8")
    head = html[:html.find("</header>")]
    assert 'id="buildId"' in head, "版本号不在顶栏（</header> 之前）"
    # 且不能在侧栏里再放一份（避免两处重复显示）
    body = html[html.find("</header>"):]
    assert 'id="buildId"' not in body, "版本号在顶栏之外还有第二份（重复显示）"


def test_version_marker_does_not_add_nav_group():
    """版本号**不得**新增左侧顶层导航分组。

    【既有契约】左侧顶层必须恰好 7 个分组：
      video / image / image-edit / music / assets / agent / system
    ⇒ 版本号只能放在 .sidebar-foot 里，不能是 .nav-group / .nav-item。
    """
    html = (REPO / "web" / "index.html").read_text(encoding="utf-8")
    foot = html.find('class="sidebar-foot"')
    assert foot > 0
    block = html[max(0, foot - 400): foot + 1200]
    assert 'class="nav-group"' not in block, "版本号区块里出现了 .nav-group"
    assert 'class="nav-item"' not in block, "版本号区块里出现了 .nav-item"
    # 顶层分组总数仍必须是 7
    groups = re.findall(r'<button[^>]*class="nav-group"[^>]*data-nav-key="([a-z0-9-]+)"',
                        html)
    assert len(groups) == 7, f"顶层分组数变成 {len(groups)}: {groups}"
    assert set(groups) == {"video", "image", "image-edit", "music",
                            "assets", "agent", "system"}, \
        f"顶层分组集合变了: {groups}"


def test_build_id_script_is_loaded():
    """build-id.js 必须被 index.html 引入（且带版本号）。"""
    html = (REPO / "web" / "index.html").read_text(encoding="utf-8")
    m = re.search(r'src="/static/build-id\.js\?v=([^"]+)"', html)
    assert m, "index.html 没引入 build-id.js"
    assert m.group(1), "build-id.js 没有 ?v= 版本号"


def test_build_id_script_degrades_without_throwing():
    """接口挂掉时必须降级为「—」，不抛异常、不影响别的功能。"""
    src = (REPO / "web" / "build-id.js").read_text(encoding="utf-8")
    assert "catch" in src, "build-id.js 没有 catch ⇒ 接口挂掉会抛"
    # 不能有 alert/confirm 这类阻塞式提示
    for bad in ("alert(", "confirm(", "prompt("):
        assert bad not in src, f"build-id.js 里不该有 {bad}"


def test_version_marker_visible_in_narrow_viewport(browser, live_server):
    """窄视口（375×667）下版本号必须**看得见**（在视口内 + 命中自身）。"""
    from playwright.sync_api import sync_playwright  # noqa: F401
    ctx = browser.new_context(viewport={"width": 375, "height": 667},
                              is_mobile=True, has_touch=True, device_scale_factor=2)
    pg = ctx.new_page()
    try:
        pg.goto(live_server + "/", wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_selector("#buildIdValue", timeout=30000)
        # 等版本号**真的填上**（不是初始的「…」）
        # 【为什么要等】build-id.js 是异步 fetch /api/version ——
        #   DOM 出现 ≠ 值已到。第一版只等 DOM，结果读到 '…' 就判失败。
        try:
            pg.wait_for_function(
                """() => { const el = document.querySelector('#buildIdValue');
                   if (!el) return false;
                   const t = String(el.textContent || '').trim();
                   return t && t !== '…' && t !== '—'; }""",
                timeout=20000)
        except Exception:
            pass
        pg.wait_for_timeout(500)
        res = pg.evaluate(
            """() => {
              const el = document.querySelector('#buildIdValue');
              if (!el) return {found: false};
              const b = el.getBoundingClientRect();
              const text = String(el.textContent || '').trim();
              const inView = b.width > 0 && b.height > 0 &&
                             b.bottom > 0 && b.top < innerHeight &&
                             b.right > 0 && b.left < innerWidth;
              let hitSelf = false;
              if (inView) {
                const x = Math.round(b.left + b.width/2);
                const y = Math.round(b.top + b.height/2);
                const t = document.elementFromPoint(x, y);
                hitSelf = t ? (t === el || el.contains(t)) : false;
              }
              return {found: true, text: text, inView: inView, hitSelf: hitSelf,
                      rect: {t: Math.round(b.top), l: Math.round(b.left),
                             w: Math.round(b.width), h: Math.round(b.height)}};
            }""")
        assert res["found"], "页面里没有 #buildIdValue"
        assert res["text"] and res["text"] != "—", \
            f"版本号没取到值（text={res['text']!r}）⇒ /api/version 没通"
        # 文本里应含短哈希形状
        assert re.search(r"[0-9a-f]{7,12}", res["text"]), \
            f"版本号里没有 commit 短哈希: {res['text']!r}"
        assert res["inView"], f"375×667 下版本号不在视口内: {res['rect']}"
        assert res["hitSelf"], f"版本号被别的元素挡住: {res['rect']}"
    finally:
        ctx.close()
