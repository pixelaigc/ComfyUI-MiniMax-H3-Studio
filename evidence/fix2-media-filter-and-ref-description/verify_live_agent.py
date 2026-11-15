
import json, sys, time
from playwright.sync_api import sync_playwright
BASE="http://127.0.0.1:8190"; OUT=sys.argv[1]
console, pageerrors, reqs = [], [], []
with sync_playwright() as p:
    b=p.chromium.launch(args=["--no-sandbox"]); ctx=b.new_context(viewport={"width":1600,"height":950}); pg=ctx.new_page()
    pg.on("console", lambda m: console.append(f"[{m.type}] {m.text}"))
    pg.on("pageerror", lambda e: pageerrors.append(str(e)))
    pg.on("request", lambda r: reqs.append(r.url) if "/api/jobs" in r.url else None)
    pg.goto(BASE+"/", wait_until="networkidle"); time.sleep(3)
    # 点 Agent
    pg.evaluate("() => document.querySelector('#navAgent')?.click()")
    time.sleep(4)
    s = pg.evaluate("""() => ({
        hash: location.hash,
        agentVisible: !document.querySelector('#agentPage')?.classList.contains('page-hidden'),
        jobsVisible: !document.querySelector('#jobsArea')?.classList.contains('page-hidden'),
    })""")
    print("AGENT_STATE", json.dumps(s, ensure_ascii=False))
    pg.screenshot(path=f"{OUT}/07_agent_page.png")
    # 回到生成页，看后台轮询请求带什么
    reqs.clear()
    pg.evaluate("() => document.querySelector('.nav-item[data-mode=\"t2i\"]')?.click()")
    time.sleep(5)
    print("REQS_AFTER_AGENT", json.dumps(reqs[-4:], ensure_ascii=False))
    s2 = pg.evaluate("""() => ({
        hash: location.hash,
        cards: document.querySelectorAll('#jobsGrid [data-jid]').length,
        doneMedia: document.querySelector('#doneMedia')?.value,
    })""")
    print("BACK_STATE", json.dumps(s2, ensure_ascii=False))
    pg.screenshot(path=f"{OUT}/08_back_from_agent.png")
    print("CONSOLE", json.dumps(console, ensure_ascii=False))
    print("PAGEERRORS", json.dumps(pageerrors, ensure_ascii=False))
    b.close()
