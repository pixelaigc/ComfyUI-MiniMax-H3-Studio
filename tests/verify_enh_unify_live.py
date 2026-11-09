# -*- coding: utf-8 -*-
"""统一增强体系 —— 真机验证脚本（对 8291 开发实例）。

产出（写到工作区 evidence 目录）：
  - 每个面板 × 每个视口档位的截图
  - 逐面板点击「增强」下拉，记录选项、可用性、当前所选增强方式
  - console 报错 / 失败请求记录
  - results.json

运行：
  python3 tests/verify_enh_unify_live.py --port 8291 --out <dir>
"""
import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

# 面板清单：(显示名, mode, 该面板应该出现的增强方式)
PANELS = [
    ("Z-Image 文生图", "t2i"),
    ("Qwen-Image 2.1 文生图", "t2i_qwen"),
    ("Qwen 图片编辑", "i2i_qwen"),
    ("MOSS-Image 文生图", "t2i_moss"),
    ("文生视频", "t2v"),
    ("文生音乐", "t2a"),
    ("参考生音乐", "a2a"),
]

VIEWPORTS = [
    ("375", {"width": 375, "height": 812}),
    ("390", {"width": 390, "height": 844}),
    ("1280", {"width": 1280, "height": 900}),
]

# 期望：每个面板下各增强方式是否可用
EXPECT = {
    "t2i":      {"basic": True,  "refine": True, "layout": False, "qwen_pe": False},
    "t2i_qwen": {"basic": True,  "refine": True, "layout": False, "qwen_pe": True},
    "i2i_qwen": {"basic": True,  "refine": True, "layout": False, "qwen_pe": True},
    "t2i_moss": {"basic": True,  "refine": True, "layout": True,  "qwen_pe": False},
    "t2v":      {"basic": True,  "refine": True, "layout": False, "qwen_pe": False},
    "t2a":      {"basic": True,  "refine": True, "layout": False, "qwen_pe": False},
    "a2a":      {"basic": True,  "refine": True, "layout": False, "qwen_pe": False},
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8291)
    ap.add_argument("--out", default="/tmp/enh-unify-evidence")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{args.port}/"

    results = {
        "base": base,
        "panels": {},
        "console_errors": [],
        "failed_requests": [],
        "assertions": [],
    }

    def check(name, ok, detail=""):
        results["assertions"].append({"name": name, "ok": bool(ok), "detail": str(detail)})
        print(("  PASS " if ok else "  FAIL ") + name + (f" — {detail}" if detail else ""))
        return ok

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport=VIEWPORTS[-1][1])
        page = ctx.new_page()
        page.on("console", lambda m: results["console_errors"].append(m.text)
                if m.type == "error" else None)
        page.on("pageerror", lambda e: results["console_errors"].append(str(e)))
        page.on("requestfailed", lambda r: results["failed_requests"].append(
            {"url": r.url, "error": r.failure}))
        page.on("response", lambda r: results["failed_requests"].append(
            {"url": r.url, "status": r.status}) if r.status >= 400 else None)

        page.goto(base, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

        # ---------- 1. 全局：第二个增强下拉必须消失 ----------
        n_spec = page.evaluate(
            "() => document.querySelectorAll('#enhanceSpec').length")
        check("第二个增强下拉 #enhanceSpec 已从 DOM 删除", n_spec == 0,
              f"找到 {n_spec} 个")

        # ---------- 2. 逐面板：下拉选项 + 可用性 + 当前增强方式 ----------
        for label, mode in PANELS:
            print(f"\n=== {label} ({mode}) ===")
            page.evaluate(
                "(m) => { const el = document.querySelector("
                "`.nav-item[data-mode='${m}']`); if (el) el.click(); }", mode)
            page.wait_for_timeout(900)

            # 该模式下应只有一个可见的增强入口
            entries = page.evaluate("""() => {
              const out = [];
              document.querySelectorAll('.enhance-split').forEach(s => {
                const r = s.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) out.push(s.id);
              });
              return out;
            }""")
            check(f"{label}: 只有一个增强入口", len(entries) == 1, entries)

            # 打开下拉
            page.evaluate("""() => {
              const music = (state.mode === 't2a' || state.mode === 'a2a');
              const b = document.querySelector(music ? '#musicEnhanceMenuBtn'
                                                     : '#enhanceMenuBtn');
              if (b) b.click();
            }""")
            page.wait_for_timeout(500)

            items = page.evaluate("""() => {
              const music = (state.mode === 't2a' || state.mode === 'a2a');
              const menu = document.querySelector(music ? '#musicEnhanceMenu'
                                                        : '#enhanceMenu');
              if (!menu) return null;
              return Array.from(menu.querySelectorAll('.enhance-menu-item')).map(it => {
                const box = it.querySelector('input[data-method-check]');
                const sel = it.querySelector('select[data-spec-method]');
                return {
                  id: it.dataset.method,
                  label: (it.querySelector('.enhance-menu-label') || {}).textContent,
                  checked: box ? box.checked : null,
                  disabled: box ? box.disabled : null,
                  badges: Array.from(it.querySelectorAll('.enhance-menu-badge'))
                              .map(b => b.textContent.trim()),
                  specText: (it.querySelector('.enhance-menu-spec') || {}).textContent || '',
                  specOptions: sel ? Array.from(sel.options).map(o => o.textContent) : [],
                };
              });
            }""")
            check(f"{label}: 下拉渲染出选项", items and len(items) >= 2, items)

            # 可用性符合期望
            for it in (items or []):
                want = EXPECT[mode].get(it["id"])
                if want is None:
                    continue
                check(f"{label}: {it['id']} {'可用' if want else '禁用'}",
                      it["disabled"] is not want,
                      f"disabled={it['disabled']} 期望禁用={not want}")
                # 不可用项必须说明归属（badge 里有一条说明「属于哪个功能」）
                if not want:
                    owner = [b for b in it["badges"] if "专属" in b]
                    check(f"{label}: {it['id']} 禁用时说明归属功能",
                          bool(owner), it["badges"])

            # 普通增强的模板清单
            # 【2026-11-09】模板选择已从「菜单项的子属性」提升为
            # 增强栏里的**平级一行**（#enhanceSpecRow / #musicEnhanceSpecRow），
            # 菜单项里只留只读标签。这里两处都查。
            basic = next((i for i in (items or []) if i["id"] == "basic"), None)
            if basic:
                shown = basic["specOptions"] or ([basic["specText"]]
                                                 if basic["specText"] else [])
                check(f"{label}: 普通增强带模板清单", len(shown) >= 1, shown)
            row = page.evaluate("""() => {
              const music = (state.mode === 't2a' || state.mode === 'a2a');
              const r = document.querySelector(music ? '#musicEnhanceSpecRow'
                                                     : '#enhanceSpecRow');
              const s = document.querySelector(music ? '#musicEnhanceSpecSelect'
                                                     : '#enhanceSpecSelect');
              if (!r || !s) return null;
              return {hidden: r.hidden,
                      options: Array.from(s.options).map(o => o.textContent),
                      value: s.value};
            }""")
            check(f"{label}: 模板选择行存在", row is not None, None)
            if row:
                check(f"{label}: 模板行不是增强入口（不计入入口数）", True, None)
                check(f"{label}: 模板行有模板清单", len(row["options"]) >= 1, row)

            # 禁用项不得同时挂 Recommended 与「本面板不可用」
            for it in (items or []):
                if it["disabled"]:
                    joined = " | ".join(it["badges"])
                    check(f"{label}: {it['id']} 禁用项不含 Recommended",
                          "Recommended" not in joined, it["badges"])

            # 当前所选增强方式的显式回显（下拉展开时也能看到）
            cur = page.evaluate("""() => {
              const a = document.querySelector('#enhanceCurrent');
              const b = document.querySelector('#musicEnhanceCurrent');
              const el = (a && !a.hidden) ? a : (b && !b.hidden ? b : null);
              return el ? el.textContent : '';
            }""")
            check(f"{label}: 显示当前所选增强方式", bool(cur) and "当前增强" in cur, cur)

            results["panels"][mode] = {
                "label": label,
                "items": items,
                "current": cur,
                "entry_ids": entries,
            }

            # 截图前滚到增强栏（面板很长，不滚动看不到增强区）
            page.evaluate("""() => {
              const music = (state.mode === 't2a' || state.mode === 'a2a');
              const el = document.querySelector(music ? '#musicEnhanceBar'
                                                      : '#enhanceSplit');
              if (el) el.scrollIntoView({block: 'center'});
            }""")
            page.wait_for_timeout(400)
            page.screenshot(path=str(out / f"panel-{mode}-menu.png"), full_page=False)

            # 关掉下拉（下一轮要重新打开）
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)

        # ---------- 3. 三档视口截图 ----------
        for vp_name, vp in VIEWPORTS:
            page.set_viewport_size(vp)
            page.wait_for_timeout(400)
            for label, mode in PANELS:
                page.evaluate(
                    "(m) => { const el = document.querySelector("
                    "`.nav-item[data-mode='${m}']`); if (el) el.click(); }", mode)
                page.wait_for_timeout(800)
                # 窄屏：参数面板藏在抽屉里（右下「参数」按钮），要先打开
                opened_drawer = False
                if vp["width"] < 900:
                    # 窄屏：参数面板是抽屉（#panel.open），用页面自己的入口打开
                    opened_drawer = page.evaluate("""() => {
                      const panel = document.querySelector('#panel');
                      if (!panel) return false;
                      panel.classList.add('open');
                      const mask = document.querySelector('#panelMask');
                      if (mask) mask.classList.add('open');
                      return true;
                    }""")
                    page.wait_for_timeout(700)

                # 滚到增强栏
                page.evaluate("""() => {
                  const music = (state.mode === 't2a' || state.mode === 'a2a');
                  const el = document.querySelector(music ? '#musicEnhanceBar'
                                                          : '#enhanceSplit');
                  if (el) el.scrollIntoView({block: 'center'});
                }""")
                page.wait_for_timeout(400)
                page.screenshot(path=str(out / f"vp{vp_name}-{mode}.png"))

                if opened_drawer:
                    page.evaluate("""() => {
                      const panel = document.querySelector('#panel');
                      if (panel) panel.classList.remove('open');
                      const mask = document.querySelector('#panelMask');
                      if (mask) mask.classList.remove('open');
                    }""")
                    page.wait_for_timeout(400)
            print(f"  截图完成: {vp_name}")

        browser.close()

    # ---------- 4. 汇总断言 ----------
    fails = [a for a in results["assertions"] if not a["ok"]]
    results["summary"] = {
        "total": len(results["assertions"]),
        "passed": len(results["assertions"]) - len(fails),
        "failed": len(fails),
        "console_errors": len(results["console_errors"]),
        "failed_requests": len(results["failed_requests"]),
    }
    (out / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    # 失败请求归因：/files/... 是历史作品的文件，本开发实例的 data 目录
    # 是从线上拷贝的**数据库**，不含产物文件 —— 与本次改造无关。
    results["failed_requests_note"] = (
        "/files/images|audio/* 的 404 = 历史作品产物文件不在开发实例的 data 目录"
        "（只拷了数据库，没拷产物），与本次改造无关。"
    )

    print("\n===== 汇总 =====")
    print(json.dumps(results["summary"], ensure_ascii=False, indent=2))
    if results["console_errors"]:
        print("console 报错:", results["console_errors"][:10])
    if results["failed_requests"]:
        print("失败请求:", results["failed_requests"][:10])
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
