#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看门狗第 8 轮 —— api_create Content-Type 校验的**变异测试**。

【为什么必须做变异测试】
「8 个用例全绿」本身不能证明用例有效。一种常见的假绿：
把校验代码删掉 / 改坏，用例仍然通过（因为断言写得太松，
或者根本没走到那段代码）。只有**故意改坏代码后用例真的变红**，
才能证明这些用例真的在守这条行为。

每个变异做三步：
  1. 备份原文件
  2. 用精确 pattern 改坏代码（**改完必须校验真的改了** ——
     pattern 没匹配上就是「假阴性」，会得出「变异存活」的错结论）
  3. 跑目标测试文件 -> 期望 FAILED
  4. 还原原文件（并校验还原后与备份逐字节一致）

用法：
    python3 scripts/mutation_check_apicreate_ct.py
退出码 0 = 全部变异都被咬住（好）；非 0 = 有变异存活（坏）。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SERVER = BASE / "server.py"
TEST = "tests/test_apicreate_content_type.py"
PY = "/home/ubuntu/comfyui/venv/bin/python"

# (编号, 描述, 原文片段, 替换片段)
MUTATIONS = [
    (
        "MUT1",
        "删掉整段 Content-Type 校验（恢复成缺陷状态：直接 multipart()）",
        '''    ctype = (request.headers.get("Content-Type") or "").strip()
    if not ctype:
        return web.json_response(
            {"error": "缺少 Content-Type 请求头：本接口只接受 "
                      "multipart/form-data（提交生成任务要带文件流）",
             "code": "content_type_required"},
            status=415)''',
        '''    ctype = (request.headers.get("Content-Type") or "").strip()
    if False:
        return web.json_response(
            {"error": "x", "code": "content_type_required"},
            status=415)''',
    ),
    (
        "MUT2",
        "把「不是 multipart/* 就拒」的条件反转（改成只拒 multipart）",
        '''    if not main_ctype.startswith("multipart/"):''',
        '''    if main_ctype.startswith("multipart/"):''',
    ),
    (
        "MUT3",
        "把 415 改成 200（错误码对了但状态码变成成功 —— 假成功）",
        '''             "code": "unsupported_media_type"},
            status=415)''',
        '''             "code": "unsupported_media_type"},
            status=200)''',
    ),
    (
        "MUT4",
        "把 boundary 检查去掉（multipart 无 boundary -> 又变回 500）",
        '''    if main_ctype == "multipart/form-data" and "boundary=" not in ctype.lower():''',
        '''    if False:''',
    ),
    (
        "MUT5",
        "把校验**真正挪到** request.multipart() 之后（顺序反了 = 形同虚设）",
        '''    ctype = (request.headers.get("Content-Type") or "").strip()
    if not ctype:
        return web.json_response(
            {"error": "缺少 Content-Type 请求头：本接口只接受 "
                      "multipart/form-data（提交生成任务要带文件流）",
             "code": "content_type_required"},
            status=415)
    main_ctype = ctype.split(";", 1)[0].strip().lower()
    if not main_ctype.startswith("multipart/"):
        return web.json_response(
            {"error": f"不支持的 Content-Type：{main_ctype or '(空)'}；"
                      "本接口只接受 multipart/form-data",
             "code": "unsupported_media_type"},
            status=415)
    # multipart/* 但没带 boundary -> aiohttp 会抛 ValueError -> 500。
    # 这里自己先查掉，避免把客户端错误伪装成服务端错误。
    if main_ctype == "multipart/form-data" and "boundary=" not in ctype.lower():
        return web.json_response(
            {"error": "Content-Type 缺少 boundary 参数："
                      "multipart/form-data 必须带 boundary",
             "code": "content_type_invalid"},
            status=400)

    reader = await request.multipart()
    mode = None''',
        '''    reader = await request.multipart()
    ctype = (request.headers.get("Content-Type") or "").strip()
    if not ctype:
        return web.json_response(
            {"error": "x", "code": "content_type_required"},
            status=415)
    mode = None''',
    ),
]


def run_tests() -> tuple[int, str]:
    proc = subprocess.run(
        [PY, "-m", "pytest", TEST, "-q", "--tb=no", "-p", "no:cacheprovider"],
        cwd=str(BASE), capture_output=True, text=True, timeout=600)
    return proc.returncode, (proc.stdout + proc.stderr)


def main() -> int:
    original = SERVER.read_bytes()
    backup = Path(tempfile.mkdtemp()) / "server.py.orig"
    backup.write_bytes(original)
    survivors = []

    try:
        for mid, desc, old, new in MUTATIONS:
            text = original.decode("utf-8")
            if old not in text:
                print(f"[{mid}] !! pattern 没匹配上 —— 变异没真正改到代码"
                      f"（假阴性，不能据此下结论）\n     描述: {desc}")
                survivors.append(mid)
                continue
            mutated = text.replace(old, new, 1)
            SERVER.write_text(mutated, encoding="utf-8")
            # 确认真的改了（避免「写回去了但内容一样」的假变异）
            if SERVER.read_bytes() == original:
                print(f"[{mid}] !! 写入后内容与原文相同 —— 变异无效")
                survivors.append(mid)
                continue
            code, out = run_tests()
            if code == 0:
                print(f"[{mid}] !! 变异**存活**（测试仍全绿 = 用例没咬住）")
                print(f"     描述: {desc}")
                survivors.append(mid)
            else:
                print(f"[{mid}] OK 被咬住（pytest 退出码 {code}）")
                print(f"     描述: {desc}")
            # 还原
            SERVER.write_bytes(original)
            if SERVER.read_bytes() != original:
                print(f"[{mid}] !! 还原失败 —— 中止")
                return 2
    finally:
        SERVER.write_bytes(original)
        shutil.rmtree(backup.parent, ignore_errors=True)

    print()
    print(f"=== 变异测试结果：{len(MUTATIONS) - len(survivors)}/"
          f"{len(MUTATIONS)} 被咬住 ===")
    if survivors:
        print("存活的变异（用例没咬住）：", ", ".join(survivors))
        return 1
    print("全部变异都被咬住 —— 这些用例真的在守这条行为。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
