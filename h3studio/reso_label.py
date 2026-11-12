# -*- coding: utf-8 -*-
"""
生成库卡片的「分辨率 / 画面比例」标签计算 —— **唯一真源**。

【为什么需要这个模块（2026-11-11 用户实测报出的缺陷）】
生成库任务卡片上的「分辨率」这一行，原先直接读任务参数里的
`params["resolution"]`（一个**档位字符串**，如 "544p" / "768p" / "1mp"）。
问题在于：

  1. **文生视频新面板根本不写 `resolution`**。它提交的是
     `width` / `height`（真实像素宽高），`resolution` 只是前端全局 state
     里遗留的旧字段（app.js 的 `state.res`），默认恒为 "544p"。
     于是面板实际跑 1344×768，卡片却显示「544p · 16:9」——
     544p 根本不是 1344×768 的任何合法档位。

  2. **档位字符串与像素是两条独立的语义**。同一个 "1mp" 在 16:9 下是
     1360×768，在 1:1 下是 1024×1024，在 21:9 下是 1568×672。
     只看档位字符串永远还原不出真实尺寸。

  3. **请求尺寸 ≠ 实际输出尺寸**是常态（不是异常）：
       * 视频侧：H3 画布上限 768×1344，超了会被按比例缩（1080p -> 1920×1088
         再被钳到画布内）；
       * 图像侧：Z-Image ≥3MP 自动走「低分辨率生成 + RealESRGAN 放大 + 低 denoise
         二次细化」的官方 2K 放大链，最终像素由 ImageScale 节点决定；
       * 图片编辑（i2i_qwen）：最终输出可能与请求的档位/比例无关
         （实测请求 1360×768 出了 2048×2048、2720×1536）。
     所以卡片必须以**实际输出像素**为准，并显式标注与请求值的差异。

【本模块的职责】
把「(请求宽高, 请求档位, 请求比例) + (实际输出宽高)」算成一行人能看懂的标签：

    width=1344, height=768            -> "768p · 1344×768 · 16:9"
    width=768,  height=1344           -> "768p · 768×1344 · 9:16"
    width=2560, height=1408 (请求 1400) -> "…（实际输出 2560×1408，与请求 2560×1400 不同）"

【硬约束】
  * 档位标签由**实际像素**反算（短边像素 -> "768p" 语义），不是查档位表；
  * 比例标签由**实际像素**反算（宽高比约分），不是照抄请求的 aspect；
  * 请求值与实际值不一致时，**必须**把差异写进 note，不允许悄悄显示一个
    与产物对不上的数字；
  * 不改各面板的生成逻辑 —— 本模块**只读**参数，不产生任何副作用。

运行：python3 -m pytest tests/test_reso_label.py -v
"""
from __future__ import annotations

from math import gcd

#: ComfyUI / H3 侧合法的画面比例集合（与 workflows.ASPECT_NUM 一致）。
#: 这里只用于「把真实宽高比**归一**到用户熟悉的写法」，不是用来反推尺寸。
KNOWN_ASPECTS: tuple[str, ...] = (
    "1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9", "21:9",
)

#: 比例标签的匹配容差（相对误差）。真实像素（如 1344×768 = 1.75）
#: 不一定精确等于名义比例（16:9 = 1.7778），所以要按容差判定。
ASPECT_TOLERANCE = 0.02

#: 像素吸附步长。H3 画布是 32 的倍数（CANVAS_MULTIPLE=32），
#: 图像侧是 16 的倍数 —— 所以「名义比例算出来的宽高」落到真实像素时
#: 必然有量化误差。比例匹配必须容忍这个量化，否则面板自己列出的
#: 16:9 预设（608×352，实测比例 1.727）会被判成 19:11。
ASPECT_QUANTUM = 32


# ------------------------------------------------------------------
# 基础工具
# ------------------------------------------------------------------
def _as_int(value) -> int | None:
    """把宽高字段安全转成 int；非法 / 缺失一律返回 None（不猜、不填默认值）。"""
    if value is None or value is True or value is False:
        return None
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _aspect_tolerance(width: int, height: int) -> float:
    """
    比例匹配容差 = max(固定容差, 量化步长 / 短边)。

    量化误差来自「宽高各自吸附到 16/32 的倍数」：短边越小，
    同一个名义比例算出来的真实比例偏差越大。
    例：16:9 的 0.2MP 预设 608×352，真实比例 1.727，
    相对名义 1.778 差 2.8% —— 超过固定 2% 就会被误判。
    """
    short = min(width, height)
    if short <= 0:
        return ASPECT_TOLERANCE
    return max(ASPECT_TOLERANCE, ASPECT_QUANTUM / float(short))


def _ratio_label(width: int, height: int, preferred: str = "") -> str:
    """
    由**实际像素**算画面比例标签。

    优先匹配已知比例（16:9 / 9:16 / …），匹配不上就约分成最简整数比
    （例如 1344×768 -> 7:4，2560×1408 -> 20:11）。

    preferred（通常是请求的比例）：当它也在容差内时优先采用 ——
    这样卡片与用户在面板里选的「画面比例」一致，
    而不是因为量化误差落到另一个比例上。
    """
    if width <= 0 or height <= 0:
        return ""
    tolerance = _aspect_tolerance(width, height)
    actual = width / height
    candidates = []
    for name in KNOWN_ASPECTS:
        try:
            wr, hr = (int(x) for x in name.split(":"))
        except ValueError:
            continue
        target = wr / hr
        err = abs(actual - target) / target
        if err <= tolerance:
            candidates.append((err, name))
    if candidates:
        if preferred:
            for _, name in candidates:
                if name == preferred:
                    return name
        candidates.sort()
        return candidates[0][1]
    # 约分到最简整数比
    divisor = gcd(width, height)
    return f"{width // divisor}:{height // divisor}"


def _tier_label(width: int, height: int) -> str:
    """
    由**实际像素**算分辨率档位标签。

    语义与视频面板的官方预设保持一致（video_models.size_presets_for_aspect
    里的 `f"{short}p · {w}×{h}"`）：档位名用**短边像素**，
    因为那是用户对「480p / 544p / 768p」这类叫法的直觉。

    【为什么不能四舍五入到 10 的倍数】
    官方预设里的短边是 352 / 416 / 480 / 544 / 608 / 640 / 672 / 736 / 768 ——
    其中 416 / 544 / 608 / 736 / 768 **都不是 10 的倍数**。
    一旦四舍五入，768 就会变成 770，与面板自己的预设标签对不上，
    用户看到「770p」会以为系统算错了。所以这里**原样用短边像素**。
    """
    if width <= 0 or height <= 0:
        return ""
    return f"{min(width, height)}p"


def _megapixels(width: int, height: int) -> float:
    return (width * height) / (1024.0 * 1024.0)


# ------------------------------------------------------------------
# 主入口
# ------------------------------------------------------------------
def describe(job_params: dict, actual: dict | None = None) -> dict:
    """
    算出生成库卡片该显示的分辨率 / 比例标签。

    参数
    ----
    job_params:
        任务记录里的 params（请求值）。可能只有 resolution 档位字符串
        （老任务 / 旧面板），也可能有 width+height（新面板），两者都有最理想。
    actual:
        实际输出尺寸 {"width": w, "height": h}。**有就以它为准**
        （这是需求第 2、4 条）。没有就退回请求值。

    返回
    ----
    {
      "width": int|None,          # 用于展示的宽度（实际优先）
      "height": int|None,
      "tier": "768p" | "",        # 档位标签
      "aspect": "16:9" | "",      # 比例标签
      "pixels": "1344×768" | "",  # 像素文本
      "label": "768p · 1344×768 · 16:9",
      "source": "actual" | "request" | "none",
      "note": "",                 # 请求值与实际值的差异说明（无差异时为空）
      "has_size": bool,
    }
    """
    job_params = job_params or {}
    actual = actual or {}

    req_w = _as_int(job_params.get("width"))
    req_h = _as_int(job_params.get("height"))
    act_w = _as_int(actual.get("width"))
    act_h = _as_int(actual.get("height"))

    # ---- 实际值优先 ----
    if act_w and act_h:
        width, height = act_w, act_h
        source = "actual"
    elif req_w and req_h:
        width, height = req_w, req_h
        source = "request"
    else:
        # 两个尺寸都没有 —— 只剩一个档位字符串（如老任务 "1mp"）
        # 这种情况**不编造尺寸**，只把档位原样带出来。
        tier_only = str(job_params.get("resolution") or "").strip()
        aspect_only = str(job_params.get("aspect") or "").strip()
        return {
            "width": None, "height": None,
            "tier": tier_only, "aspect": aspect_only,
            "pixels": "",
            "label": " · ".join(x for x in (tier_only, aspect_only) if x),
            "source": "none",
            "note": "",
            "has_size": False,
        }

    tier = _tier_label(width, height)
    # 请求的比例作为「优先候选」：只要它也在容差内就采用，
    # 这样卡片与面板里用户选的比例一致（量化误差不该改写用户的意图）。
    requested_aspect = str(job_params.get("aspect") or "").strip()
    aspect = _ratio_label(width, height, preferred=requested_aspect)
    pixels = f"{width}×{height}"

    # ---- 请求值与实际值的差异 ----
    note = ""
    if source == "actual" and req_w and req_h and (req_w, req_h) != (width, height):
        note = (f"实际输出 {pixels}，与请求的 {req_w}×{req_h} 不同"
                f"（{_megapixels(width, height):.1f}MP）")

    label = " · ".join(x for x in (tier, pixels, aspect) if x)
    return {
        "width": width, "height": height,
        "tier": tier, "aspect": aspect, "pixels": pixels,
        "label": label,
        "source": source,
        "note": note,
        "has_size": True,
    }


def tier_for_pixels(width, height) -> str:
    """只算档位标签（供测试与其它调用方复用）。"""
    w, h = _as_int(width), _as_int(height)
    if not w or not h:
        return ""
    return _tier_label(w, h)


def aspect_for_pixels(width, height, preferred: str = "") -> str:
    """只算比例标签（供测试与其它调用方复用）。"""
    w, h = _as_int(width), _as_int(height)
    if not w or not h:
        return ""
    return _ratio_label(w, h, preferred=preferred)
