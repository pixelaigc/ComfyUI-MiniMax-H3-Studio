#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
变异自证 —— 证明 tests/test_reflabel_dialect.py 的断言**真的会红**。

【为什么必须做】willis 验证纪律：报「全绿」必须自问「有没有一条本该红的」。
本脚本逐个改坏实现（改法都对应真实可能犯的错），跑测试，
断言**必须**失败；再还原，确认恢复绿。

用法：python3 scripts/mutate_reflabel_dialect.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST = "tests/test_reflabel_dialect.py"


def _run(cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "-p", "no:randomly",
         "--no-header", "-x"],
        cwd=cwd, capture_output=True, text=True, timeout=600)
    return proc.returncode, (proc.stdout + proc.stderr)


#: (名称, 文件, 原文, 改坏后的文本)
MUTATIONS = [
    (
        "方言被忽略（label_for 不看 dialect）",
        "h3studio/enhance_assets.py",
        '    if dialect == LABEL_DIALECT_QWEN:\n'
        '        # Qwen 官方语法：小写、**无空格**。\n'
        '        if category == "image":\n'
        '            return f"<image{index}>"\n'
        '        return f"<{name} {index}>"\n',
        '    if False:\n'
        '        if category == "image":\n'
        '            return f"<image{index}>"\n'
        '        return f"<{name} {index}>"\n',
    ),
    (
        "Qwen 方言放宽到接受 <Picture N>（假通过）",
        "h3studio/enhance_assets.py",
        '        return r"<\\s*image\\s*\\d+\\s*>"',
        '        return r"<\\s*image\\s*\\d+\\s*>|<(?:Subject|Picture|Video|Audio)\\s*\\d+>"',
    ),
    (
        "H3 方言放宽到接受 <imageN>（假通过）",
        "h3studio/enhance_assets.py",
        '    return r"<(?:Subject|Picture|Video|Audio)\\s*\\d+>"\n\n\n'
        'def normalize_label',
        '    return r"<(?:Subject|Picture|Video|Audio)\\s*\\d+>|<\\s*image\\s*\\d+\\s*>"\n\n\n'
        'def normalize_label',
    ),
    (
        "归一化把大小写也抹掉（<Picture1> == <image1>）",
        "h3studio/enhance_assets.py",
        '    return re.sub(r"\\s+", "", (tag or "").strip())',
        '    return re.sub(r"\\s+", "", (tag or "").strip()).lower()',
    ),
    (
        "missing_labels 不做归一化比较",
        "h3studio/enhance_assets.py",
        '    found_norm = {normalize_label(t) for t in (found or [])}\n'
        '    return [lb for lb in (expected or [])\n'
        '            if normalize_label(lb) not in found_norm]',
        '    return [lb for lb in (expected or [])\n'
        '            if lb not in (found or [])]',
    ),
    (
        "服务端方言路由把 i2i_qwen 也当成 H3",
        "server.py",
        '    if mode_ctx == "i2i_qwen":\n'
        '        return enh_assets.LABEL_DIALECT_QWEN',
        '    if False:\n'
        '        return enh_assets.LABEL_DIALECT_QWEN',
    ),
    (
        "服务端校验标签时不传方言",
        "server.py",
        '        found_labels = enh_assets.extract_reference_labels(\n'
        '            enhanced, label_dialect)',
        '        found_labels = enh_assets.extract_reference_labels(enhanced)',
    ),
    (
        "服务端**响应回显**的 found 不传方言（2026-11-21 实测踩到）",
        "server.py",
        '            "found": enh_assets.extract_reference_labels(\n'
        '                enhanced, label_dialect),',
        '            "found": enh_assets.extract_reference_labels(enhanced),',
    ),
    (
        "服务端解析素材时不传方言",
        "server.py",
        '            resolved_assets = await _resolve_enhance_assets(\n'
        '                asset_refs, label_dialect)',
        '            resolved_assets = await _resolve_enhance_assets(asset_refs)',
    ),
    (
        "describe 不再讲 Qwen 的标签写法",
        "h3studio/enhance_assets.py",
        '        dialect = resolved.get("dialect") or LABEL_DIALECT_H3\n'
        '        if dialect == LABEL_DIALECT_QWEN:',
        '        dialect = resolved.get("dialect") or LABEL_DIALECT_H3\n'
        '        if False:',
    ),
]


def main() -> int:
    # 基线：未变异必须全绿
    code, out = _run(ROOT)
    print(f"[基线] exit={code}")
    if code != 0:
        print(out[-3000:])
        print("!! 基线不是绿的，变异验证无意义")
        return 1
    print("基线全绿\n")

    ok = True
    for name, rel, old, new in MUTATIONS:
        src = ROOT / rel
        original = src.read_text(encoding="utf-8")
        if old not in original:
            print(f"!! 变异「{name}」找不到锚点 —— 锚点已漂移，需更新脚本")
            ok = False
            continue
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "repo"
            shutil.copytree(ROOT, work, symlinks=True,
                            ignore=shutil.ignore_patterns(
                                ".git", "__pycache__", "*.pyc", "evidence"))
            target = work / rel
            target.write_text(original.replace(old, new, 1), encoding="utf-8")
            code, out = _run(work)
            killed = code != 0
            print(f"[{'杀死' if killed else '!! 存活'}] {name}")
            if not killed:
                ok = False
                print(out[-1500:])
        # 还原后必须仍然全绿
        code2, _ = _run(ROOT)
        if code2 != 0:
            print(f"!! 还原后不绿：{name}")
            ok = False

    print()
    print("=== 变异自证", "全部通过" if ok else "有存活/异常", "===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
