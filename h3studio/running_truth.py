# -*- coding: utf-8 -*-
"""
「运行中」状态的**真实性**判定（A5 需求）—— 纯函数层。

【这个模块在防什么 —— 2026-11-14 A5 需求】

界面上的「运行中」原本只有一个来源：jobs 表的 status 列。
而那一列是 **worker 写进去的**，不是**真相**：

  1. **服务重启后残留**
     进程重启 -> 内存里的 worker 没了，但 SQLite 里那些
     status='running' 的行还在 -> 卡片永远显示「运行中」，
     而实际上**没有任何进程在跑它**。
  2. **ComfyUI 侧任务已消失**
     ComfyUI 里任务被取消 / 被 /interrupt / ComfyUI 自己重启过
     -> history 里查不到、队列里也没有 ->
     worker 的 `while True` 轮询循环**永远等下去**
     -> 任务永远「运行中」，还占着 Studio 的串行队列（后面的任务全部排队等着）。

两个缺陷的**共同根因**是同一句话：
    「运行中」是**声明**，不是**观测**。

【本模块的职责】

把「这个任务到底还在不在 ComfyUI 里」这件事变成**可判定、可测试**的纯函数：

  * `queue_prompt_ids()`   —— 从 /queue 响应里取出全部 prompt_id；
  * `classify()`           —— 综合 history / queue / 时间，给出**唯一**状态；
  * `plan_residual_running()` —— 决定「服务重启后的残留 running」怎么处理；
  * 各种中文原因文案 —— 失败必须说清楚**为什么**、**接下来做什么**。

【设计约束 —— 与 comfy_errors / job_meta 同一纪律】

  * **纯函数、无 IO**：不连 ComfyUI、不查盘、不读环境变量。
    单元测试完全离线可跑。
  * **判定与取数分离**：取数（HTTP）在 server.py，判定在这里。
    这样「判定逻辑」能被单独穷举，不需要真的跑 ComfyUI。
  * **不确定就说不确定**：ComfyUI 读不到时返回 None ->
    判定层给「状态未知」，**绝不**当成「队列为空」。
    （把「读不到」当成「空」= 误杀所有在跑的任务 —— 比原来的 bug 更糟。）
"""

from __future__ import annotations


# ------------------------------------------------------------------
# 状态常量（唯一真源：server.py 与前端都按这几个字符串判定）
# ------------------------------------------------------------------

#: 真的在 ComfyUI 队列里跑着（history 无结果 + 队列里能查到）。
STATE_ACTIVE = "active"

#: ComfyUI 的队列与历史里都**没有**这个任务了（被取消 / 被清队列 / ComfyUI 重启）。
STATE_LOST = "lost"

#: 超过了运行上限（还在队列里，但已经超时不合理的时长）。
STATE_TIMEOUT = "timeout"

#: 判定不了（ComfyUI 不可达 / 刚提交还没进队列 / 信息不足）。
STATE_UNKNOWN = "unknown"

#: ComfyUI 里**已经有结果**了（worker 该收尾，不该再等）。
STATE_FINISHED = "finished"

#: 状态 -> 给用户看的中文说明。
STATE_LABELS = {
    STATE_ACTIVE: "运行中（已确认在 ComfyUI 队列中）",
    STATE_LOST: "已丢失（ComfyUI 队列与历史中都没有该任务）",
    STATE_TIMEOUT: "已超时（超过运行上限仍未完成）",
    STATE_UNKNOWN: "状态未知（暂时无法确认）",
    STATE_FINISHED: "已完成（ComfyUI 已返回结果）",
}


def state_label(state: str) -> str:
    """状态 -> 中文说明（未知状态给「状态未知」，绝不返回空串）。"""
    return STATE_LABELS.get(str(state or ""), STATE_LABELS[STATE_UNKNOWN])


# ------------------------------------------------------------------
# 默认参数
# ------------------------------------------------------------------

#: 默认运行上限（秒）。30 分钟。
#:
#: 【为什么是 30 分钟】H3 文生视频在本机 2080Ti 上 5 秒 / 768p 约 1-3 分钟，
#: 1080p 长片单段可能到 10 分钟量级 —— 30 分钟留足了正常余量，
#: 又足够早地把「卡死的运行中」变成可读的失败，而不是让用户干等。
DEFAULT_TIMEOUT_SEC = 1800.0

#: 「刚提交」的宽限期（秒）。
#:
#: 【为什么需要宽限期】判定「任务已丢失」的前提是「它已经不在队列里了」。
#: 但刚提交的瞬间存在一个窗口：ComfyUI 已经把 prompt_id 返给我们，
#: 而队列快照还没包含它（中间代理 / 立刻被别的任务挤掉 / 极短的空隙）。
#: 没有宽限期就会把这个**正常的**任务误判成「丢失」并杀掉。
DEFAULT_LOST_GRACE_SEC = 10.0


# ------------------------------------------------------------------
# 中文原因文案（用户可见，必须说清「为什么」+「怎么办」）
# ------------------------------------------------------------------

#: 任务在 ComfyUI 队列与历史里都不见了。
LOST_REASON = ("ComfyUI 队列中已无该任务（可能被取消或丢失）—— "
               "本机 ComfyUI 的运行队列与历史记录里都查不到它，"
               "任务已无法继续。请重新提交一次生成。")

#: 服务重启后残留的 running（且 ComfyUI 侧确实没有这个任务）。
RESTART_STALE_REASON = ("服务重启前该任务处于「运行中」，但本机 ComfyUI 的"
                        "队列与历史中都没有对应任务（任务已丢失或被取消）—— "
                        "已自动标记为失败，请重新提交生成。")

#: 服务重启时 ComfyUI 不可达 —— 判定不了，只能终止「运行中」。
COMFY_UNREACHABLE_REASON = ("服务重启时本机 ComfyUI 无响应，无法确认该任务"
                            "是否仍在运行 —— 已标记为失败以避免永久卡在"
                            "「运行中」。请确认 ComfyUI 已启动后重新提交。")


def timeout_reason(timeout_sec: float) -> str:
    """超时的中文原因（把秒数换算成分钟，用户看得懂）。

    【2026-11-19 缺陷 1 修】本函数保留为**兼容入口** —— 真正的策略
    在 `job_timeout.timeout_reason`（它按媒体类型区分，且上限可配置）。

    【口径纪律】文案里的分钟数**必须**来自实际传入的阈值，
    不得写死 —— 上限可配置后，写死的话术会与真实行为对不上。
    """
    try:
        minutes = max(1, int(round(float(timeout_sec) / 60.0)))
    except (TypeError, ValueError):
        minutes = 30
    return (f"任务运行超过 {minutes} 分钟仍未完成，已判定为超时失败。"
            "本机 ComfyUI 可能仍在占用显卡，请先在 ComfyUI 界面确认队列状态，"
            "再重新提交生成。")


# ------------------------------------------------------------------
# ComfyUI /queue 解析
# ------------------------------------------------------------------

def _extract_prompt_id(item) -> str:
    """从一条 ComfyUI 队列项里取出 prompt_id。

    【ComfyUI 的真实形状（0.37，本机源码 server.py:1230 / execution.py:1321）】

        self.prompt_queue.put((number, prompt_id, prompt,
                               extra_data, outputs_to_execute, sensitive))

    而 /queue 端点会把 sensitive 去掉、只返回前 5 项
    （server.py:114 `_remove_sensitive_from_queue`）：

        [number, prompt_id, prompt, extra_data, outputs_to_execute]

    => **prompt_id 固定在下标 1**。这是判定「我的任务还在不在队列里」
    唯一需要的信息。
    """
    if isinstance(item, dict):
        value = item.get("prompt_id") or item.get("id") or ""
        return str(value) if value else ""
    if isinstance(item, (list, tuple)):
        if len(item) > 1 and isinstance(item[1], str) and item[1]:
            return item[1]
        # 兜底：形状不认得时，取第一个非空字符串（宁可多认，不要漏认）。
        for value in item:
            if isinstance(value, str) and value:
                return value
        return ""
    if isinstance(item, str):
        return item
    return ""


def queue_prompt_ids(payload) -> set[str]:
    """从 /queue 的响应里取出**全部** prompt_id（running + pending）。

    `payload` 是 GET /queue 的 JSON：
        {"queue_running": [...], "queue_pending": [...]}

    【为什么返回集合而不是个数】状态栏只要「在跑几个」，但 A5 要回答的是
    「**我这个**任务还在不在队列里」—— 那需要 prompt_id 级别的判定。

    【为什么对形状宽容】除了本机 ComfyUI 的元组形状，也兼容 dict 形状
    （中间代理可能把 JSON 重写过）。认不出就返回空集合 —— 上层会把它
    当成「ComfyUI 读到了但队列是空的」，而 ComfyUI **不可达**时上层
    拿到的是 None，两者含义不同。
    """
    ids: set[str] = set()
    if not isinstance(payload, dict):
        return ids
    for key in ("queue_running", "queue_pending"):
        items = payload.get(key) or []
        if not isinstance(items, (list, tuple)):
            continue
        for item in items:
            pid = _extract_prompt_id(item)
            if pid:
                ids.add(pid)
    return ids


# ------------------------------------------------------------------
# 核心判定
# ------------------------------------------------------------------

def classify(*, prompt_id: str, history_present: bool, queue_ids,
             now: float, started_at: float | None = None,
             timeout_sec: float = DEFAULT_TIMEOUT_SEC,
             grace_sec: float = DEFAULT_LOST_GRACE_SEC) -> str:
    """判定一个「声称在运行」的任务**真实**处于什么状态。

    参数：
      * `prompt_id`     —— ComfyUI 侧的 prompt id（没有就是空串）；
      * `history_present` —— /history/{prompt_id} 里已经有这条记录；
      * `queue_ids`     —— /queue 里全部 prompt_id；
                          **None 表示 ComfyUI 读不到（不可达）**；
      * `now` / `started_at` —— 时间基准（秒，epoch）；
      * `timeout_sec`   —— 运行上限；
      * `grace_sec`     —— 「刚提交」的宽限期。

    返回 STATE_* 之一。

    【判定顺序（顺序本身就是语义）】
      1. ComfyUI 里**已经有结果** -> FINISHED（worker 该收尾，不该再等）；
      2. 超过运行上限 -> TIMEOUT（哪怕它还在队列里 —— 那也是卡住了）；
      3. 队列读不到 -> UNKNOWN（**不能**当成队列为空，否则会误杀在跑的任务）；
      4. prompt_id 在队列里 -> ACTIVE（真的在跑）；
      5. 还在宽限期内 -> UNKNOWN（刚提交，可能还没进队列）；
      6. 其余 -> LOST（队列与历史都没有 = 真的丢了）。
    """
    ref = float(started_at) if started_at else float(now)
    elapsed = max(0.0, float(now) - ref)
    if history_present:
        return STATE_FINISHED
    try:
        limit = float(timeout_sec)
    except (TypeError, ValueError):
        limit = DEFAULT_TIMEOUT_SEC
    if limit > 0 and elapsed > limit:
        return STATE_TIMEOUT
    if queue_ids is None:
        return STATE_UNKNOWN
    if prompt_id and prompt_id in queue_ids:
        return STATE_ACTIVE
    try:
        grace = float(grace_sec)
    except (TypeError, ValueError):
        grace = DEFAULT_LOST_GRACE_SEC
    if elapsed < grace:
        return STATE_UNKNOWN
    return STATE_LOST


# ------------------------------------------------------------------
# 重启自愈：残留 running 该怎么办
# ------------------------------------------------------------------

def plan_residual_running(*, prompt_id: str, history_entry,
                           queue_ids, started_at: float | None = None,
                           now: float | None = None) -> dict:
    """决定「服务重启后残留的 status='running' 行」怎么处理。

    返回：
        {
          "action": "fail" | "finalize" | "adopt",
          "reason": 中文原因（action='fail' 时必填）,
          "state":  对应的 STATE_*,
        }

    三种动作：
      * `fail`     —— 判定为丢失：标记 failed 并写清原因（**这是绝大多数情况**）；
      * `finalize` —— ComfyUI 里**已经有结果**（服务重启期间跑完了）：
                     把产物落盘并标记 done / failed；
      * `adopt`    —— 任务**还在 ComfyUI 队列里**（服务重启期间 ComfyUI 没重启）：
                     接管它 —— 起一个观察任务，等 ComfyUI 出结果再收尾。
                     不接管 = 任务永远卡在「运行中」（这就是要修的坑）。
    """
    if not prompt_id:
        return {
            "action": "fail",
            "reason": RESTART_STALE_REASON,
            "state": STATE_LOST,
        }
    if history_entry:
        return {"action": "finalize", "reason": "", "state": STATE_FINISHED}
    if queue_ids is None:
        # ComfyUI 不可达 -> 判定不了。但**不能**让这一行继续「运行中」：
        # 那正是 A5 要消灭的无限运行。明确失败 + 说清原因。
        return {
            "action": "fail",
            "reason": COMFY_UNREACHABLE_REASON,
            "state": STATE_UNKNOWN,
        }
    if prompt_id in queue_ids:
        return {"action": "adopt", "reason": "", "state": STATE_ACTIVE}
    return {
        "action": "fail",
        "reason": RESTART_STALE_REASON,
        "state": STATE_LOST,
    }


# ------------------------------------------------------------------
# 下发给前端的「这条任务到底在不在跑」
# ------------------------------------------------------------------

#: 非运行态（任务根本没在跑）—— 前端不需要任何额外处理。
STATE_IDLE = ""

#: 优先级：越大越「严重」，用于在多个信号冲突时决定显示哪一个。
#:
#: 【为什么要优先级】「本地超时」与「ComfyUI 说还在队列里」可能同时成立
#: （worker 还没来得及把它标失败的那几秒）。这时候显示「已超时」更有用 ——
#: 因为它告诉了用户「这个任务已经不正常了，去 ComfyUI 看看」，
#: 而显示「运行中」会让用户继续干等。
_STATE_PRIORITY = {
    STATE_IDLE: 0,
    STATE_ACTIVE: 1,
    STATE_UNKNOWN: 2,
    STATE_TIMEOUT: 3,
    STATE_LOST: 4,
}


def job_running_truth(*, status: str, started_at: float | None = None,
                      now: float | None = None,
                      timeout_sec: float = DEFAULT_TIMEOUT_SEC,
                      live_state: str | None = None) -> dict:
    """算出「这条任务在不在真跑」—— 前端卡片直接消费这个结果。

    参数：
      * `status`      —— DB 里的 status；
      * `started_at`  —— 开始时间（epoch 秒）；
      * `now`         —— 当前时间（epoch 秒）；
      * `timeout_sec` —— 运行上限；
      * `live_state`  —— **实时观测**到的状态（STATE_ACTIVE / STATE_LOST /
                        STATE_UNKNOWN / STATE_TIMEOUT），
                        没有实时观测时传 None。

    返回：
        {
          "state":  "",              # 非运行态是空串
          "label":  "运行中（已确认在 ComfyUI 队列中）",
          "elapsed_sec": 12.3,       # 已跑多久
          "timeout_sec": 1800,       # 上限
          "deadline_at": 1794...     # 超时时刻（epoch 秒）
        }

    【为什么要「本地判定」+「实时观测」两层】
      * **本地判定**（status + started_at + timeout）不需要任何 IO ——
        每个接口都能算，**前端据此兜底**：超过上限仍在 running 的卡片
        必须显示「已超时」而不是永远「运行中」。
      * **实时观测**（查 ComfyUI 的 /queue）能进一步区分
        「真的在跑」与「ComfyUI 里已经没了」——
        但它需要一次 HTTP 往返，只对**正在跑的那一条**做（有且只有一条）。

    【优先级】lost > timeout > unknown > active。
    「丢失」最严重（任务已经不存在）；「超时」说明已经不正常；
    「未知」是 ComfyUI 读不到（不能瞎说）；「运行中」是正常态。
    """
    result = {
        "state": STATE_IDLE,
        "label": "",
        "elapsed_sec": None,
        "timeout_sec": float(timeout_sec or 0),
        "deadline_at": None,
    }
    if str(status or "") != "running":
        return result
    current = float(now) if now is not None else 0.0
    started = float(started_at) if started_at else None
    elapsed = max(0.0, current - started) if started else 0.0
    result["elapsed_sec"] = elapsed
    try:
        limit = float(timeout_sec or 0)
    except (TypeError, ValueError):
        limit = DEFAULT_TIMEOUT_SEC
    result["timeout_sec"] = limit
    if started and limit > 0:
        result["deadline_at"] = started + limit

    # 本地判定：超过上限就算「超时」，不再显示「运行中」。
    local_state = STATE_ACTIVE
    if limit > 0 and elapsed > limit:
        local_state = STATE_TIMEOUT

    state = local_state
    if live_state:
        # 实时观测覆盖本地判定，但**超时优先**于「运行中」——
        # 超过上限还在跑，已经是异常，必须让用户看见。
        candidates = [local_state, live_state]
        if STATE_TIMEOUT in candidates:
            state = STATE_TIMEOUT
        else:
            state = max(candidates, key=lambda s: _STATE_PRIORITY.get(s, 0))
    result["state"] = state
    result["label"] = state_label(state)
    return result

