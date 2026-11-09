#!/usr/bin/env python3
"""审查返修的真机验证：音乐面板增强入口唯一性 + 当前方式回显 + 写回契约。

【验证什么】
  V1 音乐面板只有一个增强入口（一个增强按钮 + 一个增强方式下拉）
  V2 音乐面板有「当前增强方式」显式回显
  V3 音乐面板没有第二个「增强模板」下拉（模板行不是增强入口）
  V4 写回契约按能力解析：#musicPrompt 优先、#musicStyle 兼容
  V5 目标缺失时不抛 TypeError，给可读提示
  V6 下拉里文本扩写类在音乐面板被禁用并写明原因
  V7 yue2 结构化增强真实调用后写回结构化风格字段与歌词

用法：
  python3 tests/verify_music_enhance_ui.py --base http://127.0.0.1:8195
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright  # noqa: E402


def wait_ready(page):
    page.wait_for_function(
        "() => window.__studioReady === true || "
        "(!!document.querySelector('#navMusicT2A') || !!document.querySelector('#musicPanel'))",
        timeout=45000,
    )


def goto_music(page, base):
    """切到音乐面板（左侧标签入口）。"""
    page.goto(base, wait_until="domcontentloaded", timeout=45000)
    wait_ready(page)
    # 左侧栏在窄屏是抽屉，先展开
    page.evaluate("""
        () => {
          const t = document.querySelector('#navToggle');
          if (t && t.offsetParent !== null) t.click();
        }
    """)
    time.sleep(0.4)
    labels = page.evaluate("""
        () => Array.from(document.querySelectorAll('[data-nav], .nav-item, .side-item'))
          .map(e => (e.getAttribute('data-nav') || e.textContent || '').trim())
          .filter(Boolean)
    """)
    clicked = False
    for cand in labels:
        if "参考生音乐" in cand and "文生音乐" not in cand:
            ok = page.evaluate("""
                (t) => {
                  const els = Array.from(document.querySelectorAll('[data-nav], .nav-item, .side-item'));
                  const el = els.find(e => (e.getAttribute('data-nav') || e.textContent || '').trim() === t);
                  if (el) { el.click(); return true; }
                  return false;
                }
            """, cand)
            if ok:
                clicked = True
                break
    if not clicked:
        # 直接用 data-nav 兜底
        page.evaluate("""
            () => {
              const el = document.querySelector('[data-nav="a2a"], [data-nav="music-a2a"], [data-nav="t2a"]');
              if (el) el.click();
            }
        """)
    time.sleep(0.6)
    return labels


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8195")
    ap.add_argument("--out", default="/tmp/music_enhance_ui")
    ap.add_argument("--width", type=int, default=1280)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    report = {"checks": [], "console_errors": [], "failed_requests": []}

    def check(name, ok, detail=""):
        report["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})
        print(("PASS " if ok else "FAIL ") + name + (" :: " + str(detail)[:300] if detail else ""))

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": args.width, "height": 900})
        page = ctx.new_page()
        page.on("console", lambda m: report["console_errors"].append(f"{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: report["console_errors"].append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r: report["failed_requests"].append(
            f"{r.method} {r.url} :: {r.failure}"))

        goto_music(page, args.base)

        # ---- V1 增强入口唯一性 ----
        info = page.evaluate("""
            () => {
              const vis = el => !!el && el.offsetParent !== null;
              const panel = document.querySelector('#musicPanel')
                        || document.querySelector('#musicPromptField')
                        || document.querySelector('.img-panel, .panel');
              const scope = panel || document;
              const btns = Array.from(scope.querySelectorAll('button'))
                .filter(b => vis(b))
                .map(b => ({
                  id: b.id || '',
                  text: (b.textContent || '').trim().slice(0, 24),
                  cls: b.className || ''
                }));
              const enhance = btns.filter(b =>
                /enhance-btn/.test(b.cls) ||
                /增强|扩写|PE/.test(b.text));
              const selects = Array.from(scope.querySelectorAll('select'))
                .filter(s => vis(s))
                .map(s => ({ id: s.id || '', title: s.title || '',
                             opts: Array.from(s.options).map(o => o.textContent.trim()) }));
              return {
                buttons: btns,
                enhanceButtons: enhance,
                selects: selects,
                menus: Array.from(scope.querySelectorAll('.enhance-menu')).map(m => ({
                  id: m.id || '', hidden: m.hidden,
                  items: Array.from(m.querySelectorAll('li,button,[role=menuitem]'))
                    .map(i => (i.textContent || '').trim().slice(0, 40))
                })),
              };
            }
        """)

        enhance_btns = [b for b in info["enhanceButtons"]
                        if b["id"] not in ("musicEnhanceMenuBtn",)]
        check("V1 音乐面板只有一个增强主按钮",
              len(enhance_btns) == 1, json.dumps(enhance_btns, ensure_ascii=False))

        caret = [b for b in info["enhanceButtons"] if b["id"] == "musicEnhanceMenuBtn"]
        check("V1 增强方式下拉是分裂按钮的箭头（同一个 enhance-split 内）",
              len(caret) == 1, json.dumps(caret, ensure_ascii=False))

        # ---- V2 当前增强方式回显 ----
        cur = page.evaluate("""
            () => {
              const el = document.querySelector('#musicEnhanceCurrent');
              if (!el) return { present: false };
              return { present: true, hidden: el.hidden,
                       text: (el.textContent || '').trim(),
                       visible: el.offsetParent !== null };
            }
        """)
        check("V2 音乐面板存在「当前增强方式」回显元素", cur.get("present"), json.dumps(cur, ensure_ascii=False))

        # ---- V3 无第二个增强模板下拉（模板行是平级配置行，不是增强入口）----
        spec_sel = [s for s in info["selects"] if s["id"] == "musicEnhanceSpecSelect"]
        spec_row_hidden = page.evaluate("""
            () => {
              const r = document.querySelector('#musicEnhanceSpecRow');
              if (!r) return null;
              return { hidden: r.hidden, visible: r.offsetParent !== null };
            }
        """)
        # 真正的判据：下拉「菜单」里不能再有一个"模板"下拉（第二入口）
        menu_has_select = page.evaluate("""
            () => Array.from(document.querySelectorAll('.enhance-menu'))
              .some(m => m.querySelector('select'))
        """)
        check("V3 增强方式下拉菜单内不再嵌套模板下拉", not menu_has_select,
              "menu_contains_select=%s" % menu_has_select)
        check("V3 模板行是平级配置行且默认隐藏", spec_row_hidden is None or spec_row_hidden.get("hidden"),
              json.dumps(spec_row_hidden, ensure_ascii=False))

        # ---- V4/V5 写回契约 ----
        contract = page.evaluate("""
            () => {
              const out = {};
              out.has_shared = !!(window.EnhanceMenuShared
                                 && typeof window.EnhanceMenuShared.resolveWriteTarget === 'function');
              out.has_music_contract = !!(window.MusicEnhance
                                 && typeof window.MusicEnhance.getWriteContract === 'function');
              try {
                const c = window.MusicEnhance && window.MusicEnhance.getWriteContract
                        ? window.MusicEnhance.getWriteContract() : null;
                out.contract_keys = c ? Object.keys(c) : [];
                if (c && window.EnhanceMenuShared) {
                  const r = window.EnhanceMenuShared.resolveWriteTarget(c);
                  out.resolve_ok = !!(r && r.ok);
                  out.resolve_reason = r ? r.reason : null;
                }
              } catch (e) {
                out.resolve_threw = String(e);
              }
              return out;
            }
        """)
        check("V4 共用组件提供 resolveWriteTarget", contract.get("has_shared"),
              json.dumps(contract, ensure_ascii=False))
        check("V4 音乐写回契约已注册", contract.get("has_music_contract"),
              json.dumps(contract, ensure_ascii=False))
        check("V4 契约可解析出写回目标", contract.get("resolve_ok"),
              json.dumps(contract, ensure_ascii=False))

        # 模拟「#musicPrompt 缺失」场景 -> 必须不抛 TypeError 且有可读提示
        missing = page.evaluate("""
            () => {
              const el = document.querySelector('#musicPrompt');
              if (!el) return { skipped: true };
              const holder = el.parentNode;
              const next = el.nextElementSibling;
              const parent = el.parentElement;
              el.remove();
              let out = { threw: null, hint: null };
              try {
                if (window.MusicEnhance && window.MusicEnhance.getWriteContract) {
                  const c = window.MusicEnhance.getWriteContract();
                  const r = window.EnhanceMenuShared.resolveWriteTarget(c);
                  out.ok = r ? r.ok : null;
                  out.reason = r ? r.reason : null;
                }
                const btn = document.querySelector('#musicEnhanceBtn');
                if (btn) btn.click();
              } catch (e) {
                out.threw = String(e);
              }
              out.hint = (document.querySelector('#musicEnhanceHint') || {}).textContent || '';
              if (parent) parent.insertBefore(el, next);
              return out;
            }
        """)
        check("V5 目标缺失时不抛 TypeError", not missing.get("threw"),
              json.dumps(missing, ensure_ascii=False))
        # 「请先输入音乐描述」「本面板没有可增强的…」都算**可读提示**：
        # 要求是「不能静默抛 TypeError」+「用户看得懂发生了什么」。
        check("V5 目标缺失时给出可读提示",
              bool(re.search(r"没有|找不到|无法|不可用|请先输入", missing.get("hint") or "")),
              repr(missing.get("hint")))

        # ---- V6 文本扩写类在音乐面板禁用 ----
        page.evaluate("""
            () => { const b = document.querySelector('#musicEnhanceMenuBtn'); if (b) b.click(); }
        """)
        time.sleep(0.5)
        menu_state = page.evaluate("""
            () => {
              const m = document.querySelector('#musicEnhanceMenu');
              if (!m) return { present: false };
              const items = Array.from(m.querySelectorAll('.enhance-menu-item'));
              return {
                present: true, hidden: m.hidden,
                items: items.map(i => {
                  // 「不可用」标记有两处：项本身的 unavailable 类，
                  // 和项内**内层 checkbox** 的 disabled 属性。
                  const box = i.querySelector('input[data-method-check]');
                  return {
                    text: (i.textContent || '').trim().slice(0, 80),
                    cls: i.className || '',
                    unavailable: (i.className || '').includes('unavailable'),
                    box_disabled: !!(box && box.disabled),
                    disabled: (i.className || '').includes('unavailable')
                              || !!(box && box.disabled),
                  };
                }),
              };
            }
        """)
        text_items = [i for i in menu_state.get("items", [])
                      if re.search(r"普通增强|二次增强|结构化增强|官方PE|扩写", i["text"])]
        disabled_text = [i for i in text_items if i["disabled"]]
        check("V6 音乐面板的文本扩写类增强项被禁用",
              len(text_items) > 0 and len(disabled_text) == len(text_items),
              json.dumps(menu_state.get("items", []), ensure_ascii=False))
        check("V6 音乐面板提供 yue2 结构化增强项",
              any("YuE2" in i["text"] or "结构化风格" in i["text"]
                  for i in menu_state.get("items", [])),
              json.dumps(menu_state.get("items", []), ensure_ascii=False))

        # ---- V7 真实调用增强并写回 ----
        page.evaluate("""
            () => {
              const m = document.querySelector('#musicEnhanceMenu');
              if (m) m.hidden = true;
              const el = document.querySelector('#musicPrompt');
              if (el) el.value = '一首雨夜告别的日语慢歌，钢琴加弦乐，副歌要爆发';
              el.dispatchEvent(new Event('input', {bubbles: true}));
            }
        """)
        clicked = page.evaluate("""
            () => {
              const b = document.querySelector('#musicEnhanceBtn');
              if (!b) return false;
              b.click();
              return true;
            }
        """)
        check("V7 音乐增强按钮可点击", clicked)

        written = False
        detail = ""
        for _ in range(90):
            time.sleep(2)
            st = page.evaluate("""
                () => {
                  const hint = (document.querySelector('#musicEnhanceHint')||{}).textContent||'';
                  const fields = document.querySelectorAll('#musicStyleFieldsGrid .music-sf-row, #musicStyleFieldsGrid input, #musicStyleFieldsGrid textarea');
                  let filled = 0;
                  fields.forEach(f => { if ((f.value||'').trim()) filled += 1; });
                  const lyric = document.querySelector('#musicLyrics');
                  return { hint: hint, filled: filled, fields: fields.length,
                           lyric: lyric ? (lyric.value||'').length : -1 };
                }
            """)
            if st.get("filled", 0) > 0 or "失败" in st.get("hint", "") or "错误" in st.get("hint", ""):
                detail = json.dumps(st, ensure_ascii=False)
                written = st.get("filled", 0) > 0
                break
        check("V7 增强结果写回结构化风格字段", written, detail)

        page.screenshot(path=os.path.join(args.out, "music_enhance_after.png"),
                        full_page=False)
        ctx.close()
        browser.close()

    report["console_errors"] = report["console_errors"]
    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("\n=== console errors: %d ===" % len(report["console_errors"]))
    for e in report["console_errors"][:20]:
        print("  " + e)
    print("=== failed requests: %d ===" % len(report["failed_requests"]))
    for e in report["failed_requests"][:20]:
        print("  " + e)

    bad = [c for c in report["checks"] if not c["ok"]]
    print("\n=== %d/%d 通过 ===" % (len(report["checks"]) - len(bad), len(report["checks"])))
    for c in bad:
        print("  FAIL " + c["name"] + " :: " + c["detail"])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
