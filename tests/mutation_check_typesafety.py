# -*- coding: utf-8 -*-
"""变异验证：证明 tests/test_comfy_errors_typesafety.py 的断言**真的能抓住 bug**。

【为什么需要这一层】
「测试全绿」本身不证明测试有用 —— 断言写错位置、变异本身是空操作，
全绿同样会发生。每个变异都要：还原 -> 改一处 -> 跑 -> 期望变红 -> 还原。

【本文件的特殊之处】
`server.py` 与 `web/app.js` 在本仓库是 **CRLF 行尾**。
变异脚本按**字节**改（CRLF-aware），否则「原文没找到」会被误当成
「变异没生效」。

用法：python3 tests/mutation_check_typesafety.py
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET = "tests/test_comfy_errors_typesafety.py"
PY = sys.executable

#: (变异名, 文件, 原文, 新文, 期望变红的断言数下限)
#: 【等价变异（equivalent mutant）说明】
#:
#: 有些「把类型守卫去掉」的变异**测不出来** —— 因为代码里有**冗余的
#: 纵深防御**（`_history_status` 归一 + `coerce_status_info` 归一 +
#: `_iter_error_messages` 里的 isinstance 守卫，三层都挡）。
#: 去掉其中一层，**行为完全不变** —— 这类变异**任何行为测试都抓不到**。
#:
#: 处理原则：**不硬凑阈值**，而是把这类变异换成「真有行为差异」的变异
#: （T4 / T4b），并在文档里如实说明「纵深防御是冗余的」。
#:
#: 判断依据：变异后跑一遍**全部**目标测试，0 条变红 -> 再手工确认
#: 「行为确实等价」-> 判定为等价变异，换掉它。

MUTATIONS = [
    (
        "T1 【原始缺陷①·完整复原】status 既不归一也不判类型",
        "server.py",
        '    status_info = comfy_errors.coerce_status_info(raw_status)\r\n'
        '    return status_info, comfy_errors.status_is_error(status_info), malformed\r\n',
        '    status_info = raw_status or {}\r\n'
        '    return status_info, status_info.get("status_str") == "error", False\r\n',
        3,
    ),
    (
        "T2 畸形 status 的**检测**失效（回归：worker 会无限轮询挂死）",
        "server.py",
        '    malformed = raw_status is not None and not isinstance(raw_status, dict)\r\n',
        '    malformed = False\r\n',
        1,
    ),
    (
        "T2b 畸形 status 不再快速失败（回归：会无限轮询挂死）",
        "server.py",
        '                            if malformed:\r\n'
        '                                # 形状超预期 -> **快速失败**，别挂死。\r\n'
        '                                raise RuntimeError(comfy_errors.MALFORMED_STATUS_REASON)\r\n',
        '                            if False:\r\n'
        '                                raise RuntimeError(comfy_errors.MALFORMED_STATUS_REASON)\r\n',
        1,
    ),
    (
        "T3 coerce_status_info 不做类型判断（str 原样返回）",
        "h3studio/comfy_errors.py",
        "    if isinstance(status_info, dict):\n"
        "        return status_info\n"
        "    return {}\n",
        "    return status_info if status_info is not None else {}\n",
        3,
    ),
    (
        "T4 已中文化的 RuntimeError 也被洗成通用文案（信息丢失）",
        "server.py",
        '    if isinstance(exc, RuntimeError) and text and _looks_chinese(text):\r\n'
        '        return text[:800]\r\n',
        '    if False:\r\n'
        '        return text[:800]\r\n',
        1,
    ),
    (
        "T4b 兜底文案里又出现原始异常文本",
        "h3studio/comfy_errors.py",
        'UNPARSED_ERROR_REASON = ("生成失败，但没能取到具体原因（ComfyUI 侧未返回"\n'
        '                          "可解析的错误信息）。详细信息见服务端日志。")',
        'UNPARSED_ERROR_REASON = "ComfyUI 执行出错"',
        1,
    ),
    (
        "T5 worker 兜底又写回原始异常文本（原始缺陷②）",
        "server.py",
        "                             (_user_facing_error(e), time.time(), job_id))",
        "                             (str(e)[:800], time.time(), job_id))",
        1,
    ),
    (
        "T6 _user_facing_error 原样返回异常文本（原始缺陷②）",
        "server.py",
        '    if isinstance(exc, RuntimeError) and text and _looks_chinese(text):\r\n'
        '        return text[:800]\r\n',
        '    if isinstance(exc, RuntimeError) and text and _looks_chinese(text):\r\n'
        '        return text[:800]\r\n'
        '    return text[:800]\r\n',
        2,
    ),
    (
        "T7 前端不再用 friendlyError（原始缺陷②）",
        "web/app.js",
        'friendlyError(job.error)',
        'job.error',
        1,
    ),
    (
        "T8 前端分类器放行 Python 异常文本",
        "web/app.js",
        "  if (!looksLikePythonTraceback(raw)) return raw;\r\n",
        "  return raw;\r\n",
        1,
    ),
    (
        "T9 worker 兜底写回 str(e)（原始缺陷②·调用点）",
        "server.py",
        "                             (_user_facing_error(e), time.time(), job_id))",
        "                             (str(e)[:800], time.time(), job_id))",
        1,
    ),
]


def run_tests() -> int:
    """跑目标测试，返回失败数。"""
    proc = subprocess.run(
        [PY, "-m", "pytest", TARGET, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(REPO), capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    failed = sum(1 for line in out.splitlines() if line.startswith("FAILED"))
    if proc.returncode != 0 and failed == 0:
        failed = 1
    return failed


def main() -> int:
    results = []
    all_caught = True
    for name, rel, old, new, expect_min in MUTATIONS:
        path = REPO / rel
        original = path.read_bytes()
        old_b = old.encode("utf-8")
        new_b = new.encode("utf-8")
        if old_b not in original:
            print(f"❌ 变异「{name}」定位失败：原文没找到（{rel}）")
            results.append((name, 0, expect_min, "LOCATE-FAIL"))
            all_caught = False
            continue
        try:
            path.write_bytes(original.replace(old_b, new_b, 1))
            failed = run_tests()
        finally:
            path.write_bytes(original)   # 还原（字节级，CRLF 不动）

        ok = failed >= expect_min
        all_caught = all_caught and ok
        results.append((name, failed, expect_min, "OK" if ok else "MISSED"))
        flag = "✅" if ok else "❌"
        print(f"{flag} {name}: 变红 {failed} 条（期望 >= {expect_min}）")

    print()
    print(f"合计：{len(results)} 项变异，全被抓住={all_caught}")
    return 0 if all_caught else 1


if __name__ == "__main__":
    sys.exit(main())
