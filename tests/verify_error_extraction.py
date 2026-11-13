# -*- coding: utf-8 -*-
"""【复现-修复对照】ComfyUI 执行错误的中文化（2026-11-12 修）。

这个脚本验证**错误提取链路**本身：

  修复前：worker 读 ComfyUI 的 `status.messages` 时把 `m[0]`（其实是字符串
          "execution_error"）当 dict 读 -> `.get()` 抛 AttributeError ->
          被兜底成 `str(e)` 存进 job.error ->
          前端显示 `'str' object has no attribute 'get'`。

  修复后：按 ComfyUI 的真实形状 `[事件名, 数据字典]` 提取 ->
          exception_message 转成**中文可读原因**（含真正原因）->
          技术细节（节点/异常类型/原文）只进服务端日志。

【怎么验证「真的进了 job.error」】
直接构造一份**真实的 ComfyUI error status**（从本机 /history 抄的），
喂给服务端的 `_comfy_error_message`，检查返回值：
  * 不是原始 Python 异常文本；
  * 是中文；
  * 带真正的原因。

用法：
    python3 tests/verify_error_extraction.py
"""

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import comfy_errors  # noqa: E402

# 本机 ComfyUI /history 的真实样本（2026-11-12 抄）
REAL_STATUS = {
    "status_str": "error",
    "completed": False,
    "messages": [
        ["execution_start", {"prompt_id": "af4df18b", "timestamp": 1794483000000}],
        ["execution_error", {
            "prompt_id": "af4df18b-ef2",
            "node_id": "300:bridge",
            "node_type": "H3RelayCopyBridge",
            "executed": ["14", "15", "16"],
            "exception_message": "拷贝桥禁止跨分辨率：上段 768x448 ≠ 本段 1024x576\n",
            "exception_type": "ValueError",
            "traceback": ["  File \"relay_core.py\", line 2809, in copy_bridge"],
            "current_inputs": {},
            "current_outputs": [],
            "timestamp": 1794483000000,
        }],
    ],
}


def main():
    out_dir = REPO / "evidence" / "continuation-mixed-state"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 服务端的包装函数（与 worker 用的是同一个）
    import importlib
    server = importlib.import_module("server")

    # 捕获它 print 到 stdout 的日志细节
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        reason = server._comfy_error_message(REAL_STATUS)
    log_output = buf.getvalue()

    result = {
        "user_visible_reason": reason,
        "server_log_detail": log_output.strip(),
        "raw_python_leak": any(token in reason for token in (
            "'str' object has no attribute 'get'",
            "object has no attribute",
            "AttributeError",
            "Traceback (most recent call last)",
        )),
        "is_chinese": any("一" <= ch <= "鿿" for ch in reason),
        "has_real_cause": ("分辨率" in reason and "768" in reason),
        "detail_in_log": ("H3RelayCopyBridge" in log_output
                          and "ValueError" in log_output),
    }
    result["verdict_pass"] = bool(
        not result["raw_python_leak"]
        and result["is_chinese"]
        and result["has_real_cause"]
        and result["detail_in_log"])

    print("=== 用户可见文案 ===")
    print(reason)
    print("\n=== 服务端日志细节 ===")
    print(result["server_log_detail"])
    print("\n=== 断言 ===")
    for key in ("raw_python_leak", "is_chinese", "has_real_cause",
                "detail_in_log"):
        print(f"  {key}: {result[key]}")
    print(f"\n结论: {'通过 ✅' if result['verdict_pass'] else '未通过 ❌'}")

    out = out_dir / "error_extraction_report.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"报告: {out}")
    return 0 if result["verdict_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
