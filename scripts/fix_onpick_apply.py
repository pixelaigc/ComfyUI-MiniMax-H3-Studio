#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 web/app.js 里做精确的行级替换（保留 CRLF 行尾）。

为什么不用 patch 工具：app.js 全文件是 CRLF，patch 的空白匹配会失配，
导致缩进被改坏（实测踩过）。这里按行号 + 精确文本双重校验。
"""
import io
import sys
from pathlib import Path

TARGET = Path(sys.argv[1])


def read_lines(path: Path):
    raw = path.read_bytes().decode("utf-8")
    return raw.split("\r\n"), raw


def write_lines(path: Path, lines: list[str]):
    path.write_bytes("\r\n".join(lines).encode("utf-8"))


def main():
    lines, raw = read_lines(TARGET)
    n = len(lines)

    # ---- 改动 1：openAssetPickerForSlot 增加 onPick 必填守卫 -------------
    # 定位函数体（按内容而不是行号，防止行号漂移）
    sig = "  function openAssetPickerForSlot(kind, onPick) {"
    idx = None
    for i, ln in enumerate(lines):
        if ln == sig:
            idx = i
            break
    if idx is None:
        print("!! 找不到 openAssetPickerForSlot 定义", file=sys.stderr)
        return 1

    old_head = [
        sig,
        "    if (!window.ASSETLIB || !window.ASSETLIB.openPicker) {",
    ]
    if lines[idx + 1] != old_head[1]:
        print(f"!! 第 {idx + 2} 行与预期不符: {lines[idx + 1]!r}", file=sys.stderr)
        return 1

    new_head = [
        sig,
        "    // 【2026-11-17 修复】onPick 是**必填**回调。",
        "    //",
        "    // 【原来错在哪】调用方漏传 onPick 时，这里不报错、照常打开弹窗，",
        "    // 用户选完素材点「确定使用」才在 onDone 里炸 TypeError；",
        "    // 而且那个 TypeError 被 .catch() 吞掉后弹出「读取素材失败：",
        "    // onPick is not a function」—— 提示指向「读取素材」，",
        "    // 真实失败点却是「回调没传」，排查方向被带偏。",
        "    //",
        "    // 【现在】缺回调就在**打开弹窗之前**拦下来（用户不会白选一遍），",
        "    // 并报明确错误。",
        "    if (typeof onPick !== \"function\") {",
        "      console.error(\"[素材库选择] 缺少 onPick 回调，无法打开选择器\", {kind});",
        "      alert(\"素材库选择器调用有误（缺少回调），请刷新页面重试。\");",
        "      return;",
        "    }",
        "    if (!window.ASSETLIB || !window.ASSETLIB.openPicker) {",
    ]
    lines[idx:idx + 2] = new_head

    # ---- 改动 2：点空槽位那条路径补上传回调 ------------------------------
    # 原来：openAssetPickerForSlot(kind);          <- 没有 onPick
    # 现在：openAssetPickerForSlot(kind, (asset) => fillFromAsset(asset));
    target = "      openAssetPickerForSlot(kind);"
    hits = [i for i, ln in enumerate(lines) if ln == target]
    if len(hits) != 1:
        print(f"!! 期望恰好 1 处 `openAssetPickerForSlot(kind);`，实际 {len(hits)} 处",
              file=sys.stderr)
        for h in hits:
            print("   行", h + 1, file=sys.stderr)
        return 1
    lines[hits[0]] = "      openAssetPickerForSlot(kind, (asset) => fillFromAsset(asset));"

    write_lines(TARGET, lines)
    print(f"OK 已写入 {TARGET}（{n} -> {len(lines)} 行，CRLF 保留）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
