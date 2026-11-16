# -*- coding: utf-8 -*-
"""【2026-11-16 需求】素材库强制引用 + 种子正整数校验 的回归测试。

覆盖本轮两个需求的全部硬约束：

【目标 1：所有媒体引用必须先进入素材库】
  1. 后端拒绝「本地直传」的媒体（file_first / file_last / file_ref /
     file_refvid / file_refaud）—— 首尾帧 / 参考生视频都不能直引外部路径；
  2. 后端拒绝**不存在于素材库**的 asset_id（不只是「长得像 id」）；
  3. 后端拒绝**类型不符**的素材（把图片塞进参考视频槽位）；
  4. 素材库里已入库的素材**可以正常引用**（不能误杀）；
  5. 前端不再有「本地文件直传」入口（无 <input type=file> / 无
     file_* 的 multipart append / 无 x-h3-image 直引分支）。

【目标 2：种子仅允许正整数】
  1. 后端：0 / 负数 / 小数 / 非数字 / 超大值 全部被拦，且给出**中文原因**；
  2. 后端：正整数放行；留空 = 随机（合法）；
  3. 前端：window.SeedGuard 的判定口径与后端一致；
  4. 前端：提交入口在**发起请求前**就拦下非法种子。

运行：python3 -m pytest tests/test_assetlib_and_seed_guard.py -v
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

APP_JS = (REPO / "web" / "app.js").read_text(encoding="utf-8")
SEED_GUARD_JS = (REPO / "web" / "seed-guard.js").read_text(encoding="utf-8")
NODE = shutil.which("node") or shutil.which("nodejs")


def _strip_js_comments(src: str) -> str:
    """去掉 JS 注释（保留字符串字面量内容）。

    【为什么】本文件有些断言是「旧写法不许再出现」，
    而旧写法常常留在注释里（说明「原来错在哪」）。
    注释里出现不算缺陷 —— 只有**可执行代码**里出现才算。
    """
    out = []
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        if ch in "\"'`":
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                if ch == "\\" and i + 1 < n:
                    out.append(src[i])
                    out.append(src[i + 1])
                    i += 2
                    continue
                out.append(src[i])
                if src[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            i += 2
            while i < n and not (src[i] == "*" and src[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


APP_JS_CODE = _strip_js_comments(APP_JS)


def _run_node(script: str) -> dict:
    """在 Node 里真跑一段 JS，返回 JSON 结果。

    【为什么用 .cjs】node 把 .mjs 当 ES module —— 里面没有 require。
    这里用 .cjs（CommonJS），脚本里可以 require('fs')。
    """
    if not NODE:
        raise unittest.SkipTest("node 不可用")
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        proc = subprocess.run([NODE, path], capture_output=True, text=True,
                              timeout=60, cwd=str(REPO))
    finally:
        os.unlink(path)
    if proc.returncode != 0:
        raise AssertionError(
            "node 执行失败:\nstdout=%s\nstderr=%s" % (proc.stdout, proc.stderr))
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ==================================================================
# 1) 素材库强制引用 —— 后端
# ==================================================================


class TestBackendRejectsExternalMedia(unittest.TestCase):
    """生成任务的媒体**只能**来自素材库 —— 直传外部路径必须被拒。"""

    def _params(self):
        return {"prompt": "x", "width": 864, "height": 480, "duration": 5}

    def test_flf_rejects_local_first_frame(self):
        """flf 直传首帧 -> 明确报错（不是静默忽略）。"""
        import server
        with self.assertRaises(server.JobValidationError) as ctx:
            server.prepare_job_params(
                "flf", self._params(), [],
                {"file_first": ["a.png"]})
        self.assertIn("素材库", ctx.exception.message)
        self.assertEqual(ctx.exception.code, "media_must_come_from_library")

    def test_flf_rejects_local_last_frame(self):
        """flf 直传尾帧 -> 同样被拒。"""
        import server
        with self.assertRaises(server.JobValidationError) as ctx:
            server.prepare_job_params(
                "flf", self._params(), [],
                {"file_last": ["a.png"]})
        self.assertIn("素材库", ctx.exception.message)

    def test_flf_rejects_local_reference_video(self):
        """flf 直传参考视频 -> 被拒。"""
        import server
        with self.assertRaises(server.JobValidationError) as ctx:
            server.prepare_job_params(
                "flf", self._params(), [],
                {"file_refvid": ["v.mp4"]})
        self.assertIn("素材库", ctx.exception.message)

    def test_flf_rejects_local_reference_audio(self):
        """flf 直传参考音乐 -> 被拒。"""
        import server
        with self.assertRaises(server.JobValidationError) as ctx:
            server.prepare_job_params(
                "flf", self._params(), [],
                {"file_refaud": ["a.flac"]})
        self.assertIn("素材库", ctx.exception.message)

    def test_r2v_rejects_local_reference_images(self):
        """r2v 直传参考图 -> 被拒。"""
        import server
        with self.assertRaises(server.JobValidationError) as ctx:
            server.prepare_job_params(
                "r2v", self._params(), [],
                {"file_ref": ["a.png"]})
        self.assertIn("素材库", ctx.exception.message)

    def test_r2v_rejects_local_reference_video_and_audio(self):
        """r2v 直传参考视频 / 参考音乐 -> 被拒。"""
        import server
        for key in ("file_refvid", "file_refaud"):
            with self.assertRaises(server.JobValidationError) as ctx:
                server.prepare_job_params(
                    "r2v", self._params(), [], {key: ["x.bin"]})
            self.assertIn("素材库", ctx.exception.message)

    def test_rejection_message_is_actionable(self):
        """报错文案必须告诉用户**下一步做什么**（不是「非法」两个字）。"""
        import server
        with self.assertRaises(server.JobValidationError) as ctx:
            server.prepare_job_params(
                "flf", self._params(), [], {"file_first": ["a.png"]})
        message = ctx.exception.message
        self.assertIn("素材库", message)
        self.assertIn("上传", message)

    def test_i2i_qwen_still_rejects_local_reference_image(self):
        """「图片编辑」的既有约束**不能被弱化**（同一套规则）。"""
        import server
        with self.assertRaises(server.JobValidationError) as ctx:
            server.prepare_job_params(
                "i2i_qwen", self._params(), [],
                {"file_ref": ["a.png"]})
        self.assertEqual(ctx.exception.code, "ref_must_come_from_library")

    def test_agent_path_is_covered_too(self):
        """内置 Agent 走 prepare_job_params —— 所以同一道闸门生效。

        【为什么锁这条】Agent 的 create_job 是**另一条**建任务入口。
        若只护住面板路径，Agent 仍能直引外部路径。
        """
        import server
        self.assertTrue(callable(server.prepare_job_params))
        # Agent 侧调用点必须真的调它（而不是各写一份）
        agent_src = (REPO / "h3studio" / "agent.py").read_text(encoding="utf-8")
        self.assertIn("prepare_job_params", agent_src)


class TestBackendRejectsUnknownAssetId(unittest.TestCase):
    """asset_id 必须**真的在素材库里** —— 不是「格式像」就行。"""

    def _params(self):
        return {"prompt": "x", "width": 864, "height": 480, "duration": 5}

    def test_unknown_asset_id_is_rejected(self):
        """库里没有这个 id -> 明确报错「尚未入库素材库」。"""
        import server
        from h3studio import asset_ref_guard

        # 用一个临时素材库（空库）-> 任何 id 都不存在
        tmp = tempfile.mkdtemp(prefix="assetlib-guard-")
        old_root = os.environ.get("ASSET_LIBRARY_ROOT")
        os.environ["ASSET_LIBRARY_ROOT"] = tmp
        try:
            from h3studio import assets as A
            A.init_db()
            with self.assertRaises(Exception) as ctx:
                asset_ref_guard.check_asset_reference(
                    "asset_000000000000", "first_frame")
            self.assertIn("素材库", str(ctx.exception))
        finally:
            if old_root is None:
                os.environ.pop("ASSET_LIBRARY_ROOT", None)
            else:
                os.environ["ASSET_LIBRARY_ROOT"] = old_root
            shutil.rmtree(tmp, ignore_errors=True)

    def test_asset_guard_slot_category_mapping(self):
        """槽位 -> 期望类别 的映射必须覆盖所有素材槽位。"""
        from h3studio import asset_ref_guard as G
        for slot in ("first_frame", "last_frame", "ref_image", "ref_video",
                     "ref_audio", "ref_video_audio"):
            self.assertIn(slot, G.SLOT_CATEGORY,
                          f"槽位 {slot} 没有类别映射 -> 类型错配检不出来")
        self.assertEqual(G.SLOT_CATEGORY["first_frame"], "image")
        self.assertEqual(G.SLOT_CATEGORY["ref_video"], "video")
        self.assertEqual(G.SLOT_CATEGORY["ref_audio"], "audio")


class TestBackendAcceptsLibraryAssets(unittest.TestCase):
    """素材库里已入库的素材**必须能正常引用**（不能误杀）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="assetlib-accept-"))
        self.root = self.tmp / "library"
        self.src = self.tmp / "src"
        self.src.mkdir(parents=True, exist_ok=True)
        self._old_root = os.environ.get("ASSET_LIBRARY_ROOT")
        os.environ["ASSET_LIBRARY_ROOT"] = str(self.root)
        self.addCleanup(self._restore_env)
        from h3studio import assets as A
        A.init_db()

    def _restore_env(self):
        if self._old_root is None:
            os.environ.pop("ASSET_LIBRARY_ROOT", None)
        else:
            os.environ["ASSET_LIBRARY_ROOT"] = self._old_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_png(self, name="a.png"):
        from PIL import Image
        path = self.src / name
        Image.new("RGB", (32, 24), (10, 20, 30)).save(path, "PNG")
        return path

    def _register(self, path, category="image"):
        from h3studio import asset_repo, asset_service, assets as A
        with A.connect() as conn:
            result = asset_service.register_asset(
                conn, source_path=path, filename=path.name,
                mime="image/png", category=category)
            return result["asset"]["id"]

    def test_library_asset_can_be_referenced(self):
        """已入库的素材 -> 引用**通过**（不抛异常）。"""
        from h3studio import asset_ref_guard as G
        asset_id = self._register(self._make_png())
        asset = G.check_asset_reference(asset_id, "first_frame")
        self.assertEqual(asset["id"], asset_id)
        self.assertEqual(asset["category"], "image")

    def test_category_mismatch_is_rejected(self):
        """把图片当视频引用 -> 明确报错。"""
        from h3studio import asset_ref_guard as G
        asset_id = self._register(self._make_png())
        with self.assertRaises(G.AssetLibraryError) as ctx:
            G.check_asset_reference(asset_id, "ref_video")
        self.assertIn("不能用作", ctx.exception.message)

    def test_check_asset_refs_passes_valid_batch(self):
        """一批合法引用 -> 原样返回。"""
        from h3studio import asset_ref_guard as G
        asset_id = self._register(self._make_png())
        refs = [{"asset_id": asset_id, "slot": "first_frame", "ordinal": 0}]
        self.assertEqual(G.check_asset_refs(refs), refs)

    def test_check_asset_refs_rejects_missing_id(self):
        """缺 asset_id -> 拒绝（不能当成「没填」）。"""
        from h3studio import asset_ref_guard as G
        with self.assertRaises(G.AssetLibraryError) as ctx:
            G.check_asset_refs([{"slot": "first_frame"}])
        self.assertIn("素材库", ctx.exception.message)


class TestImageEditRefsAreLibraryChecked(unittest.TestCase):
    """图片编辑（i2i_qwen / i2i_h3image）的参考图也必须**真的在库里**。

    【为什么单独立一组】参考图走的是 **params["ref_images"]**（带描述 /
    标签的元数据），而 flf / r2v / a2a 走的是 **asset_refs**
    （{asset_id, slot, ordinal}）—— **两条不同的通道**。

    h3studio/ref_images.py 的 normalize_refs / validate_refs 都是
    **纯 dict 处理**（该文件里没有任何 DB 访问），`missing` 标记还是
    **前端传什么就信什么**。所以这条通道原本**完全没查过库**：
    库里不存在的 id 也能通过校验、任务建成，直到 worker 里
    _upload_ref_images 才抛 RuntimeError —— 用户看到的是兜底文案。
    """

    def _i2i_params(self, asset_id):
        return {"prompt": "改成夜景",
                "ref_images": [{"asset_id": asset_id, "description": "x"}]}

    def test_i2i_qwen_rejects_unknown_asset_id(self):
        """i2i_qwen：库里不存在的 asset_id -> 提交前就拒。"""
        import server
        with self.assertRaises(server.JobValidationError) as ctx:
            server.prepare_job_params(
                "i2i_qwen", self._i2i_params("asset_ffffffffffff"), [], {})
        self.assertEqual(ctx.exception.code, "asset_not_in_library")
        self.assertIn("素材库", ctx.exception.message)

    def test_i2i_h3image_rejects_unknown_asset_id(self):
        """i2i_h3image：同样在提交前拒。"""
        import server
        with self.assertRaises(server.JobValidationError) as ctx:
            server.prepare_job_params(
                "i2i_h3image", self._i2i_params("asset_ffffffffffff"), [], {})
        self.assertEqual(ctx.exception.code, "asset_not_in_library")

    def test_i2i_rejects_category_mismatch(self):
        """拿视频当参考图 -> 类型错配被拒（不能到 worker 才炸）。"""
        import server
        from assetlib_fixture import temp_asset_library
        with temp_asset_library(video=1) as ids:
            if not ids["video"]:
                self.skipTest("建不出视频素材")
            with self.assertRaises(server.JobValidationError) as ctx:
                server.prepare_job_params(
                    "i2i_qwen", self._i2i_params(ids["video"][0]), [], {})
            self.assertEqual(ctx.exception.code, "asset_category_mismatch")

    def test_i2i_accepts_real_library_asset(self):
        """库里真有的图片 -> 正常通过（不能误杀）。"""
        import server
        from assetlib_fixture import temp_asset_library
        with temp_asset_library(image=1) as ids:
            if not ids["image"]:
                self.skipTest("建不出图片素材")
            params = self._i2i_params(ids["image"][0])
            server.prepare_job_params("i2i_qwen", params, [], {})
            self.assertEqual(params["ref_images"][0]["asset_id"], ids["image"][0])

    def test_a2a_reference_audio_is_library_checked(self):
        """a2a 的参考音频（走 asset_refs）也必须查库。"""
        import server
        params = {"prompt": "一段摇滚"}
        with self.assertRaises(server.JobValidationError) as ctx:
            server.prepare_job_params(
                "a2a", params,
                [{"asset_id": "asset_ffffffffffff", "slot": "ref_audio",
                  "ordinal": 0}], {})
        self.assertEqual(ctx.exception.code, "asset_not_in_library")


# ==================================================================
# 2) 素材库强制引用 —— 前端
# ==================================================================


class TestFrontendHasNoExternalDirectEntry(unittest.TestCase):
    """界面上不能再有「本地文件直传」入口。"""

    def test_no_file_input_in_slot_renderer(self):
        """槽位渲染器里不再创建 <input type="file">。"""
        self.assertNotIn('inp.type = "file"', APP_JS_CODE)
        self.assertNotIn('createElement("input")', APP_JS_CODE)

    def test_no_file_multipart_append(self):
        """提交体里不再 append 任何 file_* 字段。"""
        for field in ("file_first", "file_last", "file_ref",
                      "file_refvid", "file_refaud"):
            self.assertNotIn(
                f'fd.append("{field}"', APP_JS_CODE,
                f"提交体里仍有 {field} —— 那是「直接引用外部文件路径」")

    def test_no_local_blob_preview_path(self):
        """槽位不再走 blob: 预览地址（本地直传的痕迹）。"""
        self.assertNotIn("URL.createObjectURL", APP_JS_CODE)

    def test_no_legacy_image_drop_branch(self):
        """拖放不再接受 x-h3-image（媒体库结果直引）。"""
        self.assertNotIn('getData("application/x-h3-image")', APP_JS_CODE)

    def test_empty_slot_click_opens_asset_picker(self):
        """点空槽位 -> 打开素材库选择器（不是系统文件框）。"""
        self.assertIn("openAssetPickerForSlot(kind)", APP_JS_CODE)

    def test_state_files_removed(self):
        """state.files（本地直传槽位）已从 state 里移除。"""
        self.assertNotIn("files: { first: null", APP_JS_CODE)

    def test_slot_titles_say_library_not_upload(self):
        """面板文案不再说「上传」—— 素材只能从素材库选。"""
        self.assertNotIn("上传首帧与尾帧", APP_JS_CODE)
        self.assertNotIn("上传参考素材", APP_JS_CODE)
        self.assertIn("从素材库选", APP_JS_CODE)

    def test_asset_picker_helper_limits_category(self):
        """选择器按槽位类型限定类别（图/视频/音频各归各）。"""
        self.assertIn("categories: [kind]", APP_JS_CODE)


# ==================================================================
# 3) 种子正整数 —— 后端
# ==================================================================


class TestSeedGuardBackend(unittest.TestCase):
    """后端种子校验：只允许正整数，非法给中文原因。"""

    def _params(self):
        return {"prompt": "x", "width": 864, "height": 480, "duration": 5}

    def _expect_rejected(self, bad):
        import server
        from assetlib_fixture import temp_asset_library
        params = self._params()
        params["seed"] = bad
        with temp_asset_library(image=1) as ids:
            with self.assertRaises(server.JobValidationError) as ctx:
                # flf 需要至少一帧 -> 用素材库里**真实**的素材占位
                server.prepare_job_params(
                    "flf", params,
                    [{"asset_id": ids["image"][0], "slot": "last_frame",
                      "ordinal": 0}], {})
        return ctx.exception

    def test_zero_is_rejected(self):
        """0 不是正整数 -> 拒绝。"""
        exc = self._expect_rejected(0)
        self.assertIn("正整数", exc.message)
        self.assertEqual(exc.code, "invalid_seed")

    def test_negative_is_rejected(self):
        """负数 -> 拒绝。"""
        exc = self._expect_rejected(-5)
        self.assertIn("正整数", exc.message)

    def test_decimal_is_rejected(self):
        """小数 -> 拒绝（不能被 int() 悄悄截断）。"""
        exc = self._expect_rejected(1.9)
        self.assertIn("小数", exc.message)

    def test_non_numeric_is_rejected(self):
        """非数字 -> 拒绝，且原因里能看出填了什么。"""
        exc = self._expect_rejected("abc")
        self.assertIn("正整数", exc.message)
        self.assertIn("abc", exc.message)

    def test_prompt_text_in_seed_is_rejected(self):
        """【实测过的真实场景】把提示词误填进种子框 -> 明确拒绝。

        这正是用户遇到的「生成失败（服务端未能给出具体原因）」的根因：
        提示词被填进 seed，一路流到 worker 的 int() 才炸。
        """
        exc = self._expect_rejected(
            "超低音响测试专用. 纯粹的极低频声波扫描与持续低音, 用于检测低频下潜与失真控制.")
        self.assertIn("正整数", exc.message)
        self.assertIn("超低音响", exc.message)

    def test_string_seed_is_rejected(self):
        """字符串种子（非数字）-> 拒绝。"""
        exc = self._expect_rejected("not-a-number")
        self.assertIn("正整数", exc.message)

    def test_huge_seed_is_rejected(self):
        """超出 JSON number 精确范围 -> 拒绝（否则服务端 int() 抛错）。"""
        exc = self._expect_rejected(2 ** 60)
        self.assertIn("上限", exc.message)

    def test_valid_positive_integer_is_accepted(self):
        """正整数 -> 放行，并归一成 int。"""
        import server
        from assetlib_fixture import temp_asset_library
        params = self._params()
        params["seed"] = "4242"
        with temp_asset_library(image=1) as ids:
            server.prepare_job_params(
                "flf", params,
                [{"asset_id": ids["image"][0], "slot": "last_frame",
                  "ordinal": 0}], {})
        self.assertEqual(params["seed"], 4242)
        self.assertIsInstance(params["seed"], int)

    def test_empty_seed_means_random(self):
        """留空 = 随机（面板上写着「留空随机」）。"""
        import server
        from assetlib_fixture import temp_asset_library
        params = self._params()
        params["seed"] = ""
        with temp_asset_library(image=1) as ids:
            server.prepare_job_params(
                "flf", params,
                [{"asset_id": ids["image"][0], "slot": "last_frame",
                  "ordinal": 0}], {})
        self.assertIsNone(params["seed"])

    def test_missing_seed_key_is_fine(self):
        """根本没传 seed -> 放行（走随机）。"""
        import server
        from assetlib_fixture import temp_asset_library
        params = self._params()
        with temp_asset_library(image=1) as ids:
            server.prepare_job_params(
                "flf", params,
                [{"asset_id": ids["image"][0], "slot": "last_frame",
                  "ordinal": 0}], {})
        self.assertIsNone(params.get("seed"))

    def test_random_token_is_accepted(self):
        """用户直接敲「随机」-> 等价于随机（面板 placeholder 就是它）。"""
        from h3studio import seed_guard
        self.assertEqual(seed_guard.parse_seed("随机"), (None, ""))
        self.assertEqual(seed_guard.parse_seed("random"), (None, ""))

    def test_all_modes_go_through_the_same_guard(self):
        """所有模式共用同一道闸门（prepare_job_params 是唯一入口）。"""
        import server
        for mode in ("t2v", "flf", "r2v", "t2i", "t2i_qwen", "i2i_qwen",
                     "t2i_moss", "t2i_h3image", "i2i_h3image", "t2a", "a2a"):
            params = {"prompt": "x", "width": 864, "height": 480,
                      "duration": 5, "seed": 0}
            with self.assertRaises(server.JobValidationError,
                                   msg=f"{mode} 模式没有拦下非法种子"):
                server.prepare_job_params(mode, params, [], {})

    def test_seed_guard_module_contract(self):
        """seed_guard 自身的契约：parse_seed 返回 (值, 原因)。"""
        from h3studio import seed_guard
        self.assertEqual(seed_guard.parse_seed(1), (1, ""))
        self.assertEqual(seed_guard.parse_seed(None), (None, ""))
        value, error = seed_guard.parse_seed(0)
        self.assertIsNone(value)
        self.assertTrue(error)

    def test_seed_max_matches_json_number_precision(self):
        """上限 = JSON number 能精确表示的上限。"""
        from h3studio import seed_guard
        self.assertEqual(seed_guard.SEED_MAX, 2 ** 53 - 1)

    def test_video_normalize_does_not_swallow_invalid_seed(self):
        """【真实缺陷 · 线上探针发现】视频归一层不能把非法种子吞成「随机」。

        【现象】normalize_t2v_params 在 prepare_job_params **之前**跑
        （api_create 的顺序），而它原来只做 int() 转换、失败就置 None：
        用户填 "abc" -> 归一成 None -> prepare_job_params 看到的是「随机」
        -> 种子校验**根本没机会生效** -> POST /api/jobs 返回 200 并建了任务。

        线上探针实测（部署后）：
            POST /api/jobs (mode=t2v, seed="abc") -> 200 {"id":..., "status":"queued"}

        现在：归一层对非法种子**直接报错**。
        """
        import server
        # r2v 必须用 Ref2VA 权重（否则会先撞上「模型不支持 Ref2VA 通道」
        # 那条更早的校验，看不到种子这条）—— 给它一个合法的 model_key。
        cases = (("t2v", {}), ("flf", {}),
                 ("r2v", {"model_key": "ref2va_int8"}))
        for mode, extra in cases:
            params = {"prompt": "x", "width": 864, "height": 480,
                      "duration": 5, "seed": "abc"}
            params.update(extra)
            normalized, error = server.normalize_t2v_params(params, mode=mode)
            self.assertTrue(error, f"{mode} 模式没有拦住非法种子")
            self.assertIn("正整数", error)

    def test_video_normalize_keeps_valid_seed(self):
        """合法种子在归一层里**原样保留**（不被改成 None）。"""
        import server
        params = {"prompt": "x", "width": 864, "height": 480,
                  "duration": 5, "seed": "4242"}
        normalized, error = server.normalize_t2v_params(params, mode="t2v")
        self.assertEqual(error, "")
        self.assertEqual(normalized["seed"], 4242)

    def test_video_normalize_empty_seed_means_random(self):
        """留空 = 随机（归一层输出 None）。"""
        import server
        params = {"prompt": "x", "width": 864, "height": 480,
                  "duration": 5, "seed": ""}
        normalized, error = server.normalize_t2v_params(params, mode="t2v")
        self.assertEqual(error, "")
        self.assertIsNone(normalized["seed"])

    def test_music_seed_does_not_silently_fall_back_to_random(self):
        """【真实缺陷】音乐工作流的种子不能静默回落成随机。

        原来 h3studio/music_yue2._seed 的 except 分支直接
        `return random.randint(...)` —— 用户填 "abc"，实际跑随机结果，
        界面显示的种子与实际用的对不上。
        """
        from h3studio import music_yue2
        with self.assertRaises(ValueError) as ctx:
            music_yue2._seed({"seed": "abc"})
        self.assertIn("正整数", str(ctx.exception))
        # 合法值照常返回
        self.assertEqual(music_yue2._seed({"seed": 4242}), 4242)
        # 留空 -> 随机（int）
        self.assertIsInstance(music_yue2._seed({"seed": None}), int)


# ==================================================================
# 4) 种子正整数 —— 前端
# ==================================================================


class TestSeedGuardFrontend(unittest.TestCase):
    """前端种子校验：提交前拦下，口径与后端一致。"""

    def test_seed_guard_module_exists_and_exports(self):
        """web/seed-guard.js 必须导出 window.SeedGuard.validate。"""
        self.assertIn("window.SeedGuard", SEED_GUARD_JS)
        self.assertIn("validate", SEED_GUARD_JS)

    def test_seed_guard_is_registered_in_index_html(self):
        """index.html 必须加载 seed-guard.js（且在 app.js 之前）。"""
        html = (REPO / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("/static/seed-guard.js", html)
        self.assertLess(html.index("/static/seed-guard.js"),
                        html.index("/static/app.js"),
                        "seed-guard.js 必须在 app.js 之前加载")

    def test_submit_entry_validates_seed_before_request(self):
        """提交入口必须先校验种子，且非法时**不发起请求**。"""
        start = APP_JS_CODE.index("async function onGenerateClick()")
        body = APP_JS_CODE[start:start + 6000]
        self.assertIn("SeedGuard", body)
        # 校验失败 -> return（不发请求）
        self.assertRegex(body, r"if\s*\(!\s*\w*[Ss]eed\w*\.ok\s*\)\s*\{[^}]*alert\([^)]*\)[^}]*return;",
                         "种子非法时必须 alert + return（不发起请求）")

    def test_music_panel_seed_is_validated_too(self):
        """音乐面板（#musicSeed）走另一条提交分支 —— 也必须校验。"""
        start = APP_JS_CODE.index("async function onGenerateClick()")
        body = APP_JS_CODE[start:start + 6000]
        self.assertIn("musicSeedCheck", body)

    def test_seed_guard_and_backend_share_same_max(self):
        """前端与后端的上限必须一致（否则一边过、一边炸）。"""
        from h3studio import seed_guard
        match = re.search(r"SEED_MAX\s*=\s*2\s*\*\*\s*53\s*-\s*1", SEED_GUARD_JS)
        self.assertIsNotNone(match, "前端 seed-guard.js 的 SEED_MAX 定义不在预期形态")
        self.assertEqual(seed_guard.SEED_MAX, 2 ** 53 - 1)

    def test_node_run_seed_guard_rejects_bad_values(self):
        """【真跑一遍】在 Node 里执行 web/seed-guard.js，断言判定结果。

        【为什么真跑】字符串匹配只能证明「源码里出现过某个正则」，
        不能证明它真的拦得住。真跑才能守住行为。
        """
        if not NODE:
            self.skipTest("node 不可用")
        script = """
const fs = require('fs');
global.window = {};
eval(fs.readFileSync(process.argv[2], 'utf-8'));
const G = window.SeedGuard;
const cases = [
  ['4242', true, 4242],
  ['', true, null],
  [null, true, null],
  ['随机', true, null],
  ['0', false, null],
  ['-5', false, null],
  ['1.9', false, null],
  ['abc', false, null],
  ['1e3', false, null],
  // 前后空白会被 trim（前后端同一口径）-> 合法
  [' 12 ', true, 12],
];
const out = [];
for (const [raw, wantOk, wantSeed] of cases) {
  const r = G.validate(raw);
  out.push({
    raw: String(raw),
    ok: r.ok,
    seed: r.seed,
    error: r.error,
    pass: r.ok === wantOk && r.seed === wantSeed && (r.ok || r.error.length > 0),
  });
}
console.log(JSON.stringify(out));
"""
        result = _run_node(script.replace(
            "process.argv[2]",
            json.dumps(str(REPO / "web" / "seed-guard.js"))))
        for row in result:
            self.assertTrue(row["pass"],
                            f"种子 {row['raw']!r} 的判定不符合预期：{row}")
            if not row["ok"]:
                self.assertIn("种子", row["error"],
                              "错误原因必须用中文且提到「种子」")


if __name__ == "__main__":
    unittest.main()
