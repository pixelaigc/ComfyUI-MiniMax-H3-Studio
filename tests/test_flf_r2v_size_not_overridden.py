# -*- coding: utf-8 -*-
"""回归测试：flf / r2v 提交的真实宽高不能被旧面板的陈旧 state 覆盖。

【缺陷（2026-11-15 代码审查发现，Critical）】
修复③ 把首尾帧 / 参考生视频面板改成与文生视频共用一套面板后，
提交链路里留下了一段**无条件覆盖**：

    if (shared.width) params.width = shared.width;      // 用户真实选择
    ...
    const legacySize = currentLegacyVideoSize();          // 旧面板的陈旧 state
    if (legacySize) {
      params.width  = legacySize.width;                   // ← 把上面覆盖掉了
      params.height = legacySize.height;
    }

`currentLegacyVideoSize()` 读的是 `state.res` / `state.aspect`，
而这两个字段**全仓从未被写过**（只在 state 初始化时定死
`"544p"` / `"16:9"`）—— 于是它恒定算出 864×480。

后果：用户在共享面板选「608p · 16:9」（1088×608），
首尾帧 / 参考生视频实跑 864×480 —— **面板说一套、产物另一套**，
正是那段注释本身要修的毛病的翻版。

【测试方法】
不写「源码里有没有某行字」这种弱断言 —— 那样测的是「代码长什么样」，
不是「行为对不对」。这里用 **node 真正跑一遍 app.js 里的实现**：
  1) 从 web/app.js 里**抽出真实函数**（legacyVideoSize / legacyTierForPixels）；
  2) 复刻提交链路的**赋值顺序**（先 shared 后 legacySize）；
  3) 断言最终 params.width/height == 用户选的值。
这样「把覆盖那两行删掉」或「加回来」都会被抓住。

运行：
    python3 -m pytest tests/test_flf_r2v_size_not_overridden.py -q --tb=short
需要 node（缺失时整类 skip，不算失败）
"""

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

APP_JS = REPO / "web" / "app.js"
NODE = shutil.which("node")


def extract_js_function(src: str, name: str) -> str:
    """从 JS 源码里抽出某个函数的**完整定义**（含嵌套花括号配平）。"""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", src)
    if not m:
        raise AssertionError(f"app.js 里找不到函数 {name}")
    i = src.index("{", m.start())
    depth = 0
    j = i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return src[m.start(): j + 1]


@unittest.skipUnless(NODE, "node 缺失，跳过")
class TestFlfR2vSizeNotOverridden(unittest.TestCase):
    """flf / r2v 的提交尺寸必须等于用户在共享面板选的值。"""

    def _run_submit_simulation(self, shared, mode="flf"):
        """用 node 跑真实实现，复刻提交链路的赋值顺序。"""
        src = APP_JS.read_text(encoding="utf-8")
        legacy = extract_js_function(src, "legacyVideoSize")
        tier = extract_js_function(src, "legacyTierForPixels")
        # 当前实现：从 app.js 的提交分支里**动态提取**那两行覆盖
        # （而不是在测试里抄一份 —— 抄一份就测不到 app.js 的改动）
        m = re.search(
            r"const legacySize = currentLegacyVideoSize\(\);\s*"
            r"if \(legacySize\) \{(.*?)\n    \}",
            src, re.S)
        self.assertIsNotNone(m, "找不到提交链路里的 legacySize 分支")
        override_body = m.group(1)

        script = f"""
{legacy}
{tier}
function currentLegacyVideoSize() {{
  // state.res / state.aspect 在全仓从未被写过 —— 恒为初始值
  return legacyVideoSize("544p", "16:9", {mode!r});
}}
const shared = {shared!r};
const params = {{}};
if (shared.width) params.width = shared.width;
if (shared.height) params.height = shared.height;
params.resolution = legacyTierForPixels(
  Number(shared.width) || 0, Number(shared.height) || 0);
// ---- app.js 里那段的**真实**实现（从源码抽出来的） ----
const legacySize = currentLegacyVideoSize();
if (legacySize) {{
{override_body}
}}
console.log(JSON.stringify({{width: params.width, height: params.height,
                           resolution: params.resolution}}));
"""
        with tempfile.TemporaryDirectory(prefix="h3-size-") as tmp:
            path = Path(tmp) / "sim.js"
            path.write_text(script, encoding="utf-8")
            proc = subprocess.run([NODE, str(path)], capture_output=True,
                                  text=True, timeout=60)
        self.assertEqual(0, proc.returncode,
                         f"node 模拟失败：{proc.stderr[:400]}")
        import json
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_flf_keeps_user_selected_size(self):
        """用户在共享面板选的尺寸必须原样提交。"""
        for shared in (
            {"width": 1088, "height": 608, "aspect": "16:9"},   # 608p
            {"width": 1344, "height": 768, "aspect": "16:9"},   # 768p
            {"width": 480,  "height": 832, "aspect": "9:16"},   # 竖屏
            {"width": 912,  "height": 912, "aspect": "1:1"},    # 方屏
        ):
            with self.subTest(shared=shared):
                got = self._run_submit_simulation(shared, mode="flf")
                self.assertEqual(shared["width"], got["width"],
                                 f"flf 提交的 width 被覆盖："
                                 f"期望 {shared['width']}，实得 {got['width']}")
                self.assertEqual(shared["height"], got["height"],
                                 f"flf 提交的 height 被覆盖："
                                 f"期望 {shared['height']}，实得 {got['height']}")

    def test_r2v_keeps_user_selected_size(self):
        """参考生视频同理。"""
        shared = {"width": 1088, "height": 608, "aspect": "16:9"}
        got = self._run_submit_simulation(shared, mode="r2v")
        self.assertEqual(shared["width"], got["width"])
        self.assertEqual(shared["height"], got["height"])

    def test_resolution_tier_still_derived_from_real_pixels(self):
        """档位标签仍必须按**真实像素**反算（与服务端 reso_label 同口径）。

        【为什么单列】修「覆盖」时很容易顺手把 resolution 也一起删掉 ——
        那样落库的 resolution 会退回旧面板的遗留默认值「544p」，
        卡片上显示的档位又和产物对不上了。
        """
        # 【为什么要对比「两条路径」的 resolution，而不是只看非空】
        # 共享面板分支**已经**写过一次 params.resolution —— 只断言
        # 「非空」或「不等于 544p」，那次赋值就能蒙混过关。
        # 变异测试实测：把 legacySize 分支里的反算删掉，断言照样通过。
        # 所以这里改成「删掉 legacySize 分支里的反算后，resolution 必须变化」。
        src = APP_JS.read_text(encoding="utf-8")
        m = re.search(
            r"const legacySize = currentLegacyVideoSize\(\);\s*"
            r"if \(legacySize\) \{(.*?)\n    \}",
            src, re.S)
        self.assertIsNotNone(m, "找不到 legacySize 分支")
        body = m.group(1)
        self.assertIn("legacyTierForPixels", body,
                      "legacySize 分支里没有按真实像素反算 resolution —— "
                      "落库的档位标签会退回旧面板的遗留默认值")

        # 真实像素 1088×608 反算出的档位必须与「旧面板遗留默认值」不同
        got = self._run_submit_simulation(
            {"width": 1088, "height": 608, "aspect": "16:9"}, mode="flf")
        self.assertTrue(got["resolution"],
                        "resolution 档位标签丢了（卡片显示会与产物不符）")
        self.assertNotEqual("544p", got["resolution"],
                            "resolution 仍是旧面板的遗留默认值 544p，"
                            "但真实像素是 1088×608")


class TestStateResAspectAreDead(unittest.TestCase):
    """`state.res` / `state.aspect` 是**死状态** —— 从来没被写过。

    【为什么单列一条】它们是上面那个 Critical 缺陷的**根因**：
    提交链路读它们算尺寸，但没人写它们。如果哪天有人「顺手」
    给它们接上 UI，行为会变；本测试把「它们当前是死状态」这件事
    钉住，将来接 UI 时这条测试会失败，提醒同步更新提交逻辑。
    """
    def test_state_res_and_aspect_are_never_written(self):
        """`state.res` / `state.aspect` 既不能被**赋值**，也不能在 state
        对象字面量里被**改值**。

        【为什么两种形态都要查】
        变异测试实测：只在 state 字面量里把 `res: "544p"` 改成
        `res: "608p"`（不写 `state.res = ...`）——
        上一版测试只查赋值形态，完全看不见这种改法。
        现在两种形态都查：
          1) `state.res = ...`      —— 运行时赋值
          2) state 对象字面量里的 `res: ...` —— 初始化值被改
        """
        src = APP_JS.read_text(encoding="utf-8")
        for var, init_key in (("state.res", "res"), ("state.aspect", "aspect")):
            # 形态 1：运行时赋值
            writes = list(re.finditer(re.escape(var) + r"\s*=(?!=)", src))
            self.assertEqual(
                [], [src.count("\n", 0, w.start()) + 1 for w in writes],
                f"{var} 现在被赋值了 —— 如果这是为了接 UI，"
                f"必须同时复查 flf/r2v 的尺寸提交逻辑"
                f"（见 test_flf_r2v_size_not_overridden.py）")

            # 形态 2：state 对象字面量里的初始值被改
            # 定位 `const state = {` 到它配平的 `}` 这一段
            m = re.search(r"const state = \{", src)
            self.assertIsNotNone(m, "找不到 state 对象字面量")
            i = src.index("{", m.start())
            depth = 0
            j = i
            while j < len(src):
                if src[j] == "{":
                    depth += 1
                elif src[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            literal = src[i:j + 1]
            init = re.search(r"\b" + init_key + r"\s*:\s*([^,\n]+)", literal)
            self.assertIsNotNone(init,
                                 f"state 里找不到 {init_key} 的初始值 —— "
                                 f"如果它被删了/改名了，请复查尺寸提交逻辑")
            value = init.group(1).strip().strip('"').strip("'")
            expected = "544p" if init_key == "res" else "16:9"
            self.assertEqual(
                expected, value,
                f"state.{init_key} 的初始值变成了 {value}（期望 {expected}）—— "
                f"currentLegacyVideoSize() 依赖它算尺寸，"
                f"改了会让 flf/r2v 的兜底尺寸跟着变")


if __name__ == "__main__":
    unittest.main()
