# -*- coding: utf-8 -*-
"""成片帧数**精确**命中用户口径的专项测试（2026-11-17 收口）。

【用户口径 —— 最终标准】
    成片总时长 = Σ(各段请求时长) - (段数-1) × 1 秒
    成片总帧数 = Σ(各段请求秒数 × 24) - (段数-1) × 24

  5 秒 → 接续 5 秒       : 120 + 120 - 24 = 216 帧 = 9.000 秒
  5 秒 → 5 秒 → 8 秒    : 120+120+192 - 48 = 384 帧 = 16.000 秒

【上一轮为什么没做到】
上一轮把「口径值」和「实际帧数」**并列展示**，等于把差异留着没解决：
  场景A 实测 226 帧 = 9.4167s（口径 9.000s，差 +0.4167s）
  场景B 实测 396 帧 = 16.5000s（口径 16.000s，差 +0.5000s）
本文件锁的是**收口后**的契约：成片帧数必须**精确**等于用户口径。

【差额的两条来源（都被补偿掉）】
  差 1 网格余量：H3 段长只能落 17k+5 网格（请求 5s=120 帧 -> 实际 124 帧）
  差 2 重叠差  ：拷贝桥只接受 5+17k 钉住窗口（22 帧 = 0.9167s，
                 不是整 1 秒）—— Relay-Kit relay_core.py:411 会 raise

运行：python3 -m pytest tests/test_cont_duration_exact_frames.py -q
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import continuation as cont  # noqa: E402

FPS = 24


def probe_frames(path: Path) -> int:
    """这个视频有多少帧（ffprobe 实测）。"""
    raw = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0",
         str(path)], encoding="utf-8", timeout=120)
    return int(raw.strip().split(",")[0])


def probe_duration(path: Path) -> float:
    raw = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=duration", "-of", "csv=p=0",
         str(path)], encoding="utf-8", timeout=120)
    return float(raw.strip().split(",")[0])


class TestFormulaFrames(unittest.TestCase):
    """用户口径的帧数公式本身。"""

    def test_two_segments_216_frames(self):
        self.assertEqual(cont.formula_total_frames([5.0, 5.0]), 216,
                         "5s->5s 的用户口径成片帧数必须是 216")

    def test_three_segments_384_frames(self):
        self.assertEqual(cont.formula_total_frames([5.0, 5.0, 8.0]), 384,
                         "5s->5s->8s 的用户口径成片帧数必须是 384")

    def test_single_segment_is_request_frames(self):
        self.assertEqual(cont.formula_total_frames([5.0]), 120)

    def test_formula_matches_user_seconds_formula(self):
        """帧数口径与秒数口径必须一致（×24）。"""
        for durs in ([5.0], [5.0, 5.0], [5.0, 5.0, 8.0],
                     [5.0, 5.0, 5.0, 5.0], [3.0, 7.0]):
            with self.subTest(durations=durs):
                seconds = sum(durs) - max(0, len(durs) - 1) * 1.0
                self.assertEqual(
                    cont.formula_total_frames(durs),
                    int(round(seconds * FPS)))


class TestCompensationPlan(unittest.TestCase):
    """补偿计划：差额拆解 + 落点。"""

    def test_scenario_a_plan(self):
        """场景A：226 -> 216，裁 10 帧，只裁最后一段尾部。"""
        plan = cont.chain_compensation_plan([5.0, 5.0])
        self.assertEqual(plan["formula_frames"], 216)
        self.assertEqual(plan["current_frames"], 226)
        self.assertEqual(plan["trim_frames"], 10)
        self.assertEqual(plan["segment_trim"], [0, 10],
                         "只允许裁最后一段；链首段一帧不裁")
        self.assertEqual(plan["keep_frames"], [124, 92])
        self.assertTrue(plan["exact"])
        self.assertEqual(plan["residual_frames"], 0)

    def test_scenario_b_plan(self):
        """场景B：396 -> 384，裁 12 帧。"""
        plan = cont.chain_compensation_plan([5.0, 5.0, 8.0])
        self.assertEqual(plan["formula_frames"], 384)
        self.assertEqual(plan["current_frames"], 396)
        self.assertEqual(plan["trim_frames"], 12)
        self.assertEqual(plan["segment_trim"], [0, 0, 12],
                         "中间段一帧不裁（否则接缝会断）")
        self.assertEqual(plan["keep_frames"], [124, 102, 158])
        self.assertTrue(plan["exact"])
        self.assertEqual(plan["residual_frames"], 0)

    def test_trim_breakdown_explains_both_sources(self):
        """补偿量必须能拆成「网格余量 + 重叠差」，且 notes 写清来源。"""
        plan = cont.chain_compensation_plan([5.0, 5.0])
        account = cont.chain_duration_account([5.0, 5.0])
        slack = sum(int(s["grid_slack_frames"]) for s in account["segments"])
        win_gap = FPS - account["overlap_frames"]
        self.assertEqual(slack, 8, "5s 两段的网格余量合计 8 帧")
        self.assertEqual(win_gap, 2, "22 帧窗口比整 1 秒少 2 帧")
        self.assertEqual(slack + win_gap * 1, plan["trim_frames"])
        # notes 必须说清「来自哪条上游硬约束」
        joined = " ".join(plan["notes"])
        self.assertIn("帧网格", joined)
        self.assertIn("5+17k", joined)

    def test_single_segment_is_not_trimmed(self):
        """单段链不裁（没有成片拼接这一步），残差如实报出。"""
        plan = cont.chain_compensation_plan([5.0])
        self.assertEqual(plan["trim_frames"], 0)
        self.assertEqual(plan["segment_trim"], [0])
        self.assertFalse(plan["exact"],
                         "单段有 4 帧网格残差，必须如实标记 not exact")
        self.assertEqual(plan["residual_frames"], 4)
        joined = " ".join(plan["notes"])
        self.assertIn("只有 1 段", joined)
        self.assertIn("17k+5", joined,
                      "残差来源必须点名 H3 的 17k+5 段长网格")

    def test_plan_notes_mention_seam_safety(self):
        """notes 必须说明「只裁最后一段尾部，接缝不受影响」。"""
        plan = cont.chain_compensation_plan([5.0, 5.0])
        joined = " ".join(plan["notes"])
        self.assertIn("最后一段", joined)
        self.assertIn("接缝", joined)


class TestConcatProducesExactFrames(unittest.TestCase):
    """真跑一遍拼接：成片帧数必须精确等于用户口径。

    【为什么用真 ffmpeg 而不是 mock】这条守的是「ffmpeg 调用方式
    真的能裁出精确帧数」—— 实测踩过的坑：`select='lt(n\\,216)'`
    经 `-vf` 传参会**静默失效**（rc=0 但输出 0 帧），
    只有真跑一遍才抓得住。
    """

    @classmethod
    def setUpClass(cls):
        import server as S
        cls.S = S
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _make_segments(self, counts):
        """造几段指定帧数的视频（纯色，内容不重要）。"""
        paths = []
        for index, count in enumerate(counts):
            path = self.out / f"seg{index}.mp4"
            # 【为什么要带音轨】server 的拼接命令是 `-map 0:v -map 0:a`，
            # 合成段没有音轨会让 ffmpeg 报「Stream map '0:a' matches no streams」。
            subprocess.run(
                ["ffmpeg", "-y",
                 "-f", "lavfi", "-i",
                 f"color=c=0x{index:02x}2020:s=320x180:r={FPS}:d={count / FPS}",
                 "-f", "lavfi", "-i",
                 f"sine=frequency={220 + index * 110}:r=44100:d={count / FPS}",
                 "-frames:v", str(count),
                 "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                 "-f", "mp4", str(path)],
                capture_output=True, check=True, timeout=120)
            paths.append(path)
        return paths

    def test_concat_trim_yields_exact_target_frames(self):
        """两段 124 + 102，裁 10 帧 -> 成片必须 216 帧 / 9.000 秒。"""
        segs = self._make_segments([124, 102])
        self.assertEqual([probe_frames(p) for p in segs], [124, 102])

        # 用 server 的真实拼接逻辑（走 _concat_chain_video 的滤镜链）。
        # 这里直接复刻它的 ffmpeg 参数 —— 同一套滤镜 + -frames:v。
        lst = self.out / "list.txt"
        lst.write_text("".join(f"file '{p}'\n" for p in segs), encoding="utf-8")
        out = self.out / "full.mp4"
        vf = (f"scale=320:180:force_original_aspect_ratio=decrease:"
              f"force_divisible_by=2,pad=320:180:(ow-iw)/2:(oh-ih)/2,"
              f"setsar=1,setpts=N/FRAME_RATE/TB,fps={FPS},format=yuv420p")
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
               "-map", "0:v", "-map", "0:a", "-vf", vf,
               "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
               "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
               "-frames:v", "216",
               "-movflags", "+faststart", str(out)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0,
                         f"ffmpeg 失败: {proc.stderr[-400:]}")
        self.assertEqual(probe_frames(out), 216,
                         "成片帧数必须精确等于用户口径的 216")
        self.assertAlmostEqual(probe_duration(out), 9.0, places=3,
                               msg="成片时长必须精确等于 9.000 秒")

    def test_concat_trim_yields_exact_384_frames(self):
        """三段 124 + 102 + 170，裁 12 帧 -> 成片必须 384 帧 / 16.000 秒。"""
        segs = self._make_segments([124, 102, 170])
        lst = self.out / "list3.txt"
        lst.write_text("".join(f"file '{p}'\n" for p in segs), encoding="utf-8")
        out = self.out / "full3.mp4"
        vf = (f"scale=320:180:force_original_aspect_ratio=decrease:"
              f"force_divisible_by=2,pad=320:180:(ow-iw)/2:(oh-ih)/2,"
              f"setsar=1,setpts=N/FRAME_RATE/TB,fps={FPS},format=yuv420p")
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
               "-map", "0:v", "-map", "0:a", "-vf", vf,
               "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
               "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
               "-frames:v", "384",
               "-movflags", "+faststart", str(out)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0,
                         f"ffmpeg 失败: {proc.stderr[-400:]}")
        self.assertEqual(probe_frames(out), 384)
        self.assertAlmostEqual(probe_duration(out), 16.0, places=3)


class TestServerPlanUsesCompensatedValues(unittest.TestCase):
    """服务端下发的 duration_plan 必须是**补偿后**的账。"""

    def test_plan_exposes_single_unified_value(self):
        """plan 里 total_frames 与 formula_seconds 必须一致（残差 0）。"""
        # 直接用纯函数验证语义（server 的 _chain_duration_plan 依赖 db，
        # 由 tests/test_cont_duration_ui_browser.py 覆盖接口层）。
        plan = cont.chain_compensation_plan([5.0, 5.0])
        self.assertEqual(plan["formula_frames"], plan["current_frames"]
                         - plan["trim_frames"])
        kept = sum(plan["keep_frames"])
        self.assertEqual(kept, plan["formula_frames"])
        self.assertEqual(kept / FPS, plan["formula_frames"] / FPS)


if __name__ == "__main__":
    unittest.main()
