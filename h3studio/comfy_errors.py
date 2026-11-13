# -*- coding: utf-8 -*-
"""
ComfyUI 执行错误的**提取与中文化**。

【为什么需要这个模块 —— 2026-11-12 独立复验发现的缺陷】

用户点了「接续」之后又点「复用参数」（两张不同的卡片），提交后任务失败，
生成库卡片上显示的是：

    'str' object has no attribute 'get'

这不是模型的错误，也不是「用户参数填错」—— 这是一段**原始 Python
异常文本**，它对用户毫无意义。

【根因：ComfyUI 的消息形状被读错了】

ComfyUI 的 `PromptQueue.status_messages` 里每条消息的形状是
**二元组** `[事件名, 数据字典]`：

    ["execution_error", {
        "prompt_id": "...",
        "node_id": "300:bridge",
        "node_type": "H3RelayCopyBridge",
        "exception_message": "拷贝桥禁止跨分辨率：上段 768x448 ≠ 本段 1024x576\n",
        "exception_type": "ValueError",
        "traceback": [...],
        "current_inputs": {...},
        "current_outputs": [...],
    }]

而本项目原来的提取代码把它当成了「`m[0]` 是字典」：

    (m or [{}])[0].get("x", "")      # m[0] 其实是字符串 "execution_error"

于是 `.get()` 作用在 str 上 -> 抛 `AttributeError` ->
被 worker 的兜底 `except Exception as e: str(e)` 吞掉 ->
**真正的原因（拷贝桥跨分辨率）被彻底丢掉**，只剩一句 Python 报错。

【本模块的职责】

  1. 按 ComfyUI 的**真实**形状提取错误（`m[1]` 是数据字典）；
  2. 把英文/原始异常翻译成**用户可读的中文原因**（含真正原因）；
  3. 保留**完整技术细节**（节点类型、异常类型、traceback）供后端日志使用。

【设计约束】

  * **纯函数、无 IO**：不连 ComfyUI、不查盘。单元测试完全离线可跑
    （与 job_meta.py / continuation.py 同一纪律）。
  * **不吞信息**：中文原因里必须带上真正的技术原因（例如
    「拷贝桥禁止跨分辨率：上段 768x448 ≠ 本段 1024x576」），
    不能只给一句「生成失败」—— 那是把问题换了个地方藏着。
  * **未知错误不编造**：认不出来就退回到原始 exception_message，
    并明确标注「详细信息见服务端日志」，绝不臆造原因。
"""

from __future__ import annotations

import re


# ------------------------------------------------------------------
# ComfyUI 消息形状（单一真源）
# ------------------------------------------------------------------

#: ComfyUI `status.messages` 里每条消息的形状：`(事件名, 数据字典)`。
#:
#: 来源（本机 ComfyUI 0.37 源码）：
#:   comfy_extras/execution.py `PromptExecutor.add_message`:
#:       self.status_messages.append((event, data))
#:   comfy_extras/execution.py `handle_execution_error`:
#:       mes = {"prompt_id":..., "node_id":..., "node_type":...,
#:               "exception_message":..., "exception_type":..., ...}
#:       self.add_message("execution_error", mes, broadcast=False)
#:
#: 实测（ComfyUI /history 的 status.messages）：
#:   ["execution_error", {"node_type": "H3RelayCopyBridge",
#:                        "exception_message": "拷贝桥禁止跨分辨率：...",
#:                        "exception_type": "ValueError", ...}]
MESSAGE_EVENT_INDEX = 0

#: 数据字典在二元组里的位置。
MESSAGE_DATA_INDEX = 1

#: 执行错误的事件名。
EVENT_EXECUTION_ERROR = "execution_error"

#: 执行中断的事件名（用户主动取消 / 中断）。
EVENT_EXECUTION_INTERRUPTED = "execution_interrupted"

#: 取不到具体原因时给用户看的**兜底中文文案**。
#:
#: 【为什么是常量而不是各写各的】这条文案会出现在两处：
#:   * `comfy_errors.extract_error` 找不到错误时；
#:   * `server._comfy_error_message` 提取过程自身抛异常时。
#: 两处必须**说同一句话** —— 否则用户会看到两种口径的「失败了但不知道
#: 为什么」，无法判断是同一个问题。
#:
#: 【⚠ 这里绝不能出现原始 Python 异常文本】
#: 用户看到 `'str' object has no attribute 'get'` 是**没有任何信息量**的
#: —— 它是本项目的 bug，不是用户能修的东西。技术细节进服务端日志。
UNPARSED_ERROR_REASON = ("生成失败，但没能取到具体原因（ComfyUI 侧未返回"
                          "可解析的错误信息）。详细信息见服务端日志。")


# ------------------------------------------------------------------
# 已知错误的**中文映射**
# ------------------------------------------------------------------
#
# 【为什么要映射】ComfyUI 与 Relay-Kit 抛出来的异常里，一部分是
# 英文技术描述。用户看到的是「生成失败：ValueError」没有任何用处；
# 看到「接续失败：上一段与本段的分辨率不一致（768x448 ≠ 1024x576），
# 请改用「复用参数」重新生成一段」才有用。
#
# 【匹配方式】按 (节点类型, 异常文本片段) 匹配 —— 全部用**子串**匹配，
# 因为异常文本里常常带着路径、尺寸等变量部分。

#: (匹配条件, 中文原因模板)
#:
#: 匹配条件是 (node_type, substring) —— node_type 为 "" 表示不限节点。
#: 模板里的 `{detail}` 会被替换成去掉堆栈噪声的原始异常文本。
_ERROR_RULES: tuple[tuple[str, str, str], ...] = (
    # ---- Relay-Kit 拷贝桥（本项目接续链路上最常见的失败）----
    (
        "H3RelayCopyBridge", "禁止跨分辨率",
        "接续失败：上一段与本段的分辨率不一致（{detail}）。"
        "潜空间桥不能跨分辨率接续 —— 请让本段沿用上一段的尺寸，"
        "或改用「复用参数」重新生成一段。",
    ),
    (
        "H3RelayCopyBridge", "上下文帧",
        "接续失败：钉住窗口（{detail}）不符合 Relay-Kit 要求。"
        "请清掉接续上下文后重新点一次「接续」。",
    ),
    (
        "H3RelayCopyBridge", "stage_index",
        "接续失败：接续段号不合法（{detail}）。"
        "请清掉接续上下文后重新点一次「接续」。",
    ),
    (
        "H3RelayLatentLoad", "",
        "接续失败：读回上一段的潜空间文件失败（{detail}）。"
        "潜空间文件可能已被清理 —— 请改用「复用参数」重新生成一段。",
    ),
    (
        "H3RelayLatentSave", "",
        "接续失败：保存本段的潜空间文件失败（{detail}）。"
        "请检查 ComfyUI 的输出目录是否可写。",
    ),
    # ---- 通用节点失败 ----
    (
        "SaveAudioAdvanced", "missing 1 required positional argument",
        "音频保存失败：本机 ComfyUI 的 SaveAudioAdvanced 节点接口与本项目"
        "预期不一致（{detail}）。这是服务端与本机 ComfyUI 版本不匹配，"
        "请更新服务端或 ComfyUI。",
    ),
    (
        "", "out of memory",
        "显存不足（{detail}）。请降低输出分辨率或时长后重试。",
    ),
    (
        "", "CUDA out of memory",
        "显存不足（{detail}）。请降低输出分辨率或时长后重试。",
    ),
    (
        "", "no kernel image is available",
        "显卡驱动与本机 ComfyUI 使用的 CUDA 版本不匹配（{detail}）。",
    ),
    (
        "", "Prompt has no outputs",
        "工作流没有产出任何文件（{detail}）。请检查所选模式与参数。",
    ),
)


def _clean_detail(text: str) -> str:
    """把异常文本收拾成一句话（去掉尾部换行与堆栈噪声）。

    【为什么要把尺寸里的 x 换成 ×】Relay-Kit 的原始消息用 ASCII
    「768x448」，而本项目其它文案都用「768×448」。统一一下，
    用户看到的尺寸在两处长得一样。
    """
    value = str(text or "").strip()
    if not value:
        return ""
    # ComfyUI 的 exception_message 末尾带 "\n"，有时还带多行堆栈。
    # 只取第一行 —— 那一句才是真正的异常原因。
    first = value.splitlines()[0].strip()
    first = _normalize_size_separator(first)
    return first or value


#: 尺寸表达式里的 ASCII x -> 乘号。
#: 匹配「数字 + x + 数字」的三元组（Relay-Kit 的「上段 768x448 ≠ 本段 1024x576」）。
_SIZE_X_RE = re.compile(r"(\d)\s*[xX]\s*(\d)")


def _normalize_size_separator(text: str) -> str:
    """把「768x448」写成「768×448」（只改尺寸，不碰其它词）。"""
    return _SIZE_X_RE.sub(r"\1×\2", text)


def _match_rule(node_type: str, detail: str) -> tuple[str, str] | None:
    """按 (节点类型, 异常片段) 找匹配的中文规则。"""
    for want_node, needle, template in _ERROR_RULES:
        if want_node and node_type != want_node:
            continue
        if needle and needle not in detail:
            continue
        if not needle and not want_node:
            continue
        return template, detail
    return None


def _generic_message(node_type: str, exception_type: str, detail: str) -> str:
    """没有命中已知规则时的兜底文案。

    【原则】不编造原因，但必须**带上真正的技术原因**（detail），
    并指明详细信息在服务端日志里 —— 绝不只给「生成失败」。
    """
    where = f"（出错节点：{node_type}）" if node_type else ""
    kind = f"{exception_type}：" if exception_type else ""
    if detail:
        return f"生成失败：{kind}{detail}{where}。详细信息见服务端日志。"
    return f"生成失败：{kind}未知错误{where}。详细信息见服务端日志。"


def describe_exception(node_type: str, exception_type: str,
                       exception_message: str) -> dict:
    """把一条 ComfyUI 执行错误变成「中文原因 + 技术细节」。

    返回：
      {
        "reason":  给用户看的中文原因（含真正原因）,
        "detail":  完整技术细节（异常类型 + 节点 + 原始文本）,
        "node_type": 出错节点,
        "exception_type": 异常类型,
        "raw":      原始 exception_message（未加工）,
      }
    """
    node = str(node_type or "").strip()
    kind = str(exception_type or "").strip()
    raw = str(exception_message or "")
    detail = _clean_detail(raw)

    matched = _match_rule(node, detail)
    if matched:
        template, d = matched
        reason = template.format(detail=d)
    else:
        reason = _generic_message(node, kind, detail)

    parts = []
    if kind:
        parts.append(f"exception_type={kind}")
    if node:
        parts.append(f"node_type={node}")
    if raw:
        parts.append(f"exception_message={raw.strip()}")
    return {
        "reason": reason,
        "detail": " | ".join(parts) if parts else detail,
        "node_type": node,
        "exception_type": kind,
        "raw": raw,
    }


# ------------------------------------------------------------------
# 从 ComfyUI 的 status 结构里提取错误
# ------------------------------------------------------------------

def coerce_status_info(status_info) -> dict:
    """把 `history["status"]` **安全地**变成一个 dict。

    【为什么需要这一层 —— 2026-11-13 修】

    ComfyUI 的 history 条目里，`status` 的类型**不保证**是 dict：

        {"prompt": [...], "outputs": {...}, "status": None}      # 正常
        {"prompt": [...], "outputs": {...], "status": {...}}     # 正常
        {"prompt": [...], "outputs": {...}, "status": "error"}  # <- 字符串！

    上游实现直接写 `status_info = hist.get("status") or {}`，
    紧接着 `status_info.get("status_str")` —— 当 `status` 是**字符串**时，
    `.get()` 作用在 str 上 -> `AttributeError: 'str' object has no
    attribute 'get'`。

    这个 AttributeError 发生在**错误处理路径上** —— 本来要把
    ComfyUI 的失败原因翻译成中文给用户，结果自己先崩了，
    用户最终只看到一句 Python 异常文本。

    【本函数的口径】
      * 是 dict -> 原样返回（**不复制**，避免大对象拷贝）；
      * 不是 dict（含 None / str / list）-> 返回**空 dict**，
        调用方会走「未取到具体原因」的兜底文案，
        **绝不**让类型错误冒到用户面前。
    """
    if isinstance(status_info, dict):
        return status_info
    return {}


def status_is_error(status_info) -> bool:
    """这条 history 是不是「执行失败」。

    同样对**类型**免疫：`status` 是字符串时返回 False（调用方走兜底
    文案），不会抛 AttributeError。
    """
    info = coerce_status_info(status_info)
    return str(info.get("status_str") or "") == "error"


def _iter_error_messages(status_info: dict | None):
    """从 `status.messages` 里逐条取出执行错误的数据字典。

    【为什么不能直接 `m[1]`】历史/第三方实现里也有把数据放在
    `m[0]` 的形状（`[{"x": ...}]`）。本函数**两种都认**，但认的是
    「哪个位置是 dict」—— 绝不假设固定下标，否则同样的 bug 会在
    另一种形状下再来一次。

    【事件名的位置也不假设】ComfyUI 真实形状是
    `["execution_error", {...}]`（事件名在 m[0]），但也有把事件名放在
    数据字典里的实现。所以：**只要这条消息里含一个 dict，就取那个 dict**，
    并单独判断事件名 —— 而不是靠固定下标猜。
    """
    if not isinstance(status_info, dict):
        return
    messages = status_info.get("messages") or []
    if not isinstance(messages, (list, tuple)):
        return
    for message in messages:
        if not isinstance(message, (list, tuple)) or not message:
            continue
        payload = None
        for item in message:
            if isinstance(item, dict):
                payload = item
                break
        if payload is None:
            continue
        # 事件名：优先看消息里有没有独立的字符串元素；
        # 没有就看数据字典里的 type/事件字段。
        event = ""
        for item in message:
            if isinstance(item, str):
                event = item
                break
        if not event:
            event = str(payload.get("event") or payload.get("type") or "")
        if event and event not in (EVENT_EXECUTION_ERROR,
                                   EVENT_EXECUTION_INTERRUPTED):
            # 不是执行错误/中断 —— 跳过（但如果 payload 里有
            # exception_message，仍按执行错误处理，见下）。
            if not payload.get("exception_message"):
                continue
            event = EVENT_EXECUTION_ERROR
        if not event:
            # 形状不明确，但 payload 里有异常文本 -> 当执行错误处理。
            if not payload.get("exception_message"):
                continue
            event = EVENT_EXECUTION_ERROR
        yield event, payload


def extract_error(status_info) -> dict:
    """从 ComfyUI 的 `status` 里提取**唯一**一条执行错误。

    返回（无错误时 `reason` 为空串）：
      {
        "reason":  给用户看的中文原因,
        "detail":  技术细节（供服务端日志）,
        "node_type": 出错节点,
        "exception_type": 异常类型,
        "raw":      原始 exception_message,
        "found":    是否真的找到了一条执行错误,
      }

    【为什么用「找到的第一条」而不是拼接所有】一个工作流失败时
    ComfyUI 可能推多条 execution_error（例如级联失败）。用户需要的是
    **第一条真正的根因** —— 后面那些通常是它的连带反应。

    【2026-11-13】**对类型免疫**：传进来的不是 dict（str / None / list）
    时返回「没找到」，不抛异常 —— 错误处理路径本身绝不能崩。
    """
    for event, payload in _iter_error_messages(coerce_status_info(status_info)):
        node_type = payload.get("node_type") or ""
        exception_type = payload.get("exception_type") or ""
        exception_message = payload.get("exception_message") or ""
        if not exception_message:
            # 中断类消息没有 exception_message。
            if event == EVENT_EXECUTION_INTERRUPTED:
                return {
                    "reason": "生成已被中断（可能是 ComfyUI 侧取消了任务）。",
                    "detail": f"node_type={node_type}",
                    "node_type": node_type,
                    "exception_type": "",
                    "raw": "",
                    "found": True,
                }
            continue
        described = describe_exception(node_type, exception_type,
                                       exception_message)
        described["found"] = True
        return described
    return {
        "reason": "",
        "detail": "",
        "node_type": "",
        "exception_type": "",
        "raw": "",
        "found": False,
    }


def error_from_history(history) -> dict:
    """从 ComfyUI 的 history 条目里提取错误。

    `history` 就是 `/history/{prompt_id}` 返回的那一个 prompt 的字典。

    【2026-11-13 修】`history["status"]` 的类型**不保证**是 dict ——
    可能是字符串。原实现 `history.get("status") or {}` 在 status 是
    字符串时会把字符串传下去，后面 `.get()` 就炸了。
    """
    if not isinstance(history, dict):
        return extract_error(None)
    return extract_error(history.get("status"))
