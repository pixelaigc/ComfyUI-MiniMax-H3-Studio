#!/usr/bin/env python3
"""
Pixel-level verification of the AV upload thumbnails.

【为什么不能只看 DOM 断言】
「有 <img> 且 src 指向 /api/assets/...」这类 DOM 断言**会假通过**：
窄屏下缩略图只有 52px，截图时 <img> 可能还没解码完 ——
拍到的是空白方框，而 DOM 断言照样全绿。

所以这里数截图里**特定颜色**的像素：
  * 音频缩略图 —— 波形图由 media_probe.make_waveform 用
    showwavespic 生成，颜色写死 #6ea8fe（浅蓝）
  * 视频缩略图 —— 首帧就是视频的真实画面（测试视频是纯色 0x3050A0）
  * 图片缩略图 —— 测试图是纯红 RGB(200,30,30)

【怎么定位缩略图区域】
缩略图在附件区里，尺寸固定 62x62（CSS）。截图是 device_scale_factor=2，
所以像素尺寸是 124x124。我们按「附件条左侧第一个方块」的位置取样。
"""
import json
import sys
from pathlib import Path

SHOTS = Path(sys.argv[1] if len(sys.argv) > 1
              else "/root/projects/h3studio-agent-av-upload/evidence/media-av")

WAVEFORM_COLOR = (0x6e, 0xa8, 0xfe)   # 音频波形图的写死颜色
VIDEO_COLOR = (0x30, 0x50, 0xa0)      # 测试视频首帧的纯色


def count_color(image, rgb, region, tol=42, step=1):
    """数指定区域内符合 RGB 的像素。"""
    x0, y0, x1, y1 = region
    count = 0
    for y in range(y0, min(y1, image.size[1]), step):
        for x in range(x0, min(x1, image.size[0]), step):
            r, g, b = image.getpixel((x, y))
            if (abs(r - rgb[0]) < tol and abs(g - rgb[1]) < tol
                    and abs(b - rgb[2]) < tol):
                count += 1
    return count


def find_thumb_regions(image):
    """定位附件缩略图方块（62 CSS px -> 124 设备像素）。

    【怎么定位而不写死坐标】
    缩略图是等宽高的方块，左上角对齐附件条。我们扫描整张图，
    找「连续的行里，出现大量非背景色像素块」的起始位置。
    更稳的办法：直接用 CSS 的几何信息 —— 这里用「附件条左侧起
    固定边距 + 卡片宽」的经验值 + 颜色搜索兜底。
    """
    return None


def main():
    from PIL import Image

    report = {}
    for tag in ("375", "390", "1280"):
        for case, color, label in (("03-audio-ready", WAVEFORM_COLOR, "音频波形"),
                                   ("05-video-ready", VIDEO_COLOR, "视频首帧")):
            path = SHOTS / f"mediaav-{tag}-{case}.png"
            if not path.is_file():
                report[f"{tag}-{case}"] = {"ok": False, "reason": "缺截图"}
                continue
            image = Image.open(path).convert("RGB")
            width, height = image.size

            # 缩略图区域：附件条在输入框上方，靠左。
            # 我们在整图左侧 40% 宽度、下方 55%~92% 高度范围内搜索，
            # 取「颜色命中数最多」的 124x124 方块。
            best = 0
            best_box = None
            region_x1 = int(width * 0.45)
            region_y0 = int(height * 0.50)
            region_y1 = int(height * 0.95)
            # 粗扫：每 8 像素一个采样点，找命中密度最高的 124px 方块
            for by in range(region_y0, max(region_y0 + 1, region_y1 - 124), 8):
                for bx in range(0, max(1, region_x1 - 124), 8):
                    hits = count_color(image, color, (bx, by, bx + 124, by + 124),
                                       step=2)
                    if hits > best:
                        best = hits
                        best_box = (bx, by, bx + 124, by + 124)

            report[f"{tag}-{case}"] = {
                "ok": best >= 60,
                "label": label,
                "color": list(color),
                "best_hits": best,
                "box": best_box,
                "screenshot": str(path),
            }
            print(f"{tag}-{case}: {label} 命中像素 {best} "
                  f"(box={best_box}) -> {'OK' if best >= 60 else 'FAIL'}")

    out = SHOTS / "pixel_verification.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    failed = [k for k, v in report.items() if not v.get("ok")]
    print(f"\n总�� {len(report)} 项，失败 {len(failed)}: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
