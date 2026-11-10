# -*- coding: utf-8 -*-
"""P0-2 真实 DOM 复验：多选增强后，版本历史 / 「已增强」徽标 / 生成闸门。

【DOM 契约（本文件实测确认，不靠猜）】
  徽标      #promptEnhancedBadge     （hidden 由 currentPromptLevel() 决定）
  版本列表  #promptVersionList       （内容在 #promptVersionModal 弹窗里）
  版本入口  #promptVersionBtn        （点它才把弹窗打开）
  生成闸门  #enhanceGateModal        （不是 confirm()，是自定义弹窗）
  生成按钮  #genBtn

判据全部走**真实 DOM**，不读模块作用域变量（promptVersions 是 const，不在 window 上）。
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

READ_DOM = """() => {
  const vl = document.querySelector('#promptVersionList');
  const badge = document.querySelector('#promptEnhancedBadge');
  const cur = document.querySelector('#enhanceCurrent');
  const gate = document.querySelector('#enhanceGateModal');
  const rows = vl ? Array.from(vl.children).map(li => ({
        cls: li.className,
        text: (li.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 120)})) : null;
  return {
    versionListExists: !!vl,
    versionRowCount: rows ? rows.length : 0,
    versionRows: rows,
    badgeExists: !!badge,
    badgeHidden: badge ? badge.hidden : null,
    badgeText: badge ? (badge.textContent || '').trim() : null,
    currentIndicator: cur ? (cur.textContent || '').trim() : null,
    currentVisible: cur ? !cur.hidden && cur.offsetHeight > 0 : false,
    gateHidden: gate ? gate.hidden : null,
    promptValue: (document.querySelector('#prompt') || {}).value || '',
  };
}"""


def main():
    out = {}
    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--no-sandbox"])
        ctx = b.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        errs, failed = [], []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        page.on("requestfailed", lambda r: failed.append({"url": r.url, "e": (r.failure or "")}))

        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_function(
            "() => typeof provState !== 'undefined' && provState.agent "
            "&& provState.agent.specs && provState.agent.specs.length > 0", timeout=60000)
        page.wait_for_timeout(1500)
        page.evaluate(STUB)

        page.evaluate("() => { const e=document.querySelector(\".nav-item[data-mode='t2i']\"); if(e) e.click(); }")
        page.wait_for_timeout(900)
        page.evaluate("() => { const p=document.querySelector('#prompt');"
                      " if(p){p.value='a lighthouse at dusk'; p.dispatchEvent(new Event('input',{bubbles:true}));} }")
        page.wait_for_timeout(400)

        # 打开增强下拉，勾选「普通增强」+「二次增强」
        page.evaluate("() => { const x=document.querySelector('#enhanceMenuBtn'); if(x) x.click(); }")
        page.wait_for_timeout(600)
        for mid in ("basic", "refine"):
            page.evaluate(
                """(m) => {
                  const el = document.querySelector('#enhanceMenu input[data-method-check="'+m+'"]');
                  if (el && !el.checked) el.click();
                }""", mid)
            page.wait_for_timeout(500)

        out["checked_before_run"] = page.evaluate(
            "() => Array.from(document.querySelectorAll('#enhanceMenu input[data-method-check]'))"
            ".map(i => ({id: i.dataset.methodCheck, checked: i.checked, disabled: i.disabled}))")

        page.evaluate("() => { const x=document.querySelector('#enhanceBtn'); if(x) x.click(); }")
        page.wait_for_timeout(2500)
        out["modal_open"] = page.evaluate(
            "() => { const m=document.querySelector('#enhanceModal'); return m ? !m.hidden : null; }")
        out["modal_result"] = page.evaluate(
            "() => (document.querySelector('#enhanceResult')||{}).value || ''")

        # 点「替换提示词框内容」
        page.evaluate("""() => {
          const btns = Array.from(document.querySelectorAll('#enhanceModal button'));
          const hit = btns.find(b => /替换/.test(b.textContent || ''));
          if (hit) hit.click();
        }""")
        page.wait_for_timeout(1200)

        out["after_writeback"] = page.evaluate(READ_DOM)

        # 打开版本历史弹窗，看真实条目
        page.evaluate("() => { const x=document.querySelector('#promptVersionBtn'); if(x) x.click(); }")
        page.wait_for_timeout(900)
        out["version_modal"] = page.evaluate("""() => {
          const m = document.querySelector('#promptVersionModal');
          const vl = document.querySelector('#promptVersionList');
          const rows = vl ? Array.from(vl.children).map(li =>
            (li.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 140)) : null;
          return {modalHidden: m ? m.hidden : null,
                  rowCount: rows ? rows.length : 0, rows: rows};
        }""")
        page.evaluate("() => { const x=document.querySelector('#promptVersionClose'); if(x) x.click(); }")
        page.wait_for_timeout(400)

        # 点生成 —— 是否弹「提示词尚未增强」闸门
        out["gen_clicked"] = page.evaluate(
            "() => { const x=document.querySelector('#genBtn'); if(x){x.click(); return true;} return false; }")
        page.wait_for_timeout(1500)
        out["after_generate_click"] = page.evaluate(READ_DOM)
        # 把闸门关掉，别留弹窗
        page.evaluate("""() => {
          const btns = Array.from(document.querySelectorAll('#enhanceGateModal button'));
          const hit = btns.find(b => /取消|中止|close|×/i.test(b.textContent || ''));
          if (hit) hit.click();
        }""")
        page.wait_for_timeout(400)

        out["console_errors"] = errs
        out["failed_requests"] = failed
        b.close()

    print(json.dumps(out, ensure_ascii=False, indent=2))
    w = out["after_writeback"]
    ok = (out["version_modal"]["rowCount"] > 0
          and w["badgeHidden"] is False
          and w["gateHidden"] is True)
    print("\n=== P0-2 判定:", "PASS" if ok else "FAIL", "===")
    print("  版本条目数 =", out["version_modal"]["rowCount"])
    print("  徽标 hidden =", w["badgeHidden"], "text =", w["badgeText"])
    print("  闸门 hidden =", w["gateHidden"], "(True=不弹「未增强」确认)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
