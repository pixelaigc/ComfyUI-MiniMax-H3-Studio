#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异自证：A-6 / D-2 的新断言是否真的有牙齿。

【方法】对每个断言注入一个「能让它失败」的变异，跑被测断言，
必须看到 FAIL（断言抓到了变异）才算通过。

【纪律】
- 每轮先记录文件原始字节，改完立即用原始字节还原。
- 断言必须由「命名该行为的那条断言」抓住，不能靠别的断言顺带挂。
- 变异点要挑「常量巧合抓不到」的位置。

运行：
    python3 scripts/mutation_assets_hygiene.py
输出落 stdout，退出码非 0 = 有变异漏网。
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEST = "tests/test_frontend_assets_hygiene.py"
PY = sys.executable


def run_tests(selector: str):
    r = subprocess.run(
        [PY, "-m", "pytest", TEST, "-q", "--tb=no", "-x", "-k", selector],
        cwd=str(REPO), capture_output=True, text=True, timeout=300)
    return r.returncode, r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""


# (变异名, 目标文件, 原始片段, 变异片段, 期望失败的断言 selector)
MUTATIONS = [
    # --- A-6：往 web/*.js 注入 HTML 注释 ---
    ("A-6a 注入 HTML 注释到 assets.js",
     "web/assets.js",
     '    const inTrash = asset.deleted_at ? `<span class="badge" style="background:rgba(200,50,55,.85)">回收站</span>` : "";',
     '    const inTrash = asset.deleted_at ? `<span class="badge" style="background:rgba(200,50,55,.85)">回收站</span>` : "";\n    // <!-- [2026-11-15 A3 补漏] 多选勾选框 -->',
     "test_no_html_comment_in_any_js_template"),

    ("A-6b 注入 HTML 注释到 app.js",
     "web/app.js",
     'function esc(s) {',
     '// <!-- dev note -->\nfunction esc(s) {',
     "test_no_html_comment_in_any_js_template"),

    ("A-6c 注入 HTML 注释到 providers.js",
     "web/providers.js",
     'const',
     '// <!-- TODO: xxx -->\nconst',
     "test_no_html_comment_in_any_js_template"),

    # 上一版这个变异设计错了：只是把注释文字改了，注释**还在**，
    # 断言当然通过 —— 那是「等价变异」，不是有效变异。
    # 真正要模拟的是「维护者把这段注释直接删掉」。
    ("A-6d 注释「移到模板外」被整个删掉",
     "web/assets.js",
     """    // 多选勾选框：**只在多选模式下**才渲染。
    // 默认不显示 —— 用户点「多选」按钮进入多选模式后才出现。
    // 放在缩略图左下角，单独一个 .al-selbox-wrap，
    // 避免和卡片的「点开详情」冲突。
""",
     "",
     "test_assets_js_comment_moved_outside_template"),

    ("A-6e 右键菜单注释被整个删掉",
     "web/assets.js",
     "    // 添加到播放器清单：播放器只播用户主动挑的条目\n",
     "",
     "test_assets_js_comment_moved_outside_template"),

    # --- D-2：把 ?v= 改回旧值 ---
    ("D-2a assets.js ?v= 回退到 uxbatch1",
     "web/index.html",
     '/static/assets.js?v=cr20261115b',
     '/static/assets.js?v=uxbatch1',
     "test_bumped_static_assets_are_not_stale"),

    ("D-2b agent_sessions.js ?v= 回退到 agtsess2",
     "web/index.html",
     '/static/agent_sessions.js?v=cr20261115b',
     '/static/agent_sessions.js?v=agtsess2',
     "test_bumped_static_assets_are_not_stale"),

    ("D-2c video-panel-fallback.js ?v= 回退到 vpfb1",
     "web/index.html",
     '/static/video-panel-fallback.js?v=cr20261115b',
     '/static/video-panel-fallback.js?v=vpfb1',
     "test_bumped_static_assets_are_not_stale"),

    ("D-2d 顺手把不该动的 app.js 也升了（过度升版）",
     "web/index.html",
     '/static/app.js?v=ac1steps1',
     '/static/app.js?v=cr20261115b',
     "test_untouched_assets_keep_their_version"),

    ("D-2e 漏掉一个 ?v= 参数",
     "web/index.html",
     '/static/job-meta.js?v=cr20261115b',
     '/static/job-meta.js',
     "test_every_static_ref_has_version_param"),

    # --- 变异测试纪律自检：空转变异必须被抓 ---
    ("D-2f 静态引用指向不存在的文件",
     "web/index.html",
     '/static/job-meta.js?v=cr20261115b',
     '/static/job-meta-nonexistent.js?v=cr20261115b',
     "test_static_ref_points_to_existing_file"),
]


def main():
    results = []
    missed = []
    for name, rel, old, new, selector in MUTATIONS:
        path = REPO / rel
        original = path.read_bytes()
        raw = original.decode("utf-8")
        # 归一 CRLF —— index.html 是 CRLF 文件，needle 按 LF 写就匹配不上
        text = raw.replace("\r\n", "\n")
        old_n = old.replace("\r\n", "\n")
        new_n = new.replace("\r\n", "\n")
        if old_n not in text:
            results.append((name, "SKIP", "原始片段未找到"))
            missed.append(name)
            continue
        try:
            mutated = text.replace(old_n, new_n, 1)
            # 还原原文件行尾风格
            if b"\r\n" in original:
                mutated = mutated.replace("\n", "\r\n")
            path.write_bytes(mutated.encode("utf-8"))
            rc, last = run_tests(selector)
            if rc != 0:
                results.append((name, "CAUGHT", f"{selector} 失败"))
            else:
                results.append((name, "MISSED", f"{selector} 仍然通过"))
                missed.append(name)
        finally:
            path.write_bytes(original)   # 还原

    print("=" * 78)
    print("变异自证结果：A-6 / D-2 新断言")
    print("=" * 78)
    for name, status, detail in results:
        mark = {"CAUGHT": "✓", "MISSED": "✗", "SKIP": "?"}[status]
        print(f"  {mark} {status:7} {name}")
        print(f"              -> {detail}")
    print("-" * 78)
    print(f"总计 {len(results)} 个变异，捕获 {sum(1 for r in results if r[1]=='CAUGHT')}，"
          f"漏网 {len(missed)}")
    if missed:
        print("漏网清单:", missed)
    # 还原后的自检
    rc, last = run_tests("")
    print(f"还原后回归: rc={rc} | {last}")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
