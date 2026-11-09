# -*- coding: utf-8 -*-
"""统一增强体系 —— 返修后的真机多视口验证脚本。

用真实 Chromium 打开开发实例，逐面板 × 逐视口验证：
  1. 增强下拉在 375 / 390 / 1280 三档下**完整可见**（P1-1）
  2. 多选增强后版本历史非空 + 「已增强」徽标可见（P1-2）
  3. 禁用项只有一套文案（不含 Recommended）（P3-1）
  4. 模板选择器是增强栏里的**平级一行**（代码审查 P1-2）
  5. 模板选择在跨模式后失效（代码审查 P1-2）
  6. console 报错 / 失败请求记录

同时输出截图到 evidence 目录。

用法：
    python3 tests/verify_enh_unify_review_live.py --base http://127.0.0.1:8291 \
        --out <截图目录>
"""
import argparse
import json
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except Exception as exc:                                    # pragma: no cover
    print(f"playwright 缺失：{exc}")
    sys.exit(2)

# 面板 -> (模式, 增强下拉按钮 id, 菜单 id, 模板行 id, 模板 select id)
PANELS = {
    "t2i":      ("t2i",      "#enhanceMenuBtn", "#enhanceMenu", "#enhanceSpecRow", "#enhanceSpecSelect"),
    "t2i_qwen": ("t2i_qwen", "#enhanceMenuBtn", "#enhanceMenu", "#enhanceSpecRow", "#enhanceSpecSelect"),
    "i2i_qwen": ("i2i_qwen", "#enhanceMenuBtn", "#enhanceMenu", "#enhanceSpecRow", "#enhanceSpecSelect"),
    "t2i_moss": ("t2i_moss", "#enhanceMenuBtn", "#enhanceMenu", "#enhanceSpecRow", "#enhanceSpecSelect"),
    "t2v":      ("t2v",      "#enhanceMenuBtn", "#enhanceMenu", "#enhanceSpecRow", "#enhanceSpecSelect"),
    "t2a":      ("t2a",      "#musicEnhanceMenuBtn", "#musicEnhanceMenu",
                 "#musicEnhanceSpecRow", "#musicEnhanceSpecSelect"),
    "a2a":      ("a2a",      "#musicEnhanceMenuBtn", "#musicEnhanceMenu",
                 "#musicEnhanceSpecRow", "#musicEnhanceSpecSelect"),
}
VIEWPORTS = [(375, 780), (390, 844), (1280, 900)]


class Checker:
    def __init__(self):
        self.failures = []
        self.passed = 0
        self.console_errors = []
        self.failed_requests = []

    def check(self, ok, label, detail=""):
        if ok:
            self.passed += 1
        else:
            self.failures.append(f"{label} :: {detail}")
        return ok


def wait_ready(page, timeout_ms=25000):
    page.wait_for_function(
        """() => {
          try {
            return typeof provState !== 'undefined'
              && provState && provState.agent
              && Array.isArray(provState.agent.specs)
              && provState.agent.specs.length > 0
              && Object.keys(provState.modeFilters || {}).length > 0
              && Object.keys(provState.refineSpecs || {}).length > 0;
          } catch (e) { return false; }
        }""",
        timeout=timeout_ms)
    page.wait_for_timeout(400)


def switch_mode(page, mode):
    page.evaluate(
        "(m) => { const el = document.querySelector(`.nav-item[data-mode='${m}']`);"
        "if (el) el.click(); }", mode)
    page.wait_for_timeout(800)


def open_menu(page, menu_btn):
    page.evaluate(
        "(sel) => { const b = document.querySelector(sel); if (b) b.click(); }",
        menu_btn)
    page.wait_for_timeout(500)


def button_geometry(page, menu_btn):
    return page.evaluate(
        """(sel) => {
          const b = document.querySelector(sel);
          if (!b) return null;
          const r = b.getBoundingClientRect();
          return {top: Math.round(r.top), bottom: Math.round(r.bottom),
                  left: Math.round(r.left), onScreen:
                    r.top >= 0 && r.bottom <= window.innerHeight};
        }""", menu_btn)


def menu_geometry(page, menu_id):
    return page.evaluate(
        """(menuId) => {
          const m = document.querySelector(menuId);
          if (!m) return null;
          const r = m.getBoundingClientRect();
          const items = Array.from(m.querySelectorAll('.enhance-menu-item'));
          return {
            hidden: m.hidden,
            parentIsBody: m.parentElement === document.body,
            parentId: m.parentElement ? (m.parentElement.id ||
                       m.parentElement.tagName) : '',
            top: Math.round(r.top),
            bottom: Math.round(r.bottom),
            left: Math.round(r.left),
            right: Math.round(r.right),
            h: Math.round(r.height),
            w: Math.round(r.width),
            innerH: window.innerHeight,
            innerW: window.innerWidth,
            itemCount: items.length,
            items: items.map(it => ({
              method: it.dataset.method,
              unavailable: it.classList.contains('unavailable'),
              disabled: !!(it.querySelector('input[type=checkbox]') || {}).disabled,
              badges: Array.from(it.querySelectorAll('.enhance-menu-badge'))
                        .map(b => b.textContent.trim()),
            })),
          };
        }""", menu_id)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8291/")
    ap.add_argument("--out", default="enh-unify-review-evidence")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results = {"panels": {}, "multiselect": {}, "spec_row": {}, "console_errors": [],
               "failed_requests": [], "summary": {}}

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.on("console", lambda m: results["console_errors"].append(m.text)
                if m.type == "error" else None)
        page.on("pageerror", lambda e: results["console_errors"].append(str(e)))
        page.on("requestfailed", lambda r: results["failed_requests"].append({
            "url": r.url,
            "error": (r.failure or "")}))

        page.goto(args.base, wait_until="domcontentloaded")
        wait_ready(page)

        ck = Checker()

        # ---------- 逐面板 × 逐视口：下拉可见性 ----------
        for pname, (mode, menu_btn, menu_id, row_id, sel_id) in PANELS.items():
            for (w, h) in VIEWPORTS:
                page.set_viewport_size({"width": w, "height": h})
                page.wait_for_timeout(350)
                switch_mode(page, mode)
                # 窄屏下参数面板是**底部抽屉**（≤1100px），默认收起 ——
                # 按钮根本不在视口里（实测 top=1103 > innerHeight=780）。
                # 必须先打开抽屉（点 #panelFab），再把按钮滚进视野。
                page.evaluate(
                    """() => {
                      const p = document.querySelector('#panel');
                      if (p && !p.classList.contains('open')) {
                        const fab = document.querySelector('#panelFab');
                        if (fab) fab.click();
                      }
                    }""")
                page.wait_for_timeout(500)
                page.evaluate(
                    """(sel) => {
                      const b = document.querySelector(sel);
                      if (b) b.scrollIntoView({block: 'center', inline: 'nearest'});
                    }""", menu_btn)
                page.wait_for_timeout(400)
                open_menu(page, menu_btn)
                geo = menu_geometry(page, menu_id)
                btn_geo = button_geometry(page, menu_btn)
                if geo:
                    geo["button"] = btn_geo
                tag = f"{pname}@{w}"
                ck.check(geo is not None, f"[{tag}] 菜单存在", "null")
                if geo:
                    ck.check(btn_geo and btn_geo["onScreen"],
                             f"[{tag}] 增强按钮本身在视口内（测量前提）", str(btn_geo))
                    ck.check(not geo["hidden"], f"[{tag}] 菜单已展开", str(geo))
                    ck.check(geo["parentIsBody"],
                             f"[{tag}] 菜单挂在 body（脱离 transform 包含块）", str(geo))
                    ck.check(geo["top"] >= 0, f"[{tag}] 菜单顶部在视口内", str(geo))
                    ck.check(geo["bottom"] <= geo["innerH"],
                             f"[{tag}] 菜单底部在视口内", str(geo))
                    ck.check(geo["left"] >= 0, f"[{tag}] 菜单左侧在视口内", str(geo))
                    ck.check(geo["right"] <= geo["innerW"] + 1,
                             f"[{tag}] 菜单右侧在视口内", str(geo))
                    ck.check(geo["h"] > 0, f"[{tag}] 菜单有可见高度", str(geo))
                    ck.check(geo["itemCount"] > 0, f"[{tag}] 菜单有选项", str(geo))
                    # 禁用项文案：只有一套，不含 Recommended
                    for it in geo["items"]:
                        if not it["unavailable"]:
                            continue
                        joined = " | ".join(it["badges"])
                        ck.check("Recommended" not in joined,
                                 f"[{tag}] 禁用项 {it['method']} 不含 Recommended", joined)
                        # 归属文案：图像/视频面板是「XXX 专属，本面板不可用」；
                        # 音乐面板是「文本扩写类，YuE2 音乐需结构化风格+歌词」
                        # 或「图像/视频专属，音乐面板不可用」。
                        # 判据：写明**归属或不可用原因**，而不是死抠某一句文案。
                        ck.check(
                            ("专属" in joined) or ("不可用" in joined)
                            or ("文本扩写类" in joined) or ("需结构化" in joined),
                            f"[{tag}] 禁用项 {it['method']} 写明归属或不可用原因", joined)
                results["panels"].setdefault(pname, {})[str(w)] = geo
                # 截图前把增强栏滚进视野
                page.evaluate(
                    """(sel) => {
                      const el = document.querySelector(sel);
                      if (el) el.scrollIntoView({block: 'center'});
                    }""", row_id)
                page.wait_for_timeout(300)
                shot = out / f"menu-{pname}-{w}.png"
                try:
                    page.screenshot(path=str(shot))
                except Exception:
                    pass
                page.keyboard.press("Escape")
                page.wait_for_timeout(250)
                # 收起抽屉，避免影响下一个面板的测量
                page.evaluate(
                    """() => {
                      const p = document.querySelector('#panel');
                      if (p && p.classList.contains('open')) {
                        p.classList.remove('open');
                        const m = document.querySelector('#panelMask');
                        if (m) m.classList.remove('open');
                      }
                    }""")
                page.wait_for_timeout(150)

        # ---------- 模板行（平级一行） ----------
        page.set_viewport_size({"width": 1280, "height": 900})
        page.wait_for_timeout(300)
        for pname, (mode, menu_btn, menu_id, row_id, sel_id) in PANELS.items():
            switch_mode(page, mode)
            row = page.evaluate(
                """(ids) => {
                  const r = document.querySelector(ids[0]);
                  const s = document.querySelector(ids[1]);
                  if (!r || !s) return null;
                  const rb = r.getBoundingClientRect();
                  const menu = document.querySelector(ids[2]);
                  return {
                    hidden: r.hidden,
                    visible: rb.width > 0 && rb.height > 0,
                    options: Array.from(s.options).map(o => o.value),
                    value: s.value,
                    disabled: s.disabled,
                    inMenu: !!(menu && menu.contains(r)),
                    rowTop: Math.round(rb.top),
                    labelText: (document.querySelector(ids[3]) || {}).textContent || '',
                  };
                }""", [row_id, sel_id, menu_id,
                       row_id.replace("Row", "Label")])
            results["spec_row"][pname] = row
            ck.check(row is not None, f"[spec-row {pname}] 模板行存在", "null")
            if row:
                ck.check(not row["inMenu"],
                         f"[spec-row {pname}] 模板行不在增强方式下拉内部", str(row))
                # 【2026-11-10 架构裁决】音乐面板的增强是**结构化风格 + 歌词**，
                # 走自己的专属链路（/api/music-enhance），**不用模板** ——
                # 所以音乐面板的模板行是隐藏的（label 会写明「模板（YuE2 结构化风格 + 歌词）」）。
                # 其余面板（图像/视频）必须有一行可见的模板选择器。
                if pname in ("t2a", "a2a"):
                    ck.check(row["hidden"],
                             f"[spec-row {pname}] 音乐面板不提供模板行（结构化路线不用模板）",
                             str(row))
                    ck.check("YuE2" in (row["labelText"] or ""),
                             f"[spec-row {pname}] 模板行标签写明是结构化风格路线",
                             str(row))
                else:
                    ck.check(row["visible"], f"[spec-row {pname}] 模板行可见", str(row))
                    ck.check(len(row["options"]) > 0,
                             f"[spec-row {pname}] 模板行有选项", str(row))
                    ck.check(row["value"] in row["options"],
                             f"[spec-row {pname}] 当前模板在选项里", str(row))
            # 截图前把增强栏滚进视野（面板很长，不滚动看不到增强区）
            page.evaluate(
                """(sel) => {
                  const el = document.querySelector(sel);
                  if (el) el.scrollIntoView({block: 'center'});
                }""", row_id)
            page.wait_for_timeout(350)
            shot = out / f"specrow-{pname}-1280.png"
            try:
                page.screenshot(path=str(shot))
            except Exception:
                pass

        # ---------- 写回契约：图像/视频=文本契约，音乐=结构化契约 ----------
        # 【2026-11-10 真机回归】MusicEnhance 在 DOMContentLoaded 时**无条件**
        # 注册 window.MusicEnhance，providers.js 的 enhanceWriteContract()
        # 曾无条件返回音乐结构化契约 -> 图像面板的写回被解析成 kind:"structured"
        # -> 「替换/追加」按钮点了没反应（静默失败），徽标不显示、等级恒为 0。
        # 断言：只有音乐面板用结构化契约。
        for pname, (mode, _mb, _mi, _ri, _si) in PANELS.items():
            switch_mode(page, mode)
            page.wait_for_timeout(700)
            contract = page.evaluate(
                """() => {
                  const t = resolveEnhanceWriteTarget();
                  return { ok: t.ok, kind: t.kind || '', reason: t.reason || '',
                           selector: t.selector || '' };
                }""")
            results.setdefault("write_contract", {})[pname] = contract
            if pname in ("t2a", "a2a"):
                ck.check(contract.get("kind") == "structured",
                         f"[contract {pname}] 音乐面板用结构化写回契约", str(contract))
            else:
                ck.check(contract.get("kind") == "text",
                         f"[contract {pname}] 图像/视频面板用文本写回契约", str(contract))
                ck.check(contract.get("selector") == "#prompt",
                         f"[contract {pname}] 文本契约目标是 #prompt", str(contract))

        # ---------- 多选：版本历史 + 徽标 ----------
        switch_mode(page, "t2i")
        page.evaluate(
            """() => { try { localStorage.removeItem('h3studio.enhanceSelection.v1'); }
                    catch (e) {} }""")
        page.reload(wait_until="domcontentloaded")
        wait_ready(page)
        switch_mode(page, "t2i")
        open_menu(page, "#enhanceMenuBtn")
        page.evaluate(
            """() => {
              const el = document.querySelector(
                '#enhanceMenu input[data-method-check="refine"]');
              if (el) el.click();
            }""")
        page.wait_for_timeout(400)
        selected = page.evaluate(
            "() => window.enhanceMethods.getSelected()")
        ck.check(len(selected) >= 2,
                 "[multiselect] 选中了多项增强", str(selected))
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

        # 桩后端
        page.evaluate("""() => {
          window.__calls = [];
          const orig = window.fetch;
          window.fetch = async (url, opts) => {
            const u = String(url);
            window.__calls.push(u);
            const json = (b) => new Response(JSON.stringify(b), {
              status: 200, headers: {'Content-Type': 'application/json'}});
            if (u.includes('/api/enhance')) {
              return json({ok: true, provider: '桩', spec_id: 'image',
                enhanced: '【L1 特写】桩产出\\n【L2 中景】桩\\n【主体细化】桩',
                original: 'x'});
            }
            return orig(url, opts);
          };
        }""")
        page.fill("#prompt", "a lighthouse at dusk")
        page.click("#enhanceBtn")
        page.wait_for_timeout(3000)
        merged = page.evaluate(
            "() => document.querySelector('#enhanceResult').value")
        ck.check(bool(merged), "[multiselect] 合并结果非空", repr(merged)[:120])
        page.evaluate(
            """() => { const b = document.querySelector('#enhanceReplace');
                    if (b) b.click(); }""")
        page.wait_for_timeout(700)
        versions = page.evaluate(
            """() => (typeof promptVersions !== 'undefined' && promptVersions
                      ? promptVersions.versions : []).map(v => ({
                 level: v.level, source: v.source,
                 text: (v.text || '').slice(0, 60)}))""")
        badge = page.evaluate(
            """() => { const b = document.querySelector('#promptEnhancedBadge');
                 return b ? {hidden: b.hidden, text: b.textContent} : null; }""")
        level = page.evaluate(
            "() => (typeof currentPromptLevel === 'function'"
            " ? currentPromptLevel() : -1)")
        gate = page.evaluate(
            "() => { const m = document.querySelector('#enhanceGateModal');"
            "return m ? m.hidden : null; }")
        results["multiselect"] = {"selected": selected, "merged": merged[:200],
                                  "versions": versions, "badge": badge,
                                  "level": level, "gateHidden": gate}
        ck.check(len(versions) > 0, "[multiselect] 版本历史非空", str(versions))
        ck.check(any(v["level"] == 2 for v in versions),
                 "[multiselect] 版本历史含二次增强条目", str(versions))
        ck.check(any(v["level"] == 1 for v in versions),
                 "[multiselect] 版本历史含一次增强条目", str(versions))
        ck.check(badge is not None and not badge["hidden"],
                 "[multiselect] 「已增强」徽标可见", str(badge))
        ck.check(level > 0, "[multiselect] 当前提示词等级 > 0", str(level))
        try:
            page.screenshot(path=str(out / "multiselect-after-writeback-1280.png"))
        except Exception:
            pass

        # ---------- 模板选择跨模式失效 ----------
        switch_mode(page, "t2i")
        picked = page.evaluate(
            """() => {
              const sel = document.querySelector('#enhanceSpecSelect');
              if (!sel || sel.options.length < 2) return '';
              const o = sel.options[1];
              sel.value = o.value;
              sel.dispatchEvent(new Event('change', {bubbles: true}));
              return o.value;
            }""")
        page.wait_for_timeout(500)
        stored = page.evaluate("() => window.enhanceMethods.getSelectedSpecByMethod()")
        ck.check(any(picked in str(v) for v in stored.values()) if picked else True,
                 "[spec-state] 模板选择被记录", str(stored))
        switch_mode(page, "t2i_qwen")
        page.wait_for_timeout(700)
        switch_mode(page, "t2i")
        page.wait_for_timeout(700)
        back = page.evaluate(
            """() => {
              const sel = document.querySelector('#enhanceSpecSelect');
              const opts = sel ? Array.from(sel.options).map(o => o.value) : [];
              return {value: sel ? sel.value : '', options: opts};
            }""")
        results["spec_row"]["cross_mode"] = {"picked": picked, "stored": stored,
                                             "after": back}
        ck.check(back["value"] in back["options"],
                 "[spec-state] 跨模式回来后当前模板属于本面板可用集合", str(back))

        browser.close()

    results["summary"] = {"passed": ck.passed, "failed": len(ck.failures),
                           "failures": ck.failures,
                           "console_errors": len(results["console_errors"]),
                           "failed_requests": len(results["failed_requests"])}
    (out / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n=== 断言 {ck.passed} 通过 / {len(ck.failures)} 失败 ===")
    print(f"console 报错: {len(results['console_errors'])}")
    print(f"失败请求: {len(results['failed_requests'])}")
    for f in ck.failures[:40]:
        print("  FAIL:", f)
    print(f"\n证据目录: {out.resolve()}")
    return 1 if ck.failures else 0


if __name__ == "__main__":
    sys.exit(main())
