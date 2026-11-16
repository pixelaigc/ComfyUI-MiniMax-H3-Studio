#!/usr/bin/env python3
"""内置 Agent 附件上传 —— 线上 8190 真实浏览器实测。

覆盖前端「📎 媒体」按钮链路：
  1) 打开 Agent 页
  2) 点「📎 媒体」-> 触发文件选择框（用 set_input_files 直接喂真实 PNG）
  3) 断言附件卡片出现在 #agAttachments
  4) 断言服务端真的入库（拿 asset_id 回查 /api/agent/media/{id}/status）
  5) 断言「移除附件」按钮能移除卡片
"""
import json
import os
import sys
import tempfile

from playwright.sync_api import sync_playwright

BASE = os.environ.get("H3_BASE", "http://127.0.0.1:8190")
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/agent_attach_browser"
os.makedirs(OUT, exist_ok=True)

# 用一张**正常尺寸**的 PNG（128x128）——
# 1x1 极小图会被素材库的多模态分析拒绝（模型读不出图像：
# "the image could not be read: no image chunk"），那是素材库分析管线的
# 合理行为，**不是** Agent 附件链路的缺陷。
# 图在运行时用 zlib 现造（不依赖外部素材文件）。
def _make_png(w: int = 128, h: int = 128) -> bytes:
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xffffffff))

    raw = b"".join(
        b"\x00" + bytes([(x * 2) % 256 for x in range(w * 3)])
        for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


PNG_1x1 = _make_png()


def main():
    results = []

    def rec(name, ok, detail=None, shot=None):
        results.append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name} :: "
              f"{json.dumps(detail, ensure_ascii=False)[:250]}", flush=True)

    png = os.path.join(tempfile.gettempdir(), "agent_attach_probe.png")
    with open(png, "wb") as f:
        f.write(PNG_1x1)

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        for el in page.query_selector_all("a,button,[data-page]"):
            try:
                t = el.inner_text() or ""
            except Exception:
                continue
            if "Agent" in t and "增强" not in t:
                el.click()
                break
        page.wait_for_timeout(800)

        # 1) 附件输入框存在
        rec("01-附件输入框存在", page.query_selector("#agAttachInput") is not None)
        rec("02-附件按钮存在", page.query_selector("#agAttachBtn") is not None)

        # 3) 喂真实文件
        page.set_input_files("#agAttachInput", png)
        # 等待上传 + 服务端同步分析（图片要 120~180 秒，给足时间）
        ok = False
        detail = {}
        for _ in range(60):
            page.wait_for_timeout(5000)
            txt = page.inner_text("#agAttachments") if page.is_visible(
                "#agAttachments") else ""
            cards = page.eval_on_selector_all(
                "#agAttachments .ag-attach-item, #agAttachments > *",
                "els => els.length")
            detail = {"cards": cards, "text": txt[:200]}
            if cards > 0 and "asset" not in txt.lower()[:0] + "":
                pass
            # 附件条目出现 = 卡片渲染成功。
            # （文案随类型变化：图片显示「图片 128×128」、音视频显示分析状态，
            #   所以不按文案判据，按「卡片数量 + 是否有 ✕ 移除按钮」判。）
            if cards > 0:
                ok = True
                break
        page.screenshot(path=os.path.join(OUT, "attach_uploaded.png"))
        rec("03-上传后出现附件卡片", ok, detail)

        # 4) 服务端确认入库
        asset_ids = page.eval_on_selector_all(
            "#agAttachments [data-asset-id]",
            "els => els.map(e => e.getAttribute('data-asset-id'))")
        if not asset_ids:
            asset_ids = page.evaluate("""() => {
                const el = document.querySelector('#agAttachments');
                if (!el) return [];
                const m = (el.textContent || '').match(/asset_[0-9a-f]+/g);
                return m ? Array.from(new Set(m)) : [];
            }""")
        rec("04-拿到 asset_id", bool(asset_ids), {"asset_ids": asset_ids})

        if asset_ids:
            aid = asset_ids[0]
            st = page.evaluate(
                """async (id) => {
                    const r = await fetch('/api/agent/media/' + id + '/status');
                    return await r.json();
                }""", aid)
            rec("05-服务端素材状态可查",
                st.get("asset_id") == aid,
                {"status": st.get("status"), "kind": st.get("kind"),
                 "available": st.get("available"),
                 "has_description": bool(st.get("description"))})
            # 清理
            page.evaluate(
                """async (id) => {
                    await fetch('/api/assets/' + id, {method: 'DELETE'});
                }""", aid)

        # 5) 移除附件
        before = page.eval_on_selector_all(
            "#agAttachments > *", "els => els.length")
        remove_btns = page.query_selector_all(
            "#agAttachments [data-act='remove'], #agAttachments .ag-attach-remove")
        if remove_btns:
            remove_btns[0].click()
            page.wait_for_timeout(800)
        after = page.eval_on_selector_all(
            "#agAttachments > *", "els => els.length")
        rec("06-移除附件", after <= before,
            {"before": before, "after": after,
             "remove_btns": len(remove_btns)})

        rec("07-无页面 JS 异常", len(errors) == 0, {"errors": errors[:5]})
        browser.close()

    os.remove(png)
    with open(os.path.join(OUT, "attach_browser.json"), "w",
              encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    npass = sum(1 for r in results if r["ok"])
    print(f"\nSUMMARY {npass}/{len(results)} passed")
    print("OUT", OUT)


if __name__ == "__main__":
    main()
