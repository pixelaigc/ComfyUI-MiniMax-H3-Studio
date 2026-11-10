# -*- coding: utf-8 -*-
"""改前 / 改后 各面板增强入口形态对照。

同一套探针跑两个实例（基线 = 未改 local/maintainer，分支 = local/feature/enh-unify），
输出**每个面板的增强入口清单**，用来生成对照表。

用法：
    python3 tests/verify_enhance_before_after.py --base http://127.0.0.1:8297/ \
        --branch http://127.0.0.1:8291/ --out <json>
"""
import argparse
import json
import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except Exception as exc:                                    # pragma: no cover
    print("playwright 缺失：%s" % exc)
    sys.exit(2)

PANELS = [
    ("t2i",      "Z-Image 文生图",   "t2i"),
    ("t2i_qwen", "Qwen 文生图",      "t2i_qwen"),
    ("i2i_qwen", "Qwen 图片编辑",    "i2i_qwen"),
    ("t2i_moss", "MOSS 文生图",      "t2i_moss"),
    ("t2v",      "文生视频",         "t2v"),
    ("t2a",      "文生音乐",         "t2a"),
]

PROBE = """(cfg) => {
  const {menuSel, mainSel, caretSel, curSel, isMusic} = cfg;
  const q = s => document.querySelector(s);
  const vis = el => { if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0; };

  // 只统计**当前面板**的增强控件（音乐面板与图像面板在 DOM 里并存，
  // 不按面板过滤会把另一套也算进来 —— 那是探针缺陷，不是产品问题）。
  const panel = q('#panel');
  const scope = panel || document;
  // 面板内控件（按钮/模板行）在 #panel 里找；
  // 菜单容器与回显可能挂在别处 —— 用**全局 id**查，避免漏掉。
  const inPanel = sel => Array.from(scope.querySelectorAll(sel));
  const byId = sel => { const e = q(sel); return e ? [e] : []; };

  const mainBtns = inPanel('button').filter(b =>
    (isMusic ? b.id === 'musicEnhanceBtn' : b.id === 'enhanceBtn'));
  const caretBtns = inPanel('button').filter(b =>
    (isMusic ? b.id === 'musicEnhanceMenuBtn' : b.id === 'enhanceMenuBtn'));
  const menus = byId(isMusic ? '#musicEnhanceMenu' : '#enhanceMenu');
  const curs = byId(isMusic ? '#musicEnhanceCurrent' : '#enhanceCurrent');
  // 独立的第二个「增强模板」下拉（改造前的重复入口）
  const standaloneSpecs = Array.from(document.querySelectorAll('select')).filter(s =>
    /^enhanceSpec$/.test(s.id) || /^musicEnhanceSpec$/.test(s.id));
  const specRows = byId(isMusic ? '#musicEnhanceSpecRow' : '#enhanceSpecRow');

  const menu = menus[0];
  const items = menu ? Array.from(menu.querySelectorAll('.enhance-menu-item')).map(it => ({
     method: it.dataset.method,
     label: (it.querySelector('.enhance-menu-label')||{}).textContent || '',
     disabled: !!(it.querySelector('input[type=checkbox]')||{}).disabled,
     badges: Array.from(it.querySelectorAll('.enhance-menu-badge')).map(b => (b.textContent||'').trim()),
  })) : [];

  return {
    mainBtnIds: mainBtns.map(b => b.id),
    caretBtnIds: caretBtns.map(b => b.id),
    menuIds: menus.map(m => m.id),
    menuExpanded: menu ? !menu.hidden : null,
    menuItems: items,
    currentIds: curs.map(c => ({id: c.id, visible: vis(c),
                                text: (c.textContent||'').trim()})),
    standaloneSpecSelectIds: standaloneSpecs.map(s => s.id),
    specRowIds: specRows.map(r => ({id: r.id, visible: vis(r)})),
    specRowOptions: specRows.map(r => {
      const sel = r.querySelector('select');
      return sel ? Array.from(sel.options).map(o => o.value) : [];
    })[0] || [],
  };
}"""


def probe_instance(base):
    """在给定实例上量一遍各面板的增强入口形态。

    【注意】不能用嵌套的 sync_playwright() —— Playwright sync API
    不能在已有 asyncio loop 里再用。所以这里**自己起一个**，
    由调用方串行调用（不并发），避免两个实例互相抢 CPU。
    """
    out = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        page.goto(base, wait_until="domcontentloaded")
        page.wait_for_function(
            "() => typeof provState !== 'undefined' && provState "
            "&& (provState.agent || provState.specs || Object.keys(provState).length > 0)",
            timeout=60000)
        page.wait_for_timeout(2500)

        for key, label, mode in PANELS:
            page.evaluate(
                "(m) => { const e = document.querySelector(`.nav-item[data-mode='${m}']`);"
                "if (e) e.click(); }", mode)
            page.wait_for_timeout(1200)
            # 打开抽屉 + 展开下拉
            page.evaluate("""() => {
              const p = document.querySelector('#panel');
              if (p && !p.classList.contains('open')) {
                const fab = document.querySelector('#panelFab');
                if (fab) fab.click();
              }
            }""")
            page.wait_for_timeout(600)
            page.evaluate("""(sel) => {
              const b = document.querySelector(sel);
              if (b) b.click();
            }""", "#musicEnhanceMenuBtn" if mode in ("t2a", "a2a") else "#enhanceMenuBtn")
            page.wait_for_timeout(700)
            is_music = mode in ("t2a", "a2a")
            cfg = {
              "menuSel": "#musicEnhanceMenu" if is_music else "#enhanceMenu",
              "mainSel": "#musicEnhanceBtn" if is_music else "#enhanceBtn",
              "caretSel": "#musicEnhanceMenuBtn" if is_music else "#enhanceMenuBtn",
              "curSel": "#musicEnhanceCurrent" if is_music else "#enhanceCurrent",
              "isMusic": is_music,
            }
            st = page.evaluate(PROBE, cfg)
            st["label"] = label
            st["mode"] = mode
            out[key] = st
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)

        out["_console_errors"] = errs
        browser.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8297/")
    ap.add_argument("--branch", default="http://127.0.0.1:8291/")
    ap.add_argument("--out", default="enh-enhance-entry-before-after.json")
    args = ap.parse_args()

    before = probe_instance(args.base)
    after = probe_instance(args.branch)

    result = {"before": before, "after": after}
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1),
                              encoding="utf-8")

    # ---- 打印对照表 ----
    print("=" * 100)
    print("面板            | 改前(基线 local/maintainer)              | 改后(分支 local/feature/enh-unify)")
    print("=" * 100)
    for key, label, mode in PANELS:
        b = before.get(key, {})
        a = after.get(key, {})
        bs = "主按钮%s 箭头%s 下拉%s 回显%s 独立模板下拉%s" % (
            b.get("mainBtnIds"), b.get("caretBtnIds"), b.get("menuIds"),
            [c["id"] for c in b.get("currentIds", [])],
            b.get("standaloneSpecSelectIds") or "无")
        as_ = "主按钮%s 箭头%s 下拉%s 回显%s 独立模板下拉%s" % (
            a.get("mainBtnIds"), a.get("caretBtnIds"), a.get("menuIds"),
            [c["id"] for c in a.get("currentIds", [])],
            a.get("standaloneSpecSelectIds") or "无")
        print("\n### %s (%s)" % (label, mode))
        print("  改前: %s" % bs)
        print("        选项: %s" % [(i["method"], i["label"][:16], i["badges"]) for i in b.get("menuItems", [])])
        print("  改后: %s" % as_)
        print("        选项: %s" % [(i["method"], i["label"][:16], i["badges"]) for i in a.get("menuItems", [])])
        print("        模板行: %s" % (a.get("specRowIds") or "无"))

    print("\n改前 console 报错:", len(before.get("_console_errors", [])))
    print("改后 console 报错:", len(after.get("_console_errors", [])))
    print("JSON 已写入:", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
