"""增强下拉「归属徽标」文案契约。

【为什么要有这条】
2026-11-12 全量回归发现 test_qwen_pe_enhance::
test_dropdown_lists_four_methods 挂了 —— 徽标文案是
「YuE2 音乐 专属」，而既有文案是「YuE2 音乐专属」
（「音乐」和「专属」之间多了一个空格）。

根因：web/enhance-menu.js 的 unavailableReason() 里写的是
`owner + " 专属，本面板不可用"`，而 owner 的值是「YuE2 音乐」
（本身带空格语义）。引入提交 7c45ed8，已随 8190 线上一起发布。

【这条测试锁什么】
三个增强方式（layout / yue2_struct / qwen_pe）的归属徽标
必须是**既有文案**，一个空格都不能多也不能少 ——
因为下游有用例按字面量断言（"音乐专属" in badge）。

断言打在**真实前端文件**上（读 web/enhance-menu.js 的
unavailableReason 实现 + 用 js2py 式纯字符串推演），
不依赖浏览器 —— 快，且不会因机器负载抖动。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENHANCE_MENU = REPO / "web" / "enhance-menu.js"

#: owner 字段 -> 期望的徽标文案（既有文案，逐字对照）
EXPECTED_BADGES = {
    "MOSS-Image": "MOSS-Image 专属",
    "YuE2 音乐": "YuE2 音乐专属",
    "Qwen-Image 2.1": "Qwen-Image 2.1 专属",
}


def _owner_of(raw: str, owner_value: str) -> str:
    """从 enhance-menu.js 里抽出 owner 值。"""
    m = re.search(r'owner:\s*"([^"]*)"', raw)
    assert m, "没找到 owner 字段"
    return m.group(1)


class TestOwnerBadgeText(unittest.TestCase):
    """归属徽标文案契约（三个增强方式）。"""

    @classmethod
    def setUpClass(cls):
        cls.raw = ENHANCE_MENU.read_text(encoding="utf-8")

    def test_registry_owner_values_unchanged(self):
        """注册表里的 owner 值不能被悄悄改掉（它们是语义字段）。"""
        found = set(re.findall(r'owner:\s*"([^"]*)"', self.raw))
        self.assertEqual(found, set(EXPECTED_BADGES),
                         f"owner 取值集合变了：{sorted(found)}")

    def test_badge_uses_explicit_owner_map(self):
        """unavailableReason 必须走显式映射表，而不是裸拼字符串。"""
        self.assertIn("OWNER_BADGE", self.raw,
                      "unavailableReason 应使用显式 owner -> 徽标映射表")
        # 三个已知 owner 必须都在映射表里（而不是靠兜底分支现拼）
        for owner in EXPECTED_BADGES:
            self.assertIn(f'"{owner}"', self.raw,
                          f"映射表里缺 {owner}")
        # 兜底分支允许（未知 owner 时补一个空格），
        # 但**不能**再出现「owner + ' 专属'」这种无脑裸拼 ——
        # 那正是本回归的根因（YuE2 音乐 + ' 专属' = YuE2 音乐 专属）。
        code_lines = [
            ln for ln in self.raw.splitlines()
            if "owner +" in ln and not ln.strip().startswith("//")
            and not ln.strip().startswith("*")
        ]
        for ln in code_lines:
            self.assertNotIn('owner + " 专属"', ln,
                             f"不要再用 owner + ' 专属' 裸拼（会多一个空格）：{ln.strip()}")

    def test_expected_badge_texts_are_reachable(self):
        """映射表里三个 owner 的徽标必须逐字等于既有文案。"""
        for owner, badge in EXPECTED_BADGES.items():
            with self.subTest(owner=owner):
                self.assertIn(f'"{owner}": "{badge}"', self.raw,
                              f"{owner} 的徽标文案必须是「{badge}」")

    def test_no_double_space_in_any_badge(self):
        """任何 owner 拼出来的徽标都不能出现连续空格。"""
        for owner, badge in EXPECTED_BADGES.items():
            with self.subTest(owner=owner):
                self.assertNotIn("  ", badge,
                                 f"{owner} 的徽标含连续空格：{badge!r}")

    def test_yue2_badge_has_no_space_before_exclusive(self):
        """【回归点】「YuE2 音乐」后面**直接**接「专属」。

        「YuE2 音乐」本身含一个空格（YuE2 与 音乐 之间），
        但「音乐」与「专属」之间**不能**再有空格 ——
        既有文案是「YuE2 音乐专属」。
        """
        badge = EXPECTED_BADGES["YuE2 音乐"]
        self.assertEqual(badge, "YuE2 音乐专属")
        self.assertNotIn("音乐 专属", badge)

    def test_other_owners_keep_space_before_exclusive(self):
        """另两个 owner 的既有文案里「专属」前面是**有**空格的。"""
        self.assertEqual(EXPECTED_BADGES["MOSS-Image"], "MOSS-Image 专属")
        self.assertEqual(EXPECTED_BADGES["Qwen-Image 2.1"],
                         "Qwen-Image 2.1 专属")


class TestOwnerBadgeNoRegression(unittest.TestCase):
    """回归保护：曾经的失败症状不能复现。"""

    @classmethod
    def setUpClass(cls):
        cls.raw = ENHANCE_MENU.read_text(encoding="utf-8")

    def test_yue2_badge_contains_music_exclusive(self):
        """下游用例断言的是 `assertIn("音乐专属", badge)`。"""
        badge = EXPECTED_BADGES["YuE2 音乐"]
        self.assertIn("音乐专属", badge)


if __name__ == "__main__":
    unittest.main()
