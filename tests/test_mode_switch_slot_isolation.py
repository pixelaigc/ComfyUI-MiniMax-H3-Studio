# -*- coding: utf-8 -*-
"""回归测试：跨模式切换后，提交值必须与面板显示值一致（槽位不串台）。

【缺陷（2026-11-16 第 3 轮代码审查发现，Critical）】
上一轮把「槽位状态跨模式残留」标为「未证实故未改」。本轮用**真机
浏览器 + 抓取真实提交体**复现出来了：

    步骤 1) 切到「首尾帧生视频」
    步骤 2) 在首帧/尾帧槽位各选一个素材
    步骤 3) 切到「参考生视频」
    步骤 4) 点「生成」

抓到的实际提交体（8190 现场实测）：

    mode       = r2v
    asset_refs = [{"asset_id":"FAKE-1","slot":"first_frame","ordinal":0},
                  {"asset_id":"FAKE-2","slot":"last_frame","ordinal":0}]

而此时 r2v 界面上**根本没有首帧槽位** —— 实测 uiSlots 是
`["参考 1".."参考 9", "参考视频 1..3", "参考音乐 1..3"]`，
`hasFirstSlotInUI = false`。

后果链：
  1. 前端守卫（app.js:3355）按「有素材引用」判定为「已提供参考素材」-> 放行；
  2. 服务端（server.py:3715）按 slot 判定（只认 ref_image/ref_video/ref_audio）
     -> 拒绝，报「参考模式需要至少 1 个参考素材（图片 / 视频 / 音频）」。
  用户看到的是：界面一片空白（自己没选任何参考素材），
  点生成却被告知「缺少参考素材」—— **界面与报错互相矛盾，且无从修正**
  （因为界面上根本没有可以选参考素材的地方被指出来）。

根因：`state.assetRefs` 是全局的，切模式时从不清空
（`renderMode` 只重建 DOM）；而提交处
`Object.entries(state.assetRefs || {}).forEach(...)` **无模式判断全量提交**。

【修法】提交处按当前模式的白名单过滤槽位 key，口径与 `renderMode`
里真正 `mk()` 出来的槽位严格一致：
    flf -> first / last / refvid0..2 / refaud0..2
    r2v -> ref0..8 / refvid0..2 / refaud0..2
    其它 -> 无

【测试方法】
不写「源码里有没有某行字」这种弱断言 —— 那样测的是「代码长什么样」，
不是「行为对不对」。这里用 **node 真正跑一遍 app.js 里的真实实现**：
  1) 从 web/app.js 里**抽出真实的槽位过滤逻辑**（含 MODE_ALLOWED_SLOT_KEYS
     / SLOT_FOR_KEY / GROUP_OF_KEY 三段，用花括号配平法精确抽取）；
  2) 复刻提交链路里 `assetRefs` 的构建过程；
  3) 断言跨模式切换后构建出的 asset_refs 与**当前模式界面上的槽位**一致。
这样「把模式闸门删掉」或「改成别的模式口径」都会被抓住。

运行：
    python3 -m pytest tests/test_mode_switch_slot_isolation.py -q --tb=short
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


def _extract_block(src: str, start_marker: str) -> str:
    """从 start_marker 处抽出花括号配平的整个代码块（含起止大括号）。

    【为什么要跳过字符串/注释】JS 里 ``{`` 出现在字符串或注释中时
    不参与配平。不处理的话，`const SLOT_FOR_KEY = { first: "first_frame",
    last: "last_frame", }` 会被截成 ``{ first: "first_frame",
    last: "last_frame", `` —— 正好是刚才踩的坑。
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
        # 跳过字符串
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
        # 跳过行注释 / 块注释
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


def _extract_statement(src: str, marker: str) -> str:
    """抽出以 marker 开头的一整条单行语句（到行尾，去掉缩进）。

    用于取 `const slotKeyAllowed = ...;` 这种单行声明。
    """
    idx = src.find(marker)
    if idx == -1:
        raise AssertionError(f"app.js 里找不到：{marker!r}")
    line_end = src.find("\n", idx)
    if line_end == -1:
        line_end = len(src)
    line = src[idx:line_end].rstrip("\r")
    if not line.endswith(";"):
        line += ";"
    return line


def extract_asset_refs_builder(src: str) -> str:
    """抽出 app.js 里真实构建 asset_refs 的那段逻辑。

    做法：找到 `const assetRefs = [];` 之后，依次抽出
    SLOT_FOR_KEY / GROUP_OF_KEY / MODE_ALLOWED_SLOT_KEYS 三个声明，
    再把 `Object.entries(state.assetRefs ...)` 那个 forEach 循环体抽出来。
    最后拼成一个可独立执行的函数 `buildAssetRefs(state, mode)`。
    """
    anchor = src.find("const assetRefs = [];")
    if anchor == -1:
        raise AssertionError("app.js 里找不到 `const assetRefs = [];`")

    def _decl(marker: str) -> str:
        """抽出一条完整声明（从 `const X =` 到语句末尾的分号）。

        只抽花括号块会把 `const SLOT_FOR_KEY = {...}` 的声明头丢掉，
        拼出来就是 `{ first: ..., last: ... }` —— 语法错误。
        """
        blk = _extract_block(src, marker)
        start = src.rfind("const ", 0, src.index(blk))
        # 从声明头到块结束（块后紧跟的 `;` 或换行）
        end = src.index(blk) + len(blk)
        tail = src[end:end + 2]
        if tail.lstrip().startswith(";"):
            end += tail.index(";") + 1
        return src[start:end]

    slot_for_key = _decl("const SLOT_FOR_KEY =")
    group_of_key = _decl("const GROUP_OF_KEY =")
    mode_allowed = _decl("const MODE_ALLOWED_SLOT_KEYS =")

    # forEach 循环：找 `Object.entries(state.assetRefs || {}).forEach(` 起的
    # 箭头函数体（到匹配的右括号）
    fe = src.find("Object.entries(state.assetRefs || {}).forEach(", anchor)
    if fe == -1:
        raise AssertionError("app.js 里找不到 assetRefs 的 forEach 提交循环")
    arrow = src.index("=>", fe)
    # 箭头函数的参数列表 `([key, item])` 必须在 body 之前一起抽出，
    # 否则拼出来是 `.forEach({ ... })` —— 语法错误。
    params_start = src.rfind("(", fe, arrow)
    params = src[params_start:arrow]
    body_open = src.index("{", arrow)
    depth = 0
    j = body_open
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    forEach_body = params + " => " + src[body_open:j + 1]

    # 【关键】slotKeyAllowed 这一行必须**从源码里抽**，不能在这里写死。
    # 写死的话，改源码里的这一行（比如把 `|| (() => false)` 改成
    # `|| (() => true)`）测试根本跑不到被改的代码 -> 断言假通过。
    # 变异测试实测踩过这个坑（第一次跑：B2-e/B2-f 两个变异漏网）。
    allowed_line = _extract_statement(src, "const slotKeyAllowed =")
    if "MODE_ALLOWED_SLOT_KEYS" not in allowed_line:
        raise AssertionError("抽到的 slotKeyAllowed 声明不对："
                             + allowed_line[:120])

    return (
        "function buildAssetRefs(state, mode) {\n"
        "  const assetRefs = [];\n"
        f"  {slot_for_key}\n"
        f"  {group_of_key}\n"
        f"  {mode_allowed}\n"
        f"  {allowed_line}\n"
        "  const groupCounter = {};\n"
        f"  Object.entries(state.assetRefs || {{}}).forEach({forEach_body})\n"
        "  return assetRefs;\n"
        "}\n"
    )


# 与 app.js 的 renderMode 保持一致的「界面槽位」真值（见 app.js:747-768）
UI_SLOTS = {
    "flf": ["first", "last"] + [f"refvid{i}" for i in range(3)] +
           [f"refaud{i}" for i in range(3)],
    "r2v": [f"ref{i}" for i in range(9)] + [f"refvid{i}" for i in range(3)] +
           [f"refaud{i}" for i in range(3)],
}

# 槽位 key -> 提交用的 slot 名（与 app.js 的 SLOT_FOR_KEY / GROUP_OF_KEY 一致）
KEY_TO_SLOT = {"first": "first_frame", "last": "last_frame"}


def slot_of(key: str) -> str:
    if key in KEY_TO_SLOT:
        return KEY_TO_SLOT[key]
    if re.match(r"^ref\d+$", key):
        return "ref_image"
    if re.match(r"^refvid\d+$", key):
        return "ref_video"
    if re.match(r"^refaud\d+$", key):
        return "ref_audio"
    raise AssertionError(f"未知槽位 key: {key}")


class TestModeSwitchSlotIsolation(unittest.TestCase):
    """跨模式切换后，提交值必须与面板显示值一致。"""

    @classmethod
    def setUpClass(cls):
        if not NODE:
            raise unittest.SkipTest("没有 node，跳过（需要真跑 app.js 逻辑）")
        cls.src = APP_JS.read_text(encoding="utf-8")
        builder = extract_asset_refs_builder(cls.src)
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False,
                                         encoding="utf-8") as f:
            f.write(builder + """
export function build(state, mode) { return buildAssetRefs(state, mode); }
""")
            cls.tmp = f.name
        cls._run("""
import { build } from '%s';
const mk = (n) => ({asset_id: 'A'+n, name: n+'.png', category: 'image'});
const st = (refs) => ({assetRefs: refs});
const out = {};
// 场景：flf 选了首帧+尾帧，然后切到 r2v
const s1 = st({first: mk('f'), last: mk('l')});
out.flf_then_r2v = build(s1, 'r2v');
out.flf_itself = build(s1, 'flf');
// 场景：r2v 选了参考图，然后切到 flf
const s2 = st({ref0: mk('r0'), ref1: mk('r1')});
out.r2v_then_flf = build(s2, 'flf');
out.r2v_itself = build(s2, 'r2v');
// 场景：flf 选了参考视频/参考音乐，切到 r2v —— 这两个槽位两个模式都有
const s3 = st({refvid0: mk('v0'), refaud0: mk('a0')});
out.shared_slots_in_r2v = build(s3, 'r2v');
out.shared_slots_in_flf = build(s3, 'flf');
// 场景：文生视频（无素材槽位）—— 不该带任何素材
out.t2v = build(s1, 't2v');
console.log(JSON.stringify(out));
""" % cls.tmp)

    @classmethod
    def _run(cls, script: str):
        r = subprocess.run([NODE, "--input-type=module", "-e", script],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise AssertionError("node 执行失败:\n" + r.stderr[:2000])
        import json
        return json.loads(r.stdout.strip().splitlines()[-1])

    def _results(self):
        return self.__class__._run("""
import { build } from '%s';
const mk = (n) => ({asset_id: 'A'+n, name: n+'.png', category: 'image'});
const st = (refs) => ({assetRefs: refs});
const out = {};
const s1 = st({first: mk('f'), last: mk('l')});
out.flf_then_r2v = build(s1, 'r2v');
out.flf_itself = build(s1, 'flf');
const s2 = st({ref0: mk('r0'), ref1: mk('r1')});
out.r2v_then_flf = build(s2, 'flf');
out.r2v_itself = build(s2, 'r2v');
const s3 = st({refvid0: mk('v0'), refaud0: mk('a0')});
out.shared_slots_in_r2v = build(s3, 'r2v');
out.shared_slots_in_flf = build(s3, 'flf');
out.t2v = build(s1, 't2v');
console.log(JSON.stringify(out));
""" % self.tmp)

    def test_r2v_submission_excludes_flf_only_slots(self):
        """【核心断言】flf -> r2v 后，提交体里不得有 first_frame / last_frame。

        这正是 8190 现场抓到的那个真实提交体。
        """
        out = self._results()
        slots = [r["slot"] for r in out["flf_then_r2v"]]
        self.assertNotIn("first_frame", slots,
                         "切到 r2v 后仍提交了 first_frame（幽灵首帧输入）")
        self.assertNotIn("last_frame", slots,
                         "切到 r2v 后仍提交了 last_frame（幽灵尾帧输入）")
        self.assertEqual([], out["flf_then_r2v"],
                         "切到 r2v 后本该一个素材都不提交，实际："
                         + str(out["flf_then_r2v"]))

    def test_flf_submission_still_includes_its_own_slots(self):
        """修法不能把 flf 自己的首帧/尾帧也过滤掉。"""
        out = self._results()
        slots = sorted(r["slot"] for r in out["flf_itself"])
        self.assertEqual(["first_frame", "last_frame"], slots,
                         "flf 模式下首帧/尾帧没被提交，会打断现有功能")

    def test_r2v_then_flf_excludes_ref_image_slots(self):
        """反向：r2v 选的参考图，切到 flf 后不得被提交。"""
        out = self._results()
        slots = [r["slot"] for r in out["r2v_then_flf"]]
        self.assertNotIn("ref_image", slots,
                         "切到 flf 后仍提交了参考图（幽灵输入）")
        self.assertEqual([], out["r2v_then_flf"])

    def test_r2v_own_slots_still_submitted(self):
        """r2v 自己的参考图必须照常提交。"""
        out = self._results()
        slots = sorted(r["slot"] for r in out["r2v_itself"])
        self.assertEqual(["ref_image", "ref_image"], slots,
                         "r2v 模式的参考图没被提交，会打断现有功能")

    def test_shared_slots_work_in_both_modes(self):
        """参考视频 / 参考音乐两个模式都有 —— 必须都照常提交。"""
        out = self._results()
        for key in ("shared_slots_in_r2v", "shared_slots_in_flf"):
            slots = sorted(r["slot"] for r in out[key])
            self.assertEqual(["ref_audio", "ref_video"], slots,
                             f"{key} 的共享槽位被误过滤：{slots}")

    def test_t2v_submits_no_assets(self):
        """文生视频没有素材槽位 —— 不能带任何素材引用。"""
        out = self._results()
        self.assertEqual([], out["t2v"],
                         "文生视频不该带素材引用，实际：" + str(out["t2v"]))

    def test_submission_matches_ui_slots_in_every_mode(self):
        """【护栏断言】提交值与面板显示值必须一致。

        口径：把 `state.assetRefs` 里所有槽位都填满，断言每个模式
        构建出的 asset_refs 的 slot 集合 == 该模式界面槽位集合映射后的
        slot 集合。将来有人加新槽位 / 改模式口径，这条会立刻报。
        """
        results = self.__class__._run("""
import { build } from '%s';
const mk = (n) => ({asset_id: 'A'+n, name: n+'.png', category: 'image'});
const all = {
  first: mk('f'), last: mk('l'),
  ref0: mk('r0'), ref1: mk('r1'), ref2: mk('r2'), ref3: mk('r3'),
  ref4: mk('r4'), ref5: mk('r5'), ref6: mk('r6'), ref7: mk('r7'),
  ref8: mk('r8'),
  refvid0: mk('v0'), refvid1: mk('v1'), refvid2: mk('v2'),
  refaud0: mk('a0'), refaud1: mk('a1'), refaud2: mk('a2'),
};
const out = {};
for (const m of ['flf','r2v','t2v']) {
  out[m] = build({assetRefs: all}, m);
}
console.log(JSON.stringify(out));
""" % self.tmp)
        for mode in ("flf", "r2v"):
            expected_slots = set()
            for key in UI_SLOTS[mode]:
                expected_slots.add(slot_of(key))
            got_slots = {r["slot"] for r in results[mode]}
            self.assertEqual(
                expected_slots, got_slots,
                f"{mode} 模式：提交的 slot 集合与界面槽位集合不一致\n"
                f"  界面期望: {sorted(expected_slots)}\n"
                f"  实际提交: {sorted(got_slots)}")
        self.assertEqual([], results["t2v"])

    def test_mode_allowlist_is_actually_present(self):
        """防止有人把模式闸门整段删掉后，测试因「提取失败」而静默通过。"""
        self.assertIn("MODE_ALLOWED_SLOT_KEYS", self.src,
                      "app.js 里找不到 MODE_ALLOWED_SLOT_KEYS —— "
                      "模式闸门被删了？")
        self.assertIn("slotKeyAllowed", self.src,
                      "app.js 里找不到 slotKeyAllowed —— 模式闸门被删了？")


if __name__ == "__main__":
    unittest.main()
