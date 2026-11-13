# -*- coding: utf-8 -*-
"""web/qwen-refs.js 的占位语法 profile 隔离回归测试。

【为什么需要这个测试】
参考图模块（web/qwen-refs.js）是一份实现、两套语法：
  * Qwen-Image 2.1  -> ``<imageN>``，多图时**强制**要求写全
    （官方 PE-I2I system prompt 原文 "MUST use <image1>, <image2>, ..."）
  * MiniMax-H3-Image -> ``<Picture N>``，只是**建议**
    （官方文档原文 "the prompt addresses each one by its label"）

两套语法的严格程度**不一样**，且互相串族会让合法提交被误拒
（H3-Image 的 ``<Picture 1>`` 被 Qwen 的正则扫不到 -> 报「缺少 <image1>」）。

本测试直接加载**真实的** web/qwen-refs.js（不是复制一份逻辑），
用 Node 跑 validate() / placeholderFor() / parsePlaceholders()，
锁住三件事：
  1. H3-Image 不强制写全标记（自然语言提示词可以通过）
  2. H3-Image 仍拦「悬空引用」（引用了不存在的第 N 张）
  3. 两套语法的解析**互不串族**

运行：``python3 -m pytest tests/test_qwen_refs_profile.py -v``
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NODE = shutil.which("node") or "/usr/bin/node"
# 【必须是 .cjs】/root/package.json 里有 "type": "module"，
# .js 后缀会被 Node 当成 ES module -> require 未定义。
SCRIPT = REPO / "tests" / "verify_qwen_refs_profile.cjs"


class TestQwenRefsProfileIsolation(unittest.TestCase):
    """两套占位语法的严格度 + 隔离性。"""

    @classmethod
    def setUpClass(cls):
        if not NODE or not Path(NODE).exists():
            raise unittest.SkipTest("node 不可用")
        if not SCRIPT.exists():
            raise AssertionError(f"验证脚本缺失：{SCRIPT}")
        proc = subprocess.run(
            [NODE, str(SCRIPT), str(REPO)],
            capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise AssertionError(f"验证脚本执行失败：{proc.stderr[:500]}")
        cls.data = json.loads(proc.stdout.strip().splitlines()[-1])

    def _case(self, name):
        for c in self.data["cases"]:
            if c["name"] == name:
                return c
        self.fail(f"验证脚本没有产出用例 {name}")

    def test_h3image_does_not_force_all_placeholders(self):
        """H3-Image：官方语义是「建议用 <Picture N>」，不是「必须」。

        自然语言提示词（「把图1里的猫换成雪豹」）必须能通过 ——
        强制要求写全会把合法提交误拒。
        """
        self.assertFalse(self._case("h3_natural_ok")["failed"])
        self.assertFalse(self._case("h3_partial_ok")["failed"],
                         "3 张只写 1 个标记也应通过（不强制写全）")
        self.assertFalse(self._case("h3_full_ok")["failed"])

    def test_h3image_still_rejects_dangling_reference(self):
        """H3-Image：仍要拦「引用了不存在的第 N 张」。"""
        case = self._case("h3_dangling_rejected")
        self.assertTrue(case["failed"])
        self.assertTrue(any("不存在" in p for p in case["problems"]),
                        f"错误信息要说清是悬空引用：{case['problems']}")

    def test_qwen_keeps_strict_placeholder_requirement(self):
        """Qwen-Image 2.1：官方 PE-I2I 要求多图必须写全 <imageN>。

        H3-Image 的宽松规则**不能**泄漏到 Qwen 通道
        （否则 Qwen 的多图编辑会丢参考图）。
        """
        self.assertTrue(self._case("qwen_natural_rejected")["failed"])
        self.assertFalse(self._case("qwen_full_ok")["failed"])

    def test_placeholder_syntaxes_do_not_cross(self):
        """两套语法的解析互不串族。"""
        iso = self.data["isolation"]
        self.assertEqual(iso["h3_placeholder"], "<Picture 1>")
        self.assertEqual(iso["qwen_placeholder"], "<image1>")
        # H3-Image 通道只认 <Picture N>
        self.assertEqual(iso["h3_parses_picture"], [2])
        self.assertEqual(iso["h3_parses_image"], [],
                         "H3-Image 通道不该解析出 <imageN>（串族了）")
        # Qwen 通道只认 <imageN>
        self.assertEqual(iso["qwen_parses_image"], [2])
        self.assertEqual(iso["qwen_parses_picture"], [],
                         "Qwen 通道不该解析出 <Picture N>（串族了）")


if __name__ == "__main__":
    unittest.main()
