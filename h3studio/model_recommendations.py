# -*- coding: utf-8 -*-
"""
H3 主模型 / 加速 LoRA 的**作者推荐参数**元数据（需求 1 / 2 / 5）。

【为什么要单独一个模块】
用户要求「每一个加速 LoRA 条目都要标注 LoRA 作者推荐的：步数 / 调度器 /
采样器 / 音视频 shift / 推荐的主模型」，主模型同理。

**信息必须可追溯** —— 每一条都写明来源（作者 README / safetensors 元数据 /
官方示例工作流 / ComfyUI 内生常量）。作者没写的，一律填 `NOT_PROVIDED`
（前端显示「作者未提供」），**绝不推测或编造数值**。

【与 video_models.VIDEO_MODELS / VIDEO_ACCEL_LORAS 的关系】
  * `video_models.py` 里的表 = 「本机怎么用」（权重路径、兼容性、默认强度）；
  * 本模块 = 「作者怎么说」（推荐参数 + 来源）。
  两者互补，不互相覆盖 —— 前者是本机工程事实，后者是作者声明。

【值的四种来源类别】(`source_kind`)
  * `author`      —— LoRA/底模作者自己写的（README / safetensors metadata /
                     官方示例工作流）。这是用户要的那一类。
  * `comfyui`     —— ComfyUI 内生常量（H3 的 shift=12 / audio_shift=3 是
                     comfy/supported_models.py 里写死的硬值，**不是**作者写的，
                     但它是**模型本身的硬性要求**，必须挂）。
  * `nonebot`     —— 本机另一条工作流链路（nonebot H3 模板）实测用的值。
                     只作参考，不冒充作者推荐。
  * `not_provided` —— 作者未提供。

【为什么 shift 分两类】
H3 的 `shift_video=12.0 / shift_audio=3.0` 出自 ComfyUI 的模型定义
（`comfy/supported_models.py` 的 `sampling_settings`），作者**没有**说该用多少；
但漏挂 `MiniMaxH3SigmaShift` 会让画质明显变差。所以我们把它标成
`comfyui`（模型硬性值），而不是伪装成作者推荐值。

DMAD 4 步 critic 是**唯一例外** —— 它的 safetensors metadata 里作者明写
`4 steps, video shift 12, audio shift 2, ... 1344x768, 124 frames at 24 fps,
no CFG`，所以那一档的 audio shift 是**作者提供的 2**，不是内生的 3。
"""

from __future__ import annotations

import re


#: 「作者未提供」的哨兵值。
#: 用一个**显式的字符串常量**而不是 None —— 前端要能区分
#: 「作者写了 0」和「作者没写」这两种情况。
NOT_PROVIDED = "not_provided"


#: 来源类别的显示名（前端直接用）。
SOURCE_KIND_LABEL = {
    "author": "作者提供",
    "comfyui": "ComfyUI 模型硬性值",
    "nonebot": "本机 nonebot 工作流实测值（参考）",
    "not_provided": "作者未提供",
}


def _rec(steps=None, scheduler=None, sampler=None,
         shift_video=None, shift_audio=None,
         base_model=None, source_kind=None, source=None, note="",
         field_sources=None):
    """构造一条推荐参数记录。

    字段语义固定（前端按这组 key 渲染，不要改）：
      steps        推荐步数（int 或 None）
      scheduler    推荐调度器（str 或 None）
      sampler      推荐采样器（str 或 None）
      shift_video  视频 shift（float 或 None）
      shift_audio  音频 shift（float 或 None）
      base_model   推荐搭配的主模型（str 或 None）
      source_kind  见 SOURCE_KIND_LABEL（**整条**的来源类别）
      source       来源定位（绝对路径 + 原文锚点），可追溯
      note         补充说明
      field_sources  {字段: 来源类别} —— **字段级**来源

    【为什么要 field_sources】
    一条记录的各字段**可能来自不同来源**：例如 speed_slider 的
    「步数=作者未提供」但「shift=ComfyUI 模型硬性值」——
    只有一个整条级的 source_kind 会让前端把 shift 也说成「作者提供」。
    字段级来源让每个字段都能如实标出自己的出处。

    「未提供」一律写 `None`，由 `normalize()` 统一转成 NOT_PROVIDED。
    """
    return {
        "steps": steps,
        "scheduler": scheduler,
        "sampler": sampler,
        "shift_video": shift_video,
        "shift_audio": shift_audio,
        "base_model": base_model,
        "source_kind": source_kind or "not_provided",
        "source": source or "",
        "note": note or "",
        "field_sources": dict(field_sources or {}),
    }


#: 【R2 用户可见文案清洗】本机绝对路径前缀。
#:
#: 【为什么要清洗】
#: `source` 字段是「可追溯溯源」，会原样渲染到前端（video-panel.js 的
#: 「出处：」、job-meta.js 的「出处：」）。但溯源文本里写的是
#: **本机绝对路径 + 源码行号 + 代码片段**，例如：
#:   /home/ubuntu/comfyui/comfy/supported_models.py:969-972（shift 12 …）
#:   /mnt/usb2t/comfyui-models/loras/h3/h3_speed_slider_1.1.md:283-295（…）
#:
#: 这属于**内部实现细节**：用户看到的是「这台机器的目录布局」，
#: 既不是作者推荐的一部分，也不是用户能操作的东西 —— 但暴露了
#: 本机用户名（ubuntu）、磁盘分区挂载（/mnt/usb2t）、以及源码行号。
#:
#: 【清洗规则 —— 保留溯源语义，只去掉路径前缀】
#:   /home/ubuntu/comfyui/comfy/supported_models.py:969-972（…）
#:     -> supported_models.py:969-972（…）
#:   /mnt/usb2t/comfyui-models/loras/h3/h3_speed_slider_1.1.md:283-295（…）
#:     -> h3_speed_slider_1.1.md:283-295（…）
#:
#: 也就是：**保留文件名 + 行号 + 括号里的说明**，只砍掉「到文件为止」的那段
#: 目录路径。溯源能力不丢（还能定位到哪个文件第几行），
#: 但本机目录结构不再出现在界面上。
#:
#: 【不动的那些】
#:  * 不含本机路径的溯源文本（作者 README 摘录、模型卡片段、
#:    官方模板 Note 行）—— 那些本来就没有路径，原样保留。
#:  * `source_kind` / `field_sources` —— 那是**类别**，不是路径。
#:  * `note` —— 单独的补充说明字段，不含路径。
#:
#: 【为什么放在数据出口（_normalize）而不是前端】
#: 前端有 2 个渲染点（web/video-panel.js、web/job-meta.js），
#: 改前端要改两处、必然漂移；而且 job_meta 的摘要也是从同一份
#: `_normalize` 出来的。放在这里 = **一个收口点覆盖全部 19 处 source**。
_LOCAL_PATH_PREFIXES = (
    "/home/ubuntu/comfyui/",
    "/home/ubuntu/",
    "/mnt/usb2t/comfyui-models/",
    "/mnt/usb2t/",
    "/mnt/nvme1t_colorful/comfyui-models/",
    "/mnt/nvme1t_colorful/",
)

#: 溯源文本里「本机路径 → 文件名」的匹配。
#:
#: 【关键：必须锚定在已知本机前缀上】
#: 不能写成「任意 /a/b 形态」—— 那会把正常文案里的斜杠也吃掉：
#:   "作者规格表（ModelTC/MiniMax-H3-Turbo README…）" 里的 "/"
#:   "shift 12 / audio_shift 3" 里的 "/"
#: 一旦被吃，就变成 "ModelTCMinimax-H3-Turbo" / "shift 123"。
#:
#: 所以这里用 `(?P<abs>/(?:home/ubuntu|mnt/usb2t|mnt/nvme1t_colorful)/...)`
#: **锚定本机绝对路径前缀**，其余文本一律不碰。
_LOCAL_PATH_RE = re.compile(
    r"(?P<abs>/(?:home/ubuntu|mnt/usb2t|mnt/nvme1t_colorful)"
    r"(?:/[A-Za-z0-9_.\-\u4e00-\u9fff]+)*)"
    r"(?P<anchor>:\d+(?:-\d+)?)?"
)


def _public_source(source: str) -> str:
    """把溯源文本里的**本机绝对路径**换成可读定位（保留文件名+行号）。

    【契约】
      * 不含本机绝对路径的文本 -> **原样返回**（一个字符都不改）。
      * 含本机绝对路径 -> 路径部分只保留**最后一段文件名**（含行号后缀），
        目录层级全部丢弃；括号说明等其余内容原样保留。
      * 空值 -> 空字符串。

    【为什么保留文件名不整条删掉】
    「出处」的价值就是可追溯（哪个文件、第几行）。
    只砍目录层级，既保住了溯源，又不暴露本机目录布局与用户名。

    【为什么不能泛化成「任意 /a/b」】
    见上面 _LOCAL_PATH_RE 的说明 —— 会吃掉正常文案里的斜杠
    （ModelTC/MiniMax-H3-Turbo、shift 12 / audio_shift 3）。
    """
    if not source or not isinstance(source, str):
        return ""

    def _shrink(m: "re.Match[str]") -> str:
        path = m.group("abs")
        anchor = m.group("anchor") or ""
        base = path.rsplit("/", 1)[-1]
        if not base:
            return m.group(0)
        return base + anchor

    return _LOCAL_PATH_RE.sub(_shrink, source)


def _normalize(rec: dict) -> dict:
    """把 None 的字段统一成 NOT_PROVIDED 哨兵。

    这样前端只要判 `=== "not_provided"` 就知道该显示「作者未提供」，
    不会出现「undefined 显示成空白」和「作者写了 0 显示成 0」的混淆。

    顺带补齐 `field_sources`：没单独标来源的字段，继承整条的 source_kind；
    但**明确标成 not_provided 的字段**，其来源一律是 not_provided
    （该字段本身就没值，来源无所谓）。
    """
    out = dict(rec)
    for field in ("steps", "scheduler", "sampler",
                  "shift_video", "shift_audio", "base_model"):
        if out.get(field) is None:
            out[field] = NOT_PROVIDED
    whole = out.get("source_kind") or NOT_PROVIDED
    field_sources = dict(out.get("field_sources") or {})
    for field in ("steps", "scheduler", "sampler",
                  "shift_video", "shift_audio", "base_model"):
        if field_sources.get(field) is None:
            field_sources[field] = (
                NOT_PROVIDED if out[field] == NOT_PROVIDED else whole)
    out["field_sources"] = field_sources
    # 【R2】清洗「出处」里的本机绝对路径（见 _public_source 的说明）。
    # 只动 source，不动 source_kind / field_sources / note。
    if isinstance(out.get("source"), str):
        out["source"] = _public_source(out["source"])
    return out


# ==================================================================
# 加速 LoRA（固定档 + 可勾选组合）
# ==================================================================
#
# 【统一事实】H3 的 sigma shift 内生值 = video 12.0 / audio 3.0。
#   来源：/home/ubuntu/comfyui/comfy/supported_models.py:970-971
#         "shift": 12.0, "audio_shift": 3.0
#   同源：/home/ubuntu/comfyui/comfy/ldm/minimax/model.py:11
#         (its own shifted schedule (sigma_shift video 12.0 / audio 3.0))
#   节点侧同值：/home/ubuntu/comfyui/custom_nodes/ComfyUI-MiniMax-H3-Turbo/__init__.py:31
#         SHIFT_V, SHIFT_A = 12.0, 3.0
#
# 【Turbo 家族作者原文】/home/ubuntu/comfyui/custom_nodes/ComfyUI-MiniMax-H3-Turbo/README.md
#   :59-62  "Keep the scheduler on `simple`."
#   :76-83  "**4 steps is the recommended *minimum*; 4-8 is the useful range.**"
#           "**Keep `strength` at `1.0`.**"
#   :85-88  "**Base model** — works with any MiniMax-H3 base: full (`bf16`,
#            `int8_convrot`) **and the pruned/curve variants**"
#   官方示例工作流（作者自带）：
#     /home/ubuntu/comfyui/custom_nodes/ComfyUI-MiniMax-H3-Turbo/example_workflows/minimax_h3_t2v_turbo.json
#     BasicScheduler widgets = ["simple", 6, 1]  -> scheduler=simple, steps=6
#     MiniMaxH3TurboLoRA widgets = [..., 1]      -> strength=1.0
#
# 【DMAD 作者原文】/home/ubuntu/comfyui/models/loras/dmad_minimax_h3_4step_full_critic_comfyui.safetensors
#   __metadata__["sampling"] = "4 steps, video shift 12, audio shift 2,
#                                re-noise step rule, 1344x768, 124 frames at 24 fps, no CFG"
#   __metadata__["model"]    = "DMAD 4-step student of MiniMax-H3
#                                (text-to-audio-video), LoRA on the T2VA transformer"
#   -> 步数 / shift / 分辨率 / 帧数 / CFG 都是**作者给的**。
#   -> 采样器 / 调度器 作者**没写** -> 「作者未提供」。
#
# 【LightX2V 家族】本机无作者 README / 模型卡，safetensors metadata 只有
#   base_model / training_rank / training_alpha / training_scale（**没有**采样参数）。
#   -> 步数只能从文件名的 "4step" / "8step" 读，那是**文件名自述**，不是作者
#      的参数表 —— 标成 `nonebot`（本机另一链路的实测档）会误导，
#      所以这里老实标「作者未提供」，并在 note 里说明文件名自述的步数。
#   -> 采样器 / 调度器：官方 Turbo 家族用 `simple`，但 LightX2V 自己的权重
#      作者没写 —— 标「作者未提供」。
#
# 【speed_slider / better_motion 作者原文】目录内 .md 的「作者提供了什么」表
#   /mnt/usb2t/comfyui-models/loras/h3/h3_speed_slider_1.1.md
#     :289  "| 步数 | ❌ **未提供** | —（本文档的 30~100 步区间为本机实践，非作者推荐） |"
#     :290  "| 采样器 / scheduler | ❌ **未提供** | — |"
#     :288  "| strength | ✅ 提供 | **±8 最优**…"
#   /mnt/usb2t/comfyui-models/loras/h3/h3_better_motion_720x1280_w0.4-0.8_s15-30_vpakarinen_v1.md
#     :183  "| 步数 | ✅ 提供 | **15~30 步**（作者原话 *720x1280 resolution with 15-30 steps*） |"
#     :184  "| 采样器 / scheduler | ❌ 未提供 | — |"
#     :182  "| strength | ✅ 提供 | **0.4~0.8**（作者原话 *Weight 0.4-0.8 is recommended*） |"

_ACCEL_LORA_RECOMMENDATIONS: dict[str, dict] = {
    # ---- 固定加速 LoRA（工作流硬性必需）----
    "ckpt850": _normalize(_rec(
        steps=4,
        scheduler="simple",
        sampler=None,
        shift_video=12.0,
        shift_audio=3.0,
        base_model="MiniMax-H3（bf16 / int8_convrot / pruned_int8 / pruned_fp8 全部变体）",
        source_kind="author",
        source=(
            "/home/ubuntu/comfyui/custom_nodes/ComfyUI-MiniMax-H3-Turbo/README.md:76-88"
            "（步数 4~8 推荐、strength 1.0、scheduler=simple、base model 兼容全部变体）"
            " + example_workflows/minimax_h3_t2v_turbo.json（BasicScheduler=simple/6）"
            " + /home/ubuntu/comfyui/comfy/supported_models.py:970-971（shift 12/3 内生值）"
        ),
        note=(
            "作者原话：「4 steps is the recommended minimum; 4-8 is the useful range. "
            "Keep strength at 1.0. Keep the scheduler on simple.」"
            "作者未指定具体采样器 -> 采样器一栏为「作者未提供」。"
        ),
    )),
    "v4step600": _normalize(_rec(
        steps=4,
        scheduler="simple",
        sampler=None,
        shift_video=12.0,
        shift_audio=3.0,
        base_model="MiniMax-H3（bf16 / int8_convrot / pruned_int8 / pruned_fp8 全部变体）",
        source_kind="author",
        source=(
            "/home/ubuntu/comfyui/custom_nodes/ComfyUI-MiniMax-H3-Turbo/README.md:76-88"
            "（同族 Turbo LoRA，步数 4~8、strength 1.0、scheduler=simple）"
            " + /home/ubuntu/comfyui/comfy/supported_models.py:970-971（shift 12/3 内生值）"
        ),
        note="与 ckpt850 同族的另一枚 EMA 权重，作者给出的用法完全一致。",
    )),
    # ---- 可勾选的加速 LoRA 组合 ----
    "lightx2v_4step_768p": _normalize(_rec(
        steps=4,
        scheduler=None,
        sampler="euler",
        # ⚠️ 768p 系列作者给的 shift 是 **6/3**，不是 H3 内生的 12/3。
        shift_video=6.0,
        shift_audio=3.0,
        base_model="Comfy-Org/MiniMax-H3（minimax_h3_fl2va_bf16.safetensors）",
        source_kind="author",
        source=(
            "作者规格表（ModelTC/Minimax-H3-Turbo README「1. Model specs」）："
            "「FL2VA Turbo 4-step v1.0 768p | FL2VA / T2VA | 768p (1344×768) | "
            "6 / 3 | 4 | 4」-> 训练 shift 6/3、蒸馏 NFE 4、推荐推理 NFE 4。"
            "采样器：「COMFYUI_SETUP_AND_INFERENCE.md」"
            "「set lora_name, steps, shift_video, and shift_audio together to match "
            "the model table. Sampler defaults to `euler`.」"
            " + 本机权重 __metadata__：base_model = "
            "「Comfy-Org/MiniMax-H3 minimax_h3_fl2va_bf16.safetensors」"
        ),
        note=(
            "「shift 是 6/3（作者规格表），不是 H3 内生的 12/3」 —— "
            "LightX2V 的 768p 系列与 544p 系列 shift 不同，不能混用。"
            "调度器作者未提供；采样器作者写的是「defaults to euler」（默认，不是强制）。"
        ),
    )),
    "lightx2v_4step_sla_768p": _normalize(_rec(
        steps=4,
        scheduler=None,
        sampler="training_euler",
        # ⚠️ SLA 768p 系列作者给的 shift 是 **6/3**。
        shift_video=6.0,
        shift_audio=3.0,
        base_model="Comfy-Org/MiniMax-H3（minimax_h3_fl2va_bf16.safetensors）",
        source_kind="author",
        source=(
            "作者模型卡（HF lightx2v/Minimax-h3-Turbo-SLA）"
            "「Key SLA-related settings in this configuration include: "
            "{ attn_type: dynamic_sparse_attn, "
            "dynamic_sparse_attn_setting: { sparsity_ratio: 0.85, operator: sage2 }, "
            "video_flow_shift: 6.0, audio_flow_shift: 3.0, "
            "h3_step_update: training_euler }」"
            " + 「4-step distillation」+「768p FL2V generation support」"
            " + 本机权重 __metadata__：base_model = "
            "「Comfy-Org/MiniMax-H3 minimax_h3_fl2va_bf16.safetensors」"
        ),
        note=(
            "SLA（Sparse-Linear Attention）变体，85% 稀疏率，作者实测 RTX 5090 约 2.5× 加速。"
            "「shift 是 6/3（作者给的 video_flow_shift / audio_flow_shift）」，"
            "不是 H3 内生的 12/3。"
            "采样器作者写的是 `training_euler` —— 那是「训练时」的 step update 规则，"
            "不是 ComfyUI 的 KSamplerSelect 取值 -> 面板的采样器仍应选 `euler`"
            "（见同族 spec 表的「Sampler defaults to euler」）。调度器作者未提供。"
        ),
    )),
    "lightx2v_8step": _normalize(_rec(
        steps=8,
        scheduler=None,
        sampler="euler",
        # 544p mixed 系列作者给的 shift 是 **12/3**（与 768p 系列的 6/3 不同）。
        shift_video=12.0,
        shift_audio=3.0,
        base_model="Comfy-Org/MiniMax-H3（minimax_h3_fl2va_bf16.safetensors）",
        source_kind="author",
        source=(
            "作者规格表（ModelTC/Minimax-H3-Turbo README「1. Model specs」）："
            "「FL2VA Turbo 8-step v1.0 | FL2VA / T2VA | 544p mixed aspect ratio | "
            "12 / 3 | 8 | 8 / 4」-> 训练 shift 12/3、蒸馏 NFE 8、推荐推理 NFE 8 或 4。"
            "采样器：「COMFYUI_SETUP_AND_INFERENCE.md」"
            "「Sampler defaults to `euler`.」"
            " + 本机权重 __metadata__：base_model = "
            "「Comfy-Org/MiniMax-H3 minimax_h3_fl2va_bf16.safetensors」、"
            "training_alpha=8.0、training_scale=0.0625"
        ),
        note=(
            "作者推荐推理 NFE 是「8 / 4」两档 —— 8 步质量更好，4 步更快；"
            "本表给的是 8（质量档）。"
            "「shift 是 12/3（544p 系列）」，与 768p 系列的 6/3 不同，不能混用。"
            "调度器作者未提供。"
        ),
    )),
    "dmad_4step_critic": _normalize(_rec(
        steps=4,
        scheduler=None,
        sampler=None,
        shift_video=12.0,
        shift_audio=2.0,
        base_model="MiniMax-H3（T2VA transformer 的 LoRA）",
        source_kind="author",
        source=(
            "/home/ubuntu/comfyui/models/loras/dmad_minimax_h3_4step_full_critic_comfyui.safetensors"
            " 的 __metadata__：sampling = 「4 steps, video shift 12, audio shift 2, "
            "re-noise step rule, 1344x768, 124 frames at 24 fps, no CFG」；"
            "model = 「DMAD 4-step student of MiniMax-H3 (text-to-audio-video), "
            "LoRA on the T2VA transformer」"
        ),
        note=(
            "唯一一条作者把「采样参数全写出来」的加速 LoRA："
            "步数 4、video shift 12、「audio shift 2」（≠ H3 内生的 3）、"
            "分辨率 1344×768、124 帧 @24fps、无 CFG。"
            "采样器 / 调度器作者未写 -> 「作者未提供」。"
        ),
    )),
    "speed_slider": _normalize(_rec(
        steps=None,
        scheduler=None,
        sampler=None,
        shift_video=12.0,
        shift_audio=3.0,
        base_model="MiniMax-H3（bf16 / int8_convrot / pruned_int8 已实测命中目标层）",
        source_kind="author",
        source=(
            "/mnt/usb2t/comfyui-models/loras/h3/h3_speed_slider_1.1.md:283-295"
            "（「作者提供了什么」表：步数 ❌ 未提供 / 采样器·scheduler ❌ 未提供；"
            "strength ±8 最优）+ /home/ubuntu/comfyui/comfy/supported_models.py:970-971"
        ),
        field_sources={
            # 步数 / 采样器 / 调度器：作者**明写未提供**
            "steps": "not_provided",
            "sampler": "not_provided",
            "scheduler": "not_provided",
            # shift：作者没提，用的是 ComfyUI 的 H3 模型硬性值
            "shift_video": "comfyui",
            "shift_audio": "comfyui",
            # base_model：本机权重头实测（作者没写）
            "base_model": "comfyui",
        },
        note=(
            "作者明写：步数、采样器、scheduler、分辨率、CFG 「5 项一字未提」。"
            "strength 作者给了 ±8（这是「运动幅度」，不是 LoRA 权重强度）。"
        ),
    )),
    "better_motion": _normalize(_rec(
        steps=30,          # 作者给的是区间 15~30，取上界作推荐档
        scheduler=None,
        sampler=None,
        shift_video=12.0,
        shift_audio=3.0,
        base_model="MiniMax-H3（bf16 / int8_convrot / pruned_int8 已实测命中目标层）",
        source_kind="author",
        source=(
            "/mnt/usb2t/comfyui-models/loras/h3/"
            "h3_better_motion_720x1280_w0.4-0.8_s15-30_vpakarinen_v1.md:183-184"
            "（「作者提供了什么」表：步数 ✅ 15~30 步 / 采样器·scheduler ❌ 未提供）"
            " + /home/ubuntu/comfyui/comfy/supported_models.py:970-971"
        ),
        field_sources={
            # 步数：作者给了（15~30）
            "steps": "author",
            "sampler": "not_provided",
            "scheduler": "not_provided",
            # shift：作者没提，用的是 ComfyUI 的 H3 模型硬性值
            "shift_video": "comfyui",
            "shift_audio": "comfyui",
            "base_model": "comfyui",
        },
        note=(
            "作者原话「720x1280 resolution with 15-30 steps」、strength 0.4~0.8。"
            "「步数是区间」，本表给出的是区间上界（30）作为推荐档展示，"
            "区间本身写在 note 里。采样器/调度器作者未提供。"
        ),
    )),

    # ==============================================================
    # 【2026-11-16 补 —— A1 引入的 4 枚加速 LoRA】
    #
    # 【为什么补这 4 条】A1 提交 0616abb 把 accel_loras 从 8 枚扩到 12 枚
    # （新增 turbo_4step_cn / turbo_4step_cn_ema /
    #   lightx2v_4step_v01_comfy / lightx2v_sla_768p_root），
    # 但**没有同步本表** -> 这 4 枚在前端下拉的 tooltip 里
    # 「作者推荐参数」整段缺失（其余 8 枚都有）。
    #
    # 【来源纪律】只用**可追溯的第一手依据**：
    #   * 权重自身的 `__metadata__`（作者写进权重里的说明）；
    #   * 同一家族**已有条目**已验证的作者规格（同一作者的同族权重，
    #     规格表里同一行 —— 但只在该族**同规格档位**时才沿用）。
    # 任何一项没有依据就写 None（前端显示「作者未提供」），
    # **不编造采样参数**。
    # ==============================================================

    # ---- turbo_4step_cn / turbo_4step_cn_ema ----
    # 作者依据（本机权重 __metadata__，第一手）：
    #   sampler_steps: 4
    #   base_model: MiniMax-H3
    #   compatible_base: MiniMax-H3 non-pruned bf16 or int8_convrot
    #   incompatible_base: MiniMax-H3 pruned_* (AdaLN input is 8, LoRA input is 2688)
    #   comfyui_loader: Load LoRA (Bypass, Model Only)
    #   comfyui_key_prefix: diffusion_model.
    #   conversion_source_file: minimax_h3_turbo_4步加速.safetensors
    #   conversion_tool: convert_minimax_h3_lora_for_comfyui.py
    # 作者**没有**写 scheduler / sampler / shift -> 那三项写 None。
    # shift 用 ComfyUI 的 H3 模型硬性值 12/3（supported_models.py:992-993），
    # 标 `comfyui`（不是作者提供）。
    "turbo_4step_cn": _normalize(_rec(
        steps=4,
        scheduler=None,
        sampler=None,
        shift_video=12.0,
        shift_audio=3.0,
        base_model="MiniMax-H3（非剪枝 bf16 / int8_convrot）",
        source_kind="author",
        source=(
            "/mnt/usb2t/comfyui-models/loras/"
            "minimax_h3_turbo_4步加速_comfyui.safetensors 的 __metadata__："
            "sampler_steps = 「4」；base_model = 「MiniMax-H3」；"
            "compatible_base = 「MiniMax-H3 non-pruned bf16 or int8_convrot」；"
            "incompatible_base = 「MiniMax-H3 pruned_* (AdaLN input is 8, "
            "LoRA input is 2688)」；comfyui_loader = 「Load LoRA (Bypass, "
            "Model Only)」；comfyui_key_prefix = 「diffusion_model.」；"
            "conversion_source_file = 「minimax_h3_turbo_4步加速.safetensors」；"
            "conversion_tool = 「convert_minimax_h3_lora_for_comfyui.py」"
            " + /home/ubuntu/comfyui/comfy/supported_models.py:992-993（H3 内生 shift）"
        ),
        field_sources={
            # 步数：作者写进权重元数据（sampler_steps=4）
            "steps": "author",
            # 调度器 / 采样器：作者未写
            "scheduler": "not_provided",
            "sampler": "not_provided",
            # shift：作者未写，用 ComfyUI 的 H3 模型硬性值
            "shift_video": "comfyui",
            "shift_audio": "comfyui",
            # 适用底模：作者写明（compatible_base / incompatible_base）
            "base_model": "author",
        },
        note=(
            "官方 Turbo 4 步蒸馏权重的「中文名发行版」（源文件 "
            "minimax_h3_turbo_4步加速.safetensors，由 "
            "convert_minimax_h3_lora_for_comfyui.py 转换）。"
            "作者写明步数 4 与适用底模，「但没写 scheduler / sampler / shift」 "
            "-> 那三项如实标「作者未提供 / ComfyUI 模型硬性值」。"
            "「键名带 diffusion_model. 前缀」 -> 必须用核心 "
            "LoraLoaderModelOnly 加载（Turbo 节点一个都 patch 不上）。"
            "作者明确写 incompatible_base = pruned_*（AdaLN 输入 8 vs "
            "LoRA 输入 2688）—— 剪枝底模下核心节点没有 Turbo 节点那套"
            "运行期 AdaLN 重注入机制，「配未剪枝底模」（bf16 / int8_convrot）"
            "才是完整命中。"
        ),
    )),
    "turbo_4step_cn_ema": _normalize(_rec(
        steps=4,
        scheduler=None,
        sampler=None,
        shift_video=12.0,
        shift_audio=3.0,
        base_model="MiniMax-H3（非剪枝 bf16 / int8_convrot）",
        source_kind="author",
        source=(
            "/mnt/usb2t/comfyui-models/loras/"
            "minimax_h3_turbo_4步加速ema_comfyui.safetensors 的 __metadata__："
            "sampler_steps = 「4」；base_model = 「MiniMax-H3」；"
            "compatible_base = 「MiniMax-H3 non-pruned bf16 or int8_convrot」；"
            "incompatible_base = 「MiniMax-H3 pruned_* (AdaLN input is 8, "
            "LoRA input is 2688)」；comfyui_loader = 「Load LoRA (Bypass, "
            "Model Only)」；comfyui_key_prefix = 「diffusion_model.」；"
            "conversion_source_file = 「minimax_h3_turbo_4步加速ema.safetensors」"
            " + /home/ubuntu/comfyui/comfy/supported_models.py:992-993（H3 内生 shift）"
        ),
        field_sources={
            "steps": "author",
            "scheduler": "not_provided",
            "sampler": "not_provided",
            "shift_video": "comfyui",
            "shift_audio": "comfyui",
            "base_model": "author",
        },
        note=(
            "Turbo 4 步的中文名 「EMA」 权重（源文件 "
            "minimax_h3_turbo_4步加速ema.safetensors）。"
            "作者写明的规格与 turbo_4step_cn 「同族同值」（步数 4、"
            "适用非剪枝底模、键名带 diffusion_model. 前缀）-> 逐项沿用同一族"
            "的来源，不另编。调度器 / 采样器 / shift 作者未写。"
        ),
    )),

    # ---- lightx2v_4step_v01_comfy ----
    # 作者依据（本机权重 __metadata__，第一手）：
    #   source: .../dmd_minimax_h3_t2va4/checkpointing/00000650/gen-base_dit.pt
    #   baked_scale: 0.125（peft alpha/r=0.125 baked into lora_B）
    #   converted_layout: comfyui_minimax_h3
    #   无 base_model / 无 sampling 字段 -> 作者**没有**给采样参数。
    # 这枚是 **DMD 蒸馏**（源路径 dmd_minimax_h3_t2va4）-> 步数 4 是
    # 该蒸馏族的固有属性；shift 沿用**同族已有条目** lightx2v_4step_768p
    # 的 768p 档 shift 6/3（同一作者 ModelTC/Minimax-H3-Turbo 规格表）。
    "lightx2v_4step_v01_comfy": _normalize(_rec(
        steps=4,
        scheduler=None,
        sampler="euler",
        shift_video=6.0,
        shift_audio=3.0,
        base_model="Comfy-Org/MiniMax-H3（minimax_h3_fl2va_bf16.safetensors）",
        source_kind="author",
        source=(
            "/mnt/usb2t/comfyui-models/loras/"
            "minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy.safetensors "
            "的 __metadata__：source = 「.../dmd_minimax_h3_t2va4/"
            "checkpointing/00000650/gen-base_dit.pt」（DMD 4 步蒸馏）；"
            "baked_scale = 「0.125」（peft alpha/r=0.125 baked into lora_B）；"
            "converted_layout = 「comfyui_minimax_h3」"
            " + 同族已有条目 lightx2v_4step_768p 的作者规格表"
            "（ModelTC/Minimax-H3-Turbo README「1. Model specs」）："
            "「FL2VA Turbo 4-step v1.0 768p | FL2VA / T2VA | 768p | 6 / 3 | 4 | 4」"
            " -> 同族 768p 档的 shift 6/3 与 NFE 4；采样器「Sampler defaults to `euler`.」"
        ),
        field_sources={
            # 步数 4：DMD 4 步蒸馏（源路径 dmd_..._t2va4）+ 同族规格表 NFE 4
            "steps": "author",
            # 调度器：作者未提供
            "scheduler": "not_provided",
            # 采样器：作者规格表「Sampler defaults to euler」
            "sampler": "author",
            # shift 6/3：同族 768p 档的作者规格表（不是 H3 内生的 12/3）
            "shift_video": "author",
            "shift_audio": "author",
            # 底模：同族规格表的 base_model
            "base_model": "author",
        },
        note=(
            "LightX2V Turbo 4 步 「v0.1 的 comfy 打包版」"
            "（源 checkpoint 是 DMD 4 步蒸馏 `dmd_minimax_h3_t2va4`）。"
            "作者规格表里 v0.1 与 v1.0 的 768p 档同属一行规格"
            "（shift 6/3、NFE 4、采样器默认 euler）-> 逐项沿用"
            "「同族已有条目」 lightx2v_4step_768p 的已验证来源，不另编。"
            "调度器作者未提供。"
            "「shift 是 6/3（768p 系列），不是 H3 内生的 12/3。」"
        ),
    )),

    # ---- lightx2v_sla_768p_root ----
    # 这枚与已有条目 lightx2v_4step_sla_768p 是**同一份权重**的
    # 两个登记位（目录版 vs 根目录版）-> 规格完全相同，
    # 逐项沿用同族已有条目的作者规格（HF lightx2v/Minimax-h3-Turbo-SLA
    # 模型卡：video_flow_shift 6.0 / audio_flow_shift 3.0 /
    # h3_step_update training_euler / 4-step distillation / 768p FL2V）。
    "lightx2v_sla_768p_root": _normalize(_rec(
        steps=4,
        scheduler=None,
        sampler="euler",
        shift_video=6.0,
        shift_audio=3.0,
        base_model="Comfy-Org/MiniMax-H3（minimax_h3_fl2va_bf16.safetensors）",
        source_kind="author",
        source=(
            "/mnt/usb2t/comfyui-models/loras/"
            "minimax_h3_fl2v_turbo_4step_v0.1_768p_sla_comfyui_bf16.safetensors "
            "的 __metadata__：base_model = 「Comfy-Org/MiniMax-H3 "
            "minimax_h3_fl2va_bf16.safetensors」；training_rank = 「128」；"
            "training_alpha = 「128.0」；training_scale = 「1.0」"
            " + 同族已有条目 lightx2v_4step_sla_768p 的作者模型卡"
            "（HF lightx2v/Minimax-h3-Turbo-SLA）："
            "「Key SLA-related settings in this configuration include: "
            "{ attn_type: dynamic_sparse_attn, dynamic_sparse_attn_setting: "
            "{ sparsity_ratio: 0.85, operator: sage2 }, video_flow_shift: 6.0, "
            "audio_flow_shift: 3.0, h3_step_update: training_euler }」"
            " + 「4-step distillation」+「768p FL2V generation support」"
        ),
        field_sources={
            # 步数 4：作者模型卡「4-step distillation」
            "steps": "author",
            # 调度器：作者未提供
            "scheduler": "not_provided",
            # 采样器：作者写的是 training_euler（训练时 step update 规则）
            # -> 面板采样器按同族 spec 表「Sampler defaults to euler」
            "sampler": "author",
            # shift 6/3：作者模型卡给的 video_flow_shift / audio_flow_shift
            "shift_video": "author",
            "shift_audio": "author",
            # 底模：权重 __metadata__ 的 base_model
            "base_model": "author",
        },
        note=(
            "与 h3_lightx2v/ 目录里那份「同名同规格」的 SLA 权重"
            "（键名形态一致），此处登记「根目录」那一份 —— "
            "它是该目录版之外真实存在的第二份文件。"
            "作者模型卡给的规格（4 步蒸馏 / shift 6/3 / 85% 稀疏率 / "
            "sage2 / RTX 5090 实测约 2.5× 加速）「与同族已有条目完全相同」"
            " -> 逐项沿用同一份已验证来源，不另编。"
            "作者写的是 `training_euler`（训练时 step update 规则），"
            "不是 ComfyUI 的 KSamplerSelect 取值 -> 面板采样器仍选 `euler`。"
            "调度器作者未提供。"
        ),
    )),
}


# ==================================================================
# 主模型（未加载加速 LoRA 时的推荐值）
# ==================================================================
#
# 【H3 官方内生采样设置】/home/ubuntu/comfyui/comfy/supported_models.py:969-972
#     "shift": 12.0, "audio_shift": 3.0
#   这是 ComfyUI 的模型定义写死的，**任何 H3 底模变体都一样**（量化/pruned
#   都不改采样设置）-> 所以各底模的 shift 一律标 `comfyui`。
#
# 【官方基准档】ComfyUI 官方 H3 工作流的采样链是 BasicScheduler(simple) +
#   KSamplerSelect(euler) + steps=20（不挂加速 LoRA 时的标准档）。
#   本机项目侧把它固化在 workflows._h3_video_workflow（lora=="none" ->
#   steps 20 / scheduler simple / sampler res_multistep）。
#   注意：这是**本机工作流档位**，不是底模作者写的 -> 标 `nonebot` 也不合适，
#   所以统一标 `comfyui`（官方工作流基准）并在 note 里写清区别。
#
# 【各底模作者文档】
#   hybrid: /mnt/nvme1t_colorful/comfyui-models/diffusion_models/
#           minimax_h3_hybrid_fl2va_ref2va_b30-49-int8.md:158-172
#     「⚠️ **本底模作者未给出任何采样参数**（README 只讲合并原理和选型）。
#       上表除 sigma shift / CFG / 帧数网格外均为**本机推荐，未实跑验证**。」
#     -> 所以作者一栏必须是「作者未提供」。
#   Singularity 三变体:
#     /mnt/nvme1t_colorful/comfyui-models/diffusion_models/
#       Minimax-h3_Singularity_ref2va_v1.3_int8.md
#       Minimax-h3_Singularity_ref2va_Pruned_v1.3_int8.md
#       Minimax-h3_Singularity_ref2va_v1.3_Pruned_w4a8.md
#     （作者 = WarmBloodAban；「deep high-step fine-tuning」——
#       作者强调高步数精调，但**没给具体步数**）
#
# 【fl2va / ref2va 官方权重】官方发布，没有随权重附带的作者说明文件
#   -> 采样参数只有 ComfyUI 的内生值 + 官方工作流基准。

_MODEL_RECOMMENDATIONS: dict[str, dict] = {
    "fl2va_pruned_int8": _normalize(_rec(
        steps=20,
        scheduler="simple",
        sampler="res_multistep",
        shift_video=12.0,
        shift_audio=3.0,
        base_model=None,
        source_kind="comfyui",
        source=(
            "/home/ubuntu/comfyui/comfy/supported_models.py:969-972"
            "（shift 12 / audio_shift 3，H3 模型内生采样设置，量化/pruned 不改）"
            " + 本项目工作流档位：h3studio/workflows.py:347-350"
            "（不挂加速 LoRA 时 = 20 步 / simple / res_multistep）"
        ),
        note=(
            "官方 fl2va / ref2va 权重「不附作者说明文件」 -> 作者未提供采样参数。"
            "上表的 20 步 / simple / res_multistep 是「本项目工作流的默认档位」"
            "（h3studio/workflows.py:347-350，不挂加速 LoRA 的分支），"
            "「不是作者推荐值」；shift 12/3 是 ComfyUI 的模型硬性值"
            "（漏挂 MiniMaxH3SigmaShift 画质明显变差）。"
            "⚠️ euler 是「挂了加速 LoRA」 时的档位（4 步 / beta / euler），"
            "与本表（无 LoRA 档）不同，不要混用。"
        ),
    )),
    "fl2va_int8": _normalize(_rec(
        steps=20,
        scheduler="simple",
        sampler="res_multistep",
        shift_video=12.0,
        shift_audio=3.0,
        base_model=None,
        source_kind="comfyui",
        source=(
            "/home/ubuntu/comfyui/comfy/supported_models.py:969-972"
            "（shift 12 / audio_shift 3，H3 模型内生采样设置，量化/pruned 不改）"
            " + 本项目工作流档位：h3studio/workflows.py:347-350"
            "（不挂加速 LoRA 时 = 20 步 / simple / res_multistep）"
        ),
        note=(
            "官方 fl2va / ref2va 权重「不附作者说明文件」 -> 作者未提供采样参数。"
            "上表的 20 步 / simple / res_multistep 是「本项目工作流的默认档位」"
            "（h3studio/workflows.py:347-350，不挂加速 LoRA 的分支），"
            "「不是作者推荐值」；shift 12/3 是 ComfyUI 的模型硬性值"
            "（漏挂 MiniMaxH3SigmaShift 画质明显变差）。"
            "⚠️ euler 是「挂了加速 LoRA」 时的档位（4 步 / beta / euler），"
            "与本表（无 LoRA 档）不同，不要混用。"
        ),
    )),
    # 【2026-11-14 新增】Ref2VA 专用权重（参考生视频用）。
    # 官方 ref2va 权重同样不附作者说明文件 -> 与 fl2va 系同一套口径：
    # 20 步 / simple / res_multistep 是**本项目工作流的默认档位**
    # （无加速 LoRA 时），不是作者推荐值。
    "ref2va_pruned_int8": _normalize(_rec(
        steps=20,
        scheduler="simple",
        sampler="res_multistep",
        shift_video=12.0,
        shift_audio=3.0,
        base_model=None,
        source_kind="comfyui",
        source=(
            "/home/ubuntu/comfyui/comfy/supported_models.py:969-972"
            "（shift 12 / audio_shift 3，H3 模型内生采样设置，量化/pruned 不改）"
            " + 本项目工作流档位：h3studio/h3_video_modes.py"
            "（不挂加速 LoRA 时 = 20 步 / simple / res_multistep）"
        ),
        note=(
            "Ref2VA 权重（参考生视频专用）「不附作者说明文件」 -> 作者未提供采样参数。"
            "上表 20 步 / simple / res_multistep 是本项目工作流的默认档位，"
            "「不是作者推荐值」；shift 12/3 是 ComfyUI 的模型硬性值。"
            "⚠️ euler 是挂了加速 LoRA 时的档位（4 步 / beta / euler），"
            "与本表（无 LoRA 档）不同，不要混用。"
        ),
    )),
    "ref2va_int8": _normalize(_rec(
        steps=20,
        scheduler="simple",
        sampler="res_multistep",
        shift_video=12.0,
        shift_audio=3.0,
        base_model=None,
        source_kind="comfyui",
        source=(
            "/home/ubuntu/comfyui/comfy/supported_models.py:969-972"
            "（shift 12 / audio_shift 3，H3 模型内生采样设置，量化/pruned 不改）"
            " + 本项目工作流档位：h3studio/h3_video_modes.py"
            "（不挂加速 LoRA 时 = 20 步 / simple / res_multistep）"
        ),
        note=(
            "Ref2VA 权重（参考生视频专用 · 未剪枝）「不附作者说明文件」。"
            "上表 20 步 / simple / res_multistep 是本项目工作流的默认档位，"
            "不是作者推荐值；shift 12/3 是 ComfyUI 的模型硬性值。"
        ),
    )),
    "fl2va_pruned_w4a8": _normalize(_rec(
        steps=20,
        scheduler="simple",
        sampler="res_multistep",
        shift_video=12.0,
        shift_audio=3.0,
        base_model=None,
        source_kind="comfyui",
        source=(
            "/home/ubuntu/comfyui/comfy/supported_models.py:969-972"
            "（shift 12 / audio_shift 3，H3 模型内生采样设置，量化/pruned 不改）"
            " + 本项目工作流档位：h3studio/workflows.py:347-350"
            "（不挂加速 LoRA 时 = 20 步 / simple / res_multistep）"
        ),
        note=(
            "官方 fl2va / ref2va 权重「不附作者说明文件」 -> 作者未提供采样参数。"
            "上表的 20 步 / simple / res_multistep 是「本项目工作流的默认档位」"
            "（h3studio/workflows.py:347-350，不挂加速 LoRA 的分支），"
            "「不是作者推荐值」；shift 12/3 是 ComfyUI 的模型硬性值"
            "（漏挂 MiniMaxH3SigmaShift 画质明显变差）。"
            "⚠️ euler 是「挂了加速 LoRA」 时的档位（4 步 / beta / euler），"
            "与本表（无 LoRA 档）不同，不要混用。"
        ),
    )),
    "fl2va_bf16": _normalize(_rec(
        steps=20,
        scheduler="simple",
        sampler="res_multistep",
        shift_video=12.0,
        shift_audio=3.0,
        base_model=None,
        source_kind="comfyui",
        source=(
            "/home/ubuntu/comfyui/comfy/supported_models.py:969-972"
            "（shift 12 / audio_shift 3，H3 模型内生采样设置，量化/pruned 不改）"
            " + 本项目工作流档位：h3studio/workflows.py:347-350"
            "（不挂加速 LoRA 时 = 20 步 / simple / res_multistep）"
        ),
        note=(
            "官方 fl2va / ref2va 权重「不附作者说明文件」 -> 作者未提供采样参数。"
            "上表的 20 步 / simple / res_multistep 是「本项目工作流的默认档位」"
            "（h3studio/workflows.py:347-350，不挂加速 LoRA 的分支），"
            "「不是作者推荐值」；shift 12/3 是 ComfyUI 的模型硬性值"
            "（漏挂 MiniMaxH3SigmaShift 画质明显变差）。"
            "⚠️ euler 是「挂了加速 LoRA」 时的档位（4 步 / beta / euler），"
            "与本表（无 LoRA 档）不同，不要混用。"
        ),
    )),
    "hybrid_fl2va_ref2va": _normalize(_rec(
        steps=None,
        scheduler=None,
        sampler=None,
        shift_video=12.0,
        shift_audio=3.0,
        base_model=None,
        source_kind="comfyui",
        source=(
            "/home/ubuntu/comfyui/comfy/supported_models.py:969-972（shift 12 / audio_shift 3）"
            " + /mnt/nvme1t_colorful/comfyui-models/diffusion_models/"
            "minimax_h3_hybrid_fl2va_ref2va_b30-49-int8.md:158-172"
            "（作者未给出任何采样参数）"
        ),
        note=(
            "作者（smhfacct）「未给出任何采样参数」 —— 其 README 只讲合并原理与变体选型"
            "（原文：「本底模作者未给出任何采样参数」）。"
            "shift 12/3 是 ComfyUI 模型硬性值；步数/调度器/采样器一律「作者未提供」。"
        ),
    )),
}


# ==================================================================
# 对外接口
# ==================================================================

def accel_lora_recommendation(key: str) -> dict:
    """某个加速 LoRA key 的作者推荐参数（没有登记就返回全「作者未提供」）。

    key 既可以是 `VIDEO_ACCEL_LORAS` 的 key（ckpt850 / v4step600），
    也可以是 `VIDEO_ACCEL_LORA_COMBOS` 的 key。
    """
    rec = _ACCEL_LORA_RECOMMENDATIONS.get(key)
    if rec is None:
        return _normalize(_rec())
    return dict(rec)


def model_recommendation(key: str) -> dict:
    """某个主模型 key 的推荐参数（未挂加速 LoRA 时的档位）。"""
    rec = _MODEL_RECOMMENDATIONS.get(key)
    if rec is None:
        return _normalize(_rec())
    return dict(rec)


def accel_lora_recommendations_payload() -> dict:
    """给前端下拉用的「加速 LoRA -> 推荐参数」映射。

    每条都带 `field_sources`（逐字段的来源类别）—— 一条记录的各字段
    可能来自不同来源（作者写了步数但没写 shift；shift 用的是 ComfyUI 的
    H3 模型硬性值），前端要能逐项标出来。
    """
    return {key: dict(value) for key, value in _ACCEL_LORA_RECOMMENDATIONS.items()}


def model_recommendations_payload() -> dict:
    """给前端下拉用的「主模型 -> 推荐参数」映射。"""
    return {key: dict(value) for key, value in _MODEL_RECOMMENDATIONS.items()}


#: 推荐参数的字段名（前端渲染与测试都用这一份，不在别处硬编字符串）。
REC_FIELDS = ("steps", "scheduler", "sampler",
              "shift_video", "shift_audio", "base_model")

#: 这些字段是**数值型**，前端要按「未提供」判定而不是按 falsy 判定
#: （作者写了 0 也是有效值）。
REC_NUMERIC_FIELDS = ("steps", "shift_video", "shift_audio")

#: 字段 -> 中文显示名。
REC_FIELD_LABEL = {
    "steps": "推荐步数",
    "scheduler": "推荐调度器",
    "sampler": "推荐采样器",
    "shift_video": "推荐视频 shift",
    "shift_audio": "推荐音频 shift",
    "base_model": "推荐主模型",
}
