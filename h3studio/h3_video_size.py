# -*- coding: utf-8 -*-
"""首尾帧 / 参考生视频的**分辨率与画面比例**语义（唯一真源）。

【为什么单独一个模块 —— 这是一个用户可见的「说谎」缺陷】

旧面板（flf / r2v）的参数区有两个控件：
  * 「分辨率」档位：544p / 768p（``#resSeg``）
  * 「画面比例」：16:9 / 9:16 / 4:3 / 3:4 / 1:1（``#aspectChips``）

但提交链路是：

  1. 前端只提交 ``params.resolution``（档位字符串）与 ``params.aspect``（比例）；
  2. 服务端的 ``normalize_t2v_params`` **只对 t2v 跑**（server.py 的
     ``if mode == "t2v":``），flf / r2v 走不到它；
  3. ``h3studio/h3_video_modes.py`` 的 build_flf / build_r2v 只读
     ``params.width`` / ``params.height``，**缺失时硬回退 1344×768**。

于是面板显示「544p · 16:9」，工作流实际跑 1344×768 —— 而 1344×768
**不是** 544p 的任何合法组合（544p = 0.4MP，16:9 下是 736×416）。
这是典型的「界面说的和产物不是一回事」。

【本模块的职责】

把「档位 + 比例」翻译成**真实像素宽高**，让 flf / r2v 与 t2v 走**同一套**
语义：

  * 档位 -> 总像素（MP）-> 宽高：算法与 ComfyUI 自带的 ResolutionSelector
    完全一致（``comfy_extras/nodes_resolution.py`` 实测源码），
    本仓库 ``video_models.video_dimensions`` 是同一套公式；
  * 比例 -> 宽高比（``video_models.VIDEO_ASPECTS``）；
  * 上限按**各模式自己的**面积策略（``video_models.max_pixels_for_mode``）：
    **2026-11-17 起 flf / r2v = t2v = 2.0 MP**（对齐 nonebot seg1/seg2 的
    ``ResolutionSelector(megapixels=2)``；上游 T8 / 核心节点均无面积门禁）；
  * 宽高必须吸附到 32 的倍数（H3 的 CANVAS_MULTIPLE，节点 schema step=32）。

【为什么不直接把前端改成提交 width/height】

前端确实应该提交真实宽高（更不容易出错），但**服务端也必须能兜住**：
  * 内置 Agent / 老客户端 / 直接调 API 的调用方仍然会传档位字符串；
  * 面板与服务端**必须用同一套公式**，否则又是一处「两边不一致」。

所以这里做的是：**服务端在 flf / r2v 的归一层也把宽高算出来并写回 params**，
前端同时提交 width/height（真实值）与 resolution/aspect（用户意图），
两边算出来的结果必须一致 —— 不一致时以**服务端**为准（它才知道上限策略）。

【测试】tests/test_h3_video_size.py
"""

from __future__ import annotations

from . import video_models as vmodels


#: 旧面板的两个分辨率档位 -> 总像素（MP）。
#:
#: 【为什么是这两个】
#: 面板 HTML（``web/index.html`` 的 ``#resSeg``）只有 544p / 768p 两档，
#: 而旧工作流 ``workflows.RES_MP`` 也是同一组取值（{"544p": 0.4, "768p": 0.8}）。
#: 两处必须一致 —— 面板写 544p、工作流按 0.8MP 跑，就是用户看到的那种撒谎。
#:
#: 544p = 0.4 MP -> 16:9 下 736×416（短边 416）
#: 768p = 0.8 MP -> 16:9 下 1344×768（短边 768）
LEGACY_RESOLUTION_MP: dict[str, float] = {
    "544p": 0.4,
    "768p": 0.8,
}

#: 面板上的默认档位（与 ``#resSeg`` 的 active 项一致）。
DEFAULT_RESOLUTION = "768p"

#: 面板上的默认比例（与 ``#aspectChips`` 的 active 项一致）。
DEFAULT_ASPECT = "16:9"


def resolution_to_megapixels(resolution: str | None) -> float:
    """档位字符串 -> 总像素（MP）。

    未知档位 -> 默认 768p（0.8 MP）。

    【为什么不报错】档位是**展示用**的语义标签，历史上出现过
    "1mp" / "1080p" / "0.8" 等多种写法（老任务里都有）。
    服务端在这里报错会让**老任务无法复用**，得不偿失。
    真正决定产物尺寸的是 width/height，档位只是没给宽高时的兜底。
    """
    text = str(resolution or "").strip()
    if text in LEGACY_RESOLUTION_MP:
        return LEGACY_RESOLUTION_MP[text]
    return LEGACY_RESOLUTION_MP[DEFAULT_RESOLUTION]


def dimensions_for(resolution: str | None, aspect: str | None,
                    mode: str = "flf") -> tuple[int, int]:
    """档位 + 比例 -> 真实像素宽高（**已按该模式的上限裁过**）。

    返回 (width, height)。

    【为什么不直接用 video_dimensions】那个函数只按公式算，不看上限。
    flf / r2v 与 t2v 的面积上限现在**相同**（2.0 MP，2026-11-17 放宽）；
    但这里仍必须按模式上限收口，否则将来某个模式单独放宽时，
    会提交一个工作流跑不动的尺寸。

    【2026-11-20 —— 新增「MP 预算 × 百分比」入口】
    用户要求「无论生成什么媒体，都是用百万分辨率 + 百分比模式」。
    所以额外提供 :func:`dimensions_for_spec`；本函数保持**旧档位语义**
    不变（老任务重放必须得到同样的像素）。
    """
    mp = resolution_to_megapixels(resolution)
    aspect_key = str(aspect or "").strip()
    if aspect_key not in vmodels.VIDEO_ASPECTS:
        aspect_key = DEFAULT_ASPECT
    width, height = vmodels.video_dimensions(aspect_key, mp)
    max_pixels = vmodels.max_pixels_for_mode(mode)
    if max_pixels:
        limit = max_pixels * 1024 * 1024
        if width * height > limit:
            # 面积超限 -> 按比例缩回上限内（并吸附到 32 的倍数）。
            # 复用服务端权威的吸附器，保证与 /api/video-size 同口径。
            scaled_w, scaled_h, _reasons = vmodels.normalize_video_size(
                width, height, max_pixels=max_pixels)
            if scaled_w > 0 and scaled_h > 0:
                width, height = scaled_w, scaled_h
    return width, height


def dimensions_for_spec(mode: str,
                         aspect: str | None = None,
                         megapixels: float | None = None,
                         percent: float | None = None,
                         resolution: str | None = None,
                         width: int | None = None,
                         height: int | None = None,
                         ) -> tuple[int, int, list[str]]:
    """
    「MP 预算 × 百分比」-> (width, height, 调整原因)。

    【为什么薄封装到 size_spec】换算公式（MP -> 宽高、32 对齐、超限缩放）
    已经在 ``h3studio/size_spec.py`` 里统一实现，本模块只做**接线**：
    让 flf / r2v 的服务端归一层也能吃这套表达，而不是只有管理页能填。

    【优先级】显式 width/height > megapixels > percent > resolution 档位。
    与 ``size_spec.plan_size`` 完全一致（同一份实现，不会有第二套规则）。
    """
    from . import size_spec
    return size_spec.plan_size(
        mode,
        aspect=aspect or DEFAULT_ASPECT,
        megapixels=megapixels,
        percent=percent,
        resolution=resolution,
        width=width,
        height=height,
    )


def resolve_size(params: dict, mode: str) -> tuple[int, int, list[str]]:
    """给一个任务的 params 算出**最终生效**的宽高。

    返回 (width, height, notes)。

    【优先级 —— 与文生视频面板完全一致】
      1. params 里**已经有** width / height（真实像素）-> 直接采用；
         它们可能是用户手填的，也可能是别的调用方算好的。
      2. 否则用「档位 + 比例」按本模块的公式算。
      3. 都没有 -> 用面板默认值（768p / 16:9）。

    【为什么优先级 1 在前】面板会同时提交 width/height 与档位/比例，
    两者的语义**应当一致**；真不一致时以 width/height 为准 ——
    因为它才是工作流真正读的值（h3_video_modes.build_* 只读这两个键）。

    【2026-11-20 —— 新增 MP 预算 × 百分比】
    params 里出现 ``megapixels`` / ``resolution_percent`` 时走
    ``size_spec.plan_size``（**同一份**换算实现，不另造规则）：
      * ``megapixels``（MP 预算）优先于 ``resolution_percent``；
      * 两者都没有时，仍按旧档位语义算（老任务重放像素不变）。
    """
    notes: list[str] = []

    # 【2026-11-20 新表达】MP 预算 / 百分比 —— 走 size_spec 统一实现
    has_spec = (params.get("megapixels") not in (None, "")
                or params.get("resolution_percent") not in (None, ""))
    if has_spec:
        spec_w, spec_h, spec_notes = dimensions_for_spec(
            mode,
            aspect=params.get("aspect"),
            megapixels=params.get("megapixels"),
            percent=params.get("resolution_percent"),
            resolution=params.get("resolution"),
            width=params.get("width"),
            height=params.get("height"),
        )
        if spec_w > 0 and spec_h > 0:
            notes.extend(spec_notes)
            # 【2026-11-20】把**生效**的 MP 预算写回 params ——
            # 这样「用户请求的 MP」与「实际生效的 MP」可对比，
            # Agent 的 ``_collect_adjustments`` 才能回报差异。
            effective_mp = spec_w * spec_h / (1024 * 1024)
            params["megapixels"] = round(effective_mp, 3)
            return spec_w, spec_h, notes
        # 算不出来 -> 落回旧档位语义（notes 里已带原因）
        notes.extend(spec_notes)

    raw_w = params.get("width")
    raw_h = params.get("height")

    def _as_int(value) -> int | None:
        try:
            out = int(float(value))
        except (TypeError, ValueError):
            return None
        return out if out > 0 else None

    width = _as_int(raw_w)
    height = _as_int(raw_h)
    if width and height:
        # 已有真实宽高 -> 过一遍该模式的上限（可能超限）+ **32 对齐**
        max_pixels = vmodels.max_pixels_for_mode(mode)
        if max_pixels:
            limit = max_pixels * 1024 * 1024
            if width * height > limit:
                snapped_w, snapped_h, reasons = vmodels.normalize_video_size(
                    width, height, max_pixels=max_pixels, strict_over_limit=False,
                    mode=mode)
                if snapped_w > 0 and snapped_h > 0:
                    notes.append(
                        f"请求尺寸 {width}×{height} 超过 {mode} 模式上限 "
                        f"{max_pixels}MP -> 已缩到 {snapped_w}×{snapped_h}")
                    width, height = snapped_w, snapped_h
                else:
                    notes.extend(reasons)
        # 【2026-11-20 修 —— 显式宽高也必须吸附到 32 的倍数】
        #
        # 【原来错在哪（实测复现）】显式宽高只过了**面积上限**检查，
        # 没做 32 对齐 —— 提交 1900×1000 就原样跑 1900×1000。
        # 但 H3 的 CANVAS_MULTIPLE = 32、节点 schema step = 32、
        # latent 网格 = width//16 —— **非 32 倍数的宽高会得到不对齐的
        # latent 网格**，产物与面板显示对不上。
        # 这正是本项目反复修的「界面说谎」家族：面板写 1900，
        # 产物实际不是那个尺寸。
        #
        # 【为什么必须在这里吸附，而不是留给工作流】
        # ``normalize_video_size`` 是服务端权威的吸附器（与 /api/video-size
        # 同口径）。在这里调用 = 两条建任务路径（面板提交 / Agent 自建）
        # 拿到的是**同一套**吸附结果。
        multiple = vmodels.VIDEO_SIZE_MULTIPLE
        aligned_w = max(multiple, int(round(width / multiple)) * multiple)
        aligned_h = max(multiple, int(round(height / multiple)) * multiple)
        if aligned_w != width:
            notes.append(
                f"宽度 {width} 不是 {multiple} 的倍数 -> 已吸附到 {aligned_w}")
            width = aligned_w
        if aligned_h != height:
            notes.append(
                f"高度 {height} 不是 {multiple} 的倍数 -> 已吸附到 {aligned_h}")
            height = aligned_h
        # 单边上限（节点 schema max = 16384）
        if width > vmodels.VIDEO_SIZE_MAX:
            notes.append(
                f"宽度 {width} 超过上限 {vmodels.VIDEO_SIZE_MAX} -> 已钳到上限")
            width = vmodels.VIDEO_SIZE_MAX
        if height > vmodels.VIDEO_SIZE_MAX:
            notes.append(
                f"高度 {height} 超过上限 {vmodels.VIDEO_SIZE_MAX} -> 已钳到上限")
            height = vmodels.VIDEO_SIZE_MAX
        return width, height, notes

    if width or height:
        notes.append(
            f"只给了宽高其中一项（{raw_w}×{raw_h}）-> 已按档位+比例重算")
        width = height = None

    resolution = params.get("resolution")
    aspect = params.get("aspect")
    width, height = dimensions_for(resolution, aspect, mode=mode)
    notes.append(
        f"面板档位 {resolution or DEFAULT_RESOLUTION} + 比例 "
        f"{aspect or DEFAULT_ASPECT} -> {width}×{height}")
    return width, height, notes


def apply_size(params: dict, mode: str) -> dict:
    """就地写回 params["width"] / params["height"]（以及自洽的档位标签）。

    返回**同一个** params 对象（就地修改），方便链式调用。

    【为什么还要写回 resolution】``params["resolution"]`` 是**面板档位字符串**
    （544p / 768p），而 ``h3studio/reso_label.py`` 的卡片标签是**从实际像素
    反算**的。两者不必相同（0.4MP 在 1:1 下是 640×640，短边 640p，
    不是 544p）。所以这里**不改** resolution（那是用户选的原意），
    只保证 width/height 是真实生效值 —— 卡片按实际像素显示，不会撒谎。
    """
    width, height, notes = resolve_size(params, mode)
    params["width"] = width
    params["height"] = height
    if notes:
        existing = params.get("size_adjust_reasons")
        merged = list(existing) if isinstance(existing, list) else []
        for note in notes:
            if note not in merged:
                merged.append(note)
        params["size_adjust_reasons"] = merged
    return params
