# -*- coding: utf-8 -*-
"""ComfyUI 执行结果的**成功判定**（纯函数层）。

【这个模块在防什么 —— 2026-11-19 缺陷 2】

ComfyUI 任务因显存不足（OOM）而 failed 时，本项目却提示「生成成功」，
预览图是一张全黑图。取证（线上 jobs.db + 本机 ComfyUI /history）：

  * 任务 `628d92cfc062` / `86db94915467`：
        status = 'done'
        error  = '显存不足（Allocation on device 0 would exceed allowed
                  memory. (out of memory)）。请降低输出分辨率或时长后重试。'
        outputs = []
  * 对应的 ComfyUI history 条目 `921b024c-…`：
        status.status_str = 'error'
        status.messages  = [['execution_error', {...}]]
        outputs          = {'301': {'text': [...]}}   <- **只有文本节点**

根因是**判定顺序**错了：worker 的轮询循环先看
`if hist.get("outputs"):`（ComfyUI 的 outputs **字典非空**），
再看 `status_str == "error"`。而 ComfyUI 在执行失败时**也会**往
outputs 里塞已执行完的节点输出（哪怕只是一个文本节点），
于是 `hist.get("outputs")` 非空 -> 直接走「成功」分支，
`_extract_outputs` 从中取不出媒体文件 -> `saved = []`
-> 任务被标成 `status='done'`、`outputs=[]` -> 前端显示「生成成功」
+ 一个空白（视觉上是黑）的预览区。

【本模块的职责】

把「这次执行到底算不算成功」变成**可判定、可测试**的纯函数：

  * `history_outcome()` —— 从 history 条目给出唯一结论；
  * `has_media_output()` —— outputs 里有没有**媒体**产物；
  * `outputs_are_real()` —— 产物文件**真的存在且非空**。

【三种结论 —— 顺序本身就是语义】

  * `pending`  —— ComfyUI 的 status **还没落定**（`status` 是 None），
                  任务**还在跑** -> 调用方应继续等，**不能**判失败；
  * `failed`  —— status 说失败（error）/ 形状畸形 / 跑完了但没产物；
  * `ok`      —— status 成功且**有**媒体产物。

【设计约束 —— 与 running_truth / comfy_errors 同一纪律】

  * **纯函数、无 IO**：判定逻辑不查盘、不连 ComfyUI。
    文件存在性由 `outputs_are_real` 校验（它只读文件系统的大小，
    不读内容）。单元测试完全离线可跑。
  * **失败优先**：status 失败时，**无论** outputs 里有什么，都判失败。
  * **产物必须真实存在**：只有输出名、文件缺失或大小为 0，都不算成功。
"""

from __future__ import annotations

import os

from . import comfy_errors


# ------------------------------------------------------------------
# 结果常量
# ------------------------------------------------------------------

#: ComfyUI 的 status 还没落定（`status` 是 None）—— 任务**还在跑**。
OUTCOME_PENDING = "pending"

#: 执行失败（ComfyUI 报 error / 状态畸形 / 跑完但没产物）。
OUTCOME_FAILED = "failed"

#: 执行成功，媒体产物齐全。
OUTCOME_OK = "ok"


#: ComfyUI 跑完但没产出任何媒体文件时的原因文案（用户可见）。
NO_OUTPUT_REASON = ("生成失败：ComfyUI 执行结束但没有产出任何文件"
                    "（工作流可能未接上保存节点，或相关节点被跳过）。"
                    "请检查工作流配置后重试。")

#: 产物缺失 / 为空时的兜底文案。
MISSING_OUTPUT_REASON = ("生成失败：ComfyUI 报告了产物，但文件缺失或为空"
                         "（下载不完整）。请重新提交生成。")


def has_media_output(history_entry) -> bool:
    """history 条目里有没有**媒体**产物（image / video / audio）。

    【为什么要专门判「媒体」】ComfyUI 失败时 outputs 里常有
    `{'301': {'text': [...]}}` —— 那是**文本**节点输出，不是媒体。
    它让 `hist.get("outputs")` 非空，从而骗过「先看 outputs 非空」
    的旧逻辑。所以这里必须按**媒体的键**（images / videos / gifs / audio）
    判定，而不是看字典非空。

    【口径来源】与 server._extract_outputs 读的是同一组键 ——
    两个地方读不同的键集就会出现「一边说有产物、一边取不到」。
    """
    outputs = (history_entry or {}).get("outputs") or {}
    if not isinstance(outputs, dict):
        return False
    for node_out in outputs.values():
        if not isinstance(node_out, dict):
            continue
        for key in ("images", "videos", "gifs", "audio"):
            value = node_out.get(key)
            if not value:
                continue
            items = value if isinstance(value, list) else [value]
            for item in items:
                if isinstance(item, dict) and item.get("filename"):
                    return True
    return False


def history_outcome(history_entry) -> dict:
    """判定一条 ComfyUI history 条目的执行结果。

    返回：
        {
          "outcome":    OUTCOME_OK / OUTCOME_FAILED / OUTCOME_PENDING,
          "failed":     bool,      # True = 必须判失败
          "pending":    bool,      # True = 还在跑，调用方应继续等
          "reason":     中文原因（成功 / 未落定时是空串），
          "status_info": dict,     # 归一后的 status（供调用方取错误详情）
          "has_media":  bool,      # history 里有没有媒体产物
        }

    【判定顺序 —— 顺序本身就是语义】
      1. `status` 是 None -> PENDING（ComfyUI 还没跑完，**继续等**）。
         ⚠ 这一条必须排在最前面：把「还在跑」误判成「失败」会把
         正常的长视频任务直接判死（那正是缺陷 1 要避免的）。
      2. `status` 不是 dict（畸形）-> FAILED（快速失败，别挂死）；
      3. `status_str == 'error'` -> FAILED（**无论** outputs 里有什么）；
      4. status 已落定但**没有媒体产物** -> FAILED（跑完了却没东西）；
      5. 否则 -> OK。
    """
    raw_status = (history_entry or {}).get("status") \
        if isinstance(history_entry, dict) else None
    status_info = comfy_errors.coerce_status_info(raw_status)
    base = {
        "outcome": OUTCOME_FAILED,
        "failed": False,
        "pending": False,
        "reason": "",
        "status_info": status_info,
        "has_media": has_media_output(history_entry),
    }
    # 1) status 还没落定 -> 还在跑。
    if raw_status is None:
        base["outcome"] = OUTCOME_PENDING
        base["pending"] = True
        return base
    # 2) status 存在但不是 dict -> 形状超预期 -> 快速失败。
    if not isinstance(raw_status, dict):
        base["outcome"] = OUTCOME_FAILED
        base["failed"] = True
        base["reason"] = comfy_errors.MALFORMED_STATUS_REASON
        return base
    # 3) ComfyUI 报执行失败（OOM / 节点报错）-> 失败，**优先于**产物判定。
    if comfy_errors.status_is_error(status_info):
        base["outcome"] = OUTCOME_FAILED
        base["failed"] = True
        base["reason"] = ""   # 具体原因由调用方用 status_info 提取
        return base
    # 4) status 已落定（成功）但没有媒体产物 -> 失败。
    if not base["has_media"]:
        base["outcome"] = OUTCOME_FAILED
        base["failed"] = True
        base["reason"] = NO_OUTPUT_REASON
        return base
    # 5) 成功且有媒体产物。
    base["outcome"] = OUTCOME_OK
    return base


def outputs_are_real(saved: list, out_dir=None) -> tuple[bool, list[str]]:
    """校验产物条目对应的文件**真的存在且非空**。

    参数：
      * `saved`   —— 产物条目列表，形如
                     [{"kind": "video", "file": "videos/x.mp4", ...}]；
      * `out_dir` —— 产物根目录（Path 或 str）。None 时只做「条目自身」的校验
                     （条目为空 / 没有 file 字段）。

    返回 `(ok, problems)`：
      * `ok`       —— 全部产物都真实存在且非空；
      * `problems` —— 每条问题的说明（用户可见的中文）。

    【为什么必须校验文件大小】ComfyUI 的 history 里「有输出名」
    不等于「文件真的下下来了」。实测出现过下载中断 -> 文件 0 字节 ->
    视频播放器显示一片黑 / 图片显示不出来。前端只拿到 outputs 元数据，
    没有办法自己发现这件事，所以服务端必须先验。

    【为什么不是「只要有一个产物存在就算成功」】一个批次可能产出
    多个文件，只要有一个缺失就是**部分失败** —— 用户拿到的成片是残缺的。
    这里采取「全部都要真」的口径。
    """
    problems: list[str] = []
    if not saved:
        return False, ["ComfyUI 未返回任何可下载的产物文件。"]
    for item in saved:
        if not isinstance(item, dict):
            problems.append("产物条目格式异常。")
            continue
        relative = str(item.get("file") or "")
        if not relative:
            problems.append("产物条目缺少文件路径。")
            continue
        if out_dir is None:
            # 没有根目录就只做条目级校验（调用方通常会传真实目录）。
            continue
        try:
            path = os.path.join(str(out_dir), relative)
        except (TypeError, ValueError):
            problems.append(f"产物路径非法：{relative}")
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            problems.append(f"产物文件缺失：{relative}")
            continue
        if size <= 0:
            problems.append(f"产物文件为空（0 字节）：{relative}")
    return (not problems), problems


def missing_output_reason(problems: list[str]) -> str:
    """把产物校验的问题列表变成一条中文原因。"""
    items = [str(p) for p in (problems or []) if str(p).strip()]
    if not items:
        return MISSING_OUTPUT_REASON
    detail = "；".join(items[:3])
    if len(items) > 3:
        detail += f"（另有 {len(items) - 3} 项）"
    return (f"生成失败：{detail}。"
            "请重新提交生成；若反复失败，请查看服务端日志。")
