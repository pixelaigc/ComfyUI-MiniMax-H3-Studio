# -*- coding: utf-8 -*-
"""P3-2 复验：歌词「不足 4 行」只对**主副歌段**生效。

用审查报告里的**原案例**当判据：
  段落结构 [Intro]/[Verse 1]/[Pre-Chorus]/[Bridge]/[Outro] 都不是主副歌，
  审查报告实测它们被误报成「不足 4 行」。修复后不应再报。
同时确认**主副歌段**行数不足仍要报（校验没被改废）。
"""
import sys

sys.path.insert(0, "/root/projects/h3studio-enh-unify")
from h3studio.yue2_schema import _lyrics_problems   # noqa: E402

# ---- 案例 1：审查报告里的误报案例（合法段落结构）----
LEGAL = """[Intro]
微光落在窗台上

[Verse 1]
第一行
第二行
第三行

[Pre-Chorus]
风开始转

[Bridge]
桥段的四行
桥段第二行
桥段第三行
桥段第四行

[Outro]
灯灭了
"""

# ---- 案例 2：主副歌只有 2 行 —— 必须仍报 ----
THIN_CHORUS = """[Chorus]
第一行
第二行

[Verse 1]
一二三
四五六
七八九
"""

# ---- 案例 3：纯器乐（每段 [Instrumental]）—— 不得报「不足 4 行」----
INST = """[Intro]
[Instrumental]

[Verse 1]
[Instrumental]

[Chorus]
[Instrumental]

[Bridge]
[Instrumental]

[Outro]
[Instrumental]
"""

# ---- 案例 4：正常完整副歌（4 行）—— 不得报 ----
GOOD = """[Chorus]
一二三四
五六七八
九十十一
十二十三

[Bridge]
桥段四行
第二行
第三行
第四行
"""

results = {}
ok = True


def case(name, lyrics, must_not_contain, must_contain=None):
    global ok
    probs = _lyrics_problems(lyrics)
    joined = " | ".join(probs)
    passed = True
    for needle in must_not_contain:
        if needle in joined:
            passed = False
    for needle in (must_contain or []):
        if needle not in joined:
            passed = False
    results[name] = {"problems": probs, "passed": passed}
    ok = ok and passed
    print("[%s] %s" % ("PASS" if passed else "FAIL", name))
    print("    problems =", probs)
    return passed


case("案例1 合法段落结构（审查报告误报案例）", LEGAL,
     ["不足 4 行", "不足 4 行: ['[Intro]'", "['[Verse 1]']"])
case("案例2 主副歌仅 2 行（必须仍报）", THIN_CHORUS,
     [], ["副歌段不足 4 行"])
case("案例3 纯器乐（不得报行数）", INST, ["不足 4 行"])
case("案例4 正常副歌 4 行（不得报）", GOOD, ["不足 4 行"])

print()
print("=== P3-2 判定:", "PASS" if ok else "FAIL", "===")
sys.exit(0 if ok else 1)
