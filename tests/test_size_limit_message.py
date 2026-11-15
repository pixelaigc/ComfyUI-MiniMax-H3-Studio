# -*- coding: utf-8 -*-
"""回归测试：尺寸超限的错误文案必须说**该模式的实际上限**。

【缺陷（2026-11-15 代码审查发现，Medium）】
``h3studio/video_models.py::VIDEO_MAX_PIXELS_MESSAGE`` 把
「文生视频」「2.0M」**写死**在文案里，但 ``normalize_video_size()``
是通用的 —— 首尾帧 / 参考生视频的实际上限是 **0.8MP**
（``max_pixels_for_mode("flf") == 0.8``）。

结果用户做首尾帧时看到的是：

    文生视频分辨率上限为 2.0M 像素（838,860 px）。当前请求 1,032,192 px…

三个信息互相矛盾：句首说 2.0M、括号里是 0.8MP 的值、还称「文生视频」
（用户在做首尾帧）。用户没法据此判断该把尺寸调小到多少。

【修法】上限值与模式名都从参数来（``max_pixels`` / 新增的 ``mode``）。

运行：
    python3 -m pytest tests/test_size_limit_message.py -q --tb=short
"""

import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import video_models as vm  # noqa: E402


class TestSizeLimitMessage(unittest.TestCase):
    """超限文案必须与实际模式/上限一致。"""

    def _message(self, mode):
        max_pixels = vm.max_pixels_for_mode(mode)
        _w, _h, reasons = vm.normalize_video_size(
            1344, 768, max_pixels=max_pixels, strict_over_limit=True, mode=mode)
        self.assertTrue(reasons, f"{mode} 的 1344x768 应当触发超限提示")
        return reasons[0], max_pixels

    def test_flf_message_states_flf_limit(self):
        msg, max_pixels = self._message("flf")
        self.assertIn("0.8M", msg,
                      f"首尾帧的文案没有写实际上限 0.8M：{msg}")
        self.assertNotIn("2.0M", msg,
                         f"首尾帧的文案里不该出现 2.0M：{msg}")
        self.assertIn("首尾帧", msg,
                      f"首尾帧的文案不该称「文生视频」：{msg}")

    def test_r2v_message_states_r2v_limit(self):
        msg, _ = self._message("r2v")
        self.assertIn("0.8M", msg, f"参考生视频的文案没写 0.8M：{msg}")
        self.assertIn("参考生视频", msg,
                      f"参考生视频的文案不该称「文生视频」：{msg}")

    def test_t2v_message_states_t2v_limit(self):
        """文生视频的上限是 2.0MP —— 用一个真超 2.0M 的尺寸。"""
        max_pixels = vm.max_pixels_for_mode("t2v")
        _w, _h, reasons = vm.normalize_video_size(
            2560, 1440, max_pixels=max_pixels, strict_over_limit=True, mode="t2v")
        self.assertTrue(reasons, "2560x1440 应当触发 t2v 超限提示")
        msg = reasons[0]
        # 上限是 2.0 MP —— 文案里的数字必须与 max_pixels 一致
        self.assertIn(f"{max_pixels:g}M", msg,
                      f"文生视频的文案 MP 数与实际上限 {max_pixels}MP 不符：{msg}")
        self.assertIn("文生视频", msg, f"文生视频的文案没说模式名：{msg}")

    def test_message_limit_number_matches_actual_limit(self):
        """【核心】文案里的 MP 数必须等于实际生效的上限。

        【为什么要这条】原来的 bug 正是「句首写的数」与「括号里的 px」
        对不上。这条直接断言两者一致。
        """
        for mode in ("flf", "r2v"):
            msg, max_pixels = self._message(mode)
            self.assertIn(f"{max_pixels:g}M", msg,
                          f"{mode} 的文案 MP 数与实际上限 {max_pixels}MP 不符：{msg}")
            # 括号里的 px 必须等于 上限 * 1024 * 1024
            import re
            m = re.search(r"（([\d,]+) px）", msg)
            self.assertIsNotNone(m, f"文案里没有 px 数：{msg}")
            self.assertEqual(int(max_pixels * 1024 * 1024),
                             int(m.group(1).replace(",", "")),
                             f"{mode} 的文案 px 数与实际上限不符：{msg}")

    def test_server_passes_mode_to_normalizer(self):
        """端到端：server.py 的调用点必须把 mode 传下去。

        【为什么单列】上面的用例是**直接调** normalize_video_size(mode=...)，
        测的是函数本身；但真正决定用户看到什么文案的是**调用点**有没有传 mode。
        调用点漏传 -> 函数再对也没用（文案退回「视频…0.8M」泛称）。
        """
        src = (REPO / "server.py").read_text(encoding="utf-8")
        # 【为什么不能用 `.*?` 跨行贪婪匹配】
        # 调用点中间夹着注释行，`(.*?)` 会在注释里遇到换行就停 ——
        # 断言会「看起来没传 mode」而实际传了（实测踩过）。
        # 这里按**括号配平**取整个调用表达式。
        idx = src.find("normalize_video_size(")
        self.assertNotEqual(-1, idx, "找不到 server.py 的 normalize_video_size 调用点")
        depth = 0
        j = src.index("(", idx)
        k = j
        while k < len(src):
            if src[k] == "(":
                depth += 1
            elif src[k] == ")":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        call_expr = src[j:k + 1]
        self.assertIn("mode=", call_expr,
                      "server.py 的 normalize_video_size 调用点没有传 mode —— "
                      "错误文案会退回泛称，用户看不到自己在哪个模式上超限")

    def test_missing_mode_does_not_crash(self):
        """旧调用方不传 mode 时不能崩（向后兼容）。"""
        _w, _h, reasons = vm.normalize_video_size(
            1344, 768, max_pixels=0.8, strict_over_limit=True)
        self.assertTrue(reasons)
        self.assertIn("0.8M", reasons[0])

    def test_all_limits_have_a_chinese_label(self):
        """每个有上限的模式都必须有中文名（否则文案会说「视频」这种泛称）。"""
        for mode in ("t2v", "flf", "r2v"):
            self.assertIn(mode, vm.VIDEO_MODE_LABELS,
                          f"{mode} 缺少中文名，错误文案会说得不准")
            self.assertTrue(vm.VIDEO_MODE_LABELS[mode].strip())


if __name__ == "__main__":
    unittest.main()
