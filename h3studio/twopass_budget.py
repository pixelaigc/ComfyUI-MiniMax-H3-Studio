# -*- coding: utf-8 -*-
"""二采（SelfLift）的 **NFE 预算护栏**（2026-11-17 接缝色档残缺陷）。

【缺陷现象】
二采（twopass）接续链在段2 与段1 的交界处有一次肉眼可见的**闪烁**。
上一轮把接续点 RGB 跳变从 19.39 压到 10.14（比值 29.52x -> 15.44x），
但仍远高于非二采对照的 2.14x。

【本轮的重新取证（不沿用历史结论）】
本轮写了独立测量器（tools/seam_metric.py），从零把「色档断点发生在哪里」
拆开量，结论与上一轮**方向不同**：

  * 断点在**段2 自己内部**（段2 的第 0 帧 -> 第 1 帧），
    **不在拼接点**。实测（线上真实产物，受控复现）：
        段2 内部帧0->1 的 RGB L1 差：
          旧档（steps=4, transition_step=3） = 13.75
          改 transition_step=2 后            = 1.08
          非二采对照                        = 0.39
    ⇒ 上一轮「拼接时间轴 + match_prev」的修法**方向没错但打不中要害**：
      它修的是拼接点，而真正的断点在段2 的采样结果里。

  * ComfyUI 日志给出**机制**（决定性证据，`[SelfLift plan]` 行）：
        steps=4 transition_step=3 -> low_nfe=3  high_nfe=1  sigma_resume=0.7207
        steps=7 transition_step=5 -> low_nfe=4  high_nfe=5  sigma_resume=0.5868
    SelfLift 把 NFE 预算切成「低分辨率 ts 步 + 高分辨率 (steps-ts) 步」。
    **默认档 steps=4 + transition_step=3 ⇒ 高分辨率二采只剩 1 步 NFE**，
    而这 1 步的起始 sigma 高达 0.72（噪声极重）。

  * 空间高频指纹证实「低分辨率段降采样」的指纹：
        旧档 段2 帧0..3 高频 = [0.436, 0.429, 0.431, 0.468]，帧50 = 2.922
        ⇒ 段头 4 帧的细节档只有段中段的 15%（低分辨率锚点被抬升上来）
        transition_step=2 的链：帧0..3 高频 = 帧50 的同一档（无过渡区）

【机理】
低分辨率一采把「钉住区 + 新生成区」一起降采样（lowres_scale），
在**降采样空间**里定下了色档与细节档；抬升回高分辨率后，
高分辨率二采必须同时做三件事：**提分辨率 + 收敛色档 + 生成内容**。
当高分辨率只剩 1 步 NFE（sigma 0.72 -> 0）时，这个预算不够，
结果就是段头几帧**色档 + 细节档双重掉档** —— 肉眼就是「闪一下」。

【修法（Studio 侧，零 comfyui 改动）】
SelfLift 的 `transition_step` 既是「低分辨率 NFE 数」，也**决定**高分辨率
还剩几步 NFE。把它夹到**不超过总预算的一半**（向下取整），
就能保证高分辨率段至少拿到一半的 NFE：

    steps=4: ts=3 (low3/high1)  ->  ts=2 (low2/high2)
    steps=7: ts=5 (low5/high2)  ->  ts=3 (low3/high4)
    steps=8: ts=6 (low6/high2)  ->  ts=4 (low4/high4)

实测：steps=4 / transition_step=2 ⇒ 接续比值 **15.372x -> 2.996x**，
段2 内部断点 **13.75 -> 1.08**（非二采对照 0.39），
高频能量 缝后/缝前 **0.899 -> 1.108**（过渡区消失）。

【为什么不改 comfyui】
SelfLift 的 `transition_step` 已经是**公开的节点输入**
（INPUT_TYPES 里就有，tooltip 也写着「Number of low-resolution denoiser
evaluations」）。高分辨率 NFE = steps - transition_step 是**纯算术**，
在 Studio 侧就能算清楚并夹住 —— **不需要**改第三方节点源码。
本轮实测证实：只改 Studio 侧传的 transition_step，缺陷即消失。

【护栏语义】
这个夹取是**有损的**：一采步数变少 = 低分辨率段提速收益变小
（低分辨率段的注意力面积小，本来就是提速的主要来源）。
所以：
  * 只在「高分辨率 NFE 不足总预算一半」时才夹（低 NFE 档本来就没这个问题）；
  * 夹取**必须留痕**（normalize_notes），让用户看得见「一采步数被从 3 改成 2」；
  * 用户**显式**填的值如果已经被夹过，不重复夹；
  * 关掉二采（enabled=false）时完全不碰。
"""

from __future__ import annotations

#: 高分辨率二采至少要拿到总 NFE 的这个比例，否则夹 transition_step。
#:
#: 依据（线上受控复现，见 docs/实现-首尾帧参考生接续与音频续接时序-20261116.md
#: 附录 C）：steps=4 时 ts=3 -> high_nfe=1（只有 25%），段2 内部断点 13.75；
#: ts=2 -> high_nfe=2（50%），断点 1.08。ts=1 -> high_nfe=3（75%），
#: 但一采只剩 1 步，低分辨率段的提速收益几乎归零，不划算。
#:
#: 所以取 1/2：**保证高分辨率段拿到一半预算**，同时**保留一半的低分辨率收益**。
HIGH_RES_NFE_MIN_RATIO = 0.5


def high_res_nfe(steps: int, transition_step: int) -> int:
    """高分辨率二采能拿到的 NFE 数。

    SelfLift 把 sigma 表切成 ``sigmas[:ts+1]``（低分辨率）与 ``sigmas[ts:]``
    （高分辨率），所以高分辨率 NFE = 总 NFE - 低分辨率 NFE = steps - ts。
    """
    return max(0, int(steps) - int(transition_step))


def clamp_transition_step(steps: int, transition_step: int,
                          min_ratio: float = HIGH_RES_NFE_MIN_RATIO) -> int:
    """把 transition_step 夹到「高分辨率段至少拿到 min_ratio 预算」的上限。

    返回**夹后**的 transition_step。若原值已满足约束，返回原值。

    【硬约束仍然由 SelfLift 节点负责】
    节点要求 ``1 <= transition_step <= len(sigmas) - 2``；本函数只负责
    **NFE 预算**这一层，不越界接管节点的其它校验。

    【为什么向下取整而不是四舍五入】
    高分辨率 NFE = steps - ts，要它 >= steps * min_ratio，
    则 ts <= steps * (1 - min_ratio)。steps=4 / min_ratio=0.5 =>
    ts <= 2.0 => ts <= 2。取整方向必须是**向下**（ts 越小高分辨率 NFE 越多），
    否则 ts=3 会被保留下来 —— 那正是缺陷档。
    """
    steps = int(steps)
    ts = int(transition_step)
    if steps <= 0:
        return ts
    # 高分辨率 NFE 下限 = ceil(steps * min_ratio)
    # 用整数运算避免浮点误差：steps * min_ratio 先放大 1000 倍再上取整。
    scaled = int(steps) * int(round(min_ratio * 1000))
    min_high = -(-scaled // 1000)
    max_ts = steps - min_high
    if max_ts < 1:
        # steps 太小（1 步）时高分辨率至少要 1 步 => ts 只能是 0，但节点要求 >=1。
        # 此时不动 ts，交给节点的硬校验去夹。
        return ts
    return min(ts, max_ts)


def describe(steps: int, transition_step: int) -> str:
    """一句话说明当前 NFE 分配（给 normalize_notes 用）。"""
    ts = int(transition_step)
    low = ts
    high = high_res_nfe(steps, ts)
    total = max(1, int(steps))
    return (f"一采 {low} 步（二采 {high} 步，"
            f"高分辨率占 {high * 100 // total}%）")
