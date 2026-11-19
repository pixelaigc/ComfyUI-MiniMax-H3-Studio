#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7 追加修复：清掉 3 条**用户可见**的内部工程黑话。

【为什么这 3 条漏了】
第一轮 DOM 审计（修复前）命中 8 条，本轮 `e6755b8` 修了其中 5 条
（nonebot / BasicScheduler / KSamplerSelect / SelfLiftH3Sampler / MiniMaxH3SigmaShift）。
剩下 3 条在**部署后的复审**里才被最终审计工具抓到 —— 它们藏在
后端返回给前端的说明文案里（`cfg_fixed_reason` / `twopass.note`），
不在 `web/` 的静态字符串里，所以第一轮「web/ 静态扫描」没覆盖到：

  1. `潜空间放大器`（web/index.html 的字段标签）—— 内部术语，用户看不懂
  2. `二采 = 低分辨率一采 + 高分辨率二采（SelfLift）…`（video_models.VIDEO_TWOPASS.note）
  3. `H3 标准采样路径固定 CFG=1.0：… BasicGuider …（SelfLiftH3Sampler 自带 cfg 槽位）`
     （video_models.VIDEO_CFG_FIXED_REASON）

【修法口径】
信息量不丢，只把「内部工程黑话」换成用户能懂的说法：
  * 节点类名（BasicGuider / SelfLiftH3Sampler）→ 去掉或换成功能描述
  * 「潜空间放大器」→ 「放大器模型」（这是用户真正要选的东西）
  * 「单流条件化 / 负面分支」保留（这是用户判断 CFG 该不该调的依据）
  * 权重文件名**不动**（那是功能数据，用户要选具体权重，不是文案）
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path("/root/projects/h3studio-review-r7")

# ---- 修复 1：web/index.html 的「潜空间放大器」标签 ----
IDX = REPO / "web" / "index.html"
raw = IDX.read_bytes()
text = raw.decode("utf-8")
assert "潜空间放大器" in text, "index.html 里找不到「潜空间放大器」"
# 只改这一个 <span> 标签里的文字，不动注释
new_text = text.replace(
    '<div class="compact-label"><span>潜空间放大器</span>'
    '<small id="vidUpscalerHint">—</small></div>',
    '<div class="compact-label"><span>放大器模型</span>'
    '<small id="vidUpscalerHint">—</small></div>',
)
assert new_text != text, "index.html 替换未命中"
assert "潜空间放大器" not in new_text, "index.html 仍残留「潜空间放大器」"
# CRLF 保持：字节数变化必须等于字符数变化（不引入/删除换行）
before_lf = raw.count(b"\r\n")
IDX.write_bytes(new_text.encode("utf-8"))
after = IDX.read_bytes()
assert after.count(b"\r\n") == before_lf, "index.html 换行结构被改动了"
assert after.count(b"\n") - after.count(b"\r\n") == 0, "index.html 出现裸 LF"
print(f"[1] web/index.html：「潜空间放大器」→「放大器模型」（CRLF 保持，裸 LF = 0）")

# ---- 修复 2 / 3：h3studio/video_models.py 的两段说明文案 ----
VM = REPO / "h3studio" / "video_models.py"
vm_text = VM.read_text(encoding="utf-8")

OLD_NOTE = '''    "note": "二采 = 低分辨率一采 + 高分辨率二采（SelfLift）。"
            "总步数不变，提速来自一采的面积更小。"
            "H3 在 768p 短边下 lowres_scale=0.5 会掉到 384px，"
            "可能超出训练分布 —— 出糊就把它调大。",'''
NEW_NOTE = '''    "note": "二采 = 低分辨率一采 + 高分辨率二采。"
            "总步数不变，提速来自一采的面积更小。"
            "H3 在 768p 短边下 lowres_scale=0.5 会掉到 384px，"
            "可能超出训练分布 —— 出糊就把它调大。",'''

OLD_CFG = '''VIDEO_CFG_FIXED_REASON = (
    "H3 标准采样路径固定 CFG=1.0：官方工作流用的是 BasicGuider"
    "（只有 model + conditioning，没有 cfg 槽位），"
    "H3 的音视频潜变量是单流条件化，没有负面分支。"
    "只有开启二采时 CFG 才可调（SelfLiftH3Sampler 自带 cfg 槽位）。"
)'''
NEW_CFG = '''VIDEO_CFG_FIXED_REASON = (
    "H3 标准采样路径固定 CFG=1.0：标准引导器"
    "只接受模型与条件，没有 cfg 槽位，"
    "H3 的音视频潜变量是单流条件化，没有负面分支。"
    "只有开启二采时 CFG 才可调（二采节点自带 cfg 槽位）。"
)'''

for old, new, label in ((OLD_NOTE, NEW_NOTE, "twopass.note"),
                        (OLD_CFG, NEW_CFG, "VIDEO_CFG_FIXED_REASON")):
    if old not in vm_text:
        raise SystemExit(f"video_models.py 里找不到 {label} 的原文")
    vm_text = vm_text.replace(old, new)

# 断言：只能有**会被送进 DOM 的字符串**里不许出现黑话。
#
# 【重要】不是所有字符串字面量都会进 DOM：
#   * `node_available("SelfLiftH3Sampler")` —— 这是**节点类名参数**，
#     前端拿去查本机节点是否存在，不会显示给用户 ⇒ 必须保留
#   * `node_available("MiniMaxH3SigmaShift")` —— 同上
# 所以判定口径是「这段字符串本身是否就是展示文案」——
# 用「含中文 + 含黑话」来筛：展示文案必有中文，节点类名没有。
import ast

tree = ast.parse(vm_text)
literal_hits = []
for node in ast.walk(tree):
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        continue
    val = node.value
    if not ("SelfLift" in val or "BasicGuider" in val):
        continue
    has_cn = any("一" <= ch <= "鿿" for ch in val)
    if has_cn:            # 中文展示文案 ⇒ 会被用户看到 ⇒ 必须清
        literal_hits.append((node.lineno, val[:160]))
if literal_hits:
    for ln, val in literal_hits:
        print(f"  ❌ 中文展示文案仍含黑话（line {ln}）：{val}")
    raise SystemExit("video_models.py 的可见字符串里仍残留黑话")
print("[自检] video_models.py 的**中文展示文案**里已无 SelfLift / BasicGuider ✅"
      "（节点类名参数保留 —— 前端用它查本机节点可用性，不显示给用户）")

VM.write_text(vm_text, encoding="utf-8")
print("[2] video_models.py：twopass.note 去掉「（SelfLift）」")
print("[3] video_models.py：VIDEO_CFG_FIXED_REASON 去掉 BasicGuider / SelfLiftH3Sampler")

OLD_JS = '''        bits.push("二采固定用 Euler 采样器（SelfLift 只支持标准 Euler）。");'''
NEW_JS = '''        bits.push("二采固定用 Euler 采样器（只支持标准 Euler）。");'''

OLD_CONFIRM = '''            + "二采（SelfLift）在接续段会造成段头色档跳变：\\n"'''
NEW_CONFIRM = '''            + "二采在接续段会造成段头色档跳变：\\n"'''

VP = REPO / "web" / "video-panel.js"
vp_text = VP.read_text(encoding="utf-8")
for old, new, label in ((OLD_JS, NEW_JS, "「二采固定用 Euler 采样器」"),
                        (OLD_CONFIRM, NEW_CONFIRM, "二次确认文案")):
    if old not in vp_text:
        raise SystemExit(f"video-panel.js 里找不到 {label}")
    vp_text = vp_text.replace(old, new)
VP.write_text(vp_text, encoding="utf-8")
print("[4] web/video-panel.js：「二采固定用 Euler 采样器（SelfLift 只支持标准 Euler）」"
      "→「二采固定用 Euler 采样器（只支持标准 Euler）」")
print("[5] web/video-panel.js：二次确认文案「二采（SelfLift）在接续段…」"
      "→「二采在接续段…」")

# ---- 自检：可见文案里不得再有这些黑话 ----
import subprocess

probe = subprocess.run(
    ["grep", "-rn", "-E", "SelfLift|BasicGuider|潜空间放大器",
     str(REPO / "web"), str(REPO / "h3studio" / "video_models.py"),
     str(REPO / "h3studio" / "model_recommendations.py")],
    capture_output=True, text=True)
# 【判定】只把「会被送进 DOM 的字符串」算命中。
#
# 难点：多行 HTML 注释的**续行**不带 `<!--` 前缀（如 index.html:301 / 353），
#      单看行首会误判成展示文案。
# 正确做法：先做**块注释剥离**（把 <!-- --> 与 /* */ 整块抹掉），
#      再在剩余代码里找「既有中文又有黑话」的行。
import re

BLACK = re.compile(r"SelfLift|BasicGuider|潜空间放大器")
CN = re.compile(r"[一-鿿]")

BLOCK_COMMENT = re.compile(r"<!--.*?-->|/\*.*?\*/", re.S)


def strip_block_comments(text: str) -> str:
    """把 HTML 注释块与 C 风格注释块整段抹掉（保留行数，便于定位）。"""
    def blank(m: re.Match) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))
    return BLOCK_COMMENT.sub(blank, text)


def strip_line_comments(text: str) -> str:
    """抹掉行尾注释（// 之后的部分），保留行数与缩进。

    【为什么要单独做】`cfg: 1.0,   // 【…】二采专用 CFG（SelfLiftH3Sampler.cfg）`
    这行**既有代码又有注释**，只按行首判断会误判成展示文案。
    """
    out = []
    for line in text.split("\n"):
        idx = line.find("//")
        if idx >= 0:
            line = line[:idx]
        out.append(line)
    return "\n".join(out)


def strip_py_comments(text: str) -> str:
    """抹掉 Python 的 # 注释（保留行数与缩进）。

    【为什么要单独做】Python 注释用 # 而不是 //，
    strip_line_comments 不处理它。
    """
    out = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            line = line[:len(line) - len(stripped)]
        out.append(line)
    return "\n".join(out)


hits = []
for path in sorted((REPO / "web").rglob("*")):
    if not path.is_file() or path.suffix not in (".js", ".html", ".css"):
        continue
    try:
        raw = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    code = strip_line_comments(strip_block_comments(raw))
    for i, line in enumerate(code.split("\n"), 1):
        stripped = line.strip()
        if not stripped:
            continue
        if not CN.search(line) or not BLACK.search(line):
            continue
        hits.append(f"{path}:{i}: {stripped[:160]}")
for mod in ("h3studio/video_models.py", "h3studio/model_recommendations.py"):
    p = REPO / mod
    code = strip_py_comments(strip_block_comments(p.read_text(encoding="utf-8")))
    for i, line in enumerate(code.split("\n"), 1):
        stripped = line.strip()
        if not stripped:
            continue
        if not CN.search(line) or not BLACK.search(line):
            continue
        hits.append(f"{p}:{i}: {stripped[:160]}")
print("\n=== 残留检查（剥离注释块后，仍会被送进 DOM 的展示文案）===")
for ln in hits:
    print("  ", ln)
print(f"  展示文案残留 = {len(hits)} 条")
if hits:
    raise SystemExit("仍有可见黑话残留")
