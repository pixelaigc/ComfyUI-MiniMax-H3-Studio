import os
from playwright.sync_api import sync_playwright
BASE = "http://127.0.0.1:8190/"
SHOTS = "/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/shots-20261121/shots"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
    ctx = b.new_context(viewport={"width":375,"height":812}, locale="zh-CN", is_mobile=True, has_touch=True)
    pg = ctx.new_page(); pg.goto(BASE, wait_until="domcontentloaded", timeout=90000); pg.wait_for_timeout(3500)

    def drawer_click(sid):
        tog = pg.query_selector('#navToggle')
        if tog and tog.is_visible():
            tog.click(); pg.wait_for_timeout(800)
        el = pg.query_selector('#'+sid)
        if el:
            el.click(); pg.wait_for_timeout(2500); return True
        return False

    # 素材库 全页
    if drawer_click('navAssets'):
        pg.screenshot(path=os.path.join(SHOTS,"13b-narrow375-assets-FULLPAGE.png"), full_page=True)
        print("13b saved")
    # Agent 全页
    if drawer_click('navAgent'):
        pg.screenshot(path=os.path.join(SHOTS,"14b-narrow375-agent-FULLPAGE.png"), full_page=True)
        print("14b saved")
    # 抽屉全页
    tog = pg.query_selector('#navToggle')
    if tog and tog.is_visible():
        tog.click(); pg.wait_for_timeout(900)
        pg.screenshot(path=os.path.join(SHOTS,"12b-narrow375-drawer-FULLPAGE.png"))
        print("12b saved")
    b.close()
for f in ["13b-narrow375-assets-FULLPAGE.png","14b-narrow375-agent-FULLPAGE.png","12b-narrow375-drawer-FULLPAGE.png"]:
    pth=os.path.join(SHOTS,f); print(f, os.path.exists(pth), os.path.getsize(pth) if os.path.exists(pth) else 0)
