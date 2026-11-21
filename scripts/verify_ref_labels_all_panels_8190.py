#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
线上（8190）真实浏览器取证 —— **三类带素材面板**的增强链路。

【取证范围】
  1. flf  首尾帧生视频   -> 官方标签 <Picture N>（H3 官方）
  2. r2v  参考生视频     -> 官方标签 <Picture N>（H3 官方）
  3. i2i_qwen 参考生图   -> 官方标签 <imageN>（Qwen 官方 PE-I2I）
  4. t2v  文生视频       -> **无素材面板**，行为不得回归（提示词逐字不变）

【每类面板都做】
  * 真实浏览器（Playwright + Chromium）打开 8190
  * 从素材库选素材填进面板槽位
  * 输入提示词 -> 点「增强」
  * 留存：截图 / console 错误 / **请求体原文** / **响应体原文**
    —— 请求体必须能看到**全部**素材，响应必须能看到**参考编号**
  * 断言：响应里的参考标签与该面板的**官方语法**一致

【判定】
  * 请求体 asset_refs 数量 == 面板实际填入的素材数（不能少、不能多）
  * 响应 reference_labels.expected 全部出现在 found 里
  * 响应里的标签**方言**正确（H3 面板 <Picture N>，Qwen 面板 <imageN>）
  * 无素材面板：不出 /api/enhance 请求带素材（asset_refs 为空）
  * console 0 错误

用法：
  python3 scripts/verify_ref_labels_all_panels_8190.py
  H3_EVIDENCE_DIR=/path/to/out python3 scripts/verify_ref_labels_all_panels_8190.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("H3_BASE", "http://127.0.0.1:8190")
OUT = Path(os.environ.get(
    "H3_EVIDENCE_DIR",
    "/root/projects/h3s-fix-reflabel-syntax/evidence/ref-labels-all-panels-8190"))
OUT.mkdir(parents=True, exist_ok=True)

#: 面板配置：(mode, 显示名, 槽位列表, 期望标签方言)
#:   槽位 = (面板槽位 key, 素材库里的第几张图)
PANELS = [
    ("flf", "首尾帧生视频", [("first", 0), ("last", 1)], "h3"),
    ("r2v", "参考生视频", [("ref0", 0), ("ref1", 1)], "h3"),
    ("i2i_qwen", "参考生图（Qwen）", None, "qwen"),  # 参考图走 QwenRefs
]

#: 无素材面板（用于回归对照）
NO_ASSET_PANELS = ["t2v"]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _json_or_raw(raw):
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {"_raw": raw}


def pick_asset_from_library(page, index, slot_key):
    """从素材库选择器里选第 index 张图，填进指定槽位。"""
    page.evaluate(
        """(key) => {
          const el = document.querySelector(`#uploadRow .up-slot[data-key="${key}"]`);
          const btn = el && el.querySelector('.pick-asset');
          if (btn) btn.click();
        }""", slot_key)
    page.wait_for_selector("#alPickerBody .al-card", timeout=60000)
    page.wait_for_timeout(1200)
    asset_id = page.evaluate(
        """(i) => {
          const cards = document.querySelectorAll('#alPickerBody .al-card');
          const card = cards[i];
          if (!card) return null;
          const id = card.dataset.id;
          card.click();
          return id;
        }""", index)
    page.wait_for_timeout(1500)
    page.evaluate(
        """() => {
          const btns = [...document.querySelectorAll('#alPickerMask button')];
          const ok = btns.find(b => /确定|使用|完成|添加|选择/.test(b.textContent || ''));
          if (ok) ok.click();
        }""")
    page.wait_for_timeout(2500)
    return asset_id


def pick_qwen_refs(page, count):
    """Qwen / H3-Image 图片编辑面板：从素材库挑 count 张参考图。

    那套参考图由 web/qwen-refs.js 自己管状态（window.QwenRefs），
    不在 state.assetRefs 里。入口按钮是 `#imgRefAdd`（「+ 从素材库添加」）。

    【为什么要逐张加、且每次都重开选择器】素材库选择器是单选模式
    —— 点一张就填进当前槽位。Qwen 的参考图是一个列表，所以要点
    N 次「添加」，每次选不同的那张。
    """
    ids = []
    for i in range(count):
        page.evaluate(
            """() => {
              const btn = document.querySelector('#imgRefAdd');
              if (btn) btn.click();
            }""")
        try:
            page.wait_for_selector("#alPickerBody .al-card", timeout=30000)
        except Exception as exc:
            log(f"  参考图选择器没打开：{exc}")
            break
        page.wait_for_timeout(1000)
        aid = page.evaluate(
            """(idx) => {
              const cards = document.querySelectorAll('#alPickerBody .al-card');
              const card = cards[idx];
              if (!card) return null;
              const id = card.dataset.id;
              card.click();
              return id;
            }""", i)
        page.wait_for_timeout(1200)
        page.evaluate(
            """() => {
              const btns = [...document.querySelectorAll('#alPickerMask button')];
              const ok = btns.find(b => /确定|使用|完成|添加|选择/.test(b.textContent || ''));
              if (ok) ok.click();
            }""")
        page.wait_for_timeout(2000)
        if aid:
            ids.append(aid)
    return ids


def switch_mode(page, mode):
    """切到指定面板。"""
    page.wait_for_function(
        """(m) => {
          const el = document.querySelector(`.nav-item[data-mode="${m}"]`);
          return !!(el && el.offsetParent !== null);
        }""", arg=mode, timeout=90000)
    page.evaluate(
        """(m) => {
          const el = document.querySelector(`.nav-item[data-mode="${m}"]`);
          if (el) el.click();
        }""", mode)
    page.wait_for_function(
        """(m) => typeof state !== 'undefined' && state.mode === m""",
        arg=mode, timeout=30000)
    page.wait_for_timeout(2000)


def run_enhance(page, prompt, calls, responses):
    """输入提示词 -> 点「增强」-> 等结果弹窗。返回界面上的结果快照。"""
    page.evaluate(
        """(t) => {
          const el = document.querySelector('#prompt');
          el.value = t;
          el.dispatchEvent(new Event('input', {bubbles: true}));
        }""", prompt)
    page.wait_for_timeout(600)

    n_before = len(calls)
    page.evaluate(
        """() => {
          const btn = document.querySelector('#enhanceBtn');
          if (!btn) throw new Error('找不到增强按钮');
          btn.click();
        }""")
    # 【为什么用轮询而不是 wait_for_function】wait_for_function 默认按
    # rAF 轮询，本机多模态模型在 GPU 忙时一次调用可能要几十秒到几分钟；
    # 轮询能**逐次打印进度**，卡住时能立刻看出是「模型慢」而不是
    # 「脚本没点到按钮」。
    deadline = time.time() + 900
    while time.time() < deadline:
        page.wait_for_timeout(5000)
        st = page.evaluate(
            """() => {
              const m = document.querySelector('#enhanceModal');
              const h = document.querySelector('#enhanceHint');
              return {
                modal: !!(m && !m.hidden),
                hint: h ? h.textContent : '',
              };
            }""")
        log(f"  等待增强结果… modal={st['modal']} hint={st['hint'][:60]!r}")
        if st["modal"]:
            break
        if "失败" in st["hint"] or "报错" in st["hint"]:
            log(f"  增强报错：{st['hint']}")
            break
    page.wait_for_timeout(1500)

    ui = page.evaluate(
        """() => {
          const r = document.querySelector('#enhanceResult');
          const meta = document.querySelector('#enhanceMeta');
          const hint = document.querySelector('#enhanceHint');
          const m = document.querySelector('#enhanceModal');
          return {
            modal_visible: !!(m && !m.hidden),
            enhanced: r ? r.value : "",
            meta: meta ? meta.innerText : "",
            hint: hint ? hint.innerText : "",
          };
        }""")
    return ui, calls[n_before:]


def check_dialect(labels, dialect):
    """返回与该方言**不符**的标签列表（应为空）。"""
    bad = []
    for lb in labels:
        if dialect == "qwen":
            if not re.match(r"^<\s*image\s*\d+\s*>$", lb, re.I):
                bad.append(lb)
        else:
            if not re.match(r"^<(Subject|Picture|Video|Audio)\s*\d+>$", lb):
                bad.append(lb)
    return bad


def main():
    results = {}
    console_errors = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 950})
        page = ctx.new_page()

        calls, responses = [], []

        def on_request(req):
            if req.method == "POST" and req.url.endswith("/api/enhance"):
                calls.append({"url": req.url,
                              "body_raw": req.post_data,
                              "body": _json_or_raw(req.post_data)})

        def on_response(resp):
            if resp.request.method == "POST" and resp.url.endswith("/api/enhance"):
                try:
                    body = resp.json()
                except Exception:
                    body = {"_raw": "<非 JSON>"}
                responses.append({"status": resp.status, "body": body})

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: console_errors.append(f"console.{m.type}: {m.text}")
                if m.type == "error" else None)

        log(f"打开 {BASE}")
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        # ================= 带素材的三类面板 =================
        for mode, title, slots, dialect in PANELS:
            log(f"=== 面板 {mode}（{title}，方言 {dialect}）===")
            switch_mode(page, mode)
            page.screenshot(path=str(OUT / f"{mode}-01-panel.png"), full_page=False)

            if mode == "i2i_qwen":
                aids = pick_qwen_refs(page, 2)
                expected_count = 2
            else:
                aids = []
                for slot_key, idx in slots:
                    aid = pick_asset_from_library(page, idx, slot_key)
                    log(f"  槽位 {slot_key} -> {aid}")
                    if aid:
                        aids.append(aid)
                expected_count = len(slots)
            page.screenshot(path=str(OUT / f"{mode}-02-assets.png"), full_page=False)

            panel_state = page.evaluate(
                """() => ({
                  mode: state.mode,
                  assetRefs: JSON.parse(JSON.stringify(state.assetRefs || {})),
                  qwenRefs: (window.QwenRefs && typeof window.QwenRefs.getRefs === 'function')
                    ? window.QwenRefs.getRefs().map(r => r.asset_id) : [],
                })""")

            # 【为什么要先验面板真的填上了】素材库选择器是单选模式，
            # 点错/点重了会导致某个槽位没填上 —— 那样后续的「增强请求
            # 少带素材」是**取证脚本自己的错**，不是产品的错。
            # 必须先确认面板状态，再解读增强结果。
            filled = len(panel_state.get("assetRefs") or {})
            if mode == "i2i_qwen":
                filled = len(panel_state.get("qwenRefs") or [])
            log(f"  面板实际填入素材数 = {filled}")
            if filled != expected_count:
                log(f"  !! 面板没填满（{filled}/{expected_count}），"
                    f"本面板取证作废")

            ui, panel_calls = run_enhance(
                page, "镜头从女孩慢慢推向远景，保持主体一致", calls, responses)
            page.screenshot(path=str(OUT / f"{mode}-03-enhance.png"), full_page=False)

            last_req = panel_calls[-1]["body"] if panel_calls else {}
            last_resp = responses[-1]["body"] if responses else {}
            rl = last_resp.get("reference_labels") or {}
            found = rl.get("found") or []
            expected = rl.get("expected") or []
            bad_dialect = check_dialect(found, dialect)

            results[mode] = {
                "title": title,
                "dialect": dialect,
                "assets": aids,
                "expected_asset_count": expected_count,
                "panel_filled_count": filled,
                "panel_state": panel_state,
                "request_body": last_req,
                "request_asset_refs": last_req.get("asset_refs"),
                "response_body": last_resp,
                "reference_labels": rl,
                "ui_meta": ui.get("meta"),
                "ui_enhanced_head": (ui.get("enhanced") or "")[:400],
                "console_errors": list(console_errors),
            }
            log(f"  请求体 asset_refs = {json.dumps(last_req.get('asset_refs'), ensure_ascii=False)}")
            log(f"  响应 reference_labels = {json.dumps(rl, ensure_ascii=False)}")
            if bad_dialect:
                log(f"  !! 方言不符的标签：{bad_dialect}")

            # 关掉弹窗，切下一个面板
            page.evaluate(
                """() => {
                  const btns = [...document.querySelectorAll('#enhanceModal button')];
                  const close = btns.find(b => /关闭|取消|×|✕/.test(b.textContent || b.title || ''));
                  if (close) close.click();
                  else {
                    const m = document.querySelector('#enhanceModal');
                    if (m) m.hidden = true;
                  }
                }""")
            page.wait_for_timeout(1200)

        # ================= 无素材面板（回归对照）=================
        for mode in NO_ASSET_PANELS:
            log(f"=== 无素材面板 {mode}（回归对照）===")
            switch_mode(page, mode)
            n_before = len(calls)
            ui, panel_calls = run_enhance(
                page, "一只猫在窗台上打哈欠", calls, responses)
            page.screenshot(path=str(OUT / f"{mode}-04-no-asset.png"), full_page=False)
            last_req = panel_calls[-1]["body"] if panel_calls else {}
            results[mode] = {
                "title": "无素材面板（回归对照）",
                "request_body": last_req,
                "request_asset_refs": last_req.get("asset_refs"),
                "ui_meta": ui.get("meta"),
                "ui_enhanced_head": (ui.get("enhanced") or "")[:300],
            }
            log(f"  请求体 asset_refs = {json.dumps(last_req.get('asset_refs'), ensure_ascii=False)}")

        evidence = {
            "base": BASE,
            "results": results,
            "console_errors": console_errors,
            "all_requests": calls,
            "all_responses": responses,
        }
        (OUT / "evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"证据已写入 {OUT / 'evidence.json'}")
        browser.close()

    return evidence


if __name__ == "__main__":
    ev = main()
    print()
    print("=== 判定 ===")
    ok = True

    for mode, title, slots, dialect in PANELS:
        r = ev["results"].get(mode) or {}
        refs = r.get("request_asset_refs")
        n_refs = len(refs or [])
        want = r.get("expected_asset_count") or 0
        filled = r.get("panel_filled_count")
        rl = r.get("reference_labels") or {}
        expected = rl.get("expected") or []
        found = rl.get("found") or []
        missing = [x for x in expected if x not in found]
        bad = check_dialect(found, dialect)
        print(f"[{mode}] 面板填入={filled}/{want} 请求体素材数={n_refs}"
              f" 期望标签={expected} 实得={found}")
        if filled is not None and filled != want:
            print("  !! 面板没填满 —— 取证脚本自身的操作问题，不是产品缺陷")
            ok = False
        if n_refs != want:
            print("  !! 请求体素材数不符")
            ok = False
        if missing:
            print(f"  !! 响应缺参考标签：{missing}")
            ok = False
        if not expected:
            print("  !! 响应没有期望标签 —— 增强没带出参考编号")
            ok = False
        if bad:
            print(f"  !! 标签方言不符：{bad}")
            ok = False

    for mode in NO_ASSET_PANELS:
        r = ev["results"].get(mode) or {}
        refs = r.get("request_asset_refs")
        print(f"[{mode}] 无素材面板 asset_refs = {json.dumps(refs, ensure_ascii=False)}")
        if refs:
            print("  !! 无素材面板不该带素材")
            ok = False

    if ev.get("console_errors"):
        print(f"!! 控制台报错：{ev['console_errors'][:5]}")
        ok = False

    print("=== 浏览器取证", "通过" if ok else "失败", "===")
    sys.exit(0 if ok else 1)
