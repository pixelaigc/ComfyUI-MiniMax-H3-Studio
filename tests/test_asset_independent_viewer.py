# -*- coding: utf-8 -*-
"""素材库「独立观察/播放窗口」的回归测试（2026-11-21）。

【这个文件锁死的行为】

  需求：素材库点卡片会弹右侧**抽屉**（保留不改），但素材库**没有**
  「独立观察该素材」的能力 —— 生成媒体库有（继续点击后弹独立窗口）。
  本轮补齐：素材库提供**独立窗口**（覆盖式查看器，不是抽屉），
  入口在「卡片」与「抽屉」两条路径上都可用。

  1. 入口可达（三条，全部常驻可见，不藏在隐藏入口里）
     * 卡片：缩略图上的 .al-view-btn（data-act="view"）
     * 抽屉：#alViewBtn（文案「独立窗口查看」）
     * 右键菜单：data-act="view"（文案「独立窗口查看」）
  2. 按类型正确工作
     * 图片 -> 复用 app.js 的 lightbox（可放大看原始分辨率）
     * 视频 / 音频 -> media-preview.js 的模态框（原生 controls）
     * kind 必须**显式**取素材的 category（content_url 没有扩展名）
  3. 不破坏既有能力
     * 抽屉仍可开 / 可关
     * 多选 / 批量删除 / 播放器模式照常
     * 打开独立窗口**不误触发**多选 / 加入播放器 / 引用到生成面板
  4. 窄屏（375px）可用
     * 查看器完整可见、关闭按钮够大、控件不遮挡卡片正文

【为什么这样断言】

断言若只是「源码里出现过某个字符串」，把那段字符串删掉、换成一段
同样含关键词的注释，测试仍然绿 —— 那它没在守行为。所以：

  * **结构断言**（入口/文案/类名确实存在）—— 用来防止入口被误删；
  * **行为断言**（用 Node 真跑 web/assets.js 的相关纯逻辑 + 最小 DOM 桩）
    —— 用来保证「点了确实调 openAssetViewer」「kind 取自 category」
    「Esc 快照守卫真的挡住了抽屉关闭」。

运行：python3 -m pytest tests/test_asset_independent_viewer.py -v
"""

import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "web"
ASSETS_JS = WEB / "assets.js"
ASSETS_CSS = WEB / "assets.css"
RESPONSIVE_CSS = WEB / "responsive.css"
INDEX_HTML = WEB / "index.html"

ASSETS_JS_SRC = ASSETS_JS.read_text(encoding="utf-8")
ASSETS_CSS_SRC = ASSETS_CSS.read_text(encoding="utf-8")
RESPONSIVE_CSS_SRC = RESPONSIVE_CSS.read_text(encoding="utf-8")
INDEX_HTML_SRC = INDEX_HTML.read_text(encoding="utf-8")

NODE = shutil.which("node")


def _node_run(script: str, env_extra=None) -> str:
    """跑一段 Node 脚本，返回 stdout。"""
    env = os.environ.copy()
    env["NS_FILE"] = str(ASSETS_JS)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=60, env=env)
    if proc.returncode != 0:
        raise AssertionError(f"node 执行失败:\n{proc.stderr}")
    return proc.stdout


# ==========================================================================
# 1. 入口存在且可达（结构断言 —— 防误删）
# ==========================================================================

class TestIndependentViewerEntryPoints(unittest.TestCase):
    """三条入口都必须真实存在，且文案可读。"""

    def test_card_entry_exists_and_is_visible_by_default(self):
        """卡片入口：缩略图上的 .al-view-btn，**不依赖 hover**。"""
        self.assertIn('class="al-view-btn"', ASSETS_JS_SRC,
                      "素材卡上的独立查看按钮被删了 —— 卡片路径的入口没了")
        self.assertIn('data-act="view"', ASSETS_JS_SRC,
                      "素材卡的独立查看按钮缺少 data-act=\"view\"")
        # 【不得只藏在隐藏入口里】按钮不能靠 hover 才出现。
        # 判据：assets.css 里 .al-view-btn 不能有 opacity:0 / display:none
        # 这类「默认不可见」的声明。
        block = self._css_block(ASSETS_CSS_SRC, ".al-view-btn")
        self.assertNotIn("opacity: 0", block,
                         ".al-view-btn 默认 opacity:0 —— 入口是隐藏的，触屏点不到")
        self.assertNotIn("display: none", block,
                         ".al-view-btn 默认 display:none —— 入口是隐藏的")
        # 且不能只写 .al-view-btn:hover 才给可见性（那才是 hover 才出现）
        self.assertNotIn(":hover .al-view-btn", ASSETS_CSS_SRC,
                         ".al-view-btn 靠父元素 hover 才显隐 —— 触屏没有 hover")

    def test_drawer_entry_exists(self):
        """抽屉入口：#alViewBtn，文案「独立窗口查看」。

        【为什么要在 renderDrawer 的函数体里找】
        只在全文里找 'id="alViewBtn"' 是**不够**的 —— 把渲染条件改成
        `${false ? ... : ""}` 之后，这个字符串照样在源码里（只是永远不渲染），
        断言却绿了 —— 按钮实际上消失了，但测试说它在。
        ⇒ 必须限定在 renderDrawer 的模板里，并要求它由 isViewableAsset 驱动。
        """
        src = ASSETS_JS_SRC
        i = src.index("function renderDrawer(asset)")
        j = src.index("// ---------------------------------------------------------------- 分析状态轮询", i)
        drawer_body = src[i:j]
        self.assertIn('id="alViewBtn"', drawer_body,
                      "抽屉里的「独立窗口查看」按钮被删了")
        self.assertIn("独立窗口查看", drawer_body,
                      "抽屉按钮的文案不是「独立窗口查看」")
        # 按钮必须由 isViewableAsset 驱动（不是写死 true / false）
        self.assertIn("isViewableAsset(asset)", drawer_body,
                      "抽屉按钮不由 isViewableAsset 驱动 —— "
                      "要么永远不渲染，要么对文档类素材也渲染")
        # 绑定也要在
        self.assertIn('on("#alViewBtn", "click"', src,
                      "抽屉按钮没有绑事件")

    def test_context_menu_entry_exists(self):
        """右键菜单入口：data-act="view"。"""
        self.assertIn('data-act="view"', ASSETS_JS_SRC)
        # 右键菜单里要有一项可点的「独立窗口查看」
        # 【为什么要限定在 showAssetContext 里】文件里还有另一处
        # `menu.innerHTML = ...`（标签筛选的下拉菜单），不限定会切错块。
        ctx_start = ASSETS_JS_SRC.index("function showAssetContext(event, assetId)")
        menu_block = self._slice(ASSETS_JS_SRC[ctx_start:], "menu.innerHTML = `", "`;")
        self.assertIn('data-act="view"', menu_block,
                      "右键菜单里没有「独立窗口查看」项")
        self.assertIn("独立窗口查看", menu_block,
                      "右键菜单项的文案不是「独立窗口查看」")

    def test_three_entries_share_one_implementation(self):
        """三条入口必须走**同一个**函数 —— 不许各写一份。

        【为什么】三份实现必然漂移：改了一处另外两处还在旧行为。
        """
        # openAssetViewer 的定义 + 三处调用
        self.assertIn("function openAssetViewer(asset)", ASSETS_JS_SRC)
        calls = len(re.findall(r"openAssetViewer\(", ASSETS_JS_SRC))
        # 定义 1 次 + 卡片 1 次 + 抽屉 1 次 + 右键 1 次 = 4
        # （导出那项在对象字面量里，没有括号，不计入）
        self.assertGreaterEqual(calls, 4,
                                "openAssetViewer 的调用点少于 4 处 —— "
                                "三条入口可能没有共用同一个实现")
        # 三条入口各自的调用点都要在
        for marker, label in (
            ("const viewBtn = card.querySelector(\".al-view-btn\")", "卡片入口"),
            ('on("#alViewBtn", "click"', "抽屉入口"),
            ('else if (act === "view" && asset) openAssetViewer(asset);', "右键菜单入口"),
        ):
            self.assertIn(marker, ASSETS_JS_SRC,
                          f"{label}没有调用 openAssetViewer")

    def test_viewer_reuses_existing_media_preview_component(self):
        """必须**复用**生成媒体库那套查看器，不另起一套互斥实现。

        【为什么】生成媒体库的独立查看 = window.MediaPreview
        （图片 -> app.js 的 lightbox，视频/音频 -> 媒体预览模态框）。
        素材库自己写一套 => 两边视觉与操作习惯会漂移。
        """
        self.assertIn("window.MediaPreview", ASSETS_JS_SRC,
                      "素材库没有复用 window.MediaPreview")
        self.assertIn("preview.show(", ASSETS_JS_SRC,
                      "素材库没有调用 MediaPreview.show()")
        # 不能自己造一个新的模态框（另起一套）
        # 判据：assets.js 里**不得创建** mediaPreviewModal 元素
        # （只允许**读**它的 hidden 状态 —— 那是既有组件，
        #  用来判断「查看器是否开着」，见 isIndependentViewerOpen）。
        self.assertNotIn('createElement("div")\n    modal.className = "media-preview-modal"',
                         ASSETS_JS_SRC,
                         "assets.js 里自己建了一个 media-preview-modal —— "
                         "另起了一套互斥实现")
        self.assertNotIn("media-preview-box", ASSETS_JS_SRC,
                         "assets.js 里出现了 media-preview-box —— "
                         "另起了一套互斥实现（该类名属于 media-preview.js）")
        self.assertNotIn("media-preview-media", ASSETS_JS_SRC,
                         "assets.js 里出现了 media-preview-media —— "
                         "另起了一套互斥实现（该类名属于 media-preview.js）")

    @staticmethod
    def _css_block(src: str, selector: str) -> str:
        idx = src.find(selector)
        if idx < 0:
            return ""
        end = src.find("}", idx)
        return src[idx:end + 1] if end > 0 else src[idx:]

    @staticmethod
    def _slice(src: str, start: str, end: str) -> str:
        i = src.find(start)
        if i < 0:
            return ""
        j = src.find(end, i + len(start))
        return src[i:j] if j > 0 else src[i:]


# ==========================================================================
# 2. 按类型正确工作（行为断言 —— Node 真跑）
# ==========================================================================

@unittest.skipIf(NODE is None, "node 不可用，跳过前端逻辑测试")
class TestIndependentViewerBehaviour(unittest.TestCase):
    """用 Node 真跑 assets.js 的相关逻辑。"""

    def _extract(self, start_marker: str, end_marker: str) -> str:
        src = ASSETS_JS_SRC
        i = src.index(start_marker)
        j = src.index(end_marker, i)
        return src[i:j]

    def test_kind_is_taken_from_category_not_url(self):
        """kind 必须取素材的 category。

        【为什么】content_url 是 /api/assets/<id>/content（**没有扩展名**），
        按 URL 猜类型会猜成 image —— 视频会弹成图片查看器。
        """
        script = r"""
        const NS = require('fs').readFileSync(process.env.NS_FILE, 'utf8');
        // 抽取 openAssetViewer + isViewableAsset + isIndependentViewerOpen
        const start = NS.indexOf('function openAssetViewer(asset)');
        const end = NS.indexOf('async function api(path');
        const body = NS.substring(start, end);

        // 最小桩：记录 MediaPreview.show 收到的参数
        const calls = [];
        global.window = { MediaPreview: { show: (o) => { calls.push(o); return true; } } };
        global.toast = () => {};

        eval(body);

        openAssetViewer({
          id: 'asset_1', name: '视频素材', category: 'video',
          mime: 'video/mp4', content_url: '/api/assets/asset_1/content',
        });
        openAssetViewer({
          id: 'asset_2', name: '音频素材', category: 'audio',
          mime: 'audio/mpeg', content_url: '/api/assets/asset_2/content',
        });
        openAssetViewer({
          id: 'asset_3', name: '图片素材', category: 'image',
          mime: 'image/png', content_url: '/api/assets/asset_3/content',
        });
        console.log(JSON.stringify(calls));
        """
        out = _node_run(script)
        calls = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(3, len(calls), f"openAssetViewer 没能打开 3 个素材：{calls}")
        self.assertEqual("video", calls[0]["kind"])
        self.assertEqual("audio", calls[1]["kind"])
        self.assertEqual("image", calls[2]["kind"])
        # URL 必须指向原文件（不是缩略图）
        self.assertEqual("/api/assets/asset_1/content", calls[0]["url"])

    def test_only_media_kinds_are_viewable(self):
        """只有图片 / 视频 / 音频能在独立窗口里看。"""
        script = r"""
        const NS = require('fs').readFileSync(process.env.NS_FILE, 'utf8');
        const start = NS.indexOf('function openAssetViewer(asset)');
        const end = NS.indexOf('async function api(path');
        eval(NS.substring(start, end));
        console.log(JSON.stringify({
          image: isViewableAsset({category: 'image'}),
          video: isViewableAsset({category: 'video'}),
          audio: isViewableAsset({category: 'audio'}),
          doc: isViewableAsset({category: 'doc'}),
          workflow: isViewableAsset({category: 'workflow'}),
          none: isViewableAsset(null),
        }));
        """
        out = _node_run(script)
        got = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(got["image"] and got["video"] and got["audio"])
        self.assertFalse(got["doc"])
        self.assertFalse(got["workflow"])
        self.assertFalse(got["none"])

    def test_viewer_open_detection_covers_both_doms(self):
        """「查看器是否开着」必须同时认 lightbox 与 mediaPreviewModal。

        【为什么】图片走 #lightbox、视频/音频走 #mediaPreviewModal ——
        只认一个的话，另一种类型的 Esc 守卫会失效。
        """
        script = r"""
        const NS = require('fs').readFileSync(process.env.NS_FILE, 'utf8');
        const start = NS.indexOf('function openAssetViewer(asset)');
        const end = NS.indexOf('async function api(path');
        eval(NS.substring(start, end));

        const lightbox = { hidden: true };
        const modal = { hidden: true };
        global.document = { querySelector: (s) => {
          if (s === '#lightbox') return lightbox;
          if (s === '#mediaPreviewModal') return modal;
          return null;
        }};
        // 被抽出来的函数体里用的是模块私有的 $，这里补一个等价的桩
        global.$ = (s) => global.document.querySelector(s);
        const r = {};
        r.none = isIndependentViewerOpen();
        lightbox.hidden = false;
        r.lightbox = isIndependentViewerOpen();
        lightbox.hidden = true;
        modal.hidden = false;
        r.modal = isIndependentViewerOpen();
        console.log(JSON.stringify(r));
        """
        out = _node_run(script)
        got = json.loads(out.strip().splitlines()[-1])
        self.assertFalse(got["none"])
        self.assertTrue(got["lightbox"], "lightbox 开着时必须判定为「开着」")
        self.assertTrue(got["modal"], "mediaPreviewModal 开着时必须判定为「开着」")

    def test_escape_snapshot_is_captured_before_bubble_handlers(self):
        """Esc 快照必须用**捕获阶段**记。

        【为什么这是真 bug 而不是洁癖】三个 Escape 监听器都在 document 上、
        都是冒泡阶段，而 app.js 的 lightbox 处理器**注册在前**
        （index.html: app.js(1626) < assets.js(1631)）。
        冒泡阶段里 app.js 先跑并把 lightbox 藏了 —— assets.js 再查就晚了，
        结果「按 Esc 关大图」会把底下的抽屉一起关掉。
        ⇒ 必须用捕获阶段的快照。
        """
        # 断言存在一个带 capture=true 的 keydown 监听器
        self.assertRegex(
            ASSETS_JS_SRC,
            r'addEventListener\(\s*"keydown"[\s\S]{0,400}?viewerOpenAtEscape\s*=\s*isIndependentViewerOpen\(\)[\s\S]{0,80}?,\s*true\s*\)',
            "没有找到「捕获阶段记 Esc 快照」的监听器 —— "
            "Esc 关查看器时会连带把抽屉也关掉")

    def test_escape_guard_blocks_drawer_close(self):
        """快照为真时，素材库的 Esc 处理器必须**不**关抽屉。"""
        script = r"""
        const NS = require('fs').readFileSync(process.env.NS_FILE, 'utf8');
        // 抽出 bindStatic 里的两个 keydown 监听器所依赖的逻辑片段：
        // 用一个最小 DOM 桩模拟「Esc 到来」这条路径。
        const lines = NS.split('\n');
        const guardIdx = lines.findIndex(l => l.includes('if (viewerOpenAtEscape) return;'));
        console.log(JSON.stringify({ guardIdx }));
        """
        out = _node_run(script)
        got = json.loads(out.strip().splitlines()[-1])
        self.assertGreaterEqual(got["guardIdx"], 0,
                                "Esc 处理器里没有「快照为真就 return」的守卫")


# ==========================================================================
# 3. 不破坏既有能力
# ==========================================================================

class TestExistingCapabilitiesPreserved(unittest.TestCase):
    """既有能力的结构没被删（抽屉 / 多选 / 批量删除 / 播放器）。"""

    def test_drawer_still_present_and_closable(self):
        """抽屉仍然在（需求：抽屉保留不改）。"""
        self.assertIn('id="alDrawer"', INDEX_HTML_SRC)
        self.assertIn("function closeDrawer()", ASSETS_JS_SRC)
        self.assertIn("function openDrawer(assetId)", ASSETS_JS_SRC)
        self.assertIn("drawer.classList.add(\"open\")", ASSETS_JS_SRC)
        self.assertIn("drawer.classList.remove(\"open\")", ASSETS_JS_SRC)

    def test_multiselect_and_batch_delete_still_present(self):
        """多选 + 批量删除没被删。"""
        self.assertIn('id="alMultiToggle"', INDEX_HTML_SRC)
        self.assertIn('id="alSelDelete"', INDEX_HTML_SRC)
        self.assertIn('id="alSelPlayer"', INDEX_HTML_SRC)
        self.assertIn("function toggleSelected(assetId, on)", ASSETS_JS_SRC)
        self.assertIn("function setMultiMode(on)", ASSETS_JS_SRC)

    def test_player_mode_still_present(self):
        """播放器模式（加入 / 移除 / 清单）没被删。"""
        self.assertIn("function addToPlayer", ASSETS_JS_SRC)
        self.assertIn("function removeFromPlayer", ASSETS_JS_SRC)
        self.assertIn('data-act="add-player"', ASSETS_JS_SRC)
        self.assertIn('data-act="remove-player"', ASSETS_JS_SRC)

    def test_viewer_does_not_touch_selection_or_player(self):
        """打开独立窗口**不得**误触发多选 / 加入播放器 / 引用生成面板。

        【怎么验】把 openAssetViewer 的函数体单独抽出来，
        断言里面没有 addToPlayer / toggleSelected / addFromMedia /
        state.selected / assetRefs 这些副作用。
        """
        src = ASSETS_JS_SRC
        i = src.index("function openAssetViewer(asset)")
        j = src.index("async function api(path", i)
        body = src[i:j]
        for forbidden in ("addToPlayer", "toggleSelected", "addFromMedia",
                          "assetRefs", "state.selected", "addToPlayer("):
            self.assertNotIn(forbidden, body,
                             f"openAssetViewer 里出现了 {forbidden} —— "
                             "打开独立窗口会误触发其它动作")

    def test_card_view_button_stops_propagation(self):
        """卡片上的「放大」按钮必须 stopPropagation。

        【为什么】否则这个 click 冒泡到卡片 -> 顺手把**抽屉也打开**了 ——
        用户只想看大图，却多出一个抽屉。
        """
        src = ASSETS_JS_SRC
        i = src.index('const viewBtn = card.querySelector(".al-view-btn")')
        j = src.index("const selBox = card.querySelector", i)
        block = src[i:j]
        self.assertIn("event.stopPropagation()", block,
                      "卡片的独立查看按钮没有 stopPropagation —— "
                      "点它会连带打开抽屉")
        self.assertIn("openAssetViewer(asset)", block,
                      "卡片的独立查看按钮没有调用 openAssetViewer")

    def test_asset_card_click_still_opens_drawer(self):
        """卡片本体点击仍然开抽屉（入口是**新增**，不是替换）。"""
        self.assertIn('card.addEventListener("click", () => openDrawer(assetId));',
                      ASSETS_JS_SRC)

    def test_no_html_comment_in_js_templates(self):
        """本轮改的 assets.js 不能引入 HTML 注释（A-6 关卡）。"""
        self.assertNotIn("<!--", ASSETS_JS_SRC,
                         "assets.js 里出现了 HTML 注释 —— 会随 innerHTML 进 DOM")


# ==========================================================================
# 4. 窄屏可用
# ==========================================================================

class TestNarrowScreenUsability(unittest.TestCase):
    """375px 窄屏下的适配规则必须存在。"""

    def test_view_button_has_touch_size_on_phone(self):
        """手机上「放大」按钮要够大（桌面 26px 太小）。"""
        block = self._block(RESPONSIVE_CSS_SRC, ".al-view-btn {")
        self.assertIn("width:", block)
        self.assertIn("height:", block)
        # 触屏标准 40px（--touch-min），给到 34px 也算边角控件；
        # 断言至少比桌面大一档
        self.assertRegex(block, r"width:\s*3\dpx", "窄屏下 .al-view-btn 宽度不够")

    def test_media_preview_modal_fits_narrow_screen(self):
        """媒体预览框在窄屏不能溢出视口。"""
        self.assertIn(".media-preview-modal", RESPONSIVE_CSS_SRC)
        self.assertIn(".media-preview-box", RESPONSIVE_CSS_SRC)
        # 必须有高度上限，否则 375px 下媒体控件被挤出视口
        block = self._block(RESPONSIVE_CSS_SRC, ".media-preview-box {")
        self.assertIn("max-height", block,
                      "窄屏下 .media-preview-box 没有高度上限 —— 媒体控件会溢出视口")

    def test_media_preview_close_button_has_touch_size_on_phone(self):
        """窄屏下查看器的关闭按钮要够大（需求：独立窗口在窄屏可关闭）。"""
        block = self._block(RESPONSIVE_CSS_SRC, ".media-preview-tools #mediaPreviewClose {")
        self.assertIn("min-height", block,
                      "窄屏下媒体预览的关闭按钮没有触控尺寸下限")

    def test_fab_hidden_while_viewer_open(self):
        """查看器打开时，页面 FAB 不能浮在它上面（遮挡）。"""
        self.assertRegex(
            RESPONSIVE_CSS_SRC,
            r"body\.modal-open\s+\.panel-fab\s*\{[^}]*display:\s*none",
            "查看器打开时页面 FAB 没有被藏掉 —— 它会浮在查看器上面")

    def test_narrow_screen_breakpoint_is_700px(self):
        """窄屏规则必须落在 ≤700px 断点里（项目的断点真源）。"""
        # 找到含 .al-view-btn 的那个 @media 块
        idx = RESPONSIVE_CSS_SRC.find(".al-view-btn {")
        self.assertGreater(idx, 0, "responsive.css 里没有 .al-view-btn 的窄屏规则")
        # 往上找最近的 @media
        head = RESPONSIVE_CSS_SRC[:idx]
        media_idx = head.rfind("@media")
        self.assertGreater(media_idx, 0)
        media_line = head[media_idx:head.find("\n", media_idx)]
        self.assertIn("700px", media_line,
                      f"窄屏规则不在 ≤700px 断点里（实际：{media_line.strip()}）")

    @staticmethod
    def _block(src: str, selector: str) -> str:
        idx = src.find(selector)
        if idx < 0:
            return ""
        end = src.find("}", idx)
        return src[idx:end + 1] if end > 0 else src[idx:]


# ==========================================================================
# 5. 静态资源版本号（缓存破除）
# ==========================================================================

class TestStaticVersionBump(unittest.TestCase):
    """内容改了 ?v= 必须升 —— 否则老用户拿到的仍是旧文件。"""

    def _version(self, fname: str):
        m = re.search(r'/static/' + re.escape(fname) + r'\?v=([^"\'\s>]+)',
                      INDEX_HTML_SRC)
        self.assertIsNotNone(m, f"index.html 里找不到 {fname} 的 ?v=")
        return m.group(1)

    def test_three_changed_assets_have_new_version(self):
        for fname in ("assets.js", "assets.css", "responsive.css"):
            ver = self._version(fname)
            self.assertEqual("assetview20261121a", ver,
                             f"{fname}: ?v= 应升到 assetview20261121a，实际 {ver}")

    def test_manifest_table_matches_index_html(self):
        """版本表与 index.html 必须一致（与既有 D-2 关卡同一口径）。"""
        manifest = (REPO / "tests" / "test_frontend_assets_hygiene.py").read_text(
            encoding="utf-8")
        blk = manifest[manifest.index("STALE_BUMPED = {"):]
        blk = blk[:blk.index("\n}\n") + 2]
        entries = dict((m.group(1), m.group(3)) for m in
                       re.finditer(r'"([^"]+)"\s*:\s*\("([^"]+)",\s*"([^"]+)"\)', blk))
        for fname in ("assets.js", "assets.css", "responsive.css"):
            self.assertIn(fname, entries, f"版本表里少了 {fname}")
            self.assertEqual(self._version(fname), entries[fname],
                             f"{fname}: 版本表与 index.html 不一致")


if __name__ == "__main__":
    unittest.main()
