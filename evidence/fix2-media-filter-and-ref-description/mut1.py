
import re, shutil, subprocess, sys, os
W = "/root/projects/h3s-assetlib-fix"
f = W + "/h3studio/ref_api.py"
orig = open(f, encoding="utf-8").read()
bak = f + ".mutbak"
shutil.copy(f, bak)
# 变异：_describe 退回 _condense 行为（只取第一个有内容的小节 + 截 80 字）
mutated = orig.replace(
    '            return _normalize(text), "analysis"\n    note = (asset.get("note") or "").strip()\n    if note:\n        return _normalize(note), "analysis"',
    '            return _condense_like(text), "analysis"\n    note = (asset.get("note") or "").strip()\n    if note:\n        return _condense_like(note), "analysis"')
assert mutated != orig, "变异未生效（替换点没命中）"
mutated = mutated.replace(
    "def _normalize(text: str) -> str:",
    "def _condense_like(text: str, limit: int = 80) -> str:\n"
    "    text = (text or '').strip()\n"
    "    for chunk in text.replace('【', '\\n【').split('\\n'):\n"
    "        chunk = chunk.strip()\n"
    "        if len(chunk) > 6:\n"
    "            text = chunk\n"
    "            break\n"
    "    text = text.replace('【', '').replace('】', ' ')\n"
    "    text = ' '.join(text.split())\n"
    "    if len(text) > limit:\n"
    "        text = text[:limit].rstrip() + '…'\n"
    "    return text\n\n\n"
    "def _normalize(text: str) -> str:")
open(f, "w", encoding="utf-8").write(mutated)
try:
    r = subprocess.run([sys.executable, "-m", "pytest",
        "tests/test_fix2_media_filter_and_ref_description.py", "-q", "--tb=no"],
        cwd=W, capture_output=True, text=True, timeout=300)
    print("MUT1 returncode:", r.returncode)
    print(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "")
finally:
    shutil.move(bak, f)
    print("restored:", open(f, encoding="utf-8").read() == orig)
