# -*- coding: utf-8 -*-
"""「增强必须带上已引用素材」—— 缺陷回归测试。

【缺陷原样（2026-11-18 用户报告）】
在「参考生图 / 首尾帧（flf）/ 参考生视频」这类带素材引用的生成模式里，
用户已从素材库引用了图片（首帧 / 尾帧 / 参考图，可多张），并输入提示词后
点「增强」—— 得到的增强提示词**完全没有基于素材图**。
根因：模型在增强阶段**根本没拿到**这些图片。

【技术根因】
前端调 `POST /api/enhance` 时**从不带**素材：
  * web/enhance-menu.js 的 runBasic / runRefine / runDirector
    构造的 body 里没有 asset_refs；
  * 服务端 `images` 参数虽然存在，但它是「浏览器本地路径列表」——
    服务端根本读不到浏览器本地路径，所以这条参数实际是**死的**。

【修法】
  * 前端：点「增强」时收集**当前面板已引用的素材**（asset_refs:
    [{asset_id, slot, ordinal}]）一并提交；
  * 服务端：把 asset_refs 解析成**服务端可读**的图片
    （图片直给 / 视频抽帧 / 音频降级为文字），连同图序语义喂给多模态模型。

【本文件把这条约定写死，防止回退】
A. 后端契约（不依赖浏览器）
   - enhance_assets 模块能解析素材引用 -> 服务端可读的图片
   - 图片 / 视频 / 音频三类素材各自的处理方式（含音频的**明确降级**）
   - 素材缺失 -> **如实报错**，不静默忽略
   - build_messages 带素材上下文 -> 提示词里出现素材清单
   - build_messages 不带素材 -> 提示词**逐字不变**（无素材面板不回归）
   - /api/enhance 接收 asset_refs 并把它接到模型调用上
B. 前端静态契约
   - 三条增强路径（普通 / 二次 / 导演稿）都带 asset_refs
   - 素材收集函数按当前模式过滤槽位（不提交界面看不见的幽灵引用）
C. 浏览器行为（playwright + chromium，缺失时整类 skip）
   - 引用素材 -> 点「增强」-> **实际请求体里含素材**
   - 无素材面板 -> 请求体里 asset_refs 为空数组

运行：python3 -m pytest tests/test_enhance_assets.py -v
需要：playwright（chromium）。缺失时整类 skip，不算失败。
"""
import json
import os
import re
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

from h3studio import enhancer as enh
from h3studio import enhance_assets as ea

WEB = REPO / "web"


# ------------------------------------------------------------------ 素材库夹具
class AssetLibraryFixture:
    """
    在临时目录里建一个**最小素材库**，供后端契约用例使用。

    【为什么不用真实素材库】真实库在 /mnt/usb2t（外接盘），
    内容随时间变化 —— 用例不能依赖「库里恰好有一张红图」。
    自己建一个，素材内容**确定**，断言才可复现。
    """

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def add_image(self, asset_id: str, name: str, rgb=(255, 0, 0)) -> str:
        """塞一张纯色 PNG 进素材库，返回 asset_id。"""
        from h3studio import assets as A
        original = self.root / A.DIR_ORIGINALS / asset_id[:2]
        original.mkdir(parents=True, exist_ok=True)
        path = original / f"{asset_id}.png"
        self._write_png(path, rgb)
        rel = f"{A.DIR_ORIGINALS}/{asset_id[:2]}/{path.name}"
        self._insert(asset_id, name, "image", "image/png", rel)
        return asset_id

    def add_audio(self, asset_id: str, name: str, analysis_text: str = "") -> str:
        from h3studio import assets as A
        original = self.root / A.DIR_ORIGINALS / asset_id[:2]
        original.mkdir(parents=True, exist_ok=True)
        path = original / f"{asset_id}.mp3"
        path.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00fake-audio")
        rel = f"{A.DIR_ORIGINALS}/{asset_id[:2]}/{path.name}"
        self._insert(asset_id, name, "audio", "audio/mpeg", rel)
        if analysis_text:
            self._insert_analysis(asset_id, analysis_text)
        return asset_id

    def _insert(self, asset_id, name, category, mime, rel):
        import time
        from h3studio import assets as A
        now = time.time()
        with A.connect() as conn:
            conn.execute(
                "INSERT INTO assets (id, name, category, mime, source, "
                "latest_version, version_count, total_bytes, created_at, updated_at) "
                "VALUES (?,?,?,?, 'upload', 1, 1, 1, ?, ?)",
                (asset_id, name, category, mime, now, now))
            conn.execute(
                "INSERT INTO asset_versions (asset_id, version, storage_path, "
                "sha256, bytes, mime, created_at) VALUES (?,?,?,?,?,?,?)",
                (asset_id, 1, rel, "0" * 64, 1, mime, now))

    def _insert_analysis(self, asset_id, text):
        import time
        from h3studio import assets as A
        now = time.time()
        with A.connect() as conn:
            conn.execute(
                "INSERT INTO asset_analyses (asset_id, version, revision, kind, "
                "text, created_at) VALUES (?,?,?,?,?,?)",
                (asset_id, 1, 1, "audio", text, now))

    @staticmethod
    def _write_png(path: Path, rgb):
        """写一张纯色 PNG（不依赖 Pillow —— 手写最小 PNG 编码）。"""
        import struct
        import zlib
        width = height = 8
        raw = b""
        for _ in range(height):
            raw += b"\x00" + bytes(rgb) * width

        def chunk(tag, data):
            body = tag + data
            return (struct.pack(">I", len(data)) + body
                    + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

        png = b"\x89PNG\r\n\x1a\n"
        png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        png += chunk(b"IDAT", zlib.compress(raw))
        png += chunk(b"IEND", b"")
        path.write_bytes(png)


# ------------------------------------------------------------------ 后端契约
class TestEnhanceAssetsBackend(unittest.TestCase):
    """后端：素材引用 -> 服务端可读图片 -> 提示词里的素材上下文。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="enh-assets-"))
        cls.root = cls.tmp / "library"
        cls._old_env = os.environ.get("ASSET_LIBRARY_ROOT")
        os.environ["ASSET_LIBRARY_ROOT"] = str(cls.root)
        from h3studio import assets as A
        A.init_db()
        cls.fixture = AssetLibraryFixture(cls.root)
        cls.img_id = cls.fixture.add_image("asset_testimg001", "测试参考图.png",
                                           rgb=(255, 0, 0))
        cls.img2_id = cls.fixture.add_image("asset_testimg002", "测试参考图2.png",
                                            rgb=(0, 255, 0))
        cls.aud_id = cls.fixture.add_audio("asset_testaud001", "测试音频.mp3",
                                           analysis_text="一段轻快的钢琴曲。")

    @classmethod
    def tearDownClass(cls):
        if cls._old_env is None:
            os.environ.pop("ASSET_LIBRARY_ROOT", None)
        else:
            os.environ["ASSET_LIBRARY_ROOT"] = cls._old_env
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- 归一 ----

    def test_normalize_refs_accepts_dicts_and_strings(self):
        """asset_refs 既接受 dict 列表，也接受纯字符串列表（容错）。"""
        out = ea.normalize_refs([
            {"asset_id": "a1", "slot": "ref_image", "ordinal": 3},
            "a2",
            {"asset_id": ""},              # 空 -> 剔除
            {"asset_id": "a1"},            # 重复 -> 剔除
            123,                           # 脏数据 -> 剔除
        ])
        self.assertEqual([r["asset_id"] for r in out], ["a1", "a2"])
        self.assertEqual(out[0]["slot"], "ref_image")
        self.assertEqual(out[0]["ordinal"], 3)

    def test_normalize_refs_rejects_non_list(self):
        """非列表 -> 空数组（不能抛异常）。"""
        self.assertEqual(ea.normalize_refs(None), [])
        self.assertEqual(ea.normalize_refs("asset_x"), [])
        self.assertEqual(ea.normalize_refs({"asset_id": "x"}), [])

    # ---- 解析 ----

    def test_image_asset_resolves_to_server_readable_path(self):
        """图片素材 -> 服务端可读的本机绝对路径。"""
        res = ea.resolve_assets(
            [{"asset_id": self.img_id, "slot": "ref_image", "ordinal": 0}])
        self.assertEqual(len(res["images"]), 1)
        path = Path(res["images"][0])
        self.assertTrue(path.is_file(), f"图片路径不存在：{path}")
        self.assertTrue(path.is_absolute(), "必须是服务端可读的绝对路径")

    def test_multiple_images_keep_order(self):
        """
        多张图 -> 按传入顺序编号。

        【2026-11-21 更新】标签写法从自创的 `<image1>` 改成 H3 **官方**
        `<Picture 1>` / `<Picture 2>`（见 h3studio/enhance_assets.py 顶部
        的官方规范出处）。详见 tests/test_enhance_ref_labels.py。
        """
        res = ea.resolve_assets([
            {"asset_id": self.img_id, "slot": "ref_image", "ordinal": 0},
            {"asset_id": self.img2_id, "slot": "ref_image", "ordinal": 1},
        ])
        self.assertEqual(len(res["images"]), 2)
        self.assertIn("<Picture 1>", res["notes"][0])
        self.assertIn("<Picture 2>", res["notes"][1])

    def test_first_frame_slot_is_labelled(self):
        """首帧槽位 -> 提示词里说清楚它是首帧。"""
        res = ea.resolve_assets(
            [{"asset_id": self.img_id, "slot": "first_frame", "ordinal": 0}])
        self.assertIn("首帧", res["notes"][0])

    def test_missing_asset_raises_instead_of_silently_skipping(self):
        """
        素材不存在 -> **如实报错**。

        【为什么这条是硬要求】静默跳过 = 用户以为素材参与了增强，
        其实没有 —— 这正是本次缺陷「素材被静默丢弃」的翻版。
        """
        with self.assertRaises(ea.EnhanceAssetError) as ctx:
            ea.resolve_assets([{"asset_id": "asset_notexist", "slot": "ref_image"}])
        self.assertIn("素材不存在", ctx.exception.message)

    def test_audio_degrades_to_text_context_not_silent(self):
        """
        音频 -> 明确降级为**文字上下文**，且在 skipped 里说明原因。

        【为什么】LLM 读不了音频。假装模型听到了音频是撒谎。
        """
        res = ea.resolve_assets(
            [{"asset_id": self.aud_id, "slot": "ref_audio", "ordinal": 0}])
        self.assertEqual(res["images"], [], "音频不该进图片列表")
        self.assertTrue(res["text_context"], "音频的分析文本应作为文字上下文")
        self.assertIn("钢琴曲", res["text_context"])
        self.assertEqual(len(res["skipped"]), 1)
        self.assertIn("模型无法直接听取音频", res["skipped"][0]["reason"])
        # 降级说明必须出现在给模型的上下文里（否则模型会臆测音频内容）
        described = ea.describe(res)
        self.assertIn("未", described)
        self.assertIn("臆测", described)

    def test_empty_refs_yields_empty_context(self):
        """没有素材 -> 空上下文（调用方据此不注入任何东西）。"""
        res = ea.resolve_assets([])
        self.assertEqual(res["images"], [])
        self.assertEqual(ea.describe(res), "")

    # ---- 提示词构造 ----

    def _spec(self):
        return {"system_prompt": "SYSTEM", "user_template": "用户输入 prompt: {prompt}"}

    def test_build_messages_includes_asset_context(self):
        """带素材上下文 -> 提示词里出现素材清单与原文分段。"""
        msgs = enh.build_messages(
            self._spec(), "一个女孩站在雪地里", "auto",
            asset_context="以下是本次任务所引用的素材：\n- <image1> = 参考图「x」")
        user = msgs[1]["content"]
        self.assertIn("素材", user)
        self.assertIn("参考图「x」", user)
        self.assertIn("一个女孩站在雪地里", user)

    def test_build_messages_without_asset_context_unchanged(self):
        """
        没有素材上下文 -> 提示词**逐字不变**。

        【为什么这条是硬要求】无素材面板（文生图 / 文生视频）的行为
        不能因为这个修复而变 —— 否则就是引入新回归。
        """
        base = enh.build_messages(self._spec(), "一个女孩站在雪地里", "auto")
        with_empty = enh.build_messages(
            self._spec(), "一个女孩站在雪地里", "auto", asset_context="")
        with_none = enh.build_messages(
            self._spec(), "一个女孩站在雪地里", "auto", asset_context=None)
        self.assertEqual(base[1]["content"], with_empty[1]["content"])
        self.assertEqual(base[1]["content"], with_none[1]["content"])
        self.assertNotIn("素材", base[1]["content"])

    def test_build_messages_with_history_and_assets(self):
        """历史 + 素材同时存在 -> 两段上下文都在，且都排在原文前面。"""
        msgs = enh.build_messages(
            self._spec(), "本段原文", "auto",
            history=[{"stage": 0, "prompt": "第1段内容"}],
            asset_context="素材清单内容")
        user = msgs[1]["content"]
        self.assertIn("第1段内容", user)
        self.assertIn("素材清单内容", user)
        self.assertIn("本段原文", user)
        self.assertLess(user.index("第1段内容"), user.index("本段原文"))
        self.assertLess(user.index("素材清单内容"), user.index("本段原文"))


# ------------------------------------------------------------------ 前端静态契约
class TestEnhanceAssetsFrontendSource(unittest.TestCase):
    """前端源码的静态约定（不依赖浏览器）。"""

    def read(self, name: str) -> str:
        return (WEB / name).read_text(encoding="utf-8")

    def test_all_enhance_paths_send_asset_refs(self):
        """
        三条走 /api/enhance 的增强路径都必须带 asset_refs。

        【为什么是三条都要】普通增强 / 二次增强 / 视频导演稿
        都是「读当前面板素材」的语义 —— 少一条就有一类面板
        仍然是「增强不带素材」。
        """
        src = self.read("enhance-menu.js")
        for fn in ("runBasic", "runRefine", "runDirector"):
            block = re.search(rf'async function {fn}\(.*?\n  \}}', src, re.S)
            self.assertIsNotNone(block, f"找不到 {fn}")
            body = block.group(0)
            self.assertIn("collectPanelAssetRefs()", body,
                          f"{fn} 没有收集当前面板的素材")
            self.assertIn("asset_refs", body,
                          f"{fn} 的请求体里没有 asset_refs")

    def test_collector_returns_empty_when_no_assets(self):
        """无素材 -> 空数组（请求体里 asset_refs 为空，服务端行为不变）。"""
        src = self.read("enhance-menu.js")
        block = re.search(r'function collectPanelAssetRefs\(\).*?\n  \}',
                          src, re.S)
        self.assertIsNotNone(block, "找不到 collectPanelAssetRefs")
        self.assertIn("return out;", block.group(0))

    def test_collector_filters_by_current_mode(self):
        """
        素材收集必须**按当前模式过滤**槽位。

        【为什么】state.assetRefs 是全局的；不按模式过滤就会出现
        「界面上看不见的槽位却带进增强」（幽灵输入）——
        与出图链路 app.js 的 MODE_ALLOWED_SLOT_KEYS 是同一纪律。
        """
        src = self.read("enhance-menu.js")
        block = re.search(r'function collectPanelAssetRefs\(\).*?\n  \}',
                          src, re.S)
        self.assertIsNotNone(block)
        self.assertIn("MODE_ALLOWED_SLOT_KEYS", block.group(0),
                      "素材收集没有按当前模式过滤槽位")

    def test_collector_handles_qwen_refs_module(self):
        """图片编辑模式的参考图（window.QwenRefs）也要被收集。"""
        src = self.read("enhance-menu.js")
        self.assertIn("collectQwenRefAssets", src)
        block = re.search(r'function collectQwenRefAssets\(.*?\n  \}', src, re.S)
        self.assertIsNotNone(block)
        self.assertIn("QwenRefs", block.group(0))
        self.assertIn("asset_id", block.group(0))

    def test_enhance_meta_shows_asset_context(self):
        """增强结果的 meta 行必须显示素材上下文（用户要能确认素材参与了）。"""
        src = self.read("providers.js")
        block = re.search(r'function buildEnhanceMeta\(.*?\n\}', src, re.S)
        self.assertIsNotNone(block)
        self.assertIn("assets", block.group(0))
        self.assertIn("素材上下文", block.group(0))

    def test_static_assets_version_bumped(self):
        """
        改过的静态资源必须升 ?v=（否则老用户拿不到修复）。

        【2026-11-21 更新】版本号随参考标签修复升到 reflabel1
        （app.js / providers.js / enhance-menu.js 三个都改了）。
        """
        html = self.read("index.html")
        for name in ("enhance-menu.js", "providers.js", "app.js"):
            self.assertIn(f"/static/{name}?v=reflabel1", html,
                          f"{name} 的 ?v= 没升到 reflabel1")


# ------------------------------------------------------------------ 浏览器验证
@unittest.skipUnless(HAS_PLAYWRIGHT, "需要 playwright（chromium）")
class TestEnhanceAssetsBrowser(unittest.TestCase):
    """真实浏览器：引用素材 -> 点「增强」-> 请求体里确实含素材。"""

    @classmethod
    def setUpClass(cls):
        from test_frontend_defaults import StudioServer
        cls.server = StudioServer(seed_jobs=0)
        assert cls.server.start(), "Studio 实例启动失败"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])
        cls.ctx = cls.browser.new_context(viewport={"width": 1280, "height": 900})
        cls.page = cls.ctx.new_page()
        cls.base = f"http://127.0.0.1:{cls.server.port}/"

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.stop()

    def setUp(self):
        self.calls = []
        self.page.on("request", self._on_request)
        self.page.goto(self.base, wait_until="domcontentloaded")
        self.page.wait_for_timeout(2200)

    def tearDown(self):
        try:
            self.page.remove_listener("request", self._on_request)
        except Exception:
            pass

    def _on_request(self, request):
        if request.method == "POST" and request.url.endswith("/api/enhance"):
            try:
                body = json.loads(request.post_data or "{}")
            except Exception:
                body = {"_raw": request.post_data}
            self.calls.append({"url": request.url, "body": body})

    def _stub_enhance(self):
        """拦掉真实的 /api/enhance（本地模型排队慢，且本用例只验请求体）。"""
        self.page.route(
            "**/api/enhance",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "original": "x", "enhanced": "增强后的提示词内容",
                    "merged": "增强后的提示词内容", "mode": "replace",
                    "provider": "测试 Provider", "provider_id": "p1",
                    "spec_id": "image", "used_multimodal": True,
                    "assets": {"image_count": 1, "asset_count": 1,
                               "notes": [], "skipped": [], "has_text_context": False},
                    "validation": {"ok": True, "errors": [], "warnings": [], "stats": {}},
                    "elements": None,
                    "params": {"temperature": 0.7, "top_p": 0.9, "max_tokens": 300,
                               "sampling": "sample", "length": "medium"},
                }, ensure_ascii=False)))

    def _switch_mode(self, mode):
        self.page.click(f'.nav-item[data-mode="{mode}"]')
        self.page.wait_for_timeout(900)

    def _set_prompt(self, text="一个女孩站在雪地里"):
        self.page.evaluate(
            """(t) => {
              const el = document.querySelector('#prompt');
              el.value = t;
              el.dispatchEvent(new Event('input', {bubbles: true}));
            }""", text)

    def _click_enhance(self):
        self.page.evaluate(
            """() => {
              const btn = document.querySelector('#enhanceBtn');
              if (!btn) throw new Error('找不到增强按钮');
              btn.click();
            }""")
        self.page.wait_for_timeout(1200)

    def test_r2v_panel_sends_referenced_assets(self):
        """
        参考生视频（r2v）面板：引用了素材 -> 请求体里必须含 asset_refs。

        这就是用户报告的那条链路。
        """
        self._stub_enhance()
        self._switch_mode("r2v")
        # 模拟用户在「参考 1」槽位引用了一个素材
        self.page.evaluate(
            """() => {
              state.assetRefs = {
                ref0: {asset_id: "asset_fake000001", name: "参考图.png", category: "image"},
              };
            }""")
        self._set_prompt()
        self._click_enhance()
        self.assertTrue(self.calls, "没有发出 /api/enhance 请求")
        body = self.calls[-1]["body"]
        self.assertIn("asset_refs", body, "请求体里没有 asset_refs")
        self.assertTrue(body["asset_refs"], "asset_refs 是空的 —— 素材没被带上")
        ids = [r["asset_id"] for r in body["asset_refs"]]
        self.assertIn("asset_fake000001", ids)
        self.assertEqual(body["asset_refs"][0]["slot"], "ref_image")

    def test_flf_panel_sends_first_and_last_frame(self):
        """首尾帧（flf）面板：首帧 / 尾帧都带上去，且槽位语义正确。"""
        self._stub_enhance()
        self._switch_mode("flf")
        self.page.evaluate(
            """() => {
              state.assetRefs = {
                first: {asset_id: "asset_first00001", name: "首帧.png", category: "image"},
                last: {asset_id: "asset_last000001", name: "尾帧.png", category: "image"},
              };
            }""")
        self._set_prompt()
        self._click_enhance()
        self.assertTrue(self.calls)
        body = self.calls[-1]["body"]
        slots = {r["asset_id"]: r["slot"] for r in body["asset_refs"]}
        self.assertEqual(slots.get("asset_first00001"), "first_frame")
        self.assertEqual(slots.get("asset_last000001"), "last_frame")

    def test_materials_of_other_modes_are_not_sent(self):
        """
        其它模式的槽位**不得**被带进来（幽灵输入）。

        【为什么】用户在 flf 选了首帧 -> 切到 r2v -> 界面上的槽位
        全变了，但 state 里可能还留着旧 key。带着走 = 模型看到
        「首帧」，而实际工作流里那是个参考图。
        """
        self._stub_enhance()
        self._switch_mode("r2v")
        self.page.evaluate(
            """() => {
              state.assetRefs = {
                first: {asset_id: "asset_ghostfirst1", name: "x.png", category: "image"},
                ref0: {asset_id: "asset_realref001", name: "y.png", category: "image"},
              };
            }""")
        self._set_prompt()
        self._click_enhance()
        self.assertTrue(self.calls)
        body = self.calls[-1]["body"]
        ids = [r["asset_id"] for r in body["asset_refs"]]
        self.assertIn("asset_realref001", ids)
        self.assertNotIn("asset_ghostfirst1", ids,
                         "界面上看不见的槽位被带进了增强请求")

    def test_text_to_image_panel_sends_no_assets(self):
        """
        文生图（t2i）面板：请求体里 asset_refs 为空数组。

        【为什么】无素材面板的行为不能变 —— 空数组让服务端
        走「无素材」的既有路径。
        """
        self._stub_enhance()
        self._switch_mode("t2i")
        self._set_prompt()
        self._click_enhance()
        self.assertTrue(self.calls)
        body = self.calls[-1]["body"]
        self.assertIn("asset_refs", body)
        self.assertEqual(body["asset_refs"], [])


if __name__ == "__main__":
    unittest.main()
