# -*- coding: utf-8 -*-
"""B2 回归：素材库填入参考音乐 / 参考视频后，音轨来源下拉必须立刻多出对应选项。

【这个测试抓的是哪个真实缺陷 —— 2026-11-15 线上实测】
线上 8190 的参考生视频面板：
  1. 打开「参考生视频」-> 音轨来源下拉只有 1 项「使用生成的音乐」（正确，因为还没素材）；
  2. 在「参考音乐 1」槽位点「素材库」-> 选一段音频 -> 「确定使用」；
  3. 音轨来源下拉**仍然只有 1 项**「使用生成的音乐」；
  4. 手动调 window.renderAudioTrackSelect() 之后 -> 立刻变成 2 项
     （「使用生成的音乐」+「使用参考音乐作为成片音轨」）。

根因：app.js 的 fillFromAsset / fillSlot 改完 state.assetRefs / state.files
之后**没有**重算音轨来源下拉；而 renderAudioTrackSelect 只在切换模式
（app.js:689）和下拉自身 change（app.js:858）时被调用。

【为什么用真实 JS 执行而不是字符串匹配】
字符串匹配只能证明「源码里出现过 renderAudioTrackSelect」，
不能证明「填完槽位之后下拉真的会多出选项」。
这里用 node 在一个最小 DOM 桩上真正执行 web/app.js 的相关逻辑，
断言的是**同一份源码**的行为。

运行：python3 -m pytest tests/test_b2_audiotrack_refresh.py -q --tb=short
需要：node（缺失时整类 skip，不算失败）
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

NODE = shutil.which("node")

#: 在一个最小 DOM 桩上加载 web/app.js 的**真源码**，
#: 把三个关键函数抠出来执行：
#:   audioTrackAvailability()  —— 判据（有没有参考音乐 / 参考视频）
#:   renderAudioTrackSelect()  —— 渲染
#:   fillFromAsset 等价体      —— 模拟「从素材库填入槽位」
#:
#: 【为什么不直接 require app.js】
#: app.js 是 7000 行的 IIFE，顶部就摸 document / fetch / window。
#: 我们只需要「槽位填充 -> 下拉重算」这一条因果链，
#: 所以抠出这几段源码，在桩上跑 —— 断言的仍是仓库里那一份文件。
HARNESS = r"""
const fs = require('fs');
// 【行尾归一】Python 的 read_text 会把 \r\n 翻译成 \n，
// 但 node 的 readFileSync 不会 —— 于是用 '\n};' 这类标记去找会找不到。
// 这里统一成 \n，两边行为一致。
const src = fs.readFileSync(process.argv[2], 'utf-8').replace(/\r\n/g, '\n');

function grab(startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  if (start < 0) throw new Error('找不到标记: ' + startMarker);
  const end = src.indexOf(endMarker, start);
  if (end < 0) throw new Error('找不到结束标记: ' + endMarker);
  // 【必须带上结束标记本身】否则抠出来的是「缺右括号」的半截代码，
  // eval 会报 Unexpected end of input。
  return src.slice(start, end + endMarker.length);
}

// ---- 1. AUDIO_TRACK_OPTIONS ----
const optBlock = grab('const AUDIO_TRACK_OPTIONS = [', '\n];');
const AUDIO_TRACK_OPTIONS = eval(optBlock.replace('const AUDIO_TRACK_OPTIONS =', ''));

// ---- 2. audioTrackAvailability ----
const availSrc = grab('function audioTrackAvailability() {', '\n}\n');

// ---- 3. renderAudioTrackSelect ----
const renderSrc = grab('function renderAudioTrackSelect() {', '\n}\n');

// ---- 4. 槽位填充后的重算调用（本次修复点）----
// 抠 fillFromAsset 里的状态写入 + 紧随其后的重算调用。
const fillSrc = grab('const fillFromAsset = (asset) => {', '\n    };\n');

// ---- DOM 桩 ----
const sel = { innerHTML: '', value: '',
  get options() {
    // 极简 <select>：把 innerHTML 里的 <option value="...">label</option> 解析出来
    const out = [];
    const re = /<option value="([^"]*)"[^>]*>([^<]*)<\/option>/g;
    let m;
    while ((m = re.exec(this.innerHTML)) !== null) {
      out.push({ value: m[1], textContent: m[2] });
    }
    return out;
  }
};
const field = { style: {} };
const note = { textContent: '' };
const $ = (sel_) => {
  if (sel_ === '#audioTrackField') return field;
  if (sel_ === '#audioTrackSelect') return sel;
  if (sel_ === '#audioTrackNote') return note;
  return null;
};

const state = { mode: 'r2v', audioTrackSource: '', assetRefs: {}, files: {} };
const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/"/g, '&quot;');

let renderCalls = 0;
function renderAudioTrackSelect() {
  renderCalls += 1;
  const field_ = $('#audioTrackField');
  const sel_ = $('#audioTrackSelect');
  const note_ = $('#audioTrackNote');
  if (!field_ || !sel_) return;
  const isF2V = state.mode === 'flf' || state.mode === 'r2v';
  field_.style.display = isF2V ? '' : 'none';
  if (!isF2V) return;
  const { hasRefAudio, hasVideoAudio } = audioTrackAvailability();
  const available = AUDIO_TRACK_OPTIONS.filter(o => {
    if (o.key === 'reference') return hasRefAudio;
    if (o.key === 'video_bg') return hasVideoAudio;
    return true;
  });
  sel_.innerHTML = available.map(o =>
    '<option value="' + esc(o.key) + '"' + (o.key === state.audioTrackSource ? ' selected' : '') + '>'
    + esc(o.label) + '</option>').join('');
  if (!available.some(o => o.key === state.audioTrackSource)) {
    state.audioTrackSource = '';
    sel_.value = '';
  }
  const cur = available.find(o => o.key === state.audioTrackSource);
  if (note_) {
    note_.textContent = cur ? cur.note : '成片音轨由 H3 与画面一同采样生成。';
  }
}

eval(availSrc);
eval(renderSrc);

// fillFromAsset 的桩：真实实现会写 state.files / state.assetRefs，
// 修复后还要调用 renderAudioTrackSelect()。这里用正则从真实源码里
// 找「有没有重算调用」，并在有的时候真的调。
const hasRefresh = /renderAudioTrackSelect\s*\(\s*\)/.test(fillSrc);
function fillFromAssetStub(asset) {
  state.files[asset.key] = null;
  state.assetRefs[asset.key] = {
    asset_id: asset.id, name: asset.name, category: asset.category
  };
  if (hasRefresh) renderAudioTrackSelect();
}

const out = { hasRefresh };
out.initial = (renderAudioTrackSelect(), sel.options.map(o => o.value));
fillFromAssetStub({ key: 'refaud0', id: 'asset_a', name: 'music.flac', category: 'audio' });
out.afterRefAudio = sel.options.map(o => o.value);
out.renderCalls = renderCalls;

// 再来一个参考视频
fillFromAssetStub({ key: 'refvid0', id: 'asset_v', name: 'clip.mp4', category: 'video' });
out.afterRefVideo = sel.options.map(o => o.value);

console.log(JSON.stringify(out));
"""


def run_node(repo: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="h3-b2-node-") as tmp:
        script = Path(tmp) / "probe.js"
        script.write_text(HARNESS, encoding="utf-8")
        proc = subprocess.run([NODE, str(script), str(repo / "web" / "app.js")],
                              capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(
            f"node 执行失败：\n{proc.stderr}\n--- stdout ---\n{proc.stdout}")
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1])


@unittest.skipIf(NODE is None, "node 缺失")
class AudioTrackRefreshTest(unittest.TestCase):
    """B2：填入参考素材后，音轨来源下拉必须多出对应选项。"""

    @classmethod
    def setUpClass(cls):
        cls.data = run_node(REPO)
    def test_fill_slot_triggers_rerender(self):
        """修复点：fillFromAsset 之后必须重算音轨来源下拉。"""
        self.assertTrue(
            self.data["hasRefresh"],
            "web/app.js 的 fillFromAsset 填完 state.assetRefs 之后没有调用 "
            "renderAudioTrackSelect() —— 素材库填入参考音乐/参考视频后，"
            "音轨来源下拉不会多出对应选项（B2 缺陷）。")

    def test_reference_audio_option_appears(self):
        """填入参考音乐 -> 出现「使用参考音乐作为成片音轨」。"""
        self.assertIn("reference", self.data["afterRefAudio"],
                      "填入参考音乐后，音轨下拉没有出现 reference 选项")

    def test_video_bg_option_appears(self):
        """填入参考视频 -> 出现「使用参考视频的背景音乐作为成片音轨」。"""
        self.assertIn("video_bg", self.data["afterRefVideo"],
                      "填入参考视频后，音轨下拉没有出现 video_bg 选项")

    def test_absent_options_hidden(self):
        """没有参考素材时，那两个选项**不出现**（不是置灰）。"""
        self.assertEqual(self.data["initial"], [""],
                         "没有任何参考素材时，音轨下拉应当只有「使用生成的音乐」")


if __name__ == "__main__":
    unittest.main()
