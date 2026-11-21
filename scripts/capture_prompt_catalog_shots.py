# -*- coding: utf-8 -*-
"""「创意内容设计」面板截图取证（桌面 + 375px 窄屏）。

产出：
  evidence/prompt-catalog/desktop-1440.png     桌面：面板展开 + 已选内容 + 预览
  evidence/prompt-catalog/desktop-conflict.png   桌面：冲突提示可见
  evidence/prompt-catalog/narrow-375.png        375px：面板展开，无溢出
  evidence/prompt-catalog/narrow-320.png        320px：面板展开，无溢出
  evidence/prompt-catalog/narrow-390.png        390px：面板展开，无溢出
  evidence/prompt-catalog/narrow-430.png        430px：面板展开，无溢出
"""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:40939"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else
           "/root/projects/h3studio-creative-prompt/evidence/prompt-catalog")
OUT.mkdir(parents=True, exist_ok=True)


def pick(page, label, prefix):
    r = page.evaluate("""([label, prefix]) => {
      const s = Array.from(document.querySelectorAll('#pcGroups select'))
        .find(x => x.getAttribute('aria-label') === label);
      if (!s) return 'NO_SELECT';
      const o = Array.from(s.options).find(x => x.textContent.trim().startsWith(prefix));
      if (!o) return 'NO_OPTION';
      if (o.disabled) return 'DISABLED';
      s.value = o.value;
      s.dispatchEvent(new Event('change', {bubbles: true}));
      return 'OK';
    }""", [label, prefix])
    page.wait_for_timeout(250)
    return r


def type_custom(page, label, text):
    """在某个分组的「自行输入」里键入内容（模拟真人打字）。"""
    r = page.evaluate("""([label, text]) => {
      const inp = Array.from(document.querySelectorAll('#pcGroups [data-custom]'))
        .find(x => x.getAttribute('aria-label') === label + ' 自行输入');
      if (!inp) return 'NO_INPUT';
      inp.value = text;
      inp.dispatchEvent(new Event('input', {bubbles: true}));
      return 'OK';
    }""", [label, text])
    page.wait_for_timeout(250)
    return r


def open_designer(page):
    page.wait_for_selector("#pcOpenBtn", timeout=30000)
    page.evaluate("""() => {
      const panel = document.querySelector('#panel');
      const btn = document.querySelector('#pcOpenBtn');
      if (panel && btn) panel.scrollTop = btn.offsetTop - 60;
    }""")
    page.wait_for_timeout(300)
    page.click("#pcOpenBtn")
    page.wait_for_timeout(700)


PICKS = [
    ("3.0 主体类别（顶层，决定后面哪些子维度可用）", "人物（单人）"),
    ("3.3 发型与发色", "长直发"),
    ("3.5 上装", "衬衫"),
    ("1.1 动漫 / 二次元", "赛璐璐动画"),
    ("4.1 场景类型", "森林"),
    ("4.2 前景", "虚化花草"),
    ("5.A 经典构图", "三分法"),
    ("2.2 景别", "中景"),
    ("7.1 自然光时段与天气光", "黄金时刻"),
    ("6.3 饱和度策略", "低饱和 / 灰调"),
    ("8.1 氛围与情绪", "宁静"),
]

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True,
                           args=["--no-sandbox", "--disable-dev-shm-usage"])

    # ---------------- 桌面 ----------------
    ctx = b.new_context(viewport={"width": 1440, "height": 950})
    page = ctx.new_page()
    page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3500)
    page.click('.nav-item[data-mode="t2i"]')
    page.wait_for_timeout(1200)
    open_designer(page)
    for label, opt in PICKS:
        pick(page, label, opt)
    # 【自行输入】词库选项 + 自定义内容混合（截图要拍到这个入口）
    type_custom(page, "4.2 前景", "压在画面下缘的旧报纸")
    # 滚到预览区
    page.evaluate("""() => {
      const p = document.querySelector('#panel');
      const prev = document.querySelector('#pcPreviewText');
      if (p && prev) p.scrollTop = prev.offsetTop - 260;
    }""")
    page.wait_for_timeout(500)
    page.screenshot(path=str(OUT / "desktop-1440.png"))
    print("desktop-1440.png", page.evaluate(
        "() => document.querySelector('#pcPreviewText').innerText"))

    # 冲突提示
    pick(page, "7.2 影棚布光", "柔光箱")
    page.evaluate("""() => {
      const p = document.querySelector('#panel');
      const c = document.querySelector('#pcConflicts');
      if (p && c) p.scrollTop = c.offsetTop - 200;
    }""")
    page.wait_for_timeout(500)
    page.screenshot(path=str(OUT / "desktop-conflict.png"))
    print("desktop-conflict.png ok")
    ctx.close()

    # ---------------- 窄屏 ----------------
    for w in (320, 375, 390, 430):
        ctx = b.new_context(viewport={"width": w, "height": 760})
        page = ctx.new_page()
        page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        page.click("#navToggle")
        page.wait_for_timeout(700)
        page.click('.nav-item[data-mode="t2i"]')
        page.wait_for_timeout(1000)
        page.click(".panel-fab")
        page.wait_for_timeout(900)
        open_designer(page)
        for label, opt in PICKS:
            pick(page, label, opt)
        # ① 面板顶部（入口按钮 + 搜索框 + 前几个分组）
        page.evaluate("""() => {
          const p = document.querySelector('#panel');
          const head = document.querySelector('#promptCatalogField');
          if (p && head) p.scrollTop = head.offsetTop - 40;
        }""")
        page.wait_for_timeout(500)
        page.screenshot(path=str(OUT / f"narrow-{w}.png"))
        # ② 预览与写回区
        page.evaluate("""() => {
          const p = document.querySelector('#panel');
          const prev = document.querySelector('#pcPreviewText');
          if (p && prev) p.scrollTop = prev.offsetTop - 200;
        }""")
        page.wait_for_timeout(500)
        page.screenshot(path=str(OUT / f"narrow-{w}-preview.png"))
        overflow = page.evaluate("""() => {
          const vw = window.innerWidth;
          const pc = document.querySelector('#promptCatalogPanel');
          let bad = 0;
          if (pc) Array.from(pc.querySelectorAll('*')).forEach(k => {
            const r = k.getBoundingClientRect();
            if (r.width > 0 && r.right > vw + 1) bad++;
          });
          return {vw, docOverflow: document.documentElement.scrollWidth
                  - document.documentElement.clientWidth, pcOverflow: bad};
        }""")
        print(f"narrow-{w}.png", overflow)
        ctx.close()

    b.close()
print("OUT =", OUT)
