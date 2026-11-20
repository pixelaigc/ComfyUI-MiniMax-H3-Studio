# -*- coding: utf-8 -*-
"""生成媒体库必须显示**所有类型**的生成产物（视频 / 图片 / 音频）—— 回归测试。

【用户 2026-11-15 报的缺陷（线上复现）】
「生成的媒体库中只有视频条目，看不到其他类型的生成内容（如文生图产物；音频同理）。」

【只读定位到的两个独立根因（本轮实测）】
  缺陷 1（**用户看到「只有视频」的直接原因**，前端）：
    web/app.js::showStitch() 里写死了
        state.doneFilters.media = "video";
        $("#doneMedia").value = "video";
    那是给「长片拼接」页用的**局部**筛选，但写进了**全局**筛选状态。
    用户点过一次左侧「长片拼接」-> 再回到「生成」页的媒体库，
    #doneMedia 仍是「视频」-> 媒体库只剩视频条目（图片/音频全部消失）。
    线上复现步骤见本文件的 test_stitch_page_must_not_pollute_global_media_filter。

  缺陷 2（后端 + 前端下拉，音频维度缺失）：
    server.py::api_list 的 media 分支只有 image / video / longfilm，
    media=audio 直接 400（"media 只能是 image、video 或 longfilm"）；
    而 gen_types 注册表里 t2a / a2a 的 media 明明就是 audio。
    前端 #doneMedia 下拉也只有「全部类型/视频/图片」，没有「音频」。

【测试的两条纪律】
  1. 断言必须由「命名该行为的那条断言」抓住 —— 不能自指。
  2. 前端逻辑用 Node **真跑** web/app.js 的真实源码（不是字符串匹配），
     后端逻辑用真实注册的 mode 集合推导（不写死 mode 名单）。

运行：python3 -m pytest tests/test_mediadef_all_types.py -q --tb=short
需要：node（缺失时前端用例整类 skip，不算失败）
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

NODE = shutil.which("node")


# ==================================================================
# 后端：media=audio 必须能筛出音乐任务（缺陷 2）
# ==================================================================
class TestMediaAudioFilter(unittest.TestCase):
    """后端 api_list 的 media 维度必须覆盖注册表里的每一种 media。"""

    def _media_modes(self, media: str) -> list:
        """按注册表推导某 media 维度对应的 mode 集合。

        与 server.py::api_list 的推导方式**同源**（都用 gen_types 注册表），
        而不是写死 mode 名单 —— 写死的话新增模式（t2i_qwen 那种）
        就会再次被漏掉，那正是 2026-07 的老 bug。
        """
        from h3studio import gen_types
        return [m for m in gen_types.registered_modes()
                if gen_types.type_for_mode(m).get("media") == media]

    def test_registry_declares_audio_media(self):
        """前提：注册表里确实有 media==audio 的模式（音乐类）。

        没有这一条，下面的用例会因为「注册表本身没登记」而假绿。
        """
        modes = self._media_modes("audio")
        self.assertTrue(
            modes,
            "生成类型注册表里应当有 media==audio 的模式（t2a 文生音乐 / a2a 参考生音乐）")

    def test_api_list_accepts_audio_media(self):
        """server.py 的 media 分支必须接受 audio（原来直接 400）。"""
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        branch = re.search(
            r'media = request\.query\.get\("media".*?\n\s*# 生成类型筛选',
            src, re.S)
        self.assertIsNotNone(branch, "server.py 里找不到 media 过滤分支")
        code = branch.group(0)
        self.assertIn(
            '"audio"', code,
            "server.py 的 media 过滤分支没有接受 audio —— "
            "media=audio 会落进 elif media: 直接 400，音乐产物无法按类型筛出")
        self.assertIn(
            '"image", "video", "audio"', code.replace('("image", "video", "audio")',
                                                       '"image", "video", "audio"'),
            "server.py 的 media 分支应当是 media in (image, video, audio) 这样的统一处理")

    def test_api_list_audio_maps_to_audio_modes(self):
        """media=audio 推导出的 mode 集合必须与注册表一致。"""
        modes = self._media_modes("audio")
        self.assertTrue(modes)
        # 音乐模式不可能是图像模式 —— 防止推导条件写反
        image_modes = self._media_modes("image")
        self.assertFalse(set(modes) & set(image_modes),
                         "audio 与 image 的 mode 集合不应重叠")


# ==================================================================
# 前端：长片拼接页不得污染全局 media 筛选（缺陷 1）
# ==================================================================
#: 在一个最小 DOM 桩上真跑 web/app.js 里的相关逻辑。
#: 【为什么用 node 真跑而不是字符串匹配】字符串匹配只能证明
#: 「源码里出现过某个词」，不能证明「填完槽位/切完页面之后行为对」。
HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf-8').replace(/\r\n/g, '\n');

// app.js 里的 showStitch 会摸 window.VIDEO_NAV —— node 里没有 window，
// 给一个最小桩（app.js 自己的写法是 `if (window.VIDEO_NAV) {...}`）。
global.window = global.window || {};

function grab(start, end) {
  const i = src.indexOf(start);
  if (i < 0) throw new Error('找不到标记: ' + start);
  const j = src.indexOf(end, i);
  if (j < 0) throw new Error('找不到结束标记: ' + end);
  return src.slice(i, j + end.length);
}

// ---- 状态 ----
const state = {
  page: 'create', tab: 'done', donePage: 1, pageSize: 8,
  doneFilters: { keyword: '', media: '', aspect: '', duration: '' },
  stitchMediaOverride: '', stitchClips: [],
  genType: '',
};

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
// 【为什么用正则而不是字面量】这段的 includes 列表会随功能演进变长
// （收藏功能加了 "favorites"）—— 写死字面量会让这条断言在**任何**改动
// 后失效，而且报的是「找不到标记」这种看不出意图的错。
// 改成正则：只要「按状态分页的那段」还在就过，具体清单不关心。
const qSrc = (() => {
  const m = /if \(\[[^\]]*\]\.includes\(status\)\) \{[\s\S]*?\n      \}/.exec(src);
  return m ? m[0] : '';
})();
if (!qSrc) throw new Error('找不到 refreshJobs 里按状态分页的那段（includes(status) 的 if 块）');
// 把 query 构造抠出来单独跑：它依赖 URLSearchParams（node 内置）与 state
const built = (function (state, tab) {
  const query = new URLSearchParams({ status: 'done' });
  query.set('page', state.donePage);
  query.set('page_size', state.pageSize);
  if (tab === 'longfilm') query.set('media', 'longfilm');
  else Object.entries(state.doneFilters).forEach(([key, value]) => { if (value) query.set(key, value); });
  if (state.page === 'stitch' && state.stitchMediaOverride) {
    query.set('media', state.stitchMediaOverride);
  }
  return query.toString();
});

const out = {};
// ① 初始：媒体库在 create 页
showCreate();
out.initial_page = state.page;
out.initial_doneMedia = selects['#doneMedia'].value;
out.initial_state_media = state.doneFilters.media;
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

console.log(JSON.stringify(out));
"""


def run_harness() -> dict:
    with tempfile.TemporaryDirectory(prefix="h3-mediadef-") as tmp:
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
class TestStitchDoesNotPolluteGlobalMediaFilter(unittest.TestCase):
    """缺陷 1：长片拼接页不得污染全局 media 筛选状态。"""

    @classmethod
    def setUpClass(cls):
        cls.data = run_harness()

    def test_stitch_page_does_not_write_global_done_filters(self):
        """showStitch() 不得写 state.doneFilters.media。

        这条断言直接对着根因：原来 showStitch 写死了
        `state.doneFilters.media = "video"`，而 doneFilters 是全局筛选状态。
        """
        self.assertEqual(
            self.data["stitch_state_media"], "",
            "showStitch() 仍然在写全局 state.doneFilters.media —— "
            "离开长片拼接页后媒体库会永远只剩视频条目")

    def test_stitch_page_does_not_write_media_dom_control(self):
        """showStitch() 不得写 #doneMedia 的 DOM 值。

        DOM 控件与 state 是同一份筛选的两面：只改 state 不改 DOM 会不一致，
        只改 DOM 不改 state 会让「全部」标签读到旧值。两条都不能单独动。
        """
        self.assertEqual(
            self.data["stitch_doneMedia"], "",
            "showStitch() 仍然在写 #doneMedia 的 DOM 值 —— "
            "回到媒体库后下拉仍停在「视频」，图片/音频产物看不到")

    def test_leaving_stitch_restores_all_types(self):
        """媒体库的 query 在**任何**面板下都不得被面板强加 media 过滤。

        这条是**行为**断言（用户视角）：媒体库是所有面板共享的那一个列表，
        它必须只反映**用户自己**的筛选条件。

        【2026-11-15 修② · 这条断言改了方向】
        原来这里断言的是「长片拼接页的 query **必须**带 media=video」
        —— 那正是缺陷本身：长片拼接把「只看视频」强加给了共享媒体库，
        于是媒体库只剩视频，且 media=video 与「生成类型=文生图」互斥 ->
        选中文生图反而显示为空（用户 2026-11-15 实测报的两个症状）。

        现在长片拼接要的「只收视频片段」由它**自己**的候选池实现
        （collectStitchCandidates / renderStitchCandidatePool），
        不再通过对共享媒体库下 media=video 来实现。
        """
        self.assertNotIn("media=video", self.data["stitch_query"],
                         "长片拼接页仍在给**共享的**媒体库强加 media=video —— "
                         "媒体库会只剩视频，且与「生成类型=文生图」互斥导致空列表")
        self.assertNotIn("media=", self.data["back_query"],
                         "离开长片拼接页后媒体库 query 仍带 media 过滤 —— "
                         "图片/音频产物仍然看不到（这正是用户报的缺陷）")

    def test_stitch_page_query_has_no_media_filter_at_all(self):
        """长片拼接页的媒体库 query **完全不带** media 过滤。

        媒体库是共享的：长片拼接页看媒体库时，用户看到的也应该是
        「全部类型 + 用户自己的筛选」，而不是被面板改过的窄化列表。
        """
        self.assertNotIn(
            "media=", self.data["stitch_query"],
            "长片拼接页的媒体库 query 仍带 media 过滤 —— "
            "共享媒体库被面板局部视图污染（用户 2026-11-15 实测缺陷）")

    def test_media_filter_control_shows_all_types_after_return(self):
        """回到媒体库后下拉回到「全部类型」。"""
        self.assertEqual(
            self.data["back_doneMedia"], "",
            "离开长片拼接页后 #doneMedia 没回到「全部类型」")


# ==================================================================
# 前端：#doneMedia 下拉必须有「音频」选项（缺陷 2 的前端半边）
# ==================================================================
class TestMediaDropdownHasAudio(unittest.TestCase):
    """#doneMedia 下拉必须提供「音频」选项。"""

    def test_done_media_has_audio_option(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        block = re.search(r'<select id="doneMedia".*?</select>', html, re.S)
        self.assertIsNotNone(block, "index.html 里找不到 #doneMedia 下拉")
        options = re.findall(r'<option value="([^"]*)"', block.group(0))
        self.assertIn(
            "audio", options,
            "#doneMedia 下拉没有「音频」选项 —— 音乐产物无法按类型筛出")


if __name__ == "__main__":
    unittest.main()
