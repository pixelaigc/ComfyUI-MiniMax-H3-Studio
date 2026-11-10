#!/usr/bin/env python3
"""真机验证：前端确实**消费**服务端下发的 length_hint_text。

【为什么要有这个脚本】
  源码层面「前端不硬编码」只能证明它没抄一份字面量，
  但证明不了它**真的用上了**服务端那份 —— 例如取字段名取错、
  端点没返回、或者赋值顺序不对，结果就是预览里时长描述空白。

本脚本在真实页面上：
  1. 读前端 OPTIONS.length_hint_text（应与服务端一致）
  2. 切到文生音乐，改 length_hint 字段的值
  3. 断言「拼接结果」预览里的英文描述**随之变化**（= 确实在消费）
  4. 断言前端内存里的映射与服务端 /api/music-options 返回的**逐条一致**

用法：python3 tests/verify_length_hint_downstream.py --base http://127.0.0.1:8195
"""
import argparse
import json
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

FIELDS = ["language", "genre", "mood", "vocal", "vocal_color", "instruments",
          "tempo_bpm", "energy", "length_hint", "extra",
          "custom_language", "custom_genre"]


def api(base, path):
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=20) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8195")
    ap.add_argument("--out", default="/tmp/length_hint_check")
    args = ap.parse_args()

    failures = []
    checks = []

    def check(ok, label, detail=""):
        checks.append({"ok": bool(ok), "label": label, "detail": str(detail)[:400]})
        if not ok:
            failures.append(f"{label} :: {detail}")
        print(("PASS " if ok else "FAIL ") + label +
              ("" if ok else " :: " + str(detail)[:300]))

    server_opts = api(args.base, "/api/music-options")
    server_map = server_opts.get("length_hint_text") or {}
    check(len(server_map) > 0, "服务端下发 length_hint_text 非空",
          json.dumps(server_map, ensure_ascii=False))

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        errors = []
        page.on("console", lambda m: errors.append(f"{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        page.goto(args.base, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_function(
            """() => {
              try {
                if (typeof provState === 'undefined' || !provState) return false;
                const mf = provState.modeFilters;
                if (!mf || typeof mf !== 'object') return false;
                if (!Object.keys(mf).length) return false;
                return !!document.querySelector('#musicPrompt');
              } catch (e) { return false; }
            }""", timeout=45000)
        page.wait_for_timeout(800)

        # 切到文生音乐
        page.evaluate(
            "() => { const el = document.querySelector"
            "(`.nav-item[data-mode='t2a']`); if (el) el.click(); }")
        page.wait_for_timeout(1200)

        # 展开结构化风格区（否则字段还没渲染）
        page.evaluate("""
            () => {
              const t = document.querySelector('#musicStyleFieldsToggle');
              if (t && t.textContent.trim() === '展开') t.click();
            }
        """)
        page.wait_for_timeout(600)

        # 前端内存里的映射（OPTIONS.length_hint_text）
        frontend_map = page.evaluate(
            """() => {
              const o = window.MusicUI && window.MusicUI.getOptions
                        ? window.MusicUI.getOptions() : null;
              return o ? (o.length_hint_text || {}) : null;
            }""")
        check(frontend_map is not None,
              "前端能读到 OPTIONS.length_hint_text",
              json.dumps(frontend_map, ensure_ascii=False)[:200])

        # 前端映射必须与服务端**逐条一致**（值也要一致，不只是键）
        check(frontend_map == server_map,
              "前端 length_hint_text 与服务端下发逐条一致",
              f"front={json.dumps(frontend_map, ensure_ascii=False)} "
              f"server={json.dumps(server_map, ensure_ascii=False)}")

        # 改 length_hint -> 预览里的英文描述应随之变化
        # （模拟用户在下拉里选 —— 走真实的 change 事件）
        probe = page.evaluate("""
            () => {
              const sel = document.querySelector('#musicSF_length_hint');
              const preview = document.querySelector('#musicStyleFieldsPreview');
              if (!sel) return {error: 'no-select'};
              const opts = Array.from(sel.options).map(o => o.value);
              const key = opts.find(v => v.includes('full song')) || opts[opts.length-1];
              const before = preview ? preview.textContent : '';
              sel.value = key;
              sel.dispatchEvent(new Event('change', {bubbles: true}));
              return {key: key, before: before, opts: opts};
            }
        """)
        time.sleep(0.8)
        after = page.evaluate("""
            () => {
              const preview = document.querySelector('#musicStyleFieldsPreview');
              return preview ? preview.textContent : '';
            }
        """)
        expected = server_map.get(probe.get("key"), "")
        check(bool(expected) and expected in after,
              "预览里的英文时长描述来自服务端下发的映射",
              f"key={probe.get('key')!r} expected={expected!r} after={after[-160:]!r}")

        # 反证：换一个 key，预览内容应随之改变（证明不是写死的）
        probe2 = page.evaluate("""
            () => {
              const sel = document.querySelector('#musicSF_length_hint');
              if (!sel) return {};
              const opts = Array.from(sel.options).map(o => o.value);
              const key = opts.find(v => v.includes('short clip')) || opts[0];
              sel.value = key;
              sel.dispatchEvent(new Event('change', {bubbles: true}));
              return {key: key};
            }
        """)
        time.sleep(0.8)
        after2 = page.evaluate("""
            () => {
              const preview = document.querySelector('#musicStyleFieldsPreview');
              return preview ? preview.textContent : '';
            }
        """)
        expected2 = server_map.get(probe2.get("key"), "")
        check(bool(expected2) and expected2 in after2 and after2 != after,
              "换一个 length_hint 后预览随之变化（证明不是写死的）",
              f"key={probe2.get('key')!r} expected={expected2!r} after2={after2[-160:]!r}")

        check(not errors, "console 无报错", json.dumps(errors[:5], ensure_ascii=False))

        shot = args.out + "-length-hint.png"
        page.screenshot(path=shot, full_page=False)
        ctx.close()
        browser.close()

    print(f"\n=== 断言 {len(checks)-len(failures)} 通过 / {len(failures)} 失败 ===")
    for f in failures:
        print("  FAIL:", f)
    print("截图:", shot)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
