#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异验证：把每条修复**改回去**，断言必须变红。

【为什么需要】
「测试通过」不等于「测试有效」。一个空转的断言（断言的东西跟被测行为无关）
在修复被撤销后照样绿。本脚本对每条修复做一次真实的反向变异，
逐条记录断言是否抓住了它 —— 抓住才说明这条断言是有效的。

【覆盖】
  tests/test_visible_text_pollution.py      （A 段文案污染）
  tests/test_flf_r2v_size_not_overridden.py （B 段尺寸覆盖）

【用法】
    python3 docs/audit-evidence/code-review-20261115/verify_mutations.py \
        --repo /root/projects/h3s-cr-20261115
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

T1 = "tests/test_visible_text_pollution.py"
T2 = "tests/test_flf_r2v_size_not_overridden.py"

# (名称, 文件, 旧文本, 新文本, -k 过滤, 测试文件)
MUTATIONS = [
    ("A-1 骨架 model_label 回退", "web/app.js",
     'model_label: "载入中",', 'model_label: "骨架占位模型",',
     "skeleton_placeholder_text_is_neutral", T1),
    ("A-1 骨架 prompt 回退", "web/app.js",
     'prompt: "正在载入任务内容，模型与参数稍后显示，此处的临时文字仅用于对齐布局。",',
     'prompt: "骨架占位提示词，用于让元信息区高度与真实卡片保持一致，长度对齐真实内容。",',
     "skeleton_placeholder_text_is_neutral", T1),
    ("A-1 骨架 title 摘除注释掉", "web/app.js",
     'card.querySelectorAll("[title]").forEach(el => el.removeAttribute("title"));',
     '// card.querySelectorAll("[title]").forEach(el => el.removeAttribute("title"));',
     "skeleton_cards_strip_title", T1),
    ("A-1 骨架 title 摘除换属性", "web/app.js",
     'card.querySelectorAll("[title]").forEach(el => el.removeAttribute("title"));',
     'card.querySelectorAll("[title]").forEach(el => el.removeAttribute("data-title"));',
     "skeleton_cards_strip_title", T1),
    ("A-2 prompt textarea 预填回退", "web/index.html",
     '<textarea id="prompt" rows="6" placeholder="描述画面…支持多镜头分镜写法"></textarea>',
     '<textarea id="prompt" rows="6" placeholder="描述画面…支持多镜头分镜写法">'
     'integrated_multimodal_description: [Shot 1] test</textarea>',
     "prompt_textarea_has_no_default_content", T1),
    ("A-2 prompt placeholder 置空", "web/index.html",
     'placeholder="描述画面…支持多镜头分镜写法"', 'placeholder=""',
     "prompt_textarea_keeps_placeholder", T1),
    ("A-2 prompt placeholder 删除", "web/index.html",
     ' placeholder="描述画面…支持多镜头分镜写法"', '',
     "prompt_textarea_keeps_placeholder", T1),
    ("A-4 jobCard 注释回退", "web/app.js",
     '      ${!runningOrQueued ? `<button data-act="detail" title="查看完整生成参数">ℹ 详情</button>` : ""}',
     '      <!-- dev note -->\n      ${!runningOrQueued ? '
     '`<button data-act="detail" title="查看完整生成参数">ℹ 详情</button>` : ""}',
     "task_card_has_no_html_comment", T1),
    ("A-3 dict 删除失效", "h3studio/lora_text_sanitize.py",
     '    out = _DICT_DUMP.sub("", out)', '    pass', "removes_raw_tokens", T1),
    ("A-3 字段改写失效", "h3studio/lora_text_sanitize.py",
     '    for pattern, repl in _FIELD_REWRITES:\n        out = pattern.sub(repl, out)',
     '    for pattern, repl in _FIELD_REWRITES:\n        pass', "removes_raw_tokens", T1),
    ("A-3 tensor_phrase 失效", "h3studio/lora_text_sanitize.py",
     '    out = _TENSOR_PHRASE.sub("", out)', '    pass', "removes_raw_tokens", T1),
    ("A-3 tensor_slash 两条都失效", "h3studio/lora_text_sanitize.py",
     '    out = _TENSOR_SLASH.sub("权重结构为标准 LoRA", out)\n'
     '    # _TENSOR_SLASH 没匹配到的「N tensor / M lora」变体（分隔符可能是全角 /）\n'
     '    out = _TENSOR_SLASH2.sub("权重结构为标准 LoRA", out)',
     '    pass\n'
     '    # _TENSOR_SLASH 没匹配到的「N tensor / M lora」变体（分隔符可能是全角 /）\n'
     '    pass', "removes_raw_tokens", T1),
    ("A-3 幂等性破坏", "h3studio/lora_text_sanitize.py",
     '    return _collapse_spaces(out)', '    return _collapse_spaces(out) + " "',
     "sanitizer_is_idempotent", T1),
    ("A-3 误伤普通文案", "h3studio/lora_text_sanitize.py",
     '    if not text:\n        return ""',
     '    if not text:\n        return ""\n    text = text.replace("LoRA", "权重")',
     "preserves_plain_text", T1),
    ("A-3 删掉用户语义", "h3studio/lora_text_sanitize.py",
     '    if not text:\n        return ""',
     '    if not text:\n        return ""\n    text = text.replace("触发词", "词")',
     "keeps_user_meaning", T1),
    ("A-3 清空非空文案", "h3studio/lora_text_sanitize.py",
     '    if not text:\n        return ""',
     '    if not text:\n        return ""\n    return ""', "never_empties_non_empty_text", T1),
    ("A-3 scene 调用点断开", "h3studio/loras.py",
     '"scene": lora_text_sanitize.sanitize_scene(info.get("scene", "")),',
     '"scene": info.get("scene", ""),', "all_three_user_fields", T1),
    ("A-3 strength_source 调用点断开", "h3studio/loras.py",
     '"strength_source": lora_text_sanitize.sanitize_strength_source(\n'
     '                    info.get("strength_source", "")),',
     '"strength_source": info.get("strength_source", ""),', "all_three_user_fields", T1),
    ("A-3 source 调用点断开", "h3studio/loras.py",
     '"source": lora_text_sanitize.sanitize_user_text(info.get("source", "")),',
     '"source": info.get("source", ""),', "all_three_user_fields", T1),
    ("A-3 scene 整体失效(清空)", "h3studio/loras.py",
     '"scene": lora_text_sanitize.sanitize_scene(info.get("scene", "")),',
     '"scene": lora_text_sanitize.sanitize_scene(""),', "real_lora_scene_is_not_emptied", T1),
    ("D style.css 版本回退", "web/index.html",
     'href="/static/style.css?v=cr20261115a"', 'href="/static/style.css?v=uxbatch1"',
     "style_css_version_bumped", T1),
    ("D favicon 版本回退", "web/index.html",
     'href="/static/favicon.svg?v=cr20261115a"', 'href="/static/favicon.svg"',
     "all_static_assets_have_version_param", T1),
    ("B 恢复 width/height 覆盖", "web/app.js",
     '    const legacySize = currentLegacyVideoSize();\r\n    if (legacySize) {\r\n'
     '      params.resolution = legacyTierForPixels(\r\n'
     '        Number(params.width) || 0, Number(params.height) || 0);\r\n    }',
     '    const legacySize = currentLegacyVideoSize();\r\n    if (legacySize) {\r\n'
     '      params.width = legacySize.width;\r\n      params.height = legacySize.height;\r\n    }',
     "flf", T2),
    ("B 删掉 resolution 档位反算", "web/app.js",
     '    const legacySize = currentLegacyVideoSize();\r\n    if (legacySize) {\r\n'
     '      params.resolution = legacyTierForPixels(\r\n'
     '        Number(params.width) || 0, Number(params.height) || 0);\r\n    }',
     '    const legacySize = currentLegacyVideoSize();\r\n    if (legacySize) {\r\n    }',
     "flf", T2),
    ("B 改 state.res 初始值", "web/app.js",
     'const state = {\r\n', 'const state = {\r\n  res: "608p",\r\n', "never_written", T2),
    ("B 改 state.aspect 初始值", "web/app.js",
     '  aspect: "16:9",\r\n', '  aspect: "21:9",\r\n', "never_written", T2),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    args = ap.parse_args()
    repo = Path(args.repo).resolve()

    print(f"仓库：{repo}")
    print(f"变异用例：{len(MUTATIONS)} 条\n")

    caught = missed = anchor_miss = 0
    for name, rel, old, new, expr, testfile in MUTATIONS:
        path = repo / rel
        original = path.read_bytes()
        text = original.decode("utf-8")
        if old not in text:
            print(f"  ANCHOR-MISS  {name}")
            anchor_miss += 1
            continue
        try:
            path.write_bytes(text.replace(old, new, 1).encode("utf-8"))
            proc = subprocess.run(
                ["python3", "-m", "pytest", testfile, "-q", "--tb=no", "-k", expr],
                capture_output=True, text=True, cwd=repo, timeout=200)
            ok = proc.returncode != 0
            print(f"  {'CAUGHT ' if ok else 'MISSED '}    {name}")
            caught += ok
            missed += (not ok)
        finally:
            path.write_bytes(original)

    # 还原后必须仍然全绿
    final = subprocess.run(
        ["python3", "-m", "pytest", T1, T2, "-q", "--tb=no"],
        capture_output=True, text=True, cwd=repo, timeout=300)
    print(f"\n还原后回归：{final.stdout.strip().splitlines()[-1]}")
    print(f"\ncaught={caught}  missed={missed}  anchor-miss={anchor_miss}")
    print("结论：" + ("PASS —— 全部变异均被捕获" if missed == 0 else "FAIL —— 有变异未被捕获"))
    return 0 if missed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
