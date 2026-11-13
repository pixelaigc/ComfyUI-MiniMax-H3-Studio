# -*- coding: utf-8 -*-
"""变异验证：证明 tests/test_continuation_retry.py 的断言**真的能抓住 bug**。

【为什么需要这一层】
「测试全绿」本身不证明测试有用 —— 如果断言写错了位置、或者变异本身
是空操作，全绿同样会发生。所以每个变异都要：
  1. 先**还原**工作树（否则前一个变异会污染后一个）；
  2. 改一处（变异）；
  3. 跑目标测试，期望**至少一条断言变红**；
  4. 还原。

【断言纪律】
  * 变异必须**真的改变行为**（否则「0 条变红」是正确结果，要怀疑变异本身）；
  * 期望值是字面量，不从被测函数反推。

用法：python3 tests/mutation_check_retry.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET = "tests/test_continuation_retry.py"
PY = sys.executable

#: (变异名, 文件, 原文, 新文, 期望变红的断言数下限)
MUTATIONS = [
    (
        "A1 落盘目录退回 setdefault（重试沿用源任务目录）",
        "server.py",
        '            info["latent_run_id"] = cont.segment_run_id(job_id)\n'
        '            info["latent_stage"] = 0',
        '            info.setdefault("latent_run_id", cont.segment_run_id(job_id))\n'
        '            info.setdefault("latent_stage", 0)',
        2,
    ),
    (
        "A2 落盘目录完全用源任务传来的（不重算）",
        "server.py",
        '            info["latent_run_id"] = cont.segment_run_id(job_id)\n'
        '            info["latent_stage"] = 0',
        '            info.setdefault("latent_run_id", "")\n'
        '            info.setdefault("latent_stage", 0)',
        2,
    ),
    (
        "B1 读回路径不带指纹（退回固定路径）",
        "h3studio/continuation_service.py",
        "    source_path = str(ensure_fingerprinted_copy(source_run_id,\n"
        "                                                    source_latent_stage))",
        "    source_path = str(stage_path(source_run_id, source_latent_stage))",
        1,
    ),
    (
        "B2 指纹只取文件大小（内容变了指纹不变）",
        "h3studio/continuation_service.py",
        "    h = hashlib.md5()\n"
        "    h.update(str(size).encode())\n"
        "    try:\n"
        "        with open(p, \"rb\") as f:\n"
        "            h.update(f.read(8 * 1024 * 1024))\n"
        "            if size > 16 * 1024 * 1024:\n"
        "                f.seek(-8 * 1024 * 1024, os.SEEK_END)\n"
        "                h.update(f.read(8 * 1024 * 1024))\n"
        "            else:\n"
        "                f.seek(size // 2)\n"
        "                h.update(f.read(8 * 1024 * 1024))",
        "    h = hashlib.md5()\n"
        "    h.update(str(size).encode())\n"
        "    try:\n"
        "        with open(p, \"rb\") as f:\n"
        "            pass",
        4,
    ),
    (
        "B3 副本内容被截断（读回的是空壳）",
        "h3studio/continuation_service.py",
        "        try:\n"
        "            if dst.exists():\n"
        "                dst.unlink()\n"
        "            os.link(src, dst)\n"
        "        except OSError:\n"
        "            shutil.copy2(src, dst)",
        "        try:\n"
        "            if dst.exists():\n"
        "                dst.unlink()\n"
        "            dst.write_bytes(src.read_bytes()[:16])\n"
        "        except OSError:\n"
        "            shutil.copy2(src, dst)",
        1,
    ),
    (
        "B4 清理把当前指纹副本也删掉",
        "h3studio/continuation_service.py",
        "        if candidate.name == keep:\n"
        "            continue",
        "        if False:\n"
        "            continue",
        1,
    ),
    (
        "B5 副本不幂等（每次生成新名字）",
        "h3studio/continuation_service.py",
        '    dst = src.with_name(f"{src.stem}_{fp}{src.suffix}")\n'
        "    try:\n"
        "        if dst.is_file() and dst.stat().st_size == src.stat().st_size:\n"
        "            return dst",
        '    dst = src.with_name(f"{src.stem}_{fp}_{os.getpid()}{src.suffix}")\n'
        "    try:\n"
        "        if False:\n"
        "            return dst",
        2,
    ),
]


def run_tests() -> tuple[int, int]:
    """跑目标测试，返回 (失败数, 总数)。"""
    proc = subprocess.run(
        [PY, "-m", "pytest", TARGET, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(REPO), capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    failed = 0
    total = 0
    for line in out.splitlines():
        if " passed" in line or " failed" in line:
            for part in line.replace(",", " ").split():
                if part.isdigit():
                    total += int(part)
        if line.startswith("FAILED") or " error" in line:
            failed += 1
    if "failed" in out:
        for line in out.splitlines():
            if "failed" in line and "," in line:
                for part in line.replace(",", " ").split():
                    if part.isdigit():
                        failed = max(failed, int(part))
                break
    if proc.returncode != 0 and failed == 0:
        failed = 1
    return failed, total


def main() -> int:
    results = []
    all_caught = True
    for name, rel, old, new, expect_min in MUTATIONS:
        path = REPO / rel
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print(f"❌ 变异「{name}」定位失败：原文没找到（{rel}）")
            results.append((name, 0, expect_min, "LOCATE-FAIL"))
            all_caught = False
            continue
        try:
            path.write_text(original.replace(old, new, 1), encoding="utf-8")
            failed, total = run_tests()
        finally:
            path.write_text(original, encoding="utf-8")   # 还原

        ok = failed >= expect_min
        all_caught = all_caught and ok
        results.append((name, failed, expect_min, "OK" if ok else "MISSED"))
        flag = "✅" if ok else "❌"
        print(f"{flag} {name}: 变红 {failed} 条（期望 >= {expect_min}）")

    print()
    print(f"合计：{len(results)} 项变异，"
          f"全被抓住={all_caught}")
    return 0 if all_caught else 1


if __name__ == "__main__":
    sys.exit(main())
