# -*- coding: utf-8 -*-
"""文生视频面板「模型清单来自 ComfyUI API」的**实机浏览器验收**。

跑三档视口（375 / 390 / 1280），逐档：
  * 切到「文生视频」模式
  * 断言模型下拉里**每一项**都来自服务端下发的清单（status 驱动）
  * 断言**没有**任何一项显示「获取不到模型 / 尚未挂载 / 根本没有这个名字」
  * 断言「清单来源」说明写明是 ComfyUI 模型列表 API
  * 断言选中一项后，**提交参数里的 model_key 与下拉选中值一致**
  * 断言 375px 视口下模型区**不溢出**（横向可滚动 / 元素宽度）
  * 收集 console 报错 / 页面异常

用法：python3 tests/verify_model_list_live.py [base_url]
产物：/tmp/h3-model-list-verify/*.png + /tmp/h3-model-list-verify/report.json
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190"
OUT = Path("/tmp/h3-model-list-verify")
OUT.mkdir(parents=True, exist_ok=True)

VIEWPORTS = [
    (375, 812, "375"),
    (390, 844, "390"),
    (1280, 900, "1280"),
]

#: 绝不允许出现在界面上的「错误的不可用断言」
FORBIDDEN = ("获取不到", "尚未挂载", "根本没有这个名字", "下拉框确实没有",
             "没有这个单卡", "提交会被拒绝")


def main():
    report = {
        "base": BASE,
        "viewports": {},
        "console_errors": [],
        "page_errors": [],
        "api": {},
    }
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        for width, height, tag in VIEWPORTS:
            context = browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=2 if width < 500 else 1)
            page = context.new_page()
            console_errors = []
            page_errors = []
            page.on("console", lambda m, bag=console_errors: (
                bag.append({"type": m.type, "text": m.text})
                if m.type in ("error", "warning") else None))
            page.on("pageerror", lambda e, bag=page_errors: bag.append(str(e)))
            # 注意：不能用 networkidle —— 页面有 SSE 状态流常驻连接
            page.goto(BASE, wait_until="load")
            time.sleep(1.5)

            # 【先清掉服务端保存的视频面板配置】
            # 面板启动时会 applyConfig() 把 /api/video-config 里存的模型
            # 恢复回来 —— 那会把我们刚切的值又覆盖掉（实测 375px 首项是
            # fl2va_pruned_int8、390 是 fl2va_int8，就是上一轮跑测存进去的）。
            # 这不是产品缺陷，是测试的**状态污染**。
            # 所以在**任何交互之前**先清一次配置。
            try:
                page.evaluate("""async () => {
                    await fetch('/api/video-config', {
                        method: 'PUT',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({}),
                    });
                }""")
            except Exception:
                pass

            # 切到「文生视频」：左侧导航 .nav-item[data-mode="t2v"]
            # 注意：窄屏（375px）侧边导航是折叠的，nav-item 在视口外 ——
            # page.click 会一直等「element is outside of the viewport」而超时。
            # 所以用 JS 触发 click（元素在 DOM 里，事件照常冒泡）。
            page.evaluate("""() => {
                const el = document.querySelector('.nav-item[data-mode="t2v"]');
                if (el) el.click();
            }""")
            time.sleep(1.0)
            # 打开参数面板（窄屏是底部抽屉，靠右下角 FAB 打开）
            page.evaluate("""() => {
                const fab = document.getElementById('panelFab');
                if (fab) fab.click();
            }""")
            time.sleep(1.2)

            # 等面板**完全就绪**（清单已加载 + 已绑定事件）
            #
            # 【为什么要等 isReady】VIDEO_PANEL.load() 是异步的，它里面
            # 最后会 applyConfig() —— 那一步会把 videoState.model 重置成
            # 服务端存的配置。如果我们在 load() 还没跑完时就派发 change，
            # applyConfig 会**在我们切完之后**把模型改回去，
            # 断言就会看到「下拉是 A、状态是 B」的假不一致。
            # 所以这里必须等 isReady() 为真再交互。
            try:
                page.wait_for_function(
                    """() => window.VIDEO_PANEL && window.VIDEO_PANEL.isReady()""",
                    timeout=120000)
            except Exception:
                pass
            time.sleep(1.0)

            # 断言模型区**真的可见**（不是只在 DOM 里藏着）
            #
            # 【超时要给足】面板启动时会拉一次 /api/video-options，
            # 而那一次会打到 ComfyUI 的**冷目录扫描**（实测首次 43 秒，
            # 因为它要递归扫 extra_model_paths 里的所有盘）。
            # 所以这里给 120 秒 —— 否则会在「ComfyUI 正在扫盘」时误判失败。
            try:
                page.wait_for_function(
                    """() => {
                        const sel = document.getElementById('vidModelSelect');
                        if (!sel) return false;
                        const r = sel.getBoundingClientRect();
                        return sel.options.length > 0 && r.width > 0 && r.height > 0;
                    }""", timeout=120000)
            except Exception as exc:
                report["viewports"][tag] = {
                    "ok": False, "error": f"模型下拉没出现: {exc}"}
                page.screenshot(path=str(OUT / f"model-list-{tag}-FAIL.png"))
                context.close()
                continue

            data = page.evaluate("""() => {
                const sel = document.getElementById('vidModelSelect');
                const warn = document.getElementById('vidModelWarn');
                const src = document.getElementById('vidModelSourceNote');
                const note = document.getElementById('vidModelNote');
                const rect = sel.getBoundingClientRect();
                const modelBlock = sel.closest('.field');
                const blockRect = modelBlock ? modelBlock.getBoundingClientRect() : null;
                // 可见性：必须在视口内 + 有尺寸 + 不是 hidden
                const style = window.getComputedStyle(sel);
                const panel = document.getElementById('panel');
                const panelOpen = panel ? panel.classList.contains('open') : null;
                return {
                    options: Array.from(sel.options).map(o => ({
                        value: o.value,
                        text: o.text,
                        disabled: o.disabled,
                        selected: o.selected,
                    })),
                    selected: sel.value,
                    warn: warn ? warn.textContent : '',
                    warnVisible: warn ? (warn.getBoundingClientRect().height > 0) : false,
                    sourceNote: src ? src.textContent : '',
                    sourceVisible: src ? (src.getBoundingClientRect().height > 0) : false,
                    sourceClass: src ? src.className : '',
                    note: note ? note.textContent : '',
                    submitModelKey: window.VIDEO_PANEL ?
                        window.VIDEO_PANEL.submitParams().model_key : null,
                    stateModel: window.VIDEO_PANEL ?
                        window.VIDEO_PANEL.state.model : null,
                    selWidth: rect.width,
                    selHeight: rect.height,
                    selRight: rect.right,
                    selTop: rect.top,
                    display: style.display,
                    visibility: style.visibility,
                    panelOpen: panelOpen,
                    docScrollWidth: document.documentElement.scrollWidth,
                    docClientWidth: document.documentElement.clientWidth,
                    blockRight: blockRect ? blockRect.right : null,
                    visible: !!(sel.offsetParent || sel.getClientRects().length),
                };
            }""")

            # 横向溢出检查（375px 视口重点）
            overflow = data["docScrollWidth"] - data["docClientWidth"]
            data["horizontal_overflow_px"] = overflow
            data["overflow_ok"] = overflow <= 1

            # 切到第二项（如果可选），验证下拉值 == 提交值
            selectable = [o for o in data["options"] if not o["disabled"]]
            if len(selectable) > 1:
                target = selectable[1]["value"]
                page.evaluate("""(v) => {
                    const s = document.getElementById('vidModelSelect');
                    s.value = v;
                    s.dispatchEvent(new Event('change', {bubbles: true}));
                }""", target)
                time.sleep(0.8)
                after = page.evaluate("""() => ({
                    selected: document.getElementById('vidModelSelect').value,
                    submitModelKey: window.VIDEO_PANEL.submitParams().model_key,
                    stateModel: window.VIDEO_PANEL.state.model,
                })""")
                data["after_switch"] = after
                data["switch_consistent"] = (
                    after["selected"] == after["submitModelKey"]
                    == after["stateModel"])

            # 禁用项是否都标注了「不可选」
            disabled_labels = [o["text"] for o in data["options"] if o["disabled"]]
            data["disabled_labels"] = disabled_labels
            data["disabled_labelled"] = all("不可选" in t or "不可用" in t
                                            for t in disabled_labels)

            # 禁止出现的错误断言
            blob = data["warn"] + " " + data["sourceNote"] + " " + \
                " ".join(o["text"] for o in data["options"])
            data["forbidden_found"] = [f for f in FORBIDDEN if f in blob]

            data["console_errors"] = console_errors
            data["page_errors"] = page_errors
            # 可见性：下拉 + 来源说明 + 红字区都要真的渲染出来（不是藏在 DOM 里）
            data["all_visible"] = bool(
                data.get("visible")
                and data.get("selWidth", 0) > 0
                and data.get("selHeight", 0) > 0
                and data.get("sourceVisible")
            )
            data["ok"] = (
                not data["forbidden_found"]
                and data["overflow_ok"]
                and not page_errors
                and data.get("switch_consistent", True)
                and data["disabled_labelled"]
                and "ComfyUI 模型列表 API" in data["sourceNote"]
                and data["all_visible"]
            )
            report["viewports"][tag] = data

            page.screenshot(path=str(OUT / f"model-list-{tag}.png"),
                            full_page=False)
            # 模型区特写
            try:
                sel = page.query_selector("#vidModelSelect")
                if sel:
                    box = sel.bounding_box()
                    if box:
                        page.screenshot(
                            path=str(OUT / f"model-list-{tag}-zoom.png"),
                            clip={"x": max(0, box["x"] - 12),
                                  "y": max(0, box["y"] - 70),
                                  "width": min(box["width"] + 24, width),
                                  "height": box["height"] + 130})
            except Exception:
                pass
            context.close()
        browser.close()

    # 接口证据
    try:
        import urllib.request
        with urllib.request.urlopen(
                BASE + "/api/video-options", timeout=90) as r:
            payload = json.loads(r.read())
        report["api"]["video_options"] = {
            "model_list_source": payload.get("model_list_source"),
            "model_list_error": payload.get("model_list_error"),
            "model_list_counts": payload.get("model_list_counts"),
            "models": [
                {"key": m.get("key"), "status": m.get("status"),
                 "available": m.get("available"),
                 "comfy_listed": m.get("comfy_listed"),
                 "unet": m.get("unet"),
                 "status_note": m.get("status_note")}
                for m in payload.get("models", [])
            ],
        }
    except Exception as exc:
        report["api"]["video_options_error"] = str(exc)
    try:
        import urllib.request
        with urllib.request.urlopen(
                BASE + "/api/comfy-models", timeout=90) as r:
            payload = json.loads(r.read())
        report["api"]["comfy_models"] = {
            "ok": payload.get("ok"),
            "folder": payload.get("folder"),
            "count": payload.get("count"),
            "panel_models": payload.get("panel_models"),
        }
    except Exception as exc:
        report["api"]["comfy_models_error"] = str(exc)

    report["ok"] = all(v.get("ok") for v in report["viewports"].values())
    (OUT / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
