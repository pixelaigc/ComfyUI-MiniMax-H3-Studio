
import shutil, subprocess, sys
W = "/root/projects/h3s-assetlib-fix"

def run(name, relpath, old, new):
    f = W + "/" + relpath
    orig = open(f, encoding="utf-8").read()
    bak = f + ".mutbak"
    shutil.copy(f, bak)
    mutated = orig.replace(old, new)
    assert mutated != orig, f"{name}: 变异未生效（替换点没命中）"
    open(f, "w", encoding="utf-8").write(mutated)
    try:
        r = subprocess.run([sys.executable, "-m", "pytest",
            "tests/test_fix2_media_filter_and_ref_description.py", "-q", "--tb=no"],
            cwd=W, capture_output=True, text=True, timeout=300)
        last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
        print(f"{name}: returncode={r.returncode} | {last}")
    finally:
        shutil.move(bak, f)
        assert open(f, encoding="utf-8").read() == orig, f"{name}: 恢复失败"
        print(f"{name}: restored OK")

# MUT2: app.js 的 query 构造改回「长片拼接页强加 media=video」
run("MUT2 (stitch 强加 media=video)", "web/app.js",
    '        if (tab === "longfilm") query.set("media", "longfilm");\n'
    '        else Object.entries(state.doneFilters).forEach(([key, value]) => { if (value) query.set(key, value); });',
    '        if (tab === "longfilm") query.set("media", "longfilm");\n'
    '        else Object.entries(state.doneFilters).forEach(([key, value]) => { if (value) query.set(key, value); });\n'
    '        if (state.page === "stitch" && state.stitchMediaOverride) {\n'
    '          query.set("media", state.stitchMediaOverride);\n'
    '        }')

# MUT3: agent.js 退回写 window.state（死代码）
run("MUT3 (agent 写 window.state)", "web/agent.js",
    '    const bridge = window.H3Studio;\n'
    '    if (bridge && typeof bridge.notifyPageChange === "function") {\n'
    '      bridge.notifyPageChange("agent");\n'
    '    }',
    '    if (window.state) window.state.page = "agent";')

# MUT4: agent_sessions.js 的 syncRoute 去掉「Agent 页可见才写路由」
run("MUT4 (syncRoute 无条件写 URL)", "web/agent_sessions.js",
    '    const agentVisible = !!(agentPage && !agentPage.classList.contains("page-hidden"));\n'
    '    if (!agentVisible) return;',
    '    // mutated: unconditional')

# MUT5: H3-Image 不注入描述（退回原样）
run("MUT5 (H3-Image 不注入描述)", "server.py",
    '            params["prompt"] = h3img.inject_h3_image_reference_block(\n'
    '                prompt_text, refs)',
    '            pass  # mutated: no description injection')

# MUT6: Qwen 路径退回「用户写了占位就原样返回」（描述丢失）
run("MUT6 (Qwen 占位存在时不注入描述)", "h3studio/ref_images.py",
    '        desc_block = build_reference_description_block(refs)\n'
    '        if not desc_block:\n'
    '            return prompt\n'
    '        return f"{prompt}\\n{desc_block}" if prompt else desc_block',
    '        return prompt  # mutated: description dropped')
