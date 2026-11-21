# -*- coding: utf-8 -*-
"""对 3 条新守卫做变异测试 —— 证明它们不是空断言。

【为什么要有这个】守卫如果永远绿，它锁不住任何东西。
每条变异都要**故意破坏**那条守卫所断言的那个行为，
然后确认该守卫**变红**；再恢复，确认**重新变绿**。

【变异选取原则】必须是「命名该行为的那条断言」抓住的，
而不是被无关的副作用抓住。为此每条变异都配一个「阴性对照」：
  · 变异体 —— 破坏目标行为 ⇒ 目标守卫必须红
  · 恢复体 —— 恢复原状 ⇒ 目标守卫必须绿
如果变异后守卫仍绿，说明这条断言抓不到该行为（守卫无效）。

【纪律】
  · 只改**产品代码**（web/），不改守卫本身 —— 改守卫等于没测。
  · 每轮改前先 git stash/记录，改后用 git checkout -- 恢复。
    【坑】`git checkout --` 会丢未提交改动 ⇒ 改前必须先 commit 或 stash。
  · 只跑被测的那一条守卫，不跑全量（快）。
"""
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path("/root/projects/h3s-favorites")
PY = "/home/ubuntu/comfyui/venv/bin/python"

# 守卫名 -> (pytest nodeid, 说明)
GUARDS = {
    "G1_favorites_not_dead_end": (
        "tests/test_mobile_nav_regression_guards.py::test_favorites_view_is_not_a_dead_end",
        "收藏栏不是死路：选类型→进收藏→退回类型选择→再选另一个类型→媒体库仍可点",
    ),
    "G2_768_favorites_entry_reachable": (
        "tests/test_mobile_nav_regression_guards.py::test_favorites_entry_reachable_at_tablet_768",
        "768 平板档收藏入口在某个滚动位置可达 + 能进入收藏视图",
    ),
    "G3_fav_card_opens_detailModal": (
        "tests/test_mobile_nav_regression_guards.py::test_favorites_view_card_opens_detailModal",
        "收藏视图点卡片开的是既有 #detailModal 且带真实参数",
    ),
}

# 变异清单：
#   (变异名, 守卫名, 文件, 原文, 改后, 说明)
MUTATIONS = [
    # ---- G1：破坏「能退回类型选择」------------------------------------
    ("M1_收藏入口不再切视图", "G1_favorites_not_dead_end",
     "web/favorites.js",
     '''        if (window.H3Studio && typeof window.H3Studio.showFavorites === "function") {
          window.H3Studio.showFavorites();
          return;
        }''',
     '''        if (false) {
          return;
        }''',
     "让左侧「收藏」入口不再调用 showFavorites（落进兜底分支的旧路径）"),

    ("M2_汉堡不再打开侧栏", "G1_favorites_not_dead_end",
     "web/app.js",
     "  sidebar.classList.toggle(\"open\", open);",
     "  // MUTATION: 不再打开侧栏",
     "汉堡点不动 ⇒ 从收藏视图回不到类型选择"),

    # ---- G2：破坏 768 下的入口可达 ----------------------------------
    ("M3_768下收藏入口被移出滚动区", "G2_768_favorites_entry_reachable",
     "web/index.html",
     '    <a class="nav-item" id="navFavorites" title="查看已收藏的生成产物">',
     '    <a class="nav-item" id="navFavorites" style="display:none" title="查看已收藏的生成产物">',
     "收藏入口 display:none ⇒ 768 下点不到"),

    # ---- G3：破坏「同一份详情实现」 ----------------------------------
    ("M4_收藏视图卡片不开详情", "G3_fav_card_opens_detailModal",
     "web/app.js",
     """    openDetailModal(job);
    // 【2026-11-14 需求 3】阻止事件冒泡到 document ——""",
     """    // MUTATION: 收藏视图里不开详情
    if (state.tab !== "favorites") openDetailModal(job);
    // 【2026-11-14 需求 3】阻止事件冒泡到 document ——""",
     "收藏视图点卡片不开详情模态框"),
]


def run_guard(nodeid, timeout=260):
    """跑一条守卫。返回 (passed_bool, 输出尾部)。"""
    t0 = time.time()
    try:
        p = subprocess.run(
            [PY, "-m", "pytest", nodeid, "-q", "--tb=line", "-p", "no:randomly"],
            cwd=str(REPO), capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
        return ("passed" in out and "failed" not in out), out, round(time.time() - t0, 1)
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT", round(time.time() - t0, 1)


def apply_mutation(path, old, new):
    f = REPO / path
    src = f.read_text(encoding="utf-8")
    if old not in src:
        return False, "原文没找到（变异体已失效或代码已变）"
    f.write_text(src.replace(old, new, 1), encoding="utf-8")
    return True, ""


def revert():
    subprocess.run(["git", "checkout", "--", "web/"], cwd=str(REPO),
                   capture_output=True, text=True)


def main():
    results = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "cases": []}

    print("=== 基线：3 条守卫在**未变异**代码上必须全绿 ===")
    base_ok = True
    for gname, (nodeid, desc) in GUARDS.items():
        ok, out, dt = run_guard(nodeid)
        print(f"  {'PASS' if ok else 'FAIL'}  {gname}  ({dt}s)")
        results["cases"].append({"guard": gname, "phase": "baseline",
                                 "passed": ok, "seconds": dt})
        base_ok = base_ok and ok
    if not base_ok:
        print("!! 基线就不绿 —— 变异测试没有意义，先修守卫")
        json.dump(results, open(REPO / "evidence/mobile-nav/mutation-3guards.json", "w"),
                  ensure_ascii=False, indent=1)
        return 1

    print("\n=== 变异：破坏目标行为 ⇒ 目标守卫必须变红 ===")
    for mut_name, gname, path, old, new, why in MUTATIONS:
        nodeid = GUARDS[gname][0]
        applied, err = apply_mutation(path, old, new)
        if not applied:
            print(f"  SKIP  {mut_name}: {err}")
            results["cases"].append({"guard": gname, "mutation": mut_name,
                                     "applied": False, "reason": err})
            revert()
            continue
        ok, out, dt = run_guard(nodeid)
        # 红色 = 守卫抓住了变异
        caught = not ok
        print(f"  {'BITES' if caught else 'MISS '} {mut_name}  -> {gname}  ({dt}s)")
        print(f"        {why}")
        if not caught:
            for ln in out.splitlines()[-12:]:
                print("        | " + ln[:150])
        results["cases"].append({
            "guard": gname, "mutation": mut_name, "file": path,
            "why": why, "applied": True, "bites": caught, "seconds": dt,
            "tail": out[-1200:],
        })
        # 恢复后必须重新变绿（阴性对照）
        revert()
        ok2, out2, dt2 = run_guard(nodeid)
        print(f"        恢复后: {'PASS' if ok2 else 'FAIL'}  ({dt2}s)")
        results["cases"].append({
            "guard": gname, "mutation": mut_name, "phase": "revert",
            "passed": ok2, "seconds": dt2,
        })

    results["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    outf = REPO / "evidence/mobile-nav/mutation-3guards.json"
    outf.parent.mkdir(parents=True, exist_ok=True)
    json.dump(results, open(outf, "w"), ensure_ascii=False, indent=1)

    muts = [c for c in results["cases"] if c.get("applied")]
    bites = [c for c in muts if c.get("bites")]
    reverts = [c for c in results["cases"] if c.get("phase") == "revert"]
    rg = [c for c in reverts if c.get("passed")]
    print("\n=== 汇总 ===")
    print(f"  变异体: {len(bites)}/{len(muts)} 咬人")
    print(f"  恢复后重新变绿: {len(rg)}/{len(reverts)}")
    print(f"  证据: {outf}")
    return 0 if len(bites) == len(muts) and len(rg) == len(reverts) else 1


if __name__ == "__main__":
    sys.exit(main())
