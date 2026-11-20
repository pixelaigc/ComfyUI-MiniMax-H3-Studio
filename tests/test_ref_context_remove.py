# -*- coding: utf-8 -*-
"""参考素材「右键 -> 移除」的回归测试（纯逻辑 + 真实浏览器）。

【为什么要有这个测试】
参考素材被添加到面板后**没有任何移除入口**（视频面板的素材槽位尤其如此）：
用户想换掉其中一条，只能去素材库删素材，或者切模式清空整块。
更糟的是：如果只做「删掉这一条」而不动编号，界面上的「参考 1 / 参考 3」
这种空洞编号会让送进模型的图序与界面编号**对不上**，而且 T8 条件节点
在 strict 模式下会直接硬失败：
    "<Picture 2> is not connected; available picture count is 1"

【覆盖两条链路】
  1. 纯逻辑（node）：web/ref-slots.js 的「同组前移 + 占位标记重写」；
  2. 真浏览器：右键真的出菜单、点「移除」真的生效、提交体里
     不再包含被移除的素材。

【为什么要从源码抽实现，而不是在测试里重写一遍】
测试若抄一份实现，改源码里的那一行测试根本跑不到 -> 断言假通过
（变异测试实测踩过这个坑，见 docs/代码审查报告-20261115.md）。
所以这里**从 web/ref-slots.js 里抽真实的实现**再跑。

运行：
    python3 -m pytest tests/test_ref_context_remove.py -v
需要：node（纯逻辑部分缺失时整类 skip）+ playwright（浏览器部分缺失时整类 skip）
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

NODE = shutil.which("node")

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:                                   # pragma: no cover
    sync_playwright = None
    HAS_PLAYWRIGHT = False


def _run_node(script: str):
    """在 node 里跑一段 JS，返回它的 JSON 结果。"""
    proc = subprocess.run(
        [NODE, "-e", script],
        cwd=str(REPO), capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"node 执行失败：\n{proc.stdout}\n{proc.stderr}")
    return json.loads(proc.stdout)


#: 加载真实实现的引导片段（从 web/ref-slots.js 抽出来，不重写）
_LOADER = """
const fs = require('fs');
const src = fs.readFileSync('web/ref-slots.js', 'utf-8');
const win = {};
const api = new Function('window', src + '\\nreturn window.RefSlots;')(win);
"""


@unittest.skipUnless(NODE, "没有 node，跳过（需要真跑 web/ref-slots.js）")
class TestRefSlotsLogic(unittest.TestCase):
    """web/ref-slots.js 的纯逻辑：同组前移 + 占位标记重写。"""

    def _api(self, body: str):
        return _run_node(_LOADER + body)

    # ------------------------------------------------------------ 移除语义

    def test_remove_middle_compacts_and_renumbers(self):
        """删中间一条：后面的前移，编号连续无空洞。"""
        out = self._api("""
const refs = {ref0:{asset_id:'A'}, ref1:{asset_id:'B'}, ref2:{asset_id:'C'}};
const plan = api.planRemoval('r2v', refs, '', 'ref1');
api.applyRemoval(refs, 'ref1');
const order = api.mediaOrder('r2v', refs, '');
console.log(JSON.stringify({
  refs: Object.keys(refs),
  ids: order.pictures.map(e => e.assetId),
  ns: order.pictures.map(e => e.n),
}));
""")
        self.assertEqual(["ref0", "ref1"], out["refs"],
                         "删中间一条后应该剩两个槽位（ref0/ref1），不能留 ref2 空洞")
        self.assertEqual(["A", "C"], out["ids"],
                         "原来第 3 张（C）应该前移到 ref1")
        self.assertEqual([1, 2], out["ns"], "编号必须连续（1、2），不能是 1、3")

    def test_remove_first_shifts_everything_down(self):
        """删第一条：后面全部前移一位。"""
        out = self._api("""
const refs = {ref0:{asset_id:'A'}, ref1:{asset_id:'B'}, ref2:{asset_id:'C'}};
const plan = api.planRemoval('r2v', refs, '', 'ref0');
api.applyRemoval(refs, 'ref0');
const order = api.mediaOrder('r2v', refs, '');
console.log(JSON.stringify({
  refs: Object.keys(refs), ids: order.pictures.map(e => e.assetId),
  ns: order.pictures.map(e => e.n),
}));
""")
        self.assertEqual(["ref0", "ref1"], out["refs"])
        self.assertEqual(["B", "C"], out["ids"])
        self.assertEqual([1, 2], out["ns"])

    def test_remove_last_keeps_others(self):
        """删最后一条：前面不动。"""
        out = self._api("""
const refs = {ref0:{asset_id:'A'}, ref1:{asset_id:'B'}, ref2:{asset_id:'C'}};
const plan = api.planRemoval('r2v', refs, '', 'ref2');
api.applyRemoval(refs, 'ref2');
const order = api.mediaOrder('r2v', refs, '');
console.log(JSON.stringify({
  refs: Object.keys(refs), ids: order.pictures.map(e => e.assetId),
}));
""")
        self.assertEqual(["ref0", "ref1"], out["refs"])
        self.assertEqual(["A", "B"], out["ids"])

    def test_insertion_order_matches_slot_order(self):
        """提交侧按 Object.entries 的**插入顺序**编 ordinal。

        所以 state 的插入顺序必须恒等于槽位号顺序 —— 否则送进模型的
        第 1 张图是界面上的「参考 2」，图序与界面编号对不上
        （而且极难察觉）。
        """
        out = self._api("""
const refs = {ref0:{asset_id:'A'}, ref1:{asset_id:'B'}, ref2:{asset_id:'C'}};
api.planRemoval('r2v', refs, '', 'ref0');
api.applyRemoval(refs, 'ref0');
console.log(JSON.stringify({keys: Object.keys(refs)}));
""")
        self.assertEqual(["ref0", "ref1"], out["keys"],
                         "state 的键插入顺序必须就是槽位号顺序")

    # ------------------------------------------------------------ 占位标记

    def test_removed_marker_is_deleted_not_left_dangling(self):
        """被移除那条的标记必须**删掉**，不能留悬空。"""
        out = self._api("""
const refs = {ref0:{asset_id:'A'}, ref1:{asset_id:'B'}, ref2:{asset_id:'C'}};
const plan = api.planRemoval('r2v', refs, '', 'ref1');
api.applyRemoval(refs, 'ref1');
const out = api.rewriteTags(
  'A is <Picture 1>, B is <Picture 2>, C is <Picture 3>', plan);
console.log(JSON.stringify({text: out}));
""")
        text = out["text"]
        # 删掉第 2 张后：第 1 张仍是 <Picture 1>，第 3 张前移成 <Picture 2>。
        # 原来那条（第 2 张）的标记必须**整个消失**。
        self.assertEqual("A is <Picture 1>, B is , C is <Picture 2>", text,
                         "被移除那条的标记必须删掉，其余按新序号重写")
        self.assertNotIn("C is <Picture 3>", text,
                         "剩下的第 3 张现在��第 2 张，标记必须跟着改写")

    def test_remaining_markers_are_renumbered(self):
        """剩下的标记必须按**新序号**重写，不能沿用旧序号。"""
        out = self._api("""
const refs = {ref0:{asset_id:'A'}, ref1:{asset_id:'B'}, ref2:{asset_id:'C'}};
const plan = api.planRemoval('r2v', refs, '', 'ref0');
api.applyRemoval(refs, 'ref0');
const out = api.rewriteTags(
  'A=<Picture 1> B=<Picture 2> C=<Picture 3>', plan);
console.log(JSON.stringify({text: out}));
""")
        text = out["text"]
        # 删掉第 1 张后：B 是新的第 1 张，C 是新的第 2 张
        self.assertIn("B=<Picture 1>", text, f"B 应改成 <Picture 1>，实际 {text!r}")
        self.assertIn("C=<Picture 2>", text, f"C 应改成 <Picture 2>，实际 {text!r}")
        self.assertNotIn("<Picture 3>", text,
                         "只剩 2 张图，不该再有 <Picture 3>（那是悬空引用）")

    def test_flf_frame_markers_are_renumbered(self):
        """首尾帧：删掉首帧后，尾帧从 <Picture 2> 变成 <Picture 1>。"""
        out = self._api("""
const refs = {first:{asset_id:'F'}, last:{asset_id:'L'}};
const plan = api.planRemoval('flf', refs, '', 'first');
api.applyRemoval(refs, 'first');
const out = api.rewriteTags('F=<Picture 1> L=<Picture 2>', plan);
console.log(JSON.stringify({text: out, keys: Object.keys(refs)}));
""")
        self.assertEqual(["last"], out["keys"], "删掉首帧后只剩尾帧")
        self.assertIn("L=<Picture 1>", out["text"],
                      f"尾帧应改成 <Picture 1>，实际 {out['text']!r}")
        self.assertNotIn("<Picture 2>", out["text"],
                         "只剩 1 张，不该再有 <Picture 2>")

    def test_video_and_audio_markers_are_renumbered(self):
        """参考视频 / 参考音乐用 <Video N> / <Audio N>，也要跟着重写。

        删掉第 1 个参考视频后：剩下的那个视频从 <Video 2> 前移成 <Video 1>；
        参考音乐是**另一种类型**，编号独立，不受影响。
        """
        out = self._api("""
const refs = {refvid0:{asset_id:'V1'}, refvid1:{asset_id:'V2'},
              refaud0:{asset_id:'A1'}, refaud1:{asset_id:'A2'}};
const plan = api.planRemoval('r2v', refs, 'reference', 'refvid0');
api.applyRemoval(refs, 'refvid0');
const out = api.rewriteTags(
  'V1=<Video 1> V2=<Video 2> A1=<Audio 1> A2=<Audio 2>', plan);
console.log(JSON.stringify({text: out}));
""")
        text = out["text"]
        self.assertNotIn("V1=<Video 1>", text,
                         "被移除的那个参考视频的标记必须删掉")
        self.assertIn("V2=<Video 1>", text,
                      f"剩下的参考视频应改成 <Video 1>，实际 {text!r}")
        self.assertNotIn("<Video 2>", text,
                         "只剩 1 个参考视频，不该再有 <Video 2>")
        self.assertIn("A1=<Audio 1>", text,
                      "参考音乐不受参考视频移除影响（不同类型分开计数）")
        self.assertIn("A2=<Audio 2>", text, "参考音乐编号也不受影响")

    def test_removing_reference_video_drops_its_soundtrack_marker(self):
        """音轨来源是「参考视频背景音」时，移除视频 -> 它的背景音标记也删掉。

        （背景音是从视频里抽出来的，视频没了就没了。）
        """
        out = self._api("""
const refs = {refvid0:{asset_id:'V1'}, refaud0:{asset_id:'A1'}};
const plan = api.planRemoval('r2v', refs, 'video_bg', 'refvid0');
api.applyRemoval(refs, 'refvid0');
const out = api.rewriteTags('V1=<Video 1> A1=<Audio 1>', plan);
console.log(JSON.stringify({text: out}));
""")
        self.assertNotIn("<Video 1>", out["text"], "被移除视频的标记必须删掉")
        self.assertNotIn("<Audio 1>", out["text"],
                         "视频没了，它的背景音也没了 -> <Audio 1> 必须删掉")

    def test_qwen_image_marker_style_is_preserved(self):
        """Qwen 风格的 <image1>（小写、无空格）在视频模式重写后**写法不变**。

        【为什么这条重要】Qwen 的占位正则是 `/<image(\\d+)>/` ——
        小写、无空格。如果视频模式的重写把它统一改成 "<Picture 1>"，
        用户切回 Qwen 模式后那个正则扫不到 -> 整条提示词被判成
        「缺少 <image1> 占位标记」-> 合法提交被误拒。
        所以这里只换**数字**，类型文字与分隔符原样保留。

        【本例的具体语义】删掉第 1 张图后：
          * 原来第 1 张的 <image1> 被删掉（那张图没了）；
          * 原来第 2 张的 <image2> 前移成 <image1>（写法仍是 <image1>）。
        """
        out = self._api("""
const refs = {ref0:{asset_id:'A'}, ref1:{asset_id:'B'}};
const plan = api.planRemoval('r2v', refs, '', 'ref0');
api.applyRemoval(refs, 'ref0');
const out = api.rewriteTags('qwen <image1> and <image2>', plan);
console.log(JSON.stringify({text: out}));
""")
        self.assertEqual("qwen and <image1>", out["text"],
                         "只该换数字（<image2> -> <image1>），写法必须原样保留")

    def test_video_mode_does_not_touch_image_marker_when_unrelated(self):
        """视频模式里 <imageN> 与 <Picture N> 同义（T8 会规范化），
        所以它会跟着重写 —— 但**写法**必须保留（仍是 <image1> 形态）。

        删掉唯一那张参考图后，它的 <image1> 标记必须被删掉
        （那张图已经不存在了，留着就是悬空引用）。
        """
        out = self._api("""
const refs = {ref0:{asset_id:'A'}};
const plan = api.planRemoval('r2v', refs, '', 'ref0');
api.applyRemoval(refs, 'ref0');
const out = api.rewriteTags('只有 <image1>', plan);
console.log(JSON.stringify({text: out}));
""")
        self.assertEqual("只有 ", out["text"],
                         "唯一的图被移除后，它的 <image1> 标记必须删掉")

    def test_out_of_range_marker_is_left_alone(self):
        """用户手写超范围的标记原样保留（由 T8 自己报错，不前端偷偷改）。"""
        out = self._api("""
const refs = {ref0:{asset_id:'A'}};
const plan = api.planRemoval('r2v', refs, '', 'ref0');
api.applyRemoval(refs, 'ref0');
const out = api.rewriteTags('has <Picture 5>', plan);
console.log(JSON.stringify({text: out}));
""")
        self.assertEqual("has <Picture 5>", out["text"],
                         "超出范围的标记不该被前端改写")

    def test_plan_is_null_for_empty_slot(self):
        """空槽位没有可移除的东西 -> 不产生计划（调用方据此直接返回）。"""
        out = self._api("""
const refs = {ref0:{asset_id:'A'}};
const plan = api.planRemoval('r2v', refs, '', 'ref5');
console.log(JSON.stringify({plan: plan === null}));
""")
        self.assertTrue(out["plan"], "空槽位必须返回 null")

    def test_plan_does_not_mutate_before_numbering(self):
        """计划里的 before 必须是**旧序号**（不能被 after 覆盖）。

        反向夹具：如果 planRemoval 原地改 entry.n，before 的旧序号就
        丢了 -> rewriteTags 按「before 第 n 个是谁」找错人 -> 序号整体
        错位一位。这条断言直接锁住它。
        """
        out = self._api("""
const refs = {ref0:{asset_id:'A'}, ref1:{asset_id:'B'}, ref2:{asset_id:'C'}};
const plan = api.planRemoval('r2v', refs, '', 'ref0');
console.log(JSON.stringify({
  beforeNs: plan.before.pictures.map(e => e.n),
  afterNs: plan.after.pictures.map(e => e.n),
}));
""")
        self.assertEqual([1, 2, 3], out["beforeNs"],
                         "before 必须保留旧序号 1、2、3")
        self.assertEqual([1, 2], out["afterNs"],
                         "after 必须是重新编号后的 1、2")


@unittest.skipUnless(NODE and HAS_PLAYWRIGHT, "缺 node 或 playwright，跳过")
class TestRefContextRemoveBrowser(unittest.TestCase):
    """真实浏览器：右键出现、移除生效、提交不含被移除素材。"""

    @classmethod
    def setUpClass(cls):
        from test_frontend_defaults import StudioServer
        import struct
        import zlib

        cls.tmp = Path(tempfile.mkdtemp(prefix="refctx-browser-"))
        cls.root = cls.tmp / "library"
        cls._old_root = os.environ.get("ASSET_LIBRARY_ROOT")
        os.environ["ASSET_LIBRARY_ROOT"] = str(cls.root)

        def _make_png(path: Path, rgb, size=(64, 64)):
            w, h = size
            raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

            def chunk(tag, data):
                return (struct.pack(">I", len(data)) + tag + data
                        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw))
                + chunk(b"IEND", b""))
            return path

        from h3studio import assets as A
        from h3studio import asset_service as AS
        A.init_db()
        conn = A.connect()
        try:
            for i, rgb in enumerate([(220, 120, 40), (60, 110, 200),
                                     (40, 160, 90)], 1):
                src = _make_png(cls.tmp / "src" / f"pic{i}.png", rgb)
                AS.register_asset(
                    conn, source_path=src, filename=f"pic{i}.png",
                    category="image", subtype="photo",
                    tags=[f"测试{i}"], note=f"第 {i} 张测试图", source="upload")
            conn.commit()
        finally:
            conn.close()

        cls.server = StudioServer()
        assert cls.server.start(), "Studio 实例启动失败"
        cls.url = f"http://127.0.0.1:{cls.server.port}"

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "server", None):
            cls.server.stop()
        if getattr(cls, "_old_root", None) is None:
            os.environ.pop("ASSET_LIBRARY_ROOT", None)
        else:
            os.environ["ASSET_LIBRARY_ROOT"] = cls._old_root
        shutil.rmtree(getattr(cls, "tmp", "/tmp/nonexistent"), ignore_errors=True)

    def _open(self, mode):
        """打开页面 -> 切到指定模式 -> 返回 page。"""
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_context(viewport={"width": 1600, "height": 1100}).new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(self.url, wait_until="networkidle")
        page.wait_for_timeout(800)
        # 【注意】视频导航项是 <button class="nav-item" data-mode=...>（由
        # web/video-nav.js 按服务端下发的模型清单渲染），不是 <a>。
        # 用 .nav-item[data-mode=...] 这个**类**选择器才能同时命中
        # <a>（旧导航）与 <button>（视频导航）。
        page.click(f'.nav-item[data-mode="{mode}"]')
        page.wait_for_timeout(900)
        return pw, browser, page, errors

    # ------------------------------------------------------------ 右键出现

    def test_right_click_on_filled_slot_opens_remove_menu(self):
        """已填的素材槽位右键 -> 弹出含「移除」的菜单。"""
        pw, browser, page, errors = self._open("r2v")
        try:
            # 直接把素材塞进「参考 1」槽位（走真实 state.assetRefs 通道）
            page.evaluate("""async () => {
                const data = await fetch('/api/refs/assets?category=image&page_size=1')
                    .then(r => r.json());
                const id = (data.assets || [])[0].id;
                window.__h3State.assetRefs.ref0 =
                    {asset_id: id, name: '测试图.png', category: 'image'};
                window.renderModeKeepSlots();
            }""")
            page.wait_for_timeout(600)

            slot = page.query_selector('#uploadRow .up-slot[data-key="ref0"]')
            self.assertIsNotNone(slot, "找不到「参考 1」槽位")
            self.assertTrue("filled" in (slot.get_attribute("class") or ""),
                            "槽位没有被回填（filled class 缺失）")

            # 真右键
            slot.click(button="right")
            page.wait_for_timeout(400)

            menu = page.query_selector("#refContextMenu")
            self.assertIsNotNone(menu, "右键菜单容器不存在")
            self.assertTrue("open" in (menu.get_attribute("class") or ""),
                            "右键后菜单没有打开")
            labels = page.evaluate(
                "() => [...document.querySelectorAll('#refContextMenu button')]"
                ".map(e => e.textContent.trim())")
            self.assertTrue(any("移除" in t for t in labels),
                            f"菜单里没有「移除」项，实际 {labels}")
            self.assertEqual(errors, [], f"控制台有报错：{errors}")
        finally:
            browser.close()
            pw.stop()

    def test_right_click_on_empty_slot_shows_no_menu(self):
        """空槽位右键不该弹「移除」菜单（没有可移除的对象）。"""
        pw, browser, page, errors = self._open("r2v")
        try:
            slot = page.query_selector('#uploadRow .up-slot[data-key="ref1"]')
            self.assertIsNotNone(slot, "找不到「参考 2」槽位")
            slot.click(button="right")
            page.wait_for_timeout(300)
            menu = page.query_selector("#refContextMenu")
            opened = "open" in (menu.get_attribute("class") or "")
            self.assertFalse(opened, "空槽位右键不该弹出移除菜单")
            self.assertEqual(errors, [], f"控制台有报错：{errors}")
        finally:
            browser.close()
            pw.stop()

    # ------------------------------------------------------------ 移除生效

    def test_remove_via_context_menu_updates_state_and_prompt(self):
        """点「移除」-> 素材没了、编号压缩、提示词占位标记同步。"""
        pw, browser, page, errors = self._open("r2v")
        try:
            page.evaluate("""async () => {
                const data = await fetch('/api/refs/assets?category=image&page_size=3')
                    .then(r => r.json());
                const ids = (data.assets || []).map(a => a.id);
                const st = window.__h3State;
                st.assetRefs.ref0 = {asset_id: ids[0], name: 'A.png', category: 'image'};
                st.assetRefs.ref1 = {asset_id: ids[1], name: 'B.png', category: 'image'};
                st.assetRefs.ref2 = {asset_id: ids[2], name: 'C.png', category: 'image'};
                window.renderModeKeepSlots();
                document.querySelector('#prompt').value =
                    'A=<Picture 1> B=<Picture 2> C=<Picture 3>';
            }""")
            page.wait_for_timeout(600)

            # 右键第 2 张 -> 菜单 -> 移除
            slot = page.query_selector('#uploadRow .up-slot[data-key="ref1"]')
            self.assertIsNotNone(slot, "找不到「参考 2」槽位")
            slot.click(button="right")
            page.wait_for_timeout(300)
            btn = page.query_selector('#refContextMenu button[data-act="remove-ref"]')
            self.assertIsNotNone(btn, "菜单里没有移除按钮")
            btn.click()
            page.wait_for_timeout(700)

            out = page.evaluate("""() => {
                const st = window.__h3State;
                const slots = [...document.querySelectorAll('#uploadRow .up-slot')]
                    .map(s => ({
                        key: s.dataset.key,
                        filled: (s.getAttribute('class') || '').includes('filled'),
                    }));
                return {
                    keys: Object.keys(st.assetRefs),
                    ids: Object.values(st.assetRefs).map(r => r.asset_id),
                    prompt: document.querySelector('#prompt').value,
                    slots,
                };
            }""")
            self.assertEqual(["ref0", "ref1"], out["keys"],
                             f"移除后槽位应剩 ref0/ref1，实际 {out['keys']}")
            self.assertEqual(2, len(out["ids"]), "应剩 2 个素材")
            # 删掉第 2 张后：第 1 张仍是 <Picture 1>，第 3 张前移成 <Picture 2>。
            # 原来第 2 张的标记必须**整个消失**（不能留悬空 <Picture 2>）。
            self.assertNotIn("B=<Picture 2>", out["prompt"],
                             f"被移除那条的标记必须删掉，实际 {out['prompt']!r}")
            self.assertIn("C=<Picture 2>", out["prompt"],
                          "原来第 3 张前移后应是 <Picture 2>")
            self.assertNotIn("C=<Picture 3>", out["prompt"],
                             f"第 3 张的标记必须改成 <Picture 2>，实际 {out['prompt']!r}")
            self.assertIn("A=<Picture 1>", out["prompt"], "第 1 张的标记必须还在")
            # 界面上：参考 3 槽位必须变回空槽（不能还显示着旧缩略图）
            ref2 = [s for s in out["slots"] if s["key"] == "ref2"]
            self.assertTrue(ref2, "找不到参考 3 槽位")
            self.assertFalse(ref2[0]["filled"],
                             "参考 3 槽位应变空（不能残留旧素材）")
            self.assertEqual(errors, [], f"控制台有报错：{errors}")
        finally:
            browser.close()
            pw.stop()

    def test_submit_payload_excludes_removed_asset(self):
        """提交体里不能再有被移除的素材（这才是「移除生效」的落点）。"""
        pw, browser, page, errors = self._open("r2v")
        try:
            page.evaluate("""async () => {
                const data = await fetch('/api/refs/assets?category=image&page_size=3')
                    .then(r => r.json());
                const ids = (data.assets || []).map(a => a.id);
                window.__captured = {ids, body: null};
                const origFetch = window.fetch;
                window.fetch = function (url, opts) {
                    if (String(url).includes('/api/jobs') && opts
                        && opts.method === 'POST') {
                        // 【FormData 已经是 body 了】直接把它存下来；
                        // 不能 new FormData(body) —— 那要求 body 是
                        // HTMLFormElement，FormData 本身会抛 TypeError。
                        window.__captured.body = opts.body;
                        // 不真的提交（避免起真任务），返回一个假响应。
                        // 【注意】返回的 job id 是假的 —— 前端会去轮询
                        // /api/jobs/<id>，那会 404 并在控制台留下
                        // "Failed to load resource" 噪声。所以下面那条
                        // 断言只查**真正的 JS 报错**，不查资源加载噪声。
                        return Promise.resolve(new Response(
                            JSON.stringify({id: 'fake-job'}), {status: 200}));
                    }
                    return origFetch.apply(this, arguments);
                };
                const st = window.__h3State;
                st.assetRefs.ref0 = {asset_id: ids[0], name: 'A.png', category: 'image'};
                st.assetRefs.ref1 = {asset_id: ids[1], name: 'B.png', category: 'image'};
                window.renderModeKeepSlots();
            }""")
            page.wait_for_timeout(600)

            # 移除第 1 张
            slot = page.query_selector('#uploadRow .up-slot[data-key="ref0"]')
            slot.click(button="right")
            page.wait_for_timeout(300)
            page.query_selector('#refContextMenu button[data-act="remove-ref"]').click()
            page.wait_for_timeout(600)

            # 点生成 -> 抓提交体
            page.evaluate("""() => {
                document.querySelector('#prompt').value = '测试提示词';
                document.querySelector('#genBtn').click();
            }""")
            page.wait_for_timeout(1200)

            out = page.evaluate("""() => {
                const body = window.__captured.body;
                if (!body) return {captured: false};
                const raw = body.get('asset_refs');
                return {captured: true, assetRefs: raw ? JSON.parse(raw) : null};
            }""")
            self.assertTrue(out["captured"], "没有抓到提交体（生成按钮没触发？）")
            refs = out["assetRefs"] or []
            self.assertEqual(1, len(refs),
                             f"提交体里应只剩 1 条参考素材，实际 {refs}")
            self.assertEqual("ref_image", refs[0]["slot"])
            self.assertEqual(0, refs[0]["ordinal"], "剩下的应是组内第 1 个")
            # 【只查真正的 JS 报错】资源加载噪声（404）不算 ——
            # 上面那个假 job id 会被前端轮询，必然 404。
            js_errors = [e for e in errors if "Failed to load resource" not in e]
            self.assertEqual(js_errors, [], f"控制台有 JS 报错：{js_errors}")
        finally:
            browser.close()
            pw.stop()

    # ------------------------------------------------------------ Qwen 面板

    def test_qwen_ref_item_right_click_opens_remove_menu(self):
        """Qwen 图片编辑的参考图条目右键 -> 也能出「移除」菜单。"""
        pw, browser, page, errors = self._open("i2i_qwen")
        try:
            page.evaluate("""async () => {
                const data = await fetch('/api/refs/assets?category=image&page_size=2')
                    .then(r => r.json());
                const ids = (data.assets || []).map(a => a.id);
                await window.QwenRefs.addAssets(ids);
                document.querySelector('#prompt').value =
                    'A=<image1> B=<image2>';
            }""")
            page.wait_for_timeout(800)

            items = page.query_selector_all("#imgRefList .img-ref-item")
            self.assertGreaterEqual(len(items), 2, "至少要能加进 2 张素材")

            items[1].click(button="right")
            page.wait_for_timeout(400)

            menu = page.query_selector("#refContextMenu")
            self.assertTrue("open" in (menu.get_attribute("class") or ""),
                            "右键后菜单没有打开")
            btn = page.query_selector('#refContextMenu button[data-act="remove-ref"]')
            self.assertIsNotNone(btn, "菜单里没有移除按钮")

            btn.click()
            page.wait_for_timeout(600)

            out = page.evaluate("""() => ({
                count: window.QwenRefs.getRefs().length,
                prompt: document.querySelector('#prompt').value,
                idx: [...document.querySelectorAll('#imgRefList .img-ref-idx')]
                      .map(e => e.textContent.trim()),
            })""")
            self.assertEqual(1, out["count"], "移除后应剩 1 张参考图")
            self.assertEqual(["1"], out["idx"], "序号应重排为 1")
            self.assertNotIn("<image2>", out["prompt"],
                             f"被移除那条的占位标记必须删掉，实际 {out['prompt']!r}")
            self.assertIn("<image1>", out["prompt"], "剩下那张的标记必须还在")
            self.assertEqual(errors, [], f"控制台有报错：{errors}")
        finally:
            browser.close()
            pw.stop()


if __name__ == "__main__":
    unittest.main()
