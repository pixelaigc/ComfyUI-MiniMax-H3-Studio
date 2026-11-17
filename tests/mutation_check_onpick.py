# -*- coding: utf-8 -*-
"""变异测试：证明 tests/test_asset_picker_onpick.py 的断言**真的在守**。

做法：把 web/app.js 的修复**改回坏的样子**（去掉 onPick 回调），
跑本项目的测试文件，必须变红；再把守卫那段也删掉，必须仍然变红。

两次变异对应两条不同的断言：
  变异 1（去掉 onPick 回调）
    -> test_slot_click_fills_slot_without_error 必须红
  变异 2（再把 typeof onPick 守卫也删掉）
    -> test_missing_onpick_is_rejected_before_opening_picker 必须红

用法: python3 tests/mutation_check_onpick.py
返回 0 = 全部变异都被抓住（好）；非 0 = 有变异没被抓住（测试是空的）。
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_JS = REPO / "web" / "app.js"
TEST_FILE = REPO / "tests" / "test_asset_picker_onpick.py"

GOOD_CALL = "openAssetPickerForSlot(kind, (asset) => fillFromAsset(asset));"
BAD_CALL = "openAssetPickerForSlot(kind);"
GUARD = '''    if (typeof onPick !== "function") {
      console.error("[素材库选择] 缺少 onPick 回调，无法打开选择器", {kind});
      alert("素材库选择器调用有误（缺少回调），请刷新页面重试。");
      return;
    }
'''


def run_tests():
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin:/home/ubuntu/comfyui/venv/bin",
           "H3STUDIO_DATA_DIR": tempfile.mkdtemp(prefix="onpick-mut-"),
           "HOME": "/root"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(TEST_FILE), "-q", "--tb=no", "-x"],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=1800)
    return proc.returncode, proc.stdout[-3000:]


def main():
    original = APP_JS.read_text(encoding="utf-8")
    backup = APP_JS.with_suffix(".js.mutationbak")
    shutil.copy2(APP_JS, backup)
    failures = []
    try:
        # ---------------------------------------------------------- 变异 1
        mut1 = original.replace(GOOD_CALL, BAD_CALL)
        if mut1 == original:
            print("!! 变异 1 手法失效：找不到 GOOD_CALL 形状")
            return 2
        APP_JS.write_text(mut1, encoding="utf-8")
        rc, out = run_tests()
        ok = rc != 0
        print(f"[变异 1 去掉 onPick 回调] pytest rc={rc} -> "
              f"{'✅ 变红（断言守住了）' if ok else '❌ 仍全绿（断言是空的）'}")
        if not ok:
            failures.append("变异 1 没被抓住")
            print(out)

        # ---------------------------------------------------------- 变异 2
        mut2 = mut1.replace(GUARD, "")
        if mut2 == mut1:
            print("!! 变异 2 手法失效：找不到 GUARD 形状")
            return 2
        APP_JS.write_text(mut2, encoding="utf-8")
        rc, out = run_tests()
        ok = rc != 0
        print(f"[变异 2 再删掉 onPick 守卫] pytest rc={rc} -> "
              f"{'✅ 变红（断言守住了）' if ok else '❌ 仍全绿（断言是空的）'}")
        if not ok:
            failures.append("变异 2 没被抓住")
            print(out)
    finally:
        shutil.move(str(backup), str(APP_JS))
        if backup.exists():
            backup.unlink()

    # 还原后必须恢复全绿
    rc, out = run_tests()
    print(f"[还原后] pytest rc={rc} -> "
          f"{'✅ 全绿' if rc == 0 else '❌ 还原后仍红'}")
    if rc != 0:
        failures.append("还原后仍红")
        print(out)

    if failures:
        print("\n结论: ❌ " + "；".join(failures))
        return 1
    print("\n结论: ✅ 两条变异都被抓住，断言真的在守")
    return 0


if __name__ == "__main__":
    sys.exit(main())
