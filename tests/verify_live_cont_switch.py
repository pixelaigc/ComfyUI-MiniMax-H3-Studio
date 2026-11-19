# -*- coding: utf-8 -*-
"""线上验收：接续开关「接续段也用二采」在**真实 8190 服务**上显形。

【这个脚本在验什么】
本轮修复已上线（live d1304db）。但「代码在位」不等于「用户看得到」——
必须对着**线上服务**、用**真浏览器**走一遍真实接续流程，
确认开关在界面上**真的显形**。

【为什么不能用测试实例代替】
测试实例是临时起的独立进程，数据目录 / 素材库 / ComfyUI 连接都不同。
线上是 systemd 托管的真实服务，静态资源走 /static 挂载、
前端走 index.html 里的 ?v= 版本号 —— 只有对着线上跑才验得到
「浏览器真的加载到了新文件」。

【为什么要在浏览器里拦 fetch】
开关显形的前提是「**有二采** 且 **有接续上下文**」。
线上现有的可接续样本，其上游记录的 `twopass.enabled` 都是 **false**
（实测 data/jobs.db 里 5 条 r2v 全是 false）⇒ 接续回填后面板的二采
就是关的 ⇒ 开关**本来就应该**隐藏（普通段本来就能用二采）。
⇒ 用现成样本验不出「开关该显形」的那一档。

所以这里在**浏览器里**拦一次 `/api/jobs/{id}/continuation` 的响应，
把返回的 `params.twopass.enabled` 改成 true，再交给**线上那份
continuation.js** 走完整流程（fetchContext -> applyToPanel ->
current=ctx -> renderPanel -> applyAssetRefs -> rerenderVideoPanel）。

【口径 —— 只 fake 一个字段，不 fake 代码路径】
  * 假的是**上游那一段的二采设置**（一个数据字段）；
  * 跑的是**线上真实部署的那份 continuation.js**；
  * 开关显不显形由那份代码决定 —— 修复没上线就不会显形。
这是「线上确实生效」的最强证据（不改线上 DB、不造假任务、不起生成）。

用法：
    /home/ubuntu/comfyui/venv/bin/python tests/verify_live_cont_switch.py
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:
    HAS_PLAYWRIGHT = False

BASE = "http://127.0.0.1:8190"


def api(path: str):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=30) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw else {}


def main() -> int:
    if not HAS_PLAYWRIGHT:
        print("需要 playwright")
        return 2

    # ---- 1. 线上有没有可接续样本 ----
    data = api("/api/jobs")
    rows = data if isinstance(data, list) else data.get("jobs", data.get("rows", []))
    candidates = [r for r in rows
                  if r.get("mode") in ("t2v", "flf", "r2v")
                  and (r.get("continuation_info") or {}).get("can_continue")]
    print(f"线上任务 {len(rows)} 条，可接续样本 {len(candidates)} 条")
    if not candidates:
        print("!! 线上没有可接续样本 —— 无法做线上显形验收（不伪造通过）")
        return 3

    out = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        errors = []
        dialogs = []

        def _on_dialog(d):
            dialogs.append(f"{d.type}: {d.message}")
            d.accept()

        page.on("dialog", _on_dialog)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{BASE}/", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        # ---- 拦 fetch：把上游的二采设置改成 true（只 fake 这一个字段）----
        page.evaluate("""() => {
          const orig = window.fetch;
          window.fetch = async function (url, opts) {
            const resp = await orig.apply(this, arguments);
            if (String(url).indexOf('/continuation') >= 0) {
              const clone = resp.clone();
              try {
                const data = await clone.json();
                if (data && data.params) {
                  data.params.twopass = Object.assign(
                    {}, data.params.twopass || {}, {enabled: true});
                }
                return new Response(JSON.stringify(data), {
                  status: resp.status,
                  headers: {'Content-Type': 'application/json'},
                });
              } catch (e) { /* 解析失败就原样返回 */ }
            }
            return resp;
          };
        }""")

        for row in candidates:
            jid = row["id"]
            mode = row["mode"]
            page.evaluate("() => { if (window.Continuation) window.Continuation.clear(); }")
            page.wait_for_timeout(300)

            page.evaluate(
                """async (jid) => {
                     const r = await fetch('/api/jobs/' + jid + '/continuation');
                     const d = await r.json();
                     if (!d.ok) throw new Error(d.error || '取接续上下文失败');
                     await window.Continuation.start(
                       { id: jid, mode: d.mode, status: 'done', video_name: 'p.mp4' });
                   }""",
                jid)
            page.wait_for_timeout(2500)

            state = page.evaluate("""() => {
              const row = document.getElementById('vidTwopassAllowContinuationRow');
              const box = document.getElementById('vidTwopassAllowContinuation');
              const cur = window.Continuation && window.Continuation.state
                ? window.Continuation.state().current : null;
              const st = window.VIDEO_PANEL && window.VIDEO_PANEL.state
                ? window.VIDEO_PANEL.state : {};
              const r = row ? row.getBoundingClientRect() : null;
              return {
                row_exists: !!row,
                row_hidden: row ? row.hidden : null,
                row_has_box: !!(r && r.width > 0 && r.height > 0),
                has_current: !!cur,
                twopass_enabled: !!(st.twopass && st.twopass.enabled),
                box_disabled: box ? box.disabled : null,
              };
            }""")
            state["mode"] = mode
            state["job_id"] = jid
            out[jid] = state
            print(f"[{mode}] {jid} -> {json.dumps(state, ensure_ascii=False)}")

        # ---- 负向对照：清掉上下文后开关必须重新隐藏 ----
        page.evaluate("() => { if (window.Continuation) window.Continuation.clear(); }")
        page.wait_for_timeout(800)
        after_clear = page.evaluate("""() => {
          const row = document.getElementById('vidTwopassAllowContinuationRow');
          return {row_hidden: row ? row.hidden : null,
                  has_current: !!(window.Continuation &&
                                  window.Continuation.state().current)};
        }""")
        print(f"[清除后] -> {json.dumps(after_clear, ensure_ascii=False)}")
        browser.close()

    js_errors = [e for e in errors if "Failed to load resource" not in e]
    print()
    print(f"JS 报错: {js_errors if js_errors else '无'}")
    print(f"alert/confirm: {dialogs if dialogs else '无'}")

    fail = 0
    for jid, st in out.items():
        if not st["has_current"]:
            print(f"!! {jid}: 接续上下文未就绪")
            fail = 1
        elif not st["twopass_enabled"]:
            print(f"!! {jid}: 二采未开（fake 失效，验收无效）")
            fail = 1
        elif st["row_hidden"] or not st["row_has_box"]:
            print(f"!! {jid}: 二采已开但开关仍隐藏 —— 线上未生效")
            fail = 1
        else:
            print(f"[ OK ] {jid} ({st['mode']}): 开关**显形**（有渲染盒）")
    if after_clear.get("row_hidden") is not True:
        print("!! 清除上下文后开关没有重新隐藏")
        fail = 1
    else:
        print("[ OK ] 清除上下文后开关重新隐藏")
    if js_errors:
        fail = 1

    out_dir = REPO / "evidence" / "cont-switch-live-verify"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "live_result.json").write_text(
        json.dumps({"results": out, "after_clear": after_clear,
                    "js_errors": js_errors, "dialogs": dialogs},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"证据落盘：{out_dir / 'live_result.json'}")
    return fail


if __name__ == "__main__":
    raise SystemExit(main())
