# -*- coding: utf-8 -*-
"""ComfyUI 执行错误的提取与中文化 —— 回归测试。

【这个测试在防什么】

2026-11-12 独立复验发现：接续后再点「复用参数」造成混合态，提交后
任务失败，生成库卡片显示：

    'str' object has no attribute 'get'

那是**原始 Python 异常文本**，用户完全看不懂。真正的��因其实存在
（Relay-Kit 拷贝桥拒绝跨分辨率接续），但被错误提取的缺陷吞掉了。

两条红线：
  1. **不得**再出现原始 Python 异常文本（`'str' object has no
     attribute 'get'` / `AttributeError` / `Traceback`）；
  2. 用户可见的中文原因里**必须带真正的原因**（不能只说「生成失败」）。

运行：python3 -m pytest tests/test_comfy_errors.py -v
"""

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import comfy_errors  # noqa: E402


# ------------------------------------------------------------------
# 真实数据样本（取自本机 ComfyUI /history，2026-11-12）
# ------------------------------------------------------------------
#
# 【为什么要用真实样本】ComfyUI 的 status.messages 形状是
# `[事件名, 数据字典]` —— 这个形状是猜错就会全盘皆错的地方。
# 下面的样本是从本机 ComfyUI 的 /history 里**原样抄**的。

REAL_COPY_BRIDGE_ERROR = {
    "prompt_id": "af4df18b-ef2",
    "node_id": "300:bridge",
    "node_type": "H3RelayCopyBridge",
    "executed": ["14", "15", "16"],
    "exception_message": "拷贝桥禁止跨分辨率：上段 768x448 ≠ 本段 1024x576\n",
    "exception_type": "ValueError",
    "traceback": ["  File \"...\", line 2809, in copy_bridge"],
    "current_inputs": {},
    "current_outputs": [],
    "timestamp": 1794483000000,
}

REAL_SAVE_AUDIO_ERROR = {
    "prompt_id": "435ea8af-a5a",
    "node_id": "300:save",
    "node_type": "SaveAudioAdvanced",
    "executed": [],
    "exception_message": "SaveAudioAdvanced.execute() missing 1 required positional argument: 'format'\n",
    "exception_type": "TypeError",
    "traceback": [],
    "current_inputs": {},
    "current_outputs": [],
    "timestamp": 1794482000000,
}


def _status(messages):
    return {"status_str": "error", "completed": False, "messages": messages}


class TestComfyErrorExtraction(unittest.TestCase):
    """从 ComfyUI 的 status 结构里提取错误。"""

    def test_extracts_real_two_element_message_shape(self):
        """真实形状 [事件名, 数据字典] 必须能提取出异常文本。"""
        result = comfy_errors.extract_error(_status([
            ["execution_start", {"prompt_id": "x", "timestamp": 1}],
            ["execution_error", REAL_COPY_BRIDGE_ERROR],
        ]))
        self.assertTrue(result["found"])
        self.assertEqual(result["node_type"], "H3RelayCopyBridge")
        self.assertEqual(result["exception_type"], "ValueError")
        self.assertIn("禁止跨分辨率", result["raw"])

    def test_never_raises_on_real_shape(self):
        """【红线】提取过程**不得**抛异常。

        原缺陷就是这里抛的 AttributeError（把 m[0] 当 dict 读）。
        """
        for payload in (REAL_COPY_BRIDGE_ERROR, REAL_SAVE_AUDIO_ERROR):
            with self.subTest(node=payload["node_type"]):
                try:
                    comfy_errors.extract_error(_status([["execution_error", payload]]))
                except Exception as exc:  # pragma: no cover - 红了就是缺陷
                    self.fail(f"提取时抛了异常: {type(exc).__name__}: {exc}")

    def test_reason_never_contains_raw_python_exception(self):
        """【红线】用户可见原因里不得出现原始 Python 异常文本。"""
        forbidden = [
            "'str' object has no attribute 'get'",
            "AttributeError",
            "Traceback (most recent call last)",
            "object has no attribute",
        ]
        status = _status([["execution_error", REAL_COPY_BRIDGE_ERROR]])
        reason = comfy_errors.extract_error(status)["reason"]
        for token in forbidden:
            self.assertNotIn(token, reason,
                             f"用户可见原因里出现了原始 Python 异常文本: {token!r}\n{reason}")

    def test_reason_carries_real_cause(self):
        """用户可见原因必须带**真正的原因**，不能只说「生成失败」。"""
        reason = comfy_errors.extract_error(
            _status([["execution_error", REAL_COPY_BRIDGE_ERROR]]))["reason"]
        self.assertIn("分辨率", reason)
        self.assertIn("768×448", reason)
        self.assertIn("1024×576", reason)
        # 必须给出可执行的下一步
        self.assertTrue("复用参数" in reason or "尺寸" in reason)

    def test_detail_keeps_technical_information(self):
        """技术细节必须保留（供后端日志），含节点/异常类型/原文。"""
        result = comfy_errors.extract_error(
            _status([["execution_error", REAL_COPY_BRIDGE_ERROR]]))
        detail = result["detail"]
        self.assertIn("H3RelayCopyBridge", detail)
        self.assertIn("ValueError", detail)
        self.assertIn("禁止跨分辨率", detail)

    def test_ignores_non_error_events(self):
        """非 execution_error 的消息不得被当成错误。"""
        status = _status([
            ["execution_start", {"prompt_id": "x"}],
            ["execution_cached", {"nodes": ["1"], "prompt_id": "x"}],
        ])
        result = comfy_errors.extract_error(status)
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "")

    def test_handles_missing_and_malformed_input(self):
        """缺字段 / 空结构 / 畸形条目都不得抛异常。"""
        for status in (None, {}, {"messages": []}, {"messages": [None]},
                       {"messages": [["execution_error"]]},
                       {"messages": ["not-a-list"]},
                       {"messages": [["execution_error", "not-a-dict"]]}):
            with self.subTest(status=status):
                result = comfy_errors.extract_error(status)
                self.assertIsInstance(result, dict)
                self.assertIn("reason", result)

    def test_accepts_legacy_single_dict_shape(self):
        """兼容另一种形状：[数据字典]（数据在 m[0]）。

        不能假设固定下标 —— 否则同样的 bug 会在另一种形状下再来一次。
        """
        status = _status([[REAL_COPY_BRIDGE_ERROR]])
        result = comfy_errors.extract_error(status)
        self.assertTrue(result["found"])
        self.assertEqual(result["node_type"], "H3RelayCopyBridge")

    def test_takes_first_error_as_root_cause(self):
        """多条错误时取第一条（根因），不是拼接。"""
        second = dict(REAL_SAVE_AUDIO_ERROR)
        status = _status([
            ["execution_error", REAL_COPY_BRIDGE_ERROR],
            ["execution_error", second],
        ])
        result = comfy_errors.extract_error(status)
        self.assertEqual(result["node_type"], "H3RelayCopyBridge")

    def test_error_from_history_entry(self):
        """从 /history 的条目（含 status 子字典）里提取。"""
        history = {
            "prompt": [],
            "outputs": {},
            "status": _status([["execution_error", REAL_COPY_BRIDGE_ERROR]]),
        }
        result = comfy_errors.error_from_history(history)
        self.assertTrue(result["found"])
        self.assertIn("分辨率", result["reason"])


class TestComfyErrorChinese(unittest.TestCase):
    """中文文案的规则映射。"""

    def test_save_audio_mismatch_is_chinese(self):
        """SaveAudioAdvanced 的接口不匹配要有专门的中文说明。"""
        reason = comfy_errors.describe_exception(
            "SaveAudioAdvanced", "TypeError",
            REAL_SAVE_AUDIO_ERROR["exception_message"])["reason"]
        self.assertIn("音频", reason)
        self.assertNotIn("Traceback", reason)

    def test_oom_is_chinese(self):
        reason = comfy_errors.describe_exception(
            "", "RuntimeError", "CUDA out of memory. Tried to allocate 2.00 GiB")["reason"]
        self.assertIn("显存", reason)

    def test_unknown_error_keeps_original_cause(self):
        """认不出来的错误不能编造原因，但必须带上原始原因。"""
        reason = comfy_errors.describe_exception(
            "MyCustomNode", "SomeError", "something very specific went wrong")["reason"]
        self.assertIn("something very specific went wrong", reason)
        self.assertIn("MyCustomNode", reason)
        # 也要指明详细信息在日志里
        self.assertIn("日志", reason)

    def test_empty_message_does_not_crash(self):
        result = comfy_errors.describe_exception("", "", "")
        self.assertTrue(result["reason"])
        self.assertTrue(result["detail"] is not None)

    def test_multiline_message_uses_first_line(self):
        """多行异常只取第一行（真正的异常原因）。"""
        reason = comfy_errors.describe_exception(
            "N", "ValueError", "第一行原因\n  File \"x\", line 1\n  File \"y\", line 2")["reason"]
        self.assertIn("第一行原因", reason)
        self.assertNotIn("line 1", reason)


class TestSizeMismatchRule(unittest.TestCase):
    """接续的几何约束：分辨率必须一致。"""

    def test_mismatch_gives_chinese_actionable_reason(self):
        from h3studio import continuation as cont
        reason = cont.size_mismatch(
            {"width": 768, "height": 448},
            {"width": 1024, "height": 576})
        self.assertTrue(reason)
        self.assertIn("768×448", reason)
        self.assertIn("1024×576", reason)
        self.assertIn("复用参数", reason)

    def test_same_size_passes(self):
        from h3studio import continuation as cont
        self.assertEqual(cont.size_mismatch(
            {"width": 768, "height": 448},
            {"width": 768, "height": 448}), "")

    def test_unknown_size_passes(self):
        """无从比较就放行 —— 不臆造约束。"""
        from h3studio import continuation as cont
        self.assertEqual(cont.size_mismatch({}, {"width": 1024, "height": 576}), "")
        self.assertEqual(cont.size_mismatch({"width": 768, "height": 448}, {}), "")

    def test_non_numeric_size_passes(self):
        from h3studio import continuation as cont
        self.assertEqual(cont.size_mismatch(
            {"width": "abc", "height": None},
            {"width": 1024, "height": 576}), "")


class TestComfyErrorsModuleContract(unittest.TestCase):
    """模块契约：纯函数、无 IO、消息形状常量与本机 ComfyUI 一致。"""

    def test_no_network_or_disk_imports(self):
        """必须是纯函数模块（与 job_meta.py 同一纪律）。"""
        source = (REPO / "h3studio" / "comfy_errors.py").read_text(encoding="utf-8")
        for banned in ("import requests", "import aiohttp", "urllib",
                       "open(", "Path("):
            self.assertNotIn(banned, source,
                             f"comfy_errors.py 不该有 IO/网络：{banned}")

    def test_event_name_constant(self):
        self.assertEqual(comfy_errors.EVENT_EXECUTION_ERROR, "execution_error")
        self.assertEqual(comfy_errors.MESSAGE_EVENT_INDEX, 0)
        self.assertEqual(comfy_errors.MESSAGE_DATA_INDEX, 1)

    def test_real_sample_still_parses_after_refactor(self):
        """用真实样本做一次端到端解析（防回归）。"""
        raw = json.dumps({
            "status": _status([["execution_error", REAL_COPY_BRIDGE_ERROR]])
        }, ensure_ascii=False)
        status = json.loads(raw)["status"]
        result = comfy_errors.extract_error(status)
        self.assertTrue(result["found"])
        self.assertIn("分辨率", result["reason"])


if __name__ == "__main__":
    unittest.main()
