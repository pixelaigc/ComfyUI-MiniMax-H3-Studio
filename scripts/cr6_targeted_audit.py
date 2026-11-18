# -*- coding: utf-8 -*-
"""第 6 轮审查：**定向**取证 —— 接续上下文面板 / H3-Image 负面提示词提示 / 分辨率上限文案。

【为什么要有这个脚本】
通用巡检（cr6_dom_audit.py）证明了「DOM 里存在 **xxx** 残留」，
但**没说清楚是哪一段、出现在哪个面板、用户能不能看见**。
本脚本针对第 6 轮新代码涉及的三个具体位置做**逐条**取证：

  A. 接续上下文面板（web/continuation.js 的 renderPanel）
     —— 触发方式：真的调 /api/jobs/{id}/continuation 拿到上下文，
        然后走真实前端路径 Continuation.start()，再读 DOM。
  B. H3-Image 负面提示词提示（web/app.js 的 renderImagePanel）
     —— 切到 t2i_h3image 面板，读那段提示的 textContent。
  C. flf / r2v 分辨率上限文案（web/video-panel.js）
     —— 切面板读尺寸上限提示文本 + 实际可选预设数量。

输出：docs/evidence-cr6/targeted_*.json + 截图
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "docs/evidence-cr6")
OUT.mkdir(parents=True, exist_ok=True)

#: 用于取接续上下文的真实任务 id（线上 4 段链的第 2 段，status=done）。
JOB_ID = sys.argv[3] if len(sys.argv) > 3 else "1b29c6628914"

MD = "__MD__"


def main() -> int:
    from playwright.sync_api import sync_playwright

    result: dict = {"base": BASE, "job_id": JOB_ID,
                    "sampled_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path="/usr/bin/google-chrome",
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2500)

        # ---------- A. 接续上下文面板 ----------
        a = page.evaluate(
            """async (jobId) => {
          const out = {ok: false, error: '', texts: [], html: ''};
          if (!window.Continuation || typeof window.Continuation.start !== 'function') {
            out.error = 'Continuation 模块不存在'; return out;
          }
          const resp = await fetch(`/api/jobs/${jobId}/continuation?duration=5`,
                                   {cache: 'no-store'});
          const data = await resp.json();
          out.api_ok = data.ok;
          out.api_error = data.error || '';
          if (!data.ok) return out;
          // 走真实前端路径（applyToPanel + renderPanel）
          await window.Continuation.start({id: jobId});
          await new Promise(r => setTimeout(r, 900));
          const box = document.getElementById('continuationPanel');
          if (!box) { out.error = 'continuationPanel 不存在'; return out; }
          out.hidden = box.hidden;
          out.texts = Array.from(box.querySelectorAll('*'))
            .map(e => (e.textContent || '').trim())
            .filter(t => t && t.length < 400);
          out.html = box.innerHTML.slice(0, 6000);
          out.ok = true;
          return out;
        }""", JOB_ID)
        result["A_continuation_panel"] = a
        try:
            page.screenshot(path=str(OUT / "targeted-continuation-panel.png"))
        except Exception:
            pass
        # 单独把「下一段」那一行抓出来
        result["A_next_segment_line"] = page.evaluate(
            """() => {
          const box = document.getElementById('continuationPanel');
          if (!box) return null;
          const notes = Array.from(box.querySelectorAll('.continuation-note'))
            .map(e => (e.textContent || '').trim());
          return {notes: notes,
                  has_markdown_bold: notes.some(t => t.includes('**'))};
        }""")

        # ---------- B. H3-Image 负面提示词提示 ----------
        page.evaluate("() => window.activateVideoMode && window.activateVideoMode('t2i_h3image')")
        page.wait_for_timeout(900)
        b = page.evaluate(
            """() => {
          const out = {texts: []};
          const nodes = Array.from(document.querySelectorAll('*'));
          for (const el of nodes) {
            const own = Array.from(el.childNodes)
              .filter(n => n.nodeType === 3)
              .map(n => (n.nodeValue || '').trim())
              .filter(Boolean);
            for (const t of own) {
              if (t.includes('负面提示词') || t.includes('数学上无效')) {
                out.texts.push({tag: el.tagName, id: el.id || '',
                                cls: el.className || '', text: t});
              }
            }
          }
          out.has_markdown_bold = out.texts.some(x => x.text.includes('**'));
          return out;
        }""")
        result["B_h3image_negative_prompt_note"] = b
        try:
            page.screenshot(path=str(OUT / "targeted-h3image-negative-note.png"))
        except Exception:
            pass

        # ---------- C. flf / r2v 分辨率上限 ----------
        c: dict = {}
        for mode in ("t2v", "flf", "r2v"):
            page.evaluate("(m) => window.activateVideoMode && window.activateVideoMode(m)", mode)
            page.wait_for_timeout(800)
            c[mode] = page.evaluate(
                """() => {
          const out = {mode_policies: {}, preset_count: 0, preset_keys: [],
                       limit_text: '', options_max_pixels: 0};
          const vo = window.VIDEO_OPTIONS || {};
          out.mode_policies = vo.mode_size_policy || {};
          out.options_max_pixels = vo.max_pixels || 0;
          const presets = vo.size_presets || [];
          out.preset_count = presets.length;
          out.preset_keys = presets.map(p => p.key);
          const sel = document.getElementById('vidSizePresetSelect');
          out.select_options = sel ? Array.from(sel.options).map(o => o.textContent.trim()) : [];
          const hint = document.querySelector('#vidSizeHint, #vidSizeNote, .vid-size-hint');
          out.limit_text = hint ? (hint.textContent || '').trim() : '';
          out.body_has_2m = document.body.innerText.includes('2.0M');
          out.body_has_08m = document.body.innerText.includes('0.8M');
          return out;
        }""")
            try:
                page.screenshot(path=str(OUT / f"targeted-size-{mode}.png"))
            except Exception:
                pass
        result["C_resolution_limits"] = c
        result["page_errors"] = errors
        browser.close()

    (OUT / "targeted_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    a = result["A_continuation_panel"]
    print("=== A. 接续上下文面板 ===")
    print("api_ok:", a.get("api_ok"), " panel_hidden:", a.get("hidden"))
    nxt = result.get("A_next_segment_line") or {}
    print("notes 含 markdown 粗体:", nxt.get("has_markdown_bold"))
    for n in (nxt.get("notes") or [])[:6]:
        print("   note:", n[:160])
    print("\n=== B. H3-Image 负面提示词提示 ===")
    b = result["B_h3image_negative_prompt_note"]
    print("含 markdown 粗体:", b.get("has_markdown_bold"))
    for t in (b.get("texts") or []):
        print(f"   <{t['tag']} id={t['id']} cls={t['cls']}> {t['text'][:180]}")
    print("\n=== C. 各模式分辨率上限 ===")
    for mode, v in result["C_resolution_limits"].items():
        pol = (v.get("mode_policies") or {}).get(mode) or {}
        print(f"  {mode}: policy.max_pixels={pol.get('max_pixels')} "
              f"mp={pol.get('max_pixels_mp')} custom={pol.get('custom_size')} "
              f"presets={v.get('preset_count')} body_2M={v.get('body_has_2m')} "
              f"body_0.8M={v.get('body_has_08m')}")
    print("\npage errors:", errors)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
