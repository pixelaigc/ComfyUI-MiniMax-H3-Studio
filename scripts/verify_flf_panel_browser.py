# -*- coding: utf-8 -*-
"""内置 Agent 管理面板：flf / t2v 修正后取证（桌面宽屏 + 375px 窄屏）。

【为什么不用 shot_agent_defaults.py】
那个脚本只抓 ``input, select`` —— 修正后 flf 有三个**只读说明字段**
（采样档位 / sigma shift / 底模精度），它们渲染成 ``b.agm-fixed-value``
而不是表单控件，所以必须单独断言「存在只读说明、且没有对应控件」。

【采集内容】
  * flf / t2v 两个区块的**完整字段快照**（含只读字段 + 控件类型）
  * flf / t2v 的区块截图（桌面 1280 宽）
  * 375px 窄屏截图 + 横向溢出判定
  * console error 数（必须为 0）

用法：python3 verify_flf_panel_browser.py <base_url> <out_dir> <tag>
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/shot")
TAG = sys.argv[3] if len(sys.argv) > 3 else "run"
OUT.mkdir(parents=True, exist_ok=True)

DESKTOP = {"width": 1280, "height": 1500}
NARROW = {"width": 375, "height": 1400}

#: 采集一个生成类型区块里的全部字段（含只读说明）
COLLECT = """
() => {
  const out = {};
  document.querySelectorAll('#agManageBody details.agm-gen').forEach(b => {
    const tag = b.querySelector('.agm-gen-head .agm-tag:not(.req)');
    const key = (tag && tag.textContent || '').trim();
    if (!key) return;
    const fields = [];
    b.querySelectorAll('.agm-field').forEach(f => {
      const label = f.querySelector('span');
      if (!label) return;
      const name = label.textContent.trim();
      const control = f.querySelector('input, select');
      const fixed = f.querySelector('.agm-fixed-value');
      const note = f.querySelector('.agm-field-note');
      const rec = {label: name};
      if (fixed) {
        rec.kind = 'fixed';
        rec.value = fixed.textContent.trim();
      } else if (control) {
        rec.kind = control.tagName === 'SELECT' ? 'select' : control.type;
        rec.value = control.value;
        if (control.tagName === 'SELECT') {
          rec.options = Array.from(control.options).map(o => o.value);
          const sel = control.options[control.selectedIndex];
          rec.selectedLabel = sel ? sel.textContent.trim() : '';
        }
      } else {
        rec.kind = 'none';
      }
      rec.note = note ? note.textContent.trim() : '';
      fields.push(rec);
    });
    const noteEl = b.querySelector('.agm-hint');
    out[key] = {fields: fields,
                note: noteEl ? noteEl.textContent.trim() : ''};
  });
  return out;
}
"""


def main():
    result = {"base": BASE, "tag": TAG, "console_errors": [],
              "shots": [], "panels": {}, "overflow_375": None,
              "assertions": {}}
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport=DESKTOP)
        page.on("console", lambda m: result["console_errors"].append(
            f"{m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: result["console_errors"].append(
            f"pageerror: {e}"))

        # 【为什么不用 networkidle】
        # 线上有轮询 / SSE 长连接（媒体库、Agent 会话状态），
        # networkidle 永远等不到「网络静默」-> 30s 超时。
        # 改成 domcontentloaded + 显式等选择器（等的是**真的**那个元素）。
        page.goto(BASE, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_selector("#navAgent", timeout=20000)
        page.click("#navAgent")
        page.wait_for_timeout(600)
        page.wait_for_selector("#agManageBtn", state="visible", timeout=15000)
        page.click("#agManageBtn")
        page.wait_for_selector("#agManageBody details.agm-gen", timeout=15000)
        page.wait_for_timeout(800)

        for key in ("flf", "t2v"):
            opened = page.evaluate(
                """(key) => {
                    const blocks = document.querySelectorAll(
                        '#agManageBody details.agm-gen');
                    for (const b of blocks) {
                        const tag = b.querySelector(
                            '.agm-gen-head .agm-tag:not(.req)');
                        if (tag && tag.textContent.trim() === key) {
                            b.open = true;
                            b.scrollIntoView({block: 'center'});
                            return true;
                        }
                    }
                    return false;
                }""", key)
            page.wait_for_timeout(500)
            path = OUT / f"flfpanel_{key}_{TAG}.png"
            try:
                blocks = page.query_selector_all("#agManageBody details.agm-gen")
                target = None
                for b in blocks:
                    tag = b.query_selector(".agm-gen-head .agm-tag:not(.req)")
                    if tag and tag.textContent.strip() == key:
                        target = b
                if target is not None:
                    target.screenshot(path=str(path))
                else:
                    page.screenshot(path=str(path), full_page=True)
            except Exception as exc:
                page.screenshot(path=str(path), full_page=True)
                result.setdefault("shot_errors", []).append(str(exc))
            result["shots"].append({"mode": key, "opened": opened,
                                    "path": str(path)})

        result["panels"] = page.evaluate(COLLECT)

        # ---- 375px 窄屏 ----
        page.set_viewport_size(NARROW)
        page.wait_for_timeout(700)
        result["overflow_375"] = page.evaluate(
            """() => ({
                scrollWidth: document.documentElement.scrollWidth,
                clientWidth: document.documentElement.clientWidth,
                overflow: document.documentElement.scrollWidth >
                          document.documentElement.clientWidth + 1,
                bodyScrollWidth: document.body.scrollWidth,
            })""")
        path375 = OUT / f"flfpanel_375px_{TAG}.png"
        page.screenshot(path=str(path375), full_page=True)
        result["shots"].append({"mode": "375px", "path": str(path375)})

        browser.close()

    # ---- 断言（机器可验证）----
    panels = result["panels"]
    flf = panels.get("flf") or {}
    t2v = panels.get("t2v") or {}
    flf_names = [f["label"] for f in flf.get("fields", [])]
    flf_kinds = {f["label"]: f["kind"] for f in flf.get("fields", [])}

    # 1) flf 不得有「分辨率档位」
    a = {
        "flf_no_resolution_tier":
            not any("分辨率档位" in n for n in flf_names),
        # 2) 尺寸只有一套：比例 + MP 预算 + 百分比
        "flf_size_fields_present":
            any("画面比例" in n for n in flf_names)
            and any("MP 预算" in n for n in flf_names),
        # 3) 底模精度不得是可编辑控件
        "flf_precision_not_editable":
            not any(("底模精度" in n and flf_kinds.get(n) != "fixed")
                    for n in flf_names),
        # 4) 不可改项必须是 fixed
        "flf_sampling_shift_are_fixed":
            any("采样档位" in n and flf_kinds.get(n) == "fixed"
                for n in flf_names)
            and any("sigma shift" in n and flf_kinds.get(n) == "fixed"
                    for n in flf_names),
        # 5) 加速 LoRA 只列官方 turbo 系
        "flf_lora_options_official_only":
            True,
        "console_errors_zero": len(result["console_errors"]) == 0,
        "no_horizontal_overflow_375":
            not result["overflow_375"]["overflow"],
    }
    for f in flf.get("fields", []):
        if f["label"] == "加速 LoRA":
            opts = set(f.get("options") or [])
            a["flf_lora_options_official_only"] = opts <= {
                "none", "ckpt850", "v4step600"}
            a["flf_lora_options"] = sorted(opts)
    result["assertions"] = a
    result["flf_field_labels"] = flf_names
    result["t2v_field_labels"] = [f["label"] for f in t2v.get("fields", [])]

    (OUT / f"flfpanel_{TAG}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "console_errors": len(result["console_errors"]),
        "overflow_375": result["overflow_375"],
        "shots": [s["path"] for s in result["shots"]],
        "flf_fields": flf_names,
        "t2v_fields": result["t2v_field_labels"],
        "assertions": a,
        "all_pass": all(v is True for k, v in a.items()
                        if isinstance(v, bool)),
    }, ensure_ascii=False, indent=2))
    if result["console_errors"]:
        print("CONSOLE ERRORS:", result["console_errors"][:10])


if __name__ == "__main__":
    main()
