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

【selflift（二采）接续：2026-11-17 实测修正】
早先这里写的是「二采潜空间不同源、一律不能接续」。**该结论已实测推翻**：

1. **网格对齐没问题**（独立复核，2026-11-17）：
   直接读线上真实二采任务的落盘潜空间头（safetensors header，不加载权重），
   4/4 样本的 latent 时间步 T 全部 == `video_latent_t(align_frame_count(时长))`
   （10s→72、5s→37），且 `tail_aligned(fc, 22)` 全部 True。
   → 拷贝桥第 3 条硬约束（尾段起始落在 5 步周期边界）**天然满足**。
   原因：SelfLift 的抬升只改 H/W **不改 T**（`h3_upscaler.py:391`
   `target_size=(z0_low.shape[2], H, W)` —— T 原样传递），落盘的也是
   **高分辨率段**（不是两段拼接体）。

2. **SelfLift 原生支持拷贝桥的噪声掩码**：
   `comfyui-SelfLift/nodes.py:116-155` `_validate_latent_input()` 读
   `latent_image["noise_mask"]`，接受 `[B,1,T,H,W]` 并按 latent 网格 resize ——
   与 CopyBridge 输出的 `vmask` 同构；
   `nodes.py:280-297` `masked_model()` 对**低分辨率段与高分辨率段都**装
   `_mask_blend_fn` post-CFG 钩子 ⇒ 钉住区零重绘在二采的**两段**都成立。
   `nodes.py:444-445` 保留区用 `video_anchor`（未抬升的真实内容）还原 ——
   拷贝区是逐位拷贝内容，不是抬升结果。

3. **接线姿势已有先例**：nonebot `minimax_h3_v1.json` 的 171 节点就是
   `latent_image <- ["322",0]`、`positive <- ["322",3]`（拷贝桥之后）。
   本项目照这个姿势接：**SelfLift 消费拷贝桥的 latent + 桥第 4 路 conditioning**。

→ 所以二采**可以**接续，但受**能力探测**约束（见 `continuation_allowed`）：
必须「上游二采段自己有落盘潜空间」才允许，否则明确拒绝。
剩余的画质取舍（钉住区是低分辨率抬升出来的 → 可能有清晰度台阶）
由实测量化，不是格式不兼容问题 —— 见报告 §4.3.1/§4.3.2。

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
#: **t2v** —— 接续桥接在 h3studio/video_workflow.py 的
#: `build_t2v_panel` 里（文生视频新面板）。
#:
#: **flf / r2v** —— 接续桥接在 h3studio/h3_video_modes.py 的
#: `_build_common` 里（首尾帧 / 参考生视频共用骨架，条件节点是
#: T8 的 `MiniMaxH3AudioConditioningT8`）。桥接法与 t2v **完全同款**：
#:   读回上游潜空间 -> 拷贝桥（逐位拷贝尾段 + 噪声掩码）-> 采样
#:   -> 落盘本段接力棒 -> decode 后裁钉住区。
#: 差别只在「条件节点 id」不同（t2v 是 105:104，flf/r2v 是 136）。
#:
#: ⚠ **不能只加元组就上线**（这是本文件的历史教训）：
#: 以前 flf/r2v 不落盘潜空间，光把它们加进这个元组会让「点接续 →
#: 提交 → 生成」**静默退化成普通生成**（看起来成功，其实没接上）——
#: 最坏的一种失败。现在三件事都已补齐：
#:   1. flf/r2v 的工作流里真的接了桥（h3_video_modes._build_common）；
#:   2. flf/r2v 的任务记录里真的建了链 + 分配了落盘目录
#:      （server.create_job_record）；
#:   3. 潜空间真的落盘了（Relay-Kit 的 H3RelayLatentSave，输出节点）。
#:
#: selflift（二采）是**模式内的开关**，不是独立 mode，
#: 所以排除逻辑在 mode_supported() 之外还要看 params["twopass"]["enabled"]。
CONTINUABLE_MODES = ("t2v", "flf", "r2v")

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


def continuation_frames(frame_count: int, window: int = DEFAULT_CONTEXT_FRAMES,
                       is_head: bool = False) -> dict:
    """算清一段接续的**帧数账**。

    返回：
      {
        "frame_count":     本段总帧数（17k+5 网格）,
        "latent_steps":    本段 latent 步数,
        "context_frames":  实际钉住帧数（窗口被吸附过）,
        "context_steps":   钉住区占几个 latent 步,
        "trim_frames":     decode 后要裁掉的帧数（= 钉住帧数）,
        "delivered_frames":本段交付给用户的帧数（链首段 = 总帧数；续接段 = 总帧数 - 裁掉）,
        "total_seconds":   本段交付时长（秒）,
      }

    【is_head —— 链首段不裁，续接段才裁（2026-11-17 更正）】
    链首段没有上游、没有重叠区，所以它**交付整段**。
    续接段的头部是上一段尾部的**重叠**（钉住区），那部分在上一段
    已经交付过了 —— 裁掉，避免重播。

    【默认值 = False（续接段语义）—— 不能改】
    这个函数**历史上**就是「算一段续接」的账（永远裁掉钉住区），
    已有测试与调用方都按这个语义写。默认改成 True 会让
    「续接段」的账凭空多 22 帧 —— 那正是本轮要消灭的那类漂移。
    所以：**默认续接段**，链首段要显式传 `is_head=True`。
    （`continuation_service.continuation_state` 已改成显式传参。）

    【为什么这个参数必须在**这里**而不是调用方自己减】
    之前「链首段不裁」这条规则散落在 `continuation_service.continuation_state`
    里（用 `has_source` 判断）。同一个减法被写了两遍，两处一旦漂移
    就出现「显示值与真实成片对不上」。这里做成**单一真源**：
    调用方只传「我是不是链首段」，减法只在这里发生。

    【交付帧数与「成片总时长」的关系 —— 用户口径（2026-11-17）】
    用户口径：成片总时长 = Σ(各段请求时长) - (段数-1) × 1 秒
    （潜空间接续有 1 秒重叠，重叠部分来自前段尾部帧，不重复计入）。

    实现上的对应：
      * 段 i 的**总帧数** = align_frame_count(请求时长 × 24)
        —— H3 只能出 17k+5 网格的帧（请求 5s -> 124 帧 = 5.1667s），
        这是**模型侧**的硬约束，不是我们的选择；
      * 续接段**裁掉** = context_frames（默认 22 帧 = 0.9167s）
        —— 这是**协议侧**的硬约束（Relay-Kit 只认 5+17k 网格的
        钉住窗口，24 帧会被 relay_core.py:411 直接 raise）。
      * 所以实测成片 = Σ align(请求帧数) - (续接段数) × 22 帧。
        与用户公式的差 = 网格余量 + (1 - 22/24) × (段数-1)，
        **不是**「把请求时长当新增时长直接累加」，也不是「整段截断」。
        （实测对照见 docs/实现-接续合并时长与跨段提示词增强-20261117.md）
    """
    fc = align_frame_count(int(frame_count))
    win = snap_context_frames(int(window))
    if win <= 0:
        win = 0
    steps = steps_for_frames(win) if win else 0
    # 链首段没有重叠区 -> 交付整段；续接段裁掉重叠（钉住区）。
    delivered = fc - win if (win and not is_head) else fc
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
# 链级时长账（成片总时长 —— 唯一真源）
# ------------------------------------------------------------------
def chain_duration_account(requested_seconds: list[float],
                           window: int = DEFAULT_CONTEXT_FRAMES) -> dict:
    """算一条接续链的**成片总时长**（逐段累加）。

    参数
      requested_seconds  链上**各段**的请求时长（秒），按段序。
                         链首段在前。
      window             钉住窗口（帧）；续接段裁掉这么多重叠帧。

    返回
      {
        "segments":      [每段的账, ...],
        "total_frames":  成片总帧数（= 各段交付帧数之和）,
        "total_seconds": 成片总时长（秒）,
        "overlap_frames":  每段之间的重叠帧数（= window）,
        "overlap_seconds": 每段之间的重叠时长（秒）= window / 24,
        "requested_seconds": 各段请求时长之和,
        "grid_slack_frames":  网格余量（align 吸附多出来的帧数）,
        "grid_slack_seconds": 网格余量（秒）,
        "formula_seconds":     用户口径的成片时长（秒）,
        "delta_seconds":       实测成片 - 用户口径（秒）,
        "notes":          [中文说明, ...],
      }

    【用户口径（2026-11-17 更正 —— 这是权威口径）】
        潜空间接续存在 1 秒重叠：每接续一段，实际新增时长 =
        该段请求时长 - 1 秒；重叠的 1 秒来自潜空间续接的前段尾部帧，
        与前段尾部重合，不重复计入。
        成片总时长 = Σ(各段请求时长) - (段数-1) × 1 秒

        例：第1段 5s -> 成片 5s
            接续第2段（请求 5s）-> 成片 = 5 + (5-1) = 9s
            再接续第3段（请求 8s）-> 成片 = 9 + (8-1) = 16s

    【实测值与用户口径的差 —— 两处都是 H3 的硬约束，不是我们的选择】
      1. **网格余量**：H3 只能出 17k+5 网格的帧。请求 5s = 120 帧，
         但模型只能出 124 帧（= 5.1667s）。所以链首段交付比请求多 4 帧。
         请求 8s = 192 帧 —— 恰好在网格上，无余量。
      2. **重叠窗口**：Relay-Kit 的拷贝桥只认 5+17k 网格的钉住窗口
         （5/22/39/56/73/90/107/124）。24 帧（= 1.000s）**不在**网格上，
         `relay_core.py:411` 会直接 raise：「24 帧不是 H3 的整步续接窗口」。
         最接近 1 秒的合法窗口是 **22 帧 = 0.9167s**（nonebot 咏唱 h3
         的 seg1/seg2 同款）。

      于是：
        实测成片 = Σ align(请求帧数) - (续接段数) × 22 帧
        与用户口径的差 = 网格余量 + (1 - 22/24) × (段数-1)

      实测对照（真实产物 ffprobe）：
        5s -> 5s     : 实测 226 帧 = 9.4167s；用户口径 9.0000s（差 +0.4167s）
        5s -> 5s -> 8s: 实测 396 帧 = 16.5000s；用户口径 16.0000s（差 +0.5000s）

    【为什么不「改实现去凑用户的数字」】
      要严格命中用户公式，需要 (a) 链首段交付恰好 = 请求帧数、
      (b) 每续接段恰好扣 24 帧。**两条都做不到**：
      (a) 120 帧不在 17k+5 网格，H3 生成不出这个长度的段；
      (b) 24 帧不在 5+17k 合法窗口，拷贝桥会拒绝（raise）。
      改这两处只能去改第三方节点/模型的网格约束 —— 那是「为凑数字
      破坏协议」，不是修复。所以这里**如实算出实测值 + 与用户口径的差**，
      并把差值的中文原因写进 notes，让界面能说清「为什么不是整 9.000 秒」。

    【本函数是唯一真源】
    之前「链首段不裁」这条规则在 `continuation_service.continuation_state`
    与 `server._prepare_continuation_params` 各写了一遍（用 `has_source`
    判断），两处漂移就会让「显示的累计时长」与「真实成片」对不上。
    现在统一从这里取。
    """
    win = snap_context_frames(int(window))
    if win <= 0:
        win = 0
    durations = [float(d) for d in (requested_seconds or [])]
    segments: list[dict] = []
    total_frames = 0
    requested_frames = 0
    for index, dur in enumerate(durations):
        want = int(round(dur * FPS))
        fc = align_frame_count(want)
        is_head = index == 0
        # 链首段不裁（没有重叠区）；续接段裁掉重叠。
        delivered = fc - win if (win and not is_head) else fc
        total_frames += delivered
        requested_frames += want
        segments.append({
            "index": index,
            "requested_seconds": dur,
            "requested_frames": want,
            "frame_count": fc,
            "is_head": is_head,
            "overlap_frames": 0 if is_head else win,
            "delivered_frames": delivered,
            "delivered_seconds": round(delivered / FPS, 6),
            "grid_slack_frames": fc - want,
        })
    overlap_frames = win * max(0, len(durations) - 1)
    grid_slack = total_frames + overlap_frames - requested_frames
    formula = sum(durations) - max(0, len(durations) - 1) * 1.0
    actual = total_frames / float(FPS)

    notes: list[str] = []
    if grid_slack:
        notes.append(
            "H3 的段长只能落在 17k+5 帧网格（请求 5 秒实际生成 124 帧 = "
            f"{124 / FPS:.4f} 秒），整条链因此多出 {grid_slack} 帧"
            f"（{grid_slack / FPS:.4f} 秒）。这是模型侧的帧长约束。")
    if win and win != 24:
        notes.append(
            f"段与段之间的重叠是 {win} 帧（{win / FPS:.4f} 秒），不是整 1 秒 —— "
            f"潜空间拷贝桥只接受 5+17k 帧网格的钉住窗口（5/22/39/56/73/90/107/124），"
            f"24 帧会被节点直接拒绝；{win} 帧是合法窗口里最接近 1 秒的一个。")
    if abs(actual - formula) > 1e-6:
        notes.append(
            f"实测成片 {actual:.4f} 秒，与「各段请求时长之和减去每段 1 秒重叠」"
            f"（{formula:.4f} 秒）相差 {actual - formula:+.4f} 秒，"
            f"差值来自上面两条帧网格约束（不是接续丢失了内容）。")

    return {
        "segments": segments,
        "total_frames": total_frames,
        "total_seconds": round(actual, 6),
        "overlap_frames": win,
        "overlap_seconds": round(win / float(FPS), 6),
        "requested_seconds": round(sum(durations), 6),
        "grid_slack_frames": grid_slack,
        "grid_slack_seconds": round(grid_slack / float(FPS), 6),
        "formula_seconds": round(formula, 6),
        "delta_seconds": round(actual - formula, 6),
        "notes": notes,
    }


def chain_frames_from_durations(durations: list[float],
                                window: int = DEFAULT_CONTEXT_FRAMES) -> int:
    """成片总帧数（薄封装 —— 只关心帧数时用这个）。"""
    return chain_duration_account(durations, window)["total_frames"]


# ------------------------------------------------------------------
# 成片帧数补偿（2026-11-17 收口：成片时长精确命中用户口径）
# ------------------------------------------------------------------
def formula_total_frames(requested_seconds: list[float]) -> int:
    """用户口径的成片总帧数。

        成片总时长 = Σ(各段请求时长) - (段数-1) × 1 秒
        成片总帧数 = Σ(各段请求秒数 × 24) - (段数-1) × 24

    例（24fps）：
      5s → 5s      : 120 + 120 - 24 = 216 帧 = 9.000s
      5s → 5s → 8s : 120 + 120 + 192 - 48 = 384 帧 = 16.000s
    """
    durations = [float(d) for d in (requested_seconds or [])]
    if not durations:
        return 0
    requested_frames = sum(int(round(d * FPS)) for d in durations)
    return requested_frames - max(0, len(durations) - 1) * FPS


def chain_compensation_plan(requested_seconds: list[float],
                            window: int = DEFAULT_CONTEXT_FRAMES) -> dict:
    """算一条链的**成片尾裁计划**（让成片帧数精确命中用户口径）。

    返回
      {
        "formula_frames":   用户口径的成片总帧数,
        "current_frames":   不补偿时的成片总帧数,
        "trim_frames":      需要从**最后一段尾部**裁掉的帧数,
        "segment_trim":     [每段尾裁帧数]（只有最后一段可能非 0）,
        "keep_frames":      [每段在成片里保留的帧数],
        "exact":            补偿后是否**精确**命中用户口径,
        "residual_frames":  补偿后仍差多少帧（0 = 精确命中）,
        "notes":            [中文说明, ...],
      }

    【为什么要补偿 —— 两个独立的差，各自有各自的来源】

      差 1：**网格余量**。H3 的段长只能落在 17k+5 帧网格上
            （请求 5 秒 = 120 帧，模型只能出 124 帧 = 5.1667s）。
            每段多生成 align(请求帧) - 请求帧 帧。

      差 2：**重叠差额**。潜空间拷贝桥的钉住窗口只接受 5+17k 网格
            （5/22/39/…/124），24 帧（= 整 1 秒）不在网格上 ——
            Relay-Kit `relay_core.py:411` 会直接 raise。
            生产档用的是 22 帧 = 0.9167s，所以每处重叠比用户以为的
            **少 2 帧**，成片就多出 2 帧。

      两者相加 = 成片比用户口径多出的帧数。

    【补偿落在哪一端 —— 为什么只裁最后一段的尾部】

      实测（线上真实产物，ffprobe + 逐帧灰度比对）：
        * 段文件里 TrimAV 已经把**重叠头部** 22 帧裁掉了
          （段1 文件 = 124 - 22 = 102 帧）；
        * |段0 末帧 - 段1 首帧| = 0.6685 = **正常相邻帧差**
          ⇒ 段1[0] 紧接段0 末帧，**接缝连续性成立**。

      ⇒ **裁链首段或中间段的任何一端都会破坏接缝连续性**：
        - 裁链首段尾部 → 接缝处变成「段0 的倒数第 N 帧 → 段1 首帧」，
          中间凭空少 N 帧 ⇒ 接缝跳变从 0.67 涨到 5.8（实测 10.32x）；
        - 裁续接段头部 → 裁掉的是「本段真正的新内容」的开头。

      ⇒ **只裁最后一段的尾部**：
        - 接缝（段0→段1、段1→段2）**完全不受影响**（那些段的帧一个不裁）；
        - 被裁掉的是最后一段末尾「超出请求时长」的那几帧 ——
          它们本来就是 H3 在网格上多生成的，用户没要求过；
        - 实测：成片前 216 帧与旧 226 帧成片**逐帧一致**（最大差 0.0086）。

    【为什么不改上游】
      * 差 1 改不了：120 帧不在 17k+5 网格，H3 生成不出这个长度的段；
      * 差 2 改不了：24 帧不在 5+17k 合法窗口，拷贝桥会 raise
        （Relay-Kit `relay_core.py:411-419`，报错文案
        「%d 帧不是 H3 的整步续接窗口」）。
      所以补偿**只能**在解码后按帧裁剪 —— 而这正是本函数给出的计划。
    """
    win = snap_context_frames(int(window))
    if win <= 0:
        win = 0
    durations = [float(d) for d in (requested_seconds or [])]
    if not durations:
        return {"formula_frames": 0, "current_frames": 0, "trim_frames": 0,
                "segment_trim": [], "keep_frames": [], "exact": True,
                "residual_frames": 0, "notes": []}

    account = chain_duration_account(durations, win)
    current = int(account["total_frames"])
    formula = formula_total_frames(durations)
    trim = current - formula

    # 【2026-11-18 第 6 轮审查修 —— 尾裁不得跨过接缝】
    #
    # 【原缺陷】trim = current - formula 无条件全扣到**最后一段**尾部。
    # 但 trim 可能**超过最后一段自己的交付帧数** —— 那时尾裁会一路吃进
    # 上一段，把「上一段末帧 → 本段首帧」那个接缝一起裁掉：
    #   实测（4 段 × 2 秒）：最后一段交付 34 帧，trim 却要 38 帧
    #   → keep_frames[-1] = -4（负数 = 该段被裁光并啃进上一段）
    #   → 成片里段2→段3 的接缝消失，画面从段2 中段直接跳到段3 中段。
    #
    # 【为什么不是「夹到 last_delivered」而是「完全不裁」】
    # 夹到 last_delivered 之后：残差 = trim_raw - last_delivered > 0
    # → **依然命中不了用户口径**，而代价是最后一段被**整个裁光**
    #   （内容彻底消失，且用户看不到任何提示 —— 静默的内容丢失，
    #    是本项目最忌的那类失败）。
    # 所以：**裁不到就不裁** —— 保住每一段的内容与每一个接缝，
    # 残差如实报出去（前端已有「⚠ 相差 N 帧」的展示位）。
    #
    # 【为什么必须保住接缝】接缝连续性是接续功能的**核心价值**
    # （见本函数 docstring「为什么不改上游」：裁链首段/中间段会让接缝
    # 跳变从 0.67 涨到 5.8，实测 10.32x）。**绝不能为了凑用户口径的
    # 数字去破坏接缝或丢内容** —— 宁可成片比口径长几帧并如实报出残差。
    last_delivered = int(account["segments"][-1]["delivered_frames"]) \
        if account.get("segments") else 0
    trim_clamped = False
    trim_clamped_extra = 0
    segment_trim = [0] * len(durations)
    if trim > 0 and len(durations) >= 2 and trim > last_delivered:
        trim_clamped = True
        trim_clamped_extra = trim - last_delivered
        # 裁不到目标 -> 一帧都不裁（保住全部内容与全部接缝）。
        trim = 0
    elif trim > 0 and len(durations) >= 2:
        # 只从**最后一段**尾部裁；中间段与链首段一帧不裁。
        segment_trim[-1] = trim
    elif trim > 0:
        # 【单段链不裁】单段链没有「成片拼接」这一步 ——
        # 段文件本身就是最终产物（server 的 _finalize_continuation_impl
        # 对 stage<=0 直接 return，不调 _concat_chain_video）。
        # 裁它 = 改写单段视频的文件，而单段视频的时长是
        # 「H3 的 17k+5 段长网格」决定的（请求 5s -> 124 帧 = 5.1667s），
        # 那是**所有**视频（不止接续链）都有的模型侧约束，
        # 不属于「接续成片时长」的收口范围。
        # 所以这里不裁，并把残差如实报出去（界面会写明来自哪条约束）。
        pass
    keep = [s["delivered_frames"] - t
            for s, t in zip(account["segments"], segment_trim)]
    residual = sum(keep) - formula

    notes: list[str] = []
    if trim_clamped:
        # 尾裁被夹住 —— 残差如实上报，并说清「为什么不补」。
        notes.append(
            f"成片比「各段请求时长之和减去每段 1 秒重叠」多 {residual} 帧"
            f"（{residual / FPS:.4f} 秒）—— 这 {trim_clamped_extra} 帧「不能」再从"
            f"最后一段尾部裁：最后一段本身只有 {last_delivered} 帧，"
            f"再裁就会吃进上一段、把接缝（上一段末帧 → 本段首帧）一起裁掉，"
            f"画面会在接缝处跳变。「保住接缝优先于凑整秒数」。")
    if trim > 0 and len(durations) >= 2:
        slack_total = sum(int(s["grid_slack_frames"]) for s in account["segments"])
        n_cont = max(0, len(durations) - 1)
        win_gap = (FPS - win) if win else 0
        notes.append(
            f"成片帧数已按用户口径精确对齐："
            f"当前 {current} 帧 -> 目标 {formula} 帧，"
            f"从最后一段尾部裁掉 {trim} 帧。")
        notes.append(
            f"这 {trim} 帧的来源：H3 的 17k+5 段长网格多生成 {slack_total} 帧"
            + (f"，加上每处重叠窗口是 {win} 帧而不是整 1 秒（少 {win_gap} 帧 × "
               f"{n_cont} 处 = {win_gap * n_cont} 帧）——"
               f"潜空间拷贝桥只接受 5+17k 帧网格的钉住窗口，24 帧会被节点拒绝。"
               if win_gap and n_cont else "。"))
        notes.append(
            f"补偿只裁「最后一段的尾部」，前面的段一帧不裁 —— "
            f"接缝（上一段末帧 → 下一段首帧）的连续性完全不受影响，"
            f"也不会丢掉任何一段的内容。")
    elif trim > 0:
        # 单段链：残差来自模型侧的段长网格，不属于接续成片的收口范围。
        notes.append(
            f"当前只有 1 段，没有「成片拼接」这一步 —— 段文件本身就是产物，"
            f"所以成片是 {current} 帧（{current / FPS:.4f} 秒），"
            f"比请求的 {formula} 帧多 {trim} 帧（{trim / FPS:.4f} 秒）。")
        notes.append(
            f"这 {trim} 帧来自 H3 的 17k+5 段长网格"
            f"（请求 {durations[0]:g} 秒 = {int(round(durations[0] * FPS))} 帧，"
            f"模型只能出 {int(account['segments'][0]['frame_count'])} 帧）—— "
            f"这是「所有」视频（不止接续链）都有的模型侧约束。")
    elif trim < 0:
        notes.append(
            f"成片比用户口径「短」 {abs(trim)} 帧（{abs(trim) / FPS:.4f} 秒）—— "
            f"这一项无法用裁剪补齐（内容本来就不够）。")

    return {
        "formula_frames": formula,
        "current_frames": current,
        # 【实际会裁的帧数】单段链不裁（没有成片拼接这一步），
        # 所以这里报 0，而不是「算出来但没地方裁」的 trim。
        "trim_frames": int(sum(segment_trim)),
        "trim_frames_available": max(0, trim),
        # 【2026-11-18 第 6 轮审查新增】尾裁被夹住时的信息：
        #   trim_clamped       —— 是否因「会跨接缝」而少裁
        #   trim_clamped_extra —— 因此少裁了多少帧（= residual 的来源）
        # 前端据此显示「差多少帧 + 为什么不能补」。
        "trim_clamped": trim_clamped,
        "trim_clamped_extra": int(trim_clamped_extra),
        "segment_trim": segment_trim,
        "keep_frames": keep,
        "exact": residual == 0,
        "residual_frames": residual,
        "notes": notes,
    }


# ------------------------------------------------------------------
# 可接续性判定
# ------------------------------------------------------------------
def selflift_enabled(params: dict) -> bool:
    """这个任务是不是走了二采（SelfLift）路径。

    二采**可以**接续（2026-11-17 实测修正，见模块 docstring）：
    网格对齐没问题、SelfLift 原生支持拷贝桥的噪声掩码。
    但它受**能力探测**约束 —— 上游二采段必须自己有落盘潜空间。
    那个判定需要查盘，在服务层做（continuation_service.build_continuation_context）。
    """
    twopass = (params or {}).get("twopass")
    if isinstance(twopass, dict) and twopass.get("enabled"):
        return True
    return bool((params or {}).get("selflift"))


def mode_supported(mode: str) -> bool:
    """这个生成模式本身支持接续吗。

    目前支持三个视频模式：t2v（文生视频）/ flf（首尾帧）/ r2v（参考生）。
    图像、音乐不支持 —— 它们没有 AV latent 概念。
    """
    return str(mode or "") in CONTINUABLE_MODES


#: 模式 -> 中文名（给用户的文案用）。
MODE_LABELS = {
    "t2v": "文生视频",
    "flf": "首尾帧视频",
    "r2v": "参考生视频",
}


def mode_label(mode: str) -> str:
    """模式的中文名。未知模式回显原值（不编造）。"""
    key = str(mode or "")
    return MODE_LABELS.get(key, key or "未知模式")


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

    【关于二采（selflift）】二采**可以**接续（2026-11-17 实测修正）：
    网格对齐没问题、SelfLift 原生支持拷贝桥的噪声掩码。
    但它要求「上游二采段自己有落盘潜空间」—— 那是**查盘**判定，
    放在服务层（continuation_service.build_continuation_context），
    因为本模块是**纯函数**（不查盘、不连 ComfyUI）。
    → 本函数对二采**不再无条件拒绝**；真正缺潜空间时，
    服务层会给出「二采段没有落盘潜空间」的具体原因。
    """
    if not mode_supported(mode):
        # 【为什么要按模式区分】
        # 原来这一条对所有非视频模式都返回「只有视频类产物支持接续。」
        # —— 但 flf/r2v **就是**视频类产物（它们的旧文案是错的，
        # 线上实测确实这么拒过）。现在 t2v/flf/r2v 都能接续，
        # 真正被拒的是图像 / 音乐 / 长片拼接这些**没有 AV latent**
        # 的模式 —— 文案要说清「是哪一类不支持」，不要笼统。
        return False, (f"「{mode_label(mode)}」不是视频生成产物，"
                       f"没有可接续的潜空间 —— 只有文生视频 / 首尾帧 / "
                       f"参考生视频支持接续。")
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
            "可能已被清理，或这段是本功能上线前生成的。"
            "请改用「复用参数」重新生成一段。")

