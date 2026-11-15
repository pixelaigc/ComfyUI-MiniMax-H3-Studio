
import shutil, subprocess, sys
W = "/root/projects/h3s-assetlib-fix"

def run(name, edits, testfile="tests/test_fix2_media_filter_and_ref_description.py"):
    """edits: list of (relpath, old, new)"""
    baks = []
    try:
        for relpath, old, new in edits:
            f = W + "/" + relpath
            orig = open(f, encoding="utf-8").read()
            bak = f + ".mutbak"
            shutil.copy(f, bak)
            baks.append((f, bak, orig))
            mutated = orig.replace(old, new)
            assert mutated != orig, f"{name}: 变异未生效 @ {relpath}"
            open(f, "w", encoding="utf-8").write(mutated)
        r = subprocess.run([sys.executable, "-m", "pytest", testfile, "-q", "--tb=no"],
            cwd=W, capture_output=True, text=True, timeout=300)
        last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
        print(f"{name}: returncode={r.returncode} | {last}")
    finally:
        for f, bak, orig in baks:
            shutil.move(bak, f)
        print(f"{name}: restored OK")

# MUT2-FULL: 忠实还原「修复前」的原始实现（两个点同时还原）：
#   (a) showStitch 里 state.stitchMediaOverride = "video"
#   (b) refreshJobs 里按 page==stitch 叠加 media=video
run("MUT2-FULL (完整还原修复前的 stitch 强加)", [
 ("web/app.js",
  '  state.stitchMediaOverride = "";\n  renderTabs();\n  renderStitchClips();\n  refreshJobs();',
  '  state.stitchMediaOverride = "video";\n  renderTabs();\n  renderStitchClips();\n  refreshJobs();'),
 ("web/app.js",
  '        if (tab === "longfilm") query.set("media", "longfilm");\n'
  '        else Object.entries(state.doneFilters).forEach(([key, value]) => { if (value) query.set(key, value); });',
  '        if (tab === "longfilm") query.set("media", "longfilm");\n'
  '        else Object.entries(state.doneFilters).forEach(([key, value]) => { if (value) query.set(key, value); });\n'
  '        if (state.page === "stitch" && state.stitchMediaOverride) {\n'
  '          query.set("media", state.stitchMediaOverride);\n'
  '        }'),
])

# MUT7: showStitch 写全局 doneFilters.media（修复前的最初形态）
run("MUT7 (showStitch 写全局 doneFilters.media)", [
 ("web/app.js",
  '  state.stitchMediaOverride = "";\n  renderTabs();\n  renderStitchClips();\n  refreshJobs();',
  '  state.doneFilters.media = "video";\n'
  '  $("#doneMedia").value = "video";\n'
  '  state.stitchMediaOverride = "";\n  renderTabs();\n  renderStitchClips();\n  refreshJobs();'),
])

# MUT8: notifyPageChange 不更新 state.page
run("MUT8 (notifyPageChange 不更新 state.page)", [
 ("web/app.js",
  '  notifyPageChange: function (page) {\n    if (!page) return;\n    state.page = page;',
  '  notifyPageChange: function (page) {\n    if (!page) return;\n    // mutated: 不更新 state.page'),
])
