# -*- coding: utf-8 -*-
"""《人机交互流程图》与代码的**同步性**回归测试。

【为什么要有这个测试】
「流程图实时反映代码」这件事，如果没有测试守住，就只是**一次性的人工同步**：
下一次有人加了生成模式 / 改了工具清单 / 改了素材槽位，图又会悄悄过期。

所以这里把「同步」变成**可执行的断言**：

  1. `python3 scripts/gen_flow.py --check` 必须通过
     —— 已提交的 .mmd / 派生清单与**当前代码**逐字节一致；
  2. 每个后端真实注册的生成模式，**图里必须出现对应节点与连线**；
  3. 每个 Agent 工具，**图里必须出现对应节点**；
  4. 每个后端 API 路由，**派生清单里必须出现**；
  5. 生成的 .mmd 里**不能残留占位符**，也不能有会让 Mermaid 解析失败的裸 `%%` 行。

【断言纪律】
  * 期望值来自**被测系统的真实状态**（`gen_types.list_types()` / `agent.TOOLS` / 路由表），
    不是写死的字符串常量 —— 否则测试自己就成了第二份要同步的清单。
  * 断言由「命名该行为的那条断言」抓住：§2 由 `test_generation_modes_appear_in_diagram` 抓住，
    §3 由 `test_agent_tools_appear_in_diagram` 抓住，等等。

运行：python3 -m pytest tests/test_flow_diagram.py -v
"""
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DOCS = REPO / "docs"
GEN = REPO / "scripts" / "gen_flow.py"
TPL_DIR = REPO / "scripts" / "templates"

DIAGRAMS = [
    DOCS / "交互流程图-总览.mmd",
    DOCS / "交互流程图-创作链路.mmd",
    DOCS / "交互流程图-素材库.mmd",
    DOCS / "交互流程图-内置Agent.mmd",
    DOCS / "交互流程图-系统配置.mmd",
]
DERIVED = DOCS / "交互流程图-派生清单.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class FlowDiagramSyncTest(unittest.TestCase):
    """流程图 ↔ 代码的同步性。"""

    def test_all_diagram_files_exist(self):
        """五张源文件 + 派生清单必须都在 docs/ 下。"""
        for p in DIAGRAMS + [DERIVED]:
            with self.subTest(file=p.name):
                self.assertTrue(p.exists(), f"缺少流程图文件 {p}")
                self.assertGreater(p.stat().st_size, 0, f"{p.name} 是空文件")

    def test_gen_flow_check_passes(self):
        """`--check` 必须通过 —— 这是「图与代码一致」的权威判据。"""
        proc = subprocess.run(
            [sys.executable, str(GEN), "--check"],
            cwd=str(REPO), capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(
            proc.returncode, 0,
            "流程图已与代码不一致，需重跑 `make flow`：\n"
            + proc.stdout + proc.stderr,
        )

    def test_generation_modes_appear_in_diagram(self):
        """每个后端真实注册的生成模式，图里必须有节点**和**连线。"""
        from h3studio import gen_types
        create = _read(DOCS / "交互流程图-创作链路.mmd")
        for item in gen_types.list_types():
            key = item.get("key")
            if not key or key == "__other__":
                continue
            with self.subTest(mode=key):
                self.assertIn(f"M_{key}[", create,
                              f"生成模式 {key} 在创作链路图里没有节点")
                self.assertIn(f"M_{key} -->", create,
                              f"生成模式 {key} 在创作链路图里没有连线")

    def test_agent_tools_appear_in_diagram(self):
        """每个 Agent 工具，图里必须有节点。"""
        from h3studio import agent
        agent_mmd = _read(DOCS / "交互流程图-内置Agent.mmd")
        for name in agent.TOOLS:
            with self.subTest(tool=name):
                self.assertIn(f"T_{name}[", agent_mmd,
                              f"Agent 工具 {name} 在内置 Agent 图里没有节点")

    def test_api_routes_appear_in_derived_list(self):
        """每条后端路由，派生清单里必须有一行。"""
        derived = _read(DERIVED)
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, '.');\n"
             "import importlib.util as u\n"
             "spec = u.spec_from_file_location('gen_flow', 'scripts/gen_flow.py')\n"
             "m = u.module_from_spec(spec); spec.loader.exec_module(m)\n"
             "import json; print(json.dumps(m.parse_routes(), ensure_ascii=False))"],
            cwd=str(REPO), capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        import json
        routes = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertGreater(len(routes), 50, "路由表解析结果异常少")
        for r in routes:
            with self.subTest(route=r["path"]):
                self.assertIn(f"`{r['path']}`", derived,
                              f"路由 {r['path']} 没出现在派生清单里")

    def test_no_placeholder_left_in_generated_files(self):
        """生成的 .mmd 里不能残留 @KEY@ 占位符。"""
        for p in DIAGRAMS:
            with self.subTest(file=p.name):
                leftovers = re.findall(r"@[A-Z0-9_]+@", _read(p))
                self.assertEqual(leftovers, [],
                                 f"{p.name} 残留占位符：{sorted(set(leftovers))}")

    def test_no_bare_comment_line_in_generated_files(self):
        """裸 `%%` 行会让 Mermaid 11.x 解析失败 —— 生成物里不允许出现。"""
        for p in DIAGRAMS:
            with self.subTest(file=p.name):
                for i, line in enumerate(_read(p).split("\n"), 1):
                    self.assertNotEqual(
                        line.strip(), "%%",
                        f"{p.name} 第 {i} 行是裸 %%，Mermaid 解析会失败")

    def test_templates_contain_no_bare_comment_line(self):
        """模板里也不允许出现裸 `%%` 行（生成器会自动修，但源头别埋雷）。"""
        for p in sorted(TPL_DIR.glob("*.mmd")):
            with self.subTest(template=p.name):
                for i, line in enumerate(_read(p).split("\n"), 1):
                    self.assertNotEqual(
                        line.strip(), "%%",
                        f"{p.name} 第 {i} 行是裸 %%，Mermaid 解析会失败")

    def test_rendered_artifacts_exist(self):
        """每张源文件都要有对应的 SVG / PNG 渲染产物。"""
        for p in DIAGRAMS:
            with self.subTest(file=p.name):
                for ext in (".svg", ".png"):
                    out = p.with_suffix(ext)
                    self.assertTrue(out.exists(), f"缺少渲染产物 {out.name}")
                    self.assertGreater(out.stat().st_size, 1024,
                                       f"{out.name} 太小，疑似渲染失败")


class FlowDiagramCoverageTest(unittest.TestCase):
    """路径覆盖度 —— 用户点名的每条主要路径都必须在图里有对应节点。"""

    @classmethod
    def setUpClass(cls):
        cls.all_text = "\n".join(_read(p) for p in DIAGRAMS)

    def test_required_paths_are_covered(self):
        """任务要求覆盖的主要路径，逐条断言。"""
        required = {
            "文生图": ["Z-IMAGE 文生图", "Qwen-Image 2.1 文生图", "MOSS-Image 文生图"],
            "图片编辑": ["Qwen-Image 2.1 图片编辑", "H3-Image 图片编辑"],
            "h3 image 文生图": ["H3-Image 文生图"],
            "文生视频": ["文生视频", "加速 LoRA", "效果 LoRA", "接续"],
            "音频/音乐": ["文生音乐", "参考生音乐", "YuE2 音乐模型"],
            "媒体库/生成库": ["生成库（媒体库）", "素材库"],
            "复用参数": ["复用参数"],
            "队列生成": ["生成队列", "SQLite FIFO"],
            "内置 agent 会话": ["新建交互", "工具调用", "会话产物"],
            "agent 能力位": ["能力位"],
        }
        for path_name, keywords in required.items():
            with self.subTest(path=path_name):
                for kw in keywords:
                    self.assertIn(kw, self.all_text,
                                  f"路径「{path_name}」的关键内容「{kw}」不在图里")

    def test_agent_tools_are_grouped(self):
        """每个 Agent 工具必须都出现在内置 Agent 图里（按作用对象分栏）。"""
        from h3studio import agent
        agent_mmd = _read(DOCS / "交互流程图-内置Agent.mmd")
        missing = [n for n in agent.TOOLS if f"T_{n}[" not in agent_mmd]
        self.assertEqual(missing, [], f"这些工具没进图：{missing}")
        for group in ("素材类", "任务类", "模式与参数类", "提示词类", "会话与记忆类"):
            with self.subTest(group=group):
                self.assertIn(group, agent_mmd, f"工具分栏缺「{group}」")

    def test_every_agent_tool_has_a_named_group(self):
        """
        【2026-11-16 内置 Agent 审查新增】每个 Agent 工具都必须归入
        **正式命名**的分栏，不能掉进「其它工具」。

        【为什么要有这条断言】
        2026-11-14 有 10 个工具（媒体库管理 / 外部能力 / ffmpeg 音视频）
        加进 agent.TOOLS，但 scripts/gen_flow.py 的 TOOL_GROUP_RULES
        没同步更新 —— 它们被推进「其它工具」分栏，图里的语义分类就丢了。
        更糟的是 docs/ 里的流程图自那以后没再重跑，
        于是「每个 Agent 工具都必须有节点」这条断言**直接转红**，
        一直没人发现（线上遗留失败）。

        判据必须由「命名该行为的那条断言」抓住：
        这里断言的是「**没有工具掉进兜底分栏**」，
        而不是「某个具体工具在某个具体分栏」（那会变成第二份要同步的清单）。
        """
        import importlib.util as u
        spec = u.spec_from_file_location("gen_flow_group",
                                        REPO / "scripts" / "gen_flow.py")
        mod = u.module_from_spec(spec)
        spec.loader.exec_module(mod)
        from h3studio import agent
        tools = [{"name": name, "stage": "", "description": ""}
                 for name in agent.TOOLS]
        groups = mod.group_agent_tools(tools)
        others = [t["name"] for t in groups.get("其它工具", [])]
        self.assertEqual(
            others, [],
            "这些 Agent 工具没归入任何正式分栏（掉进「其它工具」兜底）："
            f"{others}。请在 scripts/gen_flow.py 的 TOOL_GROUP_RULES 里补归属，"
            "然后跑 `make flow` 重生成流程图。")

    def test_agent_tool_side_nodes_exist(self):
        """
        【2026-11-16 内置 Agent 审查新增】工具分栏的「落地侧」节点必须真实存在。

        背景：新增分栏时如果只在 TOOL_GROUP_RULES 加名字、忘了在
        templates/flow-agent.mmd 的 SIDE 子图里加对应节点，
        Mermaid 会画出**指向不存在节点**的连线 —— 图看着有，实际是断的。
        """
        import re
        import importlib.util as u
        spec = u.spec_from_file_location("gen_flow_group2",
                                        REPO / "scripts" / "gen_flow.py")
        mod = u.module_from_spec(spec)
        spec.loader.exec_module(mod)
        agent_mmd = _read(DOCS / "交互流程图-内置Agent.mmd")
        # 分栏 -> 落地侧的连线：TG1 --> AS
        edges = re.findall(r"^\s*(TG\d+)\s*-->\s*([A-Z]{2,})\s*$",
                           agent_mmd, re.M)
        self.assertTrue(edges, "内置 Agent 图里没有「工具分栏 → 落地侧」连线")
        for gid, side in edges:
            with self.subTest(group=gid, side=side):
                self.assertRegex(
                    agent_mmd, rf"\b{re.escape(side)}\[",
                    f"落地侧节点 {side}（被 {gid} 指向）在图里不存在 —— "
                    "分栏连线是断的")

    def test_new_agent_tool_groups_have_side_nodes(self):
        """
        【2026-11-16 内置 Agent 审查新增】TOOL_GROUP_RULES 里声明的每个分栏，
        都必须能映射到一个存在的落地侧节点 id。
        """
        import re
        import importlib.util as u
        spec = u.spec_from_file_location("gen_flow_group3",
                                        REPO / "scripts" / "gen_flow.py")
        mod = u.module_from_spec(spec)
        spec.loader.exec_module(mod)
        agent_mmd = _read(DOCS / "交互流程图-内置Agent.mmd")
        group_side = {
            "素材类": "AS", "任务类": "JB", "模式与参数类": "MP",
            "提示词类": "PP", "会话与记忆类": "CM", "回应类": "RS",
            "媒体库管理类": "ML", "外部能力类": "EX", "ffmpeg 音视频类": "FF",
            "其它工具": "AS",
        }
        for group, _names in mod.TOOL_GROUP_RULES:
            side = group_side.get(group)
            with self.subTest(group=group):
                self.assertIsNotNone(
                    side, f"分栏「{group}」没有声明落地侧节点 id")
                self.assertRegex(
                    agent_mmd, rf"\b{re.escape(side)}\[",
                    f"分栏「{group}」指向的落地侧节点 {side} 在图里不存在")


if __name__ == "__main__":
    unittest.main()
