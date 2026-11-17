#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接缝色档差异的**独立测量器**（2026-11-17 二采残缺陷取证）。

【为什么另写一个测量器，而不是复用上一轮的 report.json】
上一轮的数字（19.39 / 29.52x / 10.14 / 15.44x）是当时的测量器算出来的。
本轮任务明确要求「**不沿用历史结论**」—— 历史报告可能有误。
所以这里从零实现一遍度量口径，并把口径写清楚：

【度量口径】
1. 解码全部帧到 RGB uint8（ffmpeg rawvideo），**不做任何缩放/色彩转换**。
2. 接续点帧号 = 段1 帧数（因为段2 的头 N 帧被裁掉）。
   —— 用**帧数守恒**校验：full_frames == seg1_frames + seg2_frames。
3. seam_jump = ||mean_rgb(full[seam]) - mean_rgb(full[seam-1])||_2
   （接续点那一帧 vs 它前一帧的逐通道均值差，L2 范数）
4. in_segment_scale = **段内相邻帧差的稳健尺度**：
   取 full[0:seam] 里相邻帧差的中位数（median），再乘一个常数做归一。
   —— 用中位数而不是均值：均值会被接缝处的异常值污染，
      自指会导致「跳变越大 => 尺度越大 => 比值越小」这种自证循环。
      用中位数（robust）保证尺度只反映**正常段内**的帧间变化。
5. seam_ratio = seam_jump / in_segment_scale
   —— 这个比值就是「接续点的帧间差异是段内正常帧间差异的多少倍」。

【额外统计（本轮新增）】
- 接续点**前后各 8 帧**逐帧的：mean RGB、逐通道亮度、饱和度均值
- 接续点**区域的高频能量**：相邻帧差的高通分量能量（用 Laplacian 近似）
- 逐通道色度（Lab 空间的 a/b 均值）：判断是「亮度阶跃」还是「色相阶跃」
- 段1 末帧 vs 段2 首帧（**跨段**直接比，不经过拼接）—— 这是纯色彩统计

【用法】
python3 seam_metric.py <full.mp4> <seg1.mp4> <seg2.mp4> [--json out.json]
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

FPS = 24


def decode_frames(path: str) -> np.ndarray:
    """解码成 [T, H, W, 3] uint8 RGB。不缩放、不转色（保持原样）。"""
    # 先问 ffprobe 尺寸
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,nb_frames,r_frame_rate",
         "-of", "json", path],
        capture_output=True, text=True, check=True).stdout
    st = json.loads(out)["streams"][0]
    w, h = int(st["width"]), int(st["height"])
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-f", "rawvideo",
         "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True).stdout
    frame_sz = w * h * 3
    n = len(raw) // frame_sz
    if n == 0:
        raise SystemExit(f"解码不到帧: {path}")
    arr = np.frombuffer(raw[: n * frame_sz], dtype=np.uint8)
    return arr.reshape(n, h, w, 3)


def rgb_to_lab_stats(frames: np.ndarray) -> dict:
    """逐帧的亮度/饱和度/色度统计（用 numpy 手算，避免依赖 scipy/skimage）。"""
    f = frames.astype(np.float32)
    mean_rgb = f.reshape(len(f), -1, 3).mean(axis=1)          # [T,3]
    lum = f @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)  # [T,H,W]
    mx = f.max(axis=3)
    mn = f.min(axis=3)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    # 粗略色度：相对亮度的 R-G / B-G 偏差（够用来分辨「亮度阶跃」vs「色相阶跃」）
    chroma_rg = f[..., 0] - f[..., 1]
    chroma_bg = f[..., 2] - f[..., 1]
    return {
        "mean_rgb": mean_rgb,
        "luma_mean": lum.mean(axis=(1, 2)),
        "luma_std": lum.std(axis=(1, 2)),
        "sat_mean": sat.mean(axis=(1, 2)),
        "chroma_rg_mean": chroma_rg.mean(axis=(1, 2)),
        "chroma_bg_mean": chroma_bg.mean(axis=(1, 2)),
    }


def highfreq_energy(frames: np.ndarray) -> np.ndarray:
    """逐帧高频能量（Laplacian 近似）：|f - 4*blur(f)| 的均值。

    用 3x3 均值模糊做低通，残差即高频。用 numpy 的切片实现，无 scipy。
    """
    g = frames.astype(np.float32).mean(axis=3)   # [T,H,W] 灰度
    # 3x3 均值模糊（边界复制）
    p = np.pad(g, ((0, 0), (1, 1), (1, 1)), mode="edge")
    blur = (p[:, :-2, :-2] + p[:, :-2, 1:-1] + p[:, :-2, 2:] +
            p[:, 1:-1, :-2] + p[:, 1:-1, 1:-1] + p[:, 1:-1, 2:] +
            p[:, 2:, :-2] + p[:, 2:, 1:-1] + p[:, 2:, 2:]) / 9.0
    lap = np.abs(g - blur)
    return lap.reshape(len(g), -1).mean(axis=1)


def frame_diffs(frames: np.ndarray, norm: str = "l1") -> np.ndarray:
    """相邻帧的逐通道均值差 -> [T-1]

    norm='l1' : 逐通道绝对差之和（**上一轮历史报告用的口径**）
    norm='l2' : 逐通道差的 L2 范数
    两种口径都给出来 —— 历史数字必须能被复现，否则无法对拍。
    """
    m = frames.astype(np.float32).reshape(len(frames), -1, 3).mean(axis=1)
    d = np.diff(m, axis=0)
    if norm == "l2":
        return np.linalg.norm(d, axis=1)
    return np.abs(d).sum(axis=1)


def analyze(full_p, seg1_p, seg2_p) -> dict:
    full = decode_frames(full_p)
    s1 = decode_frames(seg1_p)
    s2 = decode_frames(seg2_p)

    # ---- 帧数守恒校验 ----
    n_full, n1, n2 = len(full), len(s1), len(s2)
    conserved = (n_full == n1 + n2)

    # ---- 接续点帧号 ----
    # 段2 的头被裁掉了 context 帧，所以接续点 = 段1 帧数。
    # 用「全片里哪一帧是段1 的最后一帧」来交叉验证：
    # 段1 末帧与全片 [n1-1] 应当一致（拼接不重编码画面内容）。
    seam = n1
    seg1_last_in_full = float(np.abs(full[seam - 1].astype(np.float32)
                                     - s1[-1].astype(np.float32)).mean())
    seg2_first_in_full = float(np.abs(full[seam].astype(np.float32)
                                      - s2[0].astype(np.float32)).mean())
    # 反向搜：全片里 s2[0] 最早出现在哪一帧（确认接续点没偏移）
    seg2_first_idx = None
    for i in range(max(0, seam - 3), min(n_full, seam + 4)):
        if np.array_equal(full[i], s2[0]):
            seg2_first_idx = i
            break

    # ---- 核心量 ----
    diffs_l1 = frame_diffs(full, "l1")
    diffs_l2 = frame_diffs(full, "l2")
    stats = rgb_to_lab_stats(full)
    hf = highfreq_energy(full)

    # in_segment_scale：**段内**（不含接续点那一帧）相邻帧差的中位数
    #   取 full[0:seam-1] 的差 -> 完全在段1 内部，天然不含接续点
    seg_internal = diffs_l1[: max(1, seam - 1)]
    in_scale_median = float(np.median(seg_internal))
    in_scale_mean = float(seg_internal.mean())
    in_scale_p90 = float(np.percentile(seg_internal, 90))
    in_scale_all_median = float(np.median(diffs_l1))

    seam_jump = float(diffs_l1[seam - 1]) if seam - 1 < len(diffs_l1) else float("nan")
    seam_jump_l2 = float(diffs_l2[seam - 1]) if seam - 1 < len(diffs_l2) else float("nan")

    # 跨段直接比（不经过拼接）：段1 末帧 vs 段2 首帧
    cross = float(np.linalg.norm(
        s1[-1].astype(np.float32).reshape(-1, 3).mean(axis=0) -
        s2[0].astype(np.float32).reshape(-1, 3).mean(axis=0)))

    win = 8
    lo = max(0, seam - win)
    hi = min(n_full, seam + win)
    rows = []
    for i in range(lo, hi):
        rows.append({
            "frame": i,
            "mean_rgb": [round(float(x), 3) for x in stats["mean_rgb"][i]],
            "luma": round(float(stats["luma_mean"][i]), 3),
            "luma_std": round(float(stats["luma_std"][i]), 3),
            "sat": round(float(stats["sat_mean"][i]), 4),
            "chroma_rg": round(float(stats["chroma_rg_mean"][i]), 4),
            "chroma_bg": round(float(stats["chroma_bg_mean"][i]), 4),
            "hf_energy": round(float(hf[i]), 4),
            "diff_to_prev": round(float(diffs_l1[i - 1]), 4) if i > 0 else None,
        })

    # 高频能量在接续点前后是否阶跃（低分辨率段的典型特征：细节档下降）
    hf_before = float(np.mean(hf[max(0, seam - win):seam]))
    hf_after = float(np.mean(hf[seam:min(n_full, seam + win)]))

    # ---- ★ 决定性归因：色档阶跃到底发生在「段2 自己的头」还是「拼接点」？----
    #
    # 【为什么要拆开看】上一轮的结论是「SelfLift 低分辨率段把钉住区降采样后
    # 当锚点 → 新生成区继承低分辨率段的色档」。这句话隐含一个可检验的推论：
    # 如果色档阶跃来自 **段2 的采样过程**（SelfLift 降采样锚点），
    # 那么 **段2 自己的头 8 帧内部就已经有色档阶跃**，
    # 而且这段阶跃在 **段2 单独播放时同样存在**（不依赖拼接）。
    #
    # 反之，如果色档阶跃只出现在 **拼接边界**（拼接器/时间轴问题），
    # 那么 段2 内部的色彩应当是连续的，阶跃只在 full[seam] vs full[seam-1]。
    #
    # 这两种情形的修法完全不同：
    #   · 采样侧（SelfLift）→ 要改采样/锚点策略；
    #   · 拼接侧 → 要改拼接器。
    # 所以这个归因是**决定修法方向**的分水岭。
    s2_stats = rgb_to_lab_stats(s2)
    s2_diffs = frame_diffs(s2, "l1")
    # 段2 自身的「首帧 vs 后续帧」色档阶跃（段2 第 0 帧是钉住区末帧，
    # 第 1 帧起是新生成区 —— 所以段2 的第 0→1 帧就是**段内的色档断点**）
    s2_head_break = float(s2_diffs[0]) if len(s2_diffs) else float("nan")
    # 段2 内部（跳过第 0 帧的断点）的中位帧间差 = 段2 的「正常」帧间尺度
    s2_internal_scale = float(np.median(s2_diffs[1:])) if len(s2_diffs) > 1 else float("nan")
    s2_head_ratio = (s2_head_break / s2_internal_scale
                     if s2_internal_scale and s2_internal_scale > 0 else None)
    # 段2 首帧 vs 第二帧的**逐通道**均值差（看清是哪个通道在跳）
    s2_m = s2.astype(np.float32).reshape(len(s2), -1, 3).mean(axis=1)
    s2_head_channel_delta = [round(float(x), 4) for x in (s2_m[1] - s2_m[0])]
    # 段1 末帧 vs 段2 首帧（跨段直接比）的逐通道差
    s1_m = s1.astype(np.float32).reshape(len(s1), -1, 3).mean(axis=1)
    cross_channel_delta = [round(float(x), 4) for x in (s2_m[0] - s1_m[-1])]
    # 段2 头 8 帧的逐帧亮度剖面
    s2_luma_head = [round(float(s2_stats["luma_mean"][i]), 3)
                    for i in range(min(10, len(s2)))]
    # 段1 尾 8 帧的逐帧亮度剖面（与段2 头拼起来看连续性）
    s1_luma_tail = [round(float(rgb_to_lab_stats(s1)["luma_mean"][i]), 3)
                    for i in range(max(0, len(s1) - 10), len(s1))]

    # 逐帧亮度剖面（缝前 24 帧 + 缝后 24 帧）
    luma_profile = [round(float(stats["luma_mean"][i]), 3)
                    for i in range(max(0, seam - 24), min(n_full, seam + 24))]

    return {
        "full": str(full_p), "seg1": str(seg1_p), "seg2": str(seg2_p),
        "frames": {"full": n_full, "seg1": n1, "seg2": n2,
                   "sum": n1 + n2, "conserved": conserved},
        "seam_frame_index": seam,
        "seam_rgb_jump": round(seam_jump, 3),
        "seam_rgb_jump_l2": round(seam_jump_l2, 3),
        "in_segment_rgb_scale_median": round(in_scale_median, 4),
        "in_segment_rgb_scale_mean": round(in_scale_mean, 4),
        "in_segment_rgb_scale_p90": round(in_scale_p90, 4),
        "all_frame_diffs_median": round(in_scale_all_median, 4),
        "seam_ratio_vs_median": round(seam_jump / in_scale_median, 3) if in_scale_median > 0 else None,
        "seam_ratio_vs_mean": round(seam_jump / in_scale_mean, 3) if in_scale_mean > 0 else None,
        "seam_ratio_vs_all_median": round(seam_jump / in_scale_all_median, 3) if in_scale_all_median > 0 else None,
        "cross_segment_direct_jump": round(cross, 3),
        "seg1_last_matches_full": round(seg1_last_in_full, 6),
        "seg2_first_found_at_full_frame": seg2_first_idx,
        "hf_energy_before_seam": round(hf_before, 4),
        "hf_energy_after_seam": round(hf_after, 4),
        "hf_energy_ratio_after_over_before": round(hf_after / hf_before, 4) if hf_before > 0 else None,
        # ---- ★ 归因 ----
        "attribution": {
            "seg2_head_break_l1": round(s2_head_break, 4),
            "seg2_internal_scale_median": round(s2_internal_scale, 4),
            "seg2_head_break_ratio": (round(s2_head_ratio, 3)
                                      if s2_head_ratio is not None else None),
            "seg2_head_channel_delta": s2_head_channel_delta,
            "cross_segment_channel_delta": cross_channel_delta,
            "seg1_tail_luma": s1_luma_tail,
            "seg2_head_luma": s2_luma_head,
        },
        "window": rows,
        "luma_profile_around_seam": luma_profile,
        "luma_profile_offset": max(0, seam - 24),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("full")
    ap.add_argument("seg1")
    ap.add_argument("seg2")
    ap.add_argument("--json")
    a = ap.parse_args()
    r = analyze(a.full, a.seg1, a.seg2)
    txt = json.dumps(r, ensure_ascii=False, indent=2)
    print(txt)
    if a.json:
        Path(a.json).write_text(txt, encoding="utf-8")
        print(f"\n[saved] {a.json}", file=sys.stderr)


if __name__ == "__main__":
    main()
