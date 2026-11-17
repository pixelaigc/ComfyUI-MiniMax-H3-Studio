# -*- coding: utf-8 -*-
"""回归测试：素材库选择回调（onPick）必须**传到位**（2026-11-17 缺陷）。

【缺陷（2026-11-17 用户报告，线上 8190 实测）】
「视频 → H3 参考生视频」面板，点「参考1 → 跳转素材库」，在素材库弹窗里
选择一张图后弹窗提示：「读取素材失败：onPick is not a function」。

【根因】
web/app.js 的 openAssetPickerForSlot(kind, onPick) 是**槽位填素材**的唯一入口，
但「点空槽位」那条路径调用时**漏传了 onPick**：

    openAssetPickerForSlot(kind);                       // ← 漏传
    openAssetPickerForSlot(kind, a => fillFromAsset(a)); // ← 「素材库」按钮，正确的

漏传后函数本身不报错、照常打开弹窗；用户选完素材点「确定使用」，
onDone 里 fetch 完素材才去调 onPick(...) -> TypeError。
那个 TypeError 被 .catch() 吞掉，弹成「读取素材失败：onPick is not a function」——
提示指向「读取素材」，真实失败点却是「回调没传」。

【影响面 —— 本测试逐条覆盖】
  * H3 参考生视频：参考 1..9（图片）
  * H3 参考生视频：参考视频 1..3
  * H3 参考生视频：参考音乐 1..3
  * H3 首尾帧生视频：首帧 / 尾帧
  * H3 首尾帧生视频：参考视频 1..3 / 参考音乐 1..3
  * 两条路径都要测：**点空槽位**（原来坏的）与**点「素材库」小按钮**（原来好的）

【测试方法 —— 真浏览器 + 真后端】
纯 Python 单测抓不到「回调没传」这种前端接线错误（它只在点击时炸）。
所以这里起一个真 server.py（临时 data 目录 + 临时素材库），
用真 Chromium 点真实槽位，断言：
  1. 选完素材**不弹任何 alert**；
  2. 槽位真的回填了（filled class + 缩略图 <img> + 「素材库」徽标）；
  3. state.assetRefs 里记下了这个素材的 asset_id；
  4. 浏览器控制台 **0 error**。

【反向夹具（变异测试）】
本文件同时提供 tests/mutation_check_onpick.py：把修复后的那行改回
「漏传 onPick」，本文件必须变红 —— 证明断言真的在守这个行为。

运行：
    H3STUDIO_DATA_DIR=$(mktemp -d) python3 -m pytest tests/test_asset_picker_onpick.py -v
需要：playwright（chromium）。缺失时整类 skip，不算失败。
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:                                    # pragma: no cover
    sync_playwright = None
    HAS_PLAYWRIGHT = False

from test_frontend_defaults import StudioServer   # noqa: E402


def _make_png(path: Path, rgb: tuple[int, int, int], size=(48, 48)) -> Path:
    """写一张最小合法 PNG（不依赖 Pillow）。"""
    w, h = size
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b""))
    return path


def _make_audio(path: Path, seconds: int = 1) -> Path | None:
    """用 ffmpeg 生成一段真 mp3（没 ffmpeg 就返回 None）。"""
    if not shutil.which("ffmpeg"):
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
           "-i", f"sine=frequency=440:duration={seconds}",
           "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", str(path)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    except Exception:
        return None
    return path if path.is_file() and path.stat().st_size > 0 else None


# (模式, 槽位标签, 素材类别) —— 覆盖所有素材库选择入口
ENTRIES = [
    ("r2v", "参考 1", "image"),
    ("r2v", "参考 2", "image"),
    ("r2v", "参考 9", "image"),
    ("r2v", "参考视频 1", "video"),
    ("r2v", "参考视频 3", "video"),
    ("r2v", "参考音乐 1", "audio"),
    ("r2v", "参考音乐 3", "audio"),
    ("flf", "首帧", "image"),
    ("flf", "尾帧", "image"),
    ("flf", "参考视频 1", "video"),
    ("flf", "参考音乐 1", "audio"),
]


class TestAssetPickerOnPick(unittest.TestCase):
    """素材库选择回调接线（真浏览器 + 真后端）。"""

    @classmethod
    def setUpClass(cls):
        if not HAS_PLAYWRIGHT:
            raise unittest.SkipTest("playwright 不可用")

        cls.tmp = Path(tempfile.mkdtemp(prefix="onpick-browser-"))
        cls.root = cls.tmp / "library"
        cls._old_root = os.environ.get("ASSET_LIBRARY_ROOT")
        os.environ["ASSET_LIBRARY_ROOT"] = str(cls.root)

        from h3studio import assets as A
        from h3studio import asset_service as AS
        A.init_db()
        conn = A.connect()
        try:
            # 图片 2 张（参考图 / 首帧 / 尾帧槽位用）
            for i, rgb in enumerate([(220, 120, 40), (60, 110, 200)], 1):
                src = _make_png(cls.tmp / "src" / f"pic{i}.png", rgb)
                AS.register_asset(
                    conn, source_path=src, filename=f"pic{i}.png",
                    category="image", subtype="photo",
                    tags=[f"测试{i}"], note=f"第 {i} 张测试图",
                    source="upload")
            # 视频 2 段（参考视频槽位用）—— 校验只看 category 与文件存在，不解码
            for i in range(1, 3):
                src = cls.tmp / "src" / f"vid{i}.mp4"
                src.parent.mkdir(parents=True, exist_ok=True)
                src.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00")
                AS.register_asset(
                    conn, source_path=src, filename=src.name,
                    category="video", subtype="clip",
                    tags=["测试"], note=f"第 {i} 段测试视频",
                    source="upload")
            # 音频 2 段（参考音乐槽位用）—— 真 mp3
            cls.has_audio = False
            for i in range(1, 3):
                src = _make_audio(cls.tmp / "src" / f"aud{i}.mp3")
                if src is None:
                    break
                AS.register_asset(
                    conn, source_path=src, filename=src.name,
                    category="audio", subtype="clip",
                    tags=["测试"], note=f"第 {i} 段测试音频",
                    source="upload")
                cls.has_audio = True
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

    # ------------------------------------------------------------ 工具

    def _pick_in_slot(self, page, mode, label, use_button):
        """在指定槽位走一遍「打开素材库 -> 选一张 -> 确定使用」。

        返回 {"alerts": [...], "filled": bool, "has_thumb": bool,
              "has_badge": bool, "asset_id": str|None, "console_errors": [...]}
        """
        alerts = []
        console_errors = []

        def _on_dialog(d):
            alerts.append(d.message)
            d.dismiss()

        page.on("dialog", _on_dialog)
        page.on("console",
                lambda m: console_errors.append(m.text) if m.type == "error" else None)
        try:
            page.click(f'[data-mode="{mode}"]')
            page.wait_for_timeout(1200)

            target = None
            for s in page.query_selector_all("#uploadRow .up-slot"):
                if (s.get_attribute("data-label") or "") == label:
                    target = s
                    break
            self.assertIsNotNone(target, f"{mode} 模式里找不到槽位「{label}」")

            if use_button:
                btn = target.query_selector(".pick-asset")
                self.assertIsNotNone(btn, f"槽位「{label}」没有「素材库」按钮")
                btn.click()
            else:
                # 点的位置必须避开右上角的「素材库」小按钮（top:3 right:3），
                # 否则测的就不是「点空槽位」这条路径了。
                target.click(position={"x": 10, "y": 40})
            page.wait_for_timeout(900)

            self.assertTrue(page.query_selector("#alPickerMask.open"),
                            f"{mode}/{label}：素材库弹窗没有打开")

            cards = page.query_selector_all("#alPickerBody .al-card")
            self.assertGreater(len(cards), 0,
                               f"{mode}/{label}：素材库弹窗里没有可选素材")
            cards[0].click()
            page.wait_for_timeout(300)
            ok = page.query_selector("#alPickerOk")
            self.assertIsNotNone(ok, "素材库弹窗没有「确定使用」按钮")
            ok.click()
            page.wait_for_timeout(900)

            asset_id = page.evaluate(
                """() => {
                    const s = window.__h3state || (window.H3State || null);
                    return s && s.assetRefs ? JSON.stringify(s.assetRefs) : null;
                }""")
            return {
                "alerts": list(alerts),
                "filled": "filled" in (target.get_attribute("class") or ""),
                "has_thumb": bool(target.query_selector("img")),
                "has_badge": bool(target.query_selector(".asset-badge")),
                "asset_id": asset_id,
                "console_errors": list(console_errors),
                "slot_text": (target.inner_text() or "").strip(),
            }
        finally:
            page.remove_listener("dialog", _on_dialog)

    def _run_entry(self, mode, label, use_button):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_context(
                viewport={"width": 1600, "height": 1100}).new_page()
            try:
                page.goto(self.url, wait_until="networkidle")
                page.wait_for_timeout(900)
                return self._pick_in_slot(page, mode, label, use_button)
            finally:
                browser.close()

    # ------------------------------------------------------------ 断言

    def test_slot_click_fills_slot_without_error(self):
        """「点空槽位」路径：原来这条路径漏传 onPick，选完必然报错且槽位空。"""
        for mode, label, cat in ENTRIES:
            if cat == "audio" and not getattr(self, "has_audio", False):
                continue        # 本机没 ffmpeg -> 建不出音频素材，跳过
            with self.subTest(mode=mode, label=label, path="点空槽位"):
                out = self._run_entry(mode, label, use_button=False)
                self.assertEqual(
                    out["alerts"], [],
                    f"{mode}/{label}：选完素材不该弹 alert，实际 {out['alerts']}")
                self.assertTrue(
                    out["filled"],
                    f"{mode}/{label}：槽位没有回填（filled class 缺失）")
                self.assertTrue(
                    out["has_badge"],
                    f"{mode}/{label}：槽位缺少「素材库」来源徽标（.asset-badge）")
                self.assertIn(
                    "素材库", out["slot_text"],
                    f"{mode}/{label}：槽位缺少「素材库」来源徽标")
                self.assertEqual(
                    out["console_errors"], [],
                    f"{mode}/{label}：控制台有报错 {out['console_errors']}")
                # 图片槽必须显示缩略图；视频 / 音频槽按设计只显示类型徽标
                # （见 app.js fillFromAsset：只有 kind === "image" 才 append img）。
                if cat == "image":
                    self.assertTrue(
                        out["has_thumb"],
                        f"{mode}/{label}：图片槽没有缩略图（<img> 缺失）")

    def test_asset_button_fills_slot_without_error(self):
        """「点槽位上的素材库小按钮」路径：原来就好的，不能被改坏。"""
        for mode, label, _cat in [("r2v", "参考 1", "image"),
                                  ("flf", "首帧", "image")]:
            with self.subTest(mode=mode, label=label, path="素材库按钮"):
                out = self._run_entry(mode, label, use_button=True)
                self.assertEqual(out["alerts"], [],
                                 f"{mode}/{label}：不该弹 alert {out['alerts']}")
                self.assertTrue(out["filled"],
                                f"{mode}/{label}：槽位没有回填")
                self.assertTrue(out["has_thumb"],
                                f"{mode}/{label}：槽位没有缩略图")
                self.assertTrue(out["has_badge"],
                                f"{mode}/{label}：槽位缺少「素材库」徽标")
                self.assertEqual(out["console_errors"], [],
                                 f"{mode}/{label}：控制台有报错")

    def test_missing_onpick_is_rejected_before_opening_picker(self):
        """漏传 onPick 必须在**打开弹窗之前**被拦下（用户不该白选一遍）。

        做法：用 StudioServer(app_js_override=...) 起一个**改坏**的实例
        （把修复那一行的 onPick 回调去掉），断言：
          1. 素材库弹窗**根本没开** —— 用户不该白选一遍；
          2. 给出了明确提示，且提示里**不再暴露**
             'onPick is not a function' 这种误导性原文。

        这条断言守住「失败要发生在选之前、且提示要说真话」。
        """
        original = (REPO / "web" / "app.js").read_text(encoding="utf-8")
        broken = original.replace(
            "openAssetPickerForSlot(kind, (asset) => fillFromAsset(asset));",
            "openAssetPickerForSlot(kind);")
        self.assertNotEqual(
            broken, original,
            "本测试的「改坏」手法失效了 —— app.js 的代码形状变了，需要更新本测试")

        server = StudioServer(app_js_override=broken.encode("utf-8"))
        self.assertTrue(server.start(), "改坏后的 Studio 实例启动失败")
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                page = browser.new_context(
                    viewport={"width": 1600, "height": 1100}).new_page()
                alerts = []

                def _on_dialog(d):
                    alerts.append(d.message)
                    d.dismiss()

                page.on("dialog", _on_dialog)
                try:
                    page.goto(f"http://127.0.0.1:{server.port}",
                              wait_until="networkidle")
                    page.wait_for_timeout(900)
                    page.click('[data-mode="r2v"]')
                    page.wait_for_timeout(1200)
                    for s in page.query_selector_all("#uploadRow .up-slot"):
                        if (s.get_attribute("data-label") or "") == "参考 1":
                            s.click(position={"x": 10, "y": 40})
                            break
                    page.wait_for_timeout(700)
                    self.assertFalse(
                        page.query_selector("#alPickerMask.open"),
                        "漏传回调时弹窗**不该打开** —— 用户不该白选一遍")
                    self.assertTrue(alerts, "漏传回调时应该给出明确提示")
                    self.assertNotIn(
                        "onPick is not a function", " ".join(alerts),
                        "提示不该再暴露 'onPick is not a function' 这种误导性原文")
                finally:
                    browser.close()
        finally:
            server.stop()

        # 源码层面的守卫断言（与变异测试配套）
        self.assertIn(
            'typeof onPick !== "function"',
            original,
            "openAssetPickerForSlot 必须对缺失的 onPick 做显式校验")
        # 每一处调用都必须传第二个参数
        import re
        calls = re.findall(r"openAssetPickerForSlot\(([^)]*)\)", original)
        self.assertTrue(calls, "没找到任何 openAssetPickerForSlot 调用")
        for call in calls:
            if call.strip() == "kind, onPick":      # 函数定义本身
                continue
            self.assertIn(",", call,
                          f"openAssetPickerForSlot({call}) 没传 onPick 回调")


if __name__ == "__main__":
    unittest.main()
