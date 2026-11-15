# -*- coding: utf-8 -*-
"""修复② 回归测试的**变异测试**（mutation test）。

【为什么需要这个】
新写的测试最常见的失败模式是「测了个寂寞」—— 断言看着有，
但把被测行为改回去它照样全绿。这里对修复② 的每一处关键实现
做一次「故意改坏」，要求对应断言**必须红**。

【为什么在副本上做，不在真实 worktree 上做】
第一轮在真实 worktree 上跑，出现过两次事故：
  · 同一个文件做两处变异时，第二处的备份取自「第一处已改坏」的中间态，
    还原时把中间态写回真实文件 -> 修复代码被污染（stitchMediaOverride
    被还原成 "video"）；
  · 还原时按行尾重写文件，把 CRLF 变成 LF -> 整个文件显示成大改动。
所以这里把整个 worktree **复制**到临时目录再变异，真实 worktree 一个字节都不动。

【方法】
每个变异：
  1. 把 worktree 复制到临时目录（rsync/cp -a，含 .git 不必要，去掉更快）；
  2. 在副本里改坏一处实现；
  3. 跑 tests/test_fix2_media_filter_and_ref_description.py；
  4. 要求**必须失败**（rc != 0）；
  5. 临时目录整个删掉（真实 worktree 从未被碰）。

【行尾注意】
web/ 下多数文件是 CRLF，h3studio/ 下是 LF —— 模式串里的 \n
要按文件实际行尾转换，否则替换根本命不中（会误报「变异未生效」）。

运行：python3 evidence-labc/fix2/mutation_final.py
退出码 0 = 全部变异都被抓住；非 0 = 有变异漏网。
"""
import os
import shutil
import subprocess
import sys
import tempfile

W = "/root/projects/h3s-assetlib-fix"
TESTFILE = "tests/test_fix2_media_filter_and_ref_description.py"


def make_copy(dst):
    """复制 worktree（不含 .git / __pycache__ / 大目录）。"""
    os.makedirs(dst, exist_ok=True)
    for name in ("web", "h3studio", "tests", "server.py"):
        src = os.path.join(W, name)
        out = os.path.join(dst, name)
        if os.path.isdir(src):
            shutil.copytree(src, out, ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", ".pytest_cache"))
        else:
            shutil.copy2(src, out)


def run(name, edits, testfile=TESTFILE):
    """edits: list of (relpath, old, new)。old/new 用 \n 写，按文件行尾自动转换。"""
    tmp = tempfile.mkdtemp(prefix="mutcopy-")
    work = os.path.join(tmp, "wt")
    try:
        make_copy(work)
        for relpath, old, new in edits:
            f = os.path.join(work, relpath)
            raw = open(f, "rb").read()
            crlf = b"\r\n" in raw
            text = raw.decode("utf-8")
            o = old.replace("\n", "\r\n") if crlf else old
            n = new.replace("\n", "\r\n") if crlf else new
            mutated = text.replace(o, n)
            assert mutated != text, f"{name}: 变异未生效 @ {relpath} (crlf={crlf})"
            open(f, "wb").write(mutated.encode("utf-8"))
        r = subprocess.run([sys.executable, "-m", "pytest", testfile, "-q", "--tb=no"],
                           cwd=work, capture_output=True, text=True, timeout=300)
        last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
        caught = r.returncode != 0
        print(f"{'CAUGHT ' if caught else 'MISSED!'} {name}: rc={r.returncode} | {last}")
        return caught
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 锚点：showStitch 里那段「正确分工」注释（唯一） ----
SHOW_STITCH_ANCHOR = (
    '  //   只把视频产物列进「可加入片段」的候选，不动共享媒体库的过滤。\n'
    '  state.stitchMediaOverride = "";')

# ---- 锚点：refreshJobs 里拼 query 的那段（唯一） ----
QUERY_ANCHOR = (
    '        if (tab === "longfilm") query.set("media", "longfilm");\n'
    '        else Object.entries(state.doneFilters).forEach(([key, value]) => '
    '{ if (value) query.set(key, value); });')

results = []

results.append(run(
    "MUT2-FULL 完整还原修复前的「长片拼接强加 media=video」", [
        ("web/app.js",
         SHOW_STITCH_ANCHOR,
         '  //   只把视频产物列进「可加入片段」的候选，不动共享媒体库的过滤。\n'
         '  state.stitchMediaOverride = "video";'),
        ("web/app.js",
         QUERY_ANCHOR,
         QUERY_ANCHOR + '\n'
         '        if (state.page === "stitch" && state.stitchMediaOverride) {\n'
         '          query.set("media", state.stitchMediaOverride);\n'
         '        }'),
    ]))

results.append(run(
    "MUT7 showStitch 写全局 doneFilters.media（缺陷最初形态）", [
        ("web/app.js",
         SHOW_STITCH_ANCHOR,
         '  //   只把视频产物列进「可加入片段」的候选，不动共享媒体库的过滤。\n'
         '  state.doneFilters.media = "video";\n'
         '  $("#doneMedia").value = "video";\n'
         '  state.stitchMediaOverride = "";'),
    ]))

results.append(run(
    "MUT3 agent 写 window.state（死代码，state.page 不更新）", [
        ("web/agent.js",
         '    const bridge = window.H3Studio;\n'
         '    if (bridge && typeof bridge.notifyPageChange === "function") {\n'
         '      bridge.notifyPageChange("agent");\n'
         '    }',
         '    if (window.state) window.state.page = "agent";'),
    ]))

results.append(run(
    "MUT4 syncRoute 无条件写 URL（用户没访问 Agent 也改 URL）", [
        ("web/agent_sessions.js",
         '    const agentVisible = !!(agentPage && !agentPage.classList.contains("page-hidden"));\n'
         '    if (!agentVisible) return;',
         '    // mutated: unconditional'),
    ]))

results.append(run(
    "MUT5 H3-Image 不注入描述（描述根本没进模型）", [
        ("server.py",
         '            params["prompt"] = h3img.inject_h3_image_reference_block(\n'
         '                prompt_text, refs)',
         '            pass  # mutated'),
    ]))

results.append(run(
    "MUT6 Qwen 占位存在时不注入描述（描述整段丢失）", [
        ("h3studio/ref_images.py",
         '        desc_block = build_reference_description_block(refs)\n'
         '        if not desc_block:\n'
         '            return prompt\n'
         '        return f"{prompt}\\n{desc_block}" if prompt else desc_block',
         '        return prompt  # mutated'),
    ]))

results.append(run(
    "MUT8 notifyPageChange 不更新 state.page", [
        ("web/app.js",
         '  notifyPageChange: function (page) {\n'
         '    if (!page) return;\n'
         '    state.page = page;',
         '  notifyPageChange: function (page) {\n'
         '    if (!page) return;\n'
         '    // mutated'),
    ]))

results.append(run(
    "MUT9 _describe 退回「截断到 80 字」（缺陷2 根因）", [
        ("h3studio/ref_api.py",
         '            return _normalize(text), "analysis"',
         '            return _normalize(text)[:80], "analysis"'),
        ("h3studio/ref_api.py",
         '    if note:\n        return _normalize(note), "analysis"',
         '    if note:\n        return _normalize(note)[:80], "analysis"'),
    ]))

results.append(run(
    "MUT10 _describe 退回「只取第一行」（缺陷2 根因）", [
        ("h3studio/ref_api.py",
         '            return _normalize(text), "analysis"',
         '            return _normalize(text).split("\\n")[0], "analysis"'),
    ]))

results.append(run(
    "MUT11 前端描述框写死 rows=2（只显示第一行）", [
        ("web/qwen-refs.js",
         '          <textarea data-desc-idx="${i}" rows="${rows}" spellcheck="false"',
         '          <textarea data-desc-idx="${i}" rows="2" spellcheck="false"'),
    ]))

print()
print("SUMMARY: %d/%d mutations caught" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
