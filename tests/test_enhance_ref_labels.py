# -*- coding: utf-8 -*-
"""「增强必须带官方参考标签」—— 2026-11-21 现场复验的回归测试。

【上一轮修了什么】素材**传进**了增强请求（asset_refs -> 服务端可读图片）。

【这一轮修什么】用户复验发现：**增强后的提示词里仍然没有参考标签**。
实测（8190 线上，2026-11-21）：两张图都真传了
（`used_multimodal=true`、`image_count=2`），但模型输出里
`<Picture N>` / `<Subject N>` **一个都没有** —— 它只是「描述了图片」
却没「标注哪张图」。于是下游 H3 工作流无法把文字里的引用和实际送进去的
素材对上，素材等于白引用。

【官方依据】MiniMax H3 官方 skill `h3-prompt-writing/references/ref-en.txt`
（本机路径 `/home/ubuntu/comfyui/custom_nodes/comfyui-minimax-h3-prompt-enhancer-T8/
official_skills/h3-prompt-writing/references/ref-en.txt`）规定四类参考标签：
`<Subject N>` / `<Picture N>` / `<Video N>` / `<Audio N>`，
且**每个类别各自独立编号**。

【本文件写死的约定】
A. 官方标签规则（不依赖浏览器）
   - 标签名与官方一致；每个类别独立编号（1 图 + 1 视频 + 1 音频
     -> `<Picture 1>` / `<Video 1>` / `<Audio 1>`，**不是** 1/2/3）
   - 编号与**面板顺序**一一对应
   - 图片优先占用视觉额度（视频抽帧不能挤掉图片）
   - 图片上限对齐 H3 节点真实上限 9（面板也是 9 个槽位）
   - 输出缺标签 -> 服务端**拒绝**（502），不算成功
B. 前端静态契约
   - 素材按面板顺序收集（不是对象插入顺序）
   - meta 回显参考标签
C. 真实浏览器（playwright + chromium，缺失时整类 skip）
   - 引用多张素材 -> 请求体含**全部**素材
   - 响应含参考标签回显

运行：python3 -m pytest tests/test_enhance_ref_labels.py -v
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

#: 本机官方 skill 规范文件（官方依据）。
OFFICIAL_REF_SPEC = Path(
    "/home/ubuntu/comfyui/custom_nodes/comfyui-minimax-h3-prompt-enhancer-T8/"
    "official_skills/h3-prompt-writing/references/ref-en.txt")
OFFICIAL_BASE_SPEC = Path(
    "/home/ubuntu/comfyui/custom_nodes/comfyui-minimax-h3-prompt-enhancer-T8/"
    "official_skills/h3-prompt-writing/references/base-en.txt")


# ------------------------------------------------------------------ 素材库夹具
class AssetLibraryFixture:
    """在临时目录里建一个最小素材库（内容确定，断言可复现）。"""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def add_image(self, asset_id: str, name: str, rgb=(255, 0, 0)) -> str:
        from h3studio import assets as A
        original = self.root / A.DIR_ORIGINALS / asset_id[:2]
        original.mkdir(parents=True, exist_ok=True)
        path = original / f"{asset_id}.png"
        _write_png(path, rgb)
        rel = f"{A.DIR_ORIGINALS}/{asset_id[:2]}/{path.name}"
        _insert(self.root, asset_id, name, "image", "image/png", rel)
        return asset_id

    def add_audio(self, asset_id: str, name: str, analysis_text: str = "") -> str:
        from h3studio import assets as A
        original = self.root / A.DIR_ORIGINALS / asset_id[:2]
        original.mkdir(parents=True, exist_ok=True)
        path = original / f"{asset_id}.mp3"
        path.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00fake")
        rel = f"{A.DIR_ORIGINALS}/{asset_id[:2]}/{path.name}"
        _insert(self.root, asset_id, name, "audio", "audio/mpeg", rel)
        if analysis_text:
            _insert_analysis(self.root, asset_id, analysis_text)
        return asset_id


def _insert(root: Path, asset_id, name, category, mime, rel):
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


def _insert_analysis(root: Path, asset_id, text):
    import time
    from h3studio import assets as A
    now = time.time()
    with A.connect() as conn:
        conn.execute(
            "INSERT INTO asset_analyses (asset_id, version, revision, kind, "
            "text, created_at) VALUES (?,?,?,?,?,?)",
            (asset_id, 1, 1, "audio", text, now))


def _write_png(path: Path, rgb):
    """手写最小 PNG（不依赖 Pillow）。"""
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


# ------------------------------------------------------------------ 官方规范核对
class TestOfficialSpecAlignment(unittest.TestCase):
    """
    核对本项目的标签写法**与官方规范一致**（不自创语法）。

    【为什么要有这一组】上一轮最大的问题是「自己编了一套 <image1>」。
    这一组用例把官方规范文件当**事实源**：如果官方规范变了、
    或者本项目的实现与官方不一致，用例必须挂。
    """

    @unittest.skipUnless(OFFICIAL_REF_SPEC.is_file(),
                         f"本机没有官方规范文件 {OFFICIAL_REF_SPEC}")
    def test_official_spec_defines_the_four_label_kinds(self):
        """官方规范里必须真的定义了这四类标签。"""
        spec = OFFICIAL_REF_SPEC.read_text(encoding="utf-8")
        for label in ("<Subject N>", "<Picture N>", "<Video N>", "<Audio N>"):
            self.assertIn(label, spec,
                          f"官方规范里没有 {label} —— 本项目的标签体系可能对不上")

    @unittest.skipUnless(OFFICIAL_REF_SPEC.is_file(),
                         f"本机没有官方规范文件 {OFFICIAL_REF_SPEC}")
    def test_official_spec_says_labels_are_numbered_independently(self):
        """
        官方规范明确说各类别**独立编号** —— 这是本项目编号规则的依据。

        【为什么这条是硬要求】如果按「全局连续编号」实现，
        1 图 + 1 视频 + 1 音频会变成 <Picture 1> / <Video 2> / <Audio 3>，
        与官方规范冲突 -> 模型看到的编号和官方约定对不上。
        """
        spec = OFFICIAL_REF_SPEC.read_text(encoding="utf-8")
        self.assertIn("numbered independently", spec,
                      "官方规范不再声明独立编号 —— 需要复核编号规则")

    @unittest.skipUnless(OFFICIAL_BASE_SPEC.is_file(),
                         f"本机没有官方规范文件 {OFFICIAL_BASE_SPEC}")
    def test_official_base_spec_has_flf_alignment_instruction(self):
        """官方 base 规范里有 FL2VA 的首尾帧对齐句式（我们据此写模板）。"""
        spec = OFFICIAL_BASE_SPEC.read_text(encoding="utf-8")
        self.assertIn("FL2VA", spec)
        self.assertIn("aligns with the", spec)

    def test_project_labels_match_official_names(self):
        """本项目实现的标签名必须与官方一致。"""
        self.assertEqual(ea.label_for("image", 1), "<Picture 1>")
        self.assertEqual(ea.label_for("video", 1), "<Video 1>")
        self.assertEqual(ea.label_for("audio", 1), "<Audio 1>")

    def test_reference_template_exists_and_teaches_the_labels(self):
        """参考模式模板必须存在，且真的教了模型用这四类标签。"""
        spec = enh.get_spec("h3-ref-director")
        self.assertGreater(len(spec.get("system_prompt") or ""), 500)
        prompt = spec["system_prompt"]
        for label in ("<Subject N>", "<Picture N>", "<Video N>", "<Audio N>"):
            self.assertIn(label, prompt, f"参考模板没教 {label}")
        # 六段式结构
        for section in ("subject_definitions:", "summary:", "retention_analysis:",
                        "detailed_description:", "overall_soundscape:",
                        "non_diegetic_music:"):
            self.assertIn(section, prompt, f"参考模板缺字段 {section}")


# ------------------------------------------------------------------ 后端契约
class TestReferenceLabelBackend(unittest.TestCase):
    """后端：官方标签分配 + 输出校验。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="enh-ref-"))
        cls.root = cls.tmp / "library"
        cls._old_env = os.environ.get("ASSET_LIBRARY_ROOT")
        os.environ["ASSET_LIBRARY_ROOT"] = str(cls.root)
        from h3studio import assets as A
        A.init_db()
        fx = AssetLibraryFixture(cls.root)
        cls.img1 = fx.add_image("asset_refimg0001", "参考图1.png", rgb=(255, 0, 0))
        cls.img2 = fx.add_image("asset_refimg0002", "参考图2.png", rgb=(0, 255, 0))
        cls.img3 = fx.add_image("asset_refimg0003", "参考图3.png", rgb=(0, 0, 255))
        cls.aud = fx.add_audio("asset_refaud0001", "参考音乐.mp3",
                               analysis_text="一段轻快的钢琴曲。")

    @classmethod
    def tearDownClass(cls):
        if cls._old_env is None:
            os.environ.pop("ASSET_LIBRARY_ROOT", None)
        else:
            os.environ["ASSET_LIBRARY_ROOT"] = cls._old_env
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- 标签分配 ----

    def test_categories_are_numbered_independently(self):
        """
        图片 / 视频 / 音频**各自独立编号**（官方规则）。

        1 图 + 1 音频 -> <Picture 1> / <Audio 1>（**不是** <Audio 2>）。
        """
        res = ea.resolve_assets([
            {"asset_id": self.img1, "slot": "ref_image", "ordinal": 0},
            {"asset_id": self.aud, "slot": "ref_audio", "ordinal": 0},
        ])
        self.assertEqual(res["labels"], ["<Picture 1>", "<Audio 1>"])

    def test_labels_follow_input_order(self):
        """多张图 -> <Picture 1> / <Picture 2> / <Picture 3>，与传入顺序一致。"""
        res = ea.resolve_assets([
            {"asset_id": self.img1, "slot": "ref_image", "ordinal": 0},
            {"asset_id": self.img2, "slot": "ref_image", "ordinal": 1},
            {"asset_id": self.img3, "slot": "ref_image", "ordinal": 2},
        ])
        self.assertEqual(res["labels"], ["<Picture 1>", "<Picture 2>", "<Picture 3>"])
        self.assertEqual(len(res["images"]), 3)

    def test_first_frame_and_last_frame_get_picture_1_and_2(self):
        """首帧 -> <Picture 1>，尾帧 -> <Picture 2>。"""
        res = ea.resolve_assets([
            {"asset_id": self.img1, "slot": "first_frame", "ordinal": 0},
            {"asset_id": self.img2, "slot": "last_frame", "ordinal": 0},
        ])
        self.assertEqual(res["labels"], ["<Picture 1>", "<Picture 2>"])
        self.assertIn("首帧", res["notes"][0])
        self.assertIn("尾帧", res["notes"][1])

    def test_notes_use_official_labels(self):
        """素材清单里必须用官方标签，不是自创的 <image1>。"""
        res = ea.resolve_assets([
            {"asset_id": self.img1, "slot": "ref_image", "ordinal": 0},
        ])
        self.assertIn("<Picture 1>", res["notes"][0])
        self.assertNotIn("<image1>", res["notes"][0])

    def test_describe_tells_model_to_use_the_labels(self):
        """给模型的上下文必须明确要求用这些标签。"""
        res = ea.resolve_assets([
            {"asset_id": self.img1, "slot": "first_frame", "ordinal": 0},
        ])
        text = ea.describe(res)
        self.assertIn("<Picture 1>", text)
        self.assertIn("必须用", text)

    def test_image_cap_matches_h3_node_limit(self):
        """
        图片上限必须对齐 H3 节点真实上限（9），否则第 9 张被静默丢弃。

        【为什么是硬要求】面板有 9 个参考图槽位，节点 ref_images max=9。
        上限取更小值 -> 用户填满 9 张时第 9 张根本没进模型。
        """
        from h3studio import h3_video_modes as vm
        self.assertEqual(ea.MAX_IMAGES, vm.MAX_REF_IMAGES)
        self.assertEqual(ea.MAX_IMAGES, 9)

    def test_audio_label_still_allocated_even_when_degraded(self):
        """
        音频即使降级成文字，也**要占一个 <Audio N> 标签**。

        【为什么】官方编号是「素材在各自类别里的次序」——
        音频素材参与了（以文字形式），就该有它的标签；
        模型才能在正文里写「<Audio 1> 是背景音乐风格参考」。
        """
        res = ea.resolve_assets([
            {"asset_id": self.img1, "slot": "ref_image", "ordinal": 0},
            {"asset_id": self.aud, "slot": "ref_audio", "ordinal": 0},
        ])
        self.assertIn("<Audio 1>", res["labels"])
        self.assertIn("<Audio 1>", ea.describe(res))

    # ---- 输出校验 ----

    def test_extract_reference_labels_finds_all_four_kinds(self):
        text = "xx <Picture 1> yy <Subject 2> zz <Video 1> ww <Audio 1>"
        found = ea.extract_reference_labels(text)
        for label in ("<Picture 1>", "<Subject 2>", "<Video 1>", "<Audio 1>"):
            self.assertIn(label, found)

    def test_extract_reference_labels_ignores_non_official_syntax(self):
        """自创写法（<image1>）不算官方标签。"""
        self.assertFalse(ea.has_reference_label("look at <image1> please"))
        self.assertTrue(ea.has_reference_label("look at <Picture 1>"))

    # ---- 服务端路由 ----

    def test_server_routes_to_reference_template_when_assets_present(self):
        """带素材 -> 路由到官方参考模式模板。"""
        sys.path.insert(0, str(REPO))
        import importlib
        server = importlib.import_module("server")
        self.assertEqual(
            server._reference_spec_for_mode("video-director", "flf", "fl2va_pruned_int8"),
            "h3-ref-director")
        self.assertEqual(
            server._reference_spec_for_mode("video-director", "r2v", "fl2va_pruned_int8"),
            "h3-ref-director")

    def test_server_keeps_template_for_modes_without_reference_spec(self):
        """没有参考模式模板的模式 -> 保持原模板（不乱改）。"""
        sys.path.insert(0, str(REPO))
        import importlib
        server = importlib.import_module("server")
        # 图像模式：各模型族有自己的官方参考图体系，不动模板
        self.assertEqual(
            server._reference_spec_for_mode("image", "t2i", "z_image_turbo_bf16"),
            "image")
        # 无 mode_ctx：不动模板
        self.assertEqual(server._reference_spec_for_mode("video-director", "", ""),
                         "video-director")


# ------------------------------------------------------------------ 前端静态契约
class TestReferenceLabelFrontendSource(unittest.TestCase):
    """前端源码的静态约定（不依赖浏览器）。"""

    def read(self, name: str) -> str:
        return (WEB / name).read_text(encoding="utf-8")

    def test_collector_sorts_by_panel_slot_order(self):
        """
        素材收集必须**按面板槽位顺序**排序。

        【为什么】官方 `<Picture N>` 的编号要跟面板顺序一一对应。
        用对象插入顺序的话，用户「先填参考 3 再填首帧」就会编号错位。
        """
        src = self.read("enhance-menu.js")
        block = re.search(r'function collectPanelAssetRefs\(\).*?\n  \}',
                          src, re.S)
        self.assertIsNotNone(block)
        body = block.group(0)
        self.assertIn("slotOrder", body)
        self.assertIn("sort", body)

    def test_slot_order_matches_panel_render_order(self):
        """
        槽位序号表必须与 app.js 里 renderMode 真正 mk() 的顺序一致。

        口径：flf -> first(10) / last(20) / refvid*(30+) / refaud*(40+)；
              r2v -> ref*(10+) / refvid*(30+) / refaud*(40+)。
        """
        src = self.read("enhance-menu.js")
        block = re.search(r'function slotOrder\(key\).*?\n  \}', src, re.S)
        self.assertIsNotNone(block, "找不到 slotOrder")
        body = block.group(0)
        # 增强侧复用 app.js 的唯一真源（见下一条用例），这里只锁住兜底实现
        self.assertIn("panelSlotOrder", body)
        self.assertIn("10 + parseInt", body)   # ref0..8
        self.assertIn("30 + parseInt", body)   # refvid0..2
        self.assertIn("40 + parseInt", body)   # refaud0..2

    def test_app_slot_order_source_has_the_same_bases(self):
        """
        app.js 的唯一真源 `panelSlotOrder()` 也要有同样的基数口径。

        10 = 首帧 / 参考图区，20 = 尾帧，30 = 参考视频区，40 = 参考音乐区。
        """
        src = self.read("app.js")
        block = re.search(r'function panelSlotOrder\(key\).*?\n\}', src, re.S)
        self.assertIsNotNone(block, "app.js 里找不到 panelSlotOrder")
        body = block.group(0)
        self.assertIn('key === "first") return 10', body)
        self.assertIn('key === "last") return 20', body)
        self.assertIn("10 + parseInt", body)
        self.assertIn("30 + parseInt", body)
        self.assertIn("40 + parseInt", body)

    def test_enhance_and_submit_share_the_same_slot_order_source(self):
        """
        增强侧与提交侧必须用**同一份**槽位顺序实现。

        【为什么这是硬要求】增强时算出来的 `<Picture 1>` 必须对应
        真正进工作流的第 1 张图。两边顺序口径不一致的话，
        标签会指向错的图 —— 那比没有标签更糟（模型按标签取错图）。

        实现：web/app.js 里的 `panelSlotOrder()` 是唯一真源，
        增强侧（enhance-menu.js 的 slotOrder）优先调它。
        """
        app_src = self.read("app.js")
        self.assertIn("function panelSlotOrder(key)", app_src,
                      "app.js 里没有 panelSlotOrder —— 缺唯一真源")
        enh_src = self.read("enhance-menu.js")
        block = re.search(r'function slotOrder\(key\).*?\n  \}', enh_src, re.S)
        self.assertIsNotNone(block)
        self.assertIn("panelSlotOrder", block.group(0),
                      "增强侧没有复用 app.js 的 panelSlotOrder —— 两边口径会漂移")

    def test_submit_path_sorts_by_panel_order(self):
        """
        提交侧（app.js 的 asset_refs 构建）也必须按面板顺序排序。

        【为什么】落库的 params 必须与面板一致 ——
        提交的素材顺序决定 ComfyUI 工作流里图片的槽位顺序，
        也就决定 `<Picture N>` 到底指哪张图。
        """
        src = self.read("app.js")
        marker = "fd.append(\"asset_refs\""
        idx = src.find(marker)
        self.assertNotEqual(idx, -1, "找不到提交侧的 asset_refs")
        # 取它前面的一段（构建逻辑在 append 之前）
        region = src[max(0, idx - 2500):idx]
        self.assertIn("panelSlotOrder", region,
                      "提交侧没有按面板顺序排序素材")
        self.assertIn("sort", region, "提交侧没有排序素材")

    def test_ordinal_is_per_category(self):
        """ordinal 必须按类别各自计数（与服务端标签规则一致）。"""
        src = self.read("enhance-menu.js")
        block = re.search(r'function collectPanelAssetRefs\(\).*?\n  \}',
                          src, re.S)
        self.assertIsNotNone(block)
        self.assertIn("groupCounter[entry.slot]", block.group(0))

    def test_meta_shows_reference_labels(self):
        """meta 行必须显示参考标签（用户要能核对哪张图被引用了）。"""
        src = self.read("providers.js")
        block = re.search(r'function buildEnhanceMeta\(.*?\n\}', src, re.S)
        self.assertIsNotNone(block)
        self.assertIn("reference_labels", block.group(0))
        self.assertIn("参考标签", block.group(0))


# ------------------------------------------------------------------ 浏览器验证
@unittest.skipUnless(HAS_PLAYWRIGHT, "需要 playwright（chromium）")
class TestReferenceLabelBrowser(unittest.TestCase):
    """真实浏览器：引用多张素材 -> 请求体含全部素材 -> 响应含参考标签。"""

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
            self.calls.append(body)

    def _stub_enhance(self, response_extra=None):
        payload = {
            "original": "x", "enhanced": "integrated_multimodal_description: [Shot 1] "
                                         "<Picture 1> <Picture 2>",
            "merged": "x", "mode": "replace",
            "provider": "测试 Provider", "provider_id": "p1",
            "spec_id": "h3-ref-director", "used_multimodal": True,
            "assets": {"image_count": 2, "asset_count": 2, "notes": [], "skipped": [],
                       "has_text_context": False,
                       "labels": ["<Picture 1>", "<Picture 2>"]},
            "reference_labels": {"expected": ["<Picture 1>", "<Picture 2>"],
                                 "found": ["<Picture 1>", "<Picture 2>"]},
            "validation": {"ok": True, "errors": [], "warnings": [], "stats": {}},
            "elements": None,
            "params": {"temperature": 0.7, "top_p": 0.9, "max_tokens": 300,
                       "sampling": "sample", "length": "medium"},
        }
        if response_extra:
            payload.update(response_extra)
        self.page.route(
            "**/api/enhance",
            lambda route: route.fulfill(status=200,
                                         content_type="application/json",
                                         body=json.dumps(payload, ensure_ascii=False)))

    def _switch_mode(self, mode):
        """
        切到某个生成模式。

        【为什么要等这么久】2026-11-21 起视频模式的导航项不再写死在
        index.html 里 —— 它们由 web/video-nav.js 从 `GET /api/video-categories`
        **异步渲染**到 `#navVideoCategories`（index.html 里那几条只是
        <noscript> 兜底）。

        而 app.js 的 boot() 是 `fn()` **不 await** 的（既有实现，见
        commit d46dd9f —— 不是本次改动引入的），所以视频导航要等
        `/api/video-categories` 返回后才出现。测试实例里这个接口要走
        本机 ComfyUI 的模型探测，实测 **15~20 秒**才渲染完。
        所以这里必须显式等到导航项出现，而不是固定 sleep。
        """
        self.page.wait_for_function(
            """(m) => {
              const el = document.querySelector(`.nav-item[data-mode="${m}"]`);
              return !!(el && el.offsetParent !== null);
            }""",
            arg=mode, timeout=60000)
        self.page.evaluate(
            """(m) => {
              const el = document.querySelector(`.nav-item[data-mode="${m}"]`);
              if (el) el.click();
            }""", mode)
        self.page.wait_for_timeout(900)
        # 确认模式真的切过去了（动态导航点完不一定会切）
        self.page.wait_for_function(
            """(m) => {
              try { return typeof state !== 'undefined' && state.mode === m; }
              catch (e) { return false; }
            }""", arg=mode, timeout=8000)

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

    def test_all_referenced_images_are_sent(self):
        """
        r2v 面板引用 3 张图 -> 请求体必须含**全部 3 张**。

        【这是本轮的核心断言】不能只传第一张、不能静默丢弃。
        """
        self._stub_enhance()
        self._switch_mode("r2v")
        self.page.evaluate(
            """() => {
              state.assetRefs = {
                ref0: {asset_id: "asset_a000000001", name: "a.png", category: "image"},
                ref1: {asset_id: "asset_b000000002", name: "b.png", category: "image"},
                ref2: {asset_id: "asset_c000000003", name: "c.png", category: "image"},
              };
            }""")
        self._set_prompt()
        self._click_enhance()
        self.assertTrue(self.calls, "没有发出 /api/enhance 请求")
        body = self.calls[-1]
        refs = body.get("asset_refs") or []
        self.assertEqual(len(refs), 3, f"只传了 {len(refs)} 个素材：{refs}")
        ids = [r["asset_id"] for r in refs]
        for expected in ("asset_a000000001", "asset_b000000002", "asset_c000000003"):
            self.assertIn(expected, ids)

    def test_panel_order_is_preserved(self):
        """
        素材顺序必须与**面板槽位顺序**一致（不是对象插入顺序）。

        【怎么构造这个场景】先填 last（面板上排第二），再填 first
        （面板上排第一）—— 对象的枚举顺序就会是 last -> first；
        如果前端不排序，`<Picture 1>` 就会被分配给「尾帧」，标签与面板对不上。

        【为什么用 flf 面板】flf 的槽位是 first / last / refvid* / refaud*
        （**没有**参考图槽位），首帧与尾帧两个槽位就够验证顺序了。
        """
        self._stub_enhance()
        self._switch_mode("flf")
        self.page.evaluate(
            """() => {
              // 故意先填 last（面板上排在后面），再填 first（面板上排在前面）
              state.assetRefs = {
                last: {asset_id: "asset_zzz0000002", name: "z.png", category: "image"},
                first: {asset_id: "asset_aaa0000001", name: "a.png", category: "image"},
              };
            }""")
        self._set_prompt()
        self._click_enhance()
        self.assertTrue(self.calls)
        refs = self.calls[-1].get("asset_refs") or []
        self.assertEqual(len(refs), 2, f"只传了 {len(refs)} 个素材：{refs}")
        by_id = {r["asset_id"]: r for r in refs}
        self.assertIn("asset_aaa0000001", by_id)
        self.assertIn("asset_zzz0000002", by_id)
        # 面板顺序：first 在前 -> 它必须拿到第一个序号
        self.assertEqual(by_id["asset_aaa0000001"]["ordinal"], 0,
                         "首帧没有拿到第 1 个序号（面板顺序没生效）")
        self.assertEqual(by_id["asset_aaa0000001"]["slot"], "first_frame")
        self.assertEqual(by_id["asset_zzz0000002"]["slot"], "last_frame")
        # 请求体里的**先后顺序**也必须是面板顺序
        self.assertEqual(refs[0]["asset_id"], "asset_aaa0000001",
                         "请求体里首帧不在第一位（面板顺序没生效）")

    def test_video_and_audio_assets_are_sent_with_correct_slots(self):
        """参考视频 / 参考音乐也要带上去，槽位语义正确。"""
        self._stub_enhance()
        self._switch_mode("r2v")
        self.page.evaluate(
            """() => {
              state.assetRefs = {
                ref0: {asset_id: "asset_img00000001", name: "a.png", category: "image"},
                refvid0: {asset_id: "asset_vid00000001", name: "v.mp4", category: "video"},
                refaud0: {asset_id: "asset_aud00000001", name: "a.mp3", category: "audio"},
              };
            }""")
        self._set_prompt()
        self._click_enhance()
        self.assertTrue(self.calls)
        refs = self.calls[-1].get("asset_refs") or []
        slots = {r["asset_id"]: r["slot"] for r in refs}
        self.assertEqual(slots.get("asset_img00000001"), "ref_image")
        self.assertEqual(slots.get("asset_vid00000001"), "ref_video")
        self.assertEqual(slots.get("asset_aud00000001"), "ref_audio")

    def test_text_to_video_panel_sends_no_assets(self):
        """文生视频没有素材槽位 -> asset_refs 为空数组（不回归）。"""
        self._stub_enhance()
        self._switch_mode("t2v")
        self._set_prompt()
        self._click_enhance()
        self.assertTrue(self.calls)
        self.assertEqual(self.calls[-1].get("asset_refs"), [])

    def test_meta_displays_reference_labels(self):
        """
        增强结果弹窗的 meta 必须显示参考标签。

        【为什么要看 DOM 而不是接口返回】用户看到的是弹窗里的文字；
        meta 行里 `<Picture 1>` 会被 HTML 转义成 `&lt;Picture 1&gt;`
        （这是对的 —— 防止标签被当成 HTML 标签解析掉），
        所以断言要看 `innerText`（转义前的可见文本）而不是 innerHTML。
        """
        self._stub_enhance()
        self._switch_mode("r2v")
        self.page.evaluate(
            """() => {
              state.assetRefs = {
                ref0: {asset_id: "asset_a000000001", name: "a.png", category: "image"},
              };
            }""")
        self._set_prompt()
        self._click_enhance()
        self.page.wait_for_timeout(800)
        meta = self.page.evaluate(
            """() => {
              const el = document.querySelector('#enhanceMeta');
              return el ? el.innerText : "";
            }""")
        self.assertIn("参考标签", meta)
        self.assertIn("<Picture 1>", meta)
        self.assertIn("已全部标注", meta)


if __name__ == "__main__":
    unittest.main()
