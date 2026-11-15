
import json, sys, time
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8190"
OUT = sys.argv[1]

console_msgs = []
page_errors = []

def snap(page, name):
    page.screenshot(path=f"{OUT}/{name}.png", full_page=False)
    print("SHOT", name)

with sync_playwright() as p:
    browser = p.chromium.launch(args=["--no-sandbox"])
    ctx = browser.new_context(viewport={"width":1600,"height":950})
    page = ctx.new_page()
    page.on("console", lambda m: console_msgs.append(f"[{m.type}] {m.text}"))
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    # 记录所有 /api/jobs 请求
    reqs = []
    def on_req(r):
        if "/api/jobs" in r.url:
            reqs.append(r.url)
    page.on("request", on_req)

    page.goto(BASE + "/", wait_until="networkidle")
    time.sleep(2)

    # 记录初始状态
    state0 = page.evaluate("""() => ({
        doneMedia: document.querySelector('#doneMedia')?.value,
        doneSearch: document.querySelector('#doneSearch')?.value,
        doneAspect: document.querySelector('#doneAspect')?.value,
        doneDuration: document.querySelector('#doneDuration')?.value,
        genTypeActive: document.querySelector('#genTypeTabs button.active')?.textContent?.trim(),
        stateTab: window.state?.tab,
        statePage: window.state?.page,
        stitchOverride: window.state?.stitchMediaOverride,
        doneFilters: JSON.stringify(window.state?.doneFilters),
        hash: location.hash,
        cardCount: document.querySelectorAll('#jobsGrid [data-jid]').length,
    })""")
    print("STATE0", json.dumps(state0, ensure_ascii=False))

    # 点左侧「长片拼接」
    clicked = page.evaluate("""() => {
        const items = [...document.querySelectorAll('.nav-item, .nav-model-item')];
        const hit = items.find(x => (x.textContent||'').includes('长片拼接'));
        if (hit) { hit.click(); return hit.textContent.trim(); }
        return null;
    }""")
    print("CLICKED", clicked)
    time.sleep(3)

    state1 = page.evaluate("""() => ({
        doneMedia: document.querySelector('#doneMedia')?.value,
        stateTab: window.state?.tab,
        statePage: window.state?.page,
        stitchOverride: window.state?.stitchMediaOverride,
        doneFilters: JSON.stringify(window.state?.doneFilters),
        hash: location.hash,
        cardCount: document.querySelectorAll('#jobsGrid [data-jid]').length,
        visibleKinds: [...document.querySelectorAll('#jobsGrid [data-jid]')].slice(0,20).map(c => c.dataset.kind || c.querySelector('.job-preview video')?'video':'img'),
    })""")
    print("STATE1_STITCH", json.dumps(state1, ensure_ascii=False))
    snap(page, "01_stitch_panel")

    print("REQS", json.dumps(reqs[-8:], ensure_ascii=False, indent=1))
    print("CONSOLE", json.dumps(console_msgs, ensure_ascii=False, indent=1))
    print("PAGEERRORS", json.dumps(page_errors, ensure_ascii=False, indent=1))
    browser.close()
