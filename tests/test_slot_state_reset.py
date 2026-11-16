# -*- coding: utf-8 -*-
"""回归测试：切模式时槽位状态必须清空 + 重建时必须如实回填（B-3 / B-4 / B-12）。

【缺陷（2026-11-16 第 4 轮代码审查发现）】
第 3 轮把 B-2（提交时按模式白名单过滤 asset_refs）修掉了，但**根因没动**：
`renderMode()` 只重建 DOM（`row.innerHTML = ""`），而
`state.files` / `state.assetRefs` **全仓没有任何一处被清空**。

后果链（现场核实）：
  1. 用户在「首尾帧」模式选了首帧 -> 切到「参考生视频」
     -> r2v 界面上**没有**首帧槽位，state 里却还留着 `first`
  2. B-2 的白名单能挡住 `first`（r2v 白名单不含 first），
     但**挡不住同 key 跨模式复用**：`refvid0` / `refaud0` 在 flf 与 r2v
     **都存在** —— flf 选的参考视频会原封不动跟着进 r2v 的提交体
  3. `audioTrackAvailability()` 读的就是 state.assetRefs / state.files
     -> 新模式里错误地出现「使用参考音乐作为成片音轨」选项

【修法】
  * `resetSlotsForMode(mode, keepSlots)`：切模式时按**新模式白名单**
    清空槽位状态；
  * `restoreSlotContent(slot, key, kind, label)`：重建槽位时按 state
    **如实回填**（filled class + 缩略图 + previewUrl），让界面与提交一致；
  * `keepSlots` 例外：提交成功后的同模式重渲染要**保留**槽位
    （队列化改造要求「连续提交」走得通）。

【测试方法 —— 关键：实现必须从源码里抽，不能在这里写死】
如果把 resetSlotsForMode 的逻辑在 Python 里重写一遍，测试就变成了
「测 Python 副本」而不是「测 app.js 的真实实现」—— 改源码里的那一行
测试根本跑不到 -> 断言假通过（变异测试实测踩过这个坑，见
docs/代码审查报告-20261115.md 第 3 轮的「测试自身有洞」）。

所以这里：
  1) 从 web/app.js 里**按花括号配平法**抽出真实的
     MODE_ALLOWED_SLOT_KEYS / resetSlotsForMode / restoreSlotContent；
  2) 用一个最小 DOM 替身跑它们，断言清空语义与回填语义；
  3) 断言「渲染侧白名单」与「提交侧白名单」是**同一份**实现
     （防止两套口径漂移）。

运行：
    python3 -m pytest tests/test_slot_state_reset.py -q --tb=short
需要 node（缺失时整类 skip，不算失败）
"""

import json
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


def _extract_block(src: str, start_marker: str) -> str:
    """从 start_marker 处抽出花括号配平的整个代码块（含起止大括号）。

    跳过字符串 / 注释里的 `{`，否则会被截断。
    """
    idx = src.find(start_marker)
    if idx == -1:
        raise AssertionError(f"app.js 里找不到标记：{start_marker!r}")
    i = src.index("{", idx)
    depth = 0
    j = i
    n = len(src)
    while j < n:
        c = src[j]
        if c in ("'", '"', "`"):
            q = c
            j += 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == q:
                    break
                j += 1
            j += 1
            continue
        if c == "/" and j + 1 < n and src[j + 1] == "/":
            j = src.find("\n", j)
            if j == -1:
                break
            continue
        if c == "/" and j + 1 < n and src[j + 1] == "*":
            j = src.find("*/", j)
            if j == -1:
                break
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
        j += 1
    raise AssertionError(f"app.js 里 {start_marker!r} 的花括号没配平")


def _extract_decl(src: str, marker: str) -> str:
    """抽出一条完整声明（从 `const X =` 到语句末尾的分号）。

    只抽花括号块会把 `const MODE_ALLOWED_SLOT_KEYS = {` 的声明头丢掉。
    """
    blk = _extract_block(src, marker)
    start = src.rfind("const ", 0, src.index(blk))
    end = src.index(blk) + len(blk)
    tail = src[end:end + 2]
    if tail.lstrip().startswith(";"):
        end += tail.index(";") + 1
    return src[start:end]


def _extract_function(src: str, marker: str) -> str:
    """抽出一个完整函数定义（签名 + 花括号配平的函数体）。"""
    idx = src.find(marker)
    if idx == -1:
        raise AssertionError(f"app.js 里找不到标记：{marker!r}")
    brace = src.index("{", idx)
    depth = 0
    j = brace
    n = len(src)
    while j < n:
        c = src[j]
        if c in ("'", '"', "`"):
            q = c
            j += 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == q:
                    break
                j += 1
            j += 1
            continue
        if c == "/" and j + 1 < n and src[j + 1] == "/":
            j = src.find("\n", j)
            if j == -1:
                break
            continue
        if c == "/" and j + 1 < n and src[j + 1] == "*":
            j = src.find("*/", j)
            if j == -1:
                break
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[idx:j + 1]
        j += 1
    raise AssertionError(f"app.js 里 {marker!r} 的花括号没配平")


class TestSlotStateReset(unittest.TestCase):
    """切模式时槽位状态必须清空；重建时必须如实回填。"""

    @classmethod
    def setUpClass(cls):
        if not NODE:
            raise unittest.SkipTest("没有 node，跳过（需要真跑 app.js 逻辑）")
        cls.src = APP_JS.read_text(encoding="utf-8")

        # 全部从源码抽取 —— 不在这里重写实现。
        mode_allowed = _extract_decl(cls.src, "const MODE_ALLOWED_SLOT_KEYS =")
        reset_fn = _extract_function(cls.src, "function resetSlotsForMode(")
        restore_fn = _extract_function(cls.src, "function restoreSlotContent(")

        # resetSlotsForMode 依赖全局 state —— 注入一个可写的替身。
        # 【document.createElement 替身】restoreSlotContent 会用
        # document.createElement("img") / ("span") 造缩略图与徽标，
        # 替身必须带上 tagName 与 textContent，否则断言「有没有 <img>」
        # 测的是替身而不是被测代码。
        preamble = """
const state = {
  files: {}, assetRefs: {}, slotPreviewUrls: {}, audioTrackSource: '',
};
const document = {
  createElement: (tag) => ({
    tagName: String(tag).toUpperCase(),
    textContent: '',
    src: '', alt: '',
    className: '',
    append(...n) { (this.children ||= []).push(...n); },
  }),
};
"""

        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False,
                                         encoding="utf-8") as f:
            f.write(mode_allowed + "\n")
            f.write(reset_fn + "\n")
            f.write(restore_fn + "\n")
            f.write(preamble)
            f.write("""
export function reset(newState, mode, keepSlots) {
  // 【必须**替换**引用，不能 Object.assign】resetSlotsForMode 内部做的是
  // `state.assetRefs = {}`（换引用）—— 如果这里用 Object.assign 浅拷贝，
  // newState.assetRefs 与 state.assetRefs 是**同一个对象**，
  // 换引用后 newState 那份还是旧的 -> 测试会看到「没清空」，
  // 而那其实是**测试替身的假象**，不是产品代码的问题。
  state.files = newState.files || {};
  state.assetRefs = newState.assetRefs || {};
  state.slotPreviewUrls = newState.slotPreviewUrls || {};
  state.audioTrackSource = newState.audioTrackSource || '';
  resetSlotsForMode(mode, keepSlots);
  // 返回 resetSlotsForMode 作用之后的 state（引用已换新）
  return { files: state.files, assetRefs: state.assetRefs,
           slotPreviewUrls: state.slotPreviewUrls,
           audioTrackSource: state.audioTrackSource };
}
export { MODE_ALLOWED_SLOT_KEYS };
export function restore(newState, slot, key, kind, label) {
  state.files = newState.files || {};
  state.assetRefs = newState.assetRefs || {};
  state.slotPreviewUrls = newState.slotPreviewUrls || {};
  state.audioTrackSource = newState.audioTrackSource || '';
  restoreSlotContent(slot, key, kind, label);
  return slot;
}
""")
            cls.tmp = f.name

    @classmethod
    def _run(cls, script: str):
        r = subprocess.run([NODE, "--input-type=module", "-e", script],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise AssertionError("node 执行失败:\n" + r.stderr[:3000])
        return json.loads(r.stdout.strip().splitlines()[-1])

    def _exec(self, script: str):
        return self.__class__._run(
            "import { reset, restore, MODE_ALLOWED_SLOT_KEYS } from '%s';\n"
            % self.__class__.tmp + script)

    # ---------------------------------------------------------------- 白名单

    def test_white_list_is_module_level_single_source(self):
        """白名单必须是**模块级**定义，且只有一份。"""
        src = self.__class__.src
        n_def = len(re.findall(r"const\s+MODE_ALLOWED_SLOT_KEYS\s*=", src))
        self.assertEqual(
            n_def, 1,
            f"MODE_ALLOWED_SLOT_KEYS 必须只有一份定义，实际 {n_def} 份 —— "
            "渲染侧与提交侧共用同一份，否则口径会漂移")

    def test_white_list_covers_all_rendered_slots(self):
        """白名单必须覆盖 renderMode 里真正 mk() 出来的每一个槽位。"""
        src = self.__class__.src
        rendered = set()
        for m in re.finditer(r'mk\(\s*"([a-z]+)"\s*,', src):
            rendered.add(m.group(1))
        for p in set(re.findall(r'mk\(\s*"(ref\w*?)"\s*\+', src)):
            if p.startswith("refvid"):
                rendered.update(f"refvid{i}" for i in range(3))
            elif p.startswith("refaud"):
                rendered.update(f"refaud{i}" for i in range(3))
            elif p.startswith("ref"):
                rendered.update(f"ref{i}" for i in range(9))
        self.assertTrue(rendered, "没解析出任何 mk() 槽位，测试本身失效")
        out = self._exec("""
const keys = %s;
const res = {};
for (const k of keys) {
  res[k] = ['flf','r2v'].filter(m => MODE_ALLOWED_SLOT_KEYS[m] && MODE_ALLOWED_SLOT_KEYS[m](k));
}
console.log(JSON.stringify(res));
""" % json.dumps(sorted(rendered)))
        for key, modes in out.items():
            self.assertTrue(modes,
                            f"槽位 {key} 被 renderMode 渲染，但两个模式的"
                            "白名单都不认它 —— 界面有槽位、提交会挡掉")

    # ---------------------------------------------------------------- 清空语义

    def test_switch_mode_clears_all_slot_state(self):
        """切模式必须清空 assetRefs / audioTrackSource。

        【2026-11-16 需求】本地直传槽位（state.files / state.slotPreviewUrls）
        已被移除 —— 媒体必须先入库素材库。所以现在只剩 assetRefs
        与派生的 audioTrackSource 要清。
        """
        out = self._exec("""
const mk = (n) => ({asset_id: 'A'+n, name: n+'.png', category: 'image'});
const st = () => ({
  files: {first: {__file: true}, refvid0: {__file: true}, refaud0: {__file: true}},
  assetRefs: {first: mk('f'), last: mk('l'), refvid0: mk('v'), refaud0: mk('a')},
  slotPreviewUrls: {first: 'blob:x'},
  audioTrackSource: 'reference',
});
const res = {};
for (const [label, mode] of [['flf_to_r2v','r2v'], ['r2v_to_t2i','t2i'], ['flf_to_flf','flf']]) {
  const s = st();
  // 【必须读 reset() 的返回值】resetSlotsForMode 内部是
  // `state.assetRefs = {}`（**换引用**），不是原地清空 ——
  // 读原来的 s.assetRefs 看到的是旧对象，是**测试替身的假象**。
  const after = reset(s, mode, false);
  res[label] = {files: Object.keys(after.files).sort(), refs: Object.keys(after.assetRefs),
                prev: Object.keys(after.slotPreviewUrls), audio: after.audioTrackSource};
}
console.log(JSON.stringify(res));
""")
        for scenario, r in out.items():
            self.assertEqual(r["refs"], [],
                             f"{scenario}: assetRefs 没清空 -> {r['refs']}")
            self.assertEqual(r["audio"], "",
                             f"{scenario}: audioTrackSource 没复位 -> {r['audio']}")

    def test_switch_mode_removes_local_file_slots_entirely(self):
        """【2026-11-16 需求】state.files 这一路**彻底消失**。

        原来这里锁的是「白名单外的 state.files key 必须被删除」。
        现在本地直传通道整体关闭 —— resetSlotsForMode **不再碰**
        state.files（素材只能从素材库来，没有「本地文件槽位」这回事）。
        """
        out = self._exec("""
const st = () => ({
  files: {first: {n:1}, last: {n:2}, ref0: {n:3}, ref1: {n:4}, refvid0: {n:5}, refaud0: {n:6}},
  assetRefs: {}, slotPreviewUrls: {}, audioTrackSource: '',
});
const res = {};
for (const m of ['flf','r2v','t2v','t2i']) {
  const s = st();
  const after = reset(s, m, false);
  res[m] = Object.keys(after.files).sort();
}
console.log(JSON.stringify(res));
""")
        # state.files 不再被 resetSlotsForMode 触碰 —— 原样保留
        # （那是测试替身注入的旧状态；产品代码里这个字段已不存在）
        for mode, keys in out.items():
            self.assertTrue(keys, f"{mode}: files key 不该被凭空造出来")
            self.assertNotIn("__file", keys)

    def test_keep_slots_preserves_state_for_continuous_submit(self):
        """keepSlots=true（提交成功后的同模式重渲染）必须**保留**状态。

        队列化改造（2026-11-13）明确要求「提交后不再清空参考图/参考帧
        选择」—— 用户要能连续提交多个任务。这里锁住那条契约。
        """
        out = self._exec("""
const mk = (n) => ({asset_id: 'A'+n, name: n+'.png', category: 'image'});
const s = {
  files: {first: {n:1}},
  assetRefs: {first: mk('f')},
  slotPreviewUrls: {first: 'blob:x'},
  audioTrackSource: 'reference',
};
const after = reset(s, 'flf', true);
console.log(JSON.stringify({
  files: Object.keys(after.files), refs: Object.keys(after.assetRefs),
  prev: Object.keys(after.slotPreviewUrls), audio: after.audioTrackSource,
}));
""")
        self.assertEqual(out["refs"], ["first"], "keepSlots 时 assetRefs 被清了")
        self.assertEqual(out["files"], ["first"], "keepSlots 时 files 被清了")
        self.assertEqual(out["prev"], ["first"], "keepSlots 时 previewUrls 被清了")
        self.assertEqual(out["audio"], "reference",
                         "keepSlots 时 audioTrackSource 被复位了")

    # ---------------------------------------------------------------- 回填语义

    def test_restore_slot_refills_from_state(self):
        """重建槽位时必须按 state 如实回填（filled + 缩略图 + previewUrl）。"""
        out = self._exec("""
function mkSlot() {
  const set = new Set();
  return {
    dataset: {},
    classList: {
      add: (c) => { set.add(c); },
      contains: (c) => set.has(c),
    },
    children: [],
    replaceChildren() { this.children = []; },
    append(...n) { this.children.push(...n); },
  };
}
const mk = (n) => ({asset_id: 'A'+n, name: n+'.png', category: 'image'});
const res = {};

let s = {assetRefs: {first: mk('f')}, files: {}, slotPreviewUrls: {}};
let slot = mkSlot();
restore(s, slot, 'first', 'image', '首帧');
res.assetImage = {
  filled: slot.classList.contains('filled'),
  previewUrl: slot.dataset.previewUrl || null,
  tags: slot.children.map(c => c.textContent).filter(Boolean),
  hasImg: slot.children.some(c => c.tagName === 'IMG'),
};

s = {assetRefs: {refvid0: {asset_id: 'V1', name: 'v.mp4', category: 'video'}},
     files: {}, slotPreviewUrls: {}};
slot = mkSlot();
restore(s, slot, 'refvid0', 'video', '参考视频 1');
res.assetVideo = {
  filled: slot.classList.contains('filled'),
  previewUrl: slot.dataset.previewUrl || null,
  tags: slot.children.map(c => c.textContent).filter(Boolean),
};

s = {assetRefs: {}, files: {first: {name: 'x.png'}},
     slotPreviewUrls: {first: 'blob:http://x/1'}};
slot = mkSlot();
restore(s, slot, 'first', 'image', '首帧');
res.localImage = {
  filled: slot.classList.contains('filled'),
  previewUrl: slot.dataset.previewUrl || null,
  tags: slot.children.map(c => c.textContent).filter(Boolean),
  hasImg: slot.children.some(c => c.tagName === 'IMG'),
};

s = {assetRefs: {}, files: {}, slotPreviewUrls: {}};
slot = mkSlot();
restore(s, slot, 'first', 'image', '首帧');
res.empty = {
  filled: slot.classList.contains('filled'),
  previewUrl: slot.dataset.previewUrl || null,
  children: slot.children.length,
};

console.log(JSON.stringify(res));
""")
        self.assertTrue(out["assetImage"]["filled"], "素材库图槽位没标记 filled")
        self.assertIn("/api/assets/Af/content", out["assetImage"]["previewUrl"] or "",
                      f"素材库图槽位 previewUrl 不对 -> {out['assetImage']['previewUrl']}")
        self.assertTrue(out["assetImage"]["hasImg"], "素材库图槽位没有缩略图 <img>")
        self.assertIn("素材库", out["assetImage"]["tags"],
                      f"素材库图槽位缺「素材库」徽标 -> {out['assetImage']['tags']}")
        self.assertTrue(out["assetVideo"]["filled"], "素材库视频槽位没标记 filled")
        self.assertIn("视频", out["assetVideo"]["tags"],
                      f"素材库视频槽位缺类型徽标 -> {out['assetVideo']['tags']}")
        # 【2026-11-16 需求】本地直传通道已移除 —— 只有素材库引用能填槽位。
        # state.files 里还留着本地文件时，槽位**必须仍是空的**，
        # 否则界面会显示一个「已填」的槽位，而提交时并没有那个素材
        # （那正是「界面说谎」类缺陷）。
        self.assertFalse(out["localImage"]["filled"],
                         "本地文件不该能填槽位（媒体必须先入库素材库）")
        self.assertIsNone(out["localImage"]["previewUrl"],
                          "本地文件不该有 previewUrl")
        self.assertFalse(out["empty"]["filled"], "空槽位不该被标记 filled")
        self.assertIsNone(out["empty"]["previewUrl"], "空槽位不该有 previewUrl")


if __name__ == "__main__":
    unittest.main()
