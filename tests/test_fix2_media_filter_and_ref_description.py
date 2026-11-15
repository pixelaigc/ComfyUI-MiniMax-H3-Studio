# -*- coding: utf-8 -*-
"""修复② 回归测试：媒体库类型过滤串台 + 图片编辑素材描述被截断只传第一行。

【用户 2026-11-15 实测报的两个缺陷】

缺陷 1（媒体库类型过滤串台）：
  点左侧「minimax-h3 长片拼接」后，右侧**共享的**媒体库只剩视频卡片，
  图片 / 音频全部不显示；「项目标签=全部」「生成类型=全部类型」也一样；
  把「生成类型」改成文生图，该类别反而**显示为空**。
  另外：用户没访问过 Agent，URL 却变成 #/agent/<id>。

缺陷 2（图片编辑素材描述只传第一行）：
  H3-IMAGE 图片编辑 与 Qwen-Image 2.1 图片编辑面板，从素材库加一张图后，
  <image1> 输入框里只有「基于您提供的图片素材，以下是详尽的中文内容描述：」
  这一行首句 —— 素材实际描述很长（多小节）。

【本文件测什么】
  1. 前端（node 真跑 web/app.js 真实源码）：
     · 长片拼接页不得给共享媒体库强加 media=video；
     · 切走再切回，媒体库 query 不得残留上一个面板的过滤；
     · 按类型筛选时 query 必须真的带上 media=image 等；
     · Agent 页必须真正更新 app.js 的 state.page（原来写的是死代码）。
  2. 后端（真跑 h3studio 代码）：
     · _describe 必须返回**完整**分析文本（不取首行/首节、不截断 80 字）；
     · Qwen / H3-Image 两条路的参考图描述必须真的进到提示词里；
     · 用户自己写了 <imageN>/<Picture N> 时，描述仍必须给模型。

【测试纪律】
  * 断言必须由「命名该行为的那条断言」抓住 —— 不能自指、
    不能拿被测函数自己的输出算期望值。
  * 前端逻辑用 node **真跑** web/app.js 的真实源码（不是字符串匹配）。
  * 每条「必须包含完整描述」的断言都同时校验**长度**与**尾部特征小节**
    —— 只查开头会漏掉「只传第一行」这种截断。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

NODE = shutil.which("node")


# ==================================================================
# 前端：媒体库 filter 不得被面板污染（node 真跑 app.js）
# ==================================================================

#: 一段真实的素材库分析文本（结构与素材库里真实落库的一致：
#: 首行是「基于您提供的图片素材，以下是详尽的中文内容描述：」，
#: 后面是【主体人物】【服装与配饰】… 多个小节）。
SAMPLE_ANALYSIS = (
    "基于您提供的图片素材，以下是详尽的中文内容描述：\n\n"
    "【主体人物】\n"
    "画面中心为一名年轻女性动漫角色，占据画面约三分之二的比例。\n\n"
    "【服装与配饰】\n"
    "*   **上衣**：身穿一件白色的立领衬衫，胸前系着一个巨大的鲜红色蝴蝶结。\n\n"
    "【场景与环境】\n"
    "背景呈现出一种超现实或微缩模型般的奇幻场景。\n\n"
    "【光线与色彩】\n"
    "*   **光线**：光源似乎来自正前方及上方，阴影柔和。\n\n"
    "【其他值得注意的细节】\n"
    "*   人物的紫色瞳孔中有独特的十字星芒设计。"
)

#: node harness：真跑 web/app.js 里的 showCreate / showStitch /
#: refreshJobs 的 query 构造。
HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf-8').replace(/\r\n/g, '\n');

global.window = global.window || {};

function grab(start, end) {
  const i = src.indexOf(start);
  if (i < 0) throw new Error('not found: ' + start);
  const j = src.indexOf(end, i);
  if (j < 0) throw new Error('not found end: ' + end);
  return src.slice(i, j + end.length);
}

// ---- 状态 ----
const state = {
  page: 'create', tab: 'all', donePage: 1, pageSize: 12,
  doneFilters: { keyword: '', media: '', aspect: '', duration: '' },
  stitchMediaOverride: '', stitchClips: [],
  genType: '',
};
window.__h3stateProbe = () => state;

// ---- DOM 桩 ----
const selects = { '#doneMedia': { value: '' }, '#doneAspect': { value: '' },
                  '#doneDuration': { value: '' }, '#doneSearch': { value: '' } };
function $(sel) {
  if (sel in selects) return selects[sel];
  if (sel === '#jobsGrid') return null;
  return null;
}
const $$ = () => [];
function showPage() {}
function renderTabs() {}
function renderStitchClips() {}
function refreshJobs() {}
function closePanelDrawerOnPageChange() {}
function updateResponsiveChrome() {}
function syncVideoNavActive() {}
window.VIDEO_NAV = null;

// ---- 真源码：showCreate / showStitch ----
eval(grab('function showCreate() {', '\n}'));
eval(grab('function showStitch() {', '\n}'));

// ---- 真源码：refreshJobs 里拼 query 的那段 ----
//
// 【关键：必须抠 app.js 的**真实**代码，不能在 harness 里重写一份】
//   之前这个 harness 自己写了一份 query 构造，于是「把 app.js 里的
//   media=video 覆盖加回去」这个变异**测不出来**（MUT2 假绿）——
//   因为被测的是 harness 自己的副本，不是产品代码。
//   现在直接 eval app.js 里那一段真实源码。
const realBlock = grab('const query = new URLSearchParams({status});', '\n      const response = await fetch');
// 把 async 回调体抠出来包成普通函数：status / tab 都是外部入参。
// 只需要 query 构造本身（到 fetch 之前），后面的 await 不在这里。
const bodySrc = realBlock.slice(
  0, realBlock.indexOf('\n      const response = await fetch'));
const buildQuery = new Function('state', 'tab', 'status', bodySrc + '\n return query;');
const built = (st, tab) => buildQuery(st, tab, 'done').toString();

const out = {};
// ① 初始（创作页，全部类型）
showCreate();
out.initial_page = state.page;
out.initial_doneMedia = selects['#doneMedia'].value;
out.initial_query = built(state, state.tab);

// ② 点长片拼接
showStitch();
out.stitch_page = state.page;
out.stitch_doneMedia = selects['#doneMedia'].value;
out.stitch_state_media = state.doneFilters.media;
out.stitch_override = state.stitchMediaOverride;
out.stitch_query = built(state, state.tab);

// ③ 回到「生成」页（媒体库）
showCreate();
out.back_page = state.page;
out.back_doneMedia = selects['#doneMedia'].value;
out.back_state_media = state.doneFilters.media;
out.back_override = state.stitchMediaOverride;
out.back_query = built(state, state.tab);

// ④ 用户自己选「图片」类型后，进长片拼接页，再回创作页
//    —— 用户的筛选必须**原样保留**（不得被面板改掉，也不得被清掉）
selects['#doneMedia'].value = 'image';
state.doneFilters.media = 'image';
showStitch();
out.stitch_with_user_filter_query = built(state, state.tab);
showCreate();
out.back_with_user_filter_query = built(state, state.tab);
out.back_with_user_filter_doneMedia = selects['#doneMedia'].value;
out.back_with_user_filter_state_media = state.doneFilters.media;

console.log(JSON.stringify(out));
"""


def run_harness() -> dict:
    import tempfile
    with tempfile.TemporaryDirectory(prefix="h3-fix2-") as tmp:
        script = Path(tmp) / "probe.js"
        script.write_text(HARNESS, encoding="utf-8")
        proc = subprocess.run([NODE, str(script), str(ROOT / "web" / "app.js")],
                              capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(
            f"node 执行失败：\n{proc.stderr}\n--- stdout ---\n{proc.stdout}")
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1])


@unittest.skipIf(NODE is None, "node 缺失")
class TestMediaLibraryFilterIsolation(unittest.TestCase):
    """缺陷 1：媒体库的筛选不得被任何面板改写。"""

    @classmethod
    def setUpClass(cls):
        cls.data = run_harness()

    def test_stitch_page_does_not_force_video_filter(self):
        """长片拼接页不得对**共享的**媒体库强加 media=video。

        这条直击根因：media=video 是长片拼接对共享媒体库施加的页面级覆盖。
        它的后果有两个（用户都报了）：
          · 媒体库只剩视频 -> 图片 / 音频不显示；
          · media=video 与「生成类型=文生图」互斥 -> 选中文生图反而显示为空。
        """
        self.assertNotIn(
            "media=video", self.data["stitch_query"],
            "长片拼接页仍在给共享媒体库强加 media=video —— "
            "媒体库只剩视频，且与「生成类型=文生图」互斥导致空列表")

    def test_stitch_page_query_has_no_media_filter(self):
        """长片拼接页的媒体库 query **完全不带** media 过滤。"""
        self.assertNotIn(
            "media=", self.data["stitch_query"],
            "长片拼接页的媒体库 query 仍带 media 过滤 —— 共享媒体库被面板污染")

    def test_stitch_page_does_not_write_global_state(self):
        """showStitch() 不得写 state.doneFilters.media（全局筛选状态）。"""
        self.assertEqual(
            self.data["stitch_state_media"], "",
            "showStitch() 仍���写全局 state.doneFilters.media —— "
            "粘滞状态下媒体库永远只剩视频")

    def test_stitch_page_does_not_write_dom_control(self):
        """showStitch() 不得写 #doneMedia 的 DOM 值（下拉框会停在「视频」）。"""
        self.assertEqual(
            self.data["stitch_doneMedia"], "",
            "showStitch() 仍���写 #doneMedia 的 DOM 值 —— "
            "回到媒体库后下拉仍停在「视频」，用户自己筛不掉")

    def test_leaving_stitch_restores_all_types(self):
        """离开长片拼接页后，媒体库 query 不得带任何 media 过滤。"""
        self.assertNotIn(
            "media=", self.data["back_query"],
            "离开长片拼接页后媒体库 query 仍带 media 过滤 —— "
            "图片/音频产物仍然看不到（用户报的缺陷）")

    def test_user_filter_survives_panel_roundtrip(self):
        """用户自己选的类型筛选，在面板间来回切换后必须**原样保留**。

        这是「切换面板后不残留上一个面板的过滤状态」这条要求的正面断言：
        面板**不得**改写用户的筛选（既不清掉、也不替换）。
        """
        self.assertIn(
            "media=image", self.data["stitch_with_user_filter_query"],
            "长片拼接页把用户自己选的 media=image 覆盖掉了")
        self.assertIn(
            "media=image", self.data["back_with_user_filter_query"],
            "从长片拼接页回到创作页后，用户自己选的 media=image 丢了")
        self.assertEqual(
            self.data["back_with_user_filter_doneMedia"], "image",
            "回到创作页后下拉框没恢复成用户自己选的类型")
        self.assertEqual(
            self.data["back_with_user_filter_state_media"], "image",
            "回到创作页后全局筛选状态没恢复成用户自己选的类型")


@unittest.skipIf(NODE is None, "node 缺失")
class TestAgentPageUpdatesAppState(unittest.TestCase):
    """缺陷 1 的另一半：Agent 页必须真正更新 app.js 的 state.page。

    原来 agent.js 写的是 `if (window.state) window.state.page = "agent";`
    —— 但 app.js 的 state 是模块级 const，从未挂到 window 上，
    那行是**死代码**。于是 app.js 的 state.page 一直停留在上一个面板，
    后台轮询仍按「长片拼接页」拉数据。
    """

    def test_agent_js_does_not_rely_on_window_state(self):
        """agent.js 不得再用 window.state 这个不存在的全局。

        只查**可执行代码**（排除注释行）—— 否则我们自己写的说明注释里
        出现「window.state」字样就会误报。
        """
        src = (ROOT / "web" / "agent.js").read_text(encoding="utf-8")
        code_lines = []
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
                continue
            code_lines.append(line)
        code = "\n".join(code_lines)
        self.assertNotIn(
            "window.state", code,
            "agent.js 仍在写 window.state —— app.js 的 state 从未挂到 window 上，"
            "那行是死代码，state.page 不会被更新（媒体库串台的另一半根因）")

    def test_app_exposes_page_change_bridge(self):
        """app.js 必须提供一个「切页通知」窄接口给别的模块用。"""
        src = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn(
            "notifyPageChange", src,
            "app.js 没有暴露 notifyPageChange —— 别的模块无法正确更新 state.page")

    def test_notify_page_change_sets_state_page(self):
        """notifyPageChange('agent') 必须真的把 state.page 设成 agent。

        用 node 真跑 app.js 里的这段实现（把它包成一个可调用的函数），
        而不是断言源码里有没有某个字符串 ——
        字符串匹配只能证明「源码里出现过这个词」，
        不能证明「调用它之后 state.page 真的变了」。
        """
        harness = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf-8').replace(/\r\n/g, '\n');
global.window = global.window || {};
function grab(start, end) {
  const i = src.indexOf(start);
  if (i < 0) throw new Error('not found: ' + start);
  const j = src.indexOf(end, i);
  if (j < 0) throw new Error('not found end: ' + end);
  return src.slice(i, j + end.length);
}
const state = { page: 'create', stitchMediaOverride: 'video' };
// 抠出 notifyPageChange 的函数体，改写成可独立调用的函数
const body = grab('notifyPageChange: function (page) {', '\n  },');
// body 形如 "notifyPageChange: function (page) { ... }," —— 去掉对象字面量的键
const fnSrc = body
  .replace('notifyPageChange: function (page) {', 'function (page) {')
  .replace(/,\s*$/, '');
const notifyPageChange = eval('(' + fnSrc + ')');
notifyPageChange('agent');
console.log(JSON.stringify({page: state.page, override: state.stitchMediaOverride}));
"""
        import tempfile
        with tempfile.TemporaryDirectory(prefix="h3-fix2-page-") as tmp:
            script = Path(tmp) / "probe.js"
            script.write_text(harness, encoding="utf-8")
            proc = subprocess.run([NODE, str(script), str(ROOT / "web" / "app.js")],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(
            data["page"], "agent",
            "notifyPageChange('agent') 没有把 state.page 设成 agent")
        self.assertEqual(
            data["override"], "",
            "离开长片拼接页时 notifyPageChange 必须清掉 stitchMediaOverride")


@unittest.skipIf(NODE is None, "node 缺失")
class TestAgentRouteDoesNotHijackUrl(unittest.TestCase):
    """用户没访问过 Agent，URL 不该变成 #/agent/<id>。"""

    def test_sync_route_requires_agent_page_visible(self):
        """syncRoute 必须在「Agent 页不可见」时直接返回，不写 URL。"""
        src = (ROOT / "web" / "agent_sessions.js").read_text(encoding="utf-8")
        block = re.search(
            r"function syncRoute\(sessionId\) \{.*?\n  \}", src, re.S)
        self.assertIsNotNone(block, "agent_sessions.js 里找不到 syncRoute")
        code = block.group(0)
        self.assertIn(
            "agentVisible", code,
            "syncRoute 没有判断 Agent 页是否可见 —— "
            "页面加载时的会话恢复会把 URL 改成 #/agent/<id>（用户报的 URL 污染）")
        self.assertRegex(
            code, r"if\s*\(\s*!agentVisible\s*\)\s*return",
            "syncRoute 在 Agent 页不可见时没有提前返回 —— "
            "后台恢复会话会改写用户当前 URL")


# ==================================================================
# 后端：素材描述必须完整传递
# ==================================================================


class TestRefDescriptionNotTruncated(unittest.TestCase):
    """缺陷 2：_describe 必须返回完整分析文本。"""

    def test_describe_returns_full_analysis_text(self):
        """描述必须与素材库里的分析文本**逐字相同**（去首尾空白后）。

        这条直击根因：老实现 _condense() 只取「第一个有内容的小节」
        并截断到 80 字 —— 于是用户只看到首行首句。
        """
        from h3studio import ref_api
        desc, source = ref_api._describe(
            {"note": None}, {"text": SAMPLE_ANALYSIS}, [])
        self.assertEqual(source, "analysis")
        self.assertEqual(
            desc.strip(), SAMPLE_ANALYSIS.strip(),
            "描述被截断了 —— 素材库里的完整分析文本必须原样传递")

    def test_describe_keeps_all_sections(self):
        """所有小节标题都必须还在描述里（不能只剩第一行/首节）。"""
        from h3studio import ref_api
        desc, _ = ref_api._describe(
            {"note": None}, {"text": SAMPLE_ANALYSIS}, [])
        for section in ("【主体人物】", "【服装与配饰】", "【场景与环境】",
                        "【光线与色彩】", "【其他值得注意的细节】"):
            self.assertIn(
                section, desc,
                f"描述里丢了小节 {section} —— 只传了第一行/首节")

    def test_describe_keeps_tail_content(self):
        """描述的**尾部内容**必须还在（长度断言 + 尾部特征句）。"""
        from h3studio import ref_api
        desc, _ = ref_api._describe(
            {"note": None}, {"text": SAMPLE_ANALYSIS}, [])
        self.assertIn("十字星芒设计", desc, "描述的尾部内容丢了")
        self.assertGreater(
            len(desc), len(SAMPLE_ANALYSIS) * 0.9,
            f"描述长度 {len(desc)} 远小于原文 {len(SAMPLE_ANALYSIS)} —— 疑似被截断")

    def test_condense_is_gone(self):
        """老的 _condense（只取首节 + 截 80 字）必须不存在了。

        留着它 = 缺陷随时会被重新引入（有人调它就又截断了）。
        """
        from h3studio import ref_api
        self.assertFalse(
            hasattr(ref_api, "_condense"),
            "ref_api 里还留着 _condense() —— 它只取首节并截断 80 字，"
            "是「描述只传第一行」的根因实现，必须移除")

    def test_description_max_chars_does_not_truncate_normal_assets(self):
        """上限必须远大于素材库里的真实描述长度（否则等于白设上限）。"""
        from h3studio import ref_api
        self.assertGreater(
            ref_api.DESCRIPTION_MAX_CHARS, 10000,
            "描述上限太小 —— 会把正常素材（实测最长 5250 字）截断")


class TestRefDescriptionTextareaShowsFullText(unittest.TestCase):
    """缺陷 2 的**显示侧**：多行描述在输入框里必须看得见。

    【为什么这条单独重要 —— 2026-11-15 修②】
    后端修好后（描述完整返回），如果前端还把文本框写死 rows="2"，
    用户在 <image1> 那个框里**看到的**仍然只有开头一小截 ——
    现象与「描述被截断」一模一样，用户会以为没修好。
    """

    def test_textarea_rows_are_derived_from_description_length(self):
        """文本框的行数必须按描述长度给，不能写死 2 行。"""
        src = (ROOT / "web" / "qwen-refs.js").read_text(encoding="utf-8")
        block = re.search(r'<textarea data-desc-idx="\$\{i\}".*?>', src, re.S)
        self.assertIsNotNone(block, "qwen-refs.js 里找不到参考图描述文本框")
        tag = block.group(0)
        self.assertIn(
            'rows="${rows}"', tag,
            "参考图描述文本框仍然写死行数 —— 多行描述在界面上只显示开头一小截，"
            "用户看到的现象与「描述被截断」一样")
        self.assertNotIn(
            'rows="2"', tag,
            "参考图描述文本框写死 rows=\"2\" —— 长描述在界面上只露出第一行")

    def test_rows_are_computed_from_line_count(self):
        """行数必须由描述的换行行数算出来（有实参、有上限、有下限）。"""
        src = (ROOT / "web" / "qwen-refs.js").read_text(encoding="utf-8")
        self.assertRegex(
            src, r"const\s+rows\s*=\s*Math\.max\(\s*2\s*,\s*Math\.min\(",
            "qwen-refs.js 里没有按描述长度计算文本框行数的逻辑 —— "
            "多行描述在界面上显示不全")
        self.assertRegex(
            src, r"descText\.split\(\"\\n\"\)\.length",
            "行数没有按描述的换行行数计算 —— 多行描述会被压成两行显示")


class TestQwenReferenceBlockCarriesFullDescription(unittest.TestCase):
    """Qwen 图片编辑：完整描述必须进到提示词里。"""

    def _refs(self):
        from h3studio import ref_images as RI
        return [{
            "asset_id": "a1", "version": 1, "name": "素材1",
            "description": SAMPLE_ANALYSIS, "description_source": "analysis",
            "tags": [], "created_at": 1000.0, "preview_url": "", "missing": False,
        }]

    def test_single_image_block_contains_full_description(self):
        """单图：提示词里必须有**完整**描述。"""
        from h3studio import ref_images as RI
        out = RI.inject_reference_block("把雪豹放进这张图", self._refs())
        self.assertIn("十字星芒设计", out, "完整描述没进提示词")
        self.assertIn("【光线与色彩】", out, "描述的小节丢了")

    def test_user_written_placeholder_still_gets_description(self):
        """用户自己写了 <image1> 时，描述**仍然**必须给模型。

        老实现：「用户已写占位 -> 原样返回 prompt」-> 描述整段丢失。
        模型知道有图1，却不知道图1是什么 —— 这是语义丢失。
        """
        from h3studio import ref_images as RI
        out = RI.inject_reference_block("<image1> 把雪豹放进这张图", self._refs())
        self.assertIn("十字星芒设计", out,
                      "用户自己写了 <image1> 之后描述就丢了 —— "
                      "模型拿不到「这张图是什么」")
        self.assertEqual(
            out.count("<image1>"), 1,
            "用户自己写的占位被重复注入了（描述块不该带标记）")

    def test_user_written_placeholder_number_is_not_rewritten(self):
        """用户写的编号不得被改写（老约定：编号由前端负责对齐）。"""
        from h3studio import ref_images as RI
        refs = self._refs() + [dict(self._refs()[0], asset_id="a2",
                                    description="第二张图的描述")]
        out = RI.inject_reference_block("保持 <image2> 不变", refs)
        self.assertEqual(out.count("<image2>"), 1, "用户写的 <image2> 被重复或改写")
        self.assertNotIn("<image1>", out, "不该注入用户没写的 <image1>")


class TestH3ImageReferenceBlockCarriesFullDescription(unittest.TestCase):
    """H3-Image 图片编辑：完整描述必须进到提示词里。

    【为什么这条单独重要 —— 2026-11-15 修②】
    原来 H3-Image 这条路**根本没注入过描述**：
    worker 跑 i2i_h3image 时用「上传后的文件名列表」覆盖 params["ref_images"]，
    description 随之丢失；而 build_h3_image_workflow 只把 params["prompt"]
    送进模型 —— 模型**从来没看到**素材描述。
    这是用户报的「描述只传第一行」在 H3-Image 上的**更严重**形态。
    """

    def _refs(self):
        return [{
            "asset_id": "a1", "version": 1, "name": "素材1",
            "description": SAMPLE_ANALYSIS, "description_source": "analysis",
            "tags": [], "created_at": 1000.0, "preview_url": "", "missing": False,
        }]

    def test_h3_image_block_contains_full_description(self):
        from h3studio import h3image as h3img
        out = h3img.inject_h3_image_reference_block("把雪豹放进这张图", self._refs())
        self.assertIn("十字星芒设计", out, "完整描述没进 H3-Image 提示词")
        self.assertIn("【光线与色彩】", out, "描述的小节丢了")

    def test_h3_image_user_written_picture_marker_still_gets_description(self):
        from h3studio import h3image as h3img
        out = h3img.inject_h3_image_reference_block(
            "<Picture 1> 把雪豹放进这张图", self._refs())
        self.assertIn("十字星芒设计", out,
                      "用户自己写了 <Picture 1> 之后描述就丢了")
        self.assertEqual(out.count("<Picture 1>"), 1,
                         "用户自己写的 <Picture 1> 被重复注入")

    def test_server_injects_h3_image_description_block(self):
        """服务端 i2i_h3image 分支必须调用注入函数。

        这条对着真实源码断言（不是字符串匹配整个文件）：
        取出 i2i_h3image 分支的代码块，里面必须有
        inject_h3_image_reference_block 的调用。
        """
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        block = re.search(
            r'if mode == "i2i_h3image":.*?(?=\n    # 参考生音乐|\n    if mode == "a2a")',
            src, re.S)
        self.assertIsNotNone(block, "server.py 里找不到 i2i_h3image 分支")
        code = block.group(0)
        self.assertIn(
            "inject_h3_image_reference_block", code,
            "server.py 的 i2i_h3image 分支没有注入参考图描述 —— "
            "worker 覆盖 ref_images 后描述就丢了，模型拿不到「这张图是什么」")

    def test_h3_image_workflow_prompt_carries_description(self):
        """端到端：走 server 的注入 -> 工作流里的 prompt 必须带描述。

        这是**行为**断言：模型实际收到的那段 prompt 里必须有完整描述。
        """
        from h3studio import h3image as h3img
        refs = self._refs()
        prompt = h3img.inject_h3_image_reference_block("把雪豹放进这张图", refs)
        workflow = h3img.build_h3_image_workflow(
            {"prompt": prompt, "model": h3img.H3_IMAGE_DEFAULT_MODEL},
            ref_files=["h3studio/x.png"], job_id="testjob")
        node = workflow["h3i:4"]
        self.assertIn(
            "十字星芒设计", node["inputs"]["prompt"],
            "工作流里送进模型的 prompt 没有完整描述")


if __name__ == "__main__":
    unittest.main()
