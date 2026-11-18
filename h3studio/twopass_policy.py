# -*- coding: utf-8 -*-
"""接续段的**采样路径策略**：默认走标准采样（等价 nonebot seg2）。

【背景：为什么要有这个模块】
2026-11-18 实测（见 docs/evidence-20261118-seam-and-refimg.md 的 C 部分）：
接续链在**段 2 内部 frame0→1** 出现硬切式色调跳变
（亮度 80.4→73.4、饱和度 30.0→36.8），段头断点比达 2.58x / 4.54x，
3 帧亮度降达 +7.2 灰阶。而**二采 OFF** 的 11 条链全部 ≤1.58x、亮度降 ≤+0.26。

根因不是「selflift 节点 vs 标准节点」二选一 —— 在我们路径上
**二采 ≡ SelfLiftH3Sampler**，两者是同一个东西。根因是**二采本身**
（低分辨率一采定色档 → 抬升 → 高分辨率只剩几步 NFE 收敛色档，
段头几帧色档与细节档双重掉档）。

而 nonebot 的 `minimax_h3_seg1.json` / `minimax_h3_seg2.json`
用的是**标准 SamplerCustomAdvanced**（无 SelfLift）——
所以「等价 seg2」= **接续段默认走标准采样**。

【关键可行性依据（外层已核实）】
ComfyUI 的标准 SamplerCustomAdvanced **会**读 latent 里的 noise_mask
并作为 denoise_mask 传给采样器
（comfy/comfy_extras/nodes_custom_sampler.py:1047-1055：
  `if "noise_mask" in latent: noise_mask = latent["noise_mask"]`
  -> `guider.sample(..., denoise_mask=noise_mask, ...)`）。
⇒ 拷贝桥的钉住区（noise_mask）在标准采样路径上**照常生效**，
不存在「换回标准采样就失去钉帧」的问题。

【本模块的职责：只回答一个问题】
「这一次生成，**应不应该**走二采（SelfLift）？」

返回三态，而不是 bool —— 因为「接续段 + 用户显式开了二采」需要
**区别于**「普通段 + 用户显式开了二采」：

  * ``"allow"``  —— 走标准采样（默认路径）
  * ``"deny"``   —— 用户显式要求但**不允许**（接续段默认禁用二采）
  * ``"warn"``   —— 走二采，但**必须**留痕警告

【为什么接续段默认禁用二采（而不是只警告）】
§3 的普查数据（13 条链，二采 ON 2 条 / OFF 11 条）显示 ON 组**无重叠无反例**
地全部 >2.5x，而 OFF 组全部 ≤1.58x。段内色调突变是**用户能直接看到**的
缺陷，且发生在**每一条**二采接续链上。把它留给「用户显式勾选 + 明确警告」
是产品取舍，但**默认路径**必须是无缺陷的那条 —— 即等价 seg2。

【为什么不能靠前端】
前端 `videoState.twopass.enabled` 的默认值确实是 false，但
`/api/video-config`（**服务端持久化**的面板配置）会把用户上次勾选的状态
在每次页面加载时恢复回来（web/video-panel.js 的 `applyConfig(saved)`）。
⇒ 「默认关闭」这个语义**只在服务端归一化层才成立**。
"""
from __future__ import annotations

#: 二采在**接续段**的处置方式：默认禁用（等价 nonebot seg2）。
#:
#: 【为什么是 deny 而不是 warn】
#: 见模块 docstring 的普查数据：二采 ON 组段头断点比 2.58x / 4.54x、
#: 3 帧亮度降 +4.7 / +7.2 灰阶，而 OFF 组全部 ≤1.58x / ≤+0.26。
#: 这是**每条二采接续链都会发生**的可见缺陷，不是偶发。
#: 保留 ``"warn"`` 分支是为了将来若用户明确要求「我要在接续段用二采」
#: （例如做受控 A/B 实验），可以只改这一个常量。
CONTINUATION_TWOPASS_POLICY = "deny"

#: 普通段（首段 / 非接续）：用户显式勾选就允许，**不加任何限制**。
#:
#: 【为什么首段不禁用】段 1 没有上游潜空间、没有钉住区，
#: SelfLift 的降采样-抬升路径**不涉及跨段色档对齐**，
#: 实测首段走二采没有段头掉档问题（普查里的 ON 组两条都是 stage=1 的续接段）。
#: 禁掉首段的二采会白白削掉用户在高分辨率档位下的提速收益。
PLAIN_TWOPASS_POLICY = "allow"


def resolve_twopass_policy(params: dict, *,
                            continuation_policy: str = CONTINUATION_TWOPASS_POLICY,
                            plain_policy: str = PLAIN_TWOPASS_POLICY) -> str:
    """这次生成应不应该走二采？返回 ``"allow"`` / ``"deny"`` / ``"warn"``。

    :param params: 任务参数（原始入参，不要求已归一）
    :param continuation_policy: 接续段（续接段）的处置策略
    :param plain_policy: 普通段（首段 / 非接续）的处置策略
    :return:
        * ``"allow"`` —— 走标准采样；二采开关按原值（通常 false）
        * ``"deny"``  —— 走标准采样；**即使入参 enabled=true 也强制关掉**
        * ``"warn"``  —— 走二采，但调用方必须留痕警告

    【为什么「接续段」的判据是 source_job 而不是任何东西】
    ``params["continuation"]["source_job"]`` 非空 = 本段**有上游潜空间**
    = 续接段。首段的 continuation 字典里 source_job 为空串。
    这与 ``h3studio/continuation.py`` 的 ``_continuation_state()``
    的 ``has_source`` 判据**完全一致** —— 两处必须同口径，
    否则会出现「归一层认为不是接续段、工作流层认为是接续段」的分裂。
    """
    cont = (params or {}).get("continuation")
    cont = cont if isinstance(cont, dict) else {}
    is_continuation = bool(str(cont.get("source_job") or "").strip())

    policy = continuation_policy if is_continuation else plain_policy
    if policy not in ("allow", "deny", "warn"):
        raise ValueError(f"未知的二采策略 {policy!r}（只能是 allow/deny/warn）")
    return policy


def twopass_requested(params: dict) -> bool:
    """入参**是否请求**了二采（不做任何策略判断，只读原值）。"""
    twopass = (params or {}).get("twopass")
    if isinstance(twopass, dict) and twopass.get("enabled"):
        return True
    # 遗留别名：旧版用 params["selflift"]
    return bool((params or {}).get("selflift"))


def twopass_disabled_note(mode: str, stage_index: int | None) -> str:
    """接续段禁用二采时的中文留痕（给 normalize_notes）。"""
    stage = "" if stage_index is None else f"（第 {stage_index + 1} 段）"
    return (f"接续段{stage}已按标准采样路径执行（二采已关闭）："
            f"二采在接续段会造成段头色档跳变"
            f"（实测段头断点比 2.58x–4.54x、3 帧亮度降 +4.7～+7.2 灰阶），"
            f"标准采样与 nonebot minimax_h3_seg2 等价且无此缺陷。"
            f"如确需在接续段使用二采，请在提交参数里显式指定 "
            f"twopass.enabled=true 且 "
            f"continuation.allow_twopass=true。")


def twopass_warn_note(mode: str) -> str:
    """允许二采但需要警告时的中文留痕。"""
    return ("接续段按你的显式设置走了二采（SelfLift）路径："
            "实测二采接续段可能出现段头色档跳变"
            "（段头断点比 2.58x–4.54x），"
            "若看到「第 5 秒那一下变了个色调」，关掉二采即可。")


def continuation_allows_twopass(params: dict) -> bool:
    """接续段是否被**显式**允许使用二采。

    口径：在 ``params["continuation"]`` 里显式写
    ``allow_twopass: true`` 才算显式允许。

    【为什么需要这个显式口子】产品取舍：「二采在接续段有可见缺陷」
    是事实，但**用户可能故意**要它（例如做受控 A/B、或低分辨率档下
    二采提速收益大于色档代价）。本模块默认 deny，但留一个
    **必须显式声明**的入口 —— 避免「悄悄开着」（那才是 bug）。
    """
    cont = (params or {}).get("continuation")
    cont = cont if isinstance(cont, dict) else {}
    return cont.get("allow_twopass") is True


def resolve_effective_twopass(params: dict) -> tuple[bool, str]:
    """**唯一权威入口**：这次生成到底走不走二采？返回 ``(走不走, 处置方式)``。

    【为什么要有这个统一入口】
    服务端归一层（server.normalize_t2v_params）与工作流层
    （video_workflow / h3_video_modes）都必须回答同一个问题。
    如果两边各答各的，就会出现：
      * 归一层说「放行」、工作流层说「禁用」-> 白跑一圈；
      * 归一层说「禁用」、工作流层说「放行」-> **接续段又走回 SelfLift**，
        段头色档跳变复现（这正是本轮要修的缺陷）。
    ⇒ 两层必须调用**同一个函数**，口径才能保证一致。

    :param params: 任务参数
    :return: ``(use_twopass, disposition)``
        * disposition ``"allow"`` —— 走标准采样；是否走二采取决于入参
        * disposition ``"deny"``  —— 强制走标准采样（二采被禁用）
        * disposition ``"warn"``  —— 走二采，但调用方必须留痕警告
    """
    policy = resolve_twopass_policy(params)
    if policy == "deny" and continuation_allows_twopass(params):
        # 用户**显式**声明要在接续段用二采 -> 放行，但必须留痕警告。
        policy = "warn"
    if policy == "deny":
        return False, "deny"
    return twopass_requested(params), policy
