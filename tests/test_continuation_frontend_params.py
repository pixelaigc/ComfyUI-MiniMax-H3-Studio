# -*- coding: utf-8 -*-
"""第 1 项：flf / r2v 面板发起接续时必须提交 `params.continuation`。

【这个测试在防什么】
接续的**前端**与**后端**本来就都支持 flf / r2v：
  * 后端 `server.py` 只对 `mode in ("t2v", "flf", "r2v")` 消费
    `params["continuation"]`（`_prepare_continuation_params`）；
  * 后端 `h3studio/h3_video_modes.py::build_flf / build_r2v` 都读
    `vworkflow._continuation_state(params)`，接续链路完整。
但前端 `web/app.js` 里那句 `params.continuation = ctx.continuation;`
**只写在 t2v 分支**里 —— flf / r2v 分支从不设它。

⇒ 用户在界面上点「接续」→ 面板正确回填（模式 / 素材 / 音轨 / 尺寸）
→ 点生成 → 提交体里**没有 continuation** → 服务端看不到接续 →
本段退化成**普通生成**（不读回上游潜空间、不接拷贝桥）。

**这个失败模式比「报错」更糟**：它**不报错**。界面照常显示
「接续上下文」，生成也成功，产出却是一段与上一段毫无关系的独立视频。
用户以为在续写，实际拿到两段互不相干的片段。

【为什么用静态断言而不是浏览器 E2E】
浏览器 E2E（tests/test_continuation_frontend.py）能覆盖**交互**，
但「提交体里有没有 continuation」这一步的断言写在
`test_continue_submit_payload_carries_continuation` —— 那条用的是
**t2v** 模式（`_seed_continuable_job_as` 默认 t2v）⇒ 它**抓不到**
本缺陷。本文件补上 flf / r2v 两条，并把「三种模式走同一份逻辑」
这件事本身钉住。

运行：python3 -m pytest tests/test_continuation_frontend_params.py -v
"""
from __future__ import annotations

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

APP_JS = os.path.join(ROOT, "web", "app.js")
SERVER_PY = os.path.join(ROOT, "server.py")
VIDEO_MODES_PY = os.path.join(ROOT, "h3studio", "h3_video_modes.py")
CONTINUATION_PY = os.path.join(ROOT, "h3studio", "continuation.py")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _strip_js_comments(source: str) -> str:
    """去掉 JS 的行注释与块注释，只留**代码**。

    【为什么要这个辅助函数】源码级断言必须看代码而不是注释 ——
    注释里写 `params.continuation = ctx.continuation;` 只是**解释**
    「原来错在哪」，不是赋值。直接对原文 grep 会把注释当成代码，
    断言就永远红（而且是**假红**）。
    （与 tests/test_continuation_mixed_state.py 同一套做法。）
    """
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"//[^\n]*", "", without_block)


class TestContinuationSubmittedForAllVideoModes(unittest.TestCase):
    """C1：flf / r2v 的接续提交必须带 continuation。"""

    def setUp(self):
        self.js = _read(APP_JS)
        # 源码级断言必须看**代码**：注释里出现
        # `params.continuation = ctx.continuation;` 只是解释历史缺陷。
        self.code = _strip_js_comments(self.js)

    # ------------------------------------------------------------------
    def test_video_modes_share_one_continuation_submission(self):
        """接续上下文的提交必须是**模式无关**的一段公共逻辑。

        判据：`params.continuation = ...` 的赋值点必须在一个
        **带模式白名单**的条件里，且白名单覆盖 t2v / flf / r2v 三个。
        """
        code = self.code
        assigns = [m.start() for m in
                   re.finditer(r"params\.continuation\s*=\s*_contCtx\.continuation",
                               code)]
        self.assertTrue(
            assigns,
            "app.js 里找不到接续上下文的赋值点"
            "（应形如 params.continuation = _contCtx.continuation;）")
        for pos in assigns:
            window = code[max(0, pos - 2500):pos]
            for mode in ("t2v", "flf", "r2v"):
                self.assertIn(
                    f'mode === "{mode}"', window,
                    f"接续赋值点必须在覆盖 {mode} 的模式白名单里")
            self.assertRegex(
                window, r"_isVideoMode\s*=",
                "接续赋值点必须由一个显式的模式白名单变量驱动")

    def test_flf_and_r2v_branches_do_not_lose_continuation(self):
        """flf / r2v 分支**不得**再各自设 / 清 params.continuation。

        【为什么要这条】有两种「假修」：
          (a) 只在 flf / r2v 分支里加一句赋值 —— 那么 t2v 反而丢了；
          (b) 在公共作用域赋值，但某个分支里有 `delete params.continuation`
              或 `params.continuation = null` —— 公共作用域的赋值会被
              覆盖/清掉（赋值顺序在分支之后，但 delete 在之后）。
        ⇒ 三个分支里都不得出现对 params.continuation 的写操作。
        """
        code = self.code
        flf_at = code.find('} else if (mode === "flf" || mode === "r2v") {')
        self.assertGreater(flf_at, 0, "找不到 flf/r2v 分支")
        # 分支起点（t2v 分支）到公共作用域起点
        t2v_at = code.find('if (mode === "t2v" && window.VIDEO_PANEL')
        self.assertGreater(t2v_at, 0, "找不到 t2v 分支")
        shared_at = code.find("_isVideoMode = mode ===")
        self.assertGreater(shared_at, 0, "找不到公共作用域的接续提交块")

        for label, seg in (("t2v", code[t2v_at:flf_at]),
                           ("flf/r2v", code[flf_at:shared_at])):
            self.assertNotRegex(
                seg, r"params\.continuation\s*=",
                f"{label} 分支里不该再出现 params.continuation 的赋值"
                "（公共作用域统一提交，避免分支覆盖）")
            self.assertNotRegex(
                seg, r"delete\s+params\.continuation",
                f"{label} 分支里不该 delete params.continuation")

    def test_no_mode_specific_continuation_gate(self):
        """不得用「只有 t2v 才接续」之类的模式闸把 flf/r2v 挡掉。

        这条守的是**语义**：接续是**跨模式**能力，服务端三种模式都支持。
        """
        code = self.code
        shared_at = code.find("_isVideoMode = mode ===")
        self.assertGreater(shared_at, 0, "找不到公共作用域的接续提交块")
        window = code[shared_at:shared_at + 2500]
        self.assertNotRegex(
            window, r'mode\s*===\s*"t2v"\s*&&[^;]*continuation',
            "接续提交不得只对 t2v 生效")
        # 白名单必须是「三个模式全在」，不能是「t2v 且不是 flf 之类」
        self.assertRegex(
            window,
            r'_isVideoMode\s*=\s*mode\s*===\s*"t2v"\s*\|\|\s*'
            r'mode\s*===\s*"flf"\s*\|\|\s*mode\s*===\s*"r2v"',
            "模式白名单必须显式列出 t2v / flf / r2v 三个模式")

    def test_submit_consistency_gate_still_applies_to_all_modes(self):
        """提交前的分辨率一致性闸必须**在公共作用域**里（对三模式生效）。

        【为什么重要】尺寸闸原本写在 t2v 分支里。修成公共作用域时
        很容易把它**漏掉** —— 那样 flf/r2v 接续就失去跨分辨率保护，
        错误会一路跑到 ComfyUI 才炸（Relay-Kit 拷贝桥 raise）。
        """
        code = self.code
        shared_at = code.find("_isVideoMode = mode ===")
        self.assertGreater(shared_at, 0, "找不到公共作用域的接续提交块")
        window = code[shared_at:shared_at + 2500]
        # 【为什么要查「守卫模式」而不是查字符串】
        # 只 assertIn("checkSubmitConsistency", window) 会被**注释**骗过 ——
        # 而且把 `if (typeof ... === "function") {` 改成 `if (false) {`
        # （闸变成死代码、实际不再执行）时断言仍然绿。
        # 变异测试 M3 就是这么跑的 ⇒ 必须查**可执行的守卫**本身。
        self.assertRegex(
            window,
            r'typeof\s+window\.Continuation\.checkSubmitConsistency\s*'
            r'===\s*"function"',
            "公共作用域的接续提交块里必须有分辨率一致性闸的**守卫**"
            "（typeof ... === \"function\"）")
        self.assertRegex(
            window,
            r'checkSubmitConsistency\s*\(\s*size\s*\)',
            "分辨率一致性闸必须被**调用**（并把面板尺寸传进去）")
        self.assertIn(
            "currentSize", window,
            "分辨率一致性闸必须读面板的**真实**尺寸（currentSize）")


class TestBackendAcceptsContinuationForFlfR2v(unittest.TestCase):
    """C2：后端三种模式都消费 continuation（前端能传的前提）。"""

    def test_server_consumes_continuation_for_three_modes(self):
        py = _read(SERVER_PY)
        m = re.search(
            r'mode in \("t2v",\s*"flf",\s*"r2v"\)\s*and\s*'
            r'params\.get\("continuation"\)', py)
        self.assertIsNotNone(
            m,
            "server.py 必须对 t2v / flf / r2v 三种模式消费 "
            "params['continuation']")

    def test_build_flf_and_r2v_read_continuation_state(self):
        py = _read(VIDEO_MODES_PY)
        self.assertIn(
            "def build_flf", py, "缺少 build_flf")
        self.assertIn(
            "def build_r2v", py, "缺少 build_r2v")
        # 两个构建器都走 _build_common -> _continuation_state(params)
        self.assertIn(
            "_continuation_state(params)", py,
            "工作流构建器必须读接续状态（_continuation_state）")

    def test_continuable_modes_cover_three(self):
        py = _read(CONTINUATION_PY)
        m = re.search(r'CONTINUABLE_MODES\s*=\s*\(([^)]*)\)', py)
        self.assertIsNotNone(m, "找不到 CONTINUABLE_MODES")
        modes = re.findall(r'"([a-z0-9_]+)"', m.group(1))
        for mode in ("t2v", "flf", "r2v"):
            self.assertIn(mode, modes,
                          f"CONTINUABLE_MODES 必须含 {mode}")


if __name__ == "__main__":
    unittest.main()
