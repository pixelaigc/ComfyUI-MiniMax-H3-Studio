# -*- coding: utf-8 -*-
"""接续拼接的**帧数守恒**回归测试（2026-11-17 接续点闪烁缺陷）。

【这个测试在防什么】
用户实测反馈：接续点（约 4–5 秒处）有一次**明显闪烁**。
取证后定位到**两个独立缺陷**：

  缺陷 1（拼接层，影响**所有**接续链）：concat 的 `fps=24` 滤镜
    会让成片比「各段帧数之和」**多出 1 帧** —— 那多出来的一帧正好
    落在接续点上，表现为「闪一下」。
    根因：分段视频的**首帧 PTS=0 但 duration 不是 1/24**
    （实测段1 首帧 duration=0.083333、段2=0.041667），
    concat 后各段时间戳撞车，`fps=24` 按输出时间轴重采样时
    从第一个输出时刻（实测成片首帧 PTS=0.041016，不是 0）开始铺帧
    → 每个分段边界都复制一帧。
    修法：滤镜链加 `setpts=N/FRAME_RATE/TB`（按**帧序号**重排时间戳）。

  缺陷 2（二采特有）：SelfLift 的低分辨率段把钉住区内容**降采样**后
    当锚点，抬升再回高分辨率 -> 新生成区继承了低分辨率段的色档
    → 接续点色档跳变（二采 ON 时是段内量级的 29.52x，
    同参数二采 OFF 时只有 5.48x）。

【为什么旧测试没抓到】
`tests/test_continuation_server.py::test_concat_produces_full_length_video`
断言的是「116 ≤ frames ≤ 124」（**±4 帧容差**）——
多出 1 帧完全落在容差里，绿了但没守住。
本测试用**严格相等**（帧数守恒）把它钉死。

【断言纪律】
  * 期望值是**字面量**（段帧数之和），不是从被测函数输出反推。
  * 断言要由「命名该行为的那条断言」抓住。

运行：python3 -m pytest tests/test_cont_concat_frames.py -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _ffmpeg_bin():
    return shutil.which("ffmpeg")


def _ffprobe_bin():
    return shutil.which("ffprobe")


def _make_video(path: Path, seconds: float, width=768, height=448,
                fps=24) -> bool:
    """造一个指定时长的测试视频（ffmpeg 的 testsrc）。

    【关键：必须复现线上产物的 B 帧结构】——2026-11-17 实测：
      线上真实分段视频 `has_b_frames=2` → **首帧 duration=0.083333**
      （不是 1/24），后面的帧才是 0.041667；
      而 `testsrc` 默认 **has_b_frames=0** → 所有帧 duration 都是 0.041667。
      **正是这个差异让缺陷 1 在测试里复现不出来**（B 帧的可变首帧时长
      才是 concat 后时间戳撞车、fps 滤镜多铺一帧的真正来源）。
      → 这里显式开 B 帧（`-bf 2`），让夹具与生产一致。
    """
    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        return False
    cmd = [ffmpeg, "-y", "-f", "lavfi", "-i",
           f"testsrc=size={width}x{height}:rate={fps}:duration={seconds}",
           "-f", "lavfi", "-i",
           f"sine=frequency=440:duration={seconds}",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           # B 帧：与线上产物一致（has_b_frames=2 -> 首帧 duration 变长）
           "-bf", "2",
           "-c:a", "aac", "-shortest", str(path)]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=120, check=True)
    except Exception:
        return False
    return path.is_file()


def _video_frames(path: Path) -> int:
    """数视频帧数（用 count_frames，比 nb_frames 可靠）。"""
    ffprobe = _ffprobe_bin()
    if not ffprobe:
        return -1
    raw = subprocess.check_output(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
        text=True).strip()
    try:
        return int(raw)
    except ValueError:
        return -1


def _first_frame_pts(path: Path) -> float:
    """首帧的 PTS（秒）。"""
    ffprobe = _ffprobe_bin()
    if not ffprobe:
        return -1.0
    raw = subprocess.check_output(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "frame=pts_time", "-of", "csv=p=0", "-read_intervals", "%+#1",
         str(path)], text=True).strip().split(",")[0]
    try:
        return float(raw)
    except ValueError:
        return -1.0


def _decode_gray(path: Path):
    """解码成灰度帧序列（numpy），用于帧级比对。"""
    import numpy as np
    ffprobe = _ffprobe_bin()
    ffmpeg = _ffmpeg_bin()
    if not (ffprobe and ffmpeg):
        return None
    raw = subprocess.check_output(
        [ffmpeg, "-v", "error", "-i", str(path), "-f", "rawvideo",
         "-pix_fmt", "gray", "-"], timeout=180)
    size = subprocess.check_output(
        [ffprobe, "-v", "error", "-show_entries", "stream=width,height",
         "-of", "csv=p=0", str(path)], text=True).strip()
    w, h = [int(x) for x in size.split(",")[:2]]
    a = np.frombuffer(raw, dtype=np.uint8)
    n = len(a) // (w * h)
    if n == 0:
        return None
    return a[:n * w * h].reshape(n, h, w).astype(np.float32)


class TestConcatFrameConservation(unittest.TestCase):
    """拼接必须**帧数守恒**：成片帧数 == 各段帧数之和。"""

    @classmethod
    def setUpClass(cls):
        if not (_ffmpeg_bin() and _ffprobe_bin()):
            raise unittest.SkipTest("本机没有 ffmpeg / ffprobe")
        cls.tmp = Path(tempfile.mkdtemp(prefix="h3-concat-frames-"))
        # 把 server 的目录指到临时目录（不碰真实库）
        import server as srv
        cls.srv = srv
        cls._orig = {}
        for name in ("DATA", "OUT_DIR", "VIDEO_DIR", "UPLOAD_DIR"):
            cls._orig[name] = getattr(srv, name)
        srv.DATA = cls.tmp
        srv.OUT_DIR = cls.tmp / "outputs"
        srv.VIDEO_DIR = srv.OUT_DIR / "videos"
        srv.UPLOAD_DIR = cls.tmp / "uploads"
        for d in (srv.OUT_DIR, srv.VIDEO_DIR, srv.UPLOAD_DIR):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        for name, value in cls._orig.items():
            setattr(cls.srv, name, value)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _concat(self, names, chain_id):
        return self.srv._concat_chain_video(names, chain_id)

    # ------------------------------------------------------------------
    # 缺陷 1：fps=24 滤镜导致边界帧重复
    # ------------------------------------------------------------------
    def test_concat_frame_count_equals_sum_of_segments(self):
        """成片帧数必须**精确等于**各段帧数之和（不多一帧）。

        这是接续点闪烁的**直接成因**：多出来的那一帧正好落在接续点上。
        线上实测：段1 124 + 段2 102 = 226，成片却是 227。
        """
        seg1 = self.srv.VIDEO_DIR / "fc_seg1.mp4"
        seg2 = self.srv.VIDEO_DIR / "fc_seg2.mp4"
        self.assertTrue(_make_video(seg1, 3.0), "造测试视频失败")
        self.assertTrue(_make_video(seg2, 2.0), "造测试视频失败")
        n1 = _video_frames(seg1)
        n2 = _video_frames(seg2)
        self.assertGreater(n1, 0)
        self.assertGreater(n2, 0)

        out = self._concat(["videos/fc_seg1.mp4", "videos/fc_seg2.mp4"],
                           "fcount01")
        self.assertIsNotNone(out, "拼接应成功")
        total = _video_frames(out)
        self.assertEqual(total, n1 + n2,
                         f"成片帧数必须守恒：各段 {n1}+{n2}={n1+n2}，"
                         f"实际 {total}（多/少的帧会正好落在接续点上）")

    def test_concat_three_segments_frame_count(self):
        """三段拼接同样必须帧数守恒（每个边界都不能多帧）。"""
        names = []
        counts = []
        for i, seconds in enumerate((2.0, 2.0, 2.0)):
            p = self.srv.VIDEO_DIR / f"fc3_seg{i}.mp4"
            self.assertTrue(_make_video(p, seconds))
            names.append(f"videos/fc3_seg{i}.mp4")
            counts.append(_video_frames(p))
        out = self._concat(names, "fcount02")
        self.assertIsNotNone(out)
        total = _video_frames(out)
        self.assertEqual(total, sum(counts),
                         f"三段拼接帧数必须守恒：{counts} 合计 {sum(counts)}，"
                         f"实际 {total}")

    def test_concat_first_frame_pts_starts_at_zero(self):
        """成片首帧 PTS 必须是 0。

        旧实现（无 setpts）实测首帧 PTS=0.041016 —— 因为 fps 滤镜
        从「第一个输出时刻」开始铺帧，那一帧是**复制**出来的。
        """
        seg1 = self.srv.VIDEO_DIR / "fc_pts1.mp4"
        seg2 = self.srv.VIDEO_DIR / "fc_pts2.mp4"
        self.assertTrue(_make_video(seg1, 2.0))
        self.assertTrue(_make_video(seg2, 2.0))
        out = self._concat(["videos/fc_pts1.mp4", "videos/fc_pts2.mp4"],
                           "fpts01")
        self.assertIsNotNone(out)
        pts = _first_frame_pts(out)
        self.assertAlmostEqual(pts, 0.0, places=3,
                               msg=f"成片首帧 PTS 应为 0，实际 {pts}")

    def test_concat_boundary_frame_is_not_duplicated(self):
        """分段边界处**不能**出现重复帧。

        判据：成片[k] 应当精确等于段2 的第 0 帧（MAE 极小），
        而不是「段2[0] 与 段2[1] 的混合」或「段2[0] 出现两次」。
        """
        seg1 = self.srv.VIDEO_DIR / "fc_b1.mp4"
        seg2 = self.srv.VIDEO_DIR / "fc_b2.mp4"
        self.assertTrue(_make_video(seg1, 2.0))
        self.assertTrue(_make_video(seg2, 2.0))
        n1 = _video_frames(seg1)
        n2 = _video_frames(seg2)
        out = self._concat(["videos/fc_b1.mp4", "videos/fc_b2.mp4"],
                           "fbnd01")
        self.assertIsNotNone(out)

        import numpy as np
        f_all = _decode_gray(out)
        f_seg2 = _decode_gray(seg2)
        self.assertIsNotNone(f_all)
        self.assertIsNotNone(f_seg2)
        seam = n1  # 段2 第一帧在成片里的下标
        self.assertLess(seam, len(f_all), "成片太短")
        # 成片[seam] 应等于 段2[0]
        mae = float(np.abs(f_all[seam] - f_seg2[0]).mean())
        self.assertLess(mae, 2.0,
                        f"成片[{seam}] 应当精确等于段2[0]（MAE<2），"
                        f"实际 MAE={mae:.2f} —— 边界处多了一帧或错位了")
        # 且成片[seam-1] 应当等于 段1 的最后一帧
        f_seg1 = _decode_gray(seg1)
        mae_prev = float(np.abs(f_all[seam - 1] - f_seg1[-1]).mean())
        self.assertLess(mae_prev, 2.0,
                        f"成片[{seam-1}] 应当精确等于段1 末帧（MAE<2），"
                        f"实际 MAE={mae_prev:.2f}")


class TestConcatFilterChain(unittest.TestCase):
    """滤镜链本身：必须含 setpts（按帧序号重排时间戳）。"""

    def test_concat_filter_contains_setpts(self):
        """拼接滤镜链必须含 setpts=N/FRAME_RATE/TB。

        没有它，`fps=24` 会按原 PTS 重采样 -> 每个分段边界复制一帧。
        """
        src = (REPO / "server.py").read_text(encoding="utf-8")
        idx = src.find("def _concat_chain_video")
        self.assertGreater(idx, 0, "找不到 _concat_chain_video")
        body = src[idx:idx + 9000]
        self.assertIn("setpts=N/FRAME_RATE/TB", body,
                      "拼接滤镜链必须含 setpts=N/FRAME_RATE/TB"
                      "（否则 fps=24 会在分段边界复制一帧）")
        # fps 滤镜仍然保留（统一帧率），但必须排在 setpts 之后
        set_pos = body.find("setpts=N/FRAME_RATE/TB")
        fps_pos = body.find(f"fps={{cont.FPS}}")
        self.assertGreater(set_pos, 0)
        self.assertGreater(fps_pos, set_pos,
                           "setpts 必须在 fps 之前（先重排时间戳再重采样）")


if __name__ == "__main__":
    unittest.main()
