# -*- coding: utf-8 -*-
"""P0-2 回归护栏：**单选**增强路径不许回退。

P0-2 的修法是「多选也写版本历史」。修完之后必须确认单选仍然正常：
  - 普通增强（basic）单选 -> 版本历史有「原始 + 一次增强」，徽标「一次增强」
  - 二次增强（refine）单选 -> 版本历史有「原始 + 二次增强」，徽标「二次增强」
  - 生成时不再弹「提示词尚未增强」闸门
"""
import json
import sys
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8291/"

STUB = """
(() => {
  const origFetch = window.fetch;
  window.fetch = async function (url, opts) {
    const u = String(url);
    if (u.indexOf('/api/enhance') >= 0) {
      const body = JSON.parse((opts && opts.body) || '{}');
      const txt = '【L1 特写】桩产出\\n【L2 中景】桩\\n【主体细化】桩';
      return new Response(JSON.stringify({
        enhanced: txt, provider: '桩', spec_id: body.spec_id || 'image'}),
        {status: 200, headers: {'Content-Type': 'application/json'}});
    }
    return origFetch.apply(this, arguments);
  };
})();
"""

READ = """() => {
  const vl = document.querySelector('#promptVersionList');
  const badge = document.querySelector('#promptEnhancedBadge');
  const cur = document.querySelector('#enhanceCurrent');
  const gate = document.querySelector('#enhanceGateModal');
  const rows = vl ? Array.from(vl.children).map(li =>
      (li.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 90)) : [];
  return {
    rowCount: rows.length, rows: rows,
    badgeHidden: badge ? badge.hidden : null,
    badgeText: badge ? (badge.textContent || '').trim() : null,
    currentText: cur ? (cur.textContent || '').trim() : null,
    gateHidden: gate ? gate.hidden : null,
    promptValue: (document.querySelector('#prompt') || {}).value || '',
  };
}"""


def run_single(page, method):
    """只勾一项并执行，返回写回后的真实 DOM 状态。"""
    # 清场：回到原始提示词
    page.evaluate("""() => {
      const p = document.querySelector('#prompt');
      if (p) { p.value = 'a lighthouse at dusk';
               p.dispatchEvent(new Event('input', {bubbles: true})); }
    }""")
    page.wait_for_timeout(300)

    page.evaluate("() => { const x=document.querySelector('#enhanceMenuBtn'); if(x) x.click(); }")
    page.wait_for_timeout(500)
    # 取消其它项，只留 method。
    # 【注意】每次勾选都会**重渲染整个菜单**（renderMenu），所以旧 element handle
    # 会失效 —— 必须**每次重新 query**，否则点不动（这正是我第一版测试的 bug）。
    #
    # 【产品约束】toggleMethod 规定「至少保留一项」——
    # 所以「只剩 refine」的正确操作顺序是：先勾 refine，再取消 basic。
    # 反过来（先取消 basic）会被产品挡下，selectedIds 里仍有 basic。
    if method != "basic":
        page.evaluate("""(id) => {
          const el = document.querySelector(
            '#enhanceMenu input[data-method-check="' + id + '"]');
          if (el && !el.checked) el.click();
        }""", method)
        page.wait_for_timeout(600)
    for _round in range(8):
        state = page.evaluate(
            "() => Array.from(document.querySelectorAll('#enhanceMenu input[data-method-check]'))"
            ".map(i => ({id: i.dataset.methodCheck, checked: i.checked}))")
        wrong = [s["id"] for s in state
                 if s["checked"] != (s["id"] == method) and s["id"] in ("basic", "refine")]
        if not wrong:
            break
        page.evaluate("""(id) => {
          const el = document.querySelector(
            '#enhanceMenu input[data-method-check="' + id + '"]');
          if (el && el.checked) el.click();
        }""", wrong[0])
        page.wait_for_timeout(500)
    page.wait_for_timeout(400)
    sel_state = page.evaluate(
        "() => Array.from(document.querySelectorAll('#enhanceMenu input[data-method-check]'))"
        ".map(i => ({id: i.dataset.methodCheck, checked: i.checked}))")
    print("   [%s] 勾选状态 = %s" % (method, sel_state))
    assert sum(1 for s in sel_state if s["checked"]) == 1, \
        "单选场景必须恰好 1 项被勾选，实际 %s" % sel_state

    page.evaluate("() => { const x=document.querySelector('#enhanceBtn'); if(x) x.click(); }")
    page.wait_for_timeout(2500)
    page.evaluate("""() => {
      const btns = Array.from(document.querySelectorAll('#enhanceModal button'));
      const hit = btns.find(b => /替换/.test(b.textContent || ''));
      if (hit) hit.click();
    }""")
    page.wait_for_timeout(900)

    page.evaluate("() => { const x=document.querySelector('#promptVersionBtn'); if(x) x.click(); }")
    page.wait_for_timeout(700)
    st = page.evaluate(READ)
    page.evaluate("() => { const x=document.querySelector('#promptVersionClose'); if(x) x.click(); }")
    page.wait_for_timeout(300)
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    return st


def main():
    out = {}
    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--no-sandbox"])
        ctx = b.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_function(
            "() => typeof provState !== 'undefined' && provState.agent "
            "&& provState.agent.specs && provState.agent.specs.length > 0", timeout=60000)
        page.wait_for_timeout(1500)
        page.evaluate(STUB)
        page.evaluate("() => { const e=document.querySelector(\".nav-item[data-mode='t2i']\"); if(e) e.click(); }")
        page.wait_for_timeout(900)

        out["basic"] = run_single(page, "basic")
        out["refine"] = run_single(page, "refine")
        out["console_errors"] = errs
        b.close()

    print(json.dumps(out, ensure_ascii=False, indent=2))

    ok = True
    for name, want_badge, min_rows in (("basic", "一次增强", 2), ("refine", "二次增强", 2)):
        st = out[name]
        c1 = st["rowCount"] >= min_rows
        c2 = st["badgeHidden"] is False and st["badgeText"] == want_badge
        c3 = st["gateHidden"] is True
        c4 = bool(st["promptValue"])
        print("[%s] %s: rows=%d(>=%d) badge=%s/%s gateHidden=%s promptWritten=%s" % (
            "PASS" if (c1 and c2 and c3 and c4) else "FAIL", name,
            st["rowCount"], min_rows, st["badgeText"], st["badgeHidden"],
            st["gateHidden"], c4))
        ok = ok and c1 and c2 and c3 and c4

    print("\n=== 单选路径回归判定:", "PASS" if ok else "FAIL", "===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
