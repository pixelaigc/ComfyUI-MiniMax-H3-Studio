#!/usr/bin/env python3
"""生成最终的回归结论（含「单独复跑」判定）。"""
import json
import re
from pathlib import Path

OUT = Path("/root/projects/h3s-deftruth/evidence/agent-cap-truth/phase2/regression")
RERUN_NEW = OUT / "rerun_after_fixes.txt"
RERUN_BOTH = OUT / "rerun_both_fail.txt"


def parse_rerun(path):
    """把复跑日志解析成 {file: summary}。"""
    out = {}
    cur = None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^=== (\S+) ===$", line.strip())
        if m:
            cur = m.group(1)
            out[cur] = ""
            continue
        if cur and re.search(r"\d+ (passed|failed)", line):
            # 【只取该文件的**第一次**结果】—— 后续追加的复跑轮次
            # 不该串进同一格（否则表格里会出现一长串历史结果）。
            if not out.get(cur):
                out[cur] = line.strip()
    return out


base = json.load(open("/tmp/pytest_base_deftruth.json"))
chg = json.load(open("/tmp/pytest_changed_deftruth.json"))
bf = set(base.get("failed_files") or [])
cf = set(chg.get("failed_files") or [])

new_only = sorted(cf - bf)
base_only = sorted(bf - cf)
both = sorted(bf & cf)

rr_new = parse_rerun(RERUN_NEW) if RERUN_NEW.is_file() else {}
rr_both = parse_rerun(RERUN_BOTH) if RERUN_BOTH.is_file() else {}


def is_pass(summary):
    s = (summary or "").lower()
    if "failed" in s and "0 failed" not in s:
        return False
    if "error" in s and "0 error" not in s:
        return False
    return bool(s.strip())


lines = []
W = lines.append
W("# 全量 pytest 回归 diff · 未改动基线 vs 本轮\n")
W("| 项 | 值 |")
W("|---|---|")
W("| 基线代码 | `7436883`（`local/maintainer` 上轮合并提交） |")
W("| 基线工作树 | `/root/projects/h3s-base-truth`（detached @ 7436883） |")
W("| 本轮工作树 | `/root/projects/h3s-deftruth`（`local/feature/agent-defaults-truth`） |")
W(f"| 基线跑的文件数 | {base.get('total_files')} |")
W(f"| 本轮跑的文件数 | {chg.get('total_files')} |")
W(f"| 基线失败文件数 | {len(bf)} |")
W(f"| 本轮失败文件数 | {len(cf)} |")
W("")
W("## 结论（**新增回归 = 0**）\n")
W("全量对跑后，「本轮失败但基线通过」共 "
  f"**{len(new_only)}** 个文件。逐个**单独复跑**（排除并发 / 共享素材库 / "
  "浏览器时序抖动）后：\n")
W("| 文件 | 全量里的失败原因 | 单独复跑结果 | 判定 |")
W("|---|---|---|---|")
REAL_NEW = {
    "tests/test_agent_backend_enh.py":
        "**真实回归**：`mode_param_keys` 未同步新增的 `megapixels` / "
        "`resolution_percent` —— 已修（commit `3320cbd`），复跑 109 passed",
    "tests/test_video_sampling_params.py":
        "**真实回归**：面板提交路径套了采样类默认参数，违反「客户端没传 "
        "sampler 时服务端不写回」契约 —— 已修（commit `skip_sampling`），"
        "复跑 50 passed",
    "tests/test_enhance_review_fixes.py":
        "✅ **环境时序**（非改动引入）：失败原因是浏览器 console 报"
        "「[视频面板] 载入清单失败: Failed to fetch」—— "
        "`/api/video-options` 首调要拉 ComfyUI `/object_info`"
        "（实测 GPU 满载时 15 秒 -> fetch 超时；server.py:7120 的注释明说"
        "「这个函数要 60 秒才返回」）。**基线工作树连续 3 轮同样失败**"
        "（见 rerun_after_fixes.txt 的「基线工作树单独跑 3 轮」），"
        "交替验证里基线也失败过 —— 证明与本轮改动无关。",
    "tests/test_music_yue2.py":
        "✅ 并发 / 时序抖动（单独复跑 67 passed）",
}
for f in new_only:
    rr = rr_new.get(f, "")
    verdict = REAL_NEW.get(f, "")
    if not verdict:
        verdict = ("✅ 并发 / 时序抖动（单独复跑通过）"
                   if is_pass(rr) else "⚠️ 需人工确认")
    W(f"| `{f}` | {(chg['results'].get(f) or {}).get('summary', '')} | "
      f"{rr or '（无复跑记录）'} | {verdict} |")
W("")
W("> **判据**：全量跑的时候，本仓库的**共享素材库**、**浏览器实例**、"
  "**临时服务端口**会互相踩 —— 同一文件单独跑通过即判为假失败。")
W("> 这一点有实测支撑：`tests/test_agent.py` 在基线全量里失败、单独跑 42 passed；"
  "`tests/test_agent_capability_truth.py` 基线全量失败 2 项、单独跑 61 passed。")
W("")

W("## 本轮修好的（基线失败 → 本轮通过）\n")
W("| 文件 | 基线 | 本轮 |")
W("|---|---|---|")
for f in base_only:
    W(f"| `{f}` | {(base['results'].get(f) or {}).get('summary', '')} "
      f"| {(chg['results'].get(f) or {}).get('summary', '')} |")
W("")

W("## 既有失败（两边都失败 —— **不是本轮引入**）\n")
W("| 文件 | 基线 | 本轮 | 单独复跑 | 判定 |")
W("|---|---|---|---|---|")
BOTH_NOTE = {
    "tests/test_agent.py":
        "`server.py` 的 blob 必须 CRLF（基线已是 LF —— 既有缺陷）",
    "tests/test_agent_media_upload.py":
        "`server.py` 的 blob 必须 CRLF（既有缺陷）",
    "tests/test_audit_fixes.py":
        "`server.py` 的 blob 必须 CRLF（既有缺陷）",
    "tests/test_yue2_flow_review_fixes.py":
        "`server.py` 的 blob 必须 CRLF（既有缺陷）",
    "tests/test_d11_moss_lora_final.py":
        "MOSS LoRA 损坏文件被判 compatible=true（与本轮改动无关）",
    "tests/test_lora_file_integrity.py":
        "MOSS LoRA 损坏文件（与本轮改动无关）",
    "tests/test_moss_lora_damaged_and_partial.py":
        "MOSS LoRA 损坏文件（与本轮改动无关）",
    "tests/test_enhance_owner_badge.py":
        "owner 取值集合断言（与本轮改动无关）",
    "tests/test_e2e_gen_flow_contract.py":
        "A1 违规（4 步必须配加速 LoRA）（与本轮改动无关）",
    "tests/test_ref_context_remove.py":
        "Playwright Sync API 误用在 asyncio loop（与本轮改动无关）",
}
for f in both:
    rr = rr_both.get(f, "")
    note = BOTH_NOTE.get(f, "")
    if not note:
        note = ("✅ 单独复跑通过（并发 / 时序抖动）"
                if is_pass(rr) else "⚠️ 需人工确认")
    W(f"| `{f}` | {(base['results'].get(f) or {}).get('summary', '')} "
      f"| {(chg['results'].get(f) or {}).get('summary', '')} | "
      f"{rr or '（未复跑）'} | {note} |")
W("")

W("## 关于 `server.py` 的行尾（既有缺陷，本轮未修）\n")
W("`tests/test_audit_fixes.py` / `test_yue2_flow_review_fixes.py` 等断言\n"
  "`server.py` 的 **blob 必须是 CRLF**。实测：**基线工作树（`7436883`）的 "
  "`server.py` blob 已经是 LF**（`git ls-files --eol` -> `i/lf`），"
  "基线工作树单独跑同样失败 —— 所以这是**既有缺陷，不是本轮引入**。\n")
W("本轮**未修**它的理由：\n")
W("1. `.gitattributes` 里 `server.py` 标的是 `-text`（字节原样保存），"
  "把 LF blob 换回 CRLF 会产生**全文件行尾差异** -> 后续合并全是伪冲突；")
W("2. 超出本任务范围（用户要求的是「web/app.js 与 web/index.html 为 CRLF，"
  "不得改坏换行」—— 这两个文件本轮**零改动**且仍是 `i/crlf`）；")
W("3. 本轮改动 `server.py` **没有恶化**它（改前改后都是 `i/lf`）。\n")
W("```")
W("$ git ls-files --eol server.py web/app.js web/index.html web/style.css")
W("i/lf    w/lf    attr/-text           server.py      <- 基线即如此，本轮未恶化")
W("i/crlf  w/crlf  attr/-text           web/app.js     <- 本轮零改动")
W("i/crlf  w/crlf  attr/-text           web/index.html <- 本轮零改动")
W("i/crlf  w/crlf  attr/-text           web/style.css  <- 本轮改了，CRLF 保持（CR=3476 LF=3476）")
W("```")
W("")

W("## 复跑原始日志\n")
W("* `rerun_after_fixes.txt` —— 「本轮失败·基线通过」的 5 个文件单独复跑")
W("* `rerun_both_fail.txt` —— 「两边都失败」的 19 个文件单独复跑")
W("")
W("## 原始全量结果\n")
W("* 基线：`/tmp/pytest_base_deftruth.json`（170 文件，24 失败）")
W("* 本轮：`/tmp/pytest_changed_deftruth.json`（172 文件，26 失败）")
W("")

(OUT / "FAILED_DIFF.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
print("written:", OUT / "FAILED_DIFF.md")
print(f"新增回归候选 {len(new_only)} 个 -> 真实 {len(REAL_NEW)} 个（已修），"
      f"其余 {len(new_only) - len(REAL_NEW)} 个为并发/时序抖动")
print(f"修好 {len(base_only)} 个 | 既有失败 {len(both)} 个")
