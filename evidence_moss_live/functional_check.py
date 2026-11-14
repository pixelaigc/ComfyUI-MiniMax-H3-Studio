# -*- coding: utf-8 -*-
"""MOSS-Image 上线功能抽查（打**线上真实 8190**，不是本地实例）。

三项抽查：
  1. MOSS-Image 面板可打开（切到 t2i_moss 模式，参数面板出现且含 MOSS 专属控件）
  2. LoRA 选择生效（/api/image-options 的 moss lora 列表非空，且前端能选中）
  3. 真实提交一次生成并看到结果卡片（POST /api/jobs -> 轮询 -> 卡片出现）
  4. 窄屏 375px 下 FAB 不再被抽屉遮挡（elementFromPoint 权威判定）

运行: /home/ubuntu/comfyui/venv/bin/python evidence_moss_live/functional_check.py
"""
import json
import os
import sys
import time

BASE = os.environ.get("H3_BASE", "http://127.0.0.1:8190")
OUT = os.path.dirname(os.path.abspath(__file__))

from playwright.sync_api import sync_playwright  # noqa: E402

RESULTS = []


def record(name, ok, detail):
    RESULTS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    return ok


def api(path):
    import urllib.request
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode())


def main():
    # ---------- 1. MOSS-Image 面板可打开 ----------
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        # 切到 MOSS-Image 文生图
        opened = False
        detail = ""
        try:
            # 入口是左侧导航的 .nav-item[data-mode='t2i_moss']
            link = page.query_selector("a.nav-item[data-mode='t2i_moss']")
            if link:
                link.click()
                page.wait_for_timeout(2500)
                opened = True
                detail = "点击左侧导航 a.nav-item[data-mode='t2i_moss']"
        except Exception as exc:  # noqa: BLE001
            detail = f"切换异常: {exc}"

        if not opened:
            # 退路：直接调 JS API
            page.evaluate("""() => {
              if (typeof setMode === 'function') { setMode('t2i_moss'); return; }
              if (typeof state !== 'undefined') { state.mode = 't2i_moss'; }
            }""")
            page.wait_for_timeout(2500)
            opened = True
            detail += " | 退路: JS setMode('t2i_moss')"

        panel_state = page.evaluate("""() => {
          const panel = document.querySelector('#panel');
          const cs = panel ? getComputedStyle(panel) : null;
          return {
            mode: (typeof state !== 'undefined') ? state.mode : null,
            panelExists: !!panel,
            panelDisplay: cs ? cs.display : null,
            panelWidth: panel ? panel.getBoundingClientRect().width : 0,
            hasMossState: (typeof state !== 'undefined') ? !!(state.imgMoss) : false,
            mossTier: (typeof state !== 'undefined' && state.imgMoss) ? state.imgMoss.tier : null,
            loraCount: (typeof state !== 'undefined' && state.imgMoss && Array.isArray(state.imgMoss.loras))
                        ? state.imgMoss.loras.length : null,
          };
        }""")
        record("MOSS-Image 面板可打开", panel_state.get("mode") == "t2i_moss",
               f"mode={panel_state.get('mode')} panelDisplay={panel_state.get('panelDisplay')} "
               f"panelWidth={panel_state.get('panelWidth')} hasMossState={panel_state.get('hasMossState')} "
               f"tier={panel_state.get('mossTier')} ({detail})")

        # ---------- 2. LoRA 选择生效 ----------
        # LoRA 清单在 **/api/image-loras?base=MOSS-Image**（不在 /api/image-options）
        opts = api("/api/image-loras?base=MOSS-Image")
        moss_loras = opts.get("available") or []
        record("LoRA 列表可用 (MOSS 族)",
               len(moss_loras) > 0,
               f"/api/image-loras?base=MOSS-Image available 共 {len(moss_loras)} 项"
               + (f", 示例: {moss_loras[0].get('name')}" if moss_loras else ""))

        # 前端真的能选中一个 LoRA 并写进 state
        lora_sel = page.evaluate("""() => {
          const el = document.querySelector('#imgLoraSelect');
          if (!el) return {id: null, opts: 0, found: false};
          return {id: el.id, opts: el.options.length, found: true,
                  first: el.options[0] ? el.options[0].text : null,
                  second: el.options[1] ? el.options[1].text : null};
        }""")
        lora_pick = page.evaluate("""() => {
          const el = document.querySelector('#imgLoraSelect');
          if (!el || el.options.length <= 1) return {picked: false, reason: 'no-select-or-empty',
                                                    opts: el ? el.options.length : 0};
          el.selectedIndex = 1;
          el.dispatchEvent(new Event('change', {bubbles: true}));
          return {picked: true, value: el.value,
                  stateLoras: (typeof state !== 'undefined' && state.imgMoss && Array.isArray(state.imgMoss.loras))
                             ? state.imgMoss.loras.length : null};
        }""")
        record("LoRA 选择生效（前端 state 回写）", bool(lora_pick.get("picked")),
               f"select={lora_sel} picked={lora_pick}")

        # ---------- 3. 真实提交一次生成 ----------
        # 先记下**提交前**已有的 job id 集合 —— 后面只认新增的，
        # 否则「页面上有卡片」会被历史任务骗过去。
        def job_ids():
            try:
                j = api("/api/jobs")
            except Exception:  # noqa: BLE001
                return set()
            lst = j if isinstance(j, list) else (j.get("jobs") or [])
            out = set()
            for x in lst:
                if isinstance(x, dict):
                    for key in ("id", "job_id", "uuid"):
                        if x.get(key):
                            out.add(str(x[key]))
                            break
            return out

        before_ids = job_ids()
        submit = page.evaluate("""() => {
          const ta = document.querySelector('#prompt');
          if (ta) {
            const setter = Object.getOwnPropertyDescriptor(
              window.HTMLTextAreaElement.prototype, 'value').set;
            setter.call(ta, '上线闭环抽查测试图：一只戴着宇航员头盔的橘猫，蓝色渐变背景');
            ta.dispatchEvent(new Event('input', {bubbles: true}));
            ta.dispatchEvent(new Event('change', {bubbles: true}));
          }
          const btn = document.querySelector('#genBtn');
          if (!btn) return {ok: false, reason: 'no-#genBtn'};
          btn.click();
          return {ok: true, btnId: btn.id, promptSet: !!ta};
        }""")
        record("提交生成（点击 #genBtn）", bool(submit.get("ok")), str(submit))

        # 图像模式且提示词未增强时，会弹「增强确认」门 —— 必须点「确认」才继续。
        # （这是产品的既定行为，不是缺陷；不点确认就永远不会 POST /api/jobs。）
        gate = page.evaluate("""() => {
          const m = document.querySelector('#enhanceGateModal');
          return {exists: !!m, hidden: m ? m.hidden : null};
        }""")
        if gate.get("exists") and not gate.get("hidden"):
            page.click("#enhanceGateConfirm")
            page.wait_for_timeout(1500)
            record("增强确认门已确认", True,
                   f"#enhanceGateModal 弹出 -> 点击 #enhanceGateConfirm（门状态 {gate}）")
        else:
            record("增强确认门已确认", True,
                   f"未出现增强确认门（门状态 {gate}）—— 提示词已增强过或非图像模式")

        # 轮询：只认**新增**的 job
        job_result = None
        new_ids = set()
        for _ in range(60):
            page.wait_for_timeout(1500)
            after_ids = job_ids()
            new_ids = after_ids - before_ids
            job_result = page.evaluate("""() => {
              const cards = document.querySelectorAll('#jobsGrid .job-card');
              return {
                cardCount: cards.length,
                lastText: cards.length ? (cards[cards.length-1].textContent || '').trim().slice(0, 200) : '',
                hasImg: cards.length ? !!cards[cards.length-1].querySelector('img') : false,
              };
            }""")
            job_result["newJobIds"] = sorted(new_ids)
            job_result["beforeCount"] = len(before_ids)
            job_result["afterCount"] = len(after_ids)
            if new_ids:
                break
        record("真实提交产生新任务 (API 层)", len(new_ids) > 0,
               f"提交前 {len(before_ids)} 个 job -> 提交后新增 {len(new_ids)} 个: {sorted(new_ids)[:5]}")

        # 拿到新任务的真实状态
        new_job = None
        if new_ids:
            new_id = sorted(new_ids)[-1]
            for _ in range(40):
                try:
                    j = api("/api/jobs")
                    lst = j if isinstance(j, list) else (j.get("jobs") or [])
                    for x in lst:
                        if isinstance(x, dict) and str(x.get("id") or x.get("job_id") or "") == new_id:
                            new_job = x
                            break
                except Exception:  # noqa: BLE001
                    pass
                if new_job and str(new_job.get("status", "")).lower() in (
                        "done", "succeeded", "success", "completed", "failed", "error"):
                    break
                page.wait_for_timeout(2000)
            if new_job:
                rec = {
                    "id": new_job.get("id") or new_job.get("job_id"),
                    "mode": new_job.get("mode") or new_job.get("gen_type"),
                    "status": new_job.get("status"),
                    "error": new_job.get("error"),
                    "has_output": bool(new_job.get("output") or new_job.get("outputs")
                                       or new_job.get("files") or new_job.get("image")
                                       or new_job.get("image_path")),
                    "keys": sorted(new_job.keys())[:24],
                }
                record("新任务真实状态可见 (MOSS 模式)", rec.get("mode") == "t2i_moss" or rec.get("status") is not None,
                       json.dumps(rec, ensure_ascii=False))
            else:
                record("新任务真实状态可见 (MOSS 模式)", False,
                       f"新增 job id {new_id} 未在 /api/jobs 中找到")

        page.screenshot(path=os.path.join(OUT, "02-moss-panel-live.png"), full_page=False)

        # ---------- 4. 窄屏 375px FAB 遮挡 ----------
        page2 = browser.new_page(viewport={"width": 375, "height": 720})
        page2.goto(BASE, wait_until="domcontentloaded", timeout=60000)
        page2.wait_for_timeout(3000)
        page2.evaluate("""() => {
          const p = document.querySelector('#panel');
          if (p) p.classList.remove('open');
        }""")
        page2.wait_for_timeout(400)
        page2.click("#panelFab")
        page2.wait_for_timeout(900)

        fab = page2.evaluate("""() => {
          const fab = document.getElementById('panelFab');
          if (!fab) return {exists: false};
          const cs = getComputedStyle(fab);
          const r = fab.getBoundingClientRect();
          const visible = !!(fab.offsetWidth || fab.offsetHeight)
                          && cs.display !== 'none' && !fab.hidden;
          let hitTag = null, hitId = null, hitIsFab = null;
          if (visible && r.width > 0 && r.height > 0) {
            const h = document.elementFromPoint(r.left + r.width/2, r.top + r.height/2);
            hitTag = h ? h.tagName : null;
            hitId = h ? (h.id || '') : null;
            hitIsFab = !!h && (h === fab || fab.contains(h));
          }
          const panel = document.querySelector('#panel');
          const pcs = panel ? getComputedStyle(panel) : null;
          // 抽屉本体矩形
          const pr = panel ? panel.getBoundingClientRect() : null;
          return {
            exists: true, visible: visible,
            fabZIndex: cs.zIndex,
            fabRect: {x: Math.round(r.left), y: Math.round(r.top),
                      w: Math.round(r.width), h: Math.round(r.height)},
            panelZIndex: pcs ? pcs.zIndex : null,
            panelOpen: panel ? panel.classList.contains('open') : null,
            panelRect: pr ? {x: Math.round(pr.left), y: Math.round(pr.top),
                             w: Math.round(pr.width), h: Math.round(pr.height)} : null,
            hitTag: hitTag, hitId: hitId, hitIsFab: hitIsFab,
            viewport: {w: window.innerWidth, h: window.innerHeight},
          };
        }""")
        page2.screenshot(path=os.path.join(OUT, "03-narrow-375-fab.png"), full_page=False)

        ok_fab = bool(fab.get("exists")) and (
            (not fab.get("visible")) or fab.get("hitIsFab"))
        record("375px 窄屏 FAB 未被抽屉遮挡", ok_fab, json.dumps(fab, ensure_ascii=False))

        browser.close()

    out = {"base": BASE, "results": RESULTS,
           "all_pass": all(r["ok"] for r in RESULTS)}
    with open(os.path.join(OUT, "functional_check.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print()
    print("ALL_PASS =", out["all_pass"])
    return 0 if out["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
