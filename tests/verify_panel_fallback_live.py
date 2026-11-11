# -*- coding: utf-8 -*-
"""文生视频面板「接口失败时不能空白」的**实机浏览器验收**。

跑三档视口（375 / 390 / 1280）× 三种场景：

  A. 正常           —— 后端正常，全部下拉有选项，无 console 错误
  B. 后端不可达     —— 模拟服务重启（ERR_CONNECTION_REFUSED）
  C. ComfyUI 卡住   —— 后端在，但 /api/video-options 慢（真实场景）

每档断言：
  * 尺寸预设 / 输出尺寸 / 模型 / 加速 LoRA / 注意力机制 **都有选项**
  * **无 JS 异常**（page error 为空）
  * 失败场景有**明确错误提示**（banner 可见）
  * **没有空的橙色条**（.img-warn 空内容时高度为 0）
  * 横向不溢出

用法：python3 tests/verify_panel_fallback_live.py [base_url]
产物：/tmp/h3-panel-fallback-verify/*.png + report.json
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8195"
OUT = Path("/tmp/h3-panel-fallback-verify")
OUT.mkdir(parents=True, exist_ok=True)

VIEWPORTS = [(375, 812, "375"), (390, 844, "390"), (1280, 900, "1280")]

#: 场景定义：name -> (route handler 或 None)
SCENARIOS = ["normal", "backend_down", "comfy_stuck"]


def _open_panel(page):
    page.goto(BASE, wait_until="load")
    time.sleep(2.0)
    page.evaluate("""() => {
        const el = document.querySelector('.nav-item[data-mode="t2v"]');
        if (el) el.click();
    }""")
    time.sleep(1.0)
    page.evaluate("""() => {
        const fab = document.getElementById('panelFab');
        if (fab) fab.click();
    }""")
    time.sleep(1.5)


def _probe(page):
    return page.evaluate("""() => {
        const sel = id => document.getElementById(id);
        const cnt = id => { const e = sel(id); return e && e.options ? e.options.length : -1; };
        const warn = sel('vidModelWarn');
        const banner = sel('staleVideoBanner');
        const sizeSel = sel('vidSizePresetSelect');
        return {
            sizePresets: cnt('vidSizePresetSelect'),
            models: cnt('vidModelSelect'),
            accelLoras: cnt('vidAccelLoraSelect'),
            attention: cnt('vidAttentionSelect'),
            aspectChips: sel('vidAspectChips') ? sel('vidAspectChips').children.length : -1,
            width: sel('vidWidth') ? sel('vidWidth').value : '',
            height: sel('vidHeight') ? sel('vidHeight').value : '',
            modelValue: sel('vidModelSelect') ? sel('vidModelSelect').value : '',
            sizePresetValue: sizeSel ? sizeSel.value : '',
            attentionValue: sel('vidAttentionSelect') ? sel('vidAttentionSelect').value : '',
            // 空的 .img-warn 不该有可见高度（否则就是那条橙色空条）
            warnText: warn ? warn.textContent : '',
            warnVisibleHeight: warn ? warn.getBoundingClientRect().height : 0,
            bannerVisible: banner ? !banner.hidden : false,
            bannerText: banner ? banner.textContent.slice(0, 200) : '',
            ready: window.VIDEO_PANEL ? window.VIDEO_PANEL.isReady() : false,
            submitModelKey: window.VIDEO_PANEL ?
                window.VIDEO_PANEL.submitParams().model_key : '',
            overflow: document.documentElement.scrollWidth -
                      document.documentElement.clientWidth,
        };
    }""")


def main():
    report = {"base": BASE, "scenarios": {}, "ok": False}
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        for scenario in SCENARIOS:
            report["scenarios"][scenario] = {}
            for width, height, tag in VIEWPORTS:
                context = browser.new_context(
                    viewport={"width": width, "height": height},
                    device_scale_factor=2 if width < 500 else 1)
                page = context.new_page()
                console_errors = []
                page_errors = []
                page.on("console", lambda m, bag=console_errors: (
                    bag.append({"type": m.type, "text": m.text})
                    if m.type == "error" else None))
                page.on("pageerror", lambda e, bag=page_errors: bag.append(str(e)))

                if scenario == "backend_down":
                    page.route("**/api/video-options*",
                               lambda r: r.abort("connectionrefused"))
                elif scenario == "comfy_stuck":
                    # 模拟 ComfyUI 挂住：接口挂起（真实场景是 66 秒，
                    # 这里挂 8 秒足以让浏览器放弃）
                    def slow(route):
                        time.sleep(8)
                        route.abort("connectionrefused")
                    page.route("**/api/video-options*", slow)

                _open_panel(page)
                time.sleep(6 if scenario != "normal" else 2)
                data = _probe(page)

                # 判定
                data["console_errors"] = console_errors
                data["page_errors"] = page_errors
                data["no_page_error"] = not page_errors
                data["dropdowns_ok"] = (
                    data["sizePresets"] > 1 and data["models"] > 0
                    and data["accelLoras"] > 1 and data["attention"] > 0
                    and data["aspectChips"] > 0)
                # 空的橙色条：有内容时高度 > 0；没内容时高度必须 == 0
                data["no_empty_orange_bar"] = (
                    data["warnText"].strip() != ""
                    or data["warnVisibleHeight"] == 0)
                data["overflow_ok"] = data["overflow"] <= 1
                if scenario == "normal":
                    data["banner_ok"] = not data["bannerVisible"]
                else:
                    data["banner_ok"] = (data["bannerVisible"]
                                         and len(data["bannerText"]) > 10)

                data["ok"] = all([
                    data["dropdowns_ok"], data["no_page_error"],
                    data["no_empty_orange_bar"], data["overflow_ok"],
                    data["banner_ok"],
                ])
                report["scenarios"][scenario][tag] = data

                page.screenshot(path=str(OUT / f"{scenario}-{tag}.png"))
                try:
                    sel = page.query_selector("#vidModelSelect")
                    if sel:
                        box = sel.bounding_box()
                        if box:
                            page.screenshot(
                                path=str(OUT / f"{scenario}-{tag}-zoom.png"),
                                clip={"x": max(0, box["x"] - 12),
                                      "y": max(0, box["y"] - 90),
                                      "width": min(box["width"] + 24, width),
                                      "height": box["height"] + 160})
                except Exception:
                    pass
                context.close()
        browser.close()

    report["ok"] = all(
        v.get("ok") for sc in report["scenarios"].values() for v in sc.values())
    (OUT / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # 打印摘要
    for sc, data in report["scenarios"].items():
        print(f"=== {sc} ===")
        for tag, v in data.items():
            print(f"  {tag}: ok={v['ok']} "
                  f"下拉={v['sizePresets']}/{v['models']}/{v['accelLoras']}/"
                  f"{v['attention']} chips={v['aspectChips']} "
                  f"宽高={v['width']}x{v['height']} "
                  f"pageErr={len(v['page_errors'])} "
                  f"consoleErr={len(v['console_errors'])} "
                  f"橙空条={not v['no_empty_orange_bar']} "
                  f"banner={v['bannerVisible']} overflow={v['overflow']}")
            if v["page_errors"]:
                print(f"      page errors: {v['page_errors'][:2]}")
    print("OVERALL:", report["ok"])
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
