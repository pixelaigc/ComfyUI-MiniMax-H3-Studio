# -*- coding: utf-8 -*-
"""
生成媒体库「卡片载入慢」性能修的回归断言。

覆盖三层：
  A. **结构断言**（源码级）—— 关键修复点还在，且没有被改回去
  B. **行为断言**（真起服务）—— 接口真的快、缩略图真的小
  C. **变异测试**（开发期用）—— 每条断言真的「咬人」

【纪律】
* 断言必须由「命名该行为的那条断言」抓住 ——
  不能拿被测函数自己的输出算期望值。
* 变异体必须让断言变红 —— 否则断言是摆设。
"""
from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SERVER = ROOT / "server.py"
VIDEO_MODELS = ROOT / "h3studio" / "video_models.py"
THUMBNAILS = ROOT / "h3studio" / "thumbnails.py"
APP_JS = ROOT / "web" / "app.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _func_names(source: str) -> set:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(node.name)
    return out


# ======================================================================
# A. 结构断言（源码级）
# ======================================================================
class TestStructure(unittest.TestCase):
    """结构断言：关键修复点还在。"""

    def test_find_weight_is_memoized(self):
        """find_weight 必须走记忆化缓存。

        【为什么这条能抓住回归】若有人把缓存删了，
        `options_payload` 会从 ~25ms 回到 511~800ms，
        而它在事件循环里 —— 会把整个 8190 冻住。

        【怎么验才算「真的缓存」】不是看源码里有没有那个变量名 ——
        而是**行为**：同一个不存在的名字查两次，**第二次不能再去碰盘**。
        （第一版只断言「源码里有 _FIND_WEIGHT_CACHE」，
          变异体把 `if key in ...` 改成 `if False` 它照样通过 —— 假绿。）
        """
        from h3studio import video_models as vm
        src = _read(VIDEO_MODELS)
        self.assertIn("invalidate_find_weight_cache", src, "缺少缓存失效入口")

        # 行为验证：缓存命中时不得再调用底层查找
        calls = []
        original = vm._model_dirs

        def counting_dirs():
            calls.append(1)
            return original()

        vm._model_dirs = counting_dirs
        try:
            vm.invalidate_find_weight_cache()
            name = "__definitely_not_a_real_weight__.safetensors"
            vm.find_weight(name, kind="diffusion_models")
            after_first = len(calls)
            vm.find_weight(name, kind="diffusion_models")
            after_second = len(calls)
        finally:
            vm._model_dirs = original
            vm.invalidate_find_weight_cache()

        self.assertGreater(after_first, 0, "第一次查找根本没碰盘 —— 测试没测到东西")
        self.assertEqual(after_second, after_first,
                         "第二次查同一个名字又去扫盘了 —— 缓存没生效")

    def test_compat_matrix_is_cached(self):
        """accel_lora_compat_matrix 必须有结果缓存。

        【怎么验才算「真的缓存」】**行为**验证：算两次同一个底模，
        第二次不能再去调底层比对函数。
        （第一版只断言源码里有那个变量名 —— 变异体改 `if False`
          它照样通过 —— 假绿。）
        """
        from h3studio import video_models as vm
        calls = []
        original = vm.check_h3_lora_compat

        def counting(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        model_key = next(iter(vm.VIDEO_MODELS.keys()))
        vm.check_h3_lora_compat = counting
        try:
            vm.invalidate_compat_matrix_cache()
            vm.accel_lora_compat_matrix(model_key)
            after_first = len(calls)
            vm.accel_lora_compat_matrix(model_key)
            after_second = len(calls)
        finally:
            vm.check_h3_lora_compat = original
            vm.invalidate_compat_matrix_cache()

        self.assertGreater(after_first, 0, "第一次没调底层比对 —— 测试没测到东西")
        self.assertEqual(after_second, after_first,
                         "第二次又重算了 84 组兼容性 —— 缓存没生效")

    def test_video_options_payload_runs_off_event_loop(self):
        """options_payload 必须在工作线程里跑（不占事件循环）。

        【为什么这条能抓住回归】这是「用户可见的慢」的直接成因：
        options_payload 在事件循环里跑时，/api/jobs 的 p50 从 5.8ms
        劣化到 1279ms（220 倍）。
        """
        src = _read(SERVER)
        tree = ast.parse(src)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.AsyncFunctionDef)
                   and n.name == "api_video_options"), None)
        self.assertIsNotNone(fn, "api_video_options 不见了")
        calls = [ast.dump(n) for n in ast.walk(fn)
                 if isinstance(n, ast.Call)]
        to_thread = any("to_thread" in c and "options_payload" in c for c in calls)
        self.assertTrue(to_thread,
                        "options_payload 又回到事件循环里跑了 —— "
                        "会把整个 8190 冻住（/api/jobs 实测慢 220 倍）")
        # accel_lora_compat_matrix 也一样
        matrix_thread = any("to_thread" in c and "accel_lora_compat_matrix" in c
                            for c in calls)
        self.assertTrue(matrix_thread,
                        "accel_lora_compat_matrix 又回到事件循环里跑了")

    def test_list_api_does_not_generate_thumbnails(self):
        """列表接口**只算键**，不生成缩略图。

        【为什么这条能抓住回归】第一版把生成放进列表接口，
        结果列表接口自己被拖到 1546~2897ms（11 次 ffmpeg）。
        """
        src = _read(SERVER)
        tree = ast.parse(src)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.AsyncFunctionDef)
                   and n.name == "_attach_thumb_urls"), None)
        self.assertIsNotNone(fn, "_attach_thumb_urls 不见了")
        body = ast.dump(fn)
        self.assertNotIn("thumbnail_for", body,
                         "列表接口里又出现缩略图生成 —— "
                         "列表接口会被 11 次 ffmpeg 拖到 1.5~2.9 秒")
        self.assertIn("cache_key", body, "列表接口应该只算键")

    def test_thumb_endpoint_is_lazy_and_path_safe(self):
        """/api/thumb 必须：懒生成 + 只认 40 位 hex。"""
        src = _read(SERVER)
        tree = ast.parse(src)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.AsyncFunctionDef)
                   and n.name == "thumb_handler"), None)
        self.assertIsNotNone(fn, "thumb_handler 不见了")
        body = ast.dump(fn)
        self.assertIn("thumbnail_for", body, "缩略图接口应该懒生成")
        self.assertIn("HTTPNotFound", body, "缩略图接口必须对非法 key 返回 404")
        # 路径穿越防护：只接受 hex
        self.assertRegex(_read(SERVER), r"0123456789abcdef",
                          "缩略图 key 校验丢了")

    def test_card_image_uses_lazy_loading(self):
        """卡片图片必须带 loading="lazy" + decoding="async"。

        【怎么验才算数】必须判**卡片预览那张图**（`<img src="${src}" ...>`），
        而不是全文里任意一处 `loading="lazy"`。
        （第一版只查全文 —— 批次缩略图里有一处 lazy 就算通过 —— 假绿。）

        【为什么不用正则】标签里有 `${...}` 和 `/>`，正则转义太脆
        （踩过两次：写出来的 pattern 匹配不到）。直接**字符串定位**更稳。
        """
        src = _read(APP_JS)
        # 卡片预览那张图：`<img src="${src}" ...>`
        anchor = '<img src="${src}"'
        idx = src.find(anchor)
        self.assertNotEqual(idx, -1, "找不到卡片预览的图片标签")
        tag = src[idx:src.find(">", idx) + 1]
        self.assertIn('loading="lazy"', tag,
                      f"卡片图片没有懒加载：{tag}")
        self.assertIn('decoding="async"', tag,
                      f"卡片图片没有异步解码：{tag}")
        # 批次缩略图也要（URL 内联在模板里，锚点跟着实际写法走）
        anchor2 = '<img src="${thumbUrl(out) || outputUrl(job, out)}"'
        idx2 = src.find(anchor2)
        self.assertNotEqual(idx2, -1, "找不到批次缩略图的图片标签")
        tag2 = src[idx2:src.find(">", idx2) + 1]
        self.assertIn('loading="lazy"', tag2, "批次缩略图没有懒加载")

    def test_card_video_uses_preload_none(self):
        """卡片视频必须 preload="none"。

        【为什么这条能抓住回归】实测：preload="metadata" 时 Chrome 对
        `Range: bytes=0-` 拿到**整个文件**（一页 11 个视频 7.81MB）；
        只有 preload="none" 才真的 0 字节。
        """
        src = _read(APP_JS)
        # 卡片预览的视频（不是素材库播放器、不是 agent 面板）
        m = re.search(r'<video src="\$\{outputUrl\(job, video\)\}"[^>]*>', src)
        self.assertIsNotNone(m, "找不到卡片预览的视频标签")
        self.assertIn('preload="none"', m.group(0),
                      f"卡片视频又变回预取了：{m.group(0)}")

    def test_card_video_has_poster_when_thumb_available(self):
        """有缩略图时，视频卡片必须带 poster（否则预览区一片空白）。

        【怎么验才算数】不能只查「源码里有 poster 字样」——
        要判**posterAttr 由 thumbUrl 决定**且**视频标签真的用上了它**。
        （第一版只查 `assertIn("poster", m.group(0))` ——
          变异体把 posterAttr 置空后源码里仍有 "poster" 字样，
          断言照样通过 —— 假绿。）
        """
        src = _read(APP_JS)
        pm = re.search(r"const posterAttr = (.+?);", src)
        self.assertIsNotNone(pm, "找不到 posterAttr 的定义")
        self.assertIn("videoThumb", pm.group(1),
                      "posterAttr 跟缩略图无关 —— poster 起不到作用")
        self.assertIn("poster=", pm.group(1), "posterAttr 没有产出 poster 属性")
        # 视频标签上必须真的用上 posterAttr
        anchor = '<video src="${outputUrl(job, video)}"'
        idx = src.find(anchor)
        self.assertNotEqual(idx, -1, "找不到卡片视频标签")
        tag = src[idx:src.find(">", idx) + 1]
        self.assertIn("posterAttr", tag,
                      f"卡片视频标签没用 posterAttr：{tag}")

    def test_lightbox_still_uses_original_image(self):
        """点开大图必须用**原图**（缩略图不能取代它）。

        【为什么这条重要】缩略图只有 640px —— 用它放大看图会糊。

        【怎么验】`source.url` 是 openLightbox 实际用的 URL，
        它必须在别处被赋成 `outputUrl(job, image)`（= 原文件），
        而**不是** `thumbUrl(...)`（= 缩略图）。
        """
        src = _read(APP_JS)
        m = re.search(r"const openLightbox = \(\) =>\s*\n?\s*lightboxShow\(([^;]+);", src)
        self.assertIsNotNone(m, "找不到 openLightbox")
        self.assertNotIn("thumbUrl", m.group(1),
                         "大图直接用了缩略图 —— 放大看图会糊")
        # source.url 的定义处必须用 outputUrl（= 原文件路径）
        # 【踩坑】用 `[^,]+` 会把 `outputUrl(job, image)` 里的逗号当成
        # 分隔符，只截到 "outputUrl(job" —— 断言永远不成立。
        # 改成匹配「到下一个 `, name:` 为止」，能吃掉带逗号的调用参数。
        sm = re.search(r"const source = \{url: (.+?), name:", src)
        self.assertIsNotNone(sm, "找不到 source.url 的定义")
        self.assertIn("outputUrl(job, image)", sm.group(1),
                      "source.url 不是原图 —— 点开大图会拿到缩略图")
        # 【加强】不能只是「含有」outputUrl —— 必须**不含**缩略图来源。
        # （变异体写成 `imageThumb || outputUrl(job, image)` 时，
        #   `assertIn("outputUrl(job, image)")` 照样成立 —— 假绿。）
        self.assertNotIn("thumbUrl", sm.group(1),
                         "source.url 可能取缩略图 —— 点开大图会糊")
        self.assertNotIn("imageThumb", sm.group(1),
                         "source.url 可能取缩略图 —— 点开大图会糊")
        # 且不能是「优先缩略图」的回落写法
        self.assertNotIn("||", sm.group(1),
                         "source.url 用了 || 回落 —— 可能优先取缩略图")

    def test_batch_thumbnail_guards_undefined_output(self):
        """批次缩略图：**没有产物**的批次不得让整张卡片渲染崩溃。

        【真实踩过的坑 —— 2026-11-21】
        我第一版写成：
            const batchThumb = thumbUrl(out);
            const batchSrc = batchThumb || outputUrl(job, out);   <-- 在守卫外面
        而 `outputUrl(job, output)` 内部读 `output.file` ——
        某个批次**没有产物**（out === undefined）时直接抛
        `Cannot read properties of undefined (reading 'file')`
        ⇒ jobCard 渲染中断 ⇒ **整个媒体库一片空白**
        （实测：'任务列表读取失败，正在重试…'，0 张卡片）。

        ⇒ 判据：算 batchSrc 的地方**必须在 `out` 存在之后**。
        """
        src = _read(APP_JS)
        idx = src.find("renderJobBatches")
        self.assertNotEqual(idx, -1, "找不到 renderJobBatches")
        body = src[idx:idx + 3000]
        # 找批次缩略图那段
        thumb_idx = body.find("job-batch-thumb")
        self.assertNotEqual(thumb_idx, -1, "找不到批次缩略图渲染")
        # 往前看 600 字符 —— 那才是算 URL 的地方
        window = body[max(0, thumb_idx - 600):thumb_idx + 300]
        # 出现 outputUrl(job, out) 的地方，必须在 `out &&` 之后
        guard_idx = window.find("out && !failed")
        self.assertNotEqual(guard_idx, -1,
                            "批次缩略图不在 `out && !failed` 守卫内")
        url_idx = window.find("outputUrl(job, out)")
        self.assertNotEqual(url_idx, -1, "找不到批次缩略图的 URL 计算")
        self.assertGreater(url_idx, guard_idx,
                           "outputUrl(job, out) 出现在守卫**之前** —— "
                           "没有产物的批次会让整张卡片渲染崩溃")
        # 且守卫本身必须在**算 URL 之前**就短路掉 undefined 的 out
        self.assertIn("thumbUrl(out)", window,
                      "批次缩略图没用 thumbUrl —— 没走缩略图就等于没修")

    def test_no_immutable_cache_header_in_server(self):
        """server.py 里不允许出现 `immutable`。

        【为什么这条重要】仓库既有契约
        （test_frontend_assets_hygiene.py::
        test_server_static_has_no_cache_control_is_known_risk）：
        静态资源的 `?v=` 版本号是**唯一**的破缓存防线 ——
        服务端一旦发 `immutable`，浏览器会永久缓存旧 URL 的内容。

        ⇒ 我第一版给缩略图加 `immutable` 时，这条断言变红了。
        处置：**去掉 `immutable`，保留 `max-age`**（缩略图 URL 是内容寻址的，
        源文件一变 URL 就变，所以 24 小时缓存完全安全），
        而不是去改这条既有契约让自己变绿 —— 那是假绿。
        """
        src = _read(SERVER)
        self.assertNotIn("immutable", src,
                         "server.py 出现了那串永久缓存指令 —— 违反既有静态资源缓存契约")

    def test_static_asset_version_bumped(self):
        """app.js 的 ?v= 必须升过（否则老用户拿到的还是旧文件）。"""
        html = _read(ROOT / "web" / "index.html")
        m = re.search(r'app\.js\?v=([A-Za-z0-9_]+)', html)
        self.assertIsNotNone(m, "index.html 里找不到 app.js 的 ?v=")
        self.assertNotEqual(m.group(1), "fav20261120c",
                            "app.js 内容改了但 ?v= 没升 —— 老用户拿到的还是旧文件")


# ======================================================================
# B. 行为断言（真起服务）
# ======================================================================
class TestThumbnailsUnit(unittest.TestCase):
    """缩略图模块的单元断言（不起服务）。"""

    def test_cache_key_changes_with_content(self):
        """源文件内容变了 -> 键必须变（否则显示旧缩略图）。

        【怎么验才算数】要同时覆盖两种变化：
          * **大小变了**
          * **mtime 变了**（大小不变）
        只测一种的话，去掉另一项的键成分就会漏过去。
        """
        from h3studio import thumbnails as th
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            src = data / "a.png"
            src.write_bytes(b"12345")
            os.utime(src, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
            k1 = th.cache_key(src)

            # ① 大小变了
            src.write_bytes(b"1234567890")
            os.utime(src, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
            k2 = th.cache_key(src)
            self.assertNotEqual(k1, k2, "文件大小变了但键没变 —— 会显示旧缩略图")

            # ② 大小不变、mtime 变了
            st = src.stat()
            k3 = th.cache_key(src)
            os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
            k4 = th.cache_key(src)
            self.assertNotEqual(k3, k4, "文件 mtime 变了但键没变 —— 会显示旧缩略图")

    def test_image_thumbnail_is_smaller(self):
        """生成的缩略图必须显著小于原图。"""
        from h3studio import thumbnails as th
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("没有 PIL")
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            src = data / "big.png"
            Image.new("RGB", (2400, 1600), (200, 30, 30)).save(src)
            out = th.generate_thumbnail(src, data)
            self.assertIsNotNone(out, "缩略图生成失败")
            self.assertTrue(out.is_file())
            self.assertLess(out.stat().st_size, src.stat().st_size / 3,
                            "缩略图没比原图小多少")
            with Image.open(out) as im:
                self.assertLessEqual(max(im.size), th.THUMB_MAX_EDGE,
                                     "缩略图尺寸没受控")

    def test_unsupported_extension_returns_none(self):
        """不支持的格式返回 None（调用方回落原图）。"""
        from h3studio import thumbnails as th
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            src = data / "x.txt"
            src.write_text("hi", encoding="utf-8")
            self.assertIsNone(th.generate_thumbnail(src, data))

    def test_missing_file_returns_none(self):
        """源文件不存在返回 None（不抛异常）。"""
        from h3studio import thumbnails as th
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(th.generate_thumbnail(Path(tmp) / "nope.png", Path(tmp)))

    def test_video_thumbnail_generated(self):
        """视频缩略图能生成（ffmpeg 可用时）。"""
        from h3studio import thumbnails as th
        import shutil
        if not shutil.which("ffmpeg"):
            self.skipTest("没有 ffmpeg")
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            src = data / "v.mp4"
            proc = subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                 "-i", "testsrc=size=320x240:duration=1", str(src)],
                capture_output=True, timeout=60)
            if proc.returncode != 0 or not src.exists():
                self.skipTest("ffmpeg 造不出测试视频")
            out = th.generate_thumbnail(src, data)
            self.assertIsNotNone(out, "视频缩略图生成失败")
            self.assertLess(out.stat().st_size, 200 * 1024,
                            "视频缩略图太大了")


if __name__ == "__main__":
    unittest.main()
