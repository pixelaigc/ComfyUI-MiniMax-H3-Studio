# -*- coding: utf-8 -*-
"""D11 收口轮：MOSS LoRA **形态标注**的前端契约测试（真实浏览器 + 真实后端）。

【这个测试在防什么】
服务端早就把 `qkv_shape` / `qkv_convertible` 下发到前端了，但前端**一直没用**。
后果：MOSS 面板上一条「融合 qkv」形态的 LoRA（上游把 q/k/v 合并在一个张量里）
和真·分离形态的条目在界面上**长得一模一样**，而且 LORA_CATALOG 里的
`base_verdict: "原生"` 会让它看起来像「原生就能直接挂」—— 实际上直接挂原文件
会被 ComfyUI 静默丢弃（只打 "lora key not loaded"，出图照跑但 LoRA 没生效）。

本测试用**真实浏览器**断言：
  1. 融合 qkv + 可转换 -> 条目上出现「融合 qkv · 自动转换」标签
  2. 融合 qkv + 不可转换 -> 条目上出现「融合 qkv · 不可用」标签
  3. 分离 qkv -> **不出现**任何 qkv 标签（别给正常条目乱贴标签）
  4. 详情面板里有「权重形态」行，且如实写明形态

【为什么要真实浏览器】
前一轮用「源码里有没有某个字符串」来断言 —— 实测**变异测试证明那是空洞的**：
把渲染分支的插值 `${fusedNote}` 改成 `${""}`，断言照样全绿。
所以这里必须真的把页面渲染出来，在 DOM 里找标签。

运行：python3 -m pytest tests/test_d11_moss_lora_frontend.py -v
"""

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:
    HAS_PLAYWRIGHT = False

from test_frontend_defaults import StudioServer  # noqa: E402


#: 造 3 条假条目喂给前端：融合可转换 / 融合不可转换 / 分离形态
FAKE_AVAILABLE = [
    {
        "name": "moss-effects/fake_fused_ok.safetensors",
        "display": "假条目 · 融合可转换",
        "base": "MOSS-Image",
        "family": "MOSS-Image",
        "kind": "effect",
        "compatible": True,
        "incompatible_reason": "",
        "attention_hit": 1.0,
        "compat_verified": True,
        "qkv_shape": "fused",
        "qkv_convertible": True,
        "recommended_strength": 1.0,
        "strength_cap": 1.0,
        "trigger": "",
        "scene": "",
        "group": "",
        "base_verdict": "原生",
        "size_mb": 100.0,
        "path": "/tmp/fake_fused_ok.safetensors",
    },
    {
        "name": "moss-effects/fake_fused_bad.safetensors",
        "display": "假条目 · 融合不可转换",
        "base": "MOSS-Image",
        "family": "MOSS-Image",
        "kind": "effect",
        "compatible": True,
        "incompatible_reason": "",
        "attention_hit": 1.0,
        "compat_verified": True,
        "qkv_shape": "fused",
        "qkv_convertible": False,
        "recommended_strength": 1.0,
        "strength_cap": 1.0,
        "trigger": "",
        "scene": "",
        "group": "",
        "base_verdict": "原生",
        "size_mb": 100.0,
        "path": "/tmp/fake_fused_bad.safetensors",
    },
    {
        "name": "moss-effects/fake_split.safetensors",
        "display": "假条目 · 分离形态",
        "base": "MOSS-Image",
        "family": "MOSS-Image",
        "kind": "effect",
        "compatible": True,
        "incompatible_reason": "",
        "attention_hit": 1.0,
        "compat_verified": True,
        "qkv_shape": "split",
        "qkv_convertible": False,
        "recommended_strength": 1.0,
        "strength_cap": 1.0,
        "trigger": "",
        "scene": "",
        "group": "",
        "base_verdict": "原生",
        "size_mb": 100.0,
        "path": "/tmp/fake_split.safetensors",
    },
]


@unittest.skipUnless(HAS_PLAYWRIGHT, "未安装 playwright")
class TestMossLoraShapeBadge(unittest.TestCase):
    """真实浏览器里验证「融合 qkv」标注。"""

    @classmethod
    def setUpClass(cls):
        cls.server = StudioServer()
        cls.server.start()
        cls.base = f"http://127.0.0.1:{cls.server.port}"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        cls.page = cls.browser.new_page(viewport={"width": 1500, "height": 1000})

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close()
            cls.playwright.stop()
        finally:
            cls.server.stop()

    def _render(self):
        """在页面里注入 3 条假条目并触发 LoRA 列表渲染。"""
        self.page.goto(f"{self.base}/", wait_until="domcontentloaded")
        self.page.wait_for_timeout(400)
        # 切到 MOSS 模式（imgLoraCtx 才会返回 MOSS 的 store），
        # 塞进 available 池 + 已选列表，然后触发渲染。
        self.page.evaluate(
            """(items) => {
              const hooks = window.H3Studio && window.H3Studio.__loraTestHooks;
              if (!hooks) throw new Error("LoRA 测试钩子未暴露");
              if (window.__h3State) window.__h3State.mode = 't2i_moss';
              hooks.stores.MOSS.available = items;
              hooks.stores.MOSS.incompatible = [];
              const ctx = hooks.imgLoraMeta && window.__h3State;
              // 已选列表：state.imgMoss.loras
              if (window.__h3State && window.__h3State.imgMoss) {
                const list = window.__h3State.imgMoss.loras;
                list.splice(0, list.length,
                  { name: items[0].name, strength: 1.0, enabled: true, trigger: '' },
                  { name: items[1].name, strength: 1.0, enabled: true, trigger: '' },
                  { name: items[2].name, strength: 1.0, enabled: true, trigger: '' });
              }
              hooks.renderLoraList();
            }""",
            FAKE_AVAILABLE)
        self.page.wait_for_timeout(300)

    def _labels(self):
        return self.page.evaluate(
            """() => [...document.querySelectorAll('#imgLoraList .img-lora-item')]
                 .map(el => ({
                    name: el.querySelector('.img-lora-name')?.textContent?.trim() || '',
                    tags: [...el.querySelectorAll('.img-lora-tag')].map(t => t.textContent.trim()),
                 }))""")

    def test_fused_convertible_gets_auto_convert_badge(self):
        self._render()
        rows = {r["name"]: r["tags"] for r in self._labels()}
        self.assertIn("假条目 · 融合可转换", rows,
                      f"页面没渲染出 3 条条目：{rows}")
        tags = rows["假条目 · 融合可转换"]
        self.assertTrue(
            any("融合 qkv" in t and "自动转换" in t for t in tags),
            f"融合可转换条目必须有「融合 qkv · 自动转换」标签，实际：{tags}")

    def test_fused_not_convertible_gets_unusable_badge(self):
        self._render()
        rows = {r["name"]: r["tags"] for r in self._labels()}
        tags = rows["假条目 · 融合不可转换"]
        self.assertTrue(
            any("融合 qkv" in t and "不可用" in t for t in tags),
            f"融合不可转换条目必须有「融合 qkv · 不可用」标签，实际：{tags}")

    def test_split_shape_gets_no_qkv_badge(self):
        """分离形态是本机底模的原生形态 —— 绝不能乱贴标签。"""
        self._render()
        rows = {r["name"]: r["tags"] for r in self._labels()}
        tags = rows["假条目 · 分离形态"]
        self.assertFalse(
            any("融合 qkv" in t for t in tags),
            f"分离形态条目不该有 qkv 标签，实际：{tags}")

    def test_info_modal_shows_weight_shape_row(self):
        """详情面板必须如实写明「权重形态」。"""
        self._render()
        self.page.evaluate(
            """() => {
              const el = [...document.querySelectorAll('#imgLoraList .img-lora-item')]
                .find(e => e.querySelector('.img-lora-name')?.textContent?.includes('融合可转换'));
              el.querySelector('.img-lora-info-btn').click();
            }""")
        self.page.wait_for_timeout(300)
        body = self.page.inner_text("#loraInfoBody")
        self.assertIn("权重形态", body, "详情面板必须有「权重形态」行")
        self.assertIn("融合 qkv", body, "详情面板必须如实写明形态")
        self.assertIn("自动", body, "可转换的融合形态必须说明会自动转换")


if __name__ == "__main__":
    unittest.main()
