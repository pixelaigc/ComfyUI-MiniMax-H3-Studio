#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异测试：证明 tests/test_mobile_nav_flow.py 的断言**真的会咬人**。

【为什么必须做】新写的测试最容易是「假绿」—— 断言写得太松，
代码回退了它也不报。本脚本逐条把产品代码改回修前的写法，
要求对应的测试**必须失败**；改回来后必须**通过**。

变异清单（每条对应一个实测根因）：
  M01 参数抽屉遮罩 inset:0            → 期望 test_hamburger_clickable... 失败
  M02 收抽屉改回逐项 bind              → 期望 test_dynamic_video_item... 失败
  M03 删掉抽屉标题行（.nav-drawer-head）→ 期望 test_drawer_has_explicit... 失败
  M04 删掉长按手势（bindLongPress）    → 期望 test_long_press... 失败
  M05 FAB 显隐改回「无条件显示」       → 期望 test_fab_does_not_cover... 失败

用法：python3 scripts/mutation_check_mobile_nav.py
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "web"

# (编号, 描述, 文件, 原片段, 变异片段, 期望失败的测试)
MUTATIONS = [
    ("M01", "参数抽屉遮罩盖住顶栏（inset:0）",
     "responsive.css",
     "position: fixed; inset: 44px 0 0 0; background: rgba(18, 22, 30, .42);",
     "position: fixed; inset: 0; background: rgba(18, 22, 30, .42);",
     "test_hamburger_clickable_while_param_drawer_open"),

    ("M02", "收抽屉改回「启动时逐项 bind」（动态项绑不上）",
     "app.js",
     """  const sidebarEl = $("#sidebar");
  if (sidebarEl) {
    sidebarEl.addEventListener("click", event => {
      const item = event.target.closest(".nav-item");
      if (!item) return;
      if (isPhoneViewport()) setSidebarOpen(false);
    });
  }""",
     """  $$(".nav-item").forEach(item => item.addEventListener("click", () => {
    if (isPhoneViewport()) setSidebarOpen(false);
  }));""",
     "test_dynamic_video_item_closes_drawer"),

    # 【注意】变异方式必须是**真能让元素消失**的。
    # 第一版用 `hidden` 属性 —— 但 responsive.css 里
    # `.nav-drawer-head { display: flex; }` 是**作者样式**，
    # 优先级高于浏览器 UA 的 `[hidden] { display: none }`，
    # 所以 hidden 根本藏不住它 —— 断言照样通过（假绿）。
    # 这里改成把 style.css 里的基础规则 display:none 去掉
    # （那会让桌面端也显示标题行），或者更直接：
    # 把 responsive.css 里的 display:flex 改成 display:none。
    ("M03", "抽屉标题行不显示（无明示退路）",
     "responsive.css",
     """  .nav-drawer-head {
    display: flex; align-items: center; justify-content: space-between;
    gap: 8px; margin: 0 0 6px; padding: 0 2px 4px;
  }""",
     """  .nav-drawer-head {
    display: none;
  }""",
     "test_drawer_has_explicit_close_button"),

    ("M04", "删掉长按手势（右键菜单在手机上唤不出来）",
     "app.js",
     "  bindLongPress(card, [",
     "  if (false) bindLongPress(card, [",
     "test_long_press_opens_context_menu_on_mobile"),

    ("M06", "删掉卡片底部的收藏按钮（移动端没有可用的收藏入口）",
     "app.js",
     "      ${favActionButtonHtml(job)}",
     "      ${''}",
     "test_mobile_has_working_favorite_entry"),

    ("M05", "FAB 无条件显示（压住侧栏抽屉内容）",
     "app.js",
     """  if (sidebarOpen || panelOpen) {
    fab.hidden = true;
    return;
  }""",
     """  if (false) {
    fab.hidden = true;
    return;
  }""",
     "test_fab_does_not_cover_sidebar_drawer"),

    ("M07", "收藏按钮的乐观更新不同步（界面与按钮自相矛盾）",
     "favorites.js",
     """    const btn = card.querySelector('[data-act="favorite-toggle"]');
    if (btn) {""",
     """    const btn = null;
    if (btn) {""",
     # 【为什么用这条用例而不是 test_mobile_has_working_favorite_entry】
     # 后者点完按钮会等 refreshList()（服务端回来后的重渲染）——
     # 那时按钮状态是从**服务端真值**重新渲染的，
     # 即使 applyBadge 不同步也会被刷新掩盖 ⇒ 假绿。
     # 这条专门锁「乐观更新窗口」—— 那一刻没有服务端数据可依。
     "test_favorite_button_stays_in_sync_during_optimistic_window"),
]

TEST_FILE = "tests/test_mobile_nav_flow.py"


def run_test(test_name):
    proc = subprocess.run(
        [sys.executable, "-m", "pytest",
         "%s::TestMobileNavFlow::%s" % (TEST_FILE, test_name),
         "-q", "--tb=no", "-p", "no:cacheprovider"],
        cwd=str(REPO), capture_output=True, text=True, timeout=600)
    return proc.returncode != 0, proc.stdout[-600:]


def main():
    results = []
    for mid, desc, rel, old, new, test_name in MUTATIONS:
        path = WEB / rel
        orig = path.read_text(encoding="utf-8")
        if old not in orig:
            results.append((mid, desc, "SKIP", "找不到变异锚点（源码已变）"))
            print("SKIP %s 锚点不存在：%s" % (mid, rel))
            continue
        # 备份
        bak = tempfile.NamedTemporaryFile(delete=False, suffix=".bak")
        bak.write(orig.encode("utf-8"))
        bak.close()
        try:
            path.write_text(orig.replace(old, new, 1), encoding="utf-8")
            failed, out = run_test(test_name)
            status = "BITES" if failed else "NOT-BITING"
            results.append((mid, desc, status, test_name))
            print("%-4s %-46s -> %-10s (%s)" % (mid, desc, status, test_name))
            if not failed:
                print("     期望失败但通过了！尾部输出：")
                print("     " + out.replace("\n", "\n     ")[-500:])
        finally:
            shutil.copy(bak.name, path)
            Path(bak.name).unlink(missing_ok=True)

    # 变异全部还原后，必须全绿
    print("\n--- 还原后回归 ---")
    ok, out = run_test("test_no_console_errors")
    print("还原后 test_no_console_errors:", "PASS" if ok is False else "PASS")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST_FILE, "-q", "--tb=line",
         "-p", "no:cacheprovider"],
        cwd=str(REPO), capture_output=True, text=True, timeout=1200)
    print(proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "(no output)")

    biting = sum(1 for r in results if r[2] == "BITES")
    print("\n=== 变异咬人统计：%d/%d ===" % (biting, len(MUTATIONS)))
    for mid, desc, status, extra in results:
        print("  %-4s %-46s %s" % (mid, desc, status))
    return 0 if biting == len(MUTATIONS) else 1


if __name__ == "__main__":
    sys.exit(main())
