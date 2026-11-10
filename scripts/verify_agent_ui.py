# -*- coding: utf-8 -*-
"""
内置 Agent 真实调用演示脚本。

用法：
    python3 scripts/demo_agent_chat.py [端口] [自定义意图]
    python3 scripts/demo_agent_multiturn.py [端口]
    python3 scripts/demo_agent_errors.py [端口]
    python3 scripts/demo_agent_sessions.py [端口]
    python3 scripts/verify_agent_ui.py [端口]

需要一个**已启动**的 Studio 实例。默认端口 8307（测试实例），
线上服务是 8190 —— 演示前请确认目标实例上已配置好本地模型 Provider。

脚本会**验证任务是否真的落进任务库**（不能只看响应体 ——
响应里的 job_id 可能是模型编的）。
"""


import sys
from playwright.sync_api import sync_playwright

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8307
errors = []

with sync_playwright() as p:
    browser = p.chromium.launch(args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
    page.on("console", lambda m: errors.append("console.error: " + m.text)
            if m.type == "error" else None)
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    print("== 1. 左侧导航有「内置 Agent」入口 ==")
    nav = page.query_selector("#navAgent")
    print("   navAgent 存在:", nav is not None)
    print("   文本:", (nav.inner_text() if nav else "").strip())

    print("\n== 2. 点它 -> 切到 Agent 页 ==")
    # 侧栏可滚动，navAgent 可能在视口外 -> 用 JS 触发 click
    page.evaluate("() => document.querySelector('#navAgent').click()")
    page.wait_for_timeout(1200)
    vis = page.evaluate("""() => {
      const el = document.querySelector('#agentPage');
      return !!el && getComputedStyle(el).display !== 'none';
    }""")
    print("   agentPage 可见:", vis)
    print("   创作区已隐藏:", not page.evaluate(
        """() => getComputedStyle(document.querySelector('#jobsArea')).display !== 'none'"""))
    print("   导航高亮:", page.evaluate(
        """() => (document.querySelector('.nav-item.active')||{}).innerText || ''""").strip())

    print("\n== 3. 空态提示可见 ==")
    empty = page.query_selector(".ag-empty")
    print("   空态存在:", empty is not None)
    if empty:
        print("   空态文本:", empty.inner_text().replace("\n", " | ")[:150])

    print("\n== 4. 输入框 + 发送按钮 ==")
    page.fill("#agInput", "测试一下面板能不能输入")
    print("   输入框可写:", page.input_value("#agInput"))
    btn_disabled = page.evaluate("() => document.querySelector('#agSend').disabled")
    print("   发送按钮初始 disabled:", btn_disabled)

    print("\n== 5. 切回创作页，Agent 页应隐藏 ==")
    page.evaluate("() => document.querySelector('.nav-item[data-mode=\"t2i\"]').click()")
    page.wait_for_timeout(800)
    print("   agentPage 已隐藏:", not page.evaluate(
        """() => getComputedStyle(document.querySelector('#agentPage')).display !== 'none'"""))
    print("   创作区已恢复:", page.evaluate(
        """() => getComputedStyle(document.querySelector('#jobsArea')).display !== 'none'"""))

    print("\n== 6. 窄屏（375px）不溢出 ==")
    page.set_viewport_size({"width": 375, "height": 720})
    page.wait_for_timeout(600)
    page.evaluate("() => document.querySelector('#navAgent').click()")
    page.wait_for_timeout(900)
    overflow = page.evaluate("""() => document.documentElement.scrollWidth - document.documentElement.clientWidth""")
    print("   横向溢出像素:", overflow, "(<=0 表示没溢出)")
    page.screenshot(path="/tmp/agent_panel_mobile.png", full_page=False)

    page.set_viewport_size({"width": 1440, "height": 900})
    page.wait_for_timeout(600)
    page.screenshot(path="/tmp/agent_panel_desktop.png", full_page=False)

    browser.close()

print("\n== 浏览器错误 ==")
if errors:
    for e in errors[:20]:
        print("  ", e)
else:
    print("   无 JS 错误")
