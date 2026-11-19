# -*- coding: utf-8 -*-
"""R2 补测：前端「接续段也用二采」开关 —— **端到端接线**（前端注入 → 服务端放行）。

【为什么上一轮的测试不够】
上一轮 `tests/test_twopass_allow_frontend.py` 守的是**静态位置契约**：
「注入点必须在 t2v / flf / r2v 三个分支之后」。但那条断言只证明
**代码位置对**，不证明**接线真的通** —— 位置对但注入值算错 / 被中间某步改写，
静态断言照样全绿。

【而且线上已经变了】
另一条任务 `05c5cef`（cjxz，2026-11-19）把接续上下文的提交也提到了
三模式公共作用域。也就是说：**flf / r2v 现在也会带 continuation 提交**，
前端那个开关在 flf / r2v 下第一次真正变得可达。
本文件补的就是这一层：**行为级**验证「开关勾上 ⇒ 三种模式的提交体都带
allow_twopass ⇒ 服务端归一层都判定为放行」。

【不变量】
  E1. 默认（不勾）⇒ 三种模式的提交体都**不含** allow_twopass
      ⇒ 服务端 `resolve_effective_twopass` 全部返回 (False, 'deny')
      ⇒ 默认路径仍等价 nonebot minimax_h3_seg2（接续段走标准采样）。
  E2. 显式勾选 ⇒ 三种模式的提交体都**带** allow_twopass=true
      ⇒ 服务端归一层与工作流层都判定为放行（两侧调同一个函数，不会分裂）。
  E3. 服务端是权威：即使前端不传，服务端默认也 deny
      （前端被绕过时不会「悄悄开着二采」）。

【变异对应】见 tests/mutation_twopass_allow_frontend.py 的 F8 / F9 / F10。
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from h3studio import twopass_policy  # noqa: E402

VIDEO_MODES = ("t2v", "flf", "r2v")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# 前端提交体构造：按 web/app.js 的真实逻辑复算
# ---------------------------------------------------------------------------
def frontend_submit_continuation(mode: str, allow_continuation: bool,
                                 twopass_enabled: bool = True) -> dict:
    """复算 web/app.js 在给定模式下会往提交体里写的 ``params["continuation"]``。

    【对照的线上代码位置】web/app.js:
      1) ``const _isVideoMode = mode === "t2v" || mode === "flf" || mode === "r2v";``
      2) ``if (_isVideoMode && window.Continuation && window.Continuation.state) {``
         -> ``params.continuation = _contCtx.continuation;``
      3) 之后（三分支公共作用域）:
         ``if (_allowTp && window.Continuation && window.Continuation.state) {``
             -> ``params.continuation = Object.assign({}, _ctx.continuation,
                                                     { allow_twopass: true });``

    【为什么用复算而不是真跑浏览器】
    真跑浏览器需要起服务器 + 造接续上下文，成本高且会与线上其它任务抢 8190。
    这里复算的是**判定逻辑本身**（不是断言某个字符串常量），
    所以变异掉判定条件（默认值 true / 去掉条件 / 三模式白名单写错）
    都会让断言变红 —— 鉴别力由 tests/mutation_twopass_allow_frontend.py 保证。

    【接续段的判据】
    服务端 `resolve_twopass_policy` 判「是不是接续段」看的是
    ``continuation["source_job"]`` 非空（不是 stage_index）。
    ⇒ 复算时必须带上 source_job，否则会被当成普通段、判成 allow。
    """
    params: dict = {"twopass": {"enabled": bool(twopass_enabled)}}
    if mode in VIDEO_MODES:
        # 步骤 2：三模式公共作用域里的接续上下文（source_job 非空 = 续接段）
        params["continuation"] = {"stage_index": 1, "source_job": "J-source-1"}
        # 步骤 3：开关显式放行
        _allow_tp = bool(allow_continuation)
        if _allow_tp:
            params["continuation"] = dict(params["continuation"],
                                         allow_twopass=True)
    return params


class TestFrontendToServerTwopassWiring(unittest.TestCase):
    """E1/E2：前端注入 → 服务端判定 的端到端接线。"""

    def test_default_path_all_three_modes_are_denied(self):
        """E1：不勾开关 ⇒ 三种模式的提交体都不含 allow_twopass，服务端全 deny。"""
        for mode in VIDEO_MODES:
            params = frontend_submit_continuation(mode, allow_continuation=False)
            self.assertNotIn("allow_twopass", params.get("continuation", {}),
                             f"{mode} 默认路径不该出现 allow_twopass")
            use, policy = twopass_policy.resolve_effective_twopass(params)
            self.assertFalse(use, f"{mode} 默认路径服务端应判定禁用二采")
            self.assertEqual(policy, "deny",
                             f"{mode} 默认路径处置方式应为 deny")

    def test_explicit_allow_all_three_modes_are_allowed(self):
        """E2：勾选开关 ⇒ 三种模式的提交体都带 allow_twopass，服务端放行（带 warn 留痕）。"""
        for mode in VIDEO_MODES:
            params = frontend_submit_continuation(mode, allow_continuation=True)
            self.assertIs(
                params.get("continuation", {}).get("allow_twopass"), True,
                f"{mode} 显式勾选后提交体应带 allow_twopass=true")
            use, policy = twopass_policy.resolve_effective_twopass(params)
            self.assertTrue(use, f"{mode} 显式勾选后服务端应放行二采")
            self.assertEqual(policy, "warn",
                             f"{mode} 显式勾选后处置方式应为 warn（放行但留痕）")

    def test_server_is_authoritative_even_if_frontend_is_bypassed(self):
        """E3：前端完全不传 allow_twopass ⇒ 服务端默认 deny。"""
        for mode in VIDEO_MODES:
            params = frontend_submit_continuation(
                mode, allow_continuation=False, twopass_enabled=True)
            params["continuation"].pop("allow_twopass", None)
            use, policy = twopass_policy.resolve_effective_twopass(params)
            self.assertFalse(use, f"{mode} 前端绕过时服务端必须 deny")
            self.assertEqual(policy, "deny")

    def test_only_literal_true_counts_as_explicit_allow(self):
        """E2 边界：只有字面量 True 才算显式放行；'true'/1/None 都不算。"""
        for bogus in ("true", 1, None, "yes", 0):
            params = {"continuation": {"allow_twopass": bogus}}
            use, _policy = twopass_policy.resolve_effective_twopass(params)
            self.assertFalse(use, f"allow_twopass={bogus!r} 不应算显式放行")


class TestFrontendInjectionMatchesLiveSource(unittest.TestCase):
    """把复算逻辑与**线上真实源码**对齐，防止源码改了测试还在自说自话。"""

    def test_live_source_has_three_mode_whitelist(self):
        js = _read(os.path.join(ROOT, "web", "app.js"))
        self.assertIn(
            'const _isVideoMode = mode === "t2v" || mode === "flf" || mode === "r2v";',
            js, "线上 app.js 的三模式白名单写法变了，复算逻辑需同步")

    def test_live_source_injects_with_object_assign_copy(self):
        js = _read(os.path.join(ROOT, "web", "app.js"))
        self.assertIn(
            "params.continuation = Object.assign({}, _ctx.continuation,",
            js, "线上 app.js 的注入写法变了，复算逻辑需同步")
        self.assertIn("{ allow_twopass: true });", js,
                      "线上 app.js 未再写 allow_twopass: true")

    def test_live_source_injection_is_after_three_mode_whitelist(self):
        """注入点必须在三模式白名单之后 —— 否则 flf/r2v 走不到。"""
        js = _read(os.path.join(ROOT, "web", "app.js"))
        wl = js.find('const _isVideoMode = mode === "t2v"')
        inj = js.find("params.continuation = Object.assign")
        self.assertGreater(wl, 0, "找不到三模式白名单")
        self.assertGreater(inj, 0, "找不到注入点")
        self.assertGreater(inj, wl,
                           "注入点必须在三模式白名单之后（否则 flf/r2v 走不到）")

    def test_server_preserves_allow_twopass_through_normalization(self):
        """服务端归一层重建 continuation 时，必须保住 allow_twopass。"""
        src = _read(os.path.join(ROOT, "server.py"))
        self.assertIn(
            'if info.get("allow_twopass") is True:',
            src, "server.py 的归一层不再保留 allow_twopass")
        self.assertIn(
            'params["continuation"]["allow_twopass"] = True',
            src, "server.py 的归一层不再回填 allow_twopass")


if __name__ == "__main__":
    unittest.main()
