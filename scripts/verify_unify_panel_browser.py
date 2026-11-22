# -*- coding: utf-8 -*-
"""三个视频面板（t2v / flf / r2v）的**浏览器实测**取证。

【为什么要有这个脚本】
服务端的字段规格对不对是一回事，**浏览器里渲染成什么样**是另一回事。
用户 2026-11-22 报的每一条都是**眼睛看到的**：
  「不还是和文生视频不一样吗」—— 三个区块并排看，控件不一样；
  「该配置的不能配置」—— 采样步数 / 采样器 / 调度器 / sigma shift
  在 flf / r2v 上是**纯文本**（只读说明），不是控件；
  「LORA模型都不对」—— t2v 与 flf/r2v 的下拉内容不一样。

所以这里必须**真的开浏览器、真的打开三个区块、真的截图**，
并把每个字段的「渲染形态」（控件 / 只读说明）抓下来对拍。

【断言什么】
  1. 三个区块的**采样参数控件**同名同义，且都是**可编辑控件**
     （不是只读说明）；
  2. 三个区块的**加速 LoRA 下拉内容完全一致**；
  3. 三个区块都**没有**「分辨率档位 / 底模精度（可编辑）」这类冲突表达；
  4. 只读项只剩「底模精度（随底模，不可改）」与「生效像素（只读派生）」；
  5. console error 数 = 0。

用法：python3 scripts/verify_unify_panel_browser.py <base_url> <out_dir> <tag>
"""

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/shot_unify")
TAG = sys.argv[3] if len(sys.argv) > 3 else "run"
OUT.mkdir(parents=True, exist_ok=True)

DESKTOP = {"width": 1440, "height": 1600}
MODES = ("t2v", "flf", "r2v")

#: 采集一个生成类型区块里的全部字段（含只读说明）+ 控件形态
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
        rec.editable = !control.disabled && !control.readOnly;
        if (control.tagName === 'SELECT') {
          rec.options = Array.from(control.options).map(o => o.value);
          rec.optionLabels = Array.from(control.options).map(o => o.textContent.trim());
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


def _open_block(page, key):
    """展开指定 mode 的区块并滚到视口中央。"""
    return page.evaluate(
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

        page.goto(BASE, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_selector("#navAgent", timeout=20000)
        page.click("#navAgent")
        page.wait_for_timeout(600)
        page.wait_for_selector("#agManageBtn", state="visible", timeout=15000)
        page.click("#agManageBtn")
        page.wait_for_selector("#agManageBody details.agm-gen", timeout=15000)
        page.wait_for_timeout(800)

        # ---- 逐个 mode 截图 ----
        for key in MODES:
            opened = _open_block(page, key)
            page.wait_for_timeout(500)
            path = OUT / f"panel_{key}_{TAG}.png"
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

        # ---- 三个面板并排一张（证明「看起来一样」）----
        try:
            page.evaluate(
                """() => {
                    document.querySelectorAll(
                        '#agManageBody details.agm-gen').forEach(b => {
                            b.open = ['t2v','flf','r2v'].includes(
                                (b.querySelector(
                                    '.agm-gen-head .agm-tag:not(.req)'
                                ) || {}).textContent?.trim());
                        });
                    window.scrollTo(0, 0);
                }""")
            page.wait_for_timeout(600)
            path = OUT / f"panel_all3_{TAG}.png"
            page.screenshot(path=str(path), full_page=True)
            result["shots"].append({"mode": "all3", "path": str(path)})
        except Exception as exc:
            result.setdefault("shot_errors", []).append(str(exc))

        result["panels"] = page.evaluate(COLLECT)

        # ---- 375px 窄屏 ----
        page.set_viewport_size({"width": 375, "height": 1400})
        page.wait_for_timeout(700)
        result["overflow_375"] = page.evaluate(
            """() => ({
                scrollWidth: document.documentElement.scrollWidth,
                clientWidth: document.documentElement.clientWidth,
                overflow: document.documentElement.scrollWidth >
                          document.documentElement.clientWidth + 1,
            })""")
        path375 = OUT / f"panel_375px_{TAG}.png"
        page.screenshot(path=str(path375), full_page=True)
        result["shots"].append({"mode": "375px", "path": str(path375)})

        browser.close()

    # ---- 断言（机器可验证）----
    panels = result["panels"]

    def fields_of(mode):
        return (panels.get(mode) or {}).get("fields") or []

    def kinds_of(mode):
        return {f["label"]: f.get("kind") for f in fields_of(mode)}

    def options_of(mode, label):
        """按标签取下拉选项。t2v 的标签是「加速 LoRA 组合」，
        flf / r2v 是「加速 LoRA」—— 这里按**前缀**匹配，
        免得因为一个空格差异就判不出对错。"""
        for f in fields_of(mode):
            if f["label"] == label or f["label"].startswith(label):
                return f.get("options") or []
        return None

    a = {}
    # 1) 采样五项在三个面板上都是**可编辑控件**
    sampling_labels = ("采样步数", "采样器", "调度器",
                       "视频 sigma shift", "音频 sigma shift")
    for mode in MODES:
        kinds = kinds_of(mode)
        labels = [f["label"] for f in fields_of(mode)]
        a[f"{mode}_sampling_editable"] = all(
            lab in labels and kinds.get(lab) not in (None, "fixed", "none")
            for lab in sampling_labels)
    # 1b) 三个面板的**采样控件标签**同名同义
    def sampling_labels_of(mode):
        return sorted(
            f["label"] for f in fields_of(mode)
            if f["label"] in ("采样步数", "采样器", "调度器",
                              "视频 sigma shift", "音频 sigma shift"))
    result["sampling_labels"] = {m: sampling_labels_of(m) for m in MODES}
    a["sampling_labels_identical"] = (
        result["sampling_labels"]["t2v"]
        == result["sampling_labels"]["flf"]
        == result["sampling_labels"]["r2v"])

    # 2) 加速 LoRA 下拉三面板一致
    lora_lists = {mode: options_of(mode, "加速 LoRA") for mode in MODES}
    a["accel_lora_options_identical"] = (
        lora_lists["t2v"] is not None
        and lora_lists["t2v"] == lora_lists["flf"] == lora_lists["r2v"])
    result["accel_lora_options"] = lora_lists
    # 3) 无冲突的尺寸 / 精度表达
    for mode in MODES:
        labels = [f["label"] for f in fields_of(mode)]
        kinds = kinds_of(mode)
        a[f"{mode}_no_resolution_tier"] = not any(
            "分辨率档位" in n for n in labels)
        a[f"{mode}_no_editable_precision"] = not any(
            ("底模精度" in n and kinds.get(n) != "fixed") for n in labels)
    # 4) 只读项只剩「底模精度」与「生效像素」
    for mode in MODES:
        fixed_labels = [f["label"] for f in fields_of(mode)
                        if f.get("kind") == "fixed"]
        result[f"{mode}_readonly_labels"] = fixed_labels
        a[f"{mode}_readonly_is_precision_and_derived"] = all(
            ("底模精度" in n) or ("生效像素" in n) for n in fixed_labels)
    # 5) 尺寸三项在
    for mode in MODES:
        labels = [f["label"] for f in fields_of(mode)]
        a[f"{mode}_size_fields_present"] = (
            any("画面比例" in n for n in labels)
            and any("MP 预算（" in n for n in labels)
            and any("MP 预算百分比" in n for n in labels))
    # 6) 前端卫生
    a["console_errors_zero"] = len(result["console_errors"]) == 0
    a["no_horizontal_overflow_375"] = not result["overflow_375"]["overflow"]

    result["assertions"] = a
    result["all_passed"] = all(a.values())
    for mode in MODES:
        result[f"{mode}_field_labels"] = [f["label"] for f in fields_of(mode)]

    out_json = OUT / f"panel_{TAG}.json"
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(json.dumps({
        "all_passed": result["all_passed"],
        "failed": [k for k, v in a.items() if not v],
        "accel_lora_options": lora_lists.get("t2v"),
        "console_errors": result["console_errors"][:5],
        "out_json": str(out_json),
        "shots": [s["path"] for s in result["shots"]],
    }, ensure_ascii=False, indent=2))
    return 0 if result["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
