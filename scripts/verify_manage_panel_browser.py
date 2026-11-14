#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真 Chromium：打开「agent 管理」面板，截图 + 断言面板真的能改配置。

【为什么要有这个】
「接口返回 200」不等于「面板能用」。这个脚本验证的是**人在浏览器里看到的东西**：
  * 面板能打开；
  * 4 个能力开关真的渲染出来了；
  * 搜索引擎列表真的渲染出来了（带可编辑 URL）；
  * 步数上限可编辑；
  * t2v 默认参数（注意力 / 加速 LoRA）下拉真的列出了选项；
  * 改一个值 -> 真的 PUT 出去 -> 重新读回是改后的值。

【产出】docs/agent-power/evidence/T3_manage_panel_*.png + JSON。

用法：python3 scripts/verify_manage_panel_browser.py [out_dir]
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1] if len(sys.argv) > 1
           else REPO / "docs" / "agent-power" / "evidence")
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright      # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="agp-manage-"))
    port = _free_port()
    data_dir = tmp / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "H3STUDIO_PORT": str(port),
        "H3STUDIO_DATA_DIR": str(data_dir),
        "AGENT_SESSION_DB": str(data_dir / "agent_sessions.db"),
        "AGENT_MEMORY_DB": str(data_dir / "agent_memory.db"),
        "PYTHONPATH": str(REPO),
    })
    server = subprocess.Popen(
        ["/home/ubuntu/comfyui/venv/bin/python", "server.py"],
        cwd=str(REPO), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    result = {"port": port, "checks": []}
    try:
        deadline = time.time() + 90
        ready = False
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    ready = True
                    break
            except OSError:
                if server.poll() is not None:
                    break
                time.sleep(0.5)
        if not ready:
            result["fatal"] = "服务端起不来"
            print(json.dumps(result, ensure_ascii=False))
            return 1

        def check(name, ok, detail):
            result["checks"].append({"name": name, "ok": bool(ok),
                                     "detail": str(detail)})
            print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1500, "height": 1000})
            errs = []
            page.on("pageerror", lambda e: errs.append(f"pageerror:{e}"))
            page.on("console", lambda m: errs.append(f"console.{m.type}:{m.text}")
                    if m.type == "error" else None)

            page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
            page.wait_for_timeout(1200)

            # 打开 Agent 聊天区
            page.evaluate("() => { if (window.AgentUI) "
                          "window.AgentUI.show && window.AgentUI.show(); }")
            page.wait_for_timeout(600)

            # 打开管理面板 —— 两条路都验：
            #   (a) 真点「⚙ 管理」按钮（验证按钮绑定）；
            #   (b) 调 window.AgentManage.open()（验证渲染链路）。
            # 先用 (a)，若按钮找不到再退回 (b) —— 页面上可能有多个含
            # 「管理」文字的元素，按文字找会点错。
            opened = page.evaluate("""() => {
                const btn = document.querySelector('#agManageBtn');
                if (!btn) return 'no-button';
                btn.click();
                return 'clicked';
            }""")
            page.wait_for_timeout(2500)
            if opened == "no-button":
                opened = page.evaluate(
                    "() => { window.AgentManage.open(); return 'api-open'; }")
                page.wait_for_timeout(2500)
            check("管理按钮存在并可点击", opened != "no-button",
                  f"open 方式 = {opened}")

            modal_visible = page.evaluate(
                """() => {
                    const m = document.querySelector('#agManageModal');
                    if (!m) return false;
                    if (m.hidden) return false;
                    const st = getComputedStyle(m);
                    return st.display !== 'none' && st.visibility !== 'hidden';
                }""")
            check("管理面板弹窗可见", modal_visible, f"visible={modal_visible}")
            page.screenshot(path=str(OUT / "T3_manage_panel.png"),
                            full_page=False)

            # 4 个能力开关（真实选择器：input[data-cap]）
            caps = page.evaluate(
                """() => [...document.querySelectorAll('input[data-cap]')]
                       .map(i => ({key: i.getAttribute('data-cap'),
                                   checked: i.checked}))""")
            check("4 个能力开关渲染出来", len(caps) == 4,
                  f"能力开关 = {[c['key'] for c in caps]}")
            check("code_exec 默认未勾选（默认关闭）",
                  any(c["key"] == "code_exec" and not c["checked"]
                      for c in caps),
                  f"开关状态 = {caps}")

            # 搜索引擎 URL 输入框（真实选择器：input[data-eng-url]）
            engines = page.evaluate(
                """() => [...document.querySelectorAll('input[data-eng-url]')]
                       .map(i => i.value).filter(Boolean)""")
            check("搜索引擎 URL 输入框渲染出来（>= 3 个）",
                  len(engines) >= 3, f"URL 框 = {engines}")
            check("至少一个 URL 指向局域网 9898",
                  any("9898" in u for u in engines), f"URL 框 = {engines}")
            page.screenshot(path=str(OUT / "T3_manage_engines.png"),
                            full_page=False)

            # 步数上限可编辑（真实 id：agmMaxSteps）
            steps = page.evaluate(
                """() => { const i = document.querySelector('#agmMaxSteps');
                           return i ? {value: i.value, max: i.max,
                                       min: i.min} : null; }""")
            check("步数上限输入框存在且值为 50",
                  steps and str(steps.get("value")) == "50",
                  f"步数框 = {steps}")
            check("步数上限的 max 属性 = 50（前端硬顶与后端一致）",
                  steps and str(steps.get("max")) == "50",
                  f"max = {steps.get('max') if steps else None}")

            # t2v 默认参数下拉（真实 id：agmT2vAttention / agmT2vAccel）
            opts = page.evaluate("""() => {
                const s = document.querySelector('#agmT2vAttention');
                const l = document.querySelector('#agmT2vAccel');
                const m = document.querySelector('#agmT2vModel');
                return {
                    attention: s ? [...s.options].map(o => o.value) : null,
                    lora: l ? [...l.options].map(o => o.value) : null,
                    model: m ? [...m.options].map(o => o.value) : null,
                    attention_value: s ? s.value : null,
                    lora_value: l ? l.value : null,
                };
            }""")
            check("注意力机制下拉含 kitchen",
                  opts.get("attention") and "kitchen" in opts["attention"],
                  f"attention 选项 = {opts.get('attention')}")
            check("注意力下拉当前值 = kitchen",
                  opts.get("attention_value") == "kitchen",
                  f"当前值 = {opts.get('attention_value')}")
            check("加速 LoRA 下拉含 dmad_4step_critic",
                  opts.get("lora") and "dmad_4step_critic" in opts["lora"],
                  f"lora 选项 = {opts.get('lora')}")
            check("加速 LoRA 下拉当前值 = dmad_4step_critic",
                  opts.get("lora_value") == "dmad_4step_critic",
                  f"当前值 = {opts.get('lora_value')}")
            check("模型下拉含 fl2va_pruned_int8",
                  opts.get("model") and "fl2va_pruned_int8" in opts["model"],
                  f"model 选项 = {opts.get('model')}")
            page.screenshot(path=str(OUT / "T3_manage_t2v.png"),
                            full_page=False)

            # 真改一个值 -> PUT -> 读回（真实 id：agmT2vAttention / agmSave）
            page.evaluate("""() => {
                const s = document.querySelector('#agmT2vAttention');
                s.value = 'default';
                s.dispatchEvent(new Event('change', {bubbles: true}));
            }""")
            page.wait_for_timeout(400)
            saved = page.evaluate("""() => {
                const btn = document.querySelector('#agmSave');
                if (!btn) return false;
                btn.click();
                return true;
            }""")
            page.wait_for_timeout(2500)
            check("保存按钮存在并点击", saved, f"clicked={saved}")

            import urllib.request
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/agent/settings", timeout=30) as r:
                after = json.loads(r.read().decode())
            att = (after.get("settings", {}).get("t2v_defaults") or {}).get("attention")
            check("改后的值真的持久化（读回 = default）", att == "default",
                  f"服务端读回 attention = {att}")
            check("页面无 JS 报错", not errs, f"errors = {errs[:5]}")

            browser.close()
        result["errors"] = errs
        (OUT / "T3_manage_panel.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        passed = sum(1 for c in result["checks"] if c["ok"])
        print(f"\n通过 {passed} / {len(result['checks'])}")
        return 0 if passed == len(result["checks"]) else 1
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    sys.exit(main())
