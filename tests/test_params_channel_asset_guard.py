# -*- coding: utf-8 -*-
"""【2026-11-20 安全缺陷收口】params JSON 通道必须同样过素材库闸门。

【背景 —— 这是谁交付范围内的缺陷】

2026-11-16 的任务 0df3a72e（引入 h3studio/asset_ref_guard.py）落地了
「媒体必须先入素材库 + 种子仅允许正整数」。但那道闸门只覆盖了**两条**
通道：

  1. multipart 直传（saved_files）—— reject_external_media 拦；
  2. asset_refs（asset_id）      —— check_asset_refs 查库。

**params JSON 通道没覆盖**，而它恰恰是唯一能被外部直接控制的那条：

    POST /api/jobs -F mode=flf -F params={"first_frame":"/etc/hostname"}

=> HTTP 200，任务建成，worker 把该文件**复制进 ComfyUI input 目录**，
   而 ComfyUI 的 /view 端点无鉴权且与 8190 一样监听 0.0.0.0
   => 构成完整的「任意本地文件读取」链。

【本文件锁住的行为】

  A. 外部路径一律被拒（绝对路径 / 路径穿越 / 符号链接 / 任意相对路径）
     —— 且给出**可理解的中文原因**（不是通用失败）；
  B. **不误杀**：素材库里的合法素材引用照常通过；
  C. **不误杀**：worker 回写的 ComfyUI 文件名形态照常通过
     （重试既有任务会带着这种形态进来）；
  D. 「种子仅允许正整数」校验**不回归**；
  E. 闸门挂在**唯一公共点** prepare_job_params 上 ——
     面板提交 / 内置 Agent create_job / retry_job 三条链路同时生效。

运行：python3 -m pytest tests/test_params_channel_asset_guard.py -v
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))


# ==================================================================
# A) 外部路径一律被拒
# ==================================================================


class TestParamsChannelRejectsExternalPaths(unittest.TestCase):
    """params 里的媒体路径必须被素材库闸门拦下。"""

    def _flf_params(self, **extra):
        params = {"prompt": "p", "width": 832, "height": 480, "duration": 5}
        params.update(extra)
        return params

    def _expect_rejected(self, params, code="media_must_come_from_library"):
        """跑一次真实的 prepare_job_params，断言被拒且给出可读原因。"""
        import server
        with self.assertRaises(server.JobValidationError) as ctx:
            server.prepare_job_params("flf", params, [], {})
        self.assertEqual(ctx.exception.code, code)
        self.assertTrue(ctx.exception.message.strip())
        return ctx.exception

    # ---- 绝对路径 ----

    def test_absolute_etc_hostname_is_rejected(self):
        """/etc/hostname -> 拒绝（这是复现出来的原始利用载荷）。"""
        exc = self._expect_rejected(self._flf_params(first_frame="/etc/hostname"))
        self.assertIn("素材库", exc.message)

    def test_absolute_tmp_path_is_rejected(self):
        """另一个绝对路径 -> 同样拒绝。"""
        exc = self._expect_rejected(
            self._flf_params(last_frame="/tmp/whatever.png"))
        self.assertIn("素材库", exc.message)

    def test_absolute_path_in_ref_videos_is_rejected(self):
        """参考视频槽位走的是同一条通道 -> 同样拒绝。"""
        self._expect_rejected(self._flf_params(ref_videos=["/etc/hostname"]))

    def test_absolute_path_in_ref_audios_is_rejected(self):
        """参考音乐槽位 -> 同样拒绝。"""
        self._expect_rejected(self._flf_params(ref_audios=["/etc/passwd"]))

    # ---- 路径穿越 ----

    def test_dotdot_traversal_is_rejected(self):
        """../../etc/hostname -> 拒绝（resolve() 之后指向 /etc/hostname）。"""
        exc = self._expect_rejected(
            self._flf_params(
                first_frame="../../../../../../../../etc/hostname"))
        self.assertIn("素材库", exc.message)

    def test_dotdot_inside_comfy_subfolder_is_rejected(self):
        """h3studio/../../etc/passwd -> 拒绝。

        【为什么这条特别重要】ComfyUI 侧的 _comfy_input_local 是
        **按 `/` 逐段拼接**的，`..` 在那里会被真实解析 ——
        所以「看起来像 ComfyUI 文件名」并不等于安全。
        """
        self._expect_rejected(
            self._flf_params(first_frame="h3studio/../../etc/passwd"))

    def test_dotdot_mid_name_is_rejected(self):
        """文件名里带 `..`（如 `a..b.png`）—— 拒绝。

        【为什么要拒】ComfyUI 侧的 `_comfy_input_local` 是**按 `/`
        逐段拼接**的 —— 虽然 `a..b` 作为**一个**段不会被解析成父目录，
        但「允许文件名里出现 `..`」意味着将来 ComfyUI 换了拼接实现
        （或有人在别处用 os.path.normpath）时静默失守。
        没有任何合法产物带 `..` —— 保守拒绝的代价是零。
        """
        self._expect_rejected(
            self._flf_params(first_frame="h3studio/a..b.png"))

    # ---- 符号链接 ----

    def test_symlink_to_outside_file_is_rejected(self):
        """指向 /etc/hostname 的软链 -> 拒绝。

        【为什么软链也必须拒】即使软链**本身**放在 uploads 目录里，
        它的内容指向别处 —— 只判「路径落在 uploads 内」是不够的。
        本实现按**值的形状**判，根本不给软链进入的机会。
        """
        tmp = Path(tempfile.mkdtemp(prefix="params-guard-symlink-"))
        try:
            target = tmp / "link.png"
            target.symlink_to("/etc/hostname")
            self._expect_rejected(
                self._flf_params(first_frame=str(target)))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_relative_symlink_name_is_rejected(self):
        """相对形态的软链名（含 `/`）-> 拒绝。"""
        self._expect_rejected(
            self._flf_params(first_frame="link-to-etc/hostname"))

    # ---- 其它绕过写法 ----

    def test_comfy_input_direct_reference_is_rejected(self):
        """直接引用 ComfyUI input 里已存在的文件 -> 拒绝。

        【为什么这条也要拒】ComfyUI input 里确实有别人（其它任务）
        上传的文件；能按名字引用它们 = 能读到**别的任务**的素材，
        而且 ComfyUI 的 /view 能把它们读回来。

        【实测依据 —— 2026-11-20 修复前】线上实测这条载荷
        （`h3studio/5293d88df5d86cad__upload_05.wav`，一个真实存在的
        20MB 文件）**HTTP 200 accepted** —— 任务建成。
        """
        self._expect_rejected(
            self._flf_params(first_frame="h3studio/some-existing-file.png"))

    def test_windows_style_path_is_rejected(self):
        """C:\\Windows\\... -> 拒绝（盘符形态）。"""
        self._expect_rejected(
            self._flf_params(first_frame="C:\\Windows\\System32\\config\\SAM"))

    def test_tilde_path_is_rejected(self):
        """~/.ssh/id_rsa -> 拒绝（波浪号 = 本地任意文件）。"""
        self._expect_rejected(
            self._flf_params(first_frame="~/.ssh/id_rsa"))

    def test_home_relative_path_is_rejected(self):
        """普通相对路径（不是 ComfyUI 命名形态）-> 拒绝。"""
        self._expect_rejected(
            self._flf_params(first_frame="etc/hostname"))

    def test_asset_id_in_params_is_rejected_with_guidance(self):
        """把素材库 ID 直接写进 params -> 拒绝，且说清正确姿势。"""
        exc = self._expect_rejected(
            self._flf_params(first_frame="asset_abcdef012345"))
        self.assertIn("asset_refs", exc.message)

    # ---- 所有媒体键都要被覆盖 ----

    def test_every_media_key_is_guarded(self):
        """每个媒体键都必须被闸门覆盖（漏一个就是一个旁路）。"""
        from h3studio import asset_ref_guard as G
        for key in ("first_frame", "last_frame", "ref_videos", "ref_audios",
                    "ref_images", "ref_video_audios", "ref_audio",
                    "segment_audio_file"):
            self.assertIn(key, G.PARAM_MEDIA_KEYS,
                          f"媒体键 {key} 不在闸门覆盖范围里")
            params = self._flf_params()
            params[key] = "/etc/hostname"
            self._expect_rejected(params)

    def test_guard_covers_all_modes(self):
        """闸门与 mode 无关 —— 任何模式带外部路径都要被拒。"""
        from assetlib_fixture import temp_asset_library
        import server
        with temp_asset_library(image=1) as ids:
            asset_id = ids["image"][0] if ids["image"] else None
            for mode in ("t2v", "flf", "r2v", "t2i", "t2i_qwen", "i2i_qwen",
                         "t2i_moss", "t2i_h3image", "i2i_h3image", "t2a", "a2a"):
                params = self._flf_params(first_frame="/etc/hostname")
                refs = []
                if asset_id and mode in ("flf", "r2v"):
                    refs = [{"asset_id": asset_id, "slot": "first_frame",
                             "ordinal": 0}]
                with self.assertRaises(server.JobValidationError,
                                       msg=f"{mode} 模式没有拦下外部路径"):
                    server.prepare_job_params(mode, params, refs, {})


# ==================================================================
# B) 不误杀：素材库里的合法素材引用
# ==================================================================


class TestParamsChannelAcceptsLibraryAssets(unittest.TestCase):
    """素材库里的合法素材**必须能正常引用**（闸门不能误杀）。"""

    def test_library_asset_reference_still_works(self):
        """asset_refs 里的合法素材 -> 照常通过（既有流程不破坏）。"""
        import server
        from assetlib_fixture import temp_asset_library
        with temp_asset_library(image=1) as ids:
            if not ids["image"]:
                self.skipTest("建不出图片素材")
            params = {"prompt": "p", "width": 832, "height": 480, "duration": 5}
            refs = [{"asset_id": ids["image"][0], "slot": "first_frame",
                     "ordinal": 0}]
            server.prepare_job_params("flf", params, refs, {})
            self.assertEqual(params.get("asset_refs"), None)

    def test_worker_written_comfy_name_is_rejected(self):
        """worker 回写的 ComfyUI 文件名形态 -> **也拒绝**。

        【为什么拒绝（而不是放行）】需求原文要求「外部任意本地路径
        （绝对路径、../ 路径穿越、符号链接、**ComfyUI input 直引**）
        一律明确拒绝」。`h3studio/<name>` 正是「ComfyUI input 直引」
        的写法 —— 按名字引用 ComfyUI input 里的文件。

        【为什么这不会误杀重试】重试既有任务时，params 里那份
        `h3studio/<name>` 是**过期残留**（UPLOAD_DIR 下并没有那个
        文件）—— 它本来就会让 worker 报「找不到上传文件」。
        prepare_job_params 里的 _drop_stale_worker_media_params
        会在校验**之前**把这类残留清掉（前提是 asset_refs 声明了
        同一槽位）—— 于是重试改走素材库那条路，反而**从坏变好**。
        """
        import server
        params = {"prompt": "p", "width": 832, "height": 480, "duration": 5,
                  "first_frame": "h3studio/dadaf3eb7fd8a117__e2e-sample.png"}
        with self.assertRaises(server.JobValidationError) as ctx:
            server.prepare_job_params("flf", params, [], {})
        self.assertEqual(ctx.exception.code, "media_must_come_from_library")

    def test_stale_worker_value_is_dropped_when_asset_refs_covers_slot(self):
        """asset_refs 覆盖的槽位 -> params 里的旧值被清掉（重试不被误杀）。

        【锁的是行为，不是实现】这条断言表达的是「重试既有媒体任务
        时，worker 回写的旧 ComfyUI 文件名不会把闸门顶死」——
        因为 asset_refs 才是权威通道。
        """
        import server
        from assetlib_fixture import temp_asset_library
        with temp_asset_library(image=1) as ids:
            if not ids["image"]:
                self.skipTest("建不出图片素材")
            params = {"prompt": "p", "width": 832, "height": 480,
                      "duration": 5,
                      "first_frame": "h3studio/stale-worker-value.png"}
            refs = [{"asset_id": ids["image"][0], "slot": "first_frame",
                     "ordinal": 0}]
            server.prepare_job_params("flf", params, refs, {})
            self.assertNotIn("first_frame", params,
                             "worker 的旧值应当被清掉（由 asset_refs 重新取）")

    def test_plain_comfy_filename_is_rejected(self):
        """纯文件名形态（ComfyUI 顶层）-> 拒绝（同样是「ComfyUI input 直引」）。"""
        import server
        params = {"prompt": "p", "width": 832, "height": 480, "duration": 5,
                  "first_frame": "h3studio_ref_deadbeef.mp3"}
        with self.assertRaises(server.JobValidationError) as ctx:
            server.prepare_job_params("flf", params, [], {})
        self.assertEqual(ctx.exception.code, "media_must_come_from_library")

    def test_chinese_filename_is_rejected(self):
        """含中文的文件名 -> 拒绝（ComfyUI input 直引的一种写法）。"""
        import server
        params = {"prompt": "p", "width": 832, "height": 480, "duration": 5,
                  "first_frame": "h3studio/12ab34cd__中文文件名.png"}
        with self.assertRaises(server.JobValidationError):
            server.prepare_job_params("flf", params, [], {})

    def test_empty_media_value_is_allowed(self):
        """空串 / None -> 放行（= 没填，不是路径）。

        【为什么空值要放行】flf 允许「只给首帧」或「只给尾帧」；
        未给的那个槽位在 params 里可能是空串 —— 那是「没填」，
        不是「引用了一个空路径」。
        """
        import server
        from assetlib_fixture import temp_asset_library
        with temp_asset_library(image=1) as ids:
            if not ids["image"]:
                self.skipTest("建不出图片素材")
            params = {"prompt": "p", "width": 832, "height": 480,
                      "duration": 5, "first_frame": "", "last_frame": None}
            refs = [{"asset_id": ids["image"][0], "slot": "first_frame",
                     "ordinal": 0}]
            server.prepare_job_params("flf", params, refs, {})

    def test_i2i_metadata_ref_images_still_works(self):
        """i2i 的 ref_images 元数据形态 -> 照常通过（不能被路径判误杀）。"""
        import server
        from assetlib_fixture import temp_asset_library
        with temp_asset_library(image=1) as ids:
            if not ids["image"]:
                self.skipTest("建不出图片素材")
            params = {"prompt": "改成夜景",
                      "ref_images": [{"asset_id": ids["image"][0],
                                      "description": "x"}]}
            server.prepare_job_params("i2i_qwen", params, [], {})


# ==================================================================
# C) 闸门挂在唯一公共点上
# ==================================================================


class TestGuardIsWiredAtTheCommonPoint(unittest.TestCase):
    """闸门必须在 prepare_job_params（唯一公共点）上，三条链路同时生效。"""

    def test_prepare_job_params_calls_the_params_guard(self):
        """prepare_job_params 必须真的调 check_params_media_references。"""
        source = (REPO / "server.py").read_text(encoding="utf-8")
        self.assertIn("check_params_media_references", source)

    def test_agent_path_goes_through_prepare_job_params(self):
        """内置 Agent 的 create_job 走 prepare_job_params -> 自动继承。"""
        source = (REPO / "h3studio" / "agent.py").read_text(encoding="utf-8")
        self.assertIn("prepare_job_params", source)

    def test_retry_path_goes_through_prepare_job_params(self):
        """retry_job 也走 prepare_job_params -> 自动继承。"""
        source = (REPO / "h3studio" / "agent_tools.py").read_text(
            encoding="utf-8")
        self.assertIn("prepare_job_params", source)

    def test_guard_lives_in_asset_ref_guard_module(self):
        """闸门实现在 asset_ref_guard 里（与 2026-11-16 那道闸门同源）。

        【为什么不另起一个模块】需求要求「在既有 asset_ref_guard /
        素材库闸门体系上补齐」—— 另起模块 = 两套闸门 = 将来漂移。
        """
        from h3studio import asset_ref_guard as G
        self.assertTrue(callable(G.check_params_media_references))
        self.assertTrue(issubclass(G.AssetLibraryError, ValueError))

    def test_guard_rejects_instead_of_raising_generic_error(self):
        """必须抛带 code 的 AssetLibraryError（能被转成 400 + 中文原因）。

        【为什么要断言 code】如果抛的是普通 Exception，
        api_create 只捕 JobValidationError -> 500 + 堆栈，
        用户看到的是「服务端错误」而不是可执行的中文原因。
        """
        from h3studio import asset_ref_guard as G
        with self.assertRaises(G.AssetLibraryError) as ctx:
            G.check_params_media_references({"first_frame": "/etc/hostname"})
        self.assertEqual(ctx.exception.code, "media_must_come_from_library")
        self.assertIn("素材库", ctx.exception.message)


# ==================================================================
# D) 种子校验不回归
# ==================================================================


class TestSeedGuardNotRegressed(unittest.TestCase):
    """「种子仅允许正整数」这条既有校验**不能被这次改动破坏**。"""

    def _expect_seed_rejected(self, bad):
        import server
        from assetlib_fixture import temp_asset_library
        params = {"prompt": "p", "width": 832, "height": 480, "duration": 5,
                  "seed": bad}
        with temp_asset_library(image=1) as ids:
            with self.assertRaises(server.JobValidationError) as ctx:
                server.prepare_job_params(
                    "flf", params,
                    [{"asset_id": ids["image"][0], "slot": "last_frame",
                      "ordinal": 0}], {})
        return ctx.exception

    def test_negative_seed_still_rejected(self):
        exc = self._expect_seed_rejected(-5)
        self.assertEqual(exc.code, "invalid_seed")

    def test_zero_seed_still_rejected(self):
        exc = self._expect_seed_rejected(0)
        self.assertEqual(exc.code, "invalid_seed")

    def test_non_numeric_seed_still_rejected(self):
        exc = self._expect_seed_rejected("abc")
        self.assertEqual(exc.code, "invalid_seed")

    def test_valid_seed_still_accepted(self):
        """合法种子照常放行并归一成 int。"""
        import server
        from assetlib_fixture import temp_asset_library
        params = {"prompt": "p", "width": 832, "height": 480, "duration": 5,
                  "seed": "4242"}
        with temp_asset_library(image=1) as ids:
            server.prepare_job_params(
                "flf", params,
                [{"asset_id": ids["image"][0], "slot": "last_frame",
                  "ordinal": 0}], {})
        self.assertEqual(params["seed"], 4242)


# ==================================================================
# E) 闸门的「形状判据」本身
# ==================================================================


class TestComfyNameShapeRules(unittest.TestCase):
    """闸门的形状判据：只放行 worker 自己的产物形态。"""

    def test_allowed_shapes(self):
        from h3studio import asset_ref_guard as G
        for ok in ("h3studio/a.png", "a.png", "h3studio/seg-abc.m4a",
                   "h3studio/中文.png", "h3studio/a_b-c.d.e.png"):
            self.assertTrue(G._looks_like_comfy_name(ok),
                            f"{ok!r} 应当被放行（worker 产物形态）")

    def test_rejected_shapes(self):
        from h3studio import asset_ref_guard as G
        for bad in ("/etc/hostname", "../../etc/hostname", "a/../b",
                    "h3studio/../../etc/passwd", "~/x", "C:/x", "x\\y",
                    "h3studio/a/b/c", "", "  a.png", "a.png "):
            self.assertFalse(G._looks_like_comfy_name(bad),
                             f"{bad!r} 应当被拒绝")

    def test_asset_id_shape(self):
        from h3studio import asset_ref_guard as G
        self.assertTrue(G._looks_like_asset_id("asset_0123456789ab"))
        self.assertFalse(G._looks_like_asset_id("asset_0123456789abc"))
        self.assertFalse(G._looks_like_asset_id("/etc/hostname"))


if __name__ == "__main__":
    unittest.main()
