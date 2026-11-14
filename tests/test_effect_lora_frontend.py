# -*- coding: utf-8 -*-
"""H3 效果 LoRA 多选交互的**前端契约**测试（真实浏览器 + 真实后端）。

【这个测试在防什么】
用户 2026-11-13 明确点名了 4 条硬约束。任何一条坏了用户都会立刻遇到：

  1. **多选状态管理** —— 能同时选中多枚效果 LoRA，各自带独立强度
  2. **选中回显** —— 刷新页面后，已选的 LoRA + 强度 + 触发词全部还原
  3. **可清空** —— 能一键清空全部已选（不能只能一项项删）
  4. **生成参数正确下发** —— 提交给服务端的 effect_loras 数组结构正确，
     且服务端真的把它变成了工作流里的 LoraLoaderModelOnly 节点

前 3 条在 DOM 上，第 4 条在服务端 + 工作流 JSON 里 —— 所以要真浏览器 + 真后端。

运行：python3 -m pytest tests/test_effect_lora_frontend.py -v
"""

import json
import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:
    HAS_PLAYWRIGHT = False

from test_frontend_defaults import StudioServer  # noqa: E402

sys.path.insert(0, str(REPO / "tests"))

from h3studio import h3_effect_loras as effect_registry  # noqa: E402


def _real_effect_names(count=2):
    """取本机真实存在的效果 LoRA 名（测试要用真名，不能编）。"""
    items = effect_registry.discover_effect_loras()
    return [e["name"] for e in items[:count]]


@unittest.skipUnless(HAS_PLAYWRIGHT, "未安装 playwright")
class TestEffectLoraFrontend(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = StudioServer()
        cls.server.start()
        cls.base = f"http://127.0.0.1:{cls.server.port}"
        cls.names = _real_effect_names(2)
        if len(cls.names) < 2:
            raise unittest.SkipTest("本机效果 LoRA 少于 2 枚")
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        cls.page = cls.browser.new_page(viewport={"width": 1500, "height": 1000})
        cls.page.goto(cls.base, wait_until="domcontentloaded")
        # 切到文生视频
        self_page = cls.page
        self_page.wait_for_timeout(1500)
        try:
            self_page.click('[data-mode="t2v"]', timeout=8000)
        except Exception:
            pass
        self_page.wait_for_timeout(3500)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close()
        except Exception:
            pass
        try:
            cls.playwright.stop()
        except Exception:
            pass
        cls.server.stop()

    # ------------------------------------------------------------ 工具

    def _menu_open(self):
        """【2026-11-14 需求 9】控件改成原生下拉后，搜索框不再「弹层」。

        现在的交互是：输入搜索词 -> 原生下拉只剩匹配项 -> 点「添加」。
        旧版的「点搜索框弹出多选浮层」已经不存在，保留这个方法名是为了
        不改动其余用例的调用点。
        """
        self.page.fill("#vidEffectLoraSearch", "")
        self.page.wait_for_timeout(500)

    def _search(self, keyword):
        self.page.fill("#vidEffectLoraSearch", keyword)
        self.page.wait_for_timeout(700)

    def _pick_first(self):
        """把下拉里选中的一项加进已选列表（点「添加」按钮）。

        【2026-11-14 需求 9】原来这里勾选浮层里的 checkbox；
        控件统一到原生下拉后，交互变成「下拉选一项 + 点添加」。
        """
        self.page.evaluate("""() => {
          const sel = document.querySelector('#vidEffectLoraSelect');
          if (!sel) throw new Error('no select');
          if (!sel.options.length) throw new Error('no candidate option');
          sel.selectedIndex = 0;
        }""")
        self.page.click("#vidEffectLoraAdd")
        self.page.wait_for_timeout(600)

    def _state(self):
        return json.loads(self.page.evaluate(
            "() => JSON.stringify(window.VIDEO_PANEL.state.effectLoras || [])"))

    def _submit(self):
        return json.loads(self.page.evaluate(
            "() => JSON.stringify(window.VIDEO_PANEL.submitParams().effect_loras || [])"))

    # ------------------------------------------------------------ 1. 多选状态管理

    def test_01_multi_select_keeps_both_entries(self):
        """能同时选中多枚，各自带独立强度。"""
        self.page.evaluate("() => window.VIDEO_EFFECT_LORA.clearAll()")
        self.page.wait_for_timeout(400)
        self._menu_open()
        self._search(self.names[0].split("/")[-1][:18])
        self._pick_first()
        self._search(self.names[1].split("/")[-1][:18])
        self._pick_first()

        items = self.page.query_selector_all("#vidEffectLoraList .img-lora-item")
        self.assertEqual(len(items), 2, "应同时选中 2 枚效果 LoRA")
        state = self._state()
        self.assertEqual(len(state), 2)
        self.assertEqual({s["name"] for s in state}, set(self.names))

    def test_02_each_entry_has_own_strength_slider(self):
        """每项有独立的强度滑杆，且改一项不影响另一项。

        ⚠️ 滑杆 step=0.05 —— 浏览器会把设置的值**吸附**到步长网格上
        （设 0.33 会变成 0.35）。所以断言值必须用步长对齐的数，
        否则测的是浏览器的吸附行为而不是我们的代码。
        """
        self.page.evaluate("""() => {
          const sl = document.querySelectorAll('#vidEffectLoraList input.strength');
          sl[0].value = '0.35';
          sl[0].dispatchEvent(new Event('input', {bubbles: true}));
          sl[0].dispatchEvent(new Event('change', {bubbles: true}));
        }""")
        self.page.wait_for_timeout(500)
        state = self._state()
        strengths = {s["name"]: s["strength"] for s in state}
        self.assertAlmostEqual(strengths[self.names[0]], 0.35, places=2)
        self.assertNotAlmostEqual(
            strengths[self.names[0]], strengths[self.names[1]],
            msg="两项的强度必须互相独立")
        sliders = self.page.query_selector_all("#vidEffectLoraList input.strength")
        self.assertEqual(len(sliders), 2, "每项都应有独立滑杆")
        # 界面显示的数值要与 state 一致
        shown = self.page.eval_on_selector_all(
            "#vidEffectLoraList .img-lora-strength",
            "els => els.map(e => e.textContent)")
        self.assertIn("0.35", shown)

    def test_03_toggle_disable_removes_from_submit(self):
        """停用（enabled=false）的项不下发到生成参数。"""
        before = self._submit()
        self.assertEqual(len(before), 2)
        self.page.evaluate("""() => {
          document.querySelector('#vidEffectLoraList .toggle').click();
        }""")
        self.page.wait_for_timeout(600)
        after = self._submit()
        self.assertEqual(len(after), 1, "停用后只应下发 1 项")

    # ------------------------------------------------------------ 2. 选中回显

    def test_04_selection_restored_after_reload(self):
        """刷新后已选 LoRA + 强度 + 触发词全部还原。"""
        # 恢复成两项都启用
        self.page.evaluate("() => window.VIDEO_EFFECT_LORA.clearAll()")
        self.page.wait_for_timeout(400)
        self._menu_open()
        self._search(self.names[0].split("/")[-1][:18])
        self._pick_first()
        self._search(self.names[1].split("/")[-1][:18])
        self._pick_first()
        self.page.evaluate("""() => {
          const sl = document.querySelectorAll('#vidEffectLoraList input.strength');
          sl[0].value = '0.44';
          sl[0].dispatchEvent(new Event('input', {bubbles: true}));
          sl[0].dispatchEvent(new Event('change', {bubbles: true}));
        }""")
        self.page.wait_for_timeout(1200)   # 等防抖保存落盘

        before = {s["name"]: s for s in self._state()}

        # 重新加载页面
        self.page.goto(self.base, wait_until="domcontentloaded")
        self.page.wait_for_timeout(2500)
        try:
            self.page.click('[data-mode="t2v"]', timeout=8000)
        except Exception:
            pass
        self.page.wait_for_timeout(4000)

        after = {s["name"]: s for s in self._state()}
        self.assertEqual(set(after), set(before), "刷新后选中的 LoRA 集合应一致")
        for name, item in before.items():
            self.assertAlmostEqual(
                after[name]["strength"], item["strength"], places=2,
                msg=f"{name} 的强度刷新后应还原")
            self.assertEqual(after[name]["trigger"], item["trigger"])

        # 界面也要渲染出来（不只是 state）
        rendered = self.page.query_selector_all("#vidEffectLoraList .img-lora-item")
        self.assertEqual(len(rendered), 2, "刷新后已选列表应重新渲染")

    # ------------------------------------------------------------ 3. 可清空

    def test_05_clear_all_button_exists_and_toggles(self):
        """「清空全部」按钮存在；无选中时隐藏，有选中时显示。"""
        self.page.evaluate("() => window.VIDEO_EFFECT_LORA.clearAll()")
        self.page.wait_for_timeout(400)
        self._menu_open()
        self._search(self.names[0].split("/")[-1][:18])
        self._pick_first()
        self.page.wait_for_timeout(400)

        btn = self.page.query_selector("#vidEffectLoraClear")
        self.assertIsNotNone(btn, "必须有「清空全部」按钮")
        self.assertFalse(btn.is_hidden(), "有选中时按钮应显示")

        btn.click()
        self.page.wait_for_timeout(900)
        self.assertEqual(self._state(), [], "清空后 state 应为空")
        self.assertEqual(
            self.page.query_selector_all("#vidEffectLoraList .img-lora-item"), [],
            "清空后列表应为空")
        self.assertEqual(self._submit(), [], "清空后不应下发任何效果 LoRA")
        self.assertTrue(
            self.page.query_selector("#vidEffectLoraClear").is_hidden(),
            "清空后按钮应隐藏")

    def test_06_clear_all_persists(self):
        """清空状态也要持久化（刷新后仍然是空的，不能复活）。"""
        self.page.wait_for_timeout(1200)
        self.page.goto(self.base, wait_until="domcontentloaded")
        self.page.wait_for_timeout(2500)
        try:
            self.page.click('[data-mode="t2v"]', timeout=8000)
        except Exception:
            pass
        self.page.wait_for_timeout(4000)
        self.assertEqual(self._state(), [], "清空后刷新不应复活已选项")

    def test_07_individual_delete_still_works(self):
        """单项删除仍然可用（清空不能取代它）。"""
        self._menu_open()
        self._search(self.names[0].split("/")[-1][:18])
        self._pick_first()
        self.page.wait_for_timeout(500)
        self.assertEqual(len(self._state()), 1)
        self.page.evaluate("""() => {
          document.querySelector('#vidEffectLoraList .del').click();
        }""")
        self.page.wait_for_timeout(600)
        self.assertEqual(self._state(), [], "点 × 应移除该项")


@unittest.skipUnless(HAS_PLAYWRIGHT, "未安装 playwright")
class TestEffectLoraWorkflowDispatch(unittest.TestCase):
    """第 4 条：生成参数正确下发 —— 服务端真的变成工作流节点。

    这一条**不碰浏览器**：走服务端 API + 工作流构建，
    断言的是「参数 -> LoraLoaderModelOnly 节点 + strength_model」这条链。
    """

    def test_10_submit_params_reach_workflow_nodes(self):
        from h3studio import video_workflow as vw
        from h3studio import video_models as vmodels
        names = _real_effect_names(2)
        params = {
            "job_id": "dispatch-test",
            "model_key": vmodels.VIDEO_DEFAULT_MODEL,
            "width": 768, "height": 1344, "duration": 5, "seed": 1,
            "prompt": "a cat walking",
            "fixed_accel_lora": "ckpt850",
            "effect_loras": [
                {"name": names[0], "strength": 0.55, "enabled": True,
                 "trigger": "3dsrx"},
                {"name": names[1], "strength": 0.70, "enabled": True,
                 "trigger": "DY"},
            ],
        }
        prompt = vw.build_t2v_panel(params)
        loaders = sorted(
            [(nid, n) for nid, n in prompt.items()
             if n.get("class_type") == "LoraLoaderModelOnly"],
            key=lambda kv: kv[0])
        self.assertEqual(len(loaders), 2, "应生成 2 个效果 LoRA 节点")
        self.assertEqual(loaders[0][1]["inputs"]["lora_name"], names[0])
        self.assertAlmostEqual(loaders[0][1]["inputs"]["strength_model"], 0.55)
        self.assertEqual(loaders[1][1]["inputs"]["lora_name"], names[1])
        self.assertAlmostEqual(loaders[1][1]["inputs"]["strength_model"], 0.70)
        # 串成链
        self.assertEqual(loaders[1][1]["inputs"]["model"], [loaders[0][0], 0])
        # 触发词合并进提示词
        text = prompt["105:104"]["inputs"]["prompt"]
        self.assertIn("3dsrx", text)
        self.assertIn("DY", text)

    def test_11_server_gate_rejects_bad_values(self):
        """绕过前端直接调 API，脏值一样被拦。"""
        import server
        base = {
            "job_id": "gate-test",
            "model_key": "fl2va_pruned_int8",
            "width": 768, "height": 1344, "duration": 5,
            "prompt": "x", "seed": 1, "fixed_accel_lora": "ckpt850",
        }
        # 不存在的文件
        out, error = server.normalize_t2v_params(dict(base, effect_loras=[
            {"name": "h3/nope.safetensors", "strength": 1.0}]))
        self.assertNotEqual(error, "")
        # 非数字强度
        names = _real_effect_names(1)
        out, error = server.normalize_t2v_params(dict(base, effect_loras=[
            {"name": names[0], "strength": "abc"}]))
        self.assertNotEqual(error, "")
        # 正常值 -> 通过，且强度被保留
        out, error = server.normalize_t2v_params(dict(base, effect_loras=[
            {"name": names[0], "strength": 0.55, "enabled": True,
             "trigger": "t"}]))
        self.assertEqual(error, "")
        self.assertEqual(len(out["effect_loras"]), 1)
        self.assertAlmostEqual(out["effect_loras"][0]["strength"], 0.55)
        self.assertEqual(out["effect_loras"][0]["trigger"], "t")

    def test_12_job_summary_shows_effect_loras(self):
        """任务卡片摘要里能看到用了哪几枚效果 LoRA + 强度。"""
        from h3studio import job_meta
        names = _real_effect_names(2)
        summary = job_meta.summarize("t2v", {
            "model_key": "fl2va_pruned_int8",
            "fixed_accel_lora": "ckpt850",
            "effect_loras": [
                {"name": names[0], "strength": 0.55, "enabled": True},
                {"name": names[1], "strength": 0.70, "enabled": True},
            ],
        })
        labels = summary.get("effect_lora_labels") or []
        self.assertEqual(len(labels), 2)
        self.assertAlmostEqual(labels[0]["strength"], 0.55)
        self.assertTrue(labels[0]["label"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
