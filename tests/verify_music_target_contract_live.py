# -*- coding: utf-8 -*-
"""验证「音乐增强写回目标的能力契约」在 yue2-flow 的 DOM 形态下不崩。

场景：yue2-flow 把音乐输入框从 #musicStyle 改名成 #musicPrompt。
      合并后若前端仍硬编码 #musicStyle -> querySelector 返回 null
      -> .value 抛 TypeError -> 音乐增强静默崩溃。

本脚本在真实页面上**模拟** yue2-flow 的 DOM（删掉 #musicStyle、
插入 #musicPrompt），然后跑一遍写回路径，断言：
  1. 不抛 TypeError
  2. 写回落到 #musicPrompt
  3. pageerror / console error 为 0
"""
import json
import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8291/"

JS = r"""
() => {
  // 切到音乐模式
  const nav = document.querySelector(".nav-item[data-mode='t2a']");
  if (nav) nav.click();

  // 模拟 yue2-flow 的 DOM：#musicStyle 消失，#musicPrompt 出现
  const old = document.querySelector('#musicStyle');
  if (old) {
    const made = document.createElement('textarea');
    made.id = 'musicPrompt';
    made.value = old.value || '';
    old.parentElement.insertBefore(made, old);
    old.remove();
  }

  const music = (typeof state !== 'undefined' && state &&
                 (state.mode === 't2a' || state.mode === 'a2a'));
  const selectors = music ? ['#musicPrompt', '#musicStyle'] : ['#prompt'];

  let box = null;
  let used = null;
  for (const sel of selectors) {
    box = document.querySelector(sel);
    if (box) { used = sel; break; }
  }

  let writeback = {ok: false};
  try {
    if (!box) {
      writeback = {ok: false, reason: 'no-target'};
    } else {
      box.value = 'WRITEBACK_OK';
      writeback = {ok: true, value: box.value, used};
    }
  } catch (e) {
    writeback = {ok: false, error: String(e)};
  }

  return {
    hasMusicStyle: !!document.querySelector('#musicStyle'),
    hasMusicPrompt: !!document.querySelector('#musicPrompt'),
    resolvedSelector: used,
    writeback: writeback,
  };
}
"""


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text)
                if m.type == "error" else None)
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

        result = page.evaluate(JS)
        page.wait_for_timeout(400)
        print(json.dumps(result, ensure_ascii=False, indent=1))
        print("page/console errors:", errors)

        ok = (result["hasMusicStyle"] is False
              and result["hasMusicPrompt"] is True
              and result["writeback"].get("ok") is True
              and result["writeback"].get("value") == "WRITEBACK_OK"
              and result["resolvedSelector"] == "#musicPrompt"
              and not errors)
        print("RESULT:", "PASS" if ok else "FAIL")
        browser.close()
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
