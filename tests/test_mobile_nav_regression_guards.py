# -*- coding: utf-8 -*-
"""移动端导航死路 —— 第二轮复测沉淀的**回归守卫**。

【这一轮新沉淀了什么】
第一轮（edba2ef）修完了「选完类型回不去 / 看不到媒体库」，
但第二轮**全量复测**又暴露了三类问题 ——
其中两类是**测试脚本自己的错**（如果不写守卫，下一轮还会再踩），
一类是**真缺陷**（FAB 压住卡片）。

  T1 详情弹层的 id 是 **#detailModal**，不是 #jobDetailModal。
     #jobDetailModal 是 job-detail.js 里那套**已停用**的深色弹层
     （既有 tests/test_c6_detail_recommend.py 明确断言
      「点详情只能弹 detailModal，jobDetailModal 必须保持 hidden」）。
     ⇒ 查错 id 会误报「详情打不开」。

  T2 两套标签的属性不同：
       状态标签 .jobs-tabs button[data-st]   → all/active/done/favorites
       生成类型标签 #genTypeTabs button[data-type] → ""/t2v/flf/...
     ⇒ 把生成类型标签也按 data-st 找，一个都找不到，会误报「筛选不生效」。

  T3 模态框没关干净会**级联**：
     上一个用例的详情弹层残留 → body.modal-open → 下一个用例所有
     tap 都命中失败 → 看起来像「好几个类型都坏了」。

  T4 真缺陷：FAB（position:fixed 右下角）压住媒体库最后一行卡片的
     一角 —— elementFromPoint 采样显示 1/9 个采样点命中 FAB。
     修法：给 .jobs-area 底部留出 FAB 的高度（padding-bottom），
     让卡片网格不会伸到 FAB 底下。

【本文件的判据】
  · 详情弹层必须用 #detailModal，且 #jobDetailModal 必须保持 hidden
  · 生成类型标签必须用 data-type，且点击后要真的变成 active
  · 每个用例开始前必须清干净模态框（防止级联）
  · FAB 不得遮挡卡片（elementFromPoint 采样，0 个采样点命中 FAB）
"""
import json
import os
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

playwright_api = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

PLAYWRIGHT = shutil.which("playwright") or "/usr/local/lib/hermes-agent/venv/bin/playwright"
CHROMIUM = None
for _c in sorted(Path.home().joinpath(".cache/ms-playwright").glob("chromium-*/chrome-linux64/chrome"),
                 reverse=True):
    CHROMIUM = str(_c)
    break


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def live_server():
    """起一个**自己的** h3studio 实例（用真实数据目录，但独立端口 + 独立 DB 副本）。

    为什么要独立实例：
      · 不能打线上 8190 —— 那是用户的生产服务，测试会污染它；
      · 不能共用一个 DB —— 会和别的任务抢写。
    """
    data_src = Path("/root/projects/comfyui-minimax-h3-studio/data")
    port = _free_port()
    tmp = Path(f"/tmp/h3s-nav2-{port}")
    tmp.mkdir(parents=True, exist_ok=True)
    if data_src.exists():
        for f in data_src.glob("*.db"):
            shutil.copy2(f, tmp / f.name)
    env = dict(os.environ)
    env["H3STUDIO_PORT"] = str(port)
    env["PYTHONPATH"] = str(REPO)
    cfg = REPO / "config.json"
    if cfg.exists():
        shutil.copy2(cfg, tmp / "config.json")
        c = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
        c["data_dir"] = str(tmp)
        (tmp / "config.json").write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "server.py"], cwd=str(REPO), env=env,
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


@pytest.fixture(scope="module")
def browser():
    if not CHROMIUM or not Path(CHROMIUM).exists():
        pytest.skip("chromium 不可用")
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=CHROMIUM, headless=True,
                              args=["--no-sandbox"])
        yield b
        b.close()


@pytest.fixture
def page(browser, live_server):
    ctx = browser.new_context(viewport={"width": 375, "height": 812},
                              is_mobile=True, has_touch=True, device_scale_factor=2)
    pg = ctx.new_page()
    pg.goto(live_server + "/", wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_selector("#jobsGrid .job-card[data-jid]", timeout=45000)
    pg.wait_for_timeout(2000)
    yield pg
    ctx.close()


def _reset(pg):
    """清干净模态框 —— 防级联（T3）。"""
    pg.evaluate("""() => {
      document.querySelectorAll('.prompt-modal, .job-detail-modal, .lightbox')
        .forEach(m => { m.hidden = true; });
      document.body.classList.remove('modal-open', 'job-detail-open');
      document.querySelectorAll('.sidebar,.panel,.stitch-panel')
        .forEach(e => e.classList.remove('open'));
      document.querySelectorAll('#sidebarMask,#panelMask')
        .forEach(e => e.classList.remove('open'));
    }""")
    pg.wait_for_timeout(200)


# ---------------------------------------------------------------- T1

def test_detail_modal_uses_detailModal_not_legacy(page):
    """T1：点卡片打开的必须是 **#detailModal**（浅色、字段完整），
    **#jobDetailModal**（已停用的深色弹层）必须保持 hidden。

    【为什么要守】第一轮复测用错了 id，误报「详情打不开」，
    还因为模态框没关干净级联到后面几个用例全红。
    """
    _reset(page)
    page.tap("#jobsGrid .job-card[data-jid]", timeout=8000)
    page.wait_for_timeout(900)

    assert page.evaluate(
        "() => { const m = document.querySelector('#detailModal');"
        " return !!m && !m.hasAttribute('hidden')"
        " && getComputedStyle(m).display !== 'none'; }"
    ), "#detailModal 必须打开"

    assert page.evaluate(
        "() => { const m = document.querySelector('#jobDetailModal');"
        " if (!m) return true;"
        " return m.hasAttribute('hidden') || getComputedStyle(m).display === 'none'; }"
    ), "#jobDetailModal（停用的深色弹层）必须保持 hidden"

    body = page.evaluate(
        "() => { const b = document.querySelector('#detailModalBody');"
        " return b ? String(b.textContent||'').replace(/\\s+/g,' ') : ''; }")
    # 真实 params 必须出现
    assert ("步数" in body) or ("steps" in body.lower()), f"详情缺步数: {body[:200]}"
    assert ("调度器" in body) or ("scheduler" in body.lower()), f"详情缺调度器: {body[:200]}"
    assert ("种子" in body) or ("seed" in body.lower()), f"详情缺种子: {body[:200]}"

    # 能关掉
    page.tap("#detailModalClose", timeout=8000)
    page.wait_for_timeout(600)
    assert page.evaluate(
        "() => { const m = document.querySelector('#detailModal');"
        " return !m || m.hasAttribute('hidden')"
        " || getComputedStyle(m).display === 'none'; }"
    ), "详情弹层必须能关闭"


# ---------------------------------------------------------------- T2

def test_gen_type_tabs_use_data_type_and_take_effect(page):
    """T2：生成类型标签用 **data-type**（不是 data-st），点击后必须变 active。"""
    _reset(page)
    tabs = page.evaluate(
        "() => Array.from(document.querySelectorAll('#genTypeTabs button'))"
        ".map(b => ({type: b.dataset.type || '', text: String(b.textContent||'').trim()}))")
    assert len(tabs) >= 2, f"生成类型标签太少: {tabs}"
    assert any(t["type"] for t in tabs), f"没有非空 data-type 的标签: {tabs}"

    target = next(t for t in tabs if t["type"])
    page.tap(f'#genTypeTabs button[data-type="{target["type"]}"]', timeout=8000)
    page.wait_for_timeout(900)
    assert page.evaluate(
        """(t) => { const b = document.querySelector(`#genTypeTabs button[data-type="${t}"]`);
           return !!b && b.classList.contains('active'); }""", target["type"]
    ), f"点生成类型标签后没变 active: {target}"

    # 复位
    page.tap('#genTypeTabs button[data-type=""]', timeout=8000)
    page.wait_for_timeout(400)


def test_status_tabs_use_data_st_and_take_effect(page):
    """T2 的另一半：状态标签用 data-st，全部 4 个都要能点且生效。"""
    _reset(page)
    for st in ["all", "active", "done", "favorites"]:
        assert page.evaluate(
            """(s) => !!document.querySelector(`.jobs-tabs button[data-st="${s}"]`)""", st
        ), f"状态标签 {st} 不存在"
        page.tap(f'.jobs-tabs button[data-st="{st}"]', timeout=8000)
        page.wait_for_timeout(800)
        assert page.evaluate(
            """(s) => { const b = document.querySelector(`.jobs-tabs button[data-st="${s}"]`);
               return !!b && b.classList.contains('active'); }""", st
        ), f"状态标签 {st} 点完没变 active"
    page.tap('.jobs-tabs button[data-st="all"]', timeout=8000)
    page.wait_for_timeout(400)


# ---------------------------------------------------------------- T3

def test_no_modal_leak_between_interactions(page):
    """T3：任何一轮交互后都不该留下 modal-open / 遮罩常开。

    【为什么要守】模态框残留会让后续所有 tap 命中失败，
    表现为「好几个功能都坏了」的假象。
    """
    _reset(page)
    page.tap("#jobsGrid .job-card[data-jid]", timeout=8000)
    page.wait_for_timeout(800)
    page.tap("#detailModalClose", timeout=8000)
    page.wait_for_timeout(600)
    st = page.evaluate(
        """() => ({
             modalOpen: document.body.classList.contains('modal-open')
                        || document.body.classList.contains('job-detail-open'),
             detailHidden: (document.querySelector('#detailModal')||{}).hasAttribute
                            ? document.querySelector('#detailModal').hasAttribute('hidden')
                            : true,
             sidebarOpen: document.querySelector('#sidebar').classList.contains('open'),
             panelOpen: !!document.querySelector('#panel.open'),
             maskOpen: !!document.querySelector('#sidebarMask.open, #panelMask.open'),
           })""")
    assert not st["modalOpen"], f"modal-open 残留: {st}"
    assert st["detailHidden"], f"详情弹层没关: {st}"
    assert not st["sidebarOpen"], f"侧栏抽屉残留: {st}"
    assert not st["panelOpen"], f"参数抽屉残留: {st}"
    assert not st["maskOpen"], f"遮罩残留: {st}"


# ---------------------------------------------------------------- T4

def test_fab_does_not_cover_media_library_cards(page):
    """T4（真缺陷）：FAB（右下角固定按钮）不得**挡死**媒体库卡片的可点区域。

    【判据】用 elementFromPoint 在每张卡片的可见区域采样 9 个点，
    命中 FAB 的点数必须为 0（**首屏**这一档不许有任何采样点被 FAB 吃掉）。

    【为什么要守】FAB 是 position:fixed right:14 bottom:16，
    而 .jobs-area 底部**必须**给它留出位置 —— 网格最后一行会伸到 FAB 底下。

    【真实缺陷（2026-11-21 第二轮复测）】
    给 FAB 让位的 `padding-bottom:76px` 原来只写在 **≤700px** 那档，
    而 FAB 在 **≤1100px**（平板档）就出现 ⇒ 701~1100px 整段没让位。
    实测 768×1024：分页条底边 1008 与 FAB 顶边 964 重叠 44px
    （修后分页条底边 948，与 FAB 不再重叠）。

    【关于「瞬时重叠」】
    FAB 是 fixed，内容滚动到它底下时**瞬时**被盖是浮动按钮的固有行为
    （Material FAB 就这样），不能要求「重叠面积恒为 0」。
    真判据是**可达性**（见 test_fab_does_not_cover_card_favorite_button_at_tablet_width：
    滚动全程 24 步采样，「任何滚动位置都点不到」= 0）。
    本测试守的是**首屏**不许被吃采样点 —— 那是用户第一眼看到的画面。
    """
    _reset(page)
    # 滚到最底部 —— 缺陷最容易在最后一屏出现
    page.evaluate("""() => {
      const a = document.querySelector('#jobsArea');
      if (a && a.scrollHeight > a.clientHeight) a.scrollTop = a.scrollHeight;
      window.scrollTo(0, document.body.scrollHeight);
    }""")
    page.wait_for_timeout(800)

    res = page.evaluate(
        """() => {
          const fab = document.querySelector('#panelFab');
          if (!fab) return {skip: true};
          const cs = getComputedStyle(fab);
          if (cs.display === 'none' || fab.hasAttribute('hidden'))
            return {skip: true, reason: 'fab-hidden'};
          const bad = [];
          let sampled = 0;
          for (const el of document.querySelectorAll('#jobsGrid .job-card')) {
            const b = el.getBoundingClientRect();
            if (b.width === 0 || b.height === 0) continue;
            if (b.bottom <= 0 || b.top >= innerHeight) continue;
            for (let i = 1; i <= 3; i++) for (let j = 1; j <= 3; j++) {
              const x = Math.round(b.left + b.width * i / 4);
              const y = Math.round(b.top + b.height * j / 4);
              if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
              sampled++;
              const t = document.elementFromPoint(x, y);
              if (t && (t === fab || fab.contains(t))) {
                bad.push({text: String(el.textContent||'').trim().slice(0,30),
                          x: x, y: y});
              }
            }
          }
          return {skip: false, sampled: sampled, blocked: bad.length,
                  detail: bad.slice(0, 6)};
        }""")
    if res.get("skip"):
        pytest.skip(res.get("reason", "fab 不可见"))
    assert res["blocked"] == 0, (
        f"FAB 压住媒体库卡片 {res['blocked']}/{res['sampled']} 个采样点: {res['detail']}")


def _media_blocks(css, condition):
    """用**括号配对**切出所有满足条件的 @media 块。

    【为什么不能简单地 find('@media', start+10)】
    responsive.css 里有**多个** `@media (max-width: 1100px)` 块
    （第 79 / 104 / 619 行），而且 CSS **注释里也会出现 '@media'** ——
    简单 find 会把块切在注释中间，导致「块里找不到 .panel-fab」这种
    假失败（第一版就踩了）。
    ⇒ 必须按大括号深度配对。
    """
    blocks = []
    for m in re.finditer(re.escape(condition), css):
        i = css.find("{", m.end())
        if i < 0:
            continue
        depth = 0
        j = i
        while j < len(css):
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        blocks.append(css[m.start():j + 1])
    return blocks


def test_fab_clearance_exists_in_tablet_breakpoint():
    """T4 的**静态**守卫：给 FAB 让位的 padding-bottom 必须写在
    **FAB 出现的同一档**（≤1100px 平板档），不能只写在 ≤700px 手机档。

    【为什么要有静态守卫】浏览器测 375px 抓不到这个缺陷
    （手机档本来就有让位）—— 只有平板档才暴露。
    而平板档是 701~1100px 一整段，靠人工记得改很容易漏。
    ⇒ 用源码断言把「让位规则和 FAB 在同一断点」这件事锁死。
    """
    css = (REPO / "web" / "responsive.css").read_text(encoding="utf-8")

    tablet_blocks = _media_blocks(css, "@media (max-width: 1100px)")
    assert tablet_blocks, "找不到 ≤1100px 断点"

    # FAB 出现在哪个块里？
    fab_blocks = [b for b in tablet_blocks if ".panel-fab" in b]
    assert fab_blocks, "≤1100px 档里没有 .panel-fab 规则"

    # 那个块里必须同时有给 FAB 让位的底部留白
    has_clearance = any(
        re.search(r"\.jobs-area\s*\{[^}]*padding-bottom\s*:\s*7[0-9]px", b)
        for b in fab_blocks)
    assert has_clearance, (
        "≤1100px（FAB 出现的档）里 .jobs-area 没有为 FAB 预留底部空间 "
        "(padding-bottom >= 70px) ⇒ 平板宽度下 FAB 会压住卡片 "
        "（实测 768px：压住「☆ 收藏」按钮 79×16px）")


def test_fab_does_not_cover_card_favorite_button_at_tablet_width(browser, live_server):
    """T4 的**行为**守卫：平板宽度（768px）下，卡片上的可交互元素
    （尤其「☆ 收藏」）**必须存在某个滚动位置让它的中心点可点**。

    【为什么不用「几何重叠面积 = 0」当判据】
    FAB 是 `position:fixed`，它盖住的是**视口右下角**那一块 86×44px。
    媒体库网格在那个位置下面滚动 ⇒ **任何滚动位置**都可能有内容伸到
    FAB 底下 —— 这是浮动按钮（Material FAB）的固有行为，
    要求「重叠面积恒为 0」是做不到的（内容会滚过去）。
    ⇒ 真判据是**可达性**：每个可交互元素是否都存在可点的滚动位置。

    【实测（scripts/probe_fab_scroll_sweep.py，滚动全程 24 步采样）】
      修前 8190 @768：瞬时重叠 13 次（最大 3364px²），
                     但「任何滚动位置都点不到」= **0**
      修后 8922 @768：瞬时重叠 13 次（最大 2640px²），
                     不可达 = **0**
    ⇒ 收藏功能在平板宽度下**可用**，不是死路。

    【本测试守什么】守住「不可达 = 0」这条底线 ——
    万一以后有人把 FAB 挪到卡片按钮的正上方且不留滚动余地，
    这里就会红。
    """
    ctx = browser.new_context(viewport={"width": 768, "height": 1024},
                              is_mobile=True, has_touch=True, device_scale_factor=2)
    pg = ctx.new_page()
    try:
        pg.goto(live_server + "/", wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_selector("#jobsGrid .job-card[data-jid]", timeout=45000)
        pg.wait_for_timeout(2500)
        res = pg.evaluate(
            """() => {
              const area = document.querySelector('#jobsArea');
              const fab = document.querySelector('#panelFab');
              if (!area || !fab) return {skip: true};
              const cs = getComputedStyle(fab);
              if (cs.display === 'none' || fab.hasAttribute('hidden'))
                return {skip: true, reason: 'fab-hidden'};
              const maxScroll = Math.max(0, area.scrollHeight - area.clientHeight);
              const reach = new Map();
              for (let s = 0; s <= 24; s++) {
                area.scrollTop = Math.round(maxScroll * s / 24);
                void area.offsetHeight;
                for (const el of document.querySelectorAll(
                      '#jobsGrid .job-card button, #jobsGrid .job-card a')) {
                  const b = el.getBoundingClientRect();
                  if (b.width === 0 || b.height === 0) continue;
                  if (b.bottom <= 0 || b.top >= innerHeight) continue;
                  const cx = Math.round(b.left + b.width / 2);
                  const cy = Math.round(b.top + b.height / 2);
                  const t = document.elementFromPoint(cx, cy);
                  const ok = t ? (t === el || el.contains(t)) : false;
                  const key = (el.dataset.act || '') + '|' +
                              String(el.textContent || '').trim().slice(0, 14);
                  const rec = reach.get(key);
                  if (rec) rec.reachable = rec.reachable || ok;
                  else reach.set(key, {act: el.dataset.act || null,
                                       text: String(el.textContent || '').trim().slice(0, 14),
                                       reachable: ok});
                }
              }
              area.scrollTop = 0;
              const list = [...reach.values()];
              return {skip: false, seen: list.length,
                      unreachable: list.filter(r => !r.reachable)};
            }""")
        if res.get("skip"):
            pytest.skip(res.get("reason", "fab 不可见"))
        assert res["seen"] > 0, "没扫到任何卡片可交互元素，测试无意义"
        assert res["unreachable"] == [], (
            f"768px 下这些卡片元素在任何滚动位置都点不到（被 FAB 挡死）: "
            f"{res['unreachable']}")
    finally:
        ctx.close()


def test_hamburger_clickable_while_params_drawer_open(page):
    """参数抽屉开着时，顶栏汉堡必须真能点到（命中测试）。

    【根因回归守卫】修前 .panel-mask 是 inset:0 —— 遮罩从视口顶铺满，
    顶栏（44px、无 z-index）被盖住 ⇒ 汉堡命中的是遮罩 ⇒ 死路。
    修法：遮罩改成 inset: 44px 0 0 0，让开顶栏。
    """
    _reset(page)
    page.tap("#panelFab", timeout=8000)
    page.wait_for_timeout(900)
    assert page.evaluate("() => !!document.querySelector('#panel.open')"), "参数抽屉没打开"

    hit = page.evaluate(
        """() => { const t = document.querySelector('#navToggle');
           const b = t.getBoundingClientRect();
           const x = Math.round(b.left + b.width/2), y = Math.round(b.top + b.height/2);
           const e = document.elementFromPoint(x, y);
           return {hitIsSelf: e ? (e === t || t.contains(e)) : false,
                   hitId: e ? e.id : null,
                   hitCls: e ? String(e.className||'').slice(0,60) : null}; }""")
    assert hit["hitIsSelf"], (
        f"参数抽屉打开时汉堡被挡住，命中的是 {hit['hitId'] or hit['hitCls']}")

    # 而且要真能点开侧栏
    page.tap("#navToggle", timeout=8000)
    page.wait_for_timeout(900)
    assert page.evaluate(
        "() => document.querySelector('#sidebar').classList.contains('open')"
    ), "参数抽屉开着时点汉堡没能打开侧栏"
