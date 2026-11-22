# -*- coding: utf-8 -*-
"""生成队列的**执行槽**（execution slot）—— 全局唯一「运行中」的唯一真源。

【这个模块在防什么 —— 2026-11-22 用户报「多个任务同时显示进行中」】

ComfyUI 是**单执行槽**队列：任一时刻最多 1 个 prompt 处于 executing，
其余全是 pending。本项目**不得**按「并发 / 抢占」思路建模。

但界面里确实会出现「运行中 2 · 排队 2」。现场取证（2026-11-22）：

    ComfyUI GET /queue:
        queue_running = ["320785e3-..."]   <- 唯一 executing
        queue_pending = ["bff1cfe3-..."]   <- 排队中（**不是** executing）
    Studio DB (data/jobs.db):
        236115fbc8ad  status='running'  prompt_id=320785e3-...
        1cf86678c63d  status='running'  prompt_id=bff1cfe3-...   <- 实际在 pending
        a5a9dc222e96  status='queued'

也就是说：**Studio 说「运行中 2」，ComfyUI 实际 executing 只有 1 个。**

【根因（四层，逐层叠加）】

  L1. **同机多个 h3studio 实例共享同一个 data/jobs.db**
      现场 `ps` 实测有 3 个实例（8190 线上 / 8290 验收 / 临时 UI 实例），
      全部 `H3STUDIO_DATA_DIR=/root/projects/comfyui-minimax-h3-studio/data`。
  L2. **`Worker._next_queued()` 只保证「进程内」串行**
      它的 SQL 是 `SELECT ... WHERE status='queued' LIMIT 1`，
      **没有任何跨进程互斥**。两个实例同时跑这条 SQL，
      会各自取到不同的 queued 行、各自置成 running。
  L3. **Studio 把任务交给 ComfyUI 单执行槽后，多余的任务落进 pending**
      但 Studio 侧的 `jobs.status` 仍然是 'running' —— **声明**与**观测**脱钩。
  L4. **`running_truth.classify()` 把「在 ComfyUI running 或 pending」都判成 active**
      于是第二条（实际在 pending）也被判成「真的在跑」；
      前端再按 `job.status === 'running'` 计数 -> 「运行中 2」。

【本模块的职责 —— 单一真源】

把「现在到底谁在跑」变成**可判定、可测试**的纯逻辑 + 一张**跨进程**的
租约表：

  * `exec_slot.py` 提供**纯函数**：
      - `classify_queue_position()` —— ComfyUI 队列位置 -> 执行态；
      - `reconcile()` —— 把「Studio 声明的 running 集合」与「ComfyUI 观测到的
        executing 集合」收敛成**唯一**的执行态分配；
      - `queue_position_label()` —— 「排队中 · 第 N 位」文案。
  * `server.py` 提供**跨进程原子闸门**：
      `exec_slot` 表 + `BEGIN IMMEDIATE` 事务，保证**同一 data_dir 下
      任一时刻最多 1 条 running 记录**。

【设计约束 —— 与 running_truth / job_timeout 同一纪律】

  * **纯函数部分无 IO**：判定逻辑能离线穷举测试。
  * **不确定就说不确定**：ComfyUI 读不到时返回 None ->
    上层给「状态未知」，**绝不**当成「队列为空」
    （把「读不到」当成「空」= 误杀所有在跑的任务，比原 bug 更糟）。
  * **不静默改状态**：收敛校正只改**展示态**（exec_state），
    真正的 DB status 由 worker 自己的生命周期推进。
"""

from __future__ import annotations


# ------------------------------------------------------------------
# 执行态常量（前端 / 后端共用的唯一真源）
# ------------------------------------------------------------------

#: 唯一正在执行（ComfyUI 的 queue_running 里，且是 Studio 自己的任务）。
STATE_EXECUTING = "executing"

#: 排队中（ComfyUI 的 queue_pending 里，或 Studio 侧还没轮到）。
STATE_PENDING = "pending"

#: 已完成 / 失败 / 取消（终态）。
STATE_TERMINAL = "terminal"

#: 判定不了（ComfyUI 不可达 / 刚提交还没进队列）。
STATE_UNKNOWN = "unknown"

#: 全部执行态（前端只应把 executing 当成「运行中」）。
ALL_EXEC_STATES = (STATE_EXECUTING, STATE_PENDING, STATE_TERMINAL, STATE_UNKNOWN)


# ------------------------------------------------------------------
# ComfyUI 队列观测
# ------------------------------------------------------------------

def split_queue_prompt_ids(payload) -> tuple[set[str], set[str]]:
    """把 ComfyUI `/queue` 的响应拆成 (executing 集合, pending 集合)。

    `payload` 是 GET /queue 的 JSON：
        {"queue_running": [...], "queue_pending": [...]}

    【为什么必须区分 running 与 pending —— 2026-11-22 缺陷核心】
    ComfyUI 的两个队列语义**完全不同**：

      * `queue_running` —— **正在执行**的那一个（单执行槽，恒定 ≤ 1）；
      * `queue_pending` —— **还没开始**执行的（纯排队）。

    旧实现把两者合并成一个 `queue_prompt_ids()` 集合，
    于是「在 pending 里排队」和「在 running 里执行」被**同等对待** ——
    这正是「运行中 2」的判定层根因。

    【为什么认不出就返回空集合而不是抛异常】
    上层把空集合理解为「ComfyUI 读到了但队列是空的」，
    与「ComfyUI 不可达」（None）含义不同 —— 后者绝不能当成空。
    """
    running: set[str] = set()
    pending: set[str] = set()
    if not isinstance(payload, dict):
        return running, pending
    running = _ids_of(payload.get("queue_running"))
    pending = _ids_of(payload.get("queue_pending"))
    return running, pending


def _ids_of(items) -> set[str]:
    """从一条队列项里取出 prompt_id（宽容地兼容多种形状）。

    【ComfyUI 的真实形状（0.37，本机源码 server.py:1230 / execution.py:1321）】

        self.prompt_queue.put((number, prompt_id, prompt,
                               extra_data, outputs_to_execute, sensitive))

    而 /queue 端点会把 sensitive 去掉、只返回前 5 项
    （server.py:114 `_remove_sensitive_from_queue`）：

        [number, prompt_id, prompt, extra_data, outputs_to_execute]

    => **prompt_id 固定在下标 1**。这是判定「我的任务还在不在队列里」
    唯一需要的信息。
    """
    from .running_truth import _extract_prompt_id

    ids: set[str] = set()
    if not isinstance(items, (list, tuple)):
        return ids
    for item in items:
        pid = _extract_prompt_id(item)
        if pid:
            ids.add(pid)
    return ids


# ------------------------------------------------------------------
# 核心判定：把「声明的 running」与「观测到的队列」收敛
# ------------------------------------------------------------------

def reconcile_by_prompt(*, declared_running: list[dict],
                        declared_queued: list[str],
                        queue_running: set[str] | None,
                        queue_pending: set[str] | None) -> dict:
    """按 prompt_id 精确收敛（`reconcile` 的精确版）。

    参数：
      * `declared_running` —— [{"id": job_id, "prompt_id": pid, "created_at": t}, ...]
      * `declared_queued`  —— 排队中的 job id 列表（保持 FIFO 顺序）
      * `queue_running` / `queue_pending` —— ComfyUI 观测到的 prompt_id 集合
                              （None = 读不到）

    返回：
        {
          "executing": [job_id, ...],   # 至多 1 个
          "pending":   [job_id, ...],
          "unknown":   [job_id, ...],
          "terminal":  [job_id, ...],
          "positions": {job_id: n},     # pending 任务的队列位置（1 = 下一个就跑）
        }

    【为什么必须有这个「精确版」】
    `reconcile()` 只按声明顺序分配 —— 但**真正的执行者**必须由
    ComfyUI 的 queue_running 说了算（那才是真相）。
    这个版本按 prompt_id 精确匹配，ComfyUI 说谁在 executing 谁就是。
    """
    declared_running = [d for d in (declared_running or []) if d.get("id")]
    declared_queued = [str(j) for j in (declared_queued or []) if j]

    result = {"executing": [], "pending": [], "unknown": [],
              "terminal": [], "positions": {}}

    if queue_running is None:
        result["unknown"] = ([d["id"] for d in declared_running]
                             + list(declared_queued))
        return result

    executing_set = set(queue_running or set())
    pending_set = set(queue_pending or set())

    # 1) 找真正的执行者：declared_running 里 prompt_id ∈ queue_running 的。
    #    ComfyUI 单执行槽 -> 至多一条；若多条命中取**最早启动**的。
    hits = [d for d in declared_running if (d.get("prompt_id") or "") in executing_set]
    hits.sort(key=lambda d: (d.get("created_at") or 0.0))
    executor = hits[0] if hits else None

    # 2) ComfyUI 正在执行但**不是** Studio 的任务（用户直接在 ComfyUI 提交）
    #    -> Studio 侧没有任何 executing，声明的 running 全部降级为 pending。
    for d in declared_running:
        jid = d["id"]
        if executor is not None and jid == executor["id"]:
            result["executing"].append(jid)
            continue
        pid = d.get("prompt_id") or ""
        if pid and pid in pending_set:
            result["pending"].append(jid)
        elif pid and pid in executing_set:
            # 防御：多条命中 executing（ComfyUI 单槽下不应发生）
            result["pending"].append(jid)
        else:
            # 既不在 running 也不在 pending -> ComfyUI 侧已清掉。
            # 保守地归 PENDING（真正的 LOST 由 running_truth 单独判定）。
            result["pending"].append(jid)

    # 3) declared_queued 排在 pending 尾部（Studio 还没提交给 ComfyUI）。
    result["pending"].extend(declared_queued)

    # 4) 队列位置：executing 不占位置；pending 从 1 开始。
    for index, jid in enumerate(result["pending"], start=1):
        result["positions"][jid] = index
    return result


# ------------------------------------------------------------------
# 展示文案
# ------------------------------------------------------------------

def queue_position_label(position: int | None) -> str:
    """排队中任务的队列位置文案。

    >>> queue_position_label(1)
    '排队中 · 第 1 位'
    >>> queue_position_label(3)
    '排队中 · 第 3 位'
    >>> queue_position_label(None)
    '排队中'
    """
    if position is None:
        return "排队中"
    try:
        pos = int(position)
    except (TypeError, ValueError):
        return "排队中"
    if pos <= 0:
        return "排队中"
    return f"排队中 · 第 {pos} 位"


def executing_label() -> str:
    """唯一执行中的任务的中文文案。"""
    return "运行中"


def state_text(state: str, position: int | None = None) -> str:
    """执行态 -> 给用户看的中文。

    >>> state_text("executing")
    '运行中'
    >>> state_text("pending", 2)
    '排队中 · 第 2 位'
    >>> state_text("unknown")
    '状态未知'
    """
    key = str(state or "")
    if key == STATE_EXECUTING:
        return executing_label()
    if key == STATE_PENDING:
        return queue_position_label(position)
    if key == STATE_TERMINAL:
        return "已结束"
    return "状态未知"
