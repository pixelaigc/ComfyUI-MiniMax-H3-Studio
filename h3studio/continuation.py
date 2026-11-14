# -*- coding: utf-8 -*-
"""
文生视频「接续生成」的**协议层**：潜空间落盘 / 读取 / 清理 / 帧数换算。

【接续是什么】
把上一段生成的 **AV latent**（潜空间）落盘，下一段生成时读回来，
把它的尾部逐 token 拷进本段初始 latent 的开头，并用 ComfyUI 原生的
`minimax_keyframes` 协议把同一段内容钉成条件行 —— 这样模型读到的
是**真实的上一段运动**，而不是「猜」出来的相似画面。

这不是像素续接（上一段 mp4 → 解码 → VAE 重编码 → 当锚图）。
重编码会在每一段接缝处掉一次画质、偏一次色档；潜空间桥不碰像素。

【为什么用 Relay-Kit 的节点，而不是自己实现】
ComfyUI-H3-Relay-Kit 已经把「零重编码 latent 桥」做完了，并且踩过坑：
  * `H3RelayLatentSave` / `H3RelayLatentLoad` —— 落盘 / 读回 AV latent
  * `H3RelayCopyBridge` —— 把上段尾段**逐位拷贝**进本段 latent 开头 + 附噪声掩码
    （掩码 0 区每步被钉回拷贝内容 ⇒ 钉住区零重绘 ⇒ 复现发糊/漂移从机制上消失）
  * `H3RelayTrimAV` —— 裁掉钉住区（decode 之后的**像素帧**，音画同裁）
我们**不重新实现**这些，只负责：
  1. 决定「哪些产物可接续」；
  2. 把接续上下文（配置/参数/模型/素材）回填给前端；
  3. 算清帧数账（钉住多少帧、裁多少帧、拼出来多长）；
  4. 在工作流里把上面的节点接对。

【关键帧数约定（全部来自本机实测，不是推的）】
  * H3 段长走 **17k+5** 网格：5 / 22 / 39 / … / 124 / 243 / 362 像素帧。
    来源：comfy_extras/nodes_minimax_h3.py `align_frame_count`。
  * 视频 latent 的时间步数 `video_latent_t(fc) = 2 if fc<=5 else (fc-5)//17*5 + 2`。
  * 每个 latent 步覆盖的像素帧按 **(1,4,4,4,4)** 循环
    （Relay-Kit `relay_core.FRAME_PER_TOKEN`），5 步一个周期 = 17 帧。
  * 钉住窗口（`context_frames`）只认 **5+17k** 网格：5/22/39/56/73/90/107/124。
    22 帧 = 7 个 latent 步。
  * 尾段切片的起始步必须落在 **5 步周期边界**（start % 5 == 0），否则
    各 token 的帧跨度与写入位置错位 → 渲染出**整体位移的接缝**。
    段长 17k+5 时，`total_steps - window_steps` 恒为 5 的倍数 ⇒ 天然对齐。

【selflift（二采）为什么不能接续】
二采走 `SelfLiftH3Sampler`：低分辨率一采 → 抬升 → 高分辨率二采。
它的输出 latent 是**两段不同分辨率**拼出来的，且节点内部做了
`seed + 1` 重新加噪，潜空间相位与标准采样路径不同源。
把这种 latent 当 `context_latent` 喂给拷贝桥，切出来的尾段帧跨度
与写入位置对不上 —— 表现为整体位移的接缝。
所以：**二采产物一律不显示「接续」按钮**，后端也直接拒绝。

【本模块不做 IO 的部分】
帧数换算、可用性判定、上下文组装都是**纯函数** —— 不查盘、不连 ComfyUI。
这样单元测试可以完全离线跑（与 job_meta.py 同一纪律）。
"""

from __future__ import annotations

import os
from pathlib import Path

# ------------------------------------------------------------------
# 常量（与本机 ComfyUI / Relay-Kit 源码一一对应）
# ------------------------------------------------------------------

#: H3 视频 VAE 的 latent 时间步覆盖的像素帧数（按步序循环）。
#: 来源：Relay-Kit relay_core.FRAME_PER_TOKEN = (1, 4, 4, 4, 4)
FRAME_PER_TOKEN = (1, 4, 4, 4, 4)

#: H3 固定帧率。
FPS = 24

#: 合法钉住窗口（5+17k 网格）。来源：Relay-Kit relay_core.GUIDE_RUNS
GUIDE_RUNS = (124, 107, 90, 73, 56, 39, 22, 5)

#: 生产档钉住窗口：22 帧 ≈ 0.92s。
#: 来源：Relay-Kit README §6「默认 22（只认 5+17k 网格）」+ 节点 tooltip
#: 「22 is nearly seamless」。
DEFAULT_CONTEXT_FRAMES = 22

#: 可接续的生成模式。
#:
#: 目前**只有 t2v**：接续桥接在 h3studio/video_workflow.py 的
#: `build_t2v_panel` 里（文生视频新面板）。flf / r2v 走的是
#: workflows.py 里的旧工作流路径（`_h3_video_workflow` / `build_r2v`），
#: 那两条路**还没有**接桥 —— 硬说它们可接续会导致点了之后静默
#: 退化成普通生成（最坏的一种失败：看起来成功，其实没接上）。
#:
#: 所以这里只列真正接了桥的模式。将来给 flf / r2v 补上桥之后，
#: 把它们加进这个元组即可（用户需求里写明「未来参考生视频/
#: 首末帧生视频也应可接续」）。
#:
#: selflift（二采）是**模式内的开关**，不是独立 mode，
#: 所以排除逻辑在 mode_supported() 之外还要看 params["twopass"]["enabled"]。
CONTINUABLE_MODES = ("t2v",)

# ------------------------------------------------------------------
# 帧数换算（纯函数）
# ------------------------------------------------------------------
def pixel_frames(latent_steps: int) -> int:
    """latent 时间步数 -> 覆盖的像素帧数。

    每个 latent 步覆盖的帧数按 (1,4,4,4,4) 循环，5 步一周期 = 17 帧。
    与 Relay-Kit `relay_core.pixel_frames` 同语义。
    """
    steps = max(0, int(latent_steps))
    return sum(FRAME_PER_TOKEN[k % 5] for k in range(steps))


def steps_for_frames(frames: int) -> int | None:
    """像素帧数 -> 整步覆盖它所需的 latent 步数。

    返回 None 表示这个帧数**不是整步窗口**（切不出来 —— 切出来会错位）。
    与 Relay-Kit `relay_core.steps_for_frames` 同语义。
    """
    want = int(frames)
    if want < 0:
        return None
    steps, covered = 0, 0
    while covered < want:
        covered += FRAME_PER_TOKEN[steps % 5]
        steps += 1
    return steps if covered == want else None


def align_frame_count(length: int) -> int:
    """段长（帧）-> H3 的 17k+5 网格帧数。

    来源：comfy_extras/nodes_minimax_h3.py `align_frame_count`。
    """
    n = max(5, int(length))
    if n % 17 == 5:
        return n
    return n + (5 - (n % 17)) % 17


def video_latent_t(frame_count: int) -> int:
    """帧数 -> 视频 latent 的时间步数。

    来源：comfy_extras/nodes_minimax_h3.py `video_latent_t`：
        2 if fc <= 5 else (fc - 5) // 17 * 5 + 2
    """
    fc = int(frame_count)
    if fc <= 5:
        return 2
    return ((fc - 5) // 17) * 5 + 2


def valid_context_frames(frames: int) -> bool:
    """这个钉住窗口能不能整步切出来（5+17k 网格）。"""
    value = int(frames)
    return value in GUIDE_RUNS


def snap_context_frames(frames: int) -> int:
    """把任意帧数吸附到**不大于它**的合法窗口（5+17k 网格）。

    没有合法窗口 <= frames 时返回 0（表示「无法接续」）。
    """
    value = int(frames)
    best = 0
    for candidate in GUIDE_RUNS:
        if candidate <= value and candidate > best:
            best = candidate
    return best


def tail_aligned(frame_count: int, window: int) -> bool:
    """尾段切片的起始步是否落在 5 步周期边界。

    不对齐 => 各 token 的帧跨度与写入位置错位 => 整体位移的接缝。
    """
    total = video_latent_t(int(frame_count))
    steps = steps_for_frames(int(window))
    if steps is None or steps > total:
        return False
    return (total - steps) % 5 == 0


def continuation_frames(frame_count: int, window: int = DEFAULT_CONTEXT_FRAMES) -> dict:
    """算清一段接续的**帧数账**。

    返回：
      {
        "frame_count":     本段总帧数（17k+5 网格）,
        "latent_steps":    本段 latent 步数,
        "context_frames":  实际钉住帧数（窗口被吸附过）,
        "context_steps":   钉住区占几个 latent 步,
        "trim_frames":     decode 后要裁掉的帧数（= 钉住帧数）,
        "delivered_frames":本段交付给用户的帧数（总帧数 - 裁掉）,
        "total_seconds":   本段交付时长（秒）,
      }
    """
    fc = align_frame_count(int(frame_count))
    win = snap_context_frames(int(window))
    if win <= 0:
        win = 0
    steps = steps_for_frames(win) if win else 0
    delivered = fc - win if win else fc
    return {
        "frame_count": fc,
        "latent_steps": video_latent_t(fc),
        "context_frames": win,
        "context_steps": steps or 0,
        # 裁的是 decode 之后的**像素帧**：钉住区是模型对上一段尾部的重生成，
        # 不裁就会在拼接处看到约 0.9s 的重播（Relay-Kit 节点注释）。
        "trim_frames": win,
        "delivered_frames": delivered,
        "total_seconds": round(delivered / FPS, 3),
    }


# ------------------------------------------------------------------
# 可接续性判定
# ------------------------------------------------------------------
def selflift_enabled(params: dict) -> bool:
    """这个任务是不是走了二采（SelfLift）路径。

    二采的潜空间不支持接续（见模块 docstring）。
    """
    twopass = (params or {}).get("twopass")
    if isinstance(twopass, dict) and twopass.get("enabled"):
        return True
    return bool((params or {}).get("selflift"))


def mode_supported(mode: str) -> bool:
    """这个生成模式本身支持接续吗。

    目前只支持文生视频系（t2v / flf / r2v）。图像、音乐不支持
    —— 它们没有 AV latent 概念。
    """
    return str(mode or "") in CONTINUABLE_MODES


# ------------------------------------------------------------------
# 接续的几何约束（分辨率必须一致）
# ------------------------------------------------------------------
def _size_of(params: dict) -> tuple[int, int]:
    """取一个任务的输出尺寸（宽, 高）。取不到就返回 (0, 0)。

    【为什么取不到就当「不知道」而不是「不匹配」】上层要区分
    「明确不一致」与「无从比较」—— 后者不该拦用户。
    """
    src = params or {}
    try:
        width = int(src.get("width") or 0)
        height = int(src.get("height") or 0)
    except (TypeError, ValueError):
        return 0, 0
    if width <= 0 or height <= 0:
        return 0, 0
    return width, height


def size_mismatch(source_params: dict, target_params: dict) -> str:
    """接续时上游与本段的输出尺寸不一致 —— 返回中文原因（空串 = 一致/未知）。

    【为什么这是硬约束】潜空间桥是**逐 token 拷贝**上一段的尾部，
    latent 的空间分辨率固定。跨分辨率拷贝在 Relay-Kit 里直接 raise
    （relay_core.py:2809「拷贝桥禁止跨分辨率：上段 WxH ≠ 本段 WxH」）。

    【为什么要在提交前拦】让用户等一次生成（几十秒）才看到失败，
    是把「参数冲突」伪装成「生成失败」。而且这条错误过去还会被
    错误文本提取的缺陷吞掉 —— 用户只看到一句 Python 报错。
    """
    src_w, src_h = _size_of(source_params)
    dst_w, dst_h = _size_of(target_params)
    if not src_w or not dst_w:
        # 无从比较 -> 放行（不臆造约束）
        return ""
    if (src_w, src_h) == (dst_w, dst_h):
        return ""
    return (f"接续失败：上一段是 {src_w}×{src_h}，本段是 {dst_w}×{dst_h} —— "
            f"潜空间桥不能跨分辨率接续（逐 token 拷贝上一段尾部，"
            f"latent 分辨率必须一致）。请把输出尺寸改成 {src_w}×{src_h}，"
            f"或改用「复用参数」重新生成一段。")


def continuation_allowed(mode: str, params: dict) -> tuple[bool, str]:
    """这个产物能不能被接续？返回 (能不能, 不能的原因)。

    原因文案是**给用户看的**（直接显示在卡片/提示里），要具体、
    不猜。前端拿不到这个原因就只会显示一个空的接续按钮。
    """
    if not mode_supported(mode):
        return False, "只有视频类产物支持接续。"
    if selflift_enabled(params):
        return False, ("二采（SelfLift 渐进分辨率采样）的潜空间与标准采样路径不同源，"
                       "接续会出现接缝位移 —— 二采产物不支持接续。")
    return True, ""


# ------------------------------------------------------------------
# 落盘路径（纯路径计算，不做 IO）
# ------------------------------------------------------------------
def sanitize_run_id(job_id: str) -> str:
    """任务 id -> 安全的目录名。

    与 Relay-Kit `_stage_path` 同一套规则：非法字符换成 `_`（而不是删除），
    否则 "a/b" 与 "ab" 会静默撞进同一目录。
    """
    raw = str(job_id or "")
    rid = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in raw)
    return rid or "unknown"


# ------------------------------------------------------------------
# 链（chain）与 Relay-Kit 的 run_id
# ------------------------------------------------------------------
def chain_run_id(chain_id: str) -> str:
    """链 id -> Relay-Kit 的 run_id（落盘目录名）。

    加 `h3studio_` 前缀：Relay-Kit 的 run_id 会直接变成
    `output/relay_kit/<run_id>/` 目录 —— 不加前缀会和用户手工跑
    Relay-Kit 建的目录（例如 `fionia_base`）混在一起。

    ⚠️ **这个函数只用于「链首段」**（第 0 段的外观锚）。
    第 1 段起必须用 segment_run_id（每段一个目录）—— 见那里的说明。
    """
    return f"h3studio_{sanitize_run_id(chain_id)}"


def segment_run_id(job_id: str) -> str:
    """**一个段**的 Relay-Kit run_id（落盘目录名）。

    【为什么第 1 段起必须按段分目录，而不是「一条链一个目录」】

    用户需求第 7 条：「用户可以继续对这条完整视频再点接续，实现超长视频
    连续生成」。这意味着**同一条链可以被分叉**：对同一段点两次「接续」，
    就得到两个兄弟分支 B1 / B2。

    如果「一条链一个目录 + 段号」，B1 和 B2 的落盘坐标
    `(run_id=链id, stage_index=1)` **完全相同** —— 两者写同一个
    `stage_00001.safetensors`，后完成者覆盖先完成者。

    后果不是「报错」，而是**静默的错误内容**：再对 B1 接续时，
    读回的 `stage_00001` 已经是 B2 的潜空间，于是新一段实际接的是
    兄弟分支的画面（实测灰度 MAE：C1首帧 vs B1末帧 = 38.65，
    vs B2末帧 = 2.08 —— 偏离 18.6 倍），而界面全程显示成功。

    所以落盘坐标必须与**产出它的那一段**一一对应：
    每个段用自己的任务 id 派生一个专属目录，谁也覆盖不了谁。

    【与 nonebot「咏唱 h3」参考实现的差别】
    咏唱 h3 用 `run_id = long_id`（一部片子一个目录）+ 长视频级互斥锁
    防覆盖 —— 因为它的链是**线性**的（只允许从第 N 段接第 N+1 段，
    并且禁止并发跑同一个长视频）。本项目的链**允许分叉**，所以不能
    照搬「一个目录 + 段号」：互斥锁挡不住「先后两次」的分叉。

    目录名带 `h3studio_s` 前缀，区别于链首段的 `h3studio_`，
    且与用户手工跑 Relay-Kit 建的目录不撞。
    """
    return f"h3studio_s{sanitize_run_id(job_id)}"


# ------------------------------------------------------------------
# 潜空间坐标（run_id + 文件里的段号）
# ------------------------------------------------------------------
def latent_coords(info: dict, job_id: str = "") -> tuple[str, int]:
    """一段的潜空间坐标：`(run_id, 文件里的段号)`。

    【为什么坐标是「两元组」而不是一个段号】
    Relay-Kit 的落盘路径是 `<output>/relay_kit/<run_id>/stage_<N>.safetensors`
    —— N 由节点自己算（`H3RelayLatentLoad` 读第 `stage_index-1` 段，
    `H3RelayLatentSave` 写第 `stage_index` 段）。所以「读回哪一份」
    要同时知道**目录**和**段号**，少一个都读错。

    【两种坐标制度】
      * 新任务（`latent_run_id` 字段存在）—— **每段一个目录**，
        段号恒为 0。目录名由该段自己的任务 id 派生，
        所以兄弟分支各写各的目录，**结构上不可能互相覆盖**。
      * 上线前的旧任务（没有 `latent_run_id`）—— 沿用当时的
        「一条链一个目录 + 链段号」，以便继续接续老产物
        （老产物的潜空间文件就在那个位置，不能假装它在新地方）。

    这条函数是**唯一**的真源：工作流拼装（读回/落盘）与服务层
    （存在性检查 / 错误提示）都必须调它 —— 两边各算一次必然漂移，
    而漂移的后果是「写到 A 目录、从 B 目录读」= 静默接错段。
    """
    src = info or {}
    run_id = str(src.get("latent_run_id") or "").strip()
    if run_id:
        try:
            stage = int(src.get("latent_stage", 0))
        except (TypeError, ValueError):
            stage = 0
        return run_id, max(0, stage)
    # 旧任务：一条链一个目录 + 链上的段号
    chain_id = str(src.get("chain_id") or "").strip()
    if not chain_id:
        return "", 0
    try:
        stage = int(src.get("stage_index", 0))
    except (TypeError, ValueError):
        stage = 0
    return chain_run_id(chain_id), max(0, stage)


# ------------------------------------------------------------------
# 上下文组装（把「该回填什么」算清楚）
# ------------------------------------------------------------------
#: 回填给左侧配置区的字段 —— **不含 prompt**。
#:
#: 用户需求：「把该视频所需的全部内容/配置/参数/模型/素材（提示词除外）
#: 自动回填到左侧配置区」。提示词由用户自己输入下一段的。
#:
#: 列表顺序 = 前端展示顺序（也是回填时的处理顺序）。
CONTEXT_FIELDS = (
    "model_key",
    "fixed_accel_lora",
    "accel_loras",
    "attention",
    "twopass",
    "width",
    "height",
    "aspect",
    "duration",
    "steps",
    "sampler",
    "scheduler",
    "shift_video",
    "shift_audio",
    "cfg",
    "seed",
)


def build_context(params: dict, mode: str = "t2v") -> dict:
    """把任务的落库参数组装成「接续上下文」（**不含 prompt**）。

    返回的字典就是前端回填用的**唯一数据源**：
      {
        "params":   {字段: 值}   —— 直接回填到左侧配置区,
        "prompt":  ""            —— 恒为空：提示词由用户输入下一段,
        "summary": [ {label, value}, ... ]  —— 只读展示用,
      }

    【为什么提示词恒为空】用户需求明确「用户只需输入下一段提示词即可
    接续生成」—— 上一段的提示词对下一段没有意义，回填它反而会让用户
    以为「改了再生成」而不是「写下一段」。
    """
    src = params or {}
    context_params: dict = {}
    for field in CONTEXT_FIELDS:
        if field not in src:
            continue
        value = src[field]
        # 二采相关字段在二采关闭时没有意义 —— 但保留原值即可，
        # 反正前端回填的是「这个任务当时用的配置」。
        context_params[field] = value

    # 尺寸预设键：文生视频面板用 size_preset_key 表示「用了哪个预设」。
    # 自定义尺寸时它是空串 —— 保留空串（前端据此切到自定义）。
    if "size_preset_key" in src:
        context_params["size_preset_key"] = src["size_preset_key"]

    return {
        "params": context_params,
        "prompt": "",
        "summary": summarize_context(context_params),
    }


#: 摘要项的显示名（与服务端下发的字段名一一对应）。
_CONTEXT_LABELS = {
    "model_key": "模型",
    "fixed_accel_lora": "固定加速 LoRA",
    "accel_loras": "加速 LoRA",
    "attention": "注意力机制",
    "twopass": "二采",
    "width": "宽度",
    "height": "高度",
    "aspect": "画面比例",
    "duration": "时长",
    "steps": "采样步数",
    "sampler": "采样器",
    "scheduler": "调度器",
    "shift_video": "shift（视频）",
    "shift_audio": "shift（音频）",
    "cfg": "CFG",
    "seed": "种子",
}


def summarize_context(context_params: dict) -> list[dict]:
    """接续上下文的只读摘要（给人看的那一份）。

    显示名用**人话**，但取值必须**原样透传**（不翻译、不改单位）——
    前端要把它显示成「回填进来的就是这些」，所以摘要只负责排版。
    """
    from . import job_meta
    from . import video_models as vmodels

    out: list[dict] = []
    params = context_params or {}

    model_key = str(params.get("model_key") or "")
    if model_key:
        out.append({"label": "模型", "value": job_meta._video_model_label(model_key)})

    fixed_key = str(params.get("fixed_accel_lora") or "")
    if fixed_key:
        out.append({"label": "固定加速 LoRA",
                    "value": job_meta._fixed_accel_lora_display(fixed_key)})

    for item in params.get("accel_loras") or []:
        if not isinstance(item, dict) or item.get("enabled") is False:
            continue
        key = str(item.get("key") or "")
        if not key:
            continue
        try:
            strength = float(item.get("strength", 1.0))
        except (TypeError, ValueError):
            strength = 1.0
        out.append({"label": "加速 LoRA",
                    "value": f"{job_meta._accel_lora_display(key)} ×{strength:.2f}"})
        break

    attention = str(params.get("attention") or "")
    if attention:
        meta = vmodels.VIDEO_ATTENTION_MODES.get(attention) or {}
        out.append({"label": "注意力机制",
                    "value": str(meta.get("label") or attention)})

    twopass = params.get("twopass")
    if isinstance(twopass, dict):
        out.append({"label": "二采",
                    "value": "开启" if twopass.get("enabled") else "关闭"})

    width, height = params.get("width"), params.get("height")
    if width and height:
        out.append({"label": "输出尺寸", "value": f"{width}×{height}"})

    aspect = str(params.get("aspect") or "")
    if aspect:
        out.append({"label": "画面比例", "value": aspect})

    duration = params.get("duration")
    if duration:
        out.append({"label": "时长", "value": f"{duration}s"})

    steps = params.get("steps")
    if steps:
        out.append({"label": "采样步数", "value": str(steps)})

    sampler = str(params.get("sampler") or "")
    if sampler:
        out.append({"label": "采样器", "value": sampler})

    scheduler = str(params.get("scheduler") or "")
    if scheduler:
        out.append({"label": "调度器", "value": scheduler})

    shift_video = params.get("shift_video")
    if shift_video is not None:
        out.append({"label": "shift（视频）", "value": f"{float(shift_video):.2f}"})

    shift_audio = params.get("shift_audio")
    if shift_audio is not None:
        out.append({"label": "shift（音频）", "value": f"{float(shift_audio):.2f}"})

    cfg = params.get("cfg")
    if cfg is not None:
        out.append({"label": "CFG", "value": f"{float(cfg):.2f}"})

    seed = params.get("seed")
    if seed:
        out.append({"label": "种子", "value": str(seed)})

    return out


# ------------------------------------------------------------------
# 落盘文件的落点与检查（IO，但与 ComfyUI 无关）
# ------------------------------------------------------------------
# 【落盘结构 —— 单一真源在 continuation_service 里】
# 本项目的接续用 **Relay-Kit 自己的目录结构**：
#     <ComfyUI output>/relay_kit/<run_id>/stage_<N>.safetensors
# 理由（见 video_workflow.py 的注释）：H3RelayLatentSave / H3RelayLatentLoad
# 是一对，段号语义由节点自己管；而拷贝桥的外观锚（ref_anchor_stage=0）
# 会**自动**去读同一个 run_id 的 stage_00000 —— 换成别的目录结构
# 这个自动读取就断了。
#
# 所以这里只提供「按 Relay-Kit 规则算路径」的能力，
# 具体 run_id 的推导在 continuation_service（它知道链 id -> run_id）。
def stage_relpath(run_id: str, segment: int = 0) -> str:
    """段文件相对 ComfyUI output 的路径。

    形如 `relay_kit/<run_id>/stage_00000.safetensors`。
    与 Relay-Kit `_stage_path` 完全一致。
    """
    rid = sanitize_run_id(run_id)
    return f"relay_kit/{rid}/stage_{int(segment):05d}.safetensors"


def stage_file_path(comfy_output_dir: str | os.PathLike, run_id: str,
                    segment: int = 0) -> Path:
    """段文件的绝对路径。"""
    return Path(comfy_output_dir) / stage_relpath(run_id, segment)


def stage_file_exists(comfy_output_dir: str | os.PathLike, run_id: str,
                      segment: int = 0) -> bool:
    """段文件在不在。"""
    try:
        return stage_file_path(comfy_output_dir, run_id, segment).is_file()
    except OSError:
        return False


def missing_reason(comfy_output_dir: str | os.PathLike, run_id: str,
                   segment: int = 0) -> str:
    """潜空间缺失时给用户看的中文原因（**明确**，不静默失败）。

    ⚠ 传入的是 **run_id**（不是任务 id）—— 落盘目录是
    `output/relay_kit/<run_id>/stage_<N>.safetensors`。
    """
    path = stage_file_path(comfy_output_dir, run_id, segment)
    return (f"这段视频的接续潜空间文件已不在（{path}）—— "
            "可能已被清理，或产物是二采（二采不支持接续）。"
            "请改用「复用参数」重新生成一段。")

