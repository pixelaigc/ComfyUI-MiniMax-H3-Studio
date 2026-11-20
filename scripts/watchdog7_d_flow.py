#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看门狗第 7 轮 —— D 维度交互流程取证（2026-11-21）。

【取证范围 —— 只取证，不改代码】
真实操作流：选模式 -> 填参数 -> 提交 -> 排队 -> 进度 -> 产物预览。
**不真正下发 GPU 任务**（会消耗算力额度）—— 提交一路走到
**服务端参数校验**那一步，拿到**真实的校验反馈**（这才是 D 维度
关心的「反馈是否缺失 / 隐含必填是否说得清」）。

同时记录**已确认缺陷**的当前线上表现（作为基线证据）：
  * 移动端「选择某个生成类型后无法回到生成类型选择、也看不到生成媒体库」
    —— 该缺陷由**另一条在跑任务**在修，本轮只记录表现，不碰。

用法:
    python3 scripts/watchdog7_d_flow.py --out /root/evidence/watchdog7-d
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

BASE = os.environ.get("H3_BASE", "http://127.0.0.1:8190")


def run(out_dir: Path) -> int:
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict = {"desktop": {}, "mobile": {}, "notes": []}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox",
                                         "--disable-dev-shm-usage"])

        # ---------------- 桌面：完整交互流 ----------------
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)[:300]))
        page.goto(BASE, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3500)

        d: dict = {}
        d["console_errors"] = errors

        # 1) 当前模式 + 可见的模式切换控件
        d["initial_mode"] = page.evaluate("""() => {
          const t = document.querySelector('.jobs-tabs button.active');
          const on = document.querySelector('button.gen-btn.active, .mode-btn.active');
          return {tab: t ? t.textContent.trim() : null,
                  modeBtn: on ? on.textContent.trim() : null,
                  genTypeTabs: Array.from(
                    document.querySelectorAll('#genTypeTabs button, #genTypeTabs .gen-type-tab'))
                    .map(e=>e.textContent.trim()).slice(0,30)};
        }""")

        # 2) 切换到「生成类型」标签 -> 看是否有类型选择器
        d["gen_type_tab_click"] = page.evaluate("""() => {
          const tabs = Array.from(document.querySelectorAll('#genTypeTabs button'));
          if (!tabs.length) return {ok:false, reason:'no genTypeTabs buttons'};
          const before = document.body.innerHTML.length;
          tabs[0].click();
          return {ok:true, clicked: tabs[0].textContent.trim(),
                  tabsCount: tabs.length, htmlDelta: 0, before};
        }""")
        page.wait_for_timeout(1200)
        d["gen_type_tab_click"]["htmlDelta"] = page.evaluate(
            "(n) => document.body.innerHTML.length - n",
            d["gen_type_tab_click"]["before"])

        # 3) 填参数：提示词为空 -> 提交 -> 看**反馈**
        page.evaluate("""() => {
          const p = document.querySelector('#prompt');
          if (p) { p.value=''; p.dispatchEvent(new Event('input',{bubbles:true})); }
        }""")
        page.wait_for_timeout(400)
        feedback = page.evaluate("""() => {
          const btn = document.querySelector('#genBtn, button.gen-btn');
          if (!btn) return {ok:false, reason:'no gen button'};
          btn.click();
          return {ok:true, btnText: btn.textContent.trim()};
        }""")
        page.wait_for_timeout(1800)
        feedback["after"] = page.evaluate("""() => {
          const hint = document.querySelector('#enhanceHint, .hint, .toast, .error');
          const errs = Array.from(document.querySelectorAll(
            '[class*=error],[class*=toast],[class*=hint],[role=alert]'))
            .map(e => ({cls:(e.className||'').toString().slice(0,50),
                        text:(e.textContent||'').trim().slice(0,120),
                        visible: e.getBoundingClientRect().height>0}))
            .filter(x=>x.visible && x.text);
          return {alerts: errs.slice(0,8)};
        }""")
        d["empty_prompt_submit"] = feedback

        # 4) 填上提示词再提交（真实走到服务端校验）
        page.evaluate("""() => {
          const p = document.querySelector('#prompt');
          if (p) {
            p.value = '看门狗第7轮交互流程取证测试';
            p.dispatchEvent(new Event('input',{bubbles:true}));
          }
        }""")
        page.wait_for_timeout(500)
        d["filled_submit"] = page.evaluate("""() => {
          const btn = document.querySelector('#genBtn, button.gen-btn');
          if (!btn) return {ok:false};
          btn.click();
          return {ok:true};
        }""")
        page.wait_for_timeout(2500)
        d["filled_submit"]["server_response"] = page.evaluate("""() => {
          const errs = Array.from(document.querySelectorAll(
            '[class*=error],[class*=toast],[role=alert]'))
            .map(e => (e.textContent||'').trim().slice(0,200))
            .filter(t => t);
          return {alerts: Array.from(new Set(errs)).slice(0,6)};
        }""")

        # 5) 生成媒体库 / 任务卡片是否可见 + 卡片字段
        d["jobs_cards"] = page.evaluate("""() => {
          const cards = document.querySelectorAll('.job-card');
          const first = cards[0];
          return {
            cardCount: cards.length,
            firstCardText: first ? (first.textContent||'').trim().slice(0,200) : null,
            hasPreview: !!document.querySelector('.job-card video, .job-card img'),
            hasActions: !!document.querySelector('.job-card [data-act]'),
            actionLabels: Array.from(
              document.querySelectorAll('.job-card [data-act]'))
              .map(e=>e.getAttribute('data-act')).slice(0,20)
          };
        }""")
        try:
            page.screenshot(path=str(out_dir / "desktop-flow-end.png"))
        except Exception:
            pass
        result["desktop"] = d
        ctx.close()

        # ---------------- 移动：已确认缺陷的**当前表现**（基线） ----------------
        for w, h, tag in ((375, 780, "mobile-375"), (390, 844, "mobile-390")):
            ctx = browser.new_context(viewport={"width": w, "height": h},
                                      is_mobile=True, has_touch=True)
            page = ctx.new_page()
            page.goto(BASE, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3500)
            m: dict = {}
            m["before_type_select"] = page.evaluate("""() => {
              const btns = Array.from(
                document.querySelectorAll('#genTypeTabs button, #genTypeTabs [data-type]'));
              return {typeTabCount: btns.length,
                      typeTabTexts: btns.map(b=>b.textContent.trim()).slice(0,12),
                      mediaLibraryVisible: (() => {
                        const g = document.querySelector('#genTypeTabs');
                        if (!g) return false;
                        const r = g.getBoundingClientRect();
                        return r.width>0 && r.height>0 && r.top < innerHeight;
                      })()};
            }""")
            # 点一个「生成类型」（用第二个，排除默认 active）
            clicked = page.evaluate("""() => {
              const btns = Array.from(
                document.querySelectorAll('#genTypeTabs button, #genTypeTabs [data-type]'));
              if (btns.length < 2) return {ok:false, n: btns.length};
              btns[1].click();
              return {ok:true, clicked: btns[1].textContent.trim()};
            }""")
            m["type_click"] = clicked
            page.wait_for_timeout(1500)
            m["after_type_select"] = page.evaluate("""() => {
              const g = document.querySelector('#genTypeTabs');
              const r = g ? g.getBoundingClientRect() : null;
              const btns = Array.from(
                document.querySelectorAll('#genTypeTabs button, #genTypeTabs [data-type]'));
              const visBtns = btns.filter(b=>{
                const rr=b.getBoundingClientRect();
                return rr.width>0 && rr.height>0 && rr.top<innerHeight && rr.bottom>0;
              });
              return {
                typeTabVisible: r ? (r.width>0 && r.height>0) : false,
                typeTabRect: r ? {top:Math.round(r.top),left:Math.round(r.left),
                                  w:Math.round(r.width),h:Math.round(r.height)} : null,
                visibleTypeTabs: visBtns.map(b=>b.textContent.trim()),
                // 生成媒体库（任务卡片区）在不在视口里
                mediaLibraryInViewport: (() => {
                  const a = document.querySelector('#jobsArea');
                  if (!a) return null;
                  const ar = a.getBoundingClientRect();
                  return ar.top < innerHeight && ar.bottom > 0;
                })(),
                jobCardCount: document.querySelectorAll('.job-card').length,
                scrollY: Math.round(scrollY),
              };
            }""")
            try:
                page.screenshot(path=str(out_dir / f"{tag}-after-type.png"))
            except Exception:
                pass
            result["mobile"][tag] = m
            ctx.close()

        browser.close()

    (out_dir / "flow.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2)[:6000])
    print(f"\n输出: {out_dir}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/root/evidence/watchdog7-d")
    args = ap.parse_args()
    sys.exit(run(Path(args.out)))
