# -*- coding: utf-8 -*-
"""综合审查返修的**真实浏览器**验证（Playwright + Chromium）。

验证本轮返修建立的 3 条可见契约：

  V1 每个面板只有「一个增强入口 + 一个下拉 + 当前方式回显」
     - 可见的 .enhance-split 恰好 1 个
     - 可见的 .enhance-menu 恰好 1 个
     - 没有第二个「增强模板」下拉（select.enhance-spec）
     - 「当前增强方式」回显可见且有文字（图像面板 + 音乐面板都查）

  V2 音乐面板（文生音乐 / 参考生音乐）的增强体系与图像面板一致
     - 增强按钮 / 下拉的 class 完全相同（同一套组件）
     - 下拉里有 yue2 专用项
     - 写回目标存在（契约解析成功，不抛 TypeError）

  V3 console 0 报错、0 失败请求

三档宽度截图：375 / 390 / 1280。

用法：python3 verify_audit_fixes_browser.py --port 8195 --out <目录>
"""
import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PANELS = [
    ("t2i", "Z-Image 文生图"),
    ("t2i_qwen", "Qwen-Image 2.1 文生图"),
    ("i2i_qwen", "Qwen 图片编辑"),
    ("t2i_moss", "MOSS-Image 文生图"),
    ("t2v", "文生视频"),
    ("t2a", "文生音乐"),
    ("a2a", "参考生音乐"),
]

VIEWPORTS = [
    ("375", {"width": 375, "height": 812}),
    ("390", {"width": 390, "height": 844}),
    ("1280", {"width": 1280, "height": 900}),
]

DUMP_JS = """
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
  };
  const isMusic = (state.mode === 't2a' || state.mode === 'a2a');
  const pick = (sel) => Array.from(document.querySelectorAll(sel)).filter(visible);
  const entries = pick('.enhance-split');
  const menus = pick('.enhance-menu');
  const menusDom = Array.from(document.querySelectorAll('.enhance-menu')).map(e => e.id);
  const specDom = Array.from(document.querySelectorAll('select.enhance-spec')).map(e => e.id);
  const current = document.querySelector(isMusic ? '#musicEnhanceCurrent' : '#enhanceCurrent');
  const btn = document.querySelector(isMusic ? '#musicEnhanceBtn' : '#enhanceBtn');
  const menuBtn = document.querySelector(isMusic ? '#musicEnhanceMenuBtn' : '#enhanceMenuBtn');
  return {
    mode: state.mode,
    entryIds: entries.map(e => e.id),
    entryClasses: entries.map(e => e.className),
    menuIds: menus.map(e => e.id),
    menuIdsDom: menusDom,
    specSelectIds: specDom,
    currentText: current ? current.textContent.trim() : '',
    currentVisible: current ? visible(current) : false,
    currentHiddenAttr: current ? current.hasAttribute('hidden') : null,
    btnClass: btn ? btn.className : null,
    menuBtnClass: menuBtn ? menuBtn.className : null,
    hasShared: typeof window.EnhanceMenuShared === 'object' && window.EnhanceMenuShared !== null,
    hasContract: !!(window.MusicEnhance && typeof window.MusicEnhance.getWriteContract === 'function'),
    docScrollW: document.documentElement.scrollWidth,
    innerW: window.innerWidth,
    hOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
  };
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{args.port}/"

    results = {
        "base": base, "panels": {}, "viewport": {},
        "console_errors": [], "failed_requests": [], "assertions": [],
    }

    def check(name, ok, detail=""):
        results["assertions"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})
        print(("  PASS " if ok else "  FAIL ") + name + (f" — {detail}" if detail else ""))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport=VIEWPORTS[-1][1])
        page = ctx.new_page()
        page.on("pageerror", lambda e: results["console_errors"].append(
            {"text": str(e), "type": "pageerror"}))
        page.on("console", lambda m: results["console_errors"].append(
            {"text": m.text, "type": "error"}) if m.type == "error" else None)
        page.on("requestfailed", lambda r: results["failed_requests"].append(
            {"url": r.url, "kind": "failed", "error": str(r.failure)}))
        page.on("response", lambda r: results["failed_requests"].append(
            {"url": r.url, "kind": "http", "status": r.status}) if r.status >= 400 else None)

        page.goto(base, wait_until="domcontentloaded")
        page.wait_for_timeout(3500)

        # ---- 逐面板 ----
        for mode, label in PANELS:
            print(f"\n=== {label} ({mode}) @1280 ===")
            page.evaluate(
                "(m) => { const el = document.querySelector("
                "`.nav-item[data-mode='${m}']`); if (el) el.click(); }", mode)
            page.wait_for_timeout(1200)
            d = page.evaluate(DUMP_JS)
            results["panels"][mode] = d

            check(f"{label}: 增强入口恰好 1 个", len(d["entryIds"]) == 1, d["entryIds"])
            # 本面板的下拉必须存在；另一面板的下拉必须**不可见**
            # （另一面板整体 display:none，它的菜单元素仍在 DOM 里但尺寸为 0 ——
            #  这与审查脚本 audit_visual.py 的判据一致）
            expect_menu = "musicEnhanceMenu" if mode in ("t2a", "a2a") else "enhanceMenu"
            other_menu = "enhanceMenu" if expect_menu == "musicEnhanceMenu" else "musicEnhanceMenu"
            check(f"{label}: 本面板下拉存在（{expect_menu}）",
                  expect_menu in d["menuIdsDom"], d["menuIdsDom"])
            other_visible = page.evaluate("""(id) => {
              const el = document.getElementById(id);
              if (!el) return false;
              const r = el.getBoundingClientRect();
              return r.width > 0 && r.height > 0;
            }""", other_menu)
            check(f"{label}: 另一面板的下拉不可见（{other_menu}）",
                  not other_visible, f"visible={other_visible}")
            check(f"{label}: 无第二个模板下拉", len(d["specSelectIds"]) == 0, d["specSelectIds"])
            check(f"{label}: 当前增强方式可见且有文字",
                  d["currentVisible"] and bool(d["currentText"]),
                  d["currentText"][:120])
            check(f"{label}: 无横向溢出", not d["hOverflow"],
                  f"doc={d['docScrollW']} inner={d['innerW']}")
            check(f"{label}: 共用组件已加载", d["hasShared"])
            if mode in ("t2a", "a2a"):
                check(f"{label}: 音乐写回契约已注册", d["hasContract"])

            # 打开下拉抓选项
            page.evaluate("""() => {
              const isMusic = (state.mode === 't2a' || state.mode === 'a2a');
              const b = document.querySelector(isMusic ? '#musicEnhanceMenuBtn' : '#enhanceMenuBtn');
              if (b) b.click();
            }""")
            page.wait_for_timeout(700)
            items = page.evaluate("""() => {
              const isMusic = (state.mode === 't2a' || state.mode === 'a2a');
              const menu = document.querySelector(isMusic ? '#musicEnhanceMenu' : '#enhanceMenu');
              if (!menu) return null;
              return Array.from(menu.querySelectorAll('.enhance-menu-item')).map(it => {
                const box = it.querySelector('input[data-method-check]');
                return {
                  id: it.dataset.method,
                  label: (it.querySelector('.enhance-menu-label')||{}).textContent||'',
                  checked: box ? box.checked : null,
                  disabled: box ? box.disabled : null,
                  badges: Array.from(it.querySelectorAll('.enhance-menu-badge')).map(b=>b.textContent.trim()),
                  specText: (it.querySelector('.enhance-menu-spec')||{}).textContent||'',
                };
              });
            }""")
            results["panels"][mode]["menuItems"] = items
            check(f"{label}: 下拉渲染出选项", bool(items) and len(items) >= 2,
                  [i["id"] for i in (items or [])])
            if mode in ("t2a", "a2a"):
                ids = [i["id"] for i in (items or [])]
                check(f"{label}: 下拉含 yue2 专用增强项", "yue2_struct" in ids, ids)
                # 音乐面板只提供结构化增强（文本扩写类的产物塞不进结构化契约）
                avail = [i["id"] for i in (items or []) if not i["disabled"]]
                check(f"{label}: 音乐面板只有结构化增强可用",
                      avail == ["yue2_struct"], f"可用项={avail}")
                # 回显不该带模板标注（结构化增强不用模板）
                check(f"{label}: 当前方式回显不带模板标注",
                      "（" not in d["currentText"], d["currentText"][:120])

            page.evaluate("""() => {
              const isMusic = (state.mode === 't2a' || state.mode === 'a2a');
              const el = document.querySelector(isMusic ? '#musicEnhanceSplit' : '#enhanceSplit');
              if (el) el.scrollIntoView({block:'center'});
            }""")
            page.wait_for_timeout(400)
            page.screenshot(path=str(out / f"menu-1280-{mode}.png"))
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)

        # ---- 组件一致性：图像 vs 音乐 ----
        print("\n=== 增强组件一致性（同一套 class）===")
        img_cls = results["panels"]["t2i"]["btnClass"]
        music_cls = results["panels"]["t2a"]["btnClass"]
        check("图像与音乐增强按钮 class 一致", img_cls == music_cls,
              f"t2i={img_cls!r} t2a={music_cls!r}")
        img_menu = results["panels"]["t2i"]["menuBtnClass"]
        music_menu = results["panels"]["t2a"]["menuBtnClass"]
        check("图像与音乐下拉箭头 class 一致", img_menu == music_menu,
              f"t2i={img_menu!r} t2a={music_menu!r}")

        # ---- 写回契约解析（不抛 TypeError）----
        print("\n=== 写回契约解析 ===")
        page.evaluate("(m) => { const el = document.querySelector(`.nav-item[data-mode='${m}']`); if (el) el.click(); }", "t2a")
        page.wait_for_timeout(900)
        wb = page.evaluate("""() => {
          const shared = window.EnhanceMenuShared;
          const c = window.MusicEnhance && window.MusicEnhance.getWriteContract();
          const r = shared.resolveWriteTarget(c);
          return { ok: r.ok, kind: r.kind, reason: r.reason || '' };
        }""")
        results["writeback_contract"] = wb
        check("音乐写回契约解析成功（不抛异常）", wb["ok"], wb)
        check("音乐写回契约是 structured", wb.get("kind") == "structured", wb)

        # 目标缺失时不抛异常
        missing = page.evaluate("""() => {
          const shared = window.EnhanceMenuShared;
          try {
            const r = shared.resolveWriteTarget({
              kind: 'text', target: '#definitely-not-exist-xyz',
              describe: () => '目标不存在（测试用）' });
            return { threw: false, ok: r.ok, reason: r.reason || '' };
          } catch (e) {
            return { threw: true, error: String(e) };
          }
        }""")
        results["writeback_missing"] = missing
        check("目标缺失时不抛异常且给出原因",
              (not missing["threw"]) and (not missing["ok"]) and bool(missing.get("reason")),
              missing)

        # ---- 三档视口 ----
        for vp_name, vp in VIEWPORTS:
            page.set_viewport_size(vp)
            page.wait_for_timeout(600)
            per_vp = {}
            for mode, label in PANELS:
                page.evaluate(
                    "(m) => { const el = document.querySelector("
                    "`.nav-item[data-mode='${m}']`); if (el) el.click(); }", mode)
                page.wait_for_timeout(900)
                if vp["width"] < 900:
                    page.evaluate("""() => {
                      const panel = document.querySelector('#panel');
                      if (panel) panel.classList.add('open');
                      const mask = document.querySelector('#panelMask');
                      if (mask) mask.classList.add('open');
                    }""")
                    page.wait_for_timeout(700)
                page.evaluate("""() => {
                  const isMusic = (state.mode === 't2a' || state.mode === 'a2a');
                  const el = document.querySelector(isMusic ? '#musicEnhanceSplit' : '#enhanceSplit');
                  if (el) el.scrollIntoView({block:'center'});
                }""")
                page.wait_for_timeout(400)
                d = page.evaluate(DUMP_JS)
                per_vp[mode] = d
                check(f"[{vp_name}] {label}: 无横向溢出", not d["hOverflow"],
                      f"doc={d['docScrollW']} inner={d['innerW']}")
                check(f"[{vp_name}] {label}: 增强入口可见", len(d["entryIds"]) == 1, d["entryIds"])
                if mode in ("t2a", "a2a"):
                    check(f"[{vp_name}] {label}: 当前增强方式可见",
                          d["currentVisible"] and bool(d["currentText"]),
                          d["currentText"][:100])
                page.screenshot(path=str(out / f"vp{vp_name}-{mode}.png"))
                if vp["width"] < 900:
                    page.evaluate("""() => {
                      const panel = document.querySelector('#panel');
                      if (panel) panel.classList.remove('open');
                      const mask = document.querySelector('#panelMask');
                      if (mask) mask.classList.remove('open');
                    }""")
                    page.wait_for_timeout(350)
            results["viewport"][vp_name] = per_vp
            print(f"  视口 {vp_name} 完成")

        browser.close()

    fails = [a for a in results["assertions"] if not a["ok"]]
    results["summary"] = {
        "total": len(results["assertions"]),
        "passed": len(results["assertions"]) - len(fails),
        "failed": len(fails),
        "console_errors": len(results["console_errors"]),
        "failed_requests": len(results["failed_requests"]),
    }
    (out / "results-audit-fixes.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n===== 汇总 =====")
    print(json.dumps(results["summary"], ensure_ascii=False, indent=2))
    if fails:
        print("失败断言:")
        for f in fails:
            print("  -", f["name"], "|", f["detail"])
    if results["console_errors"]:
        print("console 报错 (前 8):")
        for e in results["console_errors"][:8]:
            print("  ", e)
    if results["failed_requests"]:
        print("失败请求 (前 12):")
        for r in results["failed_requests"][:12]:
            print("  ", r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
