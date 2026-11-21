"""线上 8190 探活 + 面板真实可用性验证（部署后跑，不碰生成）。"""
import json
import sys
import urllib.request
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190"
OUT = sys.argv[2] if len(sys.argv) > 2 else \
    "/root/projects/h3studio-creative-prompt/evidence/prompt-catalog/live"

# ---------- 1) 接口探活 ----------
with urllib.request.urlopen(BASE + "/api/prompt-catalog", timeout=20) as r:
    cat = json.loads(r.read().decode())
with urllib.request.urlopen(BASE + "/", timeout=20) as r:
    html = r.read().decode("utf-8", "replace")
print("GET /api/prompt-catalog -> 200")
print("counts:", json.dumps(cat["counts"], ensure_ascii=False))
print("version:", cat["version"])
print("index.html 引用 prompt-catalog:",
      [l.strip() for l in html.splitlines() if "prompt-catalog" in l])
print("index.html app.js 版本号:",
      [l.strip() for l in html.splitlines() if "app.js?v=" in l][:1])

# ---------- 2) 浏览器真实可用性 ----------
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True,
                           args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = b.new_context(viewport={"width": 1440, "height": 950})
    page = ctx.new_page()
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3500)
    page.click('.nav-item[data-mode="t2i"]')
    page.wait_for_timeout(1200)

    assert page.query_selector("#promptCatalogPanel") is not None, "线上没有面板宿主"
    page.evaluate("""() => {
      const panel = document.querySelector('#panel');
      const btn = document.querySelector('#pcOpenBtn');
      if (panel && btn) panel.scrollTop = btn.offsetTop - 80;
    }""")
    page.wait_for_timeout(300)
    page.click("#pcOpenBtn")
    page.wait_for_timeout(900)

    groups = page.eval_on_selector_all("#pcGroups .pc-group", "e => e.length")
    print("线上分组数:", groups)

    # 真选两项 + 自行输入一项 → 组装 → 写回
    page.evaluate("""() => {
      const sels = Array.from(document.querySelectorAll('#pcGroups select[data-select]'));
      const set = (lab, pref) => {
        const s = sels.find(x => x.getAttribute('aria-label') === lab);
        if (!s) return 'NO_SELECT';
        const o = Array.from(s.options).find(x => x.textContent.trim().startsWith(pref));
        if (!o) return 'NO_OPTION';
        s.value = o.value; s.dispatchEvent(new Event('change', {bubbles: true}));
        return 'OK';
      };
      window.__r = [];
      window.__r.push(set('3.0 主体类别（顶层，决定后面哪些子维度可用）', '人物（单人）'));
      window.__r.push(set('1.1 动漫 / 二次元', '赛璐璐动画'));
    }""")
    page.wait_for_timeout(600)
    page.evaluate("""() => {
      const i = Array.from(document.querySelectorAll('#pcGroups [data-custom]'))
        .find(x => x.getAttribute('aria-label') === '4.4 远景 / 背景 自行输入');
      if (i) { i.value = '远景是雾中的高压电塔';
               i.dispatchEvent(new Event('input', {bubbles: true})); }
    }""")
    page.wait_for_timeout(600)
    parts = page.evaluate("() => window.PROMPT_CATALOG.assemble().parts")
    print("线上组装结果:", parts)
    page.screenshot(path=f"{OUT}/live-desktop-1440.png")

    # 硬互斥的可见说明
    page.evaluate("""() => {
      window.PROMPT_CATALOG.picks.clear();
      window.PROMPT_CATALOG.customs.clear();
      window.PROMPT_CATALOG.render();
    }""")
    page.wait_for_timeout(400)
    page.evaluate("""() => {
      const s = Array.from(document.querySelectorAll('#pcGroups select[data-select]'))
        .find(x => x.getAttribute('aria-label') === '1.1 动漫 / 二次元');
      const o = Array.from(s.options).find(x => x.textContent.trim().startsWith('赛璐璐动画'));
      s.value = o.value; s.dispatchEvent(new Event('change', {bubbles: true}));
    }""")
    page.wait_for_timeout(700)
    conflict = page.evaluate("""() => {
      const s = Array.from(document.querySelectorAll('#pcGroups select[data-select]'))
        .find(x => x.getAttribute('aria-label') === '1.2 写实摄影');
      const o = Array.from(s.options).find(x => x.textContent.trim().startsWith('真实摄影'));
      const note = s.closest('.pc-group').querySelector('.pc-conflict-inline');
      return {disabled: o.disabled, note: note ? note.innerText : null,
              visible: note ? note.getBoundingClientRect().height > 0 : false};
    }""")
    print("线上硬互斥可见说明:", conflict)
    page.screenshot(path=f"{OUT}/live-conflict.png")

    # 窄屏 375
    ctx2 = b.new_context(viewport={"width": 375, "height": 760})
    p2 = ctx2.new_page()
    p2.goto(BASE, wait_until="domcontentloaded", timeout=60000)
    p2.wait_for_timeout(3000)
    p2.click("#navToggle"); p2.wait_for_timeout(600)
    p2.click('.nav-item[data-mode="t2i"]'); p2.wait_for_timeout(900)
    p2.click(".panel-fab"); p2.wait_for_timeout(900)
    p2.wait_for_selector("#pcOpenBtn", timeout=30000)
    p2.evaluate("""() => {
      const panel = document.querySelector('#panel');
      const btn = document.querySelector('#pcOpenBtn');
      if (panel && btn) panel.scrollTop = btn.offsetTop - 80;
    }""")
    p2.wait_for_timeout(300)
    p2.click("#pcOpenBtn"); p2.wait_for_timeout(900)
    narrow = p2.evaluate("""() => {
      const vw = window.innerWidth;
      const pc = document.querySelector('#promptCatalogPanel');
      let bad = 0;
      if (pc) Array.from(pc.querySelectorAll('*')).forEach(k => {
        const r = k.getBoundingClientRect();
        if (r.width > 0 && r.right > vw + 1) bad++;
      });
      return {vw, docOverflow: document.documentElement.scrollWidth
              - document.documentElement.clientWidth, pcOverflow: bad,
              groups: document.querySelectorAll('#pcGroups .pc-group').length,
              customs: document.querySelectorAll('#pcGroups [data-custom]').length};
    }""")
    print("线上 375px:", narrow)
    p2.screenshot(path=f"{OUT}/live-narrow-375.png")
    print("页面 JS 错误:", errs)
    ctx.close(); ctx2.close(); b.close()
