# -*- coding: utf-8 -*-
"""参考图占位标记的**浏览器实测**回归测试（真 Chromium + 真后端）。

背景：8190 线上部署前的真机验证暴露了两个真实缺陷，纯 Python 单测全绿也没抓到：
  1. 描述编辑区的 `<imageN>` 标签**根本没渲染** ——
     `placeholderFor(i)` 返回的 `<image1>` 被浏览器当成 HTML 标签解析，
     textContent 为空。用户在界面上看不到「第几行对应第几张图」。
  2. 移除中间一张参考图后，提示词里**残留悬空 `<image3>`** ——
     原 reindexPrompt() 按「提示词里出现的序号」重排，没考虑被删的那张。

这两个都是纯前端行为，纯 Python 单测抓不到（单测只测后端 ref_images.py）。
所以这里用真实浏览器跑。

运行：python3 -m pytest tests/test_qwen_refs_browser.py -v
需要：playwright（chromium）。缺失时整类 skip，不算失败。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:                                   # pragma: no cover
    sync_playwright = None
    HAS_PLAYWRIGHT = False

from test_frontend_defaults import StudioServer  # noqa: E402


# 断言用的提示词（模拟用户手写占位）
PROMPT_3 = ("把这三张图合成一张：保持 <image1> 的灯塔与黄昏光线，"
            "把 <image2> 的雪山倒影放进 <image1> 的海面，"
            "把 <image3> 的传感器模块放在前景沙滩上。")


def _make_png(path: Path, rgb: tuple[int, int, int], size=(64, 64)) -> Path:
    """写一张最小合法 PNG（不依赖 Pillow）。"""
    import struct
    import zlib

    w, h = size
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw))
           + chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return path


class TestQwenRefsBrowser(unittest.TestCase):
    """参考图面板的浏览器实测（真 Chromium + 真 server.py + 真素材库）。"""

    @classmethod
    def setUpClass(cls):
        if not HAS_PLAYWRIGHT:
            raise unittest.SkipTest("playwright 不可用")

        # 临时素材库：3 张真 PNG，隔离于真实库（不碰 /mnt/usb2t）
        cls.tmp = Path(tempfile.mkdtemp(prefix="qwen-refs-browser-"))
        cls.root = cls.tmp / "library"
        cls._old_root = os.environ.get("ASSET_LIBRARY_ROOT")
        os.environ["ASSET_LIBRARY_ROOT"] = str(cls.root)

        from h3studio import assets as A
        from h3studio import asset_service as AS
        A.init_db()
        conn = A.connect()
        try:
            for i, rgb in enumerate([(220, 120, 40), (60, 110, 200), (40, 160, 90)], 1):
                src = _make_png(cls.tmp / "src" / f"ref{i}.png", rgb)
                AS.register_asset(
                    conn, source_path=src, filename=f"ref{i}.png",
                    category="image", subtype="photo",
                    tags=[f"测试标签{i}"], note=f"第 {i} 张参考图的测试素材",
                    source="upload")
            conn.commit()      # 不 commit -> 服务端子进程看不到素材
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

    def _run(self, script_body):
        """在真实 Chromium 上跑一段 JS，返回 JS 的返回值 + 控制台报错。"""
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_context(viewport={"width": 1600, "height": 1100}).new_page()
            errors = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            try:
                page.goto(self.url, wait_until="networkidle")
                page.wait_for_timeout(800)
                page.click('a.nav-item[data-mode="i2i_qwen"]')
                page.wait_for_timeout(800)
                value = page.evaluate(script_body)
                self.assertEqual(errors, [], f"浏览器控制台有报错: {errors}")
                return value
            finally:
                browser.close()

    # ------------------------------------------------------------ 缺陷 1

    def test_desc_tag_renders_placeholder_text(self):
        """缺陷 1：`<imageN>` 标签必须**真的显示出来**。

        反向夹具（mutation guard）：如果不加 esc()，`<image1>` 会被浏览器
        当成未知 HTML 标签解析 -> textContent 为空 -> 断言失败。
        """
        def body():
            return """(async () => {
                const data = await fetch('/api/refs/assets?category=image&page_size=3')
                    .then(r => r.json());
                const ids = (data.assets || []).map(a => a.id);
                await window.QwenRefs.addAssets(ids);
                const tags = [...document.querySelectorAll('#imgRefDescList .img-ref-desc-tag')]
                    .map(e => e.textContent.trim());
                const html = [...document.querySelectorAll('#imgRefDescList .img-ref-desc-tag')]
                    .map(e => e.innerHTML);
                return {tags, html, count: window.QwenRefs.getRefs().length};
            })()"""

        out = self._run(body())
        self.assertGreaterEqual(out["count"], 2, "至少要能加进 2 张素材")
        self.assertEqual(len(out["tags"]), out["count"], "每张图都要有一个描述行")
        # 关键断言：文本内容必须真的是 <image1> / <image2> ...
        for i, tag in enumerate(out["tags"]):
            self.assertEqual(
                tag, f"<image{i + 1}>",
                f"第 {i + 1} 行的占位标签应显示 <image{i + 1}>，实际 {tag!r}")
        # 反向夹具：innerHTML 里必须是转义后的实体，不是真的标签
        self.assertIn("&lt;image1&gt;", out["html"][0],
                      "占位标签必须是转义文本，不能是被浏览器解析的 HTML 标签")

    # ------------------------------------------------------------ 缺陷 2

    def test_remove_middle_drops_dangling_placeholder(self):
        """缺陷 2：移除中间一张后，提示词里**不能残留**该图的悬空标记。

        3 张图、提示词引用了 <image1><image2><image3>；
        删掉第 2 张 -> 提示词里 <image2> 必须消失，
        剩下 <image1>（原第 1 张）和 <image2>（原第 3 张，重编号后）。
        """
        def body():
            js_prompt = json.dumps(PROMPT_3)
            return """(async () => {
                const data = await fetch('/api/refs/assets?category=image&page_size=3')
                    .then(r => r.json());
                const ids = (data.assets || []).map(a => a.id);
                await window.QwenRefs.addAssets(ids);
                const prompt = document.querySelector('#prompt');
                prompt.value = %s;
                const before = prompt.value;
                // 移除第 2 张（真实点击删除按钮）
                const btns = document.querySelectorAll('#imgRefList [data-del]');
                btns[1].click();
                await new Promise(r => setTimeout(r, 300));
                const after = prompt.value;
                return {
                    before, after,
                    count: window.QwenRefs.getRefs().length,
                    idx: [...document.querySelectorAll('#imgRefList .img-ref-idx')]
                          .map(e => e.textContent.trim()),
                    found: (after.match(/<image\\d+>/g) || []),
                };
            })()""" % js_prompt

        out = self._run(body())
        self.assertEqual(out["count"], 2, "移除后应剩 2 张")
        self.assertEqual(out["idx"], ["1", "2"], "缩略图序号应重排为 1、2")
        # 关键断言：不能有悬空标记（占位号必须连续且 == 图数）
        self.assertEqual(sorted(set(out["found"])), ["<image1>", "<image2>"],
                         f"移除后提示词不应有悬空标记，实际 {out['found']}")
        self.assertNotIn("<image3>", out["after"], "被移除的那张不能留下 <image3>")
        # 悬空标记必须真的被**删掉**，而不是被替换成别的
        self.assertLess(len(out["after"]), len(out["before"]))

    # ------------------------------------------------------------ 排序保持身份

    def test_smart_sort_keeps_placeholders(self):
        """智能排序后，占位标记必须还在（不能被清空）。

        反向夹具：排序返回的是**新对象**，如果没保留身份（uid），
        槽位表对不上 -> reindexPrompt() 会把标记全删。
        """
        def body():
            js_prompt = json.dumps(PROMPT_3)
            return """(async () => {
                const data = await fetch('/api/refs/assets?category=image&page_size=3')
                    .then(r => r.json());
                const ids = (data.assets || []).map(a => a.id);
                await window.QwenRefs.addAssets(ids);
                const prompt = document.querySelector('#prompt');
                prompt.value = %s;
                document.querySelector('#imgRefSort').click();
                await new Promise(r => setTimeout(r, 1500));
                const after = prompt.value;
                return {
                    after,
                    count: window.QwenRefs.getRefs().length,
                    ids: window.QwenRefs.getRefs().map(r => r.asset_id),
                    found: (after.match(/<image\\d+>/g) || []),
                };
            })()""" % js_prompt

        out = self._run(body())
        self.assertEqual(out["count"], 3)
        self.assertTrue(out["found"], "排序后提示词里的占位标记不能被清空")
        self.assertEqual(sorted(set(out["found"])), ["<image1>", "<image2>", "<image3>"],
                         f"排序后占位标记应仍是 1..3，实际 {out['found']}")


if __name__ == "__main__":
    unittest.main()
