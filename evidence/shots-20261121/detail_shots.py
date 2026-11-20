import os, json
from PIL import Image
from playwright.sync_api import sync_playwright

OUT = "/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/shots-20261121"
SHOTS = os.path.join(OUT, "shots")
BASE = "http://127.0.0.1:8190/"

# 1) 桌面：FLF 面板 up-slot（对比度 1.59）裁切
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
    ctx = b.new_context(viewport={"width":1440,"height":900}, device_scale_factor=2, locale="zh-CN")
    pg = ctx.new_page()
    pg.goto(BASE, wait_until="networkidle", timeout=90000); pg.wait_for_timeout(3000)

    # FLF
    pg.click('#sidebar button.nav-model-item[data-mode="flf"][data-model-key="h3_flf"]'); pg.wait_for_timeout(1600)
    pg.screenshot(path=os.path.join(SHOTS, "x-flf-panel.png"), clip={"x":190,"y":44,"width":330,"height":856})
    el = pg.query_selector('#videoPanel')
    if el: el.screenshot(path=os.path.join(SHOTS, "x-flf-videopanel.png"))

    # longfilm: stitchBtn 禁用态对比度 1.65
    pg.click('#sidebar button.nav-model-item[data-mode="longfilm"][data-model-key="h3_longfilm"]'); pg.wait_for_timeout(1600)
    sb = pg.query_selector('#stitchBtn')
    if sb:
        bb = sb.bounding_box()
        print("stitchBtn box", bb)
        pg.screenshot(path=os.path.join(SHOTS, "x-longfilm-stitchbtn.png"),
                      clip={"x":max(0,bb["x"]-30),"y":max(0,bb["y"]-30),"width":min(400,1440-bb["x"]+30),"height":min(120,900-bb["y"]+30)})
    sp = pg.query_selector('#stitchPanel')
    if sp: sp.screenshot(path=os.path.join(SHOTS, "x-longfilm-stitchpanel.png"))

    # 音乐面板（t2a）：rgba(16,16,16,0.3) 的按钮
    pg.click('#sidebar a.nav-item[data-mode="t2a"]'); pg.wait_for_timeout(1600)
    mp = pg.query_selector('#musicPanel')
    if mp: mp.screenshot(path=os.path.join(SHOTS, "x-music-panel.png"))

    # 失败标签页 err-text（对比度 2.0）
    pg.click('.jobs-tabs button[data-st="failed"]'); pg.wait_for_timeout(1600)
    ja = pg.query_selector('#jobsArea')
    if ja: ja.screenshot(path=os.path.join(SHOTS, "x-media-failed.png"))

    # 图片面板 lora 名称截断
    pg.click('#sidebar a.nav-item[data-mode="t2i"]'); pg.wait_for_timeout(1500)
    ip = pg.query_selector('#imagePanel')
    if ip: ip.screenshot(path=os.path.join(SHOTS, "x-image-panel.png"))
    b.close()

# 2) 裁切放大
def crop(src, dst, box, scale=3):
    im = Image.open(os.path.join(SHOTS, src))
    w,h = im.size
    box = (max(0,box[0]), max(0,box[1]), min(w,box[2]), min(h,box[3]))
    im.crop(box).resize(((box[2]-box[0])*scale, (box[3]-box[1])*scale), Image.LANCZOS).save(os.path.join(SHOTS, dst))
    print("crop", dst, box)

print("done")
