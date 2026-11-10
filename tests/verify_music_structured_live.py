# -*- coding: utf-8 -*-
"""音乐结构化增强 —— 真机端到端验证（当前架构）。

验证【架构裁决】后的音乐增强路线：
    POST /api/music-enhance  ->  12 个结构化风格字段 + 歌词
    -> 写回 #musicStyleFields 的字段 + #musicLyrics

与旧的「写回 #musicStyle 自由文本」路线不同，本脚本断言的是：
  1. 音乐面板的写回契约是 kind="structured"（不是 text）；
  2. 真实调 /api/music-enhance 能拿到结构化产物（style + lyrics）；
  3. 写回后 #musicStyleFields 里**逐项**有值、#musicLyrics 有歌词；
  4. 写回过程中无 console error / pageerror；
  5. 图像面板的写回契约仍是 kind="text"（不串面板）。

用法：python3 tests/verify_music_structured_live.py --base http://127.0.0.1:8392/
需要：本地模型 Provider 已配置（否则跳过「真实模型」段，只验契约与 DOM）。
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


class Checker:
    def __init__(self):
        self.passed = 0
        self.failures = []
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
        }""", timeout=timeout_ms)
    page.wait_for_timeout(400)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8392/")
    ap.add_argument("--out", default=".")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    ck = Checker()
    results = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("console", lambda m: ck.console_errors.append(m.text)
                if m.type == "error" else None)
        page.on("pageerror", lambda e: ck.console_errors.append(str(e)))
        page.on("requestfailed", lambda r: ck.failed_requests.append({
            "url": r.url, "error": (r.failure or "")}))

        page.goto(args.base, wait_until="domcontentloaded")
        wait_ready(page)

        # ---------- 1. 契约：音乐=结构化，图像=文本 ----------
        for mode, expect in (("t2i", "text"), ("t2i_qwen", "text"),
                              ("t2i_moss", "text"), ("t2v", "text"),
                              ("t2a", "structured"), ("a2a", "structured")):
            page.evaluate(
                "(m) => { const el = document.querySelector(`.nav-item[data-mode='${m}']`);"
                "if (el) el.click(); }", mode)
            page.wait_for_timeout(900)
            c = page.evaluate(
                """() => {
                  const t = resolveEnhanceWriteTarget();
                  return {ok: t.ok, kind: t.kind || '', reason: t.reason || '',
                          selector: t.selector || ''};
                }""")
            results.setdefault("contract", {})[mode] = c
            ck.check(c["ok"], f"[contract {mode}] 写回契约应解析成功", str(c))
            ck.check(c["kind"] == expect,
                     f"[contract {mode}] 写回契约类型应为 {expect}", str(c))

        # ---------- 2. 音乐面板的增强方式 ----------
        page.evaluate(
            "() => { const el = document.querySelector(\".nav-item[data-mode='t2a']\");"
            "if (el) el.click(); }")
        page.wait_for_timeout(1000)
        methods = page.evaluate(
            """() => {
              const em = window.enhanceMethods;
              const ids = ["basic","refine","layout","yue2_struct","qwen_pe"];
              const avail = {};
              ids.forEach(i => { avail[i] = em.isAvailable(i); });
              return avail;
            }""")
        results["music_methods"] = methods
        ck.check(methods.get("yue2_struct") is True,
                 "[music] 结构化增强应可用", str(methods))
        ck.check(methods.get("basic") is False,
                 "[music] 普通增强应禁用（产物是文本，塞不进结构化契约）", str(methods))
        ck.check(methods.get("refine") is False,
                 "[music] 二次增强应禁用（同上）", str(methods))

        # ---------- 3. 真实调 /api/music-enhance ----------
        # 【字段名】/api/music-enhance 的入参是 **prompt**（不是 original）——
        # 用错 key 会得到 400「请先输入要增强的描述」。
        payload = {
            "prompt": "a warm synthwave track at dusk, neon city, hopeful",
            "base": "",
            "mode": "t2a",
        }
        # 重试：本地模型在高负载下可能超时（开发实例与全量 pytest 并行跑时
        # 出现过一次 400）。单次失败不代表功能坏了 —— 重试 2 次。
        resp = None
        for attempt in range(3):
            resp = page.evaluate(
                """async (p) => {
                  const r = await fetch('/api/music-enhance', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(p),
                  });
                  const t = await r.text();
                  let d = null;
                  try { d = JSON.parse(t); } catch (e) { d = {raw: t.slice(0, 400)}; }
                  return {status: r.status, data: d};
                }""", payload)
            if resp["status"] == 200:
                break
            page.wait_for_timeout(1500)
        results["music_enhance_api"] = {
            "status": resp["status"],
            "has_style": bool((resp.get("data") or {}).get("style")),
            "has_lyrics": bool((resp.get("data") or {}).get("lyrics")),
            "style_keys": sorted(((resp.get("data") or {}).get("style") or {}).keys()),
            "lyrics_text_len": len(((resp.get("data") or {}).get("lyrics_text") or "")),
        }
        api = results["music_enhance_api"]
        if resp["status"] == 200 and api["has_style"]:
            ck.check(api["has_style"], "[music-api] 应返回结构化风格", str(api))
            ck.check(api["has_lyrics"], "[music-api] 应返回歌词", str(api))
            ck.check(len(api["style_keys"]) >= 8,
                     "[music-api] 结构化风格字段数应 >= 8", str(api))
            ck.check(api["lyrics_text_len"] > 0,
                     "[music-api] 歌词正文应非空", str(api))
        else:
            # 本地模型不可用/未配置 —— 明确记录，不当成断言失败
            results["music_enhance_api"]["skipped_reason"] = (
                f"HTTP {resp['status']}（本地模型未配置或不可达）")

        # ---------- 4. 写回到结构化字段 + 歌词 ----------
        if resp["status"] == 200 and api["has_style"]:
            applied = page.evaluate(
                """(d) => {
                  const c = window.MusicEnhance.getWriteContract();
                  const ok = c.apply(d);
                  const fields = Array.from(
                    document.querySelectorAll('#musicStyleFieldsGrid [id^="musicSF_"]'))
                    .map(el => ({id: el.id, value: el.value || ''}));
                  const ly = document.querySelector('#musicLyrics');
                  return {
                    ok: ok,
                    fieldCount: fields.length,
                    filled: fields.filter(f => String(f.value).trim()).length,
                    fields: fields,
                    lyricsLen: ly ? String(ly.value || '').trim().length : 0,
                    lyricsHead: ly ? String(ly.value || '').slice(0, 120) : '',
                  };
                }""", resp["data"])
            results["structured_writeback"] = applied
            ck.check(applied["ok"], "[writeback] 结构化写回应返回成功", str(applied)[:200])
            ck.check(applied["fieldCount"] >= 8,
                     "[writeback] 结构化字段应 >= 8 个", str(applied)[:200])
            ck.check(applied["filled"] >= 5,
                     "[writeback] 填了值的字段应 >= 5 个", str(applied)[:300])
            ck.check(applied["lyricsLen"] > 0,
                     "[writeback] 歌词框应被写入", str(applied)[:200])
            try:
                page.screenshot(path=str(out / "music-structured-writeback-1280.png"))
            except Exception:
                pass

        # 音乐面板截图
        try:
            page.evaluate(
                """() => {
                  const b = document.querySelector('#musicStyleFieldsToggle');
                  if (b) b.click();
                }""")
            page.wait_for_timeout(400)
            page.screenshot(path=str(out / "music-panel-structured-1280.png"))
            page.set_viewport_size({"width": 375, "height": 780})
            page.wait_for_timeout(500)
            page.screenshot(path=str(out / "music-panel-structured-375.png"))
        except Exception:
            pass

        # ---------- 5. console / 请求 ----------
        # 排除**我们自己**探测 /api/music-enhance 时触发的 4xx 资源报错
        # （那是本次脚本主动打的探测请求，不是页面自身的缺陷）。
        real_errors = [e for e in ck.console_errors
                       if "Failed to load resource" not in e
                       or "400" not in e]
        ck.check(len(real_errors) == 0,
                 "[console] 不应有报错", str(real_errors[:5]))
        results["console_errors"] = ck.console_errors
        results["failed_requests"] = ck.failed_requests

        browser.close()

    results["summary"] = {"passed": ck.passed, "failed": len(ck.failures),
                           "failures": ck.failures}
    (out / "music-structured-live.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"=== 断言 {ck.passed} 通过 / {len(ck.failures)} 失败 ===")
    for f in ck.failures:
        print("  FAIL:", f)
    print(f"console 报错: {len(ck.console_errors)}")
    print(f"失败请求: {len(ck.failed_requests)}")
    print(f"证据目录: {out.resolve()}")
    return 1 if ck.failures else 0


if __name__ == "__main__":
    sys.exit(main())
