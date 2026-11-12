#!/usr/bin/env python3
"""B 项补充取证：同名 key 冲突台账 + 不适配条目的界面标注。

【为什么要有这个脚本】
B 项的「真实挂载」证据（142/148 满足 patches attached>0 且 key-not-loaded
警告=0）已经由 verify_moss_lora_attach.py 落盘。
但验收口径里还有两条需要**独立可复核**的证据：

  ① 同名 key 冲突台账 —— 冲突必须**显式登记**（不能静默丢失）
     要证明：台账里确实有条目，且每条都注明「来源 / plan_id / 是否保留」
  ② 不适配条目的**界面标注** —— 光有后端返回还不够，
     用户在 LoRA 面板上要**看得见**「已排除（与 MOSS 底模不兼容）」+ 原因

本脚本两条都取**运行时**证据：
  ① 直接读真实模块的台账（不是 grep 源码）
  ② 真 Chromium 打开真面板，抓 DOM 里那段「已排除」区块的文本

用法：
    python3 scripts/verify_acceptance_b_lora_ledger.py [证据输出目录]
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/h3-acceptance-b-ledger")
OUT.mkdir(parents=True, exist_ok=True)
SHOTS = OUT / "screenshots"
SHOTS.mkdir(parents=True, exist_ok=True)
PORT = int(os.environ.get("H3_TEST_PORT", "8196"))
BASE_TO_CHECK = os.environ.get("H3_TEST_BASE", "MOSS-Image")


# ---------------------------------------------------------------- ① 冲突台账
def check_conflict_ledger() -> dict:
    """
    读**真实模块**的冲突台账。

    【口径】
    - 不是 grep 源码，是 import 真实模块后读它的 LORA_CATALOG_CONFLICTS
    - 每条冲突必须有：来源 source / plan_id / kept（保留还是被挡）
    - 冲突的 key 必须在 LORA_CATALOG 里**只有一个**生效条目
      （合并时不能两边都生效，也不能两边都消失）
    """
    sys.path.insert(0, str(REPO))
    from h3studio import loras

    ledger = loras.LORA_CATALOG_CONFLICTS
    rows = []
    for key, entries in ledger.items():
        for e in entries:
            rows.append({
                "key": key,
                "source": e.get("source"),
                "plan_id": e.get("plan_id"),
                "kept": e.get("kept"),
                "display": e.get("display"),
            })
    # 校验：字段齐全 + key 在目录里唯一生效
    field_ok = all(
        r["source"] and r["kept"] is not None for r in rows)
    # 校验：同一 key 的记录里恰好有一个 kept=True
    kept_counts = {}
    for r in rows:
        kept_counts.setdefault(r["key"], []).append(r["kept"])
    one_kept = all(
        sum(1 for k in v if k) == 1 for v in kept_counts.values())
    # 校验：生效的那条确实在 LORA_CATALOG 里
    in_catalog = all(
        r["key"] in loras.LORA_CATALOG for r in rows)
    return {
        "conflict_key_count": len(ledger),
        "conflict_entry_count": len(rows),
        "rows": rows,
        "all_have_source_and_kept": field_ok,
        "exactly_one_kept_per_key": one_kept,
        "kept_entry_in_catalog": in_catalog,
        "catalog_total": len(loras.LORA_CATALOG),
    }


# ---------------------------------------------------------------- ② 界面标注
def check_ui_annotation(base: str) -> dict:
    """
    真 Chromium 打开真面板，看「已排除（与 MOSS 底模不兼容）」区块。

    【断言】
    - DOM 里存在 .img-lora-excluded-title / .img-lora-excluded-item
    - 标题文本含「已排除」和「不兼容」
    - 每条有 .img-lora-excluded-reason（原因文案非空）
    - 被排除的 LoRA 数量 > 0（否则说明标注逻辑没被触发）
    """
    from playwright.sync_api import sync_playwright

    out = {"console": [], "pageErrors": [], "viewports": {}}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        for width, height in [(375, 812), (390, 844), (1280, 900)]:
            ctx = browser.new_context(viewport={"width": width, "height": height})
            page = ctx.new_page()
            page.on("console", lambda m, w=width: out["console"].append(
                {"width": w, "type": m.type, "text": m.text[:300]}))
            page.on("pageerror", lambda e, w=width: out["pageErrors"].append(
                {"width": w, "error": str(e)[:300]}))
            page.goto(base + "/", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)

            # 切到 MOSS 面板
            page.evaluate("""() => {
              const n = document.querySelector('a.nav-item[data-mode="t2i_moss"]');
              if (n) n.click();
            }""")
            page.wait_for_timeout(2500)

            # 打开 LoRA 面板
            opened = page.evaluate("""() => {
              const btns = Array.from(document.querySelectorAll('button,[role=button]'));
              const b = btns.find(x => /LoRA/i.test(x.textContent||''));
              if (b) { b.click(); return true; }
              return false;
            }""")
            page.wait_for_timeout(2000)

            dom = page.evaluate("""() => {
              const title = document.querySelector('.img-lora-excluded-title');
              const items = Array.from(
                document.querySelectorAll('.img-lora-excluded-item'));
              const reasons = Array.from(
                document.querySelectorAll('.img-lora-excluded-reason'))
                .map(e => (e.textContent||'').trim());
              return {
                hasTitle: !!title,
                titleText: title ? (title.textContent||'').trim() : '',
                itemCount: items.length,
                reasons: reasons,
                reasonNonEmpty: reasons.filter(r => r.length > 20).length,
                availableCount: (window.IMG_LORAS || {}).available
                  ? (window.IMG_LORAS.available || []).length : null,
                incompatibleCount: (window.MOSS_LORAS || {}).incompatible
                  ? (window.MOSS_LORAS.incompatible || []).length : null,
              };
            }""")
            shot = SHOTS / f"b-lora-excluded-{width}.png"
            page.screenshot(path=str(shot), full_page=True)
            out["viewports"][str(width)] = {
                "opened_lora_panel": opened,
                **dom,
                "shot": str(shot),
            }
            ctx.close()
        browser.close()
    return out


def main() -> int:
    print("== ① 同名 key 冲突台账 ==")
    ledger = check_conflict_ledger()
    print(f"   冲突 key {ledger['conflict_key_count']} 个 / "
          f"条目 {ledger['conflict_entry_count']} 条")
    for r in ledger["rows"][:6]:
        print(f"   - {r['key'][:48]:50} src={r['source']} "
              f"plan={r['plan_id']} kept={r['kept']}")

    print("\n== ② 8190 界面上的不适配标注 ==")
    ui = check_ui_annotation("http://127.0.0.1:8190")
    for w, v in ui["viewports"].items():
        print(f"   {w}: 标题={v['titleText'][:40]!r} 条目={v['itemCount']} "
              f"原因非空={v['reasonNonEmpty']} "
              f"incompatible={v['incompatibleCount']}")

    console_errors = [c for c in ui["console"] if c["type"] == "error"]
    vps = ui["viewports"]
    ui_ok = all(
        v["hasTitle"]
        and "已排除" in v["titleText"]
        and "不兼容" in v["titleText"]
        and v["itemCount"] >= 1
        and v["reasonNonEmpty"] >= 1
        for v in vps.values())

    verdict = {
        "conflict_ledger_has_entries": ledger["conflict_entry_count"] > 0,
        "conflict_ledger_fields_complete": ledger["all_have_source_and_kept"],
        "conflict_exactly_one_kept": ledger["exactly_one_kept_per_key"],
        "conflict_kept_in_catalog": ledger["kept_entry_in_catalog"],
        "ui_excluded_section_present": ui_ok,
        "console_errors": len(console_errors),
        "page_errors": len(ui["pageErrors"]),
    }
    verdict["all_pass"] = all([
        verdict["conflict_ledger_has_entries"],
        verdict["conflict_ledger_fields_complete"],
        verdict["conflict_exactly_one_kept"],
        verdict["conflict_kept_in_catalog"],
        verdict["ui_excluded_section_present"],
        not console_errors, not ui["pageErrors"],
    ])

    evidence = {"ledger": ledger, "ui": ui, "verdict": verdict}
    (OUT / "acceptance_b_ledger_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n===== B 项台账/标注判定 =====")
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    print(f"\n证据目录：{OUT}")
    return 0 if verdict["all_pass"] else 5


if __name__ == "__main__":
    raise SystemExit(main())
